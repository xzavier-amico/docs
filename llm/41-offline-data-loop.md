# 41. 离线数据消费与回流

> **状态**：`已完成（初稿）` ｜ **所属**：第八部分 · 可观测、评估与数据闭环 ｜ **编号**：41 / 52
> **本章定位**：线上每天产生 180 万次 Run，其中藏着几千条「系统哪里不行」的证据。这一章讲**怎么把这些证据变成评测集、训练数据和修复动作**——以及为什么「删除」比「采集」难十倍。
> **读法**：§41.2 是数仓分层的落地表清单，§41.4 的失败聚类 SQL 可以直接跑；如果你只读一节，读 §41.6（隐私与删除传播）——它是唯一会让你被法务找上门的部分。

---

## 41.1 引子：一个 badcase 的三种命运

一个用户在周四下午被 Agent 气到了：问「发票能不能开专票」，Agent 答「可以，我们会为您开具增值税专用发票」，但实际上这个客户是小规模纳税人，**根本不能开专票**。用户点了踩，然后打了客服电话。

这条数据接下来会走向哪里？在三种团队里，它有三个完全不同的命运：

| 团队 | 发生了什么 | 三个月后 |
| --- | --- | --- |
| **A（无回流）** | 点踩记录躺在 `feedback` 表里，没人看。客服工单在另一个系统。 | 同一个错误又发生了 1,400 次，赔了 6 张发票的钱 |
| **B（有人工回流）** | 客服主管每周导出工单，人工挑 20 条，「凭感觉」加到提示词里 | 提示词从 800 字涨到 4,200 字，成本 +40%，效果没变好 |
| **C（有回流管道）** | T+0 进 badcase 池；T+1 被聚类到「税务资质类」失败簇（同簇还有 47 条）；T+2 自动生成 12 条评测样本；T+7 进 regression 集；下个版本发布时**门禁直接拦住了一个会犯同样错的提示词改动** | 这个错误再也没出现过 |

差别的不是「有没有数据」，是**「数据有没有被加工成可执行的约束」**。

> 🩸 一个残酷的数字：Aurora 上线第一年，`feedback` 表里累积了 **210 万条点踩**。团队真正处理过的不到 **3,000 条**（0.14%）。剩下 99.86% 的数据——**你有，但你等于没有**。

---

## 41.2 断崖：回流失效的五种形态

| # | 形态 | 症状 | 代价 |
| --- | --- | --- | --- |
| 1 | **只采集不消费** | 日志/事件/feedback 都在，没人查 | 存储成本逐年涨，决策仍靠感觉 |
| 2 | **消费但没有结构** | 靠人肉翻日志找 badcase | 一条 badcase 平均 25 分钟定位，一周只能处理 30 条 |
| 3 | **有结构但没有聚类** | badcase 池 5000 条，全是散的 | 看不出「这 5000 条其实是 12 个根因」 |
| 4 | **聚了类但不回灌** | 报告写了，评测集没变 | 同一个坑踩第四遍 |
| 5 | **回灌了但没管版本/隐私** | 训练集说不清来源；删号用户的数据还在样本里 | 合规事故（这一条最贵） |

第 5 条是本章的重心。因为**前面四条是效率问题，第五条是法律问题**。

---

## 41.3 结构图：数仓分层与回流闭环

```
┌─────────────────────────────────────────────────────────────────────────┐
│ ODS · 原始层（只追加，不修改，保留 30~90 天）                            │
│   ods_run_event        run/step/fragment 原始事件（Kafka → ClickHouse）  │
│   ods_tool_call        工具调用原始记录                                  │
│   ods_feedback         点赞/点踩/工单/转人工                              │
│   ods_audit_event      审计事件（含撤回，第 37 章）                       │
│   ods_llm_request      模型原始请求响应（含 token 用量，脱敏后）           │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │ dbt staging（清洗、去重、脱敏、类型规范）
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ DWD · 明细层（一实体一表，可追溯到原始事件 id）                          │
│   dwd_run            一次 Run 一行（≈ 39 章的 run_fact）                 │
│   dwd_step           一步一行                                            │
│   dwd_fragment       一个输出片段一行（内容哈希 + 判定 + 撤回状态）        │
│   dwd_tool_call      一次工具调用一行                                    │
│   dwd_retrieval_hit  一次召回命中文档一行（doc_id@version + 分数）        │
│   dwd_feedback       一次反馈一行（含来源、权重）                         │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │
                ┌───────────────┴────────────────┐
                ▼                                ▼
┌──────────────────────────────┐   ┌──────────────────────────────────────┐
│ DWS · 汇总层（按天/租户/场景）│   │ ADS · 应用层（喂给下游系统）           │
│   dws_run_daily              │   │   ads_badcase_pool      失败样本池     │
│   dws_failure_cluster        │   │   ads_eval_candidate    评测候选       │
│   dws_cost_daily             │   │   ads_sft_pool          微调正样本     │
│   dws_tool_health            │   │   ads_dpo_pairs         DPO 偏好对     │
│   dws_retrieval_quality      │   │   ads_adv_seed          对抗种子       │
└──────────────────────────────┘   └──────────────┬───────────────────────┘
                                                  │
        ┌─────────────────────────────────────────┼──────────────────────┐
        ▼                                         ▼                      ▼
┌──────────────────┐              ┌────────────────────────┐  ┌──────────────────┐
│ 评测集（第 40 章）│              │ 标注流水线（Argilla）   │  │ 训练数据          │
│ regression/adv   │              │ 双盲 + 仲裁 + 一致性    │  │ SFT / DPO / 蒸馏  │
└────────┬─────────┘              └───────────┬────────────┘  └────────┬─────────┘
         │                                    │                     │
         └──────────────┬─────────────────────┘                     │
                        ▼                                           ▼
              ┌──────────────────────┐                  ┌──────────────────────┐
              │ 发布门禁（第 40 章）  │                  │ 微调 / 蒸馏 → 新模型  │
              │ 拦住会犯同样错的改动  │                  │ → 评测 → 灰度（42 章）│
              └──────────────────────┘                  └──────────────────────┘
```

### 回流时效：每一层有每一层的 SLA

| 阶段 | 时效 | 触发 | 载体 | 为什么是这个时效 |
| --- | --- | --- | --- | --- |
| **事故样本入池** | T+0（分钟级） | 撤回 / 工单 / 严重点踩 / 安全事件 | `ads_badcase_pool` | 事故当天要能复查，慢了就丢上下文 |
| **失败聚类** | T+1（小时级） | 每日批 + 每 6 小时增量 | `dws_failure_cluster` | 聚类要成规模才有意义，但也不能等一周 |
| **评测候选生成** | T+1 | 聚类产出 + 强负反馈 | `ads_eval_candidate` | 赶得上本周的发布门禁 |
| **人工确认 → 进 regression** | T+2 ~ T+7 | 标注队列 | 评测集 v 号 | 人是最慢的一环，要给它缓冲 |
| **训练数据生产** | T+7 ~ T+14 | 月度批次 | `ads_sft_pool` / `ads_dpo_pairs` | 训练周期决定的 |
| **微调 / 蒸馏** | T+30 | 季度或按需 | 模型注册表 | 训练 + 评测 + 灰度，一轮一个月 |
| **删除传播** | T+0 ~ T+30 | 用户请求 / 租户解约 | 全链路 | **合规要求，最快的必须当天**（§41.6） |

> 💡 注意「事故样本 T+0」这一行。它的价值不在分析，在**「当天就要能回答客户『发生了什么』」**。所以 badcase 池不是分析工具，是**事故档案**。

---

## 41.4 关键设计

### 41.4.1 表设计：DWD 三张核心表

```sql
-- 1) 输出片段明细：回流的最小单位（一条样本 = 一个片段 + 它的上下文）
CREATE TABLE dwd_fragment (
    ts              DateTime64(3),
    tenant_id       LowCardinality(String),
    agent_type      LowCardinality(String),
    scenario        LowCardinality(String),
    run_id          String,
    msg_id          String,
    seq             UInt32,

    -- 内容：默认只存哈希与长度；开启采样留存的才存脱敏文本
    text_sha256     FixedString(64),
    text_len        UInt32,
    text_redacted   Nullable(String),          -- 仅 sampled_with_text=true 时有值

    -- 上下文指纹
    ctx_hash        FixedString(64),
    prompt_ver      LowCardinality(String),
    model_ver       LowCardinality(String),
    rolecard_ver    LowCardinality(String),
    retrieval_ids   Array(String),             -- doc_id@version

    -- 判定与结果
    status          Enum8('pending'=0,'committed'=1,'retracted'=2,'aborted'=3),
    audit_verdict   LowCardinality(String),
    audit_rules     Array(String),
    postproc_dropped Bool,
    grounding_contradicted UInt8,              -- claim 级矛盾数（第 38 章）

    -- 反馈
    feedback        Enum8('none'=0,'up'=1,'down'=2,'ticket'=3,'handoff'=4),
    feedback_weight Float32 DEFAULT 1.0,       -- 防刷权重

    -- 回流标记
    in_badcase_pool Bool DEFAULT false,
    in_eval_set     Bool DEFAULT false,
    in_train_set    Bool DEFAULT false,
    tombstoned      Bool DEFAULT false         -- 删除传播标记（§41.6）
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (tenant_id, ts, run_id, seq)
TTL ts + INTERVAL 365 DAY;

-- 2) 失败聚类：把 5000 条散样本压成 12 个根因
CREATE TABLE dws_failure_cluster (
    cluster_date    Date,
    cluster_id      UInt64,                    -- 稳定 id，跨天可比
    cluster_version UInt32,                    -- 聚类算法/参数版本
    label           String,                    -- 人工命名，如 "税务资质类"
    root_cause      LowCardinality(String),    -- stale_doc / tool_schema / persona / ...
    size            UInt32,
    tenant_breadth  UInt16,                    -- 涉及多少租户（广度比数量更重要）
    avg_severity    Float32,                   -- 严重度（赔付/投诉/安全加权）
    centroid        Array(Float32),            -- 聚类中心向量
    sample_frag_ids Array(UInt64),
    first_seen      DateTime,
    last_seen       DateTime,
    owner           String,
    status          Enum8('open'=0,'fixing'=1,'fixed'=2,'wontfix'=3)
)
ENGINE = ReplacingMergeTree(cluster_version)
PARTITION BY toYYYYMM(cluster_date)
ORDER BY (cluster_id, cluster_date);

-- 3) 数据集清单（血缘的根，见 §41.7）
CREATE TABLE dataset_manifest (
    dataset_name    LowCardinality(String),
    dataset_version String,
    created_at      DateTime,
    parent_versions Array(String),              -- 上游数据集版本
    n_samples       UInt32,
    content_hash    FixedString(64),            -- 全量样本 id 排序后的哈希 → 唯一指纹
    filter_sql      String,                     -- 构建它用的 SQL（可重放！）
    source_window   String,                     -- "2026-04-01..2026-05-01"
    dedup_method    LowCardinality(String),     -- minhash / exact / none
    pii_scan_result LowCardinality(String),     -- pass / redacted / quarantined
    approved_by     String,
    approved_at     Nullable(DateTime),
    tombstone_count UInt32 DEFAULT 0            -- 已从本数据集删除的样本数
)
ENGINE = MergeTree
ORDER BY (dataset_name, dataset_version);
```

### 41.4.2 失败聚类：把「5000 条散沙」压成「12 个根因」

聚类不是「跑个 k-means」就完了。**Agent 的失败有强烈的结构性，规则 + 向量要结合**：

```
第一步：规则打标（覆盖约 70%，几乎零成本）
  ├─ 撤回事件       → reason（hallucination / stale_source / policy_change / safety）
  ├─ 工具错误       → error_code + tool_name
  ├─ grounding 矛盾 → contradicted 的 claim 类型（数字/日期/条款/实体）
  ├─ 后处理 drop    → processor 名
  ├─ 意图 + 场景    → intent × agent_type
  └─ 是否转人工     → handoff

第二步：对「规则打不出标签」的剩余 30%，做向量聚类
  - 向量 = 用户问题 embedding（不是回答的）
  - 算法 = HDBSCAN（不用 k-means：簇数未知，且有大量噪声点）
  - 最小簇大小 = 5，噪声点单独归"零散"

第三步：人工命名 + 定根因（每周 30 分钟）
  - 只给 top 20 簇命名，覆盖 85% 的量
  - 每簇指定 owner
```

```sql
-- 规则聚类：按"规则标签组合"聚合，找出量最大的失败模式
SELECT
    arrayStringConcat(arraySort(groupUniqArray(
        arrayJoin(arrayConcat(audit_rules, [if(feedback='ticket','fb:ticket',''),
                                            if(status='retracted','st:retracted','')]))
    )), '|')                                        AS signature,
    count()                                         AS n,
    uniqExact(tenant_id)                            AS tenants,
    round(countIf(feedback IN ('ticket','handoff')) / count(), 3) AS escalate_rate,
    round(avg(feedback_weight), 2)                  AS avg_weight,
    groupUniqArray(5)(text_sha256)                  AS sample_hashes,
    min(ts) AS first_seen, max(ts) AS last_seen
FROM dwd_fragment
WHERE ts >= now() - INTERVAL 1 DAY
  AND tombstoned = false
  AND (status = 'retracted'
       OR feedback IN ('down','ticket','handoff')
       OR grounding_contradicted > 0
       OR postproc_dropped = true)
GROUP BY signature
HAVING n >= 5
ORDER BY tenants DESC, n DESC
LIMIT 50;
```

**排优先级不能只看数量，要看「广度 × 严重度」**：

```
priority = tenant_breadth^0.5 × avg_severity × log(1 + size)

其中 avg_severity：
  安全/合规 = 5.0
  承诺型幻觉（涉钱）= 4.0
  工具调用错误（有副作用）= 3.0
  事实错误 = 2.0
  语气/格式 = 1.0
```

Aurora 用这个公式后的效果：**修复前 12 个簇，覆盖了 78% 的负反馈量**；而按「数量排序」修前 12 个只覆盖 41%——因为量最大的簇往往是「语气不好」（严重度 1.0，但用户爱点踩）。

### 41.4.3 自动构建评测集：从 badcase 到 regression 样本

这是回流的**核心工序**。规则要写得像法律条文一样明确，否则样本质量会崩：

```yaml
# eval_candidate_rules.yaml —— 自动生成评测候选的规则
version: 5
target_dataset: regression

include:
  # 强信号：直接进候选
  - source: retraction
    where: status = 'retracted' AND audit_verdict = 'block'
    priority: P0
  - source: ticket
    where: feedback = 'ticket'
    priority: P0
  - source: safety
    where: arrayExists(r -> startsWith(r, 'safety.'), audit_rules)
    priority: P0
    target_dataset: adversarial        # 安全类进对抗集，不进 regression

  - source: contradictory
    where: grounding_contradicted > 0
    priority: P1
  - source: tool_error
    where: tool_status != 'ok' AND tool_tier IN ('write','dangerous')
    priority: P1

  # 弱信号：聚类后每簇取代表
  - source: cluster_representative
    where: cluster_size >= 5 AND cluster_status = 'open'
    take_per_cluster: 3
    priority: P2

  # 灰度/新版本：保证新配置有覆盖
  - source: version_slice
    where: prompt_ver = 'sp-v38' OR model_ver = 'gpt-x-2026-05'
    take: 50
    priority: P2

exclude:
  - tombstoned = true                      # 已删除（§41.6）
  - tenant_consent = false                 # 未授权用于评测
  - text_len < 20                          # 太短，无法判定
  - contains_pii_unredacted = true
  - duplicate_of_existing: {method: minhash, threshold: 0.85}

dedup:
  method: minhash
  shingle: 5                               # 5-gram
  threshold: 0.85
  also: embedding_cosine                   # 语义去重，阈值 0.92
  embedding_model: bge-m3

cap_per_cluster: 20                        # 单簇最多 20 条，防某一类刷满集
cap_per_tenant: 50
cap_per_day: 200

require_human_confirm: true                # 必须人工确认期望答案
confirm_sla_hours: 72
```

**去重是硬要求**。Aurora 上做过统计：不做去重时，2000 条「新样本」里只有 340 条是真正不同的模式（17%）。做了 MinHash + 语义双去重后，标注成本降了 68%。

### 41.4.4 训练数据生产：正样本比负样本难拿

**负样本（知道什么是错的）遍地都是；正样本（确定什么是对的）必须人工把关。**

| 池 | 来源 | 用途 | 数量级（Aurora / 月） | 关键关卡 |
| --- | --- | --- | --- | --- |
| **SFT 正样本** | 工单解决会话 + 👍 + 人工改写 | 微调「怎么说」 | 3,000~8,000 | grounding 全通过 + 人工确认 + 去重 |
| **DPO 偏好对** | 被撤回的回答 vs 更正后的回答 | 偏好对齐 | 1,000~2,000 | 必须同输入、同上下文，只有输出不同 |
| **拒答样本** | 对抗集命中 + 人工构造 | 学「什么时候不答」 | 500~1,000 | 必须覆盖「该拒」与「不该拒」两侧 |
| **工具轨迹** | 工具调用全成功的 Run | 学「怎么调工具」 | 5,000~15,000 | 参数正确 + 无重试 + 无截断 |
| **蒸馏数据** | 强模型输出 + 弱模型学习 | 降本 | 按需 | 必须过质量校验，见下 |

**DPO 对的构造是最精妙的一环**，而且它天然来自第 37 章的撤回数据：

```
DPO pair = {
  prompt:   当时的完整上下文（ctx_hash 对应的快照）
  chosen:   更正后的回答（correction 事件里的 text + 重生成的版本）
  rejected: 被撤回的原回答
}

要求：
  ① prompt 完全一致（否则你训的是"上下文差异"，不是"输出偏好"）
  ② chosen 必须通过 grounding（否则你在教模型偏好另一个幻觉）
  ③ 长度差异不能过大（chosen 比 rejected 长 3 倍 → 模型学到"越长越好"，第 40 章的 length bias）
  ④ 同一个 prompt 最多 1 对（防止过拟合到单个案例）
```

> 🩸 **关于蒸馏与 model collapse**：不要用「A 模型的输出」去训「B 模型的输出」，然后再拿 B 的输出训 A——这叫自噬（self-consumption），会系统性放大两者的共同缺陷、丢失分布尾部。我们的规则是：**蒸馏数据必须有 ≥10% 的人工确认正样本混入，且每轮蒸馏后必须在 Gold 集上验证「长尾场景」不掉分**。详见第 47 章（蒸馏是成本优化的最后一招）。

### 41.4.5 标注流水线：双盲、仲裁、一致性

```yaml
# argilla 标注队列配置（示意）
workspace: aurora-quality
datasets:
  - name: regression_confirm_v2026_05
    task: QuestionAnswering
    guidelines: |
      1. 判断回答是否解决了用户意图（不是"看起来专业"）
      2. 若回答含具体数字/日期/条款，必须核对 references 字段
      3. 不确定的选 "unsure"，不要猜
      4. 不要因为格式好看而给高分
    overlap: 2                  # 每条 2 人独立标 → 可算一致性
    min_submitted: 2
    batch_size: 50              # 一次 50 条，控制疲劳（超过 50 条一致率掉 8pp）
    assignment: stratified      # 按 difficulty 分层分配，避免人只标简单题
    arbitration:
      enabled: true
      trigger: disagreement     # 两人不一致 → 第三人仲裁
      assignee_pool: [lead_1, lead_2]

quality_gates:
  krippendorff_alpha_min: 0.67        # 三分类以上用 alpha
  cohen_kappa_min: 0.61               # 二分类用 kappa
  gold_injection_rate: 0.05           # 混入 5% 已知答案，检测标注员质量
  gold_accuracy_min: 0.90             # 低于此值的标注员需重新培训
  max_daily_per_annotator: 200
```

| 指标 | 阈值 | 不达标怎么办 |
| --- | --- | --- |
| Krippendorff's α | ≥ 0.67 | 改 guidelines（最常见原因：规则有歧义） |
| 标注员 gold 准确率 | ≥ 90% | 培训 / 移出队列 |
| 单条平均耗时 | 60~180 秒 | <30 秒 = 在敷衍；>300 秒 = guidelines 有问题 |
| 仲裁率 | < 20% | > 20% = 任务定义不清 |
| `unsure` 比例 | < 15% | > 15% = 参考资料不足，先补 context |

> 💡 「混入 5% 已知答案」是标注质量管理里性价比最高的一招。它同时解决两个问题：检测标注员质量、给标注员正向反馈（看到自己做对了）。成本几乎为零。

### 41.4.6 隐私与合规过滤：删除比采集难十倍

**这一节是本章唯一会让你被法务找上门的部分。**

问题不是「采集时脱敏」（那个相对简单），而是**「用户要求删除后，怎么从已经散布到 5 个数据集、2 个微调模型里的数据中删掉」**。

```
一条用户会话数据的生命周期：

  ODS（原始）  ──▶ DWD（明细） ──▶ ADS（样本池） ──▶ 评测集 ──▶ 门禁/回归
       │              │                │                │
       │              │                │                └─▶ 数据集 manifest（含该样本 id）
       │              │                └─▶ 训练集 ──▶ 微调模型（权重里！）
       │              └─▶ 特征/向量库
       └─▶ 对象存储（原始请求响应）

用户行使删除权时，你要处理六处，且第 6 处（模型权重）几乎不可能真正"删除"。
```

**务实的三层策略**：

| 层 | 对象 | 做法 | 时效 | 可验证性 |
| --- | --- | --- | --- | --- |
| **L1 硬删除** | ODS / DWD / 向量库 / 对象存储 | 物理删除 + 分区重建 | T+0（24h 内） | ✅ 可查 |
| **L2 样本级 tombstone** | ADS 样本池 / 评测集 / 训练集 | 打 `tombstoned=true` + 重算 `manifest.content_hash` + 下次构建自动排除 | T+0 标记，T+1 重算 | ✅ 可查（样本不在新版本里） |
| **L3 模型级标记 + 重训** | 微调模型权重 | 在模型注册表标记「训练数据含已删除用户」+ 排入下次重训 | T+30 | ⚠️ 只能"标记 + 承诺重训" |

> ⚠️ **必须对客户/监管诚实**：L3 无法做到「立即从权重中删除」。行业通行做法是「标记 + 在下一个训练周期排除 + 若影响重大则重训」。**别承诺做不到的事**——这比不做更危险。

```sql
-- L2 删除传播：把用户 u 的所有样本打墓碑，并重算受影响数据集
-- ① 找出受影响的数据集
SELECT dataset_name, dataset_version, count() AS affected
FROM ads_sample_lineage                    -- 样本 → 数据集 的映射表
WHERE user_id_hash = sha256('u_88231')
GROUP BY dataset_name, dataset_version;

-- ② 打墓碑（append-only 的墓碑事件表，不是 UPDATE）
INSERT INTO sample_tombstone (ts, user_id_hash, sample_id, reason)
SELECT now(), 'u_88231', sample_id, 'gdpr_erasure'
FROM ads_sample_lineage WHERE user_id_hash = sha256('u_88231');

-- ③ 下游所有数据集构建 SQL 必须带这个 join（硬约束，写进 dbt macro）
--    LEFT ANTI JOIN sample_tombstone USING (sample_id)
```

dbt 里把这件事做成**不可绕过的 macro**，而不是「记得加」：

```sql
-- models/marts/ads_eval_candidate.sql
{{ config(materialized='incremental', unique_key='sample_id') }}

WITH src AS (
    SELECT * FROM {{ ref('dwd_fragment') }}
    WHERE ts >= '{{ var("window_start") }}'
      AND tombstoned = false
      AND feedback IN ('down','ticket','handoff')
),

-- 强制：任何样本集构建都必须过墓碑过滤与 consent 校验
clean AS (
    SELECT s.*
    FROM src s
    {{ exclude_tombstoned('s') }}          -- macro: LEFT ANTI JOIN sample_tombstone
    {{ require_consent('s') }}             -- macro: JOIN tenant_consent WHERE eval_ok
)

SELECT
    {{ dbt_utils.generate_surrogate_key(['run_id','seq']) }} AS sample_id,
    s.tenant_id, s.run_id, s.seq,
    s.text_sha256, s.text_redacted,
    s.feedback, s.audit_rules
FROM clean s
```

**其他必须做的过滤**（每条都对应一类真实事故）：

| 过滤项 | 规则 | 不做会怎样 |
| --- | --- | --- |
| PII | 输出侧正则 + NER 双检，命中即 `quarantined`，不进任何池 | 训练集里带身份证号 → 模型可能背出来 |
| 密钥/凭据 | 正则扫 `sk-` / `AKIA` / 私钥头，命中即隔离 + 告警 | 密钥进了训练数据（且可能被模型复述） |
| 租户 consent | 合同里注明「数据不用于模型训练」的租户，其数据只能用于评测/统计 | 违约 |
| 跨境 | 按数据驻留要求分区，跨区样本不得混入同一数据集 | 违反数据驻留（第 31 章） |
| 内部机密 | 内部员工会话、A/B 实验流量默认排除 | 内部信息泄露到模型 |
| 未成年人/敏感场景 | 直接排除 | 合规红线 |

### 41.4.7 数据版本与血缘：能回答「这个模型用了谁的数据」

血缘不是画个图好看，它要能回答三个具体问题：

1. 这个模型（`ft-2026-05-v3`）是用哪些数据集训的？→ 数据集 → 样本 → 原始 run → 租户/用户
2. 这个评测集（`regr-v2026.05.1`）比上一版多了什么、少了什么？→ diff
3. 用户 u 要求删除 → 影响哪些数据集和模型？→ 反查（§41.4.6）

```sql
-- 血缘查询：某模型的血缘链（三层递归）
WITH RECURSIVE lineage AS (
    SELECT dataset_name, dataset_version, 0 AS depth
    FROM model_dataset_usage
    WHERE model_id = 'ft-2026-05-v3'
  UNION ALL
    SELECT m.parent_versions[1], m.parent_versions[2], l.depth + 1
    FROM dataset_manifest m
    JOIN lineage l ON m.dataset_name = l.dataset_name
    WHERE l.depth < 3 AND length(m.parent_versions) >= 2
)
SELECT * FROM lineage;
```

**数据集版本的三条规则**：

| 规则 | 为什么 |
| --- | --- |
| 版本号必须由 `content_hash` 派生，不能手填 | 手填的版本号会骗人：内容改了版本没改 |
| `filter_sql` 必须存进 manifest | 否则你无法重建同一版本（数据在变，SQL 不变才能重放） |
| 任何进入门禁/训练的数据集必须 `approved_by` 非空 | 「谁批准的这个数据集进训练」是审计必问项 |

```go
// 数据集构建时强制算指纹（少了这个，血缘就是装饰）
func (b *Builder) Publish(ctx context.Context, ds Dataset) (*Manifest, error) {
    ids := ds.SortedSampleIDs()             // 必须排序，否则哈希不稳定
    h := sha256.New()
    for _, id := range ids {
        h.Write([]byte(id))
        h.Write([]byte{'\n'})
    }

    m := &Manifest{
        Name:          ds.Name,
        Version:       fmt.Sprintf("%s-%s", ds.Name, hex.EncodeToString(h.Sum(nil))[:8]),
        NSamples:      len(ids),
        ContentHash:   hex.EncodeToString(h.Sum(nil)),
        FilterSQL:     ds.BuildSQL,          // 可重放
        SourceWindow:  ds.Window,
        DedupMethod:   ds.DedupMethod,
        PIIScanResult: ds.PIIScan,
        ParentVersions: ds.Parents,
    }
    if m.PIIScanResult != "pass" && m.PIIScanResult != "redacted" {
        return nil, fmt.Errorf("dataset %s failed PII scan: %s", ds.Name, m.PIIScanResult)
    }
    return m, b.store.Insert(ctx, m)
}
```

---

## 41.5 Java / 分布式经验对照

| 你熟悉的东西 | 回流的对应物 | 会咬人的差异 |
| --- | --- | --- |
| Hive / Spark 离线数仓 | ClickHouse + dbt | ClickHouse 不适合大 join；分层要更扁，宽表更多 |
| 每日全量重跑 | 增量 + 分区重建 | 删除传播要求「重算受影响分区」，不是全量重跑 |
| binlog 同步 | 事件流（Kafka → ODS） | 事件是 append-only，天然适合；但**片段文本含 PII**，入仓前就要脱敏 |
| 数据脱敏（静态） | 双轨脱敏（入口 + 出口） | Agent 的数据里 PII 可能在**模型生成的文本**里（第 38 章） |
| 主数据管理（MDM） | 数据集 manifest | 多了 `content_hash` 派生版本 + `filter_sql` 可重放 |
| 血缘（Atlas/DataHub） | 三层血缘 + 墓碑传播 | 多了「模型权重」这一层，传统血缘工具没有 |
| 离线任务调度（Azkaban/Dolphin） | dbt + Airflow | 关键是**事故样本 T+0 通道**：不能等每日批 |
| 数据质量监控 | 标注一致性 + gold 注入 | 传统 DQ 看空值/重复；这里看「人标得准不准」 |

最大差异：**传统数仓的「删除」是 DBA 点一下；Agent 数据闭环的「删除」是一次跨 6 个系统、跨 30 天、且最后一环（模型权重）无法真正完成的传播过程。** 这件事必须在架构第一版就设计进去，后补是补不上的。

---

## 41.6 开源坐标

| 项目 | 有什么 | 缺什么 |
| --- | --- | --- |
| **ClickHouse** | 列存、高压缩、`Array`/`LowCardinality`/`TTL`、`cosineDistance` 等向量函数；明细分析的主力 | 大 join 弱；向量检索不如专用库（第 45 章） |
| **dbt** | 分层建模、`ref()` 依赖图、增量模型、macro（可做强制过滤）、文档与测试 | 不含调度（要配 Airflow/Dagster）；血缘是「表级」，样本级要自己加 |
| **Argilla** | 标注 UI、多标注者、overlap、一致性统计、与 Hugging Face 生态集成 | 队列编排与 SLA 要自己写；gold 注入需自定义 |
| **Airflow / Dagster** | 调度、重试、回填 | 缺「T+0 事故通道」（要单独的事件触发链路） |
| **MinHash / SimHash 库**（datasketch 等） | 近重去重 | 阈值要按业务调；语义去重要另配 embedding |
| **Presidio** | PII 识别 | 中文与行业实体要自定义 |
| **Langfuse Datasets** | 从 trace 直接建数据集 | 版本/血缘/删除传播较弱 |
| **Hugging Face `datasets`** | 数据集版本与 fingerprint | 与你的数仓/血缘系统要打通 |

> 🎯 结论：**这一层的开源组件都很成熟，但没有一个"开箱即用的 Agent 数据闭环"。** 你要自研的是三样：① 分层表结构与构建 SQL（dbt 可以做）；② 失败聚类与优先级公式（业务强相关）；③ **删除传播与样本级血缘**（没有任何开源项目替你做）。第三样最不出彩，但它是最值钱的。

---

## 41.7 事故清单

- [ ] **事故 A：训练集里带了 PII**。现象：微调后模型偶尔"背出"手机号。
  → 根因：入仓脱敏只做了输入侧，模型生成的文本没扫。
  → 兜底：入池前双轨 PII 扫描（正则 + NER），命中即 `quarantined`，且 `PIIScanResult != pass` 时 `Publish` 直接报错。
- [ ] **事故 B：删号用户的数据还在评测集里**。现象：客户法务函问「为什么我们的数据还在你们的 benchmark 里」。
  → 根因：删除只做了 ODS/DWD，ADS 样本池与评测集没传播。
  → 兜底：`sample_tombstone` + 所有构建 SQL 强制 `exclude_tombstoned()` macro + manifest 重算。
- [ ] **事故 C：评测集里全是重复样本**。现象：2000 条"新样本"其实只有 340 个模式，模型过拟合。
  → 根因：没做去重。
  → 兜底：MinHash(0.85) + embedding cosine(0.92) 双去重；`cap_per_cluster: 20`。
- [ ] **事故 D：聚类把根因埋了**。现象：按数量排序修了 top 12，投诉只降 20%。
  → 根因：量最大的是「语气不好」，严重度低；涉钱幻觉量小但严重度高。
  → 兜底：`priority = breadth^0.5 × severity × log(1+size)`，严重度权重显式化。
- [ ] **事故 E：标注一致性崩了**。现象：同一批样本两个人标出来 62% 一致，全在吵。
  → 根因：guidelines 有歧义（"是否解决了意图"没定义），且标注员只看单条判断疲劳。
  → 兜底：改 guidelines + `batch_size: 50` + gold 注入 5% + α<0.67 时暂停入集。
- [ ] **事故 F：日均回流量把数仓打爆**。现象：每日聚类任务从 20 分钟涨到 4 小时，影响早班报表。
  → 根因：`dwd_fragment` 全量扫 + 没分区裁剪 + 聚类在明细层跑。
  → 兜底：聚类在 DWS 层（预聚合）+ 严格分区裁剪 + 增量（每 6 小时只算最近窗口）。

---

## 41.8 一页速记

| 关键词 | 结论 |
| --- | --- |
| 核心问题 | 不是「有没有数据」，是「数据有没有变成可执行的约束（评测集/训练集/修复）」 |
| 分层 | ODS（30~90 天）→ DWD（明细，365 天）→ DWS（聚合）→ ADS（样本池：badcase/eval/sft/dpo） |
| 时效 | 事故样本 T+0；聚类 T+1；进 regression T+2~T+7；训练 T+7~T+14；模型 T+30 |
| 聚类 | 规则打标覆盖 ~70%，剩余用 HDBSCAN 向量聚类；优先级 = `breadth^0.5 × severity × log(1+size)` |
| 严重度 | 安全 5 / 涉钱幻觉 4 / 副作用工具错 3 / 事实错 2 / 语气 1 |
| 去重 | MinHash(0.85) + 语义(0.92) 双去重；不去重时"新样本"只有 17% 是真新的 |
| 正样本 | 稀缺，必须过 grounding + 人工确认；👍 不可直接进训练集 |
| DPO | 天然来自撤回：`chosen=更正后` / `rejected=被撤回`；但 prompt 必须完全一致 |
| 蒸馏 | 每轮必须混 ≥10% 人工正样本，并在 Gold 集验长尾，防 model collapse |
| 标注 | overlap=2 + 仲裁 + gold 注入 5% + α≥0.67；单批 ≤50 条 |
| 血缘 | 版本号由 `content_hash` 派生；`filter_sql` 必须存；`approved_by` 必须非空 |
| 删除 | 三层：L1 硬删（T+0）/ L2 样本墓碑（T+0 标记）/ L3 模型标记+重训（T+30）；**别承诺做不到的** |
| 一句话 | **能回答「这个模型用了谁的数据、删得掉吗」，才叫数据闭环；否则只是数据堆积。** |

---

## 41.9 自检题

1. **你的 badcase 池里有 8,000 条样本，团队每周只能处理 30 条。你会先做什么？**
   > 要点：先做**聚类 + 优先级排序**，不是「加快处理速度」。8000 条里大概只有 15~25 个根因簇，修一个簇等于修几百条。用「规则打标 + HDBSCAN」聚类，用 `breadth^0.5 × severity × log(1+size)` 排序，只给 top 12 簇命名并指定 owner。这样每周修 1~2 个簇，两个月能覆盖 78% 的负反馈量——比每周处理 30 条散样本快 20 倍。

2. **用户行使删除权，要求「删除我的所有数据」。你的微调模型已经训完了，怎么办？**
   > 要点：分三层，且必须对客户诚实。L1 硬删 ODS/DWD/向量库/对象存储（24h 内）；L2 给样本打 tombstone、重算受影响数据集的 `content_hash`、后续构建自动排除；L3 在模型注册表标记「训练数据含已删除用户」，排入下次重训（T+30）。**不能声称"已从模型权重中删除"**——做不到。正确姿势是：标记 + 承诺重训 + 若影响重大则立即重训。

3. **为什么 DPO 的训练对里，`prompt` 必须完全一致，而"被撤回的回答 vs 更正后的回答"天然满足这个条件？**
   > 要点：DPO 学的是「在同一输入下，偏好哪个输出」。如果 prompt 不同，模型学到的是「上下文差异」而不是「输出偏好」。撤回场景天然满足：`rejected` 是原 Run 的输出，`chosen` 是用**同一个 ctx_hash 快照**重新生成的（第 37 章 R3 rebase 就是这个机制）。同时要注意两个坑：`chosen` 必须过 grounding（否则教模型偏好另一个幻觉），且长度差异不能过大（否则学到"越长越好"）。

---

## 41.10 延伸阅读

- 上一章：[40. 评估体系](40-evaluation-system.md)（回流产物最终进评测集与门禁）
- 下一章：[42. 模型与提示词的迭代上线](42-model-release.md)（数据集 → 微调 → 灰度 → 回流的最后一环）
- 相关：[39. 可观测层](39-observability.md)（`run_fact` 是 ODS/DWD 的上游）、[31. 合规审计](31-compliance-audit.md)（留存期、被遗忘权、数据驻留）、[37. 输出审计与撤回](37-output-audit-retraction.md)（撤回数据是最高价值的 DPO 来源）、[45. 存储架构](45-storage-architecture.md)（ClickHouse 分区与 TTL）、[47. 成本工程](47-cost-engineering.md)（蒸馏的 ROI 与风险）
- 外部：ClickHouse 文档（`ReplacingMergeTree` / `TTL` / `cosineDistance`）、dbt 文档（incremental models / macros / `dbt_utils`）、Argilla 标注工作流、HDBSCAN 论文与实现、MinHash/LSH 去重原理
- 总纲：[0-summary.md](0-summary.md)
- 语言/运行时细节：见 [../go/0-summary.md](../go/0-summary.md)
