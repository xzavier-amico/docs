# 41. 工具系统：Function Calling 全链路

> **状态**：`已完成（初稿）` ｜ **所属**：第七部分 · AI-Agent 应用架构 ｜ **编号**：41 / 54
> **本章定位**：第 40 章解决了「模型怎么说」，这一章解决「模型说要做的事，你怎么敢做」。工具系统是 Agent 从「聊天玩具」变成「能干活的系统」的分水岭，也是**唯一一处由概率模型直接指挥你执行代码**的地方——所有的安全、幂等、超时、审计问题都在这里爆发。
> **读法**：41.3/41.4 是骨架（接口 + schema + 校验），41.6 是并发模型，41.9 的沙箱和 41.8 的幂等是上线前必须补齐的两块。Java 对照在 41.11。

---

## 41.1 引子：模型给了你一个 `"path": "/etc/passwd"`

四个真实工单，都发生在「工具已经上线、用户量刚起来」的时候：

1. **目录穿越** —— `read_file` 工具的参数是模型生成的。某天用户问「读一下 `../../../../etc/passwd` 看看」，模型很听话地把它填进了 `path`，你的代码 `os.ReadFile(args.Path)` 直接把系统文件内容返回给了模型，模型再复述给用户。**你不是被黑客攻击了，你是被自己的工具出卖了。**
2. **一轮工具把请求挂死 5 分钟** —— 模型一轮返回 5 个 `tool_calls`，你串行执行，其中 `fetch_url` 撞上一个不响应的站点（TCP 连上但永不返回），默认 30 秒超时 × 5 = 150 秒，加上重试直接超 5 分钟。用户早就走了，goroutine 还在那儿等。
3. **prompt 预算爆炸** —— `web_fetch` 把一个 400KB 的网页正文原封不动塞进 `role: "tool"` 消息，下一轮请求直接 400 `context_length_exceeded`，然后你重试（因为它是 5xx？不，是 400），失败，用户看到「系统错误」。
4. **重复下单** —— Agent 调 `create_order` 成功了，但回填结果时网络抖动导致你的重试逻辑重跑了一遍 → 用户收到两笔订单。**LLM 工具的「重试」和 HTTP 的「重试」不是一回事：前者会重复副作用。**

这四条对应工具系统的四根支柱：**校验、超时、截断、幂等**。下面逐个拆，先从最容易被低估的一块开始——**接口设计**。

---

## 41.2 直觉模型：工具 = 带 Schema 的 RPC，而调用方是个醉汉

```
   用户: "总结 /data/q3 里几份 PDF 的结论"
        │
        ▼
   ┌─────────┐  tools=[list_dir, read_file, ...]  ┌──────────┐
   │   LLM   │ ─────────────────────────────────▶ │   你     │
   └────┬────┘                                     └────┬─────┘
        │ ◀── tool_calls:[{id:"call_1", name:"list_dir",
        │                  arguments:"{\"path\":\"/data/q3\"}"}] ─┘
        ▼
   ┌──────────────────────────────────────────────────────┐
   │ 你：① schema 校验 → ② 业务/权限校验 → ③ 沙箱执行     │
   │      → ④ 结果截断 → ⑤ 审计打点 → ⑥ 生成 tool 消息   │
   └──────────────────────┬───────────────────────────────┘
                          ▼
   messages += [{role:"tool", tool_call_id:"call_1", content:"..."}]
                          │
                          ▼
   ┌─────────┐  再问一次（带完整历史）→ 可能又要 read_file → …… → 没 tool_calls 了
   │   LLM   │
   └─────────┘
```

这个循环里有三个「和普通 RPC 完全不同」的地方，想通了后面全都顺：

1. **调用方是概率性的。** 普通 RPC 的客户端是你写的代码，参数类型错了编译期就报错。这里的客户端是一个语言模型，它会**幻觉出不存在的工具、编造参数、给字符串字段传数字、在日期里写「下周三」**。所以**服务端必须假设所有输入都是恶意的**。
2. **工具描述是 prompt 的一部分，不是文档。** 你写的 `Description()` 会进模型的上下文，直接决定它选不选这个工具。写「查询数据库」和写「按订单号查询订单状态，仅在用户提供订单号后调用」，命中率差 3 倍。
3. **「执行了」和「模型知道了」是两件事。** 回填失败、工具超时、结果被截断，模型都以为「我还没做」→ 会重复调用。**幂等不是优化，是正确性。**

| 你在 Spring 里的直觉 | Agent 工具系统的现实 |
| --- | --- |
| `@RestController` + `@RequestBody` + `@Valid` | 工具 = 名字 + JSON Schema + 一个函数；校验在「模型」和「函数」之间 |
| DTO 嵌套注解做校验 | schema 由 struct 反射生成（第 37 章），校验靠 schema 引擎 |
| Service 层直接注入调用 | 调用方是 LLM，参数可能是幻觉，且**每次都不一样** |
| `@PreAuthorize` + `SecurityContext` | 没有「登录用户」这个上下文，只有「会话所属租户 + 用户授予的权限」 |
| `@Transactional` 保证一致性 | 工具之间没有事务，「执行成功但结果没回填」是常态 → 靠幂等兜 |

---

## 41.3 底层实现 I：`Tool` 接口与 JSON Schema 自动生成

先定接口。**接口要小，且必须包含 schema**——因为 schema 是工具的一部分，不是元数据。

```go
package tool

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"

	"github.com/invopop/jsonschema"
)

// Result 是工具回给模型 + 回给程序的两份数据
type Result struct {
	Content    string          // 给模型看的文本（进上下文，要控长度）
	Structured json.RawMessage `json:"structured,omitempty"` // 给你的程序用的结构化结果（可选）
	Truncated  bool            // 是否被截断（必须告诉模型！）
}

type Tool interface {
	Name() string
	Description() string              // 这行字会进 prompt，决定模型选不选你
	InputSchema() json.RawMessage     // JSON Schema，随 tools 字段发给模型
	Call(ctx context.Context, args json.RawMessage) (Result, error)
}

// FuncTool[In] 把"一个普通函数"变成 Tool：schema 从 In 类型反射生成
type FuncTool[In any] struct {
	name, desc string
	schema     json.RawMessage
	fn         func(ctx context.Context, in In) (Result, error)
}

type ListDirIn struct {
	Path      string `json:"path" jsonschema:"description=目录绝对路径，必须在 /data 下,required"`
	Recursive bool   `json:"recursive,omitempty" jsonschema:"description=是否递归子目录"`
}

func New[In any](name, desc string, fn func(ctx context.Context, in In) (Result, error)) (*FuncTool[In], error) {
	r := &jsonschema.Reflector{
		// 这三个开关是为 OpenAI 的 strict 模式准备的，缺一个就 400
		DoNotReference:             true, // strict 模式不接受 $ref
		AllowAdditionalProperties:  false, // strict 要求 additionalProperties:false
		RequiredFromJSONSchemaTags: true, // 用 tag 里的 required 推导
	}
	var zero In // 零值就够：Reflect 只需要类型信息
	raw, err := json.Marshal(r.Reflect(zero))
	if err != nil {
		return nil, fmt.Errorf("reflect schema of %s: %w", name, err)
	}
	return &FuncTool[In]{name: name, desc: desc, schema: raw, fn: fn}, nil
}

func (t *FuncTool[In]) Name() string          { return t.name }
func (t *FuncTool[In]) Description() string   { return t.desc }
func (t *FuncTool[In]) InputSchema() json.RawMessage { return t.schema }

func (t *FuncTool[In]) Call(ctx context.Context, args json.RawMessage) (Result, error) {
	var in In
	// 第一层校验：类型。多给字段要报错（和 Jackson 的默认行为相反，见第 40 章）
	dec := json.NewDecoder(bytes.NewReader(args))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&in); err != nil {
		return Result{}, fmt.Errorf("invalid args: %w", err)
	}
	return t.fn(ctx, in)
}
```

三个设计判断：

- **泛型 `In` 而不是 `map[string]any`**。`map[string]any` 看起来省事，但你会丢掉「编译期类型安全」这个 Go 唯一擅长的地方，然后在函数里写一堆 `args["path"].(string)` 的类型断言（panic 等你）。泛型版本让工具实现变成 `func(ctx, ListDirIn)`，测试起来和普通函数一样（第 6 章）。
- **schema 在构造时生成一次**。`jsonschema.Reflect` 走反射，别在每次请求里算。
- **不支持 `$ref`**。这是为 OpenAI strict 模式的妥协：schema 里出现 `$ref` 会被拒。代价是嵌套结构会被展开，重复定义会让 schema 变长——**token 是要花钱的**（第 40 章）。

> 💡 `google/jsonschema-go` 是另一个选择（MCP 官方 Go SDK 就用它做 schema 推断与校验，只支持 JSON Schema 2020-12 草案）。选哪个都行，**关键是 schema 只能有一份来源**：同一个 struct 既生成 schema 又做 decode，才不会漂移。

---

## 41.4 底层实现 II：校验分两层，别只做一层

99% 的事故都来自「只做了第一层」：

| 层 | 管什么 | 怎么做 | 漏了会怎样 |
| --- | --- | --- | --- |
| ① 结构层 | 类型、必填、枚举、范围、长度 | struct decode + JSON Schema 校验 | 类型断言 panic、SQL 拼接注入 |
| ② 业务层 | 「这个值在业务上合法吗」 | 白名单、路径约束、SQL 只读、租户归属 | 目录穿越、越权读别人数据、`DROP TABLE` |

通用约束（`required`/`enum`/`minimum`/`maxLength`）用 `github.com/santhosh-tekuri/jsonschema/v6` 编译一次 schema 反复 `Validate` 最省事（v5/v6 的 `AddResource` 签名有差异，以 `go doc` 为准）。但**业务层没人能替你写**：

```go
var ErrForbidden = errors.New("tool: forbidden")

// safePath：把"任意用户给的路径"收敛到 root 之内
func safePath(root, p string) (string, error) {
	if !filepath.IsAbs(root) {
		return "", errors.New("root must be absolute")
	}
	// 反例：strings.HasPrefix(filepath.Clean(p), root) 拦不住 /data-evil（前缀相同但不是一个目录）
	// 正解一：先用 IsLocal 拒掉绝对路径和 ..（Go 1.20+）
	if !filepath.IsLocal(p) {
		return "", ErrForbidden
	}
	full := filepath.Join(root, p)
	// 正解二：用 Rel 再确认一次（防 Clean 的边界情况）
	rel, err := filepath.Rel(root, full)
	if err != nil || strings.HasPrefix(rel, ".."+string(filepath.Separator)) || rel == ".." {
		return "", ErrForbidden
	}
	return full, nil
}

// guardSQL：会写 SQL 的工具，只允许单条 SELECT
func guardSQL(q string) error {
	up := strings.ToUpper(strings.TrimSpace(q))
	if !strings.HasPrefix(up, "SELECT") && !strings.HasPrefix(up, "WITH") {
		return fmt.Errorf("%w: only SELECT allowed", ErrForbidden)
	}
	if strings.Contains(up, ";") {
		return fmt.Errorf("%w: multiple statements", ErrForbidden)
	}
	if strings.Contains(up, " PG_SLEEP") || strings.Contains(up, " INTO OUTFILE") {
		return fmt.Errorf("%w: suspicious token", ErrForbidden)
	}
	return nil
}
```

> ⚠️ 路径白名单有三个升级版，按需要挑：**① **开 `openat2(RESOLVE_BENEATH)`（Linux，最靠谱，能防符号链接逃逸）；**②** 用 `filepath.EvalSymlinks` 后再比对；**③** 干脆不在本进程读文件，交给一个只挂载了 `/data` 的沙箱进程（41.9）。`IsLocal` 只处理字面路径，**符号链接能绕过它**。

还有一个常被忽略的校验：**「工具调用是否被授权」**。模型不知道谁在跟它说话，所以「这个租户能不能用 `admin_export`」必须由你在执行前判：

```go
func (r *Registry) authorize(ctx context.Context, name string) (Tool, error) {
	tenant, role := tenantFrom(ctx)     // 由会话注入，绝不从模型参数里取！
	e, ok := r.get(name)
	if !ok || !e.roles[role] {
		return nil, fmt.Errorf("%w: tool %q not available for role %q", ErrForbidden, name, role)
	}
	return e.t, nil
}
```

**「租户 ID 绝不能作为工具参数出现」**——这条铁律请抄进 CR checklist。一旦 `tenant_id` 变成模型可以填的字段，某次幻觉就能让你跨租户读数据。

---

## 41.5 多轮调用协议：wire 上到底长什么样

这是最常被框架掩盖、但**你必须自己看懂**的一段协议。因为它决定了「并行工具怎么回填」「顺序能不能乱」。

```
 Client(你)                                        LLM
    │  ① messages: [system, user] + tools[...]      │
    ├───────────────────────────────────────────────▶
    │  ② assistant: {content:null,                  │
    │       tool_calls:[{id:"call_A",...},          │
    │                   {id:"call_B",...}]}          │   ← 一轮可以要多个
    ◀───────────────────────────────────────────────┤
    │  ③ 你自己执行 A、B（可以并行）                │
    │  ④ messages += {role:"tool", tool_call_id:"call_A", content:"..."}
    │             += {role:"tool", tool_call_id:"call_B", content:"..."}
    ├───────────────────────────────────────────────▶
    │  ⑤ assistant: {content:"根据 A 和 B……"}        │
    ◀───────────────────────────────────────────────┤
```

三条硬约束（违反了直接 400，且报错信息通常很难懂）：

1. **每个 `tool_call_id` 必须有且只有一条对应的 `role:"tool"` 消息**，在下一轮请求里。少一条 → `400 An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'`。
2. **`tool` 消息的顺序无所谓**（它们靠 id 匹配），但你的代码最好保持稳定顺序（按 `index`），方便 diff 和回归测试。
3. **工具失败了也要回一条 `role:"tool"`**，内容是错误描述。「执行失败」不等于「可以不回」——不回就直接把协议破坏了。

所以并行执行后的回填必须是「**按原始下标对齐**」，而不是「谁先完成谁先写」：

```go
// 错误的写法：results 按完成顺序 append → 顺序随机 → 回归测试永远飘
// 正确的写法：预分配 out[len(calls)]，按下标写
```

---

## 41.6 并行执行与超时：一轮 5 个工具，30 秒内必须收工

```go
type Call struct {
	ID   string
	Name string
	Args json.RawMessage
}

type Outcome struct {
	Idx int
	C   Call
	Res Result
	Err error
}

// RunAll 并发执行本轮所有工具调用，结果按原始顺序对齐。
func (r *Registry) RunAll(ctx context.Context, calls []Call, maxConc int) []Outcome {
	out := make([]Outcome, len(calls))
	if maxConc <= 0 {
		maxConc = 8
	}
	sem := make(chan struct{}, maxConc)
	var wg sync.WaitGroup

	for i, c := range calls { // Go 1.22+：每轮迭代的 i、c 是独立变量，闭包安全
		wg.Add(1)
		go func() {
			defer wg.Done()
			select {
			case sem <- struct{}{}:
			case <-ctx.Done(): // 用户取消时不要傻等信号量
				out[i] = Outcome{Idx: i, C: c, Err: ctx.Err()}
				return
			}
			defer func() { <-sem }()

			t, ok := r.get(c.Name)
			if !ok {
				out[i] = Outcome{Idx: i, C: c, Err: fmt.Errorf("unknown tool %q", c.Name)}
				return
			}
			// 单工具超时：一个慢工具不能吃掉整轮的预算
			tctx, cancel := context.WithTimeout(ctx, t.timeout)
			defer cancel()

			defer func() { // 工具 panic 不能带走整个进程（库代码不许 panic，但第三方工具不由你控制）
				if p := recover(); p != nil {
					out[i] = Outcome{Idx: i, C: c, Err: fmt.Errorf("tool %s panicked: %v", c.Name, p)}
				}
			}()
			res, err := t.Call(tctx, c.Args)
			out[i] = Outcome{Idx: i, C: c, Res: res, Err: err}
		}()
	}
	wg.Wait() // 每个 goroutine 都受 ctx 约束，取消后会很快返回，不会挂住
	return out
}
```

超时的三层预算（和 40.7 的三层超时是同一个思路）：

| 层 | 典型值 | 作用 |
| --- | --- | --- |
| 单个工具 | 5~15s | 防一个 `fetch_url` 拖死全轮 |
| 整轮工具执行 | 30s | 防「5 个工具 × 各自超时」累加爆炸 |
| 整个 Agent 请求 | 60~180s | 用户能忍受的上限（第 43 章的步数预算） |

> ⚠️ `wg.Wait()` 是「不可中断」的，之所以这里安全，是因为**每个 goroutine 的 ctx 都从父 ctx 派生**，父 ctx 一取消，工具调用立刻返回。反过来说：**如果你写了一个不尊重 `ctx` 的工具（比如 `time.Sleep` 或裸 `http.Get`），整个并发模型就崩了**。工具注册表应该拒绝这种实现——在 CI 里加一个测试，把所有工具都跑一遍「ctx 取消后 100ms 内必须返回」。

---

## 41.7 结果截断与错误回传：既要省 token，又不能骗模型

**截断的四条策略，按优先级排：**

| 策略 | 做法 | 适合 | 代价 |
| --- | --- | --- | --- |
| 只回结构化字段 | 网页/HTML → 提取正文 → 再摘要 | 抓取类工具 | 需要解析器 |
| 分页 + 游标 | 回前 20 条 + `next_cursor`，模型可再调 | 列表类（搜索、日志） | 模型得多跑一轮 |
| 头部+尾部截断 | 保留前 N 字节和后 M 字节，中间写「省略 X 字节」 | 长文本、代码文件 | 可能丢关键中间段 |
| 硬截断 + 落盘 | 全文存对象存储，只在上下文放 URI + 前 2KB | 超大输出 | 模型看不见全文 |

无论哪种，**必须显式告诉模型「我截断了」**：

```go
const maxToolContent = 8 << 10 // 8KB，约 2~3k token，按你的窗口调

func (o Outcome) AsMessage() llm.Message {
	content := o.Res.Content
	if o.Err != nil {
		// 错误也要回传，但必须"可行动"：不要 stack trace、不要 SQL、不要内网 IP
		content = fmt.Sprintf(`{"error":%q,"retryable":%t,"hint":"请检查参数；连续两次失败请换工具或直接回答用户"}`,
			o.Err.Error(), isRetryable(o.Err))
	} else if o.Res.Truncated || len(content) > maxToolContent {
		content = truncateHeadTail(content, maxToolContent)
		content += fmt.Sprintf("\n\n[注意：输出已截断，完整内容 %d 字节。需要更多请用 offset 参数分页读取]", len(o.Res.Content))
	}
	return llm.Message{Role: llm.RoleTool, ToolCallID: o.C.ID, Name: o.C.Name, Content: content}
}
```

三个反直觉的结论：

1. **错误信息是 prompt，不是日志。** 你写 `pq: relation "orders" does not exist`，模型会试着改表名；你写 `internal error at 0x4a2f`，模型会瞎猜。**给模型看的错误要写成「下一步怎么做」。**
2. **不告诉模型「被截断」，它一定会编**。这是幻觉最廉价的来源之一：它看到「...（完）」，就以为这就是全部，然后自信地总结出错误结论。
3. **`isRetryable` 要分层**：`rate limited` / `timeout`（模型可重试）、`invalid args`（模型可以改参数再试）、`permission denied`（模型改参数也没用，必须让它换路或直接告诉用户）。这和第 7 章的错误分类学是同一套东西，只是消费者从「程序」变成了「模型」。

---

## 41.8 幂等、副作用控制与租户隔离

### 41.8.1 幂等键

```go
// 幂等键 = 会话 + 工具 + 参数指纹。
// 语义：同一个会话里，同一工具、同样的参数，第二次调用直接返回第一次的结果。
func idemKey(convID string, c Call) string {
	h := sha256.Sum256(append([]byte(c.Name+"\x00"), c.Args...))
	return "idem:" + convID + ":" + hex.EncodeToString(h[:12])
}

type idemStore interface {
	// Get 返回上次的结果；不存在返回 (nil, false, nil)
	Get(ctx context.Context, key string) ([]byte, bool, error)
	// SetNX 抢占执行权：抢不到说明别人正在执行/已经执行完（分布式去重）
	SetNX(ctx context.Context, key string, ttl time.Duration) (bool, error)
	Set(ctx context.Context, key string, v []byte, ttl time.Duration) error
}
```

- **只读工具**（搜索、查询、读文件）：幂等天然成立，随便重试。
- **可重复写**（覆盖写文件、`PUT` 语义）：幂等，可重试。
- **不可重复写**（下单、发邮件、转账、删除）：**必须带幂等键**，且键要落 Redis（多实例共享）而不是内存 map。
- **危险工具**（删除、支付、发包）：除了幂等键，还要**人工审批**——这就是第 39 章 Demo 2 那个 WebSocket 审批弹窗的用处。

### 41.8.2 租户隔离的三道墙

```
① 上下文隔离：tenant 从会话/鉴权信息注入 ctx，绝不来自工具参数
② 数据隔离：DB 层 RLS / 每租户独立 schema / 每租户独立连接池（挑一个）
③ 执行隔离：文件系统按租户挂载子目录；外部调用用租户自己的凭据
```

最容易漏的是**第 ② 道**：你的 `search_docs` 工具写成 `SELECT * FROM docs WHERE text LIKE $1`，忘了 `AND tenant_id = $2`，一次就全泄了。**这类 SQL 建议由代码生成器产出（第 37 章），把租户条件做成不可删除的模板。**

---

## 41.9 沙箱：从「信任工具」到「不信任工具」

只要工具执行的是**模型生成的代码或命令**（Python 解释器、shell、SQL），你就必须沙箱化。CLAUDE 的教训值钱：**没有「参数校验做好就安全」这回事**。

| 方案 | 隔离强度 | 冷启动 | 适合 | 备注 |
| --- | --- | --- | --- | --- |
| 同进程加分号级校验 | ★ | 0 | 只读、无副作用工具 | 出了 bug 就是 RCE |
| 子进程 + 进程组 + rlimit | ★★ | ~5ms | shell / 脚本 / CLI | 必须杀进程组，透传环境变量是自杀 |
| 子进程 + WASI（`wazero`） | ★★★ | <1ms | 用户上传的纯计算代码 | 纯 Go 无需 cgo；生态受限（无网络、文件受限） |
| 容器（每调用一个容器 / 复用池） | ★★★★ | 100~800ms | 需要完整工具链 | K8s Job / Docker SDK；成本最高 |
| gVisor / Firecracker / microVM | ★★★★★ | 50~200ms | 多租户不可信代码 | 运维复杂度上一个台阶 |
| 远程沙箱服务（E2B 之类） | ★★★★ | 100ms+ | 不想自己运维 | 数据出域，注意合规 |

```go
// 子进程沙箱的"最小正确版"
func RunSandboxed(parent context.Context, argv []string, workspace string, timeout time.Duration) ([]byte, error) {
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	// 关键一：独立进程组 + Cancel 时杀整个组。
	// 否则 `bash -c "sleep 999 &"` 拉起的孙进程会活下来，成为孤儿（真实事故）。
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true} // Linux/Unix；Windows 无此字段
	cmd.Cancel = func() error { return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL) }
	cmd.WaitDelay = 2 * time.Second // Go 1.20+：Cancel 之后还不退，就强断 IO

	// 关键二：环境变量白名单。透传 os.Environ() 等于把 API key 交给模型。
	cmd.Env = []string{"PATH=/usr/local/bin:/usr/bin:/bin", "HOME=" + workspace, "LANG=C.UTF-8"}
	cmd.Dir = workspace

	var out bytes.Buffer
	lw := &limitedWriter{w: &out, n: 1 << 20} // 1MB 上限，防止 `cat /dev/urandom`
	cmd.Stdout, cmd.Stderr = lw, lw

	err := cmd.Run()
	if ctx.Err() == context.DeadlineExceeded {
		return out.Bytes(), fmt.Errorf("sandbox timeout after %s", timeout)
	}
	return out.Bytes(), err
}

type limitedWriter struct {
	w io.Writer
	n int64
}

func (l *limitedWriter) Write(p []byte) (int, error) {
	if l.n <= 0 {
		return len(p), nil // 静默丢弃，别让工具因为"写不出去"卡死
	}
	orig := len(p)
	if int64(len(p)) > l.n {
		p = p[:l.n]
	}
	n, err := l.w.Write(p)
	l.n -= int64(n)
	return orig, err // 必须返回 orig（原始长度），否则 io 会认为 short write
}
```

> 🩸 **事故模板**：`exec.CommandContext(ctx, "sh", "-c", modelOutput)`。这一行代码同时给了模型：任意命令执行、宿主环境变量（含 API key）、宿主文件系统。**这也是为什么「让 Agent 自己写代码并执行」的产品，一定要先把 41.9 整节做完再上线。**

---

## 41.10 注册表与审计

```go
type entry struct {
	t       Tool
	timeout time.Duration
	roles    map[string]bool // 哪些角色可见/可用
	mutating bool            // 是否有副作用（决定是否要幂等键 / 审批）
}

type Registry struct {
	mu    sync.RWMutex
	tools map[string]entry
}

func (r *Registry) Register(t Tool, opts ...Option) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, dup := r.tools[t.Name()]; dup {
		return fmt.Errorf("duplicate tool %q", t.Name()) // 名字冲突必须报错，不能后者覆盖
	}
	e := entry{t: t, timeout: 10 * time.Second, roles: map[string]bool{"user": true}}
	for _, o := range opts {
		o(&e)
	}
	r.tools[t.Name()] = e
	return nil
}

// Visible：只把"当前会话能用的工具"发给模型。
// 好处：① 省 token；② 模型不可能去调它看不见的工具（最小权限原则的第一道防线）
func (r *Registry) Visible(role string) []llm.ToolSchema {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]llm.ToolSchema, 0, len(r.tools))
	for _, e := range r.tools {
		if e.roles[role] {
			out = append(out, llm.ToolSchema{Name: e.t.Name(), Description: e.t.Description(), Schema: e.t.InputSchema()})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name }) // 顺序稳定 = prompt 缓存能命中
	return out
}
```

**审计**是这一节唯一不能省的东西。每次调用至少记一条结构化日志（`log/slog`，第 35 章）：

```go
func (r *Registry) Call(ctx context.Context, c Call) (Result, error) {
	start := time.Now()
	res, err := r.callInner(ctx, c)
	slog.InfoContext(ctx, "tool.call",
		"tool", c.Name,
		"tool_call_id", c.ID,
		"args_bytes", len(c.Args),
		"args_hash", shortHash(c.Args), // 记 hash 不记原文：参数里可能有 PII
		"result_bytes", len(res.Content),
		"truncated", res.Truncated,
		"dur_ms", time.Since(start).Milliseconds(),
		"error", err,
	)
	return res, err
}
```

配合 OTel 的 span（`tool.call` 作为 `llm.request` 的子 span），你才能回答三个灵魂问题：**这个小时哪个工具最慢？哪个工具的失败率突然涨了？这次回答到底花了多少 token 在工具输出上？**

---

## 41.11 Java 对照：五处会猜错的地方

1. **没有 `@Valid`，而且校验发生在「请求进来之前」。** Spring 里校验是入口的过滤器；这里校验的输出是「一条 `role:"tool"` 的错误消息」——**校验失败不是 4xx，而是一次成功返回（内容是一段错误 JSON）**。想清楚这个，你的错误处理就顺了。
2. **没有容器，所以「注册」是显式的。** `@Component` + 反射扫描在 Go 里不存在（第 10 章）。`Register(t)` 一行行写，换来的是「我一眼能看到全部工具」——这在安全审计时价值巨大。
3. **权限模型换了主角。** Spring Security 的主体是「已认证用户」；这里的「调用者」是模型，**只能代理用户的权限**。所以要传的是「用户授权的工具子集 + 租户上下文」，而不是一个 `Authentication` 对象。把 `SecurityContextHolder` 那套搬过来会做出「所有 Agent 都以系统管理员身份跑」的经典漏洞。
4. **没有 `@Transactional`。** 一个工具改了 DB、下一个工具失败了，没有回滚可言。跨工具一致性只能靠：`saga`（补偿）、幂等键（41.8）、或者把整个流程塞进 Temporal workflow（第 46 章）。
5. **线程池换成信号量 + goroutine。** `ThreadPoolExecutor` 的「队列 + 拒绝策略」在这里对应「有界 channel/信号量 + ctx 取消」。区别是**没有队列**——排队应该发生在上游（限流），而不是在本地攒一堆 goroutine 等着（第 26 章）。

---

## 41.12 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| 能读到 `/etc/passwd` | 直接 `os.ReadFile(args.Path)` | `filepath.IsLocal` + `Rel` 校验 / `openat2` / 沙箱 | 41.4 |
| 越权读别人数据 | SQL 少了 `tenant_id` 条件 | 租户条件进代码生成模板；`tenant` 从 ctx 取 | 41.8.2 |
| 400 `must be followed by tool messages` | 某个工具没回填结果 | 失败的调用也要回一条 `role:"tool"` | 41.5 |
| 工具结果被模型"编"了 | 截断后没告诉模型 | 截断处显式写「已截断，共 N 字节」 | 41.7 |
| 一轮工具卡 5 分钟 | 串行执行 + 无单工具超时 | `RunAll` 并发 + 每工具 `WithTimeout` | 41.6 |
| 孙子进程变孤儿 | `exec.CommandContext` 只杀直接子进程 | `Setpgid` + `Kill(-pid)` + `WaitDelay` | 41.9 |
| 模型读到了你的 API key | 透传了 `os.Environ()` | 环境变量白名单 | 41.9 |
| 重复下单/重复发邮件 | 重试重放了副作用 | 幂等键 + Redis `SetNX` | 41.8.1 |
| 工具结果顺序在测试里飘 | 按完成顺序 append | 预分配 `out[len(calls)]` 按下标写 | 41.5 |
| token 账单大半是工具输出 | 无截断上限 | `maxToolContent` + 分页游标 | 41.7 |
| prompt 缓存突然不命中 | `Visible()` 遍历 map 顺序随机 | 按名字排序后再发给模型 | 41.10 |
| 工具 panic 打挂整个服务 | 没 `recover` | `RunAll` 里 `defer recover()`，只有工具层允许 | 41.6 |

## 41.13 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 接口 | `Name/Description/InputSchema/Call` 四件套；泛型 `In` 保类型安全 |
| schema | `jsonschema.Reflect` 构造时算一次；strict 模式要 `DoNotReference`+`AllowAdditionalProperties:false` |
| 校验 | 两层：结构（schema）+ 业务（路径/SQL/租户）；**租户 ID 绝不进参数** |
| 协议 | 每个 `tool_call_id` 必有一条 `role:"tool"` 消息，失败也要回 |
| 并发 | 预分配结果切片按下标写；每工具 `WithTimeout`；`recover` 兜 panic |
| 超时 | 单工具 5~15s / 整轮 30s / 整请求 60~180s |
| 截断 | 优先级：结构化 → 分页游标 → 头尾截断 → 落盘放 URI；**必须告知模型** |
| 错误回传 | 写成「下一步怎么做」，不写 stack trace；分 retryable/fixable/fatal |
| 幂等 | 写工具必须带幂等键（Redis `SetNX`，多实例共享） |
| 沙箱 | 子进程要 `Setpgid`+杀进程组；透传环境变量=泄密；不可信代码上 wasm/容器 |
| 注册表 | 名字冲突报错；`Visible(role)` 只暴露该角色能用的（最小权限 + 省 token） |
| 审计 | `slog` 记 tool/args_hash/耗时/字节数/错误；OTel 挂 `tool.call` span |

## 41.14 自检题

1. **模型返回了 5 个 `tool_calls`，其中第 3 个超时了。你必须回填几条消息？内容分别是什么？**
   > 答案要点：**5 条**，每条对应一个 `tool_call_id`。超时那条的 `content` 是一段可行动的错误 JSON（`{"error":"timeout","retryable":true,"hint":"..."}`），而不是空字符串或省略——省略会直接破坏协议（下一轮 400）。顺序按原始 index 稳定输出。

2. **为什么「租户 ID」绝对不能作为工具参数出现在 schema 里？如果业务真的需要它（比如导出某租户报表）怎么办？**
   > 答案要点：参数是模型生成的，幻觉/注入可以让它填别的租户 → 越权。正确做法是从会话/鉴权上下文中取 `tenant`（`ctx` 注入），工具内部强制拼接；如果确实需要「管理员跨租户操作」，那是另一个工具 + 另一个角色 + 显式审批，而不是一个可填字段。

3. **你的 `read_file` 已经做了 `strings.HasPrefix(filepath.Clean(p), "/data")`，为什么还是不安全？**
   > 答案要点：① 前缀匹配不区分目录边界，`/data-evil` 也能过；② 符号链接可以把 `/data/link` 指向 `/etc`，`Clean` 不解析符号链接；③ 如果 p 是绝对路径，`filepath.Join` 会「吞掉」root。正解：`filepath.IsLocal` + `Rel` 判定 + `EvalSymlinks` 或 `openat2(RESOLVE_BENEATH)`，最稳是上沙箱。

## 41.15 延伸阅读

- `github.com/invopop/jsonschema`：`reflect.go`（tag 语义、`Reflector` 各开关）、README 里的 examples
- `github.com/google/jsonschema-go`（MCP 官方 SDK 用的 schema 推断与校验，支持 2020-12）
- `github.com/santhosh-tekuri/jsonschema/v6`：编译 + 校验 JSON Schema
- `github.com/cloudwego/eino` 的 `ToolsNode`：一次工具节点的完整实现（并发、错误、流式）
- `github.com/tetratelabs/wazero`：纯 Go 的 WASM 运行时（无 cgo），适合做代码沙箱
- OpenAI 文档：Function calling（含并行调用与 strict 模式限制）
- 上一章：[40. LLM 客户端：抽象、流式与韧性](40-llm-client-streaming.md)
- 下一章：[42. MCP：协议与 Go SDK 实战](42-mcp.md)

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：第 40 章（`tool_calls` 的产生侧）、第 37 章（schema 自动生成）、第 7 章（错误分类）、第 26 章（有界并发）
- 下一章：第 42 章（MCP：把工具变成跨进程协议）
- 相关：第 39 章（工具审批弹窗用 WebSocket）、第 43 章（多轮调用的编排）、第 47 章（限流与权限）
