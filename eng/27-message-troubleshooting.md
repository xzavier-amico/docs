# 27. 线上排查：消息去哪了

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 异步与消息 ｜ **编号**：27 / 52
> **本章定位**：排查不是「凭经验猜」，而是一条**固定的五段路径**：生产端 sendId → broker 按 key/offset/时间 → 消费位点与堆积 → 线程栈 → 业务库落地。每段都有唯一 ID 和一条命令，走完五段必然定位到「消息死在哪一段」。
> **读法**：§27.3 那张路径图贴在值班屏幕上；§27.4 第二步的五组命令直接抄；§27.4 第三步的六个故障是「照着做就能复现的现场」；两个高频坑（§27.4 第四、五步）能解释你 60% 的工单。

---

## 27.1 引子：同一类工单，4 小时与 12 分钟的差别

**事故一（2025-09-08，某电商「支付成功没发货」）**。09:12 客服转来第一条工单，到 13:26 定位，用时 **4 小时 14 分**。

- **09:12** 工单：「客户说付款成功，订单还是待支付」。
- **09:20** 值班同学第一反应：**重启消费者**（这是本章要反复批判的动作）。重启后 lag=0，看起来「恢复了」。
- **09:35** 又来了 30 条同类工单。此时他做第二件事：**在群里问**「谁动了发货服务？」
- **10:50** 有人提议「查一下 MQ」，于是他跑 `kafka-console-consumer.sh --from-beginning` 在**生产** Topic 上拉全量，把 broker 的网络和磁盘 IO 打满，**新增 2,100 条消息积压**，故障面扩大。
- **12:10** 才开始按 `order_id` 去查 broker，发现消息**根本不在 Topic 里**。
- **13:26** 在生产端日志里找到 `TimeoutException: Expiring 1 record(s) for pay.success-2`——消息在客户端就超时被丢弃了，`delivery.timeout.ms=30000`，而 broker 当时在做 preferred leader 选举，耗时 41 秒。
- 影响 **3,400 笔**订单、**1,180 笔**重复支付，客诉 **740 单**，赔付 **11.6 万元**。
- 更贵的是：**4 小时的排查里，有 3 小时 40 分花在「重启」和「问人」上，真正有效的命令只有 5 条。**

**事故二（2025-11-02，同一团队，已建立排查路径）**。同样的症状：客户付款后未发货。

- **10:04** 收到工单，值班同学打开路径图。
- **10:05** 用 `order_id` 查生产端日志 → 找到 `sendId`，状态 `SEND_OK`。
- **10:07** 用 `--partition 5 --offset 8,412,297` 从 broker 取那一条 → **在**。
- **10:09** `--describe` 看消费组 → partition 5 的 `LAG=0`，但 `CONSUMER-ID` 是 `-`。
- **10:12** 发现该分区在 09:58 的 rebalance 后没被分配（实例数 3 × `concurrency=6` = 18 个线程，24 个分区，6 个分区无人消费）。
- **10:16** 定位完成，扩容到 4 实例，10:31 积压清零，补发 640 条。
- **12 分钟定位，27 分钟恢复，影响 640 单。**

> 🩸 两起事故的差距不在技术，而在**顺序**：一个先重启再问人，一个先查 ID 再查 broker。**排查路径的价值是把「灵感」换成「流程」——让一个刚值夜班的同学也能在 15 分钟内走完。**

---

## 27.2 到底是什么：可排查性 = 唯一 ID + 每段留痕

**定义**：可排查性（debuggability）指「给定一个业务单号，能在 15 分钟内回答消息死在哪一段」。它由两个前提构成：

1. **贯穿全链路的唯一 ID**：业务单号（`order_id`）→ 生产端 `sendId`/`msgId` → broker 的 `(topic, partition, offset)` → 消费端 `consumer_group + offset` → 业务表的 `msg_id`。**这条链上任何一环缺失，你就只能靠时间戳猜。**
2. **每一段都有可查记录**：生产端要有「发送结果 + 耗时 + 异常」的日志，broker 要能被按 key/offset/时间检索，消费端要能看位点与线程栈，业务侧要能按 `msg_id` 查到落地记录。

**五个反例**（每个都是真实见过的翻车动作）：

| 反例 | 为什么错 | 不做会怎样 |
| --- | --- | --- |
| 「先重启消费者」 | 重启会清掉现场（线程栈、内存队列、lag 曲线），而且掩盖「重复消费」等问题 | 事故一：重启 3 次，问题依旧，证据没了 |
| 「先问人：谁动了服务」 | 人的记忆比日志慢 30 分钟，而且会引入错误假设 | 一半的排查时间花在等回复 |
| 「lag=0 就是没问题」 | 位点是你自己提交的，可以「提交了但没处理」（第 26 章事故二） | 漏掉最隐蔽的一类丢失 |
| 「`--from-beginning` 拉全量看看到底有没有」 | 全量消费会打满 broker 网络/磁盘，且在生产上制造新的积压 | 事故一：故障面扩大 2,100 条 |
| 「日志里有『发送成功』就是发出去了」 | 很多代码打的是 `send()` 之前/之后的日志，不是回调里的结果 | 你会去 broker 找一个从未到达的消息 |

> 💡 一条判据：**如果你不能在 60 秒内说出「这条消息的 offset 是多少」，你的系统就还不具备可排查性。** 这不是运维问题，是**生产端日志埋点**的设计问题。

---

## 27.3 结构图：五段路径与一张决策树

先看消息在五段里的「身份」怎么变——**排查的本质是「身份转换」：把业务单号换成 offset，再从 offset 换回业务行**。

```
① 业务               ② 生产端                ③ broker               ④ 消费端              ⑤ 业务库
┌──────────┐   ┌──────────────────┐   ┌───────────────────┐   ┌─────────────────┐   ┌──────────────┐
│ order_id │──►│ sendId / msgId    │──►│ topic-partition-  │──►│ group + offset  │──►│ msg_id 落地  │
│ PAY:1234 │   │ + 发送结果/耗时   │   │ offset + timestamp│   │ + consumer-id   │   │ + 业务状态   │
└──────────┘   └──────────────────┘   └───────────────────┘   └─────────────────┘   └──────────────┘
   查订单表        grep sendId            按 key/offset/时间        --describe/线程栈      按 msg_id 查
   ↑ 有            ↑ 有？                  ↑ 在不在？               ↑ 谁在消费？           ↑ 落了没？
```

**排查决策树（按这个顺序走，每一步都有「走不通就去哪」）**：

```
                 业务说「消息没到」
                        │
        ┌───────────────▼────────────────┐
        │ 1. 生产端日志有这条 sendId 吗？  │
        └───────┬───────────────┬────────┘
            没有 │               │ 有（看状态）
                 ▼               ▼
      业务根本没发/异常被吞   状态 = SEND_OK? ──否──► ③ 之前就死了：超时/过大/无权限
      （查 outbox、查 catch）        │是
                                     ▼
        ┌────────────────────────────────────────┐
        │ 2. broker 上有这个 offset 吗？          │
        │    （按 key 查 / 按 offset 取 / 按时间）│
        └───────┬───────────────────────┬────────┘
            不在 │                       │ 在
                 ▼                       ▼
        被 retention 删/写入未落盘   3. 位点是否已过这条？
        （查 earliest offset、       ┌────┴─────┐
         查 log truncation）        已过        未过
                                    │            │
                                    ▼            ▼
                          4. 消费了吗？      还在排队：查 lag、
                          （业务表有吗）     查消费者数/分区数
                                    │
                          ┌─────────┴─────────┐
                        有                  没有
                         │                    │
                    ✅ 已落地         5. 线程卡在哪？（jstack/pprof）
                                     查毒消息、无超时调用、GC
```

**关键判断点只有三个**：`SEND_OK` 之后在不在 broker、位点过没过、业务落了没。**三个「是/否」组合出八种情况，覆盖 95% 的工单。**

---

## 27.4 怎么实现：先把「可查」做出来，再谈排查

### 第一步：地基——日志字段与 trace 串联（没有这层，后面全是猜）

生产端日志必须包含六个字段，缺一个就会在排查时卡住：

| 字段 | 示例 | 缺了会怎样 |
| --- | --- | --- |
| `sendId` | `5f3a-9c21-...`（客户端生成，重试不变） | 无法区分「重试的两条」与「两条业务消息」 |
| `bizKey` | `order_id=1234` | 无法按业务单号检索 |
| `topic/partition/offset` | `pay.success/5/8412297` | 无法直接去 broker 定位那一条 |
| `result` | `SEND_OK` / `TIMEOUT` / `TOO_LARGE` | 「成功」是哪个成功说不清 |
| `latencyMs` | `1207` | 无法判断是「慢」还是「丢」 |
| `traceId` | `4bf92f3577b34da6` | 无法串起生产与消费两端 |

```java
// Java：发送日志必须在回调里打，不能在 send() 前后打
public void send(PaySuccessEvent e) {
    String sendId = SendIds.next(e.orderId());        // 与 msg_id 同源，重试不变
    ProducerRecord<String, String> rec =
        new ProducerRecord<>("pay.success", e.orderId(), Json.write(e));
    rec.headers().add("send-id", sendId.getBytes(StandardCharsets.UTF_8));
    rec.headers().add("trace-id", Trace.currentId().getBytes(StandardCharsets.UTF_8));

    long t0 = System.nanoTime();
    kafka.send(rec).whenComplete((md, ex) -> {
        long ms = (System.nanoTime() - t0) / 1_000_000;
        if (ex == null) {
            log.info("mq.send sendId={} bizKey={} topic={} partition={} offset={} result=SEND_OK latencyMs={} traceId={}",
                     sendId, e.orderId(), md.topic(), md.partition(), md.offset(), ms, Trace.currentId());
        } else {
            // 关键：这里必须能区分「可重试」与「永久失败」，排查时一眼看出死因
            log.error("mq.send.fail sendId={} bizKey={} result={} latencyMs={} cause={}",
                      sendId, e.orderId(), classify(ex), ms, ex.toString());
        }
    });
}
```

```go
// Go：结构化日志 + 同一个 sendId 贯穿重试
func (p *Publisher) Send(ctx context.Context, e PaySuccessEvent) error {
	sendID := e.SendID // 由上游生成，重试复用
	start := time.Now()
	part, off, err := p.p.Publish(ctx, "pay.success", e.OrderID, e.Bytes())
	if err != nil {
		slog.ErrorContext(ctx, "mq.send.fail",
			"sendId", sendID, "bizKey", e.OrderID,
			"result", classify(err), "latencyMs", time.Since(start).Milliseconds(),
			"err", err.Error(), "traceId", trace.From(ctx).ID())
		return err
	}
	slog.InfoContext(ctx, "mq.send",
		"sendId", sendID, "bizKey", e.OrderID, "topic", "pay.success",
		"partition", part, "offset", off, "result", "SEND_OK",
		"latencyMs", time.Since(start).Milliseconds(), "traceId", trace.From(ctx).ID())
	return nil
}
```

> ⚠️ **不要打 `log.info("发送消息: " + json)` 这种**。它没有 `sendId`、没有 offset、没有结果，除了增加磁盘压力毫无用处。排查日志的验收标准是：**「只给一个 order_id，你能 grep 到 offset 吗」。**

### 第二步：五段路径与对应命令

**① 生产端：从 order_id 找 sendId 与结果**

```bash
# 1) 按业务键找发送记录（假设日志已按 §27.4 第一步埋点）
grep 'bizKey=ORDER-20250908-771234' /data/logs/order-service/mq.log | tail -5

# 期望输出（找到这一行就拿到了 offset，跳过所有猜测）
# 2025-09-08 09:12:41.208 INFO mq.send sendId=5f3a9c21 bizKey=ORDER-20250908-771234
#   topic=pay.success partition=5 offset=8412297 result=SEND_OK latencyMs=1207 traceId=4bf92f35

# 2) 找不到「mq.send」，但有「mq.send.fail」→ 消息死在客户端，不用查 broker
grep 'sendId=5f3a9c21' /data/logs/order-service/mq.log
# ERROR mq.send.fail ... result=TIMEOUT latencyMs=30012 cause=Expiring 1 record(s) for pay.success-5

# 3) 完全没有日志 → 业务没发（查 outbox 表与异常日志）
mysql -e "SELECT msg_id,status,retry_count,last_error FROM outbox_message
          WHERE msg_id='PAY:ORDER-20250908-771234'"
```

**② broker：按 key / offset / 时间三种查法**

```bash
# A. 按 offset 精确取那一条（最快，秒级，不产生消费）
kafka-console-consumer.sh --bootstrap-server broker1:9092 \
  --topic pay.success --partition 5 --offset 8412297 --max-messages 1 \
  --property print.offset=true --property print.timestamp=true --property print.key=true
# 输出：CreateTime:1741597961208  offset:8412297  key:ORDER-20250908-771234  value:{"msgId":"PAY:..."}

# B. 按 key 查（不知道 offset 时用；Kafka 没有按 key 索引，只能用 offset 反查 + 逐条扫，
#    生产上建议用「消息轨迹」或把 (key → offset) 写进一张索引表，见 §27.8）
kafka-console-consumer.sh --bootstrap-server broker1:9092 \
  --topic pay.success --from-beginning --max-messages 200000 \
  --property print.key=true | grep -m1 'ORDER-20250908-771234'

# C. 按时间查 offset（用于「某个时间段的都丢了」）
#    先用时间戳换 offset，再按 offset 区间取
kafka-run-class.sh kafka.tools.GetOffsetShell --bootstrap-server broker1:9092 \
  --topic pay.success --time 1741597900000
# 输出：pay.success:5:8412100  （该时间点之后的第一条 offset）

# D. 消息到底还在不在：最早/最晚可读 offset
kafka-run-class.sh kafka.tools.GetOffsetShell --bootstrap-server broker1:9092 \
  --topic pay.success --time -2   # 最早（earliest）
kafka-run-class.sh kafka.tools.GetOffsetShell --bootstrap-server broker1:9092 \
  --topic pay.success --time -1   # 最晚（latest = LEO）
# 若 8412297 < earliest → 已被 retention 删掉（查 retention.ms 与消费停摆时长）

# E. 怀疑「已 ack 但被截断」时，直接读分段日志文件（只在单副本上做，别扫全集群）
kafka-dump-log.sh --files /data/kafka/pay.success-5/00000000000000840000.log \
  --print-data-log --deep-iteration | grep -A2 'offset: 8412297'
```

```bash
# RocketMQ 的对应命令（有原生消息轨迹与索引，比 Kafka 好查）
mqadmin queryMsgByKey -n namesrv1:9876 -t pay_success -k ORDER-20250908-771234
mqadmin queryMsgById  -n namesrv1:9876 -i 7F0000010A2B3C4D5E6F00001234
mqadmin consumerProgress -n namesrv1:9876 -g point-service      # 位点与堆积
mqadmin consumerConnection -n namesrv1:9876 -g point-service    # 谁在消费、订阅了什么
```

**③ 消费端：位点、堆积、谁在消费**

```bash
kafka-consumer-groups.sh --bootstrap-server broker1:9092 \
  --describe --group point-service --members --verbose

# 关键不是 LAG 的大小，而是这三列的「组合」
# GROUP          TOPIC        PART  CURRENT-OFFSET  LOG-END-OFFSET  LAG     CONSUMER-ID       HOST
# point-service  pay.success  5     8,412,300       8,412,300       0       -                 -
# point-service  pay.success  6     8,300,110       8,530,900       230,790 consumer-1-1      /10.0.3.17
# point-service  pay.success  7     8,530,900       8,530,900       0       consumer-1-1      /10.0.3.17

# 判读表（这张表能省你一小时）：
#   LAG=0 且 CONSUMER-ID='-'      → 该分区无人消费：提交过位点但没分配（实例数×concurrency < 分区数）
#   LAG>0 且 CONSUMER-ID='-'      → 分区没分配出去，堆积会一直涨（同上，或 rebalance 卡住）
#   LAG=0 且业务没数据            → 位点提前提交了（第 26 章事故二）
#   LAG 均匀上涨 + 有 consumer    → 消费能力不足（第 25 章）
#   LAG 只在一个分区涨 + 有 consumer → 毒消息卡住
#   LAG 锯齿 + 频繁 rebalance     → max.poll.interval 太小 / GC 停顿
```

**④ 线程栈：消费者卡在哪一行**

```bash
# Java：连采 3 次，间隔 5 秒。一次 jstack 只能看到快照，三次才能区分「卡住」与「慢」
for i in 1 2 3; do
  jcmd $(pgrep -f point-service) Thread.print -l > /tmp/consumer-$i.jstack
  sleep 5
done
grep -A 25 'kafka-coordinator-heartbeat\|ConsumerRecord\|point-service' /tmp/consumer-2.jstack | head -60

# 三种典型栈（认出它们，5 秒定位）：
#   at java.net.SocketInputStream.socketRead0(Native Method)      → 下游 HTTP 无超时（§27.4 故障五）
#   at com.mysql.cj.jdbc.ClientPreparedStatement.executeQuery     → DB 慢查询/锁等待
#   at org.apache.kafka.clients.consumer.internals.ConsumerCoordinator.poll → 卡在 rebalance
#   at java.lang.Object.wait / ReentrantLock.lock                 → 死锁或信号量泄漏

# Go：goroutine 全量栈，找「卡在同一个调用上的大量 goroutine」
go tool pprof -top -nodecount=30 'http://10.0.3.17:6060/debug/pprof/goroutine?debug=1'
curl -s 'http://10.0.3.17:6060/debug/pprof/goroutine?debug=2' > /tmp/goro.txt
grep -c 'net/http.(*Client).do' /tmp/goro.txt    # 数量 = 卡住的下游调用数

# GC 是否在捣乱（停顿 > session.timeout 会被踢出组）
jstat -gcutil $(pgrep -f point-service) 1000 10
# 关注 FGC 次数与 FGCT 累计；单次 Full GC > 5s 就要怀疑它导致 rebalance
```

**⑤ 业务库：落地了没**

```sql
-- 按 msg_id 查三条线：去重表（消费过）、业务表（落地了）、流水表（副作用发生了）
SELECT 'dedup' AS src, consumer_group, status, created_at
  FROM consume_dedup WHERE idem_key = 'PAY:ORDER-20250908-771234'
UNION ALL
SELECT 'ledger', biz_type, CAST(amount AS CHAR), created_at
  FROM point_ledger WHERE msg_id = 'PAY:ORDER-20250908-771234'
UNION ALL
SELECT 'account', CAST(user_id AS CHAR), CAST(balance AS CHAR), updated_at
  FROM point_account
 WHERE user_id = (SELECT user_id FROM order_info WHERE order_id = 'ORDER-20250908-771234');

-- 「消费声称完成但业务没有」——最隐蔽的一类，必须每天跑
SELECT d.idem_key, d.consumer_group, d.created_at
  FROM consume_dedup d
  LEFT JOIN point_ledger p ON p.msg_id = d.idem_key
 WHERE d.status = 2 AND p.id IS NULL
   AND d.created_at >= NOW() - INTERVAL 1 DAY
 LIMIT 50;
```

### 第三步：六个真实故障的全过程

> 下面每一例都是「现象 → 命令与输出 → 根因 → 修复 → 防复发」。**照着这六条走，你能覆盖 90% 的消息工单。**

**故障一：消息「发送成功」但 broker 没有（丢了 3,400 条）**

- **现象**：业务日志有 `SEND_OK`，broker 按 key 查不到；3,400 笔订单卡在待支付。
- **排查**：
  ```bash
  $ grep 'sendId=5f3a9c21' /data/logs/order-service/mq.log
  # ERROR mq.send.fail sendId=5f3a9c21 result=TIMEOUT latencyMs=30012
  #   cause=Expiring 1 record(s) for pay.success-5: 30001 ms has passed since batch creation
  $ kafka-topics.sh --bootstrap-server broker1:9092 --describe --topic pay.success | head -3
  # Topic: pay.success  PartitionCount: 6  ReplicationFactor: 3
  #   Topic: pay.success Partition: 5  Leader: 2  Replicas: 2,1,3  Isr: 2
  ```
  `Isr: 2` 只有 1 个副本 → 当时正在 leader 选举，写入被卡住超过 30s，客户端 `delivery.timeout.ms=30000` 到点丢弃。
- **根因**：`retries` 只配了 3、`delivery.timeout.ms` 只有 30s，而一次 preferred leader 选举 + ISR 追赶用了 41s；客户端把「暂时不可用」当成「永久失败」丢掉了。
- **修复**：`delivery.timeout.ms=120000`、`retries=MAX`、`acks=all`、`min.insync.replicas=2`；同时把「发送失败」写入 `outbox_message` 由投递器重试。
- **防复发**：`record-error-rate` 与 `buffer-exhausted-rate` 进看板；ISR 收缩 1 分钟内告警；**生产端日志必须打 `result`，禁止只打「发送成功」**。

**故障二：重复爆炸（一次滚动发布重复 6.4 万条）**

- **现象**：发布后 18 分钟内，发货单重复创建 21,400 张、短信重复 38,000 条。
- **排查**：
  ```bash
  $ kafka-consumer-groups.sh --bootstrap-server broker1:9092 --describe --group fulfill-service
  # PARTITION  CURRENT-OFFSET  LOG-END-OFFSET  LAG   CONSUMER-ID
  # 3          1,204,336       1,204,336       0     consumer-3-1     ← 已提交
  $ grep -c 'RebalanceInProgress\|Revoking previously assigned' /data/logs/fulfill/app.log
  # 42
  $ grep 'offset=1204300' /data/logs/fulfill/app.log | head -2
  # 14:07:12 consumed offset=1204300   14:12:20 consumed offset=1204300   ← 同一条被消费两次
  ```
- **根因**：`enable.auto.commit=true` 提交的是「拉取位置」；单批 500 条 × 单条 2s = 1000s > `max.poll.interval.ms=300s`，消费者处理到一半被踢出组，rebalance 后从已提交的拉取位置重来。
- **修复**：关自动提交、`max.poll.records=100`、`ack-mode=manual_immediate`；消费端补幂等键（第 24 章）。
- **防复发**：`单批最坏处理时间 < max.poll.interval.ms / 2` 写成配置校验；`rebalance_total` 突增即告警。

**故障三：顺序错乱（PAID 覆盖了 CANCELLED）**

- **现象**：1,860 笔已取消订单被改回「已支付」，1,240 件实际发货。
- **排查**：
  ```bash
  # 同一订单的两条消息，落在不同分区 = 破序的第一嫌疑
  $ grep 'bizKey=ORDER-20250812-4412' /data/logs/order-service/mq.log
  # partition=3 offset=771203  type=PAID
  # partition=9 offset=881004  type=CANCELLED
  $ kafka-topics.sh --bootstrap-server broker1:9092 --describe --topic order.events | head -3
  # Partition: 3 ...   Partition: 9 ...
  ```
- **根因**：下单事件用 `order_id` 做 key，支付事件用了 `pay_id` 做 key，同一聚合根落两个分区并行处理；`CANCELLED` 先到、`PAID` 后到覆盖。
- **修复**：所有订单事件统一用 `order_id` 做 key；状态更新加 `WHERE status IN (...)` 与 `version` 条件（第 25 章）。
- **防复发**：Topic 的 `key.field` 写进登记表并 CI 校验；状态迁移合法性 SQL 每天跑（第 25 章 §25.7）。

**故障四：不再消费（24 个分区只有 18 个有 owner）**

- **现象**：lag 从 20:00 起持续上涨，但消费组「有成员」，没有报错。
- **排查**：
  ```bash
  $ kafka-consumer-groups.sh --bootstrap-server broker1:9092 --describe --group order-service --members
  # GROUP           CONSUMER-ID       HOST          #PARTITIONS
  # order-service   consumer-1-1      /10.0.3.17    6
  # order-service   consumer-1-2      /10.0.3.17    6
  # order-service   consumer-1-3      /10.0.3.17    6      ← 3 实例 × 6 = 18 个线程
  $ kafka-consumer-groups.sh --bootstrap-server broker1:9092 --describe --group order-service \
    | awk '$6=="-" {print $3}' | wc -l
  # 6                                                       ← 6 个分区没人要
  ```
- **根因**：Topic 扩到 24 分区后，消费者仍是 3 实例 × `concurrency=6` = 18 个线程，多出的 6 个分区**没有线程可分配**（Kafka 不会把两个分区给同一线程）。这类问题不会报错，只会静默堆积。
- **修复**：实例数 × `concurrency` ≥ 分区数（4 实例 × 6 = 24）；或把 `concurrency` 提到 8。
- **防复发**：把 `分区数 / (实例数 × concurrency)` 做成指标，> 1 即告警；扩容分区后必须同步加消费者（第 25 章）。

**故障五：堆积卡死（单分区 lag 涨，线程永远阻塞）**

- **现象**：24 个分区里只有 partition 7 的 lag 从 0 涨到 41 万，其余正常；实例 CPU 12%。
- **排查**：
  ```bash
  $ jcmd $(pgrep -f order-service) Thread.print -l | grep -A 12 'consumer-1-7'
  # "consumer-1-7" #47 prio=5 ... RUNNABLE
  #   at java.net.SocketInputStream.socketRead0(Native Method)
  #   at java.net.SocketInputStream.read(SocketInputStream.java:171)
  #   at okhttp3.internal.http2.Http2Stream.waitForIo(Http2Stream.java:596)
  #   at com.vendor.risk.RiskClient.check(RiskClient.java:88)     ← 第三方 SDK，无超时
  ```
  `jstack` 连续三次都在同一行 → 不是慢，是**永久阻塞**。
- **根因**：风控 SDK 的 OkHttp client 没有设置 `callTimeout`，对端不回包时 TCP 读永久挂起；消费线程被单条消息钉死，该分区后续全部卡住。
- **修复**：给 SDK 加 `connectTimeout=1s / readTimeout=2s / callTimeout=3s`；消费端加「单条处理超时 5s + 超时进 DLQ」。
- **防复发**：`consumer_process_seconds` 直方图 + 单条 > 10s 告警；禁止在消费线程里调用无超时的外部 SDK（CI 静态检查 `OkHttpClient` 构造）。

**故障六：消息体过大被拒（2.4MB 的 base64 图片）**

- **现象**：某类带「电子发票 PDF」的消息 100% 发送失败，DLQ 里 `PERMANENT` 全是这一类。
- **排查**：
  ```bash
  $ grep -c 'RecordTooLargeException' /data/logs/billing/mq.log
  # 1,842
  $ grep -m1 'RecordTooLargeException' /data/logs/billing/mq.log
  # The message is 2516582 bytes when serialized which is larger than 1048576,
  # which is the value of the max.request.size configuration.
  $ kafka-configs.sh --bootstrap-server broker1:9092 --describe --entity-type topics \
    --entity-name billing.invoice | grep -i max.message
  # max.message.bytes=1048588
  ```
- **根因**：PDF 做了 base64 内联进消息体（2.4MB），超过 Kafka 默认 1MB。
- **修复**：改「Claim Check」模式——文件存对象存储，消息只放 `{"_ref":"s3://...","sha256":"...","size":2516582}`，消息体降到 300 字节。
- **防复发**：生产端在序列化前做大小校验（> 256KB 直接拒绝并告警）；Topic 的 `maxMessageBytes` 写进 YAML 并在 CI 里校验（第 23 章）。

### 第四步：坑一——消息体过大（上限表 + 正确做法）

| MQ | 默认上限 | 可调参数 | 调大的代价 | 备注 |
| --- | --- | --- | --- | --- |
| Kafka | 约 1MB（`max.message.bytes=1048588`） | topic 级 `max.message.bytes` + broker `message.max.bytes` + 生产端 `max.request.size` | 批处理内存放大、复制带宽放大、坏消息拖慢整个分区 | **三处必须同时改**，只改一处会得到 `RecordTooLargeException` 或 `InvalidRecordException` |
| RocketMQ | 4MB | `maxMessageSize` | 大消息走 CommitLog，影响 page cache 命中 | 5.x 对大于 4MB 有特殊处理，以文档为准 |
| Pulsar | 5MB | `maxMessageSize` | 受 BookKeeper 条目大小限制 | |
| RabbitMQ | 无硬上限，受 `frame_max`（默认 128KB）与内存水位影响 | `frame_max`、`max_message_size` | 内存与流控（credit）压力大 | 实践别超几 MB |
| NATS JetStream | 1MB | `max_payload` | 单条大消息阻塞 subject | |
| AWS SQS | 256KB | 不可调 | —— | 只能 Claim Check |

**大消息的正确做法（四选一，按推荐度排序）**：

1. **Claim Check（首选）**：大对象存 S3/OSS/MinIO，消息只传引用 + 校验和。
2. **压缩**：JSON 文本压缩比通常 3~8 倍；注意压缩后仍要校验大小，且消费端要能识别编码。
3. **分片 + 重组**：把 10MB 拆成 64 个 160KB 的分片，带 `msg_id + seq + total`，消费端按 `msg_id` 聚合（要处理丢片与超时，复杂度高）。
4. **换通道**：文件类走对象存储 + 事件通知，消息只做「元数据通知」。

```java
// Java：Claim Check 的写侧与读侧。消息体恒定在几百字节，永远不会撞上限。
public String publishWithClaimCheck(InvoiceEvent e) throws IOException {
    byte[] pdf = e.pdfBytes();
    String sha = DigestUtils.sha256Hex(pdf);
    String key = "invoice/" + e.invoiceId() + "/" + sha + ".pdf";
    if (pdf.length > 256 * 1024) {                 // 阈值：256KB，超过一律外置
        oss.putObject(bucket, key, new ByteArrayInputStream(pdf), pdf.length, "application/pdf");
        String ref = "{\"_ref\":\"oss://" + bucket + "/" + key + "\",\"sha256\":\"" + sha
                   + "\",\"size\":" + pdf.length + ",\"contentType\":\"application/pdf\"}";
        kafka.send(new ProducerRecord<>("billing.invoice", e.invoiceId(), ref));
        return ref;
    }
    kafka.send(new ProducerRecord<>("billing.invoice", e.invoiceId(), Json.write(e)));
    return null;
}

// 读侧：兼容「内联」与「引用」两种形态，灰度期间两种都会出现
public byte[] resolve(String payload) throws IOException {
    JsonNode n = Json.readTree(payload);
    if (!n.has("_ref")) return n.has("pdfBytes") ? n.get("pdfBytes").binaryValue() : new byte[0];
    String ref = n.get("_ref").asText();            // oss://bucket/key
    byte[] data = oss.getObject(ref.substring("oss://".length()));
    String expect = n.get("sha256").asText();
    if (!DigestUtils.sha256Hex(data).equals(expect)) {
        throw new IllegalStateException("claim check checksum mismatch: " + ref);  // 必须校验，防串包
    }
    return data;
}
```

```go
// Go：发送前的大小守卫。放在 Publisher 内部，业务无法绕过。
const maxInlineBytes = 256 << 10 // 256KB

func (p *Publisher) PublishMaybeExternal(ctx context.Context, topic, key string, body []byte) error {
	if len(body) <= maxInlineBytes {
		_, _, err := p.Publish(ctx, topic, key, body)
		return err
	}
	sha := fmt.Sprintf("%x", sha256.Sum256(body))
	objKey := fmt.Sprintf("%s/%s/%s.bin", topic, key, sha)
	if err := p.store.Put(ctx, objKey, body); err != nil {
		return fmt.Errorf("claim-check put: %w", err)
	}
	env := map[string]any{"_ref": "s3://" + p.bucket + "/" + objKey, "sha256": sha, "size": len(body)}
	payload, _ := json.Marshal(env)
	_, _, err := p.Publish(ctx, topic, key, payload)
	return err
}
```

### 第五步：坑二——序列化不兼容（schema 演进规则）

**这一类故障的特征**：发布后消费端大面积 `DeserializationException`，或者更糟——**不报错但字段是空的/被截断的**（`long` 变成 `double` 精度丢失，金额少了几分钱）。

| 变更动作 | JSON | Protobuf | Avro（BACKWARD） | 结论 |
| --- | --- | --- | --- | --- |
| 新增可选字段 | ✅ 安全 | ✅ 用新 tag | ✅ 必须有 default | 安全 |
| 删除字段 | ⚠️ 老消费者忽略，新消费者拿不到 | ⚠️ 必须 `reserved` tag，永不复用 | ❌ 破坏兼容 | 先停止写入，两个版本后再删 |
| 改字段类型（int→string） | ❌ 运行时才炸 | ❌ wire type 变了，解析错位 | ❌ | 只能加新字段 |
| 改字段名 | ✅（按名匹配） | ✅（按 tag，名字无关） | ⚠️ 依赖 alias | Protobuf 占优 |
| 枚举新增值 | ⚠️ 老代码 `switch` 落到 default | ⚠️ 老代码可能不认识（保留 `UNRECOGNIZED`） | ✅ 有 default 则安全 | 消费端必须有 default 分支 |
| `long` > 2^53 走 JSON | ❌ JS/部分库精度丢失 | ✅ | ✅ | 金额/ID 用 string 或 Protobuf |
| 加必填字段 | ❌ 老消息没有 | ✅ proto3 全 optional | ❌ | 永远不要加「必填」 |

```protobuf
// invoice.proto —— 演进规则写在注释里，评审时对照
syntax = "proto3";
package billing.v2;

message InvoiceEvent {
  string invoice_id = 1;          // 业务主键，永不复用 tag 1
  int64  amount_cent = 2;         // 用「分」的整数，禁止 double 表示金额
  string currency = 3;
  InvoiceType type = 4;

  // 2 号字段曾经是 bytes pdf = 2; 已废弃 → 必须 reserved，防止有人复用
  reserved 2;
  reserved "pdf";

  // 新增字段一律 optional + 明确默认语义（proto3 的 scalar 没有 presence，
  // 需要区分「没传」与「传了 0」时用 optional / wrapper）
  optional string pdf_ref = 10;   // Claim Check 引用，替代内联 bytes
  optional int64  issued_at_ms = 11;

  enum InvoiceType {
    INVOICE_TYPE_UNSPECIFIED = 0; // 0 值必须存在且语义为「未知」
    INVOICE_TYPE_BLUE = 1;
    INVOICE_TYPE_RED = 2;
  }
}
```

```bash
# 兼容性门禁：CI 里对上一个发布版本做 breaking change 检查，不过就拒绝合并
buf breaking --against 'https://github.com/org/protos.git#branch=main' .
# 典型输出：invoice.proto:22:3: Field "2" with name "pdf" on message "InvoiceEvent" is reserved.

# Kafka 侧：确认 Topic 上到底用的哪个 schema（Confluent 体系）
curl -s http://schema-registry:8081/subjects/billing.invoice-value/versions/latest | jq .
# {"subject":"billing.invoice-value","version":7,"schema":"..."}
# 兼容模式：BACKWARD 允许「删字段」，FORWARD 允许「加字段」，FULL 两者都要
curl -s http://schema-registry:8081/config/billing.invoice-value
# {"compatibilityLevel":"BACKWARD"}
```

```java
// Java：消费端必须「宽进」——未知字段忽略，但已知字段缺失要能给出明确错误
private static final ObjectMapper MAPPER = JsonMapper.builder()
        .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false) // 上游加字段不炸
        .configure(DeserializationFeature.FAIL_ON_NULL_FOR_PRIMITIVES, true)
        .build();

public InvoiceEvent parse(byte[] payload) {
    try {
        InvoiceEvent e = MAPPER.readValue(payload, InvoiceEvent.class);
        if (e.getInvoiceId() == null || e.getInvoiceId().isBlank()) {
            throw new IllegalArgumentException("invoice_id missing");  // 缺主键 = 不可处理，进 DLQ
        }
        return e;
    } catch (JsonProcessingException ex) {
        throw new PermanentException("poison message: " + ex.getOriginalMessage(), ex);
    }
}
```

> ⚠️ **两个反模式**：① `FAIL_ON_UNKNOWN_PROPERTIES=true`——上游加一个字段，你的消费端全线报错；② 用 `Map<String,Object>` 接消息——编译期不报错，运行时靠 `(String) map.get("amount")` 强转，字段改名后 `ClassCastException` 在半夜两点出现。**要宽进，但要有契约。**

---

## 27.5 双修对照：路径一样，多问一句「花了多少」

| 维度 | 传统系统 | Agent 系统 | 同 or 异 |
| --- | --- | --- | --- |
| **机制本体** | 五段路径：sendId → broker → 位点 → 线程栈 → 业务库 | **完全相同**，路径图可以直接贴到 Agent 的值班屏 | **同（可直接复用）** |
| **身份 / 唯一键** | `order_id` → `msg_id` → `(topic, partition, offset)` | `run_id` → `step_id` → `tool_call_id`；`sendId` 对应 `event_id` | 同构，链更长一层 |
| **「消息」的粒度** | 一条业务事件 | 一条 Step 事件（`llm.requested`/`tool.called`/`llm.responded`），一个 Run 8~40 条 | 异：量级大、单条大 |
| **典型丢失点** | 客户端超时丢弃、位点提前提交 | 同上 + **租约过期后 Worker 接手同一个 Run**（重复）+ **上下文超窗导致 Step 被跳过** | 多两类 |
| **排查要多问一句** | 「消息到了吗」 | 「**这一步花了多少 token、调了几次模型**」——重复 Step 在日志里长得像正常 | **异：成本维度** |
| **恢复方式** | 重投消息、reset 位点 | **从 `run_checkpoint` 续跑**；reset 位点重投 = 重跑整条 Run = 重花钱 | **根本差异** |
| **验证方法** | 生产条数 vs 落地条数 | 同 + `run_step` 条数 vs 计费条数 vs 工具调用日志，三方对账 | 框架相同，多对一笔账 |
| **观测指标** | lag、rebalance、DLQ 水位 | 加 `in-flight Run`、`checkpoint_age`、`wasted_tokens_total`、单 Run 成本 | 指标集扩展 |
| **不可回滚** | 已发短信/已扣款 | 每次工具调用都是外部副作用，且**调用与否由模型决定** | 放大一万倍 |

**可以直接复用的（一套脚本两边用）**：五段路径图、`grep bizKey` / `--offset` 取消息 / `--describe` 判读表 / `jstack` 三次采样法 / 「消费声称完成但业务没有」的对账 SQL。

**只有三样必须改**：

1. **查什么 ID**。传统是 `msg_id`，Agent 是 `(run_id, step_seq)`，副作用还要叠加 `(tool_name, args_hash)`。**只看 `run_id` 会把「同一 Run 的 8 个 Step」混成一锅**。
2. **怎么恢复**。传统排查完「reset 位点重投」是标准动作；Agent 侧这个动作**会重跑整条 Run**，8 步全烧一遍。正确动作是「修复根因 → 从 `run_checkpoint(step_seq=n)` 续跑 → 校验第 n+1 步的副作用是否已发生」。
3. **成本也是证据**。Agent 的一次「消息重复」在日志上完全正常（两条 `llm.responded`），只有账单或 `run_step.tokens` 能看出来。**所以 Agent 的排查清单里必须有「这条 Run 的 token 用量是否符合预期（步数 × 单步均值 ±30%）」这一项。**

> 三定律检验：**状态机**——真相源在 `run_step`/`run_checkpoint`，消息与日志只是副本；**稀缺资源**——传统排查耗人力，Agent 排查还要查「钱烧在哪」；**不可回滚**——已执行的工具调用只能补偿，所以排查的终点不是「找到原因」，而是「确认副作用清单」。

---

## 27.6 会在哪炸：排查动作本身造成的二次故障

这一节是本章最容易被忽略的部分：**排查工具会把生产打挂**。

| # | 危险动作 | 为什么会炸 | 正确做法 | 事故量级 |
| --- | --- | --- | --- | --- |
| 1 | `--from-beginning` 在生产 Topic 上拉全量 | 全量消费打满 broker 网络与磁盘，还制造新的消费组积压 | 用 `--partition --offset` 精确取 1 条；或用离线副本/镜像集群 | 27.1 事故一：新增 2,100 条积压 |
| 2 | `kafka-dump-log.sh --deep-iteration` 扫整个分区目录 | 顺序读数 GB 日志，IO 打满影响正常 fetch | 只 dump 单个 `.log` 文件 + `grep 'offset: N'`；在从库/离线副本上做 | 磁盘 util 100%，P99 翻 3 倍 |
| 3 | `jmap -histo:live` / `jcmd GC.run` | `:live` 触发 Full GC，STW 数秒，消费者被踢出组 | 用 `jcmd GC.class_histogram`（不带 live）或先 `jstat` 判断 | 一次 Full GC 引发全组 rebalance，重复数万条 |
| 4 | 连续 `jstack` 不控制频率 | 每次都要走到 safepoint，高频采样会拖慢应用 | 3 次 × 间隔 5s 足够；用 `-l` 只加一次 | CPU 抖动，P99 +30% |
| 5 | 排查时 `--reset-offsets --execute` | 位点回退 = 大规模重复消费 | 先 `--dry-run` 看会影响多少条；确认幂等后再执行 | 一次误操作重复 12 万条 |
| 6 | 在消费组里加一个「临时排查消费者」 | 触发 rebalance，正常消费者停摆 30s | 用 `--describe` + broker 端查询，**不要加入消费组** | 每次排查都制造一次 lag 尖刺 |
| 7 | 直接改 `min.insync.replicas` 或 `unclean` 想「先恢复」 | 把「不丢」降级成「可能丢」，事后无法判断丢了哪些 | 先降级读（停写）或扩容 broker，不改可靠性配置 | 事故一那种截断会再来一次 |

**一条纪律**：**任何「会改变状态」的排查命令（reset、pause、扩容、改配置）都必须先 `--dry-run` 或在只读副本上验证，并由第二个人确认。** 只读命令（`--describe`、`--offset` 取一条、`jstack`）可以随便跑。

---

## 27.7 怎么验证：把排查路径做成演练

**① 逐段断链演练**（每季度一次，验证「15 分钟能定位」不是口号）：人为在五段中各制造一次故障，记录定位耗时。

```bash
# 段 ②：让生产端超时（tc 注入 5s 延迟到 broker），验证日志能打出 TIMEOUT 与 latencyMs
sudo tc qdisc add dev eth0 root netem delay 5000ms
# 段 ③：kill leader，验证能查出「Isr 只剩 1」与「offset 回退」
# 段 ④：停一个消费者实例，验证 --describe 能看出「分区无 owner」
kubectl scale deploy/order-service --replicas=2   # 24 分区 × 3 实例 → 2 实例
# 段 ⑤：制造一次「去重表有、业务表无」，验证对账 SQL 能抓到
mysql -e "INSERT INTO consume_dedup(consumer_group,idem_key,topic,partition_no,offset_no,status)
          VALUES('point-service','PAY:FAKE-001','pay.success',0,1,2)"
```

**② 排查脚本化**（把五段路径做成一个脚本，值班同学只填两个参数）：

```bash
#!/usr/bin/env bash
# trace-msg.sh <biz_key> [partition] [offset]
set -euo pipefail
KEY="$1"; PART="${2:-}"; OFF="${3:-}"
echo "== ① 生产端 =="
grep -h "bizKey=${KEY}" /data/logs/*/mq.log | tail -3 || echo "!! 无发送日志：查 outbox 与异常"
echo "== ② broker =="
if [[ -n "${PART}" && -n "${OFF}" ]]; then
  timeout 20 kafka-console-consumer.sh --bootstrap-server broker1:9092 \
    --topic pay.success --partition "${PART}" --offset "${OFF}" --max-messages 1 \
    --property print.offset=true --property print.timestamp=true --property print.key=true
else
  echo "  未给 partition/offset，跳过（不要用 --from-beginning 在生产扫）"
fi
echo "== ③ 位点与堆积 =="
kafka-consumer-groups.sh --bootstrap-server broker1:9092 --describe --group point-service \
  | awk -v p="${PART}" 'NR==1 || $3==p'
echo "== ④ 线程栈（top 3 阻塞点）=="
jcmd "$(pgrep -f point-service)" Thread.print -l 2>/dev/null \
  | grep -E '^\s+at ' | sort | uniq -c | sort -rn | head -3
echo "== ⑤ 业务落地 =="
mysql -N -e "SELECT 'dedup',status,created_at FROM consume_dedup WHERE idem_key='PAY:${KEY}'
             UNION ALL SELECT 'ledger',biz_type,created_at FROM point_ledger WHERE msg_id='PAY:${KEY}'"
```

**③ 排查质量的三个指标**（否则你永远不知道流程有没有用）：

| 指标 | 定义 | 目标 |
| --- | --- | --- |
| MTTD（定位时长） | 从「收到工单」到「说出死在哪一段」 | < 15 分钟 |
| 一段命中率 | 第一次就查对段的比例 | > 80% |
| 二次故障率 | 排查动作本身引发新故障的比例 | 0 |

---

## 27.8 用什么工具：可观测性买得到，「按 key 查」买不到

| 能力 | 开源/云够用 | 必须自己写 |
| --- | --- | --- |
| 按 offset 取消息 | `kafka-console-consumer.sh`、Kafka UI、AKHQ | —— |
| 按 key 查消息 | RocketMQ 原生索引 + 轨迹；Kafka **没有** key 索引 | **自建 `msg_key_index` 表**或依赖消息轨迹平台（把 key→offset 落库） |
| 消费位点 | `kafka-consumer-groups` / RocketMQ Console | 位点与「业务已处理」的对账 |
| 线程栈 | `jstack`/`jcmd`、pprof | 定时采样 + 自动归档（出事时现场已没了） |
| 全链路 trace | OpenTelemetry + Jaeger/Tempo | 消息生产/消费两端的 span 关联（`sendId` 作为 link） |
| 消息轨迹 | RocketMQ MessageTrace、Confluent 生态较弱 | 轨迹落库 + 按业务键检索的入口 |
| Agent 成本排查 | —— | `run_step` × 计费流水 × `tool_call` 三方对账 |

**Kafka「按 key 查」的现实**：Kafka 只有 offset 索引，没有 key 索引（`index` 文件是稀疏的 offset→position）。所以生产上要么用「时间 → offset 区间 → 逐条扫」的笨办法，要么**自己建索引**：

```sql
-- 消息索引表：生产端双写（异步、可丢），只用于排查，不进主链路
CREATE TABLE msg_key_index (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  biz_key     VARCHAR(128) NOT NULL COMMENT 'order_id / run_id',
  msg_id      VARCHAR(64)  NOT NULL,
  topic       VARCHAR(128) NOT NULL,
  partition_no INT         NOT NULL,
  offset_no   BIGINT       NOT NULL,
  created_at  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  KEY idx_key_time (biz_key, created_at),
  KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='排查用索引：按业务键秒查 offset';

-- 「这条消息去哪了」一条 SQL 出结果
SELECT biz_key, topic, partition_no, offset_no, created_at
  FROM msg_key_index
 WHERE biz_key = 'ORDER-20250908-771234'
 ORDER BY created_at DESC LIMIT 20;
```

**自研边界**：**采集与存储用开源，索引与关联必须自己写**——因为「什么算同一个业务」只有你知道。

---

## 27.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 五段路径 | 生产端 sendId → broker key/offset/时间 → 位点与堆积 → 线程栈 → 业务库 |
| 三个判断点 | `SEND_OK` 后在不在 broker、位点过没过、业务落了没；组合出八种情况覆盖 95% |
| 日志六字段 | `sendId/bizKey/topic-partition-offset/result/latencyMs/traceId`，缺一个就会卡住 |
| 禁止动作 | 先重启、先问人、`--from-beginning` 扫生产、往消费组里加临时消费者 |
| `--describe` 判读 | `LAG=0 + CONSUMER-ID='-'` = 分区没分配；`LAG>0 + '-'` = 静默堆积 |
| 线程栈 | 连采 3 次 × 5s；同一行三次 = 卡死（无超时的外部调用最常见） |
| 六个故障 | 超时丢弃、重复爆炸、顺序错乱、分区无 owner、无超时阻塞、消息过大 |
| 消息体上限 | Kafka ~1MB、RocketMQ 4MB、Pulsar 5MB、SQS 256KB；**大消息走 Claim Check** |
| 序列化 | 只加不改；删字段要 `reserved`；金额用整数分；消费端 `FAIL_ON_UNKNOWN=false` |
| 排查工具的反噬 | dump-log/rejstack/jmap -live/reset-offsets 都能把生产打挂，必须先 dry-run |
| Kafka 无 key 索引 | 要秒查就自建 `msg_key_index`，否则只能按时间换 offset 再扫 |
| Agent 侧 | 多问一句「花了多少 token」；恢复用 checkpoint，不要 reset 位点重投 |

## 27.10 自检题

1. **客服说「客户付了钱没发货」。你手里只有订单号，15 分钟内要定位。写出你的前 5 条命令，并说明每条命令的「否定结果」意味着什么。**
   > 要点：① `grep bizKey=<order_id> mq.log` —— 没有 = 业务没发或日志没埋；② 若找到就取 `partition/offset`，`kafka-console-consumer --partition --offset --max-messages 1` —— 取不到 = 已被 retention 删/从未落盘；③ `--describe --group` 看该分区 `CURRENT-OFFSET` 与 `CONSUMER-ID` —— `-` 表示分区没分配；④ `jstack` 三次 —— 同一行 = 卡死；⑤ 按 `msg_id` 查 `consume_dedup` + 业务表 —— dedup 有业务无 = 位点提前提交。**关键：每一条都要能给出「否定结果的含义」，否则你只是在试。**

2. **`LAG=0` 但业务数据少了 12,000 条，可能是哪三种原因？各自的验证方法是什么？**
   > 要点：① **位点提前提交**（先 ack 后处理 / 异步线程池）——验证：`consume_dedup` 有 `status=2` 但业务表缺失（§27.4 第五步的对账 SQL）；② **消费端把异常吞了并 ack**——验证：日志里 `catch` 分支的 `warn`，或 DLQ 为空但业务无数据；③ **业务落库失败但位点已提交**（跨库/事务边界错）——验证：查 `consume_dedup` 与业务表的写入时间差、查应用日志的 rollback。三个都不是 broker 的问题，所以看 lag 永远看不出来。

3. **你们要发一个 2MB 的 PDF 到 Kafka，有同事提议把 Topic 的 `max.message.bytes` 调到 4MB。请给出三个反对理由与一个替代方案。**
   > 要点：理由：① 只改 topic 级不够，还要改 broker `message.max.bytes` 和生产端 `max.request.size`，三处不同步就会出现「有的生产者成功有的失败」；② 大消息会拖慢同分区的其它消息（单条 4MB 的复制占满网络，P99 抬升），并且压缩/批处理的内存放大数倍；③ 消费端一旦有一条坏消息，重试与 DLQ 都会带着 4MB 的载荷反复搬运（DLQ 表会被撑爆）。替代方案：**Claim Check**——PDF 存对象存储，消息只传 `{"_ref","sha256","size"}`，消息体降到几百字节；消费端读取时校验 sha256 防串包。

## 27.11 延伸阅读

- Kafka 工具全集：`kafka-consumer-groups.sh`（`--describe/--members/--reset-offsets --dry-run`）、`kafka-run-class.sh kafka.tools.GetOffsetShell`（`--time -1/-2/<ms>`）、`kafka-dump-log.sh`（`--print-data-log --deep-iteration`）
- Kafka 索引结构：`.index`（offset→position 稀疏索引，默认每 4KB 一条）、`.timeindex`（时间戳→offset），这就是「为什么 Kafka 没有 key 索引」
- RocketMQ：`mqadmin queryMsgByKey/queryMsgById/consumerProgress/consumerConnection`、消息轨迹（`MessageTrace`）与 `%RETRY%`/`%DLQ%` 的位点查看
- JVM 排查：`jcmd Thread.print`、`jstat -gcutil`、JFR（`-XX:StartFlightRecording`）——**优于** `jmap -histo:live`
- Schema 演进：Protobuf「Updating A Message Type」（`reserved`、tag 永不复用）、Avro schema resolution（BACKWARD/FORWARD/FULL）、`buf breaking` 作为 CI 门禁
- 重复与位点：[第 24 章](24-delivery-semantics.md)；顺序与堆积判定：[第 25 章](25-ordering-and-lag.md)；可靠性的八环节：[第 26 章](26-message-reliability.md)
- 延迟重试与 CDC 的排查：[第 28 章](28-delayed-queue-cdc.md)；Top 30 故障形态速查：[第 51 章](51-troubleshooting-handbook.md)
- Agent 侧 trace 与成本串联：见 [../llm/6-session-run-event-model.md](../llm/6-session-run-event-model.md)、[../llm/47-cost-engineering.md](../llm/47-cost-engineering.md)
- 总纲：[0-summary.md](0-summary.md) ｜ 语言与运行时：[../go/0-summary.md](../go/0-summary.md) ｜ 平台治理：[../llm/0-summary.md](../llm/0-summary.md)
- 前置：[第 26 章](26-message-reliability.md) ｜ 下一章：[第 28 章 延迟队列、定时任务与 CDC](28-delayed-queue-cdc.md)
