# 11. PostgreSQL 对照：堆表、xmin/xmax 与 VACUUM

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · 关系库与事务内核 ｜ **编号**：11 / 53
> **本篇定位**：这是 Part II 的对照篇，不是 PostgreSQL 教程。它只回答一个问题：**同样用 MVCC，为什么 MySQL 的麻烦在 undo，PostgreSQL 的麻烦在表本身**。答案在物理形态里——InnoDB 的表是一棵以主键为键的 B+ 树，旧版本挪进 undo；PostgreSQL 的表是一个无序堆，旧版本就留在堆里，索引叶子只存 `ctid`。这一条事实推出 PG 的主键不聚簇、更新要写新行、VACUUM 永远在追垃圾、xid 会在 42.9 亿处回卷。读完之后，「表为什么只涨不缩」「为什么 `VACUUM FULL` 会锁死业务」「为什么库突然只读」这三件事会收敛成同一个结论。
> **读法**：§11.2 的唯一真问题是支点；§11.3 的堆表图与 §11.4 的页访问对照要能默画，这是和 InnoDB 正面 PK 的地方；正在救火的直接跳 §11.8 的三本账与 §11.10 的坑清单；§11.12 的一页速记可以单独贴进选型文档。版本基准为 PostgreSQL 13–16 的默认行为，版本差异逐处标注。

---

## 11.1 引子：三起「表不大，盘满了 / 库突然只读」的故障

PostgreSQL 的故障有个共同点：**面板上最显眼的指标永远不是根因**。盘满报的是 `No space left on device`，只读报的是 `cannot execute ...`，锁表报的是连接池超时。真正的因——一个没人管的 `idle in transaction`、一个忘了删的复制槽、一次晚高峰的 `VACUUM FULL`——都藏在第二层因果里。

### 事故一：一个挂了三天的 BI 会话，把 80 GB 的表养成 608 GB（2025-08-15 → 11-03）

某 SaaS 订单中心，PostgreSQL 15 单主（32C128G，数据盘 1 TB NVMe）。`orders` 2 亿行、行均 300 B，堆 60 GB、4 个索引 20 GB，合计 80 GB；业务每天更新 10% 的行（约 2,000 万行），更新的是 `status`、`updated_at` 这类不在索引里的列。

08-15 09:12，BI 同事在 DBeaver 里点开一个查询，结果集还没拉完就切去开会了。会话停在 `idle in transaction`，`idle_in_transaction_session_timeout` 是默认值 `0`（不超时），连接池也没有回收它。它持有的事务快照把 `backend_xmin` 钉在了 08-15 09:12。

| 时间 | 事件 | 表大小 | `n_dead_tup` | autovacuum | 盘占用 |
| --- | --- | --- | --- | --- | --- |
| 08-15 09:12 | BI 会话进入 `idle in transaction`，快照钉住 | 80 GB | 0.3 亿 | 正常 | 38% |
| 08-25 | 10 天未回收，更新全部追加成新行 | 145 GB | 2.1 亿 | 每晚跑，扫完不删 | 51% |
| 09-15 | 死元组超过 6 亿，索引随之膨胀 | 300 GB | 6.2 亿 | 单次跑 40 分钟 | 72% |
| 10-15 | 表大小超过活数据 10 倍 | 450 GB | 10.4 亿 | 开始出现 `VacuumDelay` 等待 | 88% |
| 11-02 | 盘上只剩 12 GB | 590 GB | 14.8 亿 | 每夜跑到凌晨 | 97% |
| 11-03 02:14 | 一次批量更新要扩页，写入报 `No space left on device` | 608 GB | 16.0 亿 | 仍在跑，但删不动 | 100% |
| 11-03 03:40 | 找到并终止那条 `idle in transaction` 会话 | 608 GB | 16.0 亿 | 开始真正回收 | 100% |
| 11-03 06:10 | 回收完成；表**仍然是 608 GB** | 608 GB | 0.2 亿 | 空闲 | 100% |

算一遍就知道 608 GB 不是意外：80 天 × 2,000 万行 = 16 亿个死版本；每个版本按 300 B 数据 + 24 B 行头 + 4 B 行指针 + 2 B 对齐 = 330 B 计，16 亿 × 330 B ≈ 528 GB；加上 80 GB 活数据，正好 608 GB。**VACUUM 一直在跑，但它只能回收「对所有活快照都不可见」的版本；那条会话不死，08-15 之后产生的所有旧版本都还有可能被它读到，一条都不能删。**

代价：写入不可用 3 小时 46 分（02:14–06:00），失败订单 180 万笔；临时加 2 TB 云盘；后续 `pg_repack` 重写 608 GB 表又花了 5 小时 20 分。更贵的是那句复盘结论：**表不会因为 VACUUM 跑过就变小，它只会停止继续变大。**

### 事故二：晚高峰执行 `VACUUM FULL`，9 分钟锁死订单表（2025-06-18）

同一套集群的另一个库，`orders_archive` 100 GB、5 个索引 50 GB。月初做了一次归档：`DELETE FROM orders_archive WHERE created_at < '2024-01-01'`，删掉 40% 的行，但表还是 100 GB——VACUUM 只把空间标成可重用，不还给操作系统。DBA 想「一次搞定」，19:58 在业务库上直接执行 `VACUUM FULL orders_archive;`。

`VACUUM FULL` 要拿 `ACCESS EXCLUSIVE`。它先排队等一个跑了 70 秒的报表查询；一旦它的锁请求进入队列，**后面所有新的 `SELECT` 都排在它后面**——PostgreSQL 的锁队列不插队。20:00:30 它拿到锁开始重写，20:00:31 起全表对业务不可见。

| 时间 | 事件 | 连接池 | 业务表现 |
| --- | --- | --- | --- |
| 19:58:00 | `VACUUM FULL` 发起，等待 `ACCESS EXCLUSIVE` | 42/200 | 正常 |
| 19:58:10 | 锁请求入队；新查询开始排在它后面 | 88/200 | 报表变慢 |
| 20:00:30 | 拿到锁，开始重写 100 GB 堆 + 50 GB 索引 | 176/200 | 订单接口 P99 跳到 8 s |
| 20:03:00 | 连接池 200 打满，应用开始 500 | 200/200 | 下单失败 |
| 20:08:50 | 重写完成，锁释放 | 200/200 | 请求堆积 |
| 20:09:30 | 连接池回落，业务恢复 | 96/200 | P99 回到 120 ms |

按 500 MB/s 顺序带宽算：读 100 GB + 写 100 GB + 重建索引 50 GB 读 + 50 GB 写 = 300 GB，300 × 1,024 MB / 500 MB/s = 614 s，加锁等待约 2 分半，正好 9 分钟出头。**锁不是只锁「写」：`ACCESS EXCLUSIVE` 与所有表级锁都冲突，`SELECT` 也进不来。** 代价：9 分钟不可用，23,000 笔下单失败，客诉 1,100 单；`VACUUM FULL` 峰值还要求额外 150 GB 空闲盘，当时盘上只剩 180 GB，差一点在重写到 90% 时失败回滚。

### 事故三：写 TPS 从 2,000 涨到 20,000，42.9 亿的 xid 时钟开始倒计时（2025-10-27）

某数据平台，PostgreSQL 14，30 张表各 200 GB，合计 6 TB；ETL 用自动提交，一行一个事务，平时写 2,000 TPS。10-25 下游消费者下线，逻辑复制槽 `slot_etl` 没删，`active=false`，`xmin` 停在那一天。

10-26 晚上游重试风暴把写 TPS 推到 20,000。这个量级下：

- 2^31 / 20,000 = 107,374 s ≈ **1.24 天**就会撞上 32 位 xid 的可见性半区；
- `autovacuum_freeze_max_age = 2 亿`，2 亿 / 20,000 = 10,000 s ≈ **2.78 小时**就是 freeze 的全部预算；
- 6 TB 数据按 500 MB/s 扫一遍要 12,000 s ≈ **3.33 小时**，已经超预算；而默认 `autovacuum_vacuum_cost_delay=2 ms` + `cost_limit=200` 把 autovacuum 节流到约 80 MB/s，6 TB 要 **21.4 小时**，超预算 7.7 倍。

| 时间 | 事件 | `age(datfrozenxid)` | 写 TPS | 状态 |
| --- | --- | --- | --- | --- |
| 10-25 14:00 | 下游下线，`slot_etl` 变 `active=false` | 9.1 亿 | 2,000 | 正常 |
| 10-26 02:00 | 槽的 `xmin` 开始钉住回收 | 10.8 亿 | 2,000 | 表开始膨胀 |
| 10-27 08:00 | 上游重试风暴，TPS 翻 10 倍 | 15.2 亿 | 20,000 | autovacuum 追不上 |
| 10-27 12:00 | 距离 2^31 只剩 6 亿，日志开始告警 | 18.0 亿 | 20,000 | 告警被当噪音 |
| 10-27 14:40 | 到达 `XID_STOP_LIMIT`，新写事务被拒绝 | 21.44 亿 | 20,000 | **只读保护** |
| 10-27 15:10 | 确认下游已废弃，`pg_drop_replication_slot('slot_etl')` | 21.44 亿 | 0 | 开始急救 |
| 10-27 16:00 | 对最老的表执行 `VACUUM (FREEZE, PARALLEL 4)`，临时把 `cost_delay=0` | 21.44 亿 | 0 | 全速扫描 |
| 10-27 18:20 | `datfrozenxid` 推进，写事务恢复 | 19.1 亿 | 20,000 | 恢复 |

代价：写入不可用 3 小时 40 分，260 万笔 ETL 事务失败，下游报表延迟 8 小时。注意最后一行：**恢复后 `age` 只降到 19.1 亿，仍在告警区**——freeze 是持续负债，不是一次性动作。三起事故分别对应本篇三条主线：**堆内旧版本与 VACUUM（§11.5–11.6）**、**重写与锁（§11.8）**、**32 位 xid 与 freeze（§11.3、§11.8）**。

---

## 11.2 一句话定位与唯一真问题

**定位**：PostgreSQL 的 MVCC 是「**把旧版本留在表里，读的时候按 `xmin/xmax` 判可见**」；InnoDB 是「**把旧版本挪进 undo，读的时候沿 `roll_ptr` 回溯**」。两者都叫 MVCC，但账单寄到了不同地址：PG 寄给**堆表和 VACUUM**，InnoDB 寄给 **undo 和 purge**。三个"不是"：PG 的表不是聚簇索引（主键只是普通唯一索引）；更新不是原地改（默认写新行）；空间不是自动归还（VACUUM 不缩文件）。

### 唯一真问题

> **同样是 MVCC，PG 把旧版本留在表里、InnoDB 把旧版本挪进 undo——于是 PG 的一生都在和表膨胀、VACUUM、xid 回卷搏斗，而 InnoDB 的一生都在和 undo 膨胀、purge 落后搏斗。**

这一句要拆成三截：

1. **「旧版本留在表里」**：PG 的 `UPDATE` 不覆盖旧行，而是插入一条新行，旧行的 `xmax` 记上新事务 ID；索引叶子存的是 `ctid`，不是主键。于是「一行被改了 10 次」在堆里是 11 个版本，在索引里可能是 11 个条目（HOT 例外，§11.6）。
2. **「表膨胀」**：空间回收靠 `VACUUM` 扫堆、清索引、标可重用；它不缩文件，只把空间留给下一次写入。VACUUM 的边界是**最老快照**，一条长事务就能让全库的回收停摆。
3. **「xid 回卷」**：PG 的事务 ID 是 32 位，2^32 ≈ 42.9 亿，但安全半区只有 2^31 ≈ 21.5 亿；必须靠 freeze 把老 xid 变成「永远过去」，否则数据库会在 2^31 边界进入只读保护。InnoDB 的 `DB_TRX_ID` 是 6 B（48 位），没有这个 42.9 亿的钟。

从这一个矛盾推出全部纪律：

| 从矛盾推出的约束 | 推出的设计 |
| --- | --- |
| 主键不是物理顺序，`ctid` 才是 | 不要用 `ctid` 当业务 ID；主键范围扫描是「索引顺序 + 堆随机 IO」，不是 InnoDB 的聚簇扫描 |
| 更新写新行，旧行只能等 VACUUM | 高频更新表要留 `fillfactor`，让 HOT 生效；批量更新后必须巡检膨胀 |
| VACUUM 不缩文件 | 回收空间只有 `VACUUM FULL` / `pg_repack` / 重建分区；日常只能「阻止继续涨」 |
| VACUUM 被最老快照钉住 | 事务寿命是一等指标；`idle in transaction`、prepared xact、逻辑槽都要告警 |
| 32 位 xid 有硬时钟 | `age(datfrozenxid)` 必须进监控；freeze 带宽要按写入 TPS 和库大小做容量规划 |
| 索引叶子存 `ctid`，不存主键 | 二级索引更窄，但每次回表是一次堆页访问；覆盖索引要靠 VM 才能完全免回表 |
| 堆表没有持续聚簇 | `CLUSTER` 是一次性重排，不是 InnoDB 那种「插入也保持有序」 |

### 解剖卡八问：本篇答哪几问

| 八问 | 落在哪节 | 一句话答案 |
| --- | --- | --- |
| ① 承诺什么 | §11.2、§11.9 | 承诺「同一快照内读到同一行内容、读写不互相阻塞」；不承诺「表不膨胀」「空间自动归还」 |
| ② 数据长什么样 | §11.3 | 8 KB 无序堆页 + `HeapTupleHeaderData`（`xmin/xmax/ctid/infomask`）+ 独立 B-tree 索引 |
| ③ 一次读写走哪条路 | §11.4 | 主键点查 3 次索引页 + 1 次堆页；二级索引 4 次；index-only 在 VM 全可见时 3 次 |
| ④ 并发与一致 | §11.5 | 版本就在堆里，按 `xmin/xmax` 判可见；回收边界是最老快照 |
| ⑤ 扩展维度 | §11.9 | 垂直扩 + 只读副本 + 声明式分区；**没有原生分片**，横向扩要换 Citus/中间件/NewSQL |
| ⑥ 会在哪崩 | §11.9 | 系统级只有三种：盘被膨胀写满、xid 回卷进只读、`VACUUM FULL` 锁风暴 |
| ⑦ 看哪几个指标 | §11.11 | 死元组、`age(datfrozenxid)`、最老事务、VM/`Heap Fetches`、非活跃复制槽 |
| ⑧ 换掉它的信号 | §11.9 | 单表 > 数 TB 且更新密集、写 TPS 高到 freeze 预算不够、必须聚簇范围扫描 ⇒ 换形态或分片 |

---

## 11.3 物理形态：无序堆 + ctid + 行头

### 一页 8 KB 怎么切

PostgreSQL 的堆页由编译期常量 `BLCKSZ` 决定，默认 8192 B（8 KB）。页内从两头往中间长：页头是 `PageHeaderData` 24 B（`pd_lsn` 8、`pd_checksum` 2、`pd_flags` 2、`pd_lower` 2、`pd_upper` 2、`pd_special` 2、`pd_pagesize_version` 2、`pd_prune_xid` 4）；行指针数组从页头往后长，每项 `ItemIdData` 4 B（`lp_off` 15 bit、`lp_flags` 2 bit、`lp_len` 15 bit）；行数据从页尾往前长；中间是空闲空间，由 `pd_lower`/`pd_upper` 标出两端。

行指针（`ItemIdData`，4 B）不存数据，只存「这条行的偏移 + 长度 + 状态」。`ctid` 就是 `(块号, 行指针下标)`，6 B。**索引叶子不存主键，存这个 6 B 的 `ctid`；堆表也不按任何键排序，插入往哪页有空就往哪页放。**

### 堆表 vs 聚簇索引：一句话分开两个世界

```
InnoDB：表就是一棵以主键为键的 B+ 树
                  [root: PK 10]
                 /             \
          [PK 5]                 [PK 15]
         /      \               /       \
   leaf: 整行   leaf: 整行   leaf: 整行   leaf: 整行
   ↑ 叶子页就是数据页，主键顺序 = 物理顺序 = 范围扫描顺序

PostgreSQL：表是无序堆，索引是另一棵独立的树
   主键 B-tree： [root] → [branch] → [leaf: key=42, ctid=(8842,7)]
                                              │
                                              ▼
   堆文件： [blk 0][blk 1] ... [blk 8842: 行版本] ... [blk N]
            插入顺序，不按主键排序；同一条 SQL 要再走一次堆页
```

这一张图推出四条硬结论：

| 推论 | 具体表现 | 工程后果 |
| --- | --- | --- |
| ① 主键只是普通唯一索引 | `PRIMARY KEY` 建一棵 B-tree，堆不按它排序 | 主键范围扫描是「索引顺序 + 堆随机 IO」；InnoDB 是顺序扫描 |
| ② 二级索引不必回主键树 | 叶子存 `ctid`，直接定位堆页 | 二级索引更窄，但每次回表多一次堆页访问；InnoDB 要再走一次聚簇索引 |
| ③ 更新要写新行 | `UPDATE` 插入新版本，旧版本留堆里；HOT 例外 | 高频更新表会膨胀；所有含被更新列的索引都要加新条目 |
| ④ `CLUSTER` 是一次性重排 | 按某个索引重写表，之后插入不再保持顺序 | 不能当「持续聚簇」用；几周后相关性就掉光，要重新 CLUSTER 或改分区 |

**`CLUSTER` 的真相**：它拿 `ACCESS EXCLUSIVE`，按指定索引重写整张表，然后把新文件换上去；这期间**业务全停**，峰值还要约 2 倍磁盘。它不会让后续 `INSERT` 自动落在正确位置——新行还是按空闲空间插。想看一张表还剩多少物理相关性，查 `pg_stats.correlation`（-1 到 1，接近 1 说明索引顺序和堆顺序还一致）。

### 行头：23 字节里装着可见性

`HeapTupleHeaderData` 的固定部分 23 B（64 位机上按 `MAXALIGN` 补齐到 24 B；PG 12 移除行内 OID 后如此），后面才是 NULL 位图和用户列：

| 字段 | 宽度 | 是什么 | 容易踩的地方 |
| --- | --- | --- | --- |
| `xmin` | 4 B | 插入这行的事务 ID | 冻结后不再参与 xid 年龄；不是「行的创建时间」 |
| `xmax` | 4 B | 删除/更新/加锁这行的事务 ID；0 表示没有 | `SELECT FOR UPDATE` 也会写 `xmax`，但行没死（`HEAP_XMAX_LOCK_ONLY`） |
| `cmin` / `cmax` | 4 B 的 command-id 槽 | 事务内第几条命令插入/删除 | 两个命令号共享同一个槽，必要时升级为 combo CID |
| `ctid` | 6 B | `(block, offset)`，指向该行的下一个版本；最新版本指向自己 | 会被 `UPDATE` 和 `VACUUM FULL` 改；不能当稳定主键 |
| `infomask2` | 2 B | 列数 + `HEAP_HOT_UPDATED` / `HEAP_ONLY_TUPLE` 等 | HOT 链就靠这两位认 |
| `infomask` | 2 B | `XMIN/XMAX` 的 committed / invalid / is-multi / frozen 等 hint bit | 第一次读可能要去 `pg_xact` 查提交状态，并把 hint bit 写回页 |
| `t_hoff` | 1 B | 用户数据从行头往后多少字节开始 | 有 NULL 位图时不是 23，要按它算 |

两个必须说清的细节：

1. **`xmax` 不等于「这行死了」**。行锁、`SELECT FOR UPDATE`、多事务锁（MultiXact）都会占用 `xmax`，但 `infomask` 里没有「删除」语义。VACUUM 判断的是「`xmax` 已提交且对所有快照不可见」，不是「`xmax` 非零」。
2. **hint bit 是读路径的隐藏写**。事务提交状态本来在 `pg_xact`（每事务 2 bit）；第一次有人读这行时，如果 `infomask` 里没有 committed/aborted 位，就要去查 `pg_xact`，然后把结果写回行头。于是**只读查询也可能弄脏数据页**，这也是「刚重启后第一轮查询特别慢」的一个来源。

```sql
-- 看一条行的物理位置、xid 年龄、命令号；xmax 非零只说明「被删/被更新/被锁」
SELECT ctid,
       xmin, age(xmin) AS xmin_age,
       xmax,
       CASE WHEN xmax = '0'::xid THEN 'live'
            ELSE 'deleted / updated / locked' END AS xmax_state,
       cmin, cmax
FROM orders
ORDER BY ctid
LIMIT 5;

-- 更新一次，看 ctid 怎么变；非 HOT 更新会换到新位置
UPDATE orders SET status = 'paid' WHERE id = 42
RETURNING ctid, xmin, xmax;
```

### 32 位 xid 与 freeze：42.9 亿不是可用额度

`TransactionId` 是 `uint32`，空间 2^32 = 4,294,967,296 ≈ **42.9 亿**。但 PostgreSQL 用模 2^32 比较新旧，只允许「过去」的半区：一个 xid 一旦落后当前 xid 超过 2^31 ≈ **21.5 亿**，就会被当成「未来」，可见性判断翻转。所以真正的安全线是 2^31，不是 2^32。

freeze 是把老行标记成「永远过去」：现代版本在 `infomask` 里置 `HEAP_XMIN_FROZEN`（两个 hint bit 同时为 1），特殊 xid `FrozenTransactionId = 2` 是同一语义的旧写法。冻结后的行不再计入 `age(relfrozenxid)`，VACUUM 才能推进 `pg_class.relfrozenxid`；一个数据库的 `pg_database.datfrozenxid` 是所有表里最老的那个，只有最老的表被冻结，数据库的年龄才会前进。

| 写 TPS（1 事务 1 xid） | 到 2^31（21.5 亿） | 到 2^32（42.9 亿） | `autovacuum_freeze_max_age=2 亿` 的窗口 |
| --- | --- | --- | --- |
| 1,000 | 24.9 天 | 49.7 天 | 55.6 小时 |
| 5,000 | 4.97 天 | 9.94 天 | 11.1 小时 |
| 20,000 | 1.24 天 | 2.49 天 | 2.78 小时 |
| 50,000 | 11.9 小时 | 23.9 小时 | 1.11 小时 |

算式只有一条：`天数 = 2^31 / (写 TPS × 86,400)`，`freeze 窗口小时 = 200,000,000 / (写 TPS × 3,600)`。

**性质：量级结论。** 假设每个写事务只消耗 1 个 xid、没有 savepoint 子事务、没有复制槽/预提交事务额外钉住 xid；读事务不消耗 xid。换假设要重算：一个事务里 10 条写语句仍然只吃 1 个 xid，而 10 个 savepoint 子事务会各吃一个 subxid。

源码里还有两个硬闸：`XID_STOP_LIMIT = 3,000,000`、`XID_WARN_LIMIT = 10,000,000`（`src/backend/access/transam/varsup.c`），即距离 2^31 边界只剩 300 万/1,000 万个事务时，分别**拒绝新写事务**和开始告警。按 5,000 TPS 算，告警到停写只有 1,400 秒——**23 分钟**。这就是为什么 `age(datfrozenxid)` 的告警线不能设在 90%，要设在 70% 左右。

---

## 11.4 一次读写链路：同一条 SQL 的两种页访问

### 主键点查：3 次 vs 4 次

```
SELECT * FROM orders WHERE id = 42;

InnoDB（表 = 聚簇索引）
  Buffer Pool
  ┌─────────────┐    id=42   ┌─────────────┐   id=42   ┌──────────────────────┐
  │ root 页      │ ─────────► │ branch 页    │ ────────► │ leaf 页：整行在这里   │
  └─────────────┘            └─────────────┘           └──────────────────────┘
       ①                        ②                        ③
  3 次页访问；命中 buffer pool 就直接返回，没有「回表」这一步。

PostgreSQL（堆 + 独立主键索引）
  ┌─────────────┐   id=42   ┌─────────────┐   id=42   ┌──────────────────────┐
  │ PK root 页   │ ────────► │ PK branch   │ ────────► │ PK leaf：ctid=(8842,7)│
  └─────────────┘           └─────────────┘           └──────────┬───────────┘
       ①                        ②                              ③│
                                                                  │ 用 ctid 定位堆页
                                                                  ▼
                                                       ┌──────────────────────┐
                                                       │ heap blk 8842：行版本 │
                                                       │ xmin/xmax/infomask   │
                                                       └──────────────────────┘
                                                                 ④
  3 次索引页 + 1 次堆页 = 4 次；若只取索引列且 VM 全可见，可省掉 ④。
```

| 步骤 | InnoDB `WHERE id=42` | PostgreSQL `WHERE id=42` | 差异来源 |
| --- | --- | --- | --- |
| 索引 root | 1 页 | 1 页 | 都是 B+ 树 |
| 索引 branch | 1 页 | 1 页 | 树高通常 3–4 层，点查只走一条路径 |
| 索引 leaf | 1 页，**整行就在叶子里** | 1 页，只有 key + `ctid` | PG 的叶子不是数据页 |
| 堆/回表 | 0 | **1 页** | PG 必须按 `ctid` 取行 |
| 可见性判定 | 读 Read View，必要时沿 `roll_ptr` 回溯 undo | 看行头 `infomask`；hint bit 未置时查 `pg_xact` | 旧版本位置不同 |
| 合计（缓存命中） | 3 次页访问 | 4 次页访问 | 多出来的一次就是「堆表」的代价 |

### 二级索引：PG 少走一棵树，但多一次堆页

`SELECT * FROM orders WHERE phone = '138...'`（`phone` 有二级索引、主键 `id`）：InnoDB 的 `idx_phone` 叶子存 `(phone, id)`，拿到 `id` 后还要再走一次聚簇索引（3 次），合计 3 + 3 = 6 次；PG 的 `idx_phone` 叶子存 `ctid`，拿到后只读一次堆页，合计 3 + 1 = 4 次。

这张对照是两种引擎最本质的取舍：

| 维度 | InnoDB 二级索引 | PostgreSQL 二级索引 | 谁赢 |
| --- | --- | --- | --- |
| 叶子存什么 | 索引键 + **主键值** | 索引键 + **6 B `ctid`** | 主键越宽，PG 越省空间 |
| 回表路径 | 再走一次聚簇索引（3 次） | 直接一次堆页（1 次） | PG 少 2 次页访问 |
| 回表后的数据 | 整行都在聚簇叶子 | 堆页里是行版本，还要判可见性 | InnoDB 更直接 |
| 范围扫描 | 二级索引顺序 → 聚簇索引随机 | 二级索引顺序 → 堆页随机；可用 bitmap 重排 | 都随机，PG 多一层 |
| 更新索引 | 被更新列在索引里才改；主键不变则二级索引不动 | 被更新列在索引里就要加新条目；HOT 例外 | InnoDB 的更新更省索引 |

### 更新：原地改 vs 写新行

同一句 `UPDATE orders SET status = 'paid', updated_at = now() WHERE id = 42`，假设 `status` 上有索引：

| 动作 | InnoDB | PostgreSQL |
| --- | --- | --- |
| 定位 | 聚簇索引 3 页 | 主键索引 3 页 + 堆 1 页 |
| 改数据 | 在聚簇叶子内原地更新（页内有空间时），旧值写 undo | 在堆里插入一条新版本，旧版本保留；新版本 `xmin=当前事务`，旧版本 `xmax=当前事务` |
| 索引 | 只有 `status` 索引需要加新条目（`updated_at` 若不在索引则不动）；`DB_ROLL_PTR` 指向 undo | `status` 索引加新条目；若 `updated_at` 也在索引里，它也要加；每个含被更新列的索引都膨胀 |
| 旧版本 | undo，由 purge 回收 | 堆页，由 VACUUM 回收 |
| 回滚 | 沿 undo 反向应用，回滚可能很慢 | 把事务标成 aborted 即可，回滚快；但死元组要等 VACUUM 清 |
| 页写放大 | 原地更新 + undo + redo | 新行 + 新索引条目 + WAL；旧行留在页里，抬高后续扫描成本 |

**HOT 是 PG 给「高频更新小字段」留的后门**：如果被更新的列不在任何索引里，且同一页还有空间，新版本就放在同一页，索引条目不动，只把行指针重定向到新版本。§11.6 详细讲。注意 HOT 只省索引，不省堆——旧版本还是要等 VACUUM。

**范围扫描的隐藏成本**：`WHERE id BETWEEN 1000 AND 2000` 在 InnoDB 里沿聚簇叶子顺序读，页是连续的；在 PG 里主键索引给出 1,000 个 `ctid`，堆页可能散落在整个文件，优化器要么走 `Index Scan`（随机 IO），要么走 `Bitmap Heap Scan`（先按块号排序再读，顺序变好但丢掉顺序）。`EXPLAIN (ANALYZE, BUFFERS)` 里 `Rows Removed by Filter`、`Buffers: shared read` 和 `Heap Fetches` 是看这类代价的三个入口。

---

## 11.5 MVCC 对照：堆里的旧版本 vs undo 里的旧版本

### 两条版本链长什么样

```
PostgreSQL：旧版本就在堆页里，靠 xmin/xmax 判断谁看得见
  heap blk 8842
  ┌─────────────────────────────────────────────────────────────┐
  │ lp 7  → [xmin=100, xmax=150, ctid=(8842,9)]  ────────────┐  │
  │ lp 9  → [xmin=150, xmax=0,   ctid=(8842,9)]  ◄───────────┘  │
  │ lp 12 → [xmin=90,  xmax=120, ctid=(8842,7)]  死版本，等 VACUUM│
  └─────────────────────────────────────────────────────────────┘
  读事务用自己的快照 xid 去比 xmin/xmax：xmax 还没提交或在我之后 ⇒ 我看见旧版本；
  xmax 已提交且在我之前 ⇒ 这行对我不可见。没有「回溯链」可走。

InnoDB：堆/聚簇页上只有当前版本，旧版本在 undo
  聚簇索引叶子 row(id=42):
    DB_TRX_ID=150 | DB_ROLL_PTR ──► undo slot
                                    │
                                    ▼
                    [trx 150 前像: status='created'] ──► [trx 100 前像] ──► ...
  读事务拿 Read View 判断当前版本可见性；不可见就沿 roll_ptr 往回溯，
  在内存里拼出旧版本。堆页永远只有一行。
```

### 四列对照：同一个 MVCC，两张账单

| 维度 | PostgreSQL（堆内版本） | InnoDB（undo 版本） | 工程后果 |
| --- | --- | --- | --- |
| 旧版本存放 | 堆页里的独立元组，`xmin/xmax` 标记 | undo 表空间里的前像，`DB_ROLL_PTR` 链 | PG 膨胀的是表；InnoDB 膨胀的是 undo |
| 行内指针 | `ctid` 指向下一版本；HOT 用行指针重定向 | 6 B `DB_TRX_ID` + 7 B `DB_ROLL_PTR` | PG 行头 23 B；InnoDB 行头 13 B（5 B 头 + 6 + 7，另加可变长头） |
| 可见性判断 | 比较 `xmin/xmax` 与快照；hint bit 加速 | Read View 四字段 + 沿 undo 链回溯 | PG 读当前行即可判定；InnoDB 旧版本越深读越慢 |
| 更新方式 | 插入新版本，旧版本留在原页/新页 | 聚簇叶子原地更新，旧值写 undo | PG 写放大在堆和索引；InnoDB 写放大在 undo 和 redo |
| 回收器 | `VACUUM`：扫堆、清索引、冻结、设 VM | purge：沿 history list 清理 undo，可回滚段截断 | PG 的 VACUUM 是独立扫描；InnoDB purge 是后台持续线程 |
| 回收边界 | 最老快照（`backend_xmin`、复制槽、prepared xact） | 最老 Read View（长事务、只读事务） | 两者都被「最老读者」钉住，只是钉子插在不同表上 |
| 长事务的表现 | 表/索引膨胀，VM 不置位，index-only 退化 | undo 表空间涨、history list 涨、purge 落后 | 一个撑爆数据盘，一个撑爆 undo 盘 |
| 回滚成本 | 快：标 aborted，死元组等 VACUUM | 慢：反向应用 undo，大事务回滚可能几小时 | PG 的回滚便宜，但把成本推给了后续 VACUUM |
| 空间归还 | VACUUM 不缩文件；`VACUUM FULL`/pg_repack 才缩 | undo 可 truncate；表空间缩要重建 | 两者都没有「自动缩表」 |
| 崩溃恢复 | WAL 重放 + hint bit 重建 + VACUUM 清理 | redo 重放 + undo 回滚未提交事务 | PG 恢复后可能带着大量死元组；InnoDB 恢复后可能带着长 history list |

读完这张表，两句话就够用了：

1. **PG 的 MVCC 把「旧版本」变成表的一部分**，所以它的日常运维是「扫垃圾」；InnoDB 把旧版本变成 undo 的一部分，所以它的日常运维是「追 purge」。两者都不是「配置一下就不管」的机制。
2. **VACUUM ≈ InnoDB 的 purge**，但 VACUUM 多做了两件 InnoDB 不需要单独做的事：清索引里的死条目、推进 freeze/VM。代价是它必须**扫表**；表越大、越膨胀，扫得越慢，而回收量并不会随表变大而增加——这就是「膨胀自我加速」的根。

一个常被忽略的差异：**PG 的读路径不沿版本链回溯**。索引指向哪个 `ctid`，就读哪个堆元组；可见就返回，不可见就跳过（HOT 链会跟一下行指针）。所以「一行改了 100 次」不会让某次读慢 100 倍——它只会让 VACUUM 多扫 100 个版本、让索引多 100 个条目。InnoDB 相反：当前行永远在聚簇叶子里，但如果你的 Read View 很老，就要沿 undo 链回溯，链越长越慢。**PG 把成本摊给写和清理，InnoDB 把成本摊给老快照的读。**

---

## 11.6 VACUUM 三形态与 HOT

### 三种形态：同一把扫帚，三种挥法

| 形态 | 触发 | 锁 | 它做了什么 | 空间还 OS | 代价 | 什么时候用 |
| --- | --- | --- | --- | --- | --- | --- |
| `VACUUM` | 手动 / autovacuum | `SHARE UPDATE EXCLUSIVE`（9.4+） | 扫堆、清索引死条目、标行指针可重用、冻结老行、设 VM；尾部全空页可截断 | **不还**（中间空洞留在文件里） | 读堆 + 读所有索引；不阻塞读写，但和别的 VACUUM/DDL 冲突 | 日常回收、控制膨胀、维持 VM |
| `VACUUM FULL` | 手动 | `ACCESS EXCLUSIVE` | 新建 relfilenode，按活行重写堆，重建全部索引，提交后删旧文件 | **还**（文件变小） | 约 2 倍磁盘峰值；全表重写；**全程锁表** | 批量删除后回收、严重膨胀、需要缩小文件 |
| `autovacuum` | 自动，按表阈值 | 同 `VACUUM` | 同 `VACUUM`，但受 `cost_delay/cost_limit` 节流，最多 `autovacuum_max_workers` 个并发 | 不还 | 默认被限速；长快照会让它白扫 | 稳态维护；不要把它当 `VACUUM FULL` |
| `VACUUM FREEZE` | 手动 / 防回卷 | 同 `VACUUM` | 强制冻结，扫描所有未全冻结的页 | 不还 | 全表扫描，IO 大；9.6+ 可跳过全冻结页 | xid 告警、升级前、灾后急救 |

三个关键细节：

1. **普通 VACUUM 不锁表，但也不是完全无锁**。它持 `SHARE UPDATE EXCLUSIVE`，与 `VACUUM`、`ANALYZE`、`CREATE INDEX CONCURRENTLY`、部分 `ALTER TABLE` 冲突；读写业务不受影响。最后截断尾部空页时会短暂拿 `ACCESS EXCLUSIVE`，所以「VACUUM 完全不锁表」这句话只在大部分阶段成立。
2. **VACUUM 不能缩文件**。它把死元组占的行指针标成 `LP_UNUSED`，空间回到页内 free space / 表的 free list，下一次 INSERT/UPDATE 可以复用；但页还在文件里。只有 `VACUUM FULL`、`pg_repack`、`CLUSTER`、`TRUNCATE`/分区 DROP 才会让文件真的变小。
3. **`VACUUM FULL` 不能替代日常 VACUUM**。它要求额外的等量磁盘，且全程 `ACCESS EXCLUSIVE`；在一个 200 GB 表上就是十几分钟的业务全停（§11.8 算账）。日常应该是「autovacuum 保持住 + 定期处理异常膨胀」，不是「每周 FULL 一次」。

```
VACUUM 前（一页）：
┌──────────────────────────────────────────────────────┐
│ lp1 死 │ lp2 死 │ lp3 活 │ lp4 死 │ lp5 活 │  free 15% │
└──────────────────────────────────────────────────────┘
VACUUM 后（同一页）：
┌──────────────────────────────────────────────────────┐
│ lp1 空 │ lp2 空 │ lp3 活 │ lp4 空 │ lp5 活 │  free 65% │
└──────────────────────────────────────────────────────┘
  ↑ 死行被清掉，行指针变 LP_UNUSED；页还在，空间可被新行复用。
    文件大小不变，除非整页都空且位于文件末尾（才可截断）。

VACUUM FULL 后（新文件）：
┌──────────────────────────────────────┐
│ 活行紧凑排列 │ 活行 │ 活行 │  ...    │  ← 页数减少，旧文件删除
└──────────────────────────────────────┘
  ↑ 需要约 2 倍磁盘峰值；旧文件在新文件提交后才删。
```

### 一次 VACUUM 内部的四段路

1. **扫堆**：从第一页读到最后一页，剪掉 HOT 链上的死版本、把死元组记进内存的 dead TID 数组。
2. **清索引**：对每个索引删掉指向 dead TID 的条目。内存装不下就分多轮，每轮把所有索引再扫一遍。
3. **清堆/冻结**：把已经无用的行指针标空，冻结足够老的行，按页设置 VM 位。
4. **收尾**：截断尾部全空页，更新统计信息（如果带 `ANALYZE`）。

第 2 步是很多人忽略的放大器：dead TID 数组受 `maintenance_work_mem`（默认 64 MB；autovacuum 用 `autovacuum_work_mem`，默认 -1 表示跟随）限制，每个 TID 约 6 B。2,000 万 dead tuple × 6 B = 120 MB > 64 MB，于是**索引要被扫两遍**；如果有 5 个索引，等于多读 5 个索引的全量页。`pg_stat_progress_vacuum.index_vacuum_count > 1` 就是这个信号。

`VACUUM (PARALLEL n)` 是 12+ 的选项，但它**只并行索引清理**，堆扫描仍是单进程；`max_parallel_maintenance_workers` 默认 2。不要指望它把大表的堆扫描变快。

### autovacuum 的触发与节流：两个默认值决定了它追不追得上

触发条件是 `dead_tuples > autovacuum_vacuum_threshold + autovacuum_vacuum_scale_factor × reltuples`；默认 `dead > 50 + 0.2 × 表行数`。

1 亿行的表，要累积 **2,000 万**个死元组才触发第一次 autovacuum；按每天更新 10%（1,000 万行）算，**两天才跑一次**。这就是为什么很多表在两次 autovacuum 之间已经膨胀了 20%。把表级参数改成 `autovacuum_vacuum_scale_factor=0.02`，触发点降到 200 万，约 4.8 小时一次，代价是更频繁的扫描。

节流条件是：每轮最多花 `vacuum_cost_limit=200` 的代价，然后睡 `autovacuum_vacuum_cost_delay`；页命中 `cost_page_hit=1`、未命中 `cost_page_miss=10`、脏页 `cost_page_dirty=20`；默认 2 ms（12+；12 之前是 20 ms）。

按页未命中算：200 / 10 = 20 页 / 2 ms = 10,000 页/s × 8 KB ≈ **80 MB/s**；按脏页算只有约 40 MB/s。12 之前默认 20 ms，就是约 8 MB/s——**一个大表扫一天都扫不完，这就是「autovacuum 永远追不上」的算术来源**。它不是不干活，是被自己的刹车片按住了。

### HOT 与 fillfactor：让更新留在同一页

HOT（Heap-Only Tuple）成立要同时满足两个条件：

1. **被更新的列不在任何索引里**（包括主键、唯一索引、表达式索引、部分索引的谓词列）；
2. **同一页有足够空间**放新版本（页内 free space 或可剪掉的死版本腾出的空间）。

满足时，新版本放在同一页，旧行指针改成指向新版本（`LP_REDIRECT`），**索引条目一个都不动**；VACUUM 只剪 HOT 链，不碰索引。只要有一个被更新列在索引里，或者页满了，就退化成普通更新：新版本可能去别的页，所有相关索引都要加条目。

`fillfactor` 是给未来更新留的页内空间。堆表默认 100（页填满），对有热点更新的表就是灾难：

| `fillfactor` | 每页预留 | 好处 | 代价 | 适用 |
| --- | --- | --- | --- | --- |
| 100（默认） | 0 | 同样行数占最少页，全表扫描最快 | 页很快满，HOT 难成立，更新容易跨页 | 只读/追加型表 |
| 90 | 10% | 通常能容纳 1–2 次同页更新 | 页数多约 11% | 一般更新表 |
| 80 | 20% | 热点行多次更新仍能 HOT | 页数多约 25% | 高频更新、行不太宽的表 |
| 70 | 30% | 极端热点，最大化 HOT | 页数多约 43%，扫描变慢 | 少数热点表，谨慎 |

`fillfactor` 只影响**之后写入的页**（以及 `VACUUM FULL`/`pg_repack` 重写后的页），不会自动重排已有页。改完参数要配合一次重写才看得到完整效果。

---

## 11.7 index-only scan 与 visibility map

### 覆盖索引在 PG 上还差一张位图

B-tree 索引叶子只有 key + `ctid`，没有 `xmin/xmax`；索引本身**不知道**这条记录对应的堆行是不是还活着、对当前快照可不可见。所以「索引里有我要的列」不等于「不用回表」——还要证明**那一页上所有行对所有事务都可见**。这个证明就是 visibility map（VM）。

VM 是堆文件的旁路位图：每个堆页 2 bit（9.6+）：

- **all-visible**：这一页所有行对所有快照都可见，index-only scan 可以只看索引；
- **all-frozen**：这一页所有行都已冻结，VACUUM 可以跳过（9.6+ 的 freeze map）。

位图很小：2 bit/页 = 1 B / 4 页；1 TB 堆（约 1.34 亿页）的 VM 约 32 MB。**它不是缓存，是磁盘上的文件**，跟着堆文件一起持久化。

```
index-only scan 的判定：
  index leaf: (key, ctid=(blk, off))
        │
        ▼
  VM[blk] 的 all-visible 位？
    ├─ 1 → 直接返回索引里的列，不读堆页         ← 真正的 index-only
    └─ 0 → 用 ctid 读堆页，判 xmin/xmax 可见性   ← 退化成 index scan + heap fetch
```

对 `SELECT status, count(*) FROM orders WHERE status='paid' GROUP BY status` 做 `EXPLAIN (ANALYZE, BUFFERS)`，输出里看三行：`Index Only Scan using idx_orders_status`、`Heap Fetches: 18342`、`Buffers: shared hit=... read=...`。`Heap Fetches` 不为 0 就说明 VM 没全置位。

`Heap Fetches` 是 VM 的体温计：0 表示这一页全可见，索引就够；接近扫描行数表示几乎每行都回了堆，index-only scan 已经名存实亡。`EXPLAIN` 选它，是因为优化器**假设** VM 大部分是置位的；如果实际没置位，执行器只能一边扫索引一边回表，可能比顺序扫还慢。

### 为什么 VACUUM 跟不上，索引扫描会变慢

VM 的 all-visible 位**只有 VACUUM 会设置**（以及少数操作，如 `VACUUM FULL`/`CLUSTER` 重写）。而且设置条件很苛刻：这一页不能有任何死元组，且所有行都要对「所有活快照」可见。

于是形成一条直接的因果链：

- 长事务/复制槽/预提交事务 → 最老快照钉住；
- VACUUM 删不掉这一页上的死元组 → 这一页不能标 all-visible；
- index-only scan 每次都要回表 → 索引扫描的随机堆页 IO 上升；
- 查询变慢，但没有任何「错误」。

这解释了 §11.1 事故一里一个很反直觉的现象：表都 608 GB 了，`SELECT count(*) WHERE status=?` 还能跑，只是从 200 ms 变成 8 s——**它不是坏了，是每条索引记录都要去堆里确认一次**。`VACUUM` 恢复后，VM 位逐步置上，同样的查询会突然变快；这不是优化器变了，是位图变了。

对比 InnoDB：它没有独立的 VM 文件，但在二级索引页头有 `PAGE_MAX_TRX_ID` 之类的近似信息，用来判断「这一页是否足够老、能不能不回聚簇索引」。两者都在回答同一个问题——**「索引里的可见性能不能信」**；PG 把这个答案放在堆外的位图里，所以 VACUUM 的责任更重。

---

## 11.8 三本账：膨胀、VACUUM FULL 与 xid 回卷

### 账一：1 亿行、300 B、日更 10%，dead tuple 每天产多少

先立假设：1 亿行、行均 300 B（不含行头）、每天更新 10% 的行、更新列不在索引里（所以不会额外写索引条目，但也不会 HOT 到完全不写堆）、4 个二级索引、每个索引约占堆的 20%。所有数字按十进制 GB（1 GB = 10^9 B）计。

| 量 | 推导 | 结果 |
| --- | --- | --- |
| 活数据（堆） | 1 亿 × 300 B | 30 GB |
| 每天死元组 | 1 亿 × 10% | 1,000 万个 |
| 每个死版本占盘 | 300 B 数据 + 24 B 行头 + 4 B 行指针 + 2 B 对齐 | 330 B |
| 每天新增死数据 | 1,000 万 × 330 B | 3.3 GB/天 |
| 30 天不回收 | 30 × 3.3 GB | 99 GB 死数据 |
| 30 天后的表 | (30 + 99) / 30 | **约 4.3 倍** |
| 90 天不回收 | 90 × 3.3 = 297 GB | **约 10.9 倍** |
| autovacuum 触发点 | 50 + 0.2 × 1 亿 | 2,000 万死元组 ≈ 2 天产量 |

注意「4.3 倍」是**以活数据 30 GB 为 1 倍**的口径；页头、行指针、空闲空间还会再叠约 10%，但倍数关系不变。如果 autovacuum 正常工作，它会在 2,000 万死元组（约 6.6 GB）时触发，表稳态大约 **1.2–1.4 倍**；一旦被长快照钉住 30 天，就是 4.3 倍，而且**回落后文件不会缩**——VACUUM 只把空间标成可重用，表会永远保持曾经到达过的最大尺寸。

### autovacuum 要多少 IO 才追得上

一次 VACUUM 的成本不是「死数据量」，是「它必须扫过的堆页 + 索引页」。10% 的更新均匀分布时，几乎每一页都有死元组，所以它要扫整个堆：

| 量 | 推导 | 结果 |
| --- | --- | --- |
| 一次扫堆 | 30 GB | 30 GB |
| 一次扫索引 | 4 × 6 GB | 24 GB |
| 单次总读 | 30 + 24 | 54 GB |
| 单次回收死数据 | 2 天 × 3.3 GB | 6.6 GB |
| 读放大 | 54 / 6.6 | **约 8.2 倍** |
| 每天摊到的读 | 54 / 2 天 | 27 GB/天 |
| 80 MB/s 下单次耗时 | 54 × 1,024 / 80 | 约 691 s ≈ 11.5 分钟 |
| 3 个 worker 的日产能 | 3 × 80 MB/s × 86,400 | 约 20.7 TB/天 |

把「单次回收」换成「每天 3.3 GB」来算，读放大是 54 / 3.3 ≈ **16.4 倍**；上表用 2 天触发一次，所以按 6.6 GB 算 8.2 倍。两种口径都对，关键看你怎么定义「一次」。**结论是：单张 30 GB 的表，默认 autovacuum 完全追得上；它每天只花 27 GB 读，占 3 个 worker 日产能的 0.13%。**

真正追不上的三种情况：

1. **表被钉住**：扫了也删不掉，读的 54 GB 全是无效功；表按 3.3 GB/天继续涨（事故一）。
2. **表已经膨胀到 4.3 倍**：同样回收 6.6 GB，要扫 129 GB 堆 + 103 GB 索引 ≈ 232 GB，读放大从 8 倍变成 **35 倍**；80 MB/s 下单次 48 分钟。膨胀让 VACUUM 更慢，VACUUM 更慢又让膨胀更久——正反馈。
3. **表数量 × 索引数量**：1,000 张这样的表，每 2 天各扫 54 GB = 27 TB/2 天 ≈ **312 MB/s** 的持续读，已经超过 3 个 worker × 80 MB/s = 240 MB/s 的产能；如果每张表有 10 个索引（90 GB/次），需求翻倍，必然排队。**autovacuum 的瓶颈通常不是单表，是「表数 × 索引数 × 触发频率」的总和。**

**性质：量级结论。** 假设：更新均匀分布、每个索引 6 GB、页未命中按 80 MB/s、`cost_delay=2 ms`、`cost_limit=200`、3 个 worker 不互相抢 IO、没有长快照；实际值随缓存命中率、索引数、行宽、`maintenance_work_mem` 在 2–3 倍内浮动。

### 账二：200 GB 表做 `VACUUM FULL`，要等多久、要多少盘

`VACUUM FULL` 不是「整理碎片」，是**重写整张表 + 重建全部索引**。设 200 GB 堆、索引合计 80 GB（40%），顺序带宽 500 MB/s：

| 项 | 计算 | 结果 |
| --- | --- | --- |
| 读旧堆 | 200 GB | 200 GB |
| 写新堆 | 200 GB | 200 GB |
| 读旧索引 + 写新索引 | 80 + 80 | 160 GB |
| IO 合计 | 200 + 200 + 160 | 560 GB |
| 500 MB/s 下的时间 | 560 × 1,024 / 500 | 1,147 s ≈ **19.1 分钟** |
| 峰值额外磁盘（不含 WAL） | 新堆 200 + 新索引 80 | **280 GB** |
| 峰值额外磁盘（含 WAL 约 1 倍） | 280 + 280 | **560 GB** |
| 锁 | `ACCESS EXCLUSIVE` | **全程 19 分钟**，不是最后一刻才锁 |

如果只算「200 GB 读 + 200 GB 写」的简化口径：400 × 1,024 / 500 = 819 s ≈ **13.7 分钟**；索引和 WAL 会把它推到 20–30 分钟。**这就是事故二里 100 GB 表 9 分钟的来源：100 + 100 + 50 + 50 = 300 GB，300 × 1,024 / 500 ≈ 614 s。**

比时间更贵的是锁的排队效应：`VACUUM FULL` 一旦开始等锁，后面所有新的 `SELECT` 都排在它后面；它拿不到锁，别人也进不来。所以「等 1 分钟就拿锁」也可能造成 1 分钟以上的全表不可用。正确姿势：`lock_timeout` + 维护窗口 + `pg_repack`（在线重写，仍需约 2 倍磁盘和主键/唯一键）；或者干脆用分区，把「重写 200 GB」变成「DROP 一个分区」。

**性质：量级结论。** 假设：顺序带宽 500 MB/s、无业务竞争、索引占表 40%、WAL 量约等于新写页、无压缩；实际在共享云盘、有并发、有 TOAST/大字段时会差 2–4 倍。**承诺值只有一句：锁从开始重写到提交一直持有，期间该表所有读写都进不来。**

### 账三：5,000 写 TPS 的 xid 回卷时钟

| 量 | 推导 | 结果 |
| --- | --- | --- |
| 到 2^31（21.5 亿） | 2,147,483,648 / 5,000 / 86,400 | **4.97 天** |
| 到 2^32（42.9 亿） | 4,294,967,296 / 5,000 / 86,400 | **9.94 天** |
| `autovacuum_freeze_max_age=2 亿` 的窗口 | 200,000,000 / 5,000 / 3,600 | **11.1 小时** |
| `vacuum_freeze_table_age=1.5 亿` | 150,000,000 / 5,000 / 3,600 | **8.33 小时** |
| `vacuum_freeze_min_age=5,000 万` | 50,000,000 / 5,000 / 3,600 | **2.78 小时** |
| 距停写还剩 300 万 xid | 3,000,000 / 5,000 | **600 秒 = 10 分钟** |
| 距告警还剩 1,000 万 xid | 10,000,000 / 5,000 | **2,000 秒 = 33 分钟** |

注意最后两行：**从「源码告警」到「拒绝写入」只有 33 分钟；从「停写线」到真正停写只有 10 分钟**。这不是给你留一天处理的时间，是给你留一次 on-call 打电话的时间。

更重要的不是「多少天」，是 **freeze 的带宽税**：在一个 freeze 窗口内，数据库必须把所有「可能含未冻结 xid」的页扫一遍。最坏情况每张表都要全表扫描：

| 写 TPS | 到 2^31 | freeze 窗口（2 亿） | 200 GB 表每张需要的持续读 | 500 MB/s 盘最多撑几张表 |
| --- | --- | --- | --- | --- |
| 1,000 | 24.9 天 | 55.6 小时 | 1.0 MB/s | 500 张 |
| 5,000 | 4.97 天 | 11.1 小时 | 5.0 MB/s | 100 张 |
| 20,000 | 1.24 天 | 2.78 小时 | 20 MB/s | 25 张 |
| 50,000 | 11.9 小时 | 1.11 小时 | 50 MB/s | 10 张 |

算式：`每张表持续读 = 200 GB / freeze 窗口秒数`；`最多张数 = 500 MB/s / 每张读`。**在 5,000 写 TPS 下，一台 500 MB/s 盘的机器最多「养」100 张 200 GB 的表；超过这个数，freeze 的扫描需求就超过磁盘带宽，autovacuum 会永远落后，直到 xid 停写。** 20,000 TPS 时只剩 25 张——事故三的 30 张表已经超了。

实际比这乐观一点：9.6+ 的 freeze map 允许跳过全冻结页，只扫「上次 freeze 之后被改过的页」；如果表是追加型、更新集中，扫描量远小于全表。但**最坏情况就是全表**，容量规划必须按最坏情况做，否则一次全表更新就会把 freeze 预算打穿。

**性质：量级结论。** 假设：每个写事务 1 个 xid、无 savepoint、无复制槽/预提交额外钉住、每张表 200 GB、每个 freeze 窗口全表扫一遍、500 MB/s 顺序带宽、freeze 不与业务 IO 争抢。实际 freeze 读的是堆页（不读索引），9.6+ 可跳过全冻结页，所以这是上界；把表换成 50 GB 或把 TPS 减半，数字线性变化。

三本账合起来是一个闭环：**膨胀让 VACUUM 变慢 → VACUUM 变慢让 freeze 排队 → freeze 排队让 `age(datfrozenxid)` 逼近 2^31 → 你被迫在只读保护下做更重的扫描。** PG 的容量规划不是「磁盘够不够」，是「磁盘带宽能不能同时付得起业务 IO、VACUUM 扫描和 freeze 扫描」。

---

## 11.9 架构边界：PG 能做什么、InnoDB 能做什么

### 索引族：PG 的富裕 vs InnoDB 的克制

PG 的索引是**独立的访问方法（access method）**，表和索引解耦，所以可以挂很多种结构；InnoDB 的索引长在聚簇索引这棵树上，选择面窄得多。选型时这一栏经常被忽略，但它决定了很多查询形状能不能落地。

| 索引 | 物理结构 | 什么查询形状用它 | 版本 / 开关 | InnoDB 对应 |
| --- | --- | --- | --- | --- |
| **B-tree** | 有序平衡树，叶子存 key + `ctid` | 等值、范围、`ORDER BY`、唯一约束；默认 | 内置 | 聚簇 B+ 树 + 二级 B+ 树 |
| **Hash** | 哈希桶，只存 hash 值 | 纯等值；范围/排序不行 | WAL 记录 10+，此前崩溃可能损坏 | 只有内部 adaptive hash，不能显式建 |
| **GIN** | 倒排：key → posting list | 数组/JSONB 包含 `@>`、全文 `@@`、`pg_trgm` 的 `LIKE '%x%'` | 内置；`pg_trgm` 需扩展 | FULLTEXT 只覆盖文本，不覆盖 JSONB/数组 |
| **GiST** | 平衡树，节点存谓词/包围盒 | 几何相交、范围类型、KNN `<->` 最近邻 | 内置；PostGIS 等扩展 | SPATIAL（R-tree）只覆盖几何 |
| **SP-GiST** | 空间分区树（quadtree 等） | 点、前缀、非平衡空间数据 | 内置 | 无 |
| **BRIN** | 按块区间存 min/max，体积极小 | 超大追加表上的时间/自增列范围扫描；**要求物理相关性** | 内置 | 无 |
| **部分索引** | 只索引满足 `WHERE` 的行 | 热子集（`WHERE status='pending'`）、软删除表的 `deleted_at IS NULL` | 内置 | 无；只能用生成列/额外表模拟 |
| **表达式索引** | 索引表达式结果 | `lower(email)`、`(payload->>'sku')`、`date(created_at)` | 内置 | 8.0.13+ 函数键，或生成列 + 索引 |
| **`INCLUDE` 覆盖列** | 叶子附加非键列 | 让 index-only scan 少回表 | 11+ | 二级索引叶子自动带主键 |

三条使用纪律：

1. **GIN 写慢、读快、占空间**。它适合「多值包含」而不是高频更新；JSONB 上每个 key 都可能进 posting list，更新一行可能改很多条目。热点更新表不要在每个 JSON 字段上都建 GIN。
2. **BRIN 只在物理相关时有用**。一张按时间追加、从不乱序的表，BRIN 可能只有几 MB，却能替代几十 GB 的 B-tree；一旦数据乱序（比如随机 UUID 主键），BRIN 的 min/max 区间会覆盖全表，退化成全扫。
3. **部分索引/表达式索引是 PG 的独门武器**。`CREATE INDEX idx_pending ON orders(created_at) WHERE status='pending'` 只索引 1% 的热数据，既小又快；代价是查询必须带同样的谓词，否则用不上。InnoDB 没有部分索引，只能用生成列或拆表模拟；这也是很多「PG 能做的查询形状，MySQL 要做架构改造」的来源。

### 能力边界：三列对照

| 能力 | PostgreSQL | InnoDB | 架构含义 |
| --- | --- | --- | --- |
| 表形态 | 无序堆 + 独立索引 | 主键聚簇 B+ 树 | PG 的主键范围扫描是「索引顺序 + 堆随机 IO」；InnoDB 是顺序扫描 |
| 更新 | 写新版本，旧版本留堆 | 原地更新，旧值进 undo | PG 的表会膨胀；InnoDB 的 undo 会膨胀 |
| 索引族 | B-tree / Hash / GIN / GiST / SP-GiST / BRIN / 部分 / 表达式 | B+ 树 / FULLTEXT / SPATIAL；函数键 8.0.13+ | PG 能直接支持多值、地理、范围、热子集；InnoDB 常要外挂 ES/生成列 |
| 清理 | `VACUUM` / autovacuum | purge 线程 | 都需要被最老快照/最老 Read View 推着走 |
| 空间归还 | `VACUUM FULL` / `pg_repack` / `CLUSTER` | `OPTIMIZE TABLE` / 重建表空间 | 都要重写，都要约 2 倍磁盘，都不是在线零成本 |
| 事务 ID | 32 位，2^31 半区，必须 freeze | 6 B `DB_TRX_ID`（48 位），没有 42.9 亿回卷 | PG 多一条「xid 时钟」运维线；InnoDB 没有 |
| DDL | 事务性 DDL，可回滚 | 8.0 原子 DDL，但隐式提交 | PG 的 DDL 可以放在事务里；MySQL 的 DDL 不是事务的一部分 |
| 复制 | 流复制 + 逻辑解码（`wal_level=logical`） | binlog + 行复制 | 都能做读写分离；PG 的逻辑槽会钉 xmin，MySQL 的 binlog 不会 |
| 原生分片 | **没有**；分区是单机，Citus 是扩展 | **没有**；中间件/分库分表 | 两者都是单机内核，横向扩要换零件（§11.9 扩展维度） |

### 扩展维度：四选一，或者直说不行

| 扩展维度 | PostgreSQL | InnoDB | 结论 |
| --- | --- | --- | --- |
| 分片 | 无原生分片；声明式分区（10+）只是单机内切表；Citus 扩展可做分布式 | 无原生分片；中间件（ShardingSphere 等）或分库分表 | 都要在应用/中间件层做路由；**内核本身不解决跨分片事务** |
| 副本 | 流复制（物理）、逻辑复制；只读副本可分担查询 | 异步/半同步复制、MGR | 读扩展可行；写仍然单主（或换 NewSQL） |
| 垂直切 | 加 CPU/内存/更快的盘；`shared_buffers`、`work_mem` 调优 | 同样 | 单机上限内最便宜；但 freeze/VACUUM 的 IO 税随库变大线性涨 |
| 批处理摊薄 | `COPY`、多行 `INSERT`、分区批量 `DETACH` | 多行 `INSERT`、`LOAD DATA`、分区 | 能摊薄单位写入成本，但不改变「单机内核」这个事实 |

**直说**：PostgreSQL 和 InnoDB 都**不可横向扩**。它们的扩展维度是「垂直 + 读副本 + 单机分区」，写分片必须引入外部组件（Citus、中间件、或 TiKV 这类 NewSQL，→ comp/19）。当单表/单库超过 freeze 预算或单机 IO 上限时，正确动作是**换零件**，不是继续调 `autovacuum`。

### 系统级失效：只列三种

这里只写「会让整个库不可用/数据不可信/成本失控」的系统级失效；索引建错、`fillfactor` 设错、`cost_delay` 设大这类使用级错误归 §11.10。

| 系统级失效 | 触发 | 表现 | 影响范围 | 兜底 |
| --- | --- | --- | --- | --- |
| **膨胀写满磁盘** | 长快照/复制槽/预提交事务钉住 + 高频更新 | `No space left on device`，写入和 WAL 都失败 | 整个实例（共享表空间/同一块盘） | 先杀钉住者/删槽，再 `VACUUM`；空间不够时 `VACUUM FULL` 也做不了（它需要额外空间） |
| **xid 回卷只读** | 写 TPS 高 + freeze 追不上 + 老 xid 未冻结 | `database is not accepting commands to avoid wraparound data loss` | 整个数据库的写事务 | drop 废弃槽/结束预提交，`VACUUM (FREEZE)` 最老的表，临时 `cost_delay=0` |
| **`VACUUM FULL`/`CLUSTER` 锁风暴** | 业务高峰执行重写，或有长查询挡住 `ACCESS EXCLUSIVE` | 锁队列把后续所有查询堵住，连接池打满 | 单表 + 连接池 + 依赖该表的全部业务 | `pg_cancel_backend` / `pg_terminate_backend`，改维护窗口，用 `pg_repack` |

### 换掉它的信号

| 信号 | 阈值 | 为什么 | 换什么 |
| --- | --- | --- | --- |
| 单表大到 VACUUM/freeze 扫不完 | 单表 > 1–2 TB 且日更 > 5% | 一次 VACUUM 的扫描量 ≈ 表大小；freeze 窗口固定 | 按时间/租户分区；或分片/NewSQL |
| 写 TPS 把 freeze 预算打穿 | 见 §11.8 表：5,000 TPS 时 500 MB/s 只能养约 100 张 200 GB 表 | xid 时钟不等人 | 提高盘带宽、降写 TPS、分库；换 InnoDB/NewSQL 可绕开 32 位 xid（但换来 undo） |
| 必须按主键顺序做大规模范围扫描 | 扫描行数 > 千万且要求顺序 IO | PG 堆表不聚簇，范围扫描会随机回表 | InnoDB 聚簇表、ClickHouse（列存）、或按聚簇键分区 |
| 需要频繁 `VACUUM FULL` 才能维持 | 每月 > 1 次，或单次 > 30 分钟 | 重写成本和锁成本不可持续 | 分区 + DROP、`pg_repack`、或换引擎 |
| 需要跨节点事务/全局快照 | 多分片写入 | PG 单机不提供全局事务 | Citus/NewSQL（→ comp/19） |

---

## 11.10 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到物理形态） | 定位 | 修复 | 防复发 |
| --- | --- | --- | --- | --- |
| **表持续变大，autovacuum 每晚都跑但 `n_dead_tup` 不降** | 一个长事务/长查询持有快照，`backend_xmin` 钉住；VACUUM 只能删「对所有活快照都不可见」的版本 | `pg_stat_activity` 按 `age(backend_xmin)` 排序；看 `state='active'` 且 `xact_start` 很老的会话 | 结束/终止该会话；确认业务后 `pg_terminate_backend(pid)`；等 VACUUM 追 | 事务寿命监控；报表/导出走只读副本；`statement_timeout` |
| **连接池里一堆 `idle in transaction`，表慢慢涨** | 应用开了事务去等用户输入/HTTP 调用；会话不结束，快照就不放，旧版本不能删 | `pg_stat_activity.state='idle in transaction'` + `now()-state_change`；`pg_stat_activity.backend_xmin` | 设 `idle_in_transaction_session_timeout='60s'`（9.6+）；连接池回收；改应用：事务只包 DB 操作 | 连接池 `maxLifetime`/`idleTimeout`；代码评审禁止事务跨 RPC；监控 `idle in transaction` > 60 s |
| **`pg_prepared_xacts` 里有一条三天前的记录，没人知道是谁** | 两阶段提交的 prepared 事务持有 xid 和锁，直到 `COMMIT PREPARED`/`ROLLBACK PREPARED`；协调者崩了就成了悬挂 | `SELECT gid, prepared, age(transaction) FROM pg_prepared_xacts;`；`max_prepared_transactions` 是否为 0 | 在对应数据库执行 `COMMIT PREPARED 'gid'` 或 `ROLLBACK PREPARED 'gid'`；先和业务确认哪边是权威 | 能不用 XA 就不用；用则监控 prepared 年龄 > 5 分钟；协调者要有超时/补偿 |
| **逻辑复制槽 `active=false`，表膨胀 + `pg_wal` 涨** | 逻辑槽的 `xmin` 是「消费者还可能需要的旧事务」；不消费就永远不推进，VACUUM 不能删它之后的死元组 | `pg_replication_slots`：`active`、`xmin`、`catalog_xmin`、`restart_lsn`；`pg_wal` 目录大小 | 确认下游废弃后 `pg_drop_replication_slot('slot')`；下游恢复消费；`max_slot_wal_keep_size`（13+）只防 WAL 撑爆，不释放 xmin | 非活跃槽 > 15 分钟告警；为每个槽设负责人/TTL；分析型消费不要长期挂槽 |
| **晚高峰 `VACUUM FULL`，全表 9 分钟不可用** | `ACCESS EXCLUSIVE` 全程持有；锁请求入队后，后续 `SELECT` 也排在它后面 | `pg_locks` 里 `mode='AccessExclusiveLock'`、`granted=false`；`pg_blocking_pids()`（9.6+）；`pg_stat_progress_cluster`（12+） | 取消/终止；改维护窗口；用 `pg_repack` 在线重写；设 `lock_timeout` 让它在拿不到锁时快速失败 | 生产默认禁用 `VACUUM FULL`；把「重写」纳入变更评审；大表分区 |
| **批量 `DELETE` 掉 40%，表一点没缩，盘还占着** | 删除只把行标死、行指针留着；VACUUM 把空间标成可重用，但不还给 OS；中间空洞无法跨页复用 | `pgstattuple('t')` 看 `dead_tuple_percent` / `free_percent`；`pg_stat_user_tables.n_dead_tup`；`pg_class.relpages` | 先 `VACUUM` 让空间可复用；要缩文件用 `VACUUM FULL`/`pg_repack`（窗口 + 2 倍盘）；时间序列直接 `DROP`/`DETACH` 分区 | 大删除改成「按分区 DROP」；软删除表加部分索引；删除后必跑 VACUUM 并巡检 |
| **autovacuum 一直在跑，但表还是越来越胀，磁盘 IO 却不高** | `autovacuum_vacuum_cost_delay=2 ms` + `cost_limit=200` 把扫描压到约 40–80 MB/s；大表一次要几小时，触发间隔比扫描时间短 | `pg_stat_progress_vacuum.heap_blks_scanned` 长时间不动；`wait_event='VacuumDelay'`；`index_vacuum_count > 1` | 对大表设表级 `autovacuum_vacuum_cost_delay=0` 或 `1ms`，提高 `cost_limit`；加 `maintenance_work_mem`；必要时窗口内手动 `VACUUM (VERBOSE)` | 容量规划算「单次扫描时间 < 触发间隔」；分区；监控 `VacuumDelay` 占比 |
| **`CLUSTER` 后查询快了一周，又慢慢变慢** | `CLUSTER` 只是一次性重排；后续 `INSERT` 按空闲空间落，不维持索引顺序；`pg_stats.correlation` 逐渐下降 | `pg_stats.correlation`（目标列）；`EXPLAIN (ANALYZE, BUFFERS)` 看堆页访问；`pg_class.relpages` | 重新 `CLUSTER`（窗口 + `ACCESS EXCLUSIVE`）或接受；长期靠分区/插入模式 | 不把 `CLUSTER` 当持续聚簇；需要持续顺序就按时间分区或换聚簇引擎 |
| **原本 index-only 的查询突然变慢，`Heap Fetches` 接近行数** | 批量更新把页的 all-visible 位清了；VACUUM 没跟上，VM 位没重新置上；index-only 退化成 index + heap fetch | `EXPLAIN (ANALYZE, BUFFERS)` 的 `Heap Fetches`；`pg_visibility` 扩展（9.6+） | 对该表跑 `VACUUM`；确认没有长快照挡住；批量导入后固定加一次 VACUUM | 把 `Heap Fetches` 纳入慢查询巡检；批量写入后触发 VACUUM/ANALYZE |

两段可直接跑的巡检：

```sql
-- ① 谁在钉住回收：按 backend_xmin 年龄排序
SELECT pid, usename, state,
       now() - xact_start AS xact_age,
       age(backend_xmin) AS xmin_age,
       wait_event_type, wait_event,
       left(query, 60) AS query
FROM pg_stat_activity
WHERE backend_xmin IS NOT NULL
ORDER BY xmin_age DESC
LIMIT 10;

-- ② 表的死元组与 autovacuum 滞后
SELECT relname, n_live_tup, n_dead_tup,
       round(100.0 * n_dead_tup / greatest(n_live_tup, 1), 1) AS dead_pct,
       last_autovacuum, autovacuum_count,
       pg_size_pretty(pg_total_relation_size(relid)) AS total_size
FROM pg_stat_user_tables
WHERE n_dead_tup > 100000
ORDER BY n_dead_tup DESC
LIMIT 20;
```

```sql
-- ③ 谁在等 ACCESS EXCLUSIVE，谁挡住了它（pg_blocking_pids 9.6+）
SELECT a.pid, a.state, a.wait_event_type, a.wait_event,
       now() - a.xact_start AS xact_age,
       l.mode, l.granted, c.relname,
       pg_blocking_pids(a.pid) AS blockers
FROM pg_locks l
JOIN pg_class c ON c.oid = l.relation
JOIN pg_stat_activity a ON a.pid = l.pid
WHERE c.relname = 'orders_archive'
  AND l.mode = 'AccessExclusiveLock'
ORDER BY l.granted, a.pid;

-- ④ 重写进度（12+；VACUUM FULL/CLUSTER 用 pg_stat_progress_cluster）
SELECT p.pid, c.relname, p.phase,
       p.heap_blks_total, p.heap_blks_scanned,
       p.heap_tuples_scanned, p.heap_tuples_written,
       p.index_rebuild_count
FROM pg_stat_progress_cluster p
JOIN pg_class c ON c.oid = p.relid;
```

排查顺序只有一条：**先看最老快照（`age(backend_xmin)`），再看死元组斜率，最后看表大小和磁盘**。反过来先看磁盘，永远只能得到「盘满了」这个结果，得不到原因。`n_dead_tup` 是估算值，随 VACUUM/ANALYZE 更新，不要拿它当精确膨胀率；精确值用 `pgstattuple`。

---

## 11.11 观测：指标、拐点与告警

### 六个必须有的数

| 指标 | 版本 / 开关 | 看什么、拐点在哪 | 告警骨架 |
| --- | --- | --- | --- |
| **最老事务年龄** | `pg_stat_activity.xact_start` 全版本；`backend_xmin` 9.4+；`track_activities=on`（默认） | `state='idle in transaction'` 或 `xact_start` 超过 5 分钟；`age(backend_xmin)` > 10 分钟 | `now()-xact_start > interval '5 minutes'`；按 `xmin_age` 排序取 top |
| **死元组比例** | `pg_stat_user_tables.n_dead_tup` 9.1+；`track_counts=on`（默认）；**估算值** | `n_dead_tup / n_live_tup > 20%` 且 `last_autovacuum` 超过 1 小时；> 50% 为严重 | `dead_pct > 20 AND last_autovacuum < now()-'1 hour'` |
| **xid 年龄** | `pg_database.datfrozenxid` 全版本；`age()` 函数 | 70% of 2^31（约 15 亿）警告；85%（约 18 亿）严重；> 20 亿接近停写 | `age(datfrozenxid) > 1500000000`；按 `age` 降序取 top |
| **精确膨胀率** | `pgstattuple` 扩展；`pgstattuple_approx` 9.5+（采样，不锁表） | `dead_tuple_percent > 20%` 或 `free_percent > 30%` 且活数据没涨 | 巡检任务每天跑 `pgstattuple_approx`，结果入 TSDB |
| **VACUUM 进度** | `pg_stat_progress_vacuum` 9.6+；`pg_stat_progress_cluster` 12+ | `heap_blks_scanned` 10 分钟不动；`index_vacuum_count > 1` 说明内存不够 | `rate(heap_blks_scanned[10m]) == 0` 且 phase 非收尾 |
| **非活跃复制槽** | `pg_replication_slots` 9.4+；逻辑槽需 `wal_level=logical`、`max_replication_slots>0` | `active=false` 且 `xmin IS NOT NULL` 超过 15 分钟；`restart_lsn` 落后导致 `pg_wal` 涨 | `NOT active AND xmin IS NOT NULL`，按 `age(xmin)` 排序 |
| **预提交事务** | `pg_prepared_xacts`；需 `max_prepared_transactions>0` | 任何 `age(transaction) > 5 分钟` 都值得看；> 1 小时必须处理 | `age(transaction) > 300` |
| **index-only 退化** | `EXPLAIN (ANALYZE, BUFFERS)` 的 `Heap Fetches`；index-only 9.2+ | `Heap Fetches / rows > 50%`；同一查询从 0 涨到数万 | 慢查询日志里抓 `Heap Fetches`，按表聚合 |

三条版本/开关纪律，写错一个读者就会怀疑全篇：

1. `pg_stat_progress_vacuum` 是 **9.6+**；`VACUUM (PARALLEL n)` 是 **12+**，且只并行索引清理；`pg_stat_progress_cluster` 是 **12+**，`VACUUM FULL`/`CLUSTER` 的进度看它，不看 `pg_stat_progress_vacuum`。
2. `pgstattuple` 要 `CREATE EXTENSION pgstattuple`；全表版会拿 `AccessShareLock` 并真的扫表，大表别在主库高峰跑；`pgstattuple_approx` 9.5+ 用 VM 采样，快但会有误差。
3. `n_dead_tup` 是**估算**，不是实时精确值；`pg_stat_activity.backend_xmin` 是**快照需要的最老 xid**，不是「当前事务的 xid」（后者是 `backend_xid`）。把这两个搞混，排查会南辕北辙。

```bash
# 精确膨胀：pgstattuple 扩展（全表扫描，AccessShareLock；大表去只读副本跑）
psql -d app -c "CREATE EXTENSION IF NOT EXISTS pgstattuple;"
psql -d app -c "SELECT * FROM pgstattuple('orders');"

# 9.5+ 采样版，快得多，适合每天巡检
psql -d app -c "SELECT * FROM pgstattuple_approx('orders');"

# 只看关键列，方便入 TSDB
psql -d app -Atc "
SELECT table_len, tuple_count, dead_tuple_count,
       round(dead_tuple_percent, 2) AS dead_pct,
       round(free_percent, 2) AS free_pct
FROM pgstattuple('orders');"
```

```sql
-- xid 年龄（全库最老；按 2^31 的百分比看更直观）
SELECT datname, age(datfrozenxid) AS xid_age,
       round(100.0 * age(datfrozenxid) / 2147483648, 1) AS pct_of_2^31
FROM pg_database
ORDER BY xid_age DESC;

-- 非活跃复制槽：active=false 且有 xmin，就是在钉回收
-- pg_current_wal_lsn()/pg_wal_lsn_diff() 为 10+；9.6 用 pg_current_xlog_location()
SELECT slot_name, plugin, slot_type, active, xmin, catalog_xmin,
       pg_size_pretty(
         pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)
       ) AS retained_wal
FROM pg_replication_slots
WHERE NOT active OR xmin IS NOT NULL;

-- 悬挂的预提交事务（max_prepared_transactions > 0 才有）
SELECT gid, prepared, owner, database, age(transaction) AS xid_age
FROM pg_prepared_xacts
ORDER BY prepared;
```

### 拐点：先看哪条线

- **`age(datfrozenxid) > 15 亿`**：这是全库最危险的一条线。按 5,000 写 TPS，离 2^31 只剩约 2 天；按 20,000 TPS 只剩约 12 小时。先查 `pg_replication_slots`、`pg_prepared_xacts`、最老事务，再决定是否 `VACUUM (FREEZE)`。
- **`n_dead_tup / n_live_tup > 20%` 且 autovacuum 一小时没跑**：不是「等它跑」，是「有人钉住了」。先查 `backend_xmin`，不要先调 `cost_delay`。
- **`pg_stat_progress_vacuum` 长时间停在 `scanning heap`**：看 `heap_blks_scanned` 的斜率。斜率接近 0 是锁/IO 等待；斜率正常但 `index_vacuum_count` 一直涨，是 `maintenance_work_mem` 太小。
- **`Heap Fetches` 接近行数**：VM 没置位。先看有没有长快照，再看 VACUUM 有没有跑；不要先改查询。
- **`pg_replication_slots.active=false` 且 `xmin` 非空**：15 分钟内必须确认下游是否还活着；超过 1 小时直接按事故处理，因为它在制造全库膨胀。

`pg_stat_database.xact_commit` 可以拿来做写 TPS 的**上界**（它包含只读事务，会高估）；更直接的 xid 消耗速率是 `age(datfrozenxid)` 的时间导数，但 freeze 会让它跳变，所以两个一起看：前者看业务压力，后者看剩余额度。

---

## 11.12 一页速记

| 记什么 | 结论 / 数字 |
| --- | --- |
| 表形态 | 无序堆 + 独立索引；主键只是普通唯一 B-tree，不是聚簇索引 |
| `ctid` | 6 B `(block, offset)`；索引叶子存它；会被更新/`VACUUM FULL` 改，不能当业务 ID |
| 行头 | `HeapTupleHeaderData` 固定 23 B（对齐后 24 B）：`xmin/xmax` 各 4 B、command id 4 B、`ctid` 6 B、`infomask2/infomask` 各 2 B、`t_hoff` 1 B |
| 主键点查 | PG 3 次索引页 + 1 次堆页 = 4 次；InnoDB 3 次（整行在聚簇叶子） |
| 二级索引点查 | PG 3 + 1 = 4 次；InnoDB 3 + 3 = 6 次；PG 索引更窄，但必回堆 |
| 更新 | PG 写新版本 + 新索引条目；InnoDB 原地改 + undo；HOT 是 PG 的例外 |
| HOT 条件 | 被更新列不在任何索引里 + 同页有空间；`fillfactor` 80–90 换 HOT 概率 |
| 普通 VACUUM | 标空间可重用、清索引、冻结、设 VM；**不缩文件**；`SHARE UPDATE EXCLUSIVE` |
| `VACUUM FULL` | 重写堆 + 重建索引；`ACCESS EXCLUSIVE` 全程；约 2 倍磁盘；才还空间给 OS |
| autovacuum | 触发 `50 + 0.2 × 行数`；默认节流约 40–80 MB/s；3 个 worker |
| VM | 每堆页 2 bit（all-visible / all-frozen，9.6+）；只有 VACUUM 等会置位；index-only 靠它 |
| xid | 32 位，2^32 ≈ 42.9 亿；安全半区 2^31 ≈ 21.5 亿；freeze 是硬时钟 |
| freeze 窗口 | `autovacuum_freeze_max_age=2 亿`；5,000 TPS 下只有 11.1 小时 |
| 膨胀算账 | 1 亿行 × 300 B × 日更 10% = 3.3 GB 死数据/天；30 天不回收 ≈ 4.3 倍 |
| 三本账 | 膨胀（空间）、`VACUUM FULL`（重写 + 锁）、xid 回卷（时间）；三本互相放大 |

**如果只记五句：**

1. **PG 的表是堆，不是主键树**；`ctid` 才是物理位置，主键只保证唯一，不保证顺序。
2. **更新写新行、旧行留堆**；VACUUM 只把空间标成可重用，不缩文件，回收边界是最老快照。
3. **长事务、`idle in transaction`、prepared xact、逻辑复制槽**是四颗钉子，钉子不拔，VACUUM 扫再多也白扫。
4. **index-only scan 不是免费的覆盖索引**，它要 VM 的 all-visible 位；VM 靠 VACUUM，所以 vacuum 跟不上，索引扫描会退化成随机回表。
5. **xid 是 32 位硬时钟**；容量规划要算「freeze 窗口内能不能扫完所有表」，不是「磁盘还剩多少」。

> **一句话对照**：InnoDB 怕 undo 涨，PG 怕表涨；InnoDB 的 purge 追 history list，PG 的 VACUUM 追最老快照；InnoDB 的时间是 48 位 trx_id，PG 的时间是 42.9 亿的 xid 钟。选 PG 就是选了一条「定期扫全库」的运维负债，选 InnoDB 就是选了一条「undo 不能有长事务」的负债——没有哪边是免费的。

---

## 11.13 自检题（5 题，答案折叠）

**Q1.** 同一条 `SELECT * FROM orders WHERE id = 42`，为什么 InnoDB 是 3 次页访问，PG 是 4 次？换到 `WHERE phone = ?`（`phone` 有二级索引、主键是 `id`），为什么变成 InnoDB 6 次、PG 4 次？

<details><summary>答案</summary>

InnoDB 的表就是聚簇索引，主键 B+ 树的叶子页里放着整行；root → branch → leaf 三次就到数据，没有独立的「回表」。

PG 的主键 B+ 树叶子只放 `key + ctid`，拿到 `ctid=(block,offset)` 后还要读一次堆页，取行版本并检查 `xmin/xmax/infomask`，所以是 3 + 1 = 4 次。如果查询只取索引里已有的列，且该堆页的 VM all-visible 位为 1，可以省掉堆页，退化成 3 次 index-only scan。

二级索引：InnoDB 的 `idx_phone` 叶子存 `(phone, id)`，拿到 `id` 后还要再走一次聚簇索引（3 次），合计 3 + 3 = 6 次；PG 的 `idx_phone` 叶子直接存 `ctid`，拿到后只读一次堆页，合计 3 + 1 = 4 次。**PG 的二级索引更窄、回表更短，但主键点查多一次；InnoDB 的二级索引更宽、回表更长，但主键点查更直接。** 这就是「叶子存主键」与「叶子存 ctid」的取舍。
</details>

**Q2.** 一个 `idle in transaction` 会话挂了 30 天，为什么 PG 是表膨胀，而不是像 InnoDB 那样 undo 膨胀？VACUUM 为什么删不掉那些死元组？

<details><summary>答案</summary>

PG 的旧版本不在 undo，而是作为独立元组留在堆页里；`UPDATE` 写新版本，旧版本的 `xmax` 记上更新事务。VACUUM 判断一个死版本能不能删，要看「是否有任何活快照还可能看见它」；这个会话 30 天不提交，`backend_xmin` 就停在 30 天前，之后产生的所有旧版本都在它的可见范围内，VACUUM 一条都不能删。表只能按「每天新增多少版本 × 版本大小」持续涨；InnoDB 则会把旧版本放进 undo，表本身不涨，涨的是 undo 表空间和 history list。

所以 PG 的膨胀位置是**表和索引**，InnoDB 是**undo**；两者都被最老读者钉住，只是一个钉在堆上，一个钉在 undo 上。修复不是调 VACUUM 参数，而是结束会话（`pg_terminate_backend`）或设 `idle_in_transaction_session_timeout`（9.6+），让快照释放。
</details>

**Q3.** 1 亿行、行均 300 B、每天更新 10%、30 天没有回收：dead tuple 多少个？表大约膨胀到几倍？一次 autovacuum 要读多少？为什么 VACUUM 跑完文件还是不缩？

<details><summary>答案</summary>

每天 1,000 万个死元组，30 天 = **3 亿个**。每个版本按 300 B 数据 + 24 B 行头 + 4 B 行指针 + 2 B 对齐 = 330 B，30 天 = 3 亿 × 330 B ≈ **99 GB**；活数据 30 GB，所以表约 (30 + 99) / 30 ≈ **4.3 倍**。

一次 VACUUM 要扫整个堆（30 GB，因为 10% 更新均匀分布，几乎每页都有死元组）+ 4 个索引（每个约 6 GB，合计 24 GB）= **54 GB 读**。如果每 2 天触发一次，单次回收 6.6 GB，读放大 54 / 6.6 ≈ 8.2 倍；按每天 3.3 GB 算则是 16.4 倍。默认 80 MB/s 下单次约 11.5 分钟。

VACUUM 只把死元组占的行指针标成 `LP_UNUSED`，空间回到页内 free space / 表的 free list，供后续 INSERT/UPDATE 复用；页还在文件里，所以文件不缩。要让文件真的变小，只能 `VACUUM FULL`、`pg_repack`、`CLUSTER` 或 `TRUNCATE`/分区 DROP——它们都要重写，`VACUUM FULL` 还要 `ACCESS EXCLUSIVE` 和约 2 倍磁盘。
</details>

**Q4.** 8,000 写 TPS、10 TB 数据库、500 MB/s 顺序盘：`autovacuum_freeze_max_age=2 亿` 的窗口够不够？如果沿用默认 `cost_delay=2 ms` 呢？换成 20 TB 呢？

<details><summary>答案</summary>

先算时钟：2^31 / 8,000 = 268,435 s ≈ **3.11 天**到可见性半区；2 亿 / 8,000 = 25,000 s ≈ **6.94 小时**的 freeze 窗口。

10 TB 按 500 MB/s 全速扫一遍：10 × 1,024 × 1,024 MB / 500 = 20,972 s ≈ **5.83 小时**（按 10^12 B 算是 5.56 小时），小于 6.94 小时，**满带宽刚好够，余量约 1 小时**。但默认 `cost_delay=2 ms` + `cost_limit=200`，页未命中约 80 MB/s，10 TB 要 **34.7 小时**，是窗口的 5 倍——**默认配置下必然追不上**。

20 TB 时，即使 500 MB/s 全速也要 **11.1 小时** > 6.94 小时，**满带宽也不够**。这时只能：提高盘带宽、降低写 TPS、把库拆小/分片，或接受「freeze 永远在赶末班车」。注意这是最坏情况（每张表每个窗口全表扫）；9.6+ 的 freeze map 能跳过全冻结页，实际取决于更新分布，但容量规划要按最坏情况留余量。
</details>

**Q5.** `VACUUM FULL` 为什么需要约 2 倍磁盘？它只拿一次锁，为什么能让所有 `SELECT` 都进不来？

<details><summary>答案</summary>

它新建一个 relfilenode，把活行重写进新堆，同时重建全部索引；旧文件要等新文件提交后才能删。峰值时旧堆 + 新堆 + 旧索引 + 新索引同时存在，所以至少需要「新堆 + 新索引」的额外空间，按表 + 索引计约等于 1 倍总大小；加上 WAL，峰值可能接近 2 倍。磁盘不够时它会在重写到一半时报 `No space left on device`，回滚后旧表还在，白花几小时 IO。

锁是 `ACCESS EXCLUSIVE`，与 `ACCESS SHARE`（普通 `SELECT` 拿的锁）也冲突，所以不只是阻塞写，读也进不来。更糟的是锁队列：一旦 `VACUUM FULL` 的锁请求在等一个长查询，后面新来的 `SELECT` 会排在它后面，形成「它等长查询、所有人等它」的队头阻塞。正确做法是维护窗口 + `lock_timeout` + `pg_repack`（在线重写）或分区 DROP，而不是在晚高峰直接执行。
</details>

---

## 11.14 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| InnoDB 的页、行格式、聚簇索引与页分裂（本篇的正面参照） | [comp/05 InnoDB 的页与行](05-innodb-page-and-row.md) |
| undo 版本链、Read View、purge 与长事务（另一种 MVCC 账单） | [comp/06 MVCC 与隔离级别](06-mvcc-and-isolation-levels.md) |
| 复制、只读副本与延迟（报表/备份该放哪） | [comp/10 主从复制与高可用](10-replication-and-ha.md) |
| B+ 树、LSM、列存的形态选择（什么时候不该用行存） | [architect/03 存储与索引形态](../architect/03-storage-and-index-shape.md) |
| 关系库选型矩阵、MySQL 与 PG 的边界 | [eng/35 存储模型选型](../eng/35-storage-model-selection.md) |
| 全书的解剖卡八问、三本放大账与写作公约 | [0-summary 总纲](0-summary.md) |
| 锁、间隙锁与死锁（`VACUUM FULL` 之外的锁问题） | `→ comp/07` |
| 优化器与执行计划（为什么有时全表扫比索引便宜） | `→ comp/09` |
| TiKV / NewSQL 的 MVCC 与全局时间戳（跨分片快照） | `→ comp/19` |
| ClickHouse / MergeTree（列存为什么不怕单表很大） | `→ comp/20` |
| 搜索系统的段与近实时（另一种「删除只是标记」） | `→ architect/17 §17.3` |
| MySQL 的单表上限与分片时机 | `→ eng/36` |
| 本地事务的坑与大事务拆分（应用层纪律） | `→ eng/7` |

**最后一句话**：PG 和 InnoDB 的差异不是「谁更先进」，是**旧版本放在哪**这一个物理选择，长出了两套完全不同的运维人生。你选 PG，就要把 VACUUM、VM、freeze 当成一等公民写进容量规划；你选 InnoDB，就要把 undo、purge、长事务当成一等公民。**两边都没有「不用管」的选项。**
