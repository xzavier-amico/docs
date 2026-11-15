# 8. 函数、闭包与 defer/panic 实现

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · 语言内核 ｜ **编号**：8 / 54
> **本章定位**：把 `func` 当成一个**数据结构**来看：函数值是两个字（代码指针 + 环境指针），`defer` 有三条编译路径，`panic` 是一张挂在 `g` 上的链表。读完你就能回答"为什么 Go 的 defer 只要 1ns"。
> **读法**：8.3 是本章核心，建议对着 `$GOROOT/src/runtime/panic.go` 一起看。7.6 讲过 panic 的**使用策略**，这里只讲**机器怎么跑**。

---

## 8.1 引子：三个 defer，三种事故

事故一，内存：

```go
func extractAll(files []string) error {
	for _, f := range files {          // ❌ 1 万个文件
		fh, err := os.Open(f)
		if err != nil {
			return err
		}
		defer fh.Close()               // defer 在"函数"返回时才执行
	}
	return nil
}
```

`defer` 的注册单位是**函数**，不是循环体。这段代码会同时持有 1 万个文件描述符，直到 `extractAll` 返回——而它返回的时候，进程大概已经 `too many open files` 了。

事故二，返回值:

```go
func count() (n int, err error) {
	defer func() { n++ }()   // ✅ 会生效：n 是命名返回值，defer 在 return 之后、真正返回之前跑
	return 10
}

func count2() (int, error) {
	var n int
	defer func() { n++ }()   // ❌ 不生效：n 只是个普通局部变量
	return n, nil
}
```

同样的代码，一个改了返回值一个没改。差别不在 `defer`，在**返回值有没有名字**。

事故三，panic 跑飞:

```go
func main() {
	go worker()      // worker 里 panic 且没有 recover
	time.Sleep(time.Hour)
}
// 输出：panic: boom
//       goroutine 20 [running]: ...
// exit status 2  ← 整个进程没了，不只是这个 goroutine
```

Java 里线程里抛未捕获异常只死一个线程；Go 里**任何 goroutine 的未捕获 panic 都会终止整个进程**。

三起事故指向同一件事：`defer` 和 `panic` 是运行时机制，不是语法糖。要知道它们的边界，就得看它们的实现。

---

## 8.2 直觉模型：函数值是"代码 + 环境"两个字

**图一：一个普通函数值 vs 一个闭包，在内存里的样子。**

```
普通函数值 f := run                      闭包 f := func() { count++ }

   f ──► funcval{fn: ·run}                  f ──► funcval{ fn: ·main$1,
                                                       ┐
                                                       └─► [ &count ]──► heap: count int
                                                                          ^
                      捕获的变量逃逸到堆上，funcval 后面跟着环境槽位 ┘
```

`runtime/runtime2.go` 里就这几行：

```go
type funcval struct {
	fn uintptr
	// variable-size, fn-specific data here
}
```

`funcval` 之后**变长**的部分就是闭包环境（捕获变量的地址）。所以：

- 函数值是**两个字**：指向 `funcval` 的指针 + （不存在）……准确说是一个指向 `funcval` 的指针；`funcval.fn` 才是代码地址。
- **闭包捕获的是变量本身，不是值**。捕获即意味着"这个变量可能活得比栈帧长" → 触发逃逸分析把它搬到堆上（详见第 14 章）。
- 方法值 `obj.Method` 也是一个闭包：编译器生成一个 `methodValue` 结构（`fn` + `receiver`），入口是汇编桩 `runtime.makeFuncStub`（`runtime/asm_amd64.s`、`runtime/asm_amd64.s` 里的 `·methodValue`）。

**图二：defer 的三条编译路径，编译器怎么选。**

```
                        ┌─ defer 在循环体内？
                        │      └─ 是 ──────────────► deferproc()      堆分配 _defer（贵，~30-40ns）
   编译器看到的 defer ──┤
                        └─ 否 ─┬─ 函数内 defer ≤ 8 个 ─► open-coded    栈上 deferBits + 内联调用（~1ns）
                               └─ 更多 ───────────────► deferprocStack()  栈上 _defer（~5-10ns）
```

判断逻辑在 `cmd/compile/internal/ssagen/ssagen.go`：常量 `maxOpenDefers = 8`，条件是"defer 数量不超过 8 且不在循环里"。**dev 里最常见的性能退化就是把 defer 挪进循环**——它同时命中"堆分配"和"泄漏 fd"两个坑。

---

## 8.3 底层实现

### 8.3.1 `_defer` 结构体

`runtime/runtime2.go`（字段随版本演进，以你本地源码为准）：

```go
type _defer struct {
	started   bool     // 是否已经开始执行（防止 panic 嵌套时重复执行）
	openDefer bool     // 是否来自 open-coded defer
	sp        uintptr  // 注册 defer 时的栈指针，用于匹配"这是哪个栈帧的 defer"
	pc        uintptr  // 返回地址
	fn        *funcval // 要执行的函数
	_panic    *_panic  // panic 发生时反向指回去，recover 靠它校验
	link      *_defer  // 链表指针：这就是 defer 的 LIFO 结构
	heap      bool     // 是否堆分配
	varp      uintptr  // 指向栈帧里的 deferBits（open-coded 模式）
	fd        unsafe.Pointer // funcdata，runtime 靠它知道怎么传参
	framepc   uintptr
}
```

`link *_defer` 就是 LIFO 的全部秘密：**`g._defer` 是链表头，新注册的插在头部，执行时从头往后走**。所以 `defer` 天然逆序，不需要任何排序。这个链表挂在 goroutine 上（`type g struct { ... _defer *_defer ... }`，见第 17 章）。

`_defer` 对象本身有池子：`freedefer(d)` 把它还回当前 P 的 `deferpool`（`p.deferpool [32]*_defer`）或全局 `sched.deferpool`，所以"堆分配"其实常常是"池里拿一个"。

### 8.3.2 普通路径：`deferreturn` + `jmpdefer` 的尾调用循环

函数返回时，编译器插入一句 `runtime.deferreturn()`。它的骨架：

```go
// runtime/panic.go（简化，保留关键行）
func deferreturn() {
	gp := getg()
	d := gp._defer
	if d == nil {
		return // 没 defer，几乎零成本
	}
	sp := getcallersp()
	if d.sp != sp {
		return // 链上的 defer 属于内层帧（内层还在跑），不是我的
	}
	if d.openDefer {
		done := runOpenDeferFrame(d)  // open-coded 分支
		if !done {
			throw("unfinished open-coded defers in deferreturn")
		}
		gp._defer = d.link
		freedefer(d)
		return
	}
	// 常规分支：摘链 → 释放 → 执行 → 回到 deferreturn
	fn := d.fn
	d.fn = nil
	gp._defer = d.link
	freedefer(d)
	jmpdefer(fn, getcallerpc()) // 汇编尾调用，跳去执行 fn
}
```

`jmpdefer`（`runtime/asm_amd64.s`）是这段实现里最漂亮的一手：

```
TEXT runtime·jmpdefer(SB), NOSPLIT, $0-16
	MOVQ	fv+0(FP), DX	// 要执行的 funcval
	MOVQ	argp+8(FP), BX	// 调用者的 sp
	LEAQ	-8(BX), SP	// 模拟"刚 CALL 完"的栈位置
	MOVQ	-8(SP), BP	// 恢复 BP，让 fn 以为自己是被普通 CALL 进来的
	SUBQ	$5, (SP)	// 把返回地址改写成"再回 deferreturn 一次"
	MOVQ	0(DX), DX	// 取代码地址
	JMP	DX		// 跳过去
```

它**不增加栈帧**，直接把返回地址改成 `deferreturn` 开头，于是整条 defer 链靠一次"跳出去 → 跳回来"的循环跑完，不会因为 defer 多而爆栈。这也是为什么 `defer` 在 Go 里敢用在任何地方。

### 8.3.3 open-coded defers：从 30ns 打到 1ns

Go 1.14 的核心优化。思路极其直白：**既然 defer 绝大多数情况是"函数末尾执行一次"，那就在编译器里把它变成内联代码，根本不要运行时记录。**

编译器改造后的函数大致长这样（伪代码，还原 `deferBits` + 内联调用的结构）：

```go
func f() (err error) {
	var deferBits uint8   // 栈帧里的位图，最多 8 位
	// —— defer stmt #0 —— 只做两件事：
	// 1) 把参数存到栈帧（求值时机不变！）
	// 2) deferBits |= 1<<0
	// （同时把一条 openDefer=true 的 _defer 记录挂到 g._defer，供 panic 时用）
	...
	// —— return 处展开成逆序的 if 判断 ——
	if deferBits&(1<<1) != 0 {
		deferBits &^= 1 << 1 // 先清位，这样 defer 里再 panic 也不会重复执行
		doCleanup2()
	}
	if deferBits&(1<<0) != 0 {
		deferBits &^= 1 << 0
		doCleanup1()
	}
	return err
}
```

代价与边界：

1. **参数求值时机完全不变**：仍然是 `defer` 语句执行的那一刻求值并存栈，所以"求值时机"这个坑在 open-coded 下依旧存在。
2. 超过 8 个 defer、或 defer 出现在循环里，退回 `deferprocStack` / `deferproc`。
3. panic 时走 `runOpenDeferFrame(df)`（`runtime/panic.go`）：它从 `df.varp` 读出 `deferBits`，按位**逆序**调用，靠 `_FUNCDATA_OpenCodedDeferInfo` 里的信息在**当前栈帧**上布置参数。所以 open-coded defer 的参数必须留在栈帧里，不能是纯寄存器的。

实测数量级（`go test -bench`，Go 1.22+，别背具体数字，记住量级）：

| 形态 | 一次 defer 的典型开销 | 触发条件 |
| --- | --- | --- |
| open-coded | **~1 ns** | ≤ 8 个 defer，且全在函数体直线代码里 |
| `deferprocStack` | ~5–10 ns | 超过 8 个，但仍无循环内 defer |
| `deferproc`（堆） | ~30–40 ns + 一次分配 | 循环里的 defer |

> 💡 所以"`defer` 很慢"这条从 Java 带来的直觉在 Go 1.14 之后**已经过期**了。`defer f.Close()` 摆在函数开头是正确写法，别为了"省 defer"手写十几处 `Close()`。

### 8.3.4 `gopanic`：panic 到底做了什么

`runtime/panic.go` 的 `gopanic` 是整条恢复流程的心脏。先看 `_panic`：

```go
type _panic struct {
	argp      unsafe.Pointer // 指向 recover 参数所在位置，用于校验"同一个栈帧"
	arg       any            // panic 的值
	link      *_panic        // 上一层的 panic（panic 可以嵌套！）
	pc        uintptr        // recover 之后从哪继续
	sp        unsafe.Pointer
	recovered bool           // 已被 recover
	aborted   bool           // 被 Goexit 打断
	goexit    bool
}
```

`gopanic` 的执行逻辑（按步骤）：

```
gopanic(e):
  1. 在**当前栈帧**上分配一个 _panic{arg: e}，压到 gp._panic 链表头（noescape，不上堆）
  2. runningPanicDefers++（供 fatalpanic 判断"是不是在 defer 里又 panic 了"）
  3. 死循环：
       d := gp._defer
       if d == nil:  →  没有 defer 能救了 → preprintpanics + fatalpanic → 打印所有 goroutine → exit(2)
       if d.started:  → 已经在跑（嵌套 panic）→ 记下，跳过
       d.started = true
       d._panic = &p            ← 关键：defer 反向知道自己是哪次 panic 的
       摘链、freedefer(d)
       调用 d.fn（open-coded 走 runOpenDeferFrame）
       if p.recovered:          ← 刚才那个 defer 里调了 recover()
           恢复 gp._panic = p.link
           mcall(recovery)      ← 切到 g0，recovery() 里 gogo(&gp.sched) 回到了 recover 那一刻的 deferreturn
           throw("recovery failed")  // 正常不会到这里
  4. 循环自然结束时：fatalpanic → 打印 "panic: ..." + 全栈 → exit status 2
```

**`recover()` 的实现只有 8 行**（`runtime/panic.go` 的 `gorecover`）：

```go
func gorecover(argp uintptr) any {
	gp := getg()
	p := gp._panic
	if p != nil && !p.goexit && !p.recovered && argp == uintptr(p.argp) {
		p.recovered = true
		return p.arg
	}
	return nil
}
```

那个 `argp == uintptr(p.argp)` 就是"**`recover` 必须被 defer 的函数直接调用**"这条规则的实现方式：编译器把当前函数参数区的首地址传进来，只有"当前正在执行的那个 defer 函数"才能对上号。所以你写 `defer func(){ func(){ recover() }() }()` 是**无效**的——中间隔了一层，`argp` 变了。

`recovered = true` 之后，`gopanic` 会通过 `mcall(recovery)` → `gogo(&gp.sched)` 把控制流送回 `deferreturn`，于是**栈不展开**地回到正常执行路径——这就是 Go 的 "recover 只是让 panic 停止继续冒泡"，它不会像 Java 的 `catch` 那样给你一个新的 catch 块上下文；你就是在原来的 defer 函数里，然后 `gopanic` 那一层的循环被放弃了。

### 8.3.5 一个能跑的实验

```go
package main

import "fmt"

type res struct{ Close func() }

func trace(name string) res {
	fmt.Println("open", name)
	return res{Close: func() { fmt.Println("close", name) }}
}

func named() (n int) {
	defer func() { n *= 2; fmt.Println("defer sees n =", n) }()
	d := trace("A")
	defer d.Close()
	return 7 // 先赋值 n=7 → defer 改 n=14 → 返回 14
}

func unnamed() (int, error) {
	var n int
	defer func() { n *= 2 }()
	d := trace("B")
	defer d.Close()
	return 7 // 返回 7
}

func main() {
	fmt.Println("named  ->", named())
	fmt.Println("unnamed->", unnamed())
	// 观察 LIFO：A 先 open，但 d.Close() 比第一个 defer 后注册 → 先 close A
}
```

输出会告诉你两件事：**LIFO 顺序**（后注册先执行）与**命名返回值的可改写性**。

---

## 8.4 Java 对照：会猜错的四个地方

| 你的 Java 直觉 | Go 的真相 | 原因 |
| --- | --- | --- |
| `finally` 在 try 块结束时执行 | `defer` 在**函数**返回时执行 | 注册单位是函数栈帧，不是代码块 |
| lambda 捕获"effectively final 变量值" | 闭包捕获**变量本身**（可读写，共享同一份） | 1.22 起每次循环迭代有新变量，但不改变"捕获变量"的语义 |
| 线程里抛异常只死一个线程 | **任何 goroutine 未捕获 panic → 整个进程 exit(2)** | 没有"线程级未捕获异常处理器"，这是刻意设计 |
| `finally` 里 `return` 会覆盖返回值 | `defer` 能改命名返回值，也能改正在上抛的 panic | 两者都发生在"返回值已写入、函数尚未移交"的窗口 |

**Go 为什么这样选？** Java 的 `finally` 与 `catch` 是**语句块**级别的控制结构，这带来了嵌套 try 的可读性灾难（`try { try { ... } finally {} } finally {}`）。Go 把清理动作压低到"一条语句 + 一个函数"，代价是：你无法在"函数中间某段"做清理，只能靠抽函数或显式调用。这个取舍让**清理动作和资源的生命周期永远一一对应**，也顺手消灭了"`finally` 里 return 掩盖异常"这一类 bug——虽然 Go 允许 defer 改返回值，但至少不会静默吞掉 panic 以外的控制流。

另一个必须知道的差异：**`os.Exit` 不跑 defer**。Java 的 `System.exit` 会跑 shutdown hook，Go 的 `os.Exit` 什么都不跑。要优雅退出，信号处理里应该 `return` 出 main，而不是 `os.Exit`（详见第 52 章）。

---

## 8.5 框架与工程落地

**1. 中间件链：defer 是 recover 的唯一挂载点。**

```go
func Recovery(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if p := recover(); p != nil {
				slog.ErrorContext(r.Context(), "panic in handler",
					"panic", p, "path", r.URL.Path, "stack", string(debug.Stack()))
				http.Error(w, "internal error", http.StatusInternalServerError)
			}
		}()
		next.ServeHTTP(w, r)
	})
}
```

Gin 的 `Recovery()` 中间件是同一套东西；它比手写多做了"检测 broken pipe"（连客户端都断了就别写响应了）。**每个 goroutine 入口都要有这一层**——包括你自己 `go func()` 起的 worker。

**2. 资源三件套：`defer` + 立即检查 `err`。**

```go
resp, err := client.Do(req)
if err != nil {
	return err
}
defer resp.Body.Close() // 必须在 err 检查之后，否则会 nil 解引用
```

顺序反了就是线上经典事故（`resp` 为 nil 时 `defer` 立即 panic）。这条规则在 `database/sql`（`defer rows.Close()`）、文件、`grpc.ClientStream`（`defer stream.CloseSend()`）上完全一致。

**3. `context` 的 `defer cancel()` 是强制的**，`go vet` 的 `lostcancel` 检查器会抓漏（详见第 24 章）。这也是 open-coded defer 最大的受益点之一——每个函数里一个 `defer cancel()` 现在几乎免费。

**4. Agent 场景里 defer 的两个高频用法**：

- 给一次工具调用做超时 + 计时：`defer span.End()`、`defer cancel()`、`defer func(){ metrics.Observe(time.Since(start)) }()`。**注意最后一种要用闭包**，`defer metrics.Observe(time.Since(start))` 会在 defer 语句处就把 `time.Since(start)` 求值成 0。
- 在流式输出（SSE）里保证"无论怎么退出都发一个终止事件"：

```go
defer func() {
	if r := recover(); r != nil {
		_ = sendEvent(w, "error", fmt.Sprint(r))
	}
	_ = sendEvent(w, "done", "") // 客户端靠它判断流正常结束
}()
```

---

## 8.6 踩坑清单（CR 可直接抄）

| # | 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- | --- |
| 1 | fd 耗尽 / 内存线性上涨 | `defer` 写在 `for` 循环里 | 抽成内层函数，或用 `func(){...}()` 包一层 | 8.3.3 |
| 2 | `defer` 里 `Close` 的参数是初始值 | `defer` 的参数在**语句处**求值 | 需要"最终值"就用闭包 `defer func(){ use(x) }()` | 8.3.3 |
| 3 | 命名返回值被莫名改掉 | `defer` 能改命名返回值 | 要么显式用无名返回值，要么把改写写成注释 | 8.1 |
| 4 | `recover` 抓不到 panic | `recover` 不是在 defer 的直接函数里调用的 | 只在 `defer func(){ recover() }()` 里直接调 | 8.3.4 |
| 5 | goroutine 里 panic 打挂进程 | 忘了给 goroutine 装 recover | 每个 `go func()` 顶部加 `defer recover` | 8.1 |
| 6 | 循环里起 goroutine，全是最后一个值 | Go 1.21 及以前共享循环变量 | 升级到 1.22+，或显式 `i := i` | 8.2 |
| 7 | `os.Exit` 之后连接没关、buffer 没 flush | `os.Exit` 不执行 defer | 优雅退出走 `return`，用 `Shutdown` | 第 52 章 |
| 8 | `defer` 里又 panic，日志只有一半 | 嵌套 panic，前一个 panic 信息被顶掉 | `recover` 里用 `debug.Stack()` 立刻抓栈 | 8.3.4 |

---

## 8.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 函数值 | 指向 `funcval` 的指针；`funcval.fn` 才是代码地址 |
| 闭包 | 捕获**变量**，变量逃逸到堆；1.22 起每轮循环有新变量 |
| `defer` 语义 | 参数在**语句处**求值，执行在**函数返回时**，顺序 LIFO（`g._defer` 链表） |
| open-coded | ≤8 个且不在循环 → 编译器内联成 `deferBits` 位图判断，~1ns |
| `deferreturn` | 返回时调度 defer 链，用 `jmpdefer` 尾调用避免爆栈 |
| 命名返回值 | 可被 `defer` 改写；无名返回值不行 |
| `panic` | 压 `_panic` 链表 → 逆序跑 defer → 没人 recover 就 `fatalpanic` 打全栈退出 |
| `recover` | 必须在 defer 的直接函数里调用（`argp == p.argp` 校验） |
| goroutine + panic | 未捕获 = 整个进程死，每个 worker 都要装 recover |
| `os.Exit` | 不跑 defer，优雅退出别用它 |

---

## 8.8 自检题

1. **`for _, f := range files { defer f.Close() }` 为什么危险？两种修法各自的代价是什么？**
   > 要点：`defer` 注册在函数上，循环内注册会累积 N 个 `_defer`（走 `deferproc` 堆分配），并且 fd 和内存要到函数返回才释放。修法一：抽成 `func process(f string) error { fh, _ := os.Open(f); defer fh.Close(); ... }`，每轮立即释放，代价是每轮一次函数调用；修法二：循环内 `func(){ ... }()` 匿名包一层，同样每轮释放，代价是多一层闭包（可能逃逸）。

2. **为什么 `defer func(){ recover() }()` 有效，而 `defer func(){ func(){ recover() }() }()` 无效？**
   > 要点：`gorecover` 校验 `argp == uintptr(p.argp)`，即 `recover` 必须与被 defer 的那个函数处在**同一个栈帧**。多套一层匿名函数后 `argp` 变成了内层函数的参数区地址，校验失败返回 nil。同理，`recover` 也不能被 `go` 到别的 goroutine 里调用。

3. **Go 1.14 的 open-coded defer 为什么能快 30 倍？它为此放弃了什么？**
   > 要点：把"运行时记录 + 返回时遍历链表"换成"编译期生成 `deferBits` 位图 + 内联调用"，省掉 `_defer` 分配和调度循环。放弃的是：只支持 ≤8 个 defer、不支持循环内 defer（此时退回 `deferproc`/`deferprocStack`），并且 defer 的参数必须留在栈帧上（不能纯寄存器传递），panic 路径要靠 `runOpenDeferFrame` + funcdata 才能跑起来。

---

## 8.9 延伸阅读

- `$GOROOT/src/runtime/panic.go`：`gopanic`、`gorecover`、`deferreturn`、`runOpenDeferFrame`、`preprintpanics`、`fatalpanic`
- `$GOROOT/src/runtime/runtime2.go`：`_panic`、`_defer`、`funcval`
- `$GOROOT/src/runtime/asm_amd64.s`：`jmpdefer`、`methodValue`、`makeFuncStub`
- `$GOROOT/src/cmd/compile/internal/ssagen/ssagen.go`：`maxOpenDefers`、open-coded defer 的生成条件
- Go 官方设计文档 *Proposal: open-coded defers*（Go 1.14）；Dan Scales 关于 defer 性能的讲稿
- `go vet` 的 `lostcancel`、`cf`（控制流）检查器
- 下一章：[9. 零值哲学、指针 vs 值与「可选值」设计](9-zero-value-optionals.md)

---

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：第 7 章 [错误处理哲学](7-error-handling.md)（panic 的**使用边界**在那里；本章讲**实现**）
- 相关：第 17 章（`g._defer` 挂在 `g` 结构上）、第 14 章（闭包变量为何逃逸）、第 24 章（`defer cancel()` 与 `lostcancel`）
- 下一章：[9. 零值哲学、指针 vs 值与「可选值」设计](9-zero-value-optionals.md)
