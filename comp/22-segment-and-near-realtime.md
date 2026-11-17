# 22. 段：不可变、refresh、flush 与 merge

> **状态**：`已完成（初稿）` ｜ **所属**：第五部分 · 检索与向量 ｜ **编号**：22 / 53
> **本篇定位**：第 21 篇讲倒排表本身（一个 term 怎么找到 doc），本篇讲**装倒排表的那个容器——段（segment）的一生**：它由哪些文件组成、在哪个时刻被生产出来、在哪个时刻对 `_search` 可见、在哪个时刻才真正从磁盘上消失。倒排结构一句话结论带过：词典是 FST、倒排表有序且带跳表、位置信息单列成文件（→ comp/21）；接下来所有篇幅都花在段的生命周期上，因为**ES 九成"调了一行配置就塌"的事故，都发生在段的生产速率与消费速率之间**。
> **读法**：22.3 先看清一个段目录里到底有哪几类文件——这是后面所有"为什么慢、为什么不降盘、为什么 fd 不够"的物证；22.4 的时间轴是全篇主线，把 buffer / refresh / translog / flush / merge 五个动作排进一条时间线；22.6 是全篇唯一的算式，评审时间不够就只讲这一节；22.9 的段数、字段数、堆三条线可以直接搬进监控面板；22.10 的别名切换顺序图可以直接当发布手册。

---

## 22.1 引子：三起「配置改了一行，集群塌了三天」的故障

段的事故有个统一特征：**没有异常、没有 5xx、没有告警**。集群"黄着"，搜索"慢一点"，写入"偶尔 429"。等到有人看图才发现，问题在两个星期前就被写进了一份 YAML。

### 事故一：translog 涨到 7.8 GB/分片，一次内核补丁打了 6 小时（2025-08-09）

`orders_search`：6 数据节点（32C128G + 3.2 TB NVMe），24 主分片 × 1 副本 = 48 个分片，日写入 7,300 万条（新增 4,400 万 + 更新 2,900 万），均值 845 doc/s、峰值 6,800 doc/s。

三周前，有人为了"减少 flush 打满 IO"，把 `index.translog.flush_threshold_size` 从默认 `512mb` 调成 `8gb`，同时把 `refresh_interval` 设成 `60s`。看起来只是少做几次 IO。但这条索引的查询几乎全部来自**商家后台整点报表**，白天 `_search` QPS 长期低于 3——于是 `index.search.idle.after: 30s` 生效：**分片被判为搜索空闲后，ES 不再为它做后台 refresh**。refresh 被跳过，buffer 里的数据要靠 flush 才能变成段，而 flush 的上限被调到 8 GB。translog 于是稳定涨到每分片 7.8 GB 才肯落一次盘。

2025-08-09 凌晨给这个集群打内核补丁，滚动重启：

| 时间 | 事件 | 该节点待重放 translog | 集群状态 | 搜索 P99 |
| --- | --- | --- | --- | --- |
| 08-09 01:00 | 节点 1 重启，8 个分片重放 translog | 8 × 7.8 GB = 62 GB | yellow | 80 ms |
| 08-09 02:03 | 重放完成并触发重分配：**恢复过程本身又要写 62 GB 新段** | 62 GB | yellow | 620 ms |
| 08-09 02:20 | 该节点磁盘 util 96%，正在服务的分片读延迟翻倍 | — | yellow | 1,100 ms |
| 08-09 04:10 | 第 3 个节点开始重放，累计写入被拒 840 万次 | 62 GB | yellow | 1,900 ms |
| 08-09 05:40 | 第 5 个节点；`orders_search` 磁盘水位 91%，分片转只读 | 62 GB | yellow | 2,600 ms |
| 08-09 07:32 | 6 个节点全部完成，集群回 green；后台报表已 6.5 小时不可用 | 0 | green | 90 ms |

推导链条一句话：**重放字节 = 分片数 × 每分片 translog 大小 = 8 × 7.8 GB = 62 GB/节点；重放速率受"分词 + 建倒排 + 每 20% 触发一次 commit"限制，实测 15~25 MB/s，取 17 MB/s ⇒ 62 GB ÷ 17 MB/s = 3,650 s ≈ 61 min/节点；6 个节点串行滚动 ⇒ 6.1 小时**，加上每次等 green 的时间就是 6 小时 32 分。代价：商家后台不可查订单 **47 分钟**，估算影响 38 万笔订单查询；期间共 2,300 万次写入被拒。

真正的错误不是"调大了 flush 阈值"，而是**没人知道 `refresh_interval` 在空查询的分片上会被跳过**——于是"刷新频率"这个参数在写多读少的索引上，实际的语义是"translog 的增长速度"。

### 事故二：被删文档占 40% 空间，而 merge 因为盘满永远排不上（2025-09-22）

`products_search`：第三年运行，mapping 改过两次，早期用一个脚本每天 `delete_by_query` 清理下架商品再重新写入。Physical 现状：

| 指标 | 数值 | 怎么算出来的 |
| --- | --- | --- |
| `docs.count`（含墓碑） | 3.6 亿 | `_cat/indices` |
| `_count`（live） | 2.1 亿 | `_count?preference=_primary` |
| `docs.deleted` | 1.5 亿 | 1.5 ÷ 3.6 = **41.7%** |
| `pri.store.size` | 316 GB | 3.6 亿 × 0.9 KB = 324 GB（含压缩） |
| 有效数据 | 189 GB | 2.1 亿 × 0.9 KB |
| 墓碑浪费 | **127 GB × 2（含副本）= 254 GB** | 316 − 189 = 127 GB |

集群总占用 16.6 TB / 19.2 TB = **86%**，刚好越过 85% 水位。而 expunge 一次墓碑需要**先写新段、再删旧段**：峰值额外空间 ≈ 127 GB，是索引自身 316 GB 的 40%。盘上没有 40% 空闲，于是 TieredMergePolicy 找不到可执行的 merge 计划 → 墓碑只增不减 → 盘更满。这是一个**正反馈死锁**：删除越多 → 盘越满 → merge 越排不上 → 墓碑越多。

后果：查询要为每个段做一次 liveDocs 位图过滤（127 GB 的墓碑仍然被扫、被解压、被丢弃），搜索 P99 从 62 ms 涨到 240 ms；最后只能新建索引 reindex，**42 小时**完成，期间磁盘 92% 触发只读水位，**写入被拒 4 小时 12 分**，运营侧 11 万条商品变更丢失，只能靠权威源补写。

关键结论：`delete_by_query` **不回收任何字节**，它只是给文档打上墓碑位；只有 merge 会重写并丢弃它们，而 merge 需要空间。——这就是"C 盘只剩 5% 时你既删不掉也合不动"的物理解释。

### 事故三：一次 `_forcemerge` 把全站搜索打到 2.4 秒（2025-11-03）

归档索引 `logs-2025-09`：6 分片 × 1 副本，`pri.store.size` 2.4 TB（其中 45% 是墓碑，因为日志每天被 `delete_by_query` 清理过期项）。运维想省盘，在下午 15:20 对全索引发起一次合并到单段：

`POST /logs-2025-09/_forcemerge?max_num_segments=1`

- 重写量 = 读 4.8 TB（含副本）+ 写 4.8 TB，**临时空间 = 新段大小 ≈ 每分片 400 GB**；
- 客户端超时 30 s 返回 504，但**任务在服务端继续跑**；运维以为失败，15:31 又发了一次 → 两批 merge 并行；
- 归档索引与在线索引**共用同一批 data 节点**（没有用 `node.attr` 打标签做隔离），磁盘 util 在 12 分钟内从 24% 冲到 100%；

| 时间 | `products_search` 搜索 P99 | 搜索线程池 queue | 5xx |
| --- | --- | --- | --- |
| 15:20（前） | 60 ms | 0 | 0.02% |
| 15:32 | 380 ms | 120 | 1.1% |
| 15:58 | 1,400 ms | 640 | 7.8% |
| 16:40 | **2,400 ms** | 1,000（上限） | **12.3%** |
| 19:00 | forcemerge 结束，IO 回落 | 逐步归零 | 0.05% |

代价：全站搜索降级 **3 小时 40 分**，超时请求 380 万次；换来的收益是归档索引省了 41% 空间（确实有 45% 墓碑，值得做）。教训不是"别 forcemerge"，而是：**forcemerge 不是"回收空间"按钮，它是一次整段重写级别的 IO 炸弹**，必须满足三个前提——只读索引、节点隔离、低峰分批；并且**它几乎总会超时**（客户端 30 s，任务跑 3 小时），要按异步任务对待。

三起事故分别对应本篇三节：**translog 与 flush 的持久性边界（22.4）**、**删除只在 merge 时消失（22.5、22.6）**、**merge 的 IO 预算（22.6、22.9）**。

---

## 22.2 一句话定位与唯一真问题

**段（segment）就是一次 refresh 的产物**：它是一个**完全不可变、自包含、只读**的倒排索引单元，包含自己的一整套文件（词典、倒排表、位置、norms、doc values、stored fields、墓碑位图）。它一旦落成，除了"整段被删掉"或"整段被 merge 重写"，没有任何办法改它里面的一个字节。

### 唯一真问题

> **段不可变是整个 ES 的宪法：可见性靠"新开一个段"，更新其实是"删一条 + 写一条"，删除只有在 merge 时才真正消失。**

这一句话可以拆成三条推论，本篇的全部内容都是它们的展开：

| 推论 | 直接机制 | 你必须付的账 |
| --- | --- | --- |
| ① 可见性只能靠"新开段" | `_search` 只能看到已经打开成 reader 的段；buffer 里的文档任何查询都看不见 | **可见性延迟 ≈ `refresh_interval`，而段产生速率 = 1 / `refresh_interval`**——两个数是倒数，不可能同时变好 |
| ② 更新 = 删旧 + 写新 | 旧文档在旧段里无法修改，只能标墓碑；新值必须写进新段 | **一次 update 至少 2× 写放大**，叠加 merge 的 4× 层数放大 ⇒ 净 8× |
| ③ 删除只是"位图上的一个 0" | `.liv` 位图在查询时过滤，段文件与倒排字节一个都不少 | 删除只省"结果集"，不省"空间与扫描成本"；**物理回收只能等 merge 重写整个段** |

三条推论之外，还有一条更锋利的表述，评审时可以直接念：

> **段的数量不是"文件多不多"这种审美问题，它是三个物理量的乘积项**：查询的固定开销（每段一次 postings 遍历）、堆里的段元数据（每段 5~50 KB）、以及文件描述符（每段 3~90 个 fd）。所以段数是 ES 最先失控、也最先被看见的那个指标。

### 它明确不承诺什么

| 你以为 ES 承诺了 | 实际不承诺 | 反例 |
| --- | --- | --- |
| "写完就能搜到" | 只承诺"**近**实时"：最坏 `refresh_interval` 之后 | `refresh_interval: 30s` ⇒ 最长 30 s 后才可搜 |
| "写成功就持久" | 只承诺"translog 已 fsync 的那部分"；副本是否跟上由 `wait_for_active_shards` 决定 | 默认 `wait_for_active_shards: 1`，主挂 + 副本没追上 = 丢这几个请求 |
| "删了就释放空间" | 只承诺"搜不到"；空间要等 merge | 事故二：41.7% 是墓碑，盘一点没降 |
| "改一个字段就只动那一行" | 整篇文档重写，落进新段 | 改价格 = 写 0.9 KB 新文档 + 留 0.9 KB 墓碑 |

---

### 三本放大账在段上的落点

| 账 | 在段上的具体形态 | 数量级（本篇 380 GB / 12 分片算例） |
| --- | --- | --- |
| 写放大 | 一次 update 写两遍（新文档 + 旧文档墓碑），merge 每层再重写一遍 | `2（update）× 4（merge 层数）× 2（副本）= 16×` |
| 读放大 | 一个查询要遍历每个段的倒排 + 每个段的 doc values 块 + 每个段一次 liveDocs 位图 | 段数从 40 → 1,050 ⇒ 固定开销 × 26 |
| 空间放大 | 墓碑（未 merge 前）+ merge 期间的临时新段 + 副本 | 删除率 40% ⇒ 空间放大 1.67×；merge 峰值再要 40% 临时空间 |

---

## 22.3 物理形态：段文件与目录结构

一个索引的物理形态不是"一个文件"，而是**一棵目录树 + 若干个段 + 一份 commit point**。下面是一个真实分片目录（Elasticsearch 8.x，复合文件默认开启）的形状：

```
/var/lib/elasticsearch/nodes/0/indices/<index-uuid>/0/index/
├── segments_7                       ← commit point：列出"当前完整的段清单 + 每段校验和"，flush/merge 时原子替换
├── _0.si  _0.fnm  _0.cfs  _0.cfe    ← 段 _0：一次 refresh 的产物（复合文件把多文件打成 1 个）
├── _1.si  _1.fnm  _1.cfs  _1.cfe
├── _1_Lucene90_0.liv                ← 段 _1 的 liveness 位图：只有发生过删除才存在，1 bit/doc
├── _3.si  _3.fnm  _3_Lucene90_0.dvd _3_Lucene90_0.dvm   ← 段 _3 未打包（更老的 merge 产物）
├── _3_Lucene90_0.tim _3_Lucene90_0.tip _3_Lucene90_0.doc _3_Lucene90_0.pos
├── _3_Lucene90_0.fdt _3_Lucene90_0.fdx _3_Lucene90_0.nvd _3_Lucene90_0.nvm
├── write.lock                       ← 单写者锁：同一分片同一时刻只允许一个 index writer
└── translog/
    ├── translog-42.tlog             ← 当前世代（append-only，存"原始请求"而不是倒排）
    ├── translog-42.ckp              ← 校验点：记录已 fsync 的偏移
    └── translog-38.tlog             ← 老世代：flush 之后待删，或供副本恢复使用
```

**复合文件的真实意义不是"省文件"，是省 fd**：`.cfe` 是一张小映射表（虚拟文件名 → `.cfs` 内的偏移与长度），Lucene 打开一个段只需要 1~2 个真实 fd。这一点在 22.9 推导 fd 上限时会用到。

### 哪个文件服务于哪种查询

| 文件 | 装什么 | 服务于哪种查询 / 操作 | 缺失或损坏的后果 |
| --- | --- | --- | --- |
| `segments_N` | commit point，段清单 + 校验和 | 节点重启后的恢复入口 | 会回退到上一个 commit point，重放 translog |
| `_x.si` | 段信息（doc 数、版本、codec、是否含删除） | `_cat/segments`、merge 决策、复制 | 段不可用 |
| `_x.fnm` | field infos：字段号、是否 index、doc values 类型、有无 norms | 所有查询的"字段解析"前置 | 字段名对不上，查询报错 |
| `_x.tim` / `_x.tip` | 词典：term 元数据 + FST 索引（常驻堆） | **term / 全文匹配**的第一跳 | 该字段不可搜 |
| `_x.doc` / `_x.pos` | 倒排表（doc id + freq）/ 位置列表 | 匹配与短语、邻近查询 | 短语查询失效（→ comp/21） |
| `_x.nvd` / `_x.nvm` | norms：字段长度（1 B/doc，有损） | **BM25 的长度归一化** | 算分变平（所有文档按"等长"评分） |
| `_x.dvd` / `_x.dvm` | doc values：列式字段值 | **排序、聚合、`filter` 后的二次取数** | 不能排序/聚合 |
| `_x.fdt` / `_x.fdx` | stored fields：`_source` 与 `stored` 字段，按 chunk 压缩 | 返回 `_source`、`fields`、`_doc/id` 点查的取数 | 搜得到但取不出内容 |
| `_x_Lucene90_0.liv` | liveness 位图，1 bit/doc | 每个段的"文档是否已删除"过滤 | 已删文档会重新出现 |
| `_x.cfs` / `_x.cfe` | 上述多文件的打包与索引 | 减少 fd 与目录项 | 退化为散文件，fd × 30 |
| `_x.kdd` / `.kdi` / `.kdm` | BKD 树（数值 / 日期 / geo 点） | **范围查询**走它，不走倒排 | 数值范围查询退化为逐值匹配 |
| `_x.vec` / `.vex` / `.vem` | 向量（HNSW 图 + 向量数据） | kNN 查询（→ comp/26） | 向量字段不可查 |

表里有一行值得单独记：**`norms` 决定算分，`doc_values` 决定排序聚合，`stored fields` 决定返回内容，三者是彼此独立的文件**——所以"给 text 字段加排序"这种需求不是配置问题，是这个字段当初就没写 `dvd`。

---

## 22.4 写入生命周期：buffer → refresh → translog → flush → merge

五个动作，两条正交的语义轴：**可见性轴（buffer → refresh → searcher）**和**持久性轴（buffer → translog → flush → commit）**。这是本篇最该先建立的心智模型：**refresh 管"能不能搜到"，flush 管"机器挂了会不会丢"，两者互不负责**。

### 五个动作各自的账

| 动作 | 触发条件 | 它做了什么 | 谁 fsync | 清 index buffer | 清 translog | 改可见性 | 改持久性 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 写 buffer + translog | 每次 bulk | 文档进内存倒排；原始请求顺序追加到 translog | `durability=request` 时**每个请求** fdatasync | — | — | ✗ | ✓（已 fsync 部分） |
| **refresh** | `refresh_interval` 到 / 该分片 buffer 额度满 / 显式 `_refresh` | buffer → **新段**，打开新 searcher | **不 fsync**（只写进 page cache） | ✓ | ✗ | **✓ 就是这里** | ✗ |
| **flush** | translog ≥ `flush_threshold_size`（512 MB）/ 每 30 min / 显式 | Lucene commit：fsync 段文件 + 写新 `segments_N` + 清空 translog（换世代） | ✓ | ✓（先 refresh） | ✓ | ✗（已可见） | **✓** |
| **merge** | 后台，TieredMergePolicy 决策 | N 个小段 → 1 个大段，墓碑在此物理消失 | ✓（新段落盘） | — | — | 不改变（老段在新 searcher 打开前仍服务） | ✗ |
| **commit point** | flush / merge 完成后 | 替换 `segments_N`，这是崩溃恢复的唯一入口 | ✓ | — | — | — | ✓ |

三条最容易记错的事实：

1. **refresh 不 fsync**。它把段文件写进 OS page cache 就打开 reader 了——所以"搜得到"不等于"落盘了"，机器掉电这一批段全丢，只能靠 translog 重放。
2. **flush 不以"数据量"为节拍，以 translog 大小为节拍**。默认 512 MB，这就是"机器挂了最多丢多少"的上界（在 `durability=request` 下更小，等于未确认请求数）。
3. **index buffer 是节点级共享池，不是每分片私有**。`indices.memory.index_buffer_size` 默认堆的 10%（另有下限 `indices.memory.min_index_buffer_size` 48 MB），由 `IndexingMemoryController` 每 5 s 重新分配：**每分片额度 = 池大小 ÷ 活跃写入分片数**。所以分片数翻倍，每片份额就砍半，refresh 会更频繁地被"额度满"提前触发——这是"分片开太多导致写放大"的一条隐藏路径。

### 时间轴：一次 bulk 的完整一生

```
一次 bulk 从"到达"到"可被搜到、能扛住重启"（单分片，refresh_interval=30s，durability=request）

t=0    ┌──────── index buffer（节点级 10% 堆按活跃分片均分，本片额度 48 MB）+ translog append ────────┐
bulk   │ 文档 → 内存倒排（未落盘，任何查询都看不见）        +  原始请求顺序追加到 translog            │
到达    └──────────┬──────────────────────────────────────────────────────┬────────────────────────────┘
                   │ ① durability=request：本请求 fdatasync 完成才 ack       │ ② 额度满 / 30s 到期 / 显式 _refresh
                   ▼                                                        ▼
            ack 返回客户端                                    refresh：buffer → 新段（写文件，不 fsync）
        （此刻只承诺"主分片已持久"）                             打开新 searcher（SearcherManager 换 reader）
                   │                                                        ▼
                   │                                        ★ 可见性在这一刻产生：_search 能搜到
                   │                                        （GET /_doc/id 更早就读到了：它查 translog）
                   │ ③ translog 累计 512 MB 或 30 min
                   ▼
        flush：Lucene commit → fsync 段文件 → 原子替换 segments_N（新 commit point）→ 清空 translog（换世代）
                   │
                   │ ④ 段数/删除比例触发
                   ▼
        merge（后台 2~4 线程）：N 个小段 → 1 个大段，墓碑在此物理消失 → fsync 新段 → 写新 segments_N
                                → 让 searcher 换读者 → 老段引用计数归零后才 unlink（这就是"磁盘不降"的来源）
```

### 可见性：`refresh_interval` 就是"多快能搜到"

可见性不是布尔值，是一个分布。写请求从 ack 到"能被 `_search` 搜到"的距离，完全由 refresh 的相位决定：

| 量 | 推导式 | `30s` 时 | `1s` 时 |
| --- | --- | --- | --- |
| 平均可见延迟 | `interval ÷ 2`（写落在 refresh 周期内的位置是均匀的） | 15 s | 0.5 s |
| 尾延迟 | `interval + 段打开成 reader 的时间（50~200 ms）` | ≈ 30.2 s | ≈ 1.2 s |
| 每次 refresh 的段大小 | 分片写入速率 × interval | 1.9 MB | 65 KB |

**为什么 `GET /_doc/id` 是实时的，而 `_search` 不是**：两者取数的**数据源根本不同**。`GET` 走的是 real-time GET，按 `_id` 到 **translog + 内存 buffer** 里找最新版本（必要时才回落到段），所以它根本不需要 refresh；`_search` 只能在已打开的段上执行，buffer 里的文档对它不存在。于是同一条文档在两个接口上的"存在时刻"相差最多一个 `refresh_interval`。

| 接口 / 方式 | 数据源 | 延迟 | 受 `refresh_interval` 影响 |
| --- | --- | --- | --- |
| `GET /index/_doc/id` | translog + 内存 buffer，必要时回落段 | ms 级 | ✗ 不受影响 |
| `_mget`（`realtime` 默认 `true`） | 逐个 `_id` 走 translog + 内存 buffer | ms 级 | ✗ 不受影响 |
| `_search` 里的 `ids` 查询 | **走段**，不看 translog | 受 interval 支配 | ✓ 受影响 |
| `_search`（任何查询 DSL） | 已打开的段集合 | 受 interval 支配 | ✓ 完全由它决定 |
| `_count` | 段的 doc 数 − 墓碑 | 受 interval 支配 | ✓ |
| `refresh=wait_for` 的写 | 写请求本身等到一次 refresh | 多等 ≤ interval | ✓（把延迟转移到写路径） |

由此推出"写完立刻读"的正确姿势，按优先级只有三条：

1. **读回权威源**（MySQL / 主库 / 缓存）。这是唯一零延迟、强一致的做法，代价就是一次点查——比把整个索引的 refresh 调到 1s 便宜三个数量级。
2. **按 `_id` 点查**（real-time GET），用它实现 read-your-write：只需要"我自己刚写的那条"能立刻看到，不需要全库可见。
3. **接受延迟并写进契约**："该索引的写入 30 秒内可被搜索命中"写进接口文档，让调用方知道这不是 bug。

还有一个经常被误判为"ES 出 bug"的现象值得在此说明：**同一个搜索连打两次，结果不一样**。原因不是缓存，而是**每个分片的每个副本各自按自己的相位做 refresh**——请求第一次打到副本 A（还差 3 s 到 refresh），第二次打到副本 B（刚 refresh 完）。这在 `refresh_interval: 30s` 的写入密集索引上是常态，不是异常。

### 关键配置（只讲它改的是哪条物理路径）

```json
PUT /products_v1
{
  "settings": {
    "number_of_shards": 12,
    "number_of_replicas": 1,
    "refresh_interval": "30s",
    "translog": {
      "durability": "request",
      "flush_threshold_size": "512mb",
      "sync_interval": "5s"
    },
    "merge": {
      "policy": { "segments_per_tier": 10, "max_merged_segment": "5gb",
                  "floor_segment": "2mb", "expunge_deletes_allowed": 10 },
      "scheduler": { "max_thread_count": 2 }
    }
  },
  "mappings": { "dynamic": "strict" }
}
```

| 配置 | 它改的是哪条物理路径 | 改错的代价 |
| --- | --- | --- |
| `refresh_interval: 30s` | **段产生速率 = 1/30 段/秒/分片**（可见性同时被定在 30 s） | 设 1s ⇒ 段速率 ×30，见 22.6 的算例 |
| `translog.durability: request` | 每个 bulk 返回前一次 fdatasync | 改 `async` ⇒ 崩机丢最多 5 s（RPO 变大） |
| `flush_threshold_size: 512mb` | flush 节拍与**重启时要重放的字节上限** | 调到 8 GB ⇒ 事故一：重放 62 GB/节点 |
| `segments_per_tier: 10` | merge 的层高倍数 ⇒ **层数 ⇒ 写放大** | 调大省写放大但单次 merge 峰值 IO 更大 |
| `max_merged_segment: 5gb` | 顶层封顶大小 ⇒ 层数上限 + 顶层段数 | 调小 ⇒ 层数少但段多、查询慢 |
| `max_thread_count: 2` | 并发 merge 数 = 磁盘随机 IO 的并发源 | 调大 ⇒ merge 抢占查询 page cache |
| `search.idle.after`（默认 30s） | 搜索空闲的分片**不做后台 refresh** | 写多读少的索引上，它会让 translog 悄悄涨满 |

### 写入链路的耗时分解

| 环节 | 量级 | 放大在哪 |
| --- | --- | --- |
| 客户端攒批 | 0~200 ms | 批越小，段越多（每次 refresh 的段大小 = 批大小之和） |
| write 线程池排队 | 0~∞（队列满则 429） | 队列调大只是把 429 换成"长延迟 + 堆堆积" |
| 主分片 buffer + translog fsync | 1~5 ms（NVMe 上 fdatasync ≈ 0.2~2 ms） | `durability=request` 时每个 bulk 一次 |
| 等 refresh | 0 ~ interval，均值 interval/2 | **可见性延迟的主体** |
| 段打开成 reader | 50~200 ms（与段内文件数、字段数相关） | 字段多 ⇒ `.fnm` 大 ⇒ 打开慢 |
| merge 消化 | 异步，均值 0；积压时可达小时级 | **写放大在这里，不在写线程池** |

---

## 22.5 删除与更新：为什么"改一个字"要写两份

### 删除只写一个 bit

删除一个文档时，Lucene **不碰段里的任何数据文件**，它只做两件事：

1. 在**该段**的 `.liv` 位图里把这一位改成 0（每段一张位图，1 bit/doc，本段有 N 个 doc 就是 N/8 字节）；
2. 把这次删除也写进 translog 与 `.liv` 文件的持久版本。

查询时，每个段的 collector 都用这张位图做一次 `AND NOT`——**扫描量一个字节都没少，只是把命中的文档丢掉**。这就是"删了不降盘、删了还是慢"的全部原理。当一个段的所有位都变成 0，整段才可能被整段 unlink。

### 更新是"删 + 写"的两倍

ES 的 `PUT /index/_doc/id` 与 `_update` 是同一个物理动作，分两步落在两个不同的段里：

- **第一步 · 旧文档变墓碑**：在旧段（可能是 merge 出来的 5 GB 大段）的 `.liv` 里把这一位改成 0。旧文档的 0.9 KB **一字节不少地留在原地**。
- **第二步 · 新文档进新段**：新值写进当前 buffer，等下一次 refresh 落成新段，再占 0.9 KB。

结果：磁盘上同时存在 1.8 KB 的物理字节，代表 0.9 KB 的逻辑数据。所以"写放大 2 倍以上"的完整算式是四段乘起来：

**逻辑写入 1 GB × 2（update 双写：新文档 + 未回收的旧文档）× 4（merge 层数，22.6 推导）× 2（1 副本）= 16 GB 磁盘写**

一台盘每天因此要写 16 倍于业务数据量的字节——这就是"库存水位、点赞数、浏览量不要放 ES"这句话的**定量**解释，不是洁癖。

### 六种"删/改"各自的真实代价

| 操作 | 写什么字节 | 空间何时回收 | 查询成本变化 | 适用 |
| --- | --- | --- | --- | --- |
| `index`（新文档） | 0.9 KB 新文档 → 新段 | 不涉及 | 段数 +1/refresh | 正常写入 |
| `update` / 覆盖 `_id` | 0.9 KB 新 + 0.9 KB 墓碑 | **只在 merge 时** | 墓碑被扫后丢弃 | 低频变更 |
| `delete` | 0 字节（只改位图） | 只在 merge 时 | 同上 | 低频删除 |
| `delete_by_query` | 0 字节（批量改位图）+ 一次全量扫描成本 | 只在 merge 时 | 同上 | **应急，不是日常清理** |
| `_forcemerge?only_expunge_deletes=true` | 重写**含墓碑的段**（通常远小于全量） | 立刻（重写后删旧段） | 段数下降 | 只读索引、墓碑多 |
| `_forcemerge?max_num_segments=1` | **重写全部分片** | 立刻 | 段数降到 1/分片 | **只读 + 隔离节点 + 低峰**；活索引禁止 |

一句判据：**墓碑比例 > 30% 时，重建索引比 forcemerge 更划算**（reindex 顺带把 mapping 缺陷也修了，forcemerge 只是原地重写——包括那些不该存在的字段）。这就是 `→ architect/17 §17.11` 把"索引必须可重建"当成硬需求的原因之一。

---

## 22.6 merge 的账：层数、重写字节与 IO 预算

### 段数的两个守恒式

写 merge 之前先立两个守恒式，后面所有数字都由它们推出来：

- **① 段产生速率** `r = (1 / refresh_interval) × 分片数`，单位 段/秒；
- **② 段数（Little 定律）** `N_seg = r × T_reside`，其中 `T_reside` = 一个段从"生成"到"被 merge 吃掉"的平均滞留时间。

式②是判断"merge 是否跟得上"的唯一正确姿势：**段数不是"产生太快"造成的，而是"滞留太久"造成的**。事故一里观察到 1,050 段/分片，反推 `T_reside = 1,050 ÷ (1 段/秒) = 1,050 s = 17.5 min`——也就是说 merge 平均要排队 17.5 分钟才吃掉一个段。**"merge 队列积压"的量化定义就是这个 17.5 分钟**，而不是 `_cat/nodes` 里某个看不懂的数字。

### 层数与重写字节

TieredMergePolicy 的核心是一个"等比层"结构：每一层的段大小按 `segments_per_tier` 倍增长，当同尺寸的段数超过这一层允许的数量，就触发一次合并，把它们升到上一层。

```
段从原子段往上爬（本期算例：floor_segment=2 MB，max_merged_segment=5 GB，segments_per_tier=10）

 写入侧            每层允许的段数 = segments_per_tier = 10
 ────────────────────────────────────────────────────────────────────────────────────────►
 tier-0   2 MB ──(10 段合 1)──► 20 MB ──(10 段合 1)──► 200 MB ──(10 段合 1)──► 2 GB ──► 5 GB 封顶
 floor    段数上界 10          段数上界 10              段数上界 10             段数上界 10   不再合并*
 ────────────────────────────────────────────────────────────────────────────────────────►
 一个字节被重写的次数 = 它爬过的层数 L
 L ≈ log₁₀( max_merged_segment ÷ floor_segment ) = log₁₀(5 GB ÷ 2 MB) = log₁₀(2,560) ≈ 3.4
 ⇒ 稳态写放大 A ≈ L + 一点 expunge 开销 ≈ 4×

 * 顶层 5 GB 段只有两种情况下再被重写：段内删除率 > expunge_deletes_allowed(10%)，
   或者 max_merged_segment 被人为上调（这就是"forcemerge 之后空间回涨"的机制）
```

所以总重写字节的通用式是一行：**稳态 merge 写字节/天 ≈ 日写入字节 W × 层数 L × 副本系数 R**。

**这就是"段大小 S、层数 L 时总重写字节 ≈ 数据量 × L"这句话的完整含义**：你写的每一字节，在它被 delete 或重建之前，平均要被 merge 重写 L−1 次。

### 参数改的是哪一层

| 参数 | 调大 | 调小的代价 | 你在买什么 |
| --- | --- | --- | --- |
| `segments_per_tier`（10） | 层高倍数 ↑ ⇒ 层数 L ↓ ⇒ 写放大 ↓ | 单次合并段数 ↑ ⇒ 峰值 IO 与 merge 延迟 ↑，段更大更多 | 用**峰值 IO 尖峰**换**日均写量** |
| `max_merged_segment`（5 GB） | 顶层更大 ⇒ 层数 ↑ ⇒ 写放大 ↑ | 顶层段数 ↓ ⇒ 查询段数 ↓ | 用**写放大**换**查询段数**（也是分片 ≤ 50 GB 的理由） |
| `floor_segment`（2 MB） | 小于 floor 的段被优先合并 ⇒ 小段尽快消失 | 小段滞留 ⇒ 段数 ↑ | 用**写放大**换**段数**（refresh 很碎的索引要谨慎） |
| `expunge_deletes_allowed`（10%） | 允许更脏的段存在 ⇒ 空间放大 ↑ | 更频繁的 expunge ⇒ 写放大 ↑ | 空间 vs 写 |
| `max_thread_count`（2~4） | merge 并行度 ↑ ⇒ 滞留时间 ↓ | 与查询抢 page cache、抢 IOPS | 用**查询延迟**换**段数** |

**注意一个不对称**：`segments_per_tier` 与 `max_merged_segment` 调错方向时，收益是立刻的、代价是两周后才出现的（写放大慢慢把盘写满）；参数默认值之所以"看起来保守"，是因为 Lucene 的作者把代价放在了长周期那一侧。

### 算例一：4.2 亿文档 / 380 GB 的稳态 IO

| 步骤 | 推导式 | 结果 |
| --- | --- | --- |
| ① 数据量 | 4.2 亿 doc × 0.9 KB | 378 GB ≈ 380 GB（主分片，含倒排与 doc_values，不含副本） |
| ② 日写入 | 新增 4,400 万 × 0.9 KB = 39.6 GB；更新 2,900 万 × 0.9 KB = 26.1 GB | `W = 40 + 26 = 66 GB/天 = 66 × 1024 MB ÷ 86,400 s ≈ 0.78 MB/s` 均值 |
| ③ 写放大 | 层数 3.4 向上取整 + expunge 开销 | `A = 4` |
| ④ 副本 | 1 副本 | `R = 2` |
| ⑤ 日均磁盘写 | `0.78 × 4 × 2` | **6.3 MB/s** |
| ⑥ 峰值系数 | 业务写入集中在 1/3 时间里，merge 追赶在低峰期仍在跑 | `k = 6` |
| ⑦ 峰值写带宽 | `6.3 × 6` 为集群级，再 ÷ 6 节点 | **38 MB/s 集群级 ≈ 6.3 MB/s/节点** |

**结论一：在这个量级上，带宽根本不是约束。** 3.2 TB NVMe 顺序写 1.5 GB/s、随机混合负载也能到 800 MB/s 量级，6.3 MB/s 只占 0.8%。所以事故一、事故二的磁盘 util 冲到 96% 不是因为"数据量大"，而是因为 **IOPS + fsync + 段文件数**：每天 86,400 段/分片（1s refresh）产生的不是字节，而是**百万次文件创建/关闭/目录 fsync**。

**结论二：余量不是留给带宽的，是留给空间的。** 一次 expunge 必须先写新段再删旧段，峰值额外空间 ≈ 待回收的段大小。删除率 40% 时，380 GB 的索引要 152 GB 空闲（40%）才排得下一次全量 expunge——**这就是"磁盘必须留 15%~25%"这条纪律的推导过程，也是事故二死锁的成因**。带宽预算与"分片 ≤ 50 GB"的完整版本（10 亿级、192 MB/s 的算例）在 `→ architect/17 §17.4`。

### 算例二：`refresh_interval` 从 30s 改成 1s 会发生什么

每分片每秒写入量 = `66 GB/天 ÷ 12 分片 ÷ 86,400 s = 66 × 1024 × 1024 KB ÷ 12 ÷ 86,400 ≈ 65 KB/s/分片`。把它代进两个 interval：

| 项 | `refresh_interval: 30s` | `refresh_interval: 1s` |
| --- | --- | --- |
| 每次 refresh 的段大小 | 65 KB × 30 ≈ **1.9 MB**，正好落在 `floor_segment = 2 MB` 附近，被当作"正常层"温和合并 | 65 KB，是 floor 的 **1/30**，被判为"必须尽快合并的碎片" |
| 段产生速率（每分片） | `1/30 ≈ 0.033 段/秒` | `1 段/秒`（**×30**） |
| 段产生速率（12 分片） | `0.4 段/秒` = 34,560 段/天 | `12 段/秒` = **1,036,800 段/天** |
| 健康滞留时间 | 层数 3.4 × 每层等 10 段约 10 s ≈ 34 s | 同上 ≈ 34 s |
| 健康段数 `N_seg = r × T_reside` | `0.033 × 34 ≈ 1.1` 段/分片 | `1 × 34 ≈ 34` 段/分片（勉强可接受） |

积压时的正反馈链（这才是事故发生的地方，每一跳都是可观测的）：

- 磁盘 util 60% → 单次 merge 耗时 ×3 → `T_reside` 34 s → 102 s → 段数 **102 段/分片**；
- 每次查询要打开的段 ×3 → page cache 命中率下降 → 读盘更多 → util 冲到 75%；
- 单次 merge 耗时 ×6 → `T_reside ≈ 200 s` → 段数 **200 段/分片** → util 96%；
- `T_reside` 涨到 1,050 s ⇒ `N_seg = 1 段/秒 × 1,050 s = 1,050` 段/分片（与事故一实测一致）。

**这段推导是本篇最值钱的东西**：`refresh_interval` 不是一个"可见性开关"，它是**段产生速率的分母**；而段数与 merge 滞留时间互为因果，构成一个正反馈——一旦 util 超过 60%，这条链就会自己往前走，**回滚配置只能停止恶化，不能恢复现场**（已经生成的段必须靠 merge 一个个吃掉）。这与 `→ architect/17 §17.1` 事故一的形态是同一条链，只是本篇把它写成了可计算的版本。

---

## 22.7 架构边界：段不可变推出的一切

| 你想要的 | 段不可变允许吗 | 靠什么绕过（以及绕不过的部分） |
| --- | --- | --- |
| 写完 1 ms 内搜到 | ✗ 只有 `refresh=wait_for`（每个写等一个 refresh） | 读回权威源；或按 `_id` 点查（translog 是实时的）；**绕不过"全库实时"与"写吞吐"的倒数关系** |
| 修改一个字段的值 | ✗ 只能整篇重写 | 文档拆小（大文档 = 大重写）；高频变动字段拆到独立索引/别存放 ES |
| 立刻物理删除数据（合规） | ✗ 墓碑只在 merge 时消失 | 按 ID `delete` + `_forcemerge`（只读索引）；合规级删除必须**按时间分索引 + 整索引删除** |
| 改 mapping 字段类型 | ✗ 段内 `.fnm` 与倒排已固化 | 新建索引 + reindex + 别名切换（22.10） |
| 单分片内保证事务 | ✗ 每段独立、无跨段原子性 | 用单文档原子性（`_bulk` 里靠 `_id` 幂等）；跨文档一致交给权威源（→ architect/03） |
| 精确的"文档数 = 磁盘占用" | ✗ `docs.count` 含墓碑 | 用 `_count` 比 live 数；用 `pri.store.size` 看物理量；两者差值就是墓碑 |
| 按 `_id` 高频覆盖更新 | ✗ 每次都是一份新字节 + 一份墓碑 | 合并写（攒批 upsert 只写最后一次）；计数类用 Redis/ClickHouse |
| 一块盘装下"很大的索引" | ✗ 分片 ≥ 段，段有 5 GB 顶层上限 | 多分片 + 冷热分层；单分片 > 50 GB 就该拆（→ architect/17 §17.13） |
| 长期持有查询快照 | △ PIT / scroll 可以，但会**阻止 merge** | PIT TTL 设短（2 min 量级）+ 必须 `close` |

**一句话总括**：段不可变的直接推论是「**ES 里所有"修改"都要被翻译成"新建 + 标记"**」，于是所有"改"型需求都要在架构层重新定价：要么改成"追加型"（日志、消息），要么把可变部分挪出 ES（库存、计数、状态），要么接受重建（mapping、合规删除）。

---

## 22.8 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到物理形态） | 定位 | 修复 | 防复发 |
| --- | --- | --- | --- | --- |
| 磁盘 util 长期 90%+，写入 429 | merge 写放大 × 层数 与查询抢同一块盘 | `_nodes/stats/indices/merges`（`current` 长期 > 0、`total_throttled_time` 增长）+ `iostat` | 降 refresh 频率、关掉 update 风暴源、必要时重建索引 | 峰值写带宽按 6.3 MB/s + 空间余量 15%~25% 规划 |
| 段数持续上涨，回滚也不降 | 段不可变：已生成的段必须靠 merge 消化 | `_cat/segments` 按分片统计 + `N_seg = r × T_reside` 反推 `T_reside` | 先止血（停 refresh 抖动、关 update），再考虑 `_forcemerge`（只读索引） | refresh 分级：写入密集 30s、会话 1s、批量导入 -1 |
| 磁盘一点不降，删了几亿文档 | 墓碑只是位图，空间要 merge；或 PIT/scroll 持有旧段引用 | `_count` vs `docs.count` 算删除率；`_nodes/stats/indices/search.open_contexts` | 关闭泄漏的 PIT；墓碑 > 30% 直接重建索引 | PIT TTL 短 + 客户端必 close + 定时巡检 `open_contexts` |
| 重启后 6 小时才 green | translog 太大 ⇒ 重放字节 = 分片数 × translog 大小 | `_nodes/stats/indices/translog.size_in_bytes`、`earliest_last_modified_age` | 临时：加节点分摊；长期：把 `flush_threshold_size` 调回 512 MB | 把 translog 大小做成监控指标；写多读少的索引要么调小 flush 阈值，要么用探活查询让 refresh 正常跑（绕开 search idle） |
| 老索引半夜突然慢，写入还拒 | `_forcemerge` 抢了共用节点的 IO | `_tasks` 查正在跑的 `forcemerge`；`_cat/segments` 看段数骤降 | kill 任务（不可中断，只能等）；扩容；之后隔离节点 | forcemerge 只在只读索引 + `node.attr` 隔离节点 + 低峰分批；按异步任务对待（客户端 30 s 必超时） |
| 搜得到但取不出字段 / 排序报错 | `.fdt` 没开（`_source` 关）或字段没写 `.dvd`（`doc_values: false`） | 看 mapping 的 `doc_values`/`enabled`；`_cat/fielddata` | 该字段重建索引并开启 doc_values | mapping 评审：排序/聚合字段必须 doc_values |
| 分片间数据量极不均匀 | 路由热点 + 段顶层大小差异（大分片段更少更大） | `_cat/shards` 看 store 与 doc 数分布 | 大租户独立索引 / routing 打散 | 上线前按租户分布画洛伦兹曲线 |
| `too_many_open_files` / 节点启动慢 | 段数 × 每段 fd（复合文件 3~4 个，非复合 30 字段 ≈ 90 个） | `ls /proc/<pid>/fd \| wc -l`、`_cat/segments` | 开 `index.use_compound_file`；降段数 | 段数进监控；`ulimit -n` ≥ 65,536 |
| 主副分片文档数不一致 | 副本在 flush 前的段与主不同（正常），或副本恢复中断 | `_cat/shards` 的 `docs` 列与 `_count?preference=_primary` | 等恢复完成；持续不一致则重建分片 | 用 `_count` 而非 `docs.count` 做核对基准 |

---

## 22.9 观测：指标、拐点与告警

### 三个必须先看的指标组

| 指标（Prometheus 名 / API） | 从哪取 | 拐点阈值 | 它意味着什么 |
| --- | --- | --- | --- |
| 段数 / 分片 | `elasticsearch_indices_segments_count` ÷ 分片数；或 `_cat/segments` 分组计数 | 基线 ≈ `(segments_per_tier−1) × 层数 + 顶层段数 = 9×3 + 7 ≈ 34`；> 100 警告、> 300 严重 | merge 滞留时间在变长（**第一批报警**，见下） |
| merge 队列 | `_nodes/stats/indices/merges.current`、`total_throttled_time_in_millis` | `current` 持续 > 0 达 10 min；throttled 增速 > 0 | merge 与查询在抢 IO |
| translog 大小 | `elasticsearch_indices_translog_size_in_bytes`、`uncommitted_operations` | 单分片 > 512 MB（或等于你的 flush 阈值） | 下次重启要重放的字节数 |
| 删除率 | `1 − _count / docs.count` | > 30% | 空间在漏；> 40% 就该重建而不是等 merge |
| refresh 次数 | `_nodes/stats/indices/refresh.total` 的导数 | 稳态为 `1/interval × 分片数`；偏离 2× 即异常 | 段产生速率 |
| 搜索线程池 | `_cat/thread_pool/search` 的 `queue`、`rejected` | `queue` > 500 或 rejected > 0 | 段数过多已经把查询拖垮 |
| 堆与段元数据 | `jvm.mem.heap_used_percent`、`segments.memory_in_bytes` | 堆 > 75%（同机 page cache < 25%） | 堆吃掉了 page cache ⇒ 每次查询都读盘 |

### 22.9.1 为什么"段数"必须是第一报警指标

三个独立理由，任何一个单独都足以让它排第一：

1. **它是查询成本的乘数，不是加数**。一个查询要遍历每个段：每段一次 postings 遍历 + 一次 doc values 块解压 + 一次 liveDocs 位图 AND。取单段固定开销 ≈ 30 μs，则 40 段 ⇒ 1.2 ms，1,050 段 ⇒ 31.5 ms——**单分片查询的底噪从 1.2 ms 变 31.5 ms，而这台节点上还有 12 个分片在抢同一个搜索线程池**。端到端 P99 从 60 ms 走到 2,400 ms 就是这么来的。
2. **它乘上 fd 就是资源上限**。复合文件开启时每段 3~4 个 fd（`.cfs` + `.cfe` + `.si` + 可能有 `.liv`）：1,050 段/分片 × 12 分片 = 12,600 段 × 3 = **37,800 fd**，占满 `ulimit -n 65,536` 的 58%；关闭复合文件时每段 ≈ 字段数 × 4 + 6（30 字段 ≈ 126 个），同样规模要 **151 万 fd**——节点根本起不来。这就是 `index.use_compound_file` 默认 `true` 的真实动机。
3. **它会污染所有其他指标**。段数上涨 ⇒ page cache 命中率下降 ⇒ 读盘变多 ⇒ merge 更慢 ⇒ 段数再涨。等你看到"磁盘 util 96%"时，段数已经涨了两周。

### 22.9.2 `search.idle.after` 与搜索线程池的相互作用

`index.search.idle.after`（默认 30s）的语义是：**分片连续 30 s 没有收到 search/get 请求，就被判为"搜索空闲"，ES 不再为它做后台 refresh**。这条机制有三个连锁后果，全都反直觉：

| 场景 | 后果 | 应该怎么做 |
| --- | --- | --- |
| 写多读少的索引（后台报表、归档回流） | refresh 被跳过 ⇒ buffer 只按 flush 阈值落盘 ⇒ **translog 涨到 512 MB（或你设的值）才 flush** | 这是事故一的机制；把 `flush_threshold_size` 留在 512 MB，并监控 translog 大小 |
| 冷索引被第一次查询 | 分片"醒"过来前要先做一次 refresh ⇒ **首个查询慢一个数量级** | 首屏预热（启动后打一发查询）；别把"第一跳延迟"当成容量问题 |
| 段数与线程池 | refresh 被跳过时，写量的增长不会转化为段数增长，**看起来一切正常**；一旦有人来查，段数瞬间补涨 | 用 `refresh.total` 的导数而不是"段数"单独判断 refresh 是否在工作 |

搜索线程池与段数之间的关系可以写成一句可操作的话：**线程池 queue 的每一格都在持有一个正在遍历 N 个段的查询**。所以 `queue` 与段数不是两个问题——段数 × 并发 = 堆与 CPU 的实际占用，`queue` 只是这个乘积溢出的表现。

### 22.9.3 容量三条线

| 线 | 阈值 | 内核原因（本套视角） | 观测方法 | 越线之后 |
| --- | --- | --- | --- | --- |
| **段数** | ≤ 3× 基线（约 100 段/分片）；> 500 已进入事故区 | 段不可变 ⇒ 查询要遍历每段；fd = 段 × 3~90；段元数据驻留堆 | `_cat/segments` 按 `shard` 分组计数；`indices_segments_count` | 降 refresh 频率、重建、只读索引 forcemerge |
| **字段数（mapping）** | 单索引 ≤ 1,000 字段（默认 `index.mapping.total_fields.limit` = 1000）；> 300 就要警觉 | 每个字段在每个段都有一份 `.fnm` 条目 + 可能的 `.dvd`/`.nvd`：**字段数是段内文件与堆元数据的乘数**，且 mapping 变更会推高 cluster state | `_cluster/state` 大小、`GET /index/_mapping` 字段计数 | `dynamic: strict` + `flattened`；或分索引；越线只能重建 |
| **堆与 page cache** | 堆 ≤ 物理内存 50%（≤ 26~31 GB）；page cache 留给段文件 | 堆吃的是段元数据 + 查询中间态 + 段位图；page cache 吃的是段文件本身——**两者抢同一块内存**，堆多占一分，读盘就多一分 | `jvm.mem.heap_used_percent`、`segments.memory_in_bytes`、节点 `node_memory_Cached_bytes` | 减分片、减段数、禁 `fielddata`、上冷热分层 |

三条线之间是**乘数关系**而不是并列关系：`堆压力 ≈ 段数 × 字段数 × 单位段元数据`。所以"字段数从 300 涨到 11 万"这种事故（mapping 爆炸）在段数健康时也可能直接打爆堆。

形态换挡（L0 → L5、什么时候从单节点走到分集群、什么时候上可搜索快照）不在本篇展开 → `→ architect/17 §17.13`。

### 22.9.4 采集命令

```bash
# ① 段：按分片看数量/大小/删除比例（只看总数会漏掉"一个分片 1,050 段"这种局部事故）
curl -s "localhost:9200/_cat/segments/products_search?v&h=shard,segment,size,docs.count,docs.deleted,compound&s=shard,size:desc" | head -40

# ② merge 与 translog：current 长期 > 0 就是积压；throttled 在涨说明 IO 已经打满
curl -s "localhost:9200/_nodes/stats/indices/segments,merges,refresh,translog?filter_path=nodes.*.indices.{segments.count,segments.memory_in_bytes,merges.current,merges.total_time_in_millis,merges.total_throttled_time_in_millis,refresh.total,translog.size_in_bytes,translog.uncommitted_operations}"

# ③ 逻辑文档数 vs 墓碑数：删除率 = 1 - _count/docs.count，> 30% 准备重建
curl -s "localhost:9200/_cat/indices/products_search?v&h=health,docs.count,docs.deleted,store.size,pri.store.size"
curl -s "localhost:9200/products_search/_count?preference=_primary"

# ④ 段数的两个下游：搜索线程池队列，以及"有多少查询正持有旧段"（PIT/scroll 泄漏）
curl -s "localhost:9200/_cat/thread_pool/search,write?v&h=node_name,name,active,queue,rejected"
curl -s "localhost:9200/_nodes/stats/indices/search?filter_path=nodes.*.indices.search.open_contexts"
```

---

## 22.10 零停机重建：别名、双写、追平与回滚

重建的**根本原因全部来自段不可变**：mapping 类型固化在 `.fnm` 里、analyzer 改了历史段不会重算、墓碑只能靠重写回收。所以重建不是"救火手段"，是**常规操作**，必须有流程、有验收、有回滚。

**唯一的切流开关是别名**。代码里出现 `products_v1` 这样的物理索引名就是架构违规——因为一旦如此，"回滚"就从"改一个指针（1 秒）"变成"发一次版（30 分钟）"。

```
T0 ─────── T1 ───────────── T2 ────────────── T3 ───────────── T4 ────────── T5
 │          │                │                │                 │              │
别名 products_search ═════(一直指向 v1)════════════════════════┌─原子切换(1 ms)─┐
 │          │                │                │                │              └──► v2
 │          └ ① 建 v2：显式 mapping，别名暂不指向
 │             ② 全量 reindex（从权威源，不从 v1）+ 限速
 │                             └ ③ 双写开始（应用或 CDC 同时写 v1 + v2，_id 相同）
 │                                              └ ④ 追平：doc 数 + update_time 水位 + 抽样比对
 │                                                              ⑤ 切流 → 观察 30 min
 │                                                                            ⑥ 停双写 → 保留 v1 7 天
 └── 回滚路径：任意时刻把别名指回 v1（≤ 1 s），v2 留着继续追平
```

| 阶段 | 动作 | 段视角的验收（不达标不许进下一阶段） | 段视角的坑 |
| --- | --- | --- | --- |
| ① 建 v2 | 显式 mapping + 模板；别名暂不指向 | 字段数 ≤ 300，`dynamic: strict` 已生效 | 用 `dynamic: true` 建 v2 = 把旧的 mapping 缺陷原样复制一份 |
| ② 全量灌数 | `_reindex` 从**权威源**；`slices=auto`；`requests_per_second` 限速 | **v2 的段数/分片 ≤ 60**（灌数期间必须盯着，见下） | 从 v1 reindex ⇒ 把 v1 的墓碑与旧 mapping 一起继承过来；不限速 ⇒ 灌数自己的 merge 把在线索引打死 |
| ③ 双写 | 应用或 CDC 同写 v1 + v2 | v2 追平 lag < 5 s | 双写期间改 v2 的 mapping = 灾难，必须冻结变更 |
| ④ 追平与比对 | ① `_count?preference=_primary` 差 < 0.01% ② `max(update_time)` 水位差 < 5 s ③ 抽样 1,000 条逐字段比 ④ **段数健康** | 四项全过 | 用 `_version` 比对无意义（两索引各自计数）；`docs.count` 含墓碑也不可比 |
| ⑤ 切流 | 同一个 `_aliases` 请求里 `remove v1 + add v2` | 切流后 30 min：错误率、P99、零结果率、段数均不恶化 | 只切"读别名"不切"写别名" = 读新写旧 |
| ⑥ 收尾 | 停双写 → v1 保留 7 天 → 删除 | v1 删除前确认无任何请求 | 立刻删 v1 = 放弃回滚 |

**三个只有"段视角"才会提的坑**：

1. **灌数期就是段爆炸期**。用默认 `refresh_interval: 1s` 灌 4 亿文档 ⇒ 段产生速率 1 段/秒/分片，灌 8 小时 = 28,800 段/分片。灌数前必须 `refresh_interval: -1`，灌完再 `_forcemerge?max_num_segments=1`（此时它还是只读的），最后才恢复 `30s`。
2. **切流瞬间不要顺手 forcemerge**。切完就想"合并成 1 段让它快点"是最常见的自伤：此刻 v2 正在接双写 + 接全量流量，forcemerge 会把 IO 全吃掉（事故三）。顺序必须是：**停双写 → 只读 → forcemerge → 归档**。
3. **回滚窗口内 v1 的 merge 还在跑**。v1 那 7 天里段仍在合并（它还在收双写），所以"保留 v1"的磁盘成本是**满额**的，不是"躺着的静态数据"。容量规划要留 v1 + v2 两份共 2× 空间。

### 切流与回滚的 curl 序列

```bash
# ① 建 v2（别名不指向它）+ ② 从权威源灌数（异步，限速保护在线索引）
curl -XPUT localhost:9200/products_v2 -H 'Content-Type: application/json' -d @products_v2.mapping.json
curl -XPOST 'localhost:9200/_reindex?slices=auto&requests_per_second=5000&wait_for_completion=false' \
     -H 'Content-Type: application/json' -d @reindex_from_mysql.json

# ③ 追平判据：主分片 live 文档数 + 业务 update_time 水位（两项都必须过）
curl -s 'localhost:9200/products_v2/_count?preference=_primary'
curl -s 'localhost:9200/products_v2/_search?size=0' \
     -H 'Content-Type: application/json' -d '{"aggs":{"w":{"max":{"field":"update_time"}}}}'

# ④ 原子切换：同一个请求里 remove + add，不存在"两边都不指"的中间态
curl -XPOST localhost:9200/_aliases -H 'Content-Type: application/json' -d '{
  "actions":[
    {"remove":{"index":"products_v1","alias":"products_search"}},
    {"add":{"index":"products_v2","alias":"products_search","is_write_index":true}}
  ]}'

# ⑤ 回滚：同一个接口反着来，1 秒完成；v1 数据与双写保留 7 天
curl -XPOST localhost:9200/_aliases -H 'Content-Type: application/json' -d '{
  "actions":[
    {"remove":{"index":"products_v2","alias":"products_search"}},
    {"add":{"index":"products_v1","alias":"products_search","is_write_index":true}}
  ]}'
```

### 写入侧代码：显式 `refresh: false` + 业务侧读回

`Indexer` 只有三个字段（`Base string` 集群地址、`HTTP *http.Client`、`DB *sql.DB` 权威源），定义从略：

```go
// Bulk：批量写入的唯一入口。refresh 交给索引设置，绝不逐条 refresh
func (ix *Indexer) Bulk(ctx context.Context, docs []ProductDoc) error {
	var body bytes.Buffer // 5 MB / 500 条 / 1s 先到者触发；攒批越碎，段越多
	for _, d := range docs {
		meta, _ := json.Marshal(map[string]any{
			"index": map[string]any{"_index": "products_search", "_id": d.SKUID}, // 别名 + 幂等键
		})
		doc, _ := json.Marshal(d)
		body.Write(append(append(meta, '\n'), append(doc, '\n')...))
	}
	// refresh=false 是默认值，显式写出来是为了防止后人加一个 refresh=true
	u := ix.Base + "/_bulk?refresh=false&timeout=30s"
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, u, &body)
	req.Header.Set("Content-Type", "application/x-ndjson")
	resp, err := ix.HTTP.Do(req)
	if err != nil {
		return ErrRetryable // 连接级失败：退避重试
	}
	defer resp.Body.Close()
	if resp.StatusCode == 429 || resp.StatusCode >= 500 {
		return ErrRetryable // 429 靠退避解决，不靠加大队列
	}
	// 逐 item 判错：Errors 为真时 4xx 进死信、429 进重试（部分失败最易被忽略）
	var r struct{ Errors bool; Items []map[string]struct{ Status int } }
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return err
	}
	if r.Errors {
		return ErrPartial
	}
	return nil
}

// 写后立刻读：走权威源（强一致、零 refresh 延迟），不为 read-your-write 打 _search
func (ix *Indexer) ReadBack(ctx context.Context, sku string) (*Product, error) {
	return loadProduct(ctx, ix.DB, sku)
}
```

---

## 22.11 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 段 | 一次 refresh 的产物；**完全不可变**、自包含、只读；改它的唯一方式是"整段重写"或"整段删除" |
| 可见性 | `refresh` 生成新段 + 打开新 searcher 才可见；可见性延迟 ≈ `refresh_interval` |
| 段产生速率 | `r = (1/refresh_interval) × 分片数`；与可见性延迟是倒数关系，不可能同时变好 |
| 段数 | `N_seg = r × T_reside`（Little 定律）；段数涨 = merge 滞留时间涨，不是"产生太快" |
| translog | 存**原始请求**不存倒排；`durability=request` 每请求 fsync；重启要重放它 ⇒ 它的大小就是你 RTO 的一部分 |
| flush | 以 translog 512 MB / 30 min 为节拍：fsync 段 + 写 commit point + 清 translog；**持久性边界在这里** |
| commit point | `segments_N`，崩溃恢复的唯一入口；原子替换，所以恢复只会回到某个一致的段集合 |
| 删除 | 只改 `.liv` 位图（1 bit/doc）；空间与扫描成本一点不减 |
| 更新 | 删旧 + 写新 ⇒ 至少 2× 写放大；× 层数 4 × 副本 2 ⇒ 净 16× |
| merge | 层数 `L ≈ log_segments_per_tier(max_merged_segment/floor_segment)`；重写字节 ≈ 数据量 × L |
| 空间回收 | 只有 merge 会回收；墓碑 > 30% 就该重建，因为 expunge 要 40% 空闲盘 |
| forcemerge | 整段重写的 IO 炸弹；只读索引 + 节点隔离 + 低峰；客户端必超时，按异步任务对待 |
| 第一批报警 | **段数/分片**（乘数：查询成本、fd、堆元数据），其次 translog 大小、删除率、merge backlog |
| 写后读 | `_search` 不实时但 `GET /_doc/id` 实时（查 translog）⇒ "写完立刻读"就读回权威源或按 id 点查 |
| 切流 | 别名是唯一开关；回滚 = 改指针 = 1 秒；重建顺序 = 建 v2 → 灌数（`refresh: -1`）→ 双写 → 追平 → 切 → 停双写 → forcemerge |

---

## 22.12 自检题

**1. 为什么一条 0.9 KB 文档的「更新」，在磁盘上要写 16 倍？把这四个乘数逐个说出来，并指出哪个最容易被人忽略。**
<details><summary>答案</summary>

`逻辑 1 字节 × 2（update 双写）× 4（merge 层数）× 2（副本）= 16`。

- **2×（update 双写）**：旧文档在旧段里改不了，只能标墓碑；新值写进新段。旧字节要等到 merge 才回收。
- **4×（merge 层数）**：`L ≈ log₁₀(max_merged_segment / floor_segment) = log₁₀(5 GB / 2 MB) ≈ 3.4`，向上取整加 expunge 开销 ≈ 4。按 `segments_per_tier` 为 k 时更一般的式子是 `L ≈ log_k(顶层/底层)`。
- **2×（副本）**：写放大与副本数线性相关，副本数也是读吞吐，属于同一笔账。
- 最容易忽略的是**第二项**：因为它是"两周后才发作"的成本（写放大慢慢把盘写满），而第一项在大盘 util 上立刻可见。这也是为什么"库存水位、点赞数"必须挪出 ES —— 高频 update 是把 2× 直接变成常态。
</details>

**2. 有人把 `refresh_interval` 从 30s 改成 1s。请用公式说明段产生速率怎么变，以及为什么"回滚配置"不能恢复现场。**
<details><summary>答案</summary>

产生速率 `r = (1/interval) × 分片数`：30s ⇒ `1/30 × 12 = 0.4 段/秒`（34,560 段/天）；1s ⇒ `1/1 × 12 = 12 段/秒`（1,036,800 段/天），**×30**。

段数不只看产生速率，还看滞留时间：`N_seg = r × T_reside`。健康时 `T_reside ≈ 层数 3.4 × 每层等 10 段 ≈ 34 s` ⇒ 约 34 段/分片，尚可接受。但段数 ↑ ⇒ 每次查询要打开更多段 ⇒ page cache 命中率下降 ⇒ 读盘更多 ⇒ merge 更慢 ⇒ `T_reside` 变长 ⇒ 段数更多，形成正反馈。实测到 1,050 段/分片时，反推 `T_reside = 1,050 s = 17.5 min`。

**回滚只能停止恶化**：已经生成的段是不可变的文件，必须靠 merge 一段一段吃掉；而 merge 是纯 IO 活，盘已经 96% util，只能排队。所以"改回来"只是让分母不再变小，积压仍要按 `段数 × 段平均大小 ÷ merge 吞吐` 的小时数慢慢消化。
</details>

**3. `GET /index/_doc/id` 能在写入后 1 ms 内读到，`_search` 却要等一个 refresh。这背后的机制差别是什么？由此推出的"写完立刻读"正确姿势是什么？**
<details><summary>答案</summary>

机制差别只有一个：**数据源不同**。`GET /_doc/id` 走的是 `real-time GET`：先按 `_id` 查 translog（以及内存 buffer 里的未提交操作）找到最新版本，如果该版本还在内存里，直接从 `_source` 返回——**完全不经过段，所以不需要 refresh**。`_search` 只能在已经打开成 reader 的段上执行，而 buffer 里的文档还没有段，所以必须等 refresh。

正确姿势（按优先级）：① **读回权威源**（MySQL/主库），强一致、零延迟，代价是一次点查；② 需要"我写的那条立刻能查到"时，按 `_id` 点查（等价于 read-your-write）；③ 只有展示类、非关键路径才接受 refresh 延迟，并把它写进接口契约（"30 秒内可见"）。

**反面**：`refresh=wait_for` 能让这次写等到可见，但每次写都多等一个 refresh 周期，写吞吐按 `1/(interval/2)` 级别下降；`refresh=true` 更糟，等于每条文档一个新段——这是把段数当燃料烧。
</details>

**4. 一个索引 40% 的文档是墓碑，盘已经 86%。为什么 `delete_by_query` 和"再删一批"都救不了？为什么连 merge 也排不上？正确的动作是什么？**
<details><summary>答案</summary>

`delete_by_query` 只把 `.liv` 位图的对应位改成 0，**一个字节都不释放**；墓碑仍占盘、仍被每个查询扫描后丢弃。空间只能靠 merge 重写段来回收。

而 merge 排不上的原因是**它需要先有空间**：expunge 一个含墓碑的段，必须先写出新段（≈ 段内 live 数据量）再删旧段。删除率 40% 的 316 GB 索引，峰值额外需要约 127 GB（索引的 40%），盘上只剩 14%，于是 TieredMergePolicy 找不到可执行计划 ⇒ 墓碑只增不减 ⇒ 盘更满。这是正反馈死锁。

正确动作：**新建索引 + 从权威源 reindex + 别名切换**（22.10 的六阶段）。理由有三：① reindex 不需要"先有空间"（旧索引可以边写新边留旧，只要总容量够一份）；② 顺带修掉历史上积累的 mapping 缺陷；③ 22.5 节那条判据——墓碑 > 30% 时重建比原地重写更划算。应急手段是给只读分片 `_forcemerge?only_expunge_deletes=true`，但活跃写索引上做它就是事故三。
</details>

**5. 你需要在下午三点对一个 2.4 TB 的归档索引跑 `_forcemerge?max_num_segments=1` 省盘。请列出必须满足的前提，并说明为什么这个操作"几乎必然超时"。**
<details><summary>答案</summary>

必须满足五个前提：① 索引**只读**（不再收写入，否则 merge 与写入互相拖）；② 节点隔离（`node.attr` 打标签，让归档索引独占节点——事故三里它和在线搜索共用节点，直接打了全站 3 小时 40 分）；③ 低峰窗口（对"共用节点"这件事做时间上的隔离）；④ 分批（`_forcemerge` 一次只对一个索引调用，多个归档索引串行推进，绝不并发）；⑤ 先算清墓碑比例（< 10% 就是纯赔，> 40% 才值得）。

为什么必然超时：重写量 = 读 4.8 TB + 写 4.8 TB（含副本），临时空间 ≈ 每分片 400 GB。在受 merge 线程数（2~4）限制的吞吐下，即使按 200 MB/s 也要 `9.6 TB ÷ 200 MB/s ≈ 13.4 小时`，而客户端默认 30 s 就一定返回 504 —— **任务仍在服务端跑**。所以必须按异步任务对待：`wait_for_completion=false`、用 `_tasks` 观察进度、绝不重发（重发会并行两份 IO，事故三就是重发后 util 直接 100%）。
</details>

---

## 22.13 延伸阅读

- 段的生命周期在架构层的完整代价账（段数曲线、refresh 分级的写入预算、merge 带宽）+ 零停机重建时序：`→ architect/17 §17.3、§17.4、§17.11`
- 容量形态与换挡（L0 → L5、什么时候必须从单节点走到分集群、什么时候上可搜索快照）：`→ architect/17 §17.12、§17.13`
- 段内倒排的物理结构（FST 词典、跳表、位置信息、`norms` 与 BM25 的关系）：`→ comp/21`
- 为什么 ES 永远是"可重建的派生索引"，以及存储形态决策树：`→ architect/03`、`→ eng/37 §37.4`
- 向量字段的段内形态（`.vec`/`.vex`/`.vem` 与 HNSW 图）与本篇同属"段不可变"的推论：`→ comp/26`
- RAG 场景下"索引不可重建"的代价（权威源缺字段 ⇒ 线上索引成为唯一副本）：`→ llm/20`
- 本篇所在的大纲与阅读路线：`[0-summary.md](0-summary.md)`
