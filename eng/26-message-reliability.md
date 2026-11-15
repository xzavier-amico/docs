# 26. 消息可靠性：生产端与消费端

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 异步与消息 ｜ **编号**：26 / 52
> **本章定位**：可靠性不是一个开关，而是**八个环节的连乘**——任何一环为 0，端到端就是 0。这一章把生产端的 `acks`/ISR/HW 讲到「broker 什么时候真的落了盘」，把消费端的「先处理后提交」讲到 `__consumer_offsets` 的字节结构，再给你一套能跑的重试队列与死信队列。
> **读法**：§26.2 的八环节表先背下来，后面所有配置都是给这八环打补丁；§26.4 第二步（发送成功但没落盘）与第四步（DLQ）是本章的核心；§26.5 只记一句：Agent 侧长任务**用检查点续跑，不要重投整条 Run**。

---

## 26.1 引子：三条「已发送」的消息，两小时后天塌了

**事故一（2025-05-12，某电商支付成功通知）**。Topic `pay.success`，6 分区 3 副本，生产端配置是团队「祖传」的一份：

```properties
acks=1
retries=3
min.insync.replicas=1
unclean.leader.election.enable=true
```

- **10:02** `broker-3` 的 SSD 报 SMART 错误，内核把盘置为只读，Kafka 日志目录写入失败，broker 进程退出。
- **10:02:40** `pay.success` 的 partition-2 leader 在 `broker-3` 上，Controller 触发选举。ISR 里有 `broker-1`、`broker-5`。
- **10:02:41** 但 `broker-1` 当时正被一个全量同步任务拖着，ISR 已经缩到只剩 `{broker-3}`（`min.insync.replicas=1` 所以写入一直没报错）；`broker-5` 的 LEO 落后 **3,400 条**。
- **10:02:44** 因为 ISR 里没有其他成员，加上 `unclean.leader.election.enable=true`，Controller 选了 `broker-5` 当 leader，并把它的 LEO 当作新 HW。
- **10:02:45** 那 **3,400 条**「已 ack」的支付成功通知被**截断**（log truncation）。生产端日志里每一条都是 `offset=8,412,297 ... ack received`。
- **12:40** 客服开始集中收到「我付了钱，订单还是待支付」。此时距事发 **2 小时 38 分**。
- 影响 **3,400 笔**订单，其中 **1,180 笔**用户重复支付（因为页面提示「支付失败」），退款加赔付 **11.6 万元**，客诉 **740 单**。

**事故二（2025-07-03，某 SaaS 的积分服务）**。这次丢消息的不是 broker，是消费端的一行「优化」：

```java
// 上线前的写法：处理完再 ack（正确）
// 上线后的「优化」：先 ack 再异步处理，为了「提高吞吐」
ack.acknowledge();
executor.submit(() -> pointService.grant(orderId, amount));
```

- **14:00** 发布，吞吐从 3,200 msg/s 提到 7,800 msg/s，团队很满意。
- **14:37** 容器内存到达 limit（1.5Gi），`executor` 的 `LinkedBlockingQueue`（无界）里积压 **12,000** 个任务，OOMKill。
- **14:37—14:41** Pod 重启。因为位点已经提交，broker 认为这 12,000 条「已被处理」，不会重投。
- **14:41** 服务恢复，lag = 0，所有看板全绿。
- **07-06**（三天后）财务对账发现积分少了 **12,000 笔**，对应 **8.4 万元**的权益未发放，客诉 **310 单**。

> 🩸 两起事故的共同点：**生产端和消费端都「成功」了，消息还是没了。** 生产端的成功是「leader 的内存里有了」，消费端的成功是「位点提交了」——**这两个「成功」都与「业务真的发生」无关**。可靠性必须从「业务是否落地」倒着定义，这就是下一节的八环节。

---

## 26.2 到底是什么：可靠性是八个环节的连乘

**定义**：端到端可靠 = 消息从业务产生到业务落地，**每一段都不丢**。写成乘积：

```
P(端到端不丢) = P(①业务发出) × P(②客户端接受) × P(③到达 broker) × P(④broker 落盘)
              × P(⑤副本不截断) × P(⑥未被过期/磁盘清掉) × P(⑦被消费处理)
              × P(⑧业务落库成功且位点未提前提交)
```

每一环都能单独做到 99.99%，八环连乘就是 `0.9999^8 ≈ 99.92%`——**每天 1,000 万条消息，就有 8,000 条会消失**。这就是为什么「我们用了 Kafka，很可靠」这句话没有意义：Kafka 只负责 ③④⑤⑥ 四环，① ② ⑦ ⑧ 全是你的代码。

**六个反例**（每个都配「不做会怎样」）：

| 你以为的可靠 | 为什么不是 | 不做会怎样 |
| --- | --- | --- |
| 「`acks=all` 就一定不丢」 | `min.insync.replicas=1` 时 ISR 只剩 leader，`all` 等于 `1`；`unclean` 选举还会截断 | 事故一：3,400 条已 ack 的消息被 truncate |
| 「`try { send() } catch` 包住就安全」 | `send()` 是异步的，异常在回调线程里抛；超时后你也不知道写没写进去 | 生产端「没有异常」但消息从未落盘 |
| 「配了 DLQ 就不会丢」 | DLQ 只接「消费失败」，接不住「位点已提交但没处理」和第 ③④ 环 | 事故二：lag=0，DLQ=0，消息没了 |
| 「重试 3 次够了」 | 重试是**重复**的来源，不是**不丢**的保证；没有幂等，重试就是把丢失换成脏数据 | 积分双发，比丢失更难查 |
| 「broker 有 3 副本就安全」 | 副本在 ISR 里才算数；ISR 收缩时没人告警，等于单副本运行 | 长期单副本运行，一次磁盘故障就是 P0 |
| 「消费成功 = 业务成功」 | 消费者 `return` 成功可能只是「消息进了线程池」，业务还没跑 | 事故二：位点前进，业务没发生 |

**可靠性必须用「业务对账」闭环**：不管八环做得多好，都要有一条 `生产条数 vs 落地条数` 的对账（第 14 章）。**对账不是可靠性的补充，是可靠性的验收标准。**

---

## 26.3 结构图：八环节，与「假落盘」的 3 毫秒

先看八个环节分别在哪、谁负责：

```
┌── 你的代码 ──┐┌──────── 客户端 ────────┐┌──────── broker ────────┐┌── 你的代码 ──┐
│ ①业务发出    ││ ②序列化/大小校验       ││ ③网络到达 leader       ││ ⑦消费处理    │
│  事务提交后? ││  超限 → 客户端直接拒    ││ ④写 page cache → ack   ││  反序列化?   │
│  异常被吞?   ││ ③重试 → 可能重复       ││ ⑤副本 fetch → HW 推进  ││  异常吞了?   │
└──────┬───────┘└───────────┬────────────┘│ ⑥retention/磁盘满      ││ ⑧业务落库    │
       │                    │             └───────────┬────────────┘│  与位点顺序  │
       ▼                    ▼                         ▼             └──────┬───────┘
   [Outbox 表]        [RecordTooLarge]        [ISR 收缩/unclean]      [位点已提交]
       │                    │                         │                    │
       └────────────────────┴─────────────────────────┴────────────────────┘
                                    每环都要有「检查方法」（§26.4 第五步）
```

再放大 ④⑤，看「说成功但其实没落盘」的那 3 毫秒：

```
 t0   producer ──── ProduceRequest(acks=1) ────► leader(broker-3)
 t1                                              │ ① append 到本地 log
                                                 │    LEO: 8,412,300
 t2                                              │ ② 写入 OS page cache
                                                 │    （还没 fsync，甚至没刷盘）
 t3   producer ◄──── Response(offset=8,412,299) ─┘ ③ 立刻返回「成功」
                                                 │
 t4                                              │ ④ follower(broker-5) fetch
                                                 │    但它落后：LEO=8,408,900
 t5   ✗ broker-3 磁盘故障，进程退出               │    HW 仍停在 8,408,900
                                                 │
 t6   controller: ISR 只剩 {broker-3}（已死）      │
      → unclean 选举：broker-5 成为 leader         │
      → 新 HW = 8,408,900                         │
                                                 ▼
 t7   那 3,400 条（8,408,900 ~ 8,412,299）被截断 —— 它们「成功」过，但从未落盘到多数副本
```

**三个关键概念，必须分清**：

| 概念 | 定义 | 谁在推进 | 不懂它会怎样 |
| --- | --- | --- | --- |
| **LEO**（Log End Offset） | 每个副本自己日志的末尾位置 | 每次 append | 以为「写进 leader 就是写进去了」 |
| **HW**（High Watermark） | `min(ISR 内所有副本的 LEO)` | follower 的 fetch 进度 | 消费者只能读到 HW，读不到未复制的部分 |
| **ISR**（In-Sync Replicas） | 与 leader 保持同步的副本集合（`replica.lag.time.max.ms` 内追上） | follower 的 fetch + 时间窗 | ISR 缩到 1 时，`acks=all` 退化成 `acks=1` |

所以「`acks=all` 是什么」准确的答案是：**leader 等到当前 ISR 中所有副本都 append 成功（写进各自的 page cache）才回 ack**。它保证「至少写进了 N 个副本的内存」，**不保证 fsync 到磁盘**（Kafka 默认不 fsync，靠多副本冗余；要更强就配 `flush.messages`，代价是吞吐掉一个数量级）。

> ⚠️ 这就是「发送成功但 broker 没落盘」的本质：**ack 的语义是「ISR 都收到了」，不是「磁盘上有了」**。当 ISR 只剩 1 个、或发生 unclean 选举时，这个语义就崩了。

---

## 26.4 怎么实现：给八环逐环打补丁

### 第一步：生产端参数（Kafka 与 RocketMQ 的真实差异）

**Kafka 生产端**（3.x 口径，逐项给「配错的后果」）：

| 参数 | 建议值 | 作用 | 配错的后果 |
| --- | --- | --- | --- |
| `acks` | `all` | leader 等 ISR 全部落副本 | `1`：leader 宕机即丢（事故一）；`0`：发出去就不管 |
| `enable.idempotence` | `true` | PID + 分区内 sequence 去重，防重试重复 | `false`：重试产生重复（第 24 章） |
| `max.in.flight.requests.per.connection` | `5`（幂等开启时上限） | 单连接未确认请求数 | 幂等开启时 >5 会被拒绝或失去顺序保证 |
| `retries` | `Integer.MAX_VALUE`（配合 delivery.timeout） | 可重试错误的重试次数 | 设 3：一次 broker 抖动就丢弃 |
| `delivery.timeout.ms` | `120000` | 从 send 到最终成功/失败的总预算 | 小于 `linger+batch+request`，会「还没发就超时」 |
| `request.timeout.ms` | `30000` | 单次请求超时 | 太小 → 频繁误判超时 → 重试风暴 |
| `linger.ms` | `5~20` | 攒批等待 | 0：吞吐低；>100：端到端延迟被拉长 |
| `batch.size` / `compression.type` | `64KB` / `zstd` | 批大小与压缩 | 批太大 → 内存与延迟；不压缩 → 带宽成本翻倍 |
| `min.insync.replicas`（**topic/broker 级**） | `2` | `acks=all` 需要的最少 ISR 数 | `1`：`all` 形同虚设（事故一） |
| `unclean.leader.election.enable`（**broker 级**） | `false` | 是否允许落后副本当 leader | `true`：直接数据截断（事故一） |
| `auto.create.topics.enable` | `false` | 禁止自动建 Topic | 拼错 Topic 名会静默创建一个 1 分区的新 Topic |

```yaml
# Spring Kafka 生产端：可靠性优先的完整配置（数值按你的 SLO 调，别照抄）
spring:
  kafka:
    bootstrap-servers: broker1:9092,broker2:9092,broker3:9092
    producer:
      acks: all
      retries: 2147483647
      key-serializer: org.apache.kafka.common.serialization.StringSerializer
      value-serializer: org.apache.kafka.common.serialization.StringSerializer
      compression-type: zstd
      properties:
        enable.idempotence: true
        max.in.flight.requests.per.connection: 5
        delivery.timeout.ms: 120000      # 必须 > linger + request.timeout
        request.timeout.ms: 30000
        linger.ms: 10
        batch.size: 65536
        buffer.memory: 67108864
        # 关键：不要用「业务线程池」异步发送后不等待，见下方代码
    # Topic 级（用 NewTopic Bean 或运维平台声明，别用 auto-create）
    # min.insync.replicas=2, replication.factor=3, unclean.leader.election=false
```

**RocketMQ 生产端**（4.x/5.x 差异如实标注）：

| 参数 | 建议 | 说明 | 配错后果 |
| --- | --- | --- | --- |
| 发送方式 | `SYNC` | 等 broker 返回 `SEND_OK` | `ONEWAY`：无返回，丢了不知道 |
| `sendTimeout` | `3000`（ms） | 单次发送超时 | 太小 → 误判失败 → 重试重复 |
| `retryTimesWhenSendFailed` | `2` | 同步发送失败重试次数（含换 broker） | 0：一次网络抖动即失败 |
| `retryAnotherBrokerWhenNotStoreOK` | `true` | 未存成功时换 broker 重试 | `false`：在坏 broker 上死磕 |
| `acks`（5.x） | `all` | 5.x 引入 `acks=0/1/all`，以 5.x 文档为准 | 4.x 默认主从异步复制，主挂可能丢 |
| 事务消息 | 半消息 + 回查 | 用于「本地事务 + 发消息」原子化（第 10 章） | 用普通消息 + 手写补偿，容易出现「扣了钱没发消息」 |

### 第二步：「发送成功但 broker 没落盘」怎么办

先把「成功」的三种含义摆清楚，**很多争论是因为大家在说不同的成功**：

| 「成功」出现在哪 | 实际含义 | 崩溃后是否还在 |
| --- | --- | --- |
| `send()` 返回的 Future 完成 | 消息进了客户端缓冲区 / 已发出 | 客户端进程崩溃 → 丢 |
| 收到 ack（`acks=1`） | leader 的 page cache 有了 | leader 宕机 + 未复制 → 丢（事故一） |
| 收到 ack（`acks=all` + `min.insync=2`） | 至少 2 个副本的 page cache 有了 | 单副本故障 → 不丢；多数副本同时故障 → 丢 |
| broker fsync 完成 | 落盘 | 只有配 `flush.messages=1` 才有，吞吐代价极大 |

**三种应对，按「代价」排序，通常三个都要上**：

```
【应对 A：把「成功」的语义收紧】—— 防「已 ack 却丢」
  acks=all
  min.insync.replicas=2
  unclean.leader.election.enable=false
  default.replication.factor=3
  效果：ISR < 2 时写入直接失败（NotEnoughReplicasException），宁可不可用也不丢
  代价：可用性下降（2 个副本同时不可用 → 该分区不可写）；必须配告警「ISR 收缩」

【应对 B：让重试不产生重复】—— 防「收紧语义后大量重试 → 重复」
  enable.idempotence=true（PID + sequence，broker 每分区保留最近 5 个 batch 做去重）
  delivery.timeout.ms 给足（120s），retries 给到 MAX
  效果：客户端重试不会在 broker 侧写出两条
  边界：只覆盖「同一 producer session、同一分区」；producer 重启换 PID 就失效

【应对 C：业务侧兜底】—— 防「A 和 B 都覆盖不到的窗口」
  Outbox 表（本地事务写业务 + 写消息表）→ 投递器 → 对账
  效果：即使消息系统整体丢了，也能从 Outbox 重放 / 从对账发现差异
  代价：多一张表、多一个投递器（第 10 章深挖）
```

**关键判断**：A 和 B 是**基础设施层**的补丁，C 是**业务层**的补丁。三者的关系不是「选一个」，而是「A 保证不丢、B 保证不重、C 保证最终能发现并修复」。**只做 A+B 的团队，遇到「位点已提交但业务没跑」照样丢；只做 C 的团队，每天在对账里捞几百条差异。**

**为什么 `acks=all` 还要配 `min.insync.replicas=2`**：`acks=all` 的「all」指的是**当前 ISR 的所有成员**。如果 ISR 因为 follower 落后而缩到只剩 leader，`all` 就等于 `1`，此时 `acks=all` 与 `acks=1` 完全等价——这就是事故一里最讽刺的一点：**他们配了 `acks=1`，但如果只把 `acks` 改成 `all` 而不改 `min.insync.replicas`，事故照样发生。**

```bash
# 必须监控的两个量：ISR 收缩、HW 与 LEO 的差
kafka-topics.sh --bootstrap-server broker1:9092 --describe --topic pay.success \
  | awk '{print $2, $4, $6, $8}'   # Partition, Leader, Replicas, Isr

# Isr 数量 < 副本数就是告警；持续 5 分钟 = P1（你在单副本运行）
kafka-topics.sh --bootstrap-server broker1:9092 --describe --topic pay.success \
  | grep -oP 'Isr: \K[0-9,]+' | awk -F, '{print NF}' | sort | uniq -c
```

**Java：带 Outbox 的可靠发送（本地事务 + 投递器，两个阶段都不能省）**

```java
@Service
public class PaySuccessPublisher {

    private final JdbcTemplate jdbc;
    private final KafkaTemplate<String, String> kafka;

    // ① 业务写库与「消息表」在同一个本地事务里 —— 这是整个方案的地基
    @Transactional
    public void onPaySuccess(PaySuccessEvent e) {
        jdbc.update("UPDATE order_info SET status='PAID', paid_at=NOW(3) " +
                    "WHERE order_id=? AND status='PENDING_PAY'", e.orderId());
        jdbc.update("""
            INSERT INTO outbox_message
              (msg_id, topic, msg_key, payload, status, next_retry_at, created_at)
            VALUES (?, 'pay.success', ?, ?, 0, NOW(3), NOW(3))
            ON DUPLICATE KEY UPDATE msg_id = msg_id
            """, e.msgId(), e.orderId(), Json.write(e));
        // 注意：不在这里 send()。事务提交前发消息 = 事务回滚后消息已发出（第 10 章）
    }

    // ② 投递器：独立线程/任务，扫描待发送，失败退避。send 的回调决定 status
    @Scheduled(fixedDelay = 200)
    public void dispatch() {
        List<OutboxRow> rows = jdbc.query("""
            SELECT id, msg_id, topic, msg_key, payload, retry_count
              FROM outbox_message
             WHERE status = 0 AND next_retry_at <= NOW(3)
             ORDER BY id LIMIT 200
             FOR UPDATE SKIP LOCKED
            """, outboxMapper);
        for (OutboxRow r : rows) {
            kafka.send(r.topic(), r.msgKey(), r.payload())
                 .whenComplete((md, ex) -> {
                     if (ex == null) {
                         jdbc.update("UPDATE outbox_message SET status=2, sent_at=NOW(3) WHERE id=?", r.id());
                     } else {
                         // 指数退避 + 抖动，避免投递器自己形成重试风暴
                         long delay = Math.min(300, (1L << Math.min(r.retryCount(), 8))) * 1000;
                         delay = (long) (delay * (0.8 + Math.random() * 0.4));
                         jdbc.update("""
                             UPDATE outbox_message
                                SET retry_count = retry_count + 1,
                                    status = IF(retry_count >= 10, 3, 0),
                                    last_error = ?,
                                    next_retry_at = DATE_ADD(NOW(3), INTERVAL ? MICROSECOND)
                              WHERE id = ?
                             """, truncate(ex.toString(), 500), delay * 1000, r.id());
                     }
                 });
        }
    }
}
```

```sql
-- Outbox 表：status 必须是「小状态机」，不是布尔
CREATE TABLE outbox_message (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  msg_id        VARCHAR(64)   NOT NULL COMMENT '业务消息唯一 ID，消费端幂等键',
  topic         VARCHAR(128)  NOT NULL,
  msg_key       VARCHAR(128)  NOT NULL COMMENT '分区键=保序键=幂等键',
  payload       MEDIUMTEXT    NOT NULL,
  status        TINYINT       NOT NULL DEFAULT 0 COMMENT '0待发 1发送中 2已发 3死信',
  retry_count   INT           NOT NULL DEFAULT 0,
  next_retry_at DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  last_error    VARCHAR(512)  NOT NULL DEFAULT '',
  created_at    DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  sent_at       DATETIME(3)   NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_msg_id (msg_id),
  KEY idx_status_next (status, next_retry_at),
  KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='本地消息表：与业务同事务写入';

-- 巡检：卡在「发送中」超过 5 分钟（投递器崩溃留下的）
-- 巡检：死信积压（必须有人处理，否则等于丢）
SELECT status, COUNT(*) AS cnt, MIN(created_at) AS oldest
  FROM outbox_message
 WHERE created_at >= NOW() - INTERVAL 1 DAY
 GROUP BY status;
```

**Go：Sarama 同步发送 + 错误分类（可重试 vs 不可重试）**

```go
package producer

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/IBM/sarama"
)

type Publisher struct {
	p sarama.SyncProducer
}

func New(brokers []string) (*Publisher, error) {
	cfg := sarama.NewConfig()
	cfg.Version = sarama.V3_6_0_0
	cfg.Producer.RequiredAcks = sarama.WaitForAll          // acks=all
	cfg.Producer.Idempotent = true                         // 需要 Net.MaxOpenRequests=1
	cfg.Net.MaxOpenRequests = 1
	cfg.Producer.Retry.Max = 10
	cfg.Producer.Retry.Backoff = 100 * time.Millisecond
	cfg.Producer.Return.Successes = true                   // SyncProducer 必须开
	cfg.Producer.Timeout = 30 * time.Second
	cfg.Producer.Compression = sarama.CompressionZSTD
	p, err := sarama.NewSyncProducer(brokers, cfg)
	if err != nil {
		return nil, fmt.Errorf("new producer: %w", err)
	}
	return &Publisher{p: p}, nil
}

// Publish 返回的错误必须分类：可重试交给上游退避，不可重试直接进死信/告警
func (p *Publisher) Publish(ctx context.Context, topic, key string, value []byte) (int32, int64, error) {
	msg := &sarama.ProducerMessage{
		Topic: topic,
		Key:   sarama.StringEncoder(key),
		Value: sarama.ByteEncoder(value),
	}
	part, off, err := p.p.SendMessage(msg)
	if err == nil {
		return part, off, nil
	}
	switch {
	case errors.Is(err, sarama.ErrMessageSizeTooLarge):
		return part, off, fmt.Errorf("%w: value=%dB", ErrPermanent, len(value))
	case errors.Is(err, sarama.ErrInvalidMessage):
		return part, off, fmt.Errorf("%w: %v", ErrPermanent, err)
	default:
		// 超时、NotLeader、NotEnoughReplicas 等都视为可重试
		return part, off, fmt.Errorf("%w: %v", ErrRetryable, err)
	}
}

var (
	ErrRetryable = errors.New("retryable")
	ErrPermanent = errors.New("permanent")
)
```

> ⚠️ **`ErrNotEnoughReplicas` 到底是可重试还是不可重试？** 语义上可重试（等 ISR 恢复），但如果 ISR 长期不足，重试只会把消息堆在客户端内存里。正确做法：可重试 + 有上限 + 触发「ISR 不足」告警，并让上游进入背压（拒绝新请求），而不是无限缓冲。

### 第三步：消费端——先处理后提交，还是先提交后处理

| 顺序 | 崩溃时 | 语义 | 适合 | 代价 |
| --- | --- | --- | --- | --- |
| **先处理后提交**（推荐） | 重复（已处理未提交） | 至少一次 | 99% 的业务 | 必须幂等（第 24 章） |
| **先提交后处理** | **丢** | 至多一次 | 可丢数据：埋点、日志、监控 | 一旦崩溃/异常，消息永久消失 |
| 先处理后提交 + 异步线程池 | **丢 + 乱序** | 实际上是至多一次 | 任何场景都不适合 | 事故二：位点已提交，任务在内存里 |
| 批量处理后批量提交 | 整批重复 | 至少一次 | 高吞吐、每条都幂等 | 批内一条失败 → 全批重放 |
| 提交前写业务库 + 提交位点 | 重复 | 至少一次 | 与上面等价，但更明确 | 位点提交失败 → 重复量 = 未提交条数 |

**Java：正确的「先处理后提交」，且处理必须同步在消费线程里**

```java
@KafkaListener(topics = "pay.success", groupId = "point-service",
               concurrency = "6")   // = 分区数，分区内串行
public void onMessage(ConsumerRecord<String, String> rec, Acknowledgment ack) {
    // ❌ 错误写法（事故二）：ack.acknowledge(); executor.submit(...)
    //    位点前进了，任务还在内存队列里，OOM 就永久丢

    try {
        PaySuccessEvent e = Json.read(rec.value(), PaySuccessEvent.class);
        // 幂等 + 业务落库在同一本地事务里（第 24 章的去重表）
        txTemplate.executeWithoutResult(st -> {
            int inserted = jdbc.update("""
                INSERT INTO consume_dedup(consumer_group, idem_key, topic, partition_no, offset_no, status)
                VALUES (?, ?, ?, ?, ?, 2) ON DUPLICATE KEY UPDATE id = id
                """, "point-service", e.msgId(), rec.topic(), rec.partition(), rec.offset());
            if (inserted == 0) return;            // 重复：直接当成功
            jdbc.update("UPDATE point_account SET balance = balance + ? WHERE user_id = ?",
                        e.points(), e.userId());
            jdbc.update("INSERT INTO point_ledger(msg_id, user_id, amount) VALUES (?,?,?)",
                        e.msgId(), e.userId(), e.points());
        });
        ack.acknowledge();                        // 只有事务提交成功才提交位点
    } catch (RetryableException ex) {
        // 不 ack：交给容器重投（会阻塞该分区，见第 25 章）
        throw ex;
    } catch (Exception ex) {
        // 不可重试：进 DLQ（§26.4 第四步），并且必须 ack，否则永远卡住
        dlq.send(rec, ex);
        ack.acknowledge();
    }
}
```

**Go：Sarama 的 `MarkMessage` 只是「标记」，真正提交在 `session` 结束时批量发生**

```go
func (h *Handler) ConsumeClaim(sess sarama.ConsumerGroupSession, claim sarama.ConsumerGroupClaim) error {
	for msg := range claim.Messages() {
		// ❌ 错误：sess.MarkMessage(msg, "") 之后再开 goroutine 处理
		// Sarama 会在下一次 Commit 时提交这个位点，业务还没跑
		if err := h.handle(sess.Context(), msg); err != nil {
			// 不 Mark，返回错误让该分区重新分配；或按错误类型进 DLQ 后 Mark
			if errors.Is(err, ErrPermanent) {
				if derr := h.dlq.Send(msg, err); derr != nil {
					return derr // DLQ 也失败：千万别 Mark，让消息重投
				}
				sess.MarkMessage(msg, "")
				continue
			}
			return err
		}
		sess.MarkMessage(msg, "") // 只有业务成功后标记
	}
	return nil
}
```

**`__consumer_offsets` 到底存了什么**（不懂这个，你就不知道「位点提交」到底改了什么）：

```
Topic: __consumer_offsets（内部 topic，默认 50 分区，cleanup.policy=compact，RF=3）

Key（OffsetCommitKey，按 3.x 的协议版本，字段顺序以 kafka.apache.org/protocol 为准）:
  version        : int16   (0/1)
  group          : string
  topic          : string
  partition      : int32
Value（OffsetCommitValue）:
  version        : int16
  offset         : int64   ← 这就是「已提交位点」，消费者重启后从这里继续
  metadata       : string  （客户端自定义，通常空）
  commit_timestamp: int64  （毫秒）
  expire_timestamp: int64  （-1 表示不过期；老版本用于删除过期位点）

分区规则：partition = abs(group.hashCode()) % 50
  → 同一个 group 的所有位点都落在同一个 __consumer_offsets 分区
  → 这个分区不可用时，该 group 无法提交位点（会看到 CommitFailedException）

同一 topic 还有 GroupMetadata 消息（key = version + group，value = generation/protocol/leader/members），
这就是 rebalance 的协调状态（第 25 章）。
```

```bash
# 直接读位点 topic，验证「提交的到底是什么」（只读，别在生产随便灌）
kafka-console-consumer.sh --bootstrap-server broker1:9092 \
  --topic __consumer_offsets --formatter 'kafka.coordinator.group.GroupMetadataManager$OffsetsMessageFormatter' \
  --from-beginning --max-messages 20 | grep point-service

# 输出长这样（offset=124,533 就是已提交位点，不是「正在处理」的位置）
# [point-service,pay.success,2]::[OffsetMetadata[124533,NO_METADATA],CommitTime 1747031234567,ExpirationTime -1]
```

### 第四步：DLQ 与重试队列（完整表结构 + 消费代码 + 退避）

**三层结构**：原 Topic → 重试 Topic（多级延迟）→ 死信表（人工/自动处理）。**不要用「原地重试 16 次」代替分层**：原地重试会阻塞分区（第 25 章），而且第 16 次失败时你已经不知道该找谁。

**退避策略**（按「失败原因」分，不按「次数」一刀切）：

| 第几次 | 延迟 | 适用错误 | 累计耗时 | 备注 |
| --- | --- | --- | --- | --- |
| 1 | 1s | 网络抖动、下游 5xx | 1s | 抖动 ±20%，避免同步重试 |
| 2 | 5s | 下游短暂不可用 | 6s | |
| 3 | 30s | 下游重启/主从切换 | 36s | 此时应已有熔断（第 18 章） |
| 4 | 5m | 依赖恢复中 | ~6m | 转「低频重试 Topic」 |
| 5 | 30m | 数据问题待修复 | ~36m | 通知 owner |
| 6 | 2h | 最后机会 | ~2.6h | 之后进死信，**必须有人处理** |
| >6 | —— | —— | —— | 进 `dlq_message`，状态 `PENDING`，进值班工单 |

```sql
-- 统一重试/死信表：既能当重试队列（DB 轮询），也能当死信账本
CREATE TABLE mq_retry_task (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  msg_id         VARCHAR(64)   NOT NULL COMMENT '原消息 msgId（生产端生成，全局唯一）',
  consumer_group VARCHAR(64)   NOT NULL,
  src_topic      VARCHAR(128)  NOT NULL,
  src_partition  INT           NOT NULL,
  src_offset     BIGINT        NOT NULL,
  msg_key        VARCHAR(128)  NOT NULL COMMENT '分区键，重试时要回到同一 key 的语义',
  payload        MEDIUMTEXT    NOT NULL COMMENT '原始消息体（大消息存对象存储 + 这里存引用）',
  headers        JSON          NULL     COMMENT '保留 trace_id、retry_count、first_fail_at',
  error_class    VARCHAR(64)   NOT NULL COMMENT 'RETRYABLE / PERMANENT / UNKNOWN',
  last_error     VARCHAR(1000) NOT NULL DEFAULT '',
  retry_count    INT           NOT NULL DEFAULT 0,
  next_retry_at  DATETIME(3)   NOT NULL,
  status         TINYINT       NOT NULL DEFAULT 0 COMMENT '0待重试 1处理中 2成功 3死信',
  locked_until   DATETIME(3)   NULL     COMMENT '抢占租约：防多实例重复处理',
  owner          VARCHAR(64)   NOT NULL DEFAULT '' COMMENT '死信责任人，必填才允许转死信',
  created_at     DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at     DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_msg_group (msg_id, consumer_group),
  KEY idx_due (status, next_retry_at),
  KEY idx_group_status (consumer_group, status),
  KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='重试队列 + 死信账本';

-- 死信单独一张表：生命周期比重试长，且需要人工处置字段
CREATE TABLE mq_dlq_message (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  msg_id         VARCHAR(64)   NOT NULL,
  consumer_group VARCHAR(64)   NOT NULL,
  src_topic      VARCHAR(128)  NOT NULL,
  src_partition  INT           NOT NULL,
  src_offset     BIGINT        NOT NULL,
  msg_key        VARCHAR(128)  NOT NULL,
  payload_ref    VARCHAR(512)  NOT NULL COMMENT '对象存储引用，避免大消息进 DB',
  error_class    VARCHAR(64)   NOT NULL,
  last_error     VARCHAR(2000) NOT NULL,
  retry_count    INT           NOT NULL,
  first_fail_at  DATETIME(3)   NOT NULL,
  status         TINYINT       NOT NULL DEFAULT 0 COMMENT '0待处理 1处理中 2已重放 3已丢弃(需审批)',
  handler        VARCHAR(64)   NOT NULL DEFAULT '',
  handle_note    VARCHAR(512)  NOT NULL DEFAULT '',
  handled_at     DATETIME(3)   NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_msg_group (msg_id, consumer_group),
  KEY idx_status_fail (status, first_fail_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='死信：每一条都必须有归宿';
```

```java
// 重试调度器：抢占 + 退避 + 超限转死信。多实例安全靠 locked_until 租约（不是分布式锁）
@Scheduled(fixedDelay = 1000)
public void pollAndRetry() {
    List<RetryTask> due = jdbc.query("""
        SELECT * FROM mq_retry_task
         WHERE status = 0 AND next_retry_at <= NOW(3)
         ORDER BY next_retry_at LIMIT 100
         FOR UPDATE SKIP LOCKED
        """, retryMapper);
    for (RetryTask t : due) {
        // 抢占：谁先更新成功谁处理；locked_until 防止处理中崩溃导致永久「处理中」
        int claimed = jdbc.update("""
            UPDATE mq_retry_task
               SET status = 1, locked_until = DATE_ADD(NOW(3), INTERVAL 60 SECOND)
             WHERE id = ? AND status = 0
            """, t.id());
        if (claimed == 0) continue;
        try {
            handlerFor(t.consumerGroup()).handle(t);          // 业务处理器（内部幂等）
            jdbc.update("UPDATE mq_retry_task SET status=2, updated_at=NOW(3) WHERE id=?", t.id());
        } catch (Exception ex) {
            int next = t.retryCount() + 1;
            if (next > 6) {
                jdbc.update("""
                    INSERT INTO mq_dlq_message
                      (msg_id, consumer_group, src_topic, src_partition, src_offset, msg_key,
                       payload_ref, error_class, last_error, retry_count, first_fail_at, status)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,0)
                    ON DUPLICATE KEY UPDATE last_error = VALUES(last_error)
                    """, t.msgId(), t.consumerGroup(), t.srcTopic(), t.srcPartition(),
                        t.srcOffset(), t.msgKey(), refOf(t), "PERMANENT",
                        truncate(ex.toString(), 2000), next, t.createdAt());
                jdbc.update("UPDATE mq_retry_task SET status=3 WHERE id=?", t.id());
            } else {
                long delayMs = backoffMs(next);                 // 1s/5s/30s/5m/30m/2h
                jdbc.update("""
                    UPDATE mq_retry_task
                       SET status=0, retry_count=?, last_error=?,
                           locked_until=NULL,
                           next_retry_at=DATE_ADD(NOW(3), INTERVAL ? MICROSECOND)
                     WHERE id=?
                    """, next, truncate(ex.toString(), 1000), delayMs * 1000, t.id());
            }
        }
    }
}

private static long backoffMs(int attempt) {
    long[] base = {1000, 5000, 30000, 300000, 1800000, 7200000};
    long d = base[Math.min(attempt - 1, base.length - 1)];
    return (long) (d * (0.8 + Math.random() * 0.4));           // 抖动：防同步重试风暴
}
```

**为什么用 `locked_until` 而不是 Redis 分布式锁**：重试任务是「表里的行」，抢占和状态更新可以在**同一个 `UPDATE`** 里完成，天然幂等且不留锁泄漏。`FOR UPDATE SKIP LOCKED`（MySQL 8.0+）让多实例并行拉取不互相阻塞。Redis 锁在这里只增加一个故障源。

**死信必须有人处理，否则 DLQ 就是「高级垃圾桶」**：

```sql
-- 每日巡检：死信无人认领 / 超过 24 小时未处理
SELECT consumer_group, status, COUNT(*) AS cnt, MIN(first_fail_at) AS oldest
  FROM mq_dlq_message
 WHERE first_fail_at >= NOW() - INTERVAL 7 DAY
 GROUP BY consumer_group, status
HAVING oldest < NOW() - INTERVAL 24 HOUR
 ORDER BY cnt DESC;

-- 死信重放（必须有审批：status 从 0 → 1，handler 记录操作人）
UPDATE mq_dlq_message
   SET status = 1, handler = 'zhangsan', handle_note = 'INC-20250703-002 修复后重放'
 WHERE id IN (?) AND status = 0;
```

### 第五步：消息丢失的八个环节清单（每环给检查方法）

| # | 环节 | 丢的形态 | 怎么发现（可执行） | 典型根因 |
| --- | --- | --- | --- | --- |
| ① | 业务未发出 | 业务提交了但 `send()` 没执行 | 对账：业务表有条数、`outbox_message` 无记录 | 事务回滚后发、异常被 `catch` 吞、`@Async` 里抛错没人看 |
| ② | 客户端校验拒绝 | `RecordTooLargeException`、序列化失败 | `grep -c RecordTooLarge` 生产日志；DLQ 里 `PERMANENT` | 消息体超 `max.message.bytes`（第 27 章） |
| ③ | 未到达 broker | 重试耗尽、缓冲区满 | 生产端 `record-error-rate`、`buffer-exhausted-rate` | `delivery.timeout.ms` 太短、`buffer.memory` 太小 |
| ④ | broker 未落盘/被截断 | 已 ack 但 HW 回退 | 对比生产 offset 与 broker 最早可读 offset；看 `log truncation` 日志 | `acks=1`、unclean 选举（事故一） |
| ⑤ | ISR 不足写失败 | `NotEnoughReplicasException` | `kafka-topics --describe` 看 ISR 数；`UnderReplicatedPartitions` 指标 | `min.insync.replicas=2` 但副本长期不足 |
| ⑥ | 过期/磁盘满 | 消息被 retention 删、broker 只读 | `kafka-log-dirs.sh --describe` 看磁盘；`retention.ms` 与实际消费延迟对比 | 消费停摆超过 retention；磁盘 100% |
| ⑦ | 消费端未处理 | lag 正常但业务没数据 | 对账 SQL：`consume_dedup` 有、业务表无 | 先提交后处理（事故二）、异常被吞、反序列化失败跳过 |
| ⑧ | 业务落库失败但位点已提交 | 部分成功 | 对账：`consume_dedup` status=2 但业务行缺失 | 去重表与业务不同库、事务传播错误（第 24 章） |

**一条 SQL 把 ⑦⑧ 抓出来**（每天跑，比任何监控都灵）：

```sql
-- 「消费侧声称完成」vs「业务侧真的落了」的差异
SELECT d.consumer_group,
       DATE_FORMAT(d.created_at, '%Y-%m-%d %H') AS h,
       COUNT(*) AS claimed_done,
       SUM(CASE WHEN p.id IS NULL THEN 1 ELSE 0 END) AS missing_in_biz
  FROM consume_dedup d
  LEFT JOIN point_ledger p ON p.msg_id = d.idem_key
 WHERE d.status = 2
   AND d.created_at >= NOW() - INTERVAL 2 DAY
 GROUP BY d.consumer_group, h
HAVING missing_in_biz > 0
 ORDER BY h DESC;
```

### 第六步：Agent 侧——长任务用检查点，不要重投

传统消息重投的代价是「再跑一次幂等逻辑」，几毫秒。Agent 的一条消息可能对应一个 **45 秒、烧 3 万 token 的 Run**：重投 = 从第 1 步重跑 = **前面 20 万 token 白花 + 工具副作用重放**。所以 Agent 的可靠性设计与传统有本质差异：

| 场景 | 传统做法 | Agent 做法 | 为什么 |
| --- | --- | --- | --- |
| 消费失败重试 | 消息重投 | **从最近检查点续跑**（`run_checkpoint` 表） | 重投 = 重跑 = 重复花钱 |
| 幂等粒度 | 一条消息一个幂等键 | **一个副作用一个幂等键**（`tool_call_id` + `args_hash`） | 同一步里 3 个工具，只有发邮件需要去重 |
| 消息体 | 1~10KB | 10~200KB（含上下文片段） | 更容易撞大小上限（第 27 章） |
| 位点提交 | 业务落库后提交 | **Step 事件落库 + 检查点写入后提交** | 真相源是 `run_step` 表，不是消息 |
| 观测 | 生产/消费条数对账 | 加 **token 对账**：`run_step` 记录的 token vs 账单 | 重复 Step 在账单上看起来「正常」 |

```sql
-- 检查点表：Run 的续跑点。每条 Step 完成后写一次，重试时从这里恢复
CREATE TABLE run_checkpoint (
  run_id        VARCHAR(64)  NOT NULL,
  step_seq      INT          NOT NULL COMMENT '已完成的最大步序号',
  state_json    MEDIUMTEXT   NOT NULL COMMENT '恢复所需的最小状态（不要存全量上下文）',
  tokens_used   BIGINT       NOT NULL DEFAULT 0,
  cost_micro    BIGINT       NOT NULL DEFAULT 0 COMMENT '微元，避免浮点误差',
  updated_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (run_id),
  KEY idx_updated (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Run 检查点：续跑而不是重投';

-- 重试前先查检查点：有就从 seq+1 继续，没有才从头开始（并且要重新审批工具调用）
SELECT run_id, step_seq, tokens_used, cost_micro
  FROM run_checkpoint WHERE run_id = ?;
```

---

## 26.5 双修对照：重投一次，一边几毫秒，一边几块钱

| 维度 | 传统系统 | Agent 系统 | 同 or 异 |
| --- | --- | --- | --- |
| **机制本体** | `acks`/ISR/HW + 先处理后提交 + DLQ | **完全相同**：同一套八环节、同一套位点语义 | **同（可直接复用）** |
| **身份 / 唯一键** | `msg_id`（生产端生成）+ `(group, key)` | `msg_id` + `(run_id, step_seq)`；副作用再叠 `(tool_name, args_hash)` | 同构，键多一维 |
| **失败模式** | 丢、重、乱序、堆积 | 同上 + **重复 Step = 重复调模型 = 重复花钱**；上下文丢失不可重算 | 后果升级 |
| **恢复方式** | 重投消息、从位点继续 | **从 `run_checkpoint` 续跑**；重投整条 Run 等于重跑 | **根本差异**（长事务 + 随时中断） |
| **恢复成本** | 几毫秒 CPU | 一次失败重跑可能 **0.3~3 元**（3 万 token）；100 次/天 = 月增千元级 | 成本结构不同 |
| **验证方法** | 生产条数 vs 落地条数 | 同 + **token 对账**（`run_step.tokens` vs 账单） | 框架相同，多一笔账 |
| **观测指标** | `record-error-rate`、lag、DLQ 水位 | 加 `checkpoint_age`、`wasted_tokens_total`、`retry_cost_total` | 指标集扩展 |
| **稀缺资源** | 分区、连接、磁盘 | **TPM、模型并发、单 Run 预算** | 限的对象不同 |
| **不可回滚** | 已发的短信/已扣的钱 | 每一次工具调用都是外部副作用，且「要不要调」是模型决定的 | 同一条定律，Agent 放大一万倍 |

**可以直接复用的（一套代码两边用）**：Outbox 表 + 投递器、`acks=all + min.insync=2 + unclean=false` 三件套、先处理后提交的模板、`mq_retry_task` 表与退避算法、八环节检查清单。

**只有三样必须重算**：

1. **「重试」的单位从「消息」变成「Step」**。传统重投一条消息 = 重跑一个幂等操作；Agent 重投一条 Step 事件，如果处理逻辑是「重新调模型」，那就是重复花钱。所以 Agent 的消费者必须先查 `run_checkpoint`，**有检查点就续跑，没有才重跑**。
2. **DLQ 的处置从「重放」变成「重放 + 成本审计」**。传统 DLQ 重放只是「再处理一次」；Agent 的 DLQ 重放要先算「这一次要花多少」，并检查「这个 Step 的副作用是否已经发生过」（`tool_call_id` 去重），否则重放会重复发邮件、重复下单。
3. **可靠性预算里要加「重试成本」**。传统系统重试成本可忽略；Agent 的重试成本要进 SLO（第 21 章的成本维度）：`重试成本占比 = 重试消耗 token / 总 token`，超过 5% 就说明你的失败率或幂等设计有问题。

> 三定律检验：**状态机**——真相源在 `run_step`/`run_checkpoint`，消息只是通知；**稀缺资源**——传统缺分区与磁盘，Agent 缺 TPM 与预算；**不可回滚**——已调用的工具只能补偿，所以幂等键必须在**调用前**生效。

---

## 26.6 会在哪炸：失效模式清单

| # | 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- | --- |
| 1 | 已 ack 的消息 2 小时后消失 | `acks=1` + ISR 缩到 1 + unclean 选举 | 看 broker `log truncation` 日志与 HW 回退 | 补发 + 对账；改 `acks=all`、`min.insync=2`、关 unclean | ISR 数 < RF 即告警；配置进基线扫描 |
| 2 | lag=0 但业务少 12,000 条 | 先提交后处理 + 内存队列 OOM | 对账 SQL（第七步）；看堆 dump 里的队列 | 补数据；改同步处理 | Code review 禁止「ack 后异步」 |
| 3 | 生产端日志全是「发送成功」，broker 没有 | 只判断了 `send()` 返回，没看回调 | 对比生产 offset 与 broker offset | 用带回调的发送 + Outbox | 发送必须由回调/同步结果决定状态 |
| 4 | 重试把消息发了 5 遍 | 没开幂等生产者 + 重试 | 按 `msg_id` 统计重复次数 | 开 `enable.idempotence` + 消费幂等 | `msg_id` 唯一索引；重试复用原 key |
| 5 | DLQ 堆了 8 万条没人管 | DLQ 没有 owner、没有告警 | 查 `mq_dlq_message` 的 oldest | 建工单 + 定 owner | 死信 > 100 条/小时即告警；owner 必填 |
| 6 | 重试任务被两个实例同时处理 | 用「先查后改」而不是租约抢占 | 看业务表重复行的时间戳 | 改 `UPDATE ... WHERE status=0` 原子抢占 | `locked_until` + 唯一索引 |
| 7 | 消息过期被删，消费端还在追 | 消费停摆时间 > `retention.ms` | `kafka-consumer-groups --describe` 看 lag 对应的时间 | 扩消费/重置位点（已丢的靠对账补） | lag 换算成「小时」告警，而不是条数 |
| 8 | Agent 重试一次花掉 3 元 | 重投整条 Run，没有检查点 | 查 `run_step` 重复 `seq` | 改检查点续跑 | `(run_id, step_seq)` 唯一索引；重试前查 checkpoint |
| 9 | 死信重放又重复发了邮件 | 重放没做副作用去重 | 按 `tool_call_id` 查发送记录 | 补去重；人工撤回 | 副作用幂等键必须独立于消息 `msg_id` |

---

## 26.7 怎么验证：kill -9、断网、对账三件套

**① 生产端可靠性压测**（验证 `acks=all` 的真实代价与成功率）：

```bash
# 100 万条 8KB 消息，acks=all + 幂等，看 P99 与错误率
kafka-producer-perf-test.sh --topic pay.success \
  --num-records 1000000 --record-size 8192 --throughput -1 \
  --producer-props bootstrap.servers=broker1:9092 \
    acks=all enable.idempotence=true linger.ms=10 compression.type=zstd \
    delivery.timeout.ms=120000
# 期望：记录发送失败为 0；若出现 NotEnoughReplicas，说明 ISR 不足（这是好事：它拒绝写入而不是丢）
```

**② 故障注入：杀 leader，验证不丢**

```bash
# 记录当前每个分区的 (leader, HW)，然后 kill leader 所在 broker
kafka-topics.sh --bootstrap-server broker1:9092 --describe --topic pay.success
# 用 kill -9 而不是优雅停止：优雅停止会触发 controlled shutdown，测不出问题
ssh broker3 'sudo kill -9 $(pgrep -f kafka.Kafka)'
# 恢复后校验：最早可读 offset 是否回退（回退 = 丢）
kafka-run-class.sh kafka.tools.GetOffsetShell --bootstrap-server broker1:9092 \
  --topic pay.success --time -2
```

**③ 消费端「崩溃在提交前/后」两个用例**：

```bash
# 用例 A：处理到第 50 条时 kill -9，重启后应重复但业务只生效一次（幂等生效）
kill -9 $(pgrep -f point-service)

# 用例 B：人为让 commit 超时（tc 注入 3s 延迟），验证会重复而不是丢
sudo tc qdisc add dev eth0 root netem delay 3000ms
# 校验：业务表按 msg_id 分组不得有重复行
mysql -e "SELECT msg_id, COUNT(*) c FROM point_ledger GROUP BY msg_id HAVING c > 1 LIMIT 10"
```

**④ 八环节对账（每天 02:00 跑，产出「丢失清单」而不是一个数字）**：

```sql
-- 逐小时：生产 vs Outbox vs 消费 vs 业务落地，四段对齐
SELECT DATE_FORMAT(o.created_at, '%Y-%m-%d %H') AS h,
       COUNT(*)                                              AS biz_rows,
       SUM(CASE WHEN ob.msg_id IS NULL THEN 1 ELSE 0 END)    AS no_outbox,
       SUM(CASE WHEN d.idem_key IS NULL THEN 1 ELSE 0 END)   AS not_consumed,
       SUM(CASE WHEN p.id IS NULL THEN 1 ELSE 0 END)         AS consumed_not_landed
  FROM order_info o
  LEFT JOIN outbox_message ob ON ob.msg_id = CONCAT('PAY:', o.order_id)
  LEFT JOIN consume_dedup  d  ON d.idem_key = CONCAT('PAY:', o.order_id) AND d.consumer_group='point-service'
  LEFT JOIN point_ledger   p  ON p.msg_id  = CONCAT('PAY:', o.order_id)
 WHERE o.created_at >= NOW() - INTERVAL 24 HOUR
 GROUP BY h
HAVING no_outbox + not_consumed + consumed_not_landed > 0
 ORDER BY h DESC;
```

**⑤ 四个必看指标**：`record-error-rate`（生产端）、`UnderReplicatedPartitions`（ISR 不足的分区数）、`consumer lag 换算的小时数`、`dlq_pending`（死信待处理数）。第四个必须进值班看板，否则 DLQ 一定会变成垃圾场。

---

## 26.8 用什么工具：可靠性这一层，配置占七成

| 能力 | 开源/框架够用 | 必须自己写 |
| --- | --- | --- |
| 生产确认与重试 | Kafka producer / RocketMQ producer | 错误分类（可重试 vs 永久）、退避与上限 |
| 不丢的三件套 | `acks`/`min.insync.replicas`/`unclean` 配置 | 基线扫描与告警（配错没人发现 = 没配） |
| 幂等生产 | `enable.idempotence` / RocketMQ 事务消息 | 业务 `msg_id` 的生成与透传 |
| 位点管理 | 客户端 API、`__consumer_offsets` | 提交时机、连续水位、revoke 时同步提交 |
| 重试与 DLQ | 各 MQ 的重试 Topic（RocketMQ `%RETRY%`）、Kafka 需自建 | 退避曲线、`mq_retry_task`、死信 owner 与工单 |
| 对账 | —— | 八环节四段对账 SQL（没人能替你做） |
| Agent 检查点 | Temporal 等持久化执行引擎可提供 | `run_checkpoint` 的粒度与「恢复时需要什么状态」 |

**判断口径**：MQ 只保证 ③④⑤⑥ 四环，① ② ⑦ ⑧ 全在你的代码里。**买 Kafka 不会让你变可靠，只是让你有能力做到可靠。**

---

## 26.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 可靠性公式 | 八环节连乘；`0.9999^8 ≈ 99.92%`，八环都要做，缺一环就是 0 |
| ack 的真实语义 | 「当前 ISR 都写进 page cache」，不是「磁盘有了」 |
| 三件套 | `acks=all` + `min.insync.replicas=2` + `unclean.leader.election=false`，缺一不可 |
| LEO / HW / ISR | LEO 是副本末尾、HW 是 ISR 最小 LEO、ISR 是同步中的副本集合；消费者只能读到 HW |
| 幂等生产 | PID + 分区内 sequence，防「重试产生重复」；跨 producer 重启失效 |
| 消费顺序 | 先处理后提交 = 至少一次（要幂等）；先提交后处理 = 至多一次（会丢，事故二） |
| 位点存在哪 | `__consumer_offsets`，key = version+group+topic+partition，value 里那个 int64 才是「已提交位点」 |
| 重试策略 | 1s/5s/30s/5m/30m/2h，带 ±20% 抖动，6 次后进死信 |
| 抢占方式 | `UPDATE ... WHERE status=0` + `locked_until` 租约，别用 Redis 锁 |
| 死信铁律 | 每条死信必须有 owner 与状态；DLQ 增长 >100/小时即告警 |
| 八环节 | ①发出 ②校验 ③到达 ④落盘 ⑤ISR ⑥过期 ⑦消费 ⑧落库；每环都有检查方法 |
| Agent 侧 | 重投 = 重跑 = 重花钱；用 `run_checkpoint` 续跑，副作用按 `tool_call_id` 去重 |
| 一句话 | **可靠性不是「消息不丢」，是「业务一定发生」——中间那八环，每一环都要能自证。** |

## 26.10 自检题

1. **你们配了 `acks=all`，3 副本，但 `min.insync.replicas=1`、`unclean.leader.election.enable=true`。一次磁盘故障后丢了 3,400 条已 ack 的消息，请完整解释丢失链条，并给出三项修复。**
   > 要点：链条是「`acks=all` 只等当前 ISR → ISR 因 follower 落后缩到只剩 leader（`min.insync=1` 允许写入） → leader 宕机 → unclean 选举选了落后的 follower → 新 HW = 它的 LEO → 3,400 条被 truncate」。修复：① `min.insync.replicas=2`（ISR < 2 时写入失败而不是静默降级）；② `unclean.leader.election.enable=false`（宁可不可用也不截断）；③ 加 `UnderReplicatedPartitions` 告警，ISR 收缩必须在 1 分钟内被发现。补第四项：确认 `default.replication.factor=3` 且 Topic 真的是 3 副本。

2. **消费端为了「提高吞吐」，改成「先 ack 再丢进线程池处理」。请说明它在哪三种情况下会丢消息，以及为什么「lag=0」不能证明没丢。**
   > 要点：① 进程 OOM/被 kill，内存队列里的任务消失；② 线程池队列满后 `RejectedExecutionHandler` 静默丢弃（默认 `AbortPolicy` 会抛，但异常在提交线程之外可能被吞）；③ 处理抛异常但位点已提交，重试机制再也看不到这条消息。`lag=0` 只说明「位点追上了 LEO」，而位点是你自己提交的——**你可以提交一个「什么都没处理」的位点**。正确做法：同步处理 + 处理成功后提交，或用「提交位点前先持久化任务」（DB/另一个 Topic），而不是内存队列。

3. **Agent 的一个 Run 有 8 步，第 6 步调外部工具超时。你会「重投这条消息」还是「从检查点续跑」？分别算一下代价。**
   > 要点：重投整条 Run = 从第 1 步重跑，前 5 步的模型调用全部重花，按每步 2,500 token、单价混合 0.03 元/千 token 算，约 `5×2,500×0.03/1000 ≈ 0.375 元`（还不含工具副作用的补偿成本）；续跑 = 只重试第 6 步，约 0.075 元。差了 5 倍，且重投还会重复触发第 1~5 步的工具副作用（发邮件/查库/下单）。所以：**必须从 `run_checkpoint(step_seq=5)` 续跑**，并在续跑前检查第 6 步的 `tool_call_id` 是否已经产生过副作用（可能超时但已执行）。

## 26.11 延伸阅读

- Kafka 协议与位点：`__consumer_offsets` 的 `OffsetCommitKey/Value` 结构见 kafka.apache.org/protocol 的「Offset Commit」一节；`GroupMetadataManager` 源码里 `storeOffsets` 是写入入口
- ISR 与 HW 的权威说明：Kafka 设计文档「Replication」章节（`high watermark` / `leader epoch` / `log truncation`），以及 KIP-101（Leader Epoch 解决截断误判）
- 幂等生产者：KIP-98（Exactly Once Delivery），`ProducerStateManager` 保留每个 PID 最近 5 个 batch 的 sequence
- RocketMQ 重试与死信：`%RETRY%<group>` 与 `%DLQ%<group>`，`maxReconsumeTimes` 默认 16（并发消费），以 4.9/5.x 文档为准
- 投递语义与重复消费：[第 11 章](11-exactly-once-truth.md)、[第 24 章](24-delivery-semantics.md)；顺序与堆积：[第 25 章](25-ordering-and-lag.md)
- Outbox 与事务消息：[第 10 章](10-outbox-and-transactional-message.md)（本章只引用，不重复）
- 排查路径（消息到底去哪了）：[第 27 章](27-message-troubleshooting.md)；延迟重试与 CDC：[第 28 章](28-delayed-queue-cdc.md)
- Agent 侧检查点与长任务恢复：见 [../llm/12-long-task-durable.md](../llm/12-long-task-durable.md)；Run/Step 事件模型：见 [../llm/6-session-run-event-model.md](../llm/6-session-run-event-model.md)
- 总纲：[0-summary.md](0-summary.md) ｜ 语言与运行时：[../go/0-summary.md](../go/0-summary.md) ｜ 平台治理：[../llm/0-summary.md](../llm/0-summary.md)
- 前置：[第 25 章](25-ordering-and-lag.md) ｜ 下一章：[第 27 章 线上排查：消息去哪了](27-message-troubleshooting.md)
