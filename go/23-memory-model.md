# 23. Go 内存模型与 happens-before（与 JMM 正面对照）

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 并发与调度器源码 ｜ **编号**：23 / 54
> **本章定位**：这是整个第四部分唯一一章"讲规范"的。前面 6 章讲的是"并发怎么跑"，这一章讲**"并发凭什么对"**——你的代码在什么条件下可以信任跨 goroutine 的读写顺序。Java 老兵最容易在这里翻车，因为 JMM 与 Go 内存模型的差异恰好压在几个"你以为是常识"的点上。
> **读法**：23.2 的 HB 表要背下来（后面所有并发章都靠它）；23.3 逐段读，尤其 23.3.4「为什么没有 volatile」；23.4 的对照表建议贴在工位上。

---

## 23.1 引子：本地永远复现不了的"停机失效"

一个长驻的 Agent 消费者，用 Java 直觉写成这样：

```go
type Consumer struct {
	stopped bool // Java 里你会写 volatile boolean
}

func (c *Consumer) Stop() { c.stopped = true }

func (c *Consumer) Loop() {
	for !c.stopped {
		c.poll()
	}
}
```

在开发机（x86_64 MacBook、8 核）上跑了两个月毫无问题。上线后灰度到一批 ARM 实例（Graviton / 倚天），出现诡异现象：**调了 `Stop()`，`Loop()` 就是不退出**，pprof 一看还在 `for` 里转，CPU 吃满；重启后偶发复现，本地一次都复现不出来。

这不是"Go 的 bug"，而是三件事叠在一起：

1. `stopped` 的读写是 **data race** —— 我的读和别人的写之间**没有任何 happens-before 关系**，规范对它**什么都不保证**。
2. x86 的 TSO 内存模型恰好让"写后读"看起来是顺序的，所以 x86 上"通常"能读到新值；ARM 的 store buffer 让写可以延后可见。
3. 就算硬件是顺序的，**编译器**也能把 `for !c.stopped` 优化成"只读一次到寄存器"——它没有义务假设别人会改一个普通变量。

Java 里 `volatile` 解决了这件事；Go 里**没有 volatile**。这就是本章全部内容的下游产物。

---

## 23.2 直觉模型：happens-before 是"因果箭头"，不是"时间先后"

先把最容易错的心智模型换掉：

- ❌ "写了之后读，就能读到" —— 只有**同一个 goroutine 内**成立（`sequenced before`）。
- ❌ "后发生的就一定看得到先发生的" —— 没有同步原语时，两个 goroutine 之间**没有时间先后之分**，只有 **happens-before（HB）** 关系的有无。
- ✅ **HB 是一条"因果箭头"**：`A happens-before B` 意味着"A 的写入对 B 可见，且 A 的读取在 B 之前"。它由 `sequenced before`（单 goroutine 内的语句顺序）+ `synchronized before`（同步原语配对的读写）**取并集再取传递闭包**得到。

```
   G1                                G2
   ─────────────────────             ─────────────────────
   data = "hello"      ①
   ch <- true          ② ─────────►  ③ <-ch   （② synchronized before ③）
                                       print(data)  ④
   ① sequenced before ②；② sync-before ③；③ sequenced before ④
   ⇒ ① happens-before ④ ⇒ G2 一定能打印 "hello"
```

反过来，如果没有 ②③ 这条边，① 和 ④ 之间**什么关系都没有**——G2 可能打印空串、可能读到旧值、也永远不会被"保证"读到新值。

五类原语建立的 HB（这就是要背的那张表）：

| 原语 | 规范原文关键句 | 直觉 |
| --- | --- | --- |
| **goroutine 创建** | "The `go` statement that starts a new goroutine is synchronized before the start of the goroutine's execution." | `go f()` 之前的所有写在 f 里必可见 |
| **goroutine 退出** | "The exit of a goroutine is **not** guaranteed to be synchronized before any event." | ⚠️ **没有任何保证**。想拿到结果必须用 channel/`WaitGroup`/锁，`go func(){x=1}()` 之后直接读 x 是错的（编译器甚至可能直接删掉整个 `go` 语句） |
| **channel 发送/接收** | 发送 synchronized before 对应接收**完成**；无缓冲时接收 synchronized before 发送完成；`close` synchronized before 返回零值的接收；缓冲通道第 k 次接收 synchronized before 第 k+C 次发送完成 | channel 是"带背压的因果边"，Go 的核心同步手段 |
| **锁** | 对 `Mutex`/`RWMutex` 变量 l，第 n 次 `Unlock` synchronized before 第 m 次 `Lock` 返回（n < m）；`RWMutex` 另有读锁配对规则；`TryLock` 成功等价于 `Lock`，失败**没有**任何同步效果 | 锁 = 互斥 + 因果边，两件事一起给 |
| **Once / atomic** | "The completion of a single call of `f()` from `once.Do(f)` is synchronized before the return of any call of `once.Do(f)`"；"All the atomic operations ... behave as though executed in some sequentially consistent order" | `Once` 是初始化 + HB 的标准答案；atomic 是 seq-cst |

`WaitGroup`、`Pool`、`Cond`、`Map` 这些不在规范正文，而是**包文档**里给出承诺。比如 `WaitGroup.Done` 的文档原话：「a call to `Done` "synchronizes before" the return of any `Wait` call that it unblocks」；`Pool` 的文档：「a call to `Put(x)` "synchronizes before" a call to `Get` returning that same value x」。规范专门用「Additional Mechanisms」一节把这件事授权给包文档——**所以查 HB 时不要只看 go.dev/ref/mem，还要看包的 doc**。

---

## 23.3 底层实现

### 23.3.1 规范到底说了什么（三句话）

规范（go.dev/ref/mem，2022-06-06 版，Go 1.19 起生效）的行文极其克制，核心就三层：

**第一层，data race 的定义**：

> A *data race* is defined as a write to a memory location happening concurrently with another read or write to that same location, unless all the accesses involved are atomic data accesses as provided by the `sync/atomic` package.
>
> 形式化版本补充了关键的限定：「at least one of which is **non-synchronizing**, which are unordered by happens before」——即"两个访问至少一个不是同步操作，且二者之间**没有 HB 关系**"。

注意这句话的两个含义：**(a) 用的是原子操作就不算 race**（哪怕逻辑上有竞争）；**(b) 只要存在 HB 边，就不是 race**（哪怕没有互斥）。

**第二层，DRF-SC**：

> In the absence of data races, Go programs behave as if all the goroutines were multiplexed onto a single processor.

这就是全书最重要的一句保证：**只要没有 data race，你可以放心用"单线程交错执行"的直觉去推理程序**。证明抄的是 Boehm & Adve 的 PLDI 2008 那篇论文，跟 C++11 的模型同源。

**第三层，有 race 时怎么办**：

> An implementation may always react to a data race by reporting the race and terminating the program. Otherwise, each read of a single-word-sized or sub-word-sized memory location must observe a value actually written to that location ... These implementation constraints make Go **more like Java** or JavaScript, in that most races have a limited number of outcomes, and **less like C and C++**, where the meaning of any program with a race is entirely undefined.

并且规范明确警告：**超过一个机器字的读/写允许撕裂**——「races on multiword data structures can lead to inconsistent values not corresponding to a single write. When the values depend on the consistency of internal (pointer, length) or (pointer, type) pairs, as can be the case for **interface values, maps, slices, and strings**, such races can in turn lead to arbitrary memory corruption.」这句话在 Java 里对应的是"非 volatile 的 long/double 可能撕裂"，但 Go 的打击面大得多：**一个 interface 值就是 (type, data) 两个机器字**，race 下可能出现"类型是 A、数据是 B"的场面，然后你的进程就死于 SIGSEGV 而不是打印一个怪数字。

规范末尾还有一节 **Incorrect compilation**，逐条列举"编译器不许做的优化"：不许把写从条件分支搬出来、不许假设循环会终止、不许重新从共享内存加载局部变量……它解释了一个 Java 程序员很少意识到的事实：**Go 编译器为了兼容 race 的存在，主动放弃了一批优化**——这是"不需要 volatile"的另一半原因（见 23.3.4）。

### 23.3.2 `-race` 是怎么看见 race 的：影子内存 + 向量时钟

`go build -race` / `go test -race` 用的是 ThreadSanitizer v2 的运行时（预编译成 `$GOROOT/src/runtime/race/race_*.syso`，你不需要单独装 TSan，但需要 `CGO_ENABLED=1` 和一个 C 链接器）。它由两部分组成：

1. **编译器插桩**（`cmd/compile` 的 race pass）：每条内存读写前后插入 `runtime.racefuncenter`、`racereadpc`、`racewritepc`、`raceacquire`、`racerelease` 之类的调用；连 `selectgo` 里都留着 `raceacquire` / `racenotify` 的钩子（第 21 章源码里那些 `if raceenabled` 就是它）。
2. **影子内存（shadow memory）**：把程序地址空间映射一份"影子"，每 8 字节应用程序内存对应一份状态，里面记录**最近 4 次访问**的"是谁、什么时候、读还是写"。

```
   应用程序内存                    影子内存（shadow cell，每 8B 数据一格）
   0x1000 ┌──────────┐            ┌───────────────────────────────────────┐
          │ 8 bytes  │──────────► │ slot0: {GID, epoch, isWrite}          │
   0x1008 │ 8 bytes  │──────────► │ slot1: {GID, epoch, isWrite}          │
          │   ...    │            │ slot2/slot3: 更早的访问（环形复用）    │
          └──────────┘            └───────────────────────────────────────┘

   每个 goroutine 一份向量时钟 ThreadState：vc[gid] = epoch
     release 语义操作（Unlock / ch<- / Semrelease / atomic write）
        → 把当前 vc 写进相关的 shadow（"我这一支的历史"交出去）
     acquire 语义操作（Lock / <-ch / Semacquire / atomic read）
        → shadow 里的 vc 与自己取 max（"并入别人的历史"）
     判 race：本次访问 vs shadow 里的记录，
       若对方 epoch 不在我的向量时钟里（happens-before 缺失）且不是同一 goroutine → 报 WARNING
```

所以 `-race` 只能报**真实执行过的路径**：向量时钟只在发生过的访问之间比较，覆盖率取决于你的测试和流量走到哪；而且**每个格子只记 4 次访问**（更早的被覆盖，极小概率漏报）。开销约 **5~20 倍 CPU、5~10 倍内存**。

```go
// race.go —— go run -race race.go
package main

import (
	"fmt"
	"sync"
)

func main() {
	n := 0
	var wg sync.WaitGroup
	for i := 0; i < 1000; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); n++ }() // 1000 个 goroutine 抢着 n++
	}
	wg.Wait()
	fmt.Println(n)
}
```

```
$ go run -race race.go
==================
WARNING: DATA RACE
Read at 0x00c00001a0a8 by goroutine 8:
  main.main.func1()
      /tmp/race.go:12 +0x30

Previous write at 0x00c00001a0a8 by goroutine 7:
  main.main.func1()
      /tmp/race.go:12 +0x44

Goroutine 8 (running) created at:
  main.main()
      /tmp/race.go:10 +0x84
==================
Found 1 data race(s)
exit status 66
```

注意 `exit status 66`：`-race` 检出 race 后**退出码非 0**，这是 CI 能把它当门禁的原因（第 34 章）。`go test -race` 则是让测试失败。

### 23.3.3 重排的真实案例：Dekker 与 StoreLoad

内存模型不是纸上谈兵。下面这段就是经典 Dekker 算法的失败现场——**两个 goroutine 各自"先写自己的标志，再读对方的标志"，理论上必有一个读到 1**：

```go
// go run -race dekk.go：在现代多核 CPU 上，两个都读到 0 是真实发生的
package main

import (
	"fmt"
	"sync"
	"sync/atomic"
)

var a, b int // 故意不加同步 —— 这就是 data race

func main() {
	var bothZero atomic.Int64
	for i := 0; i < 500000; i++ {
		a, b = 0, 0
		var wg sync.WaitGroup
		wg.Add(2)
		go func() { // G1
			defer wg.Done()
			a = 1
			if b == 0 {
				bothZero.Add(1)
			}
		}()
		go func() { // G2
			defer wg.Done()
			b = 1
			if a == 0 {
				bothZero.Add(1)
			}
		}()
		wg.Wait()
	}
	fmt.Println("观察到「两个都读到 0」的次数:", bothZero.Load())
}
```

在 x86_64 / arm64 上跑，`both-zero` 通常会出现几千到几万次。原因只有两个，各自都足以致命：

- **CPU 层**：store buffer 让"自己的写"先留在缓冲里，别人的读还没看到 → 两个读都发生在对方的写可见之前。这叫 **StoreLoad 重排**，是唯一一种 x86 也会做的重排。
- **编译器层**：`a = 1; if b == 0` 两句话之间没有同步原语，编译器把它们互换位置完全合法。

修法只有两条：要么用互斥/`atomic`（建立 HB），要么改数据结构（不共享）。把 `a = 1` 换成 `atomic.StoreInt32(&a, 1)`、`b == 0` 换成 `atomic.LoadInt32(&b) == 0`，`both-zero` 立刻变 0——因为 **Go 的原子操作是顺序一致的**，规范原话：「The preceding definition has the same semantics as C++'s sequentially consistent atomics and **Java's `volatile` variables**」。

> 🩸 **这就是"本地跑不出来"的最终答案**：race 不是"概率低"，而是"你的 CPU 恰好帮你兜住了"。同一份代码换个架构、换个编译器版本、加个内联就塌。

### 23.3.4 为什么 Go 没有 `volatile`

Java 的 `volatile` 提供三件事：**可见性**（写立即可见）、**禁止重排**（acquire/release 语义）、**单字读写原子性**；但它**不提供复合操作的原子性**（`v++` 依然不是原子的）。

Go 的立场是：**这三件事是"半成品"，而半成品比没有更危险**。理由有两层：

1. **`volatile` 会让程序员以为"我有可见性了，就安全了"**，于是写出 `v++`、`if (v == null) v = new ...` 这类错误。Go 干脆不给这个错觉：**要么 `sync/atomic`（原子 + seq-cst），要么锁/channel（互斥 + HB），要么就是 data race**。没有中间态。规范甚至直接劝退：「If you must read the rest of this document to understand the behavior of your program, you are being too clever. **Don't be clever.**」
2. 再说，Go 编译器为了兼容 race 已经放弃了"把共享变量的读提到寄存器"这类优化（23.3.1 的 Incorrect compilation），所以"加个 volatile 防止编译器优化掉我的循环"这个 Java 式刚需在 Go 里没有逻辑基础——`stopped` 的问题不是"优化太凶"，而是"**你没有建立 HB，规范就不保证任何事**"。

Java 代码直译过来最常踩的三个坑：

| Java 写法 | 直译成 Go（错） | 正确写法 |
| --- | --- | --- |
| `volatile boolean stopped` | `stopped bool` | `done chan struct{}` + `close(done)`（还能被 `select` 用），或 `atomic.Bool` |
| `volatile int count; count++` | `count int; count++` | `atomic.Int64` 的 `Add(1)`（注意：`atomic` 的 `++` 也要用 `Add`，不存在"原子自增语法"） |
| `synchronized` 提供可见性 | 用 `sync.Mutex` 只锁写不锁读 | 读写**两侧**都要锁；锁保护的是"这段代码"，不是"这个字段" |

### 23.3.5 双重检查锁（DCL）在 Go 的正确写法

DCL 是 Java 老兵的肌肉记忆，也是 Go 里最容易写错的初始化模式。先看错的：

```go
var (
	mu   sync.Mutex
	inst *Config
)

// ❌ 错误：两次无同步读。即使把 mu.Lock 换成 RWMutex 也错
func Bad() *Config {
	if inst == nil { // data race（无 HB 的读）
		mu.Lock()
		defer mu.Unlock()
		if inst == nil {
			inst = load() // 写入
		}
	}
	return inst // data race（另一个 goroutine 可能读到部分初始化的对象）
}
```

错在哪？`if inst == nil` 和 `return inst` 这两次读**没有持有锁**，与锁内的写之间没有 HB 边。就算你的机器"看起来正常"，规范和 `-race` 都不会放过它；更糟的是 `load()` 内部构造的多字段对象可能被观察到"指针已赋值、字段还是零值"的半成品状态。

三种正确写法，按推荐顺序：

```go
// ✅ 方案一（首选）：sync.Once —— 规范级保证，语义最清楚
var (
	once sync.Once
	inst *Config
)

func Get() *Config {
	once.Do(func() { inst = load() })
	return inst // once.Do 建立了 HB：f() 完成 synchronized before 任何 Do 返回
}

// ✅ 方案二：atomic.Pointer[T]（需要"重新加载/热更新"时用）
var cur atomic.Pointer[Config]

func Get2() *Config {
	if p := cur.Load(); p != nil {
		return p
	}
	c := load()
	if cur.CompareAndSwap(nil, c) { // 只让第一个成功的写生效
		return c
	}
	return cur.Load()
}

// ✅ 方案三（最 Go）：能启动时初始化，就别懒加载
var inst3 = load() // 包初始化在单 goroutine 里完成，
                   // 且「all init functions 完成 synchronized before main.main」
```

> 💡 **判断标准**：如果你需要"初始化 + 只做一次 + 全场可见"，用 `Once`；如果需要"随时能换一份不可变快照"，用 `atomic.Pointer[T]`（第 22 章的热更新配置就是这个）；如果根本不需要懒加载，就用包级变量——**Go 的包初始化天然带 HB**，比任何 DCL 都干净。

### 23.3.6 `runtime.KeepAlive` 与 finalizer 的坑

`runtime.SetFinalizer` 是 Go 里唯一"GC 会主动调你代码"的地方，而它和编译器优化的交互非常反直觉：**编译器可以在最后一次"语义上的使用"之后立刻认为变量已死**，GC 随时可以回收 + 跑 finalizer——哪怕你以为后面还有代码"在用"它。

```go
// ❌ 危险的写法：靠 uintptr 传 fd，但 p 可能已经被回收、finalizer 已经把 fd 关了
func writeBad(p *os.File, buf []byte) error {
	fd := p.Fd() // 返回 uintptr：从这一刻起 GC 不再认为 p 可达
	_, err := syscall.Write(int(fd), buf)
	// p 的 finalizer 可能在 syscall 之前/之中就把 fd 关掉 → EBADF，甚至写到别人刚复用的 fd 上
	return err
}

// ✅ 正确：显式告诉编译器"到这儿之前 p 必须活着"
func writeGood(p *os.File, buf []byte) error {
	fd := p.Fd()
	_, err := syscall.Write(int(fd), buf)
	runtime.KeepAlive(p) // 关键一行
	return err
}
```

`runtime.KeepAlive(x)` 的文档原话是："KeepAlive marks its argument as currently reachable. This ensures that the object is not freed, and its finalizer is not run, before the point in the program where KeepAlive is called."

三条工程结论：

1. **凡把指针变成 `uintptr` 交给系统调用 / C 代码 / `unsafe` 运算的地方，后面一律补 `runtime.KeepAlive`**。
2. **别用 finalizer 做业务清理**（关连接、flush、删临时文件）：执行时机不确定（可能要等两轮 GC）、只有一条 goroutine 在跑、程序退出时根本不跑。
3. **`unsafe.Pointer` → `uintptr` 的转换本身不阻止 GC**，这正是第 16 章强调"`uintptr` 不能跨语句存下来用"的原因。

### 23.3.7 typed atomic 与指针原子性

`atomic.Int64` / `atomic.Bool` / `atomic.Pointer[T]`（1.19）的内存语义与老 API 完全一样，本章只需补两点：

- **原子发布的是指针，不是它指向的对象**。`atomic.Pointer[T].Store(p)` 保证别人"要么看到 nil、要么看到一个完全构造好的 p"（靠 sequenced before + release）；但你 Store 之后**再改 p 指向的字段**，那部分改动不在这条 HB 边里。这就是热更新配置必须"每次 Store 一个全新的不可变对象"的原因（第 22 章）。
- **对齐全靠类型保证**。老 API `atomic.AddInt64(&x, 1)` 在 32 位平台要求 8 字节对齐，否则运行期 panic；用类型化的 `atomic.Int64` 交给编译器保证（细节见第 22 章）。

### 23.3.8 「不要通过共享内存来通信」的边界

这句话被引用得太多，以至于经常被当成教条。真实的边界是这样的：

| 场景 | 该用 | 理由 |
| --- | --- | --- |
| 传递所有权（数据从 A 交给 B） | **channel** | 所有权转移 = 天然 HB + 背压 + 生命周期清晰 |
| 编排（fan-out/fan-in、pipeline、超时取消） | **channel + select + ctx** | 用 channel 表达"事件流"，代码就是流程图 |
| 计数器、标志位、指针快照 | **`sync/atomic`** | 临界区只有一条指令，用锁是浪费 |
| 保护一组必须一起变的不变量（map、ring buffer、账本） | **`sync.Mutex`** | "通信"救不了多字段不变量，只有互斥能 |
| 读多写少 + key 稳定 | **`sync.Map` / atomic 快照** | 见第 22 章 |
| 单写多读的状态机 | **single writer + channel 投递命令** | 见第 25 章 |

一句话收口：**channel 用于"所有权与事件"，锁用于"不变量"，atomic 用于"单点状态"**。三者都不是"更 Go"的选择，只是不同问题的正确工具。

---

## 23.4 Java 对照：JMM 与 Go 内存模型正面对决

| 维度 | Java（JMM，JLS §17.4） | Go（go.dev/ref/mem，2022-06 版） | 会猜错的地方 |
| --- | --- | --- | --- |
| 核心保证 | DRF-SC + causality（禁止 out-of-thin-air） | DRF-SC（同一篇 Boehm & Adve 论文），同样禁止 acausal write | 基本同构，可以放心复用"无 race ⇒ 顺序一致"的直觉 |
| `volatile` | 有（可见性 + 禁重排 + 单字原子） | **没有** | `atomic` 语义等于 `volatile`（规范明说），但没有"只读可见性"的廉价开关 |
| 复合操作原子性 | `volatile` 不保证 `v++` | `atomic.Int64.Add` 保证 | Java 的 `AtomicLong` ↔ Go 的 `atomic.Int64`，一一对应 |
| `final` 字段 | 有"冻结"语义：构造器结束前不得观察到默认值 | **没有 final** | 用包级初始化 / `sync.Once` / `atomic.Pointer` 发布不可变对象 |
| 线程创建 | `Thread.start()` happens-before 新线程的第一次动作 | `go` 语句 synchronized before goroutine 开始 | 同构 |
| 线程结束 | `Thread.join()` 之前的所有写 happens-before join 返回 | ⚠️ **goroutine 退出不建立任何 HB** | `wg.Wait()` / `<-ch` / `close` 才能拿到结果；"goroutine 跑完了变量就该更新了"是错的 |
| 锁 | `synchronized`/`ReentrantLock`：解锁 HB 后续加锁 | `Mutex`/`RWMutex`：n 次 `Unlock` HB m 次 `Lock` 返回 | 同构；Go 多了 `TryLock` 失败**无**同步效果这条明文规定 |
| 多字撕裂 | 非 volatile 的 `long`/`double` 可撕裂 | 任意 >1 机器字的读写**允许撕裂**；interface/slice/string/map 在 race 下可能内存损坏 | Go 的暴露面比 Java 大得多 |
| 内存序 API 粒度 | `VarHandle` 有 plain/opaque/acquire/release/volatile 五档 | 公开 API **只有 seq-cst**（runtime 内部有 acquire/release 原语但不暴露） | 想"抄一个 relaxed 优化"在 Go 里没有对应物 |
| 出错后行为 | 允许有限的可预测结果 | 同样允许有限结果，但实现"**可以**报错并终止程序" | `-race` 就是这条授权的产物（exit 66） |

> 🧠 **为什么 Go 敢删掉 volatile**：因为 Go 把"跨 goroutine 通信"整个搬到了语言设施里（channel 是一等公民，不是库），而 Java 只能靠语言关键字给共享变量打补丁。**当通信有一等公民的语法时，就不需要用修饰符去修共享内存**。这是设计取舍，不是功能缺失。

---

## 23.5 框架与工程落地

- **`sync.Pool`**：包文档明确写了 `Put(x)` / `New()` 返回 x 与 `Get()` 返回 x 之间的 HB——所以从池里取出的对象**一定看到了上一次持有者留下的内容**（这也是"复用对象必须自己清零"的原因，见第 22 章）。
- **`sync.Once`**：几乎每个框架的"懒加载 registry / 默认配置 / 全局 client"都用它（`langchaingo` 的默认 client、`eino` 的组件注册表）。它是规范正文点名保证初始化可见性的两个工具之一（另一个是 atomic）。
- **`atomic.Pointer[T]`**：`net/http` 的 http2 配置、`slog` 的默认 handler、各家 RPC 框架的动态开关（熔断阈值、日志级别）都用这个模式：**写者 Store 不可变快照，读者 Load 一次**（`ctx` 的 `close(done)` 广播同理，详见第 24 章）。

---

## 23.6 踩坑清单（CR 可直接抄）

| # | 现象 | 原因 | 正确写法 |
| --- | --- | --- | --- |
| 1 | 本地永不复现、换架构/上线才发疯 | data race：缺 HB 边，规范不保证任何事 | `-race` + CI 门禁；缺同步就补同步 |
| 2 | `go func(){ result = x }()` 之后读 `result` 是空 | **goroutine 退出没有 HB** | `wg.Wait()`、`<-done`、errgroup（第 26 章） |
| 3 | DCL 单例偶尔返回"半初始化对象" | 无同步读/写，缺 HB | `sync.Once` 或 `atomic.Pointer[T]` |
| 4 | Java 的 `volatile` 直译成 `var x bool` | Go 没有 volatile | `atomic.Bool` 或 `close(done)` 广播 |
| 5 | 只锁写不锁读 | 锁保护的是"代码段"，不是"字段" | 读写两侧都进临界区；或用 atomic 让所有访问都原子 |
| 6 | `if x != nil { x.field }` 偶发 SIGSEGV | interface/slice 多字撕裂（race 下允许） | 消除 race；接口值不要跨 goroutine 裸传 |
| 7 | `syscall`/cgo 之后偶发 `EBADF`、写到错误的 fd | 指针转 `uintptr` 后对象被 GC + finalizer 跑掉 | `runtime.KeepAlive(p)` |
| 8 | `atomic.AddInt64` 在 386/arm 上 panic | 字段未 8 字节对齐 | 改用 1.19 的 `atomic.Int64` 类型 |
| 9 | `-race` 全绿就以为安全了 | 向量时钟只记最近 4 次访问，且只覆盖真实执行路径 | `-race` + 高覆盖测试 + code review 双保险 |
| 10 | 在 `atomic.Pointer` Store 之后还改对象字段 | 原子发布的是指针，不是对象内容 | Store **不可变**对象；要改就 Store 新对象 |

---

## 23.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| HB | `sequenced before` ∪ `synchronized before` 的传递闭包；没有 HB 就是没有顺序 |
| 唯一大保证 | **DRF-SC**：无 data race ⇒ 程序等价于单处理器交错执行 |
| data race 定义 | 同一位置的读/写或写/写，**至少一个不是同步操作**，且**无 HB 关系** |
| `-race` 原理 | 编译器插桩 + 影子内存（每 8B 记 4 次访问）+ 每 goroutine 向量时钟；5~20x 开销 |
| goroutine 退出 | ⚠️ **不建立任何 HB**（与 `Thread.join()` 最大差异） |
| channel | 发送 HB 接收完成；无缓冲时接收 HB 发送完成；close HB 返回零值的接收 |
| 锁 | 第 n 次 `Unlock` HB 第 m 次 `Lock` 返回；`TryLock` 失败无同步效果 |
| `Once` / atomic | Once 完成 HB 所有 Do 返回；atomic 是 **seq-cst**，语义 == Java `volatile` |
| 没有 volatile | 半成品危险；用 atomic / 锁 / channel 三选一 |
| DCL | 别手写；`sync.Once` → `atomic.Pointer[T]` → 包级初始化 |
| KeepAlive | 指针转 `uintptr` 交给 syscall/cgo 后必须 `runtime.KeepAlive` |
| 边界 | channel=所有权与事件，锁=不变量，atomic=单点状态 |

---

## 23.8 自检题

1. **规范说 goroutine 的退出"不保证"与任何事件同步。那 `sync.WaitGroup.Wait()` 返回后为什么能读到 `wg.Done()` 之前写的值？依据在哪？**
   > 要点：HB 不来自"goroutine 退出"这个事件，而来自 `WaitGroup` 内部的同步操作——`Done()` 是 `Semrelease`（release 语义），`Wait()` 是 `Semacquire`（acquire 语义），二者配对建立 synchronized before。规范正文的「Additional Mechanisms」一节明确把这类保证授权给包文档，`WaitGroup.Done` 的 doc 写了「synchronizes before the return of any Wait call that it unblocks」。所以查 HB 要看包文档，别只看 ref/mem。

2. **`var cfg *Config` 被一个 goroutine 赋值、另一个 goroutine 读，本地 x86 测试两年没出问题。为什么不能认为它是安全的？要改怎么改？**
   > 要点：这是 data race（无 HB 边），规范对它的行为**不承诺**顺序一致；x86 的 TSO 掩盖了 StoreLoad 重排，且你的服务"恰好"没被编译器把读提到寄存器/循环外。改用 `atomic.Pointer[Config]`（Store 不可变快照 / Load 取值）或 `sync.RWMutex`；如果初始化一次，用 `sync.Once`。上线前用 `go test -race` 兜底。

3. **Java 里有 `volatile` 保证可见性，Go 为什么不做？如果 Go 有 `volatile`，"`v++` 不原子"这个坑会变少还是变多？**
   > 要点：`volatile` 只解决可见性与重排，不解决复合操作的原子性，于是给了"我已经安全了"的错觉——坑会**变多**（因为大家不再用锁/atomic）。Go 的策略是把选项收敛成三个明确的工具：`sync/atomic`（原子 + seq-cst）、锁/channel（互斥 + HB）、否则就是 data race（规范明说是错的）。另外 Go 编译器为了兼容 race 存在已经放弃了若干跨语句的优化，所以"防止优化掉循环"这个 Java 式刚需在 Go 里不成立。

---

## 23.9 延伸阅读

- **go.dev/ref/mem** —— The Go Memory Model（Version of June 6, 2022），尤其 "Incorrect synchronization" 与 "Incorrect compilation" 两节，值得逐字读
- `$GOROOT/src/runtime/race/` —— `race_linux_amd64.syso`、`doc.go`；`$GOROOT/src/runtime/race.go` 里的 `raceenabled` 与 linkname 钩子
- `$GOROOT/src/runtime/HACKING.md` —— 写 runtime 的人怎么思考同步（"don't use locks in the scheduler" 那套纪律）
- `$GOROOT/src/sync/atomic/doc.go` —— 内存序承诺与 1.19 typed atomic
- `$GOROOT/src/runtime/mfinal.go` —— `SetFinalizer` 与 `KeepAlive` 的实现
- Hans-J. Boehm & Sarita V. Adve, *Foundations of the C++ Concurrency Memory Model*（PLDI 2008）—— Go 与 Java 的 DRF-SC 都抄它
- Russ Cox, *Hardware Memory Models* / *Programming Language Memory Models*（两篇长文，讲清 StoreLoad 与为什么所有现代语言都在 DRF-SC 上达成一致）

## 23.10 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 22 章 sync 原语实现](22-sync-primitives.md)、[第 20 章 channel 原理](20-channel-internals.md)（channel 的 HB 实现）
- 下一章：[第 24 章 context：取消传播](24-context.md)
- 相关：[第 16 章 unsafe 与 cgo](16-unsafe-cgo.md)（`uintptr` 与 KeepAlive）、[第 25 章 并发陷阱](25-concurrency-pitfalls.md)、[第 34 章 测试](34-testing.md)（`-race` CI 门禁）
