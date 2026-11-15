# 16. 分布式限流的实现与坑

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 流量与韧性：不崩是怎么做到的 ｜ **编号**：16 / 52
> **本章定位**：单机令牌桶只能保证「这一台」不超速，保证不了「二十台加起来」不超速。本章把集中式、配额本地化、集群流控三种做法拆开算账：精度损失多少、故障时放行还是拒绝、热 key 与时钟回拨怎么处理。
> **读法**：先看 §16.2 的反例表，确认你现在的「分布式限流」是不是其实只是「每台限流」；§16.4 的 Lua 脚本和租约代码可以直接抄，但务必读完 §16.6 的故障行为再上线。

---

## 16.1 引子：20 个副本，每个 1,000 QPS，合起来 20,000

2025-08-19 21:07，某电商大促预热，订单查询服务 `order-query` 扩容到 **20 个 Pod**。

每个 Pod 都老老实实配了本地限流：

```yaml
rate_limit:
  local_qps: 1000       # 单副本上限
```

所有人都觉得「1,000 已经很大了，单机能扛住」。但下游订单库的连接池是**共享**的：`max_connections=2,000`，每个查询占用连接 **15ms**，按第 15 章的公式，数据库的真实容量是：

```
2,000 ÷ 0.015s ≈ 133,333 QPS？——错，这是「连接全给订单查询」的理论值。
实际订单库还要服务下单、库存、支付回调，能给查询的只有 600 个连接：
600 ÷ 0.015s = 40,000 QPS 的数据库侧上限。
再乘安全系数 0.75：30,000 QPS。
```

看起来 20 × 1,000 = 20,000 也没超。问题出在**流量倾斜**：大促开始后，推荐位把流量集中到 3 个热点商品，20 个 Pod 的负载从均匀变成 3 个 Pod 各 2,800 QPS、其余 17 个各 300 QPS。那 3 个 Pod 的本地限流器立刻放行到 1,000 上限，但**另外 17 个 Pod 的额度不会转移**——全局实际放行 = 3×1,000 + 17×300 = **8,100 QPS**，而热点查询在数据库里要扫更多行，单次耗时从 15ms 涨到 **90ms**。

```
数据库可承载并发 = 600 连接
热点查询单次 90ms → 600 ÷ 0.09 = 6,667 QPS
实际放行 8,100 QPS → 需要 729 个并发连接，超过 600
```

21:08:10，订单库连接池 100% 占满，`order-query` 的 P99 从 180ms 涨到 **6.8s**；21:09:00，错误率 61%；21:14:00 人工在网关把全局限流压到 6,000 QPS，21:19:00 恢复。**11 分钟，210 万次失败查询，GMV 损失约 280 万元。**

事后第一版修复是「上集中式 Redis 限流」：所有 Pod 每个请求都调一次 Redis Lua。上线三天后又炸了一次——这次是**热 key**：单个租户的 `order-query` 键在晚高峰打到 **9,000 QPS**，所在 Redis 分片 CPU 100%，Lua P99 从 1.2ms 涨到 **180ms**，限流器自己成了延迟源。6 分钟，43 万元。

> 🩸 **两句话总结**：本地限流的问题是「副本数会把阈值偷偷乘大」；集中式限流的问题是「所有请求都要过同一个点，那个点会变成新的瓶颈」。分布式限流从来不是「把计数器搬到 Redis」，而是**在精度、可用性、延迟三者之间选一个你能接受的位置**。

---

## 16.2 到底是什么：多副本共享一个配额时，怎么做出「一致」的判定

**定义**：分布式限流 = 当同一个逻辑配额被多个实例共同消耗时，用一个协调机制保证「所有实例放行的总量」不超过配额，或者在协调机制失效时按预定策略退化。

三个关键词：

- **同一个逻辑配额**：不是「每台 1,000」，而是「这个租户/接口全局 10,000」。
- **协调机制**：Redis、token server、租约、或者「预分票 + 本地桶」。
- **失效时退化**：协调机制挂了，你必须提前决定「放行」还是「拒绝」，不能等挂了再想。

### 六个常见反例

| 你以为的分布式限流 | 为什么不是 | 不做会怎样 |
| --- | --- | --- |
| 每个 Pod 配 1,000，20 个 Pod | 全局上限变成 20,000，且流量倾斜时额度不能转移 | 下游按 10,000 配的池子被打穿（16.1 就是这样） |
| 网关单点限流 10,000 | 网关后面还有内部调用、重试、消息消费、定时任务，它们不走网关 | 内部流量绕过闸门，下游照样死 |
| Redis `INCR` + `EXPIRE` 两条命令 | 两条命令之间可能失败；`INCR` 后进程崩溃，key 永不过期 | 计数器只增不减，最终永久限流 |
| 把全局配额平均分给 20 个 Pod | 流量倾斜时，热点 Pod 的额度不够，冷 Pod 的额度闲置 | 全局只用了 55% 的容量，却已经开始拒绝 |
| 只在入口限流，内部调用不限 | 一次入口请求可能扇出 5 次下游调用，重试再乘 3 | 下游实际 QPS = 入口 × 15 |
| 用客户端时间戳算令牌 | 各 Pod 时钟差 500ms，令牌桶提前/延后补充 | 同一秒内多放 50% 的请求 |

> 💡 **判断标准**：把你的限流配置乘以副本数，如果结果大于下游容量，你就没有分布式限流，只有「分布式地各自限流」。

---

## 16.3 结构图：三种实现，三种「一致性成本」

```
① 集中式（Redis + Lua）
   Pod1 ─┐
   Pod2 ─┼──► Redis 单 key（Lua 原子执行）──► allowed / wait_ms
   Pod3 ─┘        每请求 1 次 RTT，精度最高，Redis 是单点

② 配额本地化（lease + 本地令牌桶）
   Pod1 ──► 本地桶 ──(桶空)──► Redis 全局池：领 100 个 ──► 本地桶
   Pod2 ──► 本地桶 ──(桶空)──► Redis 全局池：领 100 个 ──► 本地桶
   快路径 2µs；慢路径每 100 个请求 1 次 RTT；超发上限 = lease × Pod 数

③ 集群流控（Sentinel Token Server）
   Pod1 ─┐
   Pod2 ─┼──► Token Server（独立集群，按 namespace 管规则）──► 批量令牌
   Pod3 ─┘        Token Server 挂了 → fallbackToLocalWhenFail → 本地规则

   精度：① > ③ > ②        可用性：② > ③ > ①        延迟：② < ③ < ①
```

三种不是互斥的。生产上常见的是 **② 做快路径 + ①/③ 做慢路径兜底**：本地桶扛 99% 的请求，只有桶空时才去全局池领租约。第 15 章已经讲过令牌桶、滑动窗口的算法细节，这里不再重复，只讲「多副本怎么共享」。

---

## 16.4 怎么实现：三种方案与精度账

### 方案一：集中式 Redis + Lua（精度最高，延迟最贵）

核心要求只有一条：**判定必须在一个 Lua 脚本里原子完成**，不能 `GET` 完再 `SET`。

```lua
-- token_bucket.lua
-- KEYS[1] = 桶 key，例如 rl:tb:{tenant:42}:order-query
-- ARGV[1] = rate（每秒补充令牌数）
-- ARGV[2] = burst（桶容量）
-- ARGV[3] = 本次请求的令牌数（通常 1）
-- 返回 {allowed(0/1), 剩余令牌(整数), 建议等待毫秒}
local rate      = tonumber(ARGV[1])
local burst     = tonumber(ARGV[2])
local requested = tonumber(ARGV[3])

if rate <= 0 or burst <= 0 or requested <= 0 then
  return {0, 0, 1000}
end

-- 关键：用 Redis 服务端时间，不用客户端时间，避免多 Pod 时钟不一致
local t     = redis.call('TIME')
local now   = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local data  = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts     = tonumber(data[2])

if tokens == nil then
  tokens = burst
  ts = now
end

-- 时钟回拨保护：now < ts 时按 0 处理，不补令牌
local delta = math.max(0, now - ts)
tokens = math.min(burst, tokens + delta * rate / 1000)

local allowed = 0
if tokens >= requested then
  allowed = 1
  tokens = tokens - requested
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
-- TTL = 从空桶补满所需时间 + 1s，避免冷 key 永久占内存
redis.call('PEXPIRE', KEYS[1], math.ceil(burst / rate * 1000) + 1000)

local waitMs = 0
if allowed == 0 then
  waitMs = math.ceil((requested - tokens) / rate * 1000)
end
return {allowed, math.floor(tokens), waitMs}
```

Java 调用（Spring Data Redis）：

```java
import org.springframework.core.io.ClassPathResource;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.stereotype.Component;

import java.util.List;

@Component
public class RedisTokenBucket {

    private final StringRedisTemplate redis;
    private final DefaultRedisScript<List> script;

    public RedisTokenBucket(StringRedisTemplate redis) {
        this.redis = redis;
        this.script = new DefaultRedisScript<>();
        this.script.setLocation(new ClassPathResource("lua/token_bucket.lua"));
        this.script.setResultType(List.class);
    }

    public Decision tryAcquire(String key, int rate, int burst, int permits) {
        // 注意：execute 的 args 会按 String 序列化，Lua 里 tonumber 转换
        List<?> r = redis.execute(script, List.of(key),
                String.valueOf(rate), String.valueOf(burst), String.valueOf(permits));
        if (r == null || r.size() < 3) {
            return Decision.degraded();          // 脚本异常：交给上层降级策略
        }
        return new Decision(((Long) r.get(0)) == 1L,
                ((Long) r.get(1)).intValue(),
                ((Long) r.get(2)).longValue());
    }

    public record Decision(boolean allowed, int remaining, long waitMs) {
        static Decision degraded() { return new Decision(false, 0, 1000); }
    }
}
```

Go 调用（go-redis v9）：

```go
package ratelimit

import (
	"context"
	_ "embed"

	"github.com/redis/go-redis/v9"
)

//go:embed lua/token_bucket.lua
var tokenBucketLua string

var tokenBucketScript = redis.NewScript(tokenBucketLua)

type Decision struct {
	Allowed   bool
	Remaining int64
	WaitMs    int64
}

func TryAcquire(ctx context.Context, rdb *redis.Client, key string,
	rate, burst, permits int) (Decision, error) {

	vals, err := tokenBucketScript.Run(ctx, rdb, []string{key},
		rate, burst, permits).Int64Slice()
	if err != nil {
		return Decision{}, err
	}
	return Decision{Allowed: vals[0] == 1, Remaining: vals[1], WaitMs: vals[2]}, nil
}
```

**精度**：单个 key 内是精确的；Lua 的浮点令牌只在 Redis 内部计算，不会因为客户端时钟产生漂移。**代价**：每个请求一次 RTT。5,000 QPS 就是 **5,000 次 Redis 往返/秒**；如果一次 Lua 加网络占 1ms，Redis 侧需要 5 个 CPU 核的等效处理能力（实际 Redis 单线程，只能靠分片）。第 17 章会看到这个数字怎么把 CPU 打满。

> ⚠️ **超时重试的坑**：Lua 执行成功但客户端超时，重试会**再扣一次令牌**。限流场景下「多扣」比「少扣」安全，所以不要在超时后无限重试；最多重试一次，且要接受拒绝率略高。更好的做法是带 `request_id` 做短期去重，但那是幂等表（第 2 章）的活，别把限流脚本写成账本。

### 方案二：配额本地化（lease 预取 + 本地令牌桶）

思路：全局池只负责「发票」，本地桶负责「花票」。每个 Pod 桶空时，向 Redis 领 `L` 个令牌；领到后本地慢慢花，花完再领。

```go
package ratelimit

import (
	"context"
	"math"
	"sync"
	"time"
)

// RemotePool 是全局配额池，Redis 实现见下面的 quota_pool.lua
type RemotePool interface {
	Take(ctx context.Context, key string, n int) (int, error)
}

type LeaseBucket struct {
	mu     sync.Mutex
	tokens float64
	last   time.Time
	rate   float64 // 本地补充速率，仅用于租约之间的平滑
	burst  float64
}

func NewLeaseBucket(rate, burst float64) *LeaseBucket {
	return &LeaseBucket{tokens: burst, last: time.Now(), rate: rate, burst: burst}
}

func (b *LeaseBucket) take(n float64, now time.Time) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	elapsed := now.Sub(b.last).Seconds()
	if elapsed > 0 {
		b.tokens = math.Min(b.burst, b.tokens+elapsed*b.rate)
		b.last = now
	}
	if b.tokens >= n {
		b.tokens -= n
		return true
	}
	return false
}

func (b *LeaseBucket) add(n float64) {
	b.mu.Lock()
	b.tokens = math.Min(b.burst, b.tokens+n)
	b.mu.Unlock()
}

type LeaseLimiter struct {
	key       string
	lease     int
	local     *LeaseBucket
	remote    RemotePool
	degraded  bool
	degradeMu sync.RWMutex
}

func (l *LeaseLimiter) Allow(ctx context.Context) bool {
	now := time.Now()
	if l.local.take(1, now) {
		return true // 快路径：零网络，P99 约 2µs
	}
	if l.isDegraded() {
		return l.local.take(1, now) // 降级后只靠本地补充，速率已调低
	}
	got, err := l.remote.Take(ctx, l.key, l.lease)
	if err != nil || got <= 0 {
		l.setDegraded(true) // 见 §16.6 的故障决策表
		return l.local.take(1, now)
	}
	l.local.add(float64(got))
	return l.local.take(1, now)
}

func (l *LeaseLimiter) isDegraded() bool {
	l.degradeMu.RLock()
	defer l.degradeMu.RUnlock()
	return l.degraded
}

func (l *LeaseLimiter) setDegraded(v bool) {
	l.degradeMu.Lock()
	l.degraded = v
	l.degradeMu.Unlock()
}
```

全局池的 Lua 只做「从池子里扣一笔租约」，不做每请求判定：

```lua
-- quota_pool.lua
-- KEYS[1] = rl:pool:{tenant:42}:order-query
-- ARGV[1] = capacity（全局桶容量）, ARGV[2] = refill_per_sec, ARGV[3] = want
local cap  = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local want = tonumber(ARGV[3])

local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local d = redis.call('HMGET', KEYS[1], 'avail', 'ts')
local avail = tonumber(d[1])
local ts = tonumber(d[2])
if avail == nil then avail = cap; ts = now end

local delta = math.max(0, now - ts)
avail = math.min(cap, avail + delta * rate / 1000)
local got = math.min(want, math.floor(avail))
avail = avail - got

redis.call('HSET', KEYS[1], 'avail', avail, 'ts', now)
redis.call('PEXPIRE', KEYS[1], math.ceil(cap / rate * 1000) + 1000)
return got
```

**精度损失，必须算出来**：

设全局配额 `Q=10,000/s`，Pod 数 `N=20`，租约大小 `L=100`。

```
超发上限 = L × N = 100 × 20 = 2,000 个令牌
相对误差 = 2,000 / 10,000 = 20%
Redis 调用量 = Q / L = 10,000 / 100 = 100 次/秒
（对比集中式的 10,000 次/秒，降低 100 倍）
```

租约的**浪费率**取决于「领了没用完」：

```
浪费率 ≈ max(0, 1 - r × T / L)
r = 单 Pod 实际消耗速率，T = 租约有效期，L = 租约大小
例：r=50/s，T=2s，L=200 → 实际用掉 100，浪费 50%
    r=50/s，T=2s，L=50  → 实际用掉 100 > 50，浪费 0（但 Redis 调用变多）
```

所以 `L` 是「精度 ↔ Redis 压力」的旋钮，不是越大越好。经验值：`L ≈ 单 Pod QPS × 0.5s`，再把 `L×N/Q` 控制在 5%~10%。

| 方案 | 全局精度 | 超发上限 | 每请求延迟 | Redis QPS（Q=10k） | 适用 |
| --- | --- | --- | --- | --- | --- |
| 集中式 | 精确 | 0 | +0.5~2ms | 10,000 | 低 QPS、强公平、计费前置 |
| 租约 L=100 | ±2% | 2,000（20%） | 快路径 2µs | 100 | 高 QPS、可接受少量超发 |
| 租约 L=20 | ±0.4% | 400（4%） | 快路径 2µs | 500 | 中高 QPS、误差敏感 |
| Token Server 批量 50 | ±1% | 1,000（10%） | +0.3~1ms | 200 | 已有 Sentinel 体系 |

> 💡 **租约不是「缓存」，是「提前把全局额度记在一个 Pod 名下」**。Pod 崩溃时，没花完的租约就浪费了；所以租约要带 TTL，且 Pod 优雅下线时要归还未用额度（可选，收益通常小于复杂度）。

### 方案三：集群流控（Sentinel Token Server）

Sentinel 的集群流控把「判定」放在独立的 token server 集群，客户端按 `flowId` 请求令牌。它和租约的本质一样，只是把「发票」换成了一个专门的 RPC 服务，并且规则可以按 namespace 统一管理。

```java
import com.alibaba.csp.sentinel.cluster.ClusterRuleConstant;
import com.alibaba.csp.sentinel.cluster.client.config.ClusterClientAssignConfig;
import com.alibaba.csp.sentinel.cluster.client.config.ClusterClientConfigManager;
import com.alibaba.csp.sentinel.slots.block.RuleConstant;
import com.alibaba.csp.sentinel.slots.block.flow.ClusterFlowConfig;
import com.alibaba.csp.sentinel.slots.block.flow.FlowRule;
import com.alibaba.csp.sentinel.slots.block.flow.FlowRuleManager;

import java.util.List;

public class ClusterFlowClient {

    public void init() {
        // 1) 告诉客户端 token server 在哪
        ClusterClientAssignConfig assign = new ClusterClientAssignConfig();
        assign.setServerHost("sentinel-token.internal");
        assign.setServerPort(18730);
        ClusterClientConfigManager.applyNewAssignConfig(assign);

        // 2) 声明一条集群流控规则
        FlowRule rule = new FlowRule("order:create");
        rule.setGrade(RuleConstant.FLOW_GRADE_QPS);
        rule.setCount(6000);                 // 全局阈值，不是单机阈值
        rule.setClusterMode(true);

        ClusterFlowConfig cluster = new ClusterFlowConfig();
        cluster.setFlowId(1001L);            // 全局唯一，token server 靠它找规则
        cluster.setThresholdType(ClusterRuleConstant.FLOW_THRESHOLD_GLOBAL);
        // token server 不可用时退回本地规则：可用性优先
        cluster.setFallbackToLocalWhenFail(true);
        rule.setClusterConfig(cluster);

        FlowRuleManager.loadRules(List.of(rule));
    }
}
```

Token Server 部署（1.8.x 以官方文档为准）：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: sentinel-token-server
spec:
  replicas: 2
  selector:
    matchLabels: { app: sentinel-token-server }
  template:
    metadata:
      labels: { app: sentinel-token-server }
    spec:
      containers:
        - name: token-server
          image: registry.internal/sentinel-token-server:1.8.8
          args:
            - "-Dcsp.sentinel.cluster.server.port=18730"
            - "-Dproject.name=sentinel-token-server"
            - "-Dcsp.sentinel.dashboard.server=sentinel-dashboard.internal:8080"
          ports:
            - containerPort: 18730
```

**精度损失**：客户端不是每个请求都问 token server，而是批量拿 `batchCount` 个令牌，所以误差与租约同阶：`±batch × 客户端数`。Sentinel 默认的批量策略可以在 `ClusterClientConfig` 里调，原则同样是「误差 ≤ 配额的 10%」。

**故障行为**：`fallbackToLocalWhenFail=true` 时，token server 挂了会退回本地 `FlowRule` 的单机阈值。注意：本地阈值如果还是 6,000，20 个 Pod 就是 120,000——**fallback 的本地规则必须单独配一个「保守值」，不能复用全局值**。这是 Sentinel 集群流控最常见的误配。

---

## 16.5 五个坑：热 key、时钟、抖动、分区、规则

### 坑一：热 key——一个租户打满一个 Redis 分片

Redis Cluster 按 key 的 CRC16 分 16,384 个 slot。**一个 key 只能落在一个分片**，所以「单租户 9,000 QPS」就是「单分片 9,000 次 Lua/s」。

```
Lua 单次 CPU 约 30µs → 9,000 × 30µs = 0.27s CPU/s = 27% 单核
加上网络收包、协议解析、AOF fsync，实际可到 60%~100%
```

三种解法：

| 解法 | 做法 | 精度 | 代价 |
| --- | --- | --- | --- |
| 本地租约 | 热点租户也走 lease，Redis 只发票 | 超发 ≤ L×N | 需要改客户端 |
| 逻辑分片 | `key#0..7` 八个子 key，配额各 1/8 | 总突发可达 8×burst | 要处理「某个子 key 先耗尽」的倾斜 |
| 热点隔离 | 大租户单独一个 Redis 实例/集群 | 精确 | 成本高，运维复杂 |

分片后的总额度判定不是原子的：8 个子 key 各自判定，最坏情况 8 个子 key 同时满桶，瞬时放行 `8×burst`。所以分片数越多，`burst` 越要收窄。

### 坑二：时钟回拨——客户端时间快 500ms，令牌多补 500ms

假设 `rate=1,000/s`，Pod 的 NTP 把时钟往前跳了 500ms：

```
多补令牌 = 1,000 × 0.5 = 500 个
相当于凭空多放行 500 个请求（半秒的额度）
```

两条铁律：

1. **Redis Lua 里用 `redis.call('TIME')`**，不要用 Java/Go 传进来的 `System.currentTimeMillis()`。
2. **本地桶用单调时钟**：Go 用 `time.Since`/`time.Now()` 的 monotonic 部分，Java 用 `System.nanoTime()`，不要用 `System.currentTimeMillis()`。

### 坑三：Redis 抖动——P99 从 1ms 到 180ms，限流器变成延迟源

Redis 抖动通常来自：大 key 删除、AOF rewrite、主从全量同步、同分片的其他热 key、网络重传。限流器对 Redis 的调用必须有一个**独立的短超时**（建议 20~50ms），且超时后不能同步重试。

| 现象 | 根因 | 处理 |
| --- | --- | --- |
| Lua P99 突然 100ms+ | 同分片大 key / AOF rewrite | 限流 key 独立分片；`INFO commandstats` 看 `evalsha` 耗时 |
| 超时后大量 429 | 超时被当成「拒绝」 | 超时走降级策略，不要直接拒绝 |
| 超时后大量放行，下游挂了 | 超时被当成「放行」且无本地兜底 | 降级到固定本地阈值，不是无上限 |
| Redis 主从切换，计数丢失 | 异步复制 | 接受短时间超发；用本地租约兜底 |

### 坑四：网络分区——放行还是拒绝，必须提前写死

这是分布式限流最重要的一张决策表。**不要在故障时开会决定**，要在设计时就按「资源重要性」定好。

| 场景 | 限流器不可用时的选择 | 理由 | 对外表现 |
| --- | --- | --- | --- |
| 下单、支付、扣库存 | **固定阈值放行**（本地容量 × 0.7） | 全放行会打穿数据库；全拒绝等于主动宕机 | 少量 429，核心链路保住 |
| 商品详情、推荐、搜索 | **全放行 + 告警** | 读链路有缓存和连接池硬墙，全放行的伤害小于全拒绝 | 可能变慢，但不空白 |
| 登录、鉴权 | **固定阈值放行** | 鉴权挂了全站不可用；但不能无限放行打穿用户库 | 登录变慢，不全面失败 |
| 内部批处理、导出 | **拒绝（fail-closed）** | 非核心，可延后；放行会挤占在线流量 | 任务排队，稍后重试 |
| 第三方 API（按次计费） | **拒绝 + 排队** | 超发就是真金白银，且可能被供应商封禁 | 明确提示「稍后再试」 |

> ⚠️ **「全放行」不是无脑放行**：它必须同时打开下游熔断（第 18 章）和连接池上限。限流器失效时，下游的硬墙是最后一道防线；如果下游没有硬墙，就不能选全放行。

### 坑五：规则推送不一致——20 个 Pod，3 个用了新规则

规则中心推送给 20 个 Pod，网络抖动导致 3 个 Pod 没收到；或者推送过程中新规则只应用了一半（限流值变了、burst 没变）。结果是同一时刻集群里存在两种限流值，全局配额既不是旧的也不是新的。

正确做法是**版本化快照 + 拉取兜底**：

```yaml
# 规则中心返回的快照
version: 20250819-03
checksum: "sha256:9f2c..."
rules:
  - key: "tenant:42:order-query"
    rate: 800
    burst: 1600
```

客户端每 5 秒拉一次 `/rules?version=current`，版本没变就返回 304；变了就**整体替换**（不是逐条改）。本地磁盘缓存最后一份可用快照，启动时先读磁盘再拉中心。这样最多「晚 5 秒生效」，不会「一半新一半旧」。

---

## 16.6 多租户公平：配额、WFQ 与表结构

「公平」不是「每人一样多」，而是「按约定分配，且空闲额度可以借」。三种机制：

| 机制 | 规则 | 优点 | 缺点 | 适用 |
| --- | --- | --- | --- | --- |
| 静态配额 | 每个租户固定 QPS | 简单、可预测 | 空闲额度浪费，热点租户被卡死 | 套餐制、强隔离 |
| WFQ（加权公平队列） | 按权重分配带宽，空闲时借出 | 利用率高、公平 | 实现复杂，需要队列 | 多租户共享池 |
| Max-Min Fair | 先满足小需求，再均分剩余 | 最公平 | 需要知道所有需求 | 离线批处理 |

WFQ 的核心是「虚拟完成时间」：

```
每个租户维护 finish_time
新请求到达：finish = max(finish, now) + cost / weight
每次选择 finish 最小的租户放行
权重 3:1 的两个租户，在 1,000 QPS 下分别得到 750 / 250
如果一个租户空闲，另一个可以临时用满 1,000（work-conserving）
```

表结构（MySQL 8，完整可执行）：

```sql
CREATE TABLE tenant_quota (
  tenant_id         BIGINT UNSIGNED NOT NULL,
  resource          VARCHAR(64)     NOT NULL,   -- 例如 order-query / llm:gpt-4o
  qps_limit         INT UNSIGNED    NOT NULL,
  burst             INT UNSIGNED    NOT NULL,
  daily_token_quota BIGINT UNSIGNED NOT NULL DEFAULT 0,
  weight            SMALLINT UNSIGNED NOT NULL DEFAULT 100,
  version           BIGINT UNSIGNED NOT NULL DEFAULT 1,
  updated_at        DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                                    ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (tenant_id, resource),
  KEY idx_resource (resource)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE quota_lease (
  lease_id    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  tenant_id   BIGINT UNSIGNED NOT NULL,
  resource    VARCHAR(64)     NOT NULL,
  instance_id VARCHAR(64)     NOT NULL,
  tokens      INT UNSIGNED    NOT NULL,
  state       ENUM('ISSUED','USED','EXPIRED') NOT NULL DEFAULT 'ISSUED',
  issued_at   DATETIME(3)     NOT NULL,
  expires_at  DATETIME(3)     NOT NULL,
  PRIMARY KEY (lease_id),
  KEY idx_tenant_time (tenant_id, issued_at),
  KEY idx_expire (state, expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 巡检：过去 1 分钟每个租户实际用了多少租约（用于发现「领了不用」）
SELECT tenant_id, resource,
       SUM(tokens) AS leased_tokens,
       SUM(CASE WHEN state = 'USED' THEN tokens ELSE 0 END) AS used_tokens
FROM quota_lease
WHERE issued_at >= NOW(3) - INTERVAL 1 MINUTE
GROUP BY tenant_id, resource
ORDER BY leased_tokens DESC
LIMIT 20;
```

WFQ 的 Go 简化实现（单调度器内使用，多副本时每个副本各自 WFQ，再用全局配额封顶）：

```go
type Tenant struct {
	ID     string
	Weight float64
	Finish float64 // 虚拟完成时间
}

// Pick 选择下一个应放行的租户；cost 通常为 1（一个请求）
func Pick(tenants []*Tenant, now, cost float64) *Tenant {
	if len(tenants) == 0 {
		return nil
	}
	min := tenants[0]
	for _, t := range tenants[1:] {
		if t.Finish < min.Finish {
			min = t
		}
	}
	min.Finish = math.Max(min.Finish, now) + cost/min.Weight
	return min
}
```

> 💡 **WFQ 只解决「谁先走」，不解决「总共能走多少」**。全局总量仍然要靠 Redis 池或 token server 封顶；WFQ 是在配额之内做分配。两者是乘法关系，不是替代关系。

---

## 16.7 双修对照：QPS 配额 → TPM 配额 + 供应商配额

| 维度 | 传统系统 | Agent 系统 | 同 or 异 |
| --- | --- | --- | --- |
| 机制本体 | Redis Lua、租约、token server | **完全相同**，Lua 脚本一行不改 | **同（可直接复用）** |
| 身份/唯一键 | `tenant:resource`（租户 + 接口） | `tenant:provider:model` + `run_id` + `step_id` | 同构，多一层模型/供应商 |
| 限的是什么资源 | 下游 QPS、DB 连接、线程 | **TPM**（token/分钟）、并发 Run、单 Run 预算、供应商配额 | **异**（三定律之二：换资源） |
| 全局配额 | 10,000 QPS | 供应商 10,000 RPM / 2,000,000 TPM；平台 600 并发 Run | 单位不同，算法同 |
| 失败模式 | 热 key、Redis 抖动、规则不一致 | 加：token 估算偏差、供应商 429、单 Run 超预算 | 多两类 |
| 恢复方式 | 本地租约 + 固定阈值 | 本地租约 + 降级小模型 + 预算刹车 | 同 + 换模型 |
| 验证方法 | 2× 压测、租约对账 | 同 + token 对账（预估 vs 账单） | 同 + 一层对账 |
| 观测指标 | `ratelimit_rejected_total`、Redis RTT | 加 `tokens_reserved`、`run_cost_usd`、`provider_quota_remaining` | 指标集扩展 |

**哪些完全相同、可以直接复用**：Lua 令牌桶脚本、租约预取结构、token server 的「批量发票」模型、WFQ 的权重分配、规则版本化推送。**这些是纯机制，和限的是 QPS 还是 token 无关。**

**差异根因**：Agent 的「一次请求」不是等价的。传统请求 1 次 = 1 个令牌；Agent 一次 Run 可能是 200 token，也可能是 20,000 token。所以：

```
传统：限流键 = tenant + route，每次扣 1
Agent：限流键 = tenant + provider + model，每次扣 estimated_tokens
      而且 estimated_tokens 要先算——这个「算」本身会烧 CPU（第 17 章）
```

Agent 侧的三层配额：

| 层 | 限额 | 例子 | 超限动作 |
| --- | --- | --- | --- |
| 供应商层 | RPM / TPM | 10,000 RPM、2,000,000 TPM | 排队 / failover 到备用供应商 |
| 租户层 | 并发 Run / 日 token | 200 并发、5M token/日 | 429 + 配额告警 |
| 单 Run 层 | 预算 | $0.50 / 200k token | 中断 Run，返回部分结果（第 18 章成本熔断） |

---

## 16.8 会在哪炸：失效模式清单

| 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 扩容后下游被打穿 | 本地限流 × 副本数 | `副本数 × 单机阈值 > 下游容量`？ | 改全局配额或租约 | 配置里强制写「全局值」，单机值由全局值推导 |
| Redis 单分片 CPU 100% | 热 key / 每请求 Lua | `redis-cli --hotkeys`、`INFO commandstats` | 本地租约、逻辑分片、热点隔离 | 压测时按「单租户最大 QPS」单独打 |
| 限流 P99 突然 180ms | Redis 抖动 / AOF rewrite | `LATENCY DOCTOR`、慢查询 | 短超时 + 降级 + 独立分片 | 限流 Redis 与缓存 Redis 物理隔离 |
| 同一秒放行量翻倍 | 客户端时钟回拨 / 固定窗口边界 | 对比各 Pod 时钟、看 1s 粒度曲线 | Redis TIME、令牌桶、收窄 burst | NTP 监控 + 单调时钟 |
| 20 个 Pod 规则不一致 | 推送丢失 / 部分应用 | 各 Pod 的 `rule_version` 指标 | 版本化快照 + 拉取兜底 | 规则变更后校验所有 Pod 版本一致 |
| 网络分区时全站 429 | fail-closed 用在了核心读链路 | 看限流器错误率与 429 曲线 | 按 §16.5 决策表分级 | 故障演练：主动断开 Redis |
| 租约浪费 50% | lease 太大 / TTL 太短 | `quota_lease` 对账 SQL | 调小 L、延长 TTL、归还未用 | 按 `r×T/L` 公式设参 |
| 大租户把池子吃光 | 只有全局阈值，没有租户配额 | 看 top-N 租户 QPS 占比 | 租户配额 + WFQ | 配额上线前做「单租户打满」压测 |

---

## 16.9 怎么验证：压测、故障注入、对账

**① 2 倍阈值压测，重点看 Redis 侧：**

```bash
# 目标全局 10,000 QPS，打 20,000 QPS，持续 120s
echo "GET http://order-query.internal/api/orders?uid=1" | \
  vegeta attack -rate=20000 -duration=120s -timeout=3s | \
  tee /tmp/order.bin | vegeta report

# 同时观察 Redis：Lua 调用量、P99、CPU
redis-cli -h redis-ratelimit.internal INFO commandstats | grep -E 'evalsha|eval'
redis-cli -h redis-ratelimit.internal --latency -i 1
```

期望：拒绝率约 50%，**只有 200 和 429**；Redis `evalsha` 调用量 ≈ 放行 QPS（集中式）或 ≈ QPS/lease（租约）；Redis CPU < 60%。

**② 故障注入（在预发环境做）：**

```bash
# 给 Redis 加 50ms 延迟 + 10ms 抖动，验证限流器是否降级而不是拖死业务
sudo tc qdisc add dev eth0 root netem delay 50ms 10ms
# 观察 5 分钟，然后恢复
sudo tc qdisc del dev eth0 root

# 模拟 Redis 完全不可用（测试环境）
redis-cli -h redis-ratelimit.internal DEBUG SLEEP 5   # 会阻塞，仅测试环境
```

期望：业务 P99 抬升不超过 100ms；限流器在 3 秒内切到本地固定阈值；`ratelimit_degraded=1` 告警触发；没有出现「全放行打穿下游」。

**③ 租约对账 SQL**（见 §16.6），重点看 `leased_tokens / used_tokens`，超过 1.5 说明浪费严重。

**④ 必看指标与 PromQL：**

```promql
# 限流器自身的 P99（不是业务 P99）
histogram_quantile(0.99,
  sum by (le) (rate(ratelimit_check_duration_seconds_bucket[5m])))

# Redis 调用量（集中式应 ≈ 请求量；租约应 ≈ 请求量/lease）
sum(rate(ratelimit_redis_calls_total[1m]))

# 超发估算：租约发放总量 - 实际放行总量
sum(rate(ratelimit_lease_issued_tokens_total[5m]))
  - sum(rate(ratelimit_allowed_total[5m]))

# 降级状态
max by (instance) (ratelimit_degraded)
```

---

## 16.10 用什么工具

| 场景 | 首选 | 理由 / 坑 |
| --- | --- | --- |
| 低 QPS、强公平、计费前置 | Redis + Lua 集中式 | 精确；每请求一次 RTT，QPS 高会打满 Redis |
| 高 QPS、可接受少量超发 | 自研 lease + 本地桶 | Redis QPS 降 100 倍；注意超发上限与租约浪费 |
| Java 生态、要控制台 | Sentinel 集群流控 | 规则管理完整；fallback 本地规则要单独配保守值 |
| Go 单机 | `golang.org/x/time/rate` | 令牌桶标准实现；分布式要自己加 lease |
| 网关层 | Envoy global rate limit / APISIX | Envoy 需要外挂 RLS；APISIX 有 Redis 插件 |
| 多租户公平 | WFQ / DRR + 全局配额 | WFQ 只管分配，不管总量 |

**自研边界**：Lua 脚本、租约、WFQ 都值得自研或封装，因为要和你的配额系统、计费系统打通；**不要自研的是「令牌桶的并发正确性」和「Redis 的高可用」**——前者用成熟库，后者交给云厂商或 Sentinel。

---

## 16.11 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 分布式限流 | 多副本共享一个配额；「每台 1,000」不是分布式限流 |
| 全局值 | 先算下游容量，再除副本数；单机值必须由全局值推导 |
| 集中式 | Lua 原子判定，精度最高；每请求 1 次 RTT，QPS 高会打满 Redis |
| 租约 | 本地桶 + 批量发票；超发 ≤ lease × Pod 数；Redis QPS 降为 QPS/lease |
| 精度公式 | 超发率 = L×N/Q；浪费率 ≈ max(0, 1 - r×T/L) |
| 热 key | 单 key 只能落一个分片；用租约、逻辑分片或独立实例 |
| 时钟 | Redis 用 `TIME`，本地用单调时钟；客户端时间不可信 |
| 网络分区 | 核心写走固定阈值，核心读可全放行，内部批处理 fail-closed |
| 规则 | 版本化快照 + 拉取兜底；禁止逐条推送、半新半旧 |
| 多租户 | 静态配额保底，WFQ 做借用；总量仍要全局封顶 |
| Agent | 键变成 `tenant:provider:model`，单位变成 TPM，多一层供应商配额 |

---

## 16.12 自检题

1. **全局配额 10,000 QPS，20 个 Pod，租约大小 100。最大超发多少？Redis 调用量多少？如果要求超发不超过 5%，租约该多大？**
   <details><summary>答案要点</summary>
   超发上限 = 100 × 20 = 2,000，即 20%；Redis 调用量 = 10,000 / 100 = 100 次/秒。要求 5% 即 500 个，L ≤ 500/20 = 25，取 20（超发 400，4%），Redis 调用量 500 次/秒。**租约越小越准，但 Redis 压力越大**，这就是旋钮。
   </details>

2. **Redis 限流集群主从切换，计数器丢了 3 秒，会发生什么？你怎么把影响限制在可接受范围？**
   <details><summary>答案要点</summary>
   主从异步复制，切换后新主可能少了最后几秒的扣减，导致短时间超发；如果限流 key 是热 key，3 秒 × 10,000 QPS = 30,000 个额外请求。限制影响：① 本地租约兜底，租约本身有上限；② 切换期间降级到本地固定阈值；③ 下游连接池/熔断做硬墙；④ 接受「限流是概率性保护」，不要指望它精确到个位。
   </details>

3. **一个租户在晚高峰打到 9,000 QPS，Redis 单分片 CPU 100%，但全局配额只用了 40%。你怎么在 30 分钟内止血，又不在下周复发？**
   <details><summary>答案要点</summary>
   止血：把该租户切到本地租约（或逻辑分片），把 Redis 调用从 9,000/s 降到 90/s；同时把它的 burst 收窄，防止分片后瞬时突刺。复发：按「单租户最大 QPS」做压测，限流 key 按租户哈希打散到多个分片，热点租户独立实例；监控 `redis_cpu_by_key_prefix` 与 top-N 租户 QPS。
   </details>

---

## 16.13 延伸阅读

- Redis Lua 脚本与 `EVALSHA`：Redis 官方 `EVAL` / `SCRIPT LOAD` 文档，注意脚本内 `TIME` 的确定性
- Sentinel 集群流控：`sentinel-cluster-client-default` / `sentinel-cluster-server-default`，以 1.8.x 文档为准
- Envoy `envoy.filters.http.ratelimit`（RLS）与 `local_ratelimit` 的差异
- WFQ / DRR：*Computer Networks*（Tanenbaum）调度章节；Linux `tc fq` 实现
- 上一章：[第 15 章 限流的本质、算法与层次](15-rate-limit-basics.md)
- 下一章：[第 17 章 限流自身的故障：当限流把 CPU 打满](17-rate-limit-self-failure.md)

**交叉引用**：总纲 [0-summary.md](0-summary.md) ｜ 语言与运行时 [../go/0-summary.md](../go/0-summary.md) ｜ 平台治理 [../llm/0-summary.md](../llm/0-summary.md)
