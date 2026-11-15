# 44. RAG 管线：从文档到上下文

> **状态**：`已完成（初稿）` ｜ **所属**：第七部分 · AI-Agent 应用架构 ｜ **编号**：44 / 54
> **本章定位**：RAG 不是"装个向量库就完事"，它是一条有 9 个环节的**数据管线**，每一环都能单独把效果打到地板。这一章从切分讲到评估，所有代码都能落到 Go 服务里。
> **读法**：先看 44.2 的管线图和那张「失败模式表」，把它当成排查手册；44.3 按你当前卡的环节跳读。前置知识：第 26 章（有界并发）、第 31 章（JSON 与结构化输出）、第 40 章（LLM 客户端）。

---

## 44.1 引子：三个"明明是 RAG，就是答不对"的事故

事故一（切分）：财务知识库里有一张 12 行的费率表，切分器按 800 字符硬切，把表头切到了上一块、把「阶梯区间」切到了下一块。用户问"50 万以上费率多少"，检索回来的块里只有一排孤零零的数字，模型很自信地编了个 8%。

事故二（检索）：客服系统里用户输入 "ORD20241108001" 这种单号，向量检索返回一堆"看起来很像订单问题描述"的文档，一条都不含这个单号。因为**向量检索天然不擅长精确 token 匹配**——embedding 把字符串压成了一个模糊的点。

事故三（权限）：多租户 SaaS 里，检索 SQL 忘了 `WHERE tenant_id = $1`，在应用层用 Go 过滤。结果召回 top10 里 8 条是别人的，过滤完只剩 2 条，回答质量断崖式下跌；更恐怖的是某天有人重构时把那行过滤删了——直接数据越权。

这三个事故对应管线里的三个环节：**切分、混合检索、过滤下推**。记住这句话：**RAG 的效果上限由数据决定，下限由这一章的顺序决定。**

---

## 44.2 直觉模型：一条管线，两条路径

```
                     ┌──────────────── 离线索引（build time）────────────────┐
   PDF/HTML/MD ──► ①解析 ──► ②切分 ──► ③embedding(批量并发) ──► ④入库(去重/版本)
                     │                │                              │
                     └ 表格/代码块单独处理 └ chunk 元数据(tenant/doc/pos)└ pgvector/qdrant/milvus
                                                                          │
                     ┌──────────────── 在线检索（request time）────────────┤
   user query ──► ⑤改写/扩写 ──► ⑥混合召回(向量+BM25) ──► ⑦融合 RRF ──► ⑧rerank
                                                                          │
                                             ⑨上下文拼装(预算/引用/去重) ◄─┘
                                                        │
                                                        ▼
                                              prompt → LLM → 答案(带引用)
```

每一格的失败长什么样，比原理更值得背：

| 环节 | 典型失败现象 | 根因 | 见 |
| --- | --- | --- | --- |
| 解析 | 表格全乱、PDF 公式变乱码 | 用纯文本抽取器啃二进制 PDF；没做版面还原 | 44.3.1 |
| 切分 | 答案就在库里，但召回块"缺前半句" | 固定长度硬切，语义单元被切断；overlap 太小 | 44.3.1 |
| embedding | 索引跑到一半 429 / 账单炸 | 无并发上限、无批次合并、没按 token 维度批 | 44.3.2 |
| 入库 | 文档更新后检索出两个版本 | 无 content_hash 去重、无版本字段 | 44.3.7 |
| 召回 | 型号/单号/人名查不到 | 只做稠密向量，不做稀疏/关键词 | 44.3.4 |
| 融合 | 两路结果各占一半，互相挤掉 | 用分数线性加权（分数量纲不同） | 44.3.4 |
| rerank | top10 里对的排第 9 | 直接拿 top-k 喂模型，没交叉编码重排 | 44.3.5 |
| 拼装 | 模型说"资料里没有" | 块塞太多导致中间信息被稀释；无引用编号 | 44.3.6 |
| 过滤 | 越权 / 召回变少 | 过滤写在应用层而不是 SQL | 44.3.8 |
| 评估 | 每次调参靠感觉 | 没有 golden set 和 recall@k | 44.3.9 |

---

## 44.3 底层实现

### 44.3.1 解析与切分：先把"块"这个数据结构定下来

切分的产物不是 `[]string`，而是一个带谱系的结构体。**元数据决定了你后面能不能过滤、能不能引用、能不能增量更新**：

```go
type Chunk struct {
	ID        string    // = tenant + doc_id + content_hash 的短哈希，天然幂等
	TenantID  string    // 过滤维度，必须下推到库里
	DocID     string
	Source    string    // s3://... 或原始 URL，用于展示引用
	Title     string    // Markdown 标题路径 "费率表 > 阶梯定价"
	Position  int       // 在文档中的序号，便于拼回上下文
	Kind      string    // text / table / code —— 表格必须单独成块
	Content   string
	Tokens    int       // 用 tiktoken 估算，切分时就要算，别等到拼 prompt
	Hash      string    // sha256(规范化的 Content)
	Version   int       // 文档版本，用于"全量替换某文档"
	UpdatedAt time.Time
	Score     float32 // 检索/重排分数，只在查询路径上填
}
```

三条实战规则，比任何 splitter 参数都重要：

1. **结构优先，长度其次**。先按 Markdown 标题 / HTML 层级 / 段落切"语义单元"，只有当单元超过 `maxTokens` 时才在单元内部按句子递归切。`textsplitter.RecursiveCharacter` 这类"递归字符分割"就是这个思路的简化版。
2. **表格、代码块、公式永不跨块**。它们自带完整语义，切一半等于丢掉。表格建议转成"每行一句话"的形式（`区间=50万以上; 费率=6%`）再入库，embedding 效果会明显好于原始 markdown 表格。
3. **overlap 用来救"跨块指代"**，一般取 chunk 的 10%~15%；overlap 太大只会让同一个事实在 top-k 里占多个名额（去重还得靠 44.3.6）。

### 44.3.2 embedding 批处理与并发：可跑的最小实现

embedding 是 RAG 里唯一"批量调用比单条便宜一个数量级"的环节，也是最容易写出 429 的地方。要点只有四条：**按 token 打包成批、并发有上限、单批失败要重试、结果按原始下标回填**。

```go
package main

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"golang.org/x/sync/errgroup"
)

// Embedder 与 langchaingo 的 embeddings.Embedder 同形，方便替换真实实现
type Embedder interface {
	EmbedDocuments(ctx context.Context, texts []string) ([][]float32, error)
}

// 假实现：把文本长度映射成一个 4 维向量，保证 go run 能跑通
type fakeEmbedder struct{}

func (fakeEmbedder) EmbedDocuments(_ context.Context, texts []string) ([][]float32, error) {
	out := make([][]float32, len(texts))
	for i, t := range texts {
		out[i] = []float32{float32(len(t)), float32(len(strings.Fields(t))), 1, 0}
	}
	return out, nil
}

// BatchEmbed 把 texts 按"每批至多 maxBytes 字节 / maxItems 条"切分，
// 用至多 concurrency 个 goroutine 并发调用，返回与入参严格对齐的结果。
func BatchEmbed(ctx context.Context, e Embedder, texts []string, maxItems, concurrency int) ([][]float32, error) {
	type batch struct{ start, end int }
	var batches []batch
	for i := 0; i < len(texts); i += maxItems {
		end := min(i+maxItems, len(texts))
		batches = append(batches, batch{i, end})
	}

	vecs := make([][]float32, len(texts)) // 预分配，按位置回填 → 顺序天然正确
	g, ctx := errgroup.WithContext(ctx)
	g.SetLimit(concurrency) // 有界并发：不要用裸 go 循环，1000 块能瞬间打爆配额

	for _, b := range batches {
		g.Go(func() error {
			var lastErr error
			for attempt := 0; attempt < 4; attempt++ { // 429/5xx 指数退避
				got, err := e.EmbedDocuments(ctx, texts[b.start:b.end])
				if err == nil {
					if len(got) != b.end-b.start {
						return fmt.Errorf("embed: got %d vectors, want %d", len(got), b.end-b.start)
					}
					copy(vecs[b.start:b.end], got) // 只写自己那一段，无锁无竞态
					return nil
				}
				lastErr = err
				select {
				case <-ctx.Done():
					return ctx.Err()
				case <-time.After(time.Duration(1<<attempt) * 200 * time.Millisecond):
				}
			}
			return fmt.Errorf("embed batch [%d,%d): %w", b.start, b.end, lastErr)
		})
	}
	if err := g.Wait(); err != nil {
		return nil, err
	}
	return vecs, nil
}

func main() {
	texts := make([]string, 37)
	for i := range texts {
		texts[i] = fmt.Sprintf("第 %d 段文档内容，关于阶梯费率与折扣", i)
	}
	vecs, err := BatchEmbed(context.Background(), fakeEmbedder{}, texts, 10, 4)
	fmt.Println(len(vecs), err) // 37 <nil>
}

// Java 对照：这等价于 CompletableFuture.allOf() + 固定线程池 + 重试装饰器，
// 但 Go 里没有"池"这个概念 —— errgroup.SetLimit 就是那个池，且 ctx 一取消全部退出。
```

> ⚠️ 三个隐性坑：① **批次大小要按 token 算**，不是按条数——一条 30k token 的块单独就会超限；② 有的供应商单请求最多 2048 条输入，有的按总 token 计费，先查文档；③ **别把 embedding 写进 HTTP 请求的 goroutine 里**，用独立 worker + 队列，否则大文档导入会把在线查询的并发槽吃干。

### 44.3.3 三个向量库怎么接

先看 pgvector：如果你已经有 Postgres，**它就是默认答案**——事务、备份、权限、SQL 过滤全都是现成的。

```go
package store

import (
	"context"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/pgvector/pgvector-go"
	pgxvec "github.com/pgvector/pgvector-go/pgx"
)

const ddl = `
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS chunks (
  id          text PRIMARY KEY,
  tenant_id   text NOT NULL,
  doc_id      text NOT NULL,
  content     text NOT NULL,
  title       text,
  kind        text,
  version     int  NOT NULL DEFAULT 1,
  metadata    jsonb NOT NULL DEFAULT '{}',
  embedding   vector(1024) NOT NULL,
  tsv         tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chunks_vec ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_tsv ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_tenant ON chunks (tenant_id, doc_id);`

// pgvector v0.4+：pgx v5 必须注册类型，否则 vector 列读不出来
func NewPool(ctx context.Context, dsn string) (*pgxpool.Pool, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, err
	}
	cfg.AfterConnect = func(ctx context.Context, c *pgx.Conn) error {
		return pgxvec.RegisterTypes(ctx, c)
	}
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, err
	}
	_, err = pool.Exec(ctx, ddl)
	return pool, err
}

func Upsert(ctx context.Context, pool *pgxpool.Pool, c Chunk, vec []float32) error {
	_, err := pool.Exec(ctx, `
INSERT INTO chunks (id, tenant_id, doc_id, content, title, kind, version, embedding)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
ON CONFLICT (id) DO UPDATE SET content = EXCLUDED.content,
  embedding = EXCLUDED.embedding, version = EXCLUDED.version, updated_at = now()`,
		c.ID, c.TenantID, c.DocID, c.Content, c.Title, c.Kind, c.Version,
		pgvector.NewVector(vec))
	return err
}

// 距离算子：<-> L2，<#> 负内积，<=> 余弦距离（推荐，向量已归一化时等价内积）
func Search(ctx context.Context, pool *pgxpool.Pool, tenant string, q []float32, k int) ([]Chunk, error) {
	rows, err := pool.Query(ctx, `
SELECT id, doc_id, content, 1 - (embedding <=> $1) AS score
FROM chunks WHERE tenant_id = $2
ORDER BY embedding <=> $1 LIMIT $3`, pgvector.NewVector(q), tenant, k)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Chunk
	for rows.Next() {
		var c Chunk
		var score float64
		if err := rows.Scan(&c.ID, &c.DocID, &c.Content, &score); err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}
```

三个库的选型，看的是"规模 × 过滤复杂度 × 谁来运维"：

| 维度 | pgvector | qdrant | milvus |
| --- | --- | --- | --- |
| 部署成本 | 复用现有 PG，几乎为零 | 单二进制/容器，低 | 依赖 etcd/对象存储，重 |
| 适合规模 | 百万级向量以内 | 千万级 | 亿级 |
| 索引 | HNSW / IVFFlat | HNSW（可调 ef、量化） | IVF/HNSW/DiskANN 等多种 |
| 元数据过滤 | 就是 SQL，最灵活 | payload filter，能力好 | 标量字段过滤，表达式语法 |
| 事务/一致性 | 与业务同库同事务 ✅ | 最终一致 | 最终一致 |
| 混合检索 | `tsvector` + RRF 一把梭 ✅ | 有 sparse vector 支持 | 支持 sparse + dense |
| 痛处 | 大表 + 高并发下 PG 压力大；过滤多时召回掉点 | 需独立运维、Go client API 迭代较快 | 概念多、调参复杂 |

> 💡 qdrant 的 Go client（`github.com/qdrant/go-client/qdrant`）在 1.13 之后做了大改（`qdrant.NewClient(&qdrant.Config{...})` + `NewVectors/NewQuery/NewID` 这类构造器），旧教程里的写法多半编译不过。**以 `go doc` 与官方仓库 example 为准**，别抄博客。milvus 同理，`milvus-sdk-go/v2` 的 `client.NewGrpcClient` 与 `NewClient` 在不同小版本上行为不同。

### 44.3.4 向量 + 关键词混合检索：RRF 是唯一不用调参的融合法

稠密向量擅长"语义相似"，稀疏（BM25/tsvector）擅长"精确 token"。两路都召回，再把结果融合。**不要线性加权分数**——余弦相似度是 `[-1,1]`，BM25 是 `[0, +∞)`，量纲不同，权重怎么调都是玄学。

用倒数排名融合（RRF）：`score = Σ 1/(k + rank_i)`，`k` 常取 60，只看名次不看分数：

```go
// 一条 SQL 搞定双路召回 + RRF 融合（Postgres 侧，零额外依赖）
const hybridSQL = `
WITH vec AS (
  SELECT id, row_number() OVER (ORDER BY embedding <=> $1) AS rnk
  FROM chunks WHERE tenant_id = $2 ORDER BY embedding <=> $1 LIMIT $3
), kw AS (
  SELECT id, row_number() OVER (
           ORDER BY ts_rank_cd(tsv, websearch_to_tsquery('simple', $4)) DESC) AS rnk
  FROM chunks
  WHERE tenant_id = $2 AND tsv @@ websearch_to_tsquery('simple', $4)
  LIMIT $3
), fused AS (
  SELECT id, SUM(1.0 / (60 + rnk)) AS rrf FROM (
    SELECT id, rnk FROM vec UNION ALL SELECT id, rnk FROM kw
  ) u GROUP BY id
)
SELECT c.id, c.doc_id, c.title, c.content, f.rrf
FROM fused f JOIN chunks c USING (id)
ORDER BY f.rrf DESC LIMIT $5`

func HybridSearch(ctx context.Context, pool *pgxpool.Pool, tenant, query string, qv []float32,
	candidates, topN int) ([]Chunk, error) {
	rows, err := pool.Query(ctx, hybridSQL,
		pgvector.NewVector(qv), tenant, candidates, query, topN)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Chunk
	for rows.Next() {
		var c Chunk
		var rrf float64
		if err := rows.Scan(&c.ID, &c.DocID, &c.Title, &c.Content, &rrf); err != nil {
			return nil, err
		}
		c.Score = float32(rrf)
		out = append(out, c)
	}
	return out, rows.Err()
}
```

`'simple'` 分词器对中文不友好（它按空格切）。中文场景换成 `zhparser` 或 `pg_bigm` 扩展，或者把稀疏路交给 Elasticsearch/OpenSearch，Go 侧再用同样公式做 RRF——**融合逻辑在哪个进程无所谓，公式不能改**。

### 44.3.5 rerank：把"召回的 50 条"变成"靠谱的 8 条"

召回阶段（bi-encoder）是 query 和 doc 分别编码再算距离——快，但精度有限。rerank 阶段用交叉编码器（cross-encoder）把 `(query, doc)` 拼在一起打分，慢 10~100 倍，但只需要跑 30~50 条，完全可接受。

```go
// 接口自己定，实现可以是 Cohere Rerank、BGE-reranker（TEI 部署）、或任何 HTTP 服务
type Reranker interface {
	// 返回按相关性降序排列的下标，以及对应分数
	Rerank(ctx context.Context, query string, docs []string, topN int) ([]int, []float32, error)
}

type RerankStage struct{ R Reranker }

func (s RerankStage) Do(ctx context.Context, query string, in []Chunk, topN int) ([]Chunk, error) {
	if len(in) == 0 {
		return nil, nil
	}
	docs := make([]string, len(in))
	for i := range in {
		docs[i] = in[i].Title + "\n" + in[i].Content // 标题拼进去，能明显提升工业场景的排序
	}
	idx, scores, err := s.R.Rerank(ctx, query, docs, topN)
	if err != nil {
		return in[:min(topN, len(in))], nil // 降级：宁可用召回顺序，也别让主链路挂掉
	}
	out := make([]Chunk, 0, len(idx))
	for i, pos := range idx {
		c := in[pos]
		c.Score = scores[i]
		out = append(out, c)
	}
	return out, nil
}
```

> 🩸 **事故**：rerank 服务挂了直接 return err，导致整个问答不可用。**rerank 永远是可降级的装饰器，不是主链路的必需环节**——降级方式见第 47 章的"熔断降级"。

### 44.3.6 上下文拼装与窗口控制：预算分配表

拼上下文是"分蛋糕"。模型窗口 128k 不代表你要塞 128k：**成本、延迟、以及"中间遗忘"（lost in the middle）都随长度恶化**。

| 预算项 | 建议占比 | 说明 |
| --- | --- | --- |
| system prompt + 规则 | 5%~10% | 固化前缀，利于服务端 prompt cache（第 47 章） |
| 对话历史（窗口内） | 15%~25% | 保留最近 N 轮 + 摘要，详见第 45 章 |
| 检索块（rerank 后） | 40%~60% | 常见 5~12 块，单块 300~800 token |
| 输出预留 | 10%~20% | `max_tokens` 要预留，别让答案被截断 |
| 安全冗余 | 5% | 计数器误差 + 工具结果回灌 |

拼装三条纪律：

1. **给每块编号并强制模型引用**：`[1] ... [2] ...`，答案要求 `...见[2]`。这既提高可核查性，也让评估能自动算引用命中率。
2. **去重 + 相邻合并**：同一 doc 的相邻块按 `Position` 合并成一段再算 token，避免"同一事实占三个名额"。
3. **头部放最重要的块**，尾部放次重要；中间放长尾。这是对 lost-in-the-middle 最朴素的对抗。

### 44.3.7 增量索引与去重：id 是内容哈希，不是自增

- `ID = sha256(tenant|doc|规范化content)[:16]` → 内容没变就幂等，重复导入天然去重。
- 文档更新用 `Version`：先 `UPDATE chunks SET version=$v` 插入新块，再 `DELETE FROM chunks WHERE doc_id=$d AND version < $v`。跨块的"先删后插"会有一瞬间检索不到，用事务包起来即可。
- 文档删除要有**墓碑**：软删 + 定时物理清理，否则缓存与向量库会长期不一致。
- 大批量重建索引时用独立 collection/表 + `ALTER TABLE ... RENAME` 切换，别一边重建一边对外服务。

### 44.3.8 多租户权限过滤：下推到 SQL，且要接受"召回变少"

三层防御，缺一不可：

1. **连接层**：给检索用的 DB 角色只授予视图权限，视图里强制拼 `tenant_id = current_setting('app.tenant_id')`。Postgres RLS（`CREATE POLICY ... USING (tenant_id = current_setting('app.tenant_id')::text)`）是最省心的兜底。
2. **SQL 层**：`tenant_id` 永远是第一个 WHERE 条件。
3. **应用层断言**：结果集里出现非本租户数据 → 直接 panic/告警（这是"不该发生"的事件，值得中断）。

> ⚠️ **过滤会让 HNSW 少返回**：ANN 索引先近似找 top-k 再过滤，filter 选择性高时可能只回来 1~2 条。pgvector 0.8+ 支持迭代扫描（`SET LOCAL hnsw.iterative_scan = relaxed_order;`），或者在"高选择性过滤"场景直接退回**先过滤后精确排序**（`SET LOCAL enable_indexscan = off` 的小表策略）。别在压测里发现"过滤后 recall 掉了"，那其实是设计问题。

### 44.3.9 评估：没有 golden set 的 RAG 调参等于算命

最小可用评估只需要三样：**一批真实问题 + 每个问题的正确答案片段 ID + 两个指标**。

```go
// Recall@k：正确片段有没有被召回；MRR：正确片段排得多靠前
type GoldenCase struct {
	Query      string
	GoldIDs    []string // 标注的正确 chunk id（可多个）
	Tenant     string
}

func Eval(ctx context.Context, search func(context.Context, string, string, int) ([]string, error),
	cases []GoldenCase, k int) (recall, mrr float64) {
	var hit, sumRR float64
	for _, c := range cases {
		got, err := search(ctx, c.Tenant, c.Query, k)
		if err != nil {
			continue
		}
		rank := 0
		for i, id := range got {
			if slices.Contains(c.GoldIDs, id) {
				rank = i + 1
				break
			}
		}
		if rank > 0 {
			hit++
			sumRR += 1.0 / float64(rank)
		}
	}
	n := float64(len(cases))
	return hit / n, sumRR / n
}
```

配合三个工程约束：① **检索评估和生成评估分开做**——答案不对先看 recall，recall 高才轮到 prompt 背锅；② 生成侧用 LLM-as-judge 打分（忠实度/相关性/引用正确率），但**每周人工抽检 30 条校准 judge**，否则 judge 会漂；③ 每次改切分/模型/参数，跑同一份 golden set 看指标，别用"感觉更好了"上线。

---

## 44.4 Java 对照

| 你脑子里的 Java 方案 | Go 里的现实 | 会猜错的地方 |
| --- | --- | --- |
| JDBC + Hibernate 存文档 | pgx + 手写 SQL / sqlc | pgvector 的 `vector` 类型**必须注册类型**（`pgxvec.RegisterTypes`），否则报 "unsupported type"，这个坑 Hibernate 帮你干了 |
| `ExecutorService` + `CompletableFuture.allOf` 批量 | `errgroup.SetLimit` + 预分配结果切片按位回填 | Go 没有 `Future` 的 join 顺序保证；**靠下标回填而不是 channel 收集**才不需要额外排序 |
| Spring 的 `@Transactional` 包住"删旧插新" | `pgx.Tx` 显式 Begin/Commit，或 `pool.Begin` | Go 没有声明式事务，事务边界要自己画；忘记 Rollback 会漏连接（defer tx.Rollback） |
| Elasticsearch + Spring Data 做混合检索 | SQL 里 `tsvector` + RRF，或 ES + Go client | 别在应用层做分数加权融合，量纲不同 |
| 结果集过滤靠 Service 层 if | 过滤下推 SQL / RLS | 多租户下这是安全边界，不是代码风格 |

Go 在这个场景真正的优势：**整条管线可以塞进一个静态二进制里跑**——worker 进程既解析、又调 embedding、又写库、又对外提供 HTTP 查询，部署面只有一个（第 52 章）。

---

## 44.5 框架与工程落地

- **eino-ext**：`github.com/cloudwego/eino-ext` 下有 `components/embedding/openai`、`components/indexer/*`、`components/retriever/*`，可以把"索引"和"检索"做成图节点，和 Agent 编排（第 43 章）拼在一张图里，流式传播由 eino 负责。
- **langchaingo**：`textsplitter`（`NewRecursiveCharacter`）+ `documentloaders` + `vectorstores`（`AddDocuments` / `SimilaritySearch`）+ `chains/retrieval` + `chains/combine_documents`（`stuff` / `refine` / `map_reduce`）是它给的"标准答案"。注意它的接口偏"同步阻塞"，生产里要自己在外面套并发与超时（第 48 章精读）。
- **自研建议**：把管线拆成 `Parser / Splitter / Embedder / Index / Retriever / Reranker / Assembler` 七个小接口，全部在 `main` 里显式装配（第 36 章）。RAG 参数调得比代码多，只有显式装配才方便按租户配不同策略。

---

## 44.6 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| `unsupported type vector` | pgx 没注册 pgvector 类型 | `pgxvec.RegisterTypes` 放进 `AfterConnect` | 44.3.3 |
| 导入 100 页 PDF 时服务超时 | embedding 与在线请求共享 goroutine 无限并发 | `errgroup.SetLimit` + 独立 worker 队列 | 44.3.2 |
| 向量结果顺序与入参错位 | 用 channel 收集结果，完成顺序即返回顺序 | 预分配切片 + 按 index 回填 | 44.3.2 |
| 同一事实在 top-k 出现 3 次 | overlap 过大 + 无 doc 级去重 | 相邻块合并 + `Position` 去重 | 44.3.6 |
| 单号/型号搜不到 | 只做稠密检索 | 加 tsvector/BM25 双路 + RRF | 44.3.4 |
| 更新文档后旧内容还能被召回 | 无版本字段、无删除 | `Version` + 事务替换 + 墓碑 | 44.3.7 |
| 过滤后 top-k 只剩 2 条 | ANN 先取 k 再过滤 | 迭代扫描 / 先过滤后排序 | 44.3.8 |
| 答案编造，且无法核查 | 拼装没编号、没要求引用 | `[n]` 编号 + 强制引用 + 引用命中率评估 | 44.3.6 |
| 调参全靠感觉 | 无 golden set | recall@k / MRR + judge 抽检 | 44.3.9 |

---

## 44.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 切分 | 先结构后长度；表格/代码块不跨块；overlap 10~15% |
| chunk 元数据 | tenant/doc/position/hash/version 少一个都不行 |
| embedding 并发 | 按 token 分批 + `errgroup.SetLimit` + 按 index 回填 + 退避 |
| pgvector | 必须 `pgxvec.RegisterTypes`；`<=>` 余弦；HNSW 建索引 |
| 三库选型 | 有 PG 用 pgvector，千万级上 qdrant，亿级再说 milvus |
| 混合检索 | 向量 + 关键词双路召回，RRF(`k=60`) 融合，别加权分数 |
| rerank | cross-encoder 重排 30~50 条；**必须可降级** |
| 窗口控制 | 预算分蛋糕：system 10% / 历史 20% / 检索 50% / 输出 15% |
| 去重 | id = 内容哈希；版本号做增量替换 |
| 多租户 | 过滤下推 SQL + RLS 兜底 + 应用层断言 |
| 评估 | recall@k + MRR 管检索，judge 管生成，每周人工校准 |

---

## 44.8 自检题

1. **用户说"答案就在知识库里，但 RAG 答不出来"。你按什么顺序排查？**
   > 要点：先看解析产物（表格/PDF 是否乱）→ 再看检索：用 golden set 量 recall@k，用关键词路验证"能否召回"→ recall 正常才看拼装（块是否被截断、引用是否缺失、长度是否稀释）→ 最后才怪模型。**顺序错了会花三天调 prompt。**

2. **为什么混合检索推荐 RRF 而不是 `0.7*vector + 0.3*bm25` 这种加权？**
   > 要点：两路分数量纲不同（余弦有界、BM25 无界），权重随语料漂移；RRF 只看排名，无需归一化与调参，工程上更稳。加权只有在你能把两路分数都归一化到可比区间时才考虑。

3. **多租户 RAG 里，把 `tenant_id` 过滤从 SQL 挪到应用层会有什么后果？**
   > 要点：① 召回 top-k 被其他租户数据占满，有效结果变少，回答质量下降；② 一旦某次重构漏删过滤就是数据越权（安全事故），安全边界必须由数据库兜（RLS/视图）；③ 应用层过滤无法走索引下推，浪费 IO。

---

## 44.9 延伸阅读

- pgvector 官方 README 与 `pgvector-go` 仓库 examples：`hybrid-search`（RRF）、`loading`（COPY 批量导入）
- `github.com/pgvector/pgvector-go/pgx` 的 `RegisterTypes`（pgx v5 类型注册）
- `textsplitter.RecursiveCharacter`（langchaingo）与 eino-ext 的 `indexer` / `retriever` 组件
- 论文：*Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods*（RRF 原始论文）、*Lost in the Middle*（长上下文中间遗忘）
- 下一章：[45. 记忆与会话：状态一致性](45-memory-session.md)

---

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[43. Agent 循环与编排引擎](43-agent-loop-orchestration.md)；第 26 章（有界并发）、第 31 章（结构化输出）
- 下一章：[45. 记忆与会话：状态一致性](45-memory-session.md)
