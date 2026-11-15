# 18. GMP 数据结构与 `schedule()` 主循环逐段精读

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 并发与调度器源码 ｜ **编号**：18 / 54
> **本章定位**：上一章把单个 goroutine 拆开了，这一章把**它们怎么被分发到 CPU 上**拆开。读完你要能回答：为什么必须有 `P`（不是 G + M 就够了吗）？`schedule()` 那 40 行到底在干什么？`findRunnable()` 找活的顺序为什么是这个顺序？`go tool trace` 里那条长长的 `Runnable → Running` 间隙是谁造成的？
> **读法**：打开 `$GOROOT/src/runtime/proc.go`，`Ctrl+F` 搜 `func schedule()`，然后**顺着往下读**。本章源码均是删减版，`// ... 略` 表示我砍掉的分支（大多是 GC/metrics/lockedm 相关的边角），**一切以本地 `$GOROOT/src` 为准**。

---

## 18.1 引子：8 核机器，`schedtrace` 说只有 3 个 P 在干活

你写了一个批量 embedding 的服务，机器 8 核，压测 QPS 上不去。加一行环境变量：

```bash
GODEBUG=schedtrace=1000 ./agent
```

输出（每秒一行，真实格式）：

```
SCHED 2005ms: gomaxprocs=8 idleprocs=5 threads=12 spinningthreads=0 needspinning=0
   idlethreads=2 runqueue=0 [0 0 0 0 0 0 0 0]
SCHED 3005ms: gomaxprocs=8 idleprocs=5 threads=12 spinningthreads=0 needspinning=0
   idlethreads=2 runqueue=0 [0 0 0 0 0 0 0 0]
```

翻译成人话：**8 个 P 里 5 个在睡觉，全局和工作队列全空，12 个 OS 线程里有 2 个闲着**。也就是说 —— **CPU 根本没被喂饱，你在等的东西不是 CPU**。

再去看 trace：

```bash
go tool trace -http=:8081 trace.out   # 或者 go tool trace trace.out 用浏览器打开
```

在 "Goroutine analysis" 里你会看到某类 goroutine 的 `Runnable` 时间极短、`Running` 更短，而 **`Select/Block` 时间占满整条时间线**。真相是：你的 embedding 客户端有一条 HTTP 连接，一颗信号量（`sem` 或 `errgroup.SetLimit(4)`）把并发卡在 4，剩下的 100 个 goroutine 全在 `_Gwaiting` 等信号量。

> 🩸 **这个引子的教训**：`schedtrace` 里 `idleprocs` 高 + `runqueue` 空，等于"调度器饿着"，问题一定在 IO / 锁 / 限流，**不在 GMP 配置**。反过来，如果 `runqueue` 长期几十上百、`idleprocs=0`、`spinningthreads>0`，那才是真的 CPU 饱和。

要看懂这一行日志里每个数字从哪来，得先把三张表摆在桌上。

---

## 18.2 直觉模型：工厂、工人、工位

一句话：**G 是工单，M 是工人（OS 线程），P 是工位（执行许可证 + 一整套私有工具）**。

```
                        ┌────────────────────────────────────────────┐
                        │       全局待办区 sched.runq（gQueue）        │
                        │   M 抢不到工位时，工单先堆这儿（一把大锁）      │
                        └───────────────┬────────────────────────────┘
                                        │ globrunqget（按 P 均分搬一批）
              ┌─────────────────────────┼─────────────────────────┐
              ▼                         ▼                         ▼
      ┌───────────────┐         ┌───────────────┐         ┌───────────────┐
      │   P0 (工位)    │         │   P1 (工位)    │   ...   │   P7 (工位)    │
      │ runnext: G_a  │         │ runnext: nil   │         │ runnext: G_z   │
      │ runq[256] 环  │         │ runq[256] 环  │         │ runq[256] 环  │
      │ mcache        │         │ mcache        │         │ mcache        │
      │ timer 堆       │         │ timer 堆       │         │ timer 堆       │
      └───────┬───────┘         └───────┬───────┘         └───────┬───────┘
              │ 绑定（1:1，等价于"上岗"）│                          │
              ▼                         ▼                         ▼
      ┌───────────────┐         ┌───────────────┐         ┌───────────────┐
      │  M0 (线程)     │         │  M3 (线程)     │   ...   │  M9 (线程)     │
      │ curg = G_a    │         │ curg = G_b    │         │ curg = G_z    │
      │ g0 / gsignal  │         │ g0 / gsignal  │         │ g0 / gsignal  │
      └───────┬───────┘         └───────────────┘         └───────────────┘
              │ 真正执行机器指令的只有 M；M 数量可以 > P 数量（阻塞 syscall 时）
              ▼
          CPU 核心
```

三条铁律（记住这三条，GMP 就通了八成）：

1. **`G` 只跟 `M` 有关（`g.m`），跟 `P` 没有直接指针**；`P` 由 `M` 持有（`m.p`）。所以"这个 goroutine 在哪个 P 上"要问 `g.m.p`。
2. **同一时刻，一个 `P` 只能被一个 `M` 持有；一个 `M` 只能持有一个 `P`**。`P` 的数量 = `GOMAXPROCS`，这是**真正并行执行 Go 代码的上限**。
3. **`M` 的数量不受 `GOMAXPROCS` 限制** —— 阻塞系统调用、cgo、`LockOSThread` 都会额外造 M。这就是为什么 `threads` 经常远大于 `gomaxprocs`，也是容器里 CPU 被限流却"看不见"的根源（第 19、52 章）。

| 角色 | 是什么 | 谁创建 | 数量上限 | Java 里的对应物 |
| --- | --- | --- | --- | --- |
| `G` | 待执行的函数调用 + 栈 + 状态 | `newproc` / `malg` | 百万级 | `Runnable`/虚拟线程 |
| `M` | OS 线程 + `g0` + 当前 `curg` | `newm` → `clone` | 默认 10000（`maxmcount`） | 平台线程 / carrier |
| `P` | 执行许可 + 本地队列 + `mcache` + timer 堆 | `procresize`（启动时按 `GOMAXPROCS` 造） | = `GOMAXPROCS` | **没有对应物**（ForkJoinPool 的 Worker 一半像） |
| `sched` | 全局单例：空闲 M/P 表、全局队列、锁 | 静态变量 | 1 | `ForkJoinPool` 全局状态 |

## 18.3 底层实现

### 18.3.1 三张表：`schedt` / `p` / `m`

`runtime/runtime2.go`（删减版，只留调度相关字段）：

```go
// 全局单例，`var sched schedt`
type schedt struct {
	lock mutex // 保护下面的字段（全局队列、空闲表）

	// 空闲 M 表
	midle        muintptr // 空闲 M 链表头
	nmidle       int32    // 空闲 M 数量
	nmidlelocked int32    // 被 LockOSThread 绑住的空闲 M
	mnext        int64    // 已创建 M 数 / 下一个 M 的 ID
	maxmcount    int32    // M 数量上限（默认 10000），超了直接死
	nmsys        int32    // 不计入死锁检测的系统 M
	nmfreed      int64

	ngsys atomic.Int32 // 系统 goroutine 数（不算用户并发）

	// 空闲 P 表
	pidle        puintptr     // 空闲 P 链表头
	npidle       atomic.Int32 // 空闲 P 数量
	nmspinning   atomic.Int32 // 正在"找活"的 M 数量（spinning M）
	needspinning atomic.Uint32

	// ★ 全局可运行队列：M 没有 P 时的新 g 先进这里
	runq     gQueue // head/tail 单链表
	runqsize int32

	// 死 g 全局池（每个 P 还有自己的小池 p.gFree）
	gFree struct {
		lock    mutex
		stack   gList // 带栈的
		noStack gList // 栈已被回收的
		n       int32
	}

	freem *m // 待释放的 M

	gcwaiting atomic.Bool // GC 想停下来等所有 P 到安全点
	stopwait  int32
	stopnote  note
	sysmonwait atomic.Bool
	sysmonnote note
	sysmonlock mutex
}
```

```go
// P：执行许可 + 一整套私有资源。注意几乎所有字段都"无需加锁"——
// 因为同一时刻只有一个 M 持有它。
type p struct {
	id     int32
	status uint32 // _Pidle / _Prunning / _Psyscall / _Pgcstop / _Pdead
	link   puintptr
	m      muintptr // 反向指针：当前持有我的 M（idle 时为 nil）

	schedtick   uint32 // 每次 schedule() +1（sysmon 用它判断"跑了多久"）
	syscalltick uint32 // 每次系统调用 +1
	sysmontick  sysmontick

	mcache      *mcache    // 第 15 章：无锁分配的关键
	pcache      pageCache
	goidcache   uint64     // goid 批量分配缓存
	goidcacheend uint64

	// ★★ 本地可运行队列：无锁环形队列，只有 owner 能写
	runqhead uint32
	runqtail uint32
	runq     [256]guintptr
	// ★ runnext：下一个要跑的 g，实现"插队"（LIFO 局部性）
	runnext guintptr

	gFree struct { // 本地死 g 池
		gList
		n int32
	}

	sudogcache []*sudog // sudog 池（channel/select，第 20、21 章）
	mspancache struct {
		len int
		buf [128]*mspan
	}

	timersLock mutex    // 1.14~1.22：本地 timer 堆的锁（1.23 起改造，见第 19 章）
	timers     []*timer

	gcAssistTime     int64
	gcBgMarkWorker   func()
	gcMarkWorkerMode gcMarkWorkerMode
	// ...（GC / trace / profile 相关略）
}
```

```go
// M：OS 线程。数量可以远超 P，因为阻塞系统调用时 M 会脱离 P 但线程还在。
type m struct {
	g0      *g     // 调度器栈（第 17 章）
	morebuf gobuf  // morestack 用它保存现场
	procid  uint64
	gsignal *g     // 信号栈

	mstartfn func() // 启动函数（newm 时指定；mspinning 就是它）

	curg    *g       // ★ 当前正在跑的用户 goroutine
	p       puintptr // ★ 当前持有的 P（没有 P = 不在跑 Go 代码）
	nextp   puintptr // 即将接手的 P（用于唤醒）
	oldp    puintptr // 进系统调用前的 P（exitsyscallfast 想抢回来）
	id      int64

	spinning bool // ★ 正在找工作（parked 之前的状态）
	blocked  bool // 阻塞在 note 上

	incgo  bool // 正在执行 cgo
	isextra bool // 是"额外"的 M（cgo 回调创建）

	park   note // 停泊点（stopm 睡在这上面）
	alllink *m  // 挂在全局 allm 上
	schedlink muintptr
	lockedg guintptr // LockOSThread 绑住的 g

	// 让出 CPU 去跑调度器时，保存解锁函数和等待原因
	waitunlockf func(*g, unsafe.Pointer) bool
	waitlock    unsafe.Pointer
	waitreason  waitReason
	// ...（信号、trace、cgo traceback 相关略）
}
```

> 💡 **一个特别值得记住的设计细节**：`p.runq` 是 `[256]guintptr` 且 `runqhead`/`runqtail` **只在 head 上用原子载入、tail 只在 owner 上读写**（单写多读）。理论上还会有"acq/rel"语义（`atomic.LoadAcq`/`StoreRel`），保证别的 P 偷到的是完整发布的指针。这种"单生产者 + 多消费者 + 环形数组"的队列在 Go 里到处都是（`runq`、trace、`gcWork`），是无锁编程里最实用的一招，详见第 32 章。

### 18.3.2 `runnext` 的插队语义 + `runq` 环形队列

`p` 上其实是**两个队列**：一个只有 1 格但优先级最高的 `runnext`，一个 256 格的环形 `runq`。

```
              runqput(pp, g, next=true)             runqput(pp, g, next=false)
                        │                                      │
                        ▼                                      ▼
                ┌───────────────┐                    ┌────────────────────────────┐
                │ runnext = g   │                    │ runq 环形队列（256 格）      │
                │  （LIFO）      │  挤出来的老 runnext  │  head ──▶ ... ──▶ tail      │
                └───────────────┘ ──────────────────▶ │  （FIFO，满了就把一半搬到   │
                                                       │    全局 sched.runq）        │
                                                       └────────────────────────────┘
                                     runqget 的取活顺序： runnext → runq[head] → 全局
```

**为什么要有 `runnext`？** 因为「刚刚 ready 的 g」大概率马上就能用上刚被摸热的缓存 —— channel 发送方唤醒接收方、`go f()` 后紧接着 f 就要跑，这种场景下把 g 放队尾会让它白等一圈 256 个 g。放 `runnext` 等于**继承当前 g 剩余的时间片**（源码注释原话：*"It will inherit the time left in the current time slice"*）。代价是：**LIFO 会破坏公平性**，所以 `runqput` 里有个 `randomizeScheduler && randn(2) == 0` 的开关（只在 `GODEBUG=randomizedsched` 时打开）。

```go
// runtime/proc.go（删减）
func runqput(pp *p, gp *g, next bool) {
	if randomizeScheduler && next && cheaprandn(2) == 0 {
		next = false
	}
	if next {
	retryNext:
		oldnext := pp.runnext
		if !pp.runnext.cas(oldnext, guintptr(unsafe.Pointer(gp))) {
			goto retryNext
		}
		if oldnext == 0 {
			return // 原来没人在 runnext，直接占了
		}
		// 原来那个被挤出来，降级去跑普通队列
		gp = oldnext.ptr()
	}
retry:
	h := atomic.LoadAcq(&pp.runqhead) // 同步别的 P 的"偷"
	t := pp.runqtail                  // tail 只有我自己写
	if t-h < uint32(len(pp.runq)) {
		pp.runq[t%uint32(len(pp.runq))].set(gp) // t % 256：环形复用
		atomic.StoreRel(&pp.runqtail, t+1)      // release：别的 P 现在能偷了
		return
	}
	if runqputslow(pp, gp, h, t) { // 队列满了：一半本地 + 这个，丢全局队列
		return
	}
	goto retry
}

func runqget(pp *p) (gp *g) {
	// ① 先看 runnext（这就是"插队"被消费的地方）
	next := pp.runnext
	if next != 0 && pp.runnext.cas(next, 0) {
		return next.ptr()
	}
	// ② 再看环形队列头
	for {
		h := atomic.LoadAcq(&pp.runqhead)
		t := pp.runqtail
		if t == h {
			return nil
		}
		gp := pp.runq[h%uint32(len(pp.runq))].ptr()
		if atomic.CasRel(&pp.runqhead, h, h+1) { // cas 成功 = 我抢到了这个位置
			return gp
		}
	}
}
```

一个**能跑**的实验，直接观察 `runnext` 的 LIFO 效应（结果非常稳定）：

```go
package main

import (
	"fmt"
	"runtime"
	"sync"
)

func main() {
	runtime.GOMAXPROCS(1) // 单 P，才能观察到队列顺序
	var wg sync.WaitGroup
	wg.Add(100)
	for i := 0; i < 100; i++ {
		go func(i int) {
			defer wg.Done()
			fmt.Print(i, " ")
		}(i)
	}
	wg.Wait()
	fmt.Println()
}
```

你会看到近似这样的输出（`99` 最先，然后从 `0` 开始 FIFO）：

```
99 0 1 2 3 4 ... 98
```

原因：100 次 `go` 全在 main 里执行完（main 还没让出 CPU），`runnext` 反复被挤：`g99` 留在 `runnext`，`g0..g98` 挤进 `runq` 尾部。main 一阻塞在 `wg.Wait()`，调度器先取 `runnext`（99），再 FIFO 取 `runq`（0,1,...,98）。

> ⚠️ 这不是 bug，是**故意的局部性优化**。但如果你依赖"goroutine 执行顺序"，那你写的是不确定代码 —— 顺序在任何时候都可能变（`randomizedsched`、多 P、抢占）。需要顺序就用显式同步（channel 握手 / 序号）。

### 18.3.3 偷活：`runqgrab` 为什么一次偷一半

`runqsteal` 是 work-stealing 的落地：某个 P 空了，就去别的 P 的 `runq` 里"抓"一批走。

```go
// runtime/proc.go（删减）
func runqgrab(pp *p, batch *[256]guintptr, batchHead uint32, isIdle bool) uint32 {
	for {
		h := atomic.LoadAcq(&pp.runqhead)
		t := atomic.LoadAcq(&pp.runqtail)
		n := t - h
		n = n - n/2 // ★ 偷一半（向下取整）。剩一半留给原主
		if n == 0 {
			if isIdle && atomic.LoadAcq(&pp.runnext) != 0 {
				// 最后一次尝试时，连 runnext 也可以偷
				gp := pp.runnext.ptr()
				if !pp.runnext.cas(gp, 0) {
					continue
				}
				batch[batchHead%uint32(len(batch))] = guintptr(unsafe.Pointer(gp))
				return 1
			}
			return 0
		}
		if n > uint32(len(pp.runq)/2) { // 读到了不一致的 h/t，重来
			continue
		}
		for i := uint32(0); i < n; i++ {
			g := pp.runq[(h+i)%uint32(len(pp.runq))] // 整批 memcpy 出去
			batch[(batchHead+i)%uint32(len(batch))] = g
		}
		if atomic.CasRel(&pp.runqhead, h, h+n) { // 一次 CAS 原子地"拿走 n 个"
			return n
		}
	}
}

func runqsteal(pp, p2 *p, stealRunNextG bool) *g {
	t := pp.runqtail                              // 偷来的直接追加到自己的队尾
	n := runqgrab(p2, &pp.runq, t, stealRunNextG)
	if n == 0 {
		return nil
	}
	n--
	gp := pp.runq[(t+n)%uint32(len(pp.runq))].ptr() // 取最后一个（最新的那个）先跑
	if n == 0 {
		return gp
	}
	h := atomic.LoadAcq(&pp.runqhead)
	if t-h+n >= uint32(len(pp.runq)) {
		throw("runqsteal: runq overflow")
	}
	atomic.StoreRel(&pp.runqtail, t+n) // 把剩下的发布出去，供下一轮 runqget
	return gp
}
```

**为什么偷一半而不是偷一个？**

| 偷 1 个 | 偷一半（Go 的选择） |
| --- | --- |
| 每次都要跨核 CAS，抢锁成本高 | 一次 CAS 搬走半批，均摊成本低 |
| 偷完立刻又空，马上回来再偷 → **反复抖动** | 偷一次能跑一阵，减少争抢次数 |
| 原主队列不会被掏空 | 原主还剩一半，不会"刚被偷完就没活干了" |
| 但负载均衡粒度细 | 但一次搬太多会造成"搬运浪费"，所以上限是 `len/2` |

> 💡 这就是 work-stealing 里经典的 throughput vs latency 权衡。Java 的 `ForkJoinPool` 偷的是**单个任务**（`ForkJoinWorkerThread` 从队头偷），因为它的任务粒度通常粗（一个 RecursiveTask 可能跑几毫秒）；Go 的 g 粒度可以极细（一个 goroutine 可能只有几百纳秒），必须批量搬。**同一个算法，粒度不同，实现就完全不同。**

`stealWork` 才是调度器层的"偷活驱动"：最多尝试 **4 轮**，每轮用 `stealOrder`（一个伪随机的全排列遍历，避免多个 P 同时抢同一个受害者）走遍所有 P：

```go
// runtime/proc.go（大幅删减，只保留骨架）
func stealWork(now int64) (gp *g, inheritTime bool, rnow, pollUntil int64, newWork bool) {
	pp := getg().m.p.ptr()
	const stealTries = 4
	for i := 0; i < stealTries; i++ {
		stealTimersOrRunNextG := i == stealTries-1 // 最后一轮才允许偷 timer/runnext
		for enum := stealOrder.start(cheaprand()); !enum.done(); enum.next() {
			if sched.gcwaiting.Load() {
				return nil, false, now, pollUntil, true
			}
			p2 := allp[enum.position()]
			if pp == p2 {
				continue
			}
			if !stealTimersOrRunNextG {
				if stealTimers(p2, now) { ... } // 顺带看看别人的 timer 有没有到期
			}
			if gp := runqsteal(pp, p2, stealTimersOrRunNextG); gp != nil {
				return gp, false, now, pollUntil, true
			}
		}
	}
	return nil, false, now, pollUntil, false
}
```

### 18.3.4 全局队列：`globrunqget` 与 `runqputslow`

全局队列 `sched.runq` 是一个带**一把互斥锁**的单链表（`gQueue`），它承担两个职责：**① 本地队列溢出时的泄洪区；② 没有 P 的时候（比如 `go` 语句在 `_Gsyscall` 边界被执行的场景）的临时容身处**。因为它带锁，所以进出的都是**批量**：

```go
// runtime/proc.go（删减）
func globrunqget(pp *p, max int32) *g {
	assertLockHeld(&sched.lock)
	if sched.runqsize == 0 {
		return nil
	}
	// 按 GOMAXPROCS 均分：原则是"别让一个 P 一次搬太多，也别让全局队列长期挂着"
	n := sched.runqsize/gomaxprocs + 1
	if n > sched.runqsize {
		n = sched.runqsize
	}
	if max > 0 && n > max {
		n = max
	}
	if n > int32(len(pp.runq))/2 { // 最多搬半个本地队列
		n = int32(len(pp.runq)) / 2
	}
	sched.runqsize -= n
	gp := sched.runq.pop() // 给调用者一个（直接返回去执行）
	n--
	for ; n > 0; n-- {
		gp1 := sched.runq.pop()
		runqput(pp, gp1, false) // 剩下的塞本地队列尾部
	}
	return gp
}
```

> 💡 网上常说"全局队列一次搬 1/4"，指的是早期版本里"按 `sched.runqsize/4` 左右均分"的口头总结。**当前实现是按 `GOMAXPROCS` 均分、并被 `len(runq)/2` 封顶**。搬运比例这种细节近年一直在微调，请以你本地 `$GOROOT/src/runtime/proc.go` 的 `globrunqget` 为准。

`runqputslow` 是反向的泄洪：本地队列满了，就把**一半本地 + 新来的这个**一起丢给全局：

```go
func runqputslow(pp *p, gp *g, h, t uint32) bool {
	var batch [len(pp.runq)/2 + 1]*g
	n := t - h
	n = n / 2 // 一半
	if n != uint32(len(pp.runq)/2) {
		throw("runqputslow: queue is not full")
	}
	for i := uint32(0); i < n; i++ {
		batch[i] = pp.runq[(h+i)%uint32(len(pp.runq))].ptr()
	}
	if !atomic.CasRel(&pp.runqhead, h, h+n) { // 原子地"消费掉"这 n 个
		return false
	}
	batch[n] = gp
	if randomizeScheduler { // 打散，避免相关性
		for i := uint32(1); i <= n; i++ {
			j := cheaprandn(i + 1)
			batch[i], batch[j] = batch[j], batch[i]
		}
	}
	for i := uint32(0); i < n+1; i++ {
		batch[i].schedlink.set(batch[i+1]) // 串成链表
	}
	var q gQueue
	q.head.set(batch[0])
	q.tail.set(batch[n])
	lock(&sched.lock)
	globrunqputbatch(&q, int32(n+1))
	unlock(&sched.lock)
	return true
}
```

### 18.3.5 `schedule()` 逐段精读

这是整个 runtime 的心脏，不到 40 行（删掉 GC/lockedm 分支后）。它的契约是：**永不返回** —— 函数结尾一定是 `execute(gp, ...)` → `gogo`，把控制流交给某个 goroutine。

```go
// runtime/proc.go（删减版；// [n] 是我加的段号）
func schedule() {
	mp := getg().m // 这条永远成立：schedule 只跑在 g0 上

	// [1] 断言：调度器不能持锁
	if mp.locks != 0 {
		throw("schedule: holding locks")
	}
	// [2] LockOSThread 场景：把 M 停到"等那个 g"的泊位上
	if mp.lockedg != 0 {
		stoplockedm()
	}
	// [3] cgo 里不该进调度器
	if mp.incgo {
		throw("schedule: in cgo")
	}

top:
	// [4] 拿回自己的 P（可能刚被唤醒，P 是 m.nextp 接过来的）
	pp := mp.p.ptr()
	pp.preempt = false // 清掉抢占标记（新的时间片开始）

	// [5] 不变式：正在"找活"的 M，本地必须有活不成立时才能找 —— 这里反而要求有活就别 spinning
	if mp.spinning && (pp.runnext != 0 || pp.runqhead != pp.runqtail) {
		throw("schedule: spinning with local work")
	}

	// [6] ★ 核心：找活。可能阻塞（netpoll / stopm），可能被唤醒后 goto top 重来
	gp, inheritTime, tryWakeP := findRunnable()

	// [7] GC 想停下来（freezetheworld 调试场景），略
	if debug.dontfreezetheworld > 0 && sched.gcwaiting.Load() {
		// ... 略
	}

	// [8] P 正处于系统调用状态却被拿来调度，说明状态错乱（stopwait 与 handoff 的交界）
	if sched.stopwait > 0 && mp.p.ptr().status == _Psyscall {
		// ...
	}

	// [9] 这个 g 想被锁在别的 M 上（LockOSThread 的 g 换 M）
	if gp.lockedm != 0 {
		startlockedm(gp) // 会 stopm 当前 M，把 g 交给它锁定的 M
		goto top
	}

	// [10] 真正执行：状态 _Grunnable → _Grunning，然后 gogo(&gp.sched)
	execute(gp, inheritTime)

	// [11] 传参：新 P 起来了要唤醒一个 M
	if tryWakeP {
		wakep()
	}
}
```

逐段读的重点只有 4 个：

| 段 | 代码 | 它在防止什么 | 你要记住的 |
| --- | --- | --- | --- |
| [1] | `mp.locks != 0` → throw | 持锁去调度 → 死锁 | `schedule` 不能在 `lock()` 里面被调到（`gopark` 里 `mcall` 前必须解锁） |
| [4] | `pp := mp.p.ptr()` | `m.p == nil` 就崩 | 调度**必须**持有 P；没 P 的 M 只能 `stopm` 睡在 `sched.midle` 上 |
| [6] | `findRunnable()` | 找不到活时**主动**阻塞 | 阻塞发生在 g0 上，**不占用户栈**；它可能 `netpoll` 阻塞或 `stopm` 停泊 |
| [10] | `execute(gp, inheritTime)` | —— | 一次调度就一次 `gogo`；`inheritTime` 表示"这次不算新时间片"（`runnext` 继承语义） |

> ⚠️ 一个非常常见的误解：`schedule()` 不是"时间片轮转器"。**Go 没有时间片的概念**。`inheritTime` 只是给 `p.schedtick` 记账：从 `runnext` 出来的 g 不算新的一次调度，于是它"接着上一个 g 剩余的时间片"继续跑。抢占的判定根本不看这个计数，而是看 `sysmon` 有没有超过 10ms 没换人（第 19 章）。

### 18.3.6 `findRunnable()`：四级找活顺序（实际是五级）

招牌图先上 —— 这张图值得手绘十遍：

```
                    findRunnable()
                          │
        ┌─────────────────▼──────────────────┐
        │ 0. GC 想停？gcwaiting → gcstopm 重来 │
        └─────────────────┬──────────────────┘
                          ▼
        ┌────────────────────────────────────┐
        │ 1. runnext（p.runnext，LIFO 插队）  │──有──▶ return
        └─────────────────┬──────────────────┘
                          ▼
        ┌────────────────────────────────────┐
        │ 2. 本地 runq[head]（FIFO）          │──有──▶ return
        └─────────────────┬──────────────────┘
                          ▼   (每 61 次调度先看一次全局，保证公平)
        ┌────────────────────────────────────┐
        │ 3. 全局 sched.runq（加锁，批量搬）   │──有──▶ return
        └─────────────────┬──────────────────┘
                          ▼
        ┌────────────────────────────────────┐
        │ 4. netpoll(0) 非阻塞捞 IO 就绪的 g   │──有──▶ return
        └─────────────────┬──────────────────┘
                          ▼
        ┌────────────────────────────────────┐
        │ 5. stealWork：随机顺序偷别的 P 的队列 │──有──▶ return
        │    （最多 4 轮，最后一轮偷 runnext）  │
        └─────────────────┬──────────────────┘
                          ▼  都没活
        ┌────────────────────────────────────┐
        │ 6. GC mark worker 需要人吗？         │──需要─▶ return（帮 GC 干活）
        └─────────────────┬──────────────────┘
                          ▼
        ┌────────────────────────────────────┐
        │ 7. 阻塞在 netpoll(delay) 上（delay   │──有──▶ return
        │    由最近的 timer 到期时间决定）      │
        └─────────────────┬──────────────────┘
                          ▼
        ┌────────────────────────────────────┐
        │ 8. stopm()：P 放回 sched.pidle，M 睡  │
        │    在 m.park 上，直到被 wakep 唤醒    │
        │    → 醒来 goto top，重新找活          │
        └────────────────────────────────────┘
```

关键代码（大幅删减，每段都加了注释）：

```go
// runtime/proc.go
func findRunnable() (gp *g, inheritTime, tryWakeP bool) {
	mp := getg().m

top:
	pp := mp.p.ptr()

	// [0] GC 想跑但需要所有 P 停下来（stop-the-world 的入口之一）
	if sched.gcwaiting.Load() {
		gcstopm() // 把 P 交出去，自己停
		goto top
	}
	if pp.runSafePointFn != 0 {
		runSafePointFn()
	}

	// [1] 公平性：每 61 次调度强制看一次全局队列，
	//     否则两个互相 spawn 的 goroutine 会永远霸占本地队列
	if pp.schedtick%61 == 0 && sched.runqsize > 0 {
		lock(&sched.lock)
		gp = globrunqget(pp, 1) // max=1：只要一个，剩下的分给本地
		unlock(&sched.lock)
		if gp != nil {
			return gp, false, false
		}
	}

	// [2] 本地队列（runqget 内部先看 runnext，再 FIFO 取 runq）
	if gp, inheritTime := runqget(pp); gp != nil {
		return gp, inheritTime, false
	}

	// [3] 全局队列
	if sched.runqsize != 0 {
		lock(&sched.lock)
		gp := globrunqget(pp, 0)
		unlock(&sched.lock)
		if gp != nil {
			return gp, false, false
		}
	}

	// [4] 非阻塞 netpoll：把已经就绪的 IO goroutine 捞出来
	//     仅在"有等待者 且 有别的线程在 netpoll 里阻塞着"时才尝试
	if netpollinited() && netpollAnyWaiters() && sched.lastpoll.Load() != 0 {
		if list, delta := netpoll(0); !list.empty() { // delay=0 → epoll_wait 不阻塞
			gp := list.pop()
			injectglist(&list) // 其余的塞全局队列
			// ...（更新 metrics）
			return gp, false, false
		}
	}

	// [5] 偷活。注意限制：spinning M 不超过 busy P 的一半，避免空转烧 CPU
	if mp.spinning || 2*sched.nmspinning.Load() < gomaxprocs-sched.npidle.Load() {
		if !mp.spinning {
			mp.becomeSpinning() // nmspinning++，并置 spinning=true
		}
		gp, inheritTime, now, w, newWork := stealWork(now)
		if gp != nil {
			return gp, inheritTime, false
		}
		if newWork {
			goto top // 偷的过程中发现 GC 有活，从头来
		}
		pollUntil = w
	}

	// [6] GC 标记阶段：还有后台标记任务，就自己去当 mark worker
	if gcBlackenEnabled != 0 {
		if gp := gcController.findRunnableGCWorker(pp, now); gp != nil {
			return gp, false, false
		}
	}

	// [7] 真没活了：阻塞在 netpoll 上（delay 由最近的 timer 决定）
	if netpollinited() && (netpollAnyWaiters() || pollUntil != 0) && sched.lastpoll.Swap(0) != 0 {
		sched.pollUntil.Store(0)
		if list, delta := netpoll(delay); !list.empty() { // 这里会真的睡在 epoll_wait
			gp := list.pop()
			injectglist(&list)
			return gp, false, false
		}
	}
	// 注意：醒来后要先"退出 spinning"状态并释放 P，才能去睡

	// [8] 最后的最后：把 P 交回去，自己停泊
	stopm()
	goto top
}
```

**为什么必须是这个顺序？** 每个位置都在回答一个具体问题：

| 顺序 | 位置 | 为什么在这 | 换掉会怎样 |
| --- | --- | --- | --- |
| 0 | 本地（runnext/runq） | 最快、无锁、缓存最热 | 先全局 → 白白抢锁，多核压测下全局锁成瓶颈 |
| 0.5 | 每 61 次看全局 | 61 不是魔法数，是"够大不干扰局部性，够小防饿死"的经验值 | 不做 → 两个互 spawn 的 goroutine 饿死全局队列里的活 |
| 1 | netpoll(0) 在偷之前 | 本地 + 全局都空时，IO 就绪的 g 比"偷来的"更可能马上跑完（且唤醒它是别人的期待） | 放到偷之后 → IO 延迟白白多等一轮偷活 |
| 2 | steal | 跨核成本最高，但能均摊负载 | 放前面 → 无谓的跨核 CAS |
| 3 | GC worker | 标记阶段必须有人干活，否则 GC 卡住 | 不做 → `mark assist` 撑爆用户 goroutine |
| 4 | netpoll(delay) 阻塞 | 唯一能"睡到有事件"的入口，delay 与 timer 对齐 | 用 `usleep` 轮询 → 空转烧 CPU |

> 💡 记住一个反直觉的事实：**所谓"闲着没事干的 M"并不是在睡觉**。当一个 M 找不到活且允许 spinning 时，它会去偷；偷不到才 `stopm` 睡在 `note` 上。而 `sched.nmspinning` 就是"当前有几个 M 在到处乱翻"的计数器 —— `spinningthreads` 长期 > 0 且 CPU 打满，说明你在**过度并行**（第 32 章会讲它怎么变成 P99 的杀手）。

### 18.3.7 `execute()` 与 `gogo`：最后一步

```go
// runtime/proc.go（删减）
func execute(gp *g, inheritTime bool) {
	mp := getg().m

	// ① 双向绑定：m.curg ↔ g.m
	mp.curg = gp
	gp.m = mp

	// ② 状态：_Grunnable → _Grunning（cgo 回来的 g 是 _Grunning，这里用 casfrom_Gscanstatus 兼容）
	casgstatus(gp, _Grunnable, _Grunning)
	gp.waitsince = 0 // 不再阻塞了，pprof 里那个 "12 minutes" 归零
	gp.preempt = false
	if !inheritTime {
		mp.p.ptr().schedtick++ // 记账：一次新调度
	}

	// ③ CPU profile 的采样频率可能变了（runtime.SetCPUProfileRate）
	if mp.profilehz != sched.profilehz {
		setThreadCPUProfiler(sched.profilehz)
	}

	// ④ 切栈：把寄存器恢复成 gp 上次停下的样子，然后 JMP
	gogo(&gp.sched) // 不返回
}
```

`gogo` 的汇编在 17.3.3 已拆过。**这里只要记住：`execute` 是"调度器 → 用户代码"的唯一出口，`mcall`/`gopark` 是"用户代码 → 调度器"的唯一入口。** 整个 GMP 状态机就是在这两个出口之间反复横跳：

```
   g0 栈（调度器视角）                        用户 g 栈
  ┌──────────────────────┐                ┌──────────────────┐
  │ schedule()           │   execute()    │ 你的代码          │
  │   findRunnable()     │ ─────gogo─────▶│   ...            │
  │   execute()          │                │   gopark()       │
  │                      │◀────mcall──────│   (或 morestack / │
  │                      │                │    entersyscall) │
  └──────────────────────┘                └──────────────────┘
```

### 18.3.8 M 的生命周期：`startm` / `wakep` / `stopm` / `handoffp`

```
                      ┌──────────────── 空闲（睡在 note 上，挂在 sched.midle）────────────────┐
                      │                                                                    ▲
      startm(pp,false)│  mget() 拿到一个 → nextp=pp → notewakeup → 醒来继续 schedule()      │ stopm()
                      ▼                                                                    │ （P 放回 pidle、
   ┌──────────────┐  有活就继续   ┌──────────────┐   进 syscall      ┌───────────────┐      │  M 放回 midle）
   │  M 执行 Go 代码│────────────▶│  P 被 handoff │◀─────────────────│  _Gsyscall     │      │
   └──────┬───────┘               └──────┬───────┘   sysmon retake   └───────┬───────┘      │
          │                              │                                   │              │
          │ 找不到活                      │ 这个 M 继续跑完 syscall            │ exitsyscallfast
          ▼                              │ 然后 exitsyscall → 抢 P（抢不到就    │ 抢回 oldp 成功
   ┌──────────────┐                      │ 进 pidle 睡）                      ▼              │
   │ mp.spinning  │                      │                            回到 Go 代码           │
   │ =true（偷活） │──────────────────────┴───────────────────────────────────────────────────┘
   └──────────────┘
```

```go
// runtime/proc.go（删减）
// startm：给 pp 找一个 M 来跑；pp==nil 表示"拿一个空闲 P 给新 M"
func startm(pp *p, spinning bool) {
	mp := acquirem() // 禁止抢占
	if pp == nil {
		if spinning {
			if sched.nmspinning.Add(-1) < 0 {
				throw("startm: negative nmspinning")
			}
		}
		lock(&sched.lock)
		pp = pidleget() // 从空闲 P 表里摘一个
		unlock(&sched.lock)
		if pp == nil { // 没有空闲 P（说明并行度已满），什么都不做
			releasem(mp)
			return
		}
	}
	nmp := mget() // 从空闲 M 表里摘一个
	if nmp == nil {
		// 没有空闲 M：造一个新的 OS 线程（clone）
		id := mReserveID()
		unlock(&sched.lock)
		var fn func()
		if spinning {
			fn = mspinning // 新 M 一启动就进入 spinning 状态
		}
		newm(fn, pp, id) // 注意：newm 在 g0 上、会 mstart
		releasem(mp)
		return
	}
	releasem(mp)
	nmp.spinning = spinning
	nmp.nextp.set(pp)
	notewakeup(&nmp.park) // 唤醒它
}

// wakep：确保至少有一个 spinning M（有新活儿时调用，比如 runqput 之后）
func wakep() {
	// CAS 0→1：只唤醒一个，避免惊群
	if sched.nmspinning.Load() != 0 || !sched.nmspinning.CompareAndSwap(0, 1) {
		return
	}
	startm(nil, true)
}

// stopm：把自己停泊（P 必须已经交出去）
func stopm() {
	gp := getg()
	if gp.m.locks != 0 {
		throw("stopm holding locks")
	}
	if gp.m.p != 0 {
		throw("stopm holding p") // ★ 常见崩溃点：忘了 releasep
	}
	if gp.m.spinning {
		throw("stopm spinning")
	}
	lock(&sched.lock)
	mput(gp.m) // 挂到 sched.midle
	unlock(&sched.lock)
	mPark() // notesleep(&m.park)
	acquirem()
}

// handoffp：把这个 P 交出去（进 syscall / GC 停世界时用）
func handoffp(pp *p) {
	// 有本地活 或 全局队列有活 → 立刻找个 M 接着跑
	if !runqempty(pp) || sched.runqsize != 0 {
		startm(pp, false)
		return
	}
	// 有 GC 后台标记任务 → 也要人
	if gcBlackenEnabled != 0 && gcMarkWorkAvailable(pp) {
		startm(pp, false)
		return
	}
	// 没活：如果连 spinning M 都没有，先起一个去找活
	if sched.nmspinning.Load()+sched.npidle.Load() == 0 && sched.nmspinning.CompareAndSwap(0, 1) {
		sched.needspinning.Store(1)
		startm(nil, true)
		return
	}
	// 否则把 P 放回空闲表（这时候可能触发 stopm 让 M 睡）
	lock(&sched.lock)
	pidleput(pp)
	unlock(&sched.lock)
}
```

| 函数 | 什么时候被调 | 后果 | 排障意义 |
| --- | --- | --- | --- |
| `startm(pp, spinning)` | 有新活但 P 没 M / `wakep` | 新建或唤醒 M | `threads` 指标上涨的直接原因 |
| `wakep()` | `runqput` 后、`ready()` 时 | CAS 保证只唤醒一个 | 惊群控制 |
| `stopm()` | `findRunnable` 找不到活 | P 归还 + M 睡 | `idlethreads` / `idleprocs` 上涨 |
| `handoffp(pp)` | `entersyscall` 准备阻塞 / `retake` | P 换给别的 M | 阻塞 syscall 不拖累并行度（第 19 章） |
| `mget` / `mput` | 上面几个的内部 | 维护 `sched.midle` | M 池化，避免频繁 clone |

> ⚠️ `maxmcount` 默认 10000（`runtime/debug.SetMaxThreads` 可改）。**如果日志里出现 `runtime: program exceeds 10000-thread limit`，几乎百分百是"阻塞式系统调用太多"**（比如 `os.Open` 读 NFS、cgo 里长阻塞、`syscall.Read` 某些 fd 不可 poll），因为每个阻塞的 syscall 都要独占一个 M。这是容器里 `threads` 飙到几百上千的经典原因。

### 18.3.9 为什么必须有 P？（不是"G + M 就够了吗"）

这是 Go 面试里最能分出"读过源码"和"背过八股"的一题。四个理由，每一个都是硬需求：

| # | P 带来了什么 | 去掉 P 会怎样 | 详见 |
| --- | --- | --- | --- |
| 1 | **每个 P 一份 `mcache`**（第 15 章） | 分配内存要抢 `mcentral`/`mheap` 的锁，百万 QPS 下分配器直接崩 | 15 章 |
| 2 | **每个 P 一个无锁 `runq`** | 所有 g 抢全局队列的一把大锁；`runq` 会成最大热区 | 本章 18.3.2 |
| 3 | **把"执行 Go 代码"的并发度与线程数解耦** | 阻塞 syscall 时线程被占，想保持并行只能多起线程，但 CPU 只有那么多 → 抖动 | 19 章 |
| 4 | **并行 GC 的根基**（`gcBgMarkWorker` 挂在 P 上，work buffer 是 per-P 的） | 标记工作无法 per-P 分摊，`mark assist` 完全没法按 P 计量 | 28 章 |

再加一条**软性但致命**的：

| 5 | **它是一把"资源所有权"的凭证** | `m.p == nil` 就表示"M 现在不该碰任何 Go 状态"（栈、内存、timer、GC work）。这让"阻塞时把 P 交出去"变成**一次指针赋值**，而不是一场加锁大迁移 | 本章 18.3.8 |

> 🎯 **判断力演示**：当你的服务 `threads=800`、`gomaxprocs=8`、`idleprocs=0`，说明**8 个 P 全忙，另外 792 个线程在干别的**（阻塞 syscall / cgo / GC 辅助）。此时加 `GOMAXPROCS` 没用（CPU 就那么多），要查的是"谁在阻塞"。反过来 `threads=9`、`gomaxprocs=8`、`idleprocs=4` 才是"CPU 没喂饱"。

---

## 18.4 Java 对照：ForkJoinPool 是同构，但不完全是

`ForkJoinPool` 是 JVM 世界里**唯一**和 GMP 结构相似的东西 —— 因为它也做了 work-stealing。两者可以逐点对上：

| 维度 | Java `ForkJoinPool` | Go GMP | 差异带来的实际后果 |
| --- | --- | --- | --- |
| 工作单元 | `ForkJoinTask`（`RecursiveTask`/`RecursiveAction`） | `g`（任意函数调用） | Java 要包装成 Task；Go 一个 `go` 语句即可，**粒度可以细到几百纳秒** |
| 执行者 | `ForkJoinWorkerThread`（普通 OS 线程） | `M`（OS 线程） | 同构 |
| 并行度上限 | `parallelism`（默认 `CPU-1`） | `GOMAXPROCS`（默认 `CPU`，1.25 起容器感知） | Java 默认留 1 核给"别的"，Go 默认全用 |
| 本地队列 | 每个 worker 一个 `WorkQueue[]`（双端队列，`top`/`base` 用 CAS） | 每个 P 的 `runq[256]` 环 + `runnext` | 都是"自己 push/pop 一端，别人偷另一端" |
| 偷多少 | **偷 1 个**（从对端 `base` 取） | **偷一半**（`runqgrab`） | 见 18.3.3：任务粒度决定的 |
| 新任务插队 | `push` 到 `top`（LIFO，本线程优先执行刚 fork 的） | `runnext`（LIFO，一个格） | **同一个设计意图**：缓存局部性 |
| 阻塞处理 | 有 `managedBlock`、`ForkJoinPool.managedBlock`，阻塞时会补偿 worker | `entersyscall` 直接把 P 交出去（**完全自动**） | Go 不需要你在代码里写补偿 |
| 全局队列 | 外部提交的任务进 `submission queue` | `sched.runq` + `sched.lock` | 同构 |
| 定时/IO 唤醒 | 无（要自己起线程） | `netpoll` + timer 直接唤醒 g | Go 的调度器**自带 IO 多路复用**，Java 要靠 NIO/虚拟线程 |

**最本质的差异**：Java 的 `ForkJoinPool` 是**库**，你把任务丢进去；Go 的调度器是**runtime**，任何阻塞点（channel、mutex、IO、syscall）都由 runtime 兜底。所以在 Java 里你必须"知道自己在用线程池"，在 Go 里你只需要 `go`。

> 💡 反过来说，Java 的 `ForkJoinPool` 有一个 Go 没有的好处：**它是可替换、可观测、可配置的**。Go 的调度器要调参只有 `GOMAXPROCS` 一个旋钮（1.25 起再加容器感知），别的都改不了。这不全是坏事 —— 少一个旋钮，少一堆事故。

---

## 18.5 框架与工程落地：`go tool trace` 怎么读

### ① 先跑一个能出 trace 的程序

```go
// go run main.go && go tool trace -http=:8081 trace.out
package main

import (
	"context"
	"fmt"
	"os"
	"runtime"
	"runtime/trace"
	"sync"
	"time"
)

func stage(ctx context.Context, name string, d time.Duration) {
	// 自定义 task/region，在 trace 里能按名字过滤
	ctx, task := trace.NewTask(ctx, name)
	defer task.End()
	time.Sleep(d)
	trace.Log(ctx, "stage", name)
}

func main() {
	f, err := os.Create("trace.out")
	if err != nil {
		panic(err)
	}
	defer f.Close()
	if err := trace.Start(f); err != nil {
		panic(err)
	}
	defer trace.Stop()

	fmt.Println("GOMAXPROCS =", runtime.GOMAXPROCS(0))

	ctx := context.Background()
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ { // 8 个"worker"
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			stage(ctx, fmt.Sprintf("worker-%d", i), 20*time.Millisecond)
		}(i)
	}
	wg.Wait()
	fmt.Println("done: go tool trace -http=:8081 trace.out")
}
```

### ② trace 里该看什么（按你的问题选视图）

| 你的问题 | trace 里看哪个视图 | 判断标准 |
| --- | --- | --- |
| 并行度够不够？ | `View trace` 顶部每行的 P 轨道 | P 轨道有空白的连续大片 → 没喂饱（回到 18.1 引子） |
| 某个 goroutine 为什么慢？ | `Goroutine analysis` → 选一类 | 看 `Runnable` 时间：**长 = 排不上队（CPU 挤）**；`Running` 长 = 真在算；`Block` 长 = IO/锁 |
| 调度延迟（runnable→running） | `Scheduler latency profile`（`go tool trace` 菜单里）或 `go tool trace -pprof=sched` | P99 > 几 ms 就是问题，先看 `GOMAXPROCS` 与 CPU limit |
| GC 影响了多少 | `View trace` 里的 GC 行 + `goroutine` 的 `mark assist` | assist 大面积出现 = 分配太快，见第 28、30 章 |
| 谁在阻塞别的 g | `Sync blocking profile` | `sync.Mutex` / channel 分块排队 |
| syscall 拖累 | `Syscall blocking profile` | 阻塞 syscall 越多，M 越多（`threads` 涨） |

### ③ 三个"看 trace 就能定性"的经典场景

| trace 特征 | 结论 | 处方 |
| --- | --- | --- |
| P 轨道几乎全绿（Running），`Runnable` 队列长，GC 频繁 | CPU 饱和 + 分配过多 | 减少分配（第 30 章）、降并发、加副本 |
| P 轨道大量空白，`Block` 占比高 | 并发度不足/IO 慢 | 提高并发、换连接池、检查下游（第 32 章） |
| 大量 goroutine 在 `Runnable` 排队，但 P 轨道有空 | **P 数量 < 需求**（`GOMAXPROCS` 太小，或容器 CPU limit 压着） | 对齐 `GOMAXPROCS` 与 limit（1.25 自动，见 19 章） |

> ⚠️ 一个非常常见的坑：**trace 文件是"全量事件流"，几秒钟就能上百 MB**。生产上不要长时间开 `trace.Start`，要么按需开 5~10 秒，要么 1.25+ 用 `runtime/trace.FlightRecorder`（环形缓冲，事后导出）。另外 trace 本身有可观开销（会记录每个 G 的每次状态变化），压测数字要打折看。

### ④ 在框架里的落地

| 框架/场景 | 和 GMP 相关的可调项 | 建议 |
| --- | --- | --- |
| `net/http` Server | 每连接一 goroutine，无内建池 | 用 `MaxConnsPerHost`（客户端）/ 反向代理 `MaxIdleConns` 控并发，别靠"goroutine 便宜"硬扛（第 32、38 章） |
| `x/sync/errgroup` | `SetLimit(n)` 只是信号量，不改 GMP | `n` 按"下游能承受的并发"设，不要按 CPU 数设 |
| `sourcegraph/conc` / `panjf2000/ants` | 自带 worker pool | 高频小任务（如 token 级处理）用它，省 `newproc1` 与栈分配 |
| `GOMAXPROCS` | 唯一旋钮 | 1.25 前手动与 CPU limit 对齐；1.25 起容器感知默认值，可用 `runtime.SetDefaultGOMAXPROCS()` 恢复/重设 |
| 数据库/HTTP 连接池 | `SetMaxOpenConns`、`Transport.MaxConnsPerHost` | 这两个才是真正的并发闸门，**它们小于 QPS 时，goroutine 再多也只是排队** |

---

## 18.6 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| `idleprocs` 很多但 QPS 上不去 | 并发被信号量/连接池/锁卡住，不是 CPU 问题 | 查 trace 的 `Block` 占比与 `Select` 时间，先放宽下游闸门 | 32 章 |
| `threads` 远大于 `gomaxprocs` | 阻塞系统调用 / cgo 各占一个 M | 让 IO 走 netpoller（`os.File` 可 poll 的走异步）；避免不可 poll 的阻塞读 | 19 章 |
| `runqueue` 长期几十上百、`spinningthreads>0` | CPU 真饱和 / 任务太小导致调度开销占比高 | 加副本、批处理、减少 goroutine 数量 | 30、32 章 |
| 依赖 goroutine 执行顺序 | `runnext` 是 LIFO、多 P 会乱序、还有抢占 | 用 channel 握手 / 序号显式排序 | 本章 18.3.2 |
| 认为 `GOMAXPROCS` 越大越快 | 它只决定 P 数，超出 CPU limit 只会引发 CFS 限流 | 与 CPU limit 对齐（1.25 自动） | 19、52 章 |
| 在 `init` 里 `go` 大量任务 | `mainStarted=false` 时不会 `wakep`，且可能只有 1 个 P | 放到 `main` 里显式启动 + 有界池 | 本章 18.3.8 |
| `runtime: program exceeds 10000-thread limit` | 阻塞 syscall 太多 | 换异步 IO、限制 cgo 阻塞、`debug.SetMaxThreads` 只是掩盖 | 19 章 |
| 用 `GOMAXPROCS=1` 做基准测试然后推广结论 | 单 P 下看不到并行与偷活，也掩盖数据竞争 | 压测用真实 `GOMAXPROCS` + `-race` 单独跑 | 25、30 章 |

---

## 18.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| GMP | G=待跑的函数，M=OS 线程，P=执行许可（数量=`GOMAXPROCS`） |
| 绑定关系 | `m.p` 决定谁在跑 Go 代码；`m.curg` 决定跑的是谁；`g.m` 反向指 |
| `runnext` | 1 格 LIFO 插队槽，继承当前时间片，为缓存局部性服务 |
| `runq[256]` | per-P 无锁环（head 原子、tail 单写），满了搬一半去全局 |
| 偷活 | `runqgrab` 一次偷一半，`stealWork` 最多 4 轮、随机顺序 |
| 全局队列 | `sched.runq` + 一把锁；`globrunqget` 按 `GOMAXPROCS` 均分搬 |
| `schedule()` | 永不返回；`findRunnable` → `execute` → `gogo` |
| `findRunnable` | 本地 → 全局(每 61 次) → netpoll(0) → 偷 → GC worker → 阻塞 netpoll → stopm |
| `execute` | `_Grunnable→_Grunning`，绑定 `m.curg`，然后 `gogo` |
| 为什么需要 P | mcache、无锁本地队列、并行度与线程数解耦、并行 GC |
| `handoffp` | 阻塞 syscall 时把 P 交出去，线程数不拖累并行度 |
| 排障口径 | `idleprocs` 高 + runqueue 空 = 饿着；runqueue 长 + 空闲少 = 饱和 |
| vs ForkJoinPool | 同构的 work-stealing；Go 偷一半、不用你包装任务、runtime 兜底阻塞 |

---

## 18.8 自检题

1. **`GOMAXPROCS=8` 的机器上，你的服务有 500 个 goroutine 同时在跑阻塞式 `syscall.Read`。此刻最多有几个 goroutine 在同时执行 Go 代码？`threads` 大概多少？**
   > 要点：最多 8 个（P 的数量是硬上限）。每个阻塞 syscall 的 goroutine 会让 M 执行 `entersyscall` → `handoffp(pp)` 把 P 交出去，所以那 500 个 M 都脱离 P 各自阻塞，`threads` 会接近 500+8。这就是"Go 里线程数不等于并行度"的最直接证据；也是要用异步 IO / netpoller 的原因。

2. **`runqput(pp, gp, next=true)` 和 `next=false` 有什么区别？为什么 `runqget` 先看 `runnext`？如果我把 `randomizedsched` 打开会怎样？**
   > 要点：`next=true` 把 gp 放进 `p.runnext`（LIFO 单格，挤掉的老 runnext 降级到 `runq` 尾部，并继承当前时间片）；`next=false` 直接进 `runq` FIFO 尾部。`runqget` 先取 `runnext` 是为了刚 ready 的 g 能立刻跑（缓存热、channel 交接语义）。`GODEBUG=randomizedsched` 会让 `runqput`/`runqsteal` 以 50% 概率放弃 LIFO，牺牲局部性换公平性，专门用来暴露"依赖顺序"的 bug。

3. **`schedule()` 里为什么不能持有锁（第 [1] 段那个 `throw`）？`gopark` 又是怎么绕过这一条的？**
   > 要点：`schedule()` 会通过 `gogo` 切走当前 M 很长时间（可能永远不会回到这条路径），持锁等于让别人永久卡住。所以 `gopark` 的设计是"调用方把锁指针交给我"，在 `mcall(park_m)` 切到 g0 之后由 `park_m` 用 `mp.waitunlockf(mp.curg, mp.waitlock)` 解锁，然后才真正 `casgstatus(_Grunning→_Gwaiting)` + `schedule()`。channel 的 `chanparkcommit`、`semacquire` 的 `semrelease` 都是这个模式 —— 见第 20 章的 `chansend` 阻塞分支。

---

## 18.9 延伸阅读

- `$GOROOT/src/runtime/runtime2.go`：`type schedt`、`type p`、`type m`、`guintptr`/`muintptr`/`puintptr`
- `$GOROOT/src/runtime/proc.go`：`schedule`、`findRunnable`、`runqput`、`runqputslow`、`runqget`、`runqgrab`、`runqsteal`、`globrunqget`、`execute`、`startm`、`wakep`、`stopm`、`handoffp`、`stealWork`
- 同一个文件里那段**长达 80 行的注释**：搜 `"Worker thread parking/unparking"` —— Go 调度器设计思路的原始文档
- `$GOROOT/src/runtime/trace.go`：trace 事件的产生处，对照 `go tool trace` 视图看
- 演讲：Dmitry Vyukov, *Scalable Go Scheduler Design Doc*（`golang.org/s/go11sched`）
- 上一章：[17. goroutine 源码与栈](17-goroutine-internals.md) ｜ 下一章：[19. 调度时机全景](19-scheduler-runtime.md)

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 17 章 goroutine 源码与栈](17-goroutine-internals.md)
- 下一章：[第 19 章 调度时机全景：抢占、syscall、netpoll、sysmon 与 timer](19-scheduler-runtime.md)
- 相关：[第 15 章 分配器（mcache）](15-allocator.md)、[第 20 章 channel](20-channel-internals.md)、[第 29 章 pprof 与 trace](29-pprof-trace.md)、[第 32 章 高并发实战](32-high-concurrency.md)
