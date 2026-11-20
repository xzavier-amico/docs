# 10. 消息表（Outbox）与事务消息：为什么还要它，和 MQ 冲突吗

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · 事务与一致性 ｜ **编号**：10 / 52
> **本章定位**：Outbox 只做一件事——**把「发消息」变成「写本地表」**，从而让业务数据和事件在同一个本地事务里原子提交。这一章把表结构、三种投递器、重复与顺序、清理归档一次讲透，并正面回答「Outbox 和 MQ 到底冲不冲突」。
> **读法**：§10.2 先纠正「Outbox = MQ」的误解；§10.4 的 DDL 可直接建表；三种投递器按你的运维能力选一种，不要三种都上。

---

## 10.1 引子：两条消息，两个方向的脏数据

**事故一：支付成功，库存没扣（2025-04-09）**

订单服务在本地事务提交后调用 `rocketMQTemplate.send()` 发「订单已支付」事件。当天 14:20，MQ 集群一次主从切换，发送超时 3 秒；代码 `try/catch` 后只打了 `log.error`，没有重试。

- 2,300 笔订单支付成功，库存服务一条消息都没收到。
- 超卖 2,300 件，其中 1,100 件无法发货，客诉 640 起，赔付 **86 万元**。
- 18 分钟后才从「库存对账」发现，期间运营还在继续卖。

**事故二：消息发了，订单回滚了（2025-07-22）**

另一个团队把 `send()` 放在了事务提交之前：先发消息，再提交订单。当天一次唯一索引冲突导致订单回滚，但消息已经发出。

- 库存服务收到 1,100 条「订单已创建」，扣了 1,100 件库存。
- 这些订单在订单库里根本不存在，库存成了「幽灵扣减」。
- 6 小时后才通过库存流水与订单表对账发现，人工回滚库存 1,100 件。

> 🩸 两个事故是同一个根因的两面：**「写库」和「发消息」是两个独立的写操作，中间没有原子性。** Outbox 不是让消息变得更快，而是让这两个写操作变成**一个**。

---

## 10.2 到底是什么：Outbox 是「本地事务 + 可靠投递」，不是 MQ

**定义**：Outbox（本地消息表）= 在业务数据库里建一张事件表；业务数据和事件记录在**同一个本地事务**里提交；独立的投递器（relay）异步读取事件表，把事件发到 MQ 或下游；发送成功后标记 `SENT`，失败则重试。

它的本质只有一句话：

> **把「发消息」这个不可控的外部动作，变成「写本地表」这个可控的数据库动作。**

由此得到三个直接推论：

1. **原子性来自本地事务**，不是来自 MQ，也不是来自分布式事务框架。
2. **投递语义是至少一次**：投递器可能「发成功但标记失败」，重启后会再发一次。
3. **消费端必须幂等**：重复不是 bug，是协议的一部分。

先把五个常见误解钉死：

| 你以为的 Outbox | 真实情况 | 不做会怎样 |
| --- | --- | --- |
| 「Outbox 是一个 MQ」 | 它是一张表 + 一个投递器；没有 fan-out、保留、重放、消费组 | 把它当 MQ 用，消费者一多就要自己实现订阅管理 |
| 「Outbox 能保证恰好一次」 | 只能保证「不丢」；重复一定会发生 | 消费端不去重，重复扣款/重复加积分 |
| 「写进 Outbox 就万事大吉」 | 投递器挂了、表爆了、事件格式变了，都会让链路停住 | 没有积压告警，事件在表里躺 40 分钟没人知道 |
| 「Outbox 替代 MQ」 | 它替代的是「在业务库里直接发 MQ」这个危险动作 | 误删 MQ，失去解耦、削峰、跨团队消费能力 |
| 「Outbox 只适合小流量」 | 高流量也能用，但要按 aggregate_id 分区、批量投递、归档 | 单表 2,000 万行 + 单线程投递，延迟小时级 |

**Outbox 与事务消息、直接发 MQ 的区别**：

| 方案 | 原子性靠什么 | 消息何时可见 | 典型失败 | 适用 |
| --- | --- | --- | --- | --- |
| 事务内/提交后直接 `send()` | 没有原子性 | 提交前后不一致 | 提交前发：发送成功、提交失败→幽灵；提交后发：发送失败→丢 | 允许丢的非关键通知 |
| RocketMQ 事务消息 | 半消息 + 本地事务 + 回查 | 本地事务提交后 | 回查失败、半消息堆积 | 已用 RocketMQ、单 MQ |
| Outbox + relay | 业务库本地事务 | relay 投递后 | relay 挂、重复、积压 | 任意 DB、任意 MQ、跨语言 |

> 💡 判断你该不该用 Outbox，只问一句：**「这条事件丢了，会不会导致账不平/库存错/用户投诉？」** 会，就必须用；不会，直接发 MQ 也能接受。

---

## 10.3 结构图：写路径、投递路径、消费路径

```
写路径（一个本地事务）：
┌─────────────┐    BEGIN
│ OrderService│──────────────────────────────┐
└─────────────┘                              ▼
                                  ┌─────────────────────┐
                                  │ MySQL: order_db      │
                                  │  INSERT orders       │
                                  │  INSERT outbox_event │  ← 同一事务
                                  └─────────────────────┘
                                             │ COMMIT
                                             ▼
投递路径（至少一次）：
┌─────────────┐   SELECT ... FOR UPDATE SKIP LOCKED   ┌──────────────┐
│ Relay / CDC │◄──────────────────────────────────────│ outbox_event │
│ (多实例)     │   status=NEW / next_retry_at <= now   │  NEW → SENT  │
└──────┬──────┘                                       └──────────────┘
       │ publish(key=aggregate_id)
       ▼
┌─────────────┐      ┌──────────────────┐
│     MQ      │─────►│ InventoryConsumer │ 幂等：event_id 唯一
└─────────────┘      └──────────────────┘
       │
       └──► 其它消费组（积分、搜索、数仓）各自独立消费

状态机：
  NEW ──发送成功──► SENT
   │                  │
   └──发送失败──► FAILED ──退避重试──► NEW
                    │
                    └──超过上限──► 人工队列 + 告警（不删除）
```

**分区顺序不等于业务版本顺序**：相同 `aggregate_id`（订单号、账户号、`run_id`）在路由稳定时进入同一分区，Kafka 只保留写入分区的先后。如果需要按 `aggregate_version` 应用事件，要让投递端避免跳号，或让消费端可靠暂存后序事件并补齐缺口。丢弃旧版本不是补齐策略。

---

## 10.4 怎么实现：一张表 + 三种投递器

### 10.4.1 完整 DDL：先把表设计对

```sql
CREATE TABLE outbox_event (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  event_id          VARCHAR(128)    NOT NULL COMMENT '全局唯一，消费者幂等键（可含业务后缀）',
  aggregate_type    VARCHAR(64)     NOT NULL COMMENT '聚合类型，如 order/payment/run',
  aggregate_id      VARCHAR(64)     NOT NULL COMMENT '聚合根 ID，分区键',
  aggregate_version BIGINT          NOT NULL DEFAULT 0 COMMENT '同一聚合内单调递增',
  event_type        VARCHAR(128)    NOT NULL COMMENT '如 order.created',
  payload           JSON            NOT NULL COMMENT '事件体，含 schema_version',
  headers           JSON            NULL COMMENT 'trace_id、tenant_id 等',
  status            ENUM('NEW','SENT','FAILED') NOT NULL DEFAULT 'NEW',
  retry_count       INT             NOT NULL DEFAULT 0,
  next_retry_at     DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  last_error        VARCHAR(512)    NULL,
  created_at        DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  sent_at           DATETIME(3)     NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_event_id (event_id),
  KEY idx_poll (status, next_retry_at, id),
  KEY idx_aggregate (aggregate_id, aggregate_version),
  KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='本地消息表';
```

**字段设计要点**：

- `event_id` 用 UUID/ULID，**在业务事务内生成**；消费者拿它做唯一索引，重复投递直接冲突返回。
- `aggregate_id` 是分区键，保证同一订单的事件有序；`aggregate_version` 让消费者能丢弃旧事件。
- `next_retry_at` 支持指数退避，避免失败事件每秒重试打爆 MQ。
- `last_error` 只存摘要（512 字符），完整错误进日志/trace，别把堆栈塞进表。
- 索引 `(status, next_retry_at, id)` 是轮询投递器的生命线；没有它，每次轮询全表扫。

**业务写入（Java）**：

```java
@Transactional(rollbackFor = Exception.class)
public void createOrder(OrderCmd cmd) {
    Order order = Order.from(cmd);
    orderMapper.insert(order);                       // 业务写
    String eventId = UUID.randomUUID().toString();
    OutboxEvent event = OutboxEvent.builder()
        .eventId(eventId)
        .aggregateType("order")
        .aggregateId(order.getOrderNo())
        .aggregateVersion(1L)                        // 首次事件；同一聚合后续事件 +1
        .eventType("order.created")
        .payload(Json.toJson(Map.of(
            "schema_version", 2,
            "order_no", order.getOrderNo(),
            "sku_id", order.getSkuId(),
            "qty", order.getQty(),
            "amount", order.getAmount())))
        .build();
    outboxMapper.insert(event);                       // 同一事务
}
```

> ⚠️ payload 必须在事务内、用**同一份内存状态**构造，不能提交后再查库拼装——那会读到别人改过的数据，事件与业务事实不一致。

### 10.4.2 投递器一：轮询 + `FOR UPDATE SKIP LOCKED`

适合：已有 MySQL、流量中等（<5,000 事件/秒）、不想引入 CDC 运维。

```java
@Scheduled(fixedDelay = 200)
@Transactional(rollbackFor = Exception.class)
public void relayOnce() {
    // MySQL 8.0+：跳过已被其它 relay 实例锁住的行，天然支持多实例并行
    List<OutboxEvent> batch = outboxMapper.lockBatch(100);
    for (OutboxEvent e : batch) {
        try {
            mqProducer.send(
                "outbox." + e.getAggregateType(),
                e.getAggregateId(),                  // 分区键 = 聚合根
                e.getEventId(),                      // 消息 key，便于排查
                e.getPayload());
            outboxMapper.markSent(e.getId());        // 同一事务
        } catch (Exception ex) {
            outboxMapper.markRetry(e.getId(), ex.getMessage());
        }
    }
}
```

```sql
-- 只适用于允许到达乱序的投递：被锁住或正在退避的前序版本可能被跳过
SELECT *
  FROM outbox_event
 WHERE status IN ('NEW','FAILED')
   AND next_retry_at <= NOW(3)
   AND retry_count < 10
 ORDER BY next_retry_at, id
 LIMIT 100
 FOR UPDATE SKIP LOCKED;

-- 发送成功
UPDATE outbox_event
   SET status='SENT', sent_at=NOW(3), last_error=NULL
 WHERE id = ?;

-- 发送失败：指数退避，最多 10 次后进人工队列
-- MySQL 的 SET 从左到右求值：next_retry_at 必须先算，否则会用自增后的 retry_count
UPDATE outbox_event
   SET status='FAILED',
       next_retry_at = DATE_ADD(NOW(3), INTERVAL LEAST(POW(2, retry_count) * 5, 300) SECOND),
       retry_count = retry_count + 1,
       last_error = LEFT(?, 512)
 WHERE id = ? AND retry_count < 10;

-- retry_count >= 10 的行不再被 relay 选中，由人工队列按
-- status='FAILED' AND retry_count >= 10 捞取处理。
```

**关键权衡**：上面的写法在「锁行 → 发 MQ → 更新」期间持有数据库行锁和事务。MQ 慢时，锁时间 = MQ 延迟。更稳的做法是「**认领**」：先用短事务把行改成 `SENDING` 并写 `worker_id`，再在事务外发送，最后标记 `SENT`；崩溃后由巡检把超时的 `SENDING` 改回 `NEW`。多一张状态，少一次长事务。

### 10.4.3 投递器二：CDC / Debezium（日志驱动）

适合：事件量大、要求低延迟（10~100ms）、已有 Kafka Connect 运维能力。

```yaml
# Debezium 2.x MySQL Outbox Event Router；表名/字段名按你的 DDL 调整
name: outbox-connector
connector.class: io.debezium.connector.mysql.MySqlConnector
database.hostname: mysql-order
database.port: 3306
database.user: debezium
database.password: ${DEBEZIUM_PASSWORD}
database.server.id: 184054
topic.prefix: prod
database.include.list: order_db
table.include.list: order_db.outbox_event
schema.history.internal.kafka.bootstrap.servers: kafka:9092
schema.history.internal.kafka.topic: schema-changes.order_db
transforms: outbox
transforms.outbox.type: io.debezium.transforms.outbox.EventRouter
transforms.outbox.route.by.field: aggregate_type
transforms.outbox.route.topic.replacement: outbox.${routingKey}
transforms.outbox.table.field.event.id: event_id
transforms.outbox.table.field.event.key: aggregate_id
transforms.outbox.table.field.event.type: event_type
transforms.outbox.table.field.event.payload: payload
transforms.outbox.table.fields.additional.placement: event_type:header:eventType
```

**原理**：Debezium 读 MySQL binlog，捕获 `outbox_event` 的 INSERT，用 EventRouter SMT 转成 Kafka 消息；`aggregate_id` 作为 Kafka key，保证同一聚合进同一分区。

**注意**：

- 需要 `binlog_format=ROW`、`binlog_row_image=FULL`、账号有 `REPLICATION SLAVE`/`REPLICATION CLIENT` 权限。
- CDC 本身也是至少一次：Connector 重启可能重复投递最后一批；消费者仍要幂等。
- Outbox 表不能「写完就删」：CDC 需要读到 INSERT；清理必须晚于 CDC 位点，建议保留 24~72 小时。
- 表结构变更（加字段）要先兼容旧 schema，再切消费者，避免反序列化失败。

### 10.4.4 投递器三：RocketMQ 事务消息

适合：已经用 RocketMQ、希望由 MQ 承担「半消息 + 回查」、不想维护 relay。

```java
// Spring RocketMQ（rocketmq-spring-boot-starter 2.x）：本地事务 + 半消息 + Broker 回查
@RocketMQTransactionListener
public class OrderTxListener implements RocketMQLocalTransactionListener {

    @Override
    public RocketMQLocalTransactionState executeLocalTransaction(Message msg, Object arg) {
        try {
            orderService.createInLocalTx((OrderCmd) arg);   // 与半消息绑定的本地事务
            return RocketMQLocalTransactionState.COMMIT;
        } catch (Exception e) {
            return RocketMQLocalTransactionState.ROLLBACK;
        }
    }

    @Override
    public RocketMQLocalTransactionState checkLocalTransaction(Message msg) {
        String orderNo = (String) msg.getHeaders().get("orderNo");
        return orderService.exists(orderNo)
            ? RocketMQLocalTransactionState.COMMIT
            : RocketMQLocalTransactionState.ROLLBACK;
    }
}

// 发送：半消息先落 Broker，本地事务结果决定 commit/rollback
rocketMQTemplate.sendMessageInTransaction(
    "order-tx-topic",
    MessageBuilder.withPayload(payload).setHeader("orderNo", orderNo).build(),
    cmd);
```

**事务消息的边界**：

- 只覆盖「本地事务 + 发消息」这一件事，不覆盖下游消费；下游仍要幂等。
- 回查依赖业务主键可查；如果本地事务表没有唯一键，回查会误判。
- 半消息堆积、回查频率、Broker 版本都要监控；换 MQ 时这套逻辑要重写。

### 10.4.5 三种投递器怎么选

| 维度 | 轮询 + SKIP LOCKED | CDC / Debezium | RocketMQ 事务消息 |
| --- | --- | --- | --- |
| 投递延迟 | 200ms~2s（可调） | 10~100ms | 10~100ms |
| 对 DB 压力 | 周期查询 + 行锁 | 读 binlog，几乎不碰业务表 | 半消息 + 回查 |
| 运维复杂度 | 低（一个定时任务） | 高（Kafka Connect、binlog、位点） | 中（MQ 集群 + 回查） |
| 顺序边界 | `ORDER BY id` 只影响选批；跳锁和退避都可使后序先发 | 同 key 只保分区追加顺序；另验证变更的源顺序和投递方式 | 同队列不自动修正已经乱序的发送 |
| 重复投递 | 有（发成功标记失败） | 有（Connector 重启） | 有（回查/重投） |
| 适用 | 中小流量、已有 MySQL | 大流量、低延迟、已有 Kafka | 已用 RocketMQ、单 MQ |
| 主要坑 | 长事务锁行、表膨胀 | 权限/位点/schema 变更 | 回查不准、半消息堆积 |

> 💡 选型建议：**先用轮询把链路跑通，再根据积压和延迟决定要不要上 CDC。** 一上来就 Debezium + Kafka Connect，出问题时你会在「binlog 权限、Connector 位点、schema 兼容」三件事里同时迷路。

### 10.4.6 Outbox 与 MQ 是否冲突？不冲突

这是本章必须正面回答的问题：

**Outbox 不替代 MQ，它替代的是「在业务库事务里直接发 MQ」这个危险动作。**

| 关注点 | Outbox 负责 | MQ 负责 |
| --- | --- | --- |
| 业务数据与事件的原子性 | ✅ 同一本地事务 | ❌ |
| 事件 fan-out 到多个消费组 | ❌ | ✅ |
| 削峰、缓冲、重放 | ❌ | ✅ |
| 跨团队/跨语言解耦 | ❌ | ✅ |
| 至少一次投递 | ✅（到 MQ） | ✅（到消费者） |
| 消费端幂等 | ❌（必须业务自己做） | ❌ |

所以正确的关系是：**DB 事务 → Outbox（可靠桥）→ MQ（分发/缓冲）→ 消费者（幂等）**。没有 MQ 也可以把 Outbox 投递到 HTTP/webhook，但你会失去 fan-out、保留和重放；有 MQ 但没有 Outbox，你仍然会在「提交成功、发送失败」时丢消息。

**那 RocketMQ 事务消息是不是就不需要 Outbox 了？** 不是二选一：事务消息是「MQ 帮你协调半消息」，Outbox 是「DB 帮你协调事件表」。已经用 RocketMQ 且只用一个 MQ，事务消息够用；多 MQ、跨语言、需要事件回放/审计，Outbox 更通用。**两者都不是 MQ 的替代品。**

### 10.4.7 重复与顺序：协议的一部分

**重复的四个来源**：

1. relay 发送成功，但 `markSent` 事务提交失败 → 下次重发。
2. MQ 投递成功，消费者处理成功，但 ack 丢失 → 重投。
3. 消费者 rebalance / 位点回退 → 重复消费。
4. CDC Connector 重启 → 重复最后一批。

**消费端幂等的三种落点**：

```sql
-- 落点一：事件去重表（通用）
CREATE TABLE consumed_event (
  event_id     VARCHAR(128) NOT NULL,
  consumer     VARCHAR(64) NOT NULL COMMENT '消费组/处理器名',
  consumed_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (event_id, consumer)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 落点二：业务唯一索引（最可靠）
ALTER TABLE inventory_flow
  ADD UNIQUE KEY uk_order_sku (order_no, sku_id, flow_type);

-- 落点三：状态机条件更新（天然幂等）
UPDATE orders
   SET status='PAID', paid_at=NOW(3)
 WHERE order_no='SO202506180001' AND status='CREATED';
-- 影响 0 行还可能是订单不存在、前序未应用或状态冲突；核对后才能当成重复
```

**顺序**：上述轮询器不保证业务版本顺序。即使只有一个 relay，v1 正在退避时也可能先发送可领取的 v2。若投递端负责保序，应对每个聚合串行认领，并让后序版本等待前序发送确认（失败或人工挂起时仍保留这个屏障）；不同聚合可以并行。若消费端负责保序，且版本对该订阅确实连续，可在同一事务中记录应用效果与 `last_version`，已经应用的版本去重，高于期望版本的事件先持久化暂存，再继续消费或从可信来源补齐缺失版本，最后按序应用。不要阻塞分区等一个可能排在当前消息后面的前序事件。长期缺口告警并转人工，不能静默跳过。见 [MySQL 锁定读](https://dev.mysql.com/doc/refman/8.0/en/innodb-locking-reads.html#innodb-locking-reads-nowait-skip-locked)和 [Kafka 分区顺序](https://kafka.apache.org/43/getting-started/introduction/#main-concepts-and-terminology)。

### 10.4.8 清理与归档：不清理的 Outbox 一定会炸

**容量账**：5,000 事件/秒 × 平均 400 字节 × 86,400 秒 ≈ **172 GB/天**；含索引约 1.5 倍 ≈ **258 GB/天**。保留 7 天就是 **约 1.8 TB**；不归档，三个月后这张表会拖垮整个库。

| 策略 | 做法 | 注意 |
| --- | --- | --- |
| 热表保留 | `SENT` 保留 24~72 小时（CDC 需覆盖位点延迟） | `NEW/FAILED` 永不删除 |
| 批量删除 | 每次 1,000 行，循环 + sleep 50ms | 避免长事务、锁表、binlog 膨胀 |
| 归档 | `INSERT INTO outbox_archive SELECT ...` 后删除 | 归档表按天分区/冷存储 |
| 分区（可选） | 按 `created_at` RANGE 分区，直接 DROP PARTITION | 唯一键需包含分区键，DDL 要提前设计 |

```sql
-- 归档 + 删除：同一事务内先锁定同一批 id，避免归档与删除之间状态变化
START TRANSACTION;

CREATE TEMPORARY TABLE IF NOT EXISTS tmp_outbox_ids (
  id BIGINT UNSIGNED NOT NULL PRIMARY KEY
);
DELETE FROM tmp_outbox_ids;

INSERT INTO tmp_outbox_ids
SELECT id FROM outbox_event
 WHERE status='SENT' AND created_at < NOW() - INTERVAL 7 DAY
 ORDER BY id LIMIT 1000
 FOR UPDATE;

INSERT INTO outbox_archive
SELECT o.* FROM outbox_event o JOIN tmp_outbox_ids t ON t.id = o.id;

DELETE o FROM outbox_event o JOIN tmp_outbox_ids t ON t.id = o.id;

COMMIT;
```

```yaml
# Kubernetes CronJob：每天凌晨 3 点清理，失败告警
apiVersion: batch/v1
kind: CronJob
metadata:
  name: outbox-cleaner
spec:
  schedule: "0 3 * * *"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: cleaner
              image: registry.example.com/outbox-cleaner:1.2.0
              args: ["--retention=168h", "--batch=1000", "--sleep=50ms"]
              env:
                - name: MYSQL_DSN
                  valueFrom: { secretKeyRef: { name: order-db, key: dsn } }
```

---

## 10.5 双修对照：Outbox 是两侧完全相同的少数派

| 维度 | 传统系统 | Agent 系统 | 同 or 异 |
| --- | --- | --- | --- |
| 机制本体 | 业务表 + outbox 表同事务；relay 投递 | **完全相同**：Step 结果 + step 事件同事务 | **同，可直接复用** |
| 事件类型 | `order.created`、`payment.succeeded` | `step.completed`、`tool.result`、`run.failed` | 结构同构，类型不同 |
| 聚合根 / 分区键 | `order_no`、`account_no` | `run_id`（同一 Run 内有序）；`step_id` 做幂等 | 同构，键名变化 |
| 失败模式 | relay 挂、重复、乱序、表膨胀 | 上述全部 + 事件体大（含 prompt/结果）、事件量高 | 多两类：体积与频率 |
| 恢复方式 | 重投 + 消费幂等 | 重投 + Step 幂等 + **检查点**（Outbox 不是检查点） | 同 + 检查点 |
| 验证方法 | 事件积压、消费对账 | 同 + **token/调用次数对账** | 框架相同，口径扩展 |
| 成本结构 | 一次 insert + 存储 | 同 + 事件体存储成本更高 | 异：体积 |

**哪些完全相同、可以直接复用**：DDL、`event_id` 幂等、`aggregate_id` 分区、`SKIP LOCKED` 投递、CDC 配置、清理归档、积压告警。**这套东西一行都不用为 Agent 重写。**

Agent 侧只需要换三样：

1. **聚合根换成 `run_id`**：同一 Run 的 `step.completed` 必须有序；`step_id` 作为 `event_id` 的一部分，保证重放不重复计费。
2. **payload 带模型/提示词版本**：`model`、`prompt_version`、`tool_name`、`args_hash`、`tokens_in/out`、`cost`。没有这些，重放时无法判断「同输入」是不是真的同输入。
3. **消费者多两个**：计费（按 token/调用次数）和评测（质量/成功率）；计费消费者必须和 Step 幂等共用同一个 `step_id`，否则重投会重复扣费。

```go
// Agent 侧：Step 结果 + 事件同事务；事件体带 token 与成本，供计费对账
func (r *StepRepo) Complete(ctx context.Context, step StepResult) error {
	return r.InTx(ctx, func(tx *sql.Tx) error {
		if err := r.saveResult(tx, step); err != nil { return err }
		return r.outbox.Insert(tx, OutboxEvent{
			EventID:        step.StepID + ":completed",
			AggregateType:  "run",
			AggregateID:    step.RunID,
			AggregateVersion: step.Seq,
			EventType:      "step.completed",
			Payload: map[string]any{
				"step_id": step.StepID, "tool": step.Tool,
				"tokens_in": step.TokensIn, "tokens_out": step.TokensOut,
				"cost_usd": step.CostUSD, "model": step.Model,
				"prompt_version": step.PromptVersion,
			},
		})
	})
}
```

> ⚠️ **Outbox 不是检查点。** Outbox 保证「事件能送到」；检查点保证「Run 能从第 N 步继续」。长任务两者都要：检查点存状态，Outbox 存已发生的事实。把 Outbox 当检查点，会在「事件已发、状态没存」时无法恢复。

---

## 10.6 会在哪炸：失效模式清单

| 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 下游延迟 40 分钟 | relay 单实例 + 无索引 + 事件量涨 10 倍 | `SELECT COUNT(*), MIN(created_at) FROM outbox_event WHERE status='NEW'` | 加实例 + `SKIP LOCKED` + 补索引 | 监控最老事件年龄，>5 分钟告警 |
| 同一订单被扣两次库存 | relay 重发 + 消费端无幂等 | 查 `consumed_event` / 库存流水重复 | 补唯一索引，回滚重复流水 | 消费端强制 `event_id` 幂等 |
| 同一订单的事件乱序 | 多分区/多 relay 并发投递 | 查 `aggregate_version` 是否跳号 | 按 `aggregate_id` 分区；消费者拒绝旧版本 | 分区键固定为聚合根 |
| Outbox 表 2,000 万行，查询变慢 | 只写不删 | `SHOW TABLE STATUS`；看 `idx_created` 命中 | 归档 + 批量删除 | 上线即配 TTL 清理任务 |
| 事件体 2MB，MQ 拒收 | payload 塞了整页 HTML/大模型输出 | 看 MQ 错误码/消息大小 | 事件只放 ID + 摘要，大对象进对象存储 | schema 评审限制 payload ≤ 64KB |
| 消费者反序列化失败，整批卡住 | 事件 schema 升级不兼容 | 看消费端异常栈 + 死信 | 加 `schema_version`，先兼容后切换 | 事件 schema 向后兼容规则 |
| CDC 重启后重复投递 | Connector 位点回退 | 对比 Kafka offset 与 binlog 位点 | 消费端幂等 | 监控重复率，保留 72h 事件 |
| 清理任务把未投递事件删了 | 只按时间删，没看 status | 查 `status='NEW'` 是否消失 | 恢复备份 + 补发 | 删除条件强制 `status='SENT'` |

---

## 10.7 怎么验证：杀 relay、灌重复、看积压

**① 杀 relay**：投递到一半 `kill -9` relay，重启后确认事件继续投递，且消费端只生效一次。

```bash
kubectl exec -it deploy/outbox-relay -- kill -9 1
# 观察 30 秒：outbox_new_events 应下降；consumed_event 不应出现重复业务副作用
```

**② 灌重复**：把同一 `event_id` 人为重投 100 次，断言库存只扣一次、积分只加一次。

**③ 顺序测试**：同一 `aggregate_id` 的 v1 被锁或进入退避，故意让 v2 先可用。投递端保序时断言不会先发布 v2；消费端保序时断言 v2 先落暂存，v1 到达后按序各应用一次。大样本还应验证每个版本都按序应用，不只看最终 `last_version=1000`。

**④ 积压与延迟 SQL**：

```sql
-- 按状态看积压
SELECT status, COUNT(*) AS cnt, MIN(created_at) AS oldest
  FROM outbox_event
 GROUP BY status;

-- 最老未投递事件年龄（告警核心指标）
SELECT TIMESTAMPDIFF(SECOND, MIN(created_at), NOW(3)) AS oldest_age_sec
  FROM outbox_event
 WHERE status IN ('NEW','FAILED');

-- 重试次数 Top 10，找毒事件
SELECT id, event_type, retry_count, last_error
  FROM outbox_event
 WHERE status='FAILED'
 ORDER BY retry_count DESC
 LIMIT 10;
```

**⑤ 观测指标**：

| 指标 | 含义 | 告警线 |
| --- | --- | --- |
| `outbox_new_events` | 待投递数量 | > 10,000 持续 2 分钟 |
| `outbox_oldest_age_seconds` | 最老事件年龄 | > 300 秒 |
| `outbox_retry_total` | 重试次数 | 5 分钟增长 > 100 |
| `outbox_publish_latency` | 写入到投递的延迟 | P99 > 2 秒 |
| `consumer_duplicate_total` | 重复消费次数 | 突增 10 倍（可能重投风暴） |

---

## 10.8 用什么工具

| 需求 | 工具 | 边界 |
| --- | --- | --- |
| 轮询投递 | 自研 `@Scheduled` + `SKIP LOCKED` | 简单可控；注意长事务与表膨胀 |
| CDC 投递 | Debezium + Kafka Connect | 低延迟；需要 binlog 权限与 Connect 运维 |
| 事务消息 | RocketMQ 事务消息 | 已用 RocketMQ；回查逻辑必须可靠 |
| 事件路由 | Debezium Outbox Event Router | 按 `aggregate_type` 路由 topic，按 `aggregate_id` 分区 |
| 事件存储/回放 | Kafka | Outbox 负责「进 Kafka」，Kafka 负责「保留与重放」 |
| 编排/补偿 | Temporal / Seata Saga | Outbox 只管投递，不管流程状态 |

> ⚠️ 自研边界：**Outbox 表 + 投递器值得自研**（逻辑简单、可控）；**CDC 的 binlog 解析不要自研**（Debezium 已经踩完坑）；**MQ 不要自研**。把精力花在「事件 schema、幂等、对账」上，那才是会出错的地方。

---

## 10.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 本质 | 把「发消息」变成「写本地表」，与业务同事务 |
| 原子性 | 来自本地事务，不来自 MQ，也不来自分布式事务框架 |
| 语义 | 至少一次；重复是协议的一部分，消费端必须幂等 |
| 表结构 | `event_id` 唯一、`aggregate_id` 分区、`aggregate_version` 防旧、`status/retry/next_retry_at` 可运维 |
| 投递器 | 轮询（简单）、CDC（低延迟）、事务消息（RocketMQ）三选一 |
| 与 MQ 关系 | **不冲突**：Outbox 替代「事务里直接发 MQ」，不替代 MQ 的 fan-out/缓冲/重放 |
| 顺序 | 同分区按追加顺序；业务版本靠投递端保序或消费端暂存、补齐与去重 |
| 清理 | 只删 `SENT` 且超过保留期；`NEW/FAILED` 永不删；批量 + 归档 |
| Agent 侧 | 完全相同；`run_id` 做聚合根，payload 带 token/成本/模型版本；Outbox ≠ 检查点 |
| 不做会怎样 | 提交成功消息丢 → 下游永远不知道；表不清理 → 三个月后拖垮库 |

## 10.10 自检题

1. **Outbox 能不能保证「恰好一次」？如果不能，为什么还够用？**
   <details><summary>答案要点</summary>
   不能。relay 可能「发送成功但标记失败」，重启后重发；MQ 也可能重投。它保证的是「不丢 + 至少一次」。够用的前提是消费端用 `event_id`/业务唯一键做幂等，把重复变成无副作用；再用对账发现漏处理。**恰好一次 = 生产端不丢 + 消费端幂等。**
   </details>

2. **Outbox 和 MQ 冲突吗？如果公司已经有 Kafka，还需要 Outbox 吗？**
   <details><summary>答案要点</summary>
   不冲突。Outbox 解决的是「业务库提交与发消息之间的原子性」，Kafka 解决的是「分发、缓冲、保留、重放」。有 Kafka 仍然会在「提交成功、send 失败」时丢消息，所以仍需要 Outbox 或事务消息；Outbox 把事件可靠地送进 Kafka，Kafka 再 fan-out。
   </details>

3. **同一订单的两条事件被并发投递，消费者先收到 `order.paid` 再收到 `order.created`，会出什么问题？怎么防？**
   <details><summary>答案要点</summary>
   可能把未创建的订单标记为已支付，或覆盖状态。同 key 同分区不修复先发的 v2；需要按 §10.4.7 防止跳号投递，或暂存并补齐 v1，确认前序状态后再应用 v2。条件更新是兜底，更新零行不自行证明事件已处理成功。
   </details>

## 10.11 延伸阅读

- Debezium 官方文档：*Outbox Event Router* SMT（`io.debezium.transforms.outbox.EventRouter`，以 2.x 为准）
- Apache RocketMQ 文档：事务消息（半消息、回查、`RocketMQLocalTransactionListener`）
- MySQL 8.0 Reference Manual：`SELECT ... FOR UPDATE SKIP LOCKED`、`binlog_row_image`
- 上一章：[9. 事务决策树：什么场景用什么](9-transaction-decision-tree.md)
- 下一章：[11. Exactly-once 的真相：只有端到端幂等](11-exactly-once-truth.md)
- 交叉引用：本地事务边界见第 7 章；消费幂等见第 11 章；Saga 补偿见第 13 章；对账见第 14 章
