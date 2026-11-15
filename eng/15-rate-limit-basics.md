# 15. 限流的本质、算法与层次

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 流量与韧性：不崩是怎么做到的 ｜ **编号**：15 / 52
> **本章定位**：先把「限流保护谁、限在哪一层、拒绝之后给用户什么」这三件比算法重要十倍的事讲清楚，再把四种算法的边界效应算给你看。算法本身 20 行代码就写完了，写不完的是判断。
> **读法**：急着上线只看 §15.4 第一张表也行；但请先读 §15.2——九成的限流事故不是算法选错，而是**保护对象选错**。

---

## 15.1 引子：48,000 QPS 打进来，下游先死，用户也没好

2024-11-11 00:04，某电商首页「猜你喜欢」接口。

这个接口是个薄薄的 BFF 层：自己不查库，只把个性化排序服务 P 的结果兜一下再返回。出事前一周的平均数据是 BFF CPU 18%、P 的 P99 80ms。所有人都觉得它"很轻，不会有事"。

- 00:04:12 大促流量进来，BFF 从 8,000 QPS 涨到 **48,000 QPS**（6 倍）。
- P 的容量是可用连接数除以单次耗时：**200 ÷ 0.015s ≈ 13,300 QPS**。48,000 QPS 意味着需要 **720 个请求同时在飞**，是它能承受的 3.6 倍。
- 00:04:50 P 的连接池 100% 占满，BFF 侧的调用线程全部阻塞在 `getConnection()` 上；Tomcat 线程（200 线程 × 8 实例 = 1,600）也见底。
- P99 从 80ms → **12.4s**。客户端 3s 超时，于是**重试**，有效 QPS 又乘 1.8 → 86,000。
- 连带伤害：订单详情页也调 P（复用同一份推荐），一起挂。下单转化率掉 **62%**。
- 00:18 恢复。唯一有效的动作是人工在网关把 BFF 路由限到 **10,000 QPS**，两分钟后全链路自愈。

事后算账：14 分钟 × 平均 3,200 单/分钟 × 折算，GMV 损失约 **380 万元**，另有 11 万条客诉进线。

> 🩸 **最刺眼的一点**：BFF 自己 CPU 只有 18%，它不是被自己打死的，是被「帮别人转发」打死的。限流没配在这里，不是忘了，而是所有人都默认"轻服务不用限流"。

---

## 15.2 到底是什么：限流限的是**资源**，不是用户

**定义**：限流 = 在单位时间内对**某个稀缺资源**的消耗速度设置上限，超出的部分按预定策略处理（拒绝 / 排队 / 降级）。

注意定义里的两个词。**「资源」**决定限流值，**「策略」**决定用户体验。前者是数学，后者是产品决策。用户从来不是限流对象——用户是限流的**受害者或者是受益者**，取决你限得对不对。

四个常见反例：

| 你以为的限流 | 为什么不是 | 不做会怎样 |
| --- | --- | --- |
| 「入口拦住 50% 用户就安全了」 | 拦掉一半后仍剩 24,000 QPS，还是 13,300 的 1.8 倍 | 下游照样死，而你还多损失了一半正常用户 |
| 「限流值拍个大数，100,000 QPS」 | 这个数字大于任何下游容量，等于没限 | 出事时它不会触发，只会在复盘文档里出现 |
| 「全部拒绝最安全」 | 那是下线，不是限流 | 100% 错误率，SLO 直接违约 |
| 「加机器就能扛」 | 机器能加，下游连接池、DB 连接数、三方配额加不了 | 越扩越死，因为并发被放大得更快 |

**限流值必须从下游反推**，公式只有一行：

```
单个副本的限流值 = min(连接池大小 ÷ 单次下游耗时,
                      DB 连接数 ÷ 单次占用时间,
                      三方配额 ÷ 实例数) × 安全系数(0.7~0.8)
```

代进 15.1 的数字：`200 ÷ 0.015 = 13,333`，`13,333 × 0.75 ≈ 10,000 QPS`。
事故里人工拍的 10,000，和这个算式的结果一模一样——**不是巧合，是这个数本来就只能这么算出来**。凡是你答不出「这个限流值是怎么算的」，它就一定会出事。

> 💡 判断一个限流值合不合格，只问一句：**「这个数被突破时，谁先死，死得多快？」** 答不出来就说明它在保护自己，而不保护任何东西。

---

## 15.3 结构图：限流应该长在整条链路上

```
  客户端
    │  48,000 QPS
    ▼
┌─────────────┐  按 IP / 租户 / 路由 粗粒度，10,000 QPS
│  网关/WAF   │  ← 第 1 道：挡住明显恶意与超大客户
└─────────────┘
    │ 10,000 QPS
    ▼
┌─────────────┐  按接口 + 调用方 细粒度，8,000 QPS（给重试留 20% 余量）
│ BFF / 服务  │  ← 第 2 道：真正算得清下游容量的一层
└─────────────┘
    │ 8,000 QPS
    ▼
┌─────────────┐  按具体方法/资源（热点商品、热点用户）
│ 方法/资源级 │  ← 第 3 道：Sentinel hotspot、单 key 防击穿
└─────────────┘
    │ 720 并发上限
    ▼
┌─────────────┐  连接池 200、HikariCP maximumPoolSize、max_connections
│  DB / 下游  │  ← 第 4 道：这才是硬约束，前面全是软约束
└─────────────┘
    │
    ▼   三方 API 配额（被动，只能靠"留余量 + 配额对账"）

三个出口（同一层内三选一）：
  超限 ──► ① 拒绝：429 + Retry-After，最便宜，用户明确可重试
        ──► ② 排队：有界队列换吞吐，队列长度就是最大延迟
        ──► ③ 降级：走缓存 / 简化逻辑 / 小模型，功能缺失但服务不断
```

---

## 15.4 怎么实现：四种算法与它们的边界效应

| 算法 | 允许突发 | 内存 | 精度 | 受时钟回拨影响 | 边界效应（关键） | 典型实现 |
| --- | --- | --- | --- | --- | --- | --- |
| 固定窗口计数 | 是（很危险） | O(1) | 低 | 是 | 窗口交界处可放 **2 倍**流量 | Redis `INCR`+`EXPIRE` |
| 滑动窗口（分格近似） | 部分 | O(格数) | 误差 ≤ 1 格 | 是 | 10 格 × 100ms → 误差最多 10% | Sentinel `LeapArray` |
| 滑动窗口（精确日志） | 部分 | **O(QPS×窗口)** | 精确 | 否 | 10,000 QPS×1s = 1 万条记录/键 | 少用，只在低 QPS 关键链路 |
| 令牌桶 | **是（可控）** | O(1) | 精确 | 是（按时间差补桶） | 闲置后瞬时放满 burst | Go `x/time/rate`、Guava `RateLimiter` |
| 漏桶（匀速排队） | 否 | O(队列) | 精确 | 是 | 队列长度直接等于最大排队延迟 | Sentinel `RateLimiterController`、Nginx `limit_req` |

**把边界效应算出来**（不算就等于没懂）：

- **固定窗口，阈值 1,000/s**：窗口最后 20ms 放 999 个，下个窗口前 20ms 再放 999 个 → **40ms 内放行 1,998 个**，瞬时 5 万 QPS。下游要是按 1,000 配的连接池，这一下就打死。这就是为什么「明明限了 1,000 还是被打死」。
- **滑动窗口 10 格**：同上场景最坏放 1,000 + 100 = 1,100，误差 **10%**，所以安全系数不能低于 0.9，我们用 0.75 已经很保守。
- **令牌桶 rate=1,000/s、burst=2,000**：闲置 10s 后攒满 2,000 个令牌，请求一来 1 秒内放 2,000 个 → 这 1 秒是 **2 倍速率**。所以 `burst` 必须 ≤ 下游能承受的「10 秒级瞬时并发」，不是越大越好。
- **漏桶 rate=1,000/s、队列 2,000**：队尾请求要等 **2s** 才被处理，而客户端 3s 超时——那 2s 里它占着线程、占着连接、等着一个大概率被丢弃的响应。**队列容量必须用「客户端超时时间」反推**，不是拍脑袋给个 2,000。

### Go：令牌桶 + 中间件（生产可用版）

```go
package ratelimit

import (
	"net/http"
	"sync"
	"time"

	"golang.org/x/time/rate"
)

// 每个租户/接口一把桶，键由业务决定（租户ID+接口 或 单实例全局）
type Limiter struct {
	mu       sync.RWMutex
	buckets  map[string]*rate.Limiter
	r        rate.Limit // 每秒令牌数 = 15.2 算出来的限流值 / 副本数
	burst    int        // 允许的瞬时突发，≤ 下游 10 秒级瞬时容量
	lastSeen map[string]time.Time
}

func NewLimiter(perSec float64, burst int) *Limiter {
	return &Limiter{
		buckets:  make(map[string]*rate.Limiter),
		r:        rate.Limit(perSec),
		burst:    burst,
		lastSeen: make(map[string]time.Time),
	}
}

func (l *Limiter) Allow(key string) bool {
	l.mu.Lock()
	lim, ok := l.buckets[key]
	if !ok {
		lim = rate.NewLimiter(l.r, l.burst)
		l.buckets[key] = lim
	}
	l.lastSeen[key] = time.Now()
	l.mu.Unlock()
	// 注意：Allow 不加锁，靠 rate.Limiter 内部原子操作
	return lim.Allow()
}

// 回收空闲桶，否则 map 会随租户数无限增长（这是最常见的泄漏）
func (l *Limiter) GC(idle time.Duration) {
	now := time.Now()
	l.mu.Lock()
	defer l.mu.Unlock()
	for k, t := range l.lastSeen {
		if now.Sub(t) > idle {
			delete(l.buckets, k)
			delete(l.lastSeen, k)
		}
	}
}

func Middleware(l *Limiter, keyFn func(*http.Request) string) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if !l.Allow(keyFn(r)) {
				w.Header().Set("Retry-After", "1") // 告诉客户端等多久，别裸奔重试
				w.WriteHeader(http.StatusTooManyRequests)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}
```

### Java：Sentinel 声明式限流（含拒绝/排队两种出口）

```java
import com.alibaba.csp.sentinel.slots.block.RuleConstant;
import com.alibaba.csp.sentinel.slots.block.flow.FlowRule;
import com.alibaba.csp.sentinel.slots.block.flow.FlowRuleManager;
import com.alibaba.csp.sentinel.slots.block.flow.controller.WarmUpRateLimiterController;

@Configuration
public class SentinelRules {

    @PostConstruct
    public void init() {
        FlowRule rule = new FlowRule();
        rule.setResource("reco:query");                 // 资源名 = 接口/方法
        rule.setGrade(RuleConstant.FLOW_GRADE_QPS);
        rule.setCount(8000);                            // 15.2 算出来的值，不是拍的
        // 出口①：直接拒绝（最便宜，推荐默认）
        rule.setControlBehavior(RuleConstant.CONTROL_BEHAVIOR_DEFAULT);

        FlowRule queueing = new FlowRule();
        queueing.setResource("export:report");          // 低优先级、可等的接口才排队
        queueing.setGrade(RuleConstant.FLOW_GRADE_QPS);
        queueing.setCount(50);
        // 出口②：匀速排队，maxQueueingTimeMs 必须 < 客户端超时
        queueing.setControlBehavior(RuleConstant.CONTROL_BEHAVIOR_RATE_LIMITER);
        queueing.setMaxQueueingTimeMs(800);             // 客户端 3s 超时，留 2 倍余量

        FlowRuleManager.loadRules(List.of(rule, queueing));
    }
}
```

```yaml
# Nginx 网关层：rate=1000r/s 是桶速率，burst=2000 是队列/突发，nodelay 表示不排队直接放行
limit_req_zone $binary_remote_addr zone=api_per_ip:10m rate=1000r/s;
server {
  location /api/ {
    limit_req zone=api_per_ip burst=2000 nodelay;
    limit_req_status 429;                 # 默认 503，会污染错误率看板
    add_header Retry-After 1 always;
  }
}
```

> ⚠️ **Nginx 的 `limit_req` 默认返回 503**。503 在监控里属于「服务端错误」，你会看到错误率飙升然后判断"服务挂了"，实际是限流生效。永远显式设置 `limit_req_status 429`。

---

## 15.5 限流该在几层：职责划分

| 层次 | 粒度 | 它能拦什么 | 它拦不住什么 | 配错的代价 |
| --- | --- | --- | --- | --- |
| 网关 | IP / 租户 / 路由 | 恶意刷、超大客户、单 IP 打爆 | 业务级成本差异（一次搜索 vs 一次导出） | 按 IP 限 → NAT 后整个网吧被误伤 |
| 服务（接口） | 接口 + 调用方 | 单接口流量失控、重试风暴 | 同一接口内部热点（一个商品被疯狂查询） | 限流值拍大盘平均值 → 与真实容量无关 |
| 方法/资源级 | 单 key（用户/商品/单据） | 热点 key 击穿、单用户刷接口 | 全局容量 | 键设计错 → 桶数爆炸，内存涨到 OOM |
| 数据库/连接池 | 连接数、`max_connections` | 真正的硬约束，最后一道物理墙 | 上游无效排队（请求已经进来了） | 池子给太小 → 吞吐掉；给太大 → DB 打挂 |
| 三方 API | 配额（每分钟/每天） | 超额计费、被供应商拉黑 | 你自己的容量 | 全配额给一个功能 → 其他功能集体 429 |

**结论：至少两层，且必须是「网关粗 + 服务细」。** 只有网关 = 业务不知道自己的容量；只有服务 = 恶意流量已经进到集群内部，把线程和连接先吃掉了。

### 三个出口的体验差异

| 出口 | 用户看到 | 资源成本 | 适用 | 关键参数 |
| --- | --- | --- | --- | --- |
| 拒绝 | 429 + 「稍后再试」，1 秒后重试成功 | 最低（1 次内存判断） | 秒杀、抢购、可重试的查询 | `Retry-After`，且客户端要退避 |
| 排队 | 「加载中」，1~3s 后有结果或超时 | 中（占线程/连接，队列本身也要内存） | 报表导出、批处理、异步任务 | 队列长度 × 服务速率 < 客户端超时 |
| 降级 | 「当前展示热门推荐」，功能缺失但页面正常 | 低（走缓存）+ 需维护兜底数据 | 推荐、榜单、评论、非核心信息 | 兜底数据的新鲜度与覆盖率 |

> 🩸 **最坏的一种做法是「无限队列」**：它看起来最友好（不丢请求），实际是把限流转成了**延迟放大器**。15.1 的场景里，如果 P 前面是个无界队列，48,000 QPS 有 34,700 QPS 进队列，每秒积压 34,700 个请求，客户端 3s 超时——每 3 秒白烧 10 万个已经没人在等的请求的内存和线程。**排队必须是「有界队列 + 超时丢弃」，否则等于欠债。**

---

## 15.6 双修对照：限的是资源，不是 QPS

| 维度 | 传统系统 | Agent 系统 | 同 or 异 |
| --- | --- | --- | --- |
| 机制本体 | 令牌桶 / 滑动窗口 / 漏桶 | **完全相同**，算法一行不改就能用 | **同（可直接复用）** |
| 计量单位 | QPS、并发连接数、线程数 | **TPM**（token/分钟）、并发 Run 数、步数、单 Run 预算 | **异**——单位不同，限流值必须重算 |
| 限的是哪个资源 | 线程 → 连接池 → DB 连接 → 下游 QPS | token 速率 → 模型并发 → 供应商配额 → 单 Run 预算 | **异（三定律之二：换资源）** |
| 身份/键 | IP、租户、接口、商品 ID | 租户、模型+供应商、`run_id`、Agent 角色 | 同构；键的组成多一层「模型/提示词版本」 |
| 成本结构 | 近似固定（机器折旧） | **按次显性计费**，限流值即预算闸门 | **异（根因：成本显性且按量）** |
| 失败模式 | 限太松 → 下游打死；限太紧 → 误杀好流量 | 多一类：「按请求数限流」而忽略 token 长度，长上下文请求照样烧穿预算 | 多一类 |
| 验证方法 | 2× 阈值压测看拒绝率、看下游 P99 | 同 + **token 计量对账**（预算 vs 实际计费） | 同 + 一层对账 |

**哪些完全相同、可以直接复用**：四种算法的实现（`x/time/rate`、Sentinel、Redis 令牌桶）、三层限流结构、三个出口的设计、`Retry-After` 语义。**一行代码都不用改，只需要换掉"每秒多少"这个输入。**

**差异根因只有一个**：传统系统的"每秒多少"是个**请求数**，Agent 的"每秒多少"是**token 数**，而**请求数是你在入口就知道的，token 数你要先数一遍**——这个差异会在第 17 章变成 CPU 事故。

Agent 侧的限流值怎么算：

```
TPM 上限 = 供应商 RPM 配额 × 平均每请求 token 数
例：GPT-4o 级配额 10,000 RPM，平均每 Run 3 步 × 2,500 token = 7,500 token/Run
  → 每日 Run 上限 ≈ 10,000 RPM ÷ 3 次调用/Run × 1,440 分钟 = 4.8M Run/天
  → 但预算先撞墙：4.8M Run × 7,500 token × 单价 → 通常预算比配额先到
```

> 详见第 17 章（限流器自身打满 CPU）、第 16 章（分布式限流的精度损失）。

---

## 15.7 会在哪炸：失效模式清单

| 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 「明明限了 1,000 还是被打死」 | 固定窗口边界效应，瞬时 2 倍 | 看 1 秒粒度的下游 QPS 尖刺 | 换滑动窗口或令牌桶，burst 收窄 | 限流值 = 容量 × 0.75，禁止给满 |
| 扩容后限流值翻倍，一下超过下游容量 | 单实例限流（每实例 1,000，扩到 20 实例 = 20,000） | `实例数 × 单实例限流值 > 下游容量`？ | 改集群限流，或按实例数动态分票 | 限流值配置里写「按副本数取模」的注释与校验 |
| 错误率看板飙红，但服务其实健康 | 限流返 503/500 而非 429 | 看响应码分布 | 统一 429 + `Retry-After` | 加一条「非 429 的 5xx 才算错误」的 SLO 规则 |
| 一个客户投诉「突然全公司不能用」 | 按 IP 限流，客户在 NAT 后 | 看被拒 IP 的请求来自多少账号 | 改成按租户/账号限流 | 分级限流：先账号，再租户，最后 IP |
| 限流触发后 QPS 反而更高 | 客户端重试 + 无退避 | 看上游 QPS 与你放行 QPS 的比值（>1.5 就是重试放大） | 下发 `Retry-After` + 客户端指数退避 + 抖动 | 重试预算 ≤ 原流量的 20% |
| 队列越排越长，延迟 P99 直线上升到超时 | 无界队列 | 看队列深度与等待时间 | 有界队列 + 超时丢弃 | 队列容量按「客户端超时 × 服务速率」反推 |
| 单 key 热点没挡住 | 只在接口层限流 | 看 top-N key 的 QPS | 方法/资源级热点限流（Sentinel hotspot） | 热点 key 自动探测 + 本地缓存 |

---

## 15.8 怎么验证：压测、边界、观测

**① 2 倍阈值压测**（验证拒绝率与下游是否稳）：

```bash
# 目标：限流 10,000 QPS，打 20,000 QPS，期望拒绝率 ≈ 50%，且下游 P99 不抬升
echo "GET http://bff.internal/api/reco?uid=1" | vegeta attack \
  -rate=20000 -duration=120s -timeout=3s | tee /tmp/reco.bin | vegeta report
vegeta plot /tmp/reco.bin > /tmp/reco.html
```

期望：`Success [ratio] ≈ 50%`，`Status Codes` 里 **只有 200 和 429**（出现 5xx 说明修错了），下游 P99 保持在 80ms 附近。

**② 边界效应专项**：用两个相隔 1ms 的突发打固定窗口实现，观察 40ms 内被放行的总量是否 ≤ `burst + rate×0.04`。

**③ 重试放大专项**：客户端开启 3 次重试 + 无退避，压 5,000 QPS，看网关入口实际收到多少 QPS（应 ≈ 5,000，若 ≈ 15,000 说明限流点选在了重试之后）。

**④ 必看的四个指标**：

| 指标 | 含义 | 告警线 |
| --- | --- | --- |
| `ratelimit_rejected_total{resource,key_type}` | 被拒请求数 | 按资源算拒绝率 > 5% 持续 2min |
| `ratelimit_pass_total` / `request_total` | 放行比 | 放行比突然从 0.98 掉到 0.5 |
| `inflight_requests` | 在飞请求数 | > 连接池 80% |
| `downstream_p99` | 下游延迟 | 抬升 2 倍即限流值算错 |
| `retry_amplification` = 入口 QPS ÷ 放行 QPS | 重试放大系数 | > 1.3 |

---

## 15.9 用什么工具

| 场景 | 首选 | 理由 / 坑 |
| --- | --- | --- |
| Java 服务限流 | Sentinel（`FlowRule` + hotspot） | 自带滑动窗口、控制台、降级熔断；注意规则推送需要持久化（见第 16 章） |
| Java 单机轻量 | Guava `RateLimiter` 或 Resilience4j `RateLimiter` | Guava 是令牌桶、**阻塞式**，别在 Web 线程直接 `acquire()` |
| Go 服务限流 | `golang.org/x/time/rate` | 令牌桶、`Allow()/Wait(ctx)` 两种语义；`Wait` 必须带 ctx 超时 |
| 网关限流 | Nginx `limit_req` / Envoy `local & global rate limit` / APISIX | Nginx 是漏桶 + burst；Envoy 分布式需外挂 RLS |
| 分布式精确计数 | Redis + Lua / Redis Cell（`CL.THROTTLE`） | 每次请求一次往返，QPS 高时会打满 CPU——见第 17 章 |
| 集群流控 | Sentinel token server | 精度高、多一次 RPC；见第 16 章 |

**自研边界**：单机令牌桶别自研（`x/time/rate` 已经处理了时钟、并发、预占）；**要自研的是「限流值怎么算」和「超限之后干什么」**——这两件事没有任何框架能替你做。

---

## 15.10 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 保护对象 | 限流保护的是**最贵最稀缺的下游资源**，不是用户。用户是被限的一侧 |
| 限流值来源 | `连接池 ÷ 单次耗时 × 0.75`，必须能算出来；算不出来的限流值一定出事 |
| 固定窗口 | 边界处瞬时 2 倍。这就是「明明限了还是被打死」的答案 |
| 令牌桶 | `burst` 决定闲置后能放多少，必须 ≤ 下游瞬时容量 |
| 漏桶/排队 | 队列长度 ÷ 服务速率 = 最大排队延迟，必须 < 客户端超时 |
| 层次 | 至少两层：网关粗（防恶意）+ 服务细（懂容量）。DB 连接池是最后一道物理墙 |
| 三个出口 | 拒绝最便宜、排队换吞吐、降级保功能；**无限队列 = 延迟放大器** |
| 状态码 | 限流必须返回 429 + `Retry-After`，别用 503 污染错误率 |
| 重试 | 限流 + 无退避重试 = 有效 QPS ×1.8。重试预算 ≤ 20% |
| 双修 | 算法完全复用；单位从 QPS 换成 TPM，**限流值必须重算** |
| Agent 额外坑 | 按请求数限流会漏掉长上下文，必须按 token 限 |

---

## 15.11 自检题

1. **下游连接池 200、单次下游耗时 20ms，你的服务 8 副本，每个副本该配多少 QPS 的限流？为什么不能配 10,000？**
   <details><summary>答案要点</summary>
   单副本容量 = 200 ÷ 0.02 = 10,000 QPS；× 0.75 安全系数 = 7,500；再乘 8 副本是集群容量，但**这是"单副本能扛"而非"单副本该配"**——如果下游是每个副本独立连接池，单副本 7,500 合理；如果下游是**共享**的一个服务（200 连接是它自己的池），那 8 副本合起来不能超过 10,000×0.75，单副本只能配 937。**必须先问清"这 200 连接是谁的"**，这正是本章反例的核心。
   </details>

2. **限流阈值 1,000/s 用固定窗口实现，为什么监控上看到 40ms 内放行了 1,998 个请求？怎么修？**
   <details><summary>答案要点</summary>
   窗口交界效应：上一窗口末尾与下一窗口开头的计数互不可见。修法三选一：换滑动窗口（误差 ≤ 1 格）、换令牌桶并把 burst 收窄到 ≤ 下游瞬时容量、或把阈值降到容量的 50%。**不要只调大窗口**，那只是把 2 倍摊薄成 1.5 倍，问题还在。
   </details>

3. **限流触发后，你看到入口 QPS 从 10,000 涨到 16,000，服务更卡了。最可能的原因与最小修复动作？**
   <details><summary>答案要点</summary>
   客户端重试无退避导致重试风暴（放大 1.6 倍）。最小修复：限流响应带上 `Retry-After`，客户端改指数退避 + 抖动，并把重试预算压到原流量 20% 以内。同时检查限流点是否在重试之后（网关在客户端重试之前才拦得住）。
   </details>

---

## 15.12 延伸阅读

- Sentinel `FlowRule` / `LeapArray`（滑动窗口实现）：`com.alibaba.csp.sentinel.slots.block.flow`，以 1.8.x 文档为准
- Go 标准扩展 `golang.org/x/time/rate` 源码 `limiter.go`（`reserveN` 的预占逻辑值得逐行读）
- Nginx `ngx_http_limit_req_module` 文档中的 `burst` / `nodelay` 语义
- Envoy `envoy.filters.http.local_ratelimit` 与 `ratelimit`（RLS）对比
- 上一章：[第 14 章 对账体系](14-reconciliation-system.md)
- 下一章：[第 16 章 分布式限流的实现与坑](16-distributed-rate-limit.md)

**交叉引用**：总纲 [0-summary.md](0-summary.md) ｜ 语言与运行时 [../go/0-summary.md](../go/0-summary.md) ｜ 平台治理 [../llm/0-summary.md](../llm/0-summary.md)