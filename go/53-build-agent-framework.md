# 53. 综合项目：从零构建生产级 Agent 框架

> **状态**：`已完成（初稿）` ｜ **所属**：第九部分 · 生产部署与收尾 ｜ **编号**：53 / 54
> **本章定位**：全书唯一一个「把前 52 章焊在一起」的工程章。目标不是再造一个 eino，而是让你亲手写出一套 **3000 行以内、能跑、能扩展、能上生产** 的 Agent 框架，从此看任何框架都能三分钟摸到它的骨架。
> **读法**：先看完 53.2 的分层与 53.3 的目录树（15 分钟建立全局），然后照着 53.4~53.9 一段段抄；53.10 的测试与 53.12 的扩展点清单建议在第一行代码之前就读。

---

## 53.1 引子：第三周，你的 Agent 代码变成了一坨意大利面

第一版 Agent 只有 200 行：`for i := 0; i < 5; i++ { resp := llm.Chat(...); if resp.HasToolCall() { runTool() } }`。挺好。

第三周它长成了这样：

```
main.go                    1200 行  ← 循环 + 工具 + 重试 + 日志 + prompt 拼接 + SSE 输出
utils.go                    800 行  ← JSON 解析 + 截断 + 时间格式化 + "临时" 的 Redis 客户端
handlers.go                 600 行  ← HTTP + 鉴权 + 限流 + 会话读写 + 一堆全局变量
（还有一个 llm_backup.go，是上周为了换供应商复制出来的第二份 main 循环）
```

然后你遇到四件必须做、但现在根本没法做的事：

1. **换个模型供应商**：`openai.ChatCompletion` 的调用点散在 7 个文件里，硬编码了 SDK 类型。
2. **加一个「先规划再执行」的分支**：现在的 `for` 循环只能 ReAct，加不了 DAG。
3. **支持「工具执行到一半要人工审批」**：进程重启后一切都丢了，用户白等 40 秒。
4. **测一个「模型返回空 tool_calls」的边界**：只能真的去调一次 API。

这四个需求，指向同一件事：**你缺的不是更多代码，是分层和接口**。这一章就把这套分层一次性写出来。

> 🎯 **本章的立场**：框架的价值 90% 在**接口形状**上，10% 才在实现里。所以本章重点是「为什么接口长这样」，实现只给关键路径。

---

## 53.2 直觉模型：五层 + 一条依赖规则

```
┌──────────────────────────────────────────────────────────────────────┐
│ L5 transport    HTTP/SSE · CLI ·（未来：gRPC、WebSocket）              │
│                 只做「协议 ↔ 事件」翻译，不含业务                      │
├──────────────────────────────────────────────────────────────────────┤
│ L4 agent        ReAct / Plan-Execute / Router：预置的图，不含新机制     │
├──────────────────────────────────────────────────────────────────────┤
│ L3 graph        编排引擎：节点、边、并发、中断恢复、检查点              │
├──────────────────────────────────────────────────────────────────────┤
│ L2 provider     OpenAI / Ollama / Fake：协议适配层，只依赖 core         │
│    store        会话 / 记忆 / 向量：持久化，只依赖 core                │
│    obs          Hook → slog / OTel：旁路观察，只依赖 core              │
├──────────────────────────────────────────────────────────────────────┤
│ L1 core         Message · Event · ChatModel · Tool · State · error     │
│                 ★ 只 import 标准库，不 import 本项目的任何其它包 ★      │
└──────────────────────────────────────────────────────────────────────┘

依赖规则（单向，违反即设计错误）：
  core  ←  provider / store / obs / graph  ←  agent  ←  transport  ←  cmd
```

> 💡 **Java 对照**：这套分层和 Spring 的 `controller → service → repository` 看起来一样，但有一条本质差别——**core 里没有「实体类 + DTO + VO」的三件套**。Go 的世界里一个 `Message` 结构体从头用到尾，需要变形就用函数，不开新类型。你如果按 Java 习惯搞 `MessageDTO / MessageVO / MessageEntity` 三层映射，代码会直接翻倍，而收益是零（详见第 54 章）。

这条规则为什么这么重要？因为 **`core` 不依赖任何东西 = 可以被任何东西依赖 = 它是整个框架的稳定内核**。而 `provider` 只依赖 core，意味着你换供应商不需要动 `graph`、`agent`、`transport` 一行代码——这就是「可插拔」的技术定义。

---

## 53.3 完整目录树与行数预算（3000 行怎么分）

```
agentkit/
├── go.mod                                  github.com/you/agentkit
├── Makefile                                构建/测试/lint 三件套        (~40)
├── cmd/
│   ├── agentd/main.go                      服务入口：装配 + 优雅关停     (~140)
│   └── agentctl/main.go                    CLI：单轮对话、恢复检查点     (~90)
├── internal/
│   ├── core/                               ★ 零依赖内核（~700 行）★
│   │   ├── message.go                      Message / Part / ToolCall     (~130)
│   │   ├── event.go                        Event / Emitter               (~140)
│   │   ├── model.go                        ChatModel / Request / Delta   (~100)
│   │   ├── tool.go                         Tool / Registry / Schema 生成  (~170)
│   │   ├── state.go                        State / 序列化                (~90)
│   │   └── errors.go                       哨兵错误 + 可重试分类          (~70)
│   ├── provider/                           协议适配（~620 行）
│   │   ├── openai/openai.go                请求/响应/SSE 映射            (~330)
│   │   ├── ollama/ollama.go                NDJSON 映射                   (~170)
│   │   └── fake/fake.go                    测试用：剧本化响应            (~120)
│   ├── graph/                              编排引擎（~800 行）
│   │   ├── graph.go                        Graph 定义与编译校验           (~230)
│   │   ├── node.go                         Node / NodeFunc / 并行批次     (~160)
│   │   ├── runner.go                       ★ 执行主循环（并发+中断）      (~290)
│   │   └── checkpoint.go                   Checkpoint / Store            (~120)
│   ├── agent/                              预置编排（~330 行）
│   │   ├── react.go                        ReAct 图（LLM ↔ Tools）        (~200)
│   │   └── router.go                       路由图（分类后走不同子图）      (~130)
│   ├── store/                              持久化（~600 行）
│   │   ├── session.go                      SessionStore 接口 + 内存实现   (~150)
│   │   ├── memory.go                       窗口裁剪 + 摘要记忆            (~140)
│   │   ├── redis/session.go                Redis 实现（含乐观锁）         (~160)
│   │   └── pgvector/vector.go              VectorStore 实现              (~150)
│   ├── obs/                                可观测（~350 行）
│   │   ├── hooks.go                        Hook 接口 + 组合器             (~130)
│   │   ├── sloghook.go                     结构化日志                    (~100)
│   │   └── otelhook.go                     span 属性映射                 (~120)
│   ├── config/config.go                    env 解析 + 校验                (~130)
│   └── transport/
│       ├── http.go                         POST /chat（SSE 流式）         (~190)
│       └── sse.go                          SSE 编码器 + 心跳              (~90)
├── testdata/
│   └── golden/
│       ├── react_with_tool.sse              录制回放用的 golden
│       └── empty_tool_calls.sse
└── examples/
    └── minimal/main.go                     80 行跑通一个 Agent
```

统计：`core 700 + provider 620 + graph 800 + agent 330 + store 600 + obs 350 + transport 280 ≈ 3680`，减去注释和测试脚手架，**核心 ~3000 行**。这个体量一个人一周能写完第一版，两个人三天。

**行数分配的三条原则**：

| 原则 | 表现 | 反面案例 |
| --- | --- | --- |
| 内核最厚，适配最薄 | 700 行 core、330 行 provider | provider 比 core 还大 = 业务逻辑漏进了适配层 |
| 每个文件只做一件事 | `checkpoint.go` 只有接口 + 内存实现 | `graph.go` 里又有 HTTP 又有 Redis |
| 测试与实现同层 | `fake` provider 和 `MemorySessionStore` 就是测试基础设施 | 用 mock 框架造 20 层桩 |

---

## 53.4 core 层：一口气把接口定对

core 是全书反复讲的「小接口 + 由使用方定义」的最终落地。**这里每一个类型名你都应该能在 eino / langchaingo 里找到对应物**（详见第 48、49 章）。

### 53.4.1 Message：为什么用 Part 而不是 `Content string`

```go
package core

// Role 是消息角色。用 string 而不是 int 常量，是为了直接进 JSON/日志。
type Role string

const (
	RoleSystem    Role = "system"
	RoleUser      Role = "user"
	RoleAssistant Role = "assistant"
	RoleTool      Role = "tool"
)

type PartKind string

const (
	PartText       PartKind = "text"
	PartToolCall   PartKind = "tool_call"
	PartToolResult PartKind = "tool_result"
)

// Part 是消息的最小单元。一条 assistant 消息可能同时含文本和多个工具调用。
type Part struct {
	Kind PartKind `json:"kind"`

	Text       string      `json:"text,omitempty"`
	ToolCall   *ToolCall   `json:"tool_call,omitempty"`
	ToolResult *ToolResult `json:"tool_result,omitempty"`
}

type Message struct {
	Role  Role   `json:"role"`
	Parts []Part `json:"parts"`
	// Meta 放不进标准字段的东西：时间戳、模型名、trace id、供应商原始 id
	Meta map[string]string `json:"meta,omitempty"`
}

// 便捷构造：90% 的代码只用这两个
func TextMessage(role Role, text string) Message {
	return Message{Role: role, Parts: []Part{{Kind: PartText, Text: text}}}
}

func (m Message) Text() string {
	var b strings.Builder
	for _, p := range m.Parts {
		if p.Kind == PartText {
			b.WriteString(p.Text)
		}
	}
	return b.String()
}

func (m Message) ToolCalls() []ToolCall {
	var out []ToolCall
	for _, p := range m.Parts {
		if p.Kind == PartToolCall && p.ToolCall != nil {
			out = append(out, *p.ToolCall)
		}
	}
	return out
}
```

> 💡 **为什么不直接抄 OpenAI 的 `content: string`？** 因为多模态、并行工具调用、推理摘要（reasoning content）都需要在一条消息里塞异构内容。`content: string` 一旦定型，加图片就得改协议。Part 模型是 langchaingo 的 `schema.ContentBlock` 和 eino 的 `schema.Message` 都走的路——**用「列表 + 可选字段」换扩展性，代价是每次读都要遍历**（所以给了 `Text()` / `ToolCalls()` 这种便捷方法）。

### 53.4.2 Event：流式是第一等公民

Agent 的用户体验全部押在「第一个 token 多久出来」上，所以**流式不是可选项，是核心接口的形状**。

```go
type EventType string

const (
	EventRunStart    EventType = "run_start"
	EventStepStart   EventType = "step_start"
	EventToken       EventType = "token"        // 增量文本
	EventToolStart   EventType = "tool_start"
	EventToolEnd     EventType = "tool_end"
	EventStepEnd     EventType = "step_end"
	EventInterrupt   EventType = "interrupt"    // 需要人工输入
	EventDone        EventType = "done"
	EventError       EventType = "error"
)

type Event struct {
	Type EventType      `json:"type"`
	Seq  int64          `json:"seq"`            // 单调递增，客户端据此去重/续传
	Step int            `json:"step"`
	Data map[string]any `json:"data,omitempty"` // 各事件的载荷，用 map 避免上帝类型
	At   time.Time      `json:"at"`
}

// Emitter 是「下推」抽象：引擎只管 Emit，不关心对面是 SSE、channel 还是测试切片。
type Emitter interface {
	Emit(ctx context.Context, ev Event) error
}

type EmitFunc func(ctx context.Context, ev Event) error

func (f EmitFunc) Emit(ctx context.Context, ev Event) error { return f(ctx, ev) }

// 丢弃所有事件（离线批处理、单测里不需要断言事件时用）
var Discard Emitter = EmitFunc(func(context.Context, Event) error { return nil })
```

> ⚠️ **`Emitter.Emit` 返回 error 是个设计决定**：如果客户端断开（SSE 写失败），必须让错误传回引擎，引擎才能在下一个安全点取消 ctx 并停止烧 token。**流式系统最贵的 bug 就是「客户端早就走了，模型还在生成」。**

### 53.4.3 ChatModel：一个方法，流式内建

```go
type ChatModel interface {
	// Stream 发起一次生成：增量通过 onDelta 回调吐出，返回值是聚合后的完整响应。
	// 约定：
	//   1) ctx 取消时必须在 1s 内返回 context.Canceled；
	//   2) onDelta 返回 error 时立即停止（客户端断开）；
	//   3) 不要在实现里吞掉 Usage，成本核算全靠它。
	Stream(ctx context.Context, req Request, onDelta func(Delta) error) (Response, error)
}

type Request struct {
	Model       string
	Messages    []Message
	Tools       []ToolSpec
	Temperature float64
	MaxTokens   int
	// ResponseSchema 非 nil 时要求结构化输出（供应商支持 response_format 时用它，否则走 prompt 约束 + 校验）
	ResponseSchema *jsonschema.Schema
	Extra          map[string]any // 供应商私有参数，透传
}

type Response struct {
	Message      Message
	FinishReason string // stop | tool_calls | length | content_filter | error
	Usage        Usage
}

type Usage struct {
	PromptTokens     int
	CompletionTokens int
	TotalTokens      int
	CostUSD          float64 // 由 provider 按价目表算，或由上层用 token 账本算
}

// Delta 是一次流式增量。
type Delta struct {
	Text          string    // 增量文本（可能为空）
	ToolCallDelta *ToolCall // 工具调用参数的增量拼接
	Usage         *Usage    // 有些供应商在最后一帧才给
}
```

**为什么不定义 `Generate` 和 `Stream` 两个方法？** 因为两个方法会让每个 provider 实现两遍（一份流式一份非流式），而「非流式」本质就是「忽略 delta 只取聚合结果」。一个方法 + 回调，实现成本减半，调用方需要非流式就传一个空回调：

```go
// 非流式调用的糖
func Generate(ctx context.Context, m ChatModel, req Request) (Response, error) {
	return m.Stream(ctx, req, func(Delta) error { return nil })
}
```

### 53.4.4 Tool：接口 + 注册表 + 一个必做的校验

```go
type Tool interface {
	// Spec 返回名字、描述、参数 JSON Schema。描述是给 LLM 看的，写清楚副作用。
	Spec() ToolSpec
	// Call 执行工具。args 是模型给的原始 JSON（可能缺字段、可能类型错），必须自己校验。
	Call(ctx context.Context, args json.RawMessage) (ToolResult, error)
}

type ToolSpec struct {
	Name        string             `json:"name"`
	Description string             `json:"description"`
	Parameters  *jsonschema.Schema `json:"parameters"`
	// Dangerous 为 true 的工具在人机协同模式下默认需要审批（写文件、发邮件、转账）
	Dangerous bool `json:"-"`
}

type ToolResult struct {
	Content string         `json:"content"`
	IsError bool           `json:"is_error"`          // 错误要回传给 LLM 让它自己纠正，不要 throw
	Meta    map[string]any `json:"meta,omitempty"`    // 耗时、命中缓存、数据源等
}
```

注册表（并发安全 + 忽略大小写 + 名称校验）：

```go
type Registry struct {
	mu    sync.RWMutex
	tools map[string]Tool
}

func NewRegistry() *Registry { return &Registry{tools: make(map[string]Tool)} }

func (r *Registry) Register(t Tool) error {
	s := t.Spec()
	if s.Name == "" || len(s.Name) > 64 {
		return fmt.Errorf("tool name %q: 1..64 chars", s.Name)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, dup := r.tools[s.Name]; dup {
		return fmt.Errorf("tool %q already registered", s.Name)
	}
	r.tools[s.Name] = t
	return nil
}

func (r *Registry) Get(name string) (Tool, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	t, ok := r.tools[name]
	return t, ok
}

// Specs 给 LLM 的工具清单。注意顺序要稳定（map 遍历是随机的），否则会破坏 prompt cache 命中率。
func (r *Registry) Specs() []ToolSpec {
	r.mu.RLock()
	names := make([]string, 0, len(r.tools))
	for n := range r.tools {
		names = append(names, n)
	}
	r.mu.RUnlock()
	sort.Strings(names)
	out := make([]ToolSpec, 0, len(names))
	for _, n := range names {
		out = append(out, r.tools[n].Spec())
	}
	return out
}
```

> 🩸 **「`Specs()` 必须排序」是一条真金白银的坑**：工具清单进了 system prompt，顺序一变，**prompt cache 全失效**，成本直接涨。同理，`Meta` 里的时间戳、map 序列化顺序，都会影响缓存命中。详见第 47 章。
>
> 结构体生成 JSON Schema 用 `github.com/invopop/jsonschema`：`jsonschema.Reflect(&WeatherArgs{})`，它能读 struct tag 和 `jsonschema:"description=..."`。详见第 37 章。

### 53.4.5 State 与错误分类

```go
// State 是图中流动的唯一可变状态。★ 必须可 JSON 序列化 ★（否则做不了检查点）
type State struct {
	Messages []Message      `json:"messages"`
	Vars     map[string]any `json:"vars"`   // 中间结果：plan、检索片段、评分
	Step     int            `json:"step"`
}

func NewState(msgs ...Message) *State {
	return &State{Messages: msgs, Vars: map[string]any{}}
}

// Clone 用于并行分支：每个分支拿到自己的副本，避免共享 map 打架。
func (s *State) Clone() *State {
	c := &State{Messages: slices.Clone(s.Messages), Vars: make(map[string]any, len(s.Vars)), Step: s.Step}
	for k, v := range s.Vars {
		c.Vars[k] = v
	}
	return c
}
```

```go
// errors.go：错误分类是重试策略的唯一依据（第 7 章）
var (
	ErrRateLimited  = errors.New("rate limited")     // 可重试，带退避
	ErrTransient    = errors.New("transient")        // 可重试一次
	ErrInvalidArgs  = errors.New("invalid arguments") // 不重试，回传给 LLM 让它改
	ErrMaxSteps     = errors.New("max steps exceeded")
	ErrInterrupted  = errors.New("interrupted")      // 需要人工介入，保存检查点退出
)

// 重试分类器：只有被 Wrap 成这两类的才重试
func Retryable(err error) bool {
	return errors.Is(err, ErrRateLimited) || errors.Is(err, ErrTransient)
}
```

---

## 53.5 provider 适配层：把「协议差异」全部关在这一层

适配层的职责就一句话：**把供应商的协议翻译成 core 的类型，双向翻译，别的一律不做**。这里最容易犯的错是把重试、日志、缓存写进 provider——那它们就没法复用了。

```go
package openai

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/you/agentkit/internal/core"
)

type Config struct {
	BaseURL string        // 兼容各种代理/网关
	APIKey  string
	HTTP    *http.Client  // 复用连接池，超时由 ctx 控
	Price   PriceTable    // 输入/输出每百万 token 单价
}

type Model struct {
	cfg Config
}

var _ core.ChatModel = (*Model)(nil) // 编译期断言：接口实现不完整时立刻报错
```

核心是三段映射，**全是可以独立单测的纯函数**：

```go
// 1) core.Request → 供应商 JSON 体
func toWireRequest(req core.Request) (wireRequest, error) {
	w := wireRequest{
		Model:       req.Model,
		Temperature: req.Temperature,
		MaxTokens:   req.MaxTokens,
		Stream:      true,
		StreamOptions: &streamOptions{IncludeUsage: true}, // 很多供应商默认流式不给 usage，必须显式要
	}
	for _, m := range req.Messages {
		w.Messages = append(w.Messages, toWireMessage(m))
	}
	for _, t := range req.Tools {
		w.Tools = append(w.Tools, wireTool{
			Type: "function",
			Function: wireFunction{
				Name:        t.Name,
				Description: t.Description,
				Parameters:  t.Parameters, // jsonschema.Schema 本身就能 marshal 成 JSON Schema
			},
		})
	}
	return w, nil
}

// 2) 供应商 SSE 帧 → core.Delta（增量），并累积出 core.Response
//    注意：这里的 SSE 解析细节（bufio.Scanner 缓冲上限、data: 分包、[DONE]）见第 40 章，
//         用 openai-go 之类的 SDK 时把 sse 那一段换成库调用，下面的映射逻辑不变。

// 3) 工具调用参数是分片到达的：id/name 只在第一片，arguments 每片都是字符串拼接
type toolCallAcc struct {
	ID   string
	Name string
	Args strings.Builder
}
```

> ⚠️ **第 3 点是并行工具调用最常见的 bug**：模型返回 `tool_calls` 时，每个 call 的 `arguments` 是**流式分片**的（`{"city":` / `"上海"` / `}`），你必须按 `index` 累加拼接，最后整体 `json.Valid` 校验一次。见过不止一次线上把半截 JSON 丢给工具执行、报 `unexpected end of JSON input`。详见第 41 章。

Ollama 的适配就是把 NDJSON 的行解码换成 SSE，映射函数几乎一样——所以两个 provider 加起来不到 500 行。

**Fake provider 是测试的基石**（写在 `provider/fake`，但要 import core，所以放 provider 层）：

```go
package fake

// Script 是一个剧本：第 n 次调用返回第 n 个响应。
// 这让"模型返回空 tool_calls""模型重复调用同一个工具"这类边界可以被确定性测试。
type Script struct {
	mu       sync.Mutex
	steps    []core.Response
	fallback core.Response
	Calls    []core.Request // 记录下来供断言
}

func (s *Script) Stream(ctx context.Context, req core.Request, onDelta func(core.Delta) error) (core.Response, error) {
	s.mu.Lock()
	s.Calls = append(s.Calls, req)
	var resp core.Response
	if len(s.steps) > 0 {
		resp, s.steps = s.steps[0], s.steps[1:]
	} else {
		resp = s.fallback
	}
	s.mu.Unlock()

	if err := ctx.Err(); err != nil { // 模拟真实 provider 的取消行为
		return core.Response{}, err
	}
	for _, p := range resp.Message.Parts { // 把文本切成小段模拟流式
		if p.Kind != core.PartText {
			continue
		}
		for _, chunk := range chunks(p.Text, 8) {
			if err := onDelta(core.Delta{Text: chunk}); err != nil {
				return core.Response{}, err
			}
		}
	}
	return resp, nil
}
```

---

## 53.6 编排引擎：图 + 并发 + 中断恢复

这是整个框架唯一有「算法难度」的地方，也是它区别于「一个 for 循环」的全部价值。

### 53.6.1 图定义

```go
package graph

type NodeFunc func(ctx context.Context, st *core.State, in Input) (string, error)

type Node struct {
	Name string
	Run  NodeFunc
	// Fanout 非空时该节点是「并行批次」：并发执行这些子节点，再进 Join。
	Fanout []string
	Join   string
}

type Graph struct {
	entry string
	nodes map[string]*Node
	// edges: 静态边，一个节点可以有多个后继（顺序即优先级）
	edges map[string][]string
	// conds: 条件边，返回目标节点名；返回 "" 表示走静态边
	conds map[string]CondFunc
}

type CondFunc func(st *core.State) (next string, err error)

// Builder：链式组装，编译期（Build）做一次全图校验，运行时不再检查
type Builder struct{ g *Graph }

func New(name string) *Builder { ... }

func (b *Builder) Add(name string, fn NodeFunc, opts ...NodeOpt) *Builder

// AddEdge 静态边；AddCond 条件边（优先于静态边）
func (b *Builder) AddEdge(from, to string) *Builder
func (b *Builder) AddCond(from string, fn CondFunc) *Builder

// SetEntry 必须显式指定入口，禁止"猜第一个 Add 的节点"
func (b *Builder) SetEntry(name string) *Builder

// Build 校验：入口存在、所有边指向的节点存在、无孤立节点、End 可达
func (b *Builder) Build() (*Graph, error)
```

`Build()` 的校验清单（**别省，这是把「运行时 panic」变成「启动失败」的关键**）：

| 校验项 | 不校验会怎样 |
| --- | --- |
| entry 非空且存在 | 启动即 panic 或空转 |
| 所有 `AddEdge` 目标节点存在 | 运行到那一步才 nil panic |
| 每个节点至少有一条出边或能到 `End` | 跑飞/死循环 |
| 并行批次的子节点不能互相有边 | 语义歧义（谁先跑？） |
| 节点名唯一、非空 | 覆盖注册，静默丢节点 |

### 53.6.2 执行主循环（含并行、中断、恢复）

```go
package graph

const End = "__end__"

type Runner struct {
	g         *Graph
	ckpt      CheckpointStore
	emit      core.Emitter
	hooks     []obs.Hook
	maxSteps  int
	runID     string
}

func (r *Runner) Invoke(ctx context.Context, st *core.State) (*core.State, error) {
	return r.resume(ctx, &Checkpoint{RunID: r.runID, Node: r.g.entry, State: *st, Seq: 0})
}

// Resume 从中断/崩溃处继续。★ 这是「持久化执行」的最小可用版本（对比 Temporal，详见第 46 章）
func (r *Runner) Resume(ctx context.Context, runID string) (*core.State, error) {
	ck, err := r.ckpt.Load(ctx, runID)
	if err != nil {
		return nil, fmt.Errorf("load checkpoint: %w", err)
	}
	return r.resume(ctx, ck)
}

func (r *Runner) resume(ctx context.Context, ck *Checkpoint) (*core.State, error) {
	st := &ck.State
	cur := ck.Node

	for {
		if err := ctx.Err(); err != nil {
			// 取消也要落检查点：用户关掉页面后重进能接着聊
			_ = r.save(ctx, ck, cur, st)
			return st, err
		}
		if st.Step >= r.maxSteps {
			return st, fmt.Errorf("%w: %d", core.ErrMaxSteps, st.Step)
		}
		st.Step++
		ck.Seq++

		node, ok := r.g.nodes[cur]
		if !ok {
			return st, fmt.Errorf("unknown node %q", cur)
		}
		if err := r.emit.Emit(ctx, core.Event{Type: core.EventStepStart, Seq: ck.Seq, Step: st.Step,
			Data: map[string]any{"node": cur}}); err != nil {
			return st, err // 客户端断开 → 立刻停，别继续烧 token
		}

		var (
			next string
			err  error
		)
		if len(node.Fanout) > 0 {
			next, err = r.runParallel(ctx, node, st, ck)
		} else {
			next, err = node.Run(ctx, st, Input{RunID: r.runID, Emit: r.emit, Step: st.Step})
		}

		// 中断：不是错误，是一种正常终态
		if errors.Is(err, core.ErrInterrupted) {
			_ = r.save(ctx, ck, next, st)
			_ = r.emit.Emit(ctx, core.Event{Type: core.EventInterrupt, Seq: ck.Seq, Step: st.Step})
			return st, core.ErrInterrupted
		}
		if err != nil {
			_ = r.save(ctx, ck, cur, st) // 保存现场，便于重放定位
			_ = r.emit.Emit(ctx, core.Event{Type: core.EventError, Seq: ck.Seq,
				Data: map[string]any{"node": cur, "err": err.Error()}})
			return st, fmt.Errorf("node %s: %w", cur, err)
		}

		// 决定下一跳：条件边优先
		if cond := r.g.conds[cur]; cond != nil {
			if next, err = cond(st); err != nil {
				return st, fmt.Errorf("cond %s: %w", cur, err)
			}
		}
		if next == "" {
			if outs := r.g.edges[cur]; len(outs) > 0 {
				next = outs[0] // 静态边只走第一条；要分支就用 AddCond
			} else {
				next = End
			}
		}

		_ = r.emit.Emit(ctx, core.Event{Type: core.EventStepEnd, Seq: ck.Seq, Step: st.Step,
			Data: map[string]any{"node": cur, "next": next}})
		_ = r.save(ctx, ck, next, st)

		if next == End {
			_ = r.emit.Emit(ctx, core.Event{Type: core.EventDone, Seq: ck.Seq, Step: st.Step})
			return st, nil
		}
		cur = next
	}
}
```

并行批次：每个子节点拿 State 的**副本**，最后按固定顺序合并（顺序固定 = 结果可复现 = 中断恢复时不抖动）：

```go
func (r *Runner) runParallel(ctx context.Context, n *Node, st *core.State, ck *Checkpoint) (string, error) {
	results := make([]*core.State, len(n.Fanout))
	g, gctx := errgroup.WithContext(ctx)
	g.SetLimit(len(n.Fanout)) // 有界并发：子节点数是设计期确定的，不需要信号量

	for i, name := range n.Fanout {
		i, name := i, name
		g.Go(func() error {
			sub := r.g.nodes[name]
			if sub == nil {
				return fmt.Errorf("fanout: unknown node %q", name)
			}
			local := st.Clone() // ★ 分支隔离：各自改各自的，不共享 map
			if _, err := sub.Run(gctx, local, Input{RunID: r.runID, Emit: r.emit, Step: st.Step}); err != nil {
				return fmt.Errorf("parallel %s: %w", name, err)
			}
			results[i] = local
			return nil
		})
	}
	if err := g.Wait(); err != nil {
		return "", err
	}

	// 合并：消息按分片顺序拼接（可复现），Vars 冲突时后者覆盖但记一条告警日志
	for _, s := range results {
		if s == nil {
			continue
		}
		st.Messages = append(st.Messages, s.Messages...)
		for k, v := range s.Vars {
			st.Vars[k] = v
		}
	}
	return n.Join, nil
}
```

> ⚠️ **`errgroup` + `SetLimit` 的语义先想清楚**：默认（`SetLimit(0)`）是「无限制并发」，`g.Wait()` 会等**所有** goroutine 结束；`WithContext` 的 errgroup 在**第一个错误**发生时取消 `gctx`，但其余 goroutine 仍然会被 Wait 等到——所以子节点**必须尊重 ctx 取消**，否则「一个失败，其余还在跑 30 秒」。详见第 26 章。

### 53.6.3 中断与恢复：三件事必须一起做

```
   工具执行前，策略要求人工审批
            │
            ▼
  ┌──────────────────────┐   ① 保存检查点（State + 当前节点 + Seq）
  │ return ErrInterrupted│   ② 发 EventInterrupt（带上待审批的工具名/参数摘要）
  └──────────────────────┘   ③ 进程可以安全退出（不再持有 goroutine/连接）
            │
       （用户 5 分钟后点了"同意"）
            ▼
  Resume(runID) → 从检查点恢复 → 工具真正执行
```

三个必须做对的点：

| 点 | 规则 | 踩错的后果 |
| --- | --- | --- |
| 检查点内容 | 只存**可序列化的 State**，不存函数、不存连接 | 恢复时无法反序列化，等于没存 |
| 副作用幂等 | 恢复后可能重跑同一个节点，工具必须带 `idempotency_key` | 重复发邮件、重复扣款 |
| Seq 与事件 | 检查点带 `Seq`，恢复后从 `Seq+1` 继续发事件 | 客户端看到重复 token（尤其 SSE 断线重连） |

```go
type Checkpoint struct {
	RunID     string       `json:"run_id"`
	Seq       int64        `json:"seq"`
	Node      string       `json:"node"`
	State     core.State   `json:"state"`       // ★ 全量快照，够简单；量大再改增量（事件溯源）
	CreatedAt time.Time    `json:"created_at"`
}

type CheckpointStore interface {
	Save(ctx context.Context, ck *Checkpoint) error
	Load(ctx context.Context, runID string) (*Checkpoint, error)
	Delete(ctx context.Context, runID string) error
}
```

生产建议：`Seq` 单调 + `Save` 用「写新行」而不是 UPsert（保留历史，便于回放），TTL 7 天，落 Postgres 或 Redis（Redis 要开 AOF）。

### 53.6.4 ReAct 就是一个图（`agent/react.go`）

```go
func NewReAct(model core.ChatModel, tools *core.Registry, maxSteps int) (*graph.Graph, error) {
	return graph.New("react").
		Add("llm", func(ctx context.Context, st *core.State, in graph.Input) (string, error) {
			resp, err := model.Stream(ctx, core.Request{
				Messages: st.Messages,
				Tools:    tools.Specs(),
			}, func(d core.Delta) error {
				if d.Text == "" {
					return nil
				}
				return in.Emit.Emit(ctx, core.Event{Type: core.EventToken, Step: in.Step,
					Data: map[string]any{"text": d.Text}})
			})
			if err != nil {
				return "", fmt.Errorf("llm: %w", err)
			}
			st.Messages = append(st.Messages, resp.Message)
			st.Vars["usage"] = resp.Usage
			return "", nil // 由条件边决定下一步
		}).
		Add("tools", func(ctx context.Context, st *core.State, in graph.Input) (string, error) {
			return "", execToolCalls(ctx, st, tools, in) // 并行执行本轮的多个工具调用
		}).
		SetEntry("llm").
		AddCond("llm", func(st *core.State) (string, error) {
			last := st.Messages[len(st.Messages)-1]
			if calls := last.ToolCalls(); len(calls) > 0 {
				return "tools", nil
			}
			return graph.End, nil // 没有工具调用 = 收敛
		}).
		AddEdge("tools", "llm").
		Build()
}
```

> 💡 注意这个设计的几个取舍：① **终止条件是「模型不再要求工具」而不是步数**，步数只做安全阀（`maxSteps`）；② 工具执行用 `errgroup` 并发，任一失败**不中断整轮**——把错误作为 `ToolResult{IsError:true}` 回传给模型让它自己重试或换路（第 41 章的重试分类学）；③ 整张图只有两个节点，但**未来加「反思」「人工审批」「检索」只是 `Add` 一个节点**，不用改引擎。

---

## 53.7 存储层：会话 / 记忆 / 向量

### 53.7.1 三个接口，一个原则

```go
type SessionStore interface {
	Load(ctx context.Context, key SessionKey) (*Session, error)
	// Append 用「乐观并发」：带上版本号，冲突就报 ErrConflict 让上层重试或合并
	Append(ctx context.Context, key SessionKey, msgs []core.Message, wantVersion int64) (int64, error)
}

type SessionKey struct {
	TenantID  string // ★ 多租户隔离的第一道墙，必须在 key 里而不是在过滤条件里
	SessionID string
}

type Session struct {
	Key      SessionKey
	Version  int64
	Messages []core.Message
	Summary  string // 滚动摘要
	UpdatedAt time.Time
}
```

> 🩸 **`TenantID` 必须进 key** 是安全要求，不是性能优化。见过一次「查会话按 session_id 查」，结果 A 租户猜到了 B 租户的 uuid，直接读了别人的对话历史。**租户隔离要体现在存储层的 API 签名上**（详见第 47 章的多租户部分）。

### 53.7.2 记忆：窗口 + 摘要（token 预算决定一切）

```go
// memory.Manager 负责把"无限历史"压成"能塞进上下文的一段"
type Manager struct {
	MaxTokens   int              // 模型窗口的 70%，留 30% 给输出和工具结果
	KeepLast    int              // 无论如何保留最近 N 条（用户刚说的必须留着）
	Summarizer  core.ChatModel   // 用小模型做摘要，成本低一个量级
}

func (m *Manager) Fit(ctx context.Context, sess *Session) ([]core.Message, error) {
	used := m.count(sess.Messages)
	if used <= m.MaxTokens {
		return sess.Messages, nil
	}
	// 策略：system 保留 → 最近 KeepLast 条保留 → 中间部分压缩成摘要
	head, body, tail := split(sess.Messages, m.KeepLast)
	if sess.Summary == "" && len(body) > 0 {
		sum, err := m.summarize(ctx, body) // 摘要失败就降级为直接丢弃（并打 warning）
		if err == nil {
			sess.Summary = sum
		}
	}
	out := slices.Clone(head)
	if sess.Summary != "" {
		out = append(out, core.TextMessage(core.RoleSystem, "此前对话摘要："+sess.Summary))
	}
	return append(out, tail...), nil
}
```

⚠️ **不要按「消息条数」裁剪，要按「token 数」裁剪**。一条带 5 万字符的工具结果，抵得上 200 条普通消息——按条数裁的代码会稳定收到 `context_length_exceeded`。详见第 45 章。

### 53.7.3 向量：只给最小的两个方法

```go
type VectorStore interface {
	Upsert(ctx context.Context, namespace string, docs []Document) error
	// Search 必须支持按 tenant 过滤：namespace = tenant:collection
	Search(ctx context.Context, namespace string, vec []float32, topK int, filter map[string]any) ([]Document, error)
}
```

实现用 pgvector（`github.com/pgvector/pgvector-go`），SQL 就一句：

```sql
SELECT id, content, 1 - (embedding <=> $1) AS score
FROM documents
WHERE namespace = $2 AND (metadata @> $3)
ORDER BY embedding <=> $1
LIMIT $4;
```

索引选型：`ivfflat` 建得快、召回随 `lists` 增长；`hnsw` 查询快、内存占用高。10 万条以内 ivfflat 足够，百万级再上 HNSW。详见第 44 章。

---

## 53.8 可观测：Hook 是旁路，绝不阻塞主路径

```go
package obs

// Hook 全是只读观察点。★ 实现里禁止 panic、禁止阻塞超过 1ms ★
type Hook interface {
	OnRunStart(ctx context.Context, info RunInfo)
	OnStepStart(ctx context.Context, runID string, step int, node string)
	OnLLMStart(ctx context.Context, runID, model string, req *core.Request)
	OnLLMEnd(ctx context.Context, runID string, resp *core.Response, err error)
	OnToolStart(ctx context.Context, runID, tool string, argsLen int)
	OnToolEnd(ctx context.Context, runID, tool string, d time.Duration, err error)
	OnRunEnd(ctx context.Context, runID string, st *core.State, err error)
}

// Base 提供全是空实现的基础版：嵌入它，只覆写你关心的 1~2 个方法。
// 这样加一个 Hook 字段时不会打爆所有实现（Go 没有 default method，这是标准做法）
type Base struct{}

func (Base) OnRunStart(context.Context, RunInfo)                                     {}
func (Base) OnStepStart(context.Context, string, int, string)                         {}
func (Base) OnLLMStart(context.Context, string, string, *core.Request)                {}
func (Base) OnLLMEnd(context.Context, string, *core.Response, error)                  {}
func (Base) OnToolStart(context.Context, string, string, int)                         {}
func (Base) OnToolEnd(context.Context, string, string, time.Duration, error)          {}
func (Base) OnRunEnd(context.Context, string, *core.State, error)                     {}

// Multi 把多个 Hook 组合成一个。⚠️ 一定要 recover：一个 Hook panic 不能带走整个 Agent
type Multi []Hook

func (m Multi) OnLLMEnd(ctx context.Context, id string, resp *core.Response, err error) {
	for _, h := range m {
		func() {
			defer func() { _ = recover() }()
			h.OnLLMEnd(ctx, id, resp, err)
		}()
	}
}
```

OTel Hook 里最值钱的三个属性（用 **GenAI 语义约定**，别自己造名字）：

| span | 关键属性 | 用途 |
| --- | --- | --- |
| `chat {model}` | `gen_ai.system`、`gen_ai.request.model`、`gen_ai.usage.input_tokens`、`gen_ai.usage.output_tokens` | 成本、模型对比 |
| `execute_tool {tool}` | `gen_ai.tool.name`、`gen_ai.tool.call.id`、结果大小 | 工具热点、失败率 |
| `agent.run` | `run_id`、`steps`、`total_cost_usd`、`finish_reason` | 端到端 SLO |

> ⚠️ **不要把 prompt 和工具返回体塞进 span attribute**。一条 8K token 的 prompt 会让 trace 存储成本直接爆炸，还可能把用户隐私写进第三方系统。只记 `len`、`hash`，正文留在你自己的日志系统里做关联。详见第 35、52 章。

---

## 53.9 装配：`main` 里的 40 行，胜过 Spring 的 2000 行配置

```go
func run() error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	cfg, err := config.Load()
	if err != nil {
		return err
	}

	// 1) 基础设施
	rdb := redis.NewClient(&redis.Options{Addr: cfg.RedisAddr})
	db, err := pgxpool.New(ctx, cfg.DBDSN)
	if err != nil {
		return fmt.Errorf("db: %w", err)
	}
	defer db.Close()

	// 2) provider（注册表：名字 → 构造函数，加供应商只改这里一行）
	models := map[string]core.ChatModel{
		"openai": openai.New(openai.Config{BaseURL: cfg.OpenAIBase, APIKey: cfg.OpenAIKey, HTTP: httpClient}),
		"ollama": ollama.New(ollama.Config{BaseURL: cfg.OllamaAddr, HTTP: httpClient}),
	}
	chat, ok := models[cfg.DefaultModel]
	if !ok {
		return fmt.Errorf("unknown model provider %q", cfg.DefaultModel)
	}

	// 3) 工具
	tools := core.NewRegistry()
	for _, t := range []core.Tool{
		toolsx.NewWeather(),
		toolsx.NewSearch(cfg.SearchKey),
	} {
		if err := tools.Register(t); err != nil {
			return err
		}
	}

	// 4) 编排 + 存储 + 可观测
	g, err := agent.NewReAct(chat, tools, cfg.MaxSteps)
	if err != nil {
		return err
	}
	sessions := redisstore.NewSession(rdb)
	hooks := obs.Multi{obs.NewSlog(), obs.NewOTel(tracer)}

	runner := graph.NewRunner(g,
		graph.WithCheckpointStore(redisstore.NewCheckpoint(rdb)),
		graph.WithEmitter(core.Discard), // HTTP 层会用 per-request 的 emitter 覆盖
		graph.WithHooks(hooks),
		graph.WithMaxSteps(cfg.MaxSteps),
	)

	// 5) 传输层
	srv := &http.Server{Addr: cfg.Addr, Handler: transport.NewHTTP(runner, sessions), ReadHeaderTimeout: 5 * time.Second}

	errc := make(chan error, 1)
	go func() { errc <- srv.ListenAndServe() }()

	select {
	case err := <-errc:
		return err
	case <-ctx.Done():
	}
	shCtx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()
	return srv.Shutdown(shCtx) // 优雅关停的完整姿势见第 52 章
}
```

`transport/http.go` 的核心就是「把 Event 翻成 SSE 帧」，二十行：

```go
func (h *HTTP) chat(w http.ResponseWriter, r *http.Request) {
	var in chatRequest
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	// SSE 三件套
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("X-Accel-Buffering", "no") // 关掉 nginx 缓冲，否则 token 会攒着一起发
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming unsupported", http.StatusInternalServerError)
		return
	}

	emit := core.EmitFunc(func(ctx context.Context, ev core.Event) error {
		b, err := json.Marshal(ev)
		if err != nil {
			return err
		}
		if _, err := fmt.Fprintf(w, "id: %d\nevent: %s\ndata: %s\n\n", ev.Seq, ev.Type, b); err != nil {
			return err // ★ 写失败 = 客户端走了，必须让引擎停下来
		}
		flusher.Flush()
		return nil
	})

	if _, err := h.runner.WithEmitter(emit).Invoke(r.Context(), core.NewState(in.Messages...)); err != nil &&
		!errors.Is(err, context.Canceled) {
		_ = emit(r.Context(), core.Event{Type: core.EventError, Data: map[string]any{"err": err.Error()}})
	}
}
```

---

## 53.10 测试策略：四层，成本从低到高

| 层 | 测什么 | 工具 | 成本 | 目标覆盖 |
| --- | --- | --- | --- | --- |
| 纯函数 | provider 映射、token 计数、schema 生成、裁剪 | 表驱动 + `t.Run` | 纳秒 | 全覆盖，100% |
| 图执行 | 分支、并行、中断恢复、maxSteps | `fake.Model` 剧本 | 微秒 | 每条边至少 1 例 |
| HTTP/SSE | 帧格式、`Last-Event-ID` 续传、客户端断开取消 | `httptest` + `golden/.sse` | 毫秒 | 主流程 + 断线 |
| 真模型 | prompt 效果、工具调用准确性 | 每日定时任务 / 离线评估集 | 美元 | 不进 CI，进评估流水线 |

三个具体技巧：

```go
// ① golden 断线重连：把一次真实 SSE 流录下来，回放时断言 Seq 连续性
func TestSSESeqIsMonotonic(t *testing.T) {
	// ...调用 handler，收集所有 data 帧
	var last int64
	for _, ev := range collect(t, rec.Body) {
		if ev.Seq <= last {
			t.Fatalf("seq not monotonic: %d after %d", ev.Seq, last)
		}
		last = ev.Seq
	}
}

// ② 用 synctest（Go 1.25）测超时与退避，不用真的睡
func TestRetryBackoff(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		// 假时钟推进 2s，断言退避曲线是 100ms → 200ms → 400ms
	})
}

// ③ fuzz：模型给的工具参数什么形状都有，解析必须不 panic
func FuzzToolArgs(f *testing.F) {
	f.Add([]byte(`{"city":"上海"}`))
	f.Add([]byte(`{"city":`))
	f.Fuzz(func(t *testing.T, b []byte) {
		_, _ = toolsx.NewWeather().Call(context.Background(), b) // 只要求不 panic
	})
}
```

> 💡 **Agent 项目的测试第一原则：把「模型」当成一个可以随便编排的依赖**。只要 `ChatModel` 是接口，`fake.Script` 就能造出「返回空 tool_calls」「连续 5 次调同一个工具」「返回超长文本」这些用真模型几乎造不出来的边界。详见第 34 章。

---

## 53.11 踩坑清单（写框架 / 读框架都用得上）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| 客户端断开后 token 还在烧 | Emit 的错误被吞，或 ctx 没往 provider 传 | `Emit` 返回 error 一路透传；`Stream` 必须尊重 ctx | 53.4.2 |
| 并行工具结果互相覆盖 | 并行分支共享同一个 `State.Vars` | 分支 `State.Clone()`，合并时按固定顺序 | 53.6.2 |
| 重启后对话从头上开始 | 只在内存里存状态 | 每个节点后落检点，`Resume(runID)` | 53.6.3 |
| 恢复后重复扣款/重复发邮件 | 工具没有幂等键 | `idempotency_key = runID + step + tool` | 53.6.3 |
| `context_length_exceeded` | 按「条数」而不是「token」裁剪 | 用 token 预算 + 摘要 | 53.7.2 |
| prompt cache 命中率突然归零 | 工具清单 / system prompt 顺序不稳定（map 遍历） | `Specs()` 排序，模板里不塞时间戳 | 53.4.4 |
| A 租户读到 B 租户会话 | 租户 ID 没进存储 key | `SessionKey{TenantID, SessionID}` | 53.7.1 |
| 一个 Hook panic 带走整个请求 | Hook 没 recover | `Multi` 里 `defer recover()` | 53.8 |
| trace 账单暴涨 | prompt / 工具结果进了 span attribute | 只记长度和 hash | 53.8 |
| `unexpected end of JSON input` | 工具参数流式分片没拼完就解析 | 按 index 累加，收尾再 `json.Valid` | 53.5 |
| 换供应商要改 7 个文件 | 业务代码直接使用了 SDK 类型 | 业务只认 `core.ChatModel` | 53.2 |
| 框架 2 万行，没人敢改 | 过度抽象 + 三层 DTO 映射 | 一个类型用到尾，接口 ≤3 方法 | 53.2 / 第 54 章 |

---

## 53.12 扩展点清单（给未来的自己和团队）

| 想加什么 | 改哪里 | 要不要动引擎 | 难度 |
| --- | --- | --- | --- |
| 新模型供应商 | 新增 `provider/xxx`，实现 `core.ChatModel` | 否 | 低（1 天） |
| 新工具 | 实现 `core.Tool`，`Registry.Register` | 否 | 低（半天） |
| 新编排（Plan-Execute / Debate） | 新增 `agent/xxx.go`，`graph.Builder` 组装 | 否 | 中（2~3 天） |
| 新持久化后端 | 实现 `SessionStore` / `CheckpointStore` / `VectorStore` | 否 | 低（1 天） |
| 新观测后端 | 实现 `obs.Hook`，嵌 `obs.Base` | 否 | 低（半天） |
| 新传输协议 | 新增 `transport/grpc.go`，把 Event 映射成 stream | 否 | 中（2 天） |
| 新事件类型 | `core.EventType` 加常量 + 客户端约定 | 否 | 低 |
| **改执行语义**（比如加「节点级重试」） | `graph/runner.go` | **是** | 高（要同步改检查点格式） |
| **改 State 形状** | `core/state.go` + 所有检查点迁移 | **是** | 高（要做版本迁移） |

> 🎯 这张表是框架健康度的体温计：**如果你发现自己经常要动「引擎」，说明接口形状错了**。前 7 行是「加东西」，后 2 行是「改内核」——成熟的框架应该让 90% 的需求落在前 7 行。

---

## 53.13 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 分层 | core（零依赖）← provider/store/obs/graph ← agent ← transport ← cmd，单向；core 不 import 本项目任何包 |
| Message | 用 `Parts []Part` 而不是 `Content string`，否则加多模态就得改协议 |
| ChatModel | 只有 `Stream(ctx, req, onDelta)` 一个方法；非流式是它的糖 |
| Tool | `Spec()` 给 LLM 看、`Call()` 收原始 JSON 自己校验；错误回传给模型，不 panic |
| 图 | `Builder` 链式组装 + `Build()` 一次性校验；运行时零反射 |
| 并行 | 分支 `State.Clone()`，合并按固定顺序（可复现） |
| 中断恢复 | 检查点 = 可序列化 State + 节点 + Seq；副作用必须幂等 |
| 记忆与存储 | 按 token 预算裁剪 + 滚动摘要；`TenantID` 必须进 key，会话用乐观锁版本号 |
| 观测与测试 | Hook 旁路 + recover，span 只记长度和 hash；fake provider 剧本 + golden SSE |
| 健康度 | 90% 需求应落在「扩展点清单」前 7 行，而不是改引擎 |

## 53.14 自检题

**1. 为什么 `core.ChatModel` 只定义 `Stream` 一个方法，而不像 Java 那样定义 `generate()` 和 `generateStream()` 两个？**

<details><summary>答案要点</summary>

- 两个方法 = 每个 provider 实现两遍，且两份逻辑必须保持一致（历史上无数 provider 的「流式和非流式行为不一致」bug 就出在这里）。
- 非流式本质是「忽略增量只取聚合结果」，用 `onDelta` 传空函数即可（`Generate` 糖函数）。
- 一个方法还强制了「流式优先」：框架默认就能把 token 吐给用户，TTFT 天然可控。
- 代价是调用方必须理解回调语义，且不能中途 `return` 取值——但这正好逼出正确的取消处理。

</details>

**2. 你的 Agent 在第 7 步调用了一个「转账」工具，用户点了「审批」后进程重启了。为了让恢复后不重复转账，你需要哪三样东西？**

<details><summary>答案要点</summary>

- ① **检查点**：在第 7 步之前/之后把 `State + 节点名 + Seq` 落到持久存储（`CheckpointStore`），并且 State 必须完全可序列化（不含函数、连接）。
- ② **幂等键**：工具调用带 `idempotency_key = runID + step + tool + argsHash`，服务端（或工具实现）对同一 key 只执行一次副作用。
- ③ **事件 Seq 续传**：恢复后从 `Seq+1` 继续发事件，客户端凭 `id` 去重/续接，否则用户会看到重复文本。
- 补充：还需要「中断是一种正常终态」的语义——`ErrInterrupted` 不能当成错误触发告警或重试风暴。

</details>

**3. 你的并行分支跑完发现 `State.Vars["plan"]` 被覆盖了，为什么？正确的合并策略是什么？**

<details><summary>答案要点</summary>

- 因为并行分支共享同一个 `State`（或只 clone 了 slice 却共享了 map），多个 goroutine 同时写同一个 map：要么数据竞争（`-race` 会报），要么后写覆盖先写。
- 正确做法：每个分支 `State.Clone()`（map 也要新建 + 逐键拷贝），分支结果各自收集，最后**在主 goroutine 里按固定顺序（如 `Fanout` 声明顺序）合并**。
- 冲突处理要有明确规则：list 类字段 append，标量类字段「后者覆盖」或「按 key 命名空间隔离（`plan.a` / `plan.b`）」，并在覆盖时打一条 debug 日志。
- 想更严格：合并阶段用显式的 `Merge func(dst, src *State) error` 由图的作者实现，引擎只负责调度。

</details>

## 53.15 延伸阅读

- eino 源码：`github.com/cloudwego/eino` — `compose/graph.go`（图编译）、`compose/runnable.go`（执行与流）、`schema/message.go`（对照本章 `Message`）
- langchaingo：`github.com/tmc/langchaingo` — `schema/`（消息与文档抽象）、`llms/`（provider 层）、`agents/`（ReAct 循环）
- MCP Go SDK：`github.com/modelcontextprotocol/go-sdk` — 工具与 `ToolSpec` 的协议映射
- Temporal Go SDK：`go.temporal.io/sdk` — 看它的 `workflow.Context` 与确定性重放，是「检查点 + 恢复」的工业级答案
- `github.com/invopop/jsonschema` — 从 struct 生成工具参数 Schema
- 本书相关：[第 43 章 Agent 循环与编排引擎](43-agent-loop-orchestration.md)（循环语义）、[第 46 章 持久化执行](46-multi-agent-durable.md)（对比 Temporal）、[第 48、49 章 源码精读](48-langchaingo-source.md)

---

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 52 章 生产部署与 SRE](52-deployment-sre.md)（本章的装配与关停直接复用其模板）
- 相关：[第 10 章 组合与注册表](10-composition-plugin-registry.md)（`Registry` 的设计来源）、[第 24 章 context](24-context.md)、[第 26 章 结构化并发](26-structured-concurrency.md)（并行批次）、[第 37 章 代码生成与反射](37-codegen-reflection.md)（JSON Schema）、[第 40 章 LLM 客户端](40-llm-client-streaming.md)（SSE 细节）、[第 41 章 工具系统](41-tool-system-function-calling.md)、[第 43 章 编排引擎](43-agent-loop-orchestration.md)、[第 44 章 RAG](44-rag-pipeline.md)、[第 45 章 记忆与会话](45-memory-session.md)、[第 46 章 持久化执行](46-multi-agent-durable.md)、[第 47 章 Agent 生产化](47-agent-productionization.md)、[第 34 章 测试](34-testing.md)、[第 35 章 可观测性](35-observability.md)
- 下一章：[第 54 章 Java 架构师的 Go 落地清单](54-java-architect-handbook.md)
