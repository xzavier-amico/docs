# 03. WAL、缓冲池与崩溃恢复

> **状态**：`已完成（初稿）` ｜ **所属**：第一部分 · 地基 ｜ **编号**：03 / 53
> **本篇定位**：Part I（01–04）的第三篇，也是全系列唯一一处把「一个已提交的事务凭什么不丢」拆到物理路径和系统调用级的地方。02 讲三种引擎形态的放大账，04 讲索引的物理形状，本篇夹在中间讲**它们全都共用的那一条机制**：写前日志（WAL）。MySQL 的 redo、PG 的 WAL、RocksDB 的 log、Kafka 的 segment、Flink 的 checkpoint、etcd 的 raft log，都是同一条规则的换皮。
> **读法**：§03.2 的唯一真问题是全篇支点，剩下十一节都是它的推论。§03.3 的写路径层次图与 §03.4 的「页 LSN ≥ 记录 LSN 就跳过」要能默画；正在救火的直接读 §03.8 的三本账与 §03.10 的清单；§03.11 的指标表每条都带版本与开关前提，可以整表贴进值班手册。
> **不在本篇**：redo block 字节结构、binlog event 格式、内部两阶段提交与组提交的**阶段细节** → `comp/08`；复制链路与半同步 → `comp/10`；MVCC 可见性 → `comp/06`；InnoDB 页布局与 B+ 树 → `comp/05`。本篇用 InnoDB 举例时只讲「它在这一步做了什么」，不列它的文件格式字段表。

---

## 03.1 引子：三起「机器没坏，数据丢了 / 恢复用了 40 分钟」的故障

WAL 类故障的共同点是：**盘没坏、机器没坏、监控没红、进程没崩**。坏的是三件看不见的东西——一个变量的语义、一次 fsync 到底落在哪一层、以及「重启」这件事被算成了几秒。它们都不会在压测里出现，因为压测不拔电。

### 事故一：把 `flush_log_at_trx_commit` 从 1 改成 2，一次市电闪断丢 1,186 单（2025-12-02，某电商订单中心）

`t_order` 库在云盘（ESSD PL1 类，单次 fsync 实测 1.9 ms）上，8.0.36。大促前压测发现下单 TPS 只有 1,250，开发在参数表里看到两个候选：

| 取值 | 参数文档的语义 | 压测实测（同机器、同批次、2,000 并发下单） |
| --- | --- | --- |
| `1`（默认） | 每次提交把 redo 刷到盘才返回 | 1,250 TPS，fsync 1.9 ms/次 |
| `2` | 每次提交把 redo 写进 OS cache，**每秒** fsync 一次 | **11,800 TPS**，fsync 频率 1 次/s |

「吞吐 9.4 倍，代价只是『操作系统挂了才丢』」——文档的措辞让人以为掉电不算操作系统挂。于是 11-20 改成 2，压测通过，上线。

| 时间（2025-12-02） | 事件 | 现场 |
| --- | --- | --- |
| 20:41:02 | 机房双路市电中断，UPS 切换失败，全机柜掉电 | 服务器直接断电，非优雅关机 |
| 20:41:06 | 掉电前最后 4 秒内有 47,200 条已 `COMMIT` 并返回给客户端的事务 | 全在 OS page cache 里 |
| 20:49 | 主机上电 | — |
| 20:52:41 | mysqld 启动并接受连接，崩溃恢复 11 s | redo 只重放了 11 s 的量 |
| 21:20 | 支付网关对账：回调成功但库中无记录 **1,186 单** | 1 s 窗口 × 约 1,186 单/秒 |
| 12-03 复盘 | 人工补单 1,186 单，赔付 + 客诉成本 **3.2 万元**，两名 SRE 通宵 | 与该变量相关的告警数量：0 |

**根因不是「参数设错」，是「这个变量的取值和它承诺的持久性边界不是线性关系」。** `=2` 的语义是：提交时不 fsync，把「是否落盘」交给 OS 每秒一次的 `write back`。它买到的那 9.4 倍吞吐，抵押的正是「最后 1 秒内所有已提交事务」。同一台机器上 `=0` 更糟：连 `write()` 都省了，丢的是 **MySQL 进程**的 1 秒（`innodb_flush_log_at_trx_commit=0` 与 `=2` 的差别只在「进程崩」这一种工况里体现）。

更隐蔽的一层：如果同时开了 `sync_binlog=1`（binlog 每事务刷盘），掉电后**备库比主库多 1 秒的数据**——redo 丢了、binlog 没丢，从库重放出来的行在主库不存在。这类「主从错位」不会被复制延迟监控发现，只会在下一次主从切换时以「数据回退」的形式炸出来。

### 事故二：一个没提交的大事务，把重启时间从 20 秒变成 47 分钟（2025-10-27，某 SaaS 计费库）

8.0.33，buffer pool 512 GB，`innodb_redo_log_capacity` 从默认的 100 MiB 调到 **32 GB**（理由：怕「log file 太小导致写入被 checkpoint 卡住」，这个判断本身是对的）。每晚 03:10 跑「历史积分重算」：一个事务里逐笔 `UPDATE points_ledger`，1,100 万行，中途不提交。

| 时间（2025-10-27） | 事件 | 恢复阶段 | 耗时 |
| --- | --- | --- | --- |
| 03:10 | 批处理开始，单事务，已修改 380 万行 | — | — |
| 04:02:11 | 宿主机内核 panic（网卡驱动），整机重启 | — | — |
| 04:03:40 | mysqld 启动 | 崩溃识别 + 定位 checkpoint | 1.5 min |
| 04:05 | 开始 redo 重放，待重放 redo 28 GB | redo（前滚） | 19 min |
| 04:24 | redo 到末尾，开始回滚未提交事务（1,100 万行） | undo（回滚） | 23 min |
| 04:50:30 | 开始接受连接，总不可用 **47 分钟** | — | — |
| 10-27 复盘 | 支付回调 5xx 41 万次，SLA 违约赔付 **12 万元**；期间切只读从库，从库延迟 3 分钟，仍失败 6.4 万次 | — | — |

三个数串起来才完整：**28 GB redo（能不能少）+ 19 分钟重放（盘有多快）+ 23 分钟回滚（有没有大事务）**。而且恢复期间是**冷 cache**：每一个待重放的页都不在 buffer pool 里，要现从盘随机读进来——所以「恢复」不是「读 28 GB 日志」这么一件顺序 IO 的活，它同时是一场随机 IO 风暴（推导见 §03.8）。

### 事故三：云盘「写成功」了，重启却报页校验失败（2026-01-19，某内容平台）

为了把批量导入从 9,000 行/s 提到 14,000 行/s，DBA 关掉了 `innodb_doublewrite`（理由：云盘有副本、有掉电保护，不需要双写）。云盘侧承诺「已返回的写不会丢」。

| 时间（2026-01-19） | 事件 | 后果 |
| --- | --- | --- |
| 14:07:22 | 云盘后端节点故障，宿主机收到 IO error 并触发实例重启 | 一次 16 KB 页的写被拆成 4 个 4 KB 扇区，只有前 2 个落了盘 |
| 14:09 | mysqld 启动，报 `Database page corruption on disk or a failed file read` / checksum 不匹配 | 起不来（不是「慢」，是「拒启」） |
| 14:35 | `innodb_force_recovery=4` 勉强启动，只能读，不能写 | 业务转只读 |
| 15:10–22:40 | 建临时实例，从备份 + binlog 重放到 14:07，逐表 `CHECKSUM` 比对后切换 | RTO **9.2 小时**，RPO ≈ 0（binlog 完好） |
| 01-20 复盘 | 内容不可读 9.2 小时，广告收入损失约 **84 万元** | 那 5,000 行/s 的「优化」省下的成本：约 0 |

撕裂页（torn page）之所以致命，是因为它**不是丢数据，是数据变成不可解释的字节**：页头说这页有 78 行，页尾的校验和对不上，InnoDB 既不敢用也不敢忽略。doublewrite 的全部价值就在这一个工况里，而它偏偏是「只在掉电时出现」的工况。

三次事故分别对应本篇三条主线：**持久性语义（§03.7）**、**checkpoint 与恢复时长（§03.4、§03.8）**、**torn page 与 doublewrite（§03.5、§03.6）**。

---

## 03.2 一句话定位与唯一真问题

**定位**：WAL + buffer pool + checkpoint 这三件东西合起来，是**「写的性能」与「写的持久性」之间的兑换所**。它有三个「不是」：它不是缓存（缓存丢了大不了回源，WAL 丢了就是数据没了）；它不是备份（它只保证崩溃一致性，不防误删与介质报废）；它不是复制（副本另有一套自己的日志）。

### 唯一真问题

> **数据库敢承诺「已提交不丢」，靠的不是立刻刷数据页，而是「先写日志、再改页、能重放」这个顺序——以及每一次 fsync 真的落到了介质上。**

拆成四句可验证的话：

1. **顺序追加比随机原地写便宜一个到两个数量级**（HDD 是寻道，NAND 是块内 GC）。所以把「随机改页」换成「顺序写日志」，等于用 500 字节的顺序写买下 64 KB 的随机写。
2. **数据页可以晚点写，日志不能**。因为日志是「怎么改」的完整描述，页只是「改完的样子」；只要日志在、页可以慢慢补。
3. **晚点写就必须能重放**，所以需要 LSN 与「页 LSN ≥ 记录 LSN 就跳过」这条幂等规则——它让重放可以任意重复、可以从任意点开始。
4. **「重放」和「不丢」是两件事**：日志写进 OS page cache 也叫「写好了」，只有 `fsync` 落到介质才叫「不丢」。第 4 句是第三个事故的全部内容，也是 90% 的「参数看起来没问题但数据没了」的来源。

**这一个顺序推出的全部结论：**

| 从「先写日志、再改页、能重放」推出的约束 | 推出的机制 / 纪律 |
| --- | --- |
| 日志必须比页更早到介质 | 提交路径上有一次 `fsync`；`=2` 把它挪到后台 → 丢 1 s（§03.7） |
| 日志是顺序追加的 | redo 是环形/分段顺序写；页刷是随机写、能拖就拖（§03.3） |
| 页比日志慢，就必须知道「页到哪儿了」 | LSN 三兄弟：当前 LSN / checkpoint LSN / 页头 LSN（§03.4） |
| 页不在内存也能重放 | 恢复先随机读页再 apply，冷 cache；这决定了恢复时长（§03.8） |
| 页写一半会坏，日志不会（日志有校验和与「写到哪」） | doublewrite / full_page_writes（§03.6） |
| 脏页可无限积压 → redo 必须能回收 | checkpoint 强制刷脏，checkpoint age 上限 = redo 容量（§03.5） |
| 缓冲池越大，可积压脏页越多 | 「内存越大，重启越久」不是 bug，是同一个数的两面（§03.5、§03.8） |
| 一个事务可能横跨很多页 | 恢复要分「前滚（连未提交也重放）」与「回滚（撤掉未提交）」（§03.6） |

### 解剖卡八问：本篇答哪几问

| 八问（→ [0-summary](0-summary.md) §0.4） | 落在哪节 | 一句话答案 |
| --- | --- | --- |
| ① 它承诺什么 | §03.2、§03.9 | 承诺「fsync 返回后的已提交事务，在崩溃与掉电后仍在」；**不承诺**介质报废、不承诺未 fsync 的写、不承诺多副本一致 |
| ② 数据长什么样 | §03.3、§03.4 | 顺序追加的日志段 + 16 KB 定长页 + 每页头一个 LSN |
| ③ 一次读写走哪条路 | §03.3 | 客户端 → 改内存页 → 追加日志 → `write()` 到 page cache → `fsync()` 到介质（4 跳，μs→ms） |
| ④ 并发与一致 | §03.5、`comp/08` | 日志用 latch + 顺序化保证「先日志后页」；组提交是把多个事务塞进一次 fsync |
| ⑤ 扩展维度 | §03.9 | **四选一里只有两条成立**：分片不成立（WAL 天生单机）、副本不成立（副本各写各的日志），只剩**垂直切**（换 NVMe/PLP、减 fsync 次数）与**批处理摊薄**（组提交把 N 个 fsync 摊成 1 个） |
| ⑥ 会在哪崩 | §03.9 | 系统级只有三样：**假活**（fsync 假成功，看着正常实际不持久）、**满**（redo 打满 → 写不进去即卡死）、**慢**（恢复时长超过可用性窗口） |
| ⑦ 看哪几个指标 | §03.11 | 脏页比例、checkpoint age、redo 生成速率、`Innodb_data_pending_fsyncs`、`Innodb_buffer_pool_wait_free` |
| ⑧ 换掉它的信号 | §03.9 | fsync 延迟 > 3 ms 且组提交已用满 → 换本地 NVMe；单机再压不动 → 分片（[eng/36](../eng/36-mysql-limits-sharding.md) §36.4） |

---

## 03.3 WAL 的两条规则与一次写的物理路径

WAL（Write-Ahead Logging）不是「一种日志」，是**两条不可协商的规则**。所有实现（InnoDB redo、PG WAL、RocksDB log、Kafka segment、Flink checkpoint）差异巨大，但这两条一模一样。

### 规则一：日志先于数据页落盘

> 任何数据页的修改，在它对应的日志记录**fsync 到介质**之前，不许写回数据文件。

注意措辞：不是「日志先写」，是「日志先**落盘**」。日志进 OS page cache 不算数——掉电时 page cache 一起消失，而数据页可能已经写了出去，于是「日志丢了、页改了」，这就是**不可恢复的不一致**。这条规则只允许一种违规后的状态：日志比页新（安全，可以重放）。

### 规则二：日志是顺序追加、可重放的

> 只 append、永不原地改旧字节；每条记录带「它属于哪个页的哪个偏移 + 它自己的序号（LSN）」，所以重放可以任意次、从任意点开始。

「可重放」的三个前提缺一不可：**位置信息**（否则不知道往哪 apply）、**序号**（否则不知道是否已 apply，见 §03.4 的幂等规则）、**尾部校验**（日志尾部可能只写了一半，必须能认出「最后一条不完整」并丢弃——InnoDB 用 block 级校验和，PG 用段级 CRC，细节 → `comp/08`）。

### 这两条如何把「随机写」换成「顺序写 + 延迟随机写」

拿一台 1.9 ms/次 fsync 的云盘算一次单行 `UPDATE` 的提交，三种做法：

| 做法 | 提交时必须落的 IO | 每事务 fsync | 随机/顺序 | 单线程 TPS 上限 |
| --- | --- | --- | --- | --- |
| 直接改页：把改过的 4 个页刷盘再返回 | 4 × 16 KB 随机写（64 KB） | 1 次（1.9 ms）+ 4 次随机写（≈1.6 ms） | 全随机 | ≈ 280 |
| **WAL**：追加 500 B + fsync，页延后 | 1 × 500 B 顺序写 | 1 次（1.9 ms） | 顺序 | **≈ 526** |
| WAL + 组提交 32：32 个事务挤进一次 fsync | 32 × 500 B = 16 KB 顺序写（一次 `write`） | 1 次 / 32 事务 | 顺序 | ≈ 12,000（受 CPU 封顶，见 §03.8） |

三件事在这里同时发生，必须分开看：

1. **写变小了**：64 KB → 500 B（128×）。日志只记「第几页偏移多少改成什么」，不记整页。
2. **写变序了**：随机 16 KB × 4 → 顺序 500 B。对 HDD 这是省掉 4 次寻道（5–10 ms/次）；对 NAND 这是省掉「4 KB 小写触发块内 copy-back GC」（写放大 1.0 vs 2–3×）。**顺序写便宜的原因在两种介质上都成立，只是机制不同。**
3. **随机写没有消失，只是被延迟并批量化了**：数据页最终还是随机写回，但由后台一次刷 1,000 页、共享 1 次 fsync（1.9 ms / 1,000 = 1.9 μs/页）。**这就是「延迟随机写」——延迟不是省掉了，是把 fsync 成本从「每事务一次」摊到「每千页一次」。**

代价也在这里：因为页是延后写的，**崩溃时盘上的页可能是几分钟前的样子**，于是必须有 redo 能把它补回来（§03.4）、必须有 checkpoint 控制「最多落后多少」（§03.5）、必须防「页写一半」（§03.6）。三节都是这条规则的账单。

### 一次提交的物理路径（含四层缓存）

**图 1 · 一个 `COMMIT` 落到介质之前经过的四层**

```text
  应用 / 客户端
     │ COMMIT（同步等待）
     ▼
┌──────────────────────────────────────────────────────────────┐
│ ① InnoDB buffer pool（内存）                                  │
│    改页：B+ 树定位 + memcpy        1–10 μs                    │
└───────────────┬──────────────────────────────────────────────┘
                │ 追加 redo record（顺序，0.1–1 μs）
                ▼
┌──────────────────────────────────────────────────────────────┐
│ ② redo log buffer（内存，16 MB）                              │
└───────────────┬──────────────────────────────────────────────┘
                │ write()  1–5 μs —— 只到 OS，掉电即丢
                ▼
┌──────────────────────────────────────────────────────────────┐
│ ③ OS page cache（脏页，4 KB 页框）                            │
└───────────────┬──────────────────────────────────────────────┘
                │ fsync()  ★ 全链路唯一的大头
                │ NVMe 0.06–0.2 ms ｜ 云盘 1–3 ms ｜ 跨 AZ 5–10 ms
                ▼
┌──────────────────────────────────────────────────────────────┐
│ ④ 设备控制器写缓存（易失；有 PLP 电容的型号掉电不丢）          │
└───────────────┬──────────────────────────────────────────────┘
                │ 设备自己调度刷介质（此时才"真的不丢"）
                ▼
        ⑤ 介质：NAND 页 / 云存储三副本
```

**表：一次提交每一跳的耗时与放大**

| 跳 | 做什么 | 量级 | 关键点 |
| --- | --- | --- | --- |
| ① 改 buffer pool | 定位页、改行、记 undo | 1–10 μs | 纯内存；页可能本来就不在池里 → 多一次随机读 0.1–0.5 ms |
| ② 写 log buffer | `memcpy` 500 B | 0.1–1 μs | 顺序；受 `innodb_log_buffer_size`（默认 16 MB）大小影响溢出频率 |
| ③ `write()` | 一次系统调用进 page cache | 1–5 μs | **返回 ≠ 持久**，这一步是绝大多数「以为落盘了」的现场 |
| ④ `fsync()` | 提交给设备 + 等设备确认 | NVMe 0.1 ms / 云盘 1–3 ms / 跨 AZ 5–10 ms | 提交延迟 = 这一跳，其他都可以忽略 |
| ⑤ 页刷（后台） | 随机 16 KB 写 + doublewrite 再 16 KB | 每页 0.2–0.5 ms，摊到 78 行可忽略 | 与提交解耦；崩溃恢复的全部工作量都来自这一跳的"欠账" |

**表：一次单行 UPDATE 的字节账（摊薄后）**

| 落什么 | 字节 | 是否在提交路径上 |
| --- | --- | --- |
| redo 记录（页修改 + undo 修改） | 200–500 B | 是（fsync） |
| undo 记录 | ≈ 350 B | 否（随 undo 页刷） |
| 数据页（16 KB，一页 78 行共享） | 16,384 B / 78 ≈ 210 B/行 | 否（后台刷脏） |
| doublewrite 副本 | 同样 ≈ 210 B/行 | 否（与页刷同批） |
| binlog（ROW 格式，前后像） | 300–800 B | 是（`sync_binlog=1` 时再一次 fsync，细节 → `comp/08`） |
| **合计物理写 / 逻辑写（≈100 B 行）** | **≈ 1.1–1.8 KB** | **≈ 11–18× 名义放大；实测含页分裂等为 5–8×**（口径见 `comp/05` §05.5） |

---

## 03.4 LSN、checkpoint 与「页 LSN ≥ 记录 LSN 就跳过」

### 一个数解释全部：LSN

**LSN（Log Sequence Number）就是「这条日志在日志流里的字节偏移」**，单调递增，全局唯一。它不是时间戳，但可以当时间轴用：LSN 越大 = 发生得越晚。

围绕它有三个数，三个数之间的距离就是你所有的自由度：

**图 2 · 三个 LSN 与两段距离**

```text
LSN 时间轴 ──────────────────────────────────────────────────────────────►
            │                    │              │                 │
            C                    O              F                 N
       checkpoint LSN      最老脏页的修改LSN   已刷到盘的LSN     当前写入LSN
            │◄──── 必须能重放 ────►│
            │◄──────────── checkpoint age ─────────────────────►│
                                │◄── 丢不丢 ──►│
                                （F 之后的日志还在内存/OS 里，掉电即丢）

恢复起点 = C（checkpoint LSN）：C 之前的所有日志，对应的页都保证已落盘
页头 LSN（每页自带）    = 这一页最后一次修改对应的 LSN
可回收的日志空间        = C 之前的部分（覆盖它 = 丢掉重放能力）
```

| 三个数 | 谁在推它 | 它是谁的边界 | 卡住会怎样 |
| --- | --- | --- | --- |
| 当前 LSN `N` | 每一次页修改 | 写入进度的绝对值 | — |
| 已刷盘 LSN `F` | `fsync` 返回 | **持久性边界**：`F` 之后的事务，掉电即丢 | `F` 落后 = 提交延迟高（或 `=2` 时假装不用管） |
| checkpoint LSN `C` | 后台刷脏（最老脏页推进） | **恢复起点**：`N − C` = 待重放量 | `C` 推不动 = redo 涨满 → **写入被强制阻塞**，不是变慢，是停住 |

### 幂等规则：为什么重放可以重复任意次

崩溃点可能落在任何位置，所以恢复时一定会「重放一些已经生效过的记录」——比如某页在崩溃前 200 ms 已刷盘，但 checkpoint 还没推进，于是从 C 开始重放就会再 apply 一遍。这条规则让重复无害：

**图 3 · 「页 LSN ≥ 记录 LSN 就跳过」（每条记录 apply 前的唯一判断）**

```text
redo 流（从 C 开始顺序读）：
   [rec @100][rec @200[p1] ][rec @300[p2] ][rec @400[p1] ]
                              │
页 P1 页头 LSN = 320          │  （P1 在崩溃前已刷到 320，但 C 还在 100）
   │
   ├─ rec@200[p1]：页 LSN 320 ≥ 200 → 跳过（已生效，重复 apply 会错）
   ├─ rec@300[p2]：取页 P2（冷 cache → 随机读 16 KB），页 LSN 90 < 300 → apply，页 LSN := 300
   └─ rec@400[p1]：页 LSN 320 < 400 → apply，页 LSN := 400

三个直接后果：
 ① 重放可以"从 C 开始"而不必知道每页精确进度 —— 状态在页里，不在日志里
 ② 重放可以被打断再重来（幂等）—— 这决定了恢复期间再次崩溃也安全
 ③ 用旧备份 + 新日志做"时间点恢复"时，同样这条规则在保护你
```

这条规则的正式名字叫**幂等重放（idempotent redo）**，它是「日志可以被重复应用」和「恢复可以从中断处继续」的唯一依据。缺了它，引擎就只能在「完全不重放」和「可能应用两次」之间二选一。

`comp/08` 讲 InnoDB 怎么把这条规则塞进 redo block 的字段里；本篇只要求你能画出上面这张图，并说清一件事：**页头 LSN 是"这页的进度"，checkpoint LSN 是"日志能回收的进度"，两者不是同一个东西，混了就会得出"日志可以随便删"的错误结论。**

### 恢复起点为什么在某些版本更慢

恢复起点必须从 redo 里读出 `C`。8.0 的 redo 头里存了 checkpoint 信息，启动时直接取；而更早的实现在某些异常关闭场景下要**扫描全部数据文件取最小页 LSN**——那是一遍全表空间的随机读，几百 GB 的库就是几十分钟。所以「同样的数据量，为什么这台机器恢复要 40 分钟」有一类答案跟日志大小无关，只跟「找起点的方式」有关（版本差异与文件格式 → `comp/08`）。

---

## 03.5 缓冲池：脏页、刷脏与命中率

### 三个链表就是全部

**图 4 · buffer pool 的三个链表（InnoDB；PG/RocksDB 是同构换皮）**

```text
                    ┌─────────────┐
   从盘读入的新页 ──►│  free list  │  空闲页帧，读页时从这取
                    └──────┬──────┘
                           │ 取不到 → 触发 LRU 尾部淘汰（脏的必须先刷）
                           ▼
   ┌──────────────────────────────────────────────────────────┐
   │ LRU list（改进型：midpoint 插入，不是插头部）              │
   │  ┌──────────── young（热，63%） ────────┬── old（冷，37%）──┐│
   │  │ 再被访问 → 提到 young 头             │ 新页插这里（头）  ││
   │  │                                      │ 1 s 内重复访问    ││
   │  │                                      │ 不移入 young      ││
   │  └──────────────────────────────────────┴──────────────────┘│
   │                          ▲ 淘汰从这里（old 尾）取            │
   └──────────────────────────────────────────────────────────┘
                           │ 页被修改 → 挂进 flush list
                           ▼
   ┌──────────────────────────────────────────────────────────┐
   │ flush list（按 oldest_modification LSN 升序）              │
   │  表头那页的 LSN = checkpoint 能推到哪儿 = 恢复起点          │
   │  表尾 = 最新脏页                                           │
   └──────────────────────────────────────────────────────────┘
```

三件事因此变成推论：

- **命中率**由 LRU 的 young/old 比例与工作集大小决定，不由池大小单独决定（`innodb_old_blocks_pct` 默认 37，`innodb_old_blocks_time` 默认 1000 ms：新页先进 old 头，1 秒内再被访问也不提升——这就是为了挡住「全表扫把池洗一遍」）。
- **checkpoint 推进**由 flush list 表头决定，不是由「脏页总比例」决定。所以**「脏页 25% 但 checkpoint age 已 80%」是完全可能的**：脏页少但都是老页，说明刷脏一直没跟上（§03.11 的判据）。
- **预读**（`innodb_read_ahead_threshold` 默认 56，线性预读；`innodb_random_read_ahead` 默认 OFF）把顺序扫变成批量提交，代价是把一堆可能不再用的页塞进 old 区——midpoint 插入策略正是对冲它的。

对照另外两家：PG 用 **clock sweep**（每页一个 0–5 的 `usage_count`，环形扫描减计数，为 0 则淘汰），是「两级近似 LRU 但零链表锁」；RocksDB 的「buffer pool」是 block cache，LRU 分多层（`high_pri_pool_ratio`），因为它没有脏页要刷（LSM 写的是新文件，没有原地改页）。**只有「原地改页」的引擎才需要 flush list 与 doublewrite**——这一句把 B+ 树族和 LSM 族在崩溃恢复上的所有差异都解释完了。

### 刷脏速率：自适应，但上限由你给

InnoDB 的刷脏目标速率是一个分段函数（`innodb_adaptive_flushing` 默认 ON）：

| 区间 | 触发条件 | 速率 |
| --- | --- | --- |
| 被动刷 | `dirty_pct ≤ innodb_max_dirty_pages_pct_lwm`（默认 10%） | 只按 LRU 淘汰被动刷 |
| 线性上升 | `dirty_pct` 在 lwm 与 `innodb_max_dirty_pages_pct`（默认 90%）之间 | 随 `dirty_pct` 线性上升，上限 `io_capacity`（默认 200） |
| 全速 | `dirty_pct` 逼近 max，或 checkpoint age 逼近 redo 容量 | 抬到 `io_capacity_max`（默认 2000） |
| 找活干的速度 | 每一轮扫描多少页 | `innodb_lru_scan_depth`（默认 1024，per buffer pool instance） |

四个数的关系记成一句：**`max_dirty_pages_pct` 决定「平时允许欠多少账」，`io_capacity` 决定「还得起多快」，`redo 容量` 决定「欠账的硬上限」，`lru_scan_depth` 决定「找活干找得多快」。** 把它们调成互相矛盾的组合（大池 + 小 redo + 低 `io_capacity`）就是「平时全绿，一波写入就卡在 flush」的标准配方。

### 为什么「缓冲池越大，崩溃恢复越久」

这段是全篇最反直觉的一条，也是事故二的算术：

| 池大小 | `max_dirty_pages_pct`=90 允许的脏页 | 但真正的硬上限 | 崩溃瞬间待重放 redo | 恢复时长（§03.8 模型） |
| --- | --- | --- | --- | --- |
| 32 GB | 28 GB | redo 8 GB | ≤ 8 GB | 基准 ×1 |
| 128 GB | 115 GB | redo 8 GB | ≤ 8 GB | 基准 ×1（**被 redo 卡住，池再大也没用**） |
| 512 GB | 460 GB | redo 32 GB（团队一定会跟着调大） | ≤ 32 GB | **基准 ×4** |
| 512 GB + redo 128 GB | 460 GB | redo 128 GB | ≤ 128 GB | 基准 ×16 → 十几分钟到 1 小时 |

推导链只有三步：**池大 → 允许积压的脏页多 → 为了让写入不被 checkpoint 卡住，运维必然把 redo 容量调大 → checkpoint age 的硬上限变大 → 崩溃瞬间待重放的 redo 更多 → 恢复更久。** 池大小本身不直接决定恢复时长，它决定的是「你会把 redo 调到多大」，而**恢复时长 ≈ 待重放 redo 字节 / 重放带宽**（§03.8 给出完整模型）。

再加两条同向的力：恢复期间的全部页都不在池里（**冷 cache**，命中率 0），重放每条记录都可能要一次随机读；池越大，通常伴随数据量越大、盘越「便宜」，随机 IOPS 反而更低。

### 刷脏的一次写：torn page 与 doublewrite

刷脏是**随机原地写**，而介质的最小原子单位比页小：一个 16 KB 的 InnoDB 页在 4K 扇区的设备上要拆成 4 次扇区写。掉电发生在第 2 与第 3 个扇区之间，页就变成「前半新、后半旧」——校验和对不上、页内记录链断裂。这就是 **torn page（撕裂页）**，它比丢数据更难处理：丢数据是「少了几行」，撕裂页是「这页不可解释」，引擎只能拒绝启动。

**doublewrite 的做法是「先写一份顺序的副本，再写原地」**：

| 步骤 | 动作 | 代价 |
| --- | --- | --- |
| 1 | 把脏页批量拷进 doublewrite 区（**顺序**写，8.0.20 起可独立成 `#ib_16384_*.dblwr` 文件，`innodb_doublewrite_dir` 可指定位置） | 每页 +16 KB，顺序写便宜 |
| 2 | 对 doublewrite 区 `fsync` | 与页刷同一批，摊薄 |
| 3 | 随机写数据文件各页 | 原本的随机写 |
| 4 | 崩溃后：数据页校验失败 → 从 doublewrite 拷回该页 → 重放 redo | 恢复时多一次顺序读 |

**写放大账**：物理写从 1× 变成约 2×，但吞吐损失通常只有 10–30%，因为第 1 步是顺序的、且 rsync 式批量提交减少了随机 fsync 次数。**这是全书最划算的一笔保险**：用一个已知的 2× 写放大，换掉一个概率低但后果是「起不来」的失效模式。

版本与开关（纪律 6/7）：`innodb_doublewrite` 长期只有 `ON` / `OFF`；8.0.30 起增加 `DETECT_ONLY` 与 `DETECT_AND_RECOVER`（前者只写检测信息，用于「想量代价但不想付双倍」的场景）。8.0.20 起双写页可落在独立文件、可用 `innodb_doublewrite_pages` 控制每次批量。

PG 的等价物是 **`full_page_writes`**（默认 `on`）：checkpoint 之后**第一次**修改某页时，把整页 8 KB 写进 WAL，于是重放时不需要「读旧页 + 打补丁」，直接整页覆盖，天然免疫撕裂页。代价是 **WAL 体积膨胀 2–4×**（后续对同一页的修改只记增量）。所以 PG 的这笔保险买在**日志侧**，InnoDB 买在**数据文件侧**——这是两家最本质的形态差异之一，也解释了为什么 PG 的 `max_wal_size` 要按「脏页周转量」而不是「数据量」来定。

什么时候可以关掉 doublewrite？只有一个判据：**设备承诺 ≥ 页大小的原子写**（部分高端 NVMe、Fusion-io 类的原子写，或云厂商明确写进 SLA 的 16 KB atomic write）。绝大多数云盘只承诺 512 B / 4 KB 原子性——事故三就是这条判断错了。

---

## 03.6 崩溃恢复三阶段（三家对照）

### ARIES 的三阶段

崩溃恢复的教科书答案（ARIES）永远是三段，差别只在于**每一段的物理动作**：

| 阶段 | 做什么 | 读什么 | 写什么 | 期间对外服务 |
| --- | --- | --- | --- | --- |
| **analysis** | 从最近一次 checkpoint 出发，重建「崩溃瞬间哪些事务还活着、哪些页是脏的」 | redo 里的 checkpoint 记录 + 活跃事务表 + 脏页表 | 内存里的两张表 | 不服务 |
| **redo（前滚）** | 从 checkpoint LSN 顺序重放**全部**记录（**含未提交事务**），让页回到崩溃瞬间的样子 | redo 日志（顺序） | 数据页（随机）+ 页头 LSN | 不服务 |
| **undo（回滚）** | 把没有提交标记的事务按撤销链反向抹掉 | undo / 日志里的前像 | 数据页（随机） | 不服务（InnoDB）；PG 无此阶段 |

**为什么 redo 要连未提交事务一起重放？** 因为 redo 是「物理」的：它只记「这页这个偏移改成这些字节」，不区分事务边界；而且 undo 信息本身也是页修改，也必须先被重放回来，否则回滚时找不到前像。**「先全部前滚、再统一回滚」是 ARIES 的宪法**，违反它就会在回滚第一条记录时发现「undo 页本身还没修好」。

### 三家怎么落地（差异在这里，不在三阶段本身）

**图 5 · 同一场崩溃，三种引擎在做什么**

```text
                  MySQL / InnoDB                PostgreSQL                  RocksDB
崩溃前持久结构      页（原地改）+ redo 段         堆表页（原地改）+ WAL 段     WAL + SST（不可变）+ MANIFEST
─────────────────────────────────────────────────────────────────────────────────────────
① analysis        读 redo 头里的 checkpoint    读 pg_control 里的          只读 CURRENT → MANIFEST
                  记录，恢复活跃事务表/脏页表    checkpoint（redo 起点）      找到最后一个完整 version
② redo            从 checkpoint LSN 前滚到末尾  从 checkpoint.redo 前滚      把 WAL 的 WriteBatch
                  页 LSN ≥ 记录 LSN 则跳过      到 WAL 末尾（页级幂等）       顺序重放进 memtable
③ undo            回滚未提交事务（回滚段）      ★ 没有这一段 ★              ★ 没有这一段 ★
                  回滚完才 accept 连接          未提交行靠 xmin 不可见       未提交 batch 没进 SST，
                                                由 autovacuum 慢慢清空间      直接丢弃
─────────────────────────────────────────────────────────────────────────────────────────
恢复后的额外动作    doublewrite 拷回坏页          VACUUM 收垃圾（空间放大）    重建的 memtable 刷成新 SST
```

三家的根本差异只有一句话：**「未提交的修改怎么消失」有三种答案**——InnoDB 主动用 undo 回滚掉；PG 让它在可见性规则下「看不见」，空间留给 VACUUM；RocksDB 因为目标结构不可变，未提交的东西压根没进持久结构，直接丢。

**表：三家的崩溃恢复差异（本篇最该背的表）**

| 维度 | MySQL / InnoDB | PostgreSQL | RocksDB |
| --- | --- | --- | --- |
| 重放单位 | 页修改记录（物理：页号 + 偏移 + 字节） | 整页镜像（`full_page_writes` 首次）或增量记录 | WriteBatch（逻辑/行级） |
| 恢复起点 | checkpoint LSN（在 redo 头里） | `pg_control` 的 `checkpoint.redo` | CURRENT → MANIFEST 的最后一个 version |
| 有 undo 阶段吗 | **有**，且必须做完才对外服务 | **没有**（回滚 = 不可见 + VACUUM 收尾） | **没有**（不可变结构天然丢弃） |
| 幂等靠什么 | 页头 LSN ≥ 记录 LSN 则跳过 | 同类页 LSN 判断 + 整页镜像直接覆盖 | WAL 只重放进空 memtable（天生只放一次） |
| 恢复时长由什么决定 | 待重放 redo / 带宽 + 页随机 IOPS + undo 行数 | 待重放 WAL / 带宽 + 页随机 IOPS（镜像页更大） | WAL 字节 / 带宽（**顺序**，几乎无随机写） |
| 恢复期能否服务 | 不能（含 undo 阶段） | 到达一致点即可，VACUUM 后台做 | 可（先读旧 SST，memtable 刷盘可延后） |
| 撕裂页怎么防 | doublewrite（数据文件侧） | `full_page_writes`（日志侧，WAL 膨胀 2–4×） | 不需要（SST 写完才进 MANIFEST，文件不可变） |
| 恢复的痛点在谁身上 | 大事务（undo 回滚）+ 大 redo | 大 WAL + checkpoint 间隔太长 | 几乎不痛（顺序重放），痛在恢复后那次 flush |

这张表直接推出三条运维结论：**① MySQL 的恢复痛点在「大事务」，所以「一个事务别超 10 万行」不是为了锁，是为了 RTO；② PG 的恢复痛点在 `max_wal_size` 与 checkpoint 间隔，检查点太稀 = WAL 太长 = 恢复越久；③ LSM 族恢复快是因为它只做顺序重放，代价转嫁到读放大与 compaction 上（→ `comp/19`）。**

### 把三阶段写成 40 行

真实引擎的复杂度全在字节格式与并发上，机制本身短得离谱。下面这个 mini WAL 含「顺序追加 + fsync + 截断尾部 + 幂等重放」四件事，缺任何一件都会在某个工况下丢数据或恢复出错：

```go
// mini WAL：append → fsync → 从 checkpoint 重放。只保留"顺序 + 幂等"
type WAL struct {
	f  *os.File // O_CREATE|O_RDWR|O_APPEND 打开，永远只追加
	mu sync.Mutex
}

// Commit：一条记录 + 一次 fsync。返回 nil 才算"已提交不丢"
func (w *WAL) Commit(lsn uint64, pageID uint32, payload []byte) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	rec := make([]byte, 20+len(payload))
	binary.BigEndian.PutUint64(rec[0:], lsn)                 // 序号：幂等的依据
	binary.BigEndian.PutUint32(rec[8:], pageID)               // 位置：往哪 apply
	binary.BigEndian.PutUint32(rec[12:], uint32(len(payload)))
	binary.BigEndian.PutUint32(rec[16:], crc32.ChecksumIEEE(payload))
	copy(rec[20:], payload)
	if _, err := w.f.Write(rec); err != nil {                 // → page cache
		return err
	}
	return w.f.Sync() // → 介质：这一次 fsync 就是 TPS 上限的分母
}

```

重放侧只做两件事：**从 checkpoint 往后顺序扫**，以及**遇到「页 LSN ≥ 记录 LSN」就跳过**。两者合起来就是幂等重放。

```go
// Replay：从 checkpointLSN 顺序扫，遇到"页 LSN ≥ 记录 LSN"就跳过（图 3）
func Replay(w *WAL, store PageStore, checkpointLSN uint64) error {
	if _, err := w.f.Seek(0, io.SeekStart); err != nil {
		return err
	}
	r, hdr := bufio.NewReaderSize(w.f, 1<<20), make([]byte, 20)
	for {
		if _, err := io.ReadFull(r, hdr); err != nil {
			return nil // 尾部半条 = 崩溃点 → 截断丢弃
		}
		lsn, pid := binary.BigEndian.Uint64(hdr[0:]), binary.BigEndian.Uint32(hdr[8:])
		body := make([]byte, binary.BigEndian.Uint32(hdr[12:]))
		if _, err := io.ReadFull(r, body); err != nil {
			return nil
		}
		if lsn <= checkpointLSN { // C 之前：页保证已落盘
			continue
		}
		p := store.Get(pid) // 冷 cache → 这里是一次随机读
		if p.LSN >= lsn {   // ★ 幂等：已生效就跳过
			continue
		}
		p.Apply(body)
		p.LSN = lsn
		store.PutDirty(p)
	}
}
```

代码里两个细节值得盯一眼：`Commit` 里**只有 `Sync()` 返回 nil 才能对客户端说「已提交」**（`Write` 成功什么也不保证，这就是事故一的技术本体）；`Replay` 里 `io.ReadFull` 失败就 `return nil` 而不是报错——**日志尾部有一条写了一半的记录是正常现象，不是损坏**。真实引擎还要在块级加校验和来区分「尾部截断」与「中间空洞」，那是 `comp/08` 的内容。

### 在生产里复现一次崩溃

不要相信「我觉得它不会丢」。用 `kill -9` 做一次带计时的实验，比读十篇文档有用：

```bash
# ① 造负载：单行事务持续写（先关 binlog/复制，隔离变量）
mysql -e "CREATE DATABASE IF NOT EXISTS lab;
CREATE TABLE IF NOT EXISTS lab.t(id BIGINT AUTO_INCREMENT PRIMARY KEY, v CHAR(64)) ENGINE=InnoDB;"
( for i in $(seq 1 200000); do echo "INSERT INTO lab.t(v) VALUES(MD5(RAND()));"; done ) | mysql lab &

# ② 记录现场（每 2 s 一次，另开窗口）
watch -n2 "mysql -N -e \"SHOW GLOBAL STATUS WHERE Variable_name IN
 ('Innodb_buffer_pool_pages_dirty','Innodb_buffer_pool_pages_total',
  'Innodb_data_pending_fsyncs','Innodb_buffer_pool_wait_free');\""

# ③ 硬杀：不要 mysqladmin shutdown（它会从容刷脏，测不出恢复）
kill -9 $(pidof mysqld)          # 虚拟机上可用 virsh destroy，效果等于拔电

# ④ 重启并计时：从进程启动到能 accept 连接 = 恢复总时长（redo + undo）
systemctl start mysqld; t0=$(date +%s)
until mysqladmin ping --silent 2>/dev/null; do sleep 0.5; done
echo "recovery = $(( $(date +%s) - t0 )) s"
grep -E 'Starting crash recovery|Crash recovery finished|ready for connections' /var/log/mysql/error.log | tail -3

# ⑤ 对账：崩溃前最后一次成功 commit 的自增 id 必须还在
mysql -N -e "SELECT MAX(id) FROM lab.t"
```

跑完会看到两件反直觉的事：**恢复耗时与「表有多大」几乎无关，只与 checkpoint age 和盘速有关**；以及把 `innodb_flush_log_at_trx_commit` 改成 2 再重跑一遍，`MAX(id)` 会**倒退**——倒退的那些行，客户端全都收到过成功响应。

---

## 03.7 fsync 与持久性：你以为的落盘不是落盘

### 掉电时四层缓存各丢什么

**表：同一份数据在四层里的「掉电存活率」**

| 层 | 断电 | 进程崩溃 / `kill -9` | 内核 panic | 谁负责 |
| --- | --- | --- | --- | --- |
| ① 进程内 buffer（redo log buffer、WAL buffer） | 丢 | **丢** | 丢 | 你（`*_buffer_size`） |
| ② OS page cache | **丢** | 活（内核还活着） | 丢 | 只有 `fsync` 管用 |
| ③ 设备控制器写缓存（无 PLP） | **丢** | 活 | 活 | 设备固件 / `FUA`、`O_DSYNC` |
| ③′ 设备写缓存（有 PLP 电容） | 活 | 活 | 活 | 硬件承诺 |
| ④ 介质 | 活 | 活 | 活 | 唯一真正的持久层 |

这张表就是「为什么进程崩了不丢、掉电却丢了」的全部答案：**`kill -9` 只清掉第 ① 层，`fsync` 是在打穿第 ②③ 层。** 事故一里 `=2` 丢数据，正是它主动放弃了第 ② → ③ 这一步。

### fsync 家族：每个系统调用到底刷什么

| 调用 | 刷数据 | 刷元数据 | 等设备落盘 | 常见误用 |
| --- | --- | --- | --- | --- |
| `write()` | 否（只进 page cache） | 否 | 否 | 把「write 成功」当成已持久 |
| `fsync(fd)` | 是 | **是**（size、mtime 等全部） | 是 | 忘了它对每个文件都是一次真实 IO |
| `fdatasync(fd)` | 是 | 只刷「影响读取」的元数据（主要是 size） | 是 | 以为它完全不碰元数据（新文件还得 fsync 目录） |
| `O_DSYNC`（open 标志） | 每次 `write()` 同步 | 同上 | 是 | 把批量写拆成 N 次同步 → 吞吐掉一个数量级 |
| `O_DIRECT`（open 标志） | **绕过 page cache** | 无关 | **不保证** | 以为开了 O_DIRECT 就不用 `fsync` |
| `sync_file_range()` | 触发回写 | 否 | **否** | 被当成 `fsync` 的近义替代 |
| `sync()` | 全区回写 | 是 | 是 | 范围不可控，全库一起抖 |

**目录也要 fsync。** 这是「原子 rename 换文件」这套玩法的隐藏成本：新建或 `rename` 一个文件，改的是**父目录的数据**，`fsync(文件fd)` 管不到它。所以「写临时文件 → `fsync` → `rename` → **`fsync` 父目录」是四个动作**，少最后一步，掉电后的现象是**文件凭空消失**（回到 rename 前）——文件系统层面完全合法，业务层面等于丢一次提交。InnoDB、PG、etcd 的日志目录都有自己的目录 `fsync`；自己写「用 rename 做原子发布」的工具（配置下发、快照、checkpoint 文件）时，这一步九成会漏。

### `innodb_flush_method` 的四个取值改的是哪条路

这个参数不改持久性时长，只改**要不要经过第 ② 层**，以及**怎么打穿第 ③ 层**：

| 取值 | 数据文件 | redo 文件 | 作用 / 代价 |
| --- | --- | --- | --- |
| `fsync`（Unix 默认） | 经 page cache，`fsync` 刷 | 经 page cache，`fsync` 刷 | 最稳；内存里双份缓存（浪费） |
| `O_DSYNC` | 经 page cache，`fsync` 刷 | 用 `O_SYNC` 打开，每次 `write` 都同步 | redo 写入退化成逐次同步，与 libaio 语义易混，一般不选 |
| `O_DIRECT` | **绕过 page cache** 直达设备；仍需 `fsync` 打穿设备缓存 | 仍用 `fsync`（redo 是顺序追加，page cache 帮忙合并更划算） | 大池机器的常规选择：省一次内存拷贝与 page cache 占用 |
| `O_DIRECT_NO_FSYNC` | 绕过 page cache，且**跳过**之后的 `fsync` | 同 `O_DIRECT` | 赌「文件系统扩容后元数据安全」，必须按你的文件系统实测（macOS / Windows 上无此值） |

一条纪律：**`O_DIRECT` 不等于持久，它只是「不经过 page cache」。** 第 ③ 层还得靠 `fsync`（或 `FUA`、或带 PLP 的盘）打穿。

### PG 的 `synchronous_commit`：常说的三档（实际五个取值）

| 取值 | 等谁 | 崩溃 / 切换后可能丢什么 | 适用 |
| --- | --- | --- | --- |
| `off` | 谁都不等（WAL 交给后台刷） | 最近若干毫秒~秒级的已提交事务 | 可重算的批量导入、影子表 |
| `local` | 本地 WAL 刷盘 | 本地不丢；主库宕机后**备机可能缺** | 单机库、能从上游重建的库 |
| `on`（默认） | 本地 WAL 刷盘，且若配了同步复制则至少等一个备机**收到** | 本地不丢；切换可能丢「备机未收到」的尾巴 | 绝大多数业务 |
| `remote_write` | 等备机写进它自己的 WAL（未刷盘） | 主备切换不丢；备机断电仍可能丢 | 高可用要求高的 OLTP |
| `remote_apply` | 等备机**应用完**（切换后立刻读得到） | 不丢，且切换后能立刻读到最新 | 一致性读要求严，代价是延迟 ≈ 备机回放 |

「三档」通常指 `off / on / remote_apply` 这条梯度。只记 `on` 不够——**主备切换时丢不丢，取决于 `on` 后面那半句（是否同步复制）**。MySQL 侧的镜像问题是「`flush_log_at_trx_commit` 与 `sync_binlog` 的四种组合分别承诺什么」，它牵涉内部 2PC，全部在 `comp/08`。

### 云盘「99.9999999% 持久性」承诺了什么

把那个数字翻译成人话：**9 个 9 = 单个卷每年丢失概率 10⁻⁷**。你有 1,000 个卷，「每年至少丢一个」的概率 ≈ 1,000 × 10⁻⁷ = 10⁻⁴，即平均一万年一次（假设故障独立）。S3 级的 11 个 9（10⁻¹¹）再把这一步压两个数量级，用途是「敢把备份放上面」，不是「你的数据库不用 fsync」。

它承诺的是**「介质与副本层不丢」**，不承诺下面四件事：

| 你以为它承诺了 | 实际归谁管 |
| --- | --- |
| 「`write()` 返回就算持久」 | **不**。取决于 page cache 与设备缓存，要靠 `fsync` / PLP |
| 「16 KB 写是原子的」 | 多数云盘只承诺 512 B / 4 KB 原子 → torn page 仍要 doublewrite 兜 |
| 「不会丢我这几秒的已提交事务」 | 归你的 `flush_log_at_trx_commit` / `synchronous_commit` |
| 「误删能恢复、逻辑损坏能回退」 | 归备份与快照（→ `comp/42`） |

**「雾写入」（fsync 假成功）** 是这条承诺最容易翻车的角落：虚拟化层 + 网络存储把 `fsync` 转成一次远端确认，正常情况下远端确实落了；但在后端节点故障、宿主机 panic、驱动 bug 这几个工况里，会出现「`fsync` 返回成功、数据其实没到介质」。这类问题**应用层无法发现**，只能靠三重冗余：**页校验和 + doublewrite、定期一致性校验、不信单一副本**。事故三的 9.2 小时就是只留了第一重的前半。

### 亲手量一次 fsync 的真实延迟

TPS 上限的那个分母必须自己测，不能抄别人的博客：

```bash
# A. write() 成功 ≠ 落盘：Dirty 在涨，设备 util 不动
dd if=/dev/zero of=/data/t.bin bs=1M count=2048          # 秒回：全进了 page cache
watch -n1 'grep -E "^(Dirty|Writeback):" /proc/meminfo'   # 看它慢慢回落；掉电此刻全丢

# B. 单次 fsync 的真实延迟（= §03.8 三本账的分母）
fio --name=fs --filename=/data/f.dat --size=1G --rw=write --bs=4k \
    --ioengine=libaio --iodepth=1 --fsync=1 --runtime=30 --time_based \
    --output-format=json | jq '.jobs[0].sync.lat_ns | {min, mean, max}'

# C. 目录 fsync：少这一步，"原子 rename" 会在掉电后回到解放前
python3 - <<'EOF'
import os
fd = os.open("/data/wal.tmp", os.O_CREAT | os.O_WRONLY, 0o644)
os.write(fd, b"x" * 4096); os.fsync(fd); os.close(fd)
os.rename("/data/wal.tmp", "/data/wal.log")
d = os.open("/data", os.O_DIRECTORY); os.fsync(d); os.close(d)  # ← 没这行就靠运气
EOF
```

---

## 03.8 三本账：写放大、吞吐上限与恢复时长

### 账一：WAL 用「多写字节」换「少做随机 IO」

逻辑写只有 100 字节的一行，落盘到底写了多少。这张表把 §03.3 的定性说法变成可比的数：

| 方案 | 每事务落盘字节 | 提交路径上的随机 IO | fsync 次数 | 写放大（相对 100 B 逻辑写） |
| --- | --- | --- | --- | --- |
| 直接刷页（无 WAL） | 4 页 × 16 KB = 64 KB | **4 次随机 16 KB 写** | 1 | **≈ 655×** |
| WAL（页延后刷） | 0.5 KB 日志 + 摊薄页刷 0.21 KB | 0（页刷在后台、成批） | 1 | ≈ 9× |
| WAL + doublewrite | 上面 + 0.21 KB 双写副本 | 0 | 1 | ≈ 13× |
| WAL + dblwr + binlog（ROW） | 上面 + 0.3–0.8 KB binlog | 0 | 1–2 | ≈ 13–21× |

一眼就能看出这笔交易的形状：**字节数只降了 40 倍（64 KB → 1.3 KB），但随机 IO 从 4 次降到 0 次（提交路径上），而 fsync 次数没变。** 这就是为什么「SSD 时代 WAL 还有意义」——SSD 不惩罚寻道，但仍然惩罚**小随机写带来的写放大与队列延迟**，而 fsync 的延迟由设备固件主导，与这一次要写 500 B 还是 64 KB 几乎无关。

空间那本账很短：redo 是环形/分段复用的，稳态占盘 = `innodb_redo_log_capacity`（8.0.30 默认 100 MiB，实际生产按 §03.5 的表调到 1–32 GB）；undo 的账在 `comp/06`（一条 update undo 实际吃 ≈800 B 页空间）。

### 账二：单事务 fsync 延迟 × TPS 上限

推导只有一行：

```
TPS 上限 = 1 / (T_fsync × k)          k = 每事务的 fsync 次数（MySQL 内部 2PC 场景 k=2，细节 → comp/08）
加组提交批量 N：TPS 上限 = min( N / (T_fsync × k) , 1 / T_cpu )
```

代入三档盘：

| 介质 / 拓扑 | 单次 fsync | 每事务 1 次 fsync 的单线程上限 `1/T` | 组提交 32 后摊到每事务 | 理论 `32/T` | 现实上限 = min(理论, CPU 上限 2–5 万) |
| --- | --- | --- | --- | --- | --- |
| 本地 NVMe（带 PLP） | 0.1 ms | **10,000** | 3.1 μs | 320,000 | **3–5 万**（CPU 封顶） |
| 云盘（ESSD PL1 / gp3 级） | 1 ms | **1,000** | 31 μs | 32,000 | **1.5–3 万** |
| 云盘高水位 / 3 ms 档 | 3 ms | **333** | 94 μs | 10,700 | **8,000–1 万** |
| 跨可用区同步写 | 5 ms | **200** | 156 μs | 6,400 | **5,000–6,400** |
| 跨可用区（弱网/跨区） | 10 ms | **100** | 313 μs | 3,200 | **3,200** |

读这张表的三个要点：

1. **左两列是「不改架构就能算出来」的硬上限**。云盘上单线程写到 1,000 TPS 就是天花板，因为每个事务都在等一次 1 ms 的 fsync 往返——这不是数据库慢，是你把事务串行化在了介质延迟上。
2. **组提交把 fsync 从瓶颈变成非瓶颈**：32 条摊到 31 μs，比「一次 SQL 执行」的 20–50 μs 还小。此后瓶颈换成 CPU、锁、网络——所以「上 NVMe 吞吐翻倍」在组提交用满之后往往**不成立**。
3. **跨 AZ 同步写的 5–10 ms 是拓扑决定的**，改参数、换库、加机器全部无效；只能改拓扑（异步复制 + 接受 RPO）或改业务（批量化提交）。

> **量级结论**（非承诺值）。假设清单：① 每事务 1 次 fsync（MySQL 内部 2PC 的 2 次未计，计入则全部减半）；② 32 条批量约 16 KB，仍在一个设备写单元的量级内，fsync 延迟视为常数；③ CPU 每事务执行成本 20–50 μs（简单 INSERT，无锁竞争）；④ 不含网络 RTT、不含 binlog 的第二次 fsync；⑤ 组提交需要 32 个并发事务同时在飞，单连接串行写不适用。换假设可复算：把 ③ 改成 200 μs（复杂事务），NVMe 的现实上限直接降到 5,000。

### 账三：恢复时长 ≈ 待重放 redo / 重放带宽（真正的公式是两段）

第一性公式就是你直觉里的那一条，但它只是**下界**——因为重放不是「读日志」，是「按日志去随机改页」：

```
T_redo   ≈ max( redo_bytes / B_seq ,  page_touches × 2 / IOPS )
page_touches ≈ redo_bytes / R          R = 每页次摊到的 redo 字节（InnoDB 实测区间 0.5–4 KB，此处取 1 KB）
T_undo   ≈ 未提交行数 / 回滚速率        回滚速率 ≈ 2,000–10,000 行/s（NVMe）/ 300–1,000 行/s（慢云盘）
T_recover ≈ T_analysis + T_redo + T_undo
```

代入「checkpoint age 4 GB、顺序读带宽 200 MB/s、3 万行未提交事务」：

| 项 | 推导 | 本地 NVMe（20 万 IOPS） | 云盘（5 万 IOPS） | 入门云盘（3,000 IOPS） |
| --- | --- | --- | --- | --- |
| 顺序读日志（下界） | 4 GB / 200 MB/s | 20 s | 20 s | 20 s |
| 页次 | 4 GB / 1 KB | 400 万页次 | 400 万页次 | 400 万页次 |
| 随机 IO | 400 万 × 2（读页 + 写页） | 800 万 IO | 800 万 IO | 800 万 IO |
| **T_redo** | max(顺序, 随机) | **40 s** | **160 s** | **≈ 2,667 s ≈ 44 min** |
| T_undo（3 万行） | 3 万 / 回滚速率 | 3–15 s | 10–30 s | 100–300 s |
| **合计** | — | **≈ 1 分钟** | **≈ 3–4 分钟** | **≈ 50 分钟** |

**这就是「恢复时长」的真面目：4 GB redo 的物理量是固定的，改的只有盘。** 同一个库、同一份日志，NVMe 上一分钟，入门云盘上五十分钟——而 50 分钟足够触发一次 SLA 违约（事故二）。

用这个模型反推事故二，三个数对得上：

| 事故二的现场 | 用公式算 | 实测 |
| --- | --- | --- |
| 待重放 redo 28 GB，云盘 5 万 IOPS | 2,800 万页次 × 2 / 50,000 = 1,120 s = 19 min | redo 阶段 19 min |
| 未提交 1,100 万行，回滚 8,000 行/s | 1,100 万 / 8,000 = 1,375 s = 23 min | undo 阶段 23 min |
| analysis + 冷 cache 预热 | 1.5 min | 1.5 min |
| **合计** | **≈ 43.5 min** | **47 min（含交接开销）** |

> **量级结论**（非承诺值）。假设清单：① 每页次 = 1 次随机读（页不在池里）+ 1 次随机写；② 每页次摊 1 KB redo（换 0.5 KB 则页次翻倍、时长翻倍；换 4 KB 则缩到 1/4）；③ 恢复期间无其他业务负载争 IO；④ 设备缓存失效（冷启动）；⑤ 顺序读带宽 200 MB/s 是云盘单盘常见值，NVMe 可到 2–3 GB/s，但顺序读从来不是瓶颈。**换假设可复算：把 R 从 1 KB 改成 4 KB，NVMe 上的 40 s 变成 10 s——但这不会改变「盘速决定恢复时长」的结论。**

### 一条阈值线：脏页比例 ↔ checkpoint 滞后

三个数其实是同一个东西的三个投影：**脏页比例**（池里的账）、**checkpoint age**（日志里的账）、`Innodb_buffer_pool_wait_free` 的增速（阻塞的账）。判据只需要一行：

```
checkpoint age / redo 容量  ≈  最老脏页的年龄 / 能容忍的最长欠账时间
```

| 脏页比例 | checkpoint age / redo 容量 | 状态 | 该做什么 |
| --- | --- | --- | --- |
| < 10% | < 25% | 健康，突发余量充足 | 不动 |
| 10–30% | 25–50% | 正常 | 记录峰值，别把 `max_dirty_pages_pct` 调高 |
| **30–70%** | **50–85%** | **刷脏跟不上写入——这是预警线，不是故障** | 抬 `io_capacity` / 扩 redo / 拆大事务 / 降批量 |
| > 70% | > 85% | 写入即将被强制阻塞（`wait_free` 开始计） | 立刻降写入或扩盘，准备降级 |
| 接近 100% | = 100% | **停止写入**（不是变慢，是卡死） | 事故；只能等它刷完 |

**为什么「30% / 75%」是同一条线的两个投影**：脏页比例高但 age 低（说明都是新脏页、刷得动）不危险；脏页比例低但 age 高（说明有几页老脏页卡在 flush list 表头）非常危险——所以**报警要报 `max(脏页比例, age/容量)`，不能只看脏页比例**。这条也正是 §03.11 里最容易测错的指标。

---

## 03.9 架构边界：WAL 能保证什么、不能保证什么

| 需求 | WAL 这一层能不能 | 该靠什么 |
| --- | --- | --- |
| 「`COMMIT` 返回后，进程被 `kill -9`、机器 panic，数据还在」 | **能**（这就是它存在的理由） | WAL + `fsync`（`flush_log_at_trx_commit=1` / `synchronous_commit=on`） |
| 「掉电（供电中断）后，已提交的还在」 | 能，**前提是 fsync 真的打穿了设备缓存** | 带 PLP 的盘 / 云盘 SLA 明确承诺；否则只能假设会丢最后 1 秒 |
| 「页不会变成半新半旧」 | 不能靠 WAL 单独保证 | doublewrite / `full_page_writes`（§03.5） |
| 「磁盘物理损坏 / 整卷丢失」 | **不能**：日志和数据在同一个卷上一起没 | 副本、备份、跨 AZ（→ `comp/10`、`comp/42`） |
| 「误删 / 写错数据能回退」 | **不能**：WAL 忠实重放你的错误 | 备份 + PITR（时间点恢复）+ 逻辑删除 |
| 「主从一致」 | **不能**：复制是另一套日志与位点 | 半同步 / MGR（→ `comp/10`） |
| 「跨库原子」 | 不能 | 分布式事务（→ `comp/43`） |
| 「恢复很快」 | **不保证**：WAL 越大恢复越久 | checkpoint 频率 / redo 容量 / 盘速 / 拆大事务（§03.8） |
| 「写吞吐高」 | 能，但受一次 fsync 延迟封顶 | 组提交、批量化、换 NVMe（§03.8 账二） |
| 「只读副本、水平扩读」 | 不涉及 | 加副本（→ `comp/10`） |

### 扩展维度（八问 ⑤）：只有两条路成立

| 候选 | 成立吗 | 为什么 |
| --- | --- | --- |
| 分片 | **不成立** | WAL 天生是单机顺序流；分片是「把库切成 N 个独立的 WAL」，解决的是容量与总吞吐，不是单条日志的延迟 |
| 副本 | **不成立** | 每个副本写自己的日志、重放自己的一份；副本换的是「不丢」和「读扩展」，不是「更快提交」 |
| **垂直切** | **成立** | 换带 PLP 的 NVMe（0.1 ms vs 3 ms = 30×），或用 `O_DIRECT` 省掉一次内存拷贝；这是唯一能直接抬高单事务 fsync 上限的动作 |
| **批处理摊薄** | **成立** | 组提交、`binlog_group_commit_sync_delay`、批量 INSERT：把 N 个 fsync 摊成 1 个（§03.8 账二，细节 → `comp/08`） |

换掉它的信号（与算式一一对应）：`T_fsync` 长期 > 3 ms 且组提交已用满仍不够 → 换本地 NVMe；待重放 redo 使 RTO 超过可用性窗口（恢复 > 10 分钟）→ 缩小 redo 容量 + 拆大事务 + 提高 checkpoint 频率；单机 fsync 上限 × 组提交收益 < 业务增长 → 只能分片（[eng/36](../eng/36-mysql-limits-sharding.md) §36.4），此时 WAL 问题变成「N 份 WAL 的运维问题」。

### 系统级失效（八问 ⑥）：只有三样

| 失效类别 | 在这里长什么样 | 与其他类的区别 |
| --- | --- | --- |
| **假活**（看着正常，实际不持久） | `fsync` 假成功、`=2` 参数、`O_DIRECT` 不开 fsync、云盘雾写：进程健康、监控全绿、**数据已经不保** | 不是「慢」也不是「满」，是最贵的一类，因为没有任何指标会红 |
| **满**（写不进去） | redo 打满 → checkpoint 推不动 → 所有写入等刷脏（`wait_free`）；undo 写满盘 → 主库只读 | 表现为「写入卡死」，不是「写入变慢」 |
| **慢**（RTO 违约） | 恢复 47 分钟；重启期间完全不可用，且从库可能追不上 | 与「满」的区别：慢的时候还能进来，满的时候进不来 |

索引建错、参数调错、`SELECT *` 这些**使用级**问题归 §03.10 的坑清单，两节不互相稀释。

---

## 03.10 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到物理形态） | 定位 | 修复 | 防复发 |
| --- | --- | --- | --- | --- |
| 掉电后丢最后约 1 秒的已提交订单，重启恢复只花了 11 秒 | `innodb_flush_log_at_trx_commit=2`：提交只写 OS page cache，`fsync` 交给每秒一次的后台（§03.7 表） | `SHOW VARIABLES LIKE 'innodb_flush_log_at_trx_commit'`；对比 binlog 里的事务数与表里实际行数 | 改回 `1`；若确实要吞吐，改用组提交（§03.8 账二）而不是降持久性 | 参数进「持久性相关配置基线」，改动必须在评审里写明 RPO；`=2`/`=0` 只允许出现在可重算的库上 |
| 云盘上偶发页校验失败、实例起不来（`Database page corruption` / checksum 不匹配） | 设备只保证 512 B / 4 KB 原子写 + 关闭了 doublewrite → 撕裂页（事故三） | 启动 error log 里的页号；`innodb_force_recovery` 逐级试；`CHECK TABLE` | `innodb_doublewrite=ON` 重启；从 backup + binlog 重建坏页所在表 | 建库模板强制 `ON`；确需关闭时必须拿到「16 KB 原子写」的书面承诺 |
| 容器里数据库延迟周期性抖动，`Innodb_buffer_pool_wait_free` 尖刺 | 容器 `--memory` 把 page cache 也算进限额 → 内核在限额压力下回收脏页，回收变成同步回写（刷脏抖动） | 容器内 `cat /proc/meminfo` 的 `Dirty` 与 cgroup `memory.stat` 的 `file_dirty`；对照宿主机 `iostat` | 内存限额给到「缓冲池 + 2–4 GB」；用 `memory.high` 软限制代替硬限制；`innodb_io_capacity` 按真实盘调 | 容器化数据库的 request/limit 必须写进部署模板，禁止与业务 Pod 混部争 page cache |
| 一次重启恢复 40 分钟，SLA 违约 | 缓冲池大 → redo 容量跟着调大 → checkpoint age 上限大 → 待重放多；冷 cache 下全是随机 IO（§03.5、§03.8 账三） | 崩溃前 `redo 容量`、`checkpoint age`、盘 IOPS 三个数；error log 里 apply batch 的进度与耗时 | 缩 redo 容量到「能容忍的 checkpoint age」；拆大事务；换 IOPS 更高的盘 | 把「kill -9 → 计时 → 起服务」做成每季度演练，RTO 数字进发布门禁 |
| 批量任务跑到一半，**所有写入卡死**（不是变慢，是超时） | 单事务太大 → checkpoint 推不动 → redo 打满 → 新写入必须等 `fsync` 与刷脏腾空间（满，不是慢） | redo 使用率 100%；`Innodb_buffer_pool_wait_free` 单调涨；`information_schema.innodb_trx` 里一个 `trx_rows_modified` 巨大的事务 | `KILL` 该事务（回滚要时间，见 §03.8 的 T_undo）；之后按 1,000–10,000 行一批提交 | 批处理框架强制「chunk + commit」，单事务行数上线校验；`max_binlog_size` 与 `redo` 容量一起评审 |
| PG 侧为省 WAL 体积关掉 `full_page_writes`，一次掉电后报 `invalid page` / 需要从备份恢复 | 8 KB 页 vs 512 B 扇区：第一次修改某页时没有整页镜像，掉电落在扇区之间 → 撕裂页（§03.5 末） | 启动日志的页号；`pg_controldata`；`amcheck` 校验 | 改回 `full_page_writes=on`，`initdb` 后重新校验 | 该参数不许在 OLTP 库上关；`max_wal_size` 靠容量解决，不靠关保险 |
| 跨 AZ 部署后写 TPS 只有同区的一半，加机器无效 | 同步写的 fsync 延迟由 RTT 决定（5–10 ms），是拓扑常数（§03.8 账二） | `fio --fsync=1` 在两地实测；对比 `fsync` 延迟与业务 P99 | 改组提交/批量化；或改异步复制并接受 RPO（→ `comp/10`） | 部署拓扑评审必须带「fsync 延迟预算」；跨区只读副本除外 |
| 备份/DDL/全表扫之后，业务 P99 抖动十几分钟 | 大范围扫把 buffer pool 洗一遍（命中率跌）+ 产生大量脏页（刷脏追不上），两条链同时触发 | `Innodb_buffer_pool_reads` 突增、`pages_dirty` 上升、`age` 上升 | 限制批量读的并发与条数；`innodb_old_blocks_time` 保热点；错峰 | 报表/备份走只读副本；给全表扫类 SQL 加超时与限流 |

---

## 03.11 观测：指标、拐点与告警

**表：五条主干指标（每条都标版本与开关前提；名字落地前用 `SHOW GLOBAL STATUS LIKE 'Innodb_%'` 核一遍你所在小版本的实际输出）**

| 指标 | 算法 | 拐点 / 告警线 | 版本与开关前提 |
| --- | --- | --- | --- |
| 脏页比例 | `Innodb_buffer_pool_pages_dirty / Innodb_buffer_pool_pages_total` | > 30% 预警，> 70% 告急（§03.8 阈值表） | MySQL 8.0 全版本可用，无需开关；多 buffer pool instance 时以 `information_schema.INNODB_BUFFER_POOL_STATS` 交叉核对分实例值 |
| checkpoint age | `Innodb_redo_log_current_lsn − Innodb_redo_log_checkpoint_lsn` | > 50% redo 容量预警，> 85% 告急 | **8.0.30+** 才有这组 LSN 状态变量（配合 `innodb_redo_log_capacity`）；8.0.30 之前 / 5.7 用 `SHOW ENGINE INNODB STATUS` 的 `Log sequence number` − `Last checkpoint at`，需由 exporter 解析文本（不是默认采集项） |
| redo 生成速率 | `Δ current_lsn / Δt` | 持续 > 刷脏能力的 70%；把「生成速率 × 预期恢复时长」当 RTO 预算 | 同上一行的版本前提；8.0.30 之前用 `Innodb_os_log_written` 差分（该系列变量的口径在小版本间有调整，跨版本做面板前先对一遍） |
| 刷脏阻塞 | `Innodb_buffer_pool_wait_free`（累计次数/秒） | **非 0 就是已经在卡**；连续 3 个采样 > 0 立即告警 | 8.0 全版本；只有 buffer pool 满且 LRU 尾部全脏时才增长，所以「空池」环境永远是 0（别拿它证明健康） |
| 提交同步压力 | `Innodb_data_pending_fsyncs`（当前排队 fsync 数）、`Innodb_data_fsyncs`（累计） | `pending` 持续 > 0 且随负载线性涨 → 盘到顶；`Δfsyncs/Δt` ≈ 提交 TPS 说明组提交没生效 | 8.0 全版本；组提交是否生效要看「fsync 次数是否显著小于事务数」，这一条必须与业务 TPS 同图看 |
| 恢复耗时 | 进程启动 → `mysqld` 打 `ready for connections` 的时间差；中途用 error log 的 apply batch 行（`Starting an apply batch of log records to the database...` / `Apply batch completed!`）看进度与速率 | 超过 RTO 预算的 50% 即演练整改 | 8.0 的 error log 措辞在小版本间有调整，落地前 grep 一次你所在版本的日志模板；无官方恢复时长指标，只能靠日志时间戳 |
| PG checkpoint | `checkpoints_req / checkpoints_timed`、`checkpoint_write_time`、`checkpoint_sync_time`、`buffers_checkpoint` | `checkpoints_req` 占比 > 30% → checkpoint 太频繁或 `max_wal_size` 太小；`sync_time` 上升 → 盘刷不动 | **PG ≤ 16**：在 `pg_stat_bgwriter`（含 `maxwritten_clean`）；**PG 17+**：拆到 `pg_stat_checkpointer`（`num_timed`/`num_requested`/`write_time`/`sync_time`/`buffers_written`），`pg_stat_bgwriter` 里那几个字段已移除——照旧名字抓会在 17+ 上拿到空值 |
| RocksDB WAL | `WAL_FILE_SYNCED`、`WAL_FILE_BYTES`、`STALL_MICROS`（statistics 计数） | `WAL_FILE_SYNCED` 次数 ≪ 写次数 → 用了 `manual_wal_flush` 或异步 WAL（等价于 `=2` 的语义） | RocksDB 9.x：需在 `Options` 里开 `statistics` 并用 `stats_dump_period_sec` 或 `getstatistics` 导出；`WriteOptions::sync=false`（默认）本就**不保证**落盘——这条默认值是最容易误判的一项 |

### 巡检一：MySQL 侧四个数一次抓齐

```sql
-- ① 三个 LSN：待重放量 = current - checkpoint（8.0.30+）
SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.global_status
 WHERE VARIABLE_NAME IN ('Innodb_redo_log_current_lsn','Innodb_redo_log_checkpoint_lsn',
                         'Innodb_redo_log_flushed_to_disk_lsn','Innodb_redo_log_capacity_resized');

-- ② 脏页比例（多 instance 时 SUM 才是全池）
SELECT ROUND(100 * SUM(IF(VARIABLE_NAME='Innodb_buffer_pool_pages_dirty',VARIABLE_VALUE,0))
                 / SUM(IF(VARIABLE_NAME='Innodb_buffer_pool_pages_total',VARIABLE_VALUE,1)),1) AS dirty_pct
  FROM performance_schema.global_status
 WHERE VARIABLE_NAME LIKE 'Innodb_buffer_pool_pages_%';

-- ③ 刷脏是否已经卡住 + fsync 压力
SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.global_status
 WHERE VARIABLE_NAME IN ('Innodb_buffer_pool_wait_free','Innodb_data_pending_fsyncs',
                         'Innodb_data_fsyncs','Innodb_buffer_pool_pages_flushed');

-- ④ 谁在制造 redo / 拦住 checkpoint：按修改行数排的长事务
SELECT trx_id, trx_state, trx_rows_modified,
       TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS secs,
       LEFT(COALESCE(trx_query,''), 60) AS q
  FROM information_schema.innodb_trx
 ORDER BY trx_rows_modified DESC LIMIT 5;
```

8.0.30 之前把 ① 换成 `SHOW ENGINE INNODB STATUS\G`，取 `Log sequence number` 与 `Last checkpoint at` 两行相减；② ③ ④ 不变。

### 巡检二：PG 侧 checkpoint 与 WAL 距离

```sql
-- PG ≤ 16：checkpoint 指标在 pg_stat_bgwriter
SELECT checkpoints_timed, checkpoints_req, checkpoint_write_time, checkpoint_sync_time,
       buffers_checkpoint, buffers_clean, maxwritten_clean
  FROM pg_stat_bgwriter;

-- PG 17+：拆到 pg_stat_checkpointer（上面那几个字段已从这里移除）
SELECT num_timed, num_requested, write_time, sync_time, buffers_written
  FROM pg_stat_checkpointer;

-- 两个版本都可用：当前 WAL 位置与最近 checkpoint 的距离 = "待重放量"的量级
SELECT pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), redo_lsn)) AS since_checkpoint,
       checkpoint_time, full_page_writes
  FROM pg_control_checkpoint();
```

`since_checkpoint` 就是 PG 版的 checkpoint age：它的上限由 `max_wal_size` 决定，而它直接决定「崩溃后要重放多少」。**这一条建议直接画成图**，比 `checkpoints_timed` 更能提前十分钟预警。

---

## 03.12 一页速记

| 记什么 | 具体数字 / 结论 |
| --- | --- |
| 两条规则 | ①日志先于页落盘（要 fsync，不是 write）②顺序追加、可重放（LSN + 尾部校验） |
| 一次提交 4 跳 | 改页 1–10 μs → 日志 buffer 0.1–1 μs → `write()` 1–5 μs → **`fsync` 0.1 ms / 1–3 ms / 5–10 ms** |
| 三个 LSN | 当前（写进度）、checkpoint（恢复起点 / 日志可回收点）、页头（这页的进度） |
| 幂等规则 | **页 LSN ≥ 记录 LSN → 跳过**；这条规则让重放可重复、可从任意点开始 |
| 三链表 | free list / 改进型 LRU（old 37%、1 s 防洗池）/ **flush list 按 LSN 升序，表头决定 checkpoint** |
| 刷脏参数 | `max_dirty_pages_pct` 90、`lwm` 10、`io_capacity` 200、`io_capacity_max` 2000、`lru_scan_depth` 1024 |
| 恢复三段 | analysis（找 checkpoint 与活跃事务）→ redo（**含未提交也重放**）→ undo（回滚未提交） |
| 三家差异 | InnoDB **有 undo**；PG **没 undo**（不可见 + VACUUM）；RocksDB **没 undo**（不可变结构丢弃） |
| 撕裂页 | 16 KB 页 vs 4 KB 扇区 → doublewrite（数据侧，≈2× 写放大）；PG 靠 `full_page_writes`（日志侧，WAL ×2–4） |
| TPS 上限 | `1/(T_fsync×k)`；0.1 ms→1 万，1 ms→1 千，3 ms→333，5 ms→200；组提交 N 摊薄 = `N/(T_fsync×k)`，封顶在 CPU |
| 恢复时长 | `max(redo/B_seq, 页次×2/IOPS) + undo行数/回滚速率`；4 GB redo：NVMe 1 min、入门云盘 50 min |
| 阈值线 | 脏页 30% / age 75% 是同一条线；`wait_free` 非 0 = 已经在卡 |
| 一句话 | **「已提交不丢」买的是顺序 + fsync，不是「数据页已经写好了」。** |

**如果只记五句：**

1. **先写日志、再改页、能重放**——这个顺序把随机写换成了顺序写 + 延迟随机写，代价是崩溃时页可能是旧的样子。
2. **`write()` 不等于落盘**，`fsync()` 才是；`flush_log_at_trx_commit=2` 就是主动放弃这一步（丢 1 秒）。
3. **页 LSN ≥ 记录 LSN 就跳过**，这一条让重放幂等，于是恢复可以从 checkpoint 一路重来。
4. **恢复时长 ≈ 待重放 redo / 重放带宽，但真正的瓶颈是随机页 IOPS**；缓冲池调大 → redo 调大 → 重启更慢，这是同一个决策的两面。
5. **叶子页可以晚写，日志不能；页可以只写一半，日志有校验和**——所以 doublewrite / `full_page_writes` 是这笔账的保险费，不是可选项。

---

## 03.13 自检题（5 题）

**Q1.** 一台云盘机器实测单次 `fsync` 为 1.9 ms，事务每次提交需要 1 次 fsync。①单线程写 TPS 上限是多少？②加组提交批量 64 后理论上是多少？③为什么实际跑不到，且继续把批量加到 256 也不会更快？

<details><summary>答案</summary>

① `TPS = 1 / 1.9 ms ≈ 526`。这不是数据库的能力，是「每个事务都串行等一次介质往返」的直接算术结果。

② 组提交把 64 个事务塞进一次 fsync：每事务摊 `1.9 ms / 64 ≈ 30 μs`，理论 `1/30 μs ≈ 33,700 TPS`。

③ 因为瓶颈换人了：① CPU 每事务执行（解析 + 执行 + 行锁 + undo）约 20–50 μs，64 条批量的 fsync 摊销已经和它同量级，于是 `min(33,700, 1/30 μs)` 里的 CPU 项开始封顶；② 组提交要求 64 个事务**同时在飞**，受连接数、锁冲突、`binlog_group_commit_sync_delay` 影响，实际并发不到 64；③ 每批写入从 64 × 500 B = 32 KB 继续涨到 128 KB 后会开始触发多次设备写/多个块，fsync 延迟不再恒定——「批量无限摊薄」这个假设在第 ③ 点失效。正确做法：先确认 `Δfsyncs/Δt ≪ TPS`（组提交真的生效），再谈加批量。
</details>

**Q2.** 崩溃前 `checkpoint age = 8 GB`，重放时每页次摊到 2 KB redo，盘是 5 万随机 IOPS 的云盘、顺序读 200 MB/s，未提交事务 0。估算恢复里的 redo 阶段耗时，并说明为什么「8 GB ÷ 200 MB/s = 40 秒」是错的。

<details><summary>答案</summary>

页次 = 8 GB / 2 KB = 400 万页次；每页次 1 读 + 1 写 = 800 万 IO；`800 万 / 50,000 = 160 s ≈ 2.7 分钟`。

`T_redo = max(8 GB / 200 MB/s, 160 s) = max(40 s, 160 s) = 160 s`。

「40 秒」错在把重放当成了**顺序读日志**。重放的真实动作是：读一条记录 → 找到它属于哪一页 → **这页在冷 cache 里 ⇒ 随机读 16 KB** → 改 → 记为脏（稍后**随机写**）。日志是顺序的，页是随机的；只要盘不是「随机和顺序一样快」，40 秒就只是下界。这也解释了一个常见误判：「我的日志只有 8 GB，凭什么恢复 3 分钟」——日志大小只决定页次，盘决定每个页次多贵。
</details>

**Q3.** 某库同时设置 `innodb_flush_log_at_trx_commit=2` 与 `sync_binlog=1`。掉电重启后，为什么可能出现「从库有、主库没有」的行？这对主从切换意味着什么？

<details><summary>答案</summary>

redo 是「事务在存储引擎侧生效」的日志，binlog 是「事务在复制侧生效」的日志，两者是**两个独立的 fsync 对象**。

`=2` 让 redo 每秒才 fsync 一次 → 掉电丢最后 ≤1 秒的 redo（数据页层面的修改没了）；`sync_binlog=1` 让每个事务的 binlog 都刷了盘 → binlog 里这些事务**还在**。重启后主库少了这些行，但从库早已按 binlog 重放出了这些行，于是出现「从库比主库多」。

后果不是「延迟」而是「切换后数据回退」：任何以从库为源做切换/恢复的动作，都会把一个**比主库更新的状态**当成权威，或者反过来在主库上重复应用 binlog 时冲突。正确姿势：把 redo 与 binlog 的持久性绑在同一档（`=1` + `sync_binlog=1`），或接受两者一起丢；两者混搭是最坏组合。内部 2PC 如何保证「两份日志要么都算提交、要么都不算」，见 `comp/08`。
</details>

**Q4.** doublewrite 让物理写放大约 2×，为什么实测吞吐只掉 10–30%？给出三条理由，并说出什么设备上可以关掉它。

<details><summary>答案</summary>

① **它是顺序写**：doublewrite 区是连续批量写，而原来的随机 16 KB 页写才是贵的那个；顺序写的吞吐远高于随机写，所以多出来的 1× 字节很便宜。
② **它被批量摊薄**：一次刷脏会把多个页一起拷进 doublewrite、共享一次 fsync；fsync 次数没有翻倍（这是最关键的——吞吐主要由 fsync 次数与随机 IO 决定，不由字节数决定）。
③ **它把随机写变成了「1 次顺序写 + 1 次随机写」的可调度模式**：设备能把顺序部分合并成大块，队列深度利用率更高；且恢复期只需顺序读回坏页，不用全库扫描。

可以关的唯一条件：**设备承诺 ≥ 页大小的原子写**（16 KB atomic write，写在硬件/云厂商 SLA 里），此时「页写一半」这个失效模式在物理上不存在。云盘绝大多数只承诺 512 B / 4 KB 原子性，所以默认必须开。
</details>

**Q5.** PG 没有 undo 阶段，那崩溃瞬间「已修改但未提交」的大量行难道不用处理吗？它不占空间、不影响读吗？和 InnoDB 的差别落在哪本账上？

<details><summary>答案</summary>

**逻辑上不用处理，物理上要还债。**

① 未提交事务改过的行留在堆表页里，但这些行的 `xmin`（写入者事务号）属于一个**没有 commit 记录**的事务 → 可见性判定直接判为不可见（`comp/06` 讲同一套判定的 InnoDB 版本）。所以「读不到」这件事天然成立，不需要回滚动作。

② 空间不回收：这些死行会一直占着页，直到 VACUUM 清掉。PG 也没有「原地回滚」的机制，VACUUM 是唯一的回收器——这就是 PG 的**空间放大**账（表膨胀、需要 `autovacuum` 调优、长事务会让回收边界推不动，`comp/11`）。

③ 恢复期的差别：PG 崩溃恢复只需 redo 到一致点就能开服务（VACUUM 后台慢慢还），所以它的「不可用窗口」通常比 InnoDB 短；InnoDB 必须把 undo 回滚**做完**才 accept 连接（事故二的 23 分钟）。

一句话：**InnoDB 用「恢复期一次性回滚」买了「盘上不留垃圾」；PG 用「盘上留垃圾 + 后台 VACUUM」买了「恢复快」。** 两家的选择分别落在「恢复时长」与「空间放大」两本账上，没有免费的。
</details>

---

## 03.14 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| InnoDB 页布局、8,126 字节行上限、页分裂与写放大口径 | [comp/05](05-innodb-page-and-row.md) §05.3、§05.5 |
| redo / undo / binlog 三份日志、内部两阶段提交与组提交的阶段细节 | `comp/08` |
| undo 版本链、Read View 与长事务如何撑爆 undo（恢复期 undo 回滚的另一面） | [comp/06](06-mvcc-and-isolation-levels.md) §06.3、§06.7 |
| 复制延迟、半同步与 `sync_binlog` 在主备切换时承诺什么 | `comp/10` |
| PG 的堆表、xmin/xmax 与 VACUUM（为什么它没有 undo 阶段） | `comp/11` |
| RocksDB 的 WAL、MANIFEST 与 memtable 重建（LSM 为什么恢复快） | `comp/19` |
| 云盘与对象存储的持久性承诺、IOPS/吞吐模型 | `comp/42` |
| 分布式事务为什么不能靠「两份 WAL」解决 | `comp/43` |
| 存储引擎三形态在放大账上的取舍（本篇的地基篇） | `comp/02` |
| 存储与索引形态决策树、读/写路径形态 | [architect/03](../architect/03-storage-and-index-shape.md) |
| 单机写入红线、分片拐点与换挡表 | [eng/36](../eng/36-mysql-limits-sharding.md) §36.4 |
| 全书总纲、解剖卡八问与写作纪律 | [0-summary](0-summary.md) §0.4、§0.7 |
