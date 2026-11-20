# 25. 顺序、堆积与重平衡

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 异步与消息 ｜ **编号**：25 / 52
> **本章定位**：顺序不是 MQ 的一个功能开关，而是「**同一个 key 进同一个分区 + 这个分区同时只有一个线程在处理**」两件事同时成立的结果。这一章讲三种保序方案的代价、四种破序、堆积的四类根因，以及 rebalance 那 30 秒里消息和位点到底发生了什么。
> **读法**：§25.2 记住「并发度 = 分区数」；§25.4 的破序四象限和堆积处置表是值班时查的；§25.5 只记一句：Agent 侧同一 session 必须保序，否则上下文会错位。

---

## 25.1 引子：一个 `@Async` 让 1,860 笔已取消订单重新发货

**事故一（2025-08，某零售订单中心）**：`order.events` 24 分区，key 是 `order_id`，Spring Kafka `concurrency=6`。这个配置本身是**保序**的：每个分区在任一时刻只由一个消费线程处理（一个线程可以轮转多个分区）。

问题在 listener 内部：半年前有人为了「加快状态变更」，加了一行：

```java
@Async("bizExecutor")
public void applyStatus(OrderEvent e) { ... }   // 火忘式调用，listener 立刻返回
```

于是同一订单的 `PAID` 与 `CANCELLED` 虽按序**读取**，却被丢进线程池**并行执行**。14:32 取消潮里，`CANCELLED` 先拿到行锁，`PAID` 后到覆盖了状态：

- 1,860 笔已取消订单被重新置为 `PAID`；
- 实际发货 **1,240 件**，逆向物流与折价损失 **74 万元**；
- 客诉 380 单，修复用了 6 小时（逐单比对支付流水与取消时间）。

**事故二（同月大促，堆积）**：同一个 Topic，峰值生产 **18 万 msg/s**，24 个分区，单分区消费能力约 **800 msg/s**，总消费能力 **1.92 万/s**。20:00 开始堆积以 **16 万/s** 增长，30 分钟堆了 **约 2.9 亿条**。值班同学把消费者从 24 扩到 48 实例，**lag 一点没降**——分区只有 24 个，多出的实例分不到分区，全程空转。直到 00:40 才消化完，端到端延迟 **4 小时 40 分**。

> 🩸 **两个事故的共同点**：第一个把「读取顺序」当成了「执行顺序」，第二个把「消费者数量」当成了「消费能力」。**顺序由分区和线程共同决定，吞吐由分区数封顶**——这两句话值 74 万。

---

## 25.2 到底是什么：顺序 = 分区键 + 单线程

**定义**：对同一个业务键（`order_id`、`user_id`、`session_id`），消息被**处理的顺序**与**产生的顺序**一致。注意是「处理」，不是「发送」或「读取」。

三个必须拆开的反例：

- **全局有序**：所有消息进 1 个分区、1 个线程，吞吐 = 单分区能力（1k~5k msg/s），除了账务流水这类极少数场景，没人需要。
- **发送有序 ≠ 处理有序**：生产者按顺序 `send()`，broker 按顺序追加，但消费者用线程池并行处理，执行顺序仍然会乱。**顺序的最后一公里在消费端**。
- **分区有序 ≠ 业务有序**：同一订单的下单用 `order_id`、支付用 `pay_id`，会落到不同分区并行处理。key 必须是**聚合根的唯一标识**，创建后不变。

保序的代价可以写成一行公式：

```
最大并发度 = 分区数
最大吞吐   = 分区数 × 单分区处理能力
```

所以要保序的「活跃 key 数」决定了**单个分区要串行处理多少个 key**：5 万个活跃订单、24 个分区，平均每分区要串行处理约 2,000 个订单的事件。分区数下限由「峰值吞吐 ÷ 单分区能力」决定；**分区不是越多越好**（rebalance 慢、句柄多、延迟高），但少于「峰值吞吐 ÷ 单分区能力」就一定堆积。

---

## 25.3 结构图：三种保序方案与四个破序点

```
【方案 A 单分区单消费】全局有序，吞吐被单线程封顶
  P0 ──► consumer-1（唯一线程）──► 业务
  吞吐 ≈ 2k msg/s；一个慢消息阻塞全站

【方案 B 按 key 分区】分区内有序，分区间并行（绝大多数业务的选择）
  key=order-1 ──hash──► P0 ──► consumer-1 ──► 串行处理 order-1/order-4/order-9...
  key=order-2 ──hash──► P1 ──► consumer-2 ──► 串行处理 order-2/order-5...
  key=order-3 ──hash──► P2 ──► consumer-3
  吞吐 = 分区数 × 单分区能力；扩分区会改变 key→分区映射（破序点①）

【方案 C 业务串行化】MQ 不保序，业务侧按聚合根串行
  任意分区 ──► 消费线程池 ──► key hash 到 N 条 lane ──► 每条 lane 单线程
  吞吐受 lane 数与下游能力限制；实现最复杂，只在「无法控制分区键」时用

【四个破序点】
  ① 多分区：key 选错 / 扩分区 / 同一业务键用了不同 key
  ② 消费并发：listener 内部 @Async、线程池、批量 parallelStream
  ③ 重试队列：失败消息进 retry topic，后来的消息先成功，重试的后到
  ④ 多实例无粘性：rebalance 把分区迁走，旧实例还在处理（僵尸消费者）
```

---

## 25.4 怎么实现：先选保序级别，再堵四个破序点

### 第一步：三种方案的真实代价

| 维度 | A 单分区单消费 | B 按 key 分区 | C 业务串行化 |
| --- | --- | --- | --- |
| 顺序范围 | **全局** | 单 key（分区内） | 单 key（lane 内） |
| 最大并发 | 1 | 分区数 | lane 数（可 > 分区数） |
| 吞吐上限 | 单分区能力，约 1k~5k/s | 分区数 × 单分区能力 | 受下游与 lane 调度限制 |
| 实现成本 | 最低 | 低（选对 key） | 高（lane 路由 + 水位提交） |
| 扩容方式 | **不能扩**（只能升配） | 加分区（会破序）或加实例（≤分区数） | 加 lane（需保证同一 key 不换 lane） |
| 失败恢复 | 单点，重启期间全停 | 分区迁移，秒级抖动 | lane 内存队列丢失需重放 |
| 适用 | 账务流水、配置变更 | 订单、支付、库存、会话 | 第三方 key 不可控、跨 Topic 聚合 |

**结论**：99% 的业务选 B。只有当「同一业务键的消息天然跨 Topic/跨分区，且无法改上游」时才用 C。A 只用于「顺序错误会导致账不平」的极窄场景。

### 第二步：四个破序点，逐个堵

| # | 破序点 | 现象 | 根因 | 解法 |
| --- | --- | --- | --- | --- |
| ① | **多分区** | 同一订单的 `PAID` 与 `CANCELLED` 在不同分区并行；扩分区后历史 key 映射全变 | key 不是聚合根 / 扩分区 / 不同事件用不同 key | key 统一为 `order_id`；扩分区安排在低峰并接受「迁移窗口内业务侧串行」；关键 Topic 预留足够分区，**只扩不缩** |
| ② | **消费并发** | 读取顺序正确，执行顺序错乱；日志里同一订单两条消息时间戳倒置 | listener 内 `@Async`、`parallelStream`、自建线程池 | **消费线程内同步处理**；要并行只能按分区并行（`concurrency`）；禁止在 listener 里再开线程池 |
| ③ | **重试队列** | 失败的那条 5 秒后重试，期间后续 20 条已成功，重试消息最后到，把状态改回旧值 | 重试 Topic 与原 Topic 是两条独立链路 | 重试消息**回到原分区**（RocketMQ 顺序重试）或业务侧按 `version` 拒绝旧状态；重试期间**暂停该 key 的后续消息**（按 key 阻塞） |
| ④ | **多实例无粘性** | rebalance 后重复处理 + 状态回退；旧实例日志与新实例日志时间重叠 | 分区迁移时旧实例未停止；无 static membership；处理时间 > `max.poll.interval.ms` | `group.instance.id` 静态成员；`onPartitionsRevoked` 里同步提交并停止处理；状态机加 `version` 乐观锁 |

第 ③ 条最容易被低估：重试**改变了消息的相对顺序**。

```
原分区：  A1 ✅ → A2 ❌ → A3 ✅ → A4 ✅
重试队列：            A2（5s 后）────────────────► 最后执行
业务看到的顺序：A1 → A3 → A4 → A2   ← A2 把状态改回去了
```

解法不是「重试快一点」，而是「**同一个 key 的后续消息必须等前一条成功**」。Kafka 没有原生按 key 阻塞，只能：① 不提交位点、原地重试（阻塞整个分区）；② retry topic 带 `blocked_key`，同 key 有未完成重试则暂停；③ 状态机用版本号让迟到的旧消息影响 0 行。生产上常用 ① + ③。

### 第三步：堆积的四类根因与处置

先看信号，再看动作。**别一看到 lag 就扩容**——四类根因只有第一类能靠扩容解决：

| 根因 | 判定信号 | 处置动作 | 不要做什么 |
| --- | --- | --- | --- |
| **消费能力不足** | lag 在所有分区**均匀上涨**；消费速率 < 生产速率；CPU 高但健康；无单条超时 | 扩实例（≤ 分区数）→ 优化批量/序列化 → 最后才扩分区（会破序） | 分区已满时继续加实例，纯浪费 |
| **单条卡住** | 只有**一个分区** lag 涨；该分区 `CURRENT-OFFSET` 长时间不动；其它分区正常；线程栈卡在同一个调用 | `jstack`/`pprof` 定位；把毒消息转 DLQ；修复后 reset 位点 | 盲目重启（重启后还会卡在同一条） |
| **下游慢** | 消费速率整体下降；下游 P99 升高；线程池/连接池打满；GC 时间上升 | 限流 + 熔断 + 降级（第 18、19 章）；批量写；把同步调用改异步 | 直接扩容消费者，把下游打得更死 |
| **重平衡抖动** | `rebalance` 次数飙升；lag 呈锯齿状；成员频繁进出；日志有 `RebalanceInProgressException` | 调 `session.timeout.ms` / `max.poll.interval.ms`；静态成员；避开高峰发布；查 GC 与网络 | 忽略它，以为是「正常抖动」 |

判定顺序（30 秒内完成）：

```bash
# 1) 看 lag 分布：均匀 = 能力不足；单分区 = 卡住；锯齿 = rebalance
kafka-consumer-groups.sh --bootstrap-server broker1:9092 \
  --describe --group order-service

# 2) 看分区流量是否倾斜（3 倍 = 热 key）
kafka-topics.sh --bootstrap-server broker1:9092 --describe --topic order.events

# 3) 看消费线程卡在哪（Java）
jstack -l $(pgrep -f order-service) > /tmp/order.jstack
grep -A 30 '"order-service"' /tmp/order.jstack | head -80

# 4) Go 的 goroutine（需开 pprof）
go tool pprof -top http://10.0.3.17:6060/debug/pprof/goroutine?debug=1
```

**处置动作的优先级**（从便宜到贵）：① 确认不是毒消息 → ② 加实例（不超过分区数）→ ③ 提高单条处理效率（批量、去掉 N+1 查询、异步化下游）→ ④ 扩分区（最后手段，必须评估 key 重分布）→ ⑤ 降级/丢弃非核心消息（要有业务签字）。

### 第四步：rebalance 期间发生了什么

Kafka 的 rebalance 不是「瞬间切换」。eager 协议下，所有消费者先停止拉取、撤销全部分区，再重新加入；cooperative 只撤销需要迁移的分区，其余继续消费：

```
t0    C3 加入（或 C2 心跳超时 / 发布重启 / 分区数变化）
      │
t0    coordinator 进入 PreparingRebalance，向所有成员发 JoinGroup 响应
      │
t0~t1 eager：所有消费者停止 fetch（lag 开始上涨），每个成员先 revoke 全部分区
      │  cooperative：只 revoke 需要迁走的分区，其余继续消费（顺序见下方注）
      │  ← eager 的 onPartitionsRevoked 在这里执行：提交位点、清理状态
      │
t1    leader 计算分配方案（Range / RoundRobin / Sticky / CooperativeSticky）
      │
t2    SyncGroup 下发方案，成员拿到新分区；执行 onPartitionsAssigned
      │
t3    恢复 fetch；新成员从提交位点继续
```

> ⚠️ 顺序别记反：`onPartitionsRevoked` 在 **JoinGroup/SyncGroup 之前**执行，不是拿到新方案之后。提交位点要放在这里（或更早的连续水位里），否则新实例会从旧位点重放。
>
> cooperative 的完整流程是两轮：先 SyncGroup 算出要迁移的分区，成员 revoke 后再 JoinGroup 完成交接；上面的简化图只强调 eager 的 revoke 时机。

三个关键事实：

1. **rebalance 期间不丢消息，但停止消费**。24 分区一次全量通常 3~30 秒，大集群能到几分钟，这段 lag 全记在你头上。
2. **重复发生在 revoke 与 commit 的缝隙**：`onPartitionsRevoked` 没同步提交，新实例就从更早位点开始，重复量 = 未提交的已处理条数。
3. **eager 是 stop-the-world，cooperative 是增量的**；升级 `CooperativeStickyAssignor` 时协议不能混用，需两阶段滚动。

避坑配置（Kafka 3.x / Spring Kafka 3.x 口径）：

```yaml
spring:
  kafka:
    consumer:
      group-id: order-service
      enable-auto-commit: false
      max-poll-records: 100
      properties:
        partition.assignment.strategy: org.apache.kafka.clients.consumer.CooperativeStickyAssignor
        group.instance.id: ${STABLE_INSTANCE_ID}  # 必须稳定（StatefulSet 序号/外部分配）；Deployment 的 HOSTNAME 每次重建都变
        session.timeout.ms: 45000           # 默认 45s；太小会误踢，太大故障发现慢
        heartbeat.interval.ms: 3000         # 通常是 session 的 1/3 以下
        max.poll.interval.ms: 300000        # 必须 > 单批最坏处理时间 × 2
        fetch.min.bytes: 1048576            # 1MB，攒批降低往返
        fetch.max.wait.ms: 500
    listener:
      ack-mode: manual_immediate
      concurrency: 24                       # = 分区数，一个分区一个线程
      missing-topics-fatal: false
```

> ⚠️ `group.instance.id` 只在 **ID 不变** 时有效：同一台机器、或 StatefulSet 的固定序号重启，可以复用；K8s Deployment 的 Pod 名每次重建都变，拿 `${HOSTNAME}` 当 `group.instance.id` 等于没有静态成员。静态成员解决的是「滚动重启」，不是「扩容」。

### 第五步：分区内串行、分区间并行的代码模板

Java（Spring Kafka，`concurrency` = 分区数，listener 内**绝不再开线程池**）：

```java
@KafkaListener(topics = "order.events", concurrency = "24")
public void onMessage(ConsumerRecord<String, String> rec, Acknowledgment ack) {
    // 同一分区由同一线程串行调用；别再 submit 到别的池
    OrderEvent e = json.read(rec.value(), OrderEvent.class);
    orderStateMachine.apply(e);      // 内部用 WHERE status IN (...) 做条件更新
    ack.acknowledge();               // 处理完成才提交位点
}
```

业务键天然跨分区时（上游用 `pay_id`，你要按 `order_id` 保序）只能做 lane。**lane 模式下位点不能逐条提交**，否则后一条先提交会跳过前一条：

```java
// 64 条 lane，同一 key 永远落同一条；提交按分区维护「连续完成水位」
private final ExecutorService[] lanes = IntStream.range(0, 64)
        .mapToObj(i -> Executors.newSingleThreadExecutor(r -> new Thread(r, "lane-" + i)))
        .toArray(ExecutorService[]::new);
private final ConcurrentHashMap<TopicPartition, Long> nextOffset = new ConcurrentHashMap<>();
private final ConcurrentHashMap<TopicPartition, ConcurrentSkipListSet<Long>> completed = new ConcurrentHashMap<>();

public void dispatch(ConsumerRecord<String, String> rec) {
    String key = rec.key() == null ? "p" + rec.partition() : rec.key();
    int lane = Math.floorMod(key.hashCode(), lanes.length);
    TopicPartition tp = new TopicPartition(rec.topic(), rec.partition());
    nextOffset.putIfAbsent(tp, rec.offset());   // dispatch 按 offset 顺序调用，先记下分区起点
    lanes[lane].submit(() -> {
        try {
            orderStateMachine.apply(json.read(rec.value(), OrderEvent.class));
            completed.computeIfAbsent(tp, k -> new ConcurrentSkipListSet<>()).add(rec.offset());
            long next = nextOffset.get(tp);
            while (completed.get(tp).remove(next)) next++;   // 连续完成水位
            nextOffset.put(tp, next);
            commitContiguous(tp, next);   // 用 Consumer.commitSync(tp, next)；不能用单条 Acknowledgment
        } catch (Exception ex) {
            log.error("lane failed key={} offset={}", rec.key(), rec.offset(), ex);
            // 不提交，交给容器重投；lane 内后续消息会阻塞在该 key 上
        }
    });
}
```

> 💡 位点必须按「连续完成水位」提交，不能用单条 `Acknowledgment`（它提交的是当前 record 的 offset，不是水位，仍可能跳过前面的 in-flight 消息）。分区被撤销/重新分配时，还要在 `onPartitionsRevoked`/`onPartitionsAssigned` 里清掉 `nextOffset`/`completed` 中对应的 `TopicPartition`，否则 seek 回头会带着旧水位跳过消息。嫌复杂就**改上游 key**，别造调度器。

Go（Sarama，`ConsumeClaim` 每个分区一个 goroutine，天然分区内串行）：

```go
func (h *Handler) ConsumeClaim(sess sarama.ConsumerGroupSession, claim sarama.ConsumerGroupClaim) error {
	// Sarama 保证：每个 partition 只有一个 ConsumeClaim goroutine
	for msg := range claim.Messages() {
		var e OrderEvent
		if err := json.Unmarshal(msg.Value, &e); err != nil {
			return fmt.Errorf("decode offset=%d: %w", msg.Offset, err)
		}
		if err := h.sm.Apply(sess.Context(), e); err != nil {
			return err // 返回错误会结束整个 session（所有分区一起 rebalance），不是只暂停该分区；毒消息应先转 DLQ
		}
		sess.MarkMessage(msg, "") // 标记位点，由 Sarama 批量提交
	}
	return nil
}
```

---

## 25.5 双修对照：同一个 key，两种「会话」

| 维度 | 传统系统 | Agent 系统 | 同 or 异 |
| --- | --- | --- | --- |
| **机制本体** | `order_id` 分区保序：下单 → 支付 → 发货 | **同一 `session_id` 必须保序**：用户说「改一下」不能排到「生成」之前；`run_id` 内 Step 按 `seq` 递增 | **完全相同**：都是「聚合根 + 分区键 + 单线程」 |
| **唯一键 / 身份** | `order_id`、`user_id` | `session_id`（跨 Run）、`run_id` + `seq`（Run 内） | 键的层级多一层 |
| **失败模式** | 状态回退、重复发货、超卖 | 顺序错乱 → **上下文错位**（模型基于旧状态回答）；`tool.call` 早于 `approval` → **执行了未审批的工具** | 同构，但 Agent 的错序会变成「安全事故」而不只是脏数据 |
| **消息处理时长** | 毫秒~百毫秒，分区内头阻塞可接受 | **单条 2~60 秒**（模型调用），一个慢 Step 会阻塞整个 session 的所有后续消息 | **根本差异**：传统按「条」消费，Agent 要按「Run」调度 |
| **恢复方式** | 重投消息，从位点继续 | 长任务用 **checkpoint** 恢复；不要重投整条 Run | 差异根因：长事务 + 随时中断 |
| **验证方法** | 同一 key 的消息 offset 单调、状态迁移合法 | 检查 `(run_id, seq)` 连续、Step 顺序与事件顺序一致 | 框架相同 |
| **观测指标** | lag、rebalance 次数、单分区速率 | 加 **in-flight Run 数、单 Run 时长、Step 排队时间** | 指标集扩展 |
| **稀缺资源** | 分区数、消费线程 | **模型并发、TPM、单 Run 预算** | 限的东西不同 |

**可直接复用**：key 选择原则、`concurrency = 分区数` 线程模型、`onPartitionsRevoked` 位点提交、lag 四类根因判定、静态成员配置。

**必须重算的三样**：

1. **「一条消息处理多久」决定了消费模型**。传统消息单条约 5ms、批量后单分区可到 800/s，24 分区就够；Agent 一条 Step 可能触发 30 秒的模型调用，单分区吞吐只有 0.03/s。**不能把「收消息 → 调模型 → 写结果」放在消费线程里**，应「消费只写 Run 表 + 入队，Worker 异步执行并回写 Step 事件」。
2. **顺序的边界从「订单」变成「会话」**。同一 `session_id` 的多个 Run 可能并发，业务要决定「串行还是打断」；`run_id` 内的 Step 必须按 `seq`。
3. **堆积的含义变了**。传统 lag 是条数，Agent 的 lag 是「**排队 Run 数 × 单 Run token 成本**」；10 万个 Run 排队可能是 300 万元在途预算。

> 三定律：**状态机**——真相源在 `Run`/`Step` 表，消息顺序只影响「谁先被通知」；**稀缺资源**——传统限分区，Agent 限模型并发与 TPM；**不可回滚**——乱序导致「未审批就执行工具」无法撤销，只能事前用状态机守卫。

---

## 25.6 会在哪炸：失效模式清单

| # | 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- | --- |
| 1 | 已取消订单又发货 | listener 内 `@Async`/线程池，执行顺序错乱 | 比对 offset 与 `updated_at` | 去掉线程池，改同步 | Code review 禁止 listener 内 `@Async` |
| 2 | 扩分区后大量状态回退 | key→分区映射改变，旧 key 的历史消息在新分区 | 对齐扩分区时间 | 扩分区选低峰；迁移窗口内按 key 串行；状态机加版本号 | 关键 Topic 一次预留足够分区 |
| 3 | 重试消息把状态改回旧值 | 重试 Topic 绕过原分区，迟到的旧消息覆盖新状态 | 查重试消费时间与版本号 | 条件更新 `WHERE version < ?`；同 key 阻塞 | 所有状态更新带版本号 |
| 4 | 扩容后 lag 不降 | 实例数 > 分区数，多出的实例空转 | `--describe` 看成员数与分区数 | 实例数 ≤ 分区数；或扩分区 | 容量模型写进告警：实例数/分区数 |
| 5 | 单分区 lag 涨、其它正常 | 毒消息卡住（某条反序列化失败/下游超时） | `jstack` 看线程栈；看该 offset 内容 | 转 DLQ，reset 位点，修数据 | 消费端加「单条超时 + 失败计数」 |
| 6 | lag 锯齿状、频繁 rebalance | `max.poll.interval.ms` 小于单批处理时间；GC 停顿；网络抖动 | 看 rebalance 原因日志 | 降 `max.poll.records`、调大 poll interval、静态成员 | 单批处理时间 < poll interval/2 |
| 7 | rebalance 后重复数万条 | revoke 时没提交位点；自动提交提交的是拉取位置 | 对比 `CURRENT-OFFSET` 与业务已处理数 | `onPartitionsRevoked` 同步提交；关自动提交 | 位点提交策略纳入上线检查 |
| 8 | 同一 session 的回复串台 | 按 `run_id` 分区，但同 session 的多个 Run 并行 | 按 session 重放事件，看 `seq` 是否交叉 | 改为按 `session_id` 分区 + 会话级串行 | 分区键 = 会话聚合根 |

---

## 25.7 怎么验证：顺序、堆积、rebalance 三件事

**① 顺序压测**：同一个 key 灌 10,000 条带递增 `seq` 的消息，消费端必须按 `seq` 落库：

```bash
# 灌 1 万条同 key，value 带 seq
seq 1 10000 | awk '{print "order-42\t{\"seq\":"$1"}"}' | \
  kafka-console-producer.sh --bootstrap-server broker1:9092 \
  --topic order.events --property parse.key=true --property key.separator='\t'

# 校验：seq 严格递增且无空洞
mysql -e "SELECT COUNT(*), MIN(seq), MAX(seq), COUNT(DISTINCT seq)
          FROM order_event_log WHERE order_id='order-42'"
```

**② 堆积演练**：停消费者 10 分钟，确认 lag 告警 2 分钟内触发、扩容后 5 分钟开始下降：

```bash
# kafka-consumer-groups.sh 没有 --pause/--resume；直接停消费者实例
kubectl scale deploy/order-service --replicas=0 -n trade     # 暂停
kubectl scale deploy/order-service --replicas=3 -n trade     # 恢复
# 也可以用客户端 KafkaConsumer.pause() / kill -STOP，效果相同
```

**③ rebalance 注入**：滚动重启消费者，观察 `rebalance` 次数、重复量、恢复时间：

```bash
for i in $(seq 1 6); do
  kubectl rollout restart deploy/order-service -n trade
  sleep 30
done
# 关键指标：rebalance_total、duplicate_hit_ratio、lag 恢复时间
```

**④ 对账 SQL**（发现「顺序错乱导致的非法状态迁移」）：

```sql
-- 同一订单出现「已发货 → 已支付」这种时间倒置，就是破序
SELECT order_id, status, updated_at
  FROM order_status_log
 WHERE order_id IN (
   SELECT order_id FROM order_status_log
    GROUP BY order_id
   HAVING MAX(CASE WHEN status='SHIPPED' THEN updated_at END)
        > MAX(CASE WHEN status='PAID'    THEN updated_at END)
 )
 ORDER BY updated_at DESC LIMIT 100;

-- 每个分区的消费速率（用于判断能力不足还是卡住）
SELECT partition_no,
       MAX(offset_no) - MIN(offset_no) AS consumed,
       TIMESTAMPDIFF(SECOND, MIN(created_at), MAX(created_at)) AS secs
  FROM consume_log
 WHERE created_at >= NOW() - INTERVAL 10 MINUTE
 GROUP BY partition_no;
```

---

## 25.8 用什么工具：分区、线程、位点三件事都别自研

| 能力 | 开源/框架够用 | 必须自己写 |
| --- | --- | --- |
| 分区与 rebalance | Kafka / RocketMQ / Pulsar 客户端 | key 的选择、分区数规划 |
| 分区内串行 | Spring Kafka `concurrency`、Sarama `ConsumeClaim` | listener 内禁止线程池的规范 |
| 位点提交 | 客户端 API、`onPartitionsRevoked` 钩子 | 提交时机、连续水位、重复兜底 |
| 堆积监控 | Kafka exporter、Burrow、RocketMQ Dashboard | 按分区的 lag 告警阈值、容量模型 |
| 顺序校验 | —— | 同 key `seq` 连续性对账、状态迁移合法性 SQL |
| 业务串行化 | —— | lane 路由 + 水位提交（尽量别做） |

**判断口径**：分区、线程、位点，框架都给了原语；事故几乎都来自「在框架上又加一层自研并发」。能靠 key 就别写 lane，能靠状态机就别写分布式锁。

---

## 25.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 顺序的定义 | 同一业务键的**处理顺序** = 产生顺序；不是发送顺序，也不是读取顺序 |
| 顺序公式 | 顺序 = 同 key 同分区 + 分区内单线程；吞吐 = 分区数 × 单分区能力 |
| 三种方案 | 单分区（全局有序、不能扩）、按 key 分区（99% 的选择）、业务串行化（最贵） |
| 破序① 多分区 | key 不是聚合根 / 扩分区；key 必须创建后不变 |
| 破序② 消费并发 | listener 内 `@Async`、`parallelStream` 是头号杀手 |
| 破序③ 重试队列 | 重试改变相对顺序；要么原地重试，要么版本号拒绝旧消息 |
| 破序④ 无粘性 | rebalance 迁移时旧实例还在跑；用静态成员 + revoke 提交 |
| 堆积四根因 | 能力不足（均匀涨）、单条卡住（单分区）、下游慢（速率降）、rebalance（锯齿） |
| 扩容铁律 | 实例数 > 分区数 = 白扩容；扩分区会破序，是最后手段 |
| rebalance | 停止消费但不丢消息；重复发生在 revoke 与 commit 的缝隙 |
| Agent 侧 | 同一 `session_id` 必须保序；长 Step 不能放在消费线程里，否则一个 Run 阻塞整个会话 |

## 25.10 自检题

1. **你们的 Topic 有 12 个分区，消费者扩到 20 个实例，lag 还是降不下来。列出三个最可能的原因和对应的验证方法。**
   > 要点：① 实例数 > 分区数，8 个实例分不到分区——`--describe` 看成员数与分区数；② 单条毒消息卡住某个分区——看 lag 是否只集中在一个分区、`jstack` 是否卡在同一调用；③ 下游慢导致消费速率整体上不去——看下游 P99 与连接池。三者处置完全不同：① 减实例或扩分区，② 转 DLQ + reset，③ 限流/降级/批量。

2. **为什么「消费失败进重试队列，5 秒后重试」会破坏顺序？给出两种不牺牲吞吐的修法。**
   > 要点：重试消息走的是另一条链路，原分区的后续消息不会等它，重试消息最后到达，把新状态覆盖成旧状态。修法：① 消费失败时**不提交位点、原地重试**（阻塞该分区，吞吐换顺序）；② 业务状态更新带 `version` 或 `WHERE status IN (...)`，让迟到的旧消息影响 0 行；③ 按 key 阻塞：重试消息带 `blocked_key`，消费者遇到同 key 未完成重试时暂停该 key 的后续消息。

3. **rebalance 期间消息会丢吗？会重复吗？分别说明发生在哪个时刻。**
   > 要点：不会丢——已提交位点的消息仍在 broker，未提交的会被新实例重新拉取。会重复——如果 `onPartitionsRevoked` 没有同步提交，新实例从更早的位点开始，重复量 = 已处理但未提交的条数；另外旧实例在分区迁走后仍继续处理（僵尸消费者）也会重复。防法：手动提交 + revoke 时同步提交 + `group.instance.id` 静态成员 + 业务幂等。

## 25.11 延伸阅读与交叉引用

- 重复消费与幂等落点：[第 24 章](24-delivery-semantics.md)；生产/消费端可靠性：[第 26 章](26-message-reliability.md)
- 堆积排查与消息轨迹：[第 27 章](27-message-troubleshooting.md)；限流与降级：[第 18 章](18-circuit-breaker.md)、[第 19 章](19-degradation-design.md)
- 状态机条件更新与版本号：[第 30 章](30-state-machine-modeling.md)；容量与分区规划：[第 22 章](22-load-test-capacity.md)
- Agent 侧 Run/Step 事件模型与 `seq`：[../llm/6-session-run-event-model.md](../llm/6-session-run-event-model.md)；长任务检查点：[../llm/12-long-task-durable.md](../llm/12-long-task-durable.md)
- 官方文档（以版本为准）：Kafka `ConsumerConfig` 的 `partition.assignment.strategy`、`group.instance.id`、`max.poll.interval.ms`；Spring Kafka `concurrency` 与 `AckMode`；Sarama `ConsumeClaim` 的并发语义；RocketMQ 顺序消息与 `MessageListenerOrderly`
- 总纲：[0-summary.md](0-summary.md) ｜ 语言与运行时：[../go/0-summary.md](../go/0-summary.md) ｜ 平台治理：[../llm/0-summary.md](../llm/0-summary.md)
- 前置：[第 24 章](24-delivery-semantics.md) ｜ 下一章：[第 26 章 消息可靠性：生产端与消费端](26-message-reliability.md)
