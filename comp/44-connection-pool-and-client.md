# 44. 连接池与客户端：为什么 20 个连接就够

> **状态**：`已完成（初稿）` ｜ **所属**：第九部分 · 横切基础件 ｜ **编号**：44 / 53
> **本篇定位**：全仓唯一一处把「一条数据库连接从客户端 socket 到服务端线程/进程」以及「池大小、排队、超时链三者怎么互相放大」拆到**会话状态与线程数**这一层的地方。RPC 侧「一条 TCP 上多流复用」归 [40 RPC 与连接复用](40-rpc-and-connection-reuse.md)，与数据库连接不是同一个问题（无状态通道 vs 有状态会话）；事务组件把连接与行锁占住的账归 [43 事务组件](43-transaction-components.md) §43.3；慢查询为什么慢归 [05 InnoDB 的页与行](05-innodb-page-and-row.md) 与 [06 MVCC 与隔离级别](06-mvcc-and-isolation-levels.md)。本篇只回答一个问题：**为什么池大小不是瓶颈、排队与超时链才是，以及为什么客户端比数据库先崩。**
> **读法**：44.4 的「并发 = QPS × 持有时间」是全篇支点，后面所有算式都从它推；44.5 的超时链图值得在值班前默画一遍（缺一层就等于无限等）；44.8 的三本账可以换假设直接复算；只想背结论翻 44.12。

---

## 44.1 引子：三起「池越大越慢 / 客户端先崩」的事故

这一节的三起事故有一个共同形状：**数据库没有任何一条 SQL 报错，哨兵面板上 CPU、磁盘、连接数全是绿的**。出事的是三件从未被当作「容量与时间」对待的东西——**持有时间随并发变成什么、等待上限设成和业务超时同一量级之后会怎样、客户端那几个时间常数谁该小于谁**。它们都不是业务逻辑，所以从来没人给它们做过设计评审。

### 事故一：池从 20 调到 200，数据库线程先爆（2025-12-04，某订单中心）

某订单中心，MySQL 8.4，16 vCPU / 64 GB，`innodb_buffer_pool_size` 48 GB，`max_connections` 原为 1,000。应用 8 个 Pod，每 Pod 池 20 条（`maximumPoolSize=20`），单语句持有连接 4~5 ms，常态 QPS 3,400。压测报告上有一行「池等待 P99 = 40 ms」——值班读成「池不够」，把池调到 200。

| 时刻 | 动作 / 事件 | 数据库侧看到的数 |
| --- | --- | --- |
| 10:02 | 池 20 → 200，8 个 Pod × 200 = 1,600 条连接上限 | 为让它建得起来先把 `max_connections` 改成 2,000 |
| 10:05 | 高峰到来，库上同时活跃的会话从 9 个涨到 190 个 | `Threads_running` 9 → 190，CPU 用户态只涨 8%，`sys` 从 12% 涨到 41% |
| 10:06 | 单语句耗时从 4.5 ms 涨到 31 ms | redo 刷盘、页闩、行锁等待这几处 InnoDB 内部串行点成为瓶颈 |
| 10:08 | 持有时间涨 6.9 倍 ⇒ 需求并发再涨 6.9 倍 | 池又被占满，池等待 P99 从 40 ms 变成 2.4 s |
| 10:12 | 现场提议「那就调到 500」 | 每连接稳态内存约 0.4 MB × 1,600 ≈ 640 MB，且 `Aborted_connects` 上升、`innodb_row_lock_waits` 翻 3 倍 |
| 10:16 | 回滚到 20 条，同时给一条 3.2 s 的慢查询补索引 | `Threads_running` 回到 11，P99 回到 19 ms |
| 10:41 | 恢复 | 27 分钟，下单失败 9,400 笔，P99 峰值 2.4 s |

根因必须指到物理形态上：**持有时长不是常数，它是并发的函数**。20 条时同一条语句 4.5 ms；190 条时变成 31 ms——因为数据库真正能并行推进的会话数由 CPU 核数与 InnoDB 内部串行点（redo 刷盘、缓冲池页闩、锁系统）共同决定，16 核的现实并行度远小于 190，多出来的会话只是在 run queue 与锁队列里排队。**加池把「客户端排队」换成了「数据库内排队 + 锁竞争」，后者更贵：它同时抬高了所有会话的持有时长**（长事务与锁的账见 [06 MVCC 与隔离级别](06-mvcc-and-isolation-levels.md) §06.7）。

### 事故二：pool wait 30 s × 语句超时缺省 = 30 s 雪崩（2026-01-19，某会员系统）

同一套栈：HikariCP 5.x，`connectionTimeout` 用默认 30,000 ms，`maximumPoolSize=20`；Tomcat `maxThreads=200`；MySQL 侧没有配任何语句超时；网关 read timeout 5 s、失败重试 3 次。

14:20:03，备份用的 `mysqldump --single-transaction` 与一批运维 DDL 让 3 个长事务持锁 90 s，池里 6 条连接被占死，剩余 14 条要承载全部 400 req/s。

| 时刻 | 事件 | 排队量 |
| --- | --- | --- |
| 14:20:10 | 需求并发 = 400 × 0.03 s = 12，逼近剩余 14 条 | 池等待 P99 从 3 ms 涨到 900 ms |
| 14:20:30 | 20 条全部卡住，新请求按 `connectionTimeout=30 s` 开始等 | 200 个 Tomcat 线程全部进入等待 |
| 14:20:41 | 网关 5 s 超时 → 重试 3 次，到达量 ×4（重试放大见 [40 RPC 与连接复用](40-rpc-and-connection-reuse.md) §40.6） | 队列深度 6,000+ |
| 14:21:00 | **服务能力降到 200 ÷ 30 s ≈ 6.7 req/s**，而到达 1,600 req/s | 队列只增不减 |
| 14:54 | 长事务结束；池等待改 300 ms、语句超时 1 s、重试改 1 次 | 队列 40 s 内排空 |

乘法链：`吞吐上限 = 应用线程数 ÷ 单请求最坏等待 = 200 ÷ 30 s ≈ 6.7 req/s`。只要能允许一个请求等 30 s，**这台 200 线程的机器就退化成 6.7 req/s 的服务器**——超时的杀伤力不在「慢」，在于它把并发槽位锁死在无意义的等待里；而语句超时缺省，意味着这个 30 s 里数据库还在替这些请求干活。

> **量级结论（非承诺值）**。假设清单：① 同步 JDBC，线程被池等待阻塞而非异步交出；② 故障期间到达量不下降；③ 网关重试无退避、无预算。**换假设可复算：池等待改成 300 ms，同一台机器回到 200 ÷ 0.3 = 667 req/s；再叠加语句超时 1 s，单请求最坏占用降到 min(300 ms 等待 + 1 s 执行)。**

### 事故三：一次 Full GC + 一个 DNS TTL，让 800 条连接集体失效（2026-02-11，某交易网关）

JVM 32 GB 堆（G1），40 个 Pod × 池 20 条 = 800 条连接，数据库是 8 vCPU 的 MySQL 8.4 主库。

| 时刻 | 事件 | 现场 |
| --- | --- | --- |
| 09:31:02 | 一次晋升失败触发 Full GC，STW 2.7 s | 40 个 Pod 的应用线程同时停住；3,000 个在途请求全部越过网关的 1 s 超时 |
| 09:31:05 | 恢复瞬间 3,000 个请求同时取连接 | 池等待尖刺，库上 `Threads_running` 从 12 跳到 240 |
| 09:31:06 | 池里约 30% 的连接其实已被云 LB 的 60 s idle 超时静默剪断 | 取到死连接的请求先报 `Communications link failure`，再重建 |
| 09:31:08 | 建连要解析域名，而 `networkaddress.cache.ttl` 被某 SDK 文档建议设为 0（「避免 DNS 缓存」） | 每次建连一次 DNS 查询：解析 QPS 从 200 涨到 9,000 |
| 09:31:20 | CoreDNS 单副本被 9,000 QPS 打到限流，解析 P99 4.2 s | 新连接建不起来，错误从「SQL 超时」变成 `dial tcp: i/o timeout` |
| 09:49 | DNS 缓存 TTL 恢复 30 s、池清理死连接、把 LB idle 与池 `maxLifetime` 重新排序 | 连接数回到 800 |

三件事叠在一起才炸：**GC 制造同步脉冲**（所有人同时重连）、**idle 超时顺序错**（池以为连接活着、LB 已经剪了）、**DNS 无缓存**（重连风暴变成 DNS 风暴）。任意一件单独发生都不至于出事。数据库在整个过程中只看到连接数抖了一下——**这就是「客户端配置往往比数据库先崩」的字面含义**。

> 🩸 三起事故的共同点：**没有一处 SQL 写错，也没有一处参数「不合规范」**。① 是把池当吞吐旋钮；② 是把「等待上限」设成了与业务超时同一量级；③ 是客户端的三个时间常数没人排序。它们都发生在「连接池是一层共享准入资源」这个抽象漏出来的地方：**池掩盖了排队，超时是唯一让排队可见的东西，而它一旦缺省，排队就变成无限等待。**

---

## 44.2 定位与唯一真问题

**定位**：连接池不是「数据库连接的缓存」，它是**一层把「并发请求」映射到「有限会话」的准入控制**。它只做三件事：

| 池只做这三件事 | 物理落点 | 不做什么 |
| --- | --- | --- |
| **复用建连**（reuse） | 一次 TCP + TLS + 认证的成本摊到 N 次请求上；省掉的是握手（同 AZ 0.5–2 ms、跨 AZ 4–10 ms 量级）与连接对象的内存 | 不省带宽，不省服务端会话状态的内存 |
| **限制并发**（admit） | 一个信号量/队列：拿到槽位才能用连接，拿不到就等（`connectionTimeout`） | 不保证等待有秩序（默认无公平性，也没有优先级） |
| **回收坏连接**（evict） | `maxLifetime` / `idleTimeout` / 心跳探测 / 取用时校验 | 不保证探测一定准（探测与使用之间还有时间窗） |

因此它**不**负责：加速 SQL、隔离慢查询、保证会话状态干净、替数据库兜住排队、在故障时"自动扩容"。

### 唯一真问题

> **瓶颈从来不是池大小，是排队 + 超时链；客户端配置往往比数据库先崩。**

这句话可以拆成四条可验证的推论，全篇都在给它们找证据：

1. **数据库连接是「有状态的会话」，不是「无状态的通道」**。它带着当前事务、隔离级别、锁、用户变量、临时表、prepared statement 句柄、最后一个 Read View。所以池里的每个槽位都是一个**带副作用的对象**——这也是为什么很多"连接池 bug"其实是"会话状态泄漏"（44.3、44.7）。
2. **池大小是「瞬时并发上限」，不是「吞吐上限」**。吞吐 = `连接数 ÷ 持有时间`；持有时间由 SQL 与并发共同决定。把池从 20 加到 200，若持有时间因此从 5 ms 涨到 31 ms，吞吐反而下降（事故一）。
3. **排队同时发生在四层，只有超时让它可见**：应用线程池 → 池 → 数据库 run queue → 数据库锁队列。任何一层缺省上限，故障就会以「全部卡住直到超时」的形式结束（44.5）。
4. **客户端比数据库先崩，因为数据库一直有上限，客户端常常没有**：数据库有 `max_connections`、`wait_timeout`、`max_execution_time`；而客户端的 socket 读超时默认 0（无限）、Go 的 `sql.DB.MaxOpenConns` 默认 0（无限）、HikariCP 的池等待默认 30 s（约等于无限）。

### 解剖卡八问（连接池与客户端版）

| # | 问题 | 本篇的回答（一句话） |
| --- | --- | --- |
| ① | 承诺什么 | 承诺「复用建连 + 限制并发」；**不承诺**「池里一定有可用连接」「拿到的连接一定活着」「会话状态一定干净」 |
| ② | 数据长什么样 | 客户端：池槽位（fd + 缓冲 + 会话影子状态）；服务端：一个线程或一个后端进程 + 会话结构 + 按需分配的排序/连接/读缓冲 |
| ③ | 一次取用走哪条路 | 请求 → 池队列 → 取槽位 → （可选）校验 → 发语句 → 等结果 → 归还；每一跳都可能超时 |
| ④ | 并发与一致 | 池是信号量；会话状态是真实的并发风险（事务未提交就归还、`SET` 残留、临时表残留） |
| ⑤ | 扩展维度 | **四选一：加实例（连接数按实例相乘）/ 加代理（连接数按代理收敛）/ 缩短持有时间 / 批处理摊薄**；池本身不能"横向扩" |
| ⑥ | 系统级失效 | 满（连接耗尽/`max_connections` 打满）、慢（排队与持有时间互相放大）、假活（连接在但事务卡住/被对端剪断）、语义漂移（重试导致重复执行） |
| ⑦ | 看哪几个指标 | 活跃/空闲/等待连接数、取连接耗时、超时计数、库侧 `Threads_running` 与连接内存（44.11） |
| ⑧ | 换掉它的信号 | 实例数 × 池大小接近 `max_connections`、取连接 P99 进入百毫秒、客户端等待成为 P99 主项 → 上代理或改连接模型（44.9） |

---

## 44.3 物理形态：一条连接从客户端 socket 到服务端线程

### 两端各占什么

MySQL 的默认模型是 **one-thread-per-connection**：一条连接在服务端对应**一个操作系统线程 + 一份会话结构**。PostgreSQL 是 **one-process-per-connection**：一条连接对应**一个后端进程**。这两句话推出了后面全部的内存账与上限账。

```text
一条 MySQL 连接在两端各占什么（one-thread-per-connection）

 客户端进程（池里的一个槽位）              │      MySQL 服务端（一个线程 + 会话）
 ┌────────────────────────────────┐        │   ┌──────────────────────────────────┐
 │ socket fd                      │◄─TCP──►│   │ 线程（thread_stack，默认 1 MB 量级）│
 │ 发送/接收缓冲（几 KB ~ 几十 KB）│        │   │ net buffer：16 KB 起，按需长到      │
 │ 会话影子状态（事务? 隔离级别?）  │        │   │   max_allowed_packet 上限           │
 │ prepared stmt 句柄（若有）       │        │   │ 会话状态：当前事务、用户变量、      │
 └────────────────────────────────┘        │   │   临时表、prepared stmt、当前锁、   │
                                           │   │   最后一个 Read View、undo 段引用   │
                                           │   │ 按需缓冲：sort/join/read（各        │
                                           │   │   128~256 KB 量级，用到才分配）     │
                                           │   └──────────────────────────────────┘
                                           │            ↑
                                    全局共享（不按连接算）：
                                    buffer pool、redo log、lock 系统、数据字典
```

三种服务端形态的差别是「每条连接有多重」：

| 形态 | 一条连接在服务端是什么 | 每连接内存量级 | 故上限主要由什么决定 |
| --- | --- | --- | --- |
| **MySQL 8.x**（thread-per-connection） | 一个 OS 线程 + THD 会话 | 稳态 0.2–1 MB；按需缓冲全开时上界 ~1.5–2 MB（不含临时表与 `performance_schema`） | `max_connections` × 每连接内存；线程栈与上下文切换 |
| **MySQL 8.x + 线程池插件**（`thread_pool`，仅企业版/Percona 等发行版提供） | 线程数固定为 `thread_pool_size`，连接排队复用线程 | 会话内存仍在，线程栈不再线性增长 | 队列等待而非线程数 |
| **PostgreSQL 13–17**（process-per-connection） | 一个后端进程 | 私有 RSS 5–10 MB 量级 + `work_mem`（默认 4 MB）**按每个排序/哈希节点每连接各算一份** | 进程数与 `work_mem × 并发节点数` 的内存 |

> **版本与开关前提**：`thread_stack` 默认值随版本与平台位宽变化（5.7 与 8.0 不同），**以目标版本文档为准**；`work_mem` 是「每个节点」而非「每条查询」，一条有 3 个排序节点的 PG 查询在一条连接上就吃 3 份。这条差别是「为什么 MySQL 的 200 连接和 PG 的 200 连接不是同一件事」的全部原因。

### 会话状态：池里那个槽位其实是个变量

| 会话状态 | 谁设置的 | 归还后是否残留 | 后果 |
| --- | --- | --- | --- |
| 当前事务 + 锁 | `BEGIN` / 自动提交关 | **会**（很多池不会替你回滚） | 下一个使用者拿到"未结束的事务"，行锁长期不释放（事故一的长事务就是这么被放大的） |
| 隔离级别 / 只读模式 | `SET SESSION TRANSACTION ...` | 会 | 业务 A 的 RC 泄漏给业务 B，出现"同一份代码两种可见性" |
| 用户变量 `@x`、`SET SESSION` | 任意业务 SQL | 会 | 隐式耦合；`sql_mode`/`time_zone` 被改掉后下一个使用者算错时间 |
| 临时表 | `CREATE TEMPORARY TABLE` | 会（直到连接关闭） | 表名冲突、`tmp_table` 内存不释放 |
| prepared statement 句柄 | `Prepare` | 会 | 句柄泄漏；在代理的 transaction 模式下直接失效（44.7） |
| 最后一个 Read View（长快照） | 长事务或 `--single-transaction` 备份 | 直到事务结束 | 卡住 purge，undo 涨（[06 MVCC 与隔离级别](06-mvcc-and-isolation-levels.md) §06.7） |

**纪律**：池必须在「归还」时做**会话复位**——至少是回滚未提交事务 + 恢复关键会话变量；代理的 transaction 模式之所以省连接，靠的正是「事务边界就是归还点」。

### 池的三层：连接数是被乘出来的

**连接总数的算法**：`连接总数 = Σ(Pod 数 × 每 Pod 池大小) + 旁路客户端（批处理 / 运维 / CDC / 监控）`。

| 层 | 谁在限制 | 典型量级 | 打满时的现象 |
| --- | --- | --- | --- |
| **应用内池**（HikariCP / database/sql / Druid） | 每进程 `maximumPoolSize` | 10–50/进程 | 应用侧「取连接超时」，库侧连接数不变 |
| **代理池**（PgBouncer / ProxySQL） | `default_pool_size`、`max_client_conn` | 20–100/后端库 | 代理侧 `cl_waiting` 上涨，后端连接数被钳住 |
| **数据库自身** | `max_connections`（MySQL 默认 151 量级）、PG `max_connections`（默认 100 量级） | 数百~数千 | `Too many connections` / `FATAL: sorry, too many clients already` |

算一遍最常见的失控点：**120 个 Pod × 池 20 = 2,400 条**，而目标库 `max_connections` 只有 1,000。此时"池够不够"这个问题毫无意义——**连接数是被 Pod 数乘出来的**，扩容一次应用就等于扩一次数据库连接数。这也是 44.9 里「什么时候该加代理」的唯一判据。

---

## 44.4 池大小的算术：为什么 20 条能扛 1,000 QPS

### 一个公式：并发 = QPS × 持有时间

Little 定律：稳态下，系统内的平均请求数 = 到达率 × 平均停留时间。

**三个式子，全篇反复用**：`需求并发 C = QPS × 持有时间(秒)`；`池利用率 ρ = C ÷ 池大小`；`吞吐天花板 = 池大小 ÷ 持有时间`（持有时间固定时）。

**算例（本篇的核心算例）**：假设 ① 单语句平均持有连接 10 ms（含网络往返与结果集读取，同 AZ）；② 到达 1,000 QPS；③ 池 20 条；④ 持有时间不随并发上升。

代入：`C = 1,000 × 0.010 s = 10`；`ρ = 10 ÷ 20 = 50%`；`吞吐天花板 = 20 ÷ 0.010 s = 2,000 QPS`。

50% 利用率意味着排队几乎为零——这就是「20 条扛 1,000 QPS」的全部内容。它不神秘，也不需要调参。

| 持有时间 | 需求并发（1,000 QPS） | 池 20 条的利用率 | 池 20 条的天花板 QPS |
| --- | --- | --- | --- |
| 2 ms | 2 | 10% | 10,000 |
| 5 ms | 5 | 25% | 4,000 |
| **10 ms** | **10** | **50%** | **2,000** |
| 30 ms | 30 | 150%（不够） | 667（< 到达量） |
| 100 ms | 100 | 500%（远远不够） | 200 |

> **量级结论（非承诺值）**。假设清单：① 持有时间包含「借出到归还」的全部时间（含业务处理与结果集遍历），这一点最常被算错；② 请求在池外不排队（应用有足够线程）；③ 到达量稳定。**换假设可复算：持有时间从 10 ms 变成 30 ms，需求并发就翻 3 倍，此时正确动作是查那 30 ms 花在哪（[05 InnoDB 的页与行](05-innodb-page-and-row.md) §05.4 的读路径分解），而不是把池加大到 60。**

### 两个上限：数据库线程数与内存

池大小的天花板不是「你觉得够用」，而是两个物理量：

| 上限 | 判据 | 量与假设 | 越过之后会怎样 |
| --- | --- | --- | --- |
| **上限一：数据库并行度** | 同时活跃会话数 ≈ CPU 核数 × 1~2（OLTP 短语句）；有大量 IO 等待时可按 `核数 × (1 + IO 等待/CPU 时间)` 估 | 16 核 ⇒ 16~40 个「真正在推进」的会话 | 多出来的会话在 run queue/锁队列排队，**所有会话的持有时长一起变长**（事故一） |
| **上限二：连接内存** | `连接数 × 每连接内存 ≤ 数据库可用内存的一小部分` | 稳态 0.4 MB/连接 ⇒ 500 条 = 200 MB；按需缓冲全开上界 1.7 MB ⇒ 500 条 = 850 MB | 内存吃紧 → 换页 / tmp 落盘 / OOM；PG 侧还会被 `work_mem` 放大 |

**池大小的正解区间**：`下限 = ceil(需求并发 ÷ 目标利用率) = ceil(QPS × 持有时间 ÷ 0.7)`；`上限 = min(数据库并行度上限 ÷ Pod 数, 内存预算 ÷ 每连接内存 ÷ Pod 数)`。

**算例**：3,000 QPS、持有 5 ms、16 核库（并行度上限取 32）、8 个 Pod、内存预算 1 GB、每连接稳态 0.4 MB。注意 **QPS 也要按 Pod 摊**，否则会把总数与单机数算混：

`每 Pod 需求并发 = (3,000 ÷ 8) × 0.005 s = 1.9` → `每 Pod 池大小 = ceil(1.9 ÷ 0.7) = 3` → `总连接 = 8 × 3 = 24`。

再用两个上限验一遍：① 并行度 `24 < 32` ✅；② 内存 `24 × 0.4 MB ≈ 10 MB ≪ 1 GB` ✅。对照「每 Pod 池 20」：`总连接 = 160`，是需求并发 15 的 10.7 倍，同时把 32 的并行度上限撑破 5 倍——**这就是事故一的算式形态：池不是不够，是过头了**。

> **量级结论（非承诺值）**。假设：① QPS 在 Pod 间均匀（LB 不均会打破它，这就是为什么 `least_conn` 比轮询好）；② 持有时间同前；③ 不考虑突发。**这条算例的真正用途是解释「为什么云厂商默认给 10 条」以及「为什么 20 条已经很有富余」——A 组 3 条偏紧（没有突发余量），20 条是「约 10 倍余量」的保守取值，不是「刚好够」。**

### 为什么加池会让事情变坏：持有时间是并发的函数

| 同时活跃会话（16 核库） | 单语句耗时（量级） | 需求并发（1,000 QPS） | 现象 |
| --- | --- | --- | --- |
| 8–16 | 4~5 ms | 4~5 | 顺畅 |
| 40 | 8~12 ms | 8~12 | 排队开始出现，P99 抬 2 倍 |
| 100 | 25~35 ms | 25~35 | 锁等待与页闩冲突成为主项，P99 抬 6 倍 |
| 200 | 40~80 ms | 40~80 | 上下文切换 + 内部串行点，**吞吐反而下降** |

**表里最该看的是最后一列与第二列的乘积**：池越大 → 活跃会话越多 → 单语句越慢 → 需求并发越大 → 池显得更不够。这是一个正反馈环，**唯一的出口是减少并发或缩短持有时间**，不是继续加池。

### 池大小决策表

| 场景 | 建议池大小（每 Pod） | 理由 |
| --- | --- | --- |
| 常规 OLTP Web 服务 | 10–20 | 利用率 50%~70%，留有突发余量 |
| 批处理/定时任务消费者 | 4–8（**独立池**） | 长持有时间会污染在线请求的池（44.9 池隔离） |
| 存在 100 ms+ 慢查询 | 先修 SQL，**池保持 10–20** | 加池只会把慢查询并发化，锁等待暴涨 |
| 有大量 IO 等待（如大结果集导出） | 独立池 + 少量连接（2–4） | 连接被长时占用，池大小要按「导出并发」而非 QPS 定 |
| Pod 数很多（>50）且库 `max_connections` 有限 | **加代理**，应用池 10、代理池 20–40/库 | `Pod 数 × 池` 已越过库上限（44.9 判据） |
| 高峰期 P99 抖动，池等待 < 5 ms | 池不是瓶颈，别动 | 池等待小说明连接不缺（44.11 的第一个指标） |

---

## 44.5 超时链：五层时间预算，缺一层就等于无限等

### 一张图看清谁在计时

```text
一次「读一条订单」的时间预算（从入口倒推，单位 ms）

 入口网关 800 ─────────────────────────────────────────────────────────┐
   └─ 服务 A 收到，剩余 780                                              │
      ├─ 取连接（池等待）      ≤ 200   ← 拿不到就快速失败，别等 30 s       │
      ├─ TCP 建连（如需）      ≤ 100   ← 只在池空/刚发布时发生             │
      ├─ 发送 + 执行(语句超时)  ≤ 350   ← 数据库侧上限：max_execution_time  │
      ├─ 取结果（socket 读）    ≤ 200   ← socketTimeout，缺省 0 = 无限      │
      └─ 提交（若写）           ≤ 100                                      │
   └─ 网关若 800 ms 未收到 ⇒ 网关超时、可能重试 ⇒ 上面的时间仍在跑 ◄────────┘
                                    ▲
                            这就是「超时叠加」：外层放弃了，内层还在烧资源
```

### 五层超时与它们的缺省值

| 层 | 参数（按栈） | 典型取值 | 缺省值陷阱 | 缺省时最坏等待 |
| --- | --- | --- | --- | --- |
| **建连** | JDBC `connectTimeout`；Go `DialContext` 的 ctx；PG `connect_timeout` | 200~1,000 ms | JDBC `connectTimeout=0` ⇒ 由 OS 决定（TCP SYN 重传 127 s 量级） | 分钟级 |
| **池等待** | HikariCP `connectionTimeout`；Go 用 ctx 控制 | 100~500 ms | **HikariCP 默认 30,000 ms** | 30 s × 线程数 |
| **语句** | MySQL `max_execution_time`（**只作用于 SELECT**）；PG `statement_timeout`（默认 0 = 关）；JDBC `Statement.setQueryTimeout` | 300~1,000 ms | 默认 0/未设 ⇒ 无限；MySQL 的 DML 没有等价参数 | 无限 |
| **socket 读** | JDBC `socketTimeout`；驱动 read deadline | 500~2,000 ms | 默认 0 = 无限 | 无限（连接假活时） |
| **事务** | PG `idle_in_transaction_session_timeout`（默认 0）；`@Transactional(timeout=)` **只在语句之间检查** | 1~5 s | 默认 0 ⇒ 悬挂事务永久持锁 | 无限 |
| **服务端会话** | MySQL `wait_timeout`（默认 28,800 s = 8 h）、`interactive_timeout`；云 LB idle 60~350 s | 与 LB 一起排序 | 远大于 LB ⇒ 池里全是死连接 | — |

**四条纪律**：

1. **必须从入口倒推，不能各层自定**：入口给 800 ms，则池等待 ≤ 200 ms、语句 ≤ 350 ms、读 ≤ 200 ms，任何一层超了就是抢别人的预算。
2. **最坏值 = 各层之和 × 重试次数**：`T_worst = (T_connect + T_poolwait + T_stmt + T_fetch) × attempts`。上表缺省全踩的组合是 `127 s + 30 s + ∞ + ∞`——它不是"慢"，它是"永不返回"。
3. **内层必须 ≤ 外层**：数据库语句超时必须小于应用请求超时，否则外层已经放弃、数据库还在跑（并占着连接与锁）。MySQL 的 DML 没有语句超时，所以写路径的超时必须由**事务/连接层**兜（`innodb_lock_wait_timeout` 默认 50 s 只兜锁等待，不兜执行）。
4. **超时必须能真的中断执行**：设置语句超时后，连接上的语句会被服务端杀掉（连接可用）；取消一个正在跑的查询但不断开连接，是"客户端放弃、服务端继续"的经典反模式——它把超时变成了纯放大器。

### 倒推预算表（可直接抄进设计文档）

| 组件 | 预算（ms） | 配置落点 | 备注 |
| --- | --- | --- | --- |
| 入口/网关 read timeout | 800 | Nginx `proxy_read_timeout 1s`、Envoy `timeout: 0.8s` | 与 SLO 对齐，不与后端"最长可能"对齐 |
| 服务请求超时 | 750 | 框架级（context deadline / WebMVC async timeout） | 比入口小 50 ms 留回程 |
| 池等待 | 150 | `connectionTimeout=150`（Hikari 最小值 250 ms 量级，按版本文档核对） | 拿不到就失败：这本身是信号 |
| 语句执行 | 350 | `statement_timeout` / `max_execution_time` / `setQueryTimeout` | SELECT 用参数，DML 用事务超时兜 |
| socket 读 | 300 | `socketTimeout` / read deadline | 必须小于服务请求超时 |
| 事务（写） | 400 | `idle_in_transaction_session_timeout`、`@Transactional(timeout=)` | 事务超时只在语句间检查，不能当硬中断 |
| 重试 | ≤1 次 + 预算 10% | 客户端重试策略 | 重试进入同一预算，不另开一份 |

---

## 44.6 客户端故障：六种「数据库看不见」的失效

数据库只会看到连接数抖一下，剩下的全在客户端。这六种失效的机制各不相同，但都表现为同一句话：**"数据库没问题，但请求全挂了。"**

| 故障 | 机制 | 现场特征 | 修法 |
| --- | --- | --- | --- |
| **DNS 缓存** | JVM `networkaddress.cache.ttl` 默认 30 s 量级（有 SecurityManager 时可能为 -1 = 永久）；设为 0 则每次建连都解析；负缓存 `networkaddress.cache.negative.ttl` 默认 10 s 量级 | 切主后仍连旧 IP（永久缓存），或建连风暴打挂 DNS（TTL=0） | 固定 10–30 s；主从切换用 VIP/代理而不是靠 DNS 生效 |
| **GC 停顿** | STW 期间所有线程停住；恢复瞬间既是"请求齐发"也是"连接齐断"（心跳/租约超时） | 分位曲线出现与 GC 日志对齐的尖刺；`Aborted_connects` 上涨 | 控制堆与晋升失败；池加平滑（预热 + 取连接快速失败）；GC 日志与业务 P99 同屏 |
| **连接泄漏** | 借出不还（异常路径漏 `close`）、或长事务占住不放 | 活跃连接数单调上升、空闲数降到 0、池等待上涨 | `leakDetectionThreshold`（仅测试/灰度开，有开销）；代码层用 try-with-resources |
| **idle 被服务端/LB 剪断** | 池的 `idleTimeout`/`maxLifetime` **大于** LB idle（60~350 s 量级）或防火墙的会话老化 | 每隔一个固定周期出现一小批"首请求失败" | 排序：`maxLifetime` < LB idle < 服务端 `wait_timeout`；开 `keepaliveTime` 或取时校验 |
| **主从切换** | 旧主连接全失效；新主尚未升完；客户端地址列表未刷新 | 一批 `Communications link failure`，随后又"自己好了" | 客户端连接重建 + 幂等重试；用代理收敛切换（44.9） |
| **重试放大** | 超时+重试把一次故障变成 N 份负载（[40 RPC 与连接复用](40-rpc-and-connection-reuse.md) §40.6） | 到达量瞬间 ×2~4，池等待暴涨，库侧 `Threads_running` 尖刺 | 退避 + 抖动 + 预算 + 只对幂等操作重试 |

```text
GC 停顿如何变成「重连风暴 + DNS 风暴」（事故三的时序）

  t0  ├──────── STW 2.7 s（所有线程停）────────┤
  t1                                           ├─ 3,000 个请求同时苏醒
                                               ├─ 90% 直接超时 → 重试 ×4
                                               ├─ 池里 30% 是死连接 → 先失败再重建
                                               └─ 每次重建一次 DNS → 解析 QPS ×45
                                                       ▲
                                    单看任何一项都不致命，叠加就是雪崩
```

**一条纪律**：客户端要能被"缓慢地重启"。凡是会把「重启/停顿」翻译成「同时重建全部连接」的配置（无抖动的重试、启动即满池、`ttl=0` 的 DNS、无 jitter 的 `maxLifetime`），都必须加抖动或分批——否则你的发布与一次 GC 等价。

---

## 44.7 池配置：七个旋钮、两个动作、一种代理模式

### 七个旋钮各自改变哪条物理路径

| 旋钮 | 它改变什么 | 建议 | 配错的现场 |
| --- | --- | --- | --- |
| `maximumPoolSize` | 瞬时并发上限 | 按 44.4 算，一般 10–20 | 太大：库内排队（事故一）；太小：池等待成为 P99 |
| `minimumIdle` | 稳态保有量 | 设为等于 `maximumPoolSize`（固定池） | 小于 max：突发时边用边建连，出现建连延迟尖刺 |
| `connectionTimeout`（池等待） | 排队上限 | 100–500 ms（Hikari 最小值约 250 ms，按版本文档） | 默认 30 s ⇒ 事故二 |
| `idleTimeout` | 空闲回收 | 5–10 min，且**小于** LB/服务端 idle | 大于 ⇒ 拿到死连接 |
| `maxLifetime` | 连接最长寿命 | 20–30 min，**大于**一次发布窗口、**小于**服务端 `wait_timeout` | 不设 ⇒ 切主后一直连旧节点 |
| 校验（`connectionTestQuery` / `isValid` / `keepaliveTime`） | 取用时/空闲时探测 | 能用 `isValid` 就别用 `SELECT 1`；`keepaliveTime` 30–120 s | 每次取用都 `SELECT 1` ⇒ 每请求多一次 RTT |
| `leakDetectionThreshold` | 借出超时告警 | 只在测试/灰度开（如 10 s） | 生产常开会带来额外开销与误报 |

```yaml
# HikariCP 5.x 骨架（参数语义以目标版本文档为准）
spring:
  datasource:
    hikari:
      maximum-pool-size: 20          # 按 QPS × 持有时间 ÷ 0.7 算，不按"感觉"
      minimum-idle: 20               # 固定池：避免突发时边用边建连
      connection-timeout: 300        # 池等待上限（ms），必须 << 入口预算
      validation-timeout: 1000       # 校验上限，须小于 connection-timeout
      idle-timeout: 300000           # 5 min，必须小于 LB / 服务端 idle
      max-lifetime: 1500000          # 25 min，大于发布窗口，小于 wait_timeout
      keepalive-time: 60000          # 空闲连接保活，抵消 LB 剪断
      leak-detection-threshold: 0    # 生产关；灰度开 10000 观察
      pool-name: order-db
```

### 两个必需动作：预热与缩容

| 动作 | 为什么必须做 | 做法 | 不做会怎样 |
| --- | --- | --- | --- |
| **预热**（warm-up） | 池是懒加载的：发布后第一个高峰要同时建 N 条连接，建连成本（TCP+TLS+认证，同 AZ 1–5 ms，跨 AZ 10–30 ms 量级）全落在最热的几十毫秒里 | 启动时**串行或小并发**建满 `minimumIdle`，或按比例分批（每批 20%，间隔 100 ms） | 发布后 1–3 s 的 P99 尖刺；滚动发布时被 LB 复用到"半冷"实例 |
| **缩容**（scale-down） | 缩容会**立刻**关掉一批连接，而这些连接上可能有在途事务 | 只缩 idle 部分；`maxLifetime` 让连接自然轮换；必要时先摘流量再缩 | 缩容瞬间出现一批 `Connection closed` 与回滚 |

### 代理：session / transaction / statement 三档

代理的存在意义只有一个：**把「应用连接数」与「数据库连接数」解耦**，让 `Pod 数 × 池大小` 不再直接等于库上的会话数。

| 模式 | 一条后端连接在什么时候还回去 | 连接收敛比 | 破坏的会话特性 |
| --- | --- | --- | --- |
| **session**（PgBouncer 默认） | 客户端断开时 | **≈1**（几乎没有收敛） | 无（完全透明），但省不了连接 |
| **transaction** | 事务提交/回滚时 | **高**（数十倍） | 会话级状态：`SET`、临时表、`LISTEN/NOTIFY`、advisory lock、游标；服务端 prepared statement 需代理侧支持（PgBouncer 1.21+ 的 `max_prepared_statements`，以目标版本文档为准） |
| **statement** | 每条语句结束 | 最高 | 上述全部 + 跨语句事务**根本不成立**（不能用于有事务的业务） |

**判据**：能不能用 transaction 模式，取决于你的应用是否依赖会话状态。检查清单：有没有 `SET SESSION`（尤其 `time_zone`、`sql_mode`）、有没有临时表、有没有 advisory lock / `LISTEN`、ORM 是否用服务端 prepared statement。任一项命中，就必须要么改应用（连接初始化不放在会话里），要么退回 session 模式（此时代理只做路由与限流，不做连接收敛）。

### 三个客户端骨架（可直接抄的形态）

```java
// Java：池 + 语句超时 + 事务边界（Spring Boot + HikariCP 5.x 骨架）
@Configuration
public class DbConfig {
    @Bean
    public HikariDataSource dataSource() {
        HikariConfig c = new HikariConfig();
        c.setJdbcUrl("jdbc:mysql://order-db:3306/order?useSSL=true");
        c.setMaximumPoolSize(20);
        c.setMinimumIdle(20);
        c.setConnectionTimeout(300);        // 池等待 300 ms：拿不到就失败，别等 30 s
        c.setIdleTimeout(300_000);
        c.setMaxLifetime(1_500_000);
        c.setKeepaliveTime(60_000);
        c.addDataSourceProperty("socketTimeout", "300");       // 读结果上限
        c.addDataSourceProperty("connectTimeout", "1000");     // 建连上限
        return new HikariDataSource(c);
    }

    // 语句级超时：MySQL 侧 max_execution_time 只作用于 SELECT，写路径靠事务超时兜
    @Bean
    public JdbcTemplate jdbcTemplate(HikariDataSource ds) {
        JdbcTemplate t = new JdbcTemplate(ds);
        t.setQueryTimeout(1);               // 1 s：必须小于服务请求超时
        return t;
    }
}
```

```go
// Go：database/sql 的四个 Set*，以及"池等待"必须由 ctx 提供
db, err := sql.Open("mysql", dsn)          // Open 不建连，只建池
if err != nil { return err }

db.SetMaxOpenConns(20)                     // 0 = 无上限（默认值，最常见的炸点）
db.SetMaxIdleConns(20)                     // 与 MaxOpen 相等，避免反复建连
db.SetConnMaxLifetime(25 * time.Minute)    // 小于 DB / LB 的 idle kill 窗口
db.SetConnMaxIdleTime(5 * time.Minute)     // Go 1.15+；空闲回收

// 取连接 + 语句 + 结果集读，全部受同一个 ctx 约束（这是 database/sql 唯一的"池等待"）
ctx, cancel := context.WithTimeout(ctx, 350*time.Millisecond)
defer cancel()
row := db.QueryRowContext(ctx, `SELECT id, amount FROM orders WHERE id = ?`, id)
if err := row.Scan(&oid, &amt); err != nil {
    if errors.Is(err, context.DeadlineExceeded) {
        // 池等待耗尽 或 语句超时 或 读结果超时：三者必须能区分（看 ctx 与驱动错误码）
        metrics.Inc("db_timeout", "stage=unknown")
    }
    return err
}
```

两段代码共同的坑：**`connectionTimeout` / ctx 这一层是唯一能挡住事故二的开关**；`socketTimeout` / ctx 是唯一能挡住"假活连接"的开关；而 `maxLifetime` 与 LB idle 的排序是唯一能挡住"周期性首请求失败"的开关。

---

## 44.8 三本账：连接内存、排队时间、超时叠加

三本账对应三个「上了规模才发现要付的钱」：**每条连接的内存**（服务端）、**每次取连接的排队**（客户端）、**每次故障的最坏等待**（两端的乘积）。

### 账一 · 连接内存 = 连接数 × 每连接服务端内存

**乘法链**：`连接内存 = 连接数 × 每连接内存`，其中 `每连接内存 = 固定部分（线程栈/进程基线）+ 按需部分（sort/join/read 缓冲、net buffer、临时表）`；MySQL 的上界要按"所有按需缓冲同时分配"算，PG 要额外乘 `work_mem × 每查询排序节点数`。

**算例**：假设 ① MySQL 8.4，`thread_stack` 1 MB 量级、`read_buffer_size` 128 KB、`read_rnd_buffer_size` 256 KB、`sort_buffer_size` 256 KB、`join_buffer_size` 256 KB、`binlog_cache_size` 32 KB、`net_buffer_length` 16 KB（按需增长）；② 稳态只分配线程栈 + 少量缓冲，取 0.4 MB/连接；③ 上界取 1.7 MB/连接；④ 不计临时表与 `performance_schema`。

| 连接数 | 稳态内存（0.4 MB/条） | 上界内存（1.7 MB/条） | 结论 |
| --- | --- | --- | --- |
| 20 | 8 MB | 34 MB | 无感 |
| 200 | 80 MB | 340 MB | 可接受，但线程数已是 12.5 倍核数 |
| 1,600 | 640 MB | 2.7 GB | 内存开始与 buffer pool 抢，且线程切换成本显著 |
| 10,000 | 4 GB | 17 GB | 必须先上代理收敛 |

PostgreSQL 侧的形状不同：每条连接是**一个进程**（私有 RSS 5–10 MB 量级），且 `work_mem` 是**每个排序/哈希节点每连接**各一份。一条有 3 个排序节点的查询、200 条连接、`work_mem=4 MB` ⇒ `200 × 3 × 4 MB = 2.4 GB` 的潜在分配（实际只在节点存活期间分配）。

> **量级结论（非承诺值）**。假设清单：① 各 buffer 默认值随版本与平台变化，**以目标版本文档为准**；② 稳态值来自「短语句 + 小结果集」的典型 OLTP，大结果集会把 `net_buffer` 与临时表拉高；③ 未计 `performance_schema`（打开后每线程另有开销）。**换假设可复算：把每连接稳态从 0.4 MB 改成 1 MB，1,600 条连接就是 1.6 GB——这就是为什么「连接数」必须进容量模型，而不是只进告警。**

### 账二 · 排队时间 = 池等待，随利用率非线性上涨

**乘法链**：`需求并发 C = QPS × 持有时间` → `利用率 ρ = C ÷ 池大小` → `平均排队时间 W_q ≈ 持有时间 × f(ρ, 池大小)`，其中 `f` 在 ρ→1 时以 `1/(1−ρ)` 量级发散（M/M/c 近似的形状；真实服务时间方差更小时排队更短）。

**算例**：假设 ① 池 20 条；② QPS 与持有时间使 ρ 逐步上升；③ 持有时间 10 ms；④ 服务时间近似指数分布（保守，真实 OLTP 方差通常更小）；⑤ 无优先级、无公平性。

| 需求并发 C | 利用率 ρ | 平均排队（×持有时间） | 平均池等待（持有 10 ms 时） | 现场观感 |
| --- | --- | --- | --- | --- |
| 10 | 50% | ≈0.02 | ≈0.2 ms | 顺畅 |
| 14 | 70% | ≈0.3 | ≈3 ms | 偶发尖刺 |
| 16 | 80% | ≈1.1 | ≈11 ms | 池等待开始进 P99 |
| 18 | 90% | ≈4.8 | ≈48 ms | P99 翻倍，值班开始调池 |
| 19 | 95% | ≈14 | ≈140 ms | 抖动明显，容易误判为「网络问题」 |
| 19.8 | 99% | ≈89 | ≈890 ms | 接近不可用，任何小抖动都会崩 |

> **量级结论（非承诺值）**。假设清单：① M/M/c 假设（泊松到达、指数服务时间）；② 池无公平性 ⇒ 实际尾延迟比平均值更差（可能存在饥饿）；③ 持有时间不随并发变化（现实里会变，见 44.4 的表，所以真实曲线更陡）。**这张表的核心结论：ρ 从 80% 到 95%，排队时间涨约 13 倍——所以「池利用率 90%」不是健康水位，是危险水位；容量规划的工作点是 50%~70%。**

### 账三 · 超时叠加 = 各层超时之和 × 重试次数

**乘法链**：`T_worst = (T_connect + T_poolwait + T_stmt + T_fetch + T_commit) × attempts`；而**有效服务能力 = 线程数 ÷ T_worst**。

**算例**：假设 ① 200 个工作线程（同步模型）；② 各层取上表的缺省值；③ 重试 1 次（共 2 次尝试）；④ 到达量在故障期不变。

| 配置组合 | T_worst | 有效服务能力（200 线程） | 现场 |
| --- | --- | --- | --- |
| 全缺省（connect 127 s / pool 30 s / stmt ∞ / read ∞） | **∞（永不返回）** | 0 | 全面挂死，只能重启 |
| pool 30 s + stmt 未设 + read 未设 | 60 s（含 1 次重试） | 3.3 req/s | 事故二 |
| pool 300 ms + stmt 1 s + read 300 ms | 3.2 s | 62 req/s | 仍偏大：应把 stmt 降到 350 ms |
| pool 150 ms + stmt 350 ms + read 300 ms | 1.6 s | 125 req/s | 可接受 |
| 同上 + 重试 0 次 | 800 ms | 250 req/s | 与入口预算 800 ms 对齐 |

> **量级结论（非承诺值）**。假设清单：① 线程被同步阻塞（异步/响应式下这条账换成"事件循环里挂起的请求数"）；② 各层超时是"最坏"而非"P99"；③ 没有熔断提前切断。**结论的形状：`T_worst` 每翻一倍，同一台机器的服务能力就减半——所以「把超时调大以少报错」永远是把容量减半的做法。**

```python
# 三本账复算（Python 3.9+）：改任一假设直接重跑
def little(qps, hold_ms):
    return qps * hold_ms / 1000.0                      # 需求并发 C

def pool_wait(hold_ms, conc, pool=20):                 # M/M/c（Erlang C）近似
    a = conc
    if a >= pool:
        return float("inf")
    b = 1.0                                            # Erlang B 递推
    for k in range(1, pool + 1):
        b = a * b / (k + a * b)
    c = b / (1 - (a / pool) * (1 - b))                 # Erlang C
    return hold_ms * c * a / (pool - a)                # 平均排队，单位 ms

def conn_mem(conns, steady_kb=400, worst_kb=1700):
    return conns * steady_kb / 1e6, conns * worst_kb / 1e6   # GB

for hold in (5, 10, 30, 100):
    c = little(1000, hold)
    print(f"hold={hold:>4}ms → C={c:>6.1f} → 池20排队≈{pool_wait(hold, c):8.1f} ms")

for n in (20, 200, 1600, 10000):
    s, w = conn_mem(n)
    print(f"{n:>6} 连接 → 稳态 {s:6.2f} GB / 上界 {w:6.2f} GB")
```

---

## 44.9 边界：池不是万能的，代理也不是

### 池能做什么、不能做什么

| 你想要的保证 | 连接池给不给 | 物理原因 | 正确落点 |
| --- | --- | --- | --- |
| 减少建连开销 | ✅ | 复用 TCP/TLS/认证 | — |
| 限制数据库并发 | ✅ | 池是信号量 | — |
| 让慢查询变快 | ❌ | 池不执行 SQL | 索引与执行计划（[05](05-innodb-page-and-row.md) §05.4、[06](06-mvcc-and-isolation-levels.md) §06.4） |
| 让大事务不锁表 | ❌ | 锁由事务持有时长决定 | 缩短事务（[06](06-mvcc-and-isolation-levels.md) §06.7） |
| 故障时"自动扩容" | ❌ | 池上限是配置，不是弹性 | 容量规划 + 限流（→ eng/15） |
| 隔离慢/快业务 | ⚠️ 需**分池** | 同池共享排队 | 每类负载独立池（导出/批处理各一个） |
| 跨实例/跨分片的连接管理 | ❌ | 池不感知路由 | 中间件/代理（分片路由在应用或代理） |
| 让客户端"无感"切主 | ⚠️ 部分 | 池只负责重建，地址从哪来不归它 | VIP / 代理 / 注册中心 |

**分池是最便宜、最常被忽略的一招**：把导出/批处理/报表放到独立池（3~5 条连接），在线业务池 20 条不变。代价是总连接数 +5，收益是长持有时间的负载不再污染在线 P99。

### 什么时候加代理、什么时候改 SQL：决策表

| 现象 | 先查什么 | 决定 | 理由 |
| --- | --- | --- | --- |
| `Pod 数 × 池大小` 接近或超过 `max_connections` | 库里 `Max_used_connections` 与 `Threads_connected` | **加代理**（transaction 模式） | 这是唯一真正需要代理的判据：连接数被乘法撑爆 |
| 池等待 P99 > 50 ms，而库侧 `Threads_running` 远小于核数 | 池大小 vs 需求并发 | **加池**（小幅）或查应用线程是否被别处堵住 | 连接真的不够且数据库还有余量 |
| 池等待大，同时 `Threads_running` ≈ 核数 × 2 以上 | 慢查询、锁等待、长事务 | **改 SQL / 加索引 / 拆事务**，池不动 | 加池只会把库内排队做大（事故一） |
| 短连接抖动（连接数周期性下跌） | LB idle vs `maxLifetime` vs `wait_timeout` | **改配置顺序** | 三个时间常数排序错，与容量无关 |
| 主从切换痛、故障后要人工改 DSN | DNS TTL、VIP、驱动 | **加代理或 VIP** | 客户端不该知道拓扑 |
| 多租户/分库分表，连接数按租户膨胀 | 分片数 × 池 | **代理 + 分片路由** | 应用侧池数会变成 `分片数 × 池` |

**一条判据压全部**：**先看持有时间，再看池大小。** 持有时间不降，一切加池的动作都只是把排队从客户端搬到数据库里，并且顺手放大锁竞争。

---

## 44.10 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到物理形态） | 定位 | 修复 | 防复发 |
| --- | --- | --- | --- | --- |
| 取连接超时，但库上连接数远没到上限 | 池等待上限太大 + 库内持有时间长 | 池 `acquire` 耗时 + 库侧 `Threads_running` | 降池等待、修持有时间 | 池等待 P99 进 SLO 看板 |
| `Too many connections` / `too many clients already` | `Pod 数 × 池大小` 越过 `max_connections` | `Max_used_connections`、`SHOW PROCESSLIST` 按 `Host` 汇总 | 减池或加代理 | 容量评审把连接数按乘法算 |
| P99 周期性尖刺（每 5/10/30 分钟） | `maxLifetime` / `idleTimeout` 与 LB idle 同量级、无抖动 | 连接建立速率曲线与 GC/定时任务对齐 | 加 jitter、排序时间常数 | 连接建立速率纳入指标 |
| 每隔一段时间"首请求失败" | 池里有被对端剪断的死连接，校验没开 | `Aborted_clients`、驱动 `Communications link failure` | 开 `keepaliveTime` 或取时 `isValid` | `maxLifetime` < LB idle |
| 活跃连接数单调上升直到打满 | 连接泄漏（异常路径没归还）或长事务 | 池活跃数/空闲数曲线、`information_schema.innodb_trx` 长事务 | 修 close 路径、`leakDetectionThreshold` | 泄漏检测灰度 + 事务时长告警 |
| 同一份代码两种隔离级别 | 会话状态泄漏（`SET SESSION` 未复位） | 池取用/归还日志、`@@transaction_isolation` | 归还时复位 + 连接初始化 SQL | 禁止业务写 `SET SESSION` |
| 切主后应用恢复很慢 | DNS 永久缓存 或 `maxLifetime` 太长 | DNS TTL 配置、连接年龄分布 | TTL 10–30 s、`maxLifetime` < 15 min | 切主演练纳入例行 |
| 建连风暴打挂 DNS | `networkaddress.cache.ttl=0` + 大量重连 | CoreDNS QPS 与解析延迟 | 恢复 TTL 缓存 | SDK 推荐值进评审，不照抄 |
| 库 CPU 不高但所有语句变慢 | 会话数远超核数 ⇒ 内部串行点排队 | `Threads_running` vs 核数 | 减池、限流 | 池上限与核数挂钩 |
| 事务超时设了却仍长事务 | 事务超时只在语句之间检查，长单语句不受控 | 慢日志 + `innodb_trx` | 用语句超时（SELECT）+ 拆事务 | 写路径按事务时长告警 |
| 代理上线后出现"奇怪的状态丢失" | transaction 模式断开会话状态（`SET`/临时表/`LISTEN`） | 代理日志 + 业务报错集中在会话特性 | 改 session 模式或改应用 | 代理模式纳入设计评审 |
| 重启后一段时间错误率高 | 池懒加载，冷启动同时建连 | 启动到稳态的连接数曲线 | 预热 + 分批建连 | 发布检查清单 |

---

## 44.11 观测：五个数覆盖 90% 的连接层故障

| 指标 | 看什么 | 拐点阈值 | 版本 / 开关前提 |
| --- | --- | --- | --- |
| **池活跃 / 空闲 / 等待数** | 是不是"满了"（活跃=max 且等待>0） | 等待数 > 0 持续 1 分钟即关注 | Micrometer 下为 `hikaricp_connections_pending`；Spring Boot 需暴露 Actuator metrics |
| **取连接耗时（acquire）** | 排队是否进 P99 | P99 > 50 ms 告警 | `hikaricp_connections_acquire_seconds`；需开 metrics 注册 |
| **池等待超时计数** | 用户是否已经在报错 | 任意非零即告警 | `hikaricp_connections_timeout_total`；Go 侧需自己在 ctx 超时处埋点 |
| **库侧 `Threads_running` / `Threads_connected`** | 数据库真正的并行度与连接总数 | `Threads_running` > 核数 × 2 持续 1 分钟；`Threads_connected` > `max_connections` × 0.8 | MySQL 8.x `SHOW GLOBAL STATUS`；注意它是全局值 |
| **`Max_used_connections` / `Aborted_connects`** | 历史峰值与握手失败 | 峰值 > `max_connections` × 0.8；`Aborted_connects` 增速异常 | 只增不减的计数器，需看速率 |
| **连接内存（服务端）** | 连接数 × 每连接内存是否挤压 buffer pool | 每连接内存 × 连接数 > 可用内存 20% | MySQL 用 `performance_schema.memory_summary_by_thread_by_event_name`；需开 `performance_schema` |
| **代理侧 `cl_waiting` / `maxwait` / `sv_active`** | 代理是否成为新队列 | `cl_waiting` > 0 持续上涨 | PgBouncer `SHOW POOLS`；ProxySQL `stats_mysql_connection_pool` |

```sql
-- 一次巡检：连接数、并行度、长事务、连接内存（MySQL 8.4）
SELECT VARIABLE_NAME, VARIABLE_VALUE
FROM performance_schema.global_status
WHERE VARIABLE_NAME IN ('Threads_connected','Threads_running',
                        'Max_used_connections','Aborted_connects');

-- 谁在占连接：按来源 IP 汇总（暴露 Pod 数 × 池大小的乘积）
SELECT SUBSTRING_INDEX(HOST, ':', 1) AS src, COUNT(*) AS conns
FROM information_schema.PROCESSLIST
GROUP BY src ORDER BY conns DESC LIMIT 10;

-- 长事务：持锁与 undo 的源头（与 06 篇的 purge 账同源）
SELECT trx_id, trx_state, trx_started,
       TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS secs,
       trx_rows_locked, trx_rows_modified
FROM information_schema.innodb_trx
ORDER BY secs DESC LIMIT 10;

-- 每连接内存（需 performance_schema）：按线程看当前分配
SELECT THREAD_ID, ROUND(SUM(CURRENT_NUMBER_OF_BYTES)/1024/1024, 2) AS mb
FROM performance_schema.memory_summary_by_thread_by_event_name
GROUP BY THREAD_ID ORDER BY mb DESC LIMIT 10;

-- 锁等待超时配置（默认 50 s，它只兜锁等待，不兜执行时长）
SELECT @@innodb_lock_wait_timeout, @@max_connections, @@wait_timeout, @@max_execution_time;
```

> 指标纪律：**池指标一定要与库侧指标同屏看**。池等待涨而 `Threads_running` 低 ⇒ 客户端侧问题（池小、线程被别处堵、DNS/GC）；池等待涨且 `Threads_running` 高 ⇒ 数据库侧问题（慢 SQL、锁、连接过多）。另外三条版本前提要记住：`max_execution_time` 在 MySQL 8.x 只作用于 SELECT；`Aborted_connects` 是计数器要看速率；`Threads_running` 是瞬时值，采样太稀会错过尖刺。

---

## 44.12 一页速记

| 问题 | 一句话答案 |
| --- | --- |
| 池大小怎么算 | `QPS × 持有时间 ÷ 0.7`，再按 Pod 摊；不是"越大越好" |
| 为什么 20 条能扛 1,000 QPS | 持有 10 ms ⇒ 需求并发 10 ⇒ 利用率 50% |
| 池越大越慢的原因 | 活跃会话超过库的并行度 ⇒ 排队进库内、持有时长被拉长 |
| 连接在服务端是什么 | 一个线程（MySQL）或一个进程（PG）+ 全部会话状态 |
| 一条连接吃多少内存 | MySQL 稳态 0.2–1 MB、上界 ~1.7 MB；PG 5–10 MB + `work_mem × 节点数` |
| 最该先设的超时 | 池等待（100–500 ms），它是唯一挡雪崩的开关 |
| 最坏等待怎么算 | 各层超时之和 × 重试次数；缺省组合是"永不返回" |
| 有效服务能力 | `线程数 ÷ 最坏等待`；超时翻倍 ⇒ 容量减半 |
| 为什么客户端先崩 | 库一直有上限（`max_connections`/`wait_timeout`），客户端常常没有 |
| 三个时间常数怎么排 | `maxLifetime` < LB idle < 服务端 `wait_timeout` |
| 代理什么时候上 | `Pod 数 × 池大小` 逼近 `max_connections` 时 |
| transaction 模式代价 | 会话状态（`SET`/临时表/`LISTEN`/advisory lock）全部失效 |
| 分池有什么用 | 用几条连接换故障域隔离（导出/批处理不进在线池） |
| 最该监控的两个数 | 取连接 P99、库侧 `Threads_running` |
| 池能治慢查询吗 | 不能；只会把慢查询并发化 |

---

## 44.13 自检题（5 题，答案要点）

**1. 1,000 QPS、单语句持有 10 ms，池 20 条够不够？如果持有时间变成 60 ms 呢？**

够：需求并发 = `1,000 × 0.010 = 10`，利用率 50%。变成 60 ms 后需求并发 = 60，利用率 300% ⇒ 池不够，但**正确动作不是把池加到 60**，而是查那 60 ms 花在哪（缺索引？锁等待？结果集太大？）。因为在 16 核的库上加到 60 会让单语句更慢，需求并发继续上涨——正反馈环。

**2. 池等待为什么不能设 30 s？用一句话给出算式。**

因为**有效服务能力 = 线程数 ÷ 最坏等待**：200 个线程、30 s 等待 ⇒ 6.7 req/s，一台本来能跑几千 QPS 的机器被降级 100 倍。池等待的意义是"拿不到连接就快速失败并报警"，不是"多等一会儿也许就有了"。

**3. 为什么池里 30% 的连接"看起来活着"却不能用？**

因为连接有两端，两端各有独立的 idle 时限。池的 `maxLifetime`/`idleTimeout` 大于 LB 的 idle（60 s 量级）或防火墙会话老化时，**对端已经剪了，池还以为它活着**。取到这种连接时第一个请求必定失败（`Communications link failure`/broken pipe）。修法是把时间常数排序：`maxLifetime` < LB idle < 服务端 `wait_timeout`，并开 keepalive 或取时校验。

**4. 加代理一定能省连接吗？什么情况下 transaction 模式会出事？**

能不能省取决于**模式**：session 模式几乎不收敛（客户端断开才还连接）；transaction 模式在事务边界归还，收敛比可达数十倍。事件出的条件是应用依赖会话状态：`SET SESSION`（`time_zone`/`sql_mode`）、临时表、`LISTEN/NOTIFY`、advisory lock、游标、服务端 prepared statement（老版本代理不支持）。这些在 transaction 模式下会被"随机地"分到不同后端连接上，表现为"偶发的、无法复现的状态错乱"。

**5. 一次 Full GC 之后错误率飙升，你的排查顺序是什么？**

① 看错误类型：`Communications link failure` ⇒ 死连接/重建；`i/o timeout` ⇒ DNS 或建连；SQL 超时 ⇒ 排队。② 看池三件套（活跃/等待/取连接 P99）判断是"缺连接"还是"连接坏"。③ 看库侧 `Threads_running` 与 `Aborted_connects` 判断是否重连风暴。④ 看 DNS 解析 QPS 与 TTL 配置。⑤ 最后才看 GC 日志本身——GC 通常是**触发器**而不是根因：真正放大它的是"同时重建全部连接"与"池里没有校验"。

---

## 44.14 延伸阅读（锚点式）

| 方向 | 去哪 |
| --- | --- |
| 一条 SQL 走几次页 IO（持有时间的分子在哪） | [05 InnoDB 的页与行](05-innodb-page-and-row.md) §05.4 |
| 长事务怎么钉住 undo 与 purge（持有时间与锁的另一半） | [06 MVCC 与隔离级别](06-mvcc-and-isolation-levels.md) §06.7 |
| 事务超时、`sql.Tx` 与分批提交的骨架 | [06 MVCC 与隔离级别](06-mvcc-and-isolation-levels.md) §06.8 |
| RPC 侧的连接复用、重试放大与流控（与数据库连接的分界） | [40 RPC 与连接复用](40-rpc-and-connection-reuse.md) §40.3、§40.6 |
| 客户端侧的三个时间常数（`idle_timeout`/`max_lifetime`/建连超时） | [40 RPC 与连接复用](40-rpc-and-connection-reuse.md) §40.7 |
| in-doubt 分支占满连接池（连接被锁住的另一种形态） | [43 事务组件](43-transaction-components.md) §43.3 |
| 限流与容量规划（把"排队"挪到入口做） | → eng/15 限流与容量 |
| 分布式 ID 的号段服务（本身是个高并发短持有时间的库客户端） | → comp/45 |
| 分布式锁与选主（`GET_LOCK`/advisory lock 与会话绑定的坑） | → comp/46 |
| 组件故障谱：慢 / 满 / 假活 在本篇的对应形态 | → comp/52 |
| 本套写作纪律：解剖卡八问、三本放大账、数字性质 | → comp/0 §0.4、§0.7 |
