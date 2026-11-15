# 49. eino 精读

> **状态**：`已完成（初稿）` ｜ **所属**：第八部分 · 源码精读 ｜ **编号**：49 / 54
> **本章定位**：LangChain 教会世界「组件 + 链」，eino 把它重做成「组件 + 图 + **流是一等公民**」。这一章读的是**编排引擎**——一个把 LLM 应用从「手写 for 循环」变成「可编译、可流式、可中断恢复的图」的东西。
> **读法**：先看 49.2 的两张图建立坐标系，再顺着 49.3 的六段源码往下读。卡住就跳到 49.5 跑一遍代码再回来。
> **版本基准**：`github.com/cloudwego/eino` v0.9.x（2026 年主线，含 `adk/` 目录）。eino 迭代极快，本文章的字段名与函数名以你本地 `go doc github.com/cloudwego/eino/compose` 为准。

---

## 49.1 引子：第四个人不敢改的那个函数

一个真实演进过程，几乎每个 Go 团队都走过一遍：

```text
v1（第 1 周）：func chat(ctx, q) string         // 一次模型调用，40 行
v2（第 3 周）：+ 工具调用                      // 加了个 if len(msg.ToolCalls)>0 的循环
v3（第 5 周）：+ 流式打字机                    // chat 变成 streamChat，多了一个 chan
v4（第 7 周）：+ 两个工具并行                  // 多了 WaitGroup + 结果合并
v5（第 9 周）：+ 人工审批中断、刷新页面能恢复  // 多了一张 task_state 表和一把锁
v6（第 11 周）：+ RAG 检索节点复用             // 有人开始复制粘贴 200 行
```

到 v6，那个函数已经 800 行，里面同时住着：一个 `for` 循环状态机、三个 `chan string` 的扇出扇入、两份几乎一样但改过一个 bug 的流式拼接代码、一张 `task_state` 表、以及一段「重新跑一遍会重复扣费」的续跑逻辑。

> 🩸 **事故原型**：用户在审批页点了「拒绝」，前端把 `resume` 打到了另一台实例，那台实例的内存里没有中间状态，于是整个 Agent 从头跑了一遍——工具再执行一次，订单被下了两次。

无论是 LangChain 的 `Chain`、LangChain4j 的 `AiServices`，还是你自己写的 `for` 循环，都缺同一个东西：**把「流程」从「业务代码」里抽出来，变成一个可以被编译器检查、被引擎调度、被存储快照的独立对象**。

eino 的答案就是这一章要读的东西：**Component 定义能力，Compose 定义流程，Runnable 定义它们的公共接缝。**

---

## 49.2 直觉模型：三层 + 一个四象限

### 三层架构（先建立坐标系）

```text
┌────────────────────────────────────────────────────────────────────┐
│ 第三层  ADK / Flow          开箱即用的 Agent 形态                   │
│   adk/            ChatModelAgent · DeepAgent · Runner · Interrupt  │
│   flow/agent/react/  ReAct Agent  · multiagent/host/ 多 Agent 主持 │
│   ─────────────────────────────────────────────────────────────    │
│ 第二层  Compose             「画图 → 编译 → 执行」                  │
│   graph.go      nodes / dataEdges / controlEdges / branches        │
│   graph_run.go  runner · channelManager · taskManager · checkPointer│
│   pregel.go     Pregel 超步执行 · dag.go  DAG 依赖执行              │
│   ─────────────────────────────────────────────────────────────    │
│ 第一层  Component           每种能力一个接口 + N 个实现              │
│   components/model     BaseChatModel · ToolCallingChatModel        │
│   components/tool      BaseTool · InvokableTool · StreamableTool   │
│   components/retriever / embedding / indexer / prompt / document   │
│   schema/        Message · StreamReader · ToolInfo · Document      │
└────────────────────────────────────────────────────────────────────┘
        ↑ 所有层共用两个横切设施：callbacks（切面）· checkpoint（快照）
```

### 四象限：`Runnable` 是全库唯一的接缝

任何一个「组件」加入图，都会被包装成同一个接口。它按「输入是不是流 / 输出是不是流」划成四个方法：

| 方法 | 输入 | 输出 | 别名（业界叫法） | 典型实现者 |
| --- | --- | --- | --- | --- |
| `Invoke` | 值 | 值 | 同步阻塞 | 所有组件都能有 |
| `Stream` | 值 | **流** | server-streaming | ChatModel（token 流） |
| `Collect` | **流** | 值 | client-streaming | 需要聚合输入的节点 |
| `Transform` | **流** | **流** | bidi / 增量转换 | ASR→TTS 串联、过滤器 |

**关键设计（这一章的第一个胜负手）**：组件**不需要**实现全部四个方法。只要实现任意一个，框架就用「流化（streaming）」和「拼接（concat）」两把扳手把剩下三个补齐：

```text
         ┌──────── 补齐方向 ────────┐
 Invoke ──┤ 由 Stream + Concat 拼出  │
 Stream ──┤ 由 Invoke + 装箱(boxing) │
 Collect──┤ 由 Invoke + Concat      │
 Transform┤ 由 Invoke + 展开        │
```

> 💡 **一句话记住**：eino 的组件契约是「**你只需要做好你最擅长的那一种流范式，剩下的引擎兜底**」。这在 Java 里几乎见不到——Java 框架习惯让你实现一整族接口（`StreamingChatModel` / `ChatModel` 二选一，然后适配器爆炸）。

---

## 49.3 底层实现：六段源码

### 49.3.1 目录树（认路，不要背）

```text
eino/
├── schema/                 # 所有层共用的数据契约
│   ├── message.go          # Message / ToolCall / Role
│   ├── stream.go           # StreamReader / StreamWriter / Pipe / Copy
│   └── document.go
├── components/             # 第一层：能力接口（不含实现，实现放 eino-ext）
│   ├── model/interface.go  # BaseChatModel / ToolCallingChatModel
│   ├── tool/interface.go   # BaseTool / InvokableTool / StreamableTool
│   │   └── utils/          # InferTool：普通 func → Tool
│   └── retriever/ embedding/ indexer/ prompt/ document/
├── compose/                # 第二层：编排引擎（本章重点）
│   ├── graph.go            # graph struct / AddEdge / AddBranch / Compile
│   ├── graph_run.go        # runner 主循环
│   ├── graph_manager.go    # channelManager / taskManager
│   ├── pregel.go dag.go    # 两种执行模型
│   ├── runnable.go         # Runnable 接口 + 四范式补齐
│   ├── tool_node.go        # ToolsNode：并行执行多个工具调用
│   ├── checkpoint.go       # 快照结构 + CheckPointStore
│   ├── interrupt.go        # Interrupt / StatefulInterrupt / CompositeInterrupt
│   └── stream_concat.go    # 流合并、分叉
├── callbacks/              # 横切面：五时机 Hook
├── flow/agent/react/       # 第三层：ReAct 预置编排
└── adk/                    # Agent Development Kit（2025 后新增）
    ├── runner.go turn_loop.go   # Agent 运行时 + 多轮循环
    ├── interrupt.go             # 人机中断
    └── prebuilt/deep/ planexecute/ supervisor/
```

### 49.3.2 源码精读 ①：`Runnable` 与四范式补齐

`compose/runnable.go` 全文最要紧的就这段（原文含注释）：

```go
// Runnable is the interface for an executable object. Graph, Chain can be
// compiled into Runnable.
// runnable is the core conception of eino, we do downgrade compatibility for
// four data flow patterns, and can automatically connect components that only
// implement one or more methods.
type Runnable[I, O any] interface {
	Invoke(ctx context.Context, input I, opts ...Option) (output O, err error)
	Stream(ctx context.Context, input I, opts ...Option) (output *schema.StreamReader[O], err error)
	Collect(ctx context.Context, input *schema.StreamReader[I], opts ...Option) (output O, err error)
	Transform(ctx context.Context, input *schema.StreamReader[I], opts ...Option) (output *schema.StreamReader[O], err error)
}
```

注意 `I, O` 是**类级泛型**，不是方法级泛型。这是 Go 泛型的硬限制（无泛型方法，详见第 6 章），也直接决定了 eino 的整个形态：`Runnable` 必须有类型参数，所以**图必须写成 `NewGraph[In, Out]`，一张图一个输入类型一个输出类型**。

补齐逻辑的落点是 `composableRunnable` + `runnablePacker`：

```go
// 摘录要点，非完整源码
type composableRunnable struct {
	i invoke                        // 统一函数签名：any 进 any 出
	t transform
	inputType, outputType, optionType reflect.Type
	nodeInfo *nodeInfo              // 只有在图里当节点时非 nil
}

func invokeByStream[I, O, TOption any](s Stream[I, O, TOption]) Invoke[I, O, TOption] {
	return func(ctx context.Context, input I, opts ...TOption) (output O, err error) {
		sr, err := s(ctx, input, opts...)
		if err != nil { return output, err }
		return defaultImplConcatStreamReader(sr)   // 流 → 值：把 chunk 焊起来
	}
}
```

**逐行判断**：

- `composableRunnable` 把泛型抹平成 `func(ctx, any, ...any) (any, error)`，同时**保留 `reflect.Type`**。这是「泛型给用户，反射给引擎」的经典分工：用户在编译期拿到类型安全，引擎用反射做节点间连线时的类型校验（`Compile` 阶段报错「node A 的输出类型接不住 node B 的输入」）。
- 每个组件实例对应一个 `composableRunnable` 实例——注释写得很直白：「one instance corresponds to one instance of the executable object」。**推论**：组件实例是共享的，图是并发安全的；但**你组件内部的字段就是共享状态**。所以自定义 ChatModel 里放一个可变 `map` 缓存 = 定时炸弹（详见 49.6）。
- 这层抹平是有代价的：每一次跨节点传递都要过一次 `any` 装箱 + 类型断言，热路径上会多出分配。取舍是「先能编排，再谈极致性能」，跟你自己写死代码比，图的每节点开销大概在百纳秒级——对 LLM 应用（一次调用 500ms～5s）完全可忽略。

### 49.3.3 源码精读 ②：`graph` 与两种执行模型

```go
type graph struct {
	nodes        map[string]*graphNode
	controlEdges map[string][]string          // 控制流：先执行谁
	dataEdges    map[string][]string          // 数据流：谁的数据喂给谁
	branches     map[string][]*GraphBranch    // 条件跳转（挂在节点上）
	startNodes, endNodes []string

	stateType      reflect.Type               // WithGenLocalState 才有
	expectedInputType, expectedOutputType reflect.Type
	compiled bool                              // 编译后禁止再改：ErrGraphCompiled
}
```

两个细节值得单独拎出来：

1. **`controlEdges` 与 `dataEdges` 分离**。这是 `Workflow` 能「映射字段」而 `Graph` 只能「传整个值」的根：Workflow 用 `FieldMapping` 表达数据流（`FieldMapping.FromField("a").ToFieldPath("b")`），控制流照样走边。
2. **两种运行时**：

```go
const (
	runTypePregel graphRunType = "Pregel" // 可以有环；兼容 AnyPredecessor 触发
	runTypeDAG    graphRunType = "DAG"    // 无环；兼容 AllPredecessor 触发
)
```

`Pregel` 这个名字直接泄露了设计来源：Google 的 **Pregel 超步（superstep）模型**。`compose/pregel.go` 里 `pregelChannel` 就是「超步之间传值」的那块黑板：

```go
type pregelChannel struct {
	Values map[string]any          // 本超步所有上游的产出
	mergeConfig FanInMergeConfig
}
// get()：取出所有值 → 1 个直接返回，多个走 mergeValues 合并 → 清空黑板
```

**设计取舍判断**：

- 用 Pregel 超步而不是「每个节点一个 goroutine 自由跑」，换来的是**确定性**：每一步的输入集合是可枚举的，所以可以做 checkpoint（快照的就是「超步边界上的黑板状态」）与「重放」。这正是 LangGraph 同样选择 Pregel 的原因——**能中断恢复的图，必须先有确定性**。
- 代价是**延迟**：超步之间有栅栏（barrier），一个慢节点拖住整轮。想快就得把独立逻辑拆成同一轮里的并行节点，而不是指望「下游先跑起来」。
- 允许有环（`runTypePregel`）就意味着必须配 `WithMaxRunSteps(n)`，否则模型一直要工具 = 死循环到天亮。

### 49.3.4 源码精读 ③：流为什么不是 `chan T`

`schema/stream.go` 里，`StreamReader` 是**一套 reader 的多态封装**，下面是它的一种实现：

```go
type stream[T any] struct {
	items  chan streamItem[T]
	closed chan struct{}          // 接收方主动关 → 通知发送方别再发了
	automaticClose bool
}
type streamItem[T any] struct { chunk T; err error }

func (s *stream[T]) send(chunk T, err error) (closed bool) {
	select { case <-s.closed: return true; default: }   // 已关，直接放弃
	select {
	case <-s.closed: return true
	case s.items <- item: return false
	}
}
```

再看公开 API 层：`schema.Pipe[T](cap)` 返回一对 `(reader, writer)`；`sr.Copy(n)` 把一个流**复制成 n 份独立消费**（原 reader 立刻失效）；`schema.ErrNoValue` 让 `StreamReaderWithConvert` 可以「静默丢 chunk」（过滤空包最常用）；`SourceEOF` 让 `MergeNamedStreamReaders` 能告诉你是哪条源流结束了。

**为什么不用裸 `chan T`？** 三个理由，全部来自真实需求：

| 需求 | 裸 `chan T` | `StreamReader` |
| --- | --- | --- |
| 传输错误（流到一半 429） | 得再开一路 err channel 或塞结构体 | `Recv()` 直接返回 `(chunk, err)` |
| 一条流喂两个消费者（callback 遥测 + 下游节点） | 消费者互相抢 chunk，必须自己扇出 | `Copy(2)` 一行，语义明确 |
| 上游提前退出，别把发送方憋死 | 接收方 close 后发送方 panic / 泄漏 | `closed` 通道让 sender 的 `Send` 返回 `true` 自行收摊 |

第二条尤其重要：**eino 在注入 callback 时会对流做 `Copy`**，于是「观测」和「业务」各自消费一份，互不干扰。这也是为什么你写自定义 Streamable 组件时，**绝不能把收到的 `*StreamReader` 存起来或二次 `Recv`**——它是 read-once 的。

> ⚠️ 注意 eino 的流**没有背压协商**（不像 Java 的 `Flow.Publisher` 有 `request(n)`），只有 `Pipe(cap)` 的固定容量。容量小 = 上游阻塞；容量大 = 内存与延迟。给前端做 SSE 时，`cap` 建议 1～8，靠下游消费速度自然形成背压（详见第 39 章）。

### 49.3.5 源码精读 ④：checkpoint 与中断恢复

这是 eino 最「不像 Java」的部分——它把「暂停一个正在执行的函数」做成了框架能力。

```go
type checkpoint struct {
	Channels       map[string]channel      // 每个边的通道内容（黑板快照）
	Inputs         map[string]any          // 待喂给某节点的输入
	State          any                     // WithGenLocalState 的全局状态
	SkipPreHandler map[string]bool
	RerunNodes     []string                // 中断后需要重跑的节点
	SubGraphs      map[string]*checkpoint  // 子图快照（递归）
	InterruptID2Addr  map[string]Address   // 中断点 ID → 执行地址
	InterruptID2State map[string]core.InterruptState
}
type CheckPointStore = core.CheckPointStore   // Get/Set 两方法的纯 KV 接口
```

执行流（这是本章第三张图，务必看懂）：

```text
Invoke/Stream
   │
   ├─► runner 每完成一个超步 ──► checkPointer.set(id, checkpoint) ──► Store(内存/Redis)
   │
   ├─► 节点内调用 compose.Interrupt(ctx, info) / StatefulInterrupt(ctx, info, state)
   │        └─► 返回 *core.InterruptSignal（就是个 error！）
   │              └─► 引擎截获 → 落盘快照 → 把 err 抛给调用方
   │
   └─► 调用方拿到 err：
          info, ok := compose.ExtractInterruptInfo(err)     // 给 UI 展示：审批什么？
          // ……人类点了「同意」……
          runnable.Invoke(ctx, in,
              compose.WithCheckPointID(id),                  // 从哪份快照续
              compose.ResumeWithData(ctx, interruptID, data),// 把审批结果塞回去
          )
                 │
                 └─► 引擎读快照 → 跳到既有地址 → 节点内 GetInterruptState[T](ctx) 拿到审批结果
```

**三个判断**：

- **中断用 `error` 表达**，不用 panic、不用 goroutine 挂起。因为 Go 没有 continuation（详见第 17 章），「把栈存起来下次接着跑」是做不到的；eino 的做法是**把栈换成快照**——`error` 沿返回值一路冒上来，引擎在超步边界存盘。这就是「用数据流替代控制流」的极端应用（对照第 1 章 1.3 的三层解释）。
- **`CompositeInterrupt`** 是给 `ToolsNode` 这类「一个节点里并行跑 N 个子任务」用的：N 个工具里有 2 个要审批，就把 2 个中断错误捆成一个，引擎再拆成一串可恢复点。**没有它，一个节点只能有一个中断点**，多工具审批就实现不了。
- **序列化是个坑**：`checkpoint.go` 顶部有一串 `schema.RegisterName[*checkpoint](...)、RegisterName[*dagChannel](...)、RegisterName[*pregelChannel](...)`，`internal/serialization` 里维护「类型名 ↔ `reflect.Type`」双向表。原因是快照里塞着 `any`/接口值，**没有类型注册就无法还原具体类型**。历史版本用 `encoding/gob`，当前主线是自研 `internalStruct` + sonic 转 JSON（可读性更好）；无论哪种，**你自己塞进 State 的类型必须注册**，否则运行时才报 `unknown type`。

### 49.3.6 源码精读 ⑤：callback 的五个时机

`callbacks.Handler` 的方法集合就是「切面位点」全集：`OnStart` / `OnEnd` / `OnError` / `OnStartWithStreamInput` / `OnEndWithStreamOutput`。`callbacks.RunInfo` 告诉你「我是谁」（`Name` 节点名、`Type` 组件类型、`Component` 组件标识），input/output 告诉你「发生了什么」。

工程含义：**Trace / 埋点 / token 计量 / 首 token 延迟统计，全部挂在这五个钩子上，不侵入业务节点**。这就是 Java AOP 的位点，只是用函数链实现（详见第 10 章、第 35 章）。

### 49.3.7 跨语言内核：一个需要谨慎的判断

公开仓库的 `internal/` 分层透露了设计意图：

```text
internal/core/      address.go · interrupt.go · resume.go   ← 与 Go 无关的「执行语义」
internal/serialization/                                     ← 类型注册与快照编解码
compose/                                                    ← Go 侧前端：画图 API + 组件适配
```

也就是说，eino 把「图 → IR（节点/边/通道/任务 + 中断地址）」和「执行内核」切开，内核只吃不透明数据（`any` + `streamReader` 接口）与一个地址体系（`Address`/`AddressSegment`）。**理论上这套内核可以用 C++/Rust 重写，Go 只留前端 API 与组件适配**——字节内部分享里提过用跨语言内核复用执行语义、统一多语言 SDK 的思路。

> ⚠️ **保守结论**：截至 v0.9.x，**公开的 `cloudwego/eino` 是纯 Go 实现（无 cgo）**，你能读、能改、能 fork。跨语言内核属于路线/内部实践层面，不要在产品评审上拿它当既有能力。真正的硬阻碍也很清楚：快照序列化依赖 Go 的类型注册表，**换语言 = 换一套快照格式**；流是 `StreamReader` 接口，跨 FFI 传流的水位与取消语义都要重新设计。所以你判断这件事时，看「快照格式是否语言无关」就够了。

---

## 49.4 Java 对照：只讲会猜错的地方

| 你会猜的 | Go/eino 的真相 | 为什么 |
| --- | --- | --- |
| 「`ChatModel` 接口应该像 Spring 那样，提供方定义、使用者注入」 | eino 的接口由**框架**定义（`components/model`），实现放 `eino-ext`，使用者只依赖接口 | 编排要类型检查，接口必须全局唯一；这违反「接口由使用方定义」，但对框架是必要的例外 |
| 「流应该是 `Flow.Publisher<Message>`，支持背压与 `request(n)`」 | 是 `*schema.StreamReader[Message]`，一次性消费，容量固定的 `Pipe` | Go 不需要 Reactive Streams 的线程模型，但**代价是没有背压协商**（见 49.3.4） |
| 「中断恢复应该用 `CompletableFuture.cancel` + 数据库状态」 | 中断是 `error`，恢复是「从快照 + 地址重放」 | Go 没有 continuation；LangGraph 的 Python 版靠异常 + 状态图，思路一致 |
| 「`Chain` 应该是 `List<Component>` 然后 for 循环执行」 | `Chain` 只是 `Graph` 的语法糖（`AppendXXX` 内部建边），最终都编译成同一个 `Runnable` | 一套执行引擎，避免两套语义。**这也是它比 LangChain 干净的地方** |
| 「泛型这么用，会不会像 Java 一样擦除」 | Go 是 GC shape + dictionary 实例化（第 6 章），`Runnable[In, Out]` 是编译期真实的类型 | 所以图能编译期做类型校验——Python 版要等运行到第 40 分钟才炸 |
| 「组件实例可以随便共享，像 Spring 单例」 | 图编译产物并发安全；**组件内部可变字段不安全** | `composableRunnable` 一个实例对应一个组件实例，多请求共享 |

---

## 49.5 工程落地：三段可直接抄的代码

### ① 自定义 ChatModel（接一个内部模型服务）

```go
package main

import (
	"context"
	"errors"
	"io"
	"time"

	"github.com/cloudwego/eino/components/model"
	"github.com/cloudwego/eino/schema"
)

// 只实现 Generate + Stream；Collect / Transform 由引擎补齐。
type myModel struct {
	cfg    Config
	client *httpClient // 无状态，可被多 goroutine 共享
}

func (m *myModel) Generate(ctx context.Context, in []*schema.Message,
	opts ...model.Option) (*schema.Message, error) {

	opt := model.GetCommonOptions(nil, opts...) // 版本差异：也可能叫 model.GetOptions
	resp, err := m.client.Chat(ctx, toWire(in), opt.Temperature)
	if err != nil {
		return nil, err
	}
	return &schema.Message{
		Role:    schema.Assistant,
		Content: resp.Text,
		ToolCalls: resp.ToolCalls, // 有工具调用时必须透传，否则 ToolsNode 收不到
	}, nil
}

func (m *myModel) Stream(ctx context.Context, in []*schema.Message,
	opts ...model.Option) (*schema.StreamReader[*schema.Message], error) {

	sr, sw := schema.Pipe[*schema.Message](8)
	go func() {
		defer sw.Close()                       // 铁律：一定要关，否则下游永久阻塞
		ch, err := m.client.ChatStream(ctx, toWire(in))
		if err != nil {
			sw.Send(nil, err)                  // 错误走流，不要吞
			return
		}
		for {
			select {
			case <-ctx.Done():
				sw.Send(nil, ctx.Err())        // 取消穿透到这里
				return
			case chunk, ok := <-ch:
				if !ok { return }
				sw.Send(&schema.Message{Role: schema.Assistant, Content: chunk.Text}, nil)
			}
		}
	}()
	return sr, nil
}

var _ model.BaseChatModel = (*myModel)(nil)
```

要支持工具绑定，就去实现 `model.ToolCallingChatModel` 的 `WithTools(tools []*schema.ToolInfo) (ToolCallingChatModel, error)`——注意它是**返回新实例**的不可变风格；老的 `ChatModel.BindTools` 会就地改状态，官方注释已明确「same instance used concurrently 会 race」。

### ② ReAct 图：模型 → 工具 → 模型

```go
graph := compose.NewGraph[[]*schema.Message, *schema.Message]()
_ = graph.AddChatModelNode("model", cm)           // cm 是 ToolCallingChatModel
_ = graph.AddToolsNode("tools", toolsNode)        // compose.NewToolNode(ctx, &ToolsNodeConfig{Tools: ...})
_ = graph.AddLambdaNode("check", compose.InvokableLambda(guard)) // 可选的确定性护栏

_ = graph.AddEdge(compose.START, "check")
_ = graph.AddEdge("check", "model")

// 分支挂在「节点」上：这个节点做完了自己决定下一步去哪
_ = graph.AddBranch("model", compose.NewGraphBranch(
	func(ctx context.Context, in *schema.Message) (string, error) {
		if len(in.ToolCalls) > 0 { return "tools", nil }
		return compose.END, nil
	},
	map[string]bool{"tools": true, compose.END: true},
))
_ = graph.AddEdge("tools", "model")               // 工具结果回到模型 → 天然形成环

r, err := graph.Compile(ctx,
	compose.WithMaxRunSteps(20),                   // 有环必配，否则死循环
	compose.WithCheckPointStore(store),            // 中断恢复的前提
)
if err != nil { return err }                       // 类型/拓扑错误在这里就暴露（不是运行时）
```

消费流式结果：

```go
sr, err := r.Stream(ctx, []*schema.Message{schema.UserMessage(q)})
if err != nil { return err }
defer sr.Close()                                   // read-once，必须关
for {
	msg, err := sr.Recv()
	if errors.Is(err, io.EOF) { break }
	if err != nil { return err }
	pushToSSE(msg.Content)                         // 增量上屏（第 39 章）
}
```

### ③ 人机审批：中断 + 恢复

```go
// 工具内部：要先审批就中断
func refundTool(ctx context.Context, args string) (string, error) {
	if needApproval(args) {
		// StatefulInterrupt：info 给 UI 看，state 给恢复后的自己看
		return "", compose.StatefulInterrupt(ctx,
			&ApprovalInfo{Action: "refund", Args: args},
			&refundState{OrderID: parse(args)},
		)
	}
	return doRefund(ctx, args)
}

// 调用侧
out, err := r.Invoke(ctx, in, compose.WithCheckPointID(taskID))
if info, ok := compose.ExtractInterruptInfo(err); ok {   // 内部就是 errors.As 到中断信号
	saveForUI(taskID, info)                      // 展示审批卡片
	return
}
if err != nil { return err }
// ……第二天用户点了同意（换了一台实例也 OK，状态在 Store 里）……
out, err = r.Invoke(ctx, in,
	compose.WithCheckPointID(taskID),
	compose.ResumeWithData(ctx, approvalID, &ApprovalReply{Approved: true}),
)
// 工具内：state := compose.GetInterruptState[*ApprovalReply](ctx)
```

> 💡 这段代码就是 eino 存在的全部理由：**跨进程、跨重启、跨实例的人机协作**，用 200 行框架代码换掉了你那张 `task_state` 表和 800 行状态机。

---

## 49.6 踩坑清单（CR 可直接抄）

| # | 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- | --- |
| 1 | 下游节点永远卡住 | 自定义流式组件忘了 `sw.Close()` | `defer sw.Close()`，且错误用 `sw.Send(nil, err)` 传出 | 49.3.4 |
| 2 | 同一请求 callback 收到的 chunk 少一半 | 一个 `StreamReader` 给了两个消费者 | 消费前 `sr.Copy(n)`；记住 read-once | 49.3.4 |
| 3 | 高并发下数据串号 / `-race` 报警 | 组件实例字段被多请求共享（图并发安全 ≠ 组件安全） | 组件无状态；状态走 `WithGenLocalState` / 请求级参数 | 49.3.2 |
| 4 | 有环图跑不完，账单爆炸 | 没用 `WithMaxRunSteps` | 编译时必配步数上限 + 预算刹车（第 47 章） | 49.3.3 |
| 5 | `unknown type: xxx` 恢复失败 | checkpoint 里有未注册的自定义类型 | `schema.RegisterName[T]("my_type")`，并在启动时统一注册 | 49.3.5 |
| 6 | 恢复后副作用又执行一次（重复扣费） | 快照粒度是超步边界，整步重放 | 副作用必须幂等 / 先查后写；审批类工具单独节点化 | 49.3.5 |
| 7 | `Compile` 报类型不匹配 | 相邻节点输出/输入类型不同，且没插转换 Lambda | 插 `AddLambdaNode` 做显式转换，或用 `Workflow` 字段映射 | 49.3.3 |
| 8 | 升级后编译不过 | eino 迭代快：`BindTools`→`WithTools`、`ChatModel`→`ToolCallingChatModel` | 锁版本号、盯 CHANGELOG、升级前跑集成测试 | 49.5① |
| 9 | 流式下工具参数被直接上屏 | 模型流里混着 tool call delta | 在 callback 里按 `RunInfo` 区分内容类型，过滤 tool delta | 49.3.6 |
| 10 | 中断恢复后 UI 卡在「处理中」 | 只 `Interrupt` 没落 `CheckPointStore` | `WithCheckPointStore` 是编译期选项，必须配 | 49.3.5 |

---

## 49.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 三层 | Component（能力）→ Compose（流程）→ ADK/Flow（形态） |
| `Runnable[I,O]` | 全库唯一接缝：`Invoke`/`Stream`/`Collect`/`Transform` |
| 四范式补齐 | 组件只实现一种，其余由流化 + 拼接自动生成 |
| 图三件套 | `AddXXXNode` / `AddEdge` / `AddBranch(挂在节点上)` |
| 两种运行时 | `Pregel`（可环，超步 + 黑板）· `DAG`（无环，全前驱触发） |
| 流 | `schema.StreamReader`：read-once、`Copy(n)` 分叉、`Pipe(cap)` 固定容量、无背压协商 |
| 中断/恢复/快照 | `Interrupt` 就是 `error`；`WithCheckPointID`+`ResumeWithData`+`GetInterruptState`；快照类型须 `RegisterName` |
| 切面 | `callbacks.Handler` 五时机，注入不侵入业务 |
| 并发真相 | 图产物并发安全，**组件实例内部状态不安全** |
| 大坑 | 忘关流、忘注册类型、忘 `MaxRunSteps`、副作用非幂等 |

---

## 49.8 自检题

1. **你的同事说「eino 的图不就是 DAG 吗，用拓扑排序跑一遍不就行了？」** 他的判断哪里不完整？
   > 要点：① 有环（ReAct 循环）需要 Pregel 超步模型而不是拓扑排序；② 要支持「中断—恢复」就必须有确定性边界与快照，超步是唯一的自然边界；③ 还要做流式跨节点传递（四范式转换）与并行 fan-in 合并，这不是排序问题，是运行时问题。

2. **为什么 `Interrupt` 返回 `error` 而不是「挂起 goroutine 等审批」？如果在工具里 `select` 等一个 channel 会怎样？**
   > 要点：Go 没有 continuation，无法保存栈；挂起 goroutine 会占住一个栈和连接，进程重启即丢失，跨实例无法续跑。用 `error` 把「暂停」变成「返回」，再靠快照重建，才能跨进程跨重启；代价是节点必须是幂等/可重放的。

3. **一个流式 ChatModel 组件里，你把收到的 `*StreamReader` 存进了结构体字段，然后在 callback 里再 `Recv()` 一次——会发生什么？为什么 `Copy` 能解决？**
   > 要点：`StreamReader` 是 read-once，两个消费方会「抢」chunk，且合并顺序混乱；`Copy(n)` 在流的源头就把 item 分发给 n 个独立队列，每个消费者各读一份，原 reader 失效。

---

## 49.9 延伸阅读

- 仓库：`github.com/cloudwego/eino`（Apache-2.0）；组件实现：`github.com/cloudwego/eino-ext`
- 必读源码顺序：`compose/runnable.go` → `compose/graph.go`（`graph struct` / `Compile`）→ `compose/pregel.go` → `schema/stream.go` → `compose/checkpoint.go` → `compose/interrupt.go` → `compose/tool_node.go`
- 文档：`cloudwego.io/docs/eino`（Core Modules / Stream Programming / Checkpoint & Interrupt）
- 同族：`flow/agent/react`（ReAct 预置图）、`adk/`（Runner + DeepAgent + 多 Agent 主持）
- 对照：LangGraph 的 Pregel 实现（同样选择超步 + checkpoint，值得对着读）
- 下一章：[50. MCP Go SDK 源码精读](50-mcp-sdk-source.md)

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[48. langchaingo 精读](48-langchaingo-source.md)
- 相关：第 6 章（泛型与 `Runnable[I,O]`）、第 10 章（接口作接缝）、第 20 章（channel 编排）、第 24 章（ctx 取消穿透）、第 31 章（JSON 编解码）、第 39 章（SSE 上屏）、第 43 章（Agent 循环与编排）、第 46 章（持久化执行）
- 下一章：[50. MCP Go SDK 源码精读](50-mcp-sdk-source.md)
