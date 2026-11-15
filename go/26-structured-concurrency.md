# 26. 结构化并发：errgroup、conc 与有界并发

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 并发与调度器源码 ｜ **编号**：26 / 54
> **本章定位**：这是并发部分的收尾章，也是**唯一一章"以工程模式为主"**的。前面五章把机制讲透了，这一章回答一个实践问题：**怎么让"一堆并发任务"变成一个有边界、能取消、失败即收摊的"作用域"**。
> **读法**：26.2 建立"结构化并发"的直觉（这是本部分的纲）；26.3 读 `errgroup` 的 60 行源码（值得，它短且典范）；26.5 是 Agent 场景的实战编排 + 优雅退出。前置：第 24 章 ctx、第 25 章四型泄漏。

---

## 26.1 引子：10 万条文档，"全部 go 出去"直接打爆供应商

一个 RAG 管线的索引任务：10 万条文档要算 embedding。第一版实现极其"Go 风格"：

```go
var wg sync.WaitGroup
for _, doc := range docs {
	wg.Add(1)
	go func(doc Doc) {
		defer wg.Done()
		v, err := embedder.Embed(ctx, doc) // 一条文档一个 HTTP 请求
		if err != nil {
			log.Println(err) // 只打日志，继续跑
			return
		}
		store(doc.ID, v)
	}(doc)
}
wg.Wait()
```

上线 90 秒后：

- 供应商返回 **429 Too Many Requests**（我们的并发量是它的 800 倍）；
- 本机 `ulimit -n` 打满，`http.Transport` 抛出 `dial tcp: too many open files`；
- 内存涨到 24GB（10 万个 goroutine × 每请求的 body 缓冲 + 连接 + TLS 状态）；
- 更糟的是**无法停止**：任务跑了 6 分钟才发现数据源有一半是脏数据，你连"取消"的把手都没有。

重写后是 40 行：

```go
g, gctx := errgroup.WithContext(ctx)
g.SetLimit(8) // 8 路并发 ≈ 供应商 QPS × 平均延迟，一次只放 8 个请求出去
for _, d := range docs {
	g.Go(func() error { return indexOne(gctx, d) }) // 满了就阻塞，这就是背压
}
return g.Wait() // 第一个错误 → 自动取消 gctx → 其它 goroutine 立刻收摊
```

差别不在代码量，在于**并发被"结构化"了**：有上界（`SetLimit`）、有作用域（`g.Wait()` 之前必须全部结束）、有取消（首个错误自动 cancel）、有错误语义（`Wait` 返回第一个错）。

---

## 26.2 直觉模型：结构化并发 = "goroutine 的作用域"

Java 程序员对"结构化"这个词其实很熟：`try-with-resources`、`synchronized` 块、`StructuredTaskScope`。核心思想一句话：

> **并发的子任务必须有一个明确的"词法作用域"，作用域退出时所有子任务必须已经结束（正常结束或已被取消）。**

```
❌ 非结构化（Go 的默认形态，也是最容易写出来的）      ✅ 结构化（errgroup / conc / WaitGroup.Go）
                                                   
 main ──┬── go A ────► 谁也不知道 A 何时结束          main ──┬── g.Go(A) ─┐
        ├── go B ────► 错误被吞 / 泄漏                            ├── g.Go(B) ─┤ SetLimit 背压
        └── return ──► 函数返回了，A/B 还在跑                      └── g.Go(C) ─┘
                                                                   g.Wait() ◄─ 作用域在此闭合
                                                                    ↑ 任一失败 → 取消其余
```

三根支柱，缺一不可：

| 支柱 | Go 的载体 | 没有它会怎样 |
| --- | --- | --- |
| **闭合**：作用域退出前必须等所有子任务结束 | `errgroup.Wait()` / `sync.WaitGroup.Wait()` / `conc.WaitGroup` | goroutine 泄漏（第 25 章四型） |
| **取消**：一个失败/超时，其它全部收摊 | `errgroup.WithContext` + `context` | 白烧 token、白占连接、写脏数据 |
| **有界**：同时在飞的任务有上限 | `SetLimit` / `semaphore.Weighted` / 有界 channel / `rate.Limiter` | 打爆下游、OOM、fd 耗尽 |

> 💡 一句话记法：**`go` 是"启动"，`Wait` 是"句号"，`ctx` 是"撤回"，`SetLimit` 是"刹车"。** 只有 `go` 没有后三个，就等着第 25 章的事故。

---

## 26.3 底层实现：`errgroup` 只有 60 行，但每行都有讲究

`golang.org/x/sync/errgroup`（不在标准库，但几乎是事实标准；Go 1.25 给 `sync.WaitGroup` 补了 `Go` 方法，算是把"最简版"收进了标准库）：

```go
// golang.org/x/sync/errgroup/errgroup.go
type Group struct {
	cancel  func(error)   // 只有 WithContext 创建的 Group 才非 nil
	wg      sync.WaitGroup
	sem     chan token    // SetLimit 之后才非 nil，令牌通道 = 并发上限
	errOnce sync.Once     // 只记第一个错误
	err     error
}

func WithContext(ctx context.Context) (*Group, context.Context) {
	ctx, cancel := context.WithCancelCause(ctx) // 关键：1.21+ 用 Cause 记录首个错误
	return &Group{cancel: cancel}, ctx
}

func (g *Group) Go(f func() error) {
	if g.sem != nil {
		g.sem <- token{} // ← 背压：满了就在这里阻塞调用方
	}
	g.wg.Add(1)
	go func() {
		defer g.done()
		if err := f(); err != nil {
			g.errOnce.Do(func() { // 只有第一个错误生效
				g.err = err
				if g.cancel != nil {
					g.cancel(g.err) // 立刻取消 ctx → 其它任务看到 Done 关闭
				}
			})
		}
	}()
}

func (g *Group) done() {
	if g.sem != nil {
		<-g.sem // 归还令牌
	}
	g.wg.Done()
}

func (g *Group) Wait() error {
	g.wg.Wait()
	if g.cancel != nil {
		g.cancel(g.err) // 释放 ctx 资源（即使没有任何错误）
	}
	return g.err
}
```

逐段解读，六个判断：

1. **`errOnce` 而不是"最后一次"**：并发任务的错误语义必须是"**第一个**"，否则要么不确定、要么被后来的噪声覆盖。想拿全部错误就用 `errors.Join` 或 `conc` 的聚合错误（见下）。
2. **`cancel(g.err)` 用 `WithCancelCause`（1.21+）**：这样下游能用 `context.Cause(ctx)` 拿到**那个真实的错误对象**，而不是干瘪的 `context.Canceled`。这是 errgroup 最近一次重要升级——以前取消原因全丢了，日志里只剩 `context canceled`。
3. **`Go` 里的 `g.sem <- token{}` 是阻塞的**，所以 `SetLimit` 天然提供**背压**：生产任务的速度被消费速度限死。这跟 Java 有界队列 + `CallerRunsPolicy` 是一回事，但没有队列这个中间层。
4. **`Wait` 也会 `cancel`**：即使没有错误，`Wait` 返回前也会取消 ctx，释放 `cancelCtx`、timer、children map。所以**别把 `WithContext` 拿到的 ctx 传出 `Wait` 之外复用**——它在 `Wait` 之后就死了。
5. **panic 不会传播**：`Go` 的源码注释专门讨论了为什么不把 `f` 的 panic 交给 `Wait` 去抛出（会任意延迟崩溃、隐藏调用栈、可能死锁）。**所以任务函数里的 panic 会让整个进程崩**，这跟"错误收集"是两件事。要捕获 panic 用 `conc`。
6. **`Wait` 之后不能再 `Go`**：`Group` 明确"不可复用"。

`SetLimit` 的实现也就三行，但有一个**必须知道的坑**：

```go
func (g *Group) SetLimit(n int) {
	if n < 0 { g.sem = nil; return }        // 负数 = 无限制
	if active := len(g.sem); active != 0 {  // 有任务在飞时改上限 → panic
		panic(fmt.Errorf("errgroup: modify limit while %d goroutines are still active", active))
	}
	g.sem = make(chan token, n)             // n == 0 → 谁 Go 谁阻塞
}
```

> ⚠️ **嵌套 `Go` 会死锁**：如果 `f` 内部又调用 `g.Go(...)`，而此时 `sem` 已满，`g.Go` 阻塞 → 正在跑的 `f` 卡住 → 没人归还令牌。两种情况要避开：**（a）** 不要递归用同一个 Group；**（b）** 需要"满了就跳过"的语义（比如每隔 N 个才补充任务）用 `TryGo`（返回 bool，不阻塞）。

### 26.3.1 `x/sync/semaphore`：带权重、可取消的信号量

`SetLimit` 只能"按个数"限流。Agent 场景更常见的需求是**按权重**：一次 LLM 调用要 3000 token 配额，一次 tool 调用只要 100。

```go
import "golang.org/x/sync/semaphore"

sem := semaphore.NewWeighted(10000) // 比如"总 token 预算"

// 上下文感知的获取：ctx 取消就返回，不会傻等
if err := sem.Acquire(ctx, 3000); err != nil {
	return fmt.Errorf("预算不足/已取消: %w", err)
}
defer sem.Release(3000)
```

关键区别（对比 channel 令牌桶）：

| | `chan token` | `semaphore.Weighted` |
| --- | --- | --- |
| 粒度 | 每次 1 个单位 | 任意权重 `int64` |
| 取消 | `select` 手写 | `Acquire(ctx, n)` 内建 |
| 顺序 | 通道 FIFO | 内部 `container/list` 等待队列，**按到达顺序**近似公平 |
| 用途 | 并发数上限 | 预算、连接数、内存配额 |

`Acquire` 内部用 `sync.Mutex` 保护 `cur`，等不到就把自己塞进 `waiters` 链表并 `close`/`<-` 自己的 ready channel——跟第 22 章的 treap 是同一个思路，只是这里是 Go 代码而不是 runtime。

### 26.3.2 `sourcegraph/conc`：池化 + panic 捕获 + 结果聚合

`conc` 补齐了 `errgroup` 缺的三件事：**panic 捕获、错误聚合、结果收集**。

```go
import (
	"github.com/sourcegraph/conc/pool"
)

p := pool.New().WithErrors().WithMaxGoroutines(8) // 并发上限直接写在链式调用里
for _, d := range docs {
	p.Go(func() error { return indexOne(ctx, d) })
}
if err := p.Wait(); err != nil {
	// pool.Error 聚合了所有错误（errors.Join 语义），比 errgroup 的"只留第一个"信息更多
	log.Printf("有 %d 个批次失败: %v", len(err.(pool.Error)), err)
}
```

三个关键差异：

- **panic 会被 `conc/panics.Catcher` 捕获**，并在 `Wait()` 里重新抛出（保留栈）。对"工具执行器"这种会跑用户/LLM 生成代码的地方，这是刚需。
- **`iter` 包**把 `for` 循环变成并行：`iter.ForEach(docs, fn)`、`iter.Map(docs, fn)`、`iter.MapErr`、`iter.ForEachIdx`（拿下标写独立区间）。
- **`stream` 包**做"边产边消费"的流水线。

> 📌 **版本提醒**：`conc` 的 API 在 0.3 前后有变动（`WithContext`、`WithCancelOnError`、`WithFirstError` 等选项是较新版本才有的），以你 `go.mod` 里的版本为准。用它之前先看清 `pool.Pool` 的 `Go` 签名——**带 ctx 的版本要求任务函数签名为 `func(ctx context.Context) error`**。

### 26.3.3 worker pool 的三种写法

| 写法 | 代码形态 | 启动成本 | 背压 | 取消 | 适用 |
| --- | --- | --- | --- | --- | --- |
| **① 每任务一 goroutine + 限流** | `errgroup.SetLimit(n)` / `semaphore` | 每任务一个 goroutine（2KB 起） | 有（`Go` 阻塞） | ctx 天然穿透 | **默认首选**。任务数 ≤ 十万级、任务里有 IO |
| **② 固定 worker + jobs channel** | `n` 个 `for job := range jobs` + 结果 channel | 固定 n 个 goroutine | 由 jobs channel 缓冲决定 | 要手动 `select ctx.Done()` | 需要复用昂贵资源（连接、大 buffer、CPU 密集批处理） |
| **③ 第三方 goroutine 池** | `ants.NewPool(n)` / `conc` | 复用 goroutine（省去创建/GC） | 有（池满阻塞或返回错误） | 视库而定 | 极限压榨（百万级短任务、超高频） |

> 💡 **选型心法**：Go 的 goroutine 便宜到 2KB 起、创建 ~200ns，所以**先问"我是不是真的需要池"**。需要池的信号只有两个：(a) 任务里有必须复用的重型资源；(b) 任务数上千万且 profile 显示 goroutine 创建/回收本身是热点。**为了"看起来像线程池"而引入 ants，是 Java 惯性。**

---

## 26.4 Java 对照：`allOf` 到底差在哪

| 需求 | Java | Go | 差在哪 |
| --- | --- | --- | --- |
| 并行执行 + 等全部 | `CompletableFuture.allOf(fs).join()` | `errgroup.Wait()` / `wg.Wait()` | `allOf` 只等完成，**不传递失败语义**（异常藏在每个 future 里） |
| 失败即取消兄弟 | 无内建；手写（`whenComplete` 里挨个 cancel） | `errgroup.WithContext` + 首个错误自动 cancel | Go 是**默认行为**，Java 要自己写且容易漏 |
| 收集第一个错误 | 要遍历 future 逐个 `join` | `g.Wait()` 直接返回 | —— |
| 收集全部错误 | `Stream.map(f::get).filter(失败)` 手工聚 | `conc` 的 `pool.Error` / `errors.Join` | Go 的聚合更顺手 |
| 并发上限 | `Executors.newFixedThreadPool(n)` | `SetLimit(n)` / `semaphore.NewWeighted` | Java 是**池**（线程复用）；Go 是**闸门**（goroutine 照开，只卡入口） |
| 提交时的背压 | 有界队列 + `CallerRunsPolicy` | `g.Go` 在 `SetLimit` 下**阻塞调用方** | Go 的背压是"同步"的，天然限速；Java 默认 `LinkedBlockingQueue` 是无界，最经典的坑 |
| 带权重的配额 | `java.util.concurrent.Semaphore.acquire(n)` | `semaphore.Weighted.Acquire(ctx, n)` | 同构；Go 多了 ctx 取消 |
| 超时 | `future.get(t, unit)`、`invokeAll(tasks, timeout)` | `context.WithTimeout` 贯穿全树 | Go 的超时会**自动传播到任意深度**（第 24 章） |
| 优雅退出 | `executor.shutdown()` + `awaitTermination(t)` | `signal.NotifyContext` + `srv.Shutdown(ctx)` + `wg.Wait()` | 结构相同，Go 需要自己接信号 |
| panic/异常传播 | `Future.get()` 抛 `ExecutionException`（自动包装） | **不传播**：errgroup 只收 error；panic 直接崩进程 | ⚠️ 最大的心理落差：Go 没有"任务异常"这层包装，要自己 recover |
| 结构化并发官方支持 | `StructuredTaskScope`（JEP 453 起多轮预览，截至 JDK 25 仍是预览） | 库级（errgroup）+ 1.25 `WaitGroup.Go` | Java 走"语言级"、Go 走"库级"；Go 的方案更灵活但要自己选库 |

> 🧠 **一句话总结**：**`allOf` 是"等"，`errgroup` 是"等 + 一损俱损 + 有界"。**
> `CompletableFuture` 是数据流管道（组合子丰富，取消语义弱），`errgroup` 是控制流作用域（组合子极少，取消语义强）。Java 老兵最常犯的错是把 `errgroup` 当 `allOf` 用——于是忘了"第一个错误会自动取消其它任务"这件事，然后在 `f` 里对 `gctx.Err()` 感到困惑。

---

## 26.5 实战：批量 embedding 的完整编排

把 26.1 的骨架补全成一个能跑的版本，包含**分批、有界并发、rate limit、重试退避、进度、无竞争写结果**六个要素：

```go
// go run main.go（依赖 golang.org/x/sync 与 golang.org/x/time）
package main

import (
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"sync/atomic"
	"time"

	"golang.org/x/sync/errgroup"
	"golang.org/x/time/rate"
)

type Doc struct {
	ID   int
	Text string
}
type Vec []float32

var errRateLimited = errors.New("429 too many requests")

type Embedder interface {
	Embed(ctx context.Context, batch []Doc) ([]Vec, error)
}

// 假的供应商：QPS 限制 + 偶发 429 + 80ms 延迟
type fakeEmbedder struct{ limiter *rate.Limiter }

func newFakeEmbedder() *fakeEmbedder {
	return &fakeEmbedder{limiter: rate.NewLimiter(rate.Limit(20), 5)} // 20 QPS，突发 5
}

func (f *fakeEmbedder) Embed(ctx context.Context, batch []Doc) ([]Vec, error) {
	if err := f.limiter.Wait(ctx); err != nil { // ← 客户端限流，ctx 感知
		return nil, err
	}
	if rand.IntN(40) == 0 {
		return nil, errRateLimited
	}
	select {
	case <-time.After(80 * time.Millisecond):
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	out := make([]Vec, len(batch))
	for i := range batch {
		out[i] = Vec{float32(batch[i].ID), 0.5}
	}
	return out, nil
}

// EmbedAll：分批 + 有界并发 + 重试 + 进度
func EmbedAll(ctx context.Context, e Embedder, docs []Doc, batchSize, concurrency int) ([]Vec, error) {
	vecs := make([]Vec, len(docs))
	var done atomic.Int64

	g, gctx := errgroup.WithContext(ctx)
	g.SetLimit(concurrency) // 同时在飞的批次上限

	for start := 0; start < len(docs); start += batchSize {
		end := min(start+batchSize, len(docs)) // Go 1.22+：每轮迭代独立变量，闭包安全
		g.Go(func() error {
			var lastErr error
			for attempt := 0; attempt < 3; attempt++ {
				out, err := e.Embed(gctx, docs[start:end])
				if err == nil {
					copy(vecs[start:end], out) // 每批只写自己的区间 → 零竞争
					done.Add(int64(end - start))
					return nil
				}
				lastErr = err
				if !errors.Is(err, errRateLimited) {
					break // 不可重试的错误，立刻失败 → 触发整组取消
				}
				select { // 指数退避 + 抖动
				case <-time.After(time.Duration(100*(attempt+1)) * time.Millisecond):
				case <-gctx.Done():
					return gctx.Err()
				}
			}
			return fmt.Errorf("batch[%d:%d]: %w", start, end, lastErr)
		})
	}

	if err := g.Wait(); err != nil {
		return nil, err // 只会是"第一个"错误，但已经足够定位
	}
	fmt.Printf("完成 %d/%d 条\n", done.Load(), len(docs))
	return vecs, nil
}

func main() {
	docs := make([]Doc, 200)
	for i := range docs {
		docs[i] = Doc{ID: i, Text: fmt.Sprintf("doc-%d", i)}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	vecs, err := EmbedAll(ctx, newFakeEmbedder(), docs, 16, 4)
	fmt.Println(len(vecs), err)
}
```

这段代码里每个细节都在解决一个真实问题：

| 要素 | 做法 | 不做会怎样 |
| --- | --- | --- |
| 分批 | `batchSize=16` 一批 | 一条一次 HTTP，10 万次请求直接 429 |
| 有界并发 | `SetLimit(4)` | fd 耗尽、内存爆、供应商封号 |
| 客户端限流 | `rate.Limiter.Wait(ctx)` | 8 路并发突发打穿 QPS 配额 |
| 重试 | **只对 429 重试**，3 次，指数退避 + 抖动 | 全量重试 → 雪崩；不重试 → 一条脏数据毁全批 |
| 无竞争写 | 每 goroutine 写自己的 `vecs[start:end]` | 要加锁，或干脆 race（第 23 章） |
| 进度 | `atomic.Int64` | 10 万条的任务你没法回答"还剩多少" |
| 上下文 | 全部用 `gctx` | 首个失败后其它批次不收摊，白烧钱 |

> 💡 **一个容易被忽略的收益**：`g.Go` 在 `SetLimit` 下阻塞，意味着**这个循环本身就是"生产者节流器"**——你不会一次性造出 10 万个待执行闭包，内存也就不会滚雪球。这比"先 build 一个 []func() 再提交"要省得多。

### 26.5.1 优雅退出：signal → Shutdown → 等在途任务

```go
func main() {
	// ① 信号 → ctx（Go 1.16+）
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	mux := http.NewServeMux()
	srv := &http.Server{Addr: ":8080", Handler: mux, ReadHeaderTimeout: 5 * time.Second}

	var wg sync.WaitGroup
	wg.Add(1)
	go func() { // ② 后台 worker（消费队列 / 刷新指标），也归 shutdown 管
		defer wg.Done()
		workerLoop(ctx)
	}()

	errCh := make(chan error, 1)
	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- err
		}
	}()

	select { // ③ 等信号或服务自己挂掉
	case err := <-errCh:
		log.Printf("服务异常: %v", err)
	case <-ctx.Done():
		log.Println("收到退出信号")
	}

	stop() // ④ 恢复默认信号处理：第二个 Ctrl-C / SIGTERM 会立刻杀掉进程（对"卡住"的最后保险）

	// ⑤ 停止接收新连接 + 等待在途请求完成（Shutdown 会先关 listener、再等活跃连接）
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("drain 超时，强制关闭: %v", err)
		_ = srv.Close()
	}
	wg.Wait() // ⑥ 等后台 worker 收尾
	log.Println("退出完成")
}
```

关键点：

- **`Shutdown` 用的是"新的、不带取消的"ctx**。父 ctx 已经 `Done` 了，用它当 `Shutdown` 的 ctx 会立刻返回、丢掉所有在途请求——这是最常见的优雅退出 bug。
- **`Shutdown` 只等 HTTP 请求**。你自己起的 worker、errgroup、定时任务，必须自己 `Wait`（第 ⑥ 步）。
- **`stop()` 要第二次调用**，让第二个信号走默认行为（直接杀）。否则进程可能永远卡在 drain 里。
- **`terminationGracePeriodSeconds`（K8s）必须 > 你的 drain 超时**，否则 SIGKILL 会盖掉一切（第 52 章）。
- 长连接（SSE / WebSocket）不会因为 `Shutdown` 而主动断开，要显式 `cancel` 或者让连接自己看 ctx（第 39 章）。

---

## 26.6 踩坑清单（CR 可直接抄）

| # | 现象 | 原因 | 正确写法 |
| --- | --- | --- | --- |
| 1 | 下游 429 / fd 耗尽 | 无 `SetLimit`，"全部 go 出去" | `g.SetLimit(n)`，n = 下游 QPS × 平均延迟 |
| 2 | `SetLimit` 之后出现莫名死锁 | 在任务函数里又调用同一个 `g.Go`（sem 满 + 阻塞） | 不要在任务里递归 `Go`；用 `TryGo` 或独立 group |
| 3 | `panic: errgroup: modify limit while N goroutines ... active` | 任务在飞时调 `SetLimit` | 启动前设好；不同阶段用不同 Group |
| 4 | `Wait` 返回 `context.Canceled` 而不是真实错误 | 只看了 `g.Wait()` 的 error，没看 `context.Cause(gctx)` | `g.Wait()` 会返回首个错误；下游要用 `errors.Is/As` 分类 |
| 5 | `Wait` 之后复用 `gctx` 失败 | `Wait` 内部会 `cancel` | ctx 只活到 `Wait` 之前；需要更长生命周期就在外层再派生 |
| 6 | 任务里的 panic 把整个进程带走 | errgroup 不传播 panic | 用到 `conc`（捕获后 `Wait` 重抛），或在 `f` 里自己 `recover` + 转成 error |
| 7 | 部分结果写进 slice 后出现脏数据 | 多个 goroutine 写同一区间 | 每批写自己的 `[start:end]`；或结果 channel + 汇总 |
| 8 | 重试逻辑把不可重试错误也重试了 | 没区分错误类型 | 用哨兵/类型判断（第 7 章），只对 429/5xx/超时退避重试 |
| 9 | 优雅退出时在途请求全失败 | `Shutdown` 用了已被取消的 ctx | `context.WithTimeout(context.Background(), n)` 新建一个 |
| 10 | K8s 里总是被 SIGKILL | `terminationGracePeriodSeconds` < drain 时间 | 让 grace period = drain 超时 + 5s，并加 `preStop sleep`（第 52 章） |

---

## 26.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 结构化并发 | 子任务有词法作用域：`Wait` 之前必须全部结束；三支柱 = 闭合 + 取消 + 有界 |
| `errgroup.WithContext` | 内部 `WithCancelCause`；首个错误 → `cancel(err)` → 其它任务看到 Done 关闭 |
| `SetLimit(n)` | `sem chan token`，容量 n；`Go` 阻塞 = **背压**；有任务在飞时改上限会 panic |
| `TryGo` | 满了就返回 false，不阻塞；用于"满了就跳过/降级" |
| 错误语义 | errgroup 只保留**第一个**错误；要全部用 `errors.Join` 或 `conc` 的 `pool.Error` |
| panic | errgroup **不**传播（会崩进程）；`conc` 捕获并在 `Wait` 重抛 |
| `semaphore.Weighted` | 按权重限流 + `Acquire(ctx, n)` 可取消；`chan token` 只能按个数 |
| `conc` | `pool`（并发上限 + panic 捕获 + 错误聚合）、`iter`（并行 for）、`stream`（流水线） |
| 三种 pool | ① goroutine+闸门（首选）② 固定 worker+jobs channel ③ ants（极少需要） |
| 优雅退出 | `signal.NotifyContext` → `stop()` → `srv.Shutdown(新 ctx)` → `wg.Wait()` |
| Java 对照 | `allOf` 是"等"；`errgroup` 是"等 + 一损俱损 + 有界"；`invokeAll` 的取消只在批量内 |

---

## 26.8 自检题

1. **`errgroup` 已经 `SetLimit(8)` 了，为什么还需要 `rate.Limiter`？两者是重复的吗？**
   > 要点：两者管的东西不同。`SetLimit` 限的是**同时在飞的任务数**（并发度），它假设"每个任务耗时稳定"；`rate.Limiter` 限的是**单位时间的请求数**（QPS）。当单任务延迟波动很大（比如 LLM 流式响应 2s~30s），只限并发数会让瞬时 QPS 抖动甚至打穿配额；反过来只限 QPS 则在慢响应时会堆出大量并发。生产上通常两个都要：`SetLimit` 防资源耗尽，`Limiter` 防下游配额。

2. **为什么 `errgroup` 里 `Wait()` 返回的错误常常不是"真正的根因"，而是 `context.Canceled`？怎么拿到根因？**
   > 要点：第一个任务失败会 `cancel(err)` 取消整组的 ctx，其余任务随即返回 `ctx.Err()`（即 `Canceled`）——这些"次生错误"可能比根因更早到达 `Wait` 之外的观察点，日志里就全是 `context canceled`。`errgroup` 本身保证 `Wait` 返回的是**首个非 nil 的 error**（`errOnce`），所以要区分：任务函数里遇到 `ctx.Err() != nil` 时应 `return ctx.Err()` 而不是包一层新的错误；排查时用 `context.Cause(gctx)`（1.21+，errgroup 内部就是 `WithCancelCause`）拿原始错误，并把 `errors.Is(err, context.Canceled)` 的单列出来。

3. **服务要优雅退出：为什么 `srv.Shutdown(ctx)` 里不能传那个"已经被信号取消的 ctx"？除了 `Shutdown` 还漏了什么？**
   > 要点：`Shutdown` 的 ctx 是**drain 的截止时间**，不是"取消信号"。父 ctx 已被 `signal.NotifyContext` 取消，传进去 `Shutdown` 会立即返回（甚至连在途请求都不等），等于没有优雅退出——必须新建 `context.WithTimeout(context.Background(), n)`。另外 `Shutdown` 只覆盖 `http.Server` 自己管理的连接，后台 worker/errgroup/定时刷新必须自己 `Wait()`，且要 `stop()` 二次调用信号处理以便第二个信号能强制退出。

---

## 26.9 延伸阅读

- `golang.org/x/sync/errgroup/errgroup.go` —— 全文 100 行左右，`Go`/`TryGo`/`SetLimit`/`Wait` 四个方法，注释里那句关于"为什么不传播 panic"的讨论值得读
- `golang.org/x/sync/semaphore/semaphore.go` —— `Weighted` 的等待队列实现（`container/list` + ready channel）
- `github.com/sourcegraph/conc` —— `pool` / `iter` / `stream` / `panics` 四个包，README 就是最好的文档
- `golang.org/x/time/rate` —— `Limiter.Wait(ctx)` 与 `Allow`/`Reserve` 的区别（第 32 章限流）
- Go 1.25 release notes —— `sync.WaitGroup.Go`（把最简版结构化并发收进标准库）
- JEP 453 / 后续预览 —— `StructuredTaskScope`，用来对照"语言级 vs 库级"两条路线
- `$GOROOT/src/net/http/server.go` —— `Server.Shutdown` 与 `closeIdleConns` 的实现，理解 drain 到底等的是什么

## 26.10 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 25 章 并发陷阱](25-concurrency-pitfalls.md)（四型泄漏）、[第 24 章 context](24-context.md)（取消传播）
- 下一章：[第 27 章 编译链路：AOT 与 JIT 的路线之争](27-compile-pipeline.md)（进入第五部分）
- 相关：[第 22 章 sync 原语](22-sync-primitives.md)（`WaitGroup`/`Pool`）、[第 32 章 高并发实战](32-high-concurrency.md)（限流与压测）、[第 44 章 RAG 管线](44-rag-pipeline.md)（embedding 批处理落地）、[第 52 章 部署与 SRE](52-deployment-sre.md)（优雅退出与 K8s 优雅期）
