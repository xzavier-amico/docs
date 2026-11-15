# 19. 调度时机全景：抢占、syscall、netpoll、sysmon 与 timer

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 并发与调度器源码 ｜ **编号**：19 / 54
> **本章定位**：前两章讲清了「goroutine 长什么样」和「调度器怎么找工作」。本章补上最后一块拼图：**一个正在跑的 goroutine，到底在什么时刻被迫交出 CPU？** 答案是五条互不相同的路径（抢占、栈增长、系统调用、阻塞原语、timer），每条路上都有独立的源码。读完你要能做本章最后那道 200ms 尾延迟的归因题。
> **读法**：重点看 19.3.2（异步抢占）和 19.3.4（netpoll）—— 这两块是 Go 与 Java 差别最大、也最容易被误解的地方。源码均为删减版，**以本地 `$GOROOT/src` 为准**。

---

## 19.1 引子：P99 很漂亮，P99.9 却稳定在 200ms

线上一个 Agent 对话服务，指标长这样：

```
P50    =  80ms
P99    = 180ms
P99.9  = 210ms   ← 2 小时不变，像一根横线
max    = 380ms
container_cpu_cfs_throttled_periods_total  → 在爬
go_goroutines = 1200（稳定）
```

P99.9 是"卡在整 200ms 附近不上不下"的形状，特别值得警惕：**这不是业务逻辑慢，是有东西在按固定节拍卡你**。三个候选嫌疑：

1. `GC` 的 stop-the-world（现代 Go 通常 < 1ms，排除）；
2. `sysmon` 的 `forcePreemptNS = 10ms`（会有 10ms 量级的尾巴，对不上 200ms）；
3. **cgroup CFS 限流**：`cpu.max` 的周期默认 100ms，配额用完就被 throttle 到下一个周期 —— **200ms ≈ 连续被 throttle 两个周期**。

追下去的方式，本章 19.6 会完整走一遍：`go tool trace` 看 G 的 `Runnable → Running` 间隙 → 发现所有延迟都发生在"goroutine 已经 ready 但没被调度"上 → 再看 P 轨道是空白的 → 最后在容器指标里抓到 `nr_throttled`。**这是调度器视角才能解释的现象**：CPU 明明没跑满，为什么我的 goroutine 排不上队？因为 CPU 配额被抢走了。

要把这件事讲清楚，必须从「谁在什么时候把 CPU 从 goroutine 手里拿走」讲起。

---

## 19.2 直觉模型：Go 有五条「交出 CPU」的路

Java 程序员的直觉是「时间片用完就被内核切走」。Go 不是这样。**Go 的 goroutine 在用户态运行，内核根本不知道它存在**，所以"抢占"必须是 runtime 自己完成的。全部出让时机只有下面五条：

```
   ① 函数序言检查（最频繁，也是最"便宜"的协作点）
       CMPQ SP, g.stackguard0  ──不足──▶ morestack ──▶ newstack ──▶ 顺便抢占/涨栈
                                        ▲
   ② 阻塞原语（channel / mutex / select / sleep / netpoll）
       gopark(...) ──▶ mcall(park_m) ──▶ _Gwaiting ──▶ schedule()

   ③ 系统调用（阻塞型）
       entersyscall() ──▶ P 交给别人（handoffp） ──▶ 线程自己阻塞在内核里
       exitsyscall()  ──▶ 抢回 oldp，抢不到就排队等 P

   ④ GC 需要你停一下（安全点）
       preemptM(myM) ──▶ 发 SIGURG ──▶ 此刻正好在跑？就在下一条指令边界插一个 asyncPreempt

   ⑤ 主动让出
       runtime.Gosched() / goexit / 栈搬家 copystack / GC assist
```

| 出让路径 | 谁发起 | 谁执行 | 延迟量级 | 关键源码 |
| --- | --- | --- | --- | --- |
| 协作式抢占（序言 + 抢占哨兵） | `sysmon` / `preemptone` | 被抢占的 goroutine 自己 | 取决于"多久调用一次函数" | `proc.go:preemptone`、`stack.go:newstack` |
| 异步抢占（信号） | `sysmon` / GC `suspendG` | runtime 的信号处理 | 微秒~毫秒级（要等信号送达） | `preempt.go:preemptM`、`signal_unix.go:doSigPreempt` |
| 阻塞原语 | 用户代码 | 自己（`gopark`） | 立即 | `proc.go:gopark` |
| 系统调用 | 用户代码 | runtime 解绑 P | 立即（P 侧无延迟） | `proc.go:entersyscall` |
| timer | 时间到了 | `checkTimers` + `ready` | 受 `sysmon` 睡眠粒度影响（20µs~10ms） | `time.go:checkTimers` |

> 💡 **一句话记住**：Go 的调度是「**协作 + 信号兜底**」。90% 的抢占机会来自 ①（函数调用）；只有当一个 goroutine 长时间不调用任何函数（纯计算循环）时，才需要 ② 之外的"暴力手段"——信号。历史事故就出在这里。

---

## 19.3 底层实现

### 19.3.1 协作式抢占：1.13 及以前的世界，以及那次著名事故

Go 1.13 之前，抢占**只发生在函数序言**（`morestack` 那个检查点）。`sysmon` 发现某个 P 跑太久，就把它的 `stackguard0` 改成 `stackPreempt`（一个不可能出现的魔法值），于是下一次进入函数时，`CMPQ SP, 16(R14)` 一定失败 → `morestack` → `newstack` 发现 `stackguard0 == stackPreempt` → **不涨栈，直接让出**。

```go
// runtime/proc.go（删减）—— 把一个 P 上正在跑的 g 标记为"该让出了"
func preemptone(pp *p) bool {
	mp := pp.m.ptr()
	if mp == nil || mp == getg().m {
		return false
	}
	gp := mp.curg
	if gp == nil || gp == mp.g0 {
		return false
	}

	gp.preempt = true
	// ★ 关键：把溢出阈值换成魔法值，函数序言一检查就必然触发 morestack
	gp.stackguard0 = stackPreempt

	// 请求对它的栈做异步 preemption（1.14+ 的异步抢占就挂在这里）
	if preemptMSupported && debug.asyncpreemptoff == 0 {
		pp.preempt = true
		preemptM(mp)
	}
	return true
}
```

```go
// runtime/stack.go（删减）—— newstack 里处理"这次进来不是要涨栈，而是要抢占"
func newstack() {
	thisg := getg()
	gp := thisg.m.curg

	stackguard0 := atomic.Loaduintptr(&gp.stackguard0)
	preempt := stackguard0 == stackPreempt // ★ 是魔法值 → 抢占，不是爆栈

	// 如果此刻不能抢占（比如在 malloc 里、或 Go 状态是 _Gscanrunning）
	if preempt {
		if !canPreemptM(thisg.m) {
			// 先放过它，把哨兵恢复成正常阈值，让它继续跑到下一个安全点
			gp.stackguard0 = gp.stack.lo + stackGuard
			gogo(&gp.sched) // never return（直接回到用户代码继续跑）
		}
	}
	// ... 接下来：算需要的栈大小；若 preempt 则走 gopreempt_m/goyield 让出；
	//     否则 copystack(gp, newsize) 增长
}
```

**事故长什么样**（Go ≤ 1.13 的经典 bug，issue #10958 / #24543 讨论的就是它）：

```go
// 这个 goroutine 在 1.13 上能把整个进程的 GC 卡死
func spin() {
	for i := 0; i < 1<<62; i++ { // 纯计算循环，一次函数调用都没有
		_ = i * i
	}
}
```

`for` 循环内部**没有任何函数调用**，所以永远不会经过序言检查点 → GC 想停它也停不下来 → `stop-the-world` 无限期等待 → 整个进程挂住。这不是"慢"，是**GC 完不成**。同一时期的著名现象还有：`select{}` 空转、`cgo` 里长时间不返回、以及"一个 goroutine 卡住导致所有 goroutine 都不动了"。

### 19.3.2 1.14 的答案：基于信号的异步抢占（`SIGURG` + `asyncPreempt`）

Go 1.14 引入了**异步抢占**：当 sysmon/GC 想打断某个 M 上正在跑的 g 时，**给那个 OS 线程发一个信号**，让内核把控制流强行插进用户代码里。

`runtime/preempt.go`：

```go
// 请求抢占 mp 上正在跑的 goroutine。
// 注意：这里只是"挂个请求"，真正的打断由信号 handler 完成。
func preemptM(mp *m) {
	// ...（省略：检查 mp.signalPending、确保 m 不在 GC 停世界状态等）
	if atomic.Cas(&mp.signalPending, 0, 1) {
		// 用 SIGURG 作为"抢占信号"：它默认被忽略，不会干扰用户程序，
		// 而且 Go 自己装箱了 signal handler
		signalM(mp, sigPreempt)
	}
}
```

`sigPreempt` 在 unix 平台就是 `SIGURG`（`runtime/signal_unix.go`：`const sigPreempt = _SIGURG`）。信号送到之后，进入 runtime 的 handler：

```go
// runtime/signal_unix.go（删减）
func doSigPreempt(gp *g, ctxt *sigctxt) {
	// 这个 g 还想被抢占吗？（可能它已经把 preempt 清掉了）
	if wantAsyncPreempt(gp) {
		// ★ 检查"此刻的 PC 是不是异步安全点"
		//   不是安全点（比如在汇编中间、寄存器里存着活指针）→ 放弃这次抢占
		if ok, newpc := isAsyncSafePoint(gp, ctxt.sigpc(), ctxt.sigsp(), ctxt.siglr()); ok {
			// ★ 魔改被中断现场的 PC：让它"返回"到 asyncPreempt 函数
			ctxt.pushCall(abi.FuncPCABI0(asyncPreempt), newpc)
		}
	}
	// 同步 gp.preempt 状态、必要时更新 stackguard0 等（略）
}
```

`asyncPreempt` 是一段**精心手写的汇编**：它把**所有用户寄存器（含 XMM 浮点寄存器）压栈**，再调 `asyncPreempt2`：

```asm
// runtime/asm_amd64.s（大幅删减）
TEXT ·asyncPreempt(SB),NOSPLIT|NOFRAME,$0-0
	PUSHQ	BP
	MOVQ	SP, BP
	SUBQ	$104, SP          // 悬空的空间
	MOVQ	AX, 0(SP)          // 保存通用寄存器
	MOVQ	BX, 8(SP)
	...                        // 保存全部 15 个通用寄存器
	MOVUPS	X0, 80(SP)         // 保存 XMM0-15（浮点/SIMD 也要保）
	...
	CALL	·asyncPreempt2(SB) // 进入 Go 代码做真正的决策
	MOVUPS	80(SP), X0         // 恢复
	...
	RET                       // 回到被中断的那条指令继续跑
```

```go
// runtime/preempt.go（删减）
func asyncPreempt2() {
	gp := getg()
	gp.asyncSafePoint = true
	if gp.preemptStop {
		// GC 想扫我的栈：停下来等它扫完（进入 _Gpreempted）
		mcall(preemptPark)
	} else {
		// 只是"该让别人跑跑了"：把 g 放回队列，重新调度
		gp.preempt = false
		mcall(gopreempt_m)
	}
	gp.asyncSafePoint = false
}

func preemptPark(gp *g) { // 跑在 g0 上
	status := readgstatus(gp)
	if status&^_Gscan != _Grunning {
		throw("bad g status")
	}
	// ★ 状态：_Grunning → _Gpreempted（专门给"被异步抢占停下"用的状态）
	casGToPreemptScan(gp, _Grunning, _Gscan|_Gpreempted)
	...
	casfrom_Gscanstatus(gp, _Gscan|_Gpreempted, _Gpreempted)
	schedule() // 把 CPU 交给别人；GC 扫完后会把 gp 放回队列
}
```

**GC 为什么需要它？** 因为并发标记要扫描每个 goroutine 的栈，而栈只有在**安全点**上才是"可扫描"的（寄存器里的指针已经写回栈、栈帧布局与 `stackmap` 一致）。对于跑飞了的 goroutine，GC 调 `suspendG` 强行把它停住：

```go
// runtime/preempt.go（大幅删减，只留骨架）
func suspendG(gp *g) suspType {
	...
	for i := 0; ; i++ {
		var unsafePoint unsafe.Pointer
		switch s := readgstatus(gp); s {
		case _Gdead:
			return suspendGDead
		case _Gcopystack:
			// 正在搬家，等它搬完
		case _Grunning:
			// 正常跑着：先试同步抢占（设置 preempt / stackguard0），
			// 不行就发信号做异步抢占，然后自旋等它进入安全点
			if preemptMSupported && debug.asyncpreemptoff == 0 && needAsync {
				// ... preemptM(gp.m)
			}
			// 如果停在异步安全点（栈帧里可能有非精确指针），
			// 把 _Gscan 位与状态一起处理（略）
		case _Gwaiting, _Gpreempted:
			// 已经停了：直接把它标成 _Gscan*，GC 可以扫了
			if castogscanstatus(gp, s, s|_Gscan) {
				return suspendGPreempted
			}
		}
		osyield() // 短暂自旋休息
	}
}
```

**为什么需要"异步安全点"这个概念？** 因为信号可能落在**任何**一条指令边界上，包括：

| 位置 | 能否在这里停？ | 原因 |
| --- | --- | --- |
| 普通 Go 函数的指令边界 | ✅ | 有 `stackmap`，栈上指针位置精确可知 |
| 汇编函数内部（如 `memmove` 中途） | ❌ | runtime 用 `FUNCDATA_UnsafePoint` 标注了"不安全"区间 |
| `runtime.duffcopy`、`mallocgc` 的部分区间 | ❌ | 同上，`unsafe.Pointer` 暂时不在栈上 |
| 系统调用返回点、`cgo` | ❌ | 由别的机制处理（`_Gsyscall`） |

这就是 `isAsyncSafePoint` 存在的意义：**信号来了，但不在安全点，就礼貌地放弃这次抢占**（那个 goroutine 会在下一个安全点自己让出）。

> 🎯 **这个设计的取舍**：信号抢占比"编译器在每个循环回边插检查"要**便宜得多**（不增加任何常规路径开销），但代价是：① 抢占是**尽力而为**的（可能失败，靠 sysmon 下次再来）；② 实现极其恐怖（OS 信号 + 汇编 + GC 交互）；③ 带来过一串历史 bug（早期版本偶发 `fatal error: async preempt` 段错误）。Go 团队的选择很明确：**把复杂度留在 runtime 里，让用户代码零成本。**

### 19.3.3 系统调用：M 与 P 的解绑（这是 Go 扛住阻塞 IO 的核心）

Java 程序员最容易误解的一点：**Go 里"阻塞"和"霸占线程"不是一回事**。当一个 goroutine 进入阻塞系统调用时，runtime 会把**它的 P 交给别人**，只留下那个 M 陪着内核慢慢等。

```
 时间轴 ──────────────────────────────────────────────────────────────────────▶

 G1(跑 Go 代码) ──进入 read()──▶ 在 _Gsyscall 里阻塞 ──────────────▶ 返回 ──▶ 继续
    │                                                                  ▲
    │ entersyscall:  pp.status = _Psyscall, m.p = nil                   │
    │                m.oldp = pp（记住原来是哪个 P）                     │ exitsyscallfast:
    ▼                                                                  │ 抢回 oldp
  P0 立刻被交给 M2（handoffp→startm）                                     │ 抢不到 →
    │                                                                  │ 进 pidle 排队
    ▼                                                                  │
 G2(用同一个 P0 跑，完全不知道有人阻塞过) ────────────────────────────────┘
```

```go
// runtime/proc.go（删减）
func entersyscall() {
	reentersyscall(getcallerpc(), getcallersp())
}

func reentersyscall(pc, sp uintptr) {
	gp := getg()
	// 必须先关抢占：接下来的状态对 GC 来说是"不一致"的（sched.sp 还没写好）
	gp.m.locks++
	// 保存现场，供 GC 在 _Gsyscall 状态下扫描这个 goroutine 的栈
	gp.syscallsp = sp
	gp.syscallpc = pc
	gp.m.syscalltick = gp.m.p.ptr().syscalltick

	pp := gp.m.p.ptr()
	pp.m = 0        // ★ P 不再属于这个 M
	gp.m.oldp.set(pp) // 记住这个 P，回来时优先抢它
	gp.m.p = 0      // ★ M 不再持有 P
	atomic.Store(&pp.status, _Psyscall) // ★ P 变成 "syscall" 状态，别人可以接手

	if sched.gcwaiting.Load() {
		// GC 想停世界：告诉它"我已经把 P 交出来了"
		systemstack(entersyscall_gcwait)
	}
	gp.m.locks--
	// 状态迁移：_Grunning → _Gsyscall
	casgstatus(gp, _Grunning, _Gsyscall)
	// ...（GC 相关略）
}

// 系统调用返回
func exitsyscall() {
	gp := getg()
	...
	// ★ 快路径：试着重新拿到 P，拿到就直接继续跑，不回调度器
	if exitsyscallfast(oldp) {
		// ...（处理 stopwait 等）
		return
	}
	// 慢路径：切到 g0，把 g 放回队列，重新调度
	mcall(exitsyscall0)
}

func exitsyscallfast(oldp *p) bool {
	if sched.stopwait == freezeStopWait {
		return false // 世界在冻结，别抢
	}
	// ① 优先抢回自己原来那个 P（缓存还在，最快）
	if oldp != nil && oldp.status == _Psyscall && atomic.Cas(&oldp.status, _Psyscall, _Pidle) {
		wirep(oldp) // m.p = oldp; oldp.m = m
		return true
	}
	// ② 抢不到就随便找一个空闲 P
	if sched.pidle != 0 {
		var ok bool
		systemstack(func() {
			ok = exitsyscallfast_pidle()
		})
		if ok {
			return true
		}
	}
	return false // ✗ 没有 P：只能排队等（exitsyscall0 里把 g 标记为 _Grunnable 塞队列，M 去睡）
}
```

`sysmon` 的 `retake()` 负责**兜底**：如果有 P 在 `_Psyscall` 状态待太久（默认一个 sysmon tick），或者有 P 上的 goroutine 跑太久（`forcePreemptNS = 10ms`），就把 P 抢回来：

```go
// runtime/proc.go（删减）
func retake(now int64) uint32 {
	n := 0
	lock(&allpLock)
	for i := 0; i < len(allp); i++ {
		pp := allp[i]
		if pp == nil {
			continue
		}
		pd := &pp.sysmontick
		s := pp.status

		// ① 跑太久的 G（源码里叫 "Preempt G if it's running for too long"）
		if s == _Prunning || s == _Psyscall {
			t := int64(pp.schedtick) // 这个 P 有没有发生过新调度？
			if int64(pd.schedtick) != t {
				pd.schedtick = uint32(t)
				pd.schedwhen = now // 没有 → 说明同一个 g 一直在跑
			} else if pd.schedwhen+forcePreemptNS <= now { // 超过 10ms
				preemptone(pp) // ★ 异步抢占入口
			}
		}

		// ② 卡在系统调用里的 P：收回它，交给别人
		if s == _Psyscall {
			t := int64(pp.syscalltick)
			if int64(pd.syscalltick) != t {
				pd.syscalltick = uint32(t)
				pd.syscallwhen = now
				continue
			}
			// 但如果没人等活（本地空、全局空、也没有 idle P），
			// 就别急着抢，可以先等等（最多 10ms）
			if runqempty(pp) && sched.nmspinning.Load()+sched.npidle.Load() > 0 &&
				pd.syscallwhen+10*1000*1000 > now {
				continue
			}
			incidlelocked(-1)
			if atomic.Cas(&pp.status, s, _Pidle) { // 抢回 P
				...
				n++
				handoffp(pp) // ★ 把这个 P 交给一个（新的或空闲的）M
			}
			incidlelocked(1)
		}
	}
	unlock(&allpLock)
	return uint32(n)
}
```

> 🩸 **排障口诀**：`threads` 远大于 `GOMAXPROCS` 且 `idleprocs=0` → 有一堆 M 卡在 `_Gsyscall`。这时 `pprof -o goroutine` 里能看到 `[syscall, N minutes]` 的 goroutine；而如果它们永远回不来（比如读一个不会来的数据），那就是**真正的 goroutine 泄漏 + 线程泄漏**，比普通泄漏更致命（线程是 OS 资源）。第 25 章有完整案例。

### 19.3.4 netpoller：让"阻塞 IO"看起来阻塞，实际不占 M

**这是 Go 并发模型最漂亮的一处工程设计。** 你写的代码是：

```go
n, err := conn.Read(buf) // 看起来就是阻塞读，和 Java 的 InputStream.read 一样
```

但底下发生的是：

```
 你的 goroutine                  runtime                      内核
 ──────────────────────────────────────────────────────────────────────────────
 conn.Read(buf)
   └─ internal/poll.(*FD).Read
        └─ syscall.Read(fd) ──▶ 返回 EAGAIN（非阻塞 fd，没数据）
             └─ fd.pd.waitRead()
                  └─ consequential: runtime.netpollblock(pd, 'r', true)
                       ├ 把 pd.rg 从 0 CAS 成 pdWait
                       └ gopark(netpollblockcommit, &pd.rg,
                                waitReasonIOWait, ...)   ← M 在这儿被释放！
                             │
                             ▼
                        schedule() 去跑别的 goroutine
                                                        epoll_wait 在别处（或本 M 空闲时）等待
                                                          │
                                        数据到了 ─────────┘
                                          └─ netpollready(&toRun, pd, 'r')
                                               └─ netpollunblock: pd.rg pdWait → pdReady，取出 g
                                                    └─ goready(gp) → runqput → 该 g 变 _Grunnable
```

源码（`runtime/netpoll.go`，删减）：

```go
const (
	pdReady uintptr = 1 // 有事件，且没人等（"令牌"）
	pdWait  uintptr = 2 // 有人正在等
)

type pollDesc struct {
	link *pollDesc // 所在 pollcache 的链
	fd   uintptr   // 绑定的文件描述符

	// 用原子变量存的"等待者"：
	//   0        无人等
	//   pdWait   有 g 正在等
	//   pdReady  事件已就绪
	rg atomic.Uintptr // 读方向
	wg atomic.Uintptr // 写方向

	rseq uintptr // 用来丢弃过期事件（避免 fd 复用时误唤醒）
	rt   timer   // 读操作的 deadline（time.Timer）
	rd   int64   // 读 deadline 时刻
	// ...（写方向同理：wseq/wt/wd）
}

// 在 pollDesc 上阻塞，直到有事件或被 netpollunblock 唤醒
func netpollblock(pd *pollDesc, mode int32, waitio bool) bool {
	gpp := &pd.rg
	if mode == 'w' {
		gpp = &pd.wg
	}

	// ① 自旋把状态设成 pdWait；如果已经是 pdReady 说明事件已到，不用睡
	for {
		if gpp.CompareAndSwap(pdReady, 0) {
			return true // 事件已经就绪，直接返回（这就是"快速路径"）
		}
		if gpp.CompareAndSwap(0, pdWait) {
			break
		}
		osyield()
	}

	// ② 真正睡下去：gopark 会把 M 交还给调度器
	if waitio {
		gopark(netpollblockcommit, unsafe.Pointer(gpp), waitReasonIOWait, traceBlockNet, 5)
	}
	// ③ 醒来后确认：如果状态还是 pdWait（被 close/超时等异常唤醒），返回 false
	return gpp.CompareAndSwap(pdWait, 0)
}

// gopark 的"解锁函数"：正式睡下前，把 pdWait 标成 pdReady，
// 这样如果有人在这期间投递事件，会看到 pdReady 而不是丢失唤醒
func netpollblockcommit(gp *g, gpp unsafe.Pointer) bool {
	r := atomic.Cas((*uint32)(gpp), pdWait, pdReady)
	if r {
		atomic.Xadd(&netpollWaiters, 1) // 记一笔"有人在等 IO"
	}
	return r
}

// 事件到达：把等待者唤醒（由 netpoll 的 epoll 循环调用）
func netpollready(toRun *gList, pd *pollDesc, mode int32) {
	var rg, wg *g
	if mode == 'r' || mode == 'r'+'w' {
		rg = netpollunblock(pd, 'r', true) // pdWait → pdReady，取出 g
	}
	if mode == 'w' || mode == 'r'+'w' {
		wg = netpollunblock(pd, 'w', true)
	}
	if rg != nil {
		toRun.push(rg)
	}
	if wg != nil {
		toRun.push(wg)
	}
}
```

`netpoll` 本身在 Linux 上就是 `epoll_wait`（`runtime/netpoll_epoll.go`）：

```go
// runtime/netpoll_epoll.go（删减）
func netpoll(delay int64) (gList, int32) {
	if epfd == -1 {
		return gList{}, 0
	}
	var waitms int32
	if delay < 0 {
		waitms = -1 // 永久阻塞，直到有别的事件（被 netpollBreak 打断）
	} else if delay == 0 {
		waitms = 0 // 纯轮询
	} else {
		waitms = int32(delay / 1e6) // ns → ms
	}
	var events [128]syscall.EpollEvent
retry:
	n, errno := epollwait(epfd, events[:], waitms)
	if n < 0 {
		if errno == _EINTR {
			goto retry // 被信号打断（比如抢占信号 SIGURG！），重试
		}
		println("runtime: epollwait on fd", epfd, "failed with", errno)
		return gList{}, 0
	}
	var toRun gList
	for i := range events[:n] {
		ev := &events[i]
		var mode int32
		if ev.Events&(_EPOLLIN|_EPOLLRDHUP|_EPOLLHUP|_EPOLLERR) != 0 {
			mode += 'r'
		}
		if ev.Events&(_EPOLLOUT|_EPOLLHUP|_EPOLLERR) != 0 {
			mode += 'w'
		}
		if mode != 0 {
			pd := *(**pollDesc)(unsafe.Pointer(&ev.Data)) // 事件里带着 pollDesc 指针
			netpollready(&toRun, pd, mode)
		}
	}
	return toRun, 0
}
```

三个必须知道的事实：

| 事实 | 含义 | 工程后果 |
| --- | --- | --- |
| 只有**可 poll 的 fd** 才走 netpoller（socket、pipe、eventfd、部分字符设备） | 普通文件（`os.File` 读磁盘）走的是**真·阻塞 syscall** | 读大文件/网络文件系统会占住 M，`threads` 上涨 |
| `netpoll` 是**全局一个** epoll 实例（`netpollinit` 里创建） | 所有 goroutine 的 IO 都挂在同一个 epoll 上 | 一次 `epoll_wait` 可批量返回 128 个事件，效率极高 |
| 有空闲 M 时由它阻塞在 `epoll_wait`；没有则由 `sysmon` 定时 poll | 事件循环不是"专门的线程" | 不需要 Netty 那种 `EventLoopGroup`，第 39 章会对照 |

> ⚠️ `netpollBreak` 这个函数要单独记住：当 timer 到期、或需要唤醒 `epoll_wait` 时，runtime 往一个专门的 `eventfd`/pipe 写一个字节，让阻塞在 `epoll_wait` 的 M 醒过来重新调度。**没有它，`time.Sleep` 就会被 IO 阻塞拖住**。

### 19.3.5 `sysmon`：那个永不睡眠超过 10ms 的守护线程

`sysmon` 是 runtime 启动时就创建的**一个独立 M**（不绑 P，`mstartfn = sysmon`），它的循环有六个职责：

```go
// runtime/proc.go（大幅删减，只是骨架）
func sysmon() {
	lock(&sched.lock)
	sched.nmsys++
	...
	for {
		if delay > 10*1000 { // 上限 10ms，下限 20µs（自适应：越闲睡越久）
			delay = 10 * 1000
		}
		usleep(delay)

		now := nanotime()
		// ① 回收卡在 syscall 里的 P + 抢占跑太久的 G
		if retake(now) != 0 {
			idle = 0
		} else {
			idle++
		}

		// ② 如果两分钟没 GC 且内存在涨，强制触发一次 GC
		if t := (gcTrigger{kind: gcTriggerTime, now: now}); t.test() && atomic.Load(&forcegc.idle) != 0 {
			lock(&forcegc.lock)
			forcegc.idle = 0
			forcegc.g.schedlink = 0
			injectglist(&forcegc.g) // 唤醒负责 GC 的 goroutine
			unlock(&forcegc.lock)
		}

		// ③ 归还空闲内存给 OS（scavenger，第 15、28 章）
		if scavenger.sysmonWake.Load() != 0 {
			scavenger.wake()
		}

		// ④ 给 netpoller 打工：没人等活时自己去 poll 一次 IO
		if netpollinited() && lastpoll != 0 && lastpoll+10*1000*1000 < now {
			sched.lastpoll.CompareAndSwap(lastpoll, now)
			list, _ := netpoll(0)
			if !list.empty() {
				incidlelocked(-1)
				injectglist(&list) // 把就绪的 g 塞进全局队列
				incidlelocked(1)
			}
		}

		// ⑤ 死锁检测：所有 M/P 都空、没人跑，就 throw("all goroutines are asleep")
		//    判断条件大致是 sched.nmidle+... == mcount 之类（略）

		// ⑥ 原子性地"预投递"抢占请求给 spinning M（略）
	}
}
```

| 职责 | 周期 | 出问题的症状 |
| --- | --- | --- |
| `retake`：抢占长跑的 G（`forcePreemptNS = 10ms`） | 每次循环 | P99 里的 10ms 台阶；asyncpreemptoff 打开时会明显变差 |
| `retake`：回收 syscall 里的 P（`handoffp`） | 每次循环 | 阻塞 IO 拖垮并行度（没有它的话） |
| 强制 GC（`forcegcperiod = 2min`） | 2 分钟 | "不分配就不 GC"的进程内存不归还 |
| scavenger 唤醒 | 需要时 | RSS 不降（第 28 章） |
| idle 时 netpoll | 10ms 门限 | 空闲进程的 IO 事件延迟（典型 10ms 级） |
| 死锁检测 | 每次循环 | `fatal error: all goroutines are asleep - deadlock!` |

把 sysmon 的睡眠策略单独说一句，因为它决定了**你看到的时间粒度**：`delay` 从 20µs 起步，连续空闲就翻倍，上限 10ms；一旦有活（`retake` 返回非 0）立刻重置回 20µs。**所以 Go 的空闲唤醒粒度最差 10ms** —— 这解释了为什么"纯 IO 等待 + 无定时器"的进程，事件响应会有最多 10ms 的额外延迟。

### 19.3.6 timer 的演进：从全局大锁到 1.23 的无锁实现

timer 是"调度时机"里最容易被忽视的一环：**`time.Sleep`、`ctx.WithTimeout`、`http.Server` 的超时、`net.Conn` 的 deadline 全部是 timer**。

**Go 1.14 之前**：所有 timer 挂在一个**全局四叉堆**上，配一把全局锁 `timersLock`。十万连接 + 每连接一个读超时，这把锁就是灾难。

**Go 1.14 的改造**：timer 归属到 **P**，每个 P 一个 `p.timers []*timer` 小堆（`siftupTimer`/`siftdownTimer`），只在必要时才碰别的 P 的 timer 锁。检查时机被塞进调度循环：

```go
// runtime/time.go（1.14~1.22，删减）
// 在 findRunnable / stealWork 里被调用；返回值里 pollUntil 决定 netpoll 该睡多久
func checkTimers(pp *p, now int64) (rnow, pollUntil int64, ran bool) {
	// 堆顶 timer 还没到期 → 直接返回（最快路径，不拿锁）
	...
	lock(&pp.timersLock)
	// 把所有"已到期"的 timer 从堆里取出来：modtimer/deltimer 可能改过状态
	// 状态机：timerWaiting / timerRunning / timerDeleted / timerModifiedEarlier / ...
	for {
		t := pp.timers[0]
		if t.when > now {
			break
		}
		// 取堆顶 + 下沉
		...
		f := t.f
		arg := t.arg
		seq := t.seq
		unlock(&pp.timersLock)
		if f != nil {
			f(arg, seq) // 执行回调（通常是 ready 一个 goroutine / 发 channel）
		}
		lock(&pp.timersLock)
	}
	unlock(&pp.timersLock)
	// 还要处理 profile timers、被推迟的 timer、以及算出 pollUntil
	...
}
```

**Go 1.23 的改造**（release notes 里明确写了 timer 实现重写）：

| 变化 | 1.14~1.22 | 1.23+ |
| --- | --- | --- |
| 数据结构 | per-P 最小堆 + `timersLock`（改 timer 仍要锁） | per-P 最小堆，但**常见路径无锁**：timer 的状态用原子变量（`astate`）+ 每 timer 一把小锁 |
| 改/删 timer | 可能要抢 P 的 `timersLock` | 用 atomic 状态机 + `modify`/`delete` 协作，避免大锁 |
| 谁检查 | `checkTimers` 在调度循环里 | 同样的入口（`findRunnable`/`stealWork`），但成本更低 |
| channel 语义 | timer channel 带 1 容量缓冲（异步） | `GODEBUG=asynctimerchan=1` 保持旧行为；默认改为**同步、无缓冲**（1.23 起） |

> 💡 **对你的实际影响**：① 别再用 `time.After` 在循环里造 timer（每次都分配 + 挂堆），用 `time.NewTimer` + `Reset`（详见第 21 章）；② 1.23 之后 `select` 里读 timer channel 的行为变了（不会再有"缓冲里躺着一个过期事件"），有些老代码的"侥幸通过"会开始 flaky；③ 十万级连接的 deadline 场景，1.23+ 的 timer 开销已经不再是热点。
>
> （timer 的具体字段与状态常量名在近几个版本改得比较频繁，以你本地 `$GOROOT/src/runtime/time.go` 为准。）

### 19.3.7 `GOMAXPROCS` 与容器：1.25 的 cgroup 感知

`GOMAXPROCS` 决定 P 的数量 = **同时执行 Go 代码的上限**。三个必须分清的事实：

```
  sched.gomaxprocs  = 8   ← P 的数量，同时也是并行执行 Go 代码的上限
  threads（M 数量） = 20  ← 可以远大于 8：阻塞 syscall / cgo / GC 各自占 M
  CPU limit (cgroup)= 2   ← 真正的物理约束：超出就被 CFS throttle
```

**经典事故**：Pod 的 `limits.cpu = 2`，节点 64 核，Go 1.24 及以前 `GOMAXPROCS` 默认取**宿主机核数 64**。于是 runtime 起了 64 个 P 全力并行 → 瞬间把 2 核配额烧完 → 被 throttle 到下一个 100ms 周期 → **你的服务每隔 100ms 停一次，P99 直接崩**。

**Go 1.25 的修复**：默认 `GOMAXPROCS` 改为**感知 cgroup CPU 带宽限制**（读 `/sys/fs/cgroup/.../cpu.max` 之类），并在限额变化时更新。相关旋钮：

| 开关 | 作用 |
| --- | --- |
| 默认行为（1.25+） | 取容器 CPU limit 与 `NumCPU` 的较小者 |
| `GODEBUG=containermaxprocs=0` | 关闭基于容器的初始值（回到 `NumCPU`） |
| `GODEBUG=updatemaxprocs=0` | 关闭"限额变化时自动更新" |
| `runtime.SetDefaultGOMAXPROCS()` | 手动重算并设置默认值（1.25 新增） |
| `runtime.GOMAXPROCS(n)` | 手动设定，优先级最高 |

> ⚠️ 但请注意：`GOMAXPROCS` 只管**执行 Go 代码的并行度**，**管不了线程总数**。你的服务如果有一堆阻塞 syscall 或 cgo，`threads` 依然可以把 CPU 烧穿配额。所以"`GOMAXPROCS` 对齐 limit"是必要条件，不是充分条件（第 52 章会讲在 K8s 里怎么把 `limits.cpu`、`GOMEMLIMIT`、`GOMAXPROCS` 三者对齐）。

---

## 19.4 Java 对照：Safepoint、interrupt 与 Selector

| 维度 | Java | Go | 会猜错的地方 |
| --- | --- | --- | --- |
| 抢占/暂停机制 | **全局 safepoint**：`Thread` 在安全点轮询（方法返回、循环回边插入 poll），JVM 发起 STW 时等所有线程到达 | **per-G 抢占**：`preemptone` 设哨兵/发 `SIGURG`，逐个 goroutine 处理 | JVM 的 safepoint 是"停下来做全局操作"；Go 只有 GC 的 `suspendG` 需要停，日常抢占是为了**换人跑** |
| 打断线程 | `Thread.interrupt()` 可以唤醒 `sleep`/`wait`/IO | 没有对应物；必须 `ctx` + `select` 协作 | 唯一"强行"的手段是 `panic`/`os.Exit`，都不该用 |
| 阻塞 IO 模型 | 平台线程阻塞；虚拟线程 park（内部 epoll） | goroutine 永远"看起来阻塞"，netpoller 在底下 | Go 的阻塞读写不需要任何"异步改造"，`net/http` 直接每连接一 goroutine |
| IO 事件循环 | `Selector` + `EventLoopGroup`（Netty）手工编排 | runtime 内置一个全局 epoll，`netpollReady` 唤醒 g | 不需要自己写 reactor；但要小心"不可 poll 的 fd" |
| 定时器 | `ScheduledThreadPoolExecutor` / `HashedWheelTimer`，你自己选 | runtime timer（per-P 小堆）+ `time.Timer` | Go 没有"时间轮"这一层需要你操心（1.14 也用过时间轮，1.23 又重写了） |
| 线程数上限 | `-XX:ActiveProcessorCount` / 线程池大小 | `GOMAXPROCS`(P 数) + 无上限 MOS 线程（`maxmcount` 10000） | 调小 `GOMAXPROCS` 不等于限制线程数 |
| CPU 配额感知 | `-XX:ActiveProcessorCount` 需手工设，或 JVM 自动 read cgroup | 1.25 起自动；之前要手动设 `GOMAXPROCS` | 这是两个生态共同的经典事故 |

**最本质的一句对照**：**JVM 的 safepoint 是为了"所有人停下来"，Go 的抢占是为了"一个人让一让"。** 目标不同，实现思路才不同 —— JVM 可以忍受"等所有线程到安全点"（用于 GC/偏向锁撤销/去优化），而 Go 需要"随时换个 goroutine 跑"，于是必须做到**毫秒级、单点、可控**。

> 🎯 **一个有意思的历史巧合**：Go 1.14 的异步抢占（信号 + `pushCall` 改 PC），本质上是"强行把线程推到安全点"，这和 JVM 社区讨论多年的 *async safepoint*（JEP 讨论过、Azul 的 Zing 用类似技术做到几乎零停顿）是同一个思路。**两边最后都走向了"信号 + 改 PC"**，因为纯协作轮询的开销要么加在每次循环（贵），要么让停顿不可控（疼）。

---

## 19.5 框架与工程落地

### ① 一个能跑的实验：单 P 下的"阻塞读"不拖累别人

```go
// GOMAXPROCS=1 强制单 P：如果阻塞读真的霸占线程，下面的 ticker 就会停住
package main

import (
	"fmt"
	"os"
	"runtime"
	"time"
)

func main() {
	runtime.GOMAXPROCS(1)
	fmt.Println("GOMAXPROCS =", runtime.GOMAXPROCS(0))

	pr, pw, _ := os.Pipe()

	// 消费者：阻塞在 Read 上（走 netpoller，因为 pipe 是可 poll 的 fd）
	go func() {
		buf := make([]byte, 16)
		n, err := pr.Read(buf)
		fmt.Printf("[reader] got %q err=%v at %dms\n", buf[:n], err, time.Since(start).Milliseconds())
	}()

	// 心跳：如果 M 被霸占，这个 ticker 会卡住
	go func() {
		for i := 0; i < 5; i++ {
			time.Sleep(50 * time.Millisecond)
			fmt.Printf("[ticker] tick %d at %dms\n", i, time.Since(start).Milliseconds())
		}
	}()

	time.Sleep(300 * time.Millisecond)
	pw.Write([]byte("hello"))
	time.Sleep(300 * time.Millisecond)
}

var start = time.Now()
```

你会看到 `[ticker]` **一路正常打印**，`[reader]` 在 300ms 左右才拿到数据 —— 单 P 下两者依然并发推进。原因就是 19.3.4：`Read` 返回 `EAGAIN` → `netpollblock` → `gopark`，P 被让出来了。

换成**不可 poll 的 fd**（比如读一个 NFS 上的普通文件、或者某些设备），这个实验就会变成"ticker 卡住"，因为那走的是 `entersyscall` 的真阻塞路径，需要靠 `handoffp` 把 P 交出去（也能撑住，但会多一个 M）。

### ② 三个可以直接用的调试开关

| 开关 | 用途 | 什么时候用 |
| --- | --- | --- |
| `GODEBUG=schedtrace=1000` | 每秒一行调度器快照 | 判断"CPU 饱和"还是"饿着"（见 18.1） |
| `GODEBUG=asyncpreemptoff=1` | 关掉异步抢占 | 复现/排除信号抢占相关的崩溃或抖动（**别长期开**，会让 GC 卡住） |
| `GODEBUG=containermaxprocs=0,updatemaxprocs=0` | 关容器感知 | 1.25 上复现旧行为、或你确认要手动控制 |
| `GODEBUG=inittrace=1` | init 阶段耗时 | 排查启动慢（与调度无直接关系，但常一起用） |

### ③ 在框架/标准库里的映射

| 场景 | 背后的调度机制 | 你要注意什么 |
| --- | --- | --- |
| `http.Server` 每连接一 goroutine | 连接空闲 → `Read` 阻塞 → netpoll park | `ReadTimeout`/`IdleTimeout` 生效在 timer 上（第 38 章） |
| `ctx.WithTimeout` | timer + `netpollBreak` | 超时精度受 sysmon 粒度影响（最差 10ms） |
| SSE 长连接（第 39 章） | 每个连接一 g，`Flush` 后阻塞在写/下一次读 | 十万连接 = 十万 g，2KB 起 × 10 万 = 200MB 起步，要算清 |
| `database/sql` 连接池 | 取连接是 mutex + `runtime_Semacquire`（`gopark`） | 池满时 goroutine 卡在 `waitReasonSyncCond`，不是 IO |
| cgo 调用（如推理引擎） | `entersyscall` → P 解绑，但线程被 cgo 占住 | cgo 调用慢 → M 暴涨，`threads` 指标要盯 |

---

## 19.6 归因演练：一次 200ms 尾延迟的完整定位

回到 19.1 的现场。**目标：用四步在 30 分钟内定位。**

### 第 1 步：确认"慢在哪一段"

```bash
# 1) 抓 10 秒 trace（生产上务必限时；1.25+ 可用 FlightRecorder 事后导出）
curl -s -o trace.out 'http://localhost:6060/debug/pprof/trace?seconds=10'

# 2) 看调度延迟分布（runnable → running 的等待）
go tool trace -pprof=sched trace.out > sched.pb.gz
go tool pprof -top sched.pb.gz
```

如果 top 里出现几百毫秒的 `runtime.findRunnable`/`runtime.schedule` 延迟 —— 注意，这**不一定是调度器慢**，而是"**g 已经 ready 却一直没拿到 P**"的时间被算到了这里。

### 第 2 步：看 P 轨道是"空白"还是"满绿"

```bash
go tool trace -http=:8081 trace.out
# 浏览器 → View trace → 观察每个 P 的轨道
```

| 观察结果 | 说明 | 下一步 |
| --- | --- | --- |
| P 轨道**大片空白**，但此时有 goroutine 在 `Runnable` | 有 P 却没在跑 → 线程被挂起 / CPU 被抢走 / CFS throttle | → 第 3 步 |
| P 轨道满绿（都在 Running），Runnable 排队 | 真 CPU 饱和 | 加副本 / 降并发（第 32 章） |
| P 轨道出现规律的"每 100ms 断一次" | **CFS 限流**特征 | → 第 3 步 |
| 大段 `GC` 标记 | 分配过快导致 GC 压力 | 第 28、30 章 |

### 第 3 步：核对容器 CPU 指标

```bash
# cgroup v2
cat /sys/fs/cgroup/cpu.max                  # 例：200000 100000 → 2 核 / 100ms 周期
cat /sys/fs/cgroup/cpu.stat                 # nr_throttled / throttled_usec
# K8s 侧（Prometheus）
# container_cpu_cfs_throttled_periods_total / container_cpu_cfs_periods_total
```

判据：**`throttled_periods / periods > 5%` 就已经在伤业务**；如果还能和 P99.9 的台阶对齐，基本可以定案。

### 第 4 步：验证 GOMAXPROCS 与 limit 的关系

```bash
# 运行时打印（或暴露成 metric）
GODEBUG=schedtrace=1000 ./agent | head -3
```

```
SCHED 1000ms: gomaxprocs=64 idleprocs=3 ...
```

`gomaxprocs=64` + `cpu.max = 2 核` → **典型 32 倍超卖**：64 个 P 全力并行，2 核配额瞬间烧光，剩下的时间全在被 throttle。

### 归因结论与处方

| 层 | 证据 | 结论 |
| --- | --- | --- |
| 现象层 | P99.9 ≈ 200ms 恒定 | 不是业务逻辑问题，是**节拍性**外部限制 |
| trace 层 | P 轨道空白 + goroutine `Runnable` 堆积 | g ready 了但拿不到 CPU |
| 容器层 | `nr_throttled` 与 P99.9 台阶相关 | CFS 限流 |
| 配置层 | `GOMAXPROCS=64` vs `limits.cpu=2` | 超卖 32 倍，主动把限流打满 |

**处方（按优先级）**：

1. 升级到 Go 1.25+ 拿到容器感知 `GOMAXPROCS`；或显式 `GOMAXPROCS=2`（与 limit 对齐）；
2. 提高 `limits.cpu`（或去掉 limit、只留 request）—— 但要有容量规划支撑（第 32 章）；
3. 减少"忙等"：检查是不是有大量 spinning M（`spinningthreads` 高）在偷活，本质是**任务太小 + goroutine 太多**；
4. 若还有残余尾延迟，看 `forcePreemptNS=10ms` 的抢占粒度与 netpoll 的 10ms 唤醒粒度 —— 这两个是 Go 调度的"固有节拍"，只能通过减少长循环/长阻塞来规避。

> 🩸 同类事故变体（都指向同一个根因）：
> - Java 服务迁到 Go，`-XX:ActiveProcessorCount=2` 忘了设 → 同样被 throttle；
> - `GOMAXPROCS` 调成 2 但没设 limit，反而把节点 CPU 打满影响邻居 → 被限流；
> - cgo 推理调用让 `threads` 涨到 200，2 核配额被 200 个线程分 → 每次上下文切换都在抢 CFS 配额。

---

## 19.7 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| P99.9 恒定卡在 100/200ms 台阶 | cgroup CFS 限流；`GOMAXPROCS` 远超 CPU limit | 1.25+ 自动感知，或手动 `GOMAXPROCS=limit` | 52 章 |
| 长循环导致 GC 停顿拉长 | 1.13 及以前无异步抢占 | 升级到 1.14+；循环里偶发 `runtime.Gosched()` 只是权宜 | 本章 |
| `threads` 持续上涨不回落 | 阻塞 syscall / cgo / 不可 poll 的 fd | 让 IO 走 socket/pipe；cgo 调用限流 | 25、32 章 |
| `time.After` 在热循环里 | 每次新建 timer，per-P 堆膨胀 | `time.NewTimer` + `Reset` + `Stop` | 21 章 |
| 超时"不准"，总是多 10ms | netpoll/timer 检查粒度受 sysmon 睡眠上限影响 | 不要依赖 µs 级超时；用批量/流水线降延迟 | 本章 19.3.5 |
| 升级 1.23 后 timer channel 行为变了 | `asynctimerchan` 默认值变化 | 显式检查 `select` 里 timer 分支的语义；必要时设 `GODEBUG=asynctimerchan=1` | 21 章 |
| 用 `SIGURG` 做业务信号 | 与 runtime 抢占信号冲突 | 业务别碰 `SIGURG`；用 `signal.Notify` 前先查文档 | 本章 |
| `deadlock!` 只在全进程无 goroutine 可跑时才报 | sysmon 的死锁检测条件严格 | 有 pprof server / 后台 goroutine 时永远不会报，靠自身超时兜底 | 25 章 |
| `GODEBUG=asyncpreemptoff=1` 长期开 | 长循环/GC 卡住的隐患被重新引入 | 只用于复现问题，尽快关掉 | 本章 |

---

## 19.8 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 五条让出路径 | 序言检查 / 阻塞原语 / 系统调用 / GC 安全点 / 主动让出 |
| `stackguard0 = stackPreempt` | 协作抢占哨兵：下次进函数必触发 `morestack` |
| 1.14 异步抢占 | `preemptM` → `SIGURG` → `doSigPreempt` → `pushCall(asyncPreempt)` |
| 异步安全点 | 信号落在"不安全区间"（汇编/无精确栈图）就放弃，下次再来 |
| `_Gpreempted` | 被异步抢占停下、等 GC 扫栈的专门状态 |
| `entersyscall` | `m.p = nil` + `pp.status = _Psyscall`，P 立刻可被别人用 |
| `exitsyscallfast` | 优先抢回 `oldp`，抢不到就排队（进 `pidle` 或全局队列） |
| `retake` | sysmon 里：10ms 抢跑太久的 G；回收卡死的 syscall P |
| netpoller | `netpollblock` → `gopark` → 事件到了 `netpollready` → `goready` |
| `epoll_wait` | 全局一个 epoll 实例；`EINTR` 重试（抢占信号也会打断它） |
| `netpollBreak` | 用 eventfd 打断 `epoll_wait`（timer/GC 需要调度时） |
| sysmon | 永不超过 10ms 睡眠，6 大职责：抢占、回收 P、强制 GC、scavenge、idle netpoll、死锁检测 |
| timer | 1.14 per-P 最小堆 → 1.23 无锁化；`GODEBUG=asynctimerchan` |
| `GOMAXPROCS` | 只决定 P 数；1.25 起默认容器感知（`cpu.max`） |
| 200ms 台阶 | 十有八九是 CFS 限流，不是 GC、不是调度器 bug |

---

## 19.9 自检题

1. **一个 `for i := 0; i < 1e12; i++ { x += i }` 的死循环 goroutine，在 Go 1.13 和 Go 1.20 上分别会怎样影响 GC？为什么？**
   > 要点：1.13 及以前只有协作抢占（函数序言），循环体内没有函数调用，`stackguard0 = stackPreempt` 永远没机会被检查 → GC 的 `stopTheWorld`/`suspendG` 无限等待 → 整个进程失去响应（`all goroutines are asleep`/直接挂住）。1.14 起 `preemptM` 发 `SIGURG`，信号 handler 用 `isAsyncSafePoint` 确认安全后 `pushCall(asyncPreempt)` 强行插一个调用，`asyncPreempt2` 里 `mcall(preemptPark)` 把 G 停到 `_Gpreempted`，GC 得以继续。

2. **`conn.Read` 阻塞时，M 在干什么？P 在干什么？和 `os.File.Read` 读一个 NFS 文件有什么区别？**
   > 要点：socket 是可 poll 的 fd，`syscall.Read` 返回 `EAGAIN` 后走 `netpollblock` + `gopark`，**M 回到 `schedule()` 继续跑别的 g**，P 一直在这个 M 手里（没有解绑）。而 NFS 文件的读是真正的阻塞 syscall，走 `entersyscall`：`m.p = nil`、`pp.status = _Psyscall`，P 被 `handoffp` 交给别的 M，这个 M 独自在内核里等。差别是前者"零额外线程"，后者"多一个线程但并行度不丢"。

3. **`GOMAXPROCS=64`、`limits.cpu=2`，你的服务 P99.9 稳定在 200ms。请给出你的排查顺序和前三个证据。**
   > 要点：① `go tool trace` 看 P 轨道 → 发现大片空白而 goroutine 在 `Runnable`（排除"CPU 饱和"，锁定"拿不到 CPU"）；② `schedtrace` 看 `gomaxprocs=64` 与容器 `cpu.max=200000 100000` 对比 → 超卖 32 倍；③ `cpu.stat`/`container_cpu_cfs_throttled_periods_total` 上涨且与 P99.9 台阶对齐 → 定案 CFS 限流。处方：1.25+ 容器感知或显式 `GOMAXPROCS=2`，并把 CPU limit 与容量规划对齐。

---

## 19.10 延伸阅读

- `$GOROOT/src/runtime/preempt.go`：`preemptM`、`suspendG`、`asyncPreempt2`、`preemptPark`、`gopreempt_m`
- `$GOROOT/src/runtime/signal_unix.go`：`sigPreempt = _SIGURG`、`doSigPreempt`、`isAsyncSafePoint`、`pushCall`
- `$GOROOT/src/runtime/asm_amd64.s`：`asyncPreempt`（保存全部寄存器的汇编）、`morestack`、`mcall`
- `$GOROOT/src/runtime/netpoll.go` / `netpoll_epoll.go`：`netpollblock`、`netpollready`、`netpoll`、`netpollBreak`
- `$GOROOT/src/runtime/proc.go`：`entersyscall`/`exitsyscall`/`exitsyscallfast`、`sysmon`、`retake`、`handoffp`
- `$GOROOT/src/runtime/time.go`：`timer`、`checkTimers`、`modtimer`/`deltimer`（1.23 前后差异较大）
- issue：golang/go#10958（协作抢占导致 GC 卡死）、#24543（异步抢占提案）
- 演讲：Austin Clements, *Picking the Right Goroutine to Run* / *Go GC and Preemption*
- 上一章：[18. GMP 数据结构与 schedule() 主循环](18-gmp-scheduler-source.md) ｜ 下一章：[20. channel 原理与编排模式](20-channel-internals.md)

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 18 章 GMP 数据结构与 `schedule()` 主循环](18-gmp-scheduler-source.md)
- 下一章：[第 20 章 channel 原理与编排模式](20-channel-internals.md)
- 相关：[第 17 章 goroutine 与栈](17-goroutine-internals.md)、[第 21 章 select 与超时](21-select-timeout.md)、[第 28 章 GC](28-gc.md)、[第 52 章 部署与 SRE](52-deployment-sre.md)