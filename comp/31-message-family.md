# 31. 消息族谱：RocketMQ / Pulsar / RabbitMQ / NATS

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 消息、流与定时 ｜ **编号**：31 / 53
> **本篇定位**：全仓唯一一处把四种非 Kafka 消息中间件的**数据物理形态**摆到同一张桌子上对照的篇目。Kafka 的日志与复制机制归 `→ comp/28`、`→ comp/29`，消费组协议归 `→ comp/30`，本篇一个字不重写，只给「Kafka 缺哪一块」的一句结论 + 锚点；投递语义与幂等落点归 [eng/24 投递语义](../eng/24-delivery-semantics.md)，可靠性八环节归 [eng/26 消息可靠性](../eng/26-message-reliability.md)，延迟与定时机制归 `→ comp/36`，「选哪个消息系统」的量级估算归 [architect/15 消息存储](../architect/15-message-store.md)。本篇只回答一个问题：**RocketMQ 的 CommitLog、Pulsar 的 ledger、RabbitMQ 的 queue、NATS 的 subject 这四种形状，各自把哪几条语义做进了内核、哪几条留给你在业务里补一层，而那一层要付多少钱。**
> **读法**：救火读 §31.11 的坑清单与 §31.12 的指标拐点；做选型先读 §31.2 的五维形态表建立坐标系，再读 §31.8 的判定表，最后用 §31.9 的三本账算钱；被追问「为什么当初换 MQ 反而更慢」读 §31.1 的三起事故与 §31.10 的边界表。只想知道结论的翻 §31.13。

---

## 31.1 引子：三起「换了 MQ，业务里多了一层」的事故

这一节的三个故事有一个共同形状：**换 MQ 的动机都是真实的**（要逐条 ack、要延迟、要存算分离），换完之后系统也确实跑起来了，但**每一种新队列的「语义缺口」都被业务代码补成了新的一层**——一张事务表、一张扫描表、一层重试封装。三个月后，出事的是那一层，不是 MQ。

### 事故一：换成 RabbitMQ 之后，业务库多了一张 `local_message` 表（2026-01-14）

某 SaaS 计费平台原用 Kafka：支付回调服务先写 `payment` 表，再 `send()` 到 `pay.success`。迁移目标写得很具体——「消费失败要能单独重试 3 次后进死信」「要有延迟对账」「运维要能看到单条消息的堆积状态」。AMQP 的 DLX、per-message `deliveryTag`、管理台三件都对得上，于是上了 RabbitMQ 3.13（classic → quorum queue）。

迁移评审漏掉了一页：**AMQP 的事务（`tx.select` / `tx.commit`）只覆盖一个 channel 内的 publish 与 ack，覆盖不到 MySQL 的 `COMMIT`**。也就是说「写库成功」与「消息投出去」之间没有原子性——这恰恰是 Kafka 上也一样没有、但团队原来靠 Kafka 事务 + 消费端幂等勉强糊住的那一段。

补法很自然：建 `local_message(id, status, payload, retry, created_at)`，业务事务里插一行，再由一个 `@Scheduled` 任务扫出来投递、投成功改状态。这就是标准 outbox。**问题不在 outbox，在于它跑在三个副本上、且消费端没有幂等键。**

| 时间 | 事件 | 关键数字 |
| --- | --- | --- |
| 01-06 | 切换完成，RabbitMQ quorum queue 3 副本；`local_message` 扫表任务 3 副本同时部署 | 日支付 620 万笔 |
| 01-06 ~ 01-13 | 稳态：扫表单实例每 5 s 扫 500 条，重复投递被「投递前查状态 + 乐观锁」挡住 | 重复率 < 0.02% |
| 01-14 19:40 | 大促开场，单分钟支付从 1,200 涨到 3,400 笔，扫表来不及 → 待投积压 8.6 万条 | 积压 8.6 万 |
| 19:52 | 两副本的乐观锁 `UPDATE ... WHERE status=0` 因主从延迟读到旧值而**同时成功**（读走从库） | 重复投递 **2.4 倍** |
| 20:05 | 「积分到账」消费者无幂等键，重复发积分 | 重复 21.7 万笔 |
| 20:40 | 运维把 `prefetch=250` 调到 1000 想「加快消费」，未 ack 消息堆到内存高水位 | 发布端进入 `blocked` |
| 21:10 | 发布端被 block → 支付回调线程池排队 → 下单接口 P99 从 180 ms 到 4.2 s | 下单失败 1.9 万次 |
| 01-15 02:00 | 修：扫表改单实例（Redis 锁）+ 消费端补幂等表 + `prefetch` 回 200 | 恢复 |
| 复盘 | 重复发放积分 21.7 万笔折 31 万元；对账补数 2 天；客诉 470 单 | ≈ 31 万元 |

**教训不是「RabbitMQ 不如 Kafka」，而是「push 模型的背压阀是 `prefetch`，它同时是内存水位和消费并发的旋钮」。** 在 Kafka 的 pull 模型里，「消费慢」表现为 lag 上涨、盘上堆着；在 RabbitMQ 的 push 模型里，「消费慢」表现为**broker 内存上涨、然后发布端被 block**（§31.6）。同一个操作（调大预取）在两种模型里一个治吞吐、一个埋事故。

### 事故二：延迟等级不够，只能自己扫表（2026-02-03）

某电商订单中心用 RocketMQ 4.9 做「下单后超时关单」。最初需求是「30 分钟未支付关单」——用延迟等级 16（`30m` 那一级）投一条延迟消息，到点消费即可，一行配置。

半年后业务改成「按商家配置 1–90 分钟任意整数分钟」，并把「关单前 5 分钟提醒」也挂到同一条链路。RocketMQ 4.x 的延迟是**固定等级**（`messageDelayLevel` 默认 18 级，见 §31.4），1–90 分钟这个区间里只有 14 个可用的整分钟点。团队的选择题是：

- 向上取整到最近等级 → 平均多等 1–9 分钟，仓库最在意的「释放库存的时效」被破坏；
- 保持精确 → 自己建 `order_delay_task(order_id, expire_at)` 表 + 每分钟扫。

他们选了后者，并给它配了索引。

| 时间 | 事件 | 关键数字 |
| --- | --- | --- |
| 02-01 | `order_delay_task` 上线，`expire_at` 有二级索引，3 副本各跑一个 `@Scheduled` | 表 1,840 万行 |
| 02-01 ~ 02-02 | 每分钟扫出待办约 4,000 行，单次 380 ms | 正常 |
| 02-03 00:00 | 大促开场，待办涨到每分钟 12 万行；三副本重复处理且无幂等键 | 每轮 3 倍 |
| 00:07 | 单次扫描 40 s → 7 分 40 秒；扫描期间行锁 + undo 版本链抬升，`UPDATE` 排队 | P99 写 1.4 s |
| 00:31 | 一笔 00:00 就该关的单，00:31 才被扫到；期间库存被别的渠道卖光 | 超卖 1,240 件 |
| 00:31 ~ 02:00 | 关单延迟最大 32 分钟；「前 5 分钟提醒」在同一轮里被跳过（扫表只处理到期，不处理「即将到期」） | 漏提醒 6.2 万条 |
| 02-04 | 修：迁到 RocketMQ 5.x 定时消息（任意 `deliverTimeMs`）+ 关单幂等键 | 恢复 |
| 复盘 | 超卖赔付 38 万元；漏提醒客诉 610 单；扫表把订单库拖慢 3 天 | ≈ 38 万元 |

**「延迟等级不够就自己扫表」是消息中间件里最常见的一次形态误用。** 扫表把「定时」变成了「轮询一张关系表」，于是延迟精度 = 扫描周期、吞吐上限 = 表的索引扫描能力、幂等 = 你自己写，而这三件事在 RocketMQ 5.x 的定时消息、Pulsar 的 `deliverAfter`、或一个专门的时间轮里都是内核责任（→ comp/36）。

### 事故三：Pulsar 存算分离后，BookKeeper 的一次抖动把 P99 打穿（2026-03-11）

某实时风控平台从 Kafka 迁到 Pulsar 3.0，卖点是「broker 无状态、存储独立扩、多订阅各自回放」。拓扑：8 台 broker（无盘）+ 12 台 bookie，同机房三机架，`ensemble=3 / writeQuorum=3 / ackQuorum=2`（Pulsar 的默认值随版本与「auto」策略变化，此处是生产显式配置，以目标版本文档为准）。

迁移后稳态很好：写入 P99 8 ms、端到端 P99 45 ms。**但写路径的每一个 ack 都要等 bookie**——这是存算分离的定义决定的，不是一个可调参数：

| 时间 | 事件 | 关键数字 |
| --- | --- | --- |
| 03-11 14:02 | bookie-07 上的日志采集 agent 开始打包历史日志，journal 盘 util 从 12% 起 | — |
| 14:09 | 该盘 journal `fsync` P99 从 0.8 ms 涨到 260 ms | 260 ms |
| 14:10 | 12 台 bookie 中，`ensemble` 含 bookie-07 的 ledger 区块写入变慢；按「每 ledger 随机选 3 台」估算约 25% 的 topic 受影响 | 320 topic 中约 80 |
| 14:11 | 受影响 topic 的 `publish` P99 从 8 ms 到 780 ms；未受影响的不变 | 8 → 780 ms |
| 14:13 | 业务侧同步 HTTP 回调超时设为 500 ms → 上游重试 → 重复投递 | 18.4 万条 |
| 14:20 | 运维「加 broker」扩容 2 台：**写延迟一点没变**（瓶颈在 bookie 盘） | 无效 |
| 14:35 | 再「加 bookie」2 台：**已有 ledger 不迁移**，只有新 ledger 用上它们，慢盘仍在 25% 的 ensemble 里 | 仍无效 |
| 15:10 | 停掉采集 agent + 对受影响 topic 做 `unload` 触发 ledger 重建后恢复 | 停 68 分钟 |
| 复盘 | 风控晚判 68 分钟，事后命中 6,900 笔，赔付 44 万元；重复投递被幂等表挡住 96% | ≈ 44 万元 |

**这起事故的两个「无效动作」比事故本身更值钱**：存算分离让你能**分别**扩计算和存储，但它不承诺「扩了就会均衡」。broker 加台不减 bookie 的慢盘惩罚；bookie 加台不迁移已有 ledger（新 bookie 只承载新 ledger，除非显式 `unload` 或跑 auto-recovery）。**存算分离把「扩容」从一件事变成两件事，还额外多了一件「重分布」。**

> 🩸 **三起事故的共同点**：没有一次是「MQ 挂了」，三次都是「MQ 的语义形状与业务的假设不一致，差的那一层被写在业务代码里」。事故一是 push 模型的背压阀被当成吞吐旋钮；事故二是延迟等级的形状装不下需求；事故三是存算分离的写路径把下游的抖动直接传到了 P99。**这就是本篇的唯一真问题。**

---

## 31.2 一句话定位与五维形态表

**定位**：Kafka、RocketMQ、Pulsar、RabbitMQ、NATS 都属于「消息中间件」，但它们把消息存成的**形状**完全不同——partition log（每分区一条只追加日志）、queue（每个队列一份可被多人竞争取走的消息集）、ledger（分段账本 + 独立游标）、subject（树状地址 + 可选持久化流）。形状决定了三件互相关联的事：**一次写要落到几个物理位置、一次投递由谁发起、一次投递能保证什么语义**。

### 唯一真问题

> **队列的差异不在吞吐，在语义形状：事务消息、延迟等级、推 vs 拉、存算分离，选错就得在业务里补一层。**

这句话拆成四条推论，全篇都在给它们找证据：

1. **吞吐是最不重要的维度。** 这五种系统的稳态吞吐都能到「万级到百万级 msg/s」区间，差异主要来自部署与参数，而不是形态；真正卡住你的是「你要的语义它有没有」。
2. **每一种「内核里没有」的语义，都会变成业务里的一层代码。** 事务消息没有 → outbox 表；延迟不够 → 扫表；重试没有 → retry topic + 计数；顺序没有 → 按 key 归并。这一层要付的账（幂等、对账、扫表延迟、额外存储）通常远大于「换个 MQ」的成本。
3. **push 与 pull 不是风格差异，是背压归属的差异。** pull 把「消费不过来」留在 broker 的盘上（lag），push 把「消费不过来」推到 broker 的内存上（`blocked` 发布端）。同一个调参动作在两种模型里后果相反。
4. **存算分离买的是「独立扩容」，卖的是「写路径变长」。** 每一次 ack 都要经过一次跨进程、跨机的复制写（§31.5），所以它的 P99 由**下游最慢的那台 bookie**决定，而不是由 broker 自己决定。

**它明确不承诺什么**（放在全篇开头，后面各节逐个兑现）：RocketMQ 不承诺「任意精确延迟在 4.x」（等级制）；Pulsar 不承诺「写入不等下游」（存算分离的必付项）；RabbitMQ 不承诺「大积压不占内存」（push + 内存水位）；NATS core 不承诺「消息不丢」（at-most-once 是定义）；而它们中没有一家承诺「跨你的数据库的原子性」——那一层永远在业务里。

### 五维形态表（本篇的坐标系）

| 维度 | Kafka | RocketMQ | Pulsar | RabbitMQ | NATS（core / JetStream） |
| --- | --- | --- | --- | --- | --- |
| **① 存储模型** | partition log：每分区一条只追加日志 + 段文件 + 稀疏索引（`→ comp/28`） | CommitLog（**全 topic 共用一个顺序写文件**）+ ConsumeQueue（每队列 20 B 定长逻辑索引）+ IndexFile | ledger：消息分段写进 BookKeeper 的 entry log，订阅位点以 cursor 形式持久化 | queue：每个队列一份消息集（classic 单机 v2 索引 / quorum 走 Raft 复制日志） | core 无存储（内存 subject 路由）；JetStream 有 stream（file/memory 存储 + 每 consumer 位点） |
| **② 消费模型** | **pull**（consumer 主动 `fetch`，位点在客户端/协调器） | **pull**（客户端长轮询，长轮询在体感上像 push） | **pull**（broker 侧 consumer 也有 push 语义，本质是 broker 替你拉再推给你） | **push**（broker 主动推，`prefetch`/QoS 是唯一背压阀） | core：push；JetStream：push consumer 与 pull consumer 都有 |
| **③ 订阅模型** | consumer group（分区在组内独占 + rebalance）（`→ comp/30`） | 消费组（集群消费）/ 广播消费；队列在组内分配 | **四种子订阅**：exclusive / failover / shared / key_shared | exchange → binding → queue；一个 queue 可多消费者竞争（work queue）；广播靠多 queue 绑同一 exchange | subject 层级 + queue group（同 group 内竞争）；JetStream 每 stream 多 consumer 各自独立位点 |
| **④ 事务与延迟** | 事务只在 Kafka 内部（consume-transform-produce）；**无原生延迟/按条 TTL** | **事务消息**（half message + broker 回查）；**延迟等级**（4.x 固定 18 级；5.x 支持任意时间） | 事务（跨 topic 的 read_committed）+ **任意延迟**（`deliverAfter` / `deliverAt`）+ negative ack 重投 | AMQP `tx` 事务（channel 级，吞吐差）→ 实践用 publisher confirm；延迟靠 **TTL + DLX**，有队头阻塞 | 无事务；无原生定时投递；JetStream 有 **去重窗口**（`Nats-Msg-Id` + `duplicate_window`） |
| **⑤ 存算分离** | **否**：broker 自带本地盘与 page cache（分层存储是扩展，以目标版本为准） | **否**：broker 本地 CommitLog（5.x 有分层/存算分离选项，以目标版本为准） | **是**：broker 无状态 + bookie 存；可分别扩 | **否**：队列在 broker 本地（stream 是另一形态，3.9+ 内建，以目标版本为准） | **部分**：JetStream 与 core 同进程，可单独扩集群 |

**读这张表的正确方式**：先找到你的需求落在哪一格，再去看那格的代价。要「事务消息」→ 只有 RocketMQ 把这半段做进了内核；要「任意延迟」→ Pulsar 与 RocketMQ 5.x；要「push + 逐条 ack + 优先级」→ RabbitMQ；要「极低延迟 + 极简运维 + 轻量事件」→ NATS；要「长保留 + 多消费组独立回放 + 高吞吐顺序流」→ Kafka。

### 解剖卡八问落点（本篇答案）

| 问 | 落在哪节 | 一句话答案 |
| --- | --- | --- |
| ① 承诺什么 | §31.2、§31.10 | 每家承诺的边界都在「它把哪条语义做进了内核」处断开，断口必须在业务里补 |
| ② 数据长什么样 | §31.3–§31.7 | CommitLog + ConsumeQueue / ledger + cursor / queue + Raft log / subject + stream |
| ③ 一次读写走哪条路 | §31.4–§31.7 各自一张分解表 | 差异最大的一跳是「ack 之前要等谁」：本地页缓存 / 多数派 bookie / 多数派 Raft 副本 |
| ④ 并发与一致 | §31.4、§31.5、§31.9 | RocketMQ 主从或 DLedger/Controller 多数派；Pulsar 由 E/W/A 三个数定义；RabbitMQ quorum 走 Raft |
| ⑤ 扩展维度 | §31.9 | RocketMQ 分队列 + 分 broker；Pulsar **存储按 bookie 扩、计算按 broker 扩**（本次唯一真正的「两维」）；RabbitMQ 垂直 + 多队列；NATS 多 stream |
| ⑥ 系统级失效 | §31.10、§31.11 | 集群不可用（bookie 慢导致写入雪崩 / Raft 失去多数派）、数据不一致（非同步复制的主从切换）、成本失控（写放大 × 副本 × 保留） |
| ⑦ 看哪几个指标 | §31.12 | 每家的「第一指标」不同：RocketMQ 看 CommitLog 盘水位与 CQ 构建延迟；Pulsar 看 bookie 写延迟；RabbitMQ 看内存水位与 unacked；NATS 看 `num_ack_pending` |
| ⑧ 换掉它的信号 | §31.8、§31.10 | 需要单队列百万级吞吐 / 需要跨地域强一致 / 需要「大积压 + 低内存」→ 形态不匹配，换零件 |

---

## 31.3 Kafka 的语义缺口（只给结论 + 锚点，不复述）

Kafka 的磁盘形态与复制协议在全仓已经讲透：日志为什么快、为什么不能删单条，看 `→ comp/28`；`acks`/ISR/HW 怎么联合决定丢不丢，看 `→ comp/29`；rebalance 与位点提交时机为什么**就是**语义，看 `→ comp/30`。本节只做一件事——**把 Kafka「内核里没有」的五条列出来，因为后面四种队列的差异，本质是「它们各自补掉了其中几条」**。

| # | Kafka 的语义缺口 | 物理原因（一句） | 锚点 | 业务里要补的那一层 |
| --- | --- | --- | --- | --- |
| ① | **没有原生延迟队列 / 按条 TTL** | 过期是「段级的时间戳判断」，内核里没有「按条到期」的调度结构 | `→ comp/28` §28.9、`→ comp/36` | 外挂延迟队列 / 时间轮 / 另一套 MQ（RocketMQ 延迟等级、Pulsar `deliverAfter`、RabbitMQ TTL+DLX） |
| ② | **事务消息要自己补** | Kafka 事务的原子边界是「消费位点 + 生产记录」，覆盖不到你的 MySQL | [eng/24](../eng/24-delivery-semantics.md) | outbox / 本地消息表 / CDC 抽取（`→ comp/33`）；或换 RocketMQ 事务消息 |
| ③ | **分区数只能增不能减** | `hash(key) % N` 的分区映射，N 变则同一个 key 的新旧消息跨分区 | `→ comp/28` §28.9 | 新 topic + 双写 + 切换；或消费端按 key 归并 |
| ④ | **消费组 rebalance 有停消费窗口** | 组协调协议要求「所有人停下重分家务」，窗口与成员数、分区数相关 | `→ comp/30` | 只能压小（static membership / cooperative sticky），消不掉；或改用「订阅各自独立位点」的模型（Pulsar 子订阅、JetStream consumer） |
| ⑤ | **「重试 N 次后进死信」没有原生语义** | 内核只有「位点」，没有「这条消息的第几次投递」 | [eng/26](../eng/26-message-reliability.md) | 消费端计数 + retry topic + DLQ 转发（RabbitMQ 的 `x-death` 是唯一把它们做进内核的一家） |

**这张表是本节最有用的东西**：它把「要不要换 Kafka」从偏好问题变成对表问题——如果你需要的是 ③④⑤，换 MQ 未必划算（那是配置与协议的边界）；如果你需要的是 ①②，那么**内核级的补齐**（RocketMQ 事务消息、Pulsar/RocketMQ 的任意延迟）确实是真实的省力，省掉的正是事故一与事故二里那一层自建代码。

反过来也要看清 Kafka 唯一不可替代的那一条：**同一份字节，多消费组各自独立、按位点、长窗口重放**。这一条在 RabbitMQ（消息 ack 后即消失）上不成立，在 NATS core（不存储）上不成立，在 RocketMQ（消费后 CommitLog 按保留期清理，但同一条消息默认只被一个消费组消费——多消费组要各自建 group 并各自回放，能力有但对同一份物理消息的共享程度不如 Kafka）上也弱一些。**所以「多个下游都要各自从头重放同一份历史」这个需求，是本篇唯一一条「答案永远是 Kafka」的判据。**

---

## 31.4 RocketMQ：CommitLog + ConsumeQueue + IndexFile 的三件套

**一句话**：RocketMQ 把「所有 topic 的所有队列」压进**一条顺序写的 CommitLog**，然后用两份派生索引（定长的 ConsumeQueue、哈希的 IndexFile）把「逻辑队列」和「按 key 点查」重新还原出来。它买的是一次顺序写，卖的是**读要两跳**。

### 物理布局

```text
RocketMQ 的三份文件（同一台 broker 上，所有 topic 共享同一组 CommitLog）

store/
├── commitlog/
│   └── 00000000000000000000            ← 单文件 1 GB 量级（默认，可配），文件名 = 起始物理偏移
│       ├── [A: topic=order, queueId=0] ← 约 90 B 量级定长头（含 queueOffset 8 B + physicalOffset 8 B）
│       ├── [B: topic=pay,   queueId=3] ← 别的 topic 插在中间：共享同一条顺序写
│       ├── [C: topic=order, queueId=0] ← 与 A 同队列，但物理上不相邻
│       └── …                            物理偏移单调增长，永不回头、永不原地改
├── consumequeue/
│   ├── order/0/00000000000000000000    ← 定长 20 B/条 = commitLogOffset(8) + size(4) + tagHash(8)
│   └── pay/3/…                          ← 每个 topic 的每个队列一条「目录」（默认每文件 30 万条量级）
└── index/
    └── 20260114120000000               ← 按 key/uniqKey 哈希：40 B 头 + 500 万槽×4 B + 2000 万条目×20 B ≈ 400 MB

一次写 = 一跳：追加 CommitLog 末尾（顺序 IO，写放大 ≈ 1×）
一次读 = 两跳：① 顺序读 ConsumeQueue 取 (commitLogOffset, size)
              ② 按该偏移去 CommitLog 定位读（随机 IO，可能命中 page cache）
⇒ 同一队列的消息在 CommitLog 里是「稀疏」的 —— 中间夹着别的 topic/队列
⇒ 这就是 §31.9 账一里「读放大」的来源：CQ 是顺序的，CommitLog 不是
```

### 一次写入走哪条路（每跳都要标出「ack 在等谁」）

| 跳 | 动作 | 量级 | 版本 / 开关前提 |
| --- | --- | --- | --- |
| ① | producer → broker（Netty） | 同 AZ 0.3–1 ms | — |
| ② | 追加 CommitLog（写 page cache / mmap） | 5–50 μs | 与 Kafka 同理：这一步不是落盘 |
| ③ | **刷盘**：`ASYNC_FLUSH`（默认）/ `SYNC_FLUSH` | 异步：不在 ack 路径；同步：组提交下每批一次 `fsync`，NVMe/SSD 上 0.3–2 ms 量级 | `flushDiskType`，broker 级配置 |
| ④ | **复制**：`ASYNC_REPLICATION`（默认）/ `SYNC_REPLICATION` / DLedger / 5.x Controller 模式 | 异步：不在 ack 路径；同步：**多等 1 个 RTT 到多数派** | 主从为 `brokerRole`；DLedger 在 4.5+，5.x 推荐 Controller 模式（JRaft），**具体以目标版本文档为准** |
| ⑤ | `ReputMessageService` 异步分发：CommitLog → ConsumeQueue / IndexFile | 默认毫秒级间隔（`reputInterval` 量级 1 ms），**「写入成功」到「可被消费」之间有一段构建延迟** | broker 级；构建落后会直接表现为「写入正常但消费者拉不到」 |
| ⑥ | consumer 长轮询拉取 | 长轮询挂起，无消息时由 broker hold 住请求 | `longPollingEnable`（默认开），超时以目标版本配置为准 |

第 ⑤ 跳是 RocketMQ 与 Kafka 最不一样的一处：**Kafka 写完就能读（位点推进即可见），RocketMQ 写完要等 CQ 构建完才能被消费**。所以 RocketMQ 的「实时性」多了一个可能出问题的环节——这个环节的积压就是 §31.12 里的观察点之一。

### 刷盘 × 复制：四个组合，四种丢数窗口

| 刷盘 | 复制 | ack 前等什么 | 主挂了的丢数窗口 | 适用 |
| --- | --- | --- | --- | --- |
| `ASYNC_FLUSH` | `ASYNC_REPLICATION` | 只等本地 page cache | **OS 崩溃丢一批 + 主挂丢未同步部分** | 日志、埋点、可重放事件 |
| `ASYNC_FLUSH` | `SYNC_REPLICATION` / DLedger | 等多数派写完 | 不丢已 ack（前提：多数派存活，且**磁盘掉电不在保护范围**） | 业务事件，容忍异步刷盘 |
| `SYNC_FLUSH` | `ASYNC_REPLICATION` | 等本机 fsync | 主挂时可能丢「已 ack 但从没追上」的部分 | 单机可靠、但高可用弱的场景 |
| `SYNC_FLUSH` | `SYNC_REPLICATION` / DLedger | 等 fsync + 多数派 | 最窄（仍不覆盖「多数派同时掉电」） | 支付、账务类 |

> 这张表与 `→ comp/29` 的表结构同源：**「不丢」永远是一组参数的联合体，不是某个开关。** RocketMQ 的区别是它把「多数派」这一层做成了可选项（DLedger / Controller），而 Kafka 早期版本没有这一层（KRaft 只解决元数据，不解决数据）。

### 事务消息：half message + 回查（RocketMQ 唯一独有的那半段）

```java
// RocketMQ 4.x / 5.x 事务消息（org.apache.rocketmq.client）
TransactionMQProducer producer = new TransactionMQProducer("pay_tx_producer");
producer.setNamesrvAddr("rmq-namesrv:9876");
// 回查线程池：broker 主动回调这里，不是生产者主动上报
producer.setExecutorService(new ThreadPoolExecutor(4, 8, 60, TimeUnit.SECONDS,
        new ArrayBlockingQueue<>(2000), new ThreadFactoryImpl("tx-check-")));
producer.setTransactionListener(new TransactionListener() {
    @Override public LocalTransactionState executeLocalTransaction(Message msg, Object arg) {
        try {
            payService.markPaid(arg.toString());          // 本地事务：另一个库、另一个事务
            return LocalTransactionState.COMMIT_MESSAGE;
        } catch (BizRejectException e) {
            return LocalTransactionState.ROLLBACK_MESSAGE;  // 明确业务失败才回滚
        }   // 其他异常/超时 → 未知状态，落给 broker 回查（不要在这里吞异常返回 SUCCESS）
    }
    @Override public LocalTransactionState checkLocalTransaction(MessageExt msg) {
        // 必须是幂等只读查询；返回 UNKNOW 会被继续回查，直到达到最大回查次数
        return payService.isPaid(msg.getKeys()) ? LocalTransactionState.COMMIT_MESSAGE
                                               : LocalTransactionState.ROLLBACK_MESSAGE;
    }
});
producer.start();
producer.sendMessageInTransaction(new Message("pay.success", "PAID", orderId, body), orderId);
// 注意：这行返回成功 ≠ 全局提交，它只代表「half 消息已落 broker」
```

链路与代价：

| 步 | 物理动作 | 代价 |
| --- | --- | --- |
| ① | 投递写一条 **half message** 到内部 topic（`RMQ_SYS_TRANS_HALF_TOPIC`），对消费者不可见 | 第 1 次 CommitLog 写 |
| ② | 执行本地事务（你的 MySQL） | 一次本地事务 |
| ③ | `COMMIT` → broker 把这条 half 消息**恢复**成真实 topic 的消息，再写一次 CommitLog | **第 2 次 CommitLog 写**（同一条消息写两遍） |
| ④ | 生产者没回来（进程挂了 / 网络断）→ broker 定时回查 `checkLocalTransaction` | 回查次数上限 15 次量级、间隔 60 s 量级（`transactionCheckMax` / `transactionCheckInterval`，**以目标版本为准**） |

**所以事务消息买到的是「生产者崩溃时不会出现『库改了、消息没发』或『消息发了、库没改』」，代价是双倍写入 + 一条「回查必须幂等」的硬约束。** 它买不到「消费者侧的恰好一次」——那仍然要靠幂等键（[eng/24](../eng/24-delivery-semantics.md)）。同时它买不到「跨多条消息的原子性」：事务消息是**一条消息**一个事务，不是一批。

### 延迟等级：18 个档位能装下多少需求

RocketMQ 4.x 的延迟是**等级制**：消息只带一个 `delayTimeLevel`，broker 把它先写进 18 个内部延迟队列（`SCHEDULE_TOPIC_XXXX` 的对应队列），由 `ScheduleMessageService` 按秒级粒度扫描、到期后投回真实 topic。默认 `messageDelayLevel` 是 18 档：

| `delayTimeLevel` | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 延迟 | 1s | 5s | 10s | 30s | 1m | 2m | 3m | 4m | 5m | 6m | 7m | 8m | 9m | 10m | 20m | 30m | 1h | 2h |

于是「1–90 分钟任意整数分钟」这个需求（90 个取值）能精确命中的只有 `{1..10, 20, 30, 60}` 共 **13 个点**：

| 处理方式 | 覆盖情况 | 后果 |
| --- | --- | --- |
| 精确命中 | 13 / 90 = **14.4%** | 其余 77 个值必须降级 |
| 向上取整到最近档位 | 11–19→20、21–29→30、31–59→60、61–90→120 | **平均多等 24.2 分钟**（`(9×5 + 9×5 + 29×15 + 30×44.5) ÷ 77`），「释放库存的时效」直接破产 |
| 向下取整 | 最多提前 29 分钟 | 提前关单，破坏 SLA 且无法申诉 |
| 自己扫表 | 精确 | 事故二：延迟精度 = 扫描周期、吞吐 = 索引扫描能力、幂等 = 自己写 |

> **量级结论（非承诺值）**。假设：① 业务取值是 1–90 分钟内均匀分布的整数分钟；② 4.x 默认 18 档 `messageDelayLevel` 未被改动；③ 仅考虑「单次延迟」，不考虑「多次延迟串成任意时间」。**RocketMQ 5.x 引入了定时消息（timer message），支持按 `deliverTimeMs` 指定任意时间点，上限以目标版本参数为准（天级量级）；具体实现从「18 个延迟队列」换成「时间轮 + timerlog」，不要拿 4.x 的等级表去解释 5.x 的行为。**

### 顺序消息：队列锁与「一条卡住全队列」

RocketMQ 的顺序是**队列级**的，由两段代码合起来实现：生产端按业务键选同一个 `MessageQueue`，消费端对每个 `MessageQueue` 加锁串行处理。

```java
// ① 生产端：同一 orderId 必须落到同一 MessageQueue（否则消费端再串行也没用）
producer.send(msg, (queues, m, arg) -> {
    int idx = Math.abs(arg.toString().hashCode()) % queues.size();
    return queues.get(idx);
});

// ② 消费端：Orderly 监听 → 同一队列在同一实例内串行（队列锁），不同队列并行
consumer.registerMessageListener((MessageListenerOrderly) (msgs, ctx) -> {
    for (MessageExt m : msgs) {
        try {
            closeService.close(m);                      // 必须幂等：rebalance 转移动窗口会重投
        } catch (Exception e) {
            return ConsumeOrderlyStatus.SUSPEND_CURRENT_QUEUE_A_MOMENT;  // 整条队列暂停重试
        }
    }
    return ConsumeOrderlyStatus.SUCCESS;
});
```

三点必须记住的代价：

| 代价 | 机制 | 现场 |
| --- | --- | --- |
| **队头阻塞** | 返回 `SUSPEND_CURRENT_QUEUE_A_MOMENT` 会让**整个队列**暂停一小段时间（`suspendCurrentQueueTimeMillis` 默认 1 s 量级） | 一条毒消息能让该队列停 1 s/次 × 重试次数；并发消费（`MessageListenerConcurrently`）则不会，但它不保序 |
| **并行度上限 = 队列数** | 顺序的粒度是队列，队列数固定则单 topic 的并行消费上限固定 | 一个 topic 只建 4 个队列 + 要顺序 → 最多 4 个消费线程有效，加实例只是空转 |
| **转移窗口不保序** | rebalance 时队列换实例，上一条可能仍在旧实例处理中 | 必须靠业务幂等（[eng/24](../eng/24-delivery-semantics.md)）兜住，不能靠「顺序」兜 |

**一句话**：**顺序消息不是「更强的并发消费」，而是「用并行度换有序」的另一种消费模型**——它与 Kafka 的「分区内有序 + 分区独占」同构，区别只是 RocketMQ 把队列锁做进了客户端（`MessageQueueLock`），Kafka 靠 rebalance 把分区独占在组内。两者都不承诺「跨队列有序」，也都不承诺「转移窗口内不重投」。

---

## 31.5 Pulsar：broker / Bookie 分离，ledger + cursor

**一句话**：Pulsar 把「消息存哪」和「消息怎么被服务」拆成两个集群——broker 无状态、只管协议与派发；存储交给 BookKeeper 的 bookie，消息以 **ledger**（一段不可变的 entry 日志）为单位分布到多个 bookie 上，每条订阅（subscription）各有一个 **cursor** 记录自己读到哪。**它是本篇唯一一个「存算真分离」的形态，也是唯一一个「每一次 ack 都必须等下游」的形态。**

### 物理布局与写路径

```text
Pulsar：topic = 一串 ledger；订阅 = 一个持久化的 cursor

topic: persistent://risk/order/events
  ├── ledger-41  ──► entry log 分布在 bookie {03,07,11}   （ensemble E=3）
  ├── ledger-42  ──► entry log 分布在 bookie {02,07,09}   （writeQuorum W=3 / ackQuorum A=2）
  └── ledger-43  ──► 正在写的当前 ledger（写满条数/时间就 roll）
  subscriptions:
    ├── sub "risk-realtime"  cursor → ledger-43, entry 8,120   ← 各自独立位点
    ├── sub "risk-backfill"  cursor → ledger-38, entry 2,004   ← 回放三个月前的同一份数据
    └── sub "audit"          cursor → ledger-43, entry 7,905

一次写（broker 不在本地落盘，必须等 bookie）：
  producer ──①──► broker ──②──► BookKeeper client ──③──► E 个 bookie 并行写 entry（各自 journal fsync）
                                          └──④──► 收到 A 个 ack ⇒ 才算这条消息「写成功」
  ⇒ ack 的等待对象 = 下游 bookie 的 fsync，而不是 broker 自己的 page cache
  ⇒ 任何一台被选进 ensemble 的慢 bookie，都会把「包含它的那部分 ledger」的写延迟拉起来
```

| 跳 | 动作 | 量级 | 备注 |
| --- | --- | --- | --- |
| ① | producer → broker | 同 AZ 0.3–1 ms | — |
| ② | broker 攒批 + 入队 | 客户端 `batching` 默认开启，攒批延迟毫秒级（`batchingMaxPublishDelay` 量级 1 ms，以目标版本文档为准） | 关掉攒批会显著抬高单条开销 |
| ③ | BookKeeper 写 E 份 entry | 1 个 RTT（同 AZ 0.3–1 ms）× 并行 | 跨 AZ 部署 bookie 时这个 RTT 变成跨 AZ 延迟 |
| ④ | bookie journal `fsync` | NVMe 组提交下 0.3–2 ms 量级；慢盘/GC 时可到几十至几百 ms | `journalSyncData` 默认 true（每次写等 journal 落盘），参数与默认值以目标版本文档为准 |
| ⑤ | 等 A 个 ack 后返回 producer | 第 ③④ 步的最慢者 | **P99 由 ensemble 中最慢的那台决定**（事故三） |
| ⑥ | cursor 推进持久化 | 周期写回 bookie，不在每条消息的 ack 路径上 | 消费位点也吃存储，容量规划要算 |

**第 ⑤ 行是全篇最贵的一句话**：Kafka 的 `acks=all` 等的是「follower 把数据写进它的 page cache」，Pulsar 等的是「bookie 把 entry 写进它的 journal（fsync）」。前者可以随 OS 回刷便宜地批量完成，后者是每条（或每组）都要一次真实的磁盘同步——**这就是「Pulsar 写入 P99 通常比 Kafka 高一档」的物理原因，与产品好坏无关。**

### 四种订阅：并发度与顺序的四个取舍

| 订阅类型 | 消费者数量 | 并行度 | 顺序 | 故障时的行为 | 典型用途与坑 |
| --- | --- | --- | --- | --- | --- |
| **exclusive** | 只能 1 个 | 1（单消费者串行） | **全 topic 有序** | 消费者死 → 该订阅停住，直到它回来或手动断开 | 强顺序流；坑：它天然是单点吞吐上限 |
| **failover** | 多个，按名字排序 | 1（只有主消费者收） | **全 topic 有序** | 主死 → 下一个接管，位点由 cursor 保证不丢 | 主备式顺序消费；坑：**备用消费者完全空转**，扩机器不提吞吐 |
| **shared** | 多个，轮询派发 | = 消费者数 | **无序**（同一 key 可能给不同消费者） | 消费者死 → 其未 ack 消息按 `ackTimeout` 重投 | 最常见的并行消费；坑：**不要在这种订阅下写「必须有序」的业务** |
| **key_shared** | 多个，按 key 哈希粘住 | = 消费者数（受 key 分布影响） | **同 key 有序** | 消费者死 → 它负责的 key 重新分配，可能造成短暂乱序 | 折中方案；坑：**key 倾斜 = 消费者倾斜**（与 Kafka 的分区倾斜同源） |

这张表值得与 `→ comp/30` 的 consumer group 对照读：Kafka 只有一种模型（分区独占 + rebalance），Pulsar 把它拆成四种子订阅让你显式选——**选的其实是「顺序 / 并行度 / 故障行为」这三者的组合**。选错的表现是：要顺序却用了 shared（偶发乱序）；要吞吐却用了 failover（加机器无效）。

### negative ack 与延迟投递：两件「内核里有」的语义

| 能力 | API（Java 客户端） | 语义 | 与别家对照 |
| --- | --- | --- | --- |
| **negative ack** | `consumer.negativeAcknowledge(msg)` | 告诉 broker「这条我处理失败，稍后重投」，**不阻塞同订阅的其他消息** | 与 RabbitMQ 的 `basic.nack(requeue=true)` 语义相近，但 RabbitMQ 的 requeue 会把消息放回队列（可能又给同一个消费者，且影响队列顺序）；Pulsar 走单独的延迟重投，`negativeAckRedeliveryDelay` 默认 60 s 量级（可配，以目标版本文档为准） |
| **ack 超时重投** | `ackTimeout(...)` | 「拿到但没在时限内 ack」→ 自动重投，**不看失败原因** | 这就是 RabbitMQ `consumer_timeout` / SQS 可见性超时的同类物（`→ comp/32`） |
| **延迟投递** | `newMessage().deliverAfter(90, TimeUnit.MINUTES)` / `deliverAt(instant)` | 任意延迟，broker 侧登记到延迟跟踪器，到点才派发 | RocketMQ 4.x 是等级制（§31.4），RabbitMQ 要靠 TTL+DLX（§31.6），**Pulsar 是三家里面把「任意时间」做得最直的** |

```java
// Pulsar：shared 订阅 + 负确认 + 任意延迟（只保留决策相关的行）
Consumer<byte[]> consumer = client.newConsumer()
        .topic("persistent://risk/order/events")
        .subscriptionName("risk-realtime")
        .subscriptionType(SubscriptionType.Shared)          // 要并行就 Shared；要顺序换 Failover/Exclusive
        .ackTimeout(30, TimeUnit.SECONDS)                    // 拿到不 ack → 自动重投（幂等必须自己保证）
        .negativeAckRedeliveryDelay(10, TimeUnit.SECONDS)    // 显式失败后的重投间隔
        .subscribe();

Message<byte[]> msg = consumer.receive();
try {
    riskService.evaluate(msg.getValue());
    consumer.acknowledge(msg);                               // 只有业务落地后才 ack
} catch (RetryableException e) {
    consumer.negativeAcknowledge(msg);                        // 交回 broker，不阻塞其他消息
} catch (Exception e) {
    consumer.acknowledge(msg);                               // 毒消息：ack 掉并落库，避免无限重投
}

// 任意延迟：90 分钟后才派发给该订阅
producer.newMessage().deliverAfter(90, TimeUnit.MINUTES).value(payload).send();
```

**两条必须知道的边界**：① 延迟中的消息仍然在 ledger 里、但派发被推迟，broker 需要为它们维护跟踪状态——**百万级延迟消息会吃 broker 堆**（这一点与 RocketMQ 4.x 把消息落到延迟队列再扫描的形态不同）；② 官方文档写明延迟投递主要在 `Shared` 订阅下生效，`Exclusive` / `Failover` 下的行为随版本有调整（**以目标版本文档为准**）——如果你的设计是「failover 订阅 + 延迟投递」，先验证再承诺。

### 存算分离的四笔代价

| 代价 | 机制 | 你在哪里感受到它 |
| --- | --- | --- |
| **① 写放大（副本 × 双写）** | 每条 entry 写 E 份（entry log）+ 每份在 journal 里再顺序记一遍；E=3 → entry 写 3 份、journal 写 3 份 | 磁盘总写入 ≈ payload × E × 2 量级，见 §31.9 账二 |
| **② 跨组件延迟** | ack 要跨「broker → bookie」一次 RTT + bookie fsync | 写入 P99 高一档；跨 AZ 部署时再翻倍 |
| **③ 扩容单位变成两个 + 一个重分布** | broker 无状态可随意扩；bookie 扩了**不迁移旧 ledger**（只有新 ledger 用新 bookie） | 事故三的第二个「无效动作」：加 bookie 不减慢盘影响，要 `unload` 或 auto-recovery |
| **④ 运维面翻倍** | broker 集群 + bookie 集群 + 元数据（ZooKeeper/etcd）+ 两套 SLO | 值班要看两组指标，故障域变成两组，报警语要重写 |

反过来，存算分离买到的东西也是真的：**存储按 bookie 独立扩（不必搬 broker 上的数据）、broker 扩容是同质加机器（不触发数据迁移）、多订阅各自回放不增加存储**。这三条恰好是 Kafka 最难受的三处（`→ comp/28` §28.8 账四：Kafka 扩容几乎总是加盘、加盘就要重分配分区）。

---

## 31.6 RabbitMQ：AMQP 的 exchange / queue / binding 与 push 模型

**一句话**：RabbitMQ 的存储单位是**队列**，路由单位是 **exchange + binding**。消息投递是**broker 主动推**，所以它必须有一个背压阀（`prefetch`），而这个阀同时控制「消费并发」和「broker 内存水位」。它的高可用从 3.x 的镜像队列换成 4.0 起的 **quorum queue（Raft）**，从「复制消息」变成「复制日志」。

### 路由：exchange 是「按什么规则分发」，queue 才是「存在哪」

| exchange 类型 | 路由规则 | 一条消息去哪 | 常见误用 |
| --- | --- | --- | --- |
| `direct` | routing key **完全相等** | 命中 binding 的那一个（或几个）queue | 把「同一事件多下游」写成多个 direct binding，然后忘了它们是**各存一份**（存储 × N） |
| `fanout` | 忽略 routing key，广播 | 所有绑定到它的 queue | 以为它「广播给所有消费者」——实际每个 queue 只有一个消费者拿到它 |
| `topic` | 通配匹配：`*` 一级、`#` 多级 | 匹配模式的 queue 集合 | 用 `#` 兜底做「全量审计」，结果审计 queue 成了全站消息的第一大存储 |
| `headers` | 匹配消息头 | 匹配的 queue | 几乎不用；性能与可读性都不如 topic |

**关键结构事实：RabbitMQ 里「一条消息被几个消费者看到」由「几个 queue 绑定」决定，而不是由「几个消费者」决定。** 一个 queue 上挂 3 个消费者 = 3 人**竞争**同一条消息（work queue）；要让两个下游各拿一份，就得建 2 个 queue。这一点与 Kafka 的 consumer group 正好相反（group 内竞争、group 间各一份），是迁移时最容易踩空的一格。

### push 模型与 prefetch：唯一的背压阀

```text
RabbitMQ 的推送节奏（push）与 Kafka 的拉取节奏（pull）的差别，全在这个水龙头里

  queue ──push──► consumer   未 ack 的消息计数 in-flight
    │                          ▲
    └── 只有当 in-flight < prefetch（basic.qos 的 prefetch_count）时，才继续推
                                    │
   prefetch 太小 ──► 消费者空转等推送（吞吐低，RTT 摊不开）
   prefetch 太大 ──► 未 ack 消息堆在 broker 与消费者内存里 → 内存水位 → 发布端被 block
                                                          （事故一 20:40 的那一步）
  Kafka：没有这个阀门。消费慢 = lag 上涨 = 数据留在盘上，broker 内存不动
```

| 动作 | 该看哪个数 | 拧错的样子 |
| --- | --- | --- |
| `prefetch` 调大（想提吞吐） | `messages_unacked`（每消费者 in-flight 数） | in-flight 涨 → broker 与消费端内存涨 → `vm_memory_high_watermark` 触发 → **发布端连接进入 `blocked` 状态** |
| `prefetch` 调小（想省内存） | 消费者 CPU 利用率、`message_stats.publish_details.rate` | 消费端空转、吞吐被 RTT 摊薄；单条处理很快时尤其明显 |
| 关掉自动 ack | `messages_ready` / `messages_unacked` 的比值 | 自动 ack + 处理失败 = **消息直接消失**（at-most-once） |

**这条是 §31.6 的核心**：RabbitMQ 的「消费慢」不会像 Kafka 那样变成「盘上堆着」，而是变成「内存堆着，然后所有人一起被 block」。**这就是 push 与 pull 的形态差异在故障时的样子**——它不是好坏，是两种完全不同的容量规划方式（RabbitMQ 要规划**内存与未 ack 数量**，Kafka 要规划**磁盘与保留期**）。

### classic vs quorum：从「复制消息」到「复制日志」

| 维度 | classic queue（3.x 时代） | quorum queue（3.8+ 引入，4.0 起是 HA 的唯一道路） |
| --- | --- | --- |
| 存储结构 | 每队列的单机索引（v2 索引 + 消息存储），**消息在哪个节点就在哪个节点** | 每队列一个 **Raft 组**：先写 Raft log（WAL + 段文件，多数派确认），再应用到队列 |
| 高可用 | 3.x 靠 mirrored queue（主从复制 + 同步窗口），4.0 起已移除 | Raft 多数派，leader 挂了自动选 |
| 写入路径 | 一次本地写 + fsync 由 `queue_index_embed_msgs_below`/刷盘策略决定 | **每条消息（或每批）要走 Raft 追加并等多数派** → 多一次复制与 fsync |
| 吞吐量级 | 单队列数万 msg/s 量级（小消息、批量 confirm） | **单队列数千到一万余 msg/s 量级**（延迟主要花在 Raft 提交与 fsync 上，与消息大小、批大小、盘强相关） |
| 内存 | 消息体可留在内存（`lazy` 模式可强制落盘） | 消息体在段文件里，内存里主要是索引与 in-flight 状态（**具体保留策略与版本相关，以目标版本文档为准**） |
| 什么时候选 | 单机、可重建、允许丢（如缓存失效通知） | 任何「不能丢」的业务队列（默认应选它） |

> 版本纪律：`x-queue-type: quorum` 是声明队列时的参数；`x-queue-mode: lazy` 属于 classic 队列的磁盘优先模式，3.x 常见、**4.x 的 classic 队列存储已被重写（v2 索引 + 共享消息存储），参数与行为随版本变化——以目标版本文档为准**。写错这一条，读者就会认为全篇没验证过。

### TTL + DLX 做延迟：为什么必然队头阻塞

```text
RabbitMQ 没有「按条定时投递」。标准做法是 TTL + 死信交换机（DLX）：
消息在队列里待到过期 → broker 把它投到 DLX → 另一个队列消费

  queue "delay"  ── 每条消息带自己的 x-message-ttl（或队列级 TTL）
     队头: [msg A, TTL=30m]   ← 只有队头会被判过期
           [msg B, TTL=1m ]   ← A 没到期，B 就算到期了也不会先出队
           [msg C, TTL=5m ]
  ⇒ 单队列 + per-message TTL = 队头阻塞：延迟精度被「队头最长的那条」决定
  ⇒ 正确做法：每个延迟档位一个队列 + 队列级 TTL（x-message-ttl 设在队列上）
      delay-1m / delay-5m / delay-30m … → 每档内部先进先出，档间互不影响
  ⇒ 代价：档位数 = 你在「延迟精度」上愿意付的队列数；要「任意分钟」就得 90 个队列
```

| 方案 | 精度 | 代价 | 什么时候可用 |
| --- | --- | --- | --- |
| 单队列 + per-message TTL | 被队头阻塞，实际不确定 | 最省队列 | 基本不可用于 SLA |
| 每档一个队列 + 队列级 TTL | = 档位粒度 | 队列数 = 档位数；每档一个消费链路 | 档位少（如 1m/5m/30m/2h 四档） |
| 延迟插件（`rabbitmq_delayed_message_exchange`） | 毫秒级，任意时间 | 延迟中的消息**存在 Mnesia 里**（不是队列里），量大时内存/磁盘压力大，且是社区插件 | 中小规模、延迟消息占比不高 |
| 外部调度（时间轮 / Redis ZSet / 扫表） | 由你的调度器决定 | 自建，幂等自己保证 | 已在用一整套调度基建（`→ comp/36`） |

```java
// RabbitMQ：quorum queue 声明 + prefetch + 手动 ack + 死信（只保留决策相关的行）
ConnectionFactory f = new ConnectionFactory();
f.setHost("mq.internal");
Channel ch = f.newConnection().createChannel();
ch.exchangeDeclare("pay.dlx", "direct", true);

Map<String, Object> args = new HashMap<>();
args.put("x-queue-type", "quorum");          // 4.x 起做 HA 的唯一选择
args.put("x-dead-letter-exchange", "pay.dlx");
args.put("x-delivery-limit", 5);              // quorum queue 支持按投递次数进死信（3.8+）
ch.queueDeclare("pay.success", true, false, false, args);

ch.basicQos(200);                              // prefetch：背压阀 + 并发度旋钮，两者是同一个数
ch.basicConsume("pay.success", false, (tag, msg) -> {
    try {
        pointService.grant(msg);               // 必须幂等：requeue / 重投都是常态
        ch.basicAck(msg.getEnvelope().getDeliveryTag(), false);
    } catch (RetryableException e) {
        ch.basicNack(msg.getEnvelope().getDeliveryTag(), false, true);  // requeue=true，可能又给同一个消费者
    } catch (Exception e) {
        ch.basicNack(msg.getEnvelope().getDeliveryTag(), false, false); // requeue=false → 走 DLX
    }
});
```

**注意 `basicNack(requeue=true)` 的两个陷阱**：① 消息会回到队列里（classic 下位置接近原位，quorum 下按 Raft 应用顺序），**可能立刻又投给同一个消费者**，形成热循环；② 它不改变消息内容，所以「毒消息」会用同样方式失败 N 次——正确的做法是**限量重试后 `requeue=false` 进死信**（quorum queue 的 `x-delivery-limit` 就是把这个计数做进内核的那一项，也是本篇唯一一家把「重试计数」做进内核的 MQ）。

---

## 31.7 NATS：subject 层级、core 的 at-most-once 与 JetStream

**一句话**：NATS 的核心是一张**内存里的 subject 路由表**——`publish(subject, bytes)` 进 broker，立刻按订阅关系推给匹配的客户端，**不落盘、不重试、不保证到达**（at-most-once）。JetStream 是在同一进程里加的一层持久化：把 subject 上的消息存成 **stream**，consumer 显式 ack、位点持久化、按 `Nats-Msg-Id` 去重。**它的形态更接近「带持久化的消息总线」，而不是「分区的日志」。**

### 两种形态的对照

| 维度 | core NATS | JetStream |
| --- | --- | --- |
| 存储 | **无**（broker 内存里的路由表） | stream：`file` 或 `memory` 存储；replicas（Raft）可 1/3/5 |
| 投递语义 | **at-most-once**（发了就忘，消费者掉线就丢） | at-least-once（显式 ack + `ack_wait` 超时重投 + `max_deliver` 上限） |
| 订阅模型 | subject 订阅；**queue group**（同名 group 内竞争一条） | stream + consumer：`deliver_policy`（all/last/new/by_start_sequence/by_start_time/last_per_subject）、push 或 pull |
| 顺序 | 同一 subject 内按到达顺序推给订阅者（**但没有「位点」这个概念**） | stream 内**按 sequence 单序**；并行靠「多 stream / 多 filter subject」 |
| 延迟 | **极低**（同一个进程内路由 + 单次网络往返，亚毫秒到毫秒） | 多一次磁盘写与 ack 确认；延迟上升到毫秒级 |
| 保留 | — | `limits`（按时间/条数/字节）、`interest`（有消费者才留）、`workqueue`（**ack 后即删，这是队列语义**） |
| 去重 | — | `Nats-Msg-Id` + `duplicate_window`（默认 2 分钟量级，可配）内同 id 只有一条落 stream |

> 参数纪律：`duplicate_window` 的默认值与上限、`max_deliver` 的默认值、`replicas` 的可选值都随 NATS Server 版本变化（**以目标版本文档为准**）。这一节的结论不依赖具体数值，只依赖「去重窗口是一个**时间窗口**而不是永久幂等」这一条形态事实。

```go
// NATS JetStream：幂等发布（Nats-Msg-Id）+ pull consumer 显式 ack/nak（Go 客户端）
js, _ := jetstream.New(nc)
ctx := context.Background()

// ① 发布：带 MsgId，服务端在 duplicate_window 内按 id 去重（超窗口后不保证）
_, err := js.Publish(ctx, "pay.success", payload, jetstream.WithMsgID(orderID))

// ② 消费：显式 ack，超时未 ack 由服务端重投
cons, _ := js.CreateOrUpdateConsumer(ctx, "PAY", jetstream.ConsumerConfig{
    Durable:       "point-grant",
    AckPolicy:     jetstream.AckExplicitPolicy,
    AckWait:       30 * time.Second,   // 超时 → 重投（与服务端版本/配置相关）
    MaxDeliver:    5,                  // 超过次数进入配置的死信 subject（如有）
    FilterSubject: "pay.success",
})
for {
    msg, err := cons.Next()
    if err != nil { continue }
    if err := pointService.Grant(msg.Data()); err != nil {
        msg.NakWithDelay(10 * time.Second)   // 显式失败：稍后重投
        continue
    }
    msg.Ack()                                // 只有业务落地后才 ack
}
```

### 为什么它不是「小 Kafka」

| 差异点 | 后果 |
| --- | --- |
| **没有分区** | Kafka 的并行单位是 partition（同 key 落同分区、分区内有序）；JetStream 一个 stream 是**单一有序序列**，要并行就得拆成多个 stream 并用不同 filter subject | 单 stream 的吞吐上限 ≈ 单一 Raft 组的提交能力；大数据管道那种「200 分区跑满多核」的形态它不是为它设计的 |
| **复制单位是 stream（Raft 组）** | stream 数 = Raft 组数，几十上百个 stream 时元数据与选举开销开始显著 | 「一个租户一个 stream」的多租户形态在 Kafka 上是分区，在这里是 Raft 组，规模上限差一个量级 |
| **`workqueue` 保留策略** | ack 后消息即删——这是**队列**语义，不是日志语义 | 「多个下游各自回放历史」在这里不成立（除非 `limits` + 多 consumer 各自独立位点） |
| **去重是时间窗口** | `duplicate_window` 之外的重复不会被去重 | 它是「重试安全垫」，不是幂等方案；幂等永远在消费端（[eng/24](../eng/24-delivery-semantics.md)） |
| **运维模型完全不同** | 单二进制、无外部依赖、配置极少 | 换来的是「没有分区规划、没有 rebalance、没有 JVM」；代价是「没有跨集群镜像的成熟数据管道生态」（有 `gateway`/`leafnode`，但定位不同） |

**所以 NATS 的正确位置是「轻量事件总线 + 控制面 + 服务间通知 + 需要亚毫秒的场景」，而不是「替代 Kafka 的数据管道」。** 判断只用一句话：**如果你的下游需要「按位点从头重放三个月」，你要的不是 NATS；如果你需要「一万个服务之间低延迟地广播状态变化、丢了也无所谓」，Kafka 是过度设计。**

---

## 31.8 选型判定表：你要的那个语义，谁把它做进了内核

这张表是本章的工作台。**用法**：先在左列找到你的需求，看「选谁」；再读「代价」列——那一列写的都是「你必须在业务里补的那一层或必须接受的那笔账」。如果代价你付不起，就回到左列改需求，而不是硬选。

| 你要的能力 | 选谁 | 代价 / 你要补的那一层 | 选错的样子 |
| --- | --- | --- | --- |
| **事务消息**（本地事务与消息投递原子） | **RocketMQ**（half message + 回查） | 双倍 CommitLog 写；回查接口必须幂等只读；只覆盖「一条消息」不覆盖批量 | 用 Kafka/RabbitMQ 就得自建 outbox 表 + 扫表 + 幂等（事故一） |
| **任意时间延迟** | **Pulsar**（`deliverAfter`/`deliverAt`）或 **RocketMQ 5.x**（定时消息） | Pulsar：延迟中的消息吃 broker 堆，仅在 `Shared` 订阅下可靠；RocketMQ 4.x 只有 18 档（§31.4） | 用 RabbitMQ TTL+DLX 做「任意分钟」= 90 个队列或队头阻塞（§31.6） |
| **严格顺序** | 三家都行，粒度都是**局部的**：Kafka 分区内 / RocketMQ 队列内 / Pulsar `key_shared` 或 failover | 并行度 ≤ 分区/队列数；队头阻塞；转移窗口仍可能重投 → 幂等不可省 | 以为「用了 RocketMQ 就有全局顺序」——它只保证队列内 |
| **超大积压（亿级条数）+ 低内存** | **Kafka / Pulsar**（数据在盘上，内存只是缓存） | 容量按「保留期 × 速率 × 副本」算（`→ comp/28` §28.8 账四）；读会退化成读盘 | 用 RabbitMQ：积压先吃内存 → 触发水位 → **发布端被 block**（§31.6） |
| **多租户（按租户隔离配额/保留/权限）** | **Pulsar**（tenant / namespace 是一级公民） | 多一层管理模型；Raft/ledger 的运维面翻倍 | Kafka 靠 topic 前缀 + 客户端配额（软隔离）；RabbitMQ 靠 vhost（粗粒度） |
| **跨地域多活** | **Pulsar**（内置 geo-replication，异步多集群）或 Kafka + MirrorMaker/Cluster Linking | 跨地域复制是**异步**的，RPO > 0；带宽按「写入速率 × 地域数」算 | 以为配了跨地域就「强一致」——强一致只能靠同城三副本 + 多数派写 |
| **极低延迟（亚毫秒–毫秒级事件通知）** | **NATS core**（内存路由，无存储） | **at-most-once**，掉线即丢；无重放 | 用 Kafka 做万级服务间的状态广播 → 重、慢、还要规划分区 |
| **多个下游各自从头重放同一份历史** | **Kafka**（本篇唯一「答案永远是 Kafka」的一条） | 保留期 = 成本；重放 = 顺序扫 + 网络传输 | RabbitMQ 消息 ack 后即消失；NATS core 根本没有历史 |
| **每条消息要「重试 N 次后进死信」的计数** | **RabbitMQ**（`x-delivery-limit` / `x-death`）做进了内核 | 队列要选 quorum；吞吐掉到千级 msg/s | Kafka/RocketMQ 都要自己计数 + 转发 retry topic |
| **轻量、少运维、单二进制** | **NATS**（+JetStream 需要持久化时） | stream 数 = Raft 组数，规模上限比 Kafka 的分区模型低一个量级 | 用 Kafka 做「配置变更通知」= 一个集群运维成本换一条总线 |

### 反向表：你现在用 X，什么信号说明该换（或不该换）

| 现状 | 该换的信号 | 通常**不该**换的信号 |
| --- | --- | --- |
| Kafka 上自建 outbox + 扫表，且「写库与投递」的一致性 bug 反复出现 | 一致性争议已上升到业务正确性，且团队没有 CDC 基建 → RocketMQ 事务消息或 CDC（`→ comp/33`） | 只是偶尔重复 → 补幂等键即可（[eng/24](../eng/24-delivery-semantics.md)） |
| Kafka 上用延迟 topic + 定时消费者模拟延迟 | 延迟精度要求进入分钟级 SLA，且延迟消息量大 | 只有几千条/天 → 时间轮就够（`→ comp/36`） |
| RabbitMQ 上积压到百万条就 OOM/阻塞 | 积压成为常态而非异常 → 需要「盘上堆着」的形态 | 积压是偶发的下游故障 → 加 `prefetch` 治理 + 扩容消费者更便宜 |
| Pulsar 上写入 P99 随 bookie 抖动 | 抖动来自共用盘的邻居 → 先隔离 IO/换盘，不是换 MQ | 抖动来自「跨 AZ 的 ensemble」→ 改布局即可 |
| RocketMQ 单机 CommitLog 盘到水线 | 需要「存储独立扩」→ Pulsar 或 RocketMQ 分层存储（以目标版本为准） | 只是保留期太长 → 降保留期或冷热分层（`→ comp/42`） |

---

## 31.9 三本账：CommitLog 的读放大、ledger 的写放大、quorum 的内存账

本节三个算例都按「乘法链 + 假设清单 + 量级结论」写，任何一个假设都能替换后重算（脚本见本节末）。

### 账一 · RocketMQ：写放大 ≈ 2.4×，冷读放大 1.0×–4.0×（由「混写度」决定）

| 步 | 项 | 推导 | 结果（payload = 1 KB） |
| --- | --- | --- | --- |
| 1 | 一条消息在 CommitLog 里的字节 | body 1,024 + 定长头约 90 + topic 约 30 + 属性约 60 | 1,204 B → 逻辑放大 **1.18×** |
| 2 | ConsumeQueue | 定长 20 B/条 ÷ 1,024 | +1.95% |
| 3 | IndexFile（仅当消息带 key） | 20 B 条目/条 ÷ 1,024 | +1.95% |
| 4 | 副本（主 + 从，`ASYNC_REPLICATION`） | 步骤 1–3 × 2 | **2.44 GB 盘 / 1 GB payload** |
| 5 | 换成 DLedger / Controller 三副本 | × 3 | **3.66 GB 盘 / 1 GB payload** |
| 6 | **冷读**：积压超过 page cache 后（`→ comp/28` §28.4 的覆盖深度公式） | 读 1 条 = CQ 顺序 20 B + CommitLog 一次 4 KB 页读 | `(4,096 ÷ K + 20) ÷ 1,024` |
| 7 | 代入 K（同一 4 KB 页内**属于本队列**的消息条数） | K=1 / 2 / 4 | **4.02× / 2.02× / 1.02×** |

**K 是什么，决定这张账的好坏**：CommitLog 是所有 topic 共用一条顺序写，所以同一队列的消息在物理上被别的 topic 隔开。1 KB 消息时一页能装 4 条：

- **单 topic、单队列、无混写**（K=4）：一页里 4 条都是你的 → 冷读放大 ≈ **1.02×**，跟 Kafka 几乎一样；
- **一个 broker 上 20 个 topic × 8 队列，都在写**（K≈1）：一页里平均只有 1 条是你的 → 冷读放大 ≈ **4.02×**；
- 这就是「RocketMQ 适合多 topic 混跑（写占优），但**冷读（大积压回放、故障后追数据）比 Kafka 贵**」的物理来源。

> **量级结论（非承诺值）**。假设：① payload 1 KB、属性约 60 B；② 副本数 2（或 3）；③ 消息带 key（否则步骤 3 为 0，写放大降 1.9%）；④ 页大小 4 KB、无 readahead 收益、积压已超出 page cache；⑤ ConsumeQueue 与 IndexFile 也随主从复制。**换假设请按步进表重算**：消息 100 B 时写放大升到约 6.4×（`((100+180)+40)/100 × 2`：180 B 是定长头/topic/属性，40 B 是 CQ+Index），因为定长头与小索引的占比被放大——**小消息在 CommLog 形态下比在 Kafka 段形态下更贵**（`→ comp/28` §28.8 账二：Kafka 100 B 消息是 3.37×）。

### 账二 · Pulsar：写放大 ≈ 6.2×，因为 ack 必须等两份落盘

| 步 | 项 | 推导 | 结果（payload = 1 KB） |
| --- | --- | --- | --- |
| 1 | entry 大小 | body 1,024 + entry 头约 40 B | 1,064 B → 逻辑放大 **1.04×** |
| 2 | entry log 副本 | × E（`ensemble=3`） | **3.12×** |
| 3 | bookie journal | 同样写 E 份（顺序小记，组提交摊薄但物理字节不减） | **再 +3.12×** |
| 4 | 合计磁盘写入 | 步骤 2 + 3 | **6.24 GB 写 / 1 GB payload** |
| 5 | 东西向网络 | broker → E 个 bookie | **3.12 GB 入向 / 1 GB payload**（跨 AZ 部署时按跨 AZ 副本数计价） |
| 6 | 对比 Kafka（RF=3，`→ comp/28` §28.8 账二） | 3.05 GB 盘 / 1 GB payload | Pulsar 的**写入字节是 Kafka 的约 2 倍** |
| 7 | 降低 E/W/A 换带宽 | E=2 / W=2 / A=2 | 写放大降到 4.16×，但**容错从「任一台」降为「指定的那一对」**——丢一台就可能丢 ledger |

**为什么第 3 步不能省**：bookie 的 journal 是「先顺序记一笔，再异步建索引」的 WAL——这正是 `→ comp/03` 讲的崩溃恢复代价。**它保证的是「bookie 断电后能重建」，换来的就是「同一条数据写两遍」。** 而 Kafka 没有这一层，因为它把「重建」交给副本（另一台机器上的同一份数据）。

> **量级结论（非承诺值）**。假设：① payload 1 KB、entry 头约 40 B；② `E=W=3, A=2`，同机房、无跨 AZ；③ bookie `journalSyncData=true`（默认）；④ 不计 ledger 元数据与 cursor 持久化的开销（后者约每 N 条一次，量级 < 1%）；⑤ 不做 EC / 低副本布局。**第 6 步的 2 倍是量级结论**：换批大小、换盘、换 E/W/A 都在 ±30% 浮动，但「多一份 journal」这个结构因子不会消失。

### 账三 · RabbitMQ quorum：写放大 3.3×，内存账比写账更早爆

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | Raft log（每副本） | 每条消息写 3 副本的 Raft 段文件/WAL | 3× 字节 |
| 2 | 队列索引应用（每副本） | 每副本把消息应用到队列索引（元数据量级 100 B/条） | +3×「索引字节」（小，但 fsync 与 CPU 跟着来） |
| 3 | 每条 1 KB 消息的物理写 | 步骤 1 + 2：3 份消息体 + 3 份 100 B 索引元数据 | **≈ 3.3 KB / 条**（3× 是消息体，0.3× 是索引） |
| 4 | 单队列吞吐上限（串行 fsync 组提交） | `1000 / fsync_ms × batch`，fsync 1 ms、批 50 条 | **理论 5 万 msg/s**；实测常在 **3 千–1 万 msg/s**（受索引应用与 Raft 提交往返约束） |
| 5 | 积压的内存账 | 每消息元数据约 100 B 量级 | **1 亿条积压 ≈ 10 GB 内存**（消息体在盘上的比例随版本/队列类型变化，以目标版本文档为准） |
| 6 | 触发水位 | `vm_memory_high_watermark` 默认是物理内存的一个比例（0.4 量级，随版本与配置而变）→ 64 GB 机器约 25.6 GB | 超过即 **block 发布端**（连接状态 `blocked`） |
| 7 | 结论 | 步骤 5 vs 6 | **积压到 2.5 亿条量级时先撞内存墙，而不是磁盘墙** |

**这张账的关键不是「3.3× 写放大」，而是「它的第一道墙是内存」**：Kafka/Pulsar 的积压把墙撞在磁盘上（容量 × 保留期），RabbitMQ 把墙撞在内存上（元数据 × 条数），而且撞墙的表现不是「消费变慢」，是「**所有生产者一起被 block**」（事故一的 20:40–21:10）。

> **量级结论（非承诺值）**。假设：① quorum queue、`replicas=3`；② 1 KB 消息；③ Raft 每批一次 fsync，fsync 1 ms（NVMe）、批 50 条；④ 每消息元数据 100 B 量级；⑤ `vm_memory_high_watermark.relative=0.4`、64 GB 机器。**第 4 步的吞吐区间是量级结论**，依赖盘、批大小、消息大小与 Erlang 调度，不要当承诺值写进 SLA；第 6 步的水位阈值**必须按目标版本的实际默认值核对**（这个默认值在版本间变过）。

```python
# 三本账复算脚本：改任一假设直接重跑
def rmq_write_amp(payload=1024, hdr=90, topic=30, props=60, replicas=2, with_key=True):
    """RocketMQ：每 1 GiB payload 实际写多少 GiB 盘（CommitLog + CQ + Index）"""
    msg = payload + hdr + topic + props
    cq, idx = 20 / payload, (20 / payload if with_key else 0)
    return msg / payload * replicas * (1 + cq + idx)

def rmq_read_amp(payload=1024, page=4096, cq=20, k=1):
    """RocketMQ 冷读：一条消息要读一个 4 KB 页，同页 K 条属于本队列"""
    return page / k / payload + cq / payload

def pulsar_write_amp(payload=1024, entry_hdr=40, e=3, journal=True):
    """Pulsar：entry log × E + journal × E"""
    return (payload + entry_hdr) / payload * e * (2 if journal else 1)

def quorum_write_bytes(payload=1024, replicas=3, kinds=2):
    """RabbitMQ quorum：每条消息每副本 2 类写（Raft log + 索引应用）"""
    return payload * replicas * kinds

def quorum_qps(fsync_ms=1.0, batch=50):
    """quorum queue 单队列理论吞吐上限：串行 fsync 组提交"""
    return 1000 / fsync_ms * batch

print(f"RocketMQ 写放大：{rmq_write_amp():.2f} GiB/GiB（2 副本）/{rmq_write_amp(replicas=3):.2f}（3 副本）")
print(f"RocketMQ 冷读放大：K=1 {rmq_read_amp(k=1):.2f}×  K=2 {rmq_read_amp(k=2):.2f}×  K=4 {rmq_read_amp(k=4):.2f}×")
print(f"Pulsar 写放大：{pulsar_write_amp():.2f} GiB/GiB（E=3，含 journal）")
print(f"RabbitMQ quorum 每条 1 KB 消息物理写：{quorum_write_bytes()/1024:.0f} KB")
print(f"quorum 单队列理论上限：{quorum_qps():,.0f} msg/s（fsync 1 ms、批 50）")
```

---

## 31.10 架构边界：语义上做不到的事，与必须在业务层补的那一层

### 边界表（四种形态各自的「断口」）

| 系统 | 语义上做不到 | 物理原因 | 业务层必须补的那一层 | 补错的现场 |
| --- | --- | --- | --- | --- |
| **RocketMQ** | 跨「消息 + 数据库」的原子（事务消息只覆盖一条消息 + 一次回查）；任意精确延迟（4.x）；大积压下的低延迟冷读 | half message 的回查只能问「你的事务成了吗」，问不了「你的事务和别人的事务成了吗」；延迟是 18 个档位；CommitLog 共享写导致冷读随机 | 消费端幂等表（事务消息只解决生产侧）；延迟需求超出 18 档时的外部调度；冷读场景下的读放大预算 | 以为 `sendMessageInTransaction` 返回成功 = 业务成功；回查接口写了非幂等的「补偿」逻辑 |
| **Pulsar** | 「写入不等下游」；「扩了 bookie 就自动均衡」；「延迟消息无限量」 | ack 必须等 A 个 bookie fsync；旧 ledger 不迁移；延迟跟踪器在 broker 内存里 | 生产端的重试退避与幂等（P99 抖动是常态，不是异常）；bookie 的 IO 隔离；延迟消息量的容量上限 | 事故三：加 broker 加 bookie 都无效，因为要的是「隔离 + 重分布」 |
| **RabbitMQ** | 「大积压 + 低内存」；「push 模型里消费慢不牵连生产端」；「任意延迟无队头阻塞」 | push 需要内存承接 in-flight；延迟只能靠 TTL 过期；条数是内存账不是磁盘账 | `prefetch` 与消费者数量的容量规划（不是「调大就好」）；延迟档位设计；死信与重投计数（`x-delivery-limit` 已内置） | 事故一：把 `prefetch` 当吞吐旋钮 → 发布端 `blocked` |
| **NATS** | core 的「不丢」；JetStream 的「分区级并行」；「去重窗口之外的重复」 | core 无存储；单 stream 是单 Raft 组；去重是时间窗口 | 关键路径不用 core，或加 ack 层；并行靠多 stream 拆分；幂等永远在消费端 | 用 core NATS 传「不能丢」的通知，重启就少一批；以为 `duplicate_window` 是幂等 |

### 「补的那一层」到底要付多少钱（选型时最容易低估的一栏）

| 补的那一层 | 一次性成本 | 持续成本 | 它自己会怎么坏 |
| --- | --- | --- | --- |
| **outbox / 本地消息表** | 一张表 + 一段投递代码 | 每次业务写多一次同库写；扫表或 CDC 的吞吐 | 扫表多实例重复跑（事故一）；表增长后扫表变慢（事故二） |
| **幂等 / 去重表** | 一张唯一键表 + 一次查 | 存储按「保留期 × 去重键数」增长；每次消费多一次点查 | 保留期设太短 → 老重复挡不住；设太长 → 表涨爆 |
| **retry topic / 重试队列** | topic 数 × 3（retry/backoff/dlq） | 分区与文件数膨胀；顺序被打断 | 重试 topic 自己的消费者也挂 → 消息堆在 retry 里没人管 |
| **外部调度（时间轮/扫表）** | 调度器 + 幂等 | 精度 = 调度周期；扫表吃数据库 | 与业务表同库 → 扫表把主交易拖慢（事故二） |
| **对账** | 一条 T+1 的比对链路 | 只在你已经错的时候才有价值 | 发现延迟小时级到天级 → 赔付已经发生 |

> 🩸 **本节的判决**：**「换 MQ」真正在换的不是吞吐，而是「哪些语义由内核保证、哪些由你的代码保证」。** 内核保证的那部分有 bug 时你能找厂商、能升级版本；你代码保证的那部分有 bug 时，只有对账能救你。所以「哪一层进业务」这个选择，必须写进架构评审的显式条目，而不是留在实现者手里。

---

## 31.11 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到物理形态） | 怎么定位 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| RocketMQ 写入正常、消费者长时间拉不到 | CQ 构建（`ReputMessageService` 分发）落后于 CommitLog；「写入成功」≠「可消费」 | 看 CommitLog 尾偏移与 ConsumeQueue 尾偏移的差（dispatch 落后字节数，指标名以目标版本 exporter 为准）；对比 `put` 与 `get` 的速率 | 先看是不是 CommitLog 所在盘 IO 打满把分发线程拖住；限流大消息/大属性写入 | 把「分发落后」纳入告警；大促前压测时同时测「写入 TPS」与「可消费延迟」 |
| RocketMQ 事务消息「发了但没到」，回查把已回滚的单提交了 | `checkLocalTransaction` 写了非幂等或有副作用的逻辑，或返回了 `COMMIT` 兜底 | 打日志对比 half 消息的 `transactionId` 与本地事务记录 | 回查只读、只查「事务表/状态机」的确定状态，不确定就返回 `UNKNOW` 并依赖最大回查次数 | 回查接口纳入 code review 红线；生产端「未知」一律不返回 `COMMIT` |
| RocketMQ 用了「18 档延迟」但业务要精确分钟 | 4.x 是等级制；向上取整平均多等 24 分钟（§31.4） | 抽样统计「期望到期时间 vs 实际消费时间」的分布 | 升 5.x 定时消息、或换 Pulsar `deliverAfter`、或接时间轮 | 延迟需求评审时必须问「精度是档位还是任意时间」，并核对目标版本的延迟能力 |
| RocketMQ 顺序消费某队列长时间不动 | `SUSPEND_CURRENT_QUEUE_A_MOMENT` 让整条队列停一小段；毒消息会反复触发 | 看该队列的消费位点是否停滞、日志里是否连续 `SUSPEND` | 修毒消息处理的幂等与降级；把「不可重试错误」直接 ack 并落死信 | 顺序队列必须有「本地重试上限 + 死信」策略，不允许无限重试 |
| Pulsar 写入 P99 抬升，加 broker / 加 bookie 都没用 | ack 等 A 个 bookie fsync；慢 bookie 只在「包含它的 ledger」上作恶；新 bookie 不承载旧 ledger | 看 bookie 侧 journal fsync 延迟分布；看受影响 topic 的 ensemble 是否含同一台 bookie | 隔离慢 bookie 的 IO（迁走采集/备份任务）；对受影响 topic `unload` 触发 ledger 重建 | bookie 上禁止跑非存储负载；把 journal 盘独占；扩容后主动做 unload/重分布 |
| Pulsar 延迟消息把 broker 堆吃满 | 延迟跟踪器在 broker 内存里维护「未来要投递」的集合 | 看 broker 堆与延迟消息计数（延迟相关指标随版本变化，以目标版本为准） | 降延迟消息量、缩短延迟上限、改用「业务侧调度」承接大批量延迟 | 容量评审里单列一项「延迟消息条数上限」 |
| Pulsar 订阅位点被「重置」后大面积重投 | cursor 被 `reset`/`skip` 或重建订阅（`subscriptionName` 变更视作新订阅） | 对比 cursor 时间戳与消费端位点 | 从 bookie 侧确认 cursor 状态；重投路径必须幂等 | 订阅名的变更纳入变更评审（它等价于新建订阅） |
| RabbitMQ 队列积压后发布端全部 `blocked` | push 模型 + 内存高水位；积压吃的是内存元数据，不是磁盘 | `rabbitmqctl list_queues name messages messages_ready messages_unacked`；`rabbitmqctl status` 里的 alarms / memory | 扩消费者、降 `prefetch`、把队列改 lazy/quorum 让消息体落盘、必要时给该队列单独限流 | 容量公式里必须有「条数 × 元数据」这一项；告警盯 `messages_ready` 与服务端 memory 水位 |
| RabbitMQ 用 TTL 做延迟，短 TTL 消息迟迟不消费 | per-message TTL 只从队头判过期 → 队头阻塞 | 看队列头部消息的 TTL 与实际出队时间 | 改「每档一个队列 + 队列级 TTL」，或上延迟插件/外部调度 | 延迟需求先问「档位还是任意」，档位多于 5 个就别用 TTL 方案 |
| RabbitMQ 一条毒消息把队列打转 | `basicNack(requeue=true)` 把同一条消息反复投回，可能立刻又给同一个消费者 | 看同一 `message_id` 的 redelivered 标记与 `x-death` 计数 | 改成「有限次重试后 requeue=false 进死信」；quorum queue 用 `x-delivery-limit` | 消费端模板里禁止裸 `requeue=true`，必须带重试计数 |
| NATS core 用在「不能丢」的链路上，重启后少一批 | core 是内存路由，at-most-once 是定义而不是配置 | 对比发布端的 publish 计数与消费端的收包计数 | 换 JetStream 并显式 ack；或加业务侧对账 | 选型时确认「丢失是否可以接受」，写进设计文档 |
| NATS JetStream 重复投递超出预期 | `duplicate_window` 是时间窗口；超出窗口的重复不会被去重 | 看 `Nats-Msg-Id` 与消费端去重表的命中率 | 消费端加幂等键（这才是根治） | 把去重窗口当「重试安全垫」而非幂等方案 |
| 迁移后「重复消费」比原来多一个量级 | 新 MQ 的默认语义与旧的不同（如 Kafka 手动提交位点 → RabbitMQ 自动 ack / Pulsar ackTimeout 重投） | 对比迁移前后的 ack 时机代码与「ack/提交在哪一行」 | 统一成「先处理后 ack」，并补幂等键 | 迁移 checklist 里必须有「位点/ack 时机」这一行 |

---

## 31.12 观测：四家的第一指标与拐点

**每条指标都带版本与开关前提**——写错一个指标名，读者就会认为全篇没验证过。以下名称以「官方 exporter / 自带 stats 输出的常见形态」给出，**具体名称以目标版本的输出为准**（RocketMQ 用 `rocketmq-exporter`，Pulsar 用 broker/bookie 自带 Prometheus 端点，RabbitMQ 用 `rabbitmq_prometheus` 插件，NATS 用 `/varz`、`/jsz` 或官方 exporter）。

| 系统 | 指标（常见形态） | 版本 / 开关前提 | 拐点（越线就动手） | 它度量的是哪件物理事实 |
| --- | --- | --- | --- | --- |
| RocketMQ | CommitLog 目录使用率 `...commitlogdir_capacity_free / total` | `rocketmq-exporter` 部署后才有；broker 自带也有 `BrokerStats` 文本输出 | 最满单盘 > 80% 即停非关键写入；> 90% 会触发 `磁盘写满后的同类后果（RocketMQ 侧表现为写入被拒） | 顺序写文件的物理水位（**所有 topic 共用一个目录**，一个 topic 写爆就是全 broker 写爆） |
| RocketMQ | 消费积压 `...group_diff`（按 group × topic） | 同上；需 exporter 拉取 broker 的消费位点 | 持续 5 分钟单调上涨即介入；拐点更该看「上涨速率」而非绝对值 | 「逻辑队列」还差多少条没被消费 |
| RocketMQ | 分发落后（CommitLog → ConsumeQueue 的偏移差） | broker 侧 `MessageStore#getDispatchBehindBytes` 类指标，exporter 名随版本变化 | 持续 > 数十 MB 或持续增长 → CQ 构建跟不上了 | 「写入成功但消费者拉不到」的唯一直接度量 |
| Pulsar | 写入延迟分布 `pulsar_broker_publish_latency` / `pulsar_storage_write_latency_*` | broker 自带 metrics；需开 `exposeManagedLedgerMetrics` / `exposeTopicLevelMetrics`（开关随版本变化） | P99 > 50 ms 或相对基线翻倍 → 先查 bookie | ack 等 bookie 的那一段（§31.5 第 ⑤ 跳） |
| Pulsar | bookie journal fsync 延迟 | bookie 侧 stats（BookKeeper 自带 provider，指标名随版本变化） | P99 > 10 ms 即异常（正常应为亚毫秒到毫秒级） | 存算分离的「木桶最短板」 |
| Pulsar | 订阅积压 `pulsar_subscription_back_log` | 需开 topic 级 metrics | 按「条数 × 平均大小」换算成存储与回放时间，> 消费能力 × 保留期 即危险 | cursor 落后于写入的距离 |
| Pulsar | 重投速率 `...msg_rate_redeliver` | 同上 | 突增 = 消费端在失败（或 ackTimeout 太短） | `ackTimeout` / negative ack 的实际发生率 |
| RabbitMQ | `rabbitmq_queue_messages_ready` / `...messages_unacked` | `rabbitmq_prometheus` 插件（3.8+） | `messages_unacked` 接近「消费者数 × prefetch」时消费端已饱和；`messages_ready` 单调涨 = 消费能力不足 | 积压与 in-flight（push 模型的两个不同状态） |
| RabbitMQ | `rabbitmq_alarms_memory_used_watermark` / 服务端 memory 使用 | `rabbitmq_prometheus` 插件；水位由 `vm_memory_high_watermark` 决定（默认值随版本变化） | 触发 alarm 即发布端 `blocked`；**这是 RabbitMQ 唯一的「全站性」故障信号** | 内存墙（§31.9 账三第 6 步） |
| RabbitMQ | Raft 提交延迟（quorum queue） | 3.8+ 提供 quorum/Raft 相关指标（名称随版本变化，以目标版本为准） | P99 明显高于基线 → 盘慢或副本跨了故障域 | quorum queue 的真实写入代价 |
| NATS | `jetstream_consumer_num_ack_pending` / `...num_redelivered` | `nats-server` 的 `/jsz` 或官方 exporter；JetStream 需启用 | `num_ack_pending` 持续高位 = 消费跟不上；`num_redelivered` 突增 = 消费端失败或 `ack_wait` 太短 | ack 窗口里的工作量与重投 |
| NATS | `jetstream_storage_used` / stream 消息数 | 同上；`max_bytes`/`max_msgs` 决定上限 | 触到 stream 上限 → 按保留策略丢最旧（`limits`）或拒写（`workqueue` 已满） | stream 的物理用量 |
| NATS | `/varz` 的 `slow_consumers` | core NATS 自带 | > 0 且增长 = 有订阅者处理不过来被断开（**core 的背压就是断连**） | at-most-once 在压力下的表现 |

### 三条跨系统的判读纪律

1. **先分清「积压」与「失败」。** 积压（lag / backlog / messages_ready）是消费能力问题；重投（redeliver / unacked 抖动）是失败率问题。两者的修法相反：前者加并行度，后者先修下游。
2. **先找「木桶最短板」而不是平均值。** Pulsar 看最慢 bookie、RabbitMQ 看最满队列、RocketMQ 看最满的那块盘、Kafka 看最慢 follower（`→ comp/29`）。**跨系统的共同点：P99 永远由一个最差的个体决定。**
3. **告警盯「变化率」和「结构性指标」，别盯绝对值。** 「积压 10 万条」在日终是正常的，「10 分钟涨 10 万条且速率在加速」才是事故。结构性指标（分发落后、watermark alarm、`slow_consumers`、fsync P99）比业务量指标早 5–15 分钟出现。

---

## 31.13 一页速记

| 记什么 | 具体结论 |
| --- | --- |
| **五维坐标系** | 存储模型（partition log / CommitLog+CQ / ledger+cursor / queue / subject+stream）× 消费模型（pull / push）× 订阅模型 × 事务与延迟能力 × 存算是否分离 |
| **唯一真问题** | 队列的差异不在吞吐，在语义形状；每一种「内核里没有」的语义，都会变成业务里的一层代码，且那一层比换 MQ 更贵 |
| **RocketMQ** | 所有 topic 共用一条 CommitLog（写放大 ≈ 1×，2.4× 含 2 副本与 CQ/Index）+ 每队列 20 B 定长 ConsumeQueue + 按 key 的 IndexFile；**写入成功 ≠ 可消费**（CQ 异步构建）；事务消息 = half + 回查（双倍写）；4.x 延迟 18 档、5.x 任意时间 |
| **Pulsar** | broker 无状态 + bookie 存 ledger + cursor；**ack 必须等 A 个 bookie fsync**；四种子订阅（exclusive/failover/shared/key_shared）= 顺序/并行度/故障行为的四组取舍；写放大 ≈ 6.2×（entry + journal 各 3 份） |
| **RabbitMQ** | exchange 决定路由、queue 才是存储；**push 模型的唯一背压阀是 prefetch，它同时是并发度与内存水位**；quorum queue = Raft 复制日志（写放大 3.3×，单队列千级 msg/s）；延迟靠 TTL+DLX 有队头阻塞 |
| **NATS** | core = 内存路由 + at-most-once + 亚毫秒；JetStream = stream（file/memory）+ consumer ack + `Nats-Msg-Id` 去重窗口；**stream 数 = Raft 组数**，没有分区概念 |
| **三本账一条线** | RocketMQ：**读放大**（1.0×–4.0×，由同页内本队列消息数 K 决定）；Pulsar：**写放大**（6.2×，多出的那份是 journal）；RabbitMQ：**内存账**（1 亿条 ≈ 10 GB 元数据，先撞内存墙而不是磁盘墙） |
| **必须补的那一层** | 幂等/去重表（所有 MQ 都要）、outbox（要事务消息时）、retry topic（要重试计数时）、外部调度（延迟超出内核能力时）、对账（前面全部失效时的最后一道） |
| **push vs pull 的故障形态** | pull：消费慢 → lag 涨、盘上堆着、broker 内存不动；push：消费慢 → 内存涨 → 发布端 `blocked`。**同一调参动作在两种模型里后果相反** |
| **存算分离的账** | 买到独立扩容，付出「ack 等下游 + 扩容后要重分布」；**加 broker 不减 bookie 慢盘，加 bookie 不迁移旧 ledger** |
| **第一指标** | RocketMQ：CommitLog 最满盘 + 分发落后；Pulsar：bookie journal fsync P99；RabbitMQ：memory watermark alarm + `messages_unacked`；NATS：`num_ack_pending` / `slow_consumers` |

**如果只记五句：**

1. **吞吐是最不重要的选型维度。** 五种系统的稳态吞吐都能到万级–百万级 msg/s，真正卡住你的是「你要的语义它有没有」。
2. **每种缺失的语义都会变成业务里的一层代码。** 事务消息缺 → outbox 表；延迟档位不够 → 扫表；重试计数缺 → retry topic + 计数器。这一层的运维成本通常高于换 MQ。
3. **push 的背压阀是 prefetch，pull 的背压阀是磁盘。** 前者调大是内存事故，后者调大只是拉更多数据。
4. **存算分离买「独立扩容」，卖「写路径变长」。** Pulsar 的 P99 由最慢的 bookie 决定，不由 broker 决定；扩容后必须做重分布。
5. **「多个下游各自从头重放同一份历史」仍是 Kafka 的独占地盘**；RabbitMQ 的消息 ack 后就没了，NATS core 根本没有历史，RocketMQ 的多 group 回放共享度不如 Kafka。

---

## 31.14 自检题（5 题）

**Q1.** 一个 broker 上跑 20 个 topic × 8 队列，消息平均 1 KB，都在持续写。有人把 RocketMQ 的 CommitLog 当成「顺序写所以读也快」的理由，说「冷读放大不超过 1.1×」。错在哪？给出正确区间与它依赖的那个变量。

<details><summary>答案</summary>

错在把**写路径**的顺序性当成了**读路径**的顺序性。

CommitLog 是所有 topic 共用一条顺序写，所以同一个队列的消息在物理上被别的 topic 隔开。1 KB 消息、4 KB 页 ⇒ 一页最多装 4 条。**读放大 = `(4096 ÷ K + 20) ÷ 1024`**，其中 K = 「同一页里属于本队列的消息条数」：

- 极热单队列、几乎不混写（K=4）：`(1024+20)/1024 ≈ 1.02×`；
- 20 topic × 8 队列混跑（K≈1）：`(4096+20)/1024 ≈ 4.02×`。

所以正确区间是 **1.0×–4.0×**，由「混写度」（活跃 topic/队列数与写入速率分布）决定，不是常量 1.1×。

补充两点：① 这个放大只在**冷读**（积压超过 page cache 覆盖深度，见 `→ comp/28` §28.4）时全额出现；热读命中 page cache 时几乎免费。② 写路径的放大是另一本账：约 1.18×（消息头/属性）× 副本数 ×（1 + CQ 1.95% + Index 1.95%），2 副本约 **2.44×**。

量级结论，非承诺值；假设 payload 1 KB、页 4 KB、无 readahead 收益。
</details>

**Q2.** 业务需求：① 下单后必须保证「写库成功」与「消息投递成功」原子；② 每单需要在 1–120 分钟内按商家配置**任意整数分钟**后触发一次检查；③ 峰值 5 万条/秒。给出选型与「你必须自己补的那一层」，并说明为什么另外两个组合不行。

<details><summary>答案</summary>

**选型**：RocketMQ 5.x（事务消息 + 定时消息）。

- ① 事务消息是唯一把这半段做进内核的（half message + 回查），代价是双倍 CommitLog 写 + 回查接口必须幂等只读。
- ② 5.x 的定时消息支持按 `deliverTimeMs` 指定任意时间点；**4.x 不行**（18 档，1–120 分钟里可精确命中的只有 13 个点，向上取整平均多等 24 分钟，见 §31.4）。
- ③ 5 万条/秒：按 1 KB 消息、单 broker 顺序写能吃满盘带宽来算，需要 3–6 台 broker + 足够队列数；此时要注意 CQ 构建（分发落后）与顺序消息的并行度上限（= 队列数）。

**必须自己补的那一层**：**消费端幂等 + 死信**。事务消息只解决生产侧原子性，回查也只回答「本地事务成了吗」；消费者重投、检查任务重复触发都必须靠幂等键兜住（[eng/24](../eng/24-delivery-semantics.md)）。此外「检查任务的业务重试」仍需要 retry 计数。

**另外两个组合为什么不行**：

- **Pulsar**：延迟任意 ✅、吞吐 ✅，但**没有事务消息**（Pulsar 事务覆盖的是「多条消息的 produce/ack」，覆盖不到你的 MySQL，等价于仍需 outbox）。
- **Kafka + 自建 outbox + 扫表**：这是事故一与事故二的组合——你会在业务里造出「扫表延迟 + 多实例重复」这两件事，而它们正是内核本该负责的部分。

量级结论，非承诺值。
</details>

**Q3.** RabbitMQ quorum queue 积压从 20 万条涨到 8,000 万条，`messages_unacked` 也涨，随后所有生产者连接变 `blocked`，下单接口 P99 从 180 ms 到 4 s。问：① 为什么会牵连生产者？② 按代价排序给三个修法；③ 用本篇的两本账说明「加盘」为什么没用。

<details><summary>答案</summary>

**① 牵连生产者的机制**：push 模型 + 内存高水位。积压吃的是**内存里的元数据与 in-flight 状态**（1 亿条 ≈ 10 GB 量级），当服务端 memory 使用越过 `vm_memory_high_watermark`（默认是物理内存的一个比例，随版本变化），RabbitMQ 会对**发布连接**进入 `blocked` 状态——它不是「队列满」，是「整台 broker 的内存满了」。

**② 三个修法（按代价从低到高）**：

1. **扩消费者 + 修下游**：先看 `messages_unacked` 是否接近「消费者数 × prefetch」——是则消费端已饱和。加消费者/加实例，并把 `prefetch` 调到「处理耗时 × 单消费者吞吐」能填满的值（不是越大越好：调大 = 更多 in-flight = 更多内存）。
2. **把积压搬走**：给该队列单独做「溢出队列」（超阈值转发到另一个 broker 的队列），或临时把消费转发到一个降级消费者（只落库不调外部接口），把内存峰值削掉。
3. **换形态**：如果「积压 8,000 万条」是业务常态（而不是故障），说明需要的正是「盘上堆着」的 pull 形态 —— Kafka/Pulsar。这是形态不匹配，不是调参问题。

**③ 为什么加盘没用**：RabbitMQ 的账是**内存账**不是**磁盘账**。§31.9 账三：每消息元数据约 100 B → 8,000 万条 ≈ 8 GB；撞的是内存水位线，不是 `disk_free_limit`。所以加盘、加 IOPS 都不改变「发布端被 block」这件事。

量级结论，非承诺值；假设 1 KB 消息、quorum 3 副本、每消息 100 B 元数据、水位 0.4。
</details>

**Q4.** Pulsar 集群：8 broker + 12 bookie，`E=W=3, A=2`。某台 bookie 盘慢，导致约 1/4 的 topic 写入 P99 从 8 ms 到 780 ms。运维先加了 2 台 broker，又加了 2 台 bookie，都没效果。为什么？正确的操作顺序是什么？

<details><summary>答案</summary>

**为什么两个动作都无效：**

- **加 broker**：broker 无状态，只负责协议与派发；写延迟的等待对象是 bookie 的 journal fsync（§31.5 第 ⑤ 跳）。瓶颈不在 broker，加它等于给「不堵的那一段」加宽。
- **加 bookie**：新 bookie 只承载**新产生的 ledger**，已有 ledger 的 ensemble 不会自动迁移（要 `unload` topic / 触发 ledger 重建 / 跑 auto-recovery）。慢 bookie 仍在旧 ledger 的 ensemble 里，仍在 1/4 的 topic 的写路径上。

**正确顺序：**

1. **止血（分钟级）**：找到慢 bookie，隔离它的 IO —— 停掉盘上的采集/备份/压缩任务，必要时把它上的 journal/ledger 盘独占出来；`journalSyncData` 与 journal 盘分离是部署期的纪律。
2. **止损业务侧**：生产端超时不能设得比 publish P99 更紧（事故三里 500 ms 的超时直接把抖动放大成重试风暴）；补幂等键（重复投递 18.4 万条就是这一步缺失的代价）。
3. **重分布（小时级）**：对受影响 topic `unload` 触发 ledger 重建；或让 auto-recovery 接管；确认新 ensemble 不再含慢 bookie。
4. **容量与布局复核**：检查 bookie 是否跨故障域、journal 是否独占盘、每台 bookie 的 ledger 分布是否被「热点 topic」压住。

**一句话**：**存算分离把「扩容」从一个动作变成三个动作——扩计算、扩存储、重分布。前两个是加机器，第三个才是解决慢盘的那一个。**
</details>

**Q5.** 有人说「NATS JetStream 就是小 Kafka，都是持久化 + consumer + ack」。用三个形态差异说明这句话错在哪，并给出一个「该用 NATS 而不该用 Kafka」的具体场景与一个反向场景。

<details><summary>答案</summary>

**三个形态差异：**

1. **没有分区，只有 stream（单一有序序列）。** Kafka 的并行单位是 partition（同 key 落同分区、分区内有序）；JetStream 一个 stream 就是一个 Raft 组，要并行必须拆多个 stream + filter subject。后果：**单 stream 吞吐上限 ≈ 一个 Raft 组的提交能力**，而 Kafka 靠 200 个分区摊到多核。
2. **复制单位是 stream，stream 数 = Raft 组数。** 几十上百个 stream 时选举与元数据开销显著；「一租户一 stream」的多租户形态规模上限比 Kafka 的「一租户若干分区」低一个量级。
3. **保留策略里有 `workqueue`（ack 后即删）。** 这是**队列**语义，不是日志语义。「多个下游各自从头重放历史」在这里要靠 `limits` + 多 consumer 独立位点，且没有 Kafka 那种「一份物理字节被多 group 共享」的成熟形态。另外去重是 `duplicate_window` **时间窗口**，它是重试安全垫，不是幂等方案。

**该用 NATS 的场景**：8,000 个微服务实例之间广播「配置/路由/健康状态变更」，延迟要亚毫秒级、丢失可接受（下一条变更会覆盖）——Kafka 在这里是过度设计（要规划分区、跑 KRaft、维护 JVM）。
**该用 Kafka 的场景**：一份订单事件要被风控、报表、搜索索引、审计四个下游**各自按自己的节奏从头回放 7 天历史**——JetStream 的 `workqueue` 语义会让消息 ack 后就消失，而 Kafka 的日志形态天然支持多 group 独立位点。

量级结论，非承诺值；NATS 的具体默认值（`duplicate_window`、`max_deliver`、`replicas`）以目标版本文档为准。
</details>

---

## 31.15 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| Kafka 的日志形态：segment、稀疏索引、零拷贝 —— 本篇「Kafka 侧」的一切前提都在这里，本文不重写 | [28 Kafka 的日志](28-kafka-log-segments.md) §28.3、§28.4、§28.9 |
| 复制与一致性：ISR、HW、LEO 与 acks —— 与 §31.4 的「刷盘 × 复制」四组合表同源 | [29 复制与一致性](29-kafka-replication-isr.md) §29.5、§29.9 |
| 消费组与 rebalance：位点提交时机就是语义；本篇 §31.5 的四种子订阅是它的另一种拆法 | `→ comp/30` |
| 任务队列语义：可见性超时、死信、「被取走但没做完」这个状态（与 RabbitMQ/`ackTimeout` 同源） | `→ comp/32` |
| 延迟与定时：时间轮、延迟队列与事务消息回查 —— §31.4 的延迟等级、§31.6 的 TTL+DLX 都收口到这里 | `→ comp/36` |
| 投递语义与重复消费：至少一次 + 幂等 = 效果恰好一次，本篇所有「补的那一层」都归它 | [eng/24 投递语义](../eng/24-delivery-semantics.md) |
| 消息可靠性八环节：生产端与消费端的落地 checklist，尤其「发送成功但没落盘」 | [eng/26 消息可靠性](../eng/26-message-reliability.md) |
| 异步解耦的本质与选型：该不该上 MQ、七条「不该用」判据（先回答它，再回答本篇的「用哪个」） | [eng/23 异步消息基础](../eng/23-async-messaging-basics.md) |
| 顺序、堆积与重平衡：分区键 = 保序键 = 幂等键 | [eng/25 顺序与堆积](../eng/25-ordering-and-lag.md) |
| 消息丢了怎么查：与 §31.11 的踩坑清单配套的排查顺序 | [eng/27 消息故障排查](../eng/27-message-troubleshooting.md) |
| 消息存储与分发的架构选型：「只追加日志 vs 可编辑状态」与量级估算 | [architect/15 消息存储](../architect/15-message-store.md) |
| WAL 与崩溃恢复：理解 bookie 的 journal 为什么必须「同一条数据写两遍」（§31.9 账二第 3 步） | [03 WAL、缓冲池与崩溃恢复](03-wal-bufferpool-crash-recovery.md) |
| 存储引擎三形态：CommitLog 的「共享顺序写 + 派生索引」与 LSM 的「只追加 + compaction」是两种不同的账 | [02 存储引擎三形态](02-storage-engine-three-shapes.md) §02.4、§02.6 |
| 对象存储与云盘：分层存储（Kafka tiered / Pulsar offload）的底座与计价方式 | `→ comp/42` |
| CDC：把「写库与投递一致性」从 outbox 换成 binlog 抽取的另一条路 | `→ comp/33` |
