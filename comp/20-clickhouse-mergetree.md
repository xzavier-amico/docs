# 20. ClickHouse / MergeTree：列存为什么快、为什么不能当主库

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · LSM 族与分析 ｜ **编号**：20 / 53
> **本篇定位**：Part IV 的收官篇。前三篇（MongoDB / HBase / RocksDB+TiKV）都在讲「怎样用一份追加日志把随机写摊掉」，本篇讲另一条路：**把同一列的行连续存、按块向量化算、用稀疏索引跳过整段**，用「扫描吞吐」换掉「更新与点查」。列存的通用机制（列连续、min/max 与跳数索引、向量化、删除标记 + 后台 merge）已在 `→ comp/02` §02.5 与 §02.6 钉死，本篇不再重复，只做一件事：**把 ClickHouse / MergeTree 的具体形态、它换来的四条真实收益、以及它必须付出的三笔代价（part 不可变、mutation 重写、副本异步）逐条算成数字**。读完应该能回答两个问题：为什么一次列裁剪能把 IO 从 40 GB 打到 1 GB；为什么同一套算术让你永远不该把「按订单 ID 点查」和「改订单状态」放到这张表上。
> **读法**：§20.1 三起事故分别对应三个支点（mutation、part 爆炸、点查放大），先建立「列存不报错、只换个地方死」的直觉；§20.2 的唯一真问题是全篇支点，后面每一节都在给它加证据；§20.3 的两张图（part → 列文件 → granule、一次点查只读哪 5 个文件）是全部推论的物理底座，建议照着画一遍；**§20.5–20.9 是核心**，part 的不可变性在那里被算成一笔笔账；评审材料直接抄 §20.9 的四张账表与 §20.13 的一页速记。选型（该不该用列存、用到什么规模换形态）不在本篇，一律看 [eng/35 存储选型](../eng/35-storage-model-selection.md)。

---

## 20.1 引子：三起「导入很快、更新很慢、并发很惨」的故障

列存的事故有个共同的形状：**导入压测漂亮，监控面板干净，出事的永远不是它最擅长的那件事**。它擅长扫，所以扫描从来不报警；它不擅长「改一行」「读一行」「一千个人同时读一行」，而这三个动作在业务里都藏在一个叫「更新状态」「查订单详情」的普通按钮后面。

### 事故一：把订单状态机放进 ClickHouse，一次「改状态」排了 4.1 万条 mutation（2026-02-09，某本地生活平台）

`dwd_order` 8.6 亿行，`PARTITION BY toYYYYMM(created_at)`，`ORDER BY (shop_id, created_at)`，38 个分区、每个分区 1–2 个 part、压缩后合计 92 GB（单个 part 约 2.4 GB）。数据中台「为了让报表和业务一张表」，把订单主表的**状态机**也放进了这张表，客服后台因此获得了一个「批量改状态」的按钮——每次点击执行一条 `ALTER TABLE dwd_order UPDATE status = … WHERE order_id = …`。

关键在于：**`order_id` 不在排序键里**，所以这条语句既不能用稀疏主键索引定位、也无法裁剪 part，**它必须扫全部 38 个 part**；而 mutation 的重写单位是 part，不是行。改 1 行与改 100 万行的账单几乎一样。

| 时间 | 事件 | 未完成 mutation | active part | 报表 P99 | 数据新鲜度 |
| --- | --- | --- | --- | --- | --- |
| 02-09 09:40 | 功能灰发，每次改 1–50 行，后台静默执行 | 0 | 46 | 3.1 s | 15 s |
| 02-09 11:00 | 客服批量「取消未支付」，20 分钟 1,800 次点击 | 610 | 52 | 4.8 s | 6 min |
| 02-09 14:00 | 每次 mutation 读+写 ≈ 7.4 GB，盘被重写打满 | 6,300 | 88 | 22 s | 41 min |
| 02-09 20:00 | 峰值：每分钟新增 380 条 mutation、完成 40 条 | 41,000 | 190 | 68 s | 3.5 h |
| 02-10 09:00 | 停按钮，改「离线重算 + 分区替换」 | 12,000（仍在消化） | 240 | 41 s | 2 天（报表走旧快照） |
| 02-11 复盘 | 3.2 万单状态延迟或改错，财务报表重出两次，赔付与加班 43 万元 | — | — | — | — |

**「改 1 行要扫 38 个 part」不是配置问题，是「列文件不可变」的必然结果**：没有「改一个值」这个动作，只有「生成一个新的 part 目录替换旧的」。§20.6 把这个数字算到秒。

### 事故二：采集服务逐条 insert，一天 120 万个 part（2025-12-18，某 SaaS 埋点平台）

`events` 表 20 列，采集端 8 个 pod、每个 pod 一个连接、事件一到就 `INSERT INTO events VALUES (…)`。日均 1,200 万事件 → 平均 14 条/秒 → **14 个 part/秒**。

MergeTree 里每一次 INSERT 都提交一个**新的不可变 part**：20 列 → 每列 `.bin` + `.mrk2` → 加上 `primary.idx`、`checksums.txt` 等，**每个 part 至少 45 个文件**。而且 merge **只在同一分区内进行**，`parts_to_throw_insert` 这个阈值也是**按分区**算的——按 `toDate(ts)` 分区意味着一天的全部 part 挤在同一个分区里。

| 时间 | 事件 | 写入速率 | active part | merge 线程占用 | 写入结果 |
| --- | --- | --- | --- | --- | --- |
| 12-18 00:00 | 采集上线，逐条 INSERT | 14 part/s | 0 → 累积 | 30% | 正常 |
| 12-18 06:00 | 单分区 part 数越过 `parts_to_delay_insert` | 14 part/s | 3.8 万 | 85% | 插入被主动减速（sleep） |
| 12-18 12:00 | 产生速率恒定、合并速率被文件数拖死 | 14 part/s | 21 万 | 100% | 单次插入 9 s |
| 12-18 16:40 | 越过 `parts_to_throw_insert` | 14 part/s | 37 万 | 100% | `Too many parts (309). Merges are processing significantly slower than inserts` |
| 12-18 17:00 | 采集端重试放大 3×，把「拒写」变成「更多写入」 | — | — | — | 全拒 |
| 12-18 18:30 | 采集侧改为「攒 5,000 条或 2 s 一批」 | 0.5 part/s | 2.4 万（回落中） | 60% | 恢复 |
| 12-19 复盘 | 6.5 小时埋点缺口，约 3,400 万事件丢失（客户端无重放缓冲） | — | — | — | — |

算一下最刺眼的一个数：120 万个 part × 45 个文件 ≈ **5,400 万个小文件**。这个量级下 merge 本身的元数据开销（打开、校验、比较 block 范围）已经超过数据字节本身。

> 阈值口径提醒：`parts_to_delay_insert` / `parts_to_throw_insert` 的默认值在版本间被上调过（`→ comp/02` §02.11 按早期默认 300 记），**一律以目标版本 `system.merge_tree_settings` 的输出为准**，不要背数字。

### 事故三：把「按用户查最近订单」的接口打到 ClickHouse，P99 从 5 ms 到 300 ms（2026-03-04，某电商 App）

接口 `GET /orders/recent?user_id=…`，日常 20 QPS、P99 5 ms，原本走 MySQL（`KEY (user_id, created_at)`，命中即取 20 行 = 3 次页 IO）。为「少维护一份存储」，接口被切到 `dws_user_order_recent`：3,000 万行、10 列、`ORDER BY (shop_id, created_at)`——**注意 `user_id` 不在排序键里，也没有为它建跳数索引**。

| 时间 | 事件 | 每查询读字节 | CPU（32 核） | P99 |
| --- | --- | --- | --- | --- |
| 03-04 14:00 | 切流 10%（2 QPS），一切正常 | 300 MB | 12% | 40 ms（假象） |
| 03-04 14:20 | 切到 50%（10 QPS） | 300 MB | 46% | 90 ms |
| 03-04 14:32 | 全量 20 QPS，且每个查询要吃 8 个线程 | 300 MB | **100%** | 300 ms，超时率 4% |
| 03-04 14:40 | 线程池排队，连报表查询一起变慢 | — | 100% | 报表 3 s → 68 s |
| 03-04 14:55 | 接口回切 MySQL | ≈48 KB（3 页） | 31% | 5 ms |
| 03-05 复盘 | 28 分钟超时 1.5 万次，连带下单失败 900 单 | — | — | — |

**注意它只被 20 QPS 打死了。** 因为「没有排序键定位」意味着每次查询要读**全部 granule** 的命中列：3,000 万行 × 3 列 × 20 B = 1.8 GB 原始 → 压缩后约 300 MB，**是 MySQL 路径的 6,250 倍**。如果再叠上「1,000 并发点查」的期待，算术见 §20.9 账三：那不是慢，是不可能的。

三起事故分别对应本篇三个支点：**mutation 重写（§20.6）**、**part 数爆炸（§20.5）**、**点查读放大与并发上限（§20.7、§20.9）**。

---

## 20.2 一句话定位与唯一真问题

**定位**：ClickHouse = **「不可变 part + 每列一个文件 + 稀疏主键（每 8,192 行一条）+ 向量化块执行」**的列式分析引擎。三个"不是"：不是 OLTP（无事务、无行级更新、无唯一约束）、不是 KV（点查要读一整块 granule）、不是强一致副本系统（副本异步复制）。

### 唯一真问题

> **列存用「连续 + 向量化 + 稀疏索引」把扫描吞吐买到极致，代价是「不可变的 part」——于是更新变成重写、点查变成读一整块 granule、事务与唯一约束几乎不存在；它不是「更快的 MySQL」，它是另一种形状的存储。**

这一句推出全部设计，每条都在后面有对应小节：

| 从这句话推出的约束 | 推出的设计 / 结局 | 在哪一节 |
| --- | --- | --- |
| 列文件不可变 ⇒ 「改一行」不存在 | 只有 `ALTER … UPDATE`（mutation：重写整段列文件）或「追加新版本 + 读时去重」 | §20.6 |
| 写入 = 生成新 part ⇒ part 数是写入**批数**的函数 | 必须批量写；单条插入会以「每秒批数」的速度堆 part | §20.5 |
| 稀疏索引只精确到 8,192 行 ⇒ 点查读放大 8,192× | 点查交给 B+ 树；排序键必须按过滤频率设计 | §20.9、§20.10 |
| 没有事务与唯一约束 ⇒ 不能做主库 | 主库留关系库，CH 只做「可重建的派生视图」 | §20.7 |
| 副本异步 ⇒ 写完立刻读另一个副本可能读不到 | `insert_quorum` / 同副本读 / 回源 | §20.8 |

### 解剖卡八问：本篇答哪几问

| 问 | 本篇的答案 |
| --- | --- |
| ① 它承诺什么 | 承诺：批量导入 + 大范围扫 + 聚合的吞吐（每秒数 GB 压缩数据）。**不承诺**：行级更新、事务、唯一约束、点查延迟、副本强一致、删除立刻还盘 |
| ② 数据长什么样 | 表 = 分区目录 + 若干不可变 part；part 内每列 `.bin` + `.mrk2`，加一份稀疏 `primary.idx`（每 `index_granularity`=8,192 行一条）→ §20.3 |
| ③ 一次读写走哪条路 | 读：`primary.idx` 二分 → `.mrk2` 取偏移 → `.bin` 读压缩块 → 向量化解码；写：内存排序 → 列编码压缩 → 新 part 目录 → 原子提交 → 后台 merge（§20.3、§20.5） |
| ④ 并发与一致 | 无锁、无事务、无 MVCC；一致性靠「part 一旦提交即不可变」；跨副本靠 Keeper 的复制日志，**异步**（§20.8） |
| ⑤ 扩展维度 | **分片 + 副本 + 批处理摊薄，三者都成立**：`Distributed` 表切分片、`ReplicatedMergeTree` 加副本、批越大单次写入的 part 开销越摊薄。垂直切不成立——列已经是独立文件，没有「再切」的粒度 |
| ⑥ 会在哪崩（只写系统级） | ① part 数爆炸 → **全库拒写**；② mutation 占满后台线程 → **写入与 merge 一起停摆**；③ 副本延迟窗口 → **读到的不是最新**；④ 高并发小查询 → **线程池打满，连报表一起排队**（§20.11、§20.12） |
| ⑦ 看哪几个指标 | `system.parts` 的 active 数（第一指标）、`system.mutations` 未完成数、`system.merges`、`query_log.read_bytes`、`system.replicas.absolute_delay`（§20.12） |
| ⑧ 换掉它的信号 | 需要事务 / 唯一约束 / 行级更新；点查占比 > 20%；单表常驻 part > 100；mutation 积压 > 50（§20.10） |

---

## 20.3 物理形态：part、列文件与 granule

### 一张表在磁盘上的四级结构

表目录 → 分区目录（`partition_id`）→ **part 目录**（`{min_block}_{max_block}_{level}`）→ **每列一组文件**。part 是**不可变**的：写入即封闭，之后只可能被 merge 整体替换，绝不会在原地改一个字节。

**图 1 · part → 列文件 → granule 的三级布局**

```text
/var/lib/clickhouse/data/db/dwd_order/
└── 202602_17_17_0/                     ← 分区目录（partition_id = 对分区键求值后的标识）
    ├── all_1_146_0/                    ← 一个 part：block 1..146，level 0（尚未被 merge 过）
    │   ├── tenant_id.bin  tenant_id.mrk2
    │   ├── created_at.bin created_at.mrk2
    │   ├── order_id.bin   order_id.mrk2
    │   ├── status.bin     status.mrk2      ← 每列三件套：值块(.bin) + 偏移(.mrk2) + 可选跳数索引
    │   ├── amount.bin     amount.mrk2
    │   ├── primary.idx                  ← 稀疏主键索引：每 8,192 行一条，不是每行一条
    │   ├── minmax_created_at.idx        ← part 级 min/max，供分区/part 裁剪
    │   ├── count.txt  columns.txt  checksums.txt
    │   └── skp_idx_idx_shop.idx / .mrk2 ← 跳数索引（若建了）
    └── all_147_210_1/                   ← 另一个 part（merge 过一次，level 1）
        └── …

一个 part 内、某一列的 granule 划分（index_granularity = 8,192 行，配置默认值）
   行号:   0 ──────── 8191 │ 8192 ──────── 16383 │ 16384 ───────
           granule 0      │ granule 1           │ granule 2  …
   primary.idx:   [k₀]  [k₁]  [k₂]  …              ← 每个 granule 的首行主键值，全局有序 → 可二分
   amount.mrk2:   (blk₁,off₀) (blk₁,off₁) (blk₂,off₀) …  ← 每个 granule 在 .bin 里的位置
   amount.bin:    [压缩块 1 = granule 0+1][压缩块 2 = granule 2]…      ← 压缩块大小自适应
```

三个量级要记住（后面所有算账都从这里来）：

| 结构 | 粒度 | 典型量级 | 它决定了什么 |
| --- | --- | --- | --- |
| `index_granularity` | 行 | **8,192 行/granule**（配置默认值） | 点查读放大、向量化的块大小 |
| `primary.idx` | granule | 每 granule 一条主键值；1 亿行 ≈ **1.2 万条** | 稀疏索引极小（KB–MB 级），只能粗定位到 granule |
| `.mrk2` | granule | 每 granule 一条（16 B 量级） | 「granule → 压缩块偏移」的映射，读 `.bin` 前必须先读它 |
| 压缩块 | 字节 | 自适应（受 `index_granularity_bytes` 影响，以目标版本 `system.merge_tree_settings` 为准） | 一次解压的单位；块越大压缩比越高、点查越亏 |

### 一次点查只读哪几个文件

**图 2 · `SELECT c1 FROM t WHERE k = 42` 的 IO 全景（t 有 20 列，排序键 = (k, ts)）**

```text
① t/primary.idx      读整块（KB–MB 级）→ 二分找到 k=42 落在 granule g
② t/k.mrk2           只读第 g 条 → 得到 granule g 在 k.bin 里的压缩块偏移
③ t/k.bin            只读 granule g 所在的那个压缩块 → 解压 → 校验 k=42 命中哪几行
④ t/c1.mrk2          只读第 g 条 → c1 列同一 granule 的块偏移
⑤ t/c1.bin           只读 granule g 的压缩块 → 解压 → 取对应行
────────────────────────────────────────────────────────────────────
✗ 其余 18 列的 .bin / .mrk2 一个字节都不读——列存的全部收益就在这一行
✗ 跳数索引用在 ①②之间的 part/granule 裁剪，只读它的 .idx（不是 .bin）
⚠ 即使只命中 1 行，③⑤ 也各读了一整个 granule：这是 §20.9 账二的分母
```

### 排序键（ORDER BY）与主键（PRIMARY KEY）不是一回事

这是全篇最常被误解的一条：**`ORDER BY` 决定数据在 `.bin` 里的物理顺序；`PRIMARY KEY` 只是 `ORDER BY` 的一个前缀，用来生成 `primary.idx`；两者都不是唯一约束。**

| 事实 | 直接后果 |
| --- | --- |
| `ORDER BY` 决定行在列文件里的顺序 | 它同时决定三件事：哪些查询能用稀疏索引二分到 granule；相邻值是否相似（→ 压缩比）；`LIMIT` 能否提前停 |
| `PRIMARY KEY` 必须是 `ORDER BY` 的前缀（不显式写时默认取全部/前缀） | 它只用来「每个 granule 存一条键值」；**重复键完全允许**，`primary.idx` 只保证 granule 之间有序 |
| 排序键里排在主键前缀之后的列 | 无法单独二分定位，只能靠跳数索引整段跳过（min/max、bloom 等） |
| 排序键之外的列（如 §20.1 事故三的 `user_id`） | 既不能定位也不能裁剪，**全表扫** |

```sql
-- ✅ 排序键 = 最常用的过滤前缀；主键只取前缀；跳数索引补「不在前缀里的列」
CREATE TABLE dwd_order
(
    tenant_id   UInt32,
    created_at  DateTime,
    order_id    UInt64,
    shop_id     UInt32,
    status      LowCardinality(String),
    amount      Decimal(18, 2),
    INDEX idx_shop   shop_id TYPE bloom_filter(0.01) GRANULARITY 4,  -- 每 4 个 granule 一组
    INDEX idx_amount amount  TYPE minmax           GRANULARITY 4
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(created_at)     -- 分区键只能表达「时间」这类可裁剪的粗粒度
ORDER BY (tenant_id, created_at)      -- 物理顺序：先 tenant，再时间
PRIMARY KEY tenant_id                 -- 稀疏索引只存 tenant_id（ORDER BY 的前缀）
SETTINGS index_granularity = 8192;    -- 每 8,192 行一个 granule（配置默认值）

-- ❌ 三个最常见的误建
-- ① ORDER BY (created_at) 而查询都是 WHERE tenant_id = ?   → 稀疏索引帮不上，每次全扫
-- ② 以为 PRIMARY KEY 会去重 → 重复值照收；去重只有 ReplacingMergeTree + 查询时 FINAL/argMax
-- ③ PARTITION BY order_id 或任何高基数表达式 → 分区数 = 基数 → part 数立刻爆炸
```

> 「最常用于过滤的列放最前面」这条纪律，与 B+ 树联合索引的最左前缀是**同一条物理事实**：**只有前缀有序，才能二分**。推导见 [04 索引的本质](04-index-essence-btree-page.md)，本篇只回指一句。

---

## 20.4 为什么快：四条可验证的原因

「因为列存」不是答案——列存只是把行切成了列。真正的四条收益各自有机制、有验证手段、也有明确的失效边界；任何一条不成立，「快」就当场消失。

| # | 原因 | 物理机制 | 怎么验证它真的生效 | 什么时候不成立 |
| --- | --- | --- | --- | --- |
| ① | **只读需要的列** | IO 量与「查询涉及的列数」成正比，不是「表的列数」；页/块里没有别的列要一起搬 | `query_log` 的 `read_bytes` ÷ 命中列字节；`ProfileEvents['SelectedBytes'] / ['ReadCompressedBytes']` | `SELECT *`、宽表 join、把列塞进 `ORDER BY`/`WHERE` 做排序键、JSON 一列装万物 |
| ② | **同列类型一致 → 压缩比高** | 列内定长、无跨列 padding、无行头；有序后游程/字典/delta 全部可用 | `system.columns` 的 `data_compressed_bytes / data_uncompressed_bytes`（字段以目标版本输出为准） | 高基数字符串（UUID/URL）、乱序写入、`SELECT *` 把压缩收益转成解压 CPU |
| ③ | **向量化执行 + 块处理** | 执行单位是 granule（8,192 行）；一列的值在内存里连续，SIMD 批量算，避免「逐行一次虚函数/一次分支」 | `max_threads` × `max_block_size`；同样的聚合换 `max_block_size` 看吞吐变化 | 单行点查（块里只有 1 行有用）、`LIMIT 1` 无过滤、表达式把列拆成逐行函数调用 |
| ④ | **稀疏索引 + 跳数索引** | `primary.idx` 二分把范围收窄到 granule；跳数索引（`minmax`/`set`/`bloom_filter`/`ngrambf`）跳过整段 granule 甚至整个 part | `query_log.read_rows` ÷ 表总行数；`EXPLAIN` 看 `Granules` / 跳数索引剪枝 | 高基数列上的 `minmax` 几乎无效；`bloom_filter` 对范围谓词无效；排序键与过滤列不一致 |

### 压缩比 3–10× 从哪来（只给来源与区间，不编分项拆解）

| 来源 | 机制 | 什么时候有效 | 纪律 |
| --- | --- | --- | --- |
| 列内类型一致 | 定长存放、无行头/无隐藏列/无跨列对齐 | 一直有效 | 把 JSON 塞进一列，这条立刻废掉 |
| 游程编码 | 排序后相同值相邻 | `ORDER BY` 前缀是低基数列（tenant/status/city） | 乱序写入拿不到 |
| 字典编码 | 低基数字符串 → 整数 + 字典 | 基数 < 百万级（显式声明 `LowCardinality` 才走） | UUID/URL 反而变大 |
| delta / XOR | 时间戳、单调计数器相邻差近似常数 | 时间序列、按时间排序 | 无序列即无效 |
| 通用 LZ4 / ZSTD | 上面几步剩下的熵 | 一直有效 | LZ4 快、ZSTD 更小但吃 CPU（可按列设 `CODEC(ZSTD(3))`） |
| **合计** | 以上相乘 | **3–10×，典型 5–7×** | **具体分项倍数取决于数据分布，公开资料没有可对账的拆解**；一律以 `system.parts` / `system.columns` 的实测为准 |

一句可以直接拿去评审的话：**列存的压缩比是「写入顺序」的函数，不是「引擎」的函数**。同一批数据乱序写进去，5–7× 会掉到 1.5–2×（推导在 `→ comp/02` §02.5）。

---

## 20.5 写路径：不可变 part 与 merge

### 一次 INSERT 的六步

1. 按 `ORDER BY` 在内存里给这批行排序（受 `max_insert_block_size` 切块）；
2. 按 `PARTITION BY` 切出目标分区；
3. 每列独立编码、压缩，写 `.bin` + 每 granule 的 `.mrk2`，同时写 `primary.idx`；
4. 写 `count.txt` / `columns.txt` / `checksums.txt` 等元数据；
5. 目录 **原子改名**成 `{min}_{max}_{level}` → 从这一瞬间起对查询可见（没有 refresh、没有 translog）；
6. 登记进后台 merge 队列；merge 把**同一分区内**的若干小 part 读出来、合并成一个更大的 part，然后原子替换。

**不可变性带来的是「读路径极简」**（没有 MVCC 版本链、没有 undo、没有行锁、没有可见性位图），**代价全部转移到写路径**：任何「改」都要生成新东西。

### 三条硬后果

**后果一：单条插入 = 一次 part 生成，part 数 = 写入批数。**

| 写法 | part 产生速率 | 一天 part 数 | 每 part 文件数（20 列） | merge 压力 |
| --- | --- | --- | --- | --- |
| 每秒 100 次单行 `INSERT` | **100 part/s** | 864 万 | ≈ 45（20×2 + 5） | 每秒要合并 100 个 1 行 part：纯元数据活，越合越多 |
| 每秒 1 次批量 1 万行 `INSERT` | **1 part/s** | 8.64 万 | ≈ 45 | 合并任务少 **100 倍**，每个 part 有 1 万行可摊 |

差 100 倍的 part 数、100 倍的文件 open/close、100 倍的后台任务——而**两个写的总行数一个是 100 行/秒、一个是 1 万行/秒**：批量写不但把 part 数打到 1/100，还顺带把吞吐打到 100 倍。这就是事故二的公式来源（14 part/s × 86,400 s = 120 万）。

正确开关只有两个：**客户端攒批**，或服务端 `async_insert = 1`（22.x 起提供，24.x 可生产使用，具体默认值以目标版本为准）让服务端替你攒。

**后果二：`ALTER … UPDATE/DELETE` 是 mutation，异步重写整个 part。** 单独一节讲（§20.6）。

**后果三：分区键选错 → part 数爆炸，而且是「不可合并的爆炸」。**

| 事实 | 后果 |
| --- | --- |
| merge 只在**同一分区内**进行 | 分区数 N 越大，每个分区分到的 merge 带宽 ≈ 总带宽 / N |
| 按天分区 + 3 年数据 = 1,095 个分区 | 每个分区每天若写 90 个小批 → 每天新增 **98,550 个 part**；merge 追不上就是几十万 active part |
| 小分区也有固定开销 | 每个分区至少 1 个 part（哪怕 1 行），每个 part 20 列 ≈ 41 个文件 → 1,095 × 41 ≈ **4.5 万文件**打底 |
| `parts_to_throw_insert` 按分区计 | 只要**一个**分区越线，写入就报 `TOO_MANY_PARTS`，整张表都不能再多写 |

判据只有一句：**让「每个分区的每批数据」都长到几百 MB–1 GB 量级，否则不要为它单独开分区**。按天切而每天只有几百万行的表，按 `toYYYYMM` 切；按天切而每天要写几十批的表，先修写入侧。

---

## 20.6 mutation：一次 UPDATE 为什么要重写整个 part

`ALTER TABLE … UPDATE`（以及 `DELETE`、`ALTER … UPDATE` 触发的列重写）在 MergeTree 里不是一个「改行」的动作，而是被登记成一条 **mutation**：后台线程池按 part 逐个重写，客户端**立刻**拿到 `Mutation id … started`，然后就可以走了——**这是最危险的一刻：你以为改完了，其实它排着队。**

**图 3 · 一条 mutation 重写一个 part 的时间轴**

```text
t₀  ALTER TABLE dwd_order UPDATE status='CANCELLED' WHERE order_id=12345678
     │  客户端立即收到 "started"（mutations_sync=0 的默认行为，异步）
     ▼  system.mutations: {mutation_id, parts_to_do = 38, is_done = 0}
t₀⁺  后台挑第一个 part——**不管这个 part 里到底有几行受影响**：
     ┌──────────────── part all_1_146_0（2.4 GB 压缩）────────────────────────┐
     │ 读 status.bin 全段（≈ 96 MB）+ 读 order_id.bin 全段（≈ 60 MB）            │
     │   ↓ 逐 granule 解压、逐行判定 → 命中 1 行 → 改值                          │
     │ 写新目录 all_1_146_1'/：status.bin 全段重写（≈ 96 MB）                    │
     │   其余未受影响列 hardlink 复用（不复制字节）                              │
     │ checksums.txt → 原子替换 → parts_to_do 减 1                              │
     └──────────────────────────────────────────────────────────────────────────┘
t₁ … 38 个 part 逐个（或少量并行）走完 → is_done = 1
     │ 期间：后台线程（background_pool_size）被 mutation 占住
     ▼       → 正常 merge 排队 → 新写入的 part 堆积 → active part 上涨 → 拒写
成本 = 触及的 part 数 × 每 part 受影响列的字节        ← 与「改了几行」无关
```

### 算账：100 GB 表改 1% 的行

**假设清单**：① 表 100 GB（压缩后）、20 列、30 个 part（每 part ≈ 3.33 GB）；② 更新 1% 的行共 800 万行，散布在全部 30 个 part；③ 被 `SET` 的列是 `status`，压缩后占全表体量 4%；④ 盘持续 500 MB/s；⑤ 读与写各算一次，不含副本复制的额外写。

| 步 | 推导 | 结果 |
| --- | --- | --- |
| 1 | 被波及的 part 数（1% 行散布全表） | **30 个 = 100%** |
| 2 | 需重写字节 = 100 GB × 4%（只重写被 SET 的列） | **4 GB** |
| 3 | IO 总量 = 4 GB 读 + 4 GB 写 | **8 GB** |
| 4 | 时间 = 8 GB / 500 MB/s | **16 s** |
| 5 | 若同时改 `status` + `updated_at` + `remark`（合计 25% 体量） | 50 GB IO → **100 s** |
| 6 | 若按「整 part 重写」的保守上界（`→ comp/02` 事故二口径）：30 × 3.33 GB × 2 | 200 GB → **400 s ≈ 6.7 min** |
| 7 | 改 100% 的行的成本 | 与第 1–6 步**完全相同** |
| 8 | 客服每分钟点 30 次 → 每分钟需要 30 × 16 s = 480 s 的重写工作 | **永久积压**（1 分钟只能干 60 s 的活） |

> **量级结论（非承诺值）**：一次改 1% 行的 mutation，代价在 **16 s – 6.7 min** 之间，取决于「被 SET 的列占总字节多少」与「未受影响列能否 hardlink 复用」；行数本身完全不影响这个数。假设清单见上；把「part 数」「列宽占比」「盘带宽」任一换掉请按步进表重算，不得直接引用。「整 part 重写」口径是 02 篇用的保守上界（当被 SET 的是宽列或一次改多列时接近它），能 hardlink 复用哪些列在版本间有差异，**以目标版本 `system.mutations.parts_to_do` 与实测 IO 为准**。

### 唯一的正确姿势：不要 UPDATE，要「重建」或「追加版本」

| 做法 | 物理动作 | 适用 | 代价 |
| --- | --- | --- | --- |
| 攒批 + 低峰执行一次 mutation | 排队可控，一次改完全部目标行 | 少量修数（如补一个字段） | 仍是重写；必须与 merge 错峰 |
| `ReplacingMergeTree` + 版本列 | 追加新版本行，后台 merge 时保留最大版本；**查询时 `FINAL`/`argMax` 才保证去重** | 状态频繁变更的宽表 | 读放大（多版本并存）；`FINAL` 贵 |
| 分区替换 `REPLACE PARTITION` | 离线算好正确分区 → 原子换入 | 大批量回填/纠错（最常用） | 需要一份离线算力；分区粒度决定改动范围 |
| 轻量删除 `DELETE FROM`（23.3 起） | 写 `_row_exists` 掩码列，读时过滤 | 删除率低 | 空间不还，等 merge；删除率一高就拖慢所有读 |

```sql
-- ① 反模式：交互式改状态（每条都排队重写 30 个 part，且与 merge 抢线程）
ALTER TABLE dwd_order UPDATE status = 'CANCELLED' WHERE order_id = 12345678;

-- ② 正解之一：ReplacingMergeTree —— 追加新版本，读时按版本取最新
CREATE TABLE order_state
(
    order_id   UInt64,
    ver        UInt64,                    -- 版本号（写入侧单调递增）
    status     LowCardinality(String),
    updated_at DateTime
)
ENGINE = ReplacingMergeTree(ver)          -- 同一 ORDER BY 键内保留 ver 最大的行
PARTITION BY toYYYYMM(updated_at)
ORDER BY order_id;

SELECT order_id, argMax(status, ver)      -- ✅ 正确：显式按版本聚合
FROM order_state WHERE order_id = 12345678 GROUP BY order_id;
SELECT * FROM order_state FINAL;          -- ✅ 正确但贵：读时全量去重
SELECT * FROM order_state;                -- ❌ 错：后台 merge 未及之前会返回多个版本

-- ③ 正解之二：分区替换（大批量纠错的标准姿势，先离线算准）
--    ALTER TABLE dwd_order REPLACE PARTITION 202602 FROM tmp_order_202602;

-- ④ 查有没有做完（唯一可信的判据）
SELECT mutation_id, parts_to_do, is_done, latest_fail_reason
FROM system.mutations WHERE table = 'dwd_order' AND is_done = 0;
```

---

## 20.7 为什么不能当主库：五条硬边界

这五条不是「还没做」，是**这个形态里不存在**。每条都指回 §20.3 的物理事实。

| # | 缺什么 | 物理原因 | 你会在哪里撞上 | 绕过方式（及其代价） |
| --- | --- | --- | --- | --- |
| ① | **事务** | 无多语句原子、无回滚、无隔离级别；提交单位是 part，一次 INSERT 一个 part | 「下单扣库存 + 写订单」这类跨表原子 | 主库做事务 + CDC 同步进 CH（多一条链路 + 延迟） |
| ② | **行级更新/删除** | 列文件不可变，改一行要重写整段列文件 | 订单状态机、库存水位、任何交互式改数 | `ReplacingMergeTree` 追加版本 / 分区替换（都要改业务写法） |
| ③ | **唯一约束** | `primary.idx` 只是稀疏排序索引，**允许重复值**；没有「插入冲突报错」这条路径 | 「不能重复下单」「幂等写入」 | 主库唯一索引 + 幂等键；CH 侧只做分析 |
| ④ | **副本强一致** | `ReplicatedMergeTree` 异步：一个副本写成功就返回，其余按 Keeper 复制日志拉取（§20.8） | 写完立刻读另一个副本 → 读不到 | `insert_quorum` / 同副本读 / 回源（见 §20.8 表） |
| ⑤ | **高并发小查询** | 每个查询按 `max_threads` 领线程；并发数 × 线程数 > 核数就排队 | 1,000 并发点查 | 点查交给 B+ 树 / Redis（§20.9 账三） |

### OLTP 需求 × ClickHouse 能否满足 × 替代方案

| OLTP 需求 | 能否满足 | 机制上的原因 | 替代方案 |
| --- | --- | --- | --- |
| 单行按主键读，QPS 1,000+ | ❌ | 稀疏索引只精确到 8,192 行，读放大 8,192×，且每次吃 8 个线程 | 关系库（3 页 ≈ 48 KB）/ Redis |
| 多表事务性写 | ❌ | 无事务、无回滚、无隔离级别 | MySQL / PostgreSQL |
| 唯一约束 / 幂等写入 | ❌ | `primary.idx` 允许重复；`ReplacingMergeTree` 只在 merge 后去重，**查询时不保证** | 主库唯一索引 |
| 行级 UPDATE / DELETE，秒级生效 | ❌ | 列文件不可变，改一行重写整段 | 主库改 + CDC / 分区替换 |
| 读己之写（强一致） | ⚠️ 需开关 | 副本异步拉复制日志 | `insert_quorum` + 同副本读 |
| 亿级明细聚合 < 1 s | ✅ | 列裁剪 + 向量化 + 5–7× 压缩 | — |
| 海量追加明细（埋点/日志） | ✅ | part 不可变 + merge 摊薄 | — |
| 秒级新鲜度报表（< 10 s） | ⚠️ 有条件 | 新 part 提交即可见，但高频小批会产生小 part | 攒批 + `async_insert` |
| 明细不改只过期 | ✅ | `TTL` 按分区/part 直接丢弃 | — |
| 跨分片 JOIN / 全局唯一序号 | ⚠️ 弱 | 分片间无事务，`GROUP BY` 要跨分片归并 | 关系库 / 全局 ID 服务 |

**一句话**：它可以是「主库的可重建派生视图」，不能是主库。这就是为什么 §20.1 事故一的正确修法是「主库改状态 + 分区替换刷新 CH」，而不是「把 mutation 调快一点」。

---

## 20.8 副本与「最终一致」：ReplicatedMergeTree 的延迟窗口

副本不是「同步复制」：`ReplicatedMergeTree` 用 ClickHouse Keeper（或 ZooKeeper）在 `/clickhouse/tables/{shard}/{db}/{table}` 下维护一份**复制日志**，每个副本在 `replicas/{replica}` 下有自己的队列。写入时**只有一个副本真正写了数据**，它把「我生成了哪个 part」登记进日志，其余副本的后台线程按队列去拉。

**图 4 · 一次写入的副本路径与延迟窗口**

```text
INSERT → 只发到副本 A
  副本 A：写本地 part → 写 checksums.txt → 在 Keeper 登记「新 part 名」
     │                                        （这就是复制日志的一条 entry）
     └─► 立即返回 "Ok"       ← insert_quorum = 0 时的默认行为：一个副本成功就算成功
                          │
   Keeper：/clickhouse/tables/{shard}/{db}/{table}/replicas/{A,B,C}/{log,queue,parts}
                          │  副本 B、C 的后台线程按队列拉
                          ▼
  副本 B：看到队列里的 entry → 从 A 拉 part 文件 → 校验 → 挂载
          └─ 这段延迟 = system.replicas 的 absolute_delay（秒）+ queue_size（条目数）

⚠️ 写入成功 ≠ 所有副本可读。路由到 B 的请求在延迟窗口内看到的是旧数据。
```

### 「读己之写」的补救表

| 补救做法 | 机制 | 代价 / 边界 |
| --- | --- | --- |
| **同副本读**（sticky 到刚写入的副本） | 读和写落在同一副本 | 需要客户端/网关记住「刚才写到哪」；该副本宕机即失效 |
| **`insert_quorum = 2`** | 写入等到指定数量的副本都拿到 part 才返回 | 写延迟 +一次跨机 RTT；并发插入下会互相等，`insert_quorum_parallel` 可缓解（版本与语义以目标版本文档为准） |
| **`select_sequential_consistency = 1`** | 查询只返回「所有副本都已有」的数据 | 读到的是滞后视图，只在配合 `insert_quorum` 时有意义 |
| **应用侧版本断言** | 写入返回版本号，读时校验 `≥` 该版本 | 把一致性责任交给业务代码 |
| **写完回源主库** | 主库是权威源 | 多一跳查询；适合「刚写完立刻展示」的窄场景 |

工程上还要记住两条：① `absolute_delay > 30 s` 通常意味着**某个副本在拉文件时卡住**（磁盘慢、`replication_queue` 报错），不是网络问题；② `is_readonly = 1` 表示该副本已拒绝接收写入（常见于 Keeper 会话丢失、磁盘满），此时读它仍然返回旧数据——**「能读」不等于「数据是新的」**。

---

## 20.9 三本账：只读需要的列、granule 放大与并发上限

**图 5 · 同一条查询，行存与列存各自读了哪些块**

```text
查询：SELECT c1, c7 FROM t WHERE k BETWEEN 100 AND 120   （1 亿行 · 20 列 · 行宽 400 B）

行存（B+ 树）——IO 单位是「页」，一页里 20 列全在
  叶子页 1   ████████████████████████████   20 列全部搬进 CPU，只用 c1/c7
  叶子页 2   ████████████████████████████
  …         全表扫 = 全部叶子页（40 GB）

列存（MergeTree）——IO 单位是「列文件里的 granule」
  k.bin    [granule g]  ▓▓▓     ← 定位用的那一段
  c1.bin   [granule g]  ▓▓▓     ← 只要的列
  c7.bin   [granule g]  ▓▓▓
  其余 17 个 .bin        ···     ← 一个字节都不读
  读字节 = 行数 × 命中列数 × 列宽 = 1 亿 × 2 × 20 B = 4 GB（压缩后 ≈ 0.7 GB）
```

### 账一 · 只读需要的列：40 GB → 6 GB → 1 GB

**假设清单**：① 1 亿行 × 20 列 × 每列平均 20 B；② 查询命中 3 列；③ 压缩比 6×（§20.4 的 3–10× 取偏保守值）；④ 盘持续 500 MB/s；⑤ 行存侧按未压缩页计（InnoDB 页压缩约 2× 另计，冷/热口径差见 `→ comp/02` §02.6）；⑥ 忽略 `primary.idx` 与 `.mrk2`（1% 量级）。

| 步 | 推导 | 结果 |
| --- | --- | --- |
| 1 | 原始体积 = 1 亿 × 20 列 × 20 B | **40 GB** |
| 2 | 行存扫全表：页里 20 列都在，只能整页读、整行搬进 CPU | 读 **40 GB** |
| 3 | 40 GB ÷ 500 MB/s | **80 s** |
| 4 | 列存只查 3 列 = 1 亿 × 3 × 20 B | **6 GB** 原始 |
| 5 | 压缩 6× → 6 GB ÷ 6 | **1 GB** |
| 6 | 1 GB ÷ 500 MB/s | **2 s** |
| 7 | 倍数 = 40 GB / 1 GB = 80 s / 2 s | **约 40×** |
| 8 | 若查 10 列：10/20 | 20 GB → 3.3 GB → **6.7 s**（仍有 12×） |
| 9 | 若 `SELECT *`（20 列）：只剩压缩这一条收益 | 40 GB → 6.7 GB → **13 s**（6×，向量化另算） |

> **量级结论（非承诺值）**：命中 3 列时 IO 少 40 倍、耗时少 40 倍；命中一半列时降到 12 倍；`SELECT *` 时只剩压缩的 6 倍。**「只读需要的列」这条收益与你查了几列严格成反比**，所以 `SELECT *` 不是「多读点没坏处」，是把这张表的立身之本关掉。换压缩比、换列数请按步进表重算。

换个假设自己复算（改 `PROJ` / `COMP` / `BW` 一行就够）：

```python
# comp/20 账一 + 账二的复算脚本：行存扫全表 vs 列存列裁剪 vs granule 放大
ROWS, COLS, COL_B, PROJ = 100_000_000, 20, 20, 3   # 1 亿行 × 20 列 × 20 B，查询命中 3 列
COMP, BW, GRAN, PAGE = 6, 500e6, 8192, 16384       # 压缩 6×、盘 500 MB/s、granule 8,192 行、页 16 KB

raw_full = ROWS * COLS * COL_B                     # 行存全表扫：20 列全搬
raw_proj = ROWS * PROJ * COL_B                     # 列存列裁剪：只搬 3 列
print(f"行存  = {raw_full/1e9:.0f} GB → {raw_full/BW:.0f} s")
print(f"列存  = {raw_proj/1e9:.0f} GB 原始 → {raw_proj/COMP/1e9:.1f} GB 压缩 → {raw_proj/COMP/BW:.1f} s")
print(f"倍数  = {raw_full/(raw_proj/COMP):.0f}×")

per_query = GRAN * PROJ * COL_B                    # 点查读一个 granule 的命中列
print(f"点查  = {per_query/1024:.0f} KB / 行 | 放大 {GRAN}× | InnoDB 对照 {3*PAGE/1024:.0f} KB")
```


### 账二 · granule 放大：读 1 行 = 读 8,192 行

| 步 | 推导 | 结果 |
| --- | --- | --- |
| 1 | 一行命中的 3 列 = 3 × 20 B | **60 B** |
| 2 | 读一个 granule = 8,192 行 × 60 B | **480 KB** |
| 3 | 读放大（字节比）= 480 KB / 60 B | **8,192×** |
| 4 | 再加 `primary.idx` 二分（KB 级）+ 2 条 `.mrk2`（16 B 级） | 量级不变 |
| 5 | 480 KB 顺序读 @ 500 MB/s | **≈ 1 ms** + 解压 + 线程调度 |
| 6 | InnoDB 一次主键点查 = 3 页 × 16 KB | **48 KB**，3 次页 IO ≈ 0.3–0.5 ms（页在池内则 μs 级） |
| 7 | 字节比 480 KB / 48 KB | **10×** |
| 8 | 但 IO **次数**：CH 是 1 次顺序读 + 解压，InnoDB 是 3 次随机读 | 单条延迟可能只差 2–3×，**并发一上来差距彻底反转**（账三） |

> **量级结论（非承诺值）**：一行的 60 B 换来 480 KB 的读，放大 8,192 倍；与 InnoDB 一次点查（3 页 = 48 KB）比，单条 IO 字节差 10 倍，但 CH 还要占一个查询线程、还要解压。口径差提醒：`→ comp/02` §02.8 按 25 B/列 算得 600 KB，本篇按 20 B/列 算得 480 KB，差 25%，结论不变。`index_granularity` 调小（如 4,096）可把放大减半，代价是 `primary.idx` 与 `.mrk2` 翻倍、压缩块变小压缩比下降。

### 账三 · 并发上限：`max_threads` 与并发数成反比

| 步 | 推导 | 结果 |
| --- | --- | --- |
| 1 | 单查询线程数 `max_threads`（默认取核数，此处按显式设 8 计） | 8 线程/查询 |
| 2 | 32 核 ÷ 8 线程 | **4 个查询并发即打满** |
| 3 | 第 5 个查询进来 → 每查询只分到 6 线程 → 延迟 ≈ ×1.33 | 8 个查询时每查询 4 线程 → 延迟 ≈ ×2 |
| 4 | 「支持 1,000 并发点查」需要的线程 = 1,000 × 8 | 8,000 线程 ÷ 32 核 = **250 台机器**（尚未计 IO 上限） |
| 5 | 同一需求在 InnoDB：一次点查占 1 个连接、3 页 IO | 1 台 32 核机 + 连接池即可，1,000 并发是常规水位 |
| 6 | 差距 | **约 2 个数量级**（250 台 vs 1 台） |

> **量级结论（非承诺值）**：列存的并发模型是「少数大查询各吃多线程」，不是「海量小查询各吃一个线程」。这台机器的**有效并发查询数 ≈ 核数 / max_threads**，把 `max_threads` 调小能提高并发数但每条变慢，乘积不变——**吞吐不会因为调参而增加**。

### 四本账一张表

| 账 | 结论数字 | 什么时候反噬 |
| --- | --- | --- |
| 只读需要的列 | 3 列 vs 20 列 → IO 40× | `SELECT *`、宽表 join、排序键外的列做过滤 |
| granule 放大 | 读 1 行 = 读 8,192 行 = 480 KB | 点查占比一高就撞上 |
| mutation 重写 | 改 1% 行 = 重写 30 个 part = 16 s–6.7 min | 任何交互式改数 |
| 并发上限 | 32 核 / 8 线程 = 4 并发 | 想拿它扛 C 端点查 |

---

## 20.10 架构边界：什么时候它是正确答案

| 它是最佳答案的场景 | 为什么 | 前置条件（不满足就先修） |
| --- | --- | --- |
| 埋点 / 日志 / 事件明细，只追加不改，按时间范围聚合 | 列裁剪 + 5–7× 压缩 + 分区裁剪三件套全对上 | 写入必须批量化（≥ 1 万行/批，或开 `async_insert`）；分区数受控 |
| 报表 / 看板：少数几张宽表的固定 `GROUP BY` | 向量化聚合 + 压缩后数据量小 | 查询列数 < 表列数一半；禁止 `SELECT *` |
| 大表 join 小表（`IN` 子查询 / `dictGet` / 广播） | 小表广播 + 向量化 | 大表 join 大表会触发 `max_memory_usage` |
| 秒级实时大屏 | 新 part 提交即可见，没有 refresh、没有 translog | 摄入侧攒批，part 数与 merge 可控 |
| 冷热分层 / 自动过期 | `TTL` 按分区或 part 直接丢弃，不产生 mutation | 分区键必须能表达时间 |

| 明确不该用它 | 因为 | 去哪 |
| --- | --- | --- |
| 订单 / 账户 / 库存主表 | 无事务、无唯一约束、无行级更新（§20.7） | 关系库，见 [eng/35 存储选型](../eng/35-storage-model-selection.md) |
| 按 ID 点查的在线接口 | 读放大 8,192×，每次吃多线程（§20.9） | B+ 树 / KV |
| 高频状态更新 | mutation 重写整段 part（§20.6） | 主库 + 分区替换 |
| 「读出唯一一条」的配置/维表 | 没有唯一约束 | 关系库 / KV |

**扩展维度怎么落**：`ReplicatedMergeTree`（本地表，每分片 N 副本）+ `Distributed`（分布式表，路由 + 扇出归并）是标准形态。**shard key 决定网络放大**：`GROUP BY` 的键与分片键一致时是本地聚合；不一致时每个分片的中间结果都要搬到查询节点再归并，代价 ≈ 分片数 × 中间结果大小。垂直切没有意义（列已经是独立文件），批处理摊薄是唯一免费的杠杆——**把批做大，同时摊薄 part 生成与 merge**。

**换掉它的信号（第⑧问）**：① 需要跨越两个表的事务或唯一约束；② 点查占比 > 20%；③ 单表常驻 active part > 100，或单分区 part 接近 `parts_to_throw_insert` 的七成；④ mutation 积压 > 50 条或最老一条 > 10 分钟；⑤ 需要「写入后立刻读到」且 `insert_quorum` 的延迟不可接受。

---

## 20.11 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到物理形态） | 怎么定位 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 用 CH 存订单主表然后 `UPDATE` 状态，mutation 排队几小时 | 列文件不可变，`ALTER UPDATE` = mutation，按 part 重写；且与 merge 抢同一批后台线程 | `system.mutations WHERE is_done=0` 的 `parts_to_do`、`create_time`；`system.merges` 里 `is_mutation` 的占比 | 停交互式 UPDATE；改 `ReplacingMergeTree` 或「离线重算 + 分区替换」；必要时 `KILL MUTATION` 止血 | 明细/主表禁止交互式 UPDATE，写进建表评审 |
| 采集服务逐条 insert，一天几十万个 part，报 `Too many parts` | 一次 INSERT 一个不可变 part；part 数 = 写入批数；merge 追不上 | `system.parts WHERE active` 的 `count()`；`system.merges` 队列长度 | 客户端攒批；服务端 `async_insert=1`；临时调大 `parts_to_throw_insert`（只买时间） | 写入侧硬约束「批行数下限 + 批间隔上限」，监控 active part 数 |
| `SELECT *` 把查询打爆内存 / `MEMORY_LIMIT_EXCEEDED` | 20 列全解压 + 中间结果按块放大；列裁剪这条收益被关掉 | `query_log` 的 `memory_usage`、`read_bytes`；与「只取 3 列」的同查询对比 | 显式列名；调小 `max_block_size`；对宽表用投影/物化视图 | Code Review 禁 `SELECT *`；`max_memory_usage` 设成单查询可接受的硬上限 |
| 分区键按天切，同时有 3 年数据 → 几十万个 part | merge 只在同分区内；分区数越多每分区分到的 merge 带宽越少；`parts_to_throw_insert` 按分区计 | `SELECT partition, count() FROM system.parts WHERE active GROUP BY partition` 看长尾 | 把分区粒度改成月；`OPTIMIZE` 手动合并热点分区；`DROP PARTITION` 清历史 | 建表时算「每分区每天写入批数 × 分区数」，落在 merge 能力之内 |
| 把 `ReplacingMergeTree` 当唯一约束，`SELECT … FINAL` 越来越慢 | 去重只在后台 merge 时发生，查询不加 `FINAL`/`argMax` 就会看到多版本；`FINAL` 要按 ORDER BY 全量读 | 对比 `count()` 与 `count() … FINAL`；`system.parts` 的 part 数（未合并 = 多版本） | 查询改成 `argMax(col, ver) … GROUP BY key`；定期 `OPTIMIZE … FINAL`（重操作） | 唯一性归主库；CH 侧只做可重建的派生视图 |
| 高并发点查打到 CH，CPU 打满、连报表一起排队 | 每查询吃 `max_threads` 个线程，`并发数 ≈ 核数 / max_threads` | `system.processes` 的并发数 × `max_threads`；`query_log` 里点查的 `read_bytes` | 点查回切关系库/Redis；CH 只留分析查询；设 `max_concurrent_queries` 隔离 | 按访问形态分流，不要「一份存储打天下」 |
| `ORDER BY` 与常用过滤列不一致，查询全扫、`read_rows` ≈ 全表 | 稀疏索引只能二分「排序键前缀」；前缀之外的列既不能定位也不能裁剪 | `query_log.read_rows` ÷ 表 `total_rows` ≈ 1 时必是全扫 | 重建表把过滤频率最高的列移到 `ORDER BY` 最前；给次常用列建 `bloom_filter`/`minmax` 跳数索引 | 建表前先列出 Top 5 查询，`ORDER BY` 从它们反推 |

一条贯穿七行的结论：**这七种坑的现象几乎一样（慢 / 满 / 拒写），但根因分别落在「不可变 part」「part 数 = 批数」「列没裁剪」「分区粒度」「去重只在 merge」「线程按查询分配」「排序键没对上」——只有画出 §20.3 那张物理图才分得开。**

---

## 20.12 观测：指标、拐点与告警

每条都带版本与开关前提（纪律 7）。**不确定的字段名一律写「以目标版本 system 表输出为准」，不背字段。**

| 指标 | 版本 / 开关前提 | 拐点（越线就动手） | 对应机制 |
| --- | --- | --- | --- |
| `system.parts` 的 `active = 1` 计数与 `sum(bytes_on_disk)` | CH 24.x，默认可查；只覆盖**本地表**，`Distributed` 表要逐分片查 | **单表 active part > 100**；单分区 > `parts_to_throw_insert` 的 70% | part 爆炸（§20.5） |
| `system.mutations` 的 `is_done = 0` 计数、最老 `create_time`、`parts_to_do` | 24.x；轻量删除（`DELETE FROM`，23.3 起）**不进** `system.mutations`，要看 `system.parts` 里的 `_row_exists` 掩码 | 积压 > 50，或最老一条 > 10 min | mutation 重写（§20.6） |
| `system.merges`（进行中的 merge 列表；`is_mutation` 区分是合并还是 mutation） | 24.x；列名以目标版本输出为准 | 单个 merge > 10 min，或并发 merge 数长期贴住 `background_pool_size` | merge 与 mutation 抢线程 |
| `system.query_log` 的 `read_bytes` / `read_rows` / `query_duration_ms` / `memory_usage` | **需要 `log_queries = 1`（默认开）**；24.x 起可用 `system.query_views_log` 看子查询 | 单查询 `memory_usage` 接近 `max_memory_usage`；`read_rows` ÷ 表行数 ≈ 1（全扫） | 列裁剪失效 / 排序键不匹配 |
| `ProfileEvents['SelectedBytes']` 与 `ProfileEvents['ReadCompressedBytes']`（在 `query_log.ProfileEvents` 这个 Map 列里） | 24.x；字段名与键名以目标版本 `query_log` 输出为准 | 比值接近「查询列数 / 表列数」= 列裁剪生效；接近 1 = 在读整行 | 判断「只读了几列」是否真的生效 |
| `system.replicas` 的 `absolute_delay` / `queue_size` / `is_readonly` | 仅 `ReplicatedMergeTree` 有值；单机表为空 | `absolute_delay` > 30 s；`queue_size` 持续 > 100；`is_readonly = 1` | 副本延迟窗口（§20.8） |
| `system.errors` / `system.events` 中的写入拒绝类计数（`TOO_MANY_PARTS` 等） | 24.x；事件名与错误码以目标版本输出为准 | 任意非零增长即告警 | 写入已被拒（§20.1 事故二） |

巡检骨架（可直接跑；`system.parts` 与 `system.query_log` 是每天必须看的两张表）：

```sql
-- ① 第一指标：part 数与体量
SELECT database, table, count() AS active_parts,
       formatReadableSize(sum(bytes_on_disk)) AS size, max(level) AS max_level
FROM system.parts WHERE active
GROUP BY database, table ORDER BY active_parts DESC LIMIT 10;

-- ② 分区维度的 part 分布（阈值是按「单分区」算的，所以必须看长尾）
SELECT table, partition, count() AS parts
FROM system.parts WHERE active
GROUP BY table, partition ORDER BY parts DESC LIMIT 10;

-- ③ mutation 积压：有没有做完、卡在哪一步
SELECT table, mutation_id, parts_to_do, is_done, latest_fail_reason, create_time
FROM system.mutations WHERE is_done = 0 ORDER BY create_time LIMIT 10;

-- ④ 列裁剪是否真的生效：看单查询读了多少字节、多少行
SELECT query_duration_ms, read_rows, formatReadableSize(read_bytes) AS rb,
       ProfileEvents['SelectedBytes']     AS selected_bytes,
       ProfileEvents['ReadCompressedBytes'] AS compressed_bytes
FROM system.query_log
WHERE type = 'QueryFinish' AND event_time > now() - INTERVAL 10 MINUTE
ORDER BY read_bytes DESC LIMIT 10;

-- ⑤ 副本延迟（只在 ReplicatedMergeTree 上有意义）
SELECT database, table, absolute_delay, queue_size, is_readonly
FROM system.replicas WHERE absolute_delay > 30 OR is_readonly = 1;
```

告警表达式骨架（阈值取自本节拐点列；指标名以你所用 exporter 的版本输出为准）：

```text
# part 数：单表 100 以上且持续 5 分钟 → merge 已经追不上写入
max by (table) (clickhouse_parts_active) > 100
# mutation：未完成超过 50 条 → 有人在对明细表做交互式更新
max(clickhouse_mutations_unfinished) > 50
# 副本：延迟 30 s 以上 → 该副本已退出可读的「新鲜」集合
max by (replica) (clickhouse_replicas_absolute_delay) > 30
```

---

## 20.13 一页速记

| 记什么 | 具体数字 / 结论 |
| --- | --- |
| 物理四级结构 | 表目录 → 分区目录 → 不可变 part → 每列 `.bin` + `.mrk2` + 稀疏 `primary.idx` |
| granule | `index_granularity` 默认 8,192 行一个；`primary.idx` 每 granule 一条；点查读放大 8,192× |
| 排序键 vs 主键 | `ORDER BY` 定物理顺序；`PRIMARY KEY` 只是它的前缀，用来生成稀疏索引；**都不是唯一约束** |
| 四条快的原因 | 只读需要的列（IO ∝ 列数）、同列同类型压缩 3–10×、向量化按 8,192 行块、稀疏 + 跳数索引跳段 |
| part 不可变 | 一次 INSERT 一个 part；part 数 = 写入**批数**；单条插入 100 part/s → 一天 864 万 part |
| merge 边界 | merge 只在**同一分区内**；分区越多，每分区分到的 merge 带宽越少 |
| mutation | `ALTER UPDATE` = 异步重写触及的整段列文件；成本 = 触及 part 数 × 受影响列字节，**与改了几行无关** |
| 五条硬边界 | 无事务 / 无行级更新 / 无唯一约束 / 副本异步 / 高并发小查询差 |
| 副本 | 一个副本写成功即返回（`insert_quorum=0`）；其余按 Keeper 复制日志拉取 → 有延迟窗口 |
| 三笔账 | 列裁剪 40×、granule 放大 8,192×（vs InnoDB 48 KB）、并发上限 ≈ 核数 / `max_threads` |
| 第一指标 | `system.parts` 的 active part 数；第二是 `system.mutations` 的未完成数 |
| 换零件信号 | 要事务/唯一约束/行级更新；点查占比 > 20%；part 常驻 > 100；mutation 积压 > 50 |

**如果只记五句：**

1. **列存的快是「列连续 + 向量化 + 稀疏索引」三件事叠出来的，不是「因为列存」；`SELECT *` 等于把第一件关掉。**
2. **part 不可变是这个形态的宪法**：写是新 part，改是重写 part，删是打标记——于是所有「改一行」的请求都以 part 为单位付钱。
3. **part 数是写入批数的函数**，不是数据量的函数；单条插入把每秒的请求数变成了每秒的 part 数。
4. **稀疏索引只精确到 8,192 行**，所以点查要读一整块 granule；`ORDER BY` 里没有的列，既不能定位也不能裁剪。
5. **它不是更快的 MySQL**：它是「不可变 part + 异步副本」的分析引擎；主库的位置留给有事务和唯一约束的那一个。

---

## 20.14 自检题（5 题）

**Q1.** 一张 100 GB（压缩后）、20 列、30 个 part 的明细表，要 `ALTER UPDATE` 改 1%（800 万行）的 `status` 列。算出 mutation 的 IO 与耗时，并解释为什么「改 1%」与「改 100%」的成本几乎一样。

<details><summary>答案</summary>

**为什么一样**：mutation 的重写单位是 **part**，不是行。1% 的行散布在全部 30 个 part 里 → 30 个 part 全部被选中 → 每个被选中的 part 都要把**受影响列的整个列文件**重写一遍。命中 1 行还是命中 100 万行，对「重写这个 part 的 `status.bin`」这个动作没有任何影响。

**算术**（假设 `status` 占全表体量 4%、盘 500 MB/s）：

1. 被波及 part 数 = **30**（100%）；2. 重写字节 = 100 GB × 4% = **4 GB**；3. IO = 4 GB 读 + 4 GB 写 = **8 GB**；4. 时间 = 8 GB ÷ 500 MB/s = **16 s**。
5. 若同时改 `status` + `updated_at` + `remark`（25% 体量）→ 50 GB IO → **100 s**。
6. 保守上界（整 part 重写，`→ comp/02` 事故二口径）→ 200 GB → **≈ 6.7 min**。

**推论**：这不是配置问题。正确做法是 `ReplacingMergeTree` + `argMax`、或离线重算 + `REPLACE PARTITION`；mutation 只用于低峰期的一次性修数。量级结论，换列宽占比/part 数请重算。
</details>

**Q2.** 一条主键点查只返回 1 行（60 B），为什么实际读了 480 KB？给出三种「让点查变快」的做法及其代价。

<details><summary>答案</summary>

480 KB = 8,192 行（一个 granule）× 3 个命中列 × 20 B。稀疏索引只精确到 granule 边界，粒内没有细粒度定位；而且 `.bin` 里同 granule 的值通常落在同一批压缩块里，读就整块读、解压就整块解压。

三种做法：① **调小 `index_granularity`**（如 8,192 → 2,048）→ 放大降到 1/4，但 `primary.idx` 与 `.mrk2` 条目 ×4、压缩块变小导致压缩比下降 1.5–2×、`system.parts` 体积上涨；② **改排序键**把点查列放到 `ORDER BY` 最前 → 从「扫全表 granule」变成「二分到 1 个 granule」，但排序键一改就要整表重建，且会牺牲原有排序键带来的压缩与范围扫收益；③ **把点查搬走**到 B+ 树/KV（正解）→ CH 只承载分析查询。

结论：前两种都是「把 8,192 降到 2,048」，降 4 倍仍然远差于 B+ 树的 3 页 48 KB。**点查密集不是调参能救的，是形态不匹配。**
</details>

**Q3.** 一台机器上 `system.parts WHERE active` 从 40 涨到 300，你怎么在五分钟内判断是「写入批太小」还是「分区太多」？

<details><summary>答案</summary>

看两个维度：① `SELECT partition, count() FROM system.parts WHERE active GROUP BY partition ORDER BY 2 DESC` —— 若 part **均匀分布在大量分区**上且每个分区只有几个，则是**分区太多**（merge 只在同分区内，每分区拿到的合并带宽被摊薄）；若少数分区各背着几十上百个 part，则是**写入批太小**。② 对照写入侧指标：`INSERT` 的次数 ÷ 时间 = part 产生速率；若 ≈ 业务请求数，说明每次请求一个 part（单条插入）。

修法完全不同：分区太多 → 改粗分区粒度（天 → 月）、`DROP PARTITION` 清历史；批太小 → 客户端攒批或 `async_insert=1`。**调大 `parts_to_throw_insert` 只是买时间，会让后面的拒写来得更猛**（它不改变 merge 速率）。
</details>

**Q4.** 3 副本 `ReplicatedMergeTree`，`insert_quorum=0`。刚写入一条数据，客户端立刻去查另一个副本，查不到。给出三种补救及其代价，并说明 `absolute_delay` 在这里的作用。

<details><summary>答案</summary>

机制：写入只落到一个副本，它在 Keeper 登记一条复制日志后立刻返回；其余副本后台按队列拉 part 文件。窗口期内读其他副本 = 读旧数据。

三种补救：① **同副本读**（sticky 路由到刚写入的副本）——零额外延迟，但该副本故障即失效，需要网关/客户端记录「写到哪」；② **`insert_quorum = 2`**（+ 必要时 `select_sequential_consistency=1`）——写入等多数副本确认，代价是 +1 次跨机 RTT，并发插入下会互相等待（`insert_quorum_parallel` 可缓解，以目标版本语义为准）；③ **写完回源主库**——最稳，多一跳查询，适合「刚写完要立刻展示」的窄场景。

`absolute_delay` 是「本副本落后最新数据的秒数」：它 > 0 就说明存在窗口，> 30 s 通常意味着拉文件卡住（磁盘慢 / 队列报错），此时该副本仍然**能读**、只是**数据旧**——「能读」不等于「数据是新的」。
</details>

**Q5.** 有人提议：「用 ClickHouse 存订单主表，它写入吞吐这么高，正好扛 1 万 QPS 的状态更新。」用五条硬边界反驳，并给出正确架构。

<details><summary>答案</summary>

逐条反驳：① **无事务** —— 「扣库存 + 写订单 + 记流水」跨表无原子性，任何一步失败都留脏数据；② **无行级更新** —— 每次状态变更都是一条 mutation，按 part 重写；1 万 QPS 意味着每秒 1 万条 mutation 排队，而一条 mutation 就要 16 s–6.7 min（Q1），差距是 10⁵ 量级；③ **无唯一约束** —— `primary.idx` 允许重复，幂等只能靠 `ReplacingMergeTree` 最终去重，查询不加 `FINAL`/`argMax` 就会读到多版本；④ **副本异步** —— 写完立刻读另一个副本读不到，「下单后马上查订单」会出现「不存在」；⑤ **高并发小查询差** —— 每次点查读 480 KB、吃 8 个线程，1 万 QPS 需要 10⁵ 级线程。

正确架构：**关系库做主库**（事务 + 唯一索引 + 行级更新）→ **CDC / Outbox 把变更流进 ClickHouse**（只追加的事实行，天然匹配 part）→ 报表在 CH 侧算，纠错走「离线重算 + `REPLACE PARTITION`」而不是 `UPDATE`。CH 在这里是**可重建的派生视图**，不是权威源。选型细节见 [eng/35 存储选型](../eng/35-storage-model-selection.md)。
</details>

---

## 20.15 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 列存的通用机制：列连续、min/max 与跳数索引、向量化、删除标记 + 后台 merge；以及三形态的三本账公式（本篇只写 ClickHouse 的具体形态，不重复机制） | [02 存储引擎三形态](02-storage-engine-three-shapes.md) §02.5、§02.6 |
| 行存一次点查的成本基准：16 KB 页、3 次页访问、行足迹与隐藏列（本篇 §20.9 账二的对照值） | [05 InnoDB 的页与行](05-innodb-page-and-row.md) §05.3、§05.10 |
| 「只有前缀有序才能二分」：B+ 树联合索引的最左前缀，与 MergeTree 排序键同构 | [04 索引的本质](04-index-essence-btree-page.md) |
| 存储模型选型：关系库 / KV / 文档 / 宽列 / 列式的边界表与换挡判据（**选型一律去这里**） | [eng/35 存储选型](../eng/35-storage-model-selection.md) |
| 本篇所依赖的尺子：解剖卡八问、三本放大账、版本与指标纪律 | [0-summary](0-summary.md) §0.4、§0.7 |
| 不可变段与 merge 的另一种形态：ES 的段生命周期（「可见性靠新开文件」与 part 同源） | `→ comp/22` |
| 倒排索引的物理结构：词典 + 有序倒排表 + 跳表（另一条「有序换扫描」的路） | `→ comp/21` |
| LSM 侧的对照：part 与 SST 都不可变，但 compaction 的写放大形状不同 | `→ comp/19`、`→ comp/18` |
| 数据湖表格式：把「不可变文件 + 元数据快照」搬到对象存储上（COW vs MOR 是同一笔账的变体） | `→ comp/35` |
| 组件故障谱：列存最常见的三种系统级失效是「满（part 爆炸）」「慢（mutation 排队）」「语义漂移（副本延迟窗口）」 | `→ comp/52` |
