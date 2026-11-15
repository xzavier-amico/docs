# 13. 事件驱动与异步管道

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 请求生命周期与队列调度 ｜ **编号**：13 / 52
> **本章定位**：长任务一旦异步化，你的系统就从「函数调用」变成了「消息流」。本章讲 Kafka/NATS 在 Agent 平台里的正确姿势——什么该进 MQ、什么绝对不能进、以及「至少一次」这笔账最后到底由谁付。
> **读法**：如果你的平台还在用 HTTP 同步串工具，先读第 12 章判断该不该异步；已经上了 Kafka 的，直接看 §13.4.3 幂等和 §13.4.5 在线/离线隔离——这两处是 90% 事故的发源地。

---

## 13.1 引子：一条被渲染早了的工具结果

Aurora 的前端有个诡异 bug：偶尔，用户会在聊天框里先看到「已为您查到订单 A1234，预计 3 月 5 日送达」，**然后才看到「让我查一下物流…」**。

排查了两周。最后在水槽里捞出来的真相是：

```
Kafka topic: aurora.step.evt
分区键：tenant_id          ← 这里
分区数：48

同一个 Run 的 5 个事件，因为 tenant_id 相同……应该同分区啊？
```

等等，同分区就应该有序。真正的原因更阴险：

```go
// 生产端
producer.Send(ctx, &kafka.Message{
    Topic: "aurora.step.evt",
    Key:   []byte(strconv.FormatInt(evt.TenantID, 10)), // ← 租户做 key
    Value: payload,
})
```

租户 10231 在高峰期有 40 个 Run 并发跑，全都挤在同一个分区里。于是出现了两个后果：

1. **一个大租户把单个分区打成热分区**：48 个分区里，最大的那个租户独占 22% 的流量，其它 47 个分区加起来还没它多。
2. 更要命的是，消费者为了吞吐开了 **8 个 goroutine 并行处理同一个分区的消息**（`kafka-go` 的 `Reader` 默认就会 `FetchMessage` 后并发处理），**分区内有序在应用层被自己打碎了**。

前端拿到 `tool.completed(seq=7)` 和 `status(seq=6)` 的顺序反了，先渲染了结果，后渲染了「正在查询」。

> 🩸 这条事故的成本：修复代码 3 行（换分区键 + 串行化单分区），定位 2 周，客户投诉 11 起。**事件驱动系统里 80% 的「玄学 bug」，根因都是「你以为有序，其实没有」。**

---

## 13.2 先搞清楚：Agent 平台里有几类消息，各自什么语义

大部分团队上来就问「Kafka 还是 NATS」，这是错的问题。正确的问题是：**我的消息分几类，每类的有序性、延迟、可靠性要求分别是什么？**

| 类别 | 例子 | 有序性要求 | 延迟要求 | 可靠性 | 量大不大 | 归属 |
| --- | --- | --- | --- | --- | --- | --- |
| **命令** | 「创建 Run」「取消 Run」「重试 Step」 | 按 run_id 有序 | 低（ms~s） | 至少一次 + 幂等 | 小（300/s） | MQ |
| **状态事件** | `step.started/completed/failed` | 按 run_id 严格有序 | 低 | 至少一次 | 中（8k/s） | MQ |
| **token 增量** | 每个 delta 片段 | 严格有序 | **极高（必须实时）** | 尽力而为 | **巨大（240k/s）** | **不走 MQ** |
| **计费用量** | `usage.recorded` | 无需有序 | 低 | **至少一次，不可丢** | 中（1.2k/s） | MQ + 账本 |
| **审计事件** | `audit.appended` | 按 run_id 有序 | 低 | 至少一次 + 不可篡改 | 中（8k/s） | MQ → WORM |
| **离线样本** | 采样日志、评估样本 | 无需有序 | **无要求（小时级）** | 至少一次 | 批 | 独立管道 |

### 最关键的一条：token delta 绝对不能进 Kafka

这是新手最容易犯的错，也是最容易算出账的错：

```
Aurora：300 Run/s × 平均 800 输出 token = 240,000 msg/s
单条 delta 消息 200 字节（含 key/header 开销）
→ 生产速率 48 MB/s
→ 三副本写入 144 MB/s
→ 保留 3 天：144 MB/s × 86,400 s × 3 = 37 TB
→ 每个消费组还要按 240k msg/s 做 upsert，3 个组 = 720k ops/s
```

**37 TB 磁盘 + 72 万 ops/s 的消费能力，换来的是「可以重放 token 流」这个你根本不需要的能力**——没有用户会要求「把三天前那次对话的逐字渲染过程重放一遍」。而它的实时性要求（前端 100ms 内看到字）恰恰是 MQ 最不擅长的。

正确做法：

```
流式 delta：Run Worker ──SSE/gRPC──▶ Agent-Server ──SSE──▶ 前端
                              │
                              └─ 本地 ring buffer（最近 4KB）
                                 断线时按 Last-Event-ID 从 buffer 补发
                                 攒够一个「段」再发一条聚合事件到 Kafka
```

**一个 400 字回答 = 1 条 `message.segment` 事件，而不是 400 条 delta。** 数据量降 400 倍，有序性问题同时消失（详见第 7 章、第 36 章）。

---

## 13.3 结构图：三类 topic、四个阶段

```
                        ┌──────────────────────────────────────────────┐
   用户请求 ──▶ Run API │ ① 事务写：run 行 + outbox 行（同一 DB 事务）    │
                        └───────────────────┬──────────────────────────┘
                                            │ relay（SKIP LOCKED 批量取）
                                            ▼
   ╔════════════════════════════════════════════════════════════════════╗
   ║ 命令层  aurora.run.cmd  (key=run_id, 32 分区)                      ║
   ╚════════════════════════════╤═══════════════════════════════════════╝
                                ▼
                     ┌──────────────────┐   失败 3 次
                     │ run-dispatcher   │──────────────┐
                     └────────┬─────────┘              │
                              │ 执行                    ▼
                              │              ┌──────────────────────┐
                              │              │ DLQ  aurora.*.dlq    │
                              │              │ 人工/自动重放        │
                              │              └──────────┬───────────┘
                              ▼                         │ 修复后回灌
   ╔════════════════════════════════════════════════════╪═══════════════╗
   ║ 事件层  aurora.step.evt  (key=run_id, 48 分区, 保留 3d)             ║
   ╚═══╤═══════════════╤═══════════════╤═══════════════╤════════════════╝
       ▼               ▼               ▼               ▼
  ┌─────────┐    ┌──────────┐   ┌───────────┐   ┌──────────────┐
  │projector│    │ billing  │   │  trace    │   │ audit-writer │
  │幂等 upsert│   │ 去重表   │   │ 采样写     │   │ 哈希链 append │
  └────┬────┘    └────┬─────┘   └───────────┘   └──────┬───────┘
       │              │                                 │
       ▼              ▼                                 ▼
   run_projection  账本流水(不可变)              审计库(WORM)

   ══════════════════════════════════════════════════════════════════
   离线层（独立集群，物理隔离） aurora.offline.*  → ClickHouse → 数仓
   ══════════════════════════════════════════════════════════════════
```

**四个阶段各有各的坑**：① 生产端（事务与消息的一致）→ ② 传输端（有序与分区）→ ③ 消费端（幂等）→ ④ 失败端（延迟重试与死信）。

---

## 13.4 关键设计

### 13.4.1 topic 设计：一张表定生死

| topic | 分区键 | 分区数 | 保留 | 生产速率 | 消费组 | 语义 |
| --- | --- | --- | --- | --- | --- | --- |
| `aurora.run.cmd` | `run_id` | 32 | 3d | 300/s | `run-dispatcher` | 至少一次 + 幂等 |
| `aurora.step.evt` | `run_id` | 48 | 3d | 8,000/s | `projector`、`billing`、`trace` | 至少一次，分区内有序 |
| `aurora.step.retry` | `run_id` | 16 | 7d | 200/s | `retry-pump` | 至少一次 |
| `aurora.step.evt.dlq` | `run_id` | 8 | 30d | < 50/s | `dlq-inspector` | 至少一次 |
| `aurora.billing.usage` | `tenant_id` | 24 | 30d | 1,200/s | `billing-settler` | **至少一次，不可丢** |
| `aurora.audit.append` | `run_id` | 16 | 365d | 8,000/s | `audit-writer` | 至少一次 → 哈希链 |
| `aurora.offline.sample` | `tenant_id` | 12 | 7d | 批 | `etl`（独立集群） | 至少一次，允许 lag |

三条硬规则，违反任何一条都会炸：

1. **分区键必须是 `run_id`，不是 `tenant_id`。** 按租户做 key 会产生热分区：Aurora 最大租户占 22% 流量，48 个分区里有一个吃掉 5,000 msg/s，其余 47 个平均 60 msg/s——你会为这一个分区加 broker，然后发现没用。
2. **消费者实例数 ≤ 分区数。** Kafka 的 rebalance 协议保证「一个分区在同一消费组内只分配给一个消费者」。48 分区 = 单消费组最多 48 个 Pod，第 49 个空转。**这条在扩容时最容易被忽略**：你的 HPA 把 Pod 扩到 60，结果 12 个 Pod 永远拿不到分区、CPU 0%、被 HPA 判定为「空闲」再缩回去，形成震荡。
3. **保留期必须 ≥ 最大重放窗口。** 你想「回放最近 5 天的数据修 bug」，但保留期是 3 天——第 4 天你只能对着空气 debug。

### 13.4.2 生产端：Outbox，别用「先写库再发消息」

```sql
-- 业务表与 outbox 在同一事务里写，解决「库写成功、消息发失败」
CREATE TABLE outbox (
  id            BIGSERIAL     PRIMARY KEY,
  aggregate_id  TEXT          NOT NULL,        -- run_id
  topic         TEXT          NOT NULL,
  partition_key TEXT          NOT NULL,        -- = aggregate_id，保证同 Run 同分区
  headers       JSONB         NOT NULL DEFAULT '{}',
  payload       BYTEA         NOT NULL,
  created_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
  published_at  TIMESTAMPTZ
);

-- 只索引未发送的，索引很小
CREATE INDEX idx_outbox_unpublished ON outbox (id) WHERE published_at IS NULL;
```

relay 用 `FOR UPDATE SKIP LOCKED` 批量取，天然支持多 relay 实例并行：

```sql
BEGIN;
SELECT id, topic, partition_key, headers, payload
  FROM outbox
 WHERE published_at IS NULL
 ORDER BY id
 LIMIT 500
   FOR UPDATE SKIP LOCKED;

-- 逐条发 Kafka（同步等 ack，或用事务型 producer）
UPDATE outbox SET published_at = now() WHERE id = ANY($1);
COMMIT;
```

> 💡 **relay 崩在「发了 Kafka、还没 UPDATE」之间会重复发一条。** 这不是 bug，这是 Outbox 的已知代价——**至少一次是设计选择，不是缺陷**。所以消费端必须幂等（下一节）。想彻底消除重复，只能用 Kafka 事务 + `read_committed`，但事务型 producer 的吞吐会掉 20~40%，且要求消费端也配 `isolation.level=read_committed`，运维复杂度陡增。**在 Agent 场景里，99% 的团队应该选「幂等消费」而不是「事务消息」**。

### 13.4.3 消费端幂等：先把事件分成两类

**不要给所有事件都建去重表。** 8,000/s × 86,400 = 6.9 亿行/天，任何数据库都扛不住。正确的分法是：

| 事件类型 | 处理方式 | 为什么 | 数据量占比 |
| --- | --- | --- | --- |
| 投影类（更新 Run 状态、更新 UI） | **幂等 upsert + 水位线** | 天然的「最后一次写入赢」，重复处理结果相同 | ~85% |
| 副作用类（发通知、扣额度、写外部系统） | **去重表 + 同事务** | 重复执行会造成真实损失 | ~15% |
| 账本类（计费用量） | **业务主键唯一约束** | `(run_id, seq, usage_type)` 唯一，插入冲突即忽略 | ~5% |

```go
// 副作用类事件：去重记录与业务写入必须在同一个事务里
func (h *Handler) Handle(ctx context.Context, e Event) error {
	return pgx.BeginFunc(ctx, h.pool, func(tx pgx.Tx) error {
		// ① 抢占式去重：key = (consumer, event_id)
		tag, err := tx.Exec(ctx, `
			INSERT INTO consumed_event (consumer, event_id, tenant_id, consumed_at)
			VALUES ($1, $2, $3, now())
			ON CONFLICT (consumer, event_id) DO NOTHING`,
			h.name, e.EventID, e.TenantID)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 0 {
			h.metrics.DuplicateTotal.WithLabelValues(e.Type).Inc()
			return nil // 已处理过，直接成功返回（不要报错，否则会触发重投）
		}

		// ② 业务写入：和去重记录同生共死
		switch e.Type {
		case "tool.completed":
			if _, err := tx.Exec(ctx, `
				INSERT INTO run_step (run_id, seq, tool_name, result_ref, finished_at)
				VALUES ($1, $2, $3, $4, $5)
				ON CONFLICT (run_id, seq) DO NOTHING`,
				e.RunID, e.Seq, e.Payload.ToolName, e.Payload.ResultRef, e.OccurredAt); err != nil {
				return err
			}
		case "notify.requested":
			// 通知是副作用，交给下游幂等发送器（它自己也有去重键）
			if err := h.notifier.EnqueueIdempotent(tx, e.Payload.NotifyKey, e.Payload.Target); err != nil {
				return err
			}
		}
		return nil
	})
}
```

**四个必须记住的细节**：

1. `ON CONFLICT DO NOTHING` 返回 0 行 → **返回 nil，不返回 error**。返回 error 会让 MQ 无休止重投，形成毒消息。
2. **去重表的 TTL 必须 ≥ topic 保留期**。保留 3 天、去重表只留 1 小时，那么「回放三天前的 offset」会把所有副作用重新执行一遍。这是重放事故的头号原因。
3. **幂等键选择三档**：业务键（`(run_id, seq)`，最稳）> 事件 ID（`event_id`，生产者生成 UUID）> 位点（`(topic, partition, offset)`，只对固定消费组有效，换组就失效）。
4. **去重表要分区 + 定期归档**。按 `consumed_at` 做 range 分区，每天 detach 掉超过 7 天的分区（`DROP` 比 `DELETE` 快两个数量级）。

### 13.4.4 延迟队列、死信、重放

**Kafka 没有原生延迟消息**。这是从 RocketMQ 迁过来的人最不适应的一点。三种可行方案：

| 方案 | 实现 | 精度 | 适用量级 | 主要坑 |
| --- | --- | --- | --- | --- |
| 分层延迟 topic | `delay.5s` / `delay.1m` / `delay.10m`，各由一个 pump 定时搬运 | 秒级 | 千万级 | 层数越多越难运维；超过 1 小时的延迟要落库 |
| 调度表 + 扫描器 | PG/MySQL 存 `deadline`，`SKIP LOCKED` 批量捞，投回主 topic | 秒级 | 亿级 | 扫描器是隐式单点，必须分片 |
| Redis ZSET | `score = deadline`，Lua 原子 `ZRANGEBYSCORE` + `ZREM` | 毫秒级 | 千万级 | 持久性弱于 DB，要开 AOF |
| NATS JetStream | `msg.NakWithDelay(d)`，消息级原生 | 毫秒~秒 | 中等 | 重投次数受 `MaxDeliver` 硬限制 |

NATS 的写法最省事，一个 `NakWithDelay` 就够了：

```go
func (h *Handler) Handle(msg jetstream.Msg) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	meta, _ := msg.Metadata()
	e, err := decode(msg.Data())
	if err != nil {
		_ = msg.Term() // 解码不了 = 永远处理不了，直接终止，不要重投
		return
	}

	if err := h.do(ctx, e); err != nil {
		if meta.NumDelivered >= 5 {
			// 超过 5 次 → 转死信，并把原始消息作为 payload 带上，便于回灌
			h.dlq.Publish(ctx, e, err)
			_ = msg.Term()
			return
		}
		// 指数退避，由服务端负责计时，不占你的 goroutine
		_ = msg.NakWithDelay(time.Duration(1<<meta.NumDelivered) * time.Second)
		return
	}
	_ = msg.Ack()
}
```

Kafka 侧则要自己写 pump：

```go
// 分层延迟：每层一个定时器，扫描到期消息并投递到下一跳
func (p *Pump) Run(ctx context.Context) {
	tk := time.NewTicker(500 * time.Millisecond)
	defer tk.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tk.C:
			due, err := p.store.PopDue(ctx, p.tier, 500) // Redis ZSET: ZRANGEBYSCORE + ZREM（Lua 原子）
			if err != nil || len(due) == 0 {
				continue
			}
			for _, d := range due {
				next := p.route(d) // 还没到期就进下一层，到期了就回主 topic
				p.producer.Send(ctx, &kafka.Message{
					Topic: next,
					Key:   []byte(d.PartitionKey),
					Value: d.Payload,
				})
			}
		}
	}
}
```

**死信（DLQ）的三条纪律**：

- **DLQ 消息必须自带原因**：`{original_topic, partition, offset, consumer, attempts, last_error}`。否则两周后你打开 DLQ，完全不知道该拿它怎么办。
- **DLQ 消费者只能读，不能自动修复后回灌**。回灌必须是人工触发的、限速的、可审计的动作。自动回灌 = 把毒消息循环回生产环境。
- **DLQ 要有告警**。DLQ 里堆了 3 万条没人看，等于没有 DLQ。

**重放（replay）是最危险的操作**，因为它会重放副作用。标准动作：

```
① 选择一个影子消费组（不是线上组），从指定 offset 开始
② 消费组名带 replay 前缀，其消费端强制进入「只写影子表」模式
③ 回放速率限制在线上速率的 10~20%，避免打爆下游
④ 影子表比对差异 → 人工确认 → 才决定是否修正线上数据
⑤ 绝不 reset 线上消费组的 offset（reset 会让所有副作用重跑）
```

### 13.4.5 乱序与版本：靠水位线，不靠运气

即使分区键选对了，还有三类乱序逃不掉：

| 乱序来源 | 场景 | 对策 |
| --- | --- | --- |
| 多生产端 | 两个服务同时给同一 Run 发事件 | 事件带 `seq`（Run 内单调），消费端按 seq 重排 |
| 重投 | 失败消息延迟 30s 后重投，落在新消息之后 | 幂等 upsert 天然容错；状态机拒绝回退 |
| 版本错位 | 生产者已升级到 schema v3，消费者还是 v2 | `schema_version` 字段 + 双读单写 |

**水位线（watermark）是唯一能根治乱序的结构**：

```sql
-- 投影表带 applied_seq，只允许单调前进
UPDATE run_projection
   SET applied_seq = $2,
       status      = $3,
       body        = $4,
       updated_at  = now()
 WHERE run_id = $1
   AND applied_seq < $2;   -- ★ 迟到的旧事件自然被丢弃
```

配套一个重排缓冲：收到 `seq = applied + 2` 时，说明 `applied + 1` 还没到。两种处理：

- **短暂缓冲**：等 200ms 或攒够 50 条再 flush（适合在线，牺牲 200ms 延迟）
- **直接不处理**：先落 `pending_events` 表，等水位线推进后再补（适合离线，不牺牲延迟）

> ⚠️ 别指望「Kafka 保证有序」。它只保证**单分区内**有序，且前提是**你的消费者也单线程处理该分区**。`kafka-go` 的 `Reader` 默认会并发处理同一批消息；Java 的 `ConcurrentMessageListenerContainer` 默认并发度也是 1 但可以调到 N。**有序性是你自己写代码保证的，不是 MQ 送的。**

schema 版本演进只有一条规则：**新增字段可选、旧字段永不删除、字段号永不复用**（Protobuf 语境）。没有 schema registry 时，最小可行方案是 `schema_version` 整数 + 消费端 `switch`：

```go
switch e.SchemaVersion {
case 1, 2:
	return h.handleV2(ctx, e)   // v1/v2 结构兼容，同一处理路径
case 3:
	return h.handleV3(ctx, e)   // v3 新增字段，独立路径
default:
	return fmt.Errorf("unsupported schema_version=%d", e.SchemaVersion) // 进 DLQ，不要静默丢弃
}
```

### 13.4.6 在线流式与离线批处理：必须物理隔离

| 维度 | 在线管道 | 离线管道 |
| --- | --- | --- |
| 集群 | 主力集群（高配 SSD、3 副本） | **独立集群**（可低配、可混部） |
| topic 前缀 | `aurora.*` | `aurora.offline.*` |
| 消费组 | 独立，`MaxAckPending` 严格限制 | 独立，允许 lag 数小时 |
| 优先级 | 高 | 低 |
| 数据来源 | 业务直接写 | 从在线 topic 镜像（MirrorMaker 2）或数仓 |
| 保留期 | 3 天 | 30 天 / 直接落 S3 |
| 失败处理 | 立即告警 | 允许重跑 |

**为什么必须物理隔离？** 因为 **Kafka 没有消息优先级**。你无法让离线消费者「让路」——离线消费者一旦 lag，它会疯狂 fetch，把 broker 的页缓存和网络带宽吃光，在线消费的 P99 直接从 40ms 涨到 2s。

Kafka 提供的唯一软手段是**客户端配额**：

```bash
# 限制离线消费组的带宽，保护在线消费
kafka-configs.sh --bootstrap-server broker:9092 --alter \
  --entity-type clients --entity-name offline-etl \
  --add-config 'consumer_byte_rate=10485760,producer_byte_rate=5242880'
```

但配额只是「限速」，不是「隔离」。**真正的隔离是独立集群**。Aurora 的账：离线集群用 6 台普通机型（存储型），成本是在线集群的 18%，但把在线 P99 的抖动从 ±300% 压到 ±15%。

---

## 13.5 Java / 分布式经验对照

| 你的老经验 | Agent 管道里的对应 | 会咬人的差异 |
| --- | --- | --- |
| RocketMQ 顺序消息 | Kafka 分区有序 | Kafka 只保证**分区内**有序，且要求消费端单线程处理该分区；RocketMQ 的 `MessageQueueSelector` 思路可以平移，但「顺序消费」这个开关 Kafka 没有 |
| RocketMQ 延迟消息（18 级别） | 延迟队列需自建 | Kafka 无原生延迟；NATS JetStream 的 `NakWithDelay` 最接近 |
| `@KafkaListener` + `AckMode.MANUAL` | 幂等消费 | 手动 ack 只解决「不丢」，不解决「不重」。重复投递是契约的一部分 |
| `enable.auto.commit=true` | — | **事故之源**：先提交位点后处理业务，崩溃即丢消息。一律关掉 |
| Spring 事务 + `@Transactional` | 去重表同事务 | 同构。但注意 MQ 提交不在事务内，所以只能做到「处理幂等」，做不到「恰好一次」 |
| Redis 分布式锁 | 消费去重 | 锁会释放，去重记录必须持久化。TTL 必须 ≥ 重放窗口 |
| 消费者数 ≤ 分区数 | 完全一致 | 扩容时最容易踩：HPA 扩过头 → 多余 Pod 空转 → HPA 误判缩容 → 震荡 |
| 死信队列（RocketMQ `%DLQ%`） | DLQ topic | 同构。但 Agent 的 DLQ 消息要带「原始上下文引用」，否则无法复现 |

---

## 13.6 开源坐标

| 项目 | 给了什么 | 缺什么 |
| --- | --- | --- |
| **Kafka** | 分区有序、至少一次、按 offset 重放、生态最全、客户端配额 | 无原生延迟消息、无优先级、无内置 schema 演进（要配 Schema Registry）、消费者数受分区数限制 |
| **NATS JetStream** | 轻量（单二进制）、原生 `NakWithDelay`、`MaxDeliver`/`AckWait`/`MaxAckPending` 一等公民、KV 与 ObjectStore 内建 | 生态与工具链弱于 Kafka；超大规模（>10 万 msg/s 持久化）的运维经验少；跨地域复制不如 Kafka 成熟 |
| **Pulsar** | 存算分离、原生延迟消息、多租户、队列与流统一 | 运维复杂度最高（BookKeeper + Broker + ZooKeeper），小团队别碰 |
| **Schema Registry（Confluent / Apicurio）** | 兼容性检查（BACKWARD/FORWARD/FULL） | 要额外部署；自建成本高于收益时，用 `schema_version` 字段兜住 |
| **Debezium** | 直接订阅 binlog 做 Outbox relay，省掉自研 relay | 需要 CDC 权限；DDL 变更时偶有 schema 漂移问题 |

> 🎯 **选型判据**：消息量 < 5 万/s、需要延迟消息、团队 < 10 人 → **NATS JetStream**；已经在用 Kafka 或量级 > 10 万/s → **Kafka + 自建延迟队列**；需要多租户硬隔离 + 跨地域 → **Pulsar**（但要配专职 SRE）。

---

## 13.7 事故清单

- [ ] **事故 A：分区键选错导致热分区。** 现象：48 个分区里 1 个吃掉 60% 流量，加 broker 无效。→ 根因：按 `tenant_id` 做 key。→ 兜底：分区键一律 `run_id`；上线前用真实流量分布做 key 基数分析。
- [ ] **事故 B：重放把副作用重跑了一遍。** 现象：修复 bug 后回放 3 天数据，客户收到 8,000 条重复通知。→ 根因：去重表 TTL 只有 2 小时，短于重放窗口。→ 兜底：去重表 TTL ≥ topic 保留期；重放只在影子消费组 + 影子表。
- [ ] **事故 C：毒消息无限重投。** 现象：单个分区卡死，lag 从 0 涨到 200 万。→ 根因：反序列化失败后返回 error，无限重投。→ 兜底：解码失败直接 `Term()`/跳过并进 DLQ；重投上限 5 次。
- [ ] **事故 D：扩容后消费停滞。** 现象：Pod 从 32 扩到 60，吞吐没涨，12 个 Pod CPU 0%。→ 根因：分区数 48 < 消费者数 60。→ 兜底：分区数按「峰值消费者数 × 1.5」预留；或改用共享订阅模式（NATS）。
- [ ] **事故 E：auto-commit 丢消息。** 现象：服务重启后 200 条计费用量凭空消失。→ 根因：`enable.auto.commit=true` + 异步处理。→ 兜底：关自动提交，业务成功后再提交位点。
- [ ] **事故 F：离线任务拖垮在线。** 现象：每天凌晨 2 点数仓 ETL 一跑，在线 P99 从 40ms 涨到 2.1s。→ 根因：共用集群 + 无配额。→ 兜底：独立集群；至少加 `consumer_byte_rate` 配额。
- [ ] **事故 G：schema 演进把老消费者打挂。** 现象：生产者加了必填字段，消费者全量反序列化失败。→ 根因：没有兼容性约束。→ 兜底：只加可选字段、只删可选字段、`schema_version` 必须存在且消费端显式分派。
- [ ] **事故 H：token delta 灌进 Kafka。** 现象：集群磁盘 3 天写满，broker 全部只读。→ 根因：逐 token 发消息。→ 兜底：delta 走 SSE；Kafka 只收「段」级聚合事件。

---

## 13.8 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 分类先行 | 命令 / 状态事件 / token 增量 / 计费 / 审计 / 离线样本，六类各有语义，别用一种管道装 |
| token delta | 绝不进 Kafka：240k msg/s、37 TB/3 天，换来的能力你不需要 |
| 分区键 | 一律 `run_id`；用 `tenant_id` 必产生热分区 |
| 消费者上限 | 单消费组消费者数 ≤ 分区数，扩容前先看分区 |
| Outbox | 解决「库写成功、消息发失败」；代价是至少一次，用幂等消费兜住 |
| 幂等分类 | 投影类用幂等 upsert，副作用类才建去重表（只占 15%） |
| 去重 TTL | 必须 ≥ topic 保留期，否则重放会重跑副作用 |
| 去重返回 | 命中重复必须 `return nil`，不要 `return err`（否则毒消息死循环） |
| 有序性 | MQ 只保分区内有序，还要你单线程消费；跨分区靠 `seq` + 水位线 |
| 延迟队列 | Kafka 没有；用分层 topic / 调度表 / Redis ZSET，NATS 用 `NakWithDelay` |
| 重放 | 只在影子消费组 + 影子表，限速 10~20%，绝不 reset 线上位点 |
| 在线/离线 | Kafka 无优先级，唯一隔离手段是独立集群（+ 配额做软限速） |
| 一句话 | **至少一次不是缺陷，是契约；幂等不是优化，是义务。** |

---

## 13.9 自检题

1. **你的消费端做了幂等去重，为什么重放时还是会重复发通知？**
   > 要点：去重表的 TTL 短于重放窗口。去重记录过期后，同一条事件在重放时被当作新事件处理。正确做法：去重表 TTL ≥ topic 保留期（或去重键直接由业务主键构成，永不过期）；重放只在影子消费组做，其消费端强制写影子表。另外要检查幂等键是否包含 `run_id`——只按 `event_id` 去重，而 `event_id` 由生产端每次重发时重新生成，就完全失效。

2. **为什么 Kafka 的分区键不能用 `tenant_id`？**
   > 要点：① 流量分布不均会产生热分区（Aurora 最大租户占 22% 流量，单分区被打爆，加 broker 无效，因为热分区无法拆分）；② 同一租户下多个并发 Run 的事件会串在一条有序流里，无谓地互相阻塞；③ 而 `run_id` 做 key 既保证「同一 Run 内有序」这个唯一真正需要的约束，又天然把流量打散。

3. **「我们用了 Kafka，所以消息不会丢」这句话哪里错了？**
   > 要点：Kafka 的保证是「已提交的消息在副本存活时不丢」，但你的链路里还有三个丢消息的窗口：① 生产端——业务事务提交了、消息还没发（用 Outbox 补）；② 消费端——auto-commit 位点先于业务处理完成（关自动提交）；③ 消费端——业务抛异常但你把 error 吞了并 ack（必须区分「可重试」与「终止」）。端到端的「不丢」需要三段各自兜住，MQ 只负责中间那段。

---

## 13.10 延伸阅读

- 上一章：[12. 长任务与持久化执行](12-long-task-durable.md)
- 下一章：[14. 调度器设计：GPU/CPU 混合与并发度](14-scheduler-design.md)
- Apache Kafka 官方文档：`enable.idempotence`、`isolation.level`、consumer group rebalance 协议、client quotas（以官方最新版为准）
- NATS JetStream 官方文档：`MaxDeliver`、`AckWait`、`MaxAckPending`、`NakWithDelay`
- Debezium 官方文档：Outbox Event Router（`io.debezium.transforms.outbox.EventRouter`）
- 相关：第 6 章（事件溯源）、第 12 章（checkpoint 与重放）、第 27 章（计费账本的幂等）、第 39 章（trace 管道）、第 45 章（存储选型）

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 语言/运行时细节：见 [../go/0-summary.md](../go/0-summary.md)
- 前置：第 12 章
- 下一章：第 14 章
