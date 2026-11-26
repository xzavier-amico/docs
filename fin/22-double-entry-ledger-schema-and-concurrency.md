# 22. 复式记账核心账本引擎（上）：Schema、不可变分录与防透支并发控制

> **状态**：生产级工程篇｜**所属**：第三部分·金融工程代码基座｜**编号**：22 / 48
>
> **本章定位**：把“谁拥有什么、谁欠谁什么、这笔钱为什么变化”落实为可审计、可重放、可并发执行的核心账本。本文覆盖复式记账与账户模型、不可变分录、余额物化视图、PostgreSQL DDL、下单冻结→成交→撤单状态流、Go 1.24 与 Java 21 核心代码，以及锁序、防透支、隔离级别、热点边界、对账、压测和极端故障。下一章 [23 复式记账核心账本引擎（下）](23-hot-accounts-reversals-and-fx-ledger.md) 继续处理热点账户、分桶、红字冲正和多币种汇兑。
>
> **不可妥协的红线**：金额只用币种约定的最小单位整数（USD cent、CNY fen、BTC satoshi）；严禁 `float`/`double` 进入账务；已入账的 `entry` 和 `line` 只能追加、不能 `UPDATE` 或 `DELETE`；余额只是分录的物化视图，禁止业务代码裸改余额；任何一笔业务必须在同一个数据库事务中完成幂等占位、分录、余额投影和 Outbox 事件，提交成功后才能 ACK。

---

## 22.1 引子：一行余额把冻结、成交和撤单变成了事故

### 22.1.1 事故时间线

某数字资产交易平台用一张 `user_asset` 表保存每个用户的 `balance`，下单时执行：

```sql
UPDATE user_asset
SET balance = balance - :amount
WHERE user_id = :uid AND asset = :asset;
```

撮合成交后，另一个服务再把余额加回去；撤单服务不知道成交服务是否已经处理过部分成交，只把 `frozen` 清零。凌晨行情剧烈波动时，事故如下：

| 时间 | 事件 | 结果 |
|---|---|---|
| 02:00:00.001 | 用户以 100 USDC 限价买入，冻结服务扣可用、加冻结 | 看起来成功，但没有保留业务事实 |
| 02:00:00.008 | 撮合引擎部分成交 60 USDC，发送成交消息 | 结算服务开始释放冻结并交付资产 |
| 02:00:00.010 | 撤单请求与成交消息并发到达 | 两个服务都读取到“冻结 100”的旧快照 |
| 02:00:00.015 | 撤单把 100 USDC 加回可用并把冻结置零 | 多释放 60 USDC |
| 02:00:00.021 | 成交结算再次扣 60 USDC | 余额可能负数，或被 `max(balance, 0)` 静默截断 |
| 02:00:03 | 客户重试同一个成交回调 | 没有幂等键，再次交付 60 USDC |
| 次日 | 运维直接把余额改回“对账值” | 数字暂时好看，但无法回答钱从哪里来、谁承担损失 |

直接损失不只是一笔多发资产。平台同时失去了：

1. **事实**：到底发生过几次冻结、成交、解冻和转移；
2. **顺序**：撤单和部分成交谁先生效；
3. **解释**：余额变化对应哪一笔订单、哪一个撮合成交；
4. **恢复能力**：数据库恢复、消息重放、客户争议和监管抽查时能否重建；
5. **并发安全**：两个请求分别检查“余额够”，合计却超支的竞态是否被阻断。

根因是把**会计事实、业务状态和读模型**压成了一列可写数字。生产系统应该让业务状态机决定“允许哪个动作”，让复式分录记录“这个动作造成了什么经济变化”，让余额表仅作为分录聚合的加速结果。

### 22.1.2 唯一真问题

> **在并发请求、重复消息、进程崩溃和数据库故障下，能否保证每个币种的借贷永远平衡、任何资金不会凭空出现或消失、任何账户不会透支，并且从不可变分录重新计算出的余额与在线读模型一致？**

三个看似简单但错误的答案：

| 反模式 | 为什么不成立 | 生产替代物 |
|---|---|---|
| 直接 `UPDATE balance = balance + x` | 没有经济事实、缺少对手账户、不可审计 | `entry + line` 复式分录和物化余额投影 |
| `SELECT balance` 后在应用层判断够不够 | 两个事务都读到同一个旧余额，形成双花/透支 | 在同一事务内按固定顺序锁余额行并检查 |
| 撤单直接把 `frozen` 设为 0 | 覆盖了已成交部分，也无法解释释放多少 | 冻结、成交释放、实扣、剩余解冻分别追加分录 |
| 捕获超时后换新请求号重试 | 原事务可能已提交，造成重复记账 | 原幂等键查单；结果不明是 `UNKNOWN` |
| 对账不平时手工改余额 | 掩盖根因，下一次重放又漂移 | 冻结异常、定位分录、追加冲正/补记 |

---

## 22.2 复式记账和账户模型：每一单位钱都有对手方

### 22.2.1 T 型账户与借贷方向

复式记账不等于“每次写两行”。准确说法是：一条经济事实必须由一组分录行描述，**同一币种内借方总额等于贷方总额**，且每一行都属于明确的账户。可以有两行，也可以有多行，但不能只有“余额变了”而没有对手方。

会计科目通常按正常余额方向理解：

| 账户类型 | 正常方向 | 借方的通常含义 | 贷方的通常含义 |
|---|---:|---|---|
| 资产 `ASSET` | D | 增加资产 | 减少资产 |
| 负债 `LIABILITY` | C | 减少对客户/债权人的负债 | 增加应付义务 |
| 权益 `EQUITY` | C | 减少权益 | 增加权益 |
| 收入 `REVENUE` | C | 冲减收入 | 增加收入 |
| 费用 `EXPENSE` | D | 增加费用 | 冲减费用 |
| 过渡/暂记 `TRANSIT/SUSPENSE` | 由业务定义 | 暂存一条未完成腿 | 暂存另一方向 |

例如平台托管客户 100.00 USD（金额 `10_000` cents）时，平台账上可以表现为：

```text
借：银行存款/托管资产（资产）       10,000 USD cent
贷：客户 USD 应付（负债）             10,000 USD cent
```

平台不是因为“数据库里有一个客户余额”就欠客户钱，而是因为存在一条资产与负债同时增加的可审计事实。若客户把 100 USD 提现并且银行已出款：

```text
借：客户 USD 应付（负债）             10,000 USD cent
贷：银行存款/托管资产（资产）          10,000 USD cent
```

两条分录都不可删除。若出款失败，不能把第一条分录改回去，而应追加与失败事实对应的反向分录，具体由清结算状态机决定。

### 22.2.2 账户不是用户余额字段

生产模型至少区分四个概念：

1. **逻辑科目 `ledger_account`**：报表和会计意义上的身份，如“客户可用 USD”“客户冻结 USD”“平台手续费收入 USD”。
2. **账户持有人/维度**：客户、平台实体、清算机构、银行往来户等。一个逻辑科目可按客户、资产、市场、法律实体细分。
3. **账户余额投影 `account_balance`**：在某个 `(账户, 币种)` 上由已提交分录增量维护的读模型，不能成为事实源。
4. **分录行 `ledger_line`**：不可变事实，保存借贷方向、最小单位金额、业务引用和会计日。

一个交易账户通常不是一张“可用余额表”，而是一组可解释的子账户：

```text
客户 C100
└── USD
    ├── AVAILABLE（可下单、可提现）
    ├── FROZEN（订单/保证金已占用）
    ├── SETTLEMENT（成交待交收）
    └── DISPUTED / RESTRICTED（争议或合规冻结）
```

`AVAILABLE + FROZEN + SETTLEMENT` 的总额才是客户在该资产维度的账面权益；可用额度还要经过风控、借贷、提现限额和在途约束，不能把“总余额”误当成“现在可以支付的金额”。

### 22.2.3 核心不变量

设一条分录为 `e`，币种为 `c`，每个金额 `a` 为严格大于零的最小单位整数：

1. **币种平衡**：

   \[
   \sum_{line.currency=c, side=DEBIT}a
   =
   \sum_{line.currency=c, side=CREDIT}a
   \]

   不同币种不能在同一个求和式中相加。汇兑必须有各自币种的平衡腿和明确的过渡户/损益科目。
2. **不可变**：`POSTED` 分录及分录行不能更新、删除或复用 ID；错账只能新增冲正分录和补记分录。
3. **余额可重建**：在线余额等于所有已生效分录行按账户正常方向聚合的结果。任何余额投影都必须能从分录重放得到。
4. **无透支**：对 `allow_negative = false` 的可支用账户，事务提交前必须证明 `available - debit_delta >= 0`。检查和扣减必须处于同一锁保护范围。
5. **幂等**：同一 `idempotency_key` 只能产生一个业务结果。重试必须返回原结果，不得产生第二个经济事实。
6. **原子性**：分录、余额投影、幂等结果、订单状态和 Outbox 事件要么一起提交，要么都不可见。
7. **可追溯**：每个 `entry` 关联 `business_type/business_id`，每个纠正关联原始分录，每个订单成交关联撮合成交 ID。

### 22.2.4 典型交易的分录展开

假设客户 A 以限价 100 USDC 买入 2 BTC，手续费 10 USDC，成交 1.2 BTC。用最小单位整数表示：

**冻结（100 × 2 + 10 = 210 USDC）：**

```text
借：A · USD AVAILABLE       21,000 USDC cent
贷：A · USD FROZEN          21,000 USDC cent
```

**部分成交，成交价 100，手续费 6 USDC；释放本次成交对应的冻结 126 USDC：**

```text
借：A · USD FROZEN          12,600 USDC cent
贷：A · USD SETTLEMENT      12,000 USDC cent
贷：平台手续费收入              600 USDC cent

借：卖方 · BTC AVAILABLE    120,000,000 satoshi
贷：A · BTC AVAILABLE       120,000,000 satoshi
```

第一组 USDC 行借贷平衡，第二组 BTC 行借贷平衡。若平台的手续费收入要对应一项客户资产/银行资产，还需在独立的现金结算事实中配对；不能因为“手续费是收入”就凭空增加平台可提现现金。

**剩余部分撤单：**冻结余额为 `84 USDC`，若未发生其他费用预留：

```text
借：A · USD FROZEN           8,400 USDC cent
贷：A · USD AVAILABLE        8,400 USDC cent
```

这三组事实分别有自己的 `entry_id`，不能把撤单记录成“把 frozen 字段改成 0”。

---

## 22.3 不可变分录与余额物化视图

### 22.3.1 事实层、投影层和操作层三层分离

```text
业务请求/撮合成交
       │  幂等键 + 业务引用
       ▼
事实层：ledger_entry + ledger_line（只追加、可重放）
       │  同事务增量投影
       ▼
投影层：account_balance（在线读、锁定、防透支）
       │  异步派生
       ├── 日终余额快照
       ├── 客户资产报表
       └── Outbox / 风控缓存 / 搜索索引
```

`ledger_entry` 描述一组原子经济事实，`ledger_line` 描述具体账户变化；`account_balance` 只保存查询和并发控制所需的当前聚合。余额投影损坏时，正确的修复方式是停写/隔离后从分录重建，再比较差异，不是执行一条“校正 UPDATE”掩盖问题。

### 22.3.2 为什么不能只靠事件溯源重放

纯事件溯源可以重放，但金融账本还需要数据库级的：

- 同一笔分录的币种借贷约束；
- 账户余额行锁和防透支检查；
- 同一事务内的幂等唯一键；
- 订单状态与会计事实的原子边界；
- 对账期间可查询的稳定快照。

因此生产实现通常采用“**不可变账本为事实源 + 事务内物化余额为强一致读模型 + 异步派生报表为最终一致查询模型**”。异步报表延迟不能影响核心扣款判断；核心扣款不能绕过事实层去直接改变报表。

### 22.3.3 分录状态与更正

推荐状态：

| 状态 | 含义 | 是否影响余额 |
|---|---|---:|
| `DRAFT` | 事务内尚未提交的草稿 | 否 |
| `POSTED` | 已提交且生效 | 是 |
| `VOIDED_BY_REVERSAL` | 原事实仍保留，已被完整冲正 | 原分录仍参与历史，净效果由冲正抵消 |

不要把 `VOIDED` 理解为删除。审计查询必须看到原分录、冲正分录和二者关系。部分冲正还要保存累计已冲正金额，并在数据库约束或锁内逻辑中防止“冲过头”。本章先实现完整业务动作的不可变写入；红字/蓝字的系统化方案见第 23 章。

### 22.3.4 规范化哈希和审计链

每个已入账分录可保存：

```text
canonical_payload =
  schema_version | entry_id | posting_type | business_id |
  accounting_date | sorted(currency, side, account_id, amount_min, line_no)
entry_hash = SHA-256(canonical_payload)
```

哈希不是数据库约束的替代物，也不能替代权限和备份；它用于检测历史字段被离线修改、跨库复制丢行和报表篡改。规范化时必须固定字段顺序、整数编码、时区和空值表示，否则同一事实在 Go、Java 和批处理程序中会得到不同哈希。

---

## 22.4 PostgreSQL DDL：科目、分录、余额、状态机与 Outbox

以下示例适用于 PostgreSQL 15+，同样适配 PostgreSQL 16/17。`BIGINT` 的最大值约为 `9.22e18`，应用层必须在加减乘除前做溢出检查；若业务规模可能超出范围，使用 `NUMERIC(39,0)` 和 Go `big.Int`/Java `BigInteger`，不能静默截断。

### 22.4.1 币种、科目和账户余额

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE currency (
  currency_code CHAR(3) PRIMARY KEY,
  minor_unit SMALLINT NOT NULL CHECK (minor_unit BETWEEN 0 AND 18),
  status VARCHAR(16) NOT NULL CHECK (status IN ('ACTIVE','SUSPENDED','RETIRED')),
  version BIGINT NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE ledger_account (
  account_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_code VARCHAR(160) NOT NULL UNIQUE,
  owner_type VARCHAR(24) NOT NULL CHECK (owner_type IN ('CUSTOMER','PLATFORM','BANK','CCP','SYSTEM')),
  owner_id VARCHAR(128) NOT NULL,
  account_type VARCHAR(16) NOT NULL CHECK (
    account_type IN ('ASSET','LIABILITY','EQUITY','REVENUE','EXPENSE','TRANSIT','SUSPENSE')
  ),
  normal_side CHAR(1) NOT NULL CHECK (normal_side IN ('D','C')),
  currency_code CHAR(3) NOT NULL REFERENCES currency(currency_code),
  purpose VARCHAR(32) NOT NULL CHECK (
    purpose IN ('AVAILABLE','FROZEN','SETTLEMENT','CUSTODY','FEE','CLEARING','SUSPENSE','OTHER')
  ),
  allow_negative BOOLEAN NOT NULL DEFAULT FALSE,
  status VARCHAR(16) NOT NULL CHECK (status IN ('OPEN','FROZEN','CLOSED')),
  version BIGINT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_type, owner_id, currency_code, purpose)
);

CREATE TABLE account_balance (
  account_id UUID PRIMARY KEY REFERENCES ledger_account(account_id),
  balance_min BIGINT NOT NULL DEFAULT 0,
  reserved_min BIGINT NOT NULL DEFAULT 0,
  aggregate_seq BIGINT NOT NULL DEFAULT 0,
  version BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CHECK (reserved_min >= 0)
);

CREATE INDEX ledger_account_owner_idx
  ON ledger_account(owner_type, owner_id, currency_code, purpose);
```

`balance_min` 是按科目正常方向归一化后的余额：资产/费用借方增加，负债/权益/收入贷方增加。若要展示“客户欠平台多少”或“客户可用多少”，应用层根据账户类型和业务语义渲染，不在数据库里混用正负号。物理行中的分录金额仍始终 `> 0`。

### 22.4.2 订单、成交和幂等请求

```sql
CREATE TABLE trading_order (
  order_id UUID PRIMARY KEY,
  client_order_id VARCHAR(128) NOT NULL,
  customer_id VARCHAR(128) NOT NULL,
  base_currency CHAR(3) NOT NULL REFERENCES currency(currency_code),
  quote_currency CHAR(3) NOT NULL REFERENCES currency(currency_code),
  side VARCHAR(8) NOT NULL CHECK (side IN ('BUY','SELL')),
  limit_price_min BIGINT NOT NULL CHECK (limit_price_min > 0),
  original_qty_min BIGINT NOT NULL CHECK (original_qty_min > 0),
  filled_qty_min BIGINT NOT NULL DEFAULT 0 CHECK (filled_qty_min >= 0),
  reserved_quote_min BIGINT NOT NULL DEFAULT 0 CHECK (reserved_quote_min >= 0),
  reserved_base_min BIGINT NOT NULL DEFAULT 0 CHECK (reserved_base_min >= 0),
  status VARCHAR(20) NOT NULL CHECK (
    status IN ('NEW','OPEN','PARTIALLY_FILLED','FILLED','CANCEL_PENDING','CANCELED','REJECTED')
  ),
  version BIGINT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(customer_id, client_order_id),
  CHECK (filled_qty_min <= original_qty_min)
);

CREATE TABLE trade_execution (
  execution_id UUID PRIMARY KEY,
  order_id UUID NOT NULL REFERENCES trading_order(order_id),
  match_id VARCHAR(160) NOT NULL UNIQUE,
  price_min BIGINT NOT NULL CHECK (price_min > 0),
  qty_min BIGINT NOT NULL CHECK (qty_min > 0),
  fee_min BIGINT NOT NULL DEFAULT 0 CHECK (fee_min >= 0),
  fee_currency CHAR(3) NOT NULL REFERENCES currency(currency_code),
  executed_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE ledger_posting_request (
  posting_request_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  idempotency_key VARCHAR(200) NOT NULL UNIQUE,
  request_hash BYTEA NOT NULL,
  business_type VARCHAR(48) NOT NULL,
  business_id VARCHAR(160) NOT NULL,
  status VARCHAR(16) NOT NULL CHECK (status IN ('PROCESSING','COMMITTED','REJECTED','UNKNOWN')),
  entry_id UUID,
  response_json JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(business_type, business_id)
);
```

`business_type + business_id` 防止同一个业务事实换一个幂等键重复入账；`idempotency_key` 防止同一请求被重试。若业务确实允许同一订单产生多次成交，`business_id` 应使用不可变的 `match_id`，而不是只使用 `order_id`。

### 22.4.3 不可变分录及每币种平衡约束

```sql
CREATE TABLE ledger_entry (
  entry_id UUID PRIMARY KEY,
  posting_request_id UUID NOT NULL UNIQUE
    REFERENCES ledger_posting_request(posting_request_id),
  business_type VARCHAR(48) NOT NULL,
  business_id VARCHAR(160) NOT NULL,
  posting_type VARCHAR(40) NOT NULL,
  accounting_date DATE NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  status VARCHAR(24) NOT NULL CHECK (status IN ('POSTED','VOIDED_BY_REVERSAL')),
  reversal_of_entry_id UUID REFERENCES ledger_entry(entry_id),
  correction_group_id UUID,
  canonical_hash BYTEA NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE ledger_line (
  line_id UUID PRIMARY KEY,
  entry_id UUID NOT NULL REFERENCES ledger_entry(entry_id),
  line_no SMALLINT NOT NULL CHECK (line_no >= 1),
  account_id UUID NOT NULL REFERENCES ledger_account(account_id),
  currency_code CHAR(3) NOT NULL REFERENCES currency(currency_code),
  side CHAR(1) NOT NULL CHECK (side IN ('D','C')),
  amount_min BIGINT NOT NULL CHECK (amount_min > 0),
  normal_delta BIGINT NOT NULL,
  business_ref VARCHAR(160),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(entry_id, line_no)
);

CREATE INDEX ledger_line_account_time_idx
  ON ledger_line(account_id, currency_code, created_at, line_id);
CREATE INDEX ledger_entry_business_idx
  ON ledger_entry(business_type, business_id, posting_type, created_at);

-- 同一个 entry 内，同一个币种必须借贷平衡。约束触发器在事务结束时检查，
-- 因而允许先插借方、再插贷方。
CREATE OR REPLACE FUNCTION assert_entry_balanced_trigger()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
  bad_count INT;
  line_count INT;
BEGIN
  SELECT COUNT(*) INTO line_count
  FROM ledger_line
  WHERE entry_id = NEW.entry_id;

  SELECT COUNT(*) INTO bad_count
  FROM (
    SELECT currency_code,
           SUM(amount_min) FILTER (WHERE side = 'D') AS debit_sum,
           SUM(amount_min) FILTER (WHERE side = 'C') AS credit_sum
    FROM ledger_line
    WHERE entry_id = NEW.entry_id
    GROUP BY currency_code
  ) x
  WHERE debit_sum IS DISTINCT FROM credit_sum
     OR debit_sum IS NULL
     OR debit_sum = 0;

  IF line_count < 2 OR bad_count <> 0 THEN
    RAISE EXCEPTION 'entry % is not balanced per currency', NEW.entry_id
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER ledger_line_balance_ck
AFTER INSERT ON ledger_entry
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION assert_entry_balanced_trigger();

-- 也检查向已存在 entry 追加行的情况，不能只在创建 entry 时验证一次。
CREATE CONSTRAINT TRIGGER ledger_line_insert_balance_ck
AFTER INSERT ON ledger_line
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION assert_entry_balanced_trigger();
```

**完整性边界**：上面两个平衡触发器不等于完整“封账”实现。受控发布入口还必须拒绝向已提交凭证追加任何行（即使新行仍然成对平衡），并禁止业务角色直接 INSERT；否则审计者过去看到的凭证内容仍能被事后扩充。可通过创建事务身份校验或同事务 DRAFT→POSTED 封存流程实现；采用后者时，状态元数据与经济事实分离，且分录、投影、Outbox 必须一并提交。本文代码展示交易核心，不声称单靠这些触发器覆盖全部权限攻击。

这个延迟触发器绑定在 `ledger_entry` 上：同一事务内先插入 entry、再插入借贷行，事务结束时才按 `NEW.entry_id` 检查全部行，因此也能拦截“只有 entry 没有 line”的空事实。实际项目还应为 `ledger_line` 建立拒绝 `UPDATE/DELETE` 的触发器，并在受控 `POST` 存储过程中显式检查“至少一条借方和一条贷方”；**应用层校验不能替代数据库约束。**

### 22.4.4 保护不可变事实和余额投影

```sql
CREATE OR REPLACE FUNCTION reject_ledger_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'immutable ledger table: % is append-only', TG_TABLE_NAME
    USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER ledger_entry_no_update
BEFORE UPDATE OR DELETE ON ledger_entry
FOR EACH ROW EXECUTE FUNCTION reject_ledger_mutation();

CREATE TRIGGER ledger_line_no_update
BEFORE UPDATE OR DELETE ON ledger_line
FOR EACH ROW EXECUTE FUNCTION reject_ledger_mutation();

CREATE OR REPLACE FUNCTION prevent_negative_available()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE allow_negative BOOLEAN;
BEGIN
  SELECT la.allow_negative INTO allow_negative
  FROM ledger_account la WHERE la.account_id = NEW.account_id;
  IF NOT allow_negative AND NEW.balance_min < 0 THEN
    RAISE EXCEPTION 'account % would be overdrawn', NEW.account_id
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER account_balance_no_overdraft
BEFORE INSERT OR UPDATE ON account_balance
FOR EACH ROW EXECUTE FUNCTION prevent_negative_available();

CREATE TABLE ledger_outbox (
  outbox_id BIGSERIAL PRIMARY KEY,
  aggregate_type VARCHAR(40) NOT NULL,
  aggregate_id VARCHAR(160) NOT NULL,
  event_type VARCHAR(80) NOT NULL,
  event_key VARCHAR(200) NOT NULL UNIQUE,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  published_at TIMESTAMPTZ,
  attempt_count INT NOT NULL DEFAULT 0,
  last_error TEXT
);

CREATE INDEX ledger_outbox_unpublished_idx
  ON ledger_outbox(outbox_id) WHERE published_at IS NULL;
```

`account_balance` 仍然允许被账本发布器在同一事务中更新，因为它是投影而不是事实。生产权限应分离：业务连接用户只能执行受控的 `post_ledger_entry()` 存储过程或调用账本服务，不能拥有对 `ledger_line` 的 `UPDATE/DELETE` 权限，也不能直接写 `account_balance`。数据库管理员的紧急操作必须四眼审批、记录审计，并在事后以分录修复而不是把手工改余额当作正常流程。

### 22.4.5 用受控存储过程封装投影更新

投影更新的核心伪 SQL 如下。它既不接受外部传入的“新余额”，也不接受独立的 delta 参数，只读取本事务已写入的分录行：

```sql
-- 同一事务内，先锁齐涉及的账户；UUID 原生顺序等价于规范小写文本顺序。
SELECT account_id, balance_min, version
FROM account_balance
WHERE account_id = ANY(:account_ids_sorted)
ORDER BY account_id
FOR UPDATE;

-- 每账户聚合本次 entry 的已入行，再增量更新投影。
UPDATE account_balance b
SET balance_min = b.balance_min + d.delta,
    version = b.version + 1,
    aggregate_seq = b.aggregate_seq + 1,
    updated_at = clock_timestamp()
FROM (SELECT account_id, SUM(normal_delta)::bigint AS delta
      FROM ledger_line WHERE entry_id = :entry_id GROUP BY account_id) d
WHERE b.account_id = d.account_id;
```

“禁止裸改余额”并不意味着数据库永远不能出现 `UPDATE account_balance`；它意味着**任何余额更新都必须是账本事务的内部投影动作，输入是分录行推导出的 delta，而不是调用者随意指定的余额结果**。这一区别要写进接口、权限和测试，而不只是写在注释中。

---

## 22.5 冻结、成交、撤单：状态流和复式分录边界

### 22.5.1 状态机不是账本，账本也不是状态机

订单状态是可以被当前值覆盖的操作模型，但每一次合法状态迁移造成的资金变化必须追加分录。状态机负责回答“下一步能否做”，账本负责回答“做了以后钱如何变化”。

```text
NEW ──reserve success──► OPEN
 │                         │
 └─reserve failed──► REJECTED

OPEN ──partial fill──► PARTIALLY_FILLED ──more fills──► FILLED
  │                           │
  └─cancel request──────► CANCEL_PENDING
                              │
              no remaining qty┴────► CANCELED
```

允许的迁移必须是显式白名单：

| 当前状态 | 动作 | 下一状态 | 账务动作 |
|---|---|---|---|
| `NEW` | 冻结成功 | `OPEN` | AVAILABLE → FROZEN |
| `NEW` | 冻结失败 | `REJECTED` | 无资金分录，记录拒绝原因 |
| `OPEN` | 部分成交 | `PARTIALLY_FILLED` | 释放成交对应冻结、交收 base/quote、手续费 |
| `PARTIALLY_FILLED` | 成交至总量 | `FILLED` | 同上；剩余冻结应为 0 |
| `OPEN/PARTIALLY_FILLED` | 撤单 | `CANCEL_PENDING` → `CANCELED` | 剩余冻结 FROZEN → AVAILABLE |
| `FILLED/CANCELED/REJECTED` | 重复动作 | 原状态 | 幂等返回，不产生新分录 |

`CANCEL_PENDING` 很重要：撤单请求到达不代表撮合引擎已停止产生成交。只有在订单行锁、撮合序列/成交版本和剩余数量都确认后，才能把剩余冻结解回可用。若外部撮合系统异步回传，不能先释放全部冻结再等待成交回调。

### 22.5.2 冻结分录

买入订单按“限价 × 原始数量 + 最大手续费预留”冻结报价币；卖出订单冻结基础币。以买入为例：

```text
借：客户 C100 · USDC AVAILABLE       21,000 cent
贷：客户 C100 · USDC FROZEN          21,000 cent
posting_type = ORDER_RESERVE
business_id  = order_id
```

冻结只是客户内部子账户之间的转移，不改变客户该币种总权益；它改变的是可支用约束。应在同一事务内锁：订单行、可用账户余额、冻结账户余额，并检查 `AVAILABLE` 仍有足额可用量。

### 22.5.3 部分成交分录

假设买单原始冻结 210 USDC，成交 120 USDC，手续费 6 USDC，则先从冻结中释放成交消费部分，剩余 84 USDC 仍冻结：

```text
-- quote leg（USDC）
借：买方 USDC FROZEN                 12,600 cent
贷：买方 USDC SETTLEMENT             12,000 cent
贷：平台手续费收入 USDC                  600 cent

-- base leg（BTC）
借：卖方 BTC AVAILABLE              120,000,000 sat
贷：买方 BTC AVAILABLE              120,000,000 sat
```

真实平台可能在交收前使用 `SETTLEMENT` 过渡户：

```text
借：买方 FROZEN        12,600 USDC
贷：清算过渡户          12,600 USDC

借：清算过渡户          12,000 USDC
借：清算过渡户             600 USDC
贷：卖方 AVAILABLE       12,000 USDC
贷：平台手续费收入          600 USDC
```

无论采用两组还是过渡户方案，都必须明确交收完成条件和对账键。不能把“成交消息已发出”当成“双方资产已最终交付”。每个 `match_id` 只能产生一次成交事实。

### 22.5.4 撤单分录

若已成交部分对应冻结已消费，只对剩余冻结释放。令：

```text
remaining_reserve = original_reserve - consumed_reserve - released_reserve
```

在锁内计算，且要求 `remaining_reserve >= 0`。撤单只能追加：

```text
借：客户 C100 · USDC FROZEN        remaining_reserve
贷：客户 C100 · USDC AVAILABLE     remaining_reserve
posting_type = ORDER_CANCEL_RELEASE
business_id  = order_id + cancel_request_id
```

第二次撤单命中同一幂等键时返回第一次结果；换一个请求号但订单已 `CANCELED` 时，也必须以订单状态和“剩余冻结为 0”判断为无资金动作，不能再次释放。

### 22.5.5 状态、分录和 Outbox 的同事务提交

一次成功的冻结事务应同时包含：

1. 插入 `ledger_posting_request` 幂等占位并确认新插入所有权，冲突则锁原请求校验后重放或拒绝；
2. `SELECT ... FOR UPDATE` 按全局协议锁订单和相关账户；
3. 验证状态迁移、金额溢出、可用余额和订单版本；
4. 插入 `ledger_entry/ledger_line`；
5. 增量更新 `account_balance`；
6. 更新 `trading_order.status/version/reserved_*`；
7. 插入 `ledger_outbox`；
8. 提交成功后返回 `COMMITTED` 和 `entry_id`。

任何一步失败都回滚。事务提交后进程在 ACK 前崩溃，客户端得到 `UNKNOWN`；恢复或重试通过原幂等键查询，不允许猜测为失败并再次冻结。

---

## 22.6 Go 1.24 与 Java 21 核心代码：锁序、幂等和投影

以下代码刻意只展示核心边界，省略连接池、观测和序列化细节。生产代码必须使用参数化 SQL、超时上下文、重试分类和结构化日志。代码里的 `UPDATE account_balance` 只能出现在 ledger service 的同一事务内部，业务层没有“设置余额”的 API。

**幂等协议（两种语言相同）**：`INSERT ... ON CONFLICT DO NOTHING RETURNING` 返回新行才代表当前事务取得 `PROCESSING` 所有权；无返回行时，用下一条 `SELECT ... FOR UPDATE` 读取原幂等键，先比较 `request_hash` 和业务身份，再仅对 `COMMITTED` 重放。哈希不同、业务唯一键被另一幂等键占用、已有 `PROCESSING/REJECTED/UNKNOWN` 均拒绝本次发布，不覆盖原请求、不接管旧占位。省略冲突目标是为了同时处理两组唯一约束；这里选择拒绝“换 key 的同业务请求”，而非跨 key 自动重放。在 `READ COMMITTED` 下，竞争 INSERT 等待对方提交/回滚后，下一条 SELECT 使用新快照；这依赖幂等记录不被并发删除，锁等待超时仍需分类处理。占位、分录、投影、Outbox 在同一事务内，正常流程不会单独提交 `PROCESSING`。

`RequestHash/requestHash` 必须由可信入口针对完整规范化请求计算并验证（包含全部经济字段，不含每次新生成的 entry ID），不能信任客户端自报哈希；本文只保留这个边界。Go 的 `canonicalPosting/newUUID` 是占位实现，Java 的 entry/Outbox helper 明确抛出未实现异常，均不是可直接部署的账本服务。

### 22.6.1 Go 1.24：按账户 ID 排序后在事务内发布分录

```go
package ledger

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"math"
	"sort"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var (
	ErrDuplicate = errors.New("idempotency key already used")
	ErrOverdraft = errors.New("insufficient available balance")
)

type Side byte

const (
	Debit  Side = 'D'
	Credit Side = 'C'
)

type Line struct {
	AccountID string
	Currency  string
	Side      Side
	AmountMin int64 // strictly positive minor-unit integer; no caller-supplied delta
}

type Posting struct {
	IdempotencyKey string
	RequestHash    []byte
	BusinessType   string
	BusinessID     string
	PostingType    string
	AccountingDate string
	Lines          []Line
}

type Service struct{ DB *pgxpool.Pool }

func sortedAccountIDs(lines []Line) ([]string, error) {
	seen := make(map[string]struct{}, len(lines))
	ids := make([]string, 0, len(lines))
	for _, l := range lines {
		// 拒绝非规范 UUID；不能让同一个 UUID 的大小写/缩写别名改变锁序。
		id := l.AccountID
		if len(id) != 36 || id[8] != '-' || id[13] != '-' || id[18] != '-' || id[23] != '-' || id != strings.ToLower(id) {
			return nil, fmt.Errorf("account ID must be canonical lowercase UUID")
		}
		raw, err := hex.DecodeString(strings.ReplaceAll(id, "-", ""))
		if err != nil || len(raw) != 16 { return nil, fmt.Errorf("invalid account UUID") }
		if _, ok := seen[l.AccountID]; !ok {
			seen[l.AccountID] = struct{}{}
			ids = append(ids, l.AccountID)
		}
	}
	sort.Strings(ids) // every code path uses this exact global lock order
	return ids, nil
}

func addChecked(a, b int64) (int64, error) {
	if b > 0 && a > math.MaxInt64-b { return 0, fmt.Errorf("int64 overflow") }
	if b < 0 && a < math.MinInt64-b { return 0, fmt.Errorf("int64 underflow") }
	return a + b, nil
}

func (s *Service) Post(ctx context.Context, p Posting) (string, error) {
	if len(p.Lines) < 2 { return "", fmt.Errorf("a posting needs at least two lines") }
	if len(p.RequestHash) == 0 { return "", fmt.Errorf("request hash required") }

	// 先做纯函数校验；事务内还要再次依赖数据库锁和约束。
	debit, credit := map[string]int64{}, map[string]int64{}
	for _, l := range p.Lines {
		if l.AmountMin <= 0 { return "", fmt.Errorf("amount must be positive") }
		if l.Side != Debit && l.Side != Credit { return "", fmt.Errorf("invalid side") }
		if l.Side == Debit {
			sum, e := addChecked(debit[l.Currency], l.AmountMin)
			if e != nil { return "", e }
			debit[l.Currency] = sum
		} else {
			sum, e := addChecked(credit[l.Currency], l.AmountMin)
			if e != nil { return "", e }
			credit[l.Currency] = sum
		}
	}
	if len(debit) != len(credit) { return "", fmt.Errorf("unbalanced currencies") }
	for c, d := range debit { if d != credit[c] { return "", fmt.Errorf("currency %s is unbalanced", c) } }
	ids, err := sortedAccountIDs(p.Lines)
	if err != nil { return "", err }

	tx, err := s.DB.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.ReadCommitted})
	if err != nil { return "", err }
	defer tx.Rollback(ctx) // commit 后 Rollback 返回 ErrTxClosed，可安全忽略

	var requestID string
	err = tx.QueryRow(ctx, `
		INSERT INTO ledger_posting_request
		  (idempotency_key, request_hash, business_type, business_id, status)
		VALUES ($1,$2,$3,$4,'PROCESSING')
		ON CONFLICT DO NOTHING
		RETURNING posting_request_id`,
		p.IdempotencyKey, p.RequestHash, p.BusinessType, p.BusinessID).
		Scan(&requestID)
	if errors.Is(err, pgx.ErrNoRows) {
		// INSERT 会等待竞争者结束；READ COMMITTED 下下一条语句取得新快照。
		var existingEntry, status, businessType, businessID string
		var storedHash []byte
		err = tx.QueryRow(ctx, `
			SELECT request_hash, COALESCE(entry_id::text,''), status, business_type, business_id
			FROM ledger_posting_request WHERE idempotency_key=$1 FOR UPDATE`,
			p.IdempotencyKey).Scan(&storedHash, &existingEntry, &status, &businessType, &businessID)
		if errors.Is(err, pgx.ErrNoRows) { return "", ErrDuplicate } // 业务唯一键冲突，不接管其他请求
		if err != nil { return "", err }
		if !bytes.Equal(storedHash, p.RequestHash) || businessType != p.BusinessType || businessID != p.BusinessID {
			return "", ErrDuplicate
		}
		if status == "COMMITTED" && existingEntry != "" {
			return existingEntry, nil // 只读重放，defer Rollback 释放锁；原结果已提交
		}
		return "", fmt.Errorf("existing posting is %s; query with the same idempotency key", status)
	}
	if err != nil { return "", err }
	// 只有 INSERT RETURNING 得到新 requestID 的事务拥有本次 PROCESSING，可继续写账。

	// 所有涉及账户按字典序锁定，避免 A->B 与 B->A 形成死锁环。
	for _, id := range ids {
		var balance int64
		var allowNegative bool
		err = tx.QueryRow(ctx, `
			SELECT b.balance_min, a.allow_negative
			FROM account_balance b JOIN ledger_account a USING (account_id)
			WHERE b.account_id = $1
			FOR UPDATE`, id).Scan(&balance, &allowNegative)
		if err != nil { return "", err }
		_ = balance; _ = allowNegative
	}

	entryID := newUUID() // 省略 UUID 实现；必须来自密码学安全随机源
	h := sha256.Sum256(canonicalPosting(p, entryID))
	_, err = tx.Exec(ctx, `
		INSERT INTO ledger_entry
		 (entry_id, posting_request_id, business_type, business_id, posting_type,
		  accounting_date, occurred_at, status, canonical_hash)
		VALUES ($1,$2,$3,$4,$5,$6,clock_timestamp(),'POSTED',$7)`,
		entryID, requestID, p.BusinessType, p.BusinessID, p.PostingType,
		p.AccountingDate, h[:])
	if err != nil { return "", err }

	for i, l := range p.Lines {
		tag, err := tx.Exec(ctx, `
			INSERT INTO ledger_line
			 (line_id, entry_id, line_no, account_id, currency_code, side,
			  amount_min, normal_delta, business_ref)
			SELECT gen_random_uuid(), $1, $2, a.account_id, $3, $4, $5::bigint,
			       CASE WHEN a.normal_side = $4 THEN $5::bigint ELSE -$5::bigint END, $6
			FROM ledger_account a WHERE a.account_id = $7 AND a.currency_code = $3`,
			entryID, i+1, l.Currency, string(l.Side), l.AmountMin,
			p.BusinessID, l.AccountID)
		if err != nil { return "", err }
		if tag.RowsAffected() != 1 { return "", fmt.Errorf("account/currency mismatch") }
	}

	// 仅聚合本事务已插入的数据库行；同账户多行合并一次，避免依赖请求行顺序。
	for _, id := range ids {
		tag, err := tx.Exec(ctx, `
			UPDATE account_balance b
			SET balance_min = b.balance_min + d.delta,
			    version = b.version + 1,
			    aggregate_seq = b.aggregate_seq + 1,
			    updated_at = clock_timestamp()
			FROM (SELECT SUM(normal_delta)::bigint AS delta FROM ledger_line
			      WHERE entry_id=$1 AND account_id=$2 HAVING COUNT(*) > 0) d
			WHERE b.account_id = $2`, entryID, id)
		if err != nil { return "", err }
		if tag.RowsAffected() != 1 { return "", fmt.Errorf("projection missing") }
	}
	_, err = tx.Exec(ctx, `
		UPDATE ledger_posting_request
		SET status='COMMITTED', entry_id=$1, response_json=jsonb_build_object('entry_id',$1),
		    updated_at=clock_timestamp()
		WHERE posting_request_id=$2`, entryID, requestID)
	if err != nil { return "", err }
	_, err = tx.Exec(ctx, `
		INSERT INTO ledger_outbox(aggregate_type, aggregate_id, event_type, event_key, payload)
		VALUES ('ledger_entry',$1,'LedgerEntryPosted',$2,jsonb_build_object('entry_id',$1))`,
		entryID, "ledger-entry:"+entryID)
	if err != nil { return "", err }
	if err = tx.Commit(ctx); err != nil { return "", err } // caller must treat as UNKNOWN
	return entryID, nil
}

func canonicalPosting(p Posting, entryID string) []byte { return []byte(entryID + p.BusinessType + p.BusinessID + hex.EncodeToString(p.RequestHash)) }
func newUUID() string { return "generated-by-crypto-random-source" }
```

示例中的锁定循环还需要由实际实现补充“锁内重新计算可用余额并拒绝透支”：不能只在事务外查余额。对冻结动作，服务先按字典序锁 `AVAILABLE` 与 `FROZEN` 两行，再检查 `available.balance_min >= amount`，随后用分录 delta 同时更新两行。对于两边账户的转账，锁序必须只由规范化账户 ID 决定，不能按借贷方向决定。

### 22.6.2 Go：防止余额检查与扣减分离

```go
func reserve(ctx context.Context, tx pgx.Tx, available, frozen string, amount int64) error {
	if amount <= 0 { return fmt.Errorf("amount must be positive") }
	if available == frozen { return fmt.Errorf("distinct accounts required") }
	ids, err := sortedAccountIDs([]Line{{AccountID: available}, {AccountID: frozen}})
	if err != nil { return err }
	balances := make(map[string]int64, 2)
	for _, id := range ids {
		var balance int64
		if err := tx.QueryRow(ctx, `
			SELECT balance_min FROM account_balance
			WHERE account_id=$1 FOR UPDATE`, id).Scan(&balance); err != nil { return err }
		balances[id] = balance
	}
	if balances[available] < amount { return ErrOverdraft }
	// 后续必须在同一事务插入 D/C 两行，再从已入行聚合 delta 更新投影。
	return nil
}
```

若使用 `UPDATE ... WHERE balance_min >= amount RETURNING ...`，也可以把检查和扣减合并为数据库原子条件；但双边冻结仍需要锁定两行并插入平衡分录。不要先 `SELECT`、提交，再异步扣减；也不要依靠应用进程内 mutex，它无法保护多实例、多语言和故障恢复场景。

### 22.6.3 Java 21：JDBC 事务服务与结构化锁序

```java
package com.example.ledger;

import javax.sql.DataSource;
import java.sql.*;
import java.time.LocalDate;
import java.util.*;

public final class LedgerService {
    private final DataSource dataSource;

    public LedgerService(DataSource dataSource) {
        this.dataSource = Objects.requireNonNull(dataSource);
    }

    public record Line(UUID accountId, String currency, char side,
                       long amountMin) {}

    public record Posting(String idempotencyKey, byte[] requestHash,
                          String businessType, String businessId,
                          String postingType, LocalDate accountingDate,
                          List<Line> lines) {}

    public UUID post(Posting p) throws SQLException {
        validate(p);
        try (Connection c = dataSource.getConnection()) {
            c.setAutoCommit(false);
            c.setTransactionIsolation(Connection.TRANSACTION_READ_COMMITTED);
            try {
                UUID requestId = null;
                String sql = """
                    INSERT INTO ledger_posting_request
                      (idempotency_key, request_hash, business_type, business_id, status)
                    VALUES (?, ?, ?, ?, 'PROCESSING')
                    ON CONFLICT DO NOTHING
                    RETURNING posting_request_id
                    """;
                try (PreparedStatement ps = c.prepareStatement(sql)) {
                    ps.setString(1, p.idempotencyKey());
                    ps.setBytes(2, p.requestHash());
                    ps.setString(3, p.businessType());
                    ps.setString(4, p.businessId());
                    try (ResultSet rs = ps.executeQuery()) {
                        if (rs.next()) requestId = rs.getObject(1, UUID.class);
                    }
                }
                if (requestId == null) {
                    // 新语句、新快照；不更新冲突行，更不能接管它的 PROCESSING。
                    try (PreparedStatement ps = c.prepareStatement("""
                        SELECT request_hash, entry_id, status, business_type, business_id
                        FROM ledger_posting_request WHERE idempotency_key=? FOR UPDATE
                        """)) {
                        ps.setString(1, p.idempotencyKey());
                        try (ResultSet rs = ps.executeQuery()) {
                            if (!rs.next()) throw new SQLException("business identity already used");
                            if (!Arrays.equals(rs.getBytes(1), p.requestHash())
                                    || !p.businessType().equals(rs.getString(4))
                                    || !p.businessId().equals(rs.getString(5))) {
                                throw new SQLException("idempotency key already used for a different request");
                            }
                            UUID existing = rs.getObject(2, UUID.class);
                            String status = rs.getString(3);
                            if (!"COMMITTED".equals(status) || existing == null) {
                                throw new SQLException("existing posting is " + status + "; query with the same key");
                            }
                            c.rollback(); // 只读重放，释放锁；原结果已提交
                            return existing;
                        }
                    }
                }
                // 只有新插入者持有 requestId，能够继续发布分录。

                List<UUID> ids = p.lines().stream().map(Line::accountId)
                        .distinct().sorted(Comparator.comparing(UUID::toString)).toList();
                String lock = """
                    SELECT b.balance_min, a.allow_negative
                    FROM account_balance b JOIN ledger_account a USING(account_id)
                    WHERE b.account_id=? FOR UPDATE
                    """;
                for (UUID id : ids) {
                    try (PreparedStatement ps = c.prepareStatement(lock)) {
                        ps.setObject(1, id);
                        try (ResultSet rs = ps.executeQuery()) {
                            if (!rs.next()) throw new SQLException("account not found: " + id);
                            // 这里读取的是锁内快照；支出检查必须在此处完成。
                        }
                    }
                }

                UUID entryId = UUID.randomUUID();
                insertEntry(c, entryId, requestId, p);
                insertLines(c, entryId, p);
                for (UUID id : ids) {
                    try (PreparedStatement ps = c.prepareStatement("""
                        UPDATE account_balance b
                        SET balance_min=b.balance_min+d.delta, version=b.version+1,
                            aggregate_seq=b.aggregate_seq+1, updated_at=clock_timestamp()
                        FROM (SELECT SUM(normal_delta)::bigint AS delta FROM ledger_line
                              WHERE entry_id=? AND account_id=? HAVING COUNT(*) > 0) d
                        WHERE b.account_id=?
                        """)) {
                        ps.setObject(1, entryId);
                        ps.setObject(2, id);
                        ps.setObject(3, id);
                        if (ps.executeUpdate() != 1) throw new SQLException("projection missing");
                    }
                }
                try (PreparedStatement ps = c.prepareStatement("""
                    UPDATE ledger_posting_request
                    SET status='COMMITTED', entry_id=?,
                        response_json=jsonb_build_object('entry_id',?), updated_at=clock_timestamp()
                    WHERE posting_request_id=?
                    """)) {
                    ps.setObject(1, entryId); ps.setObject(2, entryId); ps.setObject(3, requestId);
                    ps.executeUpdate();
                }
                insertOutbox(c, entryId);
                try { c.commit(); }
                catch (SQLException commitUnknown) {
                    // 不能 rollback 后换新 key 重试；向上层返回 UNKNOWN，使用原 key 查单。
                    throw commitUnknown;
                }
                return entryId;
            } catch (SQLException | RuntimeException e) {
                try { c.rollback(); } catch (SQLException suppressed) { e.addSuppressed(suppressed); }
                throw e;
            }
        }
    }

    private static void validate(Posting p) {
        if (p.lines().size() < 2) throw new IllegalArgumentException("at least two lines");
        if (p.requestHash() == null || p.requestHash().length == 0) throw new IllegalArgumentException("request hash required");
        Map<String, Long> d = new HashMap<>(), cr = new HashMap<>();
        for (Line l : p.lines()) {
            Objects.requireNonNull(l.accountId(), "account ID required");
            if (l.amountMin() <= 0) throw new IllegalArgumentException("positive minor integer required");
            if (l.side() != 'D' && l.side() != 'C') throw new IllegalArgumentException("invalid side");
            var target = l.side() == 'D' ? d : cr;
            target.merge(l.currency(), l.amountMin(), Math::addExact);
        }
        if (!d.equals(cr)) throw new IllegalArgumentException("unbalanced per currency");
    }

    private static void insertLines(Connection c, UUID entryId, Posting p) throws SQLException {
        try (PreparedStatement ps = c.prepareStatement("""
            INSERT INTO ledger_line
              (line_id, entry_id, line_no, account_id, currency_code, side,
               amount_min, normal_delta, business_ref)
            SELECT gen_random_uuid(), ?, ?, a.account_id, ?, ?, ?,
                   CASE WHEN a.normal_side = ? THEN ? ELSE -CAST(? AS bigint) END, ?
            FROM ledger_account a WHERE a.account_id = ? AND a.currency_code = ?
            """)) {
            int lineNo = 0;
            for (Line l : p.lines()) {
                ps.setObject(1, entryId); ps.setInt(2, ++lineNo);
                ps.setString(3, l.currency()); ps.setString(4, String.valueOf(l.side()));
                ps.setLong(5, l.amountMin()); ps.setString(6, String.valueOf(l.side()));
                ps.setLong(7, l.amountMin()); ps.setLong(8, l.amountMin());
                ps.setString(9, p.businessId()); ps.setObject(10, l.accountId());
                ps.setString(11, l.currency());
                if (ps.executeUpdate() != 1) throw new SQLException("account/currency mismatch");
            }
        }
    }

    private static void insertEntry(Connection c, UUID entryId, UUID requestId, Posting p) throws SQLException {
        throw new UnsupportedOperationException("teaching skeleton: implement parameterized entry INSERT");
    }
    private static void insertOutbox(Connection c, UUID entryId) throws SQLException {
        throw new UnsupportedOperationException("teaching skeleton: implement Outbox INSERT with unique event_key");
    }
}
```

Java 21 的 `record` 适合表达请求结构，但 `byte[]` 和 `List` 仍需防御性复制；Go 的切片同样不得在调用期间被并发修改。`Math.addExact` 用于发现整数溢出；不要用 `double` 保存价格或金额。实际实现可采用 `PreparedStatement` 批量插入分录行，但不能绕过每币种平衡校验和数据库约束。

**投影协议（两种语言相同）**：请求行没有 `NormalDelta/normalDelta` 字段。插入时由数据库用 `side + ledger_account.normal_side` 计算并保存 `normal_delta`，校验账户币种并要求恰好插入一行；余额更新只按当前 `entry_id + account_id` 聚合数据库中已入行的 delta。同一账户有多行时只更新一次，`version/aggregate_seq` 因而按本次 entry 对该账户递增一次，而不是按 line 递增；聚合或余额相加超出 `BIGINT` 范围由数据库报错并整笔回滚。前文无透支触发器仍是最后一道防线，业务层的锁内可用额度/订单状态校验不能省略。所有更新必须在原事务提交前完成；JDBC 即使在 commit 异常后尝试 rollback 清理，也不能据此推断原提交失败，仍按 `UNKNOWN` 用原 key 查单。

---

## 22.7 并发控制：锁序、防透支、隔离级别与热点边界

### 22.7.1 固定锁序是全局协议，不是局部习惯

假设转账 A→B 和 B→A 同时发生：

```text
事务 T1：锁 A → 等 B
事务 T2：锁 B → 等 A
```

形成死锁。所有会修改余额、订单或配额的代码必须采用统一排序键，例如：

```text
lock_key = account_id UUID 的规范小写文本字典序（等价于 16 字节无符号字典序）
```

Go 入口拒绝非 `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` 小写十六进制格式，再用 `sort.Strings`；Java 使用 `Comparator.comparing(UUID::toString)`。**不能使用 Java `UUID.compareTo` 的自然序**：它对两个 64 位部分作有符号比较，跨 `7fff…`/`8000…` 边界时会与 Go 文本序相反。所有语言、后台任务和 SQL 写入口都必须遵循相同顺序。

流程：

1. 收集事务涉及的全部行 ID，去重；
2. 按固定字典序排序；
3. 依序 `SELECT ... FOR UPDATE`；
4. 锁齐后才读取并检查余额、订单版本和业务状态；
5. 插入分录、更新物化余额、更新业务状态；
6. 尽快提交，不在事务内调用外部 HTTP、Kafka 或撮合服务。

订单行也要纳入锁序。可定义全局键空间：

```text
O:<order_id>  <  A:<account_id>  <  B:<account_id>
```

或者将订单锁和账户锁拆成两个严格规定的阶段，但所有写路径必须遵循同一个协议。本章先取得幂等占位/原请求锁，再锁订单、账户；不能其他入口先锁账户再等待同一个幂等键。不能冻结路径“先订单后账户”，成交路径“先账户后订单”，然后期待 PostgreSQL 永远替你解决。

### 22.7.2 防透支的三种层次

**层次一：单行可用余额。** 适用于一个账户就是完整可支用限额的场景。锁行后检查：

```text
new_available = old_available - debit_amount
new_available >= 0
```

**层次二：可用/冻结/在途多行总约束。** 适用于订单账户：只允许从 `AVAILABLE` 转到 `FROZEN`，不能用 `FROZEN` 的余额去满足新的提现。锁定同一客户、币种的相关子账户，检查每个业务维度的可用式：

```text
withdrawable = AVAILABLE - pending_withdrawal - compliance_hold
withdrawable >= requested_amount
```

**层次三：分桶账户的全局额度。** 一个逻辑账户拆成 N 个桶后，不能只读取桶总和然后从任意桶扣款。两个并发请求可能各自看到总余额足够，最后某些桶透支。正确方案是：

- 事先从核心额度行向支出桶转移/预留可用额度；
- 出账只消费已分配额度；
- 额度转移本身也是复式分录；
- 或者对全局额度行串行化，接受吞吐下降。

“随机分桶”只能降低写热点，不能消除全局资金约束。

### 22.7.3 PostgreSQL 隔离级别选择

| 隔离级别 | 适合用途 | 风险/处理 |
|---|---|---|
| `READ COMMITTED` | 已按行锁定的余额发布、单订单状态迁移 | 每条语句有新快照；所有关键判断必须在锁后进行 |
| `REPEATABLE READ` | 需要事务内稳定读取的批处理/对账快照 | 并发更新可能 `serialization failure`，必须有限退避重试 |
| `SERIALIZABLE` | 低频全局限额、复杂跨行约束 | 冲突率和延迟高；重试必须原幂等键，不能生成新事实 |
| advisory lock | 分布式协调/迁移中的短锁 | 不是行级数据约束；必须定义命名空间、超时和释放策略 |

核心账务通常采用 `READ COMMITTED + 明确 `FOR UPDATE` 锁序 + 数据库约束。不要把“提升到 `SERIALIZABLE`”当作无需设计锁序的万能药；它只能把部分竞态变成事务回滚，仍需要正确重试和幂等。

`FOR UPDATE SKIP LOCKED` 适合 Outbox relay、对账任务领取和后台清扫，不适合客户扣款：跳过一行会把“暂时被锁”误当成“没有余额”或漏掉必须处理的账户。

### 22.7.4 死锁、锁等待和重试

数据库死锁是可恢复的技术错误，但重试必须满足：

1. 事务完全回滚；
2. 使用同一业务身份和同一幂等键；
3. 重新执行前检查原请求是否已经提交；
4. 指数退避加随机抖动，设置最大次数和总耗时；
5. 记录死锁图、锁等待、SQL 标签和业务 ID。

提交超时不等于死锁，也不等于失败。`COMMIT` 的响应丢失时，结果是 `UNKNOWN`；只能查询幂等表或数据库状态收敛，不能立刻插入反向分录，更不能换新幂等键重跑。

### 22.7.5 热点账户的边界

一个“平台手续费收入”总账、一个“银行结算户”或一个“保险基金”可能是天然热点。锁等待超过阈值时，先问是不是把所有业务错误地汇聚到同一物理行：

- 高扇入收入：按稳定哈希分成固定收入桶，低频归集到核心科目；
- 高扇出支出：使用核心额度向支出桶预留，不能任意从桶总和扣款；
- 单一全局限额：接受串行化，或使用配额/预留协议；
- 报表查询热点：读副本/汇总表解决，不能为了查询去改事实模型。

分桶扩容不能直接把 `N=64` 改成 `N=128`，否则同一业务重试会命中不同桶。使用带版本的 `bucket_epoch`，旧 epoch 保留，迁移以真实内部转移分录完成，再切换读写版本。热点、分桶、Group Commit 和多币种细节在第 23 章展开。

---

## 22.8 对账、测试压测、极端故障、术语与后续衔接

### 22.8.1 多层对账：证明余额不是“看起来正确”

至少建立四层不变量和差异闭环：

| 层次 | 比较对象 | 频率 | 差异动作 |
|---|---|---|---|
| L0 | 每个 entry 每币种借方=贷方 | 每笔提交 | 事务拒绝，不能入账 |
| L1 | 分录聚合 vs `account_balance` | 实时抽样/分钟级 | 标记账户、冻结高风险出账、重建投影 |
| L2 | 客户子账户合计 vs 平台逻辑科目/资产负债表 | 分钟级/日终 | 生成差异单，按业务日和币种定位 |
| L3 | 平台托管资产 vs 银行/CEX/链上外部余额 | 日终及关键事件 | 进入对账工作流，禁止手工抹平 |

重建查询示例：

```sql
WITH rebuilt AS (
  SELECT l.account_id,
         SUM(l.normal_delta)::bigint AS rebuilt_balance
  FROM ledger_line l
  JOIN ledger_entry e ON e.entry_id = l.entry_id
  WHERE e.status IN ('POSTED','VOIDED_BY_REVERSAL')
  GROUP BY l.account_id
)
SELECT b.account_id, b.balance_min, COALESCE(r.rebuilt_balance, 0) AS rebuilt_balance
FROM account_balance b
LEFT JOIN rebuilt r USING (account_id)
WHERE b.balance_min <> COALESCE(r.rebuilt_balance, 0);
```

真实生产环境要按会计日、币种、分桶 epoch 和快照 LSN 分段重建，避免长事务压垮主库。差异处理必须保存：差异快照、首次发现时间、涉及 entry、责任系统、处置人、修复分录和复核结果。禁止直接执行 `UPDATE account_balance SET balance_min = rebuilt_balance` 作为“修复”。

### 22.8.2 测试矩阵

**本次示例验证边界**：从 Markdown 提取代码，在临时目录使用 Go 1.24.13 + pgx v5.7.6 完成编译、`go vet` 和 `go test -race`（4 项单测：UUID 锁序、非规范 UUID 拒绝、整数加法边界、请求无独立 delta）；使用 JDK 21.0.13.1 完成 `javac -Xlint:all --release 21`，并通过 18 项 mock JDBC 断言（首次取得所有权后继续执行至未实现 helper、重放、哈希/业务身份冲突、非终态拒绝、回滚、跨符号位锁序等）。这些不是数据库集成测试：未连接 PostgreSQL，SQL、行锁等待、延迟约束、投影与提交原子性仅作静态审查，仍须按下列矩阵实测；教学占位 helper 不代表完整服务已可运行。

**单元测试：**

- 每币种借贷平衡；三行/四行/多币种输入；金额为 0、负数和溢出；
- 资产、负债、收入正常方向的 `normal_delta`；
- 冻结金额、成交消费金额、剩余释放金额的守恒；
- 状态迁移白名单、重复撤单、成交后撤单、取消中的竞态；
- 规范化哈希在 Go/Java 对同一输入产生相同结果。
- UUID 首/后 64 位跨符号位边界时 Go/Java 锁序一致；Go 拒绝大小写、短写和错误分隔符等非规范 UUID。

**数据库集成测试：**

- 同一幂等键并发 100 次只产生一个 `entry_id`；
- 首次插入占位能继续入账；同 key 同 hash 提交后重放，同 key 不同 hash（包括已 `COMMITTED`）拒绝；已有非终态不被接管；
- 同一 `business_id` 换幂等键也只能有一个事实；
- 同账户多行及不同正常方向下，投影增量严格等于数据库已入行 `SUM(normal_delta)`；零行插入、缺失投影和聚合溢出都整笔回滚；
- A→B 与 B→A 并发不死锁或能有限重试；
- 两个并发扣款合计超过余额时，恰好一个成功；
- 任意事务异常注入后，entry、line、余额、订单和 outbox 不出现半提交；
- 直接 `UPDATE/DELETE ledger_entry/ledger_line` 被拒绝；
- 每币种平衡约束拦截错误分录。

**属性/模型测试：**

随机生成冻结、成交、撤单、重复消息和事务回滚序列，使用一个单线程参考模型计算总权益；数据库结果必须满足：

```text
客户总权益 = AVAILABLE + FROZEN + SETTLEMENT + RESTRICTED
客户总权益变化 = 外部存入 - 外部取出 + 已确认交易损益/费用
每个 entry、每个币种：debit_sum = credit_sum
```

不要只断言“API 返回 200”；要在测试结束后从不可变分录重建，验证投影、订单快照和 Outbox 的一致性。

### 22.8.3 压测方案和指标

压测至少包含以下场景：

1. 均匀账户、均匀币种，测基础吞吐；
2. 单一账户/单一币种热点，测锁等待和尾延迟；
3. 50% 重试、随机重复成交消息，测幂等命中率；
4. A→B/B→A 反向转账，测死锁和重试成功率；
5. 冻结/成交/撤单混合，测状态机竞态；
6. 不同 Group Commit 批大小，测 WAL、commit 延迟和失败批影响；
7. 主库 failover、连接断开、随机杀进程、网络分区，测 `UNKNOWN` 收敛；
8. 对账重建与在线写入并行，测快照和资源边界。

核心指标：

```text
TPS / 成功率 / UNKNOWN 数量与年龄
p50/p95/p99/p999 端到端延迟
row lock wait、deadlock、serialization failure
事务持续时间、WAL bytes、fsync latency、连接池等待
幂等命中率、Outbox lag、差异账户数、重建耗时
负余额尝试数、异常冲正数、单账户/单桶流量
```

验收标准必须按业务风险写，例如“0 个已提交账户负余额”“0 个 entry 币种不平”“故障注入后差异最终为 0”“所有 commit timeout 在查单 SLA 内收敛”，而不是只写“平均 TPS 达到 20,000”。

### 22.8.4 极端故障与处置手册

**数据库提交响应丢失：** 标记调用方 `UNKNOWN`，保留原 key；查询 `ledger_posting_request`、entry 和 outbox。若已提交，返回原结果；若未提交，才允许同 key 重试。

**主库在 entry 已写、余额未写时宕机：** 只要仍在同一事务，恢复后由 WAL 回滚整个事务；禁止异步“补余额”。若发现半成品，说明事务边界或权限被绕过，立即冻结受影响账户并从分录/审计定位。

**Outbox 发布失败：** 账本事务不回滚，relay 使用 `FOR UPDATE SKIP LOCKED` 领取并重复投递；消费者以 `event_key`/聚合序列幂等。Outbox 延迟不能被解释成账务失败。

**撮合消息重复、乱序：** `match_id` 唯一；订单按撮合序列或版本校验，旧版本进入重放/人工队列。不能按消息到达时间盲目成交。

**撤单与成交同时到达：** 锁订单，按撮合序列处理；只计算实际剩余冻结。若无法确认外部撮合最终状态，订单进入 `CANCEL_PENDING`，资金保持冻结，不能乐观释放。

**物化余额漂移：** 立即标记账户为 `RECONCILIATION_HOLD`，禁止高风险出账；以固定快照从不可变分录重建，定位是漏投影、重复投影、手工越权还是分录缺失。修复用补偿分录或受控重建，并保留审批链。

**锁等待/死锁雪崩：** 限制事务和锁等待超时，保护数据库连接池；检查是否出现反向锁序或全局热点；不要通过无限重试把流量放大。热点账户按第 23 章的分桶/配额方案迁移。

**备份恢复到旧时间点：** 账本必须有连续 WAL、备份校验和恢复演练。恢复后以最后已确认 LSN、幂等表和外部清算回执确定边界；任何“恢复后重新消费所有消息”的方案都必须依赖原业务身份去重。

### 22.8.5 术语表

| 术语 | 精确定义 | 不是它 |
|---|---|---|
| 复式记账 | 每币种借贷相等、每笔变动有明确对手账户的记录体系 | “改两个余额字段” |
| 科目/账户 | 具有会计方向、所有者、币种和用途的资金身份 | 用户表中的一个余额列 |
| 分录 `entry` | 描述一个原子经济事实的一组分录行 | 一条任意业务日志 |
| 分录行 `line` | 指向一个账户、币种、借贷方向和正整数金额的不可变事实 | 可覆盖更新的流水明细 |
| 正常方向 | 科目余额增加时通常使用借方或贷方的规则 | API 中正负号的随意约定 |
| 物化余额 | 从分录增量维护的在线读模型和锁模型 | 可以独立修改的事实源 |
| 冻结 | 可用子账户向冻结子账户的复式转移，限制支用 | 把一个 `frozen` 字段改成数字 |
| 成交 | 撮合事实触发的报价币/基础币交收及费用分录 | 收到一条未幂等的消息 |
| 撤单 | 对未成交剩余冻结追加释放分录并结束订单 | 把全部冻结余额清零 |
| 过渡户 | 跨腿、跨系统交收期间暂存头寸的真实科目 | 用于永久隐藏差额的垃圾桶 |
| 幂等键 | 同一请求重试时保持不变、唯一映射到结果的键 | 每次重试新生成的 UUID |
| `UNKNOWN` | 提交/外部结果尚未确定，必须查单收敛 | `FAILED` 或可随便重试 |
| 锁序 | 所有事务按同一规范化键顺序取得锁 | 某个方法内部的临时排序 |
| 防透支 | 在锁保护下证明扣款后的可支用额度不为负 | 先读余额、稍后再扣 |
| Outbox | 与账本事务同提交、供异步发布的事件表 | 可以代替账本的消息队列 |
| 冲正 | 追加与原分录方向相反并关联原单的新事实 | 修改或删除原分录 |
| 最小单位整数 | 币种约定的最小计量单位的整数个数 | 带小数的展示金额 |

### 22.8.6 十分钟面试答题卡

**题目：设计一个支持 2 万 TPS、订单冻结/成交/撤单、不可透支和审计的核心账本。**

1. **先报红线**：最小单位整数；每币种借贷平衡；entry/line 只追加；余额是物化视图；commit 后 ACK；超时 `UNKNOWN`；无裸改余额。
2. **画三层**：业务请求/状态机 → 不可变分录事实 → 余额投影/Outbox/报表；订单状态可更新，经济事实不可更新。
3. **讲模型**：账户有 owner、科目类型、币种、用途和正常方向；客户至少分 AVAILABLE/FROZEN/SETTLEMENT；不同币种独立平衡。
4. **讲 DDL**：`ledger_entry`、`ledger_line`、`account_balance`、幂等请求、订单成交和 Outbox；数据库拒绝历史修改，约束金额正数和每币种平衡。
5. **讲冻结**：锁订单与可用/冻结账户，检查额度，在一个 entry 中 AVAILABLE→FROZEN；状态和投影同事务提交。
6. **讲成交**：以不可变 `match_id` 幂等；按成交数量消费冻结，交付 base，结算 quote，手续费有明确科目；部分成交只消费部分冻结。
7. **讲撤单**：进入 `CANCEL_PENDING`，等待撮合序列确认；只释放剩余冻结；重复撤单无资金动作。
8. **讲并发**：所有账户按规范化 ID 排序锁；锁后检查余额；`READ COMMITTED + FOR UPDATE`；死锁/序列化失败有限重试，原 key 不变。
9. **讲故障**：提交响应丢失是 UNKNOWN；从幂等表查单；Outbox 重复发布由消费者去重；绝不换 key 重记或手工抹平余额。
10. **讲规模和取舍**：真正热点不能靠随机分桶解决全局额度；使用固定 epoch、支出配额和归集分录，或接受单写者串行。用 p99、锁等待、WAL、差异账户和 UNKNOWN 年龄验收。

### 22.8.7 三道自检题

**题 1：** 两个提现请求同时看到客户可用余额 100，分别要提 80，为什么应用层 `SELECT balance` 后判断会透支？正确做法是什么？

**答案要点：** 两个事务可能读到同一快照，各自判断通过后再扣减。必须在同一事务中按统一锁序锁定可用账户，锁后检查并更新，或使用带 `balance_min >= amount` 条件的原子更新；插入扣款复式分录和幂等记录，提交失败不能产生半笔事实。

**题 2：** 买单先成交 60%，随后撤单。为什么撤单不能把原冻结金额全部转回可用？

**答案要点：** 成交已消费一部分报价币冻结，并可能产生手续费和交收过渡头寸。撤单只能释放 `original_reserve - consumed_reserve - already_released` 的剩余值；订单锁和成交序列必须确定结果，释放动作有独立 `ORDER_CANCEL_RELEASE` 分录，重复撤单不再产生资金变化。

**题 3：** 账本服务在 `COMMIT` 后、HTTP ACK 前崩溃，客户端重试是否可以换一个幂等键？余额投影如何证明没有漂移？

**答案要点：** 不能换 key；客户端结果是 UNKNOWN，使用原 key 查询幂等表并返回原 `entry_id`。对账时按 entry/line 的正常方向重建账户余额，与 `account_balance`、逻辑科目合计和外部托管余额逐层比较；差异冻结并调查，不能直接 UPDATE 余额消警。

### 22.8.8 与第 23 章及后续章节衔接

本章交付的是**单账户、基础分录、订单状态和行锁并发模型**，边界如下：

| 本章产物 | 第 23 章扩展 | 后续章节衔接 |
|---|---|---|
| `ledger_account` / `account_balance` | 热点逻辑科目拆成固定 epoch 的收入/支出/核心桶 | [24] 全链路幂等、[27] 多层对账 |
| 不可变 `entry/line` | 红字冲正、蓝字补记、部分冲正与多币种 FX 过渡户 | [25] 状态机、TCC/Saga/Outbox |
| 统一锁序与防透支 | 支出额度预留、分桶扩容和 Group Commit | [26] 清结算、[34] 综合账户与券商账务 |
| `ledger_posting_request` | 批量请求的独立幂等和清扫幂等 | [24] 客户端→网关→账本→渠道全链路幂等 |
| `ledger_outbox` | 热点批次、发布延迟和消费者聚合序列 | [25] 事件驱动状态收敛 |
| 余额重建与四层对账 | 按桶、币种、epoch 的亿级重建和差异自动闭环 | [27] 实时双流核对与自动修复 |

从本章进入第 23 章时，必须保留以下接口契约：金额仍是最小单位整数；`entry_id`、`line_id`、`business_id` 不复用；余额更新仍只能是分录投影；热点分桶不能绕过全局额度；事务提交超时仍然是 `UNKNOWN`。第 23 章会在不破坏这些不变量的前提下，解决单一结算户的万级 TPS 锁热点、不可变冲正和多币种独立平衡问题。

---

**本章一页速记：**

```text
事实：entry + line 只追加；每币种 debit = credit；金额是最小单位正整数
余额：account_balance 是物化视图；投影可重建，不能裸改
状态：NEW → OPEN → PARTIALLY_FILLED/FILLED；撤单走 CANCEL_PENDING → CANCELED
资金：冻结 AVAILABLE→FROZEN；成交消费部分冻结；撤单只释放剩余冻结
并发：收集全部行 → 按统一键排序锁 → 锁内检查额度 → 分录/投影/状态/Outbox 同事务
可靠性：commit 后 ACK；超时 UNKNOWN；原幂等键查单，绝不换 key 重记
规模：随机分桶不能解决全局防透支；热点需配额/预留或串行化，详见第 23 章
```
