# 26. 清结算生产级工程：净额轧差、DVP、分润、保证金与 7×24 日切

> **状态**：生产级工程篇｜**所属**：第三部分·金融工程代码基座｜**编号**：26 / 48
>
> **本章定位**：承接 [21 时间与日历](21-financial-time-calendar-and-sequence-id.md)、[22 不可变账本](22-double-entry-ledger-schema-and-concurrency.md)、[23 分桶与冲正](23-hot-accounts-reversals-and-fx-ledger.md)、[24 幂等](24-end-to-end-financial-idempotency.md)，把订单转为可交收义务，完成净额、DVP、分润、保证金、不停机日切及在途对账。[26.8 衔接](#settlement-next-layer) 连接后续对账、状态机和风控。
>
> **唯一真问题**：在订单持续流入、跨市场日历、重复消息、部分交付、外部银行/托管人不原子、日切不停止服务的条件下，系统能否证明“每个应收应付只产生一次、每种币种借贷平衡、券款不裸奔、旧日与新日不串批、最终结果可由原始事实和对账文件重建”？

---

## 26.1 引子：轧差少了一腿，DVP 变成先券后款

### 26.1.1 一次看似节省资金的事故

某平台同时提供证券清算、交易员保证金和代理商分润。系统将订单直接按“成交成功”写入各账户，没有独立的清算义务和交收批次。月末 17:00 日切附近，事故时间线如下：

| 时间 | 事件 | 错误实现 | 后果 |
|---|---|---|---|
| 16:59:59.970 | 买方 A、卖方 B 成交 100 股 | 只写两笔成交订单，未生成不可变交收义务 | 后续重试没有统一事实键 |
| 17:00:00.001 | 网关收到补充订单 | 网关按“收到时间”判旧日，数据库按提交时间判新日 | 同一投资组合跨两个批次 |
| 17:00:00.120 | 结算任务开始 | 只按交易对手两两支付，未做多边净额 | 现金占用约为净额的 4 倍 |
| 17:00:01.000 | 托管人回调“证券已转出” | 回调消费者先交付券，再等待银行付款 | 买方现金不足，形成裸券风险 |
| 17:00:02.000 | 分润服务重试 | 每层代理用浮点计算并各自四舍五入 | 1 分尾差、多发佣金、无法解释 |
| 17:00:04.000 | 清算服务超时重试 | 用新 `settlement_id` 重做原批次 | 轧差义务和账本事实重复 |
| 次日对账 | 银行文件少一笔 | 人工把余额改平 | 原始错误被掩盖，重放后再次漂移 |

问题不是“批处理慢”或“锁不够多”，而是把五个层次混成了一个 `order.status=SUCCESS`：

1. **成交事实**：撮合已经发生什么；
2. **清算义务**：谁在什么价值日、以多少数量交付什么；
3. **净额结果**：同一参与者同一币种同一价值日最终只需付/收多少；
4. **交收动作**：券和款的两条腿是否在可证明的条件下同时完成；
5. **会计事实**：账本中哪些分录已经生效、哪些只是暂记或冻结。

### 26.1.2 五条红线

> **清算不是改订单状态，轧差不是覆盖明细，DVP 不是先转一腿再等另一腿，日切不是停机换表，分润不是每层独立四舍五入。**

生产实现至少遵守以下红线：

1. **成交明细不可变**：任何更正产生新版本、冲正或补记，不更新原成交金额。
2. **清算义务可重放**：`trade_id + clearing_rule_version` 唯一生成义务；消息重复不能多出一笔应收应付。
3. **净额可反算**：任意净额结果必须能从明细义务按版本、参与者、币种、价值日重算；净额不是事实源。
4. **每币种独立平衡**：USD、CNY、证券数量、费用和保证金不能用一个总金额抵消。
5. **DVP 有条件交付**：内部先冻结/预留，外部无法原子提交时使用可恢复的两阶段状态机和暂记账户；不能宣称跨机构 exactly-once。
6. **分润基于快照**：费率树、资格、有效期、舍入版本都绑定到原始事实；子代理不能因重试重新算出另一套树。
7. **保证金不等于余额**：可用保证金需扣除初始保证金、维持保证金占用、在途结算、折扣和风控保留。
8. **日切连续服务**：新请求进入新逻辑日，旧逻辑日由排空/重试任务继续运行；epoch 栅栏阻止旧 worker 写入新批次。
9. **cutoff 采用半开区间**：`accepted_at < cutoff` 属于旧窗口；等于 cutoff 的请求属于下一窗口或按明确协议拒绝。
10. **未知不等于失败**：外部超时、commit 超时、日切锁竞争都只能进入 `UNKNOWN/REVIEW`，不能凭空生成反向交收。

### 26.1.3 读者应能回答的核心问题

- 一笔成交为什么要先变成 obligation，再进入 netting？
- 交易对手有 100 笔互相抵消的订单，为什么不能只保留最后余额？
- 在银行和托管人不共享数据库时，DVP 如何降低裸露窗口？
- 16:59:59.999 收到的订单由哪个逻辑日负责？网络已到网关但尚未入库的订单呢？
- 7×24 服务怎么在不拒绝全站流量的情况下冻结旧批次、切换日历和升级规则？
- 多级分润的尾差归谁？一笔退款如何按原树冲回，而不是按当前树重算？
- 轧差后金额正确，但银行文件差一笔，如何定位是漏发、重复发还是价值日错配？

---

## 26.2 清结算对象模型：从成交到最终交收

### 26.2.1 六层事实模型

```text
撮合/订单事实 trade
       │  immutable + trade_id
       ▼
清算义务 obligation
       │  买方/卖方、资产、数量、现金、价值日、规则快照
       ▼
净额组 netting_group
       │  member + currency/security + value_date + epoch
       ▼
净额结果 netting_result
       │  每个参与者最终收/付方向与数量
       ▼
交收指令 settlement_instruction
       │  DVP 两条腿、外部引用、租约、栅栏、回执
       ▼
复式账本 posting + reconciliation evidence
```

这六层不能用一张 `settlement_order` 替代：

| 对象 | 是否可变 | 主要身份 | 作用 |
|---|---:|---|---|
| `trade` | 否 | `trade_id` | 记录撮合结果和原始价格/数量 |
| `obligation` | 否 | `obligation_id`、`trade_id + leg` | 表达一方对另一方的交付承诺 |
| `netting_group` | 创建后规则冻结 | `group_id`、维度键 | 定义可否互相抵销的边界 |
| `netting_result` | 生成后只冲正 | `group_id + member + asset` | 净额后单一方向义务 |
| `settlement_instruction` | 状态可推进，事实不可覆盖 | `instruction_id`、外部幂等键 | 驱动实际转账/转券 |
| `posting` | 追加，不更新删除 | `posting_key` | 账户、暂记户、冻结和释放的会计事实 |

清算状态不能倒推成交状态。成交已经成功而交收失败时，成交仍是事实，交收产生失败、待补交或冲正事实；不能把已撮合的成交改回“未成交”来隐藏缺口。

### 26.2.2 清算维度键

净额只能在协议允许的维度内发生。推荐的最小键为：

```text
netting_key = {
  legal_entity,
  clearing_member,
  counterparty_or_CCP,
  market,
  product,
  asset_or_currency,
  value_date,
  settlement_system,
  settlement_session,
  netting_rule_version,
  logical_day_id,
  epoch
}
```

通常不能跨以下维度净额：

- 不同法律实体、不同清算会员或不同托管账户；
- 不同币种，除非显式生成 FX 交易和汇率快照；
- 不同价值日，除非协议把远期/回购腿定义为同一可抵销组合；
- 证券和现金，不能把股票数量与现金金额加成“总价值”；
- 不同抵押品等级、不同优先级、不同监管隔离池；
- 不同规则版本，除非有版本迁移和审计批准。

### 26.2.3 最小不变量

设某净额组 `g` 中每个参与者 `p`、资产 `a` 的原始义务为 `o`，约定“正数为应收、负数为应付”：

```text
net(g,p,a) = Σ signed_amount(o)
Σ_p net(g,p,a) = 0                  # 同一资产/币种内
gross(g,p,a) = Σ abs(signed_amount(o))
```

必须同时检查：

1. `net` 只改变交收动作数量，不删除任何 `o`；
2. 证券数量在最小可交付单位上为整数，现金使用币种最小单位整数；
3. 每条 obligation 恰好有一条收方腿和一条付方腿，或有明确的失败/取消理由；
4. `settlement_instruction` 的外部请求键唯一；查询、回调、文件三条入口归并到同一指令；
5. 已发送但未知结果的指令不可自动换键重发；先查单、人工确认或按外部协议重试原键；
6. 净额完成后，明细总和、净额总和、已交收总和和未结总和可相互校验；
7. 账本 posting 按币种分别借贷平衡，暂记账户的余额有明确释放/冲正路径。

---

## 26.3 逻辑日、epoch、cutoff 与在途单归属

### 26.3.1 三种时间、两个日期、一个 epoch

继续使用 [21] 的原则：

| 概念 | 例子 | 用途 | 是否可作为唯一顺序 |
|---|---|---|---:|
| `occurred_at_utc` | `2026-11-25T08:59:59.999Z` | 订单/成交真实事件时间 | 否，需业务序列辅助 |
| `accepted_at_utc` | 服务端可信接纳时间 | cutoff 归属、审计 | 否，需唯一 ID/序列 |
| `trading_date` | 交易所 2026-11-25 | 交易规则 | 否 |
| `accounting_date` | 上海 17:00 切出的 2026-11-26 | 会计报表 | 否 |
| `logical_day_id` | `XSHG:2026-11-25:REGULAR` | 清结算批次标签 | 是标签，不是时钟 |
| `epoch` | `18427` | 防旧 worker 越权写新批次 | 在作用域内单调递增 |

**逻辑日**是由市场、会计主体、日历版本和 cutoff 规则共同计算出的业务窗口，不等于 UTC 自然日。**epoch** 是这个逻辑窗口的可排序版本号，用于租约和栅栏；日历或规则发生受控变更时，即使展示日期不变，也可以升级 epoch。

推荐把窗口写成：

```text
logical_window = [window_start_utc, window_end_utc)
accepted_at_utc < window_end_utc  → 当前 logical_day_id / epoch
accepted_at_utc >= window_end_utc  → 下一 logical_day_id / epoch
```

不允许使用 `23:59:59.999` 作为结束点，也不允许使用本机 `now()` 在不同服务分别判断归属。由统一的 `CalendarService` 计算并返回不可变的 `cutoff_token`，token 至少含：`logical_day_id`、`epoch`、`accepted_at_utc`、`calendar_version`、`rule_version` 和过期时间。

### 26.3.2 “已到网关”不等于“已接纳”

生产协议要明确在途单的判定边界：

```text
CLIENT_SENT
  └─► GATEWAY_RECEIVED       # 可能只存在于访问日志，不保证业务归属
        └─► ACCEPTED_DURABLE # DB/日志已持久化 cutoff_token，归属确定
              └─► CLEARED / SETTLEMENT_PENDING / ...
```

推荐规则：

1. 只有成功写入 `settlement_work_item` 或等价 durable log 的请求才称为 `ACCEPTED_DURABLE`。
2. `ACCEPTED_DURABLE` 在 cutoff 之前：归旧逻辑日，即使 worker 在日切后才执行。
3. 只到达网关、排队内存、TCP 已收到但尚未持久化的请求：不能声称属于旧日。若响应结果不明，返回 `202 UNKNOWN`，由客户端用原幂等键查询；重新接纳时按新日处理，不能双重接纳。
4. cutoff 恰好相等：使用 `>=` 进入下一窗口；若业务要求缓冲区，缓冲区也必须是日历规则的一部分，不能由 worker 临时决定。
5. 成交事件已经由撮合引擎以 `match_seq` 确认的，清算归属优先使用撮合市场的交易日和序列；清算服务收到消息的时间只能用于延迟和在途监控。
6. 旧日已封账后到达的修正、撤销、拒付等不是“改回旧日”，而是在当前会计日追加一条关联原事实的冲正/调整；报告同时展示原交易日和调整日。

### 26.3.3 7×24 不停机日切协议

日切不是全局 `stop-the-world`，而是一个有栅栏的状态机：

```text
OPEN
  │ 到达 freeze_at / 发布新 epoch
  ▼
FREEZING ──拒绝旧窗口的新接纳，允许已接纳任务继续
  │
  ├─ worker 续租旧 epoch，处理已持久化 work item
  ├─ 新请求使用新 epoch
  └─ 对旧 epoch 做可重复的 drain / snapshot
  ▼
DRAINING ──等待可安全收敛，超时任务转 PENDING/UNKNOWN，不强杀
  ▼
SEALED ──旧日不再新增正常事实，只允许冲正/修正入口
  ▼
RECONCILING ──生成快照、净额、缺口和对账任务
  ▼
CLOSED ──归档策略生效，原始事实仍可查询
```

关键实现：

- `cutover` 事务锁定某个 `(market, calendar_version, logical_day_id)` 的控制行，递增 epoch，并写入 `cutover_event`；它不锁全站订单表。
- 网关每次接纳时读取当前 epoch，带 `FOR SHARE` 或版本条件写入 work item；旧 epoch 进入 `FREEZING` 后，不能再插入新的正常 work item。
- worker 必须携带 `epoch + fencing_token`。每次更新批次状态都带条件：`WHERE batch_id=? AND epoch=? AND fencing_token=?`；租约过期的旧 worker 即使恢复，也只能读，不能提交副作用。
- 日切检查要有最大 drain 时间。超时不是强制把未完成单标成失败，而是转 `PENDING_REVIEW`，记录已发外部请求、最后心跳和查单计划。
- 账本和交收在 7×24 下持续可用；只对“批次封存、净额快照、报表生成”做逻辑边界，不对写入链路断电。
- 新版本日历先以 `PROVISIONAL` 发布，经过校验和审批再成为 `ACTIVE`。已使用的日历版本永不静默替换。

### 26.3.4 例：17:00 cutoff 的四个请求

假设 `Asia/Shanghai` 17:00 日切，旧窗口 `D20261125/E900`，新窗口 `D20261126/E901`：

| 请求 | durable accepted | 归属 | 解释 |
|---|---|---|---|
| A | 16:59:59.999Z 对应本地 16:59:59.999 | D25/E900 | `< cutoff`，旧 worker 可继续 |
| B | 本地 17:00:00.000 | D26/E901 | 半开区间右边界不属于旧日 |
| C | 网关日志 16:59:59，但 DB 16:... 后才写入 | 按 durable 接纳时计算 | 日志不能替代业务事实 |
| D | 旧日已发送银行，回执次日抵达 | 仍关联 D25/E900 | 回执到达日不是原交收归属；必要时在 D26 追加调整分录 |

### 26.3.5 时钟漂移与保护模式

若节点与可信时间源偏差超过阈值，例如 `±500 ms`（实际值由业务风险和网络定标），网关不得猜 cutoff：

1. 进入 `CLOCK_UNSAFE`，拒绝临界区间的旧日接纳或返回 `202`；
2. 记录时间源、偏差、最后同步时间和节点 ID；
3. 使用中心日历服务签发的 server-side `accepted_at`，不采用客户端时间；
4. 监控恢复后，按幂等键查单，不让“保护性拒绝”变成重复订单。

---

## 26.4 净额轧差：减少结算腿，但不减少经济事实

### 26.4.1 双边与多边净额

**双边净额**只在一对参与者之间按资产/币种和价值日抵销：

```text
A 应付 B 100 USD
B 应付 A  70 USD
双边净额：A 应付 B 30 USD
```

**多边净额**在同一法律/清算协议边界内计算每个参与者的净位置：

```text
A 应付 B 100 USD       B 应付 C 90 USD
C 应付 A  80 USD       A 的净额 = -20 USD
B 的净额 = +10 USD     C 的净额 = +10 USD
```

多边净额的前提不是“数学能加”，而是参与者已经签署可执行的净额协议，清算机构有权在违约时将相关义务合并处理。法律可执行性、破产隔离、抵销范围和资产隔离必须作为 `netting_rule_version` 的治理数据，而不是隐藏在代码分支里。

### 26.4.2 算法与证明

统一约定：对参与者而言，应收为正、应付为负。对每个净额组 `g`：

```text
for obligation in g:
    position[obligation.receiver, obligation.asset] += obligation.amount
    position[obligation.payer,    obligation.asset] -= obligation.amount

for member, asset:
    net = position[member, asset]
    if net > 0: create RECEIVE net
    if net < 0: create PAY -net
```

验证：

```text
Σ absolute(gross obligations) >= Σ absolute(net results)
Σ net(member, asset) == 0                         # 对每个 asset 独立
```

净额结果必须保留 `input_obligation_count`、`input_hash`、`algorithm_version`、`generated_at`、`calendar_version` 和明细引用。生成后不允许“就地修正”结果；发现漏单时，冻结该批次、生成新版本净额或追加调整义务，并保留旧结果和差异。

### 26.4.3 现金与证券不能混轧

一笔股票成交至少有两个资产维度：证券数量和结算现金。假设 A 买入 1,000 股、应付 CNY 50,000：

```text
证券维度：A 收 1,000 股，B 付 1,000 股
现金维度：A 付 50,000 CNY fen，B 收 50,000 CNY fen
```

即使系统显示一个“估值 50,000”，也不能把证券与现金合并成单一余额。跨币种净额也不允许直接相加；若 A 欠 USD、同时收 CNY，必须由 FX 交易或清算机构的多币种结算协议分别完成。

### 26.4.4 净额生成状态机

```text
COLLECTING
  ├─ cutoff 到达 → FROZEN
  ├─ 迟到合法补单 → 新 group/version 或 ADJUSTMENT
  └─ 撤单/冲正 → 追加 obligation，不删除原单

FROZEN
  ├─ 校验通过 → CALCULATING
  └─ 校验失败 → EXCEPTION

CALCULATING → CALCULATED → APPROVED → RELEASED
                                   │
                                   └─ 生成 settlement_instruction

EXCEPTION → REVIEW / REBUILT
```

`CALCULATED` 不等于可以对外发钱。至少由独立控制点核对：余额、保证金、参与者资格、净额守恒、风险限额、价值日和外部截止时间。高风险市场还应设置 maker-checker：生成者不能同时批准并释放。

### 26.4.5 不能净额的边界

- 不同优先级债权，除非法律和协议允许；
- 已进入不可撤销交收阶段的义务，不与普通未交收订单重新混轧；
- 发生部分交收的原义务，必须用剩余义务表示，不能把已交付量重新放回 gross；
- 已被抵押、冻结、争议或监管隔离的资产；
- 跨日、跨 epoch 的订单，除非显式生成调整批次并重新完成控制点。

---

## 26.5 DVP：券款对付，而不是“希望两条腿最终都成功”

### 26.5.1 DVP 的目标与现实边界

DVP（Delivery versus Payment，券款对付）的经济目标是：证券交付与现金支付存在条件依赖，不能发生“券已不可逆转交付但款没有收到”的裸露。平台内部可以做到同一事务内的冻结、状态和暂记账户原子提交；但银行、券商、托管人、央行系统通常不共享数据库，不能声称一个跨机构数据库事务或真正的全局 exactly-once。

因此协议要区分：

1. **内部原子性**：同一数据库事务内写入指令、两腿预留、posting 和 outbox；
2. **外部条件性**：向外部系统发送带稳定幂等键和条件的指令；
3. **最终可证明性**：通过外部回执、查单、对账文件和异常人工边界确认两腿结果。

### 26.5.2 DVP 状态机

```text
CREATED
  │ 义务校验、净额批准
  ▼
RESERVING
  ├─ 现金足够 + 券足够 → RESERVED
  ├─ 不足 → WAITING_LIQUIDITY / MARGIN_CALL
  └─ 规则冲突 → REJECTED

RESERVED
  │ 外部请求已持久化到 outbox
  ▼
SUBMITTING
  ├─ 两腿均确认接受 → ACCEPTED
  ├─ 某腿拒绝且另一腿未不可逆 → ROLLING_BACK
  └─ 超时/连接断 → UNKNOWN（查单，不换键）

ACCEPTED
  ├─ 两腿完成 → SETTLED
  ├─ 一腿完成、另一腿失败 → PARTIAL / FAIL_MANAGEMENT
  └─ 超过市场窗口 → FAIL_MANAGEMENT

ROLLING_BACK → RELEASED / REVERSED
SETTLED       → POSTED
PARTIAL       → BUY_IN / SELL_OUT / CASH_COMPENSATION / REVIEW
```

状态转移必须带 `instruction_version` 和 fencing token，采用数据库条件更新。外部回调只报告事实，不直接把状态从 `RESERVED` 改成 `SETTLED`；控制器先检查两腿、外部引用、金额/数量、回调签名、版本和重复键。

### 26.5.3 两种执行策略

**策略 A：外部支持原子 DVP 或条件交收**

- 向 CSD/托管人提交证券腿与现金腿的同一业务指令；
- 平台保存外部 `dvp_instruction_id`、原始请求、确认回执和查询结果；
- 只在外部报告最终交收成功后释放内部冻结并记最终 posting。

**策略 B：外部不支持原子 DVP**

- 先在内部冻结现金和证券，转入 `SETTLEMENT_TRANSIT`/暂记账户；
- 选择风险较小、法律上可回退的一腿先发；
- 另一腿只在第一腿仍可撤回或已取得保证金/担保时推进；
- 任一腿不可逆后，另一腿失败必须进入 `FAIL_MANAGEMENT`，执行买入补券、现金赔付、违约罚金或人工处置；
- 不能用“稍后会成功”覆盖暴露，暴露金额、时间、责任主体和限额必须实时监控。

### 26.5.4 DVP 的账本表达

以客户 A 买入证券、现金已冻结为例，内部冻结是状态事实，不是最终所有权转移：

```text
冻结现金：借 A·CNY AVAILABLE             50,000 fen
          贷 A·CNY SETTLEMENT_FROZEN      50,000 fen

券款交收成功：
  借 A·证券 SETTLEMENT_TRANSIT          1,000 股
  贷 卖方/托管证券 TRANSIT               1,000 股

  借 卖方·CNY SETTLEMENT_RECEIVABLE      50,000 fen
  贷 A·CNY SETTLEMENT_FROZEN             50,000 fen
```

具体借贷方向随账户正常余额和托管模型调整，但必须满足：每一资产维度独立平衡，冻结/暂记与最终交付都有可追踪的 `entry_id`。外部券已转出、款未到时，不能把内部 `SETTLEMENT_FROZEN` 假装成最终完成；应暴露 `PARTIAL_DVP` 和待处置金额。

### 26.5.5 DVP 重试规则

```text
external_key = instruction_id + ":" + leg_code + ":" + attempt_epoch
```

`attempt_epoch` 不是每次网络重试都增加，而只在经过查单确认“原外部请求不存在且协议允许重发”时增加。以下情况禁止新键重发：

- 已收到 HTTP 超时但无法确认外部是否接受；
- 平台 commit 成功、进程在 ACK 前崩溃；
- 回调签名失败但外部业务参考号已出现；
- 日切导致 worker lease 过期；
- 对账文件报告该指令已完成但实时 API 超时。

正确动作是查询原键、关联外部流水、等待对账或人工终止，而不是“再送一次也许能成功”。

---

## 26.6 多级分润：一棵快照树、一套尾差规则、一次可逆结算

### 26.6.1 分润输入必须冻结

一次收费或成交产生的分润事实至少绑定：

```text
fee_event_id
base_amount_minor
currency
fee_rule_version
channel/merchant/product
effective_at
beneficiary_tree_snapshot_id
rounding_mode
residual_policy
```

树快照包含每个节点的稳定 `beneficiary_id`、父节点、比例、固定额/封顶、资格状态和优先级。不能在清算执行时实时查询“当前代理树”；否则代理今天更换上级，历史订单会被重新分配。

### 26.6.2 比例和固定额的确定顺序

必须在规则中写死顺序。一个常见且可复算的顺序是：

1. 以最小单位计算毛手续费 `gross_fee`；
2. 先扣固定成本/渠道成本，得到可分润池 `pool`；
3. 从根到叶按快照分配比例或固定额；
4. 每一步采用指定舍入模式，保留未分配余数；
5. 按 `residual_policy` 将最终余数分配给平台、根节点或最大份额节点；
6. 生成所有受益人分录，合计必须等于可分润池；
7. 若规则有封顶/保底，明确是在舍入前还是舍入后执行。

示例：可分润池 `10,001` fen，A 50%、B 30%、C 20%。按最大余数法分配：

```text
理想值：A 5000.5，B 3000.3，C 2000.2
下取整：A 5000，B 3000，C 2000，余数 1
最大余数 A → A 5001，B 3000，C 2000
合计 = 10001
```

不能让 Go 用 `float64`、Java 用 `BigDecimal` 的默认模式各算一遍再比较；应使用整数比例和统一算法版本。分润明细保存 `numerator`、`denominator`、`floor_amount`、`remainder`、`allocated_amount` 和 `rounding_mode`，审计者可复算。

### 26.6.3 多级树的约束

- 树必须无环，父节点只能在同一法律实体和有效规则范围内；
- 比例总和不能超过 100%，固定额和比例叠加不能让池子负数；
- 节点暂停、制裁、账户冻结时，明确转平台暂存、上游代收还是整笔失败；
- 同一 `fee_event_id + beneficiary_id` 只能有一条生效分润事实；
- 退款/拒付按原始快照生成反向分润，不能按当前树追回；
- 若受益人已经出款，追回必须成为新的应收/冲正义务，不能直接把已入账历史改成负数；
- 跨币种佣金要有 FX 价格/价值日/汇率来源，不能拿展示金额直接分配。

### 26.6.4 分润状态机

```text
FEE_OBSERVED → RULE_SNAPSHOTTED → CALCULATED → APPROVED
                                             │
                                             ├─ 正常 → POSTED
                                             ├─ 规则/账户异常 → SUSPENSE
                                             └─ 退款/拒付 → REVERSAL_PENDING → REVERSED
```

分润 posting 与 `fee_event` 幂等占位、Outbox 事件在一个事务内提交。`SUSPENSE` 不是丢弃收入，而是有责任人、重试时间、原因码和最终处置期限的待处理事实。

---

## 26.7 保证金与清算：先识别暴露，再释放可用额

### 26.7.1 保证金的四个余额

至少区分：

| 余额 | 含义 |
|---|---|
| `posted_collateral` | 客户已经交付并确认的抵押品 |
| `haircut_value` | 应用资产折扣、币种/流动性/集中度后的可计值 |
| `reserved_margin` | 已被订单、净额义务或头寸占用的初始/变动保证金 |
| `available_margin` | 允许新增风险或出款的额度 |

可用保证金不能简单等于钱包余额。一个可审计的示意公式是：

```text
collateral_value = Σ quantity(asset) × price_snapshot(asset) × (1 - haircut)
equity_value      = collateral_value + realized_pnl + unrealized_pnl - fees - debts
available_margin  = equity_value - initial_margin_reserved - settlement_reserved
maintenance_headroom = equity_value - maintenance_margin_required
```

不同产品的价格、压力情景、集中度和负余额规则不同，公式必须带 `risk_rule_version` 和 `valuation_snapshot_id`。价格缺失、停牌、指数失真或 epoch 过期时，不能默认为零风险；应进入 `VALUATION_STALE` 或保护模式。

### 26.7.2 初始保证金、变动保证金和结算预留

- **初始保证金 IM**：新建或增加头寸时锁定，防范未来正常波动；
- **维持保证金 MM**：最低风险覆盖线，权益低于它触发追加或减仓；
- **变动保证金 VM**：按盯市损益、价值日和协议在参与者之间结算；
- **结算预留**：已生成 DVP/现金交收义务但尚未完成的现金或证券，不能再次用于新风险。

释放条件必须来自状态机：订单撤销只能释放未成交部分；成交后对应保证金可能转成结算预留或维持保证金，不能把整笔冻结直接清零。

### 26.7.3 追加保证金与强平状态机

```text
HEALTHY
  ├─ available >= 0 → 继续交易
  ├─ equity < IM → MARGIN_CALL
  └─ equity < MM → LIQUIDATION_PENDING

MARGIN_CALL
  ├─ 补足 → HEALTHY
  ├─ 超过 grace period → LIQUIDATION_PENDING
  └─ 价格/数据不可信 → DATA_HALT

LIQUIDATION_PENDING
  ├─ 新增风险禁止
  ├─ 生成稳定 liquidation_id，按优先级分批减仓
  ├─ 每个成交重新计算剩余敞口和保证金
  └─ 足够覆盖 → LIQUIDATING / CLOSED

LIQUIDATING
  ├─ 部分成交 → 继续，不能重复卖同一头寸
  ├─ 流动性不足 → INSOLVENT_REVIEW
  └─ 外部交收未知 → 暂记、查单、禁止释放对应抵押品
```

强平不是一个 `UPDATE position SET quantity=0`：每个减仓成交、费用、损益、保证金释放和结算预留都要有业务 ID、分录和风险快照。强平 worker 需要 fencing token，避免旧任务在新风控决策之后继续卖出。

### 26.7.4 保证金与净额的关系

净额降低的是交收腿数量，不自动降低风险。只有在协议、法律和风险模型允许时，才按净额结果重新计算结算预留。以下情况保留 gross 风险或额外 buffer：

- 交易对手跨法律实体或净额协议未生效；
- 净额批次尚未批准或输入仍可变化；
- 部分交收、争议、回购/借券的不同优先级；
- 价格快照过期、集中度超限或外部账户不可用；
- 日切期间旧 epoch 与新 epoch 的义务尚未完成迁移。

---

## 26.8 衔接下一层系统 {#settlement-next-layer}

本节是本章和后续系统的明确接口，不把“结算成功”含糊地交给一个总状态。

### 26.8.1 与第 21 章时间和序列的衔接

清结算输入必须携带：

```text
occurred_at_utc
accepted_at_utc
trading_date
accounting_date
logical_day_id
epoch
calendar_version
tzdb_version
aggregate_seq / match_seq
```

第 21 章负责 instant、交易日历、T+N、时区和序列；本章负责把这些值固化到 obligation、netting group 和 settlement instruction。下游不得根据 `created_at` 再次猜价值日。

### 26.8.2 与第 22/23 章账本的衔接

本章只产生“应写入哪些业务事实”的 posting command，不直接改余额。账本服务负责：

- 每个币种独立平衡；
- 冻结、暂记、交付、释放和冲正的不可变分录；
- 账户锁顺序、防透支、分桶和 group commit；
- posting 幂等键，例如 `settlement_instruction_id + leg + action + version`；
- commit 成功后才发布 Outbox。

清算服务可以拥有 `obligation` 和 `netting_result` 的状态，但不能把 `account_balance` 当成清算事实源。

### 26.8.3 与第 24 章幂等和外部渠道的衔接

每条腿最少提供：

```text
business_id          = instruction_id
idempotency_key      = member + instruction_id + leg
request_hash         = canonical payload hash
external_reference   = bank/CSD reference
query_url / query API
raw_request / raw_response
```

同步超时、回调丢失和 commit 后崩溃都返回/保存 `UNKNOWN`，由原键查单；禁止把“日切换批次”当成创建新业务身份的理由。

### 26.8.4 与第 27 章多层对账的衔接

本章输出四种对账输入：

1. **明细层**：每个 trade、obligation、fee、margin event；
2. **净额层**：每个 group 的 gross、net、成员位置和输入哈希；
3. **交收层**：每条腿的内部状态、外部引用、时间线和原始回执；
4. **账本层**：posting、entry、line、暂记户和余额快照。

第 27 章可以在这些层之间做差异定位，但不能只拿“最终余额”对比。差异一旦生成，原对象进入 `REVIEW`/`FROZEN` 保护，修复使用冲正、补记或新版本批次，不做手工余额覆盖。

---

## 26.9 PostgreSQL DDL：日切、义务、净额、DVP、分润与保证金

以下为 PostgreSQL 15+ 参考 DDL。示例使用 `BIGINT` 保存最小单位整数；证券数量若可能超过范围，应改用 `NUMERIC(39,0)`，并在应用层使用 `big.Int`/`BigInteger`。DDL 只保护结构和局部约束，跨行的借贷平衡、净额守恒和状态转移仍需事务代码和定期校验。

### 26.9.1 逻辑日与 cutoff

```sql
CREATE TABLE clearing_calendar_version (
  calendar_version     VARCHAR(64) PRIMARY KEY,
  market_code          VARCHAR(32) NOT NULL,
  zone_name            VARCHAR(64) NOT NULL,
  rule_version         VARCHAR(64) NOT NULL,
  status               VARCHAR(16) NOT NULL
    CHECK (status IN ('PROVISIONAL','ACTIVE','RETIRED')),
  source_ref           VARCHAR(256) NOT NULL,
  effective_from       DATE NOT NULL,
  effective_to         DATE,
  published_at         TIMESTAMPTZ NOT NULL,
  CHECK (effective_to IS NULL OR effective_to > effective_from)
);

CREATE TABLE logical_day (
  logical_day_id       VARCHAR(96) PRIMARY KEY,
  market_code          VARCHAR(32) NOT NULL,
  trading_date         DATE NOT NULL,
  accounting_date      DATE NOT NULL,
  calendar_version     VARCHAR(64) NOT NULL REFERENCES clearing_calendar_version(calendar_version),
  epoch                BIGINT NOT NULL CHECK (epoch > 0),
  window_start_utc     TIMESTAMPTZ NOT NULL,
  cutoff_at_utc        TIMESTAMPTZ NOT NULL,
  window_end_utc       TIMESTAMPTZ NOT NULL,
  status               VARCHAR(16) NOT NULL
    CHECK (status IN ('OPEN','FREEZING','DRAINING','SEALED','RECONCILING','CLOSED')),
  fencing_token        UUID NOT NULL,
  opened_at            TIMESTAMPTZ NOT NULL,
  sealed_at            TIMESTAMPTZ,
  UNIQUE (market_code, epoch),
  CHECK (window_start_utc < cutoff_at_utc AND cutoff_at_utc <= window_end_utc)
);

CREATE TABLE cutover_event (
  cutover_id           UUID PRIMARY KEY,
  market_code          VARCHAR(32) NOT NULL,
  old_logical_day_id   VARCHAR(96) NOT NULL REFERENCES logical_day(logical_day_id),
  new_logical_day_id   VARCHAR(96) NOT NULL REFERENCES logical_day(logical_day_id),
  old_epoch            BIGINT NOT NULL,
  new_epoch            BIGINT NOT NULL,
  effective_at_utc     TIMESTAMPTZ NOT NULL,
  reason               VARCHAR(256) NOT NULL,
  calendar_version     VARCHAR(64) NOT NULL,
  operator_ref         VARCHAR(128) NOT NULL,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (market_code, old_epoch, new_epoch)
);

CREATE TABLE settlement_work_item (
  work_item_id         UUID PRIMARY KEY,
  business_key         VARCHAR(192) NOT NULL,
  request_hash         CHAR(64) NOT NULL,
  logical_day_id       VARCHAR(96) NOT NULL REFERENCES logical_day(logical_day_id),
  epoch                BIGINT NOT NULL,
  accepted_at_utc      TIMESTAMPTZ NOT NULL,
  accepted_seq         BIGINT NOT NULL,
  status               VARCHAR(24) NOT NULL
    CHECK (status IN ('ACCEPTED','PROCESSING','CLEARED','PENDING','UNKNOWN','REVIEW','CANCELLED')),
  payload_json         JSONB NOT NULL,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (business_key),
  UNIQUE (logical_day_id, accepted_seq)
);
CREATE INDEX settlement_work_item_epoch_idx
  ON settlement_work_item (epoch, status, accepted_at_utc);
```

`business_key` 是稳定业务身份，不是网络请求 ID。若一个业务键允许不同动作，应把动作放入键或增加明确的唯一作用域，不能靠调用方约定。

### 26.9.2 成交、义务和净额

```sql
CREATE TABLE clearing_trade (
  trade_id             UUID PRIMARY KEY,
  match_seq            VARCHAR(128) NOT NULL UNIQUE,
  market_code          VARCHAR(32) NOT NULL,
  buyer_member_id      VARCHAR(64) NOT NULL,
  seller_member_id     VARCHAR(64) NOT NULL,
  security_code        VARCHAR(32) NOT NULL,
  quantity_min         NUMERIC(39,0) NOT NULL CHECK (quantity_min > 0),
  currency_code        CHAR(3) NOT NULL,
  price_minor          BIGINT NOT NULL CHECK (price_minor >= 0),
  cash_minor           BIGINT NOT NULL CHECK (cash_minor >= 0),
  trading_date         DATE NOT NULL,
  value_date           DATE NOT NULL,
  logical_day_id       VARCHAR(96) NOT NULL REFERENCES logical_day(logical_day_id),
  rule_version         VARCHAR(64) NOT NULL,
  occurred_at_utc      TIMESTAMPTZ NOT NULL,
  accepted_at_utc      TIMESTAMPTZ NOT NULL,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE clearing_obligation (
  obligation_id        UUID PRIMARY KEY,
  trade_id             UUID NOT NULL REFERENCES clearing_trade(trade_id),
  leg_code             VARCHAR(32) NOT NULL,
  payer_member_id      VARCHAR(64) NOT NULL,
  receiver_member_id   VARCHAR(64) NOT NULL,
  asset_code           VARCHAR(32) NOT NULL,
  asset_kind           VARCHAR(16) NOT NULL CHECK (asset_kind IN ('CASH','SECURITY','FEE','COLLATERAL')),
  currency_code        CHAR(3),
  quantity_min         NUMERIC(39,0) NOT NULL CHECK (quantity_min > 0),
  value_date           DATE NOT NULL,
  logical_day_id       VARCHAR(96) NOT NULL REFERENCES logical_day(logical_day_id),
  epoch                BIGINT NOT NULL,
  netting_group_id     UUID,
  status               VARCHAR(24) NOT NULL
    CHECK (status IN ('OPEN','NETTED','SETTLED','PARTIAL','CANCELLED','REVERSED','REVIEW')),
  rule_version         VARCHAR(64) NOT NULL,
  source_hash          CHAR(64) NOT NULL,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (trade_id, leg_code),
  CHECK ((asset_kind = 'CASH') = (currency_code IS NOT NULL))
);

CREATE TABLE netting_group (
  netting_group_id     UUID PRIMARY KEY,
  market_code          VARCHAR(32) NOT NULL,
  legal_entity_id      VARCHAR(64) NOT NULL,
  settlement_system    VARCHAR(64) NOT NULL,
  value_date           DATE NOT NULL,
  asset_code           VARCHAR(32) NOT NULL,
  currency_code        CHAR(3),
  logical_day_id       VARCHAR(96) NOT NULL REFERENCES logical_day(logical_day_id),
  epoch                BIGINT NOT NULL,
  netting_rule_version VARCHAR(64) NOT NULL,
  status               VARCHAR(24) NOT NULL
    CHECK (status IN ('COLLECTING','FROZEN','CALCULATING','CALCULATED','APPROVED','RELEASED','EXCEPTION','CLOSED')),
  input_count          BIGINT NOT NULL DEFAULT 0 CHECK (input_count >= 0),
  input_hash           CHAR(64),
  version_no           INTEGER NOT NULL DEFAULT 1 CHECK (version_no > 0),
  created_at           TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  approved_at          TIMESTAMPTZ,
  UNIQUE (market_code, legal_entity_id, settlement_system, value_date,
          asset_code, logical_day_id, epoch, version_no)
);

ALTER TABLE clearing_obligation
  ADD CONSTRAINT clearing_obligation_group_fk
  FOREIGN KEY (netting_group_id) REFERENCES netting_group(netting_group_id);

CREATE TABLE netting_result (
  netting_result_id    UUID PRIMARY KEY,
  netting_group_id     UUID NOT NULL REFERENCES netting_group(netting_group_id),
  member_id            VARCHAR(64) NOT NULL,
  asset_code           VARCHAR(32) NOT NULL,
  currency_code        CHAR(3),
  signed_quantity_min  NUMERIC(39,0) NOT NULL CHECK (signed_quantity_min <> 0),
  gross_in_min         NUMERIC(39,0) NOT NULL CHECK (gross_in_min >= 0),
  gross_out_min        NUMERIC(39,0) NOT NULL CHECK (gross_out_min >= 0),
  input_count          BIGINT NOT NULL CHECK (input_count >= 0),
  input_hash           CHAR(64) NOT NULL,
  algorithm_version    VARCHAR(64) NOT NULL,
  status               VARCHAR(24) NOT NULL
    CHECK (status IN ('CALCULATED','APPROVED','RELEASED','SETTLED','REVERSED','REVIEW')),
  created_at           TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (netting_group_id, member_id, asset_code, currency_code, status)
);
```

真实项目通常还需要把 `currency_code IS NULL` 的证券约束为具体证券品种，并使用 `NULLS NOT DISTINCT` 或规范化空值，避免 PostgreSQL 默认允许多个 `NULL` 破坏唯一语义。

### 26.9.3 DVP、分润和保证金

```sql
CREATE TABLE settlement_instruction (
  instruction_id      UUID PRIMARY KEY,
  netting_result_id   UUID REFERENCES netting_result(netting_result_id),
  business_key        VARCHAR(192) NOT NULL UNIQUE,
  participant_id      VARCHAR(64) NOT NULL,
  counterparty_id     VARCHAR(64) NOT NULL,
  value_date          DATE NOT NULL,
  status              VARCHAR(32) NOT NULL
    CHECK (status IN ('CREATED','RESERVING','RESERVED','SUBMITTING','ACCEPTED',
                      'PARTIAL','SETTLED','ROLLING_BACK','RELEASED','UNKNOWN',
                      'FAIL_MANAGEMENT','REVIEW','REVERSED')),
  fencing_token       UUID NOT NULL,
  request_hash        CHAR(64) NOT NULL,
  external_dvp_ref    VARCHAR(128),
  last_error_code     VARCHAR(64),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE settlement_leg (
  instruction_id      UUID NOT NULL REFERENCES settlement_instruction(instruction_id),
  leg_code            VARCHAR(16) NOT NULL CHECK (leg_code IN ('SECURITY','CASH','COLLATERAL')),
  asset_code          VARCHAR(32) NOT NULL,
  currency_code       CHAR(3),
  quantity_min        NUMERIC(39,0) NOT NULL CHECK (quantity_min > 0),
  direction           CHAR(1) NOT NULL CHECK (direction IN ('D','C')),
  status              VARCHAR(24) NOT NULL
    CHECK (status IN ('PENDING','RESERVED','SUBMITTED','ACCEPTED','SETTLED','FAILED','UNKNOWN','RELEASED')),
  external_ref        VARCHAR(128),
  external_key        VARCHAR(192) NOT NULL UNIQUE,
  raw_last_response   JSONB,
  PRIMARY KEY (instruction_id, leg_code),
  CHECK ((leg_code = 'CASH') = (currency_code IS NOT NULL))
);

CREATE TABLE revenue_split_snapshot (
  snapshot_id         UUID PRIMARY KEY,
  fee_event_id        UUID NOT NULL UNIQUE,
  currency_code       CHAR(3) NOT NULL,
  base_amount_minor   BIGINT NOT NULL CHECK (base_amount_minor >= 0),
  pool_amount_minor   BIGINT NOT NULL CHECK (pool_amount_minor >= 0),
  rule_version        VARCHAR(64) NOT NULL,
  rounding_mode       VARCHAR(32) NOT NULL,
  residual_policy     VARCHAR(32) NOT NULL,
  tree_json            JSONB NOT NULL,
  input_hash          CHAR(64) NOT NULL,
  status               VARCHAR(24) NOT NULL
    CHECK (status IN ('SNAPSHOTTED','CALCULATED','APPROVED','POSTED','SUSPENSE','REVERSED')),
  created_at           TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE revenue_split_line (
  snapshot_id         UUID NOT NULL REFERENCES revenue_split_snapshot(snapshot_id),
  beneficiary_id      VARCHAR(64) NOT NULL,
  parent_id           VARCHAR(64),
  level_no            INTEGER NOT NULL CHECK (level_no >= 0),
  numerator           BIGINT NOT NULL CHECK (numerator >= 0),
  denominator         BIGINT NOT NULL CHECK (denominator > 0),
  allocated_minor     BIGINT NOT NULL CHECK (allocated_minor >= 0),
  remainder_minor     BIGINT NOT NULL CHECK (remainder_minor >= 0),
  status              VARCHAR(16) NOT NULL CHECK (status IN ('PENDING','POSTED','REVERSED','SUSPENSE')),
  PRIMARY KEY (snapshot_id, beneficiary_id)
);

CREATE TABLE collateral_account (
  collateral_account_id UUID PRIMARY KEY,
  participant_id         VARCHAR(64) NOT NULL,
  asset_code             VARCHAR(32) NOT NULL,
  currency_code          CHAR(3),
  posted_quantity_min    NUMERIC(39,0) NOT NULL DEFAULT 0 CHECK (posted_quantity_min >= 0),
  reserved_quantity_min  NUMERIC(39,0) NOT NULL DEFAULT 0 CHECK (reserved_quantity_min >= 0),
  haircut_bps            INTEGER NOT NULL CHECK (haircut_bps BETWEEN 0 AND 10000),
  valuation_snapshot_id  VARCHAR(128),
  risk_rule_version      VARCHAR(64) NOT NULL,
  status                 VARCHAR(16) NOT NULL CHECK (status IN ('OPEN','FROZEN','REVIEW','CLOSED')),
  UNIQUE (participant_id, asset_code, currency_code)
);

CREATE TABLE margin_call (
  margin_call_id       UUID PRIMARY KEY,
  participant_id       VARCHAR(64) NOT NULL,
  logical_day_id       VARCHAR(96) NOT NULL REFERENCES logical_day(logical_day_id),
  valuation_snapshot_id VARCHAR(128) NOT NULL,
  equity_minor         BIGINT NOT NULL,
  initial_required_min BIGINT NOT NULL CHECK (initial_required_min >= 0),
  maintenance_required_min BIGINT NOT NULL CHECK (maintenance_required_min >= 0),
  amount_due_min       BIGINT NOT NULL CHECK (amount_due_min >= 0),
  status               VARCHAR(24) NOT NULL
    CHECK (status IN ('OPEN','PARTIAL','SATISFIED','EXPIRED','LIQUIDATION','REVIEW')),
  due_at_utc           TIMESTAMPTZ NOT NULL,
  UNIQUE (participant_id, valuation_snapshot_id)
);
```

### 26.9.4 不可变保护和 Outbox

对 `clearing_trade`、已批准的 obligation、已生成的 netting result、已 POSTED 的 posting 和已经记录的外部响应，应使用数据库权限禁止业务账号 `UPDATE/DELETE`，只允许追加冲正或版本记录。示意触发器：

```sql
CREATE OR REPLACE FUNCTION reject_financial_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'immutable financial fact: %, id=%', TG_TABLE_NAME, OLD;
END $$;

-- 实际项目对高价值事实表逐表启用；迁移/归档账号单独审计。
CREATE TRIGGER clearing_trade_no_update
BEFORE UPDATE OR DELETE ON clearing_trade
FOR EACH ROW EXECUTE FUNCTION reject_financial_mutation();
```

Outbox 应与状态变化在同一事务中写入；publisher 使用租约和 fencing，发送成功后记录响应，但不删除原事件。消费方以业务键幂等。`outbox_event` 的 payload 要保存版本、hash 和关联 `logical_day_id/epoch`，避免回放时丢失日切语义。

---

## 26.10 Go 1.24 核心代码：截止归属、整数净额和 fencing

以下代码强调边界，不是完整 HTTP/SQL 框架。生产实现还应加入超时、指标、结构化日志、错误分类和数据库重试策略。

### 26.10.1 cutoff 归属与在途保护

```go
package clearing

import (
	"context"
	"errors"
	"time"
)

var (
	ErrCutoverRace = errors.New("cutover race: re-read logical day")
	ErrClockUnsafe = errors.New("trusted clock is unsafe")
)

type LogicalDay struct {
	ID             string
	Epoch          int64
	Cutoff         time.Time // UTC, half-open right boundary
	WindowEnd      time.Time
	FencingToken   string
	CalendarVer    string
}

type Accepted struct {
	BusinessKey  string
	RequestHash  string
	AcceptedAt   time.Time
	LogicalDayID string
	Epoch        int64
	Token        string
}

type Calendar interface {
	// 返回中心可信时间和当前逻辑日；不能使用客户端时间。
	NowAndDay(ctx context.Context, market string) (time.Time, LogicalDay, error)
}

type WorkStore interface {
	// 必须在一个 DB 事务内：锁/读取逻辑日，校验 epoch，插入唯一 work item。
	InsertAccepted(ctx context.Context, a Accepted, payload []byte) error
	FindByBusinessKey(ctx context.Context, key string) (*Accepted, error)
}

func Accept(ctx context.Context, c Calendar, s WorkStore, market, key, hash string, payload []byte) (*Accepted, error) {
	if old, err := s.FindByBusinessKey(ctx, key); err == nil && old != nil {
		if old.RequestHash != hash {
			return nil, errors.New("idempotency key reused with different request")
		}
		return old, nil
	}

	now, day, err := c.NowAndDay(ctx, market)
	if err != nil {
		return nil, err
	}
	// Calendar service may enter protection mode near cutoff or on clock drift.
	if now.After(day.WindowEnd) {
		return nil, ErrCutoverRace
	}
	a := &Accepted{
		BusinessKey:  key,
		RequestHash:  hash,
		AcceptedAt:   now.UTC(),
		LogicalDayID: day.ID,
		Epoch:        day.Epoch,
		Token:        day.FencingToken,
	}
	if err := s.InsertAccepted(ctx, *a, payload); err != nil {
		// Unique conflict must be converted to a read of the winner.
		if old, readErr := s.FindByBusinessKey(ctx, key); readErr == nil && old != nil {
			if old.RequestHash != hash {
				return nil, errors.New("idempotency key conflict")
			}
			return old, nil
		}
		return nil, err
	}
	return a, nil
}
```

`InsertAccepted` 的 SQL 条件应类似：读取 `logical_day FOR SHARE`，确认状态是 `OPEN` 或允许旧日 drain 的接纳状态，确认 `accepted_at < cutoff`，再写入 work item。仅在内存中取 day 后无条件插入，会在 cutoff 竞争时把旧单误归类。

### 26.10.2 多边净额：定点整数、显式符号

```go
type SignedQty struct {
	Member string
	Asset  string
	Qty    int64 // 收为正，付为负；生产可换 big.Int 防溢出
}

type Obligation struct {
	ID       string
	Payer    string
	Receiver string
	Asset    string
	Qty      int64
}

func Net(obligations []Obligation) (map[string]SignedQty, error) {
	pos := make(map[string]int64)
	for _, o := range obligations {
		if o.Qty <= 0 || o.Payer == o.Receiver || o.Asset == "" {
			return nil, errors.New("invalid obligation")
		}
		keyIn := o.Receiver + "\x00" + o.Asset
		keyOut := o.Payer + "\x00" + o.Asset
		var err error
		pos[keyIn], err = addChecked(pos[keyIn], o.Qty)
		if err != nil { return nil, err }
		pos[keyOut], err = addChecked(pos[keyOut], -o.Qty)
		if err != nil { return nil, err }
	}
	out := make(map[string]SignedQty, len(pos))
	for key, q := range pos {
		if q == 0 { continue }
		member, asset := splitKey(key)
		out[key] = SignedQty{Member: member, Asset: asset, Qty: q}
	}
	return out, nil
}

func addChecked(a, b int64) (int64, error) {
	if (b > 0 && a > (1<<63-1)-b) || (b < 0 && a < (-1<<63)-b) {
		return 0, errors.New("quantity overflow")
	}
	return a + b, nil
}
```

真实代码不要用字符串拼接替代结构化 map key；这里为了展示简洁省略了 `splitKey`。生产版本使用 `struct{Member, Asset string}`，并在数据库中保存输入哈希、版本、原始 obligation ID 集合。

### 26.10.3 DVP worker 的 fencing 和未知结果

```go
type LegState string
const (
	LegPending  LegState = "PENDING"
	LegUnknown  LegState = "UNKNOWN"
	LegSettled  LegState = "SETTLED"
	LegFailed   LegState = "FAILED"
)

type InstructionStore interface {
	Claim(ctx context.Context, id, token string, lease time.Duration) (bool, error)
	// 条件更新必须带 instruction id + fencing token + 当前版本。
	UpdateLeg(ctx context.Context, id, leg, token string, state LegState, externalRef, raw string) error
	LookupExternal(ctx context.Context, id, leg, token string) (string, error)
}

type Gateway interface {
	Submit(ctx context.Context, externalKey, canonicalPayload string) (externalRef string, err error)
}

func SubmitLeg(ctx context.Context, st InstructionStore, gw Gateway,
	id, leg, fence, externalKey, payload string) error {
	ok, err := st.Claim(ctx, id, fence, 15*time.Second)
	if err != nil || !ok { return errors.New("lost settlement lease") }

	ref, err := gw.Submit(ctx, externalKey, payload)
	if err != nil {
		// 网络错误不等于外部拒绝；状态必须是 UNKNOWN，下一步查原 externalKey。
		_ = st.UpdateLeg(ctx, id, leg, fence, LegUnknown, "", err.Error())
		return err
	}
	return st.UpdateLeg(ctx, id, leg, fence, LegSettled, ref, "accepted")
}
```

`Claim` 不能只依赖 Redis TTL；数据库条件更新或下游支持的 fencing token 才是副作用边界。若 `Submit` 返回超时，调用者不能把 `externalKey` 改成随机值再次执行。

---

## 26.11 Java 21 核心代码：分润尾差与 DVP 状态转移

### 26.11.1 使用 `long` 和 `BigInteger` 的边界

```java
import java.math.BigInteger;
import java.util.*;

record Allocation(String beneficiary, BigInteger amount, BigInteger remainder) {}

final class RevenueSplit {
  private RevenueSplit() {}

  static List<Allocation> largestRemainder(
      BigInteger pool, Map<String, BigInteger> numerators, BigInteger denominator,
      String residualBeneficiary) {
    if (pool.signum() < 0 || denominator.signum() <= 0) {
      throw new IllegalArgumentException("invalid pool or denominator");
    }
    BigInteger numeratorSum = numerators.values().stream()
        .reduce(BigInteger.ZERO, BigInteger::add);
    if (!numeratorSum.equals(denominator)) {
      throw new IllegalArgumentException("ratios do not sum to denominator");
    }

    List<Allocation> result = new ArrayList<>();
    BigInteger floorSum = BigInteger.ZERO;
    for (var e : numerators.entrySet()) {
      BigInteger product = pool.multiply(e.getValue());
      BigInteger[] qr = product.divideAndRemainder(denominator);
      result.add(new Allocation(e.getKey(), qr[0], qr[1]));
      floorSum = floorSum.add(qr[0]);
    }
    BigInteger residual = pool.subtract(floorSum);
    // 生产版应按 remainder 降序、beneficiary_id 升序排序，不能依赖 HashMap 顺序。
    result.sort(Comparator.comparing(Allocation::remainder).reversed()
        .thenComparing(Allocation::beneficiary));
    List<Allocation> out = new ArrayList<>();
    for (int i = 0; i < result.size(); i++) {
      Allocation a = result.get(i);
      BigInteger extra = BigInteger.ZERO;
      if (i < residual.intValueExact()) extra = BigInteger.ONE;
      out.add(new Allocation(a.beneficiary(), a.amount().add(extra), a.remainder()));
    }
    BigInteger sum = out.stream().map(Allocation::amount)
        .reduce(BigInteger.ZERO, BigInteger::add);
    if (!sum.equals(pool)) throw new IllegalStateException("split does not balance");
    return out;
  }
}
```

如果业务规定尾差全部归平台，应把 `residualBeneficiary` 作为显式规则执行，而不是悄悄使用最大余数法。上例展示了统一整数算法；生产代码需处理 `residual` 大于受益人数、封顶和固定额，并将规则版本、排序键和每步中间值落库。`BigInteger.intValueExact()` 只适合已验证余数范围的场景。

### 26.11.2 严格状态转移

```java
enum InstructionState {
  CREATED, RESERVING, RESERVED, SUBMITTING, ACCEPTED,
  PARTIAL, SETTLED, ROLLING_BACK, RELEASED, UNKNOWN,
  FAIL_MANAGEMENT, REVIEW, REVERSED
}

final class StateMachine {
  private static final Map<InstructionState, Set<InstructionState>> ALLOWED = Map.of(
      InstructionState.CREATED, Set.of(InstructionState.RESERVING, InstructionState.REVIEW),
      InstructionState.RESERVING, Set.of(InstructionState.RESERVED,
          InstructionState.UNKNOWN, InstructionState.REVIEW),
      InstructionState.RESERVED, Set.of(InstructionState.SUBMITTING,
          InstructionState.ROLLING_BACK, InstructionState.REVIEW),
      InstructionState.SUBMITTING, Set.of(InstructionState.ACCEPTED,
          InstructionState.UNKNOWN, InstructionState.ROLLING_BACK),
      InstructionState.ACCEPTED, Set.of(InstructionState.SETTLED,
          InstructionState.PARTIAL, InstructionState.FAIL_MANAGEMENT),
      InstructionState.PARTIAL, Set.of(InstructionState.SETTLED,
          InstructionState.FAIL_MANAGEMENT, InstructionState.REVIEW),
      InstructionState.ROLLING_BACK, Set.of(InstructionState.RELEASED,
          InstructionState.REVERSED, InstructionState.REVIEW),
      InstructionState.UNKNOWN, Set.of(InstructionState.ACCEPTED,
          InstructionState.SETTLED, InstructionState.FAIL_MANAGEMENT, InstructionState.REVIEW)
  );

  static InstructionState move(InstructionState from, InstructionState to) {
    if (!ALLOWED.getOrDefault(from, Set.of()).contains(to)) {
      throw new IllegalStateException("illegal transition: " + from + " -> " + to);
    }
    return to;
  }
}
```

状态机库只是本地保护；最终写库必须仍然使用 `UPDATE ... WHERE state=:from AND fencing_token=:token`，并检查受影响行数为 1。状态迁移和 Outbox 必须同事务，外部 API 调用放在事务外，通过 outbox worker 驱动。

---

## 26.12 对账：从明细到银行/CSD 的多层证据链

### 26.12.1 四层对账模型

| 层级 | 内部正本 | 外部参照 | 典型差异 |
|---|---|---|---|
| L1 成交/义务 | `trade`、`obligation` | 撮合/清算所成交文件 | 漏成交、重复成交、价格/数量不一致 |
| L2 净额 | `netting_result` | 清算所净额报告 | 输入版本错、成员位置错、跨日 |
| L3 交收 | instruction/leg/outbox | 银行/CSD 回执和流水 | 未发、重复发、未知、部分交收 |
| L4 账本/余额 | entry/line/account projection | 银行余额、托管余额、客户报表 | posting 漏写、投影漂移、暂记长期未清 |

每层都同时做数量、金额、身份和时间维度，不只比较一个总余额。对账键优先级：外部流水号 > 稳定业务参考号 > 合同允许的复合键；模糊匹配只能生成候选，不能自动入账。

### 26.12.2 对账快照和公式

在逻辑日封存或外部文件到达时，生成带 `snapshot_id` 的只读快照：

```text
opening + accepted_obligation - cancelled - settled - reversed
    = closing_outstanding

internal_net_position - external_net_position = 0
internal_posted_cash + bank_in_transit - bank_reported_cash = 0
internal_security_position + custodian_in_transit - CSD_position = 0
```

每个差异记录：

```text
recon_case_id, layer, business_key, internal_value, external_value,
currency/asset, logical_day_id, epoch, external_file_id,
first_seen_at, severity, owner, next_action_at, resolution_entry_id
```

差异自动分类：

- `TIMING`：内部已发，外部文件尚未覆盖；
- `UNKNOWN_EXTERNAL`：有请求但无最终回执；
- `MISSING_INTERNAL`：外部成功但内部没有事实，先隔离并补入原始证据，不直接改余额；
- `DUPLICATE`：同一外部参考关联多个内部事实；
- `AMOUNT_MISMATCH`：金额/费用/汇率/舍入不一致；
- `DATE_MISMATCH`：价值日、交易日或时区归属不一致；
- `UNRESOLVED`：超过 SLA，升级人工/风险委员会。

### 26.12.3 自动修复边界

可以自动做：

- 重放同一 Outbox/查单请求；
- 对同键重复回执做幂等归并；
- 补建缺失的投影或索引；
- 生成待审批的冲正/补记草案；
- 重新计算尚未批准的净额版本。

不应自动做：

- 覆盖已 POSTED 分录或余额；
- 把未知外部结果标成失败并释放资金；
- 用新业务键重发可能已经成功的出款；
- 把两种币种或证券估值差额直接抵消；
- 没有外部证据时把差异归咎于网络延迟。

---

## 26.13 压测与容量：测清算峰值，也测锁和尾差

### 26.13.1 场景矩阵

| 场景 | 流量/数据 | 关键指标 | 必须保持 |
|---|---:|---|---|
| 成交义务写入 | 峰值 TPS × 2，重复 5% | p50/p95/p99、WAL、锁等待 | `trade_id` 不重复，义务守恒 |
| cutoff 风暴 | cutoff 前后 1 秒各 10 万请求 | 旧/新日归属、UNKNOWN 比例 | 半开边界无串批 |
| 多边净额 | 1,000 成员、1,000 万义务 | 计算耗时、内存、版本 hash | 每资产净额总和为 0 |
| DVP 回执乱序 | 回调延迟、重复、丢失 1% | 未知时长、部分交收数 | 不先释放另一腿 |
| 分润树 | 10 层、10 万受益人 | CPU、尾差、快照重放 | 分配和等于 pool |
| 保证金波动 | 价格每秒更新、强平并发 | 风控延迟、重复 liquidation | 同一头寸不重复减仓 |
| 7×24 日切 | 连续 72 小时、多个 epoch | 可用率、drain 延迟 | 新旧 epoch 无越权写 |
| 数据库故障 | 主库 failover、deadlock、commit timeout | UNKNOWN、恢复时间 | 重试不重复经济事实 |

### 26.13.2 压测数据必须可复算

每笔生成数据固定 seed，保存：`trade_id`、成员图、资产、日历版本、费率树、价格快照、cutoff、epoch。不要只报告“吞吐量 50k/s”；必须能用同一输入重建：

```text
obligation_count
gross_by_asset
net_by_member_asset
settlement_instruction_count
fee_pool / split_sum
posted_margin / reserved_margin / available_margin
reconciliation_difference_count
```

### 26.13.3 通过标准示例

具体门槛要依据 SLA 和数据库规格定标，不能盲抄示例。一个可作为初始验收模板的集合是：

- cutoff 临界 10 万请求中，归属判定 100% 可解释，旧/新 epoch 交叉写入为 0；
- 同一 `business_key` 并发 100 次，最多一个 work item、一个 obligation、一个最终 posting；
- 乱序/重复回调 10 倍重放后，DVP 状态和账本结果不漂移；
- 每币种借贷差异为 0，净额每资产守恒为 0，分润 `Σ allocated = pool`；
- 所有超时回到 `UNKNOWN` 或 `REVIEW`，没有自动新键出款；
- 日切期间核心接入成功率和延迟不超过约定 SLO，旧批次在 drain SLA 内封存，超时任务可查询且不丢；
- 主库故障恢复后，对账能定位 commit 前、commit 后 ACK 前、外部已成功三类断点。

---

## 26.14 故障演练：把“理论上能恢复”变成运行手册

### 26.14.1 必演故障

1. **cutoff 前后网络分区**：网关可写、日历服务不可达。预期：临界请求进入保护/UNKNOWN，不由本机时间猜归属；恢复后原键查询。
2. **旧 epoch worker 停顿 2 分钟后恢复**：新 epoch 已上线。预期：旧 worker 的 fencing 更新影响行数为 0，只能重新 claim；不得向新批次写入。
3. **数据库 commit 返回超时**：事务可能已经提交。预期：调用方收到 UNKNOWN，重试命中原键；不得新增 posting。
4. **银行已扣款、平台无响应**：查单 API 也超时。预期：instruction=UNKNOWN，冻结不释放，进入人工/文件对账；不得换 external_key。
5. **托管人先转券，现金腿拒绝**：预期：PARTIAL_DVP，计算暴露、停止同账户新增风险，执行补款/买入/赔付流程。
6. **净额计算中进程崩溃**：预期：group 保持 FROZEN/CALCULATING 可恢复，使用同一 input hash 重算；旧半成品不能释放资金。
7. **分润发布一半宕机**：预期：快照和每个 beneficiary key 幂等，重启只补未完成行，尾差不变化。
8. **价格源冻结/回拨**：预期：risk snapshot 过期进入 DATA_HALT/REVIEW，不能以零价格强平或释放保证金。
9. **外部文件重复、少行、金额错**：预期：文件 hash/序号检测，重复文件不重复入账，差异生成 recon case。
10. **主库 failover + 时区/DST 切换**：预期：logical_day 由日历版本决定，不能因本地 01:55 重复而生成两个同 epoch 批次。

### 26.14.2 演练证据

每次演练保存：故障注入时间、节点/epoch、业务键、数据库 LSN 或事务 ID、外部请求键、状态转换日志、冻结/释放 posting、对账结果、RTO/RPO、人工动作和后续改进。没有证据的“演练成功”不算完成。

### 26.14.3 恢复 Runbook 摘要

```text
发现异常
  → 标记受影响 market/day/asset 为保护模式
  → 停止新外部释放，不删除队列、不改余额
  → 以 business_key / instruction_id / external_ref 查单
  → 对比 DB、Outbox、银行/CSD、账本和对账文件
  → 对 UNKNOWN 分类：已成功/未接受/无法确认
  → 已成功：补齐内部事实或人工审批补记
  → 未接受：仍使用原键重试
  → 无法确认：维持冻结，升级人工，不猜失败
  → 完成局部对账后解除保护，生成复盘报告
```

---

## 26.15 极端边界与反模式清单

### 26.15.1 时间和日切

- DST 回拨产生两个相同本地时间：用带 offset 的 instant 和 `epoch` 区分；
- DST 跳跃产生不存在本地时间：拒绝或按日历明确映射，不能各语言各自修正；
- leap second/时间源回拨：业务时间用 UTC instant，耗时用 monotonic，临界 cutoff 进入保护；
- cutoff 恰好等于时间戳：右边界归下一窗口；
- 服务器未来日期、日历版本缺失、节假日临时变更：不猜交易日，挂起并记录来源；
- 旧日已关闭但迟到撤单：当前日追加冲正/调整，原事实仍属于旧日。

### 26.15.2 金额、数量和算法

- `BIGINT` 溢出：乘法、汇率、分润前做范围检查，必要时使用 `NUMERIC/BigInteger/big.Int`；
- 证券最小单位、面值、零碎股：按市场规则拆分，不把 0.5 股静默四舍五入；
- 负手续费、零价格、零数量、价格越界：业务校验拒绝，并保留原因码；
- 分润比例 99.99% 或 100.01%：按规则版本处理，不能因浮点误差放行；
- 跨币种净额、现金与证券净额：必须分别生成维度和 FX/交收腿；
- 退款超过原始已分润金额：转争议/应收，不允许冲到负的受益人余额后继续出款。

### 26.15.3 并发和消息

- 同一订单同时撤销、成交、强平：用聚合序列和状态条件更新决定顺序，不能以消息到达顺序覆盖事实；
- 同一外部回调 10 次：同一 `external_ref` 只推进一次，其他是幂等读；
- Kafka offset 回退、Outbox 重复、数据库 deadlock 重试：复用业务键，不能新建义务；
- lease 过期但旧进程仍运行：fencing token 拒绝写入；
- 批量事务部分失败：事务全部回滚或按独立幂等请求重试，不能 ACK 一半而没有结果表；
- 归档/分区切换：原始事实和 hash 可查询，分区不改变业务 ID 和对账键。

### 26.15.4 危险反模式

| 反模式 | 为什么危险 | 替代 |
|---|---|---|
| 用订单最终状态直接交收 | 状态覆盖历史，无法处理部分腿/重试 | trade → obligation → instruction |
| 净额后删除明细 | 对账和法律证据丢失 | 明细不可变，净额为派生结果 |
| 用总估值抵消证券和现金 | 单位、法律和交收风险不同 | 每资产维度独立净额 |
| `SELECT balance` 后应用层检查 | 并发透支 | 账本事务锁行、不可变 posting |
| 17:00 停写全站 | 7×24 不可用，仍会有未知外部结果 | epoch + freeze/drain/seal |
| Redis TTL 作为日切事实 | TTL 过期和丢失无法审计 | DB durable work item + fencing |
| 每层独立四舍五入 | 尾差漂移、多发佣金 | 整数算法、快照、统一 residual policy |
| 外部超时换新流水号 | 可能重复出款/转券 | 原键查单、UNKNOWN、对账 |
| 对账不平直接改余额 | 破坏账本和证据 | 差异 case + 冲正/补记 |

---

## 26.16 术语速查

| 术语 | 定义 |
|---|---|
| Clearing / 清算 | 根据成交计算参与者之间的权利义务、数量和价值日；不等于最终转账 |
| Settlement / 交收 | 执行现金、证券、抵押品等义务的实际转移 |
| Netting / 净额轧差 | 在合法、同维度范围内抵销多笔收付，降低交收腿数量 |
| Bilateral netting | 两个参与者之间净额 |
| Multilateral netting | 多个参与者在同一清算边界内计算各自净头寸 |
| Gross obligation | 未轧差的原始交付义务 |
| Net obligation | 轧差后某参与者/资产/价值日的单向义务 |
| DVP | Delivery versus Payment，券款对付；交付一腿依赖支付另一腿 |
| PVP | Payment versus Payment，通常指多币种支付对付 |
| Value date | 约定义务最终生效/交收的价值日，不一定等于自然日 |
| Cutoff | 接受某批次/价值日输入的截止 instant，推荐半开区间 |
| Logical day | 按市场/主体/规则定义的业务窗口，不等同 UTC 自然日 |
| Epoch | 逻辑日或规则版本的单调代数，用于隔离旧 worker 和批次 |
| In-flight order | 已到系统但是否 durable accepted 尚未确定的在途请求 |
| Fencing token | 让旧租约持有者即使恢复也不能继续写副作用的栅栏令牌 |
| IM | Initial Margin，初始保证金 |
| MM | Maintenance Margin，维持保证金 |
| VM | Variation Margin，变动保证金/盯市结算 |
| Haircut | 对抵押品价值的折扣 |
| Settlement transit | 已预留、等待交收的暂记/在途资产或现金 |
| Buy-in / Sell-out | 一腿失败时买入补券/卖出补现金等失败管理措施 |
| Revenue split | 将费用池按快照规则分配给多级受益人 |
| Residual policy | 舍入尾差的确定性归属规则 |
| Unknown | 外部副作用是否发生无法确认，不是业务失败 |
| Reconciliation | 将内部事实、外部回执、账本和余额按层对比并定位差异 |

---

## 26.17 面试答题卡：先说不变量，再说边界

### Q1：净额轧差为什么不等于删除订单？

**答**：订单/成交和 obligation 是不可变经济事实，净额只是按合法维度派生的交收优化结果。保留明细才能重算、对账、处理部分交收和违约。净额结果保存输入 hash、版本和成员位置，发现漏单生成新版本或调整义务，不覆盖旧结果。

### Q2：多边净额的安全前提是什么？

**答**：同一法律和清算协议边界、同一资产/币种、价值日、结算系统和规则版本；参与者位置总和对每个资产必须为零。法律可执行性和破产隔离不是代码能推导的，必须作为治理配置和审批证据。

### Q3：DVP 如何在银行和托管人不共享数据库时实现？

**答**：不能宣称全局原子提交。内部事务原子写入冻结、指令和 outbox，外部调用带稳定幂等键；两腿由状态机协调，外部超时进入 UNKNOWN，查单和回执/文件对账收敛。若一腿已不可逆而另一腿失败，进入 PARTIAL/FAIL_MANAGEMENT，执行补交、赔付或人工处置，而不是假装成功。

### Q4：17:00 cutoff 前到网关、17:00 后入库，属于哪一天？

**答**：网关收到不是 durable accepted。只有写入带 cutoff token 的 work item 才有旧日归属。若 17:00 前已 durable accepted，归旧 epoch，worker 可在日切后排空；只是访问日志或内存队列中的请求不能声称归旧日，结果不明则 UNKNOWN，重新接受按新日处理且复用原幂等键。

### Q5：如何做到 7×24 日切而不停止写入？

**答**：以逻辑日和 epoch 做边界，cutover 事务发布新 epoch；旧日进入 FREEZING/DRAINING，新请求进入新日，旧任务带 fencing token 继续处理已接纳工作。旧日超时任务进入 PENDING/REVIEW，不强杀或猜失败。封存、净额和对账是逻辑批次动作，不是全站停机。

### Q6：多级分润如何解决尾差？

**答**：冻结受益人树和费率版本；用最小单位整数、统一的比例算法和确定性排序，显式定义尾差归平台/根节点/最大余数者。保存每层分子、分母、下取整值、余数和实际分配，使 `sum(allocated)=pool`。退款用原快照反向事实。

### Q7：净额后保证金是否可以按净额下降？

**答**：不自动。净额减少交收腿不等于减少市场风险；只有净额协议、价值日、数据质量、部分交收状态和风险规则都满足时，才按批准的净额结果释放对应预留。保证金使用估值快照、haircut、IM/MM/VM 和 settlement reserved 计算。

### Q8：commit 超时后应该做什么？

**答**：判定为 UNKNOWN，使用同一业务键查幂等表、posting 和 outbox；如果外部有副作用，查原 external key。不能假设事务回滚，也不能新建 ID 做补偿。只有证据确认未接受且协议允许，才能复用原键重试。

### Q9：对账不平时能否直接调余额？

**答**：不能。先按明细、净额、交收、账本四层定位 timing、duplicate、missing、amount/date mismatch。修复通过重放、原键查单、冲正/补记和审批完成；余额是分录投影，不是人工修复入口。

### Q10：fencing token 解决什么问题？

**答**：解决租约过期、GC 停顿、网络分区后旧 worker 仍继续写的脑裂。每次外部副作用和内部条件更新都携带 token；数据库/下游拒绝旧 token。它不能替代外部幂等和查单，只能防止旧执行者越权。

---

## 26.18 自检题：能写出证据，才算掌握

1. 给定三方、两种币、两个价值日的 20 条 obligation，画出哪些能净额、哪些必须分开，并证明每资产总和为零。
2. 设计 `16:59:59.999`、`17:00:00.000`、网关收到但未入库、旧单回执次日到达四种请求的状态和 `logical_day_id/epoch`。
3. 写出一个 DVP 外部现金腿超时、证券腿已成功的完整状态时间线，标出哪一步不能释放冻结、哪一步生成 recon case。
4. 计算 `10,001` fen 在 50/30/20 分润树下的尾差，并说明换成三层树后规则如何保持可复算。
5. 给出一个 IM、MM、haircut、settlement reserved 共同存在的账户，分别算 available margin 和 liquidation trigger。
6. 说明为什么 `gateway_received_at < cutoff` 不能单独决定旧日归属，以及 durable accepted 需要哪些字段。
7. 对同一 `instruction_id` 并发启动两个 worker，一个暂停超过 lease 后恢复，说明 fencing token 如何让旧 worker 的 UPDATE 影响行数为 0。
8. 设计 DDL 约束，防止同一 `trade_id + leg_code` 生成两条 obligation，同时允许冲正和版本化调整。
9. 列出数据库 commit 前崩溃、commit 后 ACK 前崩溃、外部已成功但回执丢失三类故障的不同恢复动作。
10. 设计 72 小时不停机压测：至少包含 cutoff 风暴、回调乱序、重复消息、主库 failover、epoch 切换和对账文件重复。
11. 为什么“所有参与者最终余额相加为零”仍不能证明 DVP 正确？请指出部分交收、价值日和法律隔离的反例。
12. 在 DST 回拨日把两个相同本地时间映射到同一 `logical_day_id` 时，如何使用 instant、offset、calendar version 和 epoch 避免批次覆盖？

参考答案不应只有“状态最终成功”。至少要能给出：稳定 ID、输入 hash、规则版本、时间归属、状态迁移、分录/Outbox、外部引用、异常边界和可重放证据。

---

## 26.19 生产上线自检清单

### 模型与协议

- [ ] trade、obligation、netting result、instruction、leg、posting 分层，身份不复用。
- [ ] 每种资产/币种、价值日、参与者和法律实体的净额边界有配置和审批。
- [ ] DVP 外部能力、查单协议、幂等键和 partial/fail management 流程经过真实演练。
- [ ] 分润树、舍入、尾差、退款和受益人冻结规则版本化并可复算。
- [ ] IM/MM/VM、haircut、估值快照、结算预留和强平优先级有明确公式。

### 时间与日切

- [ ] 逻辑日、epoch、calendar version、tzdb version、cutoff token 全部落库。
- [ ] cutoff 使用半开区间；临界请求、时钟漂移和日历缺失进入保护模式。
- [ ] 旧 epoch drain、fencing、超时 PENDING/UNKNOWN 和新 epoch 接入已压测。
- [ ] 已封账迟到修正不改原日事实，而是当前日关联冲正/调整。

### 数据与代码

- [ ] 金额/数量不经过 float/double；溢出、精度、单位和最小交付量有测试。
- [ ] 账本事实不可变，余额只能由 posting 投影；修复不使用裸 `UPDATE balance`。
- [ ] 同事务写入幂等占位、状态、posting 和 Outbox；commit 后才 ACK。
- [ ] 外部超时不换键；所有回调、文件和查询归并到同一 instruction。
- [ ] 所有状态转移有白名单、版本条件和 fencing token。

### 运营与证据

- [ ] L1–L4 对账有快照、hash、差异分类、责任人、SLA 和冲正/补记流程。
- [ ] 监控覆盖 gross/net、DVP partial、UNKNOWN aging、暂记余额、margin headroom、epoch 越权失败。
- [ ] 已演练数据库 failover、外部扣款无回执、券先款后失败、重复文件、DST 和时钟漂移。
- [ ] 每次人工处置都有 case ID、审批人、原始证据和 resolution entry ID。
- [ ] 通过故障重放和随机消息重排后，经济事实、净额守恒和账本平衡仍成立。

> **结论**：清结算的高质量不是把批处理做得更快，而是让每一次“应收、应付、交付、冻结、释放、分润和日切归属”都有唯一身份、明确时间、可验证状态、可恢复副作用和可重建证据。净额减少的是交收动作，DVP 降低的是券款错配暴露，分润解决的是可解释分配，保证金解决的是风险覆盖；它们最终都必须回到不可变事实、每资产平衡、原键幂等和多层对账这四个根基上。
