# 20. channel 原理与编排模式

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 并发与调度器源码 ｜ **编号**：20 / 54
> **本章定位**：前面三章讲的都是「调度器怎么搬运 goroutine」，这一章讲**它们之间怎么传数据**。`channel` 是 Go 并发模型的门面，但它的实现远比「一个队列 + 两个条件变量」精妙：**直接移交（direct handoff）**、**栈间直接写内存**、**hchan 与 buf 一次分配**，这几个细节直接决定了你在 Agent 里怎么写编排代码。
> **读法**：先把 20.3 的五个源码块读透（`hchan` → `chansend` → `recv`/`send` → `closechan`），再跳到 20.5 看模式。源码为删减版，**以本地 `$GOROOT/src/runtime/chan.go` 为准**。

---

## 20.1 引子：三个工具并发跑，程序却再也不返回

一个 ReAct Agent 的"并行工具执行"看起来写得很正常：

```go
func runTools(ctx context.Context, tools []Tool, args Args) []Result {
	results := make(chan Result) // ← 罪魁祸首：无缓冲
	for _, t := range tools {
		go func(t Tool) {
			r, _ := t.Call(ctx, args)
			results <- r // 三个工具都在这里发
		}(t)
	}

	var out []Result
	for i := 0; i < len(tools); i++ {
		out = append(out, <-results)
	}
	return out
}
```

压测时它没问题，上线后开始出现：**请求超时了，但 goroutine 数不降，24 小时后 `go_goroutines` 从 400 涨到 9000。**

原因链条是这样的：某个工具 `t.Call` 卡住（下游限流）→ 另外两个工具早跑完了，它们的 `results <- r` **成功发出**（主 goroutine 收到了）→ 主 goroutine 继续收第三个 → 第三个工具最终返回，但此时 **ctx 已经超时，上层 handler 早就 return 了**，没人再 `<-results` → 那个 `results <- r` 永久阻塞在 `_Gwaiting / chan send`。

```bash
$ curl -s 'localhost:6060/debug/pprof/goroutine?debug=1' | grep -A2 'chan send' | head
3 @ 0x43e5f6 0x44a1c5 0x4a2b3d 0x4dbe21
#	0x4a2b3c	main.runTools.func1+0x5c	/app/agent.go:31
```

`main.runTools.func1` 第 31 行 —— 就是那句 `results <- r`。

> 🩸 **这不是"channel 用得不对"，是"channel 的语义没想清"**：channel 的发送是**同步契约**（无缓冲时：发送方必须等接收方伸手接），一旦接收方提前离场，发送方就成了孤儿，且 runtime 不会帮你回收它 —— `g` 是活对象，GC 看得见但管不了。
>
> 修法有三个层次，本章会全部覆盖：① 给 channel 加缓冲（缓解）；② 用 `select + ctx.Done()`（正确）；③ 理解 `hchan` 里 `sendq`/`recvq` 的存在，从根上说清为什么"孤儿发送"是个 `_Gwaiting` 状态（彻底）。

要把这三层讲清，得先看 `hchan` 长什么样。

---

## 20.2 直觉模型：channel = 一把锁 + 一个环 + 两个等待队列

Java 程序员的第一反应是"这不就是 `ArrayBlockingQueue` 吗"。结构上确实像，但有一处**本质差异**必须先建立直觉：

```
     Java:  producer ──put()──▶ [ 队列 ] ──take()──▶ consumer
                                  ▲
                          两条路都要经过队列

     Go:    producer ─────────────────────────────▶ consumer
                    （有人等着时，直接交接，队列完全不参与！）
             producer ──▶ [ 环形缓冲 ] ──▶ consumer
                    （无人等着时，才走缓冲）
```

**"有人接就直接给，没人接才进队列"** 就是 direct handoff。它带来两个后果：① 少一次内存拷贝（其实还是有，但少了入队/出队两次）；② **唤醒顺序与队列无关**，先到的接收方先拿 —— 这让 channel 的语义是"排队等"而不是"投递到邮箱"。

`hchan` 的内存布局（`make(chan int, 4)`）：

```
                    hchan（堆上一个对象）
   ┌──────────────────────────────────────────┐
   │ qcount   = 2      当前元素个数             │
   │ dataqsiz = 4      缓冲容量                │
   │ buf      ────────────────────┐           │
   │ elemsize = 8                  │           │
   │ closed   = 0                  │           │
   │ elemtype ──▶ *_type(int)      │           │
   │ sendx    = 2      下一个写入位置│           │
   │ recvx    = 0      下一个读取位置│           │
   │ recvq    ──▶ sudog ⇄ sudog    │  ← 等收的 g 链表
   │ sendq    ──▶ sudog ⇄ sudog    │  ← 等发的 g 链表
   │ lock     (mutex)              │           │
   └───────────────────────────────┼───────────┘
                                   ▼
      buf：环形数组（若元素不含指针，与 hchan 同一块分配）
   ┌──────┬──────┬──────┬──────┐
   │  17  │  42  │      │      │
   └──────┴──────┴──────┴──────┘
     ▲recvx        ▲sendx
   （读到 17）    （写到这里）

   qcount=2 → 有效数据 = buf[recvx : recvx+qcount]，超过尾部就绕回 0
```

对照 Java，把概念一一对应上（**注意有一半是错位的**）：

| 概念 | Go channel | Java 对应物 | 错位点 |
| --- | --- | --- | --- |
| 缓冲队列 | `hchan.buf` 环形数组 | `ArrayBlockingQueue` 的 `Object[] items` + `takeIndex`/`putIndex` | 几乎一样（连环形下标都一样） |
| 锁 | `hchan.lock`（runtime 内部 `mutex`） | `ReentrantLock` | Go 的锁**不可重入**且用户摸不到 |
| 等待队列 | `sendq`/`recvq`（`sudog` 双向链表） | `Condition` 的 `notFull`/`notEmpty` | Go 是**两个显式队列**，且"交接"是直接把数据写进对方栈 |
| 关闭语义 | `close(ch)`：接收方拿到零值 + `ok=false`，发送方 panic | 没有对应物（`BlockingQueue` 没有 close） | Java 里"关闭"要靠毒丸（poison pill），Go 是语言内建 |
| 无缓冲 | `make(chan T)`：纯 rendezvous | `SynchronousQueue` | 同构，但 Go 的无缓冲是**默认**用法，Java 是特例 |
| 阻塞唤醒 | `gopark` / `goready`（goroutine 级） | `LockSupport.park/unpark`（线程级） | Go 唤醒是 O(1) 的队列操作，不用抢锁 |
| 超时 | `select` + `time.After`/`Timer` | `offer(e, timeout, unit)` | Go 把超时**外置**成 select 分支（第 21 章） |

## 20.3 底层实现

### 20.3.1 `type hchan struct` 逐字段拆解

`runtime/chan.go`（原文注释保留 + 中文补充）：

```go
type hchan struct {
	qcount   uint           // 当前缓冲里有几个元素
	dataqsiz uint           // 环形缓冲的容量（make 时的第二个参数）
	buf      unsafe.Pointer // 指向 [dataqsiz]T 的数组；无缓冲时为 nil（或指向自身）
	elemsize uint16         // 单个元素大小，用于 memmove
	closed   uint32         // 0=未关闭，1=已关闭
	elemtype *_type         // 元素类型元数据（GC 扫描、写屏障要用）
	sendx    uint           // 下一个写入位置（环形下标）
	recvx    uint           // 下一个读取位置（环形下标）
	recvq    waitq          // 等待接收的 goroutine 链表（sudog）
	sendq    waitq          // 等待发送的 goroutine 链表（sudog）

	// lock 保护 hchan 的所有字段，以及阻塞在此 channel 上的 sudog 的若干字段。
	// ★ 原文注释很重要：
	//   "Do not change another G's status while holding this lock
	//    (in particular, do not ready a G), as this can deadlock
	//    with stack shrinking."
	//   ——持锁期间不能 ready 别的 G，否则可能与栈收缩死锁。
	//   所以 chansend/chanrecv/closechan 都是"先收集到 glist，解锁后统一 goready"。
	lock mutex
}

type waitq struct {
	first *sudog
	last  *sudog
}
```

**六个字段的工程含义**，值得逐条记住：

| 字段 | 类型/宽度 | 谁改 | 为什么重要 |
| --- | --- | --- | --- |
| `qcount` | `uint` | 持锁改 | `full(c)`/`empty(c)` 的依据；`len(ch)` 读的就是它（**无锁读，弱一致**） |
| `dataqsiz` | `uint` | 只在 make 时定 | `cap(ch)`；为 0 表示无缓冲 |
| `buf` | `unsafe.Pointer` | 只在 make 时定 | 指向元素数组；**无缓冲时不是 nil 就是指向 hchan 自身**（race detector 的同步地址） |
| `elemsize` | `uint16` | 只在 make 时定 | 决定 `chanbuf(c, i)` 的地址算法 `buf + i*elemsize`；**上限 64KB**（元素太大不能走 channel？不，是 elemsize 字段 16 位，超过会 separate allocation 处理） |
| `closed` | `uint32` | `closechan` | 非阻塞快路径用 `atomic.Load(&c.closed)` 读它（`chanrecv` 的 fast path） |
| `sendx`/`recvx` | `uint` | 持锁改 | 环形下标，到 `dataqsiz` 就回 0 |
| `recvq`/`sendq` | `waitq` | 持锁改 | **direct handoff 的核心**：阻塞的 g 以 `sudog` 形式挂在这两条链上 |
| `lock` | `mutex` | —— | channel 的全部正确性都靠它；Go 的 channel **不是无锁队列** |

#### `makechan`：hchan 与 buf 是不是一次分配？

```go
// runtime/chan.go（删减）
func makechan(t *chantype, size int) *hchan {
	elem := t.Elem
	// ...（size 合法性、mem 溢出检查略）
	mem, overflow := math.MulUintptr(elem.Size_, uintptr(size))
	...
	var c *hchan
	switch {
	case mem == 0:
		// ① 无缓冲 or 元素大小为 0（chan struct{}）
		//    只分配 hchan 本身
		c = (*hchan)(mallocgc(hchanSize, nil, true))
		c.buf = c.raceaddr() // race detector 需要一个同步地址
	case !elem.Pointers():
		// ② 元素不含指针（int/struct{int}…）：hchan + buf 一次分配！
		//    少一次 malloc、少一个 GC 对象、缓存更友好
		c = (*hchan)(mallocgc(hchanSize+mem, nil, true))
		c.buf = add(unsafe.Pointer(c), hchanSize) // buf 紧跟 hchan 之后
	default:
		// ③ 元素含指针：必须分开分配（GC 需要把 buf 当独立对象扫描）
		c = new(hchan)
		c.buf = mallocgc(mem, elem, true)
	}
	c.elemsize = uint16(elem.Size_)
	c.elemtype = elem
	c.dataqsiz = uint(size)
	return c
}
```

> 💡 `make(chan struct{})` 是**零成本**的：不分配 buf、元素拷贝是 0 字节，只用于"信号"。这是 Go 里最常用的"事件通知"，也是 `context`、`errgroup`、`http.Server` 关停信号的标准做法。相比之下 `make(chan bool)` 每个元素 1 字节 + 对齐——虽然差别不大，但语义上 `struct{}` 更明确："我传的是事件，不是值"。

### 20.3.2 `sudog`：阻塞的 goroutine 在队列里长什么样

`hchan.recvq`/`sendq` 里挂的不是 `*g`，而是 `*sudog`（"pseudo-g"）。为什么需要一层包装？因为：

1. 一个 g 可能**同时**在多个 channel 上等（`select`）→ 需要多个节点；
2. 要记录"数据的地址"（`elem`），让对端可以直接往里写；
3. 要记录这次操作**是否成功**（`success`），close 唤醒时用它判断要不要 panic。

```go
// runtime/runtime2.go（删减）
type sudog struct {
	g *g // 真正的 goroutine

	next *sudog // 队列里的下一个
	prev *sudog // 队列里的上一个（双向链表，便于 O(1) 删除）

	// ★★ elem 指向"数据存放处"。对阻塞的发送方来说，它指向**发送方栈上的变量**；
	//    对阻塞的接收方来说，指向**接收方栈上的变量**。
	//    所以 channel 会出现"一个 goroutine 直接写另一个 goroutine 的栈"这种操作，
	//    这也是 GC 必须特殊处理（写屏障 + 栈不可搬）的原因。
	elem unsafe.Pointer

	acquiretime int64 // 什么时候开始等（竞态检测/统计用）
	releasetime int64 // 什么时候被唤醒
	ticket      uint32 // 用于公平性选择

	isSelect bool // 是否属于 select 语句（涉及 g.selectDone 的 CAS 竞速）
	success  bool // ★ 这次通信成功了吗？close 时被唤醒的是 false

	parent   *sudog // semaRoot 的平衡树用
	waitlink *sudog // g.waiting 链表（一个 g 可能挂多个 sudog）
	waittail *sudog
	c        *hchan // 在哪个 channel 上等
}
```

> ⚠️ `sudog` 是**池化的**（`p.sudogcache`，`acquireSudog`/`releaseSudog`），因为高频 channel 操作每秒钟可能创建/销毁上百万个。同样地，`sudog.elem` 指向栈这个事实，正是第 17 章 `g.parkingOnChan`/`g.activeStackChans` 存在的原因：**栈不能在有 sudog 指着它的时候搬家**。

### 20.3.3 `chansend`：一次发送到底经历了什么

```go
// runtime/chan.go（删减，注释是我加的）
func chansend(c *hchan, ep unsafe.Pointer, block bool, callerpc uintptr) bool {
	// ① 往 nil channel 发送：永久阻塞（注意：不是 panic！）
	if c == nil {
		if !block {
			return false // select + default 的路径：直接报告"不可达"
		}
		gopark(nil, nil, waitReasonChanSendNilChan, traceBlockForever, 2)
		throw("unreachable") // 永远醒不来
	}
	// ...（race detector 埋点略）

	// ② 快速路径：非阻塞发送 + 已满 + 未关闭 → 立刻返回 false（不拿锁）
	//    注意先读 closed 再读 full，这个顺序保证"观察到满"时 closed 的读是安全的
	if !block && c.closed == 0 && full(c) {
		return false
	}

	lock(&c.lock) // ★ 从这里开始所有人排队

	// ③ 往已关闭的 channel 发送 → panic（三规则之一）
	if c.closed != 0 {
		unlock(&c.lock)
		panic(plainError("send on closed channel"))
	}

	// ④ ★★ 直接移交：有人正等着收？把数据直接写进它的栈，然后唤醒它
	if sg := c.recvq.dequeue(); sg != nil {
		send(c, sg, ep, func() { unlock(&c.lock) }, 3)
		return true // 注意：缓冲区完全没参与
	}

	// ⑤ 缓冲还有空位？入队
	if c.qcount < c.dataqsiz {
		qp := chanbuf(c, c.sendx)          // buf + sendx*elemsize
		typedmemmove(c.elemtype, qp, ep)   // 元素拷贝（含写屏障）
		c.sendx++
		if c.sendx == c.dataqsiz {
			c.sendx = 0 // 环形回绕
		}
		c.qcount++
		unlock(&c.lock)
		return true
	}

	// ⑥ 非阻塞且满了 → 放弃
	if !block {
		unlock(&c.lock)
		return false
	}

	// ⑦ 阻塞路径：造 sudog、挂到 sendq、park
	gp := getg()
	mysg := acquireSudog()
	mysg.elem = ep // ★ 指向我栈上的变量 ep（危险之处就在这里）
	mysg.g = gp
	mysg.isSelect = false
	mysg.c = c
	gp.waiting = mysg
	gp.param = nil
	c.sendq.enqueue(mysg)
	// ★ 告诉想收缩我栈的人："我正要停在 channel 上，别动我的栈"
	gp.parkingOnChan.Store(true)
	// chanparkcommit 会解锁 c.lock（避免"持锁进调度器"，见第 18 章）
	gopark(chanparkcommit, unsafe.Pointer(&c.lock), waitReasonChanSend, traceBlockChanSend, 2)
	// ★ 保证发送值在接收方拷走之前不被 GC 回收（ep 可能指向栈，sudog 也可能）
	KeepAlive(ep)

	// ⑧ 被唤醒：检查是不是 close 唤醒的（success=false → panic）
	if mysg != gp.waiting {
		throw("G waiting list is corrupted")
	}
	gp.waiting = nil
	gp.activeStackChans = false
	closed := !mysg.success
	gp.param = nil
	...
	releaseSudog(mysg)
	if closed {
		if c.closed == 0 {
			throw("chansend: spurious wakeup")
		}
		panic(plainError("send on closed channel"))
	}
	return true
}
```

流程图（这张图建议手抄）：

```
                       chansend(c, ep, block)
                                │
                    ┌───────────▼───────────┐
                    │ c == nil ?            │──是──▶ gopark 永久阻塞（nil channel）
                    └───────────┬───────────┘
                                │ 否
                    ┌───────────▼───────────┐
                    │ 非阻塞 && 满 && 未关闭 │──是──▶ return false（快路径，无锁）
                    └───────────┬───────────┘
                                │
                          lock(&c.lock)
                                │
                    ┌───────────▼───────────┐
                    │ c.closed != 0 ?       │──是──▶ panic("send on closed channel")
                    └───────────┬───────────┘
                                │
                    ┌───────────▼───────────┐
                    │ recvq 有人等着吗？     │──有──▶ send(): 直接把 ep 拷到对方栈
                    └───────────┬───────────┘        unlock → 返回 true
                                │ 没有
                    ┌───────────▼───────────┐
                    │ qcount < dataqsiz ？   │──有──▶ 拷进 buf[sendx]；sendx++；qcount++
                    └───────────┬───────────┘        unlock → 返回 true
                                │ 满了
                    ┌───────────▼───────────┐
                    │ !block ?              │──是──▶ unlock → return false
                    └───────────┬───────────┘
                                │ 阻塞发送
                    ┌───────────▼────────────────────────────┐
                    │ acquireSudog；sudog.elem = ep           │
                    │ sendq.enqueue(sudog)                    │
                    │ parkingOnChan = true                    │
                    │ gopark(chanparkcommit) → g0 → schedule  │
                    │  ...等待被接收方/closechan 唤醒...        │
                    │ success == false ? → panic(closed)      │
                    └────────────────────────────────────────┘
```

### 20.3.4 `chanrecv`：接收的五个分支

```go
// runtime/chan.go（删减）
func chanrecv(c *hchan, ep unsafe.Pointer, block bool) (selected, received bool) {
	// ① nil channel：永久阻塞
	if c == nil {
		if !block {
			return
		}
		gopark(nil, nil, waitReasonChanReceiveNilChan, traceBlockForever, 2)
		throw("unreachable")
	}

	// ② 非阻塞快路径：先看 empty，再看 closed（顺序不能反，保证语义）
	if !block && empty(c) {
		if atomic.Load(&c.closed) == 0 {
			return // 没数据也没关，直接放弃
		}
		// 已关闭且（重新确认）空 → 返回零值 + received=false
		if empty(c) {
			if ep != nil {
				typedmemclr(c.elemtype, ep) // 把接收变量清零
			}
			return true, false
		}
	}

	lock(&c.lock)

	// ③ 已关闭
	if c.closed != 0 {
		if c.qcount == 0 {
			unlock(&c.lock)
			if ep != nil {
				typedmemclr(c.elemtype, ep) // ★ 从已关闭的空 channel 收：得到零值
			}
			return true, false // received = false → 配合 v, ok := <-ch
		}
		// 还有缓冲数据：继续往下走，先把残留数据读完
	} else {
		// ④ 有人在等着发？
		if sg := c.sendq.dequeue(); sg != nil {
			// 若缓冲为空（无缓冲或无数据）：直接从发送方栈上取值（direct handoff）
			// 若有缓冲：取 buf[recvx]，同时把发送方的值补到 buf 尾部（因为此时队列满）
			recv(c, sg, ep, func() { unlock(&c.lock) }, 3)
			return true, true
		}
	}

	// ⑤ 缓冲区有数据：正常出队
	if c.qcount > 0 {
		qp := chanbuf(c, c.recvx)
		if ep != nil {
			typedmemmove(c.elemtype, ep, qp)
		}
		typedmemclr(c.elemtype, qp) // ★ 清空 slot，防止 GC 把元素当活对象留着
		c.recvx++
		if c.recvx == c.dataqsiz {
			c.recvx = 0
		}
		c.qcount--
		unlock(&c.lock)
		return true, true
	}

	if !block {
		unlock(&c.lock)
		return false, false
	}

	// ⑥ 阻塞接收：挂到 recvq
	gp := getg()
	mysg := acquireSudog()
	mysg.elem = ep // ★ 指向我栈上的接收变量，发送方会直接往里写
	mysg.g = gp
	mysg.isSelect = false
	mysg.c = c
	gp.waiting = mysg
	c.recvq.enqueue(mysg)
	gp.parkingOnChan.Store(true)
	gopark(chanparkcommit, unsafe.Pointer(&c.lock), waitReasonChanReceive, traceBlockChanRecv, 2)

	// ⑦ 醒来
	...
	releaseSudog(mysg)
	return true, mysg.success
}
```

> 💡 **一个极其重要的细节**：`typedmemclr(c.elemtype, qp)` 这行 —— 出队之后必须把 slot 清零。为什么？因为 `hchan.buf` 是一块连续内存，如果不清空，GC 会继续把已经取走的元素当成"活指针"，**造成内存泄漏**（对 `chan *BigStruct` 尤其明显）。这就是"channel 里的值被消费后不会滞留"的实现保证。

### 20.3.5 `send` / `recv`：直接移交是怎么实现的（含写屏障）

```go
// runtime/chan.go（删减）—— 直接把数据写进对端栈
func send(c *hchan, sg *sudog, ep unsafe.Pointer, unlockf func(), skip int) {
	// sg.elem != nil 说明对端是"阻塞的接收方"，它的接收变量地址已知
	if sg.elem != nil {
		sendDirect(c.elemtype, sg, ep) // ★ 直接写对方栈
		sg.elem = nil
	}
	gp := sg.g
	gp.param = unsafe.Pointer(sg) // 告诉被唤醒的 g："你的 sudog 在这里"
	sg.success = true
	if sg.releasetime != 0 {
		sg.releasetime = cputicks()
	}
	goready(gp, skip+1) // 放回 runq（注意：这里是"解锁之后"由外层 unlockf 保证）
}

// 原文注释非常经典，值得完整保留：
// "Sends and receives on unbuffered or empty-buffered channels are the
//  only operations where one running goroutine writes to the stack of
//  another running goroutine. The GC assumes that stack writes only
//  happen when the goroutine is running and are only done by that
//  goroutine. Using a write barrier is sufficient to make up for
//  violating that assumption, but the write barrier has to work."
func sendDirect(t *_type, sg *sudog, src unsafe.Pointer) {
	dst := sg.elem // 接收方栈上的地址
	typeBitsBulkBarrier(t, uintptr(dst), uintptr(src), t.Size_) // ★ 批量写屏障
	// 之后直接 memmove；因为 dst 是 goroutine 栈，可能需要 write barrier
	memmove(dst, src, t.Size_)
}
```

`recv` 的对称实现在同文件：

```go
func recv(c *hchan, sg *sudog, ep unsafe.Pointer, unlockf func(), skip int) {
	if c.dataqsiz == 0 {
		// 无缓冲：直接从发送方栈拷到接收方栈
		if ep != nil {
			recvDirect(c.elemtype, sg, ep)
		}
	} else {
		// 有缓冲且队列满：先取队头给接收方，再把发送方的值补到队尾
		qp := chanbuf(c, c.recvx)
		if ep != nil {
			typedmemmove(c.elemtype, ep, qp)
		}
		typedmemmove(c.elemtype, qp, sg.elem) // 发送方的值填进刚空出的 slot
		c.recvx++
		if c.recvx == c.dataqsiz {
			c.recvx = 0
		}
		c.sendx = c.recvx // 队满时 sendx 必然等于 recvx（都绕了一圈）
	}
	sg.elem = nil
	gp := sg.g
	gp.param = unsafe.Pointer(sg)
	sg.success = true
	goready(gp, skip+1)
}
```

> ⚠️ **`sendDirect` 这段注释是整章最值钱的一句话**：Go 的 GC 假设"一个 goroutine 的栈只由它自己写"。无缓冲 channel 打破了这个假设 —— 我直接往你栈里写。所以必须用**写屏障**（`typeBitsBulkBarrier`）让 GC 知道"这块区域的指针变了"。这也是为什么"给 C 代码传 Go 的栈指针 / 用 `uintptr` 藏栈地址"这类操作在 Go 里格外危险（第 16 章）。

### 20.3.6 `closechan`：为什么它是三规则里最危险的一个

```go
// runtime/chan.go（删减）
func closechan(c *hchan) {
	// ① 关闭 nil channel → panic
	if c == nil {
		panic(plainError("close of nil channel"))
	}

	lock(&c.lock)
	// ② 关闭已关闭的 channel → panic
	if c.closed != 0 {
		unlock(&c.lock)
		panic(plainError("close of closed channel"))
	}

	c.closed = 1 // 打标记（之后所有 send 都 panic，所有 recv 拿零值）

	var glist gList // ★ 先收集，解锁后统一唤醒（避免持锁 ready 导致栈收缩死锁）

	// ③ 唤醒所有阻塞的接收方：它们拿到零值，ok=false
	for {
		sg := c.recvq.dequeue()
		if sg == nil {
			break
		}
		if sg.elem != nil {
			typedmemclr(c.elemtype, sg.elem) // 接收变量清零
			sg.elem = nil
		}
		...
		gp := sg.g
		gp.param = unsafe.Pointer(sg)
		sg.success = false // ★ 关键：接收方据此返回 ok=false
		glist.push(gp)
	}

	// ④ 唤醒所有阻塞的发送方：它们会 panic（success=false 且 closed != 0）
	for {
		sg := c.sendq.dequeue()
		if sg == nil {
			break
		}
		sg.elem = nil
		...
		gp := sg.g
		gp.param = unsafe.Pointer(sg)
		sg.success = false
		glist.push(gp)
	}
	unlock(&c.lock)

	// ⑤ 解锁后统一唤醒（顺序无所谓，因为都已经不在队列里了）
	for !glist.empty() {
		gp := glist.pop()
		gp.schedlink = 0
		goready(gp, 3)
	}
}
```

**close 的三条规则**（背下来，CR 时一眼辨认）：

| # | 操作 | 结果 | 源码位置 | 正确做法 |
| --- | --- | --- | --- | --- |
| 1 | 向已关闭的 channel **发送** | `panic: send on closed channel` | `chansend` 的 `if c.closed != 0` | **只有发送方 close**，且发送方 close 后不再发 |
| 2 | **关闭**已关闭的 channel | `panic: close of closed channel` | `closechan` 的 `if c.closed != 0` | 用 `sync.Once` 或"唯一 owner"保证只关一次 |
| 3 | **关闭** nil channel | `panic: close of nil channel` | `closechan` 的 `if c == nil` | 别把"未初始化"当"空 channel" |
| —— | 从已关闭的 channel **接收** | 立刻返回零值，`ok=false`；缓冲里剩下的数据**会先被读完** | `chanrecv` 的 `if c.closed != 0` | `for v := range ch` 会自动在关闭且读空后退出 |
| —— | 关闭后重复接收 | 永远零值 + `ok=false`，不阻塞 | 同上 | 想"广播"给多个 goroutine，就靠这个特性 |

一条工程铁律，从源码直接推出来：**`close` 是一个"发送端的动作"，它不属于接收端**。

```go
// ✓ 正确：生产者唯一，自己 close
func produce(ch chan<- int) {
	defer close(ch) // 保证退出时关闭
	for i := 0; i < 10; i++ {
		ch <- i
	}
}

// ✗ 错误：多个生产者都 defer close(ch) → 第二个生产者 panic
// ✓ 正确：多生产者用 WaitGroup + 一个专职 closer
func produceN(ch chan<- int, ns []chan int) {
	var wg sync.WaitGroup
	for _, src := range ns {
		wg.Add(1)
		go func(src chan int) {
			defer wg.Done()
			for v := range src {
				ch <- v
			}
		}(src)
	}
	go func() { wg.Wait(); close(ch) }() // ★ 只有这里可以 close
}

// ✗ 错误：接收方 close（不知道还有没有人要继续发）
for v := range ch {
	_ = v
	close(ch) // panic 风险极高
}
```

### 20.3.7 happens-before：channel 不只是"传值"，还是"传同步"

这条是**语义级**的，比实现更重要。Go 内存模型（与第 23 章呼应）规定了四条：

| # | 规则 | 直觉 |
| --- | --- | --- |
| 1 | 对 channel 的**第 n 次发送** happens-before **第 n 次接收完成** | 无缓冲时：发送方写的所有内存，接收方都看得见 |
| 2 | 对 channel 的**关闭** happens-before **从关闭 channel 收到零值** | `close(ch)` 之前的所有写，对"看到 ok=false 的人"可见 |
| 3 | 容量为 C 的缓冲 channel：**第 n 次接收** happens-before **第 n+C 次发送完成** | 缓冲区就像一个容量 C 的"令牌池"，第 n+C 次发送必须等前 C 个被取走 |
| 4 | 无缓冲 channel 的接收 happens-before 对应发送完成 | 双向同步（收发双方互相可见） |

```go
// 规则 1 的最小验证：没有 channel 就是 data race
var data string
var ready = make(chan struct{})

func writer() {
	data = "hello"   // A
	close(ready)     // B: close happens-before 接收方看到零值
}
func reader() {
	<-ready          // C
	println(data)    // D: 一定看到 "hello"（否则就是 runtime 出错）
}
```

> ⚠️ **反过来说，这是最容易犯的错**：很多人以为"channel 就是线程安全的队列，放进去就完事了"。其实 channel 提供的 happens-before 只覆盖**通过它传的那个值**以及**发送方在发送前完成的所有写**。如果你在发送之后又改了被传的对象（尤其是传指针），那半边就完全没有同步保护：

```go
type Job struct{ N int }
ch := make(chan *Job, 1)
j := &Job{N: 1}
ch <- j     // 发送完成
j.N = 2     // ✗ 发送之后再改：接收方看不到保证（甚至触发 -race）
```

---

## 20.4 Java 对照

| 维度 | Java | Go channel | 会猜错的地方 |
| --- | --- | --- | --- |
| 本质 | 库（`java.util.concurrent`） | **语言级原语** + runtime 实现 | channel 有 `close`，`BlockingQueue` 没有（要毒丸） |
| 阻塞语义 | `put`/`take` 阻塞**线程** | 阻塞**goroutine**，M 被让出 | 十万个阻塞的 channel 操作 = 十万个 `_Gwaiting`，不吃线程 |
| 直接移交 | `SynchronousQueue`（`TransferStack`/`TransferQueue`） | **默认无缓冲 channel 就是这个**，且所有 channel 都先试直接移交 | Go 的 direct handoff 是"内建优化"，不是特例实现 |
| 关闭传播 | 无内建机制 | `close` 广播：所有接收方都拿到零值 | 用 `close(ch)` 做"取消信号广播"是 Go 惯用法（`ctx.Done()` 就是这个） |
| 超时 | `offer/put` 带 timeout 参数 | `select` + `Timer` 分支 | Go 的超时是"组合出来的"，不是 API 参数（第 21 章） |
| 公平性 | `ArrayBlockingQueue` 可选公平锁 | `sudog` 队列 FIFO（`select` 除外，是随机的） | `select` 多路就绪时**伪随机选择**，别依赖优先级 |
| 容量 | 构造时定，不可变 | 同（`make` 时定，不可变） | 想"动态扩容"得换实现（或者用 slice + mutex） |
| 性能 | 有锁（`ReentrantLock`） | 有锁（`hchan.lock`，runtime mutex） | **Go 的 channel 不是无锁的！** 高频场景比 `atomic` 慢，见 20.6 |

> 🎯 **Go 为什么把 channel 做成语言级？** 因为它是「CSP（communicating sequential processes）」模型的核心 —— 语言必须能对 `<-ch` 做**语法级**的支持（`select`、`for range`、`close` 内建、单/双向类型 `chan<- T`/`<-chan T`），才能让"用通信共享内存"变成**默认写法**而不是库调用。Java 的 `BlockingQueue` 只能叫你自己"记得用"，用不用得对，看你自觉。

---

## 20.5 编排模式：五种你天天会用到的形状

### 模式 1：pipeline（流水线）

把处理拆成多个 stage，每个 stage 一个 goroutine，stage 之间用 channel 连起来。**每个 stage 只有一条铁律：自己 close 自己的输出。**

```go
// 生成 → 过滤 → 转换 → 汇聚，全程流式，不攒全量
func pipeline(ctx context.Context, docs []string) <-chan string {
	gen := func() <-chan string {
		out := make(chan string)
		go func() {
			defer close(out)
			for _, d := range docs {
				select {
				case out <- d:
				case <-ctx.Done():
					return
				}
			}
		}()
		return out
	}

	upper := func(in <-chan string) <-chan string {
		out := make(chan string)
		go func() {
			defer close(out)
			for s := range in { // 上游 close 后自动退出
				select {
				case out <- strings.ToUpper(s):
				case <-ctx.Done():
					return
				}
			}
		}()
		return out
	}

	return upper(gen()) // 想加 stage 就套一层
}
```

注意三个细节：① 每个 stage 的 `select` 都带 `<-ctx.Done()`（否则上游取消时下游会卡死）；② `defer close(out)` 保证下游 `range` 能结束；③ 返回值是 `<-chan string`（只读方向），防止调用方误写。

### 模式 2：fan-out / fan-in（分发 + 汇聚）

```go
// fan-out: N 个 worker 抢同一个 jobs channel（天然负载均衡）
// fan-in : 把 N 个结果 channel 合并成一个
func fanIn(ctx context.Context, chans ...<-chan string) <-chan string {
	out := make(chan string)
	var wg sync.WaitGroup
	for _, c := range chans {
		wg.Add(1)
		go func(c <-chan string) {
			defer wg.Done()
			for v := range c {
				select {
				case out <- v:
				case <-ctx.Done():
					return
				}
			}
		}(c)
	}
	go func() { wg.Wait(); close(out) }() // ★ 唯一可以 close(out) 的地方
	return out
}
```

> 💡 **fan-out 的"负载均衡"是 channel 自带的**：N 个 worker 都在 `recvq` 上排队，`chansend` 时 `recvq.dequeue()` 出一个（FIFO），这就是 20.3.3 第 ④ 步的直接移交。**你不需要任何调度代码**，这就是 CSP 的威力。对比 Java：`ThreadPoolExecutor` + `BlockingQueue` 也是同构的（多个 worker `take`），但线程池是有界的、需要你自己管生命周期。

### 模式 3：orchestrator-worker（Agent 的核心形状）

这是第 41、43 章的骨架。**编排者发任务、收结果，中间处理"部分失败"和"首个成功即返回"**。

```go
type Tool struct{ Name string }
type Result struct {
	Tool string
	Out  string
	Err  error
}

func (t Tool) Call(ctx context.Context, arg string) (string, error) { /* ... */ return "", nil }

// 执行一组工具：全部完成才返回；任何一个失败只记录，不中止（除非 ctx 取消）
func runToolsRace(ctx context.Context, tools []Tool, arg string) []Result {
	// ★ 缓冲 = len(tools)：保证即使没人收了，所有 worker 也能发送完并退出（不泄漏）
	results := make(chan Result, len(tools))

	var wg sync.WaitGroup
	for _, t := range tools {
		wg.Add(1)
		go func(t Tool) {
			defer wg.Done()
			out, err := t.Call(ctx, arg)
			select {
			case results <- Result{Tool: t.Name, Out: out, Err: err}:
			case <-ctx.Done(): // ★ 兜底：编排者提前离场时不阻塞
				return
			}
		}(t)
	}

	go func() { wg.Wait(); close(results) }() // 谁关：专职 closer

	var out []Result
	for r := range results { // close 之后自动退出
		out = append(out, r)
	}
	return out
}

// 变体：首个成功即返回（其余因 ctx 取消而退出）
func firstSuccess(ctx context.Context, tools []Tool, arg string) (string, error) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel() // ★ 一定要 defer cancel，否则其余 goroutine 泄漏

	type res struct {
		out string
		err error
	}
	ch := make(chan res, len(tools))
	for _, t := range tools {
		go func(t Tool) {
			out, err := t.Call(ctx, arg)
			ch <- res{out, err} // 有缓冲，永不阻塞
		}(t)
	}
	var lastErr error
	for range tools {
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case r := <-ch:
			if r.err == nil {
				cancel() // 通知其余人退出
				return r.out, nil
			}
			lastErr = r.err
		}
	}
	return "", lastErr
}
```

**这两段代码几乎覆盖了 20.1 引子的所有坑**：`len(tools)` 缓冲 + `select ctx.Done()` + 专职 closer + `defer cancel()`。**Agent 里"并行工具调用"翻来覆去就是这个形状。**

### 模式 4：令牌桶 / 信号量（用 channel 做限流）

```go
// ① 计数信号量：用带缓冲 channel 实现"最多 N 个并发"
type Semaphore chan struct{}

func NewSemaphore(n int) Semaphore { return make(Semaphore, n) }

func (s Semaphore) Acquire(ctx context.Context) error {
	select {
	case s <- struct{}{}: // 有位置就占一个（struct{} 零成本）
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
func (s Semaphore) Release() { <-s }

// 用法：批量 embedding，最多 8 个并发
func embedAll(ctx context.Context, inputs []string) error {
	sem := NewSemaphore(8)
	var wg sync.WaitGroup
	for _, in := range inputs {
		wg.Add(1)
		go func(in string) {
			defer wg.Done()
			if err := sem.Acquire(ctx); err != nil {
				return
			}
			defer sem.Release()
			_, _ = embed(ctx, in)
		}(in)
	}
	wg.Wait()
	return ctx.Err()
}
```

> ⚠️ **令牌桶 vs 令牌桶**：上面是"并发上限"（semaphore），不是"速率限制"。真正按 QPS 限速要用 `golang.org/x/time/rate`（`rate.NewLimiter(rate.Limit(10), 20)`），它是**时间维度**的。两者常一起用：并发信号量限资源，rate limiter 限下游 QPS。另注意 `errgroup.SetLimit(n)` 与 `x/sync/semaphore` 内部也是这套（`semaphore.Weighted` 用的是 runtime 的信号量 + `sudog`，不是 channel）—— 性能更好但语义一样，详见第 26 章。

### 模式 5：结果汇聚 / token 流转发（SSE 场景）

```go
// LLM 流式 token：producer → channel → consumer（HTTP handler 逐块写出）
func streamToSSE(ctx context.Context, w http.ResponseWriter, llmStream <-chan string) error {
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	flusher, ok := w.(http.Flusher)
	if !ok {
		return errors.New("streaming unsupported")
	}
	for {
		select {
		case <-ctx.Done(): // 客户端断开
			return ctx.Err()
		case tok, ok := <-llmStream:
			if !ok { // 生产者 close → 正常结束
				fmt.Fprint(w, "data: [DONE]\n\n")
				flusher.Flush()
				return nil
			}
			fmt.Fprintf(w, "data: %s\n\n", tok)
			flusher.Flush() // ★ 必须 flush，否则攒在 bufio 里
		}
	}
}
```

这个模式的要点：**channel 是背压的载体**。如果客户端读得慢，`flusher.Flush()` 会阻塞（TCP 窗口满），于是 handler 不再从 channel 取 token，于是 LLM 生产侧的 `ch <- tok` 阻塞 —— **背压自动沿着 channel 传回上游**。这就是为什么 Go 的流式服务不需要额外的"背压机制"（第 39 章对比 SSE/WS 的背压时会再提）。

---

## 20.6 什么时候该用 Mutex 而不是 channel？

Go 社区有个被误传的口号："用通信来共享内存，不要用共享内存来通信"。**这句话不是教条**，官方在 [Effective Go](https://go.dev/doc/effective_go) 和 `sync` 包文档里都明确说过：channel 适合"移交所有权"和"编排"，`Mutex` 适合"保护状态"。

判据就三个问题：

| 问题 | 答案是 | 用什么 |
| --- | --- | --- |
| 数据是"交接棒"（一个时刻只有一个 owner）吗？ | 是 | channel（无缓冲或小缓冲） |
| 多个 goroutine 需要**读/改同一份状态**（计数器、缓存、连接池）吗？ | 是 | `sync.Mutex` / `RWMutex` / `atomic` |
| 需要"等待某个事件"或"取消广播"吗？ | 是 | channel（`close` 广播）或 `context` |
| 需要高频更新（每请求几十次）吗？ | 是 | `atomic` > `Mutex` >> channel |

**性能数量级**（同一台机器上相对比较，绝对值随版本/架构变化，以你自己的 benchmark 为准）：

| 操作 | 大致开销 | 说明 |
| --- | --- | --- |
| `atomic.Int64.Add(1)` | ~几 ns | 一条 `LOCK XADD` |
| `sync.Mutex` 无竞争 Lock/Unlock | ~10-20ns | 一次 CAS |
| 无缓冲 channel 收发（已就绪对端） | ~100-200ns | 两次 `gopark`/`goready` 记账 + 锁 + 内存拷贝 |
| 需要 park 的 channel 操作 | ~µs 级 | 真正发生调度切换 |
| `sync.Mutex` 高竞争 | ~µs 级 | 进入 `semacquire`，park |

**结论**：**channel 是编排工具，不是高性能队列**。如果你发现自己在拿 channel 当"共享内存的锁"用（比如用 `chan struct{}` 保护一个 map），那是设计走歪了 —— 用 `sync.RWMutex` 或 `sync.Map`。

```go
// ✗ 反模式：用 channel 当锁
type Cache struct{ ch chan struct{}; m map[string]string }
func (c *Cache) Set(k, v string) {
	c.ch <- struct{}{} // "加锁"
	c.m[k] = v
	<-c.ch             // "解锁"
}

// ✓ 正确：要保护状态就用 Mutex
type Cache struct {
	mu sync.RWMutex
	m  map[string]string
}
func (c *Cache) Get(k string) (string, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	v, ok := c.m[k]
	return v, ok
}
```

---

## 20.7 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| goroutine 卡在 `[chan send]` | 无缓冲且接收方已离场 | 缓冲 = 最大并发数 + `select ctx.Done()` | 20.1 |
| `panic: send on closed channel` | 多生产者都 `close`，或 close 后还有人发 | 谁生产谁关；多生产者用 `WaitGroup` + 专职 closer | 20.3.6 |
| `panic: close of closed channel` | 关闭路径不唯一 | `sync.Once` 包 close，或让 owner 独占关闭权 | 20.3.6 |
| 发送后改被传对象，出现 `-race` | channel 只保证"发送前"的写可见 | 传值或传不可变快照；发送后不再改 | 20.3.7 |
| `select` 多路都就绪时"顺序乱" | `select` 是伪随机选择（`pollorder` 打乱） | 需要优先级就嵌套 select | 21 章 |
| channel 当高性能队列用，CPU 高 | `hchan.lock` + 调度开销 | 用 `sync.Mutex`/`atomic`/分片；批量传 | 20.6、30 章 |
| `chan *BigStruct` 内存不降 | 已消费的元素没被清（其实 runtime 会 `typedmemclr`）—— 真正原因是缓冲里还压着 | 检查是否消费者跟不上；减小缓冲 | 20.3.4 |
| `for range ch` 永不退出 | 没人 close | 生产者 `defer close(out)`；或 `select ctx.Done()` 退出 | 20.5 |
| 用 channel 做"动态扩容队列" | 容量不可变 | 换 `slice + Mutex` 或第三方有界队列 | 20.4 |

---

## 20.8 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| `hchan` | `qcount`/`dataqsiz`/`buf`/`elemsize`/`closed`/`elemtype`/`sendx`/`recvx`/`recvq`/`sendq`/`lock` |
| 内存分配 | 元素无指针时 hchan + buf **一次分配**；`chan struct{}` 只有 hchan |
| 环形缓冲 | `buf[sendx]` 写、`buf[recvx]` 读，到 `dataqsiz` 回绕 |
| `sudog` | 阻塞的 g 的包装：`g`、`elem`（指向栈）、`success`、`c`，**池化复用** |
| 直接移交 | `chansend` 先查 `recvq`，有人等就 `sendDirect` 直接写对方栈（带写屏障） |
| 三规则 | 发已关闭 → panic；关已关闭 → panic；关 nil → panic |
| 关后接收 | 立刻零值 + `ok=false`，缓冲数据先读完 |
| 广播 | `close(ch)` 唤醒所有接收方 = Go 的"取消广播"（`ctx.Done()` 就是这么来的） |
| happens-before | 第 n 次发送 → 第 n 次接收；close → 收到零值；缓冲满 C 则"收 n → 发 n+C" |
| 性能 | 不是无锁！`atomic` < `Mutex` < channel；channel 是编排工具 |
| 谁 close | 只有发送方 / 唯一 owner；多生产者用 `WaitGroup` + 专职 closer |
| 防泄漏三件套 | 缓冲 = 并发数 + `select ctx.Done()` + `defer cancel()` |
| 编排模式 | pipeline / fan-out-fan-in / orchestrator-worker / 信号量 / 流式转发 |

---

## 20.9 自检题

1. **`make(chan int)` 和 `make(chan int, 1)` 的发送语义差在哪？为什么 20.1 引子里加个 `1` 就能"缓解"泄漏？**
   > 要点：无缓冲时 `chansend` 必须找到 `recvq` 里的接收方（`sendDirect` 直写对方栈）或阻塞；有缓冲且未满时，`chansend` 只把值拷进 `buf[sendx]` 就返回，**不需要接收方在场**。所以缓冲让发送方"脱离接收方存在"，缓解了"接收方离场 → 发送方永久阻塞"，但**不能根治**（缓冲满了照样阻塞），正解是 `select + ctx.Done()`。

2. **`close(ch)` 之后，缓冲里剩下的值还能读到吗？`for v := range ch` 会怎样？如果有 10 个 goroutine 同时 `<-ch` 会怎样？**
   > 要点：能读 —— `chanrecv` 里 `if c.closed != 0 { if c.qcount == 0 { 零值+ok=false } }`，qcount > 0 时继续走正常出队分支。`for range` 会在 close 且读空后自动退出。10 个 goroutine 同时等，`closechan` 会把 `recvq` 全部 `dequeue` 并逐个 `goready`，每个都拿到零值 + `ok=false` —— **这就是"广播"语义**，也是 `context` 取消的实现方式。

3. **有人说"Go 的 channel 就是线程安全队列，比 `sync.Mutex` 高级"。你怎么反驳？给出至少两个基于源码的理由。**
   > 要点：① channel 内部就是一把 `hchan.lock`（runtime `mutex`）+ 两个链表，既不"无锁"也不"更高级"；纯保护状态时它比 `Mutex` 慢一个数量级（还要额外 `gopark`/`goready` 记账）。② channel 的 happens-before 只覆盖"通过它传的值"和"发送前的写"，**不会替你保护被共享的对象**，拿它当锁用属于误用。③ 它是**编排**原语（所有权移交、事件通知、取消广播、背压传导），这些能力 `Mutex` 提供不了；反之保护"多读多写的共享状态"应该用 `RWMutex`/`atomic`。选哪个看语义，不看"高低"。

---

## 20.10 延伸阅读

- `$GOROOT/src/runtime/chan.go`：`hchan`、`waitq`、`makechan`、`chansend`、`chanrecv`、`send`、`recv`、`sendDirect`、`closechan`、`chanparkcommit`、`full`/`empty`/`chanbuf`
- `$GOROOT/src/runtime/runtime2.go`：`sudog`、`waitReasonChanSend`/`waitReasonChanReceive`
- `$GOROOT/src/sync/waitgroup.go`、`$GOROOT/src/sync/rwmutex.go`：`Mutex` 与 channel 的取舍对照（第 22 章）
- Go Memory Model（2022 修订）：channel 的四条 happens-before 规则原文
- 官方博客：*Share Memory By Communicating*、Rob Pike 的 CSP 演讲
- `golang.org/x/sync`：`errgroup`、`semaphore`（第 26 章）
- 上一章：[19. 调度时机全景](19-scheduler-runtime.md) ｜ 下一章：[21. select 与超时控制](21-select-timeout.md)

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 19 章 调度时机全景](19-scheduler-runtime.md)
- 下一章：[第 21 章 select 与超时控制](21-select-timeout.md)
- 相关：[第 17 章 goroutine 与栈（`parkingOnChan`）](17-goroutine-internals.md)、[第 22 章 sync 原语实现](22-sync-primitives.md)、[第 23 章 内存模型](23-memory-model.md)、[第 25 章 并发陷阱](25-concurrency-pitfalls.md)、[第 26 章 结构化并发](26-structured-concurrency.md)、[第 41 章 工具系统](41-tool-system-function-calling.md)
