# 30. 消费组与 rebalance：协议、位点与积压

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 消息、流与定时 ｜ **编号**：30 / 53
> **本篇定位**：全仓唯一一处把「消费组」拆到协议状态机与位点记录级的篇目。日志在磁盘上长什么样（segment、稀疏索引、零拷贝）归 [comp/28](28-kafka-log-segments.md)，ISR / HW / LEO 与 acks 归 [comp/29](29-kafka-replication-isr.md)，本篇对它们只给一句结论 + 锚点，一个字不重写。投递语义与幂等落点归 [eng/24 投递语义](../eng/24-delivery-semantics.md)，顺序与堆积的处置手册归 [eng/25 顺序、堆积与重平衡](../eng/25-ordering-and-lag.md)，可靠性八环节归 [eng/26 消息可靠性](../eng/26-message-reliability.md)，选 Kafka 还是别的消息形态归 [architect/15 消息存储](../architect/15-message-store.md)。本篇只回答一个问题：**一个组的三张表（成员表 / 分配表 / 位点表）是怎么存的、rebalance 时谁停下、位点在哪一刻被写下去，以及这三个答案如何分别推出「停顿时长」「重复与丢失」「积压消化时间」**。
> **读法**：救火读 §30.8 的四张判定表与 §30.11 的坑清单（先判断「是在 rebalance 还是真积压」）；做容量与稳定性方案直接抄 §30.9 的三本账（每本都带乘法链与假设清单）；被追问「为什么扩消费者没用」「为什么只丢了一小段」「为什么发布就掉吞吐」读 §30.6 与 §30.7；只想要结论翻 §30.13。

---

## 30.1 引子：三起「消费组一切正常，数据就是不动 / 就是少了」

消费组的故障有一个统一形状：**指标全是绿的**。组状态 `Stable`、成员数没变、`records-lag-max` 有时甚至因为分区被撤销而变成 0、位点提交延迟个位数毫秒——因为「消费组正常」只承诺一件很窄的事：**这群成员现在对这三张表意见一致**。它不承诺处理完了、不承诺不重复、不承诺均衡。

### 事故一：滚动发布把消费停掉 20 分钟，根因是 `max.poll.interval.ms` 用了默认值（2026-03-11）

某履约域，Topic `order.fulfill.events`，**60 分区，20 个消费 pod**，Spring Kafka，`enable-auto-commit: false`，`max-poll-records: 500`。`session.timeout.ms` 被显式设成 30000（半年前有人「为了加快故障发现」调小过），`heartbeat.interval.ms` 与 `max.poll.interval.ms` 都**没有显式设置，走客户端默认量级**（前者 3 s 量级，后者 5 分钟量级，以目标版本客户端默认为准）。

当天 10:05 起，下游库存服务的 P99 从 40 ms 抬到 3.2 s。单批 500 条的平均处理时长从 400 ms 涨到 **6.5 分钟**。

| 时刻 | 事件 | 组状态 | 单批处理时长 | 消费者侧 |
| --- | --- | --- | --- | --- |
| 10:05 | 下游库存 P99 40 ms → 3.2 s | Stable | 0.4 s → 45 s | 正常 |
| 10:31 | 一批遇到库存服务的重试退避，单批耗时 **6.5 min > `max.poll.interval.ms`（5 min 量级）** | Stable | 6.5 min | **客户端自己发 LeaveGroup**（不是被踢，是被它自己的 poll 间隔判定赶走） |
| 10:31 | 该成员离开 → coordinator 进 PreparingRebalance → 另外 19 个成员全部停下 | **PreparingRebalance** | — | 60 个分区**全部停止 fetch** |
| 10:31 | 重分配把该 pod 的分区摊给别的 pod，别的 pod 的单批处理时长被推高 | CompletingRebalance → Stable | 45 s → 100 s | 追平失败 |
| 10:34 / 10:41 / 10:52 / 11:06 | 又有 4 个成员先后越过 5 分钟，各触发一轮全量 rebalance | 反复 PreparingRebalance | — | 每轮停 3 s ~ 300 s |
| 11:12 | 运维把上游流量临时切到降级队列，下游恢复 | Stable | 8 s | 追平 |
| 11:15 | 停消费累计 **20 分钟**，lag 峰值 **1,866 万条 ≈ 9.3 GB** | Stable | — | 追赶 ≈104 分钟（约 13:00 追平） |

**这 20 分钟里，`UnderReplicatedPartitions` 是 0、broker 侧 `Produce` 失败率是 0、组状态在两次崩溃之间一直是 `Stable`。** 根因不是 Kafka，是把「客户端侧的 poll 间隔超限」当成了「网络/协调器问题」，以及一个从没算过的数：**单批 500 条 × 单条耗时 = 一个必须小于 `max.poll.interval.ms` 的数**。

代价：20 分钟停消费 × 平均 1,875 单/分钟 = 37,500 单履约时效违约（15,000 msg/s ÷ 480 条事件/单 ≈ 1,875 单/分钟，量级自洽），SLA 赔付 + 加急物流 **46 万元**；追赶期间下游库存被重复扣减 2,100 次（重投 + 未幂等，见 [eng/24](../eng/24-delivery-semantics.md)）。

### 事故二：位点提交在处理之前，一次 OOMKilled 丢了 12,000 条支付事件（2026-01-22）

某支付域，Topic `pay.event`，48 分区。消费代码长这样：listener 拿到一批 500 条，**丢进线程池异步处理，方法立即返回**。于是 `poll()` 循环转得飞快，看起来「消费很健康」。位点用**框架默认的自动提交**（`enable.auto.commit=true`，提交间隔 5 s 量级）。

自动提交提交的是「**上一次 poll 返回的最后一条的下一个位点**」——它完全不知道线程池里还有多少条没跑完。

| 时刻 | 事件 | 已提交位点 | 已处理位点 | 差额 |
| --- | --- | --- | --- | --- |
| 峰值稳态 | 单 pod 900 msg/s，线程池队列最多压 24 个任务 × 500 条 | 跟随 poll | 落后 0–12,000 条 | **最多 13 秒的处理量** |
| 04:10:12 | 该 pod 的堆被打满（队列里全是待处理消息体），被内核 **OOMKilled** | 已提交到「已拉未处理」的位置 | 落后 12,000 条 | **12,000 条永远不会被处理** |
| 04:10:20 | 组重新分配分区，新 pod 从**已提交位点**继续 | — | — | 那 12,000 条被跨过 |
| 04:11 | 另一个 pod 因同样原因被杀（内存超卖） | — | — | 再丢 8,000 条 |
| 09:30 | 对账发现支付成功但未发货 | — | — | — |

代价：20,000 条事件中 2,300 条是真实支付成功，货款已收、订单未推进；人工补数 96 人时，顺带产生 1,100 笔重复退款审核，直接损失 **37 万元**。

**关键点不是「自动提交有罪」，而是「提交时机 = 语义」这件事被一个异步线程池悄悄改写了**：代码看起来是「先处理后提交」（at-least-once），实际生效的是「先提交后处理」（at-most-once）。二者之间没有第三种免费选择（§30.6）。

### 事故三：lag 面板报「预计 26 分钟消化完」，实际永远消化不完（2025-12-08）

某营销域，Topic `marketing.campaign.events`，48 分区，key = `merchant_id`，24 个消费者。大促当天峰值入流 **12,000 msg/s**，其中一个大商户贡献 **62%**（它的 `merchant_id` 恒定，全部落到同一个分区）。

自研面板的 ETA 口径是「**总 lag ÷ 总入流量**」，SLA 阈值 30 分钟。

| 时刻 | 总 lag | 3 个热分区 lag | 冷分区 lag | 面板 ETA | 面板颜色 |
| --- | --- | --- | --- | --- | --- |
| 20:00 | 400 万 | 各 5 万 | 45 个分区共 385 万 | 5.6 分钟 | 绿 |
| 20:40 | 1,900 万 | 各 380 万 | **0（已清空）** | 1,900万 ÷ 12,000 = **26.4 分钟** | **绿（差 3.6 分钟到阈值）** |
| 21:20 | 3,800 万 | 各 1,180 万 | 0 | 52.8 分钟 | 黄 |
| 22:00 | 5,700 万 | 各 1,780 万 | 0 | — | 红（此时才报） |
| 02:40 | 扩消费者 24 → 36，lag 只降了 6% | — | — | — | — |
| 03:10 | 大商户单独拆 topic（24 分区、key 换成 `order_id`）+ 消费端限流 | 开始下降 | — | — | — |
| 06:40 | lag 归零 | — | — | — | — |

三个热分区的入流是 12,000 × 62% ÷ 3 = **2,480 msg/s/分区**，而单分区消费上限是 **900 msg/s**（单线程 + 同步写 DB）。于是这三个分区每秒净增 1,580 条：**任何一个「消化时间」公式在这里都必须返回 +∞**。面板之所以在 20:40 还是绿的，是因为 45 个冷分区被清空后把总量拉低了——**总 lag 在跌，热分区在涨**。

代价：端到端延迟 6 小时 40 分钟，SLA 违约赔付 **31 万元**；而两轮「扩容消费者」一共花了 4 小时，收益是 0。

> 🩸 **三起事故的共同形状**：没有一处代码写错、没有一次报错。① 是「单批耗时 × 条数」从没对着 `max.poll.interval.ms` 算过；② 是「提交时机」被异步线程池改写了语义；③ 是「并行度」被一个 key 的分布钉死，而面板用总量口径把它平均掉了。**三件事都在配置与口径层，所以都不在 code review 里。**

---

## 30.2 一句话定位与唯一真问题

**定位**：消费组是「**一组进程共同消费一个 topic 的分区，由一个 coordinator 维护三张表**」的机制：

| 表 | 内容 | 存在哪 | 谁写 | 变了会怎样 |
| --- | --- | --- | --- | --- |
| **成员表**（group metadata） | generation、protocol、leader、members[]（member id、订阅、静态成员 id） | `__consumer_offsets` 的一个 compact 记录 + coordinator 内存缓存 | coordinator | 成员增减 ⇒ 触发 rebalance |
| **分配表**（assignment） | 每个 member 分到哪些 `topic-partition` | **不落盘**：组 leader 算完经 SyncGroup 下发，coordinator 只转发 | 组 leader（客户端） | 重新计算 = rebalance 本体 |
| **位点表**（committed offset） | 每个 `(group, topic, partition)` 提交到哪个 offset | `__consumer_offsets` 的 compact 记录 + 缓存 | 消费者（经 coordinator） | 位点错了 = 重复或丢失 |

三张表里只有两张落盘，而「分配表」不落盘这件事本身决定了 rebalance 的形态：**分配是协商出来的，不是存下来的**——所以任何成员变动都必须重新协商，而不能「改一条记录」。

### 唯一真问题

> **rebalance 是「所有人停下重分家务」，位点提交时机就是语义；积压不是容量问题，是并行度问题。**

三个推论，全篇都在给它们找证据：

1. **rebalance 不是平滑迁移。** 它的停消费时长 ≈ `轮数 × 单轮时长`，而单轮时长有两个数量级：正常 3 s 级，一旦有人越过 `max.poll.interval.ms` 就是 300 s 级（§30.9 账一）。
2. **位点提交时机就是语义，二者不可兼得。** 交在「处理之前」= at-most-once（丢窗口 = 未处理的批）；交在「处理之后」= at-least-once（重复窗口 = 未提交的批）。想要「都不」就得进事务，那是另一条路（[eng/11](../eng/11-exactly-once-truth.md)、[eng/24](../eng/24-delivery-semantics.md)）。
3. **积压是并行度问题。** 磁盘不参与决策（日志形态见 [comp/28](28-kafka-log-segments.md)，一条消息在盘上占多少字节与 lag 无关），并行度 = `min(消费者数, 分区数)`，且实际被**最热分区**封顶（§30.9 账三）。

**它明确不承诺什么**：不承诺跨分区有序；不承诺 rebalance 期间不停消费（eager 一定停、cooperative 只停被迁的分区）；不承诺「扩消费者就提吞吐」；不承诺位点永不过期；不承诺「提交了位点 = 处理完了」。

### 解剖卡八问落点（本篇答案）

| 问 | 落在哪节 | 一句话答案 |
| --- | --- | --- |
| ① 承诺什么 | §30.6、§30.10 | 承诺「同一时刻一个分区只有一个成员」+「成员对分配表意见一致」；**不承诺处理完、不承诺不重复、不承诺不停顿** |
| ② 数据长什么样 | §30.3 | 两张 compact 表放在内部 topic `__consumer_offsets` 的 50 个分区里，`hash(group) % 50` 的 leader 就是 coordinator；分配表不落盘 |
| ③ 一次读写走哪条路 | §30.3、§30.4 | `poll → fetch → 处理 → commit(内部 topic append) `；rebalance 走 `JoinGroup → SyncGroup → Heartbeat` 三阶段 |
| ④ 并发与一致 | §30.4、§30.5、§30.6 | 并发单位是**分区**，一致性靠 **generation id** 把老成员挡在门外（`ILLEGAL_GENERATION`）；失效窗口是三个超时 |
| ⑤ 扩展维度 | §30.7、§30.10 | **四选一里的「分片」**：并行度只能靠分区数（而分区数决定 key 映射）；副本（[comp/29](29-kafka-replication-isr.md)）买可用性，买不到消费吞吐 |
| ⑥ 系统级失效 | §30.9、§30.10 | 三类：**假活**（进程活着、组 Stable、数据不动）、**语义漂移**（提交时机被异步改写 ⇒ 静默丢）、**成本失控**（热分区 ⇒ 无限扩容与无限积压） |
| ⑦ 看哪几个指标 | §30.12 | 分区级 lag 的 **max 与分布**（不是平均）、`rebalance-rate-per-hour` / `rebalance-latency-max`、`commit-latency-max`、`poll-idle-ratio-avg`、成员数与 generation |
| ⑧ 换掉它的信号 | §30.10 | 需要「消费侧别再停」「单分区不够用」「要 serverless 弹性」→ 换 server 端分配的协议形态（KIP-848 方向）或换存储解耦型队列（`→ comp/31`）；形态决策见 [architect/15](../architect/15-message-store.md) |

---

## 30.3 物理形态：`__consumer_offsets`、coordinator 与组状态机

### 三张表里两张落在一个内部 topic 上

`__consumer_offsets` 是 Kafka 自己创建的内部 topic：**默认 50 分区**（`offsets.topic.num.partitions`）、`cleanup.policy=compact`（同 key 只留最新一条）、副本因子由 `offsets.topic.replication.factor` 决定（**以目标版本 broker 的默认值为准**，早期版本的默认值更小，自动创建的内部 topic 常常只有 1 副本——这是「换一台 broker 组位点全丢」的经典入口）。段大小由 `offsets.topic.segment.bytes` 控制，量级与普通 topic 不同，**以目标版本文档为准**。

```text
__consumer_offsets（内部 topic：默认 50 分区 / compact / RF 由 offsets.topic.replication.factor 定）

  group.id = "order-fulfill"
        │
        │  partition = abs(hash(group.id)) % 50     ← coordinator 就住在这个分区的 leader 上
        ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ __consumer_offsets-17      （leader 在 broker B2）             │
  │                                                               │
  │  记录 A（位点，每个 group×topic×partition 一条）                 │
  │    key   = [version][group][topic][partition]                  │
  │    value = offset(8B) + leaderEpoch + metadata +               │
  │            commitTimestamp + expireTimestamp                   │
  │                                                               │
  │  记录 B（组成员表，每个 group 一条）                             │
  │    key   = [version][group]                                    │
  │    value = generation + protocolType + protocol +              │
  │            leader(memberId) + members[](memberId, 订阅,         │
  │            group.instance.id, rebalanceTimeout)                 │
  │                                                               │
  │  同 key 的新记录追加在末尾，compaction 只保留最新的一条            │
  │  ⇒ 「一次位点提交」= 一次 append，不是一次 UPDATE                 │
  └──────────────────────────────────────────────────────────────┘
        │
        ▼
   B2 上运行该组的 coordinator：
     · 内存里维护 GroupMetadata（成员表 + 状态机）与 offset cache
     · 「提交位点」= 消费者 → coordinator → append 到上面那个分区
     · 「读位点」= 大多数情况直接命中 coordinator 内存缓存（热路径不碰盘）
```

**读这张图要拿走的三件事：**

1. **位点提交是 append，不是 UPDATE。** 与 [comp/28](28-kafka-log-segments.md) 的只追加形态同源：一次提交的目录成本极低，但每条记录都落在内部 topic 上，靠 compaction 收掉旧的——所以「提交频率 × 10」放大的是内部 topic 的写入与 compaction 压力（§30.9 账二）。
2. **coordinator 的定位是纯算术。** `hash(group.id) % 50` 那个分区的 leader 在哪台 broker，该组的 coordinator 就在哪台——**一个组的全部成员都打同一台 broker**（大组会把单台打满）；这个分区的 leader 迁移 = coordinator 搬家 → 全体重找 coordinator（`FindCoordinator`）→ 一次额外 rebalance。
3. **分配表不落盘，位点会过期。** 分配只能重新协商、不能增量修补，也没有任何持久化状态能告诉你「上次谁负责 P3」；而 `offsets.retention.minutes` 控制无成员组（`Empty`）的位点保留时长（默认量级 7 天，**以目标版本为准**，该默认值在历史版本间改过）。位点过期后这个组再消费会按 `auto.offset.reset` 从头/从尾开始——这是「重跑」与「全量重投」之间的那扇门（§30.6）。

### 组状态机：五个状态，只有一个是「在干活」

```text
                     ┌──────────┐
    新组（首个成员）   │  Empty   │◄────────────────────────┐
                     └────┬─────┘                         │
          JoinGroup       │                               │ 成员全部离开
                          ▼                               │ + 位点过期 / DeleteGroups
                  ┌────────────────────┐                   │
                  │ PreparingRebalance │───────────────────┤
                  │ 等全部成员重新加入   │                   │
                  └────┬───────────────┘                   │
      全员到齐 / │ rebalance timeout                        │
                ▼                                          │
        ┌──────────────────────┐   成员中途退出/超时          │
        │ CompletingRebalance  │──────────┐                │
        │ 等 leader 算好分配   │           │                │
        └────┬─────────────────┘           │                │
             │ SyncGroup 下发（generation + 1）              │
             ▼                            │                │
        ┌──────────┐  心跳正常 / 位点提交   │                │
        │  Stable  │───────────────────────┴────────────────┘
        └────┬─────┘
             │ DeleteGroups（组被删）或组迁移完成
             ▼
        ┌──────────┐
        │   Dead   │  不可再使用：要么换 group.id，要么重建
        └──────────┘
```

| 状态 | 含义 | 期间成员还能消费吗 | 谁会把它推进到下一个状态 | 观测意义 |
| --- | --- | --- | --- | --- |
| `Empty` | 组存在、位点还在，但没有成员 | 无成员可消费 | 第一个成员 `JoinGroup` | 大面积 `Empty` = 消费者没起来（部署/依赖失败），不是 Kafka 问题 |
| `PreparingRebalance` | 有成员，正在等所有人重新加入 | **eager 下全部停 fetch**；cooperative 下只停被迁走的分区 | 全员到齐或 `rebalance.timeout` 到期 | 这里停留时间 = 停消费时长的主体（§30.9 账一） |
| `CompletingRebalance` | 全员已加入，等 leader 上传分配方案 | **停**（仍不发新分配） | 组 leader 的 `SyncGroup` | 卡在这里 = leader 算得慢或 SyncGroup 丢包重试 |
| `Stable` | 分配已下发，成员按分配拉取 | 是 | 成员变动/超时/订阅变化 → 回 `PreparingRebalance` | 唯一「在干活」的状态 |
| `Dead` | 组被删除或迁移中 | 否 | 只能是新组 | 出现即意味着「这个 group.id 的历史位点没了」 |

> 老版本客户端里还能看到一个 `Assigning` 状态名（早期把「选策略」和「下发方案」拆开过），后来统一并入 `CompletingRebalance`；**具体状态集合以目标版本的协议文档与 `--describe --state` 输出为准**。

### generation、member id 与 leader：三把用来挡人的锁

| 概念 | 谁分配 | 生命周期 | 用来干什么 | 失效时的报错 |
| --- | --- | --- | --- | --- |
| **generation id** | coordinator，每次 rebalance 成功 +1 | 组级 | **把上一代的成员挡在门外**：心跳与位点提交都带 generation，旧值一律被拒 | `ILLEGAL_GENERATION`（旧客户端可能是 `UNKNOWN_MEMBER_ID`） |
| **member id** | coordinator | 成员级，随会话 | 标识「这次会话里的这个成员」。历史上首次加入会先拿到一个"空 id + 要求重试"的应答（KIP-394 引入的机制，只讲机制不提版本号） | `UNKNOWN_MEMBER_ID` |
| **group leader** | coordinator 选（通常是先加入的成员） | 每代 | **唯一负责计算分配表的成员**（coordinator 只转发，不算分配） | leader 挂了 → 下一次 rebalance 重选 |
| `group.instance.id` | 应用自己给（静态成员，KIP-345 机制） | 跨会话 | 让 coordinator 把「同一个 instance id 的重连」认成老成员回来，**在 `session.timeout.ms` 内不触发 rebalance** | `FENCED_INSTANCE_ID`（另一个成员抢了同一个 id） |

一句话把这段收掉：**generation 是 rebalance 的「朝代」，成员、心跳、位点提交三样东西都必须报出自己属于哪一朝；报错朝代的写入会被丢弃——这正是「rebalance 期间提交位点会失败」的物理原因，也是重复量的主要来源。**

---

## 30.4 rebalance 三阶段：JoinGroup → SyncGroup → Heartbeat

rebalance 不是一个动作，是**三轮请求 + 一个持续的心跳**。理解这三轮各自携带什么、失败会怎样，就能把 90% 的「rebalance 为什么这么久」拆成可算的部分。

| 阶段 | 谁发起 | 携带什么 | coordinator 做什么 | 失败/超时的后果 |
| --- | --- | --- | --- | --- |
| **① JoinGroup** | 每个成员 | 订阅的 topic 列表、`group.instance.id`、支持的**分配策略列表**（按偏好排序）、`rebalanceTimeout` | 把成员加进成员表；**选一个所有成员都支持的策略**（公共交集里按偏好取一个）；选 leader；把全体成员元数据回给 leader；generation +1 | 有成员没来齐 → 等到 `rebalance.timeout`（取成员的 `max.poll.interval.ms` 量级）→ 把它踢掉再继续；**这一段就是「有人卡住，全组一起等」的时长** |
| **② SyncGroup** | 全体（leader 带方案，其它带空） | leader：`{memberId → [topic-partition]}` 全量分配 | 把方案存进内存、按成员切成各自的切片下发 | leader 迟迟不给方案 → 全组停在 `CompletingRebalance`；其它成员重试直到 `rebalance.timeout` |
| **③ Heartbeat** | 每个成员，周期性 | `memberId + generation` | 只做三件事：续命、**校验 generation**、在需要时回 `REBALANCE_IN_PROGRESS` 让成员重新加入 | 超过 `session.timeout.ms` 没心跳 → 移出成员表 → 触发 rebalance；回 `REBALANCE_IN_PROGRESS` → 成员主动重新 join |

### 三个超时的分工（这是本篇最容易被配错的地方）

| 参数 | 谁监测谁 | 默认量级（以目标版本客户端/ broker 默认为准） | 它管的是 | 调大的代价 | 调小的代价 |
| --- | --- | --- | --- | --- | --- |
| `session.timeout.ms` | **coordinator 监测成员的心跳线程** | 45 s（较新版本；早期版本是 10 s 量级，确实改过默认值） | 「这个成员还活着吗」 | 真死的成员更久才被移走，它的分区**无人消费**（lag 照涨）；且与 broker 的 `group.max.session.timeout.ms` 上限相撞 | 一次 GC 停顿或网络抖动就把成员踢掉 → 误触发 rebalance |
| `heartbeat.interval.ms` | 成员自己定时发 | 3 s | 心跳频率 | 超出 `session.timeout.ms / 3` 就意味着「一次丢包就掉线」 | 心跳请求量上升（每成员 × 组数 × broker） |
| `max.poll.interval.ms` | **客户端自己监测自己的 poll 间隔** | 5 min 量级 | 「这个成员还在处理上一批吗」 | 真死的消费者要更久才被发现（但它一旦死，进程没了心跳也会停，两条路都会走到）；**单批慢会被容忍更久** | 一批稍慢就被判超限 → **客户端自己发 `LeaveGroup`** → 全组 rebalance（事故一的形态） |

三条必须记住的关系式（写成不等式，评审时直接抄）：

```text
heartbeat.interval.ms  ≤  session.timeout.ms / 3       ← 允许丢两次心跳
max.poll.interval.ms   >  max(单批最坏处理时长) × 2      ← 留一倍余量给抖动
rebalance 超时上限     ≈  max(成员的 max.poll.interval.ms)  ← 慢成员决定全组的等待上限
```

**最容易搞错的一句话**：`session.timeout.ms` 调大**解决不了**「处理太慢导致的 rebalance」——因为那条路是 **客户端主动离开**（poll 间隔超限），coordinator 根本没机会「不踢你」。反过来，调大 `max.poll.interval.ms` 也解决不了网络抖动导致的踢人。**两个超时管两条不同的命**，事故一里 4 轮 300 s 级的停顿全部来自后者被误当成前者。

### 触发 rebalance 的完整清单（按「能不能避免」排序）

| 触发源 | 是否可避免 | 停消费量级 | 应对 |
| --- | --- | --- | --- |
| 新成员加入（扩容） | **不能**（除非不改成员数） | 一轮 | 一次加够；用 cooperative 策略把停消费压到「只迁走的分区」 |
| 成员正常退出（SIGTERM / 优雅停机） | 部分可以 | 一轮 | 停机前 `close()` 会**主动离开**（快），能显著缩短下一轮等待 |
| 成员崩溃 / 被驱逐 | 不能 | 一轮，且要等 `session.timeout.ms` 才发现 | 静态成员 + 快速重启（同 `group.instance.id`）可免掉这一轮 |
| poll 间隔 > `max.poll.interval.ms` | **可以**（限批大小与单批耗时） | 一轮，且往往是 300 s 级 | §30.8 判定表三 |
| 心跳停 > `session.timeout.ms` | 部分可以（调 GC、网络） | 一轮 | 别把 `session.timeout` 调到接近 GC 停顿 |
| 订阅的 topic 列表变化 / 正则匹配到新 topic | 可以（避免正则订阅） | 一轮 | 用显式 topic 列表，别用 `subscribe(Pattern)` |
| 分区数增加 | 不能（扩容分区的必然结果） | 一轮 | 一次扩到位；扩分区会改 key→分区映射（§30.7） |
| `__consumer_offsets` 某分区 leader 迁移 ⇒ coordinator 搬家 | 不能（集群运维引起） | 一轮 + 一次 `FindCoordinator` | 与 broker 变更窗口错开 |
| 组被删除 / 位点过期后重建 | 不能（人为） | 从 `Empty` 重新开始 | 见 §30.6 的 `auto.offset.reset` 陷阱 |

---

## 30.5 eager vs cooperative，以及四种分配策略的代价

### 两种「重分家务」的方式

```text
eager（Range / RoundRobin / Sticky）：全组停 → 全部撤销 → 再全部发

  C1  ▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░▓▓▓▓▓▓▓▓▓▓▓▓        ▓=在 fetch  ░=停
  C2  ▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░▓▓▓▓▓▓▓▓▓▓▓▓
  C3  ▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░▓▓▓▓▓▓▓▓▓▓▓▓
             └ revoke 全部 ┘└Join┘└Sync┘└ 预热 ┘

cooperative（CooperativeSticky，KIP-429 的增量协作机制）：只撤销「必须迁走」的分区

  C1  ▓▓▓░░▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓        ← 只 revoke P5，其余继续 fetch
  C2  ▓▓▓▓▓▓▓▓▓░░░▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
  C3  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░▓▓▓▓▓▓▓▓▓▓        ← 可能还要第 2 轮才均衡
             └ 第 1 轮：只撤必须迁走的 ┘  └ 第 2 轮：补齐 ┘
```

| 维度 | eager | cooperative |
| --- | --- | --- |
| 谁停 | **全组全部分区** | 只有「被迁走」的分区 |
| 一轮够吗 | 够（一次分完） | **可能不够**：需要 2 轮以上才能收敛到均衡（第一轮只让必要的部分让出来） |
| 停消费时长的量级 | 全组 × 一轮（事故一里被拉到 300 s/轮） | 单分区的迁移时间（毫秒级到秒级） |
| 状态机的可观测性 | 简单：`PreparingRebalance` 期间全停 | 复杂：多轮 rebalance 会让 `rebalance-rate-per-hour` 短期飙升，容易被误判为"出了什么大事" |
| 语义风险 | 无（停了一切重新开始） | **需要客户端与 broker 协议能力都支持**；混用两种策略的组会在滚动升级期间来回抖动 |
| 适合 | 老客户端、状态极重（一次重启重建所有本地缓存也行）的批处理 | **几乎所有在线消费组**；尤其状态存储型（Kafka Streams 类）与滚动发布频繁的服务 |

> ⚠️ 从 eager 迁到 cooperative 不能一步到位（协议不能混用），必须分阶段滚动：先让所有成员都**声明支持**新策略，再切换默认策略。**具体迁移步骤与兼容窗口以目标版本客户端文档为准**——这一段做错的表现是「发布期间组在两种协议间反复 rebalance」，看起来像不稳定，实际是协议协商失败。

### 四种分配策略：算法差异决定「倾斜」与「迁移量」

| 策略 | 算法 | 均衡性 | rebalance 时的迁移量 | 真正的代价 | 什么时候必须用 |
| --- | --- | --- | --- | --- | --- |
| **range** | 每个 topic **单独**把所有分区排序，按成员数切块 | 差：`P` 不能被 `C` 整除时，前几个成员多拿一个（4 分区 3 成员 = 2/1/1，差 2 倍）；多 topic 订阅会叠加倾斜 | 大（重排后几乎全变） | 倾斜直接变成「有人忙死、有人空转」，与热分区叠加时最坏 | 需要「同索引分区落在同一成员」做本地 join 的场景；不建议作为在线默认 |
| **roundrobin** | 把所有订阅到的 `topic-partition` 打平成一张表，依次发 | 好（差最多 1 个分区） | 大 | 打散 co-partition 局部性；**成员订阅集合不一致时会分配错位** | 各 topic 分区数接近、无本地 join 需求 |
| **sticky** | 先尽量保留上一轮的分配，再把多出来的分区挪给空的人 | 好 | **小**（这是它的全部目的） | 仍是 **eager**：迁移量小了，但「全组停」一点没省 | 成员频繁变（`max.poll` 边缘）且有重本地状态 |
| **cooperative-sticky** | sticky 的增量版：把「必须迁走的分区」先撤销，分两轮收敛 | 好（收敛后） | **最小** | 需要多轮；协议兼容要求；第一轮结束时的分配**不是均衡的**（面板上会看到短暂的倾斜，别当故障） | 在线服务的默认选择 |

**一句话**：`range` 的问题叫「倾斜」，`sticky` 的问题叫「还是得停」，`cooperative-sticky` 的问题叫「要多转两圈」。**没有一种策略能同时做到「不停」「一次均衡」「零迁移」**——这就是分布式分配的老三角。

---

## 30.6 位点提交：语义的分界线就在这一行代码上

### 提交的是什么：一个「下一条」的位点

Kafka 的位点语义是「**下一条要消费的 offset**」，不是「最后处理的那条」。所以处理完 offset=100 的记录后，要提交 `101`。这个 `+1` 是「丢掉一整批」与「重复一整批」之间最短的一段距离，几乎每个团队都在这上面栽过一次。

### 三种提交方式的真实差别

| 方式 | 何时提交 | 谁决定位点 | 失败会怎样 | 语义 | 代价 |
| --- | --- | --- | --- | --- | --- |
| **`enable.auto.commit=true`** | 在 `poll()` 内部周期性提交（`auto.commit.interval.ms` 默认 5 s 量级） | **框架**，提交「上次 poll 返回的最后一条 +1」 | 静默：可能提交了没处理完的位置 | **at-most-once**（只要你的处理不是同步的；事故二） | 零代码，最贵 |
| **`commitSync()`** | 你自己决定 | 你 | 抛可重试异常，内部重试；最终失败要抛给业务 | **at-least-once** | 每次一个 RTT + coordinator 一次 append；**放在 poll 循环里会拖慢拉取** |
| **`commitAsync()`** | 你自己决定 | 你 | 回调里给异常，**不重试**（避免旧位点覆盖新位点） | at-least-once，但**提交可能回退** | 不阻塞；必须配「周期同步提交」或「revoke 时同步提交」兜底 |
| **异步 + 末尾同步** | 两者混合 | 你 | 异步失败被同步兜住 | at-least-once | 实践中性价比最高：吞吐与正确性各让一步 |
| **事务（`sendOffsetsToTransaction`）** | 随事务提交 | 事务协调器 | 事务中止 ⇒ 位点与输出一起回滚 | **exactly-once（仅限「读 Kafka → 写 Kafka/幂等 Sink」这条链）** | 一个事务协调器 + 两阶段；见 [eng/11](../eng/11-exactly-once-truth.md) |

### 语义分界表：提交点放在处理之前还是之后

| 处理模式 | 提交点 | 崩溃时重复量 | 崩溃时丢失量 | 适用 |
| --- | --- | --- | --- | --- |
| 同步处理，**每批处理完就提交** | 处理之后 | ≤ 1 批（且被 `max.poll.records` 封顶） | **0** | 账务、订单、状态机 |
| 同步处理 + 异步提交 + revoke 时同步提交 | 处理之后（略有滞后） | ≤ 提交窗口 | 0（revoke 时兜住） | 高吞吐且下游幂等 |
| 异步处理（线程池 / 队列） + 自动提交 | **处理之前（事实上）** | 0 | **队列内全部** | ★ 反模式（事故二） |
| 先提交再处理（显式） | 处理之前 | 0 | ≥ 1 批 | 可丢的指标流、审计采样 |
| 处理 + 事务提交 | 与输出同事务 | 0 | 0 | 流式 ETL（Kafka→Kafka） |

一句话把这张表收掉：**你要么在「重复」上付钱，要么在「丢失」上付钱；把提交点往后挪只买到重复，挪到前面只买到丢失，挪成事务才买到「都不」，代价是吞吐与拓扑限制（[eng/24](../eng/24-delivery-semantics.md)）。**

### `auto.offset.reset`：它不是「重跑开关」

| 取值 | 生效时机 | 不是什么时候生效 | 常见误用 |
| --- | --- | --- | --- |
| `earliest` | **仅当**该分区没有已提交位点（新组）或位点越界（小于 `LogStartOffset`、大于 LEO） | 有有效位点时不生效 | 以为改成 `earliest` 就能「重跑三天」——只有换 `group.id`（或等位点过期）才真的从头开始 |
| `latest` | 同上，从末尾开始 | 同上 | 默认值，新组上线时**静默跳过存量积压**，被当成「消费不到历史数据」的 bug |
| `none` | 同上，但直接抛 `NoOffsetForPartitionException` | — | 想要「绝不静默跳过」的场景用这个，把错误暴露成启动失败 |

最贵的一种组合是：**位点过期（`offsets.retention.minutes` 到期）+ `auto.offset.reset=earliest` + 该 topic 保留了 7 天**。一个很久没跑的组重新启动，会从 7 天前开始全量重投——如果下游是发短信/发券，这就是一次事故，而不是一次「重跑」。

### 两个「少停一次 rebalance」的机制

- **静态成员（`group.instance.id`，KIP-345 机制）**：给每个实例一个稳定的 id（如 StatefulSet 的 pod 名）。重启后在 `session.timeout.ms` 内回来，coordinator 认它是老成员，**不触发 rebalance**，分区直接还给它。局限三条：① 只解决「同 id 重启」，扩容换了 id 照样 rebalance；② 窗口必须小于 `session.timeout.ms`（滚动发布要控制间隔）；③ 它挡不住 `max.poll.interval.ms` 超限导致的主动离开。
- **KIP-848 方向的新一代协议**：把分配计算从客户端 leader 挪到 **coordinator 服务端**，并把「心跳」与「poll 进度」解耦，目标是「增量、不停消费」。**本篇只提示方向：可用性、开关名与语义细节以目标版本 broker/客户端的文档为准**——线上真正按它设计之前，先确认你的客户端版本与 broker 是否都已支持并显式启用。

### 客户端骨架（Java：先处理后提交 + revoke 同步提交）

```java
// at-least-once：处理完再提交；revoke 时同步提交，否则新 owner 从更早位点重投
Properties p = new Properties();
p.put(ConsumerConfig.BOOTSTRAP_SERVERS_CONFIG, "b-1:9092");
p.put(ConsumerConfig.GROUP_ID_CONFIG, "order-fulfill");
p.put(ConsumerConfig.ENABLE_AUTO_COMMIT_CONFIG, "false");   // 语义的第一道门
p.put(ConsumerConfig.MAX_POLL_RECORDS_CONFIG, "200");       // 单批条数 = 重复量的上限
p.put(ConsumerConfig.MAX_POLL_INTERVAL_MS_CONFIG, "300000");// > 单批最坏处理时长 × 2
p.put(ConsumerConfig.SESSION_TIMEOUT_MS_CONFIG, "45000");
p.put(ConsumerConfig.HEARTBEAT_INTERVAL_MS_CONFIG, "3000");
p.put(ConsumerConfig.AUTO_OFFSET_RESET_CONFIG, "earliest");
p.put(ConsumerConfig.PARTITION_ASSIGNMENT_STRATEGY_CONFIG,
      "org.apache.kafka.clients.consumer.CooperativeStickyAssignor");

Map<TopicPartition, OffsetAndMetadata> done = new HashMap<>();
KafkaConsumer<String, String> c = new KafkaConsumer<>(p);
c.subscribe(List.of("order.fulfill.events"), new ConsumerRebalanceListener() {
    @Override public void onPartitionsRevoked(Collection<TopicPartition> ps) {
        c.commitSync(done);      // ★ 不留缝隙：重复量 = 未提交窗口
        done.clear();
    }
    @Override public void onPartitionsAssigned(Collection<TopicPartition> ps) {
        done.clear();            // 新 generation：本地缓存的位点一律作废
    }
});
while (running) {
    for (ConsumerRecord<String, String> r : c.poll(Duration.ofMillis(500))) {
        handle(r);               // 必须幂等（→ eng/24）
        done.put(new TopicPartition(r.topic(), r.partition()),
                 new OffsetAndMetadata(r.offset() + 1));   // ★ 「下一条」
    }
    if (!done.isEmpty()) c.commitSync(done);   // 批级同步提交
}
```

### 客户端骨架（Go：分区级手动存储 + 周期提交）

```go
// confluent-kafka-go：关掉自动存储，只提交「已处理」的位点
c, err := kafka.NewConsumer(&kafka.ConfigMap{
    "bootstrap.servers":             "b-1:9092",
    "group.id":                      "order-fulfill",
    "enable.auto.commit":            false,
    "enable.auto.offset.store":      false, // ★ 不关这一项，Commit() 提交的是「已收到」
    "auto.offset.reset":             "earliest",
    "session.timeout.ms":            45000,
    "max.poll.interval.ms":          300000,
    "partition.assignment.strategy": "cooperative-sticky",
})
if err != nil { log.Fatal(err) }

c.SubscribeTopics([]string{"order.fulfill.events"}, func(c *kafka.Consumer, ev kafka.Event) {
    switch e := ev.(type) {
    case kafka.RevokedPartitions:
        c.Commit()     // 先落地已处理位点，再放行撤销
        c.Unassign()
    case kafka.AssignedPartitions:
        c.Assign(e.Partitions)
    }
})

for {
    switch ev := c.Poll(500).(type) {
    case *kafka.Message:
        if err := handle(ev); err != nil { continue }  // 失败不存储位点 ⇒ 会重投
        c.StoreOffsets([]kafka.TopicPartition{{
            Topic:     ev.TopicPartition.Topic,
            Partition: ev.TopicPartition.Partition,
            Offset:    ev.TopicPartition.Offset + 1,   // ★ 「下一条」
        }})
    case kafka.Error:
        log.Printf("kafka error: %v", ev)              // 注意：ErrTimedOut 是常态，不是故障
    }
}
```

---

## 30.7 积压：lag 是并行度问题，不是容量问题

### lag 的三种口径（口径错了，后面全错）

```text
 分区 p 的位点轴：

 LogStartOffset ──── committed ────────────────── HW ══════════ LEO ──►
        │                │                        │              │
        │◄─ 已被删除 ───► │◄────── lag (本篇) ────►│◄ LEO−HW ────►│
                         │                        │   (复制，→ comp/29)
                    消费组位点            消费者能读到的最新位点

 48 分区、总 lag 1,900 万条时的真实形状：

   P7  ─██████████████████████████████████████████  3,800,000  ┐
   P19 ─██████████████████████████████████████████  3,800,000  ├ 3 个热分区 = 62% 入流
   P31 ─██████████████████████████████████████████  3,800,000  ┘
   P0  ─                                          0            ┐
   P1  ─                                          0            ├ 45 个冷分区已清空
   …                                                           ┘

   面板口径 A（总量 ÷ 总入流）：1,900 万 ÷ 12,000/s = 26.4 分钟 → 绿  ← 事故三
   真实 ETA（热分区）：3,800,000 ÷ (900 − 2,480) < 0 → 永远消化不完
```

| 口径 | 计算方式 | 回答的问题 | 什么时候会撒谎 |
| --- | --- | --- | --- |
| **总量 lag** | `Σ lag_p` | 「一共有多少条没消费」 | 冷分区被清空时总量会下降，而热分区在涨（事故三） |
| **平均 lag** | `总量 ÷ 分区数` | 「平均每个分区落后多少」 | 一条热分区 + 47 条空分区，平均永远是绿的 |
| **分区 max lag** | `max(lag_p)` | 「最坏的那个分区落后多少」 | 基本不撒谎；**这才是该进告警的口径**（`records-lag-max` 的 per-consumer 版本还要注意它只覆盖本消费者分到的分区，§30.12） |

### 并行度的三层封顶

| 层 | 公式 | 谁决定它 |
| --- | --- | --- |
| ① 结构性上限 | `并行度 = min(消费者数 C, 分区数 P)` | 分区数（多出来的消费者纯空转） |
| ② 客户端能力 | `单分区吞吐 = f(单条处理耗时, max.poll.records, fetch 往返)` | 业务处理时长、批大小、下游 P99 |
| ③ 入流约束 | `实际吞吐 = Σ_p min(单分区吞吐_p, 入流_p)` | 入流大于吞吐时，lag 只增不减 |

**第一层**：消费者数超过分区数时，多出的成员分不到分区，但它们**不会消失**：照样发心跳、占内存、出现在 `JoinGroup`/`SyncGroup` 里、`onPartitionsRevoked` 照样被调用。于是**每次 rebalance 的成本 ∝ 成员数，收益 = 0**（§30.8 表四）。

**第二层**：单消费者吞吐 ≈ `max.poll.records ÷ (单批处理时长 + 一次 fetch 往返 + 提交耗时)`。三个旋钮的代价：`max.poll.records` 调大 ⇒ 单批时长线性涨、逼近 `max.poll.interval.ms`（事故一），调小 ⇒ 往返与提交的固定开销占比上升；`fetch.min.bytes` 调大 ⇒ 攒批提吞吐，但低流量分区的延迟地板被 `fetch.max.wait.ms` 锁住；`max.partition.fetch.bytes` 是单次 fetch 单分区的字节上限，调得比大消息还小时不会卡死（客户端为保证不活锁仍会返回至少一条超限消息），但那个分区每轮只走一条，吞吐塌成个位数。

**第三层**：`入流_p > 单分区吞吐_p` 时 `lag_p` 的导数恒为正——**此时任何「消化时间」都是无穷**。必须先削峰、提单分区吞吐，或改 key 分布。

### 为什么「扩容」常常制造 rebalance 而不是提升吞吐

| 动作 | 你以为的效果 | 实际发生的 | 机制 |
| --- | --- | --- | --- |
| 消费者 24 → 36（分区 48） | 吞吐 × 1.5 | 一次 rebalance（停 3–300 s）+ 吞吐 × 1.5 − 停顿时长损失 | 新成员加入必然触发 rebalance |
| 消费者 48 → 96（分区 48） | 吞吐 × 2 | **零吞吐增益** + 每次 rebalance 成本翻倍 + 96 个心跳 | `min(C, P)` 的上限；空转成员照样参与协商 |
| 消费者 24 → 36（3 个热分区占 62%） | 吞吐 × 1.5 | **≈ 0 增益** | 瓶颈在分区内，不在消费者数（事故三） |
| 扩分区 48 → 96 | 并行度 × 2 | 吞吐可能 × 2，但 **key→分区映射改变** ⇒ 同一业务键的历史与新消息落在不同分区 ⇒ 破序 | 分区键是 `hash(key) % P`，`P` 一变映射全变（[eng/25](../eng/25-ordering-and-lag.md) §25.4） |

**这条纪律要背下来：分区数是「预留一次、用很久」的容量决策，不是「不够就加」的旋钮。** 加分区改的是数据分布契约；加消费者只改并行°——而并行度又被分区数封顶。

### 热分区的四种处置（按代价从低到高）

| 处置 | 做法 | 代价 | 适用 |
| --- | --- | --- | --- |
| ① 削峰 | 生产端限流 / 把大促流量分时段灌 | 业务侧限速 | 短期止血 |
| ② 提单分区吞吐 | 批量写、异步化下游、去掉逐条 RPC | 交付时间；伪异步会改语义（事故二） | 首选，因为不动数据契约 |
| ③ 给热 key 单开 topic | 大商户单独 topic，用更细的 key（如 `order_id`）打散 | 多一条链路要维护 | 能用、且已确认「该 key 不需全局有序」 |
| ④ 改 key / 加盐 | `merchant_id + suffix` 分片 | **破坏该 key 的分区内有序**，需要业务侧按 key 再串行（[eng/25](../eng/25-ordering-and-lag.md) 方案 C） | 只在顺序真的不重要时 |

---

## 30.8 判定表：四张值班时直接查的表

### 表一 · 该不该（能不能）rebalance

| 你看到的现象 | 先看这个 | 判定 | 处置 |
| --- | --- | --- | --- |
| 组状态在 `PreparingRebalance` 反复进出，lag 涨 | `--describe --state` + `rebalance-rate-per-hour` | **正在风暴里**：每一轮都有一批成员撑不到 SyncGroup | 找那个「拖后腿的成员」：单批耗时最长、或反复 `LeaveGroup` 的那个 pod；先把 `max.poll.records` 砍半 |
| 组 `Stable`、成员数固定，lag 单调涨 | per-partition lag 分布 | **不是 rebalance 问题**，是并行度（§30.7） | 查热分区、查单批耗时、查下游 |
| 组 `Empty` | 成员数 = 0 | 消费者根本没起来（部署/依赖/鉴权失败） | 看应用启动日志，不是 Kafka 问题 |
| `Dead` | 组被删或迁移中 | 位点历史已丢 | 换 `group.id` 重建，并评估 `auto.offset.reset` 的后果 |
| 成员数在 `C > P` 区间来回 | `C` 与分区数 | 有人在空转 | 把消费者数收敛到 ≤ 分区数 |

### 表二 · 位点提交时机怎么选

| 你的处理是 | 提交选择 | 语义 | 重复量上限 | 必须同时做 |
| --- | --- | --- | --- | --- |
| 同步、单批耗时可预测 | `commitSync` 每批一次 | at-least-once | `max.poll.records` | 幂等键（[eng/24](../eng/24-delivery-semantics.md)） |
| 同步、吞吐要求高 | 异步提交 + 每 N 批同步 + revoke 同步 | at-least-once（提交可能回退） | 提交窗口 | 幂等 + 监控 `commit-latency-max` |
| 异步线程池 | **先停下**：要么改同步，要么按分区手动跟踪「已完成的连续前缀」 | 现在多半是 at-most-once | 0 | 事故二的补丁：关掉自动提交 + 只提交连续完成的前缀 |
| 可丢（指标/采样） | 自动提交 | at-most-once | 0（但会丢） | 在文档里写清「可丢」 |
| Kafka→Kafka ETL | 事务 + `sendOffsetsToTransaction` | exactly-once（链内） | 0 | 事务超时、拓扑限制（[eng/11](../eng/11-exactly-once-truth.md)） |

### 表三 · `max.poll.interval.ms` 与处理时长的关系

| 单批最坏处理时长 `T` | 建议 `max.poll.interval.ms` | 违约后会发生什么 | 备注 |
| --- | --- | --- | --- |
| `T ≤ 10 s` | 默认量级（300 s）足够 | 极少发生 | 常规服务 |
| `T ≈ 120 s` | ≥ `2T` = 240 s，取 300 s | 一次尾部抖动就可能越过 | 必须同时配 `rebalance.timeout` 的等待上限 |
| `T` 抖动到 `400 s`（下游超时重试叠加） | 调 interval 只能「延后爆炸」 | 一旦越过：客户端**主动 `LeaveGroup`** → 全组 rebalance → 重投的批更慢 → 正反馈 | 正解是砍 `max.poll.records` 或把「慢调用」挪出 poll 线程 |
| `T > 单批 `× 3 | 拆批：把 `max.poll.records` 降到 `T` 回到 10 s 量级 | — | 单批条数 × 单条耗时 = 这一批的全部风险 |
| 无法预估 `T`（冷启动预热、批量补数） | 用「暂停分区」而不是加长 interval | — | `pause()` / `resume()` 可以在不提交位点的情况下空转心跳 |

### 表四 · 消费者数 > 分区数时谁在空转

| `C` vs `P` | 有分区的成员 | 空转成员 | 每次 rebalance 的额外成本 | 结论 |
| --- | --- | --- | --- | --- |
| `C < P` | 全部 | 0 | 正常 | 唯一「加机器有用」的区间 |
| `C = P` | 全部（各 1 个分区） | 0 | 正常 | 并行度上限；单分区慢就是单分区慢 |
| `C > P` | `P` 个 | `C − P` 个：心跳照发、内存照占、`onPartitionsRevoked` 照调 | 协商成本 ∝ `C`，收益 = 0 | **扩容不增效，只增风险** |
| `C ≫ P`（如 200 消费者 / 48 分区） | 48 个 | 152 个 | 每轮 JoinGroup 要收 200 份元数据；`rebalance-latency-max` 随 `C` 上升 | 典型「按 CPU 使用率自动扩缩容」造成的反模式：低 CPU → 扩 → 空转 → CPU 更低 → 继续扩 |

---

## 30.9 三本账：停顿时长、重复量、消化时间

```text
账一  rebalance 停消费总时长 = 轮数 × 单轮时长
      单轮时长 = 发现窗口(0~heartbeat.interval，或 0~max.poll.interval)
                 + revoke/commit + JoinGroup + SyncGroup + fetch 预热
      未消费量 = 停消费总时长 × 入流速率
      追赶时间 = 未消费量 ÷ (活跃消费者数 × 单消费者吞吐 − 入流)

账二  重复量 = 提交窗口 × 故障落在窗口内的期望比例 × 成员数 × 事件次数
      提交窗口 = 单消费者吞吐 × 提交间隔

账三  消化时间 = lag ÷ (min(C, P) × 单消费者吞吐 − 入流)        ← 无热分区
      消化时间 = max_p [ lag_p ÷ (吞吐_p − 入流_p) ]           ← 热分区修正，可能为 +∞
```

### 账一 · rebalance 停消费时长与它欠下的积压

**假设清单**：60 分区 / 20 个消费者（每 pod 一成员）；入流 15,000 msg/s（单条 500 B ⇒ 7.5 MB/s）；单消费者吞吐 900 msg/s（20 个合计 18,000/s，净消化余量 3,000/s）；同 AZ 心跳 RTT 0.5–2 ms；一轮正常 rebalance 的组成：发现（0~3 s，中位 1.5 s）+ revoke&commit 0.2 s + Join 0.02 s + Sync 0.02 s + fetch 预热 0.5 s（`fetch.min.bytes` 攒批）；一轮超时 rebalance 的发现窗口 = `max.poll.interval.ms` = 300 s。

| 场景 | 乘法链 | 停消费总时长 | 未消费量 | 追赶时间 |
| --- | --- | --- | --- | --- |
| **A · 健康滚动发布**（每 pod 间隔 60 s > 心跳周期，逐台触发 20 轮） | 20 轮 × 2.24 s | **≈ 45 s** | 45 × 15,000 = **67.5 万条** | 67.5万 ÷ (18,000−15,000) = **225 s** |
| **B · 事故一形态**（4 轮被 `max.poll` 拉长 + 16 轮正常） | 4 × 302 s + 16 × 2.24 s | **≈ 20.8 分钟（1,244 s）** | 1,244 × 15,000 = **1,866 万条 ≈ 9.3 GB** | 1,866万 ÷ 3,000 = **6,220 s ≈ 104 分钟** |
| **C · 全部成员同时重启**（例如一次 K8s 节点池轮换） | 1 轮，但发现窗口要等最慢者（`rebalance.timeout` 上限） | 3 s ~ 300 s（区间） | 4.5 万 ~ 450 万条 | 15 s ~ 25 分钟 |
| **D · 换成 cooperative + 静态成员** | 20 轮 × 「只迁该迁的分区」≈ 0.3 s | **≈ 6 s** | 9 万条 | 30 s |

| 量 | 推导 | 结果 |
| --- | --- | --- |
| 每轮 coordinator 要处理的请求量 | 20 成员 × (1 JoinGroup + 1 SyncGroup) + 每次 revoke 的一次 OffsetCommit | ≈ 60 次请求/轮 |
| 一轮里最慢成员的主导项 | `rebalance.timeout` 取成员的 `max.poll.interval.ms` | 5 分钟量级：**一个慢成员可以把全组按在这里 5 分钟** |
| 场景 B 的账，折成钱 | 15,000 msg/s ÷ 8 条/单 = 1,875 单/s；1,244 s × 1,875 单/s × 0.2 元/单 | ≈ 46.6 万元（与事故一一致） |

> **量级结论**（不是承诺值）。假设清单见上；**「单轮时长」跨越两个数量级**（2 s 级 ↔ 300 s 级）是本账唯一重要的结论——所以「rebalance 要多久」这个问题只有一个正确答法：**先问有没有人越过 `max.poll.interval.ms`**。换 SSD、换网络、换 broker 版本都不会移动这个数量级；把它们调对才会。

### 账二 · 提交频率与重复量

**假设清单**：单消费者 900 msg/s；20 个消费者；一天 **30 次**「成员离开/崩溃/重启」类事件（一次发布 20 个 pod 就是 20 次起）；故障时刻在提交窗口内均匀分布（期望落在窗口中间 ⇒ 重复量为窗口的一半）；下游幂等键可用。

| 提交策略 | 提交窗口（条） | 单次事件的重复量（全组） | 一天重复量 | `__consumer_offsets` 写入量/天 |
| --- | --- | --- | --- | --- |
| 自动提交，间隔 5 s | 900 × 5 = **4,500** | 4,500 ÷ 2 × 20 = **45,000** | 45,000 × 30 = **135 万条** | 20 成员 × 1.8 次/s × 86,400 ≈ **311 万次 append** |
| 每批 500 条同步提交 | **500** | 250 × 20 = **5,000** | **15 万条** | 20 × 1.8 次/s × 86,400 ≈ **311 万次**（同量级：批级提交在 900 msg/s 下就是 ≈1.8 次/s） |
| 每 100 条提交 | **100** | 50 × 20 = **1,000** | **3 万条** | ≈ **1,555 万次 append**（×5） |
| 事务（每批） | 0（回滚重投 = 1 批） | ≤ 500（重投） | 数量级同「每批提交」 | 另加事务协调器写入 |

| 量 | 推导 | 结果 |
| --- | --- | --- |
| 「提交更勤」的收益递减点 | 重复量 ∝ 窗口，而写入量 ∝ 提交次数 = 吞吐 ÷ 窗口 | 窗口从 4,500 降到 500：重复量 ÷9，写入量不变；再降到 100：重复量 ÷5，写入量 ×5 |
| 单个 coordinator 分区的承载 | 每次 append + 一次 group cache 更新，量级 0.1–1 ms CPU | 3,000 次/s 就把一个 coordinator 分区打到 30%–100% |
| 100 个组的规模 | 3,000 次/s × 100 组 | **30 万次/s** ⇒ 必然把 `__consumer_offsets` 打爆，需要更粗的提交粒度 |

> **量级结论**（不是承诺值）。假设清单见上。真正的结论是一条边际分析：**提交频率只在「窗口从 5 s 降到 1 批」这一段划算**（重复量降一个数量级，写入量几乎不变）；再往下走就是拿 coordinator 的写入量换重复量，而 `__consumer_offsets` 是**全集群所有组共用的一条 compact 日志**（§30.3），它的容量不属于你的那个组。

### 账三 · 积压消化时间与热分区修正

**假设清单**：lag 1,900 万条（单条 500 B ⇒ 9.5 GB）；48 分区 / 24 消费者（`C < P`，各 2 分区）；单分区吞吐 900 msg/s（单线程 + 同步写 DB）；继续入流 12,000 msg/s，其中 3 个热分区占 62%；冷分区已清空。

| 情形 | 乘法链 | 结果 |
| --- | --- | --- |
| ① 理论（入流视为 0） | 1,900万 ÷ (24 × 900 = 21,600) | **880 s ≈ 15 分钟** |
| ② 理论（入流继续） | 1,900万 ÷ (21,600 − 12,000 = 9,600) | **1,979 s ≈ 33 分钟** |
| ③ 热分区修正（事故三实况） | 每热分区入流 = 12,000 × 62% ÷ 3 = **2,480/s > 900/s** | **无解（+∞）**：lag 每秒净增 (2,480−900)×3 = **4,740 条** |
| ④ 削峰后（热分区入流降到 850/s） | 1,140万 ÷ (3 × (900 − 850)) = 1,140万 ÷ 150 | **76,000 s ≈ 21 小时**（仍不可接受 ⇒ 必须同时提吞吐） |
| ⑤ 提吞吐（批量写，单分区 2,500/s） | 1,140万 ÷ 3 × (2,500 − 2,480)… ⇒ 仍紧 | 必须先削峰，再提吞吐，两手一起 |
| ⑥ 拆 key（大商户 topic 24 分区，热 key 打散） | 热流量 7,440/s ÷ 24 分区 = 310/s/分区 < 900 | lag 按 ② 的形态收敛，**≈ 20 分钟** |

| 量 | 推导 | 结果 |
| --- | --- | --- |
| 「加消费者」的天花板 | `min(C, P) × 900`，`P=48` ⇒ 43,200/s | 只有入流 < 43,200/s 且**无热分区**时才追得上 |
| 「加消费者」在事故三的实际作用 | 24 → 36 个消费者，热分区仍只有 3 个、每个 900/s | 增益 = **0**（观察到 6% 的降幅来自冷分区波动） |
| 若把分区从 48 扩到 96 | 需重算 key 映射：`hash(key) % 96 ≠ hash(key) % 48` | 大商户仍可能落在一个分区（除非换 key）⇒ 扩分区不解决热 key |
| 数据量换算 | 1,900 万 × 500 B | 9.5 GB（与磁盘无关；`retention` 才是它的时限，见 [comp/28](28-kafka-log-segments.md)） |

> **量级结论**（不是承诺值）。假设清单见上。三件事值得单独记住：① `lag ÷ (C × 吞吐)` 这个最常见的式子**默认了「无热分区」**，它在事故三的形态下返回 +∞ 而不是一个大数；② 削峰与提吞吐必须同时做（情形 ④ 说明只削峰要 21 小时）；③ 扩分区不解决热 key，因为热 key 的分布由 `hash(key) % P` 决定，而不是由 `P` 的大小决定。

---

## 30.10 架构边界：消费组能保证什么、不能保证什么

| 能保证（在条件成立时） | 不能保证 | 靠什么绕过 |
| --- | --- | --- |
| 同一时刻一个分区只被同组的一个成员消费（generation 挡老成员） | 不保证**跨组**互斥：两个 `group.id` 会各读一份全量 | 想「广播」就故意用不同 group；想「独占」就别复制消费逻辑（[architect/15](../architect/15-message-store.md)） |
| 一个分区的消息**按 offset 顺序**交给消费者 | **不保证跨分区有序**，也不保证 `key` 相同就一定同分区（扩分区会改映射） | key 选聚合根唯一标识；保序方案三选一见 [eng/25](../eng/25-ordering-and-lag.md) §25.2 |
| 成员对分配表意见一致（`Stable`） | 不保证 rebalance **期间**不停消费：eager 全停；cooperative 停被迁的分区 | cooperative + 静态成员 + 分批发布 + 缩短单批耗时（§30.4、§30.5） |
| 位点提交成功后，重启从该位点继续 | 不保证「提交了位点 = 处理完了」；也不保证位点**回退**不会发生（`commitAsync` 乱序） | 先处理后提交 + revoke 同步提交 + 幂等（§30.6） |
| 至少一次投递（先处理后提交） | **不保证只处理一次**：重投、rebalance、生产重试都会重复 | 幂等键/去重表/条件更新（[eng/24](../eng/24-delivery-semantics.md)、[eng/26](../eng/26-message-reliability.md)） |
| —— | **不提供 exactly-once**：那要事务/幂等 Sink，属另一层（[eng/11](../eng/11-exactly-once-truth.md)） | 事务写 + `sendOffsetsToTransaction`，且只覆盖「读 Kafka→写 Kafka/幂等端」 |
| —— | 不保证「加消费者提吞吐」：上限 `min(C, P)`，且被热分区封顶 | 削峰 / 提单分区吞吐 / 拆 key / 单开 topic（§30.7） |
| —— | 不保证位点永久存在（`offsets.retention.minutes` 过期） | 定期巡检 `Empty` 组；重要组不要长期停产 |
| —— | 不保证分配均衡（`range` 的倾斜可达 2 倍） | 选 cooperative-sticky；分区数取成员数的整数倍 |

**一句话边界**：消费组买到的是「**在同一个 group 内、按分区、至少一次、且成员会短暂全停**」的协作消费；它买不到「有序（跨分区）」「只一次」「不停顿」「可水平无限扩」。四件事各有各的替代方案，而且**每一样都不在消费组这一层**。

---

## 30.11 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到物理形态/语义） | 定位 | 修复 | 防复发 |
| --- | --- | --- | --- | --- |
| 发布/扩容后 lag 立刻暴涨，消费停了 30 s ~ 几分钟 | eager rebalance：每加/减一个成员都要全组撤销重分；单轮时长 = revoke + Join + Sync + 预热 | `kafka-consumer-groups.sh --describe --state` 看状态；客户端 `rebalance-rate-per-hour` / `rebalance-latency-max` | 换 `CooperativeStickyAssignor`；分批发布；`group.instance.id` 静态成员 | 把「发布窗口内 lag 允许上升多少」写进发布 checklist；`rebalance-rate-per-hour` 进看板 |
| 发布后消费停了 5 分钟量级，日志里是 **`LeaveGroup` / poll 间隔超限** | `max.poll.interval.ms`（客户端侧）被越过 ⇒ **客户端自己走人**，coordinator 没踢它 | 客户端日志的 `LeaveGroup` reason；单批耗时打点（`poll-idle-ratio-avg` 同步上升） | 砍 `max.poll.records`；把慢调用挪出 poll 线程；`max.poll.interval.ms` 提到「最坏 ×2」 | 单批耗时必须在监控里（p99 单批 = 每条耗时 × `max.poll.records`） |
| 「我把 `session.timeout.ms` 调大避免 rebalance」，没用 | 两个超时管两条命：`session.timeout` 管心跳，`max.poll.interval` 管处理进度 | 对比两个参数与两个现象（心跳停 vs poll 停） | 按 §30.4 的分工各调各的 | 参数上线前填一张「这个参数改哪条路径」的表 |
| 重启后重复消费几千条 | `onPartitionsRevoked` 里没同步提交；重复量 = 未提交窗口 | 对比 revoke 时刻的已处理位点与 `__consumer_offsets` 里的位点 | revoke 里 `commitSync(done)`；`commitAsync` 加周期同步兜底 | 代码模板固化；单测里模拟一次 revoke |
| 位点提交正常但数据少了（无任何报错） | 自动提交 + 异步线程池：提交的是「已拉取」而非「已处理」 | 比对「已提交位点」与「已处理位点」（应用侧必须有后一个数） | 关自动提交 + 只提交「连续完成的前缀」 | 该组 `records-lag-max` 长期 0 而业务量不为 0 ⇒ 红线告警 |
| lag 面板绿，业务说延迟几小时 | 口径是「总量 ÷ 总入流」或「平均 lag」，被冷分区稀释（事故三） | 拉 per-partition lag 排序（§30.12 的 Python 片段） | 告警改成**分区 max lag** + 热分区 ETA | 面板固定展示「top3 分区 lag 占比」 |
| 扩消费者，lag 只降一点点 | `C > P` 空转；或热分区封顶 | 成员数 vs 分区数，per-partition lag 分布 | 收敛消费者数；处置热分区 | 自动扩缩容的上下限必须写成「分区数」而不是「CPU%」 |
| 扩分区后下游出现「同一订单状态倒退」 | 扩分区改了 `hash(key) % P` 映射 ⇒ 同一 key 的历史在旧分区、新消息在新分区 ⇒ 并发处理 | 查该 topic 的扩分区时间点与破序订单的时间点 | 业务侧按 key 串行（[eng/25](../eng/25-ordering-and-lag.md) 方案 C）；或新开 topic 迁移 | 「分区数」进变更评审，与「改 key」同等重量 |
| 一个很久没跑的组重启后，从 7 天前全量重投 | 位点过期（`offsets.retention.minutes`）+ `auto.offset.reset=earliest` + retention 还留着那段数据 | `--describe --state` 为 `Empty` 的时长；`__consumer_offsets` 里是否还有该组记录 | 重启前先确认位点是否存在（或显式 `seek`） | 重要组加「长期空转巡检」；`auto.offset.reset` 在模板里固定为 `none` |
| 同一个 `group.instance.id` 出现在两个 pod 上 | 静态成员 id 冲突（复制粘贴的 StatefulSet 名/手工同名） | 客户端日志 `FENCED_INSTANCE_ID` | 用真正的唯一 id（pod 名 / hostname） | 部署清单里 `group.instance.id` 必须由编排系统注入，禁止硬编码 |
| `__consumer_offsets` 只有 1 副本，换一台机器后所有组位点丢失 | 内部 topic 在集群还很小时被自动创建，用了当时的 `offsets.topic.replication.factor` 默认值 | `kafka-topics.sh --describe --topic __consumer_offsets` 看 `ReplicationFactor` | 在线重分配到 RF=3（限速） | 新集群上线第一件事就是检查这个 topic 的 RF 与分区数 |

---

## 30.12 观测：五个必须有的数字，以及它们各自的版本/开关前提

**前置开关**：broker 侧 JMX 需启动参数带 `-Dcom.sun.management.jmxremote*`（或用 exporter 以 JMX 代理抓取）；客户端侧消费指标要**每个实例有唯一 `client.id`**，否则多实例的同名指标会互相覆盖；`records-lag-max` 只在**已经分配且已发起 fetch** 的分区上有值（未分配 = 没有这个数，不是 0）。以下 metric 名以**目标版本 Kafka 的 JMX 输出为准**（3.x/KRaft 与 2.x/ZK 模式的 metric 集合有差异；KIP-848 新协议启用后，rebalance 相关指标的语义与名称会变，**必须以目标版本为准**）。

| 看什么 | 指标 / 命令 | 拐点 | 版本与开关前提 |
| --- | --- | --- | --- |
| **① 分区级 lag 的 max 与分布（第一指标）** | `--describe` 的 `LAG` 列（口径 = `LEO − CURRENT-OFFSET`）/ 自己用 AdminClient 采 `HW − committed` | `max(lag_p)` 持续 5 分钟上升即 P2；**top3 分区占总 lag > 50% ⇒ 并行度被热分区钉住，扩容无用** | 全版本；工具默认口径是 LEO，与「可消费」差一个 `LEO−HW`（[comp/29](29-kafka-replication-isr.md) §29.11 第 ⑩ 行） |
| **② rebalance 频率与时长** | `kafka.consumer:type=consumer-coordinator-metrics,name=rebalance-rate-per-hour`、`rebalance-latency-avg/max`、`rebalance-total`、`last-rebalance-seconds-ago` | `rebalance-rate-per-hour > 2` 持续 = 风暴；`rebalance-latency-max > 60000 ms` = 有人在吃 300 s 级的等待 | 客户端 JMX；cooperative 策略下「次数」会天然变大（多轮收敛），要看**总停消费时长**而不是次数 |
| **③ 处理进度（poll 是否跟得上）** | `kafka.consumer:type=consumer-metrics,name=poll-idle-ratio-avg`、`poll-total` | `poll-idle-ratio-avg > 0.5` = 线程大半时间在处理，离 `max.poll.interval` 违约不远 | 客户端 JMX；**这是事故一唯一能提前 10 分钟报警的数字** |
| **④ 提交健康度** | `commit-latency-avg/max`、`commit-rate`（consumer 侧） | `commit-latency-max` 抬到 `session.timeout.ms` 量级 ⇒ coordinator 在抖或正在 rebalance | metric 组与名称以目标版本 JMX 输出为准；KIP-848 协议下提交链路的指标集不同 |
| **⑤ 组状态与成员数** | `--describe --state`、`--describe --members --verbose`；broker 侧 `kafka.coordinator.group:type=GroupMetadataManager,name=NumGroupsPreparingRebalance`、`NumGroupsCompletingRebalance`、`NumGroupsStable`、`NumGroupsEmpty` | `NumGroupsPreparingRebalance > 0` 持续 = 有一批组一直没完成协商；`NumGroupsEmpty` 突然涨 = 消费者大面积没起来 | 状态级指标的**存在性与名称以目标版本 JMX 输出为准**（早期版本没有按状态分组的计数） |
| ⑥ fetch 效率（区分「拉不到」与「处理不完」） | `fetch-rate`、`bytes-consumed-rate`、`fetch-total`、`assigned-partitions` | `assigned-partitions` 为 0 而进程活着 = 空转成员；`bytes-consumed-rate` 正常但 lag 涨 = 瓶颈在业务处理 | 客户端 JMX；`fetch-rate` 受 `fetch.min.bytes` / `fetch.max.wait.ms` 直接影响（§30.7） |
| ⑦ 组位点是否还活着 | `kafka-topics.sh --describe --topic __consumer_offsets`（分区数 / RF）、`kafka-configs.sh --describe --entity-type topics` | RF < 3 或分区数被改过 = 隐患 | 内部 topic 的 RF 由 `offsets.topic.replication.factor` 在建 cluster 时的取值决定，**之后改配置不会自动改已有 topic** |

```bash
# 值班三板斧：组在什么状态 → 谁分到了什么 → 每分区落后多少
kafka-consumer-groups.sh --bootstrap-server b-1:9092 \
  --describe --group order-fulfill --state
# 期望 STABLE；若是 PREPARING_REBALANCE 反复出现，去查 max.poll.interval 与单批耗时

kafka-consumer-groups.sh --bootstrap-server b-1:9092 \
  --describe --group order-fulfill --members --verbose
# 看 CONSUMER-ID / HOST / #PARTITIONS：出现 #PARTITIONS=0 的成员 = 空转（C > P）

kafka-consumer-groups.sh --bootstrap-server b-1:9092 \
  --describe --group order-fulfill \
  | awk 'NR>1 && $6+0>0 {printf "%-32s P%-4s lag=%s\n", $2, $3, $6}' | sort -t= -k2 -rn | head -5
# 口径是 LEO − CURRENT-OFFSET；要看「真正可消费的积压」得自己减 LEO−HW（→ comp/29）

# 内部 topic 体检：分区数（默认 50）与副本因子必须在这一屏里看一眼
kafka-topics.sh --bootstrap-server b-1:9092 --describe --topic __consumer_offsets | head -3
# ReplicationFactor 为 1 ⇒ 换一台 broker 就可能丢全组位点（§30.11 最后一行）
```

```python
# 每分区 lag + 热分区占比：不要用「总 lag ÷ 总入流」当 ETA（事故三的假绿）
from confluent_kafka import Consumer, TopicPartition

c = Consumer({"bootstrap.servers": "b-1:9092",
              "group.id": "marketing-campaign",
              "enable.auto.commit": False})
topic, n = "marketing.campaign.events", 48

lag = {}
for p in range(n):
    tp = TopicPartition(topic, p)
    lo, hi = c.get_watermark_offsets(tp, timeout=5.0, cached=False)   # hi 即 HW
    committed = c.committed([tp], timeout=5.0)[0].offset
    if committed < 0:                      # 无位点：按 auto.offset.reset 生效
        committed = lo
    lag[p] = hi - committed                # LEO−HW 属复制层（→ comp/29）

total = sum(lag.values())
print("total=%d avg=%d" % (total, total // n))
for p, v in sorted(lag.items(), key=lambda kv: -kv[1])[:3]:
    print("P%-3d lag=%9d share=%.1f%%" % (p, v, 100.0 * v / total))

# 判据：top3 占比 > 50% ⇒ 并行度已被热分区钉住，加消费者无效（§30.7）
```

**告警表达式骨架**（指标名按你的 exporter 前缀改写；口径说明见上表）：

| 告警 | 表达式骨架 | 意图 |
| --- | --- | --- |
| ① 分区 max lag 在涨 | `max by (group, topic) (kafka_consumergroup_lag) > <1 分钟入流量 × 10>` | 不要用平均 lag 报警 |
| ② rebalance 在风暴里 | `rate(kafka_consumer_coordinator_rebalance_total[15m]) * 720 > 2` | 速率，不是瞬时值 |
| ③ 有人逼近 `max.poll.interval` | `avg by (client_id) (kafka_consumer_poll_idle_ratio_avg) > 0.5` | 唯一能提前十分钟报警的数字 |
| ④ 提交在抖 | `histogram_quantile(0.99, rate(kafka_consumer_commit_latency_ms_bucket[5m])) > 1000` | coordinator 抖动或正在 rebalance |
| ⑤ 有成员分不到分区 | `min by (group) (kafka_consumer_assigned_partitions) == 0` | `C > P` 的空转成员 |

---

## 30.13 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 三张表 | 成员表 + 位点表落盘（`__consumer_offsets`，compact）；**分配表不落盘**（由 leader 算完经 SyncGroup 转发） |
| coordinator | `hash(group.id) % 50` 的那个分区 leader；一个组的全流量打在**一台** broker 上 |
| 五个状态 | `Empty / PreparingRebalance / CompletingRebalance / Stable / Dead`；只有 `Stable` 在干活 |
| 三阶段 | `JoinGroup`（选策略 + 选 leader + 收订阅）→ `SyncGroup`（下发分配，generation+1）→ `Heartbeat`（续命 + 校验 generation） |
| 三个超时 | `session.timeout`（coordinator 看心跳）、`heartbeat.interval`（≤ session/3）、`max.poll.interval`（**客户端自己看 poll 进度**） |
| 事故一的答案 | 越过 `max.poll.interval` 是**客户端主动 `LeaveGroup`**，不是被踢；调 `session.timeout` 治不了 |
| 单轮时长 | 正常 2–3 s 级；有人卡住则 = `max.poll.interval` = **300 s 级**。差两个数量级 |
| eager vs cooperative | eager 停全组；cooperative 只停被迁的分区，但**可能要两轮**才均衡，协议不能混用 |
| 四种策略 | `range` 的毛病是「倾斜」，`sticky` 是「还是得停」，`cooperative-sticky` 是「要多转两圈」 |
| 位点语义 | 提交的是「**下一条**」；交在处理前 = at-most-once，交在处理后 = at-least-once |
| 自动提交 | 提交「上次 poll 返回的最后一条 +1」；配异步线程池时**语义被静默改成 at-most-once**（事故二） |
| revoke 必做 | `onPartitionsRevoked` 里同步提交，否则重复量 = 未提交窗口 |
| `auto.offset.reset` | 只在「无位点 / 位点越界」时生效；不是重跑开关。`none` 才是「宁可起不来」 |
| lag 口径 | 告警用**分区 max lag**；「总量 ÷ 总入流」会被冷分区稀释（事故三报 26 分钟，真值是 +∞） |
| 并行度 | `min(C, P)`；`C > P` 时多出来的人照样心跳、照样参与 rebalance、收益 0 |
| 扩容的真相 | 加消费者 = 一次 rebalance + `min(C,P)` 内的增益；加分区 = 改 key 映射 = 破序 |
| 热分区 | 入流 > 单分区吞吐 ⇒ 消化时间 +∞；处置顺序：削峰 → 提吞吐 → 拆 key → 单开 topic |
| exactly-once | 不在消费组里；要事务 + `sendOffsetsToTransaction`（[eng/11](../eng/11-exactly-once-truth.md)） |
| 一句话 | **rebalance 不是平滑迁移，位点提交时机不是风格偏好，积压不是磁盘不够——三件事分别是「协议成本」「语义选择」「并行度算术」。** |

---

## 30.14 自检题

**1. 一次滚动发布，20 个 pod 逐台重启，lag 在发布期间涨了 3,700 万条，发布窗口 21 分钟。请把这段时间拆成可算的三段，并给出三个能把停消费压到 10 秒以内的改动。**

<details><summary>答案</summary>

三段：① **轮数 × 单轮协议时长**（20 轮 × 2.2 s ≈ 45 s，这部分是「正常成本」）；② **超时轮次**（每一轮里有成员越过 `max.poll.interval.ms`，把单轮从 2 s 拉到 300 s；事故一里 4 轮 ≈ 20 分钟）；③ **欠下的追赶时间**（1,244 s × 15,000/s = 1,866 万条 ÷ (18,000−15,000)/s ≈ 104 分钟）。所以「发布 21 分钟」里，真正的协议协商只占不到 1 分钟，**其余几乎全是单批处理时长越界造成的**。

三个改动：① `max.poll.records` 砍到「单批最坏耗时 ×2 < `max.poll.interval.ms`」的水平（本场景从 500 → 100），这是唯一能把 300 s 级轮次消掉的改动；② 换 `CooperativeStickyAssignor`，把「全组停」变成「只迁走的分区」，正常轮次从 2.2 s → 0.3 s 量级；③ `group.instance.id` 静态成员 + 分批发布间隔 < `session.timeout.ms`，让重启的成员在窗口内拿回原分区、根本不触发 rebalance。三项叠加后停消费从 1,244 s 降到个位数秒。**注意第三项只在「同 id 重启」时有效，扩容无效。**
</details>

**2. 「我们关了自动提交，改成每批 `commitSync`，为什么重启后还是重复了 4,000 多条？」请给出至少三个不涉及代码写错的物理原因。**

<details><summary>答案</summary>

① **revoke 时没提交**：`onPartitionsRevoked` 里没 `commitSync`，最后一次提交之后处理的那一段（4,500 条窗口的一半 ≈ 2,250 条 × 成员数）会在新 owner 上重投。② **提交成功了但消费者还没「生效」**：位点提交到 coordinator 后要写进 `__consumer_offsets` 与缓存，紧接着进程被 kill，新成员读到的位点就是提交成功的那个——**这本身不产生重复**；产生重复的是「处理完 → 提交」之间的崩溃窗口，其大小上限 = 一批（`max.poll.records`）。4,000 条 ≈ 一批的 8 倍，说明不是这个原因，要往 ①③ 查。③ **同一批里部分失败后整批重试**：`ack-mode: batch` 或「整批失败就整批重来」的写法，会让批内已成功的记录被再处理一次。④ **事务/异步提交乱序**：`commitAsync` 的回调乱序可能让较小位点覆盖较大位点（位点回退），重启后从更早位置开始。⑤ **组重建走了 `auto.offset.reset`**：位点过期（`offsets.retention.minutes`）或 `group.id` 被换掉，新组从 `earliest` 开始——但这种情况下的重复量会是「三天」而不是「4,000 条」，可以用量级区分。
</details>

**3. 同一个组，消费者从 24 个扩到 36 个（分区 48），lag 几乎没降。请给出四种互不相同的解释，并各给一个能在 5 分钟内验证的方法。**

<details><summary>答案</summary>

① **热分区封顶**：少数分区占绝大部分入流，其入流 > 单分区吞吐 ⇒ 加人无效。验证：拉 per-partition lag 排序，看 top3 占比是否 > 50%（§30.12 的 Python 片段）。② **瓶颈在下游**：单条处理耗时由下游 DB/RPC 决定，消费者只是在等；加人只是增加并发请求，把下游打得更慢。验证：`poll-idle-ratio-avg` > 0.5 且下游 P99 同步恶化。③ **新成员根本没分到分区**：`C > P` 或分配策略倾斜（`range` 在 P 不能被 C 整除时倾斜）。验证：`--describe --members --verbose` 看 `#PARTITIONS=0` 的成员与各成员分区数。④ **扩容本身制造了 rebalance**：新成员加入触发全组重分，停消费 + 重投把收益吃掉；24→36 若触发多轮，可能净收益 ≈ 0。验证：`rebalance-rate-per-hour` 与 `rebalance-latency-max` 的突增时间点是否与扩容时间一致。⑤（第五个）**lag 口径把 LEO−HW 算进来了**：看起来的「积压」其实是复制延迟。验证：同时取 `HW − CURRENT-OFFSET` 与 `LEO − CURRENT-OFFSET` 比较。
</details>

**4. 面板显示总 lag 1,900 万条、ETA 26 分钟、颜色绿；但业务说延迟 6 小时。请写出面板的公式、真值公式，以及你会在面板上加的两个数字。**

<details><summary>答案</summary>

面板（错）：`ETA = Σ lag_p ÷ 总入流 = 1,900 万 ÷ 12,000/s = 26.4 分钟`。它隐含假设「所有分区的入流都可以被平均分摊，且消化能力与入流成比例」——这两个假设在倾斜分布下都不成立。

真值：`ETA = max_p [ lag_p ÷ (吞吐_p − 入流_p) ]`。本例中 3 个热分区各 `lag=380 万`、`入流=2,480/s`、`吞吐=900/s` ⇒ 分母为负 ⇒ **ETA = +∞**（lag 每秒净增 4,740 条）。总 lag 之所以在跌，是 45 个冷分区被清空造成的假象。

面板上加的两个数字：① **top3 分区 lag 占比**（> 50% 就说明并行度被钉住，ETA 一律按 +∞ 处理）；② **每分区 ETA 的 max**（用 `max_p lag_p ÷ (吞吐_p − 入流_p)`，分母 ≤ 0 时直接显示「不收敛」而不是显示一个大数）。
</details>

**5. 「rebalance 期间会丢消息吗？」请给一个精确回答，并说明重复与丢失分别由哪个窗口决定、各自的量级如何算。**

<details><summary>答案</summary>

精确回答：**rebalance 本身既不丢也不重复**——它只是「撤销分区、重算分配、再发下去」。数据出问题发生在**位点与处理的相对位置**上，跟 rebalance 只是碰巧同时发生。

- **重复**：窗口 = 「已处理但未提交」的那一段。量级 = `单消费者吞吐 × 提交间隔 ÷ 2 × 成员数`（§30.9 账二：900×5÷2×20 = 45,000 条/次）。rebalance 是把这个窗口「暴露」出来的时机，因为 revoke 强制换了 owner；只要 `onPartitionsRevoked` 同步提交，窗口压到 ≤ 1 批。
- **丢失**：窗口 = 「已提交但未处理」的那一段。只有当提交点落在处理之前（自动提交 + 异步线程池、或显式先提交）时才非零；量级 = 队列里在途的批（事故二：24 个任务 × 500 条 = 12,000 条）。
- 唯一「rebalance 直接导致丢数据」的路径不是丢消息，而是**语义漂移**：用异步线程池 + 自动提交，代码看起来是 at-least-once，实际是 at-most-once。这就是为什么本篇要把「提交时机 = 语义」写成唯一真问题的第二半。
</details>

---

## 30.15 延伸阅读（锚点式）

- 日志的物理形态（segment、稀疏索引、page cache、零拷贝）与「为什么它不能删不能改」：[comp/28 Kafka 的日志](28-kafka-log-segments.md)
- 复制与一致性：`HW = min(ISR 的 LEO)`、`LEO − HW` 与 `acks` 的联合体、unclean 选举后位点被复用：[comp/29 复制与一致性](29-kafka-replication-isr.md)
- 投递语义（至少一次 / 至多一次 / 恰好一次的真实边界）与三条幂等落点：[eng/24 投递语义与重复消费](../eng/24-delivery-semantics.md)
- 顺序、堆积与 rebalance 的处置手册（三种保序方案、四个破序点、堆积四类根因）：[eng/25 顺序、堆积与重平衡](../eng/25-ordering-and-lag.md)
- 消息可靠性八环节清单（生产端/消费端逐环打补丁、DLQ 与重试队列）：[eng/26 消息可靠性](../eng/26-message-reliability.md)
- 恰好一次的真实边界与事务落点（本篇 §30.6 那张语义表的完整版）：[eng/11 恰好一次的真相](../eng/11-exactly-once-truth.md)
- 异步解耦的判据与 MQ 选型打分表（「先试并行，再试 MQ」）：[eng/23 异步消息基础](../eng/23-async-messaging-basics.md)
- 消息存储的整体形态决策（什么时候该换成队列型或存算分离型）：[architect/15 消息存储](../architect/15-message-store.md)
- 消息族谱：RocketMQ / Pulsar / RabbitMQ / NATS 的消费模型差异（推 vs 拉、ack 粒度、存算分离）：`→ comp/31`
- 任务队列语义：可见性超时、死信与「被取走但没做完」这个状态：`→ comp/32`
- CDC 的位点衔接（快照与增量的交界，另一种「位点」问题）：`→ comp/33`
- Flink 的 checkpoint 与消费位点（消费组位点在流式引擎里的替身）：`→ comp/34`
- 组件故障谱：本篇三起事故在「慢 / 满 / 脑裂 / 假活 / 语义漂移」里的归类与兜底招式：`→ comp/52`
- 本套总纲：解剖卡八问、三条纪律（版本常量 / 指标前提 / 量级结论 vs 承诺值）与反冗余公约：[0-summary.md](0-summary.md)
