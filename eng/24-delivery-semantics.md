# 24. 投递语义与重复消费

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 异步与消息 ｜ **编号**：24 / 52
> **本章定位**：三种投递语义不是三个产品开关，而是**三个「位点提交」与「业务处理」的先后顺序**。这一章把「重复」拆成四个来源、一张后果分级表、三个幂等落点，让你敢把至少一次语义真正用下去。
> **读法**：§24.2 先记住一句话「语义 = 位点与处理谁先谁后」；§24.4 的四来源表和分级表是值班时直接查的；§24.5 只记一句：Agent 侧重复一个 Step，不是脏数据，是**重复花钱**。

---

## 24.1 引子：一次滚动发布，重复发了 3.8 万条短信

**事故一（2025-06，某电商履约系统）**：履约 Topic `order.fulfilled` 12 分区、3 副本，消费组 6 实例，Kafka 3.6。消费端是 Spring Kafka 的常见配置：`enable.auto.commit=true`，`auto.commit.interval.ms=5000`，`max.poll.records=500`。

那天下午上线一个新功能：发货前调用第三方仓储接口做库存锁定。第三方当天 P99 从 80ms 抖到 **90 秒**。14:07:12，实例 `fulfill-3` 拉到一批 500 条，处理到第 37 条时卡在仓储接口上；`max.poll.interval.ms` 是默认的 **300000（5 分钟）**，14:12:15 它被协调器踢出消费组，触发 rebalance。

问题出在自动提交的语义：**自动提交提交的是「拉取位置」，不是「处理完成位置」**。`fulfill-3` 最后提交 offset 1,204,300，实际处理完只到 1,204,336；rebalance 后 `fulfill-5` 从 1,204,301 重来。

- 重复窗口 14:07—14:25，**18 分钟**；
- 重复消费 **64,000 条**（12 分区 × 约 5,300 条）；
- 重复创建发货单 **21,400 张**、重复发短信 **38,000 条**、重复扣仓储费 **9.7 万元**；
- 客诉 610 单，冲正做了 2 天。

**事故二（同月）**：生产端 `acks=1`、未开幂等，broker 主从切换时 1.2 万条消息「超时但已写入」，重试又写一遍；消费端无幂等键，积分双发 **24 万分**，损失 **16 万元**。

> 🩸 **共同点**：没人写错代码，他们只是**用了默认配置，并且相信「消息只会来一次」**。至少一次语义下，重复是设计内的常态；你不设计去重，系统就会用脏数据替你设计。

---

## 24.2 到底是什么：语义由「提交位点」的位置决定

**定义**：投递语义描述的是「一条消息从生产者到消费者的过程中，允许出现哪种错误」：

| 语义 | 允许丢 | 允许重 | 实现方式（一句话） |
| --- | --- | --- | --- |
| **至多一次** at-most-once | ✅ 允许 | ❌ 不允许 | **先提交位点，再处理** |
| **至少一次** at-least-once | ❌ 不允许 | ✅ 允许 | **先处理，再提交位点** |
| **恰好一次** exactly-once | ❌ | ❌ | 至少一次 + **消费端幂等**（+ 生产端幂等） |

这三种不是三个产品选项。Kafka、RocketMQ、RabbitMQ 的默认投递都是**至少一次**；所谓「开启 exactly-once」，开的从来不是「网络只送一次」，而是「重复到达时，效果不重复」。**网络层面无法证明一条消息只到达一次**——生产者没收到 ack 时，它永远不知道是「没送到」还是「送到了但回执丢了」。这是两将军问题，配置解决不了。

### 三个必须记住的反例

- **`enable.idempotence=true` 不是端到端幂等**：它只保证「同一生产者会话、同一分区」内不产生重复，靠 PID + sequence number 去重。消费者换了、跨 Topic、跨数据库，它一概不管。
- **Kafka 事务不是万能事务**：`transactional.id` + `read_committed` 只保证「消费 → 处理 → 生产」在 **Kafka 内部**的原子性。你的 `UPDATE account` 不在这个事务里，回滚不了。
- **「消息只投一次」不存在**：即使 broker 只存一条，消费者处理完、ack 丢失、broker 重投，业务仍然执行了两次。所以「恰好一次」的准确表述是：**至少一次投递 + 幂等消费 = 效果恰好一次**。

用公式写清楚，后面所有设计都从它推出来：

```
端到端恰好一次
  = 生产不丢（acks=all + min.insync.replicas>=2 + 幂等生产者）
  × 消费不丢（先处理后提交位点）
  × 副作用去重（业务幂等键 + 去重表/状态机/唯一索引）
```

三个乘号，缺一个就是 0。生产端参数见第 26 章，本章只负责第三个乘号。

---

## 24.3 结构图：位点与处理的四种时序，以及重复的四个来源

先看「提交位点」这一个动作放在哪里，如何决定了语义：

```
【至多一次】先提交，后处理
  poll ──► commit(offset=101) ──► 处理 101 ──► 崩溃
                                        └──► 101 永远丢了（broker 从 102 开始给）

【至少一次·正常】先处理，后提交
  poll ──► 处理 101 ──► 业务提交 ──► commit(101) ──► 下一条
                                      └──► 崩溃在 commit 之前 = 101 重来（重复）

【至少一次·提交失败】处理成功，commit 超时
  poll ──► 处理 101 ✅ ──► commit(101) ╳ 网络超时 ──► 重启
                                  └──► 从 101 重来 = 重复（业务已生效）

【批量提交的坑】处理 100 条，提交到第 100 条
  处理 1~99 ✅，第 100 条 ❌ 抛异常 ──► 整批不提交 ──► 1~99 全部重来
```

再把重复的四个来源画在同一张链路上——**排查重复时，先定位它属于哪一段，再去那段找证据**：

```
① 生产重试                     ② broker 重投
producer ──send──► broker ✅    broker ──deliver(offset=100)──► consumer
    │◄── ack 丢失/超时 ──╳              │  处理超时/未 ack
    └──retry──► broker 再写一条          └──redeliver(offset=100)──► consumer
    （新 offset、新 msgId，broker 认为是两条不同消息）

③ 消费失败重试（应用层）        ④ 位点回退 / 重平衡
consumer ──catch──► retry_topic ──► consumer   consumer A 处理到 120，最后提交 100
        （延迟 5s 后再投，新 offset）           └── 被踢出组 ──► consumer B 从 100 开始
                                                     101~120 全部重复
```

**四个来源的判定特征**（这是排查时最快的一刀）：

| 来源 | 触发条件 | 重复消息的特征 | 典型量级 |
| --- | --- | --- | --- |
| ① 生产重试 | 发送超时、ack 丢失、broker 主从切换、客户端重试策略 | **msgId 不同、offset 不同**，业务键相同，时间差 = 重试间隔 | 0.001%~0.1% |
| ② broker 重投 | 消费超时（`max.poll.interval.ms`）、会话超时、消费抛异常未 ack | **同分区同 offset**，`redelivery` 计数增加 | 故障期 1%~100% |
| ③ 消费失败重试 | 业务异常进重试队列/DLQ 后重投 | **新 Topic、新 offset**，header 里有 `retry_count` | 按失败率 |
| ④ 位点回退 | rebalance、手工 reset、位点提交失败、多实例争抢 | **一段连续 offset 区间**整体重放 | 可达数万条 |

> 💡 去重表记 `msgId` 没用：来源①和③的 msgId 都是新的，只有「业务键 + 消费者组」才稳定。这就是第 23 章说「分区键 = 保序键 = 幂等键」的原因。

---

## 24.4 怎么实现：先分级，再选落点

### 第一步：给业务后果分级（不是所有重复都值得治）

| 等级 | 判据 | 典型业务 | 处理方式 | 漏判后果 |
| --- | --- | --- | --- | --- |
| **L0 可忽略** | 重复执行的结果与执行一次**完全等价**，且无外部副作用 | 埋点上报、缓存刷新、`SET status=已读` | 不做去重，只做监控 | 多几条无用日志 |
| **L1 需去重** | 重复会多一条记录/多一次计数，但可事后冲正 | 积分发放、优惠券发放、消息通知、报表计数 | 去重表 或 唯一索引 | 脏数据，需人工冲正，客诉 |
| **L2 必须阻断** | 重复会直接造成资金损失、超卖、不可逆外部副作用 | 扣款、退款、库存扣减、下单、发短信/邮件、调第三方 | **唯一索引 + 状态机 + 去重表三件套**，缺一不可 | 资金损失、法律风险、无法回滚 |

判定方法只有一句：**问「这条消息执行两次，客户会不会少钱或多拿东西」**。会，就是 L2；不会但会多一条数据，L1；都不会，L0。

> ⚠️ 最危险的不是 L2，是**把 L2 误判成 L1**。积分多发还能扣回，退款重复打款要一家家去要。分级表必须由业务 owner 签字，不能由写代码的人自己拍。

### 第二步：三种语义的真实代价

| 维度 | 至多一次 | 至少一次 | 恰好一次（效果） |
| --- | --- | --- | --- |
| 位点提交时机 | 处理前 | 处理后 | 处理后 + 幂等落库 |
| 丢失风险 | **高**（崩溃即丢） | 低（可配 acks=all） | 低 |
| 重复风险 | 无 | **必然存在** | 被幂等挡住 |
| 吞吐成本 | 最低 | 中（每条一次提交或批量提交） | 中高（多一次 DB 写/查） |
| 存储成本 | 无 | 无 | 去重表 = 消息量 × 键长度 × 保留期 |
| 实现复杂度 | 低 | 低 | **高**（键设计、事务边界、清理） |
| 适用 | 日志、埋点、可丢的监控数据 | 绝大多数业务消息 | 资金、库存、订单、外部副作用 |

**结论**：别追求「开启 exactly-once」，要追求「至少一次 + 消费幂等」；前者是话术，后者是工程事实。

### 第三步：落点一——去重表（最通用，适合 L1/L2）

DDL 必须和业务表在**同一个库**，否则「去重行提交了、业务回滚了」会留下永久脏标记：

```sql
CREATE TABLE consume_dedup (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  consumer_group VARCHAR(64)  NOT NULL COMMENT '消费组，不同组各自去重',
  idem_key       VARCHAR(128) NOT NULL COMMENT '业务幂等键：order_id / (run_id,step_id)',
  topic          VARCHAR(128) NOT NULL,
  partition_no   INT          NOT NULL,
  offset_no      BIGINT       NOT NULL,
  status         TINYINT      NOT NULL DEFAULT 1 COMMENT '1=处理中 2=已完成',
  created_at     DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at     DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_group_key (consumer_group, idem_key),
  KEY idx_created (created_at),
  KEY idx_status_created (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='消费幂等去重表';
```

Java（Spring Kafka + JdbcTemplate，**去重行与业务写在同一本地事务**）：

```java
@KafkaListener(topics = "order.fulfilled", groupId = "point-service")
public void onMessage(ConsumerRecord<String, String> rec, Acknowledgment ack) {
    String orderId = rec.key();                       // 幂等键来自 key，不解析 body
    try {
        txTemplate.execute(status -> {
            int inserted = jdbc.update("""
                INSERT INTO consume_dedup
                  (consumer_group, idem_key, topic, partition_no, offset_no, status)
                VALUES (?, ?, ?, ?, ?, 2)
                ON DUPLICATE KEY UPDATE id = id
                """, "point-service", orderId, rec.topic(), rec.partition(), rec.offset());
            if (inserted == 0) {                      // 命中重复：直接当成功
                log.info("duplicate skipped key={} offset={}", orderId, rec.offset());
                return null;
            }
            jdbc.update("UPDATE point_account SET balance = balance + ? WHERE user_id = ?",
                        100, userIdOf(orderId));
            jdbc.update("INSERT INTO point_ledger(order_id, amount, biz_type) VALUES (?,?,?)",
                        orderId, 100, "ORDER_REWARD");
            return null;
        });
        ack.acknowledge();                            // 事务提交后才提交位点
    } catch (DuplicateKeyException e) {
        ack.acknowledge();                            // 唯一索引兜底，同样视为成功
    }
}
```

Go（database/sql，同样先事务后 ack）：

```go
func (h *Handler) Handle(ctx context.Context, m kafka.Message) error {
	key := string(m.Key) // order_id
	err := h.inTx(ctx, func(tx *sql.Tx) error {
		res, err := tx.ExecContext(ctx, `
			INSERT INTO consume_dedup
			  (consumer_group, idem_key, topic, partition_no, offset_no, status)
			VALUES (?, ?, ?, ?, ?, 2)
			ON DUPLICATE KEY UPDATE id = id`,
			"point-service", key, m.Topic, m.Partition, m.Offset)
		if err != nil {
			return err
		}
		n, _ := res.RowsAffected()
		if n == 0 {
			return nil // 已处理过，静默成功
		}
		if _, err = tx.ExecContext(ctx,
			`UPDATE point_account SET balance = balance + ? WHERE user_id = ?`, 100, userID(key)); err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx,
			`INSERT INTO point_ledger(order_id, amount, biz_type) VALUES (?, ?, ?)`,
			key, 100, "ORDER_REWARD")
		return err
	})
	if err != nil {
		return err // 不 ack，交给重试/DLQ
	}
	return h.committer.Commit(ctx, m) // 业务提交成功后再提交位点
}
```

**三个必须注意的细节**：

1. `status=1`（处理中）只在「去重表与业务不在同一事务」时才有意义；同库同事务时，直接写 `2`，事务回滚会一起去滚。
2. 唯一键必须是 `(consumer_group, idem_key)`：同一个订单会被积分和风控各自消费，要各自去重。
3. 不要 `SELECT` 再 `INSERT`：查询与写入之间有窗口，200 并发必然穿透；必须靠唯一索引或 `ON DUPLICATE KEY`。

### 第四步：落点二——业务状态机条件更新（最便宜，适合有状态流转的业务）

不需要额外的表，把「幂等」写进 `WHERE` 里。**影响行数 = 0 就代表这次是重复或非法迁移**：

```sql
-- 支付回调：只有「待支付」才能变「已支付」，重复回调影响 0 行
UPDATE order_info
   SET status = 'PAID', paid_at = NOW(3), pay_channel = ?, trade_no = ?
 WHERE order_id = ?
   AND status = 'PENDING_PAY'
   AND pay_amount = ?;

-- 库存扣减：把「够不够」写进 WHERE，天然防超卖且防重复扣
UPDATE sku_stock
   SET available = available - ?, version = version + 1
 WHERE sku_id = ? AND available >= ?;

-- 发货：只有「已支付」才能发货，重复消息不会发第二次
UPDATE order_info
   SET status = 'SHIPPED', shipped_at = NOW(3)
 WHERE order_id = ? AND status = 'PAID';
```

Java 侧判断影响行数：

```java
int affected = jdbc.update("UPDATE order_info SET status='PAID' WHERE order_id=? AND status='PENDING_PAY'",
                           orderId);
if (affected == 0) {
    // 要么已支付（重复），要么状态非法（取消/关闭）——查一次当前状态再决定
    String st = jdbc.queryForObject("SELECT status FROM order_info WHERE order_id=?", String.class, orderId);
    if ("PAID".equals(st)) return;                  // 幂等成功
    throw new IllegalStateException("illegal transition: " + st);
}
```

> 💡 状态机只对「有单向流转」的业务有效；`balance = balance + 100` 这种累加没有状态可判断，必须用去重表或唯一索引。详见第 30 章。

### 第五步：落点三——唯一索引（最硬，适合 L2）

业务表上建一个「业务唯一键」，让数据库替你做最后一道拦截：

```sql
ALTER TABLE point_ledger
  ADD COLUMN biz_key VARCHAR(128) NOT NULL DEFAULT '' COMMENT '幂等键：ORDER_REWARD:order_id',
  ADD UNIQUE KEY uk_biz_key (biz_key);

-- 写入时不做任何查询，冲突就是重复
INSERT INTO point_ledger (order_id, user_id, amount, biz_type, biz_key)
VALUES (?, ?, 100, 'ORDER_REWARD', CONCAT('ORDER_REWARD:', ?));
```

冲突处理的原则只有一条：**捕获 `DuplicateKeyException` 后必须回查一次，确认冲突的是「同一个业务键」而不是别的唯一索引**。很多事故是把「手机号唯一索引冲突」当成「消息重复」吞掉了，结果用户注册失败却返回成功。

### 第六步：去重表怎么清理（不清就是定时炸弹）

清理窗口的算法，取三者最大值再乘 2：

```
保留期 >= max(消息保留期, 最大重试窗口, 位点回退窗口) × 2
```

举例：Kafka `retention=72h`、重试最长 24h、rebalance 回退最多 2h → 保留 **7 天**。少于 72h，你就是在给「三天后的重放」留一个重复入口。

```sql
-- 每天凌晨分批删，每批 5000 行，避免长事务与主从延迟
DELETE FROM consume_dedup
 WHERE created_at < NOW() - INTERVAL 7 DAY
 LIMIT 5000;
-- 循环执行直到 affected rows = 0；生产上由调度平台每 5 分钟跑一次

-- 监控：去重表水位与增长速率
SELECT DATE(created_at) AS d, COUNT(*) AS rows_cnt
  FROM consume_dedup
 WHERE created_at >= NOW() - INTERVAL 14 DAY
 GROUP BY d ORDER BY d DESC;

-- 卡在「处理中」超过 10 分钟的记录（只可能出现在跨库场景）
SELECT consumer_group, idem_key, created_at
  FROM consume_dedup
 WHERE status = 1 AND created_at < NOW() - INTERVAL 10 MINUTE
 ORDER BY created_at LIMIT 100;
```

超过 5000 万行时按 `created_at` 做 RANGE 分区，直接 `DROP PARTITION`；或独立库。见第 36 章。

### 第七步：位点提交策略（语义的另一半）

```yaml
# Spring Kafka 消费端：手动提交 + 有界 poll，避免一批太大导致 rebalance
spring:
  kafka:
    consumer:
      enable-auto-commit: false
      max-poll-records: 100          # 一批 100 条，处理时间可控
      properties:
        max.poll.interval.ms: 300000 # 必须 > 单批最坏处理时间 × 2
        session.timeout.ms: 45000
        heartbeat.interval.ms: 3000
        isolation.level: read_committed
    listener:
      ack-mode: manual_immediate     # 处理完立刻提交，不攒批
```

> ⚠️ `ack-mode: batch` 是重复放大器：一批 100 条里第 99 条失败，前 98 条已生效但位点不提交，重试全部重来。用 `manual_immediate`，或确保批内每条都幂等。

---

## 24.5 双修对照：同一套去重，两种计价单位

| 维度 | 传统系统 | Agent 系统 | 同 or 异 |
| --- | --- | --- | --- |
| **机制本体** | 订单事件、支付回调、积分发放的重复消费 | **Step 事件流**重放：`llm.requested`、`tool.called`、`llm.responded` | **完全相同**：都是「同一条不可变记录被处理两次」 |
| **身份 / 唯一键** | `order_id` + `biz_type` | `(run_id, step_id)`；工具副作用再加 `(tool_name, args_hash)` | 表结构直接复用，**键多一维** |
| **重复来源** | 生产重试、broker 重投、失败重试、位点回退 | 同上 + **Worker 租约过期后另一个 Worker 接手同一个 Run** | 四来源全中，多一个「租约」来源 |
| **失败模式** | 重复扣款、重复发货、重复积分 | 重复 Step = **重复调模型 = 重复花钱** + 重复工具副作用（发邮件/下单/删文件） | **后果升级**：多一个成本维度，且模型调用不可回滚 |
| **恢复方式** | 重投消息，从位点继续 | 长任务**用 checkpoint 恢复**，不重投整条 Run（重投 = 从头重跑，20 万 token 白花） | **根本差异**，详见第 26 章 |
| **验证方法** | 业务表条数 vs 消息条数对账 | 业务表 + `step` 表 + **token 账单**三方对账 | 框架相同，多对一笔账 |
| **观测指标** | 重复命中率、去重表水位 | 加 `duplicate_step_total`、`wasted_tokens_total`、单 Run 成本 | 指标集扩展 |
| **稀缺资源** | 数据库连接、磁盘 | **TPM、模型并发、单 Run 预算** | 限的东西不同 |

**可以直接复用的**（一套代码两边用）：去重表 DDL 与 `INSERT ... ON DUPLICATE KEY`、状态机条件更新 SQL、唯一索引 + 冲突回查、位点提交策略、保留期算法与清理任务。

**只有三样必须重算**：

1. **幂等键的组成**。传统 `order_id` 够用；Agent 必须 `(run_id, step_id)`，且 prompt/model 版本变了要生成新 `step_id`，不能复用旧键。
2. **重复的成本**。传统重复一次是几毫秒 CPU；Agent 重复一次可能是一次 3 万 token 的调用（约 0.6 元）。10 万次重复就是 6 万元，而且账单上看起来「正常」。
3. **去重的粒度**。传统按「业务单据」去重；Agent 要按「副作用」去重——同一个 Step 内可能调了 3 个工具，只有「发邮件」需要去重，「查天气」不需要。

> 三定律检验：**状态机**——真相源在 `Run`/`Step` 表，重复通知不应改变终态；**稀缺资源**——传统重复耗 DB 连接，Agent 重复耗 token 与预算；**不可回滚**——已发的邮件、已扣的钱只能补偿，必须在**调用前**用幂等键挡住。

---

## 24.6 会在哪炸：失效模式清单

| # | 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- | --- |
| 1 | 上线 7 天后突然出现重复 | 去重表 TTL 短于消息保留期/重放窗口 | 比对时间 | 延长保留期到窗口 ×2 | 保留期写进 Topic YAML，CI 校验 |
| 2 | 去重表有记录但业务没生效 | 去重表与业务表不在同一事务/同库 | 查 `status=1` 的卡死记录 | 同库同事务；跨库用状态机或补偿扫描 | 去重表必须与业务同库，写进规范 |
| 3 | 200 并发下仍然重复 | 先 `SELECT` 再 `INSERT`，查询与写入有窗口 | 压测复现，看是否两行都插入成功 | 改唯一索引 / `ON DUPLICATE KEY` | Code review 禁止「先查后插」 |
| 4 | 唯一索引冲突被吞，业务返回成功 | `catch (DuplicateKeyException)` 后没回查冲突键 | 看异常里的 index name | 只吞「业务键」冲突，其它上抛 | 封装 `IdempotentExecutor`，禁止裸 catch |
| 5 | 重试后重复 | 重试生成了新的 `request_id` | trace 里两次请求的 id 不同 | 重试复用原幂等键 | 幂等键由调用方生成并透传 |
| 6 | 批量 ack 导致整批重放 | `ack-mode=batch` + 批内单条失败 | 日志里同批 offset 重复出现 | 改 `manual_immediate` 或批内逐条幂等 | 批大小 ≤100，最坏处理时间 < poll interval/2 |
| 7 | rebalance 后重复数万条 | 自动提交提交的是拉取位置 | `--describe` 看 `CURRENT-OFFSET` 与 `LOG-END-OFFSET` 跳变 | 关自动提交；缩短单批处理时间 | `max.poll.records` 与 `max.poll.interval.ms` 联动配置 |
| 8 | 幂等键用了可变字段（如 `status`、`updated_at`） | 键随业务变化，第二次算出不同值 | 抽样同订单两次消息的键 | 改用不可变业务主键 | 幂等键评审：必须是「创建后不变的字段」 |

---

## 24.7 怎么验证：三件可执行的事

**① 并发重放压测**：同一个幂等键，200 并发打 10,000 次，业务表必须只有 1 条：

```bash
# 用 k6 直接打消费端暴露的测试入口；或往测试 Topic 灌 1 万条同 key 消息
k6 run --vus 200 --duration 60s ./dup-replay.js
# 校验 SQL：结果必须是 1
mysql -e "SELECT COUNT(*) FROM point_ledger WHERE biz_key='ORDER_REWARD:TEST-001'"
```

**② 故障注入**（提交前/后崩溃都要安全）：

```bash
# 在消费到第 50 条时 kill -9，重启后看是否重复且业务只生效一次
kill -9 $(pgrep -f point-service)
# 人为让 commit 超时：用 tc 注入 3 秒延迟到 broker
sudo tc qdisc add dev eth0 root netem delay 3000ms
```

**③ 对账 SQL**（每天跑，抓「去重表说有、业务表没有」）：

```sql
-- 去重表已标记完成，但业务流水缺失 → 说明事务边界写错了
SELECT d.idem_key, d.created_at
  FROM consume_dedup d
  LEFT JOIN point_ledger p
    ON p.biz_key = CONCAT('ORDER_REWARD:', d.idem_key)
 WHERE d.consumer_group = 'point-service'
   AND d.status = 2
   AND d.created_at >= NOW() - INTERVAL 1 DAY
   AND p.id IS NULL
 LIMIT 100;

-- 重复命中率（按小时）：突然升高说明上游在重试或发生了 rebalance
SELECT DATE_FORMAT(created_at, '%Y-%m-%d %H') AS h,
       COUNT(*) AS total,
       SUM(is_dup) AS dup
  FROM consume_dedup_stat
 GROUP BY h ORDER BY h DESC LIMIT 24;
```

**④ 四个指标**：`duplicate_hit_ratio`、`dedup_table_rows`、`consumer_lag`（按分区）、`rebalance_total`。最后一个突增，通常就是重复量突增的前兆。

---

## 24.8 用什么工具：哪些能买，哪些必须自己写

| 能力 | 开源/框架够用 | 必须自己写 |
| --- | --- | --- |
| 位点管理 | Kafka / RocketMQ / RabbitMQ 客户端 | 提交时机、批量大小、异常时的 ack 决策 |
| 消费幂等 | —— | **去重表、状态机 SQL、唯一索引，一个都不能少** |
| 去重键设计 | —— | 业务唯一键 + 版本维度，评审签字 |
| 轻量去重 | Redis `SET NX PX`（有丢数据风险） | 只能用于 L0/L1，不能用于资金 |
| 清理与水位 | 分区表、调度平台 | 保留期算法、分批删除、卡死扫描 |

**判断口径**：MQ 客户端只负责投递与记录位点，**它不知道你的业务键，也决定不了「重复了要不要跳过」**；业务幂等永远是你的代码。

---

## 24.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 语义的本质 | 不是产品开关，是「提交位点」与「业务处理」谁先谁后 |
| 至多一次 | 先提交后处理：不重，但崩了就丢 |
| 至少一次 | 先处理后提交：不丢，但重复是必然 |
| 恰好一次 | 至少一次投递 + 消费幂等 = **效果**恰好一次 |
| 三个乘号 | 生产不丢 × 消费不丢 × 副作用去重，缺一个就是 0 |
| 重复四来源 | 生产重试（新 msgId）、broker 重投（同 offset）、失败重试（新 Topic）、位点回退（连续区间） |
| 去重键 | 必须是「创建后不变」的业务字段；`msgId` 不能当去重键 |
| 后果分级 | L0 可忽略 / L1 需去重 / L2 必须阻断；L2 误判成 L1 最贵 |
| 三落点 | 去重表（通用）、状态机条件更新（有流转）、唯一索引（最硬） |
| 事务边界 | 去重表必须与业务表同库同事务，否则会留下永久脏标记 |
| 保留期 | `max(消息保留, 重试窗口, 回退窗口) × 2`，少于这个数就是给重放留后门 |
| 批提交 | `ack-mode=batch` 是重复放大器；一批失败，前面全部重来 |
| Agent 侧 | 重复 Step = 重复调模型 = **重复花钱**；去重粒度要细到「副作用」 |

## 24.10 自检题

1. **你们用 Kafka，`enable.auto.commit=true`，消费者处理一条要 2 秒，`max.poll.records=500`。请算出最坏情况下一次 rebalance 会重复多少条，并给出两个修法。**
   > 要点：一批 500 条 × 2 秒 = 1000 秒 > 默认 `max.poll.interval.ms=300000`（300 秒），消费者必然在处理途中被踢出组；最坏重复 500 条（整批未提交）。修法：① `max.poll.records` 降到 100 以下，保证单批处理时间 < poll interval/2；② 关自动提交，改 `manual_immediate`，并让每条消费幂等。只改其中一个不够——降批大小减少重复量，幂等保证重复无害。

2. **去重表已经 `INSERT` 成功了，但业务更新失败回滚了，会怎样？怎么设计才不会有这个问题？**
   > 要点：如果去重表与业务表在同一事务，业务回滚会连同去重行一起回滚，没有问题；如果不在同一事务/同库，去重行会留下 `status=1` 的永久标记，后续消息全被当成重复跳过，业务永远不生效。设计：同库同事务写 `status=2`；跨库场景必须把去重放在**业务所在的库**，或引入「处理中 + 超时补偿扫描」，不能只写一张中央去重表。

3. **为什么说「Kafka 开启 exactly-once」不能保证你的扣款只扣一次？**
   > 要点：Kafka 的 EOS 边界只在 Kafka 内部（consume-transform-produce 的原子提交 + `read_committed`），它管不到你的 MySQL `UPDATE account`。扣款是外部副作用，事务回滚不了已提交的 DB 写，也无法撤销已发出的 RPC。要保证只扣一次，必须在扣款这一侧用业务幂等键 + 唯一索引/状态机，和 MQ 的语义无关。

## 24.11 延伸阅读与交叉引用

- 三种语义与位点提交的完整推导：[第 11 章](11-exactly-once-truth.md)（本套只在那里讲一次「恰好一次的边界」）
- Outbox 与事务消息：[第 10 章](10-outbox-and-transactional-message.md)；幂等八种实现：[第 2 章](2-idempotency-implementations.md)
- 幂等失效模式：[第 5 章](5-idempotency-failure-modes.md)；对账兜底：[第 14 章](14-reconciliation-system.md)
- 顺序与堆积：[第 25 章](25-ordering-and-lag.md)；生产/消费端可靠性：[第 26 章](26-message-reliability.md)
- Agent 侧事件模型与 `(run_id, seq)` 去重：见 [../llm/6-session-run-event-model.md](../llm/6-session-run-event-model.md)
- 官方文档（以版本为准）：Kafka `ConsumerConfig` 的 `enable.auto.commit` / `max.poll.interval.ms`；Spring Kafka `AckMode`；RocketMQ 消费重试与 `MessageListenerOrderly`
- 总纲：[0-summary.md](0-summary.md) ｜ 语言与运行时：[../go/0-summary.md](../go/0-summary.md) ｜ 平台治理：[../llm/0-summary.md](../llm/0-summary.md)
- 前置：[第 23 章](23-async-messaging-basics.md) ｜ 下一章：[第 25 章 顺序、堆积与重平衡](25-ordering-and-lag.md)
