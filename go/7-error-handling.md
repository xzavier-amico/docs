# 7. 错误处理哲学：error、wrap、panic 边界

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · 语言内核 ｜ **编号**：7 / 54
> **本章定位**：把「`error` 是值」这句话从口号变成你能用的工程能力——包装链、分类器、panic 边界，最后落到 Agent 最刚需的**重试分类学**。
> **读法**：第 1.3 节已经给过「三层解释」，这里不再重复论证，直接开工。读完请把 7.5 的重试分类表抄进你的 LLM 客户端。

---

## 7.1 引子：一次把 OpenAI 账单打穿的重试风暴

时间是凌晨 3:12。你的 Agent 服务在跑批：2 万个工单摘要。监控突然报 `5xx` 比例 40%，P99 从 1.8s 飙到 47s，OpenAI 账单 20 分钟涨了 380 美元。值班同事手忙脚乱地加了副本数，情况更糟。

最后定位到的代码是这样的：

```go
// ❌ 事故版本：只区分"有没有 err"，重试一切
for i := 0; i < 5; i++ {
	resp, err := client.Chat(ctx, req)
	if err == nil {
		return resp, nil
	}
	time.Sleep(time.Second) // 没有抖动，没有上限感知
}
```

这行 `if err == nil` 背后藏着三个致死错误：

1. **`context.Canceled` 也被重试了**。用户早就关了页面，ctx 已经取消，但代码还在原地重试 5 次——每次都瞬间失败，5 次空转后仍然返回错误。取消信号被彻底无视。
2. **429 被当成普通错误**。OpenAI 的限流窗口是分钟级的，1 秒重试等于往火上浇汽油；2 万个并发请求同步重试,直接把账号打进更高的限流档。
3. **400 参数错误也被重试了**。这类错误重试 1 万次也不会成功，但每次都要花掉 200ms 的往返 + token 计费。

> 🩸 **结论**：报错不可怕，**分不清错误的种类才可怕**。Go 的 `error` 之所以设计成"值"，就是为了让你能对错误做**分类**——这是本章唯一的主线。

---

## 7.2 直觉模型：`error` 就是一个两字接口

```go
// 就这么多，builtin/builtin.go 里的定义
type error interface {
	Error() string
}
```

它和 Java 的 `Throwable` 最大的区别，不在方法数量，而在**它不携带任何控制流能力**。一个 `error` 值不会"自动向上冒泡"，它只是返回值列表里的最后一个元素。

先建立三张脑图。

**图一：Java 的异常是"栈展开"，Go 的错误是"人工包装链"。**

```
Java：                             Go：
  A() ──throw──┐                    A() → (T, err) ── if err != nil { return wrap(err) }
  B() ← 栈展开  │                    B() ←──────────────┘
  C() ← catch   │                    C() ←──────────────┘
  (JVM 自己记着"我该跳到哪")          main: errors.Is(err, ErrRateLimited)?
                                      (旅程是代码写出来的，不是运行时猜的)
```

**图二：一个包装链在内存里长什么样（`fmt.Errorf("...: %w", err)` 的结果）。**

```
err (最外层) ──► *fmt.wrapError{ msg: "chat: tool call: rate limited", err: ─┐
                                                                            │
                ┌───────────────────────────────────────────────────────────┘
                ▼
   *fmt.wrapError{ msg: "tool call: http 429", err: ─┐
                                                     │
                ┌────────────────────────────────────┘
                ▼
        *openai.APIError{ HTTPStatusCode: 429, Code: "rate_limit_exceeded" }
                │
                ▼ Unwrap() == nil  ← 链在这里结束，这就是"根错误"
```

**图三：错误分类的四个动作，分别由四个函数负责。**

```
errors.Is(err, target)   → 沿链找「是不是这个东西」      (等值判定，可自定义 Is 方法)
errors.As(err, &target)  → 沿链找「有没有这个类型的家伙」 (类型断言，可自定义 As 方法)
errors.Unwrap(err)       → 只剥一层
errors.Join(errs...)     → 把多个错误缝成一棵"树"
```

这四张图记住，剩下的都是细节。

---

## 7.3 底层实现：`error` 的三种具体形态与 Is/As 的真实逻辑

### 7.3.1 `errors.New` 与寄生壳子

`errors/errors.go` 里就这么点东西：

```go
type errorString struct {
	s string
}

func (e *errorString) Error() string { return e.s }

func New(text string) error { return &errorString{text} }
```

注意 `New` 返回的是**指针**（`*errorString`），且 `errorString` 是**不可比较的**？不，它只有一个 `string` 字段，是可比较的——所以 `errors.New("x") == errors.New("x")` 会得到 `false`（两个不同指针），而 `errors.Is(err, sentinel)` 只在**同一个哨兵变量**上成立。这就是「哨兵错误必须导出成包级变量」的根本原因：

```go
// ✅ 对：包级唯一实例，调用方拿它做 errors.Is
var ErrRateLimited = errors.New("rate limited")

// ❌ 错：每次调用都是新指针，调用方永远 Is 不到
func check() error { return errors.New("rate limited") }
```

### 7.3.2 三种形态：哨兵（sentinel）/ 包装（wrap）/ 结构体负载（payload）

| 形态 | 定义方式 | 适合表达 | 调用方怎么判 | 代价 |
| --- | --- | --- | --- | --- |
| 哨兵错误 | `var ErrX = errors.New("x")` | 无附加上下文的**分类标签** | `errors.Is(err, ErrX)` | 一个全局变量，跨包共享 |
| 包装错误 | `fmt.Errorf("ctx: %w", err)` | 给已有错误**补上下文** | 直接透传，让最外层判 | 每次一层分配，栈是手工构造的 |
| 结构体负载 | `type XError struct{ Code int; ... }` + `Error()` | 需要**带字段**的错误（状态码、重试后延时） | `errors.As(err, &xerr)` | 需要导出类型，API 面变大 |

Go 标准库里三种形态各有代表：`io.EOF` / `fs.ErrNotExist` 是哨兵，`*url.Error` 是包装 + 负载，`*os.PathError` 是负载，`*json.SyntaxError` 是负载（带 `Offset int64`）。

**`fmt.Errorf` 的 `%w` 到底做了什么？** 在 `fmt/errors.go` 里它不会把错误"打印成字符串再拼起来"，而是造了一个可展开的节点：

```go
// fmt/errors.go（简化）
type wrapError struct {
	msg string
	err error
}
func (e *wrapError) Error() string  { return e.msg }
func (e *wrapError) Unwrap() error  { return e.err }
```

Go 1.20 起 `fmt.Errorf` 允许**多个 `%w`**，此时返回 `*fmt.wrapErrors`，它的 `Unwrap() []error` 返回切片——这是"错误树"的入口之一。

`%v` 和 `%w` 的差别只有一句话：**`%v` 把错误变成字符串（死信息），`%w` 保留可 `Unwrap` 的引用（活信息）**。你每写一个 `%v`，就把下游的 `errors.Is` 能力砍掉了。

### 7.3.3 `errors.Is` 的真实循环：为什么它能记住"取消"和"超时"

`errors/wrap.go` 里的核心循环（Go 1.20+，支持 `Unwrap() []error`）：

```go
func is(err, target error) bool {
	if target == nil {
		return err == target
	}
	isComparable := reflectlite.TypeOf(target).Comparable()
	for {
		if isComparable && err == target {           // ① 等值命中
			return true
		}
		if x, ok := err.(interface{ Is(error) bool }); ok && x.Is(target) { // ② 自定义判定
			return true
		}
		switch x := err.(type) {
		case interface{ Unwrap() error }:            // ③ 单链
			err = x.Unwrap()
			if err == nil {
				return false
			}
		case interface{ Unwrap() []error }:          // ④ 树（Join / 多 %w）
			for _, err := range x.Unwrap() {
				if is(err, target) {
					return true
				}
			}
			return false
		default:
			return false                             // ⑤ 到底了
		}
	}
}
```

逐行读出来的四个结论：

1. **只有目标可比较时才做 `==`**（`isComparable`）。这条保护让你可以放心地用带 `[]byte` 字段的错误类型当目标而不会 panic——但语义上它会退化成只走 ②③④。
2. **② 是钩子**。`context.DeadlineExceeded` 之所以能被 `errors.Is` 出来，是因为 `context` 定义了 `deadlineExceededError.Is(target error) bool`；`os.PathError` 的 `Is` 会转发到内部的 `Err`。
3. **④ 让 `errors.Join` 有意义**。`errors.Join(a, b, c)` 返回 `*joinError`（`errs []error`），`Is` 会**遍历所有分支**，任何一个命中就返回 `true`——这是批量任务里"3 个工具失败了 2 个"的标准表达。
4. **手写 `Is` 方法时千万别 `return target == e` 就完事**，那是默认行为；自定义场景是"我的 `ErrNotFound` 可以匹配 `fs.ErrNotExist`"这类跨包语义对齐。

`errors.As` 是同一套循环的变体，只是把等值判断换成了 `reflectlite` 赋值（这就是它比 `Is` 贵的原因，别在热路径上循环里调几千次）。标准库还留了 `As(any) bool` 钩子，`*url.Error` 之类可以自定义。

### 7.3.4 一个能跑的分类器骨架

```go
package main

import (
	"context"
	"errors"
	"fmt"
)

// 1) 哨兵：只表达"类别"，不带数据
var (
	ErrRateLimited = errors.New("rate limited")
	ErrUpstream    = errors.New("upstream error")
)

// 2) 负载：需要携带重试建议时用结构体
type APIError struct {
	Status int
	Body   string
	RetryAfter int // 秒
}

func (e *APIError) Error() string {
	return fmt.Sprintf("api error: status=%d body=%.64s", e.Status, e.Body)
}

func callUpstream() error {
	// 假装远端返回 429 + Retry-After: 20
	return &APIError{Status: 429, RetryAfter: 20}
}

func classify(err error) {
	var ae *APIError
	switch {
	case err == nil:
		fmt.Println("ok")
	case errors.Is(err, context.Canceled):
		fmt.Println("用户取消 → 立刻退出，不上报错误")
	case errors.Is(err, context.DeadlineExceeded):
		fmt.Println("超时 → 看是不是单次超时可重试")
	case errors.As(err, &ae):
		fmt.Printf("带负载的 API 错误: status=%d retryAfter=%ds\n", ae.Status, ae.RetryAfter)
	default:
		fmt.Println("未知错误 → 包装后上抛")
	}
}

func main() {
	err := fmt.Errorf("agent step 3: %w", callUpstream())
	classify(err) // 输出: 带负载的 API 错误: status=429 retryAfter=20s
	fmt.Println(err) // agent step 3: api error: status=429 body=
}
```

这段代码的关键不是语法，而是**包装顺序**：`%w` 在前、`As` 在后，信息一路不丢。如果中间某层写了 `fmt.Errorf("step 3 failed: %v", err)`，`errors.As` 就再也见不到 `*APIError` 了。

---

## 7.4 Java 对照：会猜错的四个地方

| 你的 Java 直觉 | Go 的真相 | 为什么 |
| --- | --- | --- |
| 异常能跨 10 层自动上抛 | 每层必须显式 `return ..., err` | 控制流降级为数据流，见 1.3 节第 3 层 |
| `catch (IOException e)` 靠类型分流 | `errors.Is`（值相等）+ `errors.As`（类型）双轨 | 因为错误可以被包装，`err.(*APIError)` 在包装后必然失败 |
| 自定义异常继承 `RuntimeException` 就够了 | 自定义类型要**同时实现 `Error()` 和 `Unwrap()`** | 不实现 `Unwrap` 就等于把链掐断 |
| `finally` 保证清理 | `defer` 保证清理（**但不保证在 panic 时返回值可用**） | `defer` 在栈展开时执行，命名返回值可被改写 |

还有一条认知需要纠正：**Java 的 `cause` 链是一条链，Go 1.20 之后是"森林"**。`errors.Join` 和多个 `%w` 让一个错误可以同时有多个父节点，`errors.Is/As` 会做树遍历。这在批量执行（errgroup 收工、并行工具调用）时极其好用，但你不能再假设"一个错误只有一个 cause"。

**Go 为什么这样选？** Java 的异常体系把"分类"交给类型层次（`IOException extends Exception`），层次一旦定死就难改；Go 把分类交给**值 + 接口方法**，任何一层都能插入自定义匹配逻辑，且新增一个类别不需要改动已有代码——这是"开闭原则"在错误处理上的版本。

---

## 7.5 重试分类学：Agent 必背的一张表

这是本章最有生产价值的一节。LLM/工具调用链路里，错误必须分成三档：**立刻放弃**、**立刻重试**、**退避后重试**。

| 错误 | 判定方式 | 处置 | 理由 |
| --- | --- | --- | --- |
| `context.Canceled` | `errors.Is` | ❌ 放弃，且**不上报为错误** | 用户/上游主动取消，重试是纯浪费 |
| `context.DeadlineExceeded`（请求级） | `errors.Is` | ❌ 放弃 | 整个请求预算已耗尽，重试必然又超时 |
| deadline 但**是单次尝试超时** | 自建 `perAttemptCtx` | ✅ 重试 | 长 prompt 首 token 慢，值得再来一次 |
| HTTP 400 / 422 | `As` 取 `StatusCode` | ❌ 放弃 | 请求本身有病，重试 1 万次也一样 |
| HTTP 401 / 403 | 同上 | ❌ 放弃，**告警** | 密钥/权限问题，重试只会加速被封 |
| HTTP 404 | 同上 | ❌ 放弃 | 模型名/工具名写错了 |
| HTTP 408 / 425 | 同上 | ✅ 立即重试 1 次 | 服务端自己承认"我没处理" |
| HTTP 429 | 同上 + `Retry-After` | ⏳ 退避 + **读 `Retry-After`** | 有明确窗口，1s 重试等于自残 |
| HTTP 5xx（500/502/503/504） | 同上 | ⏳ 指数退避 + 抖动，上限 3 次 | 上游抖动 |
| 连接错误 / `ECONNRESET` / `io.ErrUnexpectedEOF` | `errors.Is` + `net.Error.Timeout()` | ⏳ 即时重试 1~2 次 | 幂等请求可安全重放 |
| SSE 流中途断开 | 自己包装的 `ErrStreamBroken` | ⏳ 有限重试（**别重放已扣费的部分**） | 只有"没收到任何 token"才可无脑重放 |
| JSON 解码失败 / schema 校验失败 | 自定义 `ErrBadOutput` | ✅ 重试，但**改 prompt** | 用同一个 prompt 重试是同一堵墙 |
| `sql.ErrNoRows` | `errors.Is` | ❌ 放弃，当业务分支处理 | 不是错误，是"没有" |

三条铁律，写进团队规范：

1. **重试必须在包装链上判，不在字符串里判**。`strings.Contains(err.Error(), "429")` 早晚把你送进事故复盘会。
2. **重试必须带抖动**（jitter），否则 2 万个 goroutine 会在同一个时钟点整齐地打过去，这就是引子里账单爆炸的物理原因。
3. **重试必须有 ctx 检查**。每一次 `select { case <-ctx.Done(): return ctx.Err() }` 都是省下来的钱。

```go
// 一个能直接用的分类 + 退避骨架（伪代码级，换成你自己的 client）
func shouldRetry(err error, attempt int, maxAttempt int) (bool, time.Duration) {
	if err == nil || attempt >= maxAttempt {
		return false, 0
	}
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return false, 0
	}
	var ae *APIError
	if errors.As(err, &ae) {
		switch {
		case ae.Status == 429:
			base := time.Duration(ae.RetryAfter) * time.Second
			if base == 0 {
				base = time.Second
			}
			return true, base + jitter(base/2)
		case ae.Status == 408, ae.Status == 425, ae.Status >= 500:
			return true, backoff(attempt) // 1s, 2s, 4s...
		default:
			return false, 0 // 4xx：放弃
		}
	}
	var ne net.Error
	if errors.As(err, &ne) && ne.Timeout() {
		return true, backoff(attempt)
	}
	return false, 0 // 未知错误：不重试，上抛让人看
}
```

> 💡 一个反直觉的经验：**"未知错误 → 不重试"比"未知错误 → 重试"安全得多**。未知意味着你无法预判副作用，对 LLM 这种按 token 收费的依赖尤其致命。

---

## 7.6 panic 边界：库永远不该 panic，但边界必须有人兜

`panic` 在 Go 里的正式定位是：**程序进入了不可恢复的非法状态**。数组越界、nil map 写、整数除零、显式 `panic("impossible")`——都属此类。

判断标准一句话：**"这个错误如果重试 100 次、换个输入、换个时间点，会不会还是同一个错误而且没人能修？"** 是 → panic 可以考虑；否 → `error`。

几条硬规则：

1. **库代码不许 panic 表达业务失败**。你无法预料调用方会不会 `recover`，一旦它没兜住，整个进程死掉。标准库里的 `MustXxx`（`regexp.MustCompile`、`template.Must`）是**显式的例外**：命名里已经告诉你"这会 panic"，且只该用在 `init`/包级变量初始化这种"启动即失败"的位置。
2. **panic 只在"进程边界"被 recover**：HTTP handler（`net/http` 自己就会 recover 并打日志 + 关连接）、goroutine 入口、插件/工具执行沙箱、消息消费者的单条消息处理。
3. **recover 之后要重新上抛或明确降级**，绝不能 `recover(); return nil` 把事故静默掉——那是把 stack trace 扔进黑洞里。
4. **`panic(nil)` 从 Go 1.21 起不再返回 nil**：会被包装成 `*runtime.PanicNilError`，避免 `if r := recover(); r != nil` 漏判。想恢复旧行为要 `GODEBUG=panicnil=1`（不建议）。

```go
// 工具执行沙箱：一个工具的 panic 不能带走整个 Agent 进程
func (r *Registry) safeCall(ctx context.Context, name string, args json.RawMessage) (out Result, err error) {
	defer func() {
		if p := recover(); p != nil {
			// 把 panic 变成 error，交给上层/LLM 决定怎么办
			err = fmt.Errorf("tool %s panicked: %v\n%s", name, p, debug.Stack())
		}
	}()
	return r.mustGet(name).Call(ctx, args)
}
```

> ⚠️ 注意 `defer` 里的 `recover()` **只在被 defer 的直接函数里有效**：套一层 `func(){...}()` 调用链后就抓不到了。这个行为由 `runtime.gorecover` 检查 `argp` 是否等于当前 `_panic.argp` 实现，机制详见第 8 章。

panic 的运行时实现（`runtime/panic.go` 的 `gopanic`、`_panic` 链表、`recovery` 恢复栈）在第 8 章完整拆解，这里只记住边界策略。

---

## 7.7 框架与工程落地

**1. `x/sync/errgroup`：并发的错误聚合器。** 它做的事就两件：第一个非 nil 错误时取消 ctx，`Wait()` 返回那个错误。

```go
g, ctx := errgroup.WithContext(ctx)
g.SetLimit(8) // Go 1.20+：限制并发度，天然背压
for _, t := range tools {
	t := t
	g.Go(func() error {
		_, err := t.Call(ctx, args)
		return err // 返回第一个错误 → 其余任务 ctx 取消
	})
}
if err := g.Wait(); err != nil { /* 只有一个错误能上来 */ }
```

注意它的取舍：**只保留第一个错误**。如果你需要"3 个都失败，告诉我 3 个原因"，就别用 `errgroup` 的返回值，自己用 `sync.Mutex` + `errors.Join` 收集。详见第 26 章。

**2. go-openai / openai-go 的错误分层。** `sashabaranov/go-openai` 把错误分成两类：`*openai.APIError`（服务端返回了 JSON 错误体，带 `HTTPStatusCode`、`Code`、`Type`）和 `*openai.RequestError`（连请求都没发成功，带 `HTTPStatusCode` 与底层 `Err`）。`openai/openai-go`（官方 v1 系）则是 `*openai.Error`，带 `StatusCode`、`Code`、`Request`/`Response`。两者都够你实现 7.5 的分类器。**实现细节随版本演进，以你本地 `go doc` 为准**。

**3. langchaingo 的 `llms.Model` 接口**把错误当作普通返回值贯穿（`GenerateContent(ctx, msgs, opts...) (string, error)`），链式调用（chain）里每一环都只做"包装 + 透传"。这是 Go 生态里最典型的写法：**业务层不做分类，分类只在最外层或最需要决策的那一层做**。

**4. 可观测性落点。** 错误在上报时应该变成结构化字段，而不是塞进字符串：

```go
slog.ErrorContext(ctx, "agent step failed",
	"err", err,
	"err_type", fmt.Sprintf("%T", errors.Unwrap(err)), // 根错误类型
	"retryable", retryable,
	"step", step,
)
```

---

## 7.8 踩坑清单（CR 可直接抄）

| # | 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- | --- |
| 1 | 日志里错误信息全是一层，看不到根因 | 中间层用 `%v` 或 `err.Error()` 重新构造 | `fmt.Errorf("doing x: %w", err)` | 7.3 |
| 2 | `errors.Is` 永远返回 false | 哨兵错误是 `errors.New` 现造的，或链被 `%v` 截断 | 哨兵做成包级变量 + `%w` 贯穿 | 7.3 |
| 3 | `errors.As` 明明类型对却失败 | 目标参数不是指针的指针，或链已断 | `var e *MyErr; errors.As(err, &e)` | 7.3 |
| 4 | 重试把用户取消吞了 | 没判 `context.Canceled` | 分类器第一分支就判取消 | 7.5 |
| 5 | 429 越重试越糟 | 没有抖动、没读 `Retry-After` | 退避 + jitter + 尊重服务端指示 | 7.5 |
| 6 | `err == nil` 但后面 nil 解引用崩了 | 返回了 `(nil *T, err)` 的 typed nil 塞进 error 接口 | 出错时显式 `return nil, err` | 第 5 章 |
| 7 | 库代码 panic 打挂整个服务 | 用 panic 表达业务失败 | 返回 error；只在边界 `recover` | 7.6 |
| 8 | 并发任务只报一个错，其他错误消失 | `errgroup` 只留第一个 | 需要全量错误时用 `errors.Join` 自行收集 | 第 26 章 |
| 9 | 错误字符串被上层当 API 解析 | 用 `strings.Contains(err.Error(), ...)` | 加哨兵或自定义 `Is` 方法 | 7.3 |

---

## 7.9 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| `error` 的本质 | 只有一个 `Error() string` 方法的接口，是值，不是控制流 |
| 哨兵错误 | 包级 `errors.New` 变量，`errors.Is` 判定分类 |
| `%w` vs `%v` | `%w` 保留可 `Unwrap` 的引用，`%v` 把错误变成死字符串 |
| `errors.Is` | 沿链（含 `Unwrap() []error` 树）做等值 + 自定义 `Is` 判定 |
| `errors.As` | 沿链做类型匹配，比 `Is` 贵，热路径少用 |
| `errors.Join` | Go 1.20，把多个错误缝成树，批量任务的标准输出形态 |
| panic | 只用于不可恢复的非法状态；库不许 panic，边界必须 recover |
| `MustXxx` | 唯一被允许的 panic 惯例，仅限初始化期 |
| 重试分类 | 取消/4xx → 放弃；429 → 退避+`Retry-After`；5xx/网络 → 指数退避+抖动 |
| 未知错误 | 默认不重试，上抛给人看 |

---

## 7.10 自检题

1. **`fmt.Errorf("load config: %v", err)` 与 `%w` 的区别，除了"能不能 Unwrap"，还有别的影响吗？**
   > 要点：影响整条错误链的**可编程性**——`errors.Is/As` 全部失效，重试分类器退化成字符串匹配；同时也影响可观测性（根错误类型丢失）与日志聚合（错误指纹被 msg 打散）。用 `%v` 等于主动放弃下游的所有决策能力。

2. **工具 A panic 了，工具 B 返回 `context.DeadlineExceeded`，工具 C 返回 429。你如何一次性把三个都汇报给上层？**
   > 要点：`errors.Join(errA, errB, errC)` 得到一个"错误树"，`Error()` 用换行拼接便于打日志；判定时 `errors.Is` 会遍历所有分支，所以能同时命中 `context.DeadlineExceeded`。注意 `Join` 的 `Unwrap() []error` 才是关键，单链 `Unwrap() error` 表达不了这种情况。

3. **为什么 Go 社区说"未知错误不要重试"，这跟"提高可用性"矛盾吗？**
   > 要点：不矛盾。重试的前提是**幂等 + 已知可恢复**。未知错误的副作用不可预判（可能已扣费、可能已写库），盲目重试会把偶发故障放大成数据事故。可用性应该由"已知可重试集合的精确覆盖 + 快速失败 + 降级"共同保证，而不是靠无差别重试。

---

## 7.11 延伸阅读

- `$GOROOT/src/errors/errors.go`、`$GOROOT/src/errors/wrap.go`（`is`、`as`、`Join`、`joinError`）
- `$GOROOT/src/fmt/errors.go`（`wrapError`、`wrapErrors`）
- `$GOROOT/src/context/context.go`（`deadlineExceededError` 的 `Is`/`Timeout` 方法）
- Go 官方博客 *Working with Errors in Go 1.13*（`%w` 与 `Is/As` 的设计动机）
- Go 1.20 Release Notes：`errors.Join` 与多 `%w`
- `golang.org/x/sync/errgroup` 文档；`cenkalti/backoff/v4`、`avast/retry-go/v4`
- 下一章：[8. 函数、闭包与 defer/panic 实现](8-funcs-closures-defer.md)

---

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：第 6 章 [泛型](6-generics.md)；错误处理的动机铺垫见 [第 1 章 1.3 节](1-mindset-shift.md)
- 相关：第 5 章「nil 接口」（`err != nil` 成立但内部为 nil 的真相）、第 8 章（`gopanic`/`recover` 实现）、第 26 章（`errgroup` 与结构化并发）
- 下一章：[8. 函数、闭包与 defer/panic 实现](8-funcs-closures-defer.md)
