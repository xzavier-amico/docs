# 27. 多层对账引擎与差错闭环：四方核对、实时双流、亿级批处理与自动修复

> **状态**：生产级工程篇｜**所属**：第三部分·金融工程代码基座｜**编号**：27 / 48
>
> **本章定位**：承接 [22 复式记账核心账本引擎（上）](22-double-entry-ledger-schema-and-concurrency.md)、[23 复式记账核心账本引擎（下）](23-hot-accounts-reversals-and-fx-ledger.md) 的不可变分录、分桶与冲正模型，以及 [24 全链路幂等](24-end-to-end-financial-idempotency.md)、[25 状态机与事务 Outbox](25-state-machines-tcc-saga-and-outbox.md)、[26 清结算与不停机日切](26-clearing-settlement-netting-and-cutoff.md) 的业务身份、事件和会计日边界，解决“账面、渠道、银行、链上到底谁对谁错，以及怎样安全地收敛”的问题。
>
> **覆盖范围**：内部总账与子账、支付/托管渠道、银行流水、链上观测四方对账；实时事件流与文件/快照流双流校验；文件 Staging 与原子发布；亿级记录的分桶并发批处理；ClickHouse 反连接与聚合 SQL；跨日滚动挂账；差错分类、证据链和自动修复；PostgreSQL/ClickHouse DDL、Go 1.24/Java 21 核心代码、测试压测、极端边界、术语、面试答题卡和自检题。
>
> **不可妥协的红线**：对账是发现和证明差异，不是把余额改成“看起来正确”；原始文件、原始事件、账本分录和链上证据不可变；所有修复必须是带审批策略、业务幂等键、反向分录/状态机动作的追加事实；`UNKNOWN` 不能被超时直接当作失败；金额用最小单位整数并携带币种；同一批次只能有一个可发布版本；没有水位、截止时间和快照块高的“已平”没有审计意义。

---

## 27.1 引子：四套系统都说成功，次日却多出一笔钱

### 27.1.1 典型事故

一个交易平台通过 PSP 扣款 100 USD，PSP 先返回成功；内部账本落了一笔客户入账，银行 T+1 文件中也有 100 USD。然而链上托管热钱包只看到 99.98 USDC 到账，因为代付方扣了 0.02 USDC 手续费。后来 PSP 重发同一回执，回调消费者因没有对账事实唯一键又入账一次；银行文件因上传中断只包含了半个文件，文件名和日期都正确，值班人员仍然把“行数差不多”当成可用。

| 证据 | 表面状态 | 真正风险 |
|---|---|---|
| 内部账本 | 客户应收 +100，渠道应收 +100 | 重复回调可能产生第二条经济事实 |
| 渠道回执流 | `SUCCESS`, `channel_tx_id=C1` | 回调可能重复、乱序、缺字段 |
| 银行流水 | +100，文件校验未完成 | 半文件会制造大面积“银行缺失” |
| 链上索引 | +99.98 USDC，区块已确认 | 费用、转账拆分和确认块高没有统一语义 |

如果只查询订单最终状态，会得到“成功”；如果只看一方余额，会得到“差 0.02”；如果直接把内部余额加 0.02，又会掩盖渠道手续费没有入账、链上转账与费用未拆分、以及文件不完整等根因。生产对账必须保留**证据的原样、证据的时间、证据的身份、证据的确认等级和证据之间的映射**。

### 27.1.2 唯一真问题

> 在事件重复、文件迟到或半传、渠道未知态、银行跨日入账、链重组、时钟偏差、数据库重试和批任务崩溃的情况下，系统能否证明每一笔可对账事实只被计数一次，正确地区分暂时未到与真实差错，并在不直接改余额的前提下自动收敛或安全升级？

对账不是一个 `JOIN`，而是一条有边界的证据管线：

```text
采集原始证据
  → 身份/签名/文件完整性校验
  → 规范化（但保留 raw）
  → 事件幂等去重与版本排序
  → 实时 provisional 对拍
  → 截止点 snapshot/final 对拍
  → 差异分类与挂账
  → 安全自动修复或人工队列
  → 修复后重对账、审计和报表
```

### 27.1.3 反模式与替代方案

| 反模式 | 事故后果 | 生产替代 |
|---|---|---|
| `SELECT SUM(internal) = SUM(bank)` 就算平 | 总额相等，单笔漏记和另一笔重复互相抵消 | 行级身份 + 金额/币种/状态 + 维度总额三级校验 |
| 按文件名覆盖导入 | 同名补传覆盖证据，无法知道版本 | `source_file_id + sha256` 不可变，版本化发布 |
| 事件流到达即最终确认 | 迟到事件、退款、链重组被误报 | provisional/final 两阶段状态与水位 |
| 发现差异直接 `UPDATE balance` | 账本与余额投影分离，重放再次漂移 | 追加冲正、补记或状态机补偿分录 |
| 只用 `transaction_id` 匹配 | 渠道交易号跨商户/重用，或者链上拆分转账 | 规范化复合键、映射表、版本和作用域 |
| ClickHouse 查询无限 `JOIN` 全表 | 内存爆、扫描雪崩、重复匹配 | 按业务日/币种/桶分区，预聚合和反连接 |
| 凌晨批任务失败就重跑整天 | 二次入账、锁住在线交易 | 批次 lease、分桶 checkpoint、可重入发布 |

---

## 27.2 四方模型：不是四张表互相硬连，而是一个规范化事实空间

### 27.2.1 四个来源的职责

四方并不意味着四套系统共享主键，更不意味着“银行是永远唯一真相”。每个来源对自己的事实负责，最终通过**对账映射层**放进同一个规范化事实空间。

| 来源 | 代表事实 | 可信边界 | 常见缺陷 |
|---|---|---|---|
| **内部账** `INTERNAL` | 不可变凭证、分录、余额投影、业务状态 | 平台“承认并记账了什么” | 可能重复消费、错误映射、投影落后 |
| **渠道** `CHANNEL` | PSP/卡组/券商/托管商回执、结算文件 | 外部服务“接受/完成了什么” | 回调重复、未知态、批量结算、字段不统一 |
| **银行** `BANK` | Nostro/结算账户流水、余额、价值日期 | 银行账户“实际记了什么” | T+1、价值日与入账日不同、手续费和汇兑拆分 |
| **链上** `CHAIN` | 交易、日志、UTXO、确认块、reorg 证据 | 链上快照“在指定最终性下观察到什么” | pending、重组、代币假日志、gas/手续费、分片延迟 |

内部账通常是平台面向客户责任的主记账事实；银行和链上是外部资产/现金位置证据；渠道是业务委托与结算责任证据。**最终状态是规则对指定范围的合取，不是“谁的总额最大就听谁的”。**

### 27.2.2 Canonical Reconciliation Fact（CRF）

所有输入先映射为 CRF。原始来源永远保留，规范化字段可按映射版本重建：

```text
ReconciliationFact {
  fact_id             // 平台生成的不可变事实身份
  source               // INTERNAL | CHANNEL | BANK | CHAIN
  source_record_id     // 来源作用域内的原始身份
  operation_id         // 业务经济意图，可为空但必须进入异常队列
  relation_id          // 跨来源映射关系；不是任一来源的裸 ID
  legal_entity         // 法律实体/账套
  account_scope        // 客户、平台、Nostro、钱包、托管户
  asset, amount_minor  // 金额最小单位，禁止浮点
  direction            // CREDIT / DEBIT
  business_type        // CAPTURE, REFUND, FEE, TRANSFER, GAS ...
  event_time_utc       // 事实发生时间
  value_date           // 银行价值日/会计日
  observed_at_utc      // 平台看到证据的时间
  finality              // PROVISIONAL | SETTLED | FINAL | REORGED
  source_version       // 文件版本/事件序号/链块哈希
  raw_ref, payload_hash
}
```

金额比较前先统一 `asset`、精度、方向和费用口径。比如链上 `100 USDC` 与内部客户 `100 USD` 不是天然可比；必须经过明确的资产映射、汇率/兑换事实和手续费分录。对账键也必须带作用域：

```text
match_key = H(
  legal_entity | account_scope | asset | business_type |
  canonical_operation_id | occurrence_version
)
```

`source_record_id` 解决同源幂等，`relation_id` 解决跨源关联，二者不可混为一个 `UNIQUE` 字段。

### 27.2.3 对账层次与不变量

一个可审计的批次至少通过以下四层：

1. **接收层**：文件/事件的哈希、签名、行数、字节数、分片范围、时间窗和来源水位完整。
2. **事实层**：源内去重，金额非负且方向有效，币种/资产在字典中，时间能映射到会计日。
3. **匹配层**：按行或可解释的聚合键配对，允许一对多/多对一，但映射必须保存。
4. **总额层**：匹配后各方按 `(legal_entity, value_date, asset, account_scope, bucket)` 试算平衡，差额与费用、汇兑、在途、挂账一致。

关键不变量：

```text
1. 同一个 (source, source_record_id, source_version) 只能产生一个有效规范化事实。
2. 一个经济事实可以有多条证据，但一个来源记录不能匹配两个互斥经济事实。
3. 同币种、同法律实体、同会计日的借贷及来源方向必须按规则平衡。
4. FINAL 只能在水位、文件完整性、最终性和截止策略全部满足后产生。
5. 差异状态不可通过覆盖历史行消失；状态变化必须追加 resolution/audit 事件。
6. 任意自动修复都必须能用相同 repair_key 重放而不产生第二次经济影响。
```

### 27.2.4 四方匹配矩阵

| 内部 | 渠道 | 银行 | 链上 | 解释 |
|---|---|---|---|---|
| 有 | 有 | 有 | 无 | 法币收单/结算中，链上不适用 |
| 有 | 有 | 无 | 无 | 渠道成功但银行未到或文件迟到，通常 `PENDING_EXTERNAL` |
| 有 | 无 | 有 | 无 | 银行先到、渠道回执迟到或渠道漏报，进入 `UNMATCHED_BANK` |
| 有 | 有 | 无 | 有 | 数字资产交付或链上结算，拆解本金、gas、平台费 |
| 无 | 有 | 有 | 无 | 渠道/银行发生平台未记账的外部事实，最高优先级 |
| 有 | 无 | 无 | 有 | 内部已记账但渠道证据缺失，需查出款任务/链索引 |
| 无 | 无 | 无 | 有 | 钱包未知入账、归集或误转，先进入链上暂收，不得自动归客户 |

矩阵不是自动过账授权。匹配引擎先给出 `candidate` 和置信度；只有白名单场景、原始证据充分、金额和资产精确相等、风险规则通过时，才能进入自动修复。

---

## 27.3 实时双流：事件追踪速度，文件/快照负责边界证明

### 27.3.1 为什么必须双流

实时回调或消息流适合低延迟发现差异，却不一定覆盖丢失消息、全量更正和跨日结算；文件/快照适合做完整范围证明，却有分钟到日级延迟。只做流会漏，只有文件会晚。

```text
渠道/银行/链索引器
    ├── Stream：事件、回调、区块增量 → provisional 对拍（秒级）
    └── Snapshot/File：全量/增量文件、余额、确认块 → final 对拍（截止点）
                         ↑
                同一个 CRF、同一个 relation_id、同一个 watermark
```

两个流不能各自维护一套金额事实。流和文件都落到 `source_evidence`，由 `(source, source_record_id, version, payload_hash)` 去重；文件若是更正版本，追加版本，不覆盖旧版本。

### 27.3.2 水位与最终性

每个来源维护独立水位：

```text
stream_watermark = source 已连续处理的事件序号/offset
file_watermark   = 已校验并发布的文件分片最大范围
chain_watermark  = canonical_block_hash + block_height + finality_depth
ledger_watermark = 内部 outbox/entry sequence
```

对账窗口 `[W_start, W_end)` 只有在以下条件都满足后才能从 `PROVISIONAL` 变为 `FINAL`：

- 内部账本水位不低于业务截止序号；
- 渠道/银行文件 manifest 完整、校验通过且超过 grace period；
- 事件流已处理至 `W_end - lateness_budget`，迟到策略已明确；
- 链上达到指定最终性（例如确认块数、finalized epoch 或 L2 challenge 规则）；
- 所有已知跨日、手续费、汇兑和挂账分区都被纳入报表。

迟到事件不能修改已发布批次的历史快照，而应产生 `late_arrival` 子批次，重新计算受影响的差异和报告版本。报告版本必须是单调的，不能让“二次重跑”偷偷改掉原始审计结果。

### 27.3.3 流处理伪代码

```go
// 同一条证据可以重复、乱序、迟到；状态表和唯一键共同保证幂等。
func (c *Consumer) Handle(ctx context.Context, e SourceEvent) error {

	key := EvidenceKey{Source: e.Source, ID: e.RecordID, Version: e.Version}
	hash := sha256.Sum256(e.RawPayload)

	err := c.tx(ctx, func(tx *sql.Tx) error {
		inserted, err := insertEvidence(ctx, tx, key, hash, e.RawPayload)
		if err != nil { return err }
		if !inserted { return nil } // 同一证据重投，不产生第二事实

		fact, err := normalize(e) // 校验资产、方向、精度、时间和签名
		if err != nil {
			return appendQuarantine(ctx, tx, key, err.Error())
		}
		if err := insertCanonicalFact(ctx, tx, fact); err != nil { return err }
		return enqueueMatch(ctx, tx, fact.FactID, "REALTIME")
	})
	if err != nil { return err } // 只有事务提交后才 ACK
	return c.ack(e.Offset)
}
```

不能只在内存中去重：进程崩溃会丢状态；不能只在 Kafka offset 上去重：数据库提交成功后 ACK 丢失会重复消费。数据库唯一约束、事务 Outbox 和消费者可重入共同形成事实边界。

---

## 27.4 文件 Staging：半文件、重传、乱序和恶意内容都不能进入生产事实

### 27.4.1 文件生命周期

```text
RECEIVED
  → HASHING
  → STAGED（对象已落盘，manifest 未发布）
  → VERIFIED（签名/哈希/行数/范围/Schema 通过）
  → LOADED（装入 staging table）
  → RECONCILED（生成对账批次）
  → PUBLISHED（唯一活动版本）
  → SUPERSEDED / QUARANTINED
```

生产库从不直接 `COPY` 到事实表。接收文件先写对象存储临时前缀或不可见版本；上传完成后，供应方提交 manifest：

```json
{
  "file_id": "bank-cn-20261125-0007",
  "source": "BANK",
  "business_date": "2026-11-25",
  "part_count": 16,
  "parts": [{"no": 0, "rows": 1000000, "sha256": "..."}],
  "total_rows": 16000000,
  "total_bytes": 918273645,
  "schema_version": "bank.v4",
  "previous_file_hash": "...",
  "signature": "detached-signature"
}
```

只有每个 part 的 hash、字节数、行数、连续分片号、总量、签名和供应方链式 hash 全部通过，才将 `staged` 原子切换为 `verified`。文件名、目录名和 `mtime` 都不是完整性证明。

### 27.4.2 Staging DDL 与原子发布

```sql
CREATE TYPE recon_file_status AS ENUM
  ('RECEIVED','STAGED','VERIFIED','LOADED','PUBLISHED','SUPERSEDED','QUARANTINED');

CREATE TABLE recon_file (
  file_id              UUID PRIMARY KEY,
  source               TEXT NOT NULL CHECK (source IN ('CHANNEL','BANK','CHAIN')),
  business_date        DATE NOT NULL,
  schema_version       TEXT NOT NULL,
  status               recon_file_status NOT NULL,
  manifest_sha256      BYTEA NOT NULL CHECK (octet_length(manifest_sha256) = 32),
  total_rows           BIGINT NOT NULL CHECK (total_rows >= 0),
  total_bytes          BIGINT NOT NULL CHECK (total_bytes >= 0),
  part_count           INTEGER NOT NULL CHECK (part_count > 0),
  source_sequence      TEXT,
  previous_file_hash   BYTEA,
  received_at          TIMESTAMPTZ NOT NULL,
  verified_at          TIMESTAMPTZ,
  published_at         TIMESTAMPTZ,
  supersedes_file_id   UUID REFERENCES recon_file(file_id),
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source, business_date, manifest_sha256)
);

CREATE TABLE recon_file_part (
  file_id       UUID NOT NULL REFERENCES recon_file(file_id),
  part_no       INTEGER NOT NULL CHECK (part_no >= 0),
  object_uri    TEXT NOT NULL,
  sha256        BYTEA NOT NULL CHECK (octet_length(sha256) = 32),
  row_count     BIGINT NOT NULL CHECK (row_count >= 0),
  byte_count    BIGINT NOT NULL CHECK (byte_count >= 0),
  verified_at   TIMESTAMPTZ,
  PRIMARY KEY (file_id, part_no)
);

CREATE TABLE recon_staging_record (
  file_id          UUID NOT NULL,
  part_no          INTEGER NOT NULL,
  line_no          BIGINT NOT NULL,
  source_record_id TEXT NOT NULL,
  raw_json         JSONB NOT NULL,
  payload_sha256   BYTEA NOT NULL,
  parse_status     TEXT NOT NULL CHECK (parse_status IN ('OK','REJECTED')),
  reject_reason    TEXT,
  PRIMARY KEY (file_id, part_no, line_no),
  UNIQUE (file_id, source_record_id, payload_sha256)
);
```

发布必须在一个短事务中完成：锁定 `(source,business_date)` 的批次行，确认所有 part `verified`、行数总和等于 manifest、解析拒绝数符合策略，然后插入规范化事实并将文件状态置为 `PUBLISHED`。大文件的解析、hash、校验在事务外；事务内只做状态和版本指针切换，避免长事务阻塞日常账本。

对重复文件：manifest hash 相同则返回已有 `file_id`；内容不同但业务日期和来源相同则作为更正版本，必须通过供应方版本/人工策略确认，不能无条件覆盖活动文件。对 `part 0..N-1` 缺洞、行数超限、Schema 字段漂移、压缩炸弹、超大单行和非法 Unicode，进入隔离区，绝不“尽量导入”。

---

## 27.5 亿级分桶批处理：把全表比较变成可恢复的确定性作业

### 27.5.1 分桶键与作业模型

亿级对账不能用一个数据库事务或全局排序。对事实生成稳定桶：

```text
bucket = hash(legal_entity | asset | canonical_relation_or_source_id)
          mod N
```

`N` 例如 4096/16384，需要用历史基数、单桶最大行数、ClickHouse 并发和对象存储吞吐压测后选择；桶数增加后不要改变历史桶算法，否则 checkpoint 和重放边界失效。热点币种/商户可做二级 shard，但必须把算法版本写入批次。

每个 `(recon_run_id, bucket_no)` 是独立幂等单元：

1. 抢租约，带 `lease_token` 和过期时间；
2. 读取窗口内四方 CRF，按键排序或分区聚合；
3. 做一对一、聚合一对多和金额/费用规则匹配；
4. 写入不可变 `recon_match` 和 `recon_break` 临时版本；
5. 校验本桶校验和、行数和差额；
6. checkpoint 成功后释放租约；
7. 所有桶成功且全局水位达标，才发布 run。

任何 worker 崩溃只重跑未完成或租约过期桶。重跑必须用 `(run_id,bucket_no,record_key,algorithm_version)` 唯一键，不能因为同一个桶重跑而重复生成修复任务。

### 27.5.2 Go 分桶协调器

```go
type BucketJob struct {
	RunID, Algorithm string
	Bucket           int
	LeaseToken       string
}

func (w *Worker) Run(ctx context.Context, j BucketJob) error {
	if !w.repo.AcquireLease(ctx, j.RunID, j.Bucket, j.LeaseToken, 2*time.Minute) {
		return nil // 另一个 worker 正在处理；不是失败
	}
	defer w.repo.ReleaseLease(context.Background(), j.RunID, j.Bucket, j.LeaseToken)

	rows, err := w.repo.ReadBucket(ctx, j.RunID, j.Bucket)
	if err != nil { return err }
	// 输入按 canonical_key、source 排序；比较器不依赖到达顺序。
	matches, breaks := MatchSorted(rows, w.rules)

	return w.repo.CommitBucket(ctx, BucketResult{
		RunID: j.RunID, Bucket: j.Bucket, LeaseToken: j.LeaseToken,
		Algorithm: j.Algorithm, Matches: matches, Breaks: breaks,
	}) // 唯一键 + token 防旧 worker 覆盖新结果
}
```

并发度要受数据库连接、ClickHouse merge、对象存储 QPS、下游修复吞吐共同限制。`goroutine = 10000` 不等于吞吐 10000；使用有界 worker pool、内存预算、每桶最大行数和背压。对大桶采用外排归并：内存只保留有限 run，临时文件带 hash，合并时保留总额校验。

### 27.5.3 ClickHouse 表与写入策略

ClickHouse 适合大范围扫描、聚合和分析，不适合作为“每条对账修复的唯一事务边界”。事实写入可通过批量 insert，修复状态和租约仍放在事务型数据库；必要时把 reconciliation result 以 append-only 版本写入 ClickHouse。

```sql
CREATE TABLE recon_fact_ch (
  run_date             Date,
  source               LowCardinality(String),
  legal_entity         LowCardinality(String),
  asset                LowCardinality(String),
  bucket               UInt16,
  source_record_id     String,
  relation_id          String,
  operation_id         String,
  amount_minor         Int64,
  direction            Enum8('CREDIT' = 1, 'DEBIT' = 2),
  value_date           Date,
  event_time           DateTime64(3, 'UTC'),
  finality             LowCardinality(String),
  algorithm_version    LowCardinality(String),
  payload_sha256       FixedString(64),
  ingested_at          DateTime64(3, 'UTC')
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(run_date)
ORDER BY (run_date, legal_entity, asset, bucket, relation_id, source, source_record_id)
SETTINGS index_granularity = 8192;
```

实际生产中按业务日只读需要的 partition，避免 `FINAL` 和 `SELECT *`。相同 source 可能有更正版本，不能依赖 MergeTree 的最终合并来实现业务去重；在查询中按版本/发布批次限定，或预先生成活动事实表。

### 27.5.4 ClickHouse 反连接与金额差异 SQL

ClickHouse 的 `LEFT ANTI JOIN` 可找内部有而外部无的记录。`join_use_nulls = 1` 很关键：否则外连接未匹配的数值列可能以默认 `0` 表示，把“没有对手行”和“对手行金额为 0”混淆。

```sql
SET join_use_nulls = 1;

WITH i AS (
  SELECT relation_id, asset, sumIf(amount_minor, direction = 'CREDIT')
                              - sumIf(amount_minor, direction = 'DEBIT') AS net_internal
  FROM recon_fact_ch
  WHERE run_date = {d:Date} AND source = 'INTERNAL'
    AND bucket = {b:UInt16} AND finality IN ('SETTLED','FINAL')
  GROUP BY relation_id, asset
), c AS (
  SELECT relation_id, asset, sumIf(amount_minor, direction = 'CREDIT')
                              - sumIf(amount_minor, direction = 'DEBIT') AS net_channel
  FROM recon_fact_ch
  WHERE run_date = {d:Date} AND source = 'CHANNEL'
    AND bucket = {b:UInt16} AND finality IN ('SETTLED','FINAL')
  GROUP BY relation_id, asset
)
SELECT i.relation_id, i.asset, i.net_internal
FROM i
LEFT ANTI JOIN c USING (relation_id, asset);
```

有候选外部行时做差额：

```sql
SELECT i.relation_id, i.asset,
       i.net_internal, c.net_channel,
       i.net_internal - c.net_channel AS delta
FROM internal_agg AS i
LEFT JOIN channel_agg AS c
  ON i.relation_id = c.relation_id AND i.asset = c.asset
WHERE c.relation_id IS NULL
   OR i.net_internal != c.net_channel;
```

四方总额应按明确的业务关系聚合，而非把银行余额、内部客户负债和链上托管资产直接相加。示例：

```sql
SELECT legal_entity, value_date, asset, source,
       sumIf(amount_minor, direction = 'CREDIT')
       - sumIf(amount_minor, direction = 'DEBIT') AS net
FROM recon_fact_ch
WHERE run_date = {d:Date} AND bucket BETWEEN {lo:UInt16} AND {hi:UInt16}
GROUP BY legal_entity, value_date, asset, source
ORDER BY legal_entity, value_date, asset, source;
```

`sum(Int64)` 溢出是严重风险：金额范围可能超过 64 位时使用 Decimal 类型或提前按币种/法律实体分片并检查上界；不允许让 ClickHouse 溢出后继续出报告。所有查询把 `asset`、精度和方向作为显式条件，不能用展示层格式化后的 decimal 字符串比较。

---

## 27.6 跨日滚动挂账：迟到不是差错，不能靠改昨日余额消失

### 27.6.1 挂账分类

日切时，把不能在截止点证明完成的差异进入滚动池，而不是强行归因：

| 挂账类型 | 例子 | 处理窗口 | 是否可自动释放 |
|---|---|---:|---:|
| `IN_TRANSIT` | 内部已发起，银行/链上仍在途 | 水位/确认满足 | 是，需原证据 |
| `LATE_SOURCE` | 银行文件、渠道回执迟到 | grace period | 是 |
| `FEE_PENDING` | 本金匹配，手续费日后单列 | 结算文件到达 | 受规则约束 |
| `UNKNOWN_EXTERNAL` | 请求超时，外部结果未知 | 查单/人工终止 | 否，默认不能重付 |
| `UNMATCHED_EXTERNAL` | 银行/链上出现内部未知事实 | 账户归属核查 | 高风险，通常人工 |
| `REORG_PENDING` | 链上交易所在非最终链被回滚风险 | finality policy | 是，必须重新观察 |
| `DATA_QUARANTINE` | 文件/字段/签名不合规 | 供应方更正 | 否 |

### 27.6.2 滚动模型

挂账行要保存原始业务日 `origin_value_date` 和当前报告日 `report_date`。跨日不是把差异改成新日的业务事实，而是在新报告中带入原始证据和年龄：

```text
opening_suspense(report_date D)
+ new_breaks(D)
- resolved_breaks(D)
= closing_suspense(D)
```

`closing_suspense(D)` 必须等于次日 `opening_suspense(D+1)`，除非有明确的批次版本和审计调整。挂账释放时追加 `RESOLVED` 事件，原挂账保持不可变；修复造成账本经济变化时，另由账本服务追加补记/冲正分录。

### 27.6.3 挂账表与年龄策略

```sql
CREATE TABLE recon_break (
  break_id             UUID PRIMARY KEY,
  run_id               UUID NOT NULL,
  bucket               INTEGER NOT NULL CHECK (bucket >= 0),
  break_type           TEXT NOT NULL,
  severity             TEXT NOT NULL CHECK (severity IN ('P0','P1','P2','P3')),
  status               TEXT NOT NULL CHECK (status IN
                         ('OPEN','PENDING','REPAIRING','RESOLVED','ESCALATED','WAIVED')),
  relation_id          TEXT,
  asset                TEXT NOT NULL,
  amount_minor         NUMERIC(38,0) NOT NULL CHECK (amount_minor <> 0),
  origin_value_date    DATE NOT NULL,
  report_date          DATE NOT NULL,
  first_seen_at        TIMESTAMPTZ NOT NULL,
  last_seen_at         TIMESTAMPTZ NOT NULL,
  age_days             INTEGER NOT NULL DEFAULT 0 CHECK (age_days >= 0),
  evidence_json        JSONB NOT NULL,
  rule_version         TEXT NOT NULL,
  resolution_id        UUID,
  version              BIGINT NOT NULL DEFAULT 1,
  UNIQUE (run_id, bucket, relation_id, break_type, rule_version)
);

CREATE INDEX recon_break_open_idx ON recon_break
  (status, severity, report_date, asset) WHERE status IN ('OPEN','PENDING','REPAIRING');
```

年龄不是简单按自然日相减。应按对应法律实体和市场日历计算，周末/节假日、银行价值日、链最终性等待分别定义。比如银行文件允许 `T+1`，链上 finality 需要 20 blocks，则在窗口内是 `PENDING`；超过 SLA 后才升级 `P1`，而不是每次日切都把它算成新差错。

### 27.6.4 防止“自动修复风暴”

- 同一 `break_id` 只能有一个活跃 repair lease；
- 最大自动重试次数、指数退避和随机抖动；
- 修复后必须重新采集/对拍，不能根据 API `200` 直接 `RESOLVED`；
- 自动修复的金额上限、币种、业务类型、年龄和风险等级全部配置化并版本化；
- 账本/渠道结果为 `UNKNOWN` 时只允许查单或挂账，不允许自动二次扣款；
- 任何自动修复失败不回滚原始证据，进入 `ESCALATED` 并告警。

---

## 27.7 差错分类与自动修复：修复的是事实链，不是报表数字

### 27.7.1 差错分类矩阵

| 类型 | 判定证据 | 示例 | 自动动作 | 红线 |
|---|---|---|---|---|
| `DUPLICATE` | 同一来源键/幂等键多条有效记录 | 回调重放 | 保留一条，标记其余重复；必要时冲正重复分录 | 不删除原记录 |
| `MISSING_INTERNAL` | 外部已终态且内部无事实 | 银行已扣款，内部未入账 | 生成待审批补记任务，按原 operation 查账 | 不凭金额猜客户 |
| `MISSING_EXTERNAL` | 内部已发起，外部未到且在 SLA 内 | 银行文件迟到 | 转 `PENDING`，继续查文件/查单 | 不立即退款/重付 |
| `AMOUNT` | 身份匹配，金额不同 | 链上扣 gas、渠道手续费 | 拆本金/费用/汇兑，生成精确补记或冲正 | 不用容差吞掉本金差 |
| `CURRENCY_ASSET` | 币种、链、合约地址不一致 | USDC 与 USD 混淆 | 隔离、人工核验资产映射 | 禁止直接汇率相加 |
| `STATUS` | 状态版本乱序/终态冲突 | 先失败后成功 | 按序列/状态机重放，保留终态证据 | 终态不可覆盖 |
| `TIMING_CUTOFF` | 价值日/会计日不同 | 23:59:59 交易跨日 | 进入滚动挂账或次日子批次 | 不修改原始发生日 |
| `CHAIN_REORG` | canonical hash 改变 | 充值所在块被回滚 | 冻结可用额、冲正 provisional、重新确认 | 不以旧块确认入账 |
| `DATA_INTEGRITY` | hash/签名/行数/schema 不符 | 半文件/篡改 | 文件隔离，向供应方索要更正 | 不部分发布 |
| `UNAUTHORIZED_EXTERNAL` | 外部存在未知转账 | 钱包误转/盗转 | 进入暂收、风控和人工 | 不自动归客户 |

### 27.7.2 自动修复决策树

```text
是否有完整原始证据？──否──> DATA_QUARANTINE / 人工
       │是
来源身份能唯一映射？──否──> UNKNOWN_EXTERNAL / 人工
       │是
是否只是迟到/最终性不足？──是──> PENDING + 查单/等水位
       │否
是否重复且原事实已成功？──是──> 去重标记；重复经济影响则追加冲正
       │否
金额差异是否由已知费用/汇兑规则解释？──是──> 拆分分录并重新对账
       │否
是否在自动修复白名单且低于限额？──是──> 带 repair_key 执行状态机
       │否
                 ESCALATED + 双人审批 + 人工操作台
```

### 27.7.3 修复必须调用账本/状态机

例如内部已入账但渠道回执重复，不能把 `recon_break.status` 直接设成 `RESOLVED`；应该：

1. 以 `repair_key = break_id + rule_version + action` 抢占修复幂等键；
2. 查内部凭证、渠道原始回执和历史修复，确认重复经济影响；
3. 通过账本服务追加反向分录，明确对手科目（如渠道暂收/差错挂账）；
4. 通过 Outbox 发布 `RECONCILIATION_REPAIRED`；
5. 重新跑受影响桶，只有事实匹配并通过总额校验才关闭差异。

Go 的修复入口示例：

```go
type RepairRequest struct {
	BreakID, RuleVersion, Action string
	AmountMinor int64
	Asset       string
	ExpectedHash string
}

func (s *RepairService) Execute(ctx context.Context, r RepairRequest) error {
	repairKey := r.BreakID + ":" + r.RuleVersion + ":" + r.Action
	created, err := s.repo.InsertRepairOnce(ctx, repairKey, r)
	if err != nil || !created { return err } // 已执行/执行中，复用结果

	b, err := s.repo.LoadBreakForUpdate(ctx, r.BreakID)
	if err != nil { return err }
	if b.Status != "OPEN" && b.Status != "PENDING" { return nil }
	if b.ExpectedEvidenceHash != r.ExpectedHash { return ErrEvidenceChanged }

	// 不直接更新余额；账本服务内部以同一 repairKey 做不可变冲正/补记。
	if err := s.ledger.AppendCompensation(ctx, LedgerCompensation{
		IdempotencyKey: repairKey, BreakID: r.BreakID,
		AmountMinor: r.AmountMinor, Asset: r.Asset, Action: r.Action,
	}); err != nil {
		return s.repo.MarkRepairRetryable(ctx, repairKey, err)
	}
	return s.repo.MarkRepairSubmitted(ctx, repairKey)
}
```

Java 21 中，规则应是纯函数，副作用交给端口；这样可以枚举边界并做 property test：

```java
public record Difference(long internal, long external, String asset,
                         Finality finality, boolean fileComplete) {}

public enum Action { WAIT, QUARANTINE, AUTO_COMPENSATE, ESCALATE }

public Action decide(Difference d, Policy p) {
    if (!d.fileComplete()) return Action.QUARANTINE;
    if (d.finality() != Finality.FINAL) return Action.WAIT;
    if (d.internal() == d.external()) return Action.WAIT; // 无差异，不创建修复
    long delta = Math.subtractExact(d.internal(), d.external());
    if (p.knownFee(d.asset(), delta) && Math.abs(delta) <= p.autoLimit(d.asset()))
        return Action.AUTO_COMPENSATE;
    return Action.ESCALATE;
}
```

### 27.7.4 修复审计链

每次分类、规则命中、外部调用、账本凭证、人工审批和重新对拍都记录：

```text
break_id → rule_version → repair_key → actor/service → request_hash
         → source evidence hash → ledger_entry_id/channel_query_id
         → before_status → after_status → verification_run_id
```

日志可以进入哈希链或 WORM 存储，但哈希链不能替代原始数据权限、加密和访问审计。PII 脱敏不等于删除关联能力：用 token 化客户身份，保留可审计的受控反查路径。

---

## 27.8 DDL：事务型控制面、事实表和报告版本

### 27.8.1 批次、事实和匹配结果

```sql
CREATE TABLE recon_run (
  run_id              UUID PRIMARY KEY,
  business_date       DATE NOT NULL,
  window_start_utc    TIMESTAMPTZ NOT NULL,
  window_end_utc      TIMESTAMPTZ NOT NULL,
  algorithm_version   TEXT NOT NULL,
  bucket_count        INTEGER NOT NULL CHECK (bucket_count > 0),
  status              TEXT NOT NULL CHECK (status IN
                        ('CREATED','RUNNING','VERIFYING','PUBLISHED','FAILED','SUPERSEDED')),
  internal_watermark  TEXT NOT NULL,
  external_watermark  JSONB NOT NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at        TIMESTAMPTZ,
  UNIQUE (business_date, algorithm_version, window_start_utc, window_end_utc)
);

CREATE TABLE recon_bucket_checkpoint (
  run_id          UUID NOT NULL REFERENCES recon_run(run_id),
  bucket_no       INTEGER NOT NULL,
  status          TEXT NOT NULL CHECK (status IN ('READY','RUNNING','DONE','FAILED')),
  lease_token     UUID,
  lease_until     TIMESTAMPTZ,
  input_checksum  BYTEA,
  output_checksum BYTEA,
  row_count       BIGINT NOT NULL DEFAULT 0,
  error_code      TEXT,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, bucket_no)
);

CREATE TABLE recon_canonical_fact (
  fact_id             UUID PRIMARY KEY,
  run_id              UUID NOT NULL REFERENCES recon_run(run_id),
  source               TEXT NOT NULL,
  source_record_id    TEXT NOT NULL,
  source_version      TEXT NOT NULL,
  relation_id         TEXT,
  operation_id        TEXT,
  legal_entity        TEXT NOT NULL,
  account_scope       TEXT NOT NULL,
  asset               TEXT NOT NULL,
  amount_minor        NUMERIC(38,0) NOT NULL CHECK (amount_minor > 0),
  direction           CHAR(1) NOT NULL CHECK (direction IN ('C','D')),
  business_type       TEXT NOT NULL,
  event_time_utc      TIMESTAMPTZ NOT NULL,
  value_date          DATE NOT NULL,
  finality            TEXT NOT NULL CHECK (finality IN ('PROVISIONAL','SETTLED','FINAL','REORGED')),
  bucket_no           INTEGER NOT NULL CHECK (bucket_no >= 0),
  payload_sha256      BYTEA NOT NULL CHECK (octet_length(payload_sha256) = 32),
  raw_evidence_id     UUID NOT NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source, source_record_id, source_version, payload_sha256)
);

CREATE INDEX recon_fact_bucket_idx ON recon_canonical_fact
  (run_id, bucket_no, relation_id, asset, source);

CREATE TABLE recon_match (
  match_id            UUID PRIMARY KEY,
  run_id              UUID NOT NULL REFERENCES recon_run(run_id),
  bucket_no           INTEGER NOT NULL,
  match_key           TEXT NOT NULL,
  asset               TEXT NOT NULL,
  internal_amount     NUMERIC(38,0) NOT NULL,
  channel_amount      NUMERIC(38,0),
  bank_amount         NUMERIC(38,0),
  chain_amount        NUMERIC(38,0),
  result              TEXT NOT NULL CHECK (result IN ('MATCHED','BREAK','PENDING')),
  evidence_json       JSONB NOT NULL,
  algorithm_version   TEXT NOT NULL,
  UNIQUE (run_id, bucket_no, match_key, algorithm_version)
);
```

金额采用 `NUMERIC(38,0)` 是数据库保护层，Go/Java 业务层仍建议用 `int64`/`BigInteger` 或明确的定点类型，并在币种上限处拒绝溢出。`NUMERIC` 不能替代币种检查；美元 cent 和 BTC satoshi 的整数没有可比性。

### 27.8.2 事务边界与发布

`recon_run` 的 `PUBLISHED` 是报告版本指针，不表示所有历史数据可修改。发布事务应锁住 run、检查全部桶 `DONE`、核对 checkpoint checksum、确认水位和文件状态，再写入 `published_at`。业务查询使用“当前活动 run”视图或显式 `run_id`，不读未发布的中间结果。

```sql
-- 伪代码：在事务中执行
SELECT status FROM recon_run WHERE run_id = :run FOR UPDATE;
SELECT count(*) FROM recon_bucket_checkpoint
 WHERE run_id = :run AND status <> 'DONE'; -- 必须为 0
SELECT 1 FROM recon_file
 WHERE source = :source AND business_date = :d
   AND status <> 'PUBLISHED'; -- 对要求完整的来源必须为 0
UPDATE recon_run SET status = 'PUBLISHED', published_at = now()
 WHERE run_id = :run AND status = 'VERIFYING';
```

如果发布中断，事务回滚；如果发布后发现迟到证据，创建新 `run_id` 或 `late_arrival` 子批次，不能修改已发布 run 的事实行。

---

## 27.9 Go + Java 核心实现：规范化、匹配与幂等

### 27.9.1 Go 金额和匹配器

```go
type Fact struct {
	Key, RelationID, Asset, Source string
	AmountMinor int64
	Credit      bool
	Final       bool
}

type Aggregate struct {
	Internal, Channel, Bank, Chain int64
}

func add(a int64, f Fact) (int64, error) {
	if f.AmountMinor <= 0 { return 0, errors.New("non-positive amount") }
	if f.Credit {
		if a > math.MaxInt64-f.AmountMinor { return 0, errors.New("amount overflow") }
		return a + f.AmountMinor, nil
	}
	if a < math.MinInt64+f.AmountMinor { return 0, errors.New("amount underflow") }
	return a - f.AmountMinor, nil
}

func AggregateSorted(rows []Fact) (map[string]Aggregate, error) {
	out := make(map[string]Aggregate)
	for _, f := range rows {
		if !f.Final { continue } // provisional 单独进入等待队列
		a := out[f.Key]
		v, err := add(0, f)
		if err != nil { return nil, err }
		switch f.Source {
		case "INTERNAL": a.Internal, err = add(a.Internal, Fact{AmountMinor:v, Credit:true})
		case "CHANNEL": a.Channel, err = add(a.Channel, Fact{AmountMinor:v, Credit:true})
		case "BANK": a.Bank, err = add(a.Bank, Fact{AmountMinor:v, Credit:true})
		case "CHAIN": a.Chain, err = add(a.Chain, Fact{AmountMinor:v, Credit:true})
		default: return nil, fmt.Errorf("unknown source %s", f.Source)
		}
		if err != nil { return nil, err }
		out[f.Key] = a
	}
	return out, nil
}
```

示例中的 `v` 为有符号净额，真实代码应把正负方向和 overflow 一次性封装，避免用 `Credit:true` 伪造一个可能为负的金额。更稳妥的实现是 `AddSigned(current, signedDelta)`，并在测试中覆盖 `MinInt64/MaxInt64`；代码片段的重点是：来源固定枚举、非最终事实不混进 final 报告、每一步都检查溢出。

### 27.9.2 Java 21 虚拟线程不是无限并发

```java
public final class ReconcileExecutor {
    private final Semaphore permits;
    private final ExecutorService workers = Executors.newVirtualThreadPerTaskExecutor();

    public ReconcileExecutor(int maxInFlight) {
        this.permits = new Semaphore(maxInFlight);
    }

    public CompletableFuture<BucketResult> submit(BucketJob job) {
        return CompletableFuture.supplyAsync(() -> {
            boolean acquired = false;
            try {
                permits.acquire();
                acquired = true;
                return processIdempotently(job); // run+bucket+algorithm 唯一
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new CompletionException(e);
            } finally {
                if (acquired) permits.release();
            }
        }, workers);
    }
}
```

虚拟线程降低阻塞成本，不会增加 ClickHouse、PostgreSQL、磁盘和渠道的容量；必须设置 semaphore、连接池上限、deadline 和取消传播。Java 金额用 `long` 最小单位或 `BigInteger`；`BigDecimal` 只在明确 scale、rounding mode、币种下使用，禁止把 `double` 传入构造函数。

### 27.9.3 数据库幂等与租约的关键 SQL

```sql
-- 抢桶：旧 worker 不能用过期 token 覆盖新结果
UPDATE recon_bucket_checkpoint
SET status = 'RUNNING', lease_token = :token,
    lease_until = now() + interval '2 minutes', updated_at = now()
WHERE run_id = :run AND bucket_no = :bucket
  AND status IN ('READY','FAILED')
  AND (lease_until IS NULL OR lease_until < now());

-- 提交时必须验证 token，且结果唯一
INSERT INTO recon_match (...)
VALUES (...)
ON CONFLICT (run_id, bucket_no, match_key, algorithm_version) DO NOTHING;

UPDATE recon_bucket_checkpoint
SET status = 'DONE', output_checksum = :checksum,
    lease_token = NULL, lease_until = NULL, updated_at = now()
WHERE run_id = :run AND bucket_no = :bucket
  AND status = 'RUNNING' AND lease_token = :token;
```

若 `UPDATE` 影响 0 行，worker 必须停止提交，不能“认为自己仍然持有租约”。严格的 fencing token 可使用单调递增的 epoch；时间租约只是回收机制，不是资金安全边界。

---

## 27.10 测试与压测：证明不重、不错、不丢、可恢复

### 27.10.1 单元与性质测试

至少覆盖：

- 同一来源记录重复 N 次，规范化事实数量仍为 1；不同 payload 使用同键必须拒绝；
- 四方输入任意排列、重复、乱序，聚合结果与排序输入一致；
- 一对多、多对一、手续费、gas、退款和部分成交均能解释且保留映射；
- `amount_minor = 0`、负数、`MaxInt64`、`MinInt64`、`NUMERIC(38,0)` 上限拒绝或正确报错；
- 币种/链/合约地址不一致永不匹配；展示小数变化不影响最小单位结果；
- 文件少 part、重复 part、行数/字节数/hash 不一致、签名错误都不可发布；
- 日切边界：`23:59:59.999`、闰秒策略、夏令时切换、UTC 与法律实体时区；
- 链 reorg：provisional 交易回滚后产生冲正/冻结，旧块不能继续是 final；
- worker 在写事实后、写 checkpoint 前崩溃，重跑不重复；旧 lease token 不能覆盖新结果；
- 修复 API 超时后重复调用，`repair_key` 只产生一个账本动作；账本返回 `UNKNOWN` 只能查单/挂账。

Go 可用 `testing/fuzz` 生成任意事实顺序和重复次数；Java 可用 jqwik/QuickTheories 验证交换律、幂等性和金额守恒。对每个规则保存输入证据 fixture，规则版本升级时跑 golden test，禁止只测“最终状态字符串”。

### 27.10.2 故障注入

在测试环境注入：

1. 对象存储上传 49% 时断链，重复上传同名不同 hash；
2. Kafka/消息队列在 DB commit 后、ACK 前断开；
3. DB deadlock、连接断开、ClickHouse insert 超时和 merge backlog；
4. worker GC pause 超过 lease、网络分区后旧 worker 恢复；
5. 渠道成功但 HTTP 响应丢失，随后回调先于查单结果到达；
6. 银行文件迟一天、同一业务日发出更正文件；
7. 链节点返回错误 canonical chain、RPC 读到不同块高；
8. 日切时仍有实时流写入，批次发布过程中进程被 kill。

每个故障都要证明：原始证据没有丢；没有重复经济事实；批次可从 checkpoint 继续；未达最终条件不会被标记 `FINAL`；告警和人工队列可定位。

### 27.10.3 压测模型与 SLO

不要只压平均吞吐，至少准备三种数据集：

| 数据集 | 规模 | 特征 |
|---|---:|---|
| 均匀 | 1 亿/日 | 4096 桶均匀，匹配率 99.9% |
| 热点 | 1 亿/日 | 1% 商户占 40% 行，单桶极端放大 |
| 差错风暴 | 1 亿/日 | 5% 重复、1% 迟到、0.1% 未知外部、文件更正 |

记录 p50/p95/p99 对拍延迟、每桶吞吐、内存峰值、外排临时空间、ClickHouse scan bytes、PostgreSQL 锁等待、挂账年龄、修复成功率和误修率。目标示例（须按容量评审，不可照抄）：实时初筛 p99 < 5 秒；文件完整性发现 < 2 分钟；亿级 final 批次在截止后 30 分钟内完成；重跑单桶不超过原桶成本的 1.2 倍；自动修复误修率为 0，宁可升级人工。

容量估算应包括放大倍数：

```text
日输入存储 = 行数 × (raw 平均大小 + canonical 大小 + 索引/副本放大)
峰值写入 = 平均 QPS × 重放/迟到/重试系数
内存上限 = worker 数 × 单桶内存 + 排序 buffer + DB/CH 客户端 buffer
```

压测环境必须包含真实 schema、索引、压缩、TTL 和副本；用“空表+假数据”的吞吐没有生产参考价值。

### 27.10.4 可观测性与告警

核心指标：

- `recon_lag_seconds{source,asset}`、各来源 watermark gap；
- `file_integrity_failures_total`、未发布 part、重复 manifest；
- `break_open_total{type,severity,age_bucket}`、挂账金额按资产分组；
- `match_rate`、`duplicate_rate`、`unknown_rate`、`auto_repair_rate`、`repair_recheck_fail_rate`；
- `bucket_retry_total`、lease 过期、checksum mismatch、报告发布耗时；
- 链 `reorg_depth`、finality lag、RPC 节点分歧；
- 内部总账、银行、链上资产与客户负债的独立总额试算。

告警要区分“数据迟到”和“资金差错”：水位停滞、文件缺洞属于接入告警；`FINAL` 窗口的本金不平、未知外部出账、链重组影响可用余额属于资金告警。日志中的 `operation_id/relation_id/run_id/bucket/break_id` 必须可串联，敏感字段脱敏。

---

## 27.11 极端边界与处理决策

### 27.11.1 时间和日切

- 用 UTC 保存事件时间，用法律实体交易日历生成 `value_date`；展示时再转时区；
- 同一秒/毫秒的排序不能依赖墙上时间，使用来源序号、Outbox sequence 或 `(event_time, source_sequence)`；
- 23:59:59 到 00:00 的跨日事件按日切 epoch 归属，不能由消费者收到时间决定；
- 夏令时重复小时使用带 offset 的时间，不能把本地字符串当唯一键；
- “今天文件”可能记录昨天价值日，按 manifest 和银行价值日处理，文件接收日只做运维维度。

### 27.11.2 链和资产

- `Latest`、软确认、finalized、challenge period 必须分开；未 final 只能是 provisional/在途；
- 同名代币必须以 chain ID + contract address + decimals 作为资产身份；原生币和 wrapped token 不相同；
- 链 reorg 后，旧交易证据保留为 `REORGED`，不能物理删除；已经释放的客户余额由受控冲正/冻结状态机处理；
- UTXO 交易的输入输出、找零、手续费要建模，不能只按地址余额猜一笔充值；
- 链上“余额相等”不证明归属正确：归集地址、热钱包、第三方托管户和客户映射必须分别核对。

### 27.11.3 文件和数据库

- 空文件是否合法必须由来源协议决定；空文件不能默认为“今日无交易”；
- `0` 金额行、负向退款行、重复退款行分开建模，不能用非正数统一丢弃；
- 超过 64 位的聚合在应用和 ClickHouse 端都要做边界检查；
- ClickHouse 异步 insert 成功不代表业务报告可发布，必须用可见性/批次确认；
- PostgreSQL 事务重试只可重试无副作用或带幂等键的动作，不能把外部调用包在“自动重试事务”里；
- 数据库主从延迟时，查单读必须选择能证明版本的节点，不能用旧副本决定修复。

### 27.11.4 人工操作

人工不能直接改 `recon_canonical_fact`、`ledger_line` 或余额；操作台只允许提交结构化 action，带原因、审批人、证据 hash、限额和有效期，由修复服务执行。紧急止血可以暂停自动修复/冻结出款，但恢复必须重新跑水位和差异，不允许用“临时 SQL”作为永久流程。

---

## 27.12 术语表：面试和设计评审必须说清楚

| 术语 | 含义 | 常见误解 |
|---|---|---|
| Reconciliation | 对多个来源事实进行身份、金额、状态和总额核对 | 不是简单余额比较 |
| CRF | Canonical Reconciliation Fact，规范化对账事实 | 不是覆盖原始记录 |
| Source evidence | 来源原始证据及 hash/签名/版本 | 不是日志文本 |
| Match key | 跨来源匹配的规范化复合键 | 不一定等于渠道订单号 |
| Relation ID | 多方证据的关联身份 | 不能强行从缺失字段猜出 |
| Provisional | 证据已到但未满足最终性/截止条件 | 不是失败 |
| Final | 在指定水位和策略下可发布的结果 | 不是宇宙绝对真相 |
| Watermark | 某来源已连续处理到的边界 | 不是最新消息时间 |
| Grace period | 等迟到证据的容忍窗口 | 不能无限期掩盖差错 |
| Staging | 未发布、待完整性校验的隔离数据区 | 不是生产事实表 |
| Anti-join | 找出左侧有、右侧没有的行 | NULL 默认值会误导结果 |
| Break | 对账差异/例外项 | 不等于业务损失 |
| Suspense | 暂时无法归属或完成的挂账 | 不是垃圾桶 |
| Rolling suspense | 跨报告日带原始日和年龄滚动的挂账池 | 不是改日平账 |
| Repair key | 修复动作的幂等身份 | 不是普通 trace ID |
| Fencing token | 防止旧租约持有者提交结果的单调令牌 | 分布式锁本身不等于 fencing |
| Canonical chain | 链索引器认定的当前规范链 | 最新 RPC 响应不一定是它 |
| Reorg | 区块链历史分叉后旧块不再 canonical | 不是普通网络重试 |
| Late arrival | 在报告发布后到达、需子批次处理的事实 | 不能修改原报告正文 |

---

## 27.13 面试答题卡：三分钟讲清一个生产级对账系统

### Q1：四方对账的“真相”是谁？

**答**：没有单一全局真相。内部不可变账本证明平台记了什么；渠道证明委托和回执；银行证明法币账户发生了什么；链上证明指定最终性下观察到什么。先保留各方原始证据，再通过带作用域的 `relation_id` 映射到 CRF；结果必须同时满足水位、最终性、文件完整性、身份和金额规则。

### Q2：为什么实时流和日终文件都要做？

**答**：流用于秒级发现、降低未知态和挂账时间，但会丢/重/乱序且缺少全量证明；文件或快照用于范围完整性和截止点 final 证明，但会迟到。两者不维护两套余额，而是落同一证据表，凭 source key/hash 幂等，流先 provisional，文件和水位满足后 final。

### Q3：亿级数据怎么跑？

**答**：按法律实体、资产、关系/来源 ID 稳定 hash 到固定数量桶；每个 `(run,bucket)` 有 lease、checkpoint、输入输出 checksum 和唯一键，worker 有界并发，热点桶外排归并。ClickHouse 做按日/桶的扫描、预聚合和 anti-join，事务型数据库负责状态、修复和发布。任意桶失败可独立重跑，全部桶和水位完成才原子发布。

### Q4：发现内部和银行差 100，是否自动补账？

**答**：先区分银行已到但内部缺事实、内部发起但银行迟到、重复、手续费、跨日和未知态。只有原始证据完整、身份唯一、金额/资产规则明确、在自动修复白名单和限额内，才用 `repair_key` 调账本服务追加补记/冲正；不能直接 UPDATE 余额。未知态默认查单或挂账，不重付。

### Q5：如何处理链重组？

**答**：把交易分为 provisional/settled/final，保存 block hash、height、canonical status 和观察版本。检测到 reorg 时，旧证据标记 `REORGED`，冻结尚未安全释放的余额或追加冲正，重新在 canonical chain 观察；不能把旧块物理删除或继续当最终入账。

### Q6：文件上传到一半但文件名正确怎么办？

**答**：文件先进入对象存储 Staging，按 manifest 校验分片连续性、sha256、行数、字节数、签名、Schema 和链式版本；任何缺洞或不符都 quarantine，不部分发布。只有所有分片通过，才在短事务中切换发布指针。

### Q7：对账系统怎样证明“不会重复修复”？

**答**：原始证据以 source/version/hash 唯一；匹配结果以 run/bucket/key/algorithm 唯一；修复以 break/rule/action 的 `repair_key` 唯一；账本本身也以同一业务幂等键唯一。提交后 ACK、租约 fencing、重跑 checkpoint 和修复后重对账共同保证，不能只靠 Redis TTL 或内存去重。

---

## 27.14 自检题：写代码前先回答

1. 你能为每个来源写出 `source_record_id`、版本、payload hash 和去重范围吗？
2. 你的匹配键是否包含法律实体、资产/链、会计日和业务类型？缺失关联 ID 时怎么办？
3. 事件流和文件流是否落同一事实模型？文件更正是否会覆盖历史证据？
4. 哪些条件使一个批次从 `PROVISIONAL` 变成 `FINAL`？每个来源的 watermark 如何证明连续？
5. 日切时跨日交易归哪一个会计日？迟到交易如何产生子批次而不修改原报告？
6. 你能解释一对多、多对一、手续费、gas、退款和汇兑差额的每一分钱吗？
7. 1000 个 worker 同时抢同一桶时，lease、fencing 和唯一键如何保护？
8. ClickHouse 的 `LEFT JOIN` 未匹配数值列是什么值？为什么要设置 `join_use_nulls=1`？
9. 自动修复若在账本 commit 后响应超时，重试如何避免第二笔经济事实？
10. 发现银行有钱、内部无钱时，如何阻止错误归户？未知外部转账进入哪里？
11. 链上交易尚未 final 但内部客户已入账，修复是删除、UPDATE 还是追加状态/分录？为什么？
12. 文件 hash 正确但 Schema 版本不兼容时能否发布？空文件和零金额行如何区分？
13. `int64` 聚合可能溢出时，Go、Java、PostgreSQL、ClickHouse 分别怎么拒绝？
14. 你能从 `break_id` 反查到原始文件行/消息、规则版本、账本凭证和重新对拍 run 吗？
15. 你的压测是否包含热点桶、文件迟到、消息重复、reorg、租约过期和数据库重试？
16. 你定义的“自动修复成功”是 API 返回 200，还是修复后证据重对拍并通过？

若第 4、8、9、10、11、16 题不能给出事务边界和证据链，系统还只是报表脚本，不是生产对账引擎。

---

## 27.15 与 28 章衔接：对账发现未知态，支付网关负责让未知态收敛

本章输出给下游的不是一个“差异列表”，而是一组有证据和状态约束的接口：

```text
reconciliation → payment gateway / settlement
  break_type, operation_id, relation_id, asset, amount_minor
  evidence_hashes, source_watermarks, finality, origin_value_date
  suggested_action (QUERY / HOLD / COMPENSATE / ESCALATE)
  repair_key, rule_version, risk_limit
```

下一章 [28 支付核心与多渠道收单网关：智能路由、未知态收敛、重付退回与逆向拒付](28-payment-gateway-routing-and-unknown-state.md) 将把这些差异接入渠道状态机：

1. `UNKNOWN_EXTERNAL` 进入查单、回调和文件三路竞争仲裁，而不是自动重付；
2. 渠道成功但内部未入账时，使用本章的证据 hash 和 `operation_id` 做幂等补记；
3. 一单多付、部分退款、拒付和重付退回沿原 `relation_id` 生成反向事实；
4. 路由成功率、超时和渠道限额变化不能突破本章的账本、对账和自动修复红线；
5. 支付网关的最终成功必须同时满足渠道终态、内部凭证和后续对账可追溯，而不是仅凭 HTTP 响应。

因此，**27 负责证明和收敛事实，28 负责在副作用不确定时安全推进业务状态**。两章之间的接口必须携带原始证据、稳定业务身份、最终性和水位；不能用一个 `status=SUCCESS` 字段替代整条证据链。

---

## 27.16 本章小结：五条可落地的生产规则

1. **先保留证据，再规范化匹配**：原始文件、消息、链上块和内部分录都不可变；规范化事实可重建。
2. **实时发现、文件定界**：双流进入同一模型，watermark/finality/grace period 决定 provisional 还是 final。
3. **亿级靠分桶和可恢复**：固定 hash 桶、有界并发、checkpoint、checksum、fencing 和 ClickHouse 预聚合，不能全表大事务。
4. **挂账是受控状态，不是垃圾桶**：跨日保留 origin date、年龄、证据和 SLA；未知态不重付，reorg 不删历史。
5. **自动修复必须产生新事实并重对账**：`repair_key` 幂等，补记/冲正走账本和状态机；任何直接改余额、覆盖文件、关闭差异的快捷方式都应被禁止。

当这些规则成立时，“对账平”才具有可证明的含义：在指定的业务窗口、来源水位、最终性和规则版本下，四方证据可追溯、差异可解释、挂账可滚动、修复可重放，且下一次重跑不会制造新的钱或抹掉旧的事实。
