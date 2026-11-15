# 21. select 与超时控制

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 并发与调度器源码 ｜ **编号**：21 / 54
> **本章定位**：`select` 是 Go 并发的"路由表"。上一章把 `hchan` 拆到字节级看过了，这一章看的问题是：**多个 channel 怎么被一次原子操作"挑一个"**，以及超时、非阻塞、动态关闭分支这三件日常事背后的真实代价。
> **读法**：先扫 21.1 的坑（你大概写过），再读 21.3 的 `selectgo`（本章唯一需要静下心的地方），21.5/21.6 当手册查。前置：`hchan` / `sudog` / `sendq` / `recvq` 直接复用第 20 章，本章不重复。

---

## 21.1 引子：那个跑了三天才被发现的循环

场景是 Agent 网关里的"工具结果聚合器"。它要同时盯三个来源：LLM 的 token 流、工具的返回、用户的中断信号。第一版长这样：

```go
func (a *aggregator) run() {
	for {
		select {
		case tok := <-a.tokens:
			a.emit(tok)
		case res := <-a.results:
			a.emit(res)
		case <-time.After(30 * time.Second): // 30 秒没消息就认为本轮结束
			a.finishRound()
		}
	}
}
```

代码看起来很干净，review 也过了。跑三天后告警来了：这个 Pod 的 RSS 从 200MB 涨到 6GB，GC 周期从 2ms 变成 300ms，p99 延迟跟着窜到 8 秒。

三个真凶，全部藏在 `select` 的语义里：

1. **`time.After` 在循环里**：每一轮 tick 都 `new` 一个 runtime timer 挂进时间堆，30 秒内不会被回收。一轮对话产生几百个 token，就是几百个 timer 常驻。
2. **没有退出条件**：`select` 里没有 `case <-ctx.Done()`，用户点了"停止"之后这个 goroutine 只能等 30 秒空闲超时，而且 `a.finishRound()` 之后它还在循环。
3. **`default` 缺席导致的隐性饥饿**：后来有人为了"防止阻塞"加了个 `default:`，直接把这个 goroutine 变成 CPU 100% 的忙轮询。

`select` 的语法只有十几行，但它是 Go 里**唯一一个"看起来很声明式、实际是命令式状态机"**的语法结构。这一章就把它拆开。

---

## 21.2 直觉模型：一个"多路复用点"

先把`select` 在脑子里的形象定下来：**它不是 if-else 链，而是一个"多路复用点"（multiplexing point）** —— 你把 N 个可能的"事件"摆上去，运行时随即挑一个能推进的，执行它，然后整个 `select` 就结束了。注意：**只执行一个 case**，没有 fallthrough，也没有"处理完继续留在 select 里"这回事（那是 `for` 的活）。

```
         ┌──────────────┐
 tokens ─┤              │
 results─┤    select    ├─► 恰好一个 case 被选中（随机）
 ctx.Done┤  (多路复用点) │   其余分支本次完全不动
  timer ─┤              │
         └──────────────┘
```

跟 Java 放在一起看，你脑子里的映射会立刻清楚——也会立刻踩坑：

| 想做的事 | Go | Java 里你会用的 | 会猜错的地方 |
| --- | --- | --- | --- |
| 等多个队列中任意一个 | `select` | `ExecutorCompletionService.take()`、`CompletableFuture.anyOf` | `anyOf` 返回的是 future，你不做额外判断就不知道**是哪一个**先完成；Go 的 `select` 直接告诉你是第几个 case |
| 带超时等 | `select` + `<-ctx.Done()` | `BlockingQueue.poll(t, unit)` | `poll` 只能盯一个队列；Go 的超时是**所有分支共享的一个分支** |
| 非阻塞地试一下 | `select { ... default: }` | `poll()`、`tryLock()` | 语义一样，但 Go 的 `default` 只有在**所有**分支都不就绪时才走 |
| 多路 IO 就绪 | `select` on channels | `java.nio.channels.Selector` | Selector 要**注册**、只支持 NIO Channel、水平触发；Go 的 `select` 是一次性的、零注册成本、case 列表编译期就固定 |
| 等待/广播通知 | `close(ch)` / `sync.Cond` | `Object.wait/notifyAll` | `close` 天然是广播，且带 happens-before；`notifyAll` 之后大家还要抢锁 |

> 💡 一句话记住语义差异：**Java 的 `anyOf` 是"谁先完成"，Go 的 `select` 是"现在谁就绪"**。前者是事件结果，后者是状态快照。这个区别决定了很多事情——比如 `select` 永远不会"错过"一个已经缓冲在 channel 里的值，而 `anyOf` 一旦完成就定死了。

---

## 21.3 底层实现：`selectgo` 三段式

### 21.3.1 数据结构：只有两个字段

`src/runtime/select.go` 里 case 的运行时描述就俩字段（老版本还有 `kind`/`releasetime`，1.20 之后被精简掉了）：

```go
// src/runtime/select.go
type scase struct {
	c    *hchan         // chan
	elem unsafe.Pointer // data element（发送时的源 / 接收时的目标）
}
```

一个 case 到底是谁（send / recv / default）不需要存在结构体里，因为**编译器保证了排列顺序**：`nsends + nrecvs = ncases`，send case 一定排在前 `nsends` 个，所以判断方向只要 `casi < nsends`。`default` 根本不进入数组，它被编译器翻译成 `selectgo` 的 `block` 参数（有 `default` → `block=false`）。

`selectgo` 的签名本身就说明了全部内存布局：

```go
func selectgo(cas0 *scase, order0 *uint16, pc0 *uintptr,
	nsends, nrecvs int, block bool) (int, bool) {

	ncases := nsends + nrecvs
	// 上限 65536：为了压住栈使用量（源码注释原话）
	scases := cas1[:ncases:ncases]
	pollorder := order1[:ncases:ncases]           // 轮询顺序（随机）
	lockorder := order1[ncases:][:ncases:ncases]  // 加锁顺序（按地址排序）
	...
	// 生成随机轮询顺序：Fisher-Yates 变体，用 cheaprandn
	for i := range scases {
		if cas.c == nil {        // nil channel：直接从两套 order 里剔除
			cas.elem = nil
			continue
		}
		j := cheaprandn(uint32(norder + 1))
		pollorder[norder] = pollorder[j]
		pollorder[j] = uint16(i)
		norder++
	}
	// 按 hchan 的地址做堆排序，得到全局一致的加锁顺序
	...heap sort...
	sellock(scases, lockorder)
```

三个关键点，逐条说：

- **`order0` 里其实塞了两个数组**：前 `ncases` 个是 `pollorder`，后 `ncases` 个是 `lockorder`，一块内存两用。这块内存**在 goroutine 栈上**，由 `cmd/compile/internal/walk/select.go` 生成——所以 `select` 本身**零堆分配**（除非你 case 里的 channel 逃逸了）。
- **`pollorder` 是随机置换**：`cheaprandn` 是 runtime 内部那个非密码学快速随机数（`runtime.rand`），比 `math/rand` 快得多也弱得多，这里够用。这就是"随机公平"的实现——不是轮转，是洗牌。
- **`lockorder` 按 channel 地址排序**：这一步是防死锁的关键。两个 goroutine 各自 `select` 了 `{ch1, ch2}`，加锁顺序相反就会互相等；排序之后所有人按同一个全局顺序加锁，死锁不可能发生。

### 21.3.2 三段式：查 → 挂 → 摘

```
selectgo(cas, order, nsends, nrecvs, block)
  │
  ├─ ① pollorder = 随机洗牌；lockorder = 按 hchan 地址堆排序
  ├─ ② sellock(lockorder)：按地址顺序把涉及的 channel 全锁上（重复地址只锁一次）
  │
  ├─ ③ pass 1（快路径）：按 pollorder 扫一遍，看谁已经就绪
  │      recv case:  sendq 有人等 → goto recv（直接移交）
  │                  qcount > 0 → goto bufrecv（读缓冲）
  │                  closed != 0 → goto rclose（零值 + recvOK=false）
  │      send case:  closed != 0 → goto sclose（panic: send on closed channel）
  │                  recvq 有人等 → goto send（直接移交）
  │                  qcount < dataqsiz → goto bufsend（写缓冲）
  │      命中 → selunlock → return
  │      没命中 ─┬─ block == false（有 default）→ selunlock → return (-1,false)
  │              └─ block == true → 往下
  │
  ├─ ④ pass 2（慢路径）：为每个 case acquireSudog()，
  │      按 lockorder 顺序挂到各自 channel 的 sendq/recvq 上，
  │      同时在 gp.waiting 里串成一条链表（顺序 == lockorder）
  │
  ├─ ⑤ gopark(selparkcommit)：让出 P。selparkcommit 里按 gp.waiting 顺序解锁
  │      （注意：必须"先解锁最后一个 channel 之后再碰 gp 的东西"，源码有专门注释）
  │
  ├─ ⑥ 某天某个 sender/receiver/closechan 把我这个 sudog 摘走，置 success
  │
  ├─ ⑦ pass 3：重新 sellock；扫 gp.waiting 找出那个 success 的；
  │      其余 sudog 从各自队列 dequeueSudoG，然后 releaseSudog 归还
  └─ ⑧ selunlock → return (casi, recvOK)
```

pass 1 的真实代码长得很有"goto 风格"，但逻辑就是上面这张图：

```go
	for _, casei := range pollorder {
		casi = int(casei)
		cas = &scases[casi]
		c = cas.c
		if casi >= nsends { // recv case
			sg = c.sendq.dequeue()
			if sg != nil { goto recv }
			if c.qcount > 0 { goto bufrecv }
			if c.closed != 0 { goto rclose }
		} else { // send case
			if c.closed != 0 { goto sclose }
			sg = c.recvq.dequeue()
			if sg != nil { goto send }
			if c.qcount < c.dataqsiz { goto bufsend }
		}
	}
	if !block { // 有 default
		selunlock(scases, lockorder)
		casi = -1
		goto retc
	}
```

> 🩸 **两个"违反直觉但正确"的细节**：
> 1. `recv` / `send` 这两个分支走的是 **direct handoff**（`recv(c, sg, cas.elem, ...)`），数据直接从发送者栈上搬到我这边，**缓冲区完全被绕过**。这是第 20 章讲过的直接移交，在 `select` 里同样生效。
> 2. `selunlock` 的注释值得读一遍：**"解锁完最后一个锁之后绝不能再碰 sel"** —— 因为另一个 M 上的同一个 G 可能已经把 `sel` 那块栈内存回收了。这就是 runtime 里那种"读懂注释比读代码重要"的典型段落。

### 21.3.3 编译器替你做的事

| 你写的 | 编译器生成 | 备注 |
| --- | --- | --- |
| `select {}`（空） | `runtime.block()`：`gopark(nil,nil,waitReasonSelectNoCases,...)` | 永久阻塞，死锁检测器会报 `all goroutines are asleep` |
| `select { case v := <-ch: }` | 直接 `chanrecv1/chanrecv2` | 单 case 没必要走 `selectgo` |
| `select { case ch <- v: case default: }` | `selectnbsend(ch, v)` | 非阻塞发送的标准实现 |
| `select { case v := <-ch: default: }` | `selectnbrecv(ch, &v)` | 非阻塞接收 |
| 只有 `default` | 什么都不做 | —— |
| 实时 `reflect.Select` | `reflect_rselect` → `selectgo`（在堆上建 `scase`） | 有反射开销，别放热路径 |

### 21.3.4 nil channel：不是 bug，是开关

`select` 里 `cas.c == nil` 时，这个 case 在 pass 1/2 里被**整个跳过**，表现就是"永远不就绪"。这不是特例逻辑，是洗牌时 `continue` 顺手做掉的自然结果。但它撑起了一个极其实用的惯用法：**把 channel 变量置为 nil，就等于动态关掉这个分支**。

```go
// 场景：合并两个上游流，某个流关闭后就不再监听它
func merge(a, b <-chan string) <-chan string {
	out := make(chan string)
	go func() {
		defer close(out)
		for a != nil || b != nil {
			select {
			case v, ok := <-a:
				if !ok { a = nil; continue } // 关键：a 变 nil，下一轮 select 直接忽略该分支
				out <- v
			case v, ok := <-b:
				if !ok { b = nil; continue }
				out <- v
			}
		}
	}()
	return out
}
```

为什么必须这么写？因为 `case v, ok := <-a` 在 `a` 已关闭后会**永远就绪**（返回零值 + `ok=false`），于是 `select` 会一直选中它，形成"关闭的 channel 把其他分支饿死"的经典 bug。置 nil 是唯一不引入额外状态的解法。

同样的技巧用在定时器上：**"只有需要超时才让超时分支存在"**。

```go
var timeout <-chan time.Time // nil，永不触发
if deadline > 0 {
	timeout = time.After(deadline)
}
select {
case v := <-ch:
	...
case <-timeout: // 只有上面赋过值才可能被选中
	...
}
```

> ⚠️ 反向提醒：**对一个 nil channel 直接收发会永久阻塞**（`chansend`/`chanrecv` 里 `c == nil` 就走 `block()`）。`select` 里 nil 是"忽略"，`select` 外 nil 是"死等"。这是 Java 程序员最难受的一点——Java 里 `null` 是立刻 NPE，Go 里是安静地卡住。

---

## 21.4 Java 对照：为什么 Go 不需要"注册/注销"

Java 的 `Selector` 用了二十年证明了一件事：**多路复用的性能取决于"注册表"的设计**。所以 `Selector` 有一整套 `register/deregister`、`SelectionKey`、`interestOps`、`selectedKeys` 的生命周期管理。

Go 走了完全相反的路：**没有注册表，每次 `select` 现场"摆摊"**。为什么能这么干？

1. **case 列表是编译期固定的**。`select` 的每个 case 在编译期就确定是 send / recv / default，运行时只可能因为 channel 为 nil 而"缺席"。所以空间可以在栈上一次算好，不需要动态容器。
2. **`sudog` 是复用的**。pass 2 从 `sudog` 池里 `acquireSudog()`，被唤醒后 `releaseSudog()` 归还，避免每次阻塞都 malloc。
3. **等待队列挂在 channel 上，不在 goroutine 上**。`hchan.sendq/recvq` 是侵入式链表，一个 goroutine 可以同时出现在多个队列里（每个 case 一个 sudog）——这点 `Selector` 做不到，因为 Java 的 `SelectionKey` 是"一个 channel 一个 key"。

代价也很直接：**任何一次会阻塞的 `select` 都要 O(ncases) 建 sudog、O(ncases) 加解锁**。所以 `select` 适合"分支少、事件稀疏"的场景（2~10 个分支），不适合拿来做几千个 channel 的路由表——那种场景正确做法是 **fan-in 到一条 channel**，或者用单 writer 的状态机（详见第 25 章）。

---

## 21.5 框架与工程落地

### 21.5.1 超时的三件套怎么选

| 工具 | 分配 | 能否复用 | 适用 | 坑 |
| --- | --- | --- | --- | --- |
| `time.After(d)` | 每次新建 Timer | 否 | **一次性** select | 放循环里 = 每轮一个 timer |
| `time.NewTimer(d)` | 每次新建，但可 `Stop`/`Reset` | 是 | 长循环、连接保活 | `Reset` 前必须 `Stop` 且 1.22 及更早还要 drain |
| `time.NewTicker(d)` | 周期 | 是 | 心跳、定时 flush | 忘了 `Stop` 会一直有 goroutine/timer |
| `context.WithTimeout` | 一棵 ctx 树 + 一个 timer | 是 | **跨函数、跨服务传递** | 必须 `defer cancel()`，否则子 ctx 挂到父树上不释放 |
| `http.Server.WriteTimeout` | 服务器级 | —— | 兜底 | 1.20+ 用 `ResponseController` 做单请求粒度 |

```go
// 正确写法：长循环里复用 timer，且能被 ctx 打断
var errIdle = errors.New("idle timeout") // 包级哨兵错误

func collect(ctx context.Context, ch <-chan int, idle time.Duration) (int, error) {
	t := time.NewTimer(idle)
	defer t.Stop()
	var n int
	for {
		select {
		case <-ctx.Done():
			return n, ctx.Err()
		case v, ok := <-ch:
			if !ok {
				return n, nil
			}
			n += v
			// Go 1.23+：timer 通道改为无缓冲，Reset 前不必再 drain；
			// 1.22 及更早要写 if !t.Stop() { select { case <-t.C: default: } }
			t.Reset(idle)
		case <-t.C:
			return n, errIdle
		}
	}
}
```

> 📌 **版本事实**：Go 1.23 重写了定时器实现（`runtime/time.go` 换成每 P 一个小根堆 + 无缓冲 timer channel），顺带解决了两件事——(1) `time.After`/`NewTimer` 创建的 timer 如果程序不再引用它，可以被 GC 回收（1.22 及以前，timer 在 fire 之前被 runtime 强引用，这才是"`time.After` 泄漏"的物理原因）；(2) `Stop`/`Reset` 不再产生陈旧值，那套 drain 咒语可以删了。**但工程结论没变**：长循环里别用 `time.After`，因为每轮仍要分配、仍要进时间堆。

### 21.5.2 取消链：`ctx.Done()` 是唯一的正统超时

`time.After` 只能停你自己这一个 `select`；`ctx` 能停**整条调用链**。这两者不是替代关系，是层次关系：

```
HTTP 请求 ctx (net/http 派生，客户端断开即取消)
   └─ WithTimeout(2s)  ←── 本轮对话总预算
        ├─ LLM 调用（SDK 内部再 WithTimeout(90%预算)）
        ├─ 并行工具 A（WithTimeout(500ms)）
        └─ 并行工具 B
```

`net/http` 侧的细节（详见第 38 章）：`conn.serve()` 为每个请求派生一个可取消的 `ctx`，值里塞了 `ServerContextKey` / `LocalAddrContextKey`，客户端断开时由 `connReader` 的后台读发现并 cancel——所以 `r.Context()` 是真的会"响"的，前提是你在 `select` 里听了它。反过来，`http.Server` 的 `ReadTimeout/WriteTimeout/IdleTimeout/ReadHeaderTimeout` 是**连接级**的，跟 ctx 不是一回事；Go 1.20 的 `http.ResponseController` 可以给单个请求设读写 deadline，做 SSE 长连接时必须用它（第 39 章）。

---

## 21.6 踩坑清单（CR 可直接抄）

| # | 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- | --- |
| 1 | 内存/GC 随时间单调上涨 | `time.After` 写在 `for` 里，每轮一个 timer | 循环外 `NewTimer` + `Reset`，或 `ctx` 超时 | 21.5 |
| 2 | CPU 100%，pprof 显示大量 `runtime.selectgo` | `for { select { ... default: ... } }` 忙轮询 | 去掉 `default`，或加 `time.Sleep`/`runtime.Gosched`、或阻塞在 channel 上 | 21.6 |
| 3 | 已关闭 channel 的分支被反复选中，其他分支饿死 | 关闭的 channel 永不阻塞 | 收到 `ok=false` 后把 channel 变量置 `nil` | 21.3.4 |
| 4 | 请求方已断开，后端还在烧 token | `select` 里没有 `case <-ctx.Done()` | 每个等待点都听 ctx；`defer cancel()` | 第 24 章 |
| 5 | 程序随机卡住，pprof 全在 `runtime.block` | 对 nil channel 收发 | 区分"select 里 nil=忽略"和"直接收发 nil=死等" | 21.3.4 |
| 6 | `panic: send on closed channel` | 多写者 + 有人 close | 只让"唯一所有者"close；或用 `select`+`default` 做 try-send 并接受丢弃 | 第 20 章 |
| 7 | 以为 `select` 有优先级 | `pollorder` 是随机洗牌，不保证顺序 | 需要真优先级就嵌套：外层先非阻塞试高优先级，失败再阻塞 | 21.3.1 |
| 8 | 只包了一个 case 还写 `select` | 单 case 会被编译器降级成直接收发 | 直接写 `<-ch` / `ch <- v`，别给自己加噪音 | 21.3.3 |

> 💡 **关于"select 没有优先级"**：很多人以为 `case` 写在前面就先被检查。事实是 `pollorder` 洗过牌，**顺序完全随机**。要写优先级，唯一可靠的办法是两段式：
>
> ```go
> select { case v := <-high: handle(v); default: }  // 先非阻塞试高优先级
> select { case v := <-high: handle(v); case v := <-low: handle(v) }
> ```
>
> 这中间仍然有窗口期（高优先级在这两行之间到达时就错过了一次），要严格保序只能上队列。

---

## 21.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 语义 | 多路复用点：所有就绪分支里**随机**挑一个执行，执行完整个 select 结束 |
| `pollorder` | 随机洗牌（`cheaprandn`），这是"公平性"的全部实现；**没有优先级** |
| `lockorder` | 按 `hchan` 地址排序后加锁，从结构上消灭 select 之间的死锁 |
| 内存 | `scases` + 双 order 数组都在**栈上**，`select` 本身零堆分配 |
| `default` | 编译期变成 `block=false`，pass 1 没命中就立刻返回 |
| 阻塞路径 | 每个 case 一个 `sudog`，挂到 `sendq/recvq`，`gopark(selparkcommit)` 让出 P |
| nil channel | `select` 里 = 该分支永久不就绪（动态开关分支的正统做法）；直接收发 = 永久阻塞 |
| 超时 | 循环里用 `NewTimer`+`Reset`；跨函数用 `context.WithTimeout`；`time.After` 只配一次性 |
| 单 case | 编译器降级为 `chanrecv`/`chansend`/`selectnbrecv`/`selectnbsend`，别包 select |
| 关闭的 channel | 永远就绪（零值+`ok=false`），不置 nil 就会饿死其它分支 |

---

## 21.8 自检题

1. **`select` 有 5 个 case，其中 2 个 channel 都已就绪。为什么不能依赖"写在前面的先执行"？如果业务真的需要优先级怎么办？**
   > 要点：`selectgo` 用 `cheaprandn` 生成随机 `pollorder`，扫描顺序每次不同，所以哪个先被看到是随机的（这是刻意设计，防止某个分支被饿死）。要优先级就两段式：先用 `select ... default` 非阻塞试高优先级，没拿到再进阻塞 select；严格的优先级只能靠队列或单 writer 状态机。

2. **`var ch chan int` 和 `ch := make(chan int)` 在 `select` 里表现一样吗？在 `ch <- 1` 里呢？**
   > 要点：`select` 里 nil channel 的 case 会被 `selectgo` 直接剔除（洗牌时 `continue`），表现为"永不就绪"；而直接 `ch <- 1` / `<-ch` 对 nil channel 会走 `block()` 永久阻塞（`chansend`/`chanrecv` 里 `c == nil` 的分支）。一个是"关掉分支"，一个是"卡死当前 goroutine"。

3. **为什么 `for { select { case v := <-ch: ... case <-time.After(30*time.Second): ... } }` 是坏味道？Go 1.23 之后还坏吗？**
   > 要点：每轮都新建一个 runtime timer，30 秒内一直挂在时间堆上；在 1.22 及更早 timer 被 runtime 强引用，构成实打实的内存增长。1.23 重写后未被引用的 timer 可被 GC 回收，内存压力大幅缓解，但每轮的分配 + 查堆开销仍在，且逻辑上"每次都重置空闲计时"的意图不明确。正确写法是循环外 `time.NewTimer` + `Reset`，或干脆用 `context.WithTimeout` 让取消贯穿全链路。

---

## 21.9 延伸阅读

- `$GOROOT/src/runtime/select.go` —— `selectgo` / `sellock` / `selunlock` / `selparkcommit`，尤其读 `selunlock` 上面那段关于"解锁后不能碰 sel"的注释
- `$GOROOT/src/cmd/compile/internal/walk/select.go` —— `walkSelectCases`：单 case、`default`、空 `select` 各被重写成什么
- `$GOROOT/src/runtime/chan.go` —— `selectnbsend` / `selectnbrecv` / `block()`
- `$GOROOT/src/runtime/rand.go` —— `cheaprandn` 与 `runtime.rand`
- Go 1.23 release notes「Timers and Tickers」一节 —— timer 重写与 GC 可回收语义
- 官方博客 *Go Concurrency Patterns: Pipelines and cancellation* —— 循环 select 退出范式的官方版本

## 21.10 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 20 章 channel 原理与编排模式](20-channel-internals.md)（`hchan`、`sudog`、direct handoff）
- 下一章：[第 22 章 sync 原语实现](22-sync-primitives.md)
- 相关：[第 24 章 context：取消传播](24-context.md)、[第 38 章 net/http 深潜](38-net-http.md)、[第 39 章 实时通信](39-realtime-sse-websocket.md)
