# 48. langchaingo 精读

> **状态**：`已完成（初稿）` ｜ **所属**：第八部分 · 源码精读 ｜ **编号**：48 / 54
> **本章定位**：`github.com/tmc/langchaingo` 是 Go 世界里"转写得最完整的 LangChain"。读它不是因为它完美，恰恰因为它**把 Python 的设计原样搬进 Go**，于是你能同时看到两件事：哪些抽象在 Go 里依然成立，哪些在 Go 里格外别扭。这一章给目录、给调用链、给判断。
> **读法**：48.2 的目录树 + 分层图先建立"哪里放什么"；48.3.4 的 executor 循环是重点（跟我们第 43 章自己写的循环对照）；48.5 的"可借鉴/可吐槽"表可以直接拿去做选型汇报。

---

## 48.1 引子：第一次打开它，两小时读完了 4 个文件

我建议团队读源码时的顺序是：**先读最小接口，再读最复杂的那条调用链**。langchaingo 里分别是：

- 最小接口：`schema.Memory`（5 个方法）、`tools.Tool`（3 个方法）、`llms.Model`（2 个方法）；
- 最复杂调用链：`agents.Executor.Call` → `Agent.Plan` → `chains.Chain.Call` → `llms.Model.GenerateContent`。

读完你会有两个反应：① "这抽象很漂亮，`schema` 一统天下"；② "这循环怎么是单线程串行跑工具的？`MaxIterations` 凭什么默认 5？"——两个反应都对，这一章就是解释为什么。

> 💡 版本说明：本章按 v0.1.x 的公开 API 与目录结构讲。这个仓库迭代很快，**函数签名与文件位置会变**，看代码时以你本地 `go doc` 与 `$GOPATH/pkg/mod/github.com/tmc/langchaingo*` 为准。

---

## 48.2 直觉模型：一个 schema，两个方向

```
langchaingo/
├── schema/            ★ 全库的"公共词汇表"：Message / Document / Memory / AgentAction
├── llms/              ★ 模型抽象 + 各 provider 适配
│   ├── openai/  anthropic/  ollama/  googleai/  ...
├── embeddings/          文本 → 向量抽象（Embedder）
├── prompts/             PromptTemplate / 消息模板 / 格式化
├── chains/            ★ 组合层：可 Call 的组件
│   ├── llm/  conversation/  sequential/  retrieval/
│   ├── question_answering/  combine_documents/（stuff / refine / map_reduce）
├── agents/            ★ 循环层：Executor + 各 Agent 实现
├── tools/             工具实现（calculator / wikipedia / scraper / serpapi…）
├── memory/            会话记忆（buffer / window buffer）
├── vectorstores/       向量库适配（pgvector / qdrant / redisvector / chroma…）
├── documentloaders/    文件 → Document
├── textsplitter/       文本切分
├── outputparser/       模型输出 → 结构化
├── callbacks/          ★ 可观测钩子（Handler 接口）
└── util/ internal/ examples/
```

三层依赖，方向永远向下（**没有向上的 import，这是它最值得学的纪律**）：

```
        ┌──────────────────────────────────────────────┐
        │ agents (Executor / OneShotReact / OpenAIFn)  │
        └───────────────┬──────────────────────────────┘
                        │ uses
        ┌───────────────▼──────────────────────────────┐
        │ chains (llm / sequential / retrieval / …)    │
        └───────────────┬──────────────────────────────┘
                        │ uses
        ┌───────────────▼──────────────────────────────┐
        │ llms.Model   tools.Tool   memory   retriever │  ← 全是小接口
        └───────────────┬──────────────────────────────┘
                        │ implements
        ┌───────────────▼──────────────────────────────┐
        │ llms/openai  tools/calculator  vectorstores/…│  ← 具体实现
        └───────────────┬──────────────────────────────┘
                        │ 依赖
        ┌───────────────▼──────────────────────────────┐
        │ schema（没有任何内部依赖，只有类型与接口）      │
        └──────────────────────────────────────────────┘

        callbacks 是横切的：任何层都可以接收一个 Handler
```

| 包 | 一句话职责 | 核心类型 | 你在生产里会替换它吗 |
| --- | --- | --- | --- |
| `schema` | 定义所有跨包数据结构与最小接口 | `Document`、`ChatMessage`、`Memory`、`AgentAction/Step/Finish` | 不会，但会被你"部分复用" |
| `llms` | 统一的模型调用面 + 选项模式 | `Model`、`MessageContent`、`CallOption` | 会（流式/重试/计量通常自己包一层） |
| `chains` | 把"输入 map → 输出 map"的组件串起来 | `Chain` | 会（生产多用显式函数编排） |
| `agents` | 推理-行动循环 | `Agent`、`Executor` | 大概率会（预算/并行/中断它都不管） |
| `memory` | 会话消息存取 | `schema.Memory` 实现 | **一定会**（要接 Redis/PG，第 45 章） |
| `tools` | 工具实现与协议 | `Tool` | 会（要接内部系统 + 鉴权 + 审计） |
| `vectorstores` | 向量库适配 | `VectorStore` | 看情况（第 44 章） |
| `callbacks` | 观测钩子 | `Handler` | 会（要桥到 OTel，第 35 章） |

---

## 48.3 底层实现

### 48.3.1 `schema`：全库唯一的"公分母"

`schema` 的精髓不是类型多，而是**它把"LLM 应用的最小数据流"压缩成了几个结构**。先看三个：

```go
// 1) RAG 的原子单位：内容 + 元数据 + 分数
type Document struct {
	PageContent string         `json:"page_content"`
	Metadata    map[string]any `json:"metadata"`
	Score       float32        `json:"score"` // 检索时填，供 rerank/阈值过滤
}

// 2) ReAct 循环的三件套：动作、已执行的步骤、终止
type AgentAction struct {
	Log      string `json:"log"`       // 模型的思考（用于 scratchpad 回灌）
	Tool     string `json:"tool"`      // 工具名
	ToolInput string `json:"tool_input"` // 工具输入（注意：是 string，不是结构化参数！）
}

type AgentStep struct {
	Action      AgentAction `json:"action"`
	Observation string      `json:"observation"` // 工具返回的文本
}

type AgentFinish struct {
	ReturnValues map[string]any `json:"return_values"`
	Log          string         `json:"log"`
}

// 3) 记忆：只有 5 个方法，且不涉及存储细节
type Memory interface {
	GetMemoryKey(ctx context.Context) string
	MemoryVariables(ctx context.Context) (map[string]any, error)
	LoadMemoryVariables(ctx context.Context, inputs map[string]any) (map[string]any, error)
	SaveContext(ctx context.Context, inputs map[string]any, outputs map[string]any) error
	Clear(ctx context.Context) error
}
```

**这里有个必须点出来的设计取舍**：`ToolInput string`。工具参数是**字符串**而不是 `map[string]any`/泛型结构体。这直接解释了后面的一切——它对应的是老式 ReAct prompt（"Action Input: 一段文本"），而不是现代 function calling（JSON Schema 参数）。所以：

- ReAct 路线的 Agent（`OneShotZeroAgent`、`ConversationalAgent`）靠**文本解析**决定调什么工具，脆弱；
- function calling 路线的 Agent（`OpenAIFunctionsAgent`）在内部把结构化参数 JSON 序列化后再塞进 `ToolInput`，**在边界上把类型信息丢了**——工具作者必须自己反序列化 + 校验（这活儿我们在第 41 章是用 `invopop/jsonschema` 自动干的）。

> 🩸 这就是"移植型框架"的典型代价：**抽象是照着 Python 的动态习惯设计的，Go 的类型系统没用上。**

### 48.3.2 `llms`：provider 适配层小得惊人

```go
// 整个模型抽象只有两个方法
type Model interface {
	GenerateContent(ctx context.Context, messages []MessageContent, options ...CallOption) (*ContentResponse, error)
	Call(ctx context.Context, prompt string, options ...CallOption) (string, error)
}
```

`GenerateContent` 是"多模态消息"路径（推荐），`Call` 是"裸字符串"的便捷路径（内部通常包一层转成 `GenerateContent`）。`MessageContent` 长这样：

```go
type MessageContent struct {
	Role  schema.ChatMessageType // system / human / ai / tool / generic
	Parts []ContentPart          // TextContent / ImageURLContent / BinaryContent…
}
```

调用参数走**functional options**，这是 Go 世界最地道的做法（第 10、36 章）：

```go
resp, err := model.GenerateContent(ctx, msgs,
	llms.WithTemperature(0.2),
	llms.WithMaxTokens(1024),
	llms.WithStreamingFunc(func(ctx context.Context, chunk []byte) error { // 流式钩子
		_, werr := w.Write(chunk)
		return werr
	}),
)
```

看 provider 适配怎么写的（这就是"适配器模式"的教科书形态，值得抄）：

```go
// 自己写一个 provider 适配器：只要实现两个方法，全库立刻能用
type MyModel struct {
	client *http.Client
	base   string
}

func (m *MyModel) Call(ctx context.Context, prompt string, opts ...llms.CallOption) (string, error) {
	resp, err := m.GenerateContent(ctx,
		[]llms.MessageContent{{Role: schema.ChatMessageTypeHuman, Parts: []llms.ContentPart{llms.TextContent{Text: prompt}}}},
		opts...)
	if err != nil {
		return "", err
	}
	if len(resp.Choices) == 0 {
		return "", errors.New("no choices returned")
	}
	return resp.Choices[0].Content, nil
}

func (m *MyModel) GenerateContent(ctx context.Context, msgs []llms.MessageContent,
	opts ...llms.CallOption) (*llms.ContentResponse, error) {
	var callOpts llms.CallOptions
	for _, o := range opts {
		o(&callOpts) // ★ 选项模式：把 []CallOption 折叠成一个结构体
	}
	// 这里做：消息转换 → HTTP 调用 → 流式解析 → 组装 ContentResponse
	_ = callOpts
	return &llms.ContentResponse{Choices: []*llms.ContentChoice{{Content: "…"}}}, nil
}

var _ llms.Model = (*MyModel)(nil) // 编译期断言，强烈建议每个适配器都写
```

> 💡 **可借鉴点 #1**：`var _ llms.Model = (*MyModel)(nil)` + 两个方法的极小接口 = 任何人 20 分钟能接一个新模型。这是"接口由使用方定义 + 小接口"（第 5 章）在真实框架里的收益。

### 48.3.3 `chains`：用"输入 map → 输出 map"做组合

```go
type Chain interface {
	Call(ctx context.Context, inputs map[string]any, options ...ChainCallOption) (map[string]any, error)
	Run(ctx context.Context, inputs map[string]any, options ...ChainCallOption) (string, error)
	GetInputKeys() []string
	GetOutputKeys() []string
}
```

**这个签名是 langchaingo 全部优雅与全部笨拙的来源**：

- 优雅：任何东西只要能 `Call`，就能被 `sequential.NewSequentialChain` 串起来、被 `retrieval.NewRetrievalQA` 嵌进去，组合成本极低；
- 笨拙：`map[string]any` 意味着**编译期零类型检查**，key 拼错要到运行时才炸；这与 Go 的价值观（显式、可读）背道而驰，是 Python `dict` 文化的直接移植。

对比一下第 43 章我们自己写的编排：明确的 `Input`/`Output` struct + 一个 `func(ctx, In) (Out, error)`。生产里我更倾向后者——**在框架内部用 `map[string]any` 做胶水，在框架边界用 struct 做契约**。

```go
// 组合示例：LLM 链 + 顺序拼接 + 回调
llmChain := chains.NewLLMChain(model, prompt)      // 输入 "input" → 输出 "text"
seq := sequential.NewSequentialChain([]chains.Chain{llmChain, secondChain}, []string{"input"}, []string{"text"})
out, err := chains.Call(ctx, seq, map[string]any{"input": "总结一下这篇文章"},
	chains.WithCallback(myHandler),   // ★ 可观测钩子从这里注入
	chains.WithTemperature(0.3),
)
```

### 48.3.4 `agents`：executor 的循环与工具调度（全章重点）

调用链先看清楚：

```
Executor.Call(ctx, inputs)
   │
   ├─ memory.LoadMemoryVariables(ctx, inputs)        // 取历史（第 45 章）
   │
   └─ for i := 0; i < MaxIterations; i++ {           // ★ 串行循环，硬上限
        │
        ├─ Agent.Plan(ctx, steps, inputs)            // 一次 LLM 调用
        │     │
        │     ├─ chains/llm 渲染 ReAct prompt（把 steps 拼成 scratchpad 文本）
        │     ├─ llms.Model.GenerateContent(...)     // /v1/chat/completions
        │     └─ ParseOutput(text) → ([]AgentAction | *AgentFinish, err)
        │
        ├─ if finish != nil { 写 memory；return finish.ReturnValues }
        │
        ├─ for _, action := range actions {          // ★ 逐个执行，不并发
        │     ├─ callbacks.HandleAgentAction(...)
        │     ├─ 按名字线性查找 tool
        │     ├─ tool.Call(ctx, action.ToolInput)    // 字符串进，字符串出
        │     └─ steps = append(steps, AgentStep{...})
        │  }
        └─ } // 超限：return nil, ErrNotFinished      // 默认上限很小
}
```

源码骨架（删减版，保留结构）：

```go
func (e *Executor) Call(ctx context.Context, inputValues map[string]any,
	options ...chains.ChainCallOption) (map[string]any, error) {

	inputs, err := inputsToString(inputValues) // map[string]any → map[string]string
	if err != nil {
		return nil, err
	}
	steps := make([]schema.AgentStep, 0, e.MaxIterations)

	for i := 0; i < e.MaxIterations; i++ {
		actions, finish, err := e.Agent.Plan(ctx, steps, inputs, options...)
		if err != nil {
			return nil, err
		}
		if finish != nil { // 拿到最终答案
			if e.Memory != nil {
				_ = e.Memory.SaveContext(ctx, inputs, finish.ReturnValues)
			}
			return finish.ReturnValues, nil
		}
		for _, action := range actions { // 顺序执行
			observation := e.observe(ctx, action) // 查 tool → 调用 → 字符串结果
			steps = append(steps, schema.AgentStep{Action: action, Observation: observation})
		}
	}
	return nil, ErrNotFinished // ★ 超限即失败，没有"部分结果"
}
```

四个必须知道的实现事实（都会在生产里咬你）：

1. **工具是串行执行的**。`Plan` 能返回 `[]AgentAction`，但循环里没有 `errgroup`——**并行工具调用它不做**（第 41 章的"并行工具 + 超时"得自己在上层做）。
2. **scratchpad 是纯文本拼接**。每一步 `Log + Observation` 都进 prompt，长任务下 token 线性膨胀且**无裁剪**（第 45 章的预算裁剪它不管）。
3. **`ErrNotFinished` 会把已完成的步骤丢掉**。跑到第 5 步超限 → 返回错误，前面 5 步的观察结果全没了。生产里你要改成"返回部分结果 + 明确的 `finish_reason`"。
4. **没有 ctx 预算/成本概念**。要加预算就自己包一层：在 `Plan` 前查预算、在回调里累计 token（下面 48.3.5）。

> 💡 **可借鉴点 #2**：`Agent.Plan` 只负责"决定下一步"，`Executor` 只负责"执行与累积状态"，两者通过 `[]AgentStep` 通信。这个拆分很干净——**你自研 Agent 时照这个切**：推理（可换模型/可换策略）和执行（工具调用/重试/审计）分离。

### 48.3.5 `callbacks`：横切的可观测钩子

```go
type Handler interface {
	HandleText(ctx context.Context, text string)
	HandleLLMStart(ctx context.Context, prompts []string)
	HandleLLMGenerateContentStart(ctx context.Context, ms []llms.MessageContent)
	HandleLLMGenerateContentEnd(ctx context.Context, res *llms.ContentResponse)
	HandleLLMError(ctx context.Context, err error)
	HandleChainStart(ctx context.Context, inputs map[string]any)
	HandleChainEnd(ctx context.Context, outputs map[string]any)
	HandleChainError(ctx context.Context, err error)
	HandleToolStart(ctx context.Context, input string)
	HandleToolEnd(ctx context.Context, output string)
	HandleToolError(ctx context.Context, err error)
	HandleAgentAction(ctx context.Context, action schema.AgentAction)
	HandleAgentFinish(ctx context.Context, finish schema.AgentFinish)
	HandleRetrieverStart(ctx context.Context, query string)
	HandleRetrieverEnd(ctx context.Context, query string, documents []schema.Document)
	HandleRetrieverError(ctx context.Context, err error)
}
```

（方法集合以你本地版本为准。）构造时不实现 16 个方法，用 builder：

```go
handler := callbacks.NewHandlerBuilder().
	OnLLMStart(func(ctx context.Context, prompts []string) {
		span.AddEvent("llm.start", trace.WithAttributes(attribute.Int("prompt.count", len(prompts))))
	}).
	OnLLMGenerateContentEnd(func(ctx context.Context, res *llms.ContentResponse) {
		// ★ 在这里做 token 计量与成本账本（第 47 章）
		if res.Usage != nil {
			ledger.Add(int64(res.Usage.TotalTokens))
		}
	}).
	OnToolEnd(func(ctx context.Context, output string) {
		span.AddEvent("tool.end", trace.WithAttributes(attribute.Int("out.len", len(output))))
	}).
	Build()
```

**判断**：这套钩子刚好够你把 OTel span、token 账本、审计日志挂上去，比"自己到处打日志"强得多；但它**没有 span 层级语义**（没有 parent/child、没有开始-结束配对的时间戳结构），所以桥接 OTel 时需要你自己维护 span 栈：`HandleLLMStart` 里 push，`HandleLLMGenerateContentEnd`/`HandleLLMError` 里 pop。

---

## 48.4 Java 对照

| langchaingo | Java 世界对应 | 关键差异 |
| --- | --- | --- |
| `llms.Model`（2 方法接口） | LangChain4j 的 `ChatLanguageModel` / Spring AI 的 `ChatModel` | 都是小接口；Go 靠隐式实现，适配器不需要 `implements` 声明 |
| functional options | Builder 模式 | Go 用 `func(*Opts)` 可变参数替代 Builder 类，少一个类、少一层链式调用 |
| `chains.Chain`（`map[string]any` 进出） | LangChain4j 的 `Chain` / Spring AI 的 `Advisor` 链 | Java 版本有泛型，能在编译期抓到类型错误；Go 版本放弃了这点 |
| `schema.Memory`（5 方法） | Spring AI 的 `ChatMemory` / `ChatMemoryStore` | Spring AI 把"记忆"与"存储"拆成两个接口，比 langchaingo 更清晰 |
| `callbacks.Handler` | Micrometer Observation / Spring 的 `ObservationHandler` | Java 侧把"观测"做成了平台能力，Go 侧要自己接 OTel |
| `AgentInput`/`Plan` 分离 | LangChain4j 的 `AiServices` + `ToolProvider` | Java 版本更依赖反射与注解自动装配工具；Go 版本全是显式代码 |

---

## 48.5 可借鉴处 vs 可吐槽处

| | 内容 | 为什么 |
| --- | --- | --- |
| ✅ 可借鉴 1 | **极小的 Provider 接口 + functional options** | 接新模型只写两个方法；选项模式让签名永不膨胀 |
| ✅ 可借鉴 2 | **`Plan` 与 `Execute` 分离** | 推理策略可插拔（ReAct / function calling / 自定义），执行器统一管状态 |
| ✅ 可借鉴 3 | **`schema` 零内部依赖** | 整个框架可以被别的框架 import 而不引入循环；分层纪律值得抄 |
| ✅ 可借鉴 4 | **`var _ llms.Model = (*X)(nil)` 编译期断言** | 适配器不用写测试就能确认接口满足 |
| ✅ 可借鉴 5 | **横切 Handler 而非到处打日志** | 你在一个地方接上 OTel/token 账本，全链路立刻可观测 |
| ❌ 可吐槽 1 | **`map[string]any` 做 Chain 契约** | 编译期零检查，key 拼错运行时炸；Go 用泛型/struct 明明可以更好 |
| ❌ 可吐槽 2 | **`ToolInput string`** | 类型信息在边界丢失，工具内自己反序列化 + 校验，容易注入与 500 |
| ❌ 可吐槽 3 | **工具串行执行** | 多工具场景延迟叠加；`errgroup` 十行就能修，但它没做 |
| ❌ 可吐槽 4 | **`MaxIterations` 小而硬（默认 5）** | 复杂任务很容易"未完成"；且超限时**丢弃全部中间结果** |
| ❌ 可吐槽 5 | **无 token 预算、无裁剪、无成本** | 第 45/47 章那套它一概不管，长会话必炸 |
| ❌ 可吐槽 6 | **无持久化/无中断恢复** | 进程挂了全丢；要长流程只能上 Temporal（第 46 章） |
| ❌ 可吐槽 7 | **`memory` 只有内存实现** | 生产必然自己实现 `schema.Memory` 接 Redis/PG |

> **一句话选型建议**：**把 langchaingo 当"零件库"，不要当"应用框架"**。`llms`、`tools`、`textsplitter`、`prompts`、`callbacks` 直接拿来用收益最大；`agents.Executor` 和 `memory` 建议自己写（第 43、45 章），或者用 eino 的图编排（第 49 章）。

---

## 48.6 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| Agent 报 `ErrNotFinished` 且没有任何结果 | 默认 `MaxIterations` 太小、超限丢弃中间态 | `agents.WithMaxIterations(15)` + 在上层保留 steps 做部分返回 | 48.3.4 |
| 工具参数反序列化失败率很高 | `ToolInput string`，模型吐的 JSON 不合法 | 工具里 `json.Unmarshal` + schema 校验 + 失败回灌错误让模型重试 | 48.3.1、第 41 章 |
| 多工具场景延迟翻倍 | 执行器串行跑工具 | 自己包一层并发工具执行（`errgroup.SetLimit`） | 48.3.4、第 26 章 |
| 长会话 400 token 超限 | scratchpad 与历史无裁剪 | 自己实现 `schema.Memory` + 预算裁剪 | 48.3.4、第 45 章 |
| Chain 的 key 拼错，运行时才炸 | `map[string]any` 契约 | 在框架边界用 struct 收口，内部才用 map | 48.3.3 |
| 观测数据东一块西一块 | 直接用 `fmt.Println`/log | `callbacks.NewHandlerBuilder()` 统一接 OTel | 48.3.5、第 35 章 |
| 升级小版本后编译不过 | 该仓库 API 迭代快（v0.1.x 仍在演进） | 锁定版本（`go.mod`）+ 用 `var _ Interface` 断言做升级检查 | 48.1 |

---

## 48.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 依赖方向 | `schema` 零依赖；`llms/tools/memory` 是小接口；`chains/agents` 在上层 |
| `llms.Model` | 只有 `GenerateContent` + `Call`；选项走 functional options |
| 适配器 | `var _ llms.Model = (*X)(nil)` 编译期断言，20 分钟接一个新模型 |
| `chains.Chain` | `map[string]any` 进出：组合极灵活，类型极不安全 |
| 循环 | `Executor` 串行：`Plan` → 执行工具 → 累积 `AgentStep` → 再 `Plan` |
| 上限 | `MaxIterations` 默认很小，超限 `ErrNotFinished` 丢结果 |
| 工具 | 串行执行、`ToolInput` 是字符串，校验要自己做 |
| 记忆 | 只有内存实现，生产必自写 `schema.Memory` |
| 回调 | 横切 `Handler`，是接 OTel 与 token 账本的唯一正确位置 |
| 选型 | 当零件库用（llms/tools/prompts/callbacks），别当应用框架 |

---

## 48.8 自检题

1. **为什么说 `chains.Chain` 的 `map[string]any` 签名既是它最大的优点也是最大的缺点？**
   > 要点：优点是任何组件都能无缝组合（输入输出不约定类型），新增链路零改接口；缺点是编译期无类型检查、key 与类型约定只存在于文档/测试里，重构极危险。工程折中：**框架内部用 map 做胶水，对外边界（你的 HTTP/handler 层）用 struct 收口并做显式转换与校验**。

2. **`agents.Executor` 的默认实现有哪四处会在生产里咬你？分别怎么补？**
   > 要点：① 工具串行 → 并发化（`errgroup`）；② `MaxIterations` 小且超限丢中间结果 → 提高上限、保留 steps、返回部分结果 + `finish_reason`；③ scratchpad/历史无裁剪 → 自写 `schema.Memory` + token 预算；④ 无成本/无持久化 → 接 `callbacks` 记账 + 长流程上 Temporal。

3. **如果你要在团队里接一个新模型供应商，langchaingo 的什么设计最值得照抄？**
   > 要点：极小接口（两个方法）+ functional options + 编译期接口断言；再加一点：适配器内部把"错误分类"（可重试/不可重试，第 7 章）标准化，因为上层重试逻辑依赖它。

---

## 48.9 延伸阅读

- `github.com/tmc/langchaingo`：`schema/document.go`、`schema/agent.go`、`schema/memory.go`、`llms/llms.go`、`chains/chains.go`、`agents/executor.go`、`agents/one_shot_zero_agent.go`、`agents/openai_functions_agent.go`、`callbacks/callbacks.go`
- `go doc github.com/tmc/langchaingo/agents`、`go doc github.com/tmc/langchaingo/llms`（比看网页快）
- 对照读：LangChain4j 的 `AiServices` 与 Spring AI 的 `ChatMemory`/`Advisor`（看"Java 怎么做同一件事"）
- 上一章：[47. Agent 的生产化：多租户、限流与成本](47-agent-productionization.md) ｜ 下一章：[49. eino 精读](49-eino-source.md)

---

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[47. Agent 的生产化：多租户、限流与成本](47-agent-productionization.md)；第 43 章（Agent 循环）、第 41 章（工具系统）、第 45 章（记忆）
- 下一章：[49. eino 精读](49-eino-source.md)
