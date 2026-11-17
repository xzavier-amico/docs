# 08. redo / undo / binlog 与内部两阶段提交

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · 关系库与事务内核 ｜ **编号**：08 / 53
> **本篇定位**：Part II 的「日志篇」。05 讲一行的物理形态、06 讲旧版本怎么被看到、07 讲锁加在哪，本篇只回答一个问题：**一次 `COMMIT` 到底落了几份日志、各自谁 fsync、这三份日志凭什么能同时成立**。本篇**不重复**版本链与可见性（那是 06 的活），也不重写通用 WAL 原理、LSN、checkpoint、doublewrite 与崩溃恢复三阶段（那是 03 的活）；它只讲 MySQL/InnoDB 专有的那部分：redo 的物理结构、undo 的写与回收路径、binlog 的格式、**内部两阶段提交**、组提交，以及「双 1」这笔 fsync 账单。
> **读法**：赶时间就看三处——§08.3 的分工表（一眼分清三份日志各自管什么）、§08.7 的「崩溃在 5 个时点」判定表（这是面试与故障复盘的唯一支点）、§08.8 的三档介质 TPS 表（用来十分钟内否掉「TPS 上不去是 CPU 问题」这种误判）。要改参数就翻 §08.10 与 §08.11：前者是坑，后者是能提前十分钟报警的数字。

---

## 08.1 引子：三起「提交成功了，主从却分叉 / 重启后少了 3 秒」的故障

日志这一层的事故有个共同点：**没有任何一个组件报错**。应用收到了 `COMMIT` 的 OK，代理层 5xx 为零，数据库错误日志干净——错的是「账」，而账要在对账脚本跑起来的那天才被发现。

### 事故一：一条 200 万行的批量更新，把 redo 打满，写请求排队 40 秒（2025-12-09）

某交易库，订单表 3.2 亿行、6 个二级索引，MySQL 8.0.36，云盘顺序写 400 MB/s。这套库是从 5.7 迁过来的，`innodb_redo_log_capacity` 一直没动过——**它继承了老参数换算出来的 96 MB**（`innodb_log_files_in_group × innodb_log_file_size`）。`innodb_io_capacity` 也是默认的 200 页/s，从来没人按 SSD 上调过。

19:12，运营要修一批活动价，DBA 执行了 `UPDATE order_item SET price = ... WHERE batch_no = '20251209A'`，影响 200 万行，单事务，耗时约 60 秒完成。

| 时间 | 事件 | redo 生成速率 | checkpoint age / 容量 | `Innodb_log_waits` | 下单 P99 |
| --- | --- | --- | --- | --- | --- |
| 19:00 | 稳态，交易 4,000 TPS | 4.8 MB/s | 12 s/ 96 MB（约 60 MB） | 0 | 8 ms |
| 19:12:03 | 批量 `UPDATE` 开始 | 13 MB/s | 迅速上涨 | 0 | 9 ms |
| 19:12:14 | redo 写满，第一次分配空间失败 | 13 MB/s | 96 MB / 96 MB（100%） | 1,204 | 210 ms |
| 19:12:20 | 所有写请求阻塞在 redo 空间分配 | ≈ 0（被限流） | 贴着 100% | 18 万 | 2,900 ms |
| 19:12:52 | checkpoint 追完，空间释放，队列消化 | 6 MB/s | 回落到 40% | 26 万 | 1,100 ms |
| 19:13:30 | 代办批次提交完成 | 4.9 MB/s | 稳定 | 26 万（不再增） | 11 ms |
| 19:20 | 业务侧统计 | — | — | — | 超时 3.1 万次，GMV 损失约 42 万元 |

**因果链只有一条，且和「批量更新慢」无关**：redo 是循环写的，能写多少取决于「checkpoint 追到哪」（§08.4）。checkpoint 推进必须先把脏页刷进数据文件；脏页刷不快，checkpoint 就推不动；checkpoint 推不动，redo 里那块空间就永远不释放；空间不释放，**全库所有写请求一起排队**——不是只有那条批量更新在等。

算一遍就明白为什么是 40 秒：

```
批量更新 200 万行，每行 redo ≈ 行镜像 320 B + 6 个二级索引项 × 28 B ≈ 490 B，取 400 B
redo 总量 = 2,000,000 × 400 B = 800 MB          ← 是 redo 容量的 8 倍
要让这 800 MB 的 redo 变旧，必须把对应脏页刷出去：脏页 ≈ 800 MB（数据）+ 索引页，取 1.2 GB 有效写盘
盘是 400 MB/s，但引擎按 innodb_io_capacity=200 页/s ≈ 3.2 MB/s 的基线刷；
撞上 innodb_max_dirty_pages_pct（默认 90%）后进入急刷，实测有效速率 ≈ 20~30 MB/s
排队时长 ≈ 800 MB ÷ 20 MB/s ≈ 40 s          ← 与观测的 40 秒吻合
⇒ 若 innodb_redo_log_capacity 设到 2 GB 且 io_capacity=4000：800 MB 在 8~10 s 内刷完，redo 不会满，写请求零排队
```

**量级结论**（假设：行宽 320 B、6 个二级索引、redo 默认 96 MB、`innodb_io_capacity` 默认 200）。换一批索引数或行宽，40 秒会在 20~90 秒之间浮动，但「redo 撑不住一个事务的体量 ⇒ 全库写阻塞」这个结论不变。

### 事故二：`sync_binlog=0` 掉电，PITR 少了 3 秒，对账差 1.7 万笔（2026-01-22）

同一个库集群的另一个实例，参数模板里写着 `sync_binlog=0`——那是 2016 年博客里的「性能优化」，被复制了十年。`innodb_flush_log_at_trx_commit` 倒是 1，看上去「已经开了双 1 的一半」。

16:40:12，机房一路 PDU 掉电，主机直接断，没有任何优雅关闭。

| 时间 | 事件 | 主库 binlog | 从库/下游 |
| --- | --- | --- | --- |
| 16:40:09 ~ 16:40:12 | 支付流水 1.7 万笔提交成功，应用已收到 OK | 写进 page cache（未 fsync） | dump 线程从 page cache 读到并**已经发给了从库与 Kafka** |
| 16:40:12 | 掉电 | 尾部 2.8 MB 消失 | 从库与 Kafka 侧已落盘 |
| 16:41 | 主机重启、InnoDB 恢复 | redo 完整（`flush_log_at_trx_commit=1`），所以**数据在** | — |
| 16:52 | 按「最后一个完整 event」做 PITR | 恢复点被截断在 16:40:09 | 主库比下游**少** 3 秒 |
| 次日 09:00 | 对账脚本发现 1.7 万笔流水对不上 | — | 人工反向补偿 + 对外公告 |

关键不是「丢了 3 秒」，是**丢的方向错了**：redo 是 fsync 过的，所以数据页里有这 1.7 万笔；binlog 没 fsync，所以日志里没有。但对下游来说，日志才是真相——于是主库有数据、主从链路与下游少了这 3 秒，**任何按 binlog 重建的动作都会把它们抹掉**。这和事故四步走（写 → 复制 → 掉电 → PITR）里最贵的一步是第三步：**drop 掉的不只是日志，是「谁是权威」这件事**。

代价：1.7 万笔人工对账、3 个工作日、直接损失 8.6 万元。修复：`sync_binlog=1` 进基线巡检（§08.11），并把「参数模板版本号」写进变更单。

### 事故三：双 1 配置下 TPS 卡在 1,200，团队扩了一倍 CPU（2025-11-05）

新业务上线，跨 AZ 云盘（单次 `fsync` 实测 8 ms），`innodb_flush_log_at_trx_commit=1` + `sync_binlog=1`，连接池上限 20。压测 TPS 1,180~1,240，怎么调都不动。

| 时间 | 动作 | TPS | CPU | 设备 util | `await` |
| --- | --- | --- | --- | --- | --- |
| 11-05 14:00 | 基线压测 | 1,200 | 6% | 35% | 16 ms |
| 11-05 16:30 | CPU 8C → 32C | 1,205 | 6% | 35% | 16 ms |
| 11-06 10:00 | 换更大规格磁盘（同 AZ） | 1,208 | 6% | 30% | 4 ms |
| 11-06 15:00 | 加 `binlog_group_commit_sync_delay=200` | **9,400** | 21% | 62% | 15 ms |
| 11-06 15:30 | 连接数 20 → 64，delay 保持 200 µs | **13,800** | 44% | 78% | 15 ms |

一笔账就能提前一天定位：双 1 下一个事务要**串行**等两次 fsync（redo 一次、binlog 一次），单事务提交耗时 `8 + 8 = 16 ms`，单连接 TPS `= 1 / 0.016 ≈ 62.5`；20 个连接并发（每个都在等 fsync，不抢 CPU）⇒ `20 × 62.5 = 1,250`，与观测的 1,200 是同一个数。而 CPU 6%、设备 util 35%、`await` 16 ms 这三个数字也全部符合「在等 fsync」，与算力无关。

**量级结论**（假设：fsync 延迟 = 介质写延迟的 2 倍以内、连接数 20、无组提交）。**这是本篇最贵的一条常识：双 1 下的 TPS 上限是 fsync 延迟的函数，不是 CPU 的函数。**

三次事故分别落在本篇三节：**redo 容量与循环写（§08.4）**、**binlog 的 fsync 语义（§08.6、§08.7）**、**组提交与 fsync 定价（§08.8）**。

---

## 08.2 一句话定位与唯一真问题

**定位**：MySQL 的持久化不是一个日志系统，是**两个日志系统被缝合起来**——存储引擎的物理日志（redo）、server 层的逻辑日志（binlog），再加一份只为「撤销与旧版本」存在的旧值集合（undo）。三方各有各的 fsync 开关，各有各的失效方式。

### 唯一真问题

> **一次提交要落三份日志，而「redo 与 binlog 一致」这件事在物理上无法一次写完——只能靠内部两阶段提交 + 组提交，用吞吐去换那个一致性。**

把这句话拆开看，每一步都是硬的：

- redo 在**存储引擎**里，按页号 + 页内偏移记「哪个字节被改成什么」，它的读者是崩溃恢复；
- binlog 在 **server 层**，按 SQL 或行镜像记「业务上发生了什么」，它的读者是从库、PITR、CDC；
- 两个日志文件**不在同一条 fsync 里**，中间必然有一个「一个已落盘、一个还没落」的时刻；
- 那个时刻一旦掉电，主库与从库就会分叉——而 **fsync 是唯一能把窗口关小的手段，一次 fsync 就是一次吞吐定价**。

由此推出全部设计，没有一条是任意的：

| 从矛盾推出的约束 | 推出的设计（本篇哪节） |
| --- | --- |
| 两个日志无法一次落盘 | **内部 2PC**：redo 先 prepare，binlog 后 fsync，用 binlog 的完整性去决定 redo 是否前滚（§08.7） |
| 2PC 让单事务付出 2 次 fsync | **组提交**：N 个事务共用 1 次 redo fsync + 1 次 binlog fsync（§08.8） |
| 组提交把「持久性」变成「批量持久性」 | `sync_binlog=1` 保住一致性、`=N`/`=0` 用窗口换吞吐，**这个窗口必须由业务定价**（§08.8 矩阵） |
| redo 必须能被覆盖才能无限运行 | **循环写 + checkpoint**，于是 redo 容量变成「能不能扛住一个事务」的硬约束（§08.4，事故一） |
| undo 的持久性不在 undo 自己身上 | undo 页的修改也进 redo ⇒ **没人 fsync undo**，它靠 redo 恢复（§08.5） |
| 恢复必须能判定「这个 prepared 事务该不该提交」 | binlog 尾部完整性成为**恢复输入**，`sync_binlog=0` 直接把这一输入弄坏（事故二） |

### 八问对照（本篇答哪几问）

| 八问 | 落在哪节 | 一句话答案 |
| --- | --- | --- |
| ① 承诺什么 | §08.2、§08.9 | 承诺「已返回 OK 的事务在单机崩溃后不丢」；不承诺「跨实例原子」「fsync 不撒谎的盘」 |
| ② 数据长什么样 | §08.4、§08.5、§08.6 | 512 B 的 redo block 环、undo 表空间里的反向记录、binlog 的 event 流 |
| ③ 一次提交走哪条路 | §08.3、§08.7 | 引擎写 redo → fsync → server 写 binlog → fsync → redo 标提交；2 次 fsync |
| ④ 并发与一致 | §08.7、§08.8 | 内部 2PC 定「一致」，组提交定「并发上限」；失效窗口在两次 fsync 之间 |
| ⑤ 扩展维度 | §08.9 | **写路径不可横向扩**：只能垂直切 + 组提交摊薄 + 分片；副本只扩读 |
| ⑥ 会在哪崩（系统级） | §08.9、§08.10 | redo 打满 ⇒ 全库写阻塞；binlog 与 redo 分叉 ⇒ 主从不一致（语义漂移） |
| ⑦ 看哪几个指标 | §08.11 | checkpoint age 占比、redo 生成速率、`Innodb_log_waits`、组提交触发原因 |
| ⑧ 换掉它的信号 | §08.9 | 单实例写入被 fsync 定价到不可接受、redo 常年 > 70% ⇒ 分片或换写入模型 |

---

## 08.3 三种日志的分工（先看这张表）

先给结论：**redo 是「怎么把页修回来」，undo 是「怎么把逻辑改回去」，binlog 是「发生过什么」**。三者服务三种完全不同的读者，所以三者的物理形态、生命周期、fsync 归属都不一样。

| 维度 | redo log | undo log | binlog |
| --- | --- | --- | --- |
| **谁产生** | InnoDB（存储引擎层） | InnoDB（存储引擎层） | MySQL server 层（与引擎无关） |
| **写什么** | **物理逻辑**日志：页号 + 页内偏移 + 变更后的字节（记录类型是源码里的 `MLOG_*` 一族，跨页变更会被拆成多条） | **逻辑反向**记录：主键 + 被改列的旧值 + 指向更早版本的 `roll_ptr`（`TRX_UNDO_INSERT_REC` / `TRX_UNDO_UPD_EXIST_REC` / `TRX_UNDO_UPD_DEL_REC` 三类） | **逻辑**日志：语句（STATEMENT）或行镜像的前像/后像（ROW），外加 GTID 与事务边界 event |
| **它的读者** | 崩溃恢复（重放，把页修到一致） | 事务回滚 + MVCC 快照读（旧版本链，→ comp/06） | 从库（复制）、PITR、CDC（→ comp/10、comp/33） |
| **存哪** | `#innodb_redo/` 目录下的 redo 文件组，**循环写**（写满就覆盖最旧的） | undo 表空间（`.ibu`，默认 2 个，最多 127 个），只追加、按事务回收 | `log_bin` 顺序文件 + index 文件，**不循环**，按保留期删除 |
| **容量由谁定** | `innodb_redo_log_capacity`（8.0.30+，动态可改）；旧版本是 `innodb_log_files_in_group × innodb_log_file_size` | `innodb_max_undo_log_size` + 表空间自动扩展（16 MB 起，按需翻倍，上限 256 MB/次） | 磁盘 + `binlog_expire_logs_seconds`（8.0 起，`expire_logs_days` 已弃用） |
| **谁 fsync、哪个开关** | 提交时 fsync，由 `innodb_flush_log_at_trx_commit` 控制（1 = 每次提交 fsync） | **没有任何人 fsync undo 页**：undo 页的修改以 redo 记录形式落盘，靠 redo 恢复 | 提交时 fsync，由 `sync_binlog` 控制（1 = 每次提交 fsync） |
| **丢了会怎样** | 最近一段已提交事务在重启后消失（数据页可能比日志更旧，无法重放补齐） | 单看 undo 丢不是独立故障——它跟着 redo 一起丢；replay 后 undo 页被修回，才能回滚未提交事务 | 从库缺少一段（复制断流或数据缺口）、PITR 恢复点回退、CDC 位点对不上 |
| **能重建出什么** | 只能重建**页**，重建不出 SQL，也重建不出历史版本 | 重建出「改之前的值」，但只对**活着的事务与活着的快照**有效，purge 之后永久消失 | 重建出**完整的历史变更流**（唯一能做 PITR 与 CDC 的东西） |

> 顺带划线：doublewrite（防页半写）与崩溃恢复三阶段属于通用 WAL 地盘，→ comp/03；undo 版本链与 Read View 的判定规则 → comp/06。本篇只碰「三份日志各自写什么、谁 fsync、怎么缝」。

**图 1 · 一次双 1 提交的写路径与两次 fsync 的归属**

```
 应用 COMMIT
   │
   ├─(1) 引擎层 DML ──► 缓冲池里的数据页（脏页，此刻不落盘）
   │        └────────► undo 记录 ──► 缓冲池里的 undo 页（也不单独落盘）
   │                                     │
   │              两者的字节变更都编码成 redo ──┐
   │                                           ▼
   ├─(2) redo log buffer（内存；innodb_log_buffer_size 只影响 write() 的批量化，
   │        │                不影响持久性——持久性由 fsync 决定）
   │        ▼  write()
   │     redo 文件组（循环写，容量 = innodb_redo_log_capacity）
   │        │  fsync()  ← ① 第 1 次 fsync：innodb_flush_log_at_trx_commit=1 时在这里付
   │        ▼
   │     【持久】redo 里出现该事务的 prepare 记录（含 XID）
   │
   ├─(3) server 层：binlog cache（内存；超 binlog_cache_size 时落临时文件）
   │        ▼  write()
   │     binlog 文件（顺序文件，不循环，所有人共用一个流）
   │        │  fsync()  ← ② 第 2 次 fsync：sync_binlog=1 时在这里付
   │        ▼
   │     【持久】binlog 里出现以 Xid event 结尾的完整事务
   │
   └─(4) 引擎层：redo 写 commit 标记（可只 write 不 fsync）→ 返回 OK
            锁释放、事务可见；脏页刷盘与 undo 回收都交给后台

 双 1 一次提交 = 2 次串行 fsync。这就是 §08.8 全部算账的起点。
```

---

## 08.4 redo：循环写的物理结构与「写满即卡」

### 512 字节的 block 与「物理逻辑」这四个字

redo 文件被切成固定大小的 **block（512 B，与磁盘扇区同尺寸，保证单块写入不会半写）**。一个 block 由三部分组成：

| 部分 | 大小 | 装什么 |
| --- | --- | --- |
| block header | **12 B** | 块内数据长度、第一条记录的偏移、本块在 LSN 序列里的起始位置 |
| 数据区 | 512 − 12 − 4 = **496 B**（跨块记录会拆到下一个 block） | redo record 流：页号、页内偏移、变更多少字节、变更后的字节 |
| block trailer | **4 B** | 老版本放的是 checkpoint 序号（`LOG_BLOCK_CHECKPOINT_NO`），后续版本改成了块校验（`LOG_BLOCK_CHECKSUM`）——**别在评审里背死这个字段的名字，它的语义随版本换过** |

「物理逻辑」是理解 redo 的关键：它不是「一整页的镜像」（那是 doublewrite 的活），也不是「一句 SQL」（那是 binlog 的活）。它记的是「第 P 号页、偏移 O 之后、要变成这串字节」。这带来两个后果：

- **重放是幂等的**：把同样的字节写回同样的页位置，结果一样，所以崩溃恢复可以从任意一个 checkpoint 重放到末尾而不用管中间是否重放过；
- **redo 只能重建页，不能重建语义**：你没法从 redo 里知道「这笔操作属于哪个事务的业务含义」，所以它替不了 binlog。

### 8.0.30+ 的容器：容量管、32 个文件、环

8.0.30 之后，你不再需要关心「文件组里有几个文件」，只需要关心**一个容量数字**：

- `innodb_redo_log_capacity` 是唯一真源（8.0.30 起动态可改，`SET GLOBAL` 立即生效但收敛要时间）；
- redo 文件落在 `#innodb_redo/` 目录（或 `innodb_log_group_home_dir` 指定处），命名 `#ib_redoN`；
- InnoDB 试图维持**总共 32 个 redo 文件**，每个等于 `1/32 × innodb_redo_log_capacity`；其中一部分是**普通文件**（正在用）、一部分是**备用文件**（带 `_tmp` 后缀，等着被用）；
- 每个普通文件覆盖一段连续的 LSN，`performance_schema.innodb_redo_log_files` 里能直接看到 `START_LSN / END_LSN / IS_FULL`；
- checkpoint 的 LSN 记在**包含该 LSN 的那个文件的 header 里**，恢复时扫全部文件、从最新的 checkpoint LSN 开始重放。

不显式设 `innodb_redo_log_capacity` 时，才回落到 `innodb_log_files_in_group × innodb_log_file_size` 的乘积——**事故一那套 96 MB 就是从这里"继承"来的，升级大版本时它不会自动变大**。

### 循环写：两个指针决定「还能写多少」

**图 2 · redo 环与两个指针（LSN 有字节含义）**

```
 把 #ib_redo580..611 这 32 个文件想成一条首尾相接的环：

        write pos（= Innodb_redo_log_current_lsn，写入位置）
              │
   ┌──────────▼──────────────────────────────────────────────┐
   │ 已 checkpoint 段  │  已经写了但还没刷脏（必须留着的）  │ 空闲 │
   │ 可以覆盖/重用      │  ◄──── checkpoint age ────►        │      │
   └───────────────────┴────────────────────────────────────┴──────┘
              ▲
        checkpoint pos（= Innodb_redo_log_checkpoint_lsn）

 checkpoint age = current_lsn − checkpoint_lsn
 age → capacity  时：没有空闲 block ⇒ 新事务**分配不到 redo 空间** ⇒ 写请求阻塞

 所以：redo 容量买的不是"日志能存多大"，而是
       "系统能容忍脏页积压多久" —— 它是「刷脏的缓冲池」的一部分。
```

因果链必须背下来，它是事故一的全部解释，五步一环扣一环：

1. 写请求要 redo 空间 → 剩余空间不足 → 触发/等待 checkpoint 推进；
2. checkpoint 要推进 → 必须先刷脏页（数据页 + 索引页 + doublewrite）；
3. 刷脏慢（盘慢 / `innodb_io_capacity` 太小 / 脏页总量 > redo 容量 / 二级索引太多）；
4. checkpoint 推不动 → redo 空间不释放；
5. **所有写请求一起等**（`Innodb_log_waits` 增长）——不是只有那条大事务在等。

注意这条链的残忍之处：**它不是「这条大事务慢」，是「全库写都慢」**。一条 `UPDATE` 影响的行数，直接决定它不是自己的问题，而是所有在线请求的问题。

### 容量算式：从业务指标倒推 `innodb_redo_log_capacity`

```
高峰 redo 生成速率 = 写 TPS × 每事务 redo 字节
每事务 redo 字节   ≈ 变更行数 × (行镜像 + 各二级索引项) × 页分裂/整页修正系数
redo 容量          ≥ 高峰生成速率 × 可容忍的刷脏窗口（秒）× 安全系数 2

代入一个中台交易库：
  在线：4,000 TPS × 3 行/事务 × 400 B ≈ 4.8 MB/s
  批量：一条 2 万行/秒的订正任务          ≈ 8 MB/s
  峰值合计                              ≈ 13 MB/s
  两次 checkpoint 之间能容忍 60 s 的脏页积压
  ⇒ 容量 ≥ 13 MB/s × 60 s × 2 ≈ 1.6 GB
  ⇒ 默认 100 MB 只能撑 7.7 秒 —— 这就是为什么默认值在今天几乎总是错的

反过来读这张表（同一台机器）：
  redo 100 MB  → 撑 7.7 s 的刷脏停顿
  redo 1 GB    → 撑 77 s
  redo 4 GB    → 撑 5 分钟（这时恢复时间开始变长：重放 4 GB 是分钟级）
```

| 你要回答的问题 | 该看的数 | 判据 |
| --- | --- | --- |
| redo 够不够 | checkpoint age / capacity | 稳态 < 50%；峰值 > 75% 就要动手；长期贴 100% 一定有等待 |
| 刷脏跟不跟得上 | checkpoint age 的斜率 vs redo 生成速率 | 斜率持续为正 ⇒ 刷脏能力不足，别先去调 redo 大小 |
| 恢复要多久 | 容量 ÷ 重放速率（重放 ≈ 顺序读 + 随机写页，量级 50~200 MB/s） | 4 GB redo ⇒ 20~80 s 恢复；这是「大 redo」的另一笔账 |
| 大事务能不能扛 | 单事务 redo 字节 vs 容量 | 单事务 redo > 容量 ⇒ **必满**，只能分批（§08.10 坑 1） |

**量级结论**（假设：行宽 320 B、6 个二级索引、每事务 3 行、稳态 4,000 TPS）。其中「撑多少秒」= 容量 ÷ 生成速率，是纯算术；重放速率那一行是区间估计，别当承诺值。

```sql
-- 当前 redo 配置：8.0.30+ 只认容量，老参数只是换算来源
SELECT @@innodb_redo_log_capacity, @@innodb_log_buffer_size,
       @@innodb_io_capacity, @@innodb_io_capacity_max;

-- 两个指针与占用（8.0.30+ 的全局状态，单位是 LSN）
SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.global_status
WHERE VARIABLE_NAME IN ('Innodb_redo_log_current_lsn',
                        'Innodb_redo_log_checkpoint_lsn',
                        'Innodb_redo_log_capacity_resized',
                        'Innodb_os_log_written',
                        'Innodb_log_waits');

-- 每个 redo 文件覆盖的 LSN 区间与是否写满（8.0.30+ 的性能模式表）
SELECT FILE_ID, START_LSN, END_LSN, SIZE_IN_BYTES, IS_FULL
FROM performance_schema.innodb_redo_log_files ORDER BY FILE_ID;

-- 8.0.30 之前只能看文本：SHOW ENGINE INNODB STATUS 的 LOG 段
--   Log sequence number − Last checkpoint at = checkpoint age
--   Log flushed up to 落后 Log sequence number 越多，说明连 write() 都跟不上
```

---

## 08.5 undo：写与回收，以及回滚的代价

### undo 写什么、写在哪

undo 记录只有三大类（源码常量名，版本稳定）：`TRX_UNDO_INSERT_REC`（插入的反向操作 = 删掉）、`TRX_UNDO_UPD_EXIST_REC`（更新已有行的前像）、`TRX_UNDO_UPD_DEL_REC`（对已删除标记行的更新）。每条记录装：**主键值 + 被改列的旧值 + 指向上一个版本的 `roll_ptr`**。行上的 6 B `trx_id` / 7 B `roll_ptr` 与版本链的判定规则是 06 的地盘，这里只要记住一句：**undo 是「行之外的旧值仓库」，而不是行的一部分**。

存储形态上，undo 住在 **undo 表空间**（`.ibu` 文件）里：

- 初始化时创建**两个默认 undo 表空间**（`undo_001`、`undo_002`，数据字典里叫 `innodb_undo_001/002`）；「至少两个」是硬要求，因为自动截断要留一个在线、另一个下线截断；
- 一个实例最多 127 个 undo 表空间；
- 初始 16 MB，按需扩展，扩展尺寸在 16 MB~256 MB 之间自适应（上一次扩展距今 < 0.1 s 就翻倍）；
- **8.0 起 `innodb_undo_tablespaces` 已弃用**（它只能在启动时定数量，且不允许有回滚段在用）。现在的做法是运行时 `CREATE UNDO TABLESPACE x ADD DATAFILE 'x.ibu'` / `ALTER UNDO TABLESPACE x SET INACTIVE|ACTIVE` / `DROP UNDO TABLESPACE x`。纪律 7 的原话：**写指标和参数时必须带版本，因为这里换过 API**。

### 谁来 fsync undo？答案是「没有人」

这是本篇最该被记住的一条缝：

> **undo 页没有独立的持久化路径。** undo 页住在缓冲池里，它是被当作「普通页」改的；改它的字节流进 **redo**。崩溃恢复时，InnoDB 先重放 redo 把 undo 页修好，再沿 undo 回滚那些「prepared / 未提交」的事务。
> 所以：**undo 的持久性 100% 是 redo 的副产物**。undo 表空间坏了等于引擎不能启动（连回滚都做不了），而 undo 丢了不需要单独处理——因为 redo 里都有。

这条推论很实用：它解释了为什么 `innodb_flush_log_at_trx_commit=0/2` 时，连 undo 的修改都可能一起丢；也解释了为什么 undo 表空间被误删后报的是「启动失败」而不是「数据不一致」。

### 回滚段与并发上限

| 量 | 值（含出处性质） | 说明 |
| --- | --- | --- |
| 每个 undo 表空间的回滚段数 | `innodb_rollback_segments`，默认 128，上限也是 128 | 8.0 起它是**每个 undo 表空间**的数，不是全实例的 |
| 每个回滚段的 slot 数 | 引擎常量，源码里是 `TRX_RSEG_N_SLOTS`（1024） | 一个 slot 记录「某事务用了本段的哪段 undo」 |
| 单表空间并发写事务上限 | 128 × 1024 = **131,072** | 撞上就是 `ER_TOO_MANY_CONCURRENT_TRXS`（→ comp/06 坑 7） |
| 一个事务跨回滚段？ | 不跨：一个事务绑定一个 rseg 的一个 slot | 这也是为什么「连接数暴涨」不会立刻报错，但「连接数 × 未提交事务」会 |

### 回收路径：不是「提交了就删」

commit 之后 undo **不能立刻回收**，因为可能还有更老的 Read View 需要它（→ comp/06 §06.7 讲透了长事务怎么钉住 purge）。回收只有一条路，五步，卡在第一步就全废：

1. 事务 commit，undo 记录带上「已提交」的边界；
2. purge 线程按「最老 Read View」推进，逐条判断该 undo 记录还有没有人要；
3. 没人要了 → 释放该记录占的 slot / 页 → 才能被后续事务复用；
4. 某 undo 表空间超过 `innodb_max_undo_log_size`（默认 1 GB）且 purge 已越过截断点 → 标记 offline → purge 清空所有 rseg → truncate 回 16 MB → 重新 online；
5. **若期间有长事务钉住边界，第 2 步永远不成立，文件只增不减**。

注意最后一步：`innodb_undo_log_truncate=ON`（8.0 默认）**不等于**「会自动缩」。它的前提是「purge 已越过截断点」，而长事务会让这个前提永远不成立。另外 `innodb_purge_rseg_truncate_frequency`（默认 128）决定 purge 每跑 128 轮才找一次可截断的表空间——调小可以更快响应，代价是 purge 主路径多几次检查。

### `trx_rollback` 的真实代价：回滚一个新写的 100 万行事务

回滚不是「标记一下」，是**沿着 undo 把反向操作再执行一遍**——于是它比正向更贵，因为它是随机 IO 的反向遍历，还要维护 12 个二级索引。

| 量 | 推导 | 结果 |
| --- | --- | --- |
| 待回滚行数 | 题设：单事务 100 万行 `UPDATE` | 1,000,000 |
| 每行 undo 占盘 | 主键 + 旧值 + 头 ≈ 300 B（行宽 340 B 时实测 250~400 B） | 300 B |
| undo 增量 | 1,000,000 × 300 B | ≈ 300 MB |
| 回滚吞吐（6 个二级索引） | 与 purge 同量级，1 万~5 万行/s，索引越多越慢 | 取 20,000 行/s |
| 回滚耗时 | 1,000,000 ÷ 20,000 | **≈ 50 s** |
| 回滚吞吐（12 个二级索引） | 每条回滚要动 12 个索引项 | 掉到 5,000 行/s |
| 回滚耗时（同上） | 1,000,000 ÷ 5,000 | **≈ 200 s（3 分 20 秒）** |
| 回滚自己也要写 redo | 1,000,000 × （行 320 B + 12 × 28 B） | ≈ 660 MB redo |
| 期间锁与 undo 空间 | 回滚期间事务仍持锁、undo 不释放 | 应用侧看起来「已经 KILL 了却还卡着」 |

**量级结论**（假设：行宽 340 B、6~12 个二级索引、无并发干扰）。结论方向是硬的：**回滚耗时 ≈ 正向耗时 × 0.5~2，绝不是一个瞬间**。所以「出错了就 rollback」在百万行规模上是一个几百秒的操作——这正是「大事务必须分批」在日志层面（不只是锁层面）的理由。

---

## 08.6 binlog：逻辑日志的三副面孔

binlog 是**唯一一份「能重放出历史变更流」的日志**，因此它同时被三种完全不同的东西消费：从库、PITR 工具、CDC。

### 格式：STATEMENT / ROW / MIXED

| 格式 | 记什么 | 体积（同一批 100 行 `UPDATE` 的粗糙对比） | 确定性 | 什么时候必炸 |
| --- | --- | --- | --- | --- |
| **STATEMENT** | 原始 SQL + 少量上下文（`use` 库、`SET TIMESTAMP`） | 1 条 event，几百字节 | **差**：`NOW()`、`RAND()`、`UUID()`、无 `ORDER BY` 的 `LIMIT`、`@@hostname` 全都不确定 | 从库执行环境与主库不同（版本、时区、字符集、索引不同导致扫描顺序不同）⇒ **主从数据分叉，且无报错** |
| **ROW**（8.0 默认） | 每行的前像/后像（`Table_map` + `Write_rows` / `Update_rows` / `Delete_rows`） | 100 条 row event，行宽 300 B 时约 30~60 KB | **强**：只记结果，不依赖执行环境 | 体积与「变更行数」成正比，不看「影响行数」；一条批量更新能灌爆网络与从库延迟 |
| **MIXED** | 默认 STATEMENT，遇到非确定性函数自动降级 ROW | 介于两者 | 中等（判定规则本身是启发式） | 判定不准的时刻仍然分叉，且「什么时候降级」不可预期 |

线上基线只有一句话：**`binlog_format=ROW`**。8.0 会直接拒绝 RC + STATEMENT 写 InnoDB（→ comp/06 坑 4），但 RR + STATEMENT 仍然允许——它照样能分叉。

### row image：三档决定「CDC 能不能拿到旧值」

| `binlog_row_image` | 后像 | 前像 | 体积 | 适用 |
| --- | --- | --- | --- | --- |
| `full`（默认） | 所有列 | 所有列 | 最大 | 复制、CDC、闪回/审计——**要旧值就必须是它** |
| `minimal` | 只记变更列 + 主键 | 只记主键 | 最小（宽表上能省 5~10 倍） | 只读复制 + 应用侧不需要旧值 |
| `noblob` | 所有列，BLOB/TEXT 未变更时只记定位信息 | 同 | 折中 | 有大字段、又不需要大字段旧值的场景 |

`minimal` 是最常见的「省流量省出事故」的参数：**更新事件里没有旧值，CDC 下游就算不出「从什么变成什么」**，只能当作「发生了变更」处理，对账、审计、下游状态机全部要改写（§08.10 坑 5）。

### binlog 的物理样子与那个缝合点

一个事务在 binlog 里是一条 event 链：`Format_desc`（每文件一次的文件头）→ `GTID`（`gtid_mode=ON` 时每事务一条，形如 `3e11fa47-...:1234567`）→ `Query("BEGIN")` → `Table_map`（表 id → 库名.表名 + 列类型）→ 若干 `Update_rows` / `Write_rows` / `Delete_rows`（会被单 event 大小上限切成多段）→ **`Xid`**（事务结束，携带 InnoDB 的 XID）。

**`Xid` event 就是 §08.7 全部机制的锚**：恢复时 InnoDB 从 redo 里读出「prepared 事务的 XID 列表」，server 层扫 binlog 得到「已完整落盘的 XID 集合」，两个集合做差，就决定了每个 prepared 事务该提交还是回滚。

另外两个结构事实：

- **binlog cache**：事务的 binlog 先写内存（`binlog_cache_size`，默认 32 KB），超了落临时文件（此时 `Binlog_cache_disk_use` 增长）。`max_binlog_cache_size` 是硬上限——大事务在这里撑爆，而且**撑爆发生在提交时**，前面的 DML 全白做。
- **GTID**：`server_uuid:transaction_id`。它把「复制位点」从「文件名 + 偏移」变成「已执行事务集合的差」，于是从库能自动定位起点、能跳过重复事务、能在故障切换后自己找位置。代价是每条写语句都要能被包在单事务里（`CREATE TABLE ... SELECT`、部分临时表用法受限制，需 `enforce_gtid_consistency=ON`）。

### 三副面孔：复制 / PITR / CDC

| 用途 | 它读 binlog 的哪一部分 | 典型形态 | 断了会怎样 |
| --- | --- | --- | --- |
| **复制** | GTID + row event 流（→ comp/10） | dump 线程推送，从库 SQL 线程回放 | 从库延迟上涨 / 复制中断（`1236` 找不到位点） |
| **PITR** | 全备 + 全备点之后的 binlog，重放到某一时刻 | `mysqlbinlog --stop-datetime` 管道进 mysql | **恢复点回退**到最后一个完整 event（事故二） |
| **CDC** | row event 的前像/后像 + 位点（→ comp/33） | Canal / Debezium 解析，产出变更流 | 断点超出保留期 ⇒ 位点失效 ⇒ **只能重做全量快照** |

### `sync_binlog` 的三档持久性

| 取值 | 语义 | 崩溃/掉电会丢什么 | 备注 |
| --- | --- | --- | --- |
| **1** | 每个事务提交都 fsync binlog | 不丢（前提：盘不撒谎） | 8.0 的默认值；双 1 的一半 |
| **N（N>1）** | 累积到 N 个事务才 fsync | 丢最后 ≤ N 个事务的 binlog | 与组提交叠加时，实际是「按组计数」，不要按单个事务推 |
| **0** | 交给操作系统回写 | 丢掉 page cache 里所有未回写的 binlog | **窗口不是「1 秒」，是「OS 脏页回写间隔 × 写入速率」**；且 dump 线程可能已经把这些内容发给从库（事故二） |

事故二的机制值得单独看一遍：`sync_binlog=0` 时 binlog 只到了 page cache，而**复制的 dump 线程是读同一个文件的 page cache**，于是「主库还没持久 + 从库已经收到」同时成立。掉电后主库回退、从库前进 ⇒ 分叉。这是本篇唯一一处「异步复制的性能优点」变成「一致性缺点」的地方。

---

## 08.7 内部两阶段提交：崩溃在 5 个时点会发生什么

### 问题陈述

假设没有 2PC，只有「先写 redo 并提交，再写 binlog」：

- 崩在两者之间 ⇒ 主库有数据、binlog 没有 ⇒ 从库缺一段、PITR 缺一段。主库自己没事，**但复制链路永久性缺一次变更**，只能重建从库；
- 反过来「先写 binlog 再提交 redo」⇒ 崩在两者之间 ⇒ binlog 有、redo 没有 ⇒ 从库**多**一段数据。从库多数据比主库少数据更难处理：所有下游（缓存、ES、Kafka 消费者、对账）都已经按那段变更动过了，主库恢复后要对全世界做反向补偿。

所以必须有 2PC。而 MySQL 的 2PC 有一个优雅的特例：**它是一个「不完整」的 2PC——第二个阶段的前滚可以靠 binlog 的完整性代替「协调者日志」**。这就是为什么 MySQL 能在没有外部协调器的情况下做到「单机崩溃后主从不分叉」。

### 六步时序

| # | 谁做 | 写什么 | fync 归属 | 典型耗时 |
| --- | --- | --- | --- | --- |
| 1 | server 层 | 判断事务是否可提交、准备 XID、给事务编号（GTID）、把 binlog cache 封口 | — | 微秒级 |
| 2 | InnoDB | 写 redo：所有页变更 + **undo 的 prepare 标记**（事务进入 prepared 状态，写 `trx_sys` 的 prepared 列表） | **fsync** ← 第 1 次 | 介质写延迟（0.1~8 ms） |
| 3 | server 层 | 把 binlog cache 写进 binlog 文件（组提交的 FLUSH 阶段） | 通常不在这里 fsync | 内存拷贝或一次 `write()` |
| 4 | server 层 | **fsync binlog**（组提交的 SYNC 阶段） | **fsync** ← 第 2 次 | 介质写延迟 |
| 5 | InnoDB | 写 redo 的 **commit 标记**，事务从 prepared 转 committed（组提交的 COMMIT 阶段） | 可只 write 不 fsync（崩了也能靠 binlog 前滚） | 微秒到毫秒 |
| 6 | server 层 | 释放锁、返回 OK 给客户端 | — | — |

**图 3 · 内部 2PC 的时序与两个「危险窗口」**

```
 应用          server 层                    InnoDB                     磁盘
  │ COMMIT ──────►│                            │                         │
  │               │── ① prepare ──────────────►│                         │
  │               │                            │ redo(prepare)+undo标记 ─►│ fsync ★第1次
  │               │                            │◄──── ok ────────────────│
  │               │  ┌──── 危险窗口 W1 ─────┐   │                         │
  │               │── ② 写 binlog（cache→文件）► │（server 层自己的文件）  │ write
  │               │                            │                         │
  │               │  ┌──── 危险窗口 W2 ─────┐   │                         │
  │               │── ③ fsync binlog ─────────────────────────────────►│ fsync ★第2次
  │               │  └──────────────────────┘   │                         │
  │               │── ④ commit ───────────────►│ redo(commit 标记) ─────►│ write（可不 fsync）
  │◄── OK ────────│                            │                         │
  │               │                            │  此时锁释放、事务可见    │

 W1：redo 有 prepared、binlog 什么都没有 ⇒ 恢复时**回滚**（主从都没有，一致）
 W2：binlog 已完整、redo 还没 commit 标记 ⇒ 恢复时**前滚**（主从都有，一致）
 ★ 关键：W2 之所以安全，是因为 binlog 尾部可判定；这也是为什么
   `sync_binlog=0` 会把 W2 从「安全」变成「不确定」。
```

### 崩溃在 5 个时点：判定表

判定规则只有一条（记住它，其余都是推论）：

> **恢复时，InnoDB 拿出 redo 里所有 prepared 事务的 XID；server 层扫 binlog 找出「完整落盘」的事务的 XID。两集合求交：交集里的前滚（补 commit 标记并刷脏），差集里的回滚（沿 undo 反向执行）。**

| # | 崩在哪 | redo 里有什么 | binlog 里有什么 | 恢复动作 | 主从一致？ |
| --- | --- | --- | --- | --- | --- |
| ① | 步骤 2 的 fsync 完成之前 | 该事务无（或只有半截 redo block，靠 block 校验丢弃） | 无 | 无动作。脏页本来就是脏的，重放后页里没有这次变更 | **一致**（两边都没有） |
| ② | 步骤 2 之后、步骤 3 之前（W1 内） | **prepared**（含 XID，undo prepare 标记） | 无 | 拿 XID 查 binlog → 找不到 ⇒ **回滚 prepared 事务**（沿 undo 反向执行，见 §08.5 的代价） | **一致**（两边都没有） |
| ③ | 步骤 3 之后、步骤 4 之前（W2 内，binlog 仅在 page cache） | prepared | 三种可能：全丢 / 已回写完整 / 回写半截 | 全丢或半截 ⇒ 按「最后一个完整 event」截断 ⇒ 同 ②；已完整 ⇒ 同 ④。**这个不确定性正是 `sync_binlog=1` 要消灭的东西** | 取决于 binlog 完整性；若复制已把 page cache 内容送出，则**从库多**（事故二） |
| ④ | 步骤 4 之后、步骤 5 的 commit 标记持久之前 | prepared（没有 commit 标记） | **完整**（含 XID） | 查 XID → 找到 ⇒ **前滚**：补 commit 标记、保留事务、正常刷脏 | **一致**（两边都有） |
| ⑤ | 步骤 5 之后、返回 OK 之前（或返回后） | committed | 完整 | 正常重放，无特殊动作 | **一致**（两边都有，但客户端可能不知道——重试必须幂等） |

**这张表的读法**：①②③ 都归结为「回滚」，④⑤ 都归结为「前滚」，中间那条线**恰好在 binlog fsync 完成的那一刻**。所以「内部 2PC 的一致性质量」= 「binlog fsync 的可靠性」，这解释了下面这个几乎没人明说的事实：

> **内部 2PC 的保证强度，直接等于 `sync_binlog` 的取值。** 把 `innodb_flush_log_at_trx_commit` 调到 2 或 0，W1 与 ② 的判定会退化成「redo 整段丢失 ⇒ binlog 有、主库无 ⇒ 从库多数据」；把 `sync_binlog=0`，W2 的判定会退化成「看运气」。双 1 不是「更安全的配置」，是**这个 2PC 协议的前提条件**。

### 一个玩具模型（30 行，只抄顺序，不是源码）

```go
// 把「两日志 + 恢复判定」抽成两个文件，验证 §08.7 的判定表
type Log struct{ f *os.File }

func (l *Log) Append(b []byte, sync bool) error {
	if _, err := l.f.Write(b); err != nil {
		return err
	}
	if sync {
		return l.f.Sync() // ← 这一行就是事故三那 8 ms／次的来源
	}
	return nil
}

type XID uint64

func Commit(redo, binlog *Log, x XID, rows []byte) error {
	if err := redo.Append(fmt.Appendf(nil, "PREPARE %d\n", x), true); err != nil {
		return err // 步骤 2：prepare 必须 fsync，否则回退到情形 ①
	}
	if err := binlog.Append(fmt.Appendf(nil, "BEGIN %d\n%x\nXID %d\n", x, rows, x), true); err != nil {
		return err // 步骤 3/4：写成功但没 fsync 的话，崩了就停在情形 ③
	}
	return redo.Append(fmt.Appendf(nil, "COMMIT %d\n", x), false) // 步骤 5：可只写不 fsync
}

// Recover 实现 §08.7 的判定：prepared ∩ binlog-complete
func Recover(redo, binlog *Log) (commit, rollback []XID) {
	bd := readCompleteEvents(binlog) // ← 尾部半截 event 必须丢弃，否则会前滚不存在的事务
	for _, x := range readPrepared(redo) {
		if bd.HasXID(x) {
			commit = append(commit, x) // 情形 ④：binlog 已持久 ⇒ 前滚
		} else {
			rollback = append(rollback, x) // 情形 ②③：binlog 没有 ⇒ 回滚
		}
	}
	return
}
```

把 `Commit` 的第二个 `sync` 改成 `false`，你就复现了 `sync_binlog=0`：此时 `Recover` 的判定依赖「page cache 有没有被回写」这个不可控变量——**协议没变，前提没了**。

---

## 08.8 组提交与双 1：吞吐是这么被 fsync 定价的

### 三个阶段的本质：把 N 次 fsync 摊成 1 次

既然一次提交要付 2 次 fsync，那唯一的出路就是**让多个事务共享同一次 fsync**。binlog 组提交就是这个，且它有三段流水：

| 阶段 | 谁领头 | 干什么 | 为什么能合并 |
| --- | --- | --- | --- |
| **FLUSH** | 组内第一个进队列的事务 | 把每个成员的 binlog cache 依次写进 binlog 文件 | 写文件是内存拷贝，合并只省系统调用，收益小但零风险 |
| **SYNC** | 同上（可以不是同一个） | 对 binlog 文件做**一次** fsync | 一次 fsync 保证「此前写入的所有事务」都持久——这才是真正的收益 |
| **COMMIT** | 同上 | 依次调引擎的 commit（写 redo commit 标记） | redo 的顺序也可以合并，且这一步通常不需要 fsync |

三段各有自己的队列与「leader」，于是**流水线化**：A 组在 SYNC 时，B 组已经在 FLUSH 里排队。所以组提交不只是「把 N 摊成 1」，还是「让 fsync 与写日志重叠」。

**图 4 · 组提交时间轴（`binlog_group_commit_sync_delay=200 µs`）**

```
 时间 ──────────────────────────────────────────────────────────────►
 事务到达   ① ② ③ ④ ⑤ ⑥ ⑦ ⑧      ① ② ③ ④ ⑤      （每格 ≈ 30 µs）
             └──── 组 A ────┘      └── 组 B ──┘
 
 组 A: [FLUSH 写 8 个事务] [SYNC 1 次 fsync 8 ms] [COMMIT 8 次]
                                      │
 组 B:              [FLUSH 已在写] ────┤ 等 SYNC 队列（可并行于 A 的 COMMIT）
                                      ▼
                            [SYNC 1 次 fsync 8 ms]

 每个事务的提交延迟 ≈ delay/2（平均等组满的时间） + fsync + 排队
 吞吐 = 组内事务数 ÷ (delay + fsync_redo + fsync_binlog)
     ⇒ delay 是「用延迟买吞吐」的旋钮：delay 越大组越大，但每人多等 delay/2
```

两个旋钮的取舍：

| 参数 | 语义 | 调大的收益 | 调大的代价 | 什么时候用 |
| --- | --- | --- | --- | --- |
| `binlog_group_commit_sync_delay` | 攒多久再 fsync（微秒） | 组变大，TPS 近线性上升 | 每个事务的提交延迟 +delay/2（P99 直接涨） | 高并发 + 能接受 ms 级提交延迟（OLTP 交易 200~1000 µs 很常见） |
| `binlog_group_commit_sync_no_delay_count` | 攒够 N 个事务就不等 delay | 低并发时避免「为了等 delay 白等」 | 组大小有上限，峰值吞吐受 N 限制 | 并发波动大的系统：`delay=1000 µs` + `count=32` 组合最稳 |

### 单事务 TPS：三档介质的硬上限

```
双 1、无组提交（并发 = 1）：
  TPS = 1 / (T_redo_fsync + T_binlog_fsync)
  两个 fsync 串行，因为它们属于两个不同的日志文件、且顺序有依赖（§08.7 步骤 2→4）
```

| 介质 | 单次 fsync 延迟 | 单事务提交耗时 | **单线程 TPS 上限** | 观测到的典型值 |
| --- | --- | --- | --- | --- |
| 本地 NVMe | 0.1 ms | 0.2 ms | **5,000** | 3,000~4,500（还有 CPU 与刷脏） |
| 同 AZ 云盘 | 2 ms | 4 ms | **250** | 180~240 |
| 跨 AZ 云盘 | 8 ms | 16 ms | **62.5** | 55~62 |

**承诺值**：给定介质写延迟，`1/(2 × fsync)` 是单连接不可逾越的上界，实际只会更低。**推论**：靠加连接数线性堆 TPS 只在 `连接数 < 1/(2×fsync)` 的尺度内成立——一旦连接数超过「每秒能完成的提交次数」，再多的连接只是排队。

### 组提交之后：G 个事务共用一次 fsync

```
TPS(G) = G / (2 × T_fsync + delay)
  G = 组内事务数 = min(并发写入线程数, 到达速率 × delay, no_delay_count)
```

| 介质 | G=1（无组提交） | G=8 | G=32 | G=128 | 现实天花板 |
| --- | --- | --- | --- | --- | --- |
| 本地 NVMe（0.1 ms） | 5,000 | 40,000 | **160,000** | 640,000 | 3~8 万（CPU / 刷脏 / 行锁） |
| 同 AZ 云盘（2 ms） | 250 | 2,000 | **8,000** | 32,000 | 1~2.5 万 |
| 跨 AZ 云盘（8 ms） | 62.5 | 500 | **2,000** | 8,000 | 2,000~6,000 |

**量级结论**（假设：组内事务全部在同一轮 FLUSH/SYNC 里、无 CPU 与刷脏瓶颈、`delay=0`）。表里 `G=128` 一列是纯算术上界，**不要拿它做容量承诺**：真实上限出现在「CPU 打满」或「checkpoint age 上涨」时，比算术值低 2~10 倍。

```python
# 组提交吞吐：把「一次 fsync 买到多少事务」算清楚（可直接复算 §08.8 的三张表）
def tps(g, fsync_ms, delay_ms=0.0):
    """g 个事务合成一组：共享 1 次 redo fsync + 1 次 binlog fsync，外加 delay 等待"""
    return g / (2 * fsync_ms + delay_ms) * 1000

MEDIA = {"NVMe(本地)": 0.1, "云盘(同 AZ)": 2.0, "云盘(跨 AZ)": 8.0}

for name, f in MEDIA.items():
    print(f"{name}: 单事务 {tps(1, f):8.0f} TPS")
    for g in (8, 32, 128):
        print(f"    G={g:<4d} delay=0      → {tps(g, f):9.0f} TPS")
        print(f"    G={g:<4d} delay=0.2 ms → {tps(g, f, 0.2):9.0f} TPS")

# 退化检查：组大小 = min(并发, 到达速率 × delay, no_delay_count)
for g_real in (1, 2, 5, 16):
    print(f"跨 AZ 实际只攒到 {g_real} 个事务 → {tps(g_real, 8.0):.0f} TPS")
# 输出解读：g_real 从 16 掉到 1，TPS 从 1,000 掉到 62.5 —— 这就是"半组提交退化"
```

### 「半组提交退化」的三种形态

这是最容易在压测里被误读的现象——TPS 明明有组提交，却远低于预期：

| 形态 | 现象 | 根因 | 怎么确认 |
| --- | --- | --- | --- |
| **组太小**（并发不够） | TPS 随连接数线性涨，直到某个点才跳变 | 组大小 = min(并发, …)；20 个连接最多攒 20 个 | `Binlog_group_commit_trigger_count` 与 `Binlog_commits` 的比值 ≈ 平均组大小 |
| **delay 白等**（低并发高 delay） | 延迟涨了、TPS 没涨 | 组本来就只有 1~2 个事务，delay 纯粹是白等 | `Binlog_group_commit_trigger_timeout` 远多于 `trigger_count` |
| **两轮错位**（fsync 与 delay 不匹配） | 组内一部分成员赶上了同一 fsync，另一部分被推到下一轮 | delay 小于到达间隔，组刚满就被新事务插队；或者 redo 的 fsync 与 binlog 的 fsync 节奏对不齐 | 事务提交 P99 ≈ 2×fsync 而 P50 ≈ fsync，双峰 |

对策不是「把 delay 调大」这么简单：**delay 的合适值 ≈ 期望组大小 ÷ 到达速率**。组大小 32、每秒到达 20,000 个事务 ⇒ 每个到达间隔 50 µs ⇒ delay ≈ 32 × 50 µs = 1.6 ms。这个数不从别人的博客抄，从你自己的到达速率算。

### 4×3 矩阵：`sync_binlog` × `innodb_flush_log_at_trx_commit`

每个单元格写三件事：**丢什么 / 相对吞吐（以双 1 为 1×，含组提交前的单事务口径）/ 一致性风险**。

| | `sync_binlog=0` | `sync_binlog=1` | `sync_binlog=N（N>1）` |
| --- | --- | --- | --- |
| **`flush_log_at_trx_commit=1`** | 丢：page cache 里全部未回写 binlog｜吞吐 ~2×｜风险**高**：主库有、binlog 无 ⇒ 从库与 PITR 永久缺口，**只能重建从库** | 丢：不丢已提交（盘不撒谎前提下）｜吞吐 1×｜风险**无**（基线） | 丢：≤N 个事务的 binlog｜吞吐 ~1.5×｜风险中：窗口有界但不为零，窗口内掉电一样分叉 |
| **`flush_log_at_trx_commit=2`** | 丢：redo ≤1 s + binlog 尾部｜吞吐 ~2×｜风险**高**：两侧窗口叠加，判定全靠运气 | 丢：redo ≤1 s，binlog 完整 ⇒ **主库缺、从库多**｜吞吐 ~2×｜风险**高（分叉）**：2PC 的 W2 前滚失去了 redo 侧依据 | 丢：redo ≤1 s + ≤N 事务｜吞吐 ~1.5×｜风险高 |
| **`flush_log_at_trx_commit=0`** | 丢：两侧全丢，窗口 = redo 每秒刷 + OS 回写间隔｜吞吐 ~4×｜风险**灾难**：已返回 OK 的事务可能整批消失，undo 修改一起丢 | 丢：redo ≤1 s（连 undo 页的修改一起丢）｜吞吐 ~3×｜风险高：反而比 `=2` 更糟——binlog 有、redo 没有 | 丢：redo ≤1 s + ≤N 事务｜吞吐 ~3×｜风险高 |
| **`=1` + `sync_delay=200 µs`**（变体行） | 同上，delay 改变不了持久性｜吞吐 ~2× × 组大小｜风险高 | 丢：不丢；提交延迟 +~0.1~0.2 ms｜吞吐 **G 倍**｜风险**无** | 丢：≤N 事务｜吞吐 G 倍｜风险中 |

**读这张表的规矩**：只有 `=1` 与 `sync_binlog=1` 那一格，「已返回 OK 就不丢」才成立。其余每一格都是**在拿一个窗口换吞吐，而这个窗口必须写成业务承诺**（能丢多少秒、丢了怎么补、谁对账）。最危险的一格不是 `0/0`（大家都知道它危险），是 **`1/2`**：它看起来「redo 有开关、binlog 也 fsync 了」，实际结果是主库缺、从库多——一个必须对全世界做反向补偿的形状。

---

## 08.9 架构边界：这套机制能保证什么、不能保证什么

| 你的需求 | 它能不能 | 靠什么 / 代价 | 该走哪 |
| --- | --- | --- | --- |
| 「返回 OK 的事务，单机掉电后还在」 | **能**（双 1 + 盘不撒谎） | 每个事务 2 次 fsync，TPS 上限 = `1/(2×fsync)` | 组提交把 N 摊 1（§08.8） |
| 「主库与从库不会出现主库有、binlog 无」 | **能**（内部 2PC） | 前提是 `sync_binlog=1` | 2PC 的保证强度 = `sync_binlog` 的取值（§08.7） |
| 「恢复到任意一秒」 | **能到「最后一个完整 event」**，不是任意秒 | PITR 精度 = binlog event 的粒度 + 你指定的 `--stop-datetime` | 要更细靠业务侧流水（→ comp/33） |
| 「fsync 返回了就一定在盘上」 | **不能** | 盘/RAID 卡/云盘块存储可能把 fsync 骗回去（写缓存 + 电池失效） | 监控「掉电后数据丢失」只能靠副本 + 定期恢复演练 |
| 「跨实例原子提交」 | **不能**（内部 2PC 只覆盖 redo 与 binlog） | 跨实例要么 XA、要么补偿 | [eng/8 分布式事务总览](../eng/8-distributed-transaction-overview.md) |
| 「redo 当备份用」 | **不能**（循环写，旧段被覆盖） | 要备份用全备 + binlog 归档 | 物理备份 + binlog 保留期（§08.11） |
| 「binlog 想留多久留多久」 | **不能**（保留期是配置，磁盘是硬约束） | `binlog_expire_logs_seconds` 到了就删 | CDC 断点必须 < 保留期（§08.10 坑 4） |
| 「undo 表空间可以无限大」 | **不能**（等于磁盘上限，且 purge 会更慢） | 长事务是唯一真凶 | 事务年龄告警（→ comp/06 §06.10） |
| 「把 redo 设小一点省磁盘」 | **不能省，只会换来写阻塞** | redo 容量 = 刷脏的缓冲池 | 按 §08.4 的算式反推，别用默认值 |
| 「加大 CPU 就能提高写 TPS」 | **不能**（双 1 下） | 提交被 fsync 定价，CPU 只在组提交后才有意义 | 先算 `1/(2×fsync)`，再谈 CPU |

### ⑤ 扩展维度：写路径不可横向扩

按八问卡 ⑤ 的四选一，InnoDB 的**写提交路径**只能给出一个不体面的答案：

| 手段 | 对写提交有没有用 | 为什么 |
| --- | --- | --- |
| **分片** | **有（唯一真正有效）** | 每个实例有自己的 redo/undo/binlog 与自己的 fsync 队列；N 个实例约等于 N 倍提交能力，代价是跨分片原子性消失、binlog 变成 N 条流（→ [eng/36 MySQL 极限与分库分表](../eng/36-mysql-limits-sharding.md)） |
| **副本** | **只扩读，不扩写** | 写必须在单点串行产生一条 binlog（GTID 单调、组提交按到达顺序），副本越多，主库的 binlog 发送压力越大、延迟越不可控（→ comp/10） |
| **垂直切** | **有，但是线性的** | 换更快的盘（8 ms → 0.1 ms 直接换 80 倍）、更大 redo、调 `io_capacity`；这是一次性收益，不是扩展 |
| **批处理摊薄** | **有，且免费** | 组提交就是这条：把 N 个事务摊到 1 次 fsync，本质是「批处理」在日志层的形态 |

一句话：**InnoDB 的写吞吐是一个「单机 fsync 队列 × 组大小」的乘积，两端天花板都在单机内。** 越过它的方式只有分片——而分片之后你换来的是一个新的问题：跨分片的 `COMMIT` 不再有原子性。

---

## 08.10 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| # | 现象 | 根因（指到物理形态） | 怎么定位 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- | --- |
| 1 | 一条批量 `UPDATE`/`DELETE` 期间全库写入排队几十秒，应用侧 P99 从 ms 跳到秒，`Innodb_log_waits` 暴涨 | 单事务 redo 量 > redo 容量 ⇒ redo 写满 ⇒ checkpoint 推不动 ⇒ 所有写请求等空间（§08.4） | `SHOW ENGINE INNODB STATUS` 的 LOG 段看 `Log sequence number − Last checkpoint at ≈ capacity`；`Innodb_log_waits` 斜率；`information_schema.innodb_trx` 里 `trx_rows_modified` 上百万的那个事务 | 短期：`KILL` 大事务（注意回滚代价，§08.5）；中期：`innodb_redo_log_capacity` 提到 GB 级、`innodb_io_capacity`/`_max` 按 SSD 上调；长期：**分批提交**，每批 < 1 万行且 < 1 s | 批量接口上线前必须 dry-run 报影响行数；`UPDATE/DELETE` 影响行数 > 10 万进审查；redo 容量按 §08.4 算式写进基线 |
| 2 | 掉电/内核 panic 后，重启丢了约 1 秒的已提交事务（应用已收到 OK） | `innodb_flush_log_at_trx_commit=2`（只 write 到 OS cache，不 fsync）或 `=0`（每秒刷一次） | `SELECT @@innodb_flush_log_at_trx_commit`；对比 binlog 尾部时间戳与业务侧「已确认订单」时间戳 | 改回 1；若确定不能改，则必须接受「丢 ≤1 s」，并把幂等键 + 补偿任务做实 | 参数进基线巡检；压测报告里必须写「本配置的 RPO」而不是「高性能」 |
| 3 | 崩溃后 PITR 恢复点比预期早几秒，binlog 尾部有半截 event；下游（从库/Kafka/CDC）比主库多 | `sync_binlog=0`：binlog 在 page cache，dump 线程却已把它读出去发给了下游 | `mysqlbinlog` 看文件尾部是否以 `Xid`/`COMMIT` 结束；`SHOW MASTER STATUS` 的 `Position` 与 `max_binlog_size` 的关系；比对从库 `gtid_executed` 是否包含主库没有的 GTID | 改 `sync_binlog=1`；已分叉的只能**重建从库** + 用主库数据反向补偿下游 | `sync_binlog=1` 与 `binlog_format=ROW` 一起进上线检查单；定期做「杀掉主库 → 恢复 → 校验 GTID 集合」演练 |
| 4 | CDC 位点失效（`binlog.000xxx` 已被清理），Canal/Debezium 报找不到文件，只能重做全量快照 | binlog 保留期（`binlog_expire_logs_seconds`，8.0 默认 30 天）短于 CDC 的落后时长/停机时长 | `SHOW BINARY LOGS` 看最早文件；对比 CDC 保存的 `gtid`/`position` 与该文件起点 | 临时提高保留期 + 尽快拉起消费者；实在来不及就按「快照 + 新位点」重启全量 | 保留期必须 > 最长停机窗口 + 最长消费落后；CDC 位点滞后做告警（→ comp/33） |
| 5 | 上游改了列，下游 CDC 拿不到旧值，对账/审计算不出来；或下游状态机发出「莫名其妙的变更」 | `binlog_row_image=minimal`（或 `noblob`）：row event 里只有主键与变更列，没有前像 | `mysqlbinlog --verbose --base64-output=DECODE-ROWS` 看 `### WHERE` 段是否只有主键 | 改 `binlog_row_image=FULL`（会话级或全局） | 参数写进 CDC 相关集群的必检项；schema 变更评审必须过 CDC 影响面 |
| 6 | 大事务提交时报 `Multi-statement transaction required more than 'max_binlog_cache_size' bytes of storage`，前面几十分钟的 DML 全白做 | binlog cache 撑爆：事务的 binlog 必须先完整存在（内存或临时文件），只有一个上限 | `Binlog_cache_disk_use / Binlog_cache_use` 比例；错误码 1197 / `ER_TRANS_CACHE_FULL` | 分批（首选）；或临时调大 `max_binlog_cache_size`（但磁盘要够，且大事务的从库延迟问题仍然在） | 事务行数上限进代码规范；`Binlog_cache_disk_use` 增长做告警 |
| 7 | KILL 掉大事务后系统继续卡好几分钟，`SHOW PROCESSLIST` 显示 `rollback` | 回滚要沿 undo 反向执行，代价与正向同量级（§08.5），且期间锁不释放、undo 不释放 | `SHOW PROCESSLIST` 状态；`innodb_trx` 里 `trx_state` | 等；别再叠加新的大事务 | 不靠 KILL 救火，靠事务年龄与 `trx_rows_modified` 预警 |
| 8 | 主库 `sync_binlog=1`、从库延迟却台阶式上涨，且每次台阶对应一次大事务 | binlog 事务在从库不可拆分；ROW 格式下一条 200 万行更新是几千个 row event 打成一个事务 | 从库 `performance_schema.replication_applier_status_by_worker`；`mysqlbinlog` 单事务字节数 | 分批；开并行回放并确认 `WRITESET` 依赖跟踪生效（→ comp/10） | 大事务治理与 §08.10 坑 1 合并管理 |

---

## 08.11 观测：指标、拐点与告警

**纪律提醒**：每条都标了版本与开关前提。同名指标在不同版本语义变过（尤其 8.0.30 的 redo 重构），照抄别人的面板是事故的常见起点。

| 指标 | 怎么取（版本 + 开关） | 含义（指到物理形态） | 稳态 | 拐点 | 告警骨架 |
| --- | --- | --- | --- | --- | --- |
| **checkpoint age 占比** | 8.0.30+：`Innodb_redo_log_current_lsn − Innodb_redo_log_checkpoint_lsn`，除以 `Innodb_redo_log_capacity_resized`。8.0.30 之前只能读 `SHOW ENGINE INNODB STATUS` 的 LOG 段（`Log sequence number` − `Last checkpoint at`）再除以容量 | redo 环里「必须留着、不能覆盖」的那一段占比；就是「刷脏还剩多少余量」 | < 40% | > 75% 开始危险，> 90% 一次批量更新就能打满 | `age_ratio > 0.75` 持续 2 分钟 → 告警；`> 0.9` → 一级 |
| **redo 生成速率** | `rate(Innodb_os_log_written[1m])`（全局状态，累计写入 redo 的字节数，不是 fsync 次数）；8.0.30+ 也可用 `Innodb_redo_log_current_lsn` 的增量（LSN 有字节含义） | 写入压力有多大；与容量相除得「撑多少秒」 | 与业务 TPS 同比例 | 突增 3 倍以上 = 有大事务或批量任务 | 配合 age 一起看：`rate` 涨而 `age` 不涨 ⇒ 没事；`age` 涨 ⇒ 刷脏跟不上 |
| **`Innodb_log_waits`** | 全局状态计数器（首次出现等待后才有意义）。8.0.30 换了 redo 文件组管理后，写满的行为有调整，**先在你自己的版本上验证一次它是否随写满增长**；不确定时用 `age_ratio → 1` 兜底 | 「因为没有 redo 空间而等待」的累计次数，是全库写阻塞的直接证据 | 0（长期恒为 0 是健康的） | 任何时间内 > 0 都值得看一眼 | `increase(..._innodb_log_waits[5m]) > 0` 告警，附当时的 `innodb_trx` 最大 `trx_rows_modified` |
| **binlog 体积与缓存落盘比** | `SHOW BINARY LOGS`（文件与大小）、`Binlog_cache_use` / `Binlog_cache_disk_use`（8.0 全局状态） | `cache_disk_use / cache_use` 高 = 大事务在落临时文件，逼近 `max_binlog_cache_size` | < 1% | > 10% 说明大事务已成常态 | 比例 > 5% 持续 10 分钟告警 |
| **组提交效率** | `Binlog_group_commit_trigger_count` / `_timeout` / `_lock_wait` / `_no_delay_count`（8.0 全局状态）+ `Binlog_commits` | 平均组大小 ≈ `Binlog_commits / trigger_count`；`trigger_timeout` 占比高 = delay 白等 | 组大小 ≥ 8 | 组大小 < 4 且 TPS 上不去 = 并发不足或 delay 不合适 | 面板上画「组大小」曲线，掉到 3 以下提示调 delay 或连接数 |
| **`last_committed` / `sequence_number`** | `mysqlbinlog --verbose` 解析 8.0 的 row event 注释 | 逻辑时钟（依赖跟踪）：同一 `last_committed` 的事务可并行回放（→ comp/10） | 递增平滑 | 出现大面积相同 `last_committed` = 从库并行度好；全不同 = 串行回放 | 不告警，用于解释「从库延迟为什么降不下来」 |
| **提交延迟 P50/P99** | 应用侧埋点，或 `performance_schema.events_statements_history_long` | P50 ≈ fsync，P99 ≈ delay + fsync + 排队；双峰 = 半组提交错位（§08.8） | P50 < 2 ms | P99 / P50 > 10 | 提交 P99 进 SLO；与组大小联动看 |
| **`Binlog_group_commit` + `Innodb_data_fsyncs` 速率** | `rate(Innodb_data_fsyncs[1m])` | 每秒 fsync 次数 ≈ 2 × TPS / 组大小；**它才是写入成本的真相** | 与组大小成反比 | 突然翻倍 = 组提交失效 | 画「TPS ÷ fsync 次数」曲线，掉下去就是退化 |

排查顺序只有一条，反过来查永远只能得到结果得不到原因：

```
① 先看 age_ratio：它高 → 是 redo 容量/刷脏问题（事故一）
② 再看 Innodb_log_waits 是否在涨：涨 → 已经进入写阻塞，立刻找大事务
③ 再看 binlog 侧：cache_disk_use 高 → 大事务；组大小小 → 并发或 delay
④ 最后看提交延迟的 P99/P50 双峰：错位 → 调 delay/count
⑤ 全部正常但业务说「丢了几秒」→ 直接查三个开关：
   innodb_flush_log_at_trx_commit / sync_binlog / 盘是否撒谎
```

```bash
# ① 看清 binlog 里到底写了什么（row event + 组提交的两个逻辑时钟）
mysqlbinlog --verbose --base64-output=DECODE-ROWS \
  --start-datetime='2026-01-22 16:39:00' --stop-datetime='2026-01-22 16:41:00' \
  /var/lib/mysql/binlog.000417 \
  | grep -E 'last_committed|sequence_number|Xid|### (WHERE|SET)' | head -40

# ② 当前落盘边界与容量（8.0.30+）
mysql -e "SHOW GLOBAL STATUS WHERE Variable_name IN (
  'Innodb_redo_log_current_lsn','Innodb_redo_log_checkpoint_lsn',
  'Innodb_redo_log_capacity_resized','Innodb_os_log_written',
  'Innodb_log_waits','Innodb_data_fsyncs','Binlog_cache_disk_use','Binlog_cache_use');"

# ③ 崩溃实验（只在测试库做）：双 1 下 kill -9，验证「已确认的一条不少」
sysbench oltp_write_only --tables=8 --table-size=100000 --threads=16 \
  --events=0 --time=120 run &
sleep 20 && kill -9 "$(pidof mysqld)"          # 模拟掉电
# 重启后：应用侧收到 OK 的主键必须全部存在；binlog 尾部允许半截，但中间不许有空洞
mysql -e "SELECT MAX(id), COUNT(*) FROM sbtest1; SHOW MASTER STATUS;"

# ④ 把 sync_binlog 改成 0 重跑同一实验：会看到「binlog 少一段、下游却已消费」的分叉
```

---

## 08.12 一页速记

### 判据表：三个开关定三件事

| 开关 / 面 | 它决定什么 | 安全值 | 危险值 | 危险值的后果形状 |
| --- | --- | --- | --- | --- |
| `innodb_flush_log_at_trx_commit` | redo 是否在提交时 fsync | 1 | 2 / 0 | 丢 ≤1 s 已提交；且 2PC 的 W1 判定失效 |
| `sync_binlog` | binlog 是否在提交时 fsync | 1 | 0 / N | 主库有、binlog 无 ⇒ 从库与 PITR 缺口；从库可能**比主库多** |
| `innodb_redo_log_capacity` | 能容忍多少脏页积压 | ≥ 峰值生成速率 × 60 s × 2 | 默认 100 MB 扛大事务 | redo 打满 ⇒ 全库写阻塞（不是大事务自己慢） |
| `binlog_row_image` | row event 有没有前像 | FULL | minimal / noblob | CDC 与审计拿不到旧值 |
| `binlog_expire_logs_seconds` | binlog 能追多久 | > 最长停机 + 最长消费落后 | 30 天（默认）但 CDC 断了 40 天 | 位点失效，只能重做全量 |
| `binlog_group_commit_sync_delay` | 用多少延迟买多少吞吐 | 期望组大小 ÷ 到达速率 | 拍脑袋设 10000 µs | 延迟涨、TPS 不涨（低并发时白等） |

### 十条一口价

1. 一次提交落三份日志：redo（引擎、物理逻辑、循环写）、undo（引擎、逻辑反向、靠 redo 持久）、binlog（server、逻辑、不循环）。
2. **没有人 fsync undo**——undo 页的变更以 redo 记录落盘，所以 undo 的持久性是 redo 的副产物。
3. redo block 是 512 B（12 B header + 数据 + 4 B trailer）；8.0.30+ 由 `innodb_redo_log_capacity` 管，约 32 个文件各占 1/32。
4. redo 容量买的不是日志大小，是「刷脏能落后多久」；`checkpoint age / capacity` 是全篇第一指标。
5. 内部 2PC 的判定规则：redo 里 prepared 的 XID ∩ binlog 里完整的 XID = 前滚，差集 = 回滚。
6. 那条「前滚还是回滚」的分界线，精确落在 **binlog fsync 完成**的那一刻 ⇒ 2PC 的保证强度 = `sync_binlog` 的取值。
7. 双 1 单事务 TPS ≤ `1 / (2 × fsync)`：NVMe 5,000、同 AZ 云盘 250、跨 AZ 62.5。CPU 在这里没有话语权。
8. 组提交 = 把 N 个事务摊到 1 次 redo fsync + 1 次 binlog fsync：`TPS = G / (2×fsync + delay)`；组大小 = min(并发, 到达率×delay, no_delay_count)。
9. 回滚不是免费的：百万行事务的回滚是几十秒到几分钟，且期间锁与 undo 都不释放。
10. 只有「`flush_log_at_trx_commit=1` + `sync_binlog=1`」这一格，「返回 OK 就不丢」才成立；其余每一格都在拿一个必须写进业务承诺的窗口换吞吐。

---

## 08.13 自检题

**1. 为什么「先提交 redo 再写 binlog」和「先写 binlog 再提交 redo」都不行？内部 2PC 到底解决了什么？**
<details><summary>答案</summary>

前者：崩在中间 ⇒ 主库有数据、binlog 没有 ⇒ 从库与 PITR 永久缺一次变更（而且主库自己看不出来，只能靠 `CHECKSUM TABLE` 或对账发现）。后者：崩在中间 ⇒ binlog 有、redo 没有 ⇒ 从库**多**一段数据；从库多比主库少更难处理，因为下游（缓存、ES、Kafka、对账）已经按那段变更动过了，要做的是对全世界反向补偿。
2PC 解决的不是「让两件事原子」（它做不到），而是**把不确定窗口压缩到「binlog 已完整但 redo 未标提交」这一个可判定的形状**：恢复时用 binlog 里是否有该 XID 来决定前滚还是回滚。它成立的前提是 binlog 尾部可判定，所以 `sync_binlog=1` 不是优化项，是协议前提。
</details>

**2. redo 是循环写的，为什么不干脆设成 10 GB 让它永远写不满？**
<details><summary>答案</summary>

两个代价。① **恢复时间**：崩溃恢复要从最新 checkpoint 重放到末尾，重放速率量级 50~200 MB/s，10 GB 意味着 50~200 秒的启动阻塞——这是可用性成本。② **它治不了真正的病**：redo 大只是把「写满」推后；如果刷脏能力长期低于 redo 生成速率（事故一里默认 `innodb_io_capacity=200`），再大的 redo 最终也会被填满，只是从「一次大事务触发」变成「每天都慢一点」。正确顺序是：**先把刷脏能力配对（`io_capacity`/`_max` 按盘调），再按「峰值生成速率 × 可容忍窗口 × 2」定容量**，最后用「单事务 redo 量 < 容量」做验收。
</details>

**3. 一笔事务返回 OK 之后 1 ms 掉电。它还在吗？分别在 `flush_log_at_trx_commit` = 1 / 2 / 0 下回答。**
<details><summary>答案</summary>

`=1`：在（redo 已 fsync；binlog 也 fsync 了，属于情形 ⑤）。`=2`：**可能不在**——redo 只 write() 到了 OS page cache，机器掉电（不是进程崩溃）就丢；进程崩溃则还在。此时 binlog 可能已 fsync ⇒ **主库丢、从库有**，这是最难处理的形状。`=0`：更可能不在，连 OS cache 都没到（每秒才刷一次），`innodb_flush_log_at_timeout` 决定窗口。
注意区分「进程崩溃」与「掉电」：`=2` 只在掉电/内核 panic 时丢，进程 crash 不丢；很多「我们压测过没丢」的结论就是在进程级 kill 下得出的。
</details>

**4. 组提交把 32 个事务合并后 TPS 应该是单事务的 32 倍，可压测只涨了 5 倍。列出至少 4 个可能原因与各自的确认方法。**
<details><summary>答案</summary>

① **组根本没攒到 32**（并发不足）：平均组大小 = `Binlog_commits / Binlog_group_commit_trigger_count`，若接近 1~5 就是它。
② **delay 白等**（低并发 + 大 delay）：`Binlog_group_commit_trigger_timeout` 远多于 `trigger_count`。
③ **瓶颈转移**：组提交省掉的是 fsync，接下来打满的是 CPU（行锁、索引维护）、刷脏（`checkpoint age` 开始上涨）或网络；看 CPU 利用率与 age_ratio 曲线。
④ **单事务本身变慢**（事务里行数多了、锁等待多了）：对比单事务模式的 `Transaction` 耗时分布。
⑤ **两阶段错位**：提交延迟 P50 ≈ fsync 而 P99 ≈ 2×fsync，双峰说明一部分成员没赶上同一轮 SYNC。
</details>

**5. 只有 `binlog_format=STATEMENT` 时，内部 2PC 还能保证主从不分叉吗？为什么？**
<details><summary>答案</summary>

**不能。** 2PC 保证的是「主库数据与 binlog 内容同时成立」，它保证不了「从库重放这段 binlog 能得到与主库相同的数据」——后者取决于 binlog 的**确定性**。STATEMENT 格式下，`NOW()`、`RAND()`、无 `ORDER BY` 的 `LIMIT`、RC 下的半一致读、甚至主从索引不同导致的扫描顺序差异，都会让从库改到不同的行集。
所以两层保证要分开看：**内部 2PC 管「日志与数据一致」**（§08.7），**ROW 格式管「日志与重放一致」**（§08.6）。少任何一层，事故形态不同但都会分叉。
</details>

---

## 08.14 延伸阅读

- 通用 WAL 原理、LSN、checkpoint、doublewrite、崩溃恢复三阶段（本篇刻意不重写）：→ comp/03
- 页、行格式、聚簇索引与二级索引的物理形态（redo 记的「页号 + 偏移」指的就是它们）：[comp/05 InnoDB 的页与行](05-innodb-page-and-row.md)
- undo 版本链、Read View、长事务与 purge（undo 版「谁在钉住回收」）：[comp/06 MVCC 与隔离级别](06-mvcc-and-isolation-levels.md)
- 行锁、间隙锁与死锁（回滚期间为什么不释放锁）：→ comp/07
- 优化器与执行计划（一条 `UPDATE` 影响多少行，是怎么被算出来的）：→ comp/09
- 主从复制、半同步、并行回放与 MGR（binlog 的下游，与 `last_committed` 的用法）：→ comp/10
- PostgreSQL 对照（WAL 只有一份、没有内部 2PC 的影子——它把逻辑复制做在另一层）：→ comp/11
- CDC 与位点（binlog 的第三副面孔）：→ comp/33
- 分布式事务（跨实例原子性，内部 2PC 管不到的那一半）：[eng/8 分布式事务总览](../eng/8-distributed-transaction-overview.md)
- MySQL 单机极限、分库分表拐点（写吞吐撞上 fsync 定价后的唯一出路）：[eng/36 MySQL 极限与分库分表](../eng/36-mysql-limits-sharding.md)
- 事务组件 XA / AT / TCC（外部 2PC 与内部 2PC 的区别）：→ comp/43
- 本篇在全套里的位置与写作公约（八问卡、三本放大账、版本与指标纪律）：[0-summary 总纲](0-summary.md)
