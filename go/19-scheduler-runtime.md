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

<!--TAIL-->