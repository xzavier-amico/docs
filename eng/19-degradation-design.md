# 19. 降级与兜底设计

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 流量与韧性：不崩是怎么做到的 ｜ **编号**：19 / 52
> **本章定位**：降级不是「catch 住异常返回 null」，而是一条有台阶的楼梯：正常 → 缓存兜底 → 简化逻辑 → 静态文案 → 明确失败。每一级都要有触发条件、实现方式、用户体验和回滚条件，而且**不能悄悄降级**。
> **读法**：§19.3 的阶梯表可以直接贴到设计文档；§19.4 的决策表与开关表结构是落地的最小集；Agent 读者重点看 §19.4 末尾的「换智能」路由代码。

---

## 19.1 引子：降级「成功」了 6 小时，公司赔了 92 万

2026-01-16 09:00，某电商年货节，价格服务 `price-service` 的 P99 从 80ms 涨到 2.3s。

09:05，价格服务的熔断器打开，商品详情页的 `PriceClient` 走了 fallback：**读本地缓存**。缓存里有价格，TTL 24 小时，接口返回 200，页面正常渲染。监控上：

- 错误率 0%；
- 降级率 100%（但没人看这个指标）；
- 客诉 0（前三个小时）。

问题在于：09:00 正好是年货节促销价生效的时刻，缓存里是**促销前**的价格。于是：

```
09:05—15:00，共 6 小时
3,800 笔订单按旧价成交
平均每笔少收 242 元
3,800 × 242 ≈ 92 万元
另有 1,200 条客诉：「为什么结算价和页面价不一样？」
```

更糟的是，15:00 修复后，团队把这次事故定性为「价格服务故障」，而不是「降级设计故障」。三个月后，同样的 fallback 在推荐服务上又演了一次：推荐缓存过期，首页推的全是下架商品。

> 🩸 **最刺眼的一点**：降级本身没有失败，它「成功」地把错误数据喂给了用户。**没有对外表达的降级，等于撒谎。**

---

## 19.2 到底是什么：主动降低功能/质量，保住核心

**定义**：降级 = 在资源不足或依赖故障时，**主动、有意识地**降低非核心功能的质量或可用性，把资源让给核心功能，并让用户/调用方知道当前处于降级状态。

四个关键词：

- **主动**：不是异常冒泡，而是提前设计的路径。
- **有意识**：知道自己在降什么、降多少、降多久。
- **让给核心**：降级的收益必须能算出来（省了多少连接/CPU/成本）。
- **让人知道**：对外可见，不能悄悄降。

### 五个反例

| 你以为的降级 | 为什么不是 | 不做会怎样 |
| --- | --- | --- |
| `catch (Exception e) { return null; }` | 这是吞异常，不是降级；调用方拿到 null 会二次崩溃 | 空指针、白屏、更难的排查 |
| 返回空列表 `[]` | 用户看到「没有数据」，不知道是没数据还是坏了 | 客诉「你们的推荐怎么没了」 |
| 降级后不记录、不告警 | 监控全绿，团队以为系统健康 | 19.1 的 6 小时 stale price |
| 所有用户一起降 | 没有灰度，故障面最大化 | 本来只影响 1% 的请求，变成 100% |
| 降级永久生效 | 没有回滚条件，缓存永远不刷新 | 系统「恢复」了，数据还是旧的 |

> 💡 **降级与熔断的分工**：熔断决定「不再调用下游」，降级决定「不调用之后给用户什么」。熔断是开关，降级是台阶。

---

## 19.3 结构图：五级降级阶梯

```
用户请求
   │
   ▼
┌─────────────────────────────────────────────────────────────┐
│ L0 正常：实时计算 / 实时调用 / 最新数据                       │
│    触发：无                                                   │
│    体验：完整功能                                             │
└───────────────────────────┬─────────────────────────────────┘
                            │ 依赖 P99 > 阈值 / 错误率 > 阈值 / 容量 > 80%
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ L1 缓存兜底：读最近一次成功结果 / 本地缓存 / CDN              │
│    触发：下游超时、熔断打开                                   │
│    体验：数据可能旧，功能完整；必须显示「数据截至 xx:xx」       │
└───────────────────────────┬─────────────────────────────────┘
                            │ 缓存也失效 / 缓存未命中率 > 30%
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ L2 简化逻辑：去掉非核心步骤（排序、个性化、二次校验）          │
│    触发：CPU > 80% / 队列 > 阈值                              │
│    体验：结果不那么「懂我」，但仍有结果                        │
└───────────────────────────┬─────────────────────────────────┘
                            │ 简化逻辑仍扛不住 / 依赖全挂
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ L3 静态文案：预置兜底页 / 热门榜单 / 模板话术                  │
│    触发：核心依赖不可用 / 降级超过 N 分钟                      │
│    体验：功能缺失但页面不空；明确提示「当前为简化版」           │
└───────────────────────────┬─────────────────────────────────┘
                            │ 连静态资源都不可用 / 合规风险
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ L4 明确失败：503 + 错误码 + Retry-After + 人工入口             │
│    触发：无兜底可用 / 数据不可信                               │
│    体验：明确告知失败原因与下一步，不假装成功                  │
└─────────────────────────────────────────────────────────────┘
```

### 五级阶梯的完整定义

| 级别 | 触发条件 | 实现方式 | 用户体验 | 数据新鲜度 | 回滚条件 |
| --- | --- | --- | --- | --- | --- |
| L0 正常 | 无 | 实时调用 | 完整 | 实时 | — |
| L1 缓存兜底 | 下游 P99 > 1s 或熔断打开 | 本地/Redis 缓存最近成功结果，TTL 分层 | 功能完整，可能旧 | 分钟~小时 | 下游 P99 恢复 < 500ms 持续 5min |
| L2 简化逻辑 | CPU > 80% 或队列 > 1,000 | 去掉排序/个性化/二次校验，保留主流程 | 结果变粗 | 实时 | CPU < 60% 持续 10min |
| L3 静态文案 | 核心依赖不可用 > 5min | 预置 JSON/HTML/热门榜单/模板话术 | 功能缺失，页面不空 | 静态 | 依赖恢复 + 数据校验通过 |
| L4 明确失败 | 无兜底或数据不可信 | 503 + 错误码 + `Retry-After` + 客服入口 | 明确失败 | — | 故障修复 + 灰度验证 |

> ⚠️ **L1 是最危险的一级**：它看起来「正常」，最容易悄悄运行几个小时。L1 必须带三个东西：**数据时间戳、降级标记、过期上限**。超过上限（例如价格 15 分钟、库存 1 分钟）必须降到 L3/L4，不能无限期用旧数据。

---

## 19.4 怎么实现：开关、着色、决策表与对外表达

### 功能开关表结构

```sql
CREATE TABLE feature_flag (
  flag_key     VARCHAR(64)  NOT NULL,
  enabled      TINYINT(1)   NOT NULL DEFAULT 0,
  level        TINYINT      NOT NULL DEFAULT 0,  -- 0 正常,1 缓存,2 简化,3 静态,4 失败
  rollout_json JSON         NOT NULL,            -- 灰度规则
  owner        VARCHAR(64)  NOT NULL,
  expires_at   DATETIME(3)  NULL,                -- 到期自动回滚，防止忘记
  version      BIGINT       NOT NULL DEFAULT 1,
  updated_at   DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                            ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (flag_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE feature_flag_audit (
  id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  flag_key   VARCHAR(64)     NOT NULL,
  old_value  JSON            NOT NULL,
  new_value  JSON            NOT NULL,
  operator   VARCHAR(64)     NOT NULL,
  reason     VARCHAR(255)    NOT NULL,
  created_at DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  KEY idx_flag_time (flag_key, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 例：价格服务降级到 L1，只对 10% 租户灰度，2 小时后自动过期
INSERT INTO feature_flag (flag_key, enabled, level, rollout_json, owner, expires_at)
VALUES ('price.degrade', 1, 1,
        '{"percentage":10,"tenant_ids":[1001,1002],"regions":["cn-east"]}',
        'price-team', NOW(3) + INTERVAL 2 HOUR);
```

### 流量着色

着色解决「降级给谁」：不是所有用户一起降，而是按租户/地域/百分比/请求头灰度。

```yaml
# 网关规则：把标记写进请求头，下游所有服务统一读取
- match:
    headers:
      X-Release-Tag: "degrade-v2"
  route:
    cluster: price-service-v2
  metadata:
    degrade_level: 1
```

```go
package degrade

import (
	"hash/fnv"
	"strconv"
)

type Rollout struct {
	Percentage int      `json:"percentage"`
	TenantIDs  []int64  `json:"tenant_ids"`
	Regions    []string `json:"regions"`
}

// Match 判断某个租户是否命中灰度；同一租户必须稳定命中（用哈希，不用随机）
func (r Rollout) Match(tenantID int64, region string) bool {
	for _, id := range r.TenantIDs {
		if id == tenantID {
			return true
		}
	}
	if len(r.Regions) > 0 {
		ok := false
		for _, reg := range r.Regions {
			if reg == region {
				ok = true
				break
			}
		}
		if !ok {
			return false
		}
	}
	if r.Percentage <= 0 {
		return false
	}
	h := fnv.New32a()
	h.Write([]byte(strconv.FormatInt(tenantID, 10)))
	return int(h.Sum32()%100) < r.Percentage
}
```

### 降级决策表（必须写，且必须有负责人）

| 触发条件 | 降级级别 | 影响面 | 回滚条件 | 负责人 | 对外表达 |
| --- | --- | --- | --- | --- | --- |
| 价格服务 P99 > 1s 持续 3min | L1 缓存 | 商品详情价格可能旧 | P99 < 500ms 持续 5min | 价格团队 on-call | 「价格更新于 10:05」 |
| 价格缓存超过 15min | L3 静态 + 禁止下单 | 详情可看，下单跳转 | 价格服务恢复 + 对账通过 | 价格 + 交易 | 「价格暂不可用，请稍后」 |
| LLM 供应商 429 > 20% | L2 小模型 | 回答质量下降 | 429 < 5% 持续 10min | AI 平台 on-call | 「当前为快速模式」 |
| LLM 全部供应商不可用 | L3 模板话术 | 只能回答常见问题 | 任一供应商恢复 | AI 平台 + 客服 | 「智能客服繁忙，可转人工」 |
| DB 连接 > 85% | L2 关非核心查询 | 推荐/榜单不更新 | 连接 < 60% 持续 10min | DBA + 业务 | 「榜单更新延迟」 |
| 大促流量 > 2× 设计值 | L2 + 限流 | 非核心功能排队 | 流量 < 1.5× | 大促指挥 | 状态页公告 |

> 💡 **决策表的三条硬规矩**：① 每一行必须有**数字触发条件**，不能写「系统压力大」；② 每一行必须有**回滚条件**，否则降级会永久化；③ 每一行必须有**一个人**，不能写「团队」。

### 为什么不能悄悄降级

| 原因 | 悄悄降级的后果 | 正确做法 |
| --- | --- | --- |
| 用户信任 | 用户以为看到的是最新价格/库存，实际是旧数据 | 页面显示「数据截至 xx:xx」 |
| 计费/合规 | 降级后仍按完整服务收费，或数据不合规 | 响应头 `X-Degraded` + 账单标记 |
| 排障 | 监控全绿，没人知道系统在降级 | `degraded_responses_total{level,reason}` 指标 |
| 复盘 | 事后无法还原「降了多久、影响多少请求」 | 降级开始/结束写审计表 |
| 回滚 | 忘记关开关，缓存永远不刷新 | `expires_at` 自动过期 + 定时巡检 |

对外表达的三种形态：

```http
HTTP/1.1 200 OK
X-Degraded: price-cache
X-Data-As-Of: 2026-01-16T08:00:00Z
X-Degrade-Reason: price-service-timeout
```

```json
{
  "code": "OK",
  "data": { "price": 199.00 },
  "degraded": true,
  "data_as_of": "2026-01-16T08:00:00Z",
  "notice": "价格可能不是最新，以结算页为准"
}
```

```yaml
# 状态页
incident:
  title: "商品价格更新延迟"
  impact: "部分商品详情页价格可能不是最新，结算页价格准确"
  started_at: "2026-01-16T09:05:00+08:00"
  status: "monitoring"
```

### Agent 侧：降级不是「返回 null」，是「换智能」

Agent 的降级阶梯和传统系统同构，但每一级换的是「智能程度」：

| 级别 | Agent 降级动作 | 质量损失 | 成本变化 | 触发 |
| --- | --- | --- | --- | --- |
| L1 | 语义缓存 / 上次成功结果 | 可能过时 | 成本≈0 | 模型超时、429 |
| L2 | **换小模型** | 质量 -10%~20% | 成本 -70%~95% | 大模型 P99 > 15s 或配额不足 |
| L3 | **关闭工具调用**，只基于上下文回答 | 不能查实时数据 | 成本 -30% | 工具错误率 > 50% |
| L4 | **模板话术** / 返回检索原文 | 只能回答常见问题 | 成本≈0 | 所有模型不可用 |
| L5 | 明确失败 + 转人工 | — | — | 合规/安全风险 |

```go
package agent

import (
	"context"
	"errors"
)

type Level int

const (
	Normal Level = iota
	CacheFallback
	SmallModel
	NoTools
	Template
	ExplicitFailure
)

type RunRequest struct {
	TenantID int64
	Query    string
	Context  []Document
}

type RunResult struct {
	Answer   string
	Model    string
	Degraded bool
	Level    Level
	Sources  []Document
}

type Degrader struct {
	flags    FlagStore
	large    Model
	small    Model
	tools    ToolRegistry
	cache    SemanticCache
	template TemplateEngine
}

func (d *Degrader) Run(ctx context.Context, req RunRequest) (RunResult, error) {
	level := d.flags.Level(req.TenantID, "agent.run")

	switch level {
	case Normal:
		return d.runWithTools(ctx, d.large, req)

	case CacheFallback:
		if r, ok := d.cache.Get(ctx, req.Query); ok {
			r.Degraded, r.Level = true, CacheFallback
			return r, nil
		}
		level = SmallModel // 缓存未命中，继续下一级

	case SmallModel:
		r, err := d.runWithTools(ctx, d.small, req)
		r.Degraded, r.Level = true, SmallModel
		return r, err

	case NoTools:
		// 关闭工具调用：只基于检索到的上下文回答，避免外部副作用
		r, err := d.small.Generate(ctx, buildPrompt(req.Context, req.Query))
		return RunResult{Answer: r, Model: "small-no-tools", Degraded: true, Level: NoTools}, err

	case Template:
		return RunResult{
			Answer:   d.template.Render("fallback", req.Query),
			Model:    "template",
			Degraded: true,
			Level:    Template,
		}, nil

	default:
		return RunResult{}, errors.New("service temporarily unavailable")
	}

	return RunResult{}, errors.New("unreachable")
}

func (d *Degrader) runWithTools(ctx context.Context, m Model, req RunRequest) (RunResult, error) {
	// 真实实现：模型 + 工具循环；这里省略
	return RunResult{Answer: "ok", Model: "model"}, nil
}
```

**「返回检索原文」**是 Agent 特有的好兜底：不生成、不调用工具，直接把 top-k 文档和链接返回给用户。它不会幻觉，代价是用户要自己读，但比「模型编一个答案」安全得多。

成本算例：大模型输出 $10/M token，小模型 $0.6/M token；一次回答 2,000 输出 token：

```
大模型：2,000 × 10/1,000,000 = $0.020
小模型：2,000 × 0.6/1,000,000 = $0.0012
省 94%，但质量可能从 92 分掉到 78 分——所以必须标注「快速模式」
```

---

## 19.5 双修对照：降级阶梯是同一把尺子

| 维度 | 传统系统 | Agent 系统 | 同 or 异 |
| --- | --- | --- | --- |
| 机制本体 | 缓存、简化逻辑、静态页、明确失败 | **完全相同**的阶梯；每级换成「换智能」 | **同** |
| 身份/唯一键 | `tenant:route` | `tenant:run_id` + `model` + `prompt_version` | 同构，多一层版本 |
| 降的是什么 | 功能、数据新鲜度、排序质量 | **模型能力、工具权限、上下文长度** | **异**（降的是智能） |
| 失败模式 | 旧价格、空列表、页面空白 | 幻觉、答非所问、工具副作用、成本失控 | 多两类 |
| 恢复方式 | 缓存刷新、依赖恢复 | 模型 failback + **评测集验证** | 同 + 质量门禁 |
| 验证方法 | 合成拨测、对账、客诉 | 同 + **离线评测 / LLM-as-judge** | 同 + 质量评估 |
| 成本结构 | 机器折旧，降级省资源 | **按 token 计费，降级直接省钱** | **异**（根因：成本显性且按量） |
| 观测指标 | `degraded_responses_total`、缓存命中率 | 加 `model`、`quality_score`、`cost_per_run`、`tool_calls` | 指标集扩展 |

**哪些完全相同、可以直接复用**：五级阶梯、功能开关表、流量着色、决策表模板、对外表达（`X-Degraded` / 状态页）、降级演练流程。**这些与「降的是价格还是模型」无关。**

**差异根因**：传统降级的正确性可以用「数据是否新鲜」判断；Agent 降级的正确性要看「回答是否仍然有用」。所以 Agent 必须有**质量 SLO**（第 21 章）和评测集，否则你降级到小模型后，错误率 0%、客诉 0，但答案质量已经掉了一半——这是最隐蔽的降级。

Agent 侧度量：TPM 是否因小模型而下降、并发 Run 是否因降级而释放、单 Run 预算是否因换模型而降低、供应商配额是否因此腾出。四个数要一起看，不能只看「成本降了」。

---

## 19.6 会在哪炸：失效模式清单

| 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 降级后数据旧了 6 小时 | 缓存无时间戳/无过期上限 | 看 `X-Data-As-Of` 与缓存 TTL | 加时间戳、过期上限、L3 兜底 | 降级决策表写「最大 stale 时长」 |
| 用户不知道在降级 | 没有对外表达 | 看响应头/页面提示 | 加 `X-Degraded` + 文案 | 上线检查清单 |
| 降级开关忘了关 | 没有过期时间/巡检 | 看 `feature_flag.expires_at` | 自动过期 + 定时任务 | 所有开关必须带 TTL |
| 降级后仍然 500 | fallback 自己也调下游 | 看 fallback 的依赖 | fallback 必须无依赖/短超时 | 演练时只开降级、不杀下游 |
| 所有用户一起降 | 没有着色/灰度 | 看降级请求的租户分布 | 按租户/百分比灰度 | 开关默认 1% 起步 |
| Agent 降级后幻觉更多 | 小模型 + 无工具 + 长上下文 | 看评测分数与工具调用 | 降级时缩短上下文、返回检索原文 | 质量 SLO + 评测门禁 |
| 降级期间仍按全价计费 | 计费没读降级标记 | 对账 `degraded` 与账单 | 账单打折/标记 | 计费链路读 `X-Degraded` |
| 降级恢复后缓存没刷新 | 只关开关，没清缓存 | 看缓存版本 | 恢复时主动 refresh | 回滚脚本包含缓存刷新 |

---

## 19.7 怎么验证：演练、拨测、对账

**① 降级演练（每季度一次，必须真做）：**

```bash
# 1) 在预发把价格服务延迟调到 3s
kubectl patch deployment price-service -p \
  '{"spec":{"template":{"metadata":{"annotations":{"chaos/delay":"3s"}}}}}'

# 2) 观察 5 分钟：是否自动进入 L1？响应头是否有 X-Degraded？
curl -s -D - "https://pre.example.com/api/product/1001" | grep -i degrade

# 3) 把缓存时间戳改成 30 分钟前，验证是否自动降到 L3/L4
# 4) 恢复，验证是否在 5 分钟内回到 L0，缓存是否刷新
```

**② 合成拨测**：每分钟请求一次核心接口，断言：

- 状态码 200/503 符合预期；
- 若 `X-Degraded: true`，必须有 `X-Data-As-Of`；
- 价格类接口的 `data_as_of` 不得超过 15 分钟；
- 降级响应时间 < 500ms。

**③ 对账 SQL**：降级期间成交价 vs 实时价：

```sql
SELECT o.order_id, o.price AS paid, p.price AS current,
       o.created_at, o.degraded
FROM orders o
JOIN product_price p ON p.product_id = o.product_id
WHERE o.created_at >= '2026-01-16 09:00:00'
  AND o.created_at <  '2026-01-16 15:00:00'
  AND ABS(o.price - p.price) > 0.01
ORDER BY o.created_at
LIMIT 100;
```

**④ 必看指标：**

```promql
# 降级响应占比（按级别/原因）
sum by (level, reason) (rate(degraded_responses_total[5m]))
  / sum(rate(http_requests_total[5m]))

# 降级持续时间
max by (flag) (feature_flag_enabled_duration_seconds)

# Agent 降级后的质量（评测分数）
avg by (model, level) (agent_quality_score)

# 降级期间的成本
sum by (level) (rate(agent_cost_usd_total[5m]))
```

---

## 19.8 用什么工具

| 场景 | 首选 | 理由 / 坑 |
| --- | --- | --- |
| 功能开关 | 自研表 + 配置中心 / Unleash / LaunchDarkly | 自研要带审计、TTL、灰度规则；SaaS 注意数据合规 |
| 流量着色 | 网关 header + 服务内拦截器 | 全链路透传 `X-Release-Tag`，别只在一层读 |
| Java 降级 | Sentinel `fallback` / Resilience4j `fallback` | fallback 必须无下游依赖、超时更短 |
| Go 降级 | 手写 router + `atomic` 开关 | 简单可控；别把 fallback 写成递归调用 |
| 静态兜底 | CDN + 对象存储 + 预置 JSON | 静态资源也要有版本和过期时间 |
| Agent 降级 | 模型路由 + 模板引擎 + 检索直出 | 需要评测集验证质量，不能只看错误率 |

**自研边界**：开关表、决策表、对外表达必须自研（和业务/合规绑定）；**不要自研的是「配置下发的高可用」**——用成熟的配置中心，别自己写长轮询。

---

## 19.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 定义 | 降级 = 主动降低非核心功能/质量，保住核心，并让用户知道 |
| 五级阶梯 | 正常 → 缓存 → 简化 → 静态 → 明确失败 |
| L1 最危险 | 看起来正常，最容易悄悄跑几小时；必须带时间戳 + 过期上限 |
| 触发条件 | 必须是数字：P99、错误率、CPU、队列、连接数 |
| 回滚条件 | 每一级都要有，否则降级永久化 |
| 决策表 | 触发/级别/影响面/回滚/负责人/对外表达，六列缺一不可 |
| 流量着色 | 按租户/地域/百分比，哈希稳定命中，默认 1% 起步 |
| 对外表达 | `X-Degraded` + `X-Data-As-Of` + 页面提示 + 状态页 |
| 不能悄悄降 | 用户信任、计费合规、排障、复盘，四个理由 |
| Agent 降级 | 缓存 → 小模型 → 关工具 → 模板/检索原文 → 明确失败 |
| Agent 成本 | 小模型省 94%，但质量可能掉 14 分；必须评测 + 标注 |
| 演练 | 每季度真做一次，验证自动降级、自动恢复、缓存刷新 |

---

## 19.10 自检题

1. **价格服务熔断后，fallback 读缓存返回 200，监控错误率 0%。为什么这是最危险的降级？你怎么在 5 分钟内发现它？**
   <details><summary>答案要点</summary>
   因为「成功返回旧数据」比「明确失败」更危险：用户和监控都以为正常，错误数据会持续成交（19.1 的 6 小时 92 万）。发现方法：① 降级指标 `degraded_responses_total` 与 `X-Data-As-Of` 年龄；② 价格类接口的 `data_as_of` 超过 15 分钟告警；③ 成交价 vs 实时价对账 SQL；④ 合成拨测断言数据新鲜度。
   </details>

2. **降级决策表为什么必须有「回滚条件」和「负责人」？只写触发条件会怎样？**
   <details><summary>答案要点</summary>
   只写触发条件，降级会变成「单向门」：故障恢复了没人知道该关，或者不敢关，缓存永远旧。回滚条件给出「什么信号出现时可以恢复」（如 P99 < 500ms 持续 5min）；负责人给出「谁有权关、谁负责验证」。没有这两列，降级就从临时措施变成永久配置。
   </details>

3. **Agent 从大模型降级到小模型，错误率仍然是 0%，你怎么判断这次降级是否可接受？**
   <details><summary>答案要点</summary>
   错误率只说明「没报错」，不说明「答得好」。要看质量 SLO：离线评测集分数、LLM-as-judge、用户采纳率/追问率、工具成功率；同时看成本（单 Run 预算）与延迟（TTFT）。设定阈值：质量分下降不超过 10 分、任务成功率不低于 85%、成本下降 ≥ 50% 才算「可接受降级」；否则应继续降到模板/检索原文，或明确失败。
   </details>

---

## 19.11 延伸阅读

- Google SRE Workbook：*Managing Load* 与 *Postmortem Culture* 中的降级与优雅退化
- AWS Well-Architected：*Reliability Pillar* 的 graceful degradation 模式
- Sentinel `fallback` 与 Resilience4j `FallbackDecorator` 的语义差异
- Feature flag 的渐进放量与审计：Unleash / LaunchDarkly 文档
- 上一章：[第 18 章 熔断：状态机、阈值与隔离](18-circuit-breaker.md)
- 下一章：[第 20 章 监控指标体系：到底该采什么](20-metrics-system.md)

**交叉引用**：总纲 [0-summary.md](0-summary.md) ｜ 语言与运行时 [../go/0-summary.md](../go/0-summary.md) ｜ 平台治理 [../llm/0-summary.md](../llm/0-summary.md)
