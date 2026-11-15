# 36. 流式输出工程：从首 token 到增量渲染

> **状态**：`已完成（初稿）` ｜ **所属**：第七部分 · 输入输出管道 ｜ **编号**：36 / 52
> **本章定位**：流式看起来只是「把 chunk 转发给前端」，实际上是**一条有状态、有顺序、有背压、还要计费的长连接数据管道**。这一章把 TTFT、事件分段、增量 JSON、工具调用分片、背压、反代这六件事一次讲透。
> **读法**：§36.2（TTFT 账）和 §36.7（工具调用分片）是必读，后者是**每个自研 Agent 平台都会踩一次的坑**。要动手的人直接抄 §36.6 / §36.7 的代码。

---

## 36.1 引子：三个同时发生的流式事故

Aurora 的流式上线第 3 周，一天内三个互相独立的告警：

**事故 1（下午 2 点，工具调用集体失败）：**
```
某类客服 Agent 的工具调用成功率从 98.2% 掉到 61%（持续 22 分钟）
日志：json: cannot unmarshal string into Go value of type OrderQuery
```

**事故 2（下午 3 点，用户投诉「卡住不动」）：**
```
客服：用户说「它先说了一句，然后就一直转圈，等了 2 分钟才出完」
nginx access log：single request 127s，upstream 正常返回 200
```

**事故 3（下午 4 点，账单对不上）：**
```
计费：某租户当天记录 4.2 万次 Run 的 token 用量
上游：该租户实际调用 5.1 万次
差额 9 千次：客户端在流式中途断开，我们的结算逻辑没跑
→ 已经付给供应商 $180，账单上收了 $0
```

三个事故的根因，全都是流式的「状态性」：

1. **工具调用的 `arguments` 是分片到达的字符串。** 我们在 `arguments` 只要非空就尝试 `json.Unmarshal`——于是每次只拿到 `{"order_`，解析必然失败。而失败率跟「分片大小」相关（那天供应商改了 chunk 策略，分片变碎），所以成功率突然掉。
2. **nginx 默认开启 `proxy_buffering`。** 上游每 20ms 吐一个 token，nginx 攒到 4KB 或 60s 才转发。用户看到的是「先一句，然后 127 秒静默」。
3. **流式的用量是「最后才知道」的。** 我们在 `[DONE]` 之后才结算，而客户端断开时 `[DONE]` 永远不会到——**已经花掉的钱，没有对应的账**。

> 🎯 **本章要建立的第一个共识**：
> **流式不是一个「传输方式」，它是一个「状态机 + 一份账 + 一条有序的事件日志」。** 把它当 `io.Copy` 写的人，会在上面这三个地方各踩一次。

---

## 36.2 TTFT：你只有 800ms 的第一印象

### 36.2.1 TTFT 的真实构成

用户感知的「快不快」几乎完全由 **TTFT（Time To First Token）** 决定，而不是总时长。Aurora 客服场景的实测（P50，用户点发送 → 屏幕出现第一个字）：

| 环节 | P50 | P95 | 优化手段 | 能压到 |
| --- | --- | --- | --- | --- |
| 客户端 → 网关（含 TLS 复用） | 45ms | 120ms | HTTP/2/3、连接预热、就近接入 | 25ms |
| 鉴权 + 配额 + 准入 | 8ms | 25ms | 本地缓存 token 校验、配额预取 | 5ms |
| **前处理链**（第 35 章） | **27ms** | 90ms | 规则前置、小模型旁路 | 15ms |
| 上下文装配（含检索） | 40ms | 180ms | 并行检索、前缀缓存命中 | 15ms |
| 路由决策 | 3ms | 8ms | 本地策略缓存 | 2ms |
| **排队等模型** | 210ms | **1,800ms** | 独立配额池、优先级、预热副本 | 100ms |
| **prefill（8k 上下文）** | 180ms | 420ms | 前缀缓存（命中可降到 30ms） | 40ms |
| 首 token 采样 | 35ms | 80ms | 更小的 max_tokens 上限不影响首 token | 35ms |
| 网关 → 客户端（首帧） | 30ms | 90ms | 关闭缓冲、TCP_NODELAY | 30ms |
| 前端渲染（一帧） | 16ms | 50ms | 增量渲染、避免整块 re-render | 16ms |
| **合计** | **594ms** | **2,863ms** | | **~283ms** |

三个结论：

1. **P50 里最大的两块是「排队」和「prefill」**，加起来 390ms（66%）。所以最有价值的优化不是「减少 token」，是**降低排队（第 11、14 章）和提升前缀缓存命中（第 24 章）**。
2. **P95 是 P50 的 4.8 倍**，差距几乎全在「排队」上（1,800ms vs 210ms）。**TTFT 的尾部问题就是排队问题。**
3. **前处理只占 27ms（4.5%）**，但它是唯一「花小钱省大钱」的环节——所以不要为了省 10ms 砍掉前处理，你会用 10 倍的钱补回来。

### 36.2.2 用「假首字」买时间

真正的 TTFT 很难压到 300ms 以内（模型 prefill 就是那么慢）。所以产品上有一招必做：**在真正生成之前，先流出一个 `status` 事件**。

```
t=0ms     用户点发送
t=40ms    服务端返回 status: "正在理解你的问题…"
t=180ms   status: "正在查订单 SO-7781…"     ← 工具调用期间
t=900ms   delta: "您的订单"                  ← 真正的首 token
```

用户感知的 TTFT 从 900ms 变成 40ms，**成本是 0（三个 status 事件一共 200 字节）**。代价是：这些文案必须是**诚实的**（不能每次都写「正在深度思考」，那是欺骗）。

> 💡 **一条产品原则**：**status 事件要在「每个真实步骤开始时」发，而不是按固定间隔发。** 固定间隔的假进度条（「已完成 30%」）是反模式——用户会盯着它算，然后发现它卡在 90%。

### 36.2.3 TTFT 预算表（写成 SLO，不要写成愿望）

| 场景 | TTFT P50 | TTFT P95 | 超标动作 |
| --- | --- | --- | --- |
| 交互式客服（默认） | < 700ms | < 1,500ms | 告警 + 检查排队深度 |
| 编码 Agent（允许多步） | < 1,200ms | < 3,000ms | status 事件必须更密 |
| 长文档分析 | < 2,000ms | < 5,000ms | 先出「大纲」再出详情 |
| 批处理/API 同步调用 | 不适用（非流式） | — | — |

---

## 36.3 结构图：一条流的完整生命周期

```
 上游模型 SSE                 Agent-Server（流式引擎）                  客户端
 ─────────────                ─────────────────────────                 ──────
 chunk: {delta:"您"} ──▶ ① 聚合器（按 index 聚合 tool_calls）
 chunk: {delta:"的"}        ② 增量 JSON 扫描器（结构化输出）
 chunk: {tool_calls:[…]}    ③ 分段器（token→句子→批次）
 chunk: {usage:{…}}         ④ 事件构造（seq 单调递增，run 内唯一）
                                  │
                            ⑤ 审计旁路（抽样/延迟窗口，第 37 章）
                                  │
                            ⑥ 计费预扣（按 max_tokens）+ 最终结算
                                  │
                            ⑦ 发送队列（每流一个有界 buffer）
                                  │
                            ⑧ 环形回放缓冲（最近 N 事件 / 60s，支持 Last-Event-ID）
                                  │
                                  ▼
                        SSE frame: id: 1042
                                   event: delta
                                   data: {"seq":1042,"text":"您"}
                                   ─────────────────▶ ⑨ 反代/CDN（禁缓冲）
                                                      ─────────────────▶ ⑩ 前端渲染
                                                                          │
                                                                    ⑪ 客户端 ack（可选）
                                                                          │
  ◀── 取消传播 ◀── ⑫ cancel（客户端点「停止」/ 断连）◀────────────────────┘

  背压回路：⑨→⑦ 发送水位高 → ⑦ 丢弃中间 delta / 合并批次 / 降级为最终一次性结果
```

**注意 ⑦ 和 ⑧ 是两个不同的缓冲区**，很多人只做一个：

- ⑦ 是「还没发出去的」（应对慢消费者，可丢弃中间态）
- ⑧ 是「已经发出去的」（应对断线重连，**不能丢**）

---

## 36.4 事件分段：token / 句子 / 段落 / 结构化增量

### 36.4.1 四种粒度的取舍

| 粒度 | 事件量（800 字） | 首字体验 | 前端成本 | 适合 |
| --- | --- | --- | --- | --- |
| **token 级** | ~1,200 帧 | 最好（逐字出现） | 高（1,200 次 re-render） | 短回答、打字机效果 |
| **句子级** | ~40 帧 | 差（等一句完整） | 低 | **TTS 语音**、逐句朗读 |
| **段落级** | ~6 帧 | 差 | 极低 | 长文档、报告 |
| **结构化增量** | 按字段 | 中 | 中（要 patch 语义） | 表格/卡片/表单渲染 |

Aurora 的选择是**混用**：正文用 token 级但合批到 50~80ms，结构化字段用增量 patch，工具状态用独立事件。

### 36.4.2 合批（batching）：为什么 50ms 是甜点

不做合批时，一个 35 token/s 的流每秒发 35 帧。看起来不多，但要看三个放大：

```
① 服务端写放大：8,000 条并发流 × 35 帧/s = 280,000 帧/s
   每帧 ≈ 90 字节 SSE + 一次 write 系统调用 → 25MB/s + 28 万次 syscall/s
② 反代放大：每帧过一次 nginx → 上下文切换与日志
③ 前端放大（最致命）：1,200 次 setState → React 每次 re-render + 布局计算
   实测：中端手机上 1,200 次逐帧更新，主线程占用 1.8s，滚动明显卡顿
```

合批到 50ms/80ms 之后：

| 批次间隔 | 每帧 token 数（35 tok/s） | 帧/秒/流 | 8k 并发总帧/s | 前端流畅度 |
| --- | --- | --- | --- | --- |
| 0（逐 token） | 1 | 35 | 280,000 | ❌ 卡 |
| **50ms** | ~2 | 20 | 160,000 | ✅ 流畅（默认） |
| 80ms | ~3 | 12.5 | 100,000 | ✅ 流畅 |
| 200ms | ~7 | 5 | 40,000 | ⚠️ 有顿感（像一行一行跳） |

> ⚠️ **合批的边界条件**：① 遇到句子结束符（`。！？\n`）立刻 flush，否则用户会看到「半句话停住」；② 第一批必须**不等待**（第一个 token 立刻发，否则 TTFT 白优化）；③ 流结束时强制 flush 残余。

### 36.4.3 分段器代码

```go
// stream/batcher.go —— 时间 + 语义双触发的合批器
package stream

import (
	"strings"
	"time"
)

type Batcher struct {
	interval time.Duration // 50ms
	maxBytes int           // 防止单帧过大（如模型一次吐 4KB）
	buf      strings.Builder
	first    bool          // 首批不等待
	timer    *time.Timer
	out      chan<- Frame
}

func (b *Batcher) Push(tok string) {
	b.buf.WriteString(tok)
	// ① 首批立刻发：TTFT 优先
	if b.first {
		b.first = false
		b.flush()
		return
	}
	// ② 语义边界立刻发：避免「半句话」停住
	if endsSentence(tok) || b.buf.Len() >= b.maxBytes {
		b.flush()
		return
	}
	// ③ 否则等定时器（定时器复用，不要每次 new）
	if b.timer == nil {
		b.timer = time.AfterFunc(b.interval, b.flush)
	}
}

func endsSentence(tok string) bool {
	if tok == "" {
		return false
	}
	switch tok[len(tok)-1] {
	case '。', '！', '？', '\n', '.', '!', '?':
		return true
	}
	return false
}

func (b *Batcher) flush() {
	if b.timer != nil {
		b.timer.Stop()
		b.timer = nil
	}
	if b.buf.Len() == 0 {
		return
	}
	s := b.buf.String()
	b.buf.Reset()
	b.out <- Frame{Type: "delta", Text: s}
}
```

---

## 36.5 事件类型设计：六种，够用且不多

事件类型不是越多越好——每多一种，前端就多一个分支、后端就多一处不兼容、回放就多一个坑。Aurora 收敛到六种：

| 事件 | 语义 | 载荷关键字段 | 前端行为 | 幂等 |
| --- | --- | --- | --- | --- |
| **`delta`** | 追加文本 | `seq, text` | 追加到当前块 | ✅ 按 seq 去重 |
| **`patch`** | 结构化字段增量 | `seq, path, op(replace/append), value` | 按 JSON Pointer 更新对象 | ✅ 幂等（同 path 同值） |
| **`tool`** | 工具调用状态 | `seq, call_id, name, phase(start/args/done), args_delta, result` | 渲染卡片/进度 | ✅ 按 call_id + phase |
| **`status`** | 阶段提示（非内容） | `seq, stage, message, hint` | 更新状态条 | ✅ 覆盖式 |
| **`error`** | 可恢复的错误 | `seq, code, message, retryable` | 显示 + 重试按钮 | ✅ |
| **`correction`** | **更正已发出的内容** | `seq, target_seq_from, target_seq_to, replacement, reason` | **替换**区间内容 | ✅ 按区间 |

`correction` 是这一组里最特殊的——它是第 37 章「撤回」的传输层基础。**没有它，你发出去的每一个字都不可撤销。**

```json
// 完整事件帧（SSE）
id: 1042
event: patch
data: {"seq":1042,"path":"/summary/rows/3/amount","op":"replace","value":"¥1,280","rev":7}

id: 1043
event: correction
data: {"seq":1043,"from":1010,"to":1038,"replacement":"该订单实际不支持退款，","reason":"policy_v3_update"}
```

三条硬规则：

1. **`seq` 在 Run 内单调递增、从 1 开始、不跳号。** 客户端用它去重、排序、检测缺口（发现 1041 缺失 → 主动请求重放）。
2. **`event` 名和 `data` 里的 `type` 必须一致**（有人会只用一个 `event: message` 然后靠 data 区分——那样 SSE 层的过滤能力就废了）。
3. **每个事件必须能独立解析**。不要设计「必须收到前一个才能解析这一个」的协议，否则一次丢帧就全乱。

---

## 36.6 流式中的结构化输出：增量 JSON 与失败回滚

### 36.6.1 问题

你要输出一个结构化结果（`{"summary":"…","rows":[…],"risk":"low"}`），并且希望**边生成边渲染**（用户先看到 summary，再看表格一行行长出来）。但流式给你的是一串**语法不完整的 JSON 片段**：

```
{"summary":"该订单为破损退
{"summary":"该订单为破损退款，","rows":[{"sku":"A-1","amo
{"summary":"该订单为破损退款，","rows":[{"sku":"A-1","amount":128
```

你需要在每个片段到达时，尽可能多地拿到**已经完整**的字段。

### 36.6.2 增量扫描器：找到「最后一个完整的顶层成员」

思路很朴素但极其有效：扫描整个累积缓冲，记录**最后一个顶层逗号的位置**（此处之前的所有顶层成员一定完整），然后把 `{ ... }` 补上闭合符号，交给标准 `json.Unmarshal`。

```go
// stream/incjson.go —— 增量 JSON：每来一段就抽出「已完整的顶层字段」
package stream

import (
	"encoding/json"
	"errors"
)

var ErrNotObject = errors.New("root must be object or array")

// lastSafePrefix 返回 buf 中「最后一个顶层成员结束」的位置（顶层逗号的下标）。
// 该位置之前的内容，加上一个闭合括号，一定构成合法 JSON。
func lastSafePrefix(buf []byte) int {
	depth, safe := 0, 0
	inStr, esc := false, false
	for i := 0; i < len(buf); i++ {
		c := buf[i]
		if inStr {
			if esc {
				esc = false
				continue
			}
			switch c {
			case '\\':
				esc = true
			case '"':
				inStr = false
			}
			continue
		}
		switch c {
		case '"':
			inStr = true
		case '{', '[':
			depth++
		case '}', ']':
			depth--
		case ',':
			if depth == 1 { // 只在顶层对象/数组内记录
				safe = i
			}
		}
	}
	return safe
}

type IncJSON struct {
	buf     []byte
	emit    func(path string, val json.RawMessage)
	lastKey map[string]struct{}
}

// Feed 喂入一段增量；返回本次「新完成」的顶层字段数量
func (d *IncJSON) Feed(chunk []byte) (int, error) {
	d.buf = append(d.buf, chunk...)
	if len(d.buf) < 2 {
		return 0, nil
	}
	root := d.buf[0]
	if root != '{' && root != '[' {
		return 0, ErrNotObject
	}
	closeCh := byte('}')
	if root == '[' {
		closeCh = ']'
	}

	safe := lastSafePrefix(d.buf)
	if safe == 0 {
		return 0, nil // 还没有任何完整成员
	}

	// 拼出「截至目前合法的前缀」
	partial := make([]byte, 0, safe+2)
	partial = append(partial, root)
	partial = append(partial, d.buf[1:safe]...)
	partial = append(partial, closeCh)

	var obj map[string]json.RawMessage
	if root == '{' {
		if err := json.Unmarshal(partial, &obj); err != nil {
			return 0, nil // 还不合法：等更多数据（不要报错，不要回滚）
		}
		n := 0
		for k, v := range obj {
			if _, ok := d.lastKey[k]; ok {
				continue
			}
			d.lastKey[k] = struct{}{}
			d.emit("/"+k, v)
			n++
		}
		return n, nil
	}
	// 数组根：只关心长度，交给上层按 index 处理
	return 1, nil
}

// Final 在流结束时调用：做一次完整校验，失败则触发回滚
func (d *IncJSON) Final() (map[string]json.RawMessage, error) {
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(d.buf, &obj); err != nil {
		return nil, err // ★ 整段非法
	}
	return obj, nil
}
```

### 36.6.3 失败回滚：三件事必须做

增量渲染的代价是「你可能渲染了一个错的中间态」。所以必须定义回滚语义：

| 失败形态 | 检测点 | 回滚动作 | 用户看到 |
| --- | --- | --- | --- |
| 流结束但 JSON 非法（截断/语法错） | `Final()` | 发 `correction`（整段替换为「解析失败，原文如下」+ 纯文本） | 内容变成纯文本，不丢信息 |
| 字段已渲染但最终校验失败（如 `amount` 不是数字） | schema 校验 | 发 `patch` 把该字段标记为 `invalid` + 提示 | 该行标红，不整段作废 |
| 字段被「修正」（同一 path 第二次出现） | `emit` 去重 + 值比较 | 发 `patch`（replace），保留 `rev` 递增 | 数字从旧值更新为新值 |
| 模型重试导致内容重来 | `rev` 变化 | 整段 `correction` | 内容重绘 |

```go
// 结构化输出的三段式校验：行级宽松 → 整体严格 → 失败降级
func (s *StructuredStream) Finish() error {
	obj, err := s.inc.Final()
	if err != nil {
		// ① 整段非法：降级为纯文本，绝不丢弃（用户至少能看到内容）
		return s.emitCorrection(s.rawText(), "invalid_json")
	}
	// ② schema 校验（用 JSON Schema 或手写）
	if verr := s.schema.Validate(obj); verr != nil {
		// ③ 部分字段非法：只修那部分，保留其他
		return s.emitFieldPatches(verr.FieldErrors)
	}
	s.markFinal()
	return nil
}
```

> 🩸 **一个真实的反模式**：为了让 JSON 一定合法，有人用「先让模型输出纯文本，再用另一个模型转成 JSON」。这是**双倍成本 + 双倍延迟 + 引入新的不确定性**。正确做法是：用模型原生支持的 structured output / JSON mode（各家都支持，以官方文档为准），再用这里的增量扫描器处理「流式过程中的不完整」。

---

## 36.7 工具调用的分片累积：每个平台都会踩一次的坑

### 36.7.1 数据到底长什么样

OpenAI 风格的流式工具调用，`delta.tool_calls` 是一串**增量对象**：

```jsonc
// chunk 1
{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_a1","type":"function",
  "function":{"name":"query_order","arguments":""}}]}}]}
// chunk 2
{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"ord"}}]}}]}
// chunk 3
{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"er_id\":\"SO-"}}]}}]}
// chunk 4  ← 注意：并行调用的第二个工具，index=1，可能在任意时刻插进来
{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"7781\"}"}},
                                {"index":1,"id":"call_b2","function":{"name":"query_logistics"}}]}}]}
```

Anthropic 风格则是 `content_block_start`（带 `tool_use` 的 name/id）+ 若干 `content_block_delta`（`input_json_delta.partial_json`）+ `content_block_stop`。

### 36.7.2 六个坑，每个都能让你掉 40% 成功率

| # | 坑 | 症状 | 修法 |
| --- | --- | --- | --- |
| 1 | **按「到达顺序 append」而不是按 `index` 聚合** | 一次调用被拆成 N 个工具调用，参数各自残缺 | 必须 `map[index]` 聚合 |
| 2 | **每片都尝试 `json.Unmarshal`** | 首片必然失败 → 误判为「工具参数非法」 | 只在 `finish_reason=tool_calls` 或 `content_block_stop` 时解析 |
| 3 | **把 `id`/`name` 每片都覆盖** | 后续片 `id=""`，把前面的 id 覆盖成空 → 调用找不到 | `if c.ID != "" { acc.ID = c.ID }` |
| 4 | **忽略 `index` 乱序/交错** | 并行两个工具时参数串到对方身上 | 全程按 index 分桶，结束再排序 |
| 5 | **把空串 `arguments:""` 当成结束信号** | 提前解析失败 | 结束信号是 `finish_reason` / `content_block_stop` |
| 6 | **按 rune 计长度做上限** | 多字节中文边界判断错，或上限算错 | 按 byte 计，用 `strings.Builder` 拼字节 |

### 36.7.3 可直接跑的聚合器

```go
// stream/toolagg.go —— 工具调用分片聚合器（含并行调用与校验）
package stream

import (
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
)

var (
	ErrIDDrift    = errors.New("tool_call id changed mid-stream")
	ErrNameDrift  = errors.New("tool name changed mid-stream")
	ErrArgsTooBig = errors.New("tool arguments exceed limit")
	ErrIncomplete = errors.New("tool_call missing name or id")
)

type ToolCallDelta struct {
	Index    int    `json:"index"`
	ID       string `json:"id,omitempty"`
	Type     string `json:"type,omitempty"`
	Function struct {
		Name      string `json:"name,omitempty"`
		Arguments string `json:"arguments,omitempty"`
	} `json:"function"`
}

type acc struct {
	index int
	id    string
	name  string
	args  strings.Builder
	bytes int
}

type ToolCallAggregator struct {
	byIndex map[int]*acc
	maxArgs int // 例如 256KB，防模型吐一个巨大的参数把内存打爆
}

func NewToolCallAggregator(maxArgs int) *ToolCallAggregator {
	return &ToolCallAggregator{byIndex: map[int]*acc{}, maxArgs: maxArgs}
}

// Feed 处理一个 chunk 里的全部 tool_call 分片
func (a *ToolCallAggregator) Feed(deltas []ToolCallDelta) error {
	for _, d := range deltas {
		cur, ok := a.byIndex[d.Index]
		if !ok {
			cur = &acc{index: d.Index}
			a.byIndex[d.Index] = cur
		}
		if d.ID != "" {
			if cur.id != "" && cur.id != d.ID {
				return fmt.Errorf("%w: index=%d %q→%q", ErrIDDrift, d.Index, cur.id, d.ID)
			}
			cur.id = d.ID
		}
		if d.Function.Name != "" {
			if cur.name != "" && cur.name != d.Function.Name {
				return fmt.Errorf("%w: index=%d %q→%q", ErrNameDrift, d.Index, cur.name, d.Function.Name)
			}
			cur.name = d.Function.Name
		}
		if s := d.Function.Arguments; s != "" {
			if cur.bytes+len(s) > a.maxArgs {
				return fmt.Errorf("%w: index=%d >%d", ErrArgsTooBig, d.Index, a.maxArgs)
			}
			cur.args.WriteString(s)
			cur.bytes += len(s)
		}
	}
	return nil
}

type ToolCall struct {
	Index int
	ID    string
	Name  string
	Args  json.RawMessage
}

// Finish 在 finish_reason=="tool_calls" / content_block_stop 时调用
func (a *ToolCallAggregator) Finish() ([]ToolCall, error) {
	idxs := make([]int, 0, len(a.byIndex))
	for i := range a.byIndex {
		idxs = append(idxs, i)
	}
	sort.Ints(idxs) // ★ 按 index 排序，不要用 map 遍历顺序

	out := make([]ToolCall, 0, len(idxs))
	for _, i := range idxs {
		c := a.byIndex[i]
		if c.name == "" || c.id == "" {
			return nil, fmt.Errorf("%w: index=%d name=%q id=%q", ErrIncomplete, i, c.name, c.id)
		}
		raw := strings.TrimSpace(c.args.String())
		if raw == "" {
			raw = "{}" // 无参工具
		}
		if !json.Valid([]byte(raw)) {
			// 不要在这里「猜」或修复：记事件，让上层决定重试还是降级
			return nil, fmt.Errorf("invalid arguments for %s(index=%d): %.120s", c.name, i, raw)
		}
		out = append(out, ToolCall{Index: i, ID: c.id, Name: c.name, Args: json.RawMessage(raw)})
	}
	return out, nil
}
```

配套的单测要点（一定要写成回归用例，因为供应商随时会改分片策略）：

```
T1: 单工具，参数被切成 12 片，其中一片在 JSON 字符串中间（"北 / 京"）→ 应拼出正确 JSON
T2: 单工具，参数被切成「\"ord」/「er」这种 key 中间 → 应拼出正确 JSON
T3: 两个工具并行，index 交错到达且乱序（1,0,1,0,1）→ 应得到两个独立调用
T4: 首片 id 有值，后续片 id 为 "" → id 必须保留
T5: 最后一片 arguments 为 "" → 不能当作结束信号
T6: 参数超过 maxArgs → 必须报错而不是 OOM
T7: 工具名在中途变化（供应商 bug）→ 必须报错（ErrNameDrift），不能静默取最后一个
```

---

## 36.8 中途打断与恢复

### 36.8.1 两种「打断」

| 类型 | 触发 | 服务端动作 | 客户端动作 |
| --- | --- | --- | --- |
| **用户主动停止** | 点「停止生成」 | 发 `cancel` 到上游（协作式取消）→ 停生成 → 落 `status:cancelled` → **按已生成 token 结算** | 保留已渲染内容 + 标记「已停止」 |
| **断线** | 网络断/SDK 崩溃/切后台 | 检测写失败 → 立即取消上游（**不要继续烧 token**）→ 保留回放缓冲 60s | 重连带 `Last-Event-ID` → 补发缺失事件 |

### 36.8.2 断点续传：`Last-Event-ID` + 环形缓冲

```go
// stream/sse.go —— 带 Last-Event-ID 回放与心跳的 SSE 服务端
func (h *Handler) ServeSSE(w http.ResponseWriter, r *http.Request) {
	f, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming unsupported", http.StatusInternalServerError)
		return
	}
	hdr := w.Header()
	hdr.Set("Content-Type", "text/event-stream; charset=utf-8")
	hdr.Set("Cache-Control", "no-cache, no-transform") // no-transform：禁止 CDN 压缩改写
	hdr.Set("Connection", "keep-alive")
	hdr.Set("X-Accel-Buffering", "no")                 // nginx：关缓冲（§36.10）
	w.WriteHeader(http.StatusOK)
	f.Flush() // ★ 立刻 flush 头，否则客户端拿不到响应就超时

	runID := r.PathValue("runID")
	stream := h.hub.Attach(runID, lastEventID(r)) // 从环形缓冲补发
	defer h.hub.Detach(runID, stream)

	// 心跳：防止反代/LB 的 idle timeout（Cloudflare 100s、ALB 默认 60s）
	hb := time.NewTicker(15 * time.Second)
	defer hb.Stop()

	for {
		select {
		case <-r.Context().Done():
			// 客户端断了：立刻取消上游，别继续烧 token
			h.engine.Cancel(runID, CancelReasonClientGone)
			return
		case <-hb.C:
			fmt.Fprint(w, ": keepalive\n\n") // 注释行，SSE 规范允许
			f.Flush()
		case ev, ok := <-stream.C:
			if !ok {
				fmt.Fprint(w, "event: end\ndata: {\"seq\":"+itoa(stream.LastSeq())+"}\n\n")
				f.Flush()
				return
			}
			fmt.Fprintf(w, "id: %d\nevent: %s\ndata: %s\n\n", ev.Seq, ev.Type, ev.Data)
			f.Flush() // ★ 每帧必须 flush，否则就是在做合批（可能不是你要的）
		}
	}
}
```

三条关键点：

1. **`X-Accel-Buffering: no`** 比改 nginx 配置更可靠（不需要运维配合，且能穿透多层反代）。
2. **心跳用 SSE 注释行 `: keepalive`**，不要发业务事件——注释行不触发 `onmessage`，客户端无感。
3. **回放缓冲要按「事件数 + 时间」双限**（如最近 2,000 条 or 60 秒）。只按条数，长文本流会撑爆内存；只按时间，短时间大量事件会撑爆带宽。

```go
// 环形回放缓冲：双限（条数 + TTL），按 seq 索引
type replayRing struct {
	events []Event // 环形
	head   int
	n      int
	baseTS time.Time
	ttl    time.Duration
}

// Since 返回 seq > lastID 的所有事件；若 lastID 太旧（已被淘汰）返回 ErrGapTooLarge
func (r *replayRing) Since(lastID int64) ([]Event, error) {
	if r.n == 0 {
		return nil, nil
	}
	oldest := r.events[(r.head-r.n+r.cap())%r.cap()]
	if lastID < oldest.Seq-1 {
		return nil, ErrGapTooLarge // 客户端必须做「全量重取」，不能静默补
	}
	// ... 顺序取出
}
```

> ⚠️ **`ErrGapTooLarge` 的处理是最容易漏的**：客户端带着一个很旧的 `Last-Event-ID` 重连，而你只能补最近 60 秒。正确做法是返回一个 `reset` 事件，让客户端**整体重新拉取当前 Run 的快照**（而不是拼接一个残缺历史）。拼出来的残缺历史会让前端渲染出「重复的半句话」。

### 36.8.3 打断后的「半句话」

用户点停止时，最后一批可能是 `"您的订单已经"`——语义上是断的。两个处理方式：

- **加一个收尾标记**：`status: cancelled` + 前端追加 `（已停止）`。
- **补一个最小可读结尾**：如果是纯文本，可以用模板补「…」；如果是结构化输出，必须发 `correction` 把不完整的字段标记为 `truncated`。

**绝不能做的**：静默停止，让用户看到一句断掉的话——他会以为系统坏了。

---

## 36.9 多路复用下的顺序保证

一个 HTTP/2 连接上可能跑 3 条流（一个页面同时有主回答 + 两个子任务卡片）；一个 WebSocket 上可能跑 20 条流。这时候顺序保证靠三件事：

| 机制 | 做法 | 漏了会怎样 |
| --- | --- | --- |
| **每流独立 `seq`** | `seq` 是「Run 内」单调，不是「连接内」单调 | 共享计数器 → 一条流卡住，所有流的 seq 都跳号 → 客户端全部报 gap |
| **帧内带 `run_id`** | 每个事件显式带归属 | 客户端要靠「最近一次 attach」猜，多路一定串台 |
| **发送串行化** | 同一连接的写操作由一个 goroutine 独占（channel 排队） | 并发 `Write` 到同一个 `http.ResponseWriter` 会**交错出半帧**，JSON 直接坏 |

```go
// 同一个连接的写者必须唯一：所有流写进同一个 channel，单 goroutine 输出
type connWriter struct {
	ch   chan []byte // 有界，见 §36.11 背压
	done chan struct{}
}

func (c *connWriter) loop(w io.Writer) {
	bw := bufio.NewWriterSize(w, 16*1024)
	for {
		select {
		case b := <-c.ch:
			bw.Write(b)
			// 批量取走已就绪的帧，减少 flush 次数（但不要跨流合并成一帧）
			for i := 0; i < 32; i++ {
				select {
				case b2 := <-c.ch:
					bw.Write(b2)
				default:
					goto flush
				}
			}
		flush:
			bw.Flush()
		case <-c.done:
			return
		}
	}
}
```

> 💡 **一个容易被忽略的顺序问题**：`tool` 事件的 `result` 和后续 `delta` 的顺序。如果工具结果先到、模型接着基于结果生成，事件顺序必须是 `tool(result)` → `delta`。**在多路复用下，这两类事件可能来自不同的 goroutine**，必须由同一个「事件序列化器」统一分配 seq，不能各自分配。

---

## 36.10 流式与计费/审计的耦合

### 36.10.1 「先发后算」的三层风险

流式的本质矛盾：**你在还不知道花了多少钱的时候，就已经把内容给出去了。**

| 层 | 风险 | 量级 | 处置 |
| --- | --- | --- | --- |
| **用量未知** | 首 token 发出时，output token 数未知 | 每次 Run 差异可达 10 倍 | **预扣按 `max_tokens` 上限**，结算按最终 `usage` |
| **断连漏账** | 客户端断开 → 没有 `[DONE]` → 结算不跑 | Aurora 曾漏 9,000 次/天（≈18%） | 服务端在「取消/断连」时也要结算；上游 usage 从响应收尾事件取 |
| **超发** | 预扣不够，实际超支 | 大输出 + 多轮 | 预扣 + 中途「余额不足」硬停（发 `error: quota_exceeded`） |

```go
// billing/stream.go —— 流式计费：预扣 → 中途校验 → 最终结算
func (b *Billing) Stream(ctx context.Context, runID string, req Request) error {
	est := Amount{
		Tokens: req.MaxTokens + req.EstimatedInputTokens,
		Micro:  b.price(req.Model, req.MaxTokens+req.EstimatedInputTokens),
	}
	lease, err := b.budget.Reserve(ctx, runID, est) // 第 33 章的预算令牌
	if err != nil {
		return ErrQuotaExceeded
	}

	var outTokens int64
	defer func() {
		// ★ 无论正常结束、报错、还是客户端断开，都必须结算
		actual := Amount{Tokens: outTokens, Micro: b.price(req.Model, outTokens)}
		_ = b.budget.Commit(context.WithoutCancel(ctx), lease, actual)
		b.ledger.Append(runID, actual, LedgerMeta{
			Model: req.Model, Streamed: true, Completed: outTokens > 0,
		})
	}()

	for {
		select {
		case <-ctx.Done():
			return ctx.Err() // defer 里结算已生成部分
		case chunk, ok := <-upstream:
			if !ok {
				return nil
			}
			if chunk.Usage != nil { // OpenAI: stream_options.include_usage=true
				outTokens = chunk.Usage.CompletionTokens
			} else {
				outTokens += int64(estimateTokens(chunk.Delta))
			}
			// 中途余额校验：每 1,000 token 检一次，超了就硬停
			if outTokens%1000 == 0 && !b.budget.HasHeadroom(runID) {
				return ErrQuotaExceeded
			}
			emit(chunk)
		}
	}
}
```

三个细节：

1. **`context.WithoutCancel(ctx)`**。断连时 `ctx` 已取消，但结算必须执行——这是 Go 1.21+ 的正确工具（等价于 Java 里用独立线程池做收尾）。
2. **`stream_options: {"include_usage": true}`**（OpenAI 风格）会让最后一个 chunk 带 usage；Anthropic 在 `message_delta` 里带 `usage`。**一定要打开**，否则你只能估算，误差 3~8%。
3. **估算兜底必须有**。有的供应商在某些错误路径下不返回 usage，你要用 tokenizer 估算，并在账本上标记 `estimated: true`——**标记比准确更重要**，因为对账时要能区分「精确」和「估算」。

### 36.10.2 审计与流式的耦合

逐 delta 做内容审核 = 1200 次/流 × 8,000 流 = **960 万次/秒**，不可能。所以审计必须是**抽样 + 窗口 + 事后**：

| 策略 | 做法 | 覆盖 | 成本 |
| --- | --- | --- | --- |
| 前置窗口 | 前 200 字做一次审核（最危险的开头） | 100% 流的开头 | 低 |
| 句子级抽样 | 每 5 句抽 1 句 | 20% 内容 | 中 |
| 流结束全量 | Run 结束后对完整输出审一次 | 100%，但滞后 | 中 |
| 风险触发 | 命中关键词/风险分高 → 转全量逐句 | < 2% 的流 | 低 |

完整的撤回机制（`correction` 的语义、双通道草稿流）在第 37 章展开。这里只需要记住一条：**流式必须预留「更正」这个事件类型，否则你上线后想加撤回功能，要改前端协议。**

---

## 36.11 慢消费者与背压

### 36.11.1 内存账

```
1 条流的有界 buffer = 64KB（例如 800 个事件）
慢消费者（4G 网络、后台标签页）不读 → buffer 填满
如果不丢弃：8,000 条流 × 200KB = 1.6GB，而且会继续涨
更糟的是 nginx 也会为每个连接缓冲 → 双份
```

### 36.11.2 四级背压策略

| 水位 | 动作 | 用户感知 |
| --- | --- | --- |
| < 50% | 正常发送 | 正常 |
| 50%~80% | **合并批次**（把未发的多个 delta 合成一个） | 略微成块，无感 |
| 80%~95% | **丢弃中间 delta**，只保留最后一个「累积文本」和最终 `correction` | 内容跳跃，但最终一致 |
| > 95% 持续 10s | 发 `error: slow_consumer` + 关闭连接 | 明确失败 + 重试入口 |

```go
// 丢弃中间态：只保留「最新累积文本」，永远可被下一个覆盖
func (q *sendQueue) push(ev Event) {
	select {
	case q.ch <- ev:
		return
	default:
	}
	// 满了：如果是 delta，用「替换最后一个 delta」而不是排队
	q.mu.Lock()
	defer q.mu.Unlock()
	if ev.Type == "delta" && len(q.pending)>0 && q.pending[len(q.pending)-1].Type == "delta" {
		q.pending[len(q.pending)-1].Text += ev.Text // 合并，不增长队列
	} else if ev.Type == "delta" {
		q.dropped += len(ev.Text)
		q.droppedSince = time.Now()
	}
}
```

> 🩸 **「丢弃中间 delta」必须配合一个约定**：前端收到的 delta 是**增量**，一旦丢了就永远少那几个字。所以丢弃策略只在你「保证最后发一个 `correction` 覆盖整段」时才安全。**如果你没有 correction 事件，就别丢 delta——宁可断开。**

### 36.11.3 上游背压：别把慢消费者传染给模型

一个慢消费者如果不取消上游，模型会继续生成——**你付钱，用户看不到**。所以：**发送队列满且超过 5 秒 → 取消上游**。

```
慢消费者
  → 发送队列满
  → 5 秒内无法排空
  → Cancel(upstream)  ← 立刻停，省钱
  → 保留已生成内容，等待重连补发（环形缓冲）
  → 重连后：从 Last-Event-ID 补发 + 继续生成（如果上游支持 resume，否则重新生成剩余部分）
```

---

## 36.12 CDN 与反代对流式的破坏

这是「本地跑得好好的，上线就一卡一卡」的头号原因。

| 症状 | 根因 | 修法 |
| --- | --- | --- |
| 一次性吐出全部内容（假流式） | nginx `proxy_buffering on`（默认） | `proxy_buffering off;` + 响应头 `X-Accel-Buffering: no` |
| 攒到 4KB 才发 | `proxy_buffer_size` / gzip 缓冲 | 关 gzip（SSE 不需要压缩）或 `gzip_flush` |
| 60 秒后连接断 | `proxy_read_timeout` 默认 60s | 设为 3600s + 15s 心跳 |
| 100 秒后断（Cloudflare） | CF 的 idle timeout（对 SSE 约 100s） | 心跳间隔 < 30s；或走独立子域绕开代理 |
| 首字延迟 +500ms | TLS 握手 + CDN 回源 | 连接预热、HTTP/2 多路复用、就近回源 |
| 内容被「优化」 | CDN 的 HTML/JS 自动优化、压缩 | `Cache-Control: no-transform` |
| 半帧乱码 | 多层反代并发写同一连接 / 缓冲截断 | 单写者 + 关闭所有中间层缓冲 |
| HTTP/2 流控卡住 | 流控窗口耗尽（客户端不读） | 监控 `SETTINGS_INITIAL_WINDOW_SIZE`；这其实是背压，别当 bug |
| 移动端后台切换断 | 系统挂起 socket | 客户端指数退避重连 + `Last-Event-ID` |

```nginx
# nginx.conf —— 流式端点专用 location
location /api/v1/runs/ {
    proxy_pass http://agent_server;
    proxy_http_version 1.1;

    # ★ 三件套：关缓冲、清 Connection、关分块重打包
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_set_header Connection "";

    # 长连接：要大于最长 Run（30min 长任务）
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;

    # SSE 不需要压缩；开着会让 nginx 攒够压缩块才 flush
    gzip off;

    # 关闭可能改写响应体的模块
    proxy_set_header Accept-Encoding "";
    add_header X-Accel-Buffering no always;
    add_header Cache-Control "no-cache, no-transform" always;
}
```

> ⚠️ **HTTP/1.1 的 6 连接上限**：浏览器对同域名最多 6 个并发连接。一个页面如果同时开 3 条流 + 若干普通请求，很容易排满，导致「新请求一直 pending」。**用 HTTP/2（多路复用）或者把流式端点放到独立子域**。

---

## 36.13 Java / 分布式经验对照

| 你的 Java 经验 | 流式里对应 | 会咬人的地方 |
| --- | --- | --- |
| `SseEmitter`（Spring MVC） | SSE 输出 | 默认超时（`spring.mvc.async.request-timeout`）会静默结束流；必须显式设长 + 心跳 |
| WebFlux `Flux<ServerSentEvent>` | 响应式流 | 背压是内建的（`onBackpressureBuffer/Drop`），**这是 WebFlux 相比 MVC 的真优势**；但 `Flux` 的 `concatMap` 用错会破坏顺序 |
| `CompletableFuture` 编排 | 工具调用聚合 | 别把「按 index 聚合」写成 `thenCombine` 链——并行 index 是动态的，用 map |
| Servlet 3.1 async + `AsyncContext` | 长连接 | 线程释放了，但 `AsyncContext` 有超时，且 `complete()` 后不能再写 |
| `OutputStream.flush()` | 每帧 flush | Java 的 `PrintWriter` 会**吞 IOException**（`checkError()` 才是真相），断连你发现不了 |
| Kafka 消费者 offset | `seq` / `Last-Event-ID` | 同构：客户端提交 offset，服务端从 offset 之后补发；缺口要显式报错而不是跳过 |
| 分库分表的「有序 ID」 | 事件 seq | 必须**每 Run 一个序列**，不能用全局自增（多路复用下会跳号） |
| 虚拟线程（Java 21） | 每流一个阻塞写循环 | 很合适（阻塞写在虚拟线程上代价低）；但注意 `synchronized` 块会 pin 住载体线程 |

> 🩸 **最容易犯的框架错**：用 `@ResponseBody` 返回一个 `Flux` 但没设 `produces = MediaType.TEXT_EVENT_STREAM_VALUE`。Spring 会按 JSON 序列化整个 Flux（等全部完成再返回），你得到的是**假流式**——本地测不出来（数据量小），上线后 TTFT 等于总时长。

---

## 36.14 开源坐标

| 能力 | Vercel AI SDK | AG-UI | 其他 | 缺什么 |
| --- | --- | --- | --- | --- |
| 流协议 | data stream protocol（自定义帧） | 事件类型标准化（run/message/tool 生命周期） | 原生 SSE | Vercel 抽象好但绑前端生态；AG-UI 面向 Agent-UI 交互 |
| 增量渲染 | `useChat` / `useCompletion` 自动管理 | 事件驱动的 UI 状态机 | — | 都缺「correction（撤回）」的一等支持 |
| 工具调用聚合 | 内部处理了分片 | 有 tool call 事件 | — | **自己写服务端时仍要自己聚合**（§36.7） |
| 结构化输出 | `streamObject`（配合 zod schema） | — | Instructor、Outlines | 缺「失败回滚」的语义（大多只做校验，不做 correction） |
| 断点续传 | 部分支持（`resumable streams`） | — | 原生 SSE 的 `Last-Event-ID` | 环形缓冲与 gap 处理要自己写 |
| 背压 | 无（浏览器侧） | — | WebFlux/RxJS 原生 | 服务端的四级背压策略要自己定 |

> **一句话**：**前端 SDK 把「消费流」做得很好，把「生产一条可靠的流」全留给你了。** 而生产端才是出事的地方——TTFT、分片、背压、计费、撤回，全在服务端。

---

## 36.15 事故清单

- [ ] **事故 A：工具调用成功率从 98% 掉到 61%** —— 现象：`cannot unmarshal`。→ 根因：每片 `arguments` 都尝试解析。→ 兜底：只在 `finish_reason` 时解析 + §36.7.2 的 7 条回归用例。
- [ ] **事故 B：假流式（127 秒静默）** —— 现象：用户等到放弃。→ 根因：nginx `proxy_buffering` 默认开。→ 兜底：`X-Accel-Buffering: no` + `proxy_buffering off`。
- [ ] **事故 C：断连漏账 18%** —— 现象：上游 5.1 万次，账上 4.2 万次。→ 根因：只在 `[DONE]` 后结算。→ 兜底：`defer` 结算 + `context.WithoutCancel` + 打开 `include_usage`。
- [ ] **事故 D：多路复用串台** —— 现象：A 会话的回答出现在 B 会话的卡片里。→ 根因：连接级共享 seq + 帧里没带 `run_id`。→ 兜底：每 Run 独立 seq + 帧内显式归属。
- [ ] **事故 E：并发写坏帧** —— 现象：偶发 JSON 解析错误，抓包看到半帧。→ 根因：多个 goroutine 并发写 `ResponseWriter`。→ 兜底：单写者 goroutine + channel。
- [ ] **事故 F：慢消费者 OOM** —— 现象：Pod 内存 8GB 打满后 OOMKilled。→ 根因：每条流的 buffer 无界。→ 兜底：有界队列 + 四级背压 + 超时断开。
- [ ] **事故 G：增量 JSON 渲染出错误数字** —— 现象：用户看到金额「¥1,2」然后变成「¥1,280」再变成「¥12,800」。→ 根因：把不完整字符串直接渲染。→ 兜底：只在「顶层成员完整」时 emit + `rev` 版本 + 最终校验失败发 `correction`。
- [ ] **事故 H：重连后内容重复** —— 现象：前端出现两遍同样的段落。→ 根因：回放缓冲淘汰了旧事件，客户端拼接了残缺历史。→ 兜底：`ErrGapTooLarge` → `reset` 事件 → 全量重取快照。

## 36.16 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| TTFT 构成 | 排队 + prefill 占 66%；P95 是 P50 的 4.8 倍，尾部就是排队问题 |
| 假首字 | 先发 `status` 事件，感知 TTFT 从 900ms 降到 40ms，成本 0 |
| 前处理占比 | 27ms / 800ms = 4.5%，**不要为省 10ms 砍掉它** |
| 合批 | 50~80ms 是甜点；首批不等待；遇句子结尾立刻 flush |
| 事件类型 | 六种够用：`delta/patch/tool/status/error/correction`；**`correction` 是撤回的地基** |
| seq | Run 内单调、不跳号、帧内带归属；不能共享计数器 |
| 增量 JSON | 找「最后一个顶层逗号」→ 补闭合 → 标准 `Unmarshal`；不要每片都解析 |
| 工具分片 | **按 `index` 聚合**、`id/name` 只取非空、结束看 `finish_reason`、按 byte 计长 |
| 打断 | 断连即取消上游（别烧钱）；保留 60s 回放缓冲；`ErrGapTooLarge` 要 `reset` 而非拼接 |
| 顺序 | 单连接单写者；事件由统一序列化器分配 seq |
| 计费 | 按 `max_tokens` 预扣 → 中途校验 → `defer` 无条件结算；`include_usage` 必开 |
| 背压 | 四级水位：正常 → 合并 → 丢中间态（需 correction）→ 断开；超 5s 取消上游 |
| 反代 | `proxy_buffering off` + `X-Accel-Buffering: no` + 心跳 15s + gzip off |
| 一句话 | **流式不是「转发 chunk」，是一条有状态、有账、有顺序、还要能撤回的事件管道。** |

## 36.17 自检题

1. **为什么「每收到一个 `arguments` 片段就 `json.Unmarshal`」必然失败？正确的解析时机在哪？**
   > 要点：`arguments` 是一个**字符串的增量拼接**，任意前缀几乎都不是合法 JSON（可能切在 key 中间、字符串中间、甚至多字节字符中间）。正确时机：① OpenAI 风格在 `finish_reason == "tool_calls"`（或 `[DONE]`）时一次性解析；② Anthropic 风格在 `content_block_stop` 时用累积的 `partial_json` 解析。中途若要做 UI 预览，用「找最后一个顶层逗号 + 补闭合」的增量扫描器（§36.6.2），而不是直接 `Unmarshal`。

2. **流式场景下，为什么计费不能等 `[DONE]`？**
   > 要点：客户端断开/网络中断时 `[DONE]` 永远不会到，但上游已经生成了（你已经付钱）。Aurora 曾因此漏账 18%。正确做法：① 调模型前按 `max_tokens` **预扣**；② 用 `defer` + `context.WithoutCancel` 保证任何退出路径都结算；③ 打开 `stream_options.include_usage` 拿精确 usage，拿不到就估算并标记 `estimated: true`；④ 中途每 N 千 token 校验余额，不够就硬停。

3. **用户点「停止生成」之后，服务端要做几件事？少做哪件会亏钱？**
   > 要点：四件事——① 把 `cancel` 传播到上游模型（**少这件就亏钱**：模型继续生成，你继续付费，用户看不到）；② 停发送、发 `status: cancelled` 收尾（避免「半句话」）；③ 按**已生成** token 结算（不能按 0，也不能按 max）；④ 保留回放缓冲一段时间，允许用户重连看已生成内容。另外要区分「主动停止」和「断线」：断线也要取消上游，且要留有 `Last-Event-ID` 重连的能力。

## 36.18 延伸阅读

- 上一章：[35. 前处理层：把脏输入挡在花 token 之前](35-pre-processing.md)
- 下一章：[37. 输出审计与「撤回已说出去的话」](37-output-audit-retraction.md)
- 总纲：[0-summary.md](0-summary.md)（§1「终端行缓冲与 flush」一行）
- 相关：[7. 流式传输：SSE / WebSocket / gRPC / 长轮询](7-streaming-transport.md)（传输选型）
- 相关：[8. Run API 设计](8-run-api-design.md)（取消语义与断点回放）
- 相关：[27. 计费的可靠性](27-billing-reliability.md)（预扣-结算-对账）
- 相关：[33. 多 Agent 协作](33-multi-agent-collaboration.md)（预算令牌 Reserve/Commit）
- 相关：[37. 输出审计与撤回](37-output-audit-retraction.md)（`correction` 事件的完整语义）
- 相关：[39. 可观测层](39-observability.md)（TTFT / 帧率 / 背压指标）
- 外部参考：WHATWG HTML 规范的 Server-Sent Events 一节（`text/event-stream`、`id`/`event`/`data`/`retry`、`Last-Event-ID`）、OpenAI Chat Completions 流式工具调用（`delta.tool_calls[].index`、`stream_options.include_usage`）、Anthropic Messages 流式事件（`content_block_start` / `content_block_delta` / `input_json_delta` / `content_block_stop` / `message_delta`），以官方文档最新版为准
