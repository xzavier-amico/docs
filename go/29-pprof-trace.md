# 29. pprof 与 trace 实战

> **状态**：`已完成（初稿）` ｜ **所属**：第五部分 · 运行时与性能 ｜ **编号**：29 / 54
> **本章定位**：把「性能分析」从「改代码试试」变成「看证据定位」。五种 profile 的采样机制讲清楚，火焰图与 execution trace 的读法讲透，微基准的五个坑列全，最后用一套真实流程定位 Agent 的 token 流延迟。
> **读法**：29.2 建立脑图，29.3 弄清「数字是怎么来的」（决定你敢不敢信），29.5 直接抄命令。

---

## 29.1 引子：首 token 8 秒，但上游日志写着 1.2 秒

线上 Agent 对话服务，用户抱怨「按下回车后要等七八秒才看到第一个字」。你查了 LLM 供应商的响应日志：首字节 1.2s，之后每 token 30ms，全部完成 6.8s。看起来「后端没问题」。可用户就是等了 8 秒。

三份证据把所有猜测一次干掉：

1. `curl /debug/pprof/goroutine?debug=1`：**4100 个 goroutine**，其中 3800 个卡在同一行——`chan send`，是埋点上报 channel 满了（消费者单 goroutine 写远端，打点一多就堵）。
2. `go tool trace`：请求 goroutine 的时间轴里，`Running` 只占 30%，而 `Blocked`（等待 channel）占 45%。
3. `go tool pprof -sample_index=delay .../block`：`delay` 顶格的两行是 `metrics.(*Reporter).emit` 与 `sync.(*Mutex).Lock`。

真相是：你给每个 SSE token 都打了一次点，打点走有界 channel，channel 满之后 `select` 走了阻塞分支，于是**每个 token 都被一次埋点阻塞**。改法是打点非阻塞丢弃（`select` + `default`）+ 批量 flush，P99 从 7.9s 掉到 1.4s。

> 🎯 本章的立场：**性能问题的第一现场永远不是代码，而是「你脑子里那个错误的归因」。** pprof 的价值不是给你看火焰图，而是**强制你把「我觉得」换成「证据显示」**。Java 侧你有 `top -H` + `jstack` + async-profiler + JFR，Go 侧这套工具其实更强——因为 goroutine 是运行时的概念，你能直接看到调度状态，而不是靠线程栈反推。

---

## 29.2 直觉模型：五种 profile，五种「谎言的精度」

先建立一张必须背下来的表。**profile 不是录像，是采样；采样就有偏差，偏差有方向。**

| Profile | 端点 | 采样机制 | 默认频率 | 能回答 | 典型误读 |
| --- | --- | --- | --- | --- | --- |
| **CPU** | `/debug/pprof/profile` | `SIGPROF` 定时打断，抓当前栈（`runtime/cpuprof.go`） | **100Hz**（`SetCPUProfileRate`） | 时间花在哪些函数 | 看成了「调用次数」；<10ms 的短函数完全抓不到 |
| **heap** | `/debug/pprof/heap` | 分配时按字节概率采样（`MemProfileRate`） | **512KB** 一次 | 谁分配了内存、谁活着 | 把采样值当精确字节；小对象全被漏采 |
| **goroutine** | `/debug/pprof/goroutine` | **不采样**，全量抓所有栈 | 每次请求 | 谁在阻塞、卡在哪一行 | 以为能看历史；只能看「此刻快照」 |
| **mutex** | `/debug/pprof/mutex` | 锁竞争事件采样（`SetMutexProfileFraction`） | 默认**关闭** | 哪个锁在打架、总延迟多少 | 忘了它是「延迟」不是「次数」 |
| **block** | `/debug/pprof/block` | 阻塞事件采样（`SetBlockProfileFraction`） | 默认**关闭** | channel/mutex/select 上的等待 | 以为包含网络 IO（不含） |

三条必须记住的推论：

1. **CPU profile 抓不到「等待」**。你的 goroutine 阻塞在 `chan send` 上，CPU 利用率只有 30%，CPU profile 会告诉你「很健康」——此时该看 block/goroutine profile。
2. **heap profile 的绝对值不可信，比例可信**。「这个函数占 60% 内存」是靠谱的结论，「它占 320MB」是采样估算。
3. **mutex/block profile 默认关闭，要开销**（每次事件记栈）。别在生产常开，按需开 + 限时。

```
            ┌───────────── CPU 时间花在哪 ─────────────┐
用户请求 ──▶ │  Running  → SIGPROF 采样 → cpu profile   │
            │  Waiting  → 阻塞事件记账 → block/mutex    │
            │  分配     → 概率采样     → heap profile   │
            └──────────────────────────────────────────┘
                 ↑ 三条虚线要同时看，任何一条「健康」都不能证明系统健康
```

---

## 29.3 底层实现：数字是怎么来的

### 29.3.1 CPU：100Hz 的统计学，不是挂钟

`runtime/cpuprof.go` 里有一个全局的 `cpuProfile` 结构：一个 100 个槽的 `log` 缓冲，加一个「两次采样之间正在记录」的 `lost` 计数。`SIGPROF` 由 `runtime.setcpuprofilerate` 设定的 100Hz 定时器发出，信号 handler 抓当前正在运行的 g 的栈，写进 buffer 后由一个专门的 `profileWriter` goroutine（1.12 起，不再用 system goroutine）写到 `io.Writer`。

由此产生两个实践结论：

- **采样精度 = 10ms**。一个 8ms 的函数在一次采样窗口里可能完全不被采到；所以「某函数看起来只占 0.5%」时不要急着优化，先跑 `-seconds=60` 拉长采样。
- **总样本数 ≈ 采样时长秒数 × 100 × 活跃线程数**。样本太少（比如只采 5 秒）时火焰图的分支会剧烈抖动，`benchstat`/`pprof -diff_base` 才是可靠做法。

### 29.3.2 heap：`MemProfileRate` 与「两种视角」

`runtime.MemProfileRate` 默认 512KB：平均每分配 512KB 才记录一次调用栈，用泊松过程选择采样点（`runtime/malloc.go` 的 `profilealloc`）。所以：

- **小对象密集的程序全是噪声**。一个占了 10 万个小 struct 的函数，可能因为单次分配都 < 512KB 而一个样本都没采到——但它的**总分配量**并不小。这时候调 `GODEBUG=allocfreetrace=1` 不现实，正确做法是把 `pprof -sample_index=alloc_space`（累计分配）而不是默认的 `inuse_space`（当前存活）看一眼。
- **heap profile 有四种 sample index**（`pprof` 顶部下拉框可切）：`inuse_space` / `inuse_objects` / `alloc_space` / `alloc_objects`。**「内存泄漏」看 inuse，「GC 压力大」看 alloc**——这是两码事（第 28 章的 mark assist 就是被 alloc 拖死的）。

### 29.3.3 goroutine / block / mutex：把「等待」变成可测的数字

- **goroutine profile** 是全量快照，所以它的成本与 goroutine 数量成正比。`?debug=1` 给聚合后的栈计数文本（最适合 `grep` 找「哪一行有 3000 个」），`?debug=2` 给每个 goroutine 的完整栈（适合看死锁）。
- **block profile** 记录的是 goroutine 在 `chan send/recv`、`select`、`sync.Mutex`、`sync.Cond`、`WaitGroup.Wait` 上的**入队/出队时间差**，实现在 `runtime/mprof.go` 的 `blockevent()`。要开：`runtime.SetBlockProfileFraction(rate)`，`rate=1` 全采（很贵），生产用 1/1000 级别的采样。
- **mutex profile** 只记录**发生竞争**的那次 `Lock`（`sync.Mutex`/`RWMutex` 里 `lockSlow` 的 `blockevent`），`rate` 语义与 block 类似。它的 `delay` 单位是纳秒，所以 **`-sample_index=delay` 排序才有意义**。

### 29.3.4 execution trace：唯一能看「调度」的工具

`runtime/trace` 不是采样，而是**事件流**（每个 P 有一块本地 buffer，满了刷到全局 buffer）。开销比 profile 高（约 1%~5%），所以只能短时开（`seconds=5~30`）。

```bash
# 采集 5 秒 trace（net/http/pprof 自动注册了 /debug/pprof/trace）
curl -o trace.out 'http://127.0.0.1:6060/debug/pprof/trace?seconds=5'
go tool trace trace.out     # 起本地 web，给出 Goroutine analysis / Scheduler / Sync blocking 等视图
```

`go tool trace` 里一个 goroutine 的状态就是第 17、18 章那张状态机在时间轴上的展开：

```
Goroutine 1234 ──────────────────────────────────────────────▶ time
  ████ Running        ░░░ Runnable      ▓▓▓ GC waiting
  ████            ░░░░                ▓▓▓         ▒▒ IO wait   ░░ Blocked

  █ Running   : 真的在某个 P 上执行（应占大头）
  ░ Runnable  : 可运行但没有空闲 P ← 调度延迟，QPS 瓶颈最常见来源
  ▓ GC        : 被 GC 标记/assist 拖住
  ▒ IO wait   : 在 netpoller 上等（对 LLM 网关来说，"等上游"通常都长这样，是正常的）
  ░ Blocked   : channel/mutex/select 阻塞 ← 29.1 那个事故就死在这里
```

**读法顺序**：先看 Goroutine analysis 里数量最多的那类 goroutine；再看它的 Runnable 占比（>10% 说明 CPU 不足或 GOMAXPROCS 设小）；然后看 Blocked/Runnable 的**堆叠位置**；最后用 `Scheduler latency` 图确认是不是「有活没人干」。

---

## 29.4 Java 对照

| 需求 | Java 侧 | Go 侧 | 关键差异 |
| --- | --- | --- | --- |
| CPU 火焰图 | async-profiler / JFR + `jfr print` | `pprof` + `-http`（自带火焰图） | Go 直接内建在运行时里，不用 attach agent |
| 内存分配 | JFR `ObjectAllocationSample`、async-profiler `-e alloc` | heap profile（概率采样） | JVM 有 TLAB 与对象头，采样惯性不同；Go 无对象头 |
| 线程/协程快照 | `jstack`（OS 线程）、虚拟线程要 dump | goroutine profile（**用户态对象**） | Go 能一次看到几十万个逻辑体，JVM 只能看几百个平台线程 |
| 锁竞争 | `jcmd Thread.print` 里的 BLOCKED、JFR `JavaMonitorEnter` | mutex profile（按延迟排序） | Go 的 mutex profile 是**延迟分布**，比「谁在 BLOCKED」信息量大 |
| 调度延迟 | JFR 有 thread park / CPU 时间线 | execution trace 的 Runnable 段 | **Go 的 trace 表达能力远强**，因为它看得见自己的调度器 |
| 常开生产 profiling | JFR 可以长期开（低开销） | trace 不能常开（事件流）；profile 按需 | 别把 JFR 的习惯带过来 |

> 💡 一句话：**JFR 是「事件 + 采样」的混合体，可以常开；Go 的 pprof 是采样，trace 是事件，两者分工明确，且都要求你「精确地短暂地开」。** 想长期监控，用 `runtime/metrics` + Prometheus（第 35 章），不要常开 trace。

---

## 29.5 框架与工程落地：一条可复制的定位流程

### 29.5.1 先写对 microbenchmark，再谈优化

```go
// bench_test.go —— 微基准的最小正确形态
package codec

import (
	"encoding/json"
	"strings"
	"testing"
)

var sink []byte // 包级变量：防止编译器把整个调用优化掉

func BenchmarkEncodeMessage(b *testing.B) {
	msg := ChatMessage{Role: "user", Content: strings.Repeat("prompt ", 128)}

	b.ReportAllocs()               // 等价于命令行 -benchmem
	b.SetBytes(int64(len(msg.Content)))
	b.ResetTimer()                 // 上面的 setup 不计入耗时
	for i := 0; i < b.N; i++ {
		out, err := json.Marshal(&msg)
		if err != nil {
			b.Fatal(err)
		}
		sink = out                 // 关键：把结果写出去
	}
}

// 并发基准：想验证「加锁 vs 分片」，必须用 RunParallel + 固定 GOMAXPROCS
func BenchmarkRegistryGet(b *testing.B) {
	b.RunParallel(func(pb *testing.PB) {
		for pb.Next() {
			_ = registry.Get("search")
		}
	})
}
```

```bash
# 1) 采集基线（-count 至少 6~10，否则统计没意义）
go test -run='^$' -bench=EncodeMessage -benchmem -count=10 -cpu=4 > old.txt

# 2) 改代码后再采一次
go test -run='^$' -bench=EncodeMessage -benchmem -count=10 -cpu=4 > new.txt

# 3) 统计显著性：benchstat 会给 p 值与置信区间，肉眼看不到的 3% 差异它能判死
benchstat old.txt new.txt
```

`benchstat` 的典型输出就是一张带 `vs base` 列的表（`sec/op`、`B/op`、`allocs/op`），**只有 p 值显著、且 allocs/op 同向变化的结论才值得写进优化台账**（第 30 章）。

### 29.5.2 线上 profile 的安全姿势

```go
package main

import (
	"context"
	"net/http"
	"net/http/pprof"
	"runtime/pprof"
)

func startDebugServer() {
	mux := http.NewServeMux()
	// 显式挂载，而不是 import _ "net/http/pprof" 挂到业务 DefaultServeMux
	mux.HandleFunc("/debug/pprof/", pprof.Index)
	mux.HandleFunc("/debug/pprof/profile", pprof.Profile)
	mux.HandleFunc("/debug/pprof/heap", pprof.Handler("heap").ServeHTTP)
	mux.HandleFunc("/debug/pprof/goroutine", pprof.Handler("goroutine").ServeHTTP)
	mux.HandleFunc("/debug/pprof/trace", pprof.Trace)

	// 只监听回环：外部访问必须经过 kubectl port-forward / SSH 隧道 + 鉴权
	go func() { _ = http.ListenAndServe("127.0.0.1:6060", mux) }()
}

// 给 profile 打标签：火焰图就能按租户/操作切分
func handleChat(ctx context.Context, tenant string) {
	pprof.Do(ctx, pprof.Labels("tenant", tenant, "op", "chat"), func(ctx context.Context) {
		runAgent(ctx)
	})
}
```

四条铁律：① **绝不复用业务端口**（`curl` 一个 30 秒的 CPU profile 会占住一个 handler）；② **只监听 127.0.0.1**，走 port-forward，绝不暴露公网（`/debug/pprof/` 泄漏的信息足够画出你的架构）；③ **给 `seconds` 设上限**，并只在排障窗口内开启；④ heap profile 需要遍历 span 位图、会带来一次短促的全局暂停（几十毫秒级，随堆大小增长），**大堆实例上顺手 curl 一把是有代价的**。

block 与 mutex profile **默认都是关闭的**（fraction = 0），所以「线上 mutex profile 是空的」不是没有竞争，是你没开。放到一个只在排障时调用的开关里：

```go
// 按需开启 block / mutex 采样，并保证排障结束一定关掉
func enableContentionProfiling(frac int) (restore func()) {
	runtime.SetBlockProfileFraction(frac) // 0=关；1=全采；生产建议 1000 量级的 1/N 采样
	runtime.SetMutexProfileFraction(frac)
	return func() {
		runtime.SetBlockProfileFraction(0)
		runtime.SetMutexProfileFraction(0)
	}
}

// 顺便：goroutine 数量是最便宜的健康指标（不采样、无栈遍历成本）
func goroutineCount() int { return runtime.NumGoroutine() }
```

常用命令速查：

```bash
go tool pprof -http=:0 'http://127.0.0.1:6060/debug/pprof/profile?seconds=30'
go tool pprof -http=:0 -sample_index=alloc_space 'http://127.0.0.1:6060/debug/pprof/heap'
go tool pprof -http=:0 -sample_index=delay 'http://127.0.0.1:6060/debug/pprof/block'   # 先 SetBlockProfileFraction
go tool pprof -http=:0 -diff_base before.pprof after.pprof                             # 优化前后差量
```

### 29.5.3 定位 Agent 的 token 流延迟：四步

1. **先分清「等」和「算」**：trace 里 goroutine 的 `IO wait` 占比高 = 等上游/等客户端（正常，LLM 就是这样）；`Running` 高 = 我们自己的 CPU；`Blocked` 高 = 锁/channel，看 29.1。
2. **每 token 的固定开销**：`pprof -sample_index=alloc_space` 看是不是每个 token 都 `json.Marshal` + `fmt.Sprintf` + 打点；这些可以被批量合并（第 30 章）。
3. **`goroutine` 数量随时间上涨**：`/debug/pprof/goroutine?debug=1` 连抓三次，若单调增长 → 泄漏，回第 25 章。
4. **客户端看到「成块到达」而不是「逐字到达」**：这是缓冲问题（反代 / 没用 `http.Flusher` / `bufio.Writer` 没 flush），profile 帮不了你，去第 39 章。

---

## 29.6 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| benchmark 显示 0.3ns/op，明显不可能 | 调用被编译器优化掉了 | 结果写包级 `sink` 变量，或用 `b.ReportAllocs` + 检查是否真分配 | 29.5.1 |
| benchmark 数字忽高忽低 | 没 `ResetTimer`，setup 计入；或 `-count=1` | `b.ResetTimer()` + `-count=10` + `benchstat` | 29.5.1 |
| `-bench` 结果第一次特别慢 | 包初始化、`sync.Pool` 未热、CPU 频率爬升 | 加 `-benchtime=2s`，赌 `-count` 多次取中位 | 29.5.1 |
| CPU 利用率只有 30%，但用户觉得慢 | CPU profile 看不到「等待」 | 查 block/goroutine profile + execution trace | 29.3.3 |
| heap profile 里某函数「占 320MB」，实际不是 | 512KB 概率采样的估算值 | 只看比例与排名；要看精确值用 `runtime.ReadMemStats` 的 `HeapAlloc` | 29.3.2 |
| mutex profile 是空的 | 默认 `SetMutexProfileFraction(0)`，没开 | 排障窗口内 `runtime.SetMutexProfileFraction(1000)` 级别的采样 | 29.3.3 |
| 在生产开了 `/debug/pprof/` 忘了关 | 挂在业务 mux + 公网可达 | 独立回环端口 + port-forward + 用完即关 | 29.5.2 |
| trace 采集时服务卡顿 | trace 事件流开销 1%~5%，且 buffer 有锁 | 只采 5~30 秒，避开高峰 | 29.3.4 |
| 火焰图里 60% 在 `runtime.gcAssistAlloc` | 分配太快，GC 在收费（第 28 章） | 降 alloc_space：池化、预分配、避免装箱 | 第 28、30 章 |

---

## 29.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| CPU profile | `SIGPROF` 100Hz 采样，精度 10ms，抓不到等待 |
| heap profile | 512KB 概率采样；`inuse_*` 看泄漏，`alloc_*` 看 GC 压力 |
| goroutine profile | 全量快照，不采样；`debug=1` 聚合栈最适合 grep |
| block / mutex | 记录「等待延迟」，默认关闭，生产用低采样率短时开 |
| trace | 事件流不是采样，能看 G 的 Running/Runnable/Blocked；只能短时开 |
| 火焰图 | 宽度 = 时间占比（不是调用次数），看顶部「叶子」别只看根 |
| 微基准 | `-benchmem -count=10` + `benchstat`；结果写 `sink` 防优化 |
| 线上安全 | 独立回环端口 + port-forward + 限 `seconds` + 用完关 |
| 归因顺序 | trace 定「等还是算」→ goroutine 找阻塞点 → block/mutex 找锁 → heap 找分配 |
| Agent 特有 | 每 token 的打点、`Marshal`、`Flush` 是三大固定开销源 |

## 29.8 自检题

1. **你的服务 CPU 利用率 25%，但接口 P99 很差。先看哪个 profile？为什么 CPU profile 会「骗你」？**
   > 要点：先 trace 定性质（Running vs Runnable vs Blocked vs IO wait），再 block/goroutine profile 找阻塞点。CPU profile 只采样正在运行的 g，75% 的时间在「不运行」状态，它天然看不到；而且 CPU 低不等于快，可能是全都在等锁/等 channel。

2. **同事贴出一张火焰图说「`encoding/json` 占了 42%，我们要换库」。你怎么验证这个结论值不值得做？**
   > 要点：① 确认采样时长够（≥30s，样本数足够）；② 看是 `alloc_space` 还是 `inuse_space`，以及是否在 `gcAssistAlloc` 里被算进去（其实是 GC 成本）；③ 用 `-diff_base` 对比换库前后的真实差量；④ 检查是否可以通过「少 marshal 一次」「复用 buffer」拿到同样收益（第 31 章）；⑤ 换 sonic 要评估平台限制与可维护性。

3. **为什么 `benchstat` 说「差异不显著」，但线上 QPS 确实涨了？三种解释。**
   > 要点：① 微基准测的是单点 CPU，线上瓶颈在别处（锁/IO/GC），微基准优化根本不影响瓶颈；② 微基准的输入与线上分布不同（数据规模、命中率、并发度）；③ 测量噪声/CPU 频率漂移，线上的变化来自机器规格或流量结构变化，不是代码。结论：微基准只证明「这个函数更快」，不证明「系统更快」。

## 29.9 延伸阅读

- `$GOROOT/src/runtime/cpuprof.go`（`cpuProfile`、`SetCPUProfileRate`）、`runtime/mprof.go`（`blockevent`、记录点）、`runtime/pprof/pprof.go`（各 profile 的 `WriteTo`）
- `$GOROOT/src/runtime/trace/`、`$GOROOT/src/internal/trace`（1.21 重写后的 trace 格式与解析）
- `golang.org/x/perf/cmd/benchstat` 的 README（会讲清楚它做的统计检验）
- 官方文档 *Profiling Go Programs*、*Diagnostics* 章节；Felix Geisendörfer 关于 Go 调度 trace 的系列文章
- `net/http/pprof` 包文档（每个端点的 `debug`/`seconds` 参数语义）

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 28 章 GC](28-gc.md)
- 相关：G 的状态机本体见[第 17 章](17-goroutine-internals.md)；`findRunnable` 与调度延迟见[第 18 章](18-gmp-scheduler-source.md)；goroutine 泄漏四型见[第 25 章](25-concurrency-pitfalls.md)
- 下一章：[第 30 章 性能优化：分配、锁、批处理](30-perf-tuning.md)
