# 15. 上下文预算：token 会计与分配策略

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 上下文工程（Context Engineering） ｜ **编号**：15 / 52
> **本章定位**：上下文窗口是你唯一真正稀缺的资源——它同时决定质量、成本和能不能跑完。本章把它当内存来分配：七份预算怎么切、超了按什么顺序砍、以及「一次调用到底花了多少 token」这四个互相打架的口径。
> **读法**：§15.3 的预算表和 §15.4 的降级阶梯是可以直接抄进设计文档的；§15.5 的四种口径是所有计费与限流的共同基础，**看不懂这一节，第 23 章和第 27 章你都读不下去**。

---

## 15.1 引子：一份 120 页的 PDF，和从 0.06 美元变成 0.43 美元的一次调用

客服 Agent 上线第 4 个月，销售签了一个大客户。第一天，他们的用户上传了一份 120 页的产品手册，问「我们的场景适合哪几个型号？」

你的链路是这么走的：

```
PDF → 文本抽取 → 120 页 ≈ 6.2 万字 ≈ 8.1 万 token
  → 切分成 380 个 chunk
  → 检索 top-40 chunk（每块 800 token）= 3.2 万 token
  → + 最近 50 轮历史（上一轮就在聊这个手册）= 4.5 万 token
  → + 长期记忆 1.0 万 token
  → + system 2.5k + tools 7.5k + persona 2k = 1.2 万 token
  ────────────────────────────────────────────────
  合计 14.2 万 token  >  128k 窗口   →   400 报错
```

报错本身还好，你加了截断逻辑。真正的问题在后面：

| 版本 | 输入 token | 单次成本 | 备注 |
| --- | --- | --- | --- |
| 上线初期（短对话） | 8,000 | $0.024 | 一切正常 |
| 加检索、加记忆之后 | 32,000 | $0.096 | 翻 4 倍，没人注意 |
| 这份 PDF 进来 | 142,000 | **$0.426** | 单次调用，翻 17.8 倍 |
| 同一会话第 8 轮 | 152,000 | $0.456 | **每轮都在重发整份手册** |

按这个客户一天 2,000 次对话算：`2,000 × 8 × $0.45 = $7,200/天`。**一个客户，一天七千美金。** 而你合同里给他的是每月 $3,000 的套餐。

> 🩸 这就是「上下文预算」要解决的问题：**它不是「别超窗口」这么简单，而是「在窗口之内，每一千 token 都要有明确的主人」。** 没有预算表，你的上下文会像没有人管的堆内存一样，一路涨到 OOM（超窗）或者一路涨到破产（成本）。

---

## 15.2 把窗口当内存：三段式划分

先建立最基础的心智模型。把上下文窗口当成一块线性地址空间：

```
window = 128,000
├── output_reserve   8,000   ← 必须留给模型生成，不能借
├── slack           12,000   ← 10%，吸收 tokenizer 估算误差 + 模板拼接开销
└── allocatable    108,000   ← 七份预算争抢的部分
```

**为什么 `output_reserve` 必须硬留？** 大多数 API 的 `max_tokens` 是「输入 + 输出 ≤ 窗口」的约束。你不留，模型会在生成到一半时被截断，返回 `finish_reason=length`，用户看到半句话——而且你已经为这些 token 付过钱了（第 36 章）。

**为什么要有 10% 的 slack？** 因为你的估算永远不准：

| 误差来源 | 典型偏差 |
| --- | --- |
| 本地 tokenizer 与线上模型不完全一致 | ±2~5% |
| 模板里的条件分支、空白、格式符 | +300~800 token（固定开销） |
| 工具返回的 JSON 美化（缩进/换行） | +15~30% 于原始文本 |
| 多模态（图片按 tile 计 token） | 单图 500~1,500 token，难精确估算 |
| 模型侧 chat template 自动加的 role 标记 | 每轮 +4~8 token，100 轮就是 800 |

10% 的 slack 在 128k 窗口上是 12k token——**听起来很多，但一份带缩进的工具返回 JSON 就能吃掉一半**。如果你的系统里有大量结构化数据，把 slack 提到 15%。

---

## 15.3 七份预算：谁该占多少

这是 Aurora 客服 Agent 的生产配置（128k 窗口）。**这张表是本章的核心交付物**，你把它换成自己的场景就能用：

| # | 分区 | 预算 token | 占窗口 | 内容 | 可压缩 | 降级级别 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `system` | 2,500 | 2.0% | 系统指令、输出契约、安全策略、格式要求 | ❌ **永不** | — |
| 2 | `tools` | 7,500 | 5.9% | 12 个工具的 JSON Schema + 调用约束 | ✅ 瘦身 | L4 |
| 3 | `persona` | 2,000 | 1.6% | 角色卡：身份、能力边界、语气（第 17 章） | ⚠️ 谨慎 | — |
| 4 | `memory` | 10,000 | 7.8% | 长期记忆事实、用户偏好、历史结论 | ✅ 裁剪 | L3 |
| 5 | `history` | 28,000 | 21.9% | 最近 6 轮原文 + 更早的摘要 | ✅ 压缩 | L2 |
| 6 | `retrieval` | 58,000 | 45.3% | RAG chunk、文档片段、表格 | ✅ 裁剪 | L1 |
| 7 | `output` | 8,000 | 6.2% | 生成预留（`max_tokens`） | ❌ **永不** | — |
| — | `slack` | 12,000 | 9.4% | 估算误差 + 模板开销 | ❌ | — |
| | **合计** | **128,000** | **100%** | | | |

**三条分配原则，请务必内化：**

1. **`system` 和 `output` 是契约，不是预算。** 它们参与「占用」，但不参与「竞争」。任何时候你都不能为了塞更多检索结果去砍系统指令——砍了模型就不知道该输出什么格式，下游解析全崩（第 38 章）。
2. **`retrieval` 是最大的弹性池。** 它占 45%，因为它是最容易「多塞一点」也最容易「少塞一点」的部分。**所有超预算的压力，第一顺位由它吸收。**
3. **`history` 的预算应该随时长增长而衰减，不是固定值。** 一个 3 轮的会话和一个 80 轮的会话，history 的实际占用完全不同——所以 history 的 28k 是「上限」而不是「配额」。

### 不同场景的 profile 差异

同一套代码，三份 profile：

| 场景 | window | system | tools | persona | memory | history | retrieval | output |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 客服（长上下文） | 128k | 2.5k | 7.5k | 2.0k | 10k | 28k | 58k | 8k |
| 客服（标准，小模型） | 32k | 2.0k | 4.0k | 1.5k | 3k | 8k | 11k | 2k |
| 编码 Agent | 200k | 3.0k | 12k | 1.0k | 4k | 60k | 110k | 8k |
| 数据分析（SQL 生成） | 64k | 2.5k | 9k | 0.5k | 2k | 18k | 28k | 3k |

注意编码 Agent 的 `retrieval` 是 110k——因为它的「检索」是代码文件，必须给足，否则改错文件。而它的 `memory` 只有 4k，因为编码会话基本不需要跨会话记忆。**profile 不是配着玩的，它是产品形态的数字化表达。**

### 成本账：为什么预算表能省钱

```
精简 profile（32k 满载）：32,000 × $3/M = $0.096 / 次调用
长上下文 profile（120k 满载）：120,000 × $3/M = $0.360 / 次调用
差额：$0.264 / 次调用

Aurora 日均 8.6M Run，其中走到「长上下文 profile」的占 15% = 1.29M Run
每 Run 平均 6 次调用 → 每天多花 1.29M × 6 × $0.264 = $2,043,000
```

**每天两百万美元，仅仅因为「没有按场景分 profile，所有人都给了最大窗口」。** 这就是为什么预算表必须在**路由之前**就决定——先选 profile，再装配上下文，而不是反过来。

---

## 15.4 结构图：装配器与它的六个闸门

```
请求 ──▶ ① 场景识别（意图/租户套餐/历史长度）
            │
            ▼
      ┌───────────────────────────────────────────────┐
      │ ② 选 profile（32k / 64k / 128k / 200k）        │
      │    → 得到 allocatable = window − output − slack│
      └───────────────────┬───────────────────────────┘
                          ▼
      ┌───────────────────────────────────────────────┐
      │ ③ 各分区「申请」预算（retrieval 要 40 chunk）   │
      └───────────────────┬───────────────────────────┘
                          ▼
              ┌───────────────────────┐
              │ Σ 申请 ≤ allocatable ?│
              └───┬───────────────┬───┘
                  │ 是            │ 否
                  │               ▼
                  │    ┌──────────────────────────────────┐
                  │    │ ④ 按降级阶梯依次砍：              │
                  │    │   L0 无损 → L1 retrieval →        │
                  │    │   L2 history → L3 memory →        │
                  │    │   L4 tools → L5 output → L6 换模型 │
                  │    └──────────────┬───────────────────┘
                  │                   │ 每砍一级重算 Σ
                  │                   └──────┐
                  ▼                          ▼
      ┌───────────────────────────────────────────────┐
      │ ⑤ 按「缓存友好顺序」拼接（稳定在前，易变在后）  │
      │   system → tools → persona → memory →          │
      │   retrieval → history → 当前输入               │
      └───────────────────┬───────────────────────────┘
                          ▼
      ┌───────────────────────────────────────────────┐
      │ ⑥ 计量：估算口径 E → 发请求 → 请求口径 R →     │
      │   计费口径 B（含缓存折扣）→ 审计口径 A          │
      └───────────────────────────────────────────────┘
```

**⑤ 那个顺序不是随便排的。** 前缀缓存（prompt cache）按「从第一个 token 起的最长公共前缀」计费折扣。你把 `retrieval` 放在 `system` 前面，每次请求的前缀都不一样，**缓存命中率直接归零**（第 24 章、第 18 章）。

一个实测的量级：稳定前缀 20k token、命中率 92%、缓存读价格 0.1× 时，每 100 万次调用省 `20000 × 1e6 × ($3 − $0.3)/1e6 = $54,000`。而重排一次上下文顺序的成本是零。**这是全书投入产出比最高的一个改动。**

---

## 15.5 关键设计

### 15.5.1 预算 profile 与降级阶梯

```yaml
# context-profiles.yaml
version: 3
profiles:
  cs-long:
    window: 128000
    output_reserve: 8000
    slack_ratio: 0.10          # → allocatable = (128000-8000)*0.9 = 108000
    sections:
      system:    { budget: 2500,  compressible: false }
      tools:     { budget: 7500,  compressible: true, max_trim_ratio: 0.5 }
      persona:   { budget: 2000,  compressible: false }
      memory:    { budget: 10000, compressible: true, min_keep: 5 }   # 至少留 5 条事实
      history:   { budget: 28000, compressible: true, keep_recent_turns: 6 }
      retrieval: { budget: 58000, compressible: true, min_keep: 8, score_floor: 0.35 }
    # 降级顺序：从左到右，砍到够为止
    degrade_order: [retrieval, history, memory, tools]
    # system / persona / output 永不进入降级链

  cs-standard:
    window: 32000
    output_reserve: 2000
    slack_ratio: 0.10
    sections:
      system:    { budget: 2000,  compressible: false }
      tools:     { budget: 4000,  compressible: true, max_trim_ratio: 0.5 }
      persona:   { budget: 1500,  compressible: false }
      memory:    { budget: 3000,  compressible: true, min_keep: 3 }
      history:   { budget: 8000,  compressible: true, keep_recent_turns: 4 }
      retrieval: { budget: 11000, compressible: true, min_keep: 4, score_floor: 0.45 }
    degrade_order: [retrieval, history, memory, tools]
```

### 15.5.2 降级阶梯：砍谁、砍多少、质量掉多少

这是本章最需要「用数字说话」的部分。下表的质量影响来自一个 500 条的客服评测集（**你应该跑自己的评测集，数字会不同，但顺序通常一致**）：

| 级别 | 动作 | 省 token | 质量影响（Δ 任务成功率） | 可逆 | 何时用 |
| --- | --- | --- | --- | --- | --- |
| **L0** | 去重 chunk、折叠空/错工具结果、截断超长 JSON | 8~25% | ~0（噪声内） | ✅ | **永远先做，无条件** |
| **L1** | 砍 retrieval 尾部（按分数阈值 + 保留 top-N） | 15~45% | recall@10 −3~8pt | ✅ 下次可重查 | 默认第一降级位 |
| **L2** | history 压缩（摘要 + 最近 6 轮原文） | history 的 30~60% | 指代消解 −2~5pt | ⚠️ 摘要丢细节，不可逆 | L1 不够时 |
| **L3** | memory 只留 top-5 事实 | memory 的 50~80% | 个性化 −1~3pt | ✅ | L2 还不够时 |
| **L4** | tools 描述瘦身、砍 few-shot 示例 | tools 的 30~50% | 工具选择正确率 −4~9pt | ✅ | 很少用，工具错代价大 |
| **L5** | 缩 output budget | 视情况 | 截断风险，`finish_reason=length` 上升 | ⚠️ | 仅当输出本身可短 |
| **L6** | 降级到更大窗口模型 / 明确拒答并引导 | 0（换模型） | 成本↑ 或 体验↓ | ✅ | 前面全砍完还超 |

**降级顺序背后的唯一原则**：

> **先砍「可再生的」，再砍「可压缩的」，最后才碰「契约性的」。**

- `retrieval` 可再生——用户再问一次，或者换个 query 重查就有了。
- `history` 可压缩——摘要是信息损失，但信息还在。
- `memory` 可裁剪——少两条偏好不影响正确性，只影响贴心度。
- `tools` 一动就影响「能不能做对事」，代价比想象中大。
- `system` / `output` 是契约，砍了就违约。

> ⚠️ **L2 有一个隐形的坑**：摘要是有损的，而**你无法从摘要里恢复被丢掉的细节**。所以压缩后的 history 必须**单独落一份**（`history_compressed_ref`），并且 Run 元数据里标注 `context_degraded=L2`。否则三个月后客户问「你上次明明说可以退」，你连原始对话都拿不出来（第 31 章）。

### 15.5.3 装配器骨架

```go
type Section string

const (
	SecSystem    Section = "system"
	SecTools     Section = "tools"
	SecPersona   Section = "persona"
	SecMemory    Section = "memory"
	SecHistory   Section = "history"
	SecRetrieval Section = "retrieval"
)

type Block struct {
	Section Section
	Text    string
	Tokens  int     // 估算口径 E
	Score   float64 // retrieval 用，其它为 0
	Sticky  bool    // true = 不可砍（system / persona）
}

type Report struct {
	Allocatable int
	Used        int
	Degraded    []string // 记录砍了哪几级，进 Trace
	FinalRatio  float64
}

func Assemble(p Profile, blocks []Block) ([]Block, Report) {
	rep := Report{Allocatable: p.Allocatable()}

	// ① L0：无损清理（去重 + 折叠空结果），永远先做
	blocks = dedupeByHash(blocks)
	blocks = dropEmptyResults(blocks)

	// ② 按 section 分开，各自按预算装
	bySec := groupBySection(blocks)
	out := make([]Block, 0, len(blocks))

	for _, s := range p.OrderedSections() { // 缓存友好顺序，不是 degrade_order
		budget := p.Sections[s].Budget
		used := 0
		cand := bySec[s]
		if s == SecRetrieval {
			sort.SliceStable(cand, func(i, j int) bool { return cand[i].Score > cand[j].Score })
		}
		for _, b := range cand {
			if used+b.Tokens > budget {
				continue // 超本区预算就跳过，不许「借」别的区的额度
			}
			out = append(out, b)
			used += b.Tokens
		}
	}

	// ③ 总预算校验 + 降级
	total := sumTokens(out)
	if total <= rep.Allocatable {
		rep.Used = total
		return out, rep
	}

	for _, s := range p.DegradeOrder { // [retrieval, history, memory, tools]
		out = degradeOneLevel(out, s, p)
		rep.Degraded = append(rep.Degraded, string(s))
		if sumTokens(out) <= rep.Allocatable {
			break
		}
	}

	rep.Used = sumTokens(out)
	rep.FinalRatio = float64(rep.Used) / float64(rep.Allocatable)

	// ④ 兜底：全砍完还超 → 交给上层走 L6（换模型 / 拒答）
	if rep.Used > rep.Allocatable {
		panic(ErrContextOverflow) // 由上层捕获，转 L6
	}
	return out, rep
}
```

两个细节值得单独说：

- **`continue` 而不是 `break`**：跳过「这一块放不下」，继续试更小的块。检索结果里经常有一条 8k 的超长 chunk 混在 500 token 的短块里，`break` 会让你白白丢掉后面所有能用的。
- **区间预算不许互借**：retrieval 没用完的额度不能给 history。一旦允许互借，`system` 就可能被挤掉，而这是最危险的一类 bug。

### 15.5.4 四种 token 计量口径

这是本章第二重要的内容。**同一个请求，你会得到四个不同的 token 数**，它们各自服务不同目的，且**永远对不上**：

| 口径 | 名字 | 来源 | 精度 | 用途 | 与其它口径的偏差 |
| --- | --- | --- | --- | --- | --- |
| **E** | 估算（Estimate） | 本地 tokenizer（tiktoken / HF tokenizers） | 同模型 ±2% | 预算分配、准入拦截、超窗预判 | 跨模型 ±15~30% |
| **R** | 请求（Request） | 响应里的 `usage.prompt_tokens` | 精确 | 限流（TPM）、路由决策、上下文优化 | 基准 |
| **B** | 计费（Billing） | 账单明细（含缓存折扣、缓存写入溢价） | 精确 | 对账、成本归因、毛利 | 与 R 差 **3~40%** |
| **A** | 审计（Audit） | 事件落库后重算 | 取决于重算工具 | 取证、法务、监管协查 | ±2~30% |

**为什么 B 和 R 会差 3~40%？** 因为你看到的 `prompt_tokens` 是「总输入」，而账单是按「未缓存 / 已缓存 / 缓存写入」三档分别计价的：

```
input_cost = (prompt_tokens − cached_tokens) × P_in
           + cached_tokens                    × P_cached
           + cache_write_tokens               × P_write
output_cost = (completion_tokens + reasoning_tokens) × P_out
```

其中 `P_cached` 通常是 `P_in` 的 0.1~0.5 倍，`P_write` 通常是 `P_in` 的 1.25~2 倍（**具体倍率必须以厂商最新价目为准，各家差异很大**）。所以：

- 缓存命中率高 → B < R（省钱）
- 大量缓存写入 → B > R（写入有溢价）
- 推理模型 → `reasoning_tokens` 会计入 output，B > R

**这四个口径分别从哪儿取？**

| 厂商 | 缓存命中 | 缓存写入 | 流式取 usage |
| --- | --- | --- | --- |
| OpenAI | `usage.prompt_tokens_details.cached_tokens` | 无独立字段（自动缓存） | 请求体加 `stream_options: {"include_usage": true}`，最后一个 chunk 带 `usage` 且 `choices` 为空 |
| Anthropic | `usage.cache_read_input_tokens` | `usage.cache_creation_input_tokens` | 流式 `message_start` 事件带输入侧 usage，`message_delta` 带累计输出 usage |
| 自建（vLLM） | 由引擎侧统计 | 同上 | 由引擎的 metrics 端点或响应头提供 |

> ⚠️ **流式下「输出 token 数」在生成结束前是未知的**。你必须在**发请求前**按 `max_tokens` 预扣额度，生成结束后按实际 usage 结算——这就是第 27 章「预扣—结算—对账」三段式的物理来源。**先发后算，是流式系统的固有负债**，你只能靠预扣把它管住。

### 15.5.5 tokenizer 差异：同一段话，四种数法

| 文本（1,000 字） | o200k_base 量级 | cl100k_base 量级 | Qwen 系量级 | Llama 3 量级 |
| --- | --- | --- | --- | --- |
| 中文散文 1,000 字 | ~1,180 | ~1,520 | ~1,050 | ~1,640 |
| 英文 1,000 词 | ~1,290 | ~1,330 | ~1,400 | ~1,380 |
| 代码 1,000 行（Python） | ~2,600 | ~3,100 | ~2,400 | ~3,300 |
| JSON（带缩进） | ~1.4 × 原始字符数 | ~1.6 × | ~1.3 × | ~1.7 × |

（**数量级示意，必须用你自己的语料实测**。不同版本的分词表差异可达 20% 以上。）

**这带来一个必须在架构上解决的问题**：你的平台接 4 家模型，如果预算分配用 A 家的 tokenizer 估算、实际请求发给 B 家，那么你的「120k 预算」可能实际是 138k → 超窗 → 400。

三个工程解法，按成本排序：

1. **按模型分 profile**（推荐）：一个 profile 绑定一个 `tokenizer_id`，路由到哪个模型就用哪个 profile 的预算。成本最低。
2. **保守系数**：统一用「最保守 tokenizer 的估算 × 1.1」，牺牲 10% 窗口换实现简单。
3. **在线校准**：用上一次调用返回的 `usage.prompt_tokens` 与本地估算做比值，滚动修正系数（`ratio = EMA(actual/estimated, α=0.2)`）。精度最高，但要维护状态。

### 15.5.6 计量落库

```sql
CREATE TABLE llm_usage (
  id             BIGSERIAL,
  run_id         TEXT          NOT NULL,
  seq            BIGINT        NOT NULL,
  tenant_id      BIGINT        NOT NULL,
  model          TEXT          NOT NULL,
  prompt_id      TEXT,
  prompt_rev     INT,
  -- 四个口径并列存放，不要试图用其中一个推导另一个
  tokens_est     INT           NOT NULL,   -- E
  tokens_prompt  INT           NOT NULL,   -- R
  tokens_cached  INT           NOT NULL DEFAULT 0,
  tokens_cache_w INT           NOT NULL DEFAULT 0,
  tokens_out     INT           NOT NULL,
  tokens_reason  INT           NOT NULL DEFAULT 0,
  cost_usd       NUMERIC(14,6) NOT NULL,   -- B
  usage_source   TEXT          NOT NULL,   -- api | estimated | reconciled
  occurred_at    TIMESTAMPTZ   NOT NULL,
  PRIMARY KEY (id, occurred_at)
) PARTITION BY RANGE (occurred_at);

-- 预算健康度：哪个分区长期超预算、哪个分区长期空转
CREATE MATERIALIZED VIEW ctx_budget_daily AS
SELECT date_trunc('day', occurred_at) AS d,
       model,
       avg(tokens_est - tokens_prompt)::numeric(10,1) AS est_error,   -- 估算偏差
       sum(tokens_cached)::float / nullif(sum(tokens_prompt),0) AS cache_hit_rate,
       avg(cost_usd) AS avg_cost
  FROM llm_usage
 GROUP BY 1, 2;
```

**`est_error` 这个字段是被低估的宝藏**：它持续为正说明你系统性高估（浪费窗口），持续为负说明你系统性低估（迟早超窗 400）。**目标的绝对值应该 < 3%**，超过 8% 就该校准 tokenizer 了。

---

## 15.6 Java / 分布式经验对照

| 你的老经验 | 上下文预算里的对应 | 会咬人的差异 |
| --- | --- | --- |
| JVM 堆分区（Eden/Survivor/Old） | 七份预算分区 | 同构。差别是**分区之间不能借额度**——JVM 的 Survivor 能晋升到 Old，上下文不能 |
| `-Xmx` 与 OOM | 窗口上限与超窗 400 | 同构，但 Agent 的 OOM 是「静默降级」：你砍了它不报错，只是答得变差 |
| GC 中的「晋升失败」 | 降级阶梯全部走完还超 | 同构：这是最后一道防线，必须显式失败（L6），不能继续硬塞 |
| 对象大小估算（Instrumentation） | tokenizer 估算 | 同构。误差是常态，必须留 slack |
| 内存泄漏排查 | 上下文膨胀排查 | 更隐蔽：泄漏源是「每轮都追加历史」，48 小时后单会话上下文从 8k 涨到 140k |
| 序列化后的字节数 vs 内存对象大小 | E 口径 vs R 口径 | 同构：两个数本来就不该相等 |
| 分页/换出到磁盘 | history 摘要、memory 外置 | 同构。区别是摘要**有损且不可逆**，页换出是可逆的 |
| JFR / heap dump | `ctx_budget_daily` 视图 | 同构：没有这个视图你无法定位「为什么昨天成本翻倍」 |

---

## 15.7 开源坐标

| 项目 | 它提供了什么 | 它不管什么 |
| --- | --- | --- |
| **tiktoken**（OpenAI） | o200k_base / cl100k_base 的快速 BPE 分词 | 只覆盖 OpenAI 自己的模型；对 Qwen / Llama 的估算偏差可达 20% |
| **HF `tokenizers`** | 各开源模型的分词器，Rust 实现，快 | 每个模型要单独加载，内存占用 × N；chat template 的开销要自己算 |
| **LiteLLM** | 统一多 provider 的 `usage` 字段、`token_counter` 工具、成本表 | 各家缓存字段的映射偶有滞后；预算/降级策略完全没有 |
| **LangChain / LlamaIndex** | `Callback` 里能拿到 usage，`TokenTextSplitter` 能做块级估算 | 只给「统计」，不给「预算分配」和「降级决策」；这两件事必须你自己写 |
| **vLLM** | 引擎侧精确的 prompt/completion token 统计与 metrics | 只在自建场景可用；跨 provider 无法统一 |
| **Langfuse** | 把 usage 与 cost 落到 trace 上，可按 prompt 版本切片 | 是「观测」不是「控制」：它告诉你花了多少，不会阻止你花 |

> 🎯 **现实的组合**：`tiktoken` + `HF tokenizers` 做 E 口径估算 → 自己写 200 行装配器做预算与降级 → provider 返回的 `usage` 做 R 口径 → 自己的账本做 B 口径 → Langfuse/ClickHouse 做 A 口径展示。**没有开源项目会替你做「降级决策」，因为那是业务判断。**

---

## 15.8 事故清单

- [ ] **事故 A：上下文膨胀，48 小时后单会话爆窗。** 现象：新会话一切正常，聊到第 60 轮开始报 400。→ 根因：每轮追加完整历史，没有窗口预算概念。→ 兜底：history 超过 60% 预算就触发 L2 压缩；会话级「上下文水位」指标进监控。
- [ ] **事故 B：估算偏差 25%，砍完还超窗。** 现象：估算 118k，实际 148k，稳定复现 400。→ 根因：用 cl100k 估算、实际调 Qwen；且工具返回的 JSON 缩进没计入。→ 兜底：按模型分 profile；`est_error` 监控；工具结果先紧凑化再计量。
- [ ] **事故 C：降级把 system 砍了。** 现象：降级上线后，JSON 输出格式错误率从 0.3% 涨到 14%。→ 根因：降级链里误把 `system` 当成可压缩分区。→ 兜底：`system`/`persona`/`output` 在代码层面用 `Sticky` 标记，装配器拒绝裁剪；CI 加单测断言。
- [ ] **事故 D：缓存命中率归零。** 现象：加了 retrieval 之后，前缀缓存命中率从 88% 掉到 2%，成本涨 3 倍。→ 根因：把 retrieval 拼在了 system 前面，每次请求前缀都变。→ 兜底：上下文顺序固定为「稳定 → 易变」；把顺序写进装配器的常量，不给业务改。
- [ ] **事故 E：预扣不足导致超支。** 现象：流式请求按平均值预扣 2k token，实际输出 8k，月末超支 40%。→ 根因：预扣用的是均值而非 `max_tokens`。→ 兜底：预扣 = `max_tokens`（上限），结算按实际 usage 退回差额（第 27 章）。
- [ ] **事故 F：摘要丢了关键约束。** 现象：压缩 3 轮后，用户说过的「我不接受分期」被摘要吃掉，Agent 又开始推分期。→ 根因：摘要 prompt 没有「必须保留否定约束」的指令。→ 兜底：摘要器强制保留「否定/禁止/偏好」类语句；压缩前后跑一致性检查。
- [ ] **事故 G：所有租户共用一个 profile。** 现象：小客户也在用 128k 窗口，成本与收入倒挂。→ 根因：没按套餐/场景分 profile。→ 兜底：profile 由「场景 + 套餐」二维决定，进路由层；成本按 profile 归因（第 47 章）。

---

## 15.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 窗口三段 | `output_reserve`（硬留）+ `slack`（10%）+ `allocatable`（七份预算争抢） |
| 七份预算 | system / tools / persona / memory / history / retrieval / output |
| 弹性池 | `retrieval` 占 45%，是第一顺位被砍的；`system` 和 `output` 永不砍 |
| 降级原则 | 先砍可再生的（retrieval）→ 可压缩的（history）→ 可裁剪的（memory）→ 最后才是 tools |
| 降级要记账 | `context_degraded=L1..L6` 必须进 Trace，否则你无法解释「为什么今天答得差」 |
| 顺序即成本 | 稳定内容在前（system→tools→persona）、易变在后（retrieval→history）；顺序错了缓存命中率归零 |
| 四口径 | E 估算 / R 请求 / B 计费 / A 审计，**永远对不上，各有各的用途** |
| B≠R 的原因 | 缓存折扣（0.1~0.5×）+ 缓存写入溢价（1.25~2×）+ reasoning token |
| 流式 | 输出 token 结束前未知 → 必须按 `max_tokens` 预扣、按 usage 结算 |
| tokenizer | 跨模型差异 15~30%；按模型分 profile 是最省钱的解法 |
| 成本 | 32k vs 120k 满载，单次调用差 $0.264；每天 1.29M 长上下文 Run 就是 $2M |
| 一句话 | **上下文不是「越多越好」，是「每一千 token 都要有主人」。** |

---

## 15.10 自检题

1. **一次调用的 `prompt_tokens` 是 50,000，账单却只收了相当于 12,000 的钱，为什么？**
   > 要点：前缀缓存。`usage` 里的 `prompt_tokens` 是总输入，但账单按三档计价：未缓存（`prompt_tokens − cached_tokens`）、已缓存（`cached_tokens`，价格是原价的 0.1~0.5 倍）、缓存写入（`cache_creation_input_tokens`，价格是原价的 1.25~2 倍）。50k 里若有 44k 命中缓存，成本 ≈ 6k×P + 44k×0.1P ≈ 10.4k 等效 token。所以「R 口径」和「B 口径」永远对不上，两者都要记，前者用于限流、后者用于对账。

2. **为什么 `system` 分区不能参与降级？**
   > 要点：因为它是**输出契约**，不是内容。system 里定义了输出格式、安全策略、能力边界。砍掉一部分，模型就不知道该输出什么结构，下游解析器直接崩（错误率从 0.3% 涨到 14% 是真实量级）。同理 `output` 不能砍——砍了就会 `finish_reason=length`，用户看到半句话，而你已经付了钱。正确做法是：把它们标记为 `Sticky`，装配器在代码层面拒绝裁剪，并用单测锁死这个行为。

3. **「把 retrieval 结果放在 system 前面，模型读起来更自然」，这句话的问题在哪？**
   > 要点：破坏前缀缓存。prompt cache 按「从第一个 token 起的最长公共前缀」计算折扣；把每次请求都不同的 retrieval 放在最前面，等于让所有请求的前缀从第 1 个 token 就分叉，缓存命中率归零（实测可从 88% 掉到 2%），成本涨 3 倍。正确顺序是「稳定 → 易变」：system → tools → persona → memory → retrieval → history → 当前输入。模型对顺序不敏感，但计费对顺序极其敏感。

---

## 15.11 延伸阅读

- 上一章：[14. 调度器设计：GPU/CPU 混合与并发度](14-scheduler-design.md)
- 下一章：[16. 提示词资产化：模板、版本与评测驱动](16-prompt-asset.md)
- OpenAI 官方文档：`usage` 对象、`prompt_tokens_details.cached_tokens`、`stream_options.include_usage`（以官方最新版为准）
- Anthropic 官方文档：prompt caching、`cache_creation_input_tokens` / `cache_read_input_tokens`、流式 usage 事件
- `tiktoken` 仓库、HuggingFace `tokenizers` 文档
- 相关：第 17 章（角色卡预算）、第 18 章（压缩与前缀复用）、第 24 章（缓存）、第 27 章（预扣结算）、第 47 章（FinOps）

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 语言/运行时细节：见 [../go/0-summary.md](../go/0-summary.md)
- 前置：第 14 章
- 下一章：第 16 章
