# 48. 可观测底座：Prometheus TSDB 与 OTel Collector

> **状态**：`已完成（初稿）` ｜ **所属**：第九部分 · 横切基础件 ｜ **编号**：48 / 53
> **本篇定位**：第九部分唯一一处把「指标的样本在内存里到底占什么」与「采集管道过载时到底先在哪儿丢东西」拆到**结构体与队列**这一层的篇目。它不讲「该采哪些指标、告警阈值怎么定」（语义侧归 `→ go/29`、`→ llm/39`）、不讲日志与追踪的采集语义（归 `→ go/35`）、不讲入口怎么拦请求（归 [39 入口网关](39-gateway-and-load-balancing.md) §39.6）、也不讲长存储的表格式与对象存储语义（前者 [35 Iceberg / 表格式](35-iceberg-table-format.md) §35.3，后者 [42 对象存储与云盘](42-object-storage-and-cloud-disk.md) §42.3）——它只回答一个问题：**指标后端的第一杀手为什么永远是标签基数、采集管道的第一杀手为什么永远是背压，以及这两个数各自怎么算出来。**
> **读法**：Prometheus 刚被 OOMKilled 或重启后半天起不来，读 §48.3 与 §48.8 账一；发布后 `scrape_samples_scraped` 悄悄涨了 10 倍，读 §48.5；Collector 内存锯齿、span 数对不上账，读 §48.6 与 §48.8 账二；看板超时、长存储配额打满，读 §48.7 与 §48.8 账三。要建立「监控自己先挂」的直觉，读 §48.1；只要结论的翻 §48.12。
> **一句话**：指标后端的敌人是**标签基数**，采集管道的敌人是**背压**；两者都以「监控自己先挂」的方式在下一次事故里教育你。

---

## 48.1 引子：三起「监控自己先挂」的事故

可观测底座的事故有一个恶毒的形态：**出事的时候你恰好失去了观测能力**。业务库没有一条 ERROR、K8s 事件里只有一行 `OOMKilled`、值班手机什么都没响——因为**发告警的那个进程正在重启循环里**。于是「P1 发生了 22 分钟没人知道」比 P1 本身更贵。

三起事故分别打在三个墙上：**基数打穿内存（§48.3、§48.5）**、**背压打穿队列（§48.6）**、**远程写与查询在长存储上互相踩踏（§48.4、§48.7）**。

### 事故一：一个 `pod_name` 标签把 42 万 series 推到 680 万，Prometheus 每 40 分钟被 OOMKilled，3 小时里监控全瞎（2026-04-08）

某交易平台每集群一个 Prometheus：15 s 抓取、`--storage.tsdb.retention.time=15d`、容器 `limits.memory: 16Gi`。稳态 **42 万活跃 series、28,000 samples/s**，RSS 3.1 GB。

变化来自一次「让 trace 与 metric 对得上」的需求：有人在 `http_request_duration_seconds` 这个 histogram 的 instrumentation 里加了 `pod_name`，理由是「想看是哪个 pod 慢」。pod 名带 ReplicaSet 哈希（`api-7d9f8c-x2k4p`），**每次发布都产生一批新值**；旧 series 要等 5 分钟 staleness 才停止追加、等 2 小时 block 落盘才离开 head。

| 时间 | 事件 | 关键数字 |
| --- | --- | --- |
| 04-08 09:41 | 发布带 `pod_name` 的版本 | 稳态 42 万 series |
| 09:52 | `prometheus_tsdb_head_series` 42 万 → **210 万**；`scrape_samples_scraped` 5,600 → 27,000/目标 | ×5 |
| 10:20 | head series **680 万**；RSS 16.2 GB；抓取周期开始超 15 s | 453k samples/s |
| 10:34 | 第一次 `OOMKilled`，重启后开始 WAL 重放 | 3.3 GB WAL |
| 10:34–13:50 | 重启循环：起 → 重放 11–27 分钟 → 刚能查 → 再 OOM，共 4 次 | 约 40 分钟一轮 |
| 10:50 | 真实故障：支付失败率 0.4% → 11%，持续 22 分钟，**没有任何告警** | 告警引擎无数据 |
| 11:30 | 第一反应「加内存」16 → 32 GiB：寿命从 40 分钟延到 **82 分钟** | 只买到一倍 |
| 13:55 | `/api/v1/status/tsdb` 里 `seriesCountByLabelValuePair` 首名是 `pod_name`，占 620 万 | 基数 |
| 14:10 | `metric_relabel_configs` 丢掉 `pod_name`，head 回落到 44 万 | 恢复 |
| 复盘 | 监控不可用 3 小时 16 分（含 4 次重启）；期间一起 22 分钟对客故障无人发现，赔付 6.8 万元 | 4 次 OOM |

**"加内存"为什么错**：内存需求是 `series 数 × 每 series 开销`（§48.8 账一）。16 → 32 GiB 只把死亡线从 680 万推到 1,360 万 series，而基数还在随发布累积。**基数是乘法，内存是加法。**

### 事故二：Collector 的 `sending_queue` 满了 23 分钟，5,400 万 span 掉了，SDK 一直返回成功（2026-06-17）

链路：应用 SDK（OTLP）→ 3 个 Collector（`batch` + `memory_limiter`）→ 后端 APM。`sending_queue` 全默认（`queue_size: 1000` 批、`num_consumers: 10`、**`block_on_overflow: false`**），`retry_on_failure` 默认（5 s 起、上限 30 s、`max_elapsed_time: 300s`），`send_batch_size: 8192`。峰值 45,000 spans/s，平均一个序列化 span 约 400 B。

| 时间 | 事件 | 关键数字 |
| --- | --- | --- |
| 20:05 | APM 后端 20 ms → 6 s（它在迁库，没人通知） | P99 6 s |
| 20:08 | `otelcol_exporter_queue_size` 顶到 1,000 | 队列满 |
| 20:08 | 队列满 → 新批次**被直接拒绝**（`block_on_overflow: false`），`otelcol_exporter_enqueue_failed_spans` 单调上涨 | 开始丢 |
| 20:09 | 算账：`1,000 × 8,192 = 819 万 span ≈ 3.3 GB`；产出 45,000/s ⇒ **180 秒填满** | 缓冲 = 3 分钟 |
| 20:12 | 「扩容」3 → 9 副本：下游没变，9 条队列同时怼同一后端，丢得更快 | 方向错 |
| 20:20 | `memory_limiter`（`limit_mib: 4096`，软限 3,072 MiB）开始拒绝，`otelcol_processor_refused_spans` 出现 | 双重丢 |
| 20:28 | 后端恢复。23 分钟产生 `45,000 × 1,380 ≈ 6,210 万 span`，能进队列最多 819 万 ⇒ **约 5,400 万丢失** | 87% |
| 20:40 | 另发现 SDK 侧批处理队列（几千条/实例）早就满了，丢弃计数在**应用进程里**，没进任何看板 | 三处丢点 |
| 复盘 | 次日那起 P1 的用户投诉恰好落在丢失窗口，定位多花 5.5 小时；修法：`block_on_overflow: true` + 持久化队列 + 队列水位告警 + 后端降级时兜底采样 | 5.5 小时 |

**"有队列就不会丢"是本篇最贵的误解**：队列不是无限的，**默认行为是"拒绝并丢弃"而不是"等一等"**，而且拒绝发生在**进入重试逻辑之前**。三处队列（SDK → Collector → 后端）任何一处满都是丢，**只有最后一处有指标**。

### 事故三：远程写把长存储从 20 TB 配额打到 61 TB，一条 `sum by (pod_name)` 扫 1,200 万 series、看板超时 6 天（2026-08-02 ~ 08-08）

平台扩到 6 个 Prometheus（3 区 × 2 副本）后接入统一长存储（对象存储 + 无状态查询层），接入时只做了一件事：把 6 个实例的 remote_write 全指过去，`write_relabel_configs` 空着。每个实例 120 万活跃 series @ 15 s = 80,000 samples/s，合计 **480,000 samples/s**。

| 时间 | 事件 | 关键数字 |
| --- | --- | --- |
| 2026-01 接入 | 6 × 80k samples/s 全量 remote_write | 480k samples/s |
| 08-02 | 20 TB 配额触顶（第 124 天）；写入开始返 429，samples 在源端排队 | 配额满 |
| 08-02 起 | 队列满后较新版本**阻塞 WAL 读取**（§48.4），6 个实例 WAL 无法截断，head 内存一路上涨 | 连带内存 |
| 08-03 | 某 SLO 看板 `sum by (pod_name) (rate(http_requests_total[5m]))` 查 30 天，匹配 1,200 万 series | 超时 |
| 08-03 | 样本数 `1,200 万 × 43,200(30d/60s) ≈ 5.2 × 10¹¹`，是单查询上限（`--query.max-samples`，5×10⁷ 量级）的 **1 万倍** | 必然失败 |
| 08-04 | 加 querier 到 18 台：仍超时（要搬的样本数没变），账单 +2.1 万元/月 | 方向错 |
| 08-06 | `write_relabel_configs` 丢弃 `pod_name`/`instance`/`container_id`，长存储活跃 series 1,200 万 → 180 万 | −85% |
| 08-08 | 看板恢复；日增量 161 GB → 58 GB | 恢复 |
| 复盘 | 看板不可用 6 天、3 个对客 SLO 违约；存储超额自付约 4.7 万元 | 6 天 |

**"全量写进长存储"不是归档，是一次没有预算的复制**：它把每个 series 每个样本都搬过去，包括寿命只有 3 分钟的 pod 名。而查询成本是 `扫描 series × 时间范围 ÷ step`（§48.8 账三）——**写入侧不降基数，查询侧就只能加机器，而加机器对样本数无效**。

> 🩸 **三起事故的共同点**：没有一次是「Prometheus 不行」或「Collector 不行」。事故一**把标签当成了注释**（以为"多一列"，其实是乘法，且乘数随发布累积）；事故二**把队列当成了缓冲垫**（以为不丢，其实队列只决定"丢之前能忍多久"，默认还不忍）；事故三**把长存储当成了备份**（其实它把「基数 × 样本频率」原封不动搬到另一台更贵的机器上）。

---

## 48.2 一句话定位与唯一真问题；三种信号的存储形态差异

**定位**：可观测底座 = **一条把「事件」编码成「数值时间序列 / 日志行 / span 树」的管道，加三种形态完全不同的后端存储**。三个「不是」：不是数据库（无事务、无点更新、无二级索引，写入即追加）、不是消息队列（无消费位点、不能回放，`→ comp/28`）、不是审计系统（采样、聚合、过期是**设计目标**）。

### 唯一真问题

> **指标后端的敌人是标签基数，采集管道的敌人是背压；两者都以「监控自己先挂」的方式在下一次事故里教育你。**

五条推论，全篇在给它们找证据：

1. **指标是"写入时就聚合掉"的数据，所以 label set 就是 schema。** series 的身份是 `(metric name, 全部 label 取值)`：没进 label 的维度**永久丢失**，进了 label 的维度就**乘以取值数**（§48.5）。这是"多打一个标签"成为最危险一次发布的原因。
2. **TSDB 的内存账是 `series 数 × 每 series 开销`，不是「最近多少条数据」。** head 里所有活跃 series 常驻内存，block 才是 mmap 的（§48.3）。**基数决定内存，样本速率只决定 CPU 与磁盘。**
3. **采集管道夹在「停不下来的生产者」与「会变慢的消费者」之间，只能三选一：丢、等、崩。** 默认配置通常选「丢」，因为选「等」会把压力反弹给业务（§48.6）。
4. **远程写不是备份，是把同一个乘法搬到另一台机器。** 源头与长存储上是同一个 `series × 样本频率`，而长存储还要额外付「查询扫多少 series」那笔钱（§48.8 账三）。
5. **长期存储必然换形态**：高基数 + 长保留 + 快查询在单机 TSDB 上不可能同时成立（§48.7）。

**它明确不承诺什么**：Prometheus 不承诺高基数（百万级活跃 series 是舒适区上限量级）、不承诺长期存储（默认 15 天量级）、不承诺精确（超时不补、重启有缺口、remote_write 会阻塞或丢弃）、不承诺 HA（两副本是两套独立数据）；Collector 不承诺「不丢」（默认 `block_on_overflow: false` 就是丢）、不承诺替代消息队列。

### 三种信号：同一条管道，三种截然不同的物理形态

**选后端的第一个问题永远是"这是哪种信号"，而不是"哪个产品好用"**——因为三种信号的敌人根本不同。

| 信号 | 物理单位 | 存储形态 | 成本主项 | 它的敌人 | 第一抓手 |
| --- | --- | --- | --- | --- | --- |
| **指标 metrics** | `(name, labels)` → series；样本是 `(int64 ts, float64 v)` | TSDB：内存 head + 不可变 block + 倒排索引 + 压缩 chunk（§48.3） | 活跃 series × 保留时间 × 每样本字节 | **基数**（乘法） | 降基数：丢无用 label、预聚合（§48.5） |
| **日志 logs** | 一条带时间戳的字节串 + 极少字段 | 倒排索引 + 段（`→ comp/21`、`→ comp/22`）或「只索引标签、正文进对象存储」 | 字节数 × 保留时间 + 索引膨胀 | **量（GB/天）与索引字段数**，顺带也被 label 基数咬 | 采样降级 + 正文移出索引 + 短保留 |
| **追踪 traces** | 一棵 span 树：`trace_id` + 若干 span | 按 `trace_id` 分片的对象/列存，索引只有少数 tag | span 数 × 每 span 字节 × 采样率 | **QPS × 采样率**，加 tag 基数 | 采样（尾采样优先）+ 减少 span 粒度 |

**一句话记住差异**：**指标在写入时就聚合掉了（便宜，但维度选择不可逆）；日志与追踪保留原始事件（贵，但能回答"那一次"）**。由此三条铁律：想让「哪个用户/哪次请求」可查 → 那是 traces/logs 的活，**不要把 ID 塞进指标 label**（事故一）；想让「趋势/告警/容量」便宜 → 那是 metrics 的活，**不要拿日志算 QPS**；三种信号要能互跳，靠的是**共享的低基数标识**（`service`/`env`/`route`）与 **exemplar**（把 trace_id 挂在样本上，而不是变成 label）。

### 解剖卡八问：本篇答哪几问

| 问 | 本篇的答案 |
| --- | --- |
| ① 它承诺什么 | 指标后端承诺**按 series 追加、可压缩、可范围查询的数值时间序列**；不承诺高基数/长期/精确。采集管道只承诺**尽最大努力传输**（默认 at-most-once） |
| ② 数据长什么样 | head（内存 series + chunk + postings + 字符串表）+ WAL（128 MiB 段）+ 2 h 不可变 block（chunks/index/meta.json）+ 长期层对象块（§48.3、§48.7） |
| ③ 一次读写走哪条路 | 写：`scrape → parse → relabel → append(head+WAL) → 2h flush 成 block → 压缩合并`；读：`matcher → postings 求交 → 取 chunk → 解压 → 按 step 求值`（§48.3） |
| ④ 并发与一致 | 单机单写者（head 锁 + 每 series 顺序追加）；**无副本、无共识**；两个 Prometheus 抄同一批 target 只会得到两份独立的、都有缺口的数据（§48.9） |
| ⑤ 扩展维度 | **分片 ✅**（按 target/租户拆实例）、**批处理摊薄 ✅**（batch/长一点 interval）、**垂直切 ✅**（加内存到基数上限）；**副本 ✗**（是双写不是复制，成本翻倍数据仍不完整） |
| ⑥ 系统级失效 | ① 基数爆炸 → head OOM → 监控全瞎（事故一）；② 背压 → 队列满 → 静默丢（事故二，三处丢点）；③ remote_write 阻塞 → WAL 不可截断 → 内存涨 + 超 2 小时样本永久丢失（§48.4）；④ 存储配额打满 + 查询超时（事故三） |
| ⑦ 看哪几个指标 | `prometheus_tsdb_head_series`、`scrape_samples_scraped`、`scrape_series_added`、`prometheus_target_scrapes_exceeded_sample_limit_total`、`prometheus_remote_storage_samples_pending`、`otelcol_exporter_queue_size/capacity`、`otelcol_exporter_enqueue_failed_*`（§48.11） |
| ⑧ 换掉它的信号 | 单实例活跃 series 超数百万 / 保留超 30 天 / 需要多租户限额 / 需要"不丢" → 换长存储或换信号形态；需要"缓冲与解耦" → 换消息队列（§48.9） |

---

## 48.3 Prometheus TSDB 的物理形态：head + WAL + 2 小时 block

### 一张图看完：内存里有什么、磁盘上有什么

```text
┌──────────────────────────── 一个 Prometheus 实例的数据布局 ────────────────────────────┐
│                                                                                      │
│  【内存 head（活跃区，默认 ~2h 跨度）】  ← ★ 基数决定这块的大小                        │
│    series A ──▶ chunks: [c0][c1][c2][c3]   每 chunk ≤ 120 样本（MaxSamplesPerChunk）  │
│    series B ──▶ chunks: [c0][c1]            样本 = (int64 ts, float64 v)              │
│    倒排索引 memPostings:  labelName → labelValue → [seriesID...]                       │
│    字符串表 symbols:       出现过的 label 名/值各只存一份（interned）                  │
│         ↓ 每 2 小时（或 head 超阈值）切一刀                                             │
│  【磁盘 block（不可变，mmap 读，不占常驻内存）】                                        │
│    01H8F2A/  ├── chunks/000001  ← 压缩样本（1~2 字节/样本量级）                         │
│              ├── index          ← 倒排索引的磁盘版，按时间窗切分 postings               │
│              ├── meta.json      ← minTime/maxTime/stats/compaction 级别               │
│              └── tombstones     ← 逻辑删除                                            │
│    01H8F2B/  ← 下一个 2h block，随后被压缩成 6h → 18h → … 逐级 ×3                       │
│  【WAL（崩溃恢复与 remote_write 的唯一来源）】                                          │
│    wal/00000000 00000001 ...  每段 128 MiB 量级，snappy 压缩                           │
│      ← 所有 append 先写这里；重启按它重建 head（"重启要好几分钟"的原因）                │
└──────────────────────────────────────────────────────────────────────────────────────┘
      关键不对称：head 里的一切都占 RAM；block 只占 page cache（可回收）
      ⇒ 「基数」买的是内存，「样本速率」买的是磁盘与 CPU。两件事不是一回事。
```

### head、block、WAL 的分工（以及它如何决定故障形态）

| 组成 | 是什么 | 关键常量 / 默认 | 它在故障里扮演什么 |
| --- | --- | --- | --- |
| **head** | 最近一段时间的全部活跃 series 与 chunk，**全在内存** | 跨度约等于 block 时长（默认 **2 h**，源码 `DefaultBlockDuration = 2h`） | **OOM 的唯一来源**；`prometheus_tsdb_head_series` 就是它的条目数 |
| **chunk** | 一个 series 的一段连续样本，XOR/差值压缩 | `MaxSamplesPerChunk = 120`（源码常量） | 决定每 series 有几个 chunk，从而决定每 series 内存（§48.8 账一） |
| **倒排 postings** | `label → value → seriesID 列表` | 内存 + 磁盘两份 | 查询第一跳；**它的长度 = series 数**，所以基数也是查询成本 |
| **WAL** | 只追加的样本日志 | 每段 **128 MiB** 量级（源码 `DefaultSegmentSize`），snappy | 重启时间 = WAL 字节 ÷ 重放吞吐；remote_write 也从这里读 |
| **2 h block** | 不可变时间切片，flush 后 mmap | 默认 2 h；压缩级别 2h→6h→18h→54h…（逐级 ×3，上限受 `--storage.tsdb.max-block-duration`，默认 31d；**具体级别以目标版本源码 `ranges` 为准**） | 后台压缩吃磁盘 IO；失败会堆小 block（`prometheus_tsdb_compactions_failed_total`） |

> **一条纪律（版本前提）**：`--storage.tsdb.min-block-duration` / `--max-block-duration` 只在实验性场景动（官方用于长期存储侧的拆分），改了会让本地 block 布局偏离默认假设。想省盘请动 retention/size 或降基数。

### 为什么「基数决定内存」：从结构体一路乘上来

一个 series 在 head 里的开销有四块：**① series 结构**（200–400 B，存 label 引用与 chunk 描述符）；**② head chunks**——样本数 = `head 跨度 ÷ interval`，15 s + 2 h ⇒ 480 个 ⇒ `ceil(480/120) = 4` 个 chunk，每个载荷 120–240 B 加结构约 200 B ⇒ 1.2–1.8 KB；**③ postings**（每 label 对一条 4–8 B 引用，摊到每 series 数十 B）；**④ symbols**（值重复度高时几乎不占，**都是唯一字符串时就巨大**）。合计 **每 series ≈ 2.2 KB**（区间 1.7–2.6）。

两个反直觉结论：**① 抓取间隔越短，内存越大**——chunk 数随 `head 跨度 ÷ interval` 线性变多，15 s 改 5 s ⇒ **同样 series 数下内存 ×3**；**② 加内存只是把死亡线后移**——`可承载 series ≈ 可用内存 ÷ 2.2 KB`，16 GiB 容器留 70% 给 head ⇒ **约 400–500 万 series 是硬上限量级**，而 WAL 大小 ≈ `样本速率 × 2h × 压缩率`，基数高 = WAL 大 = 重启慢 = 重启循环（事故一）。

### 读路径：一次 `sum by (…)` 到底碰了什么

| 跳 | 动作 | 成本随什么涨 |
| --- | --- | --- |
| 1 | 解析 PromQL，定范围与 step | 表达式复杂度（μs 级） |
| 2 | matcher 取 postings 列表，按最短优先求交 | **匹配到的 series 数** |
| 3 | 聚合（`sum by`） | 中间结果条数 = series 数 |

**一句话**：第 2–4 跳与「有多少 series 命中」成正比，第 5 跳与「命中 series × 时间点」成正比。一条查询能不能跑完，只取决于 `命中 series × (范围 ÷ step)` 这个乘法，与"库大不大"无关。

---

## 48.4 抓取与远程写：两个放大器，一个静默的丢点

### 抓取侧的放大链

Prometheus 是**拉**模型：每个 target 一个 goroutine，按 `scrape_interval`（全局默认 1 m，生产常用 15 s）GET `/metrics`，解析全部样本，relabel，append。放大链是 `每秒入库样本数 = Σ(每个 target 的 series 数) ÷ scrape_interval`——**你 instrument 了多少 series，就乘上抓取频率**。一个 5,000 series 的 exporter 在 15 s 下是 333 samples/s；1,000 个这样的 target 就是 33 万 samples/s，而单实例舒适区在 100 万 samples/s 量级以内（还要与 head 基数抢内存）。

| 放大来源 | 机制 | 你怎么看见 | 怎么办 |
| --- | --- | --- | --- |
| 每个 target 的 series 数 | exporter 自己决定输出多少（一个 histogram 默认就 10–15 条） | `scrape_samples_scraped`（每 target 自动注入） | 在 exporter 侧减指标/减桶；`metric_relabel_configs` 只省**入库**，抓取与解析开销已付 |
| 抓取频率 | 样本速率 ∝ 1/interval | `prometheus_tsdb_head_samples_appended_total` | 非关键指标拉长 interval；用 recording rule 降采样 |
| relabel 打太多 | 每条 series 每次抓取跑一遍正则 | `scrape_duration_seconds` 上升 | 少用贪婪 regex |
| 抓取超时 || relabel 打太多 | 每次抓取对每条 series 跑一遍正则 | `scrape_duration_seconds` 上升 | 少用贪婪 regex；精确匹配优先 |
| 抓取超时 | 超时的 target 下周期再抓，**不补抓** | `up == 0` + 数据点缺口 | 调 `scrape_timeout`（须 < interval）或修 exporter；**缺口永久** |

> **两条常被写错的机制**（2.x/3.x 一致）：① `scrape_timeout` 默认 10 s 且**必须小于** `scrape_interval`，超时后本周期样本直接没有；② `metric_relabel_configs` 的位置在**解析之后、入库之前**——它救 TSDB 的基数，**救不了抓取与解析的内存峰值**；要救峰值只能在 exporter 侧减，或用 `relabel_configs` 丢整个 target。

### 一个能保命的抓取配置骨架

```yaml
# prometheus.yml 片段（2.x/3.x 通用；只留与本篇论点相关的字段）
global:
  scrape_interval: 15s
  scrape_timeout: 10s        # 必须 < scrape_interval；超时不补抓
  external_labels:           # 长存储里区分数据源，避免多实例的 series 互相顶掉
    cluster: prod-sh-1
    replica: "a"

scrape_configs:
  - job_name: app
    kubernetes_sd_configs: [{ role: pod }]

    # ① target 级：省的是整套抓取开销
    relabel_configs:
      - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]
        regex: "true"
        action: keep
      - source_labels: [__meta_kubernetes_pod_label_app]
        target_label: service            # 低基数、稳定，唯一该长期保留的身份

    # ② metrics 级：省的是 head 内存（抓取开销已付）
    metric_relabel_configs:
      - regex: (pod_name|pod_uid|container_id|request_id|trace_id|user_id|session_id)
        action: labeldrop                 # 唯一 ID 类标签一律丢（§48.5）
      - source_labels: [route]
        regex: ".{80,}"
        target_label: route
        replacement: "__too_long__"       # 超长值必然是"把数据当标签"，折叠但仍保留信号
      - source_labels: [__name__]
        regex: "go_.*|process_open_fds"
        action: drop

    # ③ 兜底：单个 exporter 写错 instrument 时自动止血
    sample_limit: 20000            # 触发 prometheus_target_scrapes_exceeded_sample_limit_total
    label_limit: 30
    label_value_length_limit: 200
```

**为什么三段都在**：`relabel_configs` 管**抓不抓**，`metric_relabel_configs` 管**存不存**，`sample_limit` 管**万一有人写错 instrument**——它是你唯一能在"发布带了 `user_id` 标签"那一刻自动止血的开关。注意它的副作用：触发后该 target 的整次抓取被丢弃，**`up` 会变成 0**，所以必须同时告警那个 exceeded 计数，否则你会以为 target 挂了。

### 远程写：队列、分片与那条 2 小时的死线

remote_write 的路径是 `从 WAL 读样本 → 写进每个 shard 的内存队列 → shard 发 HTTP 请求`。每 shard 内存约 `capacity + max_samples_per_send` 个样本（官方口径每分片 < 2 MB），但**另外每个 series 还会被缓存一份「seriesID → label 值」的映射**，所以 **series churn 越大（高基数 + 频繁发布），这块内存涨得越凶**——这是"加 remote_write 后内存多 25%"之外的隐藏账。

| 参数（`queue_config`） | 作用 | 默认量级（**以目标版本文档为准**） | 调错会怎样 |
| --- | --- | --- | --- |
| `capacity` | 每 shard 内存里能放多少样本 | 10,000 | 太大吃内存；太小更容易阻塞 WAL |
| `max_samples_per_send` | 单请求装多少样本 | 2,000 | 太小请求数暴涨；太大单请求超时、后端拒 |
| `max_shards` | 并发上限 | 200 量级（早期版本 1,000） | 太小吞吐不够；太大把后端打死（与事故二同构） |
| `min_backoff` / `max_backoff` | 重试退避 | 30 ms / 5 s | 无退避 = 重试风暴（同 [42 对象存储与云盘](42-object-storage-and-cloud-disk.md) §42.4） |

**所需 shard 数是一条除法**：`所需 shard ≈ 样本速率 ÷ (max_samples_per_send ÷ 单批往返耗时)`。以事故三为例：480,000 samples/s、`max_samples_per_send=2000`、单批 200 ms ⇒ 每 shard 10,000 samples/s ⇒ **需 48 个 shard**；若后端劣化到单批 2 s ⇒ 每 shard 只剩 1,000 ⇒ **需 480 个 > max_shards(200)** ⇒ 必然排队。

**队列满了之后会怎样：本篇最该背下来的分叉**

| 版本行为 | 表现 | 代价 | 你怎么发现 |
| --- | --- | --- | --- |
| **丢样本**（部分版本/配置） | 超出的样本直接丢，计入 `prometheus_remote_storage_samples_dropped_total` | **静默丢数据**，本地 TSDB 完好 | 只有那个 `_dropped_total` 在涨 |
| **阻塞 WAL**（较新版本） | WAL 无法截断 → 目录持续增长 → head 不能释放 → **内存一路上涨**；**超过约 2 小时后 WAL 被截断，未发出的样本永久丢失** | 从"丢远程"升级成"本地也 OOM + 丢数据" | `samples_pending` 长期 > 0、`prometheus_tsdb_wal_truncations_total` 停滞、WAL 目录变大 |

> **2 小时死线的物理来源**：Prometheus 只能从 WAL 读还没发出的样本，而 WAL 保留由 head 截断策略决定（约等于 block 时长 2 h）。**后端挂 2 小时 = 你拿"本地保留窗口"当远程缓冲区用**，它一定会用完。所以 remote_write 后端的可用性预算必须按「本地 head 保留期」算，而不是按"我们有队列"算；要真不丢，得在管道两端加**持久化**缓冲（Collector 落盘队列 / 独立 Kafka，`→ comp/28`）——调大 Prometheus 队列只买到内存。

---

## 48.5 基数账：`series = 各 label 取值数的乘积`

### 先把定义钉死

- **series** = 一列以 `(metric name, 完整 label set)` 为身份、按时间追加的数值。**身份里任何一个字符不同，就是另一条 series。**
- **label set 是身份，不是元数据。** 加一个 label = 换一个身份空间 = 换一张表。
- **活跃 series** 指过去 5 分钟（`--query.lookback-delta` 默认 5 m 量级）内有样本的 series；head 里的条目数比它略大（旧 series 要等 block 落盘才走）。

### 乘积公式，以及「不是所有 label 都会乘」

类型级条数：counter/gauge = 1；classic histogram（12 桶）= 12 个 `_bucket` + `_sum` + `_count` = **14**；summary（3 分位）= 5。实例级：`series(某指标) = 类型条数 × Π(互相独立的 label 取值数)`。

**「互相独立」是关键词：函数依赖的 label 不真乘，取上界即可。** 例如 `http_requests_total` 的 `service(9) × method(4) × status(6) × route(40) = 8,640`；再加 `pod_name` 时，因为 pod 属于某个 service（service 被 pod 决定），组合数不是 `8,640 × 3,200`，而是把 service 那一维换成 pod 那一维后重算：`4 × 6 × 40 × 3,200 = 307 万`。但若 `pod_name` 与 `instance` 同时加且两者不完全一一对应，就真的再乘一次。

**这张树是事故一的全部**：

```text
http_request_duration_seconds（12 桶 histogram，类型条数 14）
│
├─ method:      GET POST PUT DELETE                        → 4
├─ status_code: 200 201 400 401 403 404 500                → 7
├─ route:       140 条（一旦混进 orderId 就无界）           → 140
├─ service:     9                                          → 9
│
├─ 无 pod_name：4 × 7 × 140 × 9 × 14 = 493,920 条          ← 已经太多
│
└─ 加 pod_name（90 天内 3,200 个历史值，每 3 天发布一次累积）
      service 被 pod 决定 ⇒ 4 × 7 × 140 × 3,200 × 14 = 17,561,600 条
      ⇒ 单个指标就能把任何单实例打穿（16 GiB ≈ 400–500 万条上限）
      ⇒ 事故一实测只涨到 680 万：scrape_interval 先撑不住超时了（自我限流）
```

> **关键洞察**：**基数爆炸往往不是「一个天文数字标签」造成的，而是「几个看起来很乖的标签相乘」造成的。** `route` 混进 `orderId`、`status` 混进错误码原文、`service` 混进环境名——每个只是 ×2 或 ×1.5，三四个乘起来就是 ×10，再乘 histogram 的 14 倍与 pod 的 3,200 倍，就是一场 OOM。

### 一个标签怎么把 series 乘 10 万

| 你加的 label | 听起来无害的理由 | 实际乘数 | 结果（基线 42 万 series 的量级） |
| --- | --- | --- | --- |
| `pod_name` | 「看是哪个 pod 慢」 | ×(历史 pod 名总数)，随发布累加，90 天可达 3,200 | 数十亿（理论）/ 抓取先崩 |
| `instance`（`ip:port`） | 「看是哪台机器」 | ×(实例数 × 端口数)，重启换 IP 就新增 | 同上 |
| `user_id` / `tenant_id` / `request_id` | 「想按客户看」「想跳到 trace」 | ×租户数（1 万–100 万）/ ×请求数（每秒数十万，**无界**） | **必然打穿** |
| `http_route`（含路径参数）/ `status`（含错误码原文） | 「想看具体接口/错误类型」 | ×(路由基数或错误码基数)，含 ID 时无界 | 无界或数百倍 |
| `client_ip` | 「想看来源分布」 | ×(客户端 IP 数)，无界 | 打穿 |

**判据（记这一条就够）**：一个 label 能不能进指标，取决于**它的取值数是否有硬上界并且很小（几十以内）**，而不是"它有没有用"。`env`/`region`/`service`/`method`/`status_class` 是好标签；`pod_name`/`instance`/`user_id`/`request_id`/`path`/`ip` 是毒标签——它们该去 traces/logs，或者该在聚合后被丢掉。

### 降基数的三种手段，以及各自改的是哪条物理路径

| 手段 | 做什么 | 改的是哪条路径 | 何时用 | 代价 |
| --- | --- | --- | --- | --- |
| **聚合（recording rule）** | 用 PromQL 把高基数原始指标压成低基数派生指标 | 改**入库的 series 集合**；原始指标仍在本实例活到 retention | 长期看板/告警 | 原始维度永久丢失；rule 自己吃 CPU（`prometheus_rule_group_iterations_missed_total` 会告诉你算不过来） |
| **丢弃（`metric_relabel_configs` / remote_write `write_relabel_configs`）** | `labeldrop` / `drop` 在入库前或远程写前裁掉 | 改**本地 head 的 series 数**或**长存储的 series 数** | 发现毒标签时立刻止血 | 干净但粗暴，**救不了抓取开销** |
| **折叠（值改写）** | `replace` 把高基数值打成 `__too_long__` / `other` 桶 | 同上，但**保留"有这件事"的信号** | 要"有这个维度但不要明细" | 正则要防误伤 |

### 审计基数的可跑片段

```promql
# ① head 里的活跃 series 总数：唯一的"总量"指标，必须告警
prometheus_tsdb_head_series

# ② 每个 metric name 的 series 数 top10 —— 找"哪个指标在犯罪"
#    ⚠️ 极贵（要扫全部 series），只在诊断时手动跑，别进看板
topk(10, count by (__name__) ({__name__=~".+"}))

# ③ 谁在输出海量 series（抓取规模）；④ 新长出来的 series（发布后 10 分钟最灵）
topk(20, scrape_samples_scraped)
topk(20, scrape_series_added)
```

> **诊断入口优先级**：先 `GET /api/v1/status/tsdb`（返回 `seriesCountByLabelName` / `seriesCountByLabelValuePair` 等四组 top10），再写 PromQL。它走内存里的索引统计，**不需要扫 series**；而上面②这种查询本身要吃大量内存——**用昂贵查询去诊断内存问题，是很多人踩的第二脚**。

下面这段 Go 把「这个维度能不能进指标」从口头约定变成代码：没在白名单里的动态值一律折叠成 `other`，未登记的键直接丢弃。

```go
// 标签守卫：把动态值收敛到有限集合，从源头掐住基数（默认拒绝）。
type LabelGuard struct {
	allowed map[string]map[string]struct{} // label -> 允许的取值集合
	maxCard int                            // 每个 label 的硬上限：防御未登记的新值潮水
}

func NewLabelGuard(spec map[string][]string) *LabelGuard {
	g := &LabelGuard{allowed: make(map[string]map[string]struct{}, len(spec)), maxCard: 64}
	for k, vals := range spec {
		set := make(map[string]struct{}, len(vals))
		for _, v := range vals {
			set[v] = struct{}{}
		}
		g.allowed[k] = set
	}
	return g
}

// Sanitize 返回可安全作为 label 的键值对：白名单内原样保留；白名单满则折叠为 "other"；
// 未登记的键一律丢弃——丢弃比折叠更安全，因为它不会创造新的取值空间。
func (g *LabelGuard) Sanitize(kv map[string]string) map[string]string {
	out := make(map[string]string, len(g.allowed))
	for k, set := range g.allowed {
		v, ok := kv[k]
		if !ok {
			continue
		}
		if _, hit := set[v]; hit {
			out[k] = v
			continue
		}
		if len(set) >= g.maxCard { // 白名单自己要被限流，否则它就成了新的无界集合
			out[k] = "other"
			continue
		}
		set[v] = struct{}{} // 新值进白名单（有上限），保留可观测性
		out[k] = v
	}
	return out
}
```

**为什么白名单而不是黑名单**：黑名单要穷举 `user_id`、`order_id`、`trace_id`、`session`、`ip`……业务每加一个字段你就漏一次。白名单的失败模式是「少了一个维度」（可见、可修），黑名单的失败模式是「多了一个乘数」（不可见、要等 OOM）。**基数防御必须默认拒绝。**

---

## 48.6 OTel Collector：receiver / processor / exporter，以及「它不是无限缓冲」

### 一条管道的物理形状：数据在哪儿排队，就在哪儿丢

```text
┌──────────────────── 一个 Collector 实例内的数据流与三个丢点 ─────────────────────┐
│ 应用 SDK ─OTLP→ ┌──────────┐  ┌───────────────┐  ┌───────┐  ┌──────────────┐   │
│ （自带队列，     │ receiver │→│ memory_limiter│→│ batch │→│ sending_queue│   │
│  几千条/实例）   └──────────┘  └───────────────┘  └───────┘  └──────┬───────┘   │
│                       │               │               │             │           │
│                       ▼               ▼               ▼             ▼           │
│                  丢点③           丢点②           只增内存       丢点①          │
│                  接收端拒绝      软限越线拒绝     （攒够才发）   队列满即丢      │
│                  (429/错误)      (非永久错误)                  block_on_       │
│                                                              overflow=false   │
│                                                        ┌────────▼───────────┐  │
│                                                        │ consumer×N + 重试  │  │
│                                                        │ 5s→30s, 上限 300s  │  │
│                                                        └────────┬───────────┘  │
│                                                                 ▼              │
│                                                            后端（会变慢）       │
└────────────────────────────────────────────────────────────────────────────────┘
   核心不对称：生产者永不减速，消费者会变慢；每一层"缓冲"都是有限的数。
```

### 三种组件与它们各自的账

| 组件类 | 代表 | 它改的是哪条物理路径 | 它**不**解决什么 |
| --- | --- | --- | --- |
| **receiver** | `otlp`、`prometheus`、`filelog`、`k8sattributes` | 把数据搬进内存；对推送型协议可"拒绝"（背压入口） | 后端慢。**拉取型 receiver 被拒绝时数据直接没了**（没有对端可退） |
| **processor** | `memory_limiter`、`batch`、`filter`、`attributes`、`tail_sampling` | 改内存占用与数据形状：`batch` 攒批（省请求、涨内存）、`memory_limiter` 拒绝、`filter` 丢数据 | 不产生持久性：`batch` 攒的批在进程被杀时全丢 |
| **exporter** | `otlp`、`otlphttp`、`prometheusremotewrite`、`loadbalancing` | 出站：队列、并发 consumer、重试退避 | 后端**永久**慢——重试只能等，等不到就丢（`max_elapsed_time`） |

### 三层缓冲的容量算术（这就是事故二的 819 万）

| 缓冲层 | 容量由什么决定 | 默认量级（**以目标版本 README 为准**） | 事故二的数 |
| --- | --- | --- | --- |
| ① 应用 SDK 队列 | SDK 的 `BatchSpanProcessor` 队列（按条计） | 几千条/实例 | 未计入任何看板 |
| ② `batch` processor | `send_batch_size`（攒批阈值）/ `send_batch_max_size`（切片上限，0 = 不限） | 8,192 / 0；`timeout` 200 ms | 每批 `8,192 × 400 B ≈ 3.3 MB` |
| ③ `sending_queue` | `queue_size` × `sizer`（默认按"批"计） | 1,000 批；`num_consumers` 10 | `1,000 × 3.3 MB ≈ 3.3 GB` |
| （可选）持久化队列 | `sending_queue.storage: file_storage` + 同样的 `queue_size` | 1,000 批，但落盘 | **没开 ⇒ 进程一挂全丢** |

> **把三层加起来的公式**：`可缓冲条数 ≈ SDK 队列 + send_batch_size × queue_size`；`可缓冲内存 ≈ 可缓冲条数 × 每记录序列化字节 × (2–3)（batch 复制开销）`。事故二：`8,192 × 1,000 = 819 万 span`，按 400 B 算 3.3 GB，而 `memory_limiter` 硬限只有 4 GiB ⇒ **队列还没满，内存限制先动手**，于是双重丢：`enqueue_failed`（队列满）+ `refused`（软限越线）。

### memory_limiter：有意设计成「拒绝」而不是「换页」

- **软限 = `limit_mib` − `spike_limit_mib`**（`spike_limit_mib` 默认是 `limit_mib` 的 20%）；**越过软限即拒绝数据**，方式是给上游返回**非永久错误**。越过硬限额外**强制 GC**（较新版本还有 `min_gc_interval_when_soft_limited`/`_hard_limited`，**以目标版本 README 为准**）。
- **`check_interval` 默认 0 s，等于必须自己设**（官方建议 1 s）；设大了会在两次检查之间冲过硬限。
- 官方最佳实践：**放在 pipeline 的第一个 processor**（才能对 receiver 形成背压），并同时把 `GOMEMLIMIT` 设成容器内存的 80%。

**最危险的组合**：`memory_limiter` 在 `batch` **之后**（或没配），且上游是**拉取型 receiver**（`prometheus`/`filelog`）——此时"拒绝"无处反弹，**拒绝 = 直接丢**。`filelog` 稍幸运：cursor 在 `storage` extension 里，没提交 offset 就能重读。

### 一份「不会静默丢」的配置骨架

```yaml
# otel-collector-config.yaml（Collector 0.9x 量级；字段名以目标版本 README 为准）
receivers:
  otlp:
    protocols:
      grpc: { endpoint: 0.0.0.0:4317, max_recv_msg_size_mib: 8 }
      http: { endpoint: 0.0.0.0:4318 }
processors:
  memory_limiter:            # ① 永远放第一位，才能把压力反弹给 receiver/SDK
    check_interval: 1s       #    默认 0s，必须显式设
    limit_percentage: 75     #    容器里用百分比，跟着 limits 走
    spike_limit_percentage: 20   # 软限 = 75% - 20% = 55%
  batch:                     # ② 攒批：省请求数，代价是进程被杀时这一批全丢
    send_batch_size: 4096    #    比默认 8192 小一档，换更低的单批内存
    send_batch_max_size: 8192
    timeout: 5s
exporters:
  otlphttp/backend:
    endpoint: https://apm.internal:4318
    timeout: 10s
    sending_queue:
      enabled: true
      queue_size: 2000
      block_on_overflow: true     # ★ 宁可让上游等，也不要静默丢
      num_consumers: 10
      storage: file_storage       # 持久化队列：进程重启不丢（盘要留余量）
    retry_on_failure:
      enabled: true
      initial_interval: 5s
      max_interval: 30s
      max_elapsed_time: 300s      # 超过这个时长的批次会被丢弃并计入 failed 计数
extensions:
  file_storage: { directory: /var/lib/otelcol/queue, timeout: 10s }
service:
  telemetry:
    metrics: { level: detailed }  # 要队列/批大小等细粒度指标必须开（版本相关）
  pipelines:
    traces:
      receivers:  [otlp]
      processors: [memory_limiter, batch]
      exporters:  [otlphttp/backend]
```

**三个必须一起做的动作**（只做一半就是事故二）：① **`block_on_overflow: true`** —— 默认 `false` 的含义是"队列满立刻拒绝并丢弃"，而拒绝发生在**进入 exporter 重试逻辑之前**，连重试机会都没有；改 `true` 后上游（receiver/SDK）才被背压，SDK 才有机会按自己的策略重试或降采样。② **持久化队列** —— 内存队列只能扛"consumer 慢"，扛不住"进程被 OOMKilled"。③ **告警这三个计数** —— `otelcol_exporter_enqueue_failed_*`、`otelcol_exporter_queue_size ÷ capacity`、`otelcol_processor_refused_*`，它们是**唯一**能告诉你"正在丢"的信号。

> **Collector 不是消息队列，差在三件事**：① 没有消费位点；② 不能回放；③ 不能扩存储（队列大小是配置，改要重启）。要「不丢 + 可回放 + 解耦」必须换真正的日志系统（`→ comp/28`）——这也是"Collector 后面挂 Kafka"这个形态存在的原因（§48.9）。

---

## 48.7 存储与保留：为什么长期存储必须换形态

### 本地 TSDB 的边界与长期层的形态差异

| 维度 | 本地 Prometheus TSDB | 对象存储 + 无状态查询层（Thanos / Mimir 类） |
| --- | --- | --- |
| 数据单位 / 保留 | 本地 block 目录，绑在这台机器上；`retention.time` 默认 15 d | 对象存储里的 block + 索引（**一次写只读**，正合 [42 对象存储与云盘](42-object-storage-and-cloud-disk.md) §42.7 的判据），保留可到年 |
| HA / 租户 | ❌ 无复制（两副本是两份独立数据）；❌ 没有租户概念 | ✅ 副本 + quorum（Mimir 类）或"双写 + 去重"（Thanos 类）；✅ per-tenant 限额（对平台方最大的价值） |
| 降采样 / 查询 | ❌ 不做；单机扫本地 block | ✅ 5 分钟 / 1 小时两档（Thanos 的 40 h / 10 d 量级口径，**以目标版本文档为准**）；查询扇出到 store-gateway，成本随集群放大 |
| 失败形态 | head OOM / 磁盘满 / compaction 落后 | 配额打满（ingest limit）、ingester OOM、querier 超时（事故三） |

**为什么"必须换形态"而不是"给 Prometheus 加块盘"**：三件事在单机上同时不成立——① **高基数**（内存 = series × 2 KB，加盘加不了内存）；② **长保留**（15 天 200 万 series 已 TB 级，13 个月十几 TB，而单机要在这些 block 上做索引查找）；③ **不丢**（单实例无副本）。换形态换的是三份契约：块进对象存储（便宜、无限、只读）、写入靠"hash 到 ingester + 复制"（换来 HA 与租户限额）、查询靠"预聚合 + 降采样"。

**查询放大**：30 天、200 万 series、step 60 s 的一条查询，要展开 30 d ÷ 2 h = **360 个原始 block**、扇出数百次 store-gateway 查找、最终解码 `200 万 × 43,200 = 8.6 × 10¹⁰` 个样本。**这就是"看板超时"的物理原因**：不是数据库慢，而是你要它搬 860 亿个数。加 querier / 加内存 / 换 SSD 都在试图把这些数搬得更快；唯一有效的是**在写入侧聚合掉 99.99%** 或**不要 60 s 的 step**（降采样到 5 m ⇒ ÷5）。

### 在列存里做「明细指标」查询（什么时候该换形态）

```sql
-- OTel span 落到列存：高基数维度在这里是"列"而不是"标签"，不受基数诅咒。
CREATE TABLE otel_spans (
  ts DateTime64(3), service LowCardinality(String), route LowCardinality(String),
  status LowCardinality(String), tenant_id UInt64, trace_id String, duration_ms UInt32
) ENGINE = MergeTree PARTITION BY toDate(ts) ORDER BY (service, toStartOfMinute(ts), route);

-- ① 按租户看 P99：这种查询只有列存敢做
SELECT tenant_id, quantile(0.99)(duration_ms) AS p99, count() AS n
FROM otel_spans WHERE ts >= now() - INTERVAL 1 HOUR AND service = 'checkout'
GROUP BY tenant_id ORDER BY p99 DESC LIMIT 20;

-- ② 指标后端该收的只有这一层：预聚合结果（series 从百万级降到千级）
SELECT toStartOfFiveMinute(ts) AS m, service, route, status,
       count() AS requests, quantile(0.99)(duration_ms) AS p99
FROM otel_spans WHERE ts >= now() - INTERVAL 6 HOUR GROUP BY m, service, route, status;
```

**分界线一句话**：**「我要趋势和告警」→ 指标后端；「我要那一次/那个租户」→ 列存或 traces。** 把后者塞进前者，就是事故一与事故三。

---

## 48.8 三本账：series 内存、摄入字节、查询成本

三个算例，各带乘法链、假设清单与「量级结论」标签。账一用事故一现场（42 万 → 680 万 series），账二算磁盘与带宽，账三算一条查询要搬多少样本。

### 账一 · series 内存 = series 数 × 每 series 开销

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 每 series 在 head 里的样本数 | `7200 s ÷ 15 s` | **480 个** |
| 2 | 每 series 的 chunk 数 | `ceil(480 ÷ 120)`（`MaxSamplesPerChunk = 120`） | **4 个** |
| 3 | chunk 内存 | 4 ×（载荷 120–240 B + 结构约 200 B） | 1.2–1.8 KB |
| 4 | series + postings + symbols | 结构 200–400 B + postings 数十 B + symbols 摊薄 | 0.5–1.0 KB |
| 5 | 每 series 合计（中值） | 3 + 4 | **≈ 2.2 KB** |
| 6 | 稳态 42 万 series | `420,000 × 2.2 KB` | ≈ 0.92 GB（实测 RSS 3.1 GB，差额是 Go 堆余量与查询/remote_write 缓存） |
| 7 | 峰值 680 万 series | `6,800,000 × 2.2 KB` | **≈ 15 GB** > 16 GiB 容器的 head 预算 ⇒ OOM |
| 8 | 加内存 16 → 32 GiB | 上限 500 万 → 1,000 万 series | 事故里只把寿命从 40 分钟延到 82 分钟 |

> **量级结论（非承诺值）**。假设：① 每 series 2.2 KB（区间 1.7–2.6 KB，取决于 label 长度与抓取间隔）；② chunk 数按 `MaxSamplesPerChunk = 120` 与 2 h head 推算；③ 容器内 70% 内存可给 head；④ 开 remote_write 约 **+25%** 内存，series churn 大时更高。**结论**：**内存是 series 数的线性函数**，只有"降基数"与"拉长 interval"两个旋钮；**加内存买的是死亡线，不是安全**。

### 账二 · 摄入字节 = series × 样本频率 × 每样本字节

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 稳态样本速率 | `420,000 ÷ 15 s` | 28,000 samples/s |
| 2 | 压缩后每样本字节 | XOR/差值编码（取中值） | **1.0–2.0 B（取 1.3）** |
| 3 | 稳态落盘 | `28,000 × 1.3 B` | 36.4 KB/s ⇒ **3.1 GB/天** |
| 4 | 事故期样本速率 | `6,800,000 ÷ 15 s` | **453,000 samples/s** |
| 5 | 事故期落盘 | `453,000 × 1.3 B` | 589 KB/s ⇒ **51 GB/天** |
| 6 | compaction 写放大 | 2h→6h→18h→54h 逐级重写整个窗口 | 实际写盘约摄入量的 **2–3 倍** |
| 7 | WAL（重启时间） | `453,000 × 7,200 s × ~1 B` | **≈ 3.3 GB**；按 1–5 M samples/s 重放 ⇒ **11–27 分钟** |
| 8 | remote_write 带宽 | `453,000 × 2–4 B` | 0.9–1.8 MB/s（**不是瓶颈**；瓶颈是 series 数与 label 映射内存） |

> **量级结论（非承诺值）**。假设：① 1.3 B/样本（区间 1–2 B，与 label 重复度有关）；② block 压缩逐级 ×3（以目标版本 `ranges` 为准）；③ 不计 WAL 与 head 的重复写盘。**结论**：**磁盘账是"样本数"的函数**（series × 频率 × 保留），省盘就动这三个数；**带宽从来不是 remote_write 的瓶颈**。

### 账三 · 查询成本 = 扫描 series 数 × (时间范围 ÷ step)

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 命中 series 数 | 长存储里 90 天内出现过的全部 pod 维度 | **12,000,000** |
| 2 | 每 series 点数 | `2,592,000 s ÷ 60 s` | **43,200** |
| 3 | 样本总数 | 1 × 2 | **5.18 × 10¹¹** |
| 4 | 与单查询上限比 | 上限量级 5×10⁷（`--query.max-samples` 默认量级） | **超 ≈ 1 万倍** ⇒ 必然失败 |
| 5 | 缩到 1 小时窗口 | `12,000,000 × 240`（15 s step） | 2.9 × 10⁹，仍超 58 倍 |
| 6 | 抓手 A：预聚合到 2,000 series | `2,000 × 43,200` | 8.6 × 10⁷（仍超 1.7 倍 ⇒ 30 天窗口必须配降采样） |
| 7 | 抓手 B：step 60 s → 5 m | ÷5 | 与 6 合用才进可行区 |
| 8 | 抓手 C：只查 7 天 | `2,000 × 10,080` | **2.0 × 10⁷**，安全 |

> **量级结论（非承诺值）**。假设：① 样本数 ≈ `命中 series × (范围 ÷ step)`（不含 range 回看窗口）；② 单查询样本上限按 5×10⁷ 量级（**以目标版本文档为准**）；③ 未计 block 索引与 RPC（与命中 series 同阶）。**结论**：查询能不能跑完由一个乘法决定；**降基数是唯一 0 元方案**，加机器最贵且最无效；**step 与范围是两个免费旋钮**。

### 三本账压成一张表

| 账 | 乘法链 | 谁在付 | 一次能还清吗 | 拐点信号 |
| --- | --- | --- | --- | --- |
| **内存账** | `series × [(head跨度 ÷ interval) ÷ 120 × chunk开销 + series开销]` | Prometheus 进程（OOM 就是它） | ⚠️ 只能降基数或拉 interval | `head_series` 逼近 `可用内存 ÷ 2.2 KB` 的 70% |
| **摄入账** | `series × (1 ÷ interval) × 样本字节 × 保留天数 × 写放大` | 磁盘 + 账单 + compaction IO | ✅ 可（降保留/降频率），但基数涨会再生 | 单实例日增 > 50 GB；`wal_truncations_total` 停滞 |
| **查询账** | `命中 series × (范围 ÷ step)` | 每个看板、每条告警规则 | ⚠️ 只能预聚合/降采样 | 样本数 > 上限 10%；看板超时 |

```python
# 三本账压成可复算脚本（Python 3，无第三方依赖）。改任一假设直接重跑。
KB = 1024

def mem_account(series, interval_s=15, head_span_s=7200, samples_per_chunk=120,
                chunk_kb=(1.2, 1.8), series_kb=(0.5, 1.0), head_ratio=0.7, ram_gib=16):
    """账一：head 内存 = series 数 × 每 series 开销；反推能撑多少 series"""
    samples = head_span_s / interval_s
    chunks = -(-samples // samples_per_chunk)          # 向上取整
    per_kb = chunks * sum(chunk_kb) / 2 + sum(series_kb) / 2
    return samples, chunks, per_kb, series * per_kb / KB**2, ram_gib * head_ratio * KB**2 / per_kb

def ingest_account(series, interval_s=15, bytes_per_sample=1.3, retention_days=15, amp=2.5):
    """账二：日增与保留期总量（压缩后），含 compaction 写放大"""
    rate = series / interval_s
    gib_day = rate * bytes_per_sample * 86400 / KB**3
    return rate, gib_day, gib_day * retention_days * amp

def query_account(series, range_s, step_s, max_samples=5e7):
    """账三：样本数 = series × (range/step)；返回超出上限的倍数"""
    samples = series * (range_s / step_s)
    return samples, samples / max_samples

s, c, per, need, cap = mem_account(6_800_000)
print(f"账一 每 series {per:.2f} KB（{s:.0f} 样本/{c:.0f} chunk）；680 万 ≈ {need:.1f} GiB；16GiB 可承载 ≈ {cap/1e4:.0f} 万")
r, d, tot = ingest_account(6_800_000)
print(f"账二 {r:,.0f} samples/s；日增 {d:.1f} GB；15 天含写放大 ≈ {tot:.0f} GB")
n, x = query_account(12_000_000, 30*86400, 60)
n2, x2 = query_account(2_000, 30*86400, 300)
print(f"账三 {n:.2e} 个样本（超上限 {x:,.0f} 倍）→ 预聚合+5m step 后 {n2:.2e} 个（{x2:.2f}×）")
# 量级输出：2.21 KB / 15.0 GiB / 430 万 series / 51 GB 日增 / 1.9 TB 15 天 / 超限 10,368 倍
```

---

## 48.9 架构边界：Prometheus 不适合高基数与长期存储，Collector 不能替代队列

| 你想做的事 | Prometheus TSDB 能做吗 | 为什么（指到物理形态） | 靠什么绕过 |
| --- | --- | --- | --- |
| 秒级趋势、阈值告警、容量规划 | ✅ **强项** | 定长样本 + 压缩 + 倒排索引，写入即聚合，扫描极便宜 | 无 |
| 千万级活跃 series | ❌ | 16 GiB 上限 400–500 万；OOM 后重启 → 重放 → 再 OOM | 长存储（ingester 分片）或降维 |
| 保留 1 年以上原始分辨率 | ❌ | 单机磁盘 × compaction 写放大；查询要展开 4,000+ block | 对象存储 + downsampling（§48.7） |
| 精确「不丢」（审计、计费） | ❌ | 抓取超时不补、重启有缺口、remote_write 会阻塞或丢弃 | 日志/队列形态（`→ comp/28`）+ 列存（`→ comp/20`） |
| 回答「那一次请求为什么慢」 | ❌ | 指标写入即聚合，ID 维度**物理上不存在** | traces（`→ go/35`）或日志 |
| **Collector**：缓冲与解耦 | ⚠️ 只在"分钟级"成立 | 队列是内存（或一块盘），容量 = `queue_size × 批次大小`（§48.6） | 后面挂 Kafka（`→ comp/28`） |
| **Collector**：保证不丢 | ❌ 默认就是丢 | `block_on_overflow: false`，拒绝发生在重试逻辑**之前** | `block_on_overflow: true` + 持久化队列 + 上游背压 |

**日志与指标的取舍（一条判据）**：「错误率是否超阈值」「昨天哪个版本引入回归」这类**答案是一个数、维度有硬上界**的问题归指标；「这个用户为什么失败了」「这条链路慢在哪一跳」这类**维度无界、要那一次**的问题归日志与 trace；「同时按 8 个维度切片看明细」归列存；「合规原样留存 7 年」归只追加的对象存储（§42.7 判据）。把后三类塞进指标，就是事故一与事故三。

**扩展维度：四选一，写清哪个成立**

| 维度 | 成立吗 | 具体做法与代价 |
| --- | --- | --- |
| **分片** | ✅ 主答案 | 按 target/租户/集群拆多实例（或 `--enable-feature=agent` 只抓取 + remote_write）；代价是"跨实例查询"要一个统一查询层 |
| **副本** | ⚠️ 不是真复制 | 两个实例抄同一批 target = 两份**都有缺口**的数据；查询层靠 `replica` 标签去重；成本 ×2 且修不了缺口 |
| **垂直切** | ✅ 在边界内 | 加内存/加核，直到基数上限或单机 samples/s 上限；只解决"不够快"，不解决"基数太大" |
| **批处理摊薄** | ✅ | `batch` / 更长 interval / 预聚合；代价是延迟与内存（§48.6） |

---

## 48.10 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到物理形态） | 怎么定位 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| Prometheus 周期性 OOMKilled，重启后很久不可用 | head 里 series 太多（每条 2.2 KB 常驻）；WAL 大 ⇒ 重放久 ⇒ 循环 | `prometheus_tsdb_head_series`；`/api/v1/status/tsdb` 的 top label value；WAL 目录大小 | 丢毒标签、预聚合、拉长 interval；**不要先加内存** | 给 `head_series` 设预算告警（可用内存 ÷ 2.2 KB × 70%）；`sample_limit` 兜底 |
| 发布后 `scrape_samples_scraped` 涨 5–10 倍 | 新增 label 或 histogram 桶，series 是**乘积**不是加法 | 对比发布前后的 `scrape_series_added` 与 `/api/v1/status/tsdb` | `metric_relabel_configs` 的 `labeldrop` / `replace` | instrument 层白名单（§48.5 的 Go 片段）；发布检查单加"series 增量 < 10%" |
| `up == 0` 批量响，但服务正常 | `sample_limit` 触发或 `scrape_timeout` 超时，整次抓取被丢 | `prometheus_target_scrapes_exceeded_sample_limit_total`、`scrape_duration_seconds` | 临时提 limit 或减 series；修 exporter 慢查询 | **该计数单独告警**，不要只看 `up` |
| remote_write 排队，WAL 目录越来越大 | 后端慢 ⇒ shard 打满 ⇒（较新版本）阻塞 WAL 读取 | `prometheus_remote_storage_samples_pending` 持续 > 0；`..._shards` 顶到 `max_shards` | 先降后端压力/降基数；必要时扩 `max_shards`（但要算后端容量） | `pending` 与 `shards==max` 做成告警；后端按"2 h WAL 窗口"做可用性预算 |
| remote_write 样本悄悄少了 | 队列满后丢弃，只记 `..._samples_dropped_total` | 该计数器 ÷ `..._samples_in_total` | 降基数/扩后端；或前置换 Collector + Kafka | 监控"投递成功率 = 1 − dropped/in" |
| Collector 内存锯齿、偶发 OOM | `send_batch_size × queue_size × 每记录字节`（默认可达 GB 级） | `otelcol_exporter_queue_size/capacity`、`otelcol_process_runtime_heap_alloc_bytes` | 减 `queue_size` / `send_batch_max_size`；调 `memory_limiter` | 用 §48.6 公式反推内存预算，并设 `GOMEMLIMIT` |
| span 数对不上账，且没人报警 | 队列满直接丢（`block_on_overflow: false`），拒绝发生在重试之前 | `otelcol_exporter_enqueue_failed_spans`、`otelcol_processor_refused_spans`、`otelcol_receiver_refused_spans` | 开 `block_on_overflow` + 持久化队列 + 上游限速 | 三个"拒绝/失败"计数全部告警；对账"产生数 vs 落库数" |

---

## 48.11 观测：六个数字定位 90% 的可观测底座故障

| 指标 | 怎么读 | 版本 / 开关前提 | 拐点（越线就动手） | 它度量的是哪件物理事实 |
| --- | --- | --- | --- | --- |
| `prometheus_tsdb_head_series` | 每实例活跃 series 数，**本篇第一指标** | 2.x/3.x 均有 | > 可用内存 ÷ 2.2 KB × 70%（16 GiB ≈ 350 万） | head 的内存需求（§48.8 账一） |
| `scrape_samples_scraped` / `scrape_series_added` | 每 target 规模与"新长出来的 series" | 每 target 自动注入，无需开关 | 单 target > 2 万样本；`series_added` 5 分钟 > 500 | 抓取侧放大（§48.4） |
| `prometheus_target_scrapes_exceeded_sample_limit_total` + `up` 比例 | 有多少抓取被 limit 丢或超时 | `sample_limit` 必须显式配置才可能触发 | 任何非零增量 | 抓取在"整次丢"（不是慢，是没了） |
| `prometheus_remote_storage_samples_pending` / `_shards` / `_dropped_total` | 积压、分片顶格、是否在丢 | 2.x/3.x 均有；3.x 对指标名做过整理，**以版本文档为准** | `pending > 0` 持续 5 分钟；`shards == max`；`dropped` 增速 > 0 | remote_write 背压（§48.4） |
| `otelcol_exporter_queue_size ÷ capacity`、`otelcol_exporter_enqueue_failed_*`、`otelcol_processor_refused_*` | 队列水位与三处丢点 | 细粒度内部指标需 `service::telemetry::metrics::level: detailed`（版本相关） | 水位 > 0.5；任何 failed/refused 增量 | Collector 是否在丢（§48.6） |
| `prometheus_engine_query_duration_seconds` + `prometheus_rule_group_iterations_missed_total` | 查询/规则是否变慢，槽位是否不够 | 后者需规则已配置 | P99 > 10 s；`iterations_missed` 有增量 | 查询成本是否越界（§48.8 账三） |

**告警骨架（PromQL，指标名按目标版本核对）**

| 告警 | 表达式 | 阈值与前提 |
| --- | --- | --- |
| 基数逼近内存墙 | `prometheus_tsdb_head_series > 0.7 * (node_memory_limit_bytes / 2252)` | 2,252 B ≈ 2.2 KB；0.7 留查询余量 |
| 抓取在整次丢弃 | `rate(prometheus_target_scrapes_exceeded_sample_limit_total[5m]) > 0` | 需 `sample_limit` 已配置，否则永远为 0（**假绿**） |
| Collector 队列将满 | `otelcol_exporter_queue_size / otelcol_exporter_queue_capacity > 0.5` | 需 detailed 级内部指标 |
| Collector 在丢 | `rate(otelcol_exporter_enqueue_failed_spans[5m]) > 0 or rate(otelcol_processor_refused_spans[5m]) > 0` | 三种信号后缀不同：`_spans`/`_metric_points`/`_log_records` |
| 监控自身存活 | `up{job="prometheus"} == 0` **且**有一个异栈探针 | **必须有第二个系统看着它**，否则它挂了没人报 |

```text
# 「谁看着监控？」的三层防呆（缺一层就等于没有监控）
① 异栈探针：用一个完全独立的系统（别家 vendor / 另一个 Region 的静态 agent /
   每分钟 curl 一次 /-/ready 并写日志）判断"Prometheus 还活着吗"
② 心跳指标：业务侧每分钟写一条 deadman 指标打到外部，外部看"心跳停了 3 分钟"就报警——
   这条信号的价值在于：**它是唯一不依赖被监控系统自身的信号**
③ 预算告警：head_series / 磁盘 / 配额都用"绝对数值告警"，不要只用"增长率告警"——
   因为增长率告警在"基数阶跃"时恰好迟钝（阶跃不是增长）
```

---

## 48.12 一页速记

| 记什么 | 具体结论 |
| --- | --- |
| 形态一句话 | 指标 = **写入时聚合掉的数值序列**；`(name, labels)` 是身份；label set 就是 schema |
| 两个敌人 | 后端：**标签基数**（乘法）；管道：**背压**（有限队列 + 默认丢） |
| TSDB 三层 | 内存 head（series + chunk + postings + symbols）→ 2 h 不可变 block（mmap）→ WAL（128 MiB/段） |
| 内存公式 | 每 series ≈ **2.2 KB**（1.7–2.6）；可承载 ≈ `可用内存 ÷ 2.2 KB`；interval ÷3 ⇒ 内存 ×3 |
| 磁盘公式 | `日增 ≈ series ÷ interval × 1.3 B × 86400`；保留期上再乘写放大 2–3× |
| 查询公式 | `样本数 = 命中 series × (范围 ÷ step)`；**这是"超时"的唯一真实原因** |
| 基数铁律 | label 取值必须有**硬上界且很小**；`pod_name`/`instance`/`user_id`/`request_id`/`ip`/`path` 是毒标签 |
| 降基数三招 | 预聚合（recording rule）/ 丢弃（`labeldrop`）/ 折叠（超长值 → `other`）；**白名单，不要黑名单** |
| 抓取纪律 | `scrape_timeout < scrape_interval`，**超时不补抓**；`metric_relabel` 省内存不省抓取开销 |
| remote_write | 队列满要么丢（`_dropped_total`）要么**阻塞 WAL**；WAL 只有约 2 h ⇒ 后端不可用超 2 h 必丢 |
| shard 数 | `样本速率 ÷ (max_samples_per_send ÷ 单批延迟)`；顶到 `max_shards` 就是排队 |
| Collector 三层缓冲 | SDK 队列 / `batch`（8192）/ `sending_queue`（1000 批）；`8192 × 1000` 就是可缓冲条数上限 |
| memory_limiter | **软限 = limit − spike**（spike 默认 20%）；越软限返回**非永久错误**；必须放 pipeline 第一位 |
| Collector 默认丢 | `block_on_overflow: false` ⇒ 队列满**直接丢**，且发生在重试之前；只有 `enqueue_failed` 计数 |
| 长期存储 | 必须换形态：块进对象存储、ingester 分片 + 租户限额、查询靠预聚合 + downsampling |

**如果只记五句：**

1. **指标后端的内存账是「series 数 × 2 KB」，不是「最近多少条数据」。** 压内存只有两个旋钮：**降基数**、**拉长抓取间隔**；加内存买的是死亡线，不是安全。
2. **标签是乘法，而且乘数随发布累积。** `pod_name` 乘上发布次数、`route` 里混进 ID、histogram 自带 14 倍——每一个都"只是多一列"，合起来就是一场 OOM。判据只有一条：**取值有硬上界且很小才配进标签。**
3. **采集管道的默认行为是"丢"，不是"等"。** `sending_queue` 满了直接拒绝（`block_on_overflow: false`），且拒绝发生在重试逻辑之前；三处队列（SDK / batch / sending_queue）任何一处满都是丢，**只有最后一处有指标**。
4. **remote_write 不是备份，是同一个乘法搬到另一台机器。** 队列满会阻塞 WAL，而 WAL 只有约 2 小时窗口——**后端挂 2 小时，你的本地数据也会跟着丢**。
5. **「查询超时」永远是 `series × (范围 ÷ step)` 这一个乘法。** 加机器搬不动那 860 亿个数；能搬动它的是**预聚合（÷6,000）** 与**放大 step（÷5）**，两者都是 0 元。

---

## 48.13 自检题（5 题）

**Q1.** 16 GiB 的 Prometheus 稳态 40 万 series、15 s 抓取；发布后一小时 `head_series` 变 680 万，每 40 分钟 OOMKilled。有人说「先加到 64 GiB 撑过这个版本」。加内存买到多少时间？正确处置顺序与验证指标是什么？

<details><summary>答案</summary>

**算内存**：每 series ≈ 2.2 KB ⇒ 680 万 ≈ **15 GB**；16 GiB 留 70% 给 head 约 11 GB ⇒ 上限约 **500 万 series**，已越线，OOM 必然。加到 64 GiB 上限约 **2,000 万**，但基数还在随发布累积，所以只买到"寿命延长"（事故里 16 → 32 GiB 是把 40 分钟延到 82 分钟），而且下次 WAL 更大、重放更久、不可用窗口更长。

**处置顺序**：① **先止血**——`labeldrop` 掉 `pod_name`（`/-/reload` 生效，不必重启），必要时把该 job 的 `scrape_interval` 临时拉到 60 s；② **定位**——`GET /api/v1/status/tsdb` 看 `seriesCountByLabelValuePair`，**不要**用 `count by (__name__)` 全量扫；③ **改 instrument**——`pod_name` 归 traces/logs，要"按 pod"就在 recording rule 里合成 `sum by (service)`；④ **兜底**——每 target 设 `sample_limit`，并为该计数单独告警（否则触发时 `up == 0`，看着像 target 挂了）。

**验证**：`head_series` 回基线 ±10%、`scrape_duration_seconds` < 1 s、重启重放时间、发布前后 `scrape_series_added` 增量（设成门禁）。量级结论，非承诺值。

</details>

**Q2.** Collector 的 `sending_queue` 用默认值（`queue_size: 1000`、`block_on_overflow: false`），`send_batch_size: 8192`。后端挂 23 分钟，span 速率 45,000/s、每 span 400 B。请算缓冲上限、丢失量、内存，并解释为什么"加 Collector 副本"更糟。

<details><summary>答案</summary>

**缓冲上限** `8192 × 1000 = 819 万 span`（队列按批计）；**产生量** `45,000 × 1,380 = 6,210 万`；**必然丢弃** `≈ 5,400 万`（**87%**）。
**内存** `819 万 × 400 B ≈ 3.3 GB` 载荷，加 batch 组装与 exporter 复制（2–3×）⇒ 堆占用 **6–10 GB 量级**；所以 `memory_limiter` 硬限 4 GiB 时**队列还没满就先被拒**——事故里的"双重丢"（`enqueue_failed` + `refused`）就是这么来的。
**时间尺度** 队列 180 秒填满，而 `max_elapsed_time` 只有 300 秒 ⇒ 后端 5 分钟恢复也已到"重试预算耗尽即丢"边缘。
**为什么加副本更糟**：下游同一个后端，`N` 副本 = `N` 份并发 + `N × queue_size` 内存，对后端**总供给**零提升，而所有副本一起重试（5 s 起、×1.5、上限 30 s）——**重试流量本身成了负载**（与 [42 对象存储与云盘](42-object-storage-and-cloud-disk.md) §42.4 同构）。
**正确做法**：`block_on_overflow: true` + `sending_queue.storage: file_storage` + 按拒绝率主动降采样（尾采样优先保错误与慢请求）+ 三个丢点全部告警。量级结论，非承诺值。

</details>

**Q3.** 一条看板查询 `sum by (pod_name) (rate(http_requests_total[5m]))`，范围 30 天、step 60 s，长存储里有 1,200 万条相关 series。请算样本数，给出三种改法（按性价比排序），并说明为什么加 querier 无效。

<details><summary>答案</summary>

**样本数** `12,000,000 × 43,200 = 5.2 × 10¹¹`；上限量级 5×10⁷ ⇒ **超 1 万倍**，不是慢，是不可能。

| 改法 | 效果 | 成本 |
| --- | --- | --- |
| ① recording rule 预聚合到 `sum by (service, route, status)` ≈ 2,000 series 并丢弃 `pod_name` | 1,200 万 → 2,000 = **÷6,000** | **0 元** |
| ② step 60 s → 5 m（÷5）+ 范围 30 天 → 7 天（÷4.3） | 再 ÷21 | 0 元，只损失分辨率/跨度 |
| ③ 原始层 30 天 / 5 m 粒度 180 天 / 1 h 粒度 24 个月（downsampling） | 存储 −64%，查询常走降采样层 | 0 元 + 一点后台作业 |

合起来 `2,000 × 2,016 ≈ 4 × 10⁶` 个样本，进入舒适区。**加 querier 无效**的原因：查询成本主体是**要解码的样本数**（§48.3 第 4 跳），不随并行度下降——18 台机器只是把"搬 5,200 亿个数"分给 18 个人。能降样本数的只有 **series 数**与 **step** 两个旋钮。量级结论，非承诺值。

</details>

**Q4.** 老板问：「已经有 6 个 Prometheus 了，为什么还要给 long-term storage 付钱？各自把 retention 调到 1 年不就行了？」

<details><summary>答案</summary>

① **内存与保留期无关，与基数有关**：retention 只决定盘上留多少 block，head 里的 680 万 series 一样占 15 GB（§48.8 账一）。② **磁盘与查询会一起死**：15 天 51 GB 量级，1 年就是 **1.2 TB 量级**（含写放大更高），而查询要在 **4,000+ 个 2 h block** 上做索引查找，`--query.max-samples` 会先把大范围查询拒掉。③ **没有 HA、没有租户限额、没有降采样**：6 个实例是 6 份**各自有缺口**的数据（超时不补、重启丢 2 h），不是 6 副本；也没有机制阻止某个团队再加一个 `user_id` 标签打穿集群。
**可评审的数字**：`480,000 samples/s × 1.3 B = 54 GB/天`，三副本 **161 GB/天**，13 个月 ≈ **61 TB**；乘对象存储单价（§42.8 量级）后与"6 台机器各配 2 TB SSD × 1 年 + 各自有人管"比，通常存储更便宜，而治理（限额与降采样）才是买不到的东西。量级结论，非承诺值；选型归 `→ eng/35`。

</details>

**Q5.** 团队说：「我们把 remote_write 队列调到 `capacity: 100000`、`max_shards: 1000`，后端抖动时就不会丢数据了。」请指出三处错误，并给出正确的"不丢"方案。

<details><summary>答案</summary>

**错误一：调大的队列在内存里，不在盘上。** `capacity × shards` 直接吃 RAM，**进程一 OOMKilled 就全丢**；而这些内存是从 head 抢来的——**你在用"更容易 OOM"换"可能不丢"**。
**错误二：队列救不了 WAL 那条 2 小时死线。** 较新版本队列满则**阻塞 WAL 读取**（不再丢样本），但 WAL 只留约 2 小时；超过就随截断**永久丢失**。调大队列什么也没改变。
**错误三：`max_shards: 1000` 会把后端打死。** shard 是并发：1,000 个并发请求怼一个还没恢复的后端，等于用重试把它按住（与 [42 对象存储与云盘](42-object-storage-and-cloud-disk.md) §42.4、§48.6 的"扩容加剧"同构）。
**正确方案（三个必要条件）**：① **两端都持久化**——Collector 开落盘队列，或用 Kafka 做真正缓冲（`→ comp/28`：有位点、能回放、能扩分区，Collector 三样都没有）；② **可用性预算按最小缓冲窗口定**——`WAL 约 2 h`、`Collector 队列约 3 分钟`、`max_elapsed_time 300 s` 取最小，超过就必须降级（降采样/丢低价值信号）而不是硬扛；③ **把"丢"变成可观测**——`samples_dropped_total`、`samples_pending`、`enqueue_failed_*`、`refused_*` 全部告警，并监控"投递成功率 = 1 − dropped/in"。
**本质**：想要"不丢"，是在买**持久化 + 位点 + 可回放**三件东西，它们属于日志系统，不属于 TSDB，也不属于 Collector。量级结论，非承诺值。

</details>

---

## 48.14 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 对象存储为什么适合装「一次写只读」的 block，以及请求费/出网费怎么算 | [42 对象存储与云盘](42-object-storage-and-cloud-disk.md) §42.3、§42.7、§42.8 |
| 重试放大与限流：`min_backoff`/`max_backoff` 与"扩容加剧"同源 | [42 对象存储与云盘](42-object-storage-and-cloud-disk.md) §42.4 |
| 表格式如何把对象存储上的一堆文件变成可查询的表（长期指标层的近亲） | [35 Iceberg / 表格式](35-iceberg-table-format.md) §35.3、§35.6 |
| 连接池与超时链：抓取与 remote_write 的 HTTP 客户端不该用默认超时 | [44 连接池与客户端](44-connection-pool-and-client.md) §44.6 |
| 倒排索引与列存：postings 的磁盘版（`→ comp/21`、`→ comp/22`）；高基数维度查询该去的列存（`→ comp/20`） | `→ comp/21`、`→ comp/20` |
| 只追加日志的语义：Collector 后面挂 Kafka 才能买到"位点 + 回放" | `→ comp/28`、`→ comp/29` |
| 网关层的重试风暴与超时预算（"扩容加剧"的第三种同构） | [39 入口网关与负载均衡](39-gateway-and-load-balancing.md) §39.5、§39.6 |
| 组件故障谱：本篇三起事故在「慢 / 满 / 脑裂 / 假活 / 语义漂移」里的归类 | `→ comp/52` |
| 解剖卡八问、三本放大账与三条纪律（版本常量 / 指标前提 / 量级结论 vs 承诺值） | `→ comp/0-summary` §0.4、§0.7 |
