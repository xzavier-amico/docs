# 24. context：取消传播（Agent 的命脉）

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 并发与调度器源码 ｜ **编号**：24 / 54
> **本章定位**：`context` 是 Go 里唯一一个"横切所有 API 边界"的类型。它解决的不是"并发怎么跑"，而是**"怎么让一整棵树上的 goroutine 同时停下来"**。对 Agent 来说这不是可选项：一次对话就是一个取消树，而取消树断了，你就是在烧钱。
> **读法**：24.2 的树和图要建立直觉；24.3 是本章的硬核（`cancelCtx` / `propagateCancel` 的五条出口）；24.5 是落地清单（http / sql / gRPC / WS 四种穿透）。前置：第 23 章的 happens-before（`close(done)` 为什么是广播）。

---

## 24.1 引子：用户点了"停止"，账单还在涨

一个客服 Agent 服务，前端有"停止生成"按钮。用户点击时，前端断开 SSE 连接。后端很"规范"地写了：

```go
func (h *Handler) Chat(w http.ResponseWriter, r *http.Request) {
	msgs := decode(r)
	reply, err := h.llm.Complete(context.Background(), msgs) // ← 这里
	if err != nil { ... }
	_, _ = w.Write(reply)
}
```

问题在于那个 `context.Background()`。它让整条下游调用链**变成一个永远不会被取消的世界**。于是：

- 用户断开 → `net/http` 取消了 `r.Context()` → 没人听，`Complete` 继续跑。
- 后端继续向 LLM 供应商发请求、继续计费；工具（查订单、调 CRM）继续执行，甚至产生**真实副作用**。
- 更糟的是重试逻辑：客户端断开后浏览器自动重连，后端又起一轮，两轮并行烧同一笔预算。
- 一周后账单涨了 40%，你翻遍 DAG 也找不到 bug——因为 bug 是"**少写了一个参数**"。

这是 context 最典型的事故形态：**它不是"多线程安全"的问题，而是"控制信号没有接上电线"**。这一章讲的就是这根电线怎么接、为什么这么接、断在哪。

---

## 24.2 直觉模型：一棵只往下传的树 + 四个方法

`context.Context` 是个四方法接口，非常小：

```go
type Context interface {
	Deadline() (deadline time.Time, ok bool) // 有 deadline 吗？
	Done() <-chan struct{}                   // 关闭即"该收工了"
	Err() error                              // 为什么收工：Canceled / DeadlineExceeded
	Value(key any) any                       // 请求作用域的数据
}
```

关键心智模型：**Context 是一棵树，信号只能从父往子传，不能反向，也不能横向**。

```
                        Background()            ← 根，永不取消（Done() == nil）
                             │
                 WithValue(tenantID)            ← 值节点，无取消能力
                             │
                WithTimeout(3s)  ← HTTP 入口     ← timerCtx：3 秒后自己关
                        ┌────┴────┐
              WithCancel │         │ WithTimeout(500ms)
                 (LLM)   │         │  (并行工具)
                         │         └──────────┬──────────┐
                    WithValue(userCtx)      tool1       tool2
                         │
                     子 goroutine...

  取消方向：父 → 子（父 Done 关闭 ⇒ 所有子 Done 关闭），单向、自动、无遗漏
  值方向：子 → 父（子查不到就往父找），Value 是"向上查找的链"，不是 map
```

四个方法的坑，先摆出来：

| 方法 | 语义 | 常见误用 |
| --- | --- | --- |
| `Done()` | 返回一个**只读 channel**，第一次 cancel 时 close；不可取消的 ctx 返回 `nil` | 反复调 `Done()` 拿新 channel（其实返回同一个）；对 nil 做 `<-ctx.Done()` 会永久阻塞（本身正确，但别指望它能被唤醒） |
| `Err()` | `Done` 未关时返回 nil；关闭后返回 `Canceled` 或 `DeadlineExceeded` | 用它判断"是不是超时"时忘了 `errors.Is`；把 `Canceled` 当 500 错误上报 |
| `Deadline()` | 只有根上设了才有值 | 自己算超时而不看父剩余预算 → 子预算比父还长，白设 |
| `Value()` | 沿链**向上**线性查找 | 拿它传业务参数、传 DB 连接、传 logger 实例；用 `string` 当 key |

`Value` 的本质是"链表查找"，不是哈希，所以：**链条越深，`Value` 越慢（O(深度)）**；而且每加一层 `WithValue` 就多一次分配。规范对此的措辞很硬：「Use context Values only for request-scoped data that transits processes and APIs, not for passing optional parameters to functions.」

---

## 24.3 底层实现（`src/context/context.go`）

### 24.3.1 `cancelCtx`：一个 mutex + 一张 children 表

```go
type cancelCtx struct {
	Context // 嵌父：Value / Deadline 靠它向上代理

	mu       sync.Mutex
	done     atomic.Value          // of chan struct{}，懒创建，首次 cancel 时 close
	children map[canceler]struct{} // 首次 cancel 时置 nil
	err      error                 // 首次 cancel 时置非 nil
	cause    error                 // 首次 cancel 时置非 nil（1.21 的 WithCancelCause）
}

// Done 是"懒创建 + 双检"，快路径一次 atomic.Load
func (c *cancelCtx) Done() <-chan struct{} {
	d := c.done.Load()
	if d != nil {
		return d.(chan struct{})
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	d = c.done.Load()
	if d == nil {
		d = make(chan struct{})
		c.done.Store(d)
	}
	return d.(chan struct{})
}
```

两个版本事实：

- **1.21 之前** `done` 是普通的 `chan struct{}` 字段（由 `mu` 保护 + 一个 `sync.Once`），`Done()` 要抢锁；1.21 起改成 `atomic.Value`，让 `Done()` 在热路径上无锁——因为 `select { case <-ctx.Done() }` 会被调几百万次。
- 有些 ctx 根本不会被取消（`valueCtx` 只是包装），`Done()` 走到底会返回 `nil`。`select` 里 `case <-nil:` 就是"这个分支永远不就绪"（第 21 章），**这正是"没有取消能力"的正确表达**。

### 24.3.2 `propagateCancel`：五条出口，其中一条会起 goroutine

这是整章最值得读的函数：父 ctx 有各种实现（标准库的、三方的、被 `WithValue` 包过的），子 ctx 必须在这三种情况下都正确地挂上去。

```go
func (c *cancelCtx) propagateCancel(parent Context, child canceler) {
	c.Context = parent

	done := parent.Done()
	if done == nil {
		return // ① 父不可取消（Background/TODO/WithoutCancel）→ 什么都不用挂
	}

	select {
	case <-done:
		child.cancel(false, parent.Err(), Cause(parent)) // ② 父已取消 → 立刻取消自己
		return
	default:
	}

	if p, ok := parentCancelCtx(parent); ok {
		// ③ 快路径：父确实是标准的 *cancelCtx（或包着它的 *timerCtx）
		p.mu.Lock()
		if p.err != nil {
			child.cancel(false, p.err, p.cause)
		} else {
			if p.children == nil {
				p.children = make(map[canceler]struct{})
			}
			p.children[child] = struct{}{} // 挂进 map，父取消时遍历它
		}
		p.mu.Unlock()
		return
	}

	if a, ok := parent.(afterFuncer); ok {
		// ④ 父实现了 AfterFunc（1.21+）→ 用它的钩子注册，不占额外 goroutine
		...
		return
	}

	goroutines.Add(1)
	go func() { // ⑤ 兜底：父是"非标准实现"，只能开个 goroutine 盯着
		select {
		case <-parent.Done():
			child.cancel(false, parent.Err(), Cause(parent))
		case <-child.Done():
		}
	}()
}
```

`parentCancelCtx` 是怎么判断"父是标准的"？它用 `parent.Value(&cancelCtxKey)` 找到最内层的 `*cancelCtx`，**再比对 `parent.Done()` 是否与它的 done channel 是同一个**。如果不一致，说明有人在中间套了个自定义实现（覆盖了 `Done()`），这时不能走快路径。

> 🩸 **最阴的一个泄漏**：路径 ⑤ 那个 goroutine，只有在父被取消**或者子自己被取消**时才退出。如果你写了一个自定义 Context（比如为了加 trace 信息，手写 struct 实现 `Context` 但没实现 `AfterFunc`），那么在这个 ctx 上 `WithTimeout` 出来的每一个子 ctx 都会**多一个 goroutine**；一旦你忘了 `cancel()`，子永远不结束、goroutine 永远不退出，还抓着整棵子树不放。**结论：不要自己实现 `Context` 接口；要加数据用 `WithValue`，要加行为用 `context.AfterFunc`。**

### 24.3.3 `cancel`：关一扇门，唤醒所有人

```go
func (c *cancelCtx) cancel(removeFromParent bool, err, cause error) {
	if err == nil { panic("context: internal error: missing cancel error") }
	if cause == nil { cause = err }
	c.mu.Lock()
	if c.err != nil { // 只生效一次
		c.mu.Unlock()
		return
	}
	c.err = err
	c.cause = cause
	d, _ := c.done.Load().(chan struct{})
	if d == nil {
		c.done.Store(closedchan) // 还没人问过 Done，直接塞一个全局已关闭 channel
	} else {
		close(d) // ← 一次 close，所有等待者同时被唤醒
	}
	for child := range c.children {
		// NOTE: acquiring the child's lock while holding parent's lock.
		child.cancel(false, err, cause)
	}
	c.children = nil // 断开引用，防止 parent 泄漏整棵子树
	c.mu.Unlock()

	if removeFromParent {
		removeChild(c.Context, c) // 从父的 children map 里把自己摘掉
	}
}
```

几处设计要点：

1. **`close(d)` 而不是发值**。发值只能叫醒一个接收者，`close` 是**广播**，而且规范保证 close happens-before 那次"返回零值的接收"（第 23 章）。这是 `ctx` 能被任意多 goroutine 同时监听的根本原因。
2. **递归用"父锁里调子锁"**。源码注释直说了这一点（`NOTE: acquiring the child's lock while holding parent's lock`）；因为传播方向严格单向（父→子），不会形成环，所以不会死锁。
3. **`c.children = nil`** 是防泄漏的关键一行。取消后父子关系断开，否则一个活着的父 ctx（比如长连接的 root）会持有整棵已死的子树。
4. **只生效一次**：`if c.err != nil` 直接返回，所以 `defer cancel()` 多调几次无害（幂等），这点跟 Java 的 `Future.cancel` 一样。

取消传播的时序：

```
T0  cancel(ctxB, cause=ErrToolFailed) 被某个工具 goroutine 调用
    │
    ├─ ctxB.mu.Lock()
    ├─ ctxB.err/cause 写入
    ├─ close(ctxB.done)          ──► 所有 <-ctxB.Done() 的 goroutine 同时就绪
    ├─ for child := range children { child.cancel(false, err, cause) }
    │      ├─ ctxC.mu.Lock() → close(ctxC.done) → 递归它的 children …
    │      └─ （单向：子永远不会反过来取消父）
    ├─ ctxB.children = nil
    └─ ctxB.mu.Unlock()
        │
T0+ε    removeChild(父, ctxB)：父的 map 里删掉 ctxB（避免父泄漏子）

注意：close 是"同时"的，但 goroutine 被调度执行是"随后"的；
所以 cancel 返回 ≠ 所有下游都已停下（CancelFunc 明确文档化："does not wait for the work to stop"）
```

最后那句非常重要：**`cancel()` 不等待**。所以在优雅退出的场景里，你需要"cancel 之后再用 `WaitGroup`/errgroup 等 drain"（第 26 章）。

### 24.3.4 `timerCtx`：`WithTimeout` 的真实结构

```go
type timerCtx struct {
	cancelCtx             // 复用 Done/Err/children
	timer    *time.Timer  // Under cancelCtx.mu.
	deadline time.Time
}

func WithDeadlineCause(parent Context, d time.Time, cause error) (Context, CancelFunc) {
	if cur, ok := parent.Deadline(); ok && cur.Before(d) {
		// 父的 deadline 更早 → 新的 d 毫无意义，退化成纯 WithCancel（省一个 timer！）
		return WithCancel(parent)
	}
	c := &timerCtx{deadline: d}
	c.cancelCtx.propagateCancel(parent, c)
	if dur := time.Until(d); dur <= 0 {
		c.cancel(true, DeadlineExceeded, cause) // 已经过期了
		return c, func() { c.cancel(false, Canceled, nil) }
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.err == nil {
		c.timer = time.AfterFunc(dur, func() {
			c.cancel(true, DeadlineExceeded, cause) // 定时器到期 → 走同一条 cancel 路径
		})
	}
	return c, func() { c.cancel(true, Canceled, nil) }
}
```

三个必须知道的细节：

- **`WithTimeout` 就是 `WithDeadline(now+timeout)`**，没有别的魔法。
- **父的 deadline 更早时不会新建 timer**（那个 `cur.Before(d)` 分支），所以"层层设超时"不一定会增加 timer 数量——但**每一层仍然会分配一个 `timerCtx` 并挂进父的 children map**。
- **不调 `cancel()` 的后果是双重的**：timer 不 `Stop`（1.23 前会一直挂在时间堆上）+ `timerCtx` 一直挂在父的 children map 里。这就是 `go vet` 有专门一条 `lostcancel` 检查的原因：「Failing to call the CancelFunc leaks the child and its children until the parent is canceled.」

### 24.3.5 `valueCtx`：一条链表，不是 map

```go
type valueCtx struct {
	Context
	key, val any
}

func (c *valueCtx) Value(key any) any {
	if c.key == key { return c.val }
	return value(c.Context, key) // 向上找
}
```

`value()`（小写）是个 `for` 循环的特化版本，按类型 switch 逐层向上（`*valueCtx` / `*cancelCtx` / `*timerCtx` / `emptyCtx`），**深度 O(n)、比较用 `==`**。两个后果：

- key 必须**可比较**，否则 `WithValue` 直接 panic（`!reflectlite.TypeOf(key).Comparable()`）。
- 深链 + 热路径调 `Value` 是真实开销。一圈中间件套 10 层 `WithValue`，每个请求查一次 tenant 就是 10 次接口比较。**要用就在入口查一次存局部变量**。

### 24.3.6 `WithCancelCause`（1.21）与 `Canceled` vs `DeadlineExceeded`

```go
ctx, cancel := context.WithCancelCause(parent)
cancel(myErr)          // 记录 cause
ctx.Err()              // 永远是 context.Canceled
context.Cause(ctx)     // 返回 myErr
```

规则很干净：**`Err()` 只回答"哪一类"（取消 / 超时），`Cause()` 回答"具体为什么"**。`WithCancelCause` / `WithDeadlineCause` / `WithTimeoutCause` 只在**第一次**取消时写 cause，之后不可覆盖（文档给了父子先后取消的完整规则表）。

`DeadlineExceeded` 是个特殊类型（不是 `errors.New`）：

```go
type deadlineExceededError struct{}
func (deadlineExceededError) Error() string   { return "context deadline exceeded" }
func (deadlineExceededError) Timeout() bool   { return true }
func (deadlineExceededError) Temporary() bool { return true }
```

那个 `Timeout() bool` 让它满足老式的 `net.Error` 接口——所以 `errors.Is(err, context.DeadlineExceeded)` 和"判断 `Timeout()`"两条路都能走通（第 7 章的错误分类学）。

| 情形 | `ctx.Err()` | `context.Cause(ctx)` | 对外该返回什么 HTTP 码 |
| --- | --- | --- | --- |
| 用户主动取消/断开 | `context.Canceled` | 通常也是 `Canceled`，或你自定义的 cause | **499**（客户端关闭）或直接不计错误 |
| 上游 deadline 到 | `context.DeadlineExceeded` | `DeadlineExceeded` 或自定义 cause | **504** |
| 你的中间件因"预算超支"取消 | `context.Canceled` | 你的 `ErrBudgetExceeded` | **429/402**（业务语义） |
| 下游服务超时 | `Canceled`（如果父被取消）或下游自己的 error | 看 `%w` 链 | 502/504 |

---

## 24.4 Java 对照：ThreadLocal 不是它的等价物

| Go | Java 里你会想到的 | 关键差异 |
| --- | --- | --- |
| `context.WithValue` | `ThreadLocal` + MDC | **ThreadLocal 是隐式的、线程绑定的、自动继承（InheritableThreadLocal 才继承且只在创建时拷贝）**；ctx 是**显式传参**、可任意分叉、跨线程、跨进程（作为 metadata）。ThreadLocal 在线程池里是**泄漏源**（线程复用 → 值不清理），ctx 没有这个问题 |
| `ctx.Done()` / `cancel()` | `Future.cancel(true)` | `Future.cancel` 只是**标记 + 尝试 interrupt**，能不能停完全看任务配合；Go 的 cancel 是 close 一个 channel，所有下游 `select` 立刻醒——**取消是一等语义，不是尽力而为** |
| 取消传播 | `CompletableFuture` 的取消 | `CompletableFuture` **完全不做**父子传播：cancel 一个 future 不会 cancel 它的上游或下游；而 `ctx` 的取消是**全树传播**且自动 |
| 超时 | `ExecutorService.invokeAll(tasks, timeout)` | `invokeAll` 超时会 cancel 所有未完成任务（有传播，但只在这一次批量调用内）；`ctx` 的超时会传播到任意深度、任意库 |
| 结构化取消 | Java 21+ 的 `StructuredTaskScope`（JEP 453，预览） | 这是 Java 唯一**接近** ctx 的东西：作用域退出时自动取消子任务。但它是"作用域内"传播，Go 的 ctx 是"参数级"传播（可以跨服务边界、可以只传一半） |
| `request-scoped` 上下文 | Spring 的 `RequestContextHolder` | 靠线程绑定 + 拦截器清理；异步/虚拟线程下容易错乱。Go 里这是**编译器级别的显式参数**，忘了传立刻看得见 |

> 🧠 **Go 为什么不用 ThreadLocal**：因为 goroutine 会被复用到不同 P 上、一个 goroutine 会派生一堆子 goroutine、还有"隐式继承"的语义陷阱。把作用域数据做成**一个显式参数**，代价是每个函数多一个参数，收益是**没有隐藏状态、没有清理问题、可以做静态检查**（`go vet lostcancel`、各种 linter 都在查这个参数）。这是"显式优于隐式"（第 1 章价值观 #1）在并发上的直接体现。

---

## 24.5 工程落地：取消怎么穿透四个关键依赖

### 24.5.1 四件套穿透表

| 依赖 | 接法 | 底层发生了什么 | 坑 |
| --- | --- | --- | --- |
| **net/http（服务端）** | `r.Context()` | `conn.serve()` 为每个请求派生可取消 ctx（值里有 `ServerContextKey`、`LocalAddrContextKey`）；客户端断开时 `connReader` 后台读发现并 cancel | 不要用 `context.Background()` 替换它；写长响应（SSE）时 `WriteTimeout` 会掐断，要 `ResponseController.SetWriteDeadline` |
| **net/http（客户端）** | `http.NewRequestWithContext(ctx, …)` | 取消时关闭连接（不能复用），`Transport` 里未完成的 `wantConn` 也被取消 | 用 `http.Client.Timeout` 会**无视 ctx 的 deadline 语义**，两者别混着瞎设 |
| **database/sql** | `QueryContext` / `ExecContext` / `PrepareContext` | 取消时 driver 尝试 cancel，并可能把连接**丢弃**（因为状态不可知） | 取消后不要复用同一事务；高频取消会打穿连接池，要配 `SetMaxOpenConns` |
| **gRPC** | `NewStream(ctx, …)` / 每个 `Invoke(ctx, …)` | ctx 取消发 `RST_STREAM`；deadline 通过 **`grpc-timeout` header 自动传给对端** | 对端的超时必须 <= 你的剩余预算，否则等于没有传播；`codes.DeadlineExceeded` 要单独处理 |
| **WebSocket** | **没有标准接法**，自己起 watcher | `conn` 不认 ctx，必须由外部 `Close()` 打断阻塞的 `Read` | 见下面的模式 |

### 24.5.2 WebSocket：ctx → 连接的单向桥

```go
// coder/websocket：ctx 不会自动打断 Read，必须有人去 Close
func pump(ctx context.Context, conn *websocket.Conn) error {
	stop := make(chan struct{})
	go func() {
		select {
		case <-ctx.Done():
			// ctx 语义 → 协议语义：给对端一个体面的 close code
			_ = conn.Close(websocket.StatusGoingAway, "context canceled")
		case <-stop:
		}
	}()
	defer close(stop) // 正常退出时别让 watcher 泄漏

	for {
		_, data, err := conn.Read(ctx) // 这一行才是真正能被打断的点
		if err != nil {
			return err
		}
		_ = data
	}
}
```

这个模式（`ctx.Done()` → `Close()`，外加一个 `stop` channel 收拾 goroutine）是所有"不认 ctx 的老 API"的通用接法：**把 ctx 翻译成它能听懂的动作**。

### 24.5.3 实战：一次请求 → 多次 LLM 调用 + 并行工具 → 全链路超时

```go
// go run main.go —— 演示预算分配、并行工具的"一损俱损"、cause 传递
package main

import (
	"context"
	"errors"
	"fmt"
	"time"
)

var ErrBudgetExceeded = errors.New("token 预算超支")

func callLLM(ctx context.Context, prompt string) (string, error) {
	select {
	case <-time.After(120 * time.Millisecond):
		return "answer:" + prompt, nil
	case <-ctx.Done():
		return "", fmt.Errorf("llm: %w", ctx.Err())
	}
}

func runTool(ctx context.Context, name string) (string, error) {
	select {
	case <-time.After(300 * time.Millisecond):
		return name + " ok", nil
	case <-ctx.Done():
		return "", fmt.Errorf("tool %s: %w", name, ctx.Err())
	}
}

func handle(userCtx context.Context, prompt string) (string, error) {
	// ① 本轮总预算：带 cause，方便日志和对外区分"超时"还是"预算"
	ctx, cancel := context.WithTimeoutCause(userCtx, 500*time.Millisecond, ErrBudgetExceeded)
	defer cancel() // 必须！否则 timerCtx 挂到 userCtx 上直到 userCtx 取消

	// ② 工具组：任一失败，整组立刻收摊
	toolCtx, cancelTools := context.WithCancelCause(ctx)
	defer cancelTools(nil)

	type res struct{ out string; err error }
	ch := make(chan res, 2) // 有缓冲：保证 goroutine 一定能退出，不会泄漏
	for _, name := range []string{"search", "calc"} {
		go func(name string) {
			out, err := runTool(toolCtx, name)
			if err != nil {
				cancelTools(fmt.Errorf("工具 %s 失败: %w", name, err))
			}
			ch <- res{out, err}
		}(name)
	}

	// ③ 主循环调 LLM：和工具并行，共享同一个总预算
	answer, err := callLLM(ctx, prompt)
	if err != nil {
		return "", fmt.Errorf("主循环失败: %w（cause=%v）", err, context.Cause(ctx))
	}

	for i := 0; i < 2; i++ {
		r := <-ch
		if r.err != nil {
			return "", r.err
		}
	}
	return answer, nil
}

func main() {
	// 场景 1：预算充足
	out, err := handle(context.Background(), "hello")
	fmt.Printf("场景1 out=%q err=%v\n", out, err)

	// 场景 2：用户 100ms 就点了"停止"（上游取消）
	uctx, ucancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer ucancel()
	out, err = handle(uctx, "hello")
	fmt.Printf("场景2 out=%q err=%v cause=%v\n", out, err, context.Cause(uctx))
}
```

这段代码里有四个必须记住的工程规则：

1. **每个 `With*` 后面必须 `defer cancel()`**，包括成功路径。`go vet` 会抓，但更多时候它藏在 `if err != nil { return }` 的分支里。
2. **并行子任务要有自己的 ctx 变量**（`toolCtx`），这样"工具组失败"只影响工具，不影响主 LLM 调用；反过来总预算一断，两边都停。
3. **结果 channel 必须带缓冲**（容量 = goroutine 数）。否则主循环提前 return 时，剩下的 goroutine 会永久阻塞在 `ch <-`，这就是第 25 章"泄漏四型"里的第二型。
4. **超时预算要分层分配**：总预算 500ms，工具最多给 300ms，LLM 给 400ms（要留出 100ms 给序列化和网络）。别每层都写 5s——那等于没设。

| 层 | 预算 | 依据 |
| --- | --- | --- |
| 网关/入口 | 8s | 前端超时 + 1s 余量 |
| 一次对话 | 6s | 用户可容忍等待 |
| 单个 LLM 调用 | 3s | 供应商 p99 × 1.5 |
| 单个工具 | 800ms | 内部服务 SLO × 1.2 |
| DB / 缓存 | 200ms | 单查询 p99 |

---

## 24.6 踩坑清单（CR 可直接抄）

| # | 现象 | 原因 | 正确写法 |
| --- | --- | --- | --- |
| 1 | 客户端断开后后端继续烧 token | 用了 `context.Background()` | 一路传 `r.Context()`；每个下游调用都收 ctx |
| 2 | goroutine 单调增长、`pprof goroutine` 里一堆卡在 `ctx.Done()` | `WithTimeout` 没有 `defer cancel()` | 每个 `With*` 立刻 `defer cancel()`；开 `go vet` 的 `lostcancel` |
| 3 | 中间件里 `WithValue` 10 层，CPU 里 `context.value` 可见 | `Value` 是 O(深度) 链式查找 | 入口查一次存局部变量；别把业务参数塞 ctx |
| 4 | `WithValue` 在运行期 panic | key 不可比较（如 `[]byte`、含 map 的 struct） | key 用私有类型 `type key struct{}` 或指针 |
| 5 | `ctx` 存进 struct 字段，跨请求复用 | 违反规范：ctx 应该是第一个参数 | `func Do(ctx context.Context, ...)` |
| 6 | 空指针 panic：`ctx.Done()` on nil | 传了 nil ctx | 不确定就用 `context.TODO()`；所有 API 显式拒绝 nil |
| 7 | 把 `context.Canceled` 当 500 上报，监控一片红 | 没有区分取消/超时 | `errors.Is(err, context.Canceled)` → 4xx 或忽略；`DeadlineExceeded` → 504 |
| 8 | 自定义 Context 实现后 goroutine 泄漏 | 走了 `propagateCancel` 的兜底 goroutine 路径 | **不要自己实现 Context**；要加数据用 `WithValue`，要加行为用 `context.AfterFunc` |
| 9 | 后台任务需要"脱离请求取消" | 用了 `context.Background()` 丢掉所有值 | `context.WithoutCancel(ctx)`（1.21）：保留值、断开取消 |
| 10 | `cancel()` 之后立刻认为下游已停 | `CancelFunc` 明确"不等待" | cancel 后用 `WaitGroup`/errgroup 等 drain（第 26 章） |

---

## 24.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 本质 | 一棵**单向向下传播**的取消树 + 一条**向上查找**的值链 |
| 四个方法 | `Deadline` / `Done` / `Err` / `Value`，就这 4 个，没有第 5 个 |
| `cancelCtx` | `sync.Mutex` + `done atomic.Value` + `children map[canceler]struct{}` |
| 取消动作 | **`close(done)` 广播** + 递归 children + `children = nil` + 从父摘除 |
| `cancel()` | **幂等**，且**不等待**下游停止（要 drain 请用 WaitGroup/errgroup） |
| `propagateCancel` | 5 条出口；最后一条会**起 goroutine 兜底**（自定义 Context 是泄漏源） |
| `timerCtx` | 就是一个带 `*time.Timer` 的 `cancelCtx`；父 deadline 更早则不建 timer |
| `valueCtx` | 链表，O(深度) 查找，key 必须可比较；只放 request-scoped metadata |
| `WithCancelCause` | 1.21 起；`Err()` 说"哪一类"，`Cause()` 说"为什么" |
| 穿透 | http：`r.Context()`/`NewRequestWithContext`；sql：`*Context` 系列；gRPC：自动传 `grpc-timeout`；WS：自己起 watcher→`Close()` |
| 铁律 | ctx 是**第一个参数**；不许存 struct；每个 `With*` 都 `defer cancel()` |

---

## 24.8 自检题

1. **`cancel()` 返回之后，你能确定所有下游 goroutine 都已经停下来了吗？优雅退出该怎么做？**
   > 要点：不能。`CancelFunc` 文档明确写了"does not wait for the work to stop"；`cancel` 只做 close + 递归 + 摘除引用，goroutine 什么时候被调度执行是另一回事。正确做法是"取消 + 等待"：用 `errgroup.WithContext` 或 `WaitGroup` 收口，等所有在途任务返回（第 26 章的优雅退出范式）。

2. **为什么说"自己实现 `Context` 接口"是危险操作？`propagateCancel` 里哪条路径会因此泄漏？**
   > 要点：`parentCancelCtx` 通过 `parent.Value(&cancelCtxKey)` 找到内层 `*cancelCtx` 后还要比对 `Done()` 是否是同一个 channel；自定义实现的 `Done()` 是新的 channel，于是匹配失败，代码落到兜底分支：`go func(){ select { case <-parent.Done(): ... case <-child.Done(): } }()`。这个 goroutine 只在父或子被取消时退出，一旦你忘了 `cancel()`，它就永久泄漏，还抓着整棵子树。所以：加数据用 `WithValue`，加副作用用 `context.AfterFunc`，不要实现 `Context`。

3. **一个请求链路：网关 8s → 对话 6s → LLM 3s，但工具调用只设了 100ms。用户侧 p99 却看到大量 504，日志里 `ctx.Err()` 是 `DeadlineExceeded`。怎么定位？**
   > 要点：先分清是哪一层超时——用 `context.Cause(ctx)` 拿到自定义 cause（`WithTimeoutCause`），或在每层结束后记录 `ctx.Deadline()` 的剩余值打点。常见根因：(a) 工具 100ms 太短导致 `cancelTools` 触发，把整组取消，主循环看到的是 `Canceled` 而不是上游超时；(b) 层间没有共享同一个 ctx，各层各自 `WithTimeout` 但某一层用了 `context.Background()` 导致预算没传下去；(c) 外层 `http.Server.WriteTimeout` 掐断，与 ctx 无关。定位手段：把 `ctx.Err()` + `context.Cause(ctx)` + 剩余 deadline 一起打进日志，并按层做直方图。

---

## 24.9 延伸阅读

- `$GOROOT/src/context/context.go` —— 全文不到 800 行，`cancelCtx` / `propagateCancel` / `parentCancelCtx` / `value` 四个函数值得逐行读
- `$GOROOT/src/net/http/server.go` —— `conn.serve` 里的 `ctx, cancelCtx := context.WithCancel(ctx)`、`backgroundRead` 如何检测断开
- `$GOROOT/src/database/sql/sql.go` —— `ctxDriverPrepare` / `watchCancel`：取消如何变成 driver 的 cancel 调用
- `grpc-go` 的 `internal/transport/http2_client.go` —— `grpc-timeout` header 的编解码与 deadline 传播
- 官方博客 *Go Concurrency Patterns: Context*（Sameer Ajmani）与 *Contexts and structs*
- Go 1.21 release notes —— `WithCancelCause` / `AfterFunc` / `WithoutCancel` / `context.Cause`

## 24.10 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 23 章 Go 内存模型](23-memory-model.md)（`close` 为什么是广播）、[第 21 章 select](21-select-timeout.md)（`<-ctx.Done()` 的等待机制）
- 下一章：[第 25 章 并发陷阱](25-concurrency-pitfalls.md)
- 相关：[第 26 章 结构化并发](26-structured-concurrency.md)（errgroup 与优雅退出）、[第 38 章 net/http 深潜](38-net-http.md)、[第 39 章 SSE/WebSocket](39-realtime-sse-websocket.md)、[第 43 章 Agent 循环](43-agent-loop-orchestration.md)
