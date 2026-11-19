# 18. HBase：LSM、Region 与 RowKey 设计

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · LSM 族与分析 ｜ **编号**：18 / 53
> **本篇定位**：Part IV 的第二篇，也是全书写「**分布式 LSM**」的唯一入口。LSM 的通用机制（memtable → immutable → L0 → Ln、`T×L` 的 compaction 写放大、bloom 挡点查）在 [02 存储引擎三形态](02-storage-engine-three-shapes.md) 已经推完，本篇**一个字都不重写**，只回答一个问题：**在一台机器上成立的 LSM，被水平切成分片（Region）之后，多了哪几件必须自己承担的事**。答案是三件：Region 是按 RowKey 字典序切的（于是键的分布 = 负载的分布）、HFile 是 HBase 自己的 SSTable（块索引 + 布隆过滤器 + 数据块）、列族是物理单位（每个 CF 一套 MemStore 与一套文件族）。这三件事推出本篇支点：**HBase 没有二级索引，它的唯一索引就是 RowKey 的字典序，所以 RowKey 设计就是索引设计，也是均衡设计**。
> **读法**：§18.1 三起事故全部是「一半机器闲着、一半在过载」，先建立「这不是调优问题」的直觉；§18.2 的唯一真问题是全篇支点；§18.3–18.5 是物理底座（三层形态图、写路径、读路径与布隆）；**§18.6 是核心**，RowKey 的三种解法和它们的读代价都在那里；§18.8 的四个算例可直接搬进容量评审，每个都标了「量级结论 / 承诺值」。只想知道结论的读 §18.9 的边界表与 §18.12 的速记。

---

## 18.1 引子：三起「集群一半机器闲着，一半在过载」的故障

HBase 的事故有个共同特征：**没有报错、没有慢查询、`hbase hbck` 能报 OK**。面板上是一台机器的 CPU 100%、GC 停顿 3 秒、compaction 队列几百，其余机器 CPU 8%。三起事故分别对应本篇的三个物理事实：**键的分布（§18.6）**、**列族数量（§18.7）**、**Region 数量（§18.6 末）**。

### 事故一：RowKey 用 `yyyyMMddHHmmss` 前缀，半年后 18 台集群的写全压在最后 1 台（2025-08-11 → 2026-02-03，某物联网设备时序平台）

`ts_event` 表存设备上报事件，ROWKEY = `yyyyMMddHHmmss + deviceId`（谁都觉得这个设计「天然按时间排序、范围查询快」）。集群 18 台 RegionServer（32C / 128 GB / 2×NVMe），稳态写入 9 万 QPS、每行 320 B。

| 时间 | 事件 | 写入 QPS | 逻辑写带宽 | 热点 RS 的 CPU / GC | 写 P99 | 空闲 RS 数 |
| --- | --- | --- | --- | --- | --- | --- |
| 08-11（上线） | 历史数据用 bulkload 铺满 90 个预切 Region，写暂时分散 | 9 万 | 28.8 MB/s | 34% / 0.1 s | 8 ms | 0 |
| 09-20 | 增量写入全部落到字典序最大的 Region（预切区已写满） | 9 万 | 28.8 MB/s | 61% / 0.8 s | 22 ms | 11 |
| 11-05 | 大促峰值；每次 split 出的新 Region 仍只接最后那段键 | 27 万 | 86.4 MB/s | 100% / 2.8 s | 240 ms | 16 |
| 12-10 | 第一次扩容 18 → 24 台（**无效**：新机器上的 Region 从不接收写入） | 27 万 | 86.4 MB/s | 100% / 3.1 s | 380 ms | 22 |
| 01-14 | 第二次扩容 24 → 32 台（**无效**），客户端超时率 14%，重试放大 2.6× | 31 万 | 99 MB/s | 100% / 4.4 s | **1,200 ms** | 30 |
| 01-28 | 第三次扩容 32 → 40 台（**无效**），hot region 的 storefile 数撞 `blockingStoreFiles` | 31 万 | 99 MB/s | 100% / 5.2 s | 超时（>10 s） | 38 |
| 02-03 | 新表 `ts_event_v2`（RowKey 加 2 位桶号）+ 双写 + 回填，切流量 | 31 万 | 分 16 个 Region | 51%（最忙那台） | 12 ms | 0 |
| 02-04 复盘 | 三次扩容约 180 万元、40 台机器里 38 台在空转；改键 4 人日 + 迁移 9 天 | — | — | — | — | — |

**关键事实：扩容加的是「Region 的容器」，不是「Region 的分布」。** 按 RowKey 字典序切分的表，新写入的键永远是字典序最大的那一小段，因此**无论集群多少台机器，最新的写永远只落在一个 Region 上**。半年里这个 Region 每 6 分钟切一次（10 GB ÷ 28.8 MB/s ≈ 356 s），一共留下 **4.3 万个从此零写入的「死 Region」**，它们照样占着 meta 行、内存里的 MemStore 结构与打开的 HFile 句柄。

### 事故二：一张表开了 8 个列族，compaction 队列长期 400+，读 P99 从 20 ms 涨到 2 s（2025-12-02，某风控画像服务）

同一张 `entity_profile` 表，为了「逻辑上分开放」开了 8 个 Column Family：`base` / `device` / `risk` / `behavior` / `tag` / `ext` / `audit` / `tmp`。业务只写其中 3 个 CF，但 HBase 的 flush 与 compaction 是**按 Store（一个 CF 一套）**做的。

| 时间 | 事件 | 每 Region storefile 数 | compaction 队列 | 磁盘写 util | 读 P99 |
| --- | --- | --- | --- | --- | --- |
| 12-02 09:00 | 上线（1 CF → 8 CF 的 migration） | 9 | 12 | 31% | 20 ms |
| 12-02 13:00 | 每次 flush 产出 8 个小 HFile，撞 `blockingStoreFiles` | 46 | 120 | 68% | 60 ms |
| 12-02 19:00 | compaction 事件数 ×8，每次合并字节 ÷8，固定开销不变 | 88 | 320 | 91% | 420 ms |
| 12-03 02:00 | 夜间批量导入叠加，compaction 永远追不上 | 140 | **410** | 100% | **2,000 ms** |
| 12-03 05:00 | 合并 8 CF → 2 CF（业务上真正需要分开的只有 2 个），重建表 | 11 | 8 | 34% | 22 ms |
| 12-04 复盘 | 画像接口 6 小时不可用，风控决策降级为「仅规则」，间接损失约 90 万元 | — | — | — | — |

**「列族」听起来像逻辑分组，物理上却是一整套独立的 MemStore + flush + 文件族。** 同一份数据分成 8 个 CF，HFile 数量 ×8、每个文件小 8 倍，而每次 compaction 的固定开销（打开/关闭 HDFS 文件、重写 bloom 与块索引、元数据）**不随文件变小而减少**。

### 事故三：Region 数涨到 2.4 万，master 每次重启要 40 分钟才稳定（2026-01-09，某广告明细表）

`ad_impression` 表为「均衡」把 `hbase.hregion.max.filesize` 调到 1 GB、split 策略设为 `ConstantSizeRegionSplitPolicy`，于是每 1 GB 就切一刀。两年后 2.4 万 Region、20 台 RS（每台 1,200 个 Region）。

| 时间 | 事件 | Region 数 | RIT 并发 | master 重启后稳定耗时 | 影响 |
| --- | --- | --- | --- | --- | --- |
| 01-09 03:00 | master 例行滚动重启 | 24,000 | 40 | — | — |
| 01-09 03:02 | assignment 开始，ZooKeeper / meta 写放大 | 24,000 | 600 | — | DDL 全停 |
| 01-09 03:15 | 部分 RS 打开 Region 超时，RIT 反复重试 | 24,000 | 1,800 | — | 客户端 meta 缓存失效，全表 5–30 s 抖动 |
| 01-09 03:31 | RIT 收敛 | 24,000 | 120 | — | 写入恢复 |
| 01-09 03:42 | 全集群稳定 | 24,000 | 0 | **40 分钟** | 期间写入失败率 9% |
| 01-11 | `merge_region` 合并到 4,000 个（每 Region 5 GB 量级），重启稳定时间 | 4,000 | 8 | **5 分钟** | 正常 |

**Region 不是免费的。** 每个 Region 是一份需要被打开、被汇报、被均衡、被写进 `hbase:meta` 的状态；2.4 万个状态在 master 重启时要逐个走一遍 assignment 状态机，而**吞吐上限是每秒十几到二十几个 Region（无竞争时）**——`24,000 ÷ 15 ≈ 1,600 s`，加上 RIT 重试，就是那 40 分钟。

三起事故分别落在 §18.6（键的分布决定负载分布）、§18.7（CF 是物理单位）、§18.6 末（Region 数量与 split 策略）。

---

## 18.2 一句话定位与唯一真问题

**定位**：HBase = **一台机器上的 LSM**（WAL + MemStore + HFile + compaction，机制见 [02 存储引擎三形态](02-storage-engine-three-shapes.md) §02.4）+ **按 RowKey 字典序水平切分的 Region**（分片）+ **HDFS 的三副本**（复制）+ **一个只做元数据与调度的 HMaster**。它有三个"不是"：不是关系库（没有二级索引、没有跨行事务）、不是缓存（每次读都要过存储层）、不是通用 KV（键的类型是 byte[]，语义只有字典序）。

### 唯一真问题

> **HBase 的性能不是调优出来的，是 RowKey 设计出来的——因为它的唯一索引就是 RowKey 的字典序，写按字典序落到 Region，单调递增的键会把整个集群的写压在一个 Region 上，而这件事扩容救不了。**

三条推论，后面每一节都在给它们找证据：

1. **RowKey 同时是「主键」「索引」「分片键」「排序键」四合一。** 在 MySQL 里这四件事可以分别设计（主键、二级索引、分片键、聚簇顺序）；在 HBase 里它们是**同一个字节串的字典序**，所以任何「按另一个字段查」的需求，都只能靠「把那个字段编进 RowKey」或「全表扫」来满足（→ §18.6、§18.9）。
2. **分片有效性 = 键分布的均匀性。** LSM 的单机写吞吐很高（见 [02](02-storage-engine-three-shapes.md) §02.6），但 HBase 只能把这台机器的写能力乘上「有写入的 Region 数」；Region 再多，如果只有一个 Region 有写入，集群就等于一台机器（§18.8 账一）。
3. **HBase 的失败几乎都不是「慢」，是「不均衡」，而不均衡在监控上表现为「慢」。** 于是所有人第一反应是调参数、扩机器、加大堆——三件都不改变键的分布（事故一）。

### 八问卡在本篇的落点

| 八问 | 本章落点 | 一句话答案 |
| --- | --- | --- |
| ① 承诺什么 | §18.9 | 单行（单个 RowKey）原子 + 按 RowKey 有序的范围扫；**不承诺**跨行事务、二级索引、低延迟的任意字段查询 |
| ② 数据长什么样 | §18.3、§18.5 | WAL → MemStore → HFile（数据块 + 块索引 + 布隆）；Region = RowKey 的一段字典序区间 |
| ③ 一次读写走哪条路 | §18.4、§18.5 | 写：RPC → WAL → MemStore → 返回（异步 flush + compaction）；读：meta 定位 → Region → MemStore/BlockCache → bloom → 块索引 → 数据块 |
| ④ 并发与一致 | §18.9 | 单行 CAS（`checkAndMutate`）+ 多版本（`maxVersions`）；跨行只能靠应用层两阶段或外部事务组件 |
| ⑤ **扩展维度** | §18.6、§18.8 | **分片**（Region，且只能按 RowKey 切）+ **批处理摊薄**（bulkload 跳过 WAL/MemStore）；副本由 HDFS 提供，**不增加写吞吐**；读副本（RegionReplica）只扩读 |
| ⑥ 会在哪崩（只写系统级） | §18.6 末、§18.11 | `hbase:meta` 所在 RS 抖动 → 全集群定位失败；单个 RS 的 WAL 管道慢 → 该机所有写阻塞；RIT 风暴 → master 状态机失控；热点 Region → 客户端重试放大传染全集群 |
| ⑦ 看哪几个指标 | §18.11 | 每 Region 的请求分布、compaction 队列、MemStore/flush 队列、每 Region storefile 数、GC 停顿、Region 数 |
| ⑧ 换掉它的信号 | §18.9、§18.12 | 需要「按非 RowKey 字段的低延迟查询」、需要跨行事务、或热点键无法加盐（如全局单调 ID）→ 换掉或加一层（ES / ClickHouse / NewSQL） |

---

## 18.3 物理形态：LSM + HFile + Region 三层

HBase 的物理形态是三个层次的嵌套，**每一层各自只解决一件事**：

```text
第三层：集群 = HMaster（元数据 + 调度）+ N 个 RegionServer（真正读写）+ HDFS（副本）
   │         HMaster 不碰数据：只管 assignment、balance、DDL、hbase:meta 的搬迁
   ▼
第二层：表 = 按 RowKey 字典序水平切成的 Region 序列（半开区间 [startKey, endKey)）
   │         每个 Region 就是一个「只属于一段键区间的 LSM 实例」
   ▼
第一层：单个 Region 内部 = 每个 Column Family 一个 Store
   │         Store = 1 个 MemStore（内存跳表）+ N 个 StoreFile（HFile，不可变）
   ▼
最内层：HFile（HBase 自己的 SSTable）
         数据块(64 KB 量级) │ 块索引 │ 布隆过滤器 │ trailer
             ↑ 读时按索引定位块；bloom 用于整文件级排除
```

### 三层各管什么，各自的最坏情况

| 层 | 物理单位 | 它解决什么 | 它引入的新问题（本篇主线） |
| --- | --- | --- | --- |
| LSM（Region 内） | MemStore / HFile | 把随机写变成追加写（机制 → [02](02-storage-engine-three-shapes.md) §02.4） | compaction 写放大、读要碰多个 HFile、flush 抖动 |
| Region（表内） | 一段 RowKey 字典序区间 | 把一张表水平切开，让多台机器分担 | **切分依据是键的字典序 ⇒ 键分布 = 负载分布**；Region 数本身有成本 |
| 集群（HMaster + RS） | 进程与元数据 | 调度、故障转移、副本（交给 HDFS） | master 是元数据单点（有 backup，但 failover 期间 DDL 与 assignment 停摆）；WAL 是**每个 RS 一条**共享管道 |

### 三个必须记牢的物理事实

1. **Region 不复制，副本在 HDFS。** Region 只是「一段键区间的读写入口」，它的数据文件在 HDFS 上已经是 3 副本。这推出两件事：① HBase 的持久性上限 = HDFS 的持久性；② **加副本不会提升写吞吐**（HDFS 管道本来就三份），提升写吞吐的唯一办法是让更多 Region 同时有写入（§18.6）。
2. **Region 的边界是 RowKey 的字节序。** 不是哈希，不是范围划分，是 `byte[]` 的字典序比较。所以 `rowkey=9` 在 `rowkey=10` 之后（字符串比较），`0x00ff` 在 `0x01` 之前——任何「键看起来有序」的直觉都要回到字节比较上（→ §18.6 的三种解法都从这一点推出）。
3. **一个 Store = 一个 CF = 一套独立的 MemStore 与文件族。** 8 个 CF 的表不是「一张表」，是「每个 Region 里 8 个几乎独立的小 LSM」（§18.7）。

> LSM 的通用机制——memtable 写满 → immutable → flush 成 L0 → 分层 compaction、`T×L` 的写放大推导、bloom 的假阳性率与内存换算——一律→ [02 §02.4 / §02.6](02-storage-engine-three-shapes.md)，本篇只写 HBase 在这套机制上**多出来的东西**。

---

## 18.4 写路径：WAL、MemStore 与 compaction 的三份账

一次 `Put` 在 HBase 里要落三份东西，这是本篇所有存储账的源头。

```text
Put(rowkey, cf:q, v)          ← 客户端已把 rowkey 定好，服务端不再「选」任何东西
  │
  ├─① 客户端缓存 meta：rowkey → Region → RegionServer（命中则 0 次额外 RPC）
  │      └─ 未命中：先查 hbase:meta（1 次跨机 RPC，1–3 ms），再连目标 RS
  │
  ├─② RS 的 RPC 层排队 → 取 Region 的写锁（按 rowkey 加锁，单行互斥）
  │
  ├─③ 追加 WAL（每个 RS 一条，所有 Region 共用）            ← 账一：写放大 +1×（HDFS 三副本 ×3）
  │      默认 Durability.USE_DEFAULT → 不逐条 fsync（异步，靠 logroll 周期兜底）
  │      显式 SYNC_WAL 才会每次 fsync：+1–3 ms，且靠 group commit 摊薄
  │
  ├─④ 写 MemStore（跳表插入，微秒级）→ 释放行锁 → 返回客户端成功
  │      └─ 到这里客户端已看到「成功」，但它还在内存里
  │
  ├─⑤ 异步：MemStore 达刷写阈值（每 store 一个阈值；多个 MemStore 共用 RS 堆的写缓冲配额）
  │      → flush 成 1 个 HFile，写 HDFS（顺序写）              ← 账二：写放大 +1×（×3 副本）
  │
  └─⑥ 异步：storefile 数达阈值 → minor compaction（合并部分）
        周期到 → major compaction（把该 Region 的**全部** HFile 重写成 1 份）  ← 账三：写放大 4–8×
```

### 每一跳的耗时量级

| 跳 | 动作 | 耗时量级 | 谁在决定它 |
| --- | --- | --- | --- |
| ① | 客户端 → RS 的 RPC（同机房） | 0.2–1 ms | 网络 RTT + `hbase.regionserver.handler.count` 排队 |
| ② | meta 未命中时查 `hbase:meta` | +1–3 ms（**只在缓存失效时**） | 客户端 meta 缓存是否新鲜；meta 所在 RS 是否健康（§18.11） |
| ③ | 追加 WAL（不 fsync） | 0.1–0.5 ms | WAL 管道是否健康；HDFS datanode 是否慢 |
| ③' | 追加 WAL（`SYNC_WAL`） | +1–3 ms，group commit 后可摊到 0.2–0.5 ms | 同一条管道上的并发写数 |
| ④ | MemStore 插入 | 1–10 μs | 跳表大小、GC 停顿 |
| ⑤ | flush 128 MB 量级的一次 | 1–5 s（后台，不阻塞写，但吃 IO） | 盘写带宽 ÷ 副本数、compaction 是否在抢 IO |
| ⑥ | major compaction 重写一个 Region | 10 GB 量级 ÷ (盘带宽/2) ≈ 20–60 s | Region 大小、盘带宽、是否有并发 compaction |

**这条表最重要的一行是 ③'。** HBase 默认的持久性不是「每次写都落盘」：`Durability.USE_DEFAULT` 下 WAL 是异步刷的，机器整机掉电会丢最近一个 logroll/flush 周期内的写（量级：秒到分钟）。**要「提交即落盘」必须显式 `SYNC_WAL`**，代价是那条**所有 Region 共享的** WAL 管道上每次多一次 fsync——这也解释了为什么「一个 RS 上有个别大 Region 在 flush」会拖慢同机所有表的写。

### 三份账，一句话一份

| 账 | 物理动作 | 量级 | 它决定了什么 |
| --- | --- | --- | --- |
| **WAL** | 每次写追加到**每 RS 一条**的日志，HDFS 三副本 | 逻辑写 ×1（×3 副本） | 持久性边界；也是「同机相互影响」的通道 |
| **flush** | MemStore 满 → 1 个 HFile（每 CF 一个） | 逻辑写 ×1（×3 副本） | **HFile 产生速率 = 读放大的分母**（§18.5） |
| **compaction** | minor 合并部分、major 合并全部 | 逻辑写 ×4–8（size-tiered 式的级联合并，量级 6；major 是周期性全量重写） | 尾延迟抖动、以及「写放大 + 空间放大」的总账（§18.8 账三） |

**为什么 compaction 是 4–8×**：HBase 的默认合并是 size-tiered 式的（`hbase.hstore.compactionThreshold` 量级 3，攒够若干同量级 HFile 就合并成更大的一个），而不是 RocksDB 的分层。于是一字节数据从「刚 flush 出来的 128 MB」长到「10 GB 的 Region」，要经历 `log2(10 GB / 128 MB) ≈ 6.3` 次级联合并——**每次合并都把它重写一遍，所以平均重写约 6 次**。再叠加 major compaction（`hbase.hregion.majorcompaction` 默认 7 天量级）把整个 Region 重写成一份，实际写放大量级 **6–10×**。

**major compaction 为什么必须单独看**：它把**一个 Region 的全部 HFile 重写成一份**。好处是读放大瞬间回到最小（1 个文件，bloom 都不用查）；代价是这一下要读写整个 Region 的数据量，而且**发生在哪个时刻不可控**（默认按周期 + 抖动触发）。业务高峰手动触发 `major_compact` 就是把「20 分钟的盘 IO」精准地安排在流量峰值（§18.10 第 8 行）。

---

## 18.5 读路径：块索引与布隆过滤器

HFile 是 HBase 自己的 SSTable，内部分三件有用的东西：**数据块（默认 64 KB 量级，`hbase.hregion.blocksize`）+ 块索引 + 布隆过滤器**。读一个 key 的顺序是「先查块索引定位块 → 布隆排除不相交的 HFile → 读块」。

```text
Get(rowkey)
  │
  ├─① 定位 Region（客户端 meta 缓存，命中即 0 RPC）
  ▼
RegionServer → Region → 该 Region 的每个 Store（= 每个 CF，读一次要过所有 CF！）
  │
  ├─② 查 MemStore（跳表，纯内存，微秒）；在则返回
  ├─③ 查 BlockCache（读过的数据块/索引块/bloom 块都在这里）
  │
  ├─④ 对每个 StoreFile（HFile）：先查它自己的布隆过滤器
  │       ├─ bloom 说「没有这个 key」→ 整个文件跳过（不读一个字节）  ← 关键收益
  │       └─ bloom 说「可能有」（含假阳性）→ 进入 ⑤
  │
  ├─⑤ 查块索引（二分）→ 定位到唯一一个数据块
  ├─⑥ 读该数据块（64 KB 量级）→ 块内二分/扫描 → 命中行
  │
  └─⑦ 多版本：同 key 按 timestamp 取最近 maxVersions 个，过滤 TTL 过期
```

### 布隆过滤器把「N 个 HFile 全查」压成「几乎只查 1 个」

假阳性率的换算（标准结论，**只给区间不给分项**）：HBase 的 `io.storefile.bloom.error.rate` 默认为 **1%** 量级，对应每键约 **9–10 bit**；配到 0.1% 量级需每键约 **14–15 bit**。

**推导**：一个 key 在某个 Region 里只可能存在**一个** HFile 中（同一个 Store 的 HFile 之间键区间可以重叠，同一 key 的多个版本也可能跨文件——这里取「稳态下每 key 通常只落在一处」的常见情形），其余 `N−1` 个文件都是「明确不相交」。

- 无 bloom：需要读 `N` 个文件的块索引 + 可能读 `N` 个数据块。
- 有 bloom：需要读 `1` 个真文件 + 其余 `N−1` 个文件的假阳性 `(N−1)×p`。

```text
期望 HFile 读取次数 = 1 + (N − 1) × p
N = 10, p = 1%   → 1 + 9 × 0.01 = 1.09
N = 12, p = 1%   → 1 + 11 × 0.01 = 1.11
N = 10, p = 3%   → 1 + 9 × 0.03 = 1.27   （把 error.rate 放宽到 3% 换内存）
N = 30, p = 1%   → 1 + 29 × 0.01 = 1.29   （文件数多到 30 时，假阳性开始累积）
```

| HFile 数 N | 无 bloom 的块读 | 有 bloom（p=1%） | 收益 | 冷读延迟（按 2 ms/块估算） |
| --- | --- | --- | --- | --- |
| 4 | 4 | 1.03 | 3.9× | 8 ms → 2.1 ms |
| 10 | 10 | **1.09** | 9.2× | 20 ms → 2.2 ms |
| 12 | 12 | 1.11 | 10.8× | 24 ms → 2.2 ms |
| 30 | 30 | 1.29 | 23× | 60 ms → 2.6 ms |
| 100 | 100 | 1.99 | 50× | 200 ms → 4.0 ms |

**注意两件事**：① bloom 只帮**点查**，**范围扫完全无效**（范围谓词的键分布在所有 HFile 里，一个都跳不掉）——这与 LSM 的结论一致（→ [02 §02.4](02-storage-engine-three-shapes.md)）。② bloom 帮不到「一次 Get 要读多个 CF」这件事：Get 会过该 Region 的**每一个** Store，CF 越多，单次 Get 的期望文件数越大（§18.7）。

**bloom 的内存账**：`内存 ≈ 行数 × bits/key ÷ 8`。取 250 亿行（10 TB ÷ 400 B）、10 bit/key → **31 GB**，摊到 20 台 RS 约 1.5 GB/台，可以常驻 BlockCache；换成 14.4 bit/key（0.1%）→ **45 GB**。**结论：bloom 内存与行数成正比、与 Region/文件数无关**——所以「为了读放大而切更多小 Region」不会让 bloom 变便宜，只会让每个 RS 上多出更多 bloom 块与索引块。

---

## 18.6 RowKey 设计就是索引设计

### 为什么「RowKey 设计 = 索引设计」

HBase **没有二级索引**：你不能在 `deviceId` 上建一个索引然后按它查。它唯一「有序」的东西，是每张表按 RowKey 字节序排出的那条长序列，而 Region 就是这条序列上的一段。

这正是 [04 索引本质](04-index-essence-btree-page.md) §04.5 的形式化结论在分布式上的原样照搬：**能高效定位的只有「从第一列开始的连续前缀」**。把那条长序列横着切成 Region 之后，多出一条新推论：

> **前缀决定「能不能定位」，而前缀的取值分布决定「定位到的那一段在哪台机器上」。**

所以 RowKey 的设计一次要满足四个约束，而它们经常互相冲突：① 查询能定位（前缀）；② 写入能分散（前缀的取值要均匀）；③ 排序有业务意义（时间/ID 顺序）；④ 长度要短（每个 cell 都带一份 rowkey，见 §18.8 账三）。

### 单调递增的键为什么必然造成热点

```text
RowKey = 20260203101530-<deviceId>     （时间戳前缀，字典序 = 时间序）

序列：  [20260201000000 … 20260203101529] │ [20260203101530 … 20260203101559] │ [ 20260203101600 …
        └──────── Region A（冻结）──────┘ └──── Region B（刚写完）─────────┘ └─ Region C：唯一的写入口
                                                                              ▲
        所有新写入的键，字典序永远最大 ─────────────────────────────────────────┘

时间推移 → 写位置沿着序列向右推进 → 左边全是死 Region，右边永远只有 1 个活 Region
          18 台机器 × 每台 100 个 Region = 1,800 个 Region，其中 1,799 个此刻零写入

加盐后 RowKey = <bucket:2 位>-20260203101530-<deviceId>    （bucket = hash(deviceId) % 16）

        bucket 00: […… 101530 …]  ←─ 1/16 的写
        bucket 01: […… 101530 …]  ←─ 1/16 的写
        ……
        bucket 15: […… 101530 …]  ←─ 1/16 的写
        16 个 Region 同时接收写，各自内部仍按时间有序
        代价：按时间范围扫 = 必须同时打 16 个 Region（扇出，见 §18.8 账二）
```

**算式（写成一行就能进评审）**：

```text
单 Region 的写带宽上限 ≈ 30–60 MB/s（逻辑字节；推导见 §18.8 账一）
集群可承载写带宽 ≈ 单 Region 上限 × 有写入的 Region 数
单调递增键 ⇒ 有写入的 Region 数 = 1 ⇒ 集群写上限 = 一台机器
```

这就解释了事故一里「扩到 40 台仍然 100% CPU」：**扩容增加了容器的数量，没有增加有写入的容器数量。**

### 三种解法与它们各自的读代价

| 解法 | RowKey 形态 | 写分散效果 | **读的时候要扇出几个 Region** | 代价与坑 |
| --- | --- | --- | --- | --- |
| **加盐（salting）** | `<bucket>-<原键>`，bucket = hash(原键某字段) % N | 均匀（N 个 Region） | **点查 1 个**（bucket 可算）；**按原键前缀之外的条件查 = N 个** | 桶数固定后难改（改桶数要重建表）；N 个桶 = N 份 scan 并发；桶内顺序仍是原键序 |
| **哈希（hash 前缀）** | `<hash(原键)>-<原键>` 或 `md5(原键)+原键` | 均匀 | 同加盐（前缀可算则 1 个） | 哈希后的键**完全失去业务顺序**：任何范围查询都要 N 路扇出 + 客户端归并；且 rowkey 变长 8–32 B |
| **反转（reverse）** | `reverse(原键)`，如手机号/时间戳倒序 | 只对「尾部变化」的键有效（如手机号后 4 位随机） | 1 个（点查无损） | 反转后**顺序语义反转**（时间戳反转 = 最新在最前，范围扫要反着写）；对真正单调的键无效（反转后仍然单调，只是递减！） |

**一句判据**：**加盐/哈希买的是写分布，卖的是「按原键前缀之外的条件做范围查询」**。如果你的查询永远是「按原键精确查（点查）」或「按原键前缀范围查」，加盐几乎免费；如果你的核心查询是「按时间范围扫全量」（事故一那种时序表），那么加盐之后每次时间范围扫都要打 16 个 Region，**而且桶号在前会让每个桶内的时间区间不再连续**（若时间戳在原键尾部），退化成 16 次全桶扫。

### RowKey 形态 × 写分布 × 读代价 × 适用场景

| RowKey 形态 | 写分布 | 点查代价 | 范围扫代价 | 适用场景 | 反例（别用） |
| --- | --- | --- | --- | --- | --- |
| `<时间戳>+<id>` | **极差**（全压 1 个 Region） | 1 Region | 时间范围 1 Region（最优） | 只追加、且**写入速率低到单 Region 能吃下**、以时间范围扫为主的归档表 | 事故一：高写入时序表 |
| `<业务维度>+<时间戳>`（如 `userId+ts`） | 取决于维度基数：用户数多则天然分散；大客户独占 | 1 Region（带维度） | 该维度内 1 Region | **天然热点分离**的用户维度表：基数百级以上、无超级大客户 | 少数超级大客户（如 `userId=平台方`），仍会热点 |
| `<bucket>+<维度>+<时间戳>`（加盐） | 均匀（bucket 数个 Region） | 1 Region（bucket 可算） | 跨维度扫 = bucket 个 Region | 高写入、以点查/单维度查为主 | 需要对全表按时间扫（扇出 ×bucket） |
| `<hash(键)>+<原键>` | 均匀 | 1 Region | **必须全扫**（顺序全失） | 只有点查、没有范围查询的 KV 型 | 任何需要 scan 的场景 |
| `<reverse(键)>` | 看键形状（尾部随机才行） | 1 Region | 语义反转，scan 要反写 | 手机号 / 订单号尾号随机且要按尾号查 | 真正单调的键（反转后仍单调，白改） |
| `<固定长度分桶前缀>+<ts>`（如 4 位分钟桶） | 每秒/分钟一个桶，随时间推进 | 1 Region | 时间范围 = 桶数个 Region（可控） | 「时间局部热点」场景，桶数是可调旋钮 | 桶粒度太细（= 没加盐） |

### `hbase.hregion.max.filesize`、split policy 与 Region 数量

| split 策略（`hbase.regionserver.region.split.policy`） | 切分依据 | 后果 | 什么时候用 |
| --- | --- | --- | --- |
| `ConstantSizeRegionSplitPolicy` | 达到 `hbase.hregion.max.filesize` 就切 | **最简单也最危险**：阈值设小 → Region 数爆炸（事故三） | 手动管理 Region 大小的成熟团队 |
| `IncreasingToUpperBoundRegionSplitPolicy` | 上限与「该表在某 RS 上的 Region 数」相关，被上限 `max.filesize` 夹住 | 早期切得碎（表刚建时 Region 小） | 表从小到大的自然成长 |
| `SteppingSplitPolicy` | 按 RS 上 Region 数分阶梯 | 早期少切、后期按上限切 | 2.x 常见默认（以目标版本 `hbase-default.xml` 为准） |
| `KeyPrefixRegionSplitPolicy` / `DelimitedKeyPrefixRegionSplitPolicy` | 按 RowKey 前缀（分隔符前）切，保证**同一前缀不跨 Region** | 业务局部性好（单前缀查询只打 1 个 Region）；但前缀热点更集中 | 前缀就是业务维度（`userId`）且要保证局部性 |
| `DisabledRegionSplitPolicy` | 不自动切，只能手动 `split` | Region 可以长到 20–50 GB，读放大与 compaction 更好管理 | **现代（2.x + 大堆 + HDFS）推荐** |
| `hbase.hregion.max.filesize` | 上面各策略共用的上界 | 2.x 默认量级 **10 GB**（以目标版本 `hbase-default.xml` 为准）；调到 1 GB 就是自造事故三 | 10–50 GB 区间是现代常见取值 |

**Region 过多的三重成本**（这三条是可算的，不是感觉）：

| 成本 | 推导 | 拐点 |
| --- | --- | --- |
| master 重启的 assignment 时间 | `Region 数 ÷ assignment 吞吐（无竞争时 10–20 个/秒量级）`：24,000 ÷ 15 ≈ 1,600 s，加 RIT 重试 → 事故三的 40 分钟 | Region 总数 > 1 万，或每 RS > 数百个 |
| 打开的 HFile 句柄 | `Region 数 × 每 Region 稳态 storefile 数`：2.4 万 × 8 ≈ 19 万，逼近 `ulimit -n`（常见 3.2 万–6.4 万） | 句柄数 / 限制 > 60% |
| MemStore 内存地板 | `Region 数 × CF 数 × 每 store 最小驻留（MB 量级）`：2.4 万 × 1 × 1 MB = 24 GB，还没算任何有效数据 | Region 数 × CF 数 > 2 万（在 32 GB 堆上） |

---

## 18.7 列族是物理单位：CF 越多，compaction 越凶

**CF 不是一个「逻辑分组标签」，它是一套独立的 MemStore + 一套独立的 flush + 一套独立的 HFile 文件族。** 建 CF 的成本清单：

| 每个 CF 独立拥有的东西 | 后果 | 谁来付 |
| --- | --- | --- |
| 一个 MemStore（内存里的跳表 + cell 集合） | 内存占用 ×CF 数；空 CF 也占结构 | RS 堆 → GC 停顿（§18.11） |
| 一套 flush 逻辑（每个 store 各写自己的 HFile） | **一次 flush 产出 CF 数个 HFile** | HFile 数 ×CF → 读放大与 compaction 事件数 |
| 一套 HFile 文件族（每个 Region 每个 CF 一组） | 句柄数 ×CF；每文件都有 bloom + 块索引 + trailer | 打开/关闭开销、BlockCache 里的元数据块 |
| 一套 compaction 队列占用 | compaction 事件数 ×CF，但每次合并的字节数 ÷CF | **固定开销不变而字节变少 ⇒ 吞吐崩塌** |
| 一套自己的压缩 / 编码 / TTL / `maxVersions` 设置 | 水位不同 → flush 频率不同 → 小文件更多 | 运维复杂度 |

### 一次 flush 放大成 8 个文件的推导

```
假设：Region 级 MemStore 刷写阈值 = 128 MB 量级（hbase.hregion.memstore.flush.size）

1 CF ：一次 flush → 1 个 128 MB 的 HFile
6 CF ：region 的写分散到 6 个 store，region 级阈值到点后各 store 一起 flush
       → 6 个 HFile，每个约 128/6 ≈ 21 MB

→ HFile 产生速率 ×6，单文件体积 ÷6
→ 达 hbase.hstore.blockingStoreFiles（2.x 默认 16 量级）所需的 flush 次数：
     1 CF：16 次 flush
     6 CF：16 ÷ 6 ≈ 2.7 次 flush    ← 三次 flush 就撞「阻塞写」线
→ compaction 事件数 ×6，每次合并的字节数 ÷6；
  但每次 compaction 的固定开销（打开/关闭 HDFS 文件、重写 bloom 与块索引、
  读多个文件的元数据）不随字节数下降
→ compaction 线程的有效吞吐掉 5–8 倍（事故二实测）
```

再叠一层：**compaction 的输出也是小文件**。minor compaction 把 3 个 21 MB 合成 1 个 63 MB 的文件当然更好，但下一次 flush 又进来 6 个 21 MB 的文件，于是「攒 3 个→合并」的循环在 6 个 CF 上以 6 倍频率发生。**6 CF × 3 次 flush = 18 个文件，正好是阻塞线的量级**——这就是事故二里「队列长期 400+」的算术。

### 该开几个 CF：判据表

| 判据 | 结论 |
| --- | --- |
| 这批数据是否**读取模式完全不同**（如「总是只读 3 列、另外 50 列很少读」）？ | 是 → 可以拆；否 → **不拆** |
| 这批数据是否**生命周期完全不同**（一个要 TTL 7 天、一个永久）？ | 是 → 拆（TTL 是按 CF 生效的） |
| 这批数据是否**大小悬殊**（一行 200 B vs 一行 200 KB）？ | 是 → 拆（避免小 CF 被大 CF 的 flush 拖着一起写） |
| 拆出来的 CF 是否**每次查询都要一起读**？ | 是 → **绝对不拆**：一次 Get 会过所有 Store，CF 越多单次读的期望文件数越大 |
| 你能接受的每 Region storefile 数上限是多少？ | 16（`blockingStoreFiles`）× 1/CF 数 = 你能容忍的 flush 次数，**CF 数 > 3 就该重算这条** |
| **默认答案** | **1–2 个 CF**。3 个是上限，5 个以上必须给出「生命周期或大小确实差一个量级」的书面理由 |

---

## 18.8 三本账：热点、存储放大与读放大

四个算例，每个末尾标「量级结论 / 承诺值」+ 假设清单。**换任一假设请按步进表逐行复算，不要直接引用结论。**

### 账一 · 热点 RowKey：12 万 QPS 全压在一个 Region，单机接得住吗

**假设清单**：20 台 RS（32C / 128 GB / 2×NVMe，单盘持续顺序写 1.1 GB/s）；写入 12 万 QPS、每行 400 B；RowKey 单调递增；`hbase.hregion.max.filesize` 10 GB；压缩未开（保守）；HDFS 三副本。

| 步 | 推导 | 结果 |
| --- | --- | --- |
| H1 | 逻辑写带宽 = 120,000 × 400 B | **48 MB/s** |
| H2 | 有写入的 Region 数（单调键）= 1 | 1 |
| H3 | 该 Region 所在 RS 的物理写 ≈ 48 × (1 WAL + 1 flush + 4 compaction) | **288 MB/s**（另加 compaction 读 192 MB/s） |
| H4 | 单 RS 可持续逻辑写上限（量级）= 盘可持续写 1.1 GB/s ÷ 2（留给读）× 1/10（WA 8–10） | **约 45–55 MB/s** |
| H5 | 该 RS 负载率 = 48 ÷ 45 | **107%**（最好情况刚好打满，典型情况越线） |
| H6 | 集群可承载逻辑写 ≈ 20 × 45 | **约 900 MB/s**；48 MB/s 只用到 **5%** |
| H7 | 活跃 Region 占比 = 1 ÷ (20 台 × 数百 Region) | **< 0.1%** |
| H8 | 10 GB Region 被写满的周期 = 10,240 MB ÷ 48 MB/s | **213 s ≈ 3.6 分钟** |
| H9 | 每天 split 次数 = 86,400 ÷ 213 | **约 405 次/天** → 每月 1.2 万个「零写入的死 Region」 |

> **量级结论（非承诺值）**：48 MB/s 的写全压在一个 Region 上，等于让**一台机器承担 20 台集群 5% 的容量里 100% 的负载**；负载率 107%，且每 3.6 分钟切出一个新 Region、留下一个死 Region。**结论：单机接不住，且扩容不改变这个比值。** 机动空间：H4 的单机上限定在 30–60 MB/s（取决于盘、副本管道、flush 线程数），换任一假设 H5 在 80%–160% 浮动，但「集群利用率 <10% 而热点机 >100%」这个结论不变。

### 账二 · 加盐 16 桶之后：写解决了，读付出了什么

**假设清单**：同上表；RowKey 改为 `<bucket:2 位>-<原键>`，bucket = `hash(原键) % 16`；原键以「设备 ID + 时间戳」组成。

| 步 | 推导 | 结果 |
| --- | --- | --- |
| S1 | 每 Region 承载写 = 48 ÷ 16 | **3 MB/s**（单机上限的 7%，舒适） |
| S2 | 跨机 RTT + 序列化固定开销（每 Region 一次 Scan） | 1–3 ms |
| S3 | **按原键点查**（bucket 可算）扇出 = 1 | 1 个 Region，**无额外代价** |
| S4 | **按时间范围扫**（时间不是原键前缀）：必须打 16 个 Region | 扇出 **16** |
| S5 | 若每个桶内时间维度不连续（时间戳在原键尾部）→ 每桶**全桶扫** | 读放大 **16×**：取 5 分钟数据要读全表 |
| S6 | 若 RowKey 改为 `<bucket>-<时间戳>-<设备>`（时间紧跟桶号）→ 每桶内时间连续 | 16 个并发 Scan，各读 1/16 → 总读 = 目标数据量，**无放大**，但 RPC 数 ×16 |
| S7 | RPC 并发上限：`hbase.regionserver.handler.count` 量级 30/台；16 个并发 Scan 分散在 16 台 → 每台 1 个 | 不排队；**桶数 > 30 且落同一台时开始排队** |
| S8 | 扇出 16 的 P99 = 单桶 scan + RTT + 归并 ≈ 200 ms + 3 ms + 归并 | **≈ 210 ms**（与单 Region 的 200 ms 同量级） |
| S9 | 桶数改成 256：RPC ×256、客户端归并 256 路、每 Region 的 scan 更碎 | P99 开始由「最慢那个 Region」决定 → **尾延迟 ×1.5–2** |

> **量级结论（非承诺值）**：加盐 16 桶把写带宽从 48 MB/s 摊到 3 MB/s/Region（**解决了写**），代价是**非前缀查询的扇出从 1 变成 16**。扇出的代价分两种：① 若桶后紧跟时间戳，扇出 16 只是 RPC 数 ×16、延迟几乎不变（210 ms vs 200 ms）；② 若时间戳在键尾，16 个桶都要全扫，读放大 **16×**——这才是加盐真正的账单。**加盐的位置（放在键开头）与「原键内部哪些字段有序」必须和查询形态一起设计，否则等于把写的问题换成读的问题。**

### 账三 · 存储放大：原始 10 TB 数据要占多少盘

**假设清单**：逻辑数据 10 TB（业务字节，未压缩）；不压缩（HBase 默认）；HDFS 三副本；`maxVersions` 取 1、未设 TTL（先算最理想情况）；WAL 稳态残留取 1 小时的写入量（48 MB/s）。

| 步 | 推导 | 结果 |
| --- | --- | --- |
| D1 | HFile 逻辑字节（不压缩）= 逻辑数据 | 10 TB |
| D2 | LSM 空间放大（storefile 多层 + 未回收旧版本，leveled 类比） | ×1.2–1.5 → **12–15 TB** |
| D3 | HDFS 三副本 × 3 | **36–45 TB** |
| D4 | WAL 稳态残留 = 48 MB/s × 3,600 s × 3 副本 | 0.52 TB |
| D5 | 合计（稳态） | **36–45 TB，取 40 TB 量级** |
| D6 | major compaction 瞬时（重写一份新的再删旧的）= D5 × 4/3 | **峰值约 53 TB** |
| D7 | 开 SNAPPY（1.5–2×）后 | **20–30 TB**（峰值 35 TB） |
| D8 | 若 `maxVersions=3` 且无 TTL：保留 3 份 → D2 变 ×3–4.5 | **90–135 TB** |

> **量级结论（非承诺值）**：**「10 TB 数据」在 HBase 上要按 36–45 TB 下单，major compaction 期间按 53 TB 预留。** 三个乘数里最大的是**三副本（×3）**，其次是 **LSM 空间放大（×1.2–1.5）**，WAL 可以忽略（0.5 TB 量级）。两个能立刻省钱的旋钮：开压缩（20–30 TB）、给历史数据设 TTL（避免 D8）。误差来源：压缩比（±50%）、副本数（EC 可降到 1.5×）、以及 Region 数过小时的碎片浪费。

### 账四 · 读放大：一个 key 落在 12 个 HFile 里

**假设清单**：目标 Region 有 12 个 storefile（未到 major compaction 的稳态）；`hbase.hregion.blocksize` 64 KB 量级；bloom `io.storefile.bloom.error.rate = 1%`；冷读（BlockCache 未命中）按 2 ms/块，bloom 块常驻 BlockCache 按 10 μs/次。

| 步 | 推导 | 结果 |
| --- | --- | --- |
| R1 | 无 bloom：读 12 个文件的块索引 + 12 个数据块 | 12 次块 IO ≈ **24 ms** |
| R2 | 有 bloom：期望文件数 = 1 + 11 × 0.01 | **1.11** |
| R3 | bloom 查询本身 = 12 次（每次 10 μs，常驻 BlockCache） | 0.12 ms |
| R4 | 有 bloom 的 IO = 1.11 次块读 + 索引 | **≈ 2.3 ms** |
| R5 | 收益 | IO **10.8×**、延迟 **10.4×** |
| R6 | bloom 内存（250 亿行 × 10 bit ÷ 8） | **31 GB**（集群摊 1.5 GB/RS） |
| R7 | 若 12 个文件里有 3 个是「同 key 的旧版本」（更新型负载）→ 这 3 个的 bloom **不会命中失败**（key 确实在） | 期望文件数回到 ≥3，**bloom 对此无能为力** |

> **量级结论（非承诺值）**：bloom 把「N 个 HFile 全查」压成 `1 + (N−1)×p` 次，12 个文件时 IO 从 12 次降到 **1.11 次**、延迟从 24 ms 降到 **2.3 ms**。**但它只对「key 不存在于该文件」有效**：更新型负载里同一个 key 的多个版本会落在多个文件（R7），此时 bloom 全部“命中”，读放大回到版本数——这是「高频更新同一行」在 HBase 上的真实代价。

---

## 18.9 架构边界：单行原子能挡到哪里

### 一致性边界的三条事实

1. **单行（单个 RowKey）写原子，且跨 CF 也原子。** 一行里的多个列族、多列的一次 `Put` 是一次原子操作（Region 内加行锁 + WAL 单条记录）——这与 MongoDB 的「单文档原子」同构（→ comp/17）。
2. **`checkAndPut` / `checkAndMutate` 是单行 CAS**：读-比较-写在 Region 的行锁内完成，不需要外部锁。它是 HBase 上唯一的「事务原语」。HBase 2.4 起 `checkAndPut` 标记弃用、3.0 移除，改用 `checkAndMutate`。
3. **跨行没有事务——HBase 2.x 也没有通用事务。** 没有多行原子、没有隔离级别、没有回滚。2.x 的 `Increment` / `Append` 是单行原子操作；跨行的原子性只能靠应用层（两阶段 + 补偿 + 幂等，→ eng/8）或外部事务组件。

```java
// 单行 CAS：把工单状态从 PENDING 改成 DONE，只有原值匹配才成功（HBase 2.x）
TableName TN = TableName.valueOf("ops:work_order");
byte[] CF = Bytes.toBytes("s"), Q = Bytes.toBytes("state");

CheckAndMutate cam = CheckAndMutate.newBuilder(rowKey)
    .ifEquals(CF, Q, Bytes.toBytes("PENDING"))
    .build(new Put(rowKey).addColumn(CF, Q, Bytes.toBytes("DONE")));

boolean ok = table.checkAndMutate(cam);   // false = 别人先改了，本行未被触碰
if (!ok) {
    // 这里拿到的是「明确的失败」，不是「不知道成功没成功」
    metrics.counter("wo.cas.conflict").increment();
}
// 注意：CAS 只在单行内原子。下面这种「两行一起改」没有等价物：
//   t.put(putOf(workOrderKey));  t.put(putOf(auditKey));   // ← 两次 Put，可能只成功一次
```

### 多版本（`maxVersions`）与 TTL 的存储代价

| 旋钮 | 它做了什么 | 存储代价 | 什么时候必须设 |
| --- | --- | --- | --- |
| `maxVersions`（CF 级） | 每个 cell 保留最近 N 个 timestamp 版本 | 空间 ×(实际版本数)，且旧版本只在 compaction 时才真正消失 → **空间放大可达 ×2–4.5**（§18.8 D8） | 默认 1 就是「只留最新」；设 3 以上必须有「要读历史」的明确需求 |
| TTL（CF 级） | 超过存活期的 cell 在 compaction 时被丢弃 | 空间不再无界增长；**但删除是「读时过滤 + compaction 时回收」**，盘不立刻变小 | **所有只追加的表都必须设**（事故：未设 TTL 吃满盘） |
| `KeepDeletedCells` / 墓碑 | 删除标记本身也占位，直到 compaction | 删除比例高时空间滞后于逻辑删除 | 不做「高频删同一 key」的设计 |

### 业务需求 → 能不能只用单行原子

| 业务需求 | 能只用单行原子？ | 物理依据 / 绕过方式 |
| --- | --- | --- |
| 按 `orderId` 查订单详情 | ✅ | 单行 Get，RowKey = `orderId` |
| 订单状态机（待支付 → 已支付 → 已发货），防重复提交 | ✅ | 单行 `checkAndMutate`：状态是列值，CAS 就是状态机守卫 |
| 计数器（累加） | ✅ | 单行 `Increment`（单行原子） |
| 同一订单的「订单行 + 支付流水 + 库存扣减」三处一致 | ❌ | 三个 RowKey = 三行；需要应用层两阶段/补偿（→ eng/8），或把三份塞进**同一个 RowKey 的不同 CF**（代价：粒度粗、单行变大） |
| 「余额不超卖」跨两行转账 | ❌ | 无跨行事务；只能外部事务组件 / 换成 NewSQL（→ comp/19） |
| 「按用户查最近 30 天所有事件」 | ⚠️ | 只能靠 RowKey 前缀设计（`userId+ts`）或第二种解法：加盐后扇出（§18.6） |
| 「按手机号查用户」（主键是 userId） | ⚠️ | **HBase 没有二级索引**：要么把手机号编进 RowKey（反向建第二张表），要么全表扫（§18.10 第 4 行） |
| 全局唯一 ID 单调递增 | ❌（作 RowKey 时） | 单调 ID 作 RowKey = 事故一；可用「雪花 ID 的位翻转 + 加盐」或按业务维度前置 |
| 「读到的就是最终值」（对账） | ⚠️ | 多版本 + 最终一致；对账要处理 bloom 无法排除的旧版本（§18.8 R7） |

### 能力边界三列

| 能做 | 不能做 | 靠什么绕过 |
| --- | --- | --- |
| RowKey 点查（1 个 Region，bloom 后 ~1.1 次块读） | 按非 RowKey 字段的低延迟查询 | 建「二级表」（索引表：`<字段值>→<rowkey>`，自己维护一致性）；或旁路一份 ES / ClickHouse |
| RowKey 前缀范围扫（有序、可切分） | 全表按任意维度范围扫（要扫所有 Region） | 改为前缀设计；或离线导出一份列存（→ comp/20） |
| 单行原子 + 单行 CAS | 跨行 / 多表事务 | 应用层两阶段 + 幂等 + 对账（→ eng/8）；换 NewSQL（→ comp/19） |
| 千万级 QPS 级的水平扩展（**前提是键分布均匀**） | 「扩容就变快」（键分布不变则扩容无效） | 加盐 / 哈希 / 反转（§18.6），代价是扇出 |
| 海量稀疏宽表的存储（10 TB–PB） | 低延迟的任意字段筛选 | 分析类查询走 ClickHouse/Iceberg；HBase 只承担点查与前缀扫 |
| 单行版本与 TTL（CF 级） | 细粒度的历史查询（按版本区间扫） | 把历史写进独立的 RowKey（`<key>#<ts>`），自己控制保留 |

---

## 18.10 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到物理形态） | 怎么定位 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| **集群一半机器 CPU 8%、一台 100%**；扩容无效（事故一） | RowKey 单调递增（`yyyyMMddHHmmss` 前缀）→ 写永远落字典序最大的那个 Region | `hbase hbtop` 看每 Region 的请求分布；看 split 出的新 Region 是否集中在一台；看每 RS 的 `regionCount` 与 `readRequestCount/writeRequestCount` 分布 | 新表加桶号前缀 + 双写 + 回填 + 切流量（不能原地改键） | 建表评审强制「RowKey 前 1–2 字节必须是分散维度」；上线前用真实键分布做一次分区模拟 |
| **compaction 队列长期数百、读 P99 从 20 ms 到 2 s**（事故二） | CF 开到 8 个 → 每 Region 8 个 Store，每次 flush 出 8 个小 HFile，compaction 事件数 ×8 而固定开销不变 | 每 Region 的 storefile 数；`CompactionQueueSize`（以目标版本 JMX 输出为准）；`hbase hstore:blockingStoreFiles` 是否被打满 | 合并 CF 到 1–2 个并重建表；调大 `hbase.hregion.memstore.flush.size` 让文件变大 | CF 数写进建表模板（默认 1–2，>3 要书面理由）；监控每 Region storefile 数的均值与 P99 |
| **master 重启后 40 分钟才稳定，期间写入失败 9%**（事故三） | Region 数 2.4 万 → assignment 状态机逐个迁移，吞吐上限每秒十几个 | `hbase hbck -j hbase-hbck2.jar`（`assigns`、`reportMissingRegionsInMeta`）；master 日志的 RIT 数量；每 RS 的 regionCount | `merge_region` 合并到数千个；调大 `hbase.hregion.max.filesize` 到 10–50 GB | Region 总数与每 RS Region 数进容量红线；禁用 `ConstantSizeRegionSplitPolicy` + 1 GB 这种组合 |
| **按非 RowKey 字段查询变慢，日志里是一次持续几十秒的 Scan** | HBase 没有二级索引，非键查询 = 全表扫全部 Region | 慢请求里 `Scan` 的 `ScanMetrics`（扫的 Region 数、返回行数）；客户端线程被长 Scan 占住 | 建索引表（`<字段>` 作 RowKey，值为原 rowkey）；或旁路 ES；**别在主集群上跑全表扫** | 所有查询必须列出「用哪个前缀定位」；查询 API 层做「无前缀查询」的拒绝/告警 |
| **把 HBase 当 OLTP 主库，多行更新后数据对不上** | 跨行无事务：两次 Put 之间进程挂了 → 半成功 | 对账：抽样比对各 RowKey 的关联字段；查失败的 Put 是否有补偿日志 | 改成「单行多 CF」把相关数据收进一个 RowKey；或引入两阶段 + 补偿 + 幂等表 | 数据模型评审明确「事务边界 = 一个 RowKey」；跨行一致性必须写在架构图里 |
| **盘一直在涨，逻辑删除不生效，最后写满** | 未设 TTL / `maxVersions` 过大：旧版本与墓碑要等 compaction 才物理消失 | 表大小 vs 逻辑数据量（`hdfs dfs -du`）；每 CF 的 `maxVersions`/`TTL` 配置；major compaction 上次完成时间 | 设 TTL 与 `maxVersions`，触发一次 major compaction 回收 | 所有只追加表**建表即设 TTL**；容量规划按 §18.8 账三的 ×3.6–4.5 下单 |
| **业务高峰手动跑 `major_compact`，全集群 IO 打满、P99 翻 10 倍** | major compaction 把每个 Region 的全部 HFile 重写一遍，读写各一份全量数据 | 盘 util 与 compaction 时间窗重合；`CompactionQueueSize` 与 compaction 读写字节突增 | 停掉；改到低峰；或按 Region 分批（`major_compact 'region_name'`） | 禁止手动 major compaction 进变更流程；用 `hbase.hregion.majorcompaction` 的周期 + 抖动参数控制节奏 |
| **`blockingStoreFiles` 撞线，写入被阻塞几十秒** | 写入速率 > compaction 消化速率（CF 多 / Region 小 / 盘慢），storefile 数越过阈值后 HBase **主动阻塞写入** | 每 Region storefile 数；`CompactionQueueSize`；写请求的 P999 与 compaction 时间窗 | 临时加 compaction 线程/限速调优；根治是减少 CF、调大 Region 与 flush 阈值 | 按「日写入量 × compaction 写放大」算 compaction 的 IO 预算，不按「存储容量」算 |
| **开启 row-col 级 bloom 后 BlockCache 抖动、命中率下降** | `BLOOMFILTER => 'ROWCOL'` 的位图按 cell 计，比 row 级大一个列数量级 | bloom 块占用的 BlockCache 比例；BlockCache 命中率 | 只在高选择性、查询必带 qualifier 的表上开 ROWCOL，其余用 ROW | 建表模板默认 ROW；ROWCOL 要给出「每行 cell 数」的估算 |
| **未开压缩，盘是同类系统的 2 倍** | HBase 默认不压缩；HFile 只是追加 + 索引，没有列存那套编码 | 表大小 vs 逻辑数据量；CF 的 `COMPRESSION` 属性 | `alter` 加 SNAPPY 并触发 major compaction 重写 | 建表模板默认 SNAPPY；容量按压缩后下单 |

---

## 18.11 观测：指标、拐点与告警

**纪律 7**：每条指标都带版本与开关前提。HBase 的指标走 JMX（`Hadoop:service=HBase,name=RegionServer,sub=…`）或 `hbase-metrics2` 的 metrics sink；**属性名在 2.x 各小版本之间重命名过**，凡不确定的地方都写「以目标版本 metrics 输出为准」，不猜。

| 指标 | 版本 / 开关前提 | 拐点（越线就动手） | 对应哪本账 |
| --- | --- | --- | --- |
| **每 Region 的读写请求分布**（`hbase hbtop`，2.x 自带；或 JMX `sub=Server` 的 `readRequestCount` / `writeRequestCount` 按 Region 汇总） | HBase 2.x；`hbtop` 需 RS 健康且 master 可连 | **单个 Region 的写入占全表 > 30%**，或 Top1 Region 的请求量是均值的 5 倍以上 | 热点（§18.8 账一）——**这是 HBase 最重要的一个指标，没有第二** |
| **compaction 队列长度**（`CompactionQueueSize`，属性名以目标版本 JMX 输出为准） | HBase 2.x；compaction 指标在 RS 的 `sub=Server`/compaction 相关 MBean 下 | **持续 > 50 且 10 分钟内不回落**；事故二到过 410 | 写放大积压（§18.7） |
| **每 Region 的 storefile 数**（JMX `storeFileCount` 之类，按 Region 汇总；或 hbtop 的 region 视图） | HBase 2.x | 均值 > 10，或任 Region 逼近 `hbase.hstore.blockingStoreFiles`（默认 16 量级） | **读放大的直接指标**（§18.8 账四） |
| **MemStore 总量与 flush 队列**（`MemStoreSize`；flush 队列长度属性名以目标版本 metrics 输出为准） | HBase 2.x；`hbase.regionserver.global.memstore.size` 默认在总堆 0.4 量级（跨版本会调整，以 `hbase-default.xml` 为准） | MemStore 总量 > 配额 × 0.8，或 flush 队列持续 > 0 且增长 | 写缓冲（§18.4 账二、§18.7） |
| **Region 数**（master 侧 `regionCount` / `hbase:meta` 行数） | HBase 2.x | 每 RS > 数百个，或集群总数 > 1 万 | master assignment 与句柄（§18.6 末） |
| **`hbase:meta` 定位延迟与命中率** | 客户端侧 meta 缓存；服务端看 meta 所在 RS 的 RPC 延迟。**具体指标名以目标版本 metrics 输出为准** | 客户端 Get 的 P99 出现「周期性 5–30 s 尖刺」且与 meta RS 的 GC 重合 | 系统级失效（⑥）：meta 抖 = 全集群定位失败 |
| **GC 停顿**（JVM GC 指标，如 G1 的 pause time；HBase 不自带，需从 JVM 侧采） | 与 HBase 版本无关，取决于 JVM（2.x 常配 JDK 8/11 + G1） | 单次 Full GC > 1 s，或 Young GC 频率与 MemStore 大小同步上升 | MemStore 大 → Full GC 风险（§18.7 内存地板） |
| **WAL 写入延迟 / 管道异常** | HDFS 侧指标（`DataNode` pipeline 相关）+ RS 的 WAL 指标；名字以目标版本为准 | 同一 RS 上所有表的写 P99 同时抬高 | 系统级失效：WAL 是每 RS 一条的共享管道 |
| **RIT（region in transition）数量** | master 日志与 `hbck2` 的 `assigns` 输出；JMX 上以目标版本为准 | 持续 > 100（事故三到过 1,800） | 系统级失效：RIT 风暴 |

采集骨架（巡检用，可直接进 runbook）：

```bash
# ① 表与 Region 层面：谁热、谁碎
hbase shell <<'EOF'
status 'detailed'                       # 每台 RS 的 region 数、请求数、storefile 数
list_regions 'ts_event'                 # 每 Region 的起止 key —— 一眼看出键区间是否均匀
scan 'hbase:meta', {LIMIT => 3}         # meta 是否可读（定位链路的第一跳）
EOF

# ② 热点：按 Region 看读写请求分布（2.x 自带 hbtop）
hbase hbtop -mode regionserver  # 交互界面，按列排序；重点看每 Region 的写入速率 Top1

# ③ storefile 数与 compaction 队列（属性名跨版本有重命名，先 dump 一遍再 grep）
curl -s http://rs01:16030/jmx?qry=Hadoop:service=HBase,name=RegionServer,sub=Server \
  | python3 -c 'import json,sys;d=json.load(sys.stdin);[print(k,"=",v) for b in d["beans"] for k,v in b.items() if any(s in k.lower() for s in ("compaction","memstore","storefile","regioncount"))]'

# ④ Region 数异常/结构异常
hbase hbck -j hbase-hbck2.jar assigns      # RIT 与未分配 Region（HBCK2 独立发布，配合 2.x）
hbase hbck -j hbase-hbck2.jar reportMissingRegionsInMeta
```

告警表达式骨架（Prometheus 风格，阈值取自本节拐点列；指标名以目标版本导出器为准）：

```text
# 热点：单 Region 写入占比过高 —— 这是 HBase 的头号告警，优先于一切延迟告警
max by (table) (hbase_region_write_requests_rate) / sum by (table) (hbase_region_write_requests_rate) > 0.3
# compaction 积压：队列长时间不回落
max_over_time(hbase_regionserver_compaction_queue_size[10m]) > 50
# 读放大：每 Region 的 storefile 数逼近 blockingStoreFiles
avg by (table) (hbase_region_storefile_count) > 10
# MemStore 水位：逼近全局写缓冲配额
hbase_regionserver_memstore_size / hbase_regionserver_max_heap > 0.32
# GC：单次停顿超过 1 秒（JVM 侧导出，名字依 exporter 而定）
max_over_time(jvm_gc_pause_seconds_max[5m]) > 1
```

---

## 18.12 一页速记

| 记什么 | 具体结论 / 数字 |
| --- | --- |
| 物理形态三层 | 单机 LSM（WAL → MemStore → HFile → compaction）→ 表按 RowKey 字典序切成 Region → 集群 = HMaster（元数据/调度）+ RegionServer（读写）+ HDFS 三副本 |
| 唯一真问题 | 性能不是调优出来的，是 **RowKey 设计**出来的：唯一索引就是字典序，单调键把全集群的写压在一个 Region，扩容救不了 |
| 写路径三份账 | WAL（每 RS 一条，×1）+ flush（每 CF 一个 HFile，×1）+ compaction（4–8×，`log2(Region/flush阈值) ≈ 6` 次重写） |
| major compaction | 把一个 Region 的**全部** HFile 重写成一份：读放大瞬间最小，代价是全量重写 + 触发时刻不可控（默认 7 天量级） |
| 读路径 | meta 定位 → Region → 每个 CF 的 Store → MemStore/BlockCache → bloom 排文件 → 块索引定位 → 64 KB 块 |
| bloom 的收益 | 期望文件数 `1 + (N−1)×p`，p=1%：10 个文件 → 1.09 次、12 个 → 1.11 次；**范围扫无效**；内存 = 行数 × 10 bit ÷ 8（250 亿行 ≈ 31 GB） |
| 热点算式 | 集群写上限 = 单 Region 上限（30–60 MB/s）× **有写入的 Region 数**；单调键时后者 = 1 |
| 三种解法 | 加盐（桶号前缀）/ 哈希（哈希前缀）/ 反转；都用「读扇出 ×N」或「失去顺序」换写分布 |
| 加盐的账 | 16 桶：写 48 → 3 MB/s/Region；非前缀查询扇出 16；若时间戳在键尾，16 个桶各要全扫 = **读放大 16×** |
| CF 是物理单位 | 每 CF 一套 MemStore + flush + HFile；6 CF ⇒ 一次 flush 出 6 个小文件，16 个就撞 `blockingStoreFiles`（≈2.7 次 flush）；**默认 1–2 个** |
| Region 数成本 | assignment 吞吐 10–20 个/秒 ⇒ 2.4 万 Region 要 30–40 分钟；句柄 = Region × storefile；MemStore 内存地板 = Region × CF × MB |
| 存储放大 | 逻辑 10 TB → 稳态 36–45 TB、major 峰值 ~53 TB；三副本 ×3 是第一大乘数，开 SNAPPY 减半 |
| 一致性边界 | 单行（跨 CF）原子 + 单行 CAS（`checkAndMutate`）；跨行无事务（2.x 也没有） |
| 头号观测指标 | **每 Region 的写入请求分布**（单 Region 占比 >30% 即告警），其次 compaction 队列、每 Region storefile 数、MemStore 水位、GC 停顿 |

**如果只记五句：**

1. **HBase 的 RowKey 一个字段干四份活**：主键、唯一索引、分片键、排序键。所以「RowKey 设计」不是表结构细节，是架构决策。
2. **写按字典序落 Region**：键单调 ⇒ 全集群写压一个 Region ⇒ 集群利用率 <10% 而热点机 >100%，扩容只是增加空转的机器。
3. **加盐买写分布、卖查询自由度**：点查无损（桶号可算），非前缀范围扫按桶数扇出；时间戳在键尾时更糟——16 个桶各要全扫。
4. **列族不是逻辑分组**：每 CF 一套 MemStore + flush + 文件族，CF 数直接乘在 compaction 事件数与内存地板上，默认 1–2 个。
5. **它只承诺单行**：单行（含多 CF）原子 + 单行 CAS；跨行一致性永远在应用层，HBase 2.x 也没有通用事务。

---

## 18.13 自检题（5 题）

**Q1.** 一张表 RowKey = `yyyyMMddHHmmss-deviceId`，18 台 RS，写入 9 万 QPS、每行 320 B。给出「单 Region 的写带宽」与「集群可承载写带宽」两个数，并解释为什么扩到 40 台仍然 100% CPU。

<details><summary>答案</summary>

逻辑写带宽 = 90,000 × 320 B = **28.8 MB/s**，全部落在字典序最大的那一个 Region。单 RS 可持续逻辑写上限的量级是 **30–60 MB/s**（盘可持续写 ÷ 2 ÷ 写放大 8–10），所以稳态就已经贴着上限跑；峰值 27 万 QPS = 86.4 MB/s 时是上限的 1.5–2.9 倍。

集群可承载写 ≈ 单 Region 上限 × **有写入的 Region 数**。单调键时后者恒为 1，所以集群上限 = 一台机器（约 45 MB/s），而不是 40 × 45 = 1.8 GB/s。

扩到 40 台无效的原因：扩容增加的是 Region 的**容器数量**，不是有写入的 Region 数量。新 RS 上被均衡过去的 Region 都是老的、零写入的键区间。顺带一个可算的副作用：10 GB ÷ 28.8 MB/s ≈ 356 s 切一次，半年约 4.3 万个零写入的「死 Region」，它们照样占内存结构与 HFile 句柄。

量级结论，非承诺值；单机上限定在 30–60 MB/s，换盘/换副本策略请重算。
</details>

**Q2.** 把上题的表改成加盐 16 桶（`bucket = hash(deviceId) % 16`，RowKey = `bucket-ts-deviceId`）。① 写带宽变成多少？② 「查今天 10:00–10:05 全部设备的事件」要扇出几个 Region、读放大多少？③ 若改成 `bucket` 紧跟时间戳（`bucket-ts-deviceId` 已经是这样），情况有变化吗？

<details><summary>答案</summary>

① 28.8 ÷ 16 = **1.8 MB/s/Region**，远低于单机上限，写侧解决。

② 扇出 **16** 个 Region。读放大取决于桶内是否有序：本例 RowKey 是 `bucket-ts-deviceId`，**时间戳紧跟桶号，所以每个桶内时间区间是连续的**——16 个并发 Scan，每个读目标时间窗的 1/16，总读字节 = 目标数据量，**没有读放大**，但 RPC 数 ×16，延迟 ≈ 单桶 scan + RTT（约 +3 ms）。

③ 会变糟的情形是把原键写成 `deviceId-ts`（时间在尾部）：则桶内按 deviceId 排，时间范围不连续，**16 个桶都必须全桶扫 = 读放大 16×**——取 5 分钟数据要读全表。

结论：**加盐的位置必须和「原键里谁有序」对齐**。加盐本身不产生读代价，「加盐破坏了你赖以定位的那个顺序」才产生读代价。
</details>

**Q3.** 一个 Region 有 12 个 storefile，bloom 误判率 1%。① 无 bloom 与有 bloom 的期望 HFile 读次数各是多少？② 冷读延迟从多少降到多少（2 ms/块）？③ 什么情况下 bloom 一点用都没有？

<details><summary>答案</summary>

① 无 bloom：**12** 次。有 bloom：`1 + (12−1) × 0.01 = 1.11` 次——即 11 个不相交文件里只有 0.11 个会被误判而白读一次索引。

② 12 × 2 ms = **24 ms** → 1.11 × 2 ms + 12 次 bloom 查询（常驻 BlockCache，约 0.12 ms）≈ **2.3 ms**，IO 降 10.8×、延迟降 10.4×。

③ 两种情况：**(a) 范围扫**——范围谓词的键分布在所有文件里，一个都排除不掉；**(b) 更新型负载**——同一个 key 的多个版本散落在多个 HFile 里，bloom 会「正确地」报告 key 存在于每个文件，期望读取次数回到版本数。所以「高频更新同一行」的读放大在 HBase 上是 bloom 治不了的，只能靠 `maxVersions` 与 compaction 节奏控制。
</details>

**Q4.** 一张表开了 8 个 CF。① 用「一次 flush 产出几个文件」解释为什么 compaction 队列会长期 400+；② 给出「该开几个 CF」的三条判据；③ 如果这 8 个 CF 每次查询都要一起读，问题会被放大还是缩小？

<details><summary>答案</summary>

① region 级 MemStore 到刷写阈值（128 MB 量级）时，**每个 Store（= 每个 CF）各写一个 HFile**：1 CF 出 1 个 128 MB 文件，8 CF 出 8 个 16 MB 文件。于是：HFile 产生速率 ×8；达 `hbase.hstore.blockingStoreFiles`（默认 16 量级）所需 flush 次数从 16 次变成 2 次；compaction 事件数 ×8 而每次合并字节数 ÷8，**固定开销（打开/关闭 HDFS 文件、重写 bloom 与块索引、读元数据）不随文件变小而减少**，有效吞吐掉 5–8 倍 → 队列越积越长直到 permanent backlog。

② 三条判据：**读取模式是否真的不同**（只在总是只读一小部分列时才拆）、**生命周期是否不同**（TTL 按 CF 生效，需要不同保留期才拆）、**大小是否悬殊**（避免小 CF 被大 CF 拖着 flush）。默认 1–2 个，>3 要书面理由。

③ **放大**。一次 Get 会遍历该 Region 的每一个 Store，所以 8 个 CF 意味着单次读的期望文件数 ×8——CF 拆分把「写入侧的小文件问题」和「读取侧的多 Store 问题」同时放大了。这是「CF 当逻辑分组」最贵的两个后果。
</details>

**Q5.** 业务说「HBase 存订单，要支持按 orderId 查、按手机号查、还要订单状态变更与支付流水强一致」。逐条判断能不能只用单行原子，并给出每条的落地方式与代价。

<details><summary>答案</summary>

① **按 orderId 查**：✅ 单行 Get，RowKey = `orderId`（可加桶号前缀分散写，点查仍 1 个 Region）。

② **按手机号查**：❌ HBase 没有二级索引。落地方式：建**索引表** `phone_idx`，RowKey = `手机号`，value = `orderId`，两表由应用双写 + 补偿（或用 Phoenix 一类方案，但那是加了一层）；代价是双写一致性要自己保证，且要防「索引表写成功、主表写失败」。另一条路是旁路一份 ES 做检索，HBase 只承担点查。

③ **状态变更 + 支付流水强一致**：❌ 跨行无事务（两个 RowKey = 两行）。三条落地方式各有代价：(a) 把两者塞进**同一个 RowKey 的不同 CF**，一次 Put 单行原子——代价是单行变大、且「流水」变成不可独立扩展的一列；(b) 单行 CAS 做状态机守卫（`checkAndMutate` 从 PENDING 到 PAID），流水用**幂等 + 追加**写，一致性靠对账兜底——代价是有一致性窗口；(c) 换 NewSQL（TiKV/TiDB）拿到跨行事务（→ comp/19），代价是放弃 HBase 的扩展模型。

一句话：**HBase 的事务边界就是一个 RowKey**。任何「跨两个 RowKey 要原子」的需求，要么被压缩进一个 RowKey，要么在应用层补，要么换零件。
</details>

---

## 18.14 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| LSM 的通用机制：memtable → immutable → L0 → Ln、compaction 写放大 `T×L` 的完整推导、bloom 假阳性与内存换算（本篇只引用，不重写） | [02 存储引擎三形态](02-storage-engine-three-shapes.md) §02.4、§02.6 |
| 最左前缀的形式化：为什么只有「从第一列开始的连续前缀」能定位（本篇把这条搬到 Region 切分上） | [04 索引的本质](04-index-essence-btree-page.md) §04.5 |
| WAL 与崩溃恢复：「先写日志再改内存」为什么是「已提交不丢」的前提，HBase 的 `SYNC_WAL` 与 group commit | → comp/03 |
| 文档库的对照：单文档原子、oplog 重放、跨文档一致性只能应用层补（与 HBase 的「单行原子」同构） | → comp/17 |
| RocksDB 与 TiKV：同样的 LSM + 一层共识（Raft）+ 一个全局调度器（PD），跨行事务为什么在那里成立 | → comp/19 |
| 列存：为什么「按非 RowKey 字段筛选 + 聚合」这类查询该交给 MergeTree 而不是 HBase 全表扫 | → comp/20 |
| 不可变段与 merge：ES 的段与 HBase 的 HFile 是同一个思路，但索引反了过来（倒排 vs 字典序） | → comp/22 |
| 组件故障谱：热点 / compaction 积压 / meta 抖动分别属于「满 / 慢 / 假活」里的哪一类，兜底套路 | → comp/52 |
| 宽列 / KV / 文档 / 关系库的边界表：什么场景根本不该用 HBase | [eng/37 NoSQL 选型](../eng/37-nosql-selection.md) |
| 全书的尺子：解剖卡八问、三本放大账、版本与指标纪律 | [0-summary](0-summary.md) §0.4、§0.7 |
