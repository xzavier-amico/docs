# 23. 复式记账核心账本引擎（下）：热点账户、冲正与多币种汇兑

> **状态**：生产级工程篇｜**所属**：第三部分·金融工程代码基座｜**编号**：23 / 48
>
> **本章定位**：承接 [22 复式记账核心账本引擎（上）](22-double-entry-ledger-schema-and-concurrency.md) 的科目、分录和防透支模型，解决三个在真实生产中会同时出现的问题：单一结算户成为万级 TPS 热点、错账不能修改却必须可追溯地修正、以及多币种交易不能把 USD 与 CNY 粗暴相加。下游衔接 [24 全链路幂等](24-end-to-end-financial-idempotency.md)、[25 状态机与分布式一致性](25-state-machines-tcc-saga-and-outbox.md)、[27 多层对账](27-multi-tier-reconciliation-and-auto-healing.md)。
>
> **总红线**：所有金额都使用该币种的**最小单位整数**（如 USD cent、CNY fen、BTC satoshi），数据库和消息中使用 `int64`/`BIGINT` 或更宽的整数；严禁 `float`/`double`。已入账分录是不可变事实，禁止 `UPDATE`、`DELETE`、覆盖写或“直接改余额”；错误只能追加红字冲正和蓝字补记。

---

## 23.1 引子：一笔结算户锁住了全平台

### 23.1.1 事故时间线

某交易平台把所有客户手续费收入记到一个 `FEE_INCOME` 账户，把所有商户出款也从一个 `SETTLEMENT_PAYABLE` 账户扣减。账务写入采用“锁住账户行 → 插入分录 → 更新余额 → 提交”的同步路径。

| 时间 | 事件 | 结果 |
|---|---|---|
| 10:00:00 | 行情波动，撮合成交峰值达到 18,000 req/s | 每笔成交都要写同一个结算户 |
| 10:00:01 | 该账户行的锁等待从 2 ms 升至 800 ms | 连接池耗尽，调用方开始超时 |
| 10:00:07 | 超时被错误当作失败，部分调用重试 | 同一业务事实出现重复入账候选 |
| 10:01:12 | 运维手工把余额改回“看起来正确”的数 | 分录和余额失配，审计无法复算 |
| 次日 | 发现一笔手续费少收 1 分、两笔冲正没有原单 | 只能全量人工重建 |

根因不是数据库“太慢”，而是把三个不同性质的动作塞在同一行：高扇入的收入、高扇出的支出、以及必须串行保护的总可用额度。正确做法是把逻辑账户拆成可证明的分桶和收支缓冲，在提交边界内仍保持复式平衡；若确需对全局余额做强一致校验，则使用配额/预留协议，而不是假装随机分片即可解决全局并发。

### 23.1.2 唯一真问题

> **怎样在不修改任何历史分录的前提下，让热点账户可以并行写入，让错误可以审计地修正，让每种币分别借贷平衡，并且在宕机、重试、乱序和对账时仍能从分录重建余额？**

三个常见但错误的答案：

| 反模式 | 为什么不成立 | 生产替代物 |
|---|---|---|
| 直接 `UPDATE balance = balance + x` | 丢失会计事实，无法解释、重放和对账 | 不可变分录 + 余额物化视图 |
| 一个逻辑账户随机复制 N 份即可 | 出账可能同时通过不同桶，合计透支 | 出账配额/预留、桶级余额和确定性分配 |
| 汇兑时把 100 USD 与 720 CNY 放进同一借贷和 | 不同币种单位不能相加，账不平 | 每币种独立平衡的汇兑组 + 过渡户 |
| 错账时改原 `amount` | 破坏原始事实和审计链 | 红字冲正原分录，蓝字补记正确分录 |
| 批量提交后先 ACK，再等数据库落盘 | 进程崩溃会制造“调用方已成功、账本不存在” | 数据库提交成功后才 ACK；超时为 UNKNOWN |

---

## 23.2 不变量、对象与资金流

### 23.2.1 账本不变量

设一条分录为 `e`，币种为 `c`，每条分录行金额 `a` 是严格大于零的最小单位整数：

1. **每币种平衡**：
   \[
   \sum_{line.currency=c, side=DEBIT} a = \sum_{line.currency=c, side=CREDIT} a
   \]
2. **不可变**：`POSTED` 分录、分录行、关联请求和哈希不能更新或删除；修正产生新 `entry_id`。
3. **余额可重建**：
   \[
   balance(account,c)=\sum normal\_delta(entry\_lines)
   \]
   `balance_min` 只是物化结果，不是事实源。
4. **单一事实幂等**：同一 `business_id + posting_type` 至多生成一组已入账分录；重试只返回原结果。
5. **关联原子性**：一条业务事实的所有分录行、余额更新、幂等记录和 Outbox 事件在同一个数据库事务提交，或全部不可见。
6. **币种独立**：任何汇兑组都必须携带 `currency_code`、汇率快照、舍入规则和过渡户；不能用汇率把账面上的两种币“数学相加”。

### 23.2.2 科目、桶和缓冲的三层身份

```text
逻辑科目 account_id
  ├─ INCOME-00 ... INCOME-(N-1)   收入桶：高扇入
  ├─ EXPENSE-00 ... EXPENSE-(N-1) 支出桶：高扇出，必须有已分配额度
  └─ CORE                      核心/归集桶：低频清扫和对账

外部业务事实
  └─ posting_request(idempotency_key)
       └─ entry + lines + bucket balance delta + outbox（同一事务）
```

- **逻辑科目**是报表和权限的身份，例如“平台待付商户”“手续费收入”。
- **物理桶**是并发隔离单元。桶不是另一种钱；桶的余额总和必须等于逻辑科目余额。
- **收入缓冲户**接收高并发入账，先落在收入桶；**支出缓冲户**只从已分配余额中支付，避免一个全局锁承受所有扣款。
- **归集/清扫**是正常的账务转移，不是后台直接改桶余额。清扫也生成两边都有的分录。

### 23.2.3 示例：手续费与商户出款

以下仅展示结构，金额均为最小单位整数。假设手续费收入 `1,250` USD cents：

```text
借：客户应付/结算清算（USD cent） 1,250
贷：手续费收入·INCOME-37        （USD cent） 1,250
```

若从支出缓冲向商户支付 `98,000` CNY fen：

```text
借：商户应付·EXPENSE-12          （CNY fen） 98,000
贷：银行存款/托管资产·EXPENSE-12  （CNY fen） 98,000
```

收入桶和支出桶不会因为“同一商户”而相互抵销；它们必须在同币种内独立平衡。周期清扫可将 `INCOME-37` 转入 `CORE`，但仍须生成可追踪的内部转移分录。

---

## 23.3 热点账户分桶、收支缓冲与 Group Commit

### 23.3.1 分桶设计

#### 路由规则

桶数 `N` 在逻辑科目创建时确定并版本化。对一个业务事实使用稳定哈希，而不是当前时间或随机数：

```text
bucket_index = uint64(SHA-256(logical_account_id || direction || business_id)[0:8]) % N
```

规则：

1. `business_id` 必须是稳定、不可变的业务身份；重试必须命中同一桶。
2. 扩容不能直接把 `N=64` 改成 `N=128`，否则同一事实重放会命中不同桶。采用 `bucket_epoch`：新 epoch 只接新事实，旧 epoch 保留；通过内部转移逐步迁移，迁移完成后才切换读模型。
3. 不能以“总桶余额看起来够”为依据直接从任意支出桶扣款。出账先取得该桶的**支出配额**，或者在一个协调器中把可用额度预留给桶。
4. 对需要账户内严格顺序的聚合，仍使用 [21] 的 `aggregate_seq`；分桶只解决物理热点，不改变业务顺序语义。

#### 收入/支出双缓冲

```text
外部入账 ──► INCOME buckets ──(低频)──► CORE
                                      │
                       配额/预留 ─────┘
                                      ▼
外部出账 ◄── EXPENSE buckets ◄── CORE/资金池
```

- 收入路径只竞争被选中的收入桶，适合支付回执、成交手续费等大量小额写入。
- 支出路径只扣支出桶，并检查该桶的本地可用额度。配额服务从 CORE 原子预留总额后，才能给支出桶增加额度。
- 配额转移也必须是双边分录；“配额”是可支用权限，不是绕开账本的虚拟余额。
- 清扫任务按桶、币种和业务日生成 `SWEEP` 分录；重复执行由 `sweep_id` 唯一键保护。

#### 什么时候不能分桶

若需求是“每一笔出账都必须看到全逻辑账户最新余额，且不能接受预留额度”，则必须牺牲并发，把该账户串行化，或使用单写者日志。分桶只能分散写热点，不能违反全局可用余额约束。

### 23.3.2 Group Commit 的边界

Group Commit 是把**多条独立、已经通过幂等和风控的分录**放入一个数据库事务批量提交，不是把它们合并为一条不可区分的分录。

```text
request A ─┐
request B ─┼─► batcher ─► 单事务：entries + lines + bucket rows + outbox
request C ─┘                 │
                             └─ commit 成功后分别 ACK
```

建议参数（需压测定标）：

| 参数 | 初始值 | 约束 |
|---|---:|---|
| 最大批量 | 128～512 请求 | 控制锁持有时间和 WAL 峰值 |
| 最大等待 | 0.5～2 ms | 延迟敏感路径不能无界攒批 |
| 单事务最大行数 | 5,000～20,000 | 超过则拆批，不能让大单饿死小单 |
| ACK 时机 | DB commit 返回成功后 | commit timeout 为 UNKNOWN，不得直接补偿 |
| 重试 | 仅凭幂等键重放 | 不能产生第二份经济事实 |

收益来自减少每条请求的 fsync、网络往返和事务开销；风险是单批失败影响一批请求、事务锁持有时间变长、数据库 WAL 突发增大。因此要记录每批的 `batch_id`、请求数、行数、锁等待、提交耗时和失败原因。

### 23.3.3 Group Commit 不是异步丢账

必须区分四个时刻：

```text
RECEIVED ─► ACCEPTED ─► DB_COMMITTED ─► ACKED
                         │
                         └─ 进程崩溃：重启扫描 idempotency_key，返回已提交结果
```

- `RECEIVED` 只表示网关收到，不表示账务成功。
- `DB_COMMITTED` 之后才能对外返回成功。
- commit 前超时：调用方得到 `UNKNOWN`，由查单接口收敛；服务端不能猜失败并生成反向账。
- commit 后 ACK 前宕机：重试命中唯一键，返回原 `entry_id`；不能重复记账。

---

## 23.4 DDL：不可变分录、分桶余额、幂等与汇兑快照

以下为 PostgreSQL 15+ 示例。`BIGINT` 保存最小单位整数；金额上限为 `9,223,372,036,854,775,807`，应用层还必须在加减乘除前做溢出检查。若业务可能超过此范围，使用 `NUMERIC(39,0)` 或 Java `BigInteger`/Go `big.Int`，但不能悄悄截断到 `BIGINT`。

### 23.4.1 币种和科目

```sql
CREATE TABLE currency (
  currency_code CHAR(3) PRIMARY KEY,
  minor_unit SMALLINT NOT NULL CHECK (minor_unit >= 0 AND minor_unit <= 18),
  status VARCHAR(16) NOT NULL CHECK (status IN ('ACTIVE','SUSPENDED','RETIRED')),
  version BIGINT NOT NULL DEFAULT 1
);

CREATE TABLE ledger_account (
  account_id UUID PRIMARY KEY,
  account_code VARCHAR(128) NOT NULL UNIQUE,
  account_type VARCHAR(16) NOT NULL
    CHECK (account_type IN ('ASSET','LIABILITY','EQUITY','REVENUE','EXPENSE','TRANSIT','SUSPENSE')),
  normal_side CHAR(1) NOT NULL CHECK (normal_side IN ('D','C')),
  currency_code CHAR(3) NOT NULL REFERENCES currency(currency_code),
  hot BOOLEAN NOT NULL DEFAULT FALSE,
  bucket_epoch BIGINT NOT NULL DEFAULT 1,
  bucket_count INTEGER NOT NULL DEFAULT 1 CHECK (bucket_count > 0),
  allow_negative BOOLEAN NOT NULL DEFAULT FALSE,
  status VARCHAR(16) NOT NULL CHECK (status IN ('OPEN','FROZEN','CLOSED')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE ledger_account_bucket (
  account_id UUID NOT NULL REFERENCES ledger_account(account_id),
  bucket_epoch BIGINT NOT NULL,
  bucket_no INTEGER NOT NULL CHECK (bucket_no >= 0),
  bucket_kind VARCHAR(16) NOT NULL
    CHECK (bucket_kind IN ('CORE','INCOME','EXPENSE','TRANSIT')),
  balance_min BIGINT NOT NULL DEFAULT 0,
  reserved_min BIGINT NOT NULL DEFAULT 0,
  version BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (account_id, bucket_epoch, bucket_no),
  CHECK (balance_min >= -9223372036854775808),
  CHECK (reserved_min >= 0)
);
```

`balance_min` 是按该科目 `normal_side` 归一化后的余额：在正常方向增加时为正，反方向减少时为负。它不是“借方永远为正”；展示报表时必须结合 `normal_side` 转换。

### 23.4.2 分录、分录行和关联关系

```sql
CREATE TABLE ledger_entry (
  entry_id UUID PRIMARY KEY,
  entry_no VARCHAR(64) NOT NULL UNIQUE,
  business_id VARCHAR(128) NOT NULL,
  posting_type VARCHAR(32) NOT NULL,
  entry_kind VARCHAR(16) NOT NULL
    CHECK (entry_kind IN ('ORIGINAL','RED_REVERSAL','BLUE_SUPPLEMENT','SWEEP','FX_LEG','ADJUSTMENT')),
  accounting_date DATE NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  currency_set VARCHAR(256) NOT NULL,
  reversal_of_entry_id UUID REFERENCES ledger_entry(entry_id),
  correction_group_id UUID,
  batch_id UUID,
  content_hash BYTEA NOT NULL,
  status VARCHAR(16) NOT NULL CHECK (status IN ('POSTED','VOIDED_BY_REVERSAL')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CHECK (entry_kind <> 'RED_REVERSAL' OR reversal_of_entry_id IS NOT NULL),
  CHECK (entry_kind <> 'BLUE_SUPPLEMENT' OR correction_group_id IS NOT NULL)
);

CREATE TABLE ledger_line (
  line_id BIGSERIAL PRIMARY KEY,
  entry_id UUID NOT NULL REFERENCES ledger_entry(entry_id),
  line_no SMALLINT NOT NULL,
  account_id UUID NOT NULL REFERENCES ledger_account(account_id),
  bucket_epoch BIGINT NOT NULL,
  bucket_no INTEGER NOT NULL,
  currency_code CHAR(3) NOT NULL REFERENCES currency(currency_code),
  debit_credit CHAR(1) NOT NULL CHECK (debit_credit IN ('D','C')),
  amount_min BIGINT NOT NULL CHECK (amount_min > 0),
  memo VARCHAR(256) NOT NULL,
  UNIQUE (entry_id, line_no),
  UNIQUE (entry_id, account_id, bucket_epoch, bucket_no, currency_code, debit_credit, line_no),
  FOREIGN KEY (account_id, bucket_epoch, bucket_no)
    REFERENCES ledger_account_bucket(account_id, bucket_epoch, bucket_no)
);

CREATE INDEX ledger_line_account_time
  ON ledger_line(account_id, currency_code, entry_id);
CREATE INDEX ledger_entry_business
  ON ledger_entry(business_id, posting_type);
```

数据库 `CHECK` 不能方便地约束“同一 entry、同一 currency 的借贷合计相等”，生产实现必须在事务内校验，并增加延迟约束触发器或独立的入账校验器。禁止只依赖应用层的单元测试。

### 23.4.3 幂等、Outbox 与 FX 快照

```sql
CREATE TABLE ledger_posting_request (
  idempotency_key VARCHAR(192) PRIMARY KEY,
  request_hash BYTEA NOT NULL,
  business_id VARCHAR(128) NOT NULL,
  posting_type VARCHAR(32) NOT NULL,
  state VARCHAR(16) NOT NULL CHECK (state IN ('PROCESSING','POSTED','UNKNOWN','REJECTED')),
  entry_id UUID,
  batch_id UUID,
  response_json JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE ledger_outbox (
  outbox_id BIGSERIAL PRIMARY KEY,
  aggregate_id UUID NOT NULL,
  event_type VARCHAR(64) NOT NULL,
  aggregate_seq BIGINT NOT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  published_at TIMESTAMPTZ,
  UNIQUE (aggregate_id, aggregate_seq)
);

CREATE TABLE fx_quote_snapshot (
  quote_id UUID PRIMARY KEY,
  base_currency CHAR(3) NOT NULL REFERENCES currency(currency_code),
  quote_currency CHAR(3) NOT NULL REFERENCES currency(currency_code),
  rate_numerator NUMERIC(39,0) NOT NULL CHECK (rate_numerator > 0),
  rate_denominator NUMERIC(39,0) NOT NULL CHECK (rate_denominator > 0),
  rounding_mode VARCHAR(24) NOT NULL
    CHECK (rounding_mode IN ('FLOOR','CEILING','HALF_UP','HALF_EVEN')),
  source VARCHAR(64) NOT NULL,
  valid_from TIMESTAMPTZ NOT NULL,
  valid_until TIMESTAMPTZ NOT NULL,
  raw_hash BYTEA NOT NULL,
  CHECK (base_currency <> quote_currency),
  CHECK (valid_from < valid_until)
);

CREATE TABLE fx_trade (
  fx_group_id UUID PRIMARY KEY,
  quote_id UUID NOT NULL REFERENCES fx_quote_snapshot(quote_id),
  base_amount_min BIGINT NOT NULL CHECK (base_amount_min > 0),
  quote_amount_min BIGINT NOT NULL CHECK (quote_amount_min > 0),
  fee_amount_min BIGINT NOT NULL DEFAULT 0 CHECK (fee_amount_min >= 0),
  base_entry_id UUID NOT NULL REFERENCES ledger_entry(entry_id),
  quote_entry_id UUID NOT NULL REFERENCES ledger_entry(entry_id),
  state VARCHAR(16) NOT NULL CHECK (state IN ('POSTED','REVERSED')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
```

### 23.4.4 余额汇总校验 SQL

```sql
-- 以科目配置的 normal_side 将分录折算成正常余额增量。
WITH x AS (
  SELECT l.account_id, l.currency_code,
         CASE WHEN a.normal_side = l.debit_credit
              THEN l.amount_min ELSE -l.amount_min END AS delta
  FROM ledger_line l
  JOIN ledger_entry e ON e.entry_id = l.entry_id AND e.status IN ('POSTED','VOIDED_BY_REVERSAL')
  JOIN ledger_account a ON a.account_id = l.account_id
)
SELECT account_id, currency_code, SUM(delta) AS rebuilt_balance
FROM x GROUP BY account_id, currency_code;
```

线上不要每次请求都扫描历史分录；使用桶余额做读模型，但日终、抽样和故障恢复必须用上述重建结果核对。任何差异先冻结自动修复，再生成调查和冲正任务，不能直接把 `balance_min` 改成查询结果而不留下事实。

---

## 23.5 Go + Java 核心伪代码

伪代码省略鉴权、连接池和日志细节，但保留必须的事务边界。金额参数全部为 `int64`/`long`；汇兑计算在需要时使用宽整数，永不转浮点。

### 23.5.1 Go：桶路由、批量入账与冲正

```go
type Side byte
const (
    Debit Side = 'D'
    Credit Side = 'C'
)

type Line struct {
    AccountID  uuid.UUID
    Epoch      int64
    BucketNo   int32
    Currency   string
    Side       Side
    AmountMin  int64 // > 0，最小单位
    Memo       string
}

type Posting struct {
    IdempotencyKey string
    BusinessID     string
    PostingType    string
    Kind           string // ORIGINAL / RED_REVERSAL / BLUE_SUPPLEMENT
    Lines          []Line
    ReversalOf     *uuid.UUID
    CorrectionID   *uuid.UUID
}

func routeBucket(account uuid.UUID, direction string, businessID string,
                 epoch int64, count int32) int32 {
    // sha256 输入固定编码；epoch/count 来自账户快照，不随重试变化。
    h := sha256.Sum256([]byte(account.String() + "|" + direction + "|" + businessID))
    return int32(binary.BigEndian.Uint64(h[:8]) % uint64(count))
}

func postBatch(ctx context.Context, db *pgxpool.Pool, batch []Posting) error {
    tx, err := db.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
    if err != nil { return err }
    defer tx.Rollback(ctx) // commit 后返回 ErrTxClosed 可忽略

    for _, p := range batch {
        if err := validateIdempotencyAndHash(ctx, tx, p); err != nil { return err }
        if err := validateLinesPositiveAndOverflow(p.Lines); err != nil { return err }
        if err := validateBalancedPerCurrency(p.Lines); err != nil { return err }

        // 幂等键已 POSTED 时只返回原 entry；请求体 hash 不同则拒绝篡改。
        if done, err := alreadyPosted(ctx, tx, p.IdempotencyKey); err != nil || done {
            if err != nil { return err }
            continue
        }

        // 先聚合要锁的桶，并按 (account, epoch, bucket_no) 排序，避免死锁。
        keys := sortedUniqueBucketKeys(p.Lines)
        buckets, err := lockBucketsForUpdate(ctx, tx, keys)
        if err != nil { return err }
        if err := checkAndApplyAvailable(buckets, p.Lines); err != nil { return err }

        entryID := uuid.New()
        hash := canonicalEntryHash(p) // 含顺序、金额、币种、业务日、关联 ID
        if err := insertEntryAndLines(ctx, tx, entryID, p, hash); err != nil { return err }
        if err := updateBucketBalances(ctx, tx, buckets, p.Lines); err != nil { return err }
        if err := markRequestPosted(ctx, tx, p.IdempotencyKey, entryID); err != nil { return err }
        if err := insertOutbox(ctx, tx, entryID, p); err != nil { return err }
    }
    // 只有这里成功，调用方才可收到 SUCCESS；错误必须向上返回 UNKNOWN/失败分类。
    return tx.Commit(ctx)
}

func reverseAndSupplement(ctx context.Context, db *pgxpool.Pool,
                          original Entry, corrected []Line) error {
    // 同一事务：锁原分录只读确认 POSTED，追加相反方向的 RED，再追加 BLUE。
    // 原 entry 的行和金额永不 UPDATE/DELETE。
    red := inverseLines(original.Lines)
    return postAtomicCorrection(ctx, db, original.EntryID, red, corrected,
        "RED_REVERSAL", "BLUE_SUPPLEMENT")
}
```

`Serializable` 不是免费午餐：如果批量冲突率高，可按桶分区、缩短事务、降低批量，或采用 `READ COMMITTED + SELECT FOR UPDATE`。无论选哪种隔离级别，必须处理 serialization failure/deadlock retry；重试使用同一幂等键和相同规范化请求，不能重新生成经济事实。

### 23.5.2 Go：宽整数汇率计算

```go
func convertMinor(base int64, num, den *big.Int, mode Rounding) (int64, error) {
    if base <= 0 || num.Sign() <= 0 || den.Sign() <= 0 { return 0, ErrInvalidRate }
    x := new(big.Int).Mul(big.NewInt(base), num)
    q, r := new(big.Int), new(big.Int)
    q.QuoRem(x, den, r)
    if r.Sign() != 0 && shouldRoundUp(mode, q, r, den) { q.Add(q, big.NewInt(1)) }
    if !q.IsInt64() || q.Sign() <= 0 { return 0, ErrAmountOverflow }
    return q.Int64(), nil
}
```

汇率快照必须先校验 `now ∈ [valid_from, valid_until)`、报价方向和币种最小单位，再计算；不要把 JSON 中的 `0.072` 解析为 `float64` 后乘法。

### 23.5.3 Java 21：批量提交与不可变修正

```java
record LedgerLine(UUID accountId, long epoch, int bucketNo,
                  String currency, Side side, long amountMin, String memo) {}

record Posting(String idempotencyKey, String businessId, String postingType,
               EntryKind kind, List<LedgerLine> lines,
               UUID reversalOf, UUID correctionGroupId) {}

void postBatch(List<Posting> batch) {
    transactionTemplate.executeWithoutResult(status -> {
        for (Posting p : batch) {
            requirePositiveAmounts(p.lines());
            requireNoLongOverflow(p.lines());
            requireBalancedByCurrency(p.lines());

            Existing existing = postingDao.findByKeyForUpdate(p.idempotencyKey());
            if (existing != null) {
                if (!MessageDigest.isEqual(existing.requestHash(), hash(p)))
                    throw new TamperedIdempotencyKey();
                continue; // 返回原 entry，不再写账
            }

            List<BucketKey> keys = sortedDistinctKeys(p.lines());
            Map<BucketKey, Bucket> locked = bucketDao.lockAllForUpdate(keys);
            ensureAndApplyAvailable(locked, p.lines());

            UUID entryId = UUID.randomUUID();
            ledgerDao.insertImmutableEntry(entryId, p, canonicalHash(p));
            ledgerDao.insertLines(entryId, p.lines());
            bucketDao.updateBalances(locked, p.lines());
            postingDao.insertPosted(p, entryId, hash(p));
            outboxDao.insert(entryId, p); // 同一 DB 事务
        }
    }); // Spring 事务提交成功后，网关才发送 ACK
}

void correct(UUID originalId, List<LedgerLine> corrected, UUID groupId) {
    transactionTemplate.executeWithoutResult(status -> {
        Entry original = ledgerDao.findPostedForUpdate(originalId);
        if (original == null) throw new NotPosted();
        if (correctionDao.existsForOriginal(originalId)) return; // 幂等
        List<LedgerLine> red = inverse(original.lines());
        postRedAndBlueInSameTransaction(original, red, corrected, groupId);
    });
}
```

Java 生产代码中使用 `long` 只适用于确定不会溢出的最小单位；需要汇率分子分母、跨币种乘法或超过 `long` 的金额时使用 `BigInteger`。`BigDecimal` 只能用于明确 scale 和 rounding 的展示/规则计算，不能让默认 scale、默认舍入或 `double` 参与账务。

### 23.5.4 红字冲正与蓝字补记的语义

原错误分录：

```text
E100（错误）: 借 客户应付 USD cent 10,000；贷 手续费收入 USD cent 10,000
```

发现实际手续费应为 `8,500`，正确处理是同一 `correction_group_id` 下追加：

```text
E101 RED_REVERSAL: 借 手续费收入 USD cent 10,000；贷 客户应付 USD cent 10,000
E102 BLUE_SUPPLEMENT: 借 客户应付 USD cent 8,500；贷 手续费收入 USD cent 8,500
```

报表可将 E101 标成红字、按负方向展示；物理数据库仍建议使用正的 `amount_min` 和反向借贷行，避免负数金额绕过 `amount_min > 0` 和余额校验。若监管格式要求红字负数，负号只能是报表层语义，不能混入同一账本的金额约束。

冲正规则：

1. 只允许冲正已 `POSTED` 的原单，且只能冲正未冲正部分；部分冲正要记录 `reversed_amount_min` 或子行引用。
2. 原单、红字、蓝字都保留；`reversal_of_entry_id` 和 `correction_group_id` 可沿链追溯。
3. 冲正日期使用实际冲正的会计日，不偷偷把原单时间改到今天或把今天改回过去。
4. 原单跨日、跨期或已关账时，仍追加新分录，按监管规则生成披露/调整标识，不开历史行的“后门”。
5. 红蓝两组是否必须同事务由业务契约决定；对外已发生的支付事实不能因为“补记失败”而回滚原始支付，只能进入待处理/过渡户并由补偿状态机收敛。

---

## 23.6 多币种汇兑、过渡户与 FX 过账

### 23.6.1 不能把不同币种混成一条平衡式

USD cent 和 CNY fen 是两个不同的计量单位。即使市场报价 `1 USD = 7.20 CNY`，也不能写：

```text
借 USD 10,000；贷 CNY 72,000
```

这在每币种维度都不平：USD 借方多 10,000，CNY 贷方多 72,000。汇兑需要一个**每种币都能单独平衡**的结构。

### 23.6.2 过渡户结构

客户用 `10,000 USD cent` 换取 `72,000 CNY fen`，报价快照为 `num=72000, den=10000`，不收手续费：

```text
FX_GROUP G1

USD leg:
  借 客户 USD 资产/余额              10,000 USD cent
  贷 FX 过渡户·USD                   10,000 USD cent

CNY leg:
  借 FX 过渡户·CNY                   72,000 CNY fen
  贷 客户 CNY 余额                    72,000 CNY fen
```

USD leg 的借贷相等，CNY leg 的借贷相等。成功交割后，过渡户应通过流动性提供方、银行 Nostro 或内部净额流程回到策略允许的余额；不能长期把未匹配头寸藏在“临时”科目。

若平台收 `200 CNY fen` 手续费，则客户收到 `71,800 CNY fen`，CNY leg 仍可写成：

```text
借 FX 过渡户·CNY             72,000 CNY fen
贷 客户 CNY 余额              71,800 CNY fen
贷 FX 手续费收入                 200 CNY fen
```

若换汇实际供应商只交付 `71,799`，差额 `1` 不能静默抹掉，必须根据合同记入汇兑损益/尾差科目，并保留原始报价和舍入模式。

### 23.6.3 整数汇率与舍入

令基准币种最小单位金额为 `B`，报价为有理数 `p/q`，目标币种最小单位金额为：

\[
Q = round(B \times p / q, mode)
\]

其中 `B`、`p`、`q` 都是整数，乘法使用 128 位/`big.Int`，在除法前检查上溢。必须明确：

- 报价是“1 base major unit 对多少 quote major unit”，还是最小单位对最小单位；不能只传一个裸 `rate`。
- `HALF_EVEN`、`HALF_UP`、`FLOOR`、`CEILING` 的选择属于产品和监管契约；同一订单不能因服务节点不同而采用不同模式。
- 计算后的 `Q` 必须落在目标币种最小单位，不能保存 0.1 fen。
- `base_amount_min=0`、报价过期、币种停用、分母为 0、结果溢出、结果为 0（若业务不允许）都应拒绝或进入人工处理。

### 23.6.4 FX 状态机与未知态

```text
QUOTED ─► RESERVED ─► BASE_POSTED ─► QUOTE_POSTED ─► SETTLED
   │           │             │              │
 EXPIRED    CANCELLED      UNKNOWN       RECONCILE
```

数据库提交超时、银行确认超时或供应商回执丢失都不是 `FAILED`。若 USD leg 已提交、CNY leg 尚未提交，不能直接再发一笔 CNY leg；应查 `fx_group_id`、过渡户、供应商回执和 Outbox，确认是否已执行，再决定补记或冲正。任何跨网络动作遵守“超时即 UNKNOWN”。

### 23.6.5 多币种对账

每日至少输出：

1. 每个 `account_id + currency_code + bucket_epoch` 的分录重建余额与物化余额差额。
2. 每个 `fx_group_id` 的 base leg、quote leg、报价快照、供应商结算回执是否齐全。
3. 每个币种过渡户余额、年龄分布、未匹配数量和最大暴露，不允许用 USD 估值后掩盖 CNY 过渡户短款。
4. 外部银行/流动性提供方的币种级余额和内部 Nostro 余额；估值换算只用于风险报表，不改变原币种账平衡。

---

## 23.7 并发、故障、对账与压测验收

### 23.7.1 并发控制矩阵

| 场景 | 竞争对象 | 处理 | 验收不变量 |
|---|---|---|---|
| 同一桶并发收入 | 一个 bucket row | `FOR UPDATE` 或乐观版本重试 | 不丢增量，余额可重建 |
| 不同桶并发收入 | 不同 bucket row | 并行提交 | 逻辑总和等于分桶总和 |
| 两笔出账抢额度 | expense bucket/reservation | 锁桶并原子扣 `balance-reserved` | 不透支、不负预留 |
| 一笔跨桶转移 | 两个或更多桶 | 按稳定字典序锁定 | 无死锁、两边同事务 |
| 同键重试 | posting_request | 唯一键 + request hash | 只有一个 entry |
| Group Commit 冲突 | 批内多个桶 | 缩批、按桶分组或重试 | 批内成功/失败可判定 |
| 冲正与原单并发 | 原 entry | `FOR UPDATE` 查原状态 | 不重复冲正 |
| 清扫与出账并发 | source/destination 桶 | 稳定顺序锁、配额检查 | 不制造可用余额 |

锁顺序必须全局一致，例如 `(account_id, bucket_epoch, bucket_no)` 字典序；不能一条路径先锁 A 后 B，另一条先锁 B 后 A。不要在数据库事务内调用银行、链上节点或 FX 供应商。

### 23.7.2 故障注入矩阵

| 故障点 | 禁止动作 | 正确恢复 |
|---|---|---|
| 插入 entry 前断电 | 不知道是否插入就重记 | 用幂等键查单；无记录才可重试 |
| DB commit 后 ACK 前断电 | 当作失败再发一笔 | 查 `posting_request`，返回原 entry |
| 主库 failover/commit timeout | 立即冲正 | 标记 UNKNOWN，等主库/审计确认 |
| Outbox 发布失败 | 回滚已提交账 | Outbox relay 重试，业务账不回滚 |
| 消费者重复消费 | 依赖消息恰好一次 | 下游幂等键/聚合序列去重 |
| FX 外部回执丢失 | 直接再汇一遍 | 查供应商状态和过渡户，人工/自动收敛 |
| 分桶配置误发布 | 原地修改桶数 | 冻结新 epoch，回滚配置版本，重建校验 |
| 发现历史错账 | UPDATE/DELETE 原分录 | 红字冲正 + 蓝字补记 |

### 23.7.3 对账与自愈边界

分三层做：

1. **事务内**：每笔 entry 按币种校验借贷相等；行、桶、幂等和 Outbox 同事务。
2. **实时**：消费余额变更和分录事件，校验 `bucket_sum == logical_sum`、请求数与入账数、Outbox offset 连续性。
3. **批量**：按日/币种/桶从不可变分录重建，与快照、银行、支付渠道、FX 供应商逐项对账。

发现差异时的自愈顺序：冻结相关自动出账 → 保存原始快照和证据 → 定位丢失/重复/错币种/错桶 → 生成补记或冲正建议 → 四眼复核 → 追加新分录 → 重新对账。禁止“自愈 SQL”直接覆盖 `balance_min`。

### 23.7.4 压测计划

压测不是只看平均 TPS，至少建立以下场景：

```text
S1：64 个收入桶，18,000 req/s，小额入账，键分布均匀
S2：80% 请求集中到同一业务热点，验证单桶锁退化
S3：50% 收入、50% 出账，支出配额不断转移
S4：Group Commit 1/32/128/512 四组批量，注入 1%、5% DB 冲突
S5：同一幂等键 100 并发重试，要求只有一个 entry
S6：FX 双腿随机在 commit 前/后断电，验证 UNKNOWN 收敛
S7：红字冲正与原单、清扫、出账并发
```

观测指标：P50/P95/P99/P99.9 延迟、锁等待、死锁/serialization retry、批量大小、WAL flush、连接池、每桶队列深度、UNKNOWN 年龄、过渡户余额和最大年龄、重复请求命中率、对账差异数。验收条件至少包括：

- 任何已成功响应的请求都能查到唯一 `entry_id`；重复请求不会多一条经济事实。
- 每个 entry、每个币种借贷相等；每个逻辑科目分桶总和与重建值相等。
- 注入进程崩溃、主库切换、消息重复后，最终 `RPO=0`（已提交账不丢）且差异为零或进入显式挂账。
- p99 不以牺牲 commit durability 换取；不能用异步“先成功后落账”制造虚假吞吐。
- 过渡户、悬挂冲正和 UNKNOWN 都有 SLA、告警和人工接管路径。

### 23.7.5 极端边界清单

1. 金额为 0、负数、超过 `int64`、加法即将溢出：拒绝并记录原因。
2. 一笔分录行数超过数据库/消息限制：拆成有明确 `posting_group_id` 的多笔平衡分录，不能截断。
3. 币种最小单位为 0 位（JPY）或 8 位（BTC）：不能假设所有币都是 2 位。
4. 尾差舍入到 0：确认产品是否允许“报价存在但交付为 0”；否则拒绝。
5. 同一业务事实在两个会计日重放：以幂等键返回原事实，不因日期变化生成第二笔。
6. 账期关闭后发现错误：新分录使用当前允许会计日，并带调整/更正原因，不改历史期间。
7. 热点桶持续过载：扩容新 epoch、迁移/清扫；不能临时改哈希函数导致重试分叉。
8. 逻辑账户冻结：允许冲正和审计查询的权限与正常出账权限分离。
9. FX 报价在锁定后过期：按契约使用已锁定报价或拒绝，不能偷偷使用最新报价改变客户应得金额。
10. 部分红冲：每个原始行的累计已冲正金额不得超过原金额；全量冲正只能成功一次。
11. 税费、手续费、汇兑损益不是“差额垃圾桶”：每种差额都要有明确科目、币种和业务原因。
12. JSON、CSV、JavaScript 客户端传金额：金额字段用字符串或整数，禁止客户端浮点和超过 `2^53-1` 的裸 JSON number。

---

## 23.8 与后续章节衔接、术语、面试答题卡与自检题

### 23.8.1 与 22、24、25、27 的接口边界

| 本章产物 | 22 章输入/依赖 | 24 章衔接 | 25/27 章衔接 |
|---|---|---|---|
| `entry_id`、`line_id`、`bucket_epoch` | 科目、正常方向、借贷规则 | 以 `idempotency_key + request_hash` 防重入和防篡改 | 状态机驱动 UNKNOWN/补偿；按币种、桶和业务日对账 |
| `RED_REVERSAL`/`BLUE_SUPPLEMENT` | 不可变分录 | 冲正请求同样幂等 | Outbox 发布修正事件，差异进入人工闭环 |
| `fx_group_id`、`quote_id`、过渡户 | 多币种科目 | 汇兑每一腿和查单请求都不可重复 | 供应商未知态由 Saga/查单收敛；过渡户按币种对账 |
| Group Commit `batch_id` | 事务提交边界 | 批内每个请求仍有独立幂等记录 | Outbox relay 可重复投递，消费者用聚合序列去重 |

**交付检查**：下一章 [24] 会把本文的 `ledger_posting_request` 扩展成客户端→网关→账本→渠道的全链路幂等；因此本章不把幂等键只当数据库唯一字段，而是要求保存规范化请求哈希、原始响应、状态和可查单入口。若绕过该边界直接调用 `postBatch`，就失去重试安全性。

### 23.8.2 术语正本清源

| 术语 | 精确定义 | 不是它 |
|---|---|---|
| 热点账户 | 写竞争集中在同一逻辑科目/行 | 只是查询慢 |
| 物理分桶 | 一个逻辑科目的并发隔离存储单元 | 创造额外资产 |
| 收支缓冲户 | 承接高扇入/高扇出并可审计清扫的真实科目/桶 | Kafka 队列或无账务余额 |
| Group Commit | 多条独立分录共用一次数据库提交 | 把账务事实合并成一条 |
| 红字冲正 | 追加与错误分录方向相反、关联原单的修正事实 | 删除或修改原单 |
| 蓝字补记 | 追加正确业务事实的正常正向分录 | 把原金额直接改正确 |
| 过渡户 | 跨系统/跨腿期间暂存未完成头寸的真实科目 | 可以永久藏差额的垃圾桶 |
| 汇兑组 | 由 base/quote 多个币种 leg 组成的可追踪业务组 | 把不同币种相加的单一借贷 |
| 物化余额 | 从分录增量维护的读优化结果 | 独立于分录的事实源 |
| UNKNOWN | 提交或外部执行结果尚未确定 | FAILED 或可安全重试 |
| 最小单位整数 | 币种约定的最小计量单位整数 | 带小数的显示金额 |

### 23.8.3 十分钟面试系统设计答题卡

**题目**：设计一个 2 万 TPS、支持多币种和冲正的核心账本，热点结算户不能成为单行锁。

1. **先说红线**：金额最小单位整数；复式账；每币种平衡；已入账分录不可更新/删除；超时 UNKNOWN；余额只是物化视图。
2. **画边界**：API/幂等层 → 分桶路由与支出配额 → Group Commit → PostgreSQL 分录/余额/Outbox → 异步发布和对账；外部渠道在事务外。
3. **讲热点**：逻辑账户下设固定 epoch 的收入、支出、核心桶；收入按稳定哈希分散，出账从已预留的支出桶扣，不能靠“总和够”绕过配额。
4. **讲事务**：按桶键排序锁行；同事务写 entry、lines、bucket balance、idempotency、outbox；commit 后才 ACK；批失败可重试，键不变。
5. **讲纠错**：原错单保留；红字反向分录关联 `reversal_of`，蓝字正确分录关联 `correction_group_id`；部分冲正防超冲，跨日使用新会计日。
6. **讲 FX**：每币种单独平衡；base/quote 两 leg 通过 USD/CNY 过渡户连接；报价保存分子、分母、舍入、有效期、来源；差额进明确损益科目。
7. **讲故障**：commit timeout/外部超时都 UNKNOWN；查单和 Outbox 收敛；绝不“失败重试”造成双记，绝不回滚已提交原账。
8. **讲一致性**：事务内币种平衡，实时桶和逻辑汇总，日终从不可变分录重建并做银行/渠道/FX 四方对账。
9. **讲压测**：均匀热点、单桶热点、并发重试、批量梯度、DB failover、随机断电和 FX 双腿故障；看 p99、锁等待、WAL、差异和 UNKNOWN 年龄。
10. **讲取舍**：分桶提高吞吐但需要额度预留；强全局实时可用余额则必须串行化；Group Commit 降低 fsync 但扩大失败批次，所有参数由压测和风险限额决定。

### 23.8.4 自检题（带答案要点）

1. **为什么收入分成 64 个桶后，仍不能任意从 64 个桶扣款？**
   因为每个桶的可用余额可能不同；并发出账若只读取逻辑总和，会分别通过检查而合计透支。必须预留/分配支出额度，或锁住全局串行校验。

2. **一笔 100 USD 换 720 CNY，为什么至少要有两条币种 leg？**
   USD 和 CNY 是不同单位。USD leg 借贷各 100 USD，CNY leg 借贷各 720 CNY，过渡户把两条 leg 关联起来；不能在同一币种平衡式中把它们相加。

3. **原单借客户 10,000、贷收入 10,000，实际应为 8,500，如何修？**
   追加红字反向分录借收入/贷客户 10,000，再追加蓝字补记借客户/贷收入 8,500；原单不改、不删，三者有原单和更正组关联。

4. **Group Commit 事务在网络超时后，是否可以把整批当失败重放？**
   不可以。超时是 UNKNOWN。逐个用幂等键查 `ledger_posting_request`；已提交的返回原 entry，未提交的才可用同键重试，不能生成新业务身份。

5. **如何证明物化余额没有漂移？**
   以 entry/line 按账户、币种、正常方向重建，与 bucket `balance_min`、逻辑汇总和外部对账源逐层比较；差异冻结和调查，不能直接更新余额消除告警。

6. **桶数从 64 改成 128，为什么不能立刻改哈希取模？**
   同一业务重试会命中不同桶，造成重复或查不到原账。使用新 `bucket_epoch`，旧 epoch 保留，迁移用真实分录，待校验完成再切读写版本。

7. **过渡户长期有余额说明什么？**
   它表示汇兑双腿或外部结算未完成、差错或隐藏短款；必须按币种、年龄和 `fx_group_id` 对账并设置告警，不能把它当利润或无限期挂账。

**一页速记**：

```text
热账：逻辑科目 ≠ 单行；固定 epoch 分桶 + 收支缓冲 + 支出配额
批量：多条独立分录同事务提交；commit 后 ACK，超时 UNKNOWN
纠错：原单永不改；红字反向冲正，蓝字正确补记
汇兑：最小单位整数；每币种独立平衡；FX 过渡户连接双腿
审计：余额是视图；从不可变分录重建；所有差额显式挂账并可追溯
```
