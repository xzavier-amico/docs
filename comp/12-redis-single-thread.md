# 12. 单线程的真相：事件循环、为什么快、多线程在哪

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 内存与无主 KV ｜ **编号**：12 / 53
> **本篇定位**：Part III 的开篇，只讲 Redis 的**执行内核**——一个 `aeEventLoop` 怎么把「几万个连接」压进「一条命令接一条命令」的串行执行里，以及这个形状买到了什么、付出了什么。它回答三个具体问题：为什么一个单线程进程能跑 10 万 QPS；为什么 6.0 的 IO 多线程没有让它变成多线程数据库；为什么一条 `KEYS *` 能让进程活着、端口通、探针 200，而业务已经全停。数据结构编码、持久化、Cluster 分片分别在 `→ comp/13`、`→ comp/14`、`→ comp/15`，本篇一句带过。
> **读法**：§12.2 的一句话是全篇支点，先背下来；§12.3 的事件循环图与 §12.4 的命令链路图决定后面所有结论；§12.6 的阻塞命令表和 §12.9 的大 key 时间轴是事故高发区；§12.12 的指标表每条都标了版本与开关，可直接搬进监控；评审时间不够只读 §12.2、§12.6、§12.12。

---

## 12.1 引子：三起「Redis 没崩，但全站都慢了」的故障

Redis 的故障有一个共同的错觉：**进程没死、端口能连、`PING` 有时还通**，于是所有告警都指向「应用慢」，没有人去看那个 CPU 100% 的单核。下面三起事故，分别对应本篇的三条主线：慢命令（§12.6）、大 key（§12.9）、内存水位（§12.7）。

### 事故一：运营后台一条 `KEYS user:*`，下单 P99 从 8 ms 到 3.2 s（2026-03-12）

`cache-user`：单实例 32 GB，主从 + Sentinel，早高峰 8.2 万 QPS，`GET` 的 `usec_per_call` 约 1.6 μs，下单接口 P99 8 ms。运营要「导出近 7 天注册用户」，开发写了个脚本：先 `KEYS user:*` 拿到全部 120 万键，再在应用层按 value 里的注册时间过滤——因为「Redis 没有按值查询」。

脚本挂在每 30 秒一次的定时任务上，跑 12 分钟：

| 时间 | 事件 | QPS | 下单 P99 | 超时请求 |
| --- | --- | --- | --- | --- |
| 09:00 | 基线，`instantaneous_ops_per_sec` 7.8 万 | 78,000 | 8 ms | 0 |
| 09:14 | 导出任务上线，每 30 s 执行一次 `KEYS user:*`（120 万键，约 240 ms/次） | 78,000 | 8 ms | 0 |
| 09:16 | 第一次执行：主线程 240 ms 不处理任何其他 fd | 78,000 → 0（240 ms 内） | **3.2 s** | 约 1.1 万 |
| 09:16–09:28 | 24 次执行 + 客户端重试 3 次，重试流量把 QPS 抬到 9.4 万 | 94,000 | 1.8–3.2 s | 累计 47 万 |
| 09:28 | 摘掉定时任务，`SLOWLOG` 里 24 条 `keys` 记录，单条 210–260 ms | 78,000 | 12 ms | 0 |
| 复盘 | 12 分钟，下单失败 8,600 单，估算 GMV 损失 260 万元 | — | — | — |

根因不是「`KEYS` 慢」，而是**`KEYS` 在主线程上执行，而主线程只有一个**：240 ms 里 epoll 的就绪队列堆着几万条命令，一条也执行不了。更贵的是输出：120 万个键名约 34 MB，全部进了这个客户端的 output buffer（`normal` 类默认无上限），网卡还要 272 ms 才排空（1 Gbps）。

### 事故二：`HGETALL` 一个 50 万字段的 hash，主线程每次停 700 ms（2026-05-21）

推荐服务把「全站类目特征」塞进一个 hash：`feature:category:all`，50 万个 field，每个 value 是 2.5 KB 的特征向量，合计约 1.35 GB。服务启动时 `HGETALL` 一次拉全量到本地，之后每 15 秒刷新一次，3 个 Pod 各刷各的。

| 时间 | 事件 | 主线程占用 | 商品详情 P99 |
| --- | --- | --- | --- |
| 基线 | 3 个 Pod 每 15 s 各一次 `HGETALL`，每次约 700 ms | 14% 时间片被占（3 × 0.7 ÷ 15） | 40 ms |
| 10:02 | 扩容到 8 个 Pod，刷新频率不变 | 37% 时间片 | 380 ms |
| 10:20 | 自动扩容到 16 个 Pod（CPU 高触发），刷新风暴 | 75% 时间片 | 1,900 ms |
| 10:41 | 商品详情开始 5xx，触发限流；人工下线刷新任务 | 0 | 55 ms |
| 复盘 | 39 分钟，详情页超时 220 万次，转化率下降 6.8 个百分点 | — | — |

700 ms 的推导：50 万 field，每个 value 2.5 KB，`HGETALL` 要把 **1.25 GB** 从 Redis 内存拷进输出缓冲。按 5 GB/s 的内存带宽仅拷贝就 250 ms；再加 50 万次 `addReplyBulk` 的固定开销（约 0.5 μs/次 = 250 ms）与 dict 遍历，700 ms 是合理量级。**`HGETALL` 的成本不取决于「hash 有多大」，取决于「value 总字节有多少」**——这条在 §12.9 会变成大 key 的判定口径。

### 事故三：`maxmemory` 没设，凌晨被 OOM killer 杀掉，DB 被打穿（2026-07-09）

同一个团队把缓存、会话、一个「临时任务队列」混部在 64 GB 机器上，`maxmemory 0`（默认，即不限制）。三个月里 `used_memory_rss` 从 38 GB 涨到 61 GB：会话 key 写了 TTL 但有一半漏写，队列的 `LPUSH` 速度是 `BRPOP` 的 1.3 倍。

| 时间 | 事件 | 内存 / 指标 |
| --- | --- | --- |
| 07-06 | 巡检时 RSS 57 GB，`mem_fragmentation_ratio` 1.08，无告警 | 89% 主机内存 |
| 07-09 03:12 | 内核 OOM killer 选中 `redis-server`（RSS 61 GB，无 swap） | 进程消失 |
| 03:12 | systemd 拉起空实例，加载 2 小时前的 RDB 快照 | 缓存命中率 8% |
| 03:13 | 回源 QPS 从 3,000 涨到 27,000，MySQL CPU 100%，连接池打满 | 全站 5xx |
| 03:50 | 手工预热热点 key + 限流，逐步恢复 | 38 分钟 |
| 复盘 | 4.7 万笔订单失败；根因不是 Redis 慢，是「没有上限的缓存」把主机吃成了 OOM | — |

三起事故分别对应：**一条命令独占主线程（§12.6）**、**一次回复拷贝 1.25 GB（§12.9）**、**没有内存上限时内核替你做逐出（§12.7）**。

---

## 12.2 一句话定位与唯一真问题

**定位**：Redis 是一个**单线程执行命令、多路复用网络、纯内存存储**的 KV 服务器。它对外承诺的是「单条命令原子 + 微秒级执行 + 按 key 的数据结构操作」；不承诺持久（`appendfsync everysec` 下最多丢 1 s）、不承诺跨 key 强一致（主从异步）、不承诺 Cluster 下跨槽多 key 原子。缓存该用什么形态、失效语义怎么设计，属于 `architect/04`，本篇不重写。

### 唯一真问题

> **Redis 快不是因为「单线程」，而是「内存 + 无锁 + 一次 epoll 批量处理」；单线程真正的含义是——一条命令不会被打断，但一条慢命令会停掉所有人。**

这句话有两个推论，全篇都在展开它们：

1. 「快」的三个来源里，**只有一个是单线程的功劳**（无锁），另外两个是内存与 IO 模型。把「单线程」当成性能原因，就会得出「加线程能更快」的错误结论——6.0 加了 IO 线程，命令执行仍然是单线程，因为**加线程解决的是网络吞吐，不是命令吞吐**。
2. 「不被打断」是免费礼物：不需要锁、不需要 MVCC、不需要考虑命令之间的交错。代价同样直接：**执行时间的上限就是最慢那条命令**。一条 200 ms 的命令，等价于把整个实例的 QPS 归零 200 ms。

### 八问卡（Redis 版，密度对齐 `01-component-anatomy-method.md` §01.4）

| 问 | 一句话答案 | 数字 / 边界 |
| --- | --- | --- |
| ① 承诺 | 单命令原子 + 微秒级内存 KV | 不承诺持久（最多丢 1 s）、不承诺跨 key 原子、不承诺切主不丢 |
| ② 数据 | `dict` + SDS + 按阈值切换的紧凑编码 | 阈值两侧差一个数量级；大 key 与 O(N) 灾难都长在这条线上（`→ comp/13`） |
| ③ 链路 | 网络 RTT 主导，命令执行 µs 级 | 单命令 1–10 µs；同机房 RTT 0.2–0.5 ms；pipeline 后每命令可到 3–5 µs |
| ④ 并发 | 单线程 ⇒ 单命令原子；复制异步 | `MULTI/EXEC` 无回滚；Cluster 跨槽不可用；脑裂窗口无限长 |
| ⑤ 扩展 | **分片**（16384 槽）；副本只扩读与可用性 | 单 key 写吞吐不可横向扩，只能垂直 + 客户端拆 key |
| ⑥ 崩 | 慢 → 假活；满 → 语义漂移；脑裂 | 逐出策略在「当数据源」时 = 数据丢失策略 |
| ⑦ 指标 | 内存水位 / 分命令分位 / 逐出增速 / fork 耗时 | `latency-tracking` 7.0+ 默认开；`latest_fork_usec` > 300 ms 越线 |
| ⑧ 换 | fork 代价 > 收益、要持久、要跨 key 事务 | 单实例 > 25 GB 且 fork > 300 ms ⇒ 拆片或换形态 |

### 本篇与相邻篇的分工（反冗余）

| 篇 | 讲什么 | 本篇只给 |
| --- | --- | --- |
| `→ comp/13` | 编码阈值（listpack / hashtable / skiplist） | 为什么 50 万 field 的 hash 一定是真哈希表，以及它如何放大 `HGETALL` |
| `→ comp/14` | fork、COW、RDB/AOF、主从同步 | `fork` 让主线程停一下这一句，其余不展开 |
| `→ comp/15` | 16384 槽、MOVED/ASK、扩容迁移 | Cluster 下多 key 命令为什么直接失败，其余不展开 |
| `architect/04` | 缓存形态谱系、失效语义、穿透/击穿/雪崩 | 一个字都不重写；本篇只讲 Redis 内核为什么只能这样 |
| `../eng/35-storage-model-selection.md` | 什么时候不该用缓存 / KV 做事实源 | 只给「`maxmemory` 到顶后的逐出 = 删数据」这一条内核依据 |

---

## 12.3 物理形态：事件循环与单线程模型

Redis 没有「请求线程」这个概念。它只有一个 `aeEventLoop`（`ae.h`），主线程在这个循环里转，每一轮做三件事：跑一次 `beforeSleep`、用 `epoll_wait` 取一批就绪的 fd、按事件类型回调。没有连接一个线程、没有协程、没有任务队列——**连接只是 fd，命令只是 fd 上可读字节流的一部分**。

### `aeEventLoop` 里真正重要的六个字段

| 字段 | 类型 | 作用 | 为什么值得记 |
| --- | --- | --- | --- |
| `maxfd` | `int` | 当前最大的 fd + 1 | `select` 后端的遗留；epoll 只用它做校验 |
| `setsize` | `int` | 事件数组容量 | 等于 `maxfd`，扩容时整体 realloc |
| `events` | `aeFileEvent *` | **fd → 回调**的数组 | 每个 fd 两个槽：`AE_READABLE` / `AE_WRITABLE` |
| `fired` | `aeFiredEvent *` | 本次就绪的 fd + 掩码 | `epoll_wait` 的结果落在这里，长度上限 `AE_MAX_EVENTS`（epoll 后端为 1024） |
| `time_events` | `aeTimeEvent *` | 时间事件链表 | `serverCron` 挂在这条链上，按 `when` 排序 |
| `apidata` | `void *` | 后端私有数据 | epoll 后端里就是 `epoll_fd` |

文件事件的注册点只有三处：监听 fd 上的 `acceptTcpHandler`、客户端 fd 上的 `readQueryFromClient`、以及回复没写完时的 `sendReplyToClient`。时间事件只有一个主角：`serverCron`。**整个服务器的行为，就是这三类回调加一个 cron 的排列组合。**

### 一轮循环到底做了什么

```text
main thread
  │
  ├─ aeMain(server.el)
  │    while (!server.el->stop) {
  │      aeProcessEvents(el, AE_ALL_EVENTS)
  │        │
  │        ├─ beforeSleep()                 ← 每轮 poll 之前
  │        │    ├─ 唤醒因数据到达而 unblock 的客户端（BLPOP 等）
  │        │    ├─ flushAppendOnlyFile()   ← AOF 缓冲刷盘（everysec 时可能不刷）
  │        │    └─ handleClientsWithPendingWrites()  ← 把 output buffer 写回 socket
  │        │
  │        ├─ epoll_wait(epfd, fired, AE_MAX_EVENTS, timeout)
  │        │    timeout = 距最近一个时间事件的毫秒数；没有时间事件则阻塞等待
  │        │
  │        ├─ 对 fired 里每个 fd：
  │        │    ├─ 可读 → acceptTcpHandler / readQueryFromClient
  │        │    │           └─ 读进 querybuf → 解析 RESP → 执行命令 → addReply
  │        │    └─ 可写 → sendReplyToClient（把上次没写完的回复继续写）
  │        │
  │        └─ processTimeEvents() → serverCron()
  │             └─ 过期采样、渐进式 rehash、客户端超时、复制、持久化状态机
  │             （注：到期的时间事件在 poll 之前也会先跑一次，这里只画主路径）
  │    }
```

三个细节决定了很多线上现象：

1. **`epoll_wait` 的 timeout 由「最近的时间事件」决定**。空闲且没有时间事件时它可以无限阻塞；有 `serverCron` 时最多等 100 ms（`hz` 默认 10）。所以 Redis 的空闲 CPU 不是 0，而是每秒醒来 10 次做巡检——`hz` 调到 100 就是用空闲 CPU 换过期与超时的及时性。
2. **一次 `epoll_wait` 返回的是一批 fd，不是一条命令**。1000 个连接同时有数据，一轮循环就处理 1000 个 fd；每个 fd 上可能还有一整条 pipeline。这就是「一次 epoll 批量处理」的字面含义，也是 §12.5 第二条原因的物证。
3. **命令执行发生在 `readQueryFromClient` 的调用栈里**。`processInputBuffer` 会把 querybuf 里所有完整命令**一口气执行完**才返回——不是执行一条就回事件循环。所以一个 1000 条的 pipeline，是在一次 fd 回调里串行跑完 1000 条命令的（§12.8）。

### 时间事件：`serverCron` 是唯一的「后台任务」

`serverCron` 默认每 `1000/hz` 毫秒跑一次（`hz 10` ⇒ 100 ms），做的是**维护而不是业务**：

| 职责 | 函数 / 机制 | 与业务延迟的关系 |
| --- | --- | --- |
| 过期键采样删除 | `activeExpireCycle`（慢周期） | 占主线程时间，预算约为 `hz` 周期的 25%（§12.7） |
| 渐进式 rehash | `incrementallyRehash` | 每个 db 每次搬一点桶，避免大字典一次性 rehash 卡住 |
| 客户端超时 / 输出缓冲检查 | `clientsCron` | 每秒检查 `client-output-buffer-limit`，超限的客户端被异步关闭 |
| 复制状态机 | `replicationCron` | 每秒检查主从连接、处理副本超时与 `WAIT` 的 `GETACK` |
| 持久化状态 | `backgroundSaveDoneHandler`、AOF 刷盘检查 | 子进程结束、AOF 重写完成等状态推进 |
| 统计与时间缓存 | `updateCachedTime`、`trackInstantaneousMetric` | `instantaneous_ops_per_sec` 就是在 `STATS_METRIC_SAMPLES`（16）个采样点上做滑动平均，`hz 10` 时窗口约 1.6 s |

注意：**`serverCron` 本身也是主线程**。它跑得越重（过期键多、客户端多、复制积压大），业务命令排得越久。这不是「后台线程」，只是「同一线程里定期执行的维护代码」。

### 「单线程」不等于「单核」

`INFO cpu` 里 `used_cpu_sys + used_cpu_user` 经常超过 100%，`top -H` 能看到十几个线程——这不矛盾，因为只有**命令执行**被限制在一个核上：

| 执行体 | 谁创建 | 干什么 | 会不会阻塞命令执行 |
| --- | --- | --- | --- |
| 主线程 | 进程本身 | 事件循环、命令执行、过期、淘汰、复制传播 | —（它就是命令执行） |
| `bio` 后台线程 | 4.0+（lazy free）/ 更早的 close、AOF fsync | 关闭大文件、`fsync` AOF、释放 `UNLINK` 的值 | 不阻塞，但会抢内存带宽与 allocator 锁 |
| IO 线程 | **6.0+**，`io-threads` 默认 1（关闭） | 回写 socket；`io-threads-do-reads yes` 时也读 socket 并解析 RESP | 不阻塞；**命令执行仍在主线程** |
| fork 子进程 | `BGSAVE` / `BGREWRITEAOF` | 写 RDB / 重写 AOF | 不阻塞执行，但 `fork` 本身停一下（`→ comp/14`） |
| jemalloc 后台线程 | 分配器 | 归还内存页给 OS | 不阻塞 |

**6.0 的 IO 多线程必须说清楚它没做什么**：它把「把回复写回 socket」这类**IO 密集**的活分给几个线程（默认 `io-threads-do-reads no`，读 socket 与 RESP 解析仍由主线程做；设为 `yes` 才把这部分也分出去），然后由主线程按顺序执行解析好的命令。`processCommand`、`call()`、数据结构操作、过期、淘汰全部还在主线程。因此：

- 开 `io-threads 4` 提升的是「大量小命令、网络往返密集」场景的吞吐，通常是 1.5–2 倍量级，不是 4 倍；
- 它不改变单条命令的延迟，也不改变「一条慢命令停掉所有人」；
- 当命令本身是瓶颈（CPU 100% 在 `call()` 里），加 IO 线程没有任何用——反而多几个线程抢同一个内存分配器。

一句话：**6.0 之后 Redis 是「IO 多线程 + 命令单线程」，不是「多线程 Redis」。**

---

## 12.4 一次命令的完整链路（逐跳耗时）

下面这张图是 `GET user:42` 从网卡到网卡的全部路径。注意「Redis 自己」只占中间一小段，而客户端感知的 P99 里，网络与排队往往是大头。

```text
客户端
  │  TCP 请求（RESP: *2\r\n$3\r\nGET\r\n$7\r\nuser:42\r\n）
  ▼
┌───────────────────────────── 内核 ─────────────────────────────┐
│ socket 接收缓冲区（rcvbuf）                                      │
└───────────────────────────────┬────────────────────────────────┘
                                │ epoll 就绪
                                ▼
┌───────────────────────────── redis 主线程 ─────────────────────┐
│ aeProcessEvents                                                │
│   └─ readQueryFromClient(fd)                                   │
│        ├─ read()  ──► c->querybuf        （一次最多读 16 KB）    │
│        ├─ processMultibulkBuffer()       （RESP 解析，切 argv）  │
│        ├─ lookupCommand()                                    │
│        │    └─ dictFetchValue(server.commands, "get")  O(1)     │
│        ├─ call()                                               │
│        │    ├─ c->cmd->proc(c)  → getCommand → dictFind(db->dict)│
│        │    ├─ addReplyBulk()   → c->buf（16 KB 静态）或 reply 链 │
│        │    └─ 统计：commandstats / slowlog / 复制传播 / AOF 缓冲  │
│        └─ 返回事件循环                                          │
│   └─ beforeSleep → handleClientsWithPendingWrites() → write()   │
└───────────────────────────────┬────────────────────────────────┘
                                │ 内核发送缓冲区（sndbuf）→ 网卡
                                ▼
客户端
```

### μs 级分解（量级结论，非承诺值）

**假设**：同机房 / loopback，长连接，无 swap，`appendfsync everysec` 且本次不触发 fsync，命令是 `GET`/`SET` 混合，无 pipeline，客户端单线程。取 §01.4 的区间上沿做容量计算。

| # | 跳 | 代码位置 | 耗时（μs） | 说明 |
| --- | --- | --- | --- | --- |
| 1 | `accept` | `acceptTcpHandler` | ≈ 0（摊销） | 长连接下每命令为 0；新建连接时 5–20 μs，一次事件最多 accept 1000 个 |
| 2 | epoll 返回 + 分发 | `aeApiPoll` / `aeProcessEvents` | 0.2–0.5 | 一批事件摊到每条命令后更小 |
| 3 | `read()` | `readQueryFromClient` | 0.5–1.5 | 系统调用 + 内核到用户态拷贝；一次读多条则摊薄 |
| 4 | RESP 解析 | `processMultibulkBuffer` | 0.2–0.5 | 短命令 2–5 个 argv，主要是边界检查与 SDS 分配 |
| 5 | 查命令表 | `lookupCommand` | 0.05–0.1 | `server.commands` 是 dict，O(1)，约一次内存访存 |
| 6 | **执行** | `call()` → `c->cmd->proc` | **1–10** | `GET` 1–2；`SET` 2–4（含 AOF 缓冲与复制传播）；复合命令 5–10 |
| 7 | 写回复 | `addReply*` | 0.2–0.5 | 先试 `c->buf`（16 KB），放不下才挂 `reply` 链 |
| 8 | 回写 socket | `beforeSleep` → `write()` | 0.5–1.5 | 一次 write 可带走多条回复；没写完就注册 `AE_WRITABLE` |
| 9 | 统计与收尾 | `call()` 尾部 | 0.2–0.5 | `commandstats` 累加、慢日志判定、`latencyAddSampleIfNeeded` |
| — | **合计** | — | **3–14** | 取 **10 μs** 做 §12.5/§12.6 的容量假设 |

这张表要读出三件事：

1. **第 6 跳是唯一随命令复杂度变化的项**。`GET` 与 `KEYS` 的差别不在第 1–5 跳，而在 `c->cmd->proc` 里面有没有 O(N) 循环。这就是为什么「Redis 单命令快」和「Redis 会被一条命令卡死」是同一件事的两面。
2. **第 3、8 跳是系统调用，不是 Redis 代码**。它们的成本由内核与网卡决定；pipeline 能摊薄它们，但摊薄不了第 6 跳。
3. **RTT 不在表里**。同机房 0.2–0.5 ms 的往返，是 Redis 内部耗时的 20–50 倍。所以「Redis 快」在端到端视角下，首先是「少一次磁盘 IO + 少一次跨机往返」，其次才是「命令执行 1–10 μs」。

把这条链路和 §12.3 的循环图叠起来看：**每个客户端 fd 的可读事件，都会在同一个线程里走完 1–9 跳；在第 6 跳没结束之前，任何其他 fd 的事件都不会被处理。** 这就是下一节要算的账。

---

## 12.5 为什么快：四条可验证的原因

「因为它是内存数据库」是最流行也最偷懒的答案：memcached 也在内存里，照样是多线程 + 锁。Redis 快的真实原因要拆成四条，每条都得能拿出一个观测证据；拿不出的，就只是信仰。

### ① 纯内存 + 无锁：省掉的不是 `lock()`，是整类并发开销

内存省掉的是磁盘 IO（一次 SSD 随机读 50–100 μs，比整条命令贵一个数量级）；无锁省掉的是**并发数据结构、原子操作、内存屏障、以及跨核 cache line 失效**。

精确一点：单次无竞争的 `pthread_mutex_lock/unlock` 只要 15–25 ns，按 10 万 QPS 算也不过 2–5 ms/s，完全不是瓶颈。**无锁的真实收益在尾延迟**——32 核上多个线程改同一个哈希表，锁竞争会让 P99 从 100 μs 跳到几毫秒，而单线程没有这个尾巴。这也是为什么「单线程」在 2009 年是正确选择：它用「放弃多核」换来了「延迟可预测」。

**证据**：`perf stat -p $(pidof redis-server) -e context-switches,cache-misses`。稳态下主线程的 `context-switches` 接近 0（每次系统调用与 IO 线程交互才有一点），而同样 QPS 的多线程 KV 每秒有数千次；`INFO cpu` 的 `used_cpu_user` 增长速率约 1 s/s，说明**只有一个核在跑命令**。

### ② 一次 `epoll_wait` 取一批事件，再批量处理

这是最被低估的一条。1000 个连接同时有数据，Redis 只做一次 `epoll_wait` 就拿回 1000 个 fd，然后在一个循环里逐个处理；而「一连接一线程」的模型要 1000 次唤醒、1000 次上下文切换。系统调用的固定成本（0.5–1 μs）被摊到了几十条命令上。

**证据**：`strace -c -p $(pidof redis-server)` 里 `epoll_wait` 的调用次数比 `instantaneous_ops_per_sec` 低 1–2 个数量级。8 万 QPS 的实例上，`epoll_wait` 通常每秒几千次，`read`/`write` 各几万次——**每次 wait 平均带回 10–30 个就绪 fd**，这个比值就是批量处理的直接物证。

### ③ RESP 协议简单 + 命令查找是哈希表 O(1)

RESP 是「前缀 + 长度 + 内容」的二进制安全协议：`*2\r\n$3\r\nGET\r\n$7\r\nuser:42\r\n`。没有 header 压缩、没有 HPACK、没有流优先级、没有 TLS 记录层（TLS 是可选外层）。解析一条短命令只需 2–5 次长度判断与一次 SDS 分配，0.2–0.5 μs。

命令查找是 `dictFetchValue(server.commands, name)`，哈希表 O(1)，约 50–100 ns，与注册了多少命令无关。**执行阶段的 key 查找也是 O(1)**：`db->dict` 是哈希表，`GET` 的耗时与 db 里有 100 万还是 1 亿个 key 无关。

**证据**：`INFO commandstats` 的 `usec_per_call`。同样一个 `get`，在 100 万键与 1000 万键的两个实例上都是 1–2 μs；而 `keys` 的 `usec_per_call` 会随键数线性涨。**这条对比就是「O(1) vs O(N)」的现场实验**，不需要看源码。

### ④ 单线程避免了上下文切换与 cache line 抖动

多线程程序的隐形成本：每次调度换栈、换寄存器、污染 L1/L2；共享数据在核间来回搬 cache line（一次 false sharing 能让吞吐掉一半）。单线程把这些全部消掉：数据结构常驻在同一个核的 L1/L2 里，指针追逐不会跨核。

**证据**：`perf stat` 的 `instructions per cycle`（IPC）在 Redis 上通常 1.5–2.5，而多线程 KV 在同等负载下常掉到 1.0 以下；`cache-misses` 也高 2–3 倍。反过来，这也解释了它的天花板：**只有一个核的 L1/L2 在为你服务**，再多的核也帮不上命令执行。

### 四条原因对照表

| 原因 | 省掉了什么 | 可观测证据 | 它买不到什么 |
| --- | --- | --- | --- |
| ① 内存 + 无锁 | 磁盘 IO、锁竞争、跨核同步 | `context-switches` ≈ 0；`used_cpu_user` ≈ 1 s/s | 不防「一条命令自己跑 10 s」 |
| ② 一次 epoll 批量 | 每连接唤醒与切换 | `epoll_wait` 次数 << ops/s | 不防「一个 fd 上有一万条 pipeline」 |
| ③ RESP + O(1) 查找 | 协议解析与查找的复杂度 | `usec_per_call` 与键数无关 | 不防 O(N) 命令本身 |
| ④ 无线程切换 / 无 cache 抖动 | 调度与缓存一致性开销 | IPC 1.5–2.5；`cache-misses` 低 | 不能利用多核，单核上限就是实例上限 |

**这四条里，只有第 ①④ 条跟「单线程」直接相关，②③ 是 IO 模型与协议设计。** 所以「Redis 快是因为单线程」这句话，最多对了 40%。

### 容量账：10 μs 为什么只兑现 8–9 万 QPS

§12.4 的 10 μs 是 `call()` 内的时间（执行 + 回复入缓冲 + 统计），但事件循环还有一笔「每命令固定开销」：`epoll_wait` 返回后的 fd 分发、`read()`/`write()` 两次系统调用、`beforeSleep` 的遍历、时间缓存更新。按 1.5–2.5 μs 估：

| 项 | 推导 | 结果 |
| --- | --- | --- |
| 执行预算 | `1 ÷ 10 μs` | 100,000 QPS |
| 加循环固定开销 | `1 ÷ (10 + 2) μs` | 83,300 QPS |
| 区间 | `1 ÷ 12.5 μs` ~ `1 ÷ 11.5 μs` | **80,000–87,000 QPS** |
| pipeline 后 | 固定开销摊到 N 条，执行仍是 10 μs | 约 99,000 QPS（上限仍是执行） |
| 简单命令 + pipeline | `exec ≈ 2 μs`，固定开销摊薄 | 30–50 万 QPS |

**量级结论**（非承诺值）。假设：单核无争抢、无 AOF fsync 进入命令路径、无 swap、`GET`/`SET` 混合、无慢命令、客户端不成为瓶颈。`instantaneous_ops_per_sec` 在 8–9 万平台化，就是「执行 10 μs + 循环 2 μs」的现场证据；一旦上 pipeline，同一个指标能过 10 万——说明瓶颈从「每命令固定开销」换成了「纯执行」。**「10 万 QPS」是每条一次往返的天花板，不是 CPU 的天花板**；这两件事经常被混为一谈。

---

## 12.6 单线程的代价：一条慢命令停掉所有人

单线程买到的最大的礼物是**原子性**：一条命令从开始执行到写完回复，中间不会插入另一条命令。你不需要为 `INCR`、`LPUSH`、`HSET` 加锁，也不需要担心读到「半个更新」。`MULTI/EXEC` 与 Lua 把这个原子性扩展到多条命令——注意 `MULTI` **没有回滚**，中途出错的命令不影响前面已执行的命令。

代价同样干净：**这条原子性的边界，就是整个实例的吞吐边界**。命令执行期间主线程不碰任何其他 fd，所以可以写成三条式子：

> 实例的有效 QPS ≈ `1 ÷ 平均每条命令占用主线程的时间`；
> 一条命令的阻塞时长 `T_block ≈ N × 单元素成本`；
> `T_block` 期间到达的请求全部排队，排队数 ≈ `QPS × T_block`（Little 定律）。

### 阻塞命令清单（在线路径上出现就是事故）

| 命令 | 复杂度 | 为什么危险 | 替代写法 | 版本 / 备注 |
| --- | --- | --- | --- | --- |
| `KEYS pattern` | O(N)，N = 全库键数 | 遍历整个 dict 并逐键 glob 匹配，期间零服务 | `SCAN 0 MATCH pattern COUNT 1000` 循环 | 全版本；`SCAN` 只保证遍历期间一直在的键必被返回，可能重复 |
| `SMEMBERS key` | O(N) | 大集合一次性拷出，回复缓冲暴涨 | `SSCAN key 0 COUNT 500` | 全版本 |
| `HGETALL key` | O(N) | 成本 ≈ value 总字节 ÷ 内存带宽，50 万 × 2.5 KB = 1.25 GB 拷贝 | `HSCAN key 0 COUNT 200`，或按业务只取需要的 field | 全版本；`HMGET` 只能取指定 field |
| `LRANGE key 0 -1` | O(N) | 取全列表；百万元素列表可停几百 ms | 分页 `LRANGE key start start+999`，用游标推进 | 全版本 |
| `SORT key ...` | O(N log N)，且可能 O(N) 内存 | 排序 + 可选 `GET`/`STORE`，大集合直接打满 | `SORT_RO`（7.0+）只读版 + `LIMIT`；或写入时维护有序结构 | `SORT_RO` 7.0 引入，避免只读副本上误用写命令 |
| `ZRANGE key 0 -1` | O(log N + M) | M = 全量时退化为 O(N)，且带 score 拷贝 | `ZRANGE key start stop` 分页 / `ZSCAN` | 全版本 |
| `FLUSHALL` / `FLUSHDB` | O(N) | 同步释放所有 dict，几十 GB 可停数秒 | `FLUSHALL ASYNC`（4.0+） | 4.0 引入 `ASYNC`/`LAZY` 修饰符 |
| `SAVE` | O(内存) | 同步写 RDB，全程阻塞 | `BGSAVE`（fork 子进程） | 全版本；`SAVE` 只该出现在测试里 |
| `DEBUG SLEEP n` | O(n) | 显式睡死主线程，连 `PING` 都不回 | 永远不要在线上用；要测延迟用 `redis-cli --latency` | 全版本；`DEBUG` 可在生产禁用 |
| `DEL bigkey` | O(N) 释放 | 同步释放每个元素，10 GB hash ≈ 10 s（§12.9） | `UNLINK bigkey`（4.0+） | 4.0 引入异步释放 |
| `MGET k1 ... k100000` | O(N) | 回复缓冲 = 所有 value 之和，慢消费者直接吃爆内存 | 分批 100–500 个 key；或 pipeline | 全版本 |
| `SUNION`/`SINTER`/`ZUNIONSTORE` | O(N×M) | 多集合运算，结果集可能巨大 | 拆批 + 客户端归并，或预计算 | 全版本 |

### 算例：100 万键的 `KEYS *`，在 10 万 QPS 上丢多少请求

**假设**：100 万键、平均键名 24 B、单键处理成本 200 ns（dict 遍历 + glob 匹配 + `addReplyBulk` 拷贝）、实例稳态 10 万 QPS、客户端超时 100 ms。

| 步 | 推导 | 结果 |
| --- | --- | --- |
| 1 | 阻塞时长 `T_block = 100 万 × 200 ns` | **200 ms** |
| 2 | 期间到达的请求（排队峰值）= `100,000 × 0.2 s` | **20,000 条** |
| 3 | 超时条数 = `QPS × max(0, T_block − timeout)` = `100,000 × (0.2 − 0.1)` | **10,000 条** |
| 4 | 若客户端超时 1 s：超时 0 条，但 P99 = 200 ms，仍有 2 万条被延迟 | 事故仍是事故 |
| 5 | 回复体积 = `100 万 × (24 B + ~10 B RESP 开销)` | **约 34 MB** |
| 6 | 34 MB 在 1 Gbps 上排空 = `34 × 8 ÷ 1,000` | **272 ms**（比执行还长） |
| 7 | 若每条超时请求重试 3 次：额外 `3 × 10,000 = 30,000` 条命令，阻塞被延长 | 正反馈 |

**量级结论**（非承诺值）。假设：100 万键、24 B 键名、200 ns/键、无 AOF fsync、输出未触发流控；换键长、匹配率、超时值，数字会变，`N_timeout = QPS × max(0, T_block − timeout)` 的关系不变。

第 3 步的公式值得记住：**超时条数不是「排队总数」，而是「等待时间超过客户端超时的那一段」**。`T_block < timeout` 时丢包为 0，但 SLO 已经破了；`T_block` 每增加一个 `timeout`，丢包就多一整段 QPS。这就是为什么「偶发一条 200 ms 的慢命令」在 100 ms 超时的系统里等于「一次丢掉 1 万条」的瞬时故障——如果它每分钟来一次，就是每分钟 1 万条，而不是「偶发」。

```text
t=0 ms      KEYS user:* 开始执行（主线程进入 O(N) 循环）
            ├─ 其他 fd 的就绪事件堆在 epoll 里，一条也处理不了
t=0–200 ms  100 万次 dictNext + stringmatchlen + addReplyBulk
t=100 ms    第一批客户端超时（timeout=100 ms），开始重试
t=200 ms    命令结束；34 MB 回复已进 output buffer
t=200–472 ms 34 MB 以 1 Gbps 排空；主线程可处理新命令，但输出缓冲仍占内存
t=472 ms    队列排空；重试流量还在路上
```

### 一条可执行的判据

**任何 O(N) 命令，先算 `N × 100 ns`。超过 1 ms 就不许出现在在线路径上**——1 ms 在 10 万 QPS 上就是 100 条请求被延迟，而 P99 的采样窗口里 1 ms 已经足够把「8 ms」打成「1 s」。超过 10 ms 的命令，必须改成 `SCAN`/`HSCAN`/分页，或者挪到离线实例上跑。

这条判据也解释了为什么 `SLOWLOG` 默认阈值是 10 ms 却不适合抓这类事故：**一条 2 ms 的命令，每秒执行 500 次，就能吃掉 100% 的主线程**，而它永远不会进慢日志。慢日志抓的是单次时长，不是累计占用；累计占用要看 `INFO commandstats` 的 `usec_per_call × calls`（§12.12）。

---

## 12.7 过期、淘汰与内存水位

Redis 的内存水位由三个独立机制共同决定：**过期（TTL）**、**逐出（maxmemory-policy）**、**分配器碎片**。三者都会让 `used_memory` 上涨，但处理方式完全不同；把它们混为一谈，就会写出「明明设了 TTL，内存还是涨」的工单。

### 过期：惰性删除 + 定期采样，没有「定时器」

`EXPIRE` 只在 `db->expires` 这个辅助 dict 里给 key 记一个到期时间戳，**key 仍然躺在主 dict 里占着内存**。删除发生在两个地方：

| 机制 | 触发点 | 做什么 | 预算 / 频率 |
| --- | --- | --- | --- |
| **惰性删除** | 任何访问该 key 的命令 | `keyIsExpired` 命中则删掉、返回空、向 AOF/副本传播 `DEL` | 单次，成本算在访问它的命令里 |
| **定期采样（慢周期）** | `serverCron` → `databasesCron` → `activeExpireCycle` | 每个 db 随机采样 `ACTIVE_EXPIRE_CYCLE_KEYS_PER_LOOP`（源码常量，默认 20）个带 TTL 的键，删掉过期的 | 时间预算约为 `hz` 周期的 25%（`hz 10` ⇒ 每秒 250 ms） |
| **定期采样（快周期）** | `beforeSleep` | 上一轮过期比例高时再来一轮，最多 1 ms | 只在有大量过期键时触发 |

采样是**自适应**的：一轮里过期键占比超过 25%，就继续采下一轮（仍在时间预算内）；低于 25% 就收工。所以「过期键很多」时它会自己加班，「过期键很少」时几乎不花时间。`active-expire-effort`（6.0+，1–10，默认 1）调高的是这个加班的上限，代价是主线程 CPU。

**副本不主动过期**。副本上的过期键要等主库传播 `DEL`/`UNLINK` 才真正删除；副本读到期键时会返回空（逻辑上过期），但内存还占着。`replica-ignore-maxmemory yes`（默认）时副本也不会自己逐出——所以一个断连的副本，内存会一直涨到 OOM。

### 内存涨而 TTL 没救：四个真实机制

| 机制 | 为什么 TTL 没生效 | 观测 | 处理 |
| --- | --- | --- | --- |
| 写入带 TTL 的速率 > 过期清理能力 | 清理是采样的、有时间预算，不是无限并行 | `expired_keys` 增速 < 带 TTL 的写入速率；`db0:expires` 持续涨 | 提高 `hz` / `active-expire-effort`；缩短 TTL；拆分实例 |
| 大量 key 没有 TTL | 漏写 `EXPIRE`，或 `SET` 覆盖时把 TTL 清掉了（`SET` 不带 `KEEPTTL` 时会清掉 TTL） | `INFO keyspace` 的 `keys` 与 `expires` 差值 | 写入路径强制 TTL；用 `SET ... EX` 而不是 `SET` + `EXPIRE` |
| `volatile-*` 策略 + 无 TTL 键占多数 | 逐出只在「有 TTL 的键」里选，选不到就拒绝写 | `evicted_keys` 不涨、`total_error_replies` 涨（OOM command not allowed） | 换 `allkeys-*`，或把无 TTL 数据挪走 |
| 大 key 到期但 `lazyfree-lazy-expire no` | 过期删除是同步释放，一个 10 GB hash 到期就是 10 s 停顿 | `LATENCY LATEST` 的 `expire-cycle` 事件；访问该 key 的那条命令在 `SLOWLOG` 里超阈值 | 开 `lazyfree-lazy-expire yes`（4.0+）；拆大 key |

**算例（量级结论）**：主库每秒写入 50 万个带 TTL 的 key，平均 100 B；过期清理按 25% CPU、每个键 1 μs 估算，能力约 `0.25 s ÷ 1 μs = 25 万键/s`。缺口 `50 − 25 = 25 万键/s`，内存以 `25 万 × 100 B = 25 MB/s` 增长，**一天 2.16 GB**。假设：`hz 10`、无大 key、单 db、删除成本 1 μs；实际会随 key 大小与 db 数浮动，但「写入速率超过清理能力就会线性涨」这个结论不变。

### 逐出：`maxmemory-policy` 的八种选择

`maxmemory` 是硬上限（默认 0 = 不限制）。每次执行**可能分配内存的命令**前，`processCommand` 会调用 `performEvictions`，按策略挑 key 删除，直到回到水位以下——**逐出发生在命令路径上，由当前这条命令买单**，不是后台线程。

| 策略 | 候选集 | 选谁 | 适用 | 风险 |
| --- | --- | --- | --- | --- |
| `noeviction` | 无 | 不逐出，写命令返回 OOM 错误 | 当数据源、队列、锁 | 写入直接失败 |
| `allkeys-lru` | 所有 key | 近似 LRU（采样） | 纯缓存 | 全表扫描会污染「最近使用」 |
| `allkeys-lfu` | 所有 key | 近似 LFU（4.0+） | 热点稳定、长尾明显 | 新 key 计数从 5 起，可能被过早淘汰 |
| `allkeys-random` | 所有 key | 随机 | 访问均匀、无热点 | 命中率明显低于 LRU |
| `volatile-lru` | 仅带 TTL | 近似 LRU | 缓存与持久数据混部 | 无 TTL 键多时退化为 `noeviction` |
| `volatile-lfu` | 仅带 TTL | 近似 LFU（4.0+） | 混部 + 热点 | 同上 |
| `volatile-random` | 仅带 TTL | 随机 | 少见 | 同上 |
| `volatile-ttl` | 仅带 TTL | 剩余 TTL 最短的 | 会话、验证码 | 同上 |

**近似 LRU 到底多近似**：`maxmemory-samples`（默认 5）表示每次随机抽 5 个 key，删掉其中最久未用的那个。从 N 个里抽 k 个，最小值的期望分位是 `1/(k+1)`——5 个样本意味着「被淘汰的 key 平均落在最旧 16.7% 的分位上」，10 个样本是 9.1%。这就是为什么默认 5 已经够用、调到 10 更接近真 LRU、调到 3 会明显掉命中率。每个 key 的 `lru` 字段只有 24 bit，精度是秒级（`LRU_CLOCK_RESOLUTION` 1000 ms）。

**LFU 是另一套**：`robj->lru` 的 24 bit 被拆成「16 bit 上次访问时间（分钟）+ 8 bit 对数计数器」，初始值 `LFU_INIT_VAL = 5`；每次访问按概率 `1/((counter−5) × lfu-log-factor + 1)` 递增，`lfu-log-factor` 默认 10；每 `lfu-decay-time` 分钟（默认 1）减 1。`OBJECT FREQ key`（4.0+）能看当前计数（需 LFU 策略）。LFU 抗「扫一遍全表把 LRU 污染」的能力更强，代价是新 key 需要时间积累计数。

**逐出的语义陷阱**：如果业务把 Redis 当数据源，`allkeys-lru` 的每一次逐出都是**静默删数据**；如果当缓存，逐出只是回源变慢。同一个参数，在两种用法下一个是正常行为、一个是数据丢失。这条界限属于架构决策，`architect/04` 讲得更细。

### `maxmemory` 该设多少

不是「主机内存的 90%」。要减掉三块：`maxmemory ≤ 主机内存 − fork COW 余量 − 输出/客户端缓冲余量 − 分配器碎片余量`。

- **COW 余量**：`BGSAVE`/`BGREWRITEAOF` 期间写操作会复制页，最坏接近 100% 写入页，常见 10–30%；内存越大、写入越猛，余量越大（`→ comp/14`）。
- **输出缓冲**：`normal` 类默认无上限，一个慢消费者能吃掉几 GB（§12.8）。
- **碎片**：`mem_fragmentation_ratio = used_memory_rss ÷ used_memory`，长期 > 1.5 说明分配器还不了页；< 1 说明部分内存被 swap 出去，延迟会灾难性变差（Redis 最怕 swap）。

经验起点是主机内存的 60–70%，再用 `latest_fork_usec` 与 `mem_fragmentation_ratio` 校准。§01.4 的换挡线是「单实例 > 25 GB 且 `latest_fork_usec` > 300 ms」——到这条线，垂直扩容的收益已经被 fork 停顿吃掉了。

---

## 12.8 管道、Lua 与输出缓冲

命令执行的单线程模型有两个「合法的例外」：pipeline 让**多条命令共享一次网络往返**，Lua/Function 让**多条命令共享一次原子执行**。两者都能把吞吐抬高一个量级，也都以「更长时间占用主线程」为代价。第三个话题是输出缓冲——它不是执行模型的一部分，却是单线程实例最常见的内存死法。

### pipeline：摊薄的是 RTT，不是执行时间

串行执行 1,000 条命令，每条都要一次完整往返：

```text
串行：  T = N × (RTT + exec)
pipeline：T = RTT + N × exec + 序列化/传输
加速比：S = (RTT + exec) / (RTT/N + exec)  →  N→∞ 时 S → 1 + RTT/exec
```

代入 RTT 0.2 ms、`exec` 10 μs、N = 1,000：

| 方式 | 推导 | 总耗时 | 每条摊销 |
| --- | --- | --- | --- |
| 串行 | `1,000 × (0.2 + 0.01)` | **210 ms** | 210 μs |
| pipeline | `0.2 + 1,000 × 0.01 + 约 0.2（传输）` | **10.4 ms** | 10.4 μs |
| 加速比 | `210 ÷ 10.4` | **约 20×** | — |
| 理论上限 | `1 + 0.2 / 0.01` | **21×** | — |

**量级结论**（非承诺值）。假设：RTT 0.2 ms、命令执行 10 μs、客户端不限制在途请求、无丢包；执行更轻或更重时，加速比按同一公式移动。

上限公式说明一件重要的事：**pipeline 只能把 RTT 压到 0，压不掉 `exec`**。所以：

- 命令越轻，pipeline 收益越大。`SET` 的 `exec` 约 2 μs 时，上限是 `1 + 200/2 = 101×`；1,000 条串行 202 ms，pipeline 约 2.2 ms。
- 命令越重，pipeline 越没用。一条 `HGETALL` 700 ms，pipeline 10 条就是 7 s 的连续阻塞。
- §01.4 给的「pipeline 100 条时每条约 3–5 μs」是同一个公式的另一组取值：100 条、`exec` 2.5 μs、RTT 0.2 ms ⇒ `0.2 + 0.25 = 0.45 ms`，每条 4.5 μs。**它不是另一个结论，只是 N 与 `exec` 不同。**

### pipeline 的四个风险

| 风险 | 机制 | 表现 | 约束 |
| --- | --- | --- | --- |
| 非原子 | pipeline 只是把命令连在一起发，服务端仍逐条执行，其他客户端可以插进来 | 「批量写」被别的请求看到中间状态 | 要原子用 `MULTI/EXEC` 或 Lua |
| 错误要逐条查 | 每条命令的回复里都可能是 `-ERR`，客户端只看最后一个会漏 | 静默失败 | 逐条检查 `Cmd.Err()` |
| 内存放大 | 请求堆在 `querybuf`，回复堆在 output buffer | 攒 100 万条 GET 1 KB 的 value = 1 GB 回复 | `client-query-buffer-limit` 默认 1 GB；**输出侧 normal 类默认无上限** |
| 独占主线程 | `processInputBuffer` 一次把 querybuf 里所有完整命令执行完 | 一次 fd 回调跑 1,000 条，其他连接等 | 批大小控制在 100–500 |

### Go 客户端骨架：批大小 + 超时 + 逐条错误

```go
const batch = 200 // 不要用「攒够 10 万条再发」；批越大，主线程被独占越久

func mgetBatch(ctx context.Context, rdb *redis.Client, keys []string) (map[string]string, error) {
	out := make(map[string]string, len(keys))
	for start := 0; start < len(keys); start += batch {
		end := min(start+batch, len(keys))
		// 每批一个独立超时：整条链路的 deadline 由上层 context 控制
		batchCtx, cancel := context.WithTimeout(ctx, 50*time.Millisecond)
		pipe := rdb.Pipeline()
		cmds := make([]*redis.StringCmd, 0, end-start)
		for _, k := range keys[start:end] {
			cmds = append(cmds, pipe.Get(batchCtx, k))
		}
		_, err := pipe.Exec(batchCtx)
		cancel()
		// redis.Nil 是「key 不存在」，不是管线失败；真正的失败要看每条 Cmd
		if err != nil && !errors.Is(err, redis.Nil) {
			return nil, fmt.Errorf("pipeline exec: %w", err)
		}
		for i, cmd := range cmds {
			v, err := cmd.Result()
			if errors.Is(err, redis.Nil) {
				continue
			}
			if err != nil {
				return nil, fmt.Errorf("key %s: %w", keys[start+i], err)
			}
			out[keys[start+i]] = v
		}
	}
	return out, nil
}
```

这段代码的三个要点，覆盖了上表四个风险里的后三个：**批大小固定**（不让一次回调独占太久）、**每批独立超时**（不让一条慢命令拖死整条链路）、**逐条取 `Result()`**（不漏掉单条错误）。注意 `redis.Nil` 必须单独处理，否则「缓存 miss」会被当成故障。

### Lua / Function：把原子性扩展到多条命令

Lua 脚本在 Redis 里是**原子执行**的：脚本运行期间不执行其他命令。这解决了「读-改-写」的竞态，但也把「一条慢命令停掉所有人」放大成「一段慢脚本停掉所有人」。

超时机制要说清楚，否则会误以为 `busy-reply-threshold` 能杀脚本：

1. 脚本运行超过 `busy-reply-threshold`（默认 5000 ms，旧名 `lua-time-limit`）后，Lua 钩子会调用 `processEventsWhileBlocked()`，让事件循环短暂跑起来，**给其他客户端回 `-BUSY`**。脚本本身**继续运行**。
2. 只有 `SCRIPT KILL`（7.0+ 还有 `FUNCTION KILL`）能终止它，而且**仅当脚本还没执行过写命令**。一旦脚本里有过 `SET`/`LPUSH` 之类的写，`SCRIPT KILL` 会返回 `UNKILLABLE`——因为杀了它，主库与副本/AOF 就会不一致。
3. 所以「Lua 死循环」的最终解法通常只有 `SHUTDOWN NOSAVE` 或 `kill -9`，然后从持久化恢复。

```lua
-- 反例：原子 ≠ 免费。这段脚本把 N 次 O(M) 操作串成一次不可打断的阻塞。
-- KEYS[1] 是一个 50 万元素的 set；每个 id 对应一个 500 field 的 hash。
local ids = redis.call('SMEMBERS', KEYS[1])          -- O(N)，50 万
for i = 1, #ids do
  redis.call('HGETALL', 'feature:' .. ids[i])        -- O(M)，每个 500
end
return #ids

-- 更糟的版本：只要脚本写过一次，SCRIPT KILL 就失效
-- redis.call('SET', KEYS[1], 'started')
-- while true do end
```

**工程纪律**：脚本里不许出现 `KEYS`、`SMEMBERS`、`HGETALL`、无界 `while`；单脚本目标执行时间 < 1 ms；需要遍历大集合时，把它拆成多次 `EVALSHA`，每次处理一批，让主线程有机会服务别人。Redis 7.0 的 Function（`FUNCTION LOAD` / `FCALL`）只是把脚本变成可命名的库，原子性与超时语义完全一样，不会让它变快。

### 输出缓冲：慢消费者如何吃爆内存

回复不是「直接写网卡」，而是先落在客户端的 output buffer 里：先试 `c->buf`（16 KB 静态缓冲），放不下就挂 16 KB 的 `reply` 块链。`beforeSleep` 尝试 `write()`；没写完就注册 `AE_WRITABLE`，等下次可写事件继续。**只要客户端读得慢，这个链就会一直长。**

| 客户端类 | 默认 hard / soft / seconds | 典型对象 | 超限后果 |
| --- | --- | --- | --- |
| `normal` | `0 0 0`（无上限） | 普通业务连接 | 不主动断开，内存可以无限涨 |
| `slave` | `256mb 64mb 60` | 副本连接 | 断开副本 → 全量同步 → fork（`→ comp/14`） |
| `pubsub` | `32mb 8mb 60` | 订阅者 | 断开订阅者；soft 限额持续 60 s 也断 |

`soft` 是「持续 seconds 秒超过 soft 才断」，`hard` 是「一超过就断」。**`normal` 默认无上限**是很多 OOM 的源头：一个客户端发 `MGET` 取 10 万个 10 KB 的 value（1 GB 回复），自己读得慢，Redis 的 RSS 就涨 1 GB。

**算例**：订阅者下行 1 Mbps，发布端 10 MB/s，积压以 `10 − 0.125 ≈ 9.9 MB/s` 增长；`pubsub` 的 32 MB hard 限额在 `32 ÷ 9.9 ≈ 3.2 s` 后触发断连。如果这是 `normal` 类（无上限），60 s 就是 594 MB，足够把一个小实例推过 `maxmemory`，触发逐出——**慢消费者不会只影响自己，它会通过内存水位影响所有人**。

观测入口是 `CLIENT LIST` 的 `omem`（该客户端输出缓冲字节数）与 `oll`（reply 链块数），以及 `INFO clients` 的 `client_recent_max_output_buffer`。处理手段：`CLIENT KILL ID <id>`、给 `normal` 类设一个符合业务的限额（注意别把合法的大回复误杀）、把大回复改成批量小回复。

---

## 12.9 大 key 与 lazy free：一次 `DEL` 为什么会停 10 秒

大 key 不是「value 很大」这么模糊。工程上要同时看两个轴，因为它们触发的是两条不同的物理路径：

| 轴 | 口径（团队约定，不是 Redis 常量） | 触发的路径 | 观测手段 |
| --- | --- | --- | --- |
| **字节数** | string > 10 KB 警告，> 1 MB 严重；集合类 > 1 MB | 网络传输、输出缓冲、`HGETALL` 拷贝 | `MEMORY USAGE key [SAMPLES n]`（4.0+）给出字节估算 |
| **元素数** | 集合类 > 5,000 警告，> 50,000 严重 | 遍历、删除、过期、`DEL` 释放 | `redis-cli --bigkeys`（按元素数采样）、`DEBUG OBJECT` 的 `serializedlength`（近似序列化长度） |
| **编码** | `hash-max-listpack-entries 128` 等阈值之上 | listpack → hashtable/skiplist，每元素一次独立分配 | `OBJECT ENCODING key` |

第二条轴的关键在编码：一个 128 field 以内、value ≤ 64 B 的 hash 是一段连续 listpack，删它是「释放一块内存」；50 万 field 的 hash 是真哈希表，每个 field 都有独立的 `dictEntry` + SDS，删它是「释放 50 万块内存」。**同样叫 `DEL`，前者 1 μs，后者 10 s。**

### 同步 `DEL` 的时间轴

`DEL` 的执行路径是 `dbDelete` → `dictDelete` → `dictFreeUnlinkedEntry` → `decrRefCount` → 递归释放 value 里的每个元素。这条路径**全部在主线程**，没有 yield、没有分批：

```text
t=0         DEL feature:category:all 到达（10 GB hash，约 1 亿个 field）
t=0–10 s    主线程在 dictRelease 里逐个 free；epoll 的就绪队列无人处理
t=1 s       客户端 A 的 1 s 超时触发 → 重试 → 又一条命令排在后面
t=3 s       所有业务连接陆续超时，重试风暴形成
t=5 s       P99 已经不可测（没有请求能完成）
t=10 s      释放完成，返回 :1；积压的命令开始执行
```

**估算**（量级结论）：释放带宽按 1 GB/s 计，`10 GB ÷ 1 GB/s = 10 s`。另一种算法：1 亿个 field × 100 ns/次 `free` = 10 s。两个算法结果一致，因为 10 GB ÷ 1 亿 ≈ 100 B/field，正好对应「每块内存的释放成本约 100 ns」。假设：无 swap、字段平均 100 B、jemalloc 正常归还；实际在 5–30 s 之间浮动。

10 s 的主线程停顿还有一个二阶后果：**Sentinel 的 `down-after-milliseconds` 默认 30 s**，所以 10 s 不会触发主从切换；但如果这个 key 是 100 GB（约 100 s），Sentinel 会判主观下线并触发切换——**一次 `DEL` 可以引发一次主从切换**，而切换又会带来全量同步与 fork（`→ comp/14`）。

### `UNLINK` 与 lazy free：把释放挪到后台线程

`UNLINK`（4.0+）做两件事：**O(1) 把 key 从 keyspace 摘掉**（之后任何读都看不到它），**把 value 塞进 `bio` 的 lazy free 队列**，由后台线程慢慢释放。主线程的阻塞从「10 s」变成「几 μs」。

| 配置（4.0+，除注明外默认 `no`） | 作用 | 什么时候该开 |
| --- | --- | --- |
| `lazyfree-lazy-user-del`（6.0+） | 让 `DEL` 等价于 `UNLINK` | 无法改客户端、又必须删大 key 时 |
| `lazyfree-lazy-user-flush`（6.0+） | 让 `FLUSHALL`/`FLUSHDB` 默认异步 | 大实例的清理脚本 |
| `lazyfree-lazy-expire` | 过期删除走异步 | 大 key 带 TTL，或过期速率高 |
| `lazyfree-lazy-eviction` | 逐出释放走异步 | 内存压力大、逐出频繁 |
| `lazyfree-lazy-server-del` | `RENAME` 等覆盖旧值走异步 | 大 key 被频繁覆盖 |
| `replica-lazy-flush` | 副本加载新 RDB 前清库走异步 | 副本全量同步期间 |

**`UNLINK` 不是免费的**，它把成本换了个地方付：

1. **内存不会立刻降**。key 从 keyspace 消失了，但 value 还在 bio 队列里，`used_memory` 要等后台线程释放才降。如果此时已接近 `maxmemory`，逐出会继续删别的 key——你以为腾出了 10 GB，实际还没有。
2. **后台线程会抢 allocator 与内存带宽**。`bio` 线程 free 的同时，主线程还在服务请求；jemalloc 的 arena 锁竞争会让主线程的分配变慢（大 key 集中释放时可见）。
3. **RSS 可能不降**。jemalloc 释放的页不一定立刻还给 OS，`mem_fragmentation_ratio` 可能先升后降；如果此时 fork 做 BGSAVE，父进程 RSS 峰值 = 未释放的旧值 + 新写入，COW 压力更大。
4. **`UNLINK` 只解决「删」的停顿，不解决「读」的停顿**。`HGETALL` 一个 10 GB hash 仍然是 10 GB 的拷贝，`UNLINK` 帮不上。

### 检测与预防

```bash
# ① 按元素数找大 key（采样，不会阻塞，但可能漏）
redis-cli --bigkeys

# ② 按字节数精确看一个 key（SAMPLES 0 = 全量采样，慢；默认 5）
redis-cli MEMORY USAGE feature:category:all SAMPLES 0

# ③ 看编码：listpack 还是 hashtable/skiplist
redis-cli OBJECT ENCODING feature:category:all

# ④ 看序列化长度与元素数（输出因版本而异，只作参考）
redis-cli DEBUG OBJECT feature:category:all
```

预防的四条，按性价比排序：**拆**（按业务维度拆成 `feature:{bucket}:{id}`，单 key 控制在 5,000 元素内）、**限**（SDK 层禁止对集合类写入超过阈值，超过就报错而不是默默写进去）、**改**（读路径用 `HSCAN`/`HMGET` 而不是 `HGETALL`，只取需要的字段）、**删**（大 key 一律 `UNLINK`，并对大 key 的过期开 `lazyfree-lazy-expire`）。

---

## 12.10 架构边界：单线程能做什么、不能做什么

### 三列对照

| 能做（在什么前提下） | 不能做（物理原因） | 靠什么绕过 |
| --- | --- | --- |
| 单 key 10 万次/s 的读写（命令 10 μs、无慢命令） | 单 key 超过 10 万次/s——命令执行被一个核串行化 | 本地缓存挡热点、按 key 拆片、批量合并（`MGET`/pipeline） |
| 单命令原子（`INCR`、`LPUSH`、`HSET` 不需要锁） | 多命令原子——`MULTI` 无回滚，中途错误不影响已执行部分 | Lua/Function；或把状态机放进一个 hash 用单命令改 |
| 亚毫秒 P99（前提是**没有** O(N) 命令） | 有 O(N) 命令时 P99 不可控——主线程被一条命令独占 | `SCAN`/`HSCAN`/分页、把重活挪到离线实例 |
| 单实例几十 GB（读多写少、fork 可控） | 单实例无限扩内存——fork + COW 的停顿随内存线性涨 | 分片（`→ comp/15`）；或换成不做 fork 的形态 |
| 异步复制 + 哨兵（RPO 秒级） | 切主不丢——异步复制的窗口永远存在 | `WAIT`、`min-replicas-to-write` 只能缩小窗口（`→ comp/14`） |
| 单机内多 key 操作 | Cluster 下跨槽多 key 原子——槽是分区单位 | hash tag `{user:1}:name`；或客户端拆分（`→ comp/15`） |

### ⑤ 扩展维度：四选一，Redis 的答案是「分片 + 垂直切 + 批处理」

- **分片（主答案）**：Cluster 16384 槽，把 key 空间切开，每片一个主线程。这是唯一能线性扩**写**的手段。
- **副本（不是写扩展）**：写永远打主节点；副本只扩读与可用性，代价是内存 × 副本数、写放大 × 副本数。
- **垂直切（最被低估）**：缓存、会话、队列、分布式锁**绝不共用一个实例**。它们的逐出策略、持久化要求、延迟预算完全不同；混部的代价是「队列的 `LPUSH` 把缓存的 `maxmemory` 顶爆」——事故三就是这个。
- **批处理摊薄**：pipeline / `MGET` 把 RTT 摊掉，是客户端侧最便宜的 10 倍；但它摊不掉 `exec`，也救不了慢命令。

**单 key 的写吞吐不可横向扩**。这是 Redis 与「无主 KV」最本质的区别：Dynamo 家族可以让 N 个副本同时接受同一个 key 的写（用冲突解决换可用性），Redis 的同一个 key 永远只有一个主线程在改。热点 key 超过 10 万 QPS，只能靠本地缓存、key 拆分或换形态——没有第四个选项。

### ⑥ 会在哪崩（只写系统级失效）

- **慢 → 假活**：一条 `KEYS`/`HGETALL`/`DEL bigkey` 卡住唯一线程。进程活着、端口能连、`PING` 也会超时，但业务已经停了。这是 Redis 最典型的失效形态，也是本篇 §12.6 的全部内容。
- **满 → 语义漂移**：`maxmemory` 到顶后按策略逐出。当缓存时，逐出只是回源；当数据源时，**逐出就是静默删数据**，而且 `noeviction` 会让写直接失败。同一个参数在两种用法下含义相反。
- **脑裂**：网络分区 + 未开 `min-replicas-to-write`，旧主继续收写，哨兵在另一侧选新主，恢复后一侧数据被丢弃。窗口长度 = 分区持续时间（`→ comp/15`、`→ comp/16`）。

### ⑧ 换掉它的信号

| 信号 | 阈值 | 为什么这条线之后它不再是正确选择 |
| --- | --- | --- |
| 单实例内存 | > 25 GB 且 `latest_fork_usec` > 300 ms | fork + COW 的停顿已经吃掉垂直扩容的收益；拆片或换持久化形态 |
| 单 key 写 QPS | > 10 万/s 持续 | 单核串行化的物理上限；本地缓存/拆 key/换形态 |
| 需要「不丢」 | RPO 要求 = 0 | 异步复制 + `everysec` 给不了；换追加日志型（`→ comp/28`）或关系库 |
| 需要跨 key 事务 | 多 key 原子写 | Cluster 从设计上不给；单实例垂直切，或换支持多 key 事务的形态 |

---

## 12.11 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到物理形态） | 定位 | 修复 | 防复发 |
| --- | --- | --- | --- | --- |
| 全站 P99 周期性尖刺到秒级，CPU 100%，无 5xx | `KEYS`/`SORT`/大 `DEL` 在主线程上跑 O(N) | `SLOWLOG GET 20` 看单条耗时；`INFO commandstats` 看 `usec_per_call` | 改成 `SCAN`/分页/`UNLINK`；把任务挪到离线实例 | ACL 禁 `KEYS`（6.0+：`ACL SETUSER app -keys`）；预发环境用 `MONITOR` 抽样 1 分钟 |
| 商品详情偶发 2 s，`HGETALL` 单条 700 ms | 50 万 field 的 hashtable，回复拷贝 1.25 GB | `MEMORY USAGE key SAMPLES 0` + `SLOWLOG` | `HSCAN`/`HMGET` 只取需要的字段；拆 hash | SDK 限制单集合元素数；评审读路径禁止 `HGETALL` |
| 缓存、队列、会话互相拖慢 | 共用一个主线程与一个 `maxmemory`；队列把缓存挤掉 | `INFO keyspace` 看 db 分布；按前缀统计命令来源 | 按用途拆实例 | 一实例一用途；`maxmemory-policy` 按用途定 |
| 客户端 OOM，`used_memory` 不高 | pipeline 无限攒批，`querybuf` + 回复链暴涨 | `CLIENT LIST` 的 `qbuf`、`omem`、`oll` | 限批 100–500，加超时 | SDK 强制 batch size；`client-query-buffer-limit` 兜底 |
| 所有请求返回 `BUSY`，几秒后恢复/或彻底卡死 | Lua 死循环；写过之后 `SCRIPT KILL` 无效 | `SCRIPT KILL` 的返回（`UNKILLABLE` 表示已写过）+ 错误日志里的 `Busy script` | 未写：`SCRIPT KILL`；已写：重启并从持久化恢复 | 脚本 review；禁止无界循环；把重脚本挪到独立实例 |
| 凌晨进程消失，`dmesg` 有 OOM killer | `maxmemory 0`，缓存无上限 | `INFO memory` 的 `maxmemory`；`dmesg -T \| grep -i oom` | 设 `maxmemory` 为 60–70% 主机内存 | 配置巡检；`maxmemory == 0` 直接告警 |
| 个别客户端被断开，日志有 output buffer limit | 慢消费者 + `normal` 类无上限/或 pubsub 限额 | `CLIENT LIST` 的 `omem`；`INFO stats` 相关断连计数 | `CLIENT KILL`；订阅端加消费速率 | 订阅单独实例；`normal` 类设合理限额 |
| 每次 BGSAVE 时 P99 抖一下 | `fork` 的页表复制 + COW | `INFO persistence` 的 `latest_fork_usec` | 见 `→ comp/14`：降内存、错峰、换形态 | 把 `latest_fork_usec` 纳入容量评审 |
| Cluster 下 `MGET`/`MULTI` 报 `CROSSSLOT` | 多 key 不在同一槽，Cluster 不做跨槽路由 | 错误信息；`CLUSTER KEYSLOT` | hash tag（`{user:1}:a`、`{user:1}:b`）或客户端拆分 | 见 `→ comp/15`：设计期就定 key 的槽归属 |
| 内存缓慢上涨，`expires` 也在涨 | 带 TTL 的写入速率 > 过期采样清理能力 | `expired_keys` 增速 vs 写入速率；`db0:expires` | 提高 `hz`/`active-expire-effort`；缩短 TTL；拆实例 | 写入强制 TTL；监控 `expires` 曲线 |
| 单个热点 key 打满一个核 | 单 key 的写永远只有一个主线程 | 按 key 的 QPS（客户端埋点）+ `INFO latencystats` | 本地缓存、key 拆分、读写分离 | 热点识别进压测；见 `architect/04` 热点 key 一节 |

这张表里前三条占了线上 Redis 事故的绝大多数。它们的共同点是：**`INFO` 的全局指标一切正常**——`connected_clients` 正常、`used_memory` 正常、`keyspace_hits` 正常，只有 `instantaneous_ops_per_sec` 掉下去、P99 翘起来。所以下一节的观测必须带「分命令」维度，否则永远抓不到是哪条命令。

---

## 12.12 观测：指标、拐点与告警

### 指标表（每条都标版本与开关）

| 指标 | 取法 | 版本 / 开关前提 | 拐点 | 它说明什么 |
| --- | --- | --- | --- | --- |
| `instantaneous_ops_per_sec` | `INFO stats` | 全版本；`STATS_METRIC_SAMPLES`（16）点滑动平均，`hz 10` 时约 1.6 s | 比基线掉 30% 且 CPU 100% | 主线程被某条命令占住，或客户端在重试 |
| `usec_per_call`（分命令） | `INFO commandstats` | 全版本；`CONFIG RESETSTAT` 会清零；7.0+ 多 `rejected_calls`/`failed_calls` | 某命令比基线涨 10× | 命令内部 N 变大（集合变大）或 CPU 被抢 |
| `latency_percentiles_usec_<cmd>` | `INFO latencystats` | **7.0+**；`latency-tracking yes`（默认开）；分位由 `latency-tracking-info-percentiles` 配置（默认 `50 99 99.9`） | `p99 > 1 ms` 对内存命令即异常 | 分命令的分位延迟；比平均值更能抓到长尾 |
| `SLOWLOG GET` | `SLOWLOG GET 20` | 全版本；`slowlog-log-slower-than 10000`（10 ms）默认，`-1` 关闭；`slowlog-max-len 128` | 出现 10 ms+ 的记录 | **只测命令执行**，不含排队与网络；慢客户端不会进 |
| `LATENCY DOCTOR` / `LATENCY LATEST` | `LATENCY DOCTOR` | 全版本；`latency-monitor-threshold 0` 默认**关闭**，需 `CONFIG SET latency-monitor-threshold 100` | `fork`、`expire-cycle`、`command` 事件 | 事件级延迟尖刺；不设阈值就永远为空 |
| `blocked_clients` | `INFO clients` | 全版本 | 持续 > 0 是正常的（BLPOP/XREAD）；突增 10× | 客户端在等数据，不是主线程被阻塞——**别把它当慢查询指标** |
| `client_recent_max_output_buffer` | `INFO clients` | 6.2+ | 单客户端 > 10 MB | 慢消费者的第一信号 |
| `omem` / `oll` / `qbuf` | `CLIENT LIST` | 全版本（字段随版本增加） | `omem` 持续 > 10 MB；`qbuf` 接近 1 GB | 输出/输入缓冲在涨；pipeline 或慢消费者 |
| `mem_fragmentation_ratio` | `INFO memory` | 全版本 | > 1.5 碎片；**< 1 说明在 swap** | RSS/used 的比值；< 1 比 > 1.5 更危险 |
| `evicted_keys` 增速 | `INFO stats` | 需设 `maxmemory`；否则恒为 0 | 任何非零且业务当数据源 | 逐出在删数据；缓存场景看命中率是否下降 |
| `expired_keys` 增速 | `INFO stats` | 全版本 | 明显低于带 TTL 的写入速率 | 过期清理跟不上，内存会涨 |
| `latest_fork_usec` | `INFO persistence` | 全版本 | > 300 ms | 每次快照/全量同步都让主线程停跳（`→ comp/14`） |

### 三个必须先建立的对照关系

1. **`commandstats` 是「执行」，不是「端到端」**。`usec_per_call = 1.6 μs` 的 `GET`，客户端 P99 可能是 3 ms——差值全在网络、排队、客户端线程池。**用 `commandstats` 定位「哪条命令慢」，用客户端指标定位「用户为什么慢」**，两者不能互相替代。
2. **`SLOWLOG` 抓不到「高频小慢」**。阈值 10 ms 时，一条 2 ms、每秒 500 次的命令永远不出现，却能吃掉 100% 主线程。要把 `usec_per_call × calls` 按秒折算成「该命令占主线程的比例」，才能看到真正的成本结构。
3. **`blocked_clients` 不是「被阻塞的客户端」**。它统计的是 `BLPOP`/`BRPOP`/`XREAD BLOCK`/`WAIT` 这类**主动等待数据**的客户端，主线程完全有空。主线程被慢命令占住时，这个值不会涨——涨的是客户端侧的超时。

### 采集命令

```bash
# ① 分命令平均耗时：按 usec_per_call 排序，先看谁最贵
redis-cli INFO commandstats | grep '^cmdstat_' | sed 's/^cmdstat_//; s/:calls=.*usec_per_call=/ /; s/,.*//' | sort -k2 -nr | head -20

# ② 分命令分位（7.0+，latency-tracking 默认开）
redis-cli INFO latencystats | grep '^latency_percentiles_usec_' | sed 's/^latency_percentiles_usec_//; s/:p50=[^,]*,p99=/ /; s/,p99.9=.*//' | sort -k2 -nr | head -20

# ③ 慢日志：只记录执行时间，不含排队；看 argv 前 3 个参数即可定位业务
redis-cli SLOWLOG GET 10

# ④ 延迟事件：先确认阈值没关（默认 0 = 关闭）
redis-cli CONFIG GET latency-monitor-threshold
redis-cli CONFIG SET latency-monitor-threshold 100
redis-cli LATENCY LATEST
redis-cli LATENCY DOCTOR

# ⑤ 谁在吃输出缓冲：omem 是字节数，oll 是 reply 链块数
redis-cli CLIENT LIST | awk '{for(i=1;i<=NF;i++){if($i ~ /^omem=/){split($i,a,"="); if(a[2]+0>10485760) print $0}}}'

# ⑥ 水位与碎片：maxmemory=0 时第一行就是"没有上限"
redis-cli INFO memory | grep -E 'used_memory:|used_memory_rss:|maxmemory:|mem_fragmentation_ratio:'

# ⑦ 过期与逐出：两个都是累计值，要看导数
redis-cli INFO stats | grep -E 'expired_keys:|evicted_keys:|keyspace_hits:|keyspace_misses:'
```

### 告警表达式骨架（Prometheus / redis_exporter 命名）

```yaml
# ① 内存水位：maxmemory 必须 > 0，否则这条永远不触发
- alert: RedisMemoryHigh
  expr: redis_memory_used_bytes / redis_memory_max_bytes > 0.85
  for: 5m

# ② 逐出：缓存场景要配合命中率看；数据源场景任何逐出都是事故
- alert: RedisEvicting
  expr: rate(redis_evicted_keys_total[5m]) > 0
  for: 2m

# ③ 碎片或 swap：< 1 比 > 1.5 更危险（说明部分内存被换出）
- alert: RedisFragmentation
  expr: redis_mem_fragmentation_ratio > 1.5 or redis_mem_fragmentation_ratio < 1
  for: 10m

# ④ 连接被输出缓冲限制断开：慢消费者已经发生
- alert: RedisOutputBufferDisconnect
  expr: increase(redis_client_output_buffer_limit_disconnections_total[5m]) > 0
```

最后一条的指标名随 exporter 版本可能不同，采集不到就退回日志关键字 `overcoming of output buffer limits`——**指标可以换名字，「慢消费者被断连」这个事实不能没有告警**。

---

## 12.13 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 执行模型 | 一个 `aeEventLoop` + 一个主线程；连接只是 fd，命令只是 fd 上可读字节流的一部分 |
| 一轮循环 | `beforeSleep` → `epoll_wait`（批量取 fd）→ 文件事件回调 → 时间事件 `serverCron` |
| `serverCron` | 默认 `hz 10` ⇒ 每 100 ms 一次；做过期采样、渐进 rehash、客户端超时、复制与持久化状态推进 |
| 单线程 ≠ 单核 | `bio` 线程、6.0+ IO 线程、fork 子进程、jemalloc 线程都在跑；**命令执行仍只有一个线程** |
| 6.0 IO 线程 | 默认只回写 socket；`io-threads-do-reads yes` 时才读 socket 并解析 RESP；`processCommand`/`call()` 仍在主线程 |
| 为什么快 | 内存 + 无锁（省尾延迟）、一次 epoll 批量、RESP 简单 + O(1) 查找、无线程切换与 cache 抖动 |
| 单命令成本 | 3–14 μs，取 10 μs 做容量假设；`GET` 1–2 μs，`SET` 2–4 μs，复合命令 5–10 μs |
| 原子性 | 单命令天然原子；`MULTI/EXEC` 无回滚；Lua/Function 原子但会独占主线程 |
| 慢命令 | `T_block ≈ N × 单元素成本`；期间所有请求排队；超时条数 = `QPS × max(0, T_block − timeout)` |
| 阻塞清单 | `KEYS`、`SMEMBERS`、`HGETALL`、`LRANGE 0 -1`、`SORT`、`ZRANGE 0 -1`、`FLUSHALL`、`SAVE`、`DEBUG SLEEP`、大 `DEL` |
| 过期 | 惰性删除 + `activeExpireCycle` 采样；慢周期在 `serverCron`，快周期在 `beforeSleep`；副本等主库 `DEL` |
| 逐出 | 八种 `maxmemory-policy`；`performEvictions` 在命令路径上，由当前命令买单；LRU 是采样近似（`1/(k+1)` 分位） |
| LFU | 8 bit 对数计数 + 分钟级衰减；`LFU_INIT_VAL = 5`；`OBJECT FREQ` 可看 |
| pipeline | 摊薄 RTT，不摊薄 `exec`；上限 `S → 1 + RTT/exec`；批大小 100–500，逐条查错 |
| Lua 超时 | `busy-reply-threshold`（默认 5000 ms）只让其他客户端收到 `-BUSY`，**不杀脚本**；写过就 `UNKILLABLE` |
| 输出缓冲 | 先 `c->buf` 16 KB，再挂 reply 链；`normal` 类默认无上限；慢消费者能通过内存水位影响所有人 |
| 大 key | 两条轴：字节数（`MEMORY USAGE`）与元素数（`--bigkeys`）；编码阈值决定它是「一块内存」还是「N 块内存」 |
| `DEL` vs `UNLINK` | `DEL` 同步释放，10 GB ≈ 10 s；`UNLINK`（4.0+）O(1) 摘 key，后台线程释放；内存不会立刻降 |
| 扩展 | 分片（唯一扩写手段）、副本（只扩读）、垂直切（最有效）、pipeline（摊 RTT）；单 key 写不可横向扩 |
| 换形态 | > 25 GB 且 `latest_fork_usec` > 300 ms；单 key > 10 万写/s；要 RPO=0；要跨 key 事务 |

---

## 12.14 自检题（5 题）

**1. 一个单线程进程为什么能跑 10 万 QPS？四条原因里哪条最关键，哪条最常被误当成「因为内存」？6.0 的 IO 线程为什么没有把上限提高 4 倍？**
<details><summary>答案</summary>

四条原因：① 纯内存 + 无锁；② 一次 `epoll_wait` 批量取事件；③ RESP 协议简单 + 命令表 O(1)；④ 无线程切换与 cache line 抖动。

最关键的是 **②**：它决定了「一条线程能同时伺候多少连接」。10 万 QPS 不是 10 万个线程，而是一次 `epoll_wait` 带回几十个就绪 fd、每个 fd 上可能还有 pipeline；系统调用的固定成本被摊到几十条命令上。没有这一条，单线程连 1 万连接都伺候不了。

最常被误当成「因为内存」的是 **①**。内存只解释了「没有磁盘 IO」，但 memcached 也在内存里、也是多线程；真正的差异是 Redis 把「并发」整个拿掉了——没有锁、没有原子操作、没有跨核同步。「因为内存」最多解释了 10 μs 里的 1 μs 量级，解释不了架构选择。

6.0 的 IO 线程没有把上限翻 4 倍，因为它默认只分担回写 socket（读与解析还要 `io-threads-do-reads yes`），`call()` 与数据结构操作还在主线程。**它提高的是「网络密集」场景的吞吐（通常 1.5–2 倍），不是「命令密集」场景的上限**。当 CPU 100% 在 `call()` 里时，加 IO 线程只会多几个线程抢分配器。
</details>

**2. 100 万键的 `KEYS *` 约 200 ms。实例 10 万 QPS、客户端超时 100 ms，丢多少请求？如果超时是 1 s，丢 0 条，为什么还算事故？**
<details><summary>答案</summary>

丢包公式：`N_timeout = QPS × max(0, T_block − timeout)`。

- `T_block = 100 万 × 200 ns = 200 ms`；
- 超时 100 ms：`100,000 × (0.2 − 0.1) = 10,000` 条超时；
- 排队峰值（Little 定律）= `100,000 × 0.2 = 20,000` 条在途。

超时 1 s 时，没有请求「超时」，但 2 万条请求的等待时间被拉长到最多 200 ms，P99 从 8 ms 变成 200 ms——**SLO 是按分位定义的，不是按错误数定义的**。而且这 2 万条里有下单、支付、库存扣减，200 ms 的额外延迟会传导到上游线程池；上游超时更短（常见 50–100 ms），照样雪崩。所以「丢 0 条」只是「客户端没报错」，不是「没事故」。

补充：`KEYS` 的回复还有 34 MB，在 1 Gbps 上要 272 ms 排空；这 272 ms 里 output buffer 占着内存，慢客户端还会把它拖得更久。
</details>

**3. 1,000 条命令、RTT 0.2 ms、每条执行 10 μs：串行与 pipeline 各多少 ms？为什么 N 再大也超不过 21 倍？§01.4 说的「pipeline 后每命令 3–5 μs」和这里的 10.4 μs 矛盾吗？**
<details><summary>答案</summary>

串行：`1,000 × (0.2 + 0.01) = 210 ms`。
pipeline：`0.2 + 1,000 × 0.01 + 约 0.2 = 10.4 ms`，加速约 20×。
上限：`S = (RTT + exec) / (RTT/N + exec)`，N→∞ 时 `S → 1 + RTT/exec = 1 + 0.2/0.01 = 21`。

超不过 21 倍的原因是：**pipeline 只能把「每条一次 RTT」变成「整批一次 RTT」，但 `exec` 仍然是每条一次、且串行**。RTT 那 200 μs 被摊成 0 之后，剩下的下限就是 `N × exec`。

不矛盾。§01.4 的 3–5 μs 对应的是 100 条、`exec ≈ 2.5 μs` 的命令：`0.2 + 0.25 = 0.45 ms`，每条 4.5 μs。本篇的 10.4 μs 是 1,000 条、`exec = 10 μs` 的命令——**同一公式，不同 N 与 `exec`**。记住结论：pipeline 的收益由 `RTT/exec` 决定，命令越重、收益越小。
</details>

**4. 一个 10 GB 的 hash，`DEL` 与 `UNLINK` 的区别是什么？为什么 `UNLINK` 之后 RSS 不立刻降？如果此时正好 fork 做 BGSAVE，会发生什么？**
<details><summary>答案</summary>

`DEL` 在主线程同步走 `dictRelease`，逐个释放约 1 亿个 field；按 1 GB/s 或 100 ns/次估算都是 **10 s**，期间主线程不处理任何其他请求。`UNLINK`（4.0+）O(1) 把 key 从 keyspace 摘掉（后续读立刻看不到），把 value 塞进 `bio` 的 lazy free 队列，由后台线程释放，主线程阻塞降到几 μs。

RSS 不立刻降有两个原因：① 后台线程要花约 10 s 才释放完，`used_memory` 是逐步降的；② jemalloc 释放的页不一定立刻还给 OS，`mem_fragmentation_ratio` 可能先升后降。

此时 fork 做 BGSAVE 的风险：父进程 RSS 峰值 = 尚未释放的旧值 + 新写入；fork 的页表复制与 COW 都按这个更大的 RSS 算，`latest_fork_usec` 变长，COW 期间的内存压力更大。所以「先 UNLINK 再立刻 BGSAVE」是一个坏顺序，应该等 `used_memory` 降下来再 fork，或者错峰。
</details>

**5. `maxmemory` 设了 32 GB，策略是 `volatile-lru`，为什么还会 OOM？给出两条排查路径。**
<details><summary>答案</summary>

`volatile-lru` 只在**带 TTL 的 key**里选逐出对象。如果 32 GB 里大部分是无 TTL 的键（会话、队列、写漏了 `EXPIRE` 的缓存），候选集很小甚至为空，`performEvictions` 删不掉足够的内存，写命令就返回 `OOM command not allowed when used memory > 'maxmemory'`。

两条排查路径：

1. **看候选集**：`INFO keyspace` 的 `keys` 与 `expires`。如果 `expires` 远小于 `keys`，就是这个问题。`INFO stats` 的 `evicted_keys` 不涨、`total_error_replies` 涨，进一步确认「不是没逐出，是没得逐」。
2. **看是谁在占**：`MEMORY USAGE` + `--bigkeys` 找大 key；`CLIENT LIST` 的 `omem` 找输出缓冲；`MEMORY STATS` 看 `dataset.bytes` 与 `overhead.total`。有时候 `used_memory` 高不是数据，是碎片或客户端缓冲。

修复：把策略换成 `allkeys-lru`（纯缓存），或把无 TTL 的数据拆到 `noeviction` 的独立实例；同时给所有缓存写入强制 TTL。**根因不是策略选错，是「缓存与数据源混部」这个架构决定**——策略只是把它暴露出来。
</details>

---

## 12.15 延伸阅读

- 数据结构与编码阈值（listpack / hashtable / skiplist，大 key 的物理成因）：`→ comp/13`
- 持久化与主从同步（`fork`、COW、replication backlog，`latest_fork_usec` 的完整推导）：`→ comp/14`
- Cluster（16384 槽、MOVED/ASK、扩容迁移、跨槽多 key 为什么失败）：`→ comp/15`
- 无主 KV 的 quorum（`R + W > N` 买到了什么、买不到什么）：`→ comp/16`
- 缓存形态、失效语义、穿透/击穿/雪崩、热点 key 的识别与隔离：[architect/04 读路径与缓存形态](../architect/04-read-path-and-cache-shape.md)
- 存储选型：什么时候不该把 KV 当事务源：[eng/35 存储形态选型](../eng/35-storage-model-selection.md)
- LLM 平台的多级缓存（语义缓存、KV cache 与 Redis 缓存的分层关系）：[llm/24 缓存分层](../llm/24-cache-hierarchy.md)
- 八问卡与三本放大账的原始口径：[01-component-anatomy-method.md](01-component-anatomy-method.md)
- 本篇在全书中的位置与阅读路线：[0-summary.md](0-summary.md)
