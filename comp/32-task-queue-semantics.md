# 32. 任务队列语义：SQS、Redis Streams、可见性超时与死信

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 消息、流与定时 ｜ **编号**：32 / 53
> **本篇定位**：全仓唯一一处把「**消息已经被取走、但还没做完**」这个中间状态拆到秒级的地方。日志长什么样归 `→ comp/28`，ISR / HW / acks 归 `→ comp/29`，投递语义的三个乘号、去重表与状态机归 [eng/24 投递语义](../eng/24-delivery-semantics.md)，可靠性八环节与 DLQ 表结构归 [eng/26 消息可靠性](../eng/26-message-reliability.md)，「该不该上 MQ」归 [architect/15 消息存储](../architect/15-message-store.md)。本篇只回答一个问题：**为什么没有任何一个队列能替你解决"被取走但没做完"，以及为什么唯一的完整答案是「可见性超时 + 幂等 + 死信」这三件互不替代的东西。**
> **读法**：救火直接读 §32.7 的「处理时长 vs 超时」表与 §32.12 坑清单；做选型读 §32.6 的五形态对照与 §32.11 边界表；被追问「SQS 到底保证什么」读 §32.4 的状态机与 §32.3 的三个崩溃点；要方案直接抄 §32.10 的三本账（全部带乘法链、假设清单与量级标记）。只想背结论读 §32.14。

---

## 32.1 引子：三起「消息被取走，但没做完」的事故

这类故障的统一形状是：**队列本身是健康的**。队列深度归零、没有积压、消费端没有报错、broker 的 CPU 20%。坏掉的是那个没人建模的状态——「消息已经不在队列里，也还没被删掉」。它不出现在任何一张默认面板上。

### 事故一：可见性超时用了默认 30 s，凌晨重复扣款 998 笔（2026-04-08）

某支付回调链路：上游支付网关把「支付成功」事件推到一个 standard 队列，24 个消费者（12 个 Pod × 2 个并发线程）处理：银行查证 → 本地记账 → 发通知。`visibility timeout` 用的是队列创建时的默认值 **30 s**，两年没人改过——因为「没人觉得它是个参数」。

处理时长的形状是这样的：P50 220 ms、P99 11 s，**但每天有 0.4% 的请求会撞上银行的夜间批处理窗口，单次耗时 40–95 s**。这批慢请求毫无规律，它们既不报错也不超时（客户端 read timeout 设的是 120 s），它们只是慢。

| 时刻 | 事件 | 可见消息数 | 在途（in flight） | 业务侧 |
| --- | --- | --- | --- | --- |
| 02:40 | 银行日终批处理开始，查证 P99 从 11 s 抬到 78 s | 0 | 常规 24 | 正常 |
| 02:41 | 首批处理时长 > 30 s 的消息租约到期，**重新可见** | 0 | 24 + 重投 | 正常（监控全绿） |
| 02:41–06:40 | 同一批消息被第二个消费者取走，**第一个消费者仍在跑**，两者都记账 | 0 | 峰值 210 | 队列深度始终 0 |
| 06:40 | 批处理窗口结束，处理时长回落到 11 s | 0 | 24 | 正常 |
| 08:15 | 财务对账发现「同一 trade_no 出现两条出款记录」 | 0 | — | 开始排查 |
| 08:40 | 统计到重复记账 1,050 笔，其中 998 笔已出款 | — | — | 冻结出款 |

**乘法链**：`窗口内消息量 2.1 万 × 超时比例 5% = 1,050 笔被重投 → 其中 998 笔第二次处理也完成 = 998 笔重复扣款`。金额 `998 × 平均 684 元 ≈ 68.3 万元`，退款与补偿工单 41 万元，对账 6 天。

**这一起事故真正的教训不是「30 s 太短」，而是「可见消息数」这个指标对这类故障完全免疫。** 消息从来没有积压过——它们只是**同时被两个消费者拿在手里**。想知道重复发生了多少次，唯一能查的是每条消息的 `ApproximateReceiveCount` 属性（> 1 就是重投过），以及 `NumberOfMessagesReceived` 与 `NumberOfMessagesSent` 的比值。这两个数都不在默认面板上。

### 事故二：Redis Streams 的 PEL 涨到 4,100 万条，写入被 `noeviction` 全线拒绝（2026-05-21）

某站内异步任务流（发券、推送、画像回填，平均每条 2 KB）跑在单实例 Redis 上：`maxmemory 26GB`、`maxmemory-policy noeviction`（业务方的原话是「这个实例不许丢数据」）。Stream 建的时候带了 `MAXLEN ~ 5000000`（约 10 GB），看上去已经有保护了；同实例上还挂着 9.8 GB 的会话类 key。

消费端代码长这样：`try { handle(m) } catch (e) { log.warn(...); continue; }`——**失败不 `XACK`，也不重试**。原意是「失败的以后再说」，实际效果是：每一条失败的消息永远留在 consumer 的 PEL（Pending Entries List）里。更糟的是 consumer 名用的是 Pod 名（含随机后缀），每次滚动发布就多出一个新 consumer，PEL 按名字碎片化，**旧的 PEL 永远不会被任何人碰到**。

| 时刻 | 事件 | stream 长度 | PEL 条数 | 已用内存 | 写入 |
| --- | --- | --- | --- | --- | --- |
| 04-01 | 部署 3 个新 Pod，消费者名换成 `task-7f9c-x2` | 500 万（已顶到 MAXLEN） | 180 万 | 20.1 GB | 正常 |
| 04-18 | 下游画像服务上线新版本，5% 的写入报 500；失败不 ack | 500 万 | 1,240 万 | 21.7 GB | 正常 |
| 05-06 | PEL 3,000 万；`MAXLEN` 只裁 stream，不裁 PEL | 500 万 | 3,010 万 | 22.9 GB | 正常 |
| 05-19 | PEL 4,050 万 × 约 130 B（含分配器碎片）≈ 5.3 GB；另有 89 个死 consumer 的元数据 | 500 万 | 4,050 万 | 24.8 GB | 正常 |
| 05-21 09:12 | 内存跨过 26 GB，`noeviction` 生效，所有写命令返回 `OOM command not allowed when used memory > 'maxmemory'` | 500 万 | 4,100 万 | **26.0 GB** | **全部失败** |
| 05-21 09:13–10:40 | 上游 `XADD` 全失败，应用侧 41 万条任务只进了本地日志 | — | — | — | 失败 |
| 10:40 | 临时 `XTRIM`+`XGROUP DELCONSUMER` 清掉死 consumer，内存回到 14 GB | 500 万 | 40 万 | 14.1 GB | 恢复 |

**这里有两个叠在一起的形态事实**：① **PEL 是独立于 stream 的第二份存储**，`MAXLEN` 裁剪不会动它——设了 `MAXLEN` 就以为「内存有上限」是错的；② **PEL 的归属是 consumer 名**，而 consumer 名在容器化部署里默认是随机的，于是「谁该来认领这些积压」在物理上就没人。

### 事故三：死信队列里躺了 204 万条，三天后才发现，差一点被 retention 清掉（2026-06-02）

某营销发券链路用 `standard` 队列 + DLQ，`maxReceiveCount=3`，重试语义配得很规范。05-30 下游契约改了：`amount` 从字符串改成对象（`{"value": 100, "currency": "CNY"}`），消费端反序列化直接抛异常。重试三次后进 DLQ——**这正是它该做的**。

问题在于：**DLQ 深度没有告警，也没有消费者**。它就那样安静地涨。

| 时刻 | 事件 | 主队列深度 | DLQ 深度 | 谁在看 |
| --- | --- | --- | --- | --- |
| 05-30 14:00 | 下游契约变更上线（同一个发布窗口） | 0 | 0 | 无人 |
| 05-30 14:20 | 首条消息反序列化失败，重试 3 次 | 3 | 1 | 无人 |
| 05-31 全天 | 日均 68 万条任务全部失败 | 3 | 68 万 | 无人 |
| 06-01 全天 | — | 3 | 136 万 | 无人 |
| 06-02 09:40 | 容量周会顺手 `get-queue-attributes` 才看到 **2,040,000** | 3 | **204 万** | 这才有人 |
| 06-02 10:20 | 查到 DLQ 的 `MessageRetentionPeriod` 是默认 **4 天** → 05-30 那批 1 天后就会被删 | — | — | 抢时间 |
| 06-02–06-05 | 修反序列化 + 重投；31 万条因上游操作日志只保留 48 小时而无法还原 | — | — | 人工补数 |

**代价**：发券延迟 3 天，31 万条无法还原的用户券走人工补发（客诉 1,900 单）。复盘时最扎人的一句话是：**「我们有死信队列，所以以为死信有归宿」。死信队列不消费，等于没有死信——它只是把「失败」从一条看不见的队列搬到了另一条看不见的队列。**

> 🩸 **三起事故的共同点**：三份代码都没有 bug，三个队列都在正常工作。① 缺的是「处理时长分布与租约时长的关系」（§32.7）；② 缺的是「PEL 是第二份存储、且归属 consumer」（§32.5）；③ 缺的是「DLQ 也是一个队列，它需要消费者与告警」（§32.9）。**队列卖给你的是一个租约，不是一个完成保证。**

---

## 32.2 一句话定位与唯一真问题

**定位**：任务队列 = **「一条消息在某一刻归某一个消费者独占，独占会过期」** 这件事的存储器。它的语义单位不是「记录在日志里的位置」（那是 `→ comp/28`），而是**一次交付的租约（lease）**。所有形态（SQS 的可见性超时、Redis Streams 的 PEL、Beanstalkd 的 TTR、RabbitMQ 的 unacked、数据库的 `locked_until`）都是同一句话的不同写法：**独占 + 到期 + 可被第二个人接手**。

### 三种消费确认模型（全篇的坐标系）

| 模型 | 代表 | 「什么时候别人能拿到」 | 崩溃后最坏情况 | 顺序 | 它把哪个责任交给了你 |
| --- | --- | --- | --- | --- | --- |
| **A. 处理完再 ack** | Kafka（offset commit）、Redis Streams（`XACK`）、RabbitMQ（`basic.ack`）、Beanstalkd（`delete`） | ack 到达时；若消费者不 ack 也不断开，就一直归它 | **消息卡住**（未 ack 且连接还在）；或会话超时后重投 | Kafka 分区内有序 | 你必须保证「一定会 ack 或一定会断开」 |
| **B. 可见性超时后自动重投** | SQS standard / FIFO | **租约到期那一刻，无需消费者同意** | **同一条消息被 N 个消费者同时处理**（N = 超时了几个窗口） | standard 无序；FIFO 按 group | 你必须保证「处理时长 < 租约」或「不断续租」 |
| **C. lease / claim + 续租** | SQS `ChangeMessageVisibility`、Beanstalkd `touch`、Streams `XCLAIM`/`XAUTOCLAIM`、DB `locked_until` | 心跳停下后最多一个租约期 | 处理超过租约且心跳已断 → 重复；心跳在 → 独占成立 | 由你的 key 决定 | 你必须实现心跳，并且**心跳周期 < 租约** |

**图 1 · 三种模型的时间轴：区别全在「崩溃之后多久，第二个人能拿到」**

```text
时间 ──────────────────────────────────────────────────────────────────────►

【A. 处理完再 ack】Kafka / RabbitMQ / Redis Streams / Beanstalkd(delete)
 t0 交付 ──► t1 处理中 ──► t2 处理完 ──► t3 ack
     │           │
     │           └ 崩溃点：t1–t2 之间 ⇒ 消息仍记在"你"名下，
     │             broker 要等会话超时/连接断开/其他消费者 claim 才知道你死了
     └ 不变量：ack 之前，这条消息对你是独占的（但"你"死了没人知道）

【B. 可见性超时后自动重投】SQS
 t0 ReceiveMessage（租约开始，默认 30 s）──► t1 处理中（不续租）──► t1+30 s 到期
                                                                    │
             消息重新可见 ◄──────────────────────────────────────────┘
             第二个人可以开始处理 —— 而第一个人**仍在跑，且不会收到任何通知**
    不变量：到期是"单方面判定"，不需要消费者同意，也不通知它
    ⇒ 同一时刻可能有 N 个消费者在处理同一条消息（N = 超时了几个窗口）

【C. lease / claim + 续租】SQS ChangeMessageVisibility / Beanstalkd touch /
                          Streams XCLAIM+XAUTOCLAIM / DB locked_until
 t0 claim（租约 T）──► 每 T/3 续租一次 ──► 心跳停
                          │                    │
                          │                    └ 最多 T 之后别人才能 claim
                          └ 处理时间可以远大于 T，但**必须有心跳**
    不变量：心跳在 ⇒ 独占成立；心跳断 ⇒ 独占在一个 T 内必然失效
```

**这张图唯一要记住的一句**：A、B、C 的差别不是「可靠不可靠」，而是**「谁来判断第一个消费者已经死了」**。A 靠会话超时（可能是分钟级），B 靠一个固定时钟（秒级，且从不问它），C 靠心跳（你自己定）——**三种判断都会误判**，而误判的两种方向分别叫「重复」和「卡死」。

### 唯一真问题

> **「被取走但没做完」这个状态没有任何队列能替你解决——可见性超时只解决「别人什么时候可以接手」，幂等只解决「接手后重复执行不出事」，死信只解决「彻底失败的那批去哪」。三件事互不替代，缺一件就有一类故障无法收敛。**

五条推论，全篇都在给它们找证据：

1. **队列承诺的是「至少一次」+「一个租约」，不是「做完了」。** 任何「队列保证任务被处理成功」的说法都是把租约误读成了完成回执。
2. **超时是单方面判定，所以「处理时长分布」是必须建模的一等公民。** 不问分布直接抄默认 30 s，等于默认 P99 < 30 s（§32.7 的算式）。
3. **幂等必须在「副作用」这一侧做，不能在「消息」这一侧做。** 消息的 ID 会变（重投、重试队列、DLQ 重放），业务键才稳定（→ [eng/24](../eng/24-delivery-semantics.md) §24.3）。
4. **死信的价值等于「有人消费它」× 「有告警」× 「有 replay 工具」。** 三个因子任一为 0，整体为 0（§32.9）。
5. **顺序、恰好一次、跨队列事务都不在任务队列的能力范围内**，它们要么靠单消费者 + 分区键，要么靠业务状态机，要么靠 outbox（§32.11）。

---

## 32.3 为什么 at-least-once 是默认：三个崩溃点与两种「恰好一次」

### 三个崩溃点：每一处都只有两个可选答案

| 崩溃点 | 现场 | 如果不重投 | 如果重投 | 队列实际怎么选 |
| --- | --- | --- | --- | --- |
| **① ack 之前崩溃** | 处理到一半进程 `kill -9` | 业务只做了一半，消息消失 → **丢** | 完整重做一遍 → **重复**（但业务状态是对的） | 一律重投（至少一次）。因为「做了一半」比「重复」更难收拾 |
| **② ack 丢失** | 处理完成、业务事务已提交，ack 在网络上丢了 | 消息永久滞留或过期被删 → **丢** | 再执行一遍 → **重复** | 一律重投。理由是：ack 丢失与「消费者根本没处理」在 broker 眼里**完全同形** |
| **③ 网络分区** | 消费者与队列之间断链，双方都活着 | 分区期间的消息没人处理 → **停摆** | 分区恢复后两边都可能重投 → **重复** | 一律重投 + 靠租约到期（B 模型）或会话超时（A 模型）解锁 |

**图 2 · ack 丢失：两种相反的解释，队列无法区分**

```text
消费者 ──处理完成──► 发 ack ──╳ 网络/进程在这里断了 ──► broker

        broker 侧只有两种可能，而它们**在物理上不可区分**：
        ① 没收到 ack  ⇒ 消息未完成 ⇒ 若不重投 = 丢
        ② 收到了 ack，只是回程的响应丢了 ⇒ 消息已完成 ⇒ 若重投 = 重复

        ┌────────────────────────────────────────────────────┐
        │ 选"重投"    ⟹ 至少一次（at-least-once）：不丢，但会重 │
        │ 选"不重投"  ⟹ 至多一次（at-most-once）：不重，但会丢 │
        │ 两个都要    ⟹ 队列做不到；只能在"副作用"侧去重        │
        └────────────────────────────────────────────────────┘
        ⇒ 这是两将军问题的队列版本，任何配置都消不掉它
```

这就是为什么**「恰好一次」在任务队列上永远是一个复合词**：至少一次投递 × 幂等执行 = 效果恰好一次。三个乘号缺一个就是 0（→ [eng/24](../eng/24-delivery-semantics.md) §24.2）。

### 两种「恰好一次」的实现，以及各自的边界

| 实现路线 | 机制 | 覆盖范围 | 边界（在这之外立刻退化） |
| --- | --- | --- | --- |
| **事务 / 两阶段** | 队列提供事务：`consume → process → produce` 在一个原子单元里提交，位点与输出一起生效（典型：Kafka 事务 + `read_committed`；SQS **不提供**；Redis Streams **不提供**） | 只在**队列内部**：读位点、写输出 topic 的原子性 | 你的 `UPDATE account` 不在事务里 → 回滚不了；跨系统要 XA/AT（→ `→ comp/43`） |
| **幂等 / 去重** | 业务侧记住「这件事做过了」：去重表、唯一索引、状态机 CAS、outbox | 覆盖到**真实副作用**（DB 写、支付、发券） | 需要「稳定的业务键」+「去重记录的保留期 ≥ 重投窗口」；键不稳定（如用消息 ID）就失效 |

**任务队列的现实是：所有主流任务队列（SQS、Redis Streams、Beanstalkd）都只给你第二种路线。** 第一种路线是 Kafka 那种「日志 + 位点」形态的特权（`→ comp/28`、`→ comp/29`）；队列形态天生没有「位点」这个全局提交点——每条消息的进度是各自独立的，没法把 10 条消息的 ack 合成一个原子提交。

**所以任务队列的工程契约只有一句话**：

```
队列承诺 = 至少一次投递 × 一个会过期的租约 × 一个「重试用尽后可以送去别处」的出口
业务必须补 = 幂等执行 × 处理时长 < 租约（或心跳续租） × 死信的消费者与告警
```

> 一句话把 §32.3 钉住：**「不丢」和「不重」不能同时免费拿到，因为它们的判据是同一个信号（ack 是否到达），而那个信号会丢。** 你能选的只是把损失放在哪一侧——而放在「重复」那一侧的代价，正好可以用幂等键买回来。

---

## 32.4 SQS：可见性超时是「一个会自己到期的独占」

SQS 的形态极简，值得单独讲，因为它是**唯一一个把 B 模型（超时自动重投）做成唯一语义**的主流队列。它没有 consumer、没有 PEL、没有 offset、没有连接状态——**服务端只记两样东西：每条消息的「下一次可见时刻」和「被收了几次」**。

**图 3 · 一条 SQS 消息的全部状态**

```text
                     SendMessage
                          │
                          ▼
          ┌─────────── 可见 (Visible) ───────────┐◄─────────────────┐
          │  ApproximateNumberOfMessagesVisible  │                  │
          └───────────────┬──────────────────────┘                  │
        ReceiveMessage    │（租约开始，默认 30 s，最长 12 h）         │
                          ▼                                        │
          ┌───── 在途 / 不可见 (In flight) ──────┐                  │
          │ ★ 服务端只记两个数：                  │                  │
          │   ApproximateReceiveCount（收了几次） │                  │
          │   一个到期时刻（不是"谁在跑"）        │                  │
          │ ★ 没有 consumer 身份，没有续租者名单   │                  │
          └──┬─────────────────┬─────────────────┬┘                  │
   DeleteMessage│     租约到期   │ ChangeMessageVisibility（续租）    │
             ▼                ▼                  │                   │
       消失（完成）      重新可见 ────────────────┼──────────────────┘
                            │                   └─► 仍在途，到期时刻后移
                            │ 第 N 次 receive 仍没被 Delete
                            ▼
                 RedrivePolicy: maxReceiveCount 用尽
                            ▼
                 ┌──── 死信队列 DLQ ────┐ ← 它也只是"另一个队列"：
                 └──────────────────────┘    没有消费者 = 没有死信
```

### 参数表：每个数字改的是哪条物理路径

| 参数 | 量级（**以目标版本文档为准**） | 它改的是哪条路径 | 拧错的现场 |
| --- | --- | --- | --- |
| `VisibilityTimeout` | 默认 **30 s**，范围 0–**43,200 s（12 h）** | 「多久之后别人可以接手」——B 模型的全部 | 太短 → 重复率随处理 P99 暴涨（§32.7）；太长 → 失败任务占着不放，重试延迟以小时计 |
| `MaxNumberOfMessages` | 1–**10** | 一次 `ReceiveMessage` 拿走几条 | 拿 10 条但**顺序处理** → 后面 9 条的租约在排队中被白白耗掉（最常见的隐形重复源） |
| `WaitTimeSeconds` | 0–**20 s**（长轮询） | 空队列时请求挂多久再返回 | 设 0 → 空轮询风暴，`NumberOfEmptyReceives` 暴涨，白付请求费 |
| `MessageRetentionPeriod` | 默认 **4 天**，范围 60 s–**14 天** | 消息在队列里最多活多久（从 `SentTimestamp` 算，**不是从收到算**） | DLQ 也用默认 4 天 → 事故三：还没人看就自己消失 |
| `DelaySeconds`（队列级 / 每条） | 0–**900 s（15 min）** | 消息发出后先不可见多久 | 想拿它做「延迟 2 小时重试」→ 做不到，要用重试队列或时间轮（`→ comp/36`） |
| `RedrivePolicy.maxReceiveCount` | 1–1,000（常见取值 3–5） | **收到第几次之后转 DLQ** | standard 队列上「receive 次数」≠「业务失败次数」（见下方坑） |
| `ReceiveRequestAttemptId`（FIFO） | 5 分钟窗口 | 让「重试一次 receive」不重复交付 | 只有 FIFO 有；standard 没有这个保护 |
| `MessageDeduplicationId`（FIFO） | **去重窗口 5 分钟** | 5 分钟内同 ID 的消息只入队一条 | 把它当业务幂等用 → 5 分钟之后的重投照样进队 |

### standard vs FIFO：差别不是"快慢"，是「顺序的代价」

| 维度 | Standard | FIFO |
| --- | --- | --- |
| 投递语义 | **至少一次**（可能重复） | 至少一次 + **同 group 内不重复并发** |
| 顺序 | **尽力而为**（无保证，可能乱序） | **MessageGroupId 内严格有序**，group 之间并发 |
| 吞吐 | 近乎无限（按 API 调用配额） | 受限：单队列默认约 300 msg/s 量级、批量可到约 3,000 msg/s 量级；高吞吐模式下上限按 message group 与区域差异极大 —— **具体数字以目标版本文档为准** |
| 去重 | 无 | `MessageDeduplicationId` 或 content-based dedup，**5 分钟**窗口 |
| 必需字段 | 无 | `MessageGroupId`（不填直接拒绝） |
| 它把什么交给你 | 顺序（你自己没法补） | 吞吐（group 数 = 并行度上限，热门 group 会成瓶颈） |
| 什么时候必须选它 | 任务之间无因果关系（发通知、刷缓存、异步扣减有幂等） | 同一实体的事件必须保序（同一笔订单的支付/退款、同一设备的指令） |

**这张表最容易被误读的一行是「FIFO 有序」**：FIFO 只保证**同一个 `MessageGroupId` 内**有序，而且**同一 group 内同时只有一条消息在途**（否则无法保序）。所以 FIFO 的并行度 = 活跃 group 数。把 `MessageGroupId` 设成用户 ID，而系统里 80% 的流量来自 5 个超级大客户 —— 这 5 个 group 会把队列的吞吐钉死在那 5 条串行链上，而队列整体「看起来远远没到上限」。

**standard 队列上的一个隐形坑**：SQS **不保证「同一个消费者处理期间，别的消费者拿不到同一条消息」的时长等于你的处理时长**——它只保证等于 `VisibilityTimeout`。于是 standard 上「receive 次数」这个计数也会被**纯粹的慢**（而非失败）推高，`maxReceiveCount=3` 可能在业务一次都没失败的情况下把消息送进 DLQ。事故一的 998 笔重复就是这样发生的，而 DLQ 里没有一条——因为重投的两次都"成功"了。

**SQS 消费者骨架（Python / boto3：心跳续租 + 幂等键 + 失败分类）**

```python
import json, threading, boto3
from botocore.config import Config

sqs = boto3.client("sqs", config=Config(retries={"max_attempts": 5, "mode": "standard"}))
QUEUE = "https://sqs.<region>.amazonaws.com/<acct>/pay-callback"
LEASE = 30                      # = VisibilityTimeout，续租周期必须 << LEASE
IDEM_TTL_DAYS = 7               # 幂等记录保留期 ≥ 最大重投窗口

def heartbeat(receipt, stop_evt):
    while not stop_evt.wait(LEASE / 3):          # 每 10 s 续一次
        sqs.change_message_visibility(QueueUrl=QUEUE, ReceiptHandle=receipt,
                                      VisibilityTimeout=LEASE)

def handle(body: dict) -> str:
    # 幂等：业务键来自 payload，不来自 MessageId（重投会换 MessageId / 换队列）
    key = f"paycb:{body['trade_no']}"
    if not dedup.claim(key, ttl_days=IDEM_TTL_DAYS):   # INSERT ... ON CONFLICT DO NOTHING
        return "DUPLICATE"                              # 已处理过 → 直接删消息
    try:
        bank_verify(body)                               # 慢的那一步（P99 可达 78 s）
        ledger.post(body)                               # 与上一步不在同一事务 → 幂等键兜住
        return "OK"
    except TransientError:
        dedup.release(key)                               # 放行重试，但保留计数
        raise                                            # 不 Delete → 等租约到期重投

def run():
    while True:
        resp = sqs.receive_message(QueueUrl=QUEUE, MaxNumberOfMessages=1,  # 慢任务别批量拿
                                   WaitTimeSeconds=20, AttributeNames=["ApproximateReceiveCount"])
        for m in resp.get("Messages", []):
            stop = threading.Event()
            threading.Thread(target=heartbeat, args=(m["ReceiptHandle"], stop), daemon=True).start()
            try:
                handle(json.loads(m["Body"]))
                sqs.delete_message(QueueUrl=QUEUE, ReceiptHandle=m["ReceiptHandle"])  # 只有成功才删
            except TransientError:
                pass                     # 不删：让租约到期重投（此时心跳线程仍在续租，需先 stop）
            finally:
                stop.set()               # ★ 顺序很重要：先停心跳，再让租约自然到期
```

**这段骨架上每一行都对应一个事故**：`MaxNumberOfMessages=1` 对应「批量拿、串行处理，后面的租约被排队耗掉」；`heartbeat` 对应「处理 78 s 而租约 30 s」；`change_message_visibility` 续租的上限是**从收到那一刻算起累计 12 h**，超过就必须重新 receive；`stop.set()` 放在 `finally` 里，是为了「失败后让租约尽快到期」——如果忘了停心跳，失败的消息会被你自己的心跳**续到 12 h 之后才重投**。

---

## 32.5 Redis Streams：PEL 是第二份存储，而且它归属 consumer

Redis Streams 是**唯一一个把「谁在处理哪条」显式存成数据结构的队列**：consumer group + PEL（Pending Entries List）。这带来 SQS 没有的能力（可以精确看到 pending 了哪些 ID、可以被别的消费者接手、可以按 `delivery_count` 自建重试），也带来 SQS 没有的故障（PEL 是独立存储，会自己涨；归属是 consumer 名，容器化下会碎片化）。

**图 4 · stream、group、PEL 三者的关系**

```text
 XADD ──► stream（按 ID 排序的追加日志，可 MAXLEN/MINID 裁剪）
             │
             │  XREADGROUP GROUP g c1 COUNT 10 STREAMS s >
             ▼
      ┌────────────────────────────────────────────────┐
      │  group "g" 的 last-delivered-id ───────────────┼──► 决定 ">" 从哪开始
      └────────────────────────────────────────────────┘
             │
             ├──► c1 的 PEL：每条 = (ID, delivery_time, delivery_count)
             ├──► c2 的 PEL
             └──► c3 的 PEL（Pod 名带随机后缀 ⇒ 每次发布多一个 PEL）
                     │
   XACK 才从 PEL 删除 ◄┘      ★ PEL 与 stream 是**两套存储**：
                                · MAXLEN 裁剪不缩 PEL（PEL 条目可能指向已被裁掉的 ID）
                                · 消费者死后没人 XAUTOCLAIM ⇒ PEL 永久挂在它名下
                                · PEL 每条约 60–100 B（ID 16 B + 索引/元数据开销）
```

### 命令表：每个命令改的是 PEL 的哪一格

| 命令 | 做什么 | 它改了 PEL 的什么 | 拧错的现场 |
| --- | --- | --- | --- |
| `XADD key [MAXLEN ~ N] * f v` | 追加一条，返回 ID（`ms-seq`） | 不改 PEL；`MAXLEN` 只裁 stream | 以为设了 `MAXLEN` 就有内存上限 → 事故二 |
| `XREADGROUP GROUP g c COUNT n BLOCK ms STREAMS key >` | 领新消息，**自动写入 c 的 PEL** | 新增 pending + `delivery_count=1` | 一次 `COUNT 100` 但串行处理 → 后 99 条的 `delivery_time` 早早开始计时 |
| `XREADGROUP ... STREAMS key 0` | 重读**自己 PEL 里**的条目 | 不改 PEL | 用 `>` 想重读 → 读不到，以为消息丢了 |
| `XACK key g id` | 标记完成，从 PEL 删除 | 删除该条 | 异常路径忘了 ack → 无界增长（事故二） |
| `XPENDING key g [start end count] [consumer]` | 查 pending 摘要/明细（含 `idle` 与 `delivery_count`） | 只读 | 不监控它 → 唯一的堆积信号丢失 |
| `XCLAIM key g c2 min-idle id` | 把某条 pending 改判给 c2 并重置 idle | 改归属 + 重置 `delivery_time` | 手动 claim 忘记重置就反复 claim → `delivery_count` 虚高 |
| `XAUTOCLAIM key g c2 min-idle start COUNT n`（Redis **6.2+**） | 批量认领 idle 超阈值的 pending（含**已被 trim 掉的条目**，从 PEL 清除并在返回值里报 deleted id） | 批量改归属 / 清幽灵 | 阈值设太小 → 把正常在跑的活抢走，制造重复 |
| `XTRIM key MAXLEN ~ N` / `MINID ~ id` | 裁剪 stream 本体 | **不检查 PEL** | 裁掉了还没 ack 的条目 → pending 变幽灵（`XCLAIM` 拿不到内容，只剩 ID） |
| `XINFO GROUPS / CONSUMERS`（7.x 起 `XINFO GROUPS` 带 `lag`） | 看 `pending`、`idle`、`lag` | 只读 | 不采集 → 不知道哪个 consumer 死了 |

### 持久化：`已 ack 但未落盘` 与 `已落盘但 ack 丢了` 是两个方向

Streams 的 `XADD`、`XACK`、PEL 变更**都走 AOF/RDB**，所以它比 SQS 多一个维度：**持久化丢的窗口多大，重复与丢失就各有一次机会**。

| 配置 | 丢的窗口 | 重启后会发生什么 | 折成哪种故障 |
| --- | --- | --- | --- |
| AOF `appendfsync everysec`（常见默认） | **最多约 1 s** 的写入 | 最后 1 s 内的 `XADD` **丢失**；最后 1 s 内的 `XACK` **丢失** ⇒ 那些消息重新回到 PEL（`delivery_count` 不减） | 前者=丢，后者=**重复**（业务已做完，又一次被投） |
| AOF `appendfsync always` | 约等于 0（每条一次 fsync） | 两者都几乎不丢 | 吞吐掉一个数量级（fsync 进写路径，→ `→ comp/14`） |
| RDB 快照（`save` 周期） | **分钟级** | 快照点之后的所有 `XADD` 与 `XACK` 全部回退 | 丢 + 大量重复同时发生 |
| 主从 + `WAIT` | 取决于 `WAIT` 的副本数确认 | 主库挂了切到从库，未同步的 `XACK` 回退 → 重复 | 重复（不会丢，但会重） |

**这就是「at-least-once 的持久化版本」**：只要 ack 不是原子的、同步落盘的，重启就会把「已 ack」退回到「未 ack」，于是业务再做一遍。**Redis 的 `appendfsync everysec` 把重复窗口钉在 1 s 量级，这个数字就是这种选型下「幂等记录必须覆盖的时间跨度」的下限**（真实要求通常更大，因为还有重投与 DLQ 重放）。

**Go 消费者骨架（`XREADGROUP` + `XAUTOCLAIM` + 失败计数转死信）**

```go
func consume(ctx context.Context, rdb *redis.Client, consumer string) {
	const group, stream, dead = "dispatch", "task:dispatch", "task:dead"
	claimIdle := 60 * time.Second            // > 处理 P99，< 重试容忍时长
	for {
		// ① 先认领别人（或死掉的自己）留下的 pending —— 这是 Streams 版的"可见性超时"
		if ids, err := rdb.XAutoClaim(ctx, &redis.XAutoClaimArgs{
			Stream: stream, Group: group, Consumer: consumer,
			MinIdle: claimIdle, Start: "0-0", Count: 20,
		}).Result(); err == nil && len(ids) > 0 {
			for _, m := range ids { process(ctx, rdb, group, stream, dead, m) }
			continue                          // 认领优先：先把历史欠账做完，再领新活
		}
		// ② 再领新消息
		res, err := rdb.XReadGroup(ctx, &redis.XReadGroupArgs{
			Group: group, Consumer: consumer, Streams: []string{stream, ">"},
			Count: 10, Block: 5 * time.Second,
		}).Result()
		if err != nil || len(res) == 0 { continue }
		for _, m := range res[0].Messages { process(ctx, rdb, group, stream, dead, m) }
	}
}

func process(ctx context.Context, rdb *redis.Client, group, stream, dead string, m redis.XMessage) {
	key := m.Values["task_key"].(string)       // 幂等键：业务键，不是 stream ID
	if !dedup.Claim(key) {                     // 已做过 → 只补 ack，不重做
		rdb.XAck(ctx, stream, group, m.ID); return
	}
	if err := doWork(ctx, m); err != nil {
		n := bumpDeliveryCount(ctx, rdb, group, m)      // XPENDING 里读 delivery_count
		if n >= 5 {                                     // ★ 自建死信：Streams 没有内置 DLQ
			rdb.XAdd(ctx, &redis.XAddArgs{Stream: dead, Values: map[string]any{
				"id": m.ID, "key": key, "err": err.Error(), "count": n}})
			rdb.XAck(ctx, stream, group, m.ID)          // 从 PEL 摘掉，避免无界增长
			return
		}
		return                                          // 不 ack：让它留在 PEL 等被 XAUTOCLAIM 接手
	}
	rdb.XAck(ctx, stream, group, m.ID)                  // ★ 成功才 ack
}
```

**这一段里有两个必须自己写的东西，SQS 送了、Streams 不送**：① **死信**——`task:dead` 是我们自己 `XADD` 出来的；② **重试上限**——`delivery_count` 只能从 `XPENDING` 读，判等逻辑在客户端。这也解释了为什么很多团队在 Streams 上「重试了 200 次」：`delivery_count` 只是一个计数器，**没有任何人替你比较它**。

**值班第一屏（bash）：PEL 与碎片化**

```bash
#!/usr/bin/env bash
R="redis-cli -h redis-1 --no-raw"
S=task:dispatch; G=dispatch
$R XLEN $S                                   # stream 本体长度（受 MAXLEN 控制）
$R XINFO STREAM $S | grep -E 'entries-added|max-deleted-entry-id'
$R XINFO GROUPS $S                           # 每组的 pending / lag / consumers（7.x 起带 lag）
$R XPENDING $S $G                            # 摘要：总数 + 最小/最大 ID + 每消费者分布
for c in $($R XINFO CONSUMERS $S $G | grep -E '^name' | awk '{print $2}'); do
  # 逐个 consumer 看 pending 与 idle：idle 很大 + pending 很多 = 死 consumer，PEL 无人认领
  echo "--- $c"; $R XPENDING $S $G - + 5 $c
done
$R MEMORY USAGE $S                           # stream 本体字节（不含 PEL）
$R INFO memory | grep -E 'used_memory_human|maxmemory_policy'
# 判读：XINFO GROUPS 的 pending 万级 + 多个 consumer 的 idle 数小时 ⇒ 事故二的前夜
```

**SQS 与 Streams 的形态差异一句话**：SQS 把「在途」记在**服务端的一个到期时刻**里（你查不到明细，只能等），Streams 把「在途」记在**客户端可见的一个 ID 列表**里（你能查到明细，但也必须自己维护它）。前者省事、有隐藏风险；后者可控、有显性负债。

---

## 32.6 其他四种形态对照：同一件事的五种写法

任务队列的谱系里，「在途状态存在哪」这一个问题就能把主流实现分成五类。它们不是「先进/落后」的关系，而是**把「谁来判断第一个消费者死了」这个决定，交给了不同的东西**。

| 维度 | SQS | Redis Streams | Beanstalkd | RabbitMQ | Kafka（offset 当 ack） | DB 队列（`SKIP LOCKED`） |
| --- | --- | --- | --- | --- | --- | --- |
| **在途状态存哪** | 服务端每条消息的一个到期时刻 | 客户端可见的 PEL（ID 列表） | 服务端 job 状态的 `reserved` 标记 | 服务端 channel 的 unacked 集合（delivery tag） | **不存在**：只有「组位点」这一个全局数字 | 你自己表里的一行（`status` + `locked_until`） |
| **谁判定消费者死了** | 时钟（到期就重投，不问） | 没人自动判；靠 `XAUTOCLAIM` 的 idle 阈值 | 时钟（TTR 到期自动 release） | 连接断开（TCP/心跳）才算，否则 unacked 一直挂着 | 组协调者（rebalance 超时 `max.poll.interval.ms`） | 没人判；`locked_until` 过期后别人能抢 |
| **重投延迟** | 一个 visibility timeout（默认 30 s） | 一个 `min-idle-time`（由你定） | 一个 TTR（默认量级 60 s） | 秒级（连接断）/ 无限（连接活着但不 ack） | 一次 rebalance（秒到分钟） | 一个 `locked_until`（由你定） |
| **能精确重投单条吗** | 不能（只能等它到期） | **能**：`XCLAIM` 指定 ID | 不能（只按 TTR） | 能：`basic.nack(requeue=true)` | 不能（只能回退整个分区位点） | 能（改一行状态） |
| **顺序** | standard 无 / FIFO 按 group | stream 内有序，group 内按 `>` 推进 | 无（优先级队列） | 队列内近似 FIFO（单消费者时成立） | **分区内严格有序** | 由 `ORDER BY` + 单消费者决定 |
| **内置死信** | 有（`RedrivePolicy` → DLQ） | **无**（自己 `XADD` 另一个 key） | 有 `bury` 状态（但不会自动转，要 `kick`） | 有（DLX + `x-death` 头） | **无**（自己写重试 topic） | 无（加一列 `status=DEAD`） |
| **积压在哪** | 服务端磁盘（无限，按 retention 过期） | **内存**（stream + PEL 都在内存，受 `maxmemory` 管） | 内存 + binlog 文件 | 内存（unacked 计入内存水位，超限才 page out） | 磁盘（这是它最大的优势，→ comp/28） | 磁盘（表里） |
| **单队列吞吐量级** | 千–万 msg/s（API 配额） | 万–十万 msg/s（单实例，单线程） | 万 msg/s（单实例） | 万 msg/s（经典队列，受 prefetch 影响大） | 十万–百万 msg/s（分区并行） | 千–几千 ops/s（每行一次事务） |

### 逐个说清它们的「那一刀」

**① Beanstalkd：`reserve` / `delete` / `release` / `bury` 四件套，是 C 模型的最小实现。**

`reserve` 拿到一个 job 并开始计 TTR（time-to-run，默认量级 60 s）；`touch` 续租；`delete` 完成；`release` 立刻放回并可按优先级重排；`bury` = 雪藏（**不会自动重试，也不会自动进 DLQ，必须有人 `kick` 或 `peek-buried` 捞出来**）。它的教训在第 4 个动词：**`bury` 是「暂停」不是「失败」**，很多团队 bury 了 200 万个任务然后再也没 `kick` 过——这和事故三同形，只是换了一个词。

**② RabbitMQ：`ack` / `nack(requeue)` / `prefetch` 三件套，是 A 模型的最强版本。**

unacked 消息挂在 channel 上，**连接不断就永远不重投**——所以它的「消费者死了」判据是**连接存活**，而不是处理时长。这带来两个特征：处理时间可以无限长（不怕超时），但**进程 hang 住而不退出时，消息永远卡在 unacked**（这是 RabbitMQ 最常见的「队列不积压但业务不动」）。`prefetch`（`basic.qos`）决定一个 channel 上最多挂多少条 unacked：prefetch 越大吞吐越高，但**内存 × 单条大小**全部记在 broker 侧，且**失败时一次 requeue 会把整批重投**（→ 事故一的放大版）。死信走 DLX，且 `x-death` 头里会累积每次死信的原因与次数——这是五种形态里**唯一自带「失败履历」的**。

**③ Kafka：offset commit 当 ack，是「批量语义」而不是「逐条语义」。**

Kafka 没有「单条在途」的概念：**位点是一个数字，提交位点 100 意味着 1–100 全部算完成**。于是它天然做不到 SQS 那种「单条到期重投」，也天然不需要 PEL——代价是**一条卡住，整个分区卡住**；而 rebalance 一旦发生（`max.poll.interval.ms` 超时、Pod 重启、扩缩容），**已处理未提交的那一段会整体重投**。这就是「任务队列 vs 日志」的分界：**日志把「你在哪」记成一个位点，队列把「这条归谁」记成一条记录。** 前者顺序强、吞吐高、单条不可控；后者单条可控、顺序弱。`→ comp/28`、`→ comp/29`。

**④ 数据库队列：`FOR UPDATE SKIP LOCKED` 是「租约 = 事务」的写法。**

它把在途状态存成你自己表里的一行，把「独占」交给行锁，把「租约到期」交给事务回滚——**进程崩溃 → 连接断 → 事务回滚 → 行锁自动释放 → 别人立刻能抢**。这条性质比 SQS 好（秒级而非 30 s），比 RabbitMQ 好（不依赖连接存活判断），比 Streams 好（在途状态在磁盘上，不受 `maxmemory` 管）。代价是每取一条要一次事务，吞吐在千级 ops/s，且**长事务会把锁和 undo 一起拖住**（→ `→ comp/06`）。

```sql
-- 数据库队列：claim（租约 = 事务 + locked_until 兜住"事务开着但进程假死"）
-- 前提：MySQL 8.0+ / PostgreSQL 9.5+ 才有 SKIP LOCKED
CREATE TABLE task_queue (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  task_key     VARCHAR(128) NOT NULL COMMENT '幂等键：业务唯一，不是自增 id',
  payload      JSON         NOT NULL,
  status       TINYINT      NOT NULL DEFAULT 0 COMMENT '0待处理 1处理中 2完成 3死信',
  retry_count  INT          NOT NULL DEFAULT 0,
  next_run_at  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  locked_until DATETIME(3)  NULL COMMENT '租约：过期即可被别人抢',
  owner        VARCHAR(64)  NOT NULL DEFAULT '',
  last_error   VARCHAR(1000) NOT NULL DEFAULT '',
  PRIMARY KEY (id),
  UNIQUE KEY uk_task_key (task_key),                    -- ★ 幂等的最后一道防线
  KEY idx_due (status, next_run_at),                    -- ★ claim 的驱动力
  KEY idx_lease (status, locked_until)                   -- ★ 找回"假死"的任务
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- claim：SKIP LOCKED 让 N 个 worker 并发拉取而不互等（这是整张表的关键词）
START TRANSACTION;
SELECT id FROM task_queue
 WHERE status = 0 AND next_run_at <= NOW(3)
 ORDER BY next_run_at
 LIMIT 50
 FOR UPDATE SKIP LOCKED;
UPDATE task_queue
   SET status = 1, owner = ?, locked_until = DATE_ADD(NOW(3), INTERVAL 60 SECOND)
 WHERE id IN (...) AND status = 0;
COMMIT;   -- ★ 事务一提交，行锁就释放了；此后的独占只靠 locked_until

-- 失败：退避 + 计数；超限转死信（不要 DELETE，死信要能被 replay）
UPDATE task_queue
   SET status = IF(retry_count + 1 >= 5, 3, 0),
       retry_count = retry_count + 1,
       next_run_at = DATE_ADD(NOW(3), INTERVAL POW(2, retry_count) SECOND),
       locked_until = NULL, last_error = ?
 WHERE id = ?;

-- 假死回收：租约过期但状态还是"处理中"的（进程挂了但事务已提交过）
UPDATE task_queue SET status = 0, locked_until = NULL
 WHERE status = 1 AND locked_until < NOW(3);
```

**这张表上最容易被忽略的两格**：① `UNIQUE KEY uk_task_key`——数据库队列**天然**能做幂等，因为幂等键和队列在同一个库里，`INSERT` 冲突就是去重；而 SQS/Streams 的幂等必须去另一个存储做。② `idx_lease` + 最后那条 `UPDATE`——**没有它，`locked_until` 就只是一列装饰**：进程 `kill -9` 之后事务已提交，那 50 行会永远停在「处理中」。

### 决策表：按你的约束选，不按「哪个更好」

| 你的约束 | 选它 | 为什么 | 代价（必须写进方案） |
| --- | --- | --- | --- |
| 无运维、按量付费、任务之间无因果关系 | **SQS standard** | 没有在途状态要管，服务端无限堆积 | 30 s 租约要显式调；重复只能靠业务幂等 |
| 同一实体的事件必须保序 | **SQS FIFO** 或 **Kafka 单分区** | group 内串行 / 分区内有序 | 并行度 = group 数 / 分区数 |
| 已在 Redis 上，量在万级、能接受内存约束 | **Redis Streams** | PEL 可查、可精确 `XCLAIM`、无额外组件 | 死信/重试/trim 保护全要自己写；PEL 无界增长 |
| 需要「失败履历」和复杂路由（DLX、TTL、优先级） | **RabbitMQ** | `x-death`、DLX、per-message TTL 是内置的 | unacked 吃内存；prefetch 与 requeue 会放大重复 |
| 已经在写业务库，任务量 < 几千/s，要强一致 | **DB 队列** | 队列与业务同库同事务，幂等靠唯一索引 | 长事务、表膨胀、vacuum/undo 压力（→ comp/06、comp/11） |
| 吞吐优先、可重放的日志语义、单条失败能忍 | **Kafka** | 磁盘堆积、分区并行、多组独立重放 | 「单条卡住卡整个分区」；无内置死信（→ comp/28、comp/30） |
| 需要「延迟 2 小时后再试」 | **以上都不选**：延迟队列 / 时间轮 | 队列的延迟上限是分钟级（SQS 15 min） | `→ comp/36` |
| 需要「跨队列/跨库的原子」 | **outbox + 队列** | 队列不提供跨系统事务 | `→ comp/33`、[eng/10](../eng/10-outbox-and-transactional-message.md) |

---

## 32.7 可见性超时的算账：处理时长分布决定一切

### 为什么「默认 30 s」几乎总是错的

可见性超时这个参数只有一个正确取值，它的定义是：**大于「绝大多数任务的处理时长」，小于「你能容忍的失败重试延迟」**。这两个要求互相拉扯，而 30 s 这个默认值两边都不一定满足。

| 处理时长分布（真实形状） | `timeout=30 s` 的后果 | `timeout=300 s` 的后果 |
| --- | --- | --- |
| P50 200 ms、P99 11 s、**0.4% 超过 40 s**（事故一） | 0.4% 的任务被重投；重投后**大概率还在跑** → 并发处理 + 重复副作用 | 重复率降到 ~0，但失败任务的**重试延迟从 30 s 变成 5 min** |
| P50 3 s、P99 25 s、尾部 5% 到 90 s | 5% 重复 | 基本消除重复，但 `maxReceiveCount=3` 意味着**最坏 15 min 后才能进 DLQ** |
| P50 50 ms，但每 10 万条有 1 条跑 20 min（全表扫描类任务） | 那 1 条被重投 40 次，每次都在跑 → **叠加 40 个并发**，把下游打垮 | 2 次重投，仍然重复，但并发只有 2 |
| 处理时长与负载相关（越积压越慢） | **正反馈**：重投 → 并发上升 → 更慢 → 更多重投 | 缓解，但仍需续租 |

**算例：timeout 与重复率的乘法链**

```
设 P = P(单次处理时长 > timeout)，各次独立
每条消息的期望投递次数 E = 1 + P + P² + … = 1 / (1 − P)
重复率（多出来的比例） = E − 1 = P / (1 − P)

代入：
  P = 0.4%  → 重复率 0.40%    （日 12.6 万条 → 504 条/日）
  P = 5%    → 重复率 5.26%    （日 12.6 万条 → 6,632 条/日）
  P = 20%   → 重复率 25%      （日 12.6 万条 → 3.15 万条/日）
  P = 50%   → 重复率 100%     （重投风暴，队列自我放大）

再乘第二项——「重复里有副作用的比例」R（有 DB 写/外部调用的任务比例）：
  真实重复副作用数 = 消息量 × P/(1−P) × R
  事故一：12.6 万 × 0.40% × 4/24 小时窗口 ≈ 998 次重复记账（R ≈ 1）
```

> **量级结论（非承诺值）**。假设清单：① 各次处理时长相互独立（**这个假设在「下游变慢」时会破**，那时 P 会随重投上升，实际比 P/(1−P) 更差）；② 重投后第二个消费者的处理时长分布与第一个相同；③ `maxReceiveCount` 足够大，未被截断；④ 处理时长分布用线上 P99.9 与慢查询日志拟合，**不是估计**。**换假设即需重算**：若 P 是 20%（例如 timeout 设成了 5 s 而任务里有下载动作），重复率不是 20% 而是 25%，且队列会进入「重投 → 更慢 → 更多重投」的正反馈。

### 反向的账：timeout 太长的代价也能算出来

```
失败任务的重试延迟（从失败到第 N 次重试） = N × timeout
最坏进 DLQ 的时间                        = maxReceiveCount × timeout
"假死"任务占住并发的时间                  = timeout（进程挂了但没人知道）

例：timeout=30 s、maxReceiveCount=5
  → 失败任务第 5 次尝试在 150 s 后发生，进 DLQ 在 150 s 后
例：timeout=300 s、maxReceiveCount=5
  → 进 DLQ 在 25 min 后；若下游已恢复，这 25 min 里用户在等
例：timeout=12 h（SQS 上限）、maxReceiveCount=3
  → 一条被 handle 但进程假死的任务，占住"在途"12 h，
    而期间它不会被任何人看到 —— 队列深度 0，业务不动
```

**这就是「两头都能弄坏系统」的参数**（与 `replica.lag.time.max.ms` 同构，`→ comp/29` §29.6）：调小 → 重复率上升；调大 → 失败反馈变慢 + 假死占位变长。**没有一个默认值能同时最优，只有一个按「处理时长分布」算出来的值。**

### 三个工程手段，按代价排序

| 手段 | 怎么用 | 解决什么 | 代价 |
| --- | --- | --- | --- |
| **1. 设 timeout ≈ 处理 P99.9 × 2**（并定期重算） | 从慢查询日志/APM 拿 P99.9，取 2 倍；每季度复核 | 覆盖尾部，代价可控 | 分布会漂移（新功能上线），需要人守 |
| **2. 心跳续租**（C 模型） | 每 `timeout/3` 续一次，成功/失败都先停心跳再返回 | 处理时长可以远超 timeout（长任务唯一解） | 多一个定时器 + 一次 API 调用（SQS 计费）；心跳线程挂了会「假装还活着」 |
| **3. 按任务类型分队列** | 快任务（< 5 s）与慢任务（分钟级）分两个队列，各自设 timeout | 用一个参数同时满足两种分布——**做不到的事就别用一个队列** | 队列数量上升，运维与告警要跟着分 |
| **4.（补丁）`maxReceiveCount` 按「最长处理时长 / timeout」定** | 确保一条任务在 DLQ 之前至少被完整尝试过 2–3 次 | 防止「第一次就被慢动作送进 DLQ」 | 与重试延迟冲突，要业务签字 |

**批量取消息的独立陷阱**（SQS `MaxNumberOfMessages=10`、Redis `COUNT 100`、RabbitMQ `prefetch 250`）：**租约是从「收到」那一刻开始计的，不是从「开始处理」那一刻**。取 10 条、串行处理、每条 5 s = 第 10 条的租约只剩 `timeout − 45 s`。这是「明明 P99 只有 5 s，却仍然大量重投」的最常见原因，而它和 §32.7 的算例乘在一起：**P 不是 `P(处理 > timeout)`，而是 `P(排队时长 + 处理时长 > timeout)`**。

---

## 32.8 幂等键设计：四种落点与两种相反故障

幂等不是「加一个 if」，而是「**选一个稳定的键 + 选一个能原子判重的落点 + 定一个够长的保留期**」。三件事缺一件，幂等就只在演示环境有效。

### 键的选择：什么样的键在重投后不变

| 候选键 | 重投后还稳定吗 | 能用吗 |
| --- | --- | --- |
| `MessageId`（SQS）/ stream ID / offset | ❌ 重投可能换 MessageId（重试队列、DLQ 重放一定换） | 只能做「幂等探针」，不能做幂等键 |
| `ReceiptHandle`（SQS） | ❌ 每次 receive 都不同 | 只能用来 `DeleteMessage` |
| 业务单号（`trade_no`、`order_id`、`request_id`） | ✅ 生产端生成，重投不变 | **首选**（→ [eng/24](../eng/24-delivery-semantics.md) §24.3） |
| 业务键 + 动作 + 版本（`order_id:refund:v2`） | ✅ | 适合「同一单据多种动作」 |
| 业务键 + 时间窗口（`user_id:2026-06-02`） | ✅ 且自带过期语义 | 适合周期性任务（日报、对账） |

**判据只有一句：这个键在「生产者重发」「队列重投」「DLQ 重放」「人工补数」四种情况下会不会变？** 任何会变的键都不是幂等键。

### 四种落点：代价与适用面

| 落点 | 机制 | 能挡住什么 | 挡不住什么 | 成本 |
| --- | --- | --- | --- | --- |
| **去重表** | `UNIQUE(group, idem_key)` + 与业务写**同一本地事务** | 任意业务，最通用 | 与业务不同库时「去重行提交了、业务回滚」会留脏标记 | 一次额外写入 + 一张按保留期增长的表 |
| **唯一索引** | 业务表自身 `UNIQUE(trade_no)` | 最硬：数据库层面不可能重复 | 只适用于「一行一个业务事件」的形态 | 0（本来就有索引） |
| **状态机 CAS** | `UPDATE ... SET status='PAID' WHERE id=? AND status='PENDING'` 影响行数 = 0 即已做过 | 有状态流转的业务（订单、工单、审批） | 无状态业务（发通知、写日志） | 0 |
| **outbox / 结果表** | 「先写结果，再发消息」；消费方查结果表判断是否已做过 | 生产端与消费端的**双向**不确定性 | 需要额外一张表与清理任务 | 一次额外写入（→ [eng/10](../eng/10-outbox-and-transactional-message.md)） |

```sql
-- 落点一：去重表。★ 必须在同一个库里、同一个事务里，否则"去重行提交了业务回滚"会留下永久脏标记
CREATE TABLE consume_dedup (
  consumer_group VARCHAR(64)  NOT NULL,
  idem_key       VARCHAR(128) NOT NULL,
  src_queue      VARCHAR(128) NOT NULL,
  status         TINYINT      NOT NULL DEFAULT 1 COMMENT '1处理中 2已完成',
  created_at     DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (consumer_group, idem_key),          -- 一个复合主键就是全部幂等性
  KEY idx_created (created_at)                      -- 给清理任务用（没这条索引清理会全表扫）
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 与业务写在同一个事务里：INSERT 成功才执行业务；INSERT 冲突 = 已经做过
START TRANSACTION;
INSERT INTO consume_dedup (consumer_group, idem_key, src_queue) VALUES ('pay-cb', ?, 'pay-callback')
  ON DUPLICATE KEY UPDATE status = status;          -- 不报错，但影响行数 = 0
-- 应用侧判 affectedRows：0 ⇒ 已处理过，直接 COMMIT 并删消息
UPDATE account SET balance = balance - ? WHERE id = ?;   -- 真实副作用
UPDATE consume_dedup SET status = 2 WHERE consumer_group = 'pay-cb' AND idem_key = ?;
COMMIT;
```

**为什么「先 INSERT 去重行、再执行业务、再标完成」要在一个事务里**：如果分开，就存在「去重行已提交（标成处理中）→ 进程崩溃 → 业务没做」的窗口，此后所有重投都会被当成重复挡掉——**消息永久丢失，而且监控全绿**。这就是「幂等把至少一次变成了至多一次」的经典反噬（→ [eng/24](../eng/24-delivery-semantics.md) §24.6）。

### 两种相反故障，各自的兜底

| 故障 | 现场 | 后果 | 兜底 |
| --- | --- | --- | --- |
| **消费成功、ack 失败** | 业务已提交，`DeleteMessage` / `XACK` 超时或进程崩溃 | 消息重投 → **必须靠幂等挡**；重复率 = 这条路径的发生率 | 幂等键 + 去重记录保留期 ≥ 重投窗口（SQS 至少 4 天，含 DLQ 更长） |
| **ack 成功、消费失败** | 先 ack 后处理（或异步处理完才真正落库），ack 一提交进程就崩 | 消息消失、业务没做 → **没有任何机制能发现它** | ① 绝不先 ack；② 若必须异步，落本地任务表（ack = 入表成功）；③ 对账（→ `→ comp/14`） |

**第二行是全篇最危险的一行**，因为它**没有信号**：队列深度 0、无重复、无 DLQ、无错误日志——只有「少了一笔」。所有「消息丢了但查不出来」的故事，形态都是「某个环节把 ack 提前了」：`auto-ack` / `enable.auto.commit=true` / `XACK` 写在 try 的最前面 / 把消息 `Delete` 在业务提交之前。

```
一句话把 §32.8 钉住：
  幂等的三要素 = 稳定键 × 原子落点 × 保留期 ≥ 重投窗口
  保留期算错 ⇒ 幂等只在第一天有效（第 5 天重投的同一笔会被当成新任务）
```

---

## 32.9 死信与重试：死信不消费等于没有死信

死信的价值有三个因子，缺一个整体为 0：

```
死信的有效性 = 有人消费它 × 有告警盯着它的深度 × 有 replay 工具能把它送回去
```

事故三是 `0 × 0 × 1 = 0`：DLQ 建了（因子 3 勉强算有），但它没有消费者、没有告警，于是「204 万条失败」这个事实在最需要被看见的三天里完全不可见。

### 重试的三条纪律

| 纪律 | 内容 | 反例（都是真实形态） |
| --- | --- | --- |
| **① 按错误类型分流，不按次数一刀切** | 反序列化失败、字段越界、业务规则拒绝 = **永不可重试**（立刻进死信 + 通知 owner）；超时、连接拒绝、5xx = 可重试 | 「一律重试 16 次」：契约变更那类错误每条都跑满 16 次，把队列吞吐变成原来的 1/16（事故三的 3 次版本已经压掉 2/3） |
| **② 指数退避 + 抖动** | 第 n 次延迟 ≈ `base × 2^n × rand(0.8, 1.2)`；抖动是为了避免所有失败任务在同一秒一起回来（**同步重试风暴**） | 固定 5 s 重试：下游恢复的瞬间被打回同样压力，二次雪崩 |
| **③ 最大重试次数要能算出「最坏时长」** | 最坏进死信时间 ≈ `Σ 退避 + maxReceiveCount × timeout`；这个数必须小于「业务允许的最晚补偿时间」 | 退避 6 次累计 2.6 h + timeout 300 s × 3，实际 5 小时后才进死信，而券的有效期只有 2 小时 |

| 档位 | 延迟量级（示例） | 适用错误 | 它改变的是哪条路径 |
| --- | --- | --- | --- |
| 短档 | 1 s / 5 s / 30 s（±20% 抖动） | 网络抖动、下游 5xx、重启 | 把「瞬时故障」在几十秒内消化掉，不进死信 |
| 中档 | 5 min / 30 min | 依赖恢复中、数据待修 | 把「要人介入」的部分拉到工单时限内，此时应已通知 owner |
| 终止 | 超限 → 死信（`PENDING` + 工单） | 不可重试或重试耗尽 | 把不确定性变成一条可检索、可 replay 的记录 |

完整的分档退避表、`mq_retry_task` / `mq_dlq_message` 表结构与调度器实现见 [eng/26](../eng/26-message-reliability.md) §26.4，本篇不重写。**这里只留一条纪律：延迟值必须按「业务容忍的最晚补偿时间」反推——券 2 小时过期，就不该有 30 min 这一档。**

### DLQ 本身的四个必配项

| 配置项 | 必须怎么设 | 不设的后果 |
| --- | --- | --- |
| **DLQ 的 `MessageRetentionPeriod`** | **≥ 主队列的值**，且 ≥「人工发现的平均时长」。SQS 默认 4 天 | 事故三：05-30 的那批 1 天后就被删，永远补不回来 |
| **告警** | DLQ 深度 > 0 且持续 5 min 就 P2；`OldestMessageAge`（最早一条的年龄）> 1 h 就 P1 | 三天无人知（事故三） |
| **消费者** | 至少一个「只读 + 打日志 + 建工单」的消费者，把死信变成**可检索的记录**（落库/落工单系统） | DLQ 变成一个 4 天后自动清空的垃圾桶 |
| **replay 工具** | 一键把一批死信搬回主队列（SQS 有 redrive-to-source；其他形态要自己写），且**必须带审批与操作人记录** | 修复后不敢重放 → 手工补数（31 万条那次） |

**replay 的两个坑**（这两个坑和「幂等」直接相连）：① **重放会换消息 ID**——`MessageId`、`ReceiptHandle`、stream ID 全都变，只有业务键不变，所以**幂等键不能用消息 ID**（§32.8）；② **重放会重置重试计数**——`ApproximateReceiveCount` 归零，如果失败原因没修好，它会再跑满 `maxReceiveCount` 次再回来，**replay 变成一台放大器**。所以 replay 之前必须先修根因，并在工具里写死「同一批死信 24 小时内只允许 replay 一次」。

### 「失败率」与「死信率」不是一回事

| 量 | 定义 | 它回答什么 |
| --- | --- | --- |
| **失败率** | 单次尝试失败数 ÷ 尝试总数 | 「下游此刻健康吗」——会随重试放大（一次失败产生多次尝试） |
| **终态失败率** | 进死信的条数 ÷ 入队条数 | 「有多少业务真的没做成」——**这才是要给业务看的数** |
| **重投率** | 重投次数 ÷ 入队条数 | 「可见性超时是不是设错了」——§32.10 账一的现场读数 |

把这三个混成一个「错误率」，是「监控显示错误率 0.2%，但用户投诉一片」的常见原因：0.2% 是失败率（含被重试救回的部分），而终态失败率可能是 0.02% 或 2%——**取决于重试有没有用**。

---

## 32.10 三本账：重复率、并发吞吐、PEL 内存

三个算例，每个都给乘法链、假设清单与性质标记，最后附一个可复算脚本。

### 账一 · 重复率 ≈ P(处理时长 > timeout) × 重投次数

假设：任务量 12.6 万条/日（支付回调链路，事故一的现场）；处理时长 `T` 的分布如事故一（P50 220 ms、P99 11 s、0.4% 超 40 s）；重投后仍由同一个 API 处理（分布不变）；`maxReceiveCount` 足够大不截断。

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 单次超时概率 | 取 `timeout = 30 s`，从「40 s 尾部的比例 0.4%」与「P99 = 11 s」反推 | `P ≈ 0.4%` |
| 2 | 期望投递次数 | `E = 1/(1 − P)` = `1/0.996` | **1.004 次/条** |
| 3 | 重复率 | `E − 1 = P/(1−P)` | **0.402%** |
| 4 | 日重复条数 | `12.6 万 × 0.402%` | **≈ 506 条/日** |
| 5 | 折成夜间窗口（4 h） | `506 × 4/24` | ≈ 84 条/窗口 |
| 6 | 与事故一对账 | 实发 1,050 笔重投 / 998 笔重复 | 比第 5 步高一个量级 —— **因为窗口内 P 从 0.4% 涨到 5%**（银行批处理） |
| 7 | 若 `timeout` 调到 300 s | 尾部 > 300 s 的比例 ≈ 0.01% 量级 | 重复率降到 **0.01%**，代价是失败重试延迟 ×10 |

**第 6 步是这张表的重点**：`P` 不是常数，它随下游健康度变化。所以这个账必须按「**峰值窗口的 P**」算，而不是按日均。事故一的真实算式是 `2.1 万 × 5% = 1,050`。

> **量级结论（非承诺值）**。假设清单：① 各次处理相互独立（下游变慢时这条会破，实际比 `P/(1−P)` 更差）；② 处理时长分布用线上 APM 拟合；③ 不考虑「批量取消息导致排队时长计入租约」（加上后 P 会更大，见 §32.7 末）；④ 重投不改变任务本身的耗时。**换 timeout 只需重算第 1 步**：30 s → 300 s 使重复率降一个数量级以上，但把失败重试的最坏时长从 150 s 拉到 25 min。

### 账二 · 并发与吞吐 = 队列可见消息 ÷（处理时长 × 消费者并发）

真正的关系式是 **Little 定律**：`并发数 = 到达速率 × 平均处理时长`。任务队列上的三种问法都由它变形：

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 到达速率 | 峰值 300 msg/s（日 2,600 万条） | λ = 300 |
| 2 | 单条平均处理时长 | 含 3 次 DB 写 + 1 次外部 API | `E[T] = 1.5 s` |
| 3 | 需要的并发线程数 | `λ × E[T] = 300 × 1.5` | **450 线程** |
| 4 | 每个 Pod 的线程数 | 队列消费者模型，`MaxNumberOfMessages=1`，4 线程/Pod | 4 |
| 5 | 需要的 Pod 数 | `450 ÷ 4` | **113 Pod**（现实里按 HPA 上限只给了 40） |
| 6 | 实际吞吐 | `40 × 4 ÷ 1.5` | **107 msg/s** |
| 7 | 积压增长 | `300 − 107` | **+193 msg/s ≈ 69 万条/小时** |
| 8 | 若下游变慢 3 倍（E[T]=4.5 s），并发不变 | `160 ÷ 4.5` | **35 msg/s** → 积压 +265/s，**1 小时 95 万条** |
| 9 | 清空一个 200 万条的积压要多久 | `200 万 ÷ 107` | **5.2 小时**（且期间一直有人在写入） |
| 10 | 若改成批量 10 条 + 并行处理 10 条 | 吞吐 ≈ 单条处理速率 × 并行度，等效并发 ×10 | 但**在途消息数**=并发×批量，租约被批量摊薄（§32.7 末） |

**第 8 步是「积压一夜涨到几百万」的全部解释**：积压不是容量问题，是 **`λ × E[T]` 的乘积超过了你的并发上限**。下游慢 3 倍 = 需要的并发要乘 3，而 HPA 通常只盯着 CPU/队列深度——**队列深度上升时扩容是滞后的，因为 Pod 启动 + 预热还要 60–120 s**（→ `→ comp/30` 里的同类结论）。

> **量级结论（非承诺值）**。假设：① 处理时长按均值算（**忽略了尾部**，实际吞吐由「并发 × 1/E[T]」决定，若 E[T] 均值 1.5 s 但 P99 30 s，长尾会吃掉 20–30% 的有效并发）；② 每条任务独占一个线程，无线程池复用的排队；③ 消费者扩缩容有 60–120 s 的滞后；④ 在途消息数上限够用（**FIFO 队列的在途消息数有上限，2 万条量级；standard 高得多，12 万条量级——具体数字以目标版本文档为准**，撞上限时 `ReceiveMessage` 会返回空。这也是「FIFO 上把 `MaxNumberOfMessages` 调大反而更慢」的原因：在途额度被少量消费者占满）。

### 账三 · PEL / 内存 = pending 条数 × 每条开销

假设：Redis 7.x 单实例；stream 键 `task:dispatch` 平均每条 2 KB payload；`MAXLEN ~ 5000000`；未被 `XACK` 的比例取事故二实测的 **≈0.8%**（48 天涨 3,870 万条 ÷ 4,147,200 s ≈ 9.3 条/s；5% 的业务报错只覆盖部分时段，见步 9）；consumer 名带 Pod 随机后缀。

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 每条 PEL 条目的内存开销 | ID 16 B（8 B ms + 8 B seq）+ rax 节点 + NACK（`delivery_time`、`delivery_count`）+ 分配器碎片/对齐 | **90–130 B/条**（实测口径，随版本与分配器不同） |
| 2 | pending 4,100 万条 | `4,100 万 × 130 B` | **≈ 5.3 GB** |
| 3 | stream 本体（被 `MAXLEN` 顶住） | `500 万 × (2 KB + 约 64 B 条目头)` | **≈ 10.4 GB** |
| 4 | 其他业务 key（同实例会话数据） | 固定 | 9.8 GB |
| 5 | 合计 | `5.3 + 10.4 + 9.8` | **≈ 25.5 GB → 撞上 26 GB 上限** |
| 6 | 反向验算：PEL 只涨到 1,000 万 | `1,000 万 × 130 B = 1.3 GB` | 总计 21.5 GB，**仍有 4.5 GB 余量**，事故不会发生 |
| 7 | 反向验算：stream 加 `MAXLEN ~ 1000000` | `100 万 × 2 KB = 2.07 GB` | 总计 17.2 GB —— **但在途消息可能被 trim 掉（幽灵 pending）** |
| 8 | 「多久涨到 5.3 GB」 | 事故二实测：48 天涨 3,870 万条 ⇒ 约 **9.3 条/s**；9.3 × 130 B ≈ 1.2 KB/s | `5.3 GB ÷ 1.2 KB/s ≈ 4.4e6 s ≈ 51 天`（与事故的 50 天吻合） |
| 9 | 为什么不是「5% × 峰值」 | 5% 的业务报错只出现在 04-18 之后的一部分流量里；主要增量是**死 consumer 留下的孤儿 PEL**（约 0.8% 量级），它不在业务失败率里 | 只看业务失败率会把增长时间**低估一个数量级**（算成 2 天） |

> **量级结论（非承诺值）**。假设清单：① 每条 PEL 90–130 B（`MEMORY USAGE` + `INFO memory` 差值反推，不同版本/分配器有 ±30% 浮动）；② stream 条目头按 64 B 量级估（`stream-node-max-entries` 默认 100 时才用 listpack 紧凑编码，**以目标版本的默认值为准**）；③ 失败率与峰值速率取事故现场值；④ 不计 RDB/AOF 重写时的 fork COW 峰值内存（**它会临时把内存乘 1.2–2 倍**，→ `→ comp/14`）。**这条账的最后一行才是重点：内存告警的到达时间由「失败率 × 速率」决定，而这两个数都不在队列的仪表盘上。**

```python
# 三本账压成一个可复算脚本（改任一假设直接重跑）
def dup_rate(p):            # 账一：重复率 = P/(1-P)，投递次数期望 = 1/(1-P)
    return p / (1 - p), 1 / (1 - p)

def concurrency(lam, et, threads_per_pod=4, pods=40):
    """账二：Little 定律 → 需要并发、实际吞吐、积压增长速率"""
    need = lam * et                     # 需要多少并发线程
    have = pods * threads_per_pod       # 实际有多少
    tput = have / et                    # 实际吞吐（条/s）
    return need, have, tput, lam - tput

def pel_mem(pending, stream_len, payload_kb, other_gb, per_entry=130, entry_hdr=64):
    """账三：PEL + stream 本体 + 其他 key → 总内存（GB）"""
    pel    = pending * per_entry / 1e9
    stream = stream_len * (payload_kb * 1024 + entry_hdr) / 1e9
    return pel, stream, pel + stream + other_gb

for p in (0.004, 0.05, 0.20, 0.50):
    d, e = dup_rate(p)
    print(f"P={p:6.1%} → 期望投递 {e:.3f} 次/条，重复率 {d:.2%}；12.6 万条/日 ⇒ {126000*d:,.0f} 条/日")

for et in (1.5, 4.5):
    need, have, tput, gap = concurrency(300, et)
    print(f"E[T]={et}s → 需并发 {need:.0f}，实有 {have}，吞吐 {tput:.0f}/s，积压 {gap:+.0f}/s（{gap*3600/1e4:+.0f} 万条/小时）")

pel, stream, total = pel_mem(41_000_000, 5_000_000, 2, 9.8)
print(f"PEL {pel:.1f} GB + stream {stream:.1f} GB + 其他 9.8 GB = {total:.1f} GB（上限 26 GB）")
```

---

## 32.11 架构边界：任务队列能保证什么、不能保证什么

| 想要的能力 | 任务队列给不给 | 原因（形态层面） | 正确做法 |
| --- | --- | --- | --- |
| **至少一次投递** | ✅ 给 | 租约到期/ack 丢失必然重投 | 这就是它的核心承诺，别期待更多 |
| **不重复** | ❌ 不给 | ack 信号会丢，broker 无法区分「没收到」与「回程丢了」 | 幂等键 + 去重落点（§32.8） |
| **恰好一次（效果）** | ⚠️ 只能由你构成 | = 至少一次 × 幂等，队列只提供前半个乘号 | 至少一次 + 幂等执行（→ [eng/24](../eng/24-delivery-semantics.md)） |
| **单条精确重投** | ⚠️ 只有部分形态给 | SQS/Beanstalkd 只能等租约到期；Streams `XCLAIM`、RabbitMQ `nack` 可以 | 需要精确控制就选 Streams/RabbitMQ |
| **严格顺序** | ⚠️ 有条件 | 顺序只在「单消费者」或「单 group/分区」内成立 | SQS FIFO + `MessageGroupId`；或 Kafka 分区键（`→ comp/30`） |
| **延迟投递（小时级）** | ❌ 不给 | 队列的延迟语义上限是分钟级（SQS 15 min） | 延迟队列 / 时间轮（`→ comp/36`） |
| **跨队列 / 跨库原子** | ❌ 不给 | 每条消息的进度独立，没有全局提交点 | outbox + 幂等消费（→ [eng/10](../eng/10-outbox-and-transactional-message.md)） |
| **「谁在处理这条」可查** | ⚠️ 分形态 | SQS 只有 `ApproximateReceiveCount`；Streams 有 PEL 明细；DB 队列有 `owner` 列 | 需要可观测就在消息里带 trace_id 并落处理日志 |
| **无限积压** | ⚠️ 分形态 | SQS 落盘可无限（按 retention）；Streams/RabbitMQ 吃内存 | 积压可能到百万级 → 只有 Kafka/SQS 形态合适（`→ comp/28`） |
| **失败履历** | ⚠️ 只有 RabbitMQ 内置 | `x-death` 头累积每次死信原因 | 其他形态自己往死信消息里加 `attempts`、`last_error`、`first_fail_at` |
| **优先级** | ❌ 主流都不给 | 单一队列只有 FIFO 顺序，没有堆 | 多队列 + 消费端按队列分配并发；Beanstalkd 有 pri |

**八问卡里最容易写错的两格（本篇答案）**：

| 八问 | 本篇的答案 |
| --- | --- |
| **⑤ 扩展维度** | **四选一里只有「分片」与「批处理摊薄」两条成立**：分片 = 多队列/多 group/多 consumer（并行度），批处理摊薄 = 一次 `ReceiveMessage` 取多条 + 批内并行。**副本不适用**：任务队列的副本是服务端的事（SQS 本身多 AZ），对客户端没有「读副本」这种扩展手段；**垂直切**只对「按任务类型分队列」有效（§32.7 手段 3）。一句话：**任务队列按「消费者并发」扩，不按「队列数量」扩——队列数只决定隔离度，不决定吞吐。** |
| **⑥ 系统级失效** | 三类：① **集群不可用**——`noeviction` + 内存打满导致全量写入被拒（事故二）；② **数据不一致**——可见性超时到期重投造成同一任务并发执行 + 副作用重复（事故一）；③ **成本失控**——`NumberOfEmptyReceives` 的空轮询请求费、DLQ 无限增长后的存储与人工成本、以及「积压 × 单条大小」的内存占用（事故二 5.3 GB PEL）。使用级错误（timeout 设错、忘了 ack、DLQ 没告警）归 §32.12。 |

**一句话边界**：任务队列买到的是「**一条消息在某一刻归某一个消费者独占，且独占会过期**」。围绕这句，它能保证「至少一次」，不能保证「只做一次」，也不负责「做失败了以后谁来看」。**后两件事只能由你的幂等键和你的值班表保证——队列连一个告警都不会替你发**。

---

## 32.12 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到形态） | 怎么定位 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 队列深度一直 0，但业务数据翻倍（事故一） | 可见性超时到期 → 同一条消息被两个消费者同时处理；B 模型的「在途」不进深度指标 | `ApproximateReceiveCount > 1` 的消息占比；`NumberOfMessagesReceived ÷ NumberOfMessagesSent > 1.05` | 调大 `VisibilityTimeout` 到 `P99.9 × 2`，或加心跳续租；补幂等键 | timeout 纳入变更评审，按「处理时长分布」推导而不是用默认值 |
| 明明 P99 只有 5 s，重复却很多 | 批量取消息（`MaxNumberOfMessages=10`）+ 串行处理：租约从「收到」开始计，不是从「开始处理」 | 对比单条处理时长与 `timeout ÷ 批量`；看消费者启动日志里的 receive 时间戳与业务开始时间戳之差 | 慢任务 `MaxNumberOfMessages=1`；或取批后并行处理；或按批大小重算 timeout | 在消费者 SDK 里把「批量 × 处理时长 < timeout」做成启动断言 |
| Redis Streams 内存线性增长，`MAXLEN` 形同虚设（事故二） | PEL 是第二份存储，`MAXLEN` 只裁 stream；且 PEL 归属 consumer 名，Pod 换名后无人认领 | `XPENDING` 总数、`XINFO GROUPS` 的 `pending` 与各 consumer 的 `idle`；`MEMORY USAGE` 与 `INFO memory` 的差 | 失败路径必须 `XACK` 或进死信；`XAUTOCLAIM` 定期认领；consumer 名用**固定逻辑名**（不随 Pod 变） | 把 `pending` 与 `idle` 上限做成一等指标并告警；consumer 命名规范进部署模板 |
| 反复 `XCLAIM` 同一条，`delivery_count` 疯涨 | `min-idle-time` 设得比处理时长短，正常在跑的任务被抢走 | `XPENDING key g - + 100 <consumer>` 看 `delivery_count` 分布 | `min-idle-time` ≥ 处理 P99.9；认领后立即处理，别再 claim | 认领阈值与 timeout 用同一个「处理时长分布」推导 |
| Streams 上「消息不见了」 | `XTRIM` / `MAXLEN` 把还没 ack 的条目裁掉了，PEL 里留下「幽灵 pending」（有 ID 无内容） | `XAUTOCLAIM` 的返回里有 deleted id 计数；日志里出现「claim 到 ID 但取不到 field」 | `MAXLEN` 必须远大于「最大在途量」（≥ 峰值 × 处理时长 / 单条大小）；用 `MINID` 按时间裁更安全 | 把 `XLEN` 与被裁条目数纳入巡检；裁剪阈值由 §32.10 账三反推 |
| DLQ 有 200 万条，三天没人知道（事故三） | DLQ 只是一个队列，没有消费者、没有告警、retention 还是默认 4 天 | `get-queue-attributes` 的 `ApproximateNumberOfMessages`；`OldestMessageAge` | 立刻加告警 + 加「只读转工单」消费者 + 把 retention 拉到主队列同值；修根因后 replay | DLQ 上线检查单：深度告警 / 年龄告警 / 消费者 / replay 工具 / retention ≥ 主队列，五项缺一不许上线 |
| 契约变更后整条链路吞吐掉到 1/3 | 不可重试错误被当成可重试，每条跑满 `maxReceiveCount` 次 | 按 `error_class` 分组的失败计数；解析异常堆栈里是不是同一种 | 按错误类型分流（解析失败/字段越界直接进死信） | `maxReceiveCount` 与「错误分类表」一起进接入规范 |
| 死信 replay 之后死信更多 | 根因未修 + `ApproximateReceiveCount` 被重置，同一批又跑满次数回来 | replay 前后 DLQ 深度曲线 | replay 前先修根因；工具里限流 + 同批 24 h 只允许一次 | replay 必须走审批（操作人 + 原因 + 批次号），并 watch 24 h |
| 幂等「只在第一天有效」 | 去重记录保留期 < 重投窗口（SQS 主队列 4 天 + DLQ 4 天 + 人工 replay 可能跨周） | 抽样重投消息的业务键，查去重表是否已被清理 | 去重记录保留期 ≥ 主队列 retention + DLQ retention + replay 窗口 | 清理任务的 TTL 由这两个 retention 推导，写进建表 DDL 注释 |
| 「我们幂等做得很好」，但少了几笔（ack 成功消费失败） | 某条代码路径抢跑了 ack：`auto-ack` / `enable.auto.commit` / `XACK` 写在 try 首行 / 先 `DeleteMessage` 再处理 | 对账：业务库条数 vs 队列 `NumberOfMessagesSent` 的差；grep 消费端所有 ack 调用位置 | 一律「处理成功后再 ack」；异步场景改成「ack = 落本地任务表成功」 | 静态检查：ack/delete/commit 必须出现在成功分支末尾（lint 规则或 code review checklist） |

---

## 32.13 观测：五个数就能覆盖 90% 的故障

**前提说明**：SQS 的 CloudWatch 指标默认 **1 分钟粒度**（部分指标为近似值，名字就带 `Approximate`），需要「每分钟以上」的精度要开 **CloudWatch detailed metrics（付费开关）**；Redis 侧依赖 `INFO` / `XINFO` / `XPENDING` 自采（Redis 7.0+ 的 `XINFO GROUPS` 带 `lag` 字段，更早版本要自己算 `last-delivered-id` 与 `last-generated-id` 的差）。**指标名与字段存在性以目标版本文档为准。**

| 看什么 | 指标 / 命令 | 拐点（越线就动手） | 它度量的是哪件物理事实 |
| --- | --- | --- | --- |
| **① 重复率（第一指标）** | `NumberOfMessagesReceived ÷ NumberOfMessagesSent`（SQS）；Streams 用 `delivery_count > 1` 的 pending 占比 | **> 1.02 即查**；> 1.1 = 可见性超时严重不匹配 | 处理时长分布与租约时长是否匹配（§32.7） |
| **② 在途数** | `ApproximateNumberOfMessagesNotVisible`（SQS）；Streams 的 `pending` | 稳态应 ≈ 并发数；**持续 > 并发数 × 3** = 有任务卡住或续租失控 | 「被取走但没做完」的唯一直接读数 |
| **③ 最老消息年龄** | `ApproximateAgeOfOldestMessage`（SQS）；Streams 用 `XPENDING` 的 `idle` 最大值 | > 你承诺的端到端时延 × 3 即 P2；> 1 h 即 P1 | 端到端延迟的真实上界（比队列深度可靠得多） |
| **④ 死信深度与年龄** | DLQ 的 `ApproximateNumberOfMessages` + `ApproximateAgeOfOldestMessage` | **深度 > 0 持续 5 min 即 P2；年龄 > 1 h 即 P1** | 「败了多少」与「已经败了多久没人管」 |
| **⑤ PEL 总量与碎片化** | `XPENDING <key> <group>` 总数；`XINFO GROUPS` 的 `pending`、`lag`；`XINFO CONSUMERS` 的 `idle` | `pending` 增速 > 0 持续 10 min；`idle > 10 min` 的 consumer > 0（说明有死 consumer 挂着积压） | 事故二的唯一前兆（§32.10 账三） |
| ⑥ 内存水位（Streams 特有） | `used_memory` / `maxmemory` 比值；`maxmemory_policy` | **> 80% 即 P2；`noeviction` 且 > 90% 即 P1**（下一次写入就被拒） | 内存是 Streams 的硬边界，SQS 没有这一项 |
| ⑦ 空轮询率 | `NumberOfEmptyReceives ÷ NumberOfMessagesReceived` | > 50% = 消费者在空转（`WaitTimeSeconds` 太小或消费者过多） | 白付的请求费 + 无意义的 API 压力 |
| ⑧ 终态失败率 | 进死信条数 ÷ 入队条数（自采，队列本身没有这个指标） | 与「失败率」的比值 > 3 倍即说明重试无效 | 「有多少业务真的没做成」——给业务看的数 |
| ⑨ 处理时长分布 | 消费端打点（APM / 直方图），取 P50/P99/P99.9 | `P99.9 > VisibilityTimeout` 即必然重复 | 所有 timeout 决策的唯一输入 |
| ⑩ 租约续租次数 | `ChangeMessageVisibility` 调用量 ÷ 收到消息数 | > 3 = 大量长任务，该拆分队列 | 「快任务 + 慢任务混在一个队列」的证据 |

```bash
# 值班第一屏：先看「重复率 / 在途 / 死信年龄」三个数（SQS）
aws sqs get-queue-attributes --queue-url "$Q" \
  --attribute-names ApproximateNumberOfMessages Visible ApproximateNumberOfMessagesNotVisible \
    ApproximateAgeOfOldestMessage MessageRetentionPeriod RedrivePolicy VisibilityTimeout
aws cloudwatch get-metric-statistics --namespace AWS/SQS --metric-name NumberOfMessagesReceived \
  --dimensions Name=QueueName,Value="$QUEUE" --start-time "$(date -u -d '-2 hour' +%FT%T)" \
  --end-time "$(date -u +%FT%T)" --period 300 --statistics Sum
aws cloudwatch get-metric-statistics --namespace AWS/SQS --metric-name NumberOfMessagesSent \
  --dimensions Name=QueueName,Value="$QUEUE" --start-time "$(date -u -d '-2 hour' +%FT%T)" \
  --end-time "$(date -u +%FT%T)" --period 300 --statistics Sum
# 判读：Received / Sent 的比值就是重复率的现场读数（> 1.02 就要查 timeout 与批量）
# 注：老版本 CLI 里属性名写作 ApproximateNumberOfMessages；新版只剩 ApproximateNumberOfMessagesVisible，以目标版本 CLI 为准

# 告警表达式骨架（Prometheus 风格，SQS 用 cloudwatch-exporter 转成指标）
# rate(sqs_number_of_messages_received[5m]) / rate(sqs_number_of_messages_sent[5m]) > 1.02
# sqs_approximate_number_of_messages_not_visible > 3 * <消费者并发数>
# sqs_approximate_age_of_oldest_message > 3600
# sqs_dlq_approximate_number_of_messages > 0   for 5m
# redis_streams_pending{group="dispatch"} > 1000000
# redis_streams_consumer_idle_seconds{group="dispatch"} > 600
# redis_memory_used_bytes / redis_maxmemory_bytes > 0.85
```

---

## 32.14 一页速记

| 记什么 | 具体结论 |
| --- | --- |
| 形态一句话 | 任务队列 = **「一条消息在某一刻归某一个消费者独占，独占会过期」**；它存的是租约，不是完成回执 |
| 三种确认模型 | A 处理完再 ack（Kafka/Streams/RabbitMQ/Beanstalkd）· B 超时自动重投（SQS）· C lease + 续租（`ChangeMessageVisibility`/`touch`/`XCLAIM`/`locked_until`） |
| 三者真正的区别 | 不是「可靠不可靠」，而是**「谁判断第一个消费者已经死了」**：会话超时 / 一个固定时钟 / 心跳。三种都会误判 |
| 为什么一定是至少一次 | 三个崩溃点（ack 前崩溃、ack 丢失、网络分区）上，「不重」与「不丢」的判据是同一个会丢的信号 |
| 恰好一次 = | 至少一次投递 × 幂等执行；任务队列只给前半个乘号 |
| SQS 关键数 | `VisibilityTimeout` 默认 30 s / 上限 12 h；`MaxNumberOfMessages` ≤ 10；长轮询 ≤ 20 s；`MessageRetentionPeriod` 默认 4 天 / 上限 14 天；单条延迟 ≤ 15 min；FIFO 去重窗口 5 min |
| Streams 关键数 | PEL 每条约 90–130 B；`MAXLEN` **不裁 PEL**；`XAUTOCLAIM` 需 Redis 6.2+；死信与重试上限全要自己写 |
| timeout 的算法 | `timeout ≈ 处理 P99.9 × 2`，且必须满足 `timeout × maxReceiveCount < 业务最晚补偿时间`；批量取消息时排队时长也算在租约里 |
| 重复率公式 | `P/(1−P)`，其中 `P = P(排队 + 处理 > timeout)`；P=5% ⇒ 重复率 5.26% |
| 积压公式 | `并发数 = 到达速率 × 平均处理时长`；下游慢 3 倍 ⇒ 需要并发 ×3，否则积压 = `λ − 并发/E[T]` |
| 幂等三要素 | 稳定键（业务单号，**不是消息 ID**）× 原子落点（去重表/唯一索引/状态机 CAS/outbox）× 保留期 ≥ 重投窗口 |
| 死信三因子 | 有人消费 × 有告警 × 有 replay；缺一为 0。DLQ 的 retention 要 ≥ 主队列 |
| 最危险的一行 | **ack 成功但消费失败**：没有信号、没有重复、没有死信，只有「少了一笔」 |
| 首要指标 | `Received ÷ Sent`（重复率）· `NotVisible`（在途）· `AgeOfOldestMessage` · DLQ 深度与年龄 · PEL 总量 |

**如果只记五句：**

1. **队列承诺的是「至少一次 + 一个会过期的租约」，不是「做完了」。** 任何「队列保证任务成功」的说法，都是把租约误读成了完成回执。
2. **可见性超时是单方面判定，不问、也不通知。** 所以「处理时长分布」是必须建模的一等公民——用默认 30 s 就等于默认 P99 < 30 s。
3. **PEL / unacked / in-flight 都是「第二份状态」，它们有独立的内存与清理责任。** Streams 的 `MAXLEN` 管不到 PEL；RabbitMQ 的 unacked 记在 broker 内存里；SQS 的在途数不在队列深度里。
4. **幂等只能在副作用侧做，键必须是业务键。** 消息 ID 在重试队列、DLQ 重放、主队列之间都会变，只有业务单号不变。
5. **死信不消费等于没有死信。** DLQ 只是一个队列，它自己也需要消费者、告警和 replay——而那三样东西队列永远不会替你准备。

---

## 32.15 自检题（5 题）

**Q1.** 你的队列是 SQS standard，`VisibilityTimeout=30 s`，消费端 `MaxNumberOfMessages=10` 且**串行**处理，单条处理 P50 800 ms、P99 6 s。请估算重复率量级，并说明为什么「P99 只有 6 s，远小于 30 s」这个判断是错的。

<details><summary>答案</summary>

**错的在「租约从什么时候开始计」**。`ReceiveMessage` 一次拿走 10 条，租约同时从**收到**那一刻开始计；串行处理时，第 k 条的可用时间是 `30 s − (k−1) × 单条耗时`。

按 P50 800 ms 算：第 10 条开始处理时已过 7.2 s，剩 22.8 s——还够。但按**尾部**算：只要某一条慢到 6 s（P99），它后面的任务可用时间就被吃掉 6 s；一条批量里出现两条 P99，第 10 条的余量只剩 `30 − 7.2 − 12 ≈ 10.8 s`。再叠加「批量取走后 10 条必须在 30 s 内全部完成」这个约束，**有效要求变成「10 条之和 < 30 s」，也就是单条均值必须 < 3 s**。

所以正确定义是：`P = P(批量内前序任务耗时之和 + 本条耗时 > timeout)`，而不是 `P(单条耗时 > timeout)`。按 10 条批量、均值 0.8 s、长尾 6 s 的分布，重复率会从「几乎 0」变成 **每天千分之几到百分之一**，而且**只在批量里出现**——排查时表现为「重复的时间戳总是成簇」。

三个修法：① 慢任务 `MaxNumberOfMessages=1`（最简单）；② 取批后**并行**处理 10 条（租约被 10 条摊薄，但每条只剩自己的处理时长）；③ 把 `VisibilityTimeout` 按「批量 × 单条 P99」重算，并保证 `maxReceiveCount × timeout` 仍在业务可忍范围内。

量级结论，非承诺值。
</details>

**Q2.** Redis Streams 上，`MAXLEN ~ 2000000` 已经设了、内存却从 12 GB 涨到 26 GB。请给出至少三个可能原因，并说明每个该怎么确认。

<details><summary>答案</summary>

① **PEL 无界增长**（最可能）：`MAXLEN` 只裁 stream，不裁 PEL。确认：`XPENDING <key> <group>` 的总数，乘每条约 90–130 B 与总量对比；再看 `XINFO GROUPS` 的 `pending` 是否在涨。

② **consumer 名随 Pod 变化导致 PEL 碎片**：`XINFO CONSUMERS` 里出现几十个 `idle` 数小时的 consumer，每个都挂着一份 PEL。确认：consumer 数量与当前 Pod 数不一致；`XPENDING` 带 consumer 参数逐个看。

③ **stream 单条很大，`MAXLEN` 条数上限 ≠ 内存上限**：`MAXLEN ~ 200 万` × 平均 2 KB = 4 GB，每条 10 KB 就是 20 GB。确认：`MEMORY USAGE <key>` 与 `XLEN` 相除得单条平均字节。

④ **其他 key 或 RDB/AOF 重写峰值**：同实例上别的 key 涨了，或 `BGSAVE` fork COW 让 RSS 短时翻倍。确认：`INFO memory` 的 `mem_fragmentation_ratio` 与 `rdb_last_cow_size`；用 `--bigkeys` / `MEMORY USAGE` 排除别的 key。

⑤ **listpack 编码退化成 rax**：单条超过阈值或 entry 过大时不再用紧凑编码。确认：`DEBUG OBJECT` 看 `encoding`（生产慎用），或对同配置环境做对照压测。

修的顺序：先 `XAUTOCLAIM` 把死 consumer 的 pending 认领并处理（或进死信）→ `XGROUP DELCONSUMER` 清掉死 consumer → 加 `pending` 与 `idle` 告警 → 把 consumer 名改成固定逻辑名。**不要先用 `XTRIM` 硬裁**：那会把在途条目裁成幽灵 pending。
</details>

**Q3.** 一个 12.6 万条/日的支付回调链路，`timeout` 从 30 s 调到 300 s。请分别算出「重复率」和「失败任务进死信的最坏时长」的变化，并说明还需要同时改什么。

<details><summary>答案</summary>

**重复率**：`P/(1−P)`。尾部 `> 30 s` 的比例在正常时段 0.4%、在银行批处理窗口 5%；`> 300 s` 的比例约 0.01% 量级。于是：

- 正常时段：`0.4% → 0.01%`，日重复从约 506 条降到约 13 条（**降 40 倍**）。
- 批处理窗口（假设 `> 300 s` 的比例按同样比例缩小 40 倍）：`5% → 0.125%`，窗口内重复从 1,050 条降到约 26 条。

**最坏时长**：`maxReceiveCount × timeout`。`maxReceiveCount=3` 时从 `90 s` 变成 `900 s`（15 min）；`=5` 时从 150 s 变成 25 min。也就是说**失败反馈延迟被放大 10 倍**，同时一条「进程假死」的任务会占住在途 300 s 而不是 30 s。

**还必须同时改三件事**：① `maxReceiveCount` 与退避表要重算（`Σ退避 + maxReceiveCount × timeout` 必须 < 业务最晚补偿时间）；② 慢任务（> 30 s）单独拆队列——300 s 的 timeout 用在快任务上是浪费；③ 幂等的去重记录保留期要覆盖更长的重投窗口（30 s 时长下你可能只留了 1 天，300 s 之后要按 `retention + DLQ retention` 算）；④ 告警的 `NotVisible` 阈值要跟着改，否则「在途数」的噪声基线变了，告警会失灵。

量级结论，非承诺值。
</details>

**Q4.** 「我们有 DLQ、`maxReceiveCount=3`，所以失败的消息不会丢。」这句话哪几处错？请给出 DLQ 上线检查单。

<details><summary>答案</summary>

错在三处：

① **DLQ 也是一个队列，它有 retention**。SQS DLQ 的 `MessageRetentionPeriod` 默认 4 天（从 `SentTimestamp` 算，也就是从**第一次进主队列**算），一条 05-30 产生的消息在 06-03 就会被物理删除——**没被处理过的消息就这么"合法"消失了**（事故三）。

② **没有消费者的 DLQ 只是一个更贵的垃圾桶**。它不进任何人的待办，`ApproximateNumberOfMessages` 这个数字没人看就是 0 信息。

③ **`maxReceiveCount` 在 standard 队列上不等于「业务失败 3 次」**：它是「被收到 3 次」，而纯粹的慢（可见性超时到期）也会推高这个计数。所以「进 DLQ」既可能漏（重试次数被慢动作吃掉），也可能误（业务一次没失败）。

**DLQ 上线检查单（五项，缺一不许上线）**：① 深度告警（> 0 持续 5 min 即 P2）；② 最老消息年龄告警（> 1 h 即 P1）；③ 一个消费者：只读 + 落工单/落库，把死信变成可检索记录；④ replay 工具：带审批、操作人、批次号，同批 24 h 只允许一次；⑤ `retention ≥ 主队列`，且 ≥ 人工发现与处置的平均时长。**再加第六项（最容易被忘）**：值班文档里写明「DLQ 非空时该找谁」——事故三里那 31 万条无法还原，就是因为没人知道该找谁。
</details>

**Q5.** 你要在「Redis Streams / DB 队列（`FOR UPDATE SKIP LOCKED`）/ SQS FIFO」之间选一个，用来跑「每笔订单的发货指令，同一订单必须按序，峰值 2,000 条/s，已在 Redis 上有 3 个实例，团队 4 人」。请给出推荐、两条排除理由与必须自建的三个东西。

<details><summary>答案</summary>

**推荐：SQS FIFO**（若已在 AWS 上）；若必须自建、且对吞吐的要求是「峰值 2,000/s 而不是 20,000/s」，则**DB 队列也可行但要看库的写能力**。

**排除 Streams 的理由**（两条）：① **它在内存里，而 2,000/s × 处理时长意味着可观的在途量**：`2,000 × 1 s = 2,000` 条在途，加上 stream 本体，内存账要按 §32.10 账三算；更要命的是 **PEL 有独立的内存责任，且 `MAXLEN` 管不到它**——团队只有 4 人，维护不了这套「自己写的死信 + 自己写的重试上限 + 自己写的认领策略」。② **`MessageGroupId` 的等价物要自己实现**（用 stream key 分片），而「同一订单必须按序」在 Streams 上只能靠「一个 group 一个 consumer 串行 + 单 stream」，吞吐会被单线程 Redis 的上限钉住。

**排除 DB 队列的理由**（若订单库是主业务库）：2,000/s 的 claim 就是 2,000 次事务/s，**会和业务读写抢同一个库**；长事务与表膨胀还会拖住 undo/vacuum（`→ comp/06`、`→ comp/11`）。它适合「几千/s 以下 + 与业务强一致」的场景，不适合当高频事件总线。

**必须自建的三个东西**（无论选谁）：① **幂等键与去重落点**——用 `order_id + action` 做键，落唯一索引或去重表（§32.8）；② **死信的消费者 + 告警 + replay**（§32.9，SQS 只送 DLQ 与 redrive，不送消费者）；③ **处理时长分布的打点与 timeout 的定期复核**（§32.7，这项工作没有任何队列能替你完成）。**如果团队只有 4 人，第 ③ 项要写进值班手册，而不是留在某个人的记忆里。**
</details>

---

## 32.16 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 「被取走但没做完」的另一半：投递语义的三个乘号、重复的四个来源、去重表 / 状态机 / 唯一索引三种落点 | [eng/24 投递语义与重复消费](../eng/24-delivery-semantics.md) |
| 可靠性八环节、DLQ 表结构与退避调度器的完整 SQL/Java 实现（本篇只给骨架与账） | [eng/26 消息可靠性](../eng/26-message-reliability.md) |
| 该不该上 MQ、日志型 vs 队列型 vs 流式存算分离的形态决策 | [architect/15 消息存储](../architect/15-message-store.md) |
| Outbox：彻底消除「消费成功但 ack 失败」的那一半不确定性 | [eng/10 Outbox 与事务消息](../eng/10-outbox-and-transactional-message.md) |
| Kafka 的日志形态：为什么它把「位点」而不是「单条租约」当 ack | [28 Kafka 的日志：segment、稀疏索引与零拷贝](28-kafka-log-segments.md) |
| ISR / HW / LEO / acks：`acks=all` 的「不丢」是一次四个参数的联署；与本篇的 `timeout` 同构（调大调小都能弄坏系统） | [29 复制与一致性：ISR、HW、LEO 与 acks](29-kafka-replication-isr.md) |
| rebalance、`max.poll.interval.ms` 与积压：Kafka 版的「在途」是整段未提交位点 | `→ comp/30` |
| 消息族谱：RocketMQ 的延迟等级、Pulsar 的存算分离、RabbitMQ 的 `x-death`、NATS 的 at-most-once | `→ comp/31` |
| 延迟投递（小时级、天级）与时间轮：任务队列的延迟上限只有分钟级 | `→ comp/36` |
| 事务组件：超出「幂等」范围、需要真正的分布式事务时（XA / AT / TCC） | `→ comp/43` |
| Redis 持久化：`appendfsync everysec` 的 1 秒窗口如何变成「重启后重复」 | `→ comp/14`、[12 单线程的真相](12-redis-single-thread.md) |
| 行锁、长事务与 undo：DB 队列的代价落在这里 | `→ comp/06`、`→ comp/07` |
| 解剖卡八问、三本放大账与三条纪律（版本常量 / 指标前提 / 量级结论 vs 承诺值） | [0-summary](0-summary.md) §0.4、§0.7 |
| 组件故障谱：本篇三起事故在「慢 / 满 / 脑裂 / 假活 / 语义漂移」里的归类 | `→ comp/52` |
