# 35. Iceberg / 表格式：快照、清单与 COW vs MOR

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 消息、流与定时 ｜ **编号**：35 / 53
> **本篇定位**：第六部分唯一一处把「一堆 Parquet 文件怎么变成一张表」拆到**元数据树的每一层**的篇目。它不讲 Kafka 的日志形态（归 `→ comp/28`）、不讲副本与提交位点（归 `→ comp/29`）、不讲列存引擎自己的 part 与 merge（归 [20 ClickHouse/MergeTree](20-clickhouse-mergetree.md)）、也不讲「湖仓该不该上、分区键怎么选」（那是 `→ architect/21` 与 [eng/35 存储选型](../eng/35-storage-model-selection.md)）——它只回答一个问题：**表格式用「catalog 指针 + 元数据快照」把对象存储上的一堆不可变文件变成了一张可原子提交、可快照隔离、可长期演进的表，而代价是小文件与频繁更新这两笔永远还不完的债**。所有后面的边界（为什么大表读得慢是因为元数据、为什么 MOR 会读放大、为什么 compaction 必须进写入路径、为什么 schema 演进必须靠 field id）都是这一个形态的推论。
> **读法**：§35.1 的三起事故先建立「表没坏，是文件数与 delete 文件在收利息」的直觉；§35.3 的文件层级图与 §35.4 的三种写法图是全篇支点，看懂「metadata.json 是唯一的真相、manifest 是文件的清单、delete 文件是读时的补丁」这三句，后面每一节都只是加证据；§35.8 的三本账是唯一可复算的部分，评审时直接拿乘法链说话；只想看结论的读 §35.12。

---

## 35.1 引子：三起「数据湖看着像表，用起来像文件堆」的事故

Iceberg 类事故的共同形状是：**表能 `SELECT`、`DESCRIBE` 里有列、Spark 作业是 SUCCESS、S3 账单也没爆**。它表现成「看板从 8 秒变成 7 分钟」「某个字段全是 NULL 但没人报错」「同一张表昨天还快今天慢 5 倍」，而根因往往埋在三个月前的一个 commit 频率、一次 `ALTER TABLE`、一个默认的 `copy-on-write` 开关里。

### 事故一：1 分钟一次的流式提交，把 3,100 万条 manifest 攒成 2.1 GB 的目录，`plan files` 吃掉 6 分 40 秒（2026-02-11）

某 SaaS 埋点湖：Iceberg on S3，`format-version=2`，按 `days(created_at)` + `hours(created_at)` 分区（日 24 个分区），保留 90 天，日写 500 GB、压缩后约 110 GB。上游 Flink 作业按 checkpoint 60 秒一次提交（`→ comp/34` 的 checkpoint 语义）。

这个组合在日 5 亿条时看不出任何问题。乘法链是：**每天 1,440 次提交 × 平均每次 24 个新文件 = 3.46 万文件/天**；90 天累计 ≈ **311 万个文件**，平均 2.2 MB（压缩后 6.8 TB）。活跃分区 = 90 天 × 24 = 2,160 个，摊到每个分区约 1,440 个文件。

| 时间 | 事件 | 文件数 | manifest 数 | manifest list 大小 | `plan files` |
| --- | --- | --- | --- | --- | --- |
| 2025-11 上线 | 60 秒 commit，`commit.manifest-merge.enabled` 默认开 | 34 万 | 4,100 | 2 MB | 1.8 s |
| 2026-01-05 | 接入第 4 个 App，写入分区数翻倍 | 156 万 | 26,000 | 13 MB | 12 s |
| 02-08 | 有同学为「让 `ls` 好看」，把 `commit.manifest.min-count-to-merge` 从默认量级调到 10,000 | 291 万 | 291 万（几乎不合并） | **1.5 GB** | 4 分 10 秒 |
| 02-11 09:20 | 早高峰，一条「最近 7 天 DAU」看板查询进入规划阶段 | 311 万 | 311 万 | **2.1 GB** | **6 分 40 秒** |
| 02-11 09:26 | Trino 上 12 个其它看板进入 pending，同一集群连带超时 | — | — | — | — |
| 02-11 10:05 | 定位：`plan files` 阶段占查询时间 94%，数据只读了 0.6 GB | — | — | — | — |
| 02-11 ~ 02-14 | 分 3 批 `rewrite_manifests` + 把 `min-count-to-merge` 调回默认量级 | 311 万 | 1,900 | 0.9 MB | 3.2 s |
| 复盘 | 看板不可用 4 小时 10 分；3 个对客 SLA 违约，赔付与人力约 **58 万元** | — | — | — | — |

**教训不是「有人改错了参数」，而是「元数据的大小与数据量无关，只与提交次数 × 分区数有关」。** 6.8 TB 的表，元数据被攒到 2.1 GB —— 这个 0.03% 的比值从来不在容量评审的校验项里，但它决定了「这张表还能不能被规划」。

### 事故二：`ALTER TABLE` 加列后旧数据读出 NULL，DROP 再 ADD 同名把指标永久打断（2026-03-03 ~ 03-09）

某电商的订单明细湖表 `lake.dw_order`，`format-version=2`，早期为了兼容一个老 Hive 外表（`STORED BY 'org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat'`），下游有 6 个报表作业**绕过 Iceberg 直接读 `data/` 目录下的 Parquet**。

| 时间 | 事件 | 现象 |
| --- | --- | --- |
| 03-03 14:10 | DBA 执行 `ALTER TABLE lake.dw_order ADD COLUMN channel STRING AFTER status`（Iceberg 侧合法） | 新数据多一列；Iceberg 按 field id 解析，读写正常 |
| 03-03 22:00 | 次日 T+1 报表出来，`GMV` 列数值变成 `status` 的字符串，全部转 NULL | GMV 环比 **−99.8%** |
| 03-04 09:30 | 值班判断「是新加的列把位置顶乱了」，要求回滚 | 但 `DROP COLUMN` 不支持回滚数据，只能改 schema |
| 03-04 11:20 | 执行 `ALTER TABLE ... DROP COLUMN channel` + 再 `ADD COLUMN channel STRING`（同名） | 得到的是**新的 field id**；历史分区里该字段全为 NULL |
| 03-04 ~ 03-09 | 投放系统按「渠道归因」报表加投，6 天多花 **194 万元**，渠道 ROI 实际为负 | 缺口直到 03-09 才发现 |
| 03-09 | 用 `SELECT * FROM lake.dw_order.schema` 与 `files` 表核对 field id | 确认两次事故都是「名字对、id 不对」 |
| 复盘 | 修法：把 6 个报表作业改成走 Iceberg 表 API；冻结 `data/` 目录直读权限 | — |

**这两次是同一条物理事实的两次出现：`field id` 才是列的身份，列名只是别名。** 任何「按列名或按列位置读文件」的路径，在表结构演进时都会静默地把数据搬到错的列里；而 `DROP` + `ADD` 同名会拿到新 id，旧数据不是「读不出」，是「读出来永远是 NULL」——**没有一个报错，只有一个安静变小的数字**。

### 事故三：MOR 的 delete 文件三个月没人合并，同一条查询读放大 5.2 倍（2026-04-20）

某交易中台的订单宽表 `lake.order_wide`，`format-version=2`，`write.update.mode=merge-on-read`（团队原话：「COW 每次都重写整个分区，太贵」）。CDC 每 5 分钟一批 upsert（`→ comp/33`），每批写 equality delete 文件。

| 时间 | 事件 | delete 文件数 | delete 字节 | 一次 7 天查询的物理读 |
| --- | --- | --- | --- | --- |
| 2026-01-18 切 MOR | 288 次提交/天 × 7 个受影响分区 | 2,016 | 5.0 GB | 521 GB / 3 分 10 秒 |
| 02-20 | 一个月后，无人跑 `rewrite_position_delete_files` | 6.0 万 | 150 GB | 940 GB / 5 分 40 秒 |
| 03-25 | 有人把 `write.delete.mode` 也改成 MOR，position delete 一并开始堆 | 24 万 | 430 GB | 1.7 TB / 10 分 50 秒 |
| 04-20 | 大促复盘要「最近 7 天订单金额按小时分布」 | **41 万** | **740 GB** | **2.5 TB / 16 分 30 秒** |
| 04-20 | 加 3 台 Trino worker 后仍然 14 分；`physicalInputBytes / processedInputBytes = 5.2` | — | — | — |
| 04-22 | `rewrite_position_delete_files` + 热点分区改回 COW + 把 `delete-file-threshold` 纳入日常作业 | 4 万 | 96 GB | 560 GB / 3 分 30 秒 |
| 复盘 | 大促期间看板延迟 6 天，两次错误补货决策，约 **120 万元** | — | — | — |

**MOR 不是「免费的更新」，它是「把写放大换成读放大」，并且那笔账默认是复利计息的**——只要没人按期还（合并 delete 文件），读路径的成本会单调上涨，直到加机器也压不住。三起事故分别对应本篇三节：**元数据与 manifest（§35.3、§35.8 账一）**、**schema 演进与 field id（§35.7）**、**COW vs MOR 与 delete 文件（§35.4、§35.8 账三）**。

---

## 35.2 一句话定位与唯一真问题

**定位**：Iceberg 表 = **「catalog 里的一个指针 + 一棵元数据树 + 一堆不可变数据文件 + 一串快照」**。表不是目录，目录只是它的落地位置；表是一份被版本化的**文件清单**。三个"不是"：不是数据库（没有行锁、没有跨表事务、没有二级索引）、不是文件格式（它就是 Parquet/ORC/Avro 文件的组织方式，加一层元数据）、不是计算引擎（它只规定"哪些文件属于这个版本的这张表"，谁来算、怎么算归 Trino/Spark/Flink）。

### 唯一真问题

> **数据湖从「一堆文件」变成「一张表」，靠的不是文件格式，而是「元数据快照」：一次提交 = 一次元数据指针的原子切换，于是原子性、快照隔离、schema 演进三件事同时成立。而同一个设计把两笔债写进了 DNA——① 文件数 × 每个文件的元数据条目，② 更新必须表达的删除文件——小文件与频繁更新因此是表格式永远在还、永远还不完的债。**

三个直接推论，全篇反复用：

1. **提交是 O(1) 的，读是 O(文件数) 的。** 写一个文件只多一条 manifest entry，所以写入便宜得不像话；但任何一次查询都要先把「候选文件清单」走一遍，所以**读的固定成本与文件数成正比，与你要读多少字节无关**。
2. **不可变意味着「改」只能被表达成「加」。** 改一行 = 加一个 delete 记录 + 加一个新版本行；删一行 = 加一个 delete 记录。于是「更新有多频繁」直接变成「读路径上要套多少层补丁」。
3. **元数据可演进的前提是「身份与名字分离」。** 列的身份是 `field id`，分区的身份是 `spec id`，快照的身份是 `snapshot id`——三者一旦混用名字/位置/时间做身份，演进当天就会静默错位（事故二）。

**它明确不承诺什么**：不承诺「写完立刻删盘」（物理删除要等快照过期）、不承诺「点查毫秒级」（没有索引，只有分区与统计裁剪）、不承诺「更新便宜且读也便宜」（COW 与 MOR 必须选一个挨打）、不承诺「schema 随便改」（能改，但你的下游必须也按 field id 读）。

### 表格式要解决的三件事，与它为此付的钱

| 要解决的问题 | 没有表格式时（裸 Parquet + Hive 分区） | Iceberg 的答案 | 代价落在哪 | 在哪节 |
| --- | --- | --- | --- | --- |
| **原子提交**（一次作业要么全可见要么全不可见） | 靠「最后写 `_SUCCESS` 文件」或改分区目录，S3 上没有 rename，读到半个分区是常态 | 元数据指针原子切换；文件先写完，再一次性挂进清单 | 需要一个支持原子的 catalog；并发提交要重试 | §35.7 |
| **快照隔离**（读到的是一个一致的文件集合） | 同一查询的两个 task 可能看到不同版本的目录 | 每个 snapshot 是一份完整的文件清单；查询开始时冻结 `snapshot-id` | 旧快照与旧文件在过期前一直占盘（空间放大） | §35.5 |
| **元数据可演进**（加列、改名、改分区不重写数据） | 加列要重写全表；改分区要重建表 | field id / spec id / sort order id 三层身份；旧文件按自己的 id 解释 | 下游必须理解 id；按名字读的引擎会静默错位 | §35.7 |
| **顺带买到的**：时间旅行、增量读、行级删改 | 都没有 | 快照链天然是版本；delete 文件表达行级变更 | 小文件与 delete 文件的两笔债 | §35.4、§35.6 |

### 解剖卡八问：本篇答哪几问

| 问 | 本篇的答案 |
| --- | --- |
| ① 它承诺什么 | 承诺：**原子提交 + 快照隔离 + 元数据演进**，以及「同一份数据可被多引擎共享」。**不承诺**：点查延迟、行级并发写、跨表事务、更新后立刻省盘 |
| ② 数据长什么样 | 五层树：catalog 指针 → `metadata.json` → manifest list → manifest files → data / delete files（§35.3） |
| ③ 一次读写走哪条路 | 写：写文件 → 写 manifest → 提交新 metadata（O(1) 元数据 + O(变更) 数据）；读：取快照 → 裁 manifest → 裁文件 → 读行组 → 应用 delete 文件（§35.4、§35.5） |
| ④ 并发与一致 | 乐观并发：提交时用「读到的 base 版本」做校验，冲突抛 `CommitFailedException` 由上层重试；**没有锁、没有 MVCC 行版本**（§35.7） |
| ⑤ 扩展维度 | **四选一里的「批处理摊薄」是主答案**：表格式横向扩的是「文件并行度」（一个查询拆成 N 个 `FileScanTask`），不是单文件速度。分片 ✅（按分区/文件切）、批处理摊薄 ✅（大文件 + 大 row group）、副本 ✗（对象存储自己管）、垂直切 ✗（列裁剪是引擎的，不是表格式的） |
| ⑥ 系统级失效（只写系统级） | ① **元数据爆炸 → 全表查不动**（manifest list 变成 GB 级单对象）；② **并发提交互相覆盖 → 数据不一致**（catalog 不支持原子时）；③ **成本失控**（文件数与请求数相乘的账单，§35.8 账二） |
| ⑦ 看哪几个指标 | snapshot 数、manifest 数与单 manifest 大小、文件大小分布（avg/p50）、delete 文件占比、`plan files` 占查询时间比（§35.11） |
| ⑧ 换掉它的信号 | 需要毫秒点查、需要行级并发更新、单表 < 数百 GB 且只有一个引擎 → 直接上数据库/列存（§35.9） |

---

## 35.3 物理形态：五层树、快照与三种 id

### 一张表在对象存储上的五层结构

```text
                       catalog（HMS / Glue / Nessie / REST / JDBC / Hadoop 文件式）
                                    │  只有一件东西：metadata_location 指针
                                    ▼
  metadata/00042-6f3a….metadata.json  ◄── 这一次提交的全部真相（量级 200 KB–几 MB）
    ├ format-version: 2             last-sequence-number: 8,734,112
    ├ schemas[]      ← 每一版 schema 一行，列带 field id（id 永不复用）
    ├ partition-specs[] ← 每一版分区规则一个 spec id，字段带 source-id
    ├ sort-orders[]  ← 每一版排序规则一个 order id
    ├ properties{}   ← 表级开关（target file size、metrics 截断、delete mode）
    ├ refs{ main → 8,734,110 }      ← 分支/标签都在这（Nessie 类 catalog 用它做多分支）
    └ snapshots[]    ← 每个 snapshot 一行：id / parent / sequence-number / timestamp / summary
                                    │
      snap-8734110-1-xxxx.avro ◄────┘  manifest list（这个快照的「目录的目录」）
        ├ manifest_path  manifest_length  partition_spec_id
        ├ content（0=数据 / 1=position-delete / 2=equality-delete）
        ├ sequence_number  min_sequence_number  added_snapshot_id
        ├ added/existing/deleted_files_count  added/existing/deleted_rows_count
        └ partitions[ {contains_null, lower_bound, upper_bound} × 分区字段 ]  ← 不打开 manifest 就能裁
                  │
   ┌──────────────┴───────────────┬──────────────────────┐
   ▼                              ▼                      ▼
 8a3f-m0.avro                  9b21-m0.avro           c7d0-m1.avro      manifest files
   ├ entry.status（EXISTING / ADDED / DELETED）
   ├ entry.snapshot_id  entry.sequence_number（数据序列号）  file_sequence_number
   └ entry.data_file:
       ├ content  file_path  file_format  partition  record_count  file_size_in_bytes
       ├ lower_bounds{} upper_bounds{} null_value_counts{}   ← **按 field id 存，不按列名存**
       └ split_offsets[]   ← 让 512 MB 的一个文件能被切成多个 split 并行读
                  │
                  ▼
  data/created_at_day=2026-05-01/created_at_hour=13/00000-3-xxxx.parquet   ← 不可变，永不原地改
  data/…/00001-…-pos-delete.parquet      ← 「第几行被删了」
  data/…/00002-…-eq-delete.parquet       ← 「哪些 key 被删了」
```

**这张图要读出的四件事：**

1. **`metadata.json` 是唯一真相。** 文件在 S3 上先写完，最后只有一步是"生效"——把新 `metadata.json` 的路径写进 catalog。所以「半个提交」在表这一层不存在。
2. **manifest list 与 manifest 是两级剪枝。** 第一级用**分区摘要**（在 manifest list 里，不需要打开 manifest），第二级用**列统计**（在 manifest 里，需要打开）。这个分工决定了元数据读放大的第一层成本。
3. **统计按 `field id` 存。** 改名不影响统计；`DROP` 再 `ADD` 同名会拿到新 id，旧文件的统计与新列对不上——这就是事故二的技术根因。
4. **delete 文件和 data 文件是同类对象**（都是 manifest entry，只是 `content` 不同）。共用同一套快照与裁剪机制——**这是 MOR 能成立的全部原因，也是它读放大的全部原因**。

### 五层的量级与各层关心什么

| 层 | 对象 | 格式 | 一条记录多大 | 数量级（一张 10 TB 表） | 它回答什么问题 |
| --- | --- | --- | --- | --- | --- |
| 0 | catalog 指针 | 各 catalog 自己的存储 | 一个路径字符串 | 1 | 现在生效的是哪份元数据 |
| 1 | `metadata.json` | JSON | 一版 schema + 全部 snapshot 各一行 | 1 个（200 KB–几 MB） | 有哪几条历史、当前哪个快照、schema/spec 演进史 |
| 2 | manifest list | Avro（通常压缩） | 每 manifest 一条，0.3–1 KB 量级 | 几十到几万 | 这个快照由哪些 manifest 组成 |
| 3 | manifest | Avro | 每文件一条，100–250 B 量级 | 几十到几百万 | 哪些文件属于这个快照、每个文件的统计与删除状态 |
| 4 | data / delete file | Parquet / ORC / Avro | 128 MB–512 MB（delete 文件小得多） | 几万到几百万 | 真正的数据 |

> **量级纪律**：manifest entry 的字节数取决于列数与统计截断策略，**不是固定常量**。100–250 B 是区间，中央值取 150 B 只用于算账；`write.metadata.metrics.default=truncate(16)` 时每列统计约 32 B 上下，列数到 100 时 entry 会明显变大。**要精确值就在目标环境上取 `manifests` 元数据表的 `length` 除以 `added_files_count`。**

### snapshot、sequence number、spec id、sort order：四种身份

| 身份 | 定义在哪 | 谁在用它 | 混用它会怎样 |
| --- | --- | --- | --- |
| **`snapshot-id`** | `metadata.json` 的 `snapshots[]`，被 `refs` 指向 | 快照隔离、时间旅行、增量读的起点 | 拿「时间戳」当快照（`TIMESTAMP AS OF` 是查最近一个 ≤ 该时间的快照），跨时区/乱序提交时会取到意外的版本 |
| **`sequence-number`** | `metadata.json` 的 `last-sequence-number`，每个 snapshot / 每个 manifest / 每个 entry 各有 | **决定 delete 文件作用到哪些数据文件** | 手工拼 delete 文件却不设序列号 → delete 要么不生效（读到脏数据），要么把未来写入的行一起删掉 |
| **`spec id`** | `partition-specs[]`，manifest 上记 `partition_spec_id` | 分区演进：旧文件按旧 spec 解释 | 假设"全表一个分区规则" → 演进后统计与裁剪全错，且**不报错**，只是少读或多读 |
| **`sort order id`** | `sort-orders[]`，文件上记 `sort_order_id` | 说明"这个文件内部是否有序、按什么序"，供引擎决定要不要再排序 | 以为表级 sort order 对历史文件也成立 → 依赖排序的 Z-order/聚簇假设在旧文件上失效 |

**`sequence number` 是这四个里最容易被忽略、后果最重的**。它的规则（v2 规范）：

- **position delete** 作用于「路径匹配、且数据文件的序列号 **≤** 删除文件序列号」的数据文件；
- **equality delete** 作用于「分区相同、且数据文件的序列号 **严格小于** 删除文件序列号」的数据文件。

那条"严格小于"就是 MERGE 一次写入里"新写入的行会不会被同批 delete 一起删掉"的分界线。**规范细节与边界情形以目标版本文档为准**，但工程含义不变：**delete 文件必须记得住「我是在哪一版之后生效的」**，否则读出来的就是错的数。

### 列统计：能裁掉多少，取决于截断

| 统计 | 存在哪 | 默认/前提 | 拿它裁什么 | 它裁不掉什么 |
| --- | --- | --- | --- | --- |
| `lower_bounds` / `upper_bounds` | manifest entry 的 `data_file` 内，按 field id | `write.metadata.metrics.default` 默认 `truncate(16)` 量级，**以目标版本配置为准** | 范围谓词（`BETWEEN`/`>`/`=`）的文件级裁剪 | 截断后前缀相同的长字符串（UUID、URL）几乎裁不掉 |
| `null_value_counts` | 同上 | 同左 | `IS NULL` / `IS NOT NULL` | — |
| `value_counts` | 同上 | 同左 | 判断列是否全空、估算选择率 | — |
| `nan_value_counts` | 同上 | 同左，浮点列 | `NaN` 相关谓词 | — |
| `column_sizes` | 同上 | 同左 | 估算读这一列要多少 IO | — |
| `split_offsets` | 同上 | Parquet/ORC 写时记录 | 把一个文件切成多 split 并行读 | 对行式小文件无效 |

> **一条反直觉结论**：**统计截断是有意为之的**。不截断的话，一个长字符串列的 `lower_bounds`/`upper_bounds` 会让每条 manifest entry 膨胀到几 KB，元数据反而先爆。所以「为什么我的 UUID 列裁剪效果差」的答案不在引擎，在于**统计被截断到了前缀**——想靠它裁，就得让前缀有区分度（例如把 `uuid` 换成按时间前缀可比的 ID，`→ comp/45`）。

---

## 35.4 写路径：append、overwrite、delete，以及 COW 与 MOR

### 三种写法的物理动作

```text
【① append】新增数据：写 1 个新 data file
   data      : +f4.parquet（写完才提交）
   manifest  : 新增 1 条 status=ADDED 的 entry（旧 manifest 不动，或与新 entry 合并成新 manifest）
   snapshot  : 新 manifest list = 旧 list − 被替换的 manifest + 新 manifest
   代价      : 写 O(新增字节)；元数据 O(1)；旧文件一个字节都不动   ⇒ 这是表格式最便宜的动作

【② overwrite 某分区】逻辑替换 dt=2026-05-01
   data      : +3 个新文件（该分区被全量重写）
   manifest  : 旧 3 条标 DELETED、新 3 条标 ADDED —— **旧文件仍在盘上**
   snapshot  : 新快照里这 6 条只有 3 条可见
   代价      : 重写整个分区（写放大 = 分区大小 ÷ 实际变更量）；物理回收要等 expire_snapshots

【③ delete / update → COW（copy-on-write）】
   做        : 找出命中的每个 data file，**整文件重写**（去掉/改掉那些行），再走 ② 的路径
   代价      : 随机分布的少量更新会命中几乎所有文件 → 写放大 50–100×（§35.4 的算账表）

【④ delete / update → MOR（merge-on-read）】
   做        : 只写 delete 文件：
                 position delete → 记 (file_path, pos)，语义是「这个文件的第 N 行不算」
                 equality delete → 记一组 key 列的取值，语义是「同分区、更早的数据文件里，key 命中的行不算」
   代价      : 写极小；但**读时每个 data file 都要套用它适用的 delete 文件** → 读放大（§35.8 账三）
```

### COW 与 MOR 的账：谁在什么时候赢

| 维度 | COW（copy-on-write） | MOR（merge-on-read） |
| --- | --- | --- |
| 一次更新的写量 | 命中的**整个文件**（512 MB 粒度） | 只写 delete 记录（KB–MB 粒度） |
| 一次更新的延迟 | 秒–分钟（受文件大小约束） | 亚秒–秒 |
| 读路径 | 文件即真相，**向量化列扫全速** | 每个文件要套 delete 补丁，**逐行 merge，慢 3–5×** |
| 读放的账记在谁头上 | 写侧（一次付清） | 读侧（**复利，且默认没人还**） |
| 空间放大 | 旧文件等快照过期才回收（通常 0–5 个快照） | delete 文件与数据文件长期并存；不合并就单调增长 |
| 谁承担 IO | 写入作业 | **每一个查询**（尤其是高频看板） |
| 适合 | 批量重算、T+1、低频更新、**读多写少** | 高频 upsert、CDC 落湖、**写多读少**、对写入延迟敏感 |
| 什么时候它一定错 | 每分钟 upsert 一张 10 TB 表 | 报表表天天被扫却从不合并 delete 文件（事故三） |

**把「更新比例」代进去，两种模式的写放大差出三个数量级：**

| 更新形态（表 500 GB，data file 512 MB ≈ 977 个文件） | COW 实际写入 | MOR 实际写入 | 逻辑变更 |
| --- | --- | --- | --- |
| 稀疏更新 5 万行（0.01%，随机分布） | ~495 GB（~99% 文件被命中） | ~5 MB（1 个 delete 文件） | 40 MB |
| 常规更新 1% 的行（随机分布） | ~495 GB | ~5 GB | 5 GB |
| 集中更新一个分区（1% 数据量） | ~5 GB | ~50 MB | 5 GB |
| 全分区批量重算 | ~5 GB（本来就该重写） | 无意义（该用 COW） | 5 GB |

> **量级结论（非承诺值）**。假设：① 文件大小 512 MB、更新行在文件间近似均匀分布；② COW 以文件为最小重写单位；③ MOR 的 delete 记录每行 100–250 B；④ 不计 manifest 重写与快照过期延迟。**结论的乘法链只有一条：`COW 写放大 ≈ 1 ÷ (被命中文件比例)`，稀疏更新时这个比例趋近于「全表」——所以 COW 的写放大与「改了多少行」几乎无关，只与「改得多散」有关。**

### 一次提交要落几份东西，以及它为什么是原子的

| 跳 | 动作 | 落在哪 | 失败会怎样 | 量级 |
| --- | --- | --- | --- | --- |
| ① | 写 data / delete 文件 | 对象存储 | 文件是孤儿（不可见），`remove_orphan_files` 清理 | 与数据量线性；对象存储 PUT 30–80 ms |
| ② | 写 manifest（含新文件条目） | 对象存储 | 同上，不可见 | KB 级，1 次 PUT |
| ③ | 写 manifest list（引用 ① 的 manifest） | 对象存储 | 同上 | 几十 KB–几 MB，1 次 PUT |
| ④ | 写新 `metadata.json`（新 snapshot 指向 ③） | 对象存储 | 同上，**这是"还没生效"的最后一步** | 200 KB–几 MB，1 次 PUT |
| ⑤ | **把 catalog 的 `metadata_location` 换成 ④** | catalog（HMS 行 / Glue 表属性 / Nessie commit / REST 服务端） | 这才是唯一一步"生效"，冲突在这里暴露 | 一次事务提交，ms–几十 ms |

**这就是"原子提交"的全部秘密：①–④ 全是可丢弃的写入，只有 ⑤ 是一次指针切换。** 反过来，**如果 catalog 不支持原子切换（例如某些部署下直接用文件系统 `rename` 覆盖，S3 上 `rename` 是 copy+delete），那 ⑤ 就退化成"两次操作"，两次写入端会互相覆盖而且不报错**——这是表格式最贵的一类事故，处理方案见 §35.7。

```sql
-- 同一个「更新一部分行」，在 COW 与 MOR 下是两种完全不同的物理动作
ALTER TABLE lake.orders SET TBLPROPERTIES ('write.delete.mode' = 'copy-on-write');  -- 默认档
ALTER TABLE lake.orders SET TBLPROPERTIES ('write.update.mode' = 'copy-on-write');
ALTER TABLE lake.orders SET TBLPROPERTIES ('write.merge.mode'  = 'copy-on-write');

-- 高频 upsert 的写法：只让「写」变便宜，代价必须由合并作业来还（§35.6）
ALTER TABLE lake.orders SET TBLPROPERTIES (
  'write.delete.mode' = 'merge-on-read',
  'write.update.mode' = 'merge-on-read',
  'write.merge.mode'  = 'merge-on-read'
);

UPDATE lake.orders SET status = 'SHIPPED'
 WHERE created_at >= TIMESTAMP '2026-05-07 00:00:00' AND status = 'PAID';
-- COW：把命中的每个 512 MB 文件整体重写一遍（写放大 ≈ 1 ÷ 命中文件比例）
-- MOR：写 1 个 position delete 文件（约 (路径8B+pos8B)×行数）＋ 1 个新 data 文件，老文件原地不动
SELECT count(*) FROM lake.orders.files WHERE content = 1;   -- MOR 欠下的「债」一眼可查
```

---

## 35.5 读路径：五级裁剪与快照隔离

### 一次查询要碰几层

```text
查询：SELECT sum(pay_amount) FROM lake.orders
      WHERE created_at >= '2026-05-01' AND created_at < '2026-05-08'

【级 1】catalog 取指针            1 次 GET  ~30 ms      → metadata.json（200 KB–几 MB）
        └ 取 current-snapshot-id（或 AS OF 指定的那个）→ **文件集合从此冻结**

【级 2】读 manifest list          1 次 GET  ~30 ms–21 s  ← 大小差 700 倍，就看它多大
        └ 用 partitions 里的分区摘要裁：10 TB / 90 天的表 → 79 个 manifest 里留 12 个

【级 3】读 manifest files         12 次 GET  ~0.4 s
        └ 用 lower_bounds/upper_bounds 裁：53 万条 entry → 9.4 万条

【级 4】打开 data files           9.4 万次 GET（并行 256）≈ 11 s
        └ 读 footer + row group 统计，再裁一层 → 真正读 7,800 个文件

【级 5】读 row group / 行         列裁剪 + 谓词下推后读 420 GB

        级 2–4 全是**元数据**，一行数据都还没读。
        它们的耗时只与「文件数 / manifest 数」有关，与「要读多少字节」无关。
```

| 级 | 用什么裁 | 能裁掉多少（典型） | 单位成本 | 它崩掉的样子 |
| --- | --- | --- | --- | --- |
| 1 | `snapshot-id` | 裁掉其他所有版本 | 1 次 GET | 快照数上万 → `metadata.json` 本身变成几十 MB，**每次查询都先解析它** |
| 2 | manifest list 里的分区摘要（`contains_null`/`lower_bound`/`upper_bound`） | 按分区数比例（90 → 7 天 ≈ 92%） | 每 manifest 0.3–1 KB | manifest 不合并 → 单对象 GB 级，一次顺序读 21 s（事故一） |
| 3 | manifest entry 里的列统计 | 按统计选择率（本例 82%） | 每 entry 100–250 B + 解析 1–2 μs | 统计被截断（长字符串/UUID 前缀相同）→ 几乎裁不掉 |
| 4 | 文件 footer / row group 统计 | 再裁 80–90% | 每文件一次 GET ≈ 30 ms | 小文件多 → 这一级变成主成本（§35.8 账二） |
| 5 | 列裁剪 + 向量化 | 按列数与谓词 | 带宽线性 | 投影列多 → 退化回行扫 |

### 快照隔离：为什么"读到的是一个一致的集合"

一个 `snapshot` 就是**一份完整的文件清单**（不是 diff）。所以：

- **查询在级 1 冻结 `snapshot-id` 之后，后面看到的所有文件中一定都是那个版本的全集**，即使此刻有人在写——新文件属于新快照，旧快照的清单里没有它们。
- **不需要锁、不需要读已提交/可重复读的概念**。这是表格式最优雅的一点：**"隔离"来自不可变清单，不来自锁**。
- **代价在写侧之外**：旧快照的清单与文件必须留着才能提供隔离，于是「保留多少个快照」直接等于「空间放大多少」。`expire_snapshots` 就是这笔账的收割机。

| 场景 | 快照隔离给出什么 | 你需要额外做什么 |
| --- | --- | --- |
| 长跑批作业（2 小时）期间有人持续写 | 作业看到的是启动时的静好版本，结果自洽 | 什么都不用做；但要知道「结果不是最新」 |
| 两个作业同时 `overwrite` 同一分区 | 后提交者会基于旧 base 检测到冲突 | 上层重试，或改用分区级互斥（`→ comp/46`） |
| 时间旅行对比「昨天 vs 今天」 | `VERSION AS OF` 两个快照，同一条 SQL 里 join | 确认两个快照都还没被过期 |
| 增量读（只读新增的文件） | 用快照区间做「增量扫描」 | 需要引擎支持（Spark 的 `start-snapshot-id`/`end-snapshot-id`） |

```java
// 读路径骨架：只做两件事——冻结快照、裁剪。打印出来的数字就是"这次查询要碰多少东西"
Table table = catalog.loadTable(TableIdentifier.of("lake", "orders"));

Snapshot snap = table.currentSnapshot();          // 时间旅行就换成 table.snapshot(id) / snapshotAsOfTime(ts)
TableScan plan = table.newScan()
    .useSnapshot(snap.snapshotId())               // ① 冻结文件集合 = 快照隔离
    .filter(Expressions.and(                       // ② 残余谓词，交给 manifest/文件两级统计裁
        Expressions.greaterThanOrEqual("created_at", "2026-05-01T00:00:00"),
        Expressions.lessThan("created_at", "2026-05-08T00:00:00")));

long files = 0, bytes = 0, delFiles = 0;
try (CloseableIterable<FileScanTask> tasks = plan.planFiles()) {   // 这一步一行数据都不读
  for (FileScanTask t : tasks) {
    files++;  bytes += t.file().fileSizeInBytes();
    delFiles += t.deletes().size();               // ③ MOR 的读放大就长在这个数上
  }
}
System.out.printf("data files=%d  data=%.1f GB  applicable delete files=%d  ratio=%.2f%n",
    files, bytes / 1e9, delFiles, delFiles * 1.0 / files);
// 生产上把这三个数打进查询日志：plan 阶段的文件数与 delete 数，是"表变慢"的第一证据
```

### 为什么"读得慢"常常是元数据太大，而不是数据太大

把事故一现场的三个数字摆在一起就能看清：

| 量 | 数值 | 说明 |
| --- | --- | --- |
| 表的数据量 | 6.8 TB | 与"慢"几乎无关 |
| 本次查询真正读的数据 | 0.6 GB | 有分区裁剪，很健康 |
| 本次查询要读的元数据 | **2.1 GB**（manifest list 单对象）+ 311 万个 manifest | 这才是 6 分 40 秒的来源 |
| 元数据 / 数据比 | 0.03% | 看着可以忽略，**但它是一个"每次查询都要付"的固定成本** |

**判据（可当值班口径）**：`plan files` 阶段耗时 ÷ 查询总耗时 > 20%，或 `planFiles` 次数 ÷ 实际读文件数 > 10，**就不是数据的问题，是元数据的问题**——去查 manifest 数与文件数，不要去加 worker。

---

## 35.6 小文件与 compaction：一笔与业务量无关的债

### 小文件从哪来：没有一个意外

| 来源 | 典型文件大小 | 产生速率 | 机制 |
| --- | --- | --- | --- |
| **流式写入按 checkpoint 提交** | 1–8 MB | 1,440 个/分区/天（60 秒一次） | 每次提交必须落一次新文件才原子 —— **主犯** |
| 微批 upsert（CDC 落湖） | < 1 MB | 数百/天 | 每批一个 data 文件 + 一个 delete 文件 |
| 迟到数据、回刷重跑 | < 8 MB | 不定 | append-only 形态下，重算 = 再写一份 |
| 分区过细 | ×分区数 | — | 分区是乘数，不是来源（`→ architect/21` §21.5） |
| writer 并行度过高 | 目标大小 ÷ 并行度 | — | 32 个写任务各写各的，单文件 = 目标 ÷ 32 |

**关键一条：小文件的产生速率与业务量无关，与「提交频率 × 活跃分区数 × 写并行度」成正比。** 所以业务量腰斩，小文件一个不少——这是成本表里唯一一个"降量不降本"的项。

### target file size：一个旋钮决定后面所有账

| 旋钮 | 典型值（以目标版本配置为准） | 它改的是哪条物理路径 | 调大的收益 | 调大的代价 |
| --- | --- | --- | --- | --- |
| `write.target-file-size-bytes` | 512 MB 量级 | 每个 writer 写多大才切下一个文件 | 文件数↓，元数据↓，打开成本↓ | 单文件重写更贵（COW 放大）；单 split 更长；小查询读一个文件就超量 |
| `write.parquet.row-group-size-bytes` | 128 MB 量级 | 文件内部 row group 的大小 | 压缩比↑、统计更粗但更省 | 谓词裁剪粒度变粗（一个 row group 内的 min/max 跨度大） |
| `commit.manifest.target-size-bytes` | 8 MB 量级 | manifest 打包目标 | manifest 数↓，manifest list↓ | 每次提交要重写更大的 manifest（写放大一点点，几乎可忽略） |
| `commit.manifest.min-count-to-merge` | 100 量级 | 攒多少个 manifest 才合并 | 少写、少读 | **调大 = 让 manifest 不合并 → 事故一的直接成因** |
| `write.metadata.metrics.default` | `truncate(16)` 量级 | 列统计截断长度 | 元数据更小 | 长字符串列裁剪失效 |

**怎么定目标大小**：`目标文件大小 ≈ 单 split 扫描吞吐 × 期望单任务时长`，并与「最小查询」对齐——一个只查 1 小时的看板，读一个 512 MB 文件就有 90% 浪费，所以热层该小（128 MB），冷层该大（512 MB）。这条与 `→ architect/21` §21.5.3 的分层表一致。

### compaction：read 1× + write 1×，必须追得上新增

```text
流式写入制造小文件                    compaction 回收
────────────────────                 ────────────────────────────────────────
60s commit → 2.2 MB ─┐               ① 按大小分级（bin-packing）：把「平均 < 目标/4」的文件分组
                     ├─ 每分区 1,440  ② 每组读出来 → 按目标大小重写 → 原子替换
60s commit → 2.2 MB ─┤   个/天       ③ rewrite_manifests：把碎 manifest 打包
   …                 │                ④ expire_snapshots：回收被替换的旧文件
迟到微批  → 0.4 MB ──┘
                                        代价：读 1× + 写 1×（+ 外部排序，若带 cluster）
   3.1M 文件 / 6.8 TB    ────────────►   4,300 文件 / 6.6 TB（压缩比 3.2× → 5.0×）
```

| 维护动作 | 治什么 | 典型参数（以目标版本文档为准） | 成本 | 不做会怎样 |
| --- | --- | --- | --- | --- |
| `rewrite_data_files`（binpack） | 小文件 | `target-file-size-bytes`、`min-input-files` | 读 1× + 写 1× 被重写的字节 | 元数据膨胀 + 打开成本线性上涨（§35.8 账一、账二） |
| `rewrite_data_files`（含 delete 的文件组） | MOR 的 delete 债 | `delete-file-threshold` | 同上，只挑有 delete 的组 | 读放大复利（§35.8 账三） |
| `rewrite_position_delete_files` | 碎 position delete 文件 | `target-file-size-bytes`、`min-input-files` | 读 1× + 写 1× delete 字节（很小） | 每个 data file 要打开成百上千个 delete 文件 |
| `rewrite_manifests` | 碎 manifest | `target-size-bytes` | 只重写元数据，**很便宜** | manifest list 变成 GB 级单对象，规划阶段爆炸（事故一） |
| `expire_snapshots` | 旧快照与孤儿文件 | `older_than`、`retain_last` | 只删对象，**最便宜的一项** | 空间放大无上限；`metadata.json` 无限变大 |
| `remove_orphan_files` | 提交失败留下的文件 | `older_than`（**必须大于最长作业时长**） | LIST 成本（可能很贵） | 账单每月慢慢涨，没人知道钱花在哪 |

**追赶不等式（必须写进值班手册）**：

```
新增文件速率（个/天） < compaction 吞吐（个/天）
   否则永远追不上，且差距按天复利

例：288 次提交 × 7 个分区 = 2,016 个新文件/天
    一次 rewrite 每小时能重写 20 组、每组 20 个文件 → 9,600 个/天 ⇒ 4.8× 余量（健康）
    若把写入频率提到 30 秒一次 → 4,032 个/天，余量降到 2.4×（开始紧张）
```

```sql
-- 维护四件套（Spark procedures；参数名与默认值以目标版本文档为准）
CALL lake.system.rewrite_data_files(
  table    => 'lake.orders',
  strategy => 'binpack',                                  -- 只做大小合并，不做排序（排序贵得多）
  options  => map(
    'target-file-size-bytes' => '134217728',              -- 流式表别追 512 MB：单 split 太长
    'min-input-files'        => '5',                      -- 少于 5 个不动，避免无谓重写
    'delete-file-threshold'  => '1',                      -- 有 delete 的文件组优先重写（还 MOR 的债）
    'max-concurrent-file-group-rewrites' => '10'
  ));

CALL lake.system.rewrite_position_delete_files(
  table   => 'lake.orders',
  options => map('target-file-size-bytes' => '134217728', 'min-input-files' => '5'));

CALL lake.system.rewrite_manifests(table => 'lake.orders');   -- 把碎 manifest 打包，最便宜的一项

CALL lake.system.expire_snapshots(
  table      => 'lake.orders',
  older_than => TIMESTAMP '2026-05-01 00:00:00',
  retain_last => 10);                                          -- 别设 1：把回滚的窗口留出来
```

---

## 35.7 Schema / partition evolution 与 catalog 的原子提交

### 列的身份：`field id` 与名字分离

| 动作 | 合法吗 | 物理动作 | 旧数据读出来是什么 | 下游风险 |
| --- | --- | --- | --- | --- |
| **加列** `ADD COLUMN` | ✅ | 新 `field id` = `last-column-id + 1`；只改元数据 | `NULL`（历史文件里没有这个 id） | 下游按位置读会错位（事故二）；`NOT NULL` 加不上（历史行是 NULL） |
| **改名** `RENAME COLUMN` | ✅ | 只改 schema 里的名字，**id 不变** | 正常 | 按列名读的下游会找不到列 |
| **改类型** `ALTER COLUMN TYPE` | ⚠️ 有限 | 只允许安全提升（`int→long`、`float→double`、decimal 加宽） | 正常 | 反向读取（新写旧读）会溢出或报错 |
| **删列** `DROP COLUMN` | ✅ | 从 schema 移除该 id；**文件里的字节还在** | 该列不可见，`SELECT` 报列不存在 | 想"找回来"只能靠 `ADD COLUMN` 拿到**新 id** → 历史值永远 NULL |
| **调顺序** `ALTER COLUMN ... AFTER` | ✅ | 只改 schema 的顺序（纯元数据） | 正常（按 id 解析） | **按位置读的下游全错位**——事故二的触发点 |
| **重命名嵌套字段** | ✅ | 同改名 | 正常 | 同上 |
| **`DROP` 后 `ADD` 同名** | ✅（但语义上错） | 得到**新 field id**，与旧数据的 id 无关 | **全部 NULL，且无任何报错** | 报表指标静默归零（事故二第二阶段） |

**纪律**：所有校验、对账、血缘都要**按 field id 做**。`SELECT * FROM t.schema` 是唯一可信的列身份清单。

### 分区演进：hidden partitioning 与 spec id

| 概念 | 含义 | 为什么重要 |
| --- | --- | --- |
| **hidden partitioning** | 分区由 `days(created_at)`、`bucket(16, user_id)` 这类**变换**定义，不需要 `dt=` 伪列出现在查询里 | 查询写 `WHERE created_at > ...` 就能裁剪；不会出现"忘了带分区列导致全表扫" |
| **`spec id`** | 每一版分区规则一个 id；每个 manifest 记住自己属于哪个 spec | 演进后旧文件按旧 spec 解释、新文件按新 spec 写 —— **同一张表同时存在两套分区逻辑是合法的** |
| **transform** | `identity` / `bucket(N,col)` / `truncate(L,col)` / `year`/`month`/`day`/`hour` / `void` | `void` 是"逻辑上退役这个分区字段但保留字段本身"，演进过渡期用 |
| **演进只影响未来** | 加/删分区字段都不会重写历史文件 | 好处：秒级完成。代价：**老分区粒度细、新分区粒度粗，查询裁剪效果不一致**；想统一只能重写（等于 compaction） |

| transform | 适用 | 典型基数（举例） | 副作用 |
| --- | --- | --- | --- |
| `days` / `hours` | 几乎所有事件表的第一段 | 365 / 8,760 每年 | 唯一"只增不减、可整段淘汰"的维度，**永远放第一段** |
| `bucket(N, col)` | 高基数等值过滤（`user_id`、`tenant_id`） | N（写死的） | 只能裁等值，裁不了范围；N 选错只能重写 |
| `truncate(L, col)` | 低基数前缀（ID 前缀、渠道码） | 由数据决定 | 倾斜风险（某前缀占多数） |
| `identity` | 低基数枚举 | 5–20 | 基数稍高就爆炸 |
| `void` | 退役一个分区字段 | — | 只为了演进期兼容 |

### catalog 的原子提交：各家形态不同，行为以目标版本文档为准

| catalog | 指针存在哪 | 原子性靠什么 | 并发写的表现 | 运维上要知道的 |
| --- | --- | --- | --- | --- |
| **Hive Metastore (HMS)** | `TABLE_PARAMS` 里的 `metadata_location` | HMS 的行更新（底层关系库事务）+ 表级锁 | 冲突抛 `CommitFailedException`，上层重试 | 与 Hive 表混用时容易被别人改参数；HMS 本身是单点（`→ architect/11` 的元数据侧） |
| **AWS Glue** | Glue 表属性 | Glue `UpdateTable` 的乐观并发（version 校验） | 冲突重试；**API 有速率限制**，高频提交会撞限流 | 需要在提交路径上做退避；`以目标版本文档为准` |
| **Nessie** | 版本化 commit 图（git-like） | commit 的哈希冲突检测 | 冲突显式报出，可 `merge`/`cherry-pick` | 多分支开发/隔离测试的最大卖点 |
| **REST catalog** | 服务端 | 服务端在 `UpdateTable` 里做 `requirements` 校验后原子落地 | 客户端只发"意图"，冲突由服务端判 | 客户端不需要访问文件系统，最适合多云/多引擎 |
| **JDBC catalog** | 关系库一张表 | 关系库事务 | 强，但吞吐受单库限制 | 适合中小规模；不适合高频流式提交 |
| **Hadoop（文件式）** | `metadata/version-hint.text` | **没有真正的原子性**（S3 上 `rename` 是 copy+delete） | **两个 writer 会互相覆盖且不报错** | 只用于单写者/测试；生产禁用（§35.7 末尾的并发事故形态） |

**并发提交的通用形态**：Iceberg 的提交是**乐观**的——提交时带着「我读到的 base 元数据」的校验条件（当前快照 id、表 uuid、last-column-id 等），catalog 比对通过才落地，不通过就抛冲突。所以：

- **正确姿势**：捕获冲突 → 重新读元数据 → 重新应用变更 → 重试（指数退避）。写文件那部分**不用重做**（文件已经落盘，只是清单没挂上）。
- **错误姿势**：把冲突当异常吞掉（丢数据）；或改成"关掉校验直接覆盖"（互相覆盖，事故形态见上表最后一行）。
- **高频提交的必然代价**：冲突率 ∝ 提交频率 × 并发 writer 数。所以**每 1 秒 commit 一次的 50 个 writer，注定要在重试上烧掉大量时间** —— 这也是"先攒批再提交"的另一个理由（与 `→ comp/28` §28.4 的攒批同源）。

```sql
-- 演进只有四类动作，但每类的物理后果不同（记住：id 是身份，名字是别名）
ALTER TABLE lake.orders ADD COLUMN IF NOT EXISTS channel STRING;   -- 新 field id，旧行读出 NULL
ALTER TABLE lake.orders RENAME COLUMN amount TO pay_amount;        -- 只改元数据，id 不变
ALTER TABLE lake.orders ALTER COLUMN user_id TYPE BIGINT;          -- 仅允许安全提升
ALTER TABLE lake.orders DROP COLUMN channel;                       -- schema 里消失，文件字节仍在

SELECT * FROM lake.orders.schema;        -- ★ 校验列身份看这里：field id 才是 id
-- 反例（会静默打断指标）：DROP 后 ADD 同名 → 新 id，历史数据永远 NULL

-- 分区演进：只影响「之后写的文件」，旧文件仍按自己的 spec id 解释
ALTER TABLE lake.orders ADD PARTITION FIELD hours(created_at);
ALTER TABLE lake.orders DROP PARTITION FIELD bucket(16, user_id);
SELECT spec_id, count(*) FROM lake.orders.manifests GROUP BY 1;   -- 演进期会看到多组 spec_id
```

---

## 35.8 三本账：元数据、打开文件、delete 合并

三个算例，每个都带乘法链、假设清单和「量级结论」标记。账一、账二用事故一的现场（311 万文件 / 6.8 TB / 90 天保留）；账三用事故三的订单宽表（288 次提交/天、7 个受影响分区）。

### 账一 · 元数据大小 = 文件数 × manifest entry（+ manifest 数 × list entry）

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 文件数 | 1,440 次提交/天 × 平均 24 个新文件/次 × 90 天 | 311 万个 |
| 2 | manifest entry 字节 | 150 B/条（区间 100–250 B，随列数与统计截断变化） | 311 万 × 150 B = **467 MB** |
| 3 | 合并后的 manifest 数 | 467 MB ÷ 8 MB（`commit.manifest.target-size-bytes`） | 59 个 |
| 4 | manifest list 字节 | 59 × 0.5 KB（区间 0.3–1 KB） | **30 KB** |
| 5 | 元数据合计（合并） | 467 MB + 30 KB | **≈ 467 MB** |
| 6 | 未合并的 manifest 数 | 每次提交 24 个 manifest（每个被触及的分区 1 个） | **311 万个** |
| 7 | 未合并的 manifest list | 311 万 × 0.5 KB + 311 万条 entry 的路径字段 | **≈ 2.1 GB** |
| 8 | 元数据合计（未合并） | 467 MB + 2.1 GB | **≈ 2.5 GB** |
| 9 | 读一遍清单的 GET 次数 | 合并：59+1；未合并：311 万+1 | 60 次 vs **311 万次** |
| 10 | 顺序读 manifest list 的耗时 | 单对象顺序读，按 100 MB/s 有效吞吐 | 30 KB ≈ 0 ms；**2.1 GB ≈ 21 s** |
| 11 | compaction 后（每分区 2 个 512 MB 文件） | 文件数 4,320 → entry 4,320 × 150 B | **0.65 MB，1 个 manifest，list 0.5 KB** |
| 12 | 元数据 / 数据比 | 合并 467 MB ÷ 6.8 TB；未合并 2.5 GB ÷ 6.8 TB | **0.0069%** vs **0.037%** |

> **量级结论（非承诺值）**。假设清单：① manifest entry 150 B（区间 100–250 B）；② manifest 目标 8 MB；③ manifest list 每条 0.5 KB（区间 0.3–1 KB，随分区字段数增长）；④ 不计 Avro 压缩（默认压缩后实际字节常为估算的 30–60%，**换成压缩后的值结论不变，只是绝对值更小**）；⑤ 单对象顺序读有效吞吐 100 MB/s。
> **结论有两句，第二句才是关键**：① 元数据相对数据只有 10⁻⁴–10⁻⁵ 量级，本身不是"容量问题"；② **它的真实大小由「提交次数」而不是「数据量」决定**——同样 6.8 TB 的数据，60 秒提交一次是 467 MB，一分钟合并一次 manifest 是 2.5 GB，差 **5.4 倍**；而 compaction 之后是 0.65 MB，差 **3,800 倍**。**换任何假设都不改变"文件数 × 每条 entry"这条乘法链。**

### 账二 · 小文件读放大 = 每文件打开/寻址开销 × 文件数

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 一次「最近 7 天」查询要打开的文件数 | 311 万 × (7 ÷ 90) | **≈ 24.2 万个** |
| 2 | 串行打开耗时 | 24.2 万 × 30 ms（对象存储 GET 首字节 20–50 ms 取 30 ms） | 7,260 s ≈ **2.0 小时** |
| 3 | 并行 256 后 | 7,260 s ÷ 256 | **≈ 28 s** |
| 4 | 每文件通常要 2 次请求（footer + 数据） | 24.2 万 × 2 ÷ 256 × 30 ms | **≈ 57 s** |
| 5 | 统计解析 CPU | 24.2 万 × 0.5 ms ÷ 256 | ≈ 0.5 s（可忽略） |
| 6 | 读同样逻辑数据要多读多少 | 2.2 MB 文件压缩比 3.2×，512 MB 文件 5.0× | 多读 **56%** 的字节 |
| 7 | 请求费 | 48.4 万 GET × 0.0088 元/千次 | **4.3 元/次查询** |
| 8 | 每天 2,000 次看板查询 | 4.3 × 2,000 | **8,600 元/天 ≈ 25.8 万元/月** |
| 9 | compaction 后（4,320 文件） | 4,320 × (7÷90) ≈ 336 个文件 | 336 × 2 ÷ 256 × 30 ms ≈ **0.08 s**；0.006 元/查询 |
| 10 | 放大倍数 | 57 s ÷ 0.08 s | **≈ 700×**（打开成本），账单 **≈ 700×** |

> **量级结论（非承诺值）**。假设清单：① 对象存储 GET 首字节 20–50 ms，取 30 ms；② 并行度 256 且对象存储不限流（现实里会限流，实际更慢）；③ 每文件 2 次范围请求；④ 单次请求 0.0088 元/千次（对象存储 GET 量级报价，随厂商/区域/存储类变化，**以目标环境报价为准**）；⑤ 5×→3.2× 的压缩比损失来自"块太小导致字典与编码失效"，量级取自列存通用经验，精确值需实测。
> **结论**：小文件的成本有两项，**「文件数 × 30 ms 的打开罚」**与**「文件数 × 请求单价」**，**两项都与数据量无关**，只与「提交频率 × 分区数」成正比。所以"把保留期从 90 天砍到 45 天"能把账单砍一半，但**单位成本一分不降**——这正是小文件病最反直觉的地方。

### 账三 · MOR 删除文件开销 = 删除条数 × 读时合并成本

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 每天更新行数 | 给定 2,000 万行/天 | 2,000 万 |
| 2 | 每行 delete 记录字节 | equality ≈ 250 B（key 列+统计+Avro 开销）；position ≈ 110 B | 取 250 B |
| 3 | 每天新增 delete 字节 | 2,000 万 × 250 B | **5.0 GB/天** |
| 4 | 每天新增 delete 文件数 | 288 次提交 × 7 个受影响分区，每次每分区 1 个 | **2,016 个/天** |
| 5 | 单个 delete 文件平均大小 | 5.0 GB ÷ 2,016 | **2.5 MB**（典型小文件） |
| 6 | 90 天累积（不合并） | 2,016 × 90；5.0 GB × 90 | **18.1 万个文件 / 450 GB** |
| 7 | 不做 rewrite 时，事故现场的实测值 | 3 个月 + 两个 delete 模式叠加 | **41 万个 / 740 GB** |
| 8 | 一次 7 天查询的 data file 数 | 3.5 TB ÷ 512 MB | 7,000 个 |
| 9 | 每个 data file 适用的 delete 文件（同分区、序列号更小） | 7 天 × 288 次提交 | 最多 2,016 个 |
| 10 | 若逐文件重新加载 delete：读量 | 7,000 × 2,016 × 2.5 MB | **35 TB（不可行 → 引擎必须成组摊薄）** |
| 11 | 成组摊薄后（每 task 处理 8 个 data file = 4 GB） | 每 task 需加载该分区适用的 2,016 × 2.5 MB = 5.0 GB | 每 task 读 9.0 GB，**其中 56% 是 delete** |
| 12 | 执行路径罚 | 向量化列扫 100–200 MB/s/core → 带 delete 逐行 merge 20–40 MB/s/core | **3–5×** |
| 13 | 组合读放大 | `1 + delete 占比` × 执行罚 | **≈ 5.2×**（与事故实测一致） |
| 14 | 修法一：`rewrite_position_delete_files` | 2,016 个 2.5 MB → 2 个 128 MB | delete 读量 5.0 GB → **0.26 GB** |
| 15 | 修法二：热点分区改 COW | 写放大 1 ÷ 命中文件比例（§35.4 的算账表） | 用写换读，值不值得看"读多少遍" |

> **量级结论（非承诺值）**。假设清单：① equality delete 每行 250 B、position delete 每行 110 B；② 每天 288 次提交、7 个受影响分区；③ 一次查询读 7 天、data file 512 MB；④ 引擎按 partition 成组缓存 delete 文件（Iceberg 的默认规划行为；**具体摊薄策略随引擎版本不同，以目标版本文档为准**）；⑤ 向量化 vs 逐行 merge 的吞吐比 3–5×（区间来自列存通用实现，精确值需实测）；⑥ 不计 delete 文件的统计裁剪（position delete 的 `file_path` 上下界能裁掉一部分，equality delete 不能）。
> **结论**：MOR 的账有**两项**，而且性质完全不同——**「delete 文件数 × 读入成本」是可以靠 `rewrite_position_delete_files` 还清的（一次性）**，**「执行路径从向量化退化到逐行」是还不清的（只要表在 MOR 状态就一直在）**。所以 MOR 的正确用法是「**热点小窗口内高频更新 + 每天把窗口内的 delete 合并掉**」，不是"永远 MOR"。

### 三本账压成一张表

| 账 | 乘法链 | 谁在付 | 一次性能还清吗 | 拐点信号 |
| --- | --- | --- | --- | --- |
| **写放大**（COW / compaction） | 重写字节 = 命中文件比例 × 表大小（COW）；compaction = 2 × 被重写字节 | 写入作业、compaction 集群 | ✅ 但会随写入继续产生 | compaction 吞吐 < 新增文件速率 |
| **读放大**（小文件 / MOR） | 打开成本 = 文件数 × 30 ms；MOR = delete 文件数 × 读入 + 3–5× 执行罚 | **每一个查询** | ✅（rewrite）／❌（执行罚） | `plan files` 占比 > 20%；avg file < 32 MB；delete 文件数 > data 文件数 |
| **空间放大**（快照 / delete / 旧文件） | 快照数 × 变更量 + delete 字节 + 未回收旧文件 | 存储账单 | ✅（expire + rewrite） | 存储字节 ÷ 逻辑字节 > 1.5 |

```python
# 把本篇三本账压成一个可复算的脚本：改任一假设直接重跑（Python 3，无第三方依赖）
GB, TB = 1e9, 1e12

def account1_metadata(files, entry=150, manifest_mb=8, list_entry=500, merged=True):
    """账一：元数据 = 文件数 × manifest entry + manifest 数 × manifest-list entry"""
    payload = files * entry
    n_man = max(1, int(payload / (manifest_mb * 1024 * 1024))) if merged else files
    return payload, n_man, n_man * list_entry

def account2_open_cost(files_in_query, get_ms=30.0, parallel=256, req_per_file=2, price_per_k=0.0088):
    """账二：小文件的打开罚 + 请求账单（与数据字节无关）"""
    wall = files_in_query * get_ms * req_per_file / 1000 / parallel
    money = files_in_query * req_per_file * price_per_k / 1000
    return wall, money

def account3_mor(updates_per_day, row_bytes=250, commits=288, parts=7,
                 data_gb_per_task=4.0, vec_mbps=150, merge_mbps=30):
    """账三：delete 文件数 × 读入 ÷ 数据读入（IO 罚），与执行路径罚（3–5×）"""
    del_gb_day = updates_per_day * row_bytes / GB
    del_files_day = commits * parts
    del_gb_per_task = del_files_day * (del_gb_day / del_files_day)   # = 该分区 7 天 delete 字节
    io_penalty = del_gb_per_task / data_gb_per_task
    return del_gb_day, del_files_day, io_penalty, vec_mbps / merge_mbps

p, n_merged, l_merged = account1_metadata(3_110_000, merged=True)
_, n_raw, l_raw       = account1_metadata(3_110_000, merged=False)
print(f"账一 合并：entry {p/1e6:.0f} MB + list {l_merged/1e3:.0f} KB（{n_merged} 个 manifest）")
print(f"账一 未合并：list {l_raw/GB:.2f} GB（{n_raw:,} 个 manifest）→ 顺序读约 {l_raw/100e6:.0f} s")

w_big, m_big = account2_open_cost(int(3_110_000 * 7 / 90))
w_sml, m_sml = account2_open_cost(336)
print(f"账二 4.2M→4.3K 文件：{w_big:.0f} s → {w_sml:.2f} s（{w_big/w_sml:.0f}×）, "
      f"请求费 {m_big:.1f} → {m_sml:.3f} 元/查询")

d_gb, d_cnt, io_pen, exec_pen = account3_mor(20_000_000)
print(f"账三 delete {d_gb:.1f} GB/天、{d_cnt} 个文件/天；"
      f"IO 罚 {io_pen:.2f}×、执行罚 {exec_pen:.0f}× ⇒ 合计约 {1 + io_pen * exec_pen:.1f}×")
```

---

## 35.9 架构边界：三种表格式的形态差异，与「什么时候别上表格式」

### 只讲形态：Iceberg / Delta / Hudi

| 维度 | **Iceberg** | **Delta Lake** | **Hudi** |
| --- | --- | --- | --- |
| 元数据层数 | **三层树**：`metadata.json` → manifest list → manifest | **单层日志**：`_delta_log/*.json` 顺序重放，每 N 个 commit 一个 parquet checkpoint | **时间线 + 索引**：`.hoodie/` 下的 timeline instants + 各文件组的索引 |
| 一次提交写什么 | 新 manifest + 新 metadata.json + 指针切换 | 一个 JSON（add/remove 动作列表）+ 可选 checkpoint | 一个 timeline instant（`commit`/`deltacommit`/`compaction`）+ 索引更新 |
| 「文件清单」怎么得到 | **每个快照一份完整清单**（读树） | **重放日志得到清单**（读日志，快照 = 到某个 version 为止的重放结果） | 重放 timeline 到某个 instant |
| 行级删除的表达 | **独立 delete 文件**（position / equality），按 `sequence number` 决定作用范围 | deletion vectors（旁挂 `.bin`，与 parquet 一起读）/ 或重写文件 | MOR：**每文件组一份 log files**（Avro/HFile 增量），COW：直接重写 base file |
| upsert 的定位 | 无内置索引 → 靠 join 找出受影响文件 | 无内置索引 → 同左 | **有索引**（bloom / HBase / bucket 索引）→ 定位到文件组更快 |
| 文件布局 | data file 与 delete file 平级 | 同左 | **file group = base file + 一串 log file**（MOR 特有） |
| 分区的表达 | hidden transform + spec id | 目录分区 / generated column（演进要重写） | 目录分区 + 元数据字段 |
| 时间旅行 | 快照 id / 时间戳 | version / 时间戳 | instant 时间 |
| 形态上的强项 | 元数据演进与多引擎互操作最干净 | 与 Spark 生态耦合最紧、日志简单 | 单文件组内的增量 log 让高频 upsert 最省写 |

> **这一节只讲形态，不站队。** 三者的差别可以压成一句：**Iceberg 用"树"回答"这个版本有哪些文件"，Delta 用"日志重放"回答同一问题，Hudi 用"文件组 + 增量 log"把"更新"下沉到了文件内部。** 选型（该不该上湖仓、选哪个）归 `→ architect/21` 与 [eng/35 存储选型](../eng/35-storage-model-selection.md)，本篇只给"它们的物理形态决定了什么"。

### 什么时候该用表格式，什么时候直接上数据库/列存

| 你的处境 | 表格式合适吗 | 该选什么 | 判据（可量化的） |
| --- | --- | --- | --- |
| 单表 > 数 TB，多引擎（Spark/Trino/Flink/湖仓 SQL）要共享同一份数据 | ✅ **最合适** | Iceberg / Delta / Hudi + 对象存储 | 引擎数 ≥ 2，或数据量超出单机列存舒适区 |
| 需要时间旅行、审计回溯、可重现的报表快照 | ✅ 强项 | 同上 | 合规要求"任意时点可查" |
| schema 演进频繁（每周加列），不能停写 | ✅ 强项 | 同上 | 演进频率 > 每月 1 次 |
| 批流一体：同一张表既被流写又被批读 | ✅ 合适（MOR + 定时合并） | Iceberg MOR + `rewrite_*` 作业 | 写入频率 ≥ 每 5 分钟一批 |
| **毫秒级点查 / 高并发点更新** | ❌ 不建议 | PostgreSQL / MySQL / HBase / Redis | 点查 P99 要求 < 50 ms |
| 单表 100 GB–2 TB，只被一个分析引擎用，查询以聚合为主 | ⚠️ 可能过度设计 | **ClickHouse / Doris 直接上**（[20 MergeTree](20-clickhouse-mergetree.md) 一句话结论：列存用「连续 + 向量化 + 稀疏索引」换扫描吞吐，代价是点更新与事务几乎不存在） | 引擎数 = 1，且无时间旅行需求 |
| 需要跨表强事务（转账、库存扣减） | ❌ 不行 | 关系库 | 事务边界跨 ≥ 2 张表 |
| 需要二级索引做多维过滤 + 排序 + 分页 | ❌ 不行 | 搜索引擎 / 关系库（`→ comp/21`） | 查询以"任意列组合过滤 + 排序"为主 |
| 数据量 < 数百 GB、日增 < 10 GB | ⚠️ 小文件与元数据的运维成本会盖过收益 | 单机列存 / 关系库 | 文件数 < 1 万时表格式的收益不明显 |

### 解剖卡八问里最容易写错的两格

| 八问 | 本篇的答案 |
| --- | --- |
| **⑤ 扩展维度** | **主答案是「批处理摊薄」**：表格式把一次查询拆成 N 个 `FileScanTask` 并行跑，扩展的是"并行读的文件数"，**不是单文件吞吐**。分片 ✅（按分区/文件切，且切法由 `split_offsets` 保证一个文件也能切）；副本 ✗（对象存储自己做多副本，表格式不管）；垂直切 ✗（列裁剪是执行引擎的事）；**批处理摊薄 ✅ 且是唯一的主轴**——这正是"表格式必须靠 compaction 维持单文件大小"的根本原因：**单文件越小，可切分性越差，摊薄就越无效**。 |
| **⑥ 系统级失效** | **只有三类**：① **元数据爆炸 → 全表查不动**（manifest list 成 GB 级单对象，事故一；它不报错，只是所有查询一起变慢）；② **数据不一致 → 静默错数**（catalog 不支持原子切换时两个 writer 互相覆盖；或 delete 文件序列号写错导致多删/少删）；③ **成本失控**（文件数 × 请求单价 × 查询次数，§35.8 账二第 7–8 步）。使用级错误（参数设错、`DROP`+`ADD` 同名、忘了跑合并）归 §35.10，两节不互相稀释。 |

---

## 35.10 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到物理形态） | 怎么定位 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 表不大（几百 GB），查询却要几分钟，`plan files` 占 90% | manifest 未合并：每次提交一个 manifest → manifest list 成 GB 级单对象 | `SELECT count(*) FROM t.manifests`；`ls -l` 看 manifest list 对象大小；`commit.manifest.min-count-to-merge` 现值 | `rewrite_manifests` + 把 `min-count-to-merge` 调回默认量级 | 把「manifest 数 / manifest list 字节」纳入日常巡检（§35.11） |
| 平均值 2 MB 的小文件几十万个，查询打开文件就超时 | 提交频率 × 活跃分区数 = 文件产生速率，与数据量无关 | `files` 表按 size 分桶；`avg(file_size_in_bytes)` | `rewrite_data_files` 到 128–512 MB，并把它做成定时作业 | 写入频率写进容量评审；**compaction 必须进写入路径** |
| 加列/改名后报表数字变小或变 NULL，没有任何报错 | 下游按列名/位置读文件；`DROP`+`ADD` 同名拿到新 field id | `SELECT * FROM t.schema` 对比 field id；核下游是否绕过表 API 直读 `data/` | 下游改走表 API；必要时 `ALTER COLUMN` 回原名并重建映射 | 冻结 `data/` 目录直读权限；schema 变更走变更评审 |
| 下游突然报 "Cannot find field id" / 分区列找不到 | 分区演进后老文件的 `spec id` 与查询假设的分区规则不一致；或改名了分区源列 | `SELECT spec_id, count(*) FROM t.manifests GROUP BY 1` | 让引擎按 spec id 解释（正常引擎会）；不要手写分区路径过滤 | 分区演进后回归跑一次全量查询；禁止手拼 `data/` 路径 |
| 同一条查询昨天 3 分钟今天 16 分钟，加机器也没用 | MOR 的 delete 文件从未合并，读放大复利 | `files` 表按 `content` 分桶；`physicalInput / processedInput` 比值；`t.deletes()` 数量 | `rewrite_position_delete_files` + 带 `delete-file-threshold` 的 `rewrite_data_files` | 把「delete 文件数 ÷ data 文件数」做成告警（> 0.5 就动手） |
| 两个作业同时写，数据少了/分区"回到过去" | catalog 无原子切换（文件式 catalog 在 S3 上 `rename` 是 copy+delete），后提交者覆盖 | 比对 `metadata_log_entries` 与两个作业的提交时间；看有无 `CommitFailedException` | 换支持原子提交的 catalog（HMS/Glue/Nessie/REST） | 生产禁用文件式 catalog；提交路径必须处理冲突重试 |
| 存储账单每月涨 30%，`expire_snapshots` 跑了却没降 | 过期只删"清单"，**孤儿文件**（提交失败/中断留下的文件）永远不在任何清单里 | `remove_orphan_files --dry-run` 看候选量；对比 `du data/` 与 `SELECT sum(file_size_in_bytes) FROM files` | `remove_orphan_files`（`older_than` 必须 > 最长作业时长） | 每次作业失败后自动跑一次；在写入模板里固定 `expire_snapshots` |
| 时间旅行查不了，报"未知快照" | 快照被 `expire_snapshots` 清掉了；或 `retain_last` 设成 1 | `SELECT * FROM t.snapshots ORDER BY committed_at DESC` | 调大 `retain_last` 与 `history.expire.max-snapshot-age-ms` | 合规要求的保留期写进表属性并纳入巡检 |
| 长字符串列（UUID/URL）的等值查询全表扫 | 统计被 `truncate(16)` 截断成前缀，上下界几乎相同 | 看 `files` 表的 `lower_bounds`/`upper_bounds` 是否只剩前缀 | 换列（前缀可比的 ID）、或改用 bucket 分区、或把该列放到排序键前缀 | 建表时就把"会不会拿它做过滤"与"前缀有没有区分度"一起评 |
| CDC 落湖每批都成功，但读起来行数忽多忽少 | delete 文件与 data 文件的 `sequence number` 关系被破坏（手写 delete 文件/自研写入器） | 对比同一快照里 delete 的 `sequence_number` 与 data 的 `data sequence number` | 用官方 writer API 生成 delete 文件，不要手拼 Avro | 自研写入器必须过一致性校验（行数 + 抽样对账） |

---

## 35.11 观测：六个数字 + 两条命令

| 指标 | 怎么读（元数据表 / 命令） | 版本 / 开关前提 | 拐点（越线就动手） | 它度量的是哪件物理事实 |
| --- | --- | --- | --- | --- |
| **快照数** | `SELECT count(*) FROM t.snapshots` | 元数据表随 Iceberg 版本演进，**表名与列名以目标版本文档为准** | > 500 或 `metadata.json` > 10 MB | 时间旅行窗口与空间放大的源头 |
| **manifest 数 / 单个大小** | `SELECT count(*), avg(length) FROM t.manifests` | 同上；`length` 是 manifest 文件字节 | manifest 数 > 文件数 ÷ 10,000；avg < 100 KB | 元数据是否被合并（事故一的直接指标） |
| **manifest list 字节** | 从 `metadata.json` 的当前 snapshot 里取 `manifest-list` 对象大小（或 `sum(length)` 近似） | 同上 | **单对象 > 50 MB 就必须处理**（顺序读 > 0.5 s） | 每次查询的固定前置成本 |
| **文件大小分布** | `files` 表按 `file_size_in_bytes` 分桶 | `content=0` 才是数据文件 | **avg < 32 MB 或 p50 < 16 MB** | 小文件病的严重度（§35.8 账二） |
| **delete 文件占比** | `SELECT content, count(*), sum(file_size_in_bytes) FROM t.files GROUP BY 1` | `content`：0=data / 1=position delete / 2=equality delete | **delete 文件数 ÷ data 文件数 > 0.5** | MOR 的债务余额（§35.8 账三） |
| **compaction 进度** | 自建：`今日新增文件数` vs `今日重写文件数`（从 `snapshots.summary` 或作业侧打点） | `summary` 字段名随版本变化，**以目标版本为准** | 新增 > 重写（连续 3 天） | 追赶不等式是否即将失守 |
| **`plan files` 占比** | 查询引擎侧的 profiling（Trino/Spark 的计划阶段耗时） | 各引擎指标名不同 | **> 20% 的查询时间** | "读得慢是元数据不是数据"的判据 |

```sql
-- 值班第一屏：六个数字（Spark SQL；元数据表名与列名以目标版本文档为准）
SELECT 'snapshots' AS k, CAST(count(*) AS STRING) AS v FROM lake.orders.snapshots
UNION ALL SELECT 'manifests',        CAST(count(*) AS STRING) FROM lake.orders.manifests
UNION ALL SELECT 'manifests_avg_kb', CAST(round(avg(length)/1024) AS STRING) FROM lake.orders.manifests
UNION ALL SELECT 'data_files',       CAST(count(*) AS STRING) FROM lake.orders.files WHERE content=0
UNION ALL SELECT 'pos_del_files',    CAST(count(*) AS STRING) FROM lake.orders.files WHERE content=1
UNION ALL SELECT 'eq_del_files',     CAST(count(*) AS STRING) FROM lake.orders.files WHERE content=2;

-- 文件大小分布：avg/p50 小于 32 MB 就是小文件病
SELECT CASE WHEN file_size_in_bytes <   8e6 THEN '<8MB'
            WHEN file_size_in_bytes <  32e6 THEN '8-32MB'
            WHEN file_size_in_bytes < 128e6 THEN '32-128MB'
            WHEN file_size_in_bytes < 536e6 THEN '128-512MB'
            ELSE '>=512MB' END AS bucket,
       count(*) AS files, round(sum(file_size_in_bytes)/1e9, 2) AS gb
FROM lake.orders.files WHERE content = 0 GROUP BY 1 ORDER BY 1;

-- 分区级 delete 债：一行就看出哪里最该重写
SELECT partition, file_count, record_count,
       position_delete_record_count, position_delete_file_count,
       equality_delete_record_count, equality_delete_file_count
FROM lake.orders.partitions
WHERE position_delete_record_count + equality_delete_record_count > 0
ORDER BY equality_delete_record_count DESC LIMIT 20;
```

```bash
#!/usr/bin/env bash
# 元数据负债巡检：不进查询引擎，只读对象存储（每次成本可忽略，适合每分钟跑）
set -euo pipefail
B=s3://lake-prod/warehouse/lake.db/orders

# ① 当前元数据指针：文件式 catalog 读 version-hint；HMS/Glue/REST 从 catalog 取 metadata_location
VER=$(aws s3 cp "$B/metadata/version-hint.text" - 2>/dev/null | tr -d '\n' || true)
echo "version-hint = ${VER:-<从 catalog 查 metadata_location>}"

# ② metadata.json 的体积趋势：快照数一多，它自己就变成一个大对象
aws s3 ls "$B/metadata/" | grep -E 'metadata\.json$' | sort -k4 | tail -3

# ③ 最近三次提交的动作与量（summary 字段名随版本变化，缺失时打 ?）
aws s3 cp "$B/metadata/${VER}.metadata.json" - 2>/dev/null | jq -r '
  "snapshots=\(.snapshots|length)  last-seq=\(."last-sequence-number")",
  (.snapshots[-3:][] | "  \(."snapshot-id") \(.summary.operation)
     added-data=\(.summary."added-data-files" // "?")
     added-del=\(.summary."added-delete-files" // "?")")'

# ④ 真实文件大小分布不要用 LIST（LIST 千次约 0.045 元且会限流）：
#    改用对象存储的 Inventory 落到 Athena，按 size 分桶查（见 §35.8 账二的第 7 步）
echo "下一步：S3 Inventory + Athena 出文件大小分布，代替 LIST"
```

```text
# 告警骨架（Prometheus / 自建指标同构；阈值取自本节拐点列）
# ① 元数据爆炸：manifest 数相对文件数失控
iceberg_manifest_count / iceberg_data_file_count > 0.0001
# ② manifest list 单对象过大（顺序读 > 0.5 s）
iceberg_current_manifest_list_bytes > 50 * 1024 * 1024
# ③ 小文件病：平均文件大小掉到 32 MB 以下
iceberg_data_file_avg_bytes < 32 * 1024 * 1024
# ④ MOR 债务：delete 文件数超过数据文件数的一半
iceberg_delete_file_count / iceberg_data_file_count > 0.5
# ⑤ 规划阶段占比：读得慢不是数据的问题
query_plan_duration_seconds / query_total_duration_seconds > 0.20
# ⑥ 追赶不等式即将失守：新增文件速率连续 3 天高于重写速率
rate(iceberg_files_added_total[1d]) > rate(iceberg_files_rewritten_total[1d])
```

---

## 35.12 一页速记

| 记什么 | 具体结论 |
| --- | --- |
| 物理形态一句话 | **catalog 指针 → `metadata.json` → manifest list → manifest → data/delete files**；一个 snapshot = 一份完整文件清单，不是 diff |
| 原子提交的秘密 | 文件先写完（可丢弃）→ 新 manifest → 新 manifest list → 新 metadata.json → **catalog 指针切换**；只有最后一步"生效" |
| 三种身份 | `field id`（列）、`spec id`（分区规则）、`snapshot id`（版本）；**`sequence number` 决定 delete 作用到谁**（position ≤，equality <） |
| COW vs MOR | COW 写放大 ≈ 1 ÷ 命中文件比例（稀疏更新趋近全表）；MOR 写极小、读放大 3–5× 且**复利计息** |
| 小文件的成本 | 打开罚 = 文件数 × 30 ms；账单 = 文件数 × 请求单价 × 查询次数 ——**两项都与数据量无关** |
| 元数据的成本 | = 文件数 × 100–250 B + manifest 数 × 0.3–1 KB；**由提交次数决定，不由数据量决定** |
| 读得慢的判据 | `plan files` 占比 > 20%，或 `planFiles` 次数 ÷ 实际读文件数 > 10 → 治元数据，别加 worker |
| 维护四件套 | `rewrite_data_files`（治小文件 + delete 债）、`rewrite_position_delete_files`（治碎 delete）、`rewrite_manifests`（治碎 manifest，最便宜）、`expire_snapshots`（治空间放大） |
| 追赶不等式 | 新增文件速率 < compaction 吞吐，否则差距按天复利 |
| 扩展维度 | 主答案是「批处理摊薄」：扩的是并行文件数，**不是单文件吞吐** → 所以必须维持单文件大小 |
| 首要指标 | manifest 数、manifest list 字节、avg 文件大小、delete 文件占比、`plan files` 占比 |

**如果只记五句：**

1. **表的本质是一份被版本化的文件清单**，不是目录；所以"原子提交"靠的是指针切换，"快照隔离"靠的是清单不可变——**两者都不需要锁**。
2. **提交是 O(1) 的，读是 O(文件数) 的**。这条不对称是表格式唯一的结构性矛盾：写越勤快（流式），读越慢，而两者的账都由「文件数」结算。
3. **改一行只能被表达成"加一条删除记录"**。COW 把代价放在写侧（重写整个文件），MOR 放在读侧（每次查询套补丁）——**你只能选在哪边付，不能两边都不付**。
4. **schema 演进的前提是"身份与名字分离"**。`field id` 是身份，列名只是别名；`DROP` 再 `ADD` 同名会拿到新 id，旧数据永远 NULL 而且**一声不响**。
5. **compaction 不是事后清理，是写入路径的一部分。** 只要有人以 60 秒的频率提交，"合并"就必须是一个有 SLA 的常驻作业——否则那笔债会以「查询规划时间」和「对象存储请求费」两种形式，按月向你收利息。

---

## 35.13 自检题（5 题）

**Q1.** 一张 Iceberg 表 6.8 TB、文件数 311 万（平均 2.2 MB），manifest 未合并。一次只读 0.6 GB 的查询跑了 6 分 40 秒。请把这个时间拆成"元数据"与"数据"两部分，并说明为什么加 3 台 worker 只能改善 10%。

<details><summary>答案</summary>

**拆解**（按 §35.8 账一、账二）：

① **元数据部分 ≈ 6.5 分钟**：
- manifest list 是单对象 2.1 GB（311 万个 manifest × 一条 0.5 KB 的 list entry）。**单对象的顺序读无法通过加 worker 变快**——一个 GET 流的有效吞吐约 100 MB/s 量级，2.1 GB 就是约 21 s，而且这 21 s 是一个任务串行做的。
- 接着要打开 311 万个 manifest；就算裁剪后只剩 12 万个，也是 `12 万 × 30 ms ÷ 256 并行 ≈ 14 s`，再加解析 467 MB 的 entry → 分钟级。
- **这一段的耗时与"要读多少数据"完全无关。**

② **数据部分 ≈ 10 秒**：0.6 GB 压缩数据，按 200 MB/s/核 × 16 并行，远小于 1 s；加上调度与网络取 10 s 已宽裕。

③ **为什么加 worker 只改善 10%**：并行只对"可并行部分"有效。级 2 是单对象串行；级 3 虽可并行，瓶颈却是对象存储的**请求速率**（并发一高就限流），不是 CPU。3 台 worker 把级 4/5 从 45 s 压到 20 s，对 400 s 总时长就是 6%。

**正解**：`rewrite_manifests`（311 万个打包成 ~60 个）+ 把 `commit.manifest.min-count-to-merge` 调回默认量级 + 写入侧"先攒批再提交"。manifest list 从 2.1 GB 降到 30 KB，规划阶段从 6 分 40 秒降到 3 秒。

量级结论，非承诺值；假设 30 ms/GET、256 并行、单对象读 100 MB/s、entry 150 B。
</details>

**Q2.** 有人提议：「我们更新量很大，所以全表都用 MOR；反正 delete 文件很小，读的时候忽略不计。」用 §35.8 账三的数字说明这句话错在哪，并给出这条路上"至少要配什么作业"。

<details><summary>答案</summary>

**错在两点，方向完全不同：**

① **delete 文件小 ≠ 读的时候便宜**。每行 equality delete 约 250 B，2,000 万行/天 = 5.0 GB/天；90 天不合并就是 18 万个文件 / 450 GB（事故现场 41 万个 / 740 GB）。而**一个 data file 必须套用它适用的全部 delete 文件**：同分区 7 天 × 288 次提交 = 最多 2,016 个 delete 文件。**文件数**（而不是字节数）才是打开成本的乘数，这与 §35.8 账二同一条乘法链。

② **更大的那一半是执行路径**：向量化列扫 100–200 MB/s/core → 带 delete 逐行 merge 后 20–40 MB/s/core，**3–5×**。这一项**不会因为合并而消失**——只要文件要套 delete 补丁，它就慢。所以"MOR 只多一点点 IO"是错的，**它的主成本在 CPU 与执行路径，不在字节**。

组合起来就是事故实测的 5.2×（`1 + 55% delete 占比` 乘上 4× 执行罚）。

**这条路上至少配的作业**（缺一个就是在借高利贷）：

- `rewrite_position_delete_files`（每天，把碎 delete 文件合到 128 MB 级）；
- `rewrite_data_files` 带 `delete-file-threshold`（每天，优先重写"带 delete 的文件组"，把补丁直接烧进数据文件）；
- `rewrite_manifests`（每天，delete 文件也会制造 manifest）；
- `expire_snapshots`（每天，否则旧 data 文件永不回收）；
- **告警**：`delete 文件数 ÷ data 文件数 > 0.5`、`physicalInput / processedInput > 2`。

**更根本的建议**：MOR 只用于"热点小窗口"（最近几天的表），超过窗口的数据改走 COW 或直接进另一个"只追加"的表——**让"要更新的部分"和"要被扫的部分"在物理上分开**。

量级结论，非承诺值。
</details>

**Q3.** 一个团队报告：「我们 `ALTER TABLE` 加了一列之后，下游报表的数字变小了。」请给出三个最可能的根因，以及各自的确认命令。

<details><summary>答案</summary>

① **下游绕过表 API 按列位置读文件**（事故二）：加列后所有后续列位置右移，读到的是隔壁列的值，类型转换失败置 NULL。确认：`SELECT * FROM t.schema` 拿到 field id 顺序，对比下游读到的列序；查下游是否直接 `read.parquet("data/...")`。

② **`DROP` 后又 `ADD` 同名**：拿到新 `field id`，历史文件里没有这个 id → 全 NULL。确认：查 `schema` 历史（`SELECT * FROM t.schemas` 或 `metadata_log_entries`），看该列名对应的 `field id` 是否变过。

③ **rename 之后旧统计/旧分区 spec 与新列名对不上**：`lower_bounds` 按 field id 存，所以 Iceberg 侧不会错；但如果有人手写 `WHERE` 用的是分区目录名或旧列名去拼路径，就会读到空分区。确认：`SELECT spec_id, count(*) FROM t.manifests GROUP BY 1` 看是否有多套 spec；核查询是否手拼 `data/dt=` 路径。

**通用判据**：**只要数字是"静默变小"而不是"报错"，第一怀疑对象就是身份错位（id vs 名字/位置），不是引擎 bug。** 修复顺序：先把下游改成走表 API（`field id` 解析），再讨论 schema 怎么演进。

</details>

**Q4.** 表 500 GB，data file 512 MB。要给 5 万行（约占 0.01%）打一个标记。分别算 COW 与 MOR 的写入量，并说明"选 MOR"之后你唯一不能省的那件事。

<details><summary>答案</summary>

**COW**：最小重写单位是文件（512 MB）。5 万行随机分布 ⇒ 几乎每个文件都被命中（977 个文件）⇒ 写入 ≈ `977 × 512 MB ≈ 495 GB`。**写放大 ≈ 495 GB ÷ 40 MB ≈ 12,400×**。若这 5 万行恰好集中在一个分区的一小片文件里，写入就降到几个 GB——**COW 的代价由"改得多散"决定，与"改多少行"几乎无关**。

**MOR**：写一个 position delete 文件，每行约 `(file_path ~80 B + pos 8 B)` + Avro 开销 ≈ 110 B ⇒ `5 万 × 110 B ≈ 5.5 MB`。**写放大 ≈ 1.1×**。

**唯一不能省的那件事**：**在 SLA 内把 delete 文件合并掉**（`rewrite_position_delete_files` + 带 `delete-file-threshold` 的 `rewrite_data_files`）。不合并的话，读放大按 §35.8 账三的乘法链复利增长，而**它收利息的对象是所有查询**——包括跟这次标记毫无关系的那些看板。

一句话：**COW 是"一次付清、付在写侧"，MOR 是"分期付款、付在每一个读者身上"，而分期的利率是正的。**
</details>

**Q5.** 「我们只有 800 GB 数据、一个 Trino 集群、每天 200 个看板查询，要不要上 Iceberg？」给出你的判断口径（要能当评审结论用）。

<details><summary>答案</summary>

**先问四个问题，任一为"是"就把天平推向表格式：**

1. **会不会超出单机列存舒适区，或需要多个引擎共享同一份数据？** 800 GB + 只有 Trino ⇒ 单机/小集群 ClickHouse、Doris，甚至 Trino + 裸 Parquet + Hive 分区都能扛，表格式收益有限（§35.9 判定表）。
2. **要不要时间旅行 / 审计回溯 / 可重现快照？** 有合规需求 ⇒ 表格式是最便宜的答案（自己实现版本管理远比它贵）。
3. **schema 演进频率高不高、能不能停写？** 每周加列且不能停 ⇒ 几乎必需。
4. **写入是不是流式（每几分钟一批）？** 是 ⇒ 你已注定要面对小文件与 delete 文件，**必须同时接受"养一个 compaction 作业"**；没有这个人力，反而该先用"批处理 + 每日全量重写"。

**结论口径（可直接写进评审）**：800 GB + 单引擎 + 无时间旅行 ⇒ **表格式是过度设计**，先上列存；但**第 2/3/4 问任一为"是"，800 GB 就不是问题**——表格式的成本与数据量关系不大，与"提交频率 × 分区数"和"团队运维人力"关系更大（§35.8 三本账）。

**必须一起报的三条运维承诺**（否则别上）：① 常驻 compaction 作业 + 追赶不等式监控；② `expire_snapshots` 定时任务 + 孤儿文件清理；③ 一条纪律：**任何 schema 变更都要核 field id，且禁止下游绕表 API 直读 `data/`**。

量级结论性质，非承诺值；具体选型归 [eng/35 存储选型](../eng/35-storage-model-selection.md) 与 `→ architect/21`。
</details>

---

## 35.14 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 列存引擎自己的 part 与 merge：本篇的「小文件 compaction」在列存里对应「part 合并」，但列存没有元数据树，也没有 delete 文件 | [20 ClickHouse / MergeTree](20-clickhouse-mergetree.md) §20.3、§20.5 |
| 只追加日志的形态对照：Kafka 的段与 Iceberg 的 file 都不可变，但一个按位点顺序读、一个按清单并行扫 | [28 Kafka 的日志](28-kafka-log-segments.md) §28.3、§28.4 |
| 副本与提交位点：本篇的「提交」是一次元数据指针切换，Kafka 的「提交」是 HW 前移，两套语义完全不同 | `→ comp/29` |
| 消费组与积压：流式写入的提交频率（也就是小文件的产生速率）由这里的并行度与位点策略决定 | `→ comp/30` |
| CDC 落湖：本篇的 MOR 与 delete 文件，在链路上游就是 CDC 的 upsert 形态 | `→ comp/33` |
| Flink checkpoint：每次 checkpoint 提交对应一个 Iceberg commit —— 这是「小文件速率 = 提交频率」的源头 | `→ comp/34` |
| 定时与延迟：`expire_snapshots` / compaction 这类"到点必须被发现"的作业，归延迟队列与时间轮的机制 | `→ comp/36` |
| 对象存储语义：没有 rename、LIST 计费、单对象顺序读吞吐、503 重试 —— 本篇所有"30 ms/GET"与"单对象 100 MB/s"都来自这里 | `→ comp/42` |
| 数据湖的分区键、格式选型与分层 TTL：「一次写入、多种查询」的决策与量级估算，本篇只给表格式的内部机制 | `→ architect/21` |
| 数据闭环：湖表在「事件 → 特征 → 决策」回路里的位置，以及口径污染如何产生 | `→ architect/11` |
| 存储选型矩阵与"数据模型决定存储"：该不该上湖仓、和数据库/列存/KV 的分界 | [eng/35 存储选型](../eng/35-storage-model-selection.md) |
| 分布式锁与选主：自研 compaction 作业的互斥、以及"旧主人写回"的挡板 | `→ comp/46` |
| 对象存储上的分布式 ID 与唯一性：主键设计影响 bucket 分区与统计裁剪的有效性 | `→ comp/45` |
