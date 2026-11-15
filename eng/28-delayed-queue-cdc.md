# 28. 延迟队列、定时任务与 CDC

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 异步与消息 ｜ **编号**：28 / 52
> **本章定位**：延迟任务、定时任务、CDC 看起来是三件事，其实是同一个问题的三种解法——**「某个状态变化，需要在另一个时刻、由另一个组件感知并处理」**。选错的代价不是性能，而是「单子 4 小时没关」和「对账跑了两遍」。
> **读法**：§28.4 第一步的五种实现对比表决定选型；第二步的 Redis ZSet Lua 与时间轮是本章的技术核心（**原子取出**与**重启不丢**是两个必须过的坎）；第三步讲多实例抢占；第四步讲 CDC 的位置与 Debezium 的位点；§28.5 只记一句：Agent 的「定时评估」和「延迟重试」用的是同一套基建。

---

## 28.1 引子：两个「跑了但没跑对」的任务

**事故一（2025-03-14，某电商的「30 分钟未支付自动关单」）**。需求很朴素：下单后 30 分钟未支付，关单并释放库存。

实现也很朴素：`order_info` 加一列 `expire_at`，一个 `@Scheduled(cron = "0 */1 * * * ?")` 每分钟扫一次 `WHERE status='PENDING_PAY' AND expire_at < NOW()`。

- **2025-02** 订单表 1,800 万行，`expire_at` 上**没有索引**（建表时忘了）。每分钟一次全表扫，单次耗时从 0.8s 涨到 **8 分 12 秒**。
- **2025-03-14 10:00** 大促开始，写入量翻 4 倍，扫描期间的行锁与 MVCC 版本链让扫表变成 **14 分钟一轮**。
- **10:31** 该关的单没关。用户 10:02 下单、10:32 才付款成功——但库存早在别的渠道卖掉了。
- **11:20** 三个实例同时触发（`@Scheduled` 在多副本下天然重复执行），同一批订单被关单两次：第一次释放库存，第二次把已支付的订单也关掉了。
- **11:20—13:05** 超卖 **1,240 件**，其中 **380 单**是「付了钱但订单被关闭」，退款 + 赔付 **38 万元**，客诉 **610 单**。
- 到 13:05 恢复时，积压的待关订单还有 **11.4 万**笔，最老的一笔**已下单 4 小时 12 分**。

**事故二（2025-06-01，某 SaaS 的日终对账）**。对账任务 `reconcileDaily` 用 `@Scheduled(cron="0 30 2 * * ?")`，部署 3 副本。

- **02:30:00** 三个实例**同时**开始跑（Quartz 默认集群模式需要数据库锁表；他们是自己写的 `@Scheduled`，没有集群协调）。
- **02:30—02:41** 三份对账同时读流水、同时写差异表、同时触发「差异自动冲正」。
- **02:41** 冲正逻辑没有幂等键，同一笔差异被冲正 **3 次**：2,800 笔重复入账，金额 **42 万元**。
- **09:10** 财务发现账不平（借方多出 42 万），追查到 11:40 才定位到「任务跑了三遍」。
- **修复成本**：手工冲正 2,800 笔 + 对账差异表重算 + 客户解释，合计 **3 人日**。

> 🩸 两个事故的共同点：**任务确实「跑了」，但没有「跑对」。** 事故一是**延迟精度**问题（扫表变慢 → 关单晚了 → 超卖），事故二是**多实例幂等**问题（三份一起跑 → 重复副作用）。这两个问题会在本章被拆成两个独立的工程能力：**「什么时候触发」与「只让一个触发」**。

---

## 28.2 到底是什么：三件事，一个本质

**定义**：

- **延迟任务**：在「未来某个时刻」执行一次动作，且**每个任务都是独立的一条**（订单 A 在 10:30 关单、订单 B 在 10:31 关单）。关键词是「海量、单条、定时」。
- **定时任务**：按固定节奏执行**同一段逻辑**（每天 02:30 对账、每 5 分钟同步一次汇率）。关键词是「周期性、批量、少实例」。
- **CDC**（Change Data Capture）：**订阅数据库的变更日志**（MySQL binlog / PG WAL），把「行级变更」变成事件流。关键词是「不侵入业务代码、按提交顺序、可回溯」。

三者本质相同：**「状态变了 → 通知另一个组件」**。区别只在「谁来决定时刻」：

```
延迟任务：业务决定时刻（订单的 expire_at）→ 需要「海量定时器」
定时任务：日历决定时刻（cron）           → 需要「单实例/分片 + 幂等」
CDC     ：数据库决定时刻（commit）        → 需要「日志位点 + 幂等消费」
```

**六个反例**（每个都配「不做会怎样」）：

| 你以为的做法 | 为什么错 | 不做会怎样 |
| --- | --- | --- |
| 「延迟任务就是 `Thread.sleep` / `ScheduledExecutorService`」 | 进程重启就丢，10 万任务 = 10 万个线程 | 重启丢任务，且内存先炸 |
| 「一张表每分钟扫一次就行」 | 无索引 = 全表扫；量一大就「每分钟扫 14 分钟」（事故一） | 延迟精度从 1 分钟劣化到小时级 |
| 「多副本部署，`@Scheduled` 会自动只有一个执行」 | Spring 的 `@Scheduled` **没有任何集群协调** | 事故二：三份一起跑，重复冲正 |
| 「加个 Redis 分布式锁就解决了」 | 锁过期/续期失败/主从切换都会导致「两个同时持有」 | 你只是把「必然重复」换成「偶发重复」，更难查 |
| 「CDC 就是数据同步，跟业务无关」 | CDC 可用于缓存失效、Outbox 投递、搜索索引，直接决定读一致性 | 缓存脏 6 小时，用户看到旧价格下单 |
| 「Debezium 配好就一直跑」 | binlog 有保留期，connector 停久了位点就失效，只能重新全量快照 | 一次重启触发 2 亿行全量同步，DB IO 打满 |

> 💡 选型的第一问不是「用哪个产品」，而是：**「这个任务有多少条、精度要求多少、允不允许重复、允不允许丢」**。四个答案一出来，产品基本就定了（见 §28.4 第一张表）。

---

## 28.3 结构图：五种实现的定位，与时间轮长什么样

先把五种实现放到「精度 × 量级」这张图上——**选型就是找自己的位置**：

```
精度
 1s ┤  时间轮(内存)         RocketMQ 5.x 定时消息
    │  Redis ZSet           
10s ┤                       RocketMQ 4.x 延迟级别(18 档)
    │
 1m ┤  DB 轮询(有索引)       调度平台(cron 最小粒度)
    │
10m ┤
    └──────┬──────────┬──────────┬──────────┬──────────► 任务量级
         1k/天      10万/天     100万/天    1000万/天

读法：
  · 量小 + 精度要求低（分钟级）→ DB 轮询最省事，还能直接用 SQL 查状态
  · 量大 + 精度秒级           → Redis ZSet（多实例安全）或 MQ 定时消息（免运维）
  · 单机、超高频、可丢一部分   → 时间轮（Netty/Kafka 内部就是这么做的）
  · 周期性批处理              → 调度平台（cron + 分片 + 失败重试 + 可视化）
  · 不要用「调度平台」做「每单 30 分钟」——那是 100 万个 job，调度中心会先死
```

**时间轮（Timing Wheel）的结构**——为什么它能用 O(1) 管理 10 万个定时器：

```
                  tick = 100ms，slots = 512（覆盖 51.2s）
                  ┌─────┬─────┬─────┬─────┬─────┬─────┐
   指针 idx ────► │  0  │  1  │  2  │ ... │ 510 │ 511 │
                  └──┬──┴─────┴──┬──┴─────┴─────┴─────┘
                     │           │
                     ▼           ▼
               ┌──────────┐  ┌──────────┐
               │ task A   │  │ task C   │   每个槽是一个链表/小 map
               │ rounds=0 │  │ rounds=2 │   rounds>0 表示「还要转 2 圈」
               │ task B   │  │          │
               │ rounds=0 │  └──────────┘
               └──────────┘
   每 tick：idx = (idx+1) % 512，扫描该槽：
     rounds > 0 → rounds--（这圈不执行）
     rounds = 0 → 取出执行（回调在锁外跑）

  三个必然的工程问题（代码里都要处理）：
   ① 时间跨度 > tick×slots → 用 rounds 计数；rounds 算错会「早触发」或「晚一圈」
   ② 重启 → 内存里的任务全丢 → 必须落库/落 Redis，启动时重建（或只用它做「一级缓存」）
   ③ 回调阻塞 → 一个慢任务拖住整个 tick → 回调必须丢进独立 goroutine/线程池
```

**CDC 在链路里的三个位置**（注意：它不是只有「数据同步」一个用途）：

```
                    ┌──────────────────────────────────────────────┐
   MySQL            │  binlog（ROW 格式，按事务提交顺序）           │
  ┌────────┐        └───────────────┬──────────────────────────────┘
  │ 业务表  │──写──►                  │ ① 解析（Debezium/binlog client）
  │ outbox │                        ▼
  └────────┘              ┌───────────────────┐
                          │ Debezium Connector │  位点 = (binlog file, pos) / GTID
                          │  + Schema History  │  存 Kafka topic（compact）
                          └─────────┬─────────┘
                                    │ 事件流（before/after/op/source）
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
 ② 缓存失效                    ③ 搜索/数仓同步              ④ Outbox 投递
 DELETE cache key             UPDATE ES / 入湖            topic = outbox.<aggregate>
 （按主键删，不写值）          （按主键 upsert）            （替代轮询投递器）
```

---

## 28.4 怎么实现：五种延迟实现 → 抢占 → CDC

### 第一步：五种实现的精度与成本对比

| 实现 | 精度 | 最大延迟 | 单机/集群吞吐 | 重启是否丢 | 运维成本 | 适用 |
| --- | --- | --- | --- | --- | --- | --- |
| **MQ 延迟消息**（RocketMQ） | 4.x 固定 18 档（1s/5s/10s/30s/1m…2h）；5.x 任意定时 | 4.x 最长 2h；5.x 以 `timerMaxDelaySec` 为准 | 十万级/s | 不丢（持久化） | 中（多一个 MQ） | 已用 RocketMQ、量大、要求不丢 |
| **时间轮（应用内）** | = tick（100ms~1s） | = tick × slots × rounds（可到天级） | 单机百万级 | **丢**（除非落库重建） | 低（纯内存） | 单机高频、可容忍重建、连接超时/重试退避 |
| **Redis ZSet** | = 轮询间隔（100ms~1s） | 无上限 | 万级/s（受 Redis QPS 限制） | 不丢（AOF/RDB，需 lease 兜底） | 低中（多一个 Redis 依赖） | 通用首选：量大、秒级、多实例 |
| **DB 轮询** | = 扫描间隔（1s~1min） | 无上限 | 千级/s（受索引扫描限制） | 不丢 | 最低（复用现有库） | 量小、要求强一致、可接受分钟级 |
| **调度平台**（xxl-job/ElasticJob/Quartz） | = cron 粒度（秒/分钟） | 按 cron | 按分片扩展 | 不丢（有调度记录） | 中（一套调度中心） | 周期性批处理、失败重试、可视化 |

**四条选型判据**（按优先级）：

1. **量级 > 100 万/天**：别用 DB 轮询，也别用「每单一个 cron」；用 Redis ZSet 或 MQ。
2. **精度要求 < 1s**：只能时间轮或 MQ 5.x 定时消息；Redis 轮询 100ms 也能做到，但 Redis QPS 会被放大（100 万任务 / 100ms = 1000 万次 ZRANGEBYSCORE/天）。
3. **不能丢**：时间轮必须配「落库 + 启动重建」，否则重启丢任务（订单永不关单）。
4. **允不允许重复**：五种实现**都可能重复**（Redis 取出后崩溃、MQ 重投、DB 轮询多实例），所以**幂等不是可选项**。

> ⚠️ RocketMQ 4.x 的延迟级别是**固定 18 档**（`messageDelayLevel=1s 5s 10s 30s 1m 2m 3m 4m 5m 6m 7m 8m 9m 10m 20m 30m 1h 2h`）。你要「30 分钟」就只能落到 `30m` 这一档；要「37 分钟」只能自己写「30m + 7m」的两跳，或者升级 5.x 的定时消息。**这一条不搞清，做出来的关单时间会莫名差 7 分钟。**

### 第二步：Redis ZSet 的原子取出（Lua + 租约）

朴素做法是「`ZRANGEBYSCORE` 查出来，再 `ZREM`」——两步之间崩溃/并发，就会**两个实例同时拿到同一个任务**，或者**取出后崩溃导致任务永久丢失**。正确做法是**一个 Lua 脚本完成「取出 + 转移到 processing 集合 + 打租约」**：

```lua
-- delay_pop.lua
-- KEYS[1] = delay:{queue}        待触发 ZSet，score = 到期毫秒时间戳
-- KEYS[2] = processing:{queue}   处理中 ZSet，score = 租约到期时间
-- ARGV[1] = now_ms   ARGV[2] = batch   ARGV[3] = lease_ms
local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, tonumber(ARGV[2]))
if #due == 0 then return {} end
local leaseUntil = tonumber(ARGV[1]) + tonumber(ARGV[3])
for i = 1, #due do
  redis.call('ZREM', KEYS[1], due[i])
  redis.call('ZADD', KEYS[2], leaseUntil, due[i])
end
return due
```

```lua
-- delay_ack.lua：业务成功后删除 processing 记录
-- KEYS[1] = processing:{queue}   ARGV[1] = member
return redis.call('ZREM', KEYS[1], ARGV[1])

-- delay_recover.lua：扫租约过期的（处理者崩溃），搬回 delay 集合
-- KEYS[1] = processing:{queue}  KEYS[2] = delay:{queue}
-- ARGV[1] = now_ms  ARGV[2] = batch
local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, tonumber(ARGV[2]))
for i = 1, #expired do
  redis.call('ZREM', KEYS[1], expired[i])
  redis.call('ZADD', KEYS[2], ARGV[1], expired[i])   -- 立刻可再次触发
end
return expired
```

**四个必须注意的细节**：

1. **所有 key 用同一个 hash tag**（`delay:{order}`、`processing:{order}`），否则 Redis Cluster 下两个 key 可能在不同 slot，Lua 直接报 `CROSSSLOT`。
2. **用 `EVALSHA` 而不是每次 `EVAL`**：脚本先 `SCRIPT LOAD` 拿 sha1，之后 `EVALSHA` 省掉脚本传输（10 万 QPS 时这很贵）。遇到 `NOSCRIPT` 再回退 `EVAL`。
3. **`batch` 别超过 200**：一次搬太多会让单个实例长时间占用，且失败时重放量太大。
4. **租约必须 > 单条任务的最坏处理时间**（比如 60s），否则任务还在跑就被别的实例重新取出——**这就是「偶发重复」的来源**。

```go
// Go：轮询 + Lua + 租约续期，一屏能看完的完整实现
package delay

import (
	"context"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
)

const popScript = `local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, tonumber(ARGV[2]))
if #due == 0 then return {} end
local leaseUntil = tonumber(ARGV[1]) + tonumber(ARGV[3])
for i = 1, #due do
  redis.call('ZREM', KEYS[1], due[i])
  redis.call('ZADD', KEYS[2], leaseUntil, due[i])
end
return due`

type Worker struct {
	rdb     *redis.Client
	queue   string
	batch   int64
	lease   time.Duration
	handle  func(ctx context.Context, member string) error
}

func (w *Worker) Run(ctx context.Context) error {
	pop := redis.NewScript(popScript)
	tk := time.NewTicker(200 * time.Millisecond) // 精度 = 轮询间隔
	defer tk.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-tk.C:
			now := time.Now().UnixMilli()
			keys := []string{"delay:{" + w.queue + "}", "processing:{" + w.queue + "}"}
			res, err := pop.Run(ctx, w.rdb, keys, now, w.batch, w.lease.Milliseconds()).StringSlice()
			if err != nil && err != redis.Nil {
				return fmt.Errorf("pop: %w", err)
			}
			for _, m := range res {
				// 并发处理，但每个任务独立 ctx（一条慢不拖累整批）
				go func(member string) {
					tctx, cancel := context.WithTimeout(ctx, w.lease)
					defer cancel()
					if err := w.handle(tctx, member); err == nil {
						_ = redis.NewScript(`return redis.call('ZREM', KEYS[1], ARGV[1])`).
							Run(ctx, w.rdb, []string{"processing:{" + w.queue + "}"}, member).Err()
					}
					// 失败不 ack：租约到期后被 delay_recover 搬回，自然重试
				}(m)
			}
		}
	}
}
```

**为什么不直接「取出就删」**：取出即删 = 至多一次，处理者崩溃任务就永久消失（订单永不关单）。**「取出 → processing 集合 → ack 删除」是至少一次**，代价是可能重复，所以业务侧必须幂等。这就是第 24 章那条铁律在延迟队列里的复现。

### 第三步：时间轮的 Go 实现（注意 rounds 与回调隔离）

```go
package wheel

import (
	"sync"
	"time"
)

type entry struct {
	id       string
	deadline time.Time
	rounds   int
}

type Wheel struct {
	mu    sync.Mutex
	slots []map[string]*entry
	tick  time.Duration
	idx   int
	fire  func(id string) // 必须非阻塞：内部丢进工作池
}

func New(tick time.Duration, slots int, fire func(string)) *Wheel {
	w := &Wheel{slots: make([]map[string]*entry, slots), tick: tick, fire: fire}
	for i := range w.slots {
		w.slots[i] = make(map[string]*entry)
	}
	go w.loop()
	return w
}

func (w *Wheel) loop() {
	tk := time.NewTicker(w.tick)
	defer tk.Stop()
	for range tk.C {
		w.advance()
	}
}

// Add: d 是相对延迟。ticks 向上取整，保证「不早于 deadline」触发（精度 = tick）
func (w *Wheel) Add(d time.Duration, id string) {
	if d < 0 {
		d = 0
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	w.addLocked(d, id)
}

func (w *Wheel) addLocked(d time.Duration, id string) {
	tick := int64(w.tick)
	ticks := int64(d) / tick
	if int64(d)%tick != 0 {
		ticks++ // ceil：宁可晚一个 tick，不要早触发
	}
	if ticks == 0 {
		ticks = 1
	}
	n := int64(len(w.slots))
	slot := int((int64(w.idx) + ticks) % n)
	rounds := int(ticks / n)
	// ticks 正好是整圈时，第一次回到本槽不应扣减 rounds（否则会晚一整圈）
	if ticks%n == 0 && rounds > 0 {
		rounds--
	}
	w.slots[slot][id] = &entry{id: id, deadline: time.Now().Add(d), rounds: rounds}
}

func (w *Wheel) advance() {
	w.mu.Lock()
	w.idx = (w.idx + 1) % len(w.slots)
	bucket := w.slots[w.idx]
	var due []string
	var reAdd [][2]any // 兜底：rounds 算错时按剩余时间重新入槽，绝不早触发
	for id, e := range bucket {
		if e.rounds > 0 {
			e.rounds--
			continue
		}
		delete(bucket, id)
		if remain := time.Until(e.deadline); remain > 0 {
			reAdd = append(reAdd, [2]any{remain, id})
			continue
		}
		due = append(due, id)
	}
	for _, r := range reAdd {
		w.addLocked(r[0].(time.Duration), r[1].(string))
	}
	w.mu.Unlock()

	// 关键：回调在锁外执行，且由调用方保证非阻塞（否则一个慢任务拖住整个 tick）
	for _, id := range due {
		w.fire(id)
	}
}
```

**三个工程约束**（不满足就别用时间轮）：

1. **重启会丢**：时间轮是内存结构。生产用法是「Redis/DB 里存任务真身，时间轮只做最近 N 秒的『一级缓存』」；启动时把 `next_trigger_at <= now+51s` 的任务灌回轮子。
2. **`fire` 必须非阻塞**：如果 `fire` 里直接跑业务（比如发 HTTP），一个 10 秒的调用会让后面所有任务晚 10 秒。正确做法是 `fire` 里 `select { case pool <- id: default: /* 队列满，落回 DB，下一轮再取 */ }`。
3. **`rounds` 的整圈边界**：`ticks % n == 0` 时如果照常 `rounds = ticks/n`，任务会晚一整圈触发。代码里那行 `rounds--` 就是为它准备的——**这种 off-by-one 在测试里很难发现，只会在「30 分钟任务 60 分钟才触发」时暴露**。

### 第四步：定时任务的多实例幂等与抢占

三种模式，选错就是事故二：

| 模式 | 做法 | 会不会重复 | 适用 | 坑 |
| --- | --- | --- | --- | --- |
| **广播** | 每个实例都跑 | **一定重复** | 本地缓存刷新、实例级探活 | 必须每条数据幂等，否则重复副作用 |
| **抢占（推荐）** | 谁先抢到锁谁跑 | 偶发重复（锁失效/超时） | 对账、结算、批量同步 | 锁的租约与续期；业务仍要幂等 |
| **调度中心分片** | 调度中心派发，按分片号分数据 | 不重复（有调度记录） | 大数据量批处理 | 分片键要均匀；重试要幂等 |

**用数据库做抢占，比 Redis 锁可靠**（因为「抢锁」与「写任务记录」可以在同一个事务里）：

```sql
-- 任务执行记录表：唯一键就是幂等键，抢锁 = 插入成功
CREATE TABLE job_execution (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  job_name     VARCHAR(64)  NOT NULL COMMENT '如 reconcileDaily',
  biz_date     DATE         NOT NULL COMMENT '业务日期：同一天只允许成功一次',
  shard_no     INT          NOT NULL DEFAULT 0 COMMENT '分片号，0 表示不分片',
  status       TINYINT      NOT NULL DEFAULT 0 COMMENT '0执行中 1成功 2失败 3超时',
  instance_id  VARCHAR(64)  NOT NULL,
  started_at   DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  heartbeat_at DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  finished_at  DATETIME(3)  NULL,
  error_msg    VARCHAR(1000) NOT NULL DEFAULT '',
  PRIMARY KEY (id),
  UNIQUE KEY uk_job_date_shard (job_name, biz_date, shard_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='定时任务执行账本：唯一键=幂等键';

-- 抢锁：插入成功 = 我拿到；插入失败 = 别人在跑（或被跑过）
INSERT INTO job_execution (job_name, biz_date, shard_no, status, instance_id)
VALUES ('reconcileDaily', CURDATE(), 0, 0, 'pod-7f9c')
ON DUPLICATE KEY UPDATE
  -- 只允许「上次失败/超时」或被接管的任务被重新抢占
  status = IF(status IN (2,3), 0, status),
  instance_id = IF(status IN (2,3), VALUES(instance_id), instance_id),
  started_at = IF(status IN (2,3), NOW(3), started_at);
-- 判断依据：affected rows = 1 且 status=0 → 我抢到了；否则跳过

-- 心跳：长任务必须续心跳，否则会被别人当「超时」接管
UPDATE job_execution
   SET heartbeat_at = NOW(3)
 WHERE job_name='reconcileDaily' AND biz_date=CURDATE() AND shard_no=0
   AND instance_id='pod-7f9c' AND status=0;

-- 接管：心跳超过 10 分钟没更新的，允许别人抢
UPDATE job_execution
   SET instance_id='pod-2a11', started_at=NOW(3), heartbeat_at=NOW(3)
 WHERE job_name='reconcileDaily' AND biz_date=CURDATE() AND shard_no=0
   AND status=0 AND heartbeat_at < NOW(3) - INTERVAL 10 MINUTE;
```

```java
// Java：抢锁 + 心跳 + 业务幂等，三段都不能少
@Component
public class ReconcileJob {

    @Scheduled(cron = "0 30 2 * * ?")
    public void run() {
        String inst = System.getenv("HOSTNAME");
        int claimed = jdbc.update("""
            INSERT INTO job_execution(job_name, biz_date, shard_no, status, instance_id)
            VALUES ('reconcileDaily', CURDATE(), 0, 0, ?)
            ON DUPLICATE KEY UPDATE
              status = IF(status IN (2,3), 0, status),
              instance_id = IF(status IN (2,3), VALUES(instance_id), instance_id)
            """, inst);
        if (claimed == 0) return;                       // 没抢到：直接退出，不报错

        ScheduledFuture<?> hb = scheduler.scheduleAtFixedRate(
            () -> jdbc.update("UPDATE job_execution SET heartbeat_at=NOW(3) " +
                              "WHERE job_name='reconcileDaily' AND biz_date=CURDATE() " +
                              "AND shard_no=0 AND instance_id=? AND status=0", inst),
            30, 30, TimeUnit.SECONDS);                  // 每 30s 续一次，租约 10min

        try {
            int n = doReconcile();                      // 内部逐笔用 biz_key 幂等（第 14 章）
            jdbc.update("UPDATE job_execution SET status=1, finished_at=NOW(3) " +
                        "WHERE job_name='reconcileDaily' AND biz_date=CURDATE() AND shard_no=0");
            log.info("reconcile done rows={}", n);
        } catch (Exception e) {
            jdbc.update("UPDATE job_execution SET status=2, error_msg=?, finished_at=NOW(3) " +
                        "WHERE job_name='reconcileDaily' AND biz_date=CURDATE() AND shard_no=0",
                        truncate(e.toString(), 900));
            throw e;                                    // 让调度平台记录失败，人工可见
        } finally {
            hb.cancel(false);
        }
    }
}
```

> ⚠️ **`@Scheduled` 在多副本下一定会跑 N 遍**。这不是 Spring 的 bug，是它的设计（它只负责「进程内定时」）。要么上调度中心（xxl-job 的「单机路由」、ElasticJob 的分片），要么就像上面这样自己用唯一键抢。

**调度中心的分片怎么算**（以 ElasticJob 的思路为例，其他产品同理）：

```
总分片数 = 3（配置），实例数 = 3
实例 0 处理 shard 0（user_id % 3 == 0），实例 1 处理 shard 1，实例 2 处理 shard 2
实例挂掉 → 分片重新分配（shard 1 交给实例 0），期间该分片的任务会「重跑」
  → 所以每个分片的处理逻辑必须幂等（按主键 upsert / 按唯一键去重）
分片数 ≠ 实例数时：实例数 > 分片数 → 有实例空转；实例数 < 分片数 → 一个实例跑多个分片
```

### 第五步：CDC 的位置、Debezium 配置与位点

**CDC 的三个位置**（每个都对「一致性」有不同要求）：

| 用途 | 做什么 | 一致性要求 | 典型实现 | 不做会怎样 |
| --- | --- | --- | --- | --- |
| **数据同步** | MySQL → 数仓/ES/异构库 | 最终一致（秒级） | Debezium + Kafka Connect | 每分钟全量拉取，DB 被压垮 |
| **缓存失效** | 行变更 → 删缓存 key | 最终一致（毫秒~秒） | Debezium + 消费者删 key | 缓存脏到 TTL 到期（可能是几小时） |
| **Outbox 投递** | `outbox` 表 → 业务 Topic | 至少一次 + 消费幂等 | Debezium Outbox Event Router | 轮询投递器空转，延迟 1~5s |
| **审计/血缘** | 记录每次行变更 | 完整、不可丢 | Debezium + 归档存储 | 出事查不到「谁改的」 |
| **搜索索引** | 主库 → ES | 最终一致 | Debezium + 按主键 upsert | 搜索到的价格与详情页不一致 |

**Debezium MySQL Connector 的完整配置**（JSON，可直接 POST 到 Kafka Connect 的 `/connectors`）：

```json
{
  "name": "trade-outbox-connector",
  "config": {
    "connector.class": "io.debezium.connector.mysql.MySqlConnector",
    "tasks.max": "1",
    "database.hostname": "mysql-primary.trade.svc",
    "database.port": "3306",
    "database.user": "debezium",
    "database.password": "${file:/opt/secret/mysql.properties:debezium.password}",
    "database.server.id": "184054",
    "topic.prefix": "cdc",
    "database.include.list": "trade",
    "table.include.list": "trade.outbox_message,trade.order_info",
    "schema.history.internal.kafka.bootstrap.servers": "broker1:9092,broker2:9092",
    "schema.history.internal.kafka.topic": "cdc.trade.schema-history",
    "offset.storage.topic": "cdc.connect-offsets",
    "offset.flush.interval.ms": "5000",
    "snapshot.mode": "schema_only",
    "snapshot.lock.timeout.ms": "10000",
    "heartbeat.interval.ms": "10000",
    "event.processing.failure.handling.mode": "warn",
    "tombstones.on.delete": "false",
    "transforms": "outbox",
    "transforms.outbox.type": "io.debezium.transforms.outbox.EventRouter",
    "transforms.outbox.table.field.event.id": "msg_id",
    "transforms.outbox.table.field.event.key": "aggregate_id",
    "transforms.outbox.table.field.event.type": "event_type",
    "transforms.outbox.table.field.event.payload": "payload",
    "transforms.outbox.route.by.field": "aggregate_type",
    "transforms.outbox.route.topic.replacement": "outbox.${routedByValue}",
    "producer.override.acks": "all",
    "producer.override.enable.idempotence": "true",
    "producer.override.compression.type": "zstd"
  }
}
```

**逐项解释（配置错一个就会在生产上出事）**：

| 配置 | 为什么这么配 | 配错的后果 |
| --- | --- | --- |
| `snapshot.mode=schema_only` | 只同步表结构，从**当前位点**开始流式读；Outbox 不需要历史数据 | `initial`：把 outbox 表历史全量重放 → 重复发几百万条消息 |
| `schema.history.internal.kafka.topic` | DDL 变更历史存 Kafka（compact），connector 重启要读它 | 用文件存储 + 容器重启 → schema 历史丢失，任务起不来 |
| `offset.storage.topic` | 位点（binlog file + pos / GTID）存 Kafka | 存本地文件 → Pod 重建后位点丢失 → 重新全量 |
| `offset.flush.interval.ms=5000` | 位点刷盘间隔；越小重复越少、开销越大 | 默认 60s → 崩溃后最多重复 60 秒的数据 |
| `heartbeat.interval.ms=10000` | 无变更时也推进位点，防止「位点长期不动 → binlog 过期」 | 低峰期几小时没写入 → 位点停在旧 binlog，过期后只能重做 |
| `database.server.id` | 伪装成一个 MySQL replica，必须唯一 | 与真实 replica 冲突 → 主库踢掉从库 |
| `event.processing.failure.handling.mode=warn` | 单条坏事件跳过并告警，不阻塞整个流 | `fail`：一条解析失败 → 整条链路停摆 |
| `tombstones.on.delete=false` | Kafka 的 tombstone（value=null）会让下游 compact 掉记录 | 下游误以为「消息丢了」 |

**位点到底是什么**：Debezium 的 offset 不是「offset 数字」，而是**binlog 坐标**：

```json
{
  "ts_ms": 1741597961208,
  "binlog": { "file": "mysql-bin.000847", "pos": 128443201 },
  "gtids": "3e11fa47-71ca-11e1-9e33-c80aa9429562:1-98213",
  "transaction_id": null
}
```

- **file + pos**：传统坐标，最精确；
- **GTID**：MySQL 5.6+ 主从切换后仍有效（`pos` 会变，GTID 不变）；
- connector 每 `offset.flush.interval.ms` 把「最后一条成功发送的事件」的坐标写进 `offset.storage.topic`；
- 重启时从该坐标继续 → **可能重复**（最后一次 flush 之后的事件会重放），所以**下游必须幂等**。

**五个必须知道的坑**（每个都给检测方法）：

| # | 坑 | 现象 | 怎么查 | 怎么修 | 怎么防 |
| --- | --- | --- | --- | --- | --- |
| 1 | **binlog 过期** | connector 重启后报 `binlog position no longer available`，然后开始全量快照 | `SHOW BINARY LOGS;` 对比 offset 里的 file | 重新快照（代价巨大）或从备份恢复 | `binlog_expire_logs_seconds` ≥ 72h；connector 停机告警；`heartbeat` 必开 |
| 2 | **重复投递** | 下游出现重复行/重复消息 | 按 `msg_id` / 主键统计重复 | 补幂等（唯一索引/去重表） | 消费端幂等是**前提**，不是优化 |
| 3 | **大事务/长事务** | 快照阶段锁表、流式阶段延迟飙到分钟级 | `SHOW PROCESSLIST` 看 `Waiting for table metadata lock`；对比 `source.ts_ms` 与当前时间 | 拆大事务（每批 < 1 万行）；快照放到低峰 | `snapshot.lock.timeout.ms` 设小一点，失败重试而不是锁死 |
| 4 | **DDL 变更** | connector 报 schema 不匹配后停止，或字段全为 null | 看 Connect 日志 `schema history` 相关错误 | 用「兼容变更」（加列/加索引），重启 connector 读新 schema | DDL 走评审；变更窗口内盯 connector 状态 |
| 5 | **DELETE 的 before 为空** | 下游拿到 `op=d` 但 `before` 只有主键（甚至为空），无法做缓存失效/审计 | 看事件 JSON 的 `before` 字段 | `binlog_row_image=FULL` + `REPLICA IDENTITY FULL`（或至少主键） | 建表规范里写死 `binlog_row_image=FULL` |

**还有一个最阴的（第 6 个，值得单独记住）**：**Outbox 清理与 CDC 位点的竞态**。如果清理任务「删掉已发送的 outbox 行」比 CDC 读取更快，事件就永远不会被投递。正确顺序是「CDC 已投递（有下游确认）→ 才允许删」，或者用一个保守的时间窗（`created_at < now - 1h`）来删。

```sql
-- Outbox 清理：必须有「CDC 已消费」的证据，不能只看 status
DELETE FROM outbox_message
 WHERE status = 2
   AND created_at < NOW() - INTERVAL 1 HOUR     -- 保守窗口：覆盖 CDC 延迟 + 重放
   AND msg_id IN (
     SELECT msg_id FROM outbox_delivered         -- 下游确认表（或位点表）
      WHERE delivered_at < NOW() - INTERVAL 10 MINUTE
   )
 LIMIT 5000;
```

---

## 28.5 双修对照：同一套定时器，两种「待办」

| 维度 | 传统系统 | Agent 系统 | 同 or 异 |
| --- | --- | --- | --- |
| **机制本体** | 延迟关单、定时对账、CDC 缓存失效 | **延迟重试**（Step 失败后 30s 重试）、**定时评估**（每小时跑评测集）、**CDC 喂记忆**（变更同步进向量库） | **完全相同**：五种实现与取舍一字不改 |
| **身份 / 唯一键** | `order_id` + `expire_at` | `(run_id, step_seq)` + `next_retry_at`；定时评估用 `(eval_id, biz_hour)` | 同构，键多一维 |
| **任务的「重量」** | 关单 10ms、对账 10 分钟 | 一次重试 = 一次模型调用（**0.05~3 元**）；一次评估 = 上千次调用 | **异**：单个任务贵 1000 倍 |
| **失败模式** | 延迟不准、重复执行、任务丢失 | 同上 + **重复重试 = 重复花钱**；**评估任务跑两遍 = 预算翻倍** | 后果升级到成本 |
| **恢复方式** | 任务重投、位点回滚 | **从检查点续跑**；重投 = 重跑整个 Run | 根本差异（长事务 + 随时中断） |
| **观测指标** | 延迟分布、任务成功率、重复执行数 | 加 `retry_cost_total`、`eval_runs_total`、`checkpoint_age` | 指标集扩展 |
| **稀缺资源** | DB 扫描能力、Redis QPS、binlog 带宽 | **TPM、模型并发、预算** | 限的对象不同 |
| **CDC 的用途** | 缓存失效、数仓同步、Outbox | 加：**知识/记忆的增量同步**（文档变更 → 重新向量化） | 多一类下游 |
| **不可回滚** | 已关的单、已冲的正 | 已调用的工具、已花的 token | 同一条定律 |

**可以直接复用的（一套代码两边用）**：Redis ZSet 的 `pop/ack/recover` 三个 Lua 脚本、时间轮的 `Add/advance`、`job_execution` 表与抢占 SQL、Debezium 配置模板、CDC 五坑检查表。

**只有三样必须重算**：

1. **任务的价值密度变了**。传统延迟任务一条值几毫秒 CPU，可以「重复就重复」；Agent 的重试任务一条值几毛到几块，**必须先把「这次重试值不值」算清楚**——比如同一个 Step 连续失败 3 次，第 4 次大概率还是失败（模型/工具没变），应该直接进死信等人工，而不是继续烧钱。
2. **「定时评估」是一种新的定时任务**。它不改业务数据，但**每次都花钱**，所以它的幂等键是 `(eval_id, biz_hour)`，且必须有「预算闸门」：本小时已评估过就跳过，评估预算用完就降级到抽样。
3. **CDC 的下游多了一类「记忆」**。文档变更 → 重新切分 → 重新向量化 → 更新索引，这条链路**没有事务**，只能靠「版本号 + 幂等 upsert + 定期全量对齐」来保证最终一致。别指望 CDC 给你强一致。

> 三定律检验：**状态机**——延迟任务的真身必须在 DB（`task` 表）或 MQ，内存时间轮只是缓存；**稀缺资源**——传统抢 DB 扫描，Agent 抢预算；**不可回滚**——已执行的定时任务（关单、冲正、发券、调工具）只能补偿，所以「只让一个实例跑」必须在执行前解决。

---

## 28.6 会在哪炸：失效模式清单

| # | 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- | --- |
| 1 | 关单延迟从 30 分钟变 4 小时 | 扫表无索引 + 量大 → 每轮扫描超过间隔 | `EXPLAIN` 看 `type=ALL`；看任务日志的单轮耗时 | 加 `(status, expire_at)` 联合索引；改 Redis ZSet | 单轮耗时 > 间隔的 50% 即告警 |
| 2 | 同一订单被关两次 | 多实例 `@Scheduled` 无协调 | 查 `job_execution` 或日志里的 instance_id | 唯一键抢占 + 状态机条件更新 | 所有定时任务必须登记到 `job_execution` |
| 3 | 任务「取出了但没执行」 | 取出即删（至多一次） | 对比 ZSet 与业务表的任务数 | 改 processing 集合 + 租约 + recover | 延迟队列必须实现 ack |
| 4 | 同一任务被两个实例执行 | 租约短于处理时间；或没做原子取出 | 按 `task_id` 查执行记录时间戳 | 租约 > P99 处理时间 × 2；Lua 原子取出 | 业务侧仍要幂等（双保险） |
| 5 | 重启后一半定时任务消失 | 纯内存时间轮，无持久化 | 重启前后任务数对比 | 任务落库 + 启动重建 | 时间轮只做缓存，不做真相源 |
| 6 | CDC 重启后全量同步 2 亿行 | binlog 过期，位点失效 | Connect 日志 `no longer available`；`SHOW BINARY LOGS` | 从备份恢复或重新快照 | `binlog_expire_logs_seconds` ≥ 72h；停机告警 |
| 7 | 缓存脏 6 小时 | CDC 挂了没人发现，缓存只能等 TTL | 对比 `source.ts_ms` 与当前时间（延迟指标） | 恢复 connector；手动清热点 key | CDC 延迟 > 60s 即告警；关键缓存加短 TTL 兜底 |
| 8 | 对账跑两遍，账更乱了 | 广播式定时任务 + 无幂等 | 查 `job_execution` 同一天多条记录 | 改抢占式 + 冲正幂等 | 任务幂等键进上线检查 |
| 9 | Agent 每小时评估把预算烧掉一半 | 定时评估无幂等、无预算闸门 | 查 `eval_id + biz_hour` 的调用次数 | 加唯一键 + 抽样降级 | 评估预算独立核算（第 21 章成本 SLO） |

---

## 28.7 怎么验证：精度、幂等、CDC 三件事都要能测

**① 延迟精度压测**（这是延迟队列唯一的硬指标）：

```bash
# 灌 10 万个「5 秒后到期」的任务，测量实际触发时间分布
redis-cli -h redis1 EVAL "$(cat seed.lua)" 1 delay:{bench} 100000 5000
# seed.lua: for i=1,ARGV[1] do redis.call('ZADD',KEYS[1], tonumber(ARGV[2])+i, 'task-'..i) end

# 统计：P50/P99/最大值 与目标 5000ms 的偏差（精度 = tick 或轮询间隔）
redis-cli -h redis1 ZCOUNT delay:{bench} -inf +inf
mysql -e "SELECT
  TIMESTAMPDIFF(MICROSECOND, expect_at, fired_at)/1000 AS delay_ms,
  COUNT(*) FROM delay_task_log
 WHERE batch='bench' GROUP BY delay_ms ORDER BY delay_ms DESC LIMIT 20"
```

**② 多实例幂等验证**（3 副本同时触发，只能有一个生效）：

```bash
# 起 3 个实例，同一时刻触发同一个 job，然后检查
kubectl exec deploy/reconcile -- sh -c 'curl -XPOST localhost:8080/job/reconcileDaily/run'
mysql -e "SELECT job_name, biz_date, COUNT(*) AS runs, SUM(status=1) AS success
            FROM job_execution WHERE job_name='reconcileDaily' AND biz_date=CURDATE()
           GROUP BY job_name, biz_date"
# 期望：runs=1（唯一键），success=1；若 runs=3 说明唯一键没生效
```

**③ CDC 端到端延迟与重复率**：

```bash
# 延迟：事件里的 source.ts_ms 与消费时刻的差
kafka-console-consumer.sh --bootstrap-server broker1:9092 --topic cdc.trade.order_info \
  --max-messages 200 --property print.timestamp=true | \
  awk -F'CreateTime:' '{print $2}' | head -5
# 生产上做成指标：cdc_lag_seconds = now - source.ts_ms，>60s 告警

# 重复率：同一主键在一小时内被 upsert 的次数
mysql -e "SELECT order_id, COUNT(*) c FROM es_sync_log
           WHERE created_at >= NOW()-INTERVAL 1 HOUR
           GROUP BY order_id HAVING c > 1 ORDER BY c DESC LIMIT 20"
```

**④ CDC 位点检查（出事前最该看的一条）**：

```bash
curl -s http://connect:8083/connectors/trade-outbox-connector/status | jq '.tasks[].state'
# 期望 "RUNNING"；出现 "FAILED" 立刻看 trace

# 对比位点与当前 binlog，判断「还剩多少没追」
mysql -e "SHOW MASTER STATUS"    # 当前 file/pos
# 与 offset.storage.topic 里的 file/pos 对比；差距 = 落后的 binlog 量
```

---

## 28.8 用什么工具：延迟与调度买得到，幂等必须自己写

| 能力 | 开源/框架够用 | 必须自己写 |
| --- | --- | --- |
| 延迟消息 | RocketMQ 4.x 延迟级别 / 5.x 定时消息、Pulsar `deliverAfter`、RabbitMQ 延迟插件 | 延迟档位不够时的「多跳拼接」 |
| 延迟队列 | Redis ZSet（`ZADD` + `ZRANGEBYSCORE`）、Redis 7 的 `ZADD GT` | **原子 pop/ack/recover 三个 Lua**（框架不给你租约语义） |
| 时间轮 | Netty `HashedWheelTimer`、Kafka `TimerWheel`、`antlabs/timer` | 与持久化的配合（启动重建） |
| 定时调度 | xxl-job（单机/分片/失败重试/告警）、ElasticJob（分片）、Quartz（集群锁表）、K8s CronJob | **任务幂等键 + `job_execution` 抢占** |
| CDC | Debezium（MySQL/PG/Mongo）、Flink CDC、Canal | 下游幂等、位点监控、Outbox 清理顺序 |
| 一致性兜底 | —— | 对账（第 14 章）：CDC 丢一条，只有对账能发现 |

**判断口径**：**「什么时候触发」可以买**（MQ、调度平台、Debezium）；**「只触发一次、且副作用只发生一次」必须自己写**。任何声称「配好就永不重复」的工具，都是在把问题推给下游。

---

## 28.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 三者本质 | 延迟任务（业务定时）/ 定时任务（日历定时）/ CDC（提交定时），都是「时刻 + 通知」 |
| 五种实现 | MQ 延迟、时间轮、Redis ZSet、DB 轮询、调度平台；按「量级 × 精度 × 能否丢」选 |
| RocketMQ 4.x | 固定 18 档，30 分钟只能落 `30m`；要任意时间用 5.x 定时消息 |
| Redis ZSet | 必须「原子取出 + processing 集合 + 租约 + recover」，否则丢/重 |
| Lua 细节 | 同一 hash tag（`{queue}`）、`EVALSHA`、batch ≤ 200、租约 > P99 处理时间 |
| 时间轮 | O(1) 插入；三个坑：rounds 整圈 off-by-one、重启丢、回调阻塞 |
| 定时任务三模式 | 广播（必重复）、抢占（偶发重复）、分片（不重复但要幂等重跑） |
| 抢占的正确做法 | `job_execution` 唯一键 `(job_name, biz_date, shard_no)` + 心跳续租 |
| CDC 三个位置 | 数据同步、缓存失效、Outbox 投递（还有审计/搜索索引） |
| Debezium 位点 | `(binlog file, pos)` 或 GTID，存 `offset.storage.topic`；flush 间隔决定重复量 |
| CDC 五坑 | binlog 过期、重复投递、大事务、DDL、DELETE 的 before 为空（+ Outbox 清理竞态） |
| Agent 侧 | 延迟重试 = 延迟任务 + **成本闸门**；定时评估的幂等键是 `(eval_id, biz_hour)` |

## 28.10 自检题

1. **「下单 30 分钟未支付自动关单」，日均 200 万单。你会选哪种实现？说明精度、丢失风险与多实例方案。**
   > 要点：200 万/天 ≈ 23 单/秒，峰值可能 100/s，**DB 轮询能扛但精度差**（每分钟扫一次 → 关单延迟 0~60s，可接受）；但事故一的教训是「必须建 `(status, expire_at)` 索引」并且「单轮扫描时间 < 间隔的 50%」。更稳的是 **Redis ZSet**：`ZADD delay:{order} expire_at task`，200 个/秒的写入对 Redis 毫无压力，精度 = 轮询间隔（200ms~1s）。多实例用 §28.4 的 Lua 原子取出 + processing 集合 + 60s 租约。**丢失风险**：取出即删会丢，必须 ack；Redis 持久化（AOF everysec）最多丢 1 秒。**保底**：每天用「`status=PENDING_PAY AND expire_at < now-10m`」的 SQL 扫一遍补关（对账思维）。

2. **三个副本都配了 `@Scheduled(cron="0 30 2 * * ?")`，为什么会跑三遍？给出两种修法并说明各自的残留风险。**
   > 要点：`@Scheduled` 是**进程内**定时器，不做任何集群协调，所以三个进程各跑一遍。修法一：**数据库唯一键抢占**（`job_execution` 的 `(job_name, biz_date, shard_no)`），残留风险是「任务卡死但心跳还在」或「心跳停了被接管，原实例又活过来」→ 需要业务幂等 + 接管后旧实例的写要能被条件更新挡住。修法二：**调度中心**（xxl-job 单机路由 / ElasticJob 分片），残留风险是「调度中心认为失败并重试，实际第一次已成功」→ 仍然要幂等。两种修法都**不能替代业务幂等**，只能把「必然重复」降到「偶发重复」。

3. **Debezium 的 connector 停了 5 天，重启后报 `binlog position no longer available`。发生了什么？怎么恢复？怎么防止再发生？**
   > 要点：offset 里的 `(file, pos)` 指向的 binlog 已经被 `binlog_expire_logs_seconds`（或 `PURGE BINARY LOGS`）清理掉了，MySQL 无法从该位点继续投递，connector 只能重新做快照（全量）。恢复：① 确认 `snapshot.mode`（生产慎用 `initial`，会重放历史 → 下游大量重复），必要时用 `schema_only` + 手工补数据；② 大表用 incremental snapshot（signal 表）分块，避免一次性锁表；③ 恢复后立刻核对下游条数与主库。防止：`binlog_expire_logs_seconds ≥ 72h`（最好 7 天）、connector 状态与 `cdc_lag_seconds` 告警、`heartbeat.interval.ms` 必开（低峰期也推进位点）。

## 28.11 延伸阅读

- Redis：`ZADD`/`ZRANGEBYSCORE`/`ZREM` 语义、Lua 的原子性与 `EVALSHA`、Cluster 的 hash tag（`{}`）规则
- 时间轮：Kafka `TimerWheel`（`kafka.utils.timer`）与 Netty `HashedWheelTimer` 的 `rounds` 处理，对照 §28.4 的 `rounds--` 那一行
- RocketMQ：4.x `messageDelayLevel` 的 18 个档位与 `ScheduleMessageService`；5.x 定时消息（`deliverTimeMs`）与 `timerMaxDelaySec`
- 调度：xxl-job 的路由策略（第一个/轮询/一致性哈希/分片广播）、ElasticJob 的分片与失效转移、Quartz 的 `QRTZ_LOCKS` 集群锁
- CDC：Debezium MySQL Connector 文档的 `snapshot.mode` 全部取值、`offset.storage.topic` 与 `schema.history.internal.kafka.topic`、Outbox Event Router 的字段映射；MySQL `binlog_row_image` / `REPLICA IDENTITY`
- Outbox 与投递器（本章只讲 CDC 这一种实现）：[第 10 章](10-outbox-and-transactional-message.md)；对账（CDC 丢数据的兜底）：[第 14 章](14-reconciliation-system.md)
- 消息可靠性与重试退避：[第 26 章](26-message-reliability.md)；排查路径：[第 27 章](27-message-troubleshooting.md)；顺序与堆积：[第 25 章](25-ordering-and-lag.md)
- Agent 侧长任务与检查点：见 [../llm/12-long-task-durable.md](../llm/12-long-task-durable.md)；成本闸门：[第 21 章](21-slo-alerting.md)
- 总纲：[0-summary.md](0-summary.md) ｜ 语言与运行时：[../go/0-summary.md](../go/0-summary.md) ｜ 平台治理：[../llm/0-summary.md](../llm/0-summary.md)
- 前置：[第 27 章](27-message-troubleshooting.md) ｜ 下一章：[第 29 章 编排是什么：Orchestration vs Choreography](29-orchestration-vs-choreography.md)
