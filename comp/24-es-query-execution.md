# 24. 查询执行：query vs filter、BM25、scroll 与深分页

> **状态**：`已完成（初稿）` ｜ **所属**：第五部分 · 检索与向量 ｜ **编号**：24 / 53
> **本篇定位**：21 篇讲清了「一个 term 怎么找到 doc」，22 篇讲清了「段为什么不可变」，23 篇讲清了「一次查怎么扇出到 N 个分片再归并」。本篇是这三者的**下一层**：**一个查询条件进到分片内部之后，到底被翻译成哪条执行路径、花掉多少 CPU 和多少堆**。倒排结构一律不复述（→ [comp/21](21-inverted-index-anatomy.md)），段生命周期一律不复述（→ [comp/22](22-segment-and-near-realtime.md)），扇出算术一律不复述（→ [comp/23](23-es-cluster-shards-replicas.md) §23.5）。它只回答一个问题：**为什么同一个条件写成 `query` 和写成 `filter` 是两种成本、为什么 `from+size` 在分布式下必然爆、为什么一次 `text` 字段聚合能把整个集群打挂**。
> **读法**：§24.2 的唯一真问题是全篇支点（一句话里同时含「两条执行路径」与「深分页成本 = 分片数 × from」）；不想读推导就跳 §24.8（四笔账，全部带假设清单）与 §24.12（一页速记）；线上正在出事直接查 §24.10（坑清单）与 §24.11（观测）。§24.4 只写到「BM25 三个因子各控制什么」，不推公式——那是相关性工程的书，不是这一篇。

---

## 24.1 引子：三起「加了个条件就慢十倍 / 翻到第 500 页就 OOM」的故障

查询执行层的故障有个共同特点：**它们全是「请求还在成功返回」的故障**。没有 5xx，没有错误日志，只有一条越来越慢的曲线，或者一次毫无预兆的节点掉线。三起事故的时间线里，报警全部出现在「已经不可用」之后。

### 事故一：列表页加上「按销量加权」后，P99 从 40 ms 到 700 ms（2026-01-12）

某电商 6 节点（32C/31 GB 堆 16 GB）、1.0 亿 SKU、5 主分片 × 1 副本。商品列表接口一直是「`match(title)` + 默认 `_score` 排序」，P99 40 ms，稳了两年。

运营的诉求是「搜索排序要带销量权重」。开发的做法是在查询里塞 `function_score` + `script`，脚本从 `_source` 里取 `sales_30d` 算加权分。为什么用 `script` 而不是直接 `field_value_factor`？因为这个字段的 mapping 里被人手工写过 `"doc_values": false`（当年为了「省盘」，见 [comp/21](21-inverted-index-anatomy.md) §21.5），`field_value_factor` 直接在报错里被拒，开发顺手用脚本绕过——**报错是护栏，绕过去就是把护栏拆了。**

| 时间 | 动作 / 事件 | 面板上看到什么 |
| --- | --- | --- |
| 01-12 10:00 | 灰度 10% 流量，脚本排序上线 | P99 40 → 110 ms，CPU +9%，「在可接受范围」 |
| 01-12 14:00 | 全量放开 | P99 40 → 620 ms，`thread_pool.search.queue` 从 0 涨到 400 |
| 01-12 14:20 | 加机器无效（分片数没变，固定开销不摊薄） | QPS 上不去，`rejected` 开始出现 |
| 01-12 15:00 | `profile: true` 单条查询，看到 `FunctionScoreQuery` 占 query phase 耗时的 **96%** | 分片侧 `build_scorer` 耗时从 3 ms 涨到 190 ms |
| 01-12 15:40 | 抓一次 `_source` 解压次数：等于候选文档数（每次命中都要解压一遍 JSON） | 4,000 万次 JSON 解压/秒级 |
| 01-12 16:20 | 回滚脚本排序 | P99 回到 45 ms |
| 01-13 ~ 01-15 | 改 mapping：`sales_30d` 数值字段 + 默认 `doc_values: true`，排序改 `sort`/`field_value_factor`，重建索引 | — |
| 01-16 | 灰度上线，P99 48 ms，且**不再算分**（纯 `sort` 时 Lucene 跳过 BM25） | — |
| 复盘 | 列表页超时 6 小时，转化率下降 11%，损失约 210 万元；根因不是「脚本慢」，是「取值路径从列存掉到了 `_source` 解压」 | — |

三种取值路径的对照分析（这才是复盘真正该留下的东西）：

| 取值方式 | 物理路径 | 单位成本 | 4,000 万次候选的总成本 |
| --- | --- | --- | --- |
| `script` 读 `_source` | 存储字段解压 JSON → 取 `sales_30d`（每条都是完整文档解压） | 2–5 µs/doc | 80–200 s 核时 ⇒ 打满搜索线程池 |
| `sort` on `doc_values` | 列存（`.dvd/.dvm`）按 docID 顺序块解码 + long 比较，mmap 堆外 | 数十 ns/doc | 1–2 s 核时 |
| `field_value_factor` on `doc_values` | 同上，但只读一个块、结果转 double | 数十 ns/doc | 1–2 s 核时 |

**同一个「按销量加权」，两种写法差 100 倍**：一个是「每个候选解压一遍文档」，一个是「按 docID 顺序读一列 8 字节整数」。这就是 §24.7 那一节的结论提前出现：**排序与聚合必须落在 `doc_values` 上，落不到就是在拿 CPU 和堆去换。**

### 事故二：导出脚本翻到第 4,000 页，协调节点 OOM 被踢出集群（2025-09-27）

财务要一份「全部在售 SKU 的价格流水」。开发写了个脚本：`from = page × 100`，`size = 100`，循环翻页，8 个并发。为了能翻到底，有人把 `index.max_result_window` 从默认的 10,000 改成 1,000,000。

| 时间 | 事件 | 协调节点堆 | 集群状态 |
| --- | --- | --- | --- |
| 09-27 02:00 | 导出任务启动，前 100 页（`from` < 10,000）正常 | 52% | 绿 |
| 09-27 02:20 | 翻到第 1,000 页（`from=100,000`） | 68% | 绿 |
| 09-27 02:41 | 翻到第 4,000 页（`from=400,000`），单请求在协调节点堆上就是 6 × 400,100 × 200 B ≈ 480 MB | 84%，GC 单次 1.8 s | 绿（没人看堆） |
| 09-27 02:49 | 8 个并发全部落在深页区间 | 94%，GC 开销 42% | 黄 |
| 09-27 02:52 | 协调节点 `follower_check` 超时（默认 10 s 量级），master 判定它离开 | 99% | 节点被踢 |
| 09-27 02:53 | 6 个主分片的拷贝重分配，恢复带宽打满，剩余节点承接全部查询 | — | 红 4 分钟 / 黄 38 分钟 |
| 09-27 03:40 | 导出脚本 kill，`max_result_window` 改回 10,000 | 41% | 绿 |
| 复盘 | 一份报表把搜索集群打到黄 38 分钟。而这份报表根本不需要「排序后的第 40 万条」——它只需要一个有序游标 | — | — |

这条事故的账可以一行算完：**深分页的成本 = 分片数 × (from + size)**。`from` 从 0 走到 400,000，每个分片都要吐出 400,100 条候选，6 个分片就是 240 万条，全部进协调节点的优先队列——**你以为你在取第 400,000 条，其实你在让全集群算完前 400,000 条**。

### 事故三：拿 `text` 字段做 terms 聚合，一次统计打挂 3 个数据节点（2025-12-03）

运营要「描述里出现最多的 50 个词」。索引 3 亿文档、9 分片、3 个数据节点（每节点 3 分片，堆 16 GB）。`product_desc` 是 `text`（`ik_max_word`），没有 `keyword` 子字段。

开发直接写 `"terms": {"field": "product_desc"}`，报错 `Fielddata is disabled on text fields by default`。搜到答案说要开 `fielddata: true`，改了 mapping —— **注意：改 mapping 只是改元数据，但这条聚合第一次跑起来时才真正把倒排反转进堆**。

| 时刻 | 节点堆（单节点） | 现象 |
| --- | --- | --- |
| 12-03 11:00 | 8.1 GB（50%） | 正常 |
| 12-03 11:04 | 14.2 GB（89%），fielddata 单字段 9 GB | 聚合请求仍在跑，GC 每秒 3 次 |
| 12-03 11:06 | 15.8 GB（99%），`Data too large` 断路器开始拒绝请求 | 全部查询 429/500 |
| 12-03 11:07 | 3 个数据节点接连 OOM，进程退出 | 9 个主分片全部失联，集群红 |
| 12-03 11:31 | 节点重启、分片恢复，堆回到 8 GB | 绿，但那次查询失败的报错被当成「偶发」 |
| 复盘 | 11 分钟全站搜索不可用。而正确的写法是加一个 `product_desc.keyword`（`ignore_above: 256`）走 `doc_values`，堆增量为 0；更好的写法是这个词频统计放离线（→ comp/25） | — |

三起事故分别对应本篇三大节：**取值路径与算分（§24.3、§24.7）**、**深分页（§24.5、§24.6）**、**聚合的堆（§24.7、§24.8）**。

---

## 24.2 一句话定位与唯一真问题

**定位**：ES 的查询执行层，是把一段 DSL 翻译成「**在每段的倒排 / 列存上做一趟受限遍历，产出一个受 `size+from` 约束的优先队列**」的全部机制。它不负责「多快能搜到」（那是段的可见性，→ comp/22），也不负责「要碰几个分片」（那是集群形态，→ comp/23）；它负责的是**分片内部那 200 微秒到 200 毫秒**：算不算分、命中集合用位图还是 posting 流、堆开多大、哪些条件能被缓存。

### 唯一真问题

> **算分与筛选是两条执行路径——90% 的条件该进 `filter`，因为 filter 不算分、能缓存、能短路；而 `from+size` 在分布式下必然爆，因为每个分片都要吐出 `from+size` 条候选再由协调节点归并，深分页的成本是「分片数 × from」。**

这一句话拆成五步讲完，本篇就结束了：

| 拆解 | 机制 | 你必须付的账 |
| --- | --- | --- |
| ① 两条路径 | `query` 上下文建 scorer 跑 BM25，产 `(docID, score)`；`filter` 上下文只回答「是不是」，产 bitset | 同一条件走错路径，CPU 差 5–20 倍（§24.8 账二） |
| ② filter 结果可缓存 | 位图按「段 × 查询形状」进 node-level query cache（7.x 语义，§24.3） | 滚动时间窗（`now-1d`）把 key 天天换掉 ⇒ 缓存永不命中 |
| ③ 不算分就能剪枝 | 只有不要求分数的 collector 才能「先比排序键、分数根本不算」 | `track_scores: true` 一开，BM25 又回来了 |
| ④ top-k 的尺寸是 `size+from` | 每分片的优先队列装 `size+from` 条，协调节点归并 `N × (size+from)` 条 | `from` 每加一个 0，内存线性、CPU 超线性 |
| ⑤ 深分页必须换姿势 | `search_after`（游标）/ PIT（固定视图），把「偏移量」换成「上一页最后一条的值」 | 代价是不能跳页，见 §24.6 的表 |

### 它明确不承诺什么

| 你以为它承诺了 | 实际不承诺 | 反例 |
| --- | --- | --- |
| 「`filter` 缓存让你第二次瞬间返回」 | 只缓存**位图**，不缓存结果与排序；每次请求仍要重新 top-k | `size=20` 的请求命中缓存，仍需遍历命中位建堆，省掉的是 posting 解码而不是全部 |
| 「分数可以跨查询比较」 | 分数只在**同一次请求内**可比，且受分片本地 `df` 影响 | 加一个高频词后所有分数整体上移，`min_score` 阈值全部失真（§24.4） |
| 「`from+size` 能翻到任意页」 | 只承诺在 `index.max_result_window`（7.x 默认 10,000）以内 | 调到 1,000,000 只是把护栏拆了，账还在（事故二） |
| 「`scroll` 是分页 API」 | 7.x 起官方把它定位为**导出**；深分页推荐 PIT + `search_after` | 40 个在线用户各开一个 scroll，撞上 `search.max_open_scroll_context`（默认 500/节点） |
| 「聚合只是统计，比查询便宜」 | 聚合靠 `doc_values`；`text` 字段聚合要靠 `fielddata` 反转进堆 | 一次 terms 聚合吃掉 9 GB 堆（事故三） |

### 解剖卡八问：本篇答哪几问

| 问 | 落在哪节 | 一句话答案 |
| --- | --- | --- |
| ① 承诺什么 | §24.2、§24.6 | 承诺「在 `max_result_window` 内给出正确的全局 top-k」，不承诺任意深度的分页 |
| ② 数据长什么样 | §24.3、§24.7（结构见 → comp/21） | 两类中间产物：**posting 流**（算分用）与 **bitset**（筛选用）；排序聚合读 **doc_values 列存** |
| ③ 一次查询走哪条路 | §24.5 | rewrite → 建 Weight/Scorer → 每段出命中集 → 与 liveDocs 求交 → 优先队列 → 剪枝 → 归并 → fetch |
| ④ 并发与一致 | §24.4、§24.6 | 多分片 `df` 不一致 ⇒ 分数不可比；`scroll`/PIT 用一个固定时间点视图换一致性 |
| ⑤ 扩展维度 | §24.5、§24.9 | **分片**（扇出并行，吞吐除、延迟不一定加）+ **副本**（查询吞吐近似线性，见 → comp/23 §23.5）；执行层本身没有第三个旋钮 |
| ⑥ 会在哪崩（只写系统级） | §24.8、§24.11 | 协调节点堆被归并队列撑爆 → 心跳丢 → 节点被踢 → 分片重分配；数据节点 fielddata 打满 → OOM → 集群红；搜索线程池 `rejected` 打满 → 全站查询失败 |
| ⑦ 看哪几个指标 | §24.11 | `query_time_in_millis`、`query_cache` 有效率、`fielddata` 堆、`thread_pool.search.queue`、`heap.percent` |
| ⑧ 换掉它的信号 | §24.9 | 需要「任意深度分页 + 强一致对账 + 高频更新」三者同时成立时，ES 不再是正确选择 |

---

## 24.3 两条执行路径：算分与筛选

### 一条 DSL 在分片内部被劈成两半

`bool` 查询里，`must`/`should`/`must_not` 进 **query 上下文**，`filter`/`must_not` 进 **filter 上下文**（`must_not` 走 filter 路径且不计分）。这不是语法糖，是两条**完全不同的物理路径**：

```
                       一次 _search 请求（协调节点收到 DSL）
                                     │
              ┌──────────────────────┴───────────────────────┐
              │ rewrite：bool 展平 / match → term|phrase /    │
              │          prefix 展开成 MultiTermQuery         │
              ▼                                              ▼
   ┌────────────────────────────┐          ┌────────────────────────────────┐
   │ query 上下文（算分）        │          │ filter 上下文（不算分）         │
   │ · 每个候选跑一遍 BM25       │          │ · 只回答「是 / 不是」           │
   │ · 读 norms 1 B/doc 做长度归一│         │ · 结果 = bitset / RoaringBitmap │
   │ · 产 (docID, score) 流      │          │ · 可按段进 query cache 复用     │
   │ · 进 TopScoreDocCollector   │          │ · 可与 liveDocs 位图直接 AND    │
   └─────────────┬──────────────┘          └───────────────┬────────────────┘
                 │  命中集先被 bitset 收窄（AND ≈ 0.01 ns/doc）│
                 └───────────────────┬──────────────────────┘
                                     ▼
          候选集 → 大小 = size+from 的优先队列 → 剪枝 → 回 (docID, score)
```

三个物理差异值得单独记：

| 维度 | query 上下文 | filter 上下文 |
| --- | --- | --- |
| 产物 | `(docID, score)` 流，逐条流过 | **bitset**（每段一个，可缓存） |
| 单位成本 | posting 解码 0.5 ns + BM25 ≈ 5 ns/doc（norms 读 1 B） | 位图 AND ≈ 0.01 ns/doc；遍历命中位 ≈ 1 ns/doc |
| 能否缓存 | **不能**（分数依赖每次请求的 `df`） | **能**，按「段 × 查询形状」进 query cache |
| 能否跳过 | 不能，每个候选都要算 | 能：先算最便宜、选择性最高的条件，再逐步求交 |
| 典型误用 | 把 `status=online`、`price` 区间、`category` 放进 `must` | 把真正影响相关性的 `title` 词放进 `filter` ⇒ 变成纯布尔匹配，无排序 |

### 三条推论

**推论 ①：filter 的 bitset 可以被 node-level query cache 复用——但不是万能缓存。**

缓存的 key 是 **「段 + 查询形状」**，value 是这一段内命中的 docID 位图。所以：

- 段一旦 refresh 出新段、或 merge 重写，**旧 key 全部作废**（段不可变，`→ comp/22`）。写多读少的索引上，缓存命中率天然很低。
- 「查询形状」包含字面量：`range(price: {gte: now-1d})` 每次请求的时间戳不同 ⇒ key 不同 ⇒ **缓存永不命中**。这就是「加了时间窗的 filter 反而更慢」的全部原因。
- 默认**只缓存大段的位图**（小段缓存收益低、还会把缓存挤掉），由 `indices.queries.cache.all_segments`（静态，默认 `false`）控制是否对所有段缓存。
- 缓存是按节点堆的百分比划的：`indices.queries.cache.size`（静态，默认 `10%`）。

**版本差异必须写清，否则读者会以为这个指标一直是同一个东西**：

| 版本 | 参数名 | 语义 |
| --- | --- | --- |
| 1.x–2.x | `indices.cache.filter.size` / `indices.cache.filter.expire` | 最早的 filter 位图缓存，全局 LRU + TTL；「小段不缓存」的行为在这一代就出现过 |
| 5.x–6.x | `indices.queries.cache.size`（同名，默认 10%） | 更名为 query cache，仍只装 filter 上下文的位图；段级 key，段 refresh 即失效 |
| 7.x–8.x/9.x | `indices.queries.cache.size` + `indices.queries.cache.all_segments` | **node-level query cache**：按段与查询形状缓存，默认只对较大的段建缓存（具体门槛是源码常量，随小版本变过，以源码为准） |

另外还有一个常被混淆的东西：`index.requests.cache.enable`（**request cache**，默认 `false`），它缓存的是**整个请求的结果**，而且**只对 `size: 0` 的请求生效**（也就是只有聚合/计数能被缓存）。「filter cache 与 request cache 是两回事」这句话，在事故排查里至少能省半小时。

**推论 ②：filter 不算分，所以可以短路、可以按成本排序。**

filter 上下文的执行器是一个可重排的布尔树：Lucene 会先跑「最便宜且选择性最高」的子句（通常是 `term`/`range` 走位图），把候选集收窄，再跑贵的子句。一旦候选集被收窄到空，**剩下的子句一个都不用跑**。这是 query 上下文做不到的——query 上下文的每个候选都必须被完整算分，否则分数不完整。

这条推论直接给出工程纪律：**只要不影响相关性，就搬进 `filter`**。`status`、`is_deleted`、`tenant_id`、`price` 区间、时间范围、类目 ID——这些条件对「相关不相关」零贡献，但对「算不算」有决定性作用。

**推论 ③：`query` 里嵌套的 `bool.filter` 会自动走 filter 路径。**

```json
// A. query 上下文：算分、不可缓存、每个候选 ~5 ns
POST /sku/_search
{ "size": 20,
  "query": { "bool": {
    "must":   [ { "match": { "title": "降噪耳机" } } ],      // 唯一该算分的部分
    "should": [ { "match": { "brand": "索尼" } } ],          // boost 用
    "filter": [] } } }

// B. filter 上下文：不算分、可缓存、结果是 bitset；排序走 doc_values 不碰算分
POST /sku/_search
{ "size": 20,
  "query": { "bool": {
    "filter": [
      { "term":  { "status": "online" } },
      { "range": { "price": { "gte": 100, "lte": 1000 } } },
      { "term":  { "stock": true } } ] } },
  "sort": [ { "sales_30d": "desc" } ] }

// C. term 是词典全等查找、不分词：写错就是 0 命中（21 篇事故一）
{ "term": { "brand.keyword": "索尼" } }    // ✅ 命中
{ "term": { "brand": "索尼" } }            // ❌ 打在 text 字段上，词典里没有整串
```

---

## 24.4 BM25：三个因子各控制什么

不需要背公式，只需要知道**三个因子各自在行为上控制什么**，以及**它们什么时候会让「看起来一样的查询」给出不可比的分数**。

| 因子 | 在公式里管什么 | 行为上的推论 | 参数 |
| --- | --- | --- | --- |
| **TF 饱和** | 词在**这一篇**里出现几次（`tf`），但增长被分母压平 | 一个词从 1 次到 10 次，分数只涨一点点 ⇒ **关键词堆砌不涨分**（但不是零收益） | `k1`（Lucene `BM25Similarity` 默认 `1.2`）：越小饱和越快 |
| **IDF** | 这个词在**全库**有多罕见（`df` vs `N`） | 罕见词权重高：「钛合金」比「手机」值钱得多 ⇒ 停用词几乎不该参与算分 | 无参数，由 `df` 决定；这也是**跨分片分数不可比**的根源 |
| **长度归一** | 用 `dl / avgdl` 惩罚长字段（`norms` 里那 1 字节） | 短文档占便宜：同样命中一次，标题比详情页更相关 | `b`（默认 `0.75`）：`b=0` 关掉归一，长文档立刻刷榜 |

三张「行为 → 机制」的对应关系，评审时最常被追问：

**① 为什么「往查询里加一个高频词」会让分数整体偏移？**

因为 ES 7.0 起（Lucene 8）**移除了协调因子 `coord` 与查询归一化 `queryNorm`**，布尔查询的总分就是各子句分数的**加和**。加一个 `df` 很大的词（比如「手机」），它给每个命中文档加的分数不多但**不为零**，于是：

- 所有文档的分数整体上移 ⇒ 分数的**绝对值失去意义**；
- `min_score` 这类绝对阈值立刻失真（原来 3.0 是「强相关」，现在 3.2 可能只是「多命中了一个泛词」）；
- 但**排序往往几乎不变**，因为偏移对大多数文档近似相同 —— 于是这个坑极其隐蔽：**面板上「排序没坏」，但所有基于分数的业务逻辑（阈值过滤、加权融合、A/B 的分数门槛）都悄悄错了**。

**② 为什么跨分片打分需要 `dfs_query_then_fetch`？**

默认 `search_type: query_then_fetch`，每个分片**用自己那部分数据算 `N` 与 `df`**。1 亿文档分 5 片，一个词在全库 `df=500`，各分片本地 `df` 可能是 40 / 60 / 300 / 80 / 20 —— IDF 差 2~3 倍，**同一个文档在不同分片上拿到的分数不可比**，归并时就出现「排序抖动」：同一份数据，刷新一下换了个分片取样，名次变了。

`dfs_query_then_fetch` 的做法是**先做一轮只收集 `df` 的扇出**，拿到全局 `df` 再算分。代价是**多一次扇出**（固定开销 × 分片数，见 → comp/23 §23.5 的扇出算术），所以生产上通常不这么干，而是选另外三条路：分片数少一些、用 `_routing` 把同质文档收进同一分片、或者干脆接受近似（前提是排序的主键不是 `_score`）。

**③ 为什么「相关性最近变怪了」要先查 `norms`？**

`b` 的归一靠 `norms`（每 doc 1 字节）。关掉 `norms` 不等于「算不准」，而是**所有文档都按平均长度处理**：`dl ≡ avgdl`，长度因子恒为 1。于是堆满关键词的长文档（SEO 垃圾、重复描述、把整段详情塞进标题的商品）**系统性排在前面**。这个 bug 没有报错、没有指标，只有客诉。

> 版本基准：本篇按 Lucene 8.x–9.x / ES 7.x–8.x 写。`BM25Similarity` 的 `k1=1.2`、`b=0.75` 是默认值；`coord`/`queryNorm` 在 Lucene 8（即 ES 7.0）起已移除——**所以「旧博客里的分数公式」在 7.x 上照抄会得出错误结论**。

---

## 24.5 分片内执行：top-k 与 from 的代价

### 固定的六步

一个查询在**一个分片内**永远走这六步，顺序不可变：

1. **rewrite**：`bool` 展平、`match` 重写成 `term`/`phrase`、`prefix`/`wildcard` 展开成 `MultiTermQuery`（这一步决定「通配代价」，见 §24.10 坑五）。
2. **建 Weight / Scorer**：每个段独立建；filter 子句在这一步被编译成「位图生产者」。
3. **每段求命中集**：filter 侧先算出 bitset 并与 `liveDocs`（`.liv`，见 → comp/22 §22.5）求交，**已删文档在这一步被排除**。
4. **收集 top-k**：用一个**大小 = `size + from` 的最小堆**（优先队列）遍历候选。
5. **剪枝**：堆满之后，新候选比堆顶差就直接丢弃——**这就是「`from` 越大越慢」的第一个来源：堆越大，能剪掉的越少**。
6. **回传**：只回 `(docID, score)`，不回正文（正文在 fetch phase 再来拿，见 → comp/23 §23.5）。

```
分片 P0（2,000 万 doc，17 个段）
  ├─ 段 1 … 段 17：各算 bitset → 与 liveDocs 求交 → 合并成命中集
  ├─ 遍历命中 doc，维护一个大小 = size + from 的最小堆
  │     from=10,000, size=20 → 堆里 10,020 个 (score, docID)
  │     堆顶 = 当前第 10,020 名；新候选不如堆顶 → 立刻丢（剪枝）
  └─ 序列化 10,020 条 (docID, score) → 协调节点

协调节点
  ├─ 收 N × (size+from) 条候选 → 再维护一个 size+from 的堆 → 全局排序
  ├─ 取 [from, from+size) 这一段（真正的「第 500 页」）
  └─ fetch phase：按 (分片, docID) 回原分片取 _source，只取 size 条
```

### 为什么 `from` 越大越慢：三条独立的原因叠在一起

| 原因 | 机制 | 量级 |
| --- | --- | --- |
| **堆更大 → 剪枝更晚** | 堆要装 `size+from` 条，前 `from` 个候选全部无条件入堆，之后才开始有效剪枝 | 候选入堆次数从 O(size) 涨到 O(from) |
| **每次比较更贵** | 堆深 `log2(size+from)`：`size=20` → 4.3 次比较；`from=50,000` → 15.6 次。且大堆的工作集超出 L1/L2，**每次比较都可能是 cache miss** | 单次比较 1.5 ns → 4 ns |
| **协调节点要归并 `N × (from+size)` 条** | 每个分片都必须给出 `from+size` 条，才能保证「全局前 `from+size` 名一定在这 `N × (from+size)` 条里」——**少给一条就可能算错**，这不是实现偷懒，是数学要求 | 内存 = `N × (from+size) × 200 B`，见 §24.8 账一 |

第三条是最容易被忽略的：**「每个分片只返回 20 条不就行了吗」这个想法在所有分片数据都一样时成立，在数据分布不均时不成立**。分片 P3 可能包了整个爆款类目，全局前 20 名有 15 个在它那儿——所以每个分片都得交足 `from+size` 条。

### `sort` 时会发生什么

纯 `sort`（排序键里没有 `_score`）时，Lucene 用 `TopFieldCollector`，并且默认 `track_scores: false` ⇒ **根本不建 scorer、不跑 BM25**。这就是事故一里「改成 `sort` 之后 P99 反而比原来还低」的原因：省下来的不只是脚本，还有整条算分链。

一旦你要求「排序里带分数」（`sort: [{price: asc}, "_score"]` 或 `function_score`），就必须 `track_scores: true`，每个候选多跑一次 BM25。**「排序键」和「分数」在物理上是两条流，混用就要付两份钱。**

---

## 24.6 深分页：为什么必然爆，以及三条出路

### 内存随 `from` 线性增长

```
协调节点堆占用（1 亿 doc / 6 分片 / size=20 / 每候选 200 B）

 from=0        →  6 × 20        = 120 条       × 200 B =    24 KB
 from=1,000    →  6 × 1,020     = 6,120 条     × 200 B =   1.2 MB
 from=10,000   →  6 × 10,020    = 60,120 条    × 200 B =    12 MB   ← 默认 max_result_window 就在这
 from=50,000   →  6 × 50,020    = 300,120 条   × 200 B =    60 MB
 from=400,000  →  6 × 400,020   = 2,400,120 条 × 200 B =   480 MB   ← 单请求
 ×8 并发       →  3.8 GB；再加每个分片自己的 400,020 条本地堆（+3.8 GB）
               →  堆 16 GB 的机器：GC 开销 > 40% → 心跳丢 → 节点被踢（事故二）
```

**为什么必然爆**：`from+size` 是一个**偏移量**，偏移量的语义是「按全局排序丢掉前 `from` 条」。在分布式归并里，「全局排序」只在协调节点存在，所以每个分片必须**把它的局部前 `from+size` 名都交上来**。偏移量越大，需要被传送、被归并、被持有在堆里的候选就越多——**成本正比于分片数 × `from`，而不是正比于你真正要的 20 条。**

### 三条出路与它们的代价

```json
// ① 打开 PIT（7.10+）：固定一个时间点的视图，keep_alive 内有效
POST /sku/_pit?keep_alive=1m
→ { "id": "48-my_pit_id..." }

// ② 首页：不带 search_after；取 size 条即可，多取一条只为判断「还有没有下一页」
GET /_search
{ "size": 20,
  "pit": { "id": "48-my_pit_id...", "keep_alive": "1m" },
  "sort": [ { "sales_30d": "desc" }, { "_shard_doc": "asc" } ],
  "track_total_hits": false }

// ③ 后续页：把上一页最后一条的 sort 值原样带回；from 永远不出现
GET /_search
{ "size": 20,
  "pit": { "id": "48-my_pit_id...", "keep_alive": "1m" },
  "search_after": [ 8421, 30017 ],
  "sort": [ { "sales_30d": "desc" }, { "_shard_doc": "asc" } ] }
// 排序键必须唯一且稳定：只按 _score 排 → 同分文档顺序不保证 → 会漏也会重
// _shard_doc 是 PIT 内的零开销 tiebreaker（7.12 起可用），不必自己造唯一键
```

| 方式 | 能跳页吗 | 一致性 | 资源占用 | 什么时候用 |
| --- | --- | --- | --- | --- |
| `from + size` | **能**（任意页） | 每次请求一个新视图，翻页之间数据变化会重复/漏 | 协调节点 `O(N×(from+size))`；`from+size > index.max_result_window`（7.x 默认 10,000）直接报错 | 前 10 页、页数可点选的列表页 |
| `scroll` | **不能**（只能向前，且必须遍历完） | 快照上下文：创建时刻的段视图，全程一致 | 每 scroll 一个 context + **持有段引用不让 merge 回收**；`search.max_open_scroll_context`（默认 500/节点），超了报错 | 离线导出、重建索引。7.x 起官方定位就是导出，不推荐做在线翻页 |
| `search_after` | **不能**（只能向后，无法回到第 3 页） | 无快照 ⇒ 翻页期间数据变化可能漏；配 PIT 才一致 | 常数内存，无 context | 无限滚动、导出（不要求快照） |
| `PIT + search_after` | **不能** | 固定 `keep_alive` 内的视图，多请求之间一致 | 一个 PIT context（比 scroll 轻，可跨索引复用给多个查询） | **7.10+ 官方推荐的深分页与导出姿势** |

三句话记住这张表：**`from+size` 买的是「能跳页」；`scroll` 买的是「一致快照」；`search_after` 买的是「常数内存」。三个都买不到「又跳页又便宜又一致」——那不存在的。**

---

## 24.7 排序与聚合：doc_values 与 fielddata 的生死线

### 三条取值路径

| 结构 | 方向 | 位置 | 谁在读 | 堆风险 |
| --- | --- | --- | --- | --- |
| 倒排 `.tim/.doc` | term → docID | mmap 堆外 | `term`/`match`/`filter` | 无（但 FST 进堆，见 → comp/21 §21.7） |
| **doc_values** `.dvd/.dvm` | docID → 值（列存） | **mmap 堆外** | `sort`、`aggs`、`script`、部分 filter | **无**（这是关键） |
| **fielddata** | term → docID（把倒排反转） | **堆内** | `text` 字段的 `sort`/`aggs`（唯一途径） | **极高，ES 最常见的 OOM 路径** |

`doc_values` 是 ES 的「正排」：按 docID 顺序把值列存下来，块压缩、mmap 打开，所以「对 1 亿文档按价格排序」**不会 OOM**——它只是顺序读一列 8 字节整数（事故一里那个 100 倍差距的来源）。

`fielddata` 是「为了给 `text` 字段硬开 sort/agg，把倒排整个反转进堆」：要带 `String` 对象、ord 映射、每 doc 的 ord 数组。**它不是一个可调参数，它是一个事故开关。**

### 聚合写法 × doc_values × 堆风险

| 聚合写法 | 走什么结构 | 用不用 doc_values | 堆风险 | 说明 |
| --- | --- | --- | --- | --- |
| `terms` on `keyword` | doc_values + ordinals | ✅ | 低 | 高基数（> 100 万）时 **global ordinals 会进堆**（也计入 `fielddata` 指标），且桶数受 `search.max_buckets` 限制（7.0+ 默认 65,536） |
| `terms` on `text` | **fielddata** | ❌ 反转进堆 | **极高** | 绝对禁止；一次查询就能吃掉 9 GB（事故三） |
| `terms` on `text.keyword` | doc_values | ✅ | 低 | `ignore_above` 超长的值不索引，会静默丢失（→ comp/21 事故二） |
| `cardinality` | doc_values + HLL 草图 | ✅ | 低 | `precision_threshold` 以内精确、以上估算 ⇒ **不能用来对账** |
| `date_histogram` / `range` | doc_values | ✅ | 低 | 桶数同样受 `search.max_buckets` 约束 |
| `scripted_metric` | 每 doc 跑脚本 + 中间态在堆 | 部分 ✅ | 中高 | 中间态随基数增长，生产禁用 |
| `nested` + `reverse_nested` | join + 子文档 doc_values | ✅ | 中 | **成本 × 子文档数**：一个父 doc 挂 50 个子 doc，聚合就是 50 倍工作量 |
| `composite` | doc_values，可分页 | ✅ | 低 | 大数据量聚合的唯一安全姿势：分页取桶，不用一次建全量哈希表 |

同一件事的三种写法，用的是三种完全不同的物理路径：

```json
// A. 聚合走 doc_values（keyword / 数值）：堆增量 ≈ 0，块解码后直接进桶
POST /sku/_search
{ "size": 0, "track_total_hits": false,
  "aggs": { "by_brand": { "terms": { "field": "brand.keyword", "size": 50 } } } }

// B. 直接对 text 字段 terms：默认报错；按报错提示开 fielddata 就是事故三
POST /sku/_search
{ "size": 0, "aggs": { "by_words": { "terms": { "field": "product_desc" } } } }
// → "Fielddata is disabled on text fields by default. Set fielddata=true on [product_desc] ..."

// C. profile：把成本花在哪一跳看清楚（profile 本身让查询慢 2~10 倍，只对样本开）
POST /sku/_search
{ "profile": true, "size": 0,
  "query": { "bool": { "filter": [ { "term": { "status": "online" } } ] } } }
// 只看三段：rewrite_time / create_weight_time / build_scorer（含 count 与 time_in_nanos）
// filter 命中 query cache 时 build_scorer 会塌到接近 0 —— 这是判断「缓存到底有没有生效」最快的办法
```

### `nested` 与 `parent-child` 的查询放大

两者都不是「多存一个字段」，而是**改变了文档数的物理含义**：

- **`nested`**：一个父 doc 内部的每个 nested 对象都是**独立的隐藏 Lucene doc**。`nested` 查询走 `ToParentBlockJoinQuery`，必须先把子文档命中集算出来再「向上跳」到父文档；聚合则要 `nested` 进去、`reverse_nested` 出来。所以一个「1 万父 doc × 20 子对象」的索引，物理上是 21 万 doc，**扇出、段遍历、top-k 的成本全部按 21 万算**。
- **`parent-child`**：父子是**不同段的真文档**，join 靠堆里的 global ordinals（`join` 字段的父子映射必须整体加载），`has_child`/`has_parent` 的代价比 `nested` 更高，且**父文档更新会连带影响 join 结构**。能用 `nested` 就别用 `parent-child`，能把关系摊平成宽表就别用两者。

---

## 24.8 三本账：深分页、filter 与聚合的堆

四个算例，每个都标「量级结论 / 承诺值」与假设清单。**假设可以换，推导式不能换。**

### 账一：深分页——1 亿文档 / 5 分片 / `from=50,000`

假设清单：① 1.0 亿 doc，5 主分片，每分片 2,000 万；② filter 命中率 20% ⇒ 每分片 400 万候选参与竞争；③ 每个候选在优先队列里 200 B（docID 8 B + score 8 B + 对象头 + 分片位）；④ 小堆每次比较 1.5 ns、深堆 4 ns（工作集超出 L1/L2 后随机访问）；⑤ 5 分片并行，墙钟取单分片最大值 + 归并。

```
每分片本地队列   = (50,000 + 20) × 200 B            ≈ 10 MB       × 5 = 50 MB
协调节点归并队列 = 5 × 50,020 × 200 B               ≈ 50 MB
单请求峰值堆     ≈ 100 MB 量级

CPU（每分片）    = 400 万候选 × log2(50,020)=15.6 次 × 4 ns
                 = 4e6 × 15.6 × 4 ns                ≈ 250 ms
对照组 from=0    = 400 万 × log2(20)=4.32 × 1.5 ns  ≈  26 ms     ← 差约 10 倍
归并             = 250,100 条 × ~100 ns              ≈  25 ms

P99 量级：40 ms → 300 ms 起步；from 再翻 8 倍（400,000）→ 单分片 1.7 s
```

**量级结论**（不是承诺值）：内存随 `from` **线性**、CPU 随 `from × log(from)` **超线性**；`from=50,000` 时单请求堆在 100 MB 量级，20 个并发就是 2 GB，堆 16 GB 的协调节点只剩 40% 余量给段元数据、聚合和 GC 周转。

### 账二：filter vs query——同一条件的 CPU 差多少

假设清单：① 条件 `status = online`，全库 `df = 500 万`（密度 5%）；② 1 亿 doc / 5 分片 ⇒ 每分片 df = 100 万、2000 万 doc；③ posting 解码 0.5 ns/posting、BM25 ≈ 5 ns/doc（含 1 B norms 读）、位图 AND ≈ 0.5 ns/64 doc、遍历命中位 ≈ 1 ns/doc（均取自 → comp/21 §21.4 的量级）。

| 路径 | 拆解 | 每分片 CPU | 相对 |
| --- | --- | --- | --- |
| query（算分，首次） | 100 万 posting × 0.5 ns = 0.5 ms；100 万 × 5 ns = 5 ms | ≈ 5.5 ms | 1× |
| filter（首次，建位图） | 2000 万 bit = 2.5 MB；AND 2000 万/64 = 31.25 万次字操作 × 0.5 ns = 0.16 ms；遍历命中 100 万 × 1 ns = 1 ms | ≈ 1.2 ms | ≈ 4.6× 便宜 |
| filter（缓存命中） | 只剩「遍历命中位」1 ms，位图查找 µs 级 | ≈ 1.0 ms | ≈ 5.5× 便宜 |
| query（高密度词，df=3000 万） | 3000 万 × 5 ns = 150 ms | ≈ 150 ms | 与 filter 差 **两个数量级** |

**量级结论**：同一条件走 query 与走 filter，CPU 差 **5–20 倍**（中低密度）/ **100 倍以上**（高密度泛词）；filter 命中缓存后成本趋近常数。**不要把这个倍数当承诺值引用**——它随密度、段数、field 类型（数值 doc_values 走的是另一条更快路径）在 3–200 倍之间浮动，能背的是那条推导链，不是倍数。

### 账三：`text` 字段 terms 聚合——fielddata 吃多少堆

假设清单：① 3 亿 doc，9 分片，3 节点（每节点 3 分片），堆 16 GB；② `product_desc` 是 `text`，平均**每 doc 20 个 term**；③ 全局唯一 term 5,000 万 ⇒ 每分片约 556 万；④ fielddata 每个 `(doc, term)` 记一个 4 B ord；⑤ 词典项按每 term 60 B（字符串 + 对象头 + ord 映射，量级估算）。

```
每分片 doc 数      = 3e8 / 9                    ≈ 3,333 万
每分片 ord 数组    = 3,333 万 × 20 × 4 B        ≈ 2.67 GB
每分片 term 词典   = 556 万 × 60 B              ≈ 0.33 GB
每分片 fielddata   ≈ 3.0 GB
每节点（3 分片）   ≈ 9.0 GB
再叠加：段元数据 ~1.5 GB + query cache 1.6 GB（10% × 16 GB）+ 聚合桶
→ 11 GB+ / 16 GB ⇒ GC 开销失控 ⇒ OOM（事故三）
```

**量级结论**：`text` 字段做 terms 聚合，堆占用 ≈ **`每分片 doc 数 × 每 doc term 数 × 4 B`**，这个量级直接正比于数据量 × 文本长度，**没有任何参数能让它变安全**。换成 `keyword` 子字段走 `doc_values`，堆增量 ≈ 0；换 `composite` 分页聚合，堆 ≈ 桶数 × 几十字节。

### 账四：分片内 top-k——`size=1000` 与 `size=10`

假设清单：① 5 分片，每分片 400 万候选；② 每条候选 200 B；③ 每候选比较 1.5 ns。

| | `size=10` | `size=1000` | 倍数 |
| --- | --- | --- | --- |
| 每分片堆 | 10 × 200 B = 2 KB | 1,000 × 200 B = 200 KB | 100× |
| 协调节点归并条数 | 5 × 10 = 50 | 5 × 1,000 = **5,000** | 100× |
| 协调节点队列内存 | 10 KB | 1 MB | 100× |
| 每候选比较次数 | log2(10) = 3.32 | log2(1000) = 9.97 | 3× |
| 每分片 CPU | 400 万 × 3.32 × 1.5 ns ≈ 20 ms | 400 万 × 9.97 × 1.5 ns ≈ 60 ms | 3× |
| fetch 网络 | 10 条 × 1 KB = 10 KB | 5,000 条 × 1 KB = **5 MB**（其中 4 MB 被客户端丢掉） | 500× |

**量级结论**：**「一次取 1,000 条」在 5 分片上等于取 5,000 条**，其中 4,000 条（80%）被计算、被传输、被丢弃。这就是「前端只显示 20 条，但接口 `size` 写了 1,000 好方便本地筛选」这个习惯的真实价格；治法是**服务端筛选 + 小 `size` 多取，或直接用 `search_after` 游标**。

### 复算脚本（改假设即可跑）

```python
# 深分页的堆与 CPU 量级：改假设即可复算（对应账一）
import math
DOCS, SHARDS, HIT_RATE, ENTRY = 1e8, 5, 0.20, 200   # 总量 / 分片 / 命中率 / 每候选字节
NS_SHALLOW, NS_DEEP = 1.5, 4.0                       # 小堆 cache 友好 / 大堆随机访问

def cost(frm, size=20, conc=1):
    cand  = (DOCS / SHARDS) * HIT_RATE               # 每分片参与竞争的候选数
    depth = frm + size                               # 本地堆尺寸
    ns    = NS_SHALLOW if depth < 1000 else NS_DEEP
    cpu_ms = cand * math.log2(max(depth, 2)) * ns / 1e6
    shard_q = depth * ENTRY
    coord_q = SHARDS * depth * ENTRY
    return cpu_ms, shard_q * SHARDS, coord_q, (shard_q * SHARDS + coord_q) * conc

for frm in (0, 1000, 10000, 50000, 400000):
    cpu, sq, cq, tot = cost(frm, conc=8)
    print(f"from={frm:>7} 单分片CPU≈{cpu:7.1f}ms 分片堆合计={sq/1e6:7.1f}MB "
          f"协调队列={cq/1e6:7.1f}MB 8并发总堆={tot/1e9:5.2f}GB")

# 参考输出（量级结论）：
# from=      0 单分片CPU≈   25.9ms 分片堆合计=   0.0MB 协调队列=   0.0MB 8并发总堆=0.00GB
# from=  50000 单分片CPU≈  249.8ms 分片堆合计=  50.0MB 协调队列=  50.0MB 8并发总堆=0.80GB
# from= 400000 单分片CPU≈ 1343.5ms 分片堆合计= 400.0MB 协调队列= 400.0MB 8并发总堆=6.40GB
```

> 脚本里的 1.5/4.0 ns、200 B 都是**量级假设**，不是实测承诺值。换一台 CPU、换一代 JVM、把候选结构从对象改成数组，都会让系数动 2–3 倍；但「内存 ∝ `from`、CPU ∝ `from·log(from)`」这两条形状不会变。

---

## 24.9 架构边界：ES 查询能做什么、不能做什么

| 查询需求 | 能做吗 | 代价 / 复杂度 | 靠什么绕过 |
| --- | --- | --- | --- |
| 相关度排序（BM25） | ✅ 主业 | 每候选 ~5 ns | 重排、学习排序放离线或单独服务 |
| 精确过滤 + 缓存 | ✅ | 位图 AND ≈ 0.01 ns/doc，命中缓存后趋近 0 | 把定性条件全搬进 `filter` |
| 排序（数值/日期/keyword） | ✅ | doc_values 顺序读，数十 ns/doc | 别用脚本读 `_source` |
| 聚合（group by / 指标） | ✅ | doc_values + 桶哈希 | 高基数用 `composite`；深层统计去 ClickHouse（→ comp/20） |
| **任意深度分页** | ⚠️ 有限 | `from` 上限 `index.max_result_window`（7.x 默认 10,000）；解法是游标 | `search_after` / PIT |
| **一致快照的导出** | ⚠️ 需显式 | `scroll` 或 PIT，占 context、挡 merge | 导出走独立索引/只读副本，别压在线集群 |
| **一次遍历全量数据** | ❌ | 无论怎么写都要扫全部命中文档，成本 O(N) | 数据湖 / ClickHouse（→ comp/20、comp/25） |
| **对账用的精确去重计数** | ❌ | `cardinality` 是 HLL 估算 | 回权威源 COUNT DISTINCT |
| **`text` 字段分组统计** | ❌（除非 fielddata） | 堆 = doc 数 × term 数 × 4 B | 加 `keyword` 子字段；或离线算 |
| **跨分片可比的绝对分数** | ❌ | 各分片本地 `df` 不同 | `dfs_query_then_fetch`（多一轮扇出）或减少分片 |
| **子串匹配 `*phone*`** | ❌ | 前置通配要展开词典里的全部 term | `ngram` 分词（体积 ×5–20）或向量召回（→ comp/26） |
| 事务 / 唯一约束 / 点查主键 | ❌ | — | 回关系库（0.3 ms 点查） |

一句话总结这张表的形状：**ES 的查询层是「有界遍历 + 受控 top-k」——凡是你需要的答案能表达成「在某个候选集内排前 K 名 / 分组计数」，它就很强；一旦你的需求是「任意偏移」「全量遍历」「精确对账」，它的物理结构就不支持，只能换姿势或换零件。**

---

## 24.10 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| # | 现象 | 根因（指到物理路径） | 怎么定位 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- | --- |
| 1 | 列表页翻到第 500 页超时 / 协调节点 OOM | `from+size` 让每分片吐 `from+size` 条 + 协调节点归并 `N×(from+size)`（§24.5） | `profile` 看 `from`；抓协调节点堆与 `search.queue`；算 `N×(from+size)×200 B` | 改 `search_after` + 唯一排序键；或 PIT + `search_after` | 把 `index.max_result_window` **保持默认 10,000**，谁要调谁走评审；接口层禁止「页码 → from」直传 |
| 2 | 一次 terms 聚合把数据节点打挂 | `text` 字段没 doc_values，开 `fielddata` 后把倒排反转进堆（§24.7） | `_nodes/stats/indices/fielddata` 按字段名看明细；堆曲线与聚合请求时间对齐 | 加 `keyword` 子字段走 doc_values；或改 `composite` 分页；或挪离线 | `fielddata: true` 列入 mapping 评审红线；对 `fielddata.memory_size` 设 `> 200 MB` 告警 |
| 3 | `term` 查某个值永远 0 命中 | `term` 不分词，拿整串去 text 字段的词典做全等查找（→ comp/21 事故一） | `_analyze` 看写入切出的 term；`_termvectors` 看该 doc 实际索引了什么 | 查询改 `match`/`match_phrase`，或改用 `keyword`/`.keyword` 子字段 | 查询侧禁用 `term` 打 `text` 字段（可做 DSL 静态检查）；字段命名约定 `xxx.keyword` |
| 4 | 同一份数据刷新后排名抖、A/B 结果对不上 | 各分片用本地 `N`/`df` 算 IDF ⇒ 分数跨分片不可比（§24.4） | 同一查询打 `?preference=` 固定分片对比；`explain` 看两侧 IDF | `dfs_query_then_fetch`（多一轮扇出）；或减少分片；或让排序主键不是 `_score` | 相关性回归用**固定分片的离线集**；A/B 只看指标差异，不看分数绝对值 |
| 5 | 联想词接口 P99 破秒，CPU 打满 | `wildcard` 前置通配（`*phone*`）要展开词典里全部 term 再逐个查 df（→ comp/21 §21.8） | `profile` 看 rewrite 阶段产出的 term 数（可达数万）；慢日志里 `wildcard`/`regexp` 占比 | 改 `prefix`（可走 FST 前缀路径）/ `ngram` 字段 / 专门的前缀索引 | 禁止前置通配上线；`prefix` 长度设下限（1–2 字的 term 数爆炸） |
| 6 | 用户越多越慢，最后 `Trying to create too many scroll contexts` | `scroll` 上下文持有段引用不让 merge，占堆与句柄（§24.6） | `_nodes/stats/indices/search` 的 `open_contexts`；`_tasks` 里的 scroll 任务 | 在线翻页改 `search_after`；导出用 PIT 且设短 `keep_alive` | 在线接口**不许用 scroll**；对 `open_contexts` 设上限告警；导出走独立集群/副本 |
| 7 | 报表页「聚合 + 翻页」同时跑到 90% 堆 | 深分页的归并队列 + 聚合的桶哈希/fielddata 同时占堆，两份预算叠在一起 | 同时看 `search.queue` 与聚合耗时；单请求 `profile` 的 `took` 拆分 | 拆成两个请求（聚合 `size:0`，明细走 `search_after`） | 报表类接口限流 + 独立协调节点；`search.max_buckets`（7.0+ 默认 65,536）别乱调高 |
| 8 | 加了 `sort` 之后排序对了，但「加权打散」完全没生效 | 纯 `sort` 时 `track_scores` 默认 `false`，BM25 压根没跑；分数是 0 或缺失 | `explain` 或返回体里看 `_score` 是否为 `null`；`profile` 里有没有 `BM25Similarity` | 显式 `"track_scores": true`，或用 `function_score` | 任何「排序里含分数」的查询，DSL 模板里强制带 `track_scores` |

---

## 24.11 观测：指标、拐点与告警

每条都标了版本与开关前提——**指标名写错一个，读者就会认为全篇没验证过**。

| 指标 / 手段 | 含义 | 拐点 | 版本与开关前提 |
| --- | --- | --- | --- |
| `profile: true` 的逐阶段耗时 | rewrite / build_scorer / `next_doc` / `collect` 各占多少 | query phase 里 `build_scorer` 或 `collect` 单段 > 50 ms | 所有版本可用；**profile 本身让查询慢 2–10 倍**，只能对单条样本开，不能全量开 |
| `_nodes/stats/indices/search` → `query_total` / `query_time_in_millis` | 节点级累计查询数与累计 query phase 耗时 | `query_time_in_millis / query_total` > 60 ms | 全版本；**是累计计数器**，要取 `rate` 或自己做滑动窗口；只含 query phase，不含 fetch |
| `indices.query_cache.memory_size_in_bytes` / `eviction_count` / `hit_count` | node-level query cache 的占用、驱逐、命中 | 驱逐率 > 100/min，或占用 > 堆 × 15% | **7.x 起语义为 node-level、按段 + 查询形状**；6.x 及以前同名参数对应的是 filter cache，**指标含义与粒度都不同，跨版本比对会误判** |
| `indices.fielddata.memory_size_in_bytes` | **堆风险第一指标** | 按字段看：任一 `text` 字段 > 200 MB 即事故 | **注意**：`keyword` 字段的 global ordinals 也计入这里，所以「> 0」不等于有人开了 text fielddata；必须按字段名看 `fielddata` 明细 |
| `thread_pool.search.queue` / `rejected` | 搜索线程池排队与拒绝 | queue > 1,000 或 `rejected` 增量 > 0 | 全版本；协调节点先排队，数据节点后排队——**先看协调节点** |
| `open_contexts`（scroll） | 打开的 scroll 上下文数 | > `search.max_open_scroll_context` 的 50%（默认 500/节点） | 全版本；7.x 起官方建议深分页用 PIT，scroll 指标仍要看（存量代码不会自动改） |
| `_cat/nodes?v&h=heap.*` | 各节点堆使用率 | `heap.percent` > 75% 持续 10 min | 全版本；`heap.max` 与 `heap.current` 一起看，别只看 percent |

```yaml
# elasticsearch_exporter 指标名 → Prometheus 告警骨架（本篇专属四条）
groups:
- name: es-query-execution
  rules:
  # ① 深分页/归并队列把协调节点堆吃掉（事故二的第一现场）
  - alert: EsSearchQueueBacklog
    expr: elasticsearch_thread_pool_queue_count{type="search"} > 1000
    for: 5m
    annotations: { summary: "搜索队列 >1000：查 from 是否被调大 / 是否有 from+size 翻页" }
  # ② fielddata 常驻 = 必炸（事故三的第一现场）
  - alert: EsFielddataResident
    expr: elasticsearch_indices_fielddata_memory_size_bytes > 2e8
    for: 10m
    annotations: { summary: "fielddata >200MB：确认是 text 字段 fielddata 还是 keyword 的 global ordinals" }
  # ③ filter 缓存被冲刷 → 每个请求都重扫 postings
  - alert: EsQueryCacheThrashing
    expr: rate(elasticsearch_indices_query_cache_evictions_total[5m]) > 100
    for: 10m
    annotations: { summary: "query cache 频繁驱逐：查 filter 里是否带了 now-1d 这类每次变的时间窗" }
  # ④ 平均 query phase 变慢（先排除 fetch 与网络）
  - alert: EsQueryPhaseSlow
    expr: rate(elasticsearch_indices_search_query_time_millis_total[5m])
          / rate(elasticsearch_indices_search_query_total[5m]) > 60
    for: 10m
    annotations: { summary: "平均 query phase >60ms：用 profile 定位是 build_scorer 还是 collect" }
```

指标之间的因果链值得单独记一句：**`query_time` 先涨（shape 变了）→ `query_cache.evictions` 再涨（缓存装不下/被冲刷）→ `search.queue` 涨（线程被占住）→ `heap.percent` 与 `fielddata` 涨（深页或聚合在吃堆）→ `open_contexts` 或 `rejected` 爆（已经在拒绝服务）**。前两步有几个小时的窗口，第三步只剩十几分钟，第五步就是事故本身。

---

## 24.12 一页速记

| 问题 | 答案 |
| --- | --- |
| query 与 filter 的物理差别 | query 建 scorer 跑 BM25 产 `(docID, score)`；filter 只产 **bitset**，可缓存、可短路、可重排 |
| 什么该进 filter | 一切不影响相关性的条件：`status`、租户、价格区间、时间范围、类目、软删标记 |
| filter 缓存能缓存什么 | 只缓存**位图**，按「段 × 查询形状」；段 refresh/merge 即失效；不缓存结果与排序 |
| query cache 的版本坑 | 7.x 是 node-level query cache（`indices.queries.cache.size` 默认 10%，`all_segments` 默认 false）；6.x 及以前是 filter cache，语义不同 |
| request cache 是什么 | `index.requests.cache.enable`（默认 false），**只缓存 `size: 0` 的请求结果**，与 query cache 无关 |
| BM25 三个因子 | TF 饱和（`k1=1.2`）、IDF（由 `df` 决定）、长度归一（`b=0.75`，靠 `norms` 那 1 字节） |
| 加高频词为什么分数乱 | 7.0/Lucene 8 起无 `coord`/`queryNorm`，总分是加和 ⇒ 整体上移、`min_score` 失真、排序却几乎不变 |
| 分数为什么跨分片不可比 | 每分片用本地 `N`/`df` 算 IDF；解法 `dfs_query_then_fetch`（多一轮扇出）或减分片 |
| 深分页为什么必然爆 | 每分片必须交 `from+size` 条，协调节点归并 `N×(from+size)` 条 ⇒ 成本 = **分片数 × from** |
| 三种翻页方式 | `from+size`（能跳页、贵）/ `scroll`（快照、占 context、7.x 起只做导出）/ `search_after`+PIT（常数内存、不能跳页，7.10+ 官方推荐） |
| 排序聚合凭什么不炸堆 | `doc_values` 列存 + mmap 堆外；`text` 没 doc_values，开 `fielddata` 就 OOM |
| 一次取 1,000 条的真实价格 | 5 分片就是 5,000 条候选：3× 比较、100× 队列内存、5 MB 网络，其中 80% 被丢弃 |
| 一条纪律 | **定性条件全进 `filter` + `size` 保持小 + 深翻只用 `search_after` + `text` 永不聚合 + `max_result_window` 不调** |

---

## 24.13 自检题

**Q1**：`filter` 命中了 query cache，为什么第二次请求不是「立刻返回」，而是「只快了一部分」？那部分省掉的是什么？

<details>
<summary>看答案</summary>

缓存的粒度是**「段 + 查询形状 → 位图」**，缓存的只是「哪些 docID 满足这个条件」，**不是「这次请求的结果」**。所以命中缓存后仍然必须：

1. 把多个 filter 的位图求交（这一步很便宜，0.01 ns/doc）；
2. 与 `liveDocs` 求交排除已删文档；
3. **遍历命中位、维护 `size+from` 大小的优先队列**（这一步是省不掉的，1 ns/doc 量级）；
4. 排序键比较（走 doc_values）；
5. 协调节点归并、fetch phase 取 `_source`。

省掉的是第 2 步之前的**posting 解码 + BM25**（5 ns/doc 级）。所以要「整个结果」级别的缓存，得用 `index.requests.cache.enable`（request cache），而它**只对 `size: 0` 的请求生效**——也就是只帮聚合和计数，不帮列表页。**这就是为什么「加了缓存却还是慢」在 filter 场景很常见：命中缓存 ≠ 命中结果。**

</details>

**Q2**：`index.max_result_window` 默认 10,000。把它改成 1,000,000「只是放宽了一个限制」，为什么事故二里它等于拆了护栏？

<details>
<summary>看答案</summary>

这个参数不是「功能开关」，是**成本断路器**：它把 `from+size` 框在一个让协调节点归并队列保持在「几十 MB 以内」的范围内。默认 10,000 时，`N × (from+size) × 200 B` 在 5–6 分片下就是 10 MB 级；改到 1,000,000 后，同样的算式变成 1 GB 级/请求，只要有几个并发就能吃掉协调节点的堆。

而「深分页」的需求本质上是**「把偏移量当游标」**——偏移量的成本随分片数线性、随 `from` 线性、随并发线性，**三项都是乘的**。正确的解法是换抽象：`search_after` 把「第几页」换成「上一页最后一条的排序值」，成本变成常数，`from` 根本不出现。

判断标准很简单：**如果你需要调 `max_result_window`，说明你的访问模式已经不适合 `from+size`，该换的是 API 而不是参数。**

</details>

**Q3**：用 `search_after` 但排序只写 `"_score": "desc"`，为什么会漏数据、也会重复数据？

<details>
<summary>看答案</summary>

`search_after` 的语义是「返回排序键**严格排在 `search_after` 给定值之后**的文档」。这要求**排序键能唯一确定一个位置**。

`_score` 是浮点、可重复：一次查询里有 500 个文档分数都是 12.345。`search_after: [12.345]` 时，游标位置的判定是不确定的——同分的 500 个文档可能整批被跳过（漏），也可能整批被再返回一次（重）。而且分数本身不是稳定函数（分片 `df` 会随写入变化），下一页的分数分布可能已经变了。

所以纪律是：**排序键必须是「业务上稳定 + 组合上唯一」**，最常见的写法是 `[业务排序键, 唯一 ID]`；在 PIT 内可以直接用 `_shard_doc`（7.12 起）做零开销 tiebreaker，它保证在一次 PIT 视图内全局有序且唯一。

</details>

**Q4**：往查询里加一个 `df` 极大的词（比如「手机」），分数整体从 8.2 涨到 9.1，但排序几乎没变。为什么？什么场景下这会真的坏事？

<details>
<summary>看答案</summary>

因为 ES 7.0（Lucene 8）起移除了 `coord` 与 `queryNorm`，布尔查询总分是**各子句分数之和**：加一个子句只会给每个命中文档**加一个非负的量**。这个量对所有命中该词的文档近似相同（IDF 低、TF 饱和后贡献小），所以像「整体平移」，排序基本不动。

真正坏事的三个场景：

1. **`min_score` 绝对阈值**：原本 `_score ≥ 3.0` 表示「强相关」，现在 3.0 只是一句泛词的加和，过滤条件失效 ⇒ 召回里混进大量泛结果，且**没有任何报错**。
2. **多路召回的加权融合**（ES 分数 × 权重 + 其他召回分数）：量纲变了，权重全部要重调。
3. **跨分片/跨时间的分数比较**：`df` 随数据增长变化，导致「同一份数据在不同时间点分数不同」，A/B 实验的相关性指标看起来漂移，实际是分数不可比。

**正确姿势**：不要用分数绝对值做业务判断；要用阈值就设成**百分位**（如「前 5% 的分」），或直接把泛词从查询里去掉（它是停用词，不该参与算分）。

</details>

**Q5**：`_nodes/stats/indices/fielddata` 显示 3.2 GB，但全集群搜索过一遍 mapping，没有任何 `fielddata: true`。怎么回事？还要不要报警？

<details>
<summary>看答案</summary>

**要报警，但结论不同。** `fielddata` 这个统计桶不只装 `text` 字段的 fielddata，还装 **`keyword`（以及 `ip` 等）字段的 global ordinals**。

`terms` 聚合和 `significant_terms` 需要把每个分片局部的 ord 映射到「全局第几号」——这张映射表按「字段 × 唯一值数」常驻堆。所以：

- 高基数 `keyword` 字段（比如 `sku_id`、`user_id`，千万级唯一值）做 `terms` 聚合，就会让这个数字涨起来，**但它不是事故**，它是「基数太大」的合法代价；
- `text` 字段的 fielddata 才是事故（那是把整列反转进堆，每 doc 数十~数百 B）。

定位方法：看 `fielddata` 明细的**按字段名拆分**（`indices.fielddata.memory_size_in_bytes` 的分字段视图），确认是 `keyword` 的 ordinals 还是 `text` 的 fielddata。前者治法是降基数 / 改 `composite` 分页聚合 / 把统计挪离线（→ comp/25）；后者治法是加 `.keyword` 子字段并立刻关掉 `fielddata`。

</details>

---

## 24.14 延伸阅读（锚点式）

- 倒排的物理结构、posting 解码为什么是唯一线性项、位图与 postings 的密度分界：`→ comp/21`（[comp/21](21-inverted-index-anatomy.md) §21.4、§21.5、§21.7）
- 段为什么不可变、`liveDocs` 位图怎么排除已删文档、refresh 如何让 query cache 失效：`→ comp/22`（[comp/22](22-segment-and-near-realtime.md) §22.4、§22.5）
- 扇出-归并-回取两阶段、分片数 → 线程数 → 吞吐的扇出算术（本篇的上一层）：`→ comp/23`（[comp/23](23-es-cluster-shards-replicas.md) §23.5、§23.8）
- 容量与故障总账：mapping 爆炸、堆预算、`fielddata` 的长期治理、零停机重建：`→ comp/25`
- 向量检索：带标量过滤的 ANN 与「过滤越严图越走进死路」——它是本篇 filter 思想在另一套结构上的复现：`→ comp/26`
- 搜索系统整体的形态决策（什么时候不该用 ES、写路径三种形态、对账节奏）：`→ architect/17`（[architect/17](../architect/17-search-system.md) §17.3、§17.5、§17.13）
- 列存为什么能扛住全量聚合、为什么它不能当主库（聚合的另一条出路）：`→ comp/20`
- 观测底座：这些指标怎么落到 Prometheus、基数为什么是它的敌人：`→ comp/48`
- 全系列总纲与 Part V 的分工（21 结构 / 22 段 / 23 集群 / 24 执行 / 25 容量）：`→ 0-summary.md` §0.6