# 06. MVCC 与隔离级别：undo 版本链与 Read View

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · 关系库与事务内核 ｜ **编号**：06 / 53
> **本篇定位**：全仓唯一一处把「可重复读」拆到字节级的地方。它不教你怎么写事务——Spring 事务生效的四个前提、大事务怎么拆、Saga 与 TCC 怎么选，已经在 [eng/7 本地事务](../eng/7-local-transaction-pitfalls.md) 与 [eng/9 事务决策树](../eng/9-transaction-decision-tree.md) 里讲透了，本篇只给机制 + 锚点。它回答一个内核问题：**同一行数据在磁盘上只有一份，为什么两个事务能同时看到两个不同的版本；而这份"旧版本"最终是谁在付钱**。读完之后，「长事务为什么要杀」「RC 为什么死锁少」「statement 格式为什么不能配 RC」「逻辑备份为什么拖慢主库」这四件看似无关的事，会收敛成同一个结论。
> **读法**：§06.2 的唯一真问题是全篇支点，一句话记不住下面的算式都是白算；§06.3 的版本链图与 §06.4 的四步可见性判定要能默画，这是面试与评审的唯一硬通货；正在救火的直接跳到 §06.7 的算账与 §06.9 的清单；§06.11 的判据表可以单独贴进选型文档。

---

## 06.1 引子：三起「事务没报错，但数据不对」的故障

MVCC 的故障有一个共性：**没有一条日志指向 MVCC**。undo 写满报的是磁盘错误，主从不一致报的是对账差额，超卖报的是业务规则。机制藏在第三层因果里，所以每一次复盘都会先怪到别的地方去。

### 事故一：一个跑 5 小时 47 分的批处理，把 undo 表空间撑到 198 GB（2025-11-18）

某 SaaS 订单中心，MySQL 8.0 单主（32C128G，数据盘 2 TB NVMe，另外按官方推荐把 undo 单独挂在一个 **210 GB** 的卷上——`innodb_undo_directory`），订单明细表 3.4 亿行、行均 340 B、**6 个二级索引**。当晚跑「历史订单金额重算」：一个事务里 `for` 循环逐笔 `UPDATE order_item SET amount=? WHERE id=?`，12 万笔一批但**没有提交**——作者以为「循环完再提交更快」。

同时报表服务开着 `START TRANSACTION` 全表遍历做导出，跑了两小时。于是全库的更新速率是 1.2 万行/s，而 purge 的边界被钉在 09:05。

| 时间 | 事件 | HLL（history list length） | undo 表空间 | undo 卷占用 |
| --- | --- | --- | --- | --- |
| 11-18 09:12 | 批处理开始，1.2 万行/s；报表事务（09:05 开始）仍开着 | 3 万 | 12 GB | 6% |
| 11-18 09:40 | purge 无法越过 09:05 的那个视图，HLL 单调上涨 | 2,000 万 | 28 GB | 13% |
| 11-18 10:30 | undo 表空间按需扩展，`innodb_undo_log_truncate` 被卡住不生效 | 5,600 万 | 57 GB | 27% |
| 11-18 12:00 | undo 增速稳定在 9.6 MB/s（= 1.2 万行/s × 800 B），HLL 破亿 | 1.2 亿 | 109 GB | 52% |
| 11-18 14:20 | purge 完全停滞（最早视图仍在 09:05），缓冲池被 undo 页挤占 | 2.2 亿 | 190 GB | 90% |
| 11-18 14:59 | undo 卷 100%，主库写入报 `OS error 28 No space left on device` | 2.5 亿 | 209 GB | 100% |
| 11-18 15:10 | `KILL` 最老事务；回滚开始（不是瞬间，见 §06.7） | 2.5 亿 | 209 GB | 100% |
| 11-18 15:20 | rollback 完成，purge 全速：4 线程 × 2 万条/s | 2.5 亿 → | 缓慢回落 | 100% |
| 11-18 16:40 | HLL 回落到 3 万，写入恢复；**不可用 11 分钟，写入失败 640 万次** | 3 万 | 24 GB | 11% |

算一遍就明白 200 GB 不是意外（每条 `UPDATE` 在 undo 里留一条 update undo：记录头 5 B + `trx_id` 6 B + `roll_ptr` 7 B + 旧值 340 B = 358 B；undo 记录不能跨页，加上页头/回滚段段头/对齐空洞，16 KB 页的**有效利用率只有约 45%**）：

| 量 | 推导 | 结果 |
| --- | --- | --- |
| 每条 update undo 占盘 | 358 B / 45% 页利用率 | ≈ 800 B |
| 事务时长 | 09:12 → 14:59 | 20,820 s |
| 版本数 | 12,000 行/s × 20,820 s | ≈ 2.50 亿 |
| undo 增量 | 2.50 亿 × 800 B | ≈ 200 GB（监控 198 GB，含基线 12 GB） |
| 二级索引淤积 | 2.50 亿 × 6 个索引 × 30 B（purge 停滞，delete-mark 只增不减） | ≈ 45 GB（在数据盘，不在 undo 卷） |
| 盘占用合计 | 200 + 45 | ≈ 245 GB，其中 undo 卷先被写满 |

关键不是「事务大」，而是**这个事务是一个活着的快照**：purge 的边界是最老 Read View，只要它不提交，从它开始时点之后产生的所有旧版本**一条都不能回收**。undo 表空间的增长速率 = 每秒更新行数 × 每行 undo 字节，与「事务改了多少行」无关——改 12 万行也可能只留 12 万条版本，但**这 5 小时里全库所有的更新**（2.5 亿条）都被它钉住了。

### 事故二：RC + STATEMENT 格式，主从差了 4.3 万行（2023-05-19，某票务系统）

为了压住 RR 间隙锁带来的死锁率（高峰每分钟 40 次死锁超时），DBA 在 MySQL 5.7 上把 `transaction_isolation` 从 `REPEATABLE-READ` 改成 `READ-COMMITTED`，并打开 `innodb_locks_unsafe_for_binlog=ON`（减少 UPDATE 死锁的老药方）。**`binlog_format` 还是 `STATEMENT`**，因为「这库一直是这么配的，没出过事」。

出事的语句长这样：`UPDATE seat SET ver = ver + 1 WHERE status > 0` —— 一条扫 3,000 行的热点 UPDATE。

| 环节 | 主库（RC + 半一致读，有锁竞争） | 从库（STATEMENT 重放，无锁竞争） |
| --- | --- | --- |
| 扫描时刻 | 09:12:01，S2 已把 `id=7` 的 `status` 从 0 改成 3 但**未提交** | — |
| 对 `id=7` 的判断 | 读到**已提交版本** `status=0` → 不满足 `status>0` → 半一致读：跳过、不等待、不加锁 | — |
| S2 提交 | 09:12:03（`status=3`） | 先应用 S2 的 UPDATE |
| S1 提交 | 09:12:05，命中 **1,204 行** | 再重放 S1 的语句：此时 `id=7` 已 `status=3` → 命中 **1,206 行** |
| 每次差异 | — | +2 行，且 S1 与 S2 的提交顺序不写在 statement 里 |

单次差 2 行，这条 SQL 一天约 1,500 次（只有热点批次才走它），两周后对账：从库 `seat` 表比主库**多 43,212 行 `ver` 增量**（1,500 × 2 × 14 ≈ 4.2 万，与差值的量级吻合），库存计算在从库上跑出 1,700 张票的可售量，超卖赔了 68 万元。

**注意现场有多不显眼**：`SHOW SLAVE STATUS` 全绿、`Seconds_Behind_Master=0`、没有任何复制错误。STATEMENT 格式的语义是「把 SQL 重放一遍」，而 RC + 半一致读让**同一句 SQL 在主从两侧能命中不同的行集**。

补一句现状：MySQL 8.0 已经把这个组合**物理禁掉**——RC 下写语句会直接报
`Cannot execute statement: impossible to write to binary log since BINLOG_FORMAT = STATEMENT and at least one table uses a storage engine limited to row-based logging. InnoDB is limited to row-logging when transaction isolation level is READ COMMITTED or READ UNCOMMITTED.`
`innodb_locks_unsafe_for_binlog` 也在 8.0 删除（RC 永远用半一致读、永远关间隙锁）。**但你要知道它保护的是什么**：不是 RC 有问题，是「把 SQL 而不是行的最终状态投递给从库」这件事在 RC 下没有唯一解。所以线上只剩一条基线：`binlog_format=ROW` + RC 或 RR。

### 事故三：以为 RR 不会幻读，库存超卖 3,100 件（2024-11-11，某生鲜电商）

「MySQL 默认 RR，RR 解决了幻读」这句话被写进了技术方案评审 PPT。于是秒杀校验写成了：先 `SELECT` 判断批次余额（**快照读**），业务层判断 `available > 0`，再 `UPDATE` 扣减（**当前读**，但 WHERE 里没有余额条件）。

| 时刻 | 会话 A（RR） | 会话 B（RR） | 结果 |
| --- | --- | --- | --- |
| T1 | `BEGIN; SELECT available FROM batch WHERE sku=88` → **1**（建立快照） | — | A 认为有 1 件 |
| T2 | （业务判断：可卖） | `BEGIN; SELECT ...` → 1 | B 也认为有 1 件 |
| T3 | — | `UPDATE batch SET available=available-1 WHERE sku=88` → 0，COMMIT | 真实余额 0 |
| T4 | `UPDATE batch SET available=available-1 WHERE sku=88` → **-1**，COMMIT | — | 超卖 |
| T5 | （下一句）`UPDATE ... WHERE sku=88 AND status=0` | 运营补货，插入新批次行 `sku=88` 并提交 | A 的当前读**命中这条新行**（当前读看得见它），幻读成立 |

两个错叠在一起：RR 的快照让 A 读到了「别人的提交看不见」，于是**检查与执行之间隔着一次别人的提交**；而 `UPDATE` 的当前读又看得见新插入的行。3,100 件超卖，赔付 42 万元。

一句话：**RR 保证的是「你这个事务里反复读到的行长得一样」，不是「你读到的东西别人改不动」，更不是「你的检查在执行那一刻还成立」。** 三起事故分别对应本篇三条主线：**版本链与 purge（§06.3、§06.7）**、**Read View 的创建时机（§06.5）**、**快照读管不到当前读（§06.6）**。

---

## 06.2 一句话定位与唯一真问题

**定位**：MVCC（多版本并发控制）是 InnoDB 用「**旧版本不删、挪到 undo、按快照挑一个给你看**」换来的「读不阻塞写、写不阻塞读」。它不是一种锁，是一套**用磁盘空间换读并发的记账系统**。三个"不是"：不是快照隔离的完整实现（不防写偏斜）、不是时间旅行（purge 之后旧版本永久消失）、不是免费的（账单以 undo 表空间和 purge 线程的形式按月结算）。

### 唯一真问题

> **可重复读不是「锁住了」，是「给你一个旧版本视图」；而快照活多久，决定了 undo 表空间会不会涨爆。**

这一句要拆成三截来用：

1. **「不是锁住了」**：RR 下的普通 `SELECT` 一行锁都不加、一个字节都不写、不阻塞任何人的写。它只是拿一个 Read View 去判断「这行的当前版本我能不能看，不能看就顺着指针往回翻」。所以「RR 隔离级别高所以并发差」这句流传二十年的话是错的——RR 的并发差来自**当前读**（`FOR UPDATE`/`UPDATE`/`DELETE` 的间隙锁，→ comp/07），不来自快照读。
2. **「给你一个旧版本视图」**：视图的边界是一个四字段结构（§06.3），它把「谁提交了」这件事冻结在某一瞬间。冻结的那一瞬间就是快照。
3. **「快照活多久」**：快照不解散，它开始时点之后产生的所有旧版本都不能被 purge 回收——**undo 的增长速率只跟你每秒改多少行有关，跟你这个事务改了多少行一点关系都没有**（事故一里那个事务只改自己那 12 万行量级的业务范围，却钉住了全库 2.5 亿条版本）。

从这一个矛盾推出全部使用纪律：

| 从矛盾推出的约束 | 推出的设计 |
| --- | --- |
| 快照 = 一笔磁盘账单，按时间计费 | 事务时长必须是**被监控的一等指标**（60 s 告警），而不是"业务需要" |
| 只读事务一样钉住 purge（它持有 Read View） | 「只读不写所以不会影响别人」是错的；报表、导出、逻辑备份都是隐藏的长事务 |
| 旧版本只在 undo 里，purge 之后就没了 | MVCC **不能**当审计/历史查询用；要历史就建时间表或走 CDC（→ comp/33） |
| 快照读不加锁 ⇒ 检查与执行之间可以被插入 | 决策必须落进**带条件的原子写**（`WHERE stock >= 1`）或先 `FOR UPDATE`（[eng/7 §7.9](../eng/7-local-transaction-pitfalls.md)） |
| 快照读只保证行内容，不保证行的集合 | 幻读要靠 next-key lock，而它只在**当前读**上生效（§06.6） |
| RC 每条语句重建视图 ⇒ purge 边界随语句推进 | RC 的 undo 压力天然低一个量级，这是它成为互联网首选的真实原因 |
| 视图边界只在本实例内有效 | 跨库/跨实例的「一致读」必须靠全局时间戳（→ comp/10、comp/19） |

### 八问对照（本篇答哪几问）

| 八问 | 落在哪节 | 一句话答案 |
| --- | --- | --- |
| ① 承诺什么 | §06.2、§06.8 | 承诺「同一快照内读到同一行内容」；不承诺「读到最新的行集合」 |
| ② 数据长什么样 | §06.3 | 行上 13 B 隐藏列（6 B trx_id + 7 B roll_ptr）+ undo 里的旧版本链 |
| ③ 一次读走哪条路 | §06.4 | 判 Read View → 不可见则沿 roll_ptr 回溯，命中 3 µs，回溯 100 版本 ~300 µs |
| ④ 并发与一致 | §06.3、§06.5 | 版本链 + 快照；失效窗口 = 快照期间的所有提交对你不可见 |
| ⑤ 会在哪崩 | §06.7、§06.9 | undo 表空间写满盘、purge 停滞、从库回放被大事务拖死 |
| ⑥ 看哪几个指标 | §06.10 | 最老事务年龄、HLL、undo 日增、purge 反推速率、从库延迟 |
| ⑦ 换掉它的信号 | §06.8 | 需要跨实例快照 / 需要历史版本 / 需要免锁检查后写 ⇒ 换模型或加协调层 |

---

## 06.3 物理形态：版本链与 Read View

### 行上的 13 个字节

`COMPACT`/`DYNAMIC` 行格式里，除业务列外每行固定带两个隐藏列：

| 字段 | 尺寸 | 作用 | 细节 |
| --- | --- | --- | --- |
| 记录头 | 5 B | 其中 **1 个 delete-mark 位**，2 B 堆号，2 B 页内下一条偏移 | delete 不是物理删除，是**置位** |
| `DB_TRX_ID` | 6 B | 最后修改这一行的事务 id（48 bit，单调递增） | 提交了也不清空，它**就是**版本号 |
| `DB_ROLL_PTR` | 7 B | 指向 undo 里「这行的上一个版本」 | 位布局：1 bit 是否 insert undo + 7 bit 回滚段号 + 4 B undo 页号 + 2 B 页内偏移 |
| `DB_ROW_ID` | 6 B | 无主键/唯一非空索引时才有 | 表没主键 → **每行多 6 B 且无全局序**，→ comp/05 |

7 字节的 `roll_ptr` 里 7 bit 是回滚段号，所以 InnoDB 的 rollback segment 编号上限是 **128**；每个 rseg 有 **1,024 个 slot**，一个正在写的事务占一个 slot ⇒ 理论并发写事务上限 128 × 1,024 ≈ **13.1 万**。默认 2 个 undo 表空间 × 每空间 32 个 rseg = 64 个在用（≈ 6.5 万并发写事务位），把自己配到 6 万并发连接的那种系统，迟早会撞到 `ER_TOO_MANY_CONCURRENT_TRXS`。**这个数字上限不是调优项，是物理常量。**

### 版本链：一行数据的三段历史

**图 1 · 行记录 → undo 记录 → 更早的 undo 记录**

```
  聚簇索引叶子页（16 KB，主键 id=1）              回滚段里的 undo 页
 ┌────────────────────────────────┐
 │ 记录头 5 B   [delete-mark=0]   │
 │ DB_TRX_ID     = 1082           │
 │ DB_ROLL_PTR   ─────────────────┼──►┌────────────────────────────────────┐
 │ id=1，stock=7                  │   │ undo record #3（update undo）      │
 └────────────────────────────────┘   │ 它还原的是事务 1043 写下的那版      │
                                      │ 旧值：stock=10                     │
  事务 1082 自己来读：                 │ 上一版 trx_id=1010 / roll_ptr ─────┼──┐
  1082 == creator_trx_id → 直接可见    └────────────────────────────────────┘  │
                                      ┌────────────────────────────────────┐  │
  1080 时刻建立的快照来读：            │ undo record #2（update undo）      │◄─┘
  当前版本 1082 不可见 → 顺 roll_ptr    │ 它还原的是事务 1010 写下的那版      │
  → #3 记的是 1043，1043 < 1080 可见   │ 旧值：stock=12                     │
  → 用 #3 的旧值构造出 stock=10 返回    │ 上一版 roll_ptr ─────────────────┼──┐
                                      └────────────────────────────────────┘  │
                                                              ...             ▼
                                      ┌────────────────────────────────────┐
                                      │ insert undo（这行的出生记录）        │
                                      │ 提交后即可丢弃（没人看得见未出生的行）│
                                      └────────────────────────────────────┘
```

三个必须记住的推论：

1. **读旧版本 = 一次链式回溯，代价与链长成正比。** 命中当前版本约 3 µs（缓冲池内一次页访问），每回溯一个版本 +2~3 µs（一次 undo 页访问 + 一次行构造）。链长 100 时单行读成本 ≈ 300 µs，是命中路径的 100 倍——这就是「长事务 + 热点行」能让 `SELECT` 变慢的全部原因，跟索引、跟执行计划没关系。
2. **insert undo 提交即可丢弃，update undo 必须留到没人看为止。** 插入的行在提交前对别人不可见，所以它的 undo 只服务于回滚；一旦提交，回滚不可能发生，这条 undo 当场可以回收。而 `UPDATE`/`DELETE` 产生的 update undo 是**历史版本**，只要还有 Read View 可能读到旧值，它就必须留在 undo 里并挂进 history list —— 「history list length」统计的就是这批**已提交但尚不能被回收**的版本数。
3. **`delete` 是置位 + 留一条 undo。** 删除时把记录头的 delete-mark 置 1，`DB_ROLL_PTR` 换成一条 undo 记录（记录的是「这条原本没被删」这个事实）。于是：索引页上仍然有这条记录、查询扫到它要走 MVCC 判定、purge 要等所有快照都不需要它了才真正删。**二级索引记录上没有 `DB_TRX_ID`**，所以 purge 判断「这条 delete-mark 的索引记录能不能删」时**必须回聚簇索引查版本链**——一张表 6 个二级索引，purge 的成本就是 6 倍回表，这是事故一里「42 分钟才消化完 2.5 亿条 undo」的直接原因。

### Read View：四个字段，把「谁提交了」冻结在某一刻

**图 2 · Read View 结构与四步可见性判定**

```
Read View（RR：一个事务一份；RC：一条语句一份）
┌──────────────────────────────────┬───────────────────────────────────────────┐
│ m_ids         = [1080, 1082]     │ 创建这一刻仍在活跃（未提交）的事务 id 列表   │
│ min_trx_id    = 1080             │ = m_ids 最小值（空则为创建时的下一个 id）     │
│ max_trx_id    = 1085             │ 创建时「下一个将被分配」的 id ⇒ ≥ 它的都是   │
│                                  │ 视图之后才开始的事务                        │
│ creator_trx_id= 1083             │ 我自己；只读事务在 8.0 里可为 0              │
└──────────────────────────────────┴───────────────────────────────────────────┘
                    行上的 DB_TRX_ID = X
                              │
        X == creator_trx_id ？ ──是──► 可见      （自己改的：不管提没提交）
                              │否
        X  <  min_trx_id     ？ ──是──► 可见      （建视图前就已提交完毕）
                              │否
        X  >= max_trx_id     ？ ──是──► 不可见    （建视图后才开始的事务）
                              │否
        X  ∈  m_ids          ？ ──是──► 不可见    （建视图时它还活着，后来提交也不行）
                              │否
                              └─────────► 可见      （id 小但是活的？不可能；此处 = 建视图前
                                                   已提交，只是 id 落在 [min,max) 区间内）
```

四步规则写成一句人话：**比 `min_trx_id` 小的一律可见（老古董），比 `max_trx_id` 大的一律不可见（未来的事），夹在中间的只看一件事——创建视图那一刻它是否还活着，活着就永远不可见，已经提交就可见。**

为什么「能看见自己的修改」：判断链的第一条就是 `X == creator_trx_id`。你自己改出来的行上写着你的 id，所以快照读会看见——**这就是「事务内读到自己未提交的写」的全部机制，它不是特例，它是规则的第一个分支**。副作用也在这里：一个事务里 `UPDATE` 了 100 万行再 `SELECT` 全表，它要读的版本可能分布在 undo 的多层，而且这些修改对别人全不可见，直到提交。

还有一条容易踩的时序：**快照不是在 `BEGIN` 建立的，是在「第一条真正读 InnoDB 表的语句」建立的。** `BEGIN` 之后 `SELECT 1`、算算术、调 RPC、睡 5 分钟，快照都没建；第一条 `SELECT * FROM t` 才把四个字段冻住。反过来，`START TRANSACTION WITH CONSISTENT SNAPSHOT` 才会在 BEGIN 那一刻就冻结。**这决定了「事务年龄」和「快照年龄」是两个不同的数**，而只有后者决定 purge 的边界。

---

## 06.4 一次快照读的完整链路

一条 `SELECT stock FROM inventory WHERE id=1` 在 InnoDB 里的实际跳数（缓冲池全命中的情况）：

| # | 跳 | 耗时量级 | 放大点 |
| --- | --- | --- | --- |
| 1 | 客户端 → server 层，解析、鉴权、优化器选索引 | 30~80 µs | 优化器是 CPU 活，与 MVCC 无关（→ comp/09） |
| 2 | 拿 Read View（RR 已缓存则 0 成本；RC 每条语句重建） | 0.5~2 µs | **RC 每语句一次，这是 RC 的唯一额外开销** |
| 3 | B+ 树下降到叶子页（缓冲池命中） | 1~3 次页访问，2~5 µs | 树高 3~4 层是常量，→ comp/05 |
| 4 | 读出记录头里 `DB_TRX_ID` | 纳秒级 | 已在 cache line 里 |
| 5 | 四步判定（图 2） | < 0.2 µs | 一次位比较 + 一次二分/数组扫描（`m_ids` 长度 = 并发活跃事务数） |
| 6 | 不可见 → 沿 `DB_ROLL_PTR` 取 undo 记录，取旧值，回填出一个「临时记录」 | 2~3 µs/版本 | **链长是唯一的乘数** |
| 7 | 重复 5–6 直到可见或链到底（到底 = 该行在本视图下不存在） | ×链长 | 长事务 + 热点行 → 10~100 版本 |
| 8 | 二级索引路径要先走索引再回表，MVCC 判定发生在**回表之后** | 索引跳 + 簇跳 | 二级索引记录无 trx_id，判定在聚簇索引做 |

合成一句话的延迟预算（链长是唯一乘数）：

| 快照状态 | 单行读成本 | 什么场景会到这里 |
| --- | --- | --- |
| 命中当前版本 | ≈ 3 µs | 无并发热点 |
| 落后 1 个版本 | ≈ 6 µs | 正常的短事务 |
| 落后 100 个版本 | ≈ 300 µs | 2 万行/s 的热点行被一个长事务钉住 5 分钟 |
| 落后 1,000 个版本 | ≈ 3 ms | 此时 1,000 QPS 就要 3,000 个并发服务线程才顶得住 |

**这一步的结论很硬**：MVCC 让读变简单了（不加锁），但给读引入了一个**可变的倍数**——链长。索引、执行计划、硬件都正常，`SELECT` 却从 3 µs 变成 3 ms 的唯一解释就是「有人在钉住一个很老的快照」。看到这个现象不要查慢查询日志（它只会显示耗时，不解释原因），直接去查最老事务。

---

## 06.5 RC vs RR：差别只有一行代码

内核里的事实是：两者共用**完全相同的**版本链、完全相同的四步判定、完全相同的 undo 结构。唯一的差别是**什么时候创建 Read View**：

- `READ COMMITTED`：每条语句开始时创建一次，语句结束就丢 → 所以两条语句之间别人提交的东西，下一条语句就看见了（不可重复读）。
- `REPEATABLE READ`：事务里**第一条快照读**时创建一次，一直用到事务结束 → 所以整个事务看到同一个世界（可重复读）。
- `SERIALIZABLE`：所有普通 `SELECT` 被隐式改写成 `SELECT ... LOCK IN SHARE MODE`，于是不再走快照读，全部变成当前读 + next-key 锁，读与读之间互相阻塞。

写成伪代码只有一行开关（本节不贴内核源码，只给形状）：

```
每次「一致读（consistent read）」入口：
    if (trx->isolation != READ_COMMITTED && trx->read_view 已存在)
        return trx->read_view;                 // RR：复用事务级快照
    return create_new_read_view();              // RC：每条语句一份
```

**这一行决定了九件事**：

| 维度 | RC | RR（MySQL 默认） |
| --- | --- | --- |
| Read View 创建时机 | 每条语句 | 事务内第一条快照读 |
| 同一事务两次读同一行 | 可能不同（不可重复读） | 相同 |
| 快照读看到别人新提交的行 | 会 | 不会 |
| 快照读会不会幻读 | 会（新行可见） | 不会 |
| 当前读加锁范围 | 记录锁，**间隙不锁**（+ 半一致读） | 记录锁 + 间隙锁（next-key） |
| undo 被钉住的最长时间 | ≈ 一条语句的时长 | ≈ 整个事务的时长 |
| statement 格式 binlog | **不安全**（§06.7） | 相对安全（锁范围一致） |
| 死锁率 | 低（无间隙锁） | 高（间隙锁互相等） |
| 典型生产选择 | **互联网业务默认** | 金融账务、需要事务级读一致 |

### 双会话实验：八步看清楚「读到旧值」

在 MySQL 8.0 上照抄执行，`④` 返回 10 就是 RR 在用 undo 版本，返回 7 就是 RC。这是评审现场唯一需要的实验，成本 30 秒。

```sql
-- 准备（任一连接）：CREATE TABLE inventory(id INT PRIMARY KEY, stock INT) ENGINE=InnoDB;
--                   INSERT INTO inventory VALUES (1, 10);
-- 会话 A                                              会话 B
-- ① 设成 RR，开事务，建立快照                          -- ② 也开事务
SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;
BEGIN;
SELECT stock FROM inventory WHERE id = 1;   -- ③ 返回 10（Read View 在这一刻冻结）
                                                        BEGIN;
                                                        UPDATE inventory SET stock = 7 WHERE id = 1;
                                                        COMMIT;            -- ④ 真实值变成 7 并已提交
SELECT stock FROM inventory WHERE id = 1;   -- ⑤ RR 返回 10 ← 沿 roll_ptr 回溯 undo 造出来的旧版本
SELECT stock FROM inventory WHERE id = 1 FOR UPDATE;
                                            -- ⑥ 当前读：返回 7，且加了 X 锁（跳过 Read View）
UPDATE inventory SET stock = 7 WHERE id = 1; -- ⑦ 当前读命中 1 行，把 A 手里的旧值 10 覆盖成 7
COMMIT;
-- ⑧ 把上面的隔离级别换成 READ COMMITTED 重跑：⑤ 返回 7 —— 差别只有这一处
```

第 ⑤ 步是全部，第 ⑥⑦ 步是陷阱：**同一个事务里，⑤ 看到 10、⑥⑦ 看到 7**，于是任何「读出来判断、再写回去」的逻辑都建立在一个已经过期的事实上（事故三）。RR 给你的不是锁，是**两个世界并存的权利**。

---

## 06.6 幻读、当前读与锁边界（MVCC 管不到的地方）

**图 3 · 快照读与当前读的分叉路径**

```
                         一条 SQL
                            │
        ┌───────────────────┴────────────────────┐
   普通 SELECT                                   UPDATE / DELETE /
   （快照读 consistent read）                     SELECT ... FOR UPDATE / FOR SHARE
        │                                        （当前读 current read / locking read）
        ▼                                                  ▼
  用 Read View 判定行版本                        直接读**最新已提交版本**（不看 Read View）
        │                                                  │
  不可见 → 沿 roll_ptr 回溯 undo                  加锁：RR = next-key（记录 + 前面的间隙）
  → 构造旧版本 → 返回                                       RC = 只有记录锁，间隙不锁
        │                                                  并行 Semi-consistent read（RC 专属）
  一行锁都不加，不阻塞任何写                       命中别人的新插入行 ⇒ **幻读**
  也不被别人阻塞（除 DDL/元数据锁）                互相等待 ⇒ 锁等待 / 死锁（→ comp/07）
        │                                                  │
        └───────────────────┬──────────────────────────────┘
                            ▼
     同一个事务里两条路径并存 ⇒ 「读到的」与「改到的」不是同一批行
     ⇒ MVCC 只解决「读到的内容一致」，不解决「读到的集合闭合」
```

### RR 下仍然幻读的三种形状

| 形状 | 快照读时 | 当前读时 | 会不会幻读 |
| --- | --- | --- | --- |
| 快照读 → 快照读 | 集合 A | — | **不会**（但可能读到过期集合，事故三是它的经济后果） |
| 快照读 → 当前读（`UPDATE`/`FOR UPDATE`） | 集合 A | 用最新版本重新扫，可能命中 A 之外的新行 | **会**（事故三 T5） |
| 当前读 → 当前读（先 `FOR UPDATE` 锁住区间） | — | 第一次就加了 next-key 锁，别人的插入被挡住 | **不会**（代价：锁等待与死锁） |
| 无索引条件 + 当前读 | — | 全表扫描逐行加锁 ≈ 锁全表 | 不会幻读，但并发直接归零 |

所以「RR 解决幻读」这句话的准确版本是：**RR 用事务级快照让「快照读」不会幻读；真正的防幻觉得靠 next-key lock，而 next-key lock 只在当前读上生效，且必须发生在竞争者插入之前。** 先读后写的顺序一错，锁就来晚了。

### 半一致读（semi-consistent read）与 `innodb_locks_unsafe_for_binlog`

一个必须在评审里说清的历史包袱：

- **它解决什么**：RR/RC 下 `UPDATE` 扫到被别人锁住的行要等待，等一堆行就会死锁。半一致读的做法是：遇到被锁的行，**不等待**，把最新已提交版本交给 server 层重新判断 `WHERE`；不匹配就跳过（不加锁），匹配才去等锁。这能显著降低「先扫后更新」型死锁。
- **它的代价**：一句 SQL 在主库**跳过了**某些行，在从库重放时**没有竞争**，于是命中了——命中行数不同，且这个差异不会被任何复制错误暴露（事故二）。
- **生命周期**：5.1 起可以用 `innodb_locks_unsafe_for_binlog=ON` 全局开启；5.7 标记弃用；**8.0 直接删除该参数，并把 RC 下 InnoDB 的写操作强制为 row-based logging**（这就是那句 `InnoDB is limited to row-logging when transaction isolation level is READ COMMITTED`）。所以今天这两件事变成了常量：**RC 一定用半一致读、一定不锁间隙；也一定要求 ROW 格式**。
- **它带来的另一个副作用**：RC 下 `UPDATE` 可以「看到」并跳过被锁的行，让「一条语句影响的行数」在并发下不确定——任何依赖 `affected_rows` 做业务的代码（比如「改到 0 行说明已经被别人抢了」）在 RC 下语义是正确的，在 RR 下则可能变成「等锁 50 秒后才发现要改」——**这就是 RC 在高并发抢购场景更受欢迎的真实原因**，不是玄学。

---

## 06.7 长事务的账：undo、purge 与主从延迟

**图 4 · 一个长事务如何把 undo 与 purge 拖死**

```
 长事务 T1 开始（它持有最老的 Read View）                  history list length
   │                                                        3 万  ── 稳态基线
   ├── purge 的边界 = 最老 Read View。T1 之前的可以回收，
   │   T1 之后产生的版本**一条都不能删** ← 全部的因果在这
   │
   ├── 09:12 起，业务每秒 1.2 万行 UPDATE
   │     生成速率 1.2 万条/s ────┐
   │     purge 吞吐 8 万条/s ────┤  有效 purge 速率 = 0（没有可回收的）
   │                             ▼
   ├── HLL 单调上涨：1.2 万/s × 秒数
   │     1 小时 → 4,320 万；5 小时 → 2.16 亿
   │
   ├── undo 表空间按需生长，`innodb_undo_log_truncate` 需要
   │   「purge 越过截断点」才动作 ⇒ 文件只增不减
   │     磁盘满 → 写入全部失败（报的是磁盘错误，不是事务错误）
   │
   └── KILL T1：不是瞬间恢复
         ① 回滚要沿 undo 反向执行，2.5 亿条版本的回滚本身 ≥ 正向时长
         ② purge 才刚开始：2.5 亿 / 8 万每秒 ≈ 3,125 s ≈ 52 分钟
         ③ 期间缓冲池被 undo 页与索引页争抢，正常查询 P99 抖动
```

### 算式：30 分钟事务 + 每秒 2 万行，undo 涨多少

```
前提：行宽 340 B（含 6 个二级索引列），undo 页有效利用率 45%，purge 4 线程 × 2 万条/s

每条 update undo 占盘 =（5 + 6 + 7 + 340）/ 0.45 ≈ 800 B
版本数        = 20,000 × 1,800 s = 3,600 万
undo 增量     = 3,600 万 × 800 B ≈ 28.8 GB        ← 30 分钟一个事务
undo 速率     = 28.8 GB / 1,800 s ≈ 16 MB/s
二级索引待删  = 3,600 万 × 6 × 30 B ≈ 6.5 GB       （长在索引页里，不属于 undo）

purge 落后多少条？
  落后量 = 生成速率 × 事务时长 = 2 万 × 1,800 = 3,600 万条
  追赶时间 = 3,600 万 / 8 万每秒 ≈ 450 s ≈ 7.5 分钟
  若表上 12 个二级索引、purge 有效吞吐掉到 2 万条/s → 追赶 30 分钟
  ⇒ 结论：**事务跑多久，purge 就要追多久；二级索引数就是这个追赶时间的乘数**

把事故一的参数代进来：
  5 h 47 m（20,820 s）× 1.2 万行/s ≈ 2.50 亿条 × 800 B ≈ 200 GB（+45 GB 索引空洞）≈ 245 GB
  undo 速率 ≈ 200 GB / 20,820 s ≈ 9.6 MB/s —— **一直不快，就是不停**
```

### 为什么「从库延迟会被大事务放大」

复制链路的三个事实叠加成一条除法：

1. 主库的事务在**提交时**才把整个事务的 binlog 一次性刷出去（ROW 格式下就是几千个 row event 连着），从库从这一刻才开始回放。
2. 一个事务**不可被拆给多个并行 worker**（MTS 的最小单位是事务；除非开 `WRITESET` 一类依赖跟踪，但那也只能并行不同事务）。
3. 于是：**从库延迟 ≈ 主库事务时长 + 从库回放时长**，而不是「平均 QPS 除以回放能力」。

| 量 | 推导 | 结果 |
| --- | --- | --- |
| 主库事务时长 | 题设 | 1,800 s |
| 主库产生的行变更 | 2 万行/s × 1,800 s | 3,600 万行 |
| 从库回放能力 | 单 worker，受二级索引数与刷盘限制 | ≈ 5 万行/s |
| 从库回放时长 | 3,600 万 / 5 万每秒 | ≈ 720 s |
| 从库延迟 | 事务时长 + 回放时长 = 1,800 + 720 | ≈ 2,520 s ≈ **42 分钟** |

这 42 分钟里，所有读从库的请求读到的是 42 分钟前的数据，而主库仍在以 12 GB / 30 分钟的速度产生 undo。

反过来说：**把大事务切成 500 行一批，从库延迟从「事务时长」变成「批时长」**——这是「分批提交」在内核层面的唯一正经理由，比为「避免锁表」更重要（锁表是 eng/7 的角度，延迟放大是内核的角度）。

### undo 表空间与 purge 线程：三个配置项各自改变哪条物理路径

不是参数清单，是「改了哪条路径、代价落在谁身上」：

| 配置 | 它改变哪条物理路径 | 代价落在谁身上 | 该知道的现状 |
| --- | --- | --- | --- |
| `innodb_purge_threads` | purge 的并发度；超过 8 之后只有「清理二级索引」还能真正并行（第一层扫 undo 仍单线程） | 主库 CPU 与 IO，随索引数放大 | 默认 4；8~16 是常见生产值 |
| `innodb_undo_tablespaces` | undo 文件的个数与轮转粒度 | 磁盘布局、truncate 的停顿窗口 | 8.0 起**已弃用**，改为运行时 `CREATE/DROP UNDO TABLESPACE`（撤销表空间可在线增删，默认 2 个） |
| `innodb_undo_log_truncate` | 让「需要截断」的 undo 表空间 offline → 截断文件 → 重新 online | 截断瞬间的 IO 峰值与短暂不可用 | 8.0 默认 ON，但**前提是 purge 已越过截断点** ⇒ 长事务在时它永远不动手 |
| `innodb_max_purge_lag` / `_delay` | HLL 超阈值时给 DML 主动插入延迟（背压） | 写吞吐，直接掉 | 默认 0（不背压），只在救火现场开 |

最后一行值得单独说：**它是唯一能在「长事务已经把 HLL 顶到天上」的时候，让系统先减速而不是先把磁盘写满的开关**。默认不开是因为它会把业务写量直接砍下来——但比起磁盘 100% 后所有写全失败，减速是一笔更好的交易。

### 骨架一 · 巡检：谁在钉住 purge（≤ 40 行）

```sql
-- ① 最老的事务（快照年龄 = purge 的边界，这一列才决定 undo 会不会涨）
SELECT trx_id, trx_state, trx_started,
       TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS age_sec,
       trx_rows_modified, trx_is_read_only,
       LEFT(trx_query, 60) AS q
FROM information_schema.innodb_trx
ORDER BY trx_started ASC
LIMIT 10;

-- ② history list length（先开指标再查；8.0 原生需要 innodb_monitor_enable）
SET GLOBAL innodb_monitor_enable = 'trx_rseg_history_len';
SELECT name, count FROM information_schema.innodb_metrics
WHERE name = 'trx_rseg_history_len';

-- ③ undo 表空间实际占盘（谁在涨、涨到多少；8.0 的 undo 是独立表空间）
SELECT t.name, ROUND(SUM(f.file_size)/1024/1024/1024, 1) AS gb
FROM information_schema.innodb_tablespaces t
JOIN information_schema.files f ON f.tablespace_name = t.name
WHERE t.name LIKE '%undo%'
GROUP BY t.name;

-- ④ 二级索引与聚簇索引的大小比（delete-mark 淤积最直观的信号）
SELECT table_name, index_name, ROUND(stat_value * @@innodb_page_size/1024/1024) AS mb
FROM mysql.innodb_index_stats
WHERE database_name = DATABASE() AND stat_name = 'size'
ORDER BY table_name, mb DESC;
```

### 骨架二 · Java：`@Transactional` 的传播与超时，以及长事务真正的根因

```java
@Service
public class ReconcileService {
    private final OrderMapper orderMapper;
    private final TransactionTemplate batchTx;   // 每个批次一个独立事务

    // ✗ 事故一的写法：单事务包住 12 万行；timeout=30 也救不了——
    //   timeout 只在语句间检查，且 -1 是默认；外层事务一旦跨 RPC/循环，快照就一直钉着 purge
    @Transactional(propagation = Propagation.REQUIRED, timeout = 30, rollbackFor = Exception.class)
    public void reconcileWrong(List<Long> ids) {
        for (Long id : ids) {
            orderMapper.fixAmount(id);           // 每次 UPDATE 产一条 update undo，purge 全部被卡
        }
    }

    // ✓ 正解：事务边界 = 一批，批大小由「undo 速率 × 批时长」定，不由「行数直觉」定
    private static final int BATCH = 500;        // 500 行 × 0.4 ms ≈ 200 ms/事务

    public void reconcileRight(List<Long> ids) {
        for (int i = 0; i < ids.size(); i += BATCH) {
            List<Long> part = ids.subList(i, Math.min(i + BATCH, ids.size()));
            batchTx.executeWithoutResult(s -> part.forEach(orderMapper::fixAmount));
        }
    }

    // 注意：Propagation.REQUIRES_NEW 只挂起外层事务，**外层的 Read View 还在**，
    // 所以「外层长事务 + 内层新事务」不会减轻 undo 压力——真正的修法是缩短最外层。
}
```

### 骨架三 · Go：`sql.Tx` + context 超时，把事务寿命变成 deadline

```go
func (r *Repo) FixAmounts(ctx context.Context, ids []int64) error {
	// 1) 事务寿命 = context deadline。调用方没给就自己盖一个硬上限（≤ 3s）
	if _, ok := ctx.Deadline(); !ok {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, 3*time.Second)
		defer cancel()
	}

	// 2) 分批：每批一个事务，提交即释放快照；不要把一个 Tx 拖过整个循环
	for _, chunk := range chunks(ids, 500) {
		if err := r.fixChunk(ctx, chunk); err != nil {
			return err // 已回滚，交给上层决定是否重试（重试必须幂等，→ eng/11）
		}
	}
	return nil
}

func (r *Repo) fixChunk(ctx context.Context, ids []int64) error {
	tx, err := r.db.BeginTx(ctx, &sql.TxOptions{
		Isolation: sql.LevelReadCommitted, // 显式写死，别依赖 DSN / 服务端默认
		ReadOnly:  false,
	})
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }() // 兜底：Commit 成功后 Rollback 是 no-op

	stmt, err := tx.PrepareContext(ctx, `UPDATE order_item SET amount=? WHERE id=?`)
	if err != nil {
		return err
	}
	defer stmt.Close()
	for _, id := range ids {
		if _, err := stmt.ExecContext(ctx, calc(id), id); err != nil {
			return err // ctx 超时 → 驱动发 KILL QUERY，事务被回滚，不会留下长事务
		}
	}
	return tx.Commit()
}
```

---

## 06.8 架构边界：MVCC 能做什么、不能做什么

| 需求 | MVCC 能不能 | 靠什么绕过 / 该走哪 |
| --- | --- | --- |
| 同一事务内两次读同一行结果相同 | **能**（RR，事务级快照） | — |
| 让读不阻塞写、写不阻塞读 | **能**（这是 MVCC 的存在理由，快照读零锁） | — |
| 无锁一致性逻辑备份 | **能**（`mysqldump --single-transaction` 就是开一个 RR 快照） | 代价：备份时长 × 每秒更新量 = 要留住的 undo（§06.7） |
| 让「先检查再写入」变原子 | **不能**（快照读不加锁 ⇒ 两个事务都能通过检查） | 条件更新 `WHERE stock>=1`、乐观锁 `WHERE version=?`、先 `FOR UPDATE`（[eng/7 §7.9](../eng/7-local-transaction-pitfalls.md)） |
| 阻止别人往我的区间插入新行 | **不能**（快照读看不见新行，当前读又会命中它） | next-key lock（RR + 当前读 + 有索引条件，→ comp/07） |
| 读到「最新已提交」 | **不能**（快照读故意不读最新） | 当前读 `FOR UPDATE`/`LOCK IN SHARE MODE`，代价是加锁 |
| 跨实例 / 跨库的一致快照 | **不能**（Read View 只在本实例内有效） | 全局时间戳（TSO/GTID 位点）、追日志位点再读（→ comp/10） |
| 回滚后读回历史（时间旅行 / 审计） | **不能**（purge 之后版本永久消失，不可逆） | 时间表 / CDC 归档（→ comp/33） |
| 长期只读事务不产生成本 | **不能**（快照就是磁盘账单，只读事务一样钉住 purge） | 事务超时 + 只读限流 + 走离线副本 |
| 不防「写偏斜」（两个事务各写一半不变式） | **不能**（RR 不是可串行化） | 串行化 / 把所有涉及行显式 `FOR UPDATE` / 应用层约束 |
| 提供一致性的「集合」 | **不能**（只保行内容，不保行集合） | 当前读 + next-key，或者放弃「集合判断」改为「逐行原子操作」 |

---

## 06.9 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| # | 现象 | 根因（指到物理形态） | 怎么定位 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- | --- |
| 1 | 磁盘满，只有 undo 文件在涨；报 `OS error 28`，应用侧看到「写入全部失败」 | 长事务持有最老 Read View，purge 与 undo truncate 都卡在它的时点 | `innodb_trx` 按 `trx_started` 排序 + HLL 单调上升；`innodb_metrics.trx_rseg_history_len` | `KILL` 最老事务（注意回滚可能比正向还久）；扩 undo 表空间只争取时间 | 事务年龄 60 s 告警；批处理强制批边界；`innodb_max_purge_lag` 做背压 |
| 2 | 同一事务两次 `SELECT` 结果不同（以为在 RR 下） | ① 会话实际是 RC（连接池串了会话级设置）② 两次查询走了不同实例（读写分离） | `SELECT @@transaction_isolation, CONNECTION_ID(), @@hostname` 在同一条连接上跑 | 隔离级别写进连接初始化；强制路由 | 连接池初始化 SQL 统一设置；读写分离只读事务别跨库 |
| 3 | `UPDATE` 改了「不该改」的新行，或影响行数比预期多 | 快照读做决策 + 当前读执行 ⇒ 中间别人的提交被「补偿」进来（幻读） | 对比 binlog ROW 镜像行数与应用日志里打印的判断行数 | 改条件更新（`WHERE ... AND stock>=1`）或先 `FOR UPDATE` 锁区间 | 禁止「无 WHERE 条件的 UPDATE」；把「先读后写」写进 review 红线 |
| 4 | 主从行数/金额对不上，`SHOW SLAVE STATUS` 全绿 | `binlog_format=STATEMENT` + RC（半一致读）/ 非确定性函数 ⇒ 主从命中不同行集 | 用 `mysqlbinlog` 看 event 格式；比对两端 `CHECKSUM TABLE` | 改 `binlog_format=ROW`（8.0 上 RC + STATEMENT 会直接报错，5.7 不会） | 参数进基线巡检；`sync_binlog`/格式纳入上线检查单 |
| 5 | purge 长期追不上，undo 只增不减、undo 文件不缩 | 二级索引多 → purge 每条 delete-mark 都要回聚簇索引确认；`innodb_purge_threads` 默认 4 不够 | `innodb_index_stats` 里二级索引与聚簇索引体积比持续上升 | `innodb_purge_threads=8~16`（>8 只对二级索引有并行收益）；砍冗余索引 | 索引评审（每个索引都要有人认领）；`innodb_undo_log_truncate=ON` |
| 6 | 一条 `UPDATE` 扫几十万行，从库延迟半小时 | 大事务在从库是不可拆的单个 binlog 事务，单 worker 串行回放 | 从库 `Seconds_Behind_Master` 台阶式上涨；`performance_schema.replication_applier_status_by_worker` | 分批（每批 ≤ 1,000 行且 < 1 s）+ 回放并行参数 | 批量接口强制 dry-run 报影响行数；大变更走在线 DDL 式的分批任务 |
| 7 | `ER_TOO_MANY_CONCURRENT_TRXS` / 找不到可用回滚段 slot | 写事务数撞上 rseg × slot 上限（128 × 1024 是物理常量） | 统计 `innodb_trx` 中 `trx_state='RUNNING'` 且非只读的数量 | 降连接数、拆库、缩短事务 | 连接池上限与事务时长一起做容量规划（→ comp/44） |
| 8 | `KILL` 长事务后系统没有立刻恢复 | 回滚要沿 undo 反向执行；purge 要补课；缓冲池被 undo 页占满 | `SHOW PROCESSLIST` 里状态 `rollback`；HLL 缓慢下降 | 等；必要时先扩 IO 能力再 purge | 别等出事才 KILL：靠事务年龄预警（§06.10） |
| 9 | `SELECT ... FOR UPDATE` 把整段锁住，随后大面积死锁 | RR 下无索引条件的当前读 = 全表扫描逐行 next-key 锁 | `performance_schema.data_locks` / `SHOW ENGINE INNODB STATUS` 的 `LATEST DETECTED DEADLOCK` | 补索引、缩小范围、按主键序访问（→ comp/07） | 当前读必须带索引条件；死锁率进 SLO |

---

## 06.10 观测：指标、拐点与告警

| 指标 | 含义（指到物理形态） | 稳态 | 拐点 | 告警阈值骨架 |
| --- | --- | --- | --- | --- |
| **最老事务年龄** | purge 的边界；它决定所有 undo 能不能回收 | < 1 s | > 10 s 开始积累版本 | `> 60 s` 告警，`> 600 s` 一级（带 `trx_query`） |
| **HLL（`trx_rseg_history_len`）** | 已提交但还不能回收的版本数 | < 5 万 | > 100 万说明有长事务；> 1,000 万说明 purge 已停滞 | `> 100 万` 持续 5 分钟告警 |
| **undo 表空间总大小 / 日增** | 事故一的直接度量 | 10~30 GB | 日增 > 10 GB | 日增 `> 10 GB` 或总量 `> 60 GB` |
| **purge 反推速率** | = ΔHLL/Δt + 生成速率；持续为负增长说明在补课 | ≥ 生成速率 | 连续 10 分钟低于生成速率 | 用 `rate(trx_rseg_history_len)` 斜率判断 |
| **二级索引/聚簇索引体积比** | delete-mark 淤积与冗余索引 | 稳定 | 比前一周上升 > 20% | 周环比报警（慢变量，周级即可） |
| **从库延迟 + 大事务** | 回放最小单位是事务 | < 1 s | 台阶式跳变 = 大事务 | `> 30 s` 且伴随 binlog event 体积异常 |

排查顺序只有一条：**先看最老事务年龄，再看 HLL 斜率，最后看 undo 文件大小**。反过来查（先看磁盘）永远只能得到「磁盘满了」这个结果，得不到原因。补一句工具实情：原生 MySQL 没有 `Innodb_history_list_length` 这个状态变量（Percona 有），`mysqld_exporter` 默认也采不到，得靠 `innodb_metrics` 或自定义 query 把 §06.7 骨架一里的 ② 抓成时序数据——**这个指标在很多团队是「没有」，于是事故一那种情况只能靠磁盘告警发现。**

---

## 06.11 一页速记

### 判据表：RC / RR / 串行化，五列定生死

| 隔离级别 | 同一事务能不能重复读 | 会不会幻读 | 加锁范围（当前读） | undo 压力 | 主从一致性（STATEMENT 格式） |
| --- | --- | --- | --- | --- | --- |
| **READ UNCOMMITTED** | 不能（脏读） | 会 | 记录锁，间隙不锁 | 极低 | 不安全 |
| **READ COMMITTED** | **不能**（每语句新快照） | **会**（快照读也看得见新行） | 记录锁，**间隙不锁** + 半一致读 | **低**（快照只活一条语句） | **不安全**（半一致读导致命中行数不同；8.0 直接报错） |
| **REPEATABLE READ** | **能**（事务级快照） | 快照读不会，**当前读会**（§06.6） | 记录锁 + 间隙锁（next-key） | **高**（快照活到事务结束，钉住全部 undo） | 相对安全（锁范围一致） |
| **SERIALIZABLE** | 能（且读也加锁） | 不会（读锁挡住插入） | 所有读加 S 锁 + next-key，范围读锁区间 | 极高（等待链拉长事务，undo 跟着涨） | 安全，但吞吐不可用 |

**怎么用这张表**：互联网业务选 RC（死锁少、undo 压力低、副作用是幻读与不可重复读，用「条件更新 + 幂等」补）；账务/对账选 RR（要事务级读一致，接受间隙锁与死锁率）；串行化只在极小热点、且能接受并发 1 的场景短期使用。**无论选哪个，`binlog_format=ROW`。**

### 十条一口价

1. 行的旧版本不在表里，在 **undo** 里；行上只有 6 B `trx_id` + 7 B `roll_ptr`。
2. 可重复读 = 给你一个旧版本视图，**一行锁都不加**。
3. Read View 四字段：`m_ids` / `min_trx_id` / `max_trx_id` / `creator_trx_id`；判定四步：小于 min 可见、≥ max 不可见、在 m_ids 不可见、其余可见；`X == creator` 排第一 ⇒ 看得见自己的修改。
4. RC 与 RR 的唯一差别是**视图创建时机**（每语句 vs 每事务第一次快照读）。
5. 快照在**第一条读到 InnoDB 表的语句**时建立，不是 `BEGIN` 时。
6. 幻读要靠 next-key lock，而它只在**当前读**上生效，且必须早于竞争者插入。
7. purge 的边界 = 最老 Read View；**只读事务一样钉住 purge**。
8. undo 增长 = 更新行数/s × 每行 undo 字节；与「本事务改了多少行」无关。2 万行/s 的 30 分钟事务 ≈ 28.8 GB。
9. 从库延迟 ≈ 主库事务时长 + 回放时长；分批提交是唯一解药。
10. MVCC 不防写偏斜、不提供历史、不提供跨实例快照；它只提供「行内容在一个快照里不变」。

---

## 06.12 自检题

**1. 行上只有一份数据，为什么事务 A 能看到 `stock=10`、事务 B 能看到 `stock=7`？**
<details><summary>答案</summary>

A 持有的是一个 Read View，它把「谁提交了」冻结在某一刻。行上真正的当前版本写着 `DB_TRX_ID=B`；A 的判定说「B 的提交在我建视图之后 ⇒ 不可见」，于是 A 沿 `DB_ROLL_PTR` 进入 undo，找到 B 之前那个版本的 `trx_id` 与旧值，在内存里构造出一个 `stock=10` 的临时记录返回。**旧版本从来没被拷回表里，它是从 undo 里"读出来再拼出来"的。** 这也解释了为什么链越长读越慢（每次回溯 2~3 µs）。
</details>

**2. RC 与 RR 的内核差别到底是什么？为什么 RC 的死锁率低、undo 压力小？**
<details><summary>答案</summary>

差别只有 Read View 的创建时机：RC 每条语句一份、RR 事务内一份。由此推出：RC 的不可重复读与幻读（每条语句都能看到新提交），RR 的可重复读（事务级快照）。死锁少的原因是 RC **不锁间隙**（间隙锁是 RR 死锁的主要来源，→ comp/07）；undo 压力小是因为快照只活一条语句，purge 的边界随语句推进，版本几乎不用留。
</details>

**3. RR 下会不会幻读？给一个一定会幻读的场景。**
<details><summary>答案</summary>

快照读不会（事务级快照看不到新行）；**先快照读、再当前读会**。场景：T1 `SELECT count(*) FROM batch WHERE sku=88` 得 3（快照）；T2 `INSERT` 第 4 行并提交；T1 执行 `UPDATE batch SET status=1 WHERE sku=88` —— 当前读用最新版本重扫，命中 4 行，把 T2 刚插入的行也改了。修法：先 `SELECT ... FOR UPDATE` 拿 next-key 锁（锁住区间，T2 插不进来），或放弃「集合判断」改成逐行带条件的原子写。
</details>

**4. 一个只读事务跑了 3 小时，会不会撑爆 undo？为什么？**
<details><summary>答案</summary>

会。purge 的边界是最老 Read View，而只读事务照样持有 Read View。它自己不产生 undo，但这 3 小时里**全库所有 UPDATE/DELETE 产生的旧版本**都不能回收，undo 增长速率 = 全库更新行数/s × 每行 undo 字节。8.0 里只读事务可以不分配 `trx_id`，但它的视图仍然钉住 purge 的上界——「只读所以无害」是错的。逃生口：`innodb_max_purge_lag` 背压、只读事务超时、导出走离线副本。
</details>

**5. 为什么 `binlog_format=STATEMENT` 配 RC 会主从不一致？说清主从各自看到了什么。**
<details><summary>答案</summary>

RC 下 InnoDB 用半一致读：`UPDATE` 扫到被别的事务锁住的行时不等锁，把**最新已提交版本**交给 server 层重判 `WHERE`，不匹配就跳过、不加锁。于是同一句 SQL 在主库会因为「扫描时刻正好有人持有该行的锁且已提交版本不匹配」而跳过若干行；从库重放时没有锁竞争，等前面的事务都应用完之后再执行这句，这些行就匹配上了 ⇒ 从库多改了几行。加上提交顺序与扫描时刻并不写进 statement，差异无法自愈。所以 MySQL 8.0 直接禁止 RC + STATEMENT 写 InnoDB（报 `InnoDB is limited to row-logging when transaction isolation level is READ COMMITTED`），线上基线是 ROW + RC/RR。
</details>

---

## 06.13 延伸阅读

- 页、行格式、聚簇索引与 `DB_ROW_ID`：→ comp/05
- 行锁、间隙锁、next-key 与死锁分析（幻读的另一半答案）：→ comp/07
- redo / undo / binlog 三份日志与内部 2PC（undo 何时落盘、何时可丢）：→ comp/08
- 主从复制与并行回放（延迟为什么是「事务大小」的函数）：→ comp/10
- PostgreSQL 对照：把旧版本留在堆表里 ⇒ 表膨胀而不是 undo 膨胀：→ comp/11
- TiKV / NewSQL 的 MVCC 与 TSO（跨实例快照怎么实现）：→ comp/19
- CDC 与位点（MVCC 给不了的历史版本从哪来）：→ comp/33
- 应用层纪律：Spring 事务四前提、十坑、大事务拆分、隔离级别选型落地 → [eng/7 本地事务](../eng/7-local-transaction-pitfalls.md)
- 事务方案决策树（什么时候该离开本地事务）：[eng/9 事务决策树](../eng/9-transaction-decision-tree.md)
- 存储形态决策（B+ 树 vs LSM，谁的空间放大更贵）：[architect/03 存储与索引形态](../architect/03-storage-and-index-shape.md)
- 选型矩阵里 MVCC 旧版本这笔账：[eng/35 存储选型](../eng/35-storage-model-selection.md)
- 平台侧的存储分层与冷热归档（长事务与归档任务怎么错峰）：[llm/45 存储架构](../llm/45-storage-architecture.md)
- 本篇在全套里的位置与写作公约（八问卡、三本放大账）：[0-summary 总纲](0-summary.md)
