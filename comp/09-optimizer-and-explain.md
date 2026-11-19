# 09. 优化器与执行计划：索引为什么会「失效」

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · 关系库与事务内核 ｜ **编号**：09 / 53
> **本篇定位**：Part II 的第五篇。05 讲页与行、06 讲版本链、07 讲锁、08 讲三份日志，四篇都是「数据的物理形状」；本篇是这一组里唯一的**决策层**：形状已经摆在那儿了，**是优化器在决定用不用它**。它不重写 B+ 树怎么切（→ `comp/04`）、不重写页怎么排（→ `comp/05`）、不重写索引下推与 MRR 的机制（→ `comp/04` §04.7），只回答一件事：**优化器用什么数字、算哪两个式子、在什么情况下会把「有索引」算成「不如全扫」，以及你怎么在 EXPLAIN 里当场抓到它算错的证据**。读完这一篇，你不再问「为什么索引失效」——你会问「它把 `rows` 估成了多少、把 `io_block_read_cost` 当成了多少」。
> **读法**：§09.3 的两个式子 + §09.9 的阈值算账是全篇支点，那张「优化器的 p\* 10%~26% vs 物理的 p\* 0.5%~7%」的对照表建议手抄；§09.5 的 `key_len` 反推与 `Extra` 处理表可直接贴在评审白板上；§09.8 的十条各给证据与修法，出事时按 `type`/`key`/`key_len`/`rows`/`filtered`/`Extra` 六个读数逐条对号；被追问「为什么要 ANALYZE」读 §09.4，被追问「为什么加了索引更慢」读 §09.9。
> **不在本篇**：B+ 树结构、最左前缀的形式化判定、覆盖索引与 ICP/MRR 的机制 → `comp/04`；页/行格式与行足迹 → `comp/05`；锁等待与死锁 → `comp/07`；redo/binlog 的提交顺序 → `comp/08`；单表极限与分库分表拐点 → [eng/36](../eng/36-mysql-limits-sharding.md) §36.4.1；存储与索引形态选型 → [architect/03](../architect/03-storage-and-index-shape.md)。

---

## 09.1 引子：三起「索引就在那儿，优化器就是不用」的故障

优化器的事故有一个统一长相：**没有报错、SQL 完全合法、`SHOW INDEX` 里索引明明在、`EXPLAIN` 甚至走了索引**。变的是三个看不见的量——**它以为表有多大、它以为一个条件能过滤掉多少、它以为一次随机读值多少钱**。三个量都在 `EXPLAIN` 的输出里，只是没人把那一列当数字看。

### 事故一：导入 1.02 亿行历史订单，第二天所有订单查询从 20 ms 变 4.1 s（2025-09-10 → 09-11，某电商订单中心）

`t_order` 原本 102 万行（近 30 天数据，老数据在归档表）。为做「全周期履约分析」，运维在 09-10 深夜用单事务 `LOAD DATA` 把 2023–2025 的 1.01 亿行历史订单并进同一张表，共 1.02 亿行。

| 时间 | 事件 | 真实行数 | `innodb_table_stats.n_rows` | `EXPLAIN` 的 `rows`（`status='REFUND'`） | 订单列表 P99 |
| --- | --- | --- | --- | --- | --- |
| 09-10 22:00 | 导入前基线 | 102 万 | 102 万 | 255,000 | 20 ms |
| 09-11 02:40 | 导入完成（单事务；`auto_recalc` 异步，队列里排着） | 1.02 亿 | 102 万（`last_update` 停在 09-01） | 255,000 | 22 ms |
| 09-11 07:00 | 早高峰，`range idx_status` 回表 61.2 万行 | 1.02 亿 | 102 万 | 255,000 | **4,100 ms** |
| 09-11 09:10 | DBA 执行 `ANALYZE TABLE`（20 页采样，4 个不同值被均分） | 1.02 亿 | 1.02 亿 | **25,500,000** | **28,000 ms**（计划翻成 `ALL`，更糟） |
| 09-11 09:40 | 加 `FORCE INDEX` 强行回到索引 | 1.02 亿 | 1.02 亿 | 25,500,000 | 4,100 ms |
| 09-11 10:05 | 建直方图 `WITH 100 BUCKETS` + 覆盖索引 `(status, id, created_at)` | 1.02 亿 | 1.02 亿 | **612,000** | 180 ms |
| 09-11 复盘 | 超时请求 4.2 万次，订单页 503 占比 4.1%，损失约 74 万元 | — | — | — | — |

`status='REFUND'` 的真实行数是 612,000（0.6%）。`ANALYZE` 之前优化器估 255,000，之后估 25,500,000——**同一个查询、同一份数据，`rows` 估算差 100 倍，两次都错，方向相反**：前者把 0.6% 的选择性当成 25%（于是以为该走索引），后者把它当成 25%（于是以为该全扫）。中间那个 09:10 的动作是全场最贵的：**一次「标准操作」把 P99 从 4.1 s 推到 28 s**，因为它把 `n_rows` 从错的 102 万修正成了对的 1.02 亿，而 `n_diff` 仍然是「4 个值均分」这个错假设。

### 事故二：一次上线给 `created_at` 包了个 `DATE()`，DB CPU 打到 95%（2025-06-24，某 SaaS 报表）

报表 v1 的写法是 `WHERE created_at >= ? AND created_at < ?`（`idx_created_at` 完美命中，8,600 万行表上 210 ms）。v2 为了「前端只传一天、少传一个参数」，改成 `WHERE DATE(created_at) = ?`。代码评审看到的是「逻辑等价」，测试环境 40 万行跑 30 ms，压测脚本里这条 SQL 占比 0.3%，所以压测也没发现。

| 时间 | 事件 | DB CPU | 报表接口 P99 | 订单接口 P99 |
| --- | --- | --- | --- | --- |
| 06-24 14:02 | 发布报表 v2（`WHERE DATE(created_at) = ?`） | 38% | 210 ms | 62 ms |
| 06-24 14:10 | 报表页 20 并发同时刷新，每条 SQL 全扫 8,600 万行 | 61% | 1,900 ms | 65 ms |
| 06-24 14:26 | 20 个全扫各占一个核，buffer pool 被顺序扫冲掉 | 82% | 4,800 ms | 380 ms |
| 06-24 14:40 | 主库 32 核打满，连接池排队，订单接口开始超时 | **95%** | 9,600 ms | **1,420 ms** |
| 06-24 14:52 | 回滚到区间写法 | 74% | 1,200 ms | 420 ms |
| 06-24 15:05 | 全扫退出，缓存重新热起来 | 41% | 210 ms | 63 ms |
| 06-24 复盘 | 22 分钟，订单接口超时 3.1 万次，赔付 18 万元 | — | — | — |

`DATE(created_at)` 让索引的有序性直接报废（§09.8 第 2 条）：树是按 `created_at` 的**原值**排序的，函数之后的值没有任何有序结构可定位，`key` 变 `NULL`、`type` 变 `ALL`。8,600 万行 × 20 并发 = 每轮 17 亿行的扫描量，而且**全表扫会顺手把 buffer pool 的热点页全冲掉**——所以它伤的不只是这条 SQL，是整个实例（这也解释了为什么订单接口 P99 会从 62 ms 涨到 1,420 ms）。

### 事故三：join 驱动表选反，接口 P99 从 80 ms 到 12 s（2025-11-20，某本地生活平台）

运营后台新加一个查询：`SELECT o.* FROM t_user u JOIN t_order o ON o.user_id = u.id WHERE u.city='杭州' AND o.amount > 500`。`t_user` 200 万行、`t_order` 从 300 万行涨到 1,000 万行（统计没刷新），`idx_city` 与 `idx_user_id` 都在。

| 时间 | 事件 | `EXPLAIN` 第一行 | 优化器估的中间结果 | P99 |
| --- | --- | --- | --- | --- |
| 11-20 10:12 | 查询上线 | `t_order` `type=ALL` `rows=3,000,000` | 300 万行 | 80 ms |
| 11-20 10:30 | 前台开始用它做「城市运营榜」，QPS 12 | 同上 | 同上 | **12,000 ms** |
| 11-20 11:40 | `EXPLAIN ANALYZE`：内层主键探测 `loops=10,000,000` | 同上 | 真实 1,000 万行 | 12,000 ms |
| 11-20 14:00 | `ANALYZE` + `idx_city(city)` 重建 | `t_user` `type=ref` `rows=20,000` | 10 万行 | **400 ms** |
| 11-20 复盘 | 运营榜卡片超时，用户侧无感但后台工单 620 张 | — | — | — |

20 页采样把「杭州 2 万用户（1%）」估成了 80 万（40%），于是优化器认为「先过滤 `t_user`」的代价和「全扫 `t_order`」差不多，而后者它还以为是 300 万行——**两个错误假设叠在一起，驱动表就翻了**。代价是内层探测被执行了 1,000 万次而不是 2 万次：中间结果 100 倍、耗时 30 倍。

三次事故对应本篇三条主线：**统计信息（§09.4）**、**成本模型与谓词形状（§09.3、§09.8）**、**join 顺序（§09.7）**。三条都在 §09.9 的算账里汇总成一句话：**优化器从不「判断能不能用索引」，它只比较两个数字。**

---

## 09.2 一句话定位与唯一真问题

**定位**：优化器是数据库里唯一一个**不看数据、只看摘要**的组件。它拿到的是「表大概多少行、某列大概多少个不同值、某个范围大概占几页」这三类摘要，用一组**写死在配置文件里的成本常数**把它们折算成「代价」，然后挑代价最小的执行路径。三个「不是」：它不是解释器（不会为了让你看懂而选慢的计划）；它不是自适应系统（选完就执行，中途不改主意，除非你开 8.0 的 `EXPLAIN ANALYZE` 手工看）；它不承诺最优（它承诺的只是「在它的模型里最便宜」）。

### 唯一真问题

> **索引「失效」九成不是没建索引，而是优化器算出来全表扫更便宜——不懂它的成本模型与统计信息，你只能靠猜和加 hint。**

拆开这句话的五个后果：

1. **「失效」是个误导词。** 索引一直有效，只是**没有被选中**。区分这两件事是诊断的第一步：`EXPLAIN` 的 `possible_keys` 里有它、`key` 里没有它，说明「能用但没选」；`type=ALL` 且 `possible_keys` 也为 `NULL`，说明「真的用不了」——这两类的修法完全不同（§09.8 前半 vs 后半）。
2. **它算的是两个式子，不是一条规则。** 全扫的成本和索引扫的成本都是可代入的表达式（§09.3）。任何「为什么不用索引」的问题，最后都能还原成「这两个式子里的哪一个数被估错了」。
3. **它看不见三个量：随机与顺序的差、缓存命中的差、`rows` 本身的误差。** 这三个量分别由成本常数、统计信息、采样页数决定，而三者都是**常量或估算**，不与你的机器、你的数据同步（§09.3、§09.4）。
4. **它的误差有两个方向，所以「加索引」和「删索引」都可能让事情更坏。** 低估回表成本 → 该全扫的走了索引 → 4.1 s（事故一）；高估 `rows` → 该走索引的走了全扫 → 28 s（事故一 09:10 之后）。同一个模型，两个方向都能伤人。
5. **你能控制的是「让它算便宜」，不是「命令它用索引」。** `FORCE INDEX`/hint 是把今天的结论硬编码进 SQL，数据一变就过期；正确做法是改**输入**：刷新统计、加直方图、把过滤列和排序列放进索引让它变成覆盖索引、或者把谓词改写成可定位的形状。

### 解剖卡八问：本篇答哪几问

| 八问（→ [0-summary](0-summary.md) §0.4） | 落在哪节 | 一句话答案 |
| --- | --- | --- |
| ① 它承诺什么 | §09.2、§09.9 | 承诺「在我的成本模型里挑最便宜的路径」；**不承诺**最优、不承诺稳定、不承诺懂你的数据分布 |
| ② 数据长什么样 | §09.3、§09.4 | 两类摘要：树的形状（`n_rows`、`n_leaf_pages`、`n_diff_pfx`）+ 列的分布（直方图，8.0）+ 一组成本常数 |
| ③ 一次决策走哪条路 | §09.3、§09.5 | 谓词分类 → 候选路径枚举 → 每条路径按 `页数 × io + 行数 × cpu` 折算 → 取最小；结果投影成 `type/key/rows/filtered/Extra` |
| ④ 并发与一致 | §09.4、§09.6 | 无锁、无状态、无缓存一致性可谈：每个连接各自决策，统计信息是**异步快照**，主从/多实例之间天然不一致 |
| ⑤ 扩展维度 | §09.9 | 四选一里只成立两样：**垂直切**（更大的实例装下统计与池）与**读副本摊薄**（同一份统计算两次）；它**不可分片**——优化器看不到跨分片的数据分布，分片后 join 与全局排序必然退化 |
| ⑥ 会在哪崩（系统级失效） | §09.10、§09.11 | 慢（统计翻转 → 全库计划同时变坏，且**同一秒内所有实例一起变**）；满（`Using temporary`/`Using filesort` 落盘，`tmpdir` 打满）；假活（延迟上去了但错误率为零，SLO 面板看不出来） |
| ⑦ 看哪几个指标 | §09.11 | `Rows_examined/Rows_sent`、`no_index_used_pct`、`SUM_ROWS_EXAMINED` 增速、`Handler_read_rnd_next`、`Last_query_cost` 与实测的比值 |
| ⑧ 换掉它的信号 | §09.9、§09.14 | hint > 5 处、同一 SQL 在实例间计划不同、计划随数据增长反复翻转、`rows` 与实测常年差 10 倍以上 → 固化（物化/预聚合）或换引擎 |

---

## 09.3 成本模型：两个式子决定一切

优化器的全部判断可以压成两个可代入的式子。看懂这两行，你就再也不需要背「什么情况下索引失效」——那些条目全是这两行的特例。

```
┌──────────────── 优化器只回答三件事 ────────────────┐
│ ① 有多少行？ N、sel、κ                              │
│ ② 走哪条路？ 全表扫 vs 二级索引 + 回表               │
│ ③ 哪个便宜？ IO 项 + CPU 项，逐项相加                │
└────────────────────────┬───────────────────────────┘
                         ▼
              C_scan  vs  C_idx(sel)  ⇒  sel_opt*

记号：N = 表行数     R = 每叶行数 = floor(16,256 / 行足迹)
      P = 叶子页数 = ceil(N / R)      h = 树高（通常 3，见 §09.4）
      sel = 命中行比例（0~1）          κ = 每个命中行平均落在几个聚簇页上（散列=1）

全表扫（聚簇索引顺序扫全部叶子页）
  C_scan      = P × io_block_read_cost                       ① IO 项：页数
              + N × row_evaluate_cost                        ② CPU 项：每行判一次条件

索引扫（二级索引定位，再逐行回表）
  C_idx(sel)  = h × io_block_read_cost                       ① 树下降
              + N·sel × key_compare_cost                     ② 每条索引项比较一次
              + N·sel·κ × io_block_read_cost                 ③ 回表：κ 个聚簇页/命中行
              + N·sel × row_evaluate_cost                    ④ 回表后判剩余条件

优化器的换挡点（令两式相等）
  sel_opt* = C_scan / (N × (κ·io_block_read_cost + key_compare_cost + row_evaluate_cost))
```

代入 `t_order`：N = 1 亿、行足迹 250 B ⇒ R = 65、P = 1,538,462 页（≈ 23.5 GiB）。8.0 默认常数 `io_block_read_cost = 1.0`、`memory_block_read_cost = 0.25`、`row_evaluate_cost = 0.1`、`key_compare_cost = 0.05`：

| 项 | 表不在池里（`io`=1.0） | 表在池里（`io`=0.25） |
| --- | --- | --- |
| ① 全扫 IO 项 `P × io` | 1,538,462 | 384,615 |
| ② 全扫 CPU 项 `N × 0.1` | 10,000,000 | 10,000,000 |
| **C_scan** | **11,538,462** | **10,384,615** |
| ③ 每命中行回表 `κ·io + key_compare + row_eval` | 1.15 | 0.40 |
| **sel_opt\*** | **10.0%** | **26.0%** |

两个必须记住的读数：

- **CPU 项占了全扫成本的 87%~96%。** 也就是说「全扫贵不贵」在优化器的模型里主要取决于 `row_evaluate_cost` 这个常数乘以行数，而与你的盘有多快几乎无关。这条推论很硬：**加更快的盘救不了全表扫**，因为全扫的瓶颈在 CPU 的逐行条件评估上（真实测量也一致：1 亿行 `WHERE status=?` 的热缓存全扫约 30 s，其中 IO 只占 1.7 s）。
- **优化器认为「命中 10%~26% 时走索引还划算」。** 这是它自己的换挡点。注意它不是「一个数」，而是一个区间——因为同一个页读，它在池内和池外用两个不同的常数（0.25 与 1.0），而它**无从知道这次究竟命中没有**。

### 为什么这些常数是「常量」而不是「实测」

成本常数存在两张表里，不在 `SHOW VARIABLES` 里。所以你在 `SHOW VARIABLES LIKE '%page_cost%'` 里找不到 `innodb_page_cost` 是**正常的**——MySQL 没有 PostgreSQL 那种 `random_page_cost`/`seq_page_cost` 的一对旋钮，对应物是 `engine_cost` 表里的一行：

| 常数 | 所在表 | 8.0 默认 | 它想表达 | 为什么它必然算错 |
| --- | --- | --- | --- | --- |
| `io_block_read_cost` | `mysql.engine_cost` | 1.0（5.7 与 8.0 同） | 读一个数据块 | **不分随机/顺序、不分 NVMe/机械盘、不分冷/热**。机械盘上随机读贵 10~40 倍，它照样给 1.0 |
| `memory_block_read_cost` | `mysql.engine_cost` | 0.25（**8.0 起**；5.7 是 1.0） | 池内命中读一块 | 与真实命中率无关。5.7 的 1.0 意味着「池内命中一点都不便宜」，同一个模型在 8.0 前后结论不同 |
| `row_evaluate_cost` | `mysql.server_cost` | 0.1 | 评估一行条件 | 与行宽、条件个数、表达式复杂度全都无关：比 1 个 `INT` 和比 5 个 `VARCHAR` 同价 |
| `key_compare_cost` | `mysql.server_cost` | 0.05 | 比较一次索引键 | 与键的字节长度无关 |
| `disk_temptable_create_cost` / `disk_temptable_row_cost` | `mysql.server_cost` | 20 / 0.5 | 磁盘临时表 | 「`Using temporary` 到底多贵」全靠这两个数拍 |
| `memory_temptable_create_cost` / `memory_temptable_row_cost` | `mysql.server_cost` | 1 / 0.1 | 内存临时表 | 同上 |

**这就是「它经常算错」的第一层原因**：这几个数是 MySQL 在机械盘 + 小内存 + 「一块随机读约等于一块顺序读」的年代拍下来的，随后二十年没回测。它们唯一能表达的相对关系是**页数比**，而真实世界里 16 KB 顺序读（≈ 2~5 µs）与 16 KB 随机读（NVMe 单队列 ≈ 80 µs，SATA SSD ≈ 250 µs，机械盘 ≈ 5~8 ms）差 16~1500 倍——**这个比值在这套常数里根本不存在**。它的后果是双向的：

- 当 `rows` 准的时候：优化器**低估**回表成本，于是**过量使用索引**（事故三：20% 选择性也敢走索引，物理上该全扫）；
- 当 `rows` 不准的时候：`sel` 被放大几十倍，优化器**过高估计索引的代价**，于是**过早放弃索引**（事故一：0.6% 的选择性被当成 25%）。

```sql
-- ① 看常量：成本常数在两张表里，不在 SHOW VARIABLES 里
SELECT cost_name, cost_value, default_value, comment FROM mysql.server_cost;
SELECT engine_name, cost_name, cost_value, default_value FROM mysql.engine_cost;

-- ② 想让「随机读更贵」只能整体改 io_block_read_cost —— 副作用是全扫的每一页也一起变贵，
--    于是它只是把整个模型等比放大，换挡点不变。这就是 MySQL 的结构性缺口。
UPDATE mysql.engine_cost SET cost_value = 2.0
 WHERE engine_name = 'innodb' AND cost_name = 'io_block_read_cost';
FLUSH OPTIMIZER_COSTS;          -- 必须刷新生效；这两张表不能用 SET GLOBAL 改

-- ③ 看优化器自己算出了多少代价（SESSION 状态变量，不是视图）
SHOW SESSION STATUS LIKE 'Last_query_cost';
```

**量级结论**：`sel_opt*` 在 10%（表冷、用 `io_block_read_cost`）到 26%（表热、用 `memory_block_read_cost`）之间浮动。假设清单：①行足迹 250 B、②常数全取 8.0 默认、③`κ=1`（命中行完全散列）、④忽略树下降以外的常数项。换假设复算：`κ` 取 1/65（命中行挤在同一页）时 `sel_opt*` 涨到 26%~90%，这就是「范围扫比等值散查便宜」在同一个模型里的表达。

---

## 09.4 统计信息：优化器的眼睛有多糊

优化器不读数据，它读三样摘要：

| 摘要 | 存在哪 | 谁写它 | 精度受什么限制 |
| --- | --- | --- | --- |
| `n_rows`（表行数） | `mysql.innodb_table_stats` | `ANALYZE TABLE` / `innodb_stats_auto_recalc` 后台线程 | **抽样估算**，8.0 手册明说 InnoDB 的 `TABLE_ROWS` 可差 40%~50% |
| `n_leaf_pages` / `size`（索引页数） | `mysql.innodb_index_stats` | 同上 | 页数是相对准的（B+ 树节点数不好撒谎） |
| `n_diff_pfx01..NN`（各前缀的基数） | `mysql.innodb_index_stats` | 同上 | **每个前缀独立采样**，联合基数尤其不准 |
| 列直方图（分布） | 数据字典 `column_statistics`（8.0） | 只有手工 `ANALYZE TABLE ... UPDATE HISTOGRAM` 会写 | 默认 100 桶、最多 1024 桶（8.0），**不随写入自动更新** |

### 采样 20 页到底意味着什么

关键参数（全部给默认值与版本）：

| 参数 | 默认 | 版本 | 它改变了什么 |
| --- | --- | --- | --- |
| `innodb_stats_persistent` | `ON` | 5.6.6+ 起默认 ON | 统计写进 `mysql.innodb_*_stats` 表（可复现）；`OFF` 时退回内存态、每次重开表重算，计划会跳 |
| `innodb_stats_persistent_sample_pages` | **20** | 5.6.6+ | 每个索引随机抽多少**叶子页**来估基数 |
| `innodb_stats_transient_sample_pages` | 8 | 5.6.6+ | `persistent=OFF` 时用的采样页数（更少，所以计划更飘） |
| `innodb_stats_auto_recalc` | `ON` | 5.6.6+ | 「本表自上次统计以来修改行数 > 10%」时由**后台线程异步**触发重算；不保证导入完成的那一刻生效，也不管分布 |
| `innodb_stats_on_metadata` | `OFF` | 5.6.6+ 起默认 OFF | 打开后 `SHOW TABLE STATUS`/查 `information_schema` 就触发重算——历史上是「一条监控 SQL 让所有查询变慢」的元凶 |
| `information_schema_stats_expiry` | `86400`（秒） | **8.0 专有** | 8.0 的 `information_schema.tables.table_rows` 走**缓存**，默认 24 小时才过期；想看真值要 `=0` 或先 `ANALYZE` |
| `eq_range_index_dive_limit` | 200 | 5.7.4+ | 等值区间数超过它就不再逐个「索引下潜」实测，改用统计估算（`IN` 里几千个值突然全扫的原因之一） |

代入算一下「20 页」有多糊：`t_order` 行足迹 250 B、R = 65 行/叶，20 页 ⇒ **采样 1,300 行**去估计 1.02 亿行的分布。

- 要估一个占 **1%** 的值（1,020,000 行）：采样里期望命中 13 行，相对标准差 ≈ 1/√13 ≈ **28%**；
- 要估一个占 **0.1%** 的值（102,000 行）：采样里期望命中 1.3 行，**相对误差 ±90%**，经常抽到 0；
- 要估 **0.01%** 的值（10,200 行）：采样里期望命中 **0.013 行**——绝大多数采样里它是 0。优化器于是认为「这个值不存在」。

而且这还只是「估某一行的多少」这一层；再上一层是**独立性假设**：多列条件下它默认「`status` 与 `city` 互不相关」，「选择性 = 各列选择性相乘」。真实数据里相关列满地都是（`city='杭州' AND district='西湖区'`），于是乘积把 `rows` 再压一个数量级。

**图 1 · 统计信息如何一路变成错误计划（事故一的四步）**

```text
        真实世界                       优化器看到的世界
 ┌──────────────────┐   20 页采样   ┌──────────────────────────┐
 │ 1.02 亿行         │ ───────────► │ n_rows = 1.02 亿          │
 │ status 4 个值，倾斜│   (~1,300 行) │ n_diff = 4 → 均分 25%     │
 │ REFUND 仅 0.6%    │              └────────────┬─────────────┘
 └──────────────────┘                           │ rows = 2,550 万（真值 61.2 万）
                                                ▼
                                     ┌──────────────────────────┐
                                     │ C_idx(25%) ≈ 2,550 万 × 1.15 │  ← 回表成本爆炸
                                     │ C_scan      ≈ 1,538,462 + 1,020 万 │
                                     └────────────┬─────────────┘
                                                  ▼
                                       计划 = ALL 全扫（28 s）

 治法是补分布：ANALYZE … UPDATE HISTOGRAM (8.0)
   singleton 桶把 REFUND 单独成桶 → rows = 612,000 → 计划翻回 range（180 ms）
 注意：直方图对「列 = 常量」有效；对 join 列、对多列相关性，8.0 依然无能为力
```

### 刷新前后：`rows` 差 100 倍的具体形状

| 这一刻的 `n_rows` | `status='REFUND'` 的 `rows` 估算 | 真实值 | 误差 | 计划 | 实测 |
| --- | --- | --- | --- | --- | --- |
| 102 万（09-01 的旧值） | 255,000（旧行数 ÷ 4） | 612,000 | **低估 2.4 倍**（但相对总行数错 100 倍） | `range` + 回表 | 4,100 ms |
| 1.02 亿（`ANALYZE` 后，均分） | 25,500,000 | 612,000 | **高估 41.7 倍** | `ALL` 全扫 | 28,000 ms |
| 1.02 亿 + 直方图 100 桶 | 612,000 | 612,000 | 基本准确 | `range` + 覆盖索引 | 180 ms |

注意第一行那个「100 倍」的性质：`n_rows` 从 102 万变 1.02 亿是**表本身涨了 100 倍**，而统计没跟上，所以优化器眼里的「世界规模」错了 100 倍——它会同时把每页装的行数、每棵树的页数、每个值出现的次数全部按 100 分之一来想。**导入之后不刷新统计，等于让优化器在一个小 100 倍的世界里做决策。**

```sql
-- 09.4 一次把统计信息的全部线索抓齐（版本：MySQL 8.0/8.4）
SHOW VARIABLES WHERE Variable_name IN (
  'innodb_stats_persistent', 'innodb_stats_persistent_sample_pages',
  'innodb_stats_auto_recalc', 'innodb_stats_on_metadata',
  'information_schema_stats_expiry', 'eq_range_index_dive_limit');

SELECT table_name, n_rows, clustered_index_size, last_update
  FROM mysql.innodb_table_stats WHERE table_name = 't_order';

SELECT index_name, stat_name, stat_value, last_update
  FROM mysql.innodb_index_stats
 WHERE table_name = 't_order' AND stat_name IN ('n_leaf_pages','size')
    OR (table_name = 't_order' AND stat_name LIKE 'n_diff_pfx%');   -- 各前缀基数

ANALYZE TABLE t_order;                                          -- 只重算 n_rows 与 n_diff，不动直方图
ANALYZE TABLE t_order UPDATE HISTOGRAM ON status WITH 100 BUCKETS;  -- 8.0 专有：补分布

SELECT COLUMN_NAME,
       JSON_LENGTH(HISTOGRAM->>'$."buckets"')  AS buckets,
       HISTOGRAM->>'$."histogram-type"'        AS htype,       -- singleton / equi-height
       HISTOGRAM->>'$."sampling-rate"'         AS sampling,    -- 1 = 全量，<1 = 采样
       HISTOGRAM->>'$."last-updated"'          AS updated      -- 不会自动更新，只有你能更新它
  FROM information_schema.COLUMN_STATISTICS
 WHERE TABLE_SCHEMA = 'shop' AND TABLE_NAME = 't_order';
```

> **纪律提示**：`ANALYZE TABLE` 与直方图是两件独立的事。前者重算行数与基数（`n_diff`），后者重算分布；**直方图永远不会被 `innodb_stats_auto_recalc` 自动更新**，只有你的脚本会。倾斜严重、且被高频用作过滤条件的列（状态、类型、城市、租户 ID），必须进「定时刷直方图」的清单。

---

## 09.5 EXPLAIN 逐列读法（含 key_len 反推）

`EXPLAIN` 输出六列真正有用：`type`、`key`、`key_len`、`rows`、`filtered`、`Extra`。其余（`id`、`select_type`、`ref`、`partitions`）只帮你定位是哪张表、哪一层子查询。

### `type`：十档，强弱与代价是同一件事

| `type` | 它做了什么 | 一次访问的页数（h=3） | 相对代价 | 什么条件下才会出现 |
| --- | --- | --- | --- | --- |
| `system` | 表只有一行 | 0~1 | 最低 | 数据字典表、只有一行的派生表 |
| `const` | 主键/唯一索引等值，最多一行 | 3 | 低 | `WHERE pk = 常量`、`WHERE uk = 常量` |
| `eq_ref` | join 时每行在唯一索引上唯一匹配 1 行 | 3 /驱动行 | 中低 | `ON u.id = o.user_id`（`u.id` 是主键/唯一） |
| `ref` | 非唯一索引等值，命中一批 | 3 + 回表数 | 中 | join 最常见形态；`WHERE k = 常量` |
| `fulltext` | 全文索引 | — | — | `MATCH ... AGAINST` |
| `ref_or_null` | `ref` 再并上 `IS NULL` 那一支 | 3 × 2 + 回表 | 中 | `WHERE k = ? OR k IS NULL` |
| `index_merge` | 两个索引各取集合再合并（`Using union/intersect/sort_union`） | 2 × 树 + 去重 | 中高 | `OR` 跨列且两列都有索引（§09.8 第 4 条） |
| `unique_subquery` / `index_subquery` | `IN (子查询)` 用唯一/普通索引探测 | 3 /值 | 中 | `WHERE id IN (SELECT ...)` 且子查询列有索引 |
| `range` | 索引区间扫 | 3 + 区间内叶子页 | 中高 | `>、<、BETWEEN、IN、LIKE 'x%'` |
| `index` | 全索引扫（扫整棵索引，仍可能要回表） | 索引叶子页总数 | 高 | 覆盖索引 / `ORDER BY` 用得上索引序 |
| `ALL` | 全表扫（聚簇叶子页全扫） | 叶子页总数 | 最高 | 谓词不可定位，或优化器算完全扫更便宜 |

两点必须说清：

1. 上表的强弱是**官方文档的名义顺序**，不是成本排序。`index` 排在 `range` 之后，但「扫一棵窄索引的全部叶子」经常比「`range` 扫 20% 再回表」便宜得多——所以看到 `type=index` 不要立刻当坏消息，先看 `Extra` 有没有 `Using index`。
2. **`type` 只对「驱动表第一次访问」有完整意义**。被驱动表显示 `eq_ref`/`ref` 时，它的真实代价要乘以驱动行数（§09.6 的 `loops`）。`EXPLAIN` 里一行 `eq_ref rows=1`，实际可能被执行 1,000 万次——这正是事故三的全部内容。

### `key_len`：反推「用到了复合索引的第几列」

`key_len` 是「这个索引在被使用的那一段里占多少字节」，把各列的存储宽度加起来就能反推用到第几列。常用宽度（utf8mb4 下 `VARCHAR(n)` 一律按最坏 4 字节/字符计）：

| 列类型 | 索引中的字节 |
| --- | --- |
| `TINYINT` / `SMALLINT` / `MEDIUMINT` / `INT` / `BIGINT` | 1 / 2 / 3 / 4 / 8 |
| `DATE` / `DATETIME` / `TIMESTAMP` | 3 / 5 / 4（带 `fsp` 时再 + `ceil(fsp/2)`） |
| `CHAR(n)` utf8mb4 | 4n |
| `VARCHAR(n)` utf8mb4 | 4n **+ 2**（长度前缀）**+ 1**（可空 NULL 位） |
| `VARCHAR(n)` utf8mb3 | 3n + 2（+1 若可空） |
| `VARCHAR(n)` gbk | 2n + 2（+1 若可空） |

**算式**：把目标索引的列按顺序累加，得到每个前缀的字节数，然后拿 `key_len` 去对号。

设 `KEY idx_su (status VARCHAR(16) NULL, user_id BIGINT NOT NULL, created_at DATETIME NOT NULL)`：

```
status     = 16×4 + 2 + 1 = 67
status,user_id            = 67 + 8          = 75
status,user_id,created   = 75 + 5          = 80
```

| 读到的 `key_len` | 用到的前缀 | 结论与下一步 |
| --- | --- | --- |
| 67 | `(status)` | 只等值定位到第 1 列；后续列只能做 `Using where` 或 ICP 过滤 |
| 75 | `(status, user_id)` | 两列都定位；`created_at` 只能过滤 |
| 80 | 全部三列 | 三列都参与定位（`SELECT` 列都在索引里时还会带 `Using index`） |
| 66 / 74 / 79 之类 | 对不上任何前缀 | 字符集或可空性与你的预期不符 → 回去查 `SHOW CREATE TABLE` 的 `CHARSET`/`NULL` |
| 比 80 更大 | — | 一定不是这个索引（可能 `key` 是另一个索引，或 8.0 的 `skip scan` 跳过了第 1 列） |

两条容易踩的细节：**① InnoDB 二级索引叶子隐含带上主键，但 `key_len` 不计它**，所以 `key_len` 恰好等于用户列宽度之和是正常的；**② 8.0.13+ 的 `Using index for skip scan`** 允许在缺少前缀的情况下用后面的列，此时 `key_len` 会显示被跳过的列之外的部分——看到它先别高兴，skip scan 的代价是「按前缀基数做多次区间跳转」，只在第 1 列基数极小时划算。

### `rows` × `filtered`：相乘才是真实扫描量

- `rows`：优化器估算「这一步要读多少行」；
- `filtered`：读完这些行之后，**本表剩余条件**还能留下百分之几；
- **真实扫描量 = 驱动表 `rows` × 驱动 `filtered` × 被驱动 `rows` × …**（嵌套循环逐层相乘）。

代入事故三的 `EXPLAIN`：`t_order` 行 `rows=3,000,000`、`filtered=33.33`，`t_user` 行 `eq_ref rows=1`。真实读取量 ≈ 300 万（全扫）→ 100 万（过滤后）→ 每行 1 次 `t_user` 主键探测 = **100 万次探测**（优化器这么算）。而实际表有 1,000 万行 ⇒ 真实的 `t_order` 扫描 1,000 万行、探测 1,000 万次。**`rows` 一个数字错了 3.3 倍，探测次数就错了 10 倍**——因为它在 `filtered` 之后还要再乘进 join 循环的 `loops` 里。

一个必须记住的坑：`filtered = 100.00` 常常不是「全都留下」，而是「**优化器估不出来，所以按 100% 算**」。它是乐观默认值，不是实测值。判断真伪只有一个办法：`EXPLAIN ANALYZE` 看实际 `rows`（§09.6）。

### `Extra`：每一条都对应一个具体动作

| `Extra` 里写了什么 | 它的物理含义 | 看到这一条该做什么 |
| --- | --- | --- |
| `Using index` | 覆盖索引：只读索引叶子，**不回表** | 别动它。任何往 `SELECT` 里加列（尤其 `SELECT *`）的改动都会把它打回回表 |
| `Using where` | 行已经读出来了，条件在 server 层过滤 | 该列能否进索引？能进就前移成定位条件或 ICP；不能进就是「命中行数必须足够少」 |
| `Using index condition` | ICP：在引擎层用索引里的列先过滤，**省掉不必要的回表**（5.6+；`optimizer_switch` 的 `index_condition_pushdown` 默认 `on`） | 好消息。若还慢，说明省的是回表不是索引扫描——要改索引列顺序（§09.8 第 5 条） |
| `Using MRR` | 回表前把主键排序成按页顺序批量读（`mrr=on`、`mrr_cost_based=on` 默认） | 冷池随机 IO 场景收益最大；默认常不出现，因为 `cost_based` 认为不值 |
| `Using filesort` | 额外排序（内存 `sort_buffer_size`，超出落盘） | 把 `ORDER BY` 的列做成索引的后缀，让索引顺序直接满足排序；或缩小 `SELECT` 列让排序变窄 |
| `Using temporary` | 建临时表（`GROUP BY`/`DISTINCT`/`UNION`/物化子查询） | 检查 `GROUP BY` 是否与索引顺序一致；`tmp_table_size` 超出后落盘，`tmpdir` 会满 |
| `Using join buffer (hash join)` | **8.0.18+**：无可用索引的等值 join 走 Hash Join（8.0.20 起 `Block Nested Loop` 已被移除，`block_nested_loop` 标记只控制 hash join） | join 列缺索引，或类型/字符集不匹配。给 join 列建索引；只想缓解就先增大 `join_buffer_size`（默认 256 KB，溢出会落盘分片） |
| `Using join buffer (Block Nested Loop)` | **8.0.18 之前**的 BNL（8.0.20 起此算法已不存在） | 同一条：join 列没索引。看到它说明版本 ≤ 8.0.19 或开关被关掉 |
| `Range checked for each record (index map: 0x…)` | 每一行都要重新选一次索引 | 典型的统计烂或 join 列类型不匹配（§09.8 第 1、8 条） |
| `Using index for group-by` / `Using index for skip scan` | 索引顺序满足 `GROUP BY`；或跳过前缀做多段扫描（**8.0.13+**） | 都是好消息，但 skip scan 只在跳过列基数极小时成立 |
| `Backward index scan` | **8.0** 的降序索引反向扫描 | 说明 `ORDER BY ... DESC` 已经吃到索引序（§09.8 第 7 条） |
| `Select tables optimized away` / `Impossible WHERE` | 常量折叠，无需访问表 | 不用管 |

---

## 09.6 EXPLAIN ANALYZE：把估算换成实测

`EXPLAIN` 是**估算**，`EXPLAIN ANALYZE`（**8.0.18+**，只有 TREE 格式）是**执行一遍再报告实测**。三个读数决定你怎么读它：

- `actual time=首行..末行`：**每次循环的平均值**（单位 ms），不是总和；
- `rows=N`：每次循环返回的行数平均值；
- `loops=L`：这个节点被执行的次数。

于是有两条换算式，忘掉它们就会把 1,000 万次探测读成 1 次：

```
真实总耗时 ≈ 末行 actual time × loops
真实总行数 ≈ rows × loops
```

`FORMAT=JSON` 给的是另一套词，两者对应关系值得单独记：

| `FORMAT=JSON` 字段 | 含义 | 与 TREE 的对应 |
| --- | --- | --- |
| `rows_examined_per_scan` | 这一次扫描**读了多少行** | TREE 里的 `rows` |
| `rows_produced_per_join` | 这一步在 join 结果里**贡献多少行** | 上一行 × `filtered` |
| `filtered` | 通过率（百分比） | 同 |
| `cost_info.read_cost` | IO 相关的成本分项（页 × `io_block_read_cost`） | 无（TREE 只给总 cost） |
| `cost_info.eval_cost` | CPU 相关的成本分项（行 × `row_evaluate_cost`） | 无 |
| `cost_info.prefix_cost` | 到本步为止的**累计**成本 | 父节点的 `cost=` 大致对应 |
| `cost_info.data_read_per_join` | 本步带出的字节数（判断有没有误读大字段） | 无 |
| `cost_info.query_cost` | 整条 SQL 的总成本 | 根节点的 `cost=` |

`rows_examined_per_scan` 与 `rows_produced_per_join` 的**比值就是过滤效率**：比值 100 表示「读了 100 行只留 1 行」——这时你要问的是「这 100 行是白读的吗？能不能靠索引把它们跳过」。`EXPLAIN` 里看到 `rows` 很大就慌是没意义的，要看这两个数。

```sql
-- ① EXPLAIN：它「打算」怎么做（估算，不执行）
EXPLAIN SELECT o.id, o.amount FROM t_order o
  JOIN t_user u ON u.id = o.user_id
 WHERE u.city = '杭州' AND o.status = 'REFUND';
-- id=1 table=o type=ALL     rows=102000000 filtered=100.00 Extra=Using where
-- id=1 table=u type=eq_ref  rows=1         filtered=10.00  Extra=Using where
-- 真实读取量 ≈ 1.02 亿（驱动全扫）× 100% × 1 = 1.02 亿次读 + 1.02 亿次探测

-- ② EXPLAIN ANALYZE（8.0.18+，只有 TREE）：它「实际」怎么做的，会真执行
EXPLAIN ANALYZE SELECT o.id, o.amount FROM t_order o
  JOIN t_user u ON u.id = o.user_id
 WHERE u.city = '杭州' AND o.status = 'REFUND';
-- -> Nested loop inner join  (cost=... rows=612) (actual time=4120..8260 rows=612 loops=1)
--     -> Table scan on o  (actual time=0.1..3980 rows=102000000 loops=1)
--     -> Single-row index lookup on u using PRIMARY (actual time=0.012..0.012 rows=1 loops=102000000)
-- 真实总耗时 ≈ 3980 + 0.012 × 102,000,000 ≈ 5,204 ms，与实测 8,260 ms 同量级

-- ③ FORMAT=JSON：把它算的账拆开
EXPLAIN FORMAT=JSON SELECT o.id FROM t_order o WHERE o.status = 'REFUND'\G
-- "rows_examined_per_scan": 102000000, "rows_produced_per_join": 25500000, "filtered": "25.00",
-- "cost_info": { "read_cost": "1538462.00", "eval_cost": "2550000.00", "prefix_cost": "4088462.00" }
```

> **两条纪律**：**① `EXPLAIN ANALYZE` 会真的执行 SQL**——对 `UPDATE`/`DELETE` 也一样（8.0.18+ 支持，8.0.19+ 覆盖更多语句），生产上要么在从库/影子库跑，要么先包在事务里并且不提交。**② 它给的 `actual time` 是「每次循环平均」，`loops` 才是放大器**；事故三就是靠 `loops=10,000,000` 一击定案的。

---

## 09.7 Join 与子查询：驱动表一错，全盘皆错

Join 的代价不是「两张表各扫一次」，而是 **驱动表行数 × 被驱动表每次访问的代价**。这个乘法决定了：**优化器在 join 上犯的错会被循环次数放大**，而循环次数恰恰是它估得最不准的那个量。

### 三种算法与版本

| 算法 | 什么时候用 | 版本 | 代价形状 | 关键参数 |
| --- | --- | --- | --- | --- |
| NLJ（嵌套循环 + 索引探测） | 被驱动表的 join 列有索引 | 一直有 | `驱动行数 × (h + 回表数)` | 被驱动表的索引质量 |
| BNL（Block Nested Loop） | 无索引等值 join，把驱动行塞 join buffer 后批量比对 | 8.0.18 前；**8.0.20 起服务端已移除 BNL** | 被驱动表被扫 `(S×C)/join_buffer_size + 1` 次 | `join_buffer_size`（默认 256 KB） |
| Hash Join | 无可用索引的等值 join（8.0.18+ 内连接；**8.0.20+ 覆盖 outer/semi/anti**） | 8.0.18+ | 建哈希表 1 遍 + 探测 1 遍 = 输入各扫一次 | `join_buffer_size`（增量分配；不够就落盘分片，可能撞 `open_files_limit`） |

一句话记：**Hash Join 把「被驱动表扫 N 次」换成了「两张表各扫 1 次」，但它只在你没有索引可用时才登场**——一旦 join 列上有索引，优化器通常会选 NLJ（因为 NLJ 的探测是 O(1) 的树查找）。

### 派生表与子查询：merge 还是物化

| 写法 | 优化器的处理 | 关键开关（默认值） | 版本 |
| --- | --- | --- | --- |
| `FROM (SELECT ... ) d`（无聚合、无 `LIMIT`、无 `UNION`） | **merge**：把派生表拆开合进外层，谓词下推 | `derived_merge=on` | 5.7+ |
| 同上但含 `GROUP BY`/`DISTINCT`/`LIMIT`/`UNION` | **物化**：建带索引的临时表，代价是「建表 + 写 + 也许落盘」 | `materialization=on` | 5.6+ |
| 派生表里的条件推不出来 | 8.0.22+ 支持把外层条件下推到派生表内部 | `derived_condition_pushdown=on` | **8.0.22+** |
| `WHERE x IN (SELECT ...)` / `EXISTS` | **半连接改写**：Table Pullout / FirstMatch / LooseScan / Materialization / DuplicateWeedout，由成本模型挑 | `semijoin`、`firstmatch`、`loosescan`、`duplicateweedout`、`materialization` 全 `on` | 5.6+（`EXISTS` 从 8.0.16 起享受同样改写） |
| `NOT IN` / `NOT EXISTS` | **反连接（antijoin）改写**，子查询消失，可与半连接同层重排 | `semijoin=on` 同时控制 antijoin | **8.0.17+**；比较列可空时通常不适用（`NOT IN` 遇 NULL 的语义限制） |

### 小表驱动大表的算例与反例

**算例**：`t_order` 1,000 万行（行足迹 250 B ⇒ 65 行/叶 ⇒ 153,846 叶页 ≈ 2.35 GiB），`t_user` 200 万行（行足迹 200 B ⇒ 81 行/叶 ⇒ 24,691 叶页 ≈ 386 MB）。查询带 `u.city='杭州'`（2 万行，1%）与 `o.amount > 500`。

| 计划 | 驱动行数 | 每行代价（池内） | 中间结果 | 估算耗时 |
| --- | --- | --- | --- | --- |
| ❌ `t_order` 全扫驱动 | 1,000 万 | 主键探测 1.2 µs（`t_user` 主键） | **1,000 万行** | 1,000 万 × 1.2 µs = **12 s** |
| ✅ `t_user` 走 `idx_city` 驱动 | 2 万 | 索引探测 1.5 µs + 平均 5 行回表 × 2 µs = 11.5 µs | **10 万行** | 2 万 × 11.5 µs ≈ 0.23 s + 驱动侧 ≈ **0.4 s** |

**放大倍数：中间结果 100 倍，耗时 30 倍。** 这就是事故三那张表里 80 ms → 12 s 的机制解释——不是「索引不好」，而是**内层探测被循环了 500 倍次数**。

**反例：结论什么时候会翻转。** 「小表驱动大表」这句话的准确版本是「**过滤后行数少的驱动**」，与表的总行数无关：

| 场景 | `u` 过滤后 | `o` 过滤后 | 该谁驱动 | 选反的代价 |
| --- | --- | --- | --- | --- |
| A | 2 万（1%） | 1,000 万（100%） | `u` | 1,000 万次探测 ≈ 12 s |
| B | 200 万（100%，城市条件全命中） | 6 万（0.6%，`amount>5000`） | **`o`** | 200 万 × 5 行 = 1,000 万中间行 ≈ 4 s |
| C | 2 万（1%） | 6 万（0.6%） | 两者都行，别争 | 差 2~3 倍，属噪声区间 |

场景 B 是所有人都会写错的那一版：**「小表」是 `t_user`（200 万 < 1,000 万），但它的过滤条件一点没生效**，于是该由 `t_order` 驱动。这也解释了为什么「按表大小排 join 顺序」的老经验经常翻车——优化器的输入是 `rows × filtered`，不是 `n_rows`。

**图 2 · 驱动表一错，代价在循环里放大**

```text
   ✅ 正确：过滤后行数少的做驱动
   t_user (idx_city → 2 万行)                t_order (idx_user_id)
   ┌────────────────────────┐  每行 1 次探测  ┌──────────────────────────┐
   │ 杭州用户 20,000         │ ─────────────► │ 索引探测 1.5 µs           │
   │ （占 200 万的 1%）      │                │ + 平均 5 行回表 × 2 µs     │
   └────────────────────────┘                └────────────┬─────────────┘
                │                                          │
                └──────────► 中间结果 100,000 行 ◄──────────┘     总耗时 ≈ 0.4 s

   ❌ 选反：全扫大表做驱动
   t_order (ALL → 1,000 万行)                t_user (PRIMARY)
   ┌────────────────────────┐  每行 1 次探测  ┌──────────────────────────┐
   │ 1,000 万行              │ ─────────────► │ 主键探测 1.2 µs × 1,000 万 │
   └────────────────────────┘                └────────────┬─────────────┘
                │                                          │
                └──────────► 中间结果 10,000,000 行 ◄───────┘     总耗时 ≈ 12 s
                                  ↑ 再被 filtered 扔掉 99.8%：白干
```

**量级结论**（假设清单：①`t_order` 1,000 万行 / 250 B、`t_user` 200 万行 / 200 B；②索引与主键探测全部命中 buffer pool（1.2~1.5 µs），回表 2 µs/页；③单线程、无网络与结果集传输；④`loops` 精确等于驱动行数）。换假设复算：若两表都不在池里，探测变成一次随机页读（80 µs），错计划的 12 s 会变成 800 s 量级——现实值介于两者之间，取决于 LRU 给这张表留了多少页。

---

## 09.8 十条索引失效：证据与修法

十条里有**五条是「真的用不了」**（谓词形状破坏了树的有序性），**五条是「能用但没选」**（成本模型算出来不划算，或统计让它以为不划算）。分清这两类，修法完全不同：前者要改 SQL 或改索引形态，后者要改统计或改成本输入。

| # | 现象 | 根因（指到模型/物理） | `EXPLAIN` 证据 | 修法 |
| --- | --- | --- | --- | --- |
| 1 | `WHERE mobile = 13800000000`（列是 `VARCHAR`）不命中索引 | 字符串列与数字比较时，**列被转成 `DOUBLE`**，树序与比较值失去对应关系 | `type=ALL`、`key=NULL`；`SHOW WARNINGS` 里能看到 `cast(mobile as double)` | 字面量加引号；类型对齐（DTO 字段与列类型一致），或反向把列改成 `BIGINT` 并统一写入侧 |
| 2 | `WHERE DATE(created_at) = '2025-09-10'` 全扫 | 函数作用在列上，树的排序键变成函数值，索引无法定位（§09.4 事故二） | `key=NULL`、`type=ALL`；`rows` 等于 `n_rows` | 改区间写法 `>= '2025-09-10' AND < '2025-09-11'`；或 8.0.13+ 建函数索引 `((DATE(created_at)))` |
| 3 | `LIKE '%888'`（前置通配）全扫 | 前缀未知⇒区间切不出来（→ `comp/04` §04.4） | `type=ALL`、`Extra=Using where` | 后缀模糊交给倒排（→ `comp/21`）；或反转存一列 `mobile_rev` 后 `LIKE '888%'` |
| 4 | `WHERE a=1 OR b=2`（两列各有索引）却全扫 | 一棵树只有一条排序路径，`OR` 需要「两个集合求并」 | 好结果：`type=index_merge` + `Using union(idx_a,idx_b)`；坏结果：`type=ALL` | 改成 `UNION ALL` 两支各自走索引；或建联合索引把 `OR` 化成前缀（能合并且选择性提升时） |
| 5 | `KEY(a,b)` 下 `WHERE a>10 AND b=2`，`b` 没起定位作用 | 范围条件之后的列在树上是**打散**的，只能过滤不能定位（→ `comp/04` §04.5） | `key_len` 只到 `a` 的宽度；`Extra=Using index condition`（ICP 在回表前滤 `b`） | 等值列前置 `(b,a)`；或接受 ICP（省回表不省索引扫描）；8.0.13+ 若 `a` 基数极小可赌 skip scan |
| 6 | 同一 SQL 昨天 20 ms、今天 4 s，SQL 没改 | 统计信息过期/采样太粗 ⇒ `rows` 不可信（§09.4） | `EXPLAIN` 的 `rows` 与 `EXPLAIN ANALYZE` 的实际 `rows` 差 10 倍以上；`n_rows` 的 `last_update` 停在改数据之前 | `ANALYZE TABLE` + 倾斜列加直方图 + 提到 `persistent_sample_pages`；导入后把 `ANALYZE` 写进流程 |
| 7 | 加了 `ORDER BY a DESC, b ASC` 就出现 `Using filesort` | 索引是 `(a ASC, b ASC)`，排序方向不一致时无法用索引序 | `Extra=Using filesort`；`EXPLAIN` 无 `Backward index scan` | 8.0 建降序索引 `KEY(a DESC, b ASC)`；或把 `ORDER BY` 改成与索引同向；`LIMIT` 小时也可接受 filesort |
| 8 | join 两列类型/字符集一致却用不上索引 | `utf8mb4` 与 `utf8mb3`（或不同 collation）比较时**一侧被转换**，索引失效 | `type=ALL` + `Using join buffer (hash join)`；`SHOW WARNINGS` 里有 `convert(... USING utf8mb4)` | 全库统一 `utf8mb4` + 统一 collation（建表模板 + CI 检查），join 列的类型逐字节一致 |
| 9 | `possible_keys` 有它，`key` 是 `NULL`，没人改过 SQL | `optimizer_switch` 关了相关能力（`index_merge=off`），或 SQL 里有 `IGNORE INDEX`，或 8.0 把索引设成了 `INVISIBLE` 忘了改回 | `possible_keys` 非空 + `key=NULL`；`SHOW WARNINGS` 能看到改写后的 SQL；`information_schema.statistics.is_visible='NO'` | 恢复开关/去掉 hint；`ALTER TABLE ... ALTER INDEX ... VISIBLE`；查变更记录 |
| 10 | `WHERE status != 'PAID'`、`NOT IN (...)`、`NOT LIKE 'x%'` 走索引反而慢 | 取反后选择性通常极差（命中大半行），`p` 远超 `p*`（§09.9） | `type=range` 但 `rows` 接近 `n_rows`；实测比全扫还慢 | 改成正向等值与范围；把「坏条件」放到覆盖索引里变成 index-only；或干脆让它全扫并优化它（减少 `SELECT` 列） |

```sql
-- 十条的最小复现：先灌 10 万行再 ANALYZE，否则小表一律 ALL，看不出差别
CREATE TABLE t_probe (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  uid VARCHAR(32) NOT NULL,          -- 故意用字符串存数字（第 1 条）
  mobile VARCHAR(20) NOT NULL,
  city VARCHAR(20) NOT NULL,
  created_at DATETIME NOT NULL,
  amount DECIMAL(12,2) NOT NULL,
  KEY idx_uid (uid), KEY idx_mobile (mobile),
  KEY idx_city_created (city, created_at)      -- 第 5 条：范围在前、等值在后
) ENGINE=InnoDB;

EXPLAIN SELECT * FROM t_probe WHERE uid = 123456;                    -- ① key=NULL（隐式转换）
EXPLAIN SELECT * FROM t_probe WHERE DATE(created_at) = '2025-09-10'; -- ② key=NULL（函数）
EXPLAIN SELECT * FROM t_probe WHERE mobile LIKE '%888';              -- ③ type=ALL（前置通配）
EXPLAIN SELECT * FROM t_probe WHERE city = '杭州' OR id > 900000;    -- ④ OR 跨列
EXPLAIN SELECT * FROM t_probe WHERE city > 'A' AND created_at = '2025-09-10';
                                                                     -- ⑤ key_len 只到 city
SHOW WARNINGS;                        -- 优化器改写后的 SQL 常常直接告诉你根因
SELECT index_name, is_visible FROM information_schema.statistics     -- ⑨ 索引被设成不可见
 WHERE table_schema = DATABASE() AND table_name = 't_probe';
```

---

## 09.9 架构边界：优化器管不到的地方

| 需求 | 优化器能不能 | 靠什么绕过 |
| --- | --- | --- |
| 任意维度过滤（10 个条件随便挑） | **不能**：B+ 树只有一条排序路径，索引组合是 2¹⁰（→ `comp/04` §04.9） | 倒排索引（→ `comp/21`）、列存、宽表预聚合 |
| 保证「这条 SQL 一定走这个索引」 | **不能**：成本是估算，两端都可能翻 | `FORCE INDEX` / hint 能锁住今天，但数据一变就过期；正解是让它变便宜 |
| 感知「刚写入的数据」 | **不能**：统计是异步快照，`auto_recalc` 由后台线程触发 | 导入/大批量变更后显式 `ANALYZE`，并写进发布流程 |
| 跨表统计（join 后的结果集分布） | **不能**：每张表独立统计，join 结果的基数靠「各表 `filtered` 相乘」猜 | 手工 hint、把中间结果落成带索引的临时表、或改写成两段查询 |
| 理解列与列之间的相关性 | **不能**（8.0 也没有多列直方图）；只有单列直方图 | 拆成覆盖索引、或把组合条件物化成派生列（`city_district`） |
| 知道你的盘有多快、缓存命中多少 | **不能**：`io_block_read_cost` 对随机/顺序/冷/热一视同仁（§09.3） | 只能整体改常数（副作用大），或换引擎（PG 有 `random_page_cost`） |
| 精确估计「`IN` 里 5,000 个值」的选择性 | **部分能**：`eq_range_index_dive_limit`（默认 200）超限后改用统计估算 | 拆成多个 `IN` 分批、或把值落进临时表再 join |
| 在多分片/分库之上做全局计划 | **不能**：优化器只看得见本实例的统计 | 中间件/数仓层做路由与归并（→ [eng/36](../eng/36-mysql-limits-sharding.md) §36.4.2） |
| 识别「`Using temporary` 落盘」的风险 | 只按常数计价（`disk_temptable_*`），不看 `tmpdir` 剩余空间 | 监控 `Created_tmp_disk_tables`，把大聚合搬去 OLAP（→ `comp/20`） |

**换掉它的信号**（每一条都对应本篇一条算式）：hint 超过 5 处且互相矛盾；同一条 SQL 在从库与主库计划不同；计划随数据增长反复翻转（`rows` 跨过一个阈值就翻）；`Last_query_cost` 与实测耗时长期差 10 倍以上；`Rows_examined/Rows_sent` 常年 > 1,000 而索引已经加无可加。出现以上任一条，说明**你的访问形态已经超出了「一棵 B+ 树 + 一个成本模型」的表达能力**——这时该做的是固化（物化视图、预聚合表、读模型），而不是继续和优化器谈判。

### 算账：全扫一次要多少钱，索引回表几行才回本

把 §09.3 的两个式子换成**物理时间**，就能得到「命中比例超过多少时索引反而不划算」的阈值。参数取自 `t_order`：N = 1 亿、R = 65、P = 1,538,462 页（23.5 GiB）。

| 场景 | 全扫一次 | 每命中行回表 | **物理阈值 p\*** |
| --- | --- | --- | --- |
| 冷池·散列（`hit=0`，每行一次随机页） | IO 12.6 s + CPU 30 s = **42.6 s** | **80 µs**（NVMe 单队列随机 16 KB） | **0.53%** |
| 热池·散列（`hit=99%`） | IO 1.7 s + CPU 30 s = **31.7 s** | **4.8 µs** | **6.7%** |
| 冷池·高相关（命中行挤在同一页，65 行/页） | 42.6 s | **1.23 µs** | **35%** |
| 热池·高相关 | 31.7 s | **0.073 µs** | **>100%（索引永远赢）** |
| **优化器自己算的换挡点** | 见 §09.3 | 0.40~1.15 成本单位 | **10% ~ 26%** |

推导式（可直接代入新假设）：

```
全扫时间 t_scan = P × 16,384 / 顺序带宽 + N × 每行 CPU
每行回表 t_row = (1−hit) × 随机页延迟 + hit × 池内访问
阈值   sel*   = t_scan / (N × t_row ÷ 相关性)
```

**量级结论**（假设清单：①行足迹 250 B ⇒ 65 行/叶；②顺序带宽 2 GB/s（冷）/ 15 GB/s（池内）；③随机 16 KB 页 80 µs（NVMe 单队列）、池内一行 4.8 µs；④每行「条件评估 + 元组组装」300 ns，按 InnoDB 单核 330 万行/s 计；⑤`LIMIT` 提前停止不计入）。换假设复算：把行足迹改成 1 KB ⇒ R = 15 ⇒ P 涨 4.3 倍 ⇒ `sel*` 也涨 4.3 倍（宽表上索引更早失效，与 `comp/04` §04.6 的 `p* = 1/(R·r)` 同构）。

三条能直接用在评审上的推论：

1. **阈值不是一个数，是一个 0.5%~35% 的区间**，由「池命中率 × 聚簇相关性 × 设备随机性能」三者决定。所以「命中超过 5% 就别用索引」这句经验只在「热池 + 中等相关」的中段成立——这也是为什么两个人的经验值会差 10 倍而各自都没错。
2. **优化的第一优先是「提高相关性」，而不是「换更快的盘」**：把 `p*` 从 0.53% 抬到 35% 靠的是让命中行挤在同几页（覆盖索引、MRR、`ORDER BY` 与索引同向），换成快 4 倍的盘只能把 80 µs 变 20 µs。
3. **MySQL 的结构性缺口**：PostgreSQL 用 `random_page_cost=4.0` / `seq_page_cost=1.0` 表达这个比值（NVMe 上建议把 `random_page_cost` 调到 1.1），**MySQL 只有一个 `io_block_read_cost`**，改它等于把所有页读等比放大，换挡点不动。所以在 SSD 上 MySQL 常常「该全扫的时候用索引」，只能靠 hint 或改写补——这不是你调参水平的问题，是模型缺了一个维度。

---

## 09.10 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到物理/模型） | 定位 | 修复 | 防复发 |
| --- | --- | --- | --- | --- |
| 批量导入/回补数据后，所有列表查询同时变慢 | 统计是异步快照，`n_rows` 停在导入前（§09.4） | 对比 `mysql.innodb_table_stats.last_update` 与数据变更时间；`EXPLAIN` 的 `rows` 与 `EXPLAIN ANALYZE` 的实际 `rows` | 导入后立刻 `ANALYZE TABLE` + 倾斜列刷直方图 | 把 `ANALYZE` 写进数据导入脚本的最后一步 |
| `ANALYZE` 之后更慢了 | `n_diff` 靠「不同值均分」假设，倾斜列被高估几十倍 | 看 `EXPLAIN` 的 `rows` 是否等于「`n_rows` ÷ 不同值个数」 | 加直方图（8.0）、提高 `persistent_sample_pages`、或建覆盖索引 | 倾斜列（状态/类型/租户）默认纳入直方图清单 |
| 加了索引没变快，写入还慢了 | `p` 超过 `p*`，优化器直接全扫；索引只贡献写放大（§09.9） | `possible_keys` 有、`key=NULL`；选择性 = `COUNT(DISTINCT col)/COUNT(*)` < 5% | 删索引，改「高基数列 + 排序列」的联合索引 | 建索引前先算选择性，< 5% 不建单列索引 |
| `EXPLAIN` 看着挺好，实际 4 s | `rows` 估小 100 倍，且它在 join 内层被 `loops` 放大（§09.6） | `EXPLAIN ANALYZE` 看 `loops` 与 `actual time × loops` | 先修统计再谈计划；join 内层缺索引就补 | 上线前用 `EXPLAIN ANALYZE` 在影子库跑一遍大表查询 |
| 同一 SQL 上午快下午慢 | 数据分布随时间漂移（`status` 从 PAID 批量转 REFUND），无直方图 | `Last_query_cost` 早晚两次对比；`EXPLAIN` 的 `filtered` 变化 | 直方图 + 定时重算；对时间敏感的查询加 `created_at` 范围 | 定时任务每天低峰刷统计与直方图 |
| `filtered` 显示 100% 但实际滤掉 99% | `filtered` 是乐观默认值，不是实测 | `EXPLAIN ANALYZE` 的 `rows×loops` 对比 `EXPLAIN` 的 `rows` | 补统计/直方图；改写谓词让它可估 | 评审时 `filtered=100` 一律要求给依据 |
| `IN (...)` 5,000 个值突然全扫 | `eq_range_index_dive_limit=200`（5.7.4+）超限后放弃索引下潜，改用统计；`range_optimizer_max_mem_size`（默认 8 MB）超限则退回全扫 | `SHOW WARNINGS` 里有 `Range checked for each record` 或内存不足提示 | 拆批（每批 ≤ 200）或把值写进临时表再 join | 代码层限制 `IN` 列表长度 |
| 从库上计划与主库不同 | 从库的统计是主库 `ANALYZE` 后的副本，回放又与主库不同步；`information_schema_stats_expiry=86400`（8.0）让缓存更旧 | 两实例比 `mysql.innodb_table_stats` 的 `last_update`、`n_rows` | 主从都跑统计任务；排障时 `SET information_schema_stats_expiry=0` | 统计刷新任务对主从都覆盖 |
| 上线一半实例慢、一半正常 | 隐式类型转换只在「新版本 DAO 传了数字」的实例上发生（§09.8 第 1 条） | 灰度实例的慢日志里同一 digest 出现两种 `Rows_examined` | 类型对齐；回滚相关变更 | DAO 层类型由代码生成，禁止手写拼接参数 |
| 生产上跑 `EXPLAIN ANALYZE` 把数据改了 | 8.0.18+ 的 `EXPLAIN ANALYZE` 会**真的执行**语句 | 事后对比 binlog 与操作记录 | 只在从库/影子库跑；DML 一律包事务不提交 | 把「`EXPLAIN ANALYZE` 只允许在从库执行」写进 Runbook |

---

## 09.11 观测：指标、拐点与告警

每条都带**版本与开关前提**——指标名写错一个，读者就会认为全篇没验证过。

| 手段 | 打开方式 / 前提 | 看什么 | 拐点与告警 |
| --- | --- | --- | --- |
| 慢查询日志 | `slow_query_log=ON`、`long_query_time`（默认 10 s；8.0 支持微秒）、`log_output=FILE`；**8.0.14+** 加 `log_slow_extra=ON` 才有 `Rows_examined`/`Rows_sent`/`Tmp_tables` 等字段 | `Query_time − Lock_time` 与 `Rows_examined` 的比值 | `Rows_examined/Rows_sent > 1000` 且 `Query_time > 100 ms` |
| `log_queries_not_using_indexes` | 依赖 `slow_query_log=ON`；5.7+ 可配 `log_throttle_queries_not_using_indexes`（默认 0 = 不限流） | **双刃**：5,000 QPS 的坏 SQL 一天能写数亿行日志，直接把磁盘写满、把 IO 抢走 | 只在排查窗口开 5 分钟；常态审计用 `sys` 视图而不是它 |
| `performance_schema.events_statements_summary_by_digest` | **8.0 默认 `performance_schema=ON`**，`statements_digest` consumer 默认 YES；5.7 需确认 `performance_schema=ON` 且 instrument 未被裁剪。`performance_schema_max_digest_length` 默认 1024 字节，超长 SQL 会被截断成同一个 digest | `SUM_TIMER_WAIT`、`COUNT_STAR`、`SUM_ROWS_EXAMINED`、`SUM_NO_INDEX_USED`、`SUM_CREATED_TMP_DISK_TABLES` | 单 digest 的 `SUM_ROWS_EXAMINED/COUNT_STAR > 1000` 进可疑清单 |
| `sys.statement_analysis` | `sys` schema 自带（5.7+）；依赖上面的 digest（8.0 默认开）。延迟单位是**皮秒** | `total_latency`、`avg_latency`、`rows_examined_avg`、`tmp_disk_tables` | 按 `total_latency` 排序取前 20，逐个看 `rows_examined_avg` |
| `sys.statements_with_full_table_scans` | 依赖 statement digest + `table_io_waits_summary_by_index_usage`（8.0 默认开） | `no_index_used_pct`、`rows_examined`、`avg_latency` | `no_index_used_pct > 50` 且平均延迟 > 100 ms |
| `sys.schema_unused_indexes` | 依赖 `table_io_waits_summary_by_index_usage`；**统计窗口 = 实例启动以来，重启清零** | 从未被任何语句使用过的索引 | 窗口 < 30 天不要删（偶发报表查询会假死） |
| `Handler_read_rnd_next` / `Handler_read_next` | `SHOW GLOBAL STATUS`；必须配 `Innodb_buffer_pool_reads` 才知道有没有真 IO | 全扫型访问的行数增速 | `Handler_read_rnd_next / QPS > 10⁴` 说明全扫已成常态 |
| `Last_query_cost` | `SHOW SESSION STATUS`；优化器自己算的成本值 | 与实测耗时的比值 | 比值 > 10 或 < 0.1 都说明模型失效 |
| `optimizer_trace` | `SET optimizer_trace='enabled=on'`，再查 `information_schema.OPTIMIZER_TRACE`（5.6+）；**只对单条 SQL 开**，开销大 | 每个候选计划的 `cost` 分项与 `rows_estimation` | 定位「为什么没选那个索引」的最后一招 |

```bash
# ① 慢日志摘要：按总耗时排序，只看前 5 类（避免逐条翻日志）
pt-query-digest --since 1h --order-by Query_time:sum --limit 5 /var/log/mysql/slow.log

# ② 全表扫最多的 digest：先看「行数/行产出」比例，再看耗时
mysql -e "SELECT query, exec_count, ROUND(avg_latency/1e9,1) AS avg_ms,
                 rows_examined_avg, no_index_used_pct
            FROM sys.statements_with_full_table_scans
           ORDER BY avg_latency DESC LIMIT 10"

# ③ 从未被用过的索引（P_S 默认 ON；重启清零，窗口太短会误杀）
mysql -e "SELECT * FROM sys.schema_unused_indexes WHERE object_schema='shop'"

# ④ pt-index-usage：把慢日志里每条 SQL 逐条 EXPLAIN，统计哪些索引一次都没被选中
pt-index-usage /var/log/mysql/slow.log --host=127.0.0.1 --user=dba --ask-pass
```

### 索引建议的机器化

四条工具，能力与边界必须一起记住：

| 工具 | 它能回答 | 它的局限（不记住就会误删） |
| --- | --- | --- |
| `sys.schema_unused_indexes` | 启动以来**一次都没被用**的索引 | 窗口 = 实例生命周期；季度报表、月度对账的索引会被误判；从库/影子库上的查询不计入 |
| `sys.statements_with_full_table_scans` | 哪些 SQL 在**该用索引时没用** | 只看「没走索引」的比例，不看「走了索引但选错列」；`no_index_used_pct` 高但选择性和结果集可能本来就该全扫 |
| 冗余索引判定（`(a)` vs `(a,b)`） | 前缀完全相同的短索引可删 | 唯一索引、外键需要的索引不能删；`(a)` 更窄、只读列更少，覆盖查询时反而更快 |
| `pt-index-usage` | 按慢日志样本逐条 `EXPLAIN`，反推「每个索引被哪些查询用到」 | **只看慢日志样本**：`long_query_time` 以下的好查询根本不进日志，于是它们的索引会被判为无用；报错日志里的 SQL 也不全 |

冗余索引的判定只需要一个前缀匹配（MySQL **8.0+** 才有 CTE）：

```sql
-- (a) 被 (a,b) 完全覆盖：前缀相同且更短 → 前者可删（唯一索引/外键相关除外）
WITH s AS (
  SELECT TABLE_SCHEMA ts, TABLE_NAME tn, INDEX_NAME iname, NON_UNIQUE nu,
         GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) cols, COUNT(*) n
    FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = 'shop' AND INDEX_NAME <> 'PRIMARY'
   GROUP BY 1,2,3,4)
SELECT a.tn AS table_name, a.iname AS redundant_index, a.cols AS covered_prefix,
       b.iname AS covered_by, b.cols AS full_columns
  FROM s a JOIN s b
    ON a.ts = b.ts AND a.tn = b.tn AND b.n > a.n
   AND LEFT(b.cols, CHAR_LENGTH(a.cols) + 1) = CONCAT(a.cols, ',')
 WHERE a.nu = 1;      -- 只挑非唯一索引；唯一索引即使前缀重复也不能删
```

**这条判据的两面**：删冗余索引是纯收益（写、池、盘三项同时降）；但「合并」两个索引（`(a)`、`(b)` → `(a,b)`）会改变现有查询的可用前缀——只有 `b` 条件的老查询会**丢掉定位能力**。合并前必须逐条验证 `EXPLAIN` 的 `key_len` 不变差。

---

## 09.12 一页速记

| 记什么 | 具体数字 / 结论 |
| --- | --- |
| 全扫成本 | `P × io_block_read_cost + N × row_evaluate_cost`；`P = ceil(N / R)`，`R = floor(16,256/行足迹)` |
| 索引成本 | `h × io + N·sel × (κ·io + key_compare + row_eval)`；`κ=1` 表示命中行完全散列 |
| 常数（8.0 默认） | `io_block_read_cost=1.0`、`memory_block_read_cost=0.25`（5.7 是 1.0）、`row_evaluate_cost=0.1`、`key_compare_cost=0.05` |
| 优化器换挡点 | `sel_opt*` = 10%（冷，`io`=1.0）~ 26%（热，`io`=0.25） |
| 物理换挡点 | 0.53%（冷·散列）/ 6.7%（热·散列）/ 35%（冷·高相关）；经验值 5%~20% 只是中段 |
| 全扫的真实瓶颈 | CPU 占 87%~96%：1 亿行全扫 ≈ 30 s CPU + 1.7~12.6 s IO ⇒ **换快盘救不了全扫** |
| 统计采样 | `persistent_sample_pages=20` ⇒ 1 亿行表只采 ~1,300 行；1% 的值相对误差 ±28%，0.01% 的值几乎必然抽不到 |
| 直方图 | 8.0 专有，默认 100 桶、最多 1024；**永不自动更新**，只有手工 `UPDATE HISTOGRAM` |
| `key_len` 反推 | 逐列累加字节：`VARCHAR(n)` utf8mb4 = `4n+2(+1)`；对到哪个前缀就是用到第几列 |
| 真实扫描量 | `rows × filtered`，再乘进 join 的 `loops`；`filtered=100` 常常是「估不出来」而非「全留」 |
| `EXPLAIN ANALYZE` | 8.0.18+，TREE only，**会真执行**；总耗时 = `actual time` 末值 × `loops` |
| Join 放大 | 中间结果 100 倍 ⇒ 耗时 30 倍（事故三：1,000 万次探测 = 12 s） |
| Hash Join | 8.0.18+（内连接）、8.0.20+（覆盖 outer/semi/anti，BNL 已移除）；受 `join_buffer_size`（256 KB）约束，溢出落盘 |
| 子查询改写 | `derived_merge`（5.7+）、`derived_condition_pushdown`（8.0.22+）、半连接（5.6+）、antijoin（**8.0.17+**） |

**如果只记五句：**

1. **优化器不判断「能不能用索引」，它只比较两个数字**——所以「索引失效」的正确问法是「哪个数被估错了」。
2. **它算错的不是加减法，是三个它看不见的量**：随机与顺序的差（常数里没有）、缓存命中的差（0.25 与 1.0 拍脑袋）、`rows` 本身的误差（20 页采样 + 均分假设）。
3. **全扫的瓶颈是 CPU 不是 IO**，这就是为什么加更快的盘、加更多内存都治不好「全表扫太慢」。
4. **join 的错会被 `loops` 放大**：`EXPLAIN` 里一行 `eq_ref rows=1`，实际可能被执行 1,000 万次。
5. **修计划靠改输入，不靠改命令**：刷统计、加直方图、把过滤列与排序列放进索引、把谓词改写成可定位形状；`FORCE INDEX` 只是把今天的结论硬编码进 SQL。

---

## 09.13 自检题（5 题）

**Q1.** 索引 `KEY idx_su (status VARCHAR(16) NULL, user_id BIGINT NOT NULL, created_at DATETIME NOT NULL)`（表为 utf8mb4）。三条 SQL 的 `key_len` 分别是 67、75、80，反推各用到了哪几列；并解释为什么「`status` 那列是 `NULL` 还是 `NOT NULL`」会让这三个数字都变。

<details><summary>答案</summary>

宽度推导：`status = 16×4 + 2（长度前缀）+ 1（NULL 位）= 67`；`user_id = 8`；`created_at = 5`。

- `key_len=67` ⇒ 只用到 `(status)`；
- `75 = 67 + 8` ⇒ 用到 `(status, user_id)`；
- `80 = 67 + 8 + 5` ⇒ 三列全用上。

若 `status` 改成 `NOT NULL`，NULL 位消失 ⇒ 三档全部 −1，变成 66 / 74 / 79。所以「`key_len` 对不上任何前缀」时，第一件要查的不是索引，是 `SHOW CREATE TABLE` 里的 `NULL` 与字符集：utf8mb3 的同一列是 `16×3+2+1 = 51`，差了 16 字节，足以让对号失败。

补充：InnoDB 二级索引叶子隐含带主键，但 `key_len` **不计**主键，所以 `key_len` 恰好等于用户列之和是正常的。
</details>

**Q2.** 一张 1.02 亿行、行足迹 250 B 的表，`status` 有 4 个值且严重倾斜（REFUND 只占 0.6%）。为什么 `ANALYZE TABLE`（`persistent_sample_pages=20`）之后 `rows` 反而从 255,000 涨到 25,500,000？算一算采样期望，并说出两个不同的补救手段各自解决了哪一层。

<details><summary>答案</summary>

20 页采样 = `20 × 65 = 1,300` 行。REFUND 的真实占比 0.6% ⇒ 采样里期望命中 `1,300 × 0.006 ≈ 7.8` 行。用 7.8 行去推 1.02 亿行的分布，相对标准差 ≈ `1/√7.8 ≈ 36%`——**但这还不是 41.7 倍误差的来源**。

真正的原因在下一层：`ANALYZE` 之后 `n_rows` 修对了（1.02 亿），而 `n_diff` 只有 4，优化器按**「不同值均分」**估等值谓词 ⇒ `1.02 亿 / 4 = 25,500,000`。真实值 612,000，**高估 41.7 倍**。之前的 255,000 是拿错的 `n_rows`（102 万）除以 4，两个错刚好互相抵消了一部分。

两层补救：
1. **直方图（8.0）**：`ANALYZE TABLE ... UPDATE HISTOGRAM ON status WITH 100 BUCKETS`。数据倾斜时生成 `singleton` 桶，REFUND 单独成桶 ⇒ `rows = 612,000`，**解决「分布倾斜」这一层**。缺点：永不自动更新，必须定时刷。
2. **提高采样页数**（`innodb_stats_persistent_sample_pages` 从 20 提到 200 甚至更高）：让 `n_diff` 与 `n_rows` 更准，但**解决不了均分假设**——`n_diff` 再准，等值估算仍然是除法。所以它只治「基数估错」，不治「倾斜」。

第三层（本例真正的赢法）：**建覆盖索引 `(status, id, created_at)`**，让 `rows=612,000` 时查询变成 index-only scan——即使估得不准，代价也可控。**统计治「估得准」，索引治「错得起」。**
</details>

**Q3.** 用 §09.9 的假设算：1 亿行、行足迹 250 B 的表，冷池（`hit=0`，随机 16 KB = 80 µs）与热池（`hit=99%`，池内一行 4.8 µs）下，全扫与索引回表的换挡点各是多少？并解释为什么「命中 8% 走索引」在冷池上是灾难、在热池上却是合理选择。

<details><summary>答案</summary>

`R = floor(16,256/250) = 65`，`P = ceil(10⁸/65) = 1,538,462` 页 = 23.5 GiB。

全扫：冷 = `23.5 GiB / 2 GB/s = 12.6 s` IO + `10⁸ × 300 ns = 30 s` CPU = **42.6 s**；热 = `23.5 GiB / 15 GB/s = 1.7 s` + 30 s = **31.7 s**。注意 CPU 项占 70%~96%。

阈值：`sel* = t_scan / (N × t_row)`。

- 冷池：`42.6×10⁶ µs / (10⁸ × 80 µs) = **0.53%**`；
- 热池：`31.7×10⁶ µs / (10⁸ × 4.8 µs) = **6.7%**`。

命中 8% 时：热池里 `8% × 10⁸ × 4.8 µs = 38.4 s ≈ 31.7 s`（略贵，临界），所以「热池上选索引」大体合理；冷池里 `8% × 10⁸ × 80 µs = 640 s`，是全扫 42.6 s 的 **15 倍**——灾难。

结论：**阈值随命中率在 0.5%~35% 之间漂移，13 倍差距；而缓存命中率只改变了一个乘数。** 这也解释了为什么同一个 SQL 在测试环境（表小、全在池里）好好的，上了生产（表远超池）就崩。
</details>

**Q4.** 事故三的查询在两种数据下会得出相反的驱动表结论。给定 `t_user` 200 万行、`t_order` 1,000 万行，请构造一组条件让它「应该由 `t_order` 驱动」（并算出代价量级），并指出优化器最可能因为哪个估算误差而选错。

<details><summary>答案</summary>

构造：`WHERE u.city = '杭州'` 命中 200 万（100%，城市分布极不均，或干脆没有城市过滤）而 `WHERE o.amount > 5000` 只命中 6 万（0.6%）。

- `t_order` 驱动：6 万次 `t_user` 主键探测 × 1.2 µs = **72 ms**，中间结果 6 万行 → 总 < 0.2 s；
- `t_user` 驱动：200 万 × 平均 5 行 = **1,000 万中间行**，再有索引探测 200 万 × 1.5 µs = 3 s + 回表 1,000 万 × 2 µs = 20 s ⇒ 4~20 s。

量级：**中间结果 167 倍，耗时 20~100 倍。**

最可能的估算误差：`o.amount > 5000` 这一侧。`amount` 没有直方图，范围条件只能按「最小值到最大值线性插值」或旧统计估 ⇒ 真实 0.6% 的选择性被估成 20%~50%，于是 `t_order` 过滤后看起来还有 200 万~500 万行，与 `t_user` 的 200 万「差不多」，模型就随便选了一个（通常按表顺序或按页数），事故就发生了。

判据：`EXPLAIN` 里看驱动表那行的 `rows × filtered`，不要看 `n_rows`；再用 `EXPLAIN ANALYZE` 的 `loops` 验证真实驱动行数。
</details>

**Q5.** `EXPLAIN ANALYZE` 输出里有一行 `-> Single-row index lookup on u using PRIMARY (actual time=0.012..0.012 rows=1 loops=102000000)`。有人据此判断「这一步只花了 0.012 ms，可以忽略」。这个判断错在哪？把它换算成真实耗时，并说明为什么 `EXPLAIN`（非 ANALYZE）在这一步上会给出「看起来很便宜」的结论。

<details><summary>答案</summary>

错在把**每次循环的平均值**当成了总时间。`actual time=0.012..0.012` 是每一次探测的平均耗时，`loops=102,000,000` 是执行次数：

```
真实耗时 = 0.012 ms × 102,000,000 = 1,224,000 ms ≈ 1,224 s ≈ 20 分钟
真实行数 = rows × loops = 1 × 102,000,000 = 1.02 亿行
```

（实际测到 8,260 ms 是因为 SQL 在 612 行就满足了过滤/聚合，或者循环被提前打断；这说明 `loops` 本身也是估算——但量级判断的依据不变：**这里是 1 亿次循环，不是 1 次**。）

`EXPLAIN` 之所以「看起来便宜」：它给的是 `type=eq_ref` + `rows=1`，而 `eq_ref` 的定义就是「每行唯一匹配 1 行」。这个 `rows=1` 是**单次访问**的行数，完全没有乘以驱动行数——把驱动表那行的 `rows × filtered` 乘进去才是总代价。所以：**`EXPLAIN` 里被驱动表的 `rows` 永远要乘以驱动表的输出行数**，而驱动表的输出行数正是优化器最容易估错的那个量（事故三：300 万估成 1,000 万，错 3.3 倍；探测次数错 10 倍）。
</details>

---

## 09.14 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 索引本质、最左前缀的形式化判定、覆盖索引、ICP/MRR 的机制 | [comp/04](04-index-essence-btree-page.md) §04.5–§04.7 |
| 「命中比例超过多少不该走索引」的 IO-only 下界推导 `p* = 1/(R·r)` | [comp/04](04-index-essence-btree-page.md) §04.6 |
| 页与行格式、行足迹怎么算、二级索引为什么是「主键表」 | [comp/05](05-innodb-page-and-row.md) §05.3–§05.6 |
| MVCC：为什么长事务会让统计与 purge 一起失真 | [comp/06](06-mvcc-and-isolation-levels.md) §06.7 |
| 锁加在索引记录上：为什么「走错索引」还会放大锁范围 | `comp/07` §07.4 |
| redo/binlog 与提交顺序（`ANALYZE`、DDL 对复制的冲击） | `comp/08` |
| 复制延迟与并行回放（从库统计不同步为什么是必然） | `comp/10` |
| 单表行数拐点、B+ 树容量与分片时机（什么时候「优化器再准也没用」） | [eng/36](../eng/36-mysql-limits-sharding.md) §36.4.1 |
| 存储与索引形态决策树（B+ 树撑不住时换什么） | [architect/03](../architect/03-storage-and-index-shape.md) |
| 倒排索引：`LIKE '%x%'` 这类「排序帮不上」的查询该去哪 | `comp/21`、`comp/22` |
| 列存与向量化：聚合型全扫为什么该离开 InnoDB | `comp/20` |
| 全书总纲、解剖卡八问与写作纪律 | [0-summary](0-summary.md) §0.4、§0.7 |
