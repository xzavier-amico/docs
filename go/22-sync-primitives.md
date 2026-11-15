# 22. sync 原语实现

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 并发与调度器源码 ｜ **编号**：22 / 54
> **本章定位**：`sync` 包是 Go 的"脏活区"。它不负责通信，只负责**互斥与复用**：一个 `Mutex` 只有 8 字节，却在 1ms 处藏着一个公平性开关；一个 `Pool` 有三层缓存，其中一层专门用来"陪葬"。
> **读法**：21.3 讲 select 的"多路等"，这一章讲"排队进"。先看 22.1 的性能现场，再按 22.3 的顺序读（Mutex → 信号量 → RWMutex/WaitGroup/Once → Pool → Map → atomic），最后 22.4/22.6 是 Java 对照和 CR 清单。

---

## 22.1 引子：40 核机器上，锁把 p99 顶高了 40 倍

一个 Agent 服务里有个"租户预算账本"：每个请求开始前 `Take(tenantID, tokens)`，结束时 `Refund(...)`。第一版是标准 Java 直觉的直译：

```go
type Ledger struct {
	mu       sync.RWMutex
	balances map[string]int64
}

func (l *Ledger) Take(id string, n int64) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.balances[id] < n {
		return false
	}
	l.balances[id] -= n
	return true
}

func (l *Ledger) Peek(id string) int64 {
	l.mu.RLock()
	defer l.mu.RUnlock()
	return l.balances[id]
}
```

压测结果：8 核时 p99 是 2ms，40 核时 p99 变成 80ms，而 CPU 只用了一半。`mutex profile` 打开一看：`sync.(*RWMutex).Lock` 占了 34% 的阻塞时间。

三个真凶：

1. **钱是整数**。这种"计数 + 阈值判断"，用 `atomic.Int64` 的 CAS 循环就够了，根本不需要锁——锁只该保护"多个字段必须一起变"的不变量。
2. **`RWMutex` 在写多读少的场景下比 `Mutex` 更慢**。它内部用"负的读者计数"实现写者优先，写者一到，后续 `RLock` 全部排队，锁竞争全压在一个 `atomic.Int32` 上。
3. **临界区里有 `defer`**。`defer Unlock()` 不是罪，但它挡住了 `Unlock` 的快路径内联，还把临界区拉长了两个函数调用。

这一章就把这些"为什么"从 runtime 里挖出来。

---

## 22.2 直觉模型：8 字节的锁怎么装下"锁 + 唤醒 + 饥饿"

Java 的 `synchronized` 有对象头（mark word）、偏向锁、轻量级锁、重量级锁一整套升降级；`ReentrantLock` 有 `Sync`/`NonfairSync`/AQS 双向队列。Go 的选择是：**一个 int32 的位图 + 一个 uint32 的信号量计数**。

```
sync.Mutex 内存布局（Go 1.24+ 实现位于 internal/sync/mutex.go）
┌──────────────────────────────────────────────┐
│ state int32                                  │
│  bit0 mutexLocked     : 已加锁                │
│  bit1 mutexWoken      : 已经有人被唤醒        │
│  bit2 mutexStarving   : 饥饿模式（关键开关）  │
│  bit3..31             : 等待者数量            │
├──────────────────────────────────────────────┤
│ sema uint32           : 交给 runtime 的信号量 │
└──────────────────────────────────────────────┘
总共 8 字节，可以塞进一个 cache line，可以被 CAS 原子改
```

`Mutex` 有两种模式，切换阈值是 **1 毫秒**：

```
       正常模式（normal）                        饥饿模式（starving）
  ┌──────────────────────────┐            ┌──────────────────────────┐
  │ 新来的 goroutine 有优势：│            │ 锁的所有权直接"交接"：   │
  │ 正在 CPU 上跑，抢到就赢  │  ──1ms──►  │ Unlock 不放开锁，直接    │
  │ 被唤醒的 waiter 常输     │            │ Semrelease(handoff=true) │
  │ → 吞吐好，尾延迟差       │  ◄──isLast │ 新来的 goroutine 不许抢  │
  │                          │   或 <1ms  │ 也不许自旋，老实排队     │
  └──────────────────────────┘            └──────────────────────────┘
```

> 💡 记住这个取舍：**正常模式优化吞吐，饥饿模式保命尾延迟**。源码注释写得很直白：正常模式下 goroutine 可能连续多次拿到锁即使有人在等；饥饿模式"important to prevent pathological cases of tail latency"。这是 Go 和 Java `ReentrantLock(fair=false)` 最大的区别——Java 非公平锁没有这种自动升级机制，理论上可以无限饿死（虽然 AQS 的实现让它很难发生）。

---

## 22.3 底层实现

### 22.3.1 Mutex：自旋、LIFO、1ms

```go
// src/internal/sync/mutex.go（Go 1.24 起；Go 1.23 及更早在 src/sync/mutex.go）
type Mutex struct {
	state int32
	sema  uint32
}

const (
	mutexLocked = 1 << iota // 已加锁
	mutexWoken              // 已唤醒一个等待者
	mutexStarving           // 饥饿模式
	mutexWaiterShift = iota // == 3，等待者计数从这个 bit 开始

	starvationThresholdNs = 1e6 // 1ms
)

func (m *Mutex) Lock() {
	if atomic.CompareAndSwapInt32(&m.state, 0, mutexLocked) { // 快路径：一条 CAS
		if race.Enabled { race.Acquire(unsafe.Pointer(m)) }
		return
	}
	m.lockSlow() // 慢路径被"外联"，目的是让 Lock 本身可以内联
}
```

> 📌 **版本事实**：Go 1.23 引入了 `internal/sync` 包让 runtime 也能复用这些原语；1.24 起 `sync.Mutex` 变成一层薄壳——`type Mutex struct { _ noCopy; mu isync.Mutex }`，真正干活的是 `internal/sync/mutex.go`。语义完全一致，读源码时别在 `src/sync/mutex.go` 里找 `state` 字段（`WaitGroup`/`Once` 仍在 `src/sync/`）。

`Lock()` 的快路径只有一条 CAS —— 这是 Go 锁在无竞争时接近零成本的原因（约 15~20ns，一次 cache line 事务）。有竞争才进 `lockSlow()`：

```go
func (m *Mutex) lockSlow() {
	var waitStartTime int64
	starving, awoke, iter := false, false, 0
	old := m.state
	for {
		// ① 自旋：只在"正常模式 + 已加锁"时才值得转圈
		if old&(mutexLocked|mutexStarving) == mutexLocked && runtime_canSpin(iter) {
			if !awoke && old&mutexWoken == 0 && old>>mutexWaiterShift != 0 &&
				atomic.CompareAndSwapInt32(&m.state, old, old|mutexWoken) {
				awoke = true // 我已经把自己标记为"被唤醒者"，Unlock 别再叫别人
			}
			runtime_doSpin() // PAUSE 指令，不放弃 P
			iter++
			old = m.state
			continue
		}
		new := old
		if old&mutexStarving == 0 { new |= mutexLocked } // 饥饿模式下不许抢
		if old&(mutexLocked|mutexStarving) != 0 { new += 1 << mutexWaiterShift }
		if starving && old&mutexLocked != 0 { new |= mutexStarving }
		if awoke { new &^= mutexWoken }
		if atomic.CompareAndSwapInt32(&m.state, old, new) {
			if old&(mutexLocked|mutexStarving) == 0 { break } // CAS 抢到了
			// ② 没抢到 → 睡进信号量。已经等过一次的排到队头（LIFO），
			//    这样"被唤醒又失败"的 goroutine 不会反复排到队尾
			queueLifo := waitStartTime != 0
			if waitStartTime == 0 { waitStartTime = runtime_nanotime() }
			runtime_SemacquireMutex(&m.sema, queueLifo, 2)
			// ③ 醒来：等了超过 1ms 就切饥饿模式
			starving = starving || runtime_nanotime()-waitStartTime > starvationThresholdNs
			old = m.state
			if old&mutexStarving != 0 {
				// 饥饿模式下 Unlock 把所有权"隔空"给我了：
				// mutexLocked 没置位，我仍被算作 waiter，要自己补上
				delta := int32(mutexLocked - 1<<mutexWaiterShift)
				if !starving || old>>mutexWaiterShift == 1 {
					delta -= mutexStarving // 我是最后一个 / 等得不久 → 退回正常模式
				}
				atomic.AddInt32(&m.state, delta)
				break
			}
			awoke = true
			iter = 0 // 被唤醒后重新抢到 → 下一轮可以再允许自旋
		} else {
			old = m.state
		}
	}
}
```

`runtime_canSpin` 的条件很苛刻（`src/runtime/proc.go`）：**GOMAXPROCS > 1、本 P 的本地 runq 为空、自旋次数 < 4**。为什么这么保守？因为自旋是在烧 CPU，如果 P 上还有别的 goroutine 在等，转圈就是纯粹的内耗。

`Unlock` 的快路径也是一条 `atomic.AddInt32(&m.state, -mutexLocked)`，只有在"还有等待者"时才进 `unlockSlow`，那里是饥饿模式的核心：

```go
	} else {
		// 饥饿模式：不放开锁，直接把所有权交接给队头 waiter，
		// 并且让出时间片，让 waiter 能立刻跑起来
		runtime_Semrelease(&m.sema, true, 2) // handoff = true
	}
```

注意这一行：`handoff=true` 意味着**解锁者主动"让位"**（`goyield`），这在正常模式下是不做的。

### 22.3.2 信号量：`sema.go` 里的那棵树

`runtime_SemacquireMutex` 是 `//go:linkname` 到 `runtime.sync_runtime_SemacquireMutex` 的壳，实现在 `src/runtime/sema.go`：

```go
// src/runtime/sema.go
func sync_runtime_Semacquire(addr *uint32) { semacquire1(addr, false, semaBlockProfile, 0) }

func semacquire1(addr *uint32, lifo bool, profile semaProfileFlags, skipframes int) {
	gp := getg()
	s := semroot(addr)     // 按地址哈希到一个 semaRoot
	s.lock()
	for {
		if cansemacquire(addr) { s.unlock(); return } // 快路径：直接 CAS 拿到
		// 慢路径：把 sudog 插进 s.treap（treap 按等待时长排序，
		// 这样"等最久的先被叫"可以近似 FIFO），然后 park
		root := s.waiters
		sudog := acquireSudog()
		sudog.g = gp
		...
		insert(&root, sudog)   // 每个 semaRoot 是一棵 treap
		s.unlock()
		gopark(nil, nil, waitReasonSemacquire, traceBlockSemacquire, skipframes)
		if gp.param == nil { return } // 被 semrelease 唤醒
		...
	}
}

func sync_runtime_Semrelease(addr *uint32, handoff bool, skipframes int) {
	semrelease1(addr, handoff, skipframes)
}
```

三点值得记住：

- **`semaRoot` 是一棵 treap（树堆）**，不是链表。按地址哈希，所有同一个锁的等待者进同一棵树；`lifo=true` 时插到最前，这就是 Mutex 的"重试者插队"。
- **`Mutex` 没有"等待队列对象"**，队列是 runtime 里的 `sudog`，跟 channel 共用同一套基础设施。所以 `sync.Mutex` 的等待是**真正的 goroutine 挂起**，不占线程、不占 P——这就是"Go 里 10000 个 goroutine 抢一把锁不会把机器撑爆"的原因。
- **`handoff=true` 会 `goyield`**：直接把 P 让给刚被唤醒的 goroutine，避免"唤醒者还在继续跑、被唤醒者排不上队"的延迟。

### 22.3.3 RWMutex：用"负数读者"实现写者优先

```go
// src/internal/sync/rwmutex.go
type RWMutex struct {
	w           Mutex        // 有写者在等时，用这把锁把后来的读者挡在门外
	writerSem   uint32       // 写者等读者退场
	readerSem   uint32       // 读者等写者退场
	readerCount atomic.Int32 // 读者数；为负 = 有写者在等
	readerWait  atomic.Int32 // 写者等待中的、尚未退场的读者数
}

const rwmutexMaxReaders = 1 << 30
```

`readerCount` 用**符号位**当开关，是全章最巧的一处：

```
无写者：  readerCount = 3          → 3 个读锁持有者，新的 RLock 直接 +1 放行
有写者：  readerCount = 3 - 2^30   → 负数！新的 RLock 看到负数就去 readerSem 排队
写者拿到：readerCount = -2^30      → 读通道彻底关死
写者释放：+2^30 恢复成正数         → 打开 readerSem 放行后人
```

于是：

- `RLock` 快路径 = 一次 `Add(1)` + 判断符号，无竞争时几乎免费；`RUnlock` = 一次 `Add(-1)`。
- `Lock` 先抢 `w`（保证写者之间互斥且只有一个写者能"关门"），再 `Add(-rwmutexMaxReaders)` 关门，然后等 `readerWait` 归零。
- **写者优先**：只要有一个写者在等，后到的 `RLock` 全部阻塞。这跟 Java `ReentrantReadWriteLock` 默认的**非公平**模式不同（Java 非公平模式下读锁可以"插队"，容易把写者饿死）。
- **不支持锁升级**：持有 `Lock()` 时调 `RLock()` 会死锁（`readerCount` 是负数，`RLock` 会去 `readerSem` 排队，而 `readerSem` 要等写者释放）——Java 的 `ReentrantReadWriteLock` 同样不支持升级，这点倒是同构。

### 22.3.4 WaitGroup / Once：快路径的艺术

```go
// src/sync/waitgroup.go（Go 1.25）
type WaitGroup struct {
	noCopy noCopy
	// bits[0:32] counter, bits[32] synctest bubble flag, bits[33:64] wait count
	state atomic.Uint64
	sema  uint32
}

// Go 1.25 新增：省掉 Add + defer Done 的三行模板
func (wg *WaitGroup) Go(f func()) {
	wg.Add(1)
	go func() { defer wg.Done(); f() }()
}
```

- **一个 `atomic.Uint64` 同时装"任务数"和"等待者数"**，所以 `Add`/`Wait` 之间几乎无锁：`Add(+1)` 就是 `state.Add(1<<32)`；`Wait` 用 CAS 把等待者计数 +1 然后 `runtime_Semacquire`。
- 只有 `counter` 归零 + 有等待者时才 `Semrelease`。
- 官方文档明确写了内存语义：「a call to `Done` "synchronizes before" the return of any `Wait` call that it unblocks」——所以 `Wait()` 返回后读共享变量是安全的（详见第 23 章）。

`Once` 更极端，快路径就是一次原子 load：

```go
type Once struct {
	done atomic.Uint32
	m    Mutex
}

func (o *Once) Do(f func()) {
	if o.done.Load() == 0 { // 快路径：无竞争时就是一次 load + 一次分支
		o.doSlow(f)
	}
}
```

`doSlow` 里才上 `m`，执行 f，`defer o.done.Store(1)`。注意 `sync.Once` 里的 "once" 语义包含**失败也不重试**：f panic 了，后续 `Do` 也不会再执行 f（panic 会继续往外传）。

### 22.3.5 sync.Pool：三层缓存，其中一层用来"陪葬"

```go
// src/sync/pool.go
type Pool struct {
	noCopy noCopy
	local     unsafe.Pointer // [P]poolLocal，真正干活的一层
	localSize uintptr
	victim     unsafe.Pointer // 上一轮 GC 前的 local，被"降级"到这里
	victimSize uintptr
	New func() any
}

type poolLocalInternal struct {
	private any       // 只有本 P 能用，无锁、无原子操作
	shared  poolChain // 本 P 头插/头取，其它 P 只能尾取（steal）
}

type poolLocal struct {
	poolLocalInternal
	pad [128 - unsafe.Sizeof(poolLocalInternal{})%128]byte // 防 false sharing
}
```

```
                    Get() 的查找顺序（src/sync/pool.go: getSlow 是后半段）
 ┌──────────────────────────────────────────────────────────────────────┐
 │ 1. 本 P 的 private       ← 零同步开销，最热的一层                    │
 │ 2. 本 P 的 shared 队列头部 popHead()  ← 同 P（同 M）快速路径          │
 │ 3. 其它 P 的 shared 尾部 popTail()    ← work-stealing，会争 atomic    │
 │ 4. 本 P 的 victim.private / victim.shared  ← 上一轮 GC 的"遗物"       │
 │ 5. 其它 P 的 victim.shared.popTail()                                  │
 │ 6. New()                                                              │
 └──────────────────────────────────────────────────────────────────────┘

 GC 时的 poolCleanup（STW 期间执行，不许分配内存）：
    oldPools.victim = nil            ← 上一轮的 victim 彻底丢掉
    allPools.victim = allPools.local ← 本轮 local 降级成 victim
    allPools.local  = nil
  ⇒ 一个对象最多活过 2 次 GC，第 3 次 GC 时必然被清掉
```

`victim`（受害者）这一层的设计意图是**削峰**：GC 刚结束时，被清空的 `local` 会让所有 goroutine 瞬间同时 `New()`，形成一次分配尖峰。留一层 victim，等于给"刚刚还在用的对象"一次复活机会——代价是**内存峰值翻倍**。

> ⚠️ **`sync.Pool` 的三条铁律**（每条都有人踩）：
> 1. **不是缓存**。任何对象随时可能被 GC 清掉，绝对不能拿来存"必须命中"的东西（连接池、buffer 复用可以，业务缓存不行）。
> 2. **不要假设 `Put` 了就能 `Get` 到**。`Get` 可能被随机丢弃（`-race` 下 `Put` 甚至按 1/4 概率直接丢，见源码 `runtime_randn(4) == 0` 那段）。
> 3. **`Pool` 不能被拷贝**（含 `noCopy`），放进 struct 要指针对待。

```go
// 一个能直接 go run 的实验：看 victim 层如何让对象多活一次 GC
package main

import (
	"fmt"
	"runtime"
	"sync"
)

type buf struct{ id int }

func main() {
	var p sync.Pool
	p.New = func() any { return &buf{id: -1} }

	b := p.Get().(*buf)
	b.id = 42
	p.Put(b) // 进本 P 的 private 或 shared

	runtime.GC() // GC #1：local → victim，对象还活着
	got := p.Get().(*buf)
	fmt.Printf("after GC#1: id=%d（命中 victim）\n", got.id)

	p.Put(got)
	runtime.GC() // GC #2：victim → 丢弃
	runtime.GC() // GC #3：确认清空（保守起见多来一次）
	got2 := p.Get().(*buf)
	fmt.Printf("after GC#2/3: id=%d（-1 表示走了 New，旧对象已被回收）\n", got2.id)
}
```

跑一遍你会看到 `42` 然后 `-1`。这就是"`sync.Pool` 是 GC 的减压阀，不是缓存"的可执行证明。

### 22.3.6 sync.Map：read / dirty / amended

```go
// src/sync/map.go
type Map struct {
	mu     Mutex
	read   atomic.Pointer[readOnly] // 只读快照，读路径无锁
	dirty  map[any]*entry           // 全量 map，写路径在锁内
	misses int                      // read 未命中次数，够了就提升 dirty
}

type readOnly struct {
	m       map[any]*entry
	amended bool // dirty 里有 read 没有的 key
}
```

```
读：loadReadOnly() → e, ok := read.m[key]
      ├─ ok              → e.load() 直接返回（全程无锁、无原子 RMW）
      ├─ !ok && !amended → 直接返回 nil（dirty 里也不可能有）
      └─ !ok && amended  → 加锁 → 查 dirty；同时 misses++

写/删：命中 read 里的 entry 时，用 entry.p 的 CAS 改值（锁都不用）
        未命中 → 加锁 → 写 dirty；如果 read 里没有这个 key，amended = true

提升：misses 累计到 >= len(dirty) → 加锁，read = readOnly{m: dirty}, dirty = nil, amended=false
      提升时会把"已被删除"的 entry（p == nil 的墓碑）直接过滤掉
```

`sync.Map` 的定位非常窄：**key 集合基本稳定（写一次读多次）、或者每个 goroutine 只碰自己的 key（不相交）**。这两个场景下它比 `RWMutex + map` 快；其它场景（频繁增删、需要 `len`、需要遍历一致性快照）应该老老实实 `Mutex + map`。它连 `Len()` 都没有（只能遍历数），这就是"不是通用 Map"的自白。

### 22.3.7 typed atomic（1.19）与弱内存序

Go 1.19 给 `sync/atomic` 加了一批类型（此前只有函数形式，`atomic.AddInt64(&x, 1)`）：

```go
var (
	hits   atomic.Int64
	online atomic.Bool
	cfg    atomic.Pointer[Config] // 泛型指针原子
)

hits.Add(1)
if cfg.Load() == nil {
	cfg.Store(defaultConfig)
}
```

好处不只是好看：**类型化之后不可能再出现"变量没对齐导致 32 位平台上 atomic 报错"**（老代码里 `int64` 字段放在 struct 第一个字段才安全，这是 Java 程序员完全不会想到的坑）；而且 `atomic.Pointer[T]` 顺带带来类型安全——不用再 `unsafe.Pointer` 强转。

内存序方面记住一句就够：**Go 的原子操作是顺序一致（seq-cst）的，没有 relaxed/acquire/release 的公开 API**。Go 内存模型规范原话：「All the atomic operations executed in a program behave as though executed in some sequentially consistent order. The preceding definition has the same semantics as C++'s sequentially consistent atomics and **Java's `volatile` variables**.」这是全章跟 Java 对照最硬的一条（详见第 23 章）。

---

## 22.4 Java 对照：只讲会猜错的地方

| 场景 | Java | Go | 会猜错的地方 |
| --- | --- | --- | --- |
| 互斥锁 | `synchronized` / `ReentrantLock` | `sync.Mutex` | **不可重入**。同一个 goroutine 二次 `Lock` 直接死锁，Java 里这是合法的 |
| 公平性 | `ReentrantLock(true)` 严格 FIFO；非公平靠 AQS 队列 | 默认"随机抢"（正常模式）+ **1ms 后自动转饥饿模式** | Go 没有"公平锁"选项，它用性能换了一个自动防饿死的开关 |
| 读写锁 | `ReentrantReadWriteLock` | `sync.RWMutex` | Go 的更简单：**写者优先**、不可重入、不可升级；没有 Java 那些 `isWriteLocked` 之类的诊断 API |
| 等待一组任务 | `CountDownLatch` / `Phaser` | `sync.WaitGroup` | Java 的 `CountDownLatch` 一次性、`Phaser` 可复用；Go 的 `WaitGroup` 复用有严格限制（`Add` 不能与 `Wait` 并发） |
| 只执行一次 | 静态内部类 Holder / `AtomicReference` CAS | `sync.Once` | Go 的 `Once` 是**失败不重试**的；而且它是"初始化 + 建立 happens-before"的标准答案 |
| 原子类 | `AtomicLong` / `LongAdder` | `atomic.Int64`（1.19） | Go 没有 `LongAdder` 那种分段累加器，高竞争计数器要自己分片（第 30 章） |
| 对象池 | `ThreadLocal` + 对象池 / `commons-pool` | `sync.Pool` | **per-P 不是 per-Thread**；对象随时被 GC 清掉；不能当缓存 |
| 并发 Map | `ConcurrentHashMap` | `sync.Map` | Java 的 CHM 是**通用**哈希表（分段/桶级锁）；Go 的 `sync.Map` 只适合两种偏门场景，没有 `size()` |
| 条件变量 | `Condition.await/signal` | `sync.Cond` | Go 的 `sync.Cond` 确实存在，但社区用得极少——绝大多数场景 channel 更好读（第 20 章） |

> 🧠 **Go 为什么不做可重入锁**：可重入锁鼓励"在一个大临界区里调用别人"的代码结构，这种结构一旦跨包就必然埋下锁序 bug。Go 选择让二次 Lock **立刻死锁**，用最响的方式逼你缩小临界区。这个决定跟"没有继承"是同一种审美：**让错误尽早、尽量响地暴露**。

---

## 22.5 框架与工程落地

- **`fasthttp`**：`RequestCtx`、`argsKV` 缓冲全靠 `sync.Pool` 复用，这是它号称"零分配"的核心。它甚至为了绕开 `Pool` 的 GC 清空，自己维护了按 server 的 `sync.Pool` + `bytebufferpool` 两层结构。
- **`panjf2000/ants`**：goroutine 池。核心是一个 `workerArray` + `Mutex` + `Cond`（是的，它用了 `sync.Cond`），`retrieveWorker` 从栈里取空闲 worker，取不到再看容量决定"新建还是阻塞"。**注意**：Go 的 goroutine 很便宜，绝大多数 Agent 服务不需要 ants；需要 ants 的典型场景是"要限制并发到个位数 + 复用大 buffer"。
- **`encoding/json`**：`encodeStatePool sync.Pool` 缓存编码器状态；`fmt` 的 `ppFree` 缓存 `pp` 结构体 —— 标准库自己就是 `sync.Pool` 的重度用户。
- **`net/http`**：`Transport` 的连接池不用 `sync.Pool`，用的是 `sync.Mutex` + `wantConn` channel 结构（因为需要"有界"和"可取消等待"）；`http.Server` 的 `conns` 是 `map[net.Conn]struct{}` + `RWMutex`。
- **`option`/配置热更新**：`atomic.Pointer[Config]` 是 Go 里"读多写少热更新"的标准答案——写者 `Store` 一个新不可变 struct，读者 `Load` 一次拿快照，全程无锁。

```go
// 热更新配置：无锁读 + 写者单点（比 RWMutex 快一个量级）
type Config struct {
	MaxTokens int
	Model     string
}

type Registry struct{ cur atomic.Pointer[Config] }

func (r *Registry) Get() *Config { return r.cur.Load() }

func (r *Registry) Update(c *Config) { r.cur.Store(c) } // 只由单 goroutine 调用
```

---

## 22.6 踩坑清单（CR 可直接抄）

| # | 现象 | 原因 | 正确写法 |
| --- | --- | --- | --- |
| 1 | 程序挂住，pprof 显示同一函数两层 `sync.Mutex.Lock` | **`sync.Mutex` 不可重入** | 拆成 `lock()` 内部版本 + `Lock()` 外部包装 |
| 2 | `suspect: sync: unlock of unlocked mutex` fatal | `Unlock` 次数多于 `Lock`，或锁被拷贝 | 用 `defer` 配对；struct 里含 `Mutex` 一律用指针传 |
| 3 | `go vet` 报 "passes lock by value" | 拷贝了含 `Mutex` 的 struct | 加 `noCopy` 或改用 `*T` |
| 4 | `sync.Pool` 里的对象"莫名其妙"消失 | 正常行为：GC 会清 | 只存可重建的临时对象；必须命中就用 `RWMutex + map` |
| 5 | 把 `sync.Pool` 当全局缓存用，命中率极低 | per-P + 随机丢弃 + victim 清空 | 缓存请用 LRU 库（如 `hashicorp/golang-lru/v2`） |
| 6 | 高竞争 `atomic.Int64` 计数器成为瓶颈 | 单个 cache line 上的 CAS 风暴 | 分片（`[64]atomic.Int64` 按 P/goroutine 分），最后汇总 |
| 7 | `RWMutex` 比 `Mutex` 还慢 | 写多读少；写者优先让读锁排队 | 写多就直接 `Mutex`；读多且 key 稳定考虑 `sync.Map` |
| 8 | `WaitGroup` 复用报 `WaitGroup is reused before previous Wait has returned` | `Add` 与 `Wait` 并发 | 每次任务组用新的 `WaitGroup`，或用 errgroup（第 26 章） |
| 9 | 锁竞争看不出来 | 没开 mutex profile | `runtime.SetMutexProfileFraction(1)` + `pprof -http=:8080 http://host/debug/pprof/mutex`（第 29 章） |
| 10 | 32 位平台/ARM 上 `atomic` 报错 | 老式 `atomic.AddInt64(&x,…)` 的字段未 8 字节对齐 | 用 1.19 的 `atomic.Int64` 类型，别自己猜对齐 |

---

## 22.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| `Mutex` 结构 | `state int32` 位图（locked/woken/starving/waiter 计数）+ `sema uint32`，共 8 字节 |
| 快路径 | 一条 CAS；慢路径进 `lockSlow`，最多自旋 4 次（且要 P 上没活干） |
| 饥饿模式 | 等超 1ms 就切换：所有权直接 handoff + `goyield`，代价是吞吐下降 |
| 信号量 | `runtime/sema.go` 的 `semacquire1` + treap 等待树，等待者是 `sudog` 挂起，不占线程 |
| `RWMutex` | `readerCount` 用负数表示"写者在等"，实现写者优先；不可升级、不可重入 |
| `WaitGroup` | 一个 `atomic.Uint64` 装 counter + waiter；`Done` happens-before `Wait` 返回 |
| `Once` | 快路径一次 `atomic.Load`；失败不重试；自带 happens-before |
| `sync.Pool` | per-P `private` + `shared` + `victim`；每次 GC 降级，两轮后清空；**不是缓存** |
| `sync.Map` | read（无锁）+ dirty（锁内）+ misses 提升；只适合读多写少/不相交 key |
| `atomic` | 1.19 起有 `Int64`/`Bool`/`Pointer[T]` 等类型；**顺序一致**，语义等同 Java `volatile` |

---

## 22.8 自检题

1. **`sync.Mutex` 为什么在 1ms 处切"饥饿模式"？不切会怎样？切了有什么代价？**
   > 要点：正常模式下被唤醒的 waiter 要和"正在 CPU 上跑的新 goroutine"竞争，可能反复失败（源码注释：woken up waiter has good chances of losing），极端情况尾延迟无限拉长。等超过 `starvationThresholdNs`（1e6 ns = 1ms）就把锁切成 starvation，之后 `Unlock` 直接用 `Semrelease(handoff=true)` 交接所有权 + 让出时间片，且新来者不许抢、不许自旋。代价是吞吐下降、上下文切换变多，所以一旦"我是队里最后一个或被等时间 < 1ms"就立刻退回正常模式。

2. **你的服务里有个 `map[string]*Session`，读远多于写，key 集合基本固定。选 `Mutex+map`、`RWMutex+map` 还是 `sync.Map`？为什么 sync.Map 不能当通用 Map 用？**
   > 要点：key 集合稳定 + 读多写少正是 `sync.Map` 的目标场景（read 快照无锁命中）。但它没有 `Len()`、遍历不保证一致快照、频繁插入新 key 会让 `misses` 不断触发 dirty 提升（提升要加锁 + 复制），删除还留有墓碑；所以频繁增删或需要一致性遍历时应该用 `RWMutex + map` 或加锁的 `map`。

3. **为什么说 `sync.Pool` 是"GC 的减压阀"而不是缓存？对象在里面最多活多久？**
   > 要点：`Get` 可能走 `New`（因为 GC 的 `poolCleanup` 会清 `victim` 并把 `local` 降级为 `victim`），`-race` 下 `Put` 甚至按 1/4 概率直接丢弃。对象最多活过两次 GC：第一次 GC 从 `local` 降到 `victim`，第二次 GC `victim` 被置 nil。所以只能存"可重建、丢了不亏"的临时对象（buffer、编码器状态），不能存业务缓存或连接。

---

## 22.9 延伸阅读

- `$GOROOT/src/internal/sync/mutex.go`、`rwmutex.go`、`waitgroup.go`、`once.go`（Go 1.24 起；1.23 及更早在 `$GOROOT/src/sync/`）—— 尤其 `Mutex fairness` 那段注释，本章 22.2 的图就是它的翻译
- `$GOROOT/src/runtime/sema.go` —— `semacquire1` / `semrelease1` / `semaRoot`（treap）
- `$GOROOT/src/sync/pool.go` —— `getSlow` 的 6 级查找顺序与 `poolCleanup`
- `$GOROOT/src/sync/map.go` —— `readOnly` / `amended` / `dirtyLocked`
- `$GOROOT/src/sync/atomic/doc.go` —— 内存序承诺与 1.19 新增类型的说明
- Dmitry Vyukov 的 *Scalable Go Scheduler Design* 与 `sync.Mutex` 饥饿模式的 issue（golang/go#13086）—— 1ms 这个数字的来历

## 22.10 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 21 章 select 与超时控制](21-select-timeout.md)、[第 17 章 goroutine 源码](17-goroutine-internals.md)（`sudog` 与 park/unpark）
- 下一章：[第 23 章 Go 内存模型与 happens-before](23-memory-model.md)
- 相关：[第 20 章 channel 原理](20-channel-internals.md)、[第 29 章 pprof 与 trace](29-pprof-trace.md)（mutex/block profile）、[第 30 章 性能优化](30-perf-tuning.md)（分片与无锁读）
