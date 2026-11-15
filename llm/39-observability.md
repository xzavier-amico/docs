# 39. 可观测层：一次 Run 的完整解剖

> **状态**：`已完成（初稿）` ｜ **所属**：第八部分 · 可观测、评估与数据闭环 ｜ **编号**：39 / 52
> **本章定位**：没有这一层，Agent 平台是一个「只能祈祷」的分布式系统——出问题你说不出为什么，改完你说不出好了多少。这一章给出**能直接落库的 trace schema、必采字段、采样策略和看板清单**。
> **读法**：§39.3 的 span 模型是地基，§39.5 的采样策略是最容易被做错的地方（做错了你的指标全在骗你）；看板清单在 §39.6，可以直接拿去建 Grafana/自建面板。

---

## 39.1 引子：23:40，成功率正常，投诉翻了 4 倍

值班手机响的时候，你看了一眼大盘：

```
Run 成功率        99.31%   ✅ 正常
P99 延迟          14.2s    ✅ 正常
错误率            0.69%    ✅ 正常
─────────────────────────────
客诉量（过去 1h）  比基线高 4.1×  ❌
```

四个指标里三个绿的，但投诉在暴涨。你打开日志，看到的是：

```
2026-05-11T23:12:04Z INFO run completed run_id=run_8f3a dur=12.4s status=200 tokens=3120
2026-05-11T23:12:07Z INFO run completed run_id=run_9a1c dur=11.8s status=200 tokens=2870
```

**这两行日志里，藏着你想知道的全部答案之外的一切**：这次 Run 走了哪个模型版本？提示词是哪版？检索命中了哪三篇文档、分数多少？工具调用是不是降级到了备用供应商？输出被后处理改了几处？审核判定是什么？这段回答被撤回过吗？

你不知道。所以接下来 3 小时你会做这些事：

| 动作 | 耗时 | 结果 |
| --- | --- | --- |
| 捞用户投诉的会话 id，翻日志 | 40 min | 找到了 6 个会话 |
| 猜是「模型厂商升级了」 | 20 min | 错，版本号没变 |
| 猜是「知识库索引出问题」 | 30 min | 半对：索引更新了但检索没重排 |
| 找 DBA 查那张没有索引的表 | 60 min | 找到了：`retrieval_log` 表按时间查，扫了 8 亿行 |
| 复盘写报告 | 60 min | 「疑似检索质量问题，待进一步定位」 |

> 🩸 **这一晚的成本不是 3 小时人力，是「你的团队从此不敢发版」。** 一个不能解释自己行为的系统，只能靠「少改」来求稳；而 Agent 平台的竞争力恰恰来自「敢改、改得快」。

**根因很简单**：检索服务的索引在 22:50 完成了一次重建，rerank 模型的热加载失败后**静默降级到了纯向量召回**，召回质量掉了一档，但没有报错、没有指标、没有 trace。它「成功」了，只是成功得很差。

这件事只有一种防线能拦住：**把「检索用的什么策略、rerank 有没有生效」变成 trace 上的一个属性，并且能被切片查询。** 这就是本章要建的东西。

---

## 39.2 断崖：没有可观测，你在为什么付钱

| # | 缺失的东西 | 你会付的代价 | 量化 |
| --- | --- | --- | --- |
| 1 | 没有片段级 trace | 无法定位「哪一步坏了」，只能整体回滚 | 平均故障定位时间（MTTD）从 8 min 涨到 90 min |
| 2 | 没有版本号入 trace | 线上效果无法复现，评测与线上对不上 | 每次灰度事故多花 1~2 天排查 |
| 3 | 没有成本归因 | 算不出每租户/每场景成本，无法定价 | 毛利算不清，第 47 章整章作废 |
| 4 | 指标被采样污染 | 你在用有偏数据做决策 | 采样 1% 时，P99 误差可达 ±40% |
| 5 | 日志当 trace 用 | 存储成本爆炸且查不动 | 全量日志 64 GB/日，查询扫表 8 亿行 |
| 6 | 只有「成功/失败」二元 | 看不到「降级后的成功」 | 静默降级平均存活 6 天才被发现 |

第 6 条是最阴的：**Agent 系统里最危险的故障不是失败，是「带着降级继续跑」**。fallback 到备用模型、rerank 失效、缓存误命中、上下文被截断——它们全部返回 200。

---

## 39.3 结构图：Trace 模型与采集链路

### Trace 树：Run → Step → 六类 span

```
trace_id = run_id
│
└─ span: run                                  [根 span]
   │  gen_ai.operation.name=invoke_agent
   │  aurora.run.id, aurora.tenant.id, aurora.agent.type
   │  aurora.input.tokens=25_100, aurora.output.tokens=1_240
   │  aurora.cost.usd=0.0181, aurora.status=ok
   │  aurora.retraction.count=0
   │
   ├─ span: preprocess                        [管道]
   │  │  aurora.normalize.changed=true, aurora.injection.score=0.02
   │  │  aurora.pii.inbound=2, aurora.route.decision=kb+tool
   │  │
   ├─ span: step 1                            [Step]
   │  │  aurora.step.index=1, aurora.step.kind=plan
   │  │
   │  ├─ span: retrieval
   │  │  │  aurora.retrieval.strategy="hybrid+rerank"   ← 就是这一条能救命
   │  │  │  aurora.retrieval.top_k=8, aurora.retrieval.rerank.applied=true
   │  │  │  aurora.retrieval.max_score=0.83, aurora.retrieval.empty=false
   │  │  │  aurora.retrieval.index_version="idx-2026-05-11"
   │  │  └─ event: docs [{id, version, score}] × 8
   │  │
   │  ├─ span: llm.call  "chat gpt-x-2026-02"
   │  │  │  gen_ai.system=openai, gen_ai.request.model=..., gen_ai.response.model=...
   │  │  │  aurora.prompt.version="sp-v37", aurora.rolecard.version="rc-v12"
   │  │  │  gen_ai.usage.input_tokens=25100, output_tokens=420
   │  │  │  aurora.ttft.ms=1180, gen_ai.response.finish_reasons=["tool_calls"]
   │  │  │  aurora.cache.prefix_hit=true
   │  │  └─ event: tool_calls_requested [{name, args_sha256}]
   │  │
   │  └─ span: tool.call  "get_order"
   │     │  gen_ai.tool.name=get_order, gen_ai.tool.call.id=call_7f
   │     │  aurora.tool.tier=read, aurora.tool.idempotency_key=...
   │     │  aurora.tool.result.bytes=1840, aurora.tool.truncated=false
   │     └─ event: error? (code=timeout, retry=1)
   │
   ├─ span: step 2 … N                        [同上]
   │
   ├─ span: postprocess                       [管道]
   │  │  aurora.postproc.chain=["schema","grounding","pii_out","brand"]
   │  │  aurora.postproc.dropped=0, aurora.postproc.flags=["degraded:brand"]
   │  │  aurora.grounding.claims=11, contradicted=0, not_found=2
   │  │
   ├─ span: audit                             [审计]
   │  │  aurora.audit.verdict=flag, aurora.audit.risk=0.31
   │  │  aurora.audit.rules=["policy.stale_doc_hint"]
   │  │  aurora.audit.auditor="classifier:v3", aurora.audit.latency_ms=214
   │  │
   └─ span: billing                           [计费]
      │  aurora.billing.pretoken=31200, aurora.billing.settled=31200
      └─ aurora.billing.amount_usd=0.0181
```

**注意三件事**：
1. `retrieval` 是 `llm.call` 的**兄弟还是子节点**，取决于你的实现；这里选「同属 step」——因为它是「为这次调用准备上下文」的动作，不是调用的内部步骤。
2. `audit` 和 `billing` 是 **Run 级的后置 span**，不是 step 级的。撤回事件（第 37 章）会作为 Run span 上的 event 追加。
3. **`aurora.retrieval.strategy` 这种「策略生效情况」的属性，比任何耗时数字都值钱**。因为它回答的是「系统是不是按你以为的方式在跑」。

### 采集链路：三段分离

```
  Agent-Server                       Collector                   存储/查询
┌───────────────┐              ┌──────────────────┐         ┌──────────────────┐
│ OTel SDK      │  OTLP/gRPC   │ OTel Collector   │         │ Trace 后端       │
│ span 批量导出 │─────────────▶│ ├ tail_sampler   │────────▶│ (Tempo/Jaeger/   │
│ (BatchSpanPr) │              │ ├ redact (PII)   │         │  Langfuse/Phoenix)│
└───────────────┘              │ ├ batch          │         │ 保留 7~30 天      │
                               │ └ attributes_    │         └──────────────────┘
                               │    processor     │
                               └────────┬─────────┘
                                        │ 全量（不采样）
                                        ▼
                               ┌──────────────────┐         ┌──────────────────┐
                               │ Metrics 聚合      │────────▶│ Prometheus/VM    │
                               │ (OTel metrics)   │         │ 保留 13 月        │
                               └──────────────────┘         └──────────────────┘
                                        │ 全量（不采样）
                                        ▼
                               ┌──────────────────┐         ┌──────────────────┐
                               │ 事件流 Kafka      │────────▶│ ClickHouse       │
                               │ (run/step/frag)  │         │ 明细 + 看板       │
                               └──────────────────┘         └──────────────────┘
                                        │
                                        │ 审计子集（不可变）
                                        ▼
                               ┌──────────────────┐         ┌──────────────────┐
                               │ 审计链（哈希链）  │────────▶│ WORM 对象存储     │
                               │ 见第 31 章        │         │ 保留 3 年         │
                               └──────────────────┘         └──────────────────┘
```

> ⚠️ **Trace 和审计必须物理分开**。它们字段重叠 80%，但要求正相反：trace 是「7 天、采样、可丢」；审计是「3 年、全量、不可篡改」。混在一张表里的团队，最后一定会在「采样导致取证缺证」和「全量导致存储爆掉」之间反复横跳。详见第 31 章。

### 四种信号的分工

| 信号 | 回答什么问题 | 采样 | 保留 | 成本量级 |
| --- | --- | --- | --- | --- |
| **Trace** | 「这一次 Run 里每一步发生了什么」 | 头部 5~20% + 尾部保失败 | 7~30 天 | GB/日 |
| **Metrics** | 「整体在变好还是变坏」 | **绝不采样**（聚合） | 13 月 | MB/日 |
| **Event 明细**（ClickHouse） | 「按租户/场景/版本切片看质量与成本」 | 全量（业务事件量可控） | 90~180 天 | 10~100 GB/日 |
| **Log** | 「代码级异常与堆栈」 | 全量 + 错误必留 | 7 天热 / 90 天冷 | GB/日 |
| **Audit** | 「当时到底发生了什么，能否取证」 | **全量、不可变** | 3 年 | 见第 31 章 |

**边界规则**：日志里**不要**打 token 数、成本、模型版本这些结构化字段——它们属于 trace 属性或 event 明细。日志只放「异常、堆栈、状态迁移」。把日志当 trace 用是 Agent 平台最常见的存储灾难。

---

## 39.4 关键设计

### 39.4.1 `gen_ai.*` 语义约定（以官方文档最新版为准）

OpenTelemetry 的 GenAI 语义约定是目前唯一跨厂商的标准，**能用就用，能对齐就对齐**——因为你的可观测后端（Langfuse / Phoenix / 商业 APM）都认它：

| 属性 | 类型 | 说明 | 必采 |
| --- | --- | --- | --- |
| `gen_ai.operation.name` | string | `chat` / `text_completion` / `embeddings` / `execute_tool` / `invoke_agent` | ✅ |
| `gen_ai.provider.name`（旧版为 `gen_ai.system`） | string | `openai` / `anthropic` / `aws.bedrock` / `azure.ai.openai` 等 | ✅ |
| `gen_ai.request.model` | string | 请求的模型名 | ✅ |
| `gen_ai.response.model` | string | 实际响应的模型名（**和请求可能不同**，provider 会路由） | ✅ |
| `gen_ai.response.id` | string | 厂商侧响应 id，用于对账 | ✅ |
| `gen_ai.request.temperature` / `top_p` / `max_tokens` | number | 采样参数 | ✅ |
| `gen_ai.usage.input_tokens` / `output_tokens` | int | 用量，**计费的唯一权威来源** | ✅ |
| `gen_ai.response.finish_reasons` | string[] | `stop` / `length` / `tool_calls` / `content_filter` | ✅ |
| `gen_ai.agent.name` / `gen_ai.agent.id` | string | 多 Agent 场景的归属 | ✅ |
| `gen_ai.tool.name` / `gen_ai.tool.call.id` | string | 工具调用 | ✅ |
| `gen_ai.conversation.id` | string | 会话 id | ✅ |
| Span 命名 | — | `chat {model}` / `execute_tool {tool}` / `invoke_agent {agent}` | ✅ |

**在此之上补 Aurora 私有属性**（`aurora.*` 前缀，避免和标准冲突）：

| 属性 | 为什么非采不可 |
| --- | --- |
| `aurora.prompt.version` / `rolecard.version` | 不采 = 线上效果无法复现（第 42 章） |
| `aurora.retrieval.strategy` / `rerank.applied` / `index_version` | 静默降级的唯一证据（§39.1 的根因） |
| `aurora.ttft.ms` | 首 token 延迟不能用 span duration 代替 |
| `aurora.cache.prefix_hit` / `semantic_hit` | 缓存命中率与成本的直接关系（第 24 章） |
| `aurora.degrade.level` | 降级阶梯走到了第几档（第 25 章） |
| `aurora.retraction.count` / `audit.verdict` | 撤回率与审核质量（第 37 章） |
| `aurora.tool.tier` / `truncated` | 副作用风险与结果截断（第 22 章） |
| `aurora.cost.usd`（含缓存折扣） | 计费与 FinOps 的桥（第 27、47 章） |

**绝对不能采的**：完整 prompt 明文、完整用户输入明文、工具返回的原始业务数据。要么哈希，要么脱敏，要么放引用 id 指向受控存储。这一条不是建议，是合规红线（见第 31 章）。

### 39.4.2 Trace Schema（落库用，ClickHouse）

Trace 后端适合查单条，但**切片分析（按版本/租户/场景聚合）要在明细表做**。这两张表是配套的：

```sql
-- 1) Run 级明细：一次 Run 一行，看板和归因的主表
CREATE TABLE run_fact (
    ts                DateTime64(3),
    tenant_id         LowCardinality(String),
    tenant_tier       LowCardinality(String),
    agent_type        LowCardinality(String),
    scenario          LowCardinality(String),

    run_id            String,
    trace_id          String,
    session_id        String,
    user_id_hash      String,                    -- 哈希，不存明文

    -- 版本四件套（缺一个就无法复现）
    model_ver         LowCardinality(String),
    prompt_ver        LowCardinality(String),
    rolecard_ver      LowCardinality(String),
    policy_ver        LowCardinality(String),

    -- 路由与降级
    route_decision    LowCardinality(String),
    degrade_level     UInt8 DEFAULT 0,
    fallback_count    UInt8 DEFAULT 0,
    cache_prefix_hit  Bool,
    cache_semantic_hit Bool,

    -- 用量与成本
    tokens_in         UInt32,
    tokens_out        UInt32,
    tokens_cached     UInt32,
    cost_usd          Decimal(12, 6),

    -- 延迟
    ttft_ms           UInt32,
    dur_ms            UInt32,
    step_count        UInt8,
    tool_calls        UInt8,

    -- 结果
    status            LowCardinality(String),    -- ok / aborted / error / timeout
    error_code        LowCardinality(String),
    audit_verdict     LowCardinality(String),
    retraction_count  UInt8 DEFAULT 0,
    postproc_dropped  UInt8 DEFAULT 0,

    -- 质量信号（线上代理指标，见第 40 章）
    feedback          Enum8('none'=0,'up'=1,'down'=2,'ticket'=3),
    judge_score       Nullable(Float32),

    -- 可追溯
    sampled           Bool,                      -- 是否被 trace 采样（明细表全量，此列只标 trace 有无）
    trace_url         String
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (tenant_id, ts, run_id)
TTL ts + INTERVAL 180 DAY;

-- 2) Tool 调用明细：按工具聚合成功率，故障定位的第二入口
CREATE TABLE tool_call_fact (
    ts             DateTime64(3),
    tenant_id      LowCardinality(String),
    run_id         String,
    step_index     UInt8,
    tool_name      LowCardinality(String),
    tool_tier      Enum8('read'=1,'write'=2,'dangerous'=3),
    provider       LowCardinality(String),      -- 内部 / MCP server 名 / 第三方
    idempotency_key String,
    attempt        UInt8,
    status         LowCardinality(String),
    error_code     LowCardinality(String),
    dur_ms         UInt32,
    result_bytes   UInt32,
    truncated      Bool
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (tenant_id, tool_name, ts)
TTL ts + INTERVAL 90 DAY;
```

### 39.4.3 采样策略：这是最容易做错的地方

**先说结论：Agent 平台的采样必须「尾部采样 + 分层保底 + 指标全量」，头部随机采样单独使用是有害的。**

为什么？因为**失败与慢请求不是随机分布的**：

```
一次 Run 的 span 数量与结果的关系（Aurora 实测）

成功 Run：  ~28 个 span, 12s,  $0.018
失败 Run：  ~61 个 span, 41s,  $0.071   ← span 多 2.2 倍，成本多 3.9 倍
撤回 Run：  ~54 个 span, 38s,  $0.066

如果按「trace 数」做 5% 头部随机采样：
  你采到的失败 Run 比例 = 真实的失败率（0.7%）
  → 每天 180 万 × 5% = 9 万条里只有 630 条失败样本
  → 而失败样本恰恰是你最需要的
  → 更要命的是：采样器在「开始」就决定了采不采，
    而 Run 开始时可不知道它会不会失败
```

**正确的采样器设计**：

```go
// 尾部采样（tail sampling）：等 Run 结束后再决定留不留。
// 部署在 OTel Collector 里，而不是在应用进程里。
type TailSampler struct {
    baseRate      float64          // 基础保留率
    strataReserve map[string]int   // 每层保底条数
}

func (s *TailSampler) Decide(r *RunSummary) Decision {
    // 1) 必留：任何"坏"信号
    if r.Status != "ok" ||
       r.ErrorCode != "" ||
       r.RetractionCount > 0 ||
       r.AuditVerdict != "pass" ||
       r.PostprocDropped > 0 ||
       r.DegradeLevel > 0 ||
       r.FallbackCount > 0 {
        return Keep("must_keep_bad")
    }

    // 2) 必留：慢请求（P99 之外）+ 超贵请求
    if r.DurMS > 30000 || r.CostUSD > 0.15 {
        return Keep("must_keep_tail")
    }

    // 3) 分层保底：每个 (租户档位 × agent_type × 模型版本) 至少留 N 条
    //    否则小众租户/新模型版本会完全没有样本
    if s.strataReserve[r.Stratum()] > 0 {
        s.strataReserve[r.Stratum()]--
        return Keep("stratum_reserve")
    }

    // 4) 其余按基础率
    if rand.Float64() < s.baseRate {
        return Keep("base")
    }
    return Drop()
}
```

| 策略 | 用在哪 | 优点 | 致命问题 |
| --- | --- | --- | --- |
| 头部随机 5% | ❌ 不建议单独用 | 简单、可在 SDK 层做 | 漏掉长尾失败（失败 Run 更长更贵） |
| **尾部采样** | ✅ trace | 能精准保失败/慢/贵 | 需要 Collector 缓冲整个 trace（内存成本） |
| **分层保底** | ✅ trace | 保证小众租户/新版本有样本 | 需要维护分层键 |
| **全量指标** | ✅ metrics | 无偏差 | 无法下钻到单 Run |
| **全量事件明细** | ✅ ClickHouse run_fact | 可任意切片 | 存储成本（180 万行/日，压缩后 ~2 GB/日，可接受） |
| **审计全量** | ✅ audit | 合规要求 | 见第 31 章 |

> 🩸 **绝对不要对成本指标做采样**。`sum(cost_usd)` 在 5% 采样下外推，误差会被长尾放大到 ±30% 以上；成本是钱，不是统计量。做法：metrics 里维护全量的 `tokens_total` 计数器，明细表的 `cost_usd` 只用于归因分析（`avg` 类），不用于总额。

### 39.4.4 看板清单：八块，一块都别少

| # | 看板 | 核心指标 | 谁看 | 刷新 |
| --- | --- | --- | --- | --- |
| 1 | **全局健康** | Run 成功率、P50/P95/P99 时长、TTFT P95、QPS、在途 Run 数 | 值班 | 1 min |
| 2 | **成本** | token/分钟、$ / 小时、$ / Run、Top20 租户成本、缓存命中率、prefix hit 率 | 平台 + 财务 | 5 min |
| 3 | **模型与路由** | 各模型成功率/延迟/成本、fallback 率、路由分布、`response.model` 与 `request.model` 不一致率 | 平台 | 5 min |
| 4 | **工具** | 调用成功率、P99、错误码 Top10、MCP server 健康、结果截断率、幂等冲突数 | 平台 | 1 min |
| 5 | **检索** | 空召回率、max_score 分布、`rerank.applied` 为 false 的比例、索引版本分布 | 算法 | 5 min |
| 6 | **管道** | 前处理拦截率、注入命中率、后处理 drop 率 / false-kill 率、**撤回率 / 已读撤回率** | 平台 | 5 min |
| 7 | **队列与容量** | 排队深度、准入拒绝率、并发水位、Pod 数、连接数（第 11、28 章） | SRE | 1 min |
| 8 | **质量代理** | 点踩率、工单率、judge 均分、拒答率、转人工率 | 产品 + 算法 | 1 h |

> 💡 第 5 块看板是最容易被忽略、也最容易救命的一块。`rerank.applied=false 比例` 这一个数字，就能提前 6 天发现 §39.1 那种静默降级。**每个「应该生效的策略」都要有一个「是否真的生效」的指标。**

### 39.4.5 告警：用 burn rate，别用单点阈值

单点阈值告警在 Agent 平台上是灾难：流量有早高峰晚高峰、模型有抖动、成本有自然波动。**用错误预算消耗速率（burn rate）**：

```
SLO：Run 成功率 ≥ 99.0%（月度错误预算 = 1% × 总 Run 数）

burn_rate = 当前错误率 / 错误预算率
  burn_rate = 1  → 刚好在预算内烧完
  burn_rate = 14.4 → 1 小时内烧掉月度预算的 2%（多窗口告警的经典参数）
```

```yaml
# aurora 告警规则（Prometheus 风格，节选）
groups:
  - name: aurora_agent_slo
    rules:
      # 快烧：1h 内错误率是预算率的 14.4 倍 → 立刻叫人
      - alert: RunSuccessBurnFast
        expr: |
          (sum(rate(aurora_run_total{status!="ok"}[1h])) / sum(rate(aurora_run_total[1h]))) > (14.4 * 0.01)
        for: 2m
        labels: { severity: page }
        annotations:
          summary: "Run 成功率快速燃烧，1h 内将烧掉月度预算 2%"

      # 慢烧：6h 窗口 6 倍 → 工作日处理
      - alert: RunSuccessBurnSlow
        expr: |
          (sum(rate(aurora_run_total{status!="ok"}[6h])) / sum(rate(aurora_run_total[6h]))) > (6 * 0.01)
        for: 30m
        labels: { severity: ticket }

  - name: aurora_cost
    rules:
      # 成本异常：与 7 天同小时基线比，偏离 2.5σ 且绝对值超阈值
      - alert: CostSpike
        expr: |
          sum(rate(aurora_cost_usd_total[10m])) * 3600
            > 1.35 * avg_over_time(sum(rate(aurora_cost_usd_total[10m]))[7d:1h] offset 0m) * 3600
          and
          sum(rate(aurora_cost_usd_total[10m])) * 3600 > 400
        for: 10m
        labels: { severity: page }
        annotations:
          summary: "小时成本 {{ $value }} USD，超基线 35%"

      # 单租户成本占比异常（薅羊毛/失控循环的第一信号）
      - alert: TenantCostOutlier
        expr: |
          topk(1, sum by (tenant_id) (rate(aurora_cost_usd_total[15m])))
            > 0.25 * sum(rate(aurora_cost_usd_total[15m]))
        for: 15m
        labels: { severity: page }

  - name: aurora_pipeline
    rules:
      - alert: RetractionRateHigh
        expr: |
          sum(rate(aurora_fragment_retracted_total[30m]))
            / sum(rate(aurora_fragment_committed_total[30m])) > 0.012
        for: 15m
        labels: { severity: page }
        annotations:
          summary: "撤回率 > 1.2%（目标 <0.4%），疑似检索/模型系统性问题"

      # 静默降级探测器：策略应该生效但没生效
      - alert: RerankNotApplied
        expr: |
          sum(rate(aurora_retrieval_total{rerank_applied="false"}[10m]))
            / sum(rate(aurora_retrieval_total[10m])) > 0.02
        for: 5m
        labels: { severity: page }
        annotations:
          summary: "rerank 未生效比例 {{ $value | humanizePercentage }}，疑似热加载失败"

      - alert: DegradeLevelRaised
        expr: sum(rate(aurora_run_total{degrade_level!="0"}[10m]))
                / sum(rate(aurora_run_total[10m])) > 0.05
        for: 10m
        labels: { severity: ticket }
```

> ⚠️ 三个告警设计要点：① **必须有 `TenantCostOutlier`**，否则一个失控循环能在 40 分钟里烧掉一个月预算（第 27 章）；② **必须有「策略未生效」类告警**，这类故障不报错、不掉成功率，只有它抓得住；③ 所有告警都要带 `runbook_url`，值班的人不需要推理，只需要照着做。

### 39.4.6 从 trace 到答案：三个必会查询

```sql
-- Q1 定位：某租户某时段，哪一步最慢最贵（按 step 聚合）
SELECT
    step_kind,
    count()                          AS n,
    quantile(0.95)(dur_ms)           AS p95_ms,
    sum(cost_usd) / count()          AS avg_cost,
    round(avg_cost / avg(p95_ms) * 1000, 6) AS usd_per_sec   -- 贵且慢的步骤优先优化
FROM step_fact
WHERE tenant_id = 't_8842' AND ts BETWEEN '2026-05-11 22:00' AND '2026-05-11 24:00'
GROUP BY step_kind
ORDER BY avg_cost DESC;

-- Q2 归因：某提示词版本上线后，质量与成本怎么变的（同环比）
SELECT
    prompt_ver,
    min(ts)                          AS first_seen,
    count()                          AS runs,
    round(countIf(feedback='down') / count(), 4)   AS down_rate,
    round(countIf(retraction_count > 0) / count(), 4) AS retract_rate,
    round(avg(cost_usd), 6)          AS avg_cost,
    quantile(0.95)(ttft_ms)          AS p95_ttft
FROM run_fact
WHERE agent_type = 'customer_service' AND ts >= now() - INTERVAL 7 DAY
GROUP BY prompt_ver
ORDER BY first_seen DESC;

-- Q3 静默降级：哪些流量在"成功"但走了降级路径
SELECT
    agent_type,
    degrade_level,
    error_code,
    count()                          AS runs,
    round(countIf(feedback IN ('down','ticket')) / count(), 4) AS bad_feedback_rate
FROM run_fact
WHERE ts >= now() - INTERVAL 1 DAY AND degrade_level > 0
GROUP BY agent_type, degrade_level, error_code
ORDER BY runs DESC;
```

Q3 是我们最常用的一条：**`degrade_level > 0` 且 `bad_feedback_rate` 明显高于均值，就说明这次降级是"降过头了"**，应该改成明确失败而不是假装成功。

---

## 39.5 Java / 分布式经验对照

| 你熟悉的东西 | Agent 可观测的对应物 | 会咬人的差异 |
| --- | --- | --- |
| SkyWalking / Zipkin span | OTel span | span 语义多了「模型、提示词版本、token、成本、TTFT」；`duration` 不再是唯一性能指标 |
| Micrometer / Prometheus | 同 | 但**成本必须做成计数器全量上报**，不能采样 |
| ELK 日志检索 | ClickHouse 明细表 | 日志是字符串；明细表要强 schema，否则切片查询做不了 |
| 链路采样率 1% | 尾部采样 + 分层 | 传统 APM 采样 1% 够用（量大且同质）；Agent 的失败样本不多但极贵，必须保 |
| 链路追踪的 trace_id | `run_id` 即 `trace_id` | 一次 Run 可能几十分钟（第 12 章），trace 要支持跨进程续接 |
| JVM GC 日志 / 线程 dump | prompt/context 快照 | 更敏感：含 PII，必须哈希或脱敏后存储 |
| 埋点 SDK | Processor / Collector | Collector 侧做脱敏比 SDK 侧可靠（防止某个服务忘了加） |
| 报错率告警 | 成功率 burn rate + **策略未生效告警** | 后者是新增类别，传统 APM 里没有 |

最大的差异：**传统 APM 回答「哪里慢」，Agent 可观测要回答「为什么这么说」**。前者是性能问题，后者是正确性问题。所以必采字段里，**版本号和「策略是否生效」的地位高于耗时**。

---

## 39.6 开源坐标

| 项目 | 有什么 | 缺什么 |
| --- | --- | --- |
| **OpenTelemetry GenAI 语义约定** | `gen_ai.*` 属性标准、span 命名规范 | 标准还在演进，字段名偶有变更（如 `gen_ai.system` → `gen_ai.provider.name`），**以官方文档最新版为准** |
| **Langfuse** | trace/generation/span 三层模型、prompt 版本管理、score 打分、数据集；自托管友好 | 存储与查询偏「单条查看」，大批量切片分析要靠导出/外部数仓 |
| **LangSmith** | 与 LangChain 生态深度集成、run 树、feedback、数据集与实验对比 | 强绑定其生态；自托管能力受限 |
| **Phoenix（Arize）** | 基于 OTel，原生吃 `gen_ai.*`；有 embedding 漂移与 RAG 分析视图 | 生产级长期存储与多租户治理要自己做 |
| **OTel Collector** | `tail_sampling`、`attributes`/`transform` processor、批量导出 | 需要自己写分层保底逻辑（标准 tail_sampler 只给策略组合） |
| **Grafana / Prometheus / Tempo / ClickHouse** | 指标、trace、明细三件套 | 一切模型语义要自己定义 |

> 🎯 结论：**这一层的开源成熟度是全书最高的**——Langfuse/Phoenix 已经把「看单条 trace」做得很好，OTel 给了标准。你要自研的是三样：① 版本四件套的强制注入；② 尾部采样 + 分层保底；③ 成本与撤回的切片分析表。这三样大概 2000 行代码，但它决定了你能不能做 FinOps 和质量归因。

---

## 39.7 事故清单

- [ ] **事故 A：采样把失败样本采没了**。现象：trace 里几乎看不到失败 Run，排障只能靠猜。
  → 根因：头部随机采样 5%，且失败 Run 也是 5% 概率被采。
  → 兜底：改尾部采样，`status!=ok / retraction>0 / degrade>0` 必留。
- [ ] **事故 B：成本指标被采样污染**。现象：月度账单比看板高 28%。
  → 根因：看板用采样后的 `cost_usd` 外推。
  → 兜底：成本走全量 counters，明细表只做 `avg` 不做 `sum`。
- [ ] **事故 C：静默降级活了 6 天**。现象：rerank 挂了，检索质量下降，但一切指标正常。
  → 根因：没有「策略是否生效」的属性与告警。
  → 兜底：每个应生效的策略配一个 `*_applied` 指标 + 告警（§39.4.5）。
- [ ] **事故 D：trace 里存了 prompt 明文，泄露 PII**。现象：可观测后端里能搜到用户身份证号。
  → 根因：SDK 层直接 `span.SetAttributes("prompt", fullPrompt)`。
  → 兜底：Collector 侧强制 redact processor + 属性白名单；prompt 只存哈希与受控引用 id。
- [ ] **事故 E：trace 存储打爆**。现象：Tempo 磁盘 3 天写满，trace 全丢。
  → 根因：全量 trace + 平均 45 个 span/Run + 每条 1.5KB = 120 GB/日。
  → 兜底：尾部采样（有效保留率 ~8%）+ span attribute 瘦身 + 7 天 TTL。
- [ ] **事故 F：告警风暴**。现象：一次模型厂商抖动触发 200 条告警，值班直接关静音，然后漏了真故障。
  → 根因：单点阈值 + 每租户一条告警 + 没有分组。
  → 兜底：burn rate 多窗口 + `group_by: [service, alertname]` + 抑制规则（上游故障时抑制下游）。

---

## 39.8 一页速记

| 关键词 | 结论 |
| --- | --- |
| 核心问题 | Agent 可观测不只回答「哪里慢」，要回答「为什么这么说」——版本号比耗时更重要 |
| Trace 模型 | Run → Step → {preprocess, retrieval, llm.call, tool.call} + 后置 {audit, billing} |
| 标准 | 对齐 OTel `gen_ai.*`（`operation.name` / `request.model` / `response.model` / `usage.*` / `finish_reasons`），私有属性用自己前缀 |
| 版本四件套 | model / prompt / rolecard / policy 版本**必须**进 trace，否则无法复现（第 42 章） |
| 隐藏属性 | `retrieval.rerank.applied`、`degrade_level`、`cache.prefix_hit`——静默降级的唯一证据 |
| 采样 | 尾部采样 + 分层保底；失败/慢/贵/撤回必留；**成本指标绝不采样** |
| 存储分离 | trace 7 天采样 / 明细 180 天全量 / 审计 3 年不可变——三套物理隔离 |
| 日志边界 | 日志只放异常与堆栈；结构化字段进 trace/明细表 |
| 看板 | 八块：健康/成本/模型路由/工具/检索/管道/容量/质量代理 |
| 告警 | burn rate 多窗口；必须有「成本异常」「单租户成本离群」「策略未生效」三类 |
| 反模式 | 全量 trace（存储爆）、头部随机采样（漏失败）、日志当 trace（查不动） |
| 一句话 | **你不能解释的 Agent，就是不能发布的 Agent。** |

---

## 39.9 自检题

1. **你的 trace 采样率是头部随机 5%，运行半年后发现「所有线上问题都复现不了」。为什么？怎么改？**
   > 要点：头部采样在 Run 开始时决定采不采，而失败 Run 的特征（span 多、时间长、成本高）在开始时不可知；同时失败 Run 数量本来就少（0.7%），5% 采样后每天只剩几百条，且是随机分布的、往往缺少关键的那几条。改成尾部采样：Run 结束后按 `status / retraction / degrade_level / audit_verdict` 必留，再按分层做保底，其余走基础率。

2. **为什么成本指标不能用采样数据外推，而延迟 P99 可以用采样数据估算？**
   > 要点：成本是**加性**的，且长尾极重（失败 Run 成本是成功的 3.9 倍），5% 采样外推会把长尾截断，误差可达 ±30%；钱不能有误差。而 P99 是**分位数**，采样会保留分布形状（只要采样与延迟无关），估算偏差小；但如果采样策略本身与延迟相关（比如「只采慢的」），P99 也会失真——所以延迟 P99 应该从全量 metrics 的 histogram 算，而不是 trace。

3. **你的成功率 99.3%、P99 正常、错误率正常，但客诉涨了 4 倍。列出你会按顺序查的三个地方。**
   > 要点：① `degrade_level > 0` 的流量占比与它们的 `bad_feedback_rate`（带着降级跑的成功最危险）；② 版本维度切片——`prompt_ver / rolecard_ver / model_ver` 最近有没有变，哪个版本的 `down_rate` 高；③ 「策略生效」类指标——`retrieval.rerank.applied`、`cache.prefix_hit`、`audit.verdict` 分布有没有漂移。这三步覆盖了「静默降级 / 版本回归 / 管道异常」三大类，占实际故障的 80%。

---

## 39.10 延伸阅读

- 上一章：[38. 后处理层](38-post-processing.md)（后处理的 drop/false-kill 率是本章要采的指标）
- 下一章：[40. 评估体系](40-evaluation-system.md)（trace 的线上信号怎么变成评测与门禁）
- 相关：[31. 合规、审计与取证](31-compliance-audit.md)（审计与 trace 的物理分离）、[27. 计费可靠性](27-billing-reliability.md)（token 是计费唯一权威来源）、[37. 输出审计与撤回](37-output-audit-retraction.md)（撤回指标的来源）、[45. 存储架构](45-storage-architecture.md)（明细表分区与 TTL）
- 外部：OpenTelemetry GenAI Semantic Conventions（以官方文档最新版为准）、OTel Collector `tail_sampling` processor、Langfuse / Phoenix / LangSmith 文档、Google SRE Workbook 第 5 章（多窗口 burn rate 告警）
- 总纲：[0-summary.md](0-summary.md)
