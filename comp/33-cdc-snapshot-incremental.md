# 33. CDC：Debezium / Canal 与「快照 + 增量」衔接

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 消息、流与定时 ｜ **编号**：33 / 53
> **本篇定位**：全仓唯一一处把「全量快照与增量流在哪一个位点上缝合」拆到 offset 级的地方。binlog 本身的 redo/undo 关系只给一句结论（→ comp/08），Kafka 侧的段、压缩、保留策略全归 [28 Kafka 的日志](28-kafka-log-segments.md)，「不丢」那一组参数归 [29 复制与一致性](29-kafka-replication-isr.md)，投递语义与消费幂等归 [eng/24 投递语义](../eng/24-delivery-semantics.md)，埋点管道的整体形态归 → architect/11，湖表侧的落库形态归 → architect/21 —— 本篇一个字不重写这些。它只回答一个问题：**为什么「解析 binlog」这件事三方开源工具都做得很好，而 CDC 依然在线上反复出事？**
> **读法**：救火读 §33.1 的三起事故（三起正好对应三类典型翻车）、§33.7 的止血顺序、§33.10 的坑清单；做架构决策读 §33.2 的两种采集对照、§33.6 的衔接方案对照表、§33.9 的边界表；被追问「CDC 到底保证什么」读 §33.9 与 §33.4 的重复来源表；做容量方案直接抄 §33.8 的三本账（每个都带乘法链、假设清单与量级标记）。只想拿结论的读 §33.12。

---

## 33.1 引子：三起「快照与增量接不上」的事故

CDC 的故障有一个共同形状：**connector 是绿的、Kafka 有数据、下游也不报错**。它表现成「数仓里某些行永远停在旧值」「某个 consumer 卡在同一个 offset」或者「connector 突然要重做全量」，而根因都不在「解析 binlog 的代码」里 —— 在「快照与增量怎么接」和「schema 变了谁先知道」这两件事上。

### 事故一：全量用 `mysqldump`、增量用 Debezium，43 分钟的订单在数仓里从不存在（2026-01-19）

某交易域 `order_db`，主库 12 张表、表空间合计 1.1 TB，核心表 `order_main` 2.4 亿行。团队为了「快照别影响主库」，把 Debezium 的 `snapshot.mode` 设成 `never`（跳过自带快照），另让 DBA 用 `mysqldump --single-transaction` 导一份全量给数仓打底，Debezium 只负责增量。

问题出在两条管道**各自取了自己的位点**，而且**谁也没比谁早**：

| 时间 | 事件 | 快照的可见性 | 增量流的起点 | 这期间的变更去哪了 |
| --- | --- | --- | --- | --- |
| 01-19 09:12 | `mysqldump --single-transaction` 开始，拿到一致性快照 | **09:12** | 未启动 | — |
| 01-19 14:05 | dump 结束，共 1.1 TB | 09:12 | 未启动 | 09:12–14:05 的变更只在 binlog 里 |
| 01-19 14:10 | Debezium 启动，用 `SHOW BINARY LOG STATUS` 取到**当前**位点 | 09:12 | **14:10** | **09:12–14:10 无人认领** |
| 01-19 14:11 起 | 增量流开始追平 | 09:12 | 14:10 → 前进 | 只有 14:10 之后的变更被投出 |
| 01-21 | 财务对账发现退款金额差 138 万元 | — | — | 那 4 小时 58 分钟的 UPDATE/硬删除**永久缺失** |
| 01-26 | 用备份 + binlog 手工补数，5 天 | — | — | 期间风控模型一直用旧状态评分 |

**这一起事故的教训不是「不该用 mysqldump」，而是「快照的可见性时刻」和「增量的起点」必须是同一个时刻，而它们当时差了一个快照时长。** 正确写法在 §33.6：`mysqldump --single-transaction --source-data=2`（8.0.26 之前的参数名是 `--master-data=2`）会把位点写在 dump 文件头里，而那个位点**早于**快照时刻 —— 于是重放时必然出现「快照里已包含的变更被再投一遍」，这是**重复**；而「位点晚于快照时刻」则是**丢失**。**同一个配置写错方向，一边多一边少，都不报错。**

> 顺带一句：这两件事在监控上的表现完全一样——`MilliSecondsBehindSource` 归零、connector 状态 RUNNING、Kafka 有数据在流。唯一的差别要到半年后对账才被发现。

### 事故二：给订单表加一列并顺手把金额精度调大，三套下游在 8 分钟内全停（2026-02-11）

`order_main` 13 列，`amount DECIMAL(12,2)`。产品要按渠道打标，DBA 执行了一条看起来很轻的 DDL：`ALTER TABLE order_main ADD COLUMN channel_code VARCHAR(16) NULL DEFAULT NULL, MODIFY COLUMN amount DECIMAL(14,2) NOT NULL, ALGORITHM=INSTANT`。

`ADD COLUMN` 在 MySQL 8.0.12+ 可以 INSTANT，但 `MODIFY` 改精度必须重建表，所以引擎拒绝了 `ALGORITHM=INSTANT`、退化成 `ALGORITHM=COPY`（改列类型/精度走的路径，具体支持矩阵以目标版本文档为准）：**2.4 亿行全表重建，写了 41 分钟**。

| 时刻 | 事件 | Debezium 侧 | 下游 ① 缓存失效服务 | 下游 ② 风控（Avro） | 下游 ③ 数仓（Flink SQL） |
| --- | --- | --- | --- | --- | --- |
| 14:02 | DDL 开始，落 COPY 路径，表只读 | 表锁等待 | 正常 | 正常 | 正常 |
| 14:03 | DDL 完成，`QUERY_EVENT` 进 binlog | schema history 追加 DDL，新 schema 13 列且 `decimal(14,2)` | 收到新字段 | 收到新 schema | 收到新字段 |
| 14:04 | 下游 ① 用 Jackson 严格模式反序列化 envelope | — | **`UnrecognizedPropertyException`** | — | — |
| 14:04 起 | 下游 ① 位置不提交 → 反复重投同一条毒消息 | — | **卡在同一 offset，lag 冻结** | — | — |
| 14:06 | 下游 ② 的 Avro reader 与新 writer 的 `decimal.precision` 不符 | — | — | **`SerializationException`，全分区停** | — |
| 14:10 | 下游 ③ 忽略未知字段，但 `amount` 读成 `null` | — | — | — | **静默错**（不报警） |
| 15:30 | 有人发现风控侧订单状态停更 | — | — | — | — |
| 17:14 | 按 §33.7 的止血顺序恢复 | — | — | — | — |

代价：3 小时 12 分钟里风控用旧状态放行 4,100 单，拒付与赔付 **27 万元**；数仓补数 9 小时；下游 ③ 的 `null` 金额直到第二天做报表才发现。

**关键的一句话：反序列化失败不表现为 lag 增长，表现为 lag 冻结。** 因为消费位点在抛异常的位置没有提交，消费者会不停重投同一条 —— 这也是「明明有积压但消费速率是 0」这种诡异形状的成因（§33.7 与 §33.10）。

### 事故三：binlog 从 7 天缩到 3 天，connector 停了 5 天后回来，位点已不存在（2026-03-04）

主库 binlog 峰值 750 GB/天（§33.8 账一），保留 7 天 = 5.25 TB，运维为省盘把 `binlog_expire_logs_seconds` 从 604800 改成 259200（3 天）。

同期有一次「Kafka Connect worker 大版本升级」，原计划 2 小时，因为 `connect-offsets` topic 的兼容问题拖成了 5 天，且没人记得这条链路上还有 CDC。

| 时刻 | 事件 | connector 状态 | 源端 binlog 起点 | 下游 |
| --- | --- | --- | --- | --- |
| 03-01 02:10 | Connect 集群停机维护，connector 落盘的最后位点在 `mysql-bin.000418:884,213` | STOPPED | 03-01 的位点仍在窗口内 | 正常（无新数据要投，但没人知道） |
| 03-02 | 保留窗口按 3 天滚动，`000418` 被 PURGE 掉 | STOPPED | 推进到 `000421` | 无人察觉 |
| 03-04 | `000418` 之后 3 天的文件也被清 | STOPPED | 推进到 `000425` | 无人察觉 |
| 03-06 10:00 | 集群恢复，connector 启动 | **FAILED**（`ClientError 1236`：找不到位点所在文件） | — | — |
| 03-06 10:20 | 判断只能重做全量：`snapshot.mode=initial` + 清空 `connect-offsets` 里的位点 | STARTING | — | — |
| 03-06 10:20–03-07 05:00 | 重做快照：1.1 TB ÷ 16.5 MB/s ≈ **18.5 小时** | SNAPSHOT | 持续写入 750 GB/天 | 只读旧数据 |
| 03-07 05:10 | 切增量，但下游没有「快照重来」的标记 | RUNNING | — | 「先新后旧」覆盖，第二轮对账 |

代价：下游 19 小时 40 分只读旧数据；两轮对账 320 人时；SLA 赔付 **55 万元**。

> 🩸 **三起事故的共同点**：① 是「快照与增量的位点方向写错了」；② 是「schema 变更的传播顺序没排」；③ 是「保留窗口与最长停机时间没有绑定」。三件事都**不在 code review 里**，因为它们的载体分别是**一份启动参数**、**一条 DDL 的发布顺序**、**一个 EXPIR 时间**。

---

## 33.2 一句话定位与唯一真问题

**定位**：CDC 不是「读 binlog 的工具」，它是一个**由一个位点串起来的两种数据源**：一份某一时刻的全量快照（静态、大、慢），加一条从该时刻之后开始的事务流（动态、小、快）。位点就是这两者的缝合线。

### 唯一真问题

> **CDC 的难点不是解析 binlog，而是「全量快照」与「增量流」在同一个位点上的无缝衔接（少一条、多一条都在这里发生），以及 schema 变更不把下游消费者打死。**

四条推论，全篇都在给它们找证据：

1. **位点是一个「方向敏感」的东西**：位点晚于快照时刻 → 中间丢失；早于快照时刻 → 重复。而这两件事**都不会报错**，只会在对账时显形（§33.6）。
2. **快照的正确性由隔离级别与锁窗口给出，不由导出工具给出**：RR 快照 / FTWRL / PG exported snapshot 三种做法买到的「一致性强度 × 阻塞时长」完全不同，选错就是把主库锁 40 分钟或把数据读成半新半旧（§33.6）。
3. **CDC 是「至少一次」的，重复来自五个不同的地方**，且每一处的放大倍数不同（§33.4）。「恰好一次」在 CDC 链路上永远要落到「下游幂等」。
4. **schema 变更不是元数据更新，是一次跨进程的契约变更**：源库 → binlog → schema history → registry → 下游代码，五跳里任何一跳没有「容忍未知 / 有默认值 / 有死信」的能力，整条链就停（§33.7）。

### 两种采集方式，以及各自的边界

| 维度 | **log-based**（binlog / WAL / oplog） | **query-based**（轮询时间戳/版本列） |
| --- | --- | --- |
| 数据来源 | 源端的**事务日志**（提交顺序的真值） | 业务表的**当前状态**，按 `WHERE updated_at > :last` 拉 |
| 延迟 | ms 级（= 日志可见延迟） | = 轮询周期（秒–分钟级），且抖动与表大小相关 |
| 能否拿到变更前后值 | ✅ 有 before/after 镜像（前提是 `binlog_row_image=FULL`） | ❌ 只有「现在是什么」，没有「之前是什么」 |
| 能否捕获硬删除 | ✅ `DELETE_ROWS` 事件 | ❌ 行消失了，没有事件；只能靠「上次有、这次没有」做差集 |
| 能否捕获被覆盖的中间态 | ✅ 每条 UPDATE 都在 | ❌ 同秒内改 3 次只剩最后 1 次；`updated_at` 相同的多行会漏 |
| 对源库的压力 | 读日志，不改表；压力在 binlog 保留与网络 | **对主键/时间列做范围扫描**，大表 + 无索引 = 全表扫（→ comp/09） |
| 失效点 | 位点落到保留窗口之外（事故三）；schema 漂移 | `updated_at` 时钟回拨、批量 backfill 改写历史值、无索引、软删除与硬删除混淆 |
| 能否保证「不丢」 | 位点在窗口内 + 快照衔接正确 ⇒ 至少一次 | **结构上不能**：两次轮询之间的窗口靠 `updated_at` 单调性兜底，而它不单调 |
| 典型工具 | Debezium、Canal、Flink CDC、PG 逻辑复制 | DataX / Sqoop / 自研定时任务、`cloudwatch` 式拉取 |

**一句话**：**query-based 不是「增量」，它是「快照的反复执行」。** 它把位点换成了一个时间戳，但没有换掉「位点」这个概念 —— 时间戳不单调、删除无痕迹、中间态被覆盖，这三件事决定了它只能做**对账与兜底**，做不了**变更流**。

### 解剖卡八问落点（本篇答案）

| 问 | 落在哪节 | 一句话答案 |
| --- | --- | --- |
| ① 承诺什么 | §33.9 | 承诺「源端已提交的行变更，按表内顺序、至少一次地被投出」，**不承诺**跨表全局顺序、不承诺恰好一次、不承诺源端 purge 后能回溯 |
| ② 数据长什么样 | §33.3 | 源端是一条**线性日志**（binlog 文件序列 / WAL 段 / oplog 环），CDC 侧是「一个位点 + 一份 schema history + 一条事件流」 |
| ③ 一次读写走哪条路 | §33.4、§33.6 | dump 线程读日志（μs 级解析）→ 反序列化成 change event → 序列化 → 写 Kafka（ms 级）→ 下游反序列化 + 幂等写（ms–100 ms 级）；**真正的成本不在解析，在快照与位点管理** |
| ④ 并发与一致 | §33.6、§33.4 | 一致性由「快照事务的隔离级别 + 位点取值时刻」联合决定；位点由 Kafka Connect 的 `connect-offsets` 落盘，刷新周期 = 重复窗口 |
| ⑤ 扩展维度 | §33.9 | **四选一里的「分片」**：一个 connector 能按表拆 task（增量侧），快照侧默认串行 —— 快照的吞吐不是一个可无限并行的问题，它是「单表 ÷ 单 task 吞吐」 |
| ⑥ 系统级失效 | §33.6、§33.9 | 三类：**数据不一致**（位点方向错 ⇒ 丢失或重复）、**集群/链路不可用**（源端 purge ⇒ connector 必须重做全量）、**成本失控**（binlog 保留 × 下游放大 × 保留天数） |
| ⑦ 看哪几个指标 | §33.11 | `MilliSecondsBehindSource`、快照进度（`RowsScanned`/`RemainingTableCount`）、源端位点距保留边界、schema 错误计数、PG slot 的 `retained` 字节 |
| ⑧ 换掉它的信号 | §33.9 | 源端 DDL 频率高到「每次都要停链」、表 TB 级且不允许长事务、需要跨库一致快照 ⇒ 改增量快照 / Flink CDC 的 checkpoint 对齐 / 直连湖表（→ architect/21） |

---

## 33.3 源端物理形态：三种日志，一个位点

CDC 的全部语义都建立在源端日志的形状上。只给一句结论把 MySQL 侧的三份日志讲完：**InnoDB 一次提交要落 redo / undo / binlog 三份，binlog 与 redo 的一致靠内部两阶段提交 + 组提交，机制全在 → comp/08，本篇只用它的输出 —— 一条按提交顺序排列、可被外部客户端读取的 binlog 流。**

### 一次「UPDATE 一行」在 binlog 里长什么样

```text
一次「UPDATE 一行」在 binlog 里长什么样（ROW 格式，FULL 镜像）

┌─ binlog 文件 mysql-bin.000418，起始位点 = 4 ─────────────────────────┐
│ offset   事件                        内容                            │
│ 4        FORMAT_DESCRIPTION_EVENT   binlog 版本 + checksum 算法       │
│ 128      GTID_LOG_EVENT             uuid:1-5000-884213（GTID）        │
│ 191      QUERY_EVENT (BEGIN)        事务起点（InnoDB）                │
│ 320      TABLE_MAP_EVENT            table_id=118 → order_db.order_main │
│                                     + 13 列的列类型/元数据            │
│ 512      UPDATE_ROWS (tbl 118)      before 镜像（13 列全量）+         │
│                                     after  镜像（13 列全量）          │
│ 1,104    XID_EVENT                  事务提交（commit 位点）            │
└──────────────────────────────────────────────────────────────────────┘
        ▲                                    ▲
   file:offset 位点                     GTID 位点（推荐）：
   （主从切换会漂移）                    uuid:1-5000-884213
                                        「已提交事务集合」，可幂等

三个位点口径，别混（主从切换时位点为什么会漂 → comp/10）：
  binlog file + position  = 字节位点，绑定具体文件；文件被 PURGE 就永久失效
  GTID set                = 事务集合，跨文件、跨主从成立；源端 purge 后同样回溯不了
  「快照的一致性点」       = 快照事务的可见时刻，必须与上面两者对齐（§33.6）
```

### 事件类型：CDC 每个动作都能对上一个事件

| 事件 | 何时出现 | CDC 用它做什么 | 踩坑点 |
| --- | --- | --- | --- |
| `FORMAT_DESCRIPTION_EVENT` | 每个 binlog 文件开头一次 | 校验 binlog 版本与 checksum | 版本跨代（5.7 → 8.x）升级后老 connector 可能直接拒绝 |
| `GTID_LOG_EVENT` | 每个事务/语句开头 | 用 GTID 做位点，主从切换后仍有效 | 需要 `gtid_mode=ON` + `enforce_gtid_consistency=ON` |
| `TABLE_MAP_EVENT` | 每个「本次事务里第一次出现的表」 | 把 `table_id` 翻成库表名 + 列定义 | 有缓存；DDL 后 table_id 会变，缓存必须失效 |
| `WRITE_ROWS` / `UPDATE_ROWS` / `DELETE_ROWS` | 行级变更 | before/after 镜像 —— **CDC 的唯一数据来源** | `binlog_row_image=MINIMAL` 时 before 只有主键，拿不到旧值 |
| `PARTIAL_UPDATE_ROWS` | JSON 列的部分更新（MySQL 8.0.16+，需 `binlog_row_value_options` 开） | 需要 connector 专门支持 | 老版本 connector 解析失败 ⇒ 直接把流打断；Debezium 类工具建议关闭该选项 |
| `QUERY_EVENT` | DDL / BEGIN | 驱动 schema history（表结构变了） | 需要 `binlog_row_metadata=FULL` 才有列名与枚举字符串 |
| `XID_EVENT` | InnoDB 事务提交 | **事务边界**：只有它之后的数据才该被投给下游 | 不看 XID 就投 = 把未提交事务的中间态发给下游 |
| `ROTATE_EVENT` | 文件切换 | 推进 file:offset | 切文件瞬间位点跳变，是「重复一小段」的常见来源 |
| `HEARTBEAT_LOG_EVENT` | 空闲期按 `binlog_heartbeat_period` 发 | 保活 + 让 connector 提交位点前进 | 不配 heartbeat：低写入表长时间无事件 ⇒ 连接被判超时；位点不前进 ⇒ 老 binlog 不敢 purge |

### 为什么「不开 ROW 格式就没有变更前后值」

| `binlog_format` | 记的是什么 | CDC 能用吗 | 为什么 |
| --- | --- | --- | --- |
| `STATEMENT` | **SQL 原文** | ❌ 不能 | 你没有 before/after，只有一条 SQL。要还原变更得在别处重放它，而 `NOW()`、`UUID()`、`LIMIT` 无 `ORDER BY`、`@@session` 变量都会让重放结果与原执行不同 —— MySQL 文档自己列了这张不确定性清单。此外它要求你能重建执行时的完整上下文（表结构、会话变量） |
| `MIXED` | 按语句是否确定性**自动选择** | ❌ 不能用 | 同一套业务里一部分语句落 STATEMENT、一部分落 ROW，行为不可预测；出问题时最难查 |
| `ROW` | **行镜像**（列值） | ✅ 唯一可用 | 与 SQL 无关、与函数无关，读到什么就是什么；`binlog_row_image` 决定镜像完整度：`FULL`（全部列）／`MINIMAL`（PK + 变更列）／`NOBLOB`（大字段省略） |
| `ROW` + `binlog_row_image=MINIMAL` | 半残的镜像 | ⚠️ 理论可用，实际不行 | UPDATE 的 before 只有主键，**下游拿不到「改之前是什么」**——「字段级 diff」「软删除识别」「幂等覆盖」全都失效；Canal/Debezium 都要求 `FULL` |

**代价对比（这里有一个必须承认的取舍）**：`FULL` 让 binlog 体积比 `MINIMAL` 大 ~55%（§33.8 账一逐步算），而 `MINIMAL` 会让 CDC 失去一半能力。**这是一个「省盘」与「CDC 能干活」之间的正面冲突，不能用调参抹平。**

### 另外两种源端的形状：位点 + 保留窗口决定一切

```text
三种源端的「位点 + 保留」形状（决定 connector 最多能停多久）

MySQL binlog      线性文件序列，按 binlog_expire_logs_seconds / max_binlog_size 切
  [000418][000419][000420][000421 活跃]        ← PURGE 从左边整段删
       ▲ 位点在这里；左边的文件被删 → 位点失效（error 1236，事故三）

PostgreSQL WAL    线性段文件 + 逻辑复制槽（logical replication slot）
  [seg][seg][seg][seg]  ← 槽的 restart_lsn 钉住「这之后一律不许回收」
       ▲ restart_lsn                        ↑ 槽不掉，WAL 就永远不回收 → pg_wal 撑满

MongoDB oplog     capped collection，环形覆盖：不是「删除」，是「被新条目挤掉」
  ┌──────────── 固定大小（如 10 GB）────────────┐
  │ 旧 ... [resume token] ... 新               │ ← 新条目从头部覆盖最老条目
  └───────────────────────────────────────────┘
        ▲ resume token 被覆盖 → 「resume token 找不到」，只能重做快照

共同结论：三种源端给 CDC 的「回溯能力」都由**保留窗口**这一个数字决定，
         所有「断点续传」承诺都不超出这个窗口 —— 这是 §33.1 事故三的全部根因。
```

| 维度 | MySQL binlog | PostgreSQL 逻辑复制槽 | MongoDB change stream / oplog |
| --- | --- | --- | --- |
| 开启条件 | `binlog_format=ROW`、`gtid_mode=ON`（推荐）、`binlog_row_image=FULL` | `wal_level=logical`、`max_replication_slots ≥ 订阅数`、`max_wal_senders ≥ 连接数` | 必须是**副本集**或分片集群（单机没有 oplog） |
| 位点形式 | `file:offset` 或 GTID set | `LSN`（`confirmed_flush_lsn` / `restart_lsn`） | `resume token`（内含 cluster time + 位置） |
| 保留由谁决定 | `binlog_expire_logs_seconds`（8.0.1+；更早是 `expire_logs_days`）+ `PURGE BINARY LOGS`（谁都能手工跑） | **槽本身**：只要槽不推进，WAL 永不回收；`max_slot_wal_keep_size`（PG 13+，默认 `-1` = 不限，以目标版本为准）是唯一的兜底闸门 | oplog 的固定容量（`--oplogSize` / `replSetResizeOplog`），写入越快窗口越短 |
| 旧值从哪来 | `before` 镜像（要 `FULL`） | `REPLICA IDENTITY`：`DEFAULT`（只有主键）／`FULL`（整行旧值）／`NOTHING`；UPDATE/DELETE 要旧值就必须 `FULL` | MongoDB 5.0+ 的 `changeStreamPreAndPostImages` + `fullDocumentBeforeChange` |
| DDL 在流里吗 | ✅ `QUERY_EVENT`（所以能用 schema history 重建表结构） | ❌ **逻辑解码不传 DDL** —— 这是 PG 侧最容易被忽略的一条 | 无 DDL（文档库天然半结构） |
| 保留耗尽的表现 | `ClientError 1236` / 「找不到 binlog 文件」 | `wal_status=lost`（槽被 `max_slot_wal_keep_size` 作废）→ connector 报错只能重做快照 | `ChangeStreamHistoryLost` / resume token 失效 |
| 对源库最危险的操作 | 手工 `PURGE BINARY LOGS`、把 `expire_logs_seconds` 调小 | **留着一条不用的槽**（一个死槽能把整个 `pg_wal` 撑满，直接把主库写死） | oplog 容量给太小（默认值在小实例上常常只有几百 MB 量级，以目标版本为准） |

**把这张表压成一句纪律**：**位点不是一个「位置」，是一个「有效期内的位置」。** 任何位点管理方案都必须回答同一个问题 —— 「connector 最长可以停多久，而位点仍然有效？」这个数字叫**位点有效期**，它由源端保留窗口决定，而它必须 ≥ 你的最坏停机时间 + 快照时长（§33.8 账二）。

---

## 33.4 Debezium：一个 Connect 连接器，加三个「外部状态」

Debezium 的形态不是「一个 agent 进程」，而是 **Kafka Connect 上的 source connector + 三份存在 Kafka 里的外部状态**。理解这一点，后面所有「重启后为什么重来」「为什么换了集群就丢了 schema」都是推论。

| 外部状态 | 存在哪里 | 存的什么 | 丢了会怎样 |
| --- | --- | --- | --- |
| **offset**（位点） | `offset.storage.topic`（默认 `connect-offsets`，compacted） | 每个 connector/task 的最后位点（GTID set 或 file:offset）、快照是否完成 | 丢 ⇒ connector 从头开始（若 `snapshot.mode=initial` 则**重做全量**）；回退 ⇒ 重复投一长段 |
| **schema history** | `schema.history.internal.kafka.topic`（compacted；2.0 之前的参数名是 `database.history.kafka.topic`） | 一条条 DDL/表结构变更，按出现顺序 | 丢 ⇒ 到任一位点都无法重建「那一刻表长什么样」⇒ 事件无法解析，只能重做全量 |
| **数据流本身** | 业务 topic（每表一个，或按 `topic.prefix` 规则） | change event（envelope：`before`/`after`/`source`/`op`/`ts_ms`） | 丢失由 Kafka 的 RF/保留策略负责（→ comp/28、comp/29） |

**三者的顺序是有强制的**：schema history 与 offset 必须**配对**（同一位点对应同一版表结构）。所以「只清 `connect-offsets` 保留 schema history」这种操作，会让 connector 在旧位点上用新表结构解析旧事件 —— 表现是一堆莫名其妙的字段错位。

### snapshot.mode：五种模式，五种风险

| `snapshot.mode` | 做什么 | 什么时候用 | 风险 |
| --- | --- | --- | --- |
| `initial`（默认） | 首次启动做全量快照，然后接增量 | 全新接入 | 快照期间源库长事务（§33.8 账二）；大表要几小时到一天 |
| `initial_only` | 只做快照，做完就停 | 只要一份静态底座、增量另建 | 之后位点不再前进 ⇒ **binlog 会被源端 purge 掉**（位点失效） |
| `schema_only` | 只读表结构，不做数据快照，从当前位置开始增量 | 数据已由别的工具灌好 | 位点与已有数据必须自己对齐 —— **事故一的翻车点就在这** |
| `schema_only_recovery` | 保留原 schema history 重建，用于修复损坏的 schema history | 灾后修复 | 会重启 connector 的整体状态机，慎用 |
| `never` | 不做快照、不读 schema history | 只做增量、schema 由外部提供 | 与 `schema_only` 类似，但更容易踩「位点与数据不对齐」 |
| `when_needed` / `configuration_based` | 仅在需要时（无位点/无 history）或按配置快照 | 2.x 的补充模式，具体语义以目标版本文档为准 | 依赖配置正确，容易误触发全量 |

### 增量快照：为什么它是「快照 + 增量衔接」的正解

原生快照有两个无法回避的问题：**要停流**（快照期间不能同时可靠地读增量），**不可重跑**（想给一张表补数就得重做全量）。Debezium 的 **incremental snapshot**（signal table + watermark）把这两个问题换成了一个 **去重窗口** 的问题：

```text
Debezium 增量快照的「低水位 / 高水位」窗口（去重窗口是唯一依据）

binlog ──┤ … ├─────────────────────────────────────────┤ … ├──►
         t_L                                          t_H
          │                                            │
   ① INSERT 低水位信号行 → connector 在流里读到它，记下位点 L
          │                                            │
          ├── ② 按主键区间分 chunk 读业务表（不加锁，每 chunk 一次 SELECT）
          │        chunk1 → chunk2 → … → chunkN（读到的行以 INSERT 事件发出）
          │                                            │
   ③ INSERT 高水位信号行 → connector 记下位点 H，并发出 high watermark 事件
                                                       │
   下游去重窗口：缓存 (L, H] 之间的所有流事件
      ┌ 收到高水位时，把缓存事件按主键覆盖到快照行上
      ├ 窗口内的 INSERT/UPDATE 胜出；窗口内的 DELETE 让该行消失（删除胜出）
      └ 窗口之外的事件正常处理（快照不覆盖它们）
   ⇒ 结论：增量快照的「至多重复一次、绝不丢失」是**下游去重窗口**买来的，
            不是 connector 单方面保证的
```

| 增量快照的优点 | 它的代价 |
| --- | --- |
| 不用停流（快照与流同时进行） | 下游**必须**实现「按主键覆盖 + 窗口内去重」，否则快照行会覆盖更新的流事件 |
| 可随时对单表/部分行重跑（`execute-snapshot` 信号） | 需要在源库建信号表（写入权限 + 建表权限，很多 DBA 会拒绝） |
| chunk 化（默认 1,024 行量级，`incremental.snapshot.chunk.size`）⇒ 每个 chunk 之间可喘息，不产生巨型长事务 | chunk 越多，信号行与水位事件越多；chunk 太小 ⇒ 位点/日志开销上来，chunk 太大 ⇒ 单个 SELECT 与长事务窗口变大 |
| 可以带过滤条件（`additional-condition`，字段名与是否支持按目标版本为准） | 过滤条件必须与业务确认，否则「补漏」补成「再漏一次」 |

信号表本身极简（下面这段可直接用）：

```sql
-- 增量快照的「遥控器」：一张普通表，connector 在流里读它的新行
CREATE TABLE dbz_signal (
  id     VARCHAR(64)   NOT NULL PRIMARY KEY,   -- 信号 id，仅用于去重与审计
  type   VARCHAR(32)   NOT NULL,               -- 'execute-snapshot' | 'stop' | 'log'
  data   VARCHAR(2048) NULL                    -- JSON 参数
) ENGINE=InnoDB;

-- 对一张「历史遗留、需要补数」的表发起增量快照（不中断当前流）
INSERT INTO dbz_signal VALUES (
  'resnap_order_main_20260306',
  'execute-snapshot',
  '{"data-collections":["order_db.order_main"],
    "type":"incremental",
    "additional-condition":"created_at >= ''2026-01-01''"}'
);

-- 信号表不会被 connector 自动清理：这是审计记录，也是「谁在什么时候补了哪张表」的唯一凭据
SELECT id, type, data, NOW() AS seen_at FROM dbz_signal ORDER BY id;

-- 止血开关：让 connector 流到这条信号时安全停下（而不是 kill -9 让它丢位点）
INSERT INTO dbz_signal VALUES ('stop_20260306','stop','');
```

### offset 与「至少一次」：重复的五个来源

Debezium 的记录投递是**至少一次**。重复不是 bug，是设计；关键是每一处放大多少、由谁吸收。

| # | 重复来源 | 放大多少 | 吸收点 |
| --- | --- | --- | --- |
| ① | **offset 落盘周期**：Connect worker 按 `offset.flush.interval.ms`（默认几十秒量级，以目标版本为准）批量刷位点；崩溃时最后一段没刷的会重投 | 最多「刷新周期内的事件数」= 15,000 行/s × 60 s ≈ **90 万条**（§33.8 参数） | 下游幂等（按主键 upsert） |
| ② | **快照中断重来**：快照进行到 60% 挂掉，重启后（`initial` 模式）从头再扫 | 整张表重复一遍 = 表的行数 | 幂等 + 「快照模式优先于流」的下游规则 |
| ③ | **task rebalance / 多 task 接手**：增量侧按表分 task，重分配时各自从落后位点开始 | 每张表最多一段 | 幂等；避免频繁 rebalance（→ comp/30） |
| ④ | **源端重放**：主从切换、GTID 空洞导致 engine 侧重读一段 | 一般很小（一个事务组） | 幂等 |
| ⑤ | **SMT / 下游投影非幂等**：`extractNewRecordState` 之后如果下游写的是「计数器 += 1」而不是「覆盖成某个值」 | 每次重复都被计入 | 只能是「覆盖写」或「带业务幂等键的写」（→ eng/24） |

> Debezium 侧也有「精确一次」的努力方向（Kafka Connect 3.3+ 按 KIP-618 引入 source connector 的精确一次支持，Debezium 在新版本里跟进，具体开关名与版本门槛以目标版本文档为准）。但即使打开，它保的也只是「Kafka 内的 offset 与事务一致性」，**保不了你下游那个 MySQL 的写入**。所以本篇的口径不变：**CDC 承诺至少一次，效果恰好一次 = 幂等。**

### tombstone：为什么 DELETE 会产出两条消息

| 项 | 内容 |
| --- | --- |
| 物理动作 | 一条 `op=d` 的消息（value 里带 `before` 镜像）之后，紧跟一条 **value 为 null 的同 key 消息**（`tombstones.on.delete=true`，默认开） |
| 为什么需要 | compact 策略的 topic 只保留「每个 key 的最后一条」；`null` 是**墓碑**，告诉 compaction「这个 key 可以彻底消失了」（→ comp/28 §28.6） |
| 为什么下游常被它打死 | 上一条还有 `before`，下一条整个 value 是 `null`；下游反序列化器若假设「value 非空」会直接 NPE / 抛异常 —— 而这条毒消息会卡死分区（事故二的同款形状） |
| 怎么用 | 消费端把 `null` 解释为「删除这个 key」；如果不需要 compact（`delete` 策略的 topic），可以关掉墓碑，但那就永远无法回收 key |

---

## 33.5 Canal：binlog dump 协议的另一种形态，与它的一个结构性缺口

Canal 的思路和 Debezium 完全不同：**它把自己伪装成一个 MySQL 从库**，向主库发 `COM_BINLOG_DUMP`（按 file:offset）或 `COM_BINLOG_DUMP_GTID`（MySQL 5.6+），主库按从库协议把 binlog 推给它。这也是为什么 Canal 必须配一个**不重复的 `server_id`** —— 主库认得每一个「从库」的 `server_id`，撞了就会踢掉一方。

| 组件（Canal 1.1.x 形态） | 职责 | 它带来的运维面 |
| --- | --- | --- |
| `canal.server` | 伪装从库、拉 binlog、解析成 `Entry`（RowChange / TransactionBegin / TransactionEnd / DDL） | 位点存 ZK 或本地 `meta.dat`；解析器与源端版本的耦合 |
| `canal.adapter` | 把 `Entry` 投递到 ES / HBase / 关系库 / MQ（`canal.mq.*`，可投 Kafka/RocketMQ） | 同步语义在 adapter 侧，ACK 与重试策略要自己定 |
| `canal.admin` | 1.1.x 的配置管理 + HA 协调（实例/任务/位点） | 多一个要部署、要备份、会脑裂的组件 |

| 维度 | Canal（1.1.x） | Debezium（2.x） |
| --- | --- | --- |
| 交付形态 | 独立 Java 服务 + adapter（自建协议，客户端拿 protobuf `Entry`） | Kafka Connect connector（输出给 Kafka，或 embedded 嵌进你的进程） |
| 位点存储 | ZK / 本地 `meta.dat`（`{logfile, position, gtid}`） | Kafka 的 `connect-offsets`（compacted topic） |
| 表结构从哪来 | 自己解析 DDL + 从 `information_schema` 拉；**没有独立的 schema history 流** | `schema.history.internal.kafka.topic` 独立持久化 |
| **全量快照** | ❌ **本体没有一致性快照** | ✅ 内置 `initial` / `schema_only` / 增量快照（signal + watermark） |
| HA | ZK 抢临时节点（1.0.x）/ admin 协调（1.1.x）；**切换可能位点回退 ⇒ 重复** | Connect worker 的 task 重分配；位点在 `connect-offsets` 里，回退同样会重复 |
| 什么时候选它 | 已经在阿里系生态、要一个「轻量 binlog → MQ」管道、只做增量 | 需要完整 CDC（快照 + 增量 + schema 演进）、要接 Kafka 生态 |

**它的结构性缺口，一句话**：**Canal 只解决「增量」，不解决「快照 + 增量的衔接」。** 所以用 Canal 的人必然要自己拼出「全量导出 + 位点对齐」这一段，而这一段是本篇唯一真问题的全部。目前只有三种补法：

| 补法 | 怎么对齐 | 一致性 | 代价 |
| --- | --- | --- | --- |
| ① `mysqldump --single-transaction --source-data=2`（8.0.26 前为 `--master-data=2`）拿位点 | dump 文件头里的位点**早于**快照时刻 ⇒ 位点比快照旧 | 至少一次（会重复，不会丢） | 必须下游幂等；大表 dump 几小时（§33.8 账二） |
| ② 先起增量、位点取「现在」，再导出全量 | 位点**晚于**快照 ⇒ 中间窗口丢失 | 会丢（事故一的形状） | 除非你能证明窗口内无变更，否则不可用 |
| ③ 用 Debezium 做快照、Canal 只做增量 | 两边共用同一份「快照完成位点」 | 正确但对齐逻辑要自己写 | 两套系统、两套位点，运维面翻倍 |

**判据**：**如果你需要「随时能给一张表补数」，那 Canal 本体做不到，只有 Debezium 的增量快照或 Flink CDC 的 checkpoint 对齐能给你。** 这不是选型偏好，是「有没有内置衔接机制」的事实差。

---

## 33.6 快照 + 增量衔接：本篇的支点

这一节是全篇最需要反复读的部分。前面所有事故、后面所有账，都从这里长出来。

### 四种「接不上」的形态

```text
「快照」与「增量」接不上的四种形态（每一种都是静默失败）

源库提交顺序  ──►  t0 ─────── t1 ─────── t2 ─────── t3 ──────►

① 位点取晚（丢）            快照可见性 = t0        取位点 = t2
   ├── 快照只含 t0 之前的数据
   ├── 增量从 t2 开始 ⇒ (t0, t2] 的变更既不在快照里、也不会被重放
   └── 现象：某些行的某些字段永远是旧值；无报错（事故一）

② 位点取早（重）            取位点 = t0            快照可见性 = t1
   ├── (t0, t1] 的变更既在增量流里、又已体现在快照里
   └── 现象：同一行被写两遍；有唯一键则无感，无唯一键则重复计数

③ 快照本身不一致            非 RR / 非 exported snapshot，多表分开读
   ├── 表 A 读到 t1 的状态，表 B 读到 t2 的状态
   └── 现象：跨表对账永远差一笔（外键悬空、金额与明细不符）

④ 快照与流并行、无边界标记  重建/补数场景：流先到、快照后到
   ├── 先写入新值，再被旧快照覆盖
   └── 现象：「数据变回去了」，只能靠人肉比对发现
```

### 一致性快照的三种做法：一致性强度的来源

| 做法 | 机制 | 一致性买到了什么 | 阻塞时长 | 适用 |
| --- | --- | --- | --- | --- |
| **MySQL `START TRANSACTION WITH CONSISTENT SNAPSHOT`（RR）** | 事务开始时建立 Read View，整个快照期内读到的是同一份 undo 版本 | 单事务内多表一致（同一时刻视图） | 0（不阻塞写）；**但会产生一个长事务** | Debezium `snapshot.isolation.mode=repeatable_read`（默认量级）走这条路 |
| **MySQL `FLUSH TABLES WITH READ LOCK`（FTWRL）** | 全局读锁，锁住所有表的写 | 最强，但期间**全库不可写** | 等于持锁时长 —— 几小时就是全站事故 | 只允许持有到「取位点 + 开启事务」这一瞬间；`snapshot.locking.mode=extended` 会持到快照结束，禁止在生产使用 |
| **PostgreSQL exported snapshot** | `pg_export_snapshot()` 导出一个快照标识，其他连接用 `SET TRANSACTION SNAPSHOT '<id>'` 加入同一个快照 | 跨连接、跨会话的同一视图（多进程并行导出也一致） | 0（不阻塞写）；但导出的快照也是一个长事务的可见性来源 | PG 侧唯一正确的快照方式；何时创建、与 slot 的 `consistent_point` 如何对齐取决于连接器实现与目标版本 |
| **MongoDB** | 事务内读 + `$clusterTime`/`atClusterTime` 对齐 | 分片集群下的一致性读 | 0 | change stream 的 resume token 与快照时刻对齐 |

**RR 快照的隐性代价必须算进来**：快照是一个长事务 ⇒ 老版本不能被 purge ⇒ undo 表空间持续膨胀、purge 线程滞后（机制 → comp/06）。一个 18.5 小时的快照在写入 15,000 行/s 的库上，undo 的增长量级 = 「快照时长 × 写入速率 × 每行旧版本大小」≈ 18.5×3600 × 15,000 × 320 B ≈ **320 GB**。这就是「CDC 一上，undo 表空间就爆」的完整推演。

### 低水位 / 高水位：把「一致性」换成一个「可去重的窗口」

上一节买到的「一致性」是**静态的**（快照那一刻），而增量流是**动态的**。两者之间必须有一个**可判定的边界**。Debezium 的答案是：往源库插两条信号行，让它们**出现在增量流里**，于是「信号行在流中的位置」就成了边界（时序见图 4，§33.4）。

| 边界定义 | 谁定义的 | 下游要做什么 | 失败形态 |
| --- | --- | --- | --- |
| **低水位 L / 高水位 H** | 源库里的两条信号行（它们在 binlog 里的位点） | 缓存 (L, H] 的流事件，收到 H 时按主键覆盖快照行 | 不实现 → 快照覆盖新值（形态 ④） |
| **位点 = 快照时刻** | 连接器内部（`snapshot` 事务的位点） | 按主键幂等覆盖即可（重复由幂等吸收） | 位点取值方向错 → 丢或重（形态 ①②） |
| **checkpoint barrier** | 流处理引擎（Flink 的 checkpoint，→ comp/34） | 快照数据与流共用同一个 checkpoint 边界，状态里存「已读到哪个位点」 | checkpoint 不对齐 → 恢复时重放或跳过 |

### 按主键去重与 Kafka compaction

| 手段 | 它解决什么 | 它不解决什么 |
| --- | --- | --- |
| **key = 主键**（Debezium 默认用 PK 做 key，可用 `message.key.columns` 覆盖） | 同 key 的消息落到**同一分区**，顺序成立；compact 得以生效 | key 不是主键（如用业务号）时，「同 key」≠「同一行」 |
| **下游按主键 upsert** | 重复投递被覆盖吸收；乱序的旧值被 `src_ts` 挡住 | 「计数型」写入（`+= 1`）无法吸收重复 |
| **`cleanup.policy=compact`** | topic 里每个 key 只留最后一条 ⇒ 空间可控、可当 changelog | 回收滞后（墓碑保留时长）与后台重写放大（→ comp/28 §28.6）；**消费它仍然是顺序全扫** |
| **tombstone（value=null）** | 让 compact 真正删掉 key | 下游必须显式处理 `null`，否则 NPE |

### 位点与快照的原子切换：三种工程实现

| 实现 | 边界在哪 | 失败时怎么恢复 | 成本 |
| --- | --- | --- | --- |
| **Debezium 内置**（`initial` 或增量快照） | 连接器自己把「快照完成」写进 `connect-offsets` | 重跑快照（增量快照可只重跑某张表） | 最低；但你被 Connect 生态绑定 |
| **自研管道**：快照数据 + offset 写进同一事务 | 你把它做成「同一批次的原子提交」（Kafka 事务，或「offset 表 + 数据表同事务写」） | 从最后一个完整批次重放；重复由主键幂等吸收 | 中；逻辑要自己写，但完全可控（下面 Go 的骨架就是这个） |
| **流引擎对齐**：Flink CDC | checkpoint 边界 | 从最近一次成功的 checkpoint 恢复，位点从状态里读 | 高（引入一个流引擎），但换来 exactly-once 的**状态**语义（→ comp/34） |

两种语言的骨架，一段是「原子切换」，一段是「幂等吸收」：

```go
// 自研管道的「位点与数据同事务」：两阶段推进，永远不出现「数据进了但位点没进」
func applyBatch(ctx context.Context, db *sql.DB, rows []Change, pos Position) error {
    tx, err := db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
    if err != nil {
        return err
    }
    defer tx.Rollback() // 幂等：成功路径上 Rollback 是 no-op

    stmt, err := tx.PrepareContext(ctx, `
        INSERT INTO ods_order(order_id, payload, src_ts, src_pos)
        VALUES(?,?,?,?)
        ON DUPLICATE KEY UPDATE
          payload = IF(VALUES(src_ts) >= src_ts, VALUES(payload), payload),
          src_ts  = GREATEST(src_ts, VALUES(src_ts)),
          src_pos = GREATEST(src_pos, VALUES(src_pos))`)
    if err != nil {
        return err
    }
    defer stmt.Close()

    for _, r := range rows { // 同一主键的重复投递在这里被 src_ts 挡住
        if _, err := stmt.ExecContext(ctx, r.PK, r.JSON, r.SrcTs, r.Pos); err != nil {
            return err // 整批回滚 ⇒ 下一次从旧位点重试 ⇒ 重复但不丢
        }
    }
    // 关键：位点与数据在**同一个事务**里前进，二者要么都生效、要么都不生效
    _, err = tx.ExecContext(ctx,
        `INSERT INTO cdc_position(job, pos, updated_at) VALUES(?,?,NOW())
         ON DUPLICATE KEY UPDATE pos = GREATEST(pos, VALUES(pos)), updated_at = NOW()`,
        "order_cdc", pos)
    if err != nil {
        return err
    }
    return tx.Commit()
}
```

```java
// 下游吸收「快照 + 增量」重合段的姿势：按主键覆盖，按源端时间防回退
record Change(String pk, long srcTs, String op, JsonNode row) {}

void apply(Change c, JdbcTemplate jdbc) {
    if ("d".equals(c.op()) || c.row() == null) {   // 硬删除 / tombstone：记账，不物理删
        jdbc.update("""
            INSERT INTO ods_order_tomb(order_id, src_ts) VALUES(?,?)
            ON DUPLICATE KEY UPDATE src_ts = GREATEST(src_ts, VALUES(src_ts))
            """, c.pk(), c.srcTs());
        return;
    }
    // srcTs 取自 Debezium envelope 的 source.ts_ms（源端提交时刻），不是消费时刻：
    //  - 快照行与流事件撞车时「更新的赢」
    //  - 同一条重复投递被 <= 挡住（幂等）
    jdbc.update("""
        INSERT INTO ods_order(order_id, payload, src_ts) VALUES(?,?,?)
        ON DUPLICATE KEY UPDATE
          payload = IF(VALUES(src_ts) >= src_ts, VALUES(payload), payload),
          src_ts  = GREATEST(src_ts, VALUES(src_ts))
        """, c.pk(), c.row().toString(), c.srcTs());
}
```

**两段代码的共同点，是全篇最该背的一句**：**去重的能力不在 connector，在「主键 + 单调的源端时间戳」这一对上。** 只要这两样都在，快照重复、位点回退、task 重分配、毒消息重投这四种重复都能被无声吸收。

---

## 33.7 Schema 演进：五跳契约，任何一跳没有容错就整链停

CDC 是全链路里唯一一个「元数据变更会自动传播」的组件 —— 这也是它危险的地方：**一条 40 毫秒的 DDL，会在 40 秒内到达下游每一个进程。**

### 谁在「知道」表结构

| 跳 | 谁持有 schema | 它怎么更新 | 更新失败的形态 |
| --- | --- | --- | --- |
| ① 源库（真值） | `information_schema` | DDL 执行 | — |
| ② binlog | `TABLE_MAP_EVENT` 里的列定义/类型 + `QUERY_EVENT` 的 DDL 原文 | 每次 DDL 后 table_id 会变 | `binlog_row_metadata=MINIMAL`（8.0.1+ 的默认值量级）⇒ 事件里没有列名与枚举字符串 ⇒ 改名/枚举只能靠猜 |
| ③ connector | `schema.history.internal.kafka.topic` 的 DDL 序列 | 流里读到 DDL 就追加 | 该 topic 丢了 ⇒ 历史位点无法重建结构 |
| ④ registry / converter | `schema registry` 的 subject（`<topic>-value`） | 生产者注册新版本，按兼容级别校验 | 兼容级别 `NONE` ⇒ 破坏性变更被放行（事故二的下游 ②） |
| ⑤ 下游代码 | 编译期生成的 POJO / Flink SQL 的建表 DDL / 手写解析 | 发版 | 严格反序列化 ⇒ 毒消息卡死分区（事故二的下游 ①） |

### 兼容模式：四种，买的东西不一样

| 兼容级别 | 谁可以读谁 | 允许的变更 | **禁止的变更** |
| --- | --- | --- | --- |
| `BACKWARD`（多数 registry 的默认） | **新**消费者能读**旧**数据 | 加**带默认值**的字段；删字段 | 加无默认值的必填字段 |
| `FORWARD` | **旧**消费者能读**新**数据 | 加字段；删**带默认值**的字段 | 删无默认值的字段 |
| `FULL` | 双向 | 只允许加/删**带默认值**的字段 | 几乎所有结构性变更 |
| `NONE` | 不管 | 全部放行 | —— 事故二的成因之一 |

**Avro 侧还有几条「看起来兼容、实际坑人」的规则**（以 Avro spec 与目标 registry 实现为准）：

- **类型提升**：`int → long`、`float → double`、`string ↔ bytes` 是**允许**的 reader 提升 ⇒ `INT → BIGINT` 在 registry 上通常能过；但 MySQL 侧这个 ALTER 往往要走 **COPY**（重建 2.4 亿行，41 分钟只读），**真正的事故在锁窗口而不在 schema**。
- **`decimal` 的 `precision` 变更**：logical type 的参数变了 ⇒ **schema resolution 失败** ⇒ 硬停（事故二的直接触发点）。
- **列改名**：等价于「删旧字段 + 加新字段」⇒ `BACKWARD` 能过，但语义变成「旧字段消失、新字段为 null」⇒ **静默错**，比硬停更难查。
- **加 `NOT NULL` 无默认列**：新 reader 读旧数据时该字段无默认值 ⇒ resolution 失败 ⇒ 硬停。加列可以，但必须 `NULL` 或带默认值。

### 止血顺序（照这个次序，别自由发挥）

| 步 | 动作 | 为什么是这个顺序 |
| --- | --- | --- |
| ① | **先让毒消息不再卡死分区**：给 consumer 加「容忍未知字段 + 死信」能力（`ErrorHandlingDeserializer` 一类封装，或反序列化异常时 `seek` 到下一条 + 落 DLQ） | 不做这一步，后面所有修法都上不去 —— 分区卡在同一个 offset 上，改代码也消费不动 |
| ② | **再看 registry 有没有拒绝**：`GET /subjects/<topic>-value/versions` 与兼容性检查；若被拒，**临时**把级别降到 `NONE`（并记一条到期回滚的工单） | 契约层不开门，新 schema 根本进不了 topic |
| ③ | **判断方向**：是要「下游跟上新 schema」还是「把 DDL 回滚」 | 精度变更是不可逆的（`DECIMAL(12,2) → (14,2)` 回滚会截断数据），加列是可逆的 |
| ④ | **修下游的「结构性假设」**：POJO/表 DDL 加字段并给默认值；把「按位置取字段」改成「按名字取字段」 | 根因往往是「代码假设了列顺序/列数」，不是「多了个字段」 |
| ⑤ | **回放 DLQ**：按 `src_ts` 幂等重放（§33.6 的 upsert 天然支持） | 用幂等吸收重复，不必精确定位「漏了哪几条」 |
| ⑥ | **补一条纪律**：DDL 与消费者发布不同天；加列必须 `NULL`/带默认值；禁用改精度与改名的「顺手」DDL | 这才是防复发（§33.10） |

```bash
# 下游会不会被 DDL 打死的进场体检（对每个 consumer 问三个问题）
# ① 反序列化器是否容忍未知字段？（Jackson 默认 FAIL_ON_UNKNOWN_PROPERTIES=true）
grep -rn "FAIL_ON_UNKNOWN_PROPERTIES\|ObjectMapper\|SpecificRecord" src/ | head
# ② 是否配了死信？没有 DLQ ⇒ 一条毒消息 = 一个分区永久卡死
kafka-consumer-groups.sh --bootstrap-server b:9092 --describe --group order-dim-sync | head -3
# ③ registry 当前兼容级别（Confluent Schema Registry 形态；自研 registry 换成对应 API）
curl -s localhost:8081/config/order_db.order_main-value      # 期望 BACKWARD 或 FULL，绝不该是 NONE
curl -s localhost:8081/subjects/order_db.order_main-value/versions | jq 'length'
# ④ 现场判断：lag 是「增长」还是「冻结」——冻结 = 毒消息，别去扩并发
kafka-consumer-groups.sh --bootstrap-server b:9092 --describe --group order-dim-sync \
  | awk '{print $2, $3, $4, $5, $6, $7}' | head
```

---

## 33.8 三本账：binlog 体积、快照时长、下游放大

三本账，每个都带乘法链与假设清单。所有数字都是**量级结论**，换假设请按步进表重算。

### 账一 · binlog 体积与保留窗口 = 写入行数 × 行大小 × 保留时长

沿用 §33.1 的源库：3,000 事务/s，平均 5 行/事务。

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 行变更速率 | 3,000 txn/s × 5 行 | **15,000 行变更/s** |
| 2 | 单个行镜像 | 行体 300 B + 行头/长度/空位图 ≈ 20 B | 320 B |
| 3 | 平均镜像数（`binlog_row_image=FULL`） | `0.3×1（INSERT） + 0.6×2（UPDATE before+after） + 0.1×1（DELETE）` | 1.6 镜像/行变更 |
| 4 | 行镜像字节 | `15,000 × 1.6 × 320 B` | **7.68 MB/s** |
| 5 | 事件头 + CRC32 | `15,000 × 1.6 × ~29 B` | 0.70 MB/s |
| 6 | 事务级事件（GTID ≈60 B + XID ≈40 B） | `3,000 × 100 B` | 0.30 MB/s |
| 7 | **合计** | `7.68 + 0.70 + 0.30` | **≈ 8.7 MB/s → 750 GB/天** |
| 8 | 保留 3 天 / 7 天 | `750 × 3` / `750 × 7` | **2.25 TB / 5.25 TB** |

| 变体 | 怎么改 | 体积 | 代价 |
| --- | --- | --- | --- |
| 改 `binlog_row_image=MINIMAL` | 平均镜像字节从 512 B 降到 230 B（before 只剩主键、after 只剩变更列） | **≈ 4.2 MB/s → 358 GB/天（−52%）** | **CDC 拿不到旧值** ⇒ 字段级 diff、软删识别、幂等覆盖全失效（§33.3） |
| 开 `binlog_transaction_compression=ON`（MySQL 8.0.20+，默认算法 zstd） | 实测压缩比取决于行间冗余，常见 **2–5×** | **1.7–4.3 MB/s → 150–380 GB/天** | CPU（压缩在写入路径上，摊到每事务）；老版本 connector 需要支持压缩事务载荷 |
| 写入量翻倍 | 步 1 变 30,000 行/s | 1.5 TB/天 → 7 天 10.5 TB | 保留窗口要么缩（位点有效期变短），要么加盘 |

> **量级结论（非承诺值）**。假设：① 事务/行混合比 30%/60%/10%，`binlog_row_image=FULL`；② 行体 300 B 含列值、不含溢出页内容；③ `max_binlog_size` 1 GB 量级切文件、不计文件尾空洞；④ binlog 只在主库一份（从库不开 `log_replica_updates` 就没有第二份）；⑤ 事件头按 25 B + CRC32 4 B 估，取区间中值。**换任一行体大小是最敏感的一步**：行体 1 KB 时总量涨到 ≈ 25.6 MB/s（≈ 2.2 TB/天，约为业务写入量 1.3 TB/天的 1.7×），这时「保留 7 天」会直接吃掉主库盘的一半。

**这一本账的工程口径**：`binlog_expire_logs_seconds` 不是「省盘旋钮」，它是**CDC 的位点有效期**。它的下限由 §33.8 账二给出（快照时长 × 3），上限由磁盘给出。两边一起卡，才是唯一的正确取值方式。

### 账二 · 快照时长 = 表大小 ÷ 导出吞吐 + 锁窗口

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 快照口径的数据量 | 12 张表，表空间合计 1.1 TB；核心表 `order_main` 2.4 亿行 × 1.1 KB ≈ 264 GB 行数据 / 380 GB 表空间 | 1.1 TB |
| 2 | 导出吞吐（单 task，Debezium `initial` 默认串行按表扫） | 实测 8,000–20,000 行/s；取 15,000 行/s × 1.1 KB | **≈ 16.5 MB/s** |
| 3 | 纯导出时长 | `1.1 TB ÷ 16.5 MB/s` | **≈ 66,700 s ≈ 18.5 小时** |
| 4 | 锁窗口（`snapshot.locking.mode=minimal`，默认量级） | 只持到「取位点 + 开快照事务」 | **亚秒级**（< 1 s），可接受 |
| 5 | 锁窗口（`extended`） | 持到快照结束 | **18.5 小时全库只读** ⇒ 事故级，禁止生产 |
| 6 | 锁窗口（`none`） | 0 | **一致性无保证** ⇒ 事故一/形态 ③ |
| 7 | 快照期间源端产生的 binlog | `8.7 MB/s × 66,700 s` | **≈ 580 GB 必须在不被 purge 的窗口内** |
| 8 | 由此得出的位点有效期下限 | `快照时长 × 3`（安全系数：两次失败重试 + 人工介入） | `18.5 h × 3 ≈ 55.5 h ≈ 2.3 天` |
| 9 | 与账一交叉验证 | 保留 7 天 = 5.25 TB；覆盖 55.5 h 只需 `750 × 2.3 ≈ 1.7 TB` | 7 天有 **3 倍余量**，可以接受 |
| 10 | 快照期间的 undo 增长（RR 长事务） | `18.5×3600 s × 15,000 行/s × 320 B` ≈ 320 GB | 必须给 undo 表空间留量（机制 → comp/06） |
| 11 | 想把 18.5 小时压到 2 小时 | 需要 ≈ 9 个并行流 ⇒ 单流吞吐提到 150 MB/s，或分 chunk 并行 | 见下方「并行化的三种做法」 |

| 并行化的做法 | 机制 | 能压到 | 代价 |
| --- | --- | --- | --- |
| 多 task 按表拆（增量侧天然支持） | 每张表一个 task，表之间并行 | 取决于最大单表占比（`order_main` 380 GB ÷ 16.5 MB/s ≈ 6.4 h） | **快照侧默认仍由单 task 串行**（`snapshot.max.threads` 在新版本才有且并非处处可用，以目标版本文档为准） |
| 增量快照 + chunk 并行 | 按主键区间切 chunk（`incremental.snapshot.chunk.size`） | 可把单表压到 1/4–1/8 | 下游必须做去重窗口；chunk 越多信号行越多 |
| 从只读副本导快照 | 快照打到只读副本，不碰主库 | 时长不变，但**不占主库 IO/undo** | 需要副本有 binlog（`log_replica_updates=ON`）或位点仍指向主库 |

> **量级结论（非承诺值）**。假设：① 单 task 吞吐 16.5 MB/s（行式 JDBC 读 + 序列化 + Kafka 写，取 8,000–20,000 行/s 的中值）；② 表空间与行数据比 1.44（含索引与页填充）；③ 快照期间源库写入速率不变；④ `snapshot.locking.mode=minimal`；⑤ 不计网络抖动与 Kafka 侧限流。**最敏感的是步 2**：把 `snapshot.fetch.size` 调小（默认 1,024 行量级）会让每批多一次往返，实测可能把吞吐压到 5,000 行/s → 快照变 55 小时，位点有效期下限跟着涨到 7 天 —— **「快照慢」和「binlog 保留不够」是同一件事故的两面。**

### 账三 · 下游放大 = 每变更消息数 × 分区副本数 × 保留天数

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 消息条数 | 15,000 行变更/s；DELETE 占 10% 且开墓碑 ⇒ `15,000 × (1 + 0.1)` | 16,500 条/s |
| 2 | 单条 payload | `1.6 镜像 × 320 B`（before + after） | ≈ 512 B |
| 3 | envelope 里的 `source` 块 | GTID/file/pos/`ts_ms`/库表名等 | ≈ 120 B |
| 4 | **`schemas.enable=true` 的内嵌 schema**（JSON converter 默认行为） | 12–13 列的表，schema 文档 2.5–4 KB/条 | **+3.0 KB** |
| 5 | 现象级结论 | 一条 512 B 的业务变更 → 一条 3.6 KB 的消息 | **放大 7×** |
| 6 | 关闭内嵌 schema（`schemas.enable=false`） | 消息只留 payload + envelope | ≈ 900 B/条（放大 1.8×） |
| 7 | 换 Avro + registry（schema 只存 ID） | ≈ 600 B/条（5 B schema id + 紧凑二进制） | 放大 1.2× |
| 8 | 字节率（三种序列化） | 步 1 × 步 5/6/7 | **59 MB/s / 14.9 MB/s / 9.9 MB/s** |
| 9 | 日增量 | × 86,400 s | **5.1 TB / 1.29 TB / 0.86 TB** |
| 10 | × RF=3（Kafka 物理副本） | × 3 | **15.3 TB / 3.9 TB / 2.6 TB** |
| 11 | × 保留 3 天 | × 3（步 10 已是 1 天的物理量） | **46 TB / 11.6 TB / 7.8 TB** |
| 12 | `cleanup.policy=compact` 的重写放大 | 后台重写段，脏段比例高时 1.5–2× | +50%～100%（→ comp/28 §28.6） |

> **量级结论（非承诺值）**。假设：① `schemas.enable` 与序列化器按 Debezium 2.x + Kafka Connect 的默认行为；② 13 列的普通业务表，schema 文档 3 KB（列数越多、`source` 块越大，这项按列数线性涨）；③ RF=3、无压缩（开 lz4/zstd 通常再砍 2–3×，但压缩比依赖批内相似度）；④ 保留 3 天、不吃 compaction 重写。**这张表最值钱的一行是步 5**：**默认配置下，Kafka 里 85% 的字节是「每一条消息都重复一遍的表结构」。**

**三本账的合订结论（一句）**：**源端账（binlog 750 GB/天）在「保留窗口」上被 CDC 绑住，Kafka 侧账（放大 7× 后 5.1 TB/天）在「序列化选择」上被 CDC 绑住，快照账（18.5 小时）在「位点有效期」上把它俩绑在一起。** 三本账不是独立的，它们共用同一个变量：**时间**。

```python
# 三本账压成一个可复算脚本（改任一假设直接重跑）
GB, TB = 1e9, 1e12

def binlog_per_day(txn_s=3000, rows_per_txn=5, row=320, mixt=(.3,.6,.1), hdr=29, tx_hdr=100):
    """账一：binlog 体积 = 行数 × 行大小 × 镜像数 + 事件头 + 事务头"""
    rows = txn_s * rows_per_txn
    imgs = mixt[0]*1 + mixt[1]*2 + mixt[2]*1
    body = rows * imgs * row
    head = rows * imgs * hdr + txn_s * tx_hdr          # 每事件头 + 每事务头
    return body + head, (body + head) * 86400

def snapshot_hours(data_tb=1.1, row_bytes=1100, rps=15000):
    """账二：快照时长 = 表大小 ÷ 导出吞吐；顺手给出位点有效期下限"""
    mbps = rps * row_bytes / 1e6
    hours = data_tb * 1e12 / (mbps * 1e6) / 3600
    return mbps, hours, hours * 3 / 24                 # 秒/行 → MB/s → 小时 → 天

def kafka_tb_per_day(rows_s=15000, del_ratio=.1, payload=512,
                     envelope=120, schema=3000, rf=3, keep=3):
    """账三：下游放大 = 消息数 × 单条字节 × 副本 × 保留"""
    msgs = rows_s * (1 + del_ratio)
    per  = payload + envelope + schema
    tb_day = msgs * per * 86400 / 1e12
    return per, tb_day, tb_day * rf * keep

burst, day = binlog_per_day()
print(f"账一 binlog {burst/1e6:.1f} MB/s → {day/GB:.0f} GB/天，保留7天 {day*7/TB:.2f} TB")
for s in (0, 3000):
    per, d, p = kafka_tb_per_day(schema=s)
    print(f"账三 schema={s:>4}B：单条 {per}B → {d:.2f} TB/天，RF3×3天 {p:.1f} TB")
mbps, h, days = snapshot_hours()
print(f"账二 快照 {mbps:.1f} MB/s → {h:.1f} 小时；位点有效期下限 {days:.1f} 天")
print(f"     快照期间 binlog ≈ {burst*1e-6*3600*h/1e3:.0f} GB 必须保留")
```

---

## 33.9 架构边界：CDC 能保证什么、不能保证什么

| 想要的能力 | CDC 给不给 | 物理原因 | 正确做法 |
| --- | --- | --- | --- |
| **单表内变更顺序** | ✅ 给 | 一个源的日志是线性的，且 key=主键 ⇒ 同 key 同分区（→ comp/28） | 确保 key 是主键；不要中途改分区数（→ comp/30） |
| **捕获变更前后值** | ✅ 给（有条件） | 前提是 `binlog_row_image=FULL` / PG `REPLICA IDENTITY FULL` | 源端配置纳入接入体检（§33.3） |
| **捕获硬删除** | ✅ 给 | `DELETE_ROWS` 事件 | 下游必须处理墓碑（value=null） |
| **至少一次** | ✅ 给 | 位点落盘是周期性的（§33.4 五个来源） | 下游按主键 + 源端时间戳幂等（§33.6） |
| **恰好一次（端到端）** | ❌ 不给 | 「Kafka 内的 offset 一致性」与「你下游那个库的写入」是两个事务 | 至少一次 + 幂等；真正的端到端 EOS 要靠流引擎的状态（→ comp/34） |
| **跨表事务的全局顺序** | ❌ 不给 | 事件按表分 topic ⇒ 并行消费 ⇒ 跨 topic 无顺序；即使同一 topic，多分区也打散 | 同一聚合根放同一表同一 key；跨表一致性用「源端事务 id + 下游按事务分组」或干脆只保因果不保全局 |
| **schema 自动兼容** | ❌ 不给 | schema 是契约，兼容性是**下游代码**的属性，不是传输层的属性 | registry 定级 + 下游容忍未知字段 + DDL 与发版错天（§33.7） |
| **源端 purge 后回溯** | ❌ 不给 | 位点是「有效期内的位置」（§33.3） | 位点有效期 ≥ 最坏停机 + 快照时长；兜底是备份 + binlog 归档 |
| **快照是「瞬时」的** | ❌ 不给 | 快照本身是一个长事务，要几小时 | 承认它是一段窗口；用增量的方式（watermark）而不是瞬时的方式去思考 |
| **对源库零影响** | ❌ 不给 | 快照读会占 IO/undo；信号表要写；binlog 要保留 | 打到只读副本；避开高峰；chunk 化让单次读变小 |

### 八问里最容易写错的两格

| 八问 | 本篇的答案 |
| --- | --- |
| **⑤ 扩展维度** | **只有「分片」一条**：增量侧按表/按库拆 connector 与 task（表与表之间并行），快照侧按主键区间切 chunk。**副本不买吞吐**（多一套 connector 只是多一份重复消费）；**垂直切**在「单表 TB 级 + 单 task 串行」面前是唯一解（把大表拆小表）；**批处理摊薄**只在导出侧有效（`snapshot.fetch.size` 与批量大小），但它改的是往返次数而不是总字节。直说一句：**CDC 的吞吐上限 = 单 task 单表导出吞吐 × 可并行的表数/分片数，没有「加机器就线性」这回事。** |
| **⑥ 系统级失效** | **只有三类**：① **数据不一致** —— 位点方向错导致的丢失或重复（事故一）、快照不一致（形态 ③）、schema 变更导致的静默错（事故二下游 ③）；② **链路不可用** —— 源端 purge 后位点失效、PG slot 被作废、Mongo resume token 被覆盖，全部只能重做全量（事故三）；③ **成本失控** —— binlog 保留 × 下游放大 7× × 保留天数的乘法链（§33.8）。使用级错误（配置写错、DDL 乱发）归 §33.10，两节不互相稀释。 |

### 什么时候该换掉它

| 信号 | 阈值 | 换成什么 |
| --- | --- | --- |
| 每次 DDL 都要停链 | 一个月内因 schema 停链 ≥ 2 次 | 先补 registry 定级与下游容错；仍不行则考虑「双写 + 比对」或湖表侧接管（→ architect/21） |
| 快照时长 > 位点有效期 / 3 | §33.8 账二步 8 | 上增量快照 + chunk 并行；或从只读副本导快照 |
| 单表 > 订单表的 5 倍且不允许长事务 | 快照 RR 期间 undo 增长 > undo 表空间 50% | 改用「按主键区间分批 + 记录每批边界」的自研快照（本质是自己实现 watermark） |
| 需要跨多库一致快照 | 跨库对账差成为常态 | 上游统一写一份 outbox/事件表，CDC 只读一份（→ eng/10 的 outbox 思路）；或改湖表 + 统一时间旅行（→ architect/21） |
| 需要端到端 exactly-once | 下游是账务类状态机 | 引入流引擎，用 checkpoint 对齐快照与流（→ comp/34） |

---

## 33.10 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到物理形态） | 怎么定位 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 数仓某些行的某些字段永远是旧值，无报错 | 位点取晚：位点晚于快照时刻，中间窗口既不重放、也不在快照里（事故一） | 取一个已知被改过的行，比对数仓值与源库 `information_schema` 或手工变更记录 | 从「快照时刻的位点」重放补数，或重做快照 | 快照与位点必须由同一个机制给出；手工拼管道时用 `--source-data=2`（位点早于快照，靠幂等吸收重复） |
| 下游计数类指标偏高，行数正确 | 位点取早 / 位点回退 ⇒ 重复投递；下游做的是 `+= 1` 而不是覆盖 | 对比「每个 key 的消息条数」与源端变更次数 | 把下游写改成幂等覆盖（按主键 + `src_ts`） | 下游写规范：CDC 链路上禁止计数器式写入 |
| 有积压但消费速率是 0，lag 冻结在某个 offset | 反序列化抛异常 ⇒ 位置不提交 ⇒ 无限重投同一条毒消息 | `--describe` 看 lag 是否长时间完全相同；看 consumer 日志里的异常类型 | 上死信 + 容忍未知字段（§33.7 步①） | 所有 CDC 消费者默认带 `ErrorHandlingDeserializer` 一类封装 + DLQ |
| 加了一列，三套下游同时出问题 | DDL 传播速度远超发版速度；各下游对「多一个字段」的容错能力不同 | 检查 registry 兼容级别、反序列化器配置、Flink SQL 建表列清单 | 按 §33.7 六步止血 | DDL 与消费者发版不同天；加列必须 NULL/带默认值 |
| connector 突然 FAILED，报找不到 binlog 文件 | 位点落在保留窗口之外（`expire_logs_seconds` 调小 / 手工 `PURGE`） | `SHOW BINARY LOGS` 的最早文件与 connector 位点比较 | 只能重做全量（或从备份 + binlog 归档补） | **位点有效期 ≥ 最坏停机 + 快照时长**；禁止手工 PURGE；停机维护清单里带上 CDC |
| PG 主库写入失败、`pg_wal` 目录爆 | 一条不用的逻辑复制槽钉住 WAL，永不回收 | `pg_replication_slots` 里 `active=false` 且 `retained` 在涨 | `pg_drop_replication_slot()`，先确认没有消费者需要它 | `max_slot_wal_keep_size` 设一个上限 + 死槽告警 |
| connector 重启后就从头重做全量 | `snapshot.mode=initial` + `connect-offsets` 里的位点丢了（topic 被删/换了集群） | 查 `connect-offsets` 里该 connector 的 key 是否存在、时间戳 | 从备份恢复 `connect-offsets`；否则只能重做（并做好下游去重窗口） | `connect-offsets`/`connect-configs`/`connect-status` 三个 topic 纳入备份与保留白名单（绝不能设短保留） |
| 快照做完后下游数据仍然是旧的 | 快照与流并行、下游无 watermark 去重 ⇒ 旧快照覆盖了新值（形态 ④） | 抽查「快照期间改过的行」在下游的值 | 下游实现 (L,H] 去重窗口，或改用内置增量快照 | 自研快照必须自带边界标记（否则不要自研） |
| `DECIMAL` 精度变更后 Avro consumer 全停 | `decimal.precision` 变化导致 schema resolution 失败 | consumer 日志 `SerializationException`；registry 里 version 对比 | 临时降兼容级别放行 + 下游升级 reader | 精度/类型变更视为**破坏性 DDL**，走单独发布窗口 |
| 墓碑（value=null）打死了下游 | 下游假设 value 非空；DELETE 产生 2 条消息 | 抓一条 DELETE 前后的两条消息看 | 显式处理 `null` | 消费者规约里写死：`value == null` = 删除 |
| 换个新连接器版本后一切正常，但天后再看数据错位 | 只重做了 offset 没重做 schema history（或反之） | 比对 `relevant` 位点与 schema history 的最后一条 DDL | 两者必须一起回退/一起重做 | 备份时**成对**备份这两个 topic |

---

## 33.11 观测：五个指标能提前十分钟报警

| 指标 | 来源 / 版本与开关前提 | 拐点（越线就动手） | 它度量的是哪件物理事实 |
| --- | --- | --- | --- |
| **`MilliSecondsBehindSource`**（streaming context） | Debezium JMX，`debezium.<connector>:type=connector-metrics,context=streaming`；需开 JMX exporter 或 `metrics.enabled` 一类开关，具体 MBean 名以目标版本为准 | > 60,000（1 分钟）持续 5 分钟；或**曲线变平但 > 0**（这是最危险的形状：卡在毒消息上） | 「源端最近一次提交」与「我已发出的最后一条」之间的时间差 |
| **快照进度**：`RowsScanned` / `RemainingTableCount` / `TotalTableCount` / `SnapshotCompleted` | Debezium JMX，`context=snapshot`；快照期间才有值 | `RowsScanned` 5 分钟不涨 = 快照卡住；`RemainingTableCount` 不下降 = 某张表卡住 | 快照的「进度条」，也是「位点有效期还剩多少」的依据 |
| **源端位点距保留边界** | MySQL：`SHOW BINARY LOGS` 的最早文件 vs connector 位点（`connect-offsets` topic 里读）；PG：`pg_replication_slots.retained`；Mongo：`rs.printReplicationInfo()` 的 oplog window | 剩余窗口 < **3 × 快照时长**即 P1；PG `retained` > `pg_wal` 容量 30% 即 P1 | **位点有效期还剩多少** —— 这个数比 lag 重要，因为它决定「还能不能续上」 |
| **`NumberOfDisconnects` / `Connected`**（streaming context） | 同上；连续断连通常是网络/权限/`server_id` 冲突 | 5 分钟内 ≥ 2 次断连 | 连接稳定性（也暴露 `server_id` 撞车、从库踢连接） |
| **队列余量**：`QueueRemainingCapacity` / `MaxQueueSizeInBytes` | streaming context；队列满 = connector 被下游拖住 | 余量 < 20% 持续 1 分钟 | 反压：Kafka 写不进去 ⇒ 位点不前进 ⇒ 位点有效期在流失 |
| **schema 错误计数** | Connect task 状态 `FAILED` + 下游 `SerializationException` 计数（应用侧打点） | 任意 1 条即 P2；持续 1 分钟即 P1 | 契约破坏（§33.7） |
| **偏移提交新鲜度** | `connect-offsets` topic 里该 connector key 的最后写入时间 | > 5 分钟没更新 = 位点没在推进（哪怕 lag 看起来正常） | 「位点是不是真的在前进」的唯一真相 |

```bash
# 值班第一屏：五条命令，回答「能不能续上」而不是「快不快」
# ① MySQL：位点还剩多少窗口（最早文件 vs connector 位点）
mysql -e "SHOW BINARY LOGS" | head -3
mysql -e "SHOW BINARY LOG STATUS\G" | grep -E 'File|Position|Executed_Gtid_Set'   # 8.2+；更早用 SHOW MASTER STATUS

# ② PostgreSQL：slot 扣住了多少 WAL（死槽是最常见的主库杀手）
psql -Atc "SELECT slot_name, active,
             pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)), wal_status
           FROM pg_replication_slots ORDER BY 3 DESC;"

# ③ 位点是否在前进：读 connect-offsets 里该连接器的 key（compacted topic，最新值即可）
kafka-console-consumer.sh --bootstrap-server b:9092 --topic connect-offsets \
  --property print.key=true --property print.value=true --from-beginning \
  | grep order_db | tail -3

# ④ 下游 lag 是「增长」还是「冻结」（冻结 = 毒消息，去扩并发没用）
kafka-consumer-groups.sh --bootstrap-server b:9092 --describe --group order-dim-sync \
  | awk 'NR==1 || $6+0 > 0'

# ⑤ 快照是否在动（连跑两次比 RowsScanned）
curl -s localhost:8080/actuator/prometheus 2>/dev/null | grep -i debezium | head -5

# 告警骨架（Prometheus 风格，阈值取自本表拐点列；指标名按你的 exporter 映射）
# a) 卡死型：lag 不为 0 但 5 分钟内没有变化
#    max_over_time(debezium_streaming_millisecondsbehindsource[5m]) > 60000
#      and deriv(debezium_streaming_millisecondsbehindsource[10m]) == 0
# b) 位点有效期：剩余窗口 < 3 × 快照时长
#    cdc_source_retention_hours < 3 * cdc_snapshot_hours
# c) 死槽：active=0 且 retained 在涨
#    pg_replication_slots_active == 0 and deriv(pg_replication_slots_retained_bytes[15m]) > 0
# d) 位点不再前进
#    time() - cdc_offset_last_write_timestamp_seconds > 300
```

---

## 33.12 一页速记

| 记什么 | 具体结论 |
| --- | --- |
| 一句话定位 | CDC = **一个位点串起来的两种数据源**：某一时刻的全量快照 + 该时刻之后的事务流 |
| 唯一真问题 | 难点不是解析 binlog，是**快照与增量在同一个位点上的衔接** + **schema 变更不打死下游** |
| 位点的方向性 | 位点**晚于**快照 ⇒ 丢；**早于**快照 ⇒ 重；两者都不报错 |
| 三种源端的本质 | MySQL binlog（线性文件）/ PG WAL + slot（不推进就不回收）/ Mongo oplog（环形覆盖）；**回溯能力 = 保留窗口** |
| 为什么必须 ROW | STATEMENT 只有 SQL 原文，没有 before/after；`MINIMAL` 只有主键，拿不到旧值 ⇒ 必须 `ROW` + `FULL` |
| 三份外部状态 | offset（`connect-offsets`）+ schema history（`schema.history.internal.kafka.topic`）+ 数据流；**前两者必须成对备份** |
| 重复的五个来源 | offset 刷新周期 / 快照重来 / task 重分配 / 源端重放 / 非幂等下游；吸收点只有一个：**主键 + 源端时间戳的覆盖写** |
| 增量快照的正确性 | 「至多重复一次、绝不丢失」是**下游 (低水位, 高水位] 去重窗口**买来的，不是 connector 单方面给的 |
| 三本账 | binlog 750 GB/天（保留 7 天 = 5.25 TB）；快照 18.5 h（位点有效期下限 = 3×）；下游放大 7×（默认 `schemas.enable=true`） |
| schema 五跳 | 源库 → binlog → schema history → registry/converter → 下游代码；**任何一跳没有「容忍未知 + 有默认值 + 有死信」，整链停** |
| 首要指标 | 源端**位点有效期剩余**（比 lag 重要）、`MilliSecondsBehindSource` 是否变平、快照 `RowsScanned` 是否在涨、PG slot `retained`、`connect-offsets` 的新鲜度 |

**如果只记五句：**

1. **位点是一个有方向、有有效期的位置。** 晚于快照时刻就丢，早于就重；窗口耗尽就只能重做全量。这两件事都不报错。
2. **快照的一致性来自隔离级别与锁窗口，不来自导出工具。** RR / exported snapshot 买到一致性但产生长事务（undo 与 purge 的代价）；FTWRL 买到最强一致性但买到的是全站停写。
3. **CDC 只承诺至少一次，且重复有五个来源。** 效果恰好一次 = 「主键 + 单调的源端时间戳 + 覆盖写」这一对，缺一不可。
4. **schema 变更是跨五个进程的契约变更。** DDL 传播只要 40 秒，你的发版要一天 —— 所以要么让下游容忍未知字段并配死信，要么把 DDL 与发版错开。
5. **三本账共用同一个变量：时间。** binlog 保留决定位点有效期，快照时长腐蚀它，下游放大把字节变成钱。

---

## 33.13 自检题（5 题）

**Q1.** 团队说「我们已经用 `mysqldump --single-transaction` 导了全量，Debezium 用 `snapshot.mode=never` 只做增量，两边都配好了」。请判断这套方案会不会丢数据、会不会重复，并给出一个不需要改工具的修法。

<details><summary>答案</summary>

**关键不是「配好了没有」，是「两份位点是否是同一个时刻」。**

`--single-transaction` 的语义是：先 `FLUSH TABLES WITH READ LOCK` 一瞬间，记下 binlog 位点，再 `START TRANSACTION WITH CONSISTENT SNAPSHOT` 并释放锁。所以 **dump 文件头里的位点早于快照时刻**。

因此：

- **不会丢**（位点比快照旧，中间所有变更都会被重放）。
- **会重复**：位点到快照时刻之间的变更，既已体现在 dump 数据里，又会被增量流再投一遍。
- **但前提是 Debezium 的起点确实用的是 dump 里那个位点。** 如果像事故一那样让它自己取「当前位点」，那就变成「位点晚于快照」⇒ **丢**。

**不改工具的修法**：把 dump 文件头里的 `CHANGE REPLICATION SOURCE TO ... SOURCE_LOG_FILE/POS`（`--source-data=2` 会把它写成注释）作为 Debezium 的初始位点（`snapshot.mode=schema_only` + 手工指定位点，或以 `schema_only` 启动后由运维写入 `connect-offsets`），并把下游写改成「按主键 + `src_ts` 覆盖」以吸收重复。

**代价与验收**：验收标准不是「数据条数对上」，而是「取一个 dump 期间被改 3 次的行，下游应当是最后那个值」。**量级结论（非承诺值）**：按 §33.8 账二，1.1 TB 的 dump 需要几小时，这几小时的重复量 = 15,000 行/s × 时长，必须由幂等吸收。

</details>

**Q2.** 源库 3,000 事务/s、平均 5 行/事务、行平均 300 B。运维想省盘，把 `binlog_row_image` 从 `FULL` 改成 `MINIMAL`。请算出 binlog 省了多少，并说明 CDC 会失去什么。

<details><summary>答案</summary>

**省了多少**（按 §33.8 账一的步进）：

- `FULL`：平均镜像字节 = `0.3×320 + 0.6×640 + 0.1×320 = 512 B/行变更` ⇒ 行镜像 `15,000 × 512 = 7.68 MB/s`，含事件头与事务头合计 **≈ 8.7 MB/s → 750 GB/天**。
- `MINIMAL`：UPDATE 的 before 只剩主键（≈60 B）、after 只剩变更列（假设约 110 B）⇒ `0.3×320 + 0.6×170 + 0.1×320 = 230 B/行变更` ⇒ 行镜像 `3.45 MB/s`，合计 **≈ 4.2 MB/s → 358 GB/天**。
- 结论：**省 52%，7 天从 5.25 TB 降到 2.5 TB。**

**失去什么**：① UPDATE 事件没有完整 before 镜像 ⇒ 下游拿不到「改之前是什么」，字段级 diff、软删识别、状态机校验全部失效；② 幂等方案被削弱（很多实现靠比较 before/after 判断是否重复）；③ 部分 connector/工具直接不支持（Debezium、Canal 都要求 `FULL`）。

**正确的省盘顺序**（按代价从小到大）：① 缩短保留窗口（但必须先满足「位点有效期 ≥ 最坏停机 + 3× 快照时长」）；② 开 `binlog_transaction_compression`（8.0.20+，通常 2–5×，不损失语义）；③ 把 binlog 放到单独的盘/归档；④ 最后才谈 `MINIMAL` —— 但那等于关掉一半 CDC。

**量级结论（非承诺值）**，假设同 §33.8 账一。

</details>

**Q3.** 一个 PG 源库突然报「磁盘写满」，排查发现有一条 3 周前创建的、`active=false` 的逻辑复制槽。请解释机制、给出处置，并说明为什么「删掉槽马上就好了」这句话只说对了一半。

<details><summary>答案</summary>

**机制**：逻辑复制槽用 `restart_lsn` 钉住 WAL —— 只要槽不推进，PostgreSQL **就不会回收它之后的任何 WAL 段**。这是为了让消费者随时能回来接着读。于是「消费者不来了」变成了「WAL 永远不删」，`pg_wal` 单调增长直到写满（`max_slot_wal_keep_size` 是 PG 13+ 引入的兜底闸门，默认 `-1` = 不限，具体默认值以目标版本文档为准）。

**处置顺序**：① 先确认没有别的消费者依赖它（`active=false` 说明当前没人在用，但可能只是暂停）；② 若确认废弃 → `pg_drop_replication_slot('xxx')`；③ 若还要用 → 让消费者立刻追平（它会推进 `confirmed_flush_lsn` ⇒ 释放 WAL）；④ 应急时先扩盘/把 `pg_wal` 移到大盘，避免「边删槽边写满」。

**为什么只说对了一半**：删槽**只解决盘**，不解决**数据**——槽对应的那份 CDC 已经从 3 周前断流，它的位点早就超出了任何合理的重放窗口；即使重连成功，你需要的是**重做快照**（因为 PG 逻辑解码不保留历史，槽一放弃就没有「从中间续上」这回事）。所以正确的表述是：**删槽止住了主库的血，但下游那份数据要按「重做快照 + 去重窗口」重新处理。**

**防复发**：`max_slot_wal_keep_size` 设上限 + 「`active=false` 且 `retained` 在涨」的告警 + 槽清单纳入变更评审（谁申请的、给谁用、什么时候到期）。

</details>

**Q4.** 一条 DDL 同时做了两件事：给 13 列的表加一列 `channel_code VARCHAR(16) NULL`，以及把 `amount DECIMAL(12,2)` 改成 `DECIMAL(14,2)`。请分别说明这两件事对 Debezium、schema registry、三类下游（Jackson 严格模式 / Avro `SpecificRecord` / Flink SQL）的影响，并给出发布顺序。

<details><summary>答案</summary>

**逐项影响**：

| 变更 | Debezium | registry（`BACKWARD`） | Jackson 严格模式 | Avro `SpecificRecord` | Flink SQL（`debezium-json`） |
| --- | --- | --- | --- | --- | --- |
| 加 `channel_code`（可空） | 新 schema 多一列，事件多一个字段 | ✅ 通过（带默认值的可选字段） | ❌ **抛 `UnrecognizedPropertyException`** ⇒ 分区卡死 | ✅ 通过（reader 忽略/填默认） | ⚠️ 默认忽略未知字段 ⇒ **静默缺字段** |
| `DECIMAL(12,2) → (14,2)` | 新 schema 的 `decimal.precision` 变了 | ❌ **resolution 失败** | 通常无感（按字符串/数字解析） | ❌ **`SerializationException` ⇒ 全停** | 可能读成 `null` 或精度错 |

**发布顺序**（不改这条 DDL 就能过的方法是不存在的，所以顺序比改法重要）：

1. **先改下游的容错**：Jackson 关掉 `FAIL_ON_UNKNOWN_PROPERTIES` 并加默认值；Avro consumer 换 `GenericRecord`/加默认值；Flink SQL 补上新列。**这一步必须早于 DDL。**
2. **再评估 registry 兼容性**：精度变更会被 `BACKWARD` 拒绝 ⇒ 要么临时降级到 `NONE`（记到期回滚），要么把精度变更拆成单独的 DDL 与新 subject。
3. **拆 DDL**：把「加列」（可逆）与「改精度」（不可逆、且走 COPY 重建表）**分成两次发布**，第二次放到低峰并预留锁窗口（2.4 亿行 ≈ 41 分钟只读）。
4. **发布后验证**：取一条被改过的行，确认下游三处都对；确认 lag 在增长而不是**冻结**。
5. **写完纪律**：加列一律 `NULL` 或带默认值；精度/类型/改名视为破坏性 DDL，单独窗口 + 双人复核。

**量级结论**：真正的成本在「41 分钟表只读」与「期间积压 = 15,000 行/s × 2,460 s ≈ 3,700 万行」，不在 schema 本身。

</details>

**Q5.** 面试官问：「我们下游消费 CDC 消息，lag 一直是 0，但数据不对；另一条链路 lag 一直涨，我加了 10 个消费者也没用。这两件事分别说明什么？」请用本篇的概念回答，并各给一个可验证的检查动作。

<details><summary>答案</summary>

**第一件（lag=0 但数据不对）**：lag 度量的是「位点追平了没有」，与「值对不对」无关。三个典型原因：
- **位点方向错**（位点晚于快照时刻）⇒ 中间那段变更从未被投出，lag 当然追得上（事故一）；
- **重复/乱序被写成非幂等**（计数型写入）⇒ 条数对不上但位点对得上；
- **schema 静默错**（Flink 忽略未知字段、金额读成 null）⇒ 数据在流但内容错。

检查动作：取一条**已知在快照期间被修改 3 次**的行，用它的 `source.ts_ms` 与 `before`/`after` 三个值，比对下游最终值；同时看下游表的行数 ≠ 源表行数时差在哪几张表。

**第二件（lag 涨，加消费者无效）**：先分辨「增长」还是「冻结」，以及「是不是并行度问题」。
- 若 lag **冻结**在某个 offset ⇒ 毒消息，加消费者完全无效（分区数不变，多出来的消费者空转）—— 这是反序列化/墓碑 NPE 的典型形状；
- 若 lag **增长**但消费速率是 0 ⇒ 同样是单分区卡住；
- 若 lag 增长且消费速率正常但追不上 ⇒ 才是吞吐/并行度问题（分区数 < 消费者数时，加人无用 → comp/30）。

检查动作：`kafka-consumer-groups.sh --describe` 连跑两次，看 `CURRENT-OFFSET` 是否完全没变（冻结）还是缓慢增长；再看 consumer 日志里的异常类型；最后确认 topic 分区数是否 ≥ 消费者数。

一句话总结：**CDC 的 lag 只能告诉你「流到哪了」，告诉不了你「值对不对」。** 值对不对要靠三样东西：源端位点有效性、下游幂等写、schema 契约检查。

</details>

---

## 33.14 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| binlog 所在的「第三个日志」：redo/undo/binlog 与内部 2PC，本篇只用了它的输出 | `→ comp/08` |
| RR 快照与 undo 版本链：为什么一个 18.5 小时的快照会让 undo 涨 320 GB | `→ comp/06` |
| Kafka 侧的段、稀疏索引、compaction 与保留策略：CDC 输出端的物理形态 | [28 Kafka 的日志](28-kafka-log-segments.md) |
| ISR、HW、acks：CDC 写 Kafka 时「不丢」的那组参数 | [29 复制与一致性](29-kafka-replication-isr.md) |
| 消费组、rebalance、位点提交时机：lag 冻结 vs lag 增长的判别与治理 | `→ comp/30` |
| 投递语义（至少一次 / 至多一次 / 恰好一次的真实边界）与幂等三个落点 | [eng/24 投递语义](../eng/24-delivery-semantics.md) |
| 顺序与积压的机制细节（分区键 = 保序键 = 幂等键） | `→ eng/25` |
| 消息可靠性八环节、丢了怎么查 | `→ eng/26`、`→ eng/27` |
| 事务消息与 outbox：当 CDC 不适合做「业务事件」时的替代路径 | `→ eng/10` |
| 埋点管道与数据闭环：CDC 在整个数据链路里的位置与形态选择 | `→ architect/11` |
| 数据湖表格式：CDC 的落库端为什么最终会走向快照 + 清单（→ 第 35 篇的内核） | `→ architect/21`、`→ comp/35` |
| Flink 的 checkpoint 与状态后端：用流引擎对齐「快照 + 增量」的第三条路 | `→ comp/34` |
| 定时与延迟：CDC 之外的另一类「到点才发现」的问题 | `→ comp/36` |
| 组件故障谱：本篇三类事故在「慢 / 满 / 脑裂 / 假活 / 语义漂移」里的归类 | `→ comp/52` |
| 本套总纲：解剖卡八问、三本放大账与写作纪律（版本前提 / 量级结论 vs 承诺值） | `→ comp/0-summary` §0.4、§0.7 |
