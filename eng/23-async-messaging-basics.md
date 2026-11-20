# 23. 异步解耦的本质与选型

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 异步与消息 ｜ **编号**：23 / 52
> **本章定位**：这一章不教你怎么 `send()`，只讲两件更贵的事——**为什么值得把一次调用拆成两次，以及拆完要付哪些账**。它给你「不该用 MQ」的七条判据和一份能直接贴到评审会上的选型打分表。
> **读法**：§23.2 的「三件事 / 四件事」先背下来；§23.4 的两张表评审时照着打；§23.5 只记一句话：Agent 侧唯一的实质差异是**消息体更大、投递更频繁、每条都对应一次花钱**。

---

## 23.1 引子：同一条架构决策的正反面

**事故一（该异步而没异步，2025-03）**：一个 SaaS 电商的下单接口同步扇出 7 个下游：库存(20ms)、优惠券(30ms)、积分(40ms)、短信(120ms)、埋点(15ms)、风控(60ms)、发票(90ms)。串行之和约 **375ms**，大盘 P99 **约 380ms**，看起来很健康。

某天积分服务做主从切换，P99 从 40ms 变成 **8.2s**。接下来 90 秒内发生的事情和技术无关，和线程有关：Tomcat 200 个工作线程全部堵在等积分返回上，accept 队列打满，**下单接口本身在 4 秒内就整体不可用了**——一个只值 40ms 的非核心动作，把主交易链路拖死。

- 12 分钟里下单失败 **32,000 次**；
- 用户重试成功 11,400 次，但重试路径没带幂等键，留下 **2,300 条重复积分**（三天后财务对账才发现）；
- GMV 损失约 **61 万元**，客诉 470 单。

**事故二（异步了，但只做了一半）**：他们上了 Kafka，把积分、短信、埋点改成发事件，下单 RT 从 9.4s 回到 **160ms / P99 220ms**。三个月后一次 broker 磁盘写满，生产端出现 `TimeoutException`；值班同学加了「重试 5 次 + 失败落本地重试表 + 补偿任务重投」。重试表**没有幂等键**。大促当天积分重复发放 **12.4 万分**，折算 **8.7 万元**，人工冲正做了 3 天。

> 🩸 **本章立场**：异步不会消灭问题，它把「同步世界里被线程挡住的问题」换成了「异步世界里被消息暴露的问题」。前者表现为**可用性事故**，后者表现为**数据事故**——而数据事故更贵，因为它事后才发现。

---

## 23.2 到底是什么：用一次「投递」换掉一次「调用」

**定义**：异步消息 = 把「A 直接调用 B 并同步等待返回」换成「A 把一条不可变记录写进 broker，B 在稍后的某个时刻读到它」。A 的成功不再依赖 B 的可用性，代价是 A 不再知道 B 的结果。

它解决三件事。**每件都给你量化判据，判据不满足就别上 MQ**：

| 解决的事 | 判据（满足才值得） | 不做的后果 |
| --- | --- | --- |
| **解耦** | 下游 ≥4 个，或下游的发布节奏与主链路不同（一周两次 vs 一天十次），或「B 挂了 A 还能不能成功」的答案是「必须能」 | 每加一个下游都要改主链路 + 回归；一个下游抖动就是全站故障（事故一） |
| **削峰** | 峰值/均值 > 3，且按峰值扩容的成本 > 按均值扩容的 3 倍 | 要么按峰值养机器（贵），要么峰值直接拒绝（丢单） |
| **最终一致** | 业务能容忍一个可见窗口（常见 100ms~5s，财务类可到 T+1）的不一致 | 用同步串起来 → 一致性靠「都不许失败」，链路越长越脆 |

它带来四件事，第四件最容易被漏掉：

| 带来的账 | 具体形态 | 量化 |
| --- | --- | --- |
| ① 复杂度 | 投递语义、顺序、重复、位点、重平衡、DLQ 全变成你的活 | 一个中等规模系统至少多 5 个 Topic、3 套重试策略、1 个积压看板 |
| ② 最终一致 | 「下单成功但积分没到」是常态，需要补偿与对账兜底 | 没有对账，静默错误永远发现不了（见第 14 章） |
| ③ 排查难 | 一次请求变成两段链路、两个 trace | 排障时间从分钟级变小时级（见第 27 章） |
| ④ 重复 | 至少一次语义意味着重复是**设计内的常态**，不是 bug | 重复率 0.01% 在 1000 万/天的量级上是 1000 次/天的脏数据（见第 24 章） |
| ⑤ 运维成本（隐性） | 你的第 N 个有状态中间件：磁盘、分区规划、扩缩容、版本升级、监控告警 | 没专人守的 Kafka 集群，一年内必爆一次磁盘 |

### 什么不是异步解耦（四个反例）

- **假异步**：发完消息立刻 `future.get()` 等结果。你只是把一次同步调用换成了「两次网络往返 + 一次序列化」，RT 反而更长。判据：**发送方是否依赖消费方的结果**，依赖就不是异步。
- **共享库当队列**：`status=0` 轮询表。它是异步，但它把削峰压力全压到主库上，且扫描成本随历史数据线性增长——详见第 28 章（DB 轮询的适用边界）。
- **事件驱动但消费者直接改生产者的表**：耦合还在，只是从「接口耦合」退化成「表结构耦合」，比原来更糟，因为表结构变更不会经过任何契约评审。
- **拿 MQ 做 RPC**：reply topic + correlationId 能跑，但排队延迟不可控，P99 通常比同步 RPC 差一个数量级。要同步语义就用同步调用。

> 💡 还有一类**假削峰**：Topic 只有 3 个分区，消费能力上限 8,000 msg/s，而峰值是 120,000 msg/s。此时堆积以 **112,000/s** 增长，4 分钟就是 2,700 万条——MQ 不是削峰，是把崩溃推迟了 4 分钟。

---

## 23.3 结构图：调用扇出 vs 事件流

```
【同步扇出】可用性 = 所有下游可用性的乘积；线程在等待中不可复用
              ┌──► 库存   (20ms)
              ├──► 优惠券 (30ms)
 客户端 ─► 订单 API ─┼──► 积分   (40ms → 出事时 8.2s ← 决定全站生死)
              ├──► 短信   (120ms)
              ├──► 埋点   (15ms)
              ├──► 风控   (60ms)
              └──► 发票   (90ms)
   总 RT ≈ 串行之和；200 个线程 × 8.2s 等待 = 吞吐 24 QPS

【异步事件流】可用性 = 订单 API + broker；每个消费者自己承压、自己重试
 客户端 ─► 订单 API ─┬─ 本地事务：order + outbox（同一事务，见第 10 章）
                     └─ 投递器 ─► broker(topic=order.created, key=order_id)
                                      │
          ┌───────────────────────────┼────────────────────────┐
          ▼                           ▼                        ▼
      积分消费者                   短信消费者                埋点消费者
   (lag/重试/位点/幂等各自独立)   (可延迟 5s 无所谓)        (允许丢)
```

**消息链路的五段式**（后面四章都围绕这张图）：生产端 → broker 落盘 → 位点推进 → 消费处理 → 业务库落地。故障也只会落在五段之一，第 27 章把这张图变成排查路径。

---

## 23.4 怎么实现：先判「该不该」，再选「用哪个」

### 第一步：七个「不该用 MQ」的信号

| # | 症状 | 为什么别上 | 正确解法 |
| --- | --- | --- | --- |
| 1 | 只有 1 个消费者，链路 2 跳，能塞进同一个事务 | 你在给自己加一个中间件换零收益 | 本地事务直接跑（第 7 章） |
| 2 | 「下游有点慢，想异步一下」 | 慢要治根：超时 + 隔离 + 降级（第 18、19 章）；异步化只是把「慢」变成「堆积」 | 并行调用 + 分下游超时预算 |
| 3 | 消费端**不具备幂等能力** | 至少一次语义下重复是必然，你等于给自己造脏数据 | 先建幂等骨架（第 24 章），再上 MQ |
| 4 | 读后写强依赖（付款后立刻要看余额变化） | 异步窗口内用户看到的是旧值 | 写后同步读主库，其余走异步 |
| 5 | 量极小（每天几百条） | 定时扫表 10 行代码解决，运维成本几乎为零 | 定时任务扫表（第 28 章） |
| 6 | 团队没有任何 MQ 运维能力 | 没监控的 broker 是定时炸弹（磁盘满 / 分区不均 / ISR 收缩） | 先有看板与 oncall，再谈接入 |
| 7 | 需要严格全局有序 + 高吞吐 | 全局有序等于单线程，吞吐会被锁死 | 按业务键分区保序（第 25 章） |

> ⚠️ **规矩**：「每个结论配一个『不做会怎样』」。反过来说，上面七条若不成立而你硬上 MQ，代价是**多一个 P0 故障源 + 多一个要值班的组件**，收益却常常只是「架构图好看」。

### 第二步：五个 MQ 的真实差异

只用「吞吐」对比是在骗自己。下面这张表按**你真正会踩的维度**排：

| 维度 | Kafka | RocketMQ | Pulsar | NATS (JetStream) | RabbitMQ |
| --- | --- | --- | --- | --- | --- |
| 存储模型 | 分区日志（append-only） | CommitLog + ConsumeQueue | Broker + BookKeeper（计算存储分离） | Subject 流 + Stream | 队列（Erlang，quorum queue 走 Raft） |
| 顺序保证 | **分区内**有序 | **MessageQueue 内**有序（`MessageQueueSelector` + 顺序监听器） | key 分区 / 单分区有序 | 单 subject 内 + ordered consumer | **单队列内**有序 |
| 单集群吞吐量级 | 百万 msg/s | 十万~百万 msg/s | 高（受 BookKeeper 影响） | core 百万级 / JetStream 十万级 | 万~十万 msg/s/节点 |
| 端到端延迟 | 空载 ms 级，批量下 ~10ms | 空载 ms 级 | ms 级（受读缓存命中影响） | **亚毫秒~ms**（core 无持久化时最快） | μs~ms（最低） |
| 语义与事务 | 至少一次；`enable.idempotence` + `transactional.id` 支持事务 | 至少一次 + **事务消息**（半消息+回查） | 至少一次；订阅支持按条 ack / ackTimeout 重投 | 至多一次（core）/ 至少一次（JetStream） | 至少一次（publisher confirm + consumer ack） |
| **回溯重放** | **强**：按 offset / 时间重放 | 中：`mqadmin resetOffsetByTime` | 强：按 messageId / 时间 | 中：按 sequence / 时间 | **无**：ack 即删 |
| 原生延迟消息 | **不支持**（需外挂表/时间轮/自建） | 4.x 18 个固定级别；5.x 支持任意定时 | 支持 `deliverAfter` / `deliverAt` | 不支持（用 per-message TTL 变通） | DLX+TTL 或 delayed-message 插件 |
| 单条消息上限 | 默认 ~1MB（`max.message.bytes` 可调） | 默认 4MB（可调） | 默认 ~5MB（`maxMessageSize`） | 默认 1MB（`max_payload`） | 理论大（`frame_max`），实践别超几 MB |
| 消费模型 | 拉取 + group 内 rebalance | 长轮询 push（客户端拉） | 订阅式（exclusive/failover/shared/key_shared） | push/pull，consumer 无 rebalance | push + `prefetch` |
| 多租户 | 靠 topic 命名 + client quota | namespace + ACL | **原生**（tenant/namespace/topic） | 弱（account） | vhost |
| 运维成本 | 中高（分区规划、ISR、磁盘、KRaft 迁移） | 低中（控制台友好、有消息轨迹） | **高**（Broker+BookKeeper+元数据中心三套组件） | **低**（单一二进制，无外部依赖） | 低（但集群化后队列迁移要规划） |
| 最适合 | 日志/埋点/流处理/数据管道 | 交易类业务消息、事务消息、顺序消息 | 多租户 SaaS、大堆积 + 分层存储 | 边缘/IoT/低延迟控制面 | 任务分发、复杂路由、RPC 替代 |

>  Conservatives note：上述上限都以具体版本与机型为准（Kafka 3.x/4.x、RocketMQ 4.9/5.x、Pulsar 3.x、NATS Server 2.10+、RabbitMQ 3.12+ 的 quorum queue）。**任何「单机 XX 万」都要在你的消息大小与副本数下重测**，别抄 benchmark。

### 第三步：选型打分表（评审用）

权重按「交易类业务系统」给，打分 1~5，加权平均 = Σ(分×权重)/100：

| 维度 | 权重 | Kafka | RocketMQ | Pulsar | NATS | RabbitMQ |
| --- | --- | --- | --- | --- | --- | --- |
| 吞吐 | 20 | 5 | 4 | 5 | 5 | 2 |
| 顺序 | 15 | 4 | 5 | 4 | 4 | 4 |
| 延迟 | 15 | 4 | 4 | 4 | 5 | 5 |
| 语义/事务 | 15 | 5 | 5 | 4 | 2 | 3 |
| 回溯重放 | 10 | 5 | 4 | 5 | 4 | 1 |
| 延迟消息 | 5 | 1 | 4 | 5 | 1 | 4 |
| 运维成本 | 15 | 3 | 4 | 2 | 5 | 4 |
| 生态与人才 | 5 | 5 | 4 | 3 | 3 | 4 |
| **加权（交易类）** | 100 | **4.20** | **4.30** | 4.00 | 4.00 | 3.30 |
| **加权（日志/埋点）** | 100 | **4.45** | 4.10 | 4.10 | 3.55 | 2.70 |

把权重换成「吞吐 30 / 回溯 25 / 语义 5」，Kafka 立刻反超——**打分表的价值不在分数，在于逼你把权重写出来**。写不出权重的评审，最后都是「谁熟悉谁赢」。

### 第四步：把规范变成可执行文件

Topic 不能靠口头约定。落成 YAML，纳入 code review 与自动化创建：

```yaml
# topics/order.created.yaml —— 一个 Topic 一份声明，CI 校验后由运维平台执行创建
apiVersion: mq/v1
kind: Topic
metadata:
  name: order.created            # 命名：<领域>.<事件过去式>，全小写，点分隔
  owner: trade-team              # 必须有 owner，没 owner 的 Topic 任何人都不能改
  env: prod
spec:
  partitions: 24                 # 峰值 24k msg/s ÷ 单分区 1k = 24；只为「峰值/单分区能力」定
  replicas: 3
  key:
    field: order_id              # 分区键 = 保序键 = 幂等键，三者必须是同一个
  retention: 72h                 # 常驻 3 天；超过 3 天的重放走归档（见第 40 章）
  maxMessageBytes: 262144        # 256KB，超过一律存对象存储 + 传引用（见第 27 章）
  minInsyncReplicas: 2           # 与 acks=all 配套，否则「落盘」是假落盘（见第 26 章）
  compression: zstd
  consumers:                     # 订阅关系登记：谁在消费必须可查
    - group: point-service
      idempotentKey: order_id    # 消费端幂等键，缺这项不予上线
      dlq: order.created.dlq
    - group: risk-recheck
      idempotentKey: order_id
      dlq: order.created.dlq
```

### 第五步：**先试并行，再试 MQ**

事故一的场景里，最便宜的修法根本不是 MQ，而是「并行 + 分档超时」。Go 里 30 行：

```go
// 并行扇出：同样的 7 个下游，RT = max(各下游) 而不是 sum
func (s *OrderSvc) afterPaid(ctx context.Context, o Order) []error {
	// 非核心动作：整体 300ms 预算，超了就交给补偿任务，绝不影响主链路返回
	nc, cancel := context.WithTimeout(ctx, 300*time.Millisecond)
	defer cancel()

	type step struct {
		name string
		fn   func(context.Context) error
	}
	steps := []step{
		{"stock", func(c context.Context) error { return s.stock.Confirm(c, o) }},
		{"coupon", func(c context.Context) error { return s.coupon.Lock(c, o) }},
		{"points", func(c context.Context) error { return s.points.Grant(c, o.ID) }},
	}
	errs := make([]error, len(steps))
	var wg sync.WaitGroup
	for i := range steps {
		wg.Add(1)
		go func(i int) { // 每个下游独立 goroutine + 独立错误槽位，互不掩盖
			defer wg.Done()
			errs[i] = steps[i].fn(nc)
		}(i)
	}
	wg.Wait()
	return errs // 非 nil 的交给补偿任务扫（order_id 幂等），而不是阻塞用户
}
```

改造后：RT 从「和」变成「最大值」，P99 从 9.4s → **310ms**，且积分服务再抖也不会拖死主链路。**这个版本和 MQ 版本的效果在「可用性」上等价，在「削峰」上不等价**——当峰值是均值 8 倍时才需要 MQ。这就是判据的意义。

Java 侧的对应做法是 `CompletableFuture` + 独立线程池（**绝不能共用 Tomcat 线程池**，否则隔离失效，见第 18 章）：

```java
// 每个下游一个隔离线程池；超时统一在 orTimeout 上兜
var pool = new ThreadPoolExecutor(8, 8, 60, TimeUnit.SECONDS,
        new ArrayBlockingQueue<>(64), new ThreadPoolExecutor.CallerRunsPolicy());

CompletableFuture<Void> points = CompletableFuture
        .runAsync(() -> pointClient.grant(orderId), pool)
        .orTimeout(250, TimeUnit.MILLISECONDS)
        .exceptionally(e -> { failed.add("points"); return null; }); // 失败不抛，进补偿名单
```

### 第六步：消息治理表（自研那一小块）

MQ 本身买开源，但「谁在用、谁负责、幂等键是什么」必须自己有账。一条 SQL 建账：

```sql
CREATE TABLE mq_topic_registry (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  topic           VARCHAR(128) NOT NULL,
  env             VARCHAR(16)  NOT NULL DEFAULT 'prod',
  domain          VARCHAR(64)  NOT NULL COMMENT '领域：trade/pay/marketing',
  owner_team      VARCHAR(64)  NOT NULL,
  partition_key   VARCHAR(64)  NOT NULL COMMENT '保序键=分区键=幂等键的来源字段',
  partitions      INT          NOT NULL,
  replicas        INT          NOT NULL,
  retention_hours INT          NOT NULL,
  status          TINYINT      NOT NULL DEFAULT 1 COMMENT '1=active 0=deprecated',
  created_at      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_env_topic (env, topic)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Topic 登记表：没登记的 Topic 视为违规';

-- 每周巡检：孤儿 Topic（有生产无消费）与孤儿消费者（消费了没人认领的 Topic）
SELECT r.topic, r.owner_team,
       SUM(g.consumer_group IS NULL) AS no_consumer
  FROM mq_topic_registry r
  LEFT JOIN mq_topic_group_map g ON g.topic = r.topic AND g.env = r.env
 WHERE r.env = 'prod' AND r.status = 1
 GROUP BY r.topic, r.owner_team
HAVING no_consumer > 0;
```

---

## 23.5 双修对照：同一套原理，两种形态

| 维度 | 传统系统 | Agent 系统 | 同 or 异 |
| --- | --- | --- | --- |
| **机制本体** | 解耦 / 削峰 / 最终一致：下单事件、扣款通知 | **异步 Run**（提交任务后 SSE/轮询取进度）、**Step 事件流**、工具结果回传 | **完全相同**：都是「投递一条不可变记录，由别人推进状态」 |
| **身份 / 唯一键** | `order_id` / `request_id` | `run_id` + `step_id`（+ `tool_call_id`） | 表结构直接复用，键的语义要扩一维 |
| **消息体** | 1~10KB，字段化 | **10~200KB**：内含上下文片段、工具返回、模型元信息 | **异**：单条大 10~100 倍 → 更容易撞上限（第 27 章） |
| **投递频率** | 峰值 2.4 万 msg/s | 一次 Run 产生 8~40 条 Step 事件，且模型每步都在等 | 异：**事件是高频的，且每条背后都在花钱** |
| **失败模式** | 丢 / 重 / 乱序 / 堆积 | 同上 + **重复 Step = 重复调用模型 = 重复花钱** + 上下文丢失不可重算 | 异：多了「成本」这个维度 |
| **恢复方式** | 重投消息，从位点继续 | 长任务**用 checkpoint 替代重投**：重投一次 Run 等于从第 1 步重跑，前面 20 万 token 白花 | **这是根本差异**（第 26 章展开） |
| **验证方法** | 业务表条数 vs 消息条数对账 | `token/调用次数` 对账 + Step 事件条数对账 | **框架完全相同**，只是被计量的「货」变了 |
| **观测指标** | consumer lag、produce 成功率、DLQ 水位 | 加 **Run 队列深度 / in-flight Run 数 / 单 Run 成本** | 指标集扩展 |
| **限的是什么稀缺资源** | 分区数、消费并发、DB 连接 | **TPM（token 速率）+ 模型并发 + 预算** | 算法同，被限的对象不同（第 15~17 章） |

**可以直接复用的是哪些**（一套代码两边用）：Outbox 表结构与投递器、幂等消费表、DLQ/重试 Topic 分层、位点提交策略、Topic 登记表与命名规范、积压监控看板。**必须重算的只有三样**：消息体大小的上限、事件的生产速率、以及「重试一次要花多少钱」。

> 用三定律过一遍：**状态机定律**——真相源永远在 `Run` 表/事件表，不在消息里，消息只是「变更通知」；**稀缺资源定律**——传统限分区与连接，Agent 限 TPM 与并发；**不可回滚定律**——发出去的邮件和调过的工具都不能收回，只能补偿（第 13 章）。

---

## 23.6 会在哪炸：失效模式清单

| # | 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- | --- |
| 1 | 上了「异步」，接口 RT 反而涨 3 倍 | 假异步：发完 `get()` 等回执 | 看 trace，发送 span 与消费 span 是否重叠 | 去掉同步等待，改回调或轮询状态表 | 评审加一条：异步化后 RT 必须下降 |
| 2 | 峰值期堆积 4 分钟就崩 | 分区数按「均值」定的 | `kafka-consumer-groups.sh --describe` 看每分区 lag | 扩分区（注意 key 与顺序影响）或扩消费者 | 分区数 = 峰值 ÷ 单分区能力，写进 YAML |
| 3 | 大消息时有 5% 发送失败 | 消息体超 `max.message.bytes`（默认 ~1MB） | 生产端日志找 `RecordTooLargeException` | 大字段（附件/上下文）外置，消息只传引用 | 上线前压测「最大报文」路径 |
| 4 | 两个服务互相「吃掉」对方的位点 | 复用了同一个 `group.id` | `--describe` 里 group 的成员数异常翻倍 | 改 group 名，group 名必须带服务名 | 规范：`group = <service>-<purpose>` |
| 5 | 「以为 Topic 全局有序」导致状态倒置 | 顺序只在分区内成立 | 抽样同一业务键的 offset 是否跨分区 | 用业务键做 key，或业务侧串行化 | 见第 25 章「四种破序」 |
| 6 | 积分重复发放 | 消费端无幂等键 | 对账：业务表 vs 去重表 | 补去重表 + 唯一索引 | 幂等键缺失即禁止上线（第 24 章） |
| 7 | 凌晨 2 点全站写超时 | 单分区/单 broker 成为热点 | 看分区流量分布与 broker 磁盘 | 重分 key、加分区、迁 Topic | 分区流量离群告警（>均值 3 倍） |
| 8 | 磁盘 100%，broker 只读 | `retention=-1` 或 `cleanup.policy=compact` 配错 | `du -sh` 数据目录 + Topic 用量排行 | 补 retention 与分层存储 | retention 必须显式声明且有默认值 |
| 9 | DLQ 里堆了 8 万条没人看 | 没有 DLQ 负责人 | 给 DLQ 建指标与告警 | 建人工处理队列 + 周巡检 | DLQ 增长 >100/小时即告警（第 26 章） |

---

## 23.7 怎么验证：可执行的三件事

**① 能力基线测试（上线前）**——别信 benchmark，用你的真实报文跑：

```bash
# 用真实消息大小（比如 8KB）压 100 万条，看生产端 P99 与 broker 磁盘增速
kafka-producer-perf-test.sh --topic order.created \
  --num-records 1000000 --record-size 8192 --throughput -1 \
  --producer-props bootstrap.servers=broker1:9092 acks=all enable.idempotence=true

# 消费侧限速压测：确认单分区处理能力（决定分区数）
kafka-consumer-perf-test.sh --topic order.created --messages 1000000 \
  --group perf-test --show-detailed-stats
```

**② 故障注入**（Chaos）：kill 一个 broker（验证 ISR 与 `acks=all`）、把一个消费者实例的 CPU 打到 100%（验证堆积告警在 2 分钟内触发）、`tc netem` 注入 5% 丢包（验证生产重试不产生重复）。

**③ 对账 SQL**（每日跑，发现静默丢失）：

```sql
-- 每小时的「生产条数 vs 消费落地条数」，差值持续 >0 就是丢消息
SELECT DATE_FORMAT(o.created_at, '%Y-%m-%d %H') AS hour,
       COUNT(*) AS produced,
       SUM(CASE WHEN p.order_id IS NOT NULL THEN 1 ELSE 0 END) AS landed,
       COUNT(*) - SUM(CASE WHEN p.order_id IS NOT NULL THEN 1 ELSE 0 END) AS missing
  FROM order_info o
  LEFT JOIN point_ledger p ON p.order_id = o.order_id AND p.biz_type = 'ORDER_REWARD'
 WHERE o.created_at >= NOW() - INTERVAL 24 HOUR
 GROUP BY hour
HAVING missing > 0
 ORDER BY hour DESC;
```

**④ 必须有的四个指标**：`consumer lag`（按分区）、生产端成功率与 P99、DLQ 增长率、重试 Topic 水位。四个里缺任何一个，你就是「出事 5 分钟定位不了」。

---

## 23.8 用什么工具：开源与自研的边界

| 能力 | 开源够用 | 必须自研（或必须自己配） |
| --- | --- | --- |
| Broker 本体 | Kafka / RocketMQ / Pulsar / NATS / RabbitMQ | **绝不自研** |
| 控制台与可观测 | Kafka UI / AKHQ / Redpanda Console / RocketMQ Dashboard、Prometheus exporter | 与你的 SLO、告警分级、值班表打通的那层 |
| Topic 治理 | —— | Topic 登记表 + 创建审批 + 孤儿巡检（§23.4 第六步） |
| 幂等消费 | —— | 去重组件 / 幂等注解（第 24 章），框架不给你业务幂等 |
| 重试与 DLQ | 各 MQ 的基础能力 | 重试分层、退避曲线、人工队列（第 26 章） |
| 消息轨迹与排查 | RocketMQ MessageTrace、Kafka 工具链 | 与业务 trace 串联的查询入口（第 27 章） |

**判断口径**：带状态的存储引擎一律用开源；「规范、幂等键、治理账、排查路径」这些和你的业务语义绑定的，一律自己写。**框架永远不解决业务幂等**（见 brief 硬规矩第 4 条）。

---

## 23.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 异步的本质 | 用一次「投递」换掉一次「调用」：A 的成功不再依赖 B 的可用性 |
| 三件事 | 解耦（下游 ≥4 或发布节奏不同）、削峰（峰值/均值 >3）、最终一致（可容忍可见窗口） |
| 四件事 | 复杂度、最终一致、排查难、重复——外加隐性的运维成本 |
| 假异步 | 发送方依赖消费方结果就不是异步；`get()` 等回执只会更慢 |
| 七个不该用 | 单消费者两跳、只是怕慢、无幂等能力、读后写、量极小、无运维、要全局有序 |
| 最便宜的修法 | 先试「并行 + 分档超时」，再试 MQ；两者在可用性上等价，只在削峰上不等价 |
| 顺序的边界 | 只有**分区内**有序，全局有序 = 单线程 |
| 选型 | 打分表的价值是逼你写出权重，不是那个分数 |
| 落盘真实性 | `acks=all` 必须配 `min.insync.replicas>=2`，否则「落盘」是假的（第 26 章） |
| 幂等键 | 分区键 = 保序键 = 幂等键，三者必须是同一个字段 |
| Agent 侧差异 | 消息体大 10~100 倍、事件高频、**每条事件背后都在花钱** |
| 一句话 | **异步换来的不是「更快」，是「可用性从乘积变成加法」，代价是把一致性交给你自己。** |

## 23.10 自检题

1. **你的订单系统要接入「发货后通知 6 个下游」，峰值是均值的 6 倍。你会直接上 Kafka 吗？为什么？**
   > 要点：不一定。先做判据：① 峰值/均值 = 6 > 3，削峰确实需要；② 下游 6 个且节奏不同，解耦需要；③ 关键前置是**消费端幂等能力**，没有就先建（第 24 章）。若不满足 ①，正确解法是并行 + 分档超时（§23.4 第五步），RT 从「和」降为「最大值」，且不引入新中间件。

2. **`acks=all` 就代表消息一定不丢吗？举出三个仍会丢的情形。**
   > 要点：不代表。① `min.insync.replicas=1` 时只有一个副本就算「all」；② `unclean.leader.election.enable=true` 可能选到落后副本，已提交数据被截断；③ broker 页缓存未 fsync 即宕机（操作系统层丢失）；④ 生产端超时后放弃但服务端已写入 → 这不是丢，是「重复」，方向相反。详见第 26 章八环节清单。

3. **为什么说「Agent 侧重投一条消息」的代价远高于传统系统？**
   > 要点：传统重投是「多执行一次幂等的业务逻辑」，成本是几毫秒的 CPU；Agent 侧一条消息可能对应一次 Run，重投 = 从第 1 步重跑 = 重复的模型调用 = **重复花钱**，还可能重复触发工具副作用。所以长任务要用 checkpoint 从断点恢复，而不是重投整条消息（第 26 章）。

## 23.11 延伸阅读与交叉引用

- 事务消息与 Outbox 的取舍：[第 10 章](10-outbox-and-transactional-message.md)（**本套只在这里深挖一次**）
- 幂等消费三落点：[第 24 章](24-delivery-semantics.md)；顺序与堆积：[第 25 章](25-ordering-and-lag.md)
- 生产/消费端可靠性参数：[第 26 章](26-message-reliability.md)；排查路径：[第 27 章](27-message-troubleshooting.md)
- 延迟消息与 CDC：[第 28 章](28-delayed-queue-cdc.md)；限流的层次与稀缺资源：[第 15 章](15-rate-limit-basics.md)
- 官方文档（以版本为准）：Kafka `producer_config` / `topic_config`；RocketMQ 4.9/5.x 顺序与事务消息文档；Pulsar 订阅类型与 `deliverAfter`；NATS JetStream 模型；RabbitMQ quorum queue 与 publisher confirm
- 总纲：[0-summary.md](0-summary.md) ｜ 语言与运行时：[../go/0-summary.md](../go/0-summary.md) ｜ 平台治理：[../llm/0-summary.md](../llm/0-summary.md)
- 前置：[第 22 章](22-load-test-capacity.md) ｜ 下一章：[第 24 章 投递语义与重复消费](24-delivery-semantics.md)
