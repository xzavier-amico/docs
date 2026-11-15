# 17. 限流自身的故障：当限流把 CPU 打满

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 流量与韧性：不崩是怎么做到的 ｜ **编号**：17 / 52
> **本章定位**：限流器本来是「保住系统」的，但它自己也是一个服务：要 CPU、要网络、要日志、要指标、要热更新。本章用一次 12 分钟的全站雪崩，把「保护者变成凶手」的完整链条拆到秒，再给出六个解法与一个比限流更重要的开关——限流器自己的降级开关。
> **读法**：§17.3 的时间线建议逐行读，每一行都是一个可观测信号；§17.5 的六段代码是生产可抄的最小集；如果你只记一句话，记 §17.6：**限流器故障时「全放行」还是「固定阈值」，必须在事故前写死。**

---

## 17.1 引子：520 大促，限流器先死，全站跟着死

2026-05-20 20:00，某电商「520」大促开场。核心链路 `promo-gateway` 有 **40 个 Pod**，每个 Pod 的 CPU limit 是 **1 核**，平时 QPS 6,000，P99 120ms。

限流方案是「标准」的集中式 Redis + Lua：每个请求进来，先调一次 `EVALSHA`，判定通过才继续。Redis 集群 6 分片，限流 key 约 20 万个，平时 Lua P99 1.2ms，Redis CPU 32%。日志同步打，指标每个请求记一次 Histogram，规则每 30 秒全量推一次。

20:00 流量开始爬坡，20:05 全站错误率 68%，20:05:30 值班同学做的第一件事是「把限流关掉」——错误率反而冲到 **91%**。20:06 关掉同步日志和逐请求指标，20:06:30 切到本地固定阈值 8,000 QPS，20:12 恢复。

**12 分钟，约 1,440 万次请求，510 万次失败，GMV 损失约 620 万元，38 万用户受影响。**

事后复盘，最扎心的一句是：

> 「我们一直以为限流是保险丝。结果保险丝自己先烧了，而且烧的时候还把整栋楼的电闸带跳了。」

---

## 17.2 到底是什么：限流器也是一个「有成本的服务」

**定义**：限流自身的故障，指限流组件在高压下消耗了过多资源（CPU、线程、连接、磁盘 IO），导致**它保护的业务还没被流量打死，先被它的判定逻辑拖死**。

它和「限流值配错」是两件事：

| 你以为的限流故障 | 实际是什么 | 区别 |
| --- | --- | --- |
| 限流值太小，误杀用户 | 策略错误 | 改配置即可，不涉及性能 |
| 限流值太大，下游被打穿 | 容量计算错误 | 第 15 章的问题 |
| 限流器 QPS 太高，自己 CPU 100% | **本章的问题** | 限流器成了新的瓶颈 |
| 限流器挂了，全站 429 | 可用性设计错误 | 本章 §17.6 的降级开关 |
| 限流规则推错了，全部拒绝 | 变更问题 | 第 19 章的功能开关 |

三个反例，帮你确认「这不是本章要讲的东西」：

- **「限流器是无状态的，怎么会挂？」** 无状态不等于无成本。每请求一次 Redis 往返、一次日志、一次 Histogram，都是成本。
- **「加机器就行了。」** 限流器的瓶颈常常在 Redis 单分片、在全局锁、在日志磁盘，不在 Pod 数。
- **「限流器挂了就放行，业务还能活。」** 放行之后下游没有硬墙，业务死得更快；放行必须有本地阈值兜底。

> 💡 **三定律之二在这里的体现**：限流器保护的是下游的稀缺资源，但它自己也要消耗稀缺资源（CPU、Redis 连接、日志 IO）。**当保护成本接近保护收益时，限流器本身就必须被限流、被熔断、被降级。**

---

## 17.3 结构图与 12 分钟时间线

### 故障链

```
流量 6k → 18k
   │
   ▼
每请求 1 次 Redis EVALSHA（18,000 次/秒）
   │
   ├──► Redis 单分片 CPU 63% → 91% → 100%
   │         │
   │         ▼
   │    Lua P99 1.2ms → 45ms → 480ms
   │         │
   │         ▼
   ├──► 网关线程阻塞在限流调用上（每请求多 45~480ms）
   │         │
   │         ▼
   ├──► 同步日志 18,000 行/秒 + 每请求 Histogram（360,000 次原子操作/秒）
   │         │
   │         ▼
   ├──► Pod CPU 100%，cgroup 节流 73%
   │         │
   │         ▼
   ├──► 请求排队 → 客户端 3s 超时 → 重试 1.4 倍 → 入口 34,000 QPS
   │         │
   │         ▼
   └──► 限流器超时后 fail-open → 下游 DB 连接池打满 → 全站 5xx
```

### 时间线（精确到秒）

| 时间 | 入口 QPS | 放行 / 拒绝 | Pod CPU / 节流 | P99 | Redis CPU / RTT | 排队 | 错误率 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 20:00:00 | 6,000 | 6,000 / 0 | 28% / 0 | 120ms | 32% / 0.9ms | 0 | 0.1% |
| 20:00:30 | 7,800 | 7,800 / 0 | 36% / 0 | 135ms | 41% / 1.0ms | 0 | 0.1% |
| 20:01:00 | 9,600 | 9,600 / 0 | 45% / 0 | 160ms | 51% / 1.3ms | 120 | 0.2% |
| 20:01:30 | 12,000 | 11,200 / 800 | 58% / 0 | 210ms | 64% / 2.1ms | 380 | 0.5% |
| 20:02:00 | 15,000 | 12,900 / 2,100 | 71% / 0 | 380ms | 79% / 6ms | 900 | 1.4% |
| 20:02:30 | 18,000 | 14,400 / 3,600 | 86% / 0 | 900ms | 93% / 21ms | 2,100 | 3.8% |
| 20:03:00 | 21,000 | 16,200 / 4,800 | 96% / 18% | 1.8s | 100% / 65ms | 4,800 | 8.2% |
| 20:03:30 | 26,000 | 19,000 / 7,000 | 100% / 42% | 3.2s | 100% / 180ms | 9,500 | 22% |
| 20:04:00 | 31,000 | 22,500 / 8,500 | 100% / 61% | 5.8s | 100% / 310ms | 15,000 | 41% |
| 20:04:30 | 33,000 | 24,000 / 9,000 | 100% / 73% | 9.4s | 100% / 480ms | 20,000 | 57% |
| 20:05:00 | 34,000 | 25,000 / 9,000 | 100% / 78% | 12s | 100% / 620ms | 24,000 | 68% |
| 20:05:30 | 34,000 | 34,000 / 0（关限流） | 100% / 82% | 15s | 100% / 700ms | 27,000 | **91%** |
| 20:06:00 | 32,000 | 24,000 / 8,000（恢复限流 + 关日志/指标） | 84% / 31% | 11s | 100% / 650ms | 22,000 | 84% |
| 20:06:30 | 30,000 | 8,000 / 22,000（本地固定阈值） | 72% / 0 | 8.4s | 34% / 2ms | 17,000 | 71% |
| 20:07:00 | 28,000 | 8,000 / 20,000 | 68% / 0 | 6.8s | 31% / 1.5ms | 12,000 | 58% |
| 20:07:30 | 26,000 | 8,000 / 18,000 | 64% / 0 | 5.2s | 30% / 1.2ms | 8,500 | 44% |
| 20:08:00 | 24,000 | 8,000 / 16,000 | 60% / 0 | 3.9s | 30% / 1.1ms | 6,000 | 32% |
| 20:09:00 | 21,000 | 8,000 / 13,000 | 55% / 0 | 2.1s | 30% / 1.0ms | 3,000 | 18% |
| 20:10:00 | 18,000 | 8,000 / 10,000 | 50% / 0 | 900ms | 30% / 0.9ms | 800 | 7% |
| 20:11:00 | 15,000 | 8,000 / 7,000 | 45% / 0 | 380ms | 30% / 0.9ms | 120 | 2.1% |
| 20:12:00 | 12,000 | 8,000 / 4,000 | 40% / 0 | 150ms | 30% / 0.9ms | 0 | 0.6% |

> 🩸 **注意 20:05:30 这一行**：关掉限流后，入口 34,000 QPS 全部放行，错误率从 68% 冲到 91%。这说明当时的问题**不是限流挡了请求，而是限流器自己太慢，且超时后 fail-open**。值班同学关限流的直觉是对的——错在没有同时打开本地阈值和下游熔断。

### 把 CPU 账算出来

正常时每个请求的 CPU 成本约 **1.8ms**：

```
6,000 QPS / 40 Pod = 150 QPS/Pod
150 × 1.8ms = 270ms CPU/秒 = 27% of 1 core
```

到 18,000 QPS 时，每个请求的 CPU 涨到 **2.1ms**（限流链路变长），于是：

```
18,000 / 40 = 450 QPS/Pod
450 × 2.1ms = 945ms CPU/秒 = 94.5% of 1 core
```

**2.1ms 的构成**（这就是六个根因的账单）：

| 成本项 | 每请求耗时 | 450 QPS/Pod 时占用 | 占比 |
| --- | --- | --- | --- |
| Redis 调用（序列化 + 网络 + 等 Lua） | 0.7ms | 315ms/s | 33% |
| 限流统计锁竞争 | 0.4ms | 180ms/s | 19% |
| 同步日志打点 | 0.3ms | 135ms/s | 14% |
| Histogram 指标聚合 | 0.2ms | 90ms/s | 10% |
| 规则查找 / 其他 | 0.2ms | 90ms/s | 10% |
| 业务逻辑 | 0.3ms | 135ms/s | 14% |
| **合计** | **2.1ms** | **945ms/s** | **100%** |

> 💡 **关键结论**：限流器本身占了 **70%** 的 CPU（0.7+0.4+0.3+0.2+0.2 = 1.8ms），业务只占 14%。当你把限流器做到「每请求一次远程调用 + 一次日志 + 一次指标」，它就不再是旁路，而是主业务。

---

## 17.4 六个根因：每一个都能单独打满 CPU

### 根因一：每请求一次 Redis 往返

18,000 QPS × 1 次 `EVALSHA` = **18,000 次 Redis 往返/秒**。Lua 本身 35µs：

```
18,000 × 35µs = 630ms CPU/秒 = 63% of one Redis core
```

到 26,000 QPS 时是 91%，加上网络收包、协议解析、AOF，Redis 单分片直接 100%。Redis 是单线程执行命令，**一个热 key 就能把整个分片拖慢**；同分片上的其他 key 跟着遭殃。

### 根因二：没有本地预取

100% 的请求都走远程。如果做租约（第 16 章），租约 100：

```
Redis 调用量 = 18,000 / 100 = 180 次/秒
180 × 35µs = 6.3ms CPU/秒 = 0.63% of one core
```

**同样的限流精度（超发 ≤ 100×40 = 4,000，约 22%），Redis 压力降 100 倍。** 这不是优化，这是架构选择。

### 根因三：限流统计锁竞争

出事代码长这样：

```java
// 反面教材：全局锁 + 每请求更新
private final Map<String, Counter> counters = new HashMap<>();

public boolean allow(String key) {
    synchronized (counters) {                    // ← 450 QPS/Pod 全排这里
        Counter c = counters.computeIfAbsent(key, k -> new Counter());
        c.total++;
        if (c.total > limit) return false;
        c.passed++;
        return true;
    }
}
```

450 QPS/Pod，每次持锁 0.4ms（含 `computeIfAbsent`、对象分配、计数），锁利用率 = 450 × 0.4ms = **18%**；200 个工作线程争一把锁，P99 等待 12ms。**CPU 没满，线程先排满。**

### 根因四：同步日志打点

每个请求一行结构化日志，350 字节：

```
18,000 行/秒 × 350B = 6.3MB/s
40 Pod 平均每个 157KB/s
```

同步 appender 写磁盘 + JSON 序列化约 0.3ms/行：

```
450 × 0.3ms = 135ms CPU/秒 = 13.5% of one core
```

更糟的是磁盘 IO 抖动：一次 fsync 卡 200ms，整条日志队列阻塞，业务线程跟着等。

### 根因五：每请求打 Histogram

每个请求调用一次 `Timer.record(...)`，Micrometer 的 percentile histogram 有 20 个桶：

```
18,000 × 20 = 360,000 次桶计数/秒
每个 Pod：450 × 20 = 9,000 次/秒
```

每次 `record` 不是纯原子加，还要查标签、找 meter、可能加锁。0.2ms/请求，**9% CPU**；如果标签是高基数（`tenant_id`、`user_id`），meter 注册表本身会成为锁热点。

### 根因六：规则热更新全量刷新

规则中心每 30 秒推一次全量 JSON，2MB，4 万条规则。客户端收到后：

```
JSON 解析 2MB：约 120ms
重建 4 万条规则的 Map：约 180ms
合计 300ms CPU，且期间持有规则锁
```

平均 CPU 只有 1%，但**尾延迟致命**：20:02:10 的一次刷新正好撞上流量爬坡，规则锁持有 1.2 秒，期间 450 QPS/Pod × 1.2s = 540 个请求排队；40 个 Pod 同时刷新，队列瞬间 +2 万。**这不是「平均 CPU」问题，是「stop-the-world」问题。**

---

## 17.5 双修对照：限流器故障，传统与 Agent 是同一场病

| 维度 | 传统系统 | Agent 系统 | 同 or 异 |
| --- | --- | --- | --- |
| 机制本体 | 本地桶、租约、规则快照、限流器熔断 | **完全相同**，代码可直接复用 | **同** |
| 身份/唯一键 | `tenant:route` | `tenant:provider:model` + `run_id` + `step_id` | 同构，多一层模型/供应商 |
| 限的是什么资源 | Redis 连接、Pod CPU、日志 IO | 加：**tokenizer CPU、TPM 配额、单 Run 预算** | **异**（三定律之二：换资源） |
| CPU 杀手 | 每请求 Redis、锁、日志、Histogram、规则刷新 | 上述全部 + **每请求估 token** | 多一个 |
| 失败形态 | 限流 P99 升高 → 线程排队 → 全站超时 | 同 + **Run 预算被慢限流吃光**，用户看到「转圈到超时」 | 同构，后果更贵 |
| 恢复方式 | 本地固定阈值 + 关日志/指标 | 同 + 降级小模型 + 关工具调用（第 19 章） | 同 + 换智能 |
| 验证方法 | 2× 压测、Redis 延迟注入、规则推送风暴 | 同 + **token 估算误差对账** | 同 + 一层对账 |
| 观测指标 | 限流器 CPU、P99、Redis RTT | 加 `tokens_estimated`、`estimate_error`、`run_cost_usd`、`provider_quota_remaining` | 指标集扩展 |
| 成本结构 | 机器折旧，近似固定 | **每次判定都可能烧钱**；估错 token 会超预算 | **异**（根因：成本显性且按量） |

**哪些完全相同、可以直接复用**：

- 本地令牌桶 / GCRA 的无锁实现；
- 租约预取与批量校验；
- `LongAdder` / `atomic.Int64` 计数；
- 采样日志、采样指标；
- 规则快照 + `atomic.Value` / `AtomicReference` 原子替换；
- **熔断限流器自己**的健康检查与降级开关。

**差异根因只有一个**：传统限流的「一次请求」是等价的，扣 1 个令牌就行；Agent 的「一次请求」要先**估 token**，而估算本身要跑 tokenizer、要缓存、要校准。这个差异会把限流器的 CPU 成本再放大 2~5 倍——详见 §17.7。

Agent 侧的度量必须同时看四个数：

```
TPM = 每分钟消耗的 token 数（不是请求数）
并发 Run = 同时在跑的 Run 数（不是 QPS）
单 Run 预算 = $ / token 上限（超了必须中断）
供应商配额 = RPM / TPM / 并发（三家供应商分别算）
```

例：60 Run/s，平均 3 步，每步 2,500 token：

```
TPM = 60 × 3 × 2,500 × 60 = 27,000,000 TPM
如果供应商配额是 2,000,000 TPM → 需要 14 个供应商配额池，或者把 Run 速率降到 4.4/s
```

> ⚠️ **不要用 QPS 限 Agent**：60 Run/s 看起来很小，但 27M TPM 已经超过大多数供应商的单租户配额。限流键必须是 `tenant:provider:model`，单位必须是 token。

---

## 17.6 六个解法：把限流器从主业务变回旁路

### 解法一：本地令牌桶 + 异步续期（把 Redis 从 18,000 降到 180）

Go 的无锁令牌桶（单指针 CAS，避免锁和竞态）：

```go
package ratelimit

import (
	"sync/atomic"
	"time"
)

const tokenUnit int64 = 1_000_000 // 微令牌，避免浮点

type bucketState struct {
	tokens int64 // 微令牌
	last   int64 // UnixNano
}

type LockFreeBucket struct {
	state atomic.Pointer[bucketState]
	rate  int64 // 微令牌/秒
	burst int64
}

func NewLockFreeBucket(ratePerSec, burst int) *LockFreeBucket {
	b := &LockFreeBucket{
		rate:  int64(ratePerSec) * tokenUnit,
		burst: int64(burst) * tokenUnit,
	}
	b.state.Store(&bucketState{tokens: b.burst, last: time.Now().UnixNano()})
	return b
}

// Allow 无锁：读快照 → 算补充 → CAS 换新快照
func (b *LockFreeBucket) Allow(now time.Time) bool {
	nowN := now.UnixNano()
	for {
		old := b.state.Load()
		elapsed := nowN - old.last
		if elapsed < 0 {
			elapsed = 0 // 单调时钟回拨保护
		}
		tokens := old.tokens + elapsed*b.rate/int64(time.Second)
		if tokens > b.burst {
			tokens = b.burst
		}
		if tokens < tokenUnit {
			// 拒绝也要推进 last，避免下一请求重复计算同一段时间
			next := &bucketState{tokens: tokens, last: nowN}
			if b.state.CompareAndSwap(old, next) {
				return false
			}
			continue
		}
		next := &bucketState{tokens: tokens - tokenUnit, last: nowN}
		if b.state.CompareAndSwap(old, next) {
			return true
		}
	}
}
```

> 💡 每次 CAS 会分配一个 16 字节的 `bucketState`。450 QPS/Pod 时 GC 压力可忽略；如果到 50k QPS，用 `sync.Pool` 或按 key 分片成 16 个桶。**先测量，再优化，别一上来就上分片。**

Java 的无锁 GCRA（令牌桶的等价形式）+ `LongAdder` 计数：

```java
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.LongAdder;

public final class LockFreeGcra {
    private final AtomicLong tat;          // theoretical arrival time
    private final long intervalNanos;
    private final long burstNanos;
    private final LongAdder allowed = new LongAdder();
    private final LongAdder rejected = new LongAdder();

    public LockFreeGcra(int ratePerSec, int burst) {
        this.intervalNanos = 1_000_000_000L / ratePerSec;
        this.burstNanos = intervalNanos * burst;
        this.tat = new AtomicLong(System.nanoTime());
    }

    public boolean tryAcquire() {
        long now = System.nanoTime();
        for (;;) {
            long old = tat.get();
            long base = Math.max(now, old);
            long next = base + intervalNanos;
            if (next - burstNanos > now) {   // 超过 burst 容忍
                rejected.increment();
                return false;
            }
            if (tat.compareAndSet(old, next)) {
                allowed.increment();
                return true;
            }
        }
    }

    public long allowed()  { return allowed.sum(); }
    public long rejected() { return rejected.sum(); }
}
```

**GCRA 与令牌桶等价**：`tat` 是「理论到达时间」，每放行一个请求就往后推 `1/rate`；`burstNanos` 是允许的提前量，就是桶容量。一次 CAS 完成判定，`LongAdder` 只做统计，不参与判定——**统计的竞争不应该拖慢判定**。

异步续期用后台协程，不在请求路径上做：

```go
// 每 500ms 检查本地桶余量，低于 30% 时向 Redis 领租约
func (l *LeaseLimiter) renewLoop(ctx context.Context) {
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if l.local.Remaining() < 0.3*float64(l.lease) {
				got, err := l.remote.Take(ctx, l.key, l.lease)
				if err == nil && got > 0 {
					l.local.add(float64(got))
				}
			}
		}
	}
}
```

### 解法二：批量化 / 批量校验

不要「一个请求一次判定」，要「一批请求一次判定」：

- HTTP 单请求：本地桶判定，桶空才领租约（解法一）。
- 批量任务：`AllowN(1000)` 一次扣 1,000 个令牌，而不是循环 1,000 次。
- Agent：一次 Run 先按估算 token 预扣，Run 内每次模型调用只做本地扣减，结束后按实际 usage 结算（§17.7）。

```go
// 批量接口：一次 CAS 扣 n 个令牌
func (b *LockFreeBucket) AllowN(now time.Time, n int64) bool {
	need := n * tokenUnit
	nowN := now.UnixNano()
	for {
		old := b.state.Load()
		elapsed := nowN - old.last
		if elapsed < 0 { elapsed = 0 }
		tokens := old.tokens + elapsed*b.rate/int64(time.Second)
		if tokens > b.burst { tokens = b.burst }
		if tokens < need {
			next := &bucketState{tokens: tokens, last: nowN}
			if b.state.CompareAndSwap(old, next) { return false }
			continue
		}
		next := &bucketState{tokens: tokens - need, last: nowN}
		if b.state.CompareAndSwap(old, next) { return true }
	}
}
```

### 解法三：atomic 无锁计数，别用全局锁

把「判定」和「统计」分开：

- 判定用无锁桶（解法一）；
- 统计用 `LongAdder`（Java）或 `atomic.Int64`（Go）；
- 需要 per-key 统计时，用 `ConcurrentHashMap` + `LongAdder`，不要 `synchronized(map)`；
- 需要精确上限时，用信号量（`Semaphore`）而不是计数器，信号量有公平/非公平选择。

```java
private final ConcurrentHashMap<String, LongAdder> passed = new ConcurrentHashMap<>();

public void onPass(String key) {
    passed.computeIfAbsent(key, k -> new LongAdder()).increment();
}
```

`ConcurrentHashMap.computeIfAbsent` 在高并发下仍有锁，但只在「第一次创建」时；后续 `increment()` 是 CAS，不会全局排队。**关键是不要在 `synchronized` 块里做 `computeIfAbsent` + 业务判断。**

### 解法四：采样打点，拒绝 100% 记录

正常请求 1/1000 采样，拒绝/错误 100% 记录：

```go
type Sampler struct {
	n     atomic.Uint64
	every uint64
}

func NewSampler(every uint64) *Sampler { return &Sampler{every: every} }

func (s *Sampler) Allow() bool {
	if s.every == 0 { return true }
	return s.n.Add(1)%s.every == 0
}

// 使用：正常放行采样；拒绝、超时、降级 100% 打
if !allowed || err != nil || sampler.Allow() {
	log.Info("ratelimit", zap.String("key", key), zap.Bool("allowed", allowed))
}
```

18,000 QPS → 18 行/秒（正常）+ 拒绝行（约 9,000/秒，如果拒绝很多）。**拒绝日志也要采样**，否则限流触发时日志反而爆炸；只对「首次拒绝 / 每 10 秒一次」打完整日志，其余只加计数。

### 解法五：规则快照与原子替换

不要「收到推送 → 清空 Map → 逐条插入」，要「构建新快照 → 一次指针替换」：

```go
type Rule struct {
	Key   string
	Rate  int
	Burst int
}

type RuleSnapshot struct {
	Version string
	Rules   map[string]Rule
}

type RuleStore struct {
	cur atomic.Pointer[RuleSnapshot]
}

func (s *RuleStore) Get(key string) (Rule, bool) {
	snap := s.cur.Load()
	if snap == nil {
		return Rule{}, false
	}
	r, ok := snap.Rules[key]
	return r, ok
}

// Replace 由后台 goroutine 调用；请求路径永远只读
func (s *RuleStore) Replace(snap *RuleSnapshot) {
	s.cur.Store(snap)
}
```

Java 版：

```java
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;

public record Rule(String key, int rate, int burst) {}
public record RuleSnapshot(String version, Map<String, Rule> rules) {}

public final class RuleStore {
    private final AtomicReference<RuleSnapshot> ref =
            new AtomicReference<>(new RuleSnapshot("0", Map.of()));

    public Rule get(String key) {
        return ref.get().rules().get(key);
    }

    public void replace(RuleSnapshot snapshot) {
        ref.set(snapshot);   // 一次引用替换，请求线程永远看到完整快照
    }
}
```

刷新流程：后台线程解析 JSON → 构建不可变 Map → 校验版本/校验和 → `Replace`。**解析和构建不在请求线程，也不持规则锁。**

### 解法六：熔断限流器自己

限流器需要自己的健康检查，和业务熔断器同构（第 18 章）：

```
健康信号：
  Redis RTT P99 > 50ms 持续 30s
  Redis 错误率 > 10% 持续 30s
  限流器自身 CPU > 80% 持续 1min
  租约续期失败率 > 20%

动作：
  NORMAL → DEGRADED_LOCAL：停止远程调用，用本地固定阈值
  DEGRADED_LOCAL → BYPASS：仅限「读链路 + 下游有硬墙」的场景
  探测成功 60s → NORMAL
```

```yaml
rate_limit:
  mode: lease
  lease: 100
  redis:
    timeout_ms: 20
    max_retries: 0          # 超时不重试，避免放大
  health:
    rtt_p99_ms: 50
    error_rate: 0.10
    window: 30s
    probe_interval: 10s
  degrade:
    local_fixed_qps_per_pod: 210   # 见 §17.7 的算式
```

> ⚠️ **`max_retries: 0` 不是偷懒**：限流调用超时后重试，会在 Redis 已经过载时再压一倍流量。限流器不是支付，**失败就降级，不要重试**。

---

## 17.7 限流降级开关：比限流更重要的一个决定

### 两种降级姿态

| 姿态 | 行为 | 适用 | 风险 |
| --- | --- | --- | --- |
| 全放行 + 告警 | 限流器故障时全部放行，只发告警 | 读链路、下游有缓存/连接池硬墙 | 下游可能被瞬间打穿 |
| 固定阈值放行 | 用本地保守阈值继续限流 | 写链路、共享 DB、按次计费的下游 | 可能误杀，但保住下游 |

**选择规则**：问一句「如果现在全放行，下游 30 秒内会不会死？」会 → 固定阈值；不会 → 全放行 + 告警。不要凭感觉，按下面的表写死。

### 决策表

| 场景 | 故障信号 | 动作 | 本地阈值 | 对外表达 | 告警级别 |
| --- | --- | --- | --- | --- | --- |
| 下单 / 支付 / 扣库存 | Redis RTT > 50ms 或错误率 > 10% | **固定阈值** | `min(单 Pod 容量, 全局配额/N) × 0.7` | 少量 429 + `Retry-After` | P1 |
| 商品详情 / 推荐 / 搜索 | 同上 | **全放行** | 不设 | 可能变慢，不报错 | P2 |
| 登录 / 鉴权 | 同上 | **固定阈值** | 同上 × 0.8 | 登录变慢，不全面失败 | P1 |
| 内部批处理 / 导出 | 同上 | **拒绝（fail-closed）** | 0 | 任务排队，稍后重试 | P2 |
| 第三方 API（按次计费） | 同上 | **拒绝 + 排队** | 0 | 明确「稍后再试」 | P1 |
| Agent Run（按 token 计费） | 同上 | **固定 TPM 阈值 + 降级小模型** | 供应商配额 × 0.6 | 回复变短/换模型，标注降级 | P1 |

### 固定阈值怎么算

```
单 Pod 安全容量 = 800 QPS（压测得出，P99 200ms）
全局配额 = 12,000 QPS，40 Pod → 300 QPS/Pod
固定阈值 = min(800, 300) × 0.7 = 210 QPS/Pod
集群固定阈值 = 210 × 40 = 8,400 QPS ≈ 事故里手工设的 8,000
```

这个 8,000 不是拍脑袋，是「全局配额 ÷ 副本数 × 安全系数」。**事故时你能在 30 秒内算出这个数，才叫有降级方案。**

### 状态机

```
                 Redis RTT P99 > 50ms 持续 30s
                或 错误率 > 10% 持续 30s
   ┌──────────┐ ──────────────────────────────► ┌────────────────┐
   │  NORMAL  │                                 │ DEGRADED_LOCAL │
   │ 远程租约 │ ◄────────────────────────────── │ 本地固定阈值   │
   └──────────┘      探测成功持续 60s            └────────────────┘
        │                                               │
        │ 仅读链路 + 下游有硬墙                          │ 本地 CPU > 85%
        ▼                                               ▼
   ┌──────────┐                                   ┌──────────┐
   │  BYPASS  │ ◄──────────────────────────────── │ 继续降级 │
   │ 全放行   │       下游错误率 > 5% 则回退        │ 拒绝非核心│
   └──────────┘                                   └──────────┘
```

---

## 17.8 Agent 特有坑：每次限流都要估 token

传统限流扣 1 个令牌；Agent 限流要扣「这次请求会消耗多少 token」。估算方法有三种：

| 方法 | 成本 | 误差 | 适用 |
| --- | --- | --- | --- |
| 精确 tokenizer | 1.2ms / 8KB | 0（对同版本 tokenizer） | 低 QPS、计费前置 |
| 近似估算 | 20µs / 请求 | ±15% | 高 QPS 的准入 |
| 缓存 | 命中 < 1µs | 0（同 prompt） | 重复 prompt、模板化场景 |

把成本算出来：600 Run/s × 3 步 = **1,800 次估算/秒**。

```
精确 tokenizer：1,800 × 1.2ms = 2.16s CPU/秒 = 2.16 核
缓存命中 85%：1,800 × 15% × 1.2ms = 0.324s = 0.32 核
近似 + 1% 采样校准：1,800 × 20µs = 0.036s = 3.6% 核
```

**精确 tokenizer 直接把限流器 CPU 打满**，这就是 Agent 侧特有的「估 token 税」。生产做法是「近似 + 缓存 + 采样校准」：

```go
package tokenest

import (
	"hash/fnv"
	"strconv"
	"sync/atomic"

	lru "github.com/hashicorp/golang-lru/v2"
)

type Message struct {
	Role    string
	Content string
}

type Tokenizer interface {
	Count(model string, msgs []Message) int
}

type Estimator struct {
	cache       *lru.Cache[string, int]
	exact       Tokenizer
	n           atomic.Uint64
	sampleEvery uint64
	factor      atomic.Uint64 // 1e6 = 1.0，按模型校准
}

func NewEstimator(exact Tokenizer, size int) *Estimator {
	c, _ := lru.New[string, int](size)
	e := &Estimator{cache: c, exact: exact, sampleEvery: 100}
	e.factor.Store(1_000_000)
	return e
}

func (e *Estimator) Estimate(model string, msgs []Message) int {
	key := hashKey(model, msgs)
	if v, ok := e.cache.Get(key); ok {
		return v
	}

	var n int
	if e.n.Add(1)%e.sampleEvery == 0 {
		n = e.exact.Count(model, msgs) // 1% 精确采样，用于校准
	} else {
		n = approx(msgs)               // 99% 走近似
	}
	n = int(float64(n) * float64(e.factor.Load()) / 1e6)
	if n < 1 {
		n = 1
	}
	e.cache.Add(key, n)
	return n
}

// 中文 1 字 ≈ 1.5 token，英文 4 字符 ≈ 1 token，再留 10% 安全边际
func approx(msgs []Message) int {
	var cjk, other int
	for _, m := range msgs {
		for _, r := range m.Content {
			if r >= 0x4E00 && r <= 0x9FFF {
				cjk++
			} else {
				other++
			}
		}
	}
	return int(float64(cjk)*1.5+float64(other)/4.0)*11/10 + 8
}

func hashKey(model string, msgs []Message) string {
	h := fnv.New64a()
	h.Write([]byte(model))
	for _, m := range msgs {
		h.Write([]byte(m.Role))
		h.Write([]byte{0})
		h.Write([]byte(m.Content))
	}
	return strconv.FormatUint(h.Sum64(), 16)
}
```

校准：每次模型返回 `usage.total_tokens` 后，更新该模型的修正因子：

```
factor_new = 0.9 × factor_old + 0.1 × (actual / estimated)
```

**预扣 + 结算**：

```
调用前：reserve(estimated_tokens)  → 扣减租户 TPM 配额
调用后：settle(actual_tokens)      → 差额补扣/退还
估算误差 > 20% 的请求：标记 estimated=true，进入对账（第 14 章）
```

> 🩸 **Agent 侧最贵的坑**：按「请求数」限流，一个 200k token 的长上下文请求只扣 1 个令牌，但它可能花掉 $0.5；一分钟来 100 个，就是 $50/分钟。**限流必须按 token，且必须预扣。**

---

## 17.9 会在哪炸：失效模式清单

| 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 限流 P99 从 2ms 涨到 400ms | Redis 单分片过载 / 热 key | `INFO commandstats`、`LATENCY DOCTOR`、看 top key | 本地租约、分片、独立 Redis | 压测按单租户最大 QPS 打 |
| 限流器 CPU 100%，业务 CPU 不高 | 每请求日志 + Histogram + 锁 | `pprof` / `async-profiler` 火焰图 | 采样、无锁、批量 | 限流器 CPU 设独立告警 |
| 关掉限流后错误率更高 | 超时 fail-open，下游无硬墙 | 看 429 与 5xx 的比例 | 固定阈值 + 下游熔断 | 降级决策表 + 演练 |
| 规则刷新时请求卡 1s | 全量解析 + 持锁重建 | 看规则刷新耗时与队列尖刺 | 快照 + 原子替换 | 刷新耗时 > 50ms 告警 |
| 限流器重启后全部放行 | 本地桶冷启动，租约未预热 | 看启动后 10s 的放行量 | 启动预热 + 初始令牌 = burst | 滚动发布时保留旧 Pod 直到预热完成 |
| Agent 预算超支 | 按请求数限流，未按 token | `run_cost_usd` 与 `tokens_used` 对比 | 预扣 + 结算 + 成本熔断 | 单 Run 预算硬上限 |
| 估算误差 40% | 只用近似，不校准 | `estimate_error` 分布 | 采样精确 + EWMA 校准 | 误差 > 20% 告警 |
| 限流器降级后不再恢复 | 探测逻辑只进不出 | `ratelimit_degraded` 持续为 1 | 半开探测 + 自动恢复 | 降级状态必须有超时 |

---

## 17.10 怎么验证：压测、故障注入、观测

### 压测：先打限流器，再打业务

```bash
# 1) 2× 阈值，持续 3 分钟，观察限流器自身 P99 与 CPU
echo "GET http://promo-gateway.internal/api/promo?uid=1" | \
  vegeta attack -rate=24000 -duration=180s -timeout=3s | \
  tee /tmp/promo.bin | vegeta report

# 2) Redis 延迟注入：50ms ± 10ms，验证降级是否在 3s 内生效
sudo tc qdisc add dev eth0 root netem delay 50ms 10ms
sleep 300
sudo tc qdisc del dev eth0 root

# 3) 规则推送风暴：每秒推一次全量规则，持续 60s
for i in $(seq 1 60); do
  curl -s -X POST http://rule-center.internal/rules/push \
    -H 'Content-Type: application/json' -d @rules-2mb.json > /dev/null
  sleep 1
done
```

期望：

- 2× 压测：拒绝率 ≈ 50%，限流器 P99 < 10ms，Pod CPU < 70%；
- 延迟注入：`ratelimit_degraded=1` 在 3 秒内出现，业务 P99 抬升 < 100ms，无 5xx；
- 规则风暴：请求 P99 无尖刺，规则版本在 5 秒内一致。

### 必看指标

| 指标 | 含义 | 告警线 |
| --- | --- | --- |
| `ratelimit_check_duration_seconds` | 限流判定耗时 | P99 > 10ms 持续 2min |
| `ratelimit_redis_rtt_seconds` | Redis 往返 | P99 > 20ms 持续 1min |
| `ratelimit_self_cpu_ratio` | 限流器占 Pod CPU | > 30% 持续 5min |
| `ratelimit_lease_renew_total` | 租约续期次数 | 失败率 > 5% |
| `ratelimit_degraded` | 降级状态 | =1 立即 P2 |
| `ratelimit_rule_reload_seconds` | 规则刷新耗时 | > 50ms |
| `ratelimit_queue_depth` | 等待判定的请求 | > 100 |
| `ratelimit_estimate_error` | Agent token 估算误差 | P95 > 20% |

```promql
# 限流器自身 P99
histogram_quantile(0.99,
  sum by (le) (rate(ratelimit_check_duration_seconds_bucket[1m])))

# 限流器 CPU 占 Pod 比例（需要 cAdvisor 指标）
sum by (pod) (rate(container_cpu_usage_seconds_total{container="promo-gateway"}[1m]))
  / sum by (pod) (kube_pod_container_resource_limits{resource="cpu"})

# Redis RTT 均值
sum(rate(ratelimit_redis_rtt_seconds_sum[1m]))
  / sum(rate(ratelimit_redis_rtt_seconds_count[1m]))

# 降级次数
sum(rate(ratelimit_degraded_total[5m]))
```

> 💡 **告警要盯「限流器自身」，不是「限流拒绝率」**。拒绝率高可能是流量真的大，属于正常；限流器 P99 高、CPU 高、Redis RTT 高，才是它自己要死的信号。

---

## 17.11 用什么工具

| 场景 | 首选 | 理由 / 坑 |
| --- | --- | --- |
| Go 本地限流 | `x/time/rate` 或本章的无锁桶 | 标准库够用；无锁桶适合超高 QPS |
| Java 本地限流 | Resilience4j `RateLimiter` / Sentinel | 自带指标；注意不要每请求打 Histogram |
| 分布式租约 | 自研 + Redis Lua | 与配额/计费打通；租约大小是精度旋钮 |
| 规则管理 | 配置中心 + 本地快照 | 不要用「推送即生效」的裸 JSON |
| 限流器熔断 | Resilience4j CircuitBreaker / 自研健康检查 | 限流器自己也要有 breaker |
| 压测 | vegeta / k6 | vegeta 适合固定速率；k6 适合脚本化 |
| 火焰图 | `pprof`（Go）/ async-profiler（Java） | 先看限流器占多少 CPU，再谈优化 |

**自研边界**：本地桶、租约、规则快照值得自研；**不要自研的是「分布式协调的一致性」**——Redis/Sentinel 已经替你踩过主从、时钟、脚本原子性的坑。

---

## 17.12 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 限流器也是服务 | 它要 CPU、Redis、日志、指标；高压下会先死 |
| 每请求 Redis | 18,000 QPS = 18,000 RTT/s；Lua 63% 单核，26k 时 100% |
| 本地租约 | 租约 100 → Redis 180 次/秒，降 100 倍；超发 ≤ 100×Pod 数 |
| 锁 | 全局 `synchronized` 在 450 QPS/Pod 下可占 18% CPU |
| 日志 | 18,000 行/秒 × 350B = 6.3MB/s；同步写占 13% CPU |
| 指标 | 每请求 20 桶 Histogram = 360,000 次原子操作/秒 |
| 规则刷新 | 2MB JSON + 4 万条重建 = 300ms stop-the-world |
| 降级开关 | 写链路固定阈值，读链路全放行；公式 `min(单机容量, 全局/N)×0.7` |
| 固定阈值算例 | 12,000/40=300，min(800,300)×0.7=210/Pod，集群 8,400 |
| Agent 估 token | 精确 tokenizer 1.2ms × 1,800/s = 2.16 核；近似 + 缓存 + 1% 采样 |
| 预扣结算 | 调用前 reserve，调用后 settle；误差 >20% 进对账 |
| 验证 | 2× 压测 + Redis 延迟注入 + 规则推送风暴 |

---

## 17.13 自检题

1. **限流器每请求调一次 Redis，QPS 从 6,000 涨到 18,000，Redis 单分片 CPU 100%。你把 Redis 从 3 分片扩到 6 分片，为什么可能没用？**
   <details><summary>答案要点</summary>
   因为限流 key 是「租户+接口」，一个热 key 只能落一个分片；扩分片不会拆开同一个 key。18,000 QPS 全打在一个分片上，6 分片里只有 1 个忙。解法是本地租约（把 18,000 降到 180）、逻辑分片（`key#0..7`）、或热点租户独立实例；单纯扩分片只对「多 key 均匀分布」有效。
   </details>

2. **你的限流器 P99 从 2ms 涨到 400ms，但业务 CPU 只有 40%。列出三个最可能的根因和对应的最小验证动作。**
   <details><summary>答案要点</summary>
   ① Redis 抖动/热 key：看 `ratelimit_redis_rtt_seconds` P99 与 `INFO commandstats`；② 限流统计锁竞争：用 `pprof`/async-profiler 看锁等待，检查是否有全局 `synchronized`；③ 同步日志/逐请求 Histogram：看限流器 CPU 火焰图里 log/histogram 占比。最小动作分别是：加租约、换 `LongAdder`/无锁桶、采样打点。**不要先扩容**，扩容会把 Redis 连接数和日志量一起放大。
   </details>

3. **限流器故障时，下单链路和商品详情链路分别应该「全放行」还是「固定阈值」？为什么不能反过来？**
   <details><summary>答案要点</summary>
   下单走固定阈值：下游是共享订单库，全放行会在 30 秒内打满连接池，且写链路不可回滚，伤害是资金级的。商品详情走全放行 + 告警：读链路有缓存和连接池硬墙，全拒绝会让用户看到空白页，伤害大于变慢。反过来做：下单全放行 → 数据库死；详情全拒绝 → 可恢复的读故障被放大成全站不可用。**判断标准是「下游有没有硬墙 + 失败是否可回滚」。**
   </details>

---

## 17.14 延伸阅读

- Google SRE Book：*Handling Overload* 与 *Addressing Cascading Failures*（限流器自身过载的经典论述）
- `golang.org/x/time/rate` 源码 `limiter.go`：`reserveN` 的预占与取消
- GCRA（Generic Cell Rate Algorithm）：ITU-T I.371 与 `throttled` 库实现
- Sentinel `LeapArray` 与 `cluster` 模块：滑动窗口与 token server
- Prometheus `histogram_quantile` 与 native histograms：分位数聚合的正确姿势（第 20 章）
- 上一章：[第 16 章 分布式限流的实现与坑](16-distributed-rate-limit.md)
- 下一章：[第 18 章 熔断：状态机、阈值与隔离](18-circuit-breaker.md)

**交叉引用**：总纲 [0-summary.md](0-summary.md) ｜ 语言与运行时 [../go/0-summary.md](../go/0-summary.md) ｜ 平台治理 [../llm/0-summary.md](../llm/0-summary.md)
