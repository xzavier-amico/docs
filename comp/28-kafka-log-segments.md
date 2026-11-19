# 28. Kafka 的日志：segment、稀疏索引与零拷贝

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 消息、流与定时 ｜ **编号**：28 / 53
> **本篇定位**：第六部分的开篇，也是全书唯一一处把 **Kafka 的磁盘形态**画到「一个 offset 怎么落到某个文件的某个字节」这一层的篇目。它不讲 ISR、不讲 acks、不讲 rebalance（那些归 comp/29、comp/30），也不讲投递语义与幂等（那些归 eng/24），更不讲「选 Kafka 还是 Pulsar」（那是 architect/15）——它只回答一个问题：**一个只追加、切成若干 segment、带稀疏索引的文件目录，为什么能扛住百万级 msg/s，又为什么因此做不到「删单条、改一条、随机读」**。所有后面的边界（保留策略、compaction、零拷贝何时退化、为什么不能给堆开大内存）都是这一个形态的推论。
> **读法**：§28.1 的三起事故先建立「Kafka 没坏，是盘、延迟、顺序三件事一起出事」的直觉；§28.3 的两张 ASCII 图是全篇支点，看懂「offset 是逻辑位点、position 是字节偏移、索引是稀疏的」这三句话，后面每一节都只是加证据；§28.4 的四条「为什么快」要能自己复述到「省了哪两次拷贝、哪两次上下文切换」；§28.8 是唯一一处可复算的算账，评审时直接拿四本账说话。只想知道结论的读 §28.12。

---

## 28.1 引子：三起「Kafka 没坏，但盘、延迟、顺序三件事一起出事」的故障

Kafka 的故障有个统一特征：**进程活着、端口通着、消费组也能连上**。它表现成「生产端偶发超时」「消费端慢」「某块盘满了」，而根因往往在三个月前被写进一份 topic 配置：`log.segment.bytes` 调大了、堆调大了、客户端没升级。

### 事故一：`log.segment.bytes` 设成 8 GB，最老 segment 已过期 3 天还没被删（2026-01-19）

某审计事件流 `audit.events`：48 分区、RF=3、`retention.ms` 显式 7 天、日写入 60 GB（每分区 1.25 GB/day ≈ 14.5 KB/s）。集群是 3 broker × 2 块 1 TB NVMe（`log.dirs` 两个目录），水线按 85% 定。

一年前有人为了「减少文件数、让 `ls` 好看一点」，把 `log.segment.bytes` 从默认的 64 MB 量级改成 **8 GB**。于是每个分区的 segment 需要 `8 GB ÷ 1.25 GB/day ≈ 6.4 天` 才 roll 一次；而删除只会发生在 segment 这一级——**一个 segment 里的第一条消息，要等这个 segment 整体被判过期才能消失**。观测到的现场是：最老的那个 segment 里，头几条消息已经过期 3 天了，整段还在盘上。

| 时间 | 事件 | 最老段已过期 | 集群已用 | 最满单盘水位 | 写入状态 |
| --- | --- | --- | --- | --- | --- |
| 01-12 10:40 | 变更：`log.segment.bytes` 64 MB → 8 GB（目标：减少文件数） | 0 天 | 3.9 TB | 71% | 正常 |
| 01-16 03:00 | 冷分区仍在写同一个段（要 6.4 天才 roll），过期判断只能按整段走 | 0 天 | 4.1 TB | 74% | 正常 |
| 01-18 22:00 | 有效保留从 7 天滑到 10.2 天（7 天 + 平均半个段 3.2 天） | 1.4 天 | 4.7 TB | 82% | 正常 |
| 01-19 03:40 | 另两个 topic 同期扩容上线，最满盘跨过 90% | 2.8 天 | 5.3 TB | 91% | 生产端 P99 8 ms → 120 ms |
| 01-19 04:12 | `data2` 盘写满 → `KafkaStorageException` → 该盘上 11 个分区离线 | 3.0 天 | 5.4 TB | **96%** | 生产端 `TimeoutException` 1.2 万条/分 |
| 01-19 05:05 | `kafka-log-dirs.sh` 定位 + 手工删最老段 + 把 segment 调回 128 MB | — | 4.6 TB | 79% | 恢复 |
| 01-19 复盘 | 审计事件断流 53 分钟；下游合规对账缺 3 天数据，从对象存储归档重放 6 小时 | — | — | — | — |

**这一起事故的教训不是「配置改错了」，而是「容量评审按 7 天精确下单、没有留余量」。** 单看 audit 多留的 1.08 TB（逻辑 360 GB × RF=3），在任何一份容量表里都只是零头；但它落在了一块已经 84% 的盘上。日志形态决定了「删除的最小单位是段」，于是**保留策略的精度是「一天」还是「一周」，取决于 segment 多久填满一次**——这个数在容量评审里从来没被算过。

### 事故二：消费端落后 6 小时，扩容堆到 32 GB 反而更慢（2026-02-24）

某电商事件总线：6 broker × 128 GB 内存、`-Xmx12g`、日写入物理 2.7 TB（逻辑 900 GB × RF=3），即每 broker 450 GB/day ≈ **5.2 MB/s**。下游报表消费组因为一次上游促销叠加自身处理变慢，lag 从 40 分钟一路爬到 6 小时。

这里有一条几乎没人算过的分界线：**page cache 能覆盖的「历史深度」= 可用 page cache ÷ 写入速率**。`128 GB − 12 GB 堆 − 约 6 GB OS/agent ≈ 110 GB`，除以 5.2 MB/s ≈ **5.9 小时**。lag 一旦越过 5.9 小时，fetch 就从「命中页缓存的内存读」变成「每一条都要读盘」。

| 时间 | 事件 | 页缓存命中率 | 磁盘读 | fetch P99 |
| --- | --- | --- | --- | --- |
| 09:00 | lag 从 40 min 涨到 5 h 40 min | 96% | 40 MB/s | 28 ms |
| 11:20 | lag 6 h 05 min，**越过 5.9 小时的覆盖深度** | 12% | 1.6 GB/s | 180 ms |
| 11:40 | 6 台盘读带宽合计 2.1 GB/s，IO util 100% | 9% | 2.1 GB/s | 620 ms |
| 12:10 | 值班判断「内存不够」，堆 12 GB → 32 GB，滚动重启 | 0（重启清空页缓存） | 骤升 | 3.2 s（冷启动） |
| 12:40 | 页缓存只剩 90 GB（覆盖 4.8 h）；G1 混合收集停顿 30 ms → 800 ms | 11% | 2.0 GB/s | **900 ms** |
| 13:30 | 回滚堆到 8 GB + 消费端并发 12 → 48（→ comp/30）+ 削掉一次全量重跑 | 27% | 1.3 GB/s | 240 ms |
| 02-25 04:00 | lag 归零 | 94% | 35 MB/s | 20 ms |
| 复盘 | 6 小时 40 分内下游报表全部延迟，两条 SLA 违约，赔付 42 万元 | — | — | — |

**「Kafka 吃内存」这句话是错的：吃内存的是 OS 的 page cache，而 `-Xmx` 抢的正是它的地盘。** Kafka 的堆里全是短命对象（请求、响应缓冲），堆越大只是让 GC 的停顿更长、让 page cache 更小。

### 事故三：老客户端连新版 broker，零拷贝失效，网络吞吐掉 40% 而 CPU 反涨（2026-03-06）

某数据管道有 8 个 broker（10 GbE），出流量峰值 4.6 GB/s、单 broker 575 MB/s、CPU 22%。一批 2021 年遗留的客户端（旧消息格式）上线做历史回补，`fetch` 请求声明的版本低于 broker 的 `log.message.format.version`，broker 于是对它做 **down-conversion：把批读进用户态、逐条重写、再发出去**。

零拷贝的前提是「这一批字节不需要被改动」。一旦需要改动，`sendfile` 就用不上了，退化成「read + write」——数据被多搬两次、上下文多切两次，CPU 直接进数据通路。

| 时间 | 事件 | 单 broker 出流量 | CPU | fetch P99 |
| --- | --- | --- | --- | --- |
| 03-06 10:00 | 稳态，全量零拷贝 | 575 MB/s | 22% | 20 ms |
| 10:20 | 旧客户端接入，回补 group 开始拉历史数据 | 520 MB/s | 31% | 32 ms |
| 10:50 | 回补 group 占出流量 45%，全部走 down-conversion | 410 MB/s | 49% | 70 ms |
| 11:30 | 消费更慢 → 回补窗口拉长 → CPU 成新瓶颈（单核约 80 MB/s 转换能力） | **345 MB/s** | 58% | 110 ms |
| 12:05 | 触发 broker 网络线程排队，正常业务 group 一起变慢 | 340 MB/s | 61% | 240 ms |
| 12:40 | 关停回补、把旧客户端换成新版（或走镜像 topic 预转格式） | 570 MB/s | 24% | 22 ms |
| 复盘 | 出流量掉 40%、CPU 涨 2.6 倍；同期消费者 lag 峰值 2.3 小时 | — | — | — |

三次事故分别对应本篇三节：**段与过期（§28.6）**、**page cache 与堆（§28.4）**、**零拷贝的退化条件（§28.7）**。

---

## 28.2 一句话定位与唯一真问题

**定位**：Kafka 的一个 partition = **一个只追加的日志目录**，目录里是若干 segment（`*.log` 数据 + `*.index` 偏移索引 + `*.timeindex` 时间索引），每条消息有一个**逻辑 offset** 和一个**物理 position**，索引是**稀疏的**。它不是队列、不是 KV、不是内存缓冲——它是一个「磁盘上的顺序日志，缓存交给操作系统」。

### 唯一真问题

> **Kafka 快的真实原因是「只追加 + 顺序 IO + page cache + sendfile」这套磁盘形态——而同一套形态也直接决定了它不能删单条、不能改、不能随机读；所以「Kafka 能做到什么」不是产品功能表决定的，是日志形态决定的。**

四条推论，全篇都在给它们找证据：

1. **快不是因为它「聪明」，是因为它「不做」。** 不做原地更新、不做随机读、不自己管缓存、不逐条处理（批量 + 零拷贝）。每一项「不做」都换掉了一次昂贵操作，也换掉了一种能力。
2. **删除的最小单位是段，不是消息。** 想要「删单条 / 只留每个 key 的最新值」，只能把形态换成 compaction（后台重写段），代价与 LSM 的 compaction 同源。
3. **缓存在 OS 手里，不在 JVM 手里。** 堆越大，page cache 越小，读路径越慢——这条推出「Kafka 堆要小」这条反直觉纪律。
4. **零拷贝是一条有前提的快路径，不是默认行为。** 同格式、走 socket、不做逐条处理；任一不成立，数据就回到用户态，CPU 进数据通路。

**它明确不承诺什么**：不承诺「删了就降盘」（要等段过期）、不承诺「按 key 查最新值」（那要 compaction）、不承诺「随机读某一条」（只能 seek 后顺序推进）、不承诺「消费慢会撑爆内存」（它只会在磁盘上堆着，和内存无关）。

---

## 28.3 物理形态：offset、segment 与稀疏索引

### 一个 partition 就是一个目录

```text
一个 partition = 一个目录（目录名 = <topic>-<partition>）

/var/lib/kafka/data/audit.events-3/
├── 00000000000000000000.log          ← 数据。baseOffset = 0（已关闭）
├── 00000000000000000000.index        ← offset → position（8 B/项，稀疏）
├── 00000000000000000000.timeindex    ← timestamp → offset（12 B/项，稀疏）
├── 00000000000003687690.log          ← baseOffset = 36,876,900
├── 00000000000003687690.index
├── 00000000000003687690.timeindex
├── 00000000000007375380.log          ← 正在写的「活跃段」(active segment)
├── 00000000000007375380.index
├── 00000000000007375380.timeindex
└── leader-epoch-checkpoint           ← 每个分区目录一份，记录该分区历任 leader 的纪元

文件名就是该段第一条消息的 offset（20 位零填充）：
  段名 = baseOffset  ⇒  「找 offset 在哪一段」= 「在一组有序的长整数上二分」
  段内相对位置 = offset − baseOffset  ⇒  换段不影响 offset 的连续性
```

三个结构事实，后面所有结论都由它们推出：

- **每个 segment 是一段连续字节**，追加就是 `write()` 到活跃段末尾。Kafka 从不在段中间写。
- **段一旦 roll（写满 `log.segment.bytes` 或到 `log.roll.ms`/`log.roll.hours`），就只读**。删除、清理、compaction 都以段为单位作用于已关闭的段。
- **索引是稀疏的**：不是每条消息一条索引项，而是「每隔 `index.interval.bytes`（默认 4 KB 量级，以目标版本配置为准）的数据记一条」。索引文件大小 ≈ 数据大小 ÷ 该间隔 × 8 B ≈ 数据的 **0.2%**。

### offset 怎么找到物理位置（本篇的支点图）

```text
消费端要读 offset = 73,800,123

① 选段（纯内存，0 次 IO，~1 μs）
   baseOffsets = [0, 36,876,900, 73,753,800]      ← 有序数组
   二分找「≤ 73,800,123 的最大者」→ 73,753,800
   ⇒ 目标在活跃段 00000000000007375380.log

② 查偏移索引（mmap 上的二分，通常只碰 1–2 个 4 KB 页，2–10 μs）
   .index 条目 = (relativeOffset: 4 B, position: 4 B)，稀疏
   段内目标 relative = 73,800,123 − 73,753,800 = 46,323
   索引中相邻两条可能是：… (46,000 → pos 6,742,016) (46,032 → pos 6,872,320) …
   二分找「≤ 46,323 的最大条目」→ 46,032 @ position 6,872,320
   代价：最多向后顺序扫一个索引间隔（默认 4 KB 量级）

③ 从 position 顺序扫（≤ 一个索引间隔 + 一个批的长度）
   读批头（批头自述长度与 baseOffset/lastOffsetDelta）
   若 baseOffset + lastOffsetDelta ≥ 46,323 → 命中，解出这条消息
   否则整批跳过 —— 不需要解析批里的每一条

两个位点，两种语义，别混：
   offset   = 逻辑序号，分区内单调 +1，跨段连续，与文件无关
   position = 段内字节偏移，换段就归零，只在 .index 里出现
```

| 一次读取的分解 | 动作 | 命中缓存 | 未命中（NVMe） | 未命中（机械盘） |
| --- | --- | --- | --- | --- |
| 选段 | 内存数组二分 | ~1 μs | 同左 | 同左 |
| 索引二分 | mmap 上 1–2 页 | 2–10 μs | ~80 μs | 5–8 ms |
| 顺序扫 | ≤ 4 KB 量级 + 一个批 | ~5 μs | ~80 μs | 5–8 ms |
| 数据传输 | `sendfile`（→ §28.7） | 与字节线性 | 同左 | 同左 |
| 网络 | 同 AZ RTT | 0.3–1 ms | 同左 | 同左 |

**关键结论：Kafka 的「随机读一条」= 1 次索引二分 + 最多 4 KB 的顺序读。** 它不是不能定位，而是定位完之后**只能顺着往下读**——没有「跳到文件中段某条记录」这个动作，因为段内没有按消息粒度的索引。

### 一次写入走哪条路

| 跳 | 动作 | 量级 |
| --- | --- | --- |
| ① producer → leader | 网络传输 + 批的 CRC 校验 | 同 AZ 0.3–1 ms |
| ② leader 追加 | 一次 `write()` 把整批写进活跃段末尾，落在 **page cache** | 5–50 μs（不是落盘） |
| ③ 写索引项 | 每 `index.interval.bytes` 才写一条 8 B 项，mmap 上写 | 摊薄后 ≈ 0 |
| ④ 副本 fetch | follower 主动拉，再写自己的 page cache | 与 ② 同量级，异步 |
| ⑤ `acks=all` 返回 | 等 ISR 中最慢的副本也写进它自己的 page cache | ≥ 1 个 RTT（→ comp/29） |
| ⑥ 回刷到盘 | OS 后台（`dirty` 页回刷） | 秒级，**不在写路径上** |

**第 ⑥ 跳不在写路径上，是整套设计最值钱的一句话**：生产者看到「写成功」时，数据大概率还在内存里；它之所以仍然安全，靠的是副本（→ comp/29），而不是 fsync。这也解释了为什么「Kafka 单机可以吃到盘带宽的上限」——它把最慢的那一步交给了 OS 的后台线程。

---

## 28.4 为什么快：只追加、page cache、零拷贝、批量

四件事，缺任何一件这套形态都不成立。它们的共同点是：**每一件都在「避免某次昂贵操作」，而不是「把某次操作做得更快」**。

| # | 机制 | 避免了什么 | 直接后果 | 它的代价 |
| --- | --- | --- | --- | --- |
| ① | **只追加** | 原地更新 → 随机 IO、读-改-写、页分裂 | 顺序写吃满盘带宽，无写放大 | **不能删单条、不能改** |
| ② | **page cache** | 自己管缓存（淘汰算法、对象管理、GC） | 读命中内存，写只写内存 | 缓存容量由 OS 定，**不能靠 `-Xmx` 拿** |
| ③ | **sendfile** | 数据在用户态的两次拷贝与两次上下文切换 | 出流量逼近网卡上限，CPU 不参与 | **只在「不用改字节」时成立** |
| ④ | **批量 + 压缩** | 每条一次系统调用、一次网络往返、一次索引项 | 系统调用与网络开销被除数摊薄 | 攒批带来 ms 级延迟（`linger.ms`） |

### ① 只追加：Kafka 是「只追加但从不重写」的第三种形态

存储引擎三形态（→ comp/02 §02.4）里，B+ 树是原地改，LSM 是只追加但**后台 compaction 会把同一份数据反复重写** `T×L` 次。Kafka 是第三种：**只追加、正常路径下从不重写**。所以它的写放大在第 ①栏接近 1×——你写 1 GB，盘上就是 1 GB（乘副本数），没有 compaction 的周期性重写。

代价同样干净：**形态里没有「改一个字节」这个动作**。想按 key 只留最新值，只能显式打开 `cleanup.policy=compact`，此后它就变成了一个「会重写段的 LSM 变体」，§02.6 那本 compaction 账就重新生效。

### ② page cache：Kafka 自己不管理缓存，把缓存交给 OS

写路径上，批从 socket 读到用户态缓冲，再 `write()` 进页缓存——**数据在 page cache 里就返回了**。读路径上，如果命中 page cache，`sendfile` 直接从页缓存推到 socket，**一个字节都不进 JVM 堆**。

这推出三条硬纪律：

- **不要给 Kafka 堆开大内存**。堆里装的是请求对象、响应缓冲、分区状态，不是消息体；消息体在页缓存里，而页缓存是「剩下的所有内存」。`-Xmx` 每多 1 GB，页缓存就少 1 GB。
- **「Kafka 吃内存」的真相是 OS 在吃**。`free` 里看到 `buff/cache` 巨大、`available` 很小，是**正常且期望**的。
- **落后多少以内是「内存读」，可以算**：覆盖深度 = 可用 page cache ÷ 写入速率（事故二的 5.9 小时就是 110 GB ÷ 5.2 MB/s）。

### ③ sendfile / 零拷贝：省的是两次拷贝与两次上下文切换

```text
【普通读：read() + write()】
  盘 ──DMA──► page cache ──CPU 拷贝──► 用户态 buffer ──CPU 拷贝──► socket buffer ──DMA──► NIC
                                  ①                          ②
  代价：2 次 DMA + 2 次 CPU 拷贝 + 4 次上下文切换；CPU 全程参与搬运

【sendfile：数据不进用户态】
  盘 ──DMA──► page cache ══════DMA（scatter-gather）══════════════════════════► NIC
                  ▲                                    ▲
                  └── 只有「fd + 偏移 + 长度」被交给内核 ┘
  代价：2 次 DMA + 0 次 CPU 拷贝 + 2 次上下文切换；大报文时 CPU 基本不动

Kafka 的 fetch 路径：
  consumer fetch(offset, maxBytes)
    → 在 .index 上定位得到 (position, length)
    → FileRecords.transferTo(socketChannel)   ⇒ sendfile
    → 4.6 GB/s 出流量下 CPU 只有 22%（§28.1 事故三的基线）
```

**只有「不需要转换格式」的消费路径才能零拷贝。** 一旦 broker 要把批读进来改（down-conversion、逐条校验、时间戳重写、TLS 加密），数据就必须进用户态，这条快路径直接失效（展开见 §28.7）。

### ④ 批量 + 压缩：压的是「批」，不是「条」

- **producer 端攒批**：`batch.size`（默认 16 KB 量级）+ `linger.ms` 决定一批有多大。一批 = 一次 `send()`、一次网络往返、一次 `write()`。
- **broker 按批存储与发送**：段里存的是一段连续的批字节，索引项指向**批的边界**；消费端一次 `max.partition.fetch.bytes` 就能拿走很多批。
- **压缩在批上做**：`compression.type=lz4/zstd`，broker **不解压**（除格式转换），直接把压缩后的字节存进段、发给消费者，消费者自己解。所以**压缩不破坏零拷贝**——这一点与很多人的直觉相反。
- **压缩比由「批内相似度 × 批大小」决定**：同一批里是同一租户、同一段时间的事件，相似度高；`linger.ms` 从 0 调到 20 常常带来 2–3 倍的批大小提升，压缩率与盘占用一起下降。

| 攒批参数 | 它改的是哪条路径 | 调大的收益 | 调大的代价 |
| --- | --- | --- | --- |
| `batch.size` | 一批的字节上限 | 系统调用、网络往返、索引项被摊薄 | 内存占用（每分区一个缓冲） |
| `linger.ms` | 最多等多久凑批 | 批更满 → 压缩率与吞吐上升 | 端到端延迟 +最多 linger |
| `compression.type` | 盘上字节数 | 盘占用与网络字节同比例下降 | producer CPU；压缩比依赖批内相似度 |
| `max.partition.fetch.bytes` | 一次 fetch 取多少 | fetch 次数少、顺序读更长 | 单次响应内存；过大反而拖慢其他分区 |

---

## 28.5 由此推出的四条硬边界：不能删、不能改、不能随机读、不是内存队列

这四条不是「还没做」，是**形态里不存在**。想绕，就必须换形态或换零件。

| 边界 | 物理原因 | 能做什么（同一个形态内） | 靠什么绕过（换了什么） |
| --- | --- | --- | --- |
| **不能删单条** | 删除的实现是「整段 unlink」；段内没有删除标记，也没有按消息的索引 | 按时间/大小整段过期（`delete` 策略）；按 key 保留最后一条（`compact` 策略） | `cleanup.policy=compact`：**后台重写段**，保留每个 key 的最后一条 —— 代价是重写写放大 + 空间回收滞后（§28.6）；或另建一个「结果 topic」由消费者投影 |
| **不能改** | 段一旦写入即不可变，任何「修改」都是覆盖旧字节 | 追加一条新消息（同 key 的新版本），由消费端读时合并 | 同 compaction，或把「状态」放到真正的 KV/DB 里（→ architect/15 的「日志 vs 可编辑状态」） |
| **不能随机读** | 消费位点只能单调推进；段内没有按消息粒度的索引 | `seek(offset)` / `seekToTimestamp()` 定位后**顺序**往下读；`auto.offset.reset` 决定起点 | 想按任意维度取数，就把它落到列存/检索/向量库（→ comp/20、comp/21），Kafka 只做「变更通知」 |
| **不是内存队列** | 它是「磁盘顺序日志 + page cache」；未消费的数据不在内存里等着，而在盘上 | 消费者落后 → 数据在盘上堆着，只是读的时候不命中缓存而变慢 | 想「消费慢就 OOM 逼你扩容」的语义，那是内存队列（Redis Streams / SQS 的可见性超时，→ comp/32） |

**这四条边界解释了两个最常见的误用：**

- **把 Kafka 当 KV 存「最新状态」**：你想要的是 `GET key → latest`，而 Kafka 只能给你「从某个 offset 开始顺序扫一遍」。用 `delete` 策略的 topic 做这件事，等于每次查询都要全表扫；用 `compact` 策略，才能把 topic 近似压成一张「每 key 一行」的表——但读它仍然是一次**顺序全扫**（Kafka 不提供点查 API）。
- **把 Kafka 当「可回溯的数据库」**：回溯重放确实是它的强项（同一段数据可以被多个 group 独立按位点重放），但重放的代价是**顺序扫 + 网络传输**；一次全量重放 5 TB 的 topic，按 500 MB/s 算至少 3 小时，这期间它是「在读盘」而不是「在服务点查」。

---

## 28.6 保留与清理：删除是 segment 级的事件

### 五个参数，各改哪条物理路径

| 参数 | 默认量级（以目标版本配置为准） | 它改的是哪条路径 | 拧错的现场 |
| --- | --- | --- | --- |
| `retention.ms` | 7 天量级 | 段被判定为「可删」的时间基准（对 `delete` 策略按段内最大时间戳判） | 设成 `-1` = 永不删，盘必满（→ comp/23 的坑 8） |
| `retention.bytes` | `-1`（不启用） | 按分区总字节判可删，与时间取「先到先得」 | 与 `retention.ms` 同时生效时，先到的那条先删，容易被误以为「保留没生效」 |
| `log.segment.bytes` | 1 GB 量级（默认 1 GB 左右） | 段多大才 roll → **决定过期精度与文件数** | 太大 → 过期滞后；太小 → 文件数与句柄爆炸 |
| `log.roll.ms` / `log.roll.hours` | 未设（仅按大小时为 7 天量级兜底） | 即使没写满，到时间也 roll | 冷分区不 roll → 段永不判过期（事故一） |
| `log.retention.check.interval.ms` | 5 分钟量级 | 后台扫描目录、执行删除的周期 | 只影响「判过期」的延迟，不改变过期精度 |

### 为什么「segment 太大」的代价要按「最慢的分区」算

删除的判定单位是段：一个段里最新的那条消息都过期了，整段才会被删。所以：

```
有效保留窗口 ≈ retention + [0, 段填满所需时间]
段填满所需时间 = log.segment.bytes ÷ 该分区的写入速率
```

对最忙的分区，这个附加项可以忽略；对最冷的分区，它可以是保留期的数倍。事故一里 14.5 KB/s 的分区，8 GB 段要 6.4 天才填满——**同一个 topic 里，热分区的过期精度是分钟级，冷分区是周级**。

```text
分区速率 2.3 MB/s（一天 200 GB），retention 7 天

  segment 1 GB ──► 每 432 s roll 一个
     稳态段数 ≈ 1,400；「已过期未删」最多 1 段 = 1 GB（名义保留的 0.07%）
     峰值盘占用 = 7 × 200 GB + 1 GB = 1,401 GB
     文件数 ≈ 1,400 × 3 = 4,200

  segment 4 GB ──► 每 1,728 s roll 一个
     稳态段数 ≈ 350；「已过期未删」最多 1 段 = 4 GB（0.29%）
     峰值盘占用 = 1,400 GB + 4 GB = 1,404 GB
     文件数 ≈ 350 × 3 = 1,050

删除永远发生在「段」这一级：
  时间轴 ──►  [段 A 已过期] [段 B 已过期] [段 C 还没过期] [段 D 活跃·正在写]
                    ▲ 整段 unlink
                                   ▲ 即使段 C 里前 90% 的消息都过期了，
                                     也必须等段 C 整体到期才能一起删
  ⇒ 「删一条」不存在；「删一天」也只在你恰好按段对齐时才近似成立
```

| 一天 200 GB 的分区（2.3 MB/s） | segment 1 GB | segment 4 GB | segment 8 GB |
| --- | --- | --- | --- |
| 段填满耗时 | 7.2 分钟 | 29 分钟 | 58 分钟 |
| 有效保留（7 天 + 平均半个段） | 7.00 天（+0.04%） | 7.01 天（+0.14%） | 7.02 天（+0.29%） |
| 峰值盘占用 | 1,401 GB | 1,404 GB | 1,408 GB |
| 稳态段数 / 文件数 | 1,400 / 4,200 | 350 / 1,050 | 175 / 525 |

**这张表说明：对忙碌的分区，「段太大」几乎无害；真正的杀伤发生在冷分区上。** 同样是 8 GB：

| 一天只写 1 GB 的冷分区（11.6 KB/s） | segment 64 MB | segment 1 GB | segment 8 GB |
| --- | --- | --- | --- |
| 段填满耗时 | 1.5 小时 | 24 小时 | 8.0 天 |
| 有效保留（名义 7 天） | 7.03 天 | 7.5 天 | **11.0 天（+57%）** |
| 一个段内的数据最多滞留 | 1.5 小时 | 1 天 | 8 天 |

**工程口径**（可直接写进 topic 规范）：`log.segment.bytes` 要按**最慢分区**的速率定，让冷分区也在 1–6 小时内 roll 一次；等价地 `log.segment.bytes ≈ 最慢分区速率 × [1h, 6h]`，并对极小流量的分区额外设 `log.roll.hours`（比如 6 小时）兜底。**只按最忙分区定段大小，是事故一的成因。**

### compaction（`cleanup.policy=compact`）的账单

| 项 | 内容 |
| --- | --- |
| 物理动作 | 后台把段读出来、按 key 归并、**只保留每个 key 的最后一条**、写一个新段、替换旧段 |
| 买到什么 | 「每个 key 一行」的近似表；可当 changelog / 状态快照 / 维表广播 |
| 写放大 | 被清理的段会被重写至少一次；脏段越多、重写越多（与 §02.4 的 compaction 同源） |
| 空间放大 | 回收滞后：墓碑（tombstone）与旧版本在清理完成前一直占盘；`delete.retention.ms` 决定墓碑留多久 |
| 读的代价 | 消费它仍然是**顺序全扫**；「点查某个 key」要扫完或靠消费端自己建索引 |
| 什么时候别用 | 每条 key 只写一次（无重复可清）、或需要按时间窗口过期（该用 `delete`） |

---

## 28.7 零拷贝什么时候退化

零拷贝是一条**有前提的快路径**，不是默认行为。三条前提，任一不成立就退化：

| 前提 | 成立时的路径 | 不成立时退化成 | 现实中的触发条件 |
| --- | --- | --- | --- |
| ① **同格式**：消费者要的就是段里存的字节 | `sendfile` 直出 | 读进堆 → 逐条重写批头 → 再发（2 次 CPU 拷贝 + 解析 + 重新计算 CRC） | 旧客户端（低 `fetch` 版本）触发 down-conversion；Kafka 4.0 起按 KIP-724 移除了旧消息格式，这条路在 4.x 上已消失，3.x 及以前仍在 |
| ② **走 socket 发送**：数据从页缓存直接进 socket | `sendfile` | 需要落成别的形态（例如要按条处理）时，只能 `read` 到用户态 | 拦截器/逐条校验/时间戳重写等 broker 侧改造 |
| ③ **不需要逐字节处理** | CPU 不进数据通路 | TLS 加密必须在内核把数据交给网卡之前、在用户态完成 → `sendfile` 用不上 | 开启 SSL/SASL_SSL 的 listener；具体退化成几次拷贝取决于目标版本的 TLS 实现（JDK `SSLEngine` 还是内核 TLS 卸载），**以目标版本文档为准** |

```bash
# 零拷贝有没有生效：三个命令交叉验证（在 broker 上跑）
# ① syscall 侧：出现 sendfile 且单次 count 接近 fetch 大小 = 零拷贝在生效
sudo timeout 10 strace -f -c -e trace=sendfile,read,write -p "$(pgrep -f 'kafka.Kafka')"
# ② 若 sendfile 几乎为 0、read 暴涨 → 数据在往用户态搬，去看是不是格式转换/TLS
# ③ 吞吐形状：磁盘读吞吐 ≈ 网卡发送吞吐，且 CPU 不随之线性上涨，才是零拷贝应有的样子
cat "/proc/$(pgrep -f 'kafka.Kafka')/io" | grep -E 'read_bytes|write_bytes'
sar -n DEV 1 5 | grep -E 'IFACE|eth|bond'
# ④ 版本相关：down-conversion 的痕迹在 server.log 里（字段名随版本变化，以目标版本为准）
sudo grep -ci 'down-conversion\|convert.*message.*format' /var/log/kafka/server.log
```

**判定口径（值班时用）：`BytesOutPerSec` 掉、`BytesInPerSec` 不变、CPU 却涨 → 八成是零拷贝退化。** 这不是「网络变差」，是「数据进了用户态」。修法按代价排序：升级客户端 > 把旧格式数据用镜像 topic 预先转好 > 给这类 group 单独限流隔离。

---

## 28.8 三本账：顺序 IO、存储放大与保留窗口

四个算例，每个都带假设清单和「量级结论 / 承诺值」标记。

### 账一 · 顺序追加 vs 随机 4K 写：差 170–400 倍

| 设备 | 顺序写 | 随机 4 KB 写 | 有效吞吐 / 顺序吞吐 |
| --- | --- | --- | --- |
| 机械盘（7.2k rpm，标称顺序 ~200 MB/s） | 200 MB/s | ~150–300 IOPS（寻道 4–8 ms + 平均旋转延迟 4.2 ms ≈ 10 ms/次） | `300 × 4 KB = 1.2 MB/s` → **1/170** |
| 企业级 SATA SSD（标称 500 MB/s 顺序） | 500 MB/s | 20k–50k IOPS（受 QD 影响大） | `30k × 4 KB = 120 MB/s` → **1/4** |
| NVMe（3.5 GB/s 顺序，QD32 随机写） | 3,500 MB/s | 200k–400k IOPS | `300k × 4 KB = 1.2 GB/s` → **1/3** |

推导式只有一条：**随机小写的有效吞吐 = IOPS × 4 KB，而 IOPS 的上限由「每次寻道/每笔事务的开销」决定，与带宽无关。**

代进「同样标称 500 MB/s 的盘，顺序追加 vs 随机 4K」的对照：顺序追加能跑到满速 500 MB/s，随机 4K 只有几百 IOPS → 1–1.2 MB/s，**差 170–400 倍**。按 1 KB 一条消息算：500,000 条/s vs 1,200 条/s。

> **量级结论（非承诺值）**。假设：① 机械盘寻道 4–8 ms、平均旋转延迟 4.2 ms；② SSD/NVMe 的 IOPS 取厂商典型值区间、QD32；③ 消息 1 KB。**换到 NVMe 上，这道差距会从 170× 缩到 3×——但这不削弱只追加的价值，它只是把优势从「IO 形状」转移到了「无写放大」这一条**：Kafka 写 1 GB 就是 1 GB，而 LSM 要写 35–57 遍、B+ 树随机主键要写 110 遍（→ comp/02 §02.6）。所以「在 NVMe 上 Kafka 为什么还快」的正确答案是 §28.4 的第 ① 与第 ② 条，不是寻道。

### 账二 · 一条消息的存储开销：每 1 GB 业务数据占 3.0–3.4 GB 盘

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 单条头 | 单条记录头由约 5 个 varint 字段组成（长度、attributes、时间戳差、offset 差、key/value 长度），`8–14 B`，取 12 B | 12 B/条 |
| 2 | 批头摊薄 | 批头是源码常量 `RECORD_BATCH_OVERHEAD`（61 B 量级），摊到 1,000 条/批 | 0.06 B/条 |
| 3 | 偏移索引 | 每 `index.interval.bytes`（默认 4 KB 量级）一条 8 B 项 | 数据的 0.20% |
| 4 | 时间索引 | 同一间隔一条 12 B 项 | 数据的 0.29% |
| 5 | 逻辑放大（1 KB 消息） | `(1024 + 12.06) / 1024 + 索引 0.49%` | **1.017×** |
| 6 | 逻辑放大（100 B 小消息） | `(100 + 12.06) / 100 + 0.49%` | **1.125×** |
| 7 | 逻辑放大（8 KB 消息） | `(8192 + 12.06) / 8192 + 0.49%` | **1.006×** |
| 8 | × RF=3 | 步骤 5/6/7 × 3 | **3.05 / 3.37 / 3.02 GB 盘 / 1 GB payload** |

**加一项常被忽略的固定成本**：每个分区有一个活跃段，它的 `.index` 与 `.timeindex` 在建文件时按 `log.index.size.max.bytes`（默认 10 MB 量级，以目标版本配置为准）预分配，roll 之后才裁到实际大小。所以**每分区约 20 MB 与数据量无关的固定开销**——10,000 个分区就是 ~200 GB（多数文件系统上表现为稀疏文件，但 mmap 地址空间与 `du` 口径都按全量算）。这是「分区数不是免费的」最硬的一条依据。

> **量级结论（非承诺值）**。假设：① payload 1 KB、key/header 忽略不计；② 每批 1,000 条（批头摊薄 0.06 B/条）；③ `index.interval.bytes` 取 4 KB 量级；④ RF=3、不计 segment 尾部未写满的空洞、不计文件系统开销。**换任一假设请按步进表重算**：批只有 10 条/批时，批头变成 6.1 B/条，1 KB 消息的逻辑放大从 1.017 涨到 1.023（+0.6%）——**小批的代价远小于小消息的代价**，因为头部是每条一份、批头是一批一份。

### 账三 · page cache、`dirty_ratio` 与「一台 64 GB 机器该怎么分」

写入 200 MB/s 时，脏页的积累与回刷是这样的：

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | `dirty_background_ratio`（默认 10% 量级）在 64 GB 机器上 | 64 GB × 10% | 6.4 GB：超过它，内核**开始后台回刷** |
| 2 | `dirty_ratio`（默认 20% 量级） | 64 GB × 20% | 12.8 GB：超过它，**写入线程被强制同步等待** |
| 3 | 以 200 MB/s 涨到后台线 | 6.4 GB ÷ 200 MB/s | 32 秒 |
| 4 | 涨到硬线 | 12.8 GB ÷ 200 MB/s | 64 秒 |
| 5 | 回刷必须跟得上 200 MB/s | 盘需要「写入 200 + 回刷 200」的余量 | 盘带宽 ≥ 400 MB/s，否则脏页单调上涨 |
| 6 | 触硬线的表现 | 生产者 `write()` 被 sleep | P99 从 8 ms 跳到 200–800 ms，而 CPU 不高、盘 100% |

这就是「Kafka 突然变慢、CPU 不高、磁盘 busy」的经典形状：**瓶颈不在 Kafka，在 `balance_dirty_pages`**。它也是「Kafka 的写入延迟不可承诺」的物理来源——真正落盘的时刻由内核决定，不由 broker 决定。

内存分配的依据（64 GB 机器的推荐形状）：

| 用途 | 大小 | 依据 |
| --- | --- | --- |
| Kafka 堆（`-Xmx`） | **6 GB** | 堆里是请求/响应缓冲与分区状态；经验上「几万分区 + 常规请求速率」在 6 GB 量级足够，且堆越大 GC 停顿越长（事故二：32 GB 堆的混合收集停顿从 30 ms 涨到 800 ms） |
| JVM 堆外（direct buffer、mmap） | 2–4 GB | 网络收发缓冲、索引 mmap（索引本身不走堆） |
| OS 与 agent | ~2 GB | 页表、文件系统、采集器 |
| **page cache** | **≈ 50 GB** | 剩下全给它；`50 GB ÷ 200 MB/s = 250 秒` 的历史数据全是内存读 |

**容器里还有一个更隐蔽的坑**：K8s 的 `memory.limit` 会把 page cache 一起算进 cgroup。若把 limit 设成 8 GB（堆 6 GB + 2 GB 余量），page cache 只剩 ~2 GB，覆盖深度从 250 秒掉到 10 秒——**「Kafka 一上容器就慢」的根因通常在这里，不在 CPU**。

> **量级结论（非承诺值）**。假设：① `vm.dirty_background_ratio=10`、`vm.dirty_ratio=20`（发行版默认量级，需在目标机上 `sysctl -a | grep dirty` 核对）；② 写入 200 MB/s 且持续；③ 单块盘、无 RAID 写放大；④ 堆 6 GB 对分区数在万级以内够用。**换机型请重算第 3/4 步的秒数，并核对 cgroup 视图**：容器里真正该看的是 `memory.stat` 的 `file` 字段，不是 `/proc/meminfo`。

### 账四 · 保留窗口的乘法链：3 个 topic × 100 MB/s × 7 天

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 单 topic 日增量 | 100 MB/s × 86,400 s | 8.64 TB/天 |
| 2 | 3 个 topic | 8.64 × 3 | 25.92 TB/天（逻辑） |
| 3 | 保留 7 天 | 25.92 × 7 | **181.4 TB 逻辑** |
| 4 | RF=3（物理副本） | 181.4 × 3 | **544 TB 物理** |
| 5 | 15% 余量（过期滞后 + 段尾 + 不均衡 + 迁移峰值） | 544 × 1.15 | **≈ 626 TB** |
| 6 | 分到 broker（12 台） | 626 ÷ 12 | 52 TB/broker |
| 7 | 每 broker 盘阵 | 12 块 8 TB JBOD = 96 TB 裸，水线 75% → 72 TB 可用 | 72 > 52 ✓（余量 38%） |
| 8 | 写入带宽复核 | 25.92 TB/天 ÷ 86,400 = 300 MB/s 逻辑；× 3（RF）= 900 MB/s 物理 | 每 broker 75 MB/s，每盘 6.25 MB/s —— **盘完全不紧张** |
| 9 | 反向验算：改成保留 30 天 | 626 × 30/7 | **≈ 2.68 PB → 需要约 52 台**或改分层存储（→ comp/42、comp/35） |

第 8 步是这张表最容易被忽略的一格：**保留窗口的瓶颈从来不是写带宽，是容量。** 900 MB/s 分散到 12 台机器只有 75 MB/s/台，而容量需求却是 626 TB。所以「Kafka 扩容」几乎总是**加盘量**，而不是加盘速——这也解释了为什么 HDD + JBOD 在 Kafka 上依然是主流（顺序写对 HDD 友好到几乎没有惩罚）。

> **量级结论（非承诺值）**。假设：① 三个 topic 速率恒定 100 MB/s、峰值不超过均值的 2 倍（峰值靠攒批与 partition 吸收）；② RF=3；③ 余量 15%；④ 12 broker × 12 × 8 TB JBOD、水线 75%（`log.dirs` 多目录均分）。**这是容量下限量级，不是采购报价**：换副本数、换水线、换分层存储都会让结果在 ±30% 浮动；第 9 步的 30 天反向验算是「承诺值性质」的结论——**保留期每翻一倍，盘就翻一倍，没有捷径。**

```python
# 把本篇四本账压成一个可复算的脚本（改任一假设直接重跑）
GB, TB = 1e9, 1e12

def per_gb_payload(rf=3, rec=1024, hdr=12, batch=1000, batch_hdr=61, idx_interval=4096):
    """每 1 GB 业务 payload 实际占几 GB 盘：单条头 + 批头摊薄 + 稀疏索引 + 副本"""
    n   = GB / rec
    head = n * (hdr + batch_hdr / batch)      # 每条一份 + 每批一份摊薄
    idx  = GB / idx_interval * (8 + 12)       # .index 8 B + .timeindex 12 B
    return (GB + head + idx) * rf / GB

def retention_tb(rate_mb_s, topics, days, rf=3, slack=0.15):
    """保留窗口乘法链：速率 → 日增量 → ×topic → ×天数 → ×副本 → ×余量"""
    day_tb  = rate_mb_s * 86400 / 1e6
    logical = day_tb * topics * days
    return logical, logical * rf, logical * rf * (1 + slack)

def seg_fill_min(rate_mb_s, seg_gb):
    """一个段要多久才填满（决定过期精度）"""
    return seg_gb * 1024 / rate_mb_s / 60

print(f"1 KB 消息：每 1 GB payload 占 {per_gb_payload():.2f} GB 盘（RF=3）")     # 3.05
print(f"100 B 消息：每 1 GB payload 占 {per_gb_payload(rec=100):.2f} GB 盘")     # 3.37
print(f"8 KB 消息：每 1 GB payload 占 {per_gb_payload(rec=8192):.2f} GB 盘")     # 3.02
l, p, s = retention_tb(100, 3, 7)
print(f"3×100MB/s×7d：逻辑 {l:.0f} TB / 物理 {p:.0f} TB / 含 15% 余量 {s:.0f} TB")
for seg in (1, 4, 8):
    m = seg_fill_min(2.315, seg)          # 单分区 200 GB/天
    print(f"segment {seg} GB：填满 {m:.0f} 分钟，平均多留 {m/2/1440:.2f} 天 (+{100*m/2/1440/7:.2f}%)")
```

---

## 28.9 架构边界：日志形态决定了哪些语义做不到

### 能做什么 / 不能做什么 / 靠什么绕过

| 想要的能力 | 日志形态给不给 | 物理原因 | 正确做法 |
| --- | --- | --- | --- |
| **分区内严格有序** | ✅ 给 | 一个分区 = 一个只追加日志，offset 单调 | 用业务键做 key；**顺序只在分区内成立**（→ eng/24、comp/30） |
| **全局有序** | ❌ 不给 | 全局有序 = 单日志 = 单分区 = 单机吞吐上限 | 别要；如果业务真需要，就在消费端按 key 串行化 |
| **多 group 独立重放** | ✅ 给 | 数据在盘上按保留期留着，位点各自维护，互不影响 | 这是 Kafka 相对 RabbitMQ 的核心优势（→ comp/31） |
| **删单条** | ❌ 不给 | 删除单位是段 | `compact`（按 key 重写段）或把状态搬到 KV |
| **按 key 点查最新值** | ❌ 不给（只能顺序扫） | 没有按 key 的索引，只有按 offset 的稀疏索引 | 消费后投影到 Redis/PG/ES（→ comp/12、comp/05） |
| **恰好一次** | ⚠️ 只在 Kafka 内部给 | 事务边界覆盖 consume-transform-produce，覆盖不到你的 MySQL | 效果恰好一次 = 至少一次 + 消费端幂等（→ eng/24） |
| **消息级 TTL / 延迟投递** | ❌ 不给（原生不支持） | 过期是段级的、时间是段内的；没有「按条到期」的机制 | 外挂延迟队列 / 时间轮（→ comp/36） |
| **消费慢不出事** | ✅ 给（但只是「不 OOM」） | 数据在盘上，不在内存里 | lag 会吃盘；治理在并行度与消费吞吐（→ comp/30） |

### 解剖卡八问里最容易写错的两格

| 八问 | 本篇的答案 |
| --- | --- |
| **⑤ 扩展维度** | **只有「分片」一条：partition 是唯一横向扩展单位，副本只买可用性不买吞吐。** 分区数只能加不能减；加分区**会改变 `key → partition` 的映射**（`hash(key) % 分区数`），于是同一 key 的新旧消息可能落在不同分区，**分区内有序被打破**。四选一里：分片 ✅、副本 ✗（不增吞吐）、垂直切 ✗（单个日志不可切）、批处理摊薄 ✅（攒批提升单分区效率，但不改变上限）。这是「它不可按 CPU 扩，只能按分区扩」的直说。 |
| **⑥ 系统级失效** | **只有三类：** ① **集群不可用**——盘写满导致分区离线（事故一）；② **数据不一致**——`unclean` 选举选到落后副本、副本截断（→ comp/29）；③ **成本失控**——保留期 × 副本数 × 速率的乘法链（§28.8 账四的第 9 步：保留翻倍，盘翻倍）。使用级错误（segment 设错、堆设错、客户端不升级）归 §28.10，两节不互相稀释。 |

### 一句话把这一节钉住

**Kafka 的产品功能表上写着「持久化、可重放、可扩展」；日志形态则规定了「只能顺序、只能整段删、只能按分区扩」。** 前者是承诺，后者是边界。评审时先问「你需要的是顺序日志还是可点查的表」——这个问题的答案决定要不要 Kafka，而不是反过来让 Kafka 去变形。

---

## 28.10 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到物理形态） | 怎么定位 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 用 Kafka 存「用户最新状态」，越用越慢 | 用 `delete` 策略的 topic 做 KV：每次查询都是顺序全扫，且旧版本按保留期一直占盘 | 消费者 `seekToBeginning` 后的扫描耗时随保留期线性增长 | 改 `cleanup.policy=compact`，或在消费端投影到真正的 KV（→ comp/12） | Topic 规范里写死：需要「按 key 取最新」的一律 compact 并登记为 changelog |
| 删了一批消息，磁盘一点没降 | 删除是段级事件：段内的消息即使全过期，也要等整段到期 | `kafka-log-dirs.sh` 看目录大小；对比该分区 `LogStartOffset` 是否推进 | 等段过期，或 `kafka-delete-records.sh` 显式推进位点（会跳过数据） | 容量规划按「段粒度」留 15% 余量；不要指望按消息删 |
| 保留 7 天，实际留了 11 天，盘莫名涨 | `log.segment.bytes` 太大 + 冷分区写满慢 → 段填满时间进入保留窗口 | 每分区「段数 × 段大小 ÷ 分区速率」与 `retention.ms` 对比 | 段大小按最慢分区定（`速率 × [1h,6h]`），并设 `log.roll.hours` 兜底 | 新建 topic 的 segment 大小由「预计最慢分区速率」推导，写进 YAML（→ comp/23） |
| 单分区吞吐上不去，第一反应是加分区 | 单分区瓶颈通常在批太小（`batch.size`/`linger.ms` 未调）或未开压缩，而不是分区不够 | 看 `BytesInPerSec` ÷ 分区数 = 单分区字节率；看平均批大小与压缩比 | 先调 `linger.ms=20`、`batch.size=256KB`、`compression=lz4`，再谈分区 | 压测基线按「真实报文 + 真实批量」跑，而不是默认参数（→ eng/23 §23.7） |
| 消费端落后，扩容堆到 32 GB 反而更慢 | page cache 被堆挤掉（覆盖深度 = page cache ÷ 速率），且大堆的 GC 停顿变长 | `free` 看 `buff/cache`；`/proc/meminfo` 的 `Dirty`；GC 日志看 mixed GC 停顿 | 堆回 6–8 GB；治 lag 靠消费并发与处理吞吐（→ comp/30） | 堆大小写进部署模板并纳入变更评审；容器 limit 必须显式给 page cache 留量 |
| 出流量掉、CPU 涨，网络却查不出问题 | 旧客户端触发 down-conversion，零拷贝退化成 read+write | `strace` 里 `sendfile` 消失、`read` 暴涨；`BytesOutPerSec` 与 CPU 反向 | 升级客户端；或先用镜像 topic 把格式转好再给老客户端 | 客户端版本纳入接入评审；升级 broker 大版本前先清旧格式（KIP-724） |
| 同名 topic 加了分区后，顺序错乱 | 分区数变 → `key → partition` 映射变 → 同一 key 的新旧消息落到不同分区 | 抽样同 key 消息的 `(partition, offset)`，看是否跨分区 | 用「新 topic + 双写 + 切换」代替原地扩分区；或消费端按 key 归并 | 分区数一次定够（按峰值 ÷ 单分区能力），变更走新 topic |
| 磁盘突然满、broker 报 `KafkaStorageException` | `retention.ms=-1`、或某个 `log.dirs` 目录不均衡、或 compact 的脏段比例过高 | `kafka-log-dirs.sh --describe` 看每目录占用；`du` 看 topic 排行 | 补显式 retention；做分区重分配（`kafka-reassign-partitions.sh`） | retention 和 segment 大小必须在创建时显式声明，不允许依赖默认值 |
| 以为「Kafka 是内存队列，消费慢会 OOM」 | 它对未消费数据只负责「留在盘上」，内存里只有缓存 | 落后时 RSS 不涨、盘涨 | 改预期：lag 吃盘，治理靠并行度 | 把「lag 增长速率 × 保留期」纳入磁盘容量公式（§28.8 账四） |

---

## 28.11 观测：指标、拐点与告警

Kafka 的指标走 **JMX**：要么直接开 JMX 端口，要么挂 JMX Exporter。**先确认这三件事，否则下面所有 MBean 都读不到**：① `KAFKA_JMX_OPTS` 里配了 `-Dcom.sun.management.jmxremote.port`（或 `-javaagent` 挂了 exporter）；② 防火墙与 `rmi` 端口放通；③ KRaft 模式下 controller 侧指标在另一组 MBean 下。**具体 MBean 名以目标版本 JMX 输出为准——写错一个名字，读者就会认为全篇没验证过。**

| 指标（MBean） | 版本 / 开关前提 | 拐点（越线就动手） | 它度量的是哪件物理事实 |
| --- | --- | --- | --- |
| `kafka.log:type=Log,name=LogStartOffset,topic=*,partition=*` | Kafka 0.10+ 即有；需 JMX 暴露或 exporter | 长期不前进 = 保留策略没生效；与 `LogEndOffset` 之差 × 平均消息大小 ≈ 该分区留存字节 | **留存窗口的直接度量**（`LSO` 什么时候被推走） |
| `kafka.log:type=Log,name=LogEndOffset,topic=*,partition=*` | 同上 | `LEO − LSO` 的增长速率 ≈ 该分区写入速率；与 lag 一起看才知道「消费慢」还是「写入快」 | 活跃段写了多远 |
| `kafka.log:type=Log,name=Size,topic=*,partition=*` 与 `name=NumSegments` | 同上；**分区级指标基数 = 分区数**，万级分区时注意抓取成本 | 段数 > 「名义保留天数 × 每日段数」的 2 倍 → segment 太大或 retention 失效 | 空间放大与过期精度 |
| `kafka.server:type=BrokerTopicMetrics,name=BytesInPerSec` / `BytesOutPerSec`（带或不带 `topic` 维度） | 无开关；带 `topic` 的维度在 3.x 默认存在，4.x 部分指标命名调整（以目标版本为准） | `Out/In` 持续 < 0.5 且 CPU 高 → 疑似 down-conversion（§28.7）；`Out` 突然掉 30%+ 且网络正常 → 同上 | 零拷贝路径是否健康 |
| `kafka.server:type=KafkaRequestHandlerPool,name=RequestHandlerAvgIdlePercent` | Kafka 2.x–3.x 存在；4.x 起大量指标迁到新命名，**以目标版本 JMX 输出为准** | < 30% 持续 5 分钟 = 请求线程池接近饱和（GC 或盘慢的下游表现） | 读路径排队 |
| `kafka.network:type=RequestChannel,name=RequestQueueSize`；`kafka.server:type=DelayedOperationPurgatory,name=PurgatorySize,delayedOperation=Produce/Fetch` | 网络层在 4.x 有重构，`RequestChannel` 系列口径变化——以目标版本为准；`Purgatory` 侧 `acks=all` 相关队列必看 | 队列 > 数百且持续增长；`Produce` purgatory 堆积 = 等 ISR | 请求在等什么 |
| `kafka.server:type=ReplicaManager,name=UnderReplicatedPartitions` / `name=IsrShrinksPerSec` / `name=IsrExpandsPerSec` | 无开关；KRaft 与 ZK 模式同名 | `UnderReplicatedPartitions > 0` 持续 2 分钟；`rate(IsrShrinksPerSec) > 0` | 与磁盘/网络相关的副本落后（**机制细节 → comp/29**） |
| OS 侧：`/proc/meminfo` 的 `Dirty` / `Writeback` / `MemAvailable`；`/proc/diskstats` 的 `%util` 与 `await` | 无开关；**容器里要读 cgroup 的 `memory.stat`（`file` 字段）才是本容器的 page cache** | `Dirty` > 总内存 10% 且持续上涨 → 回刷跟不上（§28.8 账三） | 「Kafka 吃内存」的真相 |
| JVM：`jvm.gc.pause`（JMX + exporter） | 需 `-Xlog:gc*` 或 exporter 采集；G1 下看 mixed GC 的 p99 | p99 > 200 ms 即开始污染 fetch 延迟 | 大堆的隐性成本 |

```bash
# 磁盘与 segment 巡检（值班第一屏）
# ① 每个 log.dirs 目录的占用：先看「最满的那一块」，不是集群总量
kafka-log-dirs.sh --bootstrap-server broker1:9092 --describe \
  | jq -r '.brokers[] | .logDirs[] | "\(.size/1e9|floor)\tGB\t\(.logDir)"' | sort -nr | head

# ② 每分区留存窗口：最早位点 vs 最新位点（3.x 的工具名；更早版本是 kafka-run-class.sh kafka.tools.GetOffsetShell）
kafka-get-offsets.sh --bootstrap-server broker1:9092 --topic audit.events --time -2   # LogStartOffset
kafka-get-offsets.sh --bootstrap-server broker1:9092 --topic audit.events --time -1   # LogEndOffset

# ③ 段数 Top（段数 = 文件数 / 3）：段太多 → 文件/句柄；段太少太久 → 过期滞后
for d in /data*/kafka/*-*; do
  printf '%6d 段  %8s  %s\n' "$(ls "$d"/*.log 2>/dev/null | wc -l)" "$(du -sh "$d" | cut -f1)" "$d"
done | sort -nr | head -20

# ④ OS 侧：脏页、回刷、盘 util（Kafka 变慢而 CPU 不高时先看这里）
awk '/^(Dirty|Writeback|MemAvailable):/ {print}' /proc/meminfo
iostat -x 5 3 | awk '/^(Device|nvme|sd)/ {print $1, $14"%util", $10"await"}'
```

```text
# 告警表达式骨架（Prometheus 风格，阈值取自本节拐点列）
# ① 分区留存窗口异常：LSO 六小时不前进（保留策略没生效，或段太大不 roll）
max_over_time(kafka_log_logstartoffset[6h]) == ignoring() max_over_time(kafka_log_logstartoffset[6h])
# ② 段数失控：段数 > 名义保留天数 × 每日段数 × 2
kafka_log_numsegments > 2 * (7 * 4)
# ③ 零拷贝疑似退化：出流量掉一半而 CPU 没掉
(rate(kafka_server_brokertopicmetrics_bytesoutpersec[5m]) / 1e6) < 0.5 * 500
  and on(instance) (process_cpu_seconds_total > 0.5)
# ④ 请求线程池饱和
kafka_server_kafkarequesthandlerpool_requesthandleravgidlepercent < 0.30
# ⑤ 副本落后（与 comp/29 的交接点）
max(kafka_server_replicamanager_underreplicatedpartitions) > 0
# ⑥ 脏页逼近硬线（配合 dirty_ratio=20% 的机型改分母）
node_memory_Dirty_bytes / node_memory_MemTotal_bytes > 0.10
```

---

## 28.12 一页速记

| 记什么 | 具体结论 |
| --- | --- |
| 物理形态一句话 | 一个 partition = 一个只追加目录 + 若干 `*.log` + `*.index` + `*.timeindex`；**offset 是逻辑位点、position 是段内字节偏移、索引是稀疏的** |
| 定位一条消息 | 选段（内存二分，~1 μs）→ 索引二分（mmap，2–10 μs）→ 顺序扫 ≤ `index.interval.bytes`（4 KB 量级）→ 命中 |
| 索引开销 | 每 4 KB 数据 8 B 偏移项 + 12 B 时间项 ≈ 数据的 0.49%；活跃段索引文件按 10 MB 量级预分配（每分区 ~20 MB 固定成本） |
| 为什么快（四条） | ① 只追加（顺序 IO + 无写放大）② page cache（缓存交给 OS）③ sendfile（省 2 次 CPU 拷贝 + 2 次切换）④ 批量 + 压缩（压的是批） |
| 零拷贝三条前提 | 同格式、走 socket、不做逐字节处理；**down-conversion 与 TLS 都会让它失效** |
| 四条硬边界 | 不能删单条（段级删）、不能改（只追加）、不能随机读（只能 seek 后顺序推进）、不是内存队列（lag 吃盘不吃内存） |
| 保留窗口公式 | 有效保留 ≈ `retention + [0, 段填满时间]`；**段大小按最慢分区定，不按最忙分区定** |
| 存储放大 | 每 1 GB payload 占 3.0–3.4 GB 盘（RF=3，随消息大小变化）；保留窗口 = 速率 × topic 数 × 天数 × RF × 1.15 |
| 内存分配 | 堆 6 GB 量级 + page cache 尽量大；**覆盖深度 = page cache ÷ 写入速率**（110 GB ÷ 5.2 MB/s ≈ 5.9 小时） |
| 扩展维度 | 只有 partition 一条；副本买可用性不买吞吐；加分区会改变 key 映射 → 顺序失效 |
| 首要指标 | `LogStartOffset` 是否前进（留存窗口）、最满单盘水位、`Out/In` 与 CPU 的反向、`Dirty` 页、`UnderReplicatedPartitions` |

**如果只记五句：**

1. **它快是因为它「不做」**：不做原地更新、不做随机读、不自己管缓存、不逐条处理。每一项「不做」都换到了一次昂贵操作的免除，也换走了对应的能力。
2. **删除的最小单位是段**。所以「删一条、改一条、按 key 只留最新」这三件事，在 `delete` 策略的 topic 上根本不存在；要用 `compact`，就得接受后台重写段的账。
3. **缓存在 OS 手里**。`-Xmx` 每多 1 GB，page cache 就少 1 GB，读路径就慢一点——这是「堆越大越慢」的全部原因。
4. **零拷贝有前提**。同格式、走 socket、不逐字节处理；`BytesOutPerSec` 掉而 CPU 涨，就是它退化的信号。
5. **保留窗口是乘法，不是加法**：`速率 × topic 数 × 天数 × 副本数 × 1.15`，再加「段粒度的滞后」。保留期每翻一倍，盘就翻一倍。

---

## 28.13 自检题（5 题）

**Q1.** 一个 topic 12 分区，日写入 120 GB；`log.segment.bytes=1 GB`，`retention.ms=3 天`。有人说「实际会占用 120 × 3 = 360 GB」。这个数偏大还是偏小？给出你的算式和量级结论。

<details><summary>答案</summary>

偏小。要算两件事：

① **副本与余量**：360 GB 是逻辑量；RF=3 → 1,080 GB 物理；再加 15% 余量（段尾未满、过期滞后、目录不均衡）→ **约 1.24 TB**。

② **段粒度滞后**：每分区速率 = 120 GB ÷ 12 ÷ 86,400 s ≈ 116 KB/s。1 GB 的段要 `1 GB ÷ 116 KB/s ≈ 8,600 s ≈ 2.4 小时` 才 roll。所以有效保留 ≈ 3 天 + 平均 1.2 小时 ≈ 3.05 天（+1.7%），这一项**小到可以忽略**。

结论：**约 1.2 TB 物理**。注意一个反直觉点：段粒度这一项在「忙碌分区 + 1 GB 段」的组合下几乎无害；真正把它变成灾难的是把段设成 8 GB 且分区很冷（见 §28.6 的第二张表：日写 1 GB 的分区，8 GB 段让有效保留从 7 天变 11 天）。

量级结论，非承诺值；假设恒定速率、无 compaction、RF=3。
</details>

**Q2.** 有人问「Kafka 为什么比写 MySQL 快？是不是因为它把数据先写内存再慢慢刷盘？」这个解释哪里对、哪里错？

<details><summary>答案</summary>

**对的一半**：写确实先落在 page cache 就返回，落盘是 OS 后台的事（§28.3 写路径第 ② / ⑥ 跳）。

**错的一半有三处**：

① **MySQL 也写 page cache**。差别不在「是否先写内存」，而在**写入形状**：InnoDB 刷脏页是随机写回原位（+ 双写 ×2），Kafka 是纯顺序追加，写放大接近 1×（→ comp/02 §02.6）。

② **MySQL 的慢不在「刷盘」，在「写放大 + 随机 IO」**：随机主键的 B+ 树单行写放大可到 110×，而 Kafka 没有「改一行」这个动作。

③ **Kafka 的读快靠 sendfile 不进用户态**，这一点 MySQL 没有（它必须把行物化出来）。

所以正确的说法是：**Kafka 快不是因为缓存，而是因为「只追加 + 无写放大 + 零拷贝」把三类昂贵操作（随机写、读-改-写、用户态拷贝）全部避免了；page cache 只是让「写成功」不必等盘。**
</details>

**Q3.** 一个 8 broker 的集群，出流量突然从 600 MB/s/台掉到 350 MB/s/台，CPU 从 22% 涨到 58%，磁盘和网络都不忙。给出两条最可能的根因和各自的确认方法。

<details><summary>答案</summary>

两条根因，都指向「零拷贝失效」：

① **旧客户端触发 down-conversion**：`fetch` 声明版本低于 broker 的 `log.message.format.version`，broker 必须把批读进用户态重写。
确认：`sudo strace -f -c -e trace=sendfile,read,write -p <broker-pid>`，若 `sendfile` 几乎为 0 而 `read` 暴涨即确认；再查 `server.log` 里 down-conversion 相关记录（字段名随版本变化，以目标版本为准）；用 `kafka-consumer-groups.sh --describe` 找出新接入的 group。

② **开了 TLS 的 listener 承接了大量流量**：加密必须在用户态逐字节完成，`sendfile` 用不上。
确认：`kafka-configs.sh --describe` 看 listener 的 `security.protocol`；对比同一 topic 在 PLAINTEXT 与 SSL listener 上的 `BytesOutPerSec` 与 CPU。

修法按代价排序：升级客户端 > 用镜像 topic 预转格式 > 给这批 group 单独限流隔离。**注意 Kafka 4.0 起按 KIP-724 移除了旧消息格式，3.x 及以前才存在 down-conversion 这条路——版本决定了这一条坑还在不在。**
</details>

**Q4.** 一个 topic 每个分区日写 0.8 GB，`log.segment.bytes=8 GB`，`retention.ms=7d`。算出有效保留天数，并给出两个修法（不改保留期）。

<details><summary>答案</summary>

每分区速率 ≈ `0.8 GB ÷ 86,400 s ≈ 9.3 KB/s`。8 GB 段填满需要 `8 GB ÷ 9.3 KB/s ≈ 861,000 s ≈ 10 天`。

有效保留 ≈ `7 天 + 平均半个段（5 天）≈ 12 天`，最坏 `7 + 10 = 17 天` —— **名义 7 天，实际 12–17 天，盘占用是规划的 1.7–2.4 倍**。

两个修法（都不动 `retention.ms`）：

① **把 `log.segment.bytes` 降到 128 MB 量级**：填满 ≈ `128 MB ÷ 9.3 KB/s ≈ 3.8 小时`，有效保留 ≈ 7.08 天（+1%）。代价是段数从约 2 个变成约 45 个/分区，文件数与句柄上升——按「速率 × [1h, 6h]」取段大小，128–200 MB 正好落在区间里。

② **加 `log.roll.hours`（如 4 小时）兜底**：即使没写满也按时 roll，让「过期精度」与「段大小」解耦。适合流量波动大、段大小不便一刀切的情况。

反例：如果只把保留期从 7 天改成 4 天，段是 8 GB 不变，有效保留仍会到 4 + 5 = 9 天——**治标不治本**。

量级结论，非承诺值。
</details>

**Q5.** 「我们想把 Kafka 当成用户画像的最新状态存储：30 亿用户，每人一行，每天更新 2000 万次，查询要按 `user_id` 点查。」用本篇的四条硬边界说明为什么不行，并给出一个可行的替代形态（要点到「哪部分归 Kafka、哪部分不归」）。

<details><summary>答案</summary>

**为什么不行（四条边界全中）**：

① **不能点查**：Kafka 只有按 offset 的稀疏索引，没有按 key 的索引；点查一次 = 顺序扫到那一条，30 亿行的 topic 扫一遍是 TB 级读，单次查询不可能在毫秒级。

② **不能改**：每天 2,000 万次更新 = 追加 2,000 万条新版本；若不开 compact，旧版本按保留期一直占盘（空间放大到保留期 × 更新率）；若开 `compact`，后台要持续重写段（写放大），而且它仍然只提供「顺序扫」而不是点查。

③ **不能删单条**：注销用户的画像只能靠 compact 的墓碑，且物理回收滞后。

④ **不是内存队列**：点查要走盘，热用户也拿不到内存级的响应（page cache 只帮到「最近写入」那一小段）。

**可行的替代**：把「状态」与「变更流」分开——

- **Kafka 只做变更通知**（`user.profile.updated`，key=`user_id`，compact 策略，保留短窗口），它的职责是「让下游知道谁变了」；
- **点查存储用 KV**（Redis / HBase / TiKV，按 `user_id` 分片），承担 30 亿行 × 点查；
- 消费端从 Kafka 批量消费变更、批量写 KV，并做幂等（→ eng/24）。

这就是 architect/15 说的那个不可逆选择：**「只追加的日志」与「可编辑的状态」是两种东西，不要用前者的形态去实现后者的访问模式。**
</details>

---

## 28.14 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 三种存储引擎形态的对照：本篇的「只追加但从不重写」是第三种形态，与 LSM 的「只追加 + compaction」严格区分 | [02 存储引擎三形态](02-storage-engine-three-shapes.md) §02.4、§02.6 |
| 解剖卡八问与三本放大账（本篇按同一把尺子展开，⑤ 扩展维度与 ⑥ 系统级失效的答案在 §28.9） | [0-summary](0-summary.md) §0.4、§0.7 |
| 复制与一致性：ISR、HW、LEO 与 acks ——「不丢」是一组参数的联合体，本篇只讲 LEO 在盘上是什么 | `→ comp/29` |
| 消费组与 rebalance：位点提交时机就是语义；积压不是容量问题，是并行度问题 | `→ comp/30` |
| 消息族谱：RocketMQ / Pulsar / RabbitMQ / NATS 的存储形态差异（CommitLog、BookKeeper、队列） | `→ comp/31` |
| 任务队列语义：可见性超时、死信，「被取走但没做完」这个状态 | `→ comp/32` |
| 投递语义与重复消费：本篇只讲磁盘形态，语义（至少一次 / 幂等 / 去重落点）全部归这里 | [eng/24 投递语义](../eng/24-delivery-semantics.md) |
| 异步解耦的本质与选型：该不该上 MQ、七个「不该用」判据 | [eng/23 异步消息基础](../eng/23-async-messaging-basics.md) |
| 消息存储与分发的架构选型：「只追加日志 vs 可编辑状态」的决策与量级估算 | [architect/15 消息存储](../architect/15-message-store.md) |
| 顺序、堆积与重平衡的机制细节（分区键 = 保序键 = 幂等键） | `→ eng/25` |
| 可靠性八环节、消息丢了怎么查 | `→ eng/26`、`→ eng/27` |
| 列存与分析型存储：只追加形态的另一个分支（part、granule、跳数索引） | `→ comp/20` |
| 对象存储与云盘：IOPS 与带宽的算术，Kafka 分层存储的底座 | `→ comp/42` |
| 定时与延迟：Kafka 原生不支持按条 TTL，延迟投递要外挂 | `→ comp/36` |
