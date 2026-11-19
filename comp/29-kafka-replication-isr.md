# 29. 复制与一致性：ISR、HW、LEO 与 acks

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 消息、流与定时 ｜ **编号**：29 / 53
> **本篇定位**：全仓唯一一处把「一条消息什么时候算被 Kafka 提交」拆到偏移量级的地方。日志长什么样（segment、稀疏索引、零拷贝）归 `→ comp/28`，本篇一个字不重写；投递语义与幂等消费归 [eng/24 投递语义](../eng/24-delivery-semantics.md)，生产端/消费端的可靠性清单归 [eng/26 消息可靠性](../eng/26-message-reliability.md)，本篇只回答一个问题：**`acks` / `min.insync.replicas` / `replica.lag.time.max.ms` / `unclean.leader.election.enable` 这四个参数如何联合决定「丢不丢」，以及它们各自的失效窗口在哪**。读完它，`acks=all` 会从一个"安全等级"变成一个带条件的断言，HW 会从"最新位点"变成一个"消费者看得见的边界"，unclean 选举会从一个配置项变成一份必须业务签字的预案。
> **读法**：救火直接读 §29.4 的「这中间消费者能读到什么」判定表、§29.7 的故障组合判定表与 §29.10 的坑清单；做架构决策读 §29.5 的 `acks × min.insync` 表与 §29.9 的边界表；被追问"我们配了 acks=all 为什么还会丢"读 §29.5 与 §29.6，这两节把"假安全感"拆成可复算的算术；要出容量方案直接抄 §29.8 的五个算例（全部带推导式与假设清单）。

---

## 29.1 引子：三起「消息确实发出去了，但消费者永远看不到 / 切主后少了 40 分钟」

Kafka 的复制故障有一个共同形状：**producer 侧是绿的**。`send()` 返回了、回调没有异常、监控上 `records-sent` 在涨——因为 Kafka 的"成功"只承诺一件很窄的事，而所有人都在读它没承诺的那部分。

### 事故一：`acks=1` + 一次「止血」调参，风控有 6 分钟看不到任何新订单（2026-02-17）

某支付风控链路，Topic `risk.order.events`，48 分区 3 副本。producer 用 `acks=1`，理由是团队原话："3 副本已经很安全了，等 follower 太慢。" 三周前有人把 broker 的 `replica.lag.time.max.ms` 从默认量级调到 **600,000 ms**，因为 ISR 抖动告警太吵——这是本篇所有故事里最贵的一行配置。

| 时刻 | 事件 | leader LEO − HW（已写未可见） | ISR | producer 侧 |
| --- | --- | --- | --- | --- |
| 02-17 10:58 | 稳态：HW 与 LEO 差 0.4 s 写入量 | ≈ 1.1 MB | 3 | P99 4 ms |
| 11:02:10 | 某台 broker 的日志采集 agent 升级后开始打包历史日志，该盘 util 18% → 99% | — | 3 | — |
| 11:02 起 | 该 broker 上的 follower 变成"能拉到但很慢"：拉取一直在做，只是追不上 | 爬升 | **3（不减）** | P99 4 ms，全绿 |
| 11:04 | 落后累积到 6 分钟写入量 | **≈ 4,000 万字节 / 24 万条** | 3 | 全绿 |
| 11:04–11:08 | 消费者只能读到 HW；HW 被最慢的 follower 钉住 | 冻结在 11:02 的位置 | 3 | 全绿 |
| 11:09 | 采集任务结束，follower 追平，HW 一次前跳到 LEO | 归零 | 3 | 全绿 |
| 11:15 | 风控策略同学发现"今天的拦截量曲线平了 6 分钟" | — | — | — |

**这 6 分钟里，`UnderReplicatedPartitions` 是 0，`IsrShrinksPerSec` 是 0，producer 错误率是 0。** 因为 ISR 的判据是"这个 follower 多久没追上 leader 的 LEO"，而阈值被调到 600 s —— 落后 6 分钟的副本在 Kafka 眼里依然是"同步中的"。

代价：6 分钟 × 平均 2,800 单/分钟 = 16,800 单没有做实时风控，其中事后规则命中 8,400 笔，拒付与赔付 **41 万元**，对账 3 天。根因不是那块盘，是那个 600,000。

### 事故二：两台 broker 掉电，开了 unclean 选举，1.2 GB 已 ack 消息永久消失（2026-01-09）

某 IoT 接入域，Topic `device.telemetry`，96 分区 3 副本。这次参数是配对正确的：`acks=all` + `min.insync.replicas=2` + `unclean.leader.election.enable=false`。**唯一没算的是故障域**：3 个副本的分布是"机柜 A 两台 + 机柜 B 一台"，而机柜 A 的两台共用同一路 UPS。

| 时刻 | 事件 | ISR | 写入 | 后果 |
| --- | --- | --- | --- | --- |
| 01:40 | 机柜间链路抖动，机柜 B 的 follower 掉出 ISR 并开始落后 | 2 | 正常 | 无人注意（告警没配） |
| 02:14:03 | UPS 切换失败，机柜 A 两台同时掉电 | **1**（只剩机柜 B） | — | — |
| 02:14:30 | `ISR=1 < min.insync.replicas=2`，96 个分区里 64 个的写入被**全部拒绝** | 1 | 100% 失败 | 这是**正确行为**：宁可拒写也不静默降级 |
| 02:16 | 业务方要求"先恢复写入"；运维把 `unclean.leader.election.enable` 改 true 并滚动重启 controller | 1 | — | 决策在 90 秒内做出，无业务方签字 |
| 02:19 | 机柜 B 那台（落后 **300 万条 ≈ 1.2 GB**）被提为 leader，它的 LEO 成为新 HW | 1 | 恢复 | 分叉点定在 01:40 |
| 03:40 | 机柜 A 恢复，两台 broker 回来，按新 leader 的 LEO 截断自己日志尾部 | 3 | 正常 | 那 300 万条被物理删除 |
| 09:20 | 计费对账发现差额 | 3 | 正常 | — |

代价：300 万条遥测中 12% 是计费事件（36 万条），对账差 **26 万元**，人工补数 720 人时；设备影子状态回退到 01:40，40 分钟内的"设备已激活"全部无效。更贵的是**无法用位点重放找回**：那 300 万条的 offset 已经被新消息复用（同 offset 换内容），而消费组位点早就提交到 01:40 之后（§29.7 讲清为什么）。

### 事故三：全链路 `acks=all`，但 `min.insync.replicas` 用的是默认值（2025-11-24）

某订单域，Topic `order.state`，3 副本，producer `acks=all`，评审时被夸"很规范"。半年无事。问题只有一个字：`min.insync.replicas` 没显式设过，用的是 broker 默认值（**1**）。

| 时刻 | 事件 | ISR | 写入 | 消费者 |
| --- | --- | --- | --- | --- |
| 11-21 起 | 机房迁移：`kafka-reassign-partitions.sh` 逐分区把副本搬到新机柜，限速 20 MB/s | 每分区在新副本追平前只剩 **1–2** 个 | 正常 | 正常 |
| 11-23 | 迁移进行到第 3 天，仍有 210 个分区处于 ISR=1 | **1** | **正常**（`min.insync=1` 允许） | 正常 |
| 11-24 14:02 | 某分区唯一 in-ISR 副本所在 broker 的 RAID 卡缓存电池失效，随后固件 bug 让写谎报成功 | 1 | 正常 | 正常 |
| 14:05 | 该盘一段数据不可读，broker 抛 `KafkaStorageException` 退出；同一分区另外两个副本还没追平 | **0** | 失败 | — |
| 14:30 | 确认该分区所有副本均不完整，只能从上游 MySQL binlog 重放 | — | — | 停 |
| 11-25 04:30 | 重放 3 天窗口 4.2 亿条事件完成（14 小时），期间下游报表/搜索索引停在 3 天前 | — | — | 恢复 |

代价：该 topic `retention.ms` 为了省盘只设 **3 天**，等于这 3 天的整份历史（含所有派生视图的重建依据）一次性归零；重放 14 小时，下游全量不可用；SLA 赔付与人力 ≈ **63 万元**。

> 🩸 **三起事故的共同点**：没有一处代码写错，没有一次报错。① 是"阈值被调大，于是落后变成合法状态"；② 是"参数对但故障域没算，于是可用性和数据被临时交换了一下"；③ 是"`acks=all` 缺了另一半，于是单副本语义穿了 3 副本的衣服"。三件事都发生在**配置层**，所以都不在 code review 里。

---

## 29.2 一句话定位与唯一真问题

**定位**：Kafka 的复制是「**单个 leader 串行写日志 + 一组 follower 主动拉取 + 一个最小值当提交点**」。它不是共识协议（没有投票、没有 term、多数派不作数），它是一个**主从复制 + 一组用来给主从关系上保险的参数**。理解这一句，后面全部是推论。

### 唯一真问题

> **「不丢」不是某个参数给的，是 `acks` / `min.insync.replicas` / `replica.lag.time.max.ms` / `unclean.leader.election.enable` 这组参数的**联合体**——其中任何一个被放松，整条链就降级；而 unclean 选举本质上是「拿丢数据换可用」，这个选择必须写进业务预案，不能留给运维现场决定。**

三个直接推论，全篇反复用：

1. **`acks=all` 的 "all" 指「当次写入时的 ISR」，不是你的 `replication.factor`。** 所以 `acks=all` 单独存在时，它的强度是**浮动的**，跟着 ISR 一起缩。
2. **能钉住这个浮动下限的只有 `min.insync.replicas`。** 它不增加副本，它只把"ISR 缩到多小就该拒绝写"变成一条硬线。
3. **而 ISR 由 `replica.lag.time.max.ms` 定义，unclean 由 `unclean.leader.election.enable` 定义。** 于是四个参数分别站在四个位置：客户端（acks）、topic（min.insync）、broker（lag.time.max、unclean）。**四个位置、三个层级，没有任何一个"总开关"能让它们一致** —— 这就是它最容易被配错的原因。

### 解剖卡八问落点（本篇答案）

| 问 | 落在哪节 | 一句话答案 |
| --- | --- | --- |
| ① 承诺什么 | §29.5、§29.9 | 承诺"被 ack 的消息至少存在于当时 ISR 的每一份 page cache 里"，**不承诺刷盘、不承诺未来 ISR、不承诺算子执行一次** |
| ② 数据长什么样 | §29.3（细节 `→ comp/28`） | 每个副本一份只追加日志 + 四个位点：`LogStartOffset ≤ LSO ≤ HW ≤ LEO` |
| ③ 一次读写走哪条路 | §29.4 | leader append（页缓存，亚毫秒–3 ms）→ follower **主动拉**（≤ 一次拉取周期）→ 下一次拉取把 HW 推上去（再一个周期） |
| ④ 并发与一致 | §29.4–§29.7 | 主从复制不是共识：提交位点 = ISR 的 LEO 最小值；失效窗口 = `replica.lag.time.max.ms`（落后多久算掉队）与 `delivery.timeout.ms`（重试多久算失败） |
| ⑤ 扩展维度 | §29.8、§29.9 | **四选一里的「副本」**：副本买可用性与读吞吐，**不买写吞吐**——写并行度只由分区数决定（`→ comp/30`）；副本也不买"跨故障域"，那是副本**布局**的事 |
| ⑥ 系统级失效 | §29.7、§29.9 | 三类：**集群不可用**（ISR < min.insync 时全分区拒写）、**数据不一致**（unclean 后 offset 被复用，位点语义分叉）、**成本失控**（跨 AZ 复制流量与副本重建带宽） |
| ⑦ 看哪几个指标 | §29.11 | `UnderReplicatedPartitions`（第一指标）、ISR 收缩/扩张速率、每分区 `LEO − HW`、follower `MaxLag`、`FailedProduceRequestsPerSec` |
| ⑧ 换掉它的信号 | §29.9 | 需要跨城强一致、需要"提交即多数派"、需要单分区超百万级吞吐 → 那是共识型日志（KRaft/Raft 类、Pulsar/BookKeeper 形态）的地盘，`→ comp/31` |

---

## 29.3 四个位点：LEO、HW、LSO 与 LogStartOffset

日志的**物理形态**（segment 文件、`.index`/`.timeindex` 稀疏索引、零拷贝发送）归 `→ comp/28`，本节只做一件事：把这四个偏移量摆到正确的位置上，因为本篇后面每一句话都要用到它们。

| 位点 | 属于谁 | 定义 | 谁在推进它 | 消费者能读到吗 | 它错了会怎样 |
| --- | --- | --- | --- | --- | --- |
| **`LogStartOffset`** | 每个副本各自 | 日志中第一条**仍可读**的 offset | 保留策略删除（`retention.ms`/`retention.bytes`）、`deleteRecords()` 显式删除、leader epoch 截断 | 是（下限） | 消费者请求低于它 → `OffsetOutOfRange`（`auto.offset.reset` 生效，事故级的"数据跳空"多半从这里来） |
| **`LEO`**（Log End Offset） | 每个副本各自，**一对一** | 本副本日志末尾的**下一个待写** offset | 本副本自己的 append | **否** | 它是"写进去了多少"的唯一真相；HW 由它算出 |
| **`HW`**（High Watermark） | 分区级，权威只在 leader | `min(ISR 中所有副本的 LEO)` | leader 在处理 follower 拉取请求时推进 | **是**（上限，`read_uncommitted`） | 它就是"消费者看得见的边界"；落后 = 消息写了但看不见 |
| **`LSO`**（Last Stable Offset） | 分区级 | 第一个**未完成事务**的起始 offset；若无未完成事务则 `LSO = HW` | 事务 commit/abort，或 `transaction.max.timeout.ms` 兜底超时 | 仅 `isolation.level=read_committed` 时是上限 | 一个开着不放的事务能把 LSO 钉住到事务超时（默认量级 15 分钟），期间 `read_committed` 消费者一条也读不到 |

**关系式（背下来）**：

```
LogStartOffset  ≤  LSO  ≤  HW  ≤  LEO(leader)
                 LSO = HW        （无未完成事务时）
     HW = min over r ∈ ISR of LEO(r)      ← 关键：取最小值，不是取 leader
```

**图 1 · 三副本的 LEO 与 HW：HW 卡在最慢的那一个身上**

```text
                         一次写入的方向 ──────────────────────────────►
offset →      1,000,000                  1,203,000        1,207,000
                 │                          │                │
B1 (leader)  ────┼──────── 可读区间 ────────┼── 已写未可见 ──┼────►
                 │                          │                │
            LogStartOffset              HW = 1,203,000   LEO = 1,207,000
            （retention 推进到这里）    = min(ISR 的 LEO)   = leader 本地末尾
                 │                          │
B2 (follower) ───┼──────────────────────────┼──────┐
                 │                          │      │ LEO = 1,205,000（比 leader 慢 2,000 条）
                 │                          │
B3 (follower) ───┼──────────────────────────┤      │
                 │                          │      │ LEO = 1,203,000  ★ 最慢，它钉住 HW
                 ▼                          ▼
        消费者只能读到 HW 之前          4,000 条消息
        ⇒ "写进去了但看不见"的物理原因    producer 侧可能已显示成功
```

**这张图要读出的三件事：**

1. **HW 是"最小值"，所以一条消息可被消费的时刻，由最慢的那个 ISR 成员决定**——不是由 leader、也不是由平均数决定。
2. **`LEO − HW` 就是"已写未可见"的条数**。这个差值是一个可以直接拿来当告警的物理量（§29.11），而 `LEO` 本身不能当"最新位点"给业务看，它只是"写进去了"。
3. **LSO 与 HW 之间还隔着一层事务**：即使 HW 已经推进，只要有一个事务开着不提交，`read_committed` 消费者的可见上限停在 LSO —— 这是"producer 全绿、消费者零数据"的第二种物理原因，而且**不需要任何副本出问题**。

---

## 29.4 一次写的时序：follower 主动拉，HW 卡在最慢的那一个

### 关键事实：Kafka 的复制是 follower pull，不是 leader push

leader 不主动推数据给 follower。每个 follower 起一组 `ReplicaFetcherThread`（数量由 `num.replica.fetchers` 决定，默认量级是每 broker 1 个），**带着自己的 `fetchOffset`（即自己的 LEO）去问 leader 要下一段**。这个方向选择决定了三件事：

- **好处**：follower 自己决定节奏，天然形成背压；一个 follower 慢不会拖慢 leader 的写线程（leader 只是把它的 LEO 记下来）；批量合并天然成立。
- **代价一**：**HW 的推进依赖 follower 的拉取频率**，而不是 leader 的写频率。这就是"可见延迟"的分母。
- **代价二**：follower 的拉取要经过它自己的线程池和磁盘，一台 broker 上若有几千个 leader 分区共享一个 fetcher，实际拉取周期会远大于配置值。

### 图 2 · 一次写从 producer 到「可被消费」的完整时序

```text
                     leader (B1)                        follower (B2)
                         │                                   │
t0  producer ──ProduceRequest(acks=all)──►                    │
t1  append 到本地日志尾部（写 page cache，非 fsync）           │
    LEO 1,206,999 → 1,207,000        HW 仍 = 1,203,000         │
    ★ 消费者这一刻读不到这条（offset ≥ HW）                     │
                         │                                   │
t2  ◄──────────────── FetchRequest(fetchOffset=1,203,000) ────┤  ← follower 主动拉
t3  ──── 回数据（sendfile 零拷贝，不经过用户态）──────────────►│
                         被拉走的消息还在"路上"，HW 不动         │ append → LEO 1,207,000
                         │                                   │
t4  ◄──────── FetchRequest(fetchOffset=1,207,000) ────────────┤  ← 下一次拉取带回"我到了"
t5  leader 此刻才把 min(ISR LEO) 从 1,203,000 推到 1,207,000  │
    ★ HW 前移 ⇒ 消费者从这一刻起可读                        │
t6  ──── ProduceResponse(acks=all) ──► producer 显示"发送成功" │
                         │                                   │
    消费者 poll：可见位点 ≤ HW（read_committed 时 ≤ LSO）      │
```

### 「这中间消费者能读到什么」判定表

| 时刻 | leader LEO | HW | 消费者能读到这条吗 | producer 状态 | 备注 |
| --- | --- | --- | --- | --- | --- |
| t1 append 后 | +1 | 不动 | **不能** | 等待中 | 已"写进去了"，但还没被任何 follower 确认 |
| t3 follower 拉走并 append | +1 | 仍不动 | **不能** | 等待中 | HW 要等 follower **下一次**拉取把新 LEO 带回来才前移 |
| t5 HW 前移 | +1 | +1 | **能** | 即将收到 ack | 这一跳才是"提交" |
| t6 ack 到达 | +1 | +1 | 能 | 成功 | ack 与可见之间最多差一个拉取周期 |
| 若 follower 在 t3 之后挂掉（ISR 未收缩） | +1 | 冻结 | **不能**（停留在原 HW） | **显示成功** | 这是事故一 / 事故三的形态 |
| 若该批属于一个未提交事务 | +1 | +1 | **仍不能**（`read_committed`） | 成功 | 可见上限是 LSO，不是 HW |
| 若 `acks=1` | +1 | 由最慢 ISR 决定 | 不能（直到最慢者追上） | **很早就显示成功** | leader 本地写完就回，不需要任何 follower |

**由此得到本篇最硬的一条定理：**

> **在 `acks=all` 且 ISR 不变的条件下，"producer 收到 ack"与"HW 前移"最多相差一个 follower 拉取周期。**
> 所以「producer 早就显示成功，消费者几分钟看不到」这件事，**不可能**是"某个 follower 落后"直接造成的——落后的 follower 要么在一个拉取周期内就被 ack 等到了，要么超过 `replica.lag.time.max.ms` 就被踢出 ISR、HW 立刻前跳。
> 它只可能来自三条路：**① `acks` 实际没有对等 ISR（`acks=1`，或 `acks=all` 缺 `min.insync` 降级，§29.5）；② 适用的可见性上限不是 HW 而是 LSO（未提交事务）；③ `replica.lag.time.max.ms` 被调大，让"落后"变成合法状态（事故一）。**

**推论（很反直觉，但很有用）**：ISR 收缩会让 **HW 前跳**、让 `acks=all` 的延迟**下降**。所以"ISR 抖了"在延迟曲线上看起来像"变好了"——这是它最容易被误判成"网络小问题"的原因（§29.10 坑三）。

---

## 29.5 acks 与 min.insync.replicas：两个参数是一个联合体

### 三档 `acks` 到底在等什么

| 档位 | 等谁 | 等到什么时候返回 | "发送成功"意味着什么 | 典型用途 |
| --- | --- | --- | --- | --- |
| `acks=0` | 谁都不等 | 请求进了 socket 缓冲区就算完 | **什么都不意味着**。broker 有没有收到、有没有写进去，producer 一概不知；`retries` 在这档基本无意义（丢了也发现不了） | 可丢的埋点、指标流；延迟最低（无等待） |
| `acks=1` | leader | leader 把批量 append 进**本地 page cache** 就返回 | 消息在 leader 的内存页里。leader 进程崩溃不丢（同机 OS 还在），**机器掉电或 leader 被切走就丢未同步部分** | 高吞吐、可容忍少量丢失的日志 |
| `acks=all`（`-1`） | 当次写入时的**整个 ISR** | ISR 中所有副本都 append（各自 page cache）成功 | 消息存在于**当时 ISR 的每一份**副本上；若 `ISR < min.insync.replicas`，leader 在 append 前直接拒绝 | 生产默认；账务、订单、状态机 |

**注意 `acks=all` 不在"等谁"这一列里写"等 3 份"**，它写的是"等 ISR"。这是全部假安全感的来源：`|ISR|` 是一个会变的数，而 `replication.factor` 是一个不变的数，两者在面板上长得几乎一样。

### `acks × min.insync.replicas` 联合体判定表（RF=3）

| `acks` | `min.insync.replicas` | 实际等几份 | 允许同时挂几台而不停写 | 已 ack 的消息会不会丢 | 结论 |
| --- | --- | --- | --- | --- | --- |
| `0` | 任意 | 0 | 能一直写 | **必丢**：连"写进去了"都不知道 | 只给可丢数据 |
| `1` | 任意 | 1（leader） | 能挂 2 台还在写 | **会丢**：leader 一挂，未同步部分消失 | 可容忍丢失的日志 |
| `all` | `1`（**broker 默认值**） | = 当次 ISR | 能挂 2 台还在写 | **会丢，且丢法完全等同 `acks=1`** | ★ 最常见假安全感（事故三） |
| `all` | `2` | ≥ 2 | 挂 1 台（ISR 掉到 1 就拒写） | **不丢**（前提 `unclean=false`） | ★ 生产默认 |
| `all` | `3` | 3 | **0 台**（任一台故障即全分区停写） | 不丢 | 极端账务，用可用性换 RPO=0 |
| `all` | `2` 但 **RF=2** | ≥ 2 = 全员 | **0 台**（ISR=1 就拒写） | 不丢，但**毫无冗余** | 反模式：等于用"停写"换"不丢"，却没换来冗余 |

**这张表最重要的一行是第三行。** 把它写成一句话：

> **`acks=all` 但不设 `min.insync.replicas=2` 时，一旦 ISR 收缩到只剩 leader，`acks=all` 就等于 `acks=1`** —— 而且这件事**不会报错、不会告警、不会有人告诉你**。同一条链在事故三里的完整形态是：迁移/掉队把 ISR 打到 1 → `min.insync=1` 允许写入 → 写入成功、producer 全绿 → 那次唯一的盘坏掉 → 分区归零。

### `acks=all` 承诺什么、不承诺什么

| 你以为它保证 | 它实际只保证 | 缺的那一半靠什么补 |
| --- | --- | --- |
| 消息不丢 | 消息在**当时 ISR 每一份**的 page cache 里 | `min.insync.replicas ≥ 2` 钉住 ISR 下限；`unclean=false` 挡住"落后副本上位" |
| 消息已落盘 | **完全没有**。默认不 fsync，靠"多副本"代替"fsync" | 若真要 fsync，`flush.messages`/`log.flush.interval.messages` 会让吞吐掉一个数量级；更实际的做法是承认"所有副本同时掉电会丢页缓存尾部"，用上游可重放兜底（§29.9） |
| 副本数足够 | 只等 ISR；ISR 可能只剩 1 | `UnderReplicatedPartitions` 告警 + `min.insync` 硬线 |
| 事务性可见 | `read_committed` 的可见上限是 **LSO**，不是 HW | 事务别开太久；`transaction.max.timeout.ms` 是兜底不是设计 |
| 切换后一定不丢 | 只保证"ISR 里某几份有"，**不保证活下来的那台一定是 ISR** | `unclean.leader.election.enable=false`（默认值，必须以目标版本 broker 配置为准） |
| 生产端只会写一次 | `acks=all` 与"不重复"无关；重试就会重复 | 幂等生产（PID + 分区内 sequence，`enable.idempotence`）+ 消费侧幂等，细节 → [eng/26](../eng/26-message-reliability.md)、[eng/24](../eng/24-delivery-semantics.md) |

**失败时到底报哪个错（这几个名字必须分清，否则你会把"拒绝"当成"重试一下就好"）**：

| 异常 | 发生时刻 | 语义 | 正确反应 |
| --- | --- | --- | --- |
| `NotEnoughReplicasException` | append **之前** | 当次 ISR < `min.insync.replicas`，leader 主动拒收 | 这**是保护**，不是故障。计数 + 告警；应用侧必须能落本地 outbox |
| `NotEnoughReplicasAfterAppendException` | append **之后**、ack 之前 | 消息已写进 leader，但 ISR 缩到低于下限，无法确认"够份数" | 不许当成"失败重发"（会重复）；按"可能已写入"处理，用业务幂等键 |
| `TimeoutException` | 重试耗尽 | `delivery.timeout.ms`（默认量级 120 s）到期 | **这才是真丢**：应用必须兜底（落库/对账），不能只打日志 |
| `RecordTooLargeException` | 客户端 | 超过 `max.message.bytes` 或 `replica.fetch.*.max.bytes` | 挡住；否则 follower 拉不动那一段，HW 会永久卡住 |

---

## 29.6 ISR 收缩：可用性是怎么一级一级掉下去的

### 判据：ISR 是「时间口径」，不是「字节口径」

leader 为每个 follower 记两个时间：它上次发起拉取的时间、以及它**上次追平 leader LEO** 的时间。判据是后者：

```
follower 被踢出 ISR ⟺ 距离它上一次「LEO 追平 leader 当时的 LEO」已超过 replica.lag.time.max.ms
```

三个直接后果：

1. **一个"落后 5 亿字节但一直在慢慢追"的 follower 可能留在 ISR**（只要它曾经追平过、且追平的间隔没超阈值）；反之，一个"只落后 1 万条但网络断了 25 秒"的 follower 会被踢。所以**网络抖动比磁盘慢更容易踢人**。
2. 默认值随版本变动（早期是 10,000 ms 量级，较新版本是 30,000 ms 量级），**以目标版本 broker 配置的默认值为准**。更早的版本还配有一个按条数的 `replica.lag.max.messages`，早就被移除——很多老文章里"落后 4,000 条就踢出 ISR"的说法来自那里，现在不成立。
3. **调大这个值 = 允许"落后"变成合法状态**（事故一）；**调小这个值 = 让 ISR 反复进出**（§29.10 坑四）。它是本篇唯一一个"两头都能弄坏系统"的参数。

### 图 3 · ISR 3 → 2 → 1 的恶化路径

```text
   ISR = 3                    ISR = 2                    ISR = 1
 ┌──────────────┐          ┌──────────────┐          ┌──────────────┐
 │ B1 B2 B3     │  B3 掉队  │ B1 B2        │  B2 也掉队 │ B1（leader） │
 │ LEO 三份齐全  │ ───────► │ HW 由 B2 定   │ ───────► │ HW = B1 LEO  │
 └──────────────┘  被踢出   └──────────────┘  或分区   └──────────────┘
        │                （lag.time.max）       ───►          │
        ▼                        ▼                            ▼
 写入:  正常                  写入: 正常                  写入: min.insync=2 时
        acks=all 等 3 份            acks=all 等 2 份            ★ 100% 被拒
 可见:  HW 由最慢者定            HW 前跳（延迟反降！）        可见: 没有新数据，HW 追平 LEO
 冗余:  2 份                   冗余: 1 份（假安全）         冗余: 0
 切主:  秒级，不丢              切主: 仍不丢（B2 有全量）      切主: 只能 unclean（丢）或停等
 恢复:  —                     追平需传 落后字节/可用带宽      追平需等一个完整副本回来
```

**读图要点：延迟不是单调恶化的，可用性是。** 从 3 到 2 时 HW 会前跳、`acks=all` 会变快、延迟曲线看起来"变好了"；代价是**冗余从 2 份变成 1 份**，而且没有任何指标会因此变红（`UnderReplicatedPartitions` 会变红，但它说的是"少于 RF"，不是"你离停写还有多远"）。真正的悬崖在 2 → 1：那一刻不是变慢，是**直接拒写**。

### ISR 3 → 2 → 1 时写入行为与可用性逐级恶化表（RF=3，`acks=all`，`min.insync.replicas=2`）

| ISR | HW 位置 | `acks=all` 的延迟 | 写入结果 | 实际冗余 | 还能承受几台故障 | `UnderReplicatedPartitions` |
| --- | --- | --- | --- | --- | --- | --- |
| 3 | 由最慢成员定 | 基准（≈ leader + 最慢 follower 的一个拉取周期） | 正常 | 2 份 | 1 台 | 0 |
| 2 | 前跳到剩余两台较小者 | **下降**（等的份数少了） | 正常（刚好满足下限） | 1 份 | **0 台**（再挂一台即 ISR=1） | 1 |
| 1 | 与 leader LEO 重合 | 等于 `acks=1` | **100% 被拒**（`NotEnoughReplicas`），重试直到 `delivery.timeout.ms` 耗尽 | 0 份 | 已经在停写 | 2 |

### 与 MySQL 半同步退化对照：同一类问题的两种形态

[comp/10 主从复制与高可用](10-replication-and-ha.md) §10.4 讲的"半同步超时静默退化为异步"，和本篇的"ISR 收缩"，是**同一个失效模式的两种形态**：都是"同步"这个词挂了一个会自己松开的定时器，而松开的时候不报错。

| 维度 | MySQL 半同步（`AFTER_SYNC`，未退化） | MySQL 半同步**退化后**（超时后降为异步） | Kafka ISR 正常（`ISR=3`） | Kafka ISR **收缩后**（`ISR=1`，`min.insync` 未设） |
| --- | --- | --- | --- | --- |
| **从哪里取副本** | 主库把 binlog 事件推给从库，等 `wait_for_replica_count` 个 ack | ack 等不到就**放弃等待**，不给任何副本再等 | follower **主动拉** leader 的日志，leader 记下各自的 LEO | ISR 缩到只剩 leader；"等 all"没有对象可等 |
| **什么时候算提交** | ack 到达后（`AFTER_SYNC`：先复制后可见） | 超时即提交 —— **提交时刻已经不再包含"副本已收到"** | `HW = min(ISR LEO)` 前移的那一刻 | HW 等于 leader 的 LEO —— **提交时刻不再包含任何副本** |
| **谁是"同步中的副本"集合** | 由 `rpl_semi_sync_source_clients` / `wait_for_replica_count` 隐式决定，没有"集合"这个概念 | 空集也照样提交（`no_tx` 计数器在涨） | ISR，由 `replica.lag.time.max.ms` 定义，是一个**显式集合** | ISR = {leader}，集合合法但退化 |
| **退化触发条件** | —— | ack 延迟 > `rpl_semi_sync_source_timeout`（默认 10,000 ms 量级） | —— | 落后超过 `replica.lag.time.max.ms` 未追平，**或被**有人把它调大以"止住告警" |
| **退化后丢什么** | —— | 退化持续时长 × TPS 的未同步事务；且期间已被其他会话读到的数据**无法回收** | —— | leader 宕机时的"未同步到任何 follower 的尾部"；若再叠 unclean ⇒ 已 ack 的整段怀孕消失 |
| **怎么发现（唯一可靠信号）** | `Rpl_semi_sync_source_status=ON` 且 `no_tx` 不增长 | **`status` 翻成 OFF、`no_tx` 开始增长**——它只翻状态位，不进错误日志 | `UnderReplicatedPartitions=0` 且 `IsrShrinksPerSec=0` | **`UnderReplicatedPartitions > 0` 持续、ISR 大小 < RF**；更早的信号是 `min.insync.replicas` 相对 ISR 的余量 |

**一句话对照**：MySQL 的半同步是"**推 + 等 ack + 超时放手**"，Kafka 的 ISR 是"**拉 + 最小值 + 超时踢人**"。前者放开时不留痕迹（只翻一个状态位），后者踢人时会留一个可观测的集合变化（ISR 变短）——**所以 Kafka 这边唯一的防线是盯 `UnderReplicatedPartitions`，而不是盯 producer 的错误率**。两边的完整失效窗口、切换流程与演练脚本一律看 [comp/10](10-replication-and-ha.md) §10.4 与 §10.8。

---

## 29.7 unclean 选举：拿丢数据换可用

### 它打开了什么

`unclean.leader.election.enable` 打开后，controller 允许**从不在 ISR 里的副本**中选 leader。默认值在历史上翻转过：0.11 之前默认 `true`，之后默认 `false`（**以目标版本 broker 配置的默认值为准**；是否支持 topic 级覆盖也请按目标版本文档确认）。

它省掉的成本是"等副本回来"（可能几十分钟到几小时），它付出的成本是**截断**：

```
旧 leader（已挂）:  ────────────────┘ LEO = 1,207,000  ← 已 ack 到 1,207,000
落后副本（唯一活着）:  ──────────┘ LEO = 1,190,000    ← 落后 17,000 条
                              ▲
                      unclean 选它当 leader
                              新 HW = 1,190,000
                              ▼
旧 leader 回来时：   ──────────┘✂️（按 leader epoch / HW 截断到 1,190,000）
                              被删掉的 17,000 条 offset 会被**新消息重新占用**
```

三件事同时发生，缺一不可：

1. **已 ack 的消息在唯一活着的副本上根本不存在**（它们只在那两台挂掉的机器上）；
2. **offset 被复用**——1,190,000 之后的新消息会拿到同样的 offset 号，**位点不再唯一标识一条消息**；
3. 消费组位点通常已经提交到 1,207,000 附近，消费者**已经"越过"那段从未存在的区间**，即使数据回来也不会被再投一次。

所以本篇必须写死一句话：**unclean 选举之后，"用位点重放"这条路被永久切断；唯一的真相只剩上游（业务库 binlog / Outbox / 设备网关），而它不一定还在。**

### 故障组合 × 是否 unclean × 丢多少 × 是否可恢复

前提假设：RF=3、`acks=all`、`min.insync.replicas=2`、`unclean.leader.election.enable=false`（第 3 行外均如此），"已 ack"指 producer 收到成功响应。

| 故障组合 | `unclean` | 丢多少 | 能不能恢复 | 说明 |
| --- | --- | --- | --- | --- |
| 挂 1 台（follower 或 leader），其余 2 个在 ISR | 无所谓 | **0** | 能，秒级切主 + 副本重建 | 这是 `RF=3 + min.insync=2` 买到的全部东西 |
| 挂 2 台，剩下那台**在 ISR 内** | `false` | **0** | 能（切主），但 ISR=1 立刻开始拒写，直到有副本追平 | 可用性受损、数据不丢——这是"宁可停写"的收益 |
| 挂 2 台，剩下那台**在 ISR 内** | `true` | **0**（它有全部已 ack 数据） | 能，但**没有意义**：不 unclean 也能选它，开了只是把风险窗口放大 | 开了不代表一定丢，只代表"下次落了也不管" |
| 挂 2 台，剩下那台**不在 ISR**（落后 L 条） | `false` | **0**，但**分区不可用**（无 ISR 成员可当选），停写直到副本回归 | 能，只要磁盘还在、日志没被 retention 删掉 | 代价 = 停写时长 × 该分区流量 |
| 挂 2 台，剩下那台**不在 ISR**（落后 300 万条） | `true` | **300 万条 ≈ 1.2 GB 已 ack 消息永久消失** | **不能**（位点被复用、消费者已越过、上游 binlog 可能过期） | ★ 事故二 |
| 3 台同时掉电（同机房 / 同 UPS） | 无所谓 | 各副本 page cache 中**未刷盘尾部**，量级 = 一次 OS 刷盘间隔内的写入（几百 MB 到数 GB） | 勉强能：HW 回退到已持久化的位置，但**这一段没有任何副本保住** | 这就是"Kafka 不做 fsync"的账单，靠的永远是"副本不在同一故障域" |
| Topic 只有 1 副本（`RF=1`，常见于自动建 topic） | 无所谓 | 那台机器坏多少丢多少 | **不能** | 参数再好也没用，因为没有第二个副本可等 |

### 结论：这是业务决策，必须写进预案

把它写成工程语言，是一个三变量的兑换：

```
选择 unclean=true  ：RPO = 落后副本的落后量（可能 = 半小时写入量）
                     收益 = MTTR 从「等副本回来」降到「几秒」
选择 unclean=false ：RPO = 0（对已 ack 消息）
                     收益 = 0，代价 = 停写时长 = min(副本恢复时间, 人工修盘时间)
```

所以预案里必须写清**四行**，少一行就等于把这个决策留给了凌晨三点的值班同学：

1. **谁有权开**：不是值班运维，是业务 owner + 数据 owner 双签（含"这次最多可以丢多少条 / 折多少钱"）。
2. **开了之后必须同时启动什么**：上游重放脚本 + 对账窗口 + 下游停写开关（否则丢的数据会被下游当成"从未发生"直接归档）。
3. **开多久自动关**：写进值班流程，恢复后立刻改回 `false` 并核对生效（改 broker 静态配置常需滚动重启，很多团队"改完忘了重启"或"重启完忘了它已是 true"）。
4. **事后怎么量出丢了多少**：比对 `LogStartOffset` / HW 的回退、各副本的 leader epoch 分叉点、消费组提交位点 vs 上游自增序列的最大值。**注意：事后几乎不可能精确枚举丢的具体是哪几条**——所以第 1 行才必须提前签，而不是事后算。

---

## 29.8 三本账：复制带宽、可见延迟与丢失量

### 账一：复制放大——3 副本 × 200 MB/s 写入，集群要多少网络

假设：某集群主写入 topic 峰值 **200 MB/s**（逻辑字节，压缩后入向）；RF=3；3 台 broker 均分 leadership；同机房。

| 量 | 推导 | 结果 |
| --- | --- | --- |
| 客户端 → leader（入向） | = 逻辑写入量 | **200 MB/s** |
| leader → 2 个 follower（出向） | 2 × 200 | **400 MB/s** |
| 集群总网络搬运量 | 200 + 400 | **600 MB/s = 3 倍**（每个字节被搬 3 次） |
| 单台 broker 入向（均分） | 200/3（客户端）+ 400/3（复制） | ≈ 66.7 + 133.3 = **200 MB/s** |
| 单台 broker 出向（均分） | 400/3 | ≈ **133 MB/s** |
| 单台网卡需求 | 200 入 + 133 出 | 10 GbE（≈1,190 MB/s 线速）占用约 28%；**但副本重建会临时翻倍**，建议 25 GbE |
| 稳态磁盘占用 | 200 MB/s × 86,400 s = 17.3 TB/天 × 3 副本 × 保留 3 天 | ≈ **155 TB**（再加 15% 余量给 segment 与截断） |
| 每台磁盘写入 | 17.3 TB/天 ÷ 3 台 × (1 副本 + 追赶) | ≈ 5.8 TB/天/台 ≈ **67 MB/s/台**（顺序写，机械盘也够；瓶颈在网络不在盘） |
| 挂一台后的重建流量 | 一份完整副本（≈ 51 TB / 3 ≈ 17 TB）÷ 允许带宽 100 MB/s | ≈ **47 小时**（这就是"单 broker 不能太大"的那条线） |
| 跨 AZ 的账（若 follower 在另一 AZ） | 400 MB/s × 86,400 = 34.6 TB/天；按 0.1 元/GB | ≈ **3,460 元/天 ≈ 10.4 万元/月**；单价 0.5 元/GB 时 ≈ 52 万元/月 |

> **量级结论**（不是承诺值）。假设清单：200 MB/s 为压缩后入向逻辑写入；三副本均分 leadership；重建带宽 100 MB/s 为可用量（实际还受 `replica.alter.log.dirs.io.max.bytes.per.second`、`num.replica.fetchers`、目标盘 IOPS 限制）；跨 AZ 单价按 0.1 元/GB 量级估算。**换任一假设即需重算**，尤其"跨 AZ 单价"在不同云差异可达 5 倍。

**这张账的真正结论**：`RF=3` 的成本不是"磁盘 ×3"，是**网络 ×3**——而网络比磁盘贵、也比磁盘难扩。所以"要不要 RF=3"从来不是一个可靠性问题，是"这个 topic 的丢失代价 vs 3 倍网络与 3 倍盘"的对账。

### 账二：HW 推进延迟——`acks=all` 时一条消息多久才能被消费

假设：`replica.fetch.wait.max.ms` = 500 ms（默认量级）；同机房 RTT ≈ 0.3 ms；leader append 到 page cache 的 P99 ≈ 1–3 ms；`linger.ms` = 5 ms（3.x 默认量级）。

| 阶段 | 耗时 | 说明 |
| --- | --- | --- |
| producer 攒批 | 0 – `linger.ms`（≈ 0–5 ms） | 攒批换吞吐，代价是尾部延迟 |
| 网络 + leader append | 0.5 – 3 ms | 写 page cache，**不 fsync** |
| 等 follower 下一次拉取 | **0 – 500 ms**（期望 250 ms） | 取决于消息到达时刻落在拉取周期的哪一段 |
| follower append + 回下一次拉取 | ≈ 0.5 – 2 ms | 本地页缓存追加 |
| HW 前移（下一次拉取带回 LEO） | **0 – 500 ms**（期望 250 ms） | ★ 这一步经常被漏算：HW 要等 follower **再问一次** |
| 消费者侧 `fetch.max.wait.ms` | 0 – 500 ms | 消费者自己也有攒批窗口 |
| **合计（生产者 ack → 消费者可见）** | **P50 ≈ 0.5 s，P99 ≈ 1.0–1.2 s** | 量级：**秒级，不是毫秒级** |

三个变体值得单独算：

| 变体 | 复算式 | 结果 |
| --- | --- | --- |
| `replica.fetch.min.bytes` 调大（例如 1 MB） | 低流量分区凑不满就等满 `fetch.wait.max.ms` | 可见延迟被**下限**锁在 ≈ 500 ms，与流量无关 |
| 跨机房 follower（RTT 2 ms，且对端盘抖动） | 5 ms linger + 2 次 RTT ≈ 4 ms + 对端 append P99 抬到 20–40 ms | `acks=all` 的 P99 从同机房 **8 ms → 60 ms**（量级结论，跨机房 P99 主要由对端磁盘与共享带宽的尾部决定，不是光速） |
| `num.replica.fetchers` 默认量级（每 broker 1 个 fetcher 线程处理所有分区） | 一台 broker 领 2,000 个分区、每轮 fetch 处理 0.2 ms | 单轮 ≈ 400 ms > 500 ms 的 wait 值附近抖动 ⇒ 实际拉取周期被线程池拉长，可见延迟按倍数放大 |

> **量级结论**：`可见延迟 ≈ linger + leader append + 2 × 拉取周期 + 消费者攒批`。`拉取周期` 是这里的自由度，它由 `replica.fetch.wait.max.ms`、`num.replica.fetchers`、follower 磁盘与 broker 线程池共同决定。**这不是承诺值**；换 SSD/NVMe、换跨 AZ、换大分区数都会移动一个数量级。

### 账三：ISR 收缩后的写入拒绝与恢复时间

假设：Topic 50 分区，RF=3，`acks=all`，`min.insync.replicas=2`，峰值 40,000 msg/s（每条 400 B ⇒ 16 MB/s），一次网络故障让 18 个分区的 ISR 掉到 1。

| 量 | 推导 | 结果 |
| --- | --- | --- |
| 受影响分区占比 | 18 / 50 | **36%** |
| 这些分区上的写入结果 | ISR=1 < 2 ⇒ `NotEnoughReplicas` | **100% 被拒**（不是"变慢"，是"打不进去"） |
| 重试窗口 | `delivery.timeout.ms` 默认量级 120 s | 120 s 后转 `TimeoutException` |
| 应用侧看到的失败量 | 40,000 × 36% × 120 s | ≈ **173 万条发送失败**（应用必须自己兜住，Kafka 不替你留） |
| 每 broker 落后字节 | 16 MB/s × 300 s（故障持续 5 分钟） | ≈ **4.8 GB** |
| 追平耗时（宽裕） | 4.8 GB ÷ 100 MB/s 可用复制带宽 | ≈ **48 s** |
| 追平耗时（与在线流量争） | 4.8 GB ÷ 30 MB/s | ≈ **160 s** |
| 截断后从 segment 边界重拉 | 落后点通常不在段首，最坏多拉一个 segment | 再 **+10%–30%** |
| 总停写时长 | 故障 300 s + 追平 48–160 s | **6–8 分钟** |

> **量级结论**：`停写时长 ≈ 故障时长 + 落后字节 / min(可分配复制带宽, follower 磁盘写速率)`。**这不是承诺值**：真实值取决于故障持续多久、follower 是否被 truncate（被截断就要按 leader epoch 重拉，可能翻倍），以及你是否限速（运维习惯是限速保在线，那就按 30 MB/s 算而不是 100 MB/s）。

### 账四：unclean 选举的丢失量，以及为什么找不回来

场景即事故二：3 副本，2 台挂，剩下那台落后 **300 万条 × 400 B ≈ 1.2 GB**，`unclean=true` 让它当 leader。

| 量 | 推导 | 结果 |
| --- | --- | --- |
| 永久丢失条数 | 落后量 | **300 万条** |
| 永久丢失字节 | 300 万 × 400 B | **≈ 1.2 GB** |
| 折成计费事件（占 12%） | 300 万 × 12% | **36 万条** |
| 折成钱（对账差额口径） | 36 万条 × 平均 0.72 元/条 | ≈ **26 万元** + 720 人时补数 |
| 覆盖的写入时长 | 300 万 ÷ (16 MB/s ÷ 400 B) ≈ 300 万 ÷ 40,000 msg/s | ≈ **75 秒**（若该 topic 峰值更高，覆盖时长更短，但条数一样） |
| 能否用位点重放找回 | ① offset 已被复用；② 消费位点已越过；③ retention 可能已过 | **不能** |

> **量级结论**：`unclean 的 RPO = 被提升副本的落后量`，与"故障持续多久"没有直接关系，只与**那个副本何时掉出 ISR** 有关。**这不是承诺值**：如果落后的副本恰好是"刚掉出 ISR 1 秒"的那台，损失可能只有几千条；如果它已经掉队 40 分钟，损失就是 40 分钟的写入量。**真正决定 RPO 的是"你多久发现它掉队了"**——也就是 §29.11 的第一指标。

### 账五：把 `replica.lag.time.max.ms` 从 30 s 调到 5 s 会多出多少 ISR 事件

假设：每 broker 领 2,000 个分区；网络抖动导致单个拉取周期超过 5 s 的概率约 0.1%（跨 AZ 链路上很常见）；拉取周期 500 ms。

| 量 | 推导 | 结果 |
| --- | --- | --- |
| 每分区每分钟拉取次数 | 60 s ÷ 0.5 s | 120 次 |
| 每分区每分钟被误踢概率 | 120 × 0.1% | ≈ **0.12 次/分钟** |
| 全 broker 每分钟 ISR 收缩事件 | 2,000 × 0.12 | ≈ **240 次/分钟（4 次/秒）** |
| 每次收缩的连带成本 | HW 前跳 + `acks=all` 等待集合变化 + 可能的 `NotEnoughReplicas` 窗口 + 一次告警 | —— |
| 结果 | 告警风暴 + 频繁的 ISR 抖动掩盖真实故障 + 每次收缩都可能让某些分区短暂拒写 | **监控彻底失效** |

> **量级结论**：`ISR 误踢率 ≈ 拉取次数 × 抖动超阈值概率`，**与分区数成正比**。这不是承诺值，0.1% 这个概率要按你自己链路的 RTT 分布去测（`kafka-log-dirs.sh` 的 `offsetLag` 与 broker 侧的 fetch 延迟直方图）。

---

## 29.9 架构边界：这套参数能保证什么、不能保证什么

| 能保证（在条件成立时） | 不能保证 | 靠什么绕过 |
| --- | --- | --- |
| 已 ack 消息在**当时 ISR 的每一份**上存在（`acks=all` + `min.insync≥2`） | 不保证在**故障域外**还有一份 | 副本**布局**（`rack` 感知分配，副本跨机柜/AZ），`min.insync=2` 且保证"至少 1 份在异地" |
| ISR 掉到下限以下时**拒绝写入**而不是静默降级（`min.insync.replicas`） | 不保证"拒绝"会被业务看见——`NotEnoughReplicas` 是 retriable，应用容易把它当抖动 | 显式计数 `NotEnoughReplicas` + 应用侧落本地 outbox + 对账 |
| 故障后不丢已 ack 消息（`unclean=false`） | 不保证**可用**：ISR 不足时就是停写，可能停几十分钟 | 这是决策点：停写 vs 丢数据，必须写进预案（§29.7） |
| 分区内**顺序与位点单调**（单 leader 串行 append） | 不保证位点**永久**唯一——unclean 之后 offset 会被复用 | 业务键（`order_id`）+ 幂等，不靠 offset 做业务主键 |
| 消费者看到的一定是"已提交"的（`≤ HW`） | 不保证"看到的就是最终结果"——`read_committed` 上限是 LSO，事务可能还开着 | 事务别开太久；`transaction.max.timeout.ms` 只是兜底 |
| 副本能承受单台机器故障 | 不承受"两次故障同时发生"（两台同时挂 = 靠 `unclean` 或停写） | 容量余量（留一台的空间）+ 机柜/AZ 隔离 + 定期演练 |
| 单分区吞吐线性（顺序 IO + 零拷贝，`→ comp/28`） | 副本数**不提高写吞吐**：写并行度 = 分区数 | 扩分区（`→ comp/30`），不是加副本 |
| 消息至少被处理一次（配合消费端先处理后提交） | 不保证只处理一次（生产重试、rebalance 重投都会重复） | 幂等消费 + 去重键，细节 → [eng/24](../eng/24-delivery-semantics.md) |
| —— | 不保证**刷盘**：所有副本同时掉电会丢页缓存尾部 | 承认它 + 上游可重放兜底；fsync 换不来可用性 |
| —— | 不保证跨城强一致 / 提交即多数派 | 这已超出主从复制形态，需要共识型日志（`→ comp/31`）；形态决策见 [architect/15](../architect/15-message-store.md) |

**一句话边界**：Kafka 的复制买到的是「**在故障域内、在 ISR 不缩水的前提下、在 unclean 关闭时**，已 ack 的消息不丢」。三个前提缺一个，断言就失效；而三个前提分别由**布局、`min.insync.replicas`、`unclean`** 三个不同位置的东西维持。

---

## 29.10 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到物理形态） | 定位 | 修复 | 防复发 |
| --- | --- | --- | --- | --- |
| "我们配了 `acks=all`，还是丢了" | `acks=all` 等的是**当次 ISR**；`min.insync.replicas` 用默认值（1），ISR=1 时写入照旧成功 | `kafka-configs.sh --describe --entity-type topics` 看 topic 上**到底有没有** `min.insync.replicas` 覆盖；`kafka-topics.sh --describe` 看 `Isr` 列长度 | 显式设 `min.insync.replicas=2`；查历史 ISR 曲线确认是否曾掉到 1 | topic 模板里写死 `min.insync.replicas`，并用"配置渲染测试"断言它存在（不要依赖 broker 默认值） |
| 设了 `min.insync.replicas=2`，一台机器挂就停写 | `RF=2`：ISR 最低只能是 2 = 全员，挂一台即 ISR=1 < 2 | `Replicas` 与 `Isr` 两列一起看 | 提到 `RF=3`（要走重分配，`kafka-reassign-partitions.sh`） | 上线前断言 `replication.factor ≥ min.insync.replicas + 1`；这条不满足的 topic 一律标红 |
| ISR 频繁进出，被告知"网络小问题，别管" | ISR 判据是"多久没追平 LEO"（时间口径）；收缩会让 HW **前跳**、延迟**下降**，看起来像好转 | `IsrShrinksPerSec` / `IsrExpandsPerSec` 速率、`UnderMinIsrPartitionCount`、跨机柜 RTT 分布 | 先查 RTT 与 GC 停顿；把阈值调回默认量级；给"收缩"配告警而不是给"落后条数"配 | 把 ISR 收缩速率当一级指标（不是只看 `UnderReplicatedPartitions` 的瞬时值） |
| 为压告警把 `replica.lag.time.max.ms` 调小（例如 5 s），结果更乱 | 拉取次数 × 抖动概率 = 误踢事件数，与分区数成正比（账五：4 次/秒） | 调参前后对比 `IsrShrinksPerSec` 与 broker CPU | 调回默认量级；改用"按 RTT 分布设阈值"或干脆接受告警 | 这个参数进入"变更需评审"名单；调大调小都写清代价 |
| 为压告警把它调**大**（例如 600,000 ms），半年后出现"打了个 6 分钟的空窗" | 落后变成**合法状态**：HW 被合法地钉在 6 分钟前，消费者读不到，`UnderReplicatedPartitions=0` | 比较每分区 `LEO − HW`（§29.11）与 `replica.lag.time.max.ms` 的取值 | 调回默认量级 + 用副本重建/限速取代"容忍落后" | 同上；这条比"调小"更危险，因为它**没有任何指标会变红**（事故一） |
| 机房掉电后业务要恢复，现场开了 unclean，事后发现丢了一大段已 ack 消息 | 允许非 ISR 副本上位 ⇒ 新 HW 回退 ⇒ 其余副本按新 leader 截断 ⇒ offset 被复用，位点重放失效 | 比对 `LogStartOffset` 与 HW 是否回退；看副本 leader epoch 分叉点；消费位点 vs 上游序列 | 立刻冻结下游归档、启动上游重放与对账 | 灾难预案里写清四行（谁有权开、开的同时启动什么、开多久自动关、事后怎么量）；把"改完要滚动重启才生效"写进步骤 |
| `acks=all` 的 P99 从 8 ms 变成 60 ms，团队以为是"Kafka 慢" | 副本跨机房：每次 ack 多两次 RTT，且对端磁盘尾部延迟决定 P99（账二变体） | 按分区看 leader 与 follower 的 `rack` 分布；对比同/跨 AZ 分区的 P99 | 关键 topic 的副本布局收敛到同 AZ + 异步跨域复制（代价：RPO>0）；或接受 60 ms | 把"副本布局"写进 topic 交付物，不留在集群自动分配 |
| 监控面板上"最新位点"一直在涨，但消费者读到的数据是旧的 | 把 `LEO` 当成了"最新可读位点"；可读上限是 **HW**（`read_committed` 还要看 LSO） | 同一时刻取 `LogEndOffset`、`HighWatermark`、消费组 `CURRENT-OFFSET` 三个数 | 面板改成 `HW` 与 `LEO−HW`；lag 用 `HW − CURRENT-OFFSET` 而不是 `LEO − CURRENT-OFFSET` | 监控模板统一口径；`LEO−HW` 单独一张图（这就是"已写未可见"） |
| 新业务 topic 从未显式建过，靠自动创建；某天机器坏了一块盘就丢了全部数据 | `auto.create.topics.enable=true` + `num.partitions`/`default.replication.factor` 默认，自动建出了 **RF=1** 的 topic | `kafka-topics.sh --describe` 全量扫一遍 `ReplicationFactor` 列 | 立刻重分配到 RF=3（在线重分配，注意限速） | 关掉自动建 topic；用"topic 清单巡检"每周比对 RF 与 min.insync |
| 生产端把 `NotEnoughReplicas` 当普通抖动重试，直到 `delivery.timeout.ms` 耗尽才算失败 | 该异常是 retriable，但重试期间消息**从未进入任何副本**；耗尽后是**真丢** | 应用侧统计 `NotEnoughReplicas` 计数与 `TimeoutException` 计数 | 落本地 outbox + 对账；`NotEnoughReplicas` 单独告警 | 把"发送失败必须落库"写进接入规范；错误分类表（§29.5）进 SDK 默认实现 |

---

## 29.11 观测：指标、拐点与告警

**前置开关（不满足则指标不存在，别以为是 0）**：JMX 远程访问需 broker 启动参数带 `-Dcom.sun.management.jmxremote*`（或由 exporter 以 JMX 代理方式抓取）——**很多"指标一直是 0"的故事，实际是 JMX 端口没开**。以下所有 metric 名以**目标版本 Kafka 的 JMX 输出为准**（Kafka 3.x / KRaft 模式与 2.x / ZK 模式的 metric 集合有差别，个别名字在大版本间改过）。

| 看什么 | 指标 / 命令 | 拐点 | 版本与开关前提 |
| --- | --- | --- | --- |
| **① 未充分复制的分区数（第一指标）** | `kafka.server:type=ReplicaManager,name=UnderReplicatedPartitions` | **> 0 持续 60 s 即 P2；> 0 且 `min.insync.replicas` 余量为 0 即 P1**（你在悬崖边） | 全版本存在；**单位是"分区个数"，没有比例信息**——大集群里 3 个分区和 3,000 个分区是一个数，必须配 `PartitionCount` 一起看 |
| ② 已经贴到下限的分区数 | `kafka.server:type=ReplicaManager,name=UnderMinIsrPartitionCount`、`AtMinIsrPartitionCount`（若目标版本无此项，用 §29.11 脚本自采） | `UnderMinIsr > 0` 意味着这些分区**现在就在拒写**；`AtMinIsr > 0` 意味着再掉一个就拒写 | 较新版本提供的分区级状态指标，**存在性与名称以目标版本 JMX 输出为准** |
| ③ ISR 抖动速率 | `kafka.server:type=ReplicaManager,name=IsrShrinksPerSec` / `IsrExpandsPerSec` | 稳态应接近 0；**持续 > 0.1/s 就是"一直在抖"**，而不是"抖了一下" | 全版本；这两个是**速率**指标（有些 exporter 已做 `rate`，注意别二次求导） |
| ④ 每分区「已写未可见」 | `LogEndOffset` 与 `HighWatermark` 两个数相减：`kafka.log:type=Log,name=LogEndOffset,topic=…,partition=…`（HW 在部分版本以 `kafka.cluster:type=Partition,name=HighWatermark` 暴露） | `LEO − HW` 超过"正常写入延迟 × 写入速率"的 3 倍即异常；**绝对值持续增长 = 有人在钉 HW** | 每分区级 metric 基数高（分区数 × 2），大集群要先确认是否开启分区级采集；**字段名以目标版本 JMX 输出为准**，若不存在则用 AdminClient `listOffsets` 自采 |
| ⑤ follower 落后量 | `kafka.server:type=FetcherLagMetrics,name=ConsumerLag,clientId=ReplicaFetcherThread-*,topic=…,partition=…`（**口径：消息条数**，不是字节、不是时间） | 单副本 `ConsumerLag` 持续增长 = 它在掉队（这就是"HW 被谁钉住"的答案） | 需指定 `clientId` 匹配 replica fetcher 线程；**"按时间"口径没有直接指标**，要用"落后字节 ÷ 写入速率"换算 |
| ⑥ 生产侧失败 | `kafka.network:type=RequestMetrics,name=FailedProduceRequestsPerSec,request=Produce`；客户端侧 `record-error-rate`、`request-latency-*` | 失败率 > 0.1% 即查；`FailedProduce` 突增 + ISR 缩小通常同源 | 生产端失败也可来自 `NotEnoughReplicas`（**retriable 但仍在失败计数里**） |
| ⑦ `min.insync` 相关拒绝 | **没有独立 JMX 计数**（重要）。用 ⑥ + 客户端异常分类统计 + broker 日志关键字组合 | 出现 `NotEnoughReplicas` 就意味着那些分区此刻在拒写 | 客户端 SDK 必须把该异常单独计数，否则这条链路完全不可观测（§29.5 错误分类表） |
| ⑧ LSO 滞后 | `LastStableOffsetLag`（分区级；**存在性与字段名以目标版本 JMX 输出为准**），或 AdminClient `listOffsets(LAST_STABLE)` 自采 | 非 0 且持续 = 有长事务钉住了 `read_committed` 的可见上限 | 只影响 `isolation.level=read_committed` 的消费者；非事务 topic 应恒为 0 |
| ⑨ 跨机房链路 RTT | 主机层 `ping`/`mtr` 的 P99、`tc` 记录的链路延迟、以及 ⑥ 的 P99 与同 AZ 分区的对照 | 跨区 RTT 的 P99 一抖，`acks=all` 的 P99 就跟着抖（账二变体） | 与 Kafka 无关但决定它；**必须和分区 `rack` 分布一起看** |
| ⑩ 消费侧 lag（下游告警用） | `kafka-consumer-groups.sh --describe` 的 `LAG` 列 | 用 `HW − CURRENT-OFFSET` 口径（很多工具默认用 `LEO`，会多算"已写未可见"那一段） | 全版本；积压的系统性排查见 `→ comp/30` |

```bash
# 每日 60 秒巡检：顺序即优先级（JMX 端口未开则前四条会失败，先确认前置开关）
JMX=service:jmx:rmi:///jndi/rmi://broker-1:9999/jmxrmi
run() { kafka-run-class.sh kafka.tools.JmxTool --jmx-url "$JMX" --one-shot \
        --object-name "$1" --attributes "${2:-Value}" 2>/dev/null | tail -1; }

run kafka.server:type=ReplicaManager,name=UnderReplicatedPartitions   # ① 第一指标
run kafka.server:type=ReplicaManager,name=IsrShrinksPerSec            # ③ 抖动速率
run kafka.server:type=ReplicaManager,name=IsrExpandsPerSec
run kafka.server:type=ReplicaManager,name=PartitionCount              # ① 的分母

# topic 级核对：RF / ISR / min.insync 三个数必须在同一屏里看一眼
kafka-topics.sh --bootstrap-server broker-1:9092 --describe --topic order.events \
  | grep -E 'Partition:|Isr' | head -6
# 期望：Replicas 3 个、Isr 3 个；Isr 比 Replicas 短一个 = 已经在悬崖边

kafka-configs.sh --bootstrap-server broker-1:9092 --entity-type topics \
  --entity-name order.events --describe
# 期望看到 min.insync.replicas=2 与 unclean.leader.election.enable=false
# 若输出为空 ⇒ 在用 broker 默认值（通常是 min.insync=1）★ 事故三的入口

# 哪个副本在掉队（offsetLag 列）：HW 被谁钉住，答案在这里
kafka-log-dirs.sh --bootstrap-server broker-1:9092 --describe \
  --topic-list order.events | tr ',' '\n' | grep -E 'broker|offsetLag' | head -12
```

**告警表达式骨架**（指标名按你的 exporter 前缀改写；口径说明见上表）：

```
# ① 有人在掉队（持续，不是抖一下）
max(kafka_server_replicamanager_underreplicatedpartitions) > 0                    for 5m
# ② 已经贴到下限：这不是"性能问题"，是"马上拒写"
max(kafka_server_replicamanager_underminisrpartitioncount) > 0                     for 1m
# ③ ISR 在抖（速率，不是瞬时值）
rate(kafka_server_replicamanager_isrshrinks_total[5m]) > 0.1                       for 10m
# ④ 已写未可见在扩大（需要分区级采集；分母用该分区写入速率归一）
(max by (topic) (kafka_log_logendoffset - kafka_cluster_partition_highwatermark)) > 3 * <正常延迟秒数 × 写入速率>
# ⑤ 生产侧真失败（注意：NotEnoughReplicas 也是失败，但它是保护机制 → 单独看趋势）
rate(kafka_network_requestmetrics_failedproducerequestspersec_total[5m]) > 0.001 * <总发送速率>
# ⑥ 跨区链路 P99（决定 acks=all 的尾延迟）
histogram_quantile(0.99, rate(<cross_az_rtt_bucket>[5m])) > 5
```

---

## 29.12 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 四个位点 | `LogStartOffset ≤ LSO ≤ HW ≤ LEO`；**HW = min(ISR 的 LEO)**；消费者只能读到 HW（`read_committed` 读 LSO） |
| "写进去了但看不见" | `LEO − HW` 就是"已写未可见"的条数；它不是 bug，是 HW 的定义 |
| 复制方向 | **follower 主动拉**，不是 leader 推；所以 HW 推进速度 = follower 的拉取节奏 |
| 可见延迟 | ≈ `linger + leader append + 2 × 拉取周期 + 消费者攒批`；默认量级 **P50 0.5 s / P99 1.0–1.2 s** |
| 定理 | `acks=all` + ISR 稳定时，**ack 与 HW 前移最多差一个拉取周期**；"几十分钟看不见"必有第三个原因（LSO / 拉取阈值被调大 / acks 没等 ISR） |
| `acks=0/1/all` | 0 什么也不保证；1 只保证 leader 页缓存；all 保证**当时 ISR 每一份**的页缓存 |
| 联合体 | `acks=all` + `min.insync.replicas=2` + `replica.lag.time.max.ms=默认量级` + `unclean=false`，四条缺一即降级 |
| 假安全感 | `acks=all` 但 `min.insync` 用默认 1 ⇒ ISR 收缩到 1 时**等同 `acks=1`**，且不报错不告警 |
| ISR 判据 | 时间口径：**多久没追平 leader 的 LEO**（不是落后多少字节）；所以网络抖动比磁盘慢更容易踢人 |
| ISR 收缩的反直觉 | HW 会**前跳**、`acks=all` 会**变快**、延迟曲线看起来"好转"；真正恶化的是**冗余**，悬崖在 ISR < `min.insync`（直接拒写） |
| 拒写语义 | `NotEnoughReplicas` 是**保护**不是故障；`NotEnoughReplicasAfterAppend` 按"可能已写入"处理；`TimeoutException` 才是真丢 |
| unclean 选举 | = 允许非 ISR 副本上位 = 新 HW 回退 = 其余副本被截断 = **offset 被复用** ⇒ 位点重放永久失效 |
| unclean 的账 | RPO = **被提升副本的落后量**（账四：300 万条 × 400 B = 1.2 GB ≈ 26 万元）；收益 = MTTR 从小时降到秒 |
| unclean 的处置 | 四行预案：谁有权开 / 同时启动什么 / 多久自动关 / 事后怎么量。**不是运维参数** |
| 复制放大 | RF=3 ⇒ 网络 **3 倍**（客户端 200 + 复制 400 = 600 MB/s）；盘 3 倍；跨 AZ 时这笔钱按 GB 计价 |
| 副本不买写吞吐 | 写并行度 = **分区数**；副本只买可用性与读吞吐（`→ comp/30`） |
| 防线四层 | ① RF≥3 且跨故障域 ② `acks=all`+`min.insync=2` ③ `unclean=false` ④ 容量余量 + 定期演练 |
| 第一指标 | `UnderReplicatedPartitions > 0` 持续 60 s；**第二指标是它与 `min.insync.replicas` 的余量** |
| 一句话 | **"不丢"不是参数，是四个参数的一次联署；任何一方撤回签字，整条链就降级——而其中有一方（unclean）签的其实是业务的风险额度。** |

---

## 29.13 自检题

**1. 你们的 `acks=all`、RF=3、ISR 一直稳定在 3，但下游说你"发了消息，我们 5 分钟没收到"。请给出三种可能的物理原因（各自对应哪个位点），并说明为什么"某个 follower 落后"不是答案。**
<details><summary>答案</summary>

先立定理：ISR 稳定在 3 且 `acks=all` 时，ack 与 HW 前移最多相差一个拉取周期（默认量级 0.5–1 s）。落后的 follower 要么在一个周期内被 ack 等到，要么超过 `replica.lag.time.max.ms` 就被踢出 ISR、HW 立刻前跳——两种情况都不会让 HW 落后 5 分钟。所以三种原因只能是：

1. **LSO 被钉住**：有一个事务开着没提交（下游 RPC hang、代码忘了 commit/abort），`read_committed` 消费者的可见上限停在 LSO 而不是 HW。对应位点 **LSO**。定位：`LastStableOffsetLag` 非 0、或 AdminClient `listOffsets(LAST_STABLE)` 远小于 `LEO`；同时查 `transaction.max.timeout.ms`（默认量级 15 分钟）是否就是那 5 分钟的上界。
2. **`replica.lag.time.max.ms` 被调大**：此时"落后"是合法状态，`UnderReplicatedPartitions=0`、`IsrShrinksPerSec=0`，但 HW 可以合法地停在 5 分钟前。对应位点 **HW**。定位：`LEO − HW` 持续增长，且 broker 配置里那个值是分钟级。
3. **实际生效的 `acks` 不是 all**（客户端覆盖、多环境配置漂移、或者某条代码路径用了另一个 Producer 实例配 `acks=1`）。对应位点 **HW**（但原因在生产端）。定位：broker 侧 `Produce` 请求的 `acks` 字段、客户端配置快照、以及对比"ack 时刻"与"HW 前移时刻"的时间差。

第四个候选（别忘了）：**消费者侧**——`fetch.max.wait.ms`、消费者 `max.poll.records` 太小、消费者线程卡在业务逻辑、或者消费组位点提交正常但下游处理阻塞。这一类与位点无关，看 `HW − CURRENT-OFFSET` 是否也在涨。
</details>

**2. Topic 3 副本、`acks=all`、`min.insync.replicas` 没设（broker 默认 1）。某次 leader 所在 broker 宕机，ISR 里当时只有 leader 一个。请写出完整的丢失链，并给出四项修复。**
<details><summary>答案</summary>

链条五步：① 某 follower 因网络抖动/磁盘慢，超过 `replica.lag.time.max.ms` 未追平 → ISR 缩到 {leader}；② 因为 `min.insync.replicas=1`，leader 认为"1 ≥ 1"，`acks=all` 照常接受写入并返回成功（**此时 "all" 的对象只剩 leader 一个人**）；③ 写入的这段数据只存在于 leader 的内存页与磁盘上；④ leader 所在 broker 宕机（盘坏、掉电）；⑤ 剩下两个副本的 LEO 停在掉队位置，它们的 HW 也停在那里；controller **不会**把它们提升为 leader（它们不在 ISR，且 `unclean=false`），分区进入不可用；若此时 `unclean=true`，落后副本上位并截断 → 那段已 ack 的消息永久消失，offset 被复用。

修复四项：① **`min.insync.replicas=2`**（topic 级显式设，别依赖 broker 默认）；② **`unclean.leader.election.enable=false`**（确认目标版本 broker 默认值并显式写死）；③ **`UnderReplicatedPartitions`** 告警 + 与 `min.insync.replicas` 的余量为 0 时升 P1；④ 应用侧把 `NotEnoughReplicas` 单独计数并落本地 outbox（否则"保护"变成"静默丢"）。

补充一条最容易被忽略的：确认 topic 的 `replication.factor` 真的是 3（自动建 topic + 默认值常常给出 1 或 2），并把这条断言写进 topic 交付检查。
</details>

**3. 同一个 topic，副本从同机房改成"1 份在异地 AZ"，`acks=all` 的 P99 从 8 ms 变成 60 ms。请把这 60 ms 拆开，并给出两个可选方案与各自代价。**
<details><summary>答案</summary>

拆分（量级推理）：`linger.ms` 5 ms（不变）+ leader append 到页缓存的 P99 3 ms（不变）+ **两次跨 AZ RTT**（follower 拉取请求与数据回程，各按 1–3 ms，合计约 2–6 ms）+ **异地 broker 的 append P99 抬到 20–40 ms**（异地机器常与别的负载共享盘与带宽，且它自己的 follower 也要复制）+ 队头/批处理放大。D99 的这个地板由**对端磁盘尾部**决定，不是由光速决定——这是很多人算错的地方：他们只算 RTT，忽略了对端 P99。

两个方案：
- **A. 副本全部同 AZ，跨域用异步复制（MirrorMaker 2 / 集群间复制或下层存储复制）**。代价：跨域 RPO > 0（异步必然丢尾部），要单独定义"跨域切换时允许丢多少秒"，并且异地那份**不能反哺一致性**。收益：`acks=all` 回到 8 ms，容量与延迟预算稳定。
- **B. 保留跨域副本，但把 `min.insync.replicas` 设为 2，且配合 `rack` 感知布局保证"2 份在同 AZ、1 份在异地"，同时接受异地那份允许落后**。注意这里有个陷阱：`min.insync=2` 只保证"ISR 里有 2 个"，**不保证那 2 个不在同一个机柜**——要真正买到"跨 AZ 不丢"，必须让 ISR 的构成规则跟着走（例如 `replica.selector.class` 之类的机架感知策略，可用性与具体机制以目标版本文档为准）。代价：策略复杂度上升，且一旦异地那份被算进 ISR，P99 就是 60 ms。

判据：**如果业务能接受跨域 RPO > 0，选 A**（延迟和容量都可控）；如果必须"跨域也不丢"，那么 60 ms 就是这笔保险的保费，不能既要 8 ms 又要跨域不丢。
</details>

**4. 事故二的场景重演：3 副本挂 2，剩下那台落后 300 万条（400 B/条）。请算 RPO、说明为什么"用位点重放"救不回来，并写出预案里必须有的四行。**
<details><summary>答案</summary>

RPO：300 万 × 400 B = **1.2 GB**；若 12% 是计费事件 ⇒ 36 万条，按对账差额口径 ≈ 26 万元 + 720 人时。注意 RPO 与"故障持续多久"无关，只与**那台副本何时掉出 ISR**有关——所以真正的因变量是"你多久发现它掉队"。

为什么位点重放救不回来（三条同时成立）：① 新 leader 把它的 LEO 作为分区真相，其余副本回来时按 leader epoch / HW **截断**，分叉尾部被物理删除；② 之后写入的新消息会**复用同一批 offset 号**，位点不再唯一标识一条消息，你按 offset 重放只会拉到"内容不同的同号消息"；③ 消费组位点通常已经提交到截断点之后（消费者已"越过"那段从未存在的区间），即便数据回来也不会再投一次。加上第四条常见暴击：`retention` 可能已经把那段时间的段删了。

预案四行：① **谁有权开** —— 业务 owner + 数据 owner 双签，签字时就要写"本次最多可丢 N 条 / 折 M 元"；② **开了之后必须同时启动什么** —— 上游重放脚本、对账窗口、下游停写/停归档开关（否则丢的部分会被下游当成"从未发生"直接归档，事后再也补不上）；③ **开多久自动关** —— 恢复即改回 `false` 并核对生效（broker 静态配置常需滚动重启，务必写进步骤）；④ **事后怎么量** —— `LogStartOffset`/HW 是否回退、副本 leader epoch 分叉点、消费位点 vs 上游自增序列，并事先承认"精确枚举丢的哪几条几乎不可能"。
</details>

**5. 面板上 `UnderRedubplicatedPartitions`（正确名为 `UnderReplicatedPartitions`）一直是 0，但消费者 lag 在涨。请列出至少五个可能原因，并说明为什么"没有 ISR 问题"不等于"复制链路健康"。**
<details><summary>答案</summary>

五个原因：① **LSO 被长事务钉住**（`read_committed` 消费者的可见上限不是 HW），此时复制完全健康、ISR 满员；② **`acks=1`**（或 `acks=all` 但 `min.insync=1`）—— HW 由最慢 ISR 决定，而最慢者仍在 ISR 内（尤其 `replica.lag.time.max.ms` 被调大时），`UnderReplicatedPartitions` 只统计"ISR 少于 RF 的分区数"，**不统计"ISR 内有人落后"**；③ **消费者侧问题**：`max.poll.records` 太大、处理逻辑变慢、下游 DB 抖动、rebalance 反复——lag 的定义里有一半是消费者；④ **写入速率突增**（上游批量导入、刷数），复制追得上但消费追不上，`LEO−HW` 正常而 `HW−CURRENT-OFFSET` 暴涨；⑤ **分区/副本布局问题**：某个分区的 leader 在弱网机柜，或单 broker 领了过多分区导致 fetcher 线程排队（`num.replica.fetchers` 默认量级 1），实际拉取周期被拉长；⑥ **监控口径错**：lag 用了 `LEO − CURRENT-OFFSET`，把"已写未可见"算成了"消费积压"。

为什么 0 不等于健康：`UnderReplicatedPartitions` 的**单位是分区个数、判据是"ISR 尺寸 < RF"**，它对三件事完全无感——(a) ISR 内的副本落后多少，(b) HW 与 LEO 的距离，(c) `min.insync.replicas` 的余量。所以必须配三个数一起看：每分区 `LEO − HW`、`IsrShrinksPerSec`（抖动速率而非瞬时值）、以及 `ISR 尺寸 − min.insync.replicas`（余量）。**只有把"余量"画出来，你才知道自己离"100% 拒写"还有几个副本的距离。**
</details>

---

## 29.14 延伸阅读（锚点式）

- 日志的物理形态：segment 滚动、`.index` / `.timeindex` 稀疏索引、`sendfile` 零拷贝、以及"为什么它不能删不能改"：`→ comp/28`
- 消费组、rebalance 协议、位点提交时机与积压（**lag 的系统性排查**、写并行度 = 分区数）：`→ comp/30`
- 消息族谱：RocketMQ / Pulsar / RabbitMQ / NATS 的语义形状差异（事务消息、存算分离、推 vs 拉），以及"什么时候这套复制形态不再是正确选择"：`→ comp/31`
- MySQL 复制的「半同步退化」：本篇文章 §29.6 对照表的完整版、切换时序与裂脑处置：[comp/10 主从复制与高可用](10-replication-and-ha.md) §10.4、§10.8
- 投递语义（至少一次 / 至多一次 / 恰好一次的真实边界）与幂等消费的三个落点：[eng/24 投递语义与重复消费](../eng/24-delivery-semantics.md)
- 消息可靠性八环节清单、生产端/消费端逐环打补丁、重试与死信：[eng/26 消息可靠性](../eng/26-message-reliability.md)
- 消息存储的整体形态决策（什么时候该换成日志型、队列型、流式存算分离）：[architect/15 消息存储](../architect/15-message-store.md)
- 组件故障谱：本篇三类事故在"慢 / 满 / 脑裂 / 假活 / 语义漂移"里的归类与兜底招式：`→ comp/52`
- 本套总纲：解剖卡八问、三条纪律（版本常量 / 指标前提 / 量级结论 vs 承诺值）与反冗余公约：[0-summary.md](0-summary.md)
