# 16. 提示词资产化：模板、版本与评测驱动

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 上下文工程（Context Engineering） ｜ **编号**：16 / 52
> **本章定位**：提示词是「代码 + 配置 + 资产」三位一体，而大多数团队只把它当字符串。本章讲怎么让它进 Git、有变量契约、能评审、能 A/B、能过门禁、能热更，并且**热更不绕过门禁**。
> **读法**：§16.4 的模板 schema 和 CI 门禁可以直接落地；§16.5「什么能热更」那张表建议贴在发布流程旁边。如果你的团队现在还有人直接在控制台改 prompt，先看 §16.1。

---

## 16.1 引子：一句「可以退款」，两天 37 万

周五 17:40，客服运营在提示词管理后台发现一个用户投诉：「Agent 说不能退，但你们页面写了 7 天无理由」。她想，改一句话的事，何必走发版流程。于是她在 Web 界面上把：

```
- 仅当订单未拆封且签收 ≤ 7 天时，可判定为 REFUNDABLE。
+ 用户提出退款时，可判定为 REFUNDABLE，并说明 7 天无理由。
```

点了「保存并生效」。系统提示「已发布 v38，全量生效」。

周一早上，财务发现**两天内 1,240 笔退款被自动批准，平均 300 元，合计 37.2 万元**，其中 890 笔的订单已拆封、超过 7 天——按政策根本不该退。

事后复盘，你发现这条链路上有 6 个缺口，每一个单独存在都不致命，凑在一起就是事故：

| # | 缺口 | 造成的后果 |
| --- | --- | --- |
| 1 | 提示词能直接在 Web 上改，**不经过 Git** | 没有任何 diff 记录，复盘时只能靠截图 |
| 2 | 改的人不是 owner，**没有评审** | 「可判定为」和「仅当…时」的语义差异没人看出来 |
| 3 | 发布**没跑评测集** | 500 条评测集里至少有 40 条会抓到这个问题 |
| 4 | **全量生效**，没有灰度 | 2 天影响全部 10 万租户，而不是 5% |
| 5 | 判定结果**直接触发了退款工具** | 模型输出没有被下游策略层二次校验 |
| 6 | Trace 里只记了 `prompt_v38`，**没记 content_hash** | 事后想确认「当时那版到底写了什么」，v38 已被 v39 覆盖 |

**这六个缺口，本章一个一个补。**

> 🩸 一句要刻在墙上的话：**提示词是唯一一种「改一个字符就能改变公司现金流」的配置。** 你的 `application.yaml` 改错了最多服务起不来（有健康检查兜底），提示词改错了会**安静地、正确地、大规模地做错事**。

---

## 16.2 三位一体：为什么 prompt 既不是纯配置也不是纯代码

先破除两个常见误解：

- 「提示词就是配置，放 Apollo/Nacos 里热更就行」→ **错**。配置项没有变量契约、没有行为语义、不需要评测。提示词三者全有。
- 「提示词就是代码，跟服务一起发版就行」→ **也错**。提示词的迭代频率是代码的 5~10 倍，每次都走完整的镜像构建 + 滚动发布，你会把工程师逼回控制台。

正确的模型是**三位一体**：

| 属性 | 来自「代码」 | 来自「配置」 | 来自「资产」 |
| --- | --- | --- | --- |
| 具体表现 | 变量契约、可测试、可 diff、CI 门禁 | 热更、灰度、A/B、秒级回滚 | 有 owner、有版本、有评测集、有 ROI |
| 治理手段 | Git + Code Review + 单测 | Registry + 指针切换 + Flag | 评测门禁 + 成本核算 + 权限 |
| 缺失会怎样 | 改坏了没人知道 | 迭代慢到业务放弃 | 没人负责，越改越烂 |
| 本章对应 | §16.4.1 ~ §16.4.4、§16.4.6 | §16.4.5、§16.4.7 | §16.4.3、§16.5 |

### 账本：提示词到底值多少钱

这不是修辞。算一笔最普通的账——**一句 500 token 的「礼貌用语 + 示例」**：

```
每个 Run 平均 6 次模型调用，每次都要重发 system prompt
500 token × 6 = 3,000 token / Run 的冗余

Aurora 日均 8.6M Run（峰值 300/s，日均按 100/s 折算）
→ 25.8B token / 天
→ 按 $3/M 输入计价：$77,400 / 天

命中前缀缓存后（90% 命中、缓存价 0.1×）：
→ 等效 0.19 × $77,400 = $14,706 / 天
→ 约 $441,000 / 月，约 $5.3M / 年
```

**一句话，一年 530 万美元。** 反过来说：如果你能把这 500 token 压到 200 token 而不掉质量（用评测集证明），你一年省 318 万美元。**这就是为什么提示词必须被当作资产管理——它有明确的、可计算的 ROI。**

再看变更频率，这是「为什么不能每次都发版」的答案：

| 指标 | Aurora 实测 |
| --- | --- |
| 提示词模板总数 | 240 个（含变体） |
| 每周变更次数 | 768 次（平均每个模板 3.2 次/周） |
| 折合每天 | ~110 次 |
| 其中有质量回归的比例 | 15%（约 16 次/天） |
| 走完整发版流程的耗时 | 25 分钟（构建 + 滚动 + 验证） |

每天 110 次变更 × 25 分钟发版 = 45 小时。**你不可能让提示词走代码发布流程**——不是不想，是物理上做不到。这就是「必须热更」的真正原因。

---

## 16.3 结构图：一条闭环，而不是一条直线

```
┌──────────────┐  PR   ┌───────────────────┐ 通过  ┌────────────────────┐
│ Git           │──────▶│ CI 门禁            │──────▶│ Prompt Registry     │
│ prompts/<id>/ │       │ ① lint（占位符）   │       │ rev++（不可变）      │
│  ├ body.md    │◀──────│ ② vars schema diff │       │ content_hash        │
│  ├ vars.json  │ 失败   │ ③ 评测集回归       │       │ status: canary      │
│  └ prompt.yaml│       │ ④ 成本/延迟门禁     │       └─────────┬──────────┘
└──────────────┘       └───────────────────┘                 │ 指针切换
        ▲                                                      ▼
        │                                        ┌──────────────────────────┐
        │                                        │ 运行时                    │
        │                                        │ 按 tenant 稳定分流 A/B     │
        │                                        │ 每 Run 记 id@rev + hash   │
        │                                        └────────────┬─────────────┘
        │                                                     ▼
        │                                        ┌──────────────────────────┐
        │                                        │ Trace / 评测              │
        │                                        │ 按 rev 切片看：通过率      │
        │                                        │ 成本 / TTFT / 投诉率       │
        │                                        └────────────┬─────────────┘
        │                                                     │
        └──────────── badcase 回流成新评测样本 ◀───────────────┘
```

**这是一条闭环，不是一条直线。** 闭环的最后一环（badcase → 新评测样本）决定了你的提示词是「越改越好」还是「越改越随机」。没有这一环，你的评测集会停留在 2026 年 3 月的水平，而线上流量早就变了（第 40、41 章）。

---

## 16.4 关键设计

### 16.4.1 模板 schema：front-matter 管契约，body 管内容

```yaml
# prompts/refund-judge/prompt.yaml
id: refund-judge
rev: 37                        # 单调递增，由 CI 分配，不可回填、不可修改
content_hash: sha256:9f2a1c... # CI 计算（body + vars_schema 的规范化哈希），只读
owner: team-cs-platform        # ★ 必填。无 owner 的 prompt 禁止上线
model_hint: claude-sonnet-4-5  # 仅作提示，实际模型由路由层决定（第 26 章）
params:
  temperature: 0
  max_tokens: 800
vars_schema: ./vars.schema.json
evalset: evalset://cs-refund/v4
gate:
  min_pass_rate: 0.94
  max_p95_latency_ms: 6000
  max_cost_usd_per_call: 0.012
changelog:
  - rev: 37
    at: 2026-04-28
    by: zhang.li
    why: "把「可退款」收窄为「7 天内未拆封可退」，修复 4/22 的越权承诺"
```

```markdown
<!-- prompts/refund-judge/body.md -->
你是 {{company}} 的售后判定助手。今天是 {{today}}。

## 判定规则
1. 仅当 {{order.delivered_days}} <= 7 且 {{order.opened}} == false 时，
   判定为 REFUNDABLE。
2. 其余情况一律返回 NOT_REFUNDABLE，并把 reason_code 设为对应规则编号。
3. 你不具备批准能力。任何金额都只是建议，最终由人工确认。

## 输出契约
严格输出 JSON，不要任何额外文本：
{"decision":"REFUNDABLE|NOT_REFUNDABLE","reason_code":"R1|R2|R3","confidence":0.0}

## 边界
- 不承诺具体到账时间。
- 不解释内部规则编号的业务含义。

<context>
订单：{{order_json}}
政策版本：{{policy_version}}
</context>
```

**front-matter 里三个字段最重要，缺一不可：**

| 字段 | 为什么必须有 | 不写会怎样 |
| --- | --- | --- |
| `owner` | 出事了找谁 | 240 个模板里有 60 个没人认领，改坏了只能全组一起排查 |
| `gate` | 把「发布资格」量化 | 「跑过评测了」变成主观判断，最后没人跑 |
| `content_hash` | 内容级不可变标识 | `rev` 会被覆盖，三个月后你无法证明「v38 当时写的是什么」 |

### 16.4.2 变量 schema：把「缺变量」从运行时错误变成 CI 错误

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["company", "today", "order", "order_json", "policy_version"],
  "properties": {
    "company": { "type": "string", "minLength": 1, "maxLength": 40 },
    "today": { "type": "string", "format": "date" },
    "policy_version": { "type": "string", "pattern": "^v[0-9]+\\.[0-9]+$" },
    "order": {
      "type": "object",
      "required": ["delivered_days", "opened"],
      "properties": {
        "delivered_days": { "type": "integer", "minimum": 0, "maximum": 3650 },
        "opened": { "type": "boolean" }
      }
    },
    "order_json": { "type": "string", "maxLength": 4000 }
  }
}
```

`additionalProperties: false` 这一行是刻意的：**多传一个变量也要报错**。因为多传通常意味着「你以为是这个变量名，其实是另一个」，静默忽略会让 prompt 里那个占位符渲染成空——而空串是最危险的渲染结果。

```go
var placeholder = regexp.MustCompile(`\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}`)

// Render 严格渲染：缺变量报错，绝不渲染成空串。
func Render(t *Template, vars map[string]any) (string, error) {
	// ① 变量契约校验：多传、少传、类型不对，全部拒绝
	if err := t.Schema.Validate(vars); err != nil {
		return "", fmt.Errorf("vars schema: %w", err)
	}

	// ② 模板里出现的每个占位符都必须能取到值
	var missing []string
	for _, m := range placeholder.FindAllStringSubmatch(t.Body, -1) {
		if _, ok := lookupPath(vars, m[1]); !ok {
			missing = append(missing, m[1])
		}
	}
	if len(missing) > 0 {
		// ★ 这是整章最重要的一行：宁可 500，也不要渲染出半句话
		return "", fmt.Errorf("missing vars %v in %s@%d", missing, t.ID, t.Rev)
	}

	// ③ 单遍替换：变量值里即使包含 {{...}} 也不会被二次展开（防模板注入）
	return placeholder.ReplaceAllStringFunc(t.Body, func(s string) string {
		k := placeholder.FindStringSubmatch(s)[1]
		v, _ := lookupPath(vars, k)
		return stringify(v) // 对 string 类型不再做任何转义处理，由调用方保证内容安全
	}), nil
}
```

**为什么「绝不渲染成空串」这么重要？** 看这条规则被空串污染的后果：

```
正常：仅当 5 <= 7 且 false == false 时，判定为 REFUNDABLE。
缺变量：仅当   <=  且  ==  时，判定为 REFUNDABLE。
```

模型会怎么理解第二句？它会忽略这个残缺条件，然后**按「用户提出退款就退」处理**——和你想要的行为正好相反。**这就是 §16.1 那 37 万的另一种成因。** 而它完全不会报错，日志里一切正常。

### 16.4.3 版本管理：`rev` + `content_hash`，不要 semver

很多人第一反应是给提示词打 `v1.2.3`。**这是个陷阱。**

| 方案 | 能回答的问题 | 不能回答的问题 |
| --- | --- | --- |
| semver（`v1.2.3`） | 「这次改动大不大」（主观） | 「v1.2.3 的确切内容是什么」「两次不同分支合并后该是哪个号」 |
| 单调 `rev`（`37`） | 「谁在谁之后」「回滚到上一个」 | 「内容是否相同」（同一内容可能被提交两次） |
| `content_hash` | 「内容是否完全一致」「A/B 两组内容是否真的不同」 | 「顺序」 |
| **`rev` + `content_hash`（推荐）** | 上面全部 | — |

理由很直接：**你无法判断「把『必须』改成『应当』」是 patch 还是 minor**。提示词的语义版本没有客观标准，只会变成一场关于命名法的会议。而 `rev`（顺序）+ `content_hash`（同一性）是纯客观的。

```sql
CREATE TABLE prompt_version (
  id           TEXT        NOT NULL,
  rev          INT         NOT NULL,
  content_hash CHAR(71)    NOT NULL,        -- "sha256:..."
  body_ref     TEXT        NOT NULL,        -- 正文放对象存储，库里只存引用
  vars_schema  JSONB       NOT NULL,
  model_hint   TEXT,
  params       JSONB       NOT NULL,
  evalset_ref  TEXT        NOT NULL,
  gate_result  JSONB       NOT NULL,        -- {pass_rate, p95_ms, cost_usd}
  status       TEXT        NOT NULL,        -- draft|canary|active|retired|rolled_back
  owner        TEXT        NOT NULL,
  created_by   TEXT        NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (id, rev),
  UNIQUE (content_hash)                      -- 同内容不允许两个 rev
);

-- 表上不允许 UPDATE body_ref / vars_schema；只允许 UPDATE status（推进或作废）
CREATE OR REPLACE FUNCTION forbid_prompt_content_update() RETURNS trigger AS $$
BEGIN
  IF NEW.body_ref <> OLD.body_ref OR NEW.vars_schema <> OLD.vars_schema
     OR NEW.content_hash <> OLD.content_hash THEN
    RAISE EXCEPTION 'prompt content is immutable; create a new rev instead';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
```

`UNIQUE (content_hash)` 有个额外好处：**它能自动发现「无意义的 rev」**。如果两个人提了内容完全一样的改动，CI 会直接拒绝，省掉一次全量发布的噪音。

### 16.4.4 diff 与评审：结构化 diff，不是文本 diff

普通文本 diff 在提示词上几乎没用——它会把「删了一个换行」和「把『仅当』改成『可』」渲染成同样大小的一块红色。评审者看不出哪个要命。

必须做的是**分层 diff**：

| diff 层 | 展示内容 | 评审重点 | 谁看 |
| --- | --- | --- | --- |
| **契约层** | `vars_schema` 的增删改 | **最高危**：删字段/改类型 = 老代码会 500 | 后端 owner（必须 approve） |
| **参数层** | `params`（temperature / max_tokens）、`model_hint` | 影响稳定性与成本 | 后端 owner |
| **结构层** | 段落/标题/规则编号的增删 | 规则被删 = 行为可能反转 | 产品 + 算法 |
| **语义层** | 逐句文本 diff | 语气、限定词、否定词 | 产品或运营 |
| **示例层** | few-shot 示例的增删改 | 示例会强烈影响输出风格 | 算法 |
| **契约输出** | 输出 JSON 结构是否变化 | 变了必须联动下游解析器 | 后端 + 下游 owner |

**结构性强制规则（写进 CI，别写进文档）**：

- `vars_schema` 有任何变更 → 必须由 `owner` 之外的**第二个人** approve，且 PR 标题必须带 `[contract-change]`。
- 输出契约段落（`## 输出契约` 之后到下一个 `##` 之前）有变更 → 必须同时修改下游解析器的测试，否则 CI 失败。
- 单次 diff 超过 40% 的正文行 → 警告「这已经不是微调了，考虑新建 prompt id 而不是新 rev」。理由：大改之后 A/B 的归因会失效。

### 16.4.5 A/B 与灰度：分流键必须稳定

```go
// 分流键必须是「稳定的实体」，不能是 run_id。
// 用 run_id 会让同一个用户在相邻两轮之间看到不同版本 → 体验割裂，且无法按用户切片分析。
func variant(promptID, tenantID, conversationID string) string {
	h := fnv.New64a()
	// 键的构成决定了一致性粒度：
	//   按 conversation 一致 → 同一通对话内语气稳定（推荐，用户感知最强）
	//   按 tenant 一致       → 同一客户全量一致（适合 B 端，避免客户互相看到差异）
	fmt.Fprintf(h, "%s|ab-v3|%s|%s", promptID, tenantID, conversationID)
	if h.Sum64()%100 < 5 {
		return "v38" // 5% 灰度
	}
	return "v37"
}
```

| 粒度 | 分流键 | 适用 | 缺点 |
| --- | --- | --- | --- |
| 按请求 | `run_id` | ❌ 不推荐 | 同一用户看到两个版本，体验灾难；无法归因 |
| 按会话 | `(tenant, conversation)` | ✅ 推荐，C 端 | 会话边界不清时（如跨端）会漂移 |
| 按租户 | `tenant_id` | ✅ 推荐，B 端 | 大租户在小比例灰度里可能一个都不命中 |
| 按用户 | `(tenant, user)` | ✅ C 端长期实验 | 需要稳定的 user id |

灰度节奏（Aurora 的实际参数）：

```
5%  → 观察 30 分钟：通过率、TTFT P95、成本/次、投诉率、撤回率
   ↓ 全部达标
25% → 观察 2 小时
   ↓
100% → 观察 24 小时（跨一个业务高峰）
   ↓
标记 v37 为 retired（保留可回滚，但不再分流）
```

**每一步的「达标」必须是有数字的**，否则你会一路点「下一步」。参考阈值：通过率下降 < 1pt、TTFT P95 上升 < 15%、成本/次上升 < 8%、投诉率无显著变化。

### 16.4.6 评测门禁：CI 里必须有的五道检查

```yaml
# prompts/refund-judge/promptfooconfig.yaml
prompts:
  - file://body.md
providers:
  - id: anthropic:messages:claude-sonnet-4-5
    config:
      temperature: 0
      max_tokens: 800
tests: evalset://cs-refund/v4
defaultTest:
  assert:
    # ① 结构：必须是合法 JSON 且字段齐全
    - type: is-json
    # ② 枚举与类型：判定值必须在允许集合内
    - type: javascript
      value: |
        const o = JSON.parse(output);
        return ["REFUNDABLE","NOT_REFUNDABLE"].includes(o.decision)
            && /^R[1-3]$/.test(o.reason_code)
            && typeof o.confidence === "number"
            && o.confidence >= 0 && o.confidence <= 1;
    # ③ 禁止项：绝不能出现的表述（越权承诺）
    - type: not-contains
      value: "一定可以退"
    - type: not-contains
      value: "我批准"
    # ④ 成本与延迟上限
    - type: cost
      threshold: 0.012
    - type: latency
      threshold: 6000
```

再加一个 shell 层的前置闸门，把「契约变更」和「占位符一致性」卡死：

```bash
#!/usr/bin/env bash
# .github/workflows/prompt-gate.sh
set -euo pipefail
ID="${1:?prompt id required}"
DIR="prompts/${ID}"

# ① 变量契约变更必须走人工特批
if ! git diff --quiet "origin/main" -- "${DIR}/vars.schema.json"; then
  if [[ "${PR_TITLE:-}" != *"[contract-change]"* ]]; then
    echo "::error::vars.schema.json changed but PR title lacks [contract-change]"; exit 1
  fi
  echo "::warning::contract change requires 2 approvals"
fi

# ② 占位符集合必须与 schema 的 required 完全一致（双向，不能多也不能少）
go run ./tools/promptlint --dir "${DIR}" --strict

# ③ 渲染冒烟：用评测集的每个 case 的 vars 渲染一遍，禁止出现空串占位
go run ./tools/promptrender --dir "${DIR}" --fixtures "${DIR}/fixtures/*.json"

# ④ 评测集回归（fail-on-error：任何 assertion 失败即整体失败）
npx promptfoo eval -c "${DIR}/promptfooconfig.yaml" --fail-on-error --output result.json

# ⑤ 量化门禁：通过率 / P95 延迟 / 单次成本
go run ./tools/promptgate \
  --result result.json \
  --min-pass 0.94 --max-p95-ms 6000 --max-cost 0.012
```

五道检查，各挡一类事故：

| # | 检查 | 挡掉的事故 | 漏了会怎样 |
| --- | --- | --- | --- |
| ① | 变量 schema diff + 特批 | 「删了一个变量，老代码全 500」 | 上线 5 分钟后大面积 500 |
| ② | 占位符 ↔ schema 双向一致 | 「模板用了变量，schema 没声明」 | 渲染成空串，行为静默反转（§16.1） |
| ③ | 渲染冒烟 | 「某个 case 缺字段」 | 只在长尾流量上出错，评测集覆盖不到 |
| ④ | 评测集回归 | 质量回归 | 靠用户投诉发现，平均滞后 2 天 |
| ⑤ | 成本/延迟门禁 | 「多加了 800 token 示例」 | 成本涨 30%，一个月后才在账单上看到 |

### 16.4.7 热更边界：什么能热，什么绝对不能

这是最需要「写死规则」的一节。**热更是发布速度，门禁是发布资格，两者不冲突**——事故从来不是因为「热更」本身，而是因为「因为能热更，所以跳过了门禁」。

| 变更类型 | 能热更？ | 条件 | 原因 |
| --- | --- | --- | --- |
| 文案措辞、语气、标点 | ✅ | 过门禁 | 不改变契约，只改风格 |
| few-shot 示例增删（总 token 不超预算） | ✅ | 过门禁 + 灰度 | 影响效果但不动契约 |
| 变量取值来源调整（schema 不变） | ✅ | 过门禁 | 渲染契约不变 |
| **新增占位符 / 改变量 schema** | ❌ **必须发版** | — | 老代码不提供该变量 → 渲染失败 |
| **删除占位符** | ⚠️ 可热更但必须双跑 | 新旧 concurrent 24h | 老代码仍会传，`additionalProperties:false` 会炸 |
| **输出契约变更（JSON → 其他结构）** | ❌ **必须发版 + 下游联动** | — | 解析器要一起改 |
| 输出契约的**字段新增**（可选字段） | ✅ | 下游解析器必须已支持 ignore unknown | 兼容 |
| `params` 调整（temperature / max_tokens） | ✅ | 过门禁 | 影响稳定性，需评测 |
| `model_hint` 切换到别的模型 | ❌ 走模型灰度 | 第 42 章 | 这不是 prompt 的职责 |

> ⚠️ **一条硬规则**：任何「必须发版」的变更，**热更通道必须物理上拒绝它**。不要靠文档、不要靠 code review——在 Registry 的写接口里加判断：如果新 rev 的 `vars_schema` 与当前 active rev 不兼容，且请求来自热更接口（而非发版流水线），直接 403。**人会在周五 17:40 犯懒，代码不会。**

---

## 16.5 双轨治理：代码轨与提示词轨怎么合并成一条

很多团队的痛苦在于「两套流程」。正确的做法是**一条流水线，两套产物**：

| 维度 | 代码轨 | 提示词轨 | 统一方式 |
| --- | --- | --- | --- |
| 存储 | Git | **同一个 Git 仓**（`prompts/` 目录） | 一个 PR 可以同时改代码和 prompt |
| 评审 | Code Review | Prompt Review（结构化 diff + 评测报告） | 同一条流水线，reviewer 按路径自动分配 |
| 版本 | git sha | `rev` + `content_hash` | Trace 里**两个都记**（第 39 章） |
| 测试 | 单测 + 集成 | 评测集 + 断言 | CI 同一个 job，只是 stage 不同 |
| 上线 | 镜像 + 滚动 | Registry 指针切换 | prompt 走 `canary → 5% → 25% → 100%` |
| 回滚 | `kubectl rollout undo` | 指针指回上一 rev | **都要求 1 分钟内可回滚**（这是硬 SLO） |
| 责任人 | 后端 owner | `owner` 字段（必填） | 无 owner 的 prompt 在 CI 里直接拒绝 |
| 审计 | git log | `prompt_version` 表 + 操作日志 | 都进审计链（第 31 章） |

**两个必须统一的字段（否则事后无法归因）**：

1. **`prompt_id@rev` + `content_hash` 必须进每一次 Run 的元数据。** 只记 `rev` 不够——rev 会被后续版本覆盖含义，而 hash 永远唯一。出事时你能拿 hash 从对象存储里取回**逐字节相同**的原文。
2. **`prompt_id` 必须进缓存 key。** 语义缓存、结果缓存、前缀缓存的 key 里没有 prompt 版本，你会用 v36 的答案回答 v37 的问题（第 24 章，这是缓存错配的头号成因）。

---

## 16.6 Java / 分布式经验对照

| 你的老经验 | 提示词资产化里的对应 | 会咬人的差异 |
| --- | --- | --- |
| Nacos / Apollo 配置中心 | Prompt Registry | 配置项是「值」，提示词是「行为」。配置没有评测集，提示词必须有 |
| 配置灰度（按 IP / 按标签） | 按 tenant / conversation 分流 | 同构。但 Agent 的灰度必须**能看到质量指标**，不只是错误率 |
| `@Value` + 配置热更新 | 模板热更 | 配置热更失败会启动报错，提示词热更失败是**静默行为改变** |
| Maven 版本号 | `rev` + `content_hash` | semver 在提示词上不成立（无法客观判断改动级别） |
| 数据库 migration（Flyway） | `vars_schema` 演进 | 同构：向前兼容、不删字段、不改类型。**提示词 schema 变更 ≈ DDL 变更** |
| 单元测试 + 断言 | 评测集 + 判定式断言 | 断言方式必须换：不能 `assertEquals`（第 1 章断崖 1） |
| 蓝绿发布 | A/B + 双跑 | 蓝绿只验证「能不能用」，A/B 要验证「好不好用」 |
| 代码覆盖率 | 评测集覆盖率 | 同构的概念：你的评测集有没有覆盖那 40 条会抓到这个 bug 的 case |
| JVM 参数热调（jinfo） | 热更提示词 | 都危险。区别是 JVM 参数有边界检查，prompt 没有 |

---

## 16.7 开源坐标

| 项目 | 它提供了什么 | 它不管什么 |
| --- | --- | --- |
| **Langfuse Prompt Management** | prompt 的版本、label（如 `production`/`staging`）、客户端 SDK 缓存与降级、与 trace 打通 | 变量契约校验、结构化 diff、CI 门禁都要你自己做；它是「存储 + 分发」，不是「治理」 |
| **Dify** | 可视化 prompt 编辑、版本历史、与工作流绑定、草稿/发布 | 偏「应用内配置」，跨应用复用与 CI 集成弱；变量是运行期校验，不是 PR 期 |
| **Promptflow**（Microsoft） | 把 prompt 当作 flow 的节点，有 `.promptflow` 的 DAG、本地/云端运行、批量评测 | 与 Azure 生态耦合较深；纯 prompt 资产的版本治理不如专业 Registry |
| **promptfoo** | 评测驱动的事实标准：多 provider、断言（`is-json` / `javascript` / `llm-rubric` / `cost` / `latency`）、CI 集成 | 只管「评测」，不管「版本」与「发布」 |
| **Git + JSON Schema** | 变量契约的事实标准 | 不管渲染、不管分发 |

> 🎯 **现实的最小可行组合**：`Git`（存储与评审）+ `JSON Schema`（变量契约）+ 自研 200 行 Registry（rev/hash/指针）+ `promptfoo`（评测门禁）+ `Langfuse`（Trace 切片）。**没有任何一个开源项目会替你做「发布决策」，因为那是业务判断。**

---

## 16.8 事故清单

- [ ] **事故 A：控制台直改，无 diff 无评审。** 现象：一句「可以退款」两天造成 37 万损失，复盘时连改前的原文都找不到。→ 兜底：热更接口必须写入 Registry（产生新 rev + hash），禁止「原地覆盖」；所有写操作进审计链。
- [ ] **事故 B：缺变量渲染成空串。** 现象：模型行为反转，但日志全绿、错误率 0%。→ 兜底：严格渲染，缺变量直接失败；CI 里用 fixtures 对每个 case 做渲染冒烟；模板占位符与 schema 双向一致性检查。
- [ ] **事故 C：`rev` 被覆盖，无法取回原文。** 现象：客户投诉「你们当时说可以退」，你查到 `prompt_v38`，但它已被 v39 覆盖。→ 兜底：正文存对象存储 + `content_hash` 唯一索引 + 表级触发器禁止修改正文。
- [ ] **事故 D：A/B 用 `run_id` 分流。** 现象：同一用户在相邻两轮看到不同语气，投诉「你们机器人精神分裂」。→ 兜底：分流键改为 `(tenant, conversation)`；分流函数加单测，断言「同一 conversation 100 次调用结果一致」。
- [ ] **事故 E：热更绕过评测门禁。** 现象：为了让大客户尽快看到改动，运维在后台「临时」全量推送未评测版本。→ 兜底：热更接口强制检查 `gate_result`，缺失或过期（> 7 天）一律拒绝；紧急通道需双人 approve 且强制 5% 灰度起步。
- [ ] **事故 F：输出契约改了，下游没改。** 现象：prompt 把 JSON 改成 Markdown 表格，解析器全量失败，错误率 100%。→ 兜底：输出契约段落的变更必须联动下游测试；CI 里用「解析器 + 样例输出」做契约测试（第 48 章）。
- [ ] **事故 G：提示词膨胀，成本悄悄涨 30%。** 现象：三个月内平均 prompt 长度从 1.8k 涨到 2.6k，账单涨 30% 无人察觉。→ 兜底：`max_cost_usd_per_call` 门禁 + 每次 PR 输出「token 变化」报告 + profile 预算告警（第 15 章）。
- [ ] **事故 H：缓存 key 没带 prompt 版本。** 现象：v37 上线后，语义缓存仍在返回 v36 的答案，看起来「改了没用」。→ 兜底：所有缓存 key 必须含 `prompt_id@rev` + `content_hash`（第 24 章）。

---

## 16.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 三位一体 | 代码（契约/测试/diff）+ 配置（热更/灰度/回滚）+ 资产（owner/评测/ROI） |
| 成本量级 | 500 token 冗余 × 6 次调用 × 8.6M Run/天 ≈ 一年 530 万美元 |
| 变更频率 | 240 个模板 × 3.2 次/周 ≈ 110 次/天 → 走发版流程物理上不可行 |
| 版本 | `rev`（顺序）+ `content_hash`（同一性），**不要 semver** |
| 不可变 | 正文与 schema 存进 Registry 后只读，改动只能建新 rev |
| 变量契约 | `additionalProperties:false`；缺变量**必须报错，绝不渲染成空串** |
| diff | 六层：契约/参数/结构/语义/示例/输出契约；契约层变更需第二人 approve |
| A/B 分流 | 键必须稳定（tenant / conversation），**绝不用 run_id** |
| 门禁五检 | schema diff、占位符一致性、渲染冒烟、评测回归、成本延迟 |
| 热更边界 | 改文案可以热更；改变量/改输出契约**必须发版**，且热更接口要物理拒绝 |
| 双轨 | 一条流水线两套产物；`prompt_id@rev + hash` 必须进 Trace 和缓存 key |
| 一句话 | **提示词是唯一一种「改一个字符就能改变公司现金流」的配置。** |

---

## 16.10 自检题

1. **为什么提示词不能用 semver（`v1.2.3`）来管版本？**
   > 要点：semver 的前提是「能客观判断改动的兼容性级别」，而提示词做不到——把「必须」改成「应当」是 patch 还是 minor？没有标准，最后变成命名法辩论。而且 semver 无法回答两个关键问题：「v1.2.3 的确切内容是什么」（会被后续版本覆盖）和「内容是否完全相同」（两个分支可能产出同一个语义但不同 semver）。正确做法是 `rev`（单调递增，管顺序）+ `content_hash`（管同一性），并用 `UNIQUE (content_hash)` 自动拦掉内容重复的 rev。

2. **你的模板里 `{{order.delivered_days}}` 没传值，最坏的结果是什么？**
   > 要点：最坏的不是报错，而是**静默渲染成空串**。规则会变成「仅当   <= 7 且  == false 时，判定为 REFUNDABLE」，模型会忽略残缺条件，按「用户提退款就退」处理——行为和你的意图正好相反，而且日志全绿、错误率 0%。所以渲染器必须「缺变量即失败」，绝不允许降级为空串；同时 CI 要用每个评测 case 的 fixtures 做渲染冒烟，把这类问题挡在 PR 阶段。

3. **「提示词能热更，所以不用走评测」这句话错在哪？**
   > 要点：混淆了「发布速度」和「发布资格」。热更解决的是「每天 110 次变更不可能都走 25 分钟发版」这个效率问题；门禁解决的是「这次变更值不值得发」这个资格问题。两者正交。真实事故的因果链是「因为能热更 → 所以跳过了门禁 → 所以改坏了没人知道」，而不是「热更本身有害」。正确做法是：热更通道强制检查 `gate_result` 存在且未过期，缺失即拒绝；紧急通道双人 approve + 强制 5% 灰度起步。

---

## 16.11 延伸阅读

- 上一章：[15. 上下文预算：token 会计与分配策略](15-context-budget.md)
- 下一章：[17. Agent 的角色、人格与边界（Persona Engineering）](17-persona-role-design.md)
- Langfuse 官方文档：Prompt Management（版本、label、客户端缓存与降级）
- promptfoo 官方文档：`promptfooconfig.yaml`、assert 类型（`is-json` / `javascript` / `llm-rubric` / `cost` / `latency`）、CI 集成（以官方最新版为准）
- JSON Schema 官方规范：draft 2020-12（`additionalProperties`、`format`）
- 相关：第 15 章（token 预算）、第 17 章（角色卡也是 prompt 资产）、第 24 章（缓存 key 必须含版本）、第 40 章（评测体系）、第 42 章（灰度与回滚）、第 43 章（策略中心）

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 语言/运行时细节：见 [../go/0-summary.md](../go/0-summary.md)
- 前置：第 15 章
- 下一章：第 17 章
