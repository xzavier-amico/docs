# 27. 编译链路：AOT 与 JIT 的路线之争

> **状态**：`已完成（初稿）` ｜ **所属**：第五部分 · 运行时与性能 ｜ **编号**：27 / 54
> **本章定位**：把 `go build` 那一两秒里发生的事完整摊开——token 流 → AST → 类型化 IR → SSA → 汇编 → 静态链接。读完你能对着 `go tool compile -S` 的输出指出「边界检查在哪一行」「这个函数为什么没被内联」「PGO 到底改了什么」，并且能一句话解释**为什么 Go 不需要预热、代价是什么**。
> **读法**：救火先读 27.1 / 27.6 / 27.7；想理解路线之争读 27.2 + 27.4；要动手调优读 27.3。

---

## 27.1 引子：压测曲线为什么会「越跑越快」

你把一个 Java 写的 LLM 网关迁到 Go，上线做压测。第一分钟 P99 = 380ms，第五分钟 P99 = 260ms，之后稳定。你的第一反应一定是：「哦，预热还没跑完，等 JIT 编完热路径就好」。

**这个解释在 Go 里是错的。** Go 没有 JIT，`main.main` 执行的第一纳秒跑的就是最终机器码，没有任何一层在后台偷偷把字节码编译成更高优化等级的东西。那么 380 → 260 的下降来自哪里？三个最真实的原因：

1. **连接与池在长**：`http.Transport` 连接池、`database/sql` 连接、go-redis 连接，前几十秒在反复握手建连。
2. **`encoding/json` 的字段缓存**：第一次编/解某个 struct 时走反射建 `structFields`，之后命中缓存（第 31 章）。
3. **GC 在找节奏**：堆小的时候 `GOGC=100` 触发的回收极频繁，堆涨上来后 amortize 掉了固定成本（第 28 章）。

三条都和「编译」无关，但都和「**Go 把成本前置到编译期，运行期只剩下分配和 IO**」这条设计逻辑有关。而 Java 的 380 → 260 里，真的有 50~100ms 是 C1 → C2 的 profile 驱动重编译。

> 🎯 本章的立场：**AOT 与 JIT 不是「性能高低」之争，是「成本放在哪」之争。** Go 把编译成本 100% 前置（秒级构建、毫秒启动、零预热），换来的是「峰值性能要靠 PGO 手工补一点」；JVM 把编译成本摊到运行期（启动 10s+、预热几分钟），换来的是「运行期能看到真实数据，做 profile 驱动的激进优化」。两边都在算账，只是账面不同。

从 Java 视角看，`go build` 更像 `native-image`（GraalVM AOT）而不是 `javac`——**但它没有 native-image 那堆反射配置地狱**，因为 Go 的二进制里 Runtime 只认识自己的类型表（`runtime/type.go` 里的 `_type`/`itab` 表），不依赖运行时反射发现业务类。

---

## 27.2 直觉模型：两条流水线的形状

```
 Java：                       Go：
 .java                        .go
   │ javac（很快，不做优化）      │ go tool compile（慢，全量优化）
   ▼                            │  syntax → types2 → noder → ir
 .class（字节码，栈机）          │  → walk → escape → inline → ssagen
   │ 加载 → 解释执行（慢）        │  → ssa 优化遍 → lower → regalloc
   ▼                            ▼
 JVM：解释器 30% 速度          .o（机器码，寄存器机）
   │ 采样热点                     │ go tool link
   ▼                            ▼
 C1 → 计数/内联 → C2（最优）    静态可执行文件（libc 也可以不要）
                                ▲ 启动 = execve + 初始化 runtime
```

两者的关键分歧：

| 维度 | JVM（JIT） | Go（AOT） |
| --- | --- | --- |
| 编译发生在 | 运行期，由 profile 驱动 | 构建期，一次性 |
| 编译器看到的信息 | 真实类型分布、分支概率、去虚化证据 | 只有静态类型 + 可选的 PGO 采样 |
| 优化上限 | 更高（投机优化 + 逆优化 deopt 兜底） | 略低（不能投机，错了没法撤销） |
| 启动成本 | 类加载 + 解释 + 预热，秒级 | 静态二进制 execve，毫秒级 |
| 部署物 | jar + JVM + JIT 缓存（AppCDS） | 单个 ~2MB 二进制 |
| 出问题的时刻 | 高峰前 5 分钟（还在解释执行） | 高峰那一刻（没预热缓冲，直接面对流量） |

> 💡 记住这张表最后一行：**AOT 的「零预热」是一把双刃剑**——它让扩容是秒级的（serverless / scale-to-zero 友好），也意味着你没有任何「跑一会儿就好了」的借口。Go 服务上线即满负荷，也上线即暴露。

---

## 27.3 底层实现：`go build` 到底做了什么

### 27.3.1 六个阶段与它们的源码位置

`go build` 本身（`cmd/go/internal/work`）只负责调度：算出包依赖图 → 并行调 `compile` → 调 `asm`/`cgo` → 最后 `link`。真正干活的是编译器前端。下表按执行顺序给出包路径，**这张表就是你读 `$GOROOT/src/cmd/compile` 的地图**：

| # | 阶段 | 源码位置 | 干什么 | 你能观察到的现象 |
| --- | --- | --- | --- | --- |
| 1 | 词法 + 语法 | `cmd/compile/internal/syntax`（`scanner.go`/`parser.go`） | 源码 → AST，顺手做 `gofmt` 需要的注释定位 | 语法错误在这里报 |
| 2 | 类型检查 | `cmd/compile/internal/types2` | 泛型实例化、类型推导、常量折叠 | 类型错误在这里报；与 `go/types` 同源 |
| 3 | 降糖 | `cmd/compile/internal/walk` | `range`/`switch`/`defer`/闭包/多返回值/`append` 拆成基础语句 | `range` 循环真的被改写成下标循环 |
| 4 | 逃逸分析 | `cmd/compile/internal/escape` | 决定每个变量上栈还是上堆（第 14 章） | `-gcflags=-m` 的 `escapes to heap` |
| 5 | 内联 / 死代码 | `cmd/compile/internal/inline`、`ir` 的 `Deadcode` | 小函数原地展开、消除不可达 | `-gcflags=-m` 的 `can inline … (cost N)` |
| 6 | SSA 与机器码 | `cmd/compile/internal/ssagen` → `ssa` → `amd64`/`arm64` | 构造 SSA、跑几十个优化遍、降级为机器指令、寄存器分配 | `GOSSAFUNC=Fn go build` 生成 `ssa.html` |
| 7 | 汇编 + 链接 | `cmd/internal/obj`、`cmd/link` | 生成 `.o`、符号解析、重定位、生成静态可执行文件 | `go tool nm` / `objdump` |

一个常被误解的点：**Go 编译快不是因为「不优化」，而是因为「不做跨包的重量级全局分析 + 单遍设计 + 包级并行编译 + 没有模板实例化爆炸」**。每个包独立编译，跨包协作只通过 export data——里面带着候选内联函数的**函数体**，所以跨包内联是有的（1.20 统一 IR 之后做得更好），但**跨包的内联又内联**这类需要全局视野的深挖，Go 基本不做。

### 27.3.2 SSA 优化遍：最有名的调试开关

SSA（Static Single Assignment）阶段是 Go 编译器的核心，`cmd/compile/internal/ssa` 里有约 50 个 pass，按 `compile.go` 里的 pass 列表顺序执行，大致分为三段：**机器无关优化**（`opt`、`cse`、`nilcheck`、`prove`、`deadcode`、`fuse`、`looprotate`）→ **`lower`（降级为目标架构指令）** → **机器相关**（`schedule`、`regalloc`、`flagalloc`）。

想看清它干了什么，别读源码，先看 HTML：

```go
// demo.go —— 观察 SSA 的最小例子
package main

import "fmt"

func sum(s []int) int {
	t := 0
	for i := 0; i < len(s); i++ {
		t += s[i] // 这一行的边界检查能不能被消掉？
	}
	return t
}

func main() { fmt.Println(sum([]int{1, 2, 3})) }
```

```bash
# 生成 ssa.html：左边源码，右边每个 pass 后的 SSA 形态，可以一步步点下去
GOSSAFUNC=sum go build -o /dev/null demo.go && open ssa.html

# 看内联决策与成本（cost 越小越容易内联，预算 80 个节点）
go build -gcflags='-m' demo.go

# 只看边界检查：证明器 prove 成功时会打印 Found IsInBounds
go build -gcflags='-d=ssa/check_bce/debug=1' demo.go
```

`ssa.html` 里你会看到 `prove` 这个 pass 之后，循环体的 `IsInBounds` 消失了——因为 `i < len(s)` 已经给了证明器足够的信息（这就是 27.3.3 的主题）。这类「从 HTML 里读优化过程」的手段，比在 Java 侧读 `-XX:+PrintCompilation` + `-XX:+PrintInlining` 直观得多，因为你能看到**每一个中间形态**。

### 27.3.3 内联、边界检查消除、nil check 消除：三件事一起讲

**内联是一切优化的前提**。Go 的内联判据非常土：把函数体折算成「节点成本」，`inline.InlineCall` 里预算约 80（`inlineMaxBudget`，历史上是 80，随版本有微调；`canInline` 里超过 5000 节点的函数直接放弃内联）。带闭包调用、`for` 循环、`select`、`defer`、`recover`、`panic` 的语句成本更高，所以它们容易挡住内联。

```go
package main

// cost 很低：两三次赋值 + 一次加法，稳内联
func double(x int) int { return x * 2 }

// cost 偏高：有循环、有函数调用，几乎必然 can inline 失败
func sumAll(xs []int) int {
	t := 0
	for _, x := range xs {
		t += double(x) // 但 double 在这里被内联了 → 循环体只有一条 LEAQ
	}
	return t
}
```

**边界检查消除（BCE）**是 Go 编译器最实用的自动优化，实现在 `cmd/compile/internal/ssa/prove.go` 的「证明器」里：它维护一组关于变量取值范围的「事实」，遇到 `s[i]` 就尝试用事实证出 `0 <= i < len(s)`，证得出就把 `IsInBounds` 节点删掉。

```go
// ❌ 有边界检查：range 变量与 s 的关系对证明器不够直接
for i := 0; i < len(s); i++ { _ = s[i] }

// ✅ 证明器直接消掉：len 已进入事实集
for i := range s { _ = s[i] }

// ❌ 二次检查：s[i] 与 s[j] 都要证明
for j := range s { _ = s[j] + s[0] }

// ✅ 一次证明，复用到两处
n := len(s)
for i := 0; i < n; i++ { _ = s[i] + s[0] }
```

**nil check 消除**在 `ssa/nilcheck.go`：同一指针在无写入的区间内只检查一次。这三件事的共同点是——**它们全都依赖「先内联」和「信息不跨函数丢失」**，所以 Go 代码里常见的手法是「把热点循环写在一个函数里，别拆成三层小函数互相调用」，代价是牺牲一点可读性。

### 27.3.4 PGO（1.21 GA）：给 AOT 补上「真实数据」

Go 1.20 引入 `-pgo` 预览，**1.21 正式可用（GA）**，且 `-pgo` 默认值变为 `auto`：只要主包目录下有 `default.pgo`，构建就自动使用它。PGO 的输入就是一份 pprof CPU profile：

```bash
# 1) 线上/压测环境采 30 秒 CPU profile（第 29 章会讲安全姿势）
curl -o cpu.pprof 'http://localhost:6060/debug/pprof/profile?seconds=30'

# 2) 放到主包目录并固定版本库里的文件名
cp cpu.pprof cmd/gateway/default.pgo

# 3) 正常构建即可（-pgo=auto 是 1.21+ 默认），也可以显式指定
go build -pgo=cmd/gateway/default.pgo -o gateway ./cmd/gateway
```

PGO 主要改三件事：① **热调用点的内联预算放大**（热站点的成本阈值从 80 抬到约 2000，官方博客给的量级是「热点调用点可获得更大的内联预算」）；② **接口调用去虚拟化**（profile 里 99% 是某一种具体类型，就把它改成直接调用 + 类型检查分支）；③ 更合理的寄存器分配与基本块布局。官方口径的收益量级是 **CPU 时间降低 2%~7%**，对 JSON/编解码/网关类负载更明显。

> ⚠️ PGO profile 要**跟着代码一起进仓库并定期更新**——用三个月前、代码结构已经大改的 profile 去指导内联，效果可能是负的。

### 27.3.5 汇编、静态二进制与「毫秒级启动」的真相

想确认编译器到底生成了什么，`go tool compile -S` 看的是**编译器输出**，`go tool objdump` 看的是**链接后的真实指令**（含被链接器改过的东西）——两者用途不同，别混。

```bash
# 编译器视角：含符号名、行号映射，读起来最像人话
go tool compile -S -N -l demo.go 2>&1 | sed -n '/"".sum STEXT/,/RET/p'
```

输出大致长这样（指令选择与偏移随版本变化，这里只保留骨架）：

```asm
"".sum STEXT nosplit ABIInternal
	MOVQ  "".s_base+24(SP), AX     ; 切片 data 指针
	MOVQ  "".s_len+32(SP), CX      ; 切片 len
	XORL  BX, BX                   ; t = 0
	XORL  DX, DX                   ; i = 0
loop:
	CMPQ  CX, DX                   ; len <= i ？
	JGE   done
	CMPQ  CX, DX                   ; ← 内层边界检查：被 machine prove 消除了就不用这条
	JLS   panicIndex               ; 越界 → runtime.panicIndex
	ADDQ  (AX)(DX*8), BX           ; t += s[i]
	INCQ  DX
	JMP   loop
done:
	MOVQ  BX, "".~r1+48(SP)
	RET
panicIndex:
	CALL  runtime.panicIndex(SB)
```

读这段的三个要点：① 没有函数序言/栈分裂检查被省略是 `NOSPLIT` 的效果（小函数有 leaf 优化）；② 看到 `CALL runtime.panicIndex` 就说明那里有一条没被消掉的边界检查；③ 每多一条 `CMPQ ... JLS` 就是一次可省掉的分支预测压力。

**启动为什么是毫秒级**：`CGO_ENABLED=0` 时产物是纯静态 ELF，`execve` 后只做三件事——初始化 `runtime`（`mallocinit` 建 heap arena 元数据、`mcommoninit` 起 m0）、启动后台 goroutine（`sysmon`、`bgsweep`、`bgscavenge`）、跑所有包的 `init()`。没有动态链接器找 `.so`、没有类加载、没有 JIT 线程。所以 Go 服务在 K8s 里扩一个副本到 ready 通常是 **几十毫秒**；JVM 那 20 秒里，大部分时间是类加载与 Spring 上下文装配（第 36 章讲装配为什么可以做到同样快）。

---

## 27.4 Java 对照：Go 为什么放弃了 JIT

| 政策 | JVM 的选择 | Go 的选择 | Go 的理由 |
| --- | --- | --- | --- |
| 优化时机 | 运行期，profile 驱动 | 构建期，静态 | 部署简单权重高于峰值性能；不能让「第一个请求」比「第十万个请求」慢 |
| 投机优化 | 大量使用（去虚化、分支预测、常量折叠基于真实类型） | **不允许** | Go 的二进制里没有 deopt 元数据，错了无法回滚到解释器 |
| 对象分配 | TLAB + 逃逸分析 + 标量替换（JIT 知道谁不逃逸） | 编译期逃逸分析，栈上分配（第 14 章） | 无对象头，栈分配更简单；标量替换 Go 不做 |
| 反射/注解 | 运行期反射是常态，JIT 会对热点反射路径生成字节码 | 反射用 `reflect` 纯 Go 实现，热点靠**代码生成**替代（第 37 章） | 少一层动态魔力，多一层可读性 |
| 峰值性能 | 长跑稳态更强（尤其复杂 OO 代码） | 弱 5%~20%，靠 PGO 补 2%~7% | 接受；Go 的主战场是 IO 密集服务而非计算密集单体 |
| 启动 | 秒级，冷启动治理要 AppCDS / native-image | 毫秒级，天然适合 scale-to-zero | serverless / 弹性扩缩的第一性要求 |

> 🩸 **反直觉的坑**：很多人以为「Go 没有 JIT，所以压测不用预热」。错。**Go 需要「语义预热」而不是「代码预热」**：连接池、JSON 类型缓存、`sync.Pool` 的 per-P 私有槽（第 22 章）、map 的扩容、甚至 `math/rand` 的 seed。只是预热的是**数据结构和外部连接**，不是机器码。写容量规划时，两者混淆会让你把 P99 归因到错误的地方。

---

## 27.5 框架与工程落地

- **CI 里的构建命令**：`CGO_ENABLED=0 go build -trimpath -buildvcs=false -ldflags='-s -w -X main.version=$GIT_SHA'`。`-trimpath` 去掉本机路径（可重现 + 不泄露目录结构），`-s -w` 去掉符号表与 DWARF（体积 -25% 左右，代价是 `delve` 断点信息变弱——所以**生产镜像与调试镜像分开构建**）。详见第 52 章。
- **`-gcflags` 只用于本地诊断**，别进生产构建：`-gcflags=all=-m` 会把内联/逃逸决策刷屏，`-N -l` 关掉优化与内联是为了调试。
- **分析器**：`golang.org/x/tools/go/analysis/passes/fieldalignment` 能自动指出「这个 struct 重排字段能省 24 字节」（第 14 章）；`staticcheck` 之外的这类 SSA 级检查，是 Go 相对于 Java 侧（SpotBugs 只能看字节码）的优势——它能看**优化后**的形态。
- **Agent / LLM 网关场景的真实价值**：LLM 网关是「大量 goroutine 阻塞在 socket 上 + 少量 JSON 编解码」的负载。AOT 让它能在流量突增时 100ms 内起新副本；反过来，如果把 LLM SDK 的响应体解析写成反射密集的 `map[string]any`，那 AOT 帮不了你——那是第 31 章的战场。Gin / kratos / eino 这些框架层面对编译链路没有特殊要求，它们的问题在运行时，不在这。
- **想验证「我的二进制是静态的」**：`file ./app`（看 `statically linked`）、`ldd ./app`（`not a dynamic executable`）、`go version -m ./app`（Go 1.18+，读二进制里记录的模块信息，能立刻回答「线上到底是哪个 commit、哪个依赖版本」）。

---

## 27.6 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| 同一个 hot loop 比别人慢 30%，火焰图显示全在 `memmove`/边界检查 | 循环被拆成多层小函数，内联失败导致 BCE 信息丢失 | 把热点循环收敛到一个能被内联的函数里；用 `-d=ssa/check_bce` 验证 | 27.3.3 |
| 加了一行日志后 benchmark 变慢一倍 | `fmt.Sprintf`/`log` 让参数逃逸，栈分配变堆分配 | 用 `slog` + 惰性字段；确认 `-m` 无多余 `escapes to heap` | 第 35 章 |
| 上了 PGO 反而更慢 | profile 采样的是旧版本代码 / 负载不匹配 / 只覆盖了单一场景 | profile 与版本绑定，进仓库，随大版本更新重采 | 27.3.4 |
| 线上二进制没有符号，`delve` 挂不上 | 用了 `-s -w` | 保留一份未 strip 的构建产物做符号服务器，或以 `-ldflags='-w'` 少 strip 一层 | 第 52 章 |
| 交叉编译后在 ARM 机器上启动即崩 | 用了 cgo（默认 `CGO_ENABLED=1` 且目标平台无 C 工具链） | `CGO_ENABLED=0`；必须 cgo 时（如 sqlite、推理引擎）用目标架构交叉工具链 | 第 16 章、第 52 章 |
| 以为「压测第一分钟慢」是 JIT 没热 | Go 没有 JIT | 查连接池、JSON 字段缓存、`sync.Pool`、map 扩容 | 27.4 |
| `go build` 越来越慢 | 依赖图变大 + 未命中 build cache | 查 `GOCACHE`、用 `go build -x` 看重复编译；别随手 `-a` | 第 2 章 |

---

## 27.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 阶段链 | syntax → types2 → noder/ir → walk → escape → inline → ssagen → ssa → obj → link |
| SSA 调试 | `GOSSAFUNC=Fn go build` 出 `ssa.html`，能逐 pass 对比形态 |
| 内联预算 | 节点成本约 80（版本相关），大函数（5000 节点级）直接不内联 |
| BCE | 由 `ssa/prove.go` 的证明器做；`range` 循环天然无边界检查 |
| 汇编 | `go tool compile -S` 看编译器输出，`go tool objdump` 看链接后指令 |
| PGO | 1.21 GA，`-pgo=auto` 读主包目录 `default.pgo`，收益 2%~7% |
| 静态二进制 | `CGO_ENABLED=0` + `-trimpath` + `-ldflags='-s -w'` |
| 启动 | 毫秒级：无类加载、无 JIT 线程，只有 runtime 初始化 + `init()` |
| 没有 JIT | 无投机优化、无 deopt，峰值略低，整体可预测性高 |
| 预热真相 | Go 需要「语义预热」（池/缓存），不需要「代码预热」 |

## 27.8 自检题

1. **同事说「Go 是 AOT，所以线上不用预热，压测第一分钟就代表真实性能」。这句话哪里对、哪里错？**
   > 要点：对在「没有 JIT 重编译」；错在把「代码预热」和「语义预热」混为一谈。连接池握手、`encoding/json` 字段缓存、`sync.Pool` 的 per-P 槽、map 扩容都会让前几十秒显著更慢，压测报告必须注明采集窗口。

2. **`for i := 0; i < len(s); i++ { t += s[i] }` 与 `for i := range s { t += s[i] }` 生成同样的机器码吗？为什么？**
   > 要点：通常不。`range` 的形态让证明器直接拿到 `0 <= i < len(s)` 的事实，边界检查被 `prove` pass 消掉；手写下标版本在某些形态下仍会保留一次 `CMPQ/JLS`（取决于编译器版本与函数复杂度）。用 `-d=ssa/check_bce/debug=1` 实测，别靠信仰。

3. **为什么 Go 可以「不做跨包内联」却仍然在各种库上表现不差？**
   > 要点：① 包内内联 + export data 携带可内联体已经覆盖绝大部分热点；② 标准库的抽象层（`io.Reader`、`sort`）本身设计得薄；③ 真正需要跨包重优化的地方，靠 PGO 的热点内联补；④ 换来的是包的独立编译与并行构建，这才是「秒级构建」的来源。

## 27.9 延伸阅读

- `$GOROOT/src/cmd/compile/internal/ssa/compile.go` 里的 pass 列表；`prove.go`（BCE）、`nilcheck.go`、`inline/inl.go`
- Go 官方博客：*Profile-guided optimization in Go 1.21*、*Go 1.20 PGO preview*
- `go doc cmd/compile` 里 `-d`、`-m`、`-l`、`-S`、`-N` 的完整说明（`go tool compile -d help` 可列出全部调试开关）
- Keith Randall 的 *Generating Better Machine Code with SSA*（GopherCon 2015），SSA 阶段的权威入门

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 26 章 结构化并发](26-structured-concurrency.md)
- 相关：逃逸分析与内存布局见[第 14 章](14-memory-layout-escape.md)；内存分配器见[第 15 章](15-allocator.md)；镜像与部署见[第 52 章](52-deployment-sre.md)
- 下一章：[第 28 章 GC：三色标记、混合写屏障与调参](28-gc.md)
