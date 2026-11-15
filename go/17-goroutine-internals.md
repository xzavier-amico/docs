# 17. goroutine 源码与栈：`g`、`g0`、栈增长与泄漏本质

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 并发与调度器源码 ｜ **编号**：17 / 54
> **本章定位**：全书第一次真正打开 `$GOROOT/src/runtime`。读完你要能回答三个问题：`go f()` 这行代码在机器上到底发生了什么？为什么起始栈只有 2KB 还不会爆？为什么「goroutine 泄漏」在 runtime 眼里根本不算 bug，只是一堆挂在某个队列上、状态为 `_Gwaiting` 的结构体？
> **读法**：把本地 `$GOROOT/src/runtime/runtime2.go` 打开，对着 `type g struct` 逐字段看。本章所有源码都是**删减版**（只留关键行 + 中文注释），字段顺序与常量数值随版本演进 —— **以你本地 `$GOROOT/src` 为准**。

---

## 17.1 引子：一次「goroutine 数量只涨不跌」的报警

凌晨两点，你负责的 Agent 网关报警：

```
go_goroutines          320 → 18642（40 分钟内）
process_resident_memory 180MB → 3.1GB
P99 延迟正常，P99.9 从 90ms 涨到 400ms
```

你想 `jstack`，发现没有这个命令。Go 的等价物是 pprof 的 goroutine profile（`net/http/pprof` 默认就注册了）：

```bash
curl -s 'http://localhost:6060/debug/pprof/goroutine?debug=2' | head -40
```

输出长这样（真实格式）：

```
goroutine 8412 [chan receive, 12 minutes]:
main.(*Session).waitResult(0xc0001a2000)
	/app/internal/session.go:88 +0x3c
created by main.(*Hub).attach in goroutine 1
	/app/internal/hub.go:41 +0x1a5
```

一万八千个 goroutine，全是 `[chan receive, 12 minutes]`。**中括号里那两样东西就是本章的主角**：`chan receive` 来自 `g.waitreason`，`12 minutes` 来自 `nanotime() - g.waitsince`。

最扎心的是：**这段代码没有任何"泄漏 bug"**。没有死循环，没有忘关连接，没有漏写 `cancel()`。它只是「在一个 channel 上等一个永远不会来的结果」。GC 一点办法都没有 —— 这些 `g` 还被 `allgs` 引用、被 channel 的 `recvq` 引用，全是活的。

> 🩸 **Java 大脑在这里会卡住**：JVM 里线程 WAITING 在 monitor 上，栈是预留的虚拟内存，"看不见的开销"。Go 里每个 goroutine 的 2KB 栈是**从堆上实打实分配**的，一万八千个就是 `18000 × (2KB 栈 + ~400B 的 g)` ≈ 43MB 起步，而且还在涨。它不是"看不见的开销"，它是堆上的对象。

要看懂为什么"等"就等于"泄漏"，得先把 `g` 拆开。

---

## 17.2 直觉模型：一个 goroutine = 一个结构体 + 一块栈 + 一个状态

先给一张能贴在工位上的图 —— **一个 goroutine 其实是三块内存**：

```
                         ┌──────────────────────────────────────┐
   堆上（GC 管理的对象）   │  g 结构体（runtime2.go，约 400B）      │
                         │  ├ stack       {lo, hi} ───────┐     │
                         │  ├ stackguard0 (= lo + 928)    │     │
                         │  ├ sched       gobuf{sp,pc,g}  │     │
                         │  ├ atomicstatus  _Grunning     │     │
                         │  ├ goid / m / _defer / _panic  │     │
                         │  └ waitreason / waitsince      │     │
                         └────────────────────────────────┼─────┘
                                                          ▼
                         ┌──────────────────────────────────────┐
   栈内存在 span 里         │ 栈内存 [lo ........................ hi) │
   （不被"扫描对象"看待，   │  ▲guard      ▲SP（当前栈顶）          │
     而是按栈帧精确扫描）   │  往低地址长 ────                       │
                         └──────────────────────────────────────┘
```

三句话记住：

| 部分 | 谁分配 | 谁管 | Java 里的对应物 |
| --- | --- | --- | --- |
| `g` 结构体 | `new(g)` / `malg`，堆分配 | GC（普通对象） | `java.lang.Thread` 对象的一半（field 区） |
| 栈内存 | `stackalloc`，从 span 里切 | 不移动对象，栈增长时**整块拷贝** | 线程栈（虚拟内存预留，不可移动） |
| 状态 + 寄存器快照 | `g.atomicstatus` / `g.sched` | 调度器 | 内核里线程的 task_struct + 上下文 |

**关键洞察**：Java 的线程栈一旦创建就钉在虚拟地址上，永远不会搬家；Go 的栈**会搬家**（`copystack`），搬的时候栈里的指针要被逐个重写。这一条差异，撑起了 Go "2KB 起步 + 百万 goroutine" 的全部能力，也带来了本章后面所有的复杂度（安全点、`parkingOnChan`、`stackguard0`）。

---
## 17.3 底层实现

### 17.3.1 `type g struct` 逐字段拆解

`runtime/runtime2.go`（删减版，注释是原注释的意译 + 我的补充）：

```go
type g struct {
	// stack 描述真实栈内存区间：[stack.lo, stack.hi)
	stack       stack   // offset known to runtime/cgo
	// stackguard0 是 Go 代码函数序言里比较的那个 SP 阈值。
	// 正常是 stack.lo+stackGuard，被抢占时会被改成 stackPreempt（一个魔数）。
	stackguard0 uintptr // offset known to liblink
	// stackguard1 只在 g0/gsignal 上有效：C 栈溢出检测用，普通 g 上是 ^0（永不溢出）
	stackguard1 uintptr // offset known to liblink

	_panic *_panic // 最内层 panic，链表头
	_defer *_defer // 最内层 defer，链表头
	m      *m      // 当前绑定的 M；offset known to arm liblink

	sched     gobuf  // 调度上下文：sp/pc/bp/g 四个寄存器快照，gogo 靠它切栈
	syscallsp uintptr // 若 status==_Gsyscall，GC 扫描用（= sched.sp）
	syscallpc uintptr // 同上
	syscallbp uintptr
	stktopsp  uintptr // 期望的栈顶 sp，traceback 校验用

	// param 是万能的"传参位"：
	//  1. channel 唤醒某个 goroutine 时，指向那个 sudog（直接移交的关键，见第 20 章）
	//  2. 新建但还没跑的 goroutine，指向 runtime.Goexit
	//  3. 信号处理相关
	param unsafe.Pointer

	atomicstatus atomic.Uint32 // 状态机：_Grunnable / _Grunning / ... （原子）
	stackLock    uint32        // sigprof / 栈扫描用的锁
	goid         uint64        // goroutine id（1 开始，不对外暴露）

	schedlink guintptr // 挂在各类队列上时的单链表指针（runq / gFree / gList）
	waitsince int64    // 进入 _Gwaiting 的大致时刻 → pprof 里的 "12 minutes"
	waitreason waitReason // 为什么阻塞 → pprof 里的 "[chan receive]"

	preempt       bool // 抢占信号，等价于 stackguard0 = stackPreempt
	preemptStop   bool // 抢占后是"停下等 GC 扫描"（true）还是"放回队列"（false）
	preemptShrink bool // 在同步安全点上顺便收缩栈
	asyncSafePoint bool // 是否停在异步安全点（栈上有不含精确指针的帧）

	gcscandone   bool // 栈是否已被 GC 扫过（配合 atomicstatus 的 _Gscan 位）
	activeStackChans bool // 是否正处于 channel 操作、栈上数据被 sudog 指着
	parkingOnChan atomic.Bool // 即将/正在 channel 上 park（栈不能收缩的窗口期）
	throwsplit   bool // 不许分裂栈（morestack 会直接 fatal）

	lockedm  muintptr // 被 LockOSThread 钉住的 M
	sig      uint32   // 导致 panic 的信号
	writebuf []byte   // 信号处理时的临时缓冲
	parentGoid uint64 // 谁创建了我（tracebackancestors 用）
	gopc     uintptr  // "created by ... at" 那行里的 PC —— 就是 go 语句的位置
	ancestors *[]ancestorInfo // 祖先链（debug.tracebackancestors）
	startpc  uintptr  // goroutine 入口函数 PC
	waiting  *sudog   // 我正在等的那个 sudog（channel / sema）
	timer    *timer   // time.Sleep 复用的缓存放这儿
	gcAssistBytes int64 // GC 协助标记的记账（见第 28 章）
}
```

逐行解读里，只需要真正吃透 6 个字段，其余都是"用到才查"：

| 字段 | 一句话 | 谁在读它 | 工程意义 |
| --- | --- | --- | --- |
| `stack` + `stackguard0` | 栈区间 + 溢出阈值 | 每个函数的序言（`CMPQ SP, 16(R14)`） | 栈溢出检测 + 抢占入口，**本章 17.3.5** |
| `sched`（`gobuf`） | 寄存器快照 | `gogo` / `gopark` / `morestack` | "切走再切回"能继续跑的全部秘密 |
| `atomicstatus` | 状态机 | `casgstatus` / 调度器 / GC | 排查泄漏的第一现场 |
| `goid` | 自增 ID | trace / 调试 | 代码里拿不到它（故意不给），别想着 `ThreadLocal` 那套 |
| `m` | 当前 M | 调度、抢占、`lockedm` | 与 P 无关：**`g` 绑的是 M，M 绑的是 P** |
| `waitreason` / `waitsince` | 阻塞原因 / 时刻 | `runtime.Stack` / trace / pprof | `[chan receive, 12 minutes]` 的来源 |

配套的两个小结构体也一并记住：

```go
// runtime2.go
type stack struct {
	lo uintptr // 栈底（低地址）
	hi uintptr // 栈顶（高地址）
}

type gobuf struct {
	sp   uintptr        // 栈指针
	pc   uintptr        // 程序计数器（下一条指令）
	g    guintptr       // 属于哪个 g
	ctxt unsafe.Pointer // 闭包上下文（funcval 指针）
	ret  uintptr        // 返回值暂存（amd64 上一般不用）
	lr   uintptr        // arm64 的 link register
	bp   uintptr        // 帧指针（开启 framepointer 的架构）
}
```

`gobuf` 就这七个机器字。**整个 goroutine 的"上下文切换"开销 ≈ 保存/恢复这几十个字节**，这就是它能做到几十纳秒的原因（JVM 线程切换要进内核，虚拟线程 mount/unmount 也要走 JVM 的 chunk 冻结/解冻）。

### 17.3.2 三个"特殊 goroutine"：`g0`、`gsignal`、`m0`

runtime 里有三种不是用户创建的 `g`，它们在 Java 里**没有对应物**，理解它们是从"会用 Go"到"看懂 Go"的分水岭。

```
        ┌─────────────────────────── m0（进程第一个 M，静态变量）──────────────────┐
        │                                                                          │
        │   g0（每 M 一个，调度器专用栈）        curg（用户的 goroutine）             │
        │   ┌───────────────────────┐         ┌───────────────────────┐            │
        │   │ 8KB 固定、不增长        │         │ 2KB 起、可增长可搬家    │            │
        │   │ schedule() 就跑在这里   │ ──切换──▶│ 你的代码跑在这里        │            │
        │   │ stackguard1 有效       │ ◀──gogo──│                        │            │
        │   └───────────────────────┘         └───────────────────────┘            │
        │                                                                          │
        │   gsignal（每 M 一个，信号处理专用栈，约 32KB）：收到 SIGURG/SIGSEGV 时切过去  │
        └──────────────────────────────────────────────────────────────────────────┘
```

| 特殊 g | 何时创建 | 栈来源 | 为什么必须有它 |
| --- | --- | --- | --- |
| `g0` | 每个 M 一个（`allocm` 里 `malg(8192 * sys.StackGuardMultiplier)`） | 固定大小，不可增长 | 调度、栈增长、GC 扫描这些动作**不能在用户栈上做**（正在换的就是这块栈）；`mcall`/`systemstack` 就是在 g0 上跑 |
| `gsignal` | 每个 M 一个 | 独立信号栈（`signalStackSize`，约 32KB 量级） | 信号可能在栈快满的时候到来，或者在 malloc 中途到来，必须有独立栈兜底才能 `throw` 出可读崩溃信息 |
| `m0` | 进程启动，`runtime.m0` 静态变量 | 主线程栈 + 自己的 `g0` | 它是"第 0 号工人"：`ret0_go` → `schedinit` → `main` 全都跑在 `m0.curg` 上；`cgo` 回调进来的线程也会临时造一个 M |
| `g0.m.curg` | 每 M 的生命周期 | —— | **M 永远只跑一个用户 g**，切换就是把 `m.curg` 换掉再 `gogo` |

> 💡 记住这条不等关系：**`g0` 是 M 的"值班室"，`curg` 是"M 正在接待的客户"**。你在 `runtime.Stack` 里偶尔看到 `runtime.gopark` 直接挂在 `g0` 上，说明系统正在做调度动作（通常是 GC 或 stop-the-world 相关）。

### 17.3.3 `go f()` 的完整链路：`newproc` → `newproc1` → `gostartcallfn` → `gogo`

这是本章最该背下来的一条调用链。`runtime/proc.go`：

```go
func newproc(fn *funcval) {
	gp := getg()          // 当前 goroutine
	pc := getcallerpc()   // 供 traceback 用的 "created by" PC
	// 为什么要 systemstack？因为下面可能要分配新栈、可能触发栈增长，
	// 而这类动作不能跑在"正可能被换掉"的用户栈上
	systemstack(func() {
		newg := newproc1(fn, gp, pc, false, waitReasonZero)

		pp := getg().m.p.ptr()   // 此刻在 g0 上，getg() 是 g0
		runqput(pp, newg, true)  // true = 塞进 runnext 插队（见第 18 章）

		if mainStarted {
			wakep()              // 有空闲 P 就拉一个 M 起来干活
		}
	})
}

func newproc1(fn *funcval, callergp *g, callerpc uintptr, parked bool, waitreason waitReason) *g {
	mp := acquirem()          // 禁止抢占：接下来要同时持有 m 和 p 的局部变量
	pp := mp.p.ptr()

	// ① 先从本地/全局 dead g 池捞一个复用，捞不到才新建
	newg := gfget(pp)
	if newg == nil {
		newg = malg(stackMin)                 // g 结构体 + 2KB 栈
		casgstatus(newg, _Gidle, _Gdead)
		allgadd(newg)                         // 挂进全局 allgs，GC 从此会扫到它
	}

	// ② 在栈顶腾出 4 个机器字 + MinFrameSize，用来"伪造"一次函数调用
	totalSize := uintptr(4*goarch.PtrSize + sys.MinFrameSize)
	totalSize = alignUp(totalSize, sys.StackAlign)
	sp := newg.stack.hi - totalSize
	spArg := sp
	if narg > 0 {
		// go 语句的参数从当前栈拷到新栈（栈→栈拷贝，走写屏障）
		memmove(unsafe.Pointer(spArg), unsafe.Pointer(argp), uintptr(narg))
	}

	// ③ 手工构造 gobuf —— 一个"从零开始"的上下文
	memclrNoHeapPointers(unsafe.Pointer(&newg.sched), unsafe.Sizeof(newg.sched))
	newg.sched.sp = sp
	newg.stktopsp = sp
	// ★ 返回地址故意设成 goexit（+PCQuantum 让 traceback 把上一条指令算进同一函数）
	newg.sched.pc = abi.FuncPCABI0(goexit) + sys.PCQuantum
	newg.sched.g = guintptr(unsafe.Pointer(newg))
	// ④ 把真正的入口函数压到"返回地址"的位置
	gostartcallfn(&newg.sched, fn)

	newg.parentGoid = callergp.goid
	newg.gopc = callerpc          // pprof 里 "created by ... hub.go:41" 就是它
	newg.startpc = fn.fn
	newg.goid = pp.goidcache      // goid 从 P 的本地缓存批量领，省全局原子操作
	pp.goidcache++

	casgstatus(newg, _Gdead, _Grunnable)  // 状态迁移：可被调度了
	releasem(mp)
	return newg
}
```

`gostartcallfn` / `gostartcall` 在 `runtime/stack.go`，只有 6 行，却是"伪造调用帧"的核心：

```go
func gostartcallfn(gobuf *gobuf, fv *funcval) {
	var fn unsafe.Pointer
	if fv != nil {
		fn = unsafe.Pointer(fv.fn) // 函数真实入口
	} else {
		fn = unsafe.Pointer(abi.FuncPCABIInternal(nilfunc))
	}
	gostartcall(gobuf, fn, unsafe.Pointer(fv)) // ctxt = funcval（闭包环境指针）
}

// gostartcall: 让新 goroutine 看起来像"被 gogo 调用"的一样
func gostartcall(buf *gobuf, fn, ctxt unsafe.Pointer) {
	sp := buf.sp
	sp -= goarch.PtrSize
	*(*uintptr)(unsafe.Pointer(sp)) = buf.pc  // 把 goexit 当"返回地址"压栈
	buf.sp = sp
	buf.pc = uintptr(fn)                      // PC 换成真正的函数入口
	buf.ctxt = ctxt
}
```

最后一步是汇编切栈，`runtime/asm_amd64.s`：

```asm
// void gogo(Gobuf*) —— 从 Gobuf 恢复状态，本质是一次 longjmp
TEXT runtime·gogo(SB), NOSPLIT, $0-8
	MOVQ	buf+0(FP), BX      // BX = gobuf 指针
	MOVQ	gobuf_g(BX), DX    // DX = g
	MOVQ	0(DX), CX          // 校验 g != nil
	JMP	gogo<>(SB)

TEXT gogo<>(SB), NOSPLIT, $0
	MOVQ	DX, g(BX)          // g 写进线程本地存储（amd64 用 R14 当 g 寄存器）
	MOVQ	gobuf_sp(BX), SP   // ★ 换栈：一行汇编完成用户态栈切换
	MOVQ	gobuf_ctxt(BX), DX
	MOVQ	gobuf_bp(BX), BP
	MOVQ	$0, gobuf_sp(BX)   // 清零，帮 GC 少扫一点
	MOVQ	$0, gobuf_ret(BX)
	MOVQ	$0, gobuf_ctxt(BX)
	MOVQ	gobuf_pc(BX), BX
	JMP	BX                 // ★ 跳到入口函数，goroutine 从此开始跑
```

把整条链路画成时序图：

```
 调用方 goroutine                  g0 栈                      新 goroutine
 ──────────────────────────────────────────────────────────────────────────────
 go f(a, b)
   │
   ├─► newproc(fn)
   │      └─ systemstack(...) ──切到 g0──► newproc1
   │                                        ├ gfget / malg(2KB)
   │                                        ├ 栈顶伪造帧 + 参数拷贝
   │                                        ├ gobuf{sp, pc=goexit, g=newg}
   │                                        ├ gostartcallfn → pc=f, [sp]=goexit
   │                                        ├ casgstatus(_Gdead → _Grunnable)
   │                                        └ runqput(pp, newg, next=true) → runnext
   │      ◄── 回到用户栈 ──
   │ 立刻继续执行 go 语句的下一条指令（不等！）
   │
   │   ... 某次 schedule() 选中 newg ...
   │                                        execute(newg)：_Grunnable → _Grunning
   │                                        gogo(&newg.sched)
   │                                          ├ SP = newg.stack.hi - 32
   │                                          ├ R14 = newg
   │                                          └ JMP f ─────────────► f(a,b)
   │                                                                   │
   │                                            f return ─────────────┤
   │                                            RET 弹出 goexit 地址   │
   │                                                                   ▼
   │                                                         runtime.goexit
   │                                                            └ goexit1 → mcall(goexit0)
   │                                                                └ gfput(g) → _Gdead
   │                                                                └ schedule()
```

> ⚠️ **两个必须记住的推论**：
> 1. goroutine 函数 `return` 之后**不是回到调用者**，而是回到 `runtime.goexit`。所以 `go f()` 里 `f` 的返回值只能被丢弃（编译期报错也是为这个）。
> 2. `runtime.Goexit()` 会先把 defer 全部跑完（内部用 `_panic{goexit:true}` 机制），再走 `goexit1`；但它**不能被 recover 拦住** —— 那种场景下 `recover` 返回 nil。详见第 8 章。

### 17.3.4 g 状态机：每种状态对应哪段源码

状态常量在 `runtime/runtime2.go`：

```go
const (
	_Gidle  = iota // 0：刚分配，未初始化
	_Grunnable     // 1：在某个 runq 上等着，不拥有栈
	_Grunning      // 2：正在跑用户代码，拥有栈，绑了 M 和 P
	_Gsyscall      // 3：正在跑系统调用，拥有栈，只绑 M（P 已解绑）
	_Gwaiting      // 4：被 runtime 阻塞（chan / mutex / netpoll / sleep…）
	_Gmoribund_unused // 5：历史遗留，gdb 脚本里还有
	_Gdead         // 6：死亡 / 未启用 / 在 gFree 空闲池里
	_Genqueue_unused  // 7
	_Gcopystack    // 8：栈正在搬家（copystack 执行中）
	_Gpreempted    // 9：被异步抢占停下，等 GC 扫完再放回去
	_Gscan        // 0x1000：与上面某个状态按位或，表示"GC 正在扫它的栈"
)
```

状态机长这样（箭头上的字就是触发它的函数）：

```
                go f()
                  │ newproc1: casgstatus
                  ▼
  ┌────────┐  ┌────────────┐   execute()   ┌────────────┐
  │ _Gidle │─▶│ _Grunnable │──────────────▶│ _Grunning  │
  └────────┘  └────────────┘   casgstatus  └─────┬──────┘
      ▲             ▲                            │
      │             │ goready()/ready()          │ gopark() → park_m
      │             │ (chan/netpoll/timer 唤醒)   │ casgstatus
      │             │                            ▼
      │        ┌────┴───────┐             ┌────────────┐
      │        │_Gpreempted │◀preemptPark─│  _Gwaiting │
      │        └────────────┘             └────────────┘
      │                                    （挂在某个等待队列上）
      │             ▲
      │             │ entersyscall
      │        ┌────┴──────┐
      │        │ _Gsyscall │──exitsyscall──▶ _Grunning / _Grunnable
      │        └───────────┘
      │
      │  goexit0 / gfput
  ┌───┴─────┐
  │ _Gdead  │ ← 在 sched.gFree / p.gFree 链表里，等下次 gfget 复用
  └─────────┘
```

改状态的入口只有一个 `casgstatus` 系列，它要求**在 g0 上调用**（或持锁）：

```go
// runtime/runtime2.go（删减：略去 _Gwaiting 过渡、profiling、GC 同步）
func casgstatus(gp *g, oldval, newval uint32) {
	if (oldval&_Gscan != 0) || (newval&_Gscan != 0) || oldval == newval {
		systemstack(func() {
			print("runtime: casgstatus: oldval=", hex(oldval), " newval=", hex(newval), "\n")
			throw("casgstatus: bad incoming values")
		})
	}
	for {
		old := gp.atomicstatus.Load()
		if old != oldval {
			throw("casgstatus: oldval mismatch")
		}
		if gp.atomicstatus.CompareAndSwap(oldval, newval) {
			return
		}
	}
}
```

> 💡 `_Gscan` 位为什么存在？因为 GC 扫一个 goroutine 的栈时，那个 g **不能同时跑**。`_Gscan|_Grunning` = "它在跑，但 GC 想扫"；`_Gscan|_Gwaiting` = "它停着，GC 正在扫"。这就是**安全点**在状态机里的投影 —— 详见第 19 章。

### 17.3.5 栈：`stackalloc`、`morestack`、`newstack`、`copystack`、`shrinkstack`

#### ① 起始栈只有 2KB，够用吗？

`runtime/stack.go` 里的常量：

```go
const (
	stackMin = 2048 // 新 goroutine 的起始栈 = 2KB（_StackMin）
	// 栈溢出检测的保留区：函数序言里 SP 低于 stack.lo+stackGuard 就触发 morestack
	stackGuard = 928*sys.StackGuardMultiplier + stackSystem
	stackSmall = 128  // 小帧函数可以跳过检测的阈值
	stackBig   = 4096 // 大帧函数必须检测
)
```

`malg(2048)` → `stackalloc` → 从 `mheap_.stackpool[order]`（按 2KB/4KB/8KB/16KB 分 order 的空闲 span 池）里切一块；池空时用 `mheap_.allocManual` 从堆上要一个 span。**2KB 之所以够**，是因为函数调用会触发栈增长，缺了再给：

```
        每个函数的序言（编译器自动生成）
        ┌─────────────────────────────────────────────┐
        │  CMPQ  SP, 16(R14)   ; R14=g, g+16=stackguard0
        │  JLS   morestack     ; SP 低于阈值 → 进 morestack
        └─────────────────────────────────────────────┘
```

`morestack` 是 NOSPLIT 汇编，做的事只有一件：把当前 PC/SP/BP 存进 `g.sched`/`g.morebuf`，然后 `CALL runtime·newstack`（跑在 g0 栈上）。

#### ② `newstack`：必要时把栈翻倍

```go
// runtime/stack.go（删减）
func newstack() {
	thisg := getg()          // 此刻在 g0 上
	gp := thisg.m.curg       // 真正缺栈的用户 g

	stackguard0 := atomic.Loaduintptr(&gp.stackguard0)
	// ★ 抢占也走这条路：stackguard0 被设成魔法值 stackPreempt 时，
	//   morestack 进来就是为了抢占，而不是为了增长栈
	preempt := stackguard0 == stackPreempt
	...
	// 计算需要多少栈：newsize = oldsize * 2（并向上取整到 2 的幂）
	oldsize := gp.stack.hi - gp.stack.lo
	newsize := oldsize * 2
	if newsize > maxstacksize {
		print("runtime: goroutine stack exceeds ", maxstacksize, "-byte limit\n")
		throw("stack overflow")     // 这就是你看到的 fatal error: stack overflow
	}
	copystack(gp, newsize)          // 关键：整块搬家 + 指针重写
}
```

> 🩸 `fatal error: stack overflow` 不是 panic，**没法 recover**。它有两个常见来路：无限递归（真溢出），以及一个 `//go:noinline` 的递归函数把栈顶到 1GB（`maxstacksize`，64 位上是 1GB，32 位上是 250MB 左右，具体以 `$GOROOT/src` 为准）。

#### ③ `copystack`：把栈搬到新地址，还要改栈里的指针

这是 Go 与 JVM 最本质的差异。栈搬了，栈上那些指向栈内的指针（`&x` 传出去的、`unsafe.Pointer` 除外）必须全部 +delta 修正：

```go
// runtime/stack.go（删减）
func copystack(gp *g, newsize uintptr) {
	old := gp.stack
	used := old.hi - gp.sched.sp

	new := stackalloc(uint32(newsize))  // 新栈

	// ① 先把旧栈状态标成"搬家ing"，此时任何指向这块栈的 sudog 都必须已清理
	casgstatus(gp, _Grunning, _Gcopystack)

	// ② 调整栈内指针的偏移量：所有落在 [old.lo, old.hi) 的指针整体平移
	adjust := func(ptr uintptr) uintptr {
		if ptr != 0 && old.lo <= ptr && ptr < old.hi {
			return ptr + (new.lo - old.lo)   // ← 栈内指针重定位
		}
		return ptr
	}
	// 逐帧扫描（用栈帧元数据 stackmap 判断哪个 slot 是指针）
	for sp := new.hi - used; sp < new.hi; {
		frame := (*stkframe)(noescape(unsafe.Pointer(sp)))
		adjustframe(frame)   // 内部对每个指针 slot 调 adjust
		sp += frame.argBytes ...
	}
	// ③ memmove 搬数据、改 gp.stack、更新 stackguard0、修正 gp.sched.sp
	...
	casgstatus(gp, _Gcopystack, _Grunning)
	gogo(&gp.sched)   // 回到用户代码，从"新的栈地址"继续跑
}
```

```
        旧栈 4KB                          新栈 8KB
  ┌──────────────────────┐          ┌──────────────────────────┐
  │ ...空闲...            │          │ ...空闲...                │
  ├──────────────────────┤          ├──────────────────────────┤
  │ frame: main          │          │ frame: main'             │
  │   p → 0xc00004a120 ──┼──┐       │   p → 0xc00004a120+Δ ──┐  │
  ├──────────────────────┤  │ copy  ├────────────────────────┤ │ │
  │ frame: f             │  │ ────▶ │ frame: f'              │ │ │
  │   q → 0xc00004a1f0 ──┼──┼──┐    │   q → 0xc00004a1f0+Δ ──┼─┼─┘ │
  ├──────────────────────┤  │  │    ├────────────────────────┤ │   │
  │ frame: morestack     │  │  └───▶│ frame: morestack'      │ │   │
  └──────────────────────┴──┴───────┴────────────────────────┴─┴───┘
        ▲ SP（旧）                          ▲ SP'（新，Δ 已加）
```

> ⚠️ **这就是为什么 URL 里的 "Go 栈会增长" 不能等同于 C 的 `alloca`**：C 的栈只能原地长；Go 敢搬家，是因为**编译器为每一帧都记了精确的指针位图（`stackmap`）**。手工 `unsafe` 藏起来的栈指针（`uintptr`）搬完家就失效 —— 这是第 16 章那条 `unsafe.Pointer` 铁律的底层原因。
>
> 更狠的一条：channel 的 `sudog.elem` 可能**指向发送方的栈**（无缓冲 channel 直接移交，见第 20 章）。所以栈搬家的窗口期里，绝不允许有 sudog 指着我 —— 这就是 `g.parkingOnChan` 和 `g.activeStackChans` 两个字段存在的全部理由。

#### ④ `shrinkstack`：GC 顺手把栈收回去

```go
// runtime/stack.go（删减）
func shrinkstack(gp *g) {
	oldsize := gp.stack.hi - gp.stack.lo
	newsize := oldsize / 2
	if newsize < _FixedStack {
		return // 已经很小了，不缩
	}
	// 用了不到 1/4 才值得搬
	if used := gp.stack.hi - gp.sched.sp + stackNosplit; used >= (gp.stack.hi-gp.stack.lo)/4 {
		return
	}
	copystack(gp, newsize)
}
```

谁调它？GC 在标记阶段对**停着的** goroutine 调（`scanstack` 之后标记 `gcscandone`），下一次 GC 时如果它还是闲着就缩一半。所以：

```go
func hot() { var buf [4096]byte; _ = buf } // 深栈跑过一次，栈涨到 8KB
// 之后 goroutine 空闲 → 两次 GC 后缩回 2KB
```

#### ⑤ 一张表收掉这一节

| 函数 | 文件 | 触发条件 | 干什么 | 事故面 |
| --- | --- | --- | --- | --- |
| `stackalloc` | `stack.go` | 新建 g / 增长栈 | 从 span 池切栈内存 | 池耗尽时会找 mheap，别再往上叠 |
| `morestack` | `asm_amd64.s` | 序言 `CMPQ SP, 16(R14)` 不满足 | 保存上下文、进 `newstack` | 也是**协作抢占**的入口（第 19 章） |
| `newstack` | `stack.go` | 栈不够 / `stackguard0==stackPreempt` | 翻倍 or 抢占 | `stack overflow` 就从这 throw 出去 |
| `copystack` | `stack.go` | 需要增长/收缩 | 搬家 + 重写栈内指针 | 有 `uintptr` 藏指针时立刻出事 |
| `shrinkstack` | `stack.go` | GC 发现栈利用率 < 1/4 | 缩一半 | 与 `parkingOnChan` 冲突会踩内存 |
| `stackguard0` | 字段 | —— | 溢出阈值 / 抢占哨兵 | 用户改不得，也不该假设其值 |

---

### 17.3.6 「泄漏」在 runtime 眼里到底卡在哪个状态

答案：**绝大多数卡在 `_Gwaiting`**，少数卡在 `_Gsyscall`（阻塞在不可 poll 的系统调用上）或 `_Grunnable`（有人在疯狂造 g 但跑不过来）。`_Gwaiting` 的 `waitreason` 会诚实地告诉你它挂在哪：

| `waitreason`（`runtime/runtime2.go`） | 卡在 | 常见成因 | 处置 |
| --- | --- | --- | --- |
| `waitReasonChanReceive` | channel 收 | 生产者没来 / 上游崩了 | 上游 `close` 或 `ctx` |
| `waitReasonChanSend` | channel 发 | 无人接收 / 缓冲区满 | 缓冲 + 消费者池（第 20 章） |
| `waitReasonSelect` | `select` | 所有 case 都不可能触发 | 加 `ctx.Done()` / 超时（第 21 章） |
| `waitReasonIOWait` | netpoll | 连接不关、`Body` 不 Close | 第 38 章 |
| `waitReasonSyncMutexLock` | `sync.Mutex` | 有人持锁不放 | 第 22 章 |
| `waitReasonSleep` | timer | 长 Sleep 没被取消 | 换 `select + ctx` |
| `waitReasonSelectNoEvents` | 空 select | 冻结调试 | —— |

一个能直接跑的小实验，把"泄漏"和"状态 + 原因"绑在一起看：

```go
// go run main.go；然后看 runtime.Stack 的输出
package main

import (
	"fmt"
	"net/http"
	_ "net/http/pprof"
	"runtime"
	"strings"
	"time"
)

func dump(tag string) {
	buf := make([]byte, 1<<20)
	n := runtime.Stack(buf, true) // true = 打印所有 goroutine
	var hdrs []string
	for _, line := range strings.Split(string(buf[:n]), "\n") {
		if strings.HasPrefix(line, "goroutine ") { // 首行 = goid + 状态 + 原因 + 时长
			hdrs = append(hdrs, line)
		}
	}
	fmt.Printf("== %s | goroutines=%d\n%s\n", tag, runtime.NumGoroutine(), strings.Join(hdrs, "\n"))
}

func main() {
	go http.ListenAndServe("127.0.0.1:6060", nil) // /debug/pprof/goroutine?debug=2
	dump("baseline")

	// ① 无退出条件的等待
	for i := 0; i < 5; i++ {
		go func() { <-make(chan int) }() // 卡在 _Gwaiting / chan receive
	}
	// ② 无缓冲 channel 上没人接收的发送
	ch := make(chan int)
	for i := 0; i < 3; i++ {
		go func() { ch <- 1 }() // 卡在 _Gwaiting / chan send
	}
	// ③ 对照组：靠 close 正常退出的 worker
	done := make(chan struct{})
	go func() { <-done }()
	close(done)

	time.Sleep(200 * time.Millisecond)
	dump("after")
}
```

输出会逼近这样：

```
== after | goroutines=11
goroutine 1 [IO wait]:            ← pprof server 在 epoll_wait
goroutine 7 [chan receive]:       ← <-make(chan int)
goroutine 8 [chan receive]:
goroutine 9 [chan send]:          ← ch <- 1
goroutine 10 [chan send]:
```

**拿到这份输出，你就有了 Go 版 `jstack`。** 三板斧记住：中括号里的「状态 + 原因 + 时长」→ `created by ...` 找谁生的 → 业务函数行号定位。

> 💡 生产三件套：
> 1. `go_goroutines` 指标（`client_golang` 默认 collector 自带）做**斜率告警**，别只看绝对值；
> 2. 测试里用 `go.uber.org/goleak` 断言"用例结束后 goroutine 归零"；
> 3. `GODEBUG=schedtrace=1000` 每秒打一行调度器快照（`gomaxprocs`/`idleprocs`/`threads`/`spinningthreads`/`runqueue`/`[P runq 长度]`），一眼分出"g 太多"还是"活太少"。

---

## 17.4 Java 对照：Thread、虚拟线程与 Continuation

| 维度 | Java 平台线程 | Java 虚拟线程（21+） | Go goroutine |
| --- | --- | --- | --- |
| 栈 | OS 栈，`-Xss` 定量（默认 512KB~1MB），**不可增长、不可移动** | 堆上的 `StackChunk` 链，按需增长，**不可移动**（冻结/解冻，不是拷贝） | 2KB 起，可增长**且可移动**（`copystack` 重写栈内指针） |
| 调度者 | 内核 CFS | `ForkJoinPool` carrier（用户态） | GMP（用户态，第 18 章） |
| 切换成本 | 系统调用级（µs） | mount/unmount，百 ns 级 | `gogo` 恢复寄存器 + `JMP`，几十 ns 级 |
| 阻塞 IO | 占住 OS 线程 | `park`（socket 已适配），遇 `synchronized`/native 帧会 **pin 住 carrier** | netpoller：`gopark` + M 解绑，P 立刻被别人用（第 19 章） |
| 抢占 | 内核时间片 | 只在 park/yield 处让出，长循环会霸占 carrier | 1.14 起信号异步抢占（`SIGURG` + `asyncPreempt`） |
| 元数据 | `Thread#getName/priority/daemon` | 同 | 只有 `goid`，且**故意不暴露**；无优先级、无名字、无 daemon |
| 生命周期控制 | `interrupt` | `interrupt` | 只能靠 `context` 协作取消；`g` 无法强杀 |
| 泄漏排查 | `jstack` | `jcmd Thread.dump_to_file` | `pprof goroutine?debug=2`、`runtime.Stack(buf,true)`、`go_goroutines` |
| 复用 | 线程对象不复用 | 虚拟线程对象可被 GC | `g` 与栈由 `gFree` 池复用（`gfget`/`gfput`） |

三条**会用 Java 直觉猜错**的地方：

1. **"栈会增长"在 Go 里意味着"栈会被搬走"。** 任何把栈地址存成 `uintptr` 或交给 C 的代码，栈一涨就悬空。JVM 没这问题，因为栈永不移位 —— 这也正是 Loom 选 chunk 链表、不做整块拷贝的原因（栈帧不可重定位）。
2. **Go 没有 `Thread.interrupt` 的等价物。** 你不能"打断"一个 goroutine，只能让它自己监听 `ctx.Done()`。所以在 Go 服务里，`context` 不是便利工具，是**唯一的中断协议**（第 24 章）。
3. **goroutine 不是"轻量线程"，是"被调度的函数调用"。** `runtime.gopark` 出现在栈上说明它被 runtime 接手了，而不是被 OS 挂起 —— 这是理解第 19 章 netpoll 的前提。

> 🎯 **Go 为什么这样选**：要让普通工程师随手写 10 万并发，每单位成本必须压到「几百字节 + 几十纳秒切换」。代价是栈必须能搬、必须能抢占、`g` 全部暴露在 runtime 里（没有 JVM 那样的黑盒边界）。这三笔复杂度 Go 全自己扛了，一点没留给业务代码。

---

## 17.5 框架与工程落地

`g`/栈这些底层细节，在开源框架里最常以"三件事"出现：**每连接一个 goroutine、每任务一个 goroutine、出问题靠 goroutine profile 收尸**。

| 场景 | 框架/库里的样子 | 背后的 g 行为 |
| --- | --- | --- |
| HTTP 请求隔离 | `net/http.(*Server).Serve` 每 accept 一个连接就 `go c.serve(connCtx)` | 每连接一个 g，2KB 起步；慢请求多则 g 数量 = 并发连接数 |
| 单请求内并行工具调用 | `errgroup.Group` / `sourcegraph/conc` 的 `pool` | 一次 `go` 一个 g，`gopc` 指向 `errgroup.go` 的 `Go` 方法，pprof 里一眼认出 |
| 流式 token 转发（SSE） | `langchaingo`/`eino` 的生产者 g + 消费者 handler g | 两侧都可能在 `_Gwaiting`（chan send / chan receive），是最典型的成对泄漏点 |
| 后台常驻任务 | `kratos` 的 `Server`/`Task`、`Gin` 的 cron 式 goroutine | 通常被 `sync.WaitGroup` + 优雅关停看住，见第 52 章 |
| 测试 | `go.uber.org/goleak` | 直接读 `runtime.Stack` 比对前后差异 |
| 指标 | `client_golang` 的 `go_goroutines` / `go_threads` | `go_threads` = M 数量；它暴涨说明有大量阻塞系统调用（第 19 章） |

一个真实经验值：**`go_goroutines` 应与「并发请求数 × 每请求 goroutine 数」同阶**。如果你的数量是它的 10 倍且只涨不跌，不用怀疑，就是泄漏；用 `goroutine?debug=2` 按 `created by` 聚合一下（`sort | uniq -c` 那种），三分钟能定位到具体分支。

写 goroutine 时的三条硬规矩（可以抄进 CR 清单）：

```go
// ① 谁创建，谁保证退出：ctx 或 done 永远要有一个
go func() {
	defer wg.Done()
	select {
	case <-ctx.Done():
	case r := <-results:
		handle(r)
	}
}()

// ② 无缓冲 channel 的发送方，必须确保接收方"一定会来"或者被取消
//    否则发送方永久 _Gwaiting
select {
case ch <- v:
case <-ctx.Done():
}

// ③ 长期活的 goroutine 不要吃大栈：一旦跑过深调用栈，栈会维持在较大尺寸
//    直到 shrinkstack 生效；热路径注意别把大数组放到栈上
```

> ⚠️ 一个反直觉的性能点：`go f()` 本身约几十纳秒，但**新 goroutine 第一次被执行**会有额外成本（可能要 `malg` 分配 2KB 栈 + 进 `runnext` 抢占当前时间片）。所以"每次循环里 `go` 一次"在 QPS 十万级的路径上是真瓶颈 —— 该用有界 worker pool（第 26 章）。

---

## 17.6 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| `go_goroutines` 只涨不跌 | 某处 `go` 的 g 卡在 `_Gwaiting`，无人唤醒 | 每个 `go` 都配 `ctx`/`close`/超时；测试加 `goleak` | 25 章 |
| pprof 里一堆 `[chan send, N minutes]` | 无缓冲 channel 发送方多于接收方 | 有缓冲 + 消费者池 + `select ctx.Done()` | 20 章 |
| `fatal error: stack overflow` | 无终止递归 / 巨大栈帧 | 改迭代、加深度上限；用 `//go:noinline` 复现后二分 | 本章 |
| 崩溃时 `traceback` 里出现 `?` 帧 | 异步抢占停在无精确指针的帧（`asyncSafePoint`） | 正常现象；升级 runtime、用 `GODEBUG=asyncpreemptoff=1` 复现对照 | 19 章 |
| `fatal error: found bad pointer in Go heap` | 用 `uintptr` 藏了栈指针，`copystack` 后悬空 | 只在同一表达式里转换，绝不跨语句保存 | 16 章 |
| 内存涨但 heap profile 不高 | goroutine 栈（2KB/个）+ `g` 结构体是独立开销 | 用 `goroutine` profile 数量估算：数量 × 2KB 起 | 本章 |
| `runtime.Stack` 里全是 `[IO wait]` | 连接未关 / `resp.Body` 未 Close | `defer resp.Body.Close()` + `Transport` 复用 | 38 章 |
| `LockOSThread` 后 goroutine 数暴涨 | 被锁的 M 不能跑别人，`sysmon` 会另外起 M | 只在 cgo / TLS 场景用，用完 `UnlockOSThread` | 19 章 |

---

## 17.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| `type g struct` | goroutine 就是「结构体 + 栈 + 状态」，约 400B，堆上的普通对象 |
| `stack` / `stackguard0` | 栈区间 + 溢出阈值；被抢占时 `stackguard0 = stackPreempt` |
| `sched`（`gobuf`） | 7 个机器字的寄存器快照，切走/切回的唯依据 |
| `g0` | 每 M 一个的"值班室"栈，`schedule`/`newstack`/GC 都跑在上面 |
| `gsignal` | 每 M 一个的信号栈，`SIGURG` 抢占也在这里处理 |
| `m0` | 进程第一个 M，静态变量，`main` 就跑在它的 `curg` 上 |
| `go f()` | `newproc` → `newproc1` → `gostartcallfn` → 进 `runnext`；执行时 `gogo` 切栈 |
| 返回地址 | 新 g 的 `sched.pc = goexit`，所以函数 return 后回到 runtime，不是回到调用者 |
| 状态机 | `_Gidle`→`_Grunnable`→`_Grunning`→(`_Gwaiting`/`_Gsyscall`/`_Gcopystack`/`_Gpreempted`)→`_Gdead` |
| 栈增长 | 序言检测 → `morestack` → `newstack` → `copystack`（翻倍 + 重写栈内指针） |
| 栈收缩 | GC 发现利用率 < 1/4 时 `shrinkstack` 减半 |
| 泄漏本质 | 卡在 `_Gwaiting` + 某个 `waitreason`，永远等不到唤醒 |
| vs Java 线程 | Java 栈不可移动；Go 栈可搬家，所以必须精确栈图 + 安全点 |

---

## 17.8 自检题

1. **`go f()` 之后，`f` 里面 `return` 会回到哪里？为什么 `f` 的返回值必须被丢弃？**
   > 要点：`newproc1` 把 `newg.sched.pc` 设成 `runtime.goexit + PCQuantum`，`gostartcall` 又把这个 `goexit` 地址当"返回地址"压在栈上，所以 `f` 的 `RET` 落到 `goexit` → `goexit1` → `mcall(goexit0)` → `gfput`（回收 g 和栈）→ `schedule()`。返回值的接收位置（调用者的栈帧）根本不存在，所以语言层面直接禁止 `go` 带返回值。

2. **一个 goroutine 里 `var a int; p := &a; ...` 深调用导致栈从 2KB 长到 8KB，`p` 还正确吗？如果我把 `a` 的地址存成 `uintptr` 呢？**
   > 要点：`copystack` 会按 `stackmap` 逐帧把栈内指针 +Δ 重定位，`p` 仍有效（对用户透明）。而 `uintptr` 不是指针，GC/栈拷贝不认它，搬家后立刻悬空 —— 这就是 `unsafe.Pointer` 三规则里"不要跨语句保存 `uintptr`"的物理原因。

3. **线上 `go_goroutines` 从 300 涨到 18000 不回落，给你 10 分钟，你怎么在 Go 里定位？说出你的前三条命令/动作。**
   > 要点：① `curl :6060/debug/pprof/goroutine?debug=2`，按首行 `[状态, 原因, 时长]` 与 `created by` 聚合，找出数量最多的那一类；② 看 `waitreason`：`chan receive/send` 指向缺失的 `close`/消费者，`IO wait` 指向泄漏连接，`sync.Mutex` 指向锁持有者；③ 对照 `GODEBUG=schedtrace=1000` 与 `go_threads`，判断是"g 太多"还是"M 被阻塞"，再回到代码定位那一行（`gopc` 给出的 `created by` 行号往往就是根因所在）。

---

## 17.9 延伸阅读

- `$GOROOT/src/runtime/runtime2.go`：`type g struct`、`type stack`、`type gobuf`、`_G*` 状态常量、`waitReason`
- `$GOROOT/src/runtime/proc.go`：`newproc` / `newproc1` / `goexit0` / `gfget` / `gfput`
- `$GOROOT/src/runtime/stack.go`：`stackalloc` / `newstack` / `copystack` / `shrinkstack` / `gostartcallfn`
- `$GOROOT/src/runtime/asm_amd64.s`：`gogo` / `morestack` / `mcall` / `systemstack`
- `$GOROOT/src/runtime/panic.go`：`goexit` 与 `_panic{goexit:true}`
- 官方文档：*Go's execution tracer*（`go tool trace` 里 G 状态的来源）
- 演讲：Dmitry Vyukov, *Go scheduler*（`runtime/proc.go` 里那段经典注释的作者）
- 上一章：[16. unsafe 与 cgo 的边界](16-unsafe-cgo.md) ｜ 下一章：[18. GMP 数据结构与 `schedule()` 主循环逐段精读](18-gmp-scheduler-source.md)

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 16 章 unsafe 与 cgo 的边界](16-unsafe-cgo.md)
- 下一章：[第 18 章 GMP 数据结构与 `schedule()` 主循环](18-gmp-scheduler-source.md)
- 相关：[第 19 章 调度时机全景](19-scheduler-runtime.md)、[第 20 章 channel 原理](20-channel-internals.md)、[第 25 章 并发陷阱](25-concurrency-pitfalls.md)、[第 28 章 GC 与栈扫描](28-gc.md)
