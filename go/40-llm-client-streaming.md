# 40. LLM 客户端：抽象、流式与韧性

> **状态**：`已完成（初稿）` ｜ **所属**：第七部分 · AI-Agent 应用架构 ｜ **编号**：40 / 54
> **本章定位**：Agent 的所有能力都建立在一个东西上——**一次可靠的 LLM 调用**。这一章不碰 prompt、不碰 RAG，只解决「怎么把 HTTP 请求变成可靠的 token 流」：接口怎么抽象、SSE 怎么解析、429 怎么分类、连接怎么复用、钱怎么算。
> **读法**：40.3 的接口设计决定你后面 6 章的代码长相，40.4 的两段解析代码可以直接抄进项目，40.6 的错误分类表建议打印贴墙。

---

## 40.1 引子：四个 bug，烧掉一个月

你的 Agent 上线第一周，四条告警轮流响：

1. **`bufio.Scanner: token too long`** —— 某天开始，所有带「读取网页」工具的对话 100% 失败。因为工具返回的正文被塞进了一条 SSE 的 `data:`，超了 `bufio.Scanner` 默认的 **64KB** 单行上限。而你的 `Scanner` 从来没设 `Buffer`。
2. **0.3% 的请求首字延迟 40 秒** —— 你自己那层反代开了 gzip，压缩器要攒够数据才吐，`Flush` 白干。**上游没病，中间层有病。**
3. **429 之后所有实例一起重试** —— 你没有抖动（jitter），20 个 Pod 在同一毫秒重试，把限流打成了雪崩，本来 3 秒能恢复的事变成 3 分钟。
4. **账单比预期高 2.4 倍** —— 一半来自重试时把 3 万 token 的 prompt 重发一遍；另一半来自**没利用 prompt 缓存**，因为你在 system prompt 里塞了 `当前时间：2026-...`，每次都变，缓存永远不命中。

这四个 bug 覆盖了 LLM 客户端的全部工程难点：**流式解析、中间层、重试策略、成本控制**。下面按顺序拆。

---

## 40.2 直觉模型：一个 Adapter + 一条流水线

```
   你的 Agent 代码
        │  Chat(ctx, req) / ChatStream(ctx, req)
        ▼
  ┌─────────────────┐   你的接口：不 import 任何厂商包
  │  llm.Model      │
  └────────┬────────┘
           │ 各个适配器实现
   ┌───────┼────────┬──────────┐
   ▼       ▼        ▼          ▼
 OpenAI  Anthropic  Gemini   Ollama(本地)
   │       │        │          │
   └───────┴────┬───┴──────────┘
                ▼
        HTTP POST + 流式 body
                │
                ▼
   ┌──────────────────────────┐
   │ SSE/NDJSON 解析器        │  ← 全章最值得自己写的一段
   │ 分包、多行 data、[DONE]   │
   └────────────┬─────────────┘
                ▼
        chunk → 累积器 → 完整 Message
                │      （content 拼接 + tool_calls 按 index 合并）
                ▼
        usage / 成本 / trace
```

**关键认知：所有厂商的「流式」长得都不一样，但都是「一行一个 JSON」。** 差别只在**行的包装**和**结束标记**：

| 厂商 | 端点 | 鉴权 | 线的格式 | 结束信号 | usage 在哪 |
| --- | --- | --- | --- | --- | --- |
| OpenAI | `POST /v1/chat/completions` | `Authorization: Bearer` | SSE，`data: {...}` | `data: [DONE]` | 需显式开 `stream_options.include_usage` |
| Anthropic | `POST /v1/messages` | `x-api-key` + `anthropic-version` | SSE，**带事件名**（`message_start`/`content_block_delta`/`message_stop`） | `message_stop` 事件（**没有 `[DONE]`**） | `message_start` 和 `message_delta` 里分段给 |
| Gemini | `POST /v1beta/models/{m}:streamGenerateContent?alt=sse` | `x-goog-api-key` | 必须加 `alt=sse` 才是 SSE，否则是一整个 JSON 数组 | 连接关闭 | 每块带 `usageMetadata` |
| Ollama | `POST /api/chat` | 无 | **NDJSON**（每行一个完整 JSON，不是 SSE！） | 一行 `{"done":true}` | 最后一行 |

> 💡 **Ollama 这一行是很多人踩过的坑**：你以为「流式 = SSE」，于是拿 SSE 解析器去解 NDJSON，结果每一行都解析失败（因为 NDJSON 没有 `data: ` 前缀）。**抽象层要处理的是「帧」而不是「SSE」**，这也是 40.3 把 `Stream` 接口定在 `Recv() (*Chunk, error)` 这一层的原因——帧下面是什么协议，由适配器负责。

Java 侧的同构物是 `WebClient.bodyToFlux(String.class)` + `Flux<ServerSentEvent>`，或者 Spring AI 的 `ChatClient.stream()`。差别在于：**Java 有 Reactor 帮你做背压和取消，Go 没有，你得自己决定「流的消费者是谁、谁负责关」**。

---

## 40.3 底层实现 I：`Model` 与 `Stream` 的接口长什么样

先给结论：**流式接口应该是「迭代器 + `io.EOF`」，不是 channel，也不是回调。**

```go
package llm

import "context"

type Role string

const (
	RoleSystem    Role = "system"
	RoleUser      Role = "user"
	RoleAssistant Role = "assistant"
	RoleTool      Role = "tool"
)

type Message struct {
	Role       Role       `json:"role"`
	Content    string     `json:"content"`
	Name       string     `json:"name,omitempty"`
	ToolCalls  []ToolCall `json:"tool_calls,omitempty"`
	ToolCallID string     `json:"tool_call_id,omitempty"`
}

type ToolCall struct {
	ID        string `json:"id"`
	Name      string `json:"name"`
	Arguments string `json:"arguments"` // 注意：**字符串**形式的 JSON，不是对象
}

type Usage struct {
	PromptTokens     int `json:"prompt_tokens"`
	CompletionTokens int `json:"completion_tokens"`
	CachedTokens     int `json:"cached_tokens,omitempty"`
}

type Request struct {
	Model       string
	System      string
	Messages    []Message
	Tools       []ToolSchema
	MaxTokens   int
	Temperature float64
	JSONSchema  *JSONSchema // 非 nil 时要求结构化输出
}

type Chunk struct {
	Delta     string     // 本帧新增的文本（可能为空）
	ToolCalls []ToolCall // 本帧新增的工具调用增量（按 Index 合并）
	Usage     *Usage     // 通常只在最后一帧
	Done      bool
}

// Stream 是"可以一直取到 EOF 的流"。调用方必须 Close，或者把 ctx 传下去让它自己结束。
type Stream interface {
	Recv() (*Chunk, error) // 结束返回 io.EOF；出错返回错误
	Close() error
}

type Model interface {
	Chat(ctx context.Context, req *Request) (*Message, *Usage, error)
	ChatStream(ctx context.Context, req *Request) (Stream, error)
}
```

**为什么不是 `chan *Chunk`？** 三个理由，都是线上血泪：

1. **谁负责 close**。生产者的 goroutine 出错时忘了 close，消费方永远阻塞 → goroutine 泄漏（第 25 章四大泄漏之一）。
2. **背压语义丢失**。channel 有缓冲就成了「生产者狂奔、内存暴涨」；无缓冲又要求生产者和消费者严格配对，中间隔一层就很难改。
3. **错误没地方放**。你得再开一个 `errCh` 或者塞一个 `Chunk{Err: err}` 的假 chunk，很难看。`Recv()` 直接返回 error 是最干净的。

**为什么不是回调 `func(chunk) error`？** 回调能表达背压（返回 error 就中断），也常见（langchaingo 就这么干），但它把「控制的主动权」交给了生产者：你不能「先处理一半，去做别的事，再回来继续读」。**Agent 场景恰恰需要这个**——一边流给用户，一边判断「要不要中途触发工具」。

> 💡 Go 1.23 之后还有一个更漂亮的选项：`iter.Seq2[*Chunk, error]`（range-over-func）。`for c, err := range stream.All() { ... }` 写起来和 `Recv()` 一样直白，但可以被 `slices.Collect`、`maps` 之外的标准库工具消费。代价是 `os.File` 那种「必须显式 Close」的资源用迭代器容易漏关。**生产代码我建议还是 `Recv()+Close()`，语义最不会错。**

---

## 40.4 底层实现 II：自己写 SSE 解析器（60 行，够用一辈子）

### 40.4.1 先看 `bufio.Scanner` 的 64KB 坑

```go
sc := bufio.NewScanner(resp.Body)          // 默认 MaxScanTokenSize = 64*1024
for sc.Scan() { ... }
// 某天：bufio.Scanner: token too long
```

`Scanner` 内部有一个 `maxTokenSize`（默认 `bufio.MaxScanTokenSize = 64*1024`），超过就返回 `bufio.ErrTooLong` 并**停止扫描**。LLM 的流式 chunk 通常几百字节，看起来安全，但：

- 有的网关会把多帧合并成一大帧再发；
- 工具调用参数（比如整段 HTML）可能一次性塞进 `tool_calls.arguments` 的 delta；
- Anthropic 的 `content_block_delta` 里可能有很长的 base64 图片。

修法一行：`sc.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)`。**但更好的修法是不用 Scanner**——因为 Scanner 会「按行」给你，而 SSE 是「按事件（空行分隔）」，`data:` 多行还得自己拼。所以干脆手写，顺便把多行、注释、CRLF 全处理掉。

### 40.4.2 一个正确的 SSE Reader

```go
package sse

import (
	"bufio"
	"bytes"
	"io"
)

type Event struct {
	Name string // event: 字段（OpenAI 不用，Anthropic 用）
	ID   string // id: 字段（断点续传用，见第 39 章）
	Data []byte // 多条 data: 用 "\n" 拼接后的结果
}

type Reader struct{ br *bufio.Reader }

func NewReader(r io.Reader) *Reader {
	// 只是"起始大小"，ReadBytes 会按需扩容，没有硬上限（和 Scanner 相反）
	return &Reader{br: bufio.NewReaderSize(r, 64<<10)}
}

// Next 返回下一个事件；流正常结束返回 io.EOF。
func (r *Reader) Next() (Event, error) {
	var ev Event
	var data [][]byte
	for {
		line, err := r.br.ReadBytes('\n')
		if len(line) > 0 {
			line = bytes.TrimRight(line, "\r\n") // 兼容 CRLF 的服务端
			switch {
			case len(line) == 0: // 空行 = 派发一个事件
				if len(data) > 0 {
					ev.Data = bytes.Join(data, []byte("\n"))
					return ev, nil
				}
			case line[0] == ':': // 注释行：心跳就长这样，忽略
			default:
				field, value, found := bytes.Cut(line, []byte(":"))
				if !found {
					value = nil // 规范：没有冒号 = 字段名为 value 为空
				} else if len(value) > 0 && value[0] == ' ' {
					value = value[1:] // 规范：冒号后**只**吃掉一个空格
				}
				switch string(field) {
				case "event":
					ev.Name = string(value)
				case "data":
					data = append(data, value)
				case "id":
					ev.ID = string(value)
				case "retry":
					// 重连间隔我们用自己的指数退避管，这里丢弃
				}
			}
		}
		if err != nil {
			if len(data) > 0 { // 结尾没有空行但数据完整：别丢最后一个事件
				ev.Data = bytes.Join(data, []byte("\n"))
				return ev, nil
			}
			if err == io.EOF {
				return ev, io.EOF
			}
			return ev, err
		}
	}
}
```

这段代码只有三个「考点」，但每个考点都对应一个线上 bug：

| 行 | 为什么这么写 | 不这么写会怎样 |
| --- | --- | --- |
| `TrimRight(line, "\r\n")` | 有的反代/服务端发 CRLF | `data` 后面多个 `\r`，JSON 解析时报 `invalid character` |
| `value[0] == ' '` 只吃一个空格 | 规范定义 | 你把用户的前导空格全吃了，模型输出「缩进」全没了 |
| 空行才派发 + 多条 data 用 `\n` join | 规范定义 | 一个 JSON 被拆成两条 `data:` 时你当成两个坏事件 |

### 40.4.3 把它接到一次真实的流式调用上

```go
// streamChat：一次完整的"请求 → 解析 → 累积"闭环。
// onDelta 每收到一段文本就调用一次（这里就是你写 SSE 给前端的地方，见第 39 章）。
func (c *openAIClient) ChatStream(ctx context.Context, req *llm.Request) (llm.Stream, error) {
	body, err := json.Marshal(map[string]any{
		"model":    req.Model,
		"messages": toOpenAIMessages(req),
		"stream":   true,
		// 关键：不开这个，流式响应里根本没有 usage，你就无法算钱
		"stream_options": map[string]any{"include_usage": true},
	})
	if err != nil {
		return nil, fmt.Errorf("marshal: %w", err)
	}

	// 每次尝试都要有独立的、可取消的 ctx（见 40.7）
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/chat/completions", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Authorization", "Bearer "+c.apiKey)
	httpReq.Header.Set("Accept", "text/event-stream")

	resp, err := c.hc.Do(httpReq)
	if err != nil {
		return nil, fmt.Errorf("do: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		defer resp.Body.Close()
		return nil, parseAPIError(resp) // 把 429/5xx 变成可分类的 *APIError
	}
	return &httpStream{r: sse.NewReader(resp.Body), body: resp.Body}, nil
}

// httpStream 把"SSE 事件"变成"业务 chunk"
type httpStream struct {
	r    *sse.Reader
	body io.ReadCloser
	// 流式 tool_calls 是增量拼接的：同一个 index 的 id/name/arguments 要累加
	toolAcc map[int]*llm.ToolCall
	usage   *llm.Usage
}

func (s *httpStream) Recv() (*llm.Chunk, error) {
	for {
		ev, err := s.r.Next()
		if err != nil {
			return nil, err // io.EOF 原样透传：调用方用 errors.Is(err, io.EOF) 判断正常结束
		}
		if string(ev.Data) == "[DONE]" { // OpenAI 的结束哨兵，不是 JSON！
			s.body.Close()
			return &llm.Chunk{Done: true, Usage: s.usage}, io.EOF
		}
		var raw struct {
			Choices []struct {
				Delta struct {
					Content   string `json:"content"`
					ToolCalls []struct {
						Index    int    `json:"index"`
						ID       string `json:"id"`
						Function struct {
							Name      string `json:"name"`
							Arguments string `json:"arguments"`
						} `json:"function"`
					} `json:"tool_calls"`
				} `json:"delta"`
				FinishReason *string `json:"finish_reason"`
			} `json:"choices"`
			Usage *struct {
				PromptTokens        int `json:"prompt_tokens"`
				CompletionTokens    int `json:"completion_tokens"`
				PromptTokensDetails struct {
					CachedTokens int `json:"cached_tokens"`
				} `json:"prompt_tokens_details"`
			} `json:"usage"`
		}
		if err := json.Unmarshal(ev.Data, &raw); err != nil {
			return nil, fmt.Errorf("unmarshal chunk %q: %w", truncate(ev.Data, 200), err)
		}
		if raw.Usage != nil {
			s.usage = &llm.Usage{
				PromptTokens:     raw.Usage.PromptTokens,
				CompletionTokens: raw.Usage.CompletionTokens,
				CachedTokens:     raw.Usage.PromptTokensDetails.CachedTokens,
			}
		}
		ch := &llm.Chunk{Usage: s.usage} // 只有最后一个含 usage 的帧会带上它
		if len(raw.Choices) == 0 {
			continue // include_usage 的最后一帧：只有 usage，没有 choices
		}
		ch.Delta = raw.Choices[0].Delta.Content
		for _, tc := range raw.Choices[0].Delta.ToolCalls {
			acc := s.toolAcc[tc.Index]
			if acc == nil {
				acc = &llm.ToolCall{}
				s.toolAcc[tc.Index] = acc
			}
			acc.ID += tc.ID         // 首帧给 id，后续帧为空 → 累加不会错
			acc.Name += tc.Function.Name
			acc.Arguments += tc.Function.Arguments // 字符串拼接，**不要**中途 unmarshal
			ch.ToolCalls = append(ch.ToolCalls, *acc)
		}
		if ch.Delta != "" || len(ch.ToolCalls) > 0 || ch.Usage != nil {
			return ch, nil
		}
		// 空帧（心跳/role 帧）继续读，不要往上层抛
	}
}

func (s *httpStream) Close() error { return s.body.Close() }
```

> ⚠️ **注意 `tool_calls.arguments` 是「字符串」而不是对象**——它是增量拼接出来的 JSON，**必须等 `finish_reason == "tool_calls"` 之后才能 `json.Unmarshal`**。中途解一定失败，这是 function calling 流式最容易错的地方。

> 🩸 另一个隐蔽事故：`httpStream` 忘了 `Close()`。流式响应读到 `[DONE]` 就返回 `io.EOF`，调用方一 `break` 走了，**`resp.Body` 没关 → TCP 连接不复用 → 每次请求都新建连接 → 端口耗尽**。记住第 38 章那条铁律：`Body` 必须关；如果没读完就想复用连接，得 `io.Copy(io.Discard, resp.Body)` 再关。

---

## 40.5 底层实现 III：结构化输出与 function calling 的线格式

这两件事经常被混为一谈，其实机制完全不同：

**① 结构化输出（Structured Outputs）= 约束解码。** 你把 JSON Schema 交给服务端，服务端用「受约束的解码」保证输出**一定符合 schema**：

```json
{
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "ticket",
      "strict": true,
      "schema": {
        "type": "object",
        "properties": {
          "title":  {"type": "string"},
          "level":  {"type": "string", "enum": ["P0", "P1", "P2"]}
        },
        "required": ["title", "level"],
        "additionalProperties": false
      }
    }
  }
}
```

`strict: true` 有三个硬性要求，**schema 生成器必须满足**否则 400：所有对象都要 `additionalProperties: false`、所有属性都要进 `required`（想表达「可选」就用 `["string","null"]` 的联合类型）、不支持 `$ref` 之外的复杂引用。这就是第 37 章里 `jsonschema.Reflector{DoNotReference: true, AllowAdditionalProperties: false}` 的由来。

> ⚠️ 受约束解码**只能保证语法合规，不能保证语义正确**。`"level": "P0"` 完全合法但可能是错的。业务校验必须自己再做一遍（第 41 章）。

**② Function calling = 模型输出结构化意图，你负责执行。** 请求里给工具清单，模型返回 `tool_calls`：

```json
{"choices":[{"message":{"role":"assistant","content":null,
  "tool_calls":[{"id":"call_abc","type":"function",
    "function":{"name":"get_weather","arguments":"{\"city\":\"上海\"}"}}]},
  "finish_reason":"tool_calls"}]}
```

三件事必须记住：

1. `arguments` 是**字符串**，要二次 `json.Unmarshal`。
2. `content` 可能是 `null`，而 `finish_reason` 是 `"tool_calls"` ——**判断要不要执行工具请看 `tool_calls` 是否非空，不要看 content**。
3. 你可以**一次返回多个** `tool_calls`（并行工具调用），每个 `id` 都必须有一条对应的 `role: "tool"` 消息回填，否则下一轮 400。

---

## 40.6 韧性：错误分类学、退避与抖动

这是 LLM 客户端和普通 HTTP 客户端最不一样的地方：**普通 API 的 4xx 就是你的锅，LLM 的 429 是常态**（你的 RPM/TPM 配额是硬上限，流量天然突发）。

| 错误 | 典型场景 | 重试？ | 正确处理 |
| --- | --- | --- | --- |
| 网络错误 / DNS / 连接重置 | 跨区抖动 | ✅ | 指数退避 + 抖动 |
| 429 `rate_limit_exceeded` | 超 RPM/TPM | ✅ | **尊重 `Retry-After`**，否则退避 |
| 429 `insufficient_quota` | 余额/配额耗尽 | ❌ | 告警 + 切供应商/降级模型 |
| 500 / 502 / 503 / 529 | 上游过载 | ✅ | 有界重试 + 抖动 |
| 400 `context_length_exceeded` | prompt 太长 | ❌ | 裁剪消息/摘要（第 45 章） |
| 400 `invalid_request_error` | schema/参数写错 | ❌ | 立刻修，重试只是烧钱 |
| 401 / 403 | key 过期、地域限制 | ❌ | 刷新凭据 + 告警 |
| `context.Canceled` | 用户点了停止 | ❌ | 直接上抛，**不要**当错误上报 |
| `context.DeadlineExceeded` | 自己设的超时 | ❌ | 记录 TTFT，用于容量分析 |

```go
// APIError 让"能不能重试"变成可编程的事（第 7 章的错误分类学）
type APIError struct {
	StatusCode int
	Code       string        // 如 "rate_limit_exceeded" / "insufficient_quota"
	Message    string
	RetryAfter time.Duration // 来自 Retry-After 头（秒），或 x-ratelimit-reset-*
}

func (e *APIError) Error() string {
	return fmt.Sprintf("llm api %d %s: %s", e.StatusCode, e.Code, e.Message)
}

// shouldRetry 返回 (建议等待, 是否重试)。
// wroteFirstToken=true 表示已经给用户吐过字了 —— 此时重试会造成"口吃"（前半段说两遍）。
func shouldRetry(err error, wroteFirstToken bool) (time.Duration, bool) {
	if wroteFirstToken {
		return 0, false
	}
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return 0, false
	}
	var apiErr *APIError
	if errors.As(err, &apiErr) {
		switch {
		case apiErr.StatusCode == 429 && apiErr.Code != "insufficient_quota":
			return apiErr.RetryAfter, true
		case apiErr.StatusCode >= 500:
			return 0, true
		default:
			return 0, false
		}
	}
	var ne net.Error
	if errors.As(err, &ne) {
		return 0, true // 网络层错误
	}
	return 0, false
}

// backoff：指数退避 + equal jitter。
// 没有抖动会发生什么：20 个 Pod 在同一毫秒集体重试，把限流打成雪崩。
func backoff(attempt int, base, max time.Duration) time.Duration {
	d := base << attempt // 500ms, 1s, 2s, 4s ...
	if d <= 0 || d > max {
		d = max
	}
	half := d / 2
	return half + time.Duration(rand.Int64N(int64(half)+1)) // math/rand/v2，Go 1.22+
}

// 调用侧：所有重试共用同一个总预算 ctx，不能"每次重试重新计时"
func (c *client) chatWithRetry(ctx context.Context, req *llm.Request, emit func(string) error) (*llm.Message, error) {
	const maxAttempts = 4
	var wrote bool
	var lastErr error
	for attempt := 0; attempt < maxAttempts; attempt++ {
		if attempt > 0 {
			wait, ok := shouldRetry(lastErr, wrote)
			if !ok {
				return nil, lastErr
			}
			if wait == 0 {
				wait = backoff(attempt-1, 500*time.Millisecond, 8*time.Second)
			}
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(wait): // attempt 有界，这里用 time.After 不会泄漏（第 21 章）
			}
		}
		msg, err := c.doOnce(ctx, req, func(s string) error {
			wrote = true // 一旦吐字，后续就不许再重试了
			return emit(s)
		})
		if err == nil {
			return msg, nil
		}
		lastErr = err
	}
	return nil, fmt.Errorf("after %d attempts: %w", maxAttempts, lastErr)
}
```

三条铁律：

1. **流式请求的重试只能在「首字节之前」**。一旦开始往 SSE 写 token，重试就等于让用户看两遍开头。做不到「无损续接」，就只能「重新生成并明确告诉前端」。
2. **总预算只能有一个 `ctx`**。见过最烂的实现是每次重试 `context.WithTimeout(ctx, 30*time.Second)` 一次，结果一次用户请求跑了 4 分钟——因为每次重试都重置了超时。
3. **抖动是必须的，不是优化**。`rand` 请用 `math/rand/v2`（Go 1.22+，无需 seed，并发安全）。

---

## 40.7 超时、context 与连接复用

### 40.7.1 三层超时，不要混在一起

```
用户请求 ctx（总预算，例如 120s）          ← 由 HTTP handler 的 r.Context() 派生
  └── 每次尝试 ctx（例如 60s，或"到总预算耗尽"）
        ├── 首字超时 TTFT（例如 15s）      ← 用响应头到齐来近似
        └── 单帧间隔超时（例如 30s 没新数据就断）
```

实现上，第一层是 `ctx`，第二层用 `context.WithTimeout(ctx, ...)`，第三层用 **`Transport.ResponseHeaderTimeout`**（只等响应头，不影响 body 流）。**千万别设 `http.Client.Timeout`**——它是「从发起请求到读完 body」的总时长，会把正常的 3 分钟长回答直接砍断。

### 40.7.2 `Transport` 的四个参数，两个是坑

```go
tr := &http.Transport{
	// MaxIdleConnsPerHost 默认是 2！！！（第 38 章的重点）
	// 流式请求会"长期占用"连接，池子太小 = 每次都在 Dial，延迟和 CPU 都上去了
	MaxIdleConns:          200,
	MaxIdleConnsPerHost:   100,
	IdleConnTimeout:       90 * time.Second,
	ResponseHeaderTimeout: 30 * time.Second, // 只等响应头
	ForceAttemptHTTP2:     true,             // 多路复用：一个 TCP 跑几十条流
	MaxConnsPerHost:       0,                // 0 = 不限；受限流配额约束时设成 RPM 允许的并发
}
client := &http.Client{Transport: tr} // 不设 Timeout！
```

两个坑：**① `MaxIdleConnsPerHost` 默认 2**；**② 在同一个 `Transport` 上并发几十条流式请求时，如果对端 HTTP/2 的 `MaxConcurrentStreams` 很小，多出来的请求会被排队**——表现为「延迟莫名其妙」，抓包才发现根本没发出去。

### 40.7.3 并发上限 = 上游配额

你的并发能力不是由 goroutine 决定的，而是由**上游的 TPM/RPM** 决定的。所以生产代码里一定要有信号量：

```go
sem := semaphore.NewWeighted(int64(cfg.MaxConcurrentStreams)) // golang.org/x/sync/semaphore
if err := sem.Acquire(ctx, 1); err != nil { return err }
defer sem.Release(1)
```

`errgroup.SetLimit` 也行，但信号量在「跨多个函数、按权重（大模型占 4 份、小模型占 1 份）」时更灵活。有界并发的完整写法详见第 26 章。

---

## 40.8 token 计量、prompt 缓存与降级

### 40.8.1 算钱这件事，比你想象的更容易错

```
成本 = prompt_tokens × 输入单价 + completion_tokens × 输出单价
     + cached_tokens × 缓存命中单价（通常 0.1x~0.5x）
```

三个实践要点：

1. **必须开 `stream_options.include_usage`**，否则流式响应里拿不到 usage，你只能估算。实测：中文大约 **1 token ≈ 0.7~1.5 个汉字**（视模型 tokenizer），英文 1 token ≈ 4 字符。要精确就引 `pkoukk/tiktoken-go`（社区维护的 tiktoken 移植，**不是** OpenAI 官方包）或直接用厂商的 `count_tokens` 端点（Anthropic 有）。
2. **用户中途停止 = 已经花的钱要不回来**。停止时要按「已生成的 token」计费，所以在 `emit` 回调里累加 `incremental tokens`，别只在最后一次性记账。
3. **重试要单独打点**。否则你会看到「账单是日志里 token 数的 2.4 倍」这种现象（就是 40.1 的第 4 条）。

### 40.8.2 prompt 缓存的唯一秘诀：前缀稳定

各家机制不同，但规则是同一个：**从第一个 token 开始、连续相同的前缀才能命中**。

| 厂商 | 机制 | 命中条件 | 计费 |
| --- | --- | --- | --- |
| OpenAI | 自动前缀缓存 | 前缀 ≥ 1024 token 且完全一致 | 命中部分按折扣价，`prompt_tokens_details.cached_tokens` 可见 |
| Anthropic | 显式 `cache_control: {"type":"ephemeral"}` 打在 content block 上 | 标记点之前的内容完全一致 | 写入 ~1.25x，读取 ~0.1x（具体倍数看官方 pricing） |
| DeepSeek | 自动磁盘缓存 | 前缀一致 | 命中部分大幅折扣 |

所以正确的 prompt 排版永远是：

```
[system prompt（固定）] → [工具定义（固定，按名字排序）] → [长期记忆（少变）] → [历史对话] → [本轮用户输入]
                                                                 ↑ 变化的东西只能在最右边
```

40.1 第 4 条 bug 的根因就是 `当前时间：...` 写在 system 的最前面——**一个字符的变化，整个缓存全废。**

### 40.8.3 降级阶梯

```
① 同供应商换 key / 换区域
② 换供应商（OpenAI → Azure OpenAI → 备用厂商），接口抽象层的价值就在这
③ 降级模型（大 → 小），并在 UI 上标注"简化模式"
④ 关掉流式（降低复杂度，先保证有结果）
⑤ 命中语义缓存 / 固定话术兜底
⑥ 明确拒绝 + 给 ETA（429 时诚实比装死好）
```

再加一层**熔断**：连续 N 次 5xx 就打开，30 秒内直接走降级，而不是每个请求都去撞墙。熔断与限流的完整三件套详见第 47 章。

---

## 40.9 Java 对照：四处会猜错的地方

**① 「应该有响应式流」。** Java 里 `WebClient` + `Flux<String>` 会给你 `onBackpressureBuffer` / `timeout` / `retryWhen` 一整套组合子。Go 里这些全是手写的 `for` 循环 + `select`。**好消息**：你能精确控制「重试只发生在首字节前」这种业务语义，而 Reactor 的 `retryWhen` 包在流中间时，这个问题极难处理。

**② Jackson 的默认值反了。** Java 的 Jackson 遇到未知字段**默认报错**（`FAIL_ON_UNKNOWN_PROPERTIES=true`），所以大家习惯加 `@JsonIgnoreProperties(ignoreUnknown = true)`。Go 的 `encoding/json` **默认忽略未知字段**，想严格必须显式 `dec.DisallowUnknownFields()`。这对 LLM 结构化输出是灾难性的——模型多给你一个字段，你就静默漏读了。**结论：解析模型的 JSON 一律用 `DisallowUnknownFields` + schema 校验，两头都上。**

**③ 没有 `Mono.timeout()` 这种「只等第一个元素」。** Go 的等价物是 `Transport.ResponseHeaderTimeout`（等响应头）+ 在 `emit` 第一次被调用时记录 TTFT。想要「15 秒没首字就换供应商」，得用 `context.WithCancel` + 一个 `time.AfterFunc` 主动 cancel 当前尝试。

**④ 连接池的概念不一样。** Java 的 `HttpClient` / OkHttp 连接池有 `maxIdleConnections`、`keepAliveDuration`，语义和 Go 的 `Transport` 基本同构，**但 Go 的 `MaxIdleConnsPerHost` 默认值是 2**（OkHttp 默认 5、Spring 默认 200/20），这是最经典的踩坑点，详见第 38 章。

---

## 40.10 框架与工程落地

**官方 SDK `github.com/openai/openai-go`**（注意：模块路径随大版本变化，v3 起是 `github.com/openai/openai-go/v3`，import 名仍是 `openai`；v1 时代是裸路径。**以你本地 `go.mod` 为准**）：

```go
client := openai.NewClient( // 默认读环境变量 OPENAI_API_KEY
	option.WithMaxRetries(2), // SDK 自带重试，够用就别自己写
)

stream := client.Chat.Completions.NewStreaming(ctx, openai.ChatCompletionNewParams{
	Model:    openai.ChatModelGPT4oMini,
	Messages: []openai.ChatCompletionMessageParamUnion{openai.UserMessage("用三句话解释 GMP")},
	StreamOptions: openai.ChatCompletionStreamOptionsParam{IncludeUsage: openai.Bool(true)},
})
defer stream.Close()

for stream.Next() {
	chunk := stream.Current()
	if len(chunk.Choices) > 0 {
		fmt.Print(chunk.Choices[0].Delta.Content) // 增量文本
	}
}
if err := stream.Err(); err != nil {
	var apiErr *openai.Error
	if errors.As(err, &apiErr) && apiErr.StatusCode == 429 {
		// 分类处理
	}
}
```

SDK 内部就是一个 `ssestream.Stream[ChatCompletionChunk]`（**所以 `NewStreaming` 返回 `*ssestream.Stream[T]`，`Next()/Current()/Err()/Close()` 与我们 40.3 的设计同构**）。**不要同时开两个流共用一个 `Stream` 对象**，它不是并发安全的。

**社区 SDK `github.com/sashabaranov/go-openai`**：API 更「朴素」，`CreateChatCompletionStream` 返回 `*ChatCompletionStream`，`Recv()` 一直读到 **`io.EOF`**（没有 `[DONE]` 概念，SDK 帮你吃了）。很多存量项目用它，迁移成本不高。

**langchaingo**（`github.com/tmc/langchaingo/llms`）：把「流」变成回调参数 `llms.WithStreamingFunc(func(ctx context.Context, chunk []byte) error)`。它的 `llms.Model` 接口只有两个方法（`Call` / `GenerateContent`），**回到了 Java 那种「统一抽象 + 各种 provider 适配」的思路**，是看书里第 48 章的最好素材。

**选型建议**：要控成本 → 官方 SDK（更新最快、字段最全）；要多供应商/多模态混用 → 自己按 40.3 的接口写薄适配层（100~200 行/厂商），别指望一个第三方包帮你统一所有差异。

---

## 40.11 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| `bufio.Scanner: token too long` | 默认 64KB 单行上限 | `sc.Buffer(buf, 8<<20)`，或手写 `ReadBytes` 解析器 | 40.4.1 |
| JSON 里多个 `\r`、解析报错 | 没处理 CRLF | `bytes.TrimRight(line, "\r\n")` | 40.4.2 |
| 一个 JSON 被拆成两个事件 | 多条 `data:` 没有 `\n` 拼接 | 累积 + 空行派发 | 40.4.2 |
| `tool_calls` 参数解析失败 | `arguments` 是**增量字符串** | 等 `finish_reason=="tool_calls"` 再 unmarshal | 40.4.3 |
| 首字延迟 40s，body 一次性到 | 中间层 gzip / 缓冲 | `Accept-Encoding: identity` + 反代关缓冲（第 39 章） | 39.3.4 |
| 429 后雪崩 | 无抖动，所有实例同步重试 | 指数退避 + equal jitter + 尊重 `Retry-After` | 40.6 |
| 一次请求跑 4 分钟 | 每次重试重置了超时 | 只有一个总预算 `ctx` | 40.6 |
| 长回答被砍在第 60 秒 | `http.Client.Timeout` 或反代 `proxy_read_timeout` | 用 `ctx` + `ResponseHeaderTimeout`，反代调大 | 40.7 |
| 端口耗尽 / 连接不复用 | `resp.Body` 没关（尤其早退时） | `defer resp.Body.Close()`，早退先 `io.Copy(io.Discard, ...)` | 40.4.3 |
| 流式并发一高就"卡住" | `MaxIdleConnsPerHost` 默认 2 | 设成 50~100，并确认 HTTP/2 的 stream 上限 | 40.7.2 |
| 账单是估算的 2 倍 | 没开 `include_usage`；重试未单独打点 | `stream_options.include_usage` + 重试计量 | 40.8.1 |
| 缓存命中率 0 | prompt 前缀每次都变 | 固定 system/工具在前，时间戳等易变内容放最后 | 40.8.2 |
| 模型多给一个字段就漏读 | Go 默认忽略未知字段（和 Jackson 相反） | `DisallowUnknownFields()` + schema 校验 | 40.9 |

## 40.12 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 接口设计 | `Chat` + `ChatStream` 返回 `Stream{Recv()/Close()}`；用 `io.EOF` 收尾，别用 channel |
| 三种线格式 | OpenAI/Anthropic = SSE，Ollama = NDJSON，Gemini 要 `?alt=sse` |
| Scanner 坑 | 默认单行上限 64KB，必须 `sc.Buffer`，或直接手写解析 |
| 解析三考点 | CRLF、冒号后只吃一个空格、多条 `data:` 用 `\n` 拼、空行派发 |
| 结束信号 | OpenAI 是 `data: [DONE]`；Anthropic 是 `message_stop` 事件；NDJSON 是 `done:true` |
| tool_calls | `arguments` 是字符串且**增量拼接**，`content` 可能为 null |
| 重试 | 只在首字节前重试；429 看 `Retry-After`；`insufficient_quota` 不重试 |
| 抖动 | 指数退避 + jitter，`math/rand/v2`，防多实例同步雪崩 |
| 超时 | 一个总预算 `ctx` + `ResponseHeaderTimeout`；**不要** `http.Client.Timeout` |
| 连接 | `MaxIdleConnsPerHost` 默认 2（坑）；流式要开 HTTP/2 + 足够池子 |
| 成本 | 开 `include_usage`；重试单独计量；停止时按已生成计费 |
| 缓存 | 前缀稳定才命中；易变内容永远放最右 |
| JSON | Go 默认忽略未知字段 → 解析模型输出必须显式严格 |

## 40.13 自检题

1. **为什么 `Stream` 接口不用 `chan *Chunk`？说出三个具体会出事的地方。**
   > 答案要点：① 谁 close 的问题，生产者出错忘 close → 消费者永久阻塞 → goroutine 泄漏；② 背压丢失，带缓冲就是内存炸弹，无缓冲又强耦合；③ 错误无处安放，只能塞假 chunk 或另开 errCh。而 `Recv() (*Chunk, error)` 一次解决三件事，还能用 `io.EOF` 表达正常结束。

2. **用户点了「停止生成」，你的 `chatWithRetry` 却在重试——为什么错了？怎么改？**
   > 答案要点：`shouldRetry` 必须先判 `errors.Is(err, context.Canceled)` 并返回 false；更根本的是「已吐字后不许重试」（`wroteFirstToken`）；再往上，停止应该 `cancel()` 那个 ctx，上游 HTTP 请求被取消 → 上游停止生成 → 不再计费（第 39 章的 `r.Context()` 链）。

3. **你的 prompt 缓存命中率为 0，但 system prompt 明明没改过。列出三个最可能的原因。**
   > 答案要点：① system 里塞了时间戳/随机 ID/UUID，前缀不一致；② 工具定义顺序不稳定（Go map 遍历随机，序列化顺序每次不同——第 12 章）；③ 前缀不足最小长度（如 OpenAI 需 ≥1024 token），或者改了中间的历史消息导致标记点之后全部失效。

## 40.14 延伸阅读

- OpenAI API 文档：Streaming、Structured Outputs、Function calling、Prompt caching 四节
- `github.com/openai/openai-go`：`chatcompletion.go` 里的 `NewStreaming`、`packages/ssestream`（一个 200 行的通用 SSE 流实现，值得通读）
- `github.com/sashabaranov/go-openai`：`stream.go`、`stream_reader.go`
- `github.com/tmc/langchaingo/llms`：`llms.go`（`Model` 接口）与 `options.go`（`WithStreamingFunc`）
- WHATWG HTML Standard §Server-sent events（解析规范原文）
- 上一章：[39. 实时通信：SSE / WebSocket / gRPC streaming 怎么选](39-realtime-sse-websocket.md)
- 下一章：[41. 工具系统：Function Calling 全链路](41-tool-system-function-calling.md)

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：第 39 章（SSE 协议与 Flush）、第 38 章（`Transport` 与连接池）、第 7 章（错误分类学）、第 24 章（context）
- 下一章：第 41 章（工具系统，`tool_calls` 的执行侧）
- 相关：第 31 章（JSON 与严格解码）、第 47 章（限流、熔断与成本刹车）
