# 45. 存储架构：会话、事件、向量与对象

> **状态**：`已完成（初稿）` ｜ **所属**：第九部分 · 平台工程底座 ｜ **编号**：45 / 52
> **本章定位**：Agent 平台的数据不是「一张订单表」，而是**七种形态、三种温度、四种生命周期**混在一起。选错库不会立刻炸，会在第 90 天炸；分区选错不会立刻慢，会在第 60 天慢到不可用。
> **读法**：先看 §45.2 的容量算式（这是本章的账本），再读 §45.4 的分区 DDL。**如果你只能记住一件事：能用 `DROP PARTITION` 解决的，绝不用 `DELETE`。**

---

## 45.1 引子：第 97 天，一张表把平台拖死了

你把事件表放在 Postgres 里，理由是「我们先小规模跑，简单点」。

这个决定在第 1 天是对的。第 97 天，它是这样的：

| 指标 | 第 1 天 | 第 97 天 | 变化 |
| --- | --- | --- | --- |
| 行数 | 36 万 | **349 亿** | ×97,000 |
| 表 + 索引体积 | 1.2 GB | **42 TB** | ×35,000 |
| 「查这个 Run 的全部事件」 | 8 ms | **14 s** | ×1,750 |
| 「算昨天各租户成功率」 | 0.4 s | **超时（> 300s）** | 不可用 |
| 一次 autovacuum | 无感 | **IO 打满 40 分钟** | 全站 P99 3.4s → 11s |
| 想加一个索引 | 2 s | **6 小时 20 分**（还要锁） | 不敢做 |

更糟的是三件事连锁发生：

1. **11:00 的 autovacuum** 把 IO 打满 → 影响的不只是查询，还有 `runs` 表的写入 → 新 Run 创建变慢 → API 层堆积 → 全站告警；
2. 你想「删掉 90 天前的数据」缓解，写了 `DELETE FROM events WHERE ts < now() - interval '90 days'`，这条 SQL 跑了 **4 小时**，产生 **2.1 TB WAL**，主从延迟飙到 18 分钟，最后你不得不 kill 它——而 kill 之后回滚又花了 50 分钟；
3. 由于主从延迟，读副本上出现了「Run 已结束但事件还没到」的**倒挂**，审计接口报「该 Run 不存在」，客诉 60 单。

> 🩸 **这一章的核心命题**：
> Agent 平台的存储不是「选一个数据库」，而是**给七类数据各配一个家，并让它们之间的边界清晰到不需要跨库事务**。
> 一个数据库装所有东西，前 3 个月最省事，第 4 个月开始用十倍人力还债。

---

## 45.2 五类数据、七个家：选型的第一原则是「一件事只有一个家」

先把数据分类。分类的标准不是「技术」，是**访问模式**：谁在读、怎么读、读多少、留多久。

| 数据类 | 典型内容 | 访问模式 | 量级（Aurora） | 该放哪 | 不该放哪 |
| --- | --- | --- | --- | --- | --- |
| **① 会话与 Run 状态** | session、run、step、checkpoint、待审批 | 按主键点查、强一致、事务 | 3,000 万 Run/天；热 7 天 420 GB | **Postgres**（分区 + 归档） | 不要放 CH（无事务、点查差） |
| **② 事件与 trace** | run.created / llm.call / tool.call / audit | 追加写、按 run 聚合、按时间范围扫描 | 3.6 亿行/天 | **ClickHouse**（明细）+ **S3**（全量 payload） | 不要放 Postgres（见引子） |
| **③ 缓存与计数** | 会话快照、限流计数、语义缓存、租约 | 极高 QPS、KV、可丢 | 峰值 20 万 ops/s | **Redis** | 不要当唯一真相（会丢） |
| **④ 事件流与队列** | 待执行 Run、事件总线、延迟队列、DLQ | 顺序、分区、可回放 | 峰值 15 MB/s | **Kafka**（或 NATS JetStream） | 不要当数据库（保留期短） |
| **⑤ 向量与检索** | 知识库 chunk、记忆向量、语义索引 | ANN + payload 过滤 + 租户下推 | 6 亿向量 | **pgvector**（小）→ **Qdrant/Milvus**（大） | 不要放 Redis（除超小规模） |
| **⑥ 对象与大块** | 附件、工具大结果、流式分片、归档、模型产物 | 顺序读写、按 key 取、极少改 | 1.5 TB/天新增 | **S3 兼容对象存储** | 不要放数据库（TOAST 会拖死） |
| **⑦ 图与关系** | 实体关系、知识图谱、组织层级 | 多跳遍历 | 通常 < 1,000 万边 | **Postgres 递归 CTE** → 必要时 **Neo4j** | 不要为了「图」而上图库 |

三条选型原则，按重要性排序：

1. **一件事只有一个家（single source of truth）**。`runs` 表是 Run 状态的唯一真相，ClickHouse 里的 `run_events` 只是**可重建的投影**。如果两边都能改，你必然遇到「以谁为准」的灵魂拷问。
2. **跨库不做事务，做补偿**。Postgres 写 `run` 成功、Kafka 投递失败怎么办？用 Outbox（同库事务里写 outbox 表，CDC 投递），而不是分布式事务（第 13 章）。
3. **热数据靠库，冷数据靠对象存储**。90 天前的 trace，99.7% 的请求永远不会查；把它留在 ClickHouse 里，你付的是 SSD 的钱；挪到 S3 + Parquet，你付的是 1/40 的钱。

### 容量估算：先算清楚，再选型

```
【事件表：日增】
  日均 Run            = 3,000 万
  每 Run 落库事件      = 12 条（run.created / step ×3 / llm.call ×3 / tool.call ×5 / output / audit）
  日均行数            = 3,000万 × 12 = 3.6 亿行
  平均事件大小         = 1.2 KB（JSON，含 payload）
  日均原始字节         = 3.6亿 × 1.2 KB = 432 GB/天
  列存压缩比（ZSTD）    ≈ 8:1  →  54 GB/天
  90 天保留（压缩后）   = 54 GB × 90 = 4.86 TB

【对象存储：全量归档】
  原始 432 GB/天，zstd 6:1  →  72 GB/天
  一年累积             = 72 GB × 365 = 26.3 TB
  成本（$0.023/GB·月，以云厂商官网为准）
    年末月存           = 26,300 GB × $0.023 ≈ $605/月
    转 IA（$0.0125）    ≈ $329/月，读一次再收 $0.01/GB

【热数据：Postgres】
  Run 元数据 2 KB/Run × 3,000万 × 7 天热窗口 = 420 GB
  索引膨胀 1.3×       → 546 GB
  主 + 2 副本          → 1.64 TB SSD

【流式分片：不进数据库】
  60 批/Run × 800 B = 48 KB/Run × 3,000万 = 1.44 TB/天
  只落对象存储 30 天    = 43 TB  →  43,000 × $0.023 ≈ $990/月
```

> 💡 这四组数字会直接决定你的选型：**事件 432 GB/天，等于每天新增一张 42 TB 表的 1%（引子）**。所以「事件进 Postgres」在第 97 天必然死，不是运气问题，是算术问题。

---

## 45.3 结构图：写入路径与三层温度

```
  API / Worker
      │ ① 事务内：写 runs / steps（强一致，Postgres）
      │ ② 事务内：写 outbox 表（同库，保证不丢）
      ▼
  ┌─────────────── Postgres（热）───────────────┐
  │ sessions / runs / steps / checkpoints        │  保留 7~30 天
  │ outbox / quota_ledger / api_keys             │  分区：RANGE(created_at) + HASH(org_id)
  └──────────────┬───────────────────────────────┘
                 │ ③ CDC（Debezium / 自研 binlog 消费）
                 ▼
  ┌─────────────── Kafka（管道）───────────────┐
  │ topic: run.events      保留 3 天（可回放）  │
  │ topic: run.events.dlq  保留 14 天           │
  └───┬───────────────────────────────┬─────────┘
      │ ④ 批量写（500ms / 5000 行）     │ ⑤ 归档写（Parquet，每小时）
      ▼                                ▼
  ┌──── ClickHouse（温）────┐      ┌──── S3（冷）─────────────┐
  │ run_events  MergeTree   │      │ events/date=/hour=/…parquet│
  │ 按天分区 + TTL 90 天     │      │ stream_chunks/ 30 天 TTL   │
  │ ORDER BY (org_id,run_id,│      │ attachments/ 永久           │
  │           seq)          │      │ archive/ Glacier 7 年       │
  └────────┬────────────────┘      └────────────────────────────┘
           │ ⑥ 查询：控制台 / 审计 / 评测 / 计费对账
           ▼
      BI 与看板（按 org_id / release_id / 时间切片）

  旁路：
    Redis  ← 会话快照、限流计数、语义缓存（可丢，重建）
    向量库 ← 知识库 chunk（从 PG + S3 重建，索引版本化）
```

三个必须画在图上的边界：

| 边界 | 规则 | 为什么 |
| --- | --- | --- |
| Postgres ↔ Kafka | 只能通过 **outbox + CDC**，不许应用双写 | 双写必不一致（一个成功一个失败） |
| Kafka ↔ ClickHouse | **至少一次**，CH 侧用 `ReplacingMergeTree` 做幂等去重 | 重复消费是常态，不是异常 |
| ClickHouse ↔ S3 | CH 只存「查询必需的列」，大 payload 存 S3，用 `s3()` 表函数按需拉 | CH 的存储很贵，S3 便宜 40 倍 |

---

## 45.4 关键设计

### （1）Postgres 表结构：分区键必须是「查询里一定有的那个」

```sql
-- 会话与 Run：按 created_at 月分区，子分区按 org_id 哈希
CREATE TABLE runs (
    org_id      text        NOT NULL,
    run_id      uuid        NOT NULL,
    prj_id      text        NOT NULL,
    session_id  uuid        NOT NULL,
    release_id  text        NOT NULL,     -- 第 42 章：五个版本号的封装
    state       text        NOT NULL,     -- queued/running/waiting_approval/done/failed
    input_tokens  int       NOT NULL DEFAULT 0,
    output_tokens int       NOT NULL DEFAULT 0,
    cost_micro    bigint    NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL,
    finished_at timestamptz,
    PRIMARY KEY (org_id, created_at, run_id)   -- 分区键必须在 PK 里
) PARTITION BY RANGE (created_at);

CREATE TABLE runs_2026_03 PARTITION OF runs
    FOR VALUES FROM ('2026-03-01') TO ('2026-04-01')
    PARTITION BY HASH (org_id);
CREATE TABLE runs_2026_03_p00 PARTITION OF runs_2026_03
    FOR VALUES WITH (MODULUS 32, REMAINDER 0);

-- 关键索引只有两个：按 Run 点查、按会话翻页
CREATE INDEX ON runs_2026_03 (org_id, run_id);
CREATE INDEX ON runs_2026_03 (org_id, session_id, created_at DESC);
-- 状态扫描用部分索引，只索引「进行中」的 0.1%
CREATE INDEX ON runs_2026_03 (state, created_at) WHERE state IN ('queued','running');
```

**索引预算：热表最多 3 个。** 每多一个索引，写入 IOPS ×1.5~2（见 §45.4(3) 写放大）。`org_id` 在每一个索引的第一列——这是「租户贯穿」在存储层的落地（第 44 章）。

### （2）ClickHouse：分区要「粗」，排序键要「准」

```sql
CREATE TABLE run_events (
    org_id       LowCardinality(String),
    run_id       UUID,
    seq          UInt32,
    ts           DateTime64(3, 'UTC'),
    kind         LowCardinality(String),   -- llm.call / tool.call / audit …
    release_id   LowCardinality(String),
    model        LowCardinality(String),
    in_tokens    UInt32,
    out_tokens   UInt32,
    cost_micro   UInt64,
    latency_ms   UInt32,
    payload_ref  String,                   -- S3 key，不存正文
    payload      String CODEC(ZSTD(3))     -- 只存小的关键字段
)
ENGINE = ReplacingMergeTree(ingested_at)   -- 用版本列做幂等去重
PARTITION BY toYYYYMMDD(ts)                -- 天分区：便于 TTL 与 DROP
ORDER BY (org_id, run_id, seq)             -- 主键即排序键：点查与聚合都快
TTL ts + INTERVAL 90 DAY DELETE
SETTINGS index_granularity = 8192;

-- 二级跳数索引：给「按 kind 过滤 + 时间范围」的看板查询
ALTER TABLE run_events ADD INDEX idx_kind kind TYPE set(16) GRANULARITY 4;

-- 计费对账用的物化视图（按小时预聚合，把 3.6 亿行压成 24 万行/天）
CREATE MATERIALIZED VIEW run_cost_hourly
ENGINE = SummingMergeTree
PARTITION BY toYYYYMM(ts_hour)
ORDER BY (org_id, ts_hour, release_id, model)
AS SELECT org_id, toStartOfHour(ts) AS ts_hour, release_id, model,
          sum(in_tokens) AS in_tokens, sum(out_tokens) AS out_tokens,
          sum(cost_micro) AS cost_micro
   FROM run_events GROUP BY org_id, ts_hour, release_id, model;
```

分区策略的取舍，一句话：**分区数要控制在几千以内**。按天分区（一年 365 个）合适；按小时分区（8,760 个）会让 `MergeTree` 的后台合并线程忙不过来，查询元数据也变慢。

| 策略 | 分区数/年 | TTL / 删除 | 查询裁剪 | 适用 |
| --- | --- | --- | --- | --- |
| `toYYYYMM` 月 | 12 | 粗（最小删一个月） | 差（一次扫一月） | 量小（< 1 亿行/月） |
| **`toYYYYMMDD` 天** | **365** | **精确到天，`DROP PARTITION` 200ms** | **好** | **推荐（本章）** |
| `toYYYYMMDDhh` 小时 | 8,760 | 最精确 | 最好 | 只有超高频（> 10 亿行/天）才值 |
| 按 `org_id` 分区 | 10 万 | 灾难 | 灾难 | ❌ 绝不要 |

> 🩸 最后一行是很多人第一反应会犯的错：「按租户分区，删除租户就 `DROP PARTITION` 多方便」。10 万个分区，ClickHouse 的元数据和后台合并会直接崩。**租户隔离靠 `ORDER BY` 前缀 + `WHERE org_id` 下推，不靠分区。**

### （3）写放大：一条事件到底写了几次

这是最容易被忽略的成本，也是「为什么我的库 IOPS 一直不够」的标准答案：

| 环节 | 写次数 | 说明 |
| --- | --- | --- |
| Postgres heap | 1 | 行本身 |
| 主键索引 | 1 | B-tree 插入 |
| 二级索引 × 2 | 2 | 每个索引一次 B-tree 插入 |
| WAL | 1 | 预写日志（还要 fsync） |
| 流复制到 standby | 1 | 同步复制时还要等 |
| ClickHouse 一份 | 1 | 分析副本 |
| S3 归档 | 1 | 冷备 |
| **合计** | **8×** | 而 UPDATE 是 **12×**（旧版本 + 新版本 + 索引双写） |

```
写入 IOPS 需求 = 事件速率 × 写放大
              = (3.6 亿行/天 ÷ 86,400) × 8
              = 4,167 行/s × 8 ≈ 33,000 IOPS（持续）
峰值 ×3      ≈ 100,000 IOPS
```

结论：**索引数是最便宜的优化点**。把 5 个索引砍到 3 个，写放大人 8× 降到 6×，等于白得 25% 的写入能力。所以「先加索引再说」是反模式——**索引是读的贷款，用写的利息还**。

### （4）保留期与温度：把 90% 的冷数据赶到便宜的地方

| 数据 | 热（天） | 温 | 冷 | 谁在读 | 年成本占比 |
| --- | --- | --- | --- | --- | --- |
| `runs` / `sessions` | 7 | 30（压缩） | 无（归档到 S3） | 在线业务 | 12% |
| `run_events` 明细 | 7 | 90（CH） | 1 年（S3 Parquet） | 排障 / 审计 / 评测 | 22% |
| 流式分片 | 0（直接 S3） | 30 | — | 重放 / 撤回 | 9% |
| 审计日志（哈希链） | 30 | 1 年（CH） | **7 年（WORM / Glacier）** | 合规 / 取证 | 18% |
| 计费流水（账本） | 90 | 2 年 | **10 年**（不可变） | 对账 / 税务 | 6% |
| 附件 / 工具大结果 | 30 | 90 | 1 年 | 业务 | 14% |
| 向量（知识库） | 常驻内存 | — | — | 检索 | 15% |
| 备份快照 | 7 天日备 + 4 周周备 | — | 12 个月月备 | 灾备 | 4% |

**热冷分层的钱**（同一份 42 TB 数据的三种放法）：

| 放法 | 单价 | 42 TB 月成本 | 查询延迟 |
| --- | --- | --- | --- |
| Postgres SSD（含副本） | ~$0.20/GB·月 | $8,400 | 8 ms |
| ClickHouse NVMe（3 副本） | ~$0.10/GB·月 | $4,200 | 50~300 ms |
| S3 Standard | $0.023/GB·月 | $966 | 200 ms~数秒（要拉取） |
| S3 Glacier IR | $0.004/GB·月 | $168 | 秒级~分钟级 |
| Glacier Deep Archive | $0.00099/GB·月 | $42 | 小时级 |

> 💡 42 TB 在 Postgres 里 $8,400/月，在 S3 里 $966/月，**差 8.7 倍**。而 99.7% 的查询只碰最近 7 天。**热冷分层不是优化，是唯一能让成本曲线不爆炸的手段。**

### （5）分区维护：用 `DROP` 不用 `DELETE`

```go
// 每天 03:00 跑：预建未来 7 天分区 + 删除过期分区
func (m *PartitionManager) Rotate(ctx context.Context, now time.Time) error {
	// ① 预建（必须提前，否则插入会失败——分区不存在时 PG 直接报错）
	for i := 0; i < 7; i++ {
		d := now.AddDate(0, 0, i)
		if err := m.ensurePartition(ctx, "run_events", d); err != nil {
			return err
		}
	}
	// ② 过期分区：DROP 是元数据操作，200ms；DELETE 是 4 小时 + WAL 爆炸
	expired, _ := m.listPartitionsOlderThan(ctx, "run_events", now.AddDate(0, 0, -90))
	for _, p := range expired {
		// 先 DETACH（可回退），确认后再 DROP（不可回退）
		if err := m.detach(ctx, p); err != nil {
			return err
		}
		m.audit.Record(ctx, "partition.detached", p) // 第 31 章：可审计
	}
	return nil
}
```

| 操作 | 1 亿行耗时 | WAL | 可回退 | 对主从延迟影响 |
| --- | --- | --- | --- | --- |
| `DELETE FROM … WHERE ts < X` | **4 小时** | 2.1 TB | 事务内可 | 18 分钟 |
| `DROP TABLE partition` | **200 ms** | 可忽略 | ❌ | 无 |
| `DETACH` → 观察 → `DROP` | 200 ms + 观察期 | 可忽略 | ✅（DETACH 后可再 ATTACH） | 无 |

### （6）向量库：什么时候 pgvector 就不够了

| 规模（向量总数） | 选型 | 内存估算（1536 维 float32） | 理由 |
| --- | --- | --- | --- |
| < 500 万 | **pgvector**（HNSW） | 500万 × 6 KB × 1.3 ≈ **39 GB** | 和业务表同库，事务一致，运维零成本 |
| 500 万 ~ 2 亿 | **Qdrant** | 1 亿 × 6 KB × 1.3 ≈ **780 GB**（量化后 ~200 GB） | payload 索引下推、分片、int8 量化、按租户 partition |
| > 2 亿 | **Milvus** | 存算分离，磁盘型索引 | 需要独立扩缩容、多索引类型、十亿级 |

算式：`内存 ≈ N × (维度 × 4 字节 + HNSW 图开销 ~30%)`。1 亿向量在 1536 维下就是 **780 GB 内存**——这就是为什么大租户才上 HNSW 全内存，长尾租户走 pgvector + IVFFlat（磁盘友好）或量化。

> ⚠️ 无论选哪个，**租户过滤必须下推**（第 44 章）：Qdrant 用 payload 索引或 partition，pgvector 用 `WHERE org_id = $1` 的复合索引。**「查 top_k 再过滤」等于既慢又越权。**

### （7）图：最后一个才考虑

| 需求 | 先试 | 什么时候才上 Neo4j |
| --- | --- | --- |
| 组织层级、项目归属 | Postgres 递归 CTE（深度 ≤ 10） | 永远不需要 |
| 实体关系（人-公司-订单） | Postgres 两张表 + 复合索引 | 需要 3 跳以上遍历且高频 |
| 知识图谱推理 | Postgres + 邻接表 + 物化路径 | 需要变长路径查询 + 图算法（PageRank/社区发现） |

**上 Neo4j 的代价**：多一套集群、多一套备份、多一套权限模型，且**它没有事务跨库**——你的 `org_id` 隔离要在两个库里各实现一遍。所以判据很简单：**「用 SQL 写不出来的查询，出现了每周 > 100 次」，才值得引入图库。**

---

## 45.5 Java / 分布式经验对照

| 你熟悉的 | AgentOS 对应 | 同构点 | 会咬人的地方 |
| --- | --- | --- | --- |
| MySQL 分库分表（ShardingSphere） | 分区 + 分片 | 路由键决定一切 | 分区是**同库内**的；分片是**跨库**的。先分区，真不够再分片 |
| B+ 树索引 | Postgres 索引 | 联合索引最左前缀 | 分区表的索引是**每个分区一个**，全局唯一约束要带分区键 |
| InnoDB 的 redo/undo | WAL + MVCC | 写前日志 | Postgres 的 UPDATE 是「新版本 + 旧版本」，长事务会让 autovacuum 追不上（引子的第 5 行） |
| LSM（HBase/RocksDB） | ClickHouse MergeTree | 追加写、后台合并 | CH 的 `ReplacingMergeTree` 去重是**最终**的，查询要 `FINAL` 或 `argMax`，别假设实时唯一 |
| Kafka 保留策略 | 事件流 | 按时间/大小删除 | Kafka 保留 3 天是**管道**，不是存储；7 天以上的回放需求要 S3 |
| Elasticsearch 做日志检索 | ClickHouse | 列存 + 聚合 | ES 全文检索强、聚合弱且贵；CH 聚合强、全文弱。别用 CH 做日志全文搜索 |
| MyBatis 流式查询 `ResultHandler` | 游标 / 分页 | 避免 OOM | 一次 `SELECT` 拉 1,000 万行必 OOM；用 keyset 分页（`WHERE (org_id, ts) > ($1,$2)`） |
| 定时任务清理历史数据 | 分区轮转 | 都是定时 | `DELETE` 在 MySQL 里也慢，但 PG 的 MVCC 让它**更**慢（要 vacuum） |
| Redis 当缓存 | 会话/限流/语义缓存 | 一样 | Agent 的语义缓存有**误命中**风险（第 24 章），Redis 本身不解决 |

---

## 45.6 开源坐标

| 项目 | 它解决什么 | 缺什么 |
| --- | --- | --- |
| **Postgres**（+ `pg_partman`、RLS） | 强一致状态、分区自动化、租户隔离 | 不适合 > 10 亿行的高频追加；`pg_partman` 需自己运维 |
| **ClickHouse** | 事件/trace 分析、TTL、物化视图预聚合 | 无事务、点查弱、`ReplacingMergeTree` 去重是最终一致 |
| **Redis**（+ RedisJSON/RediSearch） | 会话、限流（`CL.THROTTLE`）、语义缓存 | 不是真相源；持久化不等于备份 |
| **Kafka** | 事件总线、可回放、Outbox 下游 | 不是数据库；保留期短；消费者要自己做幂等 |
| **pgvector / Qdrant / Milvus** | 向量检索 | 租户过滤要下推；量化会掉召回；索引重建成本高 |
| **S3 / MinIO** | 对象、归档、Parquet 湖 | 没有事务；列举操作贵；生命周期规则要设计好 |
| **Neo4j** | 图遍历、图算法 | 运维重、生态独立、跨库无事务 |
| **Langfuse 的存储组合** | PG（trace 元数据）+ ClickHouse（观测）的典型组合 | 是「可观测」的选型，不含计费账本与审计 WORM |

> 这块最值钱的一条经验来自 Langfuse 这类系统：**「元数据进 PG，明细进 CH，大块进对象存储」** 是被验证过的三层组合。你要加的只有两样：**计费账本（不可变、强一致）** 和 **审计日志（WORM、哈希链）**，它们都不能放 CH。

---

## 45.7 事故清单

- [ ] **事故 A：事件表进 Postgres，第 97 天拖死全站（本章引子）。**
  现象：349 亿行 / 42 TB，autovacuum 打满 IO，全站 P99 ×3。根因：追加型明细数据放进了 OLTP 库。兜底：明细迁 ClickHouse + payload 下沉 S3 + PG 只留 7 天热状态。
- [ ] **事故 B：`DELETE` 删历史数据，主从延迟 18 分钟。**
  现象：一条清理 SQL 跑 4 小时，WAL 2.1 TB。根因：用 `DELETE` 代替分区 `DROP`。兜底：分区轮转 + `DETACH` 观察 + `DROP`；永久禁止在生产写不带分区条件的 `DELETE`。
- [ ] **事故 C：分区没预建，凌晨 0 点插入全失败。**
  现象：`no partition of relation found for row`，新 Run 全部 500。根因：分区创建是手动/月度任务，跨月没跑。兜底：提前 7 天预建 + 插入失败自动建分区的兜底触发器 + 分区存在性告警。
- [ ] **事故 D：ClickHouse 去重假设失效，账单翻倍。**
  现象：Kafka 重复消费导致同一条 `llm.call` 被计费两次。根因：`ReplacingMergeTree` 是最终去重，未合并前查询会看到重复。兜底：计费**不走 CH**，走 PG 的账本（第 27 章）；CH 只做分析与对账参考。
- [ ] **事故 E：索引加太多，写入 IOPS 打满。**
  现象：为 5 个看板查询各加一个索引，写入 P99 从 8ms → 40ms。根因：没算写放大（5 索引 → 8× 变 10×）。兜底：热表索引 ≤ 3 个；慢查询走 CH 或物化视图，不靠加索引。
- [ ] **事故 F：按 `org_id` 给 CH 分区，元数据爆炸。**
  现象：10 万分区，`MergeTree` 后台合并线程打满，查询规划 3 秒起步。根因：把「隔离」错当成「分区」。兜底：天分区 + `ORDER BY (org_id, …)` 前缀 + `WHERE org_id` 下推。
- [ ] **事故 G：向量库全量重建，检索中断 3 小时。**
  现象：知识库换 embedding 模型，需要重建索引，期间检索降级。兜底：索引版本化（`index_snapshot` 进 release_id，第 42 章）+ 蓝绿 collection 双写 + 切换别名，重建不影响线上。

---

## 45.8 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 第一原则 | 一件事只有一个家；跨库不做事务，做 Outbox + 补偿 |
| 七个家 | PG（状态）/ CH（明细）/ Redis（缓存）/ Kafka（管道）/ 向量库 / S3（大块）/ 图（最后考虑） |
| 容量 | 3.6 亿事件/天 × 1.2 KB = 432 GB/天；CH 压缩 8:1 → 54 GB/天 |
| 成本差 | 同样 42 TB：PG $8,400 / CH $4,200 / S3 $966 / Glacier $42（月） |
| 分区 | **天分区 + TTL 90 天**；分区总数控制在几千；**绝不按 org_id 分区** |
| 排序键 | CH 的 `ORDER BY (org_id, run_id, seq)` 就是「租户下推」的落地 |
| 删除 | `DROP PARTITION` 200ms vs `DELETE` 4 小时 + 2.1 TB WAL；先 `DETACH` 再 `DROP` |
| 写放大 | 一条事件写 8 次（索引 + WAL + 副本 + CH + S3）；UPDATE 是 12 次 |
| 索引预算 | 热表 ≤ 3 个；索引是读的贷款，用写的利息还 |
| 向量 | <500 万 pgvector / 500 万~2 亿 Qdrant / >2 亿 Milvus；1 亿向量 ≈ 780 GB 内存 |
| 图 | 递归 CTE 写不出来的查询每周 >100 次，才值得上 Neo4j |
| 一句话 | **存储不会在某天突然坏掉，它会在第 97 天按算术规律坏掉。** |

## 45.9 自检题

1. **你要设计事件表。请给出分区键、排序键、TTL，并说明为什么不用 `org_id` 做分区键。**
   > 要点：① 分区键 `toYYYYMMDD(ts)`（CH）/ `RANGE(created_at)` + `HASH(org_id)` 子分区（PG）；② 排序键 `(org_id, run_id, seq)`，因为点查是「按 Run 取全部事件」，聚合是「按租户+时间」，两者都能用上前缀；③ TTL 90 天 + S3 Parquet 归档 1 年；④ 不用 `org_id` 分区：10 万分区会让 CH 元数据与后台合并崩溃，也会让 PG 的分区规划变慢；租户隔离靠 `ORDER BY` 前缀 + `WHERE org_id` 下推 + RLS，不靠分区。

2. **一条事件写了几次？如果要提升 30% 写入能力，你会先动哪里？**
   > 要点：① 8 次：heap、主键索引、2 个二级索引、WAL、流复制、CH 一份、S3 归档；UPDATE 是 12 次；② 先砍索引（5 → 3，写放大 10× → 8×），再看是否需要同步复制（改异步，接受 RPO），最后才是加机器；③ 加机器是最后手段，因为写放大是乘数，砍乘数比加基数便宜。

3. **「我们把 90 天前的数据删了，用 `DELETE`，为什么不行？」**
   > 要点：① 1 亿行 `DELETE` = 4 小时 + 2.1 TB WAL + 主从延迟 18 分钟 + 回滚再 50 分钟；② PG 的 MVCC 让删除变成「标记 + 等 vacuum」，autovacuum 会和你抢 IO；③ 正确做法是分区 `DETACH`（可回退）→ 观察 24h → `DROP`（200ms）；④ 还要有「预建未来分区」的机制，否则删完下个月插入全失败。

## 45.10 延伸阅读

- 上一章：[44. 多租户与资源治理](44-multi-tenant.md)（`org_id` 为什么是分区键与 RLS 的核心）
- 下一章：[46. 部署、容量与冷启动](46-deployment-capacity.md)（有状态 Worker 的数据亲和）
- 总纲：[0-summary.md](0-summary.md)（§1「文件系统 → 会话库/事件库/向量库/对象存储」）
- 相关：[6. Session/Run/Step/Event 状态模型](6-session-run-event-model.md)（事件溯源，决定表结构）、[12. 长任务与持久化执行](12-long-task-durable.md)（checkpoint 存哪）、[13. 事件驱动与异步管道](13-event-driven-pipeline.md)（Kafka 与 Outbox）、[27. 计费的可靠性](27-billing-reliability.md)（账本为什么不能放 CH）、[31. 合规与审计](31-compliance-audit.md)（WORM 与 7 年保留）、[41. 离线数据消费与回流](41-offline-data-loop.md)（数仓与 Parquet 湖）
- 外部：Postgres 声明式分区与 RLS 官方文档、ClickHouse `MergeTree` / `TTL` / `ReplacingMergeTree` 文档、pgvector 与 Qdrant 多租户文档、S3 存储类定价页（成本数字以官网为准）
- 语言细节：[../go/31-json-codec.md](../go/31-json-codec.md)（事件 payload 的编解码开销）、[../go/28-gc.md](../go/28-gc.md)（大结果集导致的内存压力）
