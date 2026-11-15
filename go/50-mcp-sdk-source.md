# 50. MCP Go SDK 源码精读

> **状态**：`已完成（初稿）` ｜ **所属**：第八部分 · 源码精读 ｜ **编号**：50 / 54
> **本章定位**：前面第 42 章教你「怎么用 MCP」，这一章拆「它凭什么这么薄」——`Transport` 只有一个方法、`Connection` 只有四个方法，剩下全是方法表驱动的 dispatch。读完你能自己写一个 transport，也能判断什么时候该绕开 SDK。
> **读法**：先看 50.2 的协议分层图，再按 50.3 的 ①→⑥ 顺序读源码。50.4 的 Java 对照只讲「你会猜错的地方」。
> **版本基准**：`github.com/modelcontextprotocol/go-sdk`（模块 v1.x，2026 年主线，mcp 包 v1.8 左右）。**MCP 协议本身在 2025→2026 发生了较大变化**（最新协议版本 `2026-07-28`），很多老博客里的写法已经过时，文中会标注。

---

## 50.1 引子：三个「不该发生」的 Bug

**Bug 1（周五下午 5 点）**：本地 MCP Server 上线后客户端报 `invalid character 'L' looking for beginning of value`。原因：有人在 stdio server 里加了一行 `fmt.Println("tool called!")`。**stdout 是协议通道**，一行调试输出就把 JSON-RPC 帧撞碎了。

**Bug 2（压测时）**：一个 `search` 工具要跑 8 秒，同一 session 上用户紧接着点了「换个词再搜」，第二个请求一直不返回。原因：SDK 在**单连接内是串行 dispatch 的**（`handleAsync` 一次只跑一个 handler），你的慢工具把后面的请求全堵住了。

**Bug 3（安全评审）**：浏览器里一个恶意网页通过 DNS rebinding 把 `http://127.0.0.1:8080/mcp` 当成了自己的后端，直接调你机器上的本地 MCP Server 读写文件。

这三个 bug 各自对应源码里的一段设计：**framing 在 transport 层**、**并发模型在 jsonrpc2 层**、**本地服务防护在 streamable HTTP 层**。这一章就按这三层往下钻。

---

## 50.2 直觉模型：五层协议，两次「薄封装」

```text
┌──────────────────────────────────────────────────────────────────────┐
│ ⑤ 应用语义层     tools/call · resources/read · prompts/get           │
│    mcp/tool.go  mcp/resource.go  mcp/prompt.go  mcp/protocol.go      │
│    methodInfo 表：method 字符串 → {params 类型, result 类型, 适配器}  │
├──────────────────────────────────────────────────────────────────────┤
│ ④ 会话层         initialize / server-discover · 能力协商 · 分页       │
│    ServerSessionState{InitializeParams, NegotiatedProtocolVersion}   │
│    Mcp-Session-Id header（streamable HTTP）· 请求 ID ↔ stream 路由    │
├──────────────────────────────────────────────────────────────────────┤
│ ③ JSON-RPC 2.0   Request/Response/Notification · ID 关联 · 取消      │
│    jsonrpc/（wire 类型，公开）  internal/jsonrpc2/（连接与调度）      │
│    Preempter 抢跑取消通知；handler 队列串行；写响应与超时            │
├──────────────────────────────────────────────────────────────────────┤
│ ② Framing       换行分隔 JSON（stdio/IO）· SSE event（streamable）   │
│    jsonrpc.EncodeMessage / DecodeMessage                            │
├──────────────────────────────────────────────────────────────────────┤
│ ① Transport     StdioTransport · IOTransport · CommandTransport      │
│                 InMemoryTransport · StreamableServerTransport        │
│                 ── 接口只有 1 个方法：Connect(ctx) (Connection, err)  │
└──────────────────────────────────────────────────────────────────────┘
```

**一句话**：①→② 是「字节怎么切成一帧」，③ 是「帧怎么变成一次 RPC」，④⑤ 是「RPC 怎么变成一次工具调用」。**SDK 的瘦，瘦在每层只做一件事。**

### 传输方式一览（选型先看这张表）

| Transport | 分帧方式 | 双向能力 | 典型场景 | 关键限制 |
| --- | --- | --- | --- | --- |
| `StdioTransport` | 换行分隔 JSON（stdin/stdout） | 天然双向 | 本地子进程 server（Claude Desktop 那类） | **stdout 只能跑协议**；日志必须走 stderr |
| `IOTransport` | 同上，任意 `io.Reader/Writer` | 双向 | 容器 stdin、测试、自建隧道 | 自己管 `Close` 语义 |
| `CommandTransport` | 同上，内部起子进程 | 双向 | 客户端拉起本地 server | 关停要按 spec：关 stdin → 等 → SIGTERM → SIGKILL（默认 5s） |
| `InMemoryTransport` | `net.Pipe()` | 双向 | 单测（`NewInMemoryTransports()` 返回一对） | 只管内存，无跨进程 |
| Streamable HTTP | POST 请求体 = JSON-RPC；响应 SSE 或 JSON | 半双工→可恢复 | 远程/多租户 server | 需要 session + EventStore 才能断点续传 |
| SSE（旧） | GET 建流 + POST 上行 | 双向 | 老版本兼容 | 协议演进后已被 streamable HTTP 取代 |

---

## 50.3 底层实现：六段源码

### 50.3.1 目录树（认路）

```text
go-sdk/
├── jsonrpc/                 # 公开的 wire 类型：Message / Request / Response / ID + Encode/Decode
├── mcp/                     # 主体：协议 + server + client + transport
│   ├── transport.go         # Transport / Connection 接口；Stdio/IO/InMemory；connect() 装配
│   ├── cmd.go               # CommandTransport（拉起子进程）
│   ├── streamable.go        # StreamableHTTPOptions + 客户端/服务端实现（含设计文档注释！）
│   ├── streamable_server.go # 服务端分层文档：Handler → Transport → Conn → stream
│   ├── server.go            # Server struct · NewServer · AddTool · capability 推断 · 并发
│   ├── client.go            # Client / ClientOptions / ClientSession 全部调用方法
│   ├── session.go           # ServerSessionState（能力协商结果）
│   ├── protocol.go          # 协议类型：Capabilities / Implementation / 各 Params/Result
│   ├── shared.go            # MethodHandler / Middleware / 协议版本表 / 协商逻辑
│   ├── tool.go              # ToolHandler / ToolHandlerFor / schema 校验
│   └── mrtr.go features.go logging.go cache.go event.go
├── internal/jsonrpc2/       # 连接与调度（不公开，但最值得读）
│   ├── conn.go              # Connection：读循环 · handler 队列 · 取消 · 超时
│   ├── messages.go wire.go  # 编解码与 ID 表示
│   └── jsonrpc2.go          # Handler / Preempter 接口
├── auth/ oauthex/           # OAuth 2.1 / 资源元数据（远程 server 的鉴权）
└── examples/                # custom-transport、middleware、distributed 等可直接跑
```

### 50.3.2 源码精读 ①：`Transport` 与 `Connection`（薄到极致的抽象）

```go
// A Transport is used to create a bidirectional connection between MCP client
// and server. Transports should be used for at most one call to Server.Connect
// or Client.Connect.
type Transport interface {
	Connect(ctx context.Context) (Connection, error)
}

type Connection interface {
	Read(context.Context) (jsonrpc.Message, error)  // 必须是并发安全的：Close 要能打断 Read
	Write(context.Context, jsonrpc.Message) error   // 必须支持并发写
	Close() error                                   // 可多次、可并发调用
	SessionID() string
}
```

**判断**：MCP 把「传输」压缩成「你给我一个能读能写消息的管道」。于是：

- 自定义传输的成本 ≈ 写 5 个方法（见 50.3.6）。对比 gRPC 要定义 `Stream` 的五种状态机，这是**质**的差别。
- 上面那行注释 **"at most one call"** 是硬约束：`Transport` 不是连接池，**每个会话一个 Transport 实例**。多租户就得每个租户一份。
- `Connection` 的三条并发契约（Read 与 Close 并发安全、Write 可并发、Close 幂等）不是客套话——`jsonrpc2` 的读循环、写响应、心跳都是不同 goroutine。自己实现漏一条，就是「偶发卡死」。

### 50.3.3 源码精读 ②：`connect()` —— 三行装配，五处扩展点

`transport.go` 里的 `connect` 是**整个 SDK 的装配中枢**（简写）：

```go
func connect[H handler, State any](ctx context.Context, t Transport, b binder[H, State],
	s State, onClose func(), logger *slog.Logger) (H, error) {

	mcpConn, err := t.Connect(ctx)                     // ① transport 出连接
	reader, writer := jsonrpc2.Reader(mcpConn), jsonrpc2.Writer(mcpConn) // ② 适配成帧流

	mcp2rpc := func(conn *jsonrpc2.Connection) jsonrpc2.Handler {
		h = b.bind(mcpConn, conn, s, onClose)          // ③ 绑定 MCP 会话（client 或 server）
		preempter.conn = conn
		return jsonrpc2.HandlerFunc(h.handle)          // ④ 方法表 dispatch
	}

	propagate := false
	if cp, ok := mcpConn.(cancellationPropagator); ok { // ⑤ 可选：把 HTTP 请求取消传进 handler
		propagate = cp.propagateCancellation()
	}

	_ = jsonrpc2.NewConnection(ctx, jsonrpc2.ConnectionConfig{
		Reader: reader, Writer: writer, Closer: mcpConn,
		Bind: mcp2rpc,
		Preempter: &canceller{logger: logger},          // 取消通知走「抢跑」通道
		OnDone: func() { b.disconnect(h) },
		PropagateCancellation: propagate,
	})
	return h, nil
}
```

**逐点挑设计**：

- ①②之间是**唯一**知道「帧怎么切」的地方。所以换 framing（换行 / SSE / 自定义长度前缀）**不需要动协议层**。
- ③ 用泛型 `binder[H, State]` 让 client 和 server 复用同一条装配路径：server 的 `State` 是 `*ServerSession` 待创建参数，client 是 `*clientSessionState`。
- ⑤ `cancellationPropagator` 这个可选接口，是给「一个 HTTP 请求就是一个生命周期」的 streamable HTTP 用的：**HTTP 断了，handler 的 ctx 就该取消**。默认**不传播**——因为默认的 MCP 连接比某个请求活得长。这是个典型的「默认保守、需要时显式打开」的 Go 式设计。
- `preempter` 负责「抢跑」：`notifications/cancelled` 不进 handler 队列，直接找到在飞的请求 `CancelFromPeer(id, reason)`，并且把 `reason` 包成一个 `peerCancelledError`——它 `Unwrap()` 成 `context.Canceled`，所以业务里 `errors.Is(err, context.Canceled)` 照样成立。**这是 SDK 里最漂亮的一处细节**。

### 50.3.4 源码精读 ③：并发模型（Bug 2 的根因）

`internal/jsonrpc2/conn.go` 的调度可以一句话概括：**一个读循环 + 一个 handler 队列 + 一个（默认）串行执行的 worker**。

```go
// acceptRequest 摘录：没被 preempter 抢走的消息，进无界队列
s.handlerQueue = append(s.handlerQueue, req)
if !s.handlerRunning {
	s.handlerRunning = true
	go c.handleAsync()   // 空队列即退出，不常驻
}

// handleAsync 摘录：一次一个，除非 handler 主动「异步化」
for {
	// 从队列取一个 req……
	go func() {
		defer releaser.release(true)
		result, err := c.handler.Handle(ctx, req.Request)
		c.processResult(c.handler, req, result, err)
	}()
	<-releaser.ch          // ← 等它「放手」才处理下一个
}
```

**三条结论，直接决定你的代码怎么写**：

1. **同一个 `Connection` 内，请求是串行的**（读循环并发、执行串行）。所以一个 8 秒的工具会堵住同一 session 的所有后续请求。真要并发，**必须在你自己的 handler 里 `go` 出去**，或者用 MRTR / 异步响应机制先「放手」再回填。
2. **队列是无界的**，注释也承认了原因：必须读完线上所有消息才能读到响应，无法限长。**背压不在 SDK 里，在 HTTP 的 `MaxRequestBodyBytes` 和你的限流里**（第 32 章）。
3. `Preempter` 抢跑是**唯一**的乱序通道——取消、部分通知走这里。它同时警告：`Preempt must not block`。

> ⚠️ 别把这套「串行」当成缺陷：MCP 的定位是「一个本地/远程能力进程」，绝大多数工具是毫秒级。串行换来的是**实现简单 + 响应顺序可控**。你把它当 Tomcat 用，才是选错了工具。

### 50.3.5 源码精读 ④：会话、能力协商与不回头的协议演进

会话状态就一个结构体（`mcp/session.go`）：

```go
type ServerSessionState struct {
	InitializeParams          *InitializeParams
	InitializedParams         *InitializedParams
	NegotiatedProtocolVersion string   // initialize 谈定的哪个版本
	LogLevel                  LoggingLevel
}
```

能力协商的**代码量比你以为的少得多**，因为 SDK 用「**配置推断**」代替了「显式声明」：`ServerOptions.Capabilities` 为 nil 时默认只有 `{"logging":{}}`；你 `AddTool` 了，`tools` 能力（含 `listChanged:true`）就自动出现在 initialize 响应里；你设了 `SubscribeHandler`，`resources.subscribe` 自动出现。**显式设置任意字段就覆盖推断值**——想关掉 `listChanged`，写 `&ToolCapabilities{}`。

协议版本这块，SDK 是**按版本号字符串做行为分支**（`mcp/shared.go`）：

| 版本 | SDK 中的地位 | 行为变化（你会踩到的） |
| --- | --- | --- |
| `2026-07-28` | 当前最新（`latestProtocolVersion`） | `initialize` 进入废弃路径，改走 `server/discover`；`roots` / `sampling` / `logging` 均标记废弃（SEP-2577，仍可用至少 12 个月）；keepalive ping 仅对旧版本可用；支持 `PropagateRequestCancellation` |
| `2025-11-25` | 仍支持 | streamable HTTP 基线；`form` 形式的 elicitation |
| `2025-06-18` / `2025-03-26` | 仍支持 | 老 streamable / 早期 SSE 时代 |
| `2024-11-05` | 仍支持 | 最初的 stdio + SSE 双传输版本 |

`SupportedProtocolVersions()` 返回一份拷贝给你做客户端/服务端能力对齐；协商规则在 `negotiatedVersion()` / `negotiateMutuallySupportedVersion()`：**优先用客户端的版本，服务端不支持就退到服务端支持的最新版**。

> 这段设计给工程上的启示：**协议要演进，就得把「版本」当成一级输入**，而不是「升级后旧客户端自己想办法」。你自研内部协议时也可以照抄这个模式：一张版本表 + 一个协商函数 + 按版本分支的少数几处代码。

### 50.3.6 源码精读 ⑤：streamable HTTP 的分层与断点续传

`mcp/streamable_server.go` 顶部那段注释是全仓库最有价值的文档，作者自己画了四层：

```text
StreamableHTTPHandler   ← http.Handler，管「会话表」与请求路由
        │
StreamableServerTransport  ← 每会话一个，暴露 ServeHTTP
        │
streamableServerConn    ← Connection 实现，负责「消息送往哪条流」
        │
stream            ← 逻辑消息通道，可被 Last-Event-ID 恢复
```

两个「流」的概念必须分清：

- **独立 SSE 流（id=""）**：客户端 `GET` 建立，承载**服务端主动发起**的请求/通知（采样、进度、日志、list_changed）。每会话最多一条。
- **请求流（id=随机）**：每个含 JSON-RPC 调用的 `POST` 建一条，响应**路由回原 HTTP 请求**。中途断了，可用 `GET + Last-Event-ID` **恢复**。

恢复依赖 `EventStore` 四个方法：`Open` / `Append` / `After(index)` / `SessionClosed`；event id 格式是 `"<streamID>_<index>"`（`formatEventID` / `parseEventID`）。**没有 EventStore 就没有断点续传**——这是最容易漏配的一环。

`StreamableHTTPOptions` 里几个必须知道的开关：

| 字段 | 作用 | 生产建议 |
| --- | --- | --- |
| `Stateless` | 不读不写 `Mcp-Session-Id`，每请求一个临时会话；GET/DELETE 返回 405 | 纯工具型 server（无服务端主动请求）首选；对齐 spec 的 sessionless 方向（SEP-2567） |
| `JSONResponse` | 响应 `application/json` 而非 `text/event-stream` | 不想让反代/网关折腾 SSE 时用；代价是丢掉流式与中途消息 |
| `EventStore` | 断点续传 | 有长任务/大结果就配（Redis 实现很自然，第 45 章） |
| `SessionTimeout` | 空闲会话回收 | **不配就永不回收**，内存泄漏在等你 |
| `MaxRequestBodyBytes` | 限制请求体 | 一定配，别信内网 |
| `DisableLocalhostProtection` | 关掉 DNS rebinding 防护 | **别关**（Bug 3） |

### 50.3.7 源码精读 ⑥：泛型约定的 dispatch —— `ToolHandlerFor`

`mcp/tool.go` 展示了 SDK 处理「类型化工具」的全部套路：

```go
type ToolHandler func(context.Context, *CallToolRequest) (*CallToolResult, error) // 低层：裸 JSON
type ToolHandlerFor[In, Out any] func(context.Context, *CallToolRequest, In) (*CallToolResult, Out, error) // 高层：类型化

func AddTool[In, Out any](s *Server, t *Tool, h ToolHandlerFor[In, Out])
```

`ToolHandlerFor` 的文档把「它替你做了什么事」写成了一串 bullet：默认 input schema（从 `In` 反射生成）、自动 unmarshal、**自动按 schema 校验（非法输入在进 handler 之前就被拒）**、默认 output schema、把 `Out` 塞进 `structuredContent`、把业务错误打包成 `CallToolResult.IsError` 而不是协议错误。

**判断**：这是 Go 泛型在真实框架里最值钱的用法——**把一个「弱类型的协议边界」变成「强类型的函数签名」**，同时保留一个裸 JSON 的逃生口（`ToolHandler`）。你自己的框架照抄这个套路：**类型化入口 + 裸入口双 API，前者 90% 场景，后者给动态工具/代码生成工具**（第 37 章）。

---

## 50.4 Java 对照：只讲会猜错的地方

| 你会猜的 | Go SDK 的真相 | 为什么 |
| --- | --- | --- |
| 「transport 应该像 gRPC 那样有 Channel、Interceptor、LoadBalancer」 | `Transport` 只有一个 `Connect`，连拦截器都在**方法层**做（`Middleware`） | MCP 的连接极短极简单；横切关注点（日志/鉴权/限流）在「方法调用」层做更通用 |
| 「一个 server 实例可以被多个请求并发调用工具」 | 单连接内 handler **串行**；并发要么多 session，要么 handler 内自己 `go` | 简化 ID 关联与响应顺序；本地工具场景够用 |
| 「能力协商要写一大坨 `if`」 | SDK **从你的配置推断** capability，你只在需要覆盖时显式设置 | 「显式优于隐式」的例外：这里隐式推断能显著减少样板代码 |
| 「异常/错误会像 Java 那样 throw 出来」 | 工具业务失败是 `CallToolResult.IsError=true`，**不是** RPC 错误 | 协议错误（-32602）和工具执行失败是两回事，混起来会误导 LLM |
| 「HTTP 传输就是 REST 一把梭」 | 响应默认是 SSE，POST 是「请求-流」语义 | 一个请求可能产出多条消息（进度、中间结果、服务端反向请求） |
| 「协议版本升级 = 客户端跟着升」 | 一张版本表 + 协商函数 + 少数分支 | 生态里客户端更新速度不可控 |
| 「stdio server 里 `println` 打日志很方便」 | stdout = 协议通道，日志必须写 stderr | 分帧靠换行，一行调试输出就是一次协议违规 |

---

## 50.5 工程落地：四段可直接抄的代码

### ① 一个最小 server（stdio）+ 一个 HTTP server

```go
// stdio：本地子进程模式
server := mcp.NewServer(&mcp.Implementation{Name: "kb", Version: "1.0.0"},
	&mcp.ServerOptions{Instructions: "内部知识库检索"})

type searchArgs struct {
	Query string `json:"query" jsonschema:"检索关键词，必填"`
	TopK  int    `json:"top_k,omitempty" jsonschema:"返回条数，默认 5"`
}

mcp.AddTool(server, &mcp.Tool{Name: "kb_search", Description: "在内部知识库中检索"},
	func(ctx context.Context, req *mcp.CallToolRequest, in searchArgs) (*mcp.CallToolResult, any, error) {
		if in.TopK <= 0 { in.TopK = 5 }
		docs, err := kb.Search(ctx, in.Query, in.TopK)
		if err != nil {
			return nil, nil, err        // → IsError=true，模型能看到错误信息
		}
		return &mcp.CallToolResult{
			Content: []mcp.Content{&mcp.TextContent{Text: render(docs)}},
		}, map[string]any{"hits": len(docs)}, nil  // 第三个返回值进 structuredContent
	})

if err := server.Run(context.Background(), &mcp.StdioTransport{}); err != nil {
	log.Fatal(err)   // 注意：日志只能走 stderr，绝不能用 fmt.Println
}
```

```go
// streamable HTTP：远程/多租户模式
handler := mcp.NewStreamableHTTPHandler(
	func(r *http.Request) *mcp.Server { return serverFor(r) }, // 每请求/每租户取 server
	&mcp.StreamableHTTPOptions{
		Stateless:           true,                 // 纯工具 server 首选
		SessionTimeout:      10 * time.Minute,
		MaxRequestBodyBytes: 4 << 20,
		Logger:              slog.Default(),
		// EventStore: redisEventStore{},           // 需要断点续传时再开
	})
mux := http.NewServeMux()
mux.Handle("/mcp", handler)   // 别忘了前面挂鉴权中间件（第 47 章）
```

### ② 一个 client：列举 + 调用 + 超时 + 进度

```go
client := mcp.NewClient(&mcp.Implementation{Name: "agent", Version: "0.1.0"},
	&mcp.ClientOptions{
		// 服务端上报进度时回调（工具里 ss.NotifyProgress 触发）
		ProgressNotificationHandler: func(ctx context.Context, req *mcp.ProgressNotificationClientRequest) {
			slog.Info("progress", "token", req.Params.ProgressToken, "msg", req.Params.Message)
		},
	})

sess, err := client.Connect(ctx, &mcp.CommandTransport{
	Command: exec.Command("kb-mcp", "--dir", "/data"),
}, nil)                                   // ClientSessionOptions{ProtocolVersion: "2025-11-25"}
if err != nil { return err }
defer sess.Close()

// Go 1.23 迭代器：自动翻页，不用自己管 cursor
for tool, err := range sess.Tools(ctx, nil) {
	if err != nil { return err }
	slog.Info("tool", "name", tool.Name)
}

// 每次工具调用都自己带超时——SDK 不会替你兜底
callCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
defer cancel()
res, err := sess.CallTool(callCtx, &mcp.CallToolParams{
	Name:      "kb_search",
	Arguments: map[string]any{"query": "报销流程", "top_k": 3},
})
if err != nil { return fmt.Errorf("call tool: %w", err) }  // 传输/协议错误
if res.IsError { /* 工具业务失败，把文本回喂给 LLM */ }
```

> 💡 `ctx` 取消时 SDK 会尽力发 `notifications/cancelled` 给对端（默认异步、不阻塞返回路径，上限 5s）。**所以别指望「取消 = 对端立刻停」**，对端可能已经干完了一半。

### ③ 服务端上报进度 + 日志

```go
mcp.AddTool(server, &mcp.Tool{Name: "reindex", Description: "重建索引（慢）"},
	func(ctx context.Context, req *mcp.CallToolRequest, in reindexArgs) (*mcp.CallToolResult, any, error) {
		token := req.Params.GetProgressToken()      // 客户端给的进度令牌，可能为 nil

		ss := req.Session        // 当前会话（*mcp.ServerSession；字段名/类型随版本演进）
		for i, file := range files {
			if err := ctx.Err(); err != nil { return nil, nil, err }  // 尊重取消
			index(file)
			if token != nil {
				// 通知类消息：失败只记日志，绝不要因此中断业务
				_ = ss.NotifyProgress(ctx, &mcp.ProgressNotificationParams{
					ProgressToken: token, Progress: float64(i + 1), Total: float64(len(files)),
				})
			}
		}
		return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: "done"}}}, nil, nil
	})
```

### ④ 自定义 transport：只要 5 个方法

改自 `examples/server/custom-transport`（官方示例，可直接跑）：

```go
type frameTransport struct{ r *bufio.Reader; w io.Writer }

func (t *frameTransport) Connect(ctx context.Context) (mcp.Connection, error) {
	return &frameConn{r: t.r, w: t.w}, nil
}

type frameConn struct{ r *bufio.Reader; w io.Writer }

// ① 读一帧：换行分帧 → 交给 jsonrpc 解码
func (c *frameConn) Read(context.Context) (jsonrpc.Message, error) {
	data, err := c.r.ReadBytes('\n')
	if err != nil { return nil, err }
	return jsonrpc.DecodeMessage(data[:len(data)-1])
}

// ② 写一帧：编码 + 换行。Write 可能被并发调用 → 加锁或保证原子写
func (c *frameConn) Write(_ context.Context, msg jsonrpc.Message) error {
	data, err := jsonrpc.EncodeMessage(msg)
	if err != nil { return err }
	_, err1 := c.w.Write(data)
	_, err2 := c.w.Write([]byte{'\n'})
	return errors.Join(err1, err2)
}

func (c *frameConn) Close() error      { return nil }  // Close 要能打断 Read！
func (c *frameConn) SessionID() string { return "" }

// 用：server.Run(ctx, &frameTransport{r: bufio.NewReader(conn), w: conn})
// 想换成「长度前缀」「msgpack」「Kafka topic」都只改这两个方法。
```

> 真实场景里自定义 transport 的用途：把 MCP 塞进已有的 WebSocket/QUIC 通道、走 Kafka 做跨机房、或者给测试做一个可注入延迟/丢包的 fake。**只要 Read/Write/Close 三条契约守住，上面五层全部照常工作**——这就是薄抽象的红利。

### 一处值得一提的扩展点：自定义方法

SDK 留了 `AddReceivingCustomMethod(server, "x/foo", handler)` / `AddSendingCustomMethod(client, "x/foo")` / `CallCustomMethod`。**这是给「MCP 规范还没标准化、但你和伙伴想先跑」的场景留的后门**。用它的时候加一句版本约束注释，别让它变成事实标准。

---

## 50.6 踩坑清单（CR 可直接抄）

| # | 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- | --- |
| 1 | 客户端解析报 `invalid character` | stdio server 用 `fmt.Println` 打日志污染协议流 | 日志一律走 stderr / `slog` → stderr | 50.1、50.5① |
| 2 | 同一 session 第二个请求一直不来 | 单连接 handler 串行，慢工具堵队列 | 工具内并发/异步化；或多 session、多副本 | 50.3.4 |
| 3 | 长任务刷新页面后结果没了 | 没配 `EventStore`，流无法 `Last-Event-ID` 恢复 | 实现 `Open/Append/After/SessionClosed`（Redis） | 50.3.6 |
| 4 | 内存随时间线性上涨 | `SessionTimeout` 为 0 = 空闲会话永不回收 | 设 5～15 分钟；再用 `KeepAlive` 兜死连接 | 50.3.6 |
| 5 | 工具参数非法却进了 handler | 用了低层 `ToolHandler` 而非 `ToolHandlerFor` | 优先泛型 `AddTool`，或自己调 `applySchema` | 50.3.7 |
| 6 | LLM 把工具错误当成「系统挂了」乱重试 | 业务失败返回了 RPC error | 返回 `(nil, nil, err)` 让 SDK 置 `IsError` | 50.4 |
| 7 | 本地 server 被网页打穿 | 关了 localhost 防护 / 没校验 Origin | 保留 DNS rebinding 防护 + 鉴权中间件 | 50.1、50.3.6 |
| 8 | 自定义 transport 偶发卡死 | `Close` 没能打断阻塞中的 `Read` | `Close` 关底层连接；Read 返回错误 | 50.3.2 |
| 9 | 升级 SDK 后行为变了 | 协议版本相关行为按版本分支（如 keepalive 仅旧版可用） | 锁 SDK 版本；用 `SupportedProtocolVersions()` 对齐 | 50.3.5 |
| 10 | 一个 Transport 复用到多个会话 | 接口注释明确「at most one call」 | 每会话新建 Transport | 50.3.2 |

---

## 50.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 五层 | 传输 → 分帧 → JSON-RPC → 会话 → 应用方法 |
| Transport/分帧 | `Connect(ctx) (Connection, error)`，一 Transport 一会话；stdio=换行 JSON（16MB 上限），streamable=POST+SSE |
| `Connection` | `Read`/`Write`/`Close`/`SessionID`，三条并发契约必须守 |
| dispatch | 方法表（`methodInfo`）驱动；`ToolHandlerFor[In,Out]` 把协议边界变成函数签名 |
| 并发 | 读循环并发、**handler 串行**；`Preempter` 是唯一乱序通道（取消） |
| 会话/能力 | `Mcp-Session-Id`；状态=`ServerSessionState`；能力从配置**推断**，显式设置才覆盖 |
| 版本 | 支持 2024-11-05 → 2026-07-28；`initialize` 在新版走向 `server/discover` |
| 扩展 | 自定义 transport（5 个方法）· 自定义方法 · 中间件（`Middleware`） |
| 安全 | 保留 DNS rebinding 防护 · 限请求体 · 鉴权中间件 · 别污染 stdout |
| 大坑 | 慢工具堵队列、忘 EventStore、忘 SessionTimeout、业务错误当协议错误 |

---

## 50.8 自检题

1. **你的 MCP server 提供两个工具：`fast_lookup`（50ms）和 `rebuild_index`（3 分钟）。压测发现 `fast_lookup` 的 P99 飙到 3 分钟。解释原因，给出两种修法并说明各自代价。**
   > 要点：同一连接内 handler 串行（`handleAsync` 一次一个），`rebuild_index` 占住队列。修法：① 慢工具内部起 goroutine 并把结果通过通知/MRTR 回填（改动集中在工具，但要处理「异步后谁负责响应」）；② 拆成独立 session/副本/服务（隔离彻底，多一层部署与路由成本）。加一句：真正的答案往往是「慢工具根本不该做成同步 tool call」。

2. **为什么 `Transport` 只有一个方法，而 gRPC 的 `ClientConn`/`Stream` 有那么多状态？这说明了 MCP 的什么定位？**
   > 要点：MCP 的连接是「一对进程/一对会话的薄管道」，没有负载均衡、重试、多路复用等治理需求（这些交给 HTTP 层或旁路）；它把复杂度放在了「语义层」（能力协商、工具 schema、进度/取消）。所以传输实现成本极低，也容易被塞进任意已有通道（WS/Kafka/内存）。

3. **你在工具 handler 里 `return nil, nil, errors.New("库存不足")`。客户端收到的 `CallToolResult` 长什么样？为什么不能让它变成 JSON-RPC error？**
   > 要点：`ToolHandlerFor` 会把错误打包进 `CallToolResult.Content` 并置 `IsError=true`——这是一次**成功的协议调用 + 失败的业务结果**。若变成 RPC error，模型只能看到「系统错误」，无法据此调整参数重试；协议错误与业务错误的区分是 MCP 能驱动 Agent 自我纠错的前提。

---

## 50.9 延伸阅读

- 仓库：`github.com/modelcontextprotocol/go-sdk`（`go get github.com/modelcontextprotocol/go-sdk`）
- 必读源码顺序：`mcp/transport.go`（接口 + `connect`）→ `internal/jsonrpc2/conn.go`（并发与取消）→ `mcp/server.go`（能力推断 + dispatch）→ `mcp/tool.go`（泛型 handler）→ `mcp/streamable_server.go`（顶部设计文档）→ `mcp/session.go`
- 官方可跑示例：`examples/server/hello`、`examples/server/custom-transport`、`examples/server/middleware`、`examples/client/middleware`、`examples/server/distributed`（多副本 + EventStore）
- 规范：`modelcontextprotocol.io/specification`（注意选对版本 tag，2025 与 2026 版差异不小）
- 对照实现：`mark3labs/mcp-go`（社区实现，API 风格与官方不同，适合对照读「同一个协议的两种 Go 表达」）
- 前置：[42. MCP：协议与 Go SDK 实战](42-mcp.md)
- 下一章：[51. 读源码方法论 + 框架设计模式提炼](51-read-source-patterns.md)

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[49. eino 精读](49-eino-source.md)；协议使用篇：[42. MCP](42-mcp.md)
- 相关：第 24 章（ctx 取消穿透）、第 31 章（JSON 与流式解码）、第 32 章（背压与限流）、第 35 章（可观测）、第 37 章（反射生成 schema）、第 39 章（SSE）、第 45 章（Redis 存 EventStore）、第 47 章（鉴权与多租户）
- 下一章：[51. 读源码方法论 + 框架设计模式提炼](51-read-source-patterns.md)
