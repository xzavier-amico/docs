# 25. 容量与故障：mapping 爆炸、堆、merge 与零停机重建

> **状态**：`已完成（初稿）` ｜ **所属**：第五部分 · 检索与向量 ｜ **编号**：25 / 53
> **本篇定位**：21 篇拆到字节、22 篇拆到段、23 篇拆到分片、24 篇拆到查询执行，本篇把镜头拉回**运维者的仪表盘**：ES 真正会死于什么。它不复述段机制（→ [22 段：不可变、refresh、flush 与 merge](22-segment-and-near-realtime.md)）也不复述分片判据（→ [23 集群形态](23-es-cluster-shards-replicas.md) 的 §23.6、§23.8），只回答两个问题——**容量为什么会三条线一起失控**，以及**失控之后唯一的体面出路为什么是「别名 + 零停机重建」**。段数机制归 22，分片数判据归 23，查询侧深分页与 fielddata 归 24；本篇负责把它们缝成一张「三条线 × 拐点 × 救法」的运维地图，并把重建写成一个能照着执行、能演练、能回滚的七步剧本。
> **读法**：25.3 的三条线表是全篇支点（段数 / 字段数 / 堆，三者的乘数关系决定了任何一个单独优化都是白做）；25.4 讲清「为什么 mapping 爆炸卡死的是 master」（这是最反直觉、也是唯一无解只能重建的一条）；25.9 的五个算例带完整假设清单，换假设就能复算，评审只看这一节也够；25.8 的七步剧本 + 双写判定表可以直接当发布手册用；25.12 的指标表每条都带版本与开关前提。

---

## 25.1 引子：三起「查得挺快，但集群已经快死了」的故障

容量事故的共同特征是**曲线先坏、体感后坏**：P99 在 200 ms 上下晃了两个月，`docs.count` 天天涨、没人把它和三个月后的停服联系起来。等业务方开始投诉，能做的只剩重建。

### 事故一：动态映射把字段从 200 涨到 1.9 万，master 每 10 分钟一次长 GC（2025-12）

某 SaaS 可观测平台的日志集群：8 个数据节点（16C64G + 2 TB NVMe）+ 3 个专职 master，索引模板是 2021 年留下的，里面只有一行 `"dynamic": true`（这本来就是默认值，写与不写一样）。接入 SDK 把 HTTP 头、trace tag、容器 label 原样展开成字段，某个业务方还把 traceID 前缀当属性名传上来了。

| 时间 | 单索引字段数 | cluster state | master old GC | 写入拒绝率 | 查询 P99 |
| --- | --- | --- | --- | --- | --- |
| 12-01 上线 | 210 | 0.8 MB | 120 ms/次 | 0% | 70 ms |
| 12-08 | 4,100 | 6 MB | 900 ms/次 | 0.3% | 90 ms |
| 12-19 | 1.1 万 | 18 MB | 4.2 s/次，每 ~10 min 一次 | 7% | 210 ms |
| 12-27 | **1.9 万** | 31 MB | 9.5 s/次 | 22% | 480 ms |
| 12-28 03:10 | 同上 | 31 MB | 12 s/次 | **40%** | 1.4 s |
| 12-28 06:40 | 冻结 mapping（模板改 `dynamic: strict`） | 31 MB（存量改不了） | 5 s/次 | 12% | 700 ms |
| 12-28 ~ 01-02 | 重建 3 个主索引 + 归档 6 个历史索引 | < 5 MB | 300 ms/次 | 0% | 110 ms |

推导链只有一句：**字段数 → mapping 大小 → cluster state 大小 → 每次状态变更的广播字节数 × 节点数**。`_cluster/state` 里每个索引的 mapping 是**全量**存的，而每次状态变更（mapping 变更、分片分配、水线事件）都要把**全量状态**发给**所有节点**并等 ack。19,000 个字段的 mapping 序列化后 1.4 MB、堆内对象放大 5~10 倍，3 个 master 每次都要重新反序列化一遍，30 s 量级的 publish timeout 一旦被撞上，节点就被判失联 → 触发分片再分配 → 又一批状态变更。**这是一个 master 侧的正反馈，数据节点只是受害者。**

代价：peak 4 小时写入被拒 40%（约 4,100 万条日志丢失，重要告警恰好在那个窗口里没进来——**可观测平台自己先瞎了**），重建 5 天，期间两个业务方的检索能力降级为"只搜最近 2 小时"。

### 事故二：批量导入没关 `refresh_interval`，一天造出 3,000 段/分片（2026-01）

商品搜索 `products_v2` 首次接入 1.1 亿 SKU。运维写了一个 20 并发、每批 5 MB 的灌数脚本，目标是"14 小时灌完"。索引设置里 `refresh_interval` 是线上值 `1s`——没有人想到"灌数"和"在线查询"对这两个字的需求正好相反。

| 时间 | 段数/分片（峰值） | 单分片 fd | merge `current` | 查询 P99 | 现象 |
| --- | --- | --- | --- | --- | --- |
| 灌数前 | 34 | ~120 | 0 | 60 ms | 稳态 |
| 灌数 1 h | 380 | 1,300 | 3 | 240 ms | merge 队列开始积压 |
| 灌数 4 h | 1,600 | 5,400 | 4 | 780 ms | 搜索线程池 `queue` > 600 |
| 灌数 8 h | **3,000** | 9,000 | 4 | **1.2 s** | `too_many_open_files`，写入成片失败 |
| 灌数 11 h | 脚本崩 | — | — | — | `ulimit -n` 65,536 打满 |
| +6 h | 灌完（补跑），`_forcemerge` 未做 | 3,000 | 0 | 1.1 s | 索引"能搜但慢" |

推导：`refresh_interval: 1s` ⇒ 段产生速率 `r = 1 段/秒/分片`；灌数 11 小时理论产生 `11 × 3,600 = 39,600` 段/分片，merge 吃掉绝大部分后**观测峰值 3,000 段/分片**——反过来推 merge 的平均滞留时间 `T_reside ≈ 3,000 s = 50 min`（`N_seg = r × T_reside`）。代价：导入延后 6 小时，期间线上搜索 P99 从 60 ms 到 1.2 s 持续 18 小时，运营侧新品上架延迟被投诉 2,300 次。

正确的组合只有四个动作：导入索引 `refresh_interval: -1`、`number_of_replicas: 0`、单批 10~30 MB、灌完 `_forcemerge?max_num_segments=1` 再恢复 `30s` 与副本。

### 事故三：想改一个字段类型，团队「删索引重建」，线上停服 5 小时 10 分（2025-11）

订单搜索 `orders_v3` 的 `amount` 早期被建成 `text`（金额按字符串全文索引，纯属误会）。要改成 `scaled_float`，`PUT /orders_v3/_mapping` 直接报：

报错只有一行：`mapper_parsing_exception: mapper [amount] cannot be changed from type [text] to [scaled_float]`——这不是"权限不够"或"姿势不对"，是**类型固化**。

改不了怎么办？值班同学的动作是 `DELETE /orders_v3` 然后 `_reindex`。但**索引名是写死在代码里的字面量**（`orders_v3`），没有别名——删掉的那一刻，所有搜索请求 404，而客户端把 `index_not_found_exception` 当"无结果"吞掉了，所以监控面板上"错误率 0%"，只有"零结果率"从 3% 跳到 96%。

| 时间 | 动作 | 服务状态 | 影响 |
| --- | --- | --- | --- |
| 11-14 14:20 | `DELETE /orders_v3` | **搜索全挂**（404 被吞成空结果） | 从第一秒起不可用 |
| 14:52 | 发现"零结果率 96%"，开始排查 | 同上 | 38 分钟无人察觉 |
| 15:30 | 建 `orders_v3_new` + 从 MySQL reindex（限速 3,000 docs/s） | 同上 | — |
| 19:20 | reindex 完成，`_count` 校验通过 | 同上 | — |
| 19:30 | 把索引改名回 `orders_v3`（先 `_close` 再改别名式硬改） | 恢复 | **停服 5 小时 10 分** |

代价：62 万次搜索返回空结果，客服工单 1,400 件，运营手工兜底查库 3.5 小时。**复盘结论不是"要用别名"，而是"字段类型改了只能重建"这件事，在 22 篇就写死了；没写别名，等于把 1 秒的回滚能力换成了 5 小时的停服。**

三起事故分别对应本篇三节：**mapping 爆炸与集群状态（25.4）**、**段数与 merge 的 IO 账单（25.5）**、**零停机重建的七步剧本（25.8）**。

---

## 25.2 一句话定位与唯一真问题

**定位**：ES 的运维对象不是"查询延迟"这一个数字，而是**三条互相乘起来的线**——段数（每分片段数）、字段数（mapping 大小）、堆占用。查询快慢只是这三条线健康时的副产品；三条线一破，查询延迟、写入拒绝、节点掉线会同时到来，而且**修不回去**。

### 唯一真问题

> **ES 的 SLO 不是「查询快」，而是「段数、字段数、堆占用三条线不失控」——这三条线一旦破了，救回来的代价约等于重建；所以所有 mapping / 分片 / 分析器的错误，最终都要用「别名 + 零停机重建」来还。**

这句话的两个半句各自推出本篇一半的篇幅：

| 半句 | 机制 | 推出什么 |
| --- | --- | --- |
| 三条线一旦破了救不回来 | 段不可变（→ comp/22）、字段类型固化在 `.fnm`（→ comp/21）、分片数是路由分母（→ comp/23）——**这三个都是"创建时写死、事后只能重建"的量** | 所以容量工作的核心不是"调参救火"，是**不越线**，以及**随时具备重建能力** |
| 一切错误都用重建来还 | 重建必须零停机 ⇒ 必须有别名；必须不丢数据 ⇒ 必须有双写/CDC + 追平判据；必须能回滚 ⇒ 必须保留旧索引 | 所以别名不是"命名规范"，是**唯一 1 秒级的回滚开关**；重建是常规动作，不是应急预案 |

### 解剖卡八问：本篇答哪几问

| 八问 | 落在哪节 | 一句话答案 |
| --- | --- | --- |
| ① 承诺什么 | §25.2、§25.10 | 承诺"三条线在护栏内时可预测地提供服务"，**不承诺**会自动回收你造的债（字段、段、墓碑） |
| ② 数据长什么样 | §25.3、§25.4 | 三份物理量：段（磁盘 + 堆元数据）、mapping（**存在 cluster state 里**，不在数据节点）、堆内的运行态 |
| ③ 一次读写走哪条路 | §25.6、§25.7 | 写路径的最后一跳是**线程池队列**，队列满了就是 429；读路径的固定成本是**段数 × 每段开销** |
| ④ 并发与一致 | §25.7、§25.8 | 单分片单写者串行；跨索引一致性只能靠**双写 + 追平判据**，ES 不提供跨索引事务 |
| ⑤ 扩展维度 | §25.10 | 三条线各自只有一个方向：段数→**刷新分级 + 单分片上限**、字段数→**拆索引**、堆→**垂直切（加节点/加角色）**；加节点治不了任何一条线，详见下节 |
| ⑥ 会在哪崩 | §25.10、§25.11 | 只写系统级失效：慢（段/merge/GC）、满（水线→搬迁→IO 打满）、脑裂（老版本 quorum，→ comp/23）、假活（线程池全满）、语义漂移（dynamic mapping 让同名同段字段类型不同） |
| ⑦ 看哪几个指标 | §25.12 | 段数/分片、拒数量、heap_used_percent、old gen GC 时间、cluster state 大小（每条带版本与开关） |
| ⑧ 换掉它的信号 | §25.10 | 单分片 > 50 GB 且不可重建、分片数/节点 > 20、cluster state 更新 > 1 s、字段数逼近 limit |

### 三本放大账在容量上的落点

| 账 | 在容量上的具体形态 | 拐点量级 |
| --- | --- | --- |
| 写放大 | merge 重写字节 ≈ 日写入 × 层数 × 副本系数；`update` 再乘 2 | 日写入的 4~8 倍就是磁盘每天的真实写入量 |
| 读放大 | 每查询固定开销 × 段数/分片 × 扇出分片数 | 段数从 40 涨到 1,000 ⇒ 固定开销 ×25 |
| 空间放大 | 墓碑（未 merge）+ merge 峰值临时段 + 副本 + **重建期的 v1/v2 双份** | 峰值需要 2× 2× 数据量（重建期 + 副本） |

---

## 25.3 三条线：段数、字段数与堆

先把三条线各自的定义、指标、拐点、救法摆成一张表——**这张表就是本篇的目录**，后面每一节只展开其中一行。

| 线 | 为什么它是 SLO | 指标（详见 §25.12） | 拐点 | 破了之后怎么救 | 救得回来吗 |
| --- | --- | --- | --- | --- | --- |
| **① 段数/分片** | 查询要遍历每个段（读放大 ×N）；每段 3~90 个 fd；每段一份元数据驻留堆 | `_nodes/stats/indices/segments.count` ÷ 分片数；`_cat/segments` 按 shard 计数 | 基线 ≈ 41（推导见 §25.5）；**> 100 警告，> 300 严重，> 1,000 事故区** | 停 refresh 抖动 → 关 update 风暴 → 只读索引 `_forcemerge?only_expunge_deletes` → 严重时重建 | **慢**：回滚只能停止恶化，积压要 `段数 × 段均大小 ÷ merge 吞吐` 小时消化；且顶层段有硬地板 |
| **② 字段数 / mapping** | mapping 存在 **cluster state**；每次状态变更全量广播给所有节点并等 ack ⇒ 卡 master 与网络 | `_cluster/state/metadata` 响应字节数；`GET /idx/_mapping` 字段计数 | 单索引 > 300 警觉；`index.mapping.total_fields.limit` **默认 1000** 是硬墙 | `dynamic: strict/false`、`flattened`、拆索引；**兜底只有重建** | **不能**：已有字段的类型与 analyzer 固化在段的 `.fnm` 里，`PUT mapping` 只允许加字段 |
| **③ 堆占用** | 段元数据（∝ 段数 × 字段数）+ fielddata + 聚合中间态 + bulk/index buffer + cluster state 对象 | `_nodes/stats/jvm` 的 `heap_used_percent`、old gen GC 时间；`segments.memory_in_bytes` | 堆 ≤ 物理内存 50%；**建议 ≤ 约 30 GB（压缩指针边界，量级结论）**；持续 > 75% 即危险 | 减分片、减段数、禁 `fielddata`、限深分页与聚合、加节点/拆角色 | **部分**：GC 能缓，泄漏与乘数关系（段 × 字段）不能 |

### 三条线是乘法，不是加法

这是本篇最重要的一句话：

> **`堆压力 ≈ 段数 × 字段数 × 单位段元数据 + fielddata + 聚合中间态`**

所以"段数健康 + 字段数爆"和"字段数健康 + 段数爆"**都能单独打爆堆**，而两者同时中等也会爆。反过来说：**只优化其中一条线，另外两条会把收益吃掉**——这就是为什么"我们加了内存、也 forcemerge 了、还是慢"这类复盘会陷入僵局。

```text
                     ┌─────────── 三条线：一个正反馈 ───────────┐

  写入侧                        物理量                            账单
  ───────────────────────────────────────────────────────────────────────────
  bulk 速率                      ① 段数 / 分片                 查询固定开销 × N（读放大）
  refresh_interval ──► r = 1/间隔 ──► N = r × T_reside ─────► 文件句柄 N × 3~90
  主分片数 S                       （T = merge 滞留时间）        段元数据 N × 字段数
                                        │                              │
                                        └──► merge 重写字节 × 层数 ─► 磁盘带宽 / IOPS
                                                                       │
  动态字段数 F ──────► ② cluster state ≈ F × 60 B × 索引数 ─► master CPU / 全节点广播
  （dynamic 映射）                  │                              反序列化对象 ×5~10
                                    └──► 每段 FieldInfos ∝ F ──┐
                                                               ▼
                        ③ 堆 = 段元数据 + fielddata + 聚合 + index buffer
                                                               │
                                   堆↑ ⇒ page cache↓ ⇒ 读盘↑ ⇒ merge 变慢 ⇒ T_reside↑
                                                               │
                                                               └──► 回到 ①（正反馈闭合）
```

这张图解释了"为什么回滚配置不能恢复现场"：堆是**状态**，段数是**存量**，page cache 是**别人让出来的**。三者都不会因为你改回一行 YAML 而回到一小时前。

### 三个最容易搞错的判断

| 你以为 | 实际 | 后果 |
| --- | --- | --- |
| "段数多只是文件多，不影响性能" | 段是**查询固定成本的乘数**：每段一次 postings 遍历 + 一次 docValues 块 + 一次 liveDocs 位图 AND | P99 从 60 ms 到 1.2 s（事故二） |
| "mapping 是索引自己的事，改它不影响别人" | mapping 在 **cluster state** 里，全量广播给所有节点 | 一个索引的字段爆炸卡死整个集群的元数据面（事故一） |
| "加大堆就能扛住" | 堆多占一分，page cache 就少一分；而 Lucene 靠 page cache 读段文件 | 加到 40 GB 之后：GC 停顿变长 + 读盘变多，**两个方向同时恶化** |

形态换挡（L0 → L5、什么时候必须分集群、什么时候上可搜索快照）不在本篇展开 → `architect/17 §17.13`。

---

## 25.4 mapping 爆炸：为什么它卡的是 master 不是数据节点

### 动态映射的四步链条

ES 默认 `dynamic: true`：文档里出现一个没见过的 JSON key，就**新建一个字段并推断类型**。在"接入方可控"的场景这是便利；在"接入方是外部系统 / 用户自定义属性 / 采集端随手加 tag"的场景，这就是一个**由外部流量直接驱动的元数据写入接口**——而它的写入目标是集群里最脆弱的那份数据。

```text
  ① 文档到达（一个没人声明过的 JSON key）
     {"service":"api","labels":{"pod_id":"a7f3"},"trace_span_9f2c":"..."}   ← 采集端随手加的
              │
              ▼
  ② 数据节点：动态映射生成字段推断 {"trace_span_9f2c":{"type":"keyword"}}
              │  一个 bulk 里 200 个新 key = 200 个新字段 = 1 次 mapping 变更
              ▼
  ③ master：写进 cluster state（metadata.indices[i].mappings）
              │  落盘一份**全量** state-N.st（不是增量）
              │  计算 diff → 序列化 → 压缩（7.x 起有压缩 cluster state）
              ▼
  ④ 广播给所有节点 ─┬──────────┬──────────┬─────────  …  30 个节点都收到**全量状态**
                    ▼          ▼          ▼
                 节点1       节点2       节点3         JSON → Java 对象，堆内放大 5~10×
                    │          │          │           并且这份对象是**常驻**的
                    └──────────┴────┬─────┘
                                    ▼
  ⑤ 等全部 ack（最慢的那个决定）→ 才提交新版本 state
                                    │
                                    ▼
  ⑥ ack 超时（publish timeout 量级 30 s）⇒ master 认为节点失联 ⇒ 踢出
     ⇒ 分片再分配 ⇒ **又一串状态变更** ⇒ 返回 ③（这是事故一 03:10 那一格）
```

四个必须记住的事实：

1. **mapping 不在数据节点上**。数据节点只有段的 `.fnm`；索引级 mapping 的唯一权威是 master 的 cluster state。所以"改 mapping"是一次**集群级元数据写入**，代价与节点数成正比。
2. **每次都是全量，不是增量**。ES 的状态机是"版本号 + 全量状态"，广播的是整份 mapping（压缩后传输），不是"新增的 200 个字段"。
3. **提交要等全部 ack**。任何一个节点反序列化慢（大堆、长 GC、磁盘慢），整次状态更新就慢；反过来，状态更新慢又会加剧节点的 GC——互相喂。
4. **master 是唯一的串行点**。所有状态变更（建索引、改 mapping、分片分配、水线事件）排在同一队列里。所以字段爆炸的杀伤面**不只是这个索引的写入**——是**整个集群的所有元数据操作**，包括"挂了一台机器要重新分配分片"这种救命操作。

### 推导：1 万个字段 × 200 个索引 = 200 万条字段定义

| 项 | 推导 | 结果 |
| --- | --- | --- |
| 单一字段的 mapping JSON | `"field_name":{"type":"keyword"}` 量级；带 `doc_values`/`index`/multi-field 时更大 | ≈ 60 B（量级） |
| 单索引 1 万字段 | 10,000 × 60 B | ≈ 0.6 MB |
| 200 个索引 | 200 × 0.6 MB | **≈ 120 MB** 纯 mapping |
| 加上 settings / aliases / 路由表 | 路由表按分片拷贝 0.4~1 KB/条，这层不在算例内 | 略增 |
| 一次状态更新要广播的字节（压缩后，取 4× 压缩） | 120 MB ÷ 4 × 30 节点 | **≈ 900 MB 出流量/次更新** |
| 每个节点堆里常驻的 mapping 对象 | 120 MB × 5~10（JSON → Java 对象放大） | **≈ 0.6~1.2 GB** |
| master 额外负担 | 序列化 + diff + 落盘全量 `state-N.st` | 单次秒级 CPU |

**结论不是"网络慢"，是"这条路在数学上跑不通"**：动态映射下每一次含新字段的 bulk 都触发一次状态变更，而一次变更要 900 MB 出流量 + 30 个节点各反序列化 120 MB。一旦状态变更的**产生速率**超过消费速率，队列积压 → publish timeout → 节点被判失联 → 再分配 → 更多变更。这就是事故一在 03:10 那一步的真实机制：**不是"集群不够大"，是元数据面的带宽被外部流量劫持了**。

> 顺带解释一个常见困惑：为什么"字段数上万后，一次 mapping 更新 = 全集群卡顿"，但 `GET /idx/_mapping` 仍然很快？因为查询 mapping 是从**本地缓存的状态**读，而"更新 mapping"要走完整的广播 + ack 路径。**读元数据便宜，写元数据贵三个数量级。**

### 四种解法与它们的代价

| 手段 | 它改了什么物理路径 | 代价 | 什么时候用 |
| --- | --- | --- | --- |
| `dynamic: strict` | 未知字段 ⇒ **文档被拒**（`strict_dynamic_mapping_exception`），mapping 永不增长 | 上游加字段 = 写入失败；需要模板先行或发版 | 字段集合已知可控（商品、订单、结构化日志）；**绝大多数生产索引应默认这一条** |
| `dynamic: false` | 未知字段被**忽略、不建索引**，但仍进 `_source` | 字段"写进去了但搜不到"，是静默失败，最难排查 | 只需原样留存 + 只搜已声明字段（审计、留档） |
| `flattened` 类型 | 整个对象当作**一个**字段建索引：key 展平成 `key:value`，全部按 keyword 处理 | 失去逐字段类型（数值范围查询、日期解析都不行）；查询语法变成 `labels.pod_id:a7f3`；**字段数记账为 1** | 租户自定义属性、label、metadata —— 也就是真正会爆炸的那一类 |
| 调大 `total_fields.limit` | 只是把墙往外挪（`index.mapping.total_fields.limit` 默认 1000，可动态改） | mapping 大小照涨、cluster state 照涨、**下一次事故更大** | 几乎从不正确；只有"字段数确实固定 3,000 且已 `strict`"时才允许 |
| `runtime` 字段 | 查询时计算，不进索引 | 不能高效聚合/排序，查询更贵 | 少量低频字段 |
| 按字段集拆索引 | 不同字段集进不同索引，各自 1000 以内 | 查询要跨索引（CCS）；不能 join | 字段集天然分群（不同业务线/租户群） |

### 索引模板：把纪律写进模板，而不是写进 wiki

```json
PUT _index_template/app-logs
{
  "index_patterns": ["app-logs-*"],
  "priority": 200,
  "_meta": { "owner": "obs-platform", "why": "dynamic:strict 防字段爆炸" },
  "template": {
    "settings": {
      "number_of_shards": 6,
      "number_of_replicas": 1,
      "refresh_interval": "30s",
      "index.mapping.total_fields.limit": 2000,
      "index.mapping.depth.limit": 20,
      "index.mapping.nested_fields.limit": 20,
      "index.mapping.ignore_malformed": true
    },
    "mappings": {
      "dynamic": "strict",
      "properties": {
        "service":     { "type": "keyword" },
        "level":       { "type": "keyword" },
        "@timestamp":  { "type": "date" },
        "message":     { "type": "text" },
        "labels":      { "type": "flattened", "ignore_above": 256 },
        "http": {
          "properties": {
            "status": { "type": "integer" },
            "path":   { "type": "keyword" }
          }
        }
      }
    }
  }
}
```

三行要点：`dynamic: strict` 是**唯一能真正关掉爆炸源**的设置；`labels` 用 `flattened` 把"不可控的 key 集合"折叠成 1 个字段；`total_fields.limit: 2000` 是把默认 1000 抬到"确实需要"，**不是为了以后爆炸留空间**。`index.mapping.depth.limit` / `nested_fields.limit` / `ignore_malformed` 的默认值与可用性以目标版本文档为准（`flattened` 自 7.3 起提供，`runtime` 字段自 7.11 起）。

---

## 25.5 段数失控与 merge 的 IO 账单

段机制本身在 → comp/22 讲透了（不可变、refresh 产段、merge 消费段、`N_seg = r × T_reside`）。本节只算两笔 22 篇没算完的账：**段数的硬地板**，和**一次大 merge 到底重写多少字节**。

### 段数的硬地板：`max_merged_segment`

22 篇给了"健康稳态 ≈ 34 段/分片"这个基线，但没说这个数**依赖单分片大小**。补上这一步只需要一个观察：**顶层段不再合并**（只有删除率 > `expunge_deletes_allowed` 才会重写），所以顶层段数 = 单分片字节 ÷ `max_merged_segment`。

```text
  单分片 50 GB，max_merged_segment = 5 GB，segments_per_tier = 10

  tier 层（会被合并；稳态段数 ≈ (k−1) × 层数 = 9 × 3.4 ≈ 31 段）
  2 MB ──► 20 MB ──► 200 MB ──► 2 GB ──► 5 GB（封顶）
                                            │
                                            ▼
  顶层（**不再合并**）：段数 = 单分片字节 ÷ 5 GB = 50 ÷ 5 = 10 段   ← 硬地板
  ─────────────────────────────────────────────────────────────────────────
  段数/分片 ≈ 31 + 10 = 41      ← 这就是 comp/22 那条「基线 34」的算法来源
  单分片 3.6 TB 时：31 + 720 = 751 段/分片   ← 地板项主导，merge 再怎么跑也没用
```

两个直接推论：

1. **"段数要健康，单分片就不能太大"**——这条纪律通常被归因于"恢复时间"（→ comp/23 账四），但它同时也是**段数纪律**：段数地板 ≈ 单分片 ÷ `max_merged_segment`。所以调大 `max_merged_segment` 买的是"顶层段更少"，代价是"单次 merge 的峰值 IO 与临时空间更大"（→ comp/22 的参数表）。
2. **"forcemerge 之后段数会回涨"**——因为新数据还在写，顶层段会重新累积到 `单分片 ÷ 5 GB`。forcemerge 只对**只读索引**有意义。

### 段数 × 查询延迟 × 文件句柄：一张对照表

| 段数/分片 | 单分片查询固定开销（30 µs/段） | fd（复合文件 3 个/段） | 12 分片/节点时的 fd | 段元数据（30 字段、1 KB/段） | 典型表现 |
| --- | --- | --- | --- | --- | --- |
| 10 | 0.3 ms | 30 | 360 | 10 KB | 健康 |
| 41 | 1.2 ms | ~120 | 1,440 | 41 KB | **基线** |
| 100 | 3 ms | 300 | 3,600 | 100 KB | 开始抖，第一批告警 |
| 300 | 9 ms | 900 | 10,800 | 300 KB | P99 抬 2~3 倍 |
| 1,000 | 30 ms | 3,000 | 36,000（占 `ulimit -n` 65,536 的 55%） | 1 MB | P99 抬 10~20 倍，fd 逼近硬顶 |
| 5,000 | 150 ms | 15,000 | 180,000 | 5 MB | 事故区：节点起不来、查询超时 |

第三、四列的差别是"复合文件开关"的账（→ comp/22 §22.3）：关掉 `index.use_compound_file` 后每段 fd ≈ 字段数 × 4 + 6，30 字段就是 ~126 个/段，1,000 段/分片在 12 分片节点上要 **151 万 fd**——节点根本不是慢，是启动不了。

### 一次大 merge 重写多少字节

merge 的 IO 是"读全部源段 + 写一个新段"，且在**新段落盘、新 searcher 打开之前不能删旧段**：

| 场景 | 读 | 写 | 临时空间峰值 | 说明 |
| --- | --- | --- | --- | --- |
| 10 × 500 MB → 1 × 5 GB | 5 GB | 5 GB | ~5 GB | 顶层 merge，一次 10 GB IO |
| 同上但含 30% 墓碑（expunge） | 6.5 GB | 4.5 GB | ~4.5 GB 新增 | 副本系数 2 时全部 ×2 |
| `_forcemerge?max_num_segments=1`（单分片 50 GB） | 50 GB | 50 GB | ~50 GB 新增 | 只读索引、低峰、异步任务（→ comp/22 事故三） |

所以"磁盘要留 15%~25%"这条纪律的定量来源是**最后一行**：一次顶层 expunge merge 要先写出接近一整段的新文件。盘上没有这份余量，merge 就排不上，墓碑只增不减 —— 22 篇事故二那个正反馈死锁由此而来。

### 写入侧的四个节流阀（改的是哪条物理路径）

| 配置 | 它改的物理路径 | 批量导入时应该设成 | 代价 |
| --- | --- | --- | --- |
| `index.refresh_interval` | 段产生速率的分母（`r = 1/interval`） | **`-1`**（关闭周期 refresh） | 期间数据不可搜；导入结束必须恢复，否则 translog 一直涨 |
| `index.number_of_replicas` | 每份写入的同步复制份数 | `0`（导入完成后再调回） | 导入期无冗余；导入完恢复副本 = 一次全量复制 IO 风暴，要在低峰做 |
| `index.translog.flush_threshold_size` | flush 节拍 = 重启时要重放的字节上限 | 保持默认（`512mb` 量级） | 调大 ⇒ 重启重放小时级（→ comp/22 事故一） |
| `index.translog.durability` | 每个 bulk 是否 fdatasync | 视 RPO 要求；导入索引可放宽 | `async` ⇒ 崩机最多丢一个 sync 周期 |

批量导入的正确顺序只有一条链：

```bash
# ① 建导入专用索引：关 refresh、关副本、显式 mapping（别让它动态推断）
curl -XPUT 'es:9200/products_bulk' -H 'Content-Type: application/json' -d '{
  "settings": { "number_of_shards": 12, "number_of_replicas": 0,
                "refresh_interval": "-1",
                "index.translog.durability": "async" },
  "mappings": { "dynamic": "strict", "properties": { "sku": {"type":"keyword"},
                "title": {"type":"text"}, "price": {"type":"scaled_float","scaling_factor":100} } }
}'

# ② 灌数：单批 10~30 MB、4~8 并发；每批不 refresh（refresh_interval=-1 已保证）
#    curl -XPOST 'es:9200/_bulk?refresh=false' --data-binary @batch.ndjson

# ③ 灌完：先 forcemerge（此刻索引只读、不会有新段），再恢复副本与 refresh
curl -XPOST 'es:9200/products_bulk/_forcemerge?max_num_segments=1&wait_for_completion=false'
curl -XPUT  'es:9200/products_bulk/_settings' -H 'Content-Type: application/json' \
     -d '{ "index": { "refresh_interval": "30s", "number_of_replicas": 1 } }'

# ④ 校验三件：live 文档数、抽样逐字段、段数是否回到基线附近
curl -s 'es:9200/products_bulk/_count?preference=_primary'
curl -s 'es:9200/products_bulk/_cat/segments?v&h=shard,segment,size&s=shard'
```

**为什么必须"先 forcemerge 再恢复副本"**：反过来的话，`_forcemerge` 要重写 R+1 份拷贝，IO ×2，且期间副本还在同步追加新段，合并出来的东西又被新段污染。

---

## 25.6 堆：30 GB 边界与堆内堆外

### 为什么建议堆不超过约 30 GB

两个独立的原因，方向一致：

1. **压缩指针（compressed oops）的门槛**：64 位 JVM 在堆不超过约 30~32 GB 时，可以把对象引用从 8 字节压成 4 字节的偏移量，**同样一份数据占的堆少 20%~30%**。一旦越过这条线，引用变宽，对象变大，**"32 GB 堆"可能比"31 GB 堆"能装的段元数据更少**——加内存反而更差。这条临界值的精确位置由 JVM 实现的地址压缩方案决定，随版本浮动，实测常落在 30~32 GB 之间，所以工程写法是"**约 30 GB，以 JVM 与版本为准**"。
2. **page cache 要让位**：Lucene 读段文件走的是 `mmap`，命中的是 **OS page cache 而不是堆**（→ comp/22 §22.3 的段文件表、→ comp/21 的词典 FST）。堆多占 1 GB，page cache 就少 1 GB，于是每次查询从"内存读"变成"随机读盘"。所以通行规则是 **堆 ≤ 物理内存 50%**：64 GB 机器 → 31 GB 堆 + 约 33 GB page cache。

| 规则 | 数值 | 依据 | 违反的后果 |
| --- | --- | --- | --- |
| 堆 ≤ 物理内存 50% | 64 GB 机 → 31 GB | 另一半是 page cache，段文件靠它 | 每次查询都读盘，P99 抬 2~5 倍 |
| 堆 ≲ 约 30 GB | 31 GB | 压缩指针边界（量级结论，随 JVM 版本） | GC 停顿变长 + 有效容量反而下降 |
| `-Xms = -Xmx` | 相等 | 避免堆动态伸缩带来的停顿 | 冷启动后持续抖动 |
| `bootstrap.memory_lock: true` | 开启 | `mlockall` 防堆被 swap 出去 | 一次换页 = 数百 ms 停顿，看起来像"神秘长 GC" |
| 堆必须装下**最坏单次查询** | 由聚合/深分页预算决定 | OOM 几乎从不来自总量，来自某一发请求 | 熔断器先触发 429，再不行就 OOM 被踢出集群 |

`bootstrap.memory_lock: true` 有两个常踩的坑：需要 `ulimit -l unlimited`（或容器 `memlock` 限制放开），配不好**节点直接起不来**；K8s 里要确认容器没有被 `limits` 卡住 mlock，否则表现为启动日志里一行 `Unable to lock JVM Memory`。

### 堆内装什么、堆外装什么

| 位置 | 装什么 | 量级 | 谁能让它涨 | 观测 |
| --- | --- | --- | --- | --- |
| 堆内 | **段元数据**：每段的 FieldInfos、词典 FST 引用、`.si` | 每段 ≈ 1 KB + **字段数 × 30 B** | 段数 × 字段数（乘数！） | `segments.memory_in_bytes` |
| 堆内 | **fielddata**（text 字段排序/聚合时的倒排→正排） | 单字段可达 GB 级 | 给 `text` 开 `fielddata: true` | `_cat/fielddata` |
| 堆内 | **聚合中间态**（terms 桶、cardinality 草图） | 分片数 × 桶数 × 条目 | 深分页、大聚合、高基数 terms | 熔断器 `indices.breaker.request` |
| 堆内 | **index buffer** | 堆的 10%（节点级共享池） | 活跃写入分片数 | `indices.memory.index_buffer_size` |
| 堆内 | **线程池队列持有的请求体** | 队列长度 × 单请求字节 | `queue_size` 调大（事故放大器） | `thread_pool.*.queue` |
| 堆内 | **cluster state 对象** | mapping × 5~10 | 字段数、索引数、分片数 | `_cluster/state/metadata` |
| 堆外 | **page cache**（段文件 mmap） | 尽量大 | 任何读操作都会填它 | `node_memory_Cached_bytes` |
| 堆外 | JVM 自身（metaspace、code cache、GC 结构、线程栈） | 1~2 GB | 插件数、线程数 | `jvm.mem.non_heap_used_in_bytes` |
| 堆外 | 网络缓冲、translog 写缓冲 | 百 MB 量级 | bulk 大小与并发 | `iostat`、`network.tcp` |

一句话总括：**堆装"运行态"，page cache 装"数据本身"**。所以"堆不是越大越好"不是玄学，是分工——而且这个分工在 `mmap` 这条路径上是**硬约束**：堆吃了 page cache 的内存，Lucene 没有第二条路。

### 段元数据的乘数：31 GB 堆装得下多少段

数据前提：堆上限 31 GB（约 30 GB 边界之下），段元数据每段"约 1 KB"——但那个 1 KB 是**字段数 30 量级时**的值。真实的式子是：**每段元数据 ≈ 1 KB（每段的固定部分：FieldInfos 保护壳、`.si` 摘要、reader 对象）+ 字段数 × 30 B（每字段在每段上都有一份 FieldInfo 与 norms/docValues 元数据引用）**。

| 单索引字段数 | 每段元数据（量级推导） | 10 万段的元数据总量 | 占 31 GB 堆 | 判决 |
| --- | --- | --- | --- | --- |
| 30（正常） | 1 KB + 0.9 KB ≈ **2 KB** | 200 MB | 0.6% | 段数不是堆的问题 |
| 1,000 | 1 KB + 30 KB ≈ **31 KB** | 3.1 GB | 10% | 开始挤压 fielddata 与聚合 |
| 3,000 | 1 KB + 90 KB ≈ **91 KB** | 9.1 GB | **29%** | 危险：加上聚合与 buffer 就是 OOM |
| 20,000 | 1 KB + 600 KB ≈ **600 KB** | 60 GB | **195%** | 装不下，节点起不来 |

这张表是"三条线是乘法"的定量证明：**单看 10 万段的元数据只有 200 MB，占堆 0.6%，完全无害；一旦字段数是 3,000，同一批段要吃掉 29% 的堆。** 所以"段多导致 OOM"这句经验只有在"字段数也不小"时才成立，而复盘时如果不拆开这两个因子，就会得出"要多加内存"这种错误结论。

---

## 25.7 写入拒绝：线程池队列就是你的 SLO

### 一条写请求的最后一跳是队列

23 篇的写路径图里有一格："write 线程池排队（队列积压 = 429 之源）"。展开它就只有三句话：

- 每个节点有一组**固定大小**的 `write` 线程（量级 = CPU 核数），队列 `queue_size` 默认 **10,000**；
- 队列满了 ⇒ ES 立刻返回 `429 Too Many Requests`，日志里是 `es_rejected_execution_exception`；
- **拒绝是本地决策、立即返回**，所以它比你所有下游指标都灵敏：磁盘 util 还在 60%、GC 还很健康的时候，`rejected` 可能已经在涨了。

### 算账：队列买的是 25 秒，不是吞吐

| 项 | 推导 | 结果 |
| --- | --- | --- |
| 单节点 write 池并发 | 8 核 | 8 个在跑 |
| 单 bulk 处理耗时（5 MB、30 字段、NVMe） | 实测 | ≈ 5 ms |
| 单节点 bulk 吞吐上限 | 8 ÷ 0.005 s | **1,600 bulk/s** |
| 实际到达 | 20 并发客户端、每客户端 100 bulk/s | 2,000 bulk/s |
| 每秒净积压 | 2,000 − 1,600 | 400 请求/s |
| 队列 10,000 被打满 | 10,000 ÷ 400 | **25 秒** |
| 打满之后 | 每个新请求直接 429 | 客户端重试 → 到达量再涨 → 雪崩 |

**所以"把 `queue_size` 从 10,000 调到 50,000"等于把 25 秒的缓冲变成 2 分钟**，代价是队列里那 50,000 个请求体全在堆里（每请求 5 MB 就是 250 GB，先 OOM 再拒绝）。队列是**缓冲器不是扩容器**：它只改变"拒绝发生的时刻"，不改变"拒绝一定发生"。

### bulk 大小与并发：起点与判据

| 参数 | 经验起点 | 为什么是起点而不是定律 | 调大的代价 | 调小的代价 |
| --- | --- | --- | --- | --- |
| 单批字节 | **5 MB**（或 500~1,000 文档，先到者触发） | 单批的解析、路由、请求体缓存都在协调节点堆里；5 MB 是"摊销请求开销"与"不撑爆堆"的折中 | 协调节点堆压力；一个分片慢拖整批（队头阻塞）；429 时整批重试 | 每请求固定开销占比上升，段更碎 |
| 客户端并发 | **5 个**（每客户端） | 并发 × 单批字节决定节点上的瞬时内存与队列占用 | 队列积压、429、重试放大 | 单分片写不满，段数不降但吞吐上不去 |
| 每分片每秒 bulk | 由 `分片数 × 单分片速率` 决定（→ comp/23 账二） | 这才是真正的上限，前面两个数只是逼近它的手段 | — | — |

**判据（四条全过才算调对了）**：① bulk 响应 P99 < 500 ms（不含 refresh 等待）；② `thread_pool.write.rejected` 增量 = 0；③ 目标节点 CPU 60%~70%（不是 100%）；④ 段数/分片不涨。调优时**一次只动一个变量，每轮跑 15 分钟看稳态**——同时动批大小和并发，你永远不知道是谁的功劳。

### `refresh_interval` 与写入吞吐的权衡

| `refresh_interval` | 段产生速率（每分片） | 可见性延迟 | 相对写吞吐 | 适用 |
| --- | --- | --- | --- | --- |
| `1s`（默认） | 1 段/s | 平均 0.5 s | 基准（最低） | 会话、低频写、小索引 |
| `5s` | 0.2 段/s | 平均 2.5 s | +10%~30% | 中等写入的搜索型索引 |
| `30s` | 0.033 段/s | 平均 15 s | +30%~60% | **写入密集的搜索索引（推荐默认）** |
| `-1` | 0 段/s（除显式 `_refresh`） | 直到手动 refresh | 最高 | **只在批量导入/离线回填**；在线索引禁用 |

这张表的每一行都能从 `r = 1/interval` 推出来，而"写吞吐"一列之所以只是区间，是因为它同时受 merge IO、字段数、文档大小影响——**永远不要承诺一个具体的百分比**。

---

## 25.8 零停机重建的七步剧本

### 为什么这几件事只能重建

| 你想改 | 为什么改不了 | 出处 |
| --- | --- | --- |
| 已有字段的 `type` | 类型固化在该字段每个段的 `.fnm`（FieldInfos）与已生成的倒排/docValues 编码里 | → comp/21、§25.4 |
| 已有字段的 `analyzer` | 历史段的词条已经按旧分词器切好了，改设置不会重算历史段 | → comp/21 |
| `number_of_shards` | 它是路由公式 `hash(_routing) % N` 的分母，改 N 等于全量重排 | → comp/23 |
| 从 `dynamic` 改成 `strict` | 只能约束**新字段**，已有字段一个不少 | §25.4 |
| 回收墓碑空间（>30%） | expunge 需要一份额外空间，盘上往往没有 | → comp/22 |

所以重建不是"应急手段"，是 ES 的**常规维护动作**——一年做几次是正常的，问题只在于能不能**不停机**做。

### 七步时序

```text
  T0 ──────── T1 ────────── T2 ─────────── T3 ────────────── T4 ─────────── T5
   │          │              │              │                 │              │
  别名 ──────(全程指向 v1)──────────────────────────────── ⑤原子切换(1 ms)──► v2
   │          │              │              │                 │              │
   │          └① 建 v2（新 mapping / settings / 分片数；别名暂不指向）
   │                         └② 回填历史（_reindex 或离线导入；slices 并行 + 限速）
   │                                        └③ 双写开始（v1 + v2；同 _id、同版本号）
   │                                                          └④ 校验：count + 水位 + 抽样
   │                                                                          ⑤ 观察 30~60 min
   │                                                                          ⑥ 停双写，v1 转只读
   └── ⑦ 删 v1（保留 7 天后）；回滚 = 别名指回 v1，≤ 1 s，v2 留着继续追
```

### 七步与每步的验收标准

| 步 | 动作 | 验收（不达标不许进下一步） | 常见坑 |
| --- | --- | --- | --- |
| ① 建 v2 | 显式 mapping + settings + 新分片数；别名**不**指向它 | 字段数 ≤ 300 且 `dynamic: strict` 生效；`refresh_interval` 先设 `-1` | 用 `dynamic: true` 建 v2 = 把旧缺陷原样复制一份，白干 |
| ② 回填 | `_reindex` **从权威源**（不是从 v1）或离线导入；`slices` 并行 + `requests_per_second` 限速 | 回填中 v2 段数/分片不失控；在线索引 P99 不抬超过 20% | 从 v1 reindex ⇒ 继承墓碑与旧 mapping；不限速 ⇒ 回填自己的 merge 打死线上 |
| ③ 双写 | 应用或 CDC 同时写 v1 + v2；同一个 `_id`、同一个版本号 | v2 追平 lag < 5 s；双写失败率 = 0 | 双写期间改 v2 的 mapping = 灾难；**冻结变更** |
| ④ 校验 | 四项：live `_count` 差 < 0.01%、业务 `max(update_time)` 水位差 < 5 s、抽样 1,000 条逐字段比、段数健康 | 四项全过 | 用 `docs.count` 比对无意义（含墓碑）；两索引的 `_version` 各自计数不可比 |
| ⑤ 切流 | 同一个 `_aliases` 请求里 `remove v1 + add v2`（写别名要带 `is_write_index`） | 切后 30~60 min：错误率、P99、**零结果率**、段数均不恶化 | 只切读别名不切写别名 = 读新写旧；切完立刻 forcemerge = 自伤（→ comp/22 事故三） |
| ⑥ 收尾 | 停双写 → v1 转只读 → 可 forcemerge → 保留 7 天 | v1 上无任何请求（按访问日志确认） | 立刻删 v1 = 放弃回滚 |
| ⑦ 删除 | 删除 v1、清理临时脚本与限速配置 | 磁盘水位回到基线 | 忘了删 ⇒ 长期双份空间（副本 + 双份 = 4× 数据量） |

### 双写期间如何保证不丢：判定表

双写的失败几乎从不是"写不进去"，而是"**某个路径没写**"或"**悄悄吞掉了失败**"。这六项是全套判定：

| 判定项 | 判据 | 不达标的真实含义 | 兜底做法 |
| --- | --- | --- | --- |
| **写路径全覆盖** | 业务写、后台导入、运营工具、CDC 全部走同一个双写入口 | 有一条旁路 ⇒ 必然漏数据，且你查不出来 | 双写收敛到一个 `Indexer`/CDC，禁止业务直连 ES |
| **同 `_id`** | v1 与 v2 用权威源主键作为 `_id` | 用自增 ID 派生 ⇒ 两索引对不上，无法逐条比对 | `_id` = 业务主键（→ comp/23 的幂等写） |
| **顺序正确** | `version_type=external` + 权威源版本号/时间戳 | 并发乱序 ⇒ 旧值覆盖新值（最隐蔽的不一致） | external version 让 ES 拒绝低版本写 |
| **失败不吞** | v2 写失败 ⇒ 重试 + 落重试队列/表，必须可查 | "错误率 0% 但数据少了" —— 事故三那种 404 被吞的同型错误 | 失败进 MQ 或本地表，异步补偿 + 对账 |
| **追平可证** | `_count` + `update_time` 水位 + 抽样逐字段，三项全过 | 只看条数会被"错的条数相同"骗过 | 三项写进验收脚本，缺一项不许切流 |
| **回滚演练过** | 上线前**真的**把别名指回 v1 一次 | 没演练过的回滚 = 没有回滚 | 演练写进变更单，步骤与切流对称 |

### 命令序列

```bash
# ① 建 v2（别名不指向它）+ ② 从权威源限速回填（异步，保护在线索引）
curl -XPUT 'es:9200/products_v2' -H 'Content-Type: application/json' -d @products_v2.json
curl -XPOST 'es:9200/_reindex?slices=4&requests_per_second=4000&wait_for_completion=false' \
     -H 'Content-Type: application/json' -d '{
  "source": { "remote": { "host": "http://mysql-shim:9200" }, "index": "products_src", "size": 1000 },
  "dest":   { "index": "products_v2", "version_type": "external" }
}'
curl -s 'es:9200/_tasks?detailed=true&actions=*reindex'    # 看进度，别重发

# ③ 回填完成后恢复 settings（副本、refresh），再开双写
curl -XPUT 'es:9200/products_v2/_settings' -H 'Content-Type: application/json' \
     -d '{"index":{"refresh_interval":"30s","number_of_replicas":1}}'

# ④ 三项校验：live 文档数、业务水位、段数健康
curl -s 'es:9200/products_v2/_count?preference=_primary'
curl -s 'es:9200/products_v2/_search?size=0' -H 'Content-Type: application/json' \
     -d '{"aggs":{"w":{"max":{"field":"update_time"}}}}'
curl -s 'es:9200/_cat/segments/products_v2?v&h=shard,segment,size&s=shard' | wc -l

# ⑤ 原子切换（同一请求内 remove + add；写别名带 is_write_index）
curl -XPOST 'es:9200/_aliases' -H 'Content-Type: application/json' -d '{
  "actions": [
    {"remove": {"index": "products_v1", "alias": "products"}},
    {"add":    {"index": "products_v2", "alias": "products", "is_write_index": true}}
  ]}'

# ⑥ 观察 30~60 min 后停双写、v1 转只读；⑦ 7 天后删 v1
curl -XPUT 'es:9200/products_v1/_settings' -d '{"index":{"blocks.write":true}}'
# 回滚（≤1 s）：把上面 ⑤ 的 actions 反过来再发一次
```

---

## 25.9 三本账：段数、集群状态与重建时长

五个算例。**每个都带推导式与假设清单，换假设即可复算，任何一个数字都不是承诺值。**

### 账一：每秒 100 次 bulk × 5 MB × refresh 1s，一天多少段

| 项 | 推导 | 结果 |
| --- | --- | --- |
| 逻辑写入速率 | 100 bulk/s × 5 MB | **500 MB/s = 43.2 TB/天** |
| 分片分布（12 主分片） | 500 ÷ 12 | ≈ 41.7 MB/s/分片 |
| 段产生速率 | `1/refresh_interval × 分片数` = 1 × 12 | **12 段/s** |
| 每天产生的段 | 12 × 86,400 | **1,036,800 段/天** |
| 每段大小 | 41.7 MB/s × 1 s | ≈ 41.7 MB（大于 `floor_segment` 2 MB，属正常层） |
| 单分片日增量 | 41.7 MB/s × 86,400 s | **≈ 3.6 TB/分片/天** |
| tier 层稳态段数 | `(k−1) × 层数 = 9 × 3.4` | ≈ 31 段/分片 |
| 顶层段（硬地板） | 3.6 TB ÷ `max_merged_segment` 5 GB | **≈ 720 段/分片** |
| **merge 跟得上时的稳态段数** | 31 + 720 | **≈ 750 段/分片，全索引 ≈ 9,000 段** |
| 日均 merge 重写字节 | 43.2 TB × 层数 3.4 × 副本 2 | **≈ 294 TB/天 ≈ 3.4 GB/s 集群级** |

**结论 1：这个算例里"一天 100 万段"根本不是问题，因为 merge 会把它们吃掉；真正的问题是 `750 段/分片` 这个地板。** 段数的地板 = 单分片字节 ÷ `max_merged_segment`，而 750 段/分片已经是"每次查询固定开销 ×18"的水平 —— 也就是说，**在 43 TB/天的写入量下，唯一正确的动作是按时段 rollover 拆索引，而不是把数据都塞进一个索引**。

**结论 2：如果 merge 跟不上会怎样。** 按 `N_seg = r × T_reside` 反推：`T_reside` 从"健康 34 s"涨到 30 min（1,800 s）⇒ 段数 = 12 × 1,800 ÷ 12 = 1,800 段/分片；3.4 GB/s 的 merge 写加上在线读写抢同一块盘，util 越过 60% 后单次 merge 耗时 ×3，形成 22 篇描述的正反馈，最后落到 3,000~5,000 段/分片的区间（与事故二实测 3,000 一致），同时 fd 逼近 `ulimit -n`，**故障形态不是"变慢"，是"节点开始报 `too_many_open_files`、查询线程池被打满"**。

> **【量级结论】** 假设：12 主分片 1 副本、`max_merged_segment 5 GB`、`segments_per_tier 10`、层数 3.4、数据不做过期删除、单文档 ~1 KB 且写入均匀分布到 12 分片。地板项对 `max_merged_segment` 与单分片字节是线性敏感的（把 `max_merged_segment` 调到 10 GB，地板减半为 360 段/分片，代价是一次 merge 的峰值空间翻倍）。**不是承诺值。**

### 账二：2 万字段的索引，集群状态多大、广播多少字节

| 项 | 推导 | 结果 |
| --- | --- | --- |
| 单索引 2 万字段的 mapping JSON | 20,000 × 60 B | **≈ 1.2 MB** |
| 200 个这样的索引 | 200 × 1.2 MB | **≈ 240 MB**（= 200 万条字段定义） |
| 传输时压缩（4× 量级） | 240 ÷ 4 | ≈ 60 MB 一份状态 |
| 一次状态更新广播给 30 节点 | 60 MB × 30 | **≈ 1.8 GB 出流量/次更新** |
| 每节点堆里常驻的 mapping 对象 | 240 MB × 5~10 | **≈ 1.2~2.4 GB/节点** |
| master 单次状态提交的额外成本 | 全量序列化 + diff + 落盘 `state-N.st` | 秒级 CPU + 一次 60 MB 级磁盘写 |
| 判据 | publish timeout 量级 30 s；单次 apply 若达数百 ms，连续几次不同步就积压 | 折旧点：**索引数 × 字段数 ≳ 50 万条定义就开始疼** |

**结论**：**"200 万条字段定义"的重量不是磁盘，是 `上次状态更新要等 30 个节点各反序列化 60 MB`。** 单看一个索引（1.2 MB）完全无害——这就是为什么 mapping 爆炸的锅常常"结算在别人身上"：某个团队给索引加了 2 万个字段，全集群的元数据操作一起变慢。

> **【量级结论】** 假设：每字段 JSON 60 B（`{"name":{"type":"keyword"}}` 量级）、cluster state 传输压缩 4×、堆内对象放大 5~10×、30 个节点。字段名越长、multi-field 与 `format`/`analyzer` 参数越多，单字段字节可到 150 B 以上，结果按比例放大。**不是承诺值。**

### 账三：堆 31 GB、每段元数据约 1 KB、10 万段

| 项 | 推导 | 结果 |
| --- | --- | --- |
| 每段元数据（字段数 30 量级） | 1 KB 固定 + 30 × 30 B | ≈ 2 KB |
| 10 万段的元数据 | 100,000 × 2 KB | **≈ 200 MB** |
| 占 31 GB 堆 | 200 MB ÷ 31 GB | **0.6%** ← 无害 |
| 换成 3,000 字段 | 1 KB + 3,000 × 30 B ≈ 91 KB/段 | 100,000 × 91 KB ≈ **9.1 GB = 29% 堆** |
| 换成 2 万字段 | 1 KB + 20,000 × 30 B ≈ 600 KB/段 | 60 GB > 堆 ⇒ **节点起不来** |

**结论**：**"段数多导致堆爆"是一个只在字段数也不小时才成立的判断。** 单看 10 万段，元数据只占 0.6%；把它乘上 3,000 字段就吃掉近三成堆，再加上 fielddata 与聚合中间态，31 GB 就没了。所以堆排障的第一件事不是"看段数"，而是**把 `段数 × 字段数` 当乘积算一遍**——这也是 §25.3 那条乘数关系唯一的定量验证。

> **【量级结论】** 假设：每段固定元数据 ~1 KB、每字段每段 ~30 B（FieldInfo + norms/docValues 元数据引用）、堆 31 GB 全部可用于段元数据（实际还要分给 fielddata/聚合/buffer）。真实数值随 Lucene 版本、字段类型（`text` 带 positions 更重）浮动 2~3 倍，**把 30 B 换成 60 B，3,000 字段那一行从 29% 涨到 58%，结论只会更强**。**不是承诺值。**

### 账四：重建 800 GB 要多久，双写加多少负载

| 项 | 推导 | 结果 |
| --- | --- | --- |
| 待回填字节 | 800 GB（主分片口径，不含副本） | 800 × 1,024 = 819,200 MB |
| 单 slice 吞吐 | 目的分片"分词 + 建倒排 + docValues"是 CPU 与 IO 混合，实测 20~50 MB/s，取 30 | 30 MB/s |
| 4 slice 并行 | 30 × 4 | 120 MB/s |
| 纯回填时长 | 819,200 ÷ 120 | **≈ 6,827 s ≈ 1.9 小时** |
| 回填期间的磁盘写（含 merge） | 800 GB × 层数 3.4 × 副本 1（导入期先 0 副本） | ≈ 2.7 TB |
| 加 1 副本后总量 | ×2 | ≈ 5.4 TB |
| 双写额外逻辑写入 | 线上 5,000 doc/s × 1.8 KB = 9 MB/s → 双写后 18 MB/s | 逻辑写入 **×2** |
| 双写额外磁盘写 | 9 MB/s × 4（merge 层数）× 2（副本） | **+72 MB/s**（相对集群 12 节点 = +6 MB/s/节点） |
| 端到端窗口 | 建索引 1 min + 回填 1.9 h + 追平校验 30 min + 观察 60 min | **≈ 3 小时（不停机）** |
| 磁盘峰值 | 2 倍数据（v1 + v2）× 副本 2 | **≈ 3.2 TB，且要保留 7 天** |

**结论 1**：`800 GB` 的重建在"不算快"的假设下就是**2 小时回填 + 1 小时验证 ≈ 3 小时窗口**，而**停机窗口是 0**。7 天保留期是唯一的硬成本：容量规划要按 3 份算（v1 + v2 + 7 天里 v1 的 merge 仍在跑 ⇒ 它的空间是"满额"的，不是"躺着的"）。

**结论 2**：`slices` 不是越多越好。`_reindex` 的 slice 数超过目的索引主分片数（12）后不再增加并行度，只增加协调负载；并且每多一个 slice 就多一股对在线集群的 IO 压力，所以**限速（`requests_per_second`）比并行度更重要**——宁可回填 6 小时，也不要把线上 P99 打到 1 s 以上（这个权衡与恢复限速完全同型，→ comp/23 账四）。

> **【量级结论】** 假设：单 slice 30 MB/s（随字段数、analyzers、文档大小浮动 20~50 MB/s）、4 slice 线性并行、目的索引无副本、merge 层数 3.4、双写不引入额外放大（只 ×2 逻辑写）、线上 5,000 doc/s × 1.8 KB。若单 slice 只有 15 MB/s，回填变 3.8 小时；若 12 slice 且节点有余量，可压到 1 小时以内。**不是承诺值。**

### 账五：三个数字的复算脚本

```python
import math
BULK_RPS, BULK_MB, SHARDS, REPL = 100, 5, 12, 2
MAX_SEG_GB, TIER, FLOOR_MB = 5, 10, 2

# ① 段数：产生速率 / 日段数 / 地板
r = 1.0 * SHARDS                                    # refresh=1s ⇒ 每分片每秒 1 段
print(f"段产生 {r:.0f} 段/s，{r*86400:,.0f} 段/天")
day_gb = BULK_RPS * BULK_MB * 86400 / 1024
per_shard = day_gb / SHARDS
layers = math.log(MAX_SEG_GB * 1024 / FLOOR_MB) / math.log(TIER)   # log_tier(顶层/底层)
floor_seg = per_shard / MAX_SEG_GB
print(f"日逻辑 {day_gb:,.0f} GB，单分片 {per_shard:,.0f} GB，层数 {layers:.1f}")
print(f"段数 ≈ 地板 {floor_seg:.0f} + tier {(TIER-1)*layers:.0f} = {floor_seg+(TIER-1)*layers:.0f} 段/分片")

# ② 集群状态：字段数 × 索引数 × 60 B，广播 30 节点
for fields, idx in ((10_000, 200), (20_000, 1)):
    mb = fields * idx * 60 / 2**20
    print(f"{idx} 索引 × {fields:,} 字段 → {mb:,.0f} MB；压缩4×后广播30节点 ≈ {mb*30/4:,.0f} MB")

# ③ 堆：段元数据 = 段数 × (1 KB + 字段数 × 30 B)
for fields, segs in ((30, 100_000), (3_000, 100_000), (20_000, 10_000)):
    per_seg = 1024 + fields * 30
    print(f"{fields:>6} 字段 × {segs:,} 段 → {segs*per_seg/2**30:6.1f} GB 段元数据")

# ④ 重建：800 GB @ N slice
for per_slice, slices in ((30, 4), (30, 12), (15, 4)):
    h = 800 * 1024 / (per_slice * slices) / 3600
    print(f"回填 800 GB @ {per_slice} MB/s × {slices} slice ≈ {h:.1f} h")
```

---

## 25.10 架构边界：它能自愈什么、不能自愈什么

| 故障 | ES 自己能做什么 | 它明确不能做什么 | 你必须做什么 |
| --- | --- | --- | --- |
| 节点挂掉 | 秒级提升 in-sync 副本为主；`index.unassigned.node_left.delayed_timeout`（默认 `1m`）防抖 | 不能避免重建副本要**整份拷贝**（→ comp/23 账四） | 留恢复带宽；单分片 ≤ 50 GB |
| 磁盘进入只读/满 | 85/90/95 三档水线触发只读与搬迁 | **不会自己删一份数据**；搬迁本身是 IO 风暴 | ILM 删除策略；容量告警设 70% |
| 段数上涨 | 后台 TieredMergePolicy 持续合并 | 不能突破"顶层段不合并"的地板；不能在没有空闲盘时插入 merge | refresh 分级；单分片 ≤ 50 GB；留 15%~25% 空间 |
| 写不进来 | 线程池满 ⇒ 立刻 429（**这是它唯一的保护动作**） | 不会自动扩容、不会自动降级 | 客户端退避 + 限流；把 `rejected` 当 SLO |
| 堆不够 | 断路器先返回 429，再不行就 OOM 被踢出 | 不会自动禁掉那个"大聚合查询" | 禁 `fielddata`、限深分页、聚合白名单 |
| mapping 字段爆炸 | 到 `total_fields.limit`（默认 1000）就报错拒写——**只有这一道墙** | 不会回滚已建的字段，也不会自动改 `dynamic` | `dynamic: strict` 进模板；字段数进监控 |
| 字段类型错 / 分片数错 | `PUT mapping` 直接报错（拒绝执行） | **完全无法原地修** | 别名 + 七步重建（§25.8） |
| master 挂 | 自动重新选举（秒级） | 不能避免状态更新积压；老版本配错 quorum 会双主 | 3/5 台专职 master（→ comp/23 §23.7） |

### 扩展维度（第 ⑤ 问的明确回答）

三条线各自只有一个方向，而且**都不是"加数据节点"**：

| 线 | 唯一有效的扩展维度 | 为什么加节点没用 |
| --- | --- | --- |
| 段数 | **刷新分级 + 单分片上限**（批处理摊薄）：`refresh_interval` 按索引类型分级、导入用 `-1`、单分片 ≤ 50 GB 压住地板项 | 段数由 `r = 1/interval` 与"单分片字节 ÷ 5 GB"决定，与节点数无关 |
| 字段数 | **垂直切索引**（按字段集拆）：一个"字段集合"一个索引 | mapping 在 cluster state 里全量广播，**节点越多广播越贵**，加节点只会更糟 |
| 堆 | **垂直切角色**：专用 master / 专用 coordinating / 冷热分层 | 每节点堆有 ~30 GB 硬上限；只能加**节点数**（每节点一份堆）并让分片/查询分散过去 |

一句话记：**段数考"节奏"、字段数考"切分"、堆考"分工"。三者里没有一个能靠"上更多机器"解决——这正是 ES 与无状态服务的根本差别。**

### 换掉它的信号

| 信号 | 阈值 | 换什么 |
| --- | --- | --- |
| 单分片大小 | > 50 GB 且不可控 | 先 `_split`/reindex 重切；再考虑换形态 |
| 分片数 / 节点数 | > 20 | 合并索引、降分片；否则 master 与 cluster state 先崩 |
| cluster state 更新 | > 1 s | 减索引/字段；专职 master 集群 |
| 段数 / 分片 | > 300 持续 | 刷新分级 + rollover；再不行就重建 |
| 写入拒绝率 | > 0.1% 持续 5 min | 客户端限流/退避；扩主分片（不可逆）或多索引 |
| 深分页 / 大聚合占比 | > 5% 查询 | 报表挪列存（→ `architect/21`） |
| 同一份数据既要 OLTP 又要全文 | — | 承认 ES 是**派生索引**（→ [architect/17](../architect/17-search-system.md) 的 §17.2） |

---

## 25.11 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到物理形态） | 定位 | 修复 | 防复发 |
| --- | --- | --- | --- | --- |
| master CPU 100%、`_cluster/health` 卡住、写入被拒 40% | 字段数爆炸 ⇒ mapping 变大 ⇒ cluster state 全量广播给所有节点并等 ack | `GET _cluster/state/metadata` 响应字节数；`GET /idx/_mapping` 数字段 | 模板改 `dynamic: strict` + `flattened`；字段爆炸的索引重建 | mapping 大小进监控（> 1 MB/索引报警）；模板进代码仓库 + 渲染测试 |
| 查询 P99 莫名从 60 ms 到 1.2 s，`_cat/indices` 一切正常 | 段数/分片涨到几百 ⇒ 每查询固定开销 ×N；`docs.count` 看不出这件事 | `_nodes/stats/indices/segments.count` ÷ 分片数；`_cat/segments` 分 shard 计数 | 停 refresh 抖动、关 update 风暴；只读索引 `_forcemerge` | 段数/分片做第一报警指标（基线 41、警告 100） |
| 灌数脚本跑着跑着 `too_many_open_files` | `refresh_interval: 1s` ⇒ 段速率 ×30 ⇒ fd = 段数 × 3~90 撞 `ulimit -n` | `ls /proc/<pid>/fd \| wc -l`、`_cat/segments` | 导入索引 `refresh_interval: -1` + `number_of_replicas: 0` + 灌完 forcemerge | 导入脚本模板化：三个设置必须一起出现 |
| 删了索引想重建，线上"错误率 0% 但搜不到东西" | 客户端把 `index_not_found_exception` 当"无结果"吞掉；没有别名 ⇒ 无法秒级回滚 | 监控**零结果率**而不只是 5xx；`_cat/aliases` 确认别名存在 | 立刻建同名索引 + 恢复别名 | 代码里禁止出现物理索引名字面量；零结果率进 SLO |
| `PUT _mapping` 报 `cannot be changed from type [text] to [...]` | 类型固化在每个段的 `.fnm` 里，历史段无法重算 | 报错本身就是定位结论 | 七步重建（新索引 + reindex + 别名） | 建索引前做 mapping 评审：排序聚合字段必须 `doc_values`、金额用 `scaled_float` |
| 堆加到 40 GB，反而更慢、GC 更久 | 越过约 30 GB 的压缩指针边界 ⇒ 指针变宽、有效容量下降；且 page cache 被挤掉 | `jvm.mem.heap_max_in_bytes` 与 `heap_used_percent`；`node_memory_Cached_bytes` | 堆调回 31 GB 以内，把内存还给 page cache | 堆写成 `-Xms31g -Xmx31g` 并锁死；`bootstrap.memory_lock: true` |
| `es_rejected_execution_exception` 成片出现，磁盘 util 才 60% | write 线程池队列满；队列不提供吞吐只提供延迟 | `_nodes/stats/thread_pool` 的 `write.queue`/`rejected` 增量 | 客户端降并发/退避；降低单批字节；不要把 `queue_size` 当解决方案 | 拒绝率进 SLO；压测得出"每分片 bulk/s 上限"再定客户端并发 |
| 加了 6 个数据节点，mapping 更新反而更慢 | cluster state 全量广播 ⇒ 节点越多、要等的 ack 越多、总出流量越大 | 对比扩容前后的 `_cluster/state` 更新耗时 | 减索引/字段数；专职 master 与大状态索引分集群 | 容量规划里把"索引数 × 字段数"当一级指标 |
| 同一字段在 A 索引能范围查、在 B 索引查不动 | 两个索引的 `dynamic` 推断结果不同（一边 `long` 一边 `keyword` 或 `text`） | `GET /a,b/_mapping/field/字段名` 对比 | 统一模板 + `dynamic: strict`；重建类型错的那个 | 跨索引查询的字段类型纳入契约测试 |
| 重建做完磁盘一直不降，账单翻倍 | v1 保留了 7 天且仍在收双写 ⇒ 段还在合并，空间是满额而非静态 | `_cat/indices` 看 v1 的 `store.size` 是否仍在涨 | 确认无需回滚后尽早停双写、删 v1 | 重建的容量预算按 3~4 份数据量申请 |

---

## 25.12 观测：指标、拐点与告警

**每条都带版本与开关前提**——写错一个指标名，读者就会认为全篇没验证过。

| 看什么 | 从哪取 | 拐点 | 版本与开关前提 |
| --- | --- | --- | --- |
| **段数 / 分片（第一指标）** | `GET _nodes/stats/indices/segments.count` ÷ 分片数；`GET _cat/segments` 按 `shard` 分组计数 | 基线 ≈ 41；**> 100 警告，> 300 严重，> 1,000 事故区** | 全版本可用；`segments.count` 是全集群当前值，需按节点求和后再除分片数；**只看总数会漏掉"某一个分片 1,050 段"** |
| **写入拒绝（写侧最直接的 SLO）** | `GET _nodes/stats/thread_pool` 的 `write.rejected`；日志里的 `es_rejected_execution_exception` | 速率 > 0 即报警；> 0.1% 请求即事故 | 全版本；`thread_pool.write.queue_size` 在部分版本不可动态修改，要改配置重启 |
| 堆占用 | `GET _nodes/stats/jvm` 的 `mem.heap_used_percent`、`heap_max_in_bytes` | 持续 > 75% 警告；> 85% 严重 | 全版本；`heap_max_in_bytes` 应与你的 `-Xmx` 一致——不一致说明配置没生效 |
| **old gen GC 时间** | `GET _nodes/stats/jvm` → `gc.collectors`（按 `name` 取 old 的 `collection_time_in_millis`） | 增速 > 1 s/min 或单次 > 2 s；频繁 old GC = 堆不够或泄漏 | 7.x 起是 `gc.collectors` 数组且带 `name` 字段；6.x 是 `gc.collectors.young/old` 对象结构。**用前先 `curl` 一次确认结构** |
| 集群状态大小 | `GET _cluster/state/metadata` 的响应字节数（`curl -s ... \| wc -c`） | 单索引 mapping > 1 MB 或总量 > 20 MB | 无官方 Prometheus 指标，需自建探针；7.x 起默认压缩传输，用 `Accept-Encoding: identity` 量未压缩大小更准 |
| **未分配分片** | `GET _cluster/health` 的 `unassigned_shards`、`relocating_shards`、`initializing_shards` | `unassigned > 0` 持续 10 min；`relocating` 长期不为 0 | 全版本；`delayed_unassigned_shards` 与 `index.unassigned.node_left.delayed_timeout`（默认 `1m`）强相关，抖动不算故障 |
| 索引规模 | `GET _cat/indices?v&h=health,docs.count,store.size,pri,pri.store.size` | 单分片 `pri.store.size` > 50 GB；`pri` 与模板不符 | 全版本；`docs.count` **含墓碑**，与 `_count?preference=_primary` 的差就是删除率，> 30% 准备重建 |
| **merge 与段状态** | `GET _nodes/stats/indices/merges` 的 `current`、`total_time_in_millis`、`total_throttled_time_in_millis`、`total_size_in_bytes` | `current` 持续 > 0 达 10 min；`throttled` 在增长 | 全版本；`total_*` 是累计值，必须求速率；`throttled` 只在启用 merge 节流时增长 |
| **磁盘水线** | `GET _cat/allocation?v` 的 `disk.percent`、`disk.avail` | **70% 预警**；85/90/95 三档（只读 → 禁分配 → 写阻塞） | 水线名 `cluster.routing.allocation.disk.watermark.low/high/flood_stage`，默认值随版本演进，以目标版本文档为准 |
| 谁在吃 CPU | `GET _nodes/hot_threads` | 出现大量 `merge` / `search` / `write` 栈 | **需开启**：8.x 起要 `monitor` 权限；托管与 Serverless 环境常被关闭，用前先确认可访问 |
| fielddata 与熔断 | `GET _cat/fielddata?v&h=node,field,size`；`_nodes/stats/breaker` | 任一 text 字段 > 100 MB；父熔断 > 90% | 全版本；`indices.fielddata.cache.size` 默认无上限，只有断路器兜底——**别把它当缓存上限** |

### 日常巡检（顺序即优先级）

```bash
# ① 三条线的头部指标：段数/分片、拒绝、堆与 GC
curl -s 'es:9200/_nodes/stats/indices/segments,jvm,thread_pool?filter_path=nodes.*.{indices.segments.count,jvm.mem.heap_used_percent,thread_pool.write.rejected,thread_pool.search.queue}'
curl -s 'es:9200/_cat/indices?v&h=health,index,pri,rep,docs.count,store.size,pri.store.size&s=pri.store.size:desc' | head -20

# ② 元数据面：集群状态大小（字段数的唯一可信度量）与健康
curl -s 'es:9200/_cluster/state/metadata' | wc -c
curl -s 'es:9200/_cluster/health?pretty' | jq '{status,unassigned_shards,relocating_shards}'

# ③ 段与 merge：哪个分片在失控
curl -s 'es:9200/_cat/segments?v&h=index,shard,segment,size,docs.deleted&s=shard' | awk 'NR>1{c[$1"/"$2]++} END{for(k in c) print c[k], k}' | sort -rn | head
curl -s 'es:9200/_nodes/stats/indices/merges,refresh?filter_path=nodes.*.indices.{merges.current,merges.total_throttled_time_in_millis,refresh.total}'

# ④ 磁盘与热点：满的早期形态
curl -s 'es:9200/_cat/allocation?v&h=node,shards,disk.percent,disk.avail'
curl -s 'es:9200/_nodes/hot_threads?threads=3' | head -40
```

### 告警表达式骨架

```text
# 段数：第一报警（基线 41，警告 100，严重 300）
sum by (node) (elasticsearch_indices_segments_count)
  / on (node) group_left sum by (node) (elasticsearch_indices_segments_shards) > 100   for 15m

# 写入拒绝：写侧 SLO，出现即告警（比磁盘 util 早 5~10 分钟）
sum(rate(elasticsearch_thread_pool_rejected_count{type="write"}[5m])) > 0             for 5m

# 堆与 old gen GC：堆持续高压 + GC 时间在涨 = 迟早 OOM
max(elasticsearch_jvm_memory_used_bytes{area="heap"})
  / max(elasticsearch_jvm_memory_max_bytes{area="heap"}) > 0.75                       for 10m
rate(elasticsearch_jvm_gc_collection_seconds_sum{gc="old"}[5m]) > 1                    for 10m

# 元数据面：cluster state 膨胀（需自建探针，指标名按实现改）
cluster_state_bytes > 20 * 1024 * 1024                                                for 30m

# 磁盘：留 15%~25% 给 merge 的临时新段 ⇒ 可用 < 30% 就该叫
max(elasticsearch_filesystem_data_available_bytes
    / elasticsearch_filesystem_data_size_bytes) < 0.30                                for 15m

# 未分配分片：抖动不算，持续才算
sum(elasticsearch_cluster_health_unassigned_shards) > 0                               for 10m
```

指标名按你用的 exporter 前缀改写（`elasticsearch_*` 是社区 exporter 的常见命名）；7.x/8.x 的语义一致，但**GC 与 thread_pool 的字段结构在 6.x → 7.x 之间变过**，跨大版本迁移时先手工 `curl` 一次核对字段名。告警分级与通知路由的设计 → [eng/21 SLO 与告警](../eng/21-slo-alerting.md)。

---

## 25.13 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 真 SLO | 不是"查询快"，是**段数、字段数、堆占用三条线不失控**；查询延迟是这三条健康时的副产品 |
| 三条线的关系 | **乘积不是并列**：堆压力 ≈ 段数 × 字段数 × 单位元数据；只优化一条，另两条吃掉收益 |
| 段数地板 | `N_seg ≈ (k−1) × 层数 + 单分片字节 ÷ max_merged_segment`；50 GB 分片 ⇒ ≈ 41 段，3.6 TB 分片 ⇒ ≈ 750 段 |
| 段数第一指标 | 每分片段数（不是总段数）；> 100 警告、> 300 严重；它同时乘在查询、fd、堆三处 |
| 段数救法 | 回滚只能停止恶化；已生成的段只能靠 merge 一段段吃；顶层段不合并 ⇒ 地板焊死 |
| mapping 存在哪 | **cluster state 里**，不在数据节点；每次变更**全量广播给所有节点并等 ack** |
| mapping 为什么卡 master | master 是元数据的唯一串行点 + 全量落盘 + 等全部 ack；字段爆炸等于劫持整个集群的元数据面 |
| 字段数硬墙 | `index.mapping.total_fields.limit` 默认 1000；> 300 就该警觉；**调大只是把墙后移** |
| 治字段爆炸 | `dynamic: strict`（拒写）> `flattened`（折叠成 1 个字段）> 拆索引；`dynamic: false` 是静默失败 |
| 堆 30 GB | 约 30 GB 是压缩指针边界（量级结论，随 JVM 与版本）；另一半内存留给 **page cache**，Lucene 靠 mmap 读段 |
| 堆里堆外 | 堆装运行态（段元数据、fielddata、聚合、buffer、cluster state 对象），page cache 装数据本身 |
| 段元数据 | **每段 ≈ 1 KB + 字段数 × 30 B**；10 万段在 30 字段时只占 0.6% 堆，3,000 字段时占 29% |
| 写 SLO | **`thread_pool.write.rejected` 就是写侧 SLO**；队列买的是 25 秒缓冲，不是吞吐 |
| bulk 起点 | 每批 5 MB、5 并发（经验起点，不是定律）；判据：P99 < 500 ms + rejected 0 + CPU 60~70% + 段数不涨 |
| 批量导入 | `refresh_interval: -1` + `number_of_replicas: 0` + 大 bulk → 灌完 `_forcemerge` → 再恢复 `30s` 与副本（顺序不能反） |
| 只能重建的三件事 | 字段类型、analyzer、分片数——都固化在段文件或路由公式里，`PUT` 永远报错 |
| 重建七步 | ①建 v2 ②回填（限速+slices）③双写 ④校验（count+水位+抽样）⑤别名原子切 ⑥观察期留 v1 ⑦删 v1 |
| 回滚 | **别名是唯一开关**：回滚 = 改一次指针 = 1 秒；代码里出现物理索引名 = 架构违规 |
| 重建时长 | 800 GB @ 30 MB/s × 4 slice ≈ 1.9 h 回填 ⇒ 端到端 ≈ 3 h，**停机 0**；容量按 3~4 份数据量申请 |
| 系统级失效五类 | 慢（段/merge/GC）、满（水线→搬迁→IO 打满）、脑裂（老版本 quorum）、假活（线程池全满）、语义漂移（dynamic 让同名字段类型不同） |
| 自愈边界 | 它能提升副本、拒写、合并段；它**不会**删数据、回滚字段、原地改分片数 |
| 扩展维度 | 段数考节奏、字段数考切分、堆考分工；**三者都不靠加数据节点** |

---

## 25.14 自检题

**1. 一个同事说"段数太多就是文件多，forcemerge 一下就行"。请用三个数字说明这句话错在哪。**
<details><summary>答案</summary>

三个数字都在 §25.3 与 §25.5：

- **乘数效应**：段数是查询固定开销的乘数。按每段 ~30 µs 固定开销，40 段 ⇒ 1.2 ms/分片，1,000 段 ⇒ 30 ms/分片，而一台节点上还有 12 个分片在抢同一个搜索线程池——P99 从 60 ms 到 1.2 s 就是这么来的。**段数不是"文件多"，是"每查询多付 N 次固定成本"。**
- **fd 与硬顶**：复合文件下每段 3~4 个 fd。1,000 段/分片 × 12 分片 = 36,000 fd，占 `ulimit -n 65,536` 的 55%；关掉复合文件是 151 万 fd（节点起不来）。所以段数撞的是**进程级资源硬顶**，不是审美。
- **地板焊死**：顶层段不再合并，所以 `段数 ≥ 单分片字节 ÷ max_merged_segment`。50 GB 分片的地板是 10 段 + tier 层 31 段 ≈ 41；3.6 TB 分片的地板是 720 段 + 31 ≈ 750。**这意味着单分片太大时，forcemerge 做完几小时内就回涨。**

正确的顺序是：先判断段数是不是被 refresh 抖动/update 风暴打上去的（可救），再判断是不是单分片过大导致地板过高（只能重建），最后才考虑对**只读索引**做 forcemerge。活跃写索引上直接 forcemerge 是拿 IO 换几小时安静。
</details>

**2. 为什么 mapping 字段爆炸卡死的是 master，而不是那个"字段很多"的索引所在的数据节点？**
<details><summary>答案</summary>

因为**索引级 mapping 的唯一权威在 master 的 cluster state 里**，数据节点上只有段的 `.fnm`（字段号与类型）——所以"加字段"这个动作不写数据节点，它是一次**集群级元数据写入**。

完整链条（§25.4）：文档里出现未声明的 key → 数据节点动态推断出新字段 → 请求走一次状态变更 → master 把新 mapping 写进 cluster state 并全量落盘 `state-N.st` → 计算 diff、序列化、压缩 → **广播给所有节点（全量状态，不是增量）** → 每个节点反序列化成 Java 对象（堆内放大 5~10×，并且常驻） → **等所有 ack 才提交**。

所以字段数从 210 涨到 1.9 万时：mapping 1.4 MB、广播给 30 节点、每节点反序列化后常驻几十到上百 MB，单次状态更新从 40 ms 涨到秒级。而 master 是**唯一串行点**：建索引、改 mapping、分片分配、水线事件全排在同一队列——于是撞上 publish timeout（量级 30 s）后 master 认为节点失联 → 踢出 → 再分配 → **又一串状态变更**。

一句话：**数据节点只是"产生字段"的现场，master 和全集群的元数据面才是账单的收款人。** 这也是为什么"加节点"在这条线上是负优化：广播目标更多了。
</details>

**3. 你们要把堆从 31 GB 调到 48 GB，理由是"段数多、堆不够"。请给出两个反对理由，以及一个更好的替代方案。**
<details><summary>答案</summary>

**反对理由一：越过了压缩指针边界（量级结论）。** 64 位 JVM 在堆约 30~32 GB 以内可以用 4 字节压缩引用，超过后引用变宽、对象变大——**"48 GB 堆"能装的段元数据可能不比 31 GB 多**，而 GC 要扫描的引用更多，停顿更长。精确临界值随 JVM 与版本浮动，工程写法是"约 30 GB，以 JVM 与版本为准"。

**反对理由二：抢走了 page cache。** Lucene 读段文件靠 `mmap`，命中计的是 OS page cache，不是堆。64 GB 机器上 31 GB 堆 + 33 GB page cache 是分工；堆加到 48 GB 就只剩 16 GB page cache ⇒ 每次查询从内存读变成随机读盘，**段本来就多，读放大又被放大一次**。

**更好的方案**：先拆因子。段元数据 ≈ 段数 × (1 KB + 字段数 × 30 B)。若字段数是 3,000，10 万段就要 9.1 GB（29% 堆，§25.9 账三）——这种情况下**减字段数比加堆有效得多**（`dynamic: strict` + `flattened` + 重建），或者减段数（降 refresh 频率、缩小单分片以压低地板）。真正需要更多堆的场景是"单次查询/聚合的内存预算不够"，那应该限深分页、给聚合设白名单、必要时把报表挪到列存——**而不是把 17 GB 内存交给一个 30 GB 就被封顶的结构**。加机器（多节点、每节点 31 GB 堆）比加单机堆更有效。
</details>

**4. 重建一个 800 GB 的索引，回填期间线上还在双写。请写出你必须校验的四项，以及"双写看起来成功但数据仍然少了"的三种典型原因。**
<details><summary>答案</summary>

**四项校验（缺一项不许切流，§25.8）**：① 两索引 live `_count`（`preference=_primary`）差 < 0.01%；② 业务 `max(update_time)` 水位差 < 5 s（证明增量追上了，不只是总量对得上）；③ 抽样 1,000 条按 `_id` 逐字段比对；④ 段数/分片健康（否则切过去就是事故二）。注意不能用 `docs.count`（含墓碑）也不能比 `_version`（两索引各自计数）。

**"看起来成功但少了"的三种典型原因**：
1. **写路径没全覆盖**：业务写双写了，但后台导入脚本、运营工具、某个老 job 还在直连 v1 ⇒ 这部分数据在 v2 里永远不存在，而所有监控都是绿的。
2. **失败被吞**：v2 写失败（429、mapping 冲突、超时）只打了一条 warn 就过去了。这跟 §25.1 事故三里"404 被吞成空结果"是同一种病——**错误率 0% 不等于没丢数据**。
3. **乱序覆盖**：同一个 `_id` 被两个线程并发写，v1 与 v2 的到达顺序不同 ⇒ 两边最终值不同。解法是 `version_type=external` + 权威源版本号，让 ES 拒绝低版本写。

兜底做法：所有双写走同一个入口，失败必须落重试队列/表，切流前用抽样比对而不是计数比对来验收。
</details>

**5. 请把下面五个现象分别归到"慢/满/脑裂/假活/语义漂移"里，并各给一个 ES 的定位动作：① P99 从 60 ms 涨到 1.2 s 但 CPU 只有 40%；② 磁盘 util 100%，`relocating_shards` 突增；③ 两个节点各报告自己是 master；④ 节点 ping 得通、`_cat/health` 有响应，但所有 bulk 429；⑤ 跨两个索引的同一个字段，一个能范围查一个查不动。**
<details><summary>答案</summary>

| 现象 | 归类 | 为什么 | 定位动作 |
| --- | --- | --- | --- |
| ① P99 涨、CPU 低 | **慢** | 不是算力不够，是段数/merge/GC 造成的读放大或等待 | `segments.count` ÷ 分片数；`merges.current`；old gen GC 时间——找到"乘数"而不是"资源" |
| ② 磁盘 100% + 搬迁突增 | **满** | 水线触发 relocation，每个 relocation 是一份完整分片拷贝 ⇒ 自造 IO 风暴 | `_cat/allocation` 的 `disk.percent`；立刻 `cluster.routing.allocation.enable: none` 冻结分配止血，再修容量 |
| ③ 两个 master | **脑裂** | quorum 构型错误（6.x `minimum_master_nodes: 1`）或多套配置各自引导（`initial_master_nodes` 留着） | 两个 master 日志并存 + `_cat/nodes` 的 master 标记不一致；立即冻结写、隔离一侧、人工比对冲突（完整机制 → comp/23 §23.7） |
| ④ 响应但全 429 | **假活** | 进程活着、TCP 通、健康检查过，但 write 线程池全满，实际处理能力为 0 | `_nodes/stats/thread_pool` 的 `write.queue`/`rejected`；`_nodes/hot_threads` 看是不是被 merge 或大写入占满 |
| ⑤ 同名字段类型不同 | **语义漂移** | 两个索引的 `dynamic` 各自推断（一边 `long` 一边 `keyword`/`text`），查询行为不一致 | `GET /a,b/_mapping/field/字段名` 对比；根治靠统一模板 + `dynamic: strict`，错的那个重建 |

一个补充判据：**"假活"与"慢"最容易被混淆**——区别在队列：假活的请求根本不进处理（`rejected` 在涨、`active` 打满），慢的请求在处理但很费时（`queue` 在堆、`rejected` 还没出现）。看到 `rejected` 就往假活查，看到 `queue` 堆积但无拒绝就往慢查。
</details>

---

## 25.15 延伸阅读（锚点式）

- 段的不可变性、refresh/flush/merge 五个动作的时间轴、写放大 16 倍的乘法链：`[22 段：不可变、refresh、flush 与 merge](22-segment-and-near-realtime.md)`
- 分片数判据（`clamp(ceil(D/s), 节点数, 堆GB × 20)`）、恢复带宽账、master 选举与 quorum：`[23 集群形态：分片、副本、路由与 master 选举](23-es-cluster-shards-replicas.md)` 的 §23.6、§23.7、§23.8
- 查询执行：query vs filter、BM25、scroll/search_after 与深分页为什么必然爆、fielddata 的边界：`→ comp/24`（同批其他章节）
- 向量字段的容量形态（`.vec`/`.vex`/`.vem`、HNSW 图的堆占用与构建预算）与本篇同属"不可变段"的推论：`→ comp/26`
- 倒排的物理结构（FST 词典常驻堆、`norms` 决定算分、`doc_values` 决定排序聚合）：`→ comp/21`
- 搜索系统整体设计与量级换挡（L0–L5、双写与对账、六阶段重建、九种崩法）：`[architect/17 搜索系统深窥](../architect/17-search-system.md)` 的 §17.4、§17.11、§17.12、§17.14
- 组件故障谱（慢 / 满 / 脑裂 / 假活 / 语义漂移的统一信号与兜底）：`→ comp/52`
- SLO 分级、告警路由与"哪个指标配哪一档"：`[eng/21 SLO 与告警](../eng/21-slo-alerting.md)`
- 本篇所在的大纲、解剖卡八问与三条写作纪律：`[0-summary.md](0-summary.md)`
