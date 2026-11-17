# 10. 主从复制与高可用：半同步、并行回放与 MGR

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · 关系库与事务内核 ｜ **编号**：10 / 53
> **本篇定位**：全仓唯一一处把「binlog 之后发生的事」拆到线程级的地方。binlog 的**格式**、`fsync` 时机、内部 2PC 与组提交，属于 `comp/08` 的地盘，本篇一律不重写。本篇只回答一个问题：**一条已经落在主库 binlog 里的事务，是怎么被搬到从库、怎么被回放、又是在哪些环节被"丢一点、慢一点、裂一次"的**。读完它，「半同步」这三个字会变成一个带失效窗口的配置组合，「复制延迟」会变成一个可以从行数与每行开销算出来的数，「切换」会变成一个必须先回答"不丢还是不停"才能执行的流程。
> **读法**：救火直接读 §10.4 的「故障时刻能丢多少」判定表与 §10.11 的坑清单；做架构决策读 §10.7 的 3 节点 vs 5 节点表与 §10.10 的边界表；被追问"延迟为什么这么大"读 §10.5 与 §10.6，这两节把"延迟"从一个形容词变成一个算式；§10.3 的链路图和 §10.8 的七步切换时序图可以单独贴进评审材料与演练脚本。

---

## 10.1 引子：三起「状态全绿，数据已经分叉」的故障

复制的故障有一个共同形状：**监控面板是绿的，因为它盯的那个数本身在撒谎**。挂掉的主从不是你配置的那条链路，而是"你以为它在同步"这个信念。

### 事故一：半同步超时静默退化为异步，主库宕机丢了 162 秒订单（2025-09-23）

某支付订单库：MySQL 8.0 单主 + 1 从库，稳态 **2,400 TPS**，平均每事务 binlog **1.6 KB**。半同步开着（`rpl_semi_sync_source_enabled=ON`），但**没人改过 `rpl_semi_sync_source_timeout`，默认 10,000 ms**。

当晚 03:02，运维在从库上手工跑历史报表导出，从库 buffer pool 被冲刷，`sync_relay_log=1` 下 ack 路径要等 relay log 的 `fsync` —— 一次磁盘背压就让 ack 延迟从 0.6 ms 涨到 12 s。

| 时间 | 事件 | `Rpl_semi_sync_source_status` | 从库 relay log 位点 | 延迟感知 |
| --- | --- | --- | --- | --- |
| 09-23 03:02:00 | 从库开始跑报表导出，buffer pool 命中率 99.2% → 61% | ON | 追平 | 0.6 ms |
| 09-23 03:02:29 | 一个事务等 ack 超过 10 s，**主库自动降级为异步**，只翻了一个状态位 | **OFF** | 停在 03:02:29 | 无告警 |
| 09-23 03:03:00 | 主库继续以 2,400 TPS 提交，binlog 照写，没人等 ack 了 | OFF | 落后 31 s | 无告警 |
| 09-23 03:05:20 | 主库宿主机 kernel panic（网卡驱动缺陷），30 s 后确认不可恢复 | OFF | 落后 **171 s** | — |
| 09-23 03:07:10 | 提升从库，业务恢复 | — | — | — |
| 09-23 复盘 | 丢失 **03:02:29–03:05:20 = 171 s × 2,400 = 41 万个事务**，其中订单写 **21,400 笔**；对账 4 天、客诉 1,700 起、赔付与人力 ≈ 46 万元 | — | — | — |

三件事同时缺失：**把"半同步"当成了一个布尔开关**（它其实是一个带超时的、会自动放弃的等待）；**没有对 `no_tx` 与 `status=OFF` 告警**（降级这件事在 MySQL 里只翻状态位、不进错误日志）；**在从库上跑报表**（报表不是"只是读"，它把回放路径的缓存与 IO 全抢走）。

### 事故二：`Seconds_Behind_Master=0`，价格接口读到 26 分钟前的数据（2025-06-11）

某电商价格/库存库：主库峰值 **2,000 TPS**，从库 `replica_parallel_workers=8`、`replica_preserve_commit_order=ON`，读流量全部走从库。监控只有一个指标：`Seconds_Behind_Master`。

13:40，运营跑「全站调价」批量任务，为了"快"，写成一个事务改 **190 万行**。主库 13:40–14:06 提交这一个事务；从库 8 个 worker 里只有 1 个在啃它，其余 7 个因为提交顺序约束停在提交点，等于并行度 1。

而这 26 分钟里，`Seconds_Behind_Master` 一直在 **0–4 s** 之间跳：它比较的是「协调线程当前读到的 relay log 事件时间戳」与「从库系统时间」，而协调线程早就把 relay log 读到最新了 —— **它读得快，不代表 worker 干得快**。

| 时间 | 事件 | `Seconds_Behind_Master` | pt-heartbeat | GTID 差集 |
| --- | --- | --- | --- | --- |
| 13:40 | 主库开始 190 万行单事务调价 | 1 s | 1 s | 0 |
| 13:44 | 从库唯一在跑的 worker 卡在该事务第 40 万行 | **2 s** | 240 s | 8,100 事务 |
| 13:52 | 7 个 worker 在提交点空转，relay log 已堆积 1.4 GB | **3 s** | 720 s | 24,300 事务 |
| 14:06 | 主库事务提交；从库仍在回放 | **4 s** | 1,560 s | 52,000 事务 |
| 14:09 | 客诉集中爆发：下单价格与实际扣款不符 | 2 s | 1,560 s | 51,200 事务 |
| 14:31 | 从库追平 | 0 s | 0 s | 0 |
| 复盘 | 26 分钟内约 **310 万次**价格/库存读命中旧快照，错价订单 3,100 笔，赔付 12.7 万元 | — | — | — |

### 事故三：切换后旧主复活，双写产出 2.3 万条冲突订单（2025-03-08）

某交易库，主 A（10.0.1.11）+ 从 B（10.0.1.21），切换靠一个自研脚本：连续 3 次探测失败就提升 B。

| 时间 | 事件 | 结果 |
| --- | --- | --- |
| 03-08 02:07 | A 所在机架 TOR 交换机固件缺陷，A 与全网 100% 丢包 | A 仍可本地写（`read_only=OFF`、`super_read_only=OFF`） |
| 02:09 | 脚本 3 次探测失败，判定 A 死亡 | — |
| 02:11 | 提升 B（`super_read_only=OFF`），代理切流 | 业务恢复，**B 成为新主** |
| 02:31 | TOR 自动重启完成，A 网络恢复 | **A 上 30 条 Hikari 连接重连成功，部分写请求打回 A** |
| 02:31–04:38 | 双写 127 分钟 | 同一 `order_no` 在两处状态不同，冲突 **23,000 条** |
| 04:38 | 对账任务发现异常 | — |
| 04:41 | 冻结 A：`SET GLOBAL super_read_only=ON` | 双写停止 |
| 04:41–03-11 | GTID 差集（A 独有 123 个事务）→ binlog 反向补偿 → clone 重建 A | 8 人日、4.6 万元赔付 |

根因只有一条：**切换脚本里没有 fencing 这一步**。脚本假设"我探测不到 A ⇒ A 死了"，而网络分区下这句话是错的 —— 它可能只是"我这里到不了它"。A 从来没被禁止写入，它只是被遗忘了一小时。

三次事故对应本篇三条主线：**§10.4 半同步的退化窗口**、**§10.5–10.6 并行度与延迟测量**、**§10.8 切换与裂脑**。

---

## 10.2 一句话定位与唯一真问题

**定位**：复制 = **把一份只追加日志（binlog）搬到另一台机器上按序重放**的机制。它提供的是「**副本**」这一维扩展（读扩展 + 容灾 + 备份分流），不提供写扩展、不提供多写、不提供分布式事务。写扩展是分片的事：[eng/36 MySQL 的边界与分库分表](../eng/36-mysql-limits-sharding.md)。

### 唯一真问题

> **复制延迟不是网络慢，是「并行回放度被大事务吃掉」；而「不丢」与「不停」在故障那一刻只能二选一 —— 选哪个必须写进预案，而不是等 DBA 现场决定。**

两句话推完了全部设计：

| 从这句话推出的结论 | 推出的具体做法 |
| --- | --- |
| 延迟的分母是**可并行事务数**，不是带宽 | 主库要制造可并行事务（拆批、避免单事务百万行）；从库 worker 才有活干（§10.5） |
| 一个事务的 event **不能拆给两个 worker** | 大事务永远只能串行；它一旦在跑，`preserve_commit_order` 会让后面所有提交等它（§10.5） |
| 「半同步」是一个**会放弃的等待** | 超时即退化，退化即异步，异步即 RPO>0；必须对退化本身告警（§10.4） |
| 网络分区时，「探测不到」≠「已经死了」 | 没有 fencing 就没有切换，只有裂脑（§10.8） |
| 延迟是**业务定价**出来的 | 哪些读允许读旧、允许旧多久，必须写进接口契约（§10.9） |
| RPO/RTO 是预案里的两个数，不是参数里的两个数 | 半同步超时、`wait_for_replica_count`、MGR 成员数，都是这两个数的表达（§10.4、§10.7） |

### 八问对照（本篇答哪几问）

| 八问 | 落在哪节 | 一句话答案 |
| --- | --- | --- |
| ① 承诺什么 | §10.4、§10.10 | 承诺「事务最终会到」（异步）或「至少一份副本收到」（半同步），不承诺"已执行"、不承诺"零延迟" |
| ② 数据长什么样 | §10.3 | 主库 binlog = 从库 relay log 的同构拷贝（同格式、同事件），多一层 relay 索引文件 |
| ③ 一次读写走哪条路 | §10.3 | 会话线程 → binlog → dump 线程 → 网络 → receiver → relay log → coordinator → worker → InnoDB |
| ④ 并发与一致 | §10.5、§10.7 | 靠 `last_committed`/写集判依赖做并行；MGR 再加一层 Paxos 定序 + 认证 |
| ⑤ 扩展维度 | §10.10 | **副本**（读扩展 + 容灾 + 备份分流）；写扩展它做不到，只能分片 |
| ⑥ 会在哪崩 | §10.1、§10.7 | 系统级失效三类：延迟失控（慢）、relay log/磁盘满（满）、裂脑双写（脑裂） |
| ⑦ 看哪几个指标 | §10.12 | GTID 差集、pt-heartbeat、per-worker 应用位点、relay log 空间、MGR 认证队列 |
| ⑧ 换掉它的信号 | §10.10 | 单主写入逼近单机上限、RPO 要求 0 且不能停、跨 region 多写 —— 换 MGR / NewSQL |

---

## 10.3 复制链路：从 binlog 到 relay log 到回放

### 线程与文件的全景

```text
                     主库 source (10.0.1.11)                        从库 replica (10.0.1.21)
┌────────────────────────────────────────────────┐    ┌─────────────────────────────────────────────────┐
│ 会话线程                                        │    │                                                 │
│   └─ binlog cache ──► binlog file (binlog.000418)│    │                                                 │
│        （组提交：flush → sync → commit，见 08）  │    │                                                 │
└───────────────────────┬────────────────────────┘    │                                                 │
                        │ 事件按序追加（事务边界才有 XID）│                                                 │
                        ▼                              │                                                 │
              ┌──────────────────┐   TCP 3306          │   ┌──────────────────┐                          │
              │ dump 线程         │══(GTID 集合 / 位点)═►│══►│ receiver 线程     │  旧称 I/O thread         │
              │ 一个副本连接一个   │   事件流（可压缩）    │   │ 8.0.26 起文档改名 │                          │
              └──────────────────┘                      │   └────────┬─────────┘                          │
                                                       │            │ 顺序写 + fsync(sync_relay_log)      │
                                                       │            ▼                                    │
                                                       │   ┌──────────────────┐                          │
                                                       │   │ relay log         │ 与 binlog 同构的事件流   │
                                                       │   │ + relay-log.index │ 只在**事务边界**轮转     │
                                                       │   └────────┬─────────┘                          │
                                                       │            │ 协调线程按依赖关系分派              │
                                                       │            ▼                                    │
                                                       │   ┌───────────────────────────────┐             │
                                                       │   │ coordinator（旧称 SQL thread） │             │
                                                       │   │ 读 last_committed / 写集判依赖  │             │
                                                       │   └──┬─────┬─────┬─────┬──────────┘             │
                                                       │      ▼     ▼     ▼     ▼      workers            │
                                                       │   ┌────┐┌────┐┌────┐┌────┐                     │
                                                       │   │ W1 ││ W2 ││ W3 ││ W4 │  并行回放            │
                                                       │   └─┬──┘└─┬──┘└─┬──┘└─┬──┘                     │
                                                       │     └─────┴──┬──┴─────┘                        │
                                                       │              ▼                                  │
                                                       │   InnoDB 行更新 → redo/undo →（可选）本地 binlog  │
                                                       └─────────────────────────────────────────────────┘
```

五件事在这张图上就够了：

1. **dump 线程是"每副本一个"**，它在主库上做的事就是读 binlog 文件并发出去。主库 3 个副本 = 3 个 dump 线程读同一份文件，CPU 与网络出口都 ×3。
2. **relay log 是 binlog 的同构拷贝**（同格式、同事件序列）。所以磁盘账要按 binlog 的量级算，且**只在事务边界轮转** —— 一个 30 GB 的事务会造出一个 30 GB 的 relay log 文件，`max_relay_log_size` 拦不住它。
3. **receiver 写盘、coordinator 分派、worker 执行**是三个不同的进度。任何"进度"问题必须先问：是哪一层的进度？
4. **ack 发生在 receiver 写 relay log 之后、worker 执行之前**。所以半同步只保证"事件到了从库的盘上"，从不保证"事务在从库生效了"。
5. **`log_replica_updates=ON`（旧名 `log_slave_updates`）让从库把回放的事件再写成本地 binlog**，这是级联复制与从库可被再提升的基础；代价是从库磁盘写量翻倍。

### 两种寻址：file + pos 与 GTID

| 维度 | 位点寻址（`SOURCE_LOG_FILE` + `SOURCE_LOG_POS`） | GTID 寻址（`SOURCE_AUTO_POSITION=1`） |
| --- | --- | --- |
| 单位 | 文件号 + 字节偏移 | `server_uuid:transaction_number`（如 `3f1a...:1-92011`） |
| 断点续传 | 靠记住两个数；拷数据目录/换机器极易错位 | 从库自报 `gtid_executed`，主库发差集，天然幂等 |
| 能否算"还差多少" | 不能（两个数相减没有业务含义） | **能**：`GTID_SUBTRACT(主, 从)` 就是待回放事务集 |
| 跳过坏事务 | 要手工找位点，容易跳多 | `SET GTID_NEXT='uuid:n'; BEGIN; COMMIT;` 精确跳过一条 |
| 切换时的选主依据 | 比较 `Relay_Master_Log_File` / `Exec_Master_Log_Pos`，跨文件不可比 | 比较 `gtid_executed` 的包含关系，**可判定"谁更全"** |
| 前提 | 无 | `gtid_mode=ON` + `enforce_gtid_consistency=ON` |

一句话：**位点回答"我读到哪了"，GTID 回答"我还缺哪些"**。高可用的一切自动化（选主、裂脑修复、延迟差集、数据校验）都建立在"能算差集"上，所以 8.0 时代没有理由继续用位点。

### `server_id` 与过滤规则：两个静默的杀手

- `server_id` 必须全局唯一（含级联链路与临时克隆实例）。重复的后果不是报错，而是**主库把新连接踢掉、新连接再连、再被踢**的死循环，日志里只有 `A replica with the same server_id ... has connected` 一类信息，业务表现是"复制时通时断"。
- `replicate_do_db` / `replicate_ignore_db` 按**默认库（`USE` 的那个）**判定，不按语句真实操作的库判定。`USE db2; UPDATE db1.t SET ...` 在主库生效、在从库被整条丢掉 —— 这不是"延迟"，是**永久数据分叉且延迟为 0**。
- ROW 格式下应使用按表名判定的 `replicate_wild_do_table` / `replicate_wild_ignore_table`（每个行事件单独判定）。切到通配规则前，先在影子实例上把 GTID 差集压到恒为 0，再上生产。

```sql
-- ① 位点寻址：两个数都要记，且不可跨实例迁移
CHANGE REPLICATION SOURCE TO SOURCE_LOG_FILE='binlog.000418', SOURCE_LOG_POS=10234;
-- ② GTID 寻址：从库自报差集，天然幂等（8.0.23+ 语法；之前是 CHANGE MASTER TO ... MASTER_AUTO_POSITION=1）
CHANGE REPLICATION SOURCE TO SOURCE_AUTO_POSITION=1;

-- ③ server_id 唯一性：重复会让连接陷入"连上—被踢"的死循环
SELECT @@server_id, @@server_uuid;

-- ④ 过滤规则的危险示范：replicate_do_db 按"默认库"判定
USE db2;
UPDATE db1.t_order SET status=2 WHERE id=1;   -- 主库生效；默认库是 db2 ⇒ 从库整条丢掉
-- 结果是：延迟 = 0，数据 = 分叉。这类差异只能靠对账或 GTID 差集发现

-- ⑤ ROW 格式下用按表名的通配过滤，且先在影子实例验证差集恒为 0
--    replicate_wild_do_table = 'shop.%'
```

### 链路的成本账：带宽、fsync 与突发

**算例（量级）**：主库 5,000 TPS，平均每事务 binlog 2 KB。

| 量 | 推导 | 结果 |
| --- | --- | --- |
| 单副本稳态带宽 | 5,000 × 2 KB | **10 MB/s = 80 Mbps** |
| 2 个从库时主库出口 | 10 MB/s × 2 | 20 MB/s = 160 Mbps |
| 从库 receiver 的 fsync 次数 | 5,000 事务/s，每事务至少一次 relay log 落盘 | 5,000 IOPS 起步（`sync_relay_log=1` 时） |
| 批量导入/建索引/大事务的突发 | 稳态 ×5～20 | **50–200 MB/s** |
| 建议链路 | 稳态 ÷ 峰值占空比，而不是稳态 ÷ 链路容量 | 跨 AZ 独享 1 Gbps（峰值留给突发），**不要与在线业务共享** |

关键判断：**复制带宽要按 p99 算，不是按均值算。** 半同步把链路抖动直接翻译成主库提交延迟：ack 慢 200 ms，所有等 ack 的事务就慢 200 ms；抖动超过 `rpl_semi_sync_source_timeout`，复制就降级成异步（§10.4）。所以"要不要专线"的答案不是看 10 MB/s 这个平均值，而是看**你能不能接受共享链路的 p99 抖动进入写入路径**。跨 AZ 走内网专线或同 VPC 独立子网是底线；跨 region 的半同步基本不可用（40 ms RTT 直接进提交延迟）。

> **量级结论**：以上按"每事务 2 KB、稳态 TPS 恒定、无大事务"推导。若单事务平均行数从 1 行涨到 10 行，同样的 TPS 下 binlog 字节数按行数近似线性放大，带宽需求随之 ×5～×8（行镜像含前像与后像）。换成承诺值必须在真实业务上抓一周 `binlog 字节/s` 的 p99。

---

## 10.4 半同步：你以为的同步，可能是退化后的异步

### 机制：半同步只在两个点上"等"

一次写入的路径是：`会话线程写 binlog cache → 组提交 flush → sync(fsync) → 把事件推给各副本 → 等 ack（最多 rpl_semi_sync_source_timeout 毫秒）→ 提交到引擎 / 返回客户端`。这条链上只有一个真正属于"复制"的动作：**等 ack**。它的出口只有两个 —— ack 到达，或者超时。而超时的语义是"**放弃等待，直接往下走**"，也就是把复制降级为异步，同时对客户端返回成功。

两种 `wait_point` 的差别，一句话：**AFTER_SYNC 把"可见"排在"已复制"之后，AFTER_COMMIT 把"可见"排在"已复制"之前**。5.6 与 5.7 早期默认是 `AFTER_COMMIT`，之后的默认是 `AFTER_SYNC`。选 `AFTER_COMMIT` 的代价不是 RPO 更大（两者的 RPO 上界相同，都是超时窗口内的事务），而是**"已经被别的会话读到、并据此做了业务决策的数据，在故障后可能不存在"** —— 这类损失无法用对账补回来，因为下游已经发出去了。

### 半同步不承诺的五件事

| 你以为它保证 | 它实际只保证 | 缺的那一半靠什么补 |
| --- | --- | --- |
| 不丢数据 | 至少 `wait_for_replica_count` 个副本**收到**了事务（写进 relay log） | relay log 要 `sync_relay_log=1` 才算落盘；否则 ack 可能先于 fsync |
| 事务在从库已生效 | 完全没有。ack 早于 worker 执行 | 只有 MGR（认证后本地提交）或应用层 `WAIT_FOR_EXECUTED_GTID_SET` 能做到 |
| 切换后一定不丢 | 只保证"某一份副本有"，**不保证那台会成为新主** | 选主必须选 `gtid_executed` 最全的那台（§10.8） |
| 一直保持半同步 | 只在"有半同步副本连着且 ack 不超时"时成立 | 对 `Rpl_semi_sync_source_status=OFF` 与 `no_tx` 增量告警 |
| 主库崩溃也不丢 | 前提是副本没同时挂、没在做 DDL、磁盘没满 | 真正的不丢要靠多数派（MGR / 3 副本 quorum） |

「**无损复制**」这个词在 MySQL 语境里就是上面这张表全部的条件同时成立：`AFTER_SYNC` + `wait_for_replica_count ≥ 1` + 从库 relay log `fsync` + 超时不退化 + 主从不在同一故障域。少一条，"无损"就只是一个说法。

### 故障时刻能丢多少：判定表

| 配置组合 | 主库崩溃瞬间的 RPO | 破功条件 |
| --- | --- | --- |
| 全异步（默认，半同步插件没装/关着） | 未传出的 binlog = 提交速率 × （网络 + 队列延迟 + receiver 卡顿） | 只要主库挂，必丢；丢多少由主库 binlog 与 relay log 的位点差决定 |
| 半同步 `AFTER_COMMIT`，ack 成功 | 0（从库已收到），但**可能丢"已对其他会话可见"的数据** | 超时；跨会话可见性已经发生，无法回退 |
| 半同步 `AFTER_SYNC`，ack 成功 | 0（需 relay log 已 fsync） | `sync_relay_log≠1`；主从同时故障；从库磁盘写缓存撒谎 |
| 半同步 `AFTER_SYNC`，**已超时退化** | **退化持续时长 × TPS**（事故一：171 s × 2,400 = 41 万事务） | 无告警则无人知道；这是最常见的一栏 |
| MGR 单主（多数派 ack 后提交） | 0（多数派存活即不丢） | 失去多数派时**停写**——用可用性换 RPO=0 |
| MGR 多主 | 0（提交即多数派有序），但冲突事务被回滚 | 冲突率高时业务侧看到大量 1213；大事务超 `group_replication_transaction_size_limit` 被拒 |

**这张表的用法**：先认领你现在的真实栏位（大多数团队在第 1 栏或第 4 栏，而不是他们以为的第 3 栏），再把"这一栏允许丢多少笔订单"写成一句业务能签字的话。

### 算账：退化后 RPO 是多少

设主库提交速率 `T` = 5,000 TPS，`rpl_semi_sync_source_timeout` = 10,000 ms：

| 量 | 推导 | 结果 |
| --- | --- | --- |
| 单事务最长等待 | = timeout | 10 s |
| 一次抖动触发退化时，**已在本地提交但未 ack** 的事务上界 | 10 s × 5,000 | **5 万个事务** |
| 退化持续 `D` 秒后主库崩溃 | `D` × 5,000 | D=60 → 30 万事务；D=171 → 85.5 万事务 |
| 折成订单（每事务 0.4 单） | 5 万 × 0.4 | 2 万单（一次抖动就够） |
| 把 timeout 调小到 1 s | 1 s × 5,000 | RPO 上界降到 5,000 事务，但**可用性风险上升**：任何 1 s 的 ack 抖动都触发退化 |

这张表就是"不丢 versus 不停"的数学形态：**`timeout` 是这两个目标的兑换比率**。`timeout → ∞` 是"宁可停写也不丢"，`timeout → 0` 等于异步。调参不是调优，是替业务在签一个风险额度 —— 所以它必须是预案的一部分，并有对应的告警（`status=OFF` 或 `no_tx` 增速 > 0）。

> **量级结论**：RPO 上界 = `min(timeout, 退化持续时长) × TPS`，前提是 ack 的语义为"relay log 已收到"。若 `sync_relay_log≠1`，从库掉电会再叠加一段（未 fsync 的 relay log 尾部，量级为 receiver 的写批次）。它给出的是上界，真实损失取决于崩溃时刻恰好落在哪个窗口。

```sql
-- 主库（8.0.26 起插件与参数改名为 source；更早版本是 semisync_master.so / rpl_semi_sync_master_*）
INSTALL PLUGIN rpl_semi_sync_source SONAME 'semisync_source.so';
SET GLOBAL rpl_semi_sync_source_enabled = ON;
SET GLOBAL rpl_semi_sync_source_timeout = 10000;            -- ms；这个数就是你的 RPO 兑换率
SET GLOBAL rpl_semi_sync_source_wait_point = 'AFTER_SYNC';  -- 另一取值 AFTER_COMMIT
SET GLOBAL rpl_semi_sync_source_wait_for_replica_count = 1;

-- 从库
INSTALL PLUGIN rpl_semi_sync_replica SONAME 'semisync_replica.so';
SET GLOBAL rpl_semi_sync_replica_enabled = ON;
SET GLOBAL sync_relay_log = 1;   -- ack 之前 relay log 必须 fsync，否则"无损"是空话

-- 退化检测：下面两个数一变，你就在跑异步（这两个状态位必须进监控）
SHOW GLOBAL STATUS LIKE 'Rpl_semi_sync_source_status';   -- ON / OFF
SHOW GLOBAL STATUS LIKE 'Rpl_semi_sync_source_no_tx';    -- 未等到 ack 就提交的累计事务数
SHOW GLOBAL STATUS LIKE 'Rpl_semi_sync_source_clients';  -- 连着几个半同步副本
```

---

## 10.5 并行回放：并行度是怎么被大事务吃掉的

### 并行的前提：主库得先"造出"可并行的事务

从库的并行不是从库里长出来的，是**主库在 binlog 里标出来的**。每条事务事件携带两个数：`last_committed`（它依赖的最后一条事务的序号）与 `sequence_number`（自己的序号）。`last_committed` 相同的事务彼此无依赖，可同时回放。

| 依赖判定方式 | 依据 | 并行度取决于 | 备注 |
| --- | --- | --- | --- |
| `DATABASE` | 按库分派，同库串行 | schema 个数 | 5.6 时代遗留；单库业务下并行度恒为 1，等于没开 |
| `LOGICAL_CLOCK` | 主库**组提交**的分组 | 每次组提交里有多少事务 | 8.0 默认的 `replica_parallel_type`；反映"主库提交时的并发形状" |
| `WRITESET` / `WRITESET_SESSION` | 事务**改了哪些行**（行主键/唯一键的哈希） | 写集不相交的事务数 | 主库 `binlog_transaction_dependency_tracking=WRITESET`；即使不在同一组提交，只要写集不冲突也能并行；`_SESSION` 额外保证同会话不并行 |

`binlog_transaction_dependency_tracking` 的默认值各版本不同（8.0 早期为 `COMMIT_ORDER`，之后逐步转向 `WRITESET`，8.4 已把该变量移除、只保留 WRITESET 语义），上线前用 `SELECT @@binlog_transaction_dependency_tracking` 读出来，不要背。

### 主库组提交 ↔ 从库并行度的对应关系

**结论一句话：从库的并行度上界 ≈ 主库组提交的平均组大小，再被 `replica_parallel_workers` 截断。** 换句话说：主库每次组提交攒了 200 个事务，这 200 个事务带着相同的 `last_committed` 进入 relay log，coordinator 就能把它们分给至多 200 个 worker；单 worker 的热页吞吐是 500–2,000 事务/s，于是 4 个 worker ≈ 2,000–8,000 事务/s。所以：

- 主库如果是**单线程压测/单客户端业务**（组大小 ≈ 1），从库加多少 worker 都是 1；
- 主库如果是高并发小事务（组大小几十到几百），从库 worker 才吃得满；
- 想让组变大，可以 `binlog_group_commit_sync_delay` 人为延迟（用主库延迟换从库并行度）——但这会直接抬高主库提交 p99，属于"拿主库的钱补贴从库"。

### 为什么大事务永远只能串行

一个事务的所有 event（`BEGIN` → 若干行事件 → `XID`）是一个**不可分割的依赖单元**：worker 之间按事务分派，没有任何机制把同一事务的行事件拆给两个 worker（拆了就没有事务原子性，也没法正确回滚）。于是：

| 事务形状 | 从库可用的 worker | 墙钟时间 |
| --- | --- | --- |
| 100 万行拆成 8 个 12.5 万行事务，写集不相交（WRITESET） | 8 | ≈ 单事务时间（受设备 IOPS 封顶） |
| 100 万行一个事务 | **1** | 单事务时间 × 8 的工作量全压在 1 个 worker |
| 单事务 190 万行（事故二） | **1**，且 `preserve_commit_order=ON` 时**后续所有提交都在等它** | 26 分钟 |

这解释了一个常见误诊：**"并行复制开了但并行度看起来是 1"**。多数情况下不是 `replica_preserve_commit_order` 把执行并行度压成了 1（它只在提交点排队），而是这三种情况的叠加：① 主库组提交没形成（组大小 1）；② 有一个巨大事务独占一个 worker 且后面的事务都在它之后提交；③ `replica_parallel_workers` 实际是 0 或 1（默认值各版本不同，读 `SELECT @@replica_parallel_workers`）。`preserve_commit_order` 的真实代价是**"长事务会让整条回放管道在提交点停住"**，而不是"把 worker 数变成 1"。

### 算账一：100 万行 UPDATE 在从库回放要多久

先算每行的开销（ROW 格式、单事务 100 万行、表有 6 个二级索引、行均 220 B、主键离散）：

| 成本项 | 热（页在 buffer pool） | 冷（页缺失需随机读） |
| --- | --- | --- |
| 解析行事件、定位聚簇索引叶页 | 1–2 µs | + 1 次随机读 ≈ 100–150 µs |
| 更新 6 个二级索引叶页 | 3–6 µs（6 × 0.5–1 µs） | + 每页缺失各 1 次随机读 |
| 生成 undo + redo（只追加，不 fsync） | ≈ 1 µs | ≈ 1 µs |
| **每行合计** | **5–9 µs** | **150–600 µs**（按 30%–100% 页缺失区间） |

| 场景 | 推导 | 结果 |
| --- | --- | --- |
| 热页 | 10⁶ × 7 µs | **7 s** |
| 冷页 30% 缺失 | 10⁶ × 260 µs | **260 s ≈ 4.3 min** |
| 冷页 100% 缺失（刚重启/数据远大于 buffer pool） | 10⁶ × 840 µs | **840 s = 14 min** |
| 再叠加脏页回写与双写 | ×1.5 | **≈ 21 min** |
| 主库同一操作的耗时（8 线程并发 + 顺序扫描） | — | 通常 90 s 内 |

**这就是"延迟不是网络慢"的算术形态**：同一次操作在主库 90 秒、在从库 4–21 分钟，差的那十几分钟全在"串行回放 + 随机页访问"上。网络传输 100 万行 × 约 400 B（行镜像）≈ 400 MB，在 10 MB/s 下是 40 秒 —— 只占 5%。

> **量级结论**：回放时长 ≈ 行数 × 每行开销，每行开销在 5–9 µs（热）到 150–600 µs（冷）之间浮动，主导变量是**页命中率**与**二级索引个数**。假设清单：ROW 格式、无触发器/外键级联、从库同时无报表；若从库还在跑报表查询，buffer pool 命中率下降会把耗时推到区间上界（事故一里这正是触发点）。

### 算账二：一条 DDL 在从库单线程回放卡 40 分钟

DDL 在 binlog 里是**一条语句事件**，回放时由**一个 worker** 执行；执行期间，涉及同一张表的后续事务会被 MDL 挡住（其它表的事务仍可并行）。

| 量 | 推导 | 结果 |
| --- | --- | --- |
| 表体积 | 数据 160 GB + 二级索引 80 GB | 240 GB |
| 重建写量 | 240 GB × 写放大 2.5（新表 + undo/redo + 双写） | 600 GB |
| 从库有效写带宽 | 盘顺序写 1.2 GB/s，与在线回放争抢后按 250 MB/s 计 | 250 MB/s |
| 重建耗时 | 600 GB ÷ 250 MB/s | **2,400 s = 40 min** |
| 期间 relay log 堆积 | 800 TPS × 2,400 s × 1.5 KB | ≈ 2.9 GB |
| 后续消化堆积 | 2.9 GB 回放 + 大批事务排队 | +5–15 min |
| 用户可见的总延迟峰值 | 40 min + 消化 | **45–55 min** |

唯一的解法在 DDL 算法侧，不在复制参数侧：`ALGORITHM=INSTANT`（8.0 支持部分加列/加索引/重命名列的即时操作，8.0.29 起还支持 instant drop column）让回放变成"改元数据"，耗时从 40 分钟变成毫秒；`ALGORITHM=INPLACE` 与 `COPY` 都是要重建数据的，回放必然吃满这段时间。**"从库卡 40 分钟"的根因通常不在从库，在那条 DDL 选错了算法。**

```sql
-- 从库：并行回放三件套
SET GLOBAL log_replica_updates = ON;                  -- preserve_commit_order 的前提
SET GLOBAL replica_parallel_type = 'LOGICAL_CLOCK';   -- DATABASE 只在多 schema 下有并行度
SET GLOBAL replica_parallel_workers = 8;
SET GLOBAL replica_preserve_commit_order = ON;        -- 保提交顺序（默认值随版本变，读出来再定）

-- 主库：让依赖判定从"提交时刻"变成"改了哪些行"，写集不相交即可并行
SET GLOBAL binlog_transaction_dependency_tracking = 'WRITESET';
SET GLOBAL transaction_write_set_extraction = 'XXHASH64';

-- 三个数一起读，才知道并行度上界在哪
SELECT @@global.binlog_transaction_dependency_tracking AS dep,
       @@global.replica_parallel_workers               AS workers,
       @@global.replica_preserve_commit_order          AS preserve_order;
-- 看主库给了多少并行度：mysqlbinlog --verbose 输出中 last_committed 相同的事务数
```

---

## 10.6 延迟测量：三种方法各自的谎言

延迟这个问题上，"有一个数字"比"没有数字"更危险：三个主流测法测的是**三个不同的东西**，任何一个单独用都会在特定场景里骗你。

| 方法 | 它到底量了什么 | 失真场景 |
| --- | --- | --- |
| `Seconds_Behind_Master`（`SHOW REPLICA STATUS` 一列） | 从库"当前正在回放的事件的时间戳"与"从库系统时间"之差 | ① **主库空闲 → 归零**（applier 等待新事件时置 0）；② **并行回放时按协调线程所在位置算**，worker 落后不可见（事故二）；③ receiver 半死而 applier 空闲 → 0 且链路已断；④ 主从时钟漂移直接进结果；⑤ 大事务期间数值在"事件有多老"和"还剩多少活"之间反复横跳 |
| `pt-heartbeat` 心跳 | 从库 `NOW()` 减去主库每秒写入的心跳行时间戳 | ① 主从时钟偏差直接进结果（必须单独量 `chrony` 偏差）；② 心跳表被过滤规则漏掉或被人工清理 → 数字冻结在某个值；③ 心跳间隔决定分辨率，1 s 间隔下 <1 s 的延迟测不出来；④ 只反映"心跳这一行"的可见延迟，不反映长事务 |
| GTID 差集（`GTID_SUBTRACT`） | **还剩多少个事务没执行** | ① 不给秒（要除以当前回放速率才有时间含义）；② 一个 190 万行的大事务在差集里也只是"1"，却是 26 分钟；③ 需要 `gtid_mode=ON`；④ 有过滤规则时差集**永远不为 0**，于是被"优化"成不告警 |
| `performance_schema.replication_applier_status_by_worker`（8.0.26 起含 `APPLYING_TRANSACTION_*` 时间戳列） | **每个 worker 正在啃哪个事务、它的提交时间戳** | ① 只在 8.0.26+ 有这组列；② 只看 worker 不看 coordinator 会漏"receiver 已断但队列已空"；③ 需要 `performance_schema=ON`，且是行级视图，采集频率别太高 |

**结论：三个一起看，谁报警都算事。**

- `Seconds_Behind_Master` 只能当"趋势"，不能当"判定"；
- `pt-heartbeat` 是唯一能覆盖"主库空闲但链路已死"的方法（因为心跳是持续写入的）；
- GTID 差集是唯一能算"工作量"的方法，是选主、裂脑修复、数据校验的共同基础；
- `by_worker` 视图是唯一能回答"卡在哪个事务上"的方法。

```bash
# ① 主库每秒写一次心跳行（表必须被复制到从库）
pt-heartbeat --host=10.0.1.11 -u heartbeat -p *** -D percona --table=heartbeat \
  --update --interval=1 --utc --daemonize
# ② 从库读心跳：值 = 从库 NOW() - 心跳行 ts ⇒ "这一行在从库可见为止的延迟"
pt-heartbeat --host=10.0.1.21 -u heartbeat -p *** -D percona --table=heartbeat \
  --monitor --interval=1 --utc
# ③ 主从时钟偏差必须单独量（chronyc tracking / timedatectl），否则 ② 里混着偏差
# ④ GTID 差集：还剩多少事务；换算成秒要用当前回放速率，别直接当延迟用
SRC=$(mysql -N -B -h10.0.1.11 -e "SELECT @@global.gtid_executed")
RPL=$(mysql -N -B -h10.0.1.21 -e "SELECT @@global.gtid_executed")
mysql -N -B -h10.0.1.21 -e "SELECT GTID_SUBTRACT('$SRC','$RPL') AS pending"
# ⑤ 切换演练：把这条差集算在"候选主库是否最全"的判定里（§10.8 第 4 步）
```

---

## 10.7 MGR：Paxos 进来了，取舍也进来了

### 它多了一层什么

异步/半同步复制里，主库先提交、副本再追 —— **顺序由主库单点决定，副本只是观众**。MGR 把"顺序"交给了组通信层（GCS，基于 Paxos 变体的实现），于是一条事务的路径变成：本地执行（InnoDB 写 + redo/binlog prepare）→ `before_commit` 钩子拿到写集（行主键/唯一键的哈希）→ GCS 用 Paxos 把这个消息复制到多数派并**定序**（这一步决定全局顺序）→ 各成员按序做 certification，拿自己的写集历史比对，冲突就回滚 → 无冲突才本地 commit 并返回客户端。

三个直接后果：

1. **提交延迟多了一个多数派 RTT**（下面给账）；
2. **冲突检测是乐观的**：本地已经执行完才判，判失败就是整个事务回滚（业务看到 `1213` 一类死锁/回滚错误，在申请加入的成员侧是 3101 一类 applier 错误）。所以 MGR 适合"写集分散"的负载，不适合"所有人抢同一行的计数器"；
3. **有硬上限**：单事务超过 `group_replication_transaction_size_limit`（8.0 默认 150 MB 量级）会被直接拒绝；消息缓存由 `group_replication_message_cache_size` 管（8.0.16+，默认 1 GiB）。这两条把"大事务"从"慢"升级成"报错"。

### 流控：让快的人等慢的人

MGR 的吞吐由**最慢的成员**决定：任何一个成员的应用队列变长，Paxos 的定序就不能无限向前。所以它内建流控（`group_replication_flow_control_mode=QUOTA` 默认开启，`hold_percent` 默认 10、`period` 1 s、`release_percent` 50 一类参数控制"什么时候踩刹车、什么时候松"）。

流控的输入是两个队列，也正是你要看的两个数：**认证队列**（`replication_group_member_stats.COUNT_TRANSACTIONS_IN_QUEUE`）与**远端应用的 applier 队列**（`COUNT_TRANSACTIONS_REMOTE_IN_APPLIER_QUEUE`）。它们持续上涨，说明"有人跟不上"，此时主库的写入会被限速 —— 表现是**吞吐下降而不是报错**，这是 MGR 最容易被误判为"性能问题"的地方。

### 3 节点 vs 5 节点

多数派 = `floor(n/2)+1`：3 节点要 2 票，5 节点要 3 票。

| 维度 | 3 节点 | 5 节点 |
| --- | --- | --- |
| 容忍成员失效 | **1** | **2** |
| 提交需要多少 ack | 1 个远端（与最快 peer） | 2 个远端（等第 2 快的 peer） |
| 提交延迟增量（同城 3 AZ，单程 1–1.5 ms） | ≈ 1.5–2 ms | ≈ 2–3 ms；5 个 AZ 时 ≈ 3–4 ms |
| Paxos 消息扇出 | 每条消息发 2 个 peer | 每条消息发 4 个 peer ⇒ 出口带宽与 CPU 约 ×2 |
| 空间放大 | 数据 3 份 | 数据 5 份（备份算总账时这一栏最贵） |
| 单 AZ 故障（3 AZ 拓扑） | 挂 1 台 → 仍有多数派，可写 | 2/2/1 拓扑挂掉 2 台的那一 AZ → 仍有多数派，可写 |
| 连续两处故障 | 停写（只剩 1/3） | 仍可写（3/5） |
| 新成员加入的恢复窗口 | 短（clone + 追平） | 长，且 RECOVERING 期间流控会持续踩刹车 |
| 适用 | 大多数业务：RPO=0 + 容 1 台 | 跨 3 AZ 的金融/交易核心：容"一个 AZ + 另一台"同时损坏 |

### 网络分区下的两种选择

```text
   3 AZ 部署，quorum = 2

   ┌────────────┐        ┌────────────┐        ┌────────────┐
   │   AZ-a     │        │   AZ-b     │        │   AZ-c     │
   │   M1(主)   │◄──────►│   M2       │◄──────►│   M3       │
   └─────┬──────┘        └─────┬──────┘        └─────┬──────┘
         └─────────────────────┘                     │
              分区 P1：AZ-c 与 a/b 断开               │
         ┌───────────────────────────────────────────┘
         ▼
   {M1,M2} = 2/3 多数派 → 继续可写，M3 被驱逐出组（或长期 UNREACHABLE）
   {M3}    = 1/3 少数派 → M3 立刻 super_read_only，写请求直接报错
                          （宁停不分叉：少数派永远不产生第二个可写主）

              分区 P2：AZ-a 单独断开（主 M1 变少数派）
         ▼
   {M1}    = 1/3 → **停写**（这是 MGR 的宪法：少数派不服务写）
   {M2,M3} = 2/3 → 单主模式下自动选出新主（比较各成员已认证事务集）
   → 代价：M1 上正在等认证的事务全部失败（客户端拿到错误，可安全重试）
   → 收益：M1 上**没有**任何"已提交却只在本地"的事务 ⇒ RPO = 0
```

**这跟异步复制是两种世界观**：异步复制在分区时两边都能写（于是要 fencing 才能不裂脑）；MGR 在分区时少数派直接停写（于是不需要 fencing，代价是"能写"这件事被多数派绑住）。选择它们的差别不在性能，在"你要不要为 RPO=0 接受一次计划外停写"。

> **量级结论**：MGR 的提交延迟 ≈ 单程 RTT × 定序轮次 + 认证开销，3 节点同城 AZ 增量为毫秒级（1.5–4 ms），相对 OLTP 事务本身 5–20 ms 是 10%–30% 的加法；跨 region 部署（RTT 20–40 ms）会把每次提交推到几十毫秒，5 节点跨 5 城基本不可用于在线交易。换成承诺值必须实测 `p99` 的 commit 延迟，而不是用本表的估算。

---

## 10.8 切换与裂脑：最后一秒的提交去哪了

### 七步切换时序（每一步的失败都会变成一次事故）

```text
step 1  T+0.0   判定：连续 3 次探测失败（间隔 2 s）+ 多数派视角确认（避免"我这看不到"= 死亡）
step 2  T+0.5   FENCING 旧主：安全组/交换机 ACL 阻断 3306 → 尝试 SHUTDOWN → 确认写入已停
                 └─ 无法 fencing ⇒ **不切换**。这是唯一一条不可协商的规则
step 3  T+2.0   选主：在候选集中比较 gtid_executed 的包含关系，取"最全"的那台
                 └─ 并列时按数据新旧、机房位置、硬件规格、是否开了过滤规则排序
step 4  T+3.0   校验候选：super_read_only 状态、server_id 唯一、自增步长、过滤规则、GTID 差集
step 5  T+3.5   提升：SET GLOBAL super_read_only=OFF; read_only=OFF; 关闭半同步副本角色
                 └─ 若旧主 GTID 集合更全且已被 fencing ⇒ gtid_purged 补齐并集（或按 step 6 补偿）
step 6  T+4.0   切流：ProxySQL hostgroup / VIP / DNS / 配置中心，观察 30 s 错误率与 p99
step 7  T+5.0   建档：把"旧主 gtid_executed − 新主 gtid_executed"写进事故记录，进入补偿/丢弃决策
                 └─ 这条差集是后面所有对账的输入，丢了它就再也算不清丢了什么
```

三个最容易省掉的步：**step 2（fencing）**、**step 3（比较 GTID 包含关系而不是"最靠后的位点"）**、**step 7（记录差集）**。事故三就是省掉 step 2 的直接后果。

### 最后一秒的提交去哪了：四种情形

| 情形 | 主库状态 | 从库状态 | 客户端状态 | 切换后怎么办 |
| --- | --- | --- | --- | --- |
| ① 已 ack 且已回客户端 | 已提交 | relay log 已有 | **收到成功** | 数据在。选主必须选到"有这条"的那台（step 3 的意义） |
| ② 已 ack，响应包在路上丢失 | 已提交 | relay log 已有 | 超时/重试 | **数据在**，重试会造成重复 ⇒ 必须靠幂等（唯一键/去重表）消化（→ eng/2、eng/5 的一套做法） |
| ③ 等 ack 中主库崩溃（`AFTER_SYNC`） | binlog 已落盘、引擎未提交 | 可能没有，也可能有 | 未收到成功 | 语义上"未确认"，业务可按失败处理；但对账要认"可能存在" |
| ④ 引擎已提交、binlog 未落盘（内部 2PC 的 prepare 态崩） | 崩溃恢复会回滚 | 无 | 未收到成功 | 无害（客户端本来就没成功），见 `comp/08` |

**这张表的用处**：告诉业务"哪些重复是设计内的"。情形 ② 产生的重复在异步复制下同样存在（超时不代表失败），所以**幂等不是复制的选配，是复制的前提**。

### 裂脑后的数据找回：三条路

| 方法 | 怎么做 | 代价与风险 | 何时用 |
| --- | --- | --- | --- |
| GTID 差集定位 | `GTID_SUBTRACT(旧主, 新主)` 得到旧主独有的那批事务 | 只给出"哪些事务"，不给"怎么撤" | 所有情形的第一步，必做 |
| binlog 反向补偿（flashback） | 从旧主 binlog 解析独有事务的行事件，生成逆 SQL（`UPDATE` 换旧值、`INSERT` 换 `DELETE`）在新主执行 | 必须 ROW 格式；若该行在此之后被别人改过，逆操作会覆盖新值 ⇒ **必须按时间窗人工复核** | 旧主独有事务量小（几十到几百）、且这些行后来没被改过 |
| 丢弃 + 重建旧主 | 认旧主独有事务为"未成功"，用 clone 插件从新主全量重建旧主 | 丢那一段业务写（需业务签字确认） | 旧主独有事务量大、或涉及难以安全反转的业务（扣款、发券） |
| 双向补偿合并 | 两边各补偿对方的独有事务 | 冲突判断靠人；只在窗口极小、可全量人工核对时成立 | 双写时间窗 < 几分钟且数据量小 |

### fencing 到底靠什么

| 层 | 手段 | 能挡住谁 |
| --- | --- | --- |
| 数据库 | `SET GLOBAL super_read_only=ON` | **所有**会话（包括 SUPER 权限）。`read_only=ON` 挡不住 SUPER 用户，单独用等于没设 |
| 账号 | `ALTER USER ... ACCOUNT LOCK` / 改密码 | 应用侧连接（但拦不住运维手工连接） |
| 网络 | 安全组、交换机 ACL、`iptables DROP 3306` | **所有**来源（唯一能挡住"进程还在跑"的手段） |
| 主机 | STONITH / IPMI 断电 / 云主机强制停止 | 整机（最彻底，也最慢） |
| 代理 | 从 hostgroup 摘除 | 只挡代理后的流量，直连连接池不受控 |

规则很简单：**至少要有"数据库 + 网络"两层，且切换脚本的第一步是 fencing，不是提升。** 只做代理层的都不叫 fencing，那只是"让一部分流量别过来"。分布式锁语境里同一个道理（fencing token）→ comp/46。

---

## 10.9 读一致性工程：读己之写、延迟路由与备份一致性

复制带来的读扩展，本质是**用一致性换吞吐**。工程上要做的不是"消灭延迟"，是**把每个读的容忍延迟定价，并让路由按价执行**。

### 读己之写：三档方案

| 档 | 做法 | 代价 | 适用 |
| --- | --- | --- | --- |
| 会话粘滞 | 写后 N 秒内该会话的读打主库（N ≥ 复制延迟 p99，通常 1–3 s） | 主库多承担一部分读；N 取小了会漏 | 大多数业务的第一步，改造成本最低 |
| 记住位点（推荐） | 写成功后取 `@@global.gtid_executed` 作为 token；读从库前 `WAIT_FOR_EXECUTED_GTID_SET(token, timeout)`，超时回源 | 一次额外 RTT（本地，无跨机等待时几乎免费） | 有强"刚写完就要看到"诉求的场景（账户、订单详情） |
| 直接读主 | 关键路径全读主 | 主库读压力 | 支付、风控、库存扣减前后的一致性读 |

还有一条容易漏的：**单调读**。同一用户连续两次读如果落在不同从库（延迟不同），会看到"越读越旧"。解决办法是把用户 ID 哈希到固定的从库集合（会话粘从库），而不是每次轮询。

```go
// 读己之写路由骨架：写成功后把 GTID 当 token，读从库前等它追平；超时立刻回源主库。
type Router struct {
	primary  *sql.DB
	replicas []*sql.DB
}

// 返回的 token 是"此刻主库已提交事务"的并集，作下界用是安全的：
// 可能让从库多等，绝不会让从库少等。
func (r *Router) Write(ctx context.Context, fn func(*sql.Tx) error) (string, error) {
	tx, err := r.primary.BeginTx(ctx, nil)
	if err != nil {
		return "", err
	}
	if err = fn(tx); err != nil {
		_ = tx.Rollback()
		return "", err
	}
	if err = tx.Commit(); err != nil {
		return "", err
	}
	var token string
	if err = r.primary.QueryRowContext(ctx, "SELECT @@global.gtid_executed").Scan(&token); err != nil {
		return "", err
	}
	return token, nil
}

func (r *Router) ReadAfterWrite(ctx context.Context, token, q string, args ...any) (*sql.Rows, error) {
	db := pickReplica(r.replicas, token) // 先按 token 的 UUID 段做会话粘滞，再按负载挑
	var waited int
	// 返回 0 = 已执行到位；1 = 超时。超时一律回源，绝不拿旧数据顶上。
	if err := db.QueryRowContext(ctx, "SELECT WAIT_FOR_EXECUTED_GTID_SET(?, 1)", token).Scan(&waited); err != nil || waited != 0 {
		return r.primary.QueryContext(ctx, q, args...)
	}
	return db.QueryContext(ctx, q, args...)
}
```

### 延迟感知路由

- **判定用哪个数**：不要用 `Seconds_Behind_Master`（§10.6）。用 `pt-heartbeat` 或 GTID 差集；阈值通常取"业务能容忍的旧"而不是"技术上的小"（例如价格 100 ms、库存 200 ms、订单列表 2 s）。
- **摘流量要滞回**：超过阈值 3 个采样点才摘，回到阈值的 50% 才放回，否则一次抖动会让所有从库同时进出。
- **影子读 + 回源重试**：先读从库，若发现版本号/逻辑时钟落后于会话 token，再回源主库一次（比全量读主便宜，比纯从库安全）。
- **报表与备份必须与 HA 从库隔离**：报表会冲刷 buffer pool、占满 IO，直接把回放速度打下来（事故一的触发点）。

### 从库只读与备份一致性

| 事项 | 做法 | 坑 |
| --- | --- | --- |
| 只读 | `super_read_only=ON`（不只 `read_only`） | 从库被提升时必须先明确关掉，否则提升后写不进去 |
| 备份的一致性 | `START TRANSACTION WITH CONSISTENT SNAPSHOT` + **同一瞬间**取位点/GTID | 该语句只在 RR 下有"事务级快照"的含义，RC 下等于普通 `BEGIN` |
| 备份与增量衔接 | 快照必须配一组 `gtid_executed`（或 file+pos），否则恢复后无法接增量 | 位点与快照不在同一瞬间取 = 备份不可用（→ comp/33 的位点衔接同源） |
| 物理备份 | clone 插件（8.0.17+）自带 GTID 位点 | 拷完必须校验 `gtid_executed` 与主库的包含关系 |
| 从库崩溃安全 | `sync_relay_log=1`、`sync_binlog=1`（开 `log_replica_updates` 时）、`innodb_flush_log_at_trx_commit=1` | 少一个，从库掉电后 relay log 尾部与本地 binlog 就可能对不上 |

```sql
-- 备份一致性三件套：快照与位点必须同一瞬间取
SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;  -- RC 下 WITH CONSISTENT SNAPSHOT 形同虚设
START TRANSACTION WITH CONSISTENT SNAPSHOT;
SELECT @@global.gtid_executed AS gtid_at_snapshot;        -- 与快照同一时刻的位点
-- 在这个事务里做全量导出（mysqldump --single-transaction 做的正是这件事）
-- 8.0.26 起 mysqldump 用 --source-data（旧名 --master-data 已弃用）
COMMIT;

-- 物理备份：clone 插件（8.0.17+），快照自带 GTID 位点
CLONE LOCAL DATA DIRECTORY = '/backup/full-20260210';

-- 从库崩溃安全：三处 fsync 一个都不能省
SELECT @@global.sync_relay_log, @@global.sync_binlog,
       @@global.innodb_flush_log_at_trx_commit, @@global.log_replica_updates;
```

---

## 10.10 架构边界：复制能保证什么、不能保证什么

| 你以为复制保证了 | 它实际保证的 | 真要靠它，你还得自己补 |
| --- | --- | --- |
| 主从数据一致 | "事件最终会送到并执行"——**前提是链路在、过滤规则不错、回放没报错停下** | GTID 差集监控 + 定期抽样对账 |
| 不丢数据 | 半同步且未退化且 relay log 已 fsync 时，RPO≈0 | 退化告警（`status=OFF` / `no_tx` 增速）+ 预案里的 RPO 签字 |
| 不停服 | 有可提升的从库（异步下秒级提升） | fencing + 选主策略 + 每季度演练一次（含旧主复活场景） |
| 读扩展 | 从库能承担读，延迟内 | 读己之写、单调读、延迟感知路由、报表隔离 |
| 备份不压主库 | 从库可做备份 | 一致性快照 + 位点 + 恢复演练 |
| 容灾 | 跨 AZ/region 一份拷贝 | 跨 AZ 专线 + 半同步 + 定期切换演练 |
| 写扩展 | **完全没有** | 分片（[eng/36](../eng/36-mysql-limits-sharding.md)）、或换 NewSQL（comp/19） |
| 多活 / 多写 | **完全没有**（异步复制不支持多写） | MGR 多主（写集冲突回滚）或单元化（[architect/10](../architect/10-multi-active-and-cell.md)） |
| 零延迟 | 从来没有 | 延迟是物理量：每一行回放都要碰页（§10.5） |

三条硬边界，越过后就不该再调参数，该换形态：

1. **写吞吐逼近单机上限**（主库 CPU 长期 > 70%、组提交排队变长）→ 分片或换 NewSQL；
2. **RPO 必须为 0 且不能接受少数派停写** → 异步/半同步/MGR 三者都满足不了这个组合，只能改业务幂等 + 对账，或降低 RPO 要求；
3. **跨 region 双写** → 复制从来不做多写，走单元化或共识型 NewSQL。

---

## 10.11 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到物理形态） | 定位 | 修复 | 防复发 |
| --- | --- | --- | --- | --- |
| `Seconds_Behind_Master=0` 但业务读到旧数据 | ① 主库空闲时 applier 等待新事件，该数被置 0；② MTS 下它按协调线程位置算，worker 进度不在里面；③ receiver 半死但 applier 空闲 | `pt-heartbeat` 与 `GTID_SUBTRACT` 与 `by_worker` 视图三对照；看 `replication_connection_status.LAST_HEARTBEAT_TIMESTAMP` 是否推进 | 提高监控维度，不是提高采集频率 | 延迟告警只允许用 pt-heartbeat / GTID 差集；`Seconds_Behind_Master` 降级为展示项 |
| 并行复制开了，并行度看着还是 1 | ① 主库组提交没形成（组大小 1）；② 一个大事务独占 worker 且后续提交都在等它；③ `replica_parallel_workers` 实际是 0/1；④ `replica_parallel_type=DATABASE` 而业务单库 | `SELECT @@replica_parallel_workers, @@replica_parallel_type`；`mysqlbinlog --verbose` 看 `last_committed` 分布 | 主库改 `WRITESET`、业务侧拆批、worker 数按核对齐 | 容量评审时同时看"主库组提交组大小"与"从库 worker 数"两个数 |
| relay log 把从库磁盘写满 | relay log **只在事务边界轮转**，一个 30 GB 事务就产生一个 30 GB 文件；`relay_log_space_limit` 默认 0（不限额）；开 `log_replica_updates` 时磁盘还要再翻一倍 | `SHOW REPLICA STATUS` 的 `Relay_Log_Space`；`du -sh datadir/*relay*` | 清 relay log（先确认已执行位点）、杀掉源头的大事务写入方式 | relay log 与 binlog 预留 ≥ 单事务最大体积 ×3；大事务改成批提交 |
| DDL 在从库单线程回放卡 40 分钟 | DDL 是一条语句事件，只能一个 worker 执行；`COPY`/`INPLACE` 要重建整表；期间同表 DML 被 MDL 挡住 | `by_worker` 看正在 apply 的事务；从库 `SHOW PROCESSLIST` 里的 DDL 语句 | 等它跑完（别 kill，kill 会触发回滚更久）；之后改用 `ALGORITHM=INSTANT` 可覆盖的操作 | 上线前评估每个 DDL 的"从库回放时长"；变更窗口与业务峰值错开 |
| 切换后旧主复活写入，双写冲突 | 无 fencing：`read_only=OFF` 的旧主在分区恢复后照常接受连接；业务连接池自动重连 | 两个实例各自 `gtid_executed` 出现分叉；对账发现同一业务键两处状态不同 | 立即 `super_read_only=ON` → 算 GTID 差集 → 反向补偿或重建 | 切换脚本第一步必须是 fencing（数据库 + 网络两层）；演练必须包含"旧主复活"分支 |
| `gtid_mode` 从 OFF 改 ON 线上出错 | 变量只能逐级切换，且顺序错一步就报错；匿名事务未清空时切到 `ON_PERMISSIVE` 会拦住 | `SELECT @@gtid_mode, @@enforce_gtid_consistency`；`SHOW STATUS LIKE '%ANONYMOUS%'` | 按官方四级顺序逐级推进 | 写进变更模板：`enforce_gtid_consistency=WARN` → `ON` → `gtid_mode=OFF_PERMISSIVE` → `ON_PERMISSIVE` → `ON`，每级停留并观察 |
| 提升从库后写不进去 | 从库长期 `super_read_only=ON`，提升脚本只改了 `read_only` | 报错 1290 `--read-only` | `SET GLOBAL super_read_only=OFF` 再 `read_only=OFF` | 提升脚本里把这两个变量作为显式步骤 + 校验断言 |

`gtid_mode` 的切换顺序值得单独记住，它是唯一一个"顺序错了会直接报错、而且回退也得逐级"的在线变更，且必须**所有节点（含延迟从库、含 DR 实例）同步推进**，跳过一台就会在切换那天炸：

1. 所有节点 `enforce_gtid_consistency=WARN`（先找出不兼容语句，业务照常跑）；
2. 在观察窗口内修掉全部告警（`CREATE TABLE ... SELECT`、事务内临时表一类）；
3. 所有节点 `enforce_gtid_consistency=ON`；
4. 所有节点 `gtid_mode=OFF_PERMISSIVE`（新事务带 GTID，老的匿名事务仍可接收）；
5. 等 `ONGOING_ANONYMOUS_TRANSACTION_COUNT` 归零（复制链路上的也要等，别只看主库）；
6. 所有节点 `gtid_mode=ON_PERMISSIVE`；
7. 所有节点 `gtid_mode=ON`。

---

## 10.12 观测：指标、拐点与告警

| 指标 | 版本 / 开关前提 | 拐点 | 告警骨架 |
| --- | --- | --- | --- |
| `pt-heartbeat` 延迟 | 需在主库建心跳表（每秒 1 行）；要求主从时钟偏差 < 心跳间隔 | > 业务定价阈值（价格 100 ms / 库存 200 ms / 列表 2 s） | `lag > 阈值 for 3m` |
| GTID 待执行事务数（`GTID_SUBTRACT`） | 需 `gtid_mode=ON`；有过滤规则时差集恒非 0，要先减掉基线 | > 回放速率 × 阈值秒数（把"事务数"换算成"秒"） | 上升速率 > 0 且持续（比绝对值更早） |
| `replication_applier_status_by_worker` 应用位点 | 8.0 起整表；`APPLYING_TRANSACTION_*` 一组列需 **8.0.26+**；需 `performance_schema=ON` | 单个 worker 的 applying 时间戳停止推进 > 60 s | 单 worker 停滞告警（大事务/DDL 的唯一早期信号） |
| `Relay_Log_Space` | `SHOW REPLICA STATUS` 一列（8.0.22 起 `SHOW SLAVE STATUS` 更名为 `SHOW REPLICA STATUS`，输出列前缀也改为 `Replica_*`，老采集器取 `Slave_*` 会取到空值 —— 这是升级后最常见的"假绿"） | 超过磁盘可用空间的 40%，或 10 分钟内翻倍 | 双阈值：绝对值 + 增速 |
| `Rpl_semi_sync_source_status` / `_no_tx` | 需装半同步插件；8.0.26 起参数与状态从 `*_master_*` 改名 `*_source_*` | `status=OFF` 出现；`no_tx` 任意增量 | `status=OFF` 立即 P1（这等于"你在跑异步"） |
| `Replica_IO_Running` / `Replica_SQL_Running` | 8.0.22+ 列名（旧名 `Slave_*`） | 任一非 `Yes` | 立即 P1；`Connecting` 持续 > 30 s 也要报 |
| `LAST_HEARTBEAT_TIMESTAMP`（`replication_connection_status`） | 8.0 起该表含此列 | 超过 2 × `replica_net_timeout` 未更新 | receiver 半死的唯一早期信号 |
| MGR `replication_group_members.MEMBER_STATE` | 需 MGR 插件；状态集 `ONLINE / RECOVERING / UNREACHABLE / ERROR / OFFLINE` | 任一成员非 `ONLINE` 超过 `group_replication_member_expel_timeout`（8.0.13+，默认 5 s 量级） | 非 ONLINE 立刻告警；`RECOVERING` 超过 10 min 也报 |
| MGR 认证/应用队列 | `replication_group_member_stats` 的 `COUNT_TRANSACTIONS_IN_QUEUE`、`COUNT_TRANSACTIONS_REMOTE_IN_APPLIER_QUEUE`（8.0 起，无需额外开关） | 持续 > 数百（流控的输入，涨起来意味着写入被限速） | 连续 5 个采样点上升即告警 |
| `COUNT_CONFLICTS_DETECTED`（同上表） | MGR 多主必看 | 增量 > 0 且持续 | 冲突率告警 —— 说明写集在互相撞，需要业务拆分热点行 |

```sql
-- ① 链路硬信号（8.0.22 起命令与输出列名都换成 Replica_*）
SHOW REPLICA STATUS\G
SELECT CHANNEL_NAME, SERVICE_STATE, LAST_ERROR_NUMBER, LAST_ERROR_MESSAGE
FROM performance_schema.replication_applier_status;

-- ② 每个 worker 正在啃哪个事务（8.0.26 起有 APPLYING_TRANSACTION_* 一组列）
SELECT WORKER_ID, SERVICE_STATE,
       APPLYING_TRANSACTION_IMMEDIATE_COMMIT_TIMESTAMP AS applying_since,
       LAST_APPLIED_TRANSACTION_IMMEDIATE_COMMIT_TIMESTAMP AS last_done
FROM performance_schema.replication_applier_status_by_worker;

-- ③ receiver 上一次收到心跳的时间：半死连接的唯一早期信号
SELECT CHANNEL_NAME, LAST_HEARTBEAT_TIMESTAMP, RECEIVED_TRANSACTION_SET
FROM performance_schema.replication_connection_status;

-- ④ relay log 占用（大事务会把单个文件撑到事务大小）
SELECT VARIABLE_VALUE AS relay_log_space FROM performance_schema.global_status
WHERE VARIABLE_NAME = 'Relay_Log_Space';

-- ⑤ MGR：成员状态 + 认证队列 + 冲突计数
SELECT MEMBER_ID, MEMBER_STATE, MEMBER_ROLE FROM performance_schema.replication_group_members;
SELECT MEMBER_ID, COUNT_TRANSACTIONS_IN_QUEUE, COUNT_TRANSACTIONS_CHECKED,
       COUNT_CONFLICTS_DETECTED, COUNT_TRANSACTIONS_REMOTE_IN_APPLIER_QUEUE
FROM performance_schema.replication_group_member_stats;
```

---

## 10.13 一页速记

### 判据表：四种复制形态，四列定生死

| 形态 | RPO | 提交延迟增量 | 失去多数派 / 主库挂掉时 | 什么时候选它 |
| --- | --- | --- | --- | --- |
| 异步 | 未传出的 binlog（秒级 × TPS） | 0 | 从库可秒级提升，**可能丢一段** | 读扩展、日志/报表类、能接受丢秒级数据的业务 |
| 半同步 `AFTER_SYNC` | ≈0（relay log 已 fsync 时） | +1 RTT + 从库落盘 | 超时后**自动退化成异步** | 大多数 OLTP 的默认；必须配退化告警 |
| 半同步 `AFTER_COMMIT` | 上界同异步，但"已可见却被回退" | +1 RTT | 同上 | 遗留系统；新系统不要选 |
| MGR 单主（Paxos 定序） | 0（多数派存活即不丢） | +1～2 个多数派 RTT（毫秒级起） | **少数派停写**（用可用性换 RPO=0） | 交易核心、要 RPO=0 且容 1–2 台 |
| MGR 多主 | 0（认证后提交），冲突事务被回滚 | 同上 + 冲突重试 | 少数派停写 | 写集天然分散的多地域写（很少适用） |

### 十条一口价

1. **ack 只代表"relay log 收到了"**，不代表"事务在从库生效了"；半同步从来不是"同步"。
2. **半同步是一个会放弃的等待**：超时（默认 10 s）即降级为异步，且只翻一个状态位、不写错误日志。
3. **退化后的 RPO 上界 = `min(timeout, 退化时长) × TPS`**：5,000 TPS × 10 s = 5 万事务，一次抖动就够。
4. **复制延迟不是网络慢**：100 万行回放 = 行数 × 每行 5–9 µs（热）/ 150–600 µs（冷），网络只占几个百分点。
5. **并行度上界 = 主库组提交平均组大小**，再被 worker 数截断；单客户端业务组大小为 1，加 worker 无效。
6. **一个事务的 event 不能拆给两个 worker**，所以大事务永远串行，且 `preserve_commit_order` 会让后续提交全等它。
7. **DDL 回放由单 worker 执行**，`COPY/INPLACE` 重建 240 GB 表 ≈ 40 分钟；`INSTANT` 是唯一解药。
8. **`Seconds_Behind_Master` 会撒谎**（主库空闲归零、MTS 只看协调线程、receiver 半死仍为 0）；延迟告警用 pt-heartbeat + GTID 差集。
9. **没有 fencing 就没有切换，只有裂脑**：`super_read_only=ON` + 网络层阻断，两层缺一不可。
10. **RPO 与 RTO 是预案里的两个数**，不是参数里的两个数：写清楚"允许丢多少、允许停多久"，再回头选形态。

---

## 10.14 自检题

**1. 半同步开着，主库宕机还是丢了 171 秒的数据，最可能发生了什么？怎么在 30 秒内确认？**
<details><summary>答案</summary>

最可能是**超时退化**：从库 ack 超过 `rpl_semi_sync_source_timeout`（默认 10,000 ms），主库放弃等待、本地提交并返回成功，此后一直以异步模式跑。确认方法：`SHOW GLOBAL STATUS LIKE 'Rpl_semi_sync_source_status'`（应为 OFF）与 `Rpl_semi_sync_source_no_tx`（未 ack 就提交的累计数，应大于 0）；再对比主库 binlog 位点与从库 relay log 位点的时间差。次可能是 `sync_relay_log≠1`（ack 早于 fsync）或切换时选到了延迟更大的那台从库。修法是把退化本身做成 P1 告警，并承认 `timeout` 就是 RPO 的兑换率。
</details>

**2. 从库 8 个 worker、`WRITESET` 也开了，为什么一次批量任务还是把延迟打到 26 分钟？**
<details><summary>答案</summary>

因为**事务是并行的最小单位**。批量任务是一个 190 万行的单事务，全部 event 必须由同一个 worker 按序回放，其它 7 个 worker 无活可干；`preserve_commit_order` 又让后续提交都停在提交点等它，整条管道等于串行。同时 `Seconds_Behind_Master` 显示 0–4 s，因为协调线程早已把 relay log 读到最新——它量的是"读"的位置，不是"回放"的位置（正确读法：pt-heartbeat、GTID 差集、`by_worker` 的 applying 时间戳）。修法是业务侧分批提交（例如每 2,000 行一个事务），让主库把它们标记为可并行。
</details>

**3. 主库 5,000 TPS、每事务 2 KB，跨 AZ 要不要专线？按什么算？**
<details><summary>答案</summary>

稳态带宽 = 5,000 × 2 KB = 10 MB/s = 80 Mbps，单看这个数一条 1 Gbps 链路绰绰有余。但要按三个"非线性项"算：① **副本数**：2 个从库 → 主库出口 20 MB/s；② **突发**：批量导入/建索引/大事务的 binlog 速率可达稳态 5–20 倍（50–200 MB/s）；③ **p99 抖动**：半同步把链路抖动翻译成提交延迟，共享链路的尾延迟会直接把 ack 打到超时、触发退化。结论：**跨 AZ 走独享内网/专线**，容量按峰值而不是均值定；跨 region（RTT 20–40 ms）不适合半同步。
</details>

**4. 切换时"最后一秒提交"的事务去哪了？业务会看到什么？**
<details><summary>答案</summary>

分四种：① 已 ack 且已回客户端 → 数据在，选主必须选到有这条的那台；② 已 ack 但响应包丢 → 数据在，客户端会重试 ⇒ 产生**重复**，必须靠幂等消化；③ `AFTER_SYNC` 等 ack 中崩溃 → 从库可能没有，客户端没收到成功，可按失败处理；④ 引擎已提交但 binlog 未落盘 → 崩溃恢复回滚，客户端也没成功。所以"切换会丢什么"的答案不在数据库里，在**你的幂等设计与预案**里；切换第 7 步必须把旧主独有的 GTID 差集记录下来，否则事后算不清丢了什么。
</details>

**5. 3 节点 MGR 和 5 节点 MGR，除了"多容 1 台"还差什么？网络分区时各会怎样？**
<details><summary>答案</summary>

还差三件事：① **提交延迟**——5 节点要等第 2 快的 peer 的 ack，同城多 0.5–1.5 ms，跨 AZ 部署差异更明显；② **消息扇出与存储**——每条 Paxos 消息发 4 个 peer 而不是 2 个，出口带宽与 CPU 约 ×2，数据 5 份（空间放大 5×）；③ **加入/恢复窗口**——新成员 RECOVERING 期间流控会持续踩刹车，5 节点更频繁。分区时两者遵循同一宪法：**多数派继续写，少数派立刻停写**（3 节点时单飞的 1 台停写；5 节点时最多 2 台停写），因此没有裂脑、RPO=0，代价是"能写"被多数派绑住——这与异步复制"两边都能写、靠 fencing 兜"是两个世界观。
</details>

---

## 10.15 延伸阅读

- binlog 的格式、fsync 时机、内部 2PC 与组提交（本篇的前半段）：→ comp/08
- MVCC 快照、长事务与「从库延迟被大事务放大」的另一个视角：→ [comp/06](06-mvcc-and-isolation-levels.md)
- InnoDB 页、行格式与二级索引：回放为什么是"页访问"而不是"追加"：→ comp/05
- Kafka 的 ISR/HW/LEO 与 acks：另一套"不丢"的参数组合，对照阅读：→ comp/29
- CDC 的位点衔接：快照与增量如何在同一个 GTID 上对齐：→ comp/33
- 分布式锁与 fencing token：为什么"锁不可靠时靠挡住旧主人"：→ comp/46
- 连接池与超时链：切换时连接池为何把流量打回旧主：→ comp/44
- 故障谱（慢 / 满 / 脑裂 / 假活 / 语义漂移）与本篇的对应关系：→ comp/52
- MySQL 的边界与分库分表（复制解决不了写扩展）：[eng/36](../eng/36-mysql-limits-sharding.md)
- 迁移与切换的工程流程（灰度、双写、回滚）：[architect/20 数据迁移](../architect/20-data-migration.md)
- 多活与单元化（跨 region 双写为什么不能靠复制）：[architect/10 多活与单元化](../architect/10-multi-active-and-cell.md)
- 本篇在全套里的位置、八问卡与写作公约：[0-summary 总纲](0-summary.md)
