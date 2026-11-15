# 20. 监控指标体系：到底该采什么

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 流量与韧性：不崩是怎么做到的 ｜ **编号**：20 / 52
> **本章定位**：监控不是「面板越多越好」，而是「用最少的指标回答最多的问题」。本章把四黄金信号、RED/USE、指标/日志/Trace 的成本、业务与系统指标的打通、以及分位数聚合的正确做法一次讲清。
> **读法**：§20.2 的对照表决定你「采什么」；§20.4 的分位数部分是本章的技术核心，**平均延迟骗人、跨实例求 P99 是错的**，这两句话要能各举一个数字反例。

---

## 20.1 引子：订单量掉了一半，所有系统指标都是绿的

2026-02-14 10:00，情人节大促，某电商的订单量从 **3,200 单/分钟** 掉到 **1,500 单/分钟**。

值班同学先看系统大盘：

- 网关 QPS 12,000，正常；
- 订单服务 P99 180ms，正常；
- 错误率 0.3%，正常；
- CPU 45%、DB 连接 52%、消息堆积 0，全部正常。

「系统没问题啊。」——直到 10:47，客服群炸了：大量用户说「支付成功但订单没生成」。查下来，是某个支付渠道的**业务成功率**从 99.2% 掉到 71%，但支付网关对失败请求返回的是 **HTTP 200 + 业务错误码**，系统指标只统计 HTTP 状态码，所以错误率看起来正常。

| 时间 | 事件 | 数据 |
| --- | --- | --- |
| 10:00 | 情人节大促开闸，订单量应声上冲 | 3,200 单/分钟 |
| 10:03 | 某支付渠道内部限流，40% 交易返回业务码 `SYSTEM_BUSY` | 成功率 99.2% → 71% |
| 10:05 | 大盘全绿：HTTP 5xx 0.3%、P99 180ms、CPU 45% | 无人告警 |
| 10:20 | 订单量掉到 1,500 单/分钟，用户开始反复提交 | 转化率 −53% |
| 10:47 | 客服群 47 分钟时已积压 300+ 咨询 | 客诉开始 |
| 11:02 | 值班按「渠道成功率」维度拆解，定位到该渠道 | 已过 62 分钟 |
| 11:20 | 切备用渠道 + 补偿券，订单量回到 2,900 单/分钟 | 影响 80 分钟 |

```
80 分钟内潜在下单 = 3,200 单/分钟 × 80 = 256,000 单
其中支付业务失败（29%）≈ 74,000 单；扣除用户自行改渠道成功的部分，
实际少成交 ≈ 1.8 万单
按客单价 620 元 → GMV 影响约 1,120 万元；其中可直接归因于该渠道的按 1,100 万元计
```

> 🩸 **最刺眼的一点**：你不是没有数据，你是**没有把业务指标和系统指标连起来**。HTTP 200 不代表订单成功；CPU 正常不代表用户能付钱。

### 事故二：指标自己把监控打死了（2026-03-02）

同一家公司半年后。为了「看清楚每个客户」，有人给订单指标加了 `tenant_id` 标签，又给模型调用指标加了 `prompt_id`。两周后：

| 时间 | 事件 | 数据 |
| --- | --- | --- |
| 03-02 09:40 | 发布带 `tenant_id` / `prompt_id` 标签的版本 | 灰度 20% |
| 10:12 | Prometheus 活跃 series 从 120 万涨到 **480 万** | +300% |
| 10:18 | 单实例内存从 11GB 涨到 26GB，开始频繁 GC | GC 每秒 14 次 |
| 10:26 | 抓取超时（`scrape_timeout` 10s 被击穿），目标大面积 `up=0` | 丢弃 38% 样本 |
| 10:31 | Prometheus OOMKilled，重启后 WAL 回放 9 分钟 | **监控盲区开始** |
| 11:18 | 盲区期间支付渠道故障，无人收到告警 | 47 分钟无人知 |
| 11:40 | 值班从客服群得知，手工查日志定位 | 影响 6,800 单 |
| 12:05 | 回滚标签 + 扩容 Prometheus，series 回落到 130 万 | 恢复 |

代价：**47 分钟监控盲区**、6,800 单受影响、GMV 约 **420 万元**，外加一次「监控系统自己先挂」的经典复盘。根因不是 Prometheus 弱，是**把高基数维度当成了标签**：`tenant_id` 20 万 × `route` 30 × `status` 5 = 3,000 万潜在 series，任何一次流量波动都会把它们全部实例化。

> 💡 两条铁律：**① 指标标签的基数上限是 10,000，超过必须砍维度或下移到日志/Trace；② 监控系统必须有自己的监控**（`prometheus_tsdb_head_series`、`scrape_duration_seconds`、`up`），否则它死的时候你是最后一个知道的。

---

## 20.2 到底是什么：指标是「可聚合的数字」，不是「日志的替代品」

**定义**：指标（metric）= 随时间变化的数值序列，由「名字 + 标签 + 时间戳 + 值」组成，用于聚合、告警和趋势分析。

三种可观测数据的分工：

| 类型 | 回答的问题 | 数据结构 | 成本量级 | 典型工具 |
| --- | --- | --- | --- | --- |
| 指标 Metric | 「现在怎么样？趋势如何？」 | 数值 + 标签，按时间聚合 | 极低（KB~MB/天） | Prometheus、Micrometer |
| 日志 Log | 「这一次发生了什么？」 | 离散文本事件 | 高（GB~TB/天） | Loki、ES |
| Trace | 「这一次慢在哪一段？」 | 调用链 span + 父子关系 | 中（采样后 GB/天） | Jaeger、Tempo、OTel |

### 四黄金信号

| 信号 | 问题 | 传统指标 | Agent 指标 |
| --- | --- | --- | --- |
| 延迟 Latency | 用户等多久？ | P50/P95/P99、成功/失败分开 | TTFT、总时长、每步延迟 |
| 流量 Traffic | 有多少请求？ | QPS、并发连接、队列深度 | 并发 Run、TPM、工具调用数 |
| 错误 Errors | 失败多少？ | 5xx、超时、业务错误码 | 模型 429/5xx、工具失败、任务失败 |
| 饱和度 Saturation | 还剩多少余量？ | CPU、内存、连接池、线程池 | 供应商配额、单 Run 预算、上下文窗口 |

### RED 与 USE：两套框架，一个看服务，一个看资源

| 框架 | 对象 | 三个字母 | 典型指标 | 用在哪 |
| --- | --- | --- | --- | --- |
| RED | 服务/接口 | Rate、Errors、Duration | `http_requests_total`、`http_errors_total`、`http_request_duration_seconds` | 微服务、API、Agent Run |
| USE | 资源 | Utilization、Saturation、Errors | CPU 使用率、队列长度、OOM/连接失败 | 主机、容器、DB、Redis、GPU |

**对照表**：

| 四黄金信号 | RED 侧（服务） | USE 侧（资源） |
| --- | --- | --- |
| 延迟 | Duration P99 | CPU runqueue 等待、磁盘 IO await |
| 流量 | Rate（QPS） | 网络带宽、连接数 |
| 错误 | Errors（5xx/业务错误） | 网卡丢包、连接拒绝、OOM |
| 饱和度 | 并发请求 / 队列深度 | CPU 利用率、内存、fd、连接池 |

> 💡 **一句话**：RED 回答「用户感受到什么」，USE 回答「机器还剩多少」。两个都要，但**告警优先用 RED**——用户不关心你的 CPU，用户关心请求是否成功。

### 四个反例

| 你以为的监控 | 为什么不是 | 不做会怎样 |
| --- | --- | --- |
| 面板越多越专业 | 100 个面板没人看，等于 0 个 | 出事时找不到关键指标 |
| 平均延迟 200ms，很健康 | 1% 用户等 10s，平均仍可能 200ms | 长尾用户流失，你看不见 |
| CPU 是核心指标 | Agent 90% 时间在等上游 token，CPU 只有 15% | 扩容信号完全错 |
| 标签越多越细 | `user_id` 做标签 → 百万 series，Prometheus OOM | 监控系统自己先挂 |

---

## 20.3 结构图：从埋点到告警

```
应用进程
  │
  ├── Counter   （只增：请求数、错误数、token 数）
  ├── Gauge     （可增可减：并发、队列、连接数）
  └── Histogram （分桶：延迟、请求大小、token 长度）
        │
        ▼  /metrics（文本或 protobuf）
┌──────────────────┐    scrape 15s    ┌──────────────────┐
│ Prometheus / VM  │ ◄──────────────── │ Service Discovery │
│  (TSDB + 规则)   │                   └──────────────────┘
└────────┬─────────┘
         │ PromQL
         ├──► Grafana 面板（RED/USE/业务）
         ├──► Alertmanager（SLO 燃尽率、错误率）
         └──► Recording Rules（预聚合 1m/5m，降低查询成本）

日志：Loki / ES  ← 只存「异常 + 采样 + 审计」，不存每请求全量
Trace：OTel Collector ← 1%~10% 采样，按 trace_id 关联日志与指标
```

**三类数据的关联键**：`trace_id` 贯穿日志与 Trace；指标用 `service`、`route`、`status` 等低基数标签关联；业务指标用 `order_id` 只在日志/Trace 里出现，**不要进指标标签**。

---

## 20.4 怎么实现：采什么、花多少、怎么聚合

### 指标 vs 日志 vs Trace 的成本账

假设 10,000 QPS，一天 8.64 亿次请求：

| 数据 | 采集比例 | 单条大小 | 日增数据 | 备注 |
| --- | --- | --- | --- | --- |
| 每请求 1 条日志 | 100% | 500B | **432GB** | 还没算索引膨胀，通常 ×1.5 |
| 10 个指标 × 40 Pod | 100% | 15s 一个样本 | **约 3.5MB** | 400 series × 5,760 样本/天 |
| Trace（1% 采样） | 1% | 1KB/span，平均 8 span | **约 69GB** | 8.64M trace × 8KB |

算一下指标为什么便宜：

```
400 series × (86,400 / 15) 样本/天 = 400 × 5,760 = 2,304,000 样本
按 1.5 字节/样本压缩估算 = 3.5MB/天
```

**432GB vs 3.5MB，差 12 万倍。** 所以：

- 指标：全量采，但**标签必须低基数**；
- 日志：只存异常、审计、采样（1/1000 正常请求）；
- Trace：1%~10% 采样，慢请求/错误请求 100% 采（tail sampling）。

> ⚠️ **高基数标签是 Prometheus 的头号杀手**：`user_id`、`order_id`、`prompt`、`trace_id` 放进标签，series 数会从几百涨到几百万，内存和查询一起爆炸。**这些只能进日志/Trace，不能进指标。**

### 业务指标与系统指标必须打通

订单量掉了一半，排查路径应该是：

```
① 先看业务总量：orders_created_total 是否下降？
   ├── 是 → ② 按渠道/地区/租户拆：哪个维度下降？
   │        ├── 某渠道下降 → ③ 看该渠道的业务成功率（不是 HTTP 错误率）
   │        │                 └── 支付网关 200 但业务码失败 → 找支付团队
   │        └── 全渠道下降 → ④ 看入口 QPS、下单转化漏斗
   └── 否 → ⑤ 看系统指标：延迟、错误、饱和度
```

业务指标示例：

```promql
# 下单量（按渠道）
sum by (channel) (rate(orders_created_total{status="success"}[5m]))

# 支付业务成功率（不是 HTTP 成功率）
sum(rate(payment_result_total{result="success"}[5m]))
  / sum(rate(payment_result_total[5m]))

# 下单转化率：支付成功 / 商品详情 UV
sum(rate(orders_created_total{status="success"}[5m]))
  / sum(rate(product_detail_view_total[5m]))
```

**关键原则**：每个核心业务动作至少有一个「业务结果」指标，且它的标签维度（渠道、租户、地区）要和系统指标对得上。否则你只能看到「HTTP 200」，看不到「订单没成」。

### 分位数聚合：平均延迟为什么骗人

**反例一：平均值掩盖长尾。**

1,000 个请求：990 个 100ms，10 个 10s。

```
平均 = (990 × 0.1 + 10 × 10) / 1,000
     = (99 + 100) / 1,000
     = 0.199s = 199ms
P50 = 100ms
P95 = 100ms
P99 = 10s
```

平均 199ms 看起来「可接受」，但 1% 的用户等了 10 秒。如果你的 SLO 是「P99 < 1s」，这个系统已经违约，而平均延迟完全看不出来。

**反例二：跨实例求 P99 是错的。**

实例 A：10,000 个请求，P99 = 900ms（100 个请求 > 900ms）。
实例 B：100 个请求，P99 = 100ms。

```
错误做法：平均 P99 = (900 + 100) / 2 = 500ms
真实全局：10,100 个请求，1% = 101 个
  A 有 100 个 > 900ms，B 没有
  第 101 个最慢的请求仍在 A，约 900ms
  → 真实 P99 ≈ 900ms
```

平均 P99 把 900ms 说成了 500ms，**低估了 44%**。

反过来也成立：A 只有 100 个请求、P99=1,000ms（1 个慢请求），B 有 10,000 个请求、P99=100ms：

```
平均 P99 = 550ms
真实全局：10,100 个请求，1% = 101 个
  只有 1 个 > 1,000ms，其余都 ~100ms
  → 真实 P99 ≈ 100ms
```

平均 P99 把 100ms 说成了 550ms，**高估了 5.5 倍**。

> 🩸 **结论**：P99 不是线性函数，**不能平均、不能取 max、不能跨实例加权平均**。唯一正确的做法是：每个实例上报**直方图桶计数**，服务端把所有桶的计数相加，再算分位数。

### 正确做法：Histogram + 服务端聚合

```
实例 A：le=0.1: 9000, le=0.3: 9500, le=1: 9900, le=3: 10000, +Inf: 10000
实例 B：le=0.1: 90,   le=0.3: 99,   le=1: 100,  le=3: 100,   +Inf: 100
聚合：  le=0.1: 9090, le=0.3: 9599, le=1: 10000, le=3: 10100, +Inf: 10100
P99 = 第 9,999 个请求所在桶 → le=1 桶，插值约 0.9s
```

PromQL：

```promql
# 全局 P99：先 sum by (le) 聚合桶计数，再算分位
histogram_quantile(0.99,
  sum by (le) (rate(http_request_duration_seconds_bucket{job="order-service"}[5m])))

# 按路由拆分
histogram_quantile(0.95,
  sum by (route, le) (rate(http_request_duration_seconds_bucket[5m])))

# 平均延迟（只用于趋势，不用于 SLO）
sum(rate(http_request_duration_seconds_sum[5m]))
  / sum(rate(http_request_duration_seconds_count[5m]))
```

**桶怎么设计**：桶边界要覆盖 SLO 附近，且数量控制在 10~20 个。例如 SLO 是 P99 < 1s：

```
0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1, 1.5, 2, 3, 5, 10
```

桶太粗（只有 0.1/1/10），P99 误差可能 50%；桶太细（100 个），指标成本和查询成本都上去了。**先定 SLO，再定桶。**

### 分位数：从桶到数字的完整计算（`histogram_quantile` 到底做了什么）

很多人会写这条 PromQL，但说不清它为什么可信、什么时候不可信。把它拆开看，只有四步：

```
① 每个实例本地累计桶计数（单调递增的 Counter，le = 上边界）
   实例 A: le=0.1→9000  le=0.3→9500  le=1→9900  le=3→10000  le=+Inf→10000
   实例 B: le=0.1→90    le=0.3→99    le=1→100   le=3→100    le=+Inf→100

② 服务端按 le 相加（累积计数可加：两个不相交请求集合的并集计数 = 各自计数之和）
   sum by (le): 0.1→9090  0.3→9599  1→10000  3→10100  +Inf→10100

③ 求 rank = q × 总观测数 = 0.99 × 10100 = 9999

④ 找到第一个 cumulative ≥ rank 的桶 → (0.3, 1]，桶内计数 = 10000 − 9599 = 401
   桶内偏移 rank' = 9999 − 9599 = 400
   P99 = 0.3 + (1 − 0.3) × 400/401 ≈ 0.998s
```

Prometheus 的实现（`promql/quantile.go` 的 `bucketQuantile`，2.x/3.x 语义一致）有三个必须知道的边界处理：

| 情况 | 实现行为 | 后果 |
| --- | --- | --- |
| q 落在最高有限桶之上（即 `+Inf` 桶） | 返回**倒数第二个桶的上边界** | 只能说「≥ 该边界」，给不出更精确的值 |
| 只有 1 个桶 / 总观测数为 0 | 返回 `NaN` | 面板显示 `NaN`，告警表达式要能容忍 |
| 缺少 `le="+Inf"` 桶 | q 落在最后一档时返回 `NaN` | 自己拼指标（非客户端库）时最容易漏 |
| 桶计数非单调（并发/重置导致） | 先做 monotonicity 修正，再插值 | 偶发尖刺会被压平，别用它做精确审计 |

**误差从哪来**：插值假设「桶内均匀分布」。这个假设在长尾上几乎必然错误：

| 真实分布（401 笔落在 (0.5, 1] 桶内） | 插值结果 | 相对误差 |
| --- | --- | --- |
| 全部集中在 **0.51s** | 0.998s | **+96%**（把 0.5s 说成 1s） |
| 均匀分布 | 0.99s | +1% |
| 全部集中在 0.99s | 0.998s | +0.8% |

**所以桶边界必须卡在 SLO 附近**：SLO 是「P99 < 1s」，就把 `0.75/1/1.5` 放进去；如果你的桶是 `0.1/1/10`，P99 落在 (0.1,1] 里，插值出来的数字可能差 5~10 倍，你却拿它当 SLA 依据。

**原生直方图（native histogram）解决了什么**：它不再上报固定桶，而是上报「指数分桶 + 桶计数 + 零桶 + 总和/总数」，schema 每 +1 桶数翻倍、相对误差减半（以 Prometheus 官方文档与你使用的版本为准）。查询写法也变了——**没有 `_bucket` 后缀，也不需要 `sum by (le)`**：

```promql
# 经典 Histogram：必须 sum by (le)，再算分位
histogram_quantile(0.99,
  sum by (le) (rate(http_request_duration_seconds_bucket{job="order-service"}[5m])))

# 原生 Histogram：直接对整体求和（rate 作用在原生直方图上）
histogram_quantile(0.99,
  sum(rate(http_request_duration_seconds{job="order-service"}[5m])))

# 想看「桶的原始分布」而不是分位数：直接看累积计数，别用 avg
sum by (le) (rate(http_request_duration_seconds_bucket{job="order-service"}[5m]))
```

> 🩸 **一个真实的反例**：某团队 SLO 写「P99 < 1s」，桶只配了 `0.1/1/10`，看板常年显示 P99 = 0.62s，看起来达标。后来用原始日志精确计算，真实 P99 是 **2.4s**。两个错误叠在一起互相掩护：桶太粗（真实 P99 落在 (1,10] 这种 10 倍宽的桶里，插值毫无意义），加上 `histogram_quantile` 的第一个参数被误写成 `0.9`（看板显示的其实是 P90）。**半年没人发现，因为「看起来一直是绿的」。**

### Java：Micrometer Histogram

```java
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import org.springframework.stereotype.Component;

import java.time.Duration;

@Component
public class OrderMetrics {

    private final Timer requestTimer;

    public OrderMetrics(MeterRegistry registry) {
        this.requestTimer = Timer.builder("http_request_duration_seconds")
                .description("HTTP request latency")
                .publishPercentileHistogram()
                .sla(Duration.ofMillis(100),
                     Duration.ofMillis(300),
                     Duration.ofSeconds(1),
                     Duration.ofSeconds(3))
                .register(registry);
    }

    public void record(String route, String status, long nanos) {
        // route 必须是模板（/orders/{id}），不能是原始 URL；status 只取 2xx/4xx/5xx
        requestTimer.labels(route, status).record(Duration.ofNanos(nanos));
    }
}
```

> ⚠️ **不要给 Timer 加 `userId`、`orderId` 标签**。`route` 要做模板化（`/orders/{id}` → `/orders/:id`），否则每个订单一个 series，Prometheus 几天就 OOM。

### Go：Prometheus Histogram

```go
package metrics

import (
	"net/http"
	"strconv"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

var httpDuration = prometheus.NewHistogramVec(
	prometheus.HistogramOpts{
		Name:    "http_request_duration_seconds",
		Help:    "HTTP request latency",
		Buckets: []float64{0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1, 1.5, 2, 3, 5, 10},
	},
	[]string{"route", "method", "status"},
)

func init() {
	prometheus.MustRegister(httpDuration)
}

func Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rw := &statusWriter{ResponseWriter: w, status: 200}
		next.ServeHTTP(rw, r)
		route := r.Pattern // Go 1.22+ 的模板路由；不要用 r.URL.Path
		httpDuration.WithLabelValues(route, r.Method,
			strconv.Itoa(rw.status/100)+"xx").
			Observe(time.Since(start).Seconds())
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) WriteHeader(code int) {
	w.status = code
	w.ResponseWriter.WriteHeader(code)
}

func Handler() http.Handler {
	return promhttp.Handler()
}
```

### Agent 指标：token、成本、TTFT、工具成功率怎么埋

传统服务的指标是「请求数 × 延迟 × 错误」；Agent 的指标多四个维度，而且**每一个都有独特的坑**：

| 指标 | 类型 | 标签（基数受控） | 坑 | 不做会怎样 |
| --- | --- | --- | --- | --- |
| `llm_tokens_total` | Counter | `provider`, `model`, `type`(input/output/cached) | 把 `run_id` 当标签 → series 爆炸（事故二） | 算不出 TPM，也说不清钱花在哪 |
| `llm_ttft_seconds` | Histogram | `provider`, `model`, `stream`(true) | 把非流式请求也塞进来，TTFT 等于总时长 | 首包延迟被高估 3~10 倍，优化方向全错 |
| `agent_run_cost_usd` | Histogram | `tenant_tier`, `model` | 用 `tenant_id` 标签；或只记总量不记单 Run | 无法定位「哪个租户/任务在烧钱」 |
| `tool_call_total` | Counter | `tool`, `status`(ok/error/timeout) | 用工具名 + 参数拼标签 | 工具成功率算不出来，熔断没有依据 |
| `agent_task_success_total` | Counter | `task_type`, `judge`(rule/llm/human) | 把「HTTP 200」当任务成功 | 质量 SLO 变成假指标 |
| `provider_quota_remaining` | Gauge | `provider`, `quota_type`(rpm/tpm) | 只在启动时拉一次 | 配额耗尽前 0 预警 |

```java
@Component
public class AgentMetrics {

    private final Counter tokens;
    private final Counter toolCalls;
    private final Counter taskSuccess;
    private final Timer ttft;
    private final DistributionSummary runCost;

    public AgentMetrics(MeterRegistry reg) {
        this.tokens = Counter.builder("llm_tokens_total")
                .description("LLM tokens by provider/model/type")
                .register(reg);
        this.toolCalls = Counter.builder("tool_call_total")
                .description("tool invocations by tool and status")
                .register(reg);
        this.taskSuccess = Counter.builder("agent_task_success_total")
                .description("task-level success judged by rule/llm/human")
                .register(reg);
        // TTFT：只对流式请求记录首包耗时；桶卡在 200ms~5s（用户感知区间）
        this.ttft = Timer.builder("llm_ttft_seconds")
                .serviceLevelObjectives(
                        Duration.ofMillis(200), Duration.ofMillis(500),
                        Duration.ofSeconds(1), Duration.ofSeconds(2), Duration.ofSeconds(5))
                .register(reg);
        // 单 Run 成本：桶按 0.01~5 美元，覆盖 99% 的对话型 Run
        this.runCost = DistributionSummary.builder("agent_run_cost_usd")
                .serviceLevelObjectives(0.01, 0.05, 0.1, 0.5, 1.0, 5.0)
                .register(reg);
    }

    public void onLlmCall(String provider, String model, Usage u, long ttftNanos, boolean stream) {
        tokens.labels(provider, model, "input").increment(u.input());
        tokens.labels(provider, model, "output").increment(u.output());
        tokens.labels(provider, model, "cached").increment(u.cached());
        if (stream) {
            ttft.labels(provider, model, "true").record(Duration.ofNanos(ttftNanos));
        }
    }

    public void onRunEnd(String tenantTier, String model, double costUsd, boolean success, String judge) {
        runCost.labels(tenantTier, model).record(costUsd);   // tenant_tier，不是 tenant_id
        if (success) taskSuccess.labels("chat", judge).increment();
    }

    public void onToolCall(String tool, String status) {
        toolCalls.labels(tool, status).increment();
    }
}
```

```promql
# 每成功任务成本（最该上墙的一个数：它同时反映质量与成本）
sum(rate(agent_run_cost_usd_sum[1h]))
  / sum(rate(agent_task_success_total[1h]))

# TTFT 的 P95（只统计流式；桶已按用户感知区间配置）
histogram_quantile(0.95,
  sum by (le) (rate(llm_ttft_seconds_bucket{stream="true"}[5m])))

# 工具成功率（按工具拆，掉到 95% 以下就该熔断，见第 18 章）
sum by (tool) (rate(tool_call_total{status="ok"}[5m]))
  / sum by (tool) (rate(tool_call_total[5m]))

# 缓存命中带来的成本节省（cached token 单价通常是 input 的 10%~25%）
sum(rate(llm_tokens_total{type="cached"}[1h]))
  / sum(rate(llm_tokens_total{type="input"}[1h]))
```

> ⚠️ **三个 Agent 特有的指标陷阱**：① `ttft` 必须只在流式请求上记录，非流式的「首包」就是完整响应；② 成本指标必须带 `price_version` 或 `model` 标签，否则供应商调价后你无法解释「为什么单位成本涨了 30%」；③ **工具成功率 ≠ 任务成功率**——工具 99% 成功，任务也可能因为规划错误只有 70% 成功，两个都要有（第 21 章会讲怎么把它们做成 SLO）。

### Prometheus 配置

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s
  external_labels:
    cluster: prod-cn-east
    env: prod

scrape_configs:
  - job_name: order-service
    metrics_path: /actuator/prometheus
    kubernetes_sd_configs:
      - role: pod
    relabel_configs:
      - source_labels: [__meta_kubernetes_pod_label_app]
        regex: order-service
        action: keep
      - source_labels: [__meta_kubernetes_pod_name]
        target_label: pod

rule_files:
  - /etc/prometheus/rules/*.yml

alerting:
  alertmanagers:
    - static_configs:
        - targets: ["alertmanager:9093"]
```

---

## 20.5 双修对照：传统 vs Agent（名字变了，方法论没变）

| 维度 | 传统系统 | Agent 系统 | 同 or 异 |
| --- | --- | --- | --- |
| 框架 | 四黄金信号、RED/USE | **完全相同**，再加 token/成本/质量 | **同** |
| 身份/唯一键 | `service`、`route`、`status` | `provider`、`model`、`run_id`、`step_id` | 同构，多一层模型/会话 |
| 延迟 | P50/P95/P99 | 加 **TTFT**（首 token）、每步延迟、总时长 | 同 + 分段 |
| 流量 | QPS、并发连接 | **并发 Run、TPM、工具调用数** | 单位不同 |
| 错误 | 5xx、超时 | 加 429、模型拒答、工具失败、任务失败 | 多几类 |
| 饱和度 | CPU、内存、连接池 | 加 **供应商配额、上下文窗口、单 Run 预算** | 资源换成了配额/预算 |
| 成本 | 机器折旧，近似固定 | **按 token 精确计费**，必须进指标 | **异**（根因：成本显性且按量） |
| 验证 | 压测 + 对账 | 加 **评测集 / LLM-as-judge** | 同 + 质量评估 |
| 观测指标 | `http_requests_total` | 加 `llm_tokens_total`、`run_cost_usd`、`agent_quality_score` | 指标集扩展 |

**哪些完全相同、可以直接复用**：RED/USE 框架、四黄金信号、Histogram 分位数聚合、Prometheus 配置、Grafana 面板结构、告警规则。**这些不关心你调用的是 MySQL 还是大模型。**

**差异根因**：传统指标的「请求」是等价的；Agent 的「一次 Run」可能 3 步，也可能 30 步，token 从 2k 到 200k。所以 Agent 的指标必须多三个维度：

```
token：input/output/cached，按 provider/model/tenant 拆
成本：$/Run、$/成功任务、预算余额
质量：任务成功率、人工采纳率、评测分数、工具成功率
```

Agent 侧最小指标集：

| 指标 | 类型 | 标签 | 用途 |
| --- | --- | --- | --- |
| `llm_tokens_total` | Counter | provider, model, type | TPM、成本、配额 |
| `llm_request_duration_seconds` | Histogram | provider, model, phase(ttft/total) | 延迟、慢调用熔断 |
| `agent_run_duration_seconds` | Histogram | status, task_type | Run 级 SLO |
| `agent_run_cost_usd` | Histogram | tenant, model | 单 Run 预算、成本异常 |
| `tool_call_total` | Counter | tool, status | 工具熔断、成功率 |
| `agent_task_success_total` | Counter | task_type, judge | 质量 SLO |
| `provider_quota_remaining` | Gauge | provider, quota_type | 供应商配额饱和度 |

---

## 20.6 会在哪炸：失效模式清单

| 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 平均延迟正常，用户投诉慢 | 只看平均，没看 P99 | 对比 avg 与 P99 曲线 | 加 Histogram + P99 面板 | SLO 只用分位数，不用平均 |
| 跨实例 P99 忽高忽低 | 对 P99 求平均/max | 看查询语句 | `sum by (le)` 后再 `histogram_quantile` | 代码评审 PromQL |
| Prometheus 内存暴涨 | 高基数标签（user_id/order_id） | `count by (__name__)` 看 series 数 | 删标签/改日志 | 标签白名单 + CI 检查 |
| 订单掉了但错误率正常 | 只统计 HTTP 状态码 | 看业务结果指标 | 加 `orders_created_total`、支付业务码 | 每个核心动作有业务指标 |
| 指标单位和面板不一致 | ms 与 s 混用 | 看指标名与数值 | 统一用秒（Prometheus 惯例） | 命名规范 + 单测 |
| Counter 重置导致负增长 | 进程重启 | 看 `resets` | 用 `rate()` 自动处理 | 不要用 `delta()` 算 Counter |
| Histogram 分位数误差大 | 桶太粗/不覆盖 SLO | 对比精确日志 | 重设桶边界 | 按 SLO 设计桶 |
| 监控系统自己先挂 | 采集/存储过载 | 看 Prometheus CPU/磁盘 | 采样、远程写、分片 | 监控的监控 |

---

## 20.7 怎么验证：规则测试、基数审计、精度对比

**① PromQL 规则测试：**

```bash
promtool check rules rules/order-slo.yml
promtool test rules rules/order-slo_test.yml
```

**② 基数审计：**

```promql
# 每个指标的 series 数 Top 20
topk(20, count by (__name__) ({__name__=~".+"}))

# 某个指标的不同标签值数量
count(count by (route) (http_request_duration_seconds_count))
```

规则：单个指标 series 数 > 10,000 就要审查；> 100,000 基本是事故。

**③ 分位数精度对比**：用同一批请求的原始日志算精确 P99，与 Prometheus `histogram_quantile` 对比，误差应 < 10%；超过就调桶。这个对比必须做成工具，不能靠手工——下面是可直接跑的 Go 版本：

```go
// pmcheck：从 Prometheus 拉桶计数，用与 Prometheus 相同的算法复算 P99，再和看板值比对。
// 用法：go run pmcheck.go -q 'sum by (le)(rate(http_request_duration_seconds_bucket{job="order"}[5m]))' -quantile 0.99
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"math"
	"net/http"
	"net/url"
	"sort"
	"strconv"
)

type sample struct{ Metric map[string]string; Value [2]string }

func main() {
	q := flag.String("q", "", "bucket 查询语句（必须含 le 标签）")
	prom := flag.String("prom", "http://prometheus:9090", "Prometheus 地址")
	p := flag.Float64("quantile", 0.99, "目标分位")
	flag.Parse()

	resp, err := http.Get(*prom + "/api/v1/query?query=" + url.QueryEscape(*q))
	if err != nil { panic(err) }
	defer resp.Body.Close()
	var out struct{ Data struct{ Result []sample } }
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil { panic(err) }

	type bucket struct{ upper, cum float64 }
	bs := make([]bucket, 0, len(out.Data.Result))
	for _, s := range out.Data.Result {
		le := s.Metric["le"]
		var up float64
		if le == "+Inf" { up = math.Inf(1) } else if up, err = strconv.ParseFloat(le, 64); err != nil { continue }
		v, _ := strconv.ParseFloat(s.Value[1], 64)
		bs = append(bs, bucket{up, v})
	}
	sort.Slice(bs, func(i, j int) bool { return bs[i].upper < bs[j].upper })
	if len(bs) < 2 {
		fmt.Println("桶不完整：至少需要 2 个 le（其中一个是 +Inf）")
		return
	}

	// 与 promql/quantile.go 的 bucketQuantile 同构：找第一个 cum>=rank 的桶，桶内线性插值
	total := bs[len(bs)-1].cum
	if total == 0 {
		fmt.Println("NaN：窗口内没有观测")
		return
	}
	rank := *p * total
	for i, b := range bs {
		if b.cum < rank { continue }
		if i == len(bs)-1 { fmt.Printf("P%.0f >= %v（落在 +Inf 桶，只能给下界）\n", *p*100, bs[len(bs)-2].upper); return }
		lo, cnt := 0.0, b.cum
		if i > 0 { lo, cnt = bs[i-1].upper, b.cum-bs[i-1].cum }
		val := lo + (b.upper-lo)*((rank-(b.cum-cnt))/cnt)
		fmt.Printf("P%.0f = %.3fs  (桶 [%v,%v] 内 %v 个观测)\n", *p*100, val, lo, b.upper, cnt)
		return
	}
	fmt.Println("NaN：总观测数为 0 或桶不完整")
}
```

判据：**看板值 vs 本工具算出的值必须一致**（同一个算法，差 0 才正常）；再拿 **精确日志算的真值** 对比，误差 > 10% 就说明桶太粗，回去改桶边界——而不是改 SLO。

**④ 业务指标对账**：`orders_created_total` 的日增量 vs 订单表 `COUNT(*)`，差异 < 0.5%；支付成功率 vs 支付渠道账单，差异 < 0.1%。

```sql
-- 业务指标与库的日对账（T+1 跑，任一超阈值就建工单）
SELECT 'orders' AS metric,
       (SELECT COUNT(*) FROM orders WHERE created_at >= CURDATE() - INTERVAL 1 DAY
                                     AND created_at <  CURDATE())            AS db_cnt,
       (SELECT value FROM metric_daily_snapshot
         WHERE metric = 'orders_created_total' AND biz_date = CURDATE() - INTERVAL 1 DAY) AS metric_cnt
UNION ALL
SELECT 'pay_success',
       (SELECT COUNT(*) FROM payment WHERE status = 'SUCCESS'
          AND updated_at >= CURDATE() - INTERVAL 1 DAY AND updated_at < CURDATE()),
       (SELECT value FROM metric_daily_snapshot
         WHERE metric = 'payment_success_total' AND biz_date = CURDATE() - INTERVAL 1 DAY);
-- 判定：|metric_cnt - db_cnt| / db_cnt < 0.5%；超过说明埋点丢样本或指标口径与库不一致
```

**⑤ 告警演练**：故意把 P99 打到 SLO 以上，验证告警在 2 分钟内触发，且告警内容包含「路由、当前值、阈值、Runbook 链接」。

**⑥ 监控的监控（事故二的直接修复）**：

```promql
# Prometheus 自己的关键自监控：这三个数任一异常，先修监控再看业务
prometheus_tsdb_head_series{job="prometheus"}                       # series 总数，突增就是标签事故
rate(prometheus_target_scrapes_sample_dropped_total[5m]) > 0         # 丢样本 = 抓取超时
rate(prometheus_target_scrape_pool_sync_total[5m])                   # 目标同步是否卡住
```

**⑤ 告警演练**：故意把 P99 打到 SLO 以上，验证告警在 2 分钟内触发，且告警内容包含「路由、当前值、阈值、Runbook 链接」。

---

## 20.8 用什么工具

| 场景 | 首选 | 理由 / 坑 |
| --- | --- | --- |
| 指标存储 | Prometheus / VictoriaMetrics / Thanos | 单机 Prometheus 适合中小规模；长期存储用 Thanos/VM |
| Java 埋点 | Micrometer | 与 Spring Boot 集成好；注意 `publishPercentileHistogram` 的成本 |
| Go 埋点 | `prometheus/client_golang` | 标准；标签基数要自己控制 |
| 日志 | Loki / Elasticsearch | Loki 便宜但查询弱；ES 强但贵 |
| Trace | OTel + Tempo/Jaeger | 采样策略是关键；tail sampling 保慢请求 |
| 可视化 | Grafana | 面板按 RED/USE 分层，不要堆 100 个图 |
| 告警 | Alertmanager | 见第 21 章 |

**自研边界**：埋点 SDK 用成熟库；**必须自研的是「指标字典」和「SLO 定义」**——哪些指标是核心、阈值多少、谁负责，这些框架替不了你。

---

## 20.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 指标 vs 日志 vs Trace | 指标看趋势，日志看单次，Trace 看链路；成本差 10 万倍 |
| 四黄金信号 | 延迟、流量、错误、饱和度 |
| RED/USE | RED 看服务（Rate/Errors/Duration），USE 看资源（Util/Sat/Errors） |
| 平均延迟 | 990 个 100ms + 10 个 10s → 平均 199ms，P99 10s；平均骗人 |
| 跨实例 P99 | 不能平均、不能 max；先 `sum by (le)` 再 `histogram_quantile` |
| Histogram | 桶按 SLO 设计，10~20 个；桶太粗误差 50% |
| 高基数 | `user_id`/`order_id`/`prompt` 不进标签；series > 10k 要审查 |
| 业务指标 | HTTP 200 ≠ 订单成功；每个核心动作要有业务结果指标 |
| 成本 | 10k QPS 全量日志 432GB/天，10 个指标仅 3.5MB/天 |
| Agent | 加 token、TTFT、成本、任务成功率、供应商配额 |
| 验证 | promtool、基数审计、分位数精度对比、业务对账 |

---

## 20.10 自检题

1. **1,000 个请求：990 个 100ms，10 个 10s。平均延迟、P99 各是多少？为什么 SLO 不能用平均？**
   <details><summary>答案要点</summary>
   平均 = (990×0.1 + 10×10)/1,000 = 199ms；P99 = 10s。平均被 99% 的快请求拉低，完全掩盖了 1% 用户的 10 秒等待。SLO 必须用分位数（P99/P95），并且成功/失败请求要分开统计；平均只用于趋势。
   </details>

2. **实例 A 10,000 请求 P99=900ms，实例 B 100 请求 P99=100ms。全局 P99 是多少？为什么不能 (900+100)/2？**
   <details><summary>答案要点</summary>
   全局 10,100 请求，1% ≈ 101 个；A 有 100 个 >900ms，第 101 个仍在 A，所以全局 P99 ≈ 900ms，不是 500ms。P99 是非线性函数，对分位数做平均没有数学意义；正确做法是每个实例上报 Histogram 桶计数，`sum by (le)` 后统一算分位。
   </details>

3. **订单量掉了一半，但 HTTP 错误率、CPU、P99 都正常。你的排查顺序是什么？**
   <details><summary>答案要点</summary>
   ① 先看业务总量指标（`orders_created_total`）确认下降；② 按渠道/地区/租户拆，定位哪个维度；③ 看该渠道的**业务成功率**（支付返回 200 但业务码失败）；④ 看下单转化漏斗（详情 UV → 加购 → 支付）；⑤ 最后才看系统指标。关键：HTTP 200 不代表业务成功，必须有业务结果指标与系统指标打通。
   </details>

---

## 20.11 延伸阅读

- Google SRE Book：*Monitoring Distributed Systems*（四黄金信号）
- Prometheus 文档：`histogram_quantile`、native histograms、`rate()` 与 Counter 重置
- Micrometer 文档：`publishPercentileHistogram`、`sla`、`minimumExpectedValue/maximumExpectedValue`
- Tom Wilkie：*Prometheus Histograms* 与 *Native Histograms* 演讲
- 上一章：[第 19 章 降级与兜底设计](19-degradation-design.md)
- 下一章：[第 21 章 SLO、错误预算与告警降噪](21-slo-alerting.md)

**交叉引用**：总纲 [0-summary.md](0-summary.md) ｜ 语言与运行时 [../go/0-summary.md](../go/0-summary.md) ｜ 平台治理 [../llm/0-summary.md](../llm/0-summary.md)
