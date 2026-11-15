# 14. 对账体系：日终对账、实时核对与差异处理

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · 事务与一致性 ｜ **编号**：14 / 52
> **本章定位**：幂等、事务、最终一致都只能降低错误概率，**只有对账能发现「已经发生但没人报错」的静默错误**。这一章给三层对账、差异分类、亿级流水的分片核对 SQL、文件解析、断点续跑和一套能落地的指标。
> **读法**：§14.2 先建立「对账不是核总数」的认知；§14.4.4 的 ClickHouse SQL 可直接改表名用；§14.5 是 Agent 侧 token 对账的映射。

---

## 14.1 引子：两笔「没人报错」的账

**事故一：支付成功，账务没入账（2025-09-30）**

某支付平台的账务服务在月末版本发布时，把「渠道手续费四舍五入」的逻辑从 `HALF_UP` 改成了 `DOWN`。差异只有 1 分钱，但导致 3,700 笔交易的「渠道金额 ≠ 账务金额」，账务侧直接拒绝了入账，只打了 `WARN` 日志。

- 3,700 笔支付成功，账务侧没有分录；用户侧显示「支付成功」，商户侧显示「未结算」。
- 9 天后日终对账才发现，涉及 **186 万元**；其中 42 笔跨月，财务报表已经报出。
- 根因不是「支付失败」，是**支付成功与账务入账之间的静默错误**；没有任何监控报警。

| 时间 | 事件 | 数据 |
| --- | --- | --- |
| 09-30 20:00 | 账务服务 v3.14 全量发布，手续费舍入 `HALF_UP` → `DOWN` | 影响所有渠道 |
| 09-30 20:03 | 首笔 1 分钱差异，账务侧拒绝入账，只打 `WARN` | 1 笔 |
| 09-30 23:59 | 累计支付成功但无分录 | **3,700 笔 / 186 万元** |
| 10-01 00:05 | 日终对账因节假日顺延（无人值守） | 0 次核对 |
| 10-09 02:10 | 日终对账启动，比对 8.2 亿行流水 | — |
| 10-09 04:47 | 差异表写入 3,700 条 `SHORT`，差异率 0.00045% | 4,900 条/分钟 |
| 10-09 09:30 | 财务确认 42 笔跨月，报表已报出，需追溯调整 | 跨月 42 笔 |

**事故二：Agent 平台 token 账单多算 2.3%（2025-11-02）**

某 Agent 平台按 token 向客户计费。10 月账单出来后，客户发现比自己统计的多 2.3%。排查发现：Step 重试时，模型调用被重复计费，但 `step_id` 幂等只挡住了结果落库，没挡住用量累加。

- 多计 **47 万元**，涉及 312 个租户；最大单租户多计 8.6 万元。
- 客户投诉 57 起，2 个客户要求审计原始调用日志。
- 根因：**用量表没有按 `step_id` 唯一**，重试一次就多记一次 token。

| 时间 | 事件 | 数据 |
| --- | --- | --- |
| 11-02 10:00 | 月结开始，拉取 10 月内部用量与供应商账单 | 312 租户 |
| 10:12 | 内部 4.28B token，供应商账单 4.38B | 差 **2.3%** |
| 10:31 | 抽样 100 个 Run，发现同一 `step_id` 两条 usage | 重试率 3.1% |
| 11:05 | 定位：`usage` 表无 `uk(run_id, step_id)`，重试即重复计费 | 47 万元 |
| 14:30 | 退费方案审批通过，按租户逐笔冲正 | 312 笔冲正 |
| 11-05 | 客户投诉累计 57 起，2 家要求提供原始调用审计 | — |

> 🩸 两件事的共同点：**系统没有报错，业务也没有中断，但账错了。** 监控看的是「成功率、延迟、错误率」，看不到「应该有的分录没写」；只有对账能把这类错误翻出来。

---

## 14.2 到底是什么：对账是「两个独立记录源的交叉验证」

**定义**：对账 = 对同一个经济事实，从两个**独立记录**的系统里各取一份数据，按业务主键比对，找出「你有我无、我有你无、状态不同、金额不同」的差异，并分类处理。

三个关键词：

- **独立记录**：两边不能来自同一个写入路径，否则同一个 bug 会同时污染两边，对账永远平。
- **业务主键**：订单号、支付流水号、退款单号、`run_id + step_id`；没有主键就只能核总数，核总数会漏掉「一多一少相互抵消」。
- **分类处理**：差异不是「报个警」就完了，要能自动冲正/补记，或进人工工单。

先钉死五个误解：

| 你以为的对账 | 为什么错 | 不做会怎样 |
| --- | --- | --- |
| 「核总数平了就行」 | 两笔 100 元的长短款可以相互抵消，总数仍然是 0 差异 | 单笔错账永远发现不了 |
| 「有幂等就不需要对账」 | 幂等防重复，不防漏写、错写、状态错、金额算错 | 静默错误积累到月末，财务报表已经报出 |
| 「实时对账能替代日终」 | 实时对账有窗口，晚到的数据、跨日交易、渠道补文件都漏 | 日终一跑，差异一大堆，已经过了处理时效 |
| 「差异一定是对方错」 | 可能是自己漏记、重复、状态机卡住、时区/币种错 | 把责任推给渠道，自己的 bug 留到下次 |
| 「对账文件手工补一下就行」 | 手工补没有断点、没有审计、不能重跑 | 每次月结都靠人熬夜，错一次就是 P0 |

**对账与幂等、事务的关系**：

```
幂等：防「重复执行」           → 降低重复类错误
事务：防「写一半」             → 降低原子性错误
最终一致：允许「暂时不一致」    → 把不一致压缩到可接受窗口
对账：发现「已经不一致」        → 唯一能兜住静默错误的手段
```

> 💡 一句话：**幂等和事务是「事前防」，对账是「事后查」。** 事前防得再好，也需要事后查，因为代码会错、配置会错、人会手工改数据。

---

## 14.3 结构图：三层对账与差异处理

```
              ┌──────────────────────────────┐
业务库/账务库  │  明细快照（T+0 增量 / T+1 全量）│
              └──────────────┬───────────────┘
                             │
渠道文件/SFTP/API ──► 文件解析器 ──► 标准渠道明细表
                             │
                    ┌────────▼────────┐
                    │   对账引擎       │
                    │ 分片并行 + 汇总校验│
                    └────────┬────────┘
                             │
                  ┌──────────▼──────────┐
                  │ reconciliation_diff │
                  └───┬───────┬─────┬───┘
                      │       │     │
                 自动冲正  自动补记  人工工单
                      │       │     │
                      └───────┴─────┘
                             │
                    对账报表 / 指标 / 审计

三层对账：
① 实时明细：单笔，秒~分钟；高价值/高风险交易；发现「支付成功但账务没入账」
② 准实时汇总：5 分钟/小时；按商户/币种/渠道汇总 count+sum+checksum；发现整体漂移
③ 日终全量：T+1 全量文件；权威口径；发现晚到、跨日、边界、历史遗漏
```

**三层不是「三选一」，是「三道网，网眼不同」**：

| 层 | 延迟 | 数据源 | 粒度 | 能发现 | 发现不了 | 单位成本 | 典型技术 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **L1 实时明细** | 秒级（≤5s） | 业务事件流 / binlog / 渠道回调 | 单笔 | 单笔缺失、状态不一致、回调丢失 | 渠道未回调的静默单、跨日迟到 | 最高（每笔都算） | Flink、Kafka Streams、Debezium |
| **L2 准实时汇总** | 分钟级（1~5min） | 汇总表 / Redis 计数 / 物化视图 | 渠道 × 分钟 × 币种 | 批量漏记、金额漂移、突增突降 | 一多一少相互抵消 | 中 | 定时 SQL、ClickHouse 物化视图 |
| **L3 日终全量** | T+1（小时级） | 渠道对账文件 + 全量流水 | 全量，按业务主键 | 静默错误、跨日、历史遗漏、重复 | 当日已发生的客户影响（已晚） | 低（批量扫描） | ClickHouse、Spark、MaxCompute |
| **L0 事前校验** | 请求时（毫秒） | 参数 / 状态机 | 单请求 | 非法金额、状态不允许、重复提交 | 任何「已发生但无异常」的错误 | 极低 | 唯一索引、状态机守卫 |
| **对账后审计** | T+7 / 月结 | 差异表 + 工单 + 原始凭证 | 案件 | 处理是否合规、是否有二次差错 | 已造成的损失 | 人力 | 工单系统、审计报表 |

> 💡 **L0 不是对账，但它决定了 L1~L3 的工作量。** 一个团队如果 L0 做得好，日终差异应该稳定在 0.001% 以下；如果日终差异率长期 > 0.05%，先回去修 L0（幂等、状态机、校验），别加人对账——**对账是最后一道网，不是主要防线**。

**差异处理流程**：

```
发现差异
  ├─ 时间差/晚到 → HOLD（挂账），等到截止时间再判
  ├─ 长款（渠道有、我方无；多记）→ 补记 / 挂账 / 联系渠道
  ├─ 短款（我方有、渠道无；漏记）→ 冲正 / 重查 / 工单
  ├─ 状态不一致 → 以渠道/权威源为准，推进状态机
  ├─ 金额不一致 → 冻结，人工核对（禁止自动改金额）
  └─ 重复 → 冲正多余分录，保留审计
```

---

## 14.4 怎么实现：文件、引擎、亿级 SQL、断点续跑

### 14.4.1 对账文件格式：头、体、尾、校验和

渠道文件通常是 CSV/定长文本，必须带**头尾和校验**，否则你无法判断「文件是不是完整」。

```
H,20250930,CHANNEL_A,CNY,000003,000000000000000123.45,9f2c...
D,PAY202509300001,ORDER001,100.00,SUCCESS,2025-09-30 10:00:01
D,PAY202509300002,ORDER002,23.45,SUCCESS,2025-09-30 10:00:03
T,000003,000000000000000123.45
```

| 行 | 含义 | 校验 |
| --- | --- | --- |
| `H` | 文件头：日期、渠道、币种、记录数、金额合计、MD5 | 与尾部/实际解析结果比对 |
| `D` | 明细：渠道流水号、业务单号、金额、状态、时间 | 逐行解析，字段数/格式校验 |
| `T` | 文件尾：记录数、金额合计 | 与头、实际解析结果三方一致 |

**解析规则**：

1. 先读头，拿到 `record_count` / `amount_sum` / `checksum`；文件级不合法直接拒收，进异常队列。
2. 流式读明细，边读边累计 `count` / `sum` / `hash`；不要一次全加载进内存。
3. 读完比对尾部与累计值；不一致整文件拒收，不能「部分入库」。
4. `file_id`（渠道+日期+文件名/序号）唯一，重复文件直接跳过；补文件要版本号。

### 14.4.2 Java：流式解析 + 批量入库

```java
@Service
public class ReconFileParser {
    @Resource private TransactionTemplate tx;
    @Resource private ReconDetailMapper detailMapper;
    @Resource private ReconFileMapper fileMapper;

    public ReconFile parse(Path path, String channel, LocalDate date) throws IOException {
        Header h = readHeader(path);                      // 记录数、金额、MD5
        validate(path, h);                                // 第一遍：只校验，不写库
        try {
            tx.executeWithoutResult(st -> load(path, h)); // 第二遍：所有批次同一事务
        } catch (RuntimeException e) {
            fileMapper.markFailed(h.fileId(), e.getMessage());
            throw e;
        }
        fileMapper.markParsed(h.fileId(), h.recordCount(), h.amountSum());
        return new ReconFile(h.fileId(), h.recordCount(), h.amountSum());
    }

    private Header readHeader(Path path) throws IOException {
        try (BufferedReader reader = Files.newBufferedReader(path, StandardCharsets.UTF_8)) {
            return Header.parse(reader.readLine());
        }
    }

    private void validate(Path path, Header h) throws IOException {
        long count = 0;
        BigDecimal sum = BigDecimal.ZERO;
        try (BufferedReader reader = Files.newBufferedReader(path, StandardCharsets.UTF_8)) {
            reader.readLine();
            String line;
            while ((line = reader.readLine()) != null && !line.startsWith("T,")) {
                ReconDetail d = ReconDetail.parse(line);  // 字段数/金额/时间格式校验
                count++;
                sum = sum.add(d.amount());
            }
        }
        if (count != h.recordCount() || sum.compareTo(h.amountSum()) != 0) {
            throw new IllegalStateException("file checksum mismatch: " + path);
        }
    }

    private void load(Path path, Header h) {
        try (BufferedReader reader = Files.newBufferedReader(path, StandardCharsets.UTF_8)) {
            reader.readLine();
            List<ReconDetail> batch = new ArrayList<>(1000);
            String line;
            while ((line = reader.readLine()) != null && !line.startsWith("T,")) {
                batch.add(ReconDetail.parse(line));
                if (batch.size() == 1000) {
                    detailMapper.batchInsert(h.fileId(), batch);
                    batch.clear();
                }
            }
            if (!batch.isEmpty()) detailMapper.batchInsert(h.fileId(), batch);
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }
}
```

> ⚠️ 解析失败要「整文件回滚 + 记录失败原因」，不能把半截数据留在标准明细表里；否则对账引擎会把「解析残缺」当成「渠道少数据」，产生大量假差异。文件很大时，把批次写入带 `file_id` 的 staging 表，校验通过后再原子切换；标准明细表还要有 `uk(file_id, channel_txn)`，否则 `markParsed` 失败后重跑会重复入库。

### 14.4.3 差异表与处理表（完整 DDL）

```sql
CREATE TABLE reconciliation_diff (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  recon_date     DATE            NOT NULL,
  channel        VARCHAR(32)     NOT NULL,
  biz_no         VARCHAR(64)     NOT NULL COMMENT '业务主键',
  diff_type      ENUM('LONG','SHORT','STATUS_MISMATCH','AMOUNT_MISMATCH','DUPLICATE')
                 NOT NULL,
  biz_amount     DECIMAL(18,2)   NULL,
  channel_amount DECIMAL(18,2)   NULL,
  biz_status     VARCHAR(32)     NULL,
  channel_status VARCHAR(32)     NULL,
  handle_status  ENUM('OPEN','HANDLING','AUTO_FIXED','MANUAL','IGNORED') NOT NULL DEFAULT 'OPEN',
  handle_type    ENUM('REVERSAL','SUPPLEMENT','TICKET','NONE') NOT NULL DEFAULT 'NONE',
  handler        VARCHAR(64)     NULL COMMENT '认领人/节点',
  ticket_id      VARCHAR(64)     NULL,
  remark         VARCHAR(512)    NULL,
  created_at     DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at     DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                 ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_diff (recon_date, channel, biz_no, diff_type),
  KEY idx_handle (handle_status, recon_date),
  KEY idx_ticket (ticket_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE recon_file (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  file_id      VARCHAR(128)    NOT NULL COMMENT '渠道+日期+文件序号',
  channel      VARCHAR(32)     NOT NULL,
  recon_date   DATE            NOT NULL,
  status       ENUM('RECEIVED','PARSED','FAILED','RECONCILED','SUPERSEDED') NOT NULL,
  record_count BIGINT          NOT NULL DEFAULT 0,
  amount_sum   DECIMAL(20,2)   NOT NULL DEFAULT 0,
  checksum     VARCHAR(64)     NULL,
  last_error   VARCHAR(512)    NULL,
  created_at   DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_file (file_id),
  KEY idx_date_channel (recon_date, channel)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE recon_shard (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  job_id      VARCHAR(64)     NOT NULL,
  shard_no    INT             NOT NULL,
  status      ENUM('PENDING','RUNNING','DONE','FAILED') NOT NULL DEFAULT 'PENDING',
  diff_count  BIGINT          NOT NULL DEFAULT 0,
  updated_at  DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
              ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_job_shard (job_id, shard_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE recon_daily_stat (
  recon_date  DATE         NOT NULL,
  channel     VARCHAR(32)  NOT NULL,
  total_count BIGINT       NOT NULL,
  diff_count  BIGINT       NOT NULL,
  updated_at  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
              ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (recon_date, channel)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 14.4.4 亿级流水怎么核：分片并行 + 汇总校验 + ClickHouse

**第一步：汇总校验（便宜，先发现整体漂移）**

```sql
-- 业务侧与渠道侧各跑一遍，按商户/币种汇总；count/sum/checksum 任一不等即异常
SELECT merchant_id,
       currency,
       count()                              AS cnt,
       sum(amount)                          AS amt,
       groupBitXor(cityHash64(concat(biz_no, '|', toString(amount), '|', status))) AS checksum
  FROM biz_flow
 WHERE dt = '2025-09-30'
 GROUP BY merchant_id, currency;

SELECT merchant_id,
       currency,
       count()                              AS cnt,
       sum(amount)                          AS amt,
       groupBitXor(cityHash64(concat(biz_no, '|', toString(amount), '|', status))) AS checksum
  FROM channel_flow
 WHERE dt = '2025-09-30'
 GROUP BY merchant_id, currency;
```

> 💡 `checksum` 用 `groupBitXor` 而不是 `sum`，避免 UInt64 溢出；它只用于「快速发现两边不一致」，不能定位具体哪一笔，定位要靠明细比对。

**第二步：明细比对（分片并行）**

```sql
-- 分片键：物化列 shard = cityHash64(biz_no) % 64；每个分片独立跑，互不依赖
-- ① 短款（漏记）：业务有、渠道无
SELECT b.biz_no, 'SHORT' AS diff_type, b.amount AS biz_amount,
       CAST(NULL AS Nullable(Decimal(18,2))) AS channel_amount,
       b.status AS biz_status, CAST(NULL AS Nullable(String)) AS channel_status
  FROM biz_flow b
  LEFT JOIN channel_flow c
    ON b.biz_no = c.biz_no AND c.dt = '2025-09-30' AND c.shard = b.shard
 WHERE b.dt = '2025-09-30'
   AND c.biz_no IS NULL
   AND b.shard = {shard:UInt8}

UNION ALL

-- ② 长款（多记）：渠道有、业务无
SELECT c.biz_no, 'LONG', CAST(NULL AS Nullable(Decimal(18,2))),
       c.amount, CAST(NULL AS Nullable(String)), c.status
  FROM channel_flow c
  LEFT JOIN biz_flow b
    ON b.biz_no = c.biz_no AND b.dt = '2025-09-30' AND b.shard = c.shard
 WHERE c.dt = '2025-09-30'
   AND b.biz_no IS NULL
   AND c.shard = {shard:UInt8}

UNION ALL

-- ③ 状态/金额不一致（匹配上的）
SELECT b.biz_no,
       if(b.status != c.status, 'STATUS_MISMATCH', 'AMOUNT_MISMATCH') AS diff_type,
       b.amount, c.amount, b.status, c.status
  FROM biz_flow b
  INNER JOIN channel_flow c
    ON b.biz_no = c.biz_no AND c.dt = '2025-09-30' AND c.shard = b.shard
 WHERE b.dt = '2025-09-30'
   AND (b.status != c.status OR b.amount != c.amount)
   AND b.shard = {shard:UInt8};
```

**为什么用 `UNION ALL` 而不是 `FULL OUTER JOIN`**：亿级数据下 `FULL OUTER JOIN` 要把两边都放进内存/临时表，容易 OOM；三次 `LEFT/INNER JOIN` 各自可以利用 `(dt, shard, biz_no)` 的排序做 merge join，内存可控，且能分别限流。小数据量（<1,000 万）用 `FULL OUTER JOIN` 更简洁，ClickHouse 22.x+ 支持，以你的版本为准。

**第三步：Go 分片 worker（并行 + 断点）**

```go
// 依赖 clickhouse-go v2；diffSQL 里的 {shard:UInt8} 用 clickhouse.Named 绑定
type DiffRow struct {
	BizNo         string
	DiffType      string
	BizAmount     sql.NullString // SQL 里 toString(amount)，避免 Decimal 扫描类型差异
	ChannelAmount sql.NullString
	BizStatus     sql.NullString
	ChannelStatus sql.NullString
}

func RunRecon(ctx context.Context, jobID string, shards int) error {
	sem := make(chan struct{}, 8) // 并发 8，避免打满 ClickHouse
	var wg sync.WaitGroup
	errCh := make(chan error, shards)

	for i := 0; i < shards; i++ {
		wg.Add(1)
		go func(shard int) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()

			// 断点：已 DONE 的分片直接跳过；RUNNING 超时的重新认领
			if done, _ := repo.ShardDone(ctx, jobID, shard); done {
				return
			}
			if err := repo.MarkRunning(ctx, jobID, shard); err != nil {
				errCh <- err
				return
			}
			rows, err := ch.Query(ctx, diffSQL, clickhouse.Named("shard", uint8(shard)))
			if err != nil {
				errCh <- fmt.Errorf("shard %d: %w", shard, err)
				return
			}
			defer rows.Close()

			batch := make([]DiffRow, 0, 1000)
			total := 0
			for rows.Next() {
				var d DiffRow
				if err := rows.Scan(&d.BizNo, &d.DiffType, &d.BizAmount,
					&d.ChannelAmount, &d.BizStatus, &d.ChannelStatus); err != nil {
					errCh <- fmt.Errorf("shard %d scan: %w", shard, err)
					return
				}
				batch = append(batch, d)
				if len(batch) == 1000 {
					if err := diffWriter.BatchInsert(ctx, batch); err != nil {
						errCh <- err
						return
					}
					total += len(batch)
					batch = batch[:0]
				}
			}
			if err := rows.Err(); err != nil {
				errCh <- fmt.Errorf("shard %d rows: %w", shard, err)
				return
			}
			if len(batch) > 0 {
				if err := diffWriter.BatchInsert(ctx, batch); err != nil {
					errCh <- err
					return
				}
				total += len(batch)
			}
			if err := repo.MarkDone(ctx, jobID, shard, total); err != nil {
				errCh <- err
			}
		}(i)
	}
	wg.Wait()
	close(errCh)
	return firstError(errCh)
}
```

**分片数怎么定**：按「单分片 500 万~1,000 万行、单分片耗时 1~3 分钟」反推。10 亿行 → 100~200 个分片；并发 8~16，避免把 ClickHouse 的 CPU/内存打满。分片键必须稳定（`biz_no` 哈希），否则断点续跑会对不上。

### 14.4.5 差异处理：自动冲正 / 自动补记 / 人工工单

| 差异类型 | 判定 | 自动处理 | 人工介入条件 |
| --- | --- | --- | --- |
| 长款（渠道有、我方无，多记） | 渠道成功、我方无记录 | 补记分录/补单（幂等） | 金额 > 1 万、跨月、重复长款 |
| 短款（我方有、渠道无，漏记） | 我方成功、渠道无记录 | 冲正（反向分录） | 已发货/已结算/金额 > 1 万 |
| 状态不一致 | 金额同、状态不同 | 以权威源推进状态机 | 状态不可逆（已退款/已关闭） |
| 金额不一致 | 状态同、金额/手续费不同 | **禁止自动改金额**，冻结 | 全部人工 |
| 重复 | 同一 biz_no 多条 | 冲正多余分录 | 涉及已结算/已开票 |

```sql
-- 自动冲正：不删除原分录，插入反向分录；diff 标记 AUTO_FIXED
INSERT INTO ledger_entry(biz_no, entry_no, account_id, direction, amount, remark)
SELECT biz_no, 99, account_id, IF(direction='DEBIT','CREDIT','DEBIT'), amount, 'AUTO_REVERSAL'
  FROM ledger_entry
 WHERE biz_no = ? AND entry_no = 1;

UPDATE reconciliation_diff
   SET handle_status='AUTO_FIXED', handle_type='REVERSAL', updated_at=NOW(3)
 WHERE id=? AND handle_status='OPEN';

-- 人工工单：金额不一致/跨月/已结算，一律进工单，不自动改
INSERT INTO ops_ticket(ticket_type, biz_no, amount, reason, status)
VALUES ('RECON_DIFF', ?, ?, ?, 'OPEN');
```

### 14.4.6 调度与断点续跑

```yaml
# Kubernetes CronJob：T+1 02:00 跑日终对账；文件未到则等待并告警
apiVersion: batch/v1
kind: CronJob
metadata:
  name: daily-reconciliation
spec:
  schedule: "0 2 * * *"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      backoffLimit: 3
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: recon
              image: registry.example.com/recon:2.1.0
              args: ["--date=yesterday", "--shards=128", "--concurrency=8"]
              env:
                - name: CLICKHOUSE_DSN
                  valueFrom: { secretKeyRef: { name: clickhouse, key: dsn } }
                - name: CHANNEL_FILE_DIR
                  value: /data/recon
```

**断点续跑规则**：

1. `recon_file` 按 `file_id` 唯一：重复文件跳过；补文件用新版本号，旧版本标记 `SUPERSEDED`。
2. `recon_shard` 记录每个分片状态：`DONE` 跳过，`RUNNING` 超过 10 分钟视为超时，重新认领。
3. 差异表 `uk_diff` 唯一：重跑只更新状态，不重复插入。
4. 整个 job 幂等：同一天重跑 N 次，结果一致；重跑前先 `dry-run` 对比差异数。

### 14.4.7 机制下沉：ClickHouse 凭什么核得动 10 亿行

「上 ClickHouse 就快了」是一句危险的话。它快在这三个具体机制上，任何一条被破坏，亿级对账会退化成 9 小时跑批（本章开头那种）：

```
MergeTree 的物理结构（单分区 dt=2025-09-30）

  part 目录: .../all_1_1_0/
    ├── primary.idx      稀疏索引：每 8192 行（index_granularity）一个 key
    │                    key = 排序键前缀 (dt, shard, biz_no)
    ├── mrk.mark         每列每个 granule 的偏移（granule = 8192 行的压缩块）
    ├── bin              压缩列数据（按排序键有序）
    └── count.txt

  查询 WHERE dt=? AND shard=7 时：
    ① 分区裁剪：只读 dt 对应分区（其他日期整目录跳过）
    ② 主键裁剪：用 primary.idx 二分定位 (dt, shard) 的 granule 范围
    ③ 顺序扫描：只读 shard=7 的 granule，不做全表随机 IO
    ④ 合并连接：分片内两侧都按 biz_no 有序 → merge join，内存 O(1) 而非 O(N)
```

```sql
-- 完整 DDL：物化 shard 做分片裁剪，排序键再按 biz_no，分片内可走 merge join
CREATE TABLE recon.biz_flow (
  dt          Date,
  biz_no      String,
  shard       UInt8 MATERIALIZED toUInt8(cityHash64(biz_no) % 64),
  channel     LowCardinality(String),
  merchant_id UInt64,
  amount      Decimal(18,2),
  status      LowCardinality(String),
  updated_at  DateTime64(3)
) ENGINE = MergeTree
PARTITION BY toYYYYMM(dt)
ORDER BY (dt, shard, biz_no)
SETTINGS index_granularity = 8192;

CREATE TABLE recon.channel_flow (
  dt          Date,
  biz_no      String,
  shard       UInt8 MATERIALIZED toUInt8(cityHash64(biz_no) % 64),
  channel     LowCardinality(String),
  channel_txn String,
  amount      Decimal(18,2),
  status      LowCardinality(String),
  updated_at  DateTime64(3)
) ENGINE = MergeTree
PARTITION BY toYYYYMM(dt)
ORDER BY (dt, shard, biz_no)
SETTINGS index_granularity = 8192;

-- 致命检查：物化 shard 必须与分片函数一致；改成 %128 只改一侧 = 全量假差异
SELECT 'biz' AS tbl, count() AS bad_bucket
  FROM recon.biz_flow
 WHERE dt = '2025-09-30' AND shard != toUInt8(cityHash64(biz_no) % 64)
UNION ALL
SELECT 'channel', count()
  FROM recon.channel_flow
 WHERE dt = '2025-09-30' AND shard != toUInt8(cityHash64(biz_no) % 64);
-- 期望两行都是 0；非 0 说明物化列/分片函数不一致，差异率会飙到几十个百分点
```

```xml
<!-- users.xml：对账专用 profile。加在 <profiles> 下，按用户绑定 -->
<profiles>
  <recon>
    <max_memory_usage>30000000000</max_memory_usage>   <!-- 30GB，超了直接失败而不是 OOM 整机 -->
    <max_bytes_in_join>8000000000</max_bytes_in_join>  <!-- hash join 上限 8GB -->
    <join_algorithm>partial_merge</join_algorithm>     <!-- 两侧有序时走 merge，内存可控 -->
    <join_use_nulls>1</join_use_nulls>                 <!-- ★ LEFT JOIN 未匹配必须为 NULL -->
    <max_threads>8</max_threads>                       <!-- 与分片并发相乘，别打满 -->
    <max_block_size>65536</max_block_size>
    <optimize_read_in_order>1</optimize_read_in_order>
  </recon>
</profiles>
```

**`join_use_nulls` 这个参数值 0 和 1 的差别，是「几百条差异」和「几百万条假差异」的差别**：默认值 0 时，`LEFT JOIN` 未匹配的右表列会填 `0`/`''` 而不是 `NULL`，于是 `b.amount != c.amount` 对每一笔「渠道无记录」都成立——你会得到一堆 `AMOUNT_MISMATCH`，把真正的短款埋在噪音里。**要么显式设 `join_use_nulls=1`，要么一律用 `c.biz_no IS NULL` 判存在性，不要依赖列值。**

**为什么不用 `FULL OUTER JOIN` 一把梭**：它要求两侧都进内存（或落磁盘），10 亿 × 10 亿会直接触发 `max_bytes_in_join` 限制；而三次 `LEFT/INNER JOIN` 可以分别下推过滤、分别限流、分别重试。**分片 + UNION ALL 的本质是把「一个大 join」换成「N 个小 join」，每个小 join 都能独立失败、独立重跑。**

### 14.4.8 Java：差异处理器的三件套（认领、限额、幂等冲正）

差异处理比对比更危险——**比错只是多几条假差异，改错是真金白银**。所以处理器必须：① 用条件更新认领差异（防两个节点同时处理）；② 金额超阈值一律转工单；③ 冲正凭证号由 `diffId` 派生，重复执行被唯一键挡住。

```java
@Service
public class DiffHandler {

    @Resource private TransactionTemplate tx;
    @Resource private DiffMapper diffMapper;
    @Resource private LedgerMapper ledgerMapper;
    @Resource private TicketClient ticketClient;

    private static final BigDecimal AUTO_LIMIT = new BigDecimal("10000.00");

    public HandleResult handle(long diffId, boolean dryRun) {
        Diff d = diffMapper.get(diffId);
        if (d == null) return HandleResult.notFound(diffId);

        // ① 认领：条件更新，谁抢到谁处理（两个节点同时跑也只有一个生效）
        if (!dryRun && diffMapper.claim(diffId) != 1) {
            return HandleResult.skipped("already claimed");
        }

        // ② 金额/跨月/已结算 → 一律人工，绝不自动改金额
        if (d.getDiffType() == DiffType.AMOUNT_MISMATCH
                || d.getAmount().compareTo(AUTO_LIMIT) > 0
                || d.isCrossMonth() || d.isSettled()) {
            if (dryRun) return HandleResult.wouldTicket(diffId);
            ticketClient.createReconTicket(d);          // 幂等键 = diffId
            diffMapper.markManual(diffId, "over limit or amount mismatch");
            return HandleResult.ticketed(diffId);
        }

        // ③ 冲正：凭证号由 diffId 派生，重复执行靠 uk(entry_no) 幂等
        if (dryRun) return HandleResult.wouldFix(diffId, d.getDiffType());
        tx.executeWithoutResult(st -> {
            String entryNo = "REV" + d.getReconDate().toString().replace("-", "") + "-" + diffId;
            int inserted = ledgerMapper.insertReversal(entryNo, d.getBizNo(), d.getAmount());
            if (inserted == 0) {                        // 已经冲过：幂等返回，不报错
                return;
            }
            diffMapper.markAutoFixed(diffId, "REVERSAL", entryNo);
        });
        return HandleResult.fixed(diffId);
    }
}
```

```sql
-- claim 的完整语句：只有 OPEN 才能被认领，顺手写 handler 与时间，便于挂账排查
UPDATE reconciliation_diff
   SET handle_status = 'HANDLING', handler = ?, updated_at = NOW(3)
 WHERE id = ? AND handle_status = 'OPEN';

-- 冲正凭证：entry_no 唯一 → 重复执行插入 0 行（幂等），不删除原分录（第 3 章）
INSERT IGNORE INTO ledger_entry(entry_no, biz_no, account_id, direction, amount, remark)
SELECT ?, biz_no, account_id, IF(direction = 'DEBIT', 'CREDIT', 'DEBIT'), amount, 'AUTO_REVERSAL'
  FROM ledger_entry WHERE biz_no = ? AND entry_no = 1;

-- 兜底巡检：认领后超过 30 分钟没结论的，回收到 OPEN（防止节点崩溃把差异永久占住）
UPDATE reconciliation_diff
   SET handle_status = 'OPEN', handler = NULL
 WHERE handle_status = 'HANDLING' AND updated_at < NOW(3) - INTERVAL 30 MINUTE;

-- 每日差异分布：处理策略是否退化，一眼看出发（自动率掉 = 判定条件被收紧）
SELECT recon_date, diff_type, handle_status, COUNT(*) AS cnt,
       SUM(COALESCE(biz_amount, channel_amount)) AS amt
  FROM reconciliation_diff
 WHERE recon_date >= CURDATE() - INTERVAL 7 DAY
 GROUP BY recon_date, diff_type, handle_status
 ORDER BY recon_date DESC, cnt DESC;
```

> ⚠️ `dry-run` 不是可选项。上线新规则、调整阈值、改分片函数之前，先 `dry-run` 看「会动多少钱、会影响多少笔」，把结果贴进变更单。**没有 dry-run 的自动冲正，等于给生产库配了一把自动手枪。**

---

## 14.5 双修对照：传统 vs Agent（金额对账 → token 对账）

| 维度 | 传统系统 | Agent 系统 | 同 or 异 |
| --- | --- | --- | --- |
| 对账对象 | 支付/订单/账务金额 | **token 用量、模型调用次数、工具调用次数、成本** | 同构，单位不同 |
| 两个独立源 | 业务库 vs 渠道文件 | 内部用量表 vs 供应商账单/Usage API | 同构 |
| 业务主键 | `biz_no` / `payment_no` | `run_id + step_id + model` | 同构，键多一层 |
| 差异分类 | 长款/短款/状态/金额 | 多计/少计/漏记/重复计费/模型价格错 | 同构，多两类 |
| 自动处理 | 冲正/补记 | 退费/补扣/信用额度调整 | 同构，落到账单 |
| 人工处理 | 工单 | 客户成功 + 财务 + 审计 | 同构 |
| 验证方法 | 日终全量 | 日终/月结全量 + 抽样调用日志 | 框架相同 |
| 失败模式 | 文件缺失/晚到/解析错 | API 延迟/分页漏拉/价格版本错/重试重复计费 | 多两类 |

**哪些完全相同、可以直接复用**：三层对账结构、差异表 DDL、`UNION ALL` 分片 SQL、断点续跑、差异率/自动处理率/挂账时长指标、工单流程。**Agent 只是把「金额」换成「token × 单价」，把「渠道文件」换成「供应商 Usage API / 账单」。**

Agent 侧的三条额外纪律：

1. **用量表按 `step_id` 唯一**：重试只允许一条用量记录；这是事故二的直接修复。
2. **按「模型 + 价格版本」对账**：供应商调价、缓存折扣、批量折扣都会让「单价 × token」不等于账单；价格版本必须落库。
3. **对账要能下钻到原始调用**：客户质疑账单时，能给出 `run_id/step_id/request_id/token_in/token_out/model/prompt_version`，否则无法自证。

```go
// Agent 用量对账：内部 vs 供应商，按 run_id + model 汇总
type UsageRow struct {
	RunID      string
	Model      string
	TokensIn   int64
	TokensOut  int64
	Calls      int64
	CostUSD    float64
}

func ReconcileUsage(ctx context.Context, day string) ([]Diff, error) {
	internal, err := usageRepo.Aggregate(ctx, day)   // 内部用量表
	if err != nil { return nil, err }
	provider, err := providerClient.Usage(ctx, day)  // 供应商账单/Usage API
	if err != nil { return nil, err }

	diffs := make([]Diff, 0)
	for key, in := range internal {
		p := provider[key]
		if p.Calls == 0 {
			diffs = append(diffs, Diff{Key: key, Type: "MISSING_PROVIDER"})
			continue
		}
		if in.TokensIn != p.TokensIn || in.TokensOut != p.TokensOut {
			diffs = append(diffs, Diff{Key: key, Type: "TOKEN_MISMATCH", Internal: in, Provider: p})
		}
		if abs(in.CostUSD-p.CostUSD) > 0.01 {
			diffs = append(diffs, Diff{Key: key, Type: "COST_MISMATCH", Internal: in, Provider: p})
		}
	}
	return diffs, nil
}
```

> ⚠️ 模型输出是非确定性的，**不要对账「输出内容」**；对账的是「调用次数、token 数、单价、成本」。内容质量靠评测，不靠对账。

---

## 14.6 会在哪炸：失效模式清单

| 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 日终差异 3 万笔，全是假差异 | 渠道文件晚到/只到了一半 | 查 `recon_file.status` 与文件记录数 | 等文件齐再跑；补文件 | 文件未到/记录数不符直接阻断，不跑对账 |
| 总数平了，单笔错了 | 只做汇总对账，没做明细 | 抽样明细比对 | 补明细对账 | 三层对账，汇总只做预警 |
| 同一文件被跑两次，差异翻倍 | `file_id` 不唯一/没幂等 | 查 `recon_file` 重复 | 去重 + 差异表唯一键 | 文件级幂等 |
| 差异率突然 10 倍 | 解析字段错位/编码错 | 对比原始文件与入库字段 | 修解析器，重跑 | 文件头尾校验 + 抽样断言 |
| 自动冲正把正常交易冲了 | 差异判定错/金额精度 | 查 `handle_type=REVERSAL` 的工单 | 反向冲回 + 审计 | 金额不一致禁止自动处理；dry-run |
| 跨月差异没人管 | 没有挂账时长指标 | 查 `handle_status='OPEN'` 的最老记录 | 建工单 + 升级 | 挂账时长 > 24h 告警 |
| Agent 账单多计 2.3% | 用量表没按 `step_id` 唯一 | 对比内部用量与供应商账单 | 退费 + 修唯一索引 | 用量幂等 + 月结对账 |
| 对账跑 8 小时还没完 | 单机全量、无分片 | 看分片耗时/ClickHouse 内存 | 分片 + 并发 + 汇总预筛 | 按数据量定分片数，压测验证 |

---

## 14.7 怎么验证：造差异、看指标、压亿级

**① 造差异**：在测试环境故意制造四类差异各 100 笔（长款、短款、状态、金额），跑对账，断言：

- 差异表恰好 400 条，分类正确；
- 长款/短款自动处理 200 条，金额不一致全部进工单；
- 重跑一次，差异表不新增（幂等）。

**② 对账 SQL 自检**：

```sql
-- 差异率 = 差异笔数 / 应比对笔数
SELECT recon_date, channel,
       SUM(diff_count) / SUM(total_count) AS diff_rate
  FROM recon_daily_stat
 WHERE recon_date >= CURDATE() - INTERVAL 7 DAY
 GROUP BY recon_date, channel;

-- 自动处理率
SELECT recon_date,
       SUM(handle_status='AUTO_FIXED') / SUM(handle_status IN ('AUTO_FIXED','MANUAL','OPEN')) AS auto_rate
  FROM reconciliation_diff
 WHERE recon_date = CURDATE() - INTERVAL 1 DAY
 GROUP BY recon_date;

-- 挂账时长（最老未处理差异）
SELECT MIN(created_at) AS oldest_open,
       TIMESTAMPDIFF(HOUR, MIN(created_at), NOW()) AS oldest_hours
  FROM reconciliation_diff
 WHERE handle_status='OPEN';
```

**③ 亿级压测**：造 10 亿行业务流水 + 10 亿行渠道流水，128 分片、并发 8，目标：

- 汇总校验 < 3 分钟；
- 明细比对 < 30 分钟；
- ClickHouse 内存峰值 < 单节点 60%；
- 差异写入 < 100 万条/小时。

**④ 观测指标**：

| 指标 | 含义 | 告警线 |
| --- | --- | --- |
| `recon_diff_rate` | 差异率 | > 0.01% 持续 2 天 |
| `recon_auto_handle_rate` | 自动处理率 | < 90% |
| `recon_open_age_hours` | 最老挂账时长 | > 24 小时 |
| `recon_file_delay_minutes` | 文件到达延迟 | > 60 分钟 |
| `recon_job_duration_seconds` | 对账耗时 | > 2 小时 |
| `recon_shard_failed` | 失败分片数 | > 0 |

---

## 14.8 用什么工具

| 需求 | 工具 | 边界 |
| --- | --- | --- |
| 明细存储/比对 | ClickHouse | 亿级扫描快；不适合点查/事务更新 |
| 文件传输 | SFTP / S3 / 对象存储 | 需要校验、版本、补文件机制 |
| 调度 | Kubernetes CronJob / Airflow / DolphinScheduler | 断点、重跑、依赖管理要自己补 |
| 流式对账 | Flink / Kafka Streams | 低延迟；窗口/晚到数据要处理 |
| 差异工单 | 自研 / Jira / 内部运营平台 | 必须和差异表双向关联 |
| 指标 | Prometheus + Grafana | 差异率、自动处理率、挂账时长 |

> ⚠️ 自研边界：**对账引擎值得自研**（业务规则、差异分类、处理流程都在你手里）；**文件传输/调度/存储不要自研**。对账的难点不是「比对」，是「差异之后怎么办」。

---

## 14.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 对账本质 | 两个独立记录源按业务主键交叉验证；核总数会漏单笔 |
| 三层 | 实时明细（秒~分钟）、准实时汇总（5 分钟~小时）、日终全量（T+1） |
| 差异分类 | 长款、短款、状态不一致、金额不一致、重复 |
| 处理 | 长款（多记）补记、短款（漏记）冲正；金额不一致禁止自动改，必须人工 |
| 亿级 | 分片（`cityHash64(biz_no)%N`）+ 汇总校验 + `UNION ALL` 明细比对 |
| 文件 | 头/体/尾 + 记录数 + 金额合计 + checksum；整文件校验失败拒收 |
| 断点 | `recon_file` 唯一、`recon_shard` 状态、差异表唯一键；重跑幂等 |
| 指标 | 差异率、自动处理率、挂账时长、文件延迟、对账耗时 |
| Agent 侧 | 同一框架；对 token/调用次数/成本，不对输出内容 |
| 不做会怎样 | 静默错误积累到月末，财务报表已报出，客户先发现 |

## 14.10 自检题

1. **为什么「总数对平」不能证明账没问题？**
   <details><summary>答案要点</summary>
   两笔方向相反、金额相同的差异可以相互抵消（如一笔长款 100、一笔短款 100），总数仍然为 0。必须按业务主键做明细比对，汇总只用于快速发现整体漂移。
   </details>

2. **渠道文件到了，但记录数/金额合计和文件头不一致，你应该怎么处理？**
   <details><summary>答案要点</summary>
   整文件拒收，标记 `FAILED`，不进入对账；联系渠道补文件/重传，保留原始文件与校验结果。绝不能「先入库再说」，否则会产生大量假差异，且污染后续重跑。
   </details>

3. **Agent 平台按 token 计费，供应商账单比内部多 2.3%，你怎么定位？**
   <details><summary>答案要点</summary>
   按 `run_id + step_id + model` 对账：先查用量表是否按 `step_id` 唯一（重试重复计费）、再查价格版本/缓存折扣/分页漏拉、最后抽样原始调用日志与供应商 request_id 对齐。对账的是次数/token/单价/成本，不是输出内容。
   </details>

## 14.11 延伸阅读

- ClickHouse 文档：`MergeTree`、`cityHash64`、`groupBitXor`、`FULL OUTER JOIN`（以你使用的版本为准）
- Flink 文档：事件时间、水位线、迟到数据与对账窗口
- 支付清算对账文件规范（各渠道商户文档；字段以渠道最新版本为准）
- 上一章：[13. Saga 三连坑：空回滚、悬挂、幂等](13-saga-pitfalls.md)
- 下一章：[15. 限流的本质、算法与层次](15-rate-limit-basics.md)
- 交叉引用：幂等与对账的关系见第 1、3 章；Outbox 见第 10 章；Exactly-once 见第 11 章；跨服务一致性见第 12 章
