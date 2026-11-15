# 31. JSON 编解码：性能与协议设计

> **状态**：`已完成（初稿）` ｜ **所属**：第五部分 · 运行时与性能 ｜ **编号**：31 / 54
> **本章定位**：JSON 是 Go 服务与 LLM 之间唯一的公共语言，而 `encoding/json` 里埋着 Go 语言层面最阴的几个坑：`omitempty` 吃掉 `false`、`float64` 吃掉订单号、`Unmarshal` 静默忽略幻觉字段。这一章把编解码器拆开，然后给出 Agent 场景的「严格解码」模板。
> **读法**：31.3.2 的 `omitempty` 与 31.3.4 的流式解码是必读；做结构化输出的直接抄 31.5。

---

## 31.1 引子：19 位的订单号，变成了 float

一个网关把上游 JSON 直接解到 `map[string]any` 再拼装转发。上线三天后财务发现对账差异——`order_id: 1024512334455667789` 到了下游变成 `1024512334455667800`。原因只有一行：`encoding/json` 把 JSON 数字默认解成 **`float64`**，而 float64 只能精确表示到 2^53。

同一周还有两个更隐蔽的事故：

- LLM 返回的工具参数 `{"enabled": false}`，被 `omitempty` 标签吃掉了 `false`，后端把「显式关闭」理解成「没传，用默认值 true」——**功能开关静默失效**。
- LLM 幻觉出了一个 `{"timeout_ms": 30, "timeout": 30}` 字段，`Unmarshal` 静默忽略未知字段，于是 30ms 的超时被当成默认 30s 执行，把上游打了 2000 次。

三个事故的共同点：**`encoding/json` 的默认行为是「宽容」，而宽容在生产里等于「静默的数据损坏」**。

> 🎯 本章的立场：**JSON 不是「序列化细节」，是协议设计。** 你对 tag 的每一个选择、对 `Decoder` 的每一个开关，都是在定义这个服务的契约边界。Java 侧你有 Jackson 的一堆 `DeserializationFeature` 开关，Go 侧的开关更少但更锋利——默认宽容，严格的都要你自己打开。

---

## 31.2 直觉模型：一次 `Marshal` 走过的三站

```
 json.Marshal(v)
   │
   ├─① 类型 → 编码器的「编译器」：newTypeEncoder(t, allowAddr)
   │     ├─ 实现了 json.Marshaler？            → marshalerEncoder
   │     ├─ 是指针且 *T 实现 Marshaler？        → addrMarshalerEncoder
   │     ├─ 是 struct？→ typeFields(t) 反射一次，建 structFields 列表
   │     │     （字段下标、tag 名、quoted、omitempty/omitzero、匿名字段的提升…）
   │     │     然后生成一个闭包链：structEncoder → 每个字段一个 encoder
   │     └─ 是 int/string/slice/map…          → 各自的特化 encoder
   │
   ├─② 类型 → 编码器 的缓存：结果存进包级 cache（sync.Map），
   │     第二次同类型直接命中，不再反射 ← 这就是「JSON 有预热」的真相
   │
   └─③ 真正写字节：从 encodeStatePool（sync.Pool）拿一个 encodeState（buf []byte），
         沿闭包链写入，最后 copy 出来 —— 所以「每请求一次 Marshal」= 1 次池取 + 1 次分配
```

三个必须记住的观察：

1. **反射只在第一次发生**。`typeFields` 的字段元数据被缓存（`encode.go` 的 `typeFields`、`decode.go` 的 `cachedTypeFields`，各自有一份 `sync.Map` 缓存）；所以你看到「Go 的 JSON 慢」时，通常不是反射慢，而是**每请求的分配 + 反射生成的 encoder 闭包调用开销**。
2. **编码器和解码器是两套独立的缓存**——同一个 struct 编一次解一次，反射会发生两遍。
3. **每类型只编译一次，但每个值都要走反射路径读字段**（`reflect.Value.Field(i)` 之类），这是和 Jackson 生成 accessor 类的差距所在，也是 sonic/easyjson 存在的原因。

---

## 31.3 底层实现与六个必知细节

### 31.3.1 tag 的解析规则（`fields.go` 的 `parseTag`）

`json:"name,omitempty,string"` 里逗号后面是可选项：`omitempty`、`omitzero`（1.24）、`string`。`name` 为空（`json:",omitempty"`）时用字段名本身；`json:"-"` 跳过字段，而 `json:"-,"` 表示「字段名就叫 `-`」。

**匿名字段（嵌入）会被提升**：`struct { Base; Name string }` 里 `Base` 的导出字段会平铺到外层 JSON，除非 `Base` 自带 tag。同名冲突时，**浅层的赢，同层的都丢弃**——这条规则在 Java 侧（Jackson 的 `@JsonUnwrapped`）语义相似但更保守。

### 31.3.2 `omitempty` 的谎言与 `omitzero` 的补救

`omitempty` 的实现就是 `isEmptyValue`：`false`、`0`、`""`、`nil` 指针/接口、`len == 0` 的 array/map/slice/string——**全部被当成"空"，直接不出现在 JSON 里**。注意：**struct 永远不为空**，所以 `omitempty` 加在 struct 字段上完全没有效果（除非它是零长的 array）。

| 值 | `omitempty` 会省略吗 | `omitzero`（1.24）会省略吗 | 说明 |
| --- | --- | --- | --- |
| `false` | ✅ 省略 | ✅ 省略 | **「显式 false」和「没设置」不可区分** |
| `0` | ✅ 省略 | ✅ 省略 | 同上 |
| `""` | ✅ 省略 | ✅ 省略 | 同上 |
| `nil` 指针/slice/map | ✅ 省略 | ✅ 省略 | — |
| 零值 struct | ❌ **不会** | ✅ 会（零值或 `IsZero()==true`） | 这是 `omitzero` 最实用的场景 |
| `time.Time{}` 零值 | ❌ 不会（旧版） | ✅ 会（`time.Time` 有 `IsZero`） | 时序字段的正确写法 |

所以「三态」（未设置 / 零值 / 非零）在 Go 里必须显式表达：

```go
type ToolParams struct {
	Enabled  *bool   `json:"enabled,omitempty"`   // ✅ 三态：nil=未传, true/false=显式
	Timeout  *int    `json:"timeout_ms,omitempty"`
	Model    string  `json:"model,omitempty"`     // 二态就够：空串与"未传"等价
	Deadline time.Time `json:"deadline,omitzero"` // 1.24+：零值时间不序列化
}

func deref[T any](p *T, def T) T {
	if p == nil {
		return def
	}
	return *p
}

// 使用：默认值只在「真的没传」时生效
enabled := deref(p.Enabled, true)
timeout := time.Duration(deref(p.Timeout, 30000)) * time.Millisecond
```

### 31.3.3 数字精度：`float64` 的三处陷阱

```go
var m map[string]any
_ = json.Unmarshal([]byte(`{"order_id":1024512334455667789,"qty":3}`), &m)
fmt.Printf("%v\n", m["order_id"]) // 1.0245123344556678e+18 ← 精度已丢，无法挽回

// ✅ 方案 A：已知结构体 —— 用 int64/uint64 直接接
type Order struct {
	OrderID uint64 `json:"order_id"`
	Qty     int    `json:"qty"`
}

// ✅ 方案 B：未知结构 —— UseNumber() 保留原始字面量
dec := json.NewDecoder(strings.NewReader(payload))
dec.UseNumber()
var raw map[string]any
_ = dec.Decode(&raw)
if n, ok := raw["order_id"].(json.Number); ok {
	id, _ := n.Int64() // 需要时再转，转不动就报错，而不是静默变形
	_ = id
}
```

另外两个坑：`float64` 序列化 `1.0` 会输出 `1`（没小数），需要 `json.Number("1.0")` 或自定义类型；`interface{}` 里的 `nil` 与字面 `null` 在 Go 侧都变成 `nil`，反向无法区分。

### 31.3.4 `RawMessage` 与流式 `Decoder`

**`json.RawMessage`**（`type RawMessage []byte`）实现 `Marshaler`/`Unmarshaler`，作用是**延迟或跳过解析**：路由层只想看 `type` 字段决定分发给谁，其余部分原样透传——用它是零成本的。

```go
type Envelope struct {
	Type string          `json:"type"`
	Data json.RawMessage `json:"data"` // 先不解析
}

func route(body []byte) error {
	var env Envelope
	if err := json.Unmarshal(body, &env); err != nil {
		return err
	}
	switch env.Type {
	case "tool_call":
		var p ToolParams
		return json.Unmarshal(env.Data, &p) // 只在这一支才解析
	case "message":
		var m Message
		return json.Unmarshal(env.Data, &m)
	}
	return fmt.Errorf("unknown type %q", env.Type)
}
```

**流式解码**才是大杀器，LLM 的 JSONL 数据集、批量 embedding 输入、`data:` 分包后的 SSE payload 全靠它：

```go
// JSONL / NDJSON：一个流里有 N 个 JSON 对象，绝不能 io.ReadAll 再 Split
func streamJSONL(r io.Reader, fn func(Item) error) error {
	dec := json.NewDecoder(bufio.NewReaderSize(r, 1<<16))
	for {
		var it Item
		if err := dec.Decode(&it); err != nil {
			if errors.Is(err, io.EOF) {
				return nil
			}
			return fmt.Errorf("decode: %w", err)
		}
		if err := fn(it); err != nil {
			return err
		}
	}
}
```

配套 API（都在 `encoding/json` 里，没有隐藏魔法）：`dec.More()` 判断数组/流里还有没有下一个值、`dec.DisallowUnknownFields()` 严格字段、`dec.UseNumber()` 保精度、`dec.Buffered()` 拿到缓冲里剩余未消费的字节（拼接下一帧时有用）、`enc.SetEscapeHTML(false)` 关闭 `<>&` 转义（URL、HTML 片段必关，默认是开的）。

### 31.3.5 `encoding/json/v2`（1.25 实验）

Go 1.25 提供了 `GOEXPERIMENT=jsonv2` 下的两个新包：**`encoding/json/v2`**（面向值）与 **`encoding/json/jsontext`**（面向 token/字节，暴露编解码的底层控制）。`encoding/json` 在开启实验后会变成 v2 的兼容包装，**旧的语义与错误信息被尽量保留**。

v2 的设计目标是三件事：**更正确的默认行为、更快的路径（少一层接口间接）、可组合的选项**。选项走「函数式 Option」：`json.Marshal(v, json.Deterministic(true), json.RejectUnknownMembers(true), json.WithMarshalers(...))` 这种形态（具体选项名以你本地版本的 `encoding/json/v2` 文档为准，本节故意不写死）。

> ⚠️ **什么时候能用**：内部服务、对错误信息不敏感、且 CI 里能固定 `GOEXPERIMENT`——可以试。**公共 API 网关**慎用：`json/v2` 在若干边界（重复键、大小写匹配、非法 UTF-8、数字表示）上与 v1 不同，客户端可能依赖 v1 的宽容。迁移的正确方式是**先给单测补齐边界用例，再开实验开关**。

### 31.3.6 sonic / easyjson：什么时候值得换库

| 方案 | 原理 | 相对标准库 | 代价 | 适用 |
| --- | --- | --- | --- | --- |
| `encoding/json` | 反射 + 类型级缓存 + `sync.Pool` | 基准 | 无 | **默认选择**，99% 场景够用 |
| `encoding/json/v2` | 1.25 实验，重构的编解码核心 | 通常更快、语义更严格 | 实验特性、行为差异 | 内部服务、新项目试点 |
| `easyjson` | `go generate` 生成 `MarshalJSON`/`UnmarshalJSON` | 快 2~3 倍，零反射 | 代码膨胀、struct 变更要重新生成 | 字段稳定的热点 DTO |
| `sonic` | 运行时生成汇编/JIT 编解码器 | 常见口径是快 2~4 倍 | 平台限制（amd64/arm64 + AVX2/NEON）、与 Go 版本强绑定、边界行为差异 | 网关、大 payload、JSON 密集型 |

**判断标准只有一条**：`pprof` 里 `encoding/json` 占比 > 15% **且** 你已经用「少 marshal 一次 / 复用 buffer / 用 `RawMessage` 跳过」压过一轮。换句话说：**换库是最后一招，不是第一招**（第 29 章）。

---

## 31.4 Java 对照

| 主题 | Jackson（Java） | `encoding/json`（Go） | 会咬人的差异 |
| --- | --- | --- | --- |
| 复用方式 | `ObjectMapper` 是线程安全单例，内部缓存 serializer | 包级函数 + 内部 `sync.Map` 缓存，无需实例 | Go 侧不需要「注入一个 Mapper」，也没有全局配置中心 |
| 未知字段 | `FAIL_ON_UNKNOWN_PROPERTIES`（**默认 true = 报错**） | **默认忽略**（要 `DisallowUnknownFields`） | Java 直觉会让你以为 LLM 幻觉字段会报错 |
| 空值处理 | `@JsonInclude(NON_NULL)` + 包装类型区分三态 | `omitempty`（连 `false/0` 都省）+ `*T` 表达三态 | `omitempty` 比 NON_NULL 更贪婪 |
| 高精度数字 | `BigDecimal` / `long` 直接映射 | 默认 `float64`，要 `json.Number` 或强类型 | 订单号精度事故的头号来源 |
| 定制序列化 | 注解 + 注册 Serializer / Mixin | 实现 `MarshalJSON`/`UnmarshalJSON` + `RawMessage` | Go 没有全局注册表，定制跟着类型走 |
| HTML 转义 | 默认不转义 | **默认把 `<>&` 转义成 `\u003c` 等** | 传 URL、HTML、Markdown 时必须 `SetEscapeHTML(false)` |
| map 输出顺序 | 保持插入序（LinkedHashMap） | **key 排序输出**（确定性） | 依赖顺序的签名逻辑在两边表现不同 |
| 出错处理 | 异常 + `JsonProcessingException` 层级 | `error` + `*json.SyntaxError`/`*json.UnmarshalTypeError` | 后者能给你**字段名与偏移量**，比异常更有用 |

> 💡 核心取舍：**Go 把「宽容」作为默认，把「严格」做成显式开关；Jackson 反过来。** 所以从 Java 迁过来的第一件事，应该是给所有外部输入的解码加上 `DisallowUnknownFields`——这与 31.1 的第一个事故是同一件事。

---

## 31.5 框架与工程落地：LLM 结构化输出的严格解码

LLM 的结构化输出（tool call 参数、JSON mode、schema 约束）是**最不可信的 JSON 来源**：它可能少一个引号、多一个字段、把数字写成字符串、在 JSON 后面再加一段解释。

```go
package llmio

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
)

// DecodeStrict 是对抗 LLM 幻觉的三道闸门：严格字段 + 无尾随数据 + 显式类型
func DecodeStrict(data []byte, v any) error {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields() // ① 幻觉字段直接报错，而不是静默丢弃
	if err := dec.Decode(v); err != nil {
		return fmt.Errorf("decode json: %w", err)
	}
	// ② 后面还有内容？说明模型在 JSON 之后又说话了
	if dec.More() {
		return errors.New("decode json: trailing data after first value")
	}
	return nil
}

// ExtractJSON 兜住最常见的模型坏习惯：```json … ``` 围栏、前后废话
func ExtractJSON(s string) string {
	if i := strings.Index(s, "```"); i >= 0 {
		rest := s[i+3:]
		rest = strings.TrimPrefix(rest, "json")
		if j := strings.Index(rest, "```"); j >= 0 {
			return strings.TrimSpace(rest[:j])
		}
	}
	start := strings.IndexAny(s, "{[")
	end := strings.LastIndexAny(s, "}]")
	if start >= 0 && end > start {
		return s[start : end+1]
	}
	return s
}
```

**再加两道闸门**（生产必备）：

```go
// ③ schema 校验：字段存在性、取值范围、枚举，JSON 本身管不了
import "github.com/santhosh-tekuri/jsonschema/v6"

// schema 与结构体同源生成（第 37 章：invopop/jsonschema），或写成常量
func mustCompileSchema(name, schemaJSON string) *jsonschema.Schema {
	c := jsonschema.NewCompiler()
	if err := c.AddResource(name, strings.NewReader(schemaJSON)); err != nil {
		panic(err)
	}
	sch, err := c.Compile(name)
	if err != nil {
		panic(err)
	}
	return sch
}

// ④ 解析失败 → 带着错误回灌 LLM 重试一次（比 500 好得多）
func callTool(ctx context.Context, m Model, name string, args []byte) (Result, error) {
	var p ToolParams
	if err := DecodeStrict(args, &p); err != nil {
		return retryWithFeedback(ctx, m, name, args, err) // 把 err.Error() 拼进下一轮 prompt
	}
	return execTool(ctx, name, p)
}
```

**Agent 侧的六条硬规则**：

1. **永远 `int64/uint64` 接 ID，永不用 `float64`**；不确定的结构用 `UseNumber()`。
2. **外部输入一律 `DisallowUnknownFields`**，内部服务间调用也建议开——它抓到的每一个未知字段都是接口漂移的早期信号。
3. **别用 `map[string]any` 做业务逻辑**：它把编译期类型错误全部推迟到运行期，且每层都是装箱分配。
4. **工具参数 schema 从 struct 生成、单向一致**（第 37 章），别手写两份还指望它们不漂移。
5. **流式 SSE 用 `bufio.Scanner`（记得 `Buffer` 调大，默认 64KB 上限）逐帧切 `data:`，每帧再 `Unmarshal`**，而不是把整条流攒起来一次解（第 40 章）。
6. **`json.Marshal` 前想一下能不能不序列化**：日志里打整个 request 是 JSON 成本的大头，`slog` 分组 + 惰性求值更省（第 35 章）。

---

## 31.6 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| 19 位 ID 精度丢失 | 解到 `any` 变 `float64` | 强类型 `int64` / `UseNumber()` / `json.Number` | 31.3.3 |
| 显式 `false` 被当成「未传」 | `omitempty` 吞 `false` | `*bool` + `omitempty`，或用 `omitzero` | 31.3.2 |
| 时间零值 `0001-01-01T00:00:00Z` 出现在响应里 | `omitempty` 对 struct 无效 | `time.Time` + `omitzero`（1.24），或 `*time.Time` | 31.3.2 |
| LLM 幻觉字段被静默接受 | 默认忽略未知字段 | `dec.DisallowUnknownFields()` | 31.3.4 |
| JSON 之后还有一段文字，`Decode` 却不报错 | `Decode` 只消费第一个值 | 检查 `dec.More()`，或 `io.EOF` 后的残留 | 31.5 |
| URL 里的 `&` 变 `\u0026` | 默认开启 HTML 转义 | `enc.SetEscapeHTML(false)` | 31.3.4 |
| 大 JSON 一次性 `io.ReadAll` 把内存打爆 | 没有流式 | `json.Decoder` + `bufio`，或 `LimitReader` | 31.3.4 |
| 换 sonic 后某接口偶发解析失败 | sonic 对边界输入（重复键、非法转义）行为与标准库不同 | 换库前跑全套边界用例；或加 fallback 到标准库 | 31.3.6 |
| 每个 token 都 `json.Marshal` 一次 SSE | 编码开销叠加在 token 频率上 | 批量合并 / 手写 `append` 拼字节 | 第 30、39 章 |
| benchmark 里 JSON 很快，线上很慢 | 微基准只有一个类型、一次 marshal；线上是并发 + 大 payload + 分配压力 | 用真实 payload 压测，看 `allocs/op` 与 `-sample_index=alloc_space` | 第 29 章 |

---

## 31.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 反射成本 | 每类型只反射一次，元数据进 `sync.Map` 缓存；编码/解码各一份 |
| 编码路径 | 类型 → encoder 闭包链；缓冲从 `encodeStatePool` 取 |
| `omitempty` | `false/0/""/nil/空容器` 全被省略；对 struct 无效 |
| `omitzero` | 1.24，按零值或 `IsZero()` 判断，`time.Time` 的正确解法 |
| 三态 | 用 `*T`；二态用零值即可 |
| 精度 | 默认 `float64`；ID 用 `int64`，未知结构 `UseNumber()` |
| `RawMessage` | 延迟解析 / 原样透传，路由分发的标准做法 |
| 流式 | `json.NewDecoder` + `Decode` 循环，别 `io.ReadAll` |
| 严格解码 | `DisallowUnknownFields` + 检查 `dec.More()` + schema 校验 |
| json/v2 | 1.25 实验（`GOEXPERIMENT=jsonv2`），`encoding/json/v2` + `jsontext` |
| 换库时机 | pprof 里 JSON > 15% 且已做过「少 marshal + 复用 buffer」 |

## 31.8 自检题

1. **一个 struct 字段写了 `json:"timeout_ms,omitempty"` 且类型是 `int`。前端传 `0` 与不传，后端看到的区别是什么？怎么修？**
   > 要点：**没有区别**，两者都得到 `0`。`omitempty` 在序列化侧省略零值，在**反序列化侧也会把缺省保持为零值**，所以「显式 0」与「未传」在 Go 里无法区分。修法：改成 `*int`（nil = 未传），业务层用 `deref(p, default)`；或者换一种设计——用 `omitzero` + 零值即默认值的语义，并在文档里写清。

2. **为什么 `json.Marshal` 同一个 struct 的第二次调用会明显快于第一次？这算是「Go 需要 JIT 预热」的证据吗？**
   > 要点：第一次要 `typeFields` 走反射建字段元数据、生成 encoder 闭包，并缓存到包级 `sync.Map`；之后命中缓存。这不是 JIT——机器码从第一次就一样，慢的是**一次性反射建表**。所以它属于「语义预热」，压测采集窗口要把这段排除（第 27 章）。

3. **LLM 的 JSON 输出里多了一个字段，你的服务静默跑错了两天。你会加哪三样东西防止复现，各自挡什么？**
   > 要点：① `dec.DisallowUnknownFields()`——挡字段漂移与幻觉字段；② `dec.More()` / 尾随检查——挡「JSON 后面还有话」；③ JSON Schema 校验（`santhosh-tekuri/jsonschema`）——挡「字段对但值非法」（枚举、范围、必填）。再加一条工程动作：解析失败带错误回灌模型重试一次，而不是直接 500。

## 31.9 延伸阅读

- `$GOROOT/src/encoding/json/encode.go`（`typeFields`、`newTypeEncoder`、`encodeStatePool`、`isEmptyValue`）、`decode.go`（`cachedTypeFields`、`(*Decoder).Decode`）、`fields.go`（`parseTag`）、`stream.go`（`Decoder`/`Encoder`）
- Go 1.24 release notes 的 `omitzero`；Go 1.25 release notes 的 `encoding/json/v2` 与 `GOEXPERIMENT=jsonv2`
- 设计文档 *jsonv2*（github.com/go-json-experiment/json 的 README，讲清楚了 v1 的十来个历史包袱）
- `github.com/bytedance/sonic`、`github.com/mailru/easyjson`、`github.com/santhosh-tekuri/jsonschema/v6`
- 第 40 章：SSE 客户端里 `bufio.Scanner` 的分包与 64KB 上限问题

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 30 章 性能优化](30-perf-tuning.md)
- 相关：字符串与零拷贝拼接见[第 13 章](13-string-bytes-rune.md)；反射与代码生成的取舍见[第 37 章](37-codegen-reflection.md)；SSE 解析与客户端流式见[第 40 章](40-llm-client-streaming.md)；工具参数 schema 见[第 41 章](41-tool-system-function-calling.md)
- 下一章：[第 32 章 高并发高性能实战](32-high-concurrency.md)
