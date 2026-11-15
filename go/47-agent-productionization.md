# 47. Agent 的生产化：多租户、限流与成本

> **状态**：`已完成（初稿）` ｜ **所属**：第七部分 · AI-Agent 应用架构 ｜ **编号**：47 / 54
> **本章定位**：Agent 上生产后，**第一个把你叫醒的从来不是"回答质量"，而是账单和 429**。这一章把"闸门"一节一节地装上：配额、限流、熔断、缓存、预算刹车、版本灰度、评估、合规、SLO。
> **读法**：47.2 的闸门图先建立顺序感；47.3.1~47.3.5 是必须落地的五道闸；后面四节是"上规模才痛"的部分。限流原语本身的实现（令牌桶/漏桶）详见第 32 章，本章只讲 Agent 特有的变异。

---

## 47.1 引子：三个凌晨告警

**02:14** 某租户的 Agent 陷入工具调用死循环，一夜烧掉 1.2 亿 token。没有配额、没有预算、没有单会话步数上限——监控只告诉你「QPS 正常，延迟正常」，成本是一张事后账单。

**03:41** 上游模型供应商开始返回 429，重试风暴把自家网关也打满了，其他所有租户一起超时。**一个供应商的抖动变成了全站故障**——因为没有任何熔断，所有流量还在往同一条路上挤。

**09:30** 有人改了 system prompt 里的两个字，离线评估下降 12%，但没人拦——prompt 硬编码在代码里，改动随发布一起上，回滚要等半小时构建。

三件事对应三样东西：**预算刹车、熔断降级、prompt 版本化 + 灰度**。缺一样，你就等着这三条告警循环播放。

---

## 47.2 直觉模型：一次请求要过六道闸

```
 user req
    │
    ▼
[0] 认证/租户解析   tenant_id, plan, quota
    │
    ▼
[1] 配额闸  ────── 月/日 token 配额、并发会话数     ← Redis 原子扣减
    │ 超 → 429 quota_exceeded（附 reset 时间）
    ▼
[2] 限流闸  ────── 单机令牌桶 → Redis 分布式桶 → 并发信号量
    │ 超 → 429 + Retry-After
    ▼
[3] 熔断闸  ────── 供应商健康度；半开探测
    │ 断 → 降级路由（备用模型 / 缓存答案 / 固定话术）
    ▼
[4] 缓存闸  ────── 精确 hash 命中 → 直接返回；语义缓存（谨慎）
    │ miss → 继续
    ▼
[5] 预算闸  ────── 预扣 token 额度，结束时结算        ← 花超了立刻停
    │
    ▼
 LLM / 工具执行（带 ctx 超时、重试、span）
    │
    ▼
[6] 出口：内容过滤 · PII 脱敏 · 指标/账本/审计
```

**顺序不能乱**：便宜的先拦（内存判断 < Redis < 网络调用）。把配额放最后，等于让已经花了钱的请求再被拒。

| 闸门 | 拦什么 | 数据结构 | 超限语义 | 谁来观察 |
| --- | --- | --- | --- | --- |
| 配额 | 长周期超额（月账单） | Redis Hash/计数器 | 429，不可重试，提示购买/等待 | 财务 + 客户成功 |
| 限流 | 短时速率（QPS/Burst） | 令牌桶 / 滑动窗口 | 429 + `Retry-After` | SRE |
| 并发信号量 | 在途请求数（打满上游） | `x/sync/semaphore` | 排队或快速失败 | SRE |
| 熔断 | 上游持续失败 | 状态机（closed/open/half-open） | 降级而非报错 | SRE |
| 缓存 | 重复计算 | Redis/本地 LRU | — | 成本 |
| 预算刹车 | 单请求/单会话超支 | 计数 + 预扣 | 优雅截断输出 | 产品 + 财务 |
| 内容过滤 | 违规进/出 | 分类器 / 规则 | 拒答 / 替换 | 合规 |

---

## 47.3 底层实现

### 47.3.1 多租户与配额：租户是一等结构体

```go
type Tenant struct {
	ID           string
	Plan         string           // free / pro / enterprise
	TokenQuota   int64            // 每月 token 配额
	RPM          int              // requests per minute
	MaxConcurrent int             // 同时在途请求上限
	AllowedModels []string        // 模型白名单（分级定价）
	CtxWindow     int             // 允许的最大上下文（防烧钱）
}
```

三条设计判断：

1. **配额维度选 token，不选请求数**。请求数好算但不代表成本；token 才是账单单位。
2. **"公平"要在租户粒度上做**：一个租户的并发信号量是独立的，否则大租户能把所有槽吃掉（这个问题在 agent 场景尤其严重，因为单请求可能跑几十秒）。
3. **配额放 Redis**，按 `{tenant}:quota:{yyyymm}` 计数，`INCRBY` + `EXPIRE`（或用 PG 做权威、Redis 做快速判断，定期对账）。

### 47.3.2 限流三件套：单机 → 分布式 → 并发

**① 单机令牌桶**（`golang.org/x/time/rate`）：为什么要它？因为 Redis 有限流之外的延迟成本，第一层用内存挡住 90% 的浪涌：

```go
type localLimiter struct {
	mu   sync.RWMutex
	buckets map[string]*rate.Limiter // tenant -> limiter
}

func (l *localLimiter) allow(t *Tenant) bool {
	l.mu.RLock()
	lim, ok := l.buckets[t.ID]
	l.mu.RUnlock()
	if !ok {
		l.mu.Lock()
		lim = rate.NewLimiter(rate.Limit(t.RPM)/60.0, t.RPM/10) // 每秒速率 + burst
		l.buckets[t.ID] = lim
		l.mu.Unlock()
	}
	return lim.Allow() // 或 lim.Reserve() 拿 Delay 做 Retry-After
}
```

> ⚠️ 这个 map 是**进程内**的，且**永不清理**。租户有几十万个就是内存泄漏——必须加 LRU + 定期淘汰（重启后窗口重置，可接受；毕竟它只是第一道粗筛）。

**② 分布式令牌桶（Redis + Lua）**：跨副本时一致性口径的唯一选择：

```go
// 经典 token bucket：hash 存 tokens + last_refill
var luaTokenBucket = redis.NewScript(`
local key   = KEYS[1]
local now   = tonumber(ARGV[1])      -- 毫秒，由 Go 侧传入（不要用 redis TIME，便于测试）
local rate  = tonumber(ARGV[2])      -- tokens per second
local burst = tonumber(ARGV[3])
local cost  = tonumber(ARGV[4])

local t = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(t[1])
local ts = tonumber(t[2])
if tokens == nil then tokens = burst; ts = now end

-- 按流逝时间补充
tokens = math.min(burst, tokens + (now - ts) / 1000 * rate)
local allowed = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', key, math.ceil(burst / rate * 1000) + 1000)
local waitMs = 0
if allowed == 0 then
  waitMs = math.ceil((cost - tokens) / rate * 1000)
end
return {allowed, math.floor(tokens), waitMs}
`)
```

> 🩸 **坑**：`now` 一定用 Go 侧时间传入，并且**集群内 NTP 要对齐**；用 `redis.call('TIME')` 看似权威，但同一脚本里多次调用会拿到不同值，也不好测。另外 Redis Cluster 下这个 key 必须单 key（天然满足），不要和配额 key 拼到一个脚本里，否则要 hash tag。

**③ 并发信号量**：Agent 的请求不是"毫秒级"，是"几十秒"，所以**并发数比 QPS 更能保护上游**：

```go
// 每租户一个加权信号量：重量级（长上下文/大模型）占更多权
sem := semaphore.NewWeighted(int64(t.MaxConcurrent))
if err := sem.Acquire(ctx, int64(weight)); err != nil {
	return nil, err // ctx 超时 → 快速失败，别无限排队
}
defer sem.Release(int64(weight))
```

`semaphore` 的好处是**支持 ctx 取消**，天然给你"排队超时就放弃"的语义，比 `chan struct{}` 好一个档次（第 26 章）。

### 47.3.3 熔断降级与降级模型

熔断不是"出错就断"，而是**状态机**：`closed`（正常）→ 连续失败到阈值 → `open`（快速失败/降级）→ 冷却期后 `half-open`（放一个探测）→ 成功则回 `closed`。

```go
import "github.com/sony/gobreaker"

var cb *gobreaker.CircuitBreaker

func init() {
	cb = gobreaker.NewCircuitBreaker(gobreaker.Settings{
		Name:        "openai-primary",
		MaxRequests: 3,               // half-open 时允许的探测数
		Interval:    30 * time.Second, // closed 状态下统计窗口清零
		Timeout:     20 * time.Second, // open 持续时长
		ReadyToTrip: func(c gobreaker.Counts) bool {
			// 失败率 > 50% 且样本足够，才断：样本太少容易误判
			return c.Requests >= 20 && float64(c.TotalFailures)/float64(c.Requests) > 0.5
		},
	})
}

func (c *Client) Chat(ctx context.Context, req Request) (Resp, error) {
	out, err := cb.Execute(func() (any, error) { return c.primary.Chat(ctx, req) })
	if err == nil {
		return out.(Resp), nil
	}
	// 降级阶梯：备用供应商 → 小模型 → 缓存答案 → 固定话术
	if isOpen(err) {
		if r, e2 := c.backup.Chat(ctx, degrade(req)); e2 == nil {
			return r, nil
		}
	}
	return Resp{}, err
}
```

| 降级层级 | 手段 | 质量损失 | 成本 | 适用 |
| --- | --- | --- | --- | --- |
| L1 | 备用供应商（同模型异构部署） | 几乎无 | 略高 | 主供应商区域性故障 |
| L2 | 小模型 + 更短上下文 | 中 | 低 | 高峰期削峰 |
| L3 | 语义/精确缓存命中 | 低（陈旧风险） | ~0 | 重复问题多的场景 |
| L4 | 模板话术 + 工单转人工 | 高但可控 | ~0 | 兜底，永不让用户看到 500 |

> 💡 `gobreaker` v2 改成了泛型 API（`gobreaker.NewCircuitBreaker[T]`），v1 是 `interface{}`。选版本前先看团队已经用了哪套，别在一个仓库里混两个大版本。

### 47.3.4 缓存分层与 prompt cache

三层缓存，收益递减、风险递增：

1. **L1 进程内 LRU**（毫秒级）：只缓存**确定性输出**（embedding 结果、工具查询结果、模板渲染结果）。别缓存对话回答。
2. **L2 Redis 精确缓存**：key = `sha256(model + prompt_version + 温度 + 规范化消息)`。命中率取决于业务（FAQ 类常见 30%+）。
3. **L3 语义缓存（谨慎）**：用 query 的 embedding 做近邻，相似度 > 0.95 直接返回旧答案。风险是**"差一个字意思相反"**（"能退吗/不能退吗"），建议只用于高重复、低风险场景，且必须能按租户关闭。

**prompt cache 是另一回事**：它是**供应商侧的自动/显式前缀缓存**，省钱省延迟，不需要你自己存答案。规则就一条：**把稳定内容放前面、变动内容放后面**。

```
❌ 差的前缀设计                       ✅ 好的前缀设计
[system] 你是助手，当前时间 03:41      [system] 你是客服助手，规则 1/2/3…（固定，长）
[system] 用户是 VIP 张先生             [system] 工具说明与输出格式（固定，很长）
[user]   ……动态……                     [user]  当前时间 03:41；用户 VIP 张先生
                                             [user]  ……动态……
      ↑ 每次请求前缀都不同 → 0 命中          ↑ 前缀高度复用 → 命中率高
```

OpenAI 那类实现是按"最长公共前缀 token"自动缓存（前缀需达到最小长度门槛，如 1024 token 量级）；Anthropic 那类需要你显式给内容块打 `cache_control` 标记。**两种都要求"稳定前缀"**——把 `time.Now()`、session id、随机串塞进 system prompt 是最常见的自毁操作。另外注意缓存通常有 TTL（分钟级），低频场景不一定划算。

### 47.3.5 成本核算与预算刹车：token 账本

成本的三层治理：**记账**（看清）→ **预算**（拦住）→ **归因**（优化）。

```go
// 1) 价目表：按模型分输入/输出（每百万 token 的价格，示例值）
var price = map[string]struct{ In, Out float64 }{
	"gpt-4o":       {In: 2.5, Out: 10},
	"gpt-4o-mini":  {In: 0.15, Out: 0.6},
	"claude-sonnet": {In: 3, Out: 15},
}

func CostUSD(model string, inTokens, outTokens int) float64 {
	p := price[model]
	return float64(inTokens)*p.In/1e6 + float64(outTokens)*p.Out/1e6
}

// 2) 预算刹车：预扣（reserve）→ 执行 → 结算（settle），差值退回
type Brake struct{ RDB *redis.Client }

const quotaKey = "budget:req:%s" // request/run 级预算

// 预扣：超了直接拒，绝不让"先花后拒"发生
var luaReserve = redis.NewScript(`
local used = tonumber(redis.call('GET', KEYS[1]) or '0')
local reserve = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
if used + reserve > limit then return -1 end
redis.call('INCRBY', KEYS[1], reserve)
redis.call('PEXPIRE', KEYS[1], ARGV[3])
return used + reserve
`)

func (b *Brake) Reserve(ctx context.Context, id string, reserve, limit int, ttl time.Duration) (int, error) {
	return luaReserve.Run(ctx, b.RDB, []string{fmt.Sprintf(quotaKey, id)},
		reserve, limit, ttl.Milliseconds()).Int()
}

// 结算：真实用量与预扣的差额（可为负，即退回），并写入账本做归因
func (b *Brake) Settle(ctx context.Context, id string, reserved, actual int) {
	if delta := actual - reserved; delta != 0 {
		b.RDB.IncrBy(ctx, fmt.Sprintf(quotaKey, id), int64(delta))
	}
	b.ledger.Write(ctx, LedgerEntry{RequestID: id, Tokens: actual}) // 异步批量写 PG/ClickHouse
}
```

| 归因维度 | 为什么需要 | 数据从哪来 |
| --- | --- | --- |
| 租户 / 用户 | 计费与配额 | 请求上下文 |
| 模型 | 单价差异 20 倍 | provider 响应 |
| 用途（对话/工具/RAG/摘要/评估） | 找出"隐形大户" | 打 `usage` 标签 |
| 会话 / 运行 | 死循环、长尾 | `session_id` / `run_id` |
| prompt 版本 | 判断"这次优化值不值" | prompt registry |

> ⚠️ **预算刹车只截"下一次调用"，不截"当前流式输出"**。流式场景要按 chunk 累计 token，超阈值时**优雅收尾**（发一个 `finish_reason=budget` 的事件），而不是直接断连——用户体验差别巨大。

### 47.3.6 灰度与 prompt 版本管理

prompt 是**资产**，不是字符串常量。最低要求：有 ID、有版本、有 hash、能灰度、能回滚、经过评估才能晋级。

```go
type Prompt struct {
	ID       string
	Version  string    // "v7"
	Hash     string    // sha256(text)[:12]，用于缓存 key 与日志关联
	Template string    // 含 {{.Tool}} 之类占位符
	Model    string
	Tags     []string  // canary / stable
}

type Registry struct {
	mu   sync.RWMutex
	live map[string][]Prompt // ID → 版本列表
	// 线上命中规则：按租户哈希做百分比灰度
}

func (r *Registry) Pick(id, tenantID string, pct int) Prompt {
	vs := r.live[id]
	stable := vs[0]
	if len(vs) < 2 || pct <= 0 {
		return stable
	}
	canary := vs[len(vs)-1]
	h := fnv.New32a()
	_, _ = h.Write([]byte(tenantID))
	if int(h.Sum32()%100) < pct { // 同一租户始终落到同一侧（一致性哈希）
		return canary
	}
	return stable
}
```

四条纪律：① **prompt 变更不随代码发布**（放配置中心/DB，带审计）；② 灰度单位是**租户**不是请求（否则同一用户答案风格跳变）；③ 晋级条件 = 离线评估不降 + 线上指标（点踩率、转人工率、成本）不降；④ 每个 span 里带上 `prompt.version`，事故时才能定位"是不是这次改动"。

### 47.3.7 评估体系：离线集 + 线上反馈

| 层 | 内容 | 频率 | 门槛 |
| --- | --- | --- | --- |
| L1 单元 | 规则断言（JSON 合法、必含引用、拒答合规） | 每次提交（CI） | 100% 通过 |
| L2 离线集 | 200~2000 条 golden set，打分（LLM-judge + 人工抽检） | 每次 prompt/模型变更 | 不下降 > 2% |
| L3 回归（RAG） | recall@k / MRR（第 44 章） | 每次索引/切分变更 | 不下降 |
| L4 线上 | 点踩率、转人工率、平均轮次、成本/请求 | 实时看板 | SLO 内 |
| L5 A/B | 灰度组 vs 对照组，同一批真实流量 | 按周期 | 统计显著才晋级 |

### 47.3.8 安全与内容过滤 + 数据合规脱敏

三层防线，各管各的：

1. **入口（防注入）**：用户内容永远不当指令。工具结果和 RAG 片段要**明确标注为"数据"**，并对"忽略以上指令"类模式做检测（第 42 章的 prompt 注入）；工具权限按租户+用户做最小授权（第 41 章）。
2. **出口（防泄漏/违规）**：分类器过一遍（涉政/涉黄/自伤等），加**正则黑名单**兜住"把别人的数据吐出来"（如手机号、身份证、内部 URL 前缀）。出口过滤失败要 fail-closed（宁可拒答）。
3. **合规（脱敏与留存）**：日志与训练数据里**绝不放原文**。PII 脱敏在"入模型前"和"落日志前"各做一次：

```go
// 最小 PII 脱敏：可逆（存映射表）或不可逆（哈希/掩码）
var piiPatterns = []struct {
	name string
	re   *regexp.Regexp
	mask string
}{
	{"phone", regexp.MustCompile(`1[3-9]\d{9}`), "<PHONE>"},
	{"idcard", regexp.MustCompile(`\d{17}[\dXx]`), "<ID>"},
	{"email", regexp.MustCompile(`[\w.+-]+@[\w-]+\.[\w.]+`), "<EMAIL>"},
	{"card", regexp.MustCompile(`\b\d{16,19}\b`), "<CARD>"},
}

func Redact(s string) string {
	for _, p := range piiPatterns {
		s = p.re.ReplaceAllString(s, p.mask)
	}
	return s
}
```

注意：**脱敏会伤害效果**（模型看不到订单号就没法查单），所以正确做法是**"双轨"**——送模型的上下文用 token 级替换（`<PHONE_1>`），同时在服务端维护映射表，工具调用时再还原。这是 Agent 场景和传统日志脱敏最大的区别。

### 47.3.9 SLO 与告警

Agent 的 SLI 和普通服务不一样，至少要四个：

| SLI | 定义 | 典型 SLO | 告警策略 |
| --- | --- | --- | --- |
| 可用性 | 非 5xx 且非"模型拒答"的请求占比 | 99.5% | 5 分钟错误预算燃烧率 > 14.4x |
| TTFT | 首 token 延迟 P95 | < 1.5s | P95 连续 10 分钟超阈值 |
| 端到端延迟 | 完整响应 P95（工具场景单独看） | < 8s | 同上，按工具分维度 |
| 单位成本 | 每成功请求的 token 成本 | < $0.02 | **日成本环比 > 30% 立即告警**（这条最容易被忽略） |

再补三个"Agent 特有"的护栏告警：**单会话步数异常**（> 20 步）、**单请求 token 异常**（> 上下文窗口 60%）、**工具错误率**（> 5%）。它们比"CPU 高"更早预示事故。

> 🩸 **错误预算的正确用法**：不是"花完了就停止发布"，而是"花得快的那个服务必须先停下来查"。成本预算同理——**给每个租户和每个功能都设一条成本斜率告警**，比月底看账单有用一万倍。

---

## 47.4 Java 对照

| 你的 Java 直觉 | Go 的现实 | 会猜错的地方 |
| --- | --- | --- |
| Sentinel / Hystrix 注解式限流熔断 | 显式 `middleware` 函数链 + `gobreaker` | 没有 AOP，熔断器要作为依赖注入进 handler（第 36 章） |
| Guava `RateLimiter`（单机） | `x/time/rate` | 同构；但 Go 的 `Allow()` 无阻塞、`Wait(ctx)` 可取消，**在 Agent 里用 `Allow` 更好**（排队会把延迟放大） |
| Redisson `RRateLimiter` | 自己写 Lua 令牌桶 | 手写可控，但必须处理时钟与集群 slot |
| Spring `@Cacheable` | 显式缓存函数 / 中间件 | Go 没有声明式缓存；好处是 key 怎么拼一目了然 |
| Micrometer + Prometheus | `client_golang` + OTel | 同构，指标名要自己设计（第 35 章） |
| `@Retryable` + `@CircuitBreaker` | `github.com/sony/gobreaker` + 自己包 retry | Go 的重试要显式分类错误（第 7 章） |

---

## 47.5 框架与工程落地

- **kratos**：`middleware/ratelimit`、`middleware/circuitbreaker`、`middleware/metadata` 直接可用；`ratelimit` 基于 BBR 自适应限流，适合"服务自身保护"而不是"租户配额"。
- **sentinel-golang**（`github.com/alibaba/sentinel-golang`）：`sentinel.InitDefault()` + `sentinel.Entry("llm-chat", sentinel.WithTrafficType(base.Outbound))`，`blockErr != nil` 即被限/被熔断。它把流控、熔断、热点参数限流做成一套资源模型，Java 团队迁移成本最低。
- **otel**：给 `llm.request`、`tool.call` 打 span（第 35 章），把 `tenant_id`、`prompt.version`、`model`、`tokens.in/out`、`cost.usd` 作为属性；指标用 counter（token/成本）+ histogram（TTFT/延迟）。
- **go-redis**：所有跨副本的计数/限流/预算都放这里；务必设 `PoolSize` 与超时，并提供"Redis 挂了怎么办"的策略（限流降级为单机、预算降级为放行并告警，二选一，**必须明确写出来**）。

---

## 47.6 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| 某租户一夜间烧掉上亿 token | 无配额/无预算刹车/无步数上限 | 配额 + 预扣预算 + `maxSteps` 三重 | 47.3.1、47.3.5 |
| 供应商 429 引发全站雪崩 | 无熔断、无降级、重试风暴 | 熔断 + 降级阶梯 + 抖动退避 | 47.3.3 |
| 限流在扩容后失效 | 只有单机令牌桶 | 单机粗筛 + Redis 分布式精确 | 47.3.2 |
| 租户级限流 map 撑爆内存 | 无限增长的 `map[string]*rate.Limiter` | LRU + 定期淘汰 | 47.3.2 |
| prompt 里塞了时间戳，缓存 0 命中 | 前缀每次都变 | 稳定内容前置，动态内容后置 | 47.3.4 |
| 语义缓存答非所问 | 0.95 相似度的"反义"问题 | 高风险场景关闭；加同义校验 | 47.3.4 |
| 成本莫名其妙上涨 | 无归因，看不到"谁在烧" | 账本打标签（租户/模型/用途/版本） | 47.3.5 |
| prompt 改动无法回滚 | 硬编码在代码 | registry + 版本 + 灰度 + 审计 | 47.3.6 |
| 流式回答被预算打断，前端报错 | 直接断连 | 发 `finish_reason=budget` 事件优雅收尾 | 47.3.5 |
| 日志里出现完整手机号 | 只在入模型前脱敏 | 落日志前再脱一次；双轨映射表 | 47.3.8 |
| 指标很好看但用户在骂 | 只监控 QPS/延迟 | 加"步数异常/工具错误率/单请求 token"告警 | 47.3.9 |

---

## 47.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 闸门顺序 | 便宜的在前：配额 → 限流 → 熔断 → 缓存 → 预算 |
| 配额单位 | 用 token 不用请求数；按租户隔离并发槽 |
| 限流三件套 | 单机令牌桶粗筛 → Redis Lua 精确 → 并发信号量护上游 |
| 熔断 | 状态机 + 样本阈值；降级四层：备用供应商/小模型/缓存/话术 |
| 缓存分层 | L1 LRU（确定性输出）、L2 精确、L3 语义（谨慎） |
| prompt cache | 稳定前缀放最前；别在 system 里塞时间与 id |
| 预算 | 预扣 → 执行 → 结算；只截下一次调用，流式要优雅收尾 |
| 成本归因 | 租户/模型/用途/会话/版本，五维打标签 |
| prompt 版本 | ID + version + hash + 灰度(按租户) + 评估门禁 |
| 评估 | L1 规则 → L2 golden → L3 召回 → L4 线上 → L5 A/B |
| 安全 | 入口防注入、出口 fail-closed、脱敏双轨（占位符 + 映射表） |
| SLO | 可用性 / TTFT / 端到端 / 单位成本，四件套 |

---

## 47.8 自检题

1. **为什么限流要分"单机令牌桶 + Redis 分布式"两层，而不是只用 Redis？**
   > 要点：Redis 每请求一次网络往返（0.2~2ms）且是共享瓶颈，浪涌时会成为新故障点；单机桶零成本、能挡掉绝大部分突发，再由分布式桶做"全局口径"的精确控制。两层阈值应满足「单机桶 ≥ 分布式桶 / 副本数 × 1.5」这类关系，避免互相打脸。

2. **预算刹车为什么必须"预扣"而不是"事后检查"？**
   > 要点：事后检查时，钱已经花了——流式请求可能在结算前烧掉整个上下文；并发请求会同时通过"还有余额"的判断。预扣在调用前原子占额（Redis INCRBY + 上限判断在一个 Lua 里），结束时按实际用量结算，才能把超支控制在一个常数级误差内。

3. **为什么把 `time.Now()` 写进 system prompt 会让成本大幅上升？**
   > 要点：供应商的 prompt cache 按**最长公共前缀**命中，system prompt 是前缀的一部分，一旦每次都不同，后续所有内容都不再共享缓存，输入 token 全额计费（且 TTFT 变高）。正确做法是把动态信息放到消息尾部或 user 消息里。

---

## 47.9 延伸阅读

- `golang.org/x/time/rate`：`Limiter.Allow/Reserve/Wait`、`SetLimit`、`SetBurst` 的语义差异
- `golang.org/x/sync/semaphore`：`NewWeighted`、`Acquire(ctx, n)`、`TryAcquire`
- `github.com/sony/gobreaker`（v1 `Execute` / v2 泛型 `NewCircuitBreaker[T]`）
- `github.com/alibaba/sentinel-golang`：`sentinel.Entry`、`flow` 与 `circuitbreaker` 规则
- `github.com/redis/go-redis/v9` 的 `NewScript`（分布式限流/预算的基础设施）
- OpenAI / Anthropic 官方文档中关于 prompt caching 的前缀命中条件与 TTL 说明
- 上一章：[46. 多 Agent 协作与持久化执行](46-multi-agent-durable.md) ｜ 下一章：[48. langchaingo 精读](48-langchaingo-source.md)

---

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[46. 多 Agent 协作与持久化执行](46-multi-agent-durable.md)；第 32 章（限流原语与容量规划）、第 35 章（可观测）、第 52 章（SLO 与部署）
- 下一章：[48. langchaingo 精读](48-langchaingo-source.md)
