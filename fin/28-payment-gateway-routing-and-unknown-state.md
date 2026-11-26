# 28. 支付网关路由与 UNKNOWN 收敛：多渠道、状态仲裁和支付事故工程

> **状态**：生产级工程篇｜**所属**：第三部分·金融工程代码基座｜**编号**：28 / 48
>
> **本章定位**：承接 [24 金融级全链路幂等体系](24-end-to-end-financial-idempotency.md)、[25 资金订单状态机](25-state-machines-tcc-saga-and-outbox.md) 与 [27 多层对账引擎](27-multi-tier-reconciliation-and-auto-healing.md)，把“应该调用哪个支付渠道”以及“调用结果不确定时如何不重付”落到可审计的生产实现。本文覆盖支付事故时间线、智能路由、EWMA 延迟、渠道限额与熔断、支付状态机、超时 `UNKNOWN`、查单与回调竞争仲裁、一单多付退回、部分退款、拒付、PostgreSQL DDL/协议、Go 1.24 与 Java 21 核心代码、Outbox、幂等、对账、混沌与压测、极端边界、术语、面试答题卡、自检题及与下一节的衔接。
>
> **不可妥协的红线**：路由只决定“下一次允许尝试的渠道”，不能改变同一 `operation_id` 的经济身份；超时只能进入 `UNKNOWN`，绝不能猜成失败并换渠道重付；回调、同步响应、查单和对账文件都是证据，必须以渠道事件序号/版本、金额、币种和外部引用在同一事务中仲裁，不能以到达顺序覆盖状态；一单多付必须逐笔保留并逐笔退回；退款是新的不可变操作且受未退余额约束；拒付不能伪装成普通退款；渠道限额、熔断和人工止血都必须可审计、可恢复。

---

## 28.1 事故引子：路由切换把一次超时变成两次扣款

### 28.1.1 真实可复现的时间线

某电商平台配置了 `PSP_A`、`PSP_B` 两个收单渠道。路由器只按过去一分钟成功率选渠道，没有保存渠道尝试，也没有区分“请求没有发出”和“请求已发出但响应丢失”。大促时发生如下事故：

| 时间 | 事件 | 系统行为 | 结果 |
|---|---|---|---|
| 20:00:00.000 | 创建订单 `O1`，金额 1,000 CNY | 绑定 `operation_id=P1`，选择 A | 正常 |
| 20:00:00.120 | A 已受理并向卡组织发送扣款 | A 的响应连接在代理处断开 | 平台不知道是否已产生副作用 |
| 20:00:01.000 | API deadline 到期 | 网关把超时映射为 `FAILED` | 错误事实 |
| 20:00:01.010 | 路由器选择 B | 生成新 `merchant_order_no`，再次扣款 | 失去渠道级幂等保护 |
| 20:00:01.400 | B 返回成功 | 平台入账 1,000 CNY | 客户先看到一次成功 |
| 20:00:03.000 | A 的异步回调抵达 | 回调消费者再次写入成功 | 平台账本重复入账 |
| 20:02:00 | A、B 均出现在渠道对账文件 | 对账按商户订单号聚合 | 只能看到“渠道多一笔”，无法确定谁应退款 |
| 次日 | 客户银行卡出现两笔扣款 | 人工直接改订单状态 | 原始事实和补偿证据丢失 |

根因不是 A 慢，也不是 B 不可靠，而是把四个问题混成了一个布尔值：

1. **请求是否发送**：由本地 Outbox/尝试记录证明；不能由 HTTP 客户端错误推断。
2. **外部副作用是否发生**：只能由同步明确响应、回调、查单或对账证据证明；网络超时不是否定证据。
3. **本平台是否已经接受经济事实**：由不可变账本/支付事实唯一键证明；不是由订单快照推断。
4. **下一步是否可以重试**：必须根据渠道能力、外部引用和 `UNKNOWN` 策略决定；不能由路由器的可用性分数决定。

### 28.1.2 生产问题的精确定义

> 在多个渠道能力、价格、延迟、限额和健康度不断变化，且请求可能在发送前、发送中、发送后任何时刻失败的情况下，系统能否让每个经济意图只接受一个成功扣款事实；能否把同步响应、回调、查单、对账文件的矛盾信息收敛为可解释状态；能否在重复支付、部分退款、拒付和渠道切换后留下完整的证据链？

答案不能是“给 RPC 加重试”。需要一条分层管线：

```text
稳定 operation_id / 幂等键
  → 路由快照（能力、限额、EWMA、熔断版本）
  → Outbox 持久化支付意图
  → 渠道尝试（attempt）与 fencing lease
  → 渠道原子受理/同步响应/回调/查单/文件证据
  → 事务内状态仲裁与唯一记账
  → UNKNOWN 查单窗口与人工边界
  → 退款/退回/拒付新操作
  → 实时与日终对账，修复后再次验真
```

### 28.1.3 反模式与替代方案

| 反模式 | 事故后果 | 生产替代 |
|---|---|---|
| 超时统一返回 `FAILED` | 已扣款订单被换渠道再次扣款 | `UNKNOWN` + 原渠道查单 + 原 `channel_key` |
| 按实时成功率直接选渠道 | 小样本、分母漂移、把拒付当网络失败 | 分层指标、EWMA、置信度、能力/限额硬约束 |
| 每次重试生成新的渠道订单号 | 渠道无法去重，一单多付 | 一个 `operation_id` 一个 `channel_key`；换渠道须显式策略和仲裁 |
| 回调先到就覆盖快照 | 迟到旧回调把成功改回处理中 | 事件版本/渠道序号 + 锁 + 白名单转移 |
| 查单结果先到就当最终真相 | 查询缓存/最终性不足，漏掉后续拒付 | 证据等级、观察时间窗、最终状态规则 |
| 退款 `UPDATE paid_amount` | 部分退款、并发退款和拒付无法审计 | 每笔退款新 `operation_id`，按已退额度原子占用 |
| 熔断后全量切到一个渠道 | 次级渠道限额耗尽，形成雪崩 | 按渠道预算、租户、币种、时间窗分配并背压 |
| 手工改订单为成功/失败 | 无法证明是否已记账或是否需要退回 | 结构化人工命令、证据 hash、审批、补偿操作 |

---

## 28.2 支付网关模型：路由不是重试，渠道不是状态机

### 28.2.1 四个层次

| 层次 | 代表对象 | 责任 | 不能替代 |
|---|---|---|---|
| **支付意图** | `operation_id`、金额、币种、收款人 | 表达一次经济意图，参数永久绑定 | 渠道交易 |
| **渠道尝试** | `attempt_id`、provider、`channel_key` | 记录是否向某渠道发送过及其证据 | 支付最终状态 |
| **渠道证据** | 同步响应、回调、查单、文件 | 证明渠道观察到的状态和版本 | 内部账本 |
| **内部事实** | capture/refund/chargeback 分录 | 证明平台承认的经济影响 | 渠道是否扣款 |

一个支付意图可以有多个尝试，但只有在明确安全条件下才允许多个尝试：

```text
operation P1
  ├─ attempt A: SENT / UNKNOWN（可能已扣款）──┐
  ├─ query A: SUCCESS                         ├─ 只能归并为一个支付成功事实
  └─ attempt B: NOT_SENT / REJECTED           ┘
```

若 A 已进入 `UNKNOWN`，B 不能因为“路由分数更高”自动扣款。只有在协议能证明 A **未发送** 或 A 以渠道幂等键明确拒绝且无副作用时，才可安全进入下一次尝试；否则等待查单、回调或对账。

### 28.2.2 渠道适配器协议

适配器不得向上层暴露“HTTP 200 就成功”的简化接口，而应返回以下语义：

```text
SubmitResult {
  transport: NOT_SENT | SENT | RESPONSE_RECEIVED
  business: ACCEPTED | SUCCESS | DECLINED | UNKNOWN
  provider_ref: string?       // 渠道交易号，若已获得
  provider_event_version: string?
  retry: NEVER | SAME_KEY_ONLY | QUERY_FIRST | SAFE
  raw_hash: bytes32
  observed_at: timestamp
}
```

协议要求：

1. `SENT` 只表示客户端至少把请求交给了传输层，不表示渠道已受理。
2. `RESPONSE_RECEIVED + DECLINED` 才是可明确失败；连接超时、DNS 失败、读超时一律不能自动映射为 `DECLINED`。
3. 渠道适配器必须携带 `channel_key`；若渠道不支持幂等，要把能力标成 `NO_IDEMPOTENCY`，并禁止对支付副作用自动重试。
4. 回调签名、商户号、币种、金额、渠道引用必须在适配器层验证；校验失败只能隔离，不能进入状态机。
5. 渠道版本、请求/响应原文 hash、签名 key 版本和时钟偏差必须落库；日志不能代替证据。

### 28.2.3 渠道能力矩阵

路由输入不是一个分数，而是先过滤硬约束，再在候选集合内计算软分数：

| 能力 | 必须确认的维度 |
|---|---|
| 支付 | 币种、地区、卡组织、MCC、3DS、支付方式、金额上下限 |
| 幂等 | 渠道键长度/保留期/作用域、重复请求返回语义 |
| 查单 | 查询键、最终状态定义、频率限制、延迟和历史保留期 |
| 回调 | 签名、事件序号、重放保护、状态集合、延迟 SLA |
| 退款 | 原路/非原路、部分退款、最大次数、退款窗口、异步语义 |
| 拒付 | webhook/文件、证据截止时间、申诉动作、资金冻结规则 |
| 结算 | 价值日、手续费、汇率、批次文件、结算账户 |

硬约束失败时渠道不可选，不应通过提高权重“补偿”。渠道健康状态也不能绕过业务限额：`CLOSED` 只说明可以尝试，不说明还有余额、额度或合规授权。

---

## 28.3 智能路由：EWMA、限额、熔断与公平退避

### 28.3.1 目标函数与可解释性

一个可解释的渠道选择可以使用约束优化，而不是黑盒模型：

```text
候选 C = 满足能力、合规、租户授权、币种、金额、时间窗和剩余额度的渠道

score(c) =
    w_success * success_ewma(c, segment)
  - w_latency * p95_ewma(c, segment)
  - w_cost * fee(c, quote)
  - w_risk * risk_penalty(c, segment)
  + w_diversity * route_diversity_bonus(c)
  + exploration(c, confidence)
```

`segment` 至少包含商户/租户、支付方式、币种、国家、金额区间、3DS 结果和渠道版本。不能把所有流量混在一个成功率里：一个渠道在 USD 卡支付上成功率高，不代表在 CNY 钱包支付上可用。

路由决策必须保存：`policy_version`、候选过滤原因、每个候选当时的指标快照、最终选择、限额桶版本和熔断代数。事故复盘必须能回答“当时为什么选 B”，而不是只能看到一个 `provider=B`。

### 28.3.2 EWMA 的正确用法

对样本 `x_t`，

```text
EWMA_t = α * x_t + (1 - α) * EWMA_(t-1)
```

延迟可用截断后的 `log1p(latency_ms)` 或按 p50/p95 的滑动摘要；成功率要定义分母。推荐把结果分成：

- `success_business`: 渠道明确成功 / 渠道明确终态响应；
- `decline_business`: 卡拒、余额不足、风控拒绝等可预期业务拒绝；
- `transport_unknown`: 超时、连接断开、5xx、解析失败；
- `callback_lag`: 成功到回调的延迟；
- `query_convergence`: UNKNOWN 在查单窗口内收敛的比例。

网络错误不能简单算为支付失败，否则路由器会奖励一个“快速拒绝”的渠道或惩罚一个“已成功但响应丢失”的渠道。可用一个带先验的成功率估计减少冷启动偏差：

```text
posterior_success = (success + prior_success * k)
                    / (eligible + k)
confidence = eligible / (eligible + k)
```

当样本不足时保留小比例受控探索，但探索不能违反限额、风险策略和 `UNKNOWN` 不重试规则；探索的单位应是**新的支付意图**，不是对同一个未知支付再发一次。

### 28.3.3 限额是多维资源，不是一个计数器

至少需要以下维度的额度桶：

```text
(provider, legal_entity, merchant, currency, payment_method, risk_tier, window)
```

每个桶同时控制：笔数、金额、并发在途、日累计、退款金额、查单 QPS 和回调重放处理量。额度消耗要区分：

- `RESERVED`：已为支付意图预留，但尚未向渠道发送；
- `IN_FLIGHT`：已发送且未得明确终态；
- `COMMITTED`：渠道成功并完成内部事实；
- `RELEASED`：明确未发送或明确失败，才可释放；
- `UNKNOWN_HOLD`：可能已产生外部影响，不能自动释放为可用额度，直到查单/对账闭合。

在同一事务中创建支付尝试和额度预留，使用 `SELECT ... FOR UPDATE` 或原子条件更新；额度不足返回 `429`/业务不可用，不应换一个无限额度的渠道绕过租户级风控。

### 28.3.4 熔断器必须和 UNKNOWN 分开

熔断器状态可以是：

```text
CLOSED
  ├─ 连续失败/错误率超阈值 ─► OPEN
  └─ 人工禁用 ──────────────► DISABLED

OPEN
  └─ 冷却期到 + 半开探针 ───► HALF_OPEN

HALF_OPEN
  ├─ 探针明确成功/健康 ─────► CLOSED
  └─ 探针失败/未知过多 ─────► OPEN
```

熔断统计只使用安全分类：

| 结果 | 是否计入失败 | 是否释放额度 | 是否允许同意图换渠道 |
|---|---:|---:|---:|
| 明确业务拒绝 | 按策略 | 是 | 仅在业务规则允许，通常可选 |
| 连接前失败，证明未发送 | 是 | 是 | 可以 |
| 已发送后超时 | 不作为明确业务失败 | 否，转 UNKNOWN_HOLD | 不可以自动换渠道 |
| 渠道明确成功 | 否 | 转 COMMITTED | 不可以再次扣款 |
| 签名/协议错误 | 是且高优先级告警 | 依副作用证据 | 先隔离，不盲切 |

批量熔断时要限制切流比例、租户和币种，保留探针容量，避免 B 同时承接 A 的全部流量。恢复时先验证查单和回调链路，不仅验证“能返回 HTTP 200”。

### 28.3.5 Go 1.24 路由核心代码

```go
package routing

import (
	"context"
	"errors"
	"math"
	"time"
)

type Outcome uint8

const (
	OutcomeSuccess Outcome = iota
	OutcomeDecline
	OutcomeUnknown
	OutcomeNotSent
)

type Candidate struct {
	Provider       string
	SuccessEWMA    float64
	LatencyEWMA    float64
	CostBps        float64
	RiskPenalty    float64
	Confidence     float64
	Circuit        string // CLOSED | HALF_OPEN
	RemainingMinor int64
	MaxInFlight    int
	InFlight       int
}

type Decision struct {
	Provider       string
	PolicyVersion  string
	Score          float64
	Reason         string
	MetricSnapshot map[string]float64
}

func choose(c []Candidate, amount int64, policy string) (Decision, error) {
	if amount <= 0 {
		return Decision{}, errors.New("amount must be positive")
	}
	best := Decision{}
	found := false
	for _, x := range c {
		if x.Circuit != "CLOSED" && x.Circuit != "HALF_OPEN" {
			continue
		}
		if x.RemainingMinor < amount || x.InFlight >= x.MaxInFlight {
			continue
		}
		// 冷启动时 score 不应被一个异常小样本完全主导。
		confidence := math.Max(0, math.Min(1, x.Confidence))
		score := 0.65*x.SuccessEWMA*confidence +
			0.65*(1-confidence)*0.5 -
			0.20*x.LatencyEWMA/1000.0 -
			0.05*x.CostBps - 0.10*x.RiskPenalty
		if !found || score > best.Score {
			best = Decision{
				Provider: x.Provider, PolicyVersion: policy, Score: score,
				Reason: "eligible+ewma+limit+circuit",
				MetricSnapshot: map[string]float64{
					"success_ewma": x.SuccessEWMA, "latency_ewma": x.LatencyEWMA,
					"confidence": confidence, "cost_bps": x.CostBps,
				},
			}
			found = true
		}
	}
	if !found {
		return Decision{}, errors.New("no eligible provider")
	}
	return best, nil
}

// 只有证明 NOT_SENT 才允许安全进入另一个 provider。
func canTryAnother(o Outcome) bool { return o == OutcomeNotSent || o == OutcomeDecline }

func boundedEWMA(old, sample, alpha float64) float64 {
	if alpha <= 0 || alpha > 1 || math.IsNaN(sample) || math.IsInf(sample, 0) {
		return old
	}
	return alpha*sample + (1-alpha)*old
}

func observe(ctx context.Context, timeout time.Duration, submit func(context.Context) Outcome) Outcome {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	return submit(ctx) // 调用方必须把 SENT/UNKNOWN 持久化，不能在这里重试
}
```

这段代码只负责候选排序，不负责经济事实。真实系统要在数据库事务中保存 `Decision`，并让额度预留、attempt 和 Outbox 一起提交；`observe` 的调用方收到 `OutcomeUnknown` 后只能驱动查单状态机。

---

## 28.4 支付状态机：UNKNOWN 是一等业务状态

### 28.4.1 意图状态与尝试状态分离

支付意图推荐状态：

```text
CREATED
  └─ ACCEPT ─────────► ACCEPTED
                         └─ DISPATCH ─► PROCESSING

PROCESSING
  ├─ PROVIDER_SUCCESS ─► SUCCEEDED
  ├─ PROVIDER_DECLINED ─► FAILED
  ├─ NOT_SENT ──────────► RETRYABLE
  └─ DEADLINE ──────────► UNKNOWN

RETRYABLE
  ├─ ROUTE_RETRY ───────► PROCESSING
  └─ EXPIRE ────────────► FAILED

UNKNOWN
  ├─ CALLBACK_SUCCESS / QUERY_SUCCESS ─► SUCCEEDED
  ├─ CALLBACK_DECLINED / QUERY_FAILED ─► FAILED
  ├─ QUERY_NOT_FOUND（窗口内）─────────► UNKNOWN
  ├─ UNKNOWN 超过 SLA ─────────────────► MANUAL_REVIEW
  └─ 明确一单多付 ─────────────────────► SUCCEEDED + DUPLICATE_REFUND_PENDING

SUCCEEDED
  ├─ REFUND ───────────► REFUNDING ─► PARTIALLY_REFUNDED / REFUNDED
  └─ CHARGEBACK ───────► CHARGEBACK_OPEN ─► CHARGEBACK_LOST/WON
```

单个渠道尝试状态：

```text
CREATED -> RESERVED -> SENT ->
  RESPONSE_SUCCESS | RESPONSE_DECLINED | UNKNOWN
UNKNOWN -> QUERYING -> CONFIRMED_SUCCESS | CONFIRMED_DECLINED | STILL_UNKNOWN
```

关键不变量：

1. `UNKNOWN` 不是失败，也不是可无限期停留的垃圾桶；它必须有 `unknown_since`、查单计划、最大自动窗口、升级队列和额度冻结策略。
2. 一个 `operation_id` 至多允许一个 `CAPTURE` 成功经济事实；渠道尝试可以多条，但 `capture_fact(operation_id)` 必须唯一。
3. 状态转移使用 `WHERE state = old_state AND version = old_version`；成功影响行数为 0 就重读，不能盲目覆盖。
4. 只有明确证明“未发送”才能从 `PROCESSING/UNKNOWN` 转为 `RETRYABLE`；`UNKNOWN` 中的“查不到”在渠道索引延迟窗口内仍是未知。
5. 成功、失败、退款和拒付都由追加事件/事实证明；快照只用于查询和调度。
6. 终态不等于不可发生后续资金动作：`SUCCEEDED` 可产生退款，`REFUNDED` 后仍可出现拒付或渠道更正，必须以新操作表示。

### 28.4.2 超时分类：发送边界比 HTTP 错误更重要

| 失败点 | 本地证据 | 状态 | 自动动作 |
|---|---|---|---|
| 组装请求前超时 | 无 attempt/send 记录 | `NOT_SENT` | 可重试/换渠道，仍用同一 operation |
| 取得 lease 前崩溃 | lease 未提交 | `NOT_SENT` | 恢复 Outbox |
| lease 提交后、发送前崩溃 | `RESERVED`，无 send proof | `UNKNOWN_SEND_BOUNDARY` | 适配器协议判定；不能仅靠进程日志 |
| 写 socket 后读超时 | `SENT` | `UNKNOWN` | 原渠道查单，不换渠道扣款 |
| 收到 HTTP 200 但 body 损坏 | `SENT` | `UNKNOWN` | 原渠道查单，隔离原响应 |
| 明确渠道拒绝 | 签名和金额校验通过 | `FAILED` | 按业务规则释放额度 |
| 回调签名错误 | 原始 payload 已保存 | `QUARANTINED` | 不驱动状态，告警与人工审计 |

“写 socket 后是否到达渠道”通常无法由客户端证明。因此适配器最好使用渠道幂等提交协议；若渠道没有幂等键和查单能力，只能把 `SENT` 的自动化边界设为人工，不应宣称 exactly-once。

### 28.4.3 查单计划

查单要以原 `channel_key`、原渠道和原金额/币种为准，不因路由策略更新而改查另一个渠道。示例计划：

```text
0s      接到 UNKNOWN，立即写入 query_due
2s      第一次查单
5s      第二次查单
15s     第三次查单
30s     第四次查单
1m/5m   指数退避但受渠道 QPS 和窗口约束
15m     进入低频巡检，同时保留 UNKNOWN_HOLD
T+1     渠道文件/平台对账强制核对
超过 SLA  进入人工队列，禁止自动支付重试
```

查单结果 `NOT_FOUND` 的含义必须由渠道协议定义：可能是未发送、索引延迟、查询键错误或最终失败。不能把一次 `NOT_FOUND` 当作“安全重付”。只有达到渠道承诺的可见性窗口、查单最终性规则和对账边界，才可以把它作为明确未发生副作用的证据。

### 28.4.4 回调与查单竞争仲裁

同步响应、回调、主动查单和文件都写入 `payment_observation`，不直接改 `payment_order`。仲裁步骤：

1. 验证来源身份、签名、商户/账户作用域、金额、币种、外部引用和事件版本。
2. 以 `(provider, provider_ref, provider_event_id/version)` 去重；同一事件不同 payload hash 进入冲突队列。
3. 在事务内锁定 `payment_operation` 和相关 attempt，读取已接受的观察集合。
4. 先检查是否已有不可逆经济事实：已 `CAPTURED` 时，重复成功只归档，不再次入账；明确失败不得抹掉已确认成功。
5. 比较**渠道事件版本/最终性**，不比较平台接收时间。高版本或同版本相同 hash 幂等忽略；同版本不同语义需要人工升级。
6. 依据白名单驱动状态转移，并在同一事务写 capture/refund/chargeback 事实和 Outbox。
7. 提交后 ACK；事务失败则不 ACK，让消息重复投递由唯一键处理。

推荐证据优先级不是固定的“回调永远高于查单”，而是按渠道协议配置：

```text
有效签名 + provider_event_version + provider finality
    > 有效签名但 provisional 回调
    > 带原始渠道引用的查单结果
    > 同步响应
    > 无引用的聚合文件/人工输入
```

如果查单先返回 `SUCCESS`，随后回调返回同一 `provider_ref` 的 `DECLINED`，不能简单按到达顺序改状态。需检查渠道是否允许状态反转、回调事件版本和最终性；若确实发生更正，追加 `provider_correction` 和冲正/退款流程，而不是覆盖原成功事实。

#### 仲裁伪代码

```text
BEGIN
  INSERT observation ... ON CONFLICT (provider, event_id) DO NOTHING
  IF duplicate THEN COMMIT; ACK

  SELECT operation, attempts, accepted_observations
    FOR UPDATE

  verify amount/currency/merchant/ref and provider event ordering
  IF invalid THEN quarantine; COMMIT; ACK

  IF capture_fact exists:
      if observation means same success: archive as duplicate
      if observation is provider correction: create correction case
      else: never create second capture
  ELSE IF observation is final success:
      insert capture_fact(operation_id, provider_ref) -- UNIQUE
      transition UNKNOWN/PROCESSING -> SUCCEEDED
  ELSE IF observation is final decline AND no success evidence:
      transition -> FAILED
  ELSE:
      keep UNKNOWN and schedule next query

  INSERT outbox in same transaction
COMMIT
ACK only after commit
```

---

## 28.5 异常资金路径：一单多付、部分退款与拒付

### 28.5.1 一单多付的识别与退回

一单多付不是把支付订单状态改成 `FAILED`。对同一业务订单可能出现：

- 同一 `operation_id` 同一渠道重复 capture；
- 客户端生成多个 operation，但商户订单号相同；
- UNKNOWN 后错误换渠道，两个渠道各成功一次；
- 渠道回调重复导致平台重复记账，但外部实际只扣一次；
- 同一订单金额被拆成多笔授权/capture，业务是否允许必须由协议明确。

识别需要同时使用 `merchant_id + merchant_order_id + currency + payable_amount`、客户端幂等键、渠道引用、卡/账户 token 的受限指纹和时间窗口；不能只用订单号，因为订单号可能重用。找到多笔外部成功后，先把每笔原始事实全部保留并把订单标成 `DUPLICATE_REVIEW`：

```text
原始支付 P1 -> CAPTURED -> 客户应收/商户待结算
原始支付 P2 -> CAPTURED -> 客户应收/商户待结算
退回操作 R2 -> REFUNDING -> REFUNDED / REFUND_UNKNOWN
```

选择退回哪一笔要看渠道可退款性、手续费、结算状态、风控和客户权益；决策本身要保存规则版本和人工审批。退回必须：

1. 创建新的 `refund_operation_id`，`parent_operation_id=P2`；
2. 使用 `refund_key = duplicate:<merchant>:<order>:<capture_ref>` 幂等；
3. 原路退回优先，禁止未经授权把退款打到另一个账户；
4. 退款超时进入 `REFUND_UNKNOWN`，不能继续提交第二笔退款；
5. 退款成功后由渠道回调/查单/对账确认，追加退款分录，原 capture 不被修改；
6. 若渠道已结算不能退款，进入人工退回/银行转账流程，另做收款人校验和反洗钱控制。

平台内部若因重复回调产生了第二条 capture，必须用账本冲正或重建投影修复，不能仅把外部重复支付退回后假设内部已平。

### 28.5.2 部分退款的原子额度

定义：

```text
captured_amount = 所有确认 capture 的金额之和
refunded_amount = 所有确认退款的金额之和
reserved_refund = 已提交但未终态的退款额度
available_refund = captured_amount - refunded_amount - reserved_refund
```

同一 capture 并发两笔退款时，必须锁定 `payment_operation` 或使用可证明的原子条件更新，保证：

```text
requested_amount > 0
requested_amount <= available_refund
currency == capture.currency
refund_deadline 未过期
```

`REFUND_UNKNOWN` 必须占用 `reserved_refund`，直到查单/对账明确失败或成功；否则两个客户端都看到“可退 100”，最终渠道退 150。部分退款要按 `refund_operation_id` 分笔记录，允许 `30 + 70`，不允许把 `refunded_amount=100` 覆盖写成一行而失去两次审批、渠道引用和时间。

### 28.5.3 拒付（Chargeback）与退款不同

退款是平台或商户发起的主动资金动作；拒付是发卡行/卡组织/渠道发起的争议流程。拒付可能在退款后到达，也可能在 capture 后很久才到达。建议独立建模：

```text
CAPTURED
  ├─ REFUND_REQUESTED -> REFUNDED
  └─ CHARGEBACK_OPEN -> REPRESENTMENT_PENDING
                         ├─ EVIDENCE_SUBMITTED -> WON
                         └─ DEADLINE/LOST -> LOST
```

红线：

- 拒付入站事件重复不应重复扣商户；`chargeback_case(provider_case_id)` 唯一。
- `CHARGEBACK_OPEN` 不是已输；但可按渠道规则冻结商户可结算余额或建立应收风险。
- 拒付金额、手续费、汇率和争议费要分开建模，不能把净额塞进原 capture。
- 退款与拒付竞态要依据卡组织规则和渠道状态处理；必要时先冻结、人工判断，不能一边代表商户申诉一边自动退款而无证据。
- 申诉证据文件、截止时间、版本、提交回执和结果都进入不可变证据表。

---

## 28.6 PostgreSQL DDL 与支付协议

### 28.6.1 核心枚举与支付意图

金额在业务层使用最小单位整数，数据库用 `NUMERIC(38,0)` 保护超大聚合；禁止用浮点。实际生产可把状态从 enum 替换为受控字典表，以便无锁发布新状态，但示例使用 enum 表达约束：

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE payment_state AS ENUM (
  'CREATED', 'ACCEPTED', 'PROCESSING', 'RETRYABLE', 'UNKNOWN',
  'SUCCEEDED', 'FAILED', 'MANUAL_REVIEW', 'DUPLICATE_REVIEW',
  'REFUNDING', 'PARTIALLY_REFUNDED', 'REFUNDED', 'CHARGEBACK_OPEN'
);

CREATE TYPE attempt_state AS ENUM (
  'CREATED', 'RESERVED', 'SENT', 'SUCCESS', 'DECLINED',
  'NOT_SENT', 'UNKNOWN', 'QUERYING', 'QUARANTINED'
);

CREATE TYPE observation_kind AS ENUM ('SYNC', 'CALLBACK', 'QUERY', 'FILE', 'MANUAL');
CREATE TYPE observation_result AS ENUM ('SUCCESS', 'DECLINED', 'NOT_FOUND', 'PENDING', 'CORRECTED');

CREATE TABLE payment_operation (
  operation_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id             TEXT NOT NULL,
  merchant_id           TEXT NOT NULL,
  merchant_order_id     TEXT NOT NULL,
  action                TEXT NOT NULL CHECK (action IN ('CAPTURE', 'REFUND')),
  parent_operation_id   UUID REFERENCES payment_operation(operation_id),
  currency              CHAR(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  amount_minor          NUMERIC(38,0) NOT NULL CHECK (amount_minor > 0),
  request_hash          BYTEA NOT NULL CHECK (octet_length(request_hash) = 32),
  idempotency_key       TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 192),
  state                 payment_state NOT NULL,
  state_version         BIGINT NOT NULL DEFAULT 0 CHECK (state_version >= 0),
  unknown_since         TIMESTAMPTZ,
  query_due_at          TIMESTAMPTZ,
  policy_version        TEXT NOT NULL,
  route_snapshot        JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (tenant_id, action, idempotency_key),
  CHECK (state <> 'UNKNOWN' OR unknown_since IS NOT NULL)
);

CREATE INDEX payment_query_due_idx ON payment_operation (query_due_at, operation_id)
  WHERE state = 'UNKNOWN';
CREATE INDEX payment_merchant_order_idx
  ON payment_operation (merchant_id, merchant_order_id, currency, created_at);
```

上面的 `CHECK` 仅做基本保护；状态与 `unknown_since` 的一致性仍应在状态转移事务中保证。`merchant_order_id` 不设全局唯一，因为商户可能有多次合法操作；一单多付由业务关联和规则识别。

### 28.6.2 尝试、观察和事实表

```sql
CREATE TABLE payment_attempt (
  attempt_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  operation_id          UUID NOT NULL REFERENCES payment_operation(operation_id),
  provider              TEXT NOT NULL,
  channel_key           TEXT NOT NULL,
  provider_ref          TEXT,
  state                 attempt_state NOT NULL,
  fencing_epoch         BIGINT NOT NULL,
  route_policy_version  TEXT NOT NULL,
  limit_reservation_id  UUID,
  sent_at               TIMESTAMPTZ,
  final_at              TIMESTAMPTZ,
  last_error_code       TEXT,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (provider, channel_key),
  UNIQUE (operation_id, provider)
);
CREATE INDEX payment_attempt_unknown_idx ON payment_attempt (provider, state)
  WHERE state IN ('UNKNOWN', 'QUERYING');

CREATE TABLE payment_observation (
  observation_id          BIGSERIAL PRIMARY KEY,
  attempt_id              UUID NOT NULL REFERENCES payment_attempt(attempt_id),
  provider                TEXT NOT NULL,
  provider_event_id       TEXT NOT NULL,
  provider_event_version  TEXT,
  kind                    observation_kind NOT NULL,
  result                  observation_result NOT NULL,
  provider_ref            TEXT,
  currency                CHAR(3) NOT NULL,
  amount_minor            NUMERIC(38,0) NOT NULL CHECK (amount_minor > 0),
  event_time               TIMESTAMPTZ,
  observed_at              TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  payload_sha256           BYTEA NOT NULL CHECK (octet_length(payload_sha256) = 32),
  signature_key_version    TEXT,
  raw_payload              JSONB NOT NULL,
  validation_status        TEXT NOT NULL CHECK (validation_status IN ('VALID', 'QUARANTINED')),
  UNIQUE (provider, provider_event_id),
  UNIQUE (provider, provider_event_id, payload_sha256)
);
CREATE INDEX payment_observation_attempt_idx
  ON payment_observation (attempt_id, event_time, observation_id);

CREATE TABLE payment_capture_fact (
  capture_fact_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  operation_id          UUID NOT NULL REFERENCES payment_operation(operation_id),
  attempt_id            UUID NOT NULL REFERENCES payment_attempt(attempt_id),
  provider              TEXT NOT NULL,
  provider_ref          TEXT NOT NULL,
  currency              CHAR(3) NOT NULL,
  amount_minor          NUMERIC(38,0) NOT NULL CHECK (amount_minor > 0),
  evidence_observation  BIGINT NOT NULL REFERENCES payment_observation(observation_id),
  created_at            TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (operation_id),
  UNIQUE (provider, provider_ref)
);

CREATE TABLE payment_refund_fact (
  refund_fact_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  refund_operation_id   UUID NOT NULL REFERENCES payment_operation(operation_id),
  capture_fact_id       UUID NOT NULL REFERENCES payment_capture_fact(capture_fact_id),
  provider_ref          TEXT,
  amount_minor          NUMERIC(38,0) NOT NULL CHECK (amount_minor > 0),
  currency              CHAR(3) NOT NULL,
  state                 TEXT NOT NULL CHECK (state IN ('REQUESTED','UNKNOWN','SUCCEEDED','FAILED')),
  evidence_observation  BIGINT REFERENCES payment_observation(observation_id),
  created_at            TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (refund_operation_id)
);

CREATE TABLE chargeback_case (
  case_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  capture_fact_id       UUID NOT NULL REFERENCES payment_capture_fact(capture_fact_id),
  provider              TEXT NOT NULL,
  provider_case_id      TEXT NOT NULL,
  disputed_minor        NUMERIC(38,0) NOT NULL CHECK (disputed_minor > 0),
  currency              CHAR(3) NOT NULL,
  state                 TEXT NOT NULL,
  response_due_at       TIMESTAMPTZ,
  raw_evidence          JSONB NOT NULL,
  UNIQUE (provider, provider_case_id)
);
```

### 28.6.3 Outbox、租约、额度和状态日志

```sql
CREATE TABLE payment_outbox (
  outbox_id       BIGSERIAL PRIMARY KEY,
  aggregate_id    UUID NOT NULL,
  aggregate_type  TEXT NOT NULL,
  event_type      TEXT NOT NULL,
  event_version   INTEGER NOT NULL,
  sequence_no     BIGINT NOT NULL,
  idempotency_key TEXT NOT NULL,
  payload         JSONB NOT NULL,
  status          TEXT NOT NULL CHECK (status IN ('READY','CLAIMED','SENT','DEAD')),
  lease_token     UUID,
  lease_until     TIMESTAMPTZ,
  attempts        INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  sent_at         TIMESTAMPTZ,
  UNIQUE (aggregate_id, sequence_no),
  UNIQUE (idempotency_key)
);
CREATE INDEX payment_outbox_ready_idx ON payment_outbox (next_attempt_at, outbox_id)
  WHERE status IN ('READY', 'CLAIMED');

CREATE TABLE payment_state_transition (
  transition_id   BIGSERIAL PRIMARY KEY,
  operation_id    UUID NOT NULL REFERENCES payment_operation(operation_id),
  from_state      payment_state,
  to_state        payment_state NOT NULL,
  old_version     BIGINT NOT NULL,
  command_type    TEXT NOT NULL,
  evidence_id     BIGINT,
  reason          TEXT NOT NULL,
  actor           TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (operation_id, old_version)
);

CREATE TABLE route_limit_reservation (
  reservation_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  provider        TEXT NOT NULL,
  bucket_key      TEXT NOT NULL,
  operation_id    UUID NOT NULL REFERENCES payment_operation(operation_id),
  amount_minor    NUMERIC(38,0) NOT NULL CHECK (amount_minor > 0),
  state           TEXT NOT NULL CHECK (state IN ('RESERVED','COMMITTED','RELEASED','UNKNOWN_HOLD')),
  expires_at      TIMESTAMPTZ NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (provider, operation_id)
);
```

`payment_capture_fact` 的唯一键是内部经济事实边界；`payment_observation` 的唯一键是外部证据边界；`payment_outbox` 的唯一键是消息事实边界。三者不能互相替代。

### 28.6.4 HTTP/Webhook 协议

创建支付：

```http
POST /v1/payments
Idempotency-Key: merchant-42:order-991:pay:v1
Content-Type: application/json

{"merchant_id":"m42","merchant_order_id":"order-991",
 "currency":"CNY","amount_minor":100000,"payment_method":"CARD"}
```

响应语义：

| HTTP | 语义 | 客户端动作 |
|---:|---|---|
| `201` | 首次创建，未必已完成 | 保存 `operation_id`，查询同一资源 |
| `200` | 同键已有确定结果 | 不重复 POST |
| `202` | `PROCESSING` 或 `UNKNOWN` | 按 `query_url` 查询，不换键 |
| `409` | 同键请求指纹不同/业务冲突 | 生成新业务意图前先人工确认，不能覆盖旧键 |
| `429` | 额度/限流，未产生外部副作用的保证需看响应 | 相同键退避；若已进入 UNKNOWN 先查单 |
| `5xx`/网络超时 | 客户端不知道结果 | 使用同一键 GET/查单，绝不新建付款 |

Webhook 接收：

```http
POST /v1/webhooks/{provider}
X-Provider-Event-Id: evt_123
X-Provider-Event-Version: 17
X-Signature: v1=...
X-Provider-Timestamp: 2026-11-25T20:00:03.123Z

{"merchant_id":"m42","provider_ref":"ch_abc","status":"SUCCEEDED",
 "amount_minor":100000,"currency":"CNY","event_version":17}
```

接收端先落原始 payload 和 hash，再异步验证/仲裁；若同步校验通过，也必须在提交状态事务后才返回成功 ACK。ACK 只表示“平台已持久化并接受处理”，不表示商户支付已经成功。签名时间窗、重放 nonce、事件 ID、provider scope、金额币种必须校验；敏感卡数据不可写入普通 JSON 日志。

---

## 28.7 Go 1.24 与 Java 21：Outbox、仲裁和幂等核心代码

### 28.7.1 Go 1.24：UNKNOWN 事务仲裁

以下代码故意把锁、观察、唯一 capture 和 Outbox 放在一个数据库事务语义里；实际项目需要补充参数化 SQL、死锁重试和可观测字段：

```go
type Observation struct {
	ID             int64
	AttemptID      string
	Provider       string
	ProviderRef    string
	Version        string
	Result         string // SUCCESS | DECLINED | NOT_FOUND | PENDING
	Currency       string
	AmountMinor    int64
	ValidSignature bool
}

type Payment struct {
	ID      string
	State   string
	Version int64
}

func (s *Service) ApplyObservation(ctx context.Context, o Observation) error {
	return s.db.WithTx(ctx, func(tx *sql.Tx) error {
		inserted, err := insertObservation(tx, o) // UNIQUE(provider,event_id)
		if err != nil { return err }
		if !inserted { return nil } // 重放只 ACK，不产生经济事实
		if !o.ValidSignature || o.AmountMinor <= 0 || o.Currency == "" {
			return quarantine(tx, o.ID, "invalid observation")
		}

		p, err := lockPaymentByAttempt(tx, o.AttemptID)
		if err != nil { return err }
		if err := validateAgainstOperation(tx, p.ID, o); err != nil {
			return quarantine(tx, o.ID, err.Error())
		}

		if existsCapture, err := hasCapture(tx, p.ID); err != nil {
			return err
		} else if existsCapture {
			// 已有经济事实时，重复成功只归档；更正进入人工/冲正流程。
			if o.Result == "SUCCESS" { return appendAudit(tx, p.ID, o.ID, "duplicate_success") }
			return appendAudit(tx, p.ID, o.ID, "post_capture_observation")
		}

		current, err := currentProviderEvidence(tx, p.ID)
		if err != nil { return err }
		if !isNewerOrConsistent(current, o) {
			return appendAudit(tx, p.ID, o.ID, "stale_or_conflicting_observation")
		}

		switch o.Result {
		case "SUCCESS":
			if err := insertCaptureFact(tx, p.ID, o); err != nil { return err }
			if err := transition(tx, p, "SUCCEEDED", o.ID, "provider success"); err != nil { return err }
			return insertOutbox(tx, p.ID, "PaymentSucceeded", p.Version+1)
		case "DECLINED":
			if p.State == "SUCCEEDED" { return errors.New("illegal decline after success") }
			if err := transition(tx, p, "FAILED", o.ID, "provider decline"); err != nil { return err }
			return insertOutbox(tx, p.ID, "PaymentFailed", p.Version+1)
		case "NOT_FOUND", "PENDING":
			// NOT_FOUND 在渠道最终性边界前仍然是 UNKNOWN。
			return scheduleNextQuery(tx, p.ID, p.Version, time.Now().UTC().Add(5*time.Second))
		default:
			return errors.New("unknown provider result")
		}
	})
}
```

不可省略的实现细节：`lockPaymentByAttempt` 必须锁定支付意图和 attempt；`insertCaptureFact` 必须依赖 `UNIQUE(operation_id)`；`transition` 必须校验旧版本、白名单、金额和证据；`insertOutbox` 与事实同事务；事务失败不应 ACK webhook。

### 28.7.2 Go 1.24：Outbox publisher 的 lease/fencing

```go
type OutboxRow struct {
	ID          int64
	AggregateID string
	Key         string
	Payload     []byte
	LeaseToken  uuid.UUID
}

func (p *Publisher) Publish(ctx context.Context, row OutboxRow) error {
	// 外部 broker 只能至少一次；key 由消费者幂等，不能声称 exactly-once。
	if err := p.broker.Publish(ctx, row.Key, row.Payload); err != nil {
		return p.repo.ReleaseForRetry(ctx, row.ID, row.LeaseToken, backoff(row.ID))
	}
	if err := p.repo.MarkSent(ctx, row.ID, row.LeaseToken); err != nil {
		// broker 可能已经收到；下次重复投递由 key 去重。
		return err
	}
	return nil
}

func (p *Publisher) Claim(ctx context.Context, n int) ([]OutboxRow, error) {
	return p.repo.Tx(ctx, func(tx *sql.Tx) ([]OutboxRow, error) {
		return claimRows(tx, n, 30*time.Second) // FOR UPDATE SKIP LOCKED
	})
}
```

发布顺序不能依赖 `outbox_id` 的连续性：事务回滚会留下序号空洞，并发事务可能先提交较大的 ID。消费者按 `aggregate_id + sequence_no` 做版本检查，或接受乱序后由状态机白名单拒绝旧事件。租约过期只是回收机制，`lease_token/fencing_epoch` 才能阻止旧 worker 在恢复后覆盖新结果。

### 28.7.3 Java 21：渠道适配器与有界并发

```java
public record SubmitResult(
        Transport transport,
        Business business,
        String providerRef,
        String rawHash) {
    enum Transport { NOT_SENT, SENT, RESPONSE_RECEIVED }
    enum Business { ACCEPTED, SUCCESS, DECLINED, UNKNOWN }
}

public interface ProviderAdapter {
    String name();
    Capabilities capabilities();
    SubmitResult submit(PaymentCommand command); // channelKey 必须来自 operationId
    Observation query(QueryCommand command);
}

public final class GatewayDispatcher implements AutoCloseable {
    private final Semaphore permits;
    private final ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();
    private final ProviderAdapter adapter;

    public GatewayDispatcher(ProviderAdapter adapter, int maxInFlight) {
        if (maxInFlight <= 0) throw new IllegalArgumentException("maxInFlight");
        this.adapter = adapter;
        this.permits = new Semaphore(maxInFlight);
    }

    public CompletableFuture<SubmitResult> submit(PaymentCommand command) {
        return CompletableFuture.supplyAsync(() -> {
            boolean acquired = false;
            try {
                permits.acquire();
                acquired = true;
                // DB 中的 attempt/lease 必须先提交；这里不包在可自动重试的 DB 事务里。
                return adapter.submit(command);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new CompletionException(e);
            } finally {
                if (acquired) permits.release();
            }
        }, executor);
    }

    @Override public void close() { executor.close(); }
}
```

虚拟线程适合等待 I/O，但不改变渠道 QPS、数据库连接池或额度。每个 provider 仍需独立 semaphore、deadline、连接池、熔断和查单预算；禁止在 `catch (TimeoutException)` 中直接再次调用 `submit`。Java 金额使用 `long` 最小单位并先做溢出检查，跨币种聚合使用带明确 scale/rounding 的 `BigDecimal` 或 `BigInteger`，禁止 `double`。

### 28.7.4 Java 21：查单/回调统一命令

```java
public sealed interface EvidenceCommand
        permits CallbackCommand, QueryResultCommand, FileResultCommand {}

public record CallbackCommand(String provider, String eventId, String eventVersion,
                              String providerRef, String currency, long amountMinor,
                              String result, byte[] payloadHash) implements EvidenceCommand {}

public record QueryResultCommand(String provider, String queryKey, String providerRef,
                                 String result, String providerVersion) implements EvidenceCommand {}

public final class EvidenceArbiter {
    private final PaymentRepository repository;

    public void apply(EvidenceCommand command) {
        repository.inTransaction(tx -> {
            if (!tx.insertObservationIfAbsent(command)) return null;
            Payment p = tx.lockPaymentForEvidence(command);
            tx.validateScopeAmountCurrency(p, command);
            if (tx.captureExists(p.operationId())) {
                tx.appendAudit(p.operationId(), "observation_after_capture");
                return null;
            }
            if (!tx.isNewerOrConsistentEvidence(p.operationId(), command)) {
                tx.appendAudit(p.operationId(), "stale_or_conflicting_evidence");
                return null;
            }
            switch (resultOf(command)) {
                case SUCCESS -> {
                    tx.insertCaptureFact(p, command); // operation_id UNIQUE
                    tx.transition(p, "SUCCEEDED", "provider evidence");
                    tx.outbox(p.operationId(), "PaymentSucceeded");
                }
                case DECLINED -> {
                    tx.transition(p, "FAILED", "provider final decline");
                    tx.outbox(p.operationId(), "PaymentFailed");
                }
                default -> tx.scheduleQuery(p.operationId());
            }
            return null;
        });
    }
}
```

`repository.inTransaction` 不能把远程 provider 调用塞进去；事务只处理持久化证据、状态和 Outbox。远程查单由另一个 worker 调用，结果再次作为 `EvidenceCommand` 进入同一仲裁器。

---

## 28.8 衔接：把支付网关的未知态交给对账与清结算闭环

本节是本章与后续清结算、资金账本和风险章节的边界，不把支付网关变成“万能总账”。

### 28.8.1 网关交付给下游的最小事实包

每个支付/退款/拒付操作至少交付：

```text
operation_id / parent_operation_id
merchant_id / legal_entity
merchant_order_id / idempotency_key / request_hash
provider / attempt_id / channel_key / provider_ref
currency / amount_minor / fee_minor（若已确认）
最终状态与状态转移版本
证据 observation_id、payload_hash、event_version
route policy / limit bucket / circuit generation
created_at、event_time、observed_at、value_date
```

对账层把这些事实与渠道文件、银行入账和账本分录映射为 CRF；网关不能因为自己看到 `SUCCEEDED` 就宣布银行已清算。`SUCCEEDED` 表示支付渠道/平台捕获事实已确认，`SETTLED`、`AVAILABLE`、`PAID_OUT` 是不同阶段。

### 28.8.2 UNKNOWN 的对账生命周期

```text
网关 UNKNOWN
  → 查单观察（provisional）
  → 回调/文件/账本映射
  → 同一 relation_id 下的四方匹配
  → 明确成功/失败、重复支付或外部未知差异
  → 追加 capture/refund/冲正/挂账动作
  → 修复后重对账
```

对账发现“渠道成功、网关 UNKNOWN”时，可以驱动原 `operation_id` 的成功收敛；发现“网关成功、渠道文件缺失”时，先等待文件水位，不可直接退款；发现“两个渠道都成功”时，生成一笔或多笔退回操作，不能通过把支付状态改成失败来掩盖第二笔。

### 28.8.3 给 29 章/下一专题的接口

下一专题可以在本章事实边界上继续讨论清分、结算、分润和商户出款，但必须遵循：

1. 网关只负责支付生命周期和渠道证据，不直接修改可用余额。
2. 渠道手续费、汇率、拒付费和退款手续费必须作为独立事实进入账本/清算规则。
3. `UNKNOWN_HOLD`、拒付风险准备金和商户结算冻结要有清晰的资金状态，不能用订单状态代替。
4. 任何自动释放、自动退款、自动补记都必须绑定原始 `operation_id/relation_id` 和可重放 `repair_key`。

---

## 28.9 对账、混沌与压测：证明“不重、不错、可收敛”

### 28.9.1 对账分层

实时层每秒/分钟发现：

- `operation_id` 是否有多个 capture fact；
- 渠道成功而内部没有 capture；
- 内部成功而渠道没有可见证据；
- UNKNOWN 年龄、查单次数、额度冻结金额；
- 回调与查单事件版本冲突、签名失败和 provider_ref 重用。

截止层按渠道/商户/币种/价值日核对：

```text
渠道成功本金 - 内部 capture 本金
- 已确认退款
- 允许的手续费/汇率/跨日差异
= 未解释差异
```

不能只比较总额。至少使用 `relation_id/provider_ref/merchant_order_id/operation_id` 多级匹配，原始文件完整性和水位满足后才发布 `FINAL`。迟到回调或更正文件生成新报告版本，不覆盖旧证据。

自动修复白名单示例：

| 差异 | 自动动作 | 前置条件 |
|---|---|---|
| 回调丢失但查单最终成功 | 复用原 operation 追加 capture | provider_ref、金额币种、签名/文件证据完整 |
| 内部重复 capture、外部仅一笔 | 冲正内部重复分录 | 账本事实唯一识别，审批规则通过 |
| 两个渠道都成功 | 创建重复笔退款 | 每笔 provider_ref 可退款、额度/风险通过 |
| 渠道成功、内部失败但已结算 | 挂账/人工补记 | 法律实体、账户和收款归属明确 |
| UNKNOWN 超过窗口 | 人工队列 | 原始证据齐全，禁止盲重付 |

每个 repair 以 `(break_id, rule_version, action_type, target_ref)` 生成唯一 `repair_key`，修复服务、账本和回调消费者都要幂等。

### 28.9.2 性质测试与并发测试

必须测试：

1. 同一 webhook 投递 1、2、100 次，`payment_observation` 和 `capture_fact` 数量不变。
2. SUCCESS 回调、SUCCESS 查单、同步 SUCCESS 任意乱序，最终只产生一个 capture。
3. UNKNOWN 后先到 DECLINED，再到 SUCCESS；按照 provider 事件版本/最终性处理，不能按到达顺序错误终态。
4. `NOT_FOUND` 在可见性窗口内反复出现，不允许自动换渠道扣款。
5. UNKNOWN 查单和回调并发竞争，`FOR UPDATE`/唯一键只允许一个状态转移和一条经济事实。
6. 两个部分退款并发，已退 + 预留不超过 capture；退款超时不释放额度。
7. 一单两个渠道均成功，重复退回任务重放不产生第二笔退款。
8. 退款金额为 0、负数、超过 capture、币种不同、父操作不存在均拒绝。
9. `MaxInt64`、`NUMERIC(38,0)` 边界、跨币种精度、整数溢出和手续费负数规则。
10. 同一个 provider_ref 被两个 merchant/tenant 使用时必须按渠道作用域隔离；同一作用域复用必须进入冲突。
11. Outbox 在业务事务提交后、broker ACK 前崩溃，重发不重复消费；broker 已发而 `MarkSent` 失败也不重复记账。
12. lease worker GC pause 超过租约，旧 fencing epoch 不能提交新结果。

Go 使用 `testing/fuzz` 生成观察顺序、重复次数和状态组合；Java 使用 jqwik/QuickTheories 验证仲裁交换性、幂等性和额度守恒。测试断言不能只看“最终字符串”，还要检查 transition、observation、capture、outbox、audit 的数量和证据关联。

### 28.9.3 混沌场景

在沙箱渠道和可回放录制流中注入：

- DNS 解析失败、连接建立成功但响应体丢失、半响应、TLS 重连和代理自动重试；
- 渠道已扣款后杀死网关进程，恢复时 Outbox/lease/查询任务重复；
- 回调比同步响应早到、查单比回调早到、同版本不同 payload、旧版本迟到；
- provider_ref 重复、金额/币种篡改、签名 key 轮换和回调重放；
- Redis/指标系统不可用（路由不能把缓存健康度当资金事实）；
- PostgreSQL 主库切换、事务死锁、只读副本延迟、broker 分区和重复投递；
- A 熔断时 B 达到并发/日限额，观察是否背压而非无限切流；
- 日切、时区夏令时、渠道文件迟到/半文件/更正文件；
- 两个退款同时超时、随后一个成功一个失败；拒付与退款同一时间到达。

每个故障必须证明：原始请求和响应未丢、没有第二条经济事实、UNKNOWN 没被伪造为失败、查单最终可恢复、旧 worker 不能覆盖新状态、限额不会负数、告警能定位 `operation_id/attempt_id/provider_ref`。

### 28.9.4 压测模型与 SLO

至少准备四类流量：

| 数据集 | 特征 |
|---|---|
| 均匀成功 | 多渠道均匀、回调及时、正常路由 |
| 渠道雪崩 | A 延迟和超时突增，B 限额有限，触发半开探针 |
| UNKNOWN 风暴 | 发送后读超时、回调延迟 10 分钟、查单 QPS 受限 |
| 异常业务 | 一单多付、重复回调、部分退款、拒付文件迟到 |

记录并分 provider/tenant/currency/risk segment 观察：

- 支付 API p50/p95/p99；从接受到明确终态的时间；
- `unknown_rate`、UNKNOWN 年龄 p95/p99、查单收敛率和查单 QPS；
- 每渠道成功/业务拒绝/transport unknown/回调延迟；
- 路由决策延迟、限额锁等待、熔断切流比例、候选为空比例；
- capture/refund 唯一冲突、重复回调率、冲突观察数、对账差异金额；
- Outbox lag、lease 过期、broker 重投、数据库锁等待、连接池饱和；
- 一单多付检测延迟、自动退款成功率、退款误修率（目标为 0）。

容量估算必须包含 UNKNOWN 放大：

```text
渠道请求峰值 = 新支付峰值 + 安全重试（仅 NOT_SENT/明确拒绝）
             + 查单峰值 + 回调重放峰值
数据库写入 = operation + attempt + observation + transition
           + capture/refund + outbox + audit
内存上限 = 路由缓存 + 有界 worker × 单请求状态 + provider client buffer
```

目标值只能经过容量评审后确定；一个可讨论的初始预算是：路由决策 p99 < 20ms，回调持久化 p99 < 100ms，UNKNOWN 发现 < 1s，已知渠道成功在 60s 内完成查单收敛，超出窗口的未知态 100% 进入人工队列。宁可降低自动重试率，也不能用 SLO 换取重复扣款。

### 28.9.5 可观测性与告警

核心指标：

```text
payment_unknown_total{provider,tenant,currency,age_bucket}
payment_capture_duplicate_conflict_total{provider}
payment_observation_conflict_total{provider,kind}
payment_query_convergence_ratio{provider,window}
payment_route_decision_total{provider,policy,reason}
payment_circuit_state{provider,segment}
payment_limit_reserved_minor{provider,bucket,state}
payment_outbox_lag_seconds{event_type}
payment_reconciliation_break_minor{provider,currency,type}
```

告警分层：渠道健康（超时/连接/熔断）、资金风险（UNKNOWN 金额/重复 capture/未知外部成功）、系统容量（锁/连接池/Outbox lag）和数据质量（签名失败/事件版本冲突）。日志必须能以 `operation_id → attempt_id → channel_key/provider_ref → observation_id → capture_fact_id → ledger_entry_id` 串联，卡号等敏感数据只记录 token 指纹或脱敏摘要。

---

## 28.10 极端边界与处理决策

### 28.10.1 时间、版本和最终性

- 保存 UTC `event_time`、`observed_at` 和法律实体 `value_date`；不能用服务器收到时间排序渠道事件。
- 渠道事件版本可能是字符串、数字或仅有时间戳，必须由适配器转换成可比较的 provider-specific ordering；不能跨渠道比较版本。
- 相同版本不同 payload hash 不是普通重复，是证据冲突；进入隔离和人工队列。
- 渠道“成功”可能只是授权成功，capture、结算、可退款和可用余额是不同最终性层级。
- 查单结果缓存时间、渠道索引延迟、文件截止时间必须进入策略版本；一次 `NOT_FOUND` 不等于未发生。

### 28.10.2 金额、币种和费用

- 所有金额为最小单位整数，带 ISO currency；同样的 `100` 不能跨 USD、JPY、USDC 比较。
- 授权金额、capture 金额、手续费、退款金额、拒付费和汇兑差额独立保存；不能把净额反推成本金。
- 渠道返回负退款、超精度、科学计数法或超范围数值时隔离，不能“截断后继续”。
- 部分退款按原 capture 和渠道规则计算，不按商户订单当前显示余额计算。
- 费率/汇率版本与路由策略一起保存；后续重放不得使用今天的报价改写昨天事实。

### 28.10.3 渠道和身份

- `provider_ref` 只有在 provider、merchant account、环境、法律实体作用域内有意义；不能全局裸用。
- 生产、沙箱、不同商户账户必须隔离幂等命名空间；测试键不能撞生产键。
- provider_ref 重用、渠道迁移、商户换号时，保留旧映射和版本，不覆盖历史 attempt。
- 渠道不支持幂等、查单和可信回调时，自动支付重试能力必须降级为人工；“我们自己的数据库唯一键”无法约束外部银行。

### 28.10.4 数据库、消息和人工

- 不在数据库自动重试事务中调用渠道；事务回滚不代表外部请求回滚。
- 不用只读副本判断“是否已成功/是否可退款”，必须读到有版本证明的主库或一致性快照。
- 消息系统至少一次投递；consumer 以业务 key 幂等，不能依赖 offset 恰好提交。
- 人工台不能直接 `UPDATE payment_operation.state`、`UPDATE balance` 或删除 observation；只能提交带原因、证据 hash、金额上限、审批人和过期时间的结构化命令。
- 紧急止血允许暂停某 provider、冻结新支付或退款，但恢复前必须检查 UNKNOWN、额度、Outbox 和对账水位，不能只重启服务。

---

## 28.11 术语表：把“成功”和“已扣款”说准确

| 术语 | 含义 | 常见误解 |
|---|---|---|
| Payment operation | 一次稳定的支付/退款经济意图 | 不是一次 HTTP 请求 |
| Attempt | 该意图对某渠道的一次持久化尝试 | 不是新的支付意图 |
| `channel_key` | 渠道副作用幂等键 | 不能每次重试随机生成 |
| Provider reference | 渠道交易引用 | 不一定全局唯一，必须带作用域 |
| `NOT_SENT` | 有证据证明请求没有发送到副作用边界 | 不是所有网络错误 |
| `UNKNOWN` | 可能已发送/已产生副作用但结果不可知 | 不是失败，也不是成功 |
| UNKNOWN hold | UNKNOWN 占用的金额/额度保留 | 不能自动释放后重付 |
| Query | 使用原渠道键获取外部状态 | 不等于重新提交支付 |
| Observation | 同步响应、回调、查单或文件的原始证据 | 不是订单快照 |
| Evidence arbitration | 按作用域、签名、事件版本、最终性合并证据 | 不是按到达时间覆盖 |
| Capture fact | 内部接受的一次扣款经济事实 | 不是渠道 HTTP 200 |
| Partial refund | 针对已 capture 金额的一部分新操作 | 不是减少订单字段 |
| Chargeback | 外部争议/拒付流程 | 不是普通退款 |
| EWMA | 指数加权移动平均，用于指标平滑 | 不是无偏的长期真相 |
| Circuit breaker | 对渠道调用的健康保护 | 不等于支付状态 |
| Limit reservation | 为支付/退款预留的多维额度 | 不是简单 Redis 计数 |
| Fencing epoch | 防止过期 worker 提交结果的单调令牌 | 分布式锁本身不提供 fencing |
| Finality | 渠道状态达到可接受的不可逆/可结算等级 | “成功响应”不必然是 final |
| Duplicate payment | 同一业务意图/订单产生多笔外部成功 | 不能用改状态掩盖 |
| Repair key | 修复动作的业务幂等键 | 不是 trace ID |
| Suspense | 已有外部资金事实但暂时不能归属/闭合的挂账 | 不是永远不处理的垃圾桶 |
| Reconciliation | 多来源证据的身份、金额、状态和总额核对 | 不是只比较余额 |

---

## 28.12 面试答题卡：三分钟讲清生产级支付网关

### Q1：支付请求超时为什么不能直接切到第二个渠道？

**答**：超时只说明调用方没有拿到结果，不能证明请求没有发送或渠道没有扣款。先把 attempt 标成 `UNKNOWN`，用原 `channel_key` 查单、接收回调并和对账文件核对。只有持久化证据证明 `NOT_SENT`，或渠道明确拒绝且没有副作用，才允许新尝试；否则切渠道可能形成一单多付。

### Q2：路由器如何使用 EWMA？

**答**：先用能力、币种、地区、风险、额度和熔断做硬过滤，再在同一 segment 内用成功 EWMA、延迟、成本、风险和探索项打分。成功、业务拒绝、transport unknown、回调延迟分开统计，不能把所有非 2xx 当失败。保存策略版本和指标快照，能解释每次选择。

### Q3：回调和查单同时返回不同结果，谁优先？

**答**：没有脱离渠道协议的固定优先级。两者先落 observation，验证签名、作用域、金额、provider_ref 和事件版本，再在事务中锁定 operation。按渠道定义的事件序号和最终性仲裁，不按平台到达时间；已有 capture 时重复成功不再入账，反转必须作为 provider correction 和新的冲正/退款流程。

### Q4：如何保证一笔支付只入账一次？

**答**：`operation_id` 与请求指纹永久绑定，attempt 和 observation 都有作用域唯一键；最终 capture fact 以 `UNIQUE(operation_id)` 保护。状态转移、capture fact、Outbox 在同一 PostgreSQL 事务提交，消费者至少一次但业务幂等，回调、查单、文件全部走同一个仲裁器。

### Q5：部分退款如何避免并发超退？

**答**：退款是新 operation，锁定原 capture 或用原子条件更新；可退额度等于 capture 减已成功退款减所有 UNKNOWN/处理中预留。退款超时仍占用额度，直到查单/对账明确结果。每笔退款有自己的幂等键、渠道引用和账本事实，不覆盖原支付。

### Q6：拒付和退款有什么区别？

**答**：退款是平台/商户主动发起的原路退回；拒付是发卡行/卡组织发起的争议，带证据提交和截止时间，可能在退款后很久到达。拒付要独立 case、风险冻结、申诉和手续费事实，不能把它当成普通退款或把 capture 改成失败。

### Q7：熔断后能不能把流量全部切到 B？

**答**：不能。B 有自己的额度、并发、币种能力和未知态容量，全部切流会造成二次雪崩。应按租户/币种/风险做受控转移，保留探针和预算；已进入 UNKNOWN 的原支付不能因为熔断而在 B 再扣一次。

### Q8：Outbox 能保证渠道调用 exactly-once 吗？

**答**：不能。Outbox 保证数据库事实和“应发布的命令”同事务；发布到 broker/渠道仍可能至少一次。必须使用 channel_key、attempt、lease/fencing、provider 查单和内部唯一 capture，把不可避免的重复请求变成幂等重放，而不是宣称网络 exactly-once。

### Q9：`SUCCEEDED` 是否等于钱已经可给商户？

**答**：不等于。它通常表示渠道 capture 和平台 capture fact 已确认；结算、手续费、拒付风险、银行入账、可用余额和商户出款是后续阶段。对账要在水位、文件完整性、价值日和最终性满足后才能发布清结算结果。

---

## 28.13 自检题：提交设计或代码前先回答

1. 客户端超时后，如何证明它应该 GET 原 operation，而不是 POST 新键？
2. 系统如何区分“连接前失败”和“写 socket 后读超时”？证据落在哪里？
3. 一个 operation 允许几个 attempt？什么条件下第二个 attempt 合法？
4. provider 不支持幂等键或查单时，自动化边界是什么？是否仍允许支付重试？
5. EWMA 的分母是什么？业务拒绝、transport unknown 和回调延迟是否被混为成功率？
6. 冷启动渠道的先验、探索比例和最大风险暴露是多少？谁批准？
7. 渠道限额按哪些维度分桶？UNKNOWN 的金额在何时释放？
8. 熔断统计是否把一单多付风险和普通连接错误区分开？半开探针会不会制造真实扣款？
9. 回调和查单同一时刻竞争时，数据库锁住哪些行？事件版本如何比较？
10. 同一版本不同 payload hash 怎么办？是否会被普通幂等逻辑吞掉？
11. 已经有 capture fact 后，迟到的成功回调会走哪条代码路径？是否可能再次写账？
12. `NOT_FOUND` 需要满足什么渠道最终性和水位条件才可以作为失败证据？
13. 一单两渠道成功，如何选择退哪一笔？退款本身超时怎么办？
14. 两个部分退款并发时，`captured - refunded - reserved` 在哪个锁/事务中计算？
15. 拒付在退款后到达时，商户余额、争议案和账本分录分别如何表示？
16. Outbox 事务提交后 publisher 崩溃，broker 重投是否会产生第二次 capture？为什么？
17. 旧 worker 租约过期后恢复，哪一个 fencing 条件阻止它提交观察或状态？
18. 日切、迟到文件、渠道更正和对账报告版本如何避免覆盖历史？
19. 人工操作台是否能直接改状态或余额？如果不能，结构化动作包含哪些字段？
20. 你能否从 `operation_id` 追到 request hash、route snapshot、attempt、raw observation、capture、ledger 和 refund？

### 交付前红线清单

- [ ] 同一业务键绑定请求 hash，参数变化硬拒绝。
- [ ] 每个渠道尝试有 provider、channel_key、fencing epoch 和发送边界。
- [ ] 超时/读失败进入 UNKNOWN，查单使用原渠道原键，禁止盲切渠道。
- [ ] 回调、同步、查单、文件统一落 observation，按版本/最终性仲裁。
- [ ] `capture_fact(operation_id)` 唯一，状态、事实、Outbox 同事务。
- [ ] 部分退款有原子未退额度，UNKNOWN 退款占用预留额度。
- [ ] 一单多付、拒付、渠道更正各自有独立事实和补偿路径。
- [ ] EWMA、限额、熔断按 segment 可解释且保存 policy snapshot。
- [ ] 对账和修复以 relation/repair key 幂等，原始证据不可变。
- [ ] 混沌测试覆盖响应丢失、回调乱序、租约过期、broker 重投和额度边界。
- [ ] 监控能区分系统故障、渠道拒绝、UNKNOWN 金额风险和真实对账差异。
