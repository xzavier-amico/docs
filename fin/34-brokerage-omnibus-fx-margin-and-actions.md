# 34. 券商核心业务架构：Omnibus、银证换汇、融资融券与公司行动

> **状态**：工程篇｜**所属**：第四部分·支付、券商、风控、撮合与链上交易核心架构｜**编号**：34 / 48
>
> **本章定位**：承接 [05 股票与公司行动](05-equities-adjustments-margin-and-actions.md)、[20 定点算术](20-monetary-precision-rounding-and-allocation.md)、[22 复式账本](22-double-entry-ledger-schema-and-concurrency.md)、[25 状态机与 Outbox](25-state-machines-tcc-saga-and-outbox.md)、[27 对账](27-multi-tier-reconciliation-and-auto-healing.md) 与 [29 OMS](29-oms-fix-gateway-and-algo-execution.md)，把外部一个综合账户拆成内部可证明的客户权益。
>
> **红线**：总额相等不证明客户分配正确；客户资产不能替公司补洞；超时不是失败；报价到期不是外部成交撤销；拆股不得凭空增减成本；税务调整不改交易现金；所有金额使用最小货币单位整数，数量使用 `q8=10^-8 股`，禁止浮点。

---

## 34.1 教学事故：总账对平了，客户仍然少了权益

以下为**教学合成事故**，时间、数量与损失均为演算假设，不对应真实券商历史。

| 时间 | 事件 | 错误处理 | 应当保留的事实 |
|---|---|---|---|
| 09:00:00 | 银行已扣款，券商查询超时 | 解冻后换流水重发 | 原转账身份、冻结、`UNKNOWN` |
| 09:00:03 | 3 秒锁汇报价进入发送队列 | 到期任务释放资金 | 外部是否已经接受，尚不知道 |
| 09:00:04 | 原报价实际已成交 | 用新汇率覆盖旧报价 | 原报价、成交号、应收应付 |
| 16:00:00 | X 股 1 合 3 | 每客户截断为整股 | 碎股权益、上游零股处置规则 |
| 16:00:01 | 上游旧挂单仍存活 | 本地改成已撤并解冻 | 撤单待确认、晚到成交 |
| 次日 02:00 | 清算方总仓正确 | 把所有尾差塞到客户甲 | 客户逐笔 lot 与权利登记快照 |

手算损害：同一入金重复记可用 `100,000 美分`，这首先是**错误负债/潜在敞口**，不是已实现损失；若重复款已提出且最终无法追回，才形成假设损失 `100,000 美分`。遗漏 `0.5 股`，按假设补购价 `6,000 美分/股` 恢复权益需 `3,000 美分`，不应再把同一权益市值重复加算为第二笔损失。

### 34.1.1 唯一真问题与边界

> 外部托管只认一个 Omnibus 账户时，系统怎样证明每位客户的现金、证券、债务、税基与公司行动权益均来自唯一事实，并在网络未知、跨日、重放和规则修订后仍可重建？

本章区分四个主体：客户/受益所有人、介绍券商、持有资产的清算券商或托管人、银行/外汇交易对手。交易执行 venue 不一定是资产托管 location；智能路由只能在权限、借券、结算与最佳执行约束内选路，不能因为某路线便宜就挪用另一法律实体的资产。

```text
客户指令 → 身份/资产隔离/风险闸门 → 分户账 + 预占 + Outbox
                                      │
        ┌─────────────────────────────┼─────────────────────────┐
        ▼                             ▼                         ▼
 银证/FX 适配器                 OMS/借券适配器             公司行动处理器
        │                             │                         ▲
 银行/FX 对手                  清算券商 Omnibus ──公告/权利/税单─┘
        └───────────────── 原文归档 / 查单 / Inbox ──────────────┘
                                      │
                          控制总账 ↔ 客户分户 ↔ 外部对账单
```

### 34.1.2 制度边界不是一个全局开关

美国证券客户保护可参考 SEC Rule 15c3-3，香港客户款项/证券分别受当地规则约束，内地银证转账依托当地账户、存管和业务许可。它们不是同一种账户制度；本章银证是“银行与证券资金账之间转移”的工程抽象，不声称跨境自由兑换或牌照通用。

本章利率、税率、日基数、碎股处理和 TTL 均为显式教学配置。上线必须冻结 `legal_entity + jurisdiction + product + policy_version + effective_time`，由法务、税务与清算协议确认，不能从客户国籍或股票上市地猜税务适用性。

---

## 34.2 Omnibus：三层核对与客户资产隔离

### 34.2.1 同一边界才有同一个等式

内部控制总账 `G` 是分户汇总的独立控制账户，外部确认量为 `E`，客户分户为 `C_i`。固定法律实体、外部账户、资产、单位、结算日、状态与截止序列后：

```text
G = Σ C_i + P + U                         （内部总分恒等式）
E = G + B                                （外部勾稽等式）
B = Σ 已逐笔识别且尚未同时体现的时间差桥接项
```

`P` 是依法允许且单独标识的公司头寸，`U` 是待认领/待分配权益；不允许混放时 `P=0`。`B` 每项必须带业务 ID、方向、发生日、预计消差日与证据，不能把“刚好差的数”填进去。冻结是客户权益内部状态，不因冻结再增加一份证券或现金。

**教学核对**：客户甲已结算 `60 股`、乙 `40 股`，公司另有合法标识的 `1 股`，内部 `G=101`；外部已结算 `E=100`，有公司已入账但外部未交收买入 `1 股`，因此 `B=-1`。成交日视图可显示 `101`，结算日视图外部仍是 `100`，不能硬改客户少 `1 股`。

长仓、空仓、借入、借出、质押与非控制地点应分别核对：客户甲多 `100`、乙空 `100` 的净额为零，并不证明甲的 `100` 股已获适当保管。分户逐笔核对还需检查 `客户 + instrument + lot + 来源成交/行动`；把甲乙互换不会改变总数，却已侵犯权益。

### 34.2.2 隔离必须落到外部法律关系

| 隔离层 | 必须做到 | 不能替代什么 |
|---|---|---|
| 法律/合同 | 客户专用账户标识、托管协议、适用的无抵销/留置安排 | 不凭数据库标签获得破产隔离 |
| 银行/托管 | 自营资金与客户保护资产按制度分开；控制地点核验 | 不用自营“现金充足”证明客户资产可提 |
| 账务 | 客户信用余额、客户融资债权、税款应付分科目 | 不把客户负现金直接冲掉其他客户正现金 |
| 权限 | 提款、借券、抵押与自营调拨不同权限；四眼审批 | 不因可操作托管 API 就获得合法使用权 |
| 合规计算 | 按当地规则做储备、占有/控制及短缺处理 | 不把 `Σ余额=银行余额` 当完整监管计算 |

美国 Rule 15c3-3 对适用券商的 fully paid / excess margin securities 的占有或控制、客户储备等有专门要求及例外；并非所有客户现金都要“一户一银行账号”，也不是“客户资产永远不能合法借出”。证券出借需独立授权、协议、抵押与披露，不能由 margin 开关默认推导。

路由决定存 `route_decision_id`、候选集、报价新鲜度、费用、交收日、借券可用性与合规拒绝原因。一个外部子订单被接受后，重路由须先证明原单未成交/已撤；跨路线未知态不能靠换清算方消除。

---

## 34.3 银证未知态与 TTL 锁汇：到期能拒绝新接受，不能抹掉旧义务

### 34.3.1 两条状态机，不能合成一个“成功”字段

```text
银证：CREATED → HELD → SUBMITTING → ACCEPTED → SETTLED
                         │            │
                         └→ UNKNOWN ←─┘（受理后交收仍可能未知）
UNKNOWN ─权威查单/终态回执→ ACCEPTED / SETTLED / REJECTED
HELD ─未发送且原子关闭发送权→ CANCELED
SETTLED ─银行退回证据→ RETURNED（追加反向事实，不覆盖原成功）

报价：ISSUED ─原子接受且 now < expires_at→ CLAIMED → SUBMITTING
       └─到期且未 CLAIMED→ EXPIRED                 │
                     EXECUTED ←权威回执─ UNKNOWN ←┘
                       │                  └─确定未执行且不可迟到接受→ REJECTED
                       └→ SETTLING → SETTLED
```

`ACCEPTED` 只是渠道受理；`SETTLED` 才满足本产品约定的入账/可提条件。出金在 `HELD` 先把客户可用负债转到冻结负债；入金可以登记在途备查，但未取得合格确认前不能凭 HTTP 200 给可提现余额。

每个银证转移使用稳定的 `(entity, customer_request_id)` 和外部 `client_ref`，请求体保存 hash；同 key 不同金额拒绝。同一事实的回调、主动查询、对账文件必须竞争同一行锁/版本，且按事件身份去重。一次 `NOT_FOUND` 可能是索引延迟，必须依据渠道承诺的终态查单窗口和拒绝再受理机制，才允许释放资金。

**两端非原子**：银行扣款不等于清算券商入账，保存银行腿、券商腿与中间往来应收。单腿成功时挂在途并查另一腿，不立即新建相反转账；确需退回，必须先排除另一腿晚成功，再建立独立补偿身份。

### 34.3.2 锁的是哪一种汇率承诺

报价保存源/目标币、源金额、目标金额、费、`rate_num/rate_den`、价值日、对手与报价 ID。本文比例定义为**目标最小单位 / 源最小单位**，不是没有方向的“汇率”；不同货币小数位已经纳入比例。

例如源 `70,000 CNY 分`，`rate_num=5, rate_den=36`，则目标向下取整为 `9,722 USD 美分`，余数 `8/36 美分`；另收源币费用 `20 分`，原子预占 `70,020 分`。客户确认页展示最终整数和费，不能让前端重算成另一个数。外部成交金额不同，要挂差异并按协议分担，不覆盖原客户承诺。

两种产品不能混淆：① 对手提供 firm quote，必须在其到期前获得有效接受；② 券商自报 TTL、接受后自己承担对冲价格差。若只是 indicative quote，不得宣传为保证成交价。`CLAIMED` 只证明本地接受，不证明外部锁汇成功；对手拒绝过期报单后，是拒绝客户还是券商按约履行，由产品契约确定。

### 34.3.3 并发接受与截止时间

以下是**事务协议伪代码**，不是已经实现的数据库函数：

```text
BEGIN；先锁 request 幂等身份，再锁 quote，再按稳定顺序锁资金账户
若 request 已存在：校验 hash，返回原结果（即使现在报价已到期）
校验 owner、币种、金额、权限、policy；锁后读取数据库 clock_timestamp()
仅当 quote=ISSUED 且 now < expires_at：冻结 source+fee，quote→CLAIMED
同事务写 request、hold、状态事件与发送 Outbox；COMMIT 后异步发送
发送/等待超时：原 client_ref 进入 UNKNOWN；不释放 hold、不换报价重发
权威成交回执：Inbox 去重 + 状态 CAS + 应收应付/分录 + Outbox 同事务
```

PostgreSQL `now()` 固定为事务开始时间，锁等待后可能已经过期，故接受点不能用它；这里取锁后的 `clock_timestamp()`。本地接受截止与对手截止有网络差，生产应预留安全裕量，并记录真实对手接受时间。到期 worker 与接受事务锁同一 quote；只能把尚未接受的 `ISSUED` 变 `EXPIRED`。

FX 确认与交割分开：源币可记“借客户冻结负债、贷 FX 源币应付”，目标币记“借 FX 目标币应收、贷客户受限负债”；源币交付记“借应付、贷托管现金”，目标币收款记“借托管现金、贷应收”，再按可用规则解限。各币种独立平衡，手续费另作收入与现金/负债分录；报告币估值与汇兑损益不靠把两种币金额相加产生。

---

## 34.4 DDL 与协议：事实、状态和投影分别存

下列是可独立建表的 **PostgreSQL 最小控制模型**，不等于完整监管账簿。金额 `bigint` 为货币最小单位，乘积用宽整数；`numeric(38,0)` 只存整数。资产元数据须固定 q8/货币单位版本；大整数 JSON 一律字符串。

```sql
CREATE TABLE br_account (
  id uuid PRIMARY KEY, entity text NOT NULL, owner_ref text NOT NULL,
  external_account text NOT NULL, asset text NOT NULL,
  role text NOT NULL CHECK (role IN ('CUSTOMER','HOUSE','SUSPENSE','CONTROL')),
  bucket text NOT NULL, units numeric(38,0) NOT NULL DEFAULT 0,
  version bigint NOT NULL DEFAULT 0,
  UNIQUE(entity, owner_ref, external_account, asset, role, bucket)
);
CREATE TABLE br_journal (
  id uuid PRIMARY KEY, entity text NOT NULL, business_key text NOT NULL,
  payload_hash bytea NOT NULL CHECK (octet_length(payload_hash)=32),
  reverses uuid REFERENCES br_journal(id), effective_at timestamptz NOT NULL,
  policy_version text NOT NULL, UNIQUE(entity, business_key)
);
CREATE TABLE br_posting (
  journal_id uuid NOT NULL REFERENCES br_journal(id), line_no integer NOT NULL,
  account_id uuid NOT NULL REFERENCES br_account(id),
  signed_units numeric(38,0) NOT NULL CHECK (signed_units<>0),
  PRIMARY KEY(journal_id, line_no)
);
CREATE TABLE br_fx_quote (
  id uuid PRIMARY KEY, entity text NOT NULL, owner_ref text NOT NULL,
  source_ccy text NOT NULL, target_ccy text NOT NULL,
  source_minor bigint NOT NULL CHECK(source_minor>0),
  target_minor bigint NOT NULL CHECK(target_minor>0),
  fee_minor bigint NOT NULL CHECK(fee_minor>=0),
  rate_num bigint NOT NULL CHECK(rate_num>0), rate_den bigint NOT NULL CHECK(rate_den>0),
  counterparty text NOT NULL, provider_quote_id text NOT NULL,
  value_date date NOT NULL, expires_at timestamptz NOT NULL,
  policy_version text NOT NULL, state text NOT NULL CHECK
    (state IN ('ISSUED','CLAIMED','EXPIRED')), version bigint NOT NULL DEFAULT 0,
  CHECK(source_ccy<>target_ccy), UNIQUE(counterparty, provider_quote_id)
);
CREATE TABLE br_operation (
  id uuid PRIMARY KEY, entity text NOT NULL, owner_ref text NOT NULL,
  request_key text NOT NULL, kind text NOT NULL CHECK(kind IN ('BANK','FX','BORROW')),
  quote_id uuid UNIQUE REFERENCES br_fx_quote(id),
  payload_hash bytea NOT NULL CHECK(octet_length(payload_hash)=32),
  channel text NOT NULL, client_ref text NOT NULL, state text NOT NULL,
  version bigint NOT NULL DEFAULT 0, hold_ref text NOT NULL,
  bank_leg_ref text, broker_leg_ref text, next_query_at timestamptz,
  UNIQUE(entity, request_key), UNIQUE(channel, client_ref)
);
CREATE INDEX br_query_pending ON br_operation(next_query_at) WHERE state='UNKNOWN';
CREATE TABLE br_message (
  direction text NOT NULL CHECK(direction IN ('IN','OUT')),
  channel text NOT NULL, message_id text NOT NULL,
  operation_id uuid REFERENCES br_operation(id), payload jsonb NOT NULL,
  payload_hash bytea NOT NULL CHECK(octet_length(payload_hash)=32),
  created_at timestamptz NOT NULL, handled_at timestamptz,
  PRIMARY KEY(direction, channel, message_id)
);
CREATE TABLE br_accrual (
  contract_ref text NOT NULL, accrual_date date NOT NULL, revision integer NOT NULL,
  principal_minor bigint NOT NULL CHECK(principal_minor>=0),
  rate_ppm bigint NOT NULL CHECK(rate_ppm>=0), basis integer NOT NULL CHECK(basis>0),
  denominator bigint NOT NULL CHECK(denominator>0),
  carry_in bigint NOT NULL CHECK(carry_in>=0), carry_out bigint NOT NULL CHECK(carry_out>=0),
  interest_minor bigint NOT NULL CHECK(interest_minor>=0), policy_version text NOT NULL,
  input_cutoff bigint NOT NULL, journal_id uuid NOT NULL REFERENCES br_journal(id),
  PRIMARY KEY(contract_ref, accrual_date, revision),
  CHECK(carry_in<denominator AND carry_out<denominator)
);
CREATE TABLE br_action (
  entity text NOT NULL, source text NOT NULL, action_id text NOT NULL, revision integer NOT NULL,
  instrument text NOT NULL, kind text NOT NULL, state text NOT NULL,
  ex_date date NOT NULL, record_date date NOT NULL, pay_date date,
  terms jsonb NOT NULL, source_hash bytea NOT NULL CHECK(octet_length(source_hash)=32),
  entitlement_cutoff bigint NOT NULL, policy_version text NOT NULL,
  PRIMARY KEY(entity, source, action_id, revision)
);
CREATE TABLE br_entitlement (
  entity text NOT NULL, source text NOT NULL, action_id text NOT NULL, revision integer NOT NULL,
  owner_ref text NOT NULL, lot_ref text NOT NULL, qty_q8 numeric(38,0) NOT NULL,
  gross_minor bigint NOT NULL, tax_minor bigint NOT NULL, net_minor bigint NOT NULL,
  tax_profile_version text NOT NULL, journal_id uuid REFERENCES br_journal(id),
  PRIMARY KEY(entity, source, action_id, revision, owner_ref, lot_ref),
  FOREIGN KEY(entity, source, action_id, revision) REFERENCES br_action,
  CHECK(gross_minor=tax_minor+net_minor)
);
CREATE TABLE br_tax_lot_event (
  entity text NOT NULL, owner_ref text NOT NULL, event_id text NOT NULL,
  lot_ref text NOT NULL, instrument text NOT NULL, acquired_at timestamptz NOT NULL,
  qty_delta_q8 numeric(38,0) NOT NULL, basis_delta_minor bigint NOT NULL,
  action_ref text NOT NULL, tax_policy_version text NOT NULL,
  PRIMARY KEY(entity, owner_ref, event_id)
);
```

**数据库约束边界**：`CHECK` 不能跨行保证借贷平衡。生产只能经受控记账入口写 journal/posting，锁账户后校验同实体、按资产 `Σsigned_units=0`、客户桶不可透支及冻结覆盖；同事务更新余额投影。撤销写新 journal 并引用原凭证，事实表通过角色权限禁止更新/删除。证券数量使用独立数量账，不能把借券备查和托管现货重复加进一个桶。

`br_operation.state` 是通用载体，应用须按 kind 实施白名单转移并追加状态审计；DDL 没有实现 TCC、借券合同或完整税务申报。`br_message` 承担 Inbox/Outbox 事实，实际还需按 aggregate 排序、投递租约与重试记录；同身份异 hash 隔离，不用 `ON CONFLICT DO NOTHING` 吞掉冲突。并行公司行动按客户/证券序列化或用版本栅栏。

```json
{
  "schema_version": 1, "entity": "BROKER_A", "owner_ref": "C001",
  "request_key": "fx-client-001", "quote_id": "00000000-0000-0000-0000-000000000034",
  "source_ccy": "CNY", "source_minor": "70000", "fee_minor": "20",
  "target_ccy": "USD", "target_minor": "9722", "rate_num": "5", "rate_den": "36",
  "policy_version": "fx-v3", "expected_quote_version": "0"
}
```

回执至少带 `channel + message_id + client_ref + provider_trade_id + status + executed_amounts + value_date + provider_event_time`，经签名/mTLS、重放防护与原文归档验证。请求金额与 quote 必须逐字段匹配，不能允许客户传更小 source 搭配原 target；缺字段不得默认成功。

---

## 34.5 融资融券计息：本金、计日与余数都有身份

融资借现金与融券借证券是两种合同：前者按合格借款本金计息，后者通常按约定证券估值、费率及抵押安排计借券费。`locate` 不是已经借入，借入不是已卖空；归还以对手确认及约定价值日为准，不以买回成交时刻自动停费。召回、buy-in、负 rebate、最低收费与代偿股息均须单独建模。

教学融资配置：单利、ACT/360、年利率 `80,000 ppm=8%`、每日截断至美分并携余，周末按实际日数计；不自动把应计利息并入本金。定义：

```text
D = 1,000,000 × basis
N_d = principal_minor_d × rate_ppm_d × actual_days_d + carry_in
interest_minor_d = floor(N_d / D)；carry_out = N_d mod D
```

本金 `1,000,000 美分`，一天 `N=80,000,000,000`，`D=360,000,000`，得 `222 美分`、余 `80,000,000`；第二天仍 `222`、余 `160,000,000`；三天合计 `666 美分`、余 `240,000,000`。不能把余数当额外 `240,000,000 美分`。月底/销户怎样处理不足一分，由合同规定，不能无条件向客户上取整。

若改 ACT/365，余数分母改变，不能原封不动携带；需在精确有理数层换算或按获批结息规则关闭旧段。本金变动、阶梯利率、节假日多日计息均按生效区间切段；同一日多个区间先精确累加再按合同舍入，不能每小时收一次一分钱。日切冻结本金来源 cutoff，迟到的追溯还款通过修订差额冲补，不能直接覆盖历史计息行。

融券手算：约定计费估值 `100 股 × 5,000 美分/股=500,000 美分`，年费 `120,000 ppm`，ACT/360 一天 `166 美分`、余 `240,000,000/360,000,000 美分`；三天精确 `500 美分`。这是费用，不是股票本金；借券库存另记数量与对手义务。空头代偿股息不一定享有普通股息的税务待遇。

### 34.5.1 Go 1.24+：同一个整数内核计算 FX、拆股与携余计息

以下代码可独立编译，**只覆盖非负整数比例与教学单利**，不含事务、税务判断、负费率、融资资格或网络恢复；不是完整券商服务。`math/big` 保中间乘积，最终显式检查 int64；热点可换经过边界证明的宽整数实现，不能换 float。

```go
package brokerage

import (
    "errors"
    "math/big"
)

// 返回 floor(a*b/den) 及余数；不修改传入对象，无共享可变状态。
func Ratio(a, b, den int64) (int64, int64, error) {
    if a < 0 || b < 0 || den <= 0 {
        return 0, 0, errors.New("invalid ratio domain")
    }
    n := new(big.Int).Mul(big.NewInt(a), big.NewInt(b))
    q, r := new(big.Int), new(big.Int)
    q.QuoRem(n, big.NewInt(den), r)
    if !q.IsInt64() { return 0, 0, errors.New("amount overflow") }
    return q.Int64(), r.Int64(), nil
}

func Accrue(principal, ratePPM, days, basis, carry int64) (int64, int64, error) {
    if principal < 0 || ratePPM < 0 || days <= 0 ||
        (basis != 360 && basis != 365) {
        return 0, 0, errors.New("unsupported accrual domain")
    }
    den := basis * 1_000_000
    if carry < 0 || carry >= den { return 0, 0, errors.New("bad carry") }
    n := new(big.Int).Mul(big.NewInt(principal), big.NewInt(ratePPM))
    n.Mul(n, big.NewInt(days))
    n.Add(n, big.NewInt(carry))
    q, r := new(big.Int), new(big.Int)
    q.QuoRem(n, big.NewInt(den), r)
    if !q.IsInt64() { return 0, 0, errors.New("interest overflow") }
    return q.Int64(), r.Int64(), nil
}
```

`Ratio(70000,5,36)` 得 `9722,8`；`Ratio(100000000,1,3)` 得 `33333333,1`，第二个余数代表 `1/3 q8`，不是 `1/3 股`。费用加法另用受检加法；拒绝溢出后不得已冻结未记账地继续执行。Go 账务选 `pgx/v5 + sqlc` 显式事务，JSON 金额用 string 强类型解析，不让 `map[string]any` 把它变成 float64。

### 34.5.2 Java 21+：同契约、同向量、同舍入

```java
import java.math.BigInteger;

public final class BrokerageMath {
    private BrokerageMath() {}
    public record Result(long units, long remainder) {}
    private static Result divide(BigInteger n, long denominator) {
        BigInteger[] qr = n.divideAndRemainder(BigInteger.valueOf(denominator));
        return new Result(qr[0].longValueExact(), qr[1].longValueExact());
    }
    public static Result ratio(long a, long b, long denominator) {
        if (a < 0 || b < 0 || denominator <= 0)
            throw new IllegalArgumentException("invalid ratio domain");
        return divide(BigInteger.valueOf(a).multiply(BigInteger.valueOf(b)), denominator);
    }
    public static Result accrue(long principal, long ratePpm, long days,
                                long basis, long carry) {
        if (principal < 0 || ratePpm < 0 || days <= 0 || (basis != 360 && basis != 365))
            throw new IllegalArgumentException("unsupported accrual domain");
        long denominator = Math.multiplyExact(basis, 1_000_000L);
        if (carry < 0 || carry >= denominator)
            throw new IllegalArgumentException("bad carry");
        BigInteger n = BigInteger.valueOf(principal).multiply(BigInteger.valueOf(ratePpm))
            .multiply(BigInteger.valueOf(days)).add(BigInteger.valueOf(carry));
        return divide(n, denominator);
    }
    public static long reserve(long source, long fee) {
        if (source <= 0 || fee < 0) throw new IllegalArgumentException("bad reservation");
        return Math.addExact(source, fee);
    }
}
```

Java 选 `TransactionTemplate + jOOQ/JDBC` 使事务边界显式，避免注解自调用漏事务；税率与公告十进制可在入口用精确 `BigDecimal(String)` 转定点，严格拒绝多余 scale。两栈算法必须共享 golden vectors、规则版本和溢出行为；它们均未实现负数分摊，冲正应引用已确定的原结果而非重新套正数公式。

---

## 34.6 公司行动：先确定权利，再动证券、现金与税基

### 34.6.1 行动与订单的双重栅栏

```text
ANNOUNCED → VALIDATED → ENTITLEMENTS_FROZEN → CALCULATED → APPROVED
                    │                              │
                    └─公告冲突→ QUARANTINED         ▼
                                    POSTED → EXTERNAL_CONFIRMED → RECONCILED
已入账后更正：新 revision → 差额/冲正计划 → 再审批；不改原权利快照
```

除权日、登记日、支付日不同；权利集合依据公告、清算交收、due bill 和借贷安排确定，不是简单查询支付日持仓。公告修订以 source/action/revision 去重；同 revision 异 hash 停机隔离。分配键固定到客户/lot，原始权利快照、税务档案、数量分子分母与 rounding policy 一并归档。

对受影响证券发布 action epoch，停止接受旧精度委托并处置旧挂单。`CANCEL_PENDING` 不释放尚未确认的义务；回执未齐或晚到成交未处理完成时，不能跨 epoch 自动重新下单。切换通过各清算方订单水位/确认屏障，不靠“等 5 秒应该全撤完”。

**挂单不是全世界一律撤销**：FINRA Rule 5330 对适用 open order 的现金股息/拆股调整、DNR/DNI 等有具体定义与例外；反向拆股待成交买卖单须取消，交易所规则管辖订单等另有边界。系统根据 venue、订单类型、指示和规则版本选择撤销/重确认/调整，不把所有卖单都套现金除息减价；重新挂单需按适用规则取得授权。

### 34.6.2 派息与扣税手算

教学例：客户甲合格权利 `125 股`，每股股息 `37 美分`，gross=`4,625 美分`；假设其已获验证的档案适用 `15%`，本例按客户本事件 HALF_UP，tax=`694 美分`，net=`3,931 美分`。公式用整数 `gross×150000/1000000`，余数判断舍入；15% 不是全球默认税率。

若上游支付税后净额：确认 gross 时借股息应收 `4,625`，贷客户净股息应付 `3,931`、上游代扣税清算 `694`；收款时借托管现金 `3,931`、借代扣税清算 `694`，贷股息应收 `4,625`。代扣税清算不是本公司收入，税单/代扣凭证证明其结清。若本券商负责扣缴，则保留真实税款应付，实际汇缴时再结清，不能重复扣两次。

美国来源股息付给外国人，适用情形下通常有 30% 法定预扣、税收协定/法律例外可能降低；需核验受益所有人、W-8/W-9、收入分类及有效期。美国人 backup withholding、REIT/PTP、资本返还、借券代偿款不能全部套普通股息模板。上游重分类与退补税追加新修订；每客户舍入与上游按税池舍入不一致的差额单列解释，不能任意从一客户净额扣平。

### 34.6.3 拆合股、八位碎股与对冲户

正向 `2:1`：`75 股 → 150 股`，原总成本 `300,000 美分` 不变，单位成本减半；历史成交价格不覆写，复权行情是独立展示序列。反向 `1:3`：旧 `1 股` 的精确新权益为 `1/3 股`，q8 只表示 `33,333,333` 单位，还剩 `1/3 q8` 的有理残量。

三个客户各 `1 股`，分别截断后合计 `99,999,999 q8`，外部新总量 `100,000,000 q8`，差 `1 q8`。应保留各客户精确残余权益，按已披露、合规的最大余数法或汇总现金处置规则分配；不得让第三客户永久少份额。合法的过渡/对冲户可暂记该 `1 q8` 的托管数量及对客户残余义务，**不是无主收入，也不代表 1 q8 能在交易所独立交易**。

上游若只交付整股、其余 cash-in-lieu，内部支持碎股也不等于上游支持；必须说明内部碎股的受益权、对冲覆盖、不可转仓限制和现金处置权。整股对冲户的残量必须真实购入/卖出并留存成交，不能造一笔“虚拟对冲”使总分相等。

现金替代例：`5 股`、成本 `30,000 美分`，`1:3` 后为 `1+2/3 股`；按比例给 `1 股` 成本 `18,000`，`2/3 股` 成本 `12,000`。若实际现金处置收入 `13,000`，碎股实现收益 `1,000 美分`（忽略费用与其他税务调整），剩余股票成本仍 `18,000`。价格、费用、税务性质取真实上游处置报告；不是在除权日用任意市价提前结清。

### 34.6.4 FIFO 不等于所有税务，wash sale 不等于禁止交易

FIFO 教学：lot A=`100 股/100,000 美分`，B=`50 股/60,000 美分`；卖 `120 股` 收 `132,000`、费 `120`，净收入 `131,880`，消耗 A 全部和 B 的 `20 股/24,000`，总成本 `124,000`，实现收益 `7,880`；B 剩 `30 股/36,000`。部分 lot 成本分摊按累计精确比例计算并保留余数，最后一段吃尽原成本，不能每次向下取整导致成本凭空消失。

FIFO 可作为产品默认，但美国证券税基也可能允许满足要求的具体 lot 指定，部分基金有平均成本等规则。拆股保留 lot 取得日/谱系，换股与分拆依公告税务属性重分配成本；不能把新证券创建日一律当买入日。成本法变更需授权、时点与审计，不能用户看到亏损后随意改历史。

**美国 wash sale 的适用范围**：以 IRC §1091 和适用税法为准，典型是受该美国所得税规则约束的纳税人，在亏损处置股票/证券的前后各 30 个自然日内取得 substantially identical 股票/证券或相关购买合约/期权。窗口共 61 日，不是 30 个交易日；它递延或否认税损，不是系统必须拒绝卖出，也不是撮合自成交 STP。

普通应税账户简例：卖 `100 股`，成本 `100,000`、收入 `80,000`，损失 `20,000 美分`；10 日后买回 `40 股` 成本 `34,000`，匹配部分税损 `8,000` 暂不可扣，替代 lot 税基变 `42,000`，其余 `12,000` 在无其他匹配和规则限制时可扣。经济现金仍只支付 `34,000`；持有期按适用规则承接，不把递延税损记成客户现金返款。

范围陷阱：跨券商/账户、配偶、受控公司和 IRA/Roth IRA 取得可能相关；IRA 替代买入可能导致损失不能扣且不增加 IRA basis，不能照搬普通递延。券商 Form 1099-B 的强制识别范围可能仅覆盖同账户同 CUSIP 等条件，远窄于纳税人完整义务；`本券商未发现` 不能写成 `不存在 wash sale`。

对外国客户不能仅因交易美股就普遍套此税损规则，应由税务身份及所得适用性判断；对所有数字资产也不得机械推广，需逐产品核验其法律分类和生效法规。`substantially identical` 不是单纯字符串相等，更不是“相关系数高”；人工税务规则库管理映射，复杂期权/空头/§475 选择等走专门规则，不让本例 FIFO reducer 冒充全套税引擎。

实现保存亏损处置的待匹配数量、窗口截止、替代 lot 匹配边、税基调整事件和已使用量，避免一份买入重复覆盖多笔亏损。先查前 30 日，再订阅后 30 日含跨年交易；截止未到标 `PROVISIONAL`，迟到成交、拆股比例变更与转仓成本补齐都可触发可审计重算。

---

## 34.7 对账、测试压测与十分钟面试卡

### 34.7.1 五组证据闭环

| 核验对象 | 主键与不变量 | 差异处置 |
|---|---|---|
| Omnibus 总分/外部 | entity/account/asset/location/settlement/cutoff；`G=ΣC+P+U, E=G+B` | 差异逐笔编号，限制受影响资产转出，保留无争议业务 |
| 银证 | 原 client_ref 对银行腿、券商腿、在途与客户分录 | UNKNOWN 老化升级，银行成功不凭空补另一腿 |
| FX | quote→provider trade→两币种现金交割；金额、价值日、费 | 交易确认差与交割差分开；禁止净美元差盖住源币缺口 |
| 融资/借券 | 合同本金/证券数量、费率版本、天数、carry 与总账应计 | 合同外部费率差、追溯还款差各建修订；不裸改本金 |
| 公司行动/税基 | 外部 gross/tax/net、分户权利、拆股守恒、成本谱系 | 来源重分类、尾差、未付款分别挂账并追加修正 |

对账文件先验来源、完整性、行数、总量、hash、版本与截止时区，再进入 staging；缺失文件不是零资产。美国夏令时、跨市场假日与价值日错位采用第 21/26 章日历和 cutoff 口径。对已确认少券不得只建“可忽略尾差”：独立核查控制地点与客户隔离短缺，并按制度升级。

### 34.7.2 可执行验证矩阵

| 测试 | 注入 | 必须观察到 |
|---|---|---|
| 算术属性 | 极值、0、负值、超 int64 乘积；随机整数比例 | 正数域 `qD+r=N, 0≤r<D`；拒绝越界，两栈逐位一致 |
| 并发锁汇 | 同报价 1,000 次并发；同 key 异 body；到期锁等待 | 一个 operation、一份 hold；冲突报错；截止后不得新接受 |
| 网络混沌 | 发送后断电、回调先于响应、重复/逆序回执 | 重启查原身份；一次终态入账；不得 UNKNOWN 解冻 |
| 切日计息 | 周五到周一、月末、跨年、追溯还款、分母切换 | 实际天数正确，余数不丢，日终重跑不重复收费 |
| 行动重放 | 三户 1:3、1q8 残差、公告 revision、撤单晚成交 | 数量/成本守恒、epoch 拦截、差额修订可重放 |
| 税务 | 第 -30/+30 日、+31 日、部分买回、IRA、跨账户缺数据 | 匹配边不复用，适用范围明确，未知范围不宣称完整 |
| 恢复对账 | 分配中途崩溃、外部缺文件、旧 worker 复活 | 原幂等身份续跑、旧 epoch 写入失败、差异不被覆盖 |

压测是**验收方案而非实测宣称**：准备 100 万客户、1,000 万 lot、同一热门证券 30 万客户；银证/FX 接受目标 2,000 请求/秒，持续 30 分钟、5 倍突刺 60 秒；一个行动 100 万权利行按固定分片跑完并对拍。报告机器、数据库/索引/WAL 配置、数据分布、锁等待、p50/p99、GC、Outbox lag、UNKNOWN 老化和恢复时长。

不要用真实银行完成吞吐压测：对手模拟器注入 1% 超时、1% 重复、乱序与延迟，另外做小规模获授权认证环境联调。接受路径示例预算 p99≤50ms，外部交割按渠道 SLA 单列，不把网络等候算作本地锁持有时间；行动目标如“30 分钟内完成”须由实测验证，正确性门槛始终是零重复经济事实、零未解释数量差。

### 34.7.3 术语与误区速记

| 术语 | 正确定义 / 高频误区 |
|---|---|
| Omnibus / fully disclosed | 前者上游聚合、下游分户；后者上游识别各客户，均不免除账簿责任 |
| Beneficial owner | 受益所有人，不一定是名义登记人；税务穿透不由订单 user_id 决定 |
| Possession or control | 符合制度的占有/控制，不等于账上写着有券 |
| Firm quote / TTL | 有约束报价 / 接受有效期；不是成交后的自动撤销定时器 |
| Value date / carry | 交割价值日 / 精确分数余量；carry 不是额外最小货币单位 |
| Ex / record / payable | 除权 / 登记 / 支付时点，不能互换 |
| Cash-in-lieu / tax basis | 零碎权益现金替代 / 税务成本，不等于市场估值或现金余额 |
| FIFO / wash sale | lot 消耗顺序 / 特定美国税损规则；二者都不等于交易执行顺序 |

**十分钟答题卡**：① 1 分钟界定实体、托管、客户隔离与制度；② 2 分钟画总分外部三层账和证据流；③ 2 分钟讲银证两腿、TTL 接受点、UNKNOWN 不解冻；④ 2 分钟给计息余数、拆股残量与税基手算；⑤ 2 分钟讲幂等 DDL、行动 epoch、对账和混沌；⑥ 1 分钟报容量假设、恢复边界及不能自动解决的合规判断。追问“总数平了为何还错”，回答客户/lot/权利归属和控制地点都可能错。

### 34.7.4 自检题与答案

1. **报价发送超时，TTL 已过，能释放 `70,020 分` 吗？** 不能；只对未接受的报价过期。原请求可能已执行，保留 hold 查原 client_ref，取得确定终态与不可迟到受理证据后才走对应解冻/结算。
2. **三个 1 股客户各做 1:3，q8 差多少？** 每户截断 `33,333,333`，合计比外部 1 股少 `1 q8`；分别保留 `1/3 q8` 残权并按规则分配/处置，不能当公司收入。按股计算成差 1 股是单位错误。
3. **卖出亏 `20,000 美分`，窗口内普通应税账户买回 40% 为何税基增 `8,000`？** 本例等额每股损失按匹配量递延；买回成本 `34,000→42,000`，原交易现金不变。若是 IRA 或另有匹配/豁免，不能照抄此结果。
4. **融资本金 1,000,000 美分、8%、ACT/360 连续三日，收多少？** 本章截断携余配置下为 `666 美分`，余 `240,000,000/360,000,000 美分`；舍入结息日另按合同，不是每天四舍五入收 222 后永久丢尾差。

---

## 34.8 系统衔接：从券商受益权到账外托管与签名隔离

向 [29 OMS](29-oms-fix-gateway-and-algo-execution.md) 输出公司行动 epoch、撤改义务与新精度；向 [30 风控](30-pretrade-risk-margin-and-liquidation-engine.md) 输出已确认资产、真实融资/借券债务与受限桶，未交割 FX 目标币不能无条件算可提抵押品；向 [27 对账](27-multi-tier-reconciliation-and-auto-healing.md) 输出逐笔桥接项与外部证据，而非一个总差数字。

下一章 [35 托管与充提币](35-crypto-custody-hd-wallet-mpc-and-sweep.md) 把“外部持有人替客户保管”延伸到地址、冷热钱包和 MPC/HSM：银行 client_ref 对应提币业务身份，Omnibus 分户对应地址/钱包归集下的受益权分配，但链上余额、签名能力与客户法律权益仍是三件事。第 36 章继续处理链上重组与 Nonce；不能把“已广播”当银证意义上的已交收。

**来源与上线核验清单**（访问核验：2026-11-26；动态规则须保存适用版本，不构成税务或法律建议）：

- [SEC Rule 15c3-3，法规文本镜像](https://www.law.cornell.edu/cfr/text/17/240.15c3-3)：适用券商、客户定义、占有/控制、储备及例外；本文总分模型不是其完整储备公式，也不固定通用计算频率。
- [FINRA Rule 5330](https://www.finra.org/rules-guidance/rulebooks/finra-rules/5330)：open order 定义、现金/股票分配调整、反向拆股撤单、DNR/DNI 和交易所规则等例外。
- [IRS Publication 550（2025 年版）](https://www.irs.gov/publications/p550)：税基、具体识别/FIFO、拆股和 wash sales；重点区分纳税人义务、1099-B 报告范围与 IRA 例外。
- [IRS NRA withholding](https://www.irs.gov/individuals/international-taxpayers/nra-withholding)：美国来源所得付给外国人的预扣、协定及档案边界；上线另核当期 Publication 515、表单说明与具体收入分类。
- 银证协议、FX master agreement/quote API、融资与借券合同、清算公司行动公告、税务顾问确认：确定终态、TTL、价值日、计息天数、尾差与碎股处置的直接依据，不能由本文教学参数替代。
