# 42. 模型与提示词的迭代上线

> **状态**：`已完成（初稿）` ｜ **所属**：第八部分 · 可观测、评估与数据闭环 ｜ **编号**：42 / 52
> **本章定位**：这是「数据闭环」五章的收口。第 40 章教你**怎么量**，第 41 章教你**怎么把线上数据变回样本**，本章教你**怎么把一个候选版本安全地推给 10 万个租户，并且在它变差时 30 秒内退回来**。
> **读法**：先看 §42.2 的账本，再读 §42.3 的发布控制面图。以后每次有人说「换个模型试试」，把 §42.4 的门禁表和 §42.7 的事故清单拿出来过一遍再动手。

---

## 42.1 引子：一次「只是换个模型」的变更，两天烧掉 42 万美元

2026 年 3 月，第二个周四。模型供应商发了 B 系列的新快照，价格降 40%，上下文翻倍。平台组跑了一遍离线评测：核心集 200 条，B 新快照通过率 91%，线上旧模型 89%。**赢了 2 个点**。周五上午全量切换，改了一行路由配置。

周一客服群里开始冒泡：「它开始瞎调工具了，下单接口一直报 400。」

查下来是这样的：B 新快照对 JSON Schema 的遵循变「自由」了——工具参数的字段名从你声明的 `order_id` 变成了 `orderId`。你的参数解析器开了宽容模式，缺字段就填零值，于是 `order_id=""` 一路打到订单服务，400 一片。任务成功率从 **96% 掉到 71%**。

算账：

| 项 | 数值 | 算式 |
| --- | --- | --- |
| 受影响时长 | 2 天 | 周五切换 → 周一发现 |
| 受影响的 Run | 2,700 万 | 3,000 万/天 × 2 天 × 45% 走 B 的流量 |
| 失败的 Run | 675 万 | 2,700 万 × 25pp 退化 |
| 重试放大的模型调用 | 1,350 万次 | 675 万 × 平均重试 2 次 |
| 白烧的钱 | ≈ 42 万美元 | 1,350 万 × $0.031/次 |
| 其他 | 3,100 单客诉、2 家大客户启动 SLA 违约条款、1 次董事会通报 | — |

**真正扎心的问题**：那 200 条评测集为什么没拦住？

因为它只拦得住「整体崩」。这次退化集中在「工具调用参数格式」一个切片上，200 条里只有 11 条带工具调用，其中 3 条退化——落到总体就是 **1.5 个百分点**，而 200 样本在 90% 基线上的 95% 置信区间是 **±4.2pp**。也就是说：**这 3 个退化样本在你的统计噪声底下，你根本看不见。**

> 🩸 **这一章的核心命题**：
> Agent 平台的每一次上线，上的不是代码，是**行为**。代码可以 diff，行为不能。所以你必须用「版本矩阵 + 切片门禁 + 灰度双跑」三件套，把「行为变更」变成一个**可观测、可分段、可回退**的工程动作。
> 少这三件套，你就只能靠「用户先炸、你再修」来做发布。

---

## 42.2 断崖：五维版本矩阵，和它的 576 万种组合

一次 Run 的实际行为由五个维度共同决定，而它们**各自独立地**在变：

| # | 维度 | 谁在改 | 变更频率 | 变了以后症状长什么样 |
| --- | --- | --- | --- | --- |
| 1 | **模型**（provider + 模型 + 快照 + 采样参数） | 供应商 + 你自己 | 供应商静默升级不可控；你主动切换 ~3 次/月 | 格式遵循、拒答倾向、语言混用、长度突变 |
| 2 | **系统提示词**（模板 + few-shot + 输出契约） | 产品/算法 | 30 次/周（Aurora 实测） | 语气漂移、边界松动、工具该调不调 |
| 3 | **角色卡**（身份/能力边界/禁止项） | 业务 + 合规 | 5 次/周 | 角色漂移、越权承诺、合规话术失效 |
| 4 | **工具集**（schema + 实现 + MCP server 版本） | 平台 + 三方 | 12 次/周 | 参数错、超时、幂等失效 |
| 5 | **路由策略**（场景→模型的映射与权重） | 平台 | 20 次/周 | 成本突变、长尾变长、某个场景整体降智 |

组合数不是加法，是乘法：

```
活跃模型 4 个
× 提示词模板 120 个（含多语言变体）
× 角色卡 40 张
× 工具集 25 套
× 路由策略 12 个版本
= 576 万种「可能的线上状态」
```

有人会说「实际不会全组合」。对，实际线上同时只有 4×120 里的一部分在跑。但**你要能回答「昨天 14:03 那次 Run 用了哪一组」**——这就是 576 万种可能里唯一确定的那一个。回答不了，第 39 章的 trace 就是废纸，第 40 章的评测就无法归因。

### 不做会怎样：六笔账

| 不做的事 | 直接后果 | 量化 |
| --- | --- | --- |
| 版本号不进 Run 元数据 | 线上 badcase 无法复现 | 复盘时间从 10 分钟涨到 2 天；40% 的投诉查不出原因 |
| 没有注册表（模型靠字符串硬编码） | 供应商静默升级 = 你的行为静默改变 | 一次未察觉的升级 = 上述 42 万美元 |
| 没有发布门禁 | 每次上线都是赌博 | 发布回滚率 30%+，团队开始「不敢发版」 |
| 只有全量发布 | 翻车半径 = 100% 流量 | 5% 灰度能把损失压到 1/20 |
| 回滚靠「再发一次版」 | 故障时长 = 构建 + 部署 = 25~40 分钟 | 一键回滚 = 5~30 秒，差 50 倍 |
| 退化靠人肉发现 | 发现延迟 = 1~3 天 | 自动护栏 = 15 分钟窗口自动回滚 |

### 三种上线策略的钱：为什么灰度是「省钱」不是「慢」

| 策略 | 变更→全量 | 单次变更的验证成本 | 一次翻车的期望损失 |
| --- | --- | --- | --- |
| 直接全量 | 0 | 评测 2,000 条 ≈ $62 + 3 次重复 ≈ $186 | 20% 概率 × 42 万 = **$84,000** |
| 灰度 5%→25%→100%（3 天） | 3 天 | 观测 450 万 Run（不双跑，无额外推理成本） | 5% 流量 1 天 × 25pp = 9.4 万次失败 → **$2,900** |
| 双跑 Shadow 7 天（1% 采样） | 7 天 | 105 万影子 Run × $0.019 ≈ **$20,000** | 几乎为 0（未触达用户） |

> 💡 结论很反直觉：**灰度不是「为了稳而牺牲速度」，它是三种策略里期望成本最低的**。双跑更贵，但只在「换模型」这种高风险、低频变更上用；日常提示词微调走灰度就够。

---

## 42.3 结构图：发布控制面（Release Plane）

把「发布」从人的动作变成一台机器。这台机器只做四件事：**组装候选 → 判定门禁 → 分段放量 → 观测与回退**。

```
        ┌──────────────────── ① 候选组装 (Candidate) ────────────────────┐
Git: prompts/ persona/ tools/ route/   ─┐
Model Registry (model cards)           ─┼─▶  candidate = {model, prompt_set,
Eval Sets (2000 条 / 6 个切片)          ─┘      persona, toolset, route, index}
                                                        │
                                                        ▼
        ┌──────────────────── ② 门禁 (Gate) ─────────────────────────────┐
        │  质量门槛：总体 ≥ baseline-1pp 且 6 个切片每个 ≥ baseline-3pp     │
        │  成本门槛：$ / Run ≤ baseline × 1.05                            │
        │  延迟门槛：TTFT P95 ≤ 3.4s，Run P95 ≤ 46s                       │
        │  安全门槛：注入攻击集拦截率不降、PII 泄露 = 0                    │
        └───────────────────────────┬────────────────────────────────────┘
                                    │ 通过 → 签发 release_id（不可变）
                                    ▼
        ┌──────────────── ③ 灰度控制器 (Rollout Controller) ──────────────┐
        │  分桶：租户白名单 / 流量百分比(一致性哈希) / 场景 / 租户等级      │
        │  互斥：高风险场景与铂金租户默认排除                              │
        │  双跑：Shadow 采样 1%，只读工具替身                              │
        │  护栏：15 分钟窗口的自动判定 → 扩量 / 冻结 / 回滚                │
        └───────────────────────────┬────────────────────────────────────┘
                                    │ 写路由表（第 43 章：策略中心）
                                    ▼
        ┌────────── 路由表 route_table(scope → release_id) ──────────────┐
        └───────────────────────────┬────────────────────────────────────┘
                 ┌──────────────────┼───────────────────┐
                 ▼                  ▼                   ▼
           API / Worker       模型网关            评测与看板
         （只读本地快照）   （按 release 打标、    （按 release_id /
                          限流、计量前置）        切片出成功率曲线）
                 │                  │                   ▲
                 └──── Run 元数据写 5 个版本号 ──────────┘
                        （回写 trace，第 39 章）
```

这张图有三个必须记住的点：

1. **release_id 是唯一真相**。不是「模型名」，也不是「提示词版本」，而是这一组五个维度冻结后的哈希。所有指标、所有 trace、所有账单都要按它切片。
2. **发布控制面只写「路由表」这一个东西**，它不下发到具体实例。实例永远只读本地快照——这是第 43 章的核心机制，也是发布能在 5 秒内生效的原因。
3. **回退是控制面的动作，不是部署动作**。改一行路由表 = 回滚，不需要构建、不需要滚动更新。

---

## 42.4 关键设计

### （1）版本矩阵：Release 是唯一的一等公民

```go
// Release 是一组「冻结的行为」——发布后不可修改，只能新建。
type Release struct {
	ReleaseID  string `json:"release_id"`  // rel_2026_03_12_a1，全局唯一，进 trace
	Model      ModelRef `json:"model"`
	PromptSet  string `json:"prompt_set"`  // prompts@v37 + 内容哈希 sha256:9f2c…
	PersonaCard string `json:"persona_card"`
	ToolSet    string `json:"toolset"`     // tools@v5，包含每个 MCP server 的版本
	RoutePolicy string `json:"route_policy"`
	IndexSnap  string `json:"index_snapshot"` // idx_2026_05_01（RAG 也要冻结，第 20 章）
	CreatedBy  string `json:"created_by"`
	GateReport string `json:"gate_report"` // 指向门禁报告的对象存储 key
}

type ModelRef struct {
	Provider  string  `json:"provider"`   // 不要只写 "openai"，要写 endpoint 域名
	Model     string  `json:"model"`
	Snapshot  string  `json:"snapshot"`   // 有快照就锁快照，没有就锁「首次观测到的版本串」
	Temp      float64 `json:"temperature"`
	TopP      float64 `json:"top_p"`
	MaxTokens int     `json:"max_tokens"`
	ParamsHash string `json:"params_hash"` // 采样参数的哈希，防止「有人偷偷调了温度」
}

// Fingerprint 是「同一行为」的判据：五个维度全等才算同一版本
func (r Release) Fingerprint() string {
	h := sha256.New()
	fmt.Fprintf(h, "%s|%s|%s|%s|%s|%s", r.Model.ParamsHash, r.PromptSet,
		r.PersonaCard, r.ToolSet, r.RoutePolicy, r.IndexSnap)
	return "rel_" + hex.EncodeToString(h.Sum(nil))[:16]
}
```

> ⚠️ 一个必须写死的规则：**Run 开始时把 release_id 钉在 Run 上，Run 全程不换版本**。灰度扩量时，一个已经用 A 版本跑到第 5 步的 Run 不能突然切到 B——上下文里已经有 A 的工具返回，混版本会产生你没见过、也测不出的行为。这叫「Run 内版本粘性」，是灰度正确性的前提。

### （2）模型注册与能力画像：ModelCard

模型注册表的第一个版本，所有人都会写成「名字 + endpoint + key」。它三天后就废了，因为你要回答的问题是：

- 这个模型能不能吃 200k 上下文？**实际**能吃多少（不是标称）？
- 它的工具调用准不准？JSON 输出的 schema 遵循率是多少？
- 它的首 token 延迟分布是什么样（不是平均值）？
- 它支不支持前缀缓存？缓存折扣是多少？缓存 TTL 多久？
- 它在哪个区域可用？数据会不会出境（第 31 章）？
- 它的弃用日期是什么时候？有没有已知缺陷（比如长上下文中间遗忘）？

| 画像字段 | 类型 | 为什么要它 | 影响哪一章 |
| --- | --- | --- | --- |
| `context_window` / `effective_ctx` | int | 标称 200k 实际有效可能只 60k，超了质量断崖 | 15 |
| `price_in / cache_in / price_out` | $/M | 路由与成本核算的唯一依据 | 26、47 |
| `ttft_p50/p95`、`tps_p50` | ms、tok/s | 延迟预算分配与并发配额 | 10、14 |
| `tool_call_reliability` | 0~1（分工具数档） | < 0.95 的模型不能进「多工具链路」 | 22 |
| `json_schema_fidelity` | 0~1 | 结构化输出能否直接用，决定要不要后处理修复 | 38 |
| `prefix_cache` | bool + TTL | 缓存键设计的前置条件 | 18、24 |
| `regions` / `data_residency` | 枚举 | 合规硬约束，不是性能选项 | 31、44 |
| `known_defects` | 文本列表 | 把踩过的坑固化，别让第二个人再踩 | 本章 |
| `deprecated_at` | date | 到期前 60 天自动告警（否则你会被突袭） | 本章 |

> 🩸 有 `effective_ctx` 这一栏，是因为我们吃过亏：某模型标称 128k，超过 60k 后对中间段的召回率从 92% 掉到 61%，而评测集全是短样本，完全测不出来。**能力画像不是文档，是实测基线。**

### （3）门禁：总体通过率是骗人的，必须切片

```go
type Gate struct {
	Baseline  ReleaseEval            // 当前全量版本的评测结果
	MinSamples int                   // 每个切片的最小样本量，不足则判 INSUFFICIENT
	Slices    []string               // tool_call / multi_turn / long_ctx / zh / refuse / rag
}

func (g *Gate) Evaluate(ctx context.Context, cand ReleaseEval) (Decision, error) {
	// ① 总体：不允许退化，允许微升
	if cand.Overall < g.Baseline.Overall-0.01 {
		return Rejected, fmt.Errorf("overall regressed: %.3f -> %.3f",
			g.Baseline.Overall, cand.Overall)
	}
	// ② 切片：任何一个切片退化 > 3pp 就拦——这是引子事故的解药
	for _, s := range g.Slices {
		b, c := g.Baseline.Slice(s), cand.Slice(s)
		if c.N < g.MinSamples {
			return Insufficient, fmt.Errorf("slice %s: n=%d < %d", s, c.N, g.MinSamples)
		}
		if c.Pass < b.Pass-0.03 {
			return Rejected, fmt.Errorf("slice %s regressed: %.3f -> %.3f (n=%d)",
				s, b.Pass, c.Pass, c.N)
		}
	}
	// ③ 成本：单位成本不允许超 5%
	if cand.CostPerRun > g.Baseline.CostPerRun*1.05 {
		return Rejected, fmt.Errorf("cost: $%.4f > $%.4f", cand.CostPerRun, g.Baseline.CostPerRun*1.05)
	}
	// ④ 延迟：TTFT P95 与 Run P95 双门槛（只看均值会被长尾骗）
	if cand.TTFTP95 > 3400*time.Millisecond || cand.RunP95 > 46*time.Second {
		return Rejected, errors.New("latency budget exceeded")
	}
	return Approved, nil
}
```

**样本量与检测能力**（两比例检验，α=0.05 双侧，power=0.8，基线 90%）：

| 每切片样本量 | 最小可检测差异 (MDD) | 能挡住什么 |
| --- | --- | --- |
| 200 | 8.4pp | 只有灾难级（工具全废、模型换了语种） |
| 800 | 4.2pp | 明显退化（换了模型档位） |
| 2,000 | 2.7pp | 常规退化（提示词改动翻车） |
| 5,000 | 1.7pp | 精细退化（few-shot 顺序、措辞微调） |

算式：`MDD = sqrt(2 × (1.96+0.84)² × p(1-p) / n)`，p=0.9 时 ≈ `sqrt(1.411/n)`。

> 💡 所以 Aurora 的做法是**两层**：核心门禁集 2,000 条（每个切片 ≥ 250 条）挡 2.7pp；线上双跑与灰度护栏挡 1pp 量级。**指望 200 条集挡住 1.5pp，是统计学不允许的。**

### （4）灰度配置：按租户 / 按流量 / 按场景，三把刀

```yaml
# rollout/rel_2026_03_12_a1.yaml
release: rel_2026_03_12_a1
strategy: canary            # canary | shadow | blue_green
owner: platform-release
window: "09:30-16:30 Asia/Shanghai"   # 只在有人值班的时段放量

# ── 分桶：三层叠加，命中任一即进入灰度 ──────────────────────────
buckets:
  - name: internal_and_friendly        # 第一层：确定性白名单，先自伤
    by: tenant_whitelist
    values: [t_000001, t_000002, t_004411, t_009812]
  - name: traffic_slice                # 第二层：一致性哈希，同一租户不跳变
    by: traffic_percent
    percent: 5
    hash: { key: tenant_id, salt: rel_2026_03_12_a1 }
    # 扩量时只改 percent：5 → 25 → 50，同一 salt 保证「先灰的还在灰里」
  - name: low_risk_scenes              # 第三层：场景定向
    by: scene
    values: [kb_qa, faq, doc_summary]

# ── 排除：这些地方默认不进灰度，要单独签字 ────────────────────
exclude:
  scenes: [payment_action, contract_review, legal_advice]
  tenant_tier: [platinum]
  tags: [regulated, on_prem]

# ── 护栏：不达标自动回滚，不需要人半夜起床 ────────────────────
guards:
  - metric: task_success_rate
    window: 15m
    min_samples: 300          # 样本不足不动手，防误杀
    rule: ">= baseline - 0.02"
    action: rollback
  - metric: tool_call_error_rate
    window: 10m
    min_samples: 200
    rule: "<= baseline * 1.5"
    action: freeze            # 先冻结扩量，留 30 分钟观察
  - metric: cost_per_run
    window: 60m
    rule: "<= baseline * 1.10"
    action: alert
  - metric: ttft_p95
    window: 15m
    rule: "<= 3400ms"
    action: rollback

promotion:                    # 从 5% 到 100% 的条件
  stages: [5, 25, 50, 100]
  hold: 6h                    # 每档至少观察 6 小时（覆盖一个流量峰）
  require: [guard_pass_all, oncall_ack]
```

三个设计细节，每个都对应一次事故：

| 设计 | 不用会怎样 |
| --- | --- |
| **一致性哈希 + 固定 salt** | 用 `rand()` 分桶 → 同一租户每次请求在 A/B 之间跳，上下文和人格都漂移，用户看到「精神分裂」 |
| **窗口限定时段** | 凌晨 3 点自动扩量到 100%，早高峰直接炸，没人知道是新版本 |
| **`min_samples` 与 `freeze` 分级** | 只有 rollback 一个动作 → 小样本噪声触发误回滚，团队开始不信护栏，最后把它关掉 |

### （5）双跑（Shadow）：只读替身，别让影子把世界改了

双跑的价值是「用真流量测新版本，但不让用户看到」。它的陷阱只有一个，但很致命：**影子 Run 会调用写工具**。

```go
// 影子执行：三类硬隔离 —— 只读工具替身、独立预算、丢弃副作用
func (s *ShadowRunner) MaybeShadow(ctx context.Context, req RunRequest, cur Release) {
	if !s.cfg.Enabled || !s.sampler.Hit(req.TenantID, s.cfg.SampleRate) {
		return
	}
	// ① 影子不许碰任何写操作：工具集替换为 dry-run 版本
	shadowTools := s.tools.DryRunOnly()          // send_email → log_only, refund → validate_only
	// ② 影子有独立预算，超了就丢，不许影响主链路
	budget := s.budget.NewChild(ctx, s.cfg.ShadowBudgetMicro, 8*time.Second)
	// ③ 影子用独立的 run_id 前缀，事件不写主事件流，只写 shadow 表
	go func() {
		defer s.recoverPanic("shadow")
		out, err := s.runner.Run(budget, req.WithRunID("sh_"+req.RunID),
			s.candidate, shadowTools)
		s.compare.Report(req, out, err)   // 一致性 / 质量分 / 成本 / 延迟
	}()
}
```

双跑看四个指标，缺一个就会得出错误结论：

| 指标 | 定义 | 危险信号 |
| --- | --- | --- |
| **一致性** | 关键实体（订单号、金额、日期、拒答与否）与主版本一致的比例 | < 85% 说明行为变了，哪怕质量分更高也要人工看 |
| **质量分** | LLM-as-judge 或规则判定（第 40 章） | 高 2pp 以内不算赢，可能是 judge 偏差 |
| **成本** | 单 Run token 与钱 | 涨 30% 而质量只涨 1pp = 不划算 |
| **延迟** | TTFT / Run 总时长 | 影子不影响用户体验，但影响容量规划 |

> ⚠️ 双跑的钱要算清：1% 采样、7 天，105 万次影子 Run × $0.019 ≈ **2 万美元/次**。所以它只用在「换模型」「换上下文策略」这类高风险变更上，提示词微调走灰度。

### （6）一键回滚：三层，五个数量级

很多人以为回滚就是「切回去」。实际上回滚有四个层次，速度差 100 倍，而大多数团队只有最慢的那一层。

| 层 | 动作 | 生效时间 | 覆盖什么 | 谁来执行 |
| --- | --- | --- | --- | --- |
| L1 | 改 `route_table` 的 `release_id` | **5 秒**（配置推送 P99） | 新 Run 全部回旧版本 | 值班，一键 |
| L2 | 关 kill switch（走独立通道） | **1 秒** | 关闭某个功能/场景/工具 | 值班，一键 |
| L3 | 回滚配置快照（第 43 章） | 5 秒 | 限流/降级/路由参数 | 值班 |
| L4 | 回滚镜像（重新部署） | 8~25 分钟 | 代码缺陷 | 发布负责人 |

```sql
-- L1：一条 SQL 就是一次回滚。前提是路由表有 scope 优先级与单调版本号。
BEGIN;
INSERT INTO route_table (scope, scope_value, release_id, version, updated_by, reason)
VALUES ('global', '*', 'rel_2026_02_28_g7',
        (SELECT max(version)+1 FROM route_table), 'oncall:zhang', 'tool_call regression')
ON CONFLICT (scope, scope_value) DO UPDATE
  SET release_id = EXCLUDED.release_id,
      version    = EXCLUDED.version,
      updated_by = EXCLUDED.updated_by;
COMMIT;
```

**回滚之后还有两件事，90% 的团队会忘：**

1. **数据修复**。回滚只让新 Run 不再出错；已经产生的 675 万个坏 Run 还在库里，可能已经写坏了订单状态。要有一张 `remediation_queue`，把失败 Run 按 `release_id` 捞出来重放或补偿（第 12 章的 Saga）。
2. **冻结期**。回滚后 2 小时内，禁止对同一 scope 再发新版本。否则会出现「回滚 → 有人手贱重发 → 又炸」的二次事故，而且第二次没人敢动了。

### （7）能力退化：模型升级反而变差，怎么处置

这是本章最容易被低估的一段。**供应商的「升级」从来不是单调变好**：新快照可能更聪明但更啰嗦、更听话但更爱拒答、更便宜但工具调用更差。你要有一套「不接受升级」的权利。

```
        ┌──────────── 退化发现 ────────────┐
        │ ① 影子跑（换模型前）              │
        │ ② 灰度护栏（换模型后 15 分钟）    │
        │ ③ 供应商变更公告订阅 + 版本串漂移检测 │
        └───────────────┬──────────────────┘
                        ▼
              ┌──── 归因：是模型还是我？ ────┐
              │ 用 release_id 切片对比       │
              │ 同一 release 内也变差 → 模型 │
              │ 只是某切片变差 → 提示词/工具  │
              └───────────────┬─────────────┘
                              ▼
        ┌──────── 四条处置路径（按代价排序）────────┐
        │ A. 回滚到旧快照（如果供应商还留着）        │
        │ B. 锁版本 + 加适配层（schema 归一化）      │
        │ C. 提示词补偿（针对性 few-shot / 强约束）   │
        │ D. 拆场景：把退化切片路由到别的模型         │
        └───────────────────────────────────────────┘
```

| 退化形态 | 典型信号 | 处置 | 不要做什么 |
| --- | --- | --- | --- |
| 格式遵循变差 | 工具参数解析失败率↑ | B（归一化适配层）+ C | 别急着改提示词，先修解析器 |
| 变啰嗦 | 输出 token +40%，成本涨 | 提示词加长度约束 + 后处理截断 | 别用 `max_tokens` 硬截（会截在句子中间） |
| 过度拒答 | 拒答率从 3% → 11% | C（角色卡边界放宽 + 明确允许项） | 别关掉安全审核 |
| 长上下文退化 | 长文档场景准确率↓ | D（长上下文场景路由回旧模型） | 别全局回滚（短场景本来变好了） |
| 不可用/限流 | 429 率↑ | 多供应商 failover（第 25 章） | 别只加重试（会放大 429） |

> 🎯 一句话：**「供应商升级」在你的系统里必须表现为「一次候选发布」，而不是一次既成事实。** 做不到这一点，你就把行为的控制权交给了别人。

---

## 42.5 Java / 分布式经验对照：哪些能直接搬，哪些会咬人

| 你熟悉的 | Agent 平台的对应物 | 同构点 | 会咬人的地方 |
| --- | --- | --- | --- |
| Argo Rollouts / 金丝雀发布 | 灰度控制器 | 分阶段、按百分比、可暂停 | 指标不是 HTTP 5xx，是**质量分数**，采集延迟 15 分钟，不能秒级判定 |
| `analysis` + Prometheus 查询 | 门禁与护栏 | 声明式阈值 | 质量指标要 `min_samples`，否则小样本误杀 |
| Spring Cloud Config / Apollo | 版本矩阵的路由表 | 配置外置 + 热更 | 配置内容是「行为」，不是「参数」 |
| 蓝绿部署 | 双跑 Shadow | 两份并存 | 影子**不能有副作用**，要工具替身 |
| 数据库迁移的 expand-contract | 提示词/工具 schema 演进 | 双写 → 迁移 → 清理 | 提示词没有 schema 版本，只能靠命名约定 |
| 单元测试 + 覆盖率门槛 | 评测门禁 | 不过不合并 | 覆盖率是确定的，评测是**统计的**，必须给样本量与置信区间 |
| 灰度回滚 = 换镜像 | L1 路由回滚 | 都是回退 | 差 50 倍速度：5 秒 vs 25 分钟，别用部署做回滚 |
| Maven 依赖版本锁定 | 模型快照锁定 | 可复现构建 | 供应商可能**不给你留旧快照**，要有备选供应商 |
| 上线 checklist | 发布单（release ticket） | 流程化 | checklist 要包含「回滚演练过吗」 |

---

## 42.6 开源坐标：这块别人做到哪，缺什么

| 工具 | 它有什么 | 它缺什么（你要自己补的） |
| --- | --- | --- |
| **MLflow Model Registry** | 模型版本、阶段/别名（新版用 alias 替代了早期 stage，以官方文档为准）、lineage | 只管「模型」这一个维度；没有成本/延迟门槛；没有五维矩阵 |
| **KServe** | `InferenceService` 的 `canaryTrafficPercent` 做流量切分、predictor 版本管理、scale-to-zero | 切的是**推理服务**流量，不是「租户/场景」维度；不理解质量指标 |
| **Argo Rollouts** | `AnalysisTemplate` + `AnalysisRun`，canary steps（setWeight/pause）、自动回滚 | 分析指标默认是 Prometheus 的可用性/延迟；质量指标要自己造 exporter；不理解「Run 内版本粘性」 |
| **Langfuse Prompt Management** | 提示词版本、label（production/latest）、按 label 取模板 | 只覆盖 5 个维度里的 1 个；没有跨维度组合与门禁 |
| **LiteLLM Router** | 多模型负载均衡、fallback、按权重分流、成本表 | 是**运行时路由**，不是**发布流程**；没有门禁与切片判定 |
| **OpenAI / Anthropic 的模型弃用公告** | 提前公告弃用时间 | 不给「行为变更」的细节；快照策略各家不同，必须自己测 |

> 这块的开源现状可以概括成：**「模型版本管理」有人做了，「行为版本管理」没人做。** 你的自研重点就是把五个维度合成一个 `release_id`，并让它贯穿 trace、账单、评测、路由。

---

## 42.7 事故清单

- [ ] **事故 A：200 条评测集全绿，全量切换后工具调用成功率掉 25pp。**
  现象：解析器宽容模式把缺失字段填零值，下游 400。根因：门禁只有总体通过率，没有工具调用切片。兜底：切片门禁（每切片 ≥250 条）+ 参数解析器改严格模式 + 灰度 5% 起步。
- [ ] **事故 B：供应商静默升级，两天后才发现。**
  现象：某天开始「答非所问」变多，无任何部署记录。根因：模型用字符串名调用，未锁快照，也未检测版本串漂移。兜底：ModelCard 记录 `snapshot` + 每 6 小时探针请求比对响应头里的版本串，变化即告警。
- [ ] **事故 C：灰度扩量时同一租户在 A/B 之间跳。**
  现象：用户投诉「它一会儿记得我一会儿不记得」。根因：分桶用 `rand()`，且 Run 中途换版本。兜底：一致性哈希 + 固定 salt + Run 内版本粘性。
- [ ] **事故 D：回滚成功，但坏数据还在。**
  现象：回滚后成功率恢复，第二天财务发现 4,100 笔重复退款。根因：只回滚了路由，没修复已产生的副作用。兜底：`remediation_queue` + 按 release_id 捞失败 Run 做补偿。
- [ ] **事故 E：影子跑把生产环境的邮件发了。**
  现象：客户收到 3,000 封「测试邮件」。根因：影子 Run 用的是真实工具集。兜底：影子必须注入 dry-run 工具替身，写操作在网关层直接拒绝。
- [ ] **事故 F：护栏误杀，团队把护栏关了。**
  现象：灰度 3 分钟就被自动回滚，反复 4 次，最后有人把 `guards` 注释掉，一周后真事故无人拦。根因：`min_samples` 太小（30），且窗口只有 3 分钟。兜底：最小样本 300 + 窗口 15 分钟 + 分级动作（alert → freeze → rollback）。
- [ ] **事故 G：供应商下线旧快照，你被迫升级。**
  现象：某天 404，发现旧版本已下线。根因：`deprecated_at` 没人看。兜底：弃用日提前 60/30/7 天三级告警 + 常态化保留一个备选供应商的等价评测。

---

## 42.8 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 版本矩阵 | 模型 × 提示词 × 角色卡 × 工具集 × 路由 = 576 万组合；`release_id` 是唯一真相 |
| 五个版本号 | 必须进 Run 元数据，否则线上效果不可复现，复盘成本 ×100 |
| 能力画像 | ModelCard 要有**实测**有效上下文、工具可靠性、缓存折扣、弃用日 |
| 门禁 | 总体通过率是骗人的；必须切片，每切片 ≥250 条 |
| 样本量 | n=200 只能挡 8.4pp；要挡 2.7pp 得上 2,000 条（`MDD=sqrt(1.411/n)`） |
| 灰度 | 三把刀：租户白名单 / 一致性哈希流量 / 场景定向；必须有排除名单 |
| 分桶 | 固定 salt，扩量只改 percent，保证「先灰的还在灰里」 |
| 双跑 | 只读工具替身 + 独立预算 + 独立 run_id；1% 采样 7 天 ≈ $2 万 |
| 回滚 | L1 改路由 **5 秒**，L4 换镜像 25 分钟；别用部署做回滚 |
| 回滚之后 | 数据修复（remediation_queue）+ 2 小时冻结期 |
| 退化处置 | 供应商升级必须表现为「候选发布」；四条路径按代价排序 |
| 一句话 | **你上的不是代码，是行为；行为不可 diff，只能灰度、双跑、回退。** |

## 42.9 自检题

1. **你的团队要换模型，理由是「便宜 40%、评测分数还高 2 个点」。请给出你的上线方案，并说出至少三个会拦住你的点。**
   > 要点：① 换模型 = 新 release_id，五个维度冻结；② 门禁要按切片看（工具调用/多轮/长上下文/中文/拒答/RAG），200 条总体通过率不够；③ 先影子 1% 跑 3~5 天看一致性与成本，再灰度 5%→25%→100%，每档 6 小时；④ 排掉支付动作、合同审查场景与铂金租户；⑤ 护栏带 `min_samples=300`、15 分钟窗口；⑥ 准备好 L1 回滚与旧快照仍可用的确认。

2. **为什么「Run 中途不允许换版本」？如果灰度扩量时确实想让更多流量用新版本，怎么做？**
   > 要点：Run 是长事务，上下文里已有旧版本的输出与工具结果，混版本会产生未测过的行为，且 trace 里一个 Run 有两个 release_id，归因失效。正确做法：**只对新 Run 生效**；扩量改的是「新建 Run 的分配比例」，在途 Run 跑完为止。代价是扩量生效有延迟（最长一个 Run 的时长），所以灰度节奏要按 Run 时长（30 秒~30 分钟）来设计，不能 5 分钟一档。

3. **回滚失败了怎么办？请给出你的第二、第三预案。**
   > 要点：① 回滚失败通常是「旧版本也不可用」（供应商下线/依赖变更），所以要 L2 kill switch 先止血（降级到模板回复，第 25 章）；② 若配置推送本身故障，走「实例本地 last-known-good 快照」兜底（第 43 章），必要时重启实例回落到磁盘快照；③ 最后手段是多供应商 failover + 降级阶梯，接受质量下降换可用性；④ 全程要有对外话术与状态页。

## 42.10 延伸阅读

- 上一章：[41. 离线数据消费与回流](41-offline-data-loop.md)
- 下一章：[43. 策略中心与 Feature Flag](43-policy-center.md)（`release_id` 怎么下发、怎么保证 5 秒生效）
- 总纲：[0-summary.md](0-summary.md)（§1 把 LLM 当 CPU：模型与提示词灰度 ≈ 内核模块热更新）
- 相关：[40. 评估体系](40-evaluation-system.md)（门禁的指标怎么来）、[41. 离线数据消费与回流](41-offline-data-loop.md)（切片评测集怎么长出来）、[26. 模型路由与多策略](26-model-routing.md)（路由权重与 bandit）、[39. 可观测层](39-observability.md)（按 release_id 切片）、[48. 测试策略](48-testing-strategy.md)（录制回放做门禁）
- 外部：MLflow Model Registry、KServe `InferenceService`、Argo Rollouts `AnalysisTemplate`、Langfuse Prompt Management、LiteLLM Router 文档（版本与参数以官方最新文档为准）
- 语言细节：[../go/34-testing.md](../go/34-testing.md)（表驱动与统计式断言）、[../go/40-llm-client-streaming.md](../go/40-llm-client-streaming.md)（客户端如何携带版本标）
