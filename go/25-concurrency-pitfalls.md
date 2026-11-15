# 25. 并发陷阱：竞态、泄漏、死锁、背压

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 并发与调度器源码 ｜ **编号**：25 / 54
> **本章定位**：前面四章讲了"并发怎么才能对"，这一章讲"**它是怎么错的**"。Go 的并发错误有极强的家族相似性：四种泄漏、三类死锁、一种竞态。把它们认全了，你 review 时能靠"形状"就报警。
> **读法**：25.2 的四型图和诊断树贴在显示器上；25.3 的 demo 都能 `go run`，建议真跑一遍（跑出 `fatal error: all goroutines are asleep - deadlock!` 那一下很有教育意义）；25.4/25.6 是 Java 对照与 CR 清单。

---

## 25.1 引子：一次 40 分钟的 P0 复盘

**现象**：Agent 网关凌晨 2:14 开始 5xx 飙升，p99 从 600ms 涨到 12s，4 分钟后 Pod 被 OOMKilled；滚动重启后 20 分钟复现一次，一共三轮。

**上面的第一现场**：

- `container_memory_working_set` 从 400MB 稳步涨到 limit 的 2GB 然后被杀（不是尖峰，是斜坡 —— 典型泄漏）。
- `go_goroutines` 从 800 涨到 61 万。
- `process_resident_memory` 与 goroutine 数完全同步。
- CPU 只有 30% —— 没有忙等，全是**阻塞**的 goroutine。

**抓到的 goroutine dump**（`pprof/goroutine?debug=2`，节选）：

```
610258 @ 0x43c1e5 0x44d1f0 0x9b2a31 0x47a441
#	0x9b2a30	main.(*Fanout).Start.func1+0x70	/app/agent/fanout.go:88
#	0x47a440	runtime.goexit+0x10
```

`fanout.go:88` 是这么一行：

```go
results := make(chan Result) // 无缓冲！
for _, t := range tools {
	go func(t Tool) {
		r, _ := t.Call(ctx, args)
		results <- r // ← 61 万个 goroutine 全卡在这里
	}(t)
}
for i := range tools {
	select {
	case r := <-results:
		...
	case <-ctx.Done():
		return nil, ctx.Err() // ← 提前返回，再也没人读 results
	}
}
```

**根因一句话**：`ctx` 被上游取消（用户断开）→ 主循环 `return` → `results` 无人消费 → 所有工具 goroutine 永久阻塞在发送上 → 每个还抱着 `args`、`Result`、`http.Response` 等一堆对象 → 内存斜坡 → OOM。

**修法**：`make(chan Result, len(tools))`。一个字符的改动，一场 P0。

> 🩸 这条事故的教训不是"记得加缓冲"，而是：**Go 里 goroutine 的生死不由栈帧决定**。函数返回了，它启动的 goroutine 还活着；Java 里 `Thread` 至少还挂在 `ExecutorService` 上能被 `shutdownNow` 打断，Go 里没人替你收拾。

---

## 25.2 直觉模型：四型泄漏 + 一棵诊断树

### 25.2.1 泄漏四型

```
① 无退出条件                 ② 无缓冲阻塞
   ┌──────────────┐            ┌──────────────┐
   │ for { ... }  │            │ results <- r │ ← 没人收
   │ 永远等一个   │            └──────────────┘
   │ 永不到来的   │            主流程已 return
   │ 事件         │            
   └──────────────┘            ③ ctx 未取消
                                 ┌──────────────┐
   ┌──────────────┐             │ <-ctx.Done() │ ← 永远不关
   │ 生产者速度   │             └──────────────┘
   │ >> 消费者     │            忘了 defer cancel()
   │ 缓冲塞满后阻塞│
   └──────────────┘
        ④ 生产者快于消费者
```

| 型 | 现象 | 根因 | 修法 |
| --- | --- | --- | --- |
| ① 无退出条件 | goroutine 数缓涨；dump 里卡在 `chan receive` / `select` | 循环体的 `for` 没有 `return`/`break` 路径，或等待的事件源已废弃 | 每个 `for` 必须有 `case <-ctx.Done()` 或 `case v, ok := <-ch`（`!ok` 就退出） |
| ② 无缓冲阻塞 | goroutine 数随请求数线性涨；dump 卡在 `chan send` | 结果 channel 无缓冲 + 消费者提前退出（超时、取消、error 分支） | `make(chan T, n)` 且 `n >= 生产者数量`；或用 `select { case ch<-v: case <-ctx.Done(): }` |
| ③ ctx 未取消 | 每次请求泄漏 1~N 个 | `WithTimeout/WithCancel` 没有 `defer cancel()`；或自建 goroutine 没监听 ctx | 每个 `With*` 立即 `defer cancel()`；goroutine 内所有阻塞点都带 `case <-ctx.Done()` |
| ④ 生产快于消费 | 内存单调涨；dump 里 goroutine 数稳定但 channel 长度巨大 | 无界/过大缓冲 + 没有背压 | 有界队列 + 拒绝策略（`select default` 丢弃或返回 429）；限流（`x/time/rate`、`semaphore`） |

### 25.2.2 诊断决策树

```
goroutine 数异常
   │
   ├─ pprof/goroutine?debug=2 → 看第一条栈帧（不用看全量）
   │     │
   │     ├─ chan send        → 型②（谁该读没读）或型④（缓冲满）
   │     ├─ chan receive     → 型①（谁该写没写/没 close）
   │     ├─ select / IO wait → 型③（ctx 没传/没取消）或下游慢
   │     ├─ sync.(*Mutex).Lock  → 死锁或锁竞争（用 mutex profile 区分！）
   │     ├─ syscall.Read     → fd 泄漏 / 下游 hang
   │     └─ 无栈（几十万条相似）→ 按栈聚合，找"同一个调用点"
   │
   ├─ 数一下：数量 ≈ 请求数 × 每请求 goroutine 数？→ 泄漏
   │          稳定不涨但内存涨？→ 型④ 或 buffer 复用问题
   │
   └─ 时间上对得上一次发布/流量突变？→ 找那个 PR 里新增的 go 语句
```

> 💡 **技巧**：`curl 'localhost:6060/debug/pprof/goroutine?debug=1'` 输出的是**按栈聚合**的计数（比 `debug=2` 好用得多，后者是全量列表）。60 万个 goroutine 时，`debug=1` 只有几十行。

### 25.2.3 死锁三类

```
A) 锁序死锁                      B) channel 环                C) 分片/等待自己
  G1: Lock(a) → Lock(b)           G1: <-ch2 再发 ch1           G1 持锁 A 等 B
  G2: Lock(b) → Lock(a)           G2: <-ch1 再发 ch2           G2 持锁 B 等 A
  （交叉持有 → 互相等）           （互相等对方发）             （与 A 同构，但常藏在回调里）
```

Go 的运行时只会在**所有 goroutine 都无法推进、且没有 timer/netpoll 事件**时报 `fatal error: all goroutines are asleep - deadlock!`；只要有任意一个 goroutine 还能跑（比如 HTTP server 的 accept 循环），它就**不会报**——你的服务只会表现为"某个接口永久卡住"。这就是为什么生产环境必须靠 profile 和超时，而不是靠这个检测器。

---

## 25.3 底层实现视角：错误在 runtime 里长什么样

### 25.3.1 `-race` 实战：先让它替你看见

```go
// go run -race race.go
package main

import (
	"fmt"
	"sync"
)

type cache struct {
	m map[string]int
}

func main() {
	c := &cache{m: map[string]int{}}
	var wg sync.WaitGroup
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			c.m[fmt.Sprint(i)] = i // 并发写 map
		}(i)
	}
	wg.Wait()
	fmt.Println(len(c.m))
}
```

`go run -race` 会给你**两份报告**：一份是 `WARNING: DATA RACE`（第 23 章讲的影子内存 + 向量时钟），另一份是 map 自身的 `fatal error: concurrent map writes`（`runtime/map.go` 里 `hmap.flags` 的 `hashWriting` 标志位在读写前后检查，一旦冲突直接 `throw`）。

两点必须分清：

| 手段 | 抓什么 | 抓不到什么 |
| --- | --- | --- |
| `-race` | 真实执行到的**无 HB 的并发访问**（内存读写级） | 死锁、goroutine 泄漏、逻辑竞态（有 HB 但顺序不对）、未执行的路径 |
| map/slice 的运行时检查 | 并发写 map（直接 fatal） | 其它一切 |
| `go vet` | `lostcancel`、lock by value、`loopclosure` 等**静态模式** | 运行期的任何东西 |

所以三者是互补的，CI 里三样都要跑（第 34 章）。

### 25.3.2 泄漏四型：一个能跑的实验

```go
// go run leak.go：四种泄漏各来一遍，看 goroutine 数怎么涨
package main

import (
	"context"
	"fmt"
	"runtime"
	"time"
)

func report(tag string) {
	time.Sleep(80 * time.Millisecond) // 给新 goroutine 一点启动时间
	fmt.Printf("%-16s goroutines=%d\n", tag, runtime.NumGoroutine())
}

func main() {
	report("baseline")

	// 型① 无退出条件：channel 永不 close，range 永不结束
	go func() {
		ch := make(chan int)
		go func() {
			for range ch { // 谁都不会 close 它
			}
		}()
		ch <- 1
	}()
	report("after ①")

	// 型② 无缓冲阻塞：消费者提前 return
	go func() {
		results := make(chan int) // 应该 make(chan int, 1)
		go func() { results <- 42 }()
		// 主流程不读就退出
		return
	}()
	report("after ②")

	// 型③ ctx 未取消：忘了 defer cancel
	go func() {
		ctx, _ := context.WithTimeout(context.Background(), time.Hour) // ← 漏了 cancel
		go func() { <-ctx.Done() }()
	}()
	report("after ③")

	// 型④ 生产快于消费：缓冲塞满后生产者永久阻塞
	go func() {
		out := make(chan int, 10)
		go func() {
			for i := 0; i < 100; i++ {
				out <- i // 塞满 10 个后卡住，因为消费者只读一个
			}
			close(out)
		}()
		<-out
	}()
	report("after ④")

	fmt.Println("主函数也要退出了，那些 goroutine 不会跟着退（进程退出才清）")
}
```

四型的修法（就是把这四段改掉，`-race` + `goleak` 都能验）：

```go
// ① 给 for 一个出口：用 close 或 ctx
func fixed1(ctx context.Context) {
	ch := make(chan int)
	defer close(ch) // 由"唯一所有者"在退出时 close
	go func() {
		for v := range ch {
			_ = v
		}
	}()
	ch <- 1
}

// ② 结果 channel 带缓冲 = 生产者数量
func fixed2(ctx context.Context) {
	results := make(chan int, 1) // 只要 >= 生产者数就一定不会卡住
	go func() { results <- 42 }()
	select {
	case <-results:
	case <-ctx.Done():
	}
}

// ③ 每个 With* 都 defer cancel()（并让子 goroutine 也监听 ctx）
func fixed3(ctx context.Context) {
	ctx, cancel := context.WithTimeout(ctx, time.Hour)
	defer cancel()
	go func() { <-ctx.Done() }()
}

// ④ 有界 + 背压：满了就明确失败，而不是无声堆积
var errQueueFull = errors.New("queue full") // 包级哨兵错误

func fixed4(ctx context.Context, out chan<- int, v int) error {
	select {
	case out <- v:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	default:
		return errQueueFull // 或阻塞等待（真背压），但不能悄悄丢或无限缓冲
	}
}
```

### 25.3.3 死锁与锁序：一个必跑的实验

```go
// go run deadlock.go —— 输出：fatal error: all goroutines are asleep - deadlock!
package main

import (
	"fmt"
	"sync"
)

type account struct {
	id     int
	mu     sync.Mutex
	amount int
}

func transfer(from, to *account, n int) {
	from.mu.Lock()
	defer from.mu.Unlock()
	to.mu.Lock()
	defer to.mu.Unlock()
	from.amount -= n
	to.amount += n
}

func main() {
	a := &account{id: 1, amount: 100}
	b := &account{id: 2, amount: 100}
	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); transfer(a, b, 10) }() // Lock(1) → Lock(2)
	go func() { defer wg.Done(); transfer(b, a, 10) }() // Lock(2) → Lock(1)
	wg.Wait()
	fmt.Println(a.amount, b.amount)
}
```

修法：**给锁定一个全局顺序**。runtime 自己在 `selectgo` 里就是这么干的（第 21 章：`lockorder` 按 `hchan` 地址堆排序），标准库是这门手艺的天花板：

```go
func transfer(from, to *account, n int) {
	first, second := from, to
	if first.id > second.id { // 用稳定 id 排序（或用指针地址排序）
		first, second = to, from
	}
	first.mu.Lock()
	defer first.mu.Unlock()
	second.mu.Lock()
	defer second.mu.Unlock()
	from.amount -= n
	to.amount += n
}
```

四条防死锁纪律：

1. **一次持有多把锁时，必须按全局唯一顺序加锁**（id、字典序、地址都行，唯一要求是**所有人用同一套**）。
2. **临界区里不调用外部代码**（打印、HTTP、回调、`fmt.Println` 里的 Stringer 方法都可能拿别的锁）。Java 的 `synchronized` 也有这条铁律，但 Go 的 `Mutex` 不可重入，代价更直接：同一 goroutine 二次 Lock 立刻死锁。
3. **优先"只持一把锁"**：把需要原子性的部分抽成一个函数，用一把锁保护，而不是"逐个字段加锁"。
4. **用 `TryLock` 做有界等待**：Go 1.18 起 `Mutex.TryLock` / `RWMutex.TryRLock` 可用，失败就返回错误（而不是干等），能把手死锁降级成"快速失败"。

> ⚠️ 注意 `TryLock` 的语义陷阱：`TryLock` 失败**不建立任何 happens-before**（第 23 章规范明文），所以 `if !mu.TryLock() { read shared; }` 依然是 race。`TryLock` 只解决"卡住"，不解决"数据可见"。

### 25.3.4 出路：single writer / actor

共享可变状态的正解只有三个：**不共享**（值拷贝 / channel 传所有权）、**不可变**（每次 Store 新快照）、**单写者**（把状态关进一个 goroutine）。第三个就是 actor，Go 里实现只要 20 行：

```go
// single writer：状态只在 owner goroutine 里被碰，外部全靠 channel 投递命令
type op struct {
	delta int
	reply chan int
}

type Counter struct {
	ops  chan op
	done chan struct{}
}

func NewCounter() *Counter {
	c := &Counter{ops: make(chan op, 64), done: make(chan struct{})}
	go func() {
		n := 0 // ← 只有这个 goroutine 碰它，一把锁都不用
		for o := range c.ops {
			n += o.delta
			o.reply <- n
		}
		close(c.done) // ops 关闭后退出，通知外面
	}()
	return c
}

func (c *Counter) Add(delta int) int {
	reply := make(chan int, 1) // 有缓冲：即使调用方放弃等待，owner 也不会卡
	c.ops <- op{delta: delta, reply: reply}
	return <-reply
}

func (c *Counter) Close() {
	close(c.ops)
	<-c.done
}
```

这个模型的三个好处，正好对上三个老问题：

- **没有锁**，所以没有锁序、没有死锁、没有饥饿模式。
- **没有共享内存**，所以没有 data race，`-race` 永远绿。
- **天然可审计**：所有状态变更都在一个 `for` 里，日志/trace 打点插进去就是完整的事件流（Agent 状态机、会话状态、限流桶都适合这么写）。

代价也很真实：**所有操作要串行**，而且多了一次 channel 往返（约 100~300ns）。所以只适合"状态变更不频繁、但必须一致"的东西——会话记忆、预算账本、连接注册表。**也不要用它保护热点计数器**：那种场景分片 atomic 才是答案（第 30 章）。

`net/http` 的 HTTP/2 实现就是这个模式的教科书案例：所有帧的写都交给一条写循环 + `writeScheduler`，所以 `http.ResponseWriter` 文档明确禁止并发写。第 38 章会再见到它。

### 25.3.5 可见性：没有 volatile，但有 HB

这一节只给结论，推导在第 23 章：

- Go **没有** `volatile`。Java 里 `volatile boolean stopped` 直译成 `var stopped bool` 就是 data race。
- 正确姿势：`close(done)`（最 Go，还能被 `select` 用）、`atomic.Bool`、或者锁**两侧都进临界区**。
- 反例（高频事故）：`if err != nil { s.lastErr = err }` 只在写侧加锁，读侧裸读——`-race` 会报，生产上表现是"偶发读到旧的错误信息"。

---

## 25.4 Java 对照：**有界**与**无界**的分水岭

| 维度 | Java | Go | 会猜错的地方 |
| --- | --- | --- | --- |
| 泄漏的物理载体 | 线程对象 + 栈（1MB/线程）→ 几百个线程就爆 | goroutine 2KB 起 + 逃逸到堆的对象 → 60 万个才 OOM | Go 的泄漏**更隐蔽**：线程数看得见，goroutine 数要主动暴露指标 |
| 有界性 | `ThreadPoolExecutor` 强制你写 `maxPoolSize` + 队列 + 拒绝策略 | **没有池，没有上限**，`go` 想开多少开多少 | Go 里"背压"必须自己搭：有界 channel、`semaphore`、`rate.Limiter`、`errgroup.SetLimit` |
| 泄漏现象 | 队列堆积 / `RejectedExecutionException` / 线程数顶满 | 内存斜坡 + `go_goroutines` 单调涨 + 全部阻塞 | 两边都"卡"，但 Java 卡在池边界上（有告警），Go 卡在内存上（OOM 才发现） |
| 线程转储 | `jstack` / `kill -3`，一次一份 | `pprof/goroutine?debug=1/2`、`GOTRACEBACK=all`、`SIGQUIT` | Go 的 `debug=1` 是**按栈聚合**的，60 万 goroutine 也只有几十行——必须会用 |
| 死锁检测 | JVM 在 dump 里给出 "Found one Java-level deadlock" | runtime 只在**全体阻塞**时报 `all goroutines are asleep` | 服务里有 accept 循环时 Go 永远不报，只能靠 profile + 超时 |
| 中断机制 | `Thread.interrupt()` + `InterruptedException`（协作式） | `ctx` + channel | Go 的中断是**数据传播**而不是异常传播，不需要 try/catch |
| race 检测 | JVM 无内置 detector（有 JMM 但无工具；JNI 场景可用 TSan） | `-race` 内置，CI 一行搞定 | Java 老兵第一次看到 `-race` 会震惊：这是"半个编译器+运行时" |
| 强停线程 | `Thread.stop()`（已废弃） | 没有对应物 | Go 里"停不下来"的 goroutine 只能靠它自己检查 ctx |
| 内存可见性错误的表现 | 通常是"读到旧值" | 可能**内存损坏**（interface/slice 多字撕裂，见规范） | Go 的 race 后果比 Java 更凶 |

> 🧠 一句话总结这条分水岭：**Java 用"有界的池"把并发度这种事管起来，Go 用"便宜的 goroutine"把它交给你。**
> 前者是省心的，代价是一次线程上下文的成本；后者是自由的，代价是**你必须自己做背压**。这就是为什么第 26 章（结构化并发）在 Go 里不是"锦上添花"，而是必需品。

---

## 25.5 框架与工程落地

### 25.5.1 block / mutex profile：看见"阻塞在哪"

```go
package main

import (
	"net/http"
	_ "net/http/pprof" // 自动注册 /debug/pprof/*
	"runtime"
	"time"
)

func main() {
	// 两个默认关闭的 profile，线上排障必须显式打开
	runtime.SetBlockProfileRate(1)     // 1 = 全量采样（生产建议 10000 之类，按纳秒阈值）
	runtime.SetMutexProfileFraction(1) // 0 = 关闭；1 = 全量（生产建议 5~100）

	go func() { _ = http.ListenAndServe("127.0.0.1:6060", nil) }() // 只绑内网/本地

	// ... 业务：http.ListenAndServe(":8080", handler)
	time.Sleep(time.Hour)
}
```

```bash
# 抓 30 秒 block profile，直接在浏览器看火焰图
go tool pprof -http=:8081 'http://127.0.0.1:6060/debug/pprof/block?seconds=30'
go tool pprof -http=:8081 'http://127.0.0.1:6060/debug/pprof/mutex?seconds=30'

# 泄漏现场：按栈聚合的 goroutine 计数，先看 TOP
curl -s 'http://127.0.0.1:6060/debug/pprof/goroutine?debug=1' | head -40

# 调度器视角：每 1 秒打印 GMP 状态（总 G 数、runq 长度、GC 次数）
GODEBUG=schedtrace=1000 ./agent
```

| profile | 采样机制 | 什么时候看 | 典型结论 |
| --- | --- | --- | --- |
| `goroutine` | 快照（非采样） | 泄漏、卡死 | "6 万个 goroutine 卡在 `fanout.go:88` 的 chan send" |
| `block` | 每次阻塞超过阈值就记一条（默认关闭） | 延迟归因、channel/锁/select 阻塞 | "40% 的阻塞时间在 `<-ctx.Done()`"（说明在等下游） |
| `mutex` | 采样竞争者的等待（默认关闭） | 锁竞争、锁粒度 | "`Ledger.mu` 占 34% 的阻塞时间" |
| `heap` | 按 size class 采样 | 内存斜坡、分配热点 | "60% 内存在 `*Result` 上" |
| `trace` | `runtime/trace` | 调度延迟、GC pause、goroutine 生命周期 | "P 空转但 runq 有 G"（锁/系统调用问题，第 29 章） |

### 25.5.2 把"陷阱"变成流程

**（1）测试里守 goroutine 数量**。`go.uber.org/goleak` 是事实标准：

```go
func TestFanout(t *testing.T) {
	defer goleak.VerifyNone(t) // 测试结束时若还有泄漏的 goroutine，直接 fail 并打印栈
	f := NewFanout(...)
	ctx, cancel := context.WithCancel(context.Background())
	cancel() // 模拟上游取消
	f.Start(ctx)
}
```

**（2）CI 三件套**：`go vet ./...`、`go test -race ./...`、`staticcheck ./...`。`-race` 的退出码是 66，直接当门禁（第 34 章）。

**（3）指标**：`go_goroutines`、`go_gc_duration_seconds`、`process_resident_memory_bytes` 三条曲线放同一张板子上。**goroutine 数与内存同斜率上涨 = 泄漏**，这个模式几乎不会误报。

**（4）代码约定**（写进团队规范）：

- 每一个 `go` 语句都要能回答："**它什么时候退出？谁让它退出？**"答不上来就不许合。
- 每一个 `make(chan T)` 都要能回答："**消费者会不会提前走？**"会 → 加缓冲或加 `select ctx.Done()`。
- 每一个 `With*` 后面必须紧跟 `defer cancel()`。
- 跨包导出含 `sync.Mutex` 的 struct 一律用指针传递。

**（5）事故复盘模板**（本章引子那份）：留一份"症状 → profile 证据 → 根因 → 一行修法 → 防复发机制"。防复发机制必须是**可执行的**（加 `goleak` 用例、加 lint 规则、加指标告警），否则一定复发。

---

## 25.6 踩坑清单（CR 可直接抄）

| # | 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- | --- |
| 1 | 内存斜坡 + `go_goroutines` 单调涨 | 无缓冲结果 channel + 消费者提前 return | `make(chan T, len(producers))` | 25.3.2 |
| 2 | 每个请求泄漏一个 goroutine | `WithTimeout` 没 `defer cancel()` | 紧跟 `defer cancel()`；`go vet` 的 `lostcancel` | 第 24 章 |
| 3 | 程序在 CI 里偶发 `deadlock` fatal | 交叉加锁（A→B 与 B→A） | 全局锁序；或合并成一把锁；或 `TryLock` | 25.3.3 |
| 4 | 生产环境某接口永久卡住，但不报死锁 | runtime 只在全体阻塞时报 | 加超时（ctx）+ mutex/block profile | 25.2.3 |
| 5 | `fatal error: concurrent map writes` | 多 goroutine 写同一个 map | 加锁 / `sync.Map` / single writer | 第 22 章 |
| 6 | 同一 goroutine 里二次 `Lock` 卡死 | `sync.Mutex` 不可重入 | 拆内层不加锁函数；或改 single writer | 第 22 章 |
| 7 | 计数器偶发丢更新 | `count++` 不是原子操作 | `atomic.Int64.Add(1)`；高竞争要分片 | 第 23 章 |
| 8 | `stopped` 标志不生效 | 没有 volatile，也没有 HB | `close(done)` / `atomic.Bool` | 第 23 章 |
| 9 | `-race` 全绿但线上仍有怪问题 | `-race` 只覆盖执行到的路径；且不抓死锁/泄漏 | `-race` + `goleak` + profile + 混沌注入 | 23.3.2 |
| 10 | 队列无界，流量高峰直接 OOM | 无背压 | 有界 channel + 拒绝策略 / `errgroup.SetLimit` / `rate.Limiter` | 第 26、32 章 |

---

## 25.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 泄漏的物理原因 | goroutine 的生死**与栈帧无关**；没人唤醒它，它就永远活着，还抱着引用 |
| 四型 | ① 无退出条件 ② 无缓冲阻塞 ③ ctx 未取消 ④ 生产快于消费 |
| 最快诊断 | `pprof/goroutine?debug=1`（按栈聚合）→ 看第一条栈帧 → 对照四型 |
| 结果 channel | 缓冲容量 **>= 生产者数量**；否则必须有 `select ctx.Done()` 兜底 |
| 死锁三类 | 锁序反转 / channel 环 / 回调里再拿锁（本质都是锁序） |
| 死锁检测 | runtime 只在**全体阻塞**时 fatal；服务里基本指望不上 → 用超时 + profile |
| 锁序纪律 | 多锁必须全局有序；临界区里不调外部代码；`TryLock` 只防卡死不防 race |
| 出路 | 不共享（传值/channel）→ 不可变（atomic 快照）→ 单写者（actor，20 行） |
| profile | block/mutex 默认**关闭**，要 `SetBlockProfileRate` / `SetMutexProfileFraction` |
| 流程 | `goleak` 守测试、`-race` 守 CI、三条指标守线上、一条约定守 review |
| Java 分水岭 | Java 用**有界池**管并发，Go 把并发交给你 —— 背压必须自己搭 |

---

## 25.8 自检题

1. **一段代码 `go func(){ results <- r }()`，主循环里 `select { case <-ctx.Done(): return }` 提前返回了。为什么这一定会泄漏？两种修法各有什么代价？**
   > 要点：`results` 无缓冲（或缓冲已满），发送操作需要接收者就绪；主循环 return 之后再无接收者，发送永久阻塞，goroutine 永不退出且持有 `r` 及其引用链。修法一：`make(chan T, N)` 且 `N >= 生产者数`——零成本但占内存，且要求提前知道 N。修法二：发送侧改成 `select { case results <- r: case <-ctx.Done(): }`——不占内存，但要求 ctx 一定要被取消（否则也卡）。两者常常一起用（缓冲 + ctx 兜底）。

2. **线上 `go_goroutines` 从 800 涨到 60 万但 CPU 只有 30%，你怎么在 5 分钟内定位？**
   > 要点：`curl 'localhost:6060/debug/pprof/goroutine?debug=1'` 拿按栈聚合的 TOP，看第一条栈帧卡在哪（chan send / chan receive / select / Lock）；`chan send` → 消费者提前退出（型②）；`chan receive` → 生产者没 close（型①）；`select` → ctx 没传/没取消（型③）；数量与请求数成比例 → 泄漏而非竞争。然后 `go tool pprof -http` 看 block profile 确认阻塞点。最后对照最近一次发布里新增的 `go` 语句。

3. **为什么 `TryLock` 失败后直接读共享变量仍然是错的？`TryLock` 到底解决了什么问题？**
   > 要点：内存模型规范明文规定「An unsuccessful call has no synchronizing effect at all」，即 `TryLock` 失败**不建立任何 happens-before**，读到的值没有任何保证，`-race` 也会报。它解决的是**活性问题**（不让你无限期卡住，把手死锁降级为快速失败 / 可重试），不解决**可见性问题**（那是锁成功或 atomic 的职责）。

---

## 25.9 延伸阅读

- `$GOROOT/src/runtime/map.go` —— `hashWriting` 标志与 `fatal error: concurrent map writes` 的抛出点
- `$GOROOT/src/runtime/proc.go` —— `checkdead()`：`all goroutines are asleep - deadlock!` 的判定条件（理解它为什么在服务里不触发）
- `$GOROOT/src/runtime/mprof.go` 的 `blockevent` / `saveblockevent` —— block/mutex profile 的采样实现
- `go.uber.org/goleak` 文档 —— 测试里怎么守 goroutine
- `golang.org/x/sync/semaphore`、`golang.org/x/time/rate` —— 背压与限流的两个标准件（第 26、32 章）
- Dmitry Vyukov, *Go channels and goroutine leaks*（以及他关于 lock-free 与 actor 的一系列文章）

## 25.10 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 24 章 context](24-context.md)（取消没传好 = 型③ 泄漏）、[第 23 章 内存模型](23-memory-model.md)（可见性问题）、[第 17 章 goroutine 源码](17-goroutine-internals.md)（泄漏在 `_Gwaiting` 状态里的样子）
- 下一章：[第 26 章 结构化并发](26-structured-concurrency.md)（把"背压"变成框架能力）
- 相关：[第 20 章 channel](20-channel-internals.md)、[第 29 章 pprof 与 trace](29-pprof-trace.md)、[第 32 章 高并发实战](32-high-concurrency.md)、[第 54 章 并发安全 CR 清单](54-java-architect-handbook.md)
