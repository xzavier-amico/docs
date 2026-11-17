# 21. 倒排索引的物理结构：一个 term 怎么找到 doc

> **状态**：`已完成（初稿）` ｜ **所属**：第五部分 · 检索与向量 ｜ **编号**：21 / 53
> **本篇定位**：全书唯一一篇**钻进索引文件字节里**的篇目。它只回答一个问题：`term="手机"` 这句话进到磁盘上之后，到底是怎么找到那 3000 万个 docID 的。倒排的**三个物理部件**（词典 / 倒排表 / 位置与归一信息）以及它们为什么必须「有序」，是本篇的全部内容。**段的生命周期、refresh/flush/merge、删除标记全部不在本篇**（→ comp/22），分片与路由不在（→ comp/23），query vs filter 的执行路径与 BM25 的打分细节不在（→ comp/24）。这里只到「倒排表长什么样、一次 term 查询怎么走」为止。
> **读法**：§21.2 的那一句「全在有序两个字上」是全篇支点，后面每一节都是它的推论，读不下去时回到它。§21.3 的三张图（文件布局 / FST 前缀共享 / postings delta+skip）建议对照自己集群的 `_cat/segments` 看一遍。要现场答辩直接跳 §21.7（三本账，全部带推导式）与 §21.11（一页速记）。被追问「为什么 ES 快而 MySQL 的 LIKE '%x%' 慢」读 §21.8。

---

## 21.1 引子：三起「搜不到 / 搜错了 / 搜得慢」的故障

倒排索引的故障有个共同特点：**没有异常、没有错误日志、没有超时**。它只会给你一堆「看起来像答案的结果」——空结果、错顺序、慢三倍。所以这三起事故的时间线上，报警全部是最后才响的。

### 事故一：把 `brand` 从 keyword 改成 text，一次 filter 变成扫 3000 万 posting（2025-11-03）

某电商 6 主分片、1.2 亿 SKU、单分片 22 GB。`brand` 存的是「品牌 + 主推型号」的整串（`Redmi K70 至尊版`）。运营的诉求是「品牌要能模糊搜」（想让「红米」也能搜到 `Redmi`），开发看完文档，把 `brand` 的 `type` 从 `keyword` 改成 `text`（`analyzer: standard`），重建索引。查询侧没动，仍是 `term(brand="Redmi")`。

| 时间 | 动作 / 事件 | 面板上看到什么 |
| --- | --- | --- |
| 11-03 14:00 | 重建完成，1.2 亿 doc 落 6 分片 | 一切正常 |
| 11-03 14:20 | 品牌筛选接口 P99 从 18 ms → 96 ms | 无错误码，qps 不变 |
| 11-03 15:00 | 品牌筛选返回**空结果**：`term` 查 `"Redmi"` 命中 0 | 仓库侧 `_count` 也是 0 |
| 11-03 15:10 | 定位：`_analyze` 显示 `brand` 写入被切成 `["redmi","k70","至尊版"]`，词典里根本没有 `Redmi` 这个 term | — |
| 11-03 15:30 | 把 filter 改成 `match`，命中恢复，但每个命中 doc 都要过一遍打分链路 | 一次品牌筛选命中 **3000 万** doc，P99 620 ms，查询线程池打满 |
| 11-03 15:50 | 运营要「按品牌排序」，`sort: brand asc` 直接 400：`Fielddata is disabled on text fields` | — |
| 11-03 16:10 | 有人按报错提示打开 `fielddata: true` | 堆从 12 GB 涨到 20 GB，一个节点 OOM，分片重分配，集群黄 41 分钟 |
| 11-03 17:30 | 回滚：`brand: keyword` + `brand.text` 子字段（text 只用于模糊搜，筛选/排序仍走 keyword），重建 | — |
| 11-03 22:10 | 重建完成、别名切回 | — |
| 复盘 | 品牌筛选不可用 8 小时，大促预热门户 GMV 损失约 340 万元 | — |

**三件事同时出错，而且都指向同一个物理事实**：`keyword` 的词典里存的是**整串**（`Redmi K70 至尊版` 是 1 个 term），`text` 的词典里存的是**分词后的词**（3 个 term）。`term` 查询**不做分析**，它拿着原样字符串去词典里做「全等查找」——所以字段类型一换，词典内容就换了，查询就必然落空。而把 `match`（走分析 + 打分）当 `filter`（走布尔位图）用，等于把「点查一个位图」换成「扫 3000 万 posting 再算 3000 万次 BM25 分数取 top 20」，这就是 96 ms → 620 ms 的来源（打分路径的成本结构 → comp/24）。

### 事故二：写入用 standard、查询用 keyword 子字段，某商品搜不到（2025-08-19）

多租户 SaaS 商品库。租户 A 的搜索服务自己拼 DSL，为了「精确匹配」用了 `term` 打在 `title.keyword` 上。`title` 的 `keyword` 子字段带 `ignore_above: 256`。

| 时间 | 事件 |
| --- | --- |
| 08-19 | 客服收到 3 起同租户工单：「搜 `iPhone 15 Pro Max` 一条结果都没有」 |
| 08-19 深夜 | 手工复现：`match` 查 `title` 有 2,100 条；`term` 查 `title.keyword` 有 0 条 |
| 08-20 | `_termvectors` 打出该 doc 的 term：`["iphone","15","pro","max","256g","原色","钛金属"]`；`title.keyword` 的 term 是整串 `iPhone 15 Pro Max 256G 原色钛金属` |
| 08-20 | 再查：该租户标题长度 > 256 字符的 SKU 共 3,200 个，`keyword` 子字段被 `ignore_above` **静默跳过**，连整串都不存在 |
| 08-21 | 修：查询侧 `title` 走 `match_phrase`，精确匹配改用独立字段 `sku_code`（keyword）；对该租户 3,200 个 SKU 补字段后重建 |
| 复盘 | 累计空结果会话 6,400 次、客诉 41 起，其中 12 个 SKU 是当季主推；`ignore_above` 超限**不报错**，只是这一项悄悄不索引 |

物理根因只有一句：**`term` 是词典全等查找，词典里没有这个字符串就是 0 命中，与「语义对不对」无关**。这也是为什么「搜不到」类工单的第一条排查动作应当是 `_analyze` + `_termvectors`，而不是去翻代码。

### 事故三：订单号当 text 建索引，词典被撑到 12 GB（2025-12-08）

订单中台 8 分片 × 1 副本、6,000 万订单。开发把 `order_no`（UUID v4，36 字符）和 `tracking_no` 塞进 ES 供运营查单，索引模板是 `dynamic: true`（默认）→ 自动推断成 `text` + `keyword` 双字段。

算一遍就够：`standard` 分词器按 `-` 切，一个 UUID 变成 5 个 token；6,000 万单 × 5 = **3 亿个 term**，且几乎全唯一（df≈1）→ 词典体积 ≈ 3e8 × 42 B ≈ **12.6 GB**，FST 堆 ≈ 3e8/32 × 50 B ≈ **470 MB/分片**，8 个分片就是 3.8 GB 堆，只为了存「每个订单号」。

| 时刻 | 词典堆/分片 | 单分片盘 | 现象 |
| --- | --- | --- | --- |
| 12-08 | 0.30 GB | 8 GB | 正常 |
| 12-15 | 0.32 GB | 62 GB | 无异常（盘在涨，没人看） |
| 12-22 | 0.35 GB | 118 GB | 段数从 14 涨到 41 |
| 12-30 | 0.41 GB | 176 GB | 查询 P99 15 ms → 110 ms：每段都要 seek 一次词典 |
| 01-06 | 0.44 GB × 8 = 3.5 GB | 205 GB（×2 副本 = 410 GB） | 一次 Full GC 4.2 s，节点被踢出，集群红 6 分钟 |
| 01-07 | 改 `order_no: {"type":"keyword","index":false}`（只存不索引）+ `order_no_hash`（keyword，精确查） | — | 重建 9 小时，别名切换 |
| 复盘 | 为「运营能用订单号搜」付了 410 GB 盘 + 3.5 GB 堆 + 一次集群重启；而正确的路是点查回 MySQL，0.3 ms | — | — |

三起事故分别对应本篇三大节：**分析链与字段类型（§21.3）**、**倒排表与跳表（§21.4）**、**词典体积（§21.7）**。

---

## 21.2 一句话定位与唯一真问题

**定位**：倒排索引是「**以 term 为键的、多值的、有序的 docID 集合索引**」。它和 B+ 树的区别不在实现细节，而在键的基数方向：B+ 树是「一个键 → 一行」，倒排是「一个词 → 一千万行」。

### 唯一真问题

> **倒排 = 词典（FST）+ 有序倒排表（delta 压缩 + 跳表）+ 位置信息；「全文搜索为什么快」的答案全在「有序」两个字上。**

这一句要能拆成四步讲完，否则等于没懂：

| 有序带来的能力 | 具体机制 | 没有它会怎样 |
| --- | --- | --- |
| **词典有序** → 能二分、能前缀共享、能压成 FST | 3,000 万个 term 的索引项压到 94 万个节点边，堆占用 47 MB 而不是 1.2 GB | 词典只能哈希，前缀共享失效（体积 ×3~5），也做不了 `prefix` 查询与前缀压缩 |
| **docID 升序** → 相邻 docID 差值很小 → delta 编码后位宽只有 3~9 bit | 216 亿 posting 压到 26 GB；不去重排序的话每个 docID 要 4 B = 86 GB | 体积 ×3.3，而且 CPU cache 全打穿 |
| **docID 升序** → 可以「跳」 | 每 128 个 doc 一个 skip 点，`advance(target)` 不需要从头扫 | 多 term 求交退化成 O(df_A × df_B) 或必须建哈希表 |
| **docID 升序** → 两个 posting 列表可以像归并排序一样线性求交 | 交并差的总代价 O(df_A + df_B)，一趟扫完、顺序 IO | 只能建哈希/位图再求交，多一份内存与一次随机写 |

**反证**：假设倒排表是无序的 `map<term, hash_set<docID>>`——你依然能回答「包含某词吗」，但你失去了 delta（体积×3.3）、失去了跳表（求交要建两张哈希表）、失去了按 docID 顺序读盘（全变随机 IO）。**所有「倒排为什么快」的答案，最后都归到「因为它是有序数组」这一条上**——这是 §21.4 的 PForDelta 与 skip list 的全部前提。

一条更锋利的表述：**倒排不是「查得快」，而是「把一千万行的扫描换成一千万个增量的解码」，而增量之所以能只有 4 bit，是因为 docID 有序。**

### 解剖卡八问：本篇答哪几问

| 八问 | 落在哪节 | 一句话答案 |
| --- | --- | --- |
| ① 承诺什么 | §21.8 | 承诺「按词命中 O(命中数)」；不承诺「子串匹配」「写后立刻可见」「精确更新」 |
| ② 数据长什么样 | §21.3、§21.4、§21.5 | `.tip`(FST) + `.tim`(block) + `.doc`(delta+skip) + `.pos` + `.nvd/.nvm` + `.dvd/.dvm` |
| ③ 一次读走哪条路 | §21.6 | 11 跳，唯一随数据量线性增长的是「遍历 posting」：0.5 ns/posting |
| ④ 并发与一致 | 不在本篇 | 段不可变与可见性 → comp/22；分片副本 → comp/23 |
| ⑤ 怎么横向扩 | 不在本篇 | 分片 = 索引水平切分 + 查询扇出 → comp/23 |
| ⑥ 会在哪崩 | §21.9、§21.10 | 词典爆炸（UUID 当 text）、fielddata 堆炸、posting 扫穿 |
| ⑦ 看哪几个指标 | §21.10 | `terms_memory` / `fielddata.memory_size` / `query_cache.evictions` / store per doc |
| ⑧ 换掉它的信号 | §21.8 | 需要子串/拼写容错 → ngram / 向量（→ comp/26）；需要点查 → 回主库 |

---

## 21.3 物理形态之一：词典与 FST

### 一个段里到底有哪些文件

ES/Lucene 默认把段内文件打成 compound（`.cfs/.cfe`），但逻辑上仍是下面这一套。**记住这套文件清单，后面所有故障都能指到其中一个文件上。**

```
index/sku_v1/0/_0.si  _0.fnm  _0.fdt/_0.fdx  _0.del  ← 段元信息 / 字段表 / 存储字段(_source) / 删除位图
──────────────── ① 词典（term dictionary）────────────────
  _0.tip   FST 索引：每 block 一个索引项，(term 前缀 → .tim 中的 filePointer)   ← 段打开时读进【堆】
  _0.tim   block 数据：块内前缀共享的 term 字节 + docFreq + docStartFP/posStartFP/payStartFP  ← mmap【堆外】
──────────────── ② 倒排表（postings）────────────────────
  _0.doc   docID delta（位宽打包，128 个一组）+ freq + skip 表
  _0.pos   位置 delta（phrase 必需；每出现一次一个值）
  _0.pay   payload / 高亮 offset
──────────────── ③ 归一与列存 ────────────────────────────
  _0.nvd / _0.nvm   norms：每 doc 每字段 1 字节（长度归一因子，BM25 用）
  _0.dvd / _0.dvm   doc_values：列存，排序/聚合/脚本用，mmap【堆外】
──────────────────────────────────────────────────────────
一个分片 = 一个索引目录 = N 个段；每段各有一整套上面这些文件（→ comp/22）
```

| 文件 | 谁的 | 位置 | 丢了会怎样 | 能否从 `_source` 重建 |
| --- | --- | --- | --- | --- |
| `.tim` / `.tip` | 词典 | 堆（FST）+ mmap（block） | 搜不到任何 term | 能（但要全量重灌） |
| `.doc` / `.pos` | 倒排表 | mmap | 能搜到但无法出 doc / 无法验短语 | 能 |
| `.nvd/.nvm` | norms | mmap | 排序相关性全错 | 能（重算长度） |
| `.dvd/.dvm` | doc_values | mmap | 排序聚合全废 | 能 |
| `.fdt/.fdx` | `_source` | mmap | **重建的唯一依据没了** | **不能** |
| `.del` | 删除位图 | mmap | 删掉的文档复活（→ comp/22） | — |

### 分析链：一段文本怎么变成 term

倒排的「键」不是文本，是 term；**term 由分析链生产**，链条三节，每一节都能改变召回。以原文 `小米 Redmi K70Ａ，至尊版 5G` 为例：

- **① char filter**：全角→半角、去 HTML 标签、字符映射 → `K70Ａ， ` 变成 `K70A, `
- **② tokenizer**：切词（`standard` / `ik_max_word` / `ngram`）→ `["小米","Redmi","K70A","至尊版","5G"]`
- **③ token filter**：`lowercase` → `stop` → `stemmer` → `synonym` → `asciifolding` → `["小米","redmi","k70a","至尊","版","5g"]`，并带上位置号 `0,1,2,3,4`
- **④ 落盘**：term 进 `.tim`，位置进 `.pos`，本 doc 内该 term 的出现次数（freq）进 `.doc`

两个硬纪律：**写入与查询必须同一条链**（`analyzer` 与 `search_analyzer` 的差异必须是**故意**的，例如写入 `ik_max_word` 求召回、查询 `ik_smart` 求精度）；**每条链都要能用 `_analyze` 一键复现**，否则「搜不到」永远是猜。

```bash
# 1) 显式 analyzer + 严格 mapping：字段类型绝不许靠 dynamic 猜
curl -s -XPUT 'localhost:9200/sku_v1' -H 'Content-Type: application/json' -d '{
  "settings": { "index": { "analysis": {
      "char_filter": { "fullwidth": { "type": "mapping", "mappings": ["，=>,", "Ａ=>A"] } },
      "filter": { "my_syn": { "type": "synonym", "synonyms_path": "analysis/syn.txt",
                              "updateable": true } } } },
      "number_of_shards": 6, "refresh_interval": "30s" } },
  "mappings": {
    "dynamic": "strict",
    "properties": {
      "title": { "type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart" },
      "brand": { "type": "keyword", "ignore_above": 256 },
      "brand_text": { "type": "text", "analyzer": "standard" },
      "price": { "type": "scaled_float", "scaling_factor": 100 },
      "desc":  { "type": "text", "index_options": "positions" },
      "order_no": { "type": "keyword", "index": false }
    } } }'

# 2) 分析链的每一步都必须能复现：这一步的输出＝写进 .tim 的 term 列表
curl -s -XPOST 'localhost:9200/sku_v1/_analyze' -H 'Content-Type: application/json' -d '{
  "char_filter": ["fullwidth"], "tokenizer": "ik_max_word",
  "filter": ["lowercase", "my_syn"], "text": "小米 Redmi K70Ａ，至尊版" }'
# 期望看到 ["小米","redmi","红米","k70a","至尊","至尊版","版"] —— 少任何一个，就是「搜不到」的根因
```

分析链对完之后，还要看「一个 term 在倒排里到底长什么样」。这三条命令能把 §21.1 的三起事故全部定位到文件：

```bash
# ① 一条已写入的文档被切成了什么 term：doc_freq 直接告诉你 df 有多大（第 ⑦ 跳的成本）
curl -s 'localhost:9200/sku_v1/_termvectors/10086?fields=title&positions=true&term_statistics=true'
{ "term_vectors": { "title": {
    "field_statistics": { "doc_count": 120000000, "sum_ttf": 21600000000 },
    "terms": { "redmi": { "term_freq": 1, "doc_freq": 30000000, "ttf": 66000000,
                          "tokens": [ { "position": 1, "start_offset": 3, "end_offset": 8 } ] },
               "k70":   { "term_freq": 1, "doc_freq": 412000,
                          "tokens": [ { "position": 2 } ] } } } } }

# ② 剖开 term 查询：整串被当成【一个】term ⇒ 词典里没这一项 ⇒ 必然 0 命中
curl -s -XGET 'localhost:9200/sku_v1/_validate/query?explain=true' \
  -H 'Content-Type: application/json' -d '{ "query": { "term": { "title": "红米 k70" } } }'
# → "explanation": "title:红米 k70"        ← term 不做分析，这是事故一/二的定位动作

# ③ 换成 match：能看到展开成哪几个 term（同义词扩张也在这步现形）
curl -s -XGET 'localhost:9200/sku_v1/_validate/query?explain=true' \
  -H 'Content-Type: application/json' -d '{ "query": { "match": { "title": { "query": "红米 k70", "analyzer": "ik_smart" } } } }'
# → "title:(红米 k70)"  /  "title:(小米:0.5 k70:1.0)"
```

| 写入侧 analyzer | 查询侧写法 | 结果 | 物理原因（落在哪个文件） |
| --- | --- | --- | --- |
| `standard` | `term` 打 `title.keyword` | **0 命中** | `.tim` 里 term 是 `["iphone","15","pro"]`，没有 `"iPhone 15 Pro"` 这一项 |
| `keyword`（整值） | `match` 打 `title` | **0 命中** | `.tim` 里只有整串这一个 term，查询被切成 3 个 term，全不在 |
| `ik_max_word` | `ik_smart` | 一般能命中，有歧义 | 两侧 term 粒度不同，短 term 覆盖长 term 时命中，反之漏 |
| 停用词表两侧不一致 | `match_phrase` | 短语位置错位 | `.pos` 里的位置号被 stop filter 留下的 `position_increment_gap` 改变 |
| 同义词只在查询期扩 | 任意 | 召回正常，**相关性漂移** | 扩展后 df 变成多个 term 的并集，IDF 变小 → 打分集体偏软 |

### 词典的两个文件：`.tim` 与 `.tip`

词典不是一个「大哈希表」，而是 **BlockTree**：把所有 term 按字典序排好，切成 block，块内前缀共享，块与块之间用一层 FST 索引。

- **block 切分**：容量规划口径按 `indexInterval` = **每 128 个 term 一个索引项**（Lucene 4 之后的 BlockTree 让 block 内的项数在 25~48 之间自适应，取 128 作为「一个索引项覆盖的 term 数」这个量级不会算错）；
- **块内前缀共享（prefix sharing / suffix 压缩）**：一个 block 内所有 term 共享前缀只存一次，每个 term 只存「与上一个 term 的差异后缀」+ 自己那条倒排的指针；
- **块头**：`(前缀长度, 后缀长度, 后缀字节, docStartFP, posStartFP, payStartFP, docFreq, totalTermFreq)`；
- **FST（`.tip`）**：只存每个 block 的**第一个 term** → `(block 在 .tim 的 filePointer, block 内 term 数)`，且 pointer 存的是**与上一个索引项指针的差值**（通常 1~3 字节，而不是 8 字节绝对地址）。

```
有序 term： apple, applepie, apples, apply, banana, band, bank ...
BlockTree 切块（概念上是每 128 个 term 一块，这里只画 4 个）:
   block 0: [apple | applepie | apples | apply]     block 1: [banana | band | bank | ...]
                   ▲ 块内前缀共享：只存 (前缀长度, 差异后缀) + 该 term 的倒排指针
   block 0 内部布局（概念）:
     prefix=0  suffix="apple"   → docStartFP, posStartFP, docFreq=112,340
     prefix=5  suffix="pie"     → docStartFP, ...          共享 "apple"
     prefix=5  suffix="s"       → docStartFP, ...          共享 "apple"
     prefix=4  suffix="y"       → docStartFP, ...          共享 "appl"

.tip 里的 FST（只存块首 term，value = filePointer 差值）:
        ┌── a ──┬── p ──┬── p ──┬── l ──┬── e ──► block0 @ 0        output +0
        │       │       │       │       └── i ──►  (块内，不在 FST)
        │       │       │       └── y ──►      (块内，不在 FST)
        │       └── 多个 term 共享 a→p→p 这段路径，边只存一次
        └── b ──► ban... ──► block1 @ 1,412                       output +1,412 (varint 2B)
   3,000 万 term → 索引项 ≈ 3e7/32 ≈ 94 万个 → FST 堆占用 ≈ 94 万 × 50 B ≈ 47 MB
```

### 词典放哪里：mmap 与堆，以及「term index 常数级内存」的直觉

| 部件 | 打开方式 | 内存归属 | 量级（3,000 万 term / 分片） | 为什么 |
| --- | --- | --- | --- | --- |
| `.tim` block 数据 | `MMapDirectory` | **堆外**（page cache） | 1.2 GB 文件，热 block 占几 MB 页缓存 | 顺序读 + 随机 seek 少量 block，交给内核页缓存最划算 |
| `.tip` 的 FST | 段打开时**读进堆** | **堆内** | 94 万索引项 × ~50 B ≈ **47 MB/分片** | 每次查询要跳 2~4 层，mmap 的 page fault 成本远高于常驻 |
| `.dvd/.dvm` | `MMapDirectory` | **堆外** | 与字段基数相关 | 列式顺序扫，页缓存复用率高 |

**这就是「term index 常数级内存」的由来**：FST 的堆占用 = 唯一 term 数 × **1~2 字节**（因为索引项 = term 数/32，每项约 50 B，50/32 ≈ 1.6）。它之所以是「常数级」而不是「随文档数线性」，是因为 **term 数只跟语料词汇量有关，跟文档数无关**——1 亿文档和 10 亿文档的电商库，唯一 term 数可能都在 3,000 万上下。反过来说，**任何让唯一 term 数随文档数线性增长的设计（UUID、订单号、时间戳当 text）都会把这条「常数」变成直线**，这就是事故三。

---

## 21.4 物理形态之二：倒排表、跳表与位置

### docID 升序 → delta 编码 → 位宽打包（PForDelta）

一个 term 的倒排表在逻辑上是「升序 docID 数组」，物理上是**分块 + 差分 + 按位宽打包**：

1. 每 **128** 个 docID 一个 block；
2. block 第一个值存**绝对** docID（varint），其余 127 个存**与上一个的差值**（delta）；
3. 取本块最大 delta 的位宽 `b`，128 个值各用 `b` bit 打包 → `128 × b / 8` 字节；
4. 少数超出 `b` bit 的值作为「异常值（patch）」单独存：3 bit 的位置 + 完整值（这就是 PForDelta / Lucene 的 `PForUtil`）；
5. 极端情况整块 delta 全相等（例如连续 docID）→ `b=0`，**这一块 0 字节**。

| 例子 | 原始 docID | delta | 位宽 b | 每 128 值体积 | 压缩比 |
| --- | --- | --- | --- | --- | --- |
| 稀疏（词只出现在某类目） | 3, 5, 9, 17, 400, 401 … | 3,2,4,8,383,1 … | 9 bit | 128 × 9/8 = 144 B | vs 4 B/值 = 3.6× |
| 中等 | 112,340, 112,401 … | 恒在各处 +61 左右 | 6~7 bit | 96~112 B | 4.6~5.3× |
| 密集连续（含停用词的字段） | 1000,1001,1002 … | 全是 1 | 0~1 bit | 0~16 B | 32×+ |
| 均值（容量规划用） | — | — | ~7 bit | — | **≈ 1.2 B/posting** |

**为什么它是「有序」的直接推论**：随机顺序的 docID 差值均匀分布在 `[0, N)`，位宽恒为 `log2(N)` = 27 bit（1.2 亿 doc）→ 3.4 B/posting；有序后差值集中在小值区间，均值 7 bit → 0.9 B/posting。**同一份数据，只因为排了序，体积差 3.8 倍。**

### 跳表（skip data）：每 128 个 doc 一个「跳点」

```
.doc 文件（term = "手机"，df = 30,000,000，占 1.2 亿 doc 的 25%）
┌ block 0 ─────────────────────────────────────────────────────────┐
│ 首值: docID=3 (varint) │ 增量: 2,4,8,1,1,3,...  位宽 b=4          │
│                        │ 128 个值 × 4 bit = 64 B                   │
└──────────────────────────────────────────────────────────────────┘
┌ block 1 ─────────────────────────────────────────────────────────┐
│ 增量: 1,1,2,...,383    位宽 b=9 → 128 × 9 bit = 144 B             │
└──────────────────────────────────────────────────────────────────┘
   ... 共 30,000,000/128 = 234,375 个 block ...
┌ skip 表（每 128 个 doc 一条，分层 MultiLevelSkipList）───────────┐
│ 层0: [docID delta | docStartFP | posStartFP | payStartFP] × 234,375 条 ≈ 1.9 MB │
│ 层1: 每 8 条层0 抽 1 条 → 29,296 条                                │
│ 层2: 每 8 条层1 抽 1 条 →  3,662 条                                │
│ 层3+: ... 直到单条                                                  │
└──────────────────────────────────────────────────────────────────┘
   advance(target) = 二分 skip 层定位 block + 块内 ≤128 次比较；
   没有 skip 表时，advance 只能「从当前位置一路解码过去」。
```

`index_options` 决定哪几个文件存在，也直接决定体积（相对「只开 docs」= 1.0）：

| `index_options` | 写了什么 | 落哪个文件 | 体积倍数（相对 docs） | 换来的能力 | 关掉会怎样 |
| --- | --- | --- | --- | --- | --- |
| `docs` | docID delta | `.doc` | **1.0**（≈0.85 B/posting） | term / 布尔 / filter | — |
| `freqs` | + 每 doc 的 freq | `.doc` | 1.4（≈1.2 B/posting） | BM25 的 TF 项 | 打分退化（TF 当 1） |
| `positions` | + 每个 occurrence 的位置 delta | `.pos` | **3.7**（≈3.1 B/posting，按 2.2 次/doc 算） | `match_phrase`、短语、邻近 | **短语查询直接失效或退化** |
| `offsets` | + 字符起止偏移 | `.pos`（+ `.pay`） | 4.2（≈3.6 B/posting） | unified highlighter 免重分析 | 高亮变慢（要重新分词） |

**注意顺序不可逆**：ES 的 `index_options` 只能往「更贵」改（`docs → freqs → positions → offsets`），反向必须重建索引。这不是限制癖，是因为 `.pos` 缺席时，历史数据里根本没有位置信息可补。

---

### 第三条路线：位图（bitset / RoaringBitmap）

当条件是「字段等于 X」「字段在集合里」「字段在范围内」且**不参与打分**时，命中集合最终会被表示成位图而不是 posting 流。物理上有两种表示，取舍只看**密度**：

- **位图**：docID 空间按 2^16 = 65,536 一个「容器」切（RoaringBitmap 的做法）。容器内密度 > 1/16（4,096 个命中）时用定长位图 = 8 KB；低于阈值用**有序 short 数组**（2 B/命中）；整段连续时退化成一个 `(start, length)` 的 run。1,000 万 doc = 153 个容器；全稀疏时内存 ≈ 命中数 × 2 B，全稠密时 = 153 × 8 KB = 1.2 MB。
- **postings**：`df` 个 posting × 1.2 B，且解码是逐块进行的。

| 维度 | postings 求交 | 位图求交 |
| --- | --- | --- |
| 单次操作粒度 | 1 个 posting（解码 + 比较） | 64 个 doc（一次机器字 AND） |
| 单位成本 | ~0.5 ns/posting | ~0.3~1 ns / 64 doc ⇒ **~0.01 ns/doc** |
| 1,000 万 doc 一次 AND | — | 156,250 个字 → **0.05~0.15 ms** |
| 内存 | 0（流式，不落地） | 命中数 × 2 B（稀疏）或 8 KB/容器（稠密） |
| 什么时候赢 | 命中率 < 1%（`df` 小） | 命中率 > 5~10%（`df` 大、可复用缓存） |

**所以「为什么 filter 场景会退化成位图」的答案是密度算术**：一个 `status = online` 的 filter 在 1.2 亿 doc 的段里命中 5,000 万（密度 42%），用 postings 要解 5,000 万个增量（≈25 ms），用位图（1.2e8 bit = 15 MB）做一次 AND（≈0.09 ms），差 270 倍——而且位图能被缓存下来给下一个请求复用（`indices.queries.cache`），postings 不能。**哪些条件进 filter、缓存命中率怎么算、query 与 filter 两条执行路径的分工，属于执行层（→ comp/24）**；本篇只管「这两条数据结构长什么样、哪个在什么密度下更省」。

### 40 行 Go：把这个结构复刻一遍

```go
package tinysi

import "sort"

type Index struct{ post, skip map[string][]int32 } // term → 升序 docID；skip 每 128 doc 一条
func Build(terms [][]string) *Index { // terms[docID] = 分析链输出（已去重）
	ix := &Index{map[string][]int32{}, map[string][]int32{}}
	for doc, ts := range terms {
		for _, t := range ts {
			ix.post[t] = append(ix.post[t], int32(doc))
			if len(ix.post[t])%128 == 0 { // 每满 128 个 doc 落一个跳点
				ix.skip[t] = append(ix.skip[t], int32(doc))
			}
		}
	}
	return ix
}
// skipTo：走 skip 跳到 target 附近，块内线性扫 —— O(log|skip|+128) 而非 O(n)
func skipTo(p, sk []int32, cur int, target int32) int {
	if b := sort.SearchInts(sk, target) - 1; b >= 0 && b*128 > cur {
		cur = b * 128
	}
	for cur < len(p) && p[cur] < target {
		cur++
	}
	return cur
}
// Intersect：leapfrog 求交，谁小谁追 —— query 侧多 term 归并就是这么走的
func (ix *Index) Intersect(a, b string) (out []int32) {
	for i, j, pa, pb := 0, 0, ix.post[a], ix.post[b]; i < len(pa) && j < len(pb); {
		if pa[i] == pb[j] {
			out, i, j = append(out, pa[i]), i+1, j+1
		} else if pa[i] < pb[j] {
			i = skipTo(pa, ix.skip[a], i, pb[j])
		} else {
			j = skipTo(pb, ix.skip[b], j, pa[i])
		}
	}
	return out
}
```

---

## 21.5 物理形态之三：norms 与 doc_values

倒排回答「哪些 doc 命中」，但**排序与统计**不能靠倒排。于是每个字段旁边还挂两套附属结构：

| 维度 | 倒排（term→doc） | norms（`.nvd/.nvm`） | doc_values（`.dvd/.dvm`） | fielddata |
| --- | --- | --- | --- | --- |
| 方向 | term → docID 列表 | docID → 1 字节长度因子 | docID → 值（列式） | term → docID |
| 位置 | `.tim`+`.doc`（mmap） | mmap，**1 B/doc** | mmap，**堆外** | **堆内** |
| 谁在读 | term / 布尔 / filter | BM25 的长度归一 | sort / aggs / script / 部分过滤 | text 字段的 sort/agg（唯一途径） |
| 成本（1.2 亿 doc 单字段） | 与 df 相关 | 120 MB（=1.2e8 × 1 B） | keyword 字典序 ord 1~4 B/值；数值 4~8 B/值 | 数十~数百 B/doc ⇒ **10~50 GB** |
| 关掉的后果 | — | 长文档系统性占便宜 | **不能排序、不能聚合** | — |
| 典型反模式 | — | 对超长文本字段开（无所谓，1 B 而已） | 对高基数字段开（盘涨） | **一开就是 OOM 候选** |

**norms 是什么，为什么它只有 1 字节**：BM25 的长度归一因子是 `1 - b + b × dl/avgdl`（`b = 0.75`），其中 `dl` 是字段长度。Lucene 不存精确长度，而是用 `SmallFloat` 把长度压成**1 字节、4 位有效数字**的近似值——1.2 亿文档只要 120 MB，且顺序访存对 CPU cache 极友好（打分时每 doc 读 1 字节，几乎全部命中 cache line）。**把 `norms` 关掉的后果不是「算不准」，而是「所有 doc 都按平均长度处理」**：`dl` 恒等于 `avgdl`，归一因子恒为 1，于是**堆满关键词的长文档（SEO 垃圾、重复描述的商品）会系统性排在前面**。这个 bug 没有报错，只有「相关性最近变怪了」。

**doc_values 是什么**：把字段值按 docID 顺序**列存**下来（默认对所有非 `text` 字段开启），带字典序（keyword 转成整数 ord，再靠 ord 做范围查询）+ 块压缩（每 `2^n` 个值一块，块头存最小值与位宽）。它用 mmap 打开，**不进堆**，所以「对 1.2 亿 doc 排序」不会 OOM。这就是「**能**对 keyword/数值排序聚合、**不能**对 text 排序聚合」的全部物理原因：text 字段没有 doc_values。

**fielddata 是什么**：为了给 `text` 字段强开 sort/agg，把倒排**反转**成「term → docID」结构并放进**堆**里（要带 `String` 对象和 ord 映射）。一次 `terms` 聚合在 3 秒内吃掉 20 GB 堆是常规操作——事故一里打开 `fielddata: true` 之后的 OOM 就是这条路径。**纪律**：生产环境把 `indices.fielddata.memory_size_in_bytes` 当报警指标（§21.10），而不是当可调参数。

---

## 21.6 一次 term 查询的完整链路（逐跳耗时）

把前四节拼起来，就是一次 `term="手机"` 查询的真实路径。**注意第 7 跳是唯一随数据量线性增长的环节**——这是整张图的读法。

```
 client ──POST /sku_v1/_search {"term":{"title":"手机"}}──► 协调节点
                                                                  │ 按 _routing / 轮询选分片（→ comp/23）
 ② 分片内：query parse → DSL rewrite（同义词扩、类目映射）───────► [terms: 手机, mobilephone]
                                                                  │
                      ┌───────────────────────────────────────────┘
                      ▼  每个 term 走一遍 ③④⑤
   ③ .tip (FST, 堆内)  ── 找 ≤key 的索引项 ──► (block filePointer, block 内 term 数)
   ④ .tim (mmap)       ── seek 到 block ──► 块内前缀共享解压 ──► 顺序比较 ≤48 次 ──► 命中 term
   ⑤ TermState         ── docStartFP, posStartFP, docFreq=30,000,000, totalTermFreq=66,000,000
                      ┌───────────────────────────┘
                      ▼
   ⑥ .doc  postings 迭代器（建 skip：skipOffset → 层0~3）
   ⑦ leapfrog 求交：advance(target) 走 skip → block 内解码 → 交集 docID
   ⑧ 需要 phrase？ ──► 回 .pos 读位置数组，验「相邻且有序」──► 候选集收缩
   ⑨ 打分：.nvd/.nvm 取 dl（1 B/doc）→ BM25(tf, df, dl)   ── 只有进候选集的 doc 才打分
   ⑩ collector：K=10 的小顶堆，O(N log K)；N = 候选数，K 与 size 有关
   ⑪ 协调节点归并各分片 top-K（size × 分片数 的堆）──► 返回 hits
```

| 跳 | 动作 | 碰什么文件 / 内存 | 量级（单分片，段数 ≤ 20） | 备注 |
| --- | --- | --- | --- | --- |
| ① | 请求解析 + 路由 | 协调节点 CPU | 200~800 μs | 含鉴权与 DSL 校验 |
| ② | query parse → rewrite（同义词/类目） | CPU | 100~300 μs | 同义词扩张是 term 数的乘数 |
| ③ | FST 查找 | `.tip`（**堆**） | 0.2~1 μs/term | 3~4 层跳转，约 3 次字符串比较 |
| ④ | 定位并解压 `.tim` block | mmap **页缓存**（热）/ 盘（冷） | 2~10 μs（冷 50~200 μs） | 一次只解一个 ≤48 term 的 block |
| ⑤ | 拿 TermState | 段元数据 | < 1 μs | docFreq / totalTermFreq 就在这里 |
| ⑥ | 建 postings 迭代器 + skip | `.doc` | 1~5 μs | skip 层数是常数（≤4） |
| ⑦ | **遍历 / 求交** | `.doc` | **0.5 ns/posting**；3,000 万 → **15 ms** | **唯一线性项**，一切慢查询都在这一行 |
| ⑧ | 验 phrase | `.pos` | 每候选 doc 20~100 ns + 位置解码 | 只有 phrase 才付 |
| ⑨ | BM25 打分 | `.nvd/.nvm`（norms） | ~5 ns/doc | 1 B/doc，cache 友好 |
| ⑩ | collector top-K | JVM 堆 | N log K：N=10 万、K=10 → 0.5 ms；K=10,000 → 2 ms | 深分页的成本在这里（→ comp/24） |
| ⑪ | 跨分片归并 | 网络 + 堆 | 0.5~2 ms + `size × 分片数` 的内存 | 分片越多越贵（→ comp/23） |
| — | **合计（命中 300 万，K=10）** | — | **P50 3~6 ms / 分片；P99 15~30 ms** | 命中 3,000 万时 P99 直接进 300 ms |

**这张表的用法**：任何一次搜索变慢，先问「第 ⑦ 跳扫了多少 posting」。`df` 是唯一能解释「同一份代码昨天 20 ms 今天 500 ms」的量（`profile: true` 会直接给 `match_count` 与每个 term 的 `docFreq`）。其余十跳加起来通常不到 1 ms。

### 客户端骨架（Java，写入 + 查询）

```java
// 依赖：co.elastic.clients:elasticsearch-java 8.x（ES 8 新客户端）
// 1) 建索引：显式 mapping + 严格 dynamic（事故一/二/三的共同预防）
var body = """
    {"settings":{"number_of_shards":6,"refresh_interval":"30s"},
     "mappings":{"dynamic":"strict","properties":{
       "title":{"type":"text","analyzer":"ik_max_word","search_analyzer":"ik_smart"},
       "brand":{"type":"keyword","ignore_above":256},
       "price":{"type":"scaled_float","scaling_factor":100},
       "desc" :{"type":"text","index_options":"positions"},
       "order_no":{"type":"keyword","index":false}}}}""";
client.indices().create(c -> c.index("sku_v1").withJson(new StringReader(body)));

// 2) 写：_id 由业务主键派生（重放幂等），批量走 bulk
var ops = new ArrayList<BulkOperation>();
for (var s : batch) {
    ops.add(new BulkOperation.Builder().index(i -> i
        .index("sku_v1").id(Long.toString(s.id()))
        .document(Map.of("title", s.title(), "brand", s.brand(),
                         "price", s.priceCents() / 100.0, "desc", s.desc()))).build());
}
client.bulk(b -> b.operations(ops));     // 429 时退避重试，不加队列（→ eng/15）

// 3) 查：短语走 must（要打分），品牌过滤走 filter（不进打分、可缓存位图 → comp/24）
var resp = client.search(s -> s.index("sku_v1")
        .query(q -> q.bool(b -> b
            .must(m -> m.matchPhrase(mp -> mp.field("title").query("红米 k70")))
            .filter(f -> f.term(t -> t.field("brand").value("Redmi")))))
        .trackTotalHits(t -> t.enabled(false))          // 不数总数：省掉一趟全量遍历
        .size(20)
        .source(src -> src.filter(f -> f.includes("title", "brand", "price"))),
    ObjectNode.class);
```

---

## 21.7 三本账：体积、构建成本与查询成本

目标量级：**1.2 亿文档、6 主分片、平均 180 个 term/文档、唯一 term 3,000 万**。全部给推导式，评审时能逐行复述。

```
① 倒排表体积（postings）
   总 posting 数 = 1.2e8 doc × 180 term/doc = 216 亿
   只开 docs  ：delta 位宽均值 ~7 bit ≈ 0.85 B/posting（skip 表与块头的分摊已算进）
                216e8 × 0.85 B = 184e8 B ≈ 18.4 GB
   加 freqs   ：每 doc 一个 freq，位宽 3 bit ≈ 0.35 B/posting
                216e8 × 0.35 B = 7.6e9 B → 合计 ≈ 26.0 GB  ← 「≈1.2 B/posting → 26 GB」
   加 positions：每 term 每 doc 平均出现 2.2 次（标题里词会重复）
                位置数 = 216e8 × 2.2 = 475 亿 × 0.9 B = 42.8 GB
                合计 ≈ 68.8 GB  = 26.0 × 2.6 倍（相对 docs-only 是 3.7 倍）
   加 offsets  ：位置 + 字符偏移，约再 ×1.15 → ≈ 79 GB
② 词典体积（唯一 term 3,000 万）
   .tim ≈ 3e7 × 40 B（平均 term 12 B + 前缀共享后净增 + 每 term ~20 B 元数据）≈ 1.2 GB
   .tip(FST) ≈ (3e7/32) 个索引项 × 50 B ≈ 94 万 × 50 B ≈ 47 MB  ← 这部分进【堆】
   ⇒ 「词典大小 ≈ 唯一 term 数 × 常数（40 B）」；常数与文档数无关，只与词汇量有关
③ 构建成本（单 doc）
   分析链：180 term/doc × 0.15 μs/term ≈ 27 μs
   编码 + 落段：≈ 10 μs            ⇒ 单 doc ≈ 40 μs ⇒ 8 核理论上限 ≈ 20 万 doc/s
   但真实写入上限不在 CPU：merge 写放大 3~5×（→ comp/22）+ 与查询抢磁盘带宽
   全量灌数要重写的字节 = 1.2e8 × 880 B × 4 = 42 GB
   ⇒ 稳态单节点 ≈ 20 万 / 4 ≈ 5 万 doc/s，且这只是「离线灌数」的数字
④ 查询成本（单 term，df = 3,000 万）
   扫 posting：3e7 × 1.2 B = 36 MB（page cache 命中则无 IO）
   解码吞吐 ~0.5 ns/posting → 3e7 × 0.5 ns = 15 ms      ← P99 的分水岭
   skip 收益：与「df = 3 万」的另一个 term 求交
     无跳表：3,000 万 + 3 万 = 3,003 万次推进 ≈ 15.0 ms
     有跳表：3 万个目标各跳一次，最多解 128 个 posting/次 → 3e4 × 128 = 384 万 ≈ 1.9 ms
     ⇒ 省 (3003-384)/3003 = 87%
     反例：若另一侧 df 也是 3,000 万且几乎重合，跳表收益趋近 0，
           总代价 ≈ df_A + df_B = 6,000 万 ≈ 30 ms —— 这就是大词 AND 大词必然慢的原因
⑤ 位图缓存账（filter 的收益上限）
   df = 5,000 万的 status filter：postings 解 5,000 万增量 ≈ 25 ms
   位图：1.2e8 bit = 15 MB（密度 42% ⇒ 全部容器都走位图，不进入 sparse 数组表示）
         一次 AND = 1.2e8/64 = 187.5 万字 × ~0.5 ns ≈ 0.09 ms ⇒ 快 270 倍
```

**三本账合起来的一句话**：这个形态的**空间**被 `positions` 主导（26 GB → 79 GB 全在位置），**写入**被 merge 主导（42 GB 重写量），**读**被 `df` 主导（15 ms 的天花板在单个高频 term 上，不在你的代码里）。所以容量评审时真正要问的三个数是：**开不开 positions、删除/更新比例、最大的 3 个 term 的 df**。

| 决策 | 省下 | 代价 | 什么时候值得 |
| --- | --- | --- | --- |
| `index_options: docs`（不开 positions） | 体积 68.8 GB → 26 GB（省 62%） | `match_phrase` 失效、高亮要重新分析 | 只需要布尔筛选 + 单 term 匹配的字段（分类、标签、`enum` 类） |
| `norms: false` | 120 MB/字段（可以忽略） | 长文档系统性占便宜 | 长度无关的字段（`order_no`、枚举、统一长度的编码） |
| `doc_values: false` | keyword 列存（往往占索引 30~50%） | 不能排序/聚合/脚本 | 只用于 filter 的字段（`status`、`tenant_id`） |
| `index: false`（只存不索引） | 整个倒排 + 词典 | 完全不能搜，只能从 `_source` 取回 | 订单号/URL/原始 JSON 这类「只展示、只用 `_id` 或 hash 查」的字段 |
| `_source: false` | 存储字段（约 doc 大小的 1 倍） | **失去重建能力**，mapping 一错只能回权威源全量重灌 | 只在「权威源 100% 可回放 + 有 CDC」时考虑（→ architect/17 §17.11） |

---

## 21.8 架构边界：倒排能做什么、不能做什么

| 需求 | 倒排能不能 | 复杂度 | 靠什么绕过 |
| --- | --- | --- | --- |
| 「包含某个词」的文档 | **能**，这是它的存在理由 | O(df + k) | — |
| 多词布尔组合（AND/OR/NOT） | 能 | O(Σdf) 一趟归并 | — |
| 短语 / 邻近（`match_phrase`、`slop`） | 能，**必须开 positions** | O(df × 平均位置数) | 无 positions 时降级为 AND，精度丢失 |
| 前缀匹配（`prefix`、联想） | 能（词典有序 → FST 走前缀路径） | O(前缀下的 term 数 × 每个的 df) | 前缀太短（1~2 字）时 term 数爆炸，改 ngram 或专门的前缀索引 |
| **中缀 / 子串匹配**（`*phone*`） | **不能**（除非 ngram） | 前缀通配会退化全 term 扫描 | `ngram` 分词器（体积 ×5~20）或向量召回（→ comp/26） |
| 拼写容错 / 模糊（`fuzzy`） | 勉强（编辑距离走 FST） | 与 Levenshtein 半径和词典分支因子成正比 | 只对短词开、半径 ≤ 2；规模大了上向量 |
| 相关性排序 | 能（BM25 + norms + doc_values） | 每候选 doc ~5 ns | `boost`/`function_score`，重活下推到离线（→ comp/24） |
| 精确聚合 / 排序 | 能（走 doc_values） | 列存顺序扫 | 高基数 `cardinality` 只是估算，不能对账 |
| 唯一约束 / 事务 / 点查主键 | **不能** | — | 回关系库（0.3 ms 点查，见事故三复盘） |
| 高频更新（计数、库存） | **不能**（更新=重建倒排 + merge） | 写放大 3~5× | 权威源 + 查询后二次校验（→ architect/17 §17.1） |

### 与 B+ 树的正面比较

| 维度 | B+ 树索引（InnoDB 二级索引） | 倒排索引 |
| --- | --- | --- |
| 键 → 值 | **1 个键 → 1 行**（唯一或少量重复） | **1 个 term → 最多上千万 doc** |
| 键是什么 | 整列值（或前缀） | 分析链产出的 **token** |
| 等值查询 | O(log N + k)，每层一次页读 | O(FST 查找 + df) |
| 「包含某词」查询 | **做不到**（除非建 FULLTEXT/GIN 那种倒排） | 这是主业 |
| `LIKE '%x%'`（中缀） | **全表扫**：B+ 树只对**前缀**有序，中缀没有可下推的边界 | O(命中数)；只有跨词边界的子串仍不行（要 ngram） |
| 范围查询 | **强项**（叶子链表顺序扫） | 弱：term 之间无序，范围 = 多个 term 的 df 相加 |
| 存储粒度 | 行 / 页（8~16 KB） | docID 增量（压缩后 1.2 B）+ 位置 |
| 更新 | 原地改页 + redo/undo | **不可变 + 重建**（→ comp/22） |
| 一条记录的「宽度」 | 整行（含所有列） | 只有 docID 与位置，字段值在 doc_values/`_source` |

**为什么 `LIKE '%x%'` 在关系库上只能全扫**：B+ 树的有序性只对「比较的前缀」有效——`LIKE 'x%'` 能找到起点然后顺序扫，`LIKE '%x%'` 的起点不确定，优化器没有任何边界可下推，只能逐行做字符串匹配。倒排把这个问题的解法换掉了：**它不索引「字符串」，它索引「词」**，于是「某个词出现在哪些 doc」天然是一个键值查找。这就是为什么关系库要做全文检索也必须在旁边挂一个倒排（MySQL `FULLTEXT`、PG `GIN`）——而挂了之后，你就同时继承了这个形态的全部代价：**近实时、写放大、需要重建**（「什么时候不该选 ES」见 [eng/37 §37.4.4](../eng/37-nosql-selection.md)，形态边界与 mapping 决策见 [architect/17 §17.5](../architect/17-search-system.md)）。

---

## 21.9 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到物理形态） | 怎么定位 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 某商品「搜不到」，`_count` 也是 0 | `term` 是**词典全等查找**，写入/查询分析链不一致（事故一、二） | `_analyze`（写入侧 + 查询侧）、`_termvectors`、`_validate/query?explain=true` | 查询侧文本字段改 `match`/`match_phrase`；精确匹配用独立 `keyword` 字段 | 字段命名约定：`*_text` 只给全文、`*_code`/`*_kw` 只给精确；CI 里跑 `_analyze` 断言 |
| 同样的查询「昨日 20 ms、今日 500 ms」 | 某个 term 的 `df` 变了（词典/倒排没变，数据变了）→ 第 ⑦ 跳线性放大 | `profile: true` 看 `docFreq` 与 `match_count`；`_termvectors?term_statistics=true` | 加 `filter`/时间窗收敛候选；对该 term 走位图缓存 | 把「top-3 高频 term 的 df」做成日更看板（§21.10） |
| 短语查询返回空、但两个词单独都能搜到 | `index_options` 只到 `docs/freqs`，`.pos` 不存在 | `GET idx/_mapping` 看 `index_options`；`_termvectors?positions=true` 为空 | 改 `positions` 并**重建**（不可逆） | 模板里对需要短语/高亮的字段显式写 `"index_options":"positions"` |
| 排序/聚合相关性「最近变怪」 | `norms: false` → 长度归一恒为 1，长文档占便宜 | 对比同一 query 加 `explain` 的 `dl` 与 `avgdl` | 打开 norms 并重建 | 只对长度无关字段关 norms，且写进模板注释 |
| 节点 OOM，堆直方图全是 `BytesRef`/`LongArray` | `text` 字段开了 `fielddata` | `indices.fielddata.memory_size_in_bytes` 飙升；`_nodes/hot_threads` | 立即关 `fielddata`，改用 `.keyword` 子字段 | 该指标 > 0 就告警（§21.10），不给「打开试试」的机会 |
| 磁盘半年涨 5 倍，doc 数没变 | 唯一 term 数随文档数线性增长（UUID/时间戳当 text，事故三） | `store.size / docs.count` 比值；`_cat/segments` 看 `size.terms` | 字段改 `keyword + index:false`，精确查走 hash 字段或回主库 | 建索引前算一遍「唯一 term 数 ≈ 文档数 × 每 doc 的随机 token 数」 |
| 一次 filter 查询把查询线程池打满 | `match` 当 `filter` 用：3000 万命中逐个打分 | 慢日志 + `profile` 里 `score` 阶段占比高 | 不参与排序的条件全部放 `filter` context | 代码规范：`must` 只放需要打分的条件（→ comp/24） |
| 索引重建耗时 9 小时、期间无搜索结果 | 词典太大 + `_source` 关闭后无法从索引侧重建 | 重建任务的分阶段耗时 | 别名 + 影子索引（→ architect/17 §17.11） | 每季度演练一次重建，把重建窗口写进 SLO |

**反模式速查（按物理根因归类）**：

| 反模式 | 物理根因 | 后果（可算） | 正确做法 |
| --- | --- | --- | --- |
| UUID / 订单号 / URL 建 `text` | `standard` 把一个 UUID 切成 5 个 token，几乎全唯一 → 唯一 term 数 = 文档数 × 5 | 6,000 万单 → 3 亿 term → `.tim` ≈ 12.6 GB，FST 堆 ≈ 470 MB/分片 | `keyword` + `index:false`；要查就存 hash 后精确匹配，或回主库点查 |
| 关 `_source` | 段不可变，`_source` 是唯一的重建原料 | mapping/分析器一错，只能从权威源全量重灌（小时~天） | 保留 `_source`；至少要保证权威源 + CDC 可回放 |
| 关 `positions` | `.pos` 缺席 | `match_phrase` 失效或退化成 AND；高亮要重新分析 | 需要短语/高亮的字段显式 `positions` |
| 关 `norms` | 长度归一因子缺失 | 长文档 TF 虚高、排序系统性偏向堆砌型文档 | 只对「长度无意义」的字段关 |
| 同义词只在索引期扩 | 词典里塞进大量同义 term，df 与 IDF 全变 | 想改同义词必须**重建**；词典体积 +N 倍 | 同义词放**查询期**（`updateable: true` 可热更，可回滚）；索引期扩展只在必须 ngram 类召回时用 |
| 高频更新字段进 ES | 更新 = 删标记 + 重建倒排 + merge | 写放大 3~5× | 计数/库存放权威源，查询时二次校验（→ architect/17 §17.1） |

---

## 21.10 观测：指标、拐点与告警

只看 5 个数就能覆盖本篇的全部崩法。**拐点值的算法都写在表里**，不要照抄别人的阈值。

| 指标 | 含义 | 拐点 | 拐点怎么来的 |
| --- | --- | --- | --- |
| `indices.segments.terms_memory_in_bytes` | **FST 词典堆占用**（本篇主角） | 每 doc > 6 B，或单分片 > 400 MB | 正常 = 唯一 term × 1.6 B；1.2 亿 doc 分 6 片 → 3,000 万 term/片 ≈ 47 MB ≈ 0.4 B/doc。涨到 6 B/doc 说明词典被高基数字段撑爆 |
| `indices.fielddata.memory_size_in_bytes` | text 字段反转到堆内 | **> 0 就告警**（允许 < 200 MB 的临时窗口） | 唯一合法用途是「马上要改 mapping」，长驻即事故 |
| `indices.query_cache.evictions` / `memory_size` | filter 位图缓存（§21.4 第三条路线） | 驱逐率 > 100/min 或占用 > 堆 × 10% | 缓存被高频变化的 filter 冲刷 → 每次请求都重扫 postings |
| `indices.store.size_in_bytes / indices.docs.count` | **每 doc 字节数**（体积放大镜） | > 1,500 B/doc 或 7 天内 +30% | `_source` 800 B + 倒排/doc_values ~300~500 B ≈ 1,100~1,300 B 是健康值；UUID 当 text 会到 3,000 B+ |
| `search.slowlog`（`query` 阶段 > 300 ms）/ `profile` 的 `match_count` | 第 ⑦ 跳扫了多少 posting | 单查询 `match_count` > 2,000 万 | 2,000 万 × 0.5 ns = 10 ms 解码，加 page cache 抖动即 P99 破 300 ms |

```yaml
# elasticsearch_exporter 指标 → Prometheus 告警骨架
groups:
- name: es-inverted-index
  rules:
  # ① 词典被高基数字段撑爆（事故三的实时探测器）
  - alert: EsTermDictionaryTooBigPerDoc
    expr: elasticsearch_indices_segments_terms_memory_in_bytes
          / (elasticsearch_indices_docs_count > 0) > 6144
    for: 30m
    annotations: { summary: "FST 堆 > 6 B/doc：查高基数字段是不是被建成 text" }
  # ② fielddata 常驻 = 必炸（事故一的下半场）
  - alert: EsFielddataLeak
    expr: elasticsearch_indices_fielddata_memory_size_bytes > 2e8
    for: 10m
    annotations: { summary: "text 字段开了 fielddata，改 .keyword 子字段" }
  # ③ filter 位图缓存被冲刷 → 每次重扫 postings
  - alert: EsQueryCacheThrashing
    expr: rate(elasticsearch_indices_query_cache_evictions_total[5m]) > 100
    for: 10m
    annotations: { summary: "filter 缓存失效：检查 filter 的变化频率/基数" }
  # ④ 每 doc 体积异常 = 词典线性膨胀的前兆
  - alert: EsBytesPerDocAnomaly
    expr: elasticsearch_indices_store_size_bytes / (elasticsearch_indices_docs_count > 0) > 1500
    for: 6h
    annotations: { summary: ">1.5 KB/doc：比对 mapping 与唯一 term 数增长曲线" }
```

指标之间的因果链值得单独记一句：**`store.size/doc` 先涨（段落盘）→ `terms_memory` 后涨（词典进堆）→ `query_cache.evictions` 再涨（缓存装不下）→ 最后 `search.slowlog` 爆**。前三步任何一步报警，都有几小时到几天的窗口，第四步只剩几十分钟。

---

## 21.11 一页速记

| 问题 | 答案 |
| --- | --- |
| 倒排由哪几件物理结构组成 | 词典（`.tip` FST + `.tim` block）+ 有序倒排表（`.doc` delta + skip）+ 位置（`.pos`）+ 归一（`.nvd/.nvm`）+ 列存（`.dvd/.dvm`） |
| 为什么快 | 全在「有序」：词典有序 → FST 压缩 + 前缀查询；docID 有序 → delta 位宽 3~9 bit（1.2 B/posting）+ 可跳 + 可线性求交 |
| term 怎么找到 doc | FST 找索引项（µs 以下）→ seek `.tim` 解 block（几 µs）→ 拿 TermState → 建 postings 迭代器 → 跳表求交（0.5 ns/posting） |
| 体积怎么算 | `posting 数 × 1.2 B`（docs+freqs）；开 positions × 2.6；开 offsets 再 ×1.15；1.2 亿×180 term → 26 GB / 69 GB / 79 GB |
| 词典多大 | 唯一 term 数 × 40 B；FST 堆 = 唯一 term 数 × 1.6 B。**与文档数无关**，只与词汇量有关 |
| 第 ⑦ 跳为什么是唯一线性项 | 其余十跳都是 µs 级常数，只有「遍历 posting」= df × 0.5 ns；df=3,000 万 → 15 ms |
| 跳表省多少 | 求交时对选择性高的一侧：3,003 万次推进 → 384 万（省 87%）；两侧都稠密时收益趋近 0 |
| 什么时候退化成位图 | 命中密度 > 5~10% 的 filter：位图 AND 0.09 ms vs postings 25 ms（270 倍） |
| 排序聚合凭什么不炸堆 | doc_values（`.dvd/.dvm`）列存 + mmap 堆外；text 没有 doc_values，强开 `fielddata` 就 OOM |
| 相关性「变怪」先查什么 | `norms` 是否被关（长度归一）；分析链两侧是否一致；`df` 分布是否变 |
| 一条纪律 | **`dynamic: strict` + 显式 `index_options` + 非排序条件全进 `filter` + 高基数字段不进倒排** |

---

## 21.12 自检题

**Q1**：词典里只有 3,000 万个 term，为什么 `_nodes/stats` 里的 `terms_memory` 只要几十 MB，而 `.tim` 文件却有 1.2 GB？这两者的关系是什么？

<details>
<summary>看答案</summary>

`.tim` 存的是**全部 term 的字节 + 每个 term 的倒排指针 + docFreq 等元数据**（3e7 × 40 B ≈ 1.2 GB），走 mmap，只在需要时读某个 block，属于**堆外、按需**。

`.tip` 里的 FST 只存**每个 block 的首 term**（一个索引项覆盖约 32~128 个 term），每项约 50 B，并且用前缀/后缀共享 + pointer 差分压缩 → 94 万项 × 50 B ≈ 47 MB，且**段打开时整块读进堆**（跳转太频繁，mmap page fault 不划算）。

所以关系是：`FST 堆 ≈ (term 数 / 32) × 50 B ≈ term 数 × 1.6 B`。这也是「term index 常数级内存」的来源——它随**词汇量**线性，而不随文档数线性。

</details>

**Q2**：一个 term 的 df 从 300 万涨到 3,000 万，查询延迟从 2 ms 变成 18 ms，但 CPU、IO、堆三项面板都没变化。为什么？该怎么治？

<details>
<summary>看答案</summary>

第 ⑦ 跳是唯一线性项：posting 解码 0.5 ns/posting，300 万 → 1.5 ms，3,000 万 → 15 ms。这部分是**纯 CPU 的解码 + 比较**，量小到不会在任何一张面板上形成尖峰（15 ms × 100 QPS = 1.5 核），但足以把 P99 从 3 ms 推到 20 ms 以上。`df` 变化来自数据（新上了一批带这个词的商品），不是代码。

治法按顺序：① 把不参与排序的条件搬进 `filter`（走位图缓存，0.09 ms 级）；② 用时间窗/类目收窄候选，让第 ⑦ 跳的有效 df 变小；③ `track_total_hits: false` 省掉全量遍历；④ 若该 term 本身没意义（如停用词/品牌泛词），从分析链里去掉。

</details>

**Q3**：为什么 `index_options: positions` 让体积从 26 GB 涨到 69 GB（2.6 倍），而不是「多存一个位置字段」的小增量？

<details>
<summary>看答案</summary>

因为数量的量级不同：posting 是**每个 (term, doc) 一条**，位置是**每次出现一个**。1.2 亿 doc × 180 term = 216 亿 posting；而位置数 = posting 数 × 平均每次 doc 内出现次数（2.2）≈ 475 亿个位置。

按 0.9 B/位置算：475e8 × 0.9 = 42.8 GB，叠加在 26 GB 之上就是 68.8 GB。**「多一列」的直觉在这里失效**：倒排的每一列都是「按出现次数」而不是「按文档数」计数的。

</details>

**Q4**：某字段既要做 `match_phrase` 又要做 `terms` 聚合，怎么建才不炸堆？

<details>
<summary>看答案</summary>

一个字段两种物理形态，拆成两个子字段：

- 主字段 `type: text` + `analyzer: ik_max_word` + `index_options: positions`（提供 `.pos` 给短语与高亮）；
- 子字段 `keyword`（`ignore_above: 256`）+ `doc_values: true`，聚合/排序走它（`.dvd/.dvm`，mmap 堆外）。

代价是双份存储（约 +25%~50%），换来的是「聚合永不进堆」。**绝不能**对 `text` 开 `fielddata`：那是把整列反转进堆，20 GB 起步，是 ES 最经典的 OOM 路径。

</details>

**Q5**：如果只用 `keyword` 字段、不用任何 `text` 字段，倒排索引还有必要存在吗？为什么 ES 不能干脆只保留 doc_values？

<details>
<summary>看答案</summary>

有必要，但工作量会小很多。`keyword` 字段的 `term`/`terms` 过滤默认仍走 **postings**（`.tim` + `.doc`）：一次 `terms` 查询要拿多个 term 的 docID 集合并起来，postings 是天然有序的 docID 流，求并/求交是 O(Σdf) 一趟归并；而 doc_values 是按 **docID 顺序**存的，要回答「哪些 doc 的该字段等于 X」只能**全列扫描**（O(N)，1.2 亿次比较）。

所以两者方向相反：postings 是「值 → doc」（查找用），doc_values 是「doc → 值」（排序聚合用）。稠密且可缓存的 filter 会退化成位图（§21.4 第三条路线），但**位图也是从 postings 那一侧构建出来的**。少一个都做不到另一件事。

</details>

---

## 21.13 延伸阅读（锚点式）

- 段为什么不可变、refresh/flush/merge 的代价、删除标记何时物理消失：`→ comp/22`
- 一次查询如何扇出到分片、副本怎么选、`_routing` 与分片数定错的后果：`→ comp/23`
- query vs filter 两条执行路径、filter cache 命中率、BM25 的三个直觉、深分页：`→ comp/24`
- 容量与故障总账：mapping 爆炸、堆预算、段数与零停机重建：`→ comp/25`、`→ architect/17 §17.4`、`→ architect/17 §17.5`
- 搜索系统的架构边界（什么时候不该用 ES、写路径三种形态、对账节奏）：`→ architect/17 §17.3`、`→ architect/17 §17.6`、`→ eng/37 §37.4.4`
- 三套索引的分层融合（精确前缀 / 倒排 / 语义），以及为什么倒排之后还需要向量：`→ architect/16`、`→ comp/26`
- 带标量过滤的 ANN：过滤越严，图越容易走进死路（倒排与向量的交接处）：`→ comp/27`
- RAG 里「检索」这一跳的工程形态（分块、混合检索、重排）：`→ llm/20`
- 存储形态决策树：为什么 ES 永远是派生索引而不是主库：`→ eng/35`、`→ architect/17 §17.2`
- 全系列总纲与 21/22 的分工（21 讲物理结构，22 讲段与近实时）：`→ 0-summary.md §0.6`
