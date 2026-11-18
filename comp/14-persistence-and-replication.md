# 14. 持久化与主从同步：fork、COW 与 replication backlog

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 内存与无主 KV ｜ **编号**：14 / 53
> **本篇定位**：12 篇把 Redis 的执行内核拆到「一条慢命令停掉所有人」，13 篇把内存形态拆到「一个 field 越线内存翻三倍」。本篇接着往下推一层，回答 12 篇 `latest_fork_usec > 300 ms` 那条换挡线**是凭什么定出来的**，以及每个人都被问过一次的两个问题：为什么内存越大 BGSAVE 越像一次心跳停跳？为什么明明配了主从，机房抖 3 秒还是要全量同步？答案是两个物理事实——**fork 只复制页表、不改数据页，于是快照的代价是一份「写时复制」的账单，账单大小由「内存 × 期间写入比例」决定**；**backlog 是一个存字节流的环，不是存时间的窗口，于是「能续传多久」等于「环有多深 ÷ 写入速率」**。RDB/AOF/PSYNC 的所有配置项，都是这两个事实的下游。
> **读法**：§14.2 的一句话是全篇支点，先背下来；§14.3 的两张物理形态图与 §14.4 的 fork/COW 时间轴决定后面所有账怎么算；§14.7 的 backlog 环形图与判定树是「网络抖一下就要全量同步」的唯一解释；§14.8 的五个算例给出可直接替换假设的公式；评审时间不够只读 §14.2、§14.7、§14.12。主从语义一律以「异步 + 会丢最后一次 fsync 之后的数据」为基线，不假设任何同步复制能力。

---

## 14.1 引子：三起「主从一致，但一台重启就把集群拖垮」的故障

持久化与复制的事故有一个共同形状：**数据没丢、日志没报错、主从也确实一致，出事的是「一次快照或一次全量同步顺手把别的东西打死了」**。下面三起分别对应本篇三条主线：COW 的内存峰值（§14.4）、backlog 的默认值（§14.7）、`everysec` 的真实窗口（§14.5）。

### 事故一：100 GB 实例 BGSAVE 期间 COW 把内存顶到 190 GB，被 OOM killer 杀掉（2026-04-08）

`cache-order`：单机 192 GiB 物理内存、无 swap、`maxmemory 0`（默认，即不限制）、一主一从、`save` 保留默认规则、`repl-diskless-sync yes`。日常 `used_memory` 104 GB，`latest_fork_usec` 长期在 760–840 ms 之间——**这条线 12 篇已经标成换挡线，但它一直在被忽略**。

| 时间 | 事件 | `used_memory` / RSS | 主线程 | 后果 |
| --- | --- | --- | --- | --- |
| 04:12 | 定时 `BGSAVE` 触发，fork 104 GB 的页表 | 104 / 104 GB | 停 812 ms（`latest_fork_usec`） | 一轮超时约 1.4 万条 |
| 04:18 | 上游「历史订单归档回填」开始，30 GB 逻辑数据以 `SET`/`EXPIRE`/`DEL` 混合重写进 Redis | 112 / 118 GB | 正常 | 无异常信号 |
| 04:22 | COW 复制开始显形，`rdb_changes_since_last_save` 涨到 1,900 万 | 118 / 128 GB | 正常 | 无人看这个指标 |
| 04:31 | 写入触及的页占到全库 48%，RSS 越过主机内存的 80% | 129 / 161 GB | 偶发 200 ms 抖动 | 监控只有 `used_memory` 告警，没响 |
| 04:39 | 触及页占比约 83%，RSS 逼近物理上限 | 137 / 190 GB | 开始 swap 尝试（无 swap，直接失败） | 内核回收失败 |
| 04:41 | OOM killer 选中 `redis-server`（RSS 190 GB / 主机 192 GB） | 进程消失 | — | 主库没了 |
| 04:42 | Sentinel 提升从库；从库内存只有 68 GB（它先被逐出过），命中率 96% → 11% | — | — | 回源 QPS 3,000 → 41,000 |
| 04:58 | MySQL 连接池打满，订单写入 5xx | — | — | 全站下单失败 |
| 06:02 | 手工预热热点 key + 限流 + 扩从库，逐级恢复 | — | — | 78 分钟 |
| 复盘 | 订单失败 3.4 万笔，估算 GMV 损失 610 万元 | — | — | — |

**为什么 190 GB 不是「写入的 30 GB」**：COW 复制的是 4 KiB 的**页**，不是写入的字节。回填把写分散在几乎整个键空间上，触及页占比 α 涨到 0.83，峰值就逼近 `104 × (1 + 0.83) ≈ 190 GB`。**上限永远是「数据集本身」，与期间写了多少字节无关**——写 30 GB 可以复制 86 GB，写 300 GB 也只复制 86 GB（每页最多复制一次）。这条反直觉结论在 §14.4 与 §14.8.2 各算一遍。

### 事故二：`repl-backlog-size` 留在默认 1 MB，一次 3 秒抖动触发三次全量同步，2 Gbps 被打满（2026-06-17）

`session-store`：一主三从（跨两个机柜），主库写入 8.2 万 QPS、平均命令 120 B，等价 **9.8 MB/s** 的复制字节流。`repl-backlog-size` 是默认的 **1 MB**——从 2.8 起这个值就没变过，而团队的数据量比默认值假设的场景大了三个数量级。

| 时间 | 事件 | 关键数字 |
| --- | --- | --- |
| 15:41:07 | 机房 ToR 交换机固件升级，三个从库连接同时被 RST | 链路中断 |
| 15:41:10 | 三个从库重连并发 `PSYNC`，上报 offset 落后约 29.5 MB | `psync` 请求 `offset = master_repl_offset − 29.5 MB` |
| 15:41:10 | 29.5 MB > 1 MB backlog，三次全部 `sync_partial_err +1` | 一次抖动 = 三次全量同步 |
| 15:41:10 | `repl-diskless-sync-delay 5` 把三次请求合并成一次 fork（省了两次停顿） | 主线程停 780 ms |
| 15:41:15 | 三条链路各传一份 6.2 GB RDB，共 18.6 GB | — |
| 15:42:33 | 2 Gbps 上联（250 MB/s 线速）被打满 78 s | 18.6 GB ÷ 250 MB/s ≈ 74 s |
| 15:42:40 | 同机房 MySQL 半同步往返 0.4 ms → 62 ms，Kafka ISR 抖动 | 跨服务连带 |
| 15:43:00 | 订单服务 P99 从 90 ms 涨到 2.4 s，超时 4.1 万笔；告警指向「订单服务慢」 | — |
| 15:44 | 人工把 `repl-backlog-size` 调到 `512mb`，全量同步还在跑，改不了本轮 | — |
| 复盘 | 78 秒的机房带宽事故，根因是一个「没人改过的默认值」 | — |

**1 MB 能覆盖多久**：`1 MB ÷ 9.8 MB/s ≈ 102 ms`。也就是说，**只要主从链路断开超过 0.1 秒，就必须全量同步**。这不是玄学，是除法（§14.8.4）。

### 事故三：`appendfsync everysec` + 主机掉电，丢 1.7 万条订单状态，而「每 1.3 秒才 fsync 一次」（2026-02-25）

`order-state`：AOF `appendfsync everysec`、一主两从、写入 13,000 QPS。所有人对 everysec 的认知是「最多丢 1 秒」。复盘时发现丢的窗口是 1.3 秒，而原因不在 Redis 的参数里。

| 时间 | 事件 | 关键数字 |
| --- | --- | --- |
| 02:00 | 定时 `BGREWRITEAOF`（`auto-aof-rewrite-percentage 100`，此时 AOF 已 41 GB） | 重写要写 ~20 GB |
| 02:00–06:30 | 同一台机上的日志采集 agent 在这块盘上做全量压缩，盘 util 常驻 99% | 重写耗时 4.5 h |
| 03:10 | `aof_delayed_fsync` 开始以 4,700/分钟增长（后台 fsync 还没完，下一次 fsync 只能排队） | 实际 fsync 间隔 0.9–1.4 s |
| 09:07:12 | 机房 UPS 切换失败，整机掉电 | 最后一次 fsync 是 09:07:10.86 |
| 09:09 | 重启，`aof_last_write_status:ok`、`aof_last_bgrewrite_status:ok`，`redis-check-aof` 报尾部干净 | 监控全绿 |
| 09:12 | 对账发现 17,200 条订单状态停在旧值，其中 3,100 条「已支付」退回「待支付」 | 客诉 800 起 |
| 复盘 | 丢失窗口 = 1.26 s × 13,000 写/s ≈ 1.64 万条；**everysec 在磁盘被打满时会静默退化** | — |

三个事故分别对应：**COW 的峰值由 α 而不是写入量决定（§14.4）**、**backlog 存字节不存时间（§14.7）**、**everysec 的窗口由 fsync 实际间隔而非参数决定（§14.5）**。

---

## 14.2 一句话定位与唯一真问题

**定位**：Redis 的持久化 = **一个单线程进程如何在不停机、不阻塞自己的前提下，产出一份某个时刻的一致数据副本**；Redis 的复制 = **把这同一份字节流按序喂给若干台从库**。两者共用同一个底层机制（fork + 写 RDB），所以它们的代价也共用同一个形状。它承诺的是「进程崩溃可恢复」「主从最终一致」；不承诺「掉电零丢失」（RPO>0）、不承诺「同步复制」（异步为主）、不承诺「切主不丢数据」。

### 唯一真问题

> **Redis 的持久化不是「写数据」的问题，是「怎么在不阻塞唯一那个线程的前提下拿到一份一致快照」的问题——fork 和 COW 决定了代价的形状，而 backlog 决定了你的「高可用」在故障瞬间是真高可用还是全量重传。**

这句话拆成两个推论，全篇都在展开它们：

1. **快照的一致性不是免费的，但它的账单是「写时」付的**。`fork()` 只复制页表（16.78M 条 PTE / 64 GiB），数据页一页都不复制；快照的一致性靠「父进程要改页时，内核先把这一页复制一份」保证。于是代价从「快照那一刻」被摊到了「快照期间每一次写」上——**内存越大，那一停越长（页表越长）；期间写越猛，复制越多（峰值越高）**。这两条都是除法，不是经验。
2. **复制的「续传」能力是一个容量问题，不是可靠性问题**。`PSYNC` 能不能部分重同步，只取决于「副本落后的那段字节流还在不在主库的 backlog 环里」。环是 1 MB 时，容纳能力就是 `1 MB ÷ 写入速率`，与网络多稳定、Redis 多新、Sentinel 多聪明都没有关系。

### 八问卡（持久化与复制版，密度对齐 `→ comp/01 §01.4`）

| 问 | 一句话答案 | 数字 / 边界 |
| --- | --- | --- |
| ① 承诺 | 进程崩溃可恢复 + 主从最终一致 | 不承诺掉电零丢（RPO ≥ 0）；不承诺同步复制；不承诺切主不丢 |
| ② 数据 | RDB = 某时刻整库快照（二进制）；AOF = 写命令追加流（7.0 起 base + incr + manifest） | RDB 载入按字节算；AOF 载入按**命令数**算，差一个数量级（§14.8.5） |
| ③ 链路 | `fork()` 复制页表 → 子进程写 RDB → 从库 `LOADING` → `PSYNC` 增量 | fork 0.3–0.7 s / 64 GiB；RDB 载入 300 MB/s–1 GB/s |
| ④ 并发与一致 | fork + COW 给「快照一致性」；复制是单向异步字节流 | 无共识、无位点仲裁、无半同步退化——只有「收到」与「没收到」 |
| ⑤ 扩展维度 | **垂直切**（更小实例降 fork 代价）+ **分片**（把 100 GB 拆成 10 × 10 GB）+ **副本**（扩读/容灾） | 单实例内存是 fork 代价的唯一变量；副本数只加带宽不加写吞吐 |
| ⑥ 崩 | 满（COW 打爆内存、全量同步打满带宽、AOF 写满盘）；假活（全量同步中的从库返回旧/空数据）；语义漂移（把 `WAIT` 当同步复制） | 见 §14.9 的故障时点判定表 |
| ⑦ 指标 | `sync_partial_err` / `latest_fork_usec` / `rdb_changes_since_last_save` / `aof_delayed_fsync` / `master_link_status` | 全部带版本 + 开关，见 §14.11 |
| ⑧ 换 | fork 停顿 > 300 ms 且数据集 > 25 GB；要 RPO=0；要跨 key 事务 | 拆片或换形态（AOF 型 KV / NewSQL），见 §14.9 |

### 本篇与相邻篇的分工（反冗余）

| 篇 | 讲什么 | 本篇只给 |
| --- | --- | --- |
| `→ comp/12` | 单线程执行内核、慢命令、内存水位 | 直接沿用它的 `latest_fork_usec > 300 ms` 换挡线与「fork 让主线程停一下」的结论，只往下推「这一停有多长、为什么」 |
| `→ comp/13` | 编码阈值与单向门 | 只在「RDB 存逻辑数据、载入时按本机阈值重新编码」这一处回指：主从 `*-max-listpack-*` 不一致 ⇒ 内存不一致 |
| `→ comp/15` | Cluster 16384 槽、MOVED/ASK | 一个字不写；只说「分片是降低 fork 代价的手段，不是提高一致性的手段」 |
| `→ comp/10` | **MySQL** 的复制：binlog / 半同步 / 并行回放 / MGR | 只做一次六列对照（§14.9），全部细节锚过去 |
| `../architect/04-read-path-and-cache-shape.md` | 缓存形态、失效语义、穿透/击穿/雪崩 | 一个字不重写 |

---

## 14.3 物理形态：RDB 快照与 AOF 日志

两条路线的差别不是「二进制 vs 文本」，而是**记录的是「结果」还是「过程」**。RDB 记「某一刻每个 key 的值长什么样」，AOF 记「按顺序发生过的每一条写命令」。这个差别推出后面的一切：体积、载入速度、可读性、可修复性、以及崩溃时丢多少。

```
① RDB：某一时刻的「结果」
   Redis 内存                                 RDB 文件（一次性写出，不可追加）
   ┌──────────────┐        fork+COW          ┌───────────────────────────────┐
   │ db0  dict A  │ ──────────────────────►   │ "REDIS0011"  魔数 + 版本       │
   │ db0  dict B  │     子进程遍历所有 key     │ aux: redis-ver / used-mem …   │
   │ db1  dict C  │     按类型编码后顺序写     │ SELECTDB 0                    │
   └──────────────┘                           │ 0xFE/类型 opcode + key + value │
                                              │ …（LZF 压缩、CRC64 校验）      │
                                              │ EOF + CRC64                   │
                                              └───────────────────────────────┘
   特性：二进制紧凑、载入快（逐对象重建，不做命令解析）；只能整份重写，不能追加

② AOF：按序的「过程」（Redis 7.0 起的 multi-part 形态）
   每条写命令 ──► 命令传播 ──► aof_buf ──► write() ──► [fsync] ──► incr 文件尾
                                  │
   appendonlydir/  ├─ appendonly.aof.manifest      ← 元数据：谁是谁、谁是 base
                   ├─ appendonly.aof.1.base.rdb    ← 重写产物（默认 RDB 格式，7.0 起）
                   ├─ appendonly.aof.1.incr.aof    ← 当前追加的增量
                   └─ appendonly.aof.2.incr.aof    ← 重写期间新开的增量（写完即接替）
   特性：可读、可截断修复（redis-check-aof）、体积大；载入是「逐条重新执行」
```

**7.0 的 multi-part AOF 到底改了什么**：7.0 之前 AOF 是**单个文件**，重写期间的增量命令只能攒在内存里的一个 rewrite 缓冲（`aof_rewrite_buf`），一直攒到子进程写完、父进程把缓冲追加到新文件为止——数据集大 + 写入猛时，这个缓冲能涨到几百 MB，是 6.x 上一个典型的「重写期间内存无故上涨」。7.0 改成 multi-part 之后，重写一开始就**新开一个 incr 文件**，增量直接写进文件，内存里不再有那个双缓冲；manifest 记录「base 是哪个、incr 有几个、谁接替谁」，重写完只需原子换 manifest。**结论：7.0+ 上，「AOF 重写导致内存翻倍」这条老经验只剩 fork+COW 那一半。**

| 维度 | RDB | AOF（单文件） | AOF（7.0+ multi-part） |
| --- | --- | --- | --- |
| 记录单位 | 某一刻的 key/值 | 每条写命令的字节 | base（RDB 或 AOF）+ N 个 incr |
| 写入时机 | `save` 规则或手动 `BGSAVE`，整份重写 | 每条写命令后追加（`appendfsync` 决定何时落盘） | 同左，但分成「当前 incr + 历史 base/incr」 |
| 体积 | 最紧凑（LZF 压缩 + 二进制编码） | 最大（同一 key 被改 1 万次就有 1 万条） | base 用 RDB 格式后体积接近 RDB |
| 载入成本 | **按字节**：解析 + 重建对象 | **按命令数**：解析 + 重新执行（§14.8.5） | base 走 RDB 速度，尾部长短由重写频率决定 |
| 崩溃语义 | 丢上次快照之后的所有写 | 丢最后一次 fsync 之后的写 | 只截断最后一个 incr 文件，历史部分不受影响 |
| 可读/可修复 | 不可读，无部分修复 | 可读，`redis-check-aof --fix` 截掉尾部半条命令 | 同左，但只需修最后一个 incr |
| 重写期间内存 | 只有 fork 的 COW 账单 | fork 的 COW + `aof_rewrite_buf`（可能几百 MB） | **只有 fork 的 COW** |

**一个必须回指的坑**：RDB 里存的是**逻辑数据**（key + 值 + 类型 + TTL），不含编码方式。从库载入时会用**从库自己的阈值**重新编码——所以主库 `hash-max-listpack-entries 128`、从库是默认值时，两侧 `used_memory` 会不同；从库若把阈值调大，载入后内存会小，但一旦跨线就永远回不去（`→ comp/13 §13.5` 的单向门）。这是「主从内存不一致」最常见的原因，和 COW 无关。

```conf
# 一条持久化基线，每一行都写清「买什么、付什么」
dir /data/redis                  # RDB/AOF 都往这里写；盘满 = 两种持久化同时失效
dbfilename dump.rdb
save 3600 1 300 100              # 关掉默认的 900/300/60：让快照节奏可控，而不是随写入量跳
rdbcompression yes               # LZF：省盘与省带宽，多花一点 CPU（主线程外）
rdbchecksum yes                  # CRC64：用一点 CPU 买「损坏能被发现」
appendonly yes
appendfsync everysec             # 窗口 ≤1 s，但真实间隔取决于 fsync 是否被抢（§14.5）
aof-use-rdb-preamble yes         # 7.0 起 base 文件默认 RDB 格式：载入快 5~10×
auto-aof-rewrite-percentage 100
auto-aof-rewrite-min-size 1gb    # 默认 64mb 对大实例过小，频繁重写 = 频繁 fork
```

---

## 14.4 fork 与 COW：内存越大，快照越像一次心跳停跳

`BGSAVE`、`BGREWRITEAOF`、以及每一次全量同步，做的都是同一件事：**`fork()` 出一个子进程，让它去遍历这份数据**。而 `fork()` 在现代 Linux 上**不复制内存**——它复制**页表**。这是理解本篇一切代价的起点。

### 页表有多大：一条纯算术

x86-64 四级页表、4 KiB 页、每个页表项（PTE）8 B。于是：

| 量 | 算式 | 64 GiB 数据集的代入结果 |
| --- | --- | --- |
| 页数 | `64 GiB ÷ 4 KiB` | 16,777,216 页 |
| 页表/数据比 | `8 B ÷ 4096 B` | 1/512 ≈ 0.195% |
| 需要复制的 PTE 字节 | `16.78M × 8 B` | 134 MB |
| 需要新分配的页表页 | `134 MB ÷ 4 KiB` | 33,554 个 |

所以 `fork()` 的固定成本 ≈ **复制 134 MB 页表 + 分配 33.5K 个页表页 + 把 16.78M 条 PTE 在父子两边都置为只读 + 全核 TLB shootdown**。按内核里每页 20–40 ns 的量级摊：`16.78M × 20~40 ns = 0.34~0.67 s`。这换算成工程口径就是「**每 GB 5–10 ms**」，与一线测量的量级一致（24 GB 实例 130–250 ms，100 GB 实例 0.8–1.0 s）。

**这条算术直接解释了 12 篇的换挡线**：`latest_fork_usec > 300 ms` 对应的数据集大约在 30–60 GB。到那一步，每次快照都要把这个实例的 QPS 归零 0.3 秒以上，而快照是周期性的——**它不是偶发故障，是一个稳定的吞吐税**。

### COW 的写放大：账单在「快照期间」，不在「快照那一刻」

```
t=0      fork()  ── 主线程停 0.3~0.7 s（复制 16.78M 条 PTE，数据页一页都没复制）
         │
         ▼
t=0.5s   child 开始遍历 64 GiB、写 RDB ─────────────────────────► 25~40 s（受磁盘/带宽限）
         │
         │  父进程继续处理命令。每条 SET 落在页 P 上时：
         │    ① P 在父子两边都是只读 → 父进程写触发写保护缺页
         │    ② 内核分配新页，把 P 的 4 KiB 整页复制过去
         │    ③ 父进程改的是新页；child 仍看到旧页
         │    ④ 一致性由「旧页永不被改」保证 —— 这就是 COW 买的东西
         ▼
t=0.5~40s  COW 复制的字节 = 被触及页数 × 4 KiB（每页最多一次，上限 = 数据集本身）
           峰值 RSS ≈ R × (1 + α)，R = 数据集，α = 被触及页占比

  内存
  96 GB ┤                                  ╭──── α = 0.45（写 8 GB 打散在旧页上，jemalloc 页填充率 ~40%）
  80 GB ┤                        ╭─────────╯
  64 GB ┤───────────●────────────┴──────────  α = 0（纯新增 key：新页不复用旧页，不产生 COW）
        └────┬────────────┬──────────────┬──► t
           fork        RDB 写出中       RDB 结束（COW 账单随之清零）
```

**两条推不掉的工程结论**：

1. **内存越大，fork 越久**。页表与数据集成正比（1/512），而 fork 的停顿就是「复制页表 + 置只读 + TLB shootdown」的时间。这是 12 篇 `latest_fork_usec` 的物理来源，也是「单实例别超过 25 GB」这条建议的唯一出处——**不是 Redis 处理不了大内存，是快照这件事在大内存上会周期性停掉唯一那个线程**。
2. **快照期间写越猛，COW 复制越多、峰值越高**。注意放大器是**页粒度**：写 1 个 100 B 的 value，可能复制 4 KiB 的页（40×）。α 与「写入字节数 ÷ 数据集」不是同一个数——如果写高度分散，α 会远大于字节比；如果写集中在少数页上（比如同一个热 key 反复写），α 反而很小。

**THP 在这里是一个反直觉的开关**：开透明大页能让页表缩小 512 倍（2 MiB 页 ⇒ PTE 占比从 0.195% 降到 0.0004%），fork 变快；但 COW 的复制粒度同时从 4 KiB 变成 2 MiB —— 改 100 B 要复制 2 MiB，写放大 2 万倍。Redis 7.x 启动时会主动检测并告警，生产上应当关闭。

---

## 14.5 AOF 的三档 fsync：丢失窗口是怎么被定价的

AOF 的落盘有三个动作，**要分清谁在什么时候做**：主线程把命令追加到 `aof_buf` 并 `write()` 到文件（进内核页缓存，此时掉电仍会丢）；`fsync()` 把页缓存刷到介质（由后台 bio 线程或主线程执行，取决于档位）。**参数选择的是 `fsync` 的频率，不是 `write` 的频率。**

| `appendfsync` | 谁在 fsync | 丢失窗口 | 吞吐代价 | 什么时候能用 |
| --- | --- | --- | --- | --- |
| `always` | **主线程**，每条写命令后同步 fsync | 0 条（只丢「正在写但还没应答」的那条） | 吞吐 ≈ `1 ÷ fsync 延迟`；本地 SSD 0.1–1 ms ⇒ **1,000–10,000 写/s 封顶**；且无组提交，退化成同步调用 | 真正不能丢的少量写（金融流水、幂等记录），且 QPS 很低 |
| `everysec` | **bio 线程**，每秒一次 | ≤ 1 s 的数据，**实际是「最后一次成功 fsync 到现在」** | 几乎无损主线程吞吐 | 默认，95% 的场景 |
| `no` | **内核** page writeback | 由内核 `vm.dirty_expire_centisecs`（默认 30 s）+ `vm.dirty_ratio` 决定，**几十秒量级** | 最快 | 有外部副本/可重建的数据，或从库的本地 AOF |

`everysec` 是「每秒**最多**一次」而不是「每秒**恰好**一次」，这条差别就是事故三的全部内容：fsync 由 bio 线程执行，如果上一次 fsync 还没返回（盘被打满、重写正在抢 IO），下一次只能排队，实际间隔被拉长到 1.3 s、4 s 甚至更久。**衡量这个退化的指标不是参数，是 `aof_delayed_fsync` 的导数**（§14.11）。

**`no-appendfsync-on-rewrite`**：重写期间（`BGREWRITEAOF` 或全量同步）子进程会把盘写满，fsync 在这种背景下可能阻塞几百 ms 到几秒；把 `fsync` 关掉可以让「主线程/写路径不被 fsync 拖住」，代价是**重写期间的丢失窗口从 1 s 扩到「重写耗时」**（重写 5 分钟 ⇒ 窗口 5 分钟）。默认 `no`（即重写期间仍 fsync）。取舍很直白：**你更怕延迟尖刺，还是更怕丢一段能算出来的数据**。设为 `yes` 时必须在文档里写下「重写期间 RPO ≈ 重写耗时」。

**重写风暴**：`auto-aof-rewrite-percentage 100` + `auto-aof-rewrite-min-size 64mb` 的组合在「写入量大、AOF 增长快」的实例上会变成「重写刚结束，增量又涨了 100%，立刻再重写」——每次重写都是一次 fork（一次 0.3–0.7 s 的主线程停顿）加一轮盘写。再叠加多台从库同时触发全量同步，就是「fork 排队」。判定方法很简单：看 `aof_rewrite_in_progress` 在 24 h 内的占比，超过 20% 就说明这只实例的时间大部分花在给 AOF 瘦身上。

---

## 14.6 全量同步与 diskless：一次重启能打爆什么

全量同步的链路只有四步，但每一步都能单独打死你：

1. **从库连上，发 `PSYNC <replid> <offset>`（首次是 `PSYNC ? -1`）** —— 主库判定能否部分重同步（§14.7），不能就回 `FULLRESYNC` 并 fork。
2. **主库 fork 出子进程开始产 RDB** —— 主线程停 0.3–0.7 s（64 GiB，§14.4）。
3. **RDB 走哪条路** —— 方式 A：子进程写 `dump.rdb`，再从文件读出来发给从库（`repl-diskless-sync no`）；方式 B：子进程直接写 socket（diskless，**7.0 起默认**）。
4. **从库 `LOADING`** —— 默认先清空数据集再载入，期间不服务请求（返回 `-LOADING`）；载入完成后主库把这段时间攒的增量从 backlog 与输出缓冲补发。

| 维度 | 走磁盘（`repl-diskless-sync no`） | diskless（`repl-diskless-sync yes`，**7.0 起默认**） |
| --- | --- | --- |
| 磁盘占用 | 每个并发的全量同步一份 RDB（6 GB × N） | 0（不落盘） |
| 磁盘压力 | 写 6 GB + 读 6 GB，与 AOF/业务盘争 IO | 0（但 fork 的子进程要一直占着 socket，fork 时长 = 传输时长） |
| 额外延迟 | 写盘 + 读回，多 10–20 s（6 GB @ 500 MB/s） | 省掉这一段 |
| 对从库的要求 | 从库自己落盘再载入 | 从库用 `repl-diskless-load` 直接从 socket 载入 |
| 失败代价 | RDB 留在盘上可复用/排查 | 链路断 = 这份 RDB 没了，从头再来 |
| 适用 | 磁盘快、从库多、网络差 | 磁盘慢/云盘限速、链路好（同机房/同 AZ） |

**`repl-diskless-sync-delay`（默认 5 s）**：主库收到全量请求后先**等 5 秒**，把这段时间内到达的其它从库请求合并进同一次 fork。它的取舍是：从库越多越赚（一次 fork 喂 N 个从库），但单台从库恢复要平白多等 5 s。**故障切换场景下这 5 秒要算进 RTO。**

**`repl-diskless-load`（默认 `disabled`）**：三档的差别是「载入期间这台从库还能不能被读」——

| 取值 | 行为 | 代价 |
| --- | --- | --- |
| `disabled` | 先完整收到 RDB 文件落盘，再一次性载入 | 需要磁盘空间；载入期间仍不可服务，但至少没有「半空」状态 |
| `on-empty-db` | 收到即**清空当前数据集**，边收边载入 | **载入期间从库是空的**：此时读到的是「没有数据」而不是「旧数据」；链路中断则数据集已丢，要重来 |
| `swapdb`（7.0+） | 在另一个 db 里载入，完成后原子换 | 内存峰值 ≈ 2× 数据集；换来的是一直能服务旧数据 |

`on-empty-db` 是最危险的一档，因为它把「副本暂时旧」悄悄变成了「副本暂时空」。如果 Sentinel 在这段时间把读流量切过去，或者提升它，就是一次数据事故——**「载入期间实例不可服务」是它唯一诚实的状态**，监控上要能看出 `master_sync_in_progress:1` 与从库的 `-LOADING`。

**「从库慢 → 主库 OOM」的因果链**，这是全量同步最贵的那一条。它是一条真正的单行道，每一步都能单独失败：

1. 从库在传 RDB（或网络慢），**复制链路排空速度 < 主库写入产生速度**；
2. 增量命令堆进**该从库**的 replication output buffer —— 这块内存**在 `maxmemory` 之外、不参与逐出判定，但占 RSS**；
3. 未超限 → 主库内存上涨，先被 OOM killer 盯上的是主库，不是从库；
4. 超过 `client-output-buffer-limit replica`（默认 hard 256 MB / soft 64 MB × 60 s）→ 主库**主动断开**这条复制连接（日志关键字 `overcoming of output buffer limits`）；
5. 从库重连 → `PSYNC` 又落后 → 又一次全量同步 → 又堆 buffer → 又断，这就是**同步风暴**。

注意那个「不参与逐出判定」：输出缓冲是**客户端缓冲**，不占 `used_memory`，但确实占 RSS。所以一台 `maxmemory` 设在 70% 的实例，仍然可能被复制输出缓冲顶到 OOM。

**`repl-timeout`（默认 60 s）**：主库判「从库死了」、从库判「主库链路死了」用的是同一个超时。它必须**显著大于** `repl-ping-replica-period`（默认 10 s），否则会在链路正常但因为一次 RDB 传输慢而误判——全量同步期间的心跳节奏会变，`repl-timeout` 太小会在传输中途把连接踢掉，然后重来，然后继续被踢。

---

## 14.7 replication backlog：为什么网络抖一下就要全量同步

这是本篇第二个支点，也是「Redis 高可用」四个字最容易被高估的地方。

### 它就是一个环形字节缓冲

主库在执行完每条写命令后，除了把命令发给在线的从库，还会把**相同的字节**追加进一个固定大小的环：`repl_backlog`。它不是一个日志文件，不长在盘上，重启即消失，只活在主库内存里，大小由 `repl-backlog-size` 决定——**默认 1 MB**（2.8 起未变）。

```
repl_backlog：默认 repl-backlog-size = 1 MB 的环形缓冲（只存在于主库内存）
                                            写到最老的一端会被覆盖
  ┌────┬────┬────┬────┬────┬────┬────┬────┐
  │ C1 │ C2 │ C3 │ C4 │ C5 │ …  │ C8 │ C9 │   ← 存的是「字节流」，不是命令、不是时间
  └────┴────┴────┴────┴────┴────┴────┴────┘
    ▲                                  ▲
    │                                  │
  repl_backlog_first_byte_offset    master_repl_offset（当前写到哪）
    = master_repl_offset − repl_backlog_histlen

  PSYNC 能续传（+CONTINUE）的三个条件，缺一即全量：
    ① 副本上报的 replid == 主库的 master_replid（或 master_replid2：切主后旧 replid 的保留位）
    ② 副本上报的 offset ≥ repl_backlog_first_byte_offset（落后的那段还没被覆盖）
    ③ 副本的数据集仍然是同一段历史（换过数据集 ⇒ offset 无意义）
```

### 判定树

```text
副本重连 ──► PSYNC <replid> <offset>
   │
   ├─ replid 不认识 / 从未同步过（PSYNC ? -1）
   │        └──────────────────────────────────────► FULLRESYNC（全量，fork 一次）
   │
   ├─ replid 认识（含 master_replid2，切主后的一段宽限期）
   │     │
   │     ├─ offset 落在 [first_byte_offset, master_repl_offset] 内？
   │     │        ├─ 是 ──► +CONTINUE（部分重同步；sync_partial_ok +1）
   │     │        └─ 否 ──► FULLRESYNC；sync_partial_err +1
   │     │                    └─ 这一支每跳变一次 = 一次 fork + 一份 RDB 的带宽
   │     │
   │     └─ 但主库在「切主后宽限期」外 / 副本重启过 ──► FULLRESYNC
   │
   └─ 全量同步期间：buffer 超限 / repl-timeout / 链路断 / repl-diskless-load 配错
            └──► 再从 FULLRESYNC 开始（这就是「同步风暴」的形状）
```

### 关键结论：backlog 存的是字节流，不是时间

这是本篇最需要背下来的一句。**「我的 backlog 能容 30 秒」这句话没有意义**，除非你说清写入速率。正确的写法是两条公式：

> `可覆盖时长 T = repl-backlog-size ÷ (写入 QPS × 平均命令字节数)`
> `需要的 backlog = 写入速率 × 允许断开时长 × 安全系数`

代入事故二的数：`8.2 万 QPS × 120 B/命令 = 9.8 MB/s`；`1 MB ÷ 9.8 MB/s ≈ 102 ms`。**默认配置下，链路断开 0.1 秒以上就必须全量同步。** 换成「要让 30 秒闪断能续传」：`9.8 MB/s × 30 s = 294 MB`，留 2 倍安全系数（要容纳正常复制延迟 + 写入尖刺）⇒ **`repl-backlog-size 512mb`**。

代价要算清：backlog 是**主库一份**（不是每个从库一份），512 MB 对一台 64 GiB 的实例是 0.8%；但**每个从库的 replication output buffer 是独立的**，三个从库同时落后就是三份。所以「调大 backlog」便宜，「从库集体慢」很贵。

还有两条配套事实：`repl-backlog-ttl`（默认 3600 s）——没有任何从库连着超过这个时长，主库就把 backlog 释放，之后重连必然全量；以及**切主之后 backlog 没了**，新主是从从库长出来的，`replid` 会换（旧 replid 存进 `master_replid2` 给一段宽限期），这段时间里旧主的从库还能部分重同步，超过宽限期就只能全量。

---

## 14.8 三本账：fork 停顿、同步带宽与丢失窗口

下面每个算例都给「公式 + 代入 + 假设清单 + 性质」。**「量级结论」不是承诺值**——换假设（页大小、填充率、链路效率）会在 2–4 倍内浮动，但结论的形状不变。

### 14.8.1 五个公式，一次算完

```
① fork 停顿（数据集 R = 64 GiB，快照期间写 8 GB）
   页数     = 64 GiB ÷ 4 KiB          = 16.78M
   PTE 字节 = 16.78M × 8 B            = 134 MB
   停顿     = 16.78M × (20~40 ns/页)  = 0.34 ~ 0.67 s  ⇒ 每 GB 5~10 ms
   对照：12 篇的 300 ms 换挡线 ⇒ 约 30~60 GB 越线

② COW 峰值内存
   上界   = R（每页最多复制一次）= 64 GB 天花板，与写入字节数无关
   触页   = 8,000 万 × 4 KiB = 32 GB（若每次写落在不同页，占全库 50%）
   实取 α = 0.15 ~ 0.45（jemalloc 一页服务多个小对象；纯新增 key 不产生 COW）
   峰值   = R × (1 + α) = 74 ~ 93 GB
   水位   = maxmemory ≤ 物理内存 ÷ (1 + α_max) = 96 GiB ÷ 1.45 ≈ 66 GiB

③ 全量同步（RDB 6 GB，1 Gbps 链路）
   有效带宽 = 1 Gbps ÷ 8 × 0.8 = 100 MB/s  ⇒ 传输 = 6 GB ÷ 100 MB/s ≈ 60 s（53~64 s）
   走盘净增 = 写 6 GB（与 fork 重叠）+ 读回 6 GB @500 MB/s ≈ 12~24 s（另占 6 GB 盘）
   buffer 累积 = 9.6 MB/s × 60 s = 576 MB vs 默认 limit 256 MB ⇒ 27 s 处被断开

④ backlog（8 万 QPS × 120 B/命令）
   写入速率 = 80,000 × 120 B = 9.6 MB/s
   默认覆盖 = 1 MB ÷ 9.6 MB/s = 104 ms
   3 s 闪断需 28.8 MB；30 s 闪断需 288 MB ⇒ 配 512 MB（覆盖 53 s）

⑤ 恢复时长（2,000 万条 AOF vs 6 GB RDB）
   AOF = 2,000 万 × 1.5~3 μs = 30 ~ 60 s（实测口径 10~20 万命令/s ⇒ 100 ~ 200 s）
   RDB = 6 GB ÷ 500 MB/s     = 6 ~ 20 s       ⇒ AOF 慢 3 ~ 15 倍，瓶颈不同
```

### 14.8.2 fork 与 COW：三条从算式里掉出来的结论

1. **fork 停顿与数据集成正比**（页表恒占数据 0.195%）：64 GiB ⇒ 0.34–0.67 s，100 GiB ⇒ 0.8–1.0 s；12 篇那条 300 ms 换挡线大致对应 30–60 GB 的数据集。
2. **COW 峰值由「写入的分散度 α」决定，不由「写入的量」决定**：同样写 8 GB，落在 2 万个页上是复制 78 MB，落在 800 万个页上是复制 32 GB。判断 α 只能看快照期间 `used_memory` 的涨速，不能看写入字节数。
3. **上限永远是数据集本身**：每页最多复制一次，所以 `R × 2` 是这份账单的绝对上界；`maxmemory` 必须按这个上界留余量，按「常见 10–30%」做容量规划就是事故一。

> **性质：量级结论。** 假设：4 KiB 页、x86-64 四级页表、8 B/PTE、无 THP、每页内核处理 20–40 ns、`maxmemory=0` 时 `used_memory ≈ RSS`（差一个 jemalloc 碎片因子 1.05–1.15）。若开 THP，fork 会快 3–5 倍但 COW 按 2 MiB 粒度放大，峰值可能顶到 `1 + α` 的真实上限 = 100%；若写入全部落在少数热页上，α 会降到 0.02 以下、峰值几乎不动。**「快照期间内存可能翻倍」是这份账单的唯一上界，工程设计要按上界留余量，按中位数做容量规划是事故一。**

### 14.8.3 全量同步：三笔开销，只有第三笔会要命

**第一笔是传输本身**：1 Gbps 线速 125 MB/s，扣掉 TCP 与单进程写 socket 的损耗（0.75–0.9）后约 100 MB/s，6 GB 要 **60 s（53–64 s）**。**第二笔是走盘**：写 6 GB + 读回 6 GB（各 12 s @500 MB/s），净增约 12–24 s 与 6 GB 盘占用，三个从库并发就是 18 GB——这笔在 diskless 下是 0，但要拿「子进程活满整个传输时长」去换。

**第三笔才是要命的**：传输这 60 s 里主库没停，写入照旧产生 `9.6 MB/s × 60 s = 576 MB` 增量，全部堆在**该从库**的 replication output buffer 里。默认 `client-output-buffer-limit replica` 是 hard **256 MB**、soft 64 MB×60 s，于是反解出 `256 MB ÷ 9.6 MB/s = 27 s`：**只要「RDB 传输 > 27 s 且写入不停」，默认配置下这次全量同步必然被主库自己掐断**，然后重连、重新落后、再全量。这就是「一次重启打爆集群」的完整算术。

> **性质：量级结论。** 假设：1 Gbps 有效 80%、单条复制连接单线程发送、主库写入在传输期间不降速、RDB 6 GB 对应约 12–14 GB 数据集。修法只有三个方向：**提带宽**（把传输压到 27 s 内 ⇒ ≥ 2 Gbps 有效）、**放大缓冲**（`client-output-buffer-limit replica 1024mb 512mb 120`，代价是这块内存在逐出判定之外，主机必须留出来）、**降写入**（全量同步期间限速业务写）。三选一，别指望「Redis 会自己处理好」。

### 14.8.4 backlog：该设多少，代进去就知道

写入速率 `80,000 × 120 B = 9.6 MB/s`，默认 1 MB 只覆盖 **104 ms** —— 任何「connect 重试 + TCP 重传 + 从库 GC 停顿」超过 100 ms 的抖动都会触发全量。按「允许断开时长 × 写入速率」往上顶：3 s 闪断要 28.8 MB，30 s 闪断要 288 MB，留 2 倍安全系数就是 **512 MB（可覆盖 53 s）**；配 1 GB 可覆盖 107 s，成本是主库内存的 1.6%（64 GiB 实例）。注意大促：写入放大 3 倍 ⇒ 28.8 MB/s，512 MB 只能覆盖 18 s，**每次大促前都要重算一次这个除法**。

> **性质：量级结论。** 假设：写入速率平稳、命令平均 120 B、无大 key 批量写、`repl-backlog-ttl` 未触发、切主事件不在窗口内。换成「大 key `MGET`/`DEL` 型写入」时单条命令几百 KB，同样的 QPS 会让速率涨 10 倍、覆盖时长掉到 10 s 级。**判断配置够不够的唯一指标是 `sync_partial_err`（§14.11），不是这个算式** —— 算式告诉你「该设多少」，指标告诉你「设得对不对」。

### 14.8.5 恢复时长：RTO 由两个完全不同的东西主导

**AOF 的成本是「命令数」**：2,000 万条 × 1.5–3 μs（比在线执行贵，多了 RESP 文本解析且没有 pipeline 收益）= **30–60 s**；按实测口径 10–20 万命令/s 则是 100–200 s（命令越「重」越慢：删一个百万元素的集合和写一个小字符串差三个数量级）。**RDB 的成本是「字节数」**：`6 GB ÷ 500 MB/s`（区间 300 MB/s–1 GB/s，简单 string 快、嵌套 hash/zset 慢）= **6–20 s**。两者差 3–15 倍，而且优化手段不通用：AOF 想快就得多做重写把尾部缩短，RDB 想快只能靠更快的盘与更多内存带宽。

7.0 默认 `aof-use-rdb-preamble yes` 的混合形态是两者的合体：base 文件走 RDB 速度，incr 尾部走 AOF 速度，于是 `恢复时长 ≈ RDB(base) + 尾部命令数 × 1.5 μs`——**重写频率直接决定 RTO**：incr 只攒 2 分钟，尾部就永远很短。

> **性质：量级结论。** 假设：单线程载入（Redis 载入 RDB 也是单线程重建对象）、无 swap、页缓存足够、AOF 文件无损坏。若主机被 swap，载入会慢 10 倍以上并可能永远拉不起来。RDB 载入期间实例返回 `-LOADING`，**这段时间计入 RTO，不计入 RPO**。

### 14.8.6 五个数合成一张决策表

```python
# 三个数的复算脚本：改假设就能重算，别把结论当承诺值引用
GiB, KiB = 1 << 30, 1 << 10

def fork_cost(dataset_gib, ns_per_page=30):
    pages  = dataset_gib * GiB // (4 * KiB)
    pte_mb = pages * 8 / 1e6                       # 每页 1 个 PTE，8 B
    return pages, pte_mb, pages * ns_per_page / 1e9  # 页数, PTE 字节, 秒

def cow_peak(dataset_gib, alpha):
    return dataset_gib * (1 + alpha)               # 峰值 RSS 上界

def full_sync(rdb_gb, link_gbps, wire_eff=0.8):
    bps = link_gbps * 1e9 / 8 * wire_eff           # bit → Byte，再打折
    return rdb_gb * 1e9 / bps, bps

def backlog(qps, bytes_per_cmd, seconds, safety=2.0):
    rate = qps * bytes_per_cmd                     # B/s
    return rate, rate * seconds * safety / 1e6     # B/s, 需要的 MB

print("fork 64GiB  ", fork_cost(64))               # (16.78M, 134 MB, ~0.5 s)
print("fork 100GiB ", fork_cost(100))
print("COW 峰值    ", cow_peak(64, 0.15), cow_peak(64, 0.45))
t, bps = full_sync(6, 1.0)
print("6GB@1Gbps   ", round(t), "s", round(bps / 1e6), "MB/s")
rate, need = backlog(80_000, 120, 30)
print("写入速率    ", round(rate / 1e6, 1), "MB/s → 30s 需", round(need), "MB")
print("1MB 覆盖    ", round(1e6 / rate * 1000), "ms")
```

| 场景 | 关键数 | 结论形状 | 该动的旋钮 |
| --- | --- | --- | --- |
| 64 GiB 快照 | fork 0.34–0.67 s；峰值 74–93 GB | 每次快照停半秒 | `save` 改稀疏、`maxmemory` 按 `÷(1+α)` 定、拆实例 |
| 6 GB 全量 @1 Gbps | 传输 60 s；buffer 需 576 MB > 默认 256 MB | 默认配置下全量同步必然中断 | 提带宽 / `client-output-buffer-limit` / 限写 |
| 8 万 QPS backlog | 9.6 MB/s；1 MB = 104 ms | 每次抖动都全量 | `repl-backlog-size` ≥ 512 MB |
| 2,000 万条 AOF | 30–200 s；RDB 6–20 s | RDB 快一个数量级 | 用 RDB 做 base、提高重写频率 |

---

## 14.9 架构边界：持久化能保证什么、不能保证什么

### 三列边界

| 你想保证的事 | Redis 能保证吗 | 靠什么（或绕什么） |
| --- | --- | --- |
| 进程崩溃后数据回来 | **能**，到「最后一次成功持久化点」 | RDB：上次快照；AOF：最后一次 fsync |
| 整机掉电后数据回来 | **部分**：`always` ≈ 0 条，`everysec` ≤ 1 s（实际看 fsync 间隔），`no` 几十秒 | 只有 `always` + 低 QPS 才接近「不丢」 |
| 主库宕机不丢 | **不能**。复制是异步的，切换必然丢「主库已 ACK、尚未到新主」的那一段 | 只有 `WAIT` + 业务侧可补偿 + 对账，才能把损失「定价」而不是「消灭」 |
| 从库读到最新 | **不能**。从库是字节流的被动接收方，永远落后于主库的 `master_repl_offset` | 读己之写靠「写后按 key 回主库读」，或按 offset 做会话粘滞 |
| 跨 key 事务性落盘 | **部分**：`MULTI/EXEC` 在 AOF 里是一个整体，但**无回滚**（运行期出错不回退） | 别把 Redis 当有事务的库用 |
| 一份一致性快照（备份/全量同步） | **能**，且几乎不停机 | fork + COW：主线程只停「复制页表」那 0.3–0.7 s |
| 快照期间内存不变 | **不能**。峰值 = `R × (1 + α)`，α 由期间写入的分散度决定 | 留余量、控写入、拆实例、关 THP |
| 从库慢慢跟得上 | **不能保证**。输出缓冲超限主库就断它 | `client-output-buffer-limit` + 监控 `omem` + 从库别跨高延迟链路 |

### `WAIT` 的真实语义：它买到的是「到达」，不是「安全」

`WAIT numreplicas timeout` 做的是：把当前客户端的连接阻塞住（`blocked_clients +1`，**主线程不阻塞**），直到 `numreplicas` 个从库确认「已经收到并处理了截至当前 `master_repl_offset` 的这段字节流」，或超时返回实际到达数。**它不 fsync、不回滚、不改变主库已经 ACK 的事实**：

- 主库自己没落盘（`appendfsync everysec` 下最坏还有 1 s 没落）—— `WAIT` 之后主库掉电，写在从库、主库本地丢，勉强能救；
- 从库收到的是**内存里的数据**，从库自己的 AOF 也没 fsync —— `WAIT` 之后两台同时掉电，全丢；
- 超时返回 `n < numreplicas` 时**不会回滚**，主库的写入依然有效 —— 你必须自己决定怎么办。

```go
// WAIT 只保证「数据到达了 N 个副本的内存」。它不 fsync，也不回滚。
// 返回值是「timeout 内确认到达的副本数」，不是成功/失败。
func SetAndConfirm(ctx context.Context, rdb *redis.Client, key, val string) error {
    if err := rdb.Set(ctx, key, val, 0).Err(); err != nil {
        return err // ① 已进主库内存 + aof_buf，尚未 fsync
    }
    // ② 等 2 个副本确认；40 ms 是「同机房 RTT 0.3 ms + 从库处理」的量级，不是拍的
    n, err := rdb.Wait(ctx, 2, 40*time.Millisecond).Result()
    if err != nil {
        return err // 本地超时：状态未知，绝不当失败回滚（数据已生效）
    }
    if n < 2 {
        // ③ 此刻的事实：副本不足。三条路，选一条写进预案，别留给 oncall 现场想：
        //    a) 记录并继续（可用性优先，接受 RPO 变大）
        //    b) 降级为「只读/拒绝新单」（一致性优先）
        //    c) 写审计表，靠对账补（事后修复）
        metrics.Inc("redis.wait_shortfall", float64(2-n))
    }
    return nil
}
```

**`min-replicas-to-write` / `min-replicas-max-lag`** 是把同一个判断搬到主库侧、对**所有**写命令生效的版本：健康从库数少于 `min-replicas-to-write`（默认 0 = 关）就拒绝写；「健康」的定义是 `master_last_io_seconds_ago ≤ min-replicas-max-lag`（默认 10 s）。版本纪律：**5.0 起配置名从 `slave-*` 改为 `replica-*`，`min-slaves-*` 作为别名保留**；而 `INFO replication` 里的字段名至今仍是 `slave0/slave1`（只改了配置，没改 INFO 字段）。

两个必须同时设的理由：只设 `min-replicas-to-write 1` 而不管 lag，一个「连接还在但已经卡住 3 分钟」的从库照样算「活着的 1 个」——**这就是「假活从库也算数」，它把保护机制变成了安慰剂**。

### 故障时点 × 配置 × 会丢什么

| 故障时点 | `appendfsync` | 复制 | 会丢什么 | 备注 |
| --- | --- | --- | --- | --- |
| 从库进程被 kill | 任意 | 异步 | 不丢数据，丢「从库本地的未 fsync 尾部」 | 重连后 `PSYNC` 从 backlog 续上（只要没超 backlog） |
| 主库进程崩溃（主机活着） | `everysec` | 异步 | ≤ 1 s 的主库写；若从库在线且未被切走，从库上有这段数据 | 人工/工具从从库「捞」是常见补救 |
| 主机整体掉电（主库） | `everysec` | 异步 | **最后一次 fsync 之后的所有写**（事故三：1.26 s × 1.3 万写 = 1.6 万条） | 从库若活着，数据还在它内存里 |
| 主机整体掉电（主 + 从同机房） | `everysec` | 异步 | 主库 1 s + 从库 1 s，且两份不重叠的窗口都丢 | 跨机房副本的唯一价值就在这里 |
| 主库宕机后 Sentinel 提升从库 | 任意 | 异步 | 「主库已 ACK、未到新主」的那段（典型几十 ms 的字节流，按 9.6 MB/s = 每秒 9.6 MB） | 提升越快，丢得越少；但快提升 = 更容易误判 |
| 全量同步进行到一半，主库崩 | 任意 | 全量 | 从库数据集被清（`on-empty-db`），且没有可用主库 | 这是「一次重启打爆集群」的典型路径 |
| 从库输出缓冲超限被主库断开 | 任意 | 全量 | 从库掉线期间的数据全靠 backlog/全量补 | 若 backlog 不够 ⇒ 又一轮全量 |

### 与 MySQL 复制的六列对照（细节全部锚 `→ comp/10`）

同为「主从 + 全量/增量」，MySQL 与 Redis 的差异全部来自「增量载体是什么」：MySQL 是**结构化的 binlog event**（可解析、可定位、可按事务并行），Redis 是**原始命令字节流**（只能顺序喂）。

| 维度 | MySQL（`→ comp/10`） | Redis（本篇） | 差异的物理原因 | 工程后果 | 细节锚 |
| --- | --- | --- | --- | --- | --- |
| 增量载体 | binlog event（行/语句，结构化） | 命令字节流（原样追加） | Redis 没有「事务提交」这个落点，只有「命令执行完」 | Redis 无法按内容过滤/改写，也没法「跳过一条坏 event」 | `→ comp/10 §10.3` |
| 位点标识 | GTID 或 `file + pos` | `master_replid + offset`（字节偏移） | offset 是**字节数**，不是事件序号 | 从库无法「理解」自己落后多少事务，只能报字节 | §14.7 |
| 续传机制 | relay log 落盘保留 + `auto_position` 重拉 | 内存里的 `repl_backlog` 环，默认 1 MB | MySQL 靠盘，Redis 靠内存环 | **主库重启 / 切主 → backlog 消失；MySQL 的 relay log 还在盘上** | §14.7 |
| 并行回放 | worker 线程按 `last_committed` / 写集并行 | **单线程顺序应用**（无并行概念） | Redis 的命令是「改内存」，无法判定两条命令是否冲突 | 从库应用能力上限 = 主库写入速率的 1 倍，`repl-backlog` 之外没有第二道缓冲 | `→ comp/10 §10.5` |
| 同步语义 | 半同步可配（`after_sync`/`after_commit`），**会退化**为异步 | `WAIT` 是一次性询问；`min-replicas-*` 是写入闸门；**没有"半同步"，也没有退化告警这件事** | Redis 没有「等待副本 ACK 再提交」的提交点 | 要么全异步接受丢，要么用 `WAIT` 在业务层自建（代价：延迟 + 失败语义自负） | §14.9、`→ comp/10 §10.4` |
| 延迟主因 | 大事务 + 并行度不足（回放侧） | 网络 + 从库单线程应用 + 主库 fork 停顿（发送侧） | 一侧是 CPU/并行，一侧是带宽/单线程 | Redis 的延迟优化方向是「少写、快网、别 fork」，MySQL 是「拆事务、加 worker」 | `→ comp/10 §10.5` |

**一句话对照**：MySQL 复制是「一条可定位、可并行、可退化的日志流水线」；Redis 复制是「一根只能顺序倒的字节带」——**前者用复杂度换可控，后者用简单换能力上限**。

---

## 14.10 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因 | 定位 | 修复 | 防复发 |
| --- | --- | --- | --- | --- |
| 掉电丢了 1 秒以上的数据，AOF 状态全绿 | `everysec` 的 fsync 被重写/别的进程抢盘，实际间隔被拉长到 1.3 s+ | `INFO persistence` 的 `aof_delayed_fsync` 导数；`iostat` 看该盘的 `%util` 与 `await` | 持久化盘与业务盘/日志盘分离；重写窗口错峰；必要时改 `always`（付吞吐） | 把「fsync 间隔」当 SLI 监控，而不是把 `appendfsync` 当承诺 |
| 大内存实例 BGSAVE 期间内存翻倍、被 OOM killer 杀 | COW 按 4 KiB 页复制，峰值 = `R × (1+α)`，α 由期间写入分散度决定 | `INFO memory` 的 `used_memory` 峰值 vs `rdb_changes_since_last_save`；`INFO persistence` 的 `rdb_bgsave_in_progress` | `maxmemory ≤ 物理内存 ÷ (1+α)`；关 THP；拆实例 | 容量规划按「快照期间内存翻倍」的上界留余量，不做中位数规划 |
| 一次网络抖动就全量同步，`sync_full` 持续涨 | `repl-backlog-size` 留在默认 1 MB，覆盖时长 = `1 MB ÷ 写入速率`（9.6 MB/s ⇒ 104 ms） | `INFO stats` 的 `sync_partial_err` / `sync_full`；`INFO replication` 的 `repl_backlog_histlen` 与 `master_repl_offset` | `repl-backlog-size` 按「写入速率 × 允许断开时长 × 2」设 | 把「写入速率 → 需要的 backlog」写成公式进容量评审；大促前重算 |
| 全量同步反复失败，主库日志刷 `output buffer limits` | 传输期间增量堆进 replica output buffer，超过 `client-output-buffer-limit replica`（默认 256 MB） | `CLIENT LIST TYPE replica` 看 `omem`；主库日志关键字 | 放大 limit（如 `1024mb 512mb 120`）+ 提带宽 + 全量期间限写 | 全量同步时长 × 写入速率 必须 < buffer 上限，这条进变更检查 |
| 从库在同步期间被读到「空数据」 | `repl-diskless-load on-empty-db`：先清库再边收边载 | `INFO replication` 的 `master_sync_in_progress`；从库 `-LOADING` 报错率 | 改 `swapdb`（7.0+，付 2× 内存）或 `disabled`（付磁盘） | 全量同步期间把该从库从读流量里摘掉，写进运维手册 |
| AOF 把盘写满，主库 `MISCONF` 拒绝写 | 重写触发太晚（`auto-aof-rewrite-min-size` 太大 / `percentage` 太高），增长期间没人管盘 | `INFO persistence` 的 `aof_rewrite_in_progress`、AOF 目录大小、盘使用率 | 临时扩容盘 + 手动 `BGREWRITEAOF` | 盘使用率 70% 告警；`min-size` 与实例规模匹配（大实例别留 64 MB） |
| 设了 `min-replicas-to-write` 还是丢了数据 | 没设 `min-replicas-max-lag`：一个「连接在、心跳停了 3 分钟」的从库照样算活 | `INFO replication` 的 `slave0:…,lag=` 与 `master_last_io_seconds_ago` | 两个一起设（如 `1` + `5`） | 两个参数必须成对出现在配置基线里，只出现一个视为配置错误 |
| 从库内存比主库大 / 从库被 OOM | 主从耐久性配置不一致：从库默认 `replica-ignore-maxmemory yes`（不自己逐出），且编码阈值不同会重新编码 | 对比两侧 `CONFIG GET maxmemory`、`*-max-listpack-*`、`used_memory` | 统一配置；从库按主库 1.2–1.5 倍留内存 | 主从配置漂移进巡检；编码阈值纳入配置基线（`→ comp/13`） |
| 业务把 `WAIT` 当同步复制，切主后照样丢 | `WAIT` 只保证「到达 N 个副本的内存」，不保证落盘、不回滚、超时不报错 | 灰度演练：`WAIT` 成功后同时 kill 主从，看数据在不在 | 明确定义 RPO；`WAIT` + 对账 + 幂等，三件一起上 | 「`WAIT` ≠ 同步复制」写进架构约束；用两条命令的延迟数据说话 |
| fork 时延突然从 200 ms 涨到 1.5 s | 开了 THP（COW 变 2 MiB 粒度）或主机被别的进程换页/抢占 | `latest_fork_usec` 趋势；`/sys/kernel/mm/transparent_hugepage/enabled`；`vmstat` 的 `si/so` | 关 THP；给 Redis 独占核 / 隔离主机 | THP 状态进基线检查；Redis 启动日志里的 THP 告警必须有告警项 |

---

## 14.11 观测：指标、拐点与告警

### 指标表（每条带版本 + 开关前提）

| 指标 | 取法 | 版本 / 开关前提 | 拐点 | 它说明什么 |
| --- | --- | --- | --- | --- |
| `rdb_bgsave_in_progress` | `INFO persistence` | 全版本 | 连续 > 30 s，或 24 h 占比 > 10% | 快照一直在跑 ⇒ fork+COW 长期开着，内存余量被持续占用 |
| `rdb_last_bgsave_status` | `INFO persistence` | 全版本，取值为 `ok`/`err` | 任何 `err` | 上一次快照失败（盘满、fork 失败），RPO 无声变大 |
| `rdb_changes_since_last_save` | `INFO persistence` | 全版本 | 增速与实际写入速率同步 ⇒ 快照规则没生效 | 快照期间它就是「COW 账单的分子」 |
| `aof_rewrite_in_progress` | `INFO persistence` | 全版本 | 24 h 占比 > 20% | 重写风暴：一直在给 AOF 瘦身 |
| `aof_rewrite_scheduled` | `INFO persistence` | 全版本 | 持续为 1 | 这次重写是「被推迟的」，通常是正在 BGSAVE，两次 fork 排队 |
| `aof_last_bgrewrite_status` | `INFO persistence` | 全版本；7.0+ multi-part 下仍适用 | 任何 `err` | 重写失败 ⇒ AOF 只增不减 ⇒ 盘迟早满 |
| `aof_delayed_fsync` | `INFO persistence` | 全版本，累计值（看导数） | 导数 > 0 且持续 | **`everysec` 正在被拖长**：后台 fsync 没完，下一次只能排队（事故三的元凶） |
| `latest_fork_usec` | `INFO persistence` | 全版本 | **> 300 ms**（12 篇换挡线）；趋势上行 | 每次快照/全量同步让主线程停多久；**唯一能直接换算成用户损失的持久化指标** |
| `sync_full` | `INFO stats` | 全版本，累计值 | 出现增长（尤其两次抖动之间） | 发生了全量同步 ⇒ 带宽与 fork 双份代价 |
| `sync_partial_ok` | `INFO stats` | 全版本，累计值 | 占比应 > 95% | 续传成功的次数 |
| `sync_partial_err` | `INFO stats` | 全版本，累计值 | **任何非零增长都要查** | **判断 backlog 够不够的第一指标**：`err` 涨 = 副本落后的字节已被覆盖 |
| `master_repl_offset` / `slave_repl_offset` | `INFO replication` | 全版本；字段名仍是 `slave*`（5.0 只改了配置名，INFO 字段没改） | 差值持续扩大 | 真实复制延迟（字节），不与秒数混用 |
| `repl_backlog_active` / `repl_backlog_histlen` / `repl_backlog_size` | `INFO replication` | 全版本 | `histlen/size > 0.8` 且持续 | 环快被写满 ⇒ 一旦断连就必然全量 |
| `master_link_status` | `INFO replication` | 从库侧；取值为 `up`/`down` | 任何 `down` | 链路断了；配合 `master_link_down_since_seconds` 看断了多久 |
| `master_last_io_seconds_ago` | `INFO replication` | 从库侧；主库侧对每个从库也有 `lag` | **> `min-replicas-max-lag`** | 「假活从库」的唯一判据：连接在但很久没数据 |
| `master_sync_in_progress` | `INFO replication` | 从库侧 | 为 1 时该从库不可信 | 全量同步进行中，读它可能拿到旧库或空库 |
| `omem`（复制连接） | `CLIENT LIST TYPE replica` | 6.0+ 支持 `TYPE` 过滤 | 接近 `client-output-buffer-limit replica` | 「从库慢 → 主库断它 → 又全量」的现场 |
| `used_memory` 峰值 | `INFO memory`（`used_memory_peak`） | 全版本 | 快照窗口内涨 > 50% | COW 账单在显形；对照 `rdb_bgsave_in_progress` 确认因果 |
| `LATENCY HISTORY fork` | `LATENCY HISTORY fork` | **需 `latency-monitor-threshold > 0`（默认 0 = 关）**；与 7.0 的 `latency-tracking` / `INFO latencystats` 是两套东西 | 事件高度 > 300 ms | fork 停顿时序；`LATENCY GRAPH fork` 可看分布 |
| `LATENCY HISTORY command` | `LATENCY HISTORY command` | 同上（需开阈值）；7.0+ 另有 `INFO latencystats` 分命令分位 | 与快照窗口重合的尖刺 | 分辨「是 fork 拖的」还是「命令自己慢」（`→ comp/12 §12.12`） |

### 采集命令

```bash
# ① 持久化状态巡检：一次看全「快照/重写/fork/fsync 退化」
redis-cli INFO persistence | grep -E \
 'rdb_bgsave_in_progress|rdb_last_bgsave_status|rdb_changes_since_last_save|rdb_last_save_time|aof_rewrite_in_progress|aof_rewrite_scheduled|aof_last_bgrewrite_status|aof_delayed_fsync|latest_fork_usec'

# ② 主从与 backlog：sync_partial_err 比任何延迟指标都先告警
redis-cli INFO replication | grep -E \
 'role|connected_slaves|master_replid|master_repl_offset|repl_backlog_active|repl_backlog_size|repl_backlog_first_byte_offset|repl_backlog_histlen|master_link_status|master_last_io_seconds_ago|master_sync_in_progress|slave[0-9]'
redis-cli INFO stats | grep -E 'sync_full|sync_partial_ok|sync_partial_err'

# ③ 每条复制连接的输出缓冲：omem 逼近 limit 就是在等一次断连
redis-cli CLIENT LIST TYPE replica | awk '{for(i=1;i<=NF;i++){if($i ~ /^omem=/){split($i,a,"="); if(a[2]+0>67108864) print}}}'

# ④ fork 与时延事件：阈值默认关，先开（单位 ms）
redis-cli CONFIG GET latency-monitor-threshold
redis-cli CONFIG SET latency-monitor-threshold 100
redis-cli LATENCY HISTORY fork
redis-cli LATENCY DOCTOR

# ⑤ 关键配置快照：主从两侧对比，找配置漂移
for k in appendfsync appendonly save auto-aof-rewrite-min-size repl-backlog-size \
         client-output-buffer-limit repl-diskless-load min-replicas-to-write \
         min-replicas-max-lag maxmemory; do
  echo "$k = $(redis-cli CONFIG GET $k | tail -1)"
done
```

### 告警表达式骨架

```yaml
# ① fork 停顿：唯一的「持久化 → 用户损失」直连指标
- alert: RedisForkSlow
  expr: redis_latest_fork_usec > 300000
  for: 10m

# ② 部分重同步失败增长：backlog 不够的唯一硬证据
- alert: RedisPartialResyncFailing
  expr: increase(redis_sync_partial_err_total[30m]) > 0

# ③ fsync 被拖长：everysec 的真实退化
- alert: RedisAofFsyncDelayed
  expr: increase(redis_aof_delayed_fsync_total[10m]) > 0
  for: 10m

# ④ 复制链路断开 / 假活
- alert: RedisReplicasUnhealthy
  expr: redis_connected_slaves < 1 or redis_master_link_status{role="replica"} == 0
  for: 2m

# ⑤ 内存水位：快照窗口内要按「可能翻倍」看，不是按 maxmemory 看
- alert: RedisMemoryNearHostLimit
  expr: redis_memory_used_rss_bytes / on(instance) node_memory_MemTotal_bytes > 0.75
  for: 5m
```

最后一条的指标名随 exporter 版本可能不同（`redis_memory_used_rss_bytes` / `redis_memory_used_bytes`），采集不到就退回 `node_memory_MemAvailable_bytes` 与 `used_memory_peak` 的组合——**指标可以换名字，「快照期间内存可能翻倍」这条事实不能没有告警**。

---

## 14.12 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 唯一真问题 | 不是「怎么把数据写下去」，是「怎么不阻塞唯一线程地拿到一致快照」+「backlog 决定了高可用是真是假」 |
| RDB | 某一时刻的结果快照；二进制紧凑、载入按**字节**算；只能整份重写 |
| AOF | 按序的过程日志；可读可修、体积大、载入按**命令数**算 |
| 7.0 multi-part AOF | base + incr + manifest；重写期间新开 incr，**内存里不再有 rewrite 双缓冲** |
| fork 的本质 | 复制页表，不复制数据页；PTE 8 B / 4 KiB 页 ⇒ 页表约占数据 0.195% |
| fork 停顿 | 64 GiB ⇒ 16.78M PTE ⇒ 134 MB；16.78M × 20–40 ns = 0.34–0.67 s（每 GB 5–10 ms） |
| COW 峰值 | `R × (1 + α)`，α = 被触及页占比；**上限是数据集本身**，与写了多少字节无关 |
| COW 放大器 | 页粒度：写 100 B 可能复制 4 KiB（40×）；开 THP 变 2 MiB（2 万×） |
| `maxmemory` | ≤ 物理内存 ÷ (1 + α_max)；常见 α 10–30%，事故时能到 80%+ |
| 三档 fsync | `always` 0 条/吞吐塌；`everysec` ≤1 s（实际看 fsync 间隔）；`no` 几十秒 |
| everysec 的谎言 | 「每秒一次」≠「间隔一秒」；被抢盘时 `aof_delayed_fsync` 涨，窗口跟着涨 |
| `no-appendfsync-on-rewrite yes` | 重写期间不 fsync：买延迟、卖窗口（窗口 ≈ 重写耗时） |
| 重写风暴 | 重写刚完增量又到 100% ⇒ 立刻再 fork；看 `aof_rewrite_in_progress` 占比 |
| 全量同步链路 | `PSYNC` → fork → RDB（走盘或走 socket）→ 从库 `LOADING` → 补发增量 |
| diskless 取舍 | 省 12 s 与 6 GB 盘占用；代价是 fork 时长 = 传输时长，链路断则重来 |
| `repl-diskless-sync-delay` | 默认 5 s，合并多从库的请求为一次 fork；**这 5 s 要算进 RTO** |
| `repl-diskless-load` | `disabled` / `on-empty-db`（载入期间是**空库**）/ `swapdb`（7.0+，2× 内存） |
| output buffer 因果链 | 从库慢 → 增量堆在主库 buffer → 超 256 MB → 主库断它 → 又全量 → 又堆 |
| `repl-timeout` | 默认 60 s，必须 ≫ `repl-ping-replica-period`（10 s），否则传输中被踢 |
| backlog 的本质 | 内存里的**环形字节流**，不是时间窗口；默认 1 MB |
| backlog 覆盖时长 | `size ÷ (QPS × 命令字节)`；8 万 × 120 B ⇒ 1 MB = **104 ms** |
| backlog 该怎么设 | 写入速率 × 允许断开时长 × 2；30 s 闪断 ⇒ 512 MB |
| PSYNC 三条件 | replid 认识（或 `master_replid2`）+ offset 未被覆盖 + 数据集未换 |
| `sync_partial_err` | **判断 backlog 够不够的第一指标**；任何增长都要查 |
| `WAIT` | 只保证「到达 N 个副本的内存」；不落盘、不回滚、超时不报错 |
| `min-replicas-*` | 5.0 起从 `slave-*` 改名；**两个必须成对设**，否则假活从库也算数 |
| 与 MySQL 复制的差别 | 它是可定位可并行的 binlog 流水线；Redis 是一根只能顺序倒的字节带（`→ comp/10`） |
| 换形态的信号 | `latest_fork_usec` > 300 ms 且数据 > 25 GB；要 RPO=0；要跨 key 事务 |

---

## 14.13 自检题（5 题）

**1. 一个 64 GiB 的 Redis 实例，`maxmemory 0`、无 swap、`save 900 1` 默认规则，业务在 12:00 做了一次 8 GB 的批量回填，12:03 实例被 OOM killer 杀了。请把内存从 64 GB 到 93 GB 的过程算出来，并说明为什么「只写了 8 GB、涨了 29 GB」不是 bug。**
<details><summary>答案</summary>

过程：某个时刻触发了 `BGSAVE` → `fork()` 复制 16.78M 条 PTE（约 134 MB，主线程停 0.34–0.67 s）→ 子进程开始遍历写 RDB → 父进程继续接写入。父进程改任何一页时触发写保护缺页，内核把**整页 4 KiB** 复制一份，COW 账单开始累积。

为什么 8 GB 变成 29 GB：COW 的计量单位是**页**，不是写入字节。回填把 8 GB 分散在几乎整个键空间上，触及页占比 α 达到 0.45，于是 `64 × (1 + 0.45) = 92.8 GB`。反过来，如果这 8 GB 全部写在同一批热 key 上（页复用率高），α 可能只有 0.02，峰值几乎不动；如果全部是**新增** key（jemalloc 从新页分配），α ≈ 0，也不产生 COW。

三条结论：① 上限是数据集本身（每页最多复制一次），与写入量无关；② 峰值由「写入的**分散度**」决定，不由「写入的**量**」决定；③ `maxmemory` 必须按 `物理内存 ÷ (1 + α_max)` 定，α 取 0.45–1.0，因为最坏情况（全库被扫一遍）是 2×。
</details>

**2. 主库写入 8 万 QPS、平均命令 120 B、`repl-backlog-size` 用默认 1 MB。一次 3 秒的机房抖动之后，你会在 `INFO stats` 里看到什么？为什么调大 `repl-backlog-size` 是唯一正确的第一反应，而「升级网络」不是？**
<details><summary>答案</summary>

会看到 `sync_partial_err` 增长 3 次（每个从库一次）、`sync_full` 也涨 3 次；随后 `latest_fork_usec` 出现一次新的记录，主库到从库的出口带宽被打满，`CLIENT LIST TYPE replica` 里的 `omem` 逼近 limit。

算术：写入速率 `80,000 × 120 B = 9.6 MB/s`；1 MB 的环覆盖 `1 ÷ 9.6 ≈ 104 ms`；3 秒抖动需要保留 `9.6 × 3 = 28.8 MB` —— 差 29 倍，必然 `FULLRESYNC`。

为什么先调 backlog：网络抖动是**不可消除**的（BGP 收敛、交换机升级、TCP 重传、从库 GC 停顿、云厂商维护），任何链路都会断，区别只是断多久。backlog 决定的是「断多久以内不用还债」，是一个纯容量问题，改配置即可生效；而「升级网络」降低的是**抖动发生的概率**，不改变「抖了就要全量」这个性质。正确的组合是：先把 backlog 按 `写入速率 × 可容忍断开时长 × 2` 设到 512 MB，再用网络冗余去降低抖动频率——**顺序不能反**。
</details>

**3. `appendfsync everysec` 号称最多丢 1 秒。事故三里丢了 1.26 秒的数据，`aof_last_write_status` 还是 `ok`。请解释这个「1 秒」到底由什么决定，以及在事故前你会用哪个指标提前发现。**
<details><summary>答案</summary>

「1 秒」是**发起频率**，不是**实际间隔**。`everysec` 的实现是：主线程 `write()` 进页缓存（快，不落盘），bio 线程每秒调一次 `fsync()`。如果上一次 `fsync` 还没返回（盘被打满、AOF 重写正在抢 IO、同机别的进程做备份），这一次只能排队——实际间隔被拉长到 1.3 s、4 s 甚至更久，而**这期间所有 `write()` 都是成功的**，所以 `aof_last_write_status` 依然是 `ok`，`aof_last_bgrewrite_status` 也是 `ok`，`redis-check-aof` 也只报「尾部干净」。

提前发现的指标是 `aof_delayed_fsync`（`INFO persistence`，全版本）：它统计的是「因为已有 fsync 在执行而被迫推迟的次数」，导数为 0 时 everysec 名副其实，导数持续大于 0 时窗口正在被拉长。配合 `iostat` 看该盘的 `%util` 与 `await`，能直接定位到「谁在抢盘」。

防复发：持久化盘独立（不与 AOF 重写、日志采集、备份共盘）、重写窗口错峰、把「fsync 实际间隔」当 SLI 而不是把参数当承诺。
</details>

**4. 有人提议「把 `min-replicas-to-write` 设成 1，我们就不会丢数据了」。这句话错在哪？要让这句话接近成立，还需要哪几件事，它们各自的代价是什么？**
<details><summary>答案</summary>

错在两点。

第一，**它保护的是「写入时不接受孤家寡人」，不是「写下去的数据不会丢」**。`min-replicas-to-write 1` 只保证「至少有一个健康从库时主库才接受写」；而「健康」的定义是 `master_last_io_seconds_ago ≤ min-replicas-max-lag`（默认 10 s）——**如果不设这个 lag，一个连接还在、心跳已经停了 3 分钟的从库照样算「1 个」**，保护机制变成安慰剂。两个参数必须成对设（5.0 起名字从 `slave-*` 改为 `replica-*`）。

第二，即使设对了，从库收到的也只是**内存里的数据**：从库自己的 AOF 没 fsync，从库崩了照样丢；主库与从库同时掉电，两份都不在盘上。

要让 RPO 接近 0 需要三件事：① `WAIT`（或 `min-replicas-*`）保证数据**到达** N 个副本；② 主从两侧 `appendfsync always` 保证**落盘**（代价：吞吐掉到 fsync 延迟的倒数，1000–10000 写/s 封顶）；③ 业务侧幂等 + 对账，处理「`WAIT` 超时不回滚」这一支（代价：多一次远程确认的延迟 + 一套对账流程）。三者都要，缺一个就只是把「丢多少」换了个位置。
</details>

**5. 一台从库正在全量同步一台 6 GB 的 RDB，1 Gbps 链路。主库同期的写入速率是 9.6 MB/s。为什么这次全量同步大概率会失败？请给出两种修法和各自的副作用，并说明为什么「把 `repl-timeout` 调大」不解决问题。**
<details><summary>答案</summary>

传输需要 `6 GB ÷ 100 MB/s ≈ 60 s`。这 60 s 里主库写了 `9.6 × 60 ≈ 576 MB`，这些增量堆在该从库的 replication output buffer 里（注意：**它是客户端缓冲，不占 `used_memory`，不参与逐出判定，但占用 RSS**）。`client-output-buffer-limit replica` 默认 hard 256 MB / soft 64 MB×60 s ⇒ 大约在 `256 MB ÷ 9.6 MB/s = 27 s` 时超限，主库主动断开这条复制连接（日志 `overcoming of output buffer limits`），从库重连、`PSYNC` 又落后、又全量——**风暴**。

两种修法：① 放大 limit，如 `client-output-buffer-limit replica 1024mb 512mb 120`——副作用是这块内存在逐出判定之外，主机必须额外留出 1 GB 级余量，否则换成主库 OOM；② 提高有效带宽（≥ 2 Gbps），把传输压到 27 s 以内——副作用是成本，且要求从库侧也能吃下。第三条是「全量同步期间限写」，副作用是业务受损，通常只在故障恢复期用。

`repl-timeout` 调大没用：它管的是「多久没动静算对方死了」，而这里是**主动断开**——是主库因为 buffer 超限主动执行的动作，不是超时误判。调大它只会让「链路半死」的判定更迟钝，反而延长故障窗口。
</details>

---

## 14.14 延伸阅读（锚点式）

- **执行内核与慢命令**：本篇的 `latest_fork_usec > 300 ms` 换挡线、`latency-tracking` 与 `LATENCY HISTORY` 的区别、内存水位与逐出的关系，全部来自 [12 单线程的真相](12-redis-single-thread.md) §12.7、§12.12——**先读 12 篇的指标表，再读本篇的 §14.11**，否则会把两套同名不同源的指标混在一起。
- **编码与阈值**：RDB 存逻辑数据、载入时按本机阈值重新编码 ⇒ 主从内存不一致（`→ comp/13 §13.5` 的单向门），以及「删除不缩容」如何让从库在长期运行后比主库更胖，见 [13 数据结构与底层编码](13-encoding-and-thresholds.md) §13.5、§13.10。
- **MySQL 复制的对照面**：binlog event 的结构、半同步的退化、并行回放度被大事务吃掉、MGR 的 Paxos 认证队列，全在 [10 主从复制与高可用](10-replication-and-ha.md) §10.3–§10.7。本篇 §14.9 的六列对照只是把「物理载体不同」这一条差异的后果列出来，不重复其推导。
- **分片是降 fork 代价的手段**：单实例内存是 fork 代价的唯一变量，所以「把一个 100 GB 实例拆成 10 个 10 GB」同时解决了 fork 停顿与 COW 峰值——但代价是跨槽多 key 操作失效，见 `→ comp/15`（16384 槽、MOVED/ASK 与扩容迁移）。
- **无主 quorum 的另一条路**：如果业务真的需要 `R + W > N` 那种「副本数算术」换一致性，Redis 这条路给不了，形态上是另一个家族，见 `→ comp/16`（Dynamo 家族的 N/R/W）。
- **缓存形态与失效语义**：什么时候 Redis 可以当数据源、什么时候绝对不能，逐出策略在两种用法下的语义差异，属形态决策，见 [architect/04 读路径与缓存形态](../architect/04-read-path-and-cache-shape.md)，本篇不重写。
- **数据迁移与切流**：AOF/RDB 之外还有「不停机迁移」这一整套（双写、影子流量、位点校验），属于迁移工程而非内核机制，见 [architect/20 数据迁移](../architect/20-data-migration.md)。
