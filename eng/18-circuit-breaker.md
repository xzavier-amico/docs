# 18. 熔断：状态机、阈值与隔离

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 流量与韧性：不崩是怎么做到的 ｜ **编号**：18 / 52
> **本章定位**：熔断不是「出错就断」，而是「用一个状态机替下游争取恢复时间」。本章把三态机、错误率与慢调用两套阈值、线程池与信号量两种隔离拆开算账，并给出判断一个熔断是否「强壮」的 8 条判据。
> **读法**：§18.3 的状态图与转换表必须能背；§18.4 的「错误率 vs 慢调用」和「线程池 vs 信号量」两张表决定你会不会误熔断；Agent 读者直接看 §18.4 末尾的成本熔断。

---

## 18.1 引子：下游只是变慢，没有报错，熔断器全程没动

2026-03-03 20:12，某电商商品详情页开始变慢。

链路是 `product-page → reco-service`（推荐服务）。`product-page` 有 **24 个 Pod，每个 200 个工作线程**，平时 3,000 QPS，P99 180ms，线程池利用率约 11%。

20:12:30，`reco-service` 的 P99 从 300ms 涨到 **8 秒**，但它的错误率只有 **2%**——因为它没挂，只是每个请求要等一个慢查询。`product-page` 的熔断器配置是：

```yaml
failure_rate_threshold: 0.5    # 错误率 50% 才断
sliding_window_size: 100
minimum_number_of_calls: 20
```

错误率 2% < 50%，熔断器**永远不会打开**。于是：

```
3,000 QPS × 8s = 24,000 个并发请求
线程池只有 24 × 200 = 4,800 个线程
缺口 = 19,200 个请求在队列里等
客户端 3s 超时 → 重试 1.5 倍 → 入口 4,500 QPS
4,500 × 8s = 36,000 个并发 → 队列更长
```

20:15，`product-page` 线程池 100% 占满，P99 12s，错误率 74%；20:18 人工把 `reco-service` 的调用超时从 10s 压到 1s 并打开熔断，20:35 恢复。**23 分钟，140 万次失败请求，GMV 损失约 210 万元。**

复盘时发现两个问题：

1. **只配了错误率熔断，没配慢调用熔断**。下游「慢而不错」是最常见的死法，尤其在 Agent 场景——模型供应商很少直接报错，它只是从 3s 变成 40s。
2. **熔断器是全局一个**。`reco-service` 的慢查询只影响「猜你喜欢」，却把「商品详情」主接口一起断了；其实应该按依赖隔离，详情页可以降级到缓存推荐，而不是整页失败。

> 🩸 **一句话**：熔断器只认「错」不认「慢」，等于给慢死的人判了「健康」。而慢，才是大多数系统真正的死因。

---

## 18.2 到底是什么：用状态机替下游「争取恢复时间」

**定义**：熔断 = 当对某个依赖的调用失败率/慢调用率超过阈值时，**主动停止调用**该依赖一段时间，让下游恢复，同时给上游一个明确的兜底路径；经过探测确认恢复后，再逐步放量。

四个关键词：

- **某个依赖**：熔断的粒度是「依赖」，不是「整个服务」。`reco` 挂了不该熔断 `payment`。
- **失败率/慢调用率**：不是单次失败，是滑动窗口内的比例。
- **停止一段时间**：OPEN 状态有冷却时间，不是永久断。
- **兜底路径**：熔断后必须有 fallback，否则只是把 500 换成另一种 500。

### 五个反例

| 你以为的熔断 | 为什么不是 | 不做会怎样 |
| --- | --- | --- |
| 重试 3 次 | 重试是「再试一次」，熔断是「先别试」 | 下游已经过载，重试把流量乘 3 |
| 超时 3s | 超时是单次调用的边界，熔断是跨调用的统计决策 | 每次调用都等 3s，线程照样耗尽 |
| 负载均衡的健康检查 | 健康检查管「这台机器活不活」，熔断管「这个依赖整体值不值得调」 | 下游全慢但都「健康」，流量继续打 |
| 限流 | 限流是主动控制入口流量，熔断是被动响应下游质量 | 限流挡不住「已放行的请求全部卡在慢下游」 |
| 有 fallback 就算熔断 | fallback 是熔断后的动作，不是触发条件 | 没有状态机，fallback 永远不会被触发 |

> 💡 **熔断与限流的分工**：限流是「我知道下游只能吃 1,000，所以只放 1,000 进来」；熔断是「我放了 1,000 进来，但下游 80% 都超时，我先别再放了，让它喘口气」。**限流是预防，熔断是止损。**

---

## 18.3 结构图：三态机与探测策略

```
                   失败率 ≥ 50% 或 慢调用率 ≥ 30%
                   且窗口内调用数 ≥ 20
        ┌──────────┐ ───────────────────────────────► ┌──────────┐
        │  CLOSED  │                                  │   OPEN   │
        │  正常放行 │ ◄─────────────────────────────── │  直接拒绝 │
        └──────────┘        探测成功（5 次中 ≥3）        └──────────┘
              ▲                                             │
              │                                             │ 冷却 30s
              │                                             ▼
              │                                      ┌────────────┐
              └───────────────────────────────────── │ HALF_OPEN  │
                  探测失败 → 回 OPEN（冷却翻倍）        │ 只放 5 个探针│
                                                      └────────────┘

  关键：HALF_OPEN 只能有一个「探测窗口」，不能 200 个线程同时冲进去
        否则下游刚恢复又被 200 个探针打死（半开风暴）
```

### 状态转换表

| 当前状态 | 触发条件 | 动作 | 下一状态 | 观测 |
| --- | --- | --- | --- | --- |
| CLOSED | 滑动窗口内失败率 ≥ 50% 且调用数 ≥ 20 | 记录快照，停止放行 | OPEN | `state=open`，记录触发时的失败率 |
| CLOSED | 慢调用率 ≥ 30% 且慢调用数 ≥ 10 | 同上 | OPEN | 记录 P99 与慢调用阈值 |
| OPEN | 冷却时间到（默认 30s） | 只放 `permittedNumberOfCallsInHalfOpenState` 个探针 | HALF_OPEN | `state=half_open` |
| HALF_OPEN | 探针成功数 ≥ 阈值（如 5 中 3） | 清空窗口，恢复放行 | CLOSED | `state=closed`，记录恢复耗时 |
| HALF_OPEN | 探针窗口结束（如 5 个）且失败率 ≥ 50%（或成功率 < 60%） | 重新打开，冷却翻倍（30→60→120s，上限 5min） | OPEN | `state=open`，记录退避时长 |
| 任意 | 人工强制 open/close | 写入审计，忽略自动转换 | 指定状态 | `manual_override=true` |

**探测策略的三种做法**：

| 策略 | 做法 | 优点 | 风险 |
| --- | --- | --- | --- |
| 单探针 | HALF_OPEN 只放 1 个请求 | 最安全 | 恢复慢；一个偶发失败就回 OPEN |
| 小批量探针 | 放 5~10 个，成功率 ≥ 60% 才恢复 | 平衡 | 探针太多会二次打崩 |
| 渐进放量 | 10% → 30% → 100%，每步观察 30s | 最稳 | 实现复杂，需要流量调度 |

> ⚠️ **半开风暴**：HALF_OPEN 状态下如果所有线程都认为「现在可以试」，200 个线程会同时发请求。必须用原子状态机保证「只有一个线程负责探测」，或者用令牌限制探针数量。

---

## 18.4 怎么实现：阈值、隔离与 8 条判据

### 错误率熔断 vs 慢调用熔断

| 维度 | 错误率熔断 | 慢调用熔断 |
| --- | --- | --- |
| 触发信号 | 5xx / 超时 / 连接失败占比 | 超过 `slowCallDurationThreshold` 的调用占比 |
| 典型阈值 | 失败率 ≥ 50%，窗口 ≥ 20 次 | 慢调用率 ≥ 30%，慢阈值 = P99 × 1.5 或业务超时 × 0.6 |
| 适用 | 下游明确报错、快速失败 | 下游变慢、线程/连接被拖死 |
| 误伤场景 | 下游某个接口 10% 报错，但其余 90% 正常；全局熔断会误杀 | 下游只是「长尾慢」，比如 5% 的报表查询本来就慢；阈值太低会频繁误熔断 |
| 关键参数 | `minimumNumberOfCalls` 太小 → 抖动；太大 → 反应慢 | `slowCallDurationThreshold` 必须小于上游超时，否则熔断前线程已经死 |
| 不配会怎样 | 错误率不高但全慢，熔断永不触发（18.1） | 偶发慢调用导致频繁开关，流量抖动 |

**慢调用阈值怎么定**：

```
slowCallDurationThreshold = min(上游超时 × 0.6, 下游 P99 × 1.5)
例：上游 3s 超时，下游 P99 300ms
  min(3s × 0.6, 300ms × 1.5) = min(1.8s, 450ms) = 450ms
慢调用率阈值 = 30%（窗口内至少 10 次慢调用）
```

### 熔断 vs 限流

| 维度 | 限流 | 熔断 |
| --- | --- | --- |
| 方向 | 主动，入口处 | 被动，调用处 |
| 依据 | 预设容量（QPS/并发） | 实时质量（失败率/慢调用率） |
| 目标 | 不让下游过载 | 让已经过载的下游恢复 |
| 触发后 | 拒绝/排队/降级 | 直接走 fallback，不发请求 |
| 恢复 | 按时间窗口自动 | 冷却 + 半开探测 |
| 典型误用 | 限流值拍脑袋 | 错误率阈值太低，频繁误熔断 |

**两者必须一起用**：限流保证「不超过下游容量」，熔断保证「下游质量下降时不再加码」。只有限流，下游一慢，已放行的请求还是会把线程占满；只有熔断，流量突增时熔断器会被打穿（它只统计已发生的调用）。

### 隔离方式：线程池 vs 信号量

| 维度 | 线程池隔离 | 信号量隔离 |
| --- | --- | --- |
| 机制 | 每个依赖一个独立线程池 + 有界队列 | 一个计数器，拿到许可才调用 |
| 并发上限 | 线程数 | 许可数 |
| 超时 | 可以用 `Future.get(timeout)` 主动取消 | 只能依赖下游调用的超时，取消能力弱 |
| 上下文传递 | 跨线程，ThreadLocal 会丢 | 同线程，上下文天然保留 |
| 资源成本 | 每线程约 1MB 栈 + 上下文切换；200 线程 ≈ 200MB 虚拟内存 | 一个整数，几乎零成本 |
| 隔离强度 | 强：一个依赖的线程池满了不影响其他依赖 | 弱：所有依赖共享业务线程，慢依赖会占满线程 |
| 适用 | 下游不可控、可能无限慢（第三方 API、模型供应商） | 下游可控、调用快、QPS 高（内部 RPC、缓存） |
| 典型实现 | Hystrix（已停更）、Resilience4j Bulkhead（线程池模式） | Resilience4j Bulkhead（信号量模式）、Sentinel 并发线程数 |

**结论**：Agent 调用模型供应商必须用**线程池/虚拟线程隔离 + 超时取消**，因为供应商可能 40s 不返回；内部 Redis/DB 调用用信号量就够，线程池反而浪费。

### 「强壮熔断」的 8 条判据

| # | 判据 | 合格线 | 不满足会怎样 |
| --- | --- | --- | --- |
| 1 | 误熔断占比 | ≤ 5%（等价于被拒请求中真实故障占比 ≥ 95%） | 健康下游被误杀，容量白丢 |
| 2 | 恢复探测 | HALF_OPEN 有明确探针数、成功率、退避上限 | 永远不恢复，或恢复即二次打崩 |
| 3 | 并发安全 | 状态转换用 CAS/原子变量，半开只有一个探测窗口 | 半开风暴，200 个探针同时打 |
| 4 | 指标完整 | state、calls、failures、slow、not_permitted、fallback 都有 | 出事时不知道熔断器为什么开 |
| 5 | 可手工干预 | 支持强制 open/close，带审计和过期时间 | 事故时只能改代码重启 |
| 6 | 按依赖隔离 | 每个 `provider:model` / `db:orders` 独立熔断器 | 一个推荐接口挂了，全站熔断 |
| 7 | 有兜底 | fallback 有数据、有超时、有降级标记 | 熔断后还是 500，只是快了一点 |
| 8 | 可观测 | 状态变化有日志/trace，能回答「为什么开」 | 复盘时只能猜 |

### Resilience4j：配置 + Java 代码

```yaml
resilience4j:
  circuitbreaker:
    instances:
      reco:
        slidingWindowType: COUNT_BASED
        slidingWindowSize: 100
        minimumNumberOfCalls: 20
        failureRateThreshold: 50
        slowCallRateThreshold: 30
        slowCallDurationThreshold: 2s
        permittedNumberOfCallsInHalfOpenState: 5
        waitDurationInOpenState: 30s
        automaticTransitionFromOpenToHalfOpenEnabled: true
        recordExceptions:
          - java.io.IOException
          - java.util.concurrent.TimeoutException
        ignoreExceptions:
          - com.example.BusinessException
  bulkhead:
    instances:
      llmProvider:
        maxConcurrentCalls: 200
        maxWaitDuration: 10ms
```

```java
import io.github.resilience4j.circuitbreaker.CallNotPermittedException;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import io.github.resilience4j.circuitbreaker.CircuitBreakerRegistry;
import org.springframework.stereotype.Service;

import java.util.function.Supplier;

@Service
public class RecoClient {

    private final CircuitBreaker breaker;
    private final RecoFallback fallback;

    public RecoClient(CircuitBreakerRegistry registry, RecoFallback fallback) {
        this.breaker = registry.circuitBreaker("reco");
        this.fallback = fallback;
    }

    public RecoResult query(String userId) {
        Supplier<RecoResult> call = CircuitBreaker.decorateSupplier(
                breaker, () -> doQuery(userId));
        try {
            return call.get();
        } catch (CallNotPermittedException e) {
            // 熔断打开：走缓存兜底，并打降级标记
            return fallback.cached(userId).withDegraded("reco_open");
        }
    }

    private RecoResult doQuery(String userId) {
        // 真实调用，超时 2s；慢调用由 Resilience4j 统计
        return httpClient.get("/reco?uid=" + userId, RecoResult.class);
    }
}
```

### Sentinel：慢调用比例熔断

```java
import com.alibaba.csp.sentinel.slots.block.degrade.DegradeRule;
import com.alibaba.csp.sentinel.slots.block.degrade.DegradeRuleManager;
import com.alibaba.csp.sentinel.slots.block.degrade.circuitbreaker.CircuitBreakerStrategy;

import java.util.List;

public class SentinelDegrade {

    public void init() {
        DegradeRule rule = new DegradeRule("reco")
                .setGrade(CircuitBreakerStrategy.SLOW_REQUEST_RATIO.getType())
                .setCount(2000)                 // 慢调用阈值：2000ms
                .setSlowRatioThreshold(0.3)     // 慢调用比例 30%
                .setMinRequestAmount(20)        // 至少 20 次才统计
                .setStatIntervalMs(10_000)      // 10s 统计窗口
                .setTimeWindow(30);             // 熔断 30s
        DegradeRuleManager.loadRules(List.of(rule));
    }
}
```

> ⚠️ Sentinel 的 `count` 在慢调用比例模式下是「慢调用 RT 阈值」，`slowRatioThreshold` 才是比例；不同版本文档有差异，以 1.8.x 官方文档为准。上线前用单测验证一次「慢调用 30% 是否触发」。

### Go：gobreaker + 慢调用包装

```go
package reco

import (
	"context"
	"errors"
	"time"

	"github.com/sony/gobreaker/v2"
)

var ErrSlowCall = errors.New("reco slow call")

type Client struct {
	cb *gobreaker.CircuitBreaker[Result]
}

func NewClient() *Client {
	return &Client{cb: gobreaker.NewCircuitBreaker[Result](gobreaker.Settings{
		Name:        "reco",
		MaxRequests: 5,               // HALF_OPEN 最多 5 个探针
		Interval:    30 * time.Second, // CLOSED 统计窗口
		Timeout:     30 * time.Second, // OPEN 冷却
		ReadyToTrip: func(c gobreaker.Counts) bool {
			if c.Requests < 20 {
				return false
			}
			failRatio := float64(c.TotalFailures) / float64(c.Requests)
			return failRatio >= 0.5
		},
		OnStateChange: func(name string, from, to gobreaker.State) {
			// 打点：reco_breaker_state{from,to}
		},
	})}
}

func (c *Client) Query(ctx context.Context, uid string) (Result, error) {
	start := time.Now()
	res, err := c.cb.Execute(func() (Result, error) {
		r, err := doQuery(ctx, uid)
		if err == nil && time.Since(start) > 2*time.Second {
			return Result{}, ErrSlowCall // 慢调用按失败计入熔断统计
		}
		return r, err
	})
	if err != nil {
		return Result{}, err
	}
	return res, nil
}
```

gobreaker 本身不统计慢调用，所以要用 `ErrSlowCall` 把「慢」翻译成「失败」。**这是所有熔断器的通用技巧：状态机只认 success/failure，慢调用要在包装层变成 failure。**

### Agent 侧：按模型/供应商熔断 + 成本熔断

| 熔断器 | 键 | 触发 | 动作 |
| --- | --- | --- | --- |
| 供应商熔断 | `provider:openai` | 错误率 ≥ 30% 或慢调用率 ≥ 40%（>10s） | failover 到备用供应商 |
| 模型熔断 | `provider:model:gpt-4o` | 错误率 ≥ 20% 或 P99 > 15s | 切小模型/备用模型 |
| 工具熔断 | `tool:search` | 错误率 ≥ 50% 或 P99 > 3s | 跳过工具，让模型用已有信息回答 |
| 成本熔断 | `run_id` | 单 Run 花费 > $0.50 | 中断 Run，返回部分结果 + `budget_exceeded` |
| 租户预算熔断 | `tenant_id` | 日花费 > 预算 90% | 降级小模型 / 拒绝新 Run |

成本熔断代码（Java，原子累加，单 Run 内无锁）：

```java
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;

public final class CostBreaker {
    private final long limitMicroUsd;
    private final AtomicLong spentMicroUsd = new AtomicLong();
    private final AtomicBoolean tripped = new AtomicBoolean();

    public CostBreaker(long limitMicroUsd) {
        this.limitMicroUsd = limitMicroUsd;
    }

    /** 返回 false 表示已超预算，调用方必须中断 Run */
    public boolean charge(long deltaMicroUsd) {
        long now = spentMicroUsd.addAndGet(deltaMicroUsd);
        if (now > limitMicroUsd) {
            tripped.set(true);
            return false;
        }
        return true;
    }

    public boolean tripped() { return tripped.get(); }
    public long spentMicroUsd() { return spentMicroUsd.get(); }
}
```

算例：单 Run 预算 $0.50，模型输入 $2.5/M token、输出 $10/M token：

```
第 1 步：20,000 输入 + 2,000 输出
  = 20,000 × 2.5/1,000,000 + 2,000 × 10/1,000,000
  = $0.05 + $0.02 = $0.07
第 8 步：8 × $0.07 = $0.56 > $0.50 → 第 8 步调用前中断
返回：已有 7 步结果 + "预算已用尽" + 建议缩小任务
```

> 🩸 **成本熔断不是「省钱」，是「防止一个失控的 Agent 循环烧掉一个月预算」**。模型可能陷入工具调用死循环，10 分钟烧 $200；成本熔断是最后一道闸。

---

## 18.5 双修对照：熔断的是依赖，不是「服务」

| 维度 | 传统系统 | Agent 系统 | 同 or 异 |
| --- | --- | --- | --- |
| 机制本体 | 三态机、滑动窗口、半开探测 | **完全相同**，Resilience4j/gobreaker 可直接用 | **同** |
| 身份/唯一键 | `db:orders`、`http:reco` | `provider:model`、`tool:search`、`run_id` | 同构，多一层模型/供应商 |
| 熔断对象 | 下游服务、DB、第三方 API | 供应商、模型、工具、**单 Run 成本** | 同 + 成本维度 |
| 失败信号 | 5xx、超时、连接拒绝 | 429、5xx、慢调用（首 token 慢）、内容质量失败 | 多一类「慢而不错」 |
| 恢复方式 | 半开探测 + 缓存兜底 | 半开 + failover 到备用供应商 + 降级小模型 | 同 + 换模型 |
| 验证方法 | 延迟注入、错误注入 | 同 + **成本注入**（故意跑长 Run） | 同 + 成本验证 |
| 观测指标 | state、failure_rate、slow_rate | 加 `tokens_per_run`、`run_cost_usd`、`provider_quota_remaining` | 指标集扩展 |
| 成本结构 | 机器折旧，熔断省不了多少钱 | **每次调用都计费**；熔断直接省钱，成本熔断是刚需 | **异**（根因：成本显性且按量） |

**哪些完全相同、可以直接复用**：三态机、滑动窗口、错误率/慢调用率阈值、半开探针、线程池/信号量隔离、Resilience4j/gobreaker 的代码。**熔断器不知道也不关心被熔断的是数据库还是大模型。**

**差异根因**：Agent 的「失败」多两类——**慢而不错**（模型 40s 才返回，HTTP 200）和**贵而不错**（回答正确但花了 $3）。所以 Agent 的熔断器要接三个信号：

```
错误率（传统）
慢调用率（传统，但阈值要按首 token / 总时长分开）
单位成本（Agent 特有：$/1k token、$/成功 Run）
```

Agent 侧的度量：`TPM` 决定供应商配额是否够；`并发 Run` 决定线程/连接池；`单 Run 预算` 决定成本熔断阈值；`供应商配额` 决定 failover 的目标容量。四个数缺一个，熔断策略就是拍脑袋。

---

## 18.6 会在哪炸：失效模式清单

| 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 下游全慢，熔断器不打开 | 只配错误率，没配慢调用 | 看下游 P99 与 breaker state | 加慢调用率阈值 | 每个依赖必须同时配错误率 + 慢调用率 |
| 熔断频繁开关，流量抖动 | `minimumNumberOfCalls` 太小 / 窗口太短 | 看 state 变化频率 | 提高最小调用数、加长窗口、加退避 | 熔断器变更要走压测 |
| 一个接口挂，全站熔断 | 全局一个 breaker | 看 breaker 的 key 粒度 | 按依赖/租户隔离 | 代码评审检查 breaker 命名 |
| 半开时下游又被打崩 | 所有线程同时探测 | 看 HALF_OPEN 期间 QPS 尖刺 | 限制探针数 + CAS 状态转换 | 半开并发单测 |
| 熔断后仍然 500 | fallback 没数据/也超时 | 看 fallback 的 P99 与错误率 | 预置缓存、fallback 超时更短 | 演练时只开熔断、不杀下游 |
| 手工强制关闭后忘了恢复 | 没有审计/过期 | 看 `manual_override` 指标 | 强制状态带 TTL + 审计 | 变更单 + 自动提醒 |
| Agent 成本失控 | 没有单 Run 预算 | 看 `run_cost_usd` 分布 | 成本熔断 + 租户日预算 | 每个 Run 启动时创建 CostBreaker |
| 熔断器指标缺失 | 只打日志，没打 state | 看 dashboard 有没有 state | 接 Micrometer/Prometheus | 上线检查清单 |

---

## 18.7 怎么验证：延迟注入、错误注入、成本注入

**① 延迟注入（Chaos Mesh）：**

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: reco-delay
spec:
  action: delay
  mode: one
  selector:
    namespaces: [prod]
    labelSelectors:
      app: reco-service
  delay:
    latency: "8s"
    correlation: "100"
    jitter: "500ms"
  duration: "5m"
```

期望：`reco` 的慢调用率在 30 秒内 > 30%，breaker 进入 OPEN；`product-page` 的 P99 不超过 1.5s（走 fallback），线程池利用率 < 70%。

**② 错误注入**：用故障开关让下游返回 50% 的 503，验证错误率熔断；再返回 5% 的 503，验证**不会**误熔断。

**③ 成本注入**：构造一个会循环调用工具 20 次的 Run，预算 $0.50，验证第 8 步左右中断，且返回部分结果。

**④ 必看指标与 PromQL：**

```promql
# 熔断器状态（1=open, 0.5=half_open, 0=closed，具体以 exporter 为准）
max by (name, state) (resilience4j_circuitbreaker_state)

# 失败率
sum(rate(resilience4j_circuitbreaker_calls_seconds_count{kind="failed"}[1m]))
  by (name)
/
sum(rate(resilience4j_circuitbreaker_calls_seconds_count[1m])) by (name)

# 被熔断拒绝的请求
sum(rate(resilience4j_circuitbreaker_not_permitted_calls_total[1m])) by (name)

# 慢调用率 = 1 - ≤2s 的占比（慢阈值按业务定，这里以 2s 为例）
1 - (
  sum(rate(http_client_duration_seconds_bucket{le="2",service="reco"}[1m]))
  /
  sum(rate(http_client_duration_seconds_count{service="reco"}[1m]))
)
```

**⑤ 单测**：用假时钟推进 `waitDurationInOpenState`，验证 CLOSED → OPEN → HALF_OPEN → CLOSED 的完整转换；并发 200 个 goroutine 同时调用，验证只有 5 个探针进入 HALF_OPEN。

---

## 18.8 用什么工具

| 场景 | 首选 | 理由 / 坑 |
| --- | --- | --- |
| Java/Spring | Resilience4j | 轻量、函数式、指标完整；Hystrix 已停更，别再用 |
| Java 全链路 | Sentinel | 流控 + 熔断 + 热点 + 控制台；慢调用比例要确认版本语义 |
| Go | `sony/gobreaker/v2` | 简单、并发安全；慢调用要自己包装 |
| 服务网格 | Envoy outlier detection | 基础设施层熔断，不用改代码；粒度较粗，配置复杂 |
| Agent 模型网关 | 自研 breaker + 成本熔断 | 需要按 provider/model/run 三个维度，现成库不覆盖成本 |
| 压测/故障注入 | Chaos Mesh / Toxiproxy | 延迟、错误、断网都能模拟 |

**自研边界**：三态机、滑动窗口用成熟库；**必须自研的是「熔断键的粒度」和「fallback 的业务语义」**——这两件事框架替不了你。

---

## 18.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 定义 | 熔断 = 用状态机在下游质量下降时停止调用，争取恢复时间 |
| 三态 | CLOSED → OPEN → HALF_OPEN → CLOSED；OPEN 有冷却，HALF_OPEN 有探针 |
| 慢调用 | 只配错误率会漏掉「慢而不错」；慢阈值 = min(上游超时×0.6, 下游 P99×1.5) |
| 熔断 vs 限流 | 限流主动预防，熔断被动止损；两者必须一起用 |
| 线程池隔离 | 强隔离、可超时取消、成本高；适合不可控的第三方/模型 |
| 信号量隔离 | 轻量、同线程、取消弱；适合内部快调用 |
| 8 条判据 | 误熔断率、恢复探测、并发安全、指标、手工干预、按依赖隔离、有兜底、可观测 |
| 半开风暴 | 必须限制探针数，状态转换用 CAS |
| Agent | 按 provider/model/tool 熔断 + 单 Run 成本熔断 |
| 成本熔断算例 | 20k 输入 + 2k 输出 = $0.07/步；$0.50 预算第 8 步中断 |
| 验证 | 延迟注入、错误注入、成本注入、半开并发单测 |

---

## 18.10 自检题

1. **下游 P99 从 300ms 涨到 8s，但错误率只有 2%，你的错误率熔断器为什么不触发？怎么改？**
   <details><summary>答案要点</summary>
   因为错误率熔断只看 5xx/超时占比，8s 的 200 响应不算失败。改法：加慢调用率熔断，`slowCallDurationThreshold = min(上游超时×0.6, 下游 P99×1.5)`，例如 450ms；慢调用率阈值 30%，窗口 ≥ 10 次慢调用。同时把上游超时压到 3s 以内，避免线程被 8s 占死。
   </details>

2. **为什么 HALF_OPEN 不能一次放全部流量？你怎么保证只有一个探测窗口？**
   <details><summary>答案要点</summary>
   下游刚恢复，容量可能还没回来；一次放全量会二次打崩，形成「熔断—恢复—再熔断」的抖动。保证方法：① 只放 `permittedNumberOfCallsInHalfOpenState` 个探针（如 5 个）；② 状态转换用 CAS/原子变量，只有一个线程能把 OPEN 改成 HALF_OPEN；③ 探针成功后按 10%→30%→100% 渐进放量。
   </details>

3. **线程池隔离和信号量隔离，分别用在你系统的哪个依赖上？为什么？**
   <details><summary>答案要点</summary>
   模型供应商/第三方 API 用线程池（或虚拟线程）隔离：它们可能 40s 不返回，必须能超时取消，且不能占满业务线程；内部 Redis/DB 快调用用信号量：调用短、QPS 高，线程池的 1MB/线程和上下文切换不划算。判断标准：**下游是否可能无限慢 + 是否需要主动取消**。
   </details>

---

## 18.11 延伸阅读

- Resilience4j 官方文档：CircuitBreaker 的 `slidingWindowType`、`slowCallRateThreshold`、`permittedNumberOfCallsInHalfOpenState`
- Sentinel `DegradeRule` 与 `CircuitBreakerStrategy`，以 1.8.x 文档为准
- `sony/gobreaker` 源码：`ReadyToTrip`、`Counts`、`OnStateChange`
- Google SRE Book：*Addressing Cascading Failures* 中的熔断与重试预算
- 上一章：[第 17 章 限流自身的故障：当限流把 CPU 打满](17-rate-limit-self-failure.md)
- 下一章：[第 19 章 降级与兜底设计](19-degradation-design.md)

**交叉引用**：总纲 [0-summary.md](0-summary.md) ｜ 语言与运行时 [../go/0-summary.md](../go/0-summary.md) ｜ 平台治理 [../llm/0-summary.md](../llm/0-summary.md)
