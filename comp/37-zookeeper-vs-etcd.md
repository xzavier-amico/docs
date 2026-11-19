# 37. ZooKeeper vs etcd：ZAB、Raft、session 与 watch

> **状态**：`已完成（初稿）` ｜ **所属**：第七部分 · 协调、注册与配置 ｜ **编号**：37 / 53
> **本篇定位**：全仓唯一一处把 ZooKeeper 与 etcd 的**物理形态**摆在同一张桌子上对照的篇目——znode 树 + txnlog + 模糊快照 + ZAB，对上 MVCC B-tree + WAL + snapshot + Raft。共识与日志的通用机制不在本篇重写：WAL 与崩溃恢复的套路归 [03 WAL、缓冲池与崩溃恢复](03-wal-bufferpool-crash-recovery.md)，「多数派换一致性」的算术归 [16 一致性哈希与 quorum](16-consistent-hashing-and-quorum.md)，Raft + 存储引擎的组合形态归 [19 RocksDB 与 TiKV](19-rocksdb-and-tikv.md)，日志与复制位点的语义归 [28 Kafka 的日志](28-kafka-log-segments.md) 与 [29 复制与一致性](29-kafka-replication-isr.md)。本篇只回答一个问题：**协调组件的本质是「小数据集 + 共识 + 变更通知」，为什么 session 失效与一次性 watch 是它全部坑的来源。**
> **读法**：救火直接读 §37.11 的坑清单与 §37.12 的指标拐点；做选型先读 §37.2 的三个不可替代能力建立坐标系，再读 §37.10 的判定表；被追问「为什么 GC 一停顿就双主」读 §37.5 与 §37.9 账一；被追问「配置为什么没推到我机器上」读 §37.6 与 §37.9 账二；只想背结论翻 §37.13。

---

## 37.1 引子：三起「协调组件先坏」的事故

协调组件有三个别称，每一个都精确描述了它的处境：**它是全站的单点依赖**（所有组件都要问它「谁是主」）、**它存的都是小数据**（所以没人给它做容量规划）、**它只在变更时出现**（所以它的故障总是以「别的系统的语义错误」的形式暴露）。下面三起事故，起因分别是「它自己先挂」「它误判了别人死」「它没把消息送到」。

### 事故一：协调组件自己先挂，全站选主停摆 37 分钟（2026-01-09）

某交易平台的 ZooKeeper ensemble 5 节点跨两机房（3+2），承担四类职责：对账任务的主备选主、定时任务的分片计算、订单状态机的超时兜底、以及两个老框架的注册发现。稳态写入约 **300 txn/s**（临时节点续期、分片进度、锁心跳都在里面），单条事务记录平均 250 B 量级。

出事前 180 天里，`autopurge.purgeInterval` 一直保持默认值 **0**——也就是「不自动清理」。`autopurge.snapRetainCount` 默认 3 只对 snapshot 生效，对 txnlog 没有任何强制上限。

| 时刻 | 事件 | 关键数字 |
| --- | --- | --- |
| 09:12 | zk-04 的 `/data` 到 100%，告警被当成「日志没清」，运维清了 `/var/log`（无关目录） | 单节点 txnlog 目录 432 GB |
| 09:20 | zk-04 写 txnlog 失败 → 自认为无法服务 → 进程反复重启 | 流失 1 / 5 节点 |
| 09:26 | zk-05 同型号同批盘，同样满 → 也退出 | 剩 3 / 5（quorum 还在，业务无感） |
| 09:31 | leader（zk-01）堆 16 GB、DataTree 4.2 GB、watch 800 万条，Full GC 8 s → 心跳超时被 FLE 换下，zk-02 上位 | leader 切换 1 次 |
| 09:32 | 换主窗口内所有「创建临时节点」请求失败，业务侧 Curator 重试策略（3 次 × 1 s）放大成请求风暴 | 重试放大 3× |
| 09:34 | zk-03 盘也满 → 剩 2 / 5，**失去多数派**：整个 ensemble 只读，无法创建/删除任何节点 | quorum 2 < 3 |
| 09:34–10:11 | 对账选不出主（37 分钟没跑）；定时任务分片全停；订单超时兜底不触发 | 卡单 27 万笔 |
| 10:11 | 恢复：扩盘 + 只保留最新 snapshot 与其后的 txnlog + 打开 `autopurge.purgeInterval=6` | 停 37 分钟 |
| 复盘 | 重复扣款 8,400 笔（对账追回 6,100）、37 分钟收入损失 | ≈ 102 万元 |

**这起事故最反直觉的一点**：ZK 挂掉的时候，业务系统没有任何「连接失败」——它们只是**再也选不出主、再也拿不到锁、再也不更新分片进度**，然后按各自的兜底逻辑静默降级。协调组件的故障在业务侧表现为「一个本该发生的事没发生」，这是它比数据库难排查的根本原因。

### 事故二：一次 12.4 秒的 Full GC，让 session 过期、临时节点被删，双主跑批（2026-02-21）

某支付公司日终跑批用 ZK 抢主：抢到 `/batch/leader` 这个**临时节点**的进程才允许跑批，`sessionTimeoutMs = 10_000`。主节点 JVM 堆 32 GB、G1，跑批期间堆里缓存 20 万条订单快照。

| 时刻 | 事件 | 关键数字 |
| --- | --- | --- |
| 02:00 | 跑批开始，堆从 12 GB 涨到 29 GB，Mixed GC 间隔缩短到 40 s | 堆利用率 90% |
| 02:14:14 | 一次 Full GC 开始（晋升失败） | 停顿 **12.4 s** |
| 02:14:20 | 服务端判定 session 过期（阈值 10 s，已 6 s 没收到心跳），删除临时节点 → 备机收到 watch → 抢主成功 → 开始跑批 | 过期判定 6 s 后的那一跳 |
| 02:14:26 | 主节点 GC 结束，**进程活着**，仍在处理第 7 分片的写批次；它要等到「连接断开/重连 → SESSION_EXPIRED 事件」才知道自己不是主 | 双主窗口 ≈ 2.4 s |
| 02:14:26–02:14:31 | 两个进程同时写第 7 分片对账结果，幂等键缺失 → 重复入账 | 重复 3.2 万笔 |
| 02:15 | 备机跑完第 7 分片，主节点也跑完；两批结果合并时金额差 9,800 笔 | 差异 9,800 笔 |
| 复盘 | 赔付 31 万元；财务重跑 3 天；根因写的是「ZK session 超时」，真正的根因是「堆太大 + 跑批把堆打满 + 用临时节点当锁而没有 fencing」 | ≈ 31 万元 |

**为什么这起事故不是「ZK 的 bug」**：ZK 只知道「10 秒没收到这个 session 的请求」。它不可能知道对方是在 GC、在 swap、在网络黑洞、还是真的死了——**这四种情况在协议里完全同形**。所以「会话有效期」永远是一个**单方面判定**，而单方面判定必然有误判，误判的方向只有一个：**它还活着，但已经被开除了。**

### 事故三：一次性 watch 丢了一次，配置永远到不了那台机器（2026-03-05）

同一个平台的两套订阅代码，栽在同一个坑的两侧。6,000 台网关订阅「限流规则」这个节点。

- **ZK 侧**：`getData(path, true)` 是一次性 watch。回调里重新注册 watch 的 `getData` 恰逢重连抛了 `ConnectionLoss`，代码只 `log.warn` 就返回了。这台机器从此**没有任何 watch**，永久停留在当时的阈值上。
- **etcd 侧**：`Watch(ctx, key)` 没带 `WithRev`，默认从「当前 revision 之后」开始。某个节点断线 17 分钟，期间规则改了 3 次（3,000 → 2,000 → 1,500 QPS），重连后从「现在」往后 watch——那 3 次变更**永久丢失**，它仍以为阈值是 3,000。

| 时间 | 事件 | 关键数字 |
| --- | --- | --- |
| 03-05 20:00 | 灰度发布：限流阈值 3,000 → 1,500 QPS，分 3 批推 | 6,000 台订阅者 |
| 20:04 | 200 台因 etcd watch 丢事件、4 台因 ZK 一次性 watch 未重注册，仍用旧值 | 204 / 6,000 |
| 20:10–20:52 | 这 204 台按 3,000 QPS 放行，下游风控集群被打到 2.4 倍容量，超时 | 风控超时 4,200 万次 |
| 03-12 | 某值班同事抽查配置 CRC，发现一台机器与集群不一致 | 潜伏 **7 天** |
| 复盘 | 降级与赔付 26 万元；根因不是「watch 不可靠」，而是**把「收到通知」当成了「状态正确」** | ≈ 26 万元 |

> 🩸 **三起事故的共同形状**：没有一次是「协调组件算错了」。事故一是**它的容量没人算**（小数据集不等于小写入量，写次数 × 保留期才是账）；事故二是**它的判定权没人质疑**（会话超时是服务端单方面宣布的死亡证明）；事故三是**它的通知被当成了状态**（watch 只承诺「变了」，不承诺「你知道了之后状态就是对的」）。三件事都长在同一个根上：**协调组件只保证「共识」，不保证「你的进程还活着」和「你的缓存还正确」。**

---

## 37.2 一句话定位与唯一真问题

**定位**：ZooKeeper 与 etcd 不是「小数据库」，也不是「配置中心」——它们是**一个被共识算法保护起来的、极小的、带版本有序的 KV 命名空间，外加一个「谁在监听」的注册表**。它们存的东西只有三类：**谁活着**（session / lease）、**谁是主**（临时顺序节点 / lease + 竞争 key）、**当前是第几版**（zxid / revision）。所有能力的上限，都来自「这三类数据必须每次写都过半数 fsync」这一条物理事实。

### 唯一真问题

> **协调组件的本质是「小数据集 + 共识 + 变更通知」，会话失效与一次性 watch 是全部坑的来源。**

这句话拆成五条推论，全篇都在给它们找证据：

1. **「小数据集」是共识的代价换来的，不是设计者的偏好。** 每次写都要多数派落盘，所以写吞吐在数千 txn/s 量级；要把这个数字用在业务数据上，成本会是数据库的几十倍。**小是结论，不是选择。**
2. **共识买的是「顺序」，不是「正确」。** zxid 与 revision 保证「所有节点看到同一个写序列」，但保证不了「你的进程在写的时候还是主」——后者需要 fencing token（→ comp/46）。
3. **会话与租约都是单方面判定，判据只有一个信号：心跳有没有按时到。** 而 GC 停顿、swap、网络黑洞、进程真死，在这个信号上完全同形（§37.5）。
4. **watch 是「事件」不是「状态」。** 事件可以丢、可以合并、可以被 compaction 抹掉；收到事件后必须**全量重读**才能得到状态。把事件当状态的系统，一定会在某台机器上悄悄停在旧版本（事故三）。
5. **因此协调组件的正确用法只有一种形状**：`watch 触发 → 全量重读 → 幂等 reconcile`。所有「订阅一次就长期正确」的幻想，都要在 §37.6 被拆掉。

**它明确不承诺什么**（后面各节逐个兑现）：ZK 不承诺「follower 的读是最新的」（默认 stale read）；etcd 不承诺「serializable 读是最新的」；两者都不承诺「session 不会误失效」；都不承诺「watch 事件不丢」；都不承诺「你的主在失去租约的那一刻会立刻停手」。**它们承诺的只有一件事：只要你读到某个 zxid/revision，你就读到了一个全序中被多数派确认过的前缀。**

### 三个不可替代的能力（为什么不能用数据库/Redis 替代）

| 能力 | 物理实现 | 为什么别的东西替代不了 | 代价（你必须在哪付钱） |
| --- | --- | --- | --- |
| **小数据集** | 全量状态在内存（ZK 的 DataTree / etcd 的内存 btree 索引），值在 append-only 日志与快照里 | 只有「全量状态装得进内存」才可能做到「任一副本毫秒级回答 + 快照可整体搬运 + 恢复时间可控」 | 内存是上限：ZK 的 DataTree 几 GB 就开始 GC 抖动，etcd 的 keyspace 建议几个 GB 以内（官方口径是「小于数 GB」，以目标版本文档为准） |
| **共识** | ZAB 的 epoch + zxid / Raft 的 term + index，写要过半数落盘 | 「谁是主」「谁是第几版」这两件事**必须是全序的单一答案**，否则同一时刻会出现两个合法答案（双主、配置回退） | 写延迟下限 = 一次多数派 RTT + 至少一次 fsync；写吞吐上限 = 几千 txn/s 量级 |
| **变更通知** | ZK 的服务端 watch 表（一次性）/ etcd 的 watch stream（按 revision 续传） | 轮询的成本与延迟随订阅者数线性增长：6,000 订阅者 × 1 s 轮询 = 6,000 QPS 常态开销；推送把它变成「只在变更时花 N 次」 | 通知风暴（§37.9 账二）：一次变更 × N 个订阅者；以及「事件不是状态」的全部后果 |

### 解剖卡八问落点（本篇答案）

| 问 | 落在哪节 | 一句话答案 |
| --- | --- | --- |
| ① 承诺什么 | §37.2、§37.10 | 承诺「一个全序前缀」，不承诺「你的进程还活着」和「你的缓存是最新的」 |
| ② 数据长什么样 | §37.3 | ZK：znode 树（内存）+ txnlog + 模糊快照；etcd：bolt 里的 MVCC 版本 + WAL + snapshot |
| ③ 一次读写走哪条路 | §37.3、§37.4 | 写必过 leader（etcd 两跳 fsync、ZK 一跳 fsync + 显式 COMMIT 广播）；读可以本地（两者默认语义不同） |
| ④ 并发与一致 | §37.4、§37.7 | ZAB 的 epoch+zzid 全序 / Raft 的 term+index；读一致性：ZK 默认 stale、etcd 默认 linearizable |
| ⑤ 扩展维度 | §37.8 | **只读副本可以加，写不能扩**：写永远在单 leader 上，只能靠组提交摊薄；这是「不可横向扩，只能换零件」的典型 |
| ⑥ 系统级失效 | §37.9、§37.10 | 失去多数派（全站不可写）、会话误失效（假活 → 双主）、通知丢失（语义漂移）、磁盘被日志写满 |
| ⑦ 看哪几个指标 | §37.12 | ZK：`zk_watch_count` / `zk_outstanding_requests` / txnlog 目录水位 / GC 停顿；etcd：leader 变更、WAL fsync P99、backend commit P99、DB size / quota |
| ⑧ 换掉它的信号 | §37.10 | 写入常驻 > 数千 txn/s、keyspace > 数 GB、需要多租户/灰度/历史版本、需要「配置变更的可审计灰度」→ 换到 38 篇的注册配置中心或真正的数据库 |

---

## 37.3 物理形态：znode 树 + txnlog + 快照，对上 MVCC B-tree + WAL + snapshot

两边都是「日志 + 快照 + 内存状态」的经典三件套（这套套路在 [03 WAL、缓冲池与崩溃恢复](03-wal-bufferpool-crash-recovery.md) 里已经拆透），但**内存状态与磁盘状态的分工正好相反**——这是本篇最重要的一处结构性差异。

- **ZK**：真状态是**内存里的 DataTree**，磁盘上只有「事务日志」和「偶尔一次的整树快照」。apply 是纯内存操作，不落盘。
- **etcd**：真状态是**bolt 文件里的 MVCC 版本**，每次 apply 都要提交一次 bolt 事务（再 fsync 一次）。内存里只有索引。

这个差别的推论很硬：**ZK 的写是「1 次 fsync」（txnlog），etcd 的写是「2 次 fsync」（WAL + bolt commit）**；而 **ZK 的重启恢复要做「加载整树 + 重放 txnlog」，etcd 的恢复要做「加载 db + 重放 WAL」**。前者恢复时间由 DataTree 大小决定，后者由 WAL 长度决定。

### 图 1 · ZooKeeper：内存里是树，盘上只有日志与快照

```text
一个 ZK 节点本地只有三类东西（dataDir/version-2/），但"真状态"在内存里

  内存 · DataTree（全量，ConcurrentHashMap<path, DataNode>）
  /
  ├── /config/app          data ≤ jute.maxbuffer（默认 1 MB，实际建议 KB 级）
  │                        stat: czxid / mzxid / ctime / mtime / version / cversion / pzxid
  ├── /elect/batch         ← 临时节点：ephemeralOwner = sessionId，session 过期即被删
  ├── /nodes/api-1 …       ← 临时顺序节点：子节点序号来自父节点的 cversion
  └── /locks/order-7f3a-0000000012

  内存 · 另外两张大表（决定了容量与故障形态）
  ├── SessionTracker：sessionId → timeout；过期判定按 tickTime（默认 2000 ms）分桶批量处理
  └── WatchManager：(path, eventType) → {watcher 集合}   ← 每条 watch 都占堆，且变更时全量通知

  磁盘 · dataDir/version-2/
  ├── log.0000000001       ← txnlog：所有写事务顺序追加，每条带长度 + 校验（3.4+ 形态）
  │                          预分配 64 MB 量级（preAllocSize=65536 KB），写满或换 epoch 时 roll
  └── snapshot.0a0000f3    ← 模糊快照：把整棵 DataTree + 全部 session/timeout 序列化
                             大约每 snapCount（默认 100,000）个事务做一次（有随机化以打散各节点）

  一次写 = 追加 txnlog（fsync）→ apply 到内存 DataTree（不落盘）→ 应答
  重启恢复 = 读最新 snapshot + 重放其后的 txnlog（→ comp/03 的重放套路）
  ⇒ 推论：zkServer 重启的耗时 ∝ DataTree 大小 + 未快照事务数，与"业务数据量"无关
  ⇒ 推论：autopurge.purgeInterval 默认 0（不清理）⇒ txnlog 目录没有上限（事故一的物理原因）
```

### 图 2 · etcd：内存里是索引，盘上是版本与日志

```text
一个 etcd 成员本地只有四类东西（member/），"真状态"在 bolt 文件里

  /var/lib/etcd/member/
  ├── wal/0000000000000000-0000000000000000.wal   ← Raft 日志段（64 MB 量级）
  │                                                 每条提案先追加并 fsync 这里，才回 ACK
  ├── snap/00000000000000a1-0000000000000f3.snap  ← 某 revision 的全量状态（bolt 文件副本）
  ├── snap/db                                     ← 被真正打开的 bolt 文件（"当前状态"）
  └── (bolt 内部的 bucket)
       ├── "key"   : revision → KeyValue{key, create_rev, mod_rev, version, lease, value}
       ├── "meta"  : consistent_index / term / finishedCompactRev
       ├── "lease" : leaseID → TTL / expiry
       └── "auth" / "members"

  内存 · 两张表
  ├── btree 索引：key → keyIndex{generation → revisions[]}   ← 全 keyspace 的键都在内存
  └── watchableStore：synced watchers（已追上）/ unsynced watchers（落后，从 bolt 回放）

  一次 PUT = ① Raft 追加：leader 与 follower 各自 fsync WAL
             ② 多数派持有时推进 commitIndex
             ③ apply：bolt 事务提交（**再 fsync 一次**），同时写内存 btree 索引
  ⇒ 推论：一次写 = 两跳 fsync ⇒ etcd 写延迟的物理下限比 ZK 高一档
  ⇒ 推论：每个 key 的每次修改都留一个 revision，直到 compaction ⇒ 空间账 = 写速率 × compact 窗口
```

### 一次写落在哪个文件、哪条日志（逐跳对照）

| 跳 | ZooKeeper（写请求由 leader 定序） | etcd（写请求进 Raft 日志） | 量级（同 AZ、NVMe） |
| --- | --- | --- | --- |
| ① | 客户端连到任一节点；**写请求被该节点转发给 leader** | 客户端连到任一成员；非 leader 的提案被**转发给 leader** | 0.2–0.5 ms 一跳 RTT |
| ② | leader 分配 `zxid = (epoch<<32) \| counter` | leader 追加一条 Raft entry（带 term + index） | 纯内存，μs 级 |
| ③ | leader 广播 PROPOSE；各节点写 **txnlog** 并 fsync | leader 与 follower 各自写 **WAL** 并 fsync | fsync 0.1–1 ms（组提交后按批摊薄） |
| ④ | **多数派落 txnlog 后，leader 再广播 COMMIT**（显式第二阶段） | 多数派持有即推进 `commitIndex`（**无第二阶段消息**，由后续 AppendEntries 携带） | 多 1 个单向 RTT（ZK） |
| ⑤ | 各节点 apply 到**内存 DataTree**（不落盘） | 各节点 apply 到 **bolt**（落盘，第二次 fsync） | ZK：μs；etcd：0.5–2 ms |
| ⑥ | 应答客户端 | 应答客户端 | 写 P50 常在 1–3 ms（ZK）/ 2–6 ms（etcd）量级 |
| ⑦ | 读：本地 DataTree（**可能旧**） | 读：默认 linearizable（ReadIndex，要一次 leader 确认）；`WithSerializable()` 才本地读 | 本地读 μs–百 μs；linearizable 读多 1 RTT |

> **这张表要背下来的只有两行**：第 ④ 行（ZK 的提交是 leader 显式宣布的第二阶段，etcd 的提交是 commitIndex 的隐式推进）与第 ⑤ 行（ZK 的 apply 在内存，etcd 的 apply 落盘）。其余差异都是这两行的推论。

### 同一个语义在两边的物理落点

| 语义 | ZooKeeper 落点 | etcd 落点 | 一致的地方 |
| --- | --- | --- | --- |
| 「谁活着」 | session（服务端 SessionTracker + 客户端 PING） | lease（TTL + KeepAlive 流） | 都是**心跳 + 超时**，都由服务端单方面判定 |
| 「谁是主」 | 临时顺序节点 + 「序号最小者为主」 | lease 绑一个竞争 key + 事务 CAS（`--swap`） | 都是「只有一个客户端能拿到某个 key 的独占」，都不保证拿到的人还活着 |
| 「第几版」 | `zxid`（epoch + counter，单调） | `revision`（main + sub，全局单调） | 都提供**全局单调版本号**，都可用于 fencing |
| 状态在哪 | 内存 DataTree（磁盘只有日志+快照） | bolt 里的 MVCC 版本（内存只有索引） | 都靠「日志 + 快照」恢复 |
| 变更通知 | 服务端 watch 表，**一次性**，只带事件类型 | watch stream，**可续传**，带 KV 与 revision | 都是「事件」，都不带「完整状态」 |

---

## 37.4 共识协议：ZAB 的 epoch 与 Raft 的 term

两者解决的是同一类问题——**「一个 leader 提的案，什么时候算数」以及「旧 leader 什么时候必须闭嘴」**——但表达方式不同，而这套表达方式直接决定了「谁先提交」与「谁能读」这两件日常决策。

### ZAB 的三个阶段

ZAB（ZooKeeper Atomic Broadcast）在每次 leader 更替时走两阶段恢复，然后进入广播：

```text
ZAB：一次 leader 更替 = Discovery + Synchronization，然后长期处于 Broadcast

  ┌─ 阶段 1 · Discovery（定 epoch，不是选票）───────────────────────────┐
  │  新 leader 向各 follower 收集 {acceptedEpoch, lastZxid}               │
  │  ⇒ 收到多数派后，把 epoch 提升到 max(acceptedEpoch)+1                  │
  │  ⇒ 任何旧 leader 拿旧 epoch 的 PROPOSE 都凑不齐多数派 ⇒ 自动失效       │
  └────────────────────────────────────────────────────────────────────┘
  ┌─ 阶段 2 · Synchronization（补齐历史）───────────────────────────────┐
  │  新 leader 对每个 follower 决定：DIFF（补差量）/ TRUNC（截断多余）     │
  │                                / SNAP（整份快照传过去）                │
  │  ⇒ 同步完成后，所有节点拥有"同一个前缀"，且该前缀包含所有已 COMMIT 的写 │
  └────────────────────────────────────────────────────────────────────┘
  ┌─ 阶段 3 · Broadcast（稳态）────────────────────────────────────────┐
  │  client → (follower) → leader：分配 zxid → PROPOSE → 多数派写 txnlog    │
  │  → leader 广播 COMMIT → 各节点 apply 到内存 → 应答                     │
  │  ⇒ 不变量：**zxid 全序**，且任一节点看到 zxid=k 时，k 之前的所有写都已提交 │
  └────────────────────────────────────────────────────────────────────┘

  zxid = (epoch << 32) | counter     epoch 变则 counter 归零
  ⇒ 所以「zxid 大小比较」同时编码了「哪个任期的写」与「该任期第几笔」
```

### Raft 的 term 与 index

Raft 把「任期」和「位置」拆成两个独立的数：`term` 是任期（每次选举 +1），`index` 是日志槽位（跨任期连续、**不重置**）。选举靠随机化的 election timeout 打破平票，提交靠「多数派持久化 + 只对当前 term 的 entry 计数」。

### 图 3 · 脑裂与 fencing：协议只保护集群内部，不保护你的客户端

```text
场景：leader 与多数派之间网络分区（GC / 网卡 / 交换机都长这样）

  ┌──────────── 旧多数派侧（含旧 leader）─────────┐   ┌──── 新多数派侧 ────┐
  │ 旧 leader 仍认为自己是主，仍在给客户端回"成功" │   │ 选出新 leader       │
  │ 但它的 PROPOSE 凑不齐多数派 ⇒ 永远提交不了     │   │ 新 epoch/term 生效  │
  └───────────────────────────────────────────────┘   └────────────────────┘
        │                                                      │
        │ ① 协议层：旧 leader 的写被拒绝（ZK：epoch 不够；Raft：term 不够）
        │ ② 但它回给客户端的那一句"我以为成功了"已经发出去了 —— 这就是假活
        ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │ 所以：拿到租约 ≠ 有资格写数据。真正写数据的下游必须校验单调 token      │
  │   ZK：把 mzxid / 自增号当 token；etcd：把 revision（或 lease 版本）当 token │
  │   下游（数据库 / 存储 / 下游服务）拒绝 token 不大于已见的写 ⇒ fencing 成立 │
  └──────────────────────────────────────────────────────────────────────┘
  ⇒ 这就是「分布式锁」的全部要害（→ comp/46），也是事故二里 2.4 s 双主窗口的唯一解
```

### ZAB vs Raft：一张对照表

| 维度 | ZAB（ZK 3.5+ / 3.8 系列） | Raft（etcd 3.5+） |
| --- | --- | --- |
| 角色 | leader + follower + observer（observer 不投票、不参与 quorum） | leader + follower + candidate；learner（不投票，只追日志） |
| 任期表达 | `epoch`，编码进 zxid 的高 32 位 | `term`，独立字段，每次选举 +1 |
| 位置表达 | zxid 的低 32 位 `counter`，**每个 epoch 从 0 重新开始** | `index`，**跨任期连续、永不重置** |
| 选主依据 | FLE：先比 `lastZxid`（谁的历史更新），再比 `sid`（平票兜底） | 先比 `(lastTerm, lastIndex)`；随机化 election timeout；3.4+ 默认启用 PreVote + CheckQuorum |
| 恢复阶段 | **Discovery → Synchronization → Broadcast**（显式两阶段恢复，再长期广播） | 无显式阶段：RequestVote → AppendEntries 复制 → commitIndex 推进；靠日志匹配性质保证一致性 |
| 提交判据 | 多数派把提案写进 **txnlog**（fsync）后，leader **显式广播 COMMIT** | 多数派**持久化** entry 且 leader 的当前 term 与之匹配时推进 `commitIndex`（只对当前 term 的 entry 计数，历史 entry 随之前移） |
| 日志回滚 | `TRUNC`：把 follower 多余的尾部截断到 leader 的 lastZxid | 冲突 entry 被 leader 的覆盖（log matching） |
| 默认读语义 | **任一副本本地读，可能旧**；`sync()` 是唯一补救 | **linearizable**（ReadIndex / 租约读）；要旧读必须显式 `WithSerializable()` |
| 脑裂防护 | epoch 递增 ⇒ 旧 leader 的 PROPOSE 拿不到多数派 | term 递增 + 多数派心跳确认 ⇒ 旧 leader 无法推进 commitIndex 也无法完成 ReadIndex |
| 典型心跳/超时 | `tickTime`（默认 2000 ms）的倍数关系决定 session 与选举 | `--heartbeat-interval`（默认 100 ms）与 `--election-timeout`（默认 1000 ms） |

### 「谁先提交」与「谁能读」——两句话总结

- **谁先提交**：两边都是「多数派落盘之后」。差别是 **ZK 用一次显式的 COMMIT 广播**（两轮，写延迟多 1 个单向 RTT），**Raft 用 commitIndex 的隐式推进**（一轮 AppendEntries，下次心跳/追加顺手带上 commitIndex）。所以同样硬件下，ZK 的写 P50 通常略低（1 次 fsync），而 etcd 的写 P50 略高（2 次 fsync）——**这两个成本不在同一处，不要用「谁更快」来记。**
- **谁能读**：**ZK 默认任何节点都能立刻读，读到的是「本站已 apply 的前缀」，可能落后 leader 一个 RTT 甚至更多**；**etcd 默认要走 leader 确认（ReadIndex），因此默认就是 linearizable，代价是每次读多一跳**。这一条是选型中最容易被忽略的差异：把 etcd 的读习惯带到 ZK 上，会出现「刚写入的值马上读不到」；把 ZK 的读习惯带到 etcd 上，只是白花一跳 RTT。

---

## 37.5 会话与租约：心跳线程被 STW 卡住，是两边共同的第一故障源

会话（session）与租约（lease）是同一件事的两种写法：**服务端给客户端发一张有期限的「你还活着」的证明，到期没续就作废，作废时把挂在它名下的东西（临时节点 / 绑定的 key）一起删掉。**

### 两边的机制细节

| 维度 | ZK session | etcd lease |
| --- | --- | --- |
| 谁提议期限 | 客户端请求 `sessionTimeout`，服务端**夹在** `[2×tickTime, 20×tickTime]` 之间（默认 tickTime 2000 ms ⇒ 4–40 s） | 客户端 `Grant(ttl)`，TTL 以**秒**为粒度（有最小值，以目标版本文档为准） |
| 怎么续期 | 客户端在连接空闲时发 `PING`（Java 客户端的空闲发送阈值 `pingRwTimeout` 取 `min(1000, readTimeout)` 量级；其他语言客户端常用 `sessionTimeout/3`，**以实现为准**） | `KeepAlive` 是一条**流**，每次收到回执就把 TTL 重置一次；clientv3 的默认保活间隔是 TTL 的 1/3 量级 |
| 谁判定过期 | 服务端 `SessionTracker`，按 `tickTime` 分桶，**按桶批量处理**过期 | leader 的 lessor 扫描过期 lease（约 500 ms 量级一次，**以目标版本为准**） |
| 判定粒度 | `tickTime`（默认 2 s） | 秒级 TTL + 扫描周期 |
| 过期后果 | 删除该 session 名下**全部临时节点**（这些删除本身是一批事务，要过 consensus），并触发相关 watch | 删除该 lease 绑定的**全部 key**，产生一批带新 revision 的 DELETE 事件 |
| 客户端怎么知道 | 收到 `SESSION_EXPIRED` / Curator 的 `ConnectionState.LOST`；**必须新建 session 并重读全量状态** | KeepAlive 通道关闭 / 后续写返回 `ErrLeaseNotFound`；必须重新 `Grant` |
| 关键不变量 | 「session 活着 ⇔ 临时节点在」 | 「lease 活着 ⇔ key 在」 |
| 两边共同的漏洞 | 服务端只知道「多久没收到心跳」，**不知道你还活着** | 同左 |

### 图 4 · 一次 GC 停顿如何变成 2.4 秒双主

```text
时间 ─────────────────────────────────────────────────────────────────────►
T=0s        最后一次心跳到达（服务端 lastTouched = 0）
T=6.7s      【最早】若停顿从心跳刚成功时开始，超过 (T_ttl − h) 即被判死
              T_ttl = 10 s，h = 3.33 s ⇒ 阈值 6.67 s（最坏情况）
T=10s       【必须】服务端判定过期 ⇒ 删临时节点 ⇒ 触发 watch
T=10+ε      备机抢主成功，开始干活
T=12.4s     ★ 原进程 GC 结束，恢复执行 —— 它并不知道自己已被开除
T=12.4–14.8 双主窗口 = (GC 剩余 + 重连 + 事件投递 + 应用响应)
             实测 2.4 s：重连 0.3 s + 事件到达 + "跑完手里这一批"的 1.8 s

  三个数字决定这个窗口：
    ① GC 停顿 P       （不可控，由堆大小与分配率决定）
    ② 心跳间隔 h      （可控，通常取 T_ttl/3）
    ③ 超时阈值 T_ttl  （可控，但越长故障发现越慢）

  ⇒ 误失效的条件：P > T_ttl − h  （最坏情况；平均约 P > T_ttl − h/2）
  ⇒ 双主窗口 ≈ P − (T_ttl − h) + 重连与事件时延
  ⇒ 唯一能让窗口归零的不是「调大 T_ttl」，而是「写数据前校验 fencing token」
```

### 为什么「心跳线程被 STW 卡住」是两边共同的第一故障源

心跳是**应用进程里的一条线程/一个 goroutine 发出去的**。它受三类停顿影响，而这三类都**不出现在网络指标与 QPS 指标上**：

1. **JVM GC 停顿**（ZK 客户端几乎都是 JVM 应用）：32 GB 堆的 G1，P99 可能 300 ms–2 s，P99.9 到 5–15 s。**默认 10 s 的 session/lease 在这条曲线上是不够的。**
2. **容器 CPU 限流**（cgroup `cpu.max` 用尽、被 `throttled`）：Go 的 `runtime` 与 JVM 都不感知，表现为「进程活着但不调度」。
3. **swap / 内存回收**：堆被换出到磁盘时，一次心跳的回执可能要几秒。

> **参数纪律（三条）**：① `T_ttl ≥ 3 × P99.9 停顿`，且实际生产上宁可从 10 s 提到 20–30 s；② 心跳间隔 `h ≤ T_ttl / 3`；③ **任何自称「主」的写入动作之前，必须验证自己仍然是主**——ZK 侧读一次自己那个临时节点的 `Stat`（或直接用 `sync()+getData`），etcd 侧用一次 linearizable `Get` 加 revision 比对，并且下游必须校验单调 token。

```java
// Curator 5.x + ZooKeeper 3.8 系列：会话生命周期、选主、以及"拿到锁之后还要再验一次"
CuratorFramework client = CuratorFrameworkFactory.builder()
        .connectString("zk1:2181,zk2:2181,zk3:2181")
        .sessionTimeoutMs(30_000)      // 服务端会夹在 [2*tickTime, 20*tickTime]；30 s 是为 GC 留余量
        .connectionTimeoutMs(3_000)
        .retryPolicy(new ExponentialBackoffRetry(500, 5))   // 必须有上限 + 退避，否则换主时打成风暴
        .build();
client.start();

// ① LOST 必须清缓存：SESSION_EXPIRED = 临时节点已全删 + watch 全失效，旧状态一律作废
client.getConnectionStateListenable().addListener((c, state) -> {
    if (state == ConnectionState.LOST) { cache.clear(); reconcileFromScratch(); }
});

// ② 选主：临时顺序节点 + 序号最小者；但它只保证"同一时刻只有一个人认为自己持有"，
//    不保证"上一个主已经停手"（GC 停顿可以跨越 isLeader/notLeader 两个回调）
LeaderLatch latch = new LeaderLatch(client, "/elect/batch", hostname);
latch.addListener(new LeaderLatchListener() {
    public void isLeader()  { scheduler.start(); }   // 启动前先自检：见 ③
    public void notLeader() { scheduler.stopImmediately(); }  // 立刻停，禁止"跑完这一批"
});
latch.start();

// ③ 真正写数据前的 fencing：读回自己的节点，用 zxid 当单调 token 交给下游校验
Stat stat = new Stat();
byte[] token = client.getData().storingStatIn(stat).forPath("/elect/batch/_c_xxx-0000000012");
storage.write(batchResult, stat.getMzxid());   // 下游拒绝 token 不大于已见记录的写
```


---

## 37.6 watch：一次性 watch 与 revision 续传，是两种完全不同的「通知」

| 维度 | ZK watch（`getData/getChildren(path, true)`） | etcd watch（gRPC stream） |
| --- | --- | --- |
| 生命期 | **一次性**：触发一次即被服务端移除，必须重新注册 | **流式**：一条流可以持续到连接断开或显式取消 |
| 事件内容 | 只有**类型**：`NodeCreated` / `NodeDataChanged` / `NodeChildrenChanged` / `NodeDeleted`；**不带值** | 带 `KeyValue`（`WithPrevKV` 时还带旧值）+ `ModRevision` + `CreateRevision` |
| 变更后要做什么 | **必须重新读**（`getData` / `getChildren`）才能得到状态 | 事件里已有值，可以直接应用；但**仍建议重读**（见下文「事件不是状态」） |
| 注册点 | 与数据读一起注册（`getData(path, watch)`） | `Watch(key, WithRev(r))`，可显式指定起始 revision |
| 落后/断线 | 客户端自己负责重注册（Curator 的 `CuratorCache`/`TreeCache` 替你做了）；**服务端不为你保留任何「你错过了什么」** | 从「最后处理过的 `ModRevision + 1`」续传；**如果该 revision 已被 compaction，服务端返回 `ErrCompacted`，必须重做「全量 + 从新 revision 开始 watch」** |
| 是否保证你收到全部变更 | **不保证**：漏了就是漏了（事故三的 ZK 侧） | 保证「从指定的、尚未被 compact 的 revision 起，每条变更都在」（`WithProgressNotify` 用于无变更时也能确认自己没落后） |
| 羊群效应 | 强：一个节点被 N 个客户端 watch ⇒ 一次变更 N 条通知 + N 次重读 | 也强，但**每次通知已带值**，省掉 N 次重读（带宽从「60 KB 全量列表」降到「200 B 单条 KV」） |
| 大规模替代方案 | ZK 3.6+ 的 `addWatch`（永久/递归 watch）降低重复注册成本，但语义仍是「事件」 | 单键 watch 无重复注册问题，成本主要落在「watch 数与服务端内存」 |

### 图 5 · watch 丢失的三种方式，以及为什么 revision 是唯一的安全带

```text
【丢失方式一】ZK 一次性 watch 触发后没重新注册（事故三的 ZK 侧）
  客户端 A: getData("/config/app", watch=true)  ──► 变更到 ──► 收到事件 ──► 重注册抛
  ConnectionLoss ──► catch 只打日志 ──► A 从此无 watch ──► 永久停留在旧值（潜伏 7 天）

【丢失方式二】etcd watch 从「当前」开始，而不是从「最后处理到的 revision」开始
  节点 B: 20:03 断线（最后处理到 rev=1000）
          断线期间 rev=1001,1002,1003 三次变更
          20:20 重连：Watch(key)          ← 默认从"现在"开始 ⇒ 丢掉 1001–1003
          正确：Watch(key, WithRev(1001)) ← 从最后处理过的下一个续传

【丢失方式三】compaction 把你要的 revision 删了
  C: 落后 3 天，想从 rev=1000 续传；服务端已 compact 到 rev=90000
     ⇒ 返回 ErrCompacted ⇒ **必须重做：全量 Get → 得到 rev=100500 → 从 100501 开始 watch**
     ⇒ 所以客户端必须有"全量重列 + 幂等 reconcile"这条路径，它不是备选方案而是必需品

  正确的形状（三段式，两边通用）：
    ① 全量读 → 建立状态 + 记下 revision/zxid
    ② 从「全量之后」开始 watch（早于全量的 revision 都要重列）
    ③ 每个事件：应用 + 推进 revision；任何断线/compaction/异常 → 回到 ①
```

```go
// etcd v3.5 + clientv3：三段式（全量 → 从新 revision watch → 每个事件推进 revision）
cli, _ := clientv3.New(clientv3.Config{
    Endpoints:   []string{"etcd1:2379", "etcd2:2379", "etcd3:2379"},
    DialTimeout: 3 * time.Second,
})
ctx := context.Background()

func reconcile() int64 { // ① 全量读，返回"下一个要 watch 的 revision"
    resp, err := cli.Get(ctx, "/config/app") // 默认 linearizable，别加 WithSerializable
    if err != nil { return 0 }
    if len(resp.Kvs) > 0 { apply(resp.Kvs[0].Value, resp.Header.Revision) }
    return resp.Header.Revision + 1
}

rev := reconcile()
for {
    wch := cli.Watch(ctx, "/config/app", clientv3.WithRev(rev), clientv3.WithPrevKV())
    for resp := range wch {
        if resp.Canceled {                        // ② ErrCompacted / 流被取消 → 回到全量
            log.Printf("watch canceled: %v", resp.Err()); rev = reconcile(); break
        }
        for _, ev := range resp.Events {
            apply(ev.Kv.Value, ev.Kv.ModRevision) // ③ 幂等应用；随后推进 revision
            rev = ev.Kv.ModRevision + 1
        }
    }
    time.Sleep(200 * time.Millisecond)            // 退避后重建；**revision 绝不重置为 0**
}
```

### 为什么「通知」不是「状态」

这一段是本篇最想让人记住的：

1. **事件可以丢，状态不能丢。** ZK 的一次性 watch 触发后就不存在了；etcd 的 watch 在 compaction 之后无法从旧 revision 续传。**两个系统都不提供「保证你收到每一条变更」的绝对承诺**——它们提供的是「从某个版本起，顺序地、不重复地」送给你。
2. **事件只有「增量」，状态需要「全量」。** ZK 的事件连值都不带；etcd 的事件带值但不带「与你本地状态的一致性证明」。收到事件时你的本地状态可能是错的（漏了上一条），所以**只做增量应用一定会漂移**。
3. **事件可能比你本地状态更旧。** 断线重连、多路 watch（多个 key 的事件属于不同 revision）时，你会拿到「比已应用的状态更旧的事件」，所以 reconcile 必须幂等且能吸收乱序。
4. **通知不负责「送达之后的行为」。** 收到配置变更通知 ≠ 已经加载了新配置；加载失败、GC 期间堆积、进程重启都在这个缝里（事故三的 7 天潜伏）。

> **一句话**：**watch 只保证「变化会被通知一次」，不保证「你不会漏」，更不保证「你收到时状态是对的」。所以业务侧的正确形状永远是「事件触发 → 全量重读 → 幂等 reconcile → 汇报版本号」，并且要有一个「定期校验版本号」的兜底任务——事故三就是靠一次 CRC 抽查才发现的。**

---

## 37.7 读一致性：`sync()` 与 ReadIndex，两种补救哲学

| 读法 | ZK | etcd | 语义 | 代价 |
| --- | --- | --- | --- | --- |
| 默认读 | 任一节点本地读 DataTree | `Get`（默认 linearizable） | ZK：可能旧（落后一个或几个 zxid）；etcd：linearizable | ZK 极快（μs–百 μs）；etcd 多一次 leader 确认 |
| 显式「读最新」 | `sync(path)` 再读（同一连接上有序） | 无（默认已是最新） | ZK：`sync` 让该 server 追上 leader 的最新 zxid，之后的读有序 | ZK 多 1 个 RTT 到 leader |
| 显式「允许旧读」 | 默认就是 | `WithSerializable()` | etcd：读本地，可能落后（跨成员可到秒级） | 省一次 RTT，换来「可能旧」 |
| 「只要多数派还在」的读 | — | `WithRequireLeader(ctx)` | 分区时快速失败，而不是等超时 | 多一个失败分支要处理 |

### 什么时候必须 linearizable（这条判断决定了很多事故）

| 场景 | 必须 linearizable？ | 原因 | 用错的样子 |
| --- | --- | --- | --- |
| 抢主 / 抢锁：判断「这个 key 现在归谁」 | **必须** | 两个客户端各自读到「锁是我的」⇒ 双主（事故二的 etcd 版本） | stale read 下两个主都开工 |
| 拿到租约后「确认自己仍是主」 | **必须** | 这是 fencing 动作的前置：读到自己那一版才算数 | 只看本地标志位 ⇒ 假活 |
| 配置切换点：判断「我该切到新版本了吗」 | **必须** | 读到旧版本 ⇒ 切换时序错乱（灰度批次倒挂） | 灰度把 100% 流量切到旧规则 |
| 服务列表 / 路由表（有健康检查兜底） | 可以 serializable | 落后几百 ms 的结果由下游超时与重试吸收 | — |
| 监控/巡检读一个计数用于展示 | 可以 serializable | 展示层允许旧 | — |
| 容量决策 / 巡检任务读取「当前成员数」 | 建议 linearizable | 用旧数据做删除/缩容决策会误删 | 缩容时按旧列表把活节点踢了 |

> **ZK 的忠实口径**：ZK 的保证是「**顺序一致性（全序）+ 客户端 FIFO**」。这意味着：同一个 session 内，读到的 zxid 单调不减；但**跨 session、跨 server 的读可以互相落后**。想让「读到自己刚写的」成立，要么在同一个连接上读（通常成立），要么 `sync()` 后再读（一定成立，代价 1 RTT）。**把这两句分清楚，80% 的「ZK 读到旧数据」的争论就结束了。**

---

## 37.8 容量与性能：写永远过 leader，读可以本地

| 项 | ZK | etcd | 决定它的物理事实 |
| --- | --- | --- | --- |
| 写路径 | leader 定序 + 多数派 fsync txnlog + 显式 COMMIT | leader 追 WAL + 多数派 fsync + bolt commit fsync | 写吞吐 ≈ `批大小 ÷ fsync时间 × 组提交摊薄` |
| 写吞吐量级 | 数千 txn/s（组提交、NVMe、同 AZ）；纯小事务可到万级 | 数千 txn/s（两跳 fsync 拖低）；批量写（txn 内多 key）可显著提高 | **不是靠加节点提升**——加 follower 只会增加 leader 的复制负担 |
| 读吞吐量级 | 数万 QPS/节点（本地读，CPU/连接数受限）；加 follower = 加读容量 | 数万–十几万 QPS（`WithSerializable()` 时本地读） | 读可水平扩，**写不可** |
| 客户端连接 | `maxClientCnxns` 默认 60/客户端 IP（防连接风暴） | 无同名限制，但每条 watch/keepalive 都是服务端 goroutine | 连接数 × watch 数 = 服务端内存与调度 |
| 单值大小 | `jute.maxbuffer` 默认 1 MB（包含路径与开销）⇒ **实际可用远小于 1 MB** | `--max-request-bytes` 默认 1.5 MB 量级（以目标版本为准） | 值走进日志与快照，大小直接影响每次写的字节量 |
| keyspace 建议 | DataTree 全在堆里：**几百 MB 舒适，几 GB 开始 GC 抖动** | bolt + 内存索引：**建议 < 数 GB**；`--quota-backend-bytes` 默认 2 GB | 全量状态必须能被一个进程装下 + 快照可整体搬运 |
| 快照与恢复 | 快照 = 整树序列化；恢复 = 加载 + 重放 | snapshot = db 副本（默认每 100,000 条 raft entry 一次）；恢复 = 加载 + 重放 WAL | 恢复时间 ∝ 状态大小（ZK）/ WAL 长度（etcd） |
| 变更通知成本 | 每条 watch 占堆；一次变更 × N 个 watcher | 每条 watch 占内存与一个 goroutine；事件已带值 | 通知是「按订阅者数线性放大」的成本 |

### 为什么它只能存 KB 级数据（四条互相独立的理由）

1. **全量状态必须装进一个进程的内存**（ZK）或一个 bolt 文件（etcd）。这是「任一副本能毫秒级回答 + 快照能整体搬运」的前提，也是硬上限。
2. **每次写都要多数派 fsync**。写放大 = 副本数 × 日志冗余（+ etcd 的第二次 fsync）。存 1 GB 业务数据，要在这条只适合「几千次写/秒」的通道上过 1 GB÷值大小 次。
3. **每次变更都要广播给所有订阅者**。数据越大，事件带的值越大（etcd）或重读返回的字节越多（ZK）。
4. **快照是整份序列化**。DataTree 越大，快照的生成、传输、加载时间越长——而这几段时间里集群的容错余量在下降（新节点加入要 SNAP，恢复要重放）。

> **口算尺子**：**协调组件适合存「几万条、每条几百字节、每天改几百次」的东西；不适合存「几亿条、每条几 KB、每秒改几千次」的东西。** 越过这条线不是调优问题，是形态问题（§37.10）。

---

## 37.9 三本账

三个算例都按「乘法链 + 假设清单 + 量级结论」写；任何一个假设都能替换后重算（复算脚本见本节末）。

### 账一 · 会话误失效窗口 = GC 停顿 + 心跳间隔 + 超时阈值

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 超时阈值 `T` | 生产配置 `sessionTimeout` / lease TTL | 10,000 ms |
| 2 | 心跳间隔 `h` | `T/3`（客户端实现各异，此处按 1/3） | 3,333 ms |
| 3 | 误失效的最坏阈值 | `T − h`（停顿紧跟一次成功心跳时所需的最小停顿） | **6,667 ms** |
| 4 | 加上判定粒度 `g` | ZK `tickTime` 2 s 桶 / etcd lessor 约 0.5 s 扫描 | 有效阈值 ≈ **6.5–7 s** |
| 5 | 双主窗口 | `P − (T − h) + 重连 + 事件投递 + 手头批次` | P=12.4 s 时 ≈ **2.4 s**（实测） |
| 6 | 单实例误失效频率 | 每实例每天大停顿次数 `k` × `P(P > 6.5 s)` | k=6 次/天，分布 P99=2 s、P99.9=9 s ⇒ P≈0.3% |
| 7 | 集群年误失效次数 | `实例数 × 天数 × k × P` | 200 × 365 × 6 × 0.003 ≈ **1,314 次/年 ≈ 3.6 次/天** |
| 8 | 期望业务损失 | `次数 × 双主窗口 × 写入速率 × 无幂等比例` | 3.6 × 3 s × 300 笔/s × 30% ≈ **970 笔/天** |
| 9 | 把 T 提到 30 s | 阈值 20 s ⇒ `P(P>20s) ≈ 0.01%` | ≈ **44 次/年**，代价：故障发现从 10 s 变 30 s |

> **量级结论（非承诺值）**。假设：① `T=10 s`、心跳 `T/3`；② 停顿分布 P99 = 2 s、P99.9 = 9 s，两者之间按对数线性插值；③ 200 个候选主实例、每实例每天 6 次大停顿；④ 双主窗口内写入 300 笔/s、30% 无幂等键；⑤ 判定粒度对阈值的贡献 < 0.2 s。**换假设请按行重算**：堆从 32 GB 降到 8 GB，P99.9 通常落到 1–3 s，误失效会降低一个量级以上；而**无论怎么调 T，双主窗口都不会归零**——只有下游校验 fencing token 才能。

### 账二 · watch 风暴 QPS = 订阅者数 × 变更频率（还有一次「重读放大」）

| 步 | 项 | 推导 | 结果（ZK 侧） |
| --- | --- | --- | --- |
| 1 | 订阅者数 `N` | 网关实例数 | 6,000 |
| 2 | 变更频率 `f` | 600 个 provider 滚动发布 × 2 次（上线+下线）÷ 300 s | 4 次/s |
| 3 | 通知总量 | `N × f` | **24,000 条/s** |
| 4 | 每次通知的客户端动作 | 1 次重读（`getChildren`）+ 1 次重注册 | × 2 = **48,000 请求/s** |
| 5 | 每次重读的字节 | 600 个子节点 × 约 100 B/条 | 约 60 KB |
| 6 | 下行带宽 | `24,000 × 60 KB` | ≈ **1.4 GB/s**（这就是羊群效应） |
| 7 | 换成 etcd 单键 watch | 事件已带值（200 B），不必全量重读 | `24,000 × 200 B` ≈ **4.8 MB/s** |
| 8 | 修法 | 按 hash 把目录拆成 64 份（订阅粒度下降）+ 客户端随机退避 0–3 s + 只订阅自己需要的子集 | 请求量降到 `1/64` 量级 |

> **量级结论（非承诺值）**。假设：① 6,000 个订阅者订阅同一个 children 列表；② 变更频率 4 次/s；③ 每个 `getChildren` 返回 600 个节点、每节点 100 B；④ 客户端收到事件后立刻重读（无退避）。**这张账的价值不在于精确值，而在于那个 1.4 GB/s**：它说明「watch 的成本不是『变更次数』，而是『变更次数 × 订阅者数 × 每次重读的字节』」。ZK 因为「事件不带值」必须重读全量，所以它在「大列表 + 多订阅者」这种形态下先天不利；etcd 因为「事件带值」，同样的场景带宽低三个量级——**但代价是 etcd 要为每个订阅者维护 watch 状态与回放位置**。

### 账三 · 快照 / WAL 磁盘 = 写速率 × 保留 + 状态大小

**ZK 侧**：

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 写速率 | 300 txn/s（临时节点续期、锁心跳、分片进度） | 300 txn/s |
| 2 | 单条事务记录 | 平均值约 250 B（头 + 路径 + 少量数据 + 校验） | 250 B |
| 3 | txnlog 日增量 | `300 × 250 B × 86,400 s` | **≈ 6.5 GB/天** |
| 4 | txnlog 保留 | `autopurge.purgeInterval` 默认 **0 = 不清理** ⇒ 无上限 | 180 天 ⇒ **≈ 1.17 TB** |
| 5 | 快照大小 | DataTree 120 万 znode × 约 350 B | ≈ 420 MB/份 |
| 6 | 快照保留 | `snapRetainCount` 默认 3（+ 期间不完整的） | ≈ 1.5 GB |
| 7 | 单节点磁盘需求 | 步骤 4 + 6；按 180 天保留上限算是 1.17 TB，而事故一的盘只有 **432 GB**，写到满只需约 67 天 | 180 天上限 **≈1.17 TB**；事故现场 **432 GB（约 67 天写满）** |

**etcd 侧**：

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 写速率 | 200 txn/s，单条 raft entry 约 400 B | 80 KB/s |
| 2 | WAL 保留 | 段 64 MB，`--max-wals` 默认 5 量级 ⇒ 大部分被回收 | 稳态 **≈ 320 MB–1 GB** |
| 3 | 真正的增长项 | 每个 key 每次修改留一个 revision，直到 compaction | DB 增量 = `变更速率 × compact 窗口` |
| 4 | 举例：1 MB 的对象每秒更新一次 | `1 MB/s × 300 s`（5 分钟 compact） | **≈ 300 MB 版本空间** + 碎片 |
| 5 | 默认 quota | `--quota-backend-bytes` 默认 2 GB | 触线 ⇒ `NOSPACE` 告警 ⇒ **集群只读** |
| 6 | 到达 quota 的时间 | `(2,000 MB − 活跃数据) ÷ 变更速率` | 活跃 500 MB、200 KB/s ⇒ **≈ 2.1 小时** |
| 7 | 空间回收 | compaction 只标记可回收，**必须 `defrag` 才真正归还**（defrag 期间该成员阻塞） | 巡检项，不是可选 |

> **量级结论（非承诺值）**。ZK 侧假设：① 300 txn/s、平均 250 B/条；② 未开 autopurge；③ 120 万 znode、每 znode 约 350 B（含 stat 与路径）；④ `snapCount` 默认 100,000。etcd 侧假设：① 200 txn/s、entry 约 400 B；② `--max-wals` 与 snapshot 保留为默认量级；③ compact 周期 5 分钟；④ 碎片率 1.2–1.5×。**两个结论都指向同一句话：协调组件的磁盘账不按「数据量」算，按「写次数 × 保留窗口」算**——这就是为什么一个「只存了几百 KB 配置」的集群能把盘写满。

```python
# 三本账复算：改任一假设直接重跑
def session_false_expiry(T_ms=10_000, h_ratio=1/3, gran_ms=500,
                         inst=200, big_pauses_per_day=6, p_gt=0.003,
                         dup_window_s=3.0, write_per_s=300, no_idem=0.3):
    """账一：误失效次数/年 与 期望重复写入笔数/天"""
    thresh = T_ms * (1 - h_ratio) + gran_ms
    per_year = inst * 365 * big_pauses_per_day * p_gt
    per_day_writes = per_year / 365 * dup_window_s * write_per_s * no_idem
    return thresh, per_year, per_day_writes

def zk_disk(txn_per_s=300, bytes_per_txn=250, days=180,
            znodes=1_200_000, bytes_per_znode=350, snap_retain=3):
    """账三(ZK)：txnlog 总增量 + 快照占用"""
    txnlog = txn_per_s * bytes_per_txn * 86_400 * days
    snaps = znodes * bytes_per_znode * snap_retain
    return txnlog, snaps

def etcd_disk(change_bytes_per_s=200_000, compact_window_s=300,
              live_bytes=500e6, quota_bytes=2e9):
    """账三(etcd)：版本空间、到达 quota 的时间"""
    version_space = change_bytes_per_s * compact_window_s
    hours_to_quota = (quota_bytes - live_bytes) / change_bytes_per_s / 3600
    return version_space, hours_to_quota

def watch_storm(subscribers=6_000, changes_per_s=4, children=600,
                bytes_per_child=100, reread=True):
    """账二：ZK 侧下行带宽（事件不带值 ⇒ 必须重读全量）"""
    notes = subscribers * changes_per_s
    payload = children * bytes_per_child if reread else 200
    return notes, notes * payload

t, py, pd = session_false_expiry()
print(f"账一：有效阈值 {t:.0f} ms，误失效 {py:.0f} 次/年，重复写 {pd:.0f} 笔/天")
log_gb, snap_gb = zk_disk()
print(f"账三(ZK)：txnlog {log_gb/1e9:.0f} GB / 180 天，快照 {snap_gb/1e9:.1f} GB")
vs, h = etcd_disk()
print(f"账三(etcd)：版本空间 {vs/1e6:.0f} MB，{h:.1f} 小时到 quota")
n, bw = watch_storm()
print(f"账二：{n:,.0f} 通知/s，下行 {bw/1e9:.2f} GB/s（重读全量）")
```

---

## 37.10 架构边界：它不能当数据库、不能当消息队列、不能存大配置

### 边界表

| 想拿它做什么 | 在物理上为什么不行 | 会怎么坏 | 该用什么 |
| --- | --- | --- | --- |
| **当 KV 存储/数据库** | 每次写要多数派 fsync；全量状态在内存；没有二级索引、没有查询、没有多版本历史 | 写吞吐打满（几千 txn/s）；DataTree/DB 撑到 GB 级后 GC 与恢复时间失控 | 关系库 / LSM（[02 存储引擎三形态](02-storage-engine-three-shapes.md)、`→ comp/17`） |
| **当消息队列** | 没有「消费位点」概念，watch 是一次性事件、不持久、不可回放；写了就只剩「最新值」 | 断线的消费者永久丢失事件（事故三）；没有 ack、没有重试、没有积压 | Kafka / 任务队列（[28 Kafka 的日志](28-kafka-log-segments.md)、[32 任务队列语义](32-task-queue-semantics.md)） |
| **存大配置（几百 KB–MB 级对象）** | 值进入每次写的字节量与每份快照；ZK 单值上限 1 MB，etcd 请求上限 1.5 MB 量级 | 一次配置更新 = 一次大事务，慢到影响所有别的写；快照膨胀 | 对象存储 + 版本号放协调组件（`→ comp/42`） |
| **当分布式锁的「唯一保障」** | 锁服务的可用性买不到「旧主人停手」；会话可误失效 | 双主（事故二） | 锁服务 + **下游 fencing token 校验**（→ comp/46） |
| **高频写（每秒万级以上的计数/状态）** | 写路径在单 leader 上串行定序 + fsync | leader CPU/磁盘打满，全部写入 P99 爆炸 | Redis（可容忍丢）/ 真正存储（[16 一致性哈希与 quorum](16-consistent-hashing-and-quorum.md) 的取舍） |
| **多租户、灰度推送、审计回滚** | 内核只有 KV + watch，没有「按环境/分组/批次」的模型 | 只能靠路径约定硬凑，灰度与回滚全靠应用拼 | 注册配置中心（→ comp/38） |

### ZK vs etcd 选型判定表

| 你的处境 | 选 ZK | 选 etcd | 代价 / 注意 |
| --- | --- | --- | --- |
| 已有 Hadoop / HBase / 老 Kafka 生态（它们自带 ZK 依赖） | ✅ 复用现成集群 | — | 版本与 JVM 运维；`4lw` 白名单要开 |
| K8s / 云原生栈、需要 gRPC + TLS + RBAC + 单键 watch | — | ✅ 事实标准 | 必须做 compaction + defrag + quota 预案 |
| 需要成熟的 Java 客户端套件（选主、锁、cache、barrier 一应俱全） | ✅ Curator 是这条路上的最短路径 | jetcd 可用但生态薄 | Curator 的 cache 会替你处理「一次性 watch 重注册」 |
| 需要「默认读到最新」的语义 | ❌ 默认 stale read | ✅ 默认 linearizable | ZK 上必须 `sync()+get`，成本 1 RTT |
| 需要「订阅一个目录的增删」这种列表型 watch | ✅ watch 原生支持 children 变化 | ⚠️ 只能前缀 watch 后自己聚合 | ZK 的羊群效应要用分片目录治理（§37.9 账二） |
| 需要「事件带值、断线可按版本续传」 | ❌ 一次性、不带值、不续传 | ✅ revision 续传（compaction 之内） | etcd 要处理 `ErrCompacted` 的全量重列 |
| 需要「大 keyspace（数十 GB）」 | ❌ 全内存 DataTree | ❌ quota 默认 2 GB、建议 < 数 GB | 换真正的存储；协调组件只放「指针」 |
| 运维团队只有 Go 栈、想要单二进制 | ZK 要 JVM + 调 GC | ✅ 单二进制 | etcd 的坑在 compaction/defrag/quota，不在进程 |
| 写入常驻超过数千 txn/s | 都不行 | 都不行 | 拆集群（按业务域分成多个 ensemble/集群） |

### 一条被忽略的判定：`写次数` 而不是 `数据量`

选型评审里最常见的错误问法是「我们配置总共才 2 MB，用哪个都行吧？」——**决定协调组件命运的是「写次数 × 保留窗口」**（§37.9 账三），不是数据量。一个 2 MB 的配置集群，如果每个 Pod 每秒刷新一次心跳、每个巡检任务每秒写一次进度，写次数就会把它打穿；反过来一个 200 MB 的静态配置（几乎不改），放进去一点问题都没有。

---

## 37.11 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到物理形态） | 怎么定位 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| ZK 集群某天开始大量 `ConnectionLoss`，写全失败，读还能读 | 失去多数派：节点因磁盘满/GC 退出；**只剩 ≤ quorum−1 台** | `mntr` 看 `zk_server_state` 与 `zk_followers`/`zk_synced_followers`；逐台看 txnlog 目录水位与进程存活 | 扩盘；只保留最新 snapshot 与其后 txnlog；恢复节点至 quorum 以上 | 打开 `autopurge.purgeInterval`（默认 0 = 不清理）；txnlog 目录单独盘 + 80% 告警 |
| 备机突然接管，主机「还活着」并继续写 → 数据重复 | session 误失效（GC 停顿 > `T−h`），临时节点被删 | 对齐两边时钟，比较「ZK 事务时间戳（过期删除）」与主机「GC 日志停顿区间」 | 立刻停写、按业务幂等键对账；把 `sessionTimeout` 提到 `≥3×P99.9 停顿` | **写数据前校验 fencing token**；跑批/选主禁止「只凭本地标志位开工」 |
| 客户端收到 `SESSION_EXPIRED` 后仍在用旧状态 | SESSION_EXPIRED 意味着**临时节点已全删**、watch 已全失效，必须重建 session 并全量重读 | 看客户端是否在 LOST 之后仍持有旧缓存 | 在 `ConnectionStateListener` 里清缓存 + 触发全量 reconcile | 把「LOST → 清缓存」写成框架级强制，不给业务自己选 |
| 某台机器上的配置永远是旧的（潜伏数天） | 一次性 watch 触发后重注册失败被吞掉；或 etcd watch 未带 revision，从「当前」开始 | 抽查各实例「已应用配置的版本号/CRC」并做分布；对比 watch 注册数与订阅者数 | 补「定期版本号校验 + 不一致则全量重列」的兜底任务 | 禁止裸用一次性 watch；用 `CuratorCache`/`PersistentWatcher`，并把「事件 → 全量重读」写进模板 |
| etcd watch 报 `ErrCompacted` / 客户端反复重连 | watch 的起始 revision 已被 compaction 删除 | `etcdctl endpoint status` 看 revision 与 DB 大小；看客户端日志的 `required revision has been compacted` | 客户端实现「全量 Get → 从新 revision 重开 watch」 | 把 `ErrCompacted` 当常态分支而非异常；compaction 周期与客户端最长断线时间对齐 |
| etcd 集群突然只读，写返回 `mvcc: database space exceeded` | 触到 `--quota-backend-bytes`（默认 2 GB）；版本空间 + 碎片吃满 | `etcdctl alarm list` 看 `NOSPACE`；`etcd_mvcc_db_total_size_in_bytes` 与 `..._in_use_in_bytes` 的差 = 碎片 | `etcdctl compact` + `etcdctl defrag`；必要时 `alarm disarm` | 开自动 compaction；把 defrag 写进巡检；大对象不要放 etcd |
| ZK 的 watch 一变更，整个集群 CPU 飙到 100% | 羊群效应：一个 children 列表被 N 个客户端 watch | `mntr` 的 `zk_watch_count` 与请求数尖峰对齐变更时刻 | 拆目录（按 hash 分片）+ 客户端随机退避 | 把「订阅粒度」当容量指标评审，禁止「全量列表 watch」 |
| ZK 客户端连接数暴涨，老连接超时 | 客户端重启后建连风暴；`maxClientCnxns` 默认 60/客户端 IP 把重试挡在门外 | `mntr` 的 `zk_num_alive_connections`；按 IP 聚合连接数 | 加 DNS/SLB 抖动 + 客户端重试退避（指数 + jitter） | 重试策略必须有上限 + jitter；避免同一 Pod 内多线程各建一个 client |
| etcd 的 `etcd_disk_wal_fsync_duration_seconds` P99 飙高，写入慢 | WAL 与数据共盘、或盘被别的进程抢 IO（日志采集/备份） | 对比 WAL 目录所在盘的 util 与 iowait；看该成员的 fsync 直方图 | 隔离 WAL 盘；停掉非存储负载 | 部署纪律：etcd 独占盘，禁止同盘跑采集/备份 |
| etcd 偶发 leader 变更 | 网络抖动或 CPU 限流导致心跳超时（默认 heartbeat 100 ms / election 1000 ms） | `etcd_server_leader_changes_seen_total`；`etcd_server_heartbeat_send_failures_total` | 修网络/提升 CPU 配额；跨 AZ 部署时把 election timeout 调大（有代价：故障发现变慢） | leader 变更必须是 0 的告警项，任何非零都当事故复盘 |
| ZK 重启后恢复要十几分钟 | 恢复 = 加载整树快照 + 重放 txnlog；DataTree 越大越慢 | 看启动日志的 snapshot 读取与 txnlog 重放条数；对比 `zk_approximate_data_size` | 降 DataTree 规模；缩短快照间隔（代价：快照 IO 更频繁） | 把「恢复时间」写进演练目标（例如 < 2 分钟） |
| 迁移到 etcd 后「刚写的配置读不到」 | 客户端用了 `WithSerializable()` 而不自知（或读了 follower 的本地状态） | 看客户端代码里 Get 的 Option；看响应 header 的 `raft_term`/`revision` | 去掉 `WithSerializable()`；或读后校验 revision 是否 ≥ 写入返回的 revision | 「读自己刚写的」必须显式 `Get` 默认模式 + revision 校验 |
| 临时节点数无限增长，ZK 内存缓慢上涨 | 客户端一 Pod 一临时节点 + Pod 频繁重建但旧 session 未及时过期（`sessionTimeout` 太长） | `mntr` 的 `zk_ephemerals_count` 与 Pod 数对比 | 缩短 `sessionTimeout`；清理僵尸客户端 | 临时节点命名带实例唯一 ID；监控 ephemerals ≈ 实例数 |

---

## 37.12 观测：五个数覆盖 90% 的故障

**每条都带版本与开关前提**——指标名写错一个，读者就会认为全篇没验证过。ZK 侧的名称以 `mntr`（四字命令）与 3.6+ 的 Prometheus provider 常见形态给出；etcd 侧以 3.5/3.6 的 `/metrics` 常见形态给出，**名称与默认值以目标版本文档为准**。

| 系统 | 指标 | 版本 / 开关前提 | 拐点（越线就动手） | 它度量的是哪件物理事实 |
| --- | --- | --- | --- | --- |
| ZK | `zk_server_state` + `zk_synced_followers` | `4lw.commands.whitelist` 必须含 `mntr`（3.5 起默认只允许 `srvr`） | `synced_followers < quorum−1` 立即 P0 | 是否还拥有多数派（能不能写） |
| ZK | txnlog 目录使用率（`du`/node exporter） | 任何版本；需按 `dataDir` 单独挂盘 | > 80% 停非关键写；**> 90% 就会走事故一的路** | 顺序日志的物理水位（与业务数据量无关） |
| ZK | `zk_outstanding_requests` | `mntr` | 持续 > 数百且上升 = 请求在 leader 上排队 | 写路径的排队深度（fsync 拖慢的直接信号） |
| ZK | `zk_avg_latency` / `zk_max_latency` | `mntr`（单位 ms，服务端视角） | `avg` 相对基线翻倍，或 `max` 反复 > 100 ms | 写路径端到端（含 fsync 与 quorum） |
| ZK | `zk_watch_count` / `zk_ephemerals_count` | `mntr`（3.6+ 也有对应 Prometheus 指标） | watch 数突增 = 订阅放大；ephemerals 与实例数偏离 = 会话泄漏 | 通知成本与「谁活着」的规模 |
| ZK | JVM GC 停顿（`jvm_gc_pause_seconds`，Micrometer 形态） | 需 JVM agent/exporter；`-XX:MaxGCPauseMillis` 只影响目标不影响实际 | `max` 逼近 `sessionTimeout − 心跳间隔` 即 P0（账一的阈值） | **会话误失效的唯一前兆**，且它不在任何 ZK 指标里 |
| etcd | `etcd_server_leader_changes_seen_total` | 各版本均有 | **任何非零增长都是事故**（稳态必须是常量） | 共识层的稳定性 |
| etcd | `etcd_disk_wal_fsync_duration_seconds`（P99） | 各版本直方图 | 官方口径的警戒值在 10 ms 量级；持续越线先查盘 | 写路径第一跳 fsync |
| etcd | `etcd_disk_backend_commit_duration_seconds`（P99） | 各版本直方图 | 官方口径警戒值在 25 ms 量级 | 写路径第二跳 fsync（bolt 提交） |
| etcd | `etcd_server_proposals_failed_total` / `..._slow_apply_total` | 各版本 | 持续增长 = 提交或应用跟不上 | Raft 层的失败与积压 |
| etcd | `etcd_mvcc_db_total_size_in_bytes` vs `..._in_use_in_bytes` | 各版本 | 前者逼近 `etcd_server_quota_backend_bytes` 时 P0；两者差值 = 碎片，需 defrag | keyspace 与碎片（账三） |
| etcd | watch 相关计数（`etcd_debugging_mvcc_watcher_total` 等，**3.5 起名称有调整**） | 名称随版本变化，以目标版本文档为准 | 突增 = 订阅放大；下降 = 客户端在掉线 | 通知成本与订阅者健康 |
| etcd | `etcd_server_heartbeat_send_failures_total` | 各版本 | 非零增长 = 到某个 follower 的心跳不稳定 | 选举抖动的直接原因 |

### 三条判读纪律

1. **先分「不能写」与「写得慢」。** 不能写优先查多数派（`zk_synced_followers`、etcd 的 `has_leader`）；写得慢优先查 fsync 与排队（`zk_outstanding_requests`、两个 fsync 直方图）。两者的修法完全不同，混着查会浪费掉事故的黄金十分钟。
2. **协调组件的「假活」必须先看客户端侧。** 服务端指标全绿时双主也在发生：所以必须有「同一资源持有者数量 > 1」的业务侧监控（对账跑批的主数量、锁持有者数量），这是唯一能提前发现误失效的信号。
3. **容量告警要盯「写次数 × 保留」，不盯「数据量」。** txnlog 目录水位、etcd DB size/quota、DataTree 大小（`zk_approximate_data_size`）这三个数才是容量；「我们只存了 2 MB 配置」不是。告警阈值与 SLO 的表达方式见 [../eng/21 SLO 告警](../eng/21-slo-alerting.md)。

```bash
# ZK 侧：四字命令与目录水位（3.5+ 记得把 mntr 加进 4lw.commands.whitelist）
echo mntr | nc zk1 2181 | grep -E 'zk_server_state|zk_synced_followers|zk_outstanding_requests|zk_watch_count|zk_ephemerals_count|zk_avg_latency|zk_approximate_data_size'
du -sh /data/zookeeper/version-2                  # txnlog + snapshot 的实际水位
ls -1 /data/zookeeper/version-2/log.* | wc -l     # 日志段数量：没有 autopurge 时节数只增不减

# etcd 侧：健康、revision 与 quota、告警、碎片
etcdctl endpoint status --cluster -w table        # 看 DB SIZE / IS LEADER / RAFT TERM / RAFT INDEX
etcdctl endpoint health --cluster -w table
etcdctl alarm list                                # NOSPACE 会出现在这里，出现即集群只读
etcdctl compact $(etcdctl endpoint status -w json | python3 -c 'import json,sys;print(json.load(sys.stdin)[0]["Status"]["header"]["revision"])')
etcdctl defrag --cluster                          # 归还碎片；期间该成员阻塞，要逐台做
```

---

## 37.13 一页速记

| 记什么 | 具体结论 |
| --- | --- |
| **一句话本质** | 协调组件 = 小数据集 + 共识 + 变更通知；只有三类数据：谁活着、谁是主、第几版 |
| **唯一真问题** | 会话失效与一次性 watch 是全部坑的来源——前者被误判就双主，后者丢一次就永久旧值 |
| **ZK 形态** | 内存 DataTree + txnlog（1 次 fsync）+ 模糊快照；`autopurge.purgeInterval` 默认 **0 = 不清理** |
| **etcd 形态** | bolt 里的 MVCC 版本 + WAL + snapshot；一次写 **2 次 fsync**（WAL + bolt commit） |
| **ZAB vs Raft** | zxid = epoch 高 32 位 + counter 低 32 位（epoch 变则 counter 归零）；Raft 的 index 跨任期连续不重置 |
| **谁先提交** | 都是「多数派落盘之后」；ZK 多一条显式 COMMIT 广播，etcd 靠 commitIndex 隐式推进 |
| **谁能读** | ZK 默认本地读、**可能旧**，`sync()+读` 才能最新；etcd 默认 linearizable，`WithSerializable()` 才旧读 |
| **会话/租约** | 判据只有一个：心跳有没有按时到。GC 停顿、CPU 限流、swap、真死**在协议里同形** |
| **误失效阈值** | 停顿 `P > T_ttl − 心跳间隔` 就会误失效（10 s 阈值 + 1/3 心跳 ⇒ 约 6.5 s） |
| **watch** | ZK 一次性、不带值、丢了自己负责；etcd 流式、带值、按 revision 续传、compaction 后必须重列 |
| **通知不是状态** | 正确形状：`事件 → 全量重读 → 幂等 reconcile → 上报版本号`，外加定期版本校验兜底 |
| **容量账** | 按「写次数 × 保留窗口」算，不按数据量算；ZK txnlog 无上限会写满盘；etcd 触 quota ⇒ 只读 |
| **第一指标** | ZK：`synced_followers` + txnlog 水位 + JVM GC 停顿；etcd：leader 变更 + WAL fsync P99 + DB size/quota |
| **换零件信号** | 写入常驻 > 数千 txn/s、keyspace > 数 GB、需要灰度/多租户/历史版本（→ comp/38） |

**如果只记五句：**

1. **它是「小」的，因为每次写都要过半数落盘。** 小是共识的价格，不是设计偏好；所以它永远只能存「谁、哪版、是否活着」。
2. **会话与租约都是单方面判定的死亡证明。** 你的进程可能只是被 GC 停住了；能救你的不是更长的超时，而是下游校验的 fencing token。
3. **watch 是事件，不是状态。** 一次性、会被 compaction 抹掉、可能比你本地状态还旧——收到通知后必须全量重读。
4. **ZK 默认读到旧数据，etcd 默认读到最新。** 这条差异决定了「抢主、切配置」的代码该怎么写，也决定了迁移时哪一段会突然变慢。
5. **磁盘账按「写次数 × 保留窗口」算。** 一个只存几百 KB 配置的集群，能被 300 txn/s 的 txnlog 在 180 天里写成 1.17 TB；事故一的 432 GB 盘只撑了约 67 天。

---

## 37.14 自检题（5 题）

**Q1.** 某团队用 ZK 做选主，`sessionTimeout=10 s`，主节点堆 32 GB。他们观察到一年内发生 3 次双主，每次都伴随一次 8–13 秒的 Full GC。请给出：① 为什么 GC 会导致双主（用本篇的阈值公式）；② 为什么「把 sessionTimeout 调到 60 s」不是正确答案；③ 正确的三道防线。

<details><summary>答案</summary>

**① 机制**：ZK 的过期判据只有一个——「多久没收到这个 session 的任何请求」。心跳由应用进程里的一条线程发送，Full GC 期间它不发。误失效的最坏阈值是 `T − h`（`T=10,000 ms`、`h≈T/3=3,333 ms` ⇒ **约 6.5 s**，再叠加 `tickTime` 2 s 的分桶粒度）。所以 8–13 s 的停顿必然被判死：服务端删临时节点 → 备机抢主 → 原进程 GC 结束后仍以为自己持有锁，直到重连收到 `SESSION_EXPIRED` 才停手。双主窗口 ≈ `P − (T−h) + 重连 + 手头批次`，13 s 的停顿可能带来 5 s 以上的窗口。

**② 为什么调大不是答案**：`T` 越大，故障发现越慢（真的挂了要 60 s 才切换），而且堆 32 GB 的 P99.9 停顿本身可能到 20 s 以上——**你永远在和一条没有上限的尾巴赛跑**（§37.9 账一第 9 步：把 T 提到 30 s 只把 1,314 次/年降到几十次）。同时更大的 `T` 意味着「僵尸 session 与临时节点」保留更久，ephemerals 会积累。

**③ 三道防线**：
1. **降低停顿本身**：主节点不要背 32 GB 堆的业务缓存；把跑批/选主进程与业务缓存分进程；GC 日志 + `jvm_gc_pause_seconds` 告警在 `T − h` 之下。
2. **提 T 并缩短心跳**：`T ≥ 3 × P99.9 停顿`、`h ≤ T/3`（这是「减少误失效次数」，不是「消除」）。
3. **fencing**：写数据前校验单调 token（ZK 侧可用 `mzxid`/自增号，etcd 侧可用 revision）；下游存储拒绝「token 不大于已见」的写。**只有这一道能把双主窗口的业务后果归零。**

量级结论，非承诺值；假设见 §37.9 账一。
</details>

**Q2.** 一个 etcd 集群（3 节点）跑了 8 个月，运维发现 `etcd_mvcc_db_total_size_in_bytes` 到了 1.9 GB，写入开始偶发 `database space exceeded`，业务侧「配置永远推不下去」。给出：① 这个 1.9 GB 从哪来（按写速率推一次）；② 为什么「删掉很多 key」不解决问题；③ 正确的处置顺序。

<details><summary>答案</summary>

**① 来源**：etcd 的 DB 增量 = **变更速率 × compaction 窗口** + 碎片（§37.9 账三）。假设每秒 200 次写、单条 entry 与版本开销合计 400 B–1 KB，5 分钟 compact 窗口 ⇒ 版本空间每秒 80–200 KB ⇒ 每天 7–17 GB 的**写入量**；真正留下的是「窗口内未被 compact 的版本 + 碎片」，碎片率 1.2–1.5× 时，一个活跃数据只有几百 MB 的集群到 1.9 GB 是常态。**关键点：它和「存了多少配置」几乎无关。**

**② 为什么删 key 没用**：删除在 MVCC 里是**又写了一个新版本（tombstone）**，DB 反而更大；旧版本只有 compaction 才能标记回收，而 **compaction 只标记、不归还空间**——boltdb 的空闲页留在文件里，文件大小不会缩小。必须 `defrag` 才真正把空间还给文件系统（且 defrag 期间该成员阻塞读写，要逐台做）。

**③ 处置顺序**：
1. **止血**：`etcdctl alarm list` 确认 `NOSPACE`；先 `compact` 到当前 revision，再 `defrag` 一台，然后 `alarm disarm`（一次一个成员，避免同时阻塞影响多数派）。
2. **治本**：打开自动 compaction（`--auto-compaction-mode=periodic --auto-compaction-retention=1h` 量级），并把 `--quota-backend-bytes` 从默认 2 GB 提到你能承受的水平（官方建议上限在 8 GB 量级，**以目标版本文档为准**）。
3. **治形态**：找出「谁在狂写」——常见是 1 MB 级对象（大 ConfigMap / 状态快照）每秒更新。把它搬到对象存储（`→ comp/42`），etcd 里只留指针与版本号；把高频状态从 etcd 挪到别的存储。
4. **兜底**：「配置永远推不下去」说明客户端没有 `ErrCompacted` / 全量重列路径，必须补上（§37.6 图 5）。

量级结论，非承诺值。
</details>

**Q3.** 6,000 个实例 watch 同一个 `/nodes` 目录（下面 600 个临时子节点），滚动发布期间每分钟约 240 次上下线。有人算「每分钟 240 次变更，ZK 完全扛得住」。错在哪？给出正确的量级与两条治理手段。

<details><summary>答案</summary>

**错在漏掉了「订阅者数」这个乘号**。通知量 = 订阅者数 × 变更频率 = `6,000 × 4 次/s = 24,000 条/s`；而 ZK 的 watch **不带值**，每个客户端收到事件后必须 `getChildren` 重读全量（600 × 100 B ≈ 60 KB）并重新注册 watch ⇒ 请求 48,000/s、下行 ≈ **1.4 GB/s**（§37.9 账二）。「每分钟 240 次」是变更侧的数字，它乘以 6,000 才是协调组件的负载。

**正确的量级**：单向通知 24,000/s 在集群层面不算天文数字，但「× 60 KB 的重读」把带宽推到 GB/s 级，同时 6,000 个客户端在同一毫秒内重注册会把 leader 的写路径打出一个尖峰（watch 注册本身也是请求）。

**两条治理手段**：
1. **减小订阅粒度**：按 hash 把目录拆成 64 份（每份约 10 个节点），每个实例只 watch 自己需要的那份 ⇒ 通知量降到 `1/64` 量级，单次重读从 60 KB 降到 1 KB。
2. **客户端随机退避**：事件到达后加 0–3 s jitter 再重读，把「同一毫秒的 6,000 个请求」摊平成「3 秒内的 6,000 个请求」——**这只削峰，不减少总量**，所以必须和第 1 条一起用。

补充：换 etcd 的单键 watch 能把「事件带值」这一项拿掉（4.8 MB/s），但**没有解决「一个变更 × N 个订阅者」的本质**——只要订阅者是 6,000，通知数就是 24,000/s。真正的形态解是「分级推送」：由少数聚合器 watch，再向下广播（那就是 → comp/38 注册配置中心解决的事）。

量级结论，非承诺值。
</details>

**Q4.** 一个系统从 ZK 迁到 etcd，代码里把 `getData(path, true)` 的用法改成了「启动时 `Get` 一次 + `Watch(key)`」。上线后出现两类新问题：① 配置更新后有些实例要过很久才生效；② 某些实例在 etcd 重启后彻底收不到更新。分别给出根因与修法。

<details><summary>答案</summary>

**① 生效慢**：`Watch(key)` **未带 `WithRev`**，默认从「当前 revision 之后」开始。启动时先 `Get` 再 `Watch` 之间存在一个空窗——Get 之后、Watch 建立之前发生的变更全部丢失（§37.6 图 5 的第二种丢失）。而且如果客户端把 Watch 放在一个「断线不重建」的循环里，漏掉的事件会产生「要过很久才生效」（下一次变更才顺带纠正）。

**修法**：`Get` 的响应里取 `Header.Revision`，然后 `Watch(ctx, key, WithRev(rev+1))`；每个事件处理完后把 `rev` 推进到 `ev.Kv.ModRevision`；断线重连一律从 `rev+1` 续传。

**② etcd 重启后彻底收不到**：两个常见原因。**(a)** 客户端用了 `WithSerializable()` 或本地缓存，读到的是旧 revision，然后从一个「已被 compaction 的 revision」去 watch ⇒ `ErrCompacted`，流被关闭；如果代码把这当成普通错误并 `return`，watch 就永久消失了。**(b)** 服务端重启后客户端需要重新建立 watch 流，而客户端的重连逻辑没有「从最后 ModRevision+1 重开」的路径（很多实现会默认从「现在」开始）。

**修法**：把 `ErrCompacted` 当**常态分支**：收到就执行「全量 `Get` → 记新 revision → 从新 revision 重开 watch」。再加一层兜底：每 N 分钟做一次「版本号校验」（写入方把 revision 写进配置内容或旁路 key，客户端比对），不一致就全量重列——这正是事故三里最后发现问题的那个手段（CRC 抽查），把它自动化。

**一句话**：ZK 的一次性 watch 逼着你写「每次重注册」；etcd 的流式 watch 逼着你写「每次从 revision 续传 + compaction 重列」。**两种形态都不能让你「订阅一次，永远正确」。**

量级结论，非承诺值。
</details>

**Q5.** 老板问：「我们已经有一个 Redis，用 Redis 存选主锁和配置不就行了吗？为什么要多运维一套 ZK/etcd？」请用本篇的三条物理事实回答，并说出在什么场景下 Redis 反而更合适。

<details><summary>答案</summary>

**三条物理事实**：
1. **Redis 主从是异步复制，没有「多数派提交」**。主挂掉时从库可能缺最后几条写：一个刚写进去的锁「消失」，或者一个刚过期的锁「复活」——这在协调语义里是**两种都不可接受**的方向。ZK/etcd 的写是「多数派落盘才应答」，选主的结论不会被一次宕机改写。
2. **Redis 没有单调、可跨重启比较的全局版本号**。它有的 `INCR` 是单实例的、会随故障切换回退（failover 后可能倒退）。协调语义需要「它是第几版」这个可比较、可 fencing 的数（zxid / revision）。没有它，下游无法拒绝旧主人的写。
3. **Redis 的过期是「被动 + 定期删除」，不是「共识过的删除」**。一个 key 过期不会产生一条「全网有序的删除事件」；而 etcd 的 lease 过期会删 key 并产生带 revision 的 DELETE 事件，ZK 的 session 过期会删临时节点并触发 watch。**「删除即事件」是协调语义的必需品**，否则订阅者永远不知道该重读。

另外还有一条工程事实：Redis 的持久化（AOF/RDB）与协调组件的日志**性质不同**——前者是「尽量不丢缓存」，后者是「不丢就等于不丢，丢了就作废」（[02 存储引擎三形态](02-storage-engine-three-shapes.md) 的账在协调组件上的极端版）。

**Redis 更合适的场景**：
- 锁只是**「减少重复劳动」而非「防止错误」**：例如「同一个报表任务只让一个实例跑」，重复跑的后果只是浪费 CPU（幂等且无害）。
- 需要**极高吞吐**的短租约（每秒数万次抢锁），且能接受「极少数情况下两个实例同时进入」。
- **缓存失效通知**、限流计数这类「丢失可接受、下一条会覆盖」的场景。
- 但只要有「双主就赔钱」的业务（扣款、发货、库存、账务），就必须用有共识的组件，并且仍然要 fencing。

量级结论，非承诺值；Redis 的具体持久化与复制语义见 `→ comp/14`，共识算术见 [16 一致性哈希与 quorum](16-consistent-hashing-and-quorum.md)。
</details>

---

## 37.15 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| WAL + 快照 + 重放：ZK 的 txnlog／etcd 的 WAL 完全复用这套套路，本文不再解释「为什么先写日志」 | [03 WAL、缓冲池与崩溃恢复](03-wal-bufferpool-crash-recovery.md) §03.2、§03.5 |
| 多数派与 quorum 算术：本文假设「过半数落盘」是常识，推导在那篇 | [16 一致性哈希与 quorum](16-consistent-hashing-and-quorum.md) §16.4 |
| Raft + 存储引擎的组合形态：etcd 是「Raft + 单机 KV」的最小样本，TiKV 是放大版 | [19 RocksDB 与 TiKV](19-rocksdb-and-tikv.md) §19.5 |
| 日志与位点语义：想让协调组件「当消息队列」，先看 Kafka 为什么必须承诺位点 | [28 Kafka 的日志](28-kafka-log-segments.md) §28.3、§28.9 |
| 复制与 `acks`：本文的「多数派落盘才应答」与 Kafka 的 ISR/HW 是两套不同的保证 | [29 复制与一致性](29-kafka-replication-isr.md) §29.5 |
| 「消息被取走但没做完」：租约语义在任务队列上的完整形态（可见性超时、死信） | [32 任务队列语义](32-task-queue-semantics.md) §32.4、§32.7 |
| 语义形状对照：NATS core 的 at-most-once 与 ZK watch 的「事件不保证送达」是同一家族的问题 | [31 消息族谱](31-message-family.md) §31.2、§31.7 |
| 定时与延迟：ZK 的临时节点常被拿来当「租约超时」，与时间轮的精度取舍对照读 | [36 定时与延迟](36-timing-delay-queue.md) §36.3 |
| 存储引擎三形态：为什么「只追加日志」在协调组件上是唯一正确形态 | [02 存储引擎三形态](02-storage-engine-three-shapes.md) §02.4、§02.6 |
| 读路径与缓存形态：ZK 的「本地读 = stale」是读路径设计的一般结论的一个实例 | [../architect/04 读路径与缓存形态](../architect/04-read-path-and-cache-shape.md) |
| 分布式锁与选主：fencing token 的完整论证（本文 §37.5 只给结论） | `→ comp/46` |
| 注册中心与配置中心：watch 风暴、健康检查误判、配置灰度三件事的工程解 | `→ comp/38` |
| 存储选型与量级换挡：什么时候该把「协调」交给别的形态 | [../eng/35 存储选型](../eng/35-storage-model-selection.md) |
| 「只追加日志 vs 可编辑状态」的架构级对照与容量估算 | [../architect/15 消息存储](../architect/15-message-store.md) |
| 投递语义与幂等：本文反复出现的「幂等 reconcile」= 至少一次 × 幂等的同一句话 | [../eng/24 投递语义](../eng/24-delivery-semantics.md) §24.2 |
| 可靠性 checklist：把「协调组件挂了谁会停」写进演练清单 | [../eng/26 消息可靠性](../eng/26-message-reliability.md) |
| 组件故障谱：协调组件对应「脑裂」与「假活」两类，收口在 52 篇 | `→ comp/52` |
| 可观测：`synced_followers`、fsync 直方图这类结构性指标的告警写法 | [../eng/21 SLO 告警](../eng/21-slo-alerting.md) |
