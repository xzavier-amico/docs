# 《Go 深水区：从 Java 到 AI-Agent 架构》· 总纲

> **一句话**：这本书不教「怎么写出能跑的 Go」，而是把你那颗 JVM 脑袋里的插头拆下来，插进 goroutine 的插座，再一路通电到 LLM Agent 的生产架构里。
>
> **读完之后的状态**：能读懂 `runtime` 源码里的注释；CR 时一眼看出并发缺陷；能自己写出带工具调用 / 流式输出 / 持久化 / 可观测的 Agent 框架；并且清楚每行代码在机器上、在 GC 眼里、在调度器眼里发生了什么。

| 项 | 说明 |
| --- | --- |
| 目标读者 | 3 年以上 Java 经验，熟 JVM / 集合 / 并发 / Spring；对 Go 只停留在「能看懂 hello world」 |
| 语言基准 | **Go 1.24 ~ 1.25+**（含 1.24 Swiss Table map、1.25 Green Tea GC、cgroup 感知 GOMAXPROCS、`encoding/json/v2` 实验特性） |
| 篇幅规划 | 9 个部分 · **50 章** · 每章 1 个文件，可独立阅读，也可连成体系 |
| 风格 | 轻松、类比多、Java 对照只出现在「你会猜错的地方」、先直觉后源码再回工程、**同一个概念绝不讲两遍** |
| 文件约定 | `go/0-summary.md`（本文件）+ `go/1-xx.md` … `go/50-xx.md` |

---

## 0. 方法论：四层透视法

每一章都走同一套骨架，保证「看得懂 + 拔得高 + 不冗余」：

```
① 场景引子   一个真实会痛的问题（不是语法手册式罗列）
② 直觉模型   用人话 + Java 类比，先建立正确的心智画布
③ 底层实现   struct 定义 / runtime 源码 / 关键汇编 / 内存布局图
④ Java 对照  同构点、差异点，以及「Go 为什么这样选」
⑤ 框架落地   这个知识点在 langchaingo / eino / MCP SDK / Gin 里长什么样
⑥ 踩坑清单   Code Review 可以直接照抄的检查项
⑦ 一页速记   Cheat Sheet，复习只看这一节
```

**反冗余公约**（决定这书能不能读完）：

1. 一个概念只在**一处**深挖到底，其它章节只给结论 + 一句「详见第 N 章」。
2. 语法细节只讲一次：第 3 章是全书唯一允许「罗列语法」的地方，之后全部问题驱动。
3. 每个知识点必须绑定一个「Go 的设计取舍」——追问一句 *为什么不是 Java 那样*，否则不许写。
4. Java 对照不做功能清单式罗列，只出现在**理解断层处**，即「用 Java 直觉会猜错的地方」。

---

## 1. 全局地图

```
                    ┌──────────────────────────────────────────────┐
                    │ 第九部分 收尾：综合项目 + 落地清单（49-50）    │
                    └────────────────────▲─────────────────────────┘
                                         │
                    ┌────────────────────┴─────────────────────────┐
                    │ 第八部分 源码精读（45-48）                    │
                    │ langchaingo · eino · MCP SDK · 读码方法论     │
                    └────────────────────▲─────────────────────────┘
                                         │ 全部依赖
    ╔════════════════════════════════════╧═════════════════════════╗
    ║ 第七部分 AI-Agent 应用架构（37-44）                            ║
    ║ LLM 客户端 · 工具系统 · MCP · Agent 循环 · RAG · 记忆          ║
    ║ 多 Agent 与持久化执行 · 生产化                                 ║
    ╚════════════════════════════════════╤═════════════════════════╝
                                         │ 用得上
        ┌────────────────────────────────┴─────────────────────────┐
        │ 第六部分 工程化（31-36）                                   │
        │ 模块版本 · 测试 · 可观测 · DI · 代码生成 · net/http 深潜   │
        ├──────────────────────────────────────────────────────────┤
        │ 第五部分 运行时与性能（26-30）                             │
        │ 编译链路 · GC · pprof/trace · 性能调优 · JSON 编解码        │
        ├──────────────────────────────────────────────────────────┤
        │ 第四部分 并发（17-25）                                     │
        │ goroutine · GMP · 抢占/netpoll · channel · select          │
        │ sync 原语 · context · 并发陷阱 · 结构化并发                │
        ├──────────────────────────────────────────────────────────┤
        │ 第三部分 数据结构与内存（11-16）                           │
        │ slice · map · string · 内存布局/逃逸 · 分配器 · unsafe     │
        ├──────────────────────────────────────────────────────────┤
        │ 第二部分 语言内核（4-10）                                  │
        │ 类型系统 · 接口底层 · 泛型 · error · 函数/defer · 零值      │
        │ 组合与插件化                                               │
        ├──────────────────────────────────────────────────────────┤
        │ 第一部分 心智迁移（1-3）                                   │
        └──────────────────────────────────────────────────────────┘
```

**三条阅读路线**（按时间预算自选）：

| 路线 | 时长 | 章节 | 适合 |
| --- | --- | --- | --- |
| 🏃 速通落地 | 1 周 | 1,3,4,5,7,11,12,17,20,23,25,32,36,37,38,40 | 立刻要上手写 Agent 服务 |
| 🚶 深度通关 | 4~6 周 | 全部 50 章顺序读 | 想彻底换了脑子里的世界观 |
| 🔬 源码攻坚 | 2 周 | 5,6,18,20,22,23,26,27,45,46,47,48 | 要为团队选型 / 造轮子 |

---

## 2. 章节清单

### 第一部分 · 心智迁移：Java → Go（1-3）

| # | 章节 | 你将真正搞懂 | 源码 / 框架坐标 |
| --- | --- | --- | --- |
| 1 | 从 Java 到 Go：心智模型重置 | Go 的 10 条设计价值观及对应语言决策；`if err != nil` 的三层解释；没有注解/容器带来的编译期-运行期边界哲学；用 Java 直觉最容易踩的 8 个坑 | `go/doc` 设计文档；Go 官方 FAQ |
| 2 | 工程骨架：module、目录布局与工具链 | `go.mod/go.sum` ↔ Maven 的映射；`internal/cmd/pkg` 由来；gofmt/vet/staticcheck 的分工；交叉编译与 `-ldflags -X`；多阶段构建 + distroless（镜像 100MB → 10MB） | 任意主流 Go 仓库（gin/eino 目录结构） |
| 3 | 语法极速通道 + 高频陷阱 | 全书唯一允许罗列语法的一章：多重返回值、`:=` 作用域、range 语义（1.22 循环变量）、可见性首字母、无 while、switch 无 fallthrough、struct tag、`struct{}` 的三种用途、`init` 顺序 | `go spec` 精简导读 |

### 第二部分 · 语言内核（4-10）

| # | 章节 | 你将真正搞懂 | 源码 / 框架坐标 |
| --- | --- | --- | --- |
| 4 | 类型系统：struct、嵌入与方法集 | 值语义与拷贝；指针/值接收者的铁律；嵌入 ≠ 继承（没有 override 多态链，方法提升与遮蔽）；**方法集如何决定接口满足**；`type A = B` vs `type A B` | `runtime/type.go`、`cmd/compile/internal/types` |
| 5 | 接口的底层：eface / iface / itab | 接口只是两个机器字；`itab` 缓存与 `getitab`；**「接口非 nil 但里面是 nil 指针」的真相**；空接口装箱分配；去虚拟化与内联；为什么 Go 社区说「接口由使用方定义」 | `runtime/iface.go`、`runtime/type.go`；langchaingo 的 `llms.Model` |
| 6 | 泛型：约束、type set 与实现原理 | type parameter / constraint / `~T`；Go 用 **GC shape stenciling + dictionaries**，既不是 C++ 单态展开也不是 Java 擦除，代价与收益；泛型方法为何不支持；**什么时候不该用泛型**；SDK 中的 `Tool[T]`/`Result[T]` | `cmd/compile/internal/types2`、`runtime` dictionary 实现；go-sdk |
| 7 | 错误处理哲学：error、wrap、panic 边界 | `error` 是值；哨兵错误 vs 自定义类型 vs `%w` 包装；`errors.Is/As/Unwrap/Join`；panic/recover 的正确使用边界（库不许 panic）；**重试分类学**：哪些错误该重试、哪些立刻放弃（Agent 必用） | `errors` 包；`x/sync/errgroup`；go-openai 的错误分层 |
| 8 | 函数、闭包与 defer/panic 实现 | 函数是一等公民；闭包捕获变量（1.22 前后差异）；defer 的三个坑（求值时机、循环内、命名返回值改写）；**open-coded defers** 如何把 defer 从 30ns 打到 1ns；`_panic` 链表与 `gopanic` 的恢复流程 | `runtime/panic.go`、`cmd/compile` defer 重写 |
| 9 | 零值哲学、指针 vs 值与「可选值」设计 | 零值可用性设计（`sync.Mutex`/`bytes.Buffer`）；nil 对 slice/map/chan/func/interface 的**五种不同含义**；指针 vs 值接收者与拷贝成本；可选值怎么表达（指针 / `Option[T]` / `omitempty` 的谎言）；与 Java 的 null 文化对照 | `encoding/json` tag 语义 |
| 10 | 组合优于继承：插件化与注册表设计 | 没有继承体系后如何做扩展点；**接口注册表 + 工厂**；装饰器/中间件链；依赖倒置在 Go 的写法；`plugin` 包为何几乎没人用；设计一个可热插拔的 Tool/Provider 注册系统（Agent 骨架） | Gin 中间件、eino 组件注册、kratos 插件 |

### 第三部分 · 数据结构与内存（11-16）

| # | 章节 | 你将真正搞懂 | 源码 / 框架坐标 |
| --- | --- | --- | --- |
| 11 | 数组与切片：header、扩容与共享陷阱 | slice header 三字段；**append 扩容公式（1.18+）**；共享底层数组导致的数据污染；切片泄漏（大数组只留小切片）；三索引切片 `s[a:b:c]`；与 `ArrayList` 的本质差异（没有对象头、没有视图保护） | `runtime/slice.go`、`growslice` |
| 12 | map 内部结构与 Go 1.24 Swiss Table | `hmap`/`bmap`、tophash、8 槽 bucket、负载因子 6.5、溢出桶、**渐进式扩容**；遍历随机性原理；delete 不释放内存；并发写 fatal 的原因；**1.24 Swiss Table 的组探测与小 map 优化** | `runtime/map.go`、`runtime/map_swiss.go` |
| 13 | string / []byte / rune 与零拷贝 | string 是不可变 header；UTF-8 与 rune；`len` vs 字符数；`strings.Builder`；**`unsafe.String` / `StringData` 零拷贝转换（1.20）**；四种拼接方式的基准对比；流式处理大 prompt | `strings`、`runtime/string.go`、`bytes` |
| 14 | 内存布局、对齐与逃逸分析 | 字段顺序与 padding；false sharing（cache line）；**逃逸分析的完整规则集**与 `-gcflags=-m` 实战；栈上分配收益；合并分配技巧；与 JVM 对象头 / TLAB 的对照 | `cmd/compile/internal/escape`；pprof heap |
| 15 | 内存分配器：mcache / mcentral / mheap | TCMalloc 思路三级缓存；size class；span 与 arena；tiny allocator；scavenger 与内存归还；`runtime.MemStats` 读法；与 JVM 分代/区域化的对照 | `runtime/malloc.go`、`runtime/mheap.go` |
| 16 | unsafe 与 cgo 的边界 | `unsafe.Pointer` 三条转换规则；`uintptr` 的 GC 陷阱；struct 布局重解释；零拷贝序列化；cgo 的真实成本（线程、栈、GC 交互）；何时该用（高性能编解码、FFI 到推理引擎），何时千万别用 | `unsafe` 包文档；sonic 的 unsafe 用法 |

### 第四部分 · 并发（17-25）——全书的心脏

| # | 章节 | 你将真正搞懂 | 源码 / 框架坐标 |
| --- | --- | --- | --- |
| 17 | goroutine 与栈：最便宜的「线程」 | 2KB 起始栈的 `g` 结构；栈增长（`morestack`）与连续栈拷贝；goroutine 泄漏与生命周期管理；**与 Java 平台线程 / 虚拟线程（Loom）正面对比**：内存模型、调度、阻塞语义、迁移能力 | `runtime/runtime2.go`（g/m/p）、`runtime/stack.go` |
| 18 | GMP 调度器原理 | G/M/P 三者关系与为什么需要 P；`runnext` + 本地队列 256；全局队列与 **work stealing**；handoff；sysmon；调度器 trace 解读；1.25 cgroup 感知 GOMAXPROCS；与 ForkJoinPool 的对照 | `runtime/proc.go`、`runtime/runtime2.go` |
| 19 | 抢占、网络轮询与 sysmon | 协作式抢占的死循环问题 → **1.14 基于信号的异步抢占**；安全点与栈扫描；netpoller（epoll/kqueue）如何把阻塞 IO 变成可调度；阻塞系统调用时的 M/P 交接；timer 实现演进 | `runtime/preempt.go`、`runtime/netpoll.go`、`runtime/time.go` |
| 20 | channel 原理与编排模式 | `hchan` 结构、环形缓冲、`sendq/recvq/sudog`、**直接移交（direct handoff）**；close 的三条规则；happens-before 与内存模型；无缓冲 channel 的同步本质；**编排模式**：pipeline / fan-out-fan-in / orchestrator-worker / 令牌桶 / 结果汇聚；何时该用 Mutex 代替 channel | `runtime/chan.go`；Agent 中工具结果汇聚、token 转发 |
| 21 | select 与超时控制 | select 实现（`pollorder`/`lockorder`、sudog 链）；随机公平性；`default` 非阻塞；**nil channel 永久阻塞（动态关闭分支的利器）**；`time.After` 泄漏陷阱 → `NewTimer/Stop`；循环 select 的退出范式 | `runtime/select.go`；net/http 超时链路 |
| 22 | sync 原语实现 | Mutex 内部：自旋、**饥饿模式（1ms 阈值）**、信号量唤醒；RWMutex 的读写计数与写者优先；WaitGroup、Once 快路径；**sync.Pool 的 private/shared/victim 三层与 GC 清理**；sync.Map 的 read/dirty/amended；`atomic` 泛型化（1.19）与 Go 的弱内存序 | `runtime/sync.go`、`runtime/mutex.go`、`runtime/sema.go`；Ants、fasthttp 池 |
| 23 | context：取消传播（Agent 的命脉） | 接口与树形结构；`cancelCtx`（children map / `propagateCancel`）、`timerCtx`、`valueCtx` 链与查找成本；`WithCancelCause`；`Canceled` vs `DeadlineExceeded`；**取消如何穿透 net/http、database/sql、gRPC**；「一次用户请求 → 多次 LLM 调用 + 并行工具 → 全链路超时」实战；与 ThreadLocal + `CompletableFuture.cancel` 对照 | `context/context.go`；任何 LLM SDK 的 ctx 语义 |
| 24 | 并发陷阱：竞态、泄漏、死锁、背压 | `-race` 实战；goroutine 泄漏的 4 种典型（无退出条件 / 无缓冲阻塞 / ctx 未取消 / 生产者快于消费者）；死锁与锁序；共享可变状态的设计出路（single writer / actor）；内存可见性（没有 volatile，但有 happens-before）；block/mutex profile | `golang.org/x/sync` 文档；线上事故复盘 |
| 25 | 结构化并发：errgroup、conc 与有界并发 | `errgroup.WithContext/SetLimit`；`x/sync/semaphore`；`sourcegraph/conc`（pool/iter/panic 捕获）；worker pool 三种写法对比；**批量 embedding 的并发编排实战**；优雅退出（signal → Shutdown → 等在途任务）；与 `CompletableFuture.allOf` + 线程池的严格对照 | `x/sync/errgroup`、`sourcegraph/conc`、`panjf2000/ants` |

### 第五部分 · 运行时与性能（26-30）

| # | 章节 | 你将真正搞懂 | 源码 / 框架坐标 |
| --- | --- | --- | --- |
| 26 | 编译链路：AOT 与 JIT 的路线之争 | `go build` 全流程（lexer → parser → typecheck → SSA → 机器码 → link）；内联阈值、边界检查消除、nil check 消除、PGO（1.21）；`go tool compile -S` 读汇编；静态二进制与毫秒级启动；**为什么 Go 不需要预热而 Java 需要**，代价是什么 | `cmd/compile`、`internal/ssa` |
| 27 | GC：三色标记、混合写屏障与调参 | 并发标记清扫、三色不变式与**混合写屏障（Dijkstra + Yuasa）**；mark assist；GC pacer 与 `GOGC`；`GOMEMLIMIT`（1.19）软限制；两次 STW；栈扫描与安全点；scavenger 与 RSS；**Green Tea GC（1.25，按 span 批量扫描）**；`gctrace` 观测；与 G1/ZGC/分代假说对照（Go 为何不做分代、不做压缩、不做对象移动） | `runtime/mgc.go`、`runtime/mbarrier.go` |
| 28 | pprof 与 trace 实战 | CPU/heap/goroutine/mutex/block profile 的采样原理；火焰图解读；execution trace 里 G 的状态机；`-benchmem` + `benchstat`；微基准的五个陷阱；线上 profile 的安全做法；**用它定位 Agent 的 token 流延迟与工具热点** | `net/http/pprof`、`runtime/trace` |
| 29 | 性能优化：分配、锁、批处理 | 分配优化（预分配 cap、对象复用、`sync.Pool`、避免装箱）；字符串与字节处理；锁优化三板斧（分片、原子、无锁读）；syscall 与 IO 批处理；内存对齐与缓存友好；**先测量后优化**的纪律与优化台账 | sonic、fasthttp、pprof 案例 |
| 30 | JSON 编解码：性能与协议设计 | `encoding/json` 的反射 + 字段缓存；tag 与 `omitempty` 的谎言；`RawMessage` 延迟解析；**`Decoder` 流式解码（JSONL / SSE）**；数字精度（`float64` vs `json.Number`）；**`encoding/json/v2`（1.25 实验）**；sonic/easyjson 的取舍；**LLM 结构化输出的严格解码（DisallowUnknownFields + schema 校验）** | `encoding/json`、sonic、jsonschema |

### 第六部分 · 工程化（31-36）

| # | 章节 | 你将真正搞懂 | 源码 / 框架坐标 |
| --- | --- | --- | --- |
| 31 | 模块、版本与 MVS | module 语义（包路径即 import 路径）；`go.sum` 校验；语义化版本与 `/v2`；**MVS 最小版本选择 vs Maven「最近者胜」**（冲突解决哲学完全不同）；`replace/exclude/retract`；`go.work`；私有仓库与 `GOPRIVATE`；`govulncheck` 与依赖治理 | `cmd/go/internal/modload`、`mvs` 包 |
| 32 | 测试：表驱动、mock、fuzz 与可重放 | 表驱动 + 子测试；`t.Cleanup/Helper/TempDir`；testify；接口 mock 与 gomock/mockery；`httptest` 与 Testcontainers-go；golden 文件；fuzz（1.18）；**LLM 依赖的录制回放测试（httptest + golden SSE）**；覆盖率与 CI 矩阵 | `testing`、`httptest`、testify |
| 33 | 可观测性：slog、OTel、Prometheus | `log/slog`（Handler / 分组 / 零分配技巧）；日志上下文与 trace id 传递；OpenTelemetry Go 三件套；**Agent 的语义约定：`llm.request`、`tool.call` span**；指标设计（延迟分位、token 计数、错误率）；采样与成本；与 Logback/Micrometer/Sleuth 对照 | `log/slog`、otel-go、client_golang |
| 34 | 依赖注入与装配：fx / wire / 手工 | 为什么 Go 社区主流是**显式手工装配**；functional options 模式；构造函数返回接口；uber/fx 与 dig；**wire 编译期 DI**；生命周期（OnStart/OnStop）；与 Spring `@Component/@Autowired` 的对照 | fx、wire、kratos 装配层 |
| 35 | 代码生成与反射：schema 自动化 | `go:generate` 工作流；stringer/mockgen/wire/protobuf 生成物如何融入 CI；`reflect` 的能力边界与成本；tag 解析；**从 struct 自动生成 JSON Schema 喂给 LLM 工具**；`go/ast` 入门（自研生成器）；反射 vs 生成的取舍 | `go/ast`、`invopop/jsonschema`、mockery |
| 36 | net/http 深潜：中间件、超时、SSE | Server 结构（accept → goroutine per conn → keep-alive）；Handler 即函数与中间件组合；超时四件套；优雅关停 `Shutdown`；Client/Transport 连接池（**`MaxIdleConnsPerHost=2` 经典坑**）；Body 必须关闭与连接复用；**SSE 流式（`Flush` / `ResponseController` / 反代缓冲）**；`ReverseProxy`；与 Tomcat 线程模型的对照 | `net/http`、`httputil`、Gin |

### 第七部分 · AI-Agent 应用架构（37-44）——本书的目的地

| # | 章节 | 你将真正搞懂 | 源码 / 框架坐标 |
| --- | --- | --- | --- |
| 37 | LLM 客户端：抽象、流式与韧性 | 多供应商抽象（`Model`/`Client` 接口设计）；SSE 解析实战（`bufio.Scanner` 64KB 坑、`data:` 分包、`[DONE]`）；结构化输出与 function calling 协议；**重试/退避/抖动/429 分类**；超时与 context 贯穿；连接复用与并发；token 计量与成本；prompt 缓存与降级 | `openai/openai-go`、`sashabaranov/go-openai`、langchaingo `llms` |
| 38 | 工具系统：Function Calling 全链路 | Tool 抽象与 **JSON Schema 自动生成 + 参数校验**；多轮调用协议；**并行工具执行的并发模型与超时**；结果截断与错误回传 LLM；幂等与副作用控制；租户隔离与鉴权；**沙箱（子进程 / 容器 / wasm）**；工具注册表与审计 | eino `ToolsNode`、go-sdk、`invopop/jsonschema` |
| 39 | MCP：协议与 Go SDK 实战 | JSON-RPC 2.0 与 transport（stdio / streamable-http）；capabilities 协商；tools / resources / prompts / sampling / elicitation；写一个 MCP Server 暴露内部能力；写 Client 聚合多个 MCP；命名空间冲突；**工具描述注入攻击与权限边界** | `modelcontextprotocol/go-sdk`、`mark3labs/mcp-go` |
| 40 | Agent 循环与编排引擎 | ReAct 循环的正确实现；消息与状态模型；终止条件与最大步数；Plan-and-Execute / Router / Reflection / Critic；**状态机 vs DAG 编排**；流式事件模型（token/tool/step）；HITL 中断与恢复；失败重规划 | langchaingo `agents`、eino `graph` |
| 41 | RAG 管线：从文档到上下文 | 解析与切分策略；embedding 批处理与并发；向量库接入（pgvector / qdrant / milvus）；向量 + 关键词混合检索；rerank；上下文拼装与窗口控制；增量索引与去重；多租户权限过滤；**评估（召回/命中）** | pgvector-go、qdrant/go-client、eino-ext |
| 42 | 记忆与会话：状态一致性 | 短期记忆（窗口 / 摘要）与长期记忆（事实抽取 / 向量）；会话存储（内存 / Redis / Postgres）；**并发安全与多实例一致性**；TTL 与清理；token 预算与消息裁剪；幂等写入；与 Java 侧 session/缓存体系对接 | go-redis、langchaingo `memory` |
| 43 | 多 Agent 协作与持久化执行 | 拓扑：supervisor / hierarchical / debate / handoff；通信与共享状态（消息总线 / channel）；资源预算与失败隔离；**长久运行任务：Temporal Go SDK 的 workflow/activity、确定性重放、signal/query**；工作流引擎 vs 自研状态机；事件驱动（Kafka / NATS） | temporal Go SDK、franz-go、nats.go |
| 44 | 生产化：多租户、限流、成本与部署 | 配额与多租户；限流（令牌桶 / 分布式限流）；熔断降级；缓存分层；成本核算与预算刹车；灰度与 prompt 版本管理；评估体系（离线集 + 线上反馈）；安全与内容过滤；SLO 告警；**Docker 多阶段 + distroless、K8s 探针、`GOMEMLIMIT` 与 limit 对齐** | kratos、sentinel-golang、otel |

### 第八部分 · 源码精读（45-48）

| # | 章节 | 你将真正搞懂 | 精读重点 |
| --- | --- | --- | --- |
| 45 | langchaingo 精读 | `schema` 抽象如何统摄全库；`llms` provider 适配层；chain 组合；`agents` executor 的循环与工具调度；callbacks 可观测钩子；**可借鉴处与可吐槽处** | `github.com/tmc/langchaingo` |
| 46 | eino 精读 | 字节的思路：组件 + 图编排 + **流式一等公民**；`ChatModel`/`ToolsNode`/`Graph`/`Chain`；流的传播与合并；checkpoint 与中断恢复；跨语言内核设计 | `github.com/cloudwego/eino` |
| 47 | MCP Go SDK 源码精读 | 协议类型定义与编解码；transport 抽象；server 端 dispatch 与并发模型；session 与能力协商；client 调用与超时；进度/日志通知；**如何扩展自定义 transport** | `modelcontextprotocol/go-sdk` |
| 48 | 读源码方法论 + 框架设计模式提炼 | 怎么读 Go 源码（`go doc`、tests 即文档、delve、`-gcflags`）；Go 世界高频的 8 个模式（functional options / middleware / registry / actor / pipeline / worker pool / errgroup / broker）；Gin、Kratos、go-zero 的装配与中间件对比；**把前 47 章织成一张网** | 横向对比 |

### 第九部分 · 收尾（49-50）

| # | 章节 | 你将真正搞懂 | 产出 |
| --- | --- | --- | --- |
| 49 | 综合项目：从零构建生产级 Agent 框架 | 目录与分层、core（消息/事件/模型/工具）、provider 适配、编排引擎（图 + 并发 + 中断恢复）、存储（会话/记忆/向量）、可观测、配置与测试策略 | **一个 3000 行以内、可运行、可扩展的框架** |
| 50 | Java 架构师的 Go 落地清单 | 别把 Java 习惯带过来（不要为分层而分层、不要过度抽象、接口由使用方定义）；**并发安全 CR 清单 20 条**；错误处理 / 性能 / 可观测 / API 演进清单；团队协作（gofmt + CI + PR 模板） | 可打印的 **Checklist 手册** |

---

## 3. Java ↔ Go 术语与概念映射速查

| Java | Go | 关键差异（会咬人的地方） |
| --- | --- | --- |
| 类 / 继承 | struct + 嵌入 | 没有继承与 override，方法提升不是多态 |
| interface（nominal） | interface（structural） | 隐式实现；接口应定义在使用方 |
| 虚方法表 | `itab`（接口）/ 直接调用 | 动态派发有成本，可被去虚拟化 |
| 泛型（擦除） | 泛型（GC shape + dictionary） | 支持基本类型特化，不支持泛型方法 |
| ArrayList | slice | 共享底层数组，append 可能重新分配 |
| HashMap | map | 不保证顺序、遍历随机、并发写直接 fatal |
| String（对象） | string（只读 header） | 不可变、UTF-8 字节长度 ≠ 字符数 |
| BigDecimal | `math/big` / decimal 库 | 没有内建高精度十进制 |
| 受检异常 | `error` 返回值 | 没有异常控制流，必须显式传递 |
| RuntimeException + `finally` | panic + defer | panic 只用于不可恢复的程序错误 |
| Thread / 虚拟线程 | goroutine | 2KB 栈、用户态调度、M:N |
| 线程池 / ForkJoinPool | channel + goroutine / errgroup / ants | 没有内建「池」，靠模式组合 |
| `BlockingQueue` | channel | 兼具同步与背压语义 |
| `Future` / `CompletableFuture` | channel + select + context | 没有内建 merge/组合子，靠 select |
| `ThreadLocal` | context（**不是等价物**） | 显式传递，禁止塞 struct |
| `synchronized` / `ReentrantLock` | `sync.Mutex`（不可重入） | **不可重入**，RWMutex 语义不同 |
| `AtomicInteger` / LongAdder | `atomic.Int64` / 分片计数 | 1.19 起泛型化，弱内存序 |
| `volatile` | 无 | 用 channel/atomic 建立 happens-before |
| JVM GC（G1/ZGC） | 并发标记清扫（非分代、非压缩） | 目标不同：低延迟优先、不看吞吐 |
| JIT 预热 | AOT 编译 | 启动快、峰值可能略低，靠 PGO 补 |
| Maven / Gradle | go mod / go.work | MVS 而非「最近者胜」 |
| Spring 容器 | 手工装配 / fx / wire | 默认显式，依赖注入是「写出来的」 |
| 注解 + AOP | struct tag + 代码生成 | 没有运行时注解与动态代理魔法 |
| Lombok | 无（IDE 补全 / 生成器） | 直接写 struct 和 getter 就够 |
| JUnit + Mockito | testing + testify + gomock | 表驱动是默认风格 |
| Logback + MDC | `log/slog` + ctx | 上下文靠显式传递 |
| Spring Boot Actuator | `/healthz` + prometheus + otel | 组合式，没有全家桶 |

---

## 4. 里程碑：每部分必须交付一个「能跑的东西」

不做纯阅读式学习。每读完一个部分，交付一个可运行产物，作为掌握度的验收：

| 部分 | 交付物 | 验收标准 |
| --- | --- | --- |
| 一 | `helloagent` 骨架 | 能编译、有 `cmd/`+`internal/`、CI 跑通 gofmt/vet/test |
| 二 | `toolkit` 小库 | 用接口 + 注册表实现 3 个可插拔 Tool，`error` 分层清晰 |
| 三 | 内存实验场 | 能画出 3 个 struct 的内存布局，讲清 3 处逃逸原因，跑出 slice 共享踩坑 demo |
| 四 | 并发编排器 | 有界并发 + ctx 超时 + 优雅退出的批量任务器，`-race` 全绿，附 trace 截图 |
| 五 | 性能报告 | 同一服务优化前后 pprof 火焰图对比 + GC 参数说明 + `benchstat` 数据 |
| 六 | 生产级 HTTP 服务 | 中间件、超时四件套、SSE 流式、OTel trace、K8s 探针就绪 |
| 七 | 可用 Agent | 能流式对话、调用 2 个工具、接 1 个 MCP Server、有记忆与 trace |
| 八 | 源码笔记 | 3 篇框架源码分析（每篇含调用链图 + 设计取舍判断） |
| 九 | 自研框架 | 第 49 章的框架 + 一份自己的 Go 规范文档 |

---

## 5. 进度清单

- [ ] 01 心智模型重置
- [ ] 02 工程骨架与工具链
- [ ] 03 语法极速通道 + 高频陷阱
- [ ] 04 类型系统：struct、嵌入与方法集
- [ ] 05 接口的底层：eface / iface / itab
- [ ] 06 泛型：约束、type set 与实现原理
- [ ] 07 错误处理哲学
- [ ] 08 函数、闭包与 defer/panic 实现
- [ ] 09 零值哲学、指针 vs 值与可选值设计
- [ ] 10 组合优于继承：插件化与注册表
- [ ] 11 数组与切片
- [ ] 12 map 内部结构与 1.24 Swiss Table
- [ ] 13 string / []byte / rune
- [ ] 14 内存布局、对齐与逃逸分析
- [ ] 15 内存分配器
- [ ] 16 unsafe 与 cgo 的边界
- [ ] 17 goroutine 与栈
- [ ] 18 GMP 调度器原理
- [ ] 19 抢占、netpoll 与 sysmon
- [ ] 20 channel 原理与编排模式
- [ ] 21 select 与超时控制
- [ ] 22 sync 原语实现
- [ ] 23 context：取消传播
- [ ] 24 并发陷阱：竞态、泄漏、死锁、背压
- [ ] 25 结构化并发：errgroup/conc/有界并发
- [ ] 26 编译链路：AOT vs JIT
- [ ] 27 GC：三色标记与调参
- [ ] 28 pprof 与 trace 实战
- [ ] 29 性能优化：分配、锁、批处理
- [ ] 30 JSON 编解码
- [ ] 31 模块、版本与 MVS
- [ ] 32 测试：表驱动、mock、fuzz
- [ ] 33 可观测性：slog/OTel/Prometheus
- [ ] 34 依赖注入与装配
- [ ] 35 代码生成与反射
- [ ] 36 net/http 深潜
- [ ] 37 LLM 客户端：抽象、流式与韧性
- [ ] 38 工具系统：Function Calling
- [ ] 39 MCP：协议与 Go SDK
- [ ] 40 Agent 循环与编排引擎
- [ ] 41 RAG 管线
- [ ] 42 记忆与会话
- [ ] 43 多 Agent 协作与持久化执行
- [ ] 44 生产化
- [ ] 45 langchaingo 精读
- [ ] 46 eino 精读
- [ ] 47 MCP Go SDK 精读
- [ ] 48 读源码方法论与设计模式
- [ ] 49 综合项目：Agent 框架
- [ ] 50 Java 架构师落地清单

---

## 6. 写作与仓库约定

- **文件命名**：`序号-英文短横线slug.md`（如 `20-channel-internals.md`），保证 git、URL、终端友好；标题用中文写在文件内。想改成中文文件名随时可批量重命名。
- **代码约定**：基于 Go 1.24+；demo 一律可 `go run` 运行；文中对比用 `// Java:` 与 `// Go:` 注释块。
- **交叉引用**：`详见第 20 章`，避免任何重复讲解。
- **每章结尾必备**：`一页速记`（表格）+ `自检题`（3 题，含答案要点）+ `延伸阅读`（源码路径/文档链接）。
- **提交规范**：`docs(go): 第 N 章 …`，一章一提交，方便 review 与回滚。
- **状态标记**：每章文件开头有 `> 状态：待撰写 / 草稿 / 完成`，便于看板式推进。

---

## 7. 需要你拍板的 4 个点（我先按默认值写）

| # | 议题 | 默认方案 |
| --- | --- | --- |
| 1 | 文件名语言 | 英文 slug（可一键改中文） |
| 2 | 单章篇幅 | 精读型，2500~5000 字 + 2~4 个可运行 demo；宁可少讲不重复讲 |
| 3 | demo 代码放哪 | `go/examples/chNN/` 独立可运行目录，正文贴关键片段 |
| 4 | Agent 实战的技术栈 | LLM: openai-go / ollama；框架: eino + langchaingo 双线对照；MCP: 官方 go-sdk；编排: Temporal；存储: pgvector + Redis |

> 你审完这份大纲，指出「要加/要删/要调顺序/要加深」的地方，我再动笔写正文。

