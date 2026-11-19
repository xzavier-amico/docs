# 19. RocksDB 与 TiKV：可嵌入 LSM + Raft + PD

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · LSM 族与分析 ｜ **编号**：19 / 53
> **本篇定位**：Part IV 的第三篇，也是全书的「NewSQL 内核篇」。LSM 的通用机制（`T×L` 写放大、leveled vs tiered、三种 write stall）全部锚 `→ comp/02`，本篇一个字节都不重写；只回答一件 02 篇回答不了的事：**把一个 LSM 做成「库」而不是「数据库」，再在库外面套一圈 Raft 与一个全局调度器，会得到什么，代价记在谁的账上。** InnoDB 侧的对照（页、行、undo、Read View）锚 `→ comp/05`、`→ comp/06`；HBase 的 Region 辨析锚 `→ comp/18`。
> **读法**：§19.1 三起事故先建立「容量模型错、不是参数错」的直觉；§19.6 是全篇支点（三件套 + 链路图 + 每跳耗时）；§19.7 讲清 Percolator 的三个「为什么」；**§19.8 是唯一可复算的一节**，四个算例的脚本在 §19.11 里能直接跑。只想拿结论的读 §19.12。

---

## 19.1 引子：三起「换了个 NewSQL，P99 反而翻了五倍」的故障

NewSQL 的故障有个共同的形状：**迁移报告全绿、压测报告全绿、上线两周后曲线开始爬坡。** 它不长成「报错」，长成「越来越慢」「某台机器特别忙」「盘满得比预期早」，而根因都在三个月前那张容量评审表上——那张表只问了「存多少数据」，没问「一次写要写几遍」。

### 事故一：按 MySQL 的容量模型扩容，P99 从 20 ms 到 130 ms（2026-03-05，某订单履约中心）

某订单状态表从 MySQL 8.4 迁到 TiKV 上的 TiDB（5 个 TiKV 节点，3 副本）。表 2 亿行、行值 400 B。业务侧每处理一次履约事件就回写一次 `order_state` 与 `updated_at`——**日均 2 亿次逻辑写 = 80 GB/天逻辑写入**。

迁移前的容量评审用的是 MySQL 的口径：写放大 7.3×（`→ comp/02` §02.8 的 B+ 树侧），所以盘带宽按 `80 GB × 7.3 = 584 GB/天 ≈ 6.8 MB/s` 备了 30 倍余量，结论是「写这块完全不是瓶颈」。

迁完才知道，TiKV 的一次逻辑写要过三本账（§19.8 账一）：**RocksDB 的 LSM 写放大 46× × Raft 日志 2× 量级 × 副本数 3 = 144×**。同一个 80 GB/天，物理写变成 **11.5 TB/天**，是 MySQL 口径的 19.7 倍。

| 时间 | 事件 | 逻辑写速率 | 全集群物理写 | 每节点盘写 | pending compaction | P99 |
| --- | --- | --- | --- | --- | --- | --- |
| 03-05 08:00 | 迁移完成，压测通过（**压测只打点查**） | 1.0 MB/s | 144 MB/s | 29 MB/s | 4 GB | 21 ms |
| 03-05 09:30 | 早高峰 | 3.0 MB/s | 432 MB/s | 86 MB/s | 21 GB | 27 ms |
| 03-05 20:00 | 晚高峰 ≈ 峰值 4.6 MB/s | 4.6 MB/s | 663 MB/s | 133 MB/s | 58 GB | 48 ms |
| 03-05 21:10 | `pending_compaction_bytes` 越过软限 64 GB | 5.1 MB/s | 734 MB/s | 147 MB/s | 67 GB | 96 ms |
| 03-05 21:40 | L0 文件数越过 `level0_slowdown_writes_trigger`，写线程被 sleep | 5.4 MB/s | 778 MB/s | 盘 util 96% | 79 GB | **130 ms** |
| 03-05 22:05 | 扩容 2 个 TiKV 节点 → Region 迁移抢走 180 MB/s 盘带宽 | — | 迁移额外流量 | 100% | 96 GB | 210 ms |
| 03-05 23:20 | 限速 compaction + 把「水位回写」改成异步批写 | 2.0 MB/s | 288 MB/s | 58 MB/s | 41 GB | 45 ms |
| 03-06 复盘 | 4 小时 P99 > 100 ms，`SLA` 赔付 62 万元 | — | — | — | — | — |

**三件事同时错了**：① 容量模型用「逻辑字节 × MySQL 写放大」而不是「逻辑字节 × LSM 写放大 × 副本数」；② 压测只测单条点查延迟，不测「一天要重写多少字节」；③ 扩容是错的解法——**多一个 TiKV 节点不会让一个已有 Region 的 compaction 变快，反而先搬一遍数据抢带宽**。

### 事故二：主键是自增的，单 Region 打满，扩容 6 个节点无效（2026-01-22，某支付账户库）

`t_pay_account` 1.4 亿行，主键沿用 MySQL 的 `BIGINT AUTO_INCREMENT`。TiKV 按 key 区间切 Region，而自增主键意味着**新写入永远落在最后一个 Region 的右边界**：所有新开户、所有对最新一批账户的扣款，全部压在同一个 Region 的同一个 leader 上。

那个 leader 在 node-3 上。node-3 的 raftstore 与 apply 线程 CPU 100%，其余 5 个节点稳定在 28%–34%。

| 时间 | 事件 | 热点 Region 写 QPS | node-3 CPU | 其他节点 CPU | 现象 |
| --- | --- | --- | --- | --- | --- |
| 01-22 10:00 | 大促预热 | 8,000 | 62% | 30% | 正常 |
| 01-22 20:00 | 峰值 | 21,000 | 88% | 33% | 单 Region 队列开始涨 |
| 01-22 20:25 | 越过单 Region 上限（约 1 万 QPS 量级，§19.8 账二） | 34,000 | 100%（含 raftstore 与 apply 抢核） | 35% | P99 从 8 ms → 190 ms |
| 01-22 20:40 | 加 2 个节点 | 35,000 | 100% | 31% | **无效**：新节点接不到那个 Region 的写 |
| 01-22 21:10 | 加到 11 个节点（共扩 6 个） | 36,000 | 100% | 33% | 仍然无效；且 PD 在搬 Region 抢带宽 |
| 01-22 22:00 | 变更主键为 `AUTO_RANDOM`（在线 DDL，重建表 + 双写切换） | 打散到 2.1 万 Region | 41% | 44% | 恢复 |
| 01-23 复盘 | 单 Region 打满 5 小时，超时订单 310 万笔 | — | — | — | — |

**热点不是「机器不够」，是「一个 Raft group 只能在一台机器的一个线程组上串行处理写」。** 加节点只增加「可用的 Region 数」，不增加「单个 Region 的并行度」。这就是为什么 TiKV 的手册反复强调 `AUTO_RANDOM` / `SHARD_ROW_ID_BITS`——它不是优化项，是**把一个串行流变成 N 个并行流**的唯一手段。

### 事故三：`statistics` 没开，所有 `rocksdb.*` 指标读出来是 0（2025-12-18，某自研指标服务）

某团队在自己进程里嵌 RocksDB 9.x 存时序点，用 `rocksdb.stats` 与几个 `GetProperty()` 值做自监控。上线半年，**面板上 `Stall(us)` 一直是 0、block cache 命中率一直是 0、compaction 次数一直是 0**。运维的结论是：「这个引擎真稳，从来没发生过 write stall」。

真相是：RocksDB 的 `Options::statistics` 默认是 `nullptr`。**没开统计时，所有 ticker/histogram 类型的计数器都返回 0**——不是「没有 stall」，是「没人记账」。而 `GetProperty("rocksdb.stats")` 这类属性字符串里，stall 累计时间一栏也是 0。

| 时间 | 事件 | 面板显示的 stall | 实际 stall（事后用 `LOG` 复盘） | 据此做的决策 |
| --- | --- | --- | --- | --- |
| 12-18 前的半年 | 面板全绿 | 0 s/天 | 峰值 40–90 s/天 | 容量规划按「写放大 2×」估（把 LSM 当 WAL 算） |
| 12-18 10:00 | 单机盘写满告警 | 0 s/天 | 92 s/天 | 误判为「数据量估小了」，加盘 |
| 12-18 14:00 | 打开 `statistics` 后重启一个节点 | 首次非 0 | — | 发现真实写放大是 **41×**，不是 2× |
| 12-18 16:00 | 全量复盘 | — | — | 3 个月写穿 8 TB 盘；6 周内 P99 尖刺全是 compaction 引起的 |

**这一节的教训重复三遍**：`0` 不是健康，`0` 往往是「没开表」。观测的第一条纪律不是「看哪个指标」，是「先确认这个指标在这个开关下会不会真的有值」（→ §19.11）。

---

## 19.2 一句话定位与唯一真问题

**定位**：RocksDB 是**一个库**（link 进你的进程，操作一堆文件）；TiKV 是**在 RocksDB 外面套 Raft 与 PD 的那一层**，于是「一堆文件」变成「一个能跨机复制的有序 KV」。它们不组成一个数据库：SQL 层、权限、网络协议都在更外面（TiDB / 自研 client）。

### 唯一真问题

> **NewSQL = 一个能嵌进进程的 LSM + 一层共识 + 一个全局调度器；分布式事务之所以敢做得便宜，是因为它把「写」拆成「可重放的 LSM 追加」和「多数派确认的位置」，代价是每一次逻辑写都要按「LSM 写放大 × 副本数」付账。**

这一句要拆成三截来用：

1. **「能嵌进进程的 LSM」**：把「数据库」里最贵的两样东西（网络协议栈、SQL 解析与优化）剥掉之后，剩下的核心只是「有序 KV + 崩溃恢复」。剥掉之后它能被嵌进任何进程，也能被套上任何协议——于是同一份引擎代码同时养活 TiKV、MyRocks、Kafka Streams 的状态存储、Flink 的 state backend 和一堆元数据服务。
2. **「多数派确认的位置」**：Raft 让「写」有了两个不同的时间点——**日志被多数派 fsync（提交）** 与 **状态机 apply（可见）**。这两点之间的空隙，是所有「为什么刚写完读不到」「为什么 leader 切换后数据还在」问题的出生地。
3. **「按 LSM 写放大 × 副本数付账」**：MySQL 中一次 UPDATE 付 7.3 份钱（`→ comp/02` §02.8），TiKV 中同一次 UPDATE 付 **144 份**（§19.8 账一）。这不是 TiKV 写得差，是它把「三副本独立可恢复」和「后台重排数据」这两件事的钱都记在了同一笔写上。

从这一个矛盾推出全部使用纪律：

| 从矛盾推出的约束 | 推出的设计 |
| --- | --- |
| 写放大 × 副本数是常数倍，规模一大就撞盘 | 容量规划按「逻辑写速率 × 144 × 峰值系数」算盘带宽，不按数据量算盘容量 |
| 单个 Region 只能在一个 Raft group 里串行 | 热点必须打散（`AUTO_RANDOM` / `SHARD_ROW_ID_BITS`），否则加节点无效 |
| 提交与 apply 是两个时间点 | 「写入成功但读不到」是正常语义，写后读必须走 leader 或带 `read_ts` 的强一致读 |
| 分布式事务的原子性靠「一个可重放的提交点」 | 大事务要把锁与 TSO 开销按 Region 数摊，所以事务必须**小**（TiDB 默认 100 MB 级上限） |
| Region 大小是分裂阈值，不是磁盘单位 | Region 数 = 数据量 / 96 MB 量级；小表配太多 Region 会把心跳与调度变成新瓶颈 |
| 副本迁移是后台带宽的对手 | `store limit` 不是「安全阀」，是「前台读延迟的旋钮」 |
| GC 只能回收「最老活跃事务」之前的版本 | 长事务不只是锁问题，它会让整个集群的旧版本回收**停住**（空间放大） |

### 八问对照（本篇答哪几问）

| 八问 | 落在哪节 | 一句话答案 |
| --- | --- | --- |
| ① 承诺什么 | §19.3、§19.9 | RocksDB 承诺「单进程内一批写的原子性 + 崩溃后可恢复」；TiKV 承诺「多副本线性一致 + 跨 Region 事务」 |
| ② 数据长什么样 | §19.3、§19.4 | 每 CF 一棵 LSM 树（memtable + L0..Ln 的 SST）；TiKV 再叠 MVCC 三列族 + 独立 raftdb |
| ③ 一次读写走哪条路 | §19.5、§19.6 | 点查碰约 10 个文件但只读 1–2 个 block；写是「1 次 Raft 往返 + 多数派 fsync」再异步 apply |
| ④ 并发与一致 | §19.6、§19.7 | Raft 多数派 = 提交；Percolator 的两阶段 + 全局单调 TSO = 快照隔离 |
| ⑤ 扩展维度 | §19.9 | **分片 + 副本**同时用；LSM 不能靠「单机加大」线性扩，只能靠 Region 数买并行度 |
| ⑥ 会在哪崩 | §19.9 | PD/TSO 不可用 → 全库写入停；Region 多数派丢失 → 该范围不可用；分裂/迁移风暴 → 全集群抖 |
| ⑦ 看哪几个指标 | §19.11 | stall 时间、L0 文件数、pending compaction、Region 数与 pending-peer、hot region |
| ⑧ 换掉它的信号 | §19.9 | 需要一个 Region 承载的写超过 1 万 QPS 且无法打散；或写放大 × 副本数已经吃掉全部盘带宽 |

---

## 19.3 RocksDB：一个库，不是一台数据库

### 「库」这个词的全部含义

RocksDB 的 API 面只有四个动词：`Open`、`Get`、`Put`/`Delete`/`Write`、`NewIterator`。没有 `LISTEN`，没有 `GRANT`，没有 `SELECT`，也没有「连接到某个地址」这件事。**你调用它，是在你自己的进程里操作一堆文件**：同一个进程里的变量生命周期、同一个 `ulimit -n`、同一个 cgroup 配额、同一个 OOM killer。

这个形态带来的第一推论是：**它把「数据库」里最容易被误认为核心的两样东西（网络协议与 SQL）证明为非核心**。剥掉它们之后剩下的东西——「有序 KV」+「崩溃后能恢复」——恰好是 90% 的存储型组件真正需要的那 90%。所以它成了公共底座：

| 谁 | 拿 RocksDB 干了什么 |
| --- | --- |
| TiKV | 单机存储引擎（kv 一个实例 + raft 日志一个实例）→ 本文主线 |
| MyRocks | 替换 MySQL 的 InnoDB 作为存储引擎，白拿 LSM 的写吞吐与压缩率 |
| CockroachDB | 早期版本用 RocksDB 存 KV，后来自研 Pebble（RocksDB 的 Go 重写）以摆脱 C++ 依赖与 GC 摩擦 |
| Kafka Streams | 每个 state store 一个本地 RocksDB，做窗口聚合与 join 的本地状态 |
| Flink | `EmbeddedRocksDBStateBackend`，把算子状态放在 task manager 本地盘上 |
| 各类元数据服务 | HDFS 的 Ozone、部分对象存储的元数据层，用 RocksDB 存「路径 → 块列表」这类小 KV |

### 它提供什么 / 不提供什么

这张表是全篇最该背的一张。**每一行「不提供」都是一个必须由上层补的组件**，而不同 NewSQL 的差异基本就是「这些洞补得多好」。

| 能力 | 提供吗 | 说明与反例 |
| --- | --- | --- |
| 有序 KV 读写 + 前缀/范围遍历 | ✅ | 单进程内、单机文件系统上的有序映射，这是它的全部内核 |
| 一批写的原子性 | ✅ | `WriteBatch` = 一条 WAL 记录，要么全生效要么全不生效 |
| 崩溃恢复 | ✅ | WAL 重放 + SST 不可变；`sync=true` 时 RPO = 0 |
| 快照读（进程内） | ✅ | `GetSnapshot()` / `Transaction::SetSnapshot()` |
| 列族（Column Family）隔离 | ✅ | 每个 CF 是一棵**独立的 LSM 树**，各有自己的 memtable 与 compaction 策略 |
| 压缩、布隆、block cache、限速 | ✅ | 全是「一条物理路径上的旋钮」 |
| **网络协议 / 服务端** | ❌ | 没有监听端口。要远程访问必须自己套 gRPC（TiKV 就是这么干的） |
| **SQL 解析与优化器** | ❌ | 连「表」这个概念都没有。`SELECT` 要 TiDB 那一层给 |
| **权限与审计** | ❌ | 谁能读这个目录谁就能读全部数据；权限只能靠文件系统 |
| **跨节点复制** | ❌ | **反例**：它不会把 `Put` 发给别人。一致性完全由你负责（Raft / 或干脆不保证） |
| **二级索引** | ❌ | **反例**：没有「按非主键列查」。要索引只能自己再开一个 CF 存 `(列值, 主键)`，并**自己保证双写一致** |
| **跨进程事务 / 全局时间戳** | ❌ | `TransactionDB` 的锁活在进程内存里；两个进程各自 open 同一个目录 = 数据损坏（RocksDB 排他锁会拦，但拦不住你的设计错误） |
| **多语句隔离级别** | ⚠️ 部分 | 有快照隔离的影子，没有真正的可串行化；写偏斜照样发生（对照 `→ comp/06`） |
| **主键以外的访问路径、聚合、join** | ❌ | 全表扫只能靠 `Iterator`，聚合只能自己在客户端算 |

**这张表最贵的一行是「跨节点复制」**。因为它意味着：RocksDB 的「已提交」只到「这条 WAL 落到**这台机器**的盘上」为止。把这句话变成「三台机器里至少两台落盘」需要一整层共识协议——这就是 §19.6 的全部动机。

### column family 是独立的 LSM 树（与 HBase 的物理文件族不是一回事）

辨析一句，因为这两个东西同名不同物：

| | RocksDB 的 Column Family | HBase 的 Column Family |
| --- | --- | --- |
| 它是什么 | 一个**独立的键空间 + 独立的 LSM 树**：自己的 memtable、自己的 SST 集合、自己的 compaction 策略与压缩算法 | 同一 Region 内的**列的物理分组**：同族列落同一个 Store，共享同一 RowKey 排序 |
| 键怎么解释 | 同一个 key 在不同 CF 里是**两条互不相干的数据**，读必须显式指定 CF | 同一个 `(rowkey, cf, qualifier)` 才唯一确定一格；CF 是 schema 的一部分，必须建表时声明 |
| 谁决定数据落哪 | 调用方：`WriteBatch::Put(cf, k, v)` | 引擎：`rowkey` 决定 Region，`cf` 决定 Store |
| 开多少个合适 | 少。每个 CF 一棵树，CF 越多 flush/compaction 越难对齐 | 少。HBase 官方口径是「2–3 个」，每多一个都多一份 memstore 与 flush 压力 |
| 崩溃后一致性 | 默认多个 CF 各自 flush，**可能恢复到不同时间点**；`atomic_flush=true` 才一起落盘 | 同一次 flush 产出的 HFile 天然是一组，不存在「不同族不一致」 |

`atomic_flush` 为什么要存在，把它想清楚就懂了：**默认情况下多个 CF 共享一个 WAL，但各自独立 flush。** 于是「CF-A 已经刷到 L0、CF-B 还在 memtable 里」是一个合法的持久化状态，崩溃恢复后两者会停在不同的逻辑时间点。`atomic_flush=true` 让一次 flush 把所有 CF 的 memtable 一起落盘（写进一个 version edit），于是**恢复后所有 CF 处于同一逻辑时间点**。代价立刻出现：一次 flush 的耗时由最大/最慢的 CF 决定，CF 一多就越容易抖动（§19.10 坑四）。

这正是 TiKV 必须讨论它的原因：TiKV 把同一个 MVCC key 的三部分拆在 `default` / `write` / `lock` 三个列族里，**三者必须同时可见或同时不可见**，靠的就是列族级的原子落盘。

---

## 19.4 写路径与三种 compaction 的取舍

### 一次 `Put` 的完整路径

**图 1 · RocksDB 写路径与分层（每一层都在加一笔账）**

```text
Put(k, v) / WriteBatch（一串 (CF, type, key, value)，作为一个原子单元）
  │
  ├─① WriteBatch 在内存里攒：它定义「原子性的边界」——一条 WAL 记录 = 一批
  ▼
  ├─② WAL 追加（顺序 1×）
  │      └ WriteOptions::sync = false（默认）→ 只是 write() 进 page cache
  │        掉电丢「最近一批还没被内核刷盘」的写；sync = true 才 fdatasync
  ▼
  ├─③ memtable 插入：默认 skiplist（有序，支持任意 Seek）
  │      └ hash skiplist / hash linked list：按前缀分桶，只支持前缀 Seek
  │        写满 write_buffer_size（64 MB 量级）→ 转 immutable，换新 memtable
  ▼
  ├─④ flush：immutable memtable 整段写成 L0 的一个 SST（有序、顺序写 1×）
  │      └ 多个 CF 时：atomic_flush 决定「各 CF 是否一起落盘」
  ▼
  └─⑤ compaction：L0 ≥ level0_file_num_compaction_trigger(4) → L1 → … → L6
         ├ leveled ：层内不重叠，每层二分定位 1 个文件 → 写放大 (1+T)(L−1)+2
         ├ universal：同尺寸 run 攒够再合，层内可重叠 → 写放大 ≈ L
         └ FIFO   ：不合并，只按最老/最大删 → 不是一棵树
         推导与数值全在 → comp/02 §02.6，本篇不复写
```

三个必须自己买单的动作（第三个直接锚出去）：

| 动作 | 触发 | 它做了什么 | 代价 | 它决定的边界 |
| --- | --- | --- | --- | --- |
| WAL 追加 | 每次写 | 顺序追加一条记录 | 写放大 +1×；`sync` 决定 RPO | 持久性边界 |
| memtable → L0 flush | memtable 满 | 有序跳表整段写成一个 SST | 写放大 +1×；**L0 文件数是读放大与 stall 的同一个分母** | 读放大 + 尾延迟 |
| compaction | L0 文件数 / 层容量 / pending bytes | 读重叠文件 → 归并 → 写新文件 → 删旧 | 写放大 `T×(L−1)`，`→ comp/02` §02.6 | P99 尖刺的唯一来源 |
| `atomic_flush` | flush 时 | 多 CF 的 memtable 一起落盘 | flush 时长由最慢的 CF 决定 | 多 CF 的一致性 |

### 三种 compaction 的取舍：只有配比，没有中间态

| 策略 | 合并规则 | 写放大（T=10） | 空间放大 | 点查读放大 | RocksDB 里的开关 | 适合谁 |
| --- | --- | --- | --- | --- | --- | --- |
| **Leveled** | L_i 的一个文件 + L_{i+1} 中重叠文件归并，层内不重叠 | `(1+T)(L−1)+2` ≈ **46–57** | `1+1/T` ≈ **1.1×** | 每层 1 次二分 ≈ `L` 个文件 | `kCompactionStyleLevel`（默认） | 读多写少、要稳定 P99（TiKV 默认） |
| **Universal（tiered）** | 攒够 N 个同尺寸 run 再合成更大的 run，层内允许重叠 | `≈ L+2` ≈ **7** | `≈ N` ≈ **10×** | 每层最多 N 个 run，`≈ N×L/2` | `kCompactionStyleUniversal` | 写多读少、能忍空间（时序、埋点、日志） |
| **FIFO** | 不合并，只按「最老 / 总大小超限」删文件 | `≈ 1` | 由 TTL / `max_table_files_size` 决定 | 文件数决定，天然差 | `kCompactionStyleFIFO` | 「只留 N 天」的形态；**它不是一棵树** |

两句话记住这张表：

- **tiered 把写放大从 46 压到 7，同时把空间放大从 1.1× 抬到 10×。** 这就是为什么 TiKV 默认不走 tiered：它的用户数据是「查得多、还不能多占盘」。
- **FIFO 只适合「数据会过期」的表。** 拿 FIFO 做主存等于放弃「读到的就是最新的」——它删的是文件，不是 key。

### 为什么 TiKV 给 raft 日志单开一个 RocksDB 实例

TiKV 的进程里有两个独立的 RocksDB：`kv`（业务数据）与 `raftdb`（Raft 日志）。这不是洁癖，是为了让两件事互不干扰：

| | kv 引擎 | raft 引擎（raftdb） |
| --- | --- | --- |
| 装什么 | MVCC 三列族（`default` / `write` / `lock`，具体列族集合以目标版本源码为准） | Raft 日志条目 + Raft 自身状态 |
| 生命周期 | 与数据同寿，越长越大 | **随 apply 完成即截断**，是环形语义 |
| compaction | leveled，写放大 46 量级 | 几乎只有「删旧」，写放大 2 量级 |
| 关键旋钮 | `write_buffer_size`、block cache、`level0_*` | `sync-log`（是否 fsync） |
| 为什么分开 | 若混在一起，一次 kv 侧的大 compaction 会拖慢日志 fsync → 直接变成写延迟尖刺 | |

**「Raft 数据与业务数据分开存储」这一条是所有自己做共识的系统的通用姿势**，因为「日志要尽快被 fsync 且尽快被丢掉」与「数据要被反复重排且长期保留」是两个完全相反的访问模式。

### RocksDB 的事务：它买到的原子性到哪为止

`WriteBatch` 只承诺一件事：**一批写要么全生效、要么全不生效**。它不承诺隔离——两个 `WriteBatch` 之间没有任何互斥，你读到的一半可能来自这一批、另一半来自那一批。要「读到一致的快照」必须显式要到事务 API。

| 你要的语义 | 该用哪个 API | 它给到什么 | 它的边界 |
| --- | --- | --- | --- |
| 一批写要么全生效要么全不生效 | `WriteBatch` + `WriteOptions` | 原子性（一条 WAL 记录） | **没有隔离**：读不到一致视图 |
| 崩溃后不丢（RPO=0） | 同上，`sync = true` | 落盘保证 | 每条写多一次 fsync；`sync=false` 时掉电丢一批 |
| 一个调用里读到一致快照 | `DB::GetSnapshot()` / 迭代器绑快照 | **进程内**快照读 | 序列号是进程内的，不是全局时间；与 TiKV 的 `start_ts` 不是一回事 |
| 读-改-写不被打断（互斥） | `TransactionDB`（悲观）+ `GetForUpdate(key, exclusive)` | 行级（key 级）互斥 | 锁活在**进程内存**里；只有本进程的其它事务能被挡住 |
| 长事务想避免加锁、冲突时再重试 | `OptimisticTransactionDB` + `SetSnapshot()` | 提交时做冲突检测 | 冲突返回 `Busy`，**重试逻辑要你自己写** |
| 跨节点 / 跨进程的原子性 | **没有** | — | 必须在外面套 Raft/Percolator；TiKV 就是那一层 |
| 一致性快照备份 | `Checkpoint`（硬链接，秒级） | 一个可打开的只读副本 | 不做去重，占空间 |

**它不管跨进程，这一点必须刻进设计里**：同一个目录不能被两个进程同时 `Open`（RocksDB 的内部文件锁会拦一次，但拦不住你在两个容器里各挂一块盘）；`TransactionDB` 的锁不会在进程崩溃后残留（因为每批写都是原子的 `WriteBatch`，崩了就是整批没写）——**这既是优点，也意味着它无法提供任何跨进程的互斥**。`→ comp/46` 的分布式锁、以及本篇 §19.6 的 Raft，都是在这个洞上补的东西。

```rust
// RocksDB 9.x + rust-rocksdb（API 名以目标版本 crate 为准）
use rocksdb::{Options, TransactionDB, TransactionDBOptions, WriteOptions};

// ① open 时把三件「配置决定物理路径」的事定下来
fn open(path: &str) -> rocksdb::Result<TransactionDB> {
    let mut opts = Options::default();
    opts.create_if_missing(true);
    opts.enable_statistics();      // 不打开 ⇒ §19.11 里的 counters 恒为 0（事故三）
    opts.set_atomic_flush(true);   // 多 CF 的 memtable 一起落盘，恢复后处于同一时间点
    TransactionDB::open(&opts, &TransactionDBOptions::default(), path)
}

fn debit(db: &TransactionDB, from: &[u8], to: &[u8], amt: i64) -> rocksdb::Result<()> {
    // ② sync = false（默认）：快，但掉电丢「最近一批还没刷盘的写」
    let wo = WriteOptions::default();
    let txn_opts = rocksdb::TransactionOptions::default();
    let txn = db.transaction_opt(&wo, &txn_opts);

    // ③ 悲观锁：把「读余额 → 判断 → 扣减」变为本进程内不被其它事务打断的单元
    //    注意：这把锁只在**本进程**有效，跨进程必须靠 Raft / 分布式锁
    let cur = txn.get_for_update(from, true)?;               // exclusive = true
    let bal = cur.map(|v| i64::from_le_bytes(v.try_into().unwrap())).unwrap_or(0);
    if bal < amt {
        return Ok(());                                        // 不提交 = 两条都没发生
    }
    txn.put(from, (bal - amt).to_le_bytes())?;
    txn.put(to, (amt).to_le_bytes())?;
    // ④ commit = 一个 WriteBatch 落一条 WAL：要么两条都在，要么两条都不在
    txn.commit()
}
```

---

## 19.5 读路径：读放大是怎么被布隆挡住的

### 一次点查到底碰几个文件

**图 2 · `Get(k)` 的文件扇出（默认参数、240 GB 数据集）**

```text
Get(k)
  │
  ├─① memtable（skiplist 二分）.................. 1 个  命中则 0 次 IO
  ├─② immutable memtable（最多 max_write_buffer_number−1 = 1）
  │                                             1 个
  ├─③ L0：**键区间重叠**，必须从新到旧逐个查 ... 4 个（常态）
  │     └ stall 线 level0_slowdown = 20 / stop = 36 → 最多 20–36 个
  ├─④ L1..Ln：层内**不重叠**，二分定位「1 个」.. 4 个（240 GB 见底 L4）
  │
  └─ 被「碰」的文件总数 = 1 + 1 + 4 + 4 = 10
        每个文件先查 bloom（10 bit/key → 假阳性 0.82%）
        真正读 data block 的数量 = 1（命中那个）+ 10 × 0.82% ≈ 1.08
```

**这就是「N 个文件压成 1–2 个」的全部含义**：文件数决定了你要做几次「bloom 判断」，但只有假阳性才会变成一次真正的 4 KB block 读。10 bits/key 的 bloom 让假阳性率降到 0.82%，于是 10 次判断的期望成本只有 `10 × 0.0082 ≈ 0.08` 次额外 IO。

| 场景 | 被碰的文件数 | 真读 data block | 单次点查磁盘 IO |
| --- | --- | --- | --- |
| memtable 命中（热写热读） | 1 | 0 | 0 |
| 默认参数、240 GB、bloom 生效，命中 L2 | 10 | 1 + 0.08 | 1 × 4 KB（默认 `block_size` 4 KB 量级） |
| 同上但 **bloom 关闭** | 10 | 最多 10 | 最多 40 KB |
| L0 到 slowdown 线（20 个文件） | 26 | 1 + 0.16 | 1–2 × 4 KB |
| L0 到 stop 线（36 个文件） | 42 | 1 + 0.23 | 1–2 × 4 KB |
| 1 TB 数据集（见底 L5） | 1+1+4+5 = 11 | 1.08 | 同 |
| 范围扫 100 万行 | 8 个重叠文件全要 merge | 全读 | 逻辑 220 MB → 实读 0.7–1.1 GB（`→ comp/02` §02.8 L7） |

**最后一行是全篇最容易被误解的一句**：bloom 挡的是「**单个 key 不在这个文件里**」，对「这个 key 在不在某个区间里」一问三不知。所以 LSM 的点查可以便宜到 4 KB，范围扫永远要多层 merge。

### block cache 的三块与布隆的两副面孔

block cache 里装的其实是三种东西，混着算「命中率」会得出荒谬的结论：

| 块类型 | 什么时候进缓存 | 谁在读它 | 开关 |
| --- | --- | --- | --- |
| **data block** | 点查/扫描命中后 | 所有读 | 由 `block_cache`（LRU）统一管 |
| **index block** | 打开 SST 时 | 每次二分都要它 | `cache_index_and_filter_blocks=true` 才进 cache（否则常驻堆，无上限） |
| **filter block（bloom）** | 每次点查前 | 每次点查都要它 | 同上；`pin_l0_filter_and_index_blocks_in_cache` 把 L0 的钉住 |

- **index/filter 不进 cache 时**：它们常驻内存，优点是永远不 miss，缺点是**内存占用不设上限**——一个 1 TB 数据集、bloom 10 bits/key 的表，光 bloom 就要 `1 TB ÷ 平均值大小 × 10 bit` 量级的常驻内存。这是「LSM 引擎内存莫名涨」最常见的原因。
- **分区索引**（两段式 index + partitioned filters）让大索引可以「按需载入一个分区」，代价是每次二分多一次 cache 查找。数据集 > 几百 GB 时基本是必选项。
- **命中率要分块算**：`index` 命中率接近 100% 是正常的，`data` 命中率掉到 70% 以下才说明 working set 装不下。

**prefix bloom 解决的是另一件事**：`prefix_extractor` 让 SST 里额外建一份「按前缀」的过滤器，于是 `Iterator::Seek(prefix)` 能跳过不含该前缀的文件。**它只对前缀这一种访问有效**：一旦你把它当范围过滤器用（`SeekToFirst()` 扫一大段），它一个字节也跳不过去；而且 `prefix_extractor` 一旦设置，全序 `Seek` 必须显式打开 `total_order_seek` 才能正确工作——这个坑在 §19.10 里也会再出现一次。

---

## 19.6 TiKV 三件套：LSM + Raft + PD

### 三件套各管什么

**图 3 · 一条 SQL 写到底层三副本的完整链路**

```text
客户端 / 业务服务                                     ── ① 网络 + 协议解析
   │  MySQL 协议 / gRPC           同机房 0.1–0.3 ms ｜ 跨 AZ 1.0–1.6 ms
   ▼
TiDB / SQL 层（解析 → 计划 → 按 Region 边界切成 key 区间批次）
   │                                                   ── ② 取 start_ts
   ▼
PD（TSO 分配器：单点串行 + 批量窗口 + 持久化上界）
   │                                                   ── ③ 悲观锁 / prewrite
   ▼                                                     并行发往涉及的每个 Region
TiKV leader（Region R 的 raftstore，store-pool 线程组）
   ├─ propose → 追加 raft log（raftdb）
   ├─ 本地 fdatasync ◄──── 组提交摊薄：NVMe 0.1–0.2 ms ｜ 云盘 0.5–2 ms
   ├─ 并行 AppendEntries ► follower×2：各自追加 + fsync
   │      ◄── 最快的那一个回来就够 = 多数派（leader + 1 副本）
   └─ ══ 提交索引推进：这一刻「写已提交」，但**还没人读得到** ══
   │                                                   ── ④ apply（异步，不阻塞提交确认）
   ▼
RocksDB(kv)：WriteBatch → WAL → memtable → L0 → L1..Ln
   （到这里才开始付 46× 的 compaction 账，与写延迟**无关**）
   │                                                   ── ⑤ 第二阶段：写 commit 记录 + 清锁
   ▼                                                     又一次 ③–④ 的往返
客户端看到「事务已提交」（= commit_ts 已被多数派确认）
```

| 跳 | 动作 | 同机房 | 跨 AZ（RTT 1.5 ms） | 谁在等 |
| --- | --- | --- | --- | --- |
| ① 客户端 → SQL 层 | 网络 + 解析 + 计划 | 0.1–0.3 ms | 1.0–1.6 ms | 客户端 |
| ② 取 TSO | PD 内存分配 + 1 次 RPC（客户端批量取时摊薄） | 0.1–0.3 ms（摊薄后 < 0.05 ms） | 0.8–1.6 ms | 事务 |
| ③ SQL 层 → Region leader | 网络 | 0.1–0.2 ms | 0.8–1.5 ms | 事务 |
| ④ leader 本地 raft log fsync | 组提交 | 0.1–0.2 ms（NVMe）/ 0.5–2 ms（云盘） | 同左 | Raft |
| ⑤ leader → follower | 并行，等最快的对端 | 0.2–0.4 ms | 1.5–3.0 ms | Raft（多数派） |
| ⑥ follower fsync | 组提交 | 0.1–0.2 ms | 0.1–0.2 ms | Raft |
| ⑦ apply 到 kv RocksDB | **异步** | 0（对写延迟不可见） | 0 | 没人等（但决定「什么时候能被读到」） |
| ⑧ 第二阶段 commit | 再一次 ③–⑥ | 0.4–0.8 ms | 3–6 ms | 事务 |
| **合计（悲观事务、单 Region）** | | **1–2 ms** | **5–9 ms** | |

**这张表的两条推论：**

1. **写延迟有一个由物理决定的地板**：`≥ 1 次 RTT（propose）+ 1 次 fsync + 1 次多数派 RTT + 1 次 fsync`。跨 AZ 部署把地板从 1 ms 抬到 5 ms 以上，**这不是调优能解决的**，只能靠「减少事务次数」「同 AZ 部署」「异步化」。
2. **写放大与写延迟无关**：compaction 是后台的事，它污染的是**尾延迟**（stall 时写线程被 sleep），不是均值。这就是为什么「均值 8 ms、P99 190 ms」是 LSM 系统的标准体检单。

### ① RocksDB：单机 LSM（且日志与数据分开）

见 §19.4 末表。这里只加一句：**TiKV 的 kv 引擎是一个「MVCC 化的 LSM」**——同一个业务 key 在 `default` CF 里按 `(key, start_ts)` 存值，在 `write` CF 里按 `(key, commit_ts)` 存「这次提交是 Put 还是 Delete」，在 `lock` CF 里按 `(key, start_ts)` 存锁。三者的可见性必须一致（`atomic_flush` 的动机），而「读一个 key 的某个版本」= 在 `write` CF 里找 `commit_ts ≤ read_ts` 的最大一条，再去 `default` CF 取对应 `start_ts` 的值。**MVCC 在 InnoDB 里是 undo 版本链（`→ comp/06`），在 TiKV 里是「同一棵 LSM 里的时间戳前缀」——两种实现，同一个契约。**

### ② Raft：一个 Region = 一个 Raft group

| 概念 | 定义 | 它决定了什么 |
| --- | --- | --- |
| **Region** | 一段连续的 key 区间（左闭右开），**是一个 Raft group 的数据范围** | 并行度的单位；一个 Region 的写只能在它的 leader 上串行 |
| **peer** | 一个 Region 在某个 store 上的副本 | 副本数 3 ⇒ 每个 Region 3 个 peer |
| **leader / follower** | 每 Region 独立选举（Multi-Raft） | 写只能进 leader；读可走 lease read / ReadIndex |
| **commit index** | 多数派已 fsync 的最大日志位置 | **这一刻才算提交**；apply 在它之后，异步 |
| **apply** | 把已提交日志写进 kv 引擎 | 决定「读得到」的时间点；apply 落后 → 读到的可能比已提交的旧 |
| **Region 分裂** | 达到 `region-split-size` 量级（默认 96 MiB 区间，早期版本 64 MiB，随版本变化）或 `region-split-keys` 时一分为二 | Region 数随数据量线性增长；分裂后 leader 可能立刻迁移 |

**一句辨析（其余不复述，→ comp/18）**：HBase 与 TiKV 都叫「Region」，两者都是**按 key 范围切的一段连续数据**，但回答的问题不同——**HBase 的 Region 是「这一段表由哪台 RegionServer 服务」（单副本主，一致性托给 HDFS），TiKV 的 Region 是「这一段数据由哪三个副本构成一个 Raft group」（三副本，一致性托给 Raft）**。前者切的是表，后者切的是共识单元；由此推出：HBase 的 Region 过大只影响并行度，TiKV 的 Region 过大还会让「多数派确认」这件事变粗（一次迁移/一次 compaction 要动的数据更多）。

**写延迟的推导**（这是本篇的支点公式）：

写延迟 = `1 次 propose RTT + 本地 fsync + 1 次多数派 RTT + 多数派 fsync` = `2 × RTT + 2 × fsync`：

| 组成 | 同机房（RTT 0.2 ms、NVMe 组提交 0.15 ms） | 跨 AZ（RTT 1.5 ms、云盘 fsync 1 ms） |
| --- | --- | --- |
| propose RTT | 0.2 ms | 1.5 ms |
| leader 本地 fsync | 0.15 ms | 1.0 ms |
| 多数派 RTT | 0.2 ms | 1.5 ms |
| 多数派 fsync | 0.15 ms | 1.0 ms |
| **合计 = 2 × RTT + 2 × fsync** | **0.7 ms** | **5.0 ms** |

再加 TiDB 侧一条 SQL 的四次往返、TSO、以及 Percolator 的第二阶段，就到了上表的 1–2 ms / 5–9 ms。**「一次 Raft 往返 + 多数派落盘」这一句话里，「多数派」是免费的并行，「往返」是要付两次的串行。**

### ③ PD：全局调度——它调的四件事

| 调度器 | 它做什么 | 触发判据（量级） | 代价落在谁身上 |
| --- | --- | --- | --- |
| **Region 分裂** | 把一个太大的 Region 一分为二 | `region-split-size` 96 MiB 量级 / `region-split-keys` 96 万量级 | 元数据与心跳量；分裂太多 → 风暴 |
| **balance-leader** | 把 leader 从忙 store 挪到闲 store | store 的 leader 数偏离均值 | 一次 leader 切换 = 短暂写不可用（毫秒级） |
| **balance-region** | 把 peer 从大 store 挪到小 store | store 的 region size 偏离均值 + `store limit` 限速 | **搬数据，抢前台盘带宽** |
| **hot-region** | 对写入/读取热点做「拆分 + 迁移」 | 单 store 的 hot 流量占比超阈值 | 分片后的 Region 变小、心跳变多 |
| （store-limit） | 给迁移限速 | 默认量级为每个 store 每秒数 MB（`store-limit-v2` 后单位与默认值变过） | 限太松 → 打满盘；限太紧 → 扩容几小时才见效 |

**PD 是全局单点这一条必须直说**：TSO 由 PD 的 leader 提供，**PD 多数派丢失 = 全集群无法开始新事务**——即使每一个 TiKV 都健康。这是 19.9 的 ⑥ 里第一条系统级失效。

---

## 19.7 Percolator 两阶段：commit_ts 为什么必须单调

### 为什么非两阶段不可

先想清楚一件事：一个事务要改的 key 可能落在 5 个 Region，**5 个不同 Raft group**。而 Raft 只保证「组内原子」，组间没有任何原子写原语。RocksDB 的 `WriteBatch` 更是连跨进程都不管（§19.3）。所以「把 5 个 key 一起改」在物理上不可能一步完成。

Percolator 的答案是把「原子性」**缩到一个点上**：在参与分片里选一个当 **primary**，在 primary 上写一条「提交记录」。**这条记录的 Raft 写成功 = 整个事务已提交**；其余四处的写入都变成「可重放的续作」，失败就重试，重试多少次都收敛。

**图 4 · Percolator 两阶段时序**

```text
 客户端(事务 T)        PD(TSO)      TiKV Region A(primary)      TiKV Region B(secondary)
     │                   │                   │                          │
     │ ① GetTS ─────────►│                   │                          │
     │◄──── start_ts=100 ┘                   │                          │
     │ ② Prewrite：写锁 + 写数据@start_ts（并行发往所有 Region）        │
     │──────────────────────────────────────►│ put lock(key_a, ts=100)  │
     │                                       │ put value_a@100          │
     │──────────────────────────────────────────────────────────────────►│
     │                                       │  put lock(key_b, ts=100) │
     │                                       │  put value_b@100         │
     │ ③ GetTS ─────────►│                   │                          │
     │◄─── commit_ts=120 ─┘                   │                          │
     │ ④ Commit(primary)─────────────────────►│                          │
     │      ══ 这一条 Raft 写成功 = 整个事务提交 ══                          │
     │                                       │ 写 commit 记录@120       │
     │                                       │ 删锁                     │
     │ ⑤ Commit(secondary)：可异步、可失败、可重放                         │
     │──────────────────────────────────────────────────────────────────►│
     │                                       │      写 commit 记录@120 + 删锁
     │ ⑥ 读(key_b, read_ts=200)───────────────────────────────────────────►│
     │    遇到残留锁 → CheckTxnStatus(primary) → 得知 @120 已提交
     │    → 读 commit_ts ≤ 200 的最大版本 = @120 → 返回新值
```

### 三个「为什么」

**为什么 commit_ts 必须单调（由全局 TSO 分配）？**

三个理由，缺一个系统就会错：

1. **快照读的正确性**：读一个 key 的规则是「取 `commit_ts ≤ read_ts` 的最大版本」。如果 `commit_ts` 可以乱序分配，那么一个**后提交**的事务可能拿到**更小**的 `commit_ts`，于是「读 `read_ts=200`」会看见它、而「读 `read_ts=250`」却看不见它——快照倒退，读到的数据比刚才更旧。
2. **物理顺序 = 逻辑顺序**：`write` CF 里的 key 是 `(key, commit_ts)`，LSM 按字节序排列。`commit_ts` 单调 ⇒ 磁盘上的版本顺序就是提交顺序 ⇒ 「往回找第一个 ≤ read_ts 的版本」只需要 Seek 一次，不需要扫全链。
3. **自己的写必须对自己可见**：`commit_ts > start_ts` 是硬约束，否则事务读不到自己刚写的值。

TSO 由 PD 的一个**混合逻辑时钟**生成：物理时间 + 单调逻辑计数，批量下发（客户端取一批在本地消费，网络开销被摊到几十上百次分配上，批大小随版本变化），并且**定期把上界持久化**（`tso-save-interval` 量级 3 s），重启时从「持久化上界 + 物理时间」继续，**保证永不倒退**。这就是为什么「PD 挂了全库写停」而不是「PD 挂了换个号继续发」——倒退的时间戳会让所有已提交数据的可见性规则失效。

**为什么读要检查锁？**

因为 prewrite 已经把**新值**写进 `default` CF 了（带 `start_ts`）。如果不看锁，一个 `read_ts` 足够大的读会跳过 `write` CF 的提交记录检查、直接按某个规则拿到那个还没提交的版本——读到未提交数据。所以读路径必须先看 `lock` CF：**有锁 → 要么等，要么去问 primary「这个事务到底提交了还是回滚了」**。

**为什么清理不需要事务自己活着？**

这就是「可重放」的全部价值。任何一个后来的读者/写者，只要遇到锁，就能调 `CheckTxnStatus` 去问 primary，然后**替那个死掉的事务做决定**：primary 没有提交记录且锁已过期 → 写一条 Rollback 记录并删锁；primary 有提交记录 → 补写 commit 记录并删锁。所以「客户端在提交后一秒崩溃」不会留下永久不可读的数据，只是留下一点需要被清掉的垃圾。

### 崩溃时点判定表

| 崩溃时点 | 盘上留下什么 | 谁来清理、怎么清 | 读者会看到什么 |
| --- | --- | --- | --- |
| **① prewrite 之前** | 什么都没有（或只有事务开始的时间戳） | 无需清理 | 看不到任何痕迹，正常 |
| **② prewrite 部分成功**（primary 未写或写了但没到多数派） | 若干 secondary 上有锁、有新值 | 任何遇到锁的人 → `CheckTxnStatus(primary)` → **无提交记录且锁过期** → 写 Rollback、删锁 | 短暂阻塞（毫秒到秒级，取决于锁 TTL 与重试），随后读到事务前的值 |
| **③ prewrite 全部成功、commit 未开始** | primary 与 secondary 上都有锁 | 同上：primary 无提交记录 → **回滚整个事务** | 同上；注意此时**已写入的新值永远不会被读到** |
| **④ commit_ts 已取、primary 提交记录写了一半** | primary 上可能没有完整提交记录（Raft 未到多数派 ⇒ 视为没写） | 回到情形 ③ 的处理：回滚 | 读到事务前的值 |
| **⑤ primary 已提交、secondary 未清锁** | primary 有 commit 记录@120；secondary 仍是锁 | 读者查 primary 得知 @120 已提交 → **补提交** secondary 并删锁（异步） | 读到 @120 的新值；延迟一次 `CheckTxnStatus`（同机房 0.2–0.5 ms） |
| **⑥ 全部 commit 记录已写、锁未清** | 有提交记录 + 残留锁 | 后台 GC / 后续访问顺手清 | 读到新值，且不会回退 |

**这张表的读法**：整张表里没有一格是「数据不一致」，只有「短暂阻塞」与「一次额外的 RPC」。**Percolator 用「读者帮忙清理」把崩溃恢复从「后台任务」变成了「惰性触发的副作用」——这也是它敢把分布式事务做便宜的原因。**

### 跨 Region 事务的延迟账

三种执行方式的延迟公式：乐观模式 `TSO(1 RTT) + 并行 prewrite(1 RTT) + commit(1 RTT) ≈ 3 RTT + fsync`；悲观模式再加一次 `acquire_pessimistic_lock` ⇒ `≈ 4 RTT + fsync`；而**串行最坏**是每个参与分片依次 prewrite + commit，即 `2 × RTT × N`。

| RTT | 并行 prewrite（2 RTT 量级） | 串行（`2 × RTT × 5`） | 差 |
| --- | --- | --- | --- |
| 同机房 0.2 ms | **0.4 ms** | `2 × 0.2 × 5 =` **2.0 ms** | 5× |
| 跨 AZ 1.5 ms | **3.0 ms** | `2 × 1.5 × 5 =` **15.0 ms** | 5× |

**加进 TSO 与两次 fsync 后**，同机房 5 Region 事务的实际量级是 **3–5 ms**，跨 AZ 是 **16–20 ms**。这里有两个必须记住的推论：

1. **`2 × RTT × N` 是串行下界，不是必然结果**。并行 prewrite 能把 N 从乘法里拿掉，代价是「要么全成要么全回滚」的重试成本——所以客户端的批次切分方式（按 Region 分组、并发度）直接决定大事务是 2 ms 还是 20 ms。
2. **新版引擎有把两阶段压成一阶段的手段**（TiDB 5.0 引入、6.0 起默认的 Async Commit 与 1PC：在满足条件时把 `commit_ts` 提前算好写进锁里，省掉第二阶段）。**开关与默认值随版本变化，以目标版本文档为准**；但即使开了，§19.8 的写放大账一分钱都不会少——它省的是 RTT，不是字节。

### 客户端骨架：重试、超时与「别把重试放大成写风暴」

Percolator 把「不丢不重」的责任分给了两端：**引擎保证「崩在任意时刻都不会不一致」，客户端保证「重试是幂等的」**。所以客户端代码里真正重要的不是事务写法，而是这三件事：超时优先于重试、只重试可重试的错误、退避带抖动。

```go
// database/sql + go-sql-driver/mysql 连 TiDB。TiDB 默认悲观事务：Begin 即 start_ts，
// Commit 时由 PD 分配 commit_ts 并走 Percolator 两阶段（§19.7）。
func transfer(ctx context.Context, db *sql.DB, from, to int64, amt int64) error {
	const maxAttempts = 5
	var last error
	for i := 0; i < maxAttempts; i++ {
		if err := ctx.Err(); err != nil { // ① 上游已超时就别再试：重试只在剩余预算内
			return err
		}
		tx, err := db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead})
		if err != nil { return err }
		// ② FOR UPDATE 走 acquire_pessimistic_lock：多一次 RTT，换「读后写」不冲突
		var bal int64
		err = tx.QueryRowContext(ctx,
			"SELECT balance FROM acct WHERE id = ? FOR UPDATE", from).Scan(&bal)
		if err != nil { tx.Rollback(); if retryable(err) { last = err; continue }; return err }
		if bal < amt { tx.Rollback(); return ErrInsufficient }
		if _, err = tx.ExecContext(ctx, "UPDATE acct SET balance=balance-? WHERE id=?", amt, from); err != nil {
			tx.Rollback(); if retryable(err) { last = err; continue }; return err
		}
		if _, err = tx.ExecContext(ctx, "UPDATE acct SET balance=balance+? WHERE id=?", amt, to); err != nil {
			tx.Rollback(); if retryable(err) { last = err; continue }; return err
		}
		if err = tx.Commit(); err == nil { return nil } else { // ③ 提交阶段也可能冲突
			last = err
			if !retryable(err) { return err }
		}
		time.Sleep(backoff(i)) // 指数退避 + 抖动；重试预算要进 SLO，别静默放大
	}
	return fmt.Errorf("give up after %d attempts: %w", maxAttempts, last)
}

// retryable：WriteConflict / 9007 / 8022 / Region 不可用 / 锁超时 → 重试；
// 唯一键冲突、余额不足、语法错 → 立刻失败（重试只会放大错误）。
```

---

## 19.8 算账：三层写放大、单 Region 上限、调度带宽与跨 Region 事务

### 账一 · 三层写放大：一次逻辑写到底写了多少字节

**假设清单（换任一假设请按脚本重算，不要直接引用结论）**

1. 数据集 240 GB，RocksDB leveled，`max_bytes_for_level_multiplier=10`，见底 L4（下沉 4 次）；
2. 逻辑写一行 400 B；写是单行更新（非批量导入）；
3. 副本数 3；`raftstore.sync-log` 开启；
4. raft 日志引擎自身的 LSM 放大取 2×（日志段 apply 完即被截断，所以远小于 kv 侧）；
5. 不计快照/迁移流量，不计 TiDB 侧的日志，不计网络字节；GB = 10⁹ B。

| 层 | 倍数 | 单次逻辑写的累计物理字节 | 推导 |
| --- | --- | --- | --- |
| ① 逻辑写 | 1× | 400 B | 业务值 |
| ② kv 引擎（WAL 1 + flush 1 + compaction 44） | **46×** | 18.4 KB | `(1+T)(L−1)+2 = 11×4+2`，`→ comp/02` §02.6 |
| ③ Raft 日志（含 raftdb 自己的 LSM） | +2× | 19.2 KB | 日志追加 1× + 日志段改写 1× |
| ④ 副本数 3（leader 与每个 follower 各自算 ②③ 一整套） | **×3** | **57.6 KB** | 每副本独立写自己那套盘 |
| **合计** | **144×** | **57.6 KB / 400 B** | |

同一份负载在两种引擎下的对照：

| 口径 | 写放大 | 400 B 的一次更新 → 盘上字节 | 80 GB/天逻辑写 → 物理写 |
| --- | --- | --- | --- |
| MySQL 8.4 / InnoDB（池装得下，`→ comp/02` §02.8） | 7.3× | 2.9 KB | 584 GB/天 ≈ 6.8 MB/s |
| TiKV / RocksDB leveled + 3 副本（本表） | **144×** | **57.6 KB** | **11.5 TB/天 ≈ 133 MB/s 均值** |
| TiKV 若换 universal（写放大 7） | `(7+2)×3 =` 27× | 10.8 KB | 2.16 TB/天 ≈ 25 MB/s |

**这就是事故一的完整算术**：`11.5 TB/天` 摊到 5 个节点 = 2.3 TB/天/节点 = 27 MB/s 均值；晚高峰 5 倍 = 133 MB/s/节点，再叠加前台读与 compaction 尖峰，盘 util 到 96% 是必然的。

上面四个算例（三层写放大 / 单 Region 上限 / 调度带宽 / 跨 Region 事务）可以用同一段脚本复算——**换假设请改常数重跑，不要直接引用本节的数字**：

```python
# 本篇 §19.8 四个算例的可复算版本。改假设重跑，别抄结论。
ROW_KB   = 400 / 1000   # 单次逻辑写的值大小（KB，十进制口径 GB=10⁹B）
T, L     = 10, 5        # 层间倍数 / 参与层数（240 GB 见底 L4 ⇒ 下沉 4 次）
REPLICAS = 3
RAFT_LSM = 2            # raft 日志引擎自身的 LSM 放大（日志段被截断，远小于 kv 侧）

wa_kv   = (1 + T) * (L - 1) + 2                  # 46：已含 kv WAL(1) + flush(1)
wa_peer = wa_kv + RAFT_LSM                       # 48：单副本
wa_all  = wa_peer * REPLICAS                     # 144：全集群
print(f"账一 kv={wa_kv}x 单副本={wa_peer}x 全集群={wa_all}x → 一次写 {ROW_KB*wa_all:.1f} KB")
print(f"     日逻辑写 80 GB ⇒ 物理 {80*wa_all/1000:.1f} TB/天（均值 {80*wa_all*1e3/86400:.0f} MB/s）")
wa_uni = (L + 2 + RAFT_LSM) * REPLICAS           # universal：L+2 的写放大
print(f"     换 universal ⇒ {wa_uni}x，但空间放大 1.1x → {T}x")

RTT_MS, K = 0.2, 10                              # 同机房 RTT / 组提交摊薄倍数
rtt_per_txn = 3 * RTT_MS                         # 悲观单 Region 事务：取锁+prewrite+commit
print(f"账二 单 Region 串行 ≈ {1e3/rtt_per_txn:.0f}/s；组提交×{K} ⇒ {1e3/rtt_per_txn*K:,.0f}/s")

REGION_MB, N_STORE = 96, 3
n_region = 1e6 / REGION_MB                       # 1 TB / 96 MB
print(f"账三 1 TB ⇒ {n_region:,.0f} 个 Region，{n_region*REPLICAS:,.0f} 个 peer")
for lim_mb in (2, 10, 20):                       # 每 store 限速
    hours = 1e6 / (lim_mb * N_STORE) / 3600
    print(f"     每 store {lim_mb:>2} MB/s ⇒ 搬完 1 TB 需 {hours:.1f} 小时")

for rtt, tag in ((0.2, "同机房"), (1.5, "跨 AZ ")):
    print(f"账四 {tag} RTT {rtt} ms：并行 2 RTT = {2*rtt:.1f} ms；串行 2xRTTx5 = {2*rtt*5:.1f} ms")
```

> **量级结论（非承诺值）**：TiKV 上一次单行更新的物理写约 **144 份**（±30%，取决于层数 T/L、raftdb 的实际放大、压缩算法）。把副本数改成 5 就变 240×；换 universal compaction 可到 27×，代价是空间放大 1.1× → 10×。**这个数字与「写延迟」无关**——它只决定你要买多少盘带宽，以及多久之后 compaction 会被追不上。

### 账二 · 单 Region 写上限：为什么热点行必须打散

一个 Region 的写只能在它的 leader 上串行 propose，所以上限由「每次事务的串行往返数」决定：

```text
单 Region 串行 TPS ≈ 1 / (事务串行往返数 × RTT)
悲观单 Region 事务 = acquire_lock(1 RTT) + prewrite(1 RTT) + commit(1 RTT) = 3 × 0.2 ms = 0.6 ms
  ⇒ 串行上限 ≈ 1 / 0.6 ms ≈ 1,600 TPS
组提交摊薄：一次 fsync 摊 K 个并发事务（K 取 10）
  ⇒ 上限 ≈ 1,600 × 10 ≈ 1.6 万 TPS（理论上限，实际被 raftstore/apply 线程与 CPU 截断）
工程预算口径：**单 Region ≈ 1 万写 QPS**；**单行热点 ≈ 10³ QPS**
```

| 场景 | 并行单位 | 上限量级 | 加节点有效吗 |
| --- | --- | --- | --- |
| 单行热点（同一主键反复更新） | **1 个 Region，且还要抢同一把锁** | **10³ QPS** | ❌ 无效（锁冲突在 Region 内排队） |
| 单 Region 内 1 万个不同 key | 1 个 Region 的组提交 | 10³–10⁴ QPS | ❌ 基本无效 |
| 打散到 1 万个 Region | 1 万个 Raft group | 10⁷ QPS 量级 | ✅ 有效（受节点数与 Region 数约束） |

**为什么热点行/热点表必须 `AUTO_RANDOM` / `SHARD_ROW_ID_BITS`**：这两个语法都在**主键的高位塞随机位**，`SHARD_ROW_ID_BITS = 4` 造 2⁴ = 16 个分片，`AUTO_RANDOM(5)` 造 2⁵ = 32 个分片。于是「单调递增的一串写」被切成 N 条独立的 key 流，落在 N 个不同的 Region 上、N 个不同的 Raft group 里、可能落在 N 个不同的机器上。这是把一个串行流变成并行流的**唯一引擎级手段**——加机器解决不了「一个 Raft group 一次只能提交一条日志」这件事。

> **量级结论（非承诺值）**：单 Region 写上限 **10⁴ QPS 量级**、单行热点 **10³ QPS 量级**，误差可达 3×（取决于 RTT、fsync 能力、组提交效率、apply 线程数）。**承诺值只有一条**：单 Region 的写是串行的，因此它的吞吐不会因为你加节点而上升。

### 账三 · Region 分裂与调度带宽：1 TB 要搬多久

**推导链**：`Region 数 = 总数据量 / region-split-size`；`peer 数 = Region 数 × 副本数`；一次全量 balance 的搬运量最坏是**一份数据**（每个字节搬一次）；`搬运时长 = 搬运量 / (每 store 限速 × 并行 store 数)`。全部按 `GB = 10⁹ B` 的十进制口径（与账一的假设清单一致）：

| 每 store 限速（量级） | 3 个 store 并行 | 搬 1 TB（10⁶ MB）需要 |
| --- | --- | --- |
| 2 MB/s（`store limit` 默认量级） | 6 MB/s | 1e6 / 6 ≈ 166,700 s ≈ **46.3 小时** |
| 10 MB/s | 30 MB/s | ≈ 9.3 小时 |
| 20 MB/s | 60 MB/s | ≈ 4.6 小时 |

**三个必须提前算清的连带成本：**

1. **迁移流量与前台读抢同一块盘。** 事故一里扩容 2 个节点后 P99 从 130 ms 涨到 210 ms，就是因为迁移在 100% util 的盘上又压了 180 MB/s。**「扩容是一种攻击」**这句话在 TiKV 上是字面意义的。
2. **Region 越多，心跳与 PD 调度负载越高。** 1.04 万个 Region × 3 peer ≈ 3.1 万个心跳源；如果 Region 数被切成 10 倍（例如小表也按 96 MB 切、或者 split size 被调成 9.6 MB），心跳与调度开销直接 ×10，PD 的 CPU 与 TiKV 的心跳线程会先于数据量成为瓶颈（§19.10 坑五）。
3. **分裂不是免费的元数据操作**：每次分裂都要 PD 分配新 Region ID、更新元数据、触发一次心跳风暴。分裂速率本身有限速（`region-split-check-diff` 等参数），写入突增时会先堆积再一次性分裂。

> **量级结论（非承诺值）**：1 TB / 96 MB ≈ **1.04 万个 Region**、3.1 万个 peer；按默认量级限速做一次全量重分布需要 **两天量级**，把限速调到 10 倍可压到 **十小时量级**，但代价是这段时间的前台读延迟。`store-limit` 的默认值与单位在新版本里变过（`store-limit-v2`），**以目标版本 `pd-ctl config show` 的输出为准**。

### 账四 · 跨 Region 事务：5 个 Region 是多少毫秒

见 §19.7 末表：`2 × RTT × 5` 在同机房是 **2.0 ms**，跨 AZ 是 **15.0 ms**；叠加 TSO 与两次 fsync 后分别为 **3–5 ms** 与 **16–20 ms**。

一个反直觉的推论：**「事务涉及多少个 Region」比「事务改了多少行」更决定延迟。** 改 1,000 行如果全在同一个 Region 里，prewrite 是一次 RTT 里的一批；改 5 行如果落在 5 个 Region，你要付 5 份（甚至串行时 10 份）往返。所以把「相关数据放同一个 Region / 同一张表按同一个维度分片」比「把事务写小」有时更有效——而 TiDB 的 `AUTO_RANDOM` 恰恰会破坏这个性质，这是打散热点必须接受的代价。

---

## 19.9 架构边界：NewSQL 便宜在哪、贵在哪

### 便宜在哪：四笔原来要人肉付的钱

| 原来怎么付 | TiKV 里谁替你付 | 换来了什么 | 新的账单 |
| --- | --- | --- | --- |
| 分库分表：中间件 + 路由 + 全局唯一 ID + 跨库 join 自己写 | Region 分裂 + PD 调度 | 分片变成后台行为，业务不需要知道有几个库 | Region 数、心跳、迁移带宽 |
| 主从复制：配半同步、管延迟、处理脑裂与切主 | Raft（多数派 + 选举 + 日志回填） | 副本一致性内建，不靠 DBA 的经验 | 写延迟多一次往返；写字节 ×3 |
| 分布式事务：XA 的两阶段长锁、或者 Saga 的补偿代码 | Percolator + 全局 TSO | 事务边界写回业务代码，不用写补偿 | 每次事务多 1–2 次 RTT；锁表；GC safe point |
| 单库上限：单机 IOPS / 单表大小 | Region 数可水平扩 | 「表多大都行」的错觉 | 热点 Region 上限；大事务上限 |

### 贵在哪：三类不可消除的地板

| 地板 | 数值（量级） | 能不能绕 |
| --- | --- | --- |
| **写延迟地板**：1 次 Raft 往返 + 多数派 fsync | 同机房 1–2 ms、跨 AZ 5–9 ms（单 Region 悲观事务） | 不能；只能减事务次数、同 AZ 部署、或接受最终一致 |
| **写字节地板**：LSM 写放大 × 副本数 | 144×（leveled + 3 副本） | 部分能：换 tiered/universal、换压缩、减副本（牺牲可用性） |
| **并行度地板**：一个 Region 一个 Raft group 串行 | 单 Region 10⁴ QPS、单行 10³ QPS | 只能打散（`AUTO_RANDOM` / 分区表），不能靠加机器 |

### 能做什么 / 不能做什么 / 靠什么绕过

| 能做 | 不能做 | 靠什么绕过 |
| --- | --- | --- |
| 单表无限增长的 OLTP（靠 Region 分裂） | 一个热点 key 的高频写（单 Region 串行 + 锁冲突） | `AUTO_RANDOM` / `SHARD_ROW_ID_BITS` 打散；或把计数类热点挪到 Redis（`→ comp/12`） |
| 跨行跨表跨 Region 的 ACID 事务 | 大事务（默认 `txn-total-size-limit` 100 MB 量级，且越大越慢） | 拆批 + 幂等 + 状态机；离线走批量导入（绕开事务） |
| 强一致读（线性一致） | 跨地域低延迟写（跨 AZ 一次事务 16–20 ms） | 同城三 AZ + 异步异地副本；或业务侧最终一致 |
| 在线扩缩容 | **秒级**扩容（迁移是按 MB/s 限速的） | 提前扩容；`store limit` 临时调高（接受前台延迟） |
| 高频点查与范围扫 | 大范围扫 + 聚合（LSM 多层 merge + 不能靠 bloom） | 聚合交给列存 / TiFlash 类副本（`→ comp/20`） |
| PB 级存储 | 每 Region 的稳定 P99（compaction 必然抖动） | 限速 compaction + 预留 1.5× 盘；把长尾负载隔离 |

### ⑤ 扩展维度：分片 + 副本（四选一里的两个一起用）

按 0.4 的尺子，TiKV 的答案是明确的：**分片（Region）× 副本（Raft）**，而且是**同时用**。

- **分片**：Region 是分片单位，分裂让「单表」变成「1 万个可独立调度的单元」。
- **副本**：每个 Region 3（或 5）副本，靠 Raft 做共识，读可以走 follower（历史读）或必须走 leader（强一致读）。
- **垂直切不成立**：LSM 的写放大随数据集**对数上升**（`→ comp/02` §02.6：20 GB 是 35×，1 TB 是 57×），所以「把机器加大」不但拿不到线性收益，还会让 compaction 的绝对字节数更大。
- **批处理摊薄有限**：组提交与批量导入能摊薄 RTT，但摊不薄 compaction 的字节。

**于是 Region 的大小有一个甜点区**（默认 96 MiB 量级）：太小 → Region 数与心跳爆炸、调度风暴；太大 → 单个 Region 的 compaction 与迁移代价变粗、热点更难拆。这个「96 MiB」不是一个可以随便改的运维参数，它是「并行度」与「元数据开销」之间的那条线。

### ⑥ 系统级失效：只写会让整个集群不可用的那几类

使用级错误（指标名写错、参数设错、索引建错）在 §19.10，两者不互相稀释。

| 失效 | 现场 | 为什么是**系统级** |
| --- | --- | --- |
| **PD / TSO 不可用** | PD 多数派丢失或 leader 长时间选不出；所有 SQL 返回超时，`GetTS` 全失败 | 时间戳是**全局**的：没有新 `start_ts`/`commit_ts`，**全库无法开始或提交任何事务**，哪怕所有 TiKV 都健康。这是本架构唯一的「全局单点依赖」 |
| **某 Region 多数派丢失** | 3 副本里 2 个同时不可用（同机架掉电、同 AZ 断网）；该 Region 范围读写全部超时 | 单 Region 不可用本是局部的，但**热点 Region 或元数据 Region** 不可用会让整个库「看起来挂了」；副本放置策略（同 AZ / 同机架）决定这个概率 |
| **Region 分裂 / 迁移风暴** | 写入突增导致 Region 数暴涨，或小表被切成极多 Region；PD CPU 100%、心跳超时、调度队列积压 | 元数据是**集群级共享资源**：Region 数一多，所有 store 的心跳、所有 SQL 的「查路由」都变慢，与具体哪张表无关 |
| **GC safe point 被钉住** | 一个长事务/大事务持续数小时；`tikv_gc_safe_point` 不再推进 | 旧版本无法回收 ⇒ **全集群空间放大持续上涨**，最终所有 store 的 `available` 一起跌。一个业务的长事务能让全集群写满盘 |
| **迁移把盘带宽吃满** | 扩容或故障恢复期间 P99 全表抬升 | 迁移是**集群级的后台流量**，它与前台读共享同一块盘，没有硬隔离 |
| **脑裂 / 落单副本** | 网络分区后旧 leader 仍自认为 leader 的时间窗内 | 由多数派 + 租约兜住，但 **`sync-log=false` 或 `read` 走 local lease 的配置组合**会把窗口放大（假活类失效，`→ comp/52`） |

---

## 19.10 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到物理形态） | 怎么定位 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 把 RocksDB 当数据库用：以为能 `SELECT`、能连地址、能建用户 | 它是**库**不是服务：无协议、无 SQL、无权限（§19.3） | 看架构图里有没有那一层 SQL/协议网关；看 `rocksdb::DB::Open` 在谁的进程里 | 加一层：TiDB 类 SQL 层，或自研 gRPC 服务 + 自己的鉴权 | 把「RocksDB 不提供什么」那张表贴进设计评审模板 |
| 写入飞快、掉电后少一批数据 | `WriteOptions::sync = false`（默认）⇒ WAL 只在 page cache 里；`disableWAL=true` 更彻底 | `GetProperty("rocksdb.wal-file-time")` 与 fsync 次数；对照掉电前的最后一批写 | 关键写用 `sync=true` 或改批量提交；或交给 Raft（`raftstore.sync-log=true`） | 在接口契约里写清 RPO；把 durability 级别做成可配置参数而不是默认值 |
| 写入周期性变慢几百毫秒，均值正常 | compaction 追不上；三种触发见 `→ comp/02` §02.9：L0 文件数 / pending bytes / memtable 数 | `num-files-at-level0`、`estimate-pending-compaction-bytes`、`rocksdb.is-write-stopped` | 加 `max_background_jobs`、限速 compaction、扩盘带宽；根治靠降写放大 | 容量规划按「写放大 × 逻辑写速率」算盘带宽 |
| 上面那条照做了还是抖，加线程反而更抖 | `max_background_jobs` 太小是病，**高峰期调大也是病**：compaction 线程与前台读抢 CPU 与 IO | 看 stall 时间窗与 CPU steal / 盘 util 是否重合 | 错峰调线程；用 `rate_limiter` 把 compaction 限到「盘带宽的 30%」 | 线程数是「分配」问题不是「有没有」问题；把 IO 预算显式分给前台/后台 |
| flush 时长抖动、写延迟周期性尖刺 | column family 开太多：一次 flush/`atomic_flush` 的耗时由最慢/最大的 CF 决定 | 按 CF 看 `rocksdb.num-files-at-level0` 与 flush 耗时；看 `atomic_flush` 是否开启 | 合并 CF（同生命周期放一起）；关掉不必要的 `atomic_flush`；调 `write_buffer_size` 对齐 | CF 数量写进设计纪律：**每个 CF 都要有「它为什么不能和别人共用」的理由** |
| TiKV 里小表也切出几千个 Region，PD CPU 高、调度队列积压 | `region-split-size` 是全局参数，小表也按同一阈值切；Region 数是元数据开销的乘数 | `tikv_raftstore_region_count` 与 PD `region check`；看 Region 数与数据量的比值 | 提高 split size、合并碎片 Region（`unsafe-recover` 类工具需谨慎）、清理无用表 | 「Region 数 ÷ 数据量」做成容量指标；小表不进 TiKV 或走单独集群 |
| 单 Region 写打满，扩容无效（事故二） | 自增/时间前缀主键 ⇒ 写全落在末尾 Region；一个 Raft group 只能串行 | `pd-ctl hot write` 看单 store 占比；看 `tikv_raftstore_region_count` 的 leader 分布 | 主键改 `AUTO_RANDOM` / 加 `SHARD_ROW_ID_BITS`；在线 DDL 重建表 | 建表规范：**凡是写入速率 > 单 Region 上限的表，主键必须自带散列位** |
| 大的跨 Region 事务把锁表与旧版本撑爆 | 事务涉及的 key 全程持锁，且 pin 住 GC safe point ⇒ 旧版本不能回收、空间放大上涨 | `pd-ctl` 的 `gc safe point` 是否推进；锁 CF 大小；`region check pending-peer` | 拆小事务；把批量任务改成「分片提交 + 幂等」；监控最长事务时长 | 事务时长做成一等指标（60 s 告警）；批处理强制分片大小 |
| 用了 `SHARD_ROW_ID_BITS` 之后查询变慢、扇出爆炸 | 主键高位被随机化 ⇒ **按原业务键的范围扫变成全 Region 扫描**（`AUTO_RANDOM` 表的范围查询要扫所有切片） | `EXPLAIN` 里看 `estRows` 与实际扫到的 Region 数；看 `cop_task` 数量 | 给业务键建二级索引；或范围查询改走「按分片逐个点查」 | 打散热点的同时必须**同时**设计访问路径：点查靠完整主键，范围扫必须有独立有序的索引 |
| 面板上 `rocksdb.*` 全是 0，「引擎很健康」（事故三） | `Options::statistics` 默认 `nullptr`，ticker/histogram 全返回 0 | 打开 `statistics` 前后对比同一指标；看 `rocksdb.stats` 输出里 `Stall(us)` 是否为 0 | 打开统计（生产建议 `ExceptDetailedTimers` 级别，避免性能开销） | 所有 `0` 值的面板先做一次「假 0 审查」（§19.11） |

---

## 19.11 观测：指标、拐点与告警

**先立纪律 7 的前提**：每一条都带版本 + 开关。特别地——**RocksDB 的 `statistics` 未开启时，绝大多数计数器读出来是 0**；「0」意味着「没记账」，不是「没发生」。RocksDB 的 `GetProperty()` 类属性（如 `num-files-at-level`、`estimate-pending-compaction-bytes`）不受这个开关影响，这是它们与 ticker 类指标的关键差别。

| 指标 | 组件 / 版本 | 开关或前提 | 拐点（越线就动手） | 对应哪本账 |
| --- | --- | --- | --- | --- |
| 累计 stall 时间（ticker `STALL_MICROS`，属性名 `rocksdb.stall-micros`） | RocksDB 9.x | **必须 `options.statistics = CreateDBStatistics()`**，否则恒为 0 | 任一分钟 > 1 s | 尾延迟 / 写放大积压 |
| `rocksdb.num-files-at-level<N>` 与 `rocksdb.estimate-num-keys` | RocksDB 9.x | `GetProperty()`，**不需要 statistics**；TiKV 侧暴露为 `tikv_engine_num_files_at_level` | `level0 > 20`（`level0_slowdown_writes_trigger` 默认量级） | 读放大 + stall |
| `rocksdb.estimate-pending-compaction-bytes` / `rocksdb.compaction-pending` | RocksDB 9.x | `GetProperty()`；**只统计 compaction，不含 flush** | > 软限（默认量级 64 GB），或 10 分钟内翻倍 | 写放大积压 |
| block cache 命中率 = `hit / (hit + miss)` | RocksDB 9.x | ticker `BLOCK_CACHE_HIT` / `BLOCK_CACHE_MISS`，**需 statistics**；TiKV 侧对应 `tikv_engine_cache_efficiency` | `data` 块命中率 < 70% 且持续恶化 | 读放大（working set 装不下） |
| 每层文件数与总大小（按 CF / 按层） | RocksDB 9.x | `GetColumnFamilyMetaData()`；TiKV 侧 `tikv_engine_size_bytes` | 单层文件数突增、或 L0 大小 > L1 容量 | 空间放大 + 读放大 |
| `rocksdb.is-write-stopped` / `rocksdb.actual-delayed-write-rate` | RocksDB 9.x | `GetProperty()` | `is-write-stopped = 1` 即已进入 stop 区 | 写路径已停 |
| `tikv_raftstore_region_count{type="region"}` | TiKV（指标名跨版本改过） | 需 PD 连通；分区期间本指标会与 PD 侧不一致 | 与 PD 侧 `region count` 差异 > 1%，或 Region 数 10 分钟内涨 > 10% | 分片 / 调度风暴 |
| `tikv_engine_*`（`tikv_engine_size_bytes`、`tikv_engine_pending_compaction_bytes`、`tikv_engine_num_files_at_level`） | TiKV | **指标名与开关项跨版本改过名，以目标版本 `/metrics` 输出为准** | 单 store 的 L0 文件数 > 20，或 pending > 软限 | 写放大 |
| PD `region health`（`pd-ctl region check {miss-peer,extra-peer,down-peer,pending-peer,offline-peer}`） | PD | 子命令名跨版本变过，也可用 `pd-ctl region --jq` 过滤 | `pending-peer` 涉及的 Region > 总数的 5% | 副本一致性 |
| PD `hot region`（`pd-ctl hot write` / `hot read`） | PD | 热点统计有阈值（`hot-region-cache-hits-threshold` 量级），低于阈值不上榜 | 单 store 承载 > 30% 的集群写热点 | 单 Region 上限 |
| PD `store available`（`pd-ctl store` 的 `state_name` 与 `available`；指标 `pd_cluster_status`） | PD | — | 任一 store `available` < 20%，或 state 非 `Up` | 空间放大 / 集群健康 |
| GC safe point 推进间隔 | TiDB（`tikv_gc_safe_point`，用 `SELECT * FROM mysql.tidb` 看） | 长事务会让它停住 | 超过 10 分钟不推进 | 空间放大（旧版本无法回收） |

### 巡检骨架

```bash
# ① TiKV 引擎侧：L0 文件数、pending compaction、stall（stall 需 statistics 已开）
#    指标名以目标版本 /metrics 输出为准：改名过多次，先 grep 再写进面板
curl -s http://127.0.0.1:20180/metrics \
  | grep -E 'tikv_engine_(num_files_at_level|pending_compaction_bytes|size_bytes)|tikv_engine_stall'
# 每层文件数 / 每层大小（子命令是否存在随版本变化，不存在就看 /metrics）
tikv-ctl --host 127.0.0.1:20160 metrics 2>/dev/null | head -40

# ② Region 视角：Region 数、每个 store 的 leader 数、pending peer
curl -s http://127.0.0.1:2379/pd/api/v1/stores | jq '.stores[] | {id: .store.id, state: .store.state_name, leaders: .status.leader_count, regions: .status.region_count, available: .status.available}'
curl -s http://127.0.0.1:2379/pd/api/v1/regions | jq '.count'
pd-ctl -u http://127.0.0.1:2379 region check pending-peer

# ③ 热点：谁是那个「一个 store 干了 80% 写」的元凶
pd-ctl -u http://127.0.0.1:2379 hot write | head -20
pd-ctl -u http://127.0.0.1:2379 store limit          # 迁移限速，扩容前先看这个

# ④ 事务与 GC：长事务会让 safe point 停住，全集群空间放大
mysql -h 127.0.0.1 -P 4000 -e "SELECT * FROM mysql.tidb WHERE variable_name IN ('tikv_gc_safe_point','tikv_gc_last_run_time')"
mysql -h 127.0.0.1 -P 4000 -e "SELECT * FROM information_schema.cluster_tidb_trx ORDER BY start_time LIMIT 5"
```

### 告警表达式骨架（Prometheus 风格）

```text
# ① stall 在涨 → P99 已经开始被污染（前提：RocksDB statistics 已开）
rate(rocksdb_stall_micros_total[5m]) > 0.02          # 每秒 stall 超 20 ms
# ② L0 越过 slowdown 线并持续 2 分钟 → 写入即将被 sleep
max_over_time(rocksdb_num_files_at_level0[2m]) > 20
# ③ 待合并字节越过软限 → 合并速度已持续低于写入速度
rocksdb_estimate_pending_compaction_bytes > 64e9
# ④ 单 store 热点占比过高 → 主键没打散，加节点无用
max by (store) (rate(tikv_raftstore_region_written_bytes[5m]))
  / scalar(sum(rate(tikv_raftstore_region_written_bytes[5m]))) > 0.30
# ⑤ Region 数暴涨 → 分裂风暴，元数据要压垮 PD
delta(tikv_raftstore_region_count{type="region"}[10m]) > 0.10 * tikv_raftstore_region_count{type="region"}
# ⑥ pending-peer 超过 5% → 副本在追日志，读延迟与可用性同时恶化
sum(pd_region_health{type="pending-peer"}) > 0.05 * sum(pd_region_health{type="region"})
```

> **每一条面板都要过一遍「假 0 审查」**：这个指标在默认配置下会不会真的有值？需不需要打开某个开关、某个 feature flag、某个 `statistics`？**事故三的半年白干就是因为没人做这一步。**

---

## 19.12 一页速记

| 记什么 | 具体数字 / 结论 |
| --- | --- |
| 三件套一句话 | **RocksDB 给单机 LSM、Raft 给副本共识、PD 给全局调度与时间戳**；SQL 层在它们外面 |
| RocksDB 是什么 | 一个**库**：无网络协议、无 SQL、无权限、无跨节点复制、无二级索引 |
| 写路径 | `WriteBatch`（原子边界）→ WAL（`sync` 决定 RPO）→ memtable（skiplist / hash skiplist）→ immutable → L0 → L1..Ln |
| column family | **每 CF 一棵独立的 LSM 树**；与 HBase 的「列物理分组」不是一回事；多 CF 默认各自 flush，`atomic_flush` 才一起落盘 |
| compaction 三选一 | leveled 写放大 46–57 / 空间 1.1×；universal 写放大 ≈7 / 空间 ≈10×；FIFO 不是树 |
| 点查文件扇出 | `1 + (max_write_buffer_number−1) + L0 文件数 + (L−1)`；默认参数、240 GB = **10 个**；bloom 把它压成真读 **1.08 个** block |
| bloom 的边界 | 假阳性 0.82%（10 bit/key）；**挡点查不挡范围扫**；prefix bloom 只对前缀 Seek 有效 |
| 写延迟地板 | `2 × RTT + 2 × fsync`：同机房 1–2 ms、跨 AZ 5–9 ms（单 Region 悲观事务全链路） |
| 提交 vs 可见 | Raft 多数派 fsync = **已提交**；apply 到 kv 引擎 = **可读**。两者之间是异步的 |
| 写放大总账 | `46（LSM）× 3（副本）+ raft log ≈ 144×`；400 B 逻辑写 → **57.6 KB** 物理写；vs InnoDB 7.3× 是 **20 倍** |
| 单 Region 上限 | 单 Region **10⁴ QPS**、单行热点 **10³ QPS**；加节点无效，只能 `AUTO_RANDOM` / `SHARD_ROW_ID_BITS`（2^bits 个分片） |
| Region 数 | 1 TB / 96 MB ≈ **1.04 万个 Region**、3.1 万 peer；全量重分布按默认限速要 **两天量级**（46 小时） |
| 跨 Region 事务 | `2 × RTT × N`：同机房 5 Region = 2.0 ms，跨 AZ = 15.0 ms；并行 prewrite 可去掉 N |
| Percolator | `start_ts` → 并行 prewrite（锁+数据）→ `commit_ts` → commit primary（**提交点**）→ 异步清锁 |
| commit_ts 为什么单调 | 快照读、物理顺序 = 逻辑顺序、`commit_ts > start_ts`；由 PD 的 TSO 保证，**永不倒退** |
| 谁清理崩掉的事务 | 遇到锁的**读者**：`CheckTxnStatus(primary)` → 回滚或补提交。没有后台孤儿清理的必要 |
| 系统级失效 | PD/TSO 挂 → 全库写停；Region 多数派丢失 → 范围不可用；分裂/迁移风暴 → 全集群抖；GC safe point 被钉住 → 全集群空间涨 |
| 观测第一纪律 | `statistics` 没开时 `rocksdb.*` 大多是 **0**——**0 不是健康，是没记账** |

**如果只记五句：**

1. **RocksDB 是库不是数据库**：它只承诺「单进程内一批写原子 + 崩溃可恢复」，跨节点、SQL、权限全要你自己补。
2. **NewSQL = LSM + 共识 + 调度器**：它把「分库分表、主从复制、分布式事务」这三件人肉活接管了，代价全记在「写字节 × 副本数」这一笔上。
3. **一次逻辑写在 TiKV 上是 144 份**：LSM 写放大 46 × 3 副本 + raft 日志。容量规划必须按这个数字买盘带宽。
4. **一个 Region = 一个 Raft group = 一条串行写队列**：单 Region 约 10⁴ QPS、单行约 10³ QPS，热点不打散，加多少机器都无效。
5. **分布式事务便宜的原因是可重放**：任意时刻崩溃都不留不一致，只留「锁」；解析锁的是读者自己，于是没有恢复流程要维护——但每一个锁都要等一次 `CheckTxnStatus`。

---

## 19.13 自检题（5 题，答案折叠）

**1. 一张表 500 GB、3 副本、leveled（T=10）、日逻辑写 200 GB。算一下每天要写多少物理字节？换成 universal（写放大 7）能省多少，代价是什么？**

<details><summary>答案</summary>

先定层数：500 GB 落在 L4（累计容量 284 GB）之上、L5 之内（2.8 TB），所以下沉 4 次，`WA_kv = 11×4 + 2 = 46`。

单副本 = 46（kv）+ 2（raft 日志引擎）= 48；全集群 = 48 × 3 = **144×**。

日物理写 = 200 GB × 144 = **28.8 TB/天 ≈ 333 MB/s 均值**（摊到 5 个 store = 67 MB/s/store）。**注意这个数字与「数据集 500 GB」无关，只与「每天写多少逻辑字节」有关**——这是容量评审里最常被搞混的一点。

换 universal：`WA_kv ≈ L + 2 = 6`（4 次下沉 + 2），单副本 8，全集群 24× ⇒ 200 × 24 = **4.8 TB/天**，省 6 倍写带宽。

代价立刻出现：空间放大从 `1+1/T ≈ 1.1×` 抬到 `≈ N ≈ 10×`（同尺寸 run 并存），500 GB 逻辑数据可能占 5 TB 盘；且点查读放大从「每层 1 个文件」变成「每层最多 N 个 run」，范围扫的 merge 层数也翻倍。**在「盘便宜、写带宽贵」的存储型业务里这是好交易；在「点查为主」的 OLTP 里通常不是。** 量级结论，±30%。

</details>

**2. 一个 Region 的写 QPS 上限为什么是 10⁴ 量级？如果业务是对同一行做 5 万 QPS 的扣减，加 10 个 TiKV 节点有用吗？给两条正解。**

<details><summary>答案</summary>

上限来自「串行」：一个 Region 是一个 Raft group，写只能在 leader 上一条条 propose，提交要等多数派 fsync。悲观单 Region 事务 ≈ 3 次 RTT（取锁 / prewrite / commit），同机房 0.2 ms ⇒ 每次 0.6 ms ⇒ 串行约 1,600 TPS；组提交把一次 fsync 摊给 K≈10 个并发事务 ⇒ 上限 1.6 万 TPS。工程上按 **10⁴ QPS/Region** 预算。**加 10 个节点完全无效**：节点只增加「可用的 Region 数」，不增加「一个 Raft group 的并行度」。

对**同一行**做扣减更糟：不但串行 propose，还要抢同一把 Percolator 锁，冲突重试让有效吞吐掉到 **10³ QPS 量级**。

两条正解：① **打散**——加 `SHARD_ROW_ID_BITS = 4`（16 分片）或 `AUTO_RANDOM(5)`（32 分片），把「一行」变成「N 行的余额之和」，事务改成对分片的原子扣减 + 汇总读；② **移出 LSM**——计数/库存水位这类单点高频写放 Redis（`→ comp/12`）或单独的「号段/计数器」服务，TiKV 只存最终对账结果。**注意这只是把瓶颈挪走，不是消灭它**：Redis 单 key 也是单线程的（约 10⁵ QPS 量级，比 TiKV 高两个数量级，但仍不是无限）。

</details>

**3. 跨 Region 事务涉及 5 个 Region，同机房 RTT 0.2 ms。为什么有人算出 2 ms、有人算出 0.4 ms？哪个是对的？**

<details><summary>答案</summary>

两个都对，它们算的是两种执行方式：

- **并行 prewrite**：把 5 个 Region 的 prewrite 同时发出去，等最慢的那个 ⇒ `2 RTT`（prewrite + commit）= **0.4 ms**。
- **串行**：逐个 Region 依次 prewrite 与 commit ⇒ `2 × RTT × N = 2 × 0.2 × 5 =` **2.0 ms**。

`2 × RTT × N` 是**最坏下界**（此时每个 Region 之间还有依赖，或者客户端故意按顺序发）。真实值由客户端的批次切分决定，通常接近并行那一侧（同机房 5 Region 事务端到端 3–5 ms，含 TSO 与两次 fsync）。

推论：**「事务涉及几个 Region」比「改了几行」更决定延迟**。改 1,000 行在同一个 Region 里可能比改 5 行在 5 个 Region 里更快。所以「按 Region 分组、并发发送、重试也按 Region 粒度」是客户端最重要的优化，比调任何服务端参数都有效。量级结论，±50%。

</details>

**4. 一个事务在「primary 已提交、secondary 还没清锁」时客户端崩溃了。数据会不会不一致？谁会修？读者会看到什么？**

<details><summary>答案</summary>

不会不一致。这正是 Percolator 的核心设计：**primary 的提交记录就是唯一的提交点**。primary 上 `commit` 记录已写入（且已过多数派）⇒ 这个事务在逻辑上**已经提交**，secondary 上的锁只是「还没被清理的标记」。

修的人是**任何后来访问这些 key 的读者或写者**：读到 secondary 时发现锁，调 `CheckTxnStatus(primary)`，发现 primary 有 `commit_ts=120` 的提交记录 ⇒ 判定「已提交」⇒ 补写 secondary 的 commit 记录、删锁，然后按 `commit_ts ≤ read_ts` 的规则返回 `@120` 的版本。

读者看到的是**新值**，代价是一次额外的 `CheckTxnStatus` RPC（同机房 0.2–0.5 ms）。与之相对的是「prewrite 全成功、primary 未提交」：此时 `CheckTxnStatus` 发现 primary **没有**提交记录且锁已过期 ⇒ 判定事务失败 ⇒ 写 Rollback 记录、删锁 ⇒ 读者看到**旧值**，而那个已写入 `default` CF 的新值永远不会被读到。

所以 Percolator 没有「恢复流程」，只有「惰性清理」；这既是它便宜的原因，也是它要求**锁必须有超时语义、且所有事务共用同一个 TSO** 的原因。

</details>

**5. 你的面板上 `rocksdb.stall-micros` 一直是 0，`block cache 命中率` 一直是 0，`compaction 次数` 一直是 0。这说明什么？怎么在 10 分钟内验证？**

<details><summary>答案</summary>

这三个指标同时为 0，几乎不可能是「健康」——它们的物理含义完全不同：stall 为 0 意味着「从没减速」，命中率为 0 意味着「每次读都 miss」，compaction 为 0 意味着「SST 从不合并」。三者同时成立在真实系统里不可能，唯一的共同解释是**没有记账**。

根因：RocksDB 的 `Options::statistics` 默认是 `nullptr`，ticker/histogram 类的计数器（`STALL_MICROS`、`BLOCK_CACHE_HIT/MISS`、`COMPACTION_*`）全部返回 0。

验证方法：① 在一个非核心实例上加 `options.statistics = rocksdb::CreateDBStatistics()`（TiKV 侧对应开 `rocksdb` 配置块里的 metrics 相关开关，具体项以目标版本配置模板为准），重启后看这三个值是否立刻非 0；② 用不受 statistics 影响的 `GetProperty()` 类指标做交叉验证——`rocksdb.num-files-at-level<N>`、`rocksdb.estimate-pending-compaction-bytes`、`rocksdb.is-write-stopped` **在没开统计时依然有值**。如果属性有值而 ticker 全 0，就确诊了。

纪律：所有 `0` 值面板都要过一次「假 0 审查」——**这个指标在默认配置下会不会真的有值？需要哪个开关？** 事故三为此付了半年和 8 TB 盘的代价。

</details>

---

## 19.14 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| LSM 写放大 `T×L` 的完整推导、leveled vs tiered 的取舍、write stall 的三种触发（本篇 §19.4–19.5 全部锚定在那里，未重写） | [02 存储引擎三形态：B+ 树、LSM 与列存](02-storage-engine-three-shapes.md) §02.4、§02.6、§02.9 |
| WAL 与崩溃恢复：为什么「先写日志再改页」能重放（RocksDB 的 WAL 与 raft log 是同一个道理） | `→ comp/03` |
| InnoDB 的页与行：B+ 树一侧的对照物，本篇 §19.8 的 7.3× 出自它 | [05 InnoDB 的页与行](05-innodb-page-and-row.md) §05.5、§05.10 |
| MVCC 的两种实现对照：undo 版本链（InnoDB）vs 时间戳前缀（TiKV 的 `default`/`write`/`lock`，§19.6） | [06 MVCC 与隔离级别](06-mvcc-and-isolation-levels.md) §06.2–06.3 |
| HBase 的「Region」是**按 RowKey 范围切的一段表**（由哪台 RegionServer 服务），TiKV 的「Region」是**一个 Raft group 的数据范围**（哪三副本构成共识组）——同名不同物，完整推导看它，本篇 §19.6 只给一句辨析；RowKey 设计纪律与 `AUTO_RANDOM` 是同一件事的两副面孔 | `→ comp/18` |
| 列存与 MergeTree：为什么「压测看着更好」的引擎在点更新上必然失败（本篇 §19.4 的另一种形态） | `→ comp/20` |
| 分布式事务的六种工程方案与补偿写法（Percolator 只是其中一种「引擎内建」路线） | [eng/8 分布式事务总览](../eng/8-distributed-transaction-overview.md) |
| NoSQL / NewSQL 选型矩阵：什么时候该选 TiKV、什么时候该回到单机 MySQL | [eng/37 NoSQL 选型](../eng/37-nosql-selection.md) |
| 分布式 ID 与号段（`AUTO_RANDOM` 与 Snowflake 在「打散 vs 有序」上的同一个权衡） | `→ comp/45` |
| Raft 的一层：ZAB 与 Raft 的会话语义对照（共识组件的通用失效面） | `→ comp/37` |
| 五类失效（慢 / 满 / 脑裂 / 假活 / 语义漂移）与本篇 §19.9 ⑥ 的对应关系 | `→ comp/52` |
| 单线程事件循环为什么能到 10⁵ QPS（把热点行移出 LSM 的那个去处） | `→ comp/12` |
| 全套的尺子与写作公约（八问卡、三本放大账、数字纪律） | [0-summary 总纲](0-summary.md) §0.4、§0.7 |
