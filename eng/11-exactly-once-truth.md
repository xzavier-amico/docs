# 11. Exactly-once 的真相：只有端到端幂等

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · 事务与一致性 ｜ **编号**：11 / 52
> **本章定位**：把「恰好一次」从营销词拆成可执行的工程约束：**生产端不丢 + 消费端幂等**。这一章讲清楚 ack 丢失的两种情形、位点提交与业务提交的先后、Kafka 事务的边界，并给出一套能直接抄的端到端幂等模板。
> **读法**：§11.3 的崩溃时间线必须看懂；§11.4.3 的模板可以直接建表；§11.5 是 Agent 侧最容易多花钱的地方。

---

## 11.1 引子：同一套代码，两个方向的错

**事故一：先提交位点，再处理（2025-02-14）**

营销服务消费「用户注册」事件发新人券。消费者用了 `enable.auto.commit=true`，Kafka 每 5 秒自动提交位点；业务处理在另一个线程池里异步执行。

- 14:32，Pod 因内存超限被 OOM Kill；此时位点已提交到 offset 8,842,100，但线程池里还有 12,600 条事件没处理。
- 重启后从 8,842,100 之后开始消费，**12,600 个新用户永远没收到新人券**。
- 客诉 3,100 起，补发成本 31 万元，T+2 对账才发现「注册数 - 发券数」差 12,600。

**事故二：先处理，后提交，但消费不幂等（2025-06-03）**

积分服务改成「先处理、再手动提交位点」，但积分流水没有唯一索引。当天一次 rebalance，同一个分区被两个实例先后消费。

- 4,200 个用户被重复加积分，人均 200 分，可抵扣金额 **8.4 万元**。
- 更麻烦的是：部分用户已经用重复积分下了单，回收积分要把订单一起处理，客诉 860 起。

> 🩸 两个事故合起来就是 Exactly-once 的全部真相：**先提交位点会丢，先处理会重；只有「先处理 + 幂等 + 后提交」才能既不丢也不重。** MQ 本身给不了这个保证，它只能给你「至少一次」或「至多一次」。

---

## 11.2 到底是什么：三种语义，只有一种能落地

**定义**：

- **至多一次（at-most-once）**：消息最多被处理一次，可能丢。实现方式是「先提交位点/先 ack，再处理」。
- **至少一次（at-least-once）**：消息至少被处理一次，可能重。实现方式是「先处理，再提交位点/ack」。
- **恰好一次（exactly-once）**：每条消息的业务效果恰好一次。它不是 MQ 的单点能力，而是**端到端**的组合：生产端不丢 + 消费端幂等（+ 必要时事务）。

**为什么 MQ 只能二选一？** 因为「处理完成」和「位点提交/ack」是两个独立的写操作，它们之间没有原子性。看两种 ack 丢失：

```
情形 A：消费端处理成功，ack/位点提交丢失
 Consumer                    Broker
    │◄──── msg(offset=100) ────│
    │ 处理成功（DB 已提交）      │
    ├──── ack(100) ───────────►│  ✗ 网络丢包/消费者崩溃
    │                          │  认为 100 未确认
    │◄──── msg(offset=100) ────│  重投
    │ 再处理一次 → 重复副作用    │

情形 B：生产端发送成功，broker ack 丢失
 Producer                    Broker
    ├──── msg ───────────────►│  已落盘
    │◄──── ack ───────────────┤  ✗ ack 丢包
    │ 认为失败 → 重试           │
    ├──── msg（同一条）────────►│  再落一条 → 重复
```

所以：

| 语义 | 实现 | 结果 | 适用 |
| --- | --- | --- | --- |
| 至多一次 | 先 commit offset / 先 ack，再处理 | 可能丢，不重 | 可丢的日志、指标 |
| 至少一次 | 先处理，再 commit offset / ack | 不丢，可能重 | 绝大多数业务 |
| 恰好一次 | 生产端不丢 + 消费端幂等 + 事务/去重 | 业务效果一次 | 资金、积分、发券、扣库存 |

> 💡 记住一句话：**「恰好一次」不是「消息只投一次」，而是「业务效果只发生一次」。** 消息可以投 10 次，只要第 2~10 次都被幂等挡住，业务上就是恰好一次。

---

## 11.3 结构图：位点与业务提交之间的死亡窗口

```
时间线（Kafka 消费 + MySQL 业务写）：

T1  poll 拿到 msg(offset=100)
T2  BEGIN DB transaction
T3  INSERT INTO points_flow(...)          ← 业务写
T4  COMMIT DB                             ← 业务事实已生效
T5  commit offset=101                     ← 位点已推进

崩溃点分析：
  T4 之后、T5 之前崩溃 → 位点仍是 100 → 重投 → 业务已生效 → 重复（至少一次）
  若代码改成先 T5 后 T4：T5 之后、T4 之前崩溃 → 位点已到 101 → 不重投 → 业务没生效 → 丢（至多一次）
  T2~T4 之间崩溃       → DB 回滚 + 位点未提交 → 重投 → 安全（前提是幂等）

结论：T4 和 T5 无法在同一个原子事务里（跨 DB 与 Kafka），
      所以只能选「先 T4 后 T5」= 至少一次，再用幂等把重复吃掉。

Kafka 事务的边界：
┌──────────────────────────────────────────────────────────┐
│ Kafka transaction（producer.beginTransaction / commit）   │
│   ├─ 发送到 output topic 的记录                            │
│   └─ 消费位点（sendOffsetsToTransaction）                  │
│                                                          │
│   ✗ 你的 MySQL 写入不在这个事务里                           │
│   ✗ 你的 Redis 写入不在这个事务里                           │
│   ✗ 你的第三方调用不在这个事务里                            │
└──────────────────────────────────────────────────────────┘
```

**Kafka 事务能做什么**：在「消费 Kafka → 处理 → 生产 Kafka」的流处理里，把「输出记录 + 输入位点」原子提交，配合 `isolation.level=read_committed`，实现 **Kafka 内部**的 exactly-once。

**Kafka 事务不能做什么**：它不覆盖你的数据库。你在事务里写 MySQL，MySQL 的提交仍然独立；「Kafka commit 成功、MySQL commit 失败」或反之，都会产生不一致。**跨 DB 的恰好一次，仍然要靠幂等表/Outbox。**

---

## 11.4 怎么实现：从「二选一」到「端到端幂等」

### 11.4.1 位点提交的两种写法（代码对比）

```java
// ❌ 至多一次：先提交位点，再处理。崩溃/异常 → 消息永久丢失
consumer.subscribe(List.of("user-registered"));
while (running) {
    ConsumerRecords<String, String> records = consumer.poll(Duration.ofMillis(500));
    for (ConsumerRecord<String, String> r : records) {
        consumer.commitSync(Map.of(
            new TopicPartition(r.topic(), r.partition()),
            new OffsetAndMetadata(r.offset() + 1)));
        handler.handle(r.value());          // 这里抛异常/进程被杀 → 消息没了
    }
}

// ✅ 至少一次：先处理（幂等），再提交位点。崩溃 → 重投 → 幂等挡住
consumer.subscribe(List.of("user-registered"));
while (running) {
    ConsumerRecords<String, String> records = consumer.poll(Duration.ofMillis(500));
    for (ConsumerRecord<String, String> r : records) {
        handler.handleIdempotent(r.value()); // DB 事务 + 幂等表，提交成功才返回
        consumer.commitSync(Map.of(
            new TopicPartition(r.topic(), r.partition()),
            new OffsetAndMetadata(r.offset() + 1)));
    }
}
```

**性能提醒**：每条 `commitSync` 一次往返，吞吐会掉。生产上按批处理：一批业务提交成功后，提交这批的最大 offset；重复窗口变大，但幂等能兜住。rebalance 时在 `ConsumerRebalanceListener.onPartitionsRevoked` 里同步提交已处理位点，避免重复扩大。

### 11.4.2 Kafka 事务：只覆盖 Kafka 内部

```java
Properties props = new Properties();
props.put(ProducerConfig.BOOTSTRAP_SERVERS_CONFIG, "kafka:9092");
props.put(ProducerConfig.TRANSACTIONAL_ID_CONFIG, "points-tx-1"); // 必须稳定且唯一
props.put(ProducerConfig.ENABLE_IDEMPOTENCE_CONFIG, "true");
KafkaProducer<String, String> producer = new KafkaProducer<>(props);
producer.initTransactions();

while (running) {
    ConsumerRecords<String, String> records = consumer.poll(Duration.ofMillis(500));
    producer.beginTransaction();
    try {
        for (ConsumerRecord<String, String> r : records) {
            String result = transform(r.value());
            producer.send(new ProducerRecord<>("points-calculated", r.key(), result));
        }
        // 把输入位点纳入 Kafka 事务：输出 + 位点原子提交
        producer.sendOffsetsToTransaction(
            currentOffsets(records), consumer.groupMetadata());
        producer.commitTransaction();
    } catch (Exception e) {
        producer.abortTransaction();          // 输出与位点一起回滚
        throw e;
    }
}
```

消费者侧必须配 `isolation.level=read_committed`，否则会读到未提交事务的消息。**注意**：这段代码里没有 MySQL；一旦你在循环里写库，Kafka 事务就管不到它了。

### 11.4.3 端到端幂等模板：一张表 + 一个事务

```sql
-- 消费幂等表：consumer_group + event_id 唯一
CREATE TABLE consume_idempotency (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  consumer_group VARCHAR(128)    NOT NULL COMMENT '消费组/处理器',
  event_id       VARCHAR(128)    NOT NULL COMMENT '消息唯一 ID',
  biz_key        VARCHAR(128)    NULL COMMENT '业务键，便于排查',
  status         ENUM('PROCESSING','DONE','FAILED') NOT NULL DEFAULT 'DONE',
  lease_until    DATETIME(3)     NULL COMMENT '外部副作用处理中的租约',
  result         JSON            NULL COMMENT '可选的返回摘要',
  created_at     DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at     DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                 ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_consumer_event (consumer_group, event_id),
  KEY idx_biz_key (biz_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

```java
// 消费模板：业务写 + 幂等记录在同一个 DB 事务；提交后再提交位点
@Transactional(rollbackFor = Exception.class)
public void handleIdempotent(Event event) {
    try {
        idempotencyMapper.insert(event.consumerGroup(), event.eventId(), event.bizKey());
    } catch (DuplicateKeyException e) {
        return;                                  // 已处理过，直接成功返回
    }
    switch (event.type()) {
        case "user.registered" -> couponService.grant(event.userId());
        case "order.paid"      -> pointsService.add(event.orderNo(), event.amount());
        default -> throw new IllegalArgumentException("unknown event: " + event.type());
    }
    // 业务唯一索引是第二道防线：即使幂等表被绕过，也不会重复
}

// 消费循环：DB 提交成功后，才提交 offset
for (ConsumerRecord<String, String> r : records) {
    handler.handleIdempotent(Event.from(r));
    consumer.commitSync(Map.of(tp(r), new OffsetAndMetadata(r.offset() + 1)));
}
```

**为什么这个模板成立**：

- 幂等插入与业务写在**同一个 DB 事务**：要么都成功，要么都回滚；不会出现「幂等记录写了、业务没写」。
- 重复消息第二次插入触发唯一键冲突，直接返回成功；业务不会执行第二遍。
- 崩溃在 DB 提交后、offset 提交前 → 重投 → 幂等表命中 → 直接返回；offset 最终推进。
- 崩溃在 DB 提交前 → 事务回滚 → 重投 → 重新处理。

**外部副作用怎么办？** 如果业务写是「调第三方发券」而不是 DB 写，幂等表无法和外部调用原子提交。这时用状态机：

```
INSERT idempotency(status=PROCESSING, lease_until=now+30s)
  ├─ 已存在 DONE → 返回
  ├─ 已存在 PROCESSING 且 lease 未过期 → 返回「处理中」，稍后重试
  └─ 已存在 PROCESSING 且 lease 过期 → 抢占，继续
调用第三方（带 idempotency_key = event_id）
UPDATE idempotency SET status=DONE
```

第三方必须支持幂等键；不支持就只能「查询 + 对账 + 人工」，没有别的办法。

### 11.4.4 消费端配置（YAML）

```yaml
spring:
  kafka:
    consumer:
      enable-auto-commit: false          # 必须关；自动提交是丢消息的头号原因
      auto-offset-reset: earliest
      isolation-level: read_committed    # 配合 Kafka 事务，避免读未提交
      max-poll-records: 100              # 一批别太大，重复窗口可控
      session-timeout-ms: 45000
      heartbeat-interval-ms: 3000
    listener:
      ack-mode: manual_immediate         # 业务提交成功后手动 ack
      concurrency: 4
```

> ⚠️ `enable-auto-commit=true` 的本质是「按时间提交，不按处理结果提交」。它不知道你的业务有没有成功，所以它一定会丢。**生产环境把它关掉，是消费可靠性的第一条纪律。**

---

## 11.5 双修对照：Agent 侧多一层「模型结果复用」

| 维度 | 传统系统 | Agent 系统 | 同 or 异 |
| --- | --- | --- | --- |
| 语义本体 | 至少一次 + 消费幂等 | **完全相同**；MQ/Step 都只能至少一次 | 同，可直接复用 |
| 身份 / 唯一键 | `event_id`、`request_id`、`order_no` | `run_id` + `step_id` + `tool_call_id`；`step_id` 含工具名 + 参数 hash | 同构，键多一层 |
| 位点 / 进度 | Kafka offset、RabbitMQ ack | **检查点（checkpoint）**：Run 执行到第几步、工具结果是什么 | 同构，粒度更细 |
| 重复的后果 | 重复扣款、重复发券 | 重复扣款 + **重复调用模型（重复花钱）** + 重复工具副作用 | 后果升级 |
| 失败模式 | 先提交位点丢、先处理重复 | 上述全部 + 模型已返回但结果没落库、流式输出半截、工具已执行但 Step 失败 | 多一类非确定性 |
| 验证方法 | 金额/条数对账 | 金额 + **token/调用次数对账** + 输出质量抽检 | 框架相同，口径扩展 |
| 成本结构 | 重复消费 ≈ 多一次 DB 写 | 重复消费 = 再烧一次 token/工具费 | 异：成本显性 |

**哪些完全相同、可以直接复用**：消费幂等表、`event_id` 唯一索引、先处理后提交、Kafka 手动位点、Outbox、死信/重试队列。**Agent 的 Step 消费和传统消息消费是同一套模板。**

Agent 侧要额外做的三件事：

1. **模型结果缓存**：`(model, prompt_version, args_hash)` 做键，缓存完整响应；重放时先查缓存，命中就不调用模型。这不是「优化」，是「防重复计费」。
2. **Step 幂等键**：`step_id = hash(run_id + step_seq + tool_name + args_hash)`；同一个 Step 重试只允许一次副作用。模型调用本身用「至少一次 + 缓存」，工具调用用「幂等键 + 补偿」。
3. **检查点不是位点**：Kafka offset 只告诉你「消息读到哪」，检查点告诉你「Run 执行到哪、上一步结果是什么」。长任务恢复必须用检查点，不能靠重放整个 Run。

```go
// Agent Step 消费：先查模型缓存，再执行工具；Step 结果与幂等记录同事务
func (h *StepHandler) Handle(ctx context.Context, evt StepEvent) error {
	// 先查 Step 是否已完成，避免重复 Step 再次调用模型
	if done, err := h.idem.IsDone(ctx, evt.ConsumerGroup, evt.StepID); err != nil {
		return err
	} else if done {
		return nil
	}
	if cached, ok := h.llmCache.Get(evt.ModelKey()); ok {
		evt.Result = cached                       // 重放不重复调用模型
	} else {
		resp, err := h.llm.Call(ctx, evt.Prompt)
		if err != nil { return err }
		h.llmCache.Set(evt.ModelKey(), resp, 24*time.Hour)
		evt.Result = resp
	}
	// 并发重试仍可能各调用一次模型：缓存 + token 对账兜底，DB 副作用靠唯一键收敛
	return h.repo.InTx(ctx, func(tx *sql.Tx) error {
		if err := h.idem.Insert(tx, evt.ConsumerGroup, evt.StepID); err != nil {
			if errors.Is(err, ErrDuplicateKey) {
				return nil                        // 已处理，幂等返回
			}
			return err                            // 不能把连接失败当成「已处理」
		}
		if err := h.repo.SaveStepResult(tx, evt); err != nil { return err }
		return h.outbox.Insert(tx, evt.ToCompletedEvent())
	})
}
```

> 💡 Agent 侧的「恰好一次」= **Step 不重复执行 + 模型结果可复用 + 工具副作用幂等 + token 对账**。前三个是技术，最后一个是兜底；少一个，你都会在账单上看到它。

---

## 11.6 会在哪炸：失效模式清单

| 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 重启后一批消息没处理 | 自动提交位点 / 先提交后处理 | 对比 offset 与业务表最大 ID；看 `auto.commit` 配置 | 关自动提交；改先处理后提交 | 配置扫描：`enable-auto-commit` 必须 false |
| 同一用户被加两次积分 | 先处理后提交 + 无幂等 | 查积分流水重复；查 `consume_idempotency` | 补唯一索引，回滚重复 | 消费模板强制幂等表 |
| rebalance 后重复消费 | 分区被两个实例先后处理，位点未提交 | 看 rebalance 日志与重复时间窗 | 在 `onPartitionsRevoked` 提交位点；幂等兜底 | 控制单批时长 < `max.poll.interval.ms` |
| 消费卡在毒消息，整分区不动 | 一条消息反序列化/业务异常，无限重试 | 看消费 lag + 异常栈 | 重试上限 + 死信队列；跳过毒消息 | 死信 + 告警 + 人工重放 |
| Kafka 事务提交了，DB 没提交 | 事务边界搞错：DB 不在 Kafka 事务里 | 对比 output topic 与 DB 记录 | 改 Outbox/幂等；补数据 | 明确「Kafka 事务只覆盖 Kafka」 |
| offset 越界，消费者从 earliest/latest 重置 | 位点过期/ topic 删除重建 | 看 `auto.offset.reset` 与 lag | 从业务表重建位点；补数 | 位点保留期 > 最长停机时间 |
| 僵尸生产者写入重复数据 | 旧 producer 实例未隔离，transactional.id 冲突 | 看 producer epoch / 日志 | 用唯一 transactional.id + fencing | 滚动发布时确保旧实例先退出 |
| 流式输出半截，客户端重试后重复 | 没有消息 ID/断点 | 看客户端重试日志 | 流式响应带 `message_id` + `seq`；客户端去重 | 协议层支持断点续传 |

---

## 11.7 怎么验证：在 T4 和 T5 之间杀进程

**① 崩溃注入**：在「DB 提交后、offset 提交前」`kill -9` 消费者，重启后断言：

- 业务表只有一条记录；
- `consume_idempotency` 只有一条 `DONE`；
- offset 最终推进到正确位置；
- 没有重复积分/重复发券。

**② 重复注入**：把同一 `event_id` 重投 100 次，断言业务副作用只发生一次。

**③ 乱序注入**：同一 key 的 `created` / `paid` 事件倒序投递，断言状态机拒绝非法迁移，最终状态正确。

**④ 对账 SQL**：

```sql
-- 消费侧：消息数 vs 业务生效数（按小时）
SELECT DATE_FORMAT(created_at, '%Y-%m-%d %H:00') AS hour,
       COUNT(*) AS consumed,
       COUNT(DISTINCT biz_key) AS distinct_biz
  FROM consume_idempotency
 WHERE consumer_group='points-consumer'
 GROUP BY hour
HAVING consumed <> distinct_biz;

-- 业务侧：积分流水与订单应付积分是否一致
SELECT o.order_no, o.points_expected, COALESCE(SUM(f.points),0) AS points_actual
  FROM orders o
  LEFT JOIN points_flow f ON f.order_no = o.order_no
 WHERE o.status='PAID' AND o.paid_at >= CURDATE() - INTERVAL 1 DAY
 GROUP BY o.order_no, o.points_expected
HAVING points_actual <> o.points_expected;
```

**⑤ 观测指标**：

| 指标 | 含义 | 告警线 |
| --- | --- | --- |
| `kafka_consumer_lag` | 消费积压 | > 10 万持续 5 分钟 |
| `idempotency_hit_total` | 幂等命中次数 | 突增 10 倍（重投风暴） |
| `offset_commit_latency` | 处理完成到提交位点 | P99 > 1 秒 |
| `consume_failed_total` | 消费失败 | 5 分钟 > 0 |
| `dlq_size` | 死信数量 | > 0 即告警 |
| `duplicate_business_total` | 重复业务副作用 | > 0（说明幂等漏了） |

---

## 11.8 用什么工具

| 需求 | 工具 | 边界 |
| --- | --- | --- |
| 至少一次 + 手动位点 | Kafka Consumer（`enable.auto.commit=false`） | 位点提交在业务提交之后；幂等自己做 |
| Kafka 内部 exactly-once | Kafka 事务 + `read_committed` | 只覆盖 Kafka 记录与位点，不覆盖 DB |
| RocketMQ | 消费重试 + 幂等 | 重试 16 次后进死信；业务幂等自己做 |
| Pulsar | 订阅类型 + ack | 同上；事务 API 也只在 Pulsar 内部 |
| 流处理 | Flink checkpoint + 两阶段提交 | 端到端 exactly-once 需要 sink 支持事务/幂等 |
| 消费幂等 | 自研幂等表 + 唯一索引 | 最可靠；注意外部副作用无法与 DB 同事务 |

> ⚠️ 框架能力的边界：Kafka 的 `enable.idempotence=true` 只保证**单个 producer 会话、单个分区**不重复；它不保证跨会话、跨系统。**别把 producer 幂等当成端到端 exactly-once。**

---

## 11.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 三种语义 | 至多一次会丢、至少一次会重、恰好一次靠端到端 |
| ack 丢失 | 消费端 ack 丢 → 重投；生产端 ack 丢 → 重试；两者都产生重复 |
| 位点顺序 | 先提交位点再处理 = 丢；先处理后提交 = 重；只能选后者 + 幂等 |
| 死亡窗口 | DB 提交（T4）与 offset 提交（T5）之间没有原子性 |
| Kafka 事务 | 只覆盖 Kafka 内部（输出 + 位点），不覆盖你的 DB/Redis/第三方 |
| 端到端模板 | 幂等表 + 业务写同事务 + 提交后提交位点 |
| 外部副作用 | 幂等表 + 状态机 + 第三方幂等键 + 对账；没有银弹 |
| Agent 侧 | Step 幂等 + 模型结果缓存 + 检查点 + token 对账 |
| 不做会怎样 | 先提交 → 用户丢券；先处理无幂等 → 重复积分；两者都是钱 |
| 一句话 | **恰好一次 = 生产端不丢 + 消费端幂等，MQ 只负责「至少一次」。** |

## 11.10 自检题

1. **Kafka 开了 `enable.auto.commit=true`，业务处理在异步线程池里，为什么一定会丢消息？**
   <details><summary>答案要点</summary>
   自动提交按时间推进位点，不知道异步线程是否处理成功。Pod 被杀时位点已提交、线程池任务丢失，重启后从新位点开始，未处理的消息永远不会再投。修法：关自动提交、同步处理、业务提交后再提交位点、幂等兜底。
   </details>

2. **Kafka 事务能不能保证「消费消息 + 写 MySQL + 发下一条消息」恰好一次？**
   <details><summary>答案要点</summary>
   不能。Kafka 事务只把「输出到 Kafka 的记录 + 输入位点」原子提交，MySQL 写入不在事务内。DB 提交成功、Kafka 提交失败 → 重复；Kafka 提交成功、DB 提交失败 → 丢失。跨 DB 要用 Outbox/幂等表，Kafka 事务只解决 Kafka 内部。
   </details>

3. **先处理后提交位点，重复不可避免。为什么系统仍然能对用户表现为「恰好一次」？**
   <details><summary>答案要点</summary>
   消费端用 `consumer_group + event_id` 唯一索引（或业务唯一键/状态机条件更新）做幂等，重复消息第二次直接返回成功，不产生第二次副作用；再用对账发现漏处理。**消息可以重复，业务效果只发生一次。**
   </details>

## 11.11 延伸阅读

- Apache Kafka 文档：*Exactly-once semantics*、`sendOffsetsToTransaction`、`isolation.level`
- Spring for Apache Kafka：`enable.auto.commit`、`AckMode.MANUAL_IMMEDIATE`、`ConsumerRebalanceListener`
- Apache RocketMQ 文档：消费重试与死信队列
- 上一章：[10. 消息表（Outbox）与事务消息](10-outbox-and-transactional-message.md)
- 下一章：[12. 跨服务一致性实战：订单-库存-支付的编排](12-cross-service-consistency.md)
- 交叉引用：Outbox 见第 10 章；重复消费与位点见第 24、26 章；对账见第 14 章
