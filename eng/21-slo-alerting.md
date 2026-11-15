# 21. SLO、错误预算与告警降噪

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 流量与韧性：不崩是怎么做到的 ｜ **编号**：21 / 52
> **本章定位**：SLO 不是一块看板，它是**给「发布」装的一个刹车片**——预算还有，就放心发；预算烧完，只准修不准发。告警也不是越多越安全，本章给你一套从 SLO 反推阈值、再靠五法把告警压到「一晚上三条、每条都要动手」的工程做法。
> **读法**：§21.2 的三个算式必须能默写（预算是分钟数，燃尽率是倍数）；§21.4 第四步的降噪五法是值班体验的全部；§21.5 只记一句：Agent 侧的 SLO 是**三维**的，成本和成功率各有一个预算。

---

## 21.1 引子：一夜 1,284 条告警，漏了一个 P1

2026-01-08，某在线教育公司的支付回调服务。

22:00 起，MySQL 主库所在物理机的磁盘 IO 抖动（后来查出是备份任务与 binlog 归档撞在一起）。这一个根因，经由 5 条链路放大成了 5 类告警：`DBSlowQuery`、`OrderApiLatencyHigh`、`PayCallbackErrorRate`、`ThreadPoolSaturated`、`DownstreamTimeout`。每一类每 60 秒重复一次，五个半小时里同一个根因发了 **1,196 条**通知，占当晚总量的 93%。

- 22:00—06:00，所有告警进一个 2,400 人的大群，共 **1,284 条**；
- **02:40**，值班同学把这个群设为「消息免打扰」（这个动作没写进任何日志）；
- **03:12**，支付回调失败率从 0.4% 升到 **34%**，告警如约发进了那个静音群；
- **03:59**，早班客服在另一个群里问「为什么有人付了两次钱」，才发现；
- 影响 **47 分钟**、**8,600 笔**支付，其中 **2,300 笔**重复支付需退款，直接赔付 **6.2 万元**，客诉 **1,240 单**。

**事故二（2025-09 至 11，同一家公司，支付渠道切换）**——这一起没有任何「告警没被看到」的情节，它是纯粹的**账算错了**：

- **09-02 10:00** 新支付渠道 B 上线。渠道 B 的风控会对约 0.4% 的交易返回 `PAY_CHANNEL_REJECT`，接口层把它映射成 **HTTP 200 + 业务错误码**。
- **09-02 ~ 11-07** 共发布 **27 次**（每周 3 次）。看板上的季度可用性一路是 **99.94%**，比 99.9% 的 SLO「超额完成」，因此没有人提议减速。
- **10-18 20:14** v2.27 发布，为了「减少锁等待」，把库存锁 TTL 从 30s 改成 **3s**（评审记录写着「影响可忽略」）。
- **10-18 20:31** 超卖开始出现：订单创建后 3 秒锁即失效，同一 SKU 的并发扣减穿透。
- **10-18 21:02** 客服上报「同一个尺码卖了 40 件只剩 3 件」，21:19 回滚。超卖 **1,240 件**，赔付与折价 **38 万元**。
- **11-10 季度复盘**：手算后真实可用性是 **99.62%**，30 天窗口 43.2 分钟的预算实际烧掉 **204 分钟**（4.7 倍）。按正确口径，预算在 **10-21** 就已经是负数——也就是说，10-18 那次发布**本不该发生**。

那个 99.94% 有三个口径错误：把限流返回的 429 算成失败（冤枉了自己）、把 `PAY_CHANNEL_REJECT` 这类业务拒绝也算成失败（分母被污染，方向相反）、延迟用平均值（见第 20 章）。三个错误互相抵消，最后得到一个「看起来很健康」的数。

> 🩸 **两个事故的共性**：**告警没人看，是因为看不过来；发布没停，是因为账算错了。** 前者是降噪问题，后者是口径问题。它们不是两件事——口径错的时候，你连「该不该紧张」都判断不了。而 38 万元的超卖，根因不是那行 TTL 改动，是**一个永远不会变红的预算**。

---

## 21.2 到底是什么：三个数和一个除法

**定义**（四个词，别混）：

| 词 | 定义 | 一句话记法 |
| --- | --- | --- |
| **SLI** | 一个**比值**：好事件 ÷ 有效事件，带窗口 | 「此刻有多好」 |
| **SLO** | 对 SLI 的目标值 + 统计窗口 | 「应该有多好」 |
| **SLA** | 对客户的合同承诺 + 违约赔付条款 | 「坏了赔多少」 |
| **错误预算** | `1 - SLO`，换算成分钟数或失败请求数 | 「还能坏多久」 |

**三个必须能默写的算式**：

```
① 预算（时间口径）
   30 天预算分钟 = 30 × 24 × 60 × (1 - SLO)
   99.9%  → 43.2 分钟      99.95% → 21.6 分钟      99.99% → 4.32 分钟

② 预算（请求口径）
   预算失败数 = 窗口总请求数 × (1 - SLO)
   例：30 天 2.6 亿请求 × 0.001 = 26 万次失败

③ 燃尽率（burn rate）——本章最重要的一个数
   burn = 实际错误率 ÷ (1 - SLO)
   例：SLO 99.9%、当前错误率 1%  → burn = 1% / 0.1% = 10
       预算 43.2 分钟 ÷ 10 = 4.32 分钟后烧完
   burn = 1 表示「正好按 30 天匀速烧完」，burn = 10 表示「3 天窗口的量 4 分钟烧完」
```

**燃尽率是唯一能把「错误率」翻译成「还剩多少时间」的量。** 这也是为什么不能直接对错误率设阈值——你无法回答「1% 错误率算严重吗」，但你能回答「burn=10 还能撑 4 分钟」。

**四个反例**（每个配一句「不做会怎样」）：

| 你以为的 SLO | 为什么不是 | 不做会怎样 |
| --- | --- | --- |
| 「我们可用性 99.99%」，但没说哪些算失败 | 4xx？429？超时？业务错误码？每换一个口径数就变一次 | 事故复盘时运维和业务算出两个数，谁都不服 |
| 「平均延迟 < 200ms」 | 平均延迟骗人，跨实例求 P99 是错的（第 20 章） | 平均值绿着，P99 已经 8 秒，用户全在骂 |
| 「这个季度没出 P0，所以达标」 | 没有 SLI 就没有预算，没有预算就没有发布刹车 | 事故靠运气，节奏靠感觉，一到旺季就全崩 |
| 「SLO = SLA，对外 99.9% 内部也 99.9%」 | 对外是赔付线，对内是刹车线，两者重合时你没有任何缓冲 | 第一次违约就同时是「预算耗尽 + 赔钱 + 客户流失」 |

> 💡 判断 SLO 定得对不对，只问一句：**「这个数被击穿时，我们打算停什么？」** 如果你的答案是「继续发版、写个复盘」，那它不是 SLO，是装饰。

---

## 21.3 结构图：从一条埋点到一个人半夜起床

```
 ① 埋点            ② SLI 计算           ③ 预算与燃尽            ④ 告警决策
┌─────────┐      ┌──────────────┐     ┌────────────────┐    ┌──────────────────┐
│ 请求计数 │─────►│ 好/有效 = SLI │────►│ burn = err/预算│───►│ 多窗口多燃尽率    │
│ 延迟直方图│     │ 5m/1h/6h/3d  │     │ 剩余 = 1-∫burn │    │ 14.4 / 6 / 1     │
└─────────┘      └──────────────┘     └────────────────┘    └────────┬─────────┘
                                                                      │
       ┌──────────────────────────────────────────────────────────────┘
       ▼
┌──────────────────── Alertmanager（降噪五法全在这里）─────────────────────┐
│  ①分级 route(severity)   ②聚合 group_by   ③抑制 inhibit_rules            │
│  ④静默 silence(带工单+到期)   ⑤根因抑制(上游故障压掉派生告警)             │
└───────────────────────────────┬─────────────────────────────────────────┘
                                ▼
                     ┌──────────────────────┐
                     │ 值班手机（P0/P1 才响）│
                     │ 每条告警必须能回答：   │
                     │ 影响谁？证据？做什么？ │
                     └──────────┬───────────┘
                                ▼
                     ┌──────────────────────┐
                     │ 发布门禁：预算<25% 冻结│
                     │ （CI 里查，人管不住） │
                     └──────────────────────┘
```

两个容易被跳过的点：**③ 与 ④ 之间是「决策」，不是「转发」**——90% 的团队把告警当日志转发，所以噪声全到了人身上；**④ 之下必须有门禁**，因为「预算不足时少发版」靠自觉一定失败。

---

## 21.4 怎么实现：六步，从口径到值班

### 第一步：把 SLI 写成一张有排除项的表

SLI 的价值全在「排除项」里。没有排除项的 SLI，等于一个可以被业务随意解释的数字。

| 类别 | SLI 定义 | 必须排除 | 数据源 | 常见错法 |
| --- | --- | --- | --- | --- |
| 可用性 | 2xx+3xx 请求 ÷ 有效请求 | 4xx（客户端错）、**429（限流，是我们在保护自己）**、健康检查、压测流量 | 网关 access log / 指标 | 把 429 算失败 → 一限流就「违约」 |
| 延迟 | 延迟 < 阈值 的请求 ÷ 有效请求 | 同上 + 超长异步任务 | Histogram 桶 | 用平均延迟、跨实例求分位 |
| 质量（Agent 必备） | 任务成功数 ÷ 任务总数 | 用户主动取消、内容审核拒绝 | 评测集 / 人工标注 | 只看「HTTP 200」不看答得对不对 |
| 成本（Agent 必备） | 单次成功任务成本 < X 元的比例 | 免费额度、内部压测 | 计费流水 | 只看总额，不看每成功任务成本 |
| 新鲜度 | 数据延迟 < N 秒 的比例 | 全量重跑窗口 | 消费位点 - 生产位点 | 用 lag 条数代替时间 |

写法（可作为模板）：

```
SLO-ORDER-API-01
  SLI      : sum(rate(http_requests_total{job="order-api",code=~"2..|3.."}[5m]))
             / sum(rate(http_requests_total{job="order-api",code!~"4.."}[5m]))
  排除     : code=429 / path=~"/health.*" / header[x-shadow]=1（压测流量）
  目标     : ≥ 99.9%，滚动 30 天
  现有预算 : 43.2 分钟 / 26 万次失败
  击穿动作 : 冻结非修复类发布 + 值班升级到 P1
```

**口径必须写在代码里，不能只写在文档里**。否则下一个人加一个 `catch`、或者把 429 当失败，你的 SLI 就悄悄变了，而看板上看不出来：

```java
// Java：SLI 口径的唯一实现。禁止各接口自己算「成功/失败」，否则口径必然分叉
@Component
public class SloRecorder {

    private final Counter good;
    private final Counter valid;
    private final Timer   latency;

    public SloRecorder(MeterRegistry reg) {
        this.good = Counter.builder("slo_events_total").tag("kind", "good").register(reg);
        this.valid = Counter.builder("slo_events_total").tag("kind", "valid").register(reg);
        this.latency = Timer.builder("slo_request_duration_seconds")
                .publishPercentileHistogram()
                .sla(Duration.ofMillis(100), Duration.ofMillis(200), Duration.ofMillis(500),
                     Duration.ofSeconds(1), Duration.ofSeconds(3))
                .register(reg);
    }

    public void record(String route, int status, long latencyMs) {
        if (isExcluded(route, status)) {
            return;                       // 既不计分子也不计分母，口径才干净
        }
        valid.increment();
        if (status < 400) {
            good.increment();
        }
        latency.record(Duration.ofMillis(latencyMs));   // 延迟 SLI 用直方图单独算 P99
    }

    // 排除项集中在一处，改这里必须走 SLO 口径评审（对应 slo_registry.exclude_note）
    private boolean isExcluded(String route, int status) {
        return route.startsWith("/health")
            || route.startsWith("/internal/")
            || route.startsWith("/actuator/")
            || (status >= 400 && status < 500);   // 4xx：客户端错误 + 429（限流是我们主动的）
    }
}
```

> ⚠️ 反例：在网关层用 `status != 200` 判失败。这样 201/204 会被算成失败，429 也会——你的 SLO 会在每次限流时「违约」，然后团队学会无视这个指标。**口径错误比没有 SLO 更糟，因为它给了你一个假的安全感。**

### 第二步：从 SLO 反推告警阈值（多窗口 + 多燃尽率）

为什么不能「错误率 > 5% 就报警」：SLO 是 99.9%，允许错误率只有 **0.1%**；5% 阈值意味着**你已经烧掉 50 倍预算才被告知**。反过来把阈值设成 0.11%，正常波动就会让你一晚上醒 20 次。正确解法是**同时看「烧得多快」和「烧了多久」**——这就是 Google SRE Workbook 的多窗口多燃尽率：

| 档位 | 长窗口 | 短窗口 | 燃尽率阈值 | 消耗预算 | 动作 | 通知方式 |
| --- | --- | --- | --- | --- | --- | --- |
| **P0 page** | 1h | 5m | > 14.4 | 1 小时烧 2% | 立刻止血 | 电话 + 短信 |
| **P1 page** | 6h | 30m | > 6 | 6 小时烧 5% | 30 分钟内响应 | 电话（非工作时间） |
| **P2 ticket** | 3d | 6h | > 1 | 3 天烧 10% | 次日处理 | 工单 + 群 |

算一下第一个数为什么是 14.4：30 天 = 720 小时，1 小时烧掉 2% 预算 → `0.02 × 720 / 1 = 14.4`。三个数字都是这么来的，不是拍的。

**为什么必须两个窗口同时成立**（这是多窗口告警的灵魂，画出来就懂了）：

```
只用一个短窗口(5m)                    只用一个长窗口(1h)
错误率                               错误率
 10% ┤    ██                           10% ┤        ▄▄▄▄
     │    ██      ██                       │   ▄▄▄▄████████
  0% ┼────████────████────► t            0% ┼───████████████──► t
         ↑ 两次抖动各触发一次                ↑ 故障结束 1 小时后才回落
         每次 30s，共吵醒 2 次              期间一直红着，无法判断是否恢复

【两个窗口 AND：短窗口负责「现在还在烧」，长窗口负责「确实烧得狠」】
  P0 触发条件 = burn(5m) > 14.4  AND  burn(1h) > 14.4
        ↑ 立刻响应抖动            ↑ 排除偶发尖刺（1 小时平均被拉平）
  结果：单次 30 秒抖动 → 5m 高但 1h 低 → 不响
        真故障     → 5m 高且 1h 高 → 立刻响
        故障恢复   → 5m 先降 → 立刻停（1h 还高也不影响，因为 AND）
```

**这个 AND 结构是所有「快速响应 + 抗抖动」需求的通用解**，不只是告警：限流的滑动窗口（第 15 章）、熔断的错误率统计（第 18 章）用的都是同一个思路——**短窗口管灵敏度，长窗口管置信度**。

```yaml
# prometheus/rules/slo-order-api.yaml
groups:
  - name: slo-order-api
    interval: 30s
    rules:
      - record: sli:order_api:ratio5m
        expr: |
          sum(rate(http_requests_total{job="order-api",code=~"2..|3.."}[5m]))
          / sum(rate(http_requests_total{job="order-api",code!~"4.."}[5m]))
      - record: sli:order_api:ratio1h
        expr: |
          sum(rate(http_requests_total{job="order-api",code=~"2..|3.."}[1h]))
          / sum(rate(http_requests_total{job="order-api",code!~"4.."}[1h]))
      # burn = 错误率 / 预算率；0.001 就是 1 - 99.9%
      - record: slo:order_api:burn1h
        expr: (1 - sli:order_api:ratio1h) / 0.001
      - record: slo:order_api:burn5m
        expr: (1 - sli:order_api:ratio5m) / 0.001
      # 剩余预算（0~1）；<0 表示已经超支
      - record: slo:order_api:budget_remaining
        expr: 1 - (
            sum(increase(http_requests_total{job="order-api",code=~"5.."}[30d]))
            / sum(increase(http_requests_total{job="order-api",code!~"4.."}[30d]))
          ) / 0.001
      - alert: SLOBurnFast
        expr: slo:order_api:burn5m > 14.4 and slo:order_api:burn1h > 14.4
        for: 2m
        labels:
          severity: critical
          slo: order-api-availability
        annotations:
          summary: "order-api 1 小时内将烧掉 2% 预算（burn={{ $value | printf \"%.1f\" }}）"
          impact: "约 3.2% 的下单请求失败，按当前速率 4 分钟后预算见底"
          evidence: "看板 slo-order-api（5m/1h/6h 三条 burn 曲线 + 错误码 Top5）"
          action: "1) 看依赖健康页 2) 限流非核心 3) 若为发版引起，回滚上一个 release"
          runbook_url: "https://wiki/runbook/order-api-error"
```

> ⚠️ **`for: 2m` 不是可选项**。没有 `for`，一次 15 秒的抖动就会打进电话；`for` 太长（10m），P0 就变成 P2 了。

### 第三步：错误预算与发布节奏联动

预算的意义只有一条：**它把「能不能发版」从一个政治问题变成一个算术问题。**

| 剩余预算 | 发布策略 | 谁能批准 | 备注 |
| --- | --- | --- | --- |
| > 50% | 正常节奏，可发新功能 | 研发负责人 | 鼓励多发小版本 |
| 25%~50% | 正常节奏，但每次发布必须有回滚预案 | 研发负责人 | 灰度比例 ≥ 10% 起步 |
| 10%~25% | **只发修复类 + 已完成灰度验证的版本** | 技术负责人 + SRE | 冻结大功能 |
| < 10% | **全面冻结**，只允许「降低错误率」的变更 | SRE 负责人 | 任何发布需事故指挥签字 |
| < 0（超支） | 冻结 + 强制可靠性专项，下季度 SLO 复核 | CTO | 复盘必须回答「预算为什么算错」 |

**这个策略必须写进 CI，不能靠自觉**：

```go
package releasegate

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"time"
)

// Gate 查 Prometheus 的剩余预算，决定这次发布是否放行。
// 关键点：门禁只允许「更严」，不允许研发用参数绕过（阈值写死在服务端）。
type Gate struct {
	promURL string
	client  *http.Client
}

type gateResult struct {
	Allow     bool
	Reason    string
	Remaining float64 // 0~1，可为负（已超支）
}

// Policy 把「剩余预算」映射成发布策略。这个映射必须只有一份实现（CI 与看板共用）
func Policy(remaining float64) string {
	switch {
	case remaining <= 0:
		return "FREEZE"
	case remaining < 0.10:
		return "FIX_ONLY"
	case remaining < 0.25:
		return "REVIEW"
	default:
		return "NORMAL"
	}
}

func (g *Gate) Check(ctx context.Context, slo string, isFixOnly bool) (gateResult, error) {
	q := fmt.Sprintf(`slo:%s:budget_remaining`, slo) // 例如 order_api
	u := g.promURL + "/api/v1/query?query=" + url.QueryEscape(q)
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	resp, err := g.client.Do(req)
	if err != nil {
		// Prometheus 挂了不允许发布：门禁失败要 fail-closed，不是 fail-open
		return gateResult{Allow: false, Reason: "prometheus unavailable, gate fail-closed"}, err
	}
	defer resp.Body.Close()

	var out struct {
		Data struct {
			Result []struct {
				Value [2]any `json:"value"`
			} `json:"result"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return gateResult{Allow: false, Reason: "bad prometheus response"}, err
	}
	if len(out.Data.Result) == 0 {
		return gateResult{Allow: false, Reason: "no slo series, refuse to release"}, nil
	}
	raw, _ := out.Data.Result[0].Value[1].(string)
	var remaining float64
	fmt.Sscanf(raw, "%f", &remaining)

	res := gateResult{Remaining: remaining}
	switch Policy(remaining) {
	case "FREEZE":
		res.Reason = "预算已超支：全面冻结"
	case "FIX_ONLY":
		res.Allow = isFixOnly
		res.Reason = "预算 <10%：只允许修复类发布"
	case "REVIEW":
		res.Allow = isFixOnly
		res.Reason = "预算 <25%：需技术负责人 + SRE 双签"
	default:
		res.Allow = true
		res.Reason = fmt.Sprintf("预算剩余 %.1f%%，正常发布", remaining*100)
	}
	return res, nil
}

// SnapshotStore 由调用方注入（MySQL/ClickHouse 都行）；门禁只关心「写成功没有」
type SnapshotStore interface {
	Upsert(ctx context.Context, sloKey string, remaining float64, policy string) error
}

// 每天 09:00 由调度任务跑一次，把结果写进 release_freeze 表，让所有人看得见
func (g *Gate) DailySnapshot(ctx context.Context, slos []string, store SnapshotStore) error {
	for _, s := range slos {
		r, err := g.Check(ctx, s, false)
		if err != nil {
			return err
		}
		remaining := r.Remaining // Check 内部解析出的 0~1 数值
		if err := store.Upsert(ctx, s, remaining, Policy(remaining)); err != nil {
			return err // 落库失败必须让任务失败：没有快照 = 没有刹车
		}
	}
	return nil
}
```

```java
// Java：策略判定的「唯一实现」。CI 的 Jenkins step 与内部发布平台都调它，禁止各写一份阈值
@Service
public class ReleasePolicyService {

    public enum Policy { NORMAL, REVIEW, FIX_ONLY, FREEZE }

    private static final double REVIEW_LINE   = 0.25;
    private static final double FIX_ONLY_LINE = 0.10;

    private final JdbcTemplate jdbc;
    private final RestTemplate rest;

    @Value("${prometheus.base-url}")
    private String promUrl;

    public ReleasePolicyService(JdbcTemplate jdbc, RestTemplate rest) {
        this.jdbc = jdbc;
        this.rest = rest;
    }

    public Policy decide(double remaining) {
        if (remaining <= 0)          return Policy.FREEZE;
        if (remaining < FIX_ONLY_LINE) return Policy.FIX_ONLY;
        if (remaining < REVIEW_LINE)   return Policy.REVIEW;
        return Policy.NORMAL;
    }

    // Jenkins / 发布平台调这个接口：不放行就返回 409 + 原因，前端直接把原因展示给研发
    public void assertReleasable(String sloKey, boolean fixOnly) {
        Double remaining = jdbc.queryForObject(
                "SELECT budget_remain FROM release_freeze " +
                "WHERE slo_key = ? AND snap_date = CURDATE()", Double.class, sloKey);
        if (remaining == null) {
            throw new IllegalStateException("no budget snapshot today: gate fail-closed");
        }
        Policy p = decide(remaining);
        boolean allowed = switch (p) {
            case NORMAL -> true;
            case REVIEW -> fixOnly;                  // 修复类可发，大功能要双签
            case FIX_ONLY -> fixOnly;
            case FREEZE -> false;
        };
        if (!allowed) {
            throw new ReleaseBlockedException(
                "release blocked: slo=" + sloKey + " remaining=" + remaining + " policy=" + p);
        }
    }

    @Scheduled(cron = "0 5 9 * * ?")   // 09:05，避开 09:00 的发布高峰
    public void snapshotDaily() {
        jdbc.queryForList("SELECT slo_key FROM slo_registry WHERE status = 1", String.class)
            .forEach(k -> {
                Double r = queryRemainingFromPrometheus(k);   // §21.4 第二步的 budget_remaining
                if (r == null) return;                        // 查不到就不写：宁可缺快照也不写假数
                jdbc.update("""
                    INSERT INTO release_freeze(slo_key, snap_date, budget_remain, policy, decided_by)
                    VALUES (?, CURDATE(), ?, ?, 'auto')
                    ON DUPLICATE KEY UPDATE budget_remain = VALUES(budget_remain),
                                            policy = VALUES(policy)
                    """, k, r, decide(r).name());
            });
    }

    private Double queryRemainingFromPrometheus(String sloKey) {
        // slo_registry 里存的是 order-api-availability，PromQL 里的指标名用下划线
        String q = "slo:" + sloKey.replace('-', '_') + ":budget_remaining";
        String body = rest.getForObject(promUrl + "/api/v1/query?query={q}", String.class, q);
        JsonNode result = Json.readTree(body).path("data").path("result");
        if (!result.isArray() || result.isEmpty()) {
            return null;                        // 查询无结果：不写快照，等下一次重试
        }
        return Double.parseDouble(result.get(0).path("value").get(1).asText());
    }
}
```

```sql
CREATE TABLE slo_registry (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  slo_key        VARCHAR(64)  NOT NULL COMMENT '如 order-api-availability',
  service        VARCHAR(64)  NOT NULL,
  sli_expr       VARCHAR(512) NOT NULL COMMENT 'PromQL：好事件/有效事件',
  exclude_note   VARCHAR(512) NOT NULL COMMENT '排除项说明，必填，否则口径不可审计',
  target         DECIMAL(6,5) NOT NULL COMMENT '如 0.99900',
  window_days    INT          NOT NULL DEFAULT 30,
  owner_team     VARCHAR(64)  NOT NULL,
  status         TINYINT      NOT NULL DEFAULT 1,
  created_at     DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_slo_key (slo_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='SLO 登记表：没有登记的 SLO 不算 SLO';

CREATE TABLE release_freeze (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  slo_key        VARCHAR(64)   NOT NULL,
  snap_date      DATE          NOT NULL,
  budget_remain  DECIMAL(8,4)  NOT NULL COMMENT '剩余预算比例，可为负',
  policy         VARCHAR(32)   NOT NULL COMMENT 'NORMAL/REVIEW/FIX_ONLY/FREEZE',
  decided_by     VARCHAR(64)   NOT NULL DEFAULT 'auto',
  PRIMARY KEY (id),
  UNIQUE KEY uk_slo_date (slo_key, snap_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日预算快照 + 发布策略';

-- 今天哪些服务被冻结？（发版前必查）
SELECT r.service, r.slo_key, f.budget_remain, f.policy
  FROM release_freeze f
  JOIN slo_registry r ON r.slo_key = f.slo_key
 WHERE f.snap_date = CURDATE()
   AND f.policy IN ('FIX_ONLY','FREEZE')
 ORDER BY f.budget_remain ASC;

-- 预算消耗速率 Top10：谁在偷偷烧钱
SELECT slo_key,
       budget_remain,
       LAG(budget_remain) OVER (PARTITION BY slo_key ORDER BY snap_date) - budget_remain AS burned_1d
  FROM release_freeze
 WHERE snap_date >= CURDATE() - INTERVAL 7 DAY
 ORDER BY burned_1d DESC LIMIT 10;
```

### 第四步：告警降噪五法（每法一段配置）

**法一 · 分级**：不是「所有告警都发同一个群」，而是让严重度决定通道。

```yaml
# alertmanager.yml（0.27 口径，matchers/match 语法以你的版本为准）
route:
  receiver: ticket-only          # 默认出口：工单，不响铃
  group_by: [alertname, cluster, service]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - matchers: [severity="critical"]
      receiver: oncall-phone      # 电话 + 短信，只给当天值班的 2 个人
      group_wait: 10s
      repeat_interval: 30m
      continue: false
    - matchers: [severity="warning"]
      receiver: oncall-im
      repeat_interval: 2h
    - matchers: [severity="info"]
      receiver: ticket-only
      repeat_interval: 24h
```

**法二 · 聚合**：`group_by` 是把 230 条重复压成 1 条的地方。加了 `service` 就会出现「按服务各发一条」，不加就是「一起发一条」——**先按 `alertname+cluster` 聚合，再按 `service` 细分**，两级 route 分别配。

```yaml
  routes:
    - matchers: [team="trade"]
      group_by: [alertname, cluster]   # 粗聚合：一个根因一条通知
      group_wait: 30s                  # 等 30s 把同批告警收进来一起发
      group_interval: 5m               # 同组新增告警 5 分钟才再发
      repeat_interval: 4h              # 未恢复的 4 小时提醒一次（不是 1 分钟）
      receiver: trade-oncall
```

**法三 · 抑制**：上游挂了，下游的告警全是噪音，用 `inhibit_rules` 让它们闭嘴。

```yaml
inhibit_rules:
  # 数据库实例宕机 → 压制所有依赖它的服务错误告警
  - source_matchers: [alertname="MySQLInstanceDown"]
    target_matchers: [alertname=~"OrderApiError|PayCallbackError|DBSlowQuery"]
    equal: [cluster]
  # 整个可用区不可用 → 压制该区所有实例级告警（只留一条「AZ down」）
  - source_matchers: [alertname="AvailabilityZoneDown"]
    target_matchers: [severity=~"warning|info"]
    equal: [az]
  # 已有一条 P0 在手，压制同服务 P2（避免同一时刻 5 条一起响）
  - source_matchers: [severity="critical"]
    target_matchers: [severity="info"]
    equal: [service]
```

> ⚠️ 抑制的代价是**可能掩盖真实故障**：如果下游不是被上游拖死的，而是自己也坏了，你会少一条告警。所以抑制规则必须配一条「抑制生效计数」指标（Alertmanager 的 `alertmanager_alerts_invalid_total` 与通知日志），每周看一次。

**法四 · 静默**：变更窗口的合法静音，但**必须带工单号 + 到期时间 + 作者**，且过期为硬约束。

```bash
# 发布窗口静默：2 小时，带工单号，写清范围
amtool silence add \
  alertname=~"OrderApiError|SLOBurnFast" \
  --alertmanager.url=http://alertmanager:9093 \
  --duration=2h --author="zhangsan" \
  --comment="CHG-20260312-017 灰度发布 order-api v2.31"

# 巡检：所有超过 4 小时还在生效的静默（这是隐患）
amtool silence query --alertmanager.url=http://alertmanager:9093 \
  | awk 'NR>1 {print}' # 生产上按 expiresAt 过滤 > 4h 的条目并推到值班群
```

```go
// Go：静默审计。每天 09:00 跑，把「超期 / 无工单 / 覆盖过宽」的静默推到值班群，
// 并自动删掉「expired 但没清」的条目。这是防「静默吃掉故障」最有效的一招。
package silenceaudit

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"
)

type Silence struct {
	ID        string    `json:"id"`
	CreatedBy string    `json:"createdBy"`
	Comment   string    `json:"comment"`
	StartsAt  time.Time `json:"startsAt"`
	EndsAt    time.Time `json:"endsAt"`
	Status    struct {
		State string `json:"state"` // active / pending / expired
	} `json:"status"`
	Matchers []struct {
		Name    string `json:"name"`
		Value   string `json:"value"`
		IsRegex bool   `json:"isRegex"`
	} `json:"matchers"`
}

func Fetch(ctx context.Context, c *http.Client, base string) ([]Silence, error) {
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, base+"/api/v2/silences", nil)
	resp, err := c.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var list []Silence
	if err := json.NewDecoder(resp.Body).Decode(&list); err != nil {
		return nil, err
	}
	return list, nil
}

// Audit 只返回「需要人看一眼」的静默；空切片 = 健康
func Audit(list []Silence) []string {
	var bad []string
	for _, s := range list {
		if s.Status.State != "active" {
			continue
		}
		span := s.EndsAt.Sub(s.StartsAt)
		hasTicket := strings.Contains(s.Comment, "CHG-") || strings.Contains(s.Comment, "INC-")
		tooWide := len(s.Matchers) <= 1 // 只按 alertname 静默 = 同名告警全灭
		switch {
		case span > 4*time.Hour:
			bad = append(bad, fmt.Sprintf("超期静默 id=%s by=%s span=%s endsAt=%s",
				s.ID, s.CreatedBy, span, s.EndsAt.Format(time.RFC3339)))
		case !hasTicket:
			bad = append(bad, fmt.Sprintf("无工单号 id=%s by=%s comment=%q",
				s.ID, s.CreatedBy, s.Comment))
		case tooWide:
			bad = append(bad, fmt.Sprintf("覆盖过宽 id=%s matchers=%d",
				s.ID, len(s.Matchers)))
		}
	}
	return bad
}
```

规矩三条：① 静默必须带 `CHG-` 或 `INC-` 前缀的工单号；② 单次 ≤ 4 小时，超时要重新申请（防止「永久静默」）；③ 每周清理一次「作者已离职 / 工单已关闭但静默仍在」的条目——这是最阴的一类事故来源。

**法五 · 根因抑制**：抑制是按「已知因果」写死规则；根因抑制是**在告警里带上因果链的标签**，让系统自己判断谁是父、谁是子。

```yaml
# 每个服务的告警带上依赖标签，由 exporter/服务启动时注入
labels:
  severity: critical
  service: order-api
  depends_on: "mysql-order,kafka-trade"   # 因果链：我依赖谁
  symptom_of: ""                          # 若由探针判定为派生，填父告警名
```

```yaml
# 根因告警（由黑盒探针直接产生）优先级最高，且压制所有 symptom_of 指向它的告警
inhibit_rules:
  - source_matchers: [alertname="ProbeMySQLOrderFailed", severity="critical"]
    target_matchers: [depends_on=~".*mysql-order.*"]
    equal: []
```

一句话总结五法的分工：**分级决定「谁会醒」，聚合决定「醒几次」，抑制决定「不该醒的不醒」，静默决定「预期内的不醒」，根因抑制决定「同一个病只报一次」。**

### 第五步：可执行告警模板（60 秒三问）

一条合格的告警，值班同学读完 60 秒内必须能回答三问：**影响谁？证据在哪？我现在做什么？** 答不出来的一律降级成工单。

| 要素 | 必填 | 反例（不可执行） | 正例（可执行） |
| --- | --- | --- | --- |
| 标题 | 服务 + 现象 + 量级 | 「订单服务异常」 | 「order-api 5 分钟内 3.2% 下单失败（burn 14.6）」 |
| 影响 | 业务语言 | 「错误率上升」 | 「约 1,100 单/小时无法创建，影响支付前环节」 |
| 证据 | 看板/查询链接 | 「请查看监控」 | 「看板 slo-order-api / 日志查询已预填 trace 条件」 |
| 动作 | 1-2-3 步 | 「请排查」 | 「1) 看依赖健康页 2) 若 5 分钟前有发布则回滚 3) 仍失败则限流非核心接口」 |
| 升级 | 多久没人接就升级 | 无 | 「15 分钟未认领 → 升级到 SRE 二线」 |
| Runbook | 链接 | 无 | `runbook_url` 指向 §现象/判定/止血/根因/验证 |

```yaml
# 反例 vs 正例：同一个 alertname，annotations 质量差 10 倍
- alert: SLOBurnFast
  annotations:
    # ❌ 反例
    # summary: "错误率过高"
    # description: "请检查服务"
    summary: "{{ $labels.service }} 1h 内将烧掉 2% 可用性预算（burn {{ $value | printf \"%.1f\" }}）"
    impact: "按当前速率约 4 分钟后该 SLO 预算见底（30 天 43.2 分钟）"
    evidence: "5m 与 1h 的 burn 同时超阈值；错误码 Top5 见看板 slo-order-api"
    action: "1) 查依赖健康页 2) 5 分钟内有发布→回滚 3) 否则限流非核心接口并升级 P1"
    runbook_url: "https://wiki/runbook/{{ $labels.service }}-error"
```

> 💡 一条硬规矩：**告警里出现「请检查」「请排查」「注意一下」这类词，就退回重写。** 它把判断成本转嫁给了凌晨三点的人。

### 第六步：Agent 侧的 SLO 是三维的

传统系统一个可用性 SLO 就够：请求成功即业务成功。Agent 不是——**HTTP 200 只说明模型回了话，不说明它答对了；而且每次调用都在花钱。**

| 维度 | SLI 定义 | 目标示例 | 预算折算 | 击穿后果 |
| --- | --- | --- | --- | --- |
| **可用性** | 完成（非 5xx / 非超时）的 Run ÷ 有效 Run | ≥ 99.5% | 30 天 216 分钟 | 用户直接不能用 → P0 |
| **任务成功率** | 通过评测 / 无人工返工的 Run ÷ 完成 Run | ≥ 92% | 30 天 8% 的 Run 可失败 | 用户「能用但没用」→ 质量投诉（第 41 章） |
| **成本** | 单次**成功**任务成本 ≤ 0.35 元的比例 | ≥ 95% | 30 天 5% 的超支额度 | 毛利被打穿，账单看起来「正常」 |

三个维度各有独立预算，**不能用一个平均分掩盖**：可用性 99.9% + 任务成功率 70%，平均下来好看，业务已经死了。成本维度的特殊性在于它是**单向**的：可用性预算「没花完」是好事，成本预算「没花完」意味着你可能过度节省（用小模型硬扛难任务），要单独看质量。

```promql
# 三维 SLI（骨架）
# 可用性
sum(rate(run_total{status=~"completed"}[1h])) / sum(rate(run_total{status!="cancelled"}[1h]))
# 任务成功率（来自评测/回访，非实时；按批导入为 gauge 或离线算好再推）
sum(rate(run_eval_pass_total[24h])) / sum(rate(run_eval_total[24h]))
# 成本：每成功任务成本 P95（用直方图，别用平均）
histogram_quantile(0.95,
  sum by (le) (rate(cost_per_success_bucket[6h])))
```

> 详见第 20 章（指标与分位数）、第 45 章（SLO 违约的对外口径与赔付核算）。

### 第七步：Alertmanager 内部到底怎么去重（不知道这层，配不出对的参数）

很多人把 Alertmanager 当成「转发器」，于是 `group_wait`、`group_interval`、`repeat_interval` 三个参数全靠抄。它的真实处理管线是四段，每一段都会丢东西或者合并东西：

```
   Prometheus                Alertmanager 单实例
┌──────────────┐   POST   ┌──────────────────────────────────────────────────┐
│ rule 评估     │─/api/v2─►│ ① Dispatcher：按 route 树做最长匹配              │
│ 每 15s/30s   │  alerts  │    每个 alert 只走一条 route（continue=false 时）│
└──────────────┘          │    ↓                                             │
                          │ ② Inhibition：拿这条 alert 和「当前所有 firing 的 │
                          │    source alerts」逐条比对 matchers + equal       │
                          │    ↓ 被抑制的不进通知管线（但仍计入 alertmanager_ │
                          │      alerts{state="suppressed"}）                 │
                          │ ③ Silence：查内存中的 silence 集合（按 matcher    │
                          │    哈希索引），命中则 state=suppressed            │
                          │    ↓                                             │
                          │ ④ Grouping：用 group_by 的 label 值算 GroupKey    │
                          │    （一个字符串哈希），同 key 的 alert 进同一个   │
                          │    AggregationGroup                              │
                          │    ↓                                             │
                          │ ⑤ Notify：flush 时机 = group_wait（首次）        │
                          │    / group_interval（组内新增）/                  │
                          │    repeat_interval（组内无变化但未恢复）           │
                          └──────────────────────────────────────────────────┘
```

四个必须知道的事实：

1. **`group_wait` 只影响「第一次」**。组已经存在时，新来的 alert 等 `group_interval`，不是 `group_wait`。所以「我把 group_wait 调到 5 分钟怎么还是一条条来」的答案是：第一批之后的都归 `group_interval` 管。
2. **`group_by` 的粒度决定通知条数，`repeat_interval` 决定重复次数**。一个根因 5 类告警 × 24 分区：`group_by: [alertname]` → 5 条；`group_by: [alertname, partition]` → 120 条。**加一个 label，通知量乘一次**。
3. **抑制是 O(告警数 × 源告警数) 的匹配**，源告警（`source_matchers` 命中的）越多，每次评估越慢。生产上超过 5,000 条并发 firing 时，抑制会成为 Alertmanager 的 CPU 热点——这不是理论，是 1,000 节点集群里真实出现过的（`alertmanager_alerts_invalid_total` 与处理延迟同时升高）。
4. **HA 双实例只能保证「不丢」，不保证「不重」**。两个 Alertmanager 通过 `--cluster.peer` 的 gossip 同步静默与通知日志（nflog），但**通知本身可能各发一次**。所以下游（电话/短信网关）必须按 `(GroupKey, 通知序号)` 做幂等，否则一次故障你会收到两个电话。

```bash
# 判断「到底是谁把告警吞了」：三个计数器的差值就是答案
curl -s localhost:9093/metrics | grep -E \
  'alertmanager_alerts_invalid_total|alertmanager_alerts{state|alertmanager_notifications_total|alertmanager_notification_latency'

# 关键判读：
#   alerts{state="suppressed"} 高  → 抑制/静默吃掉了（查 inhibit_rules 与 silence）
#   notifications_total 远小于 alerts → 分组生效（正常），但要看 group_by 是否过粗
#   notification_latency 的 0.99 分位 > 30s → 集成侧（webhook/短信网关）慢，不是 Alertmanager 慢
```

### 第八步：告警的生命周期（把「谁该动」变成状态机）

```
                        ┌─────────────┐
   rule 命中 ──────────►│   pending   │  for: 2m 未满足 → 回到 inactive
                        └──────┬──────┘
                               │ for 满足
                               ▼
                        ┌─────────────┐   inhibit 命中  ┌──────────────┐
                        │   firing    │────────────────►│  suppressed  │
                        └──────┬──────┘   silence 命中  └──────┬───────┘
                               │ 进入通知管线                    │ 源恢复/静默到期
                               ▼                                ▼
                        ┌─────────────┐                  （重新评估，可能回到 firing）
                        │  notified   │  无人认领 15min
                        └──────┬──────┘──────────────► 升级到二线（escalation）
                               │ 有人 ack
                               ▼
                        ┌─────────────┐
                        │ acknowledged│  处理中，repeat_interval 内不再重复通知
                        └──────┬──────┘
                               │ 指标恢复
                               ▼
                        ┌─────────────┐
                        │  resolved   │  发一条「已恢复」，并记录 MTTR
                        └─────────────┘
```

这个状态机里有三个「事故高发点」：**pending 的 `for` 太长 → P0 迟到**；**firing 被 suppress 后没人跟踪 → 故障被静默吃掉**（21.1 的 47 分钟就是这个）；**acknowledged 之后没有超时 → 告警被「认领」但没人处理**（要配 `ack 超时自动升级`）。

---

## 21.5 双修对照：指标换了，方法论一动不动

| 维度 | 传统系统 | Agent 系统 | 同 or 异 |
| --- | --- | --- | --- |
| **机制本体** | 可用性/延迟 SLO + 错误预算 + 燃尽率告警 | 三维 SLO（可用性 + **任务成功率** + **成本**），每个维度一套预算 | **机制完全相同**：算式、窗口、燃尽率一个字不改 |
| **身份 / 唯一键** | `service` / `route` / `status_code` | `run_id` / `step_id` / `model` / `provider`；**Step 事件是计费与告警的共同原子单位** | 同构，多一层会话与模型 |
| **「好事件」的定义** | 2xx 且延迟达标 | 完成 **且** 评测通过 **且** 成本达标 | **异**：好事件要三个条件同时成立 |
| **失败模式** | 5xx、超时、限流误伤 | 加上：答错（200 但不对）、工具循环、上下文爆窗、**重复 Step = 重复花钱** | 多两类：质量与成本 |
| **恢复方式** | 回滚 / 扩容 / 限流 | 加：切小模型、收紧上下文、**用检查点续跑而不是重投整条 Run** | 差异根因：长事务 + 随时中断 |
| **验证方法** | 规则单测（`promtool test rules`）+ 告警演练 | 同 + **成本对账**（预算 vs 账单）+ 评测集回归 | 框架相同，多一笔账 |
| **观测指标** | SLI、burn rate、预算剩余、告警量/人/晚 | 加 `run_success_rate`、`cost_per_success`、`wasted_tokens_total` | 指标集扩展 |
| **稀缺资源** | 连接、线程、下游 QPS | **TPM、模型并发、单 Run 预算** | 限的对象不同（第 15 章） |

**可以直接复用的（一套配置两边用，一行不改）**：`slo_registry` 表结构、燃尽率 recording rules、三档告警窗口（14.4 / 6 / 1）、Alertmanager 五法配置、静默工单规范、发布门禁代码、可执行告警模板的六个要素。

**只有三样必须重算**：

1. **预算的折算单位**。传统的预算是「分钟」；Agent 的成本预算得折成「元」——`30 天预算 = 月预算 × (1-SLO_cost)`，例如月预算 60 万元、成本 SLO 95%，超支预算就是 3 万元。一笔 3 万元的超支，在账单上表现为「用量涨了 5%」，很容易被当成业务增长放过。
2. **「成功」要等评测**。可用性 SLI 是实时的（请求一结束就知道），任务成功率 SLI 有**数小时到一天**的延迟（要等评测/回访）。所以质量维度的告警必须用长窗口（6h/3d），短窗口的 burn 会因数据不全而误报。
3. **重复执行的代价进了预算**。传统系统重试一次消耗几毫秒 CPU，不进 SLO；Agent 的一次重复 Step 直接消耗 token，**必须计入成本 SLI 的分子**，否则你的成本预算永远算不准。

> 三定律检验：**状态机**——真相源在 `Run`/`Step` 表与计费流水，告警只是「哪里不对」的指针；**稀缺资源**——传统限线程与连接，Agent 限 TPM 与预算；**不可回滚**——已花的 token 不退，只能靠事前预算闸门（第 15 章）与事前降级（第 19 章）。

---

## 21.6 会在哪炸：三个失败模式与失效清单

**三个失败模式，各有各的死法**：

| 失败模式 | 现场长什么样 | 根因 | 代价 | 解法 |
| --- | --- | --- | --- | --- |
| **没人看** | 告警发到 2,400 人大群/无人订阅的邮箱；或噪音太多被静音；或告警一直是「已知问题」 | 告警没有 owner，通道没有责任人 | 21.1 的 47 分钟，6.2 万元 | 分级 + 只给值班 2 人 + 每条告警必有 owner 与 runbook |
| **全员看** | P2 也打电话，20 个人同时被叫醒，互相等对方处理 | 严重度不分级、fan-out 到全员 | 责任分散 = 没人负责；三个月后全员静音 | 只有 P0/P1 进电话通道；P2 进工单；一告警一 owner |
| **看错** | 告警说「CPU 高」，实际是下游慢；或看「平均延迟」判断健康；或 429 被算成错误率 | 告警描述症状不描述影响；SLI 口径错 | 按错的方向止血，把小故障治成大故障 | SLI 口径进登记表 + 告警必写影响 + 复盘核对口径 |

**失效模式清单**：

| # | 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- | --- |
| 1 | 一晚上 1,000+ 条告警 | 没有 `group_by` 与 `repeat_interval`，一个根因无限重复 | 统计 `alertname` 分布，Top1 占比 > 50% 就是它 | 粗聚合 + `repeat_interval` 提到 4h | 告警量/人/晚 做成看板，> 20 条即复盘 |
| 2 | 限流一开就「SLO 违约」 | SLI 把 429 算成失败 | 对比限流触发时刻与 SLI 曲线 | 排除 `code=429`（第 15 章） | 口径评审：SLI 必须有 exclude 栏且非空 |
| 3 | 预算一直显示「充足」，事故却频发 | 只统计了 5xx，漏了业务错误码与超时 | 拿一次真实事故手算 SLI | 把业务错误码并入 SLI | 每季度用「最近一次 P1」反算校验口径 |
| 4 | 告警响了但没人动，因为「它每天都响」 | 阈值贴着正常波动（如 CPU > 70%） | 看该告警 30 天触发次数与处理率 | 改成燃尽率告警或直接删掉 | 处理率 < 30% 的告警强制下线 |
| 5 | 静默忘了取消，故障被吞 4 小时 | 静默没有到期硬约束、无工单号 | `amtool silence query` 查长期静默 | 强制 ≤ 4h + 工单号 | 每周清理，静默条目进审计 |
| 6 | 抑制规则掩盖了真实故障 | 抑制范围过宽（`equal: []`） | 对比「抑制计数」与「实际故障数」 | 收窄到 `cluster`/`az` 级别 | 抑制规则必须写「预期抑制哪些告警」 |
| 7 | 预算耗尽但发布没停 | 门禁靠人看、靠周会 | 查 `release_freeze` 有没有每日快照 | 门禁写进 CI 且 fail-closed | 门禁代码纳入 SRE 代码库，变更需 review |
| 8 | Agent 成本超支 30% 没有任何告警 | 只监控了总额与 QPS，没有「每成功任务成本」 | 按 `model`/`tenant` 拆成本 | 加成本 SLI + 预算闸门 | 成本维度进三维 SLO，月度对账 |

---

## 21.7 怎么验证：规则单测、告警演练、口径反算

**① 规则单测**（Prometheus 的告警规则是可测的，别靠「上线看看」）：

```yaml
# promtool test rules slo_order_api_test.yaml
rule_files: [slo-order-api.yaml]
evaluation_interval: 1m
tests:
  - interval: 1m
    input_series:
      # 1 小时内 5% 的请求失败 → burn1h = 0.05/0.001 = 50 → 必须触发 P0
      - series: 'http_requests_total{job="order-api",code="200"}'
        values: '950+950x70'
      - series: 'http_requests_total{job="order-api",code="500"}'
        values: '50+50x70'
    alert_rule_test:
      - eval_time: 70m
        alertname: SLOBurnFast
        exp_alerts:
          - exp_labels: {severity: critical, slo: order-api-availability}
```

```bash
promtool check rules prometheus/rules/slo-order-api.yaml
promtool test rules slo_order_api_test.yaml    # 期望：SUCCESS，且断言 P0 在 70 分钟触发
```

**② 告警演练**（每季度一次，验证「人」这一环）：

```bash
# 造一个可控故障：注入 30% 错误率 5 分钟，看告警在 2 分钟内到人、15 分钟内被认领
kubectl exec -n trade deploy/order-api -- \
  curl -s -XPOST localhost:9090/chaos/error-rate -d '{"ratio":0.3,"duration":"5m"}'

# 记录三个数字：告警发出→手机响（应 < 2min）、响→认领（应 < 15min）、认领→止血（P0 应 < 30min）
```

**③ 口径反算**（最容易被跳过，也最能救命）：拿**最近一次真实 P1**，用事故时间窗手工算一遍 SLI 与 burn，和看板对照。对不上就说明口径错——21.1 的 99.94% 就是这么被发现的。

**④ 必看的五个指标**：

| 指标 | 含义 | 告警线 |
| --- | --- | --- |
| `slo:xxx:budget_remaining` | 剩余预算比例 | < 25% 通知，< 10% 冻结 |
| `slo:xxx:burn1h` | 燃尽率 | > 14.4 持续 2 分钟 |
| 告警量/人/晚 | 值班体验 | > 20 条即降噪，> 50 条即失败 |
| 告警处理率 | 响了有没有人动 | < 30% 的告警强制下线 |
| 静默条目数与最长时长 | 隐患 | 存在 > 4h 的静默即告警 |

---

## 21.8 用什么工具：SLO 这层，开源只给你半套

| 能力 | 开源够用 | 必须自己写 |
| --- | --- | --- |
| 指标与规则 | Prometheus + recording rules + `promtool` | SLI 的**口径与排除项**（没人能替你定义什么叫失败） |
| 告警路由与降噪 | Alertmanager（route/inhibit/silence） | 五法的**规则内容**：谁抑制谁、谁能静默多久 |
| SLO 平台 | Sloth / Pyrra / OpenSLO（自动生成燃尽率规则） | `slo_registry` 与发布门禁的**判定策略** |
| 值班与升级 | PagerDuty / Grafana OnCall / 自建轮值表 | 升级路径与「谁有权冻结发布」 |
| 成本 SLO | 云账单 + Prometheus | 每成功任务成本的口径与归因（按租户/模型/Agent 角色） |
| 质量 SLO | 无成熟开源 | 评测集、人工抽检、投诉归因（第 41、43 章） |

**判断口径**：SLO 这一层，开源给你的是「管道」（指标、路由、静默），你必须自己写的是「法律」（什么叫失败、谁能静默、预算烧完谁说了算）。**Sloth/Pyrra 能替你生成 40 行 PromQL，但生成不了你的排除项。**

---

## 21.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 四个词 | SLI 是比值，SLO 是目标+窗口，SLA 是赔付，预算是 `1-SLO` |
| 预算算式 | `30×24×60×(1-SLO)`：99.9% = 43.2 分钟，99.99% = 4.32 分钟 |
| 燃尽率 | `burn = 错误率 ÷ (1-SLO)`；burn=10 意味着 43.2 分钟的预算 4 分钟烧完 |
| 反推阈值 | 不能对错误率直接设阈值；用多窗口多燃尽率：14.4（1h）/ 6（6h）/ 1（3d） |
| 三个数怎么来 | `0.02×720/1=14.4`、`0.05×720/6=6`、`0.10×720/3=24→按 3d 窗口折成 1` |
| 降噪五法 | 分级（谁醒）、聚合（醒几次）、抑制（不该醒）、静默（预期内）、根因抑制（一个病一次） |
| 静默铁律 | 必须带工单号、≤ 4 小时、每周清理；离职作者的静默最危险 |
| 可执行告警 | 60 秒三问：影响谁、证据在哪、现在做什么；出现「请排查」即退回 |
| 三个失败模式 | 没人看（无 owner）、全员看（不分级）、看错（口径错/只看平均） |
| 发布联动 | 预算 < 25% 需双签，< 10% 只修不发，< 0 全面冻结——**必须写进 CI 且 fail-closed** |
| Agent 三维 | 可用性 + 任务成功率 + 成本，各有独立预算；成本预算的单位是「元/成功任务」 |
| 一句话 | **SLO 不是看板，是刹车；告警不是通知，是任务分派。** |

## 21.10 自检题

1. **SLO 99.95%、窗口 30 天，当前错误率 0.4%。还剩多少预算？按多窗口燃尽率，应该触发哪一档告警？**
   > 要点：预算 = `30×24×60×0.0005` = **21.6 分钟**（请求口径 = 总请求 × 0.0005）。burn = `0.4% ÷ 0.05%` = **8**。1h 窗口的 8 > 6 但 < 14.4，所以是 **P1（6h/30m 档，阈值 6）**，不是 P0；同时要校验长窗口：若 6h 的 burn 也 > 6，触发 P1；若只有 5m 短窗口高而 1h/6h 低，那是一次抖动，靠 `for` 挡掉。剩余预算 = `1 - 8×(经过时间/720h)`，运行 1 小时后剩 `1-8/720≈98.9%`。

2. **你们的告警一晚上 800 条，值班同学已经静音了。给出三条最小改动，并说明每条降的是哪一类噪音。**
   > 要点：① `group_by: [alertname, cluster]` + `repeat_interval: 4h`——降「同一根因重复」（通常占 90%）；② `inhibit_rules` 用上游宕机压制下游派生告警——降「因果放大」；③ 把 `severity=info` 全部改成工单通道、不进电话——降「不需要半夜处理的信息」。做完这三条若还 > 20 条/晚，说明有告警在处理率 < 30% 的区间里反复触发，应直接删除或改燃尽率阈值。

3. **错误预算还剩 40%，产品要求发一个大版本。你怎么判断？如果发完预算掉到 8%，接下来该做什么？**
   > 要点：40% 属于「正常节奏 + 必须有回滚预案 + 灰度 ≥10%」区间，可以发，但要带三条约束：灰度比例、自动回滚判据（burn 1h > 14.4 连续 5 分钟自动回滚）、发布窗口内禁止其他变更。发完掉到 8% 触发 `FIX_ONLY`：冻结一切非修复发布、开可靠性专项、复核 SLO 口径（8% 掉得这么快，先怀疑是不是口径错或某类失败未统计），并在复盘里回答「为什么预算在 2 周内烧掉 90%」。**关键动作是让 `release_freeze` 表自动更新，而不是靠人记住「现在不能发」。**

## 21.11 延伸阅读

- 本章方法源头：Google SRE Workbook 第 5 章《Alerting on SLOs》——多窗口多燃尽率的 14.4/6/1 就是从这里来的，建议逐行读那张表
- Prometheus 告警规则单测：`promtool test rules`（官方文档 `unit_testing_rules`），本章 §21.7 的用例可直接改用
- Alertmanager 配置：`route` 的 `group_by/group_wait/group_interval/repeat_interval`、`inhibit_rules` 的 `source_matchers/target_matchers/equal`、`time_intervals`（0.27 起用 `matchers`，旧配置用 `match`，以你的版本为准）
- SLO 自动生成规则：Sloth（`sloth generate`）与 Pyrra，适合规则量大时减少手写错误
- 指标与分位数的正确做法：[第 20 章](20-metrics-system.md)；限流返回码对 SLI 的影响：[第 15 章](15-rate-limit-basics.md)；降级时的对外表达：[第 19 章](19-degradation-design.md)
- 值班、Runbook、事故指挥：[第 42 章](42-ops-system.md)；SLO 违约的对外核算：[第 45 章](45-incident-two-reports.md)；容量结论如何反哺 SLO：[第 22 章](22-load-test-capacity.md)
- 总纲：[0-summary.md](0-summary.md) ｜ 语言与运行时：[../go/0-summary.md](../go/0-summary.md) ｜ 平台治理：[../llm/0-summary.md](../llm/0-summary.md)
- 前置：[第 20 章](20-metrics-system.md) ｜ 下一章：[第 22 章 压测与容量规划](22-load-test-capacity.md)

**交叉引用**：错误预算的 Agent 侧成本口径见 [../llm/47-cost-engineering.md](../llm/47-cost-engineering.md)；单 Run 预算与检查点见 [../llm/12-long-task-durable.md](../llm/12-long-task-durable.md)。
