# 29. 金融订单管理系统（OMS）与 FIX 协议网关：撤改竞争、部分成交与算法拆单

> **状态**：生产级工程篇｜**所属**：第三部分·金融工程代码基座｜**编号**：29 / 48
>
> **本章定位**：承接 [24 全链路幂等](24-end-to-end-financial-idempotency.md)、[25 状态机与事务 Outbox](25-state-machines-tcc-saga-and-outbox.md)、[26 清结算与不停机日切](26-clearing-settlement-netting-and-cutoff.md)、[27 多层对账](27-multi-tier-reconciliation-and-auto-healing.md)，把“客户下的一笔订单”落实为可恢复的 OMS 聚合、可审计的 FIX 会话、可精确释放的资金/证券冻结，以及有数量守恒证明的 TWAP、冰山和 POV 拆单。本文覆盖 OMS 订单状态机、FIX `NewOrderSingle`/`ExecutionReport`、撤改单与部分成交、回报乱序/补包/脑裂恢复、`ClOrdID` 幂等、PostgreSQL DDL 与内部协议、Go + Java 核心代码、对账、压测、混沌、极端边界、术语、面试答题卡和自检题。
>
> **不可妥协的红线**：订单状态不是成交事实，`ExecutionReport` 的成交量必须以 `(venue, session, ExecID)` 幂等落库；正常订单必须能证明 `CumQty + LeavesQty + CanceledQty = OrderQty`，交易所更正/终态调整必须显式记录；撤单/改单发送成功不等于交易所接受，冻结只能依据最终可核验事实释放；FIX 序号、重传和 `PossDupFlag` 不能靠内存猜测；一个 `ClOrdID` 只能绑定一个不可变命令指纹，冲突必须拒绝；算法子单总量、在途量和已成交量不得超过母单目标；脑裂恢复宁可暂停发单进入 `UNKNOWN`，也不能双主下单。

---

## 29.1 先把事故讲清楚：撤单成功了吗，钱该不该解冻

### 29.1.1 一条真实可发生的时间线

客户要买入 10,000 股 `XYZ`，限价 100.00。OMS 冻结 1,000,000.00 现金后，通过 FIX 发出 `NewOrderSingle`。交易所先成交 3,000 股，回报在网络中延迟；客户同时点击撤单；网关收到撤单确认前发生断线，重连时又发现一条旧 `ExecutionReport`。若系统用“撤单按钮已点击”作为释放条件，或者把最后收到的状态覆盖当前状态，会出现以下任一灾难：

| 时间 | 外部事实 | 错误实现 | 后果 |
|---|---|---|---|
| `10:00:00.000` | 新单被接受，冻结 1,000,000 | 只在 Redis 写冻结 | 数据库提交与冻结事实脱节 |
| `10:00:00.020` | 成交 3,000，回报丢在网络中 | OMS 仍认为 `WORKING` | 可用现金被高估或撤单释放全额 |
| `10:00:00.025` | 客户发 `CancelRequest` | `PENDING_CANCEL` 直接当 `CANCELED` | 交易所可能继续成交，资金被重复使用 |
| `10:00:00.040` | 交易所回 `CancelReject` | 旧回报覆盖当前状态 | 订单回到 `WORKING` 但冻结已释放 |
| `10:00:00.050` | 重传原成交回报，`PossDupFlag=Y` | 没有 `ExecID` 唯一键 | 3,000 股被入账两次 |
| `10:00:00.100` | 网关旧主恢复并继续发单 | 共享账号没有 fencing token | 交易所收到两条改单/撤单或两条新单 |

根因不是“某个字段写错”，而是把不同层级混在一起：

1. **命令意图**：客户想下单、撤单、改单，不能证明交易所已执行。
2. **会话事实**：FIX 消息是否发送、是否收到、是否存在序号缺口，不能证明订单已接受。
3. **市场事实**：交易所 `ExecutionReport` 说接受、成交、拒绝或撤销；这才是外部执行事实。
4. **经济事实**：成交产生的持仓、应收应付和费用；冻结是可逆的风险预留，不是成交。
5. **视图状态**：OMS 当前展示的 `PARTIALLY_FILLED`、`PENDING_CANCEL` 等，是对事实的投影，必须可重建。

### 29.1.2 唯一真问题

> 在消息重复、乱序、迟到、FIX 补包、连接重连、主备脑裂、撤改单竞争、部分成交和算法重启的情况下，系统能否证明：没有重复成交、没有丢失成交、没有超量发单、冻结金额不会凭空释放，且最终能通过对账把订单、交易所、持仓和现金收敛到可解释状态？

网络无法承诺“请求只执行一次”。生产系统应承诺：

- 每个命令有不可变身份和请求指纹，重复命令返回第一次结果，指纹冲突拒绝；
- 每个外部回报按交易所作用域的 `ExecID` 去重，并保存原始 FIX 文本；
- 观察不到结果时使用 `UNKNOWN`，通过查询、Drop Copy、日终报告或人工边界收敛；
- 订单状态、成交事实、资金冻结和 Outbox 同一事务提交，异步副作用可重试；
- 恢复、切主、补包都以持久化序号和 fencing token 为依据，不以进程内变量为依据。

### 29.1.3 关键不变量总表

| 类别 | 不变量 | 失败时动作 |
|---|---|---|
| 数量 | `0 ≤ CumQty ≤ OrderQty`；正常订单 `LeavesQty = OrderQty - CumQty - CanceledQty` | 拒绝回报、进入人工/交易所更正队列，不静默截断 |
| 子单 | `filled + working + canceled + expired ≤ parent_qty`；任何新单前预留量不超过剩余量 | 算法熔断，禁止继续发单 |
| 身份 | `(account, venue, ClOrdID)` 唯一；同一 `ClOrdID` 指纹必须相同 | 相同返回原结果，冲突返回 `IDEMPOTENCY_CONFLICT` |
| 成交 | `(venue, session_id, ExecID)` 唯一；同一成交的数量、价格、方向不可变 | 哈希冲突报警，冻结继续保守持有 |
| 资金 | 买单冻结覆盖未成交买入义务与费用上限；卖单冻结覆盖未成交证券量 | 先补足冻结，再允许继续发单 |
| 状态 | 状态转移只能走白名单和版本条件；终态不能被普通重试覆盖 | 追加冲突事件，不直接 `UPDATE status` |
| FIX | 入站序号连续或有显式 `ResendRequest`；出站只从单一活跃主发送 | 停发、补包、重新选主 |
| 审计 | 原始 FIX、规范化事件、状态转移、资金动作和操作者可关联 | 证据缺失的订单不得自动终结 |

---

## 29.2 OMS 领域模型：订单、命令、回报、成交和冻结必须分开

### 29.2.1 五个对象

| 对象 | 例子 | 是否可重放 | 是否直接代表成交 |
|---|---|---:|---:|
| `OrderCommand` | `NewOrder`, `CancelOrder`, `ReplaceOrder` | 是 | 否，表达意图 |
| `VenueMessage` | `NewOrderSingle`, `ExecutionReport` | 是，原文只追加 | 可能包含外部事实 |
| `OrderSnapshot` | `WORKING`, `PENDING_CANCEL`, `FILLED` | 是，可重建 | 否，是投影 |
| `Execution` | `ExecID=E1`, `LastQty=3000` | 是，但只记一次经济影响 | 是 |
| `Reservation` | 买入冻结现金、卖出冻结证券 | 是，按 reservation event 重建 | 否，是风险预留 |

不要把 `orders.status` 当成事实账本。一个可重建的链路是：

```text
API command
  → command_fingerprint / ClOrdID 绑定
  → OMS order event + reservation event + outbox（同一事务）
  → FIX gateway 发送（至少一次）
  → venue ExecutionReport 原文落库
  → ExecID 去重 + 成交事实 + 状态投影 + 冻结调整（同一事务）
  → 持仓/现金账本与对账
```

### 29.2.2 推荐的 OMS 状态

状态表达“下一步允许做什么”，不是把所有历史压成一个字符串。`venue_status` 和 `oms_status` 可以分别保存；以下是 OMS 聚合的最小集合：

```text
RECEIVED
  └─ ACCEPT ───────────────► VALIDATING
                               ├─ RISK_OK ─────► PENDING_NEW
                               └─ REJECT ──────► REJECTED

PENDING_NEW
  ├─ VENUE_NEW ────────────► WORKING
  ├─ VENUE_FILL ───────────► PARTIALLY_FILLED / FILLED
  ├─ VENUE_REJECT ─────────► REJECTED
  └─ DEADLINE/UNKNOWN ─────► UNKNOWN

WORKING
  ├─ FILL(q < leaves) ─────► PARTIALLY_FILLED
  ├─ FILL(q = leaves) ─────► FILLED
  ├─ CANCEL_CMD ───────────► PENDING_CANCEL
  ├─ REPLACE_CMD ──────────► PENDING_REPLACE
  ├─ EXPIRE ───────────────► EXPIRED
  └─ SESSION_LOST ─────────► UNKNOWN（仍保守冻结）

PARTIALLY_FILLED
  ├─ FILL ─────────────────► PARTIALLY_FILLED / FILLED
  ├─ CANCEL_CMD ───────────► PENDING_CANCEL
  └─ REPLACE_CMD ──────────► PENDING_REPLACE

PENDING_CANCEL
  ├─ CANCEL_ACK ───────────► CANCELED（若 leaves=0 则按事实收敛）
  ├─ CANCEL_REJECT ────────► WORKING / PARTIALLY_FILLED
  └─ LATE_FILL ────────────► PARTIALLY_FILLED / FILLED，继续等撤单结果

PENDING_REPLACE
  ├─ REPLACE_ACK ──────────► WORKING / PARTIALLY_FILLED
  ├─ REPLACE_REJECT ───────► WORKING / PARTIALLY_FILLED
  └─ FILL ─────────────────► 先记成交，再按新旧 leaves 计算

UNKNOWN
  ├─ QUERY_WORKING ────────► WORKING / PARTIALLY_FILLED
  ├─ QUERY_CANCELED ───────► CANCELED
  ├─ QUERY_FILLED ──────────► FILLED
  └─ MANUAL_TERMINATE ─────► TERMINATED（不代表外部没有副作用）
```

`CANCELED`、`REJECTED`、`EXPIRED`、`FILLED` 是普通流程终态，但“终态”不代表不再接收外部事实。迟到成交、交易所更正、Bust/Correct、Drop Copy 补事实必须追加事件；它们不能通过普通重试把订单快照偷偷覆盖。必要时引入 `CORRECTED` 或独立的 `execution_adjustment`。

### 29.2.3 成交回报的状态归并规则

对每个唯一 `Execution`，先做事实去重，再在锁定的订单版本上更新：

```text
new_cum = old_cum + last_qty       （Trade/Fill）
new_leaves = max(order_qty - new_cum - canceled_qty, 0)

if new_cum == order_qty: status = FILLED
else if new_cum > 0:     status = PARTIALLY_FILLED
else if venue_status is NEW: status = WORKING
```

但 `ExecutionReport` 里的 `14 CumQty` 是交易所报告的累计视图，不一定可以直接加。生产实现同时保存：

1. `LastQty`：该条回报宣称的增量；
2. `CumQty`：交易所宣称的累计量；
3. `oms_cum_qty`：OMS 已接受的唯一成交事实之和；
4. `cum_qty_delta`：校验 `reported_cum - previous_reported_cum` 与本次新成交的关系。

若交易所是累计回报且没有稳定 `ExecID`，需要用“报告序列 + `(order_id, exec_type, cum_qty, last_px, transact_time)` 指纹”做谨慎去重，不能仅按累计量去重，因为同累计量可能对应价格更正。出现 `reported_cum < oms_cum_qty`、累计跳增超过订单量或同 `ExecID` 内容变化时，进入 `EXECUTION_INCONSISTENT`，冻结保守持有并人工核验。

### 29.2.4 状态机必须是显式白名单

```text
allow(current, event, facts, version) = true
且 UPDATE orders
    SET version = version + 1, status = next_status
  WHERE order_id = :id
    AND version = :expected_version
    AND status = :current
影响行数 = 1
```

不可接受的写法：

```sql
-- 可能覆盖 PENDING_CANCEL、晚到成交或人工挂起状态。
UPDATE oms_order SET status = :status WHERE order_id = :id;
```

正确做法是同一事务内锁订单、插入回报/成交幂等事实、校验数量和金额、追加状态事件、调整冻结、写 Outbox。应用层先 `SELECT`、随后在另一个连接 `UPDATE` 会在并发撤单和成交下产生竞态。

---

## 29.3 FIX：NewOrderSingle、ExecutionReport 与会话序号

### 29.3.1 `NewOrderSingle (35=D)` 的最小协议面

FIX 不是“把 JSON 换成 `|` 分隔字符串”。字段的必填性、枚举、长度、条件必填和交易所扩展必须以对手方的 Data Dictionary 为准。以下是常见 FIX 4.4 基础字段：

| Tag | 名称 | 示例 | OMS 约束 |
|---:|---|---|---|
| `35` | `MsgType` | `D` | 必须是 `D` |
| `11` | `ClOrdID` | `OMS-20261125-000001` | 作用域内唯一，命令身份不可复用 |
| `1` | `Account` | `A1001` | 与风险、持仓和清算账户绑定 |
| `55` | `Symbol` | `XYZ` | 不能只依赖展示代码，保存 `SecurityID/Source` |
| `48/22` | `SecurityID/Source` | `12345/8` | 优先使用交易所可识别的证券身份 |
| `54` | `Side` | `1` Buy / `2` Sell | 方向不允许改单时改变，除非交易所支持特殊流程 |
| `38` | `OrderQty` | `10000` | 量化到最小交易单位 |
| `40` | `OrdType` | `2` Limit / `1` Market | Market 必须有风险价格/金额上限 |
| `44` | `Price` | `100.00` | Limit 必填；用 tick 校验 |
| `59` | `TimeInForce` | `0` Day / `3` IOC / `4` FOK | 与交易所支持矩阵校验 |
| `21` | `HandlInst` | `1` | 路由策略，不等于风控通过 |
| `100` | `ExDestination` | `XNAS` | 交易场所/路由目的地 |
| `60` | `TransactTime` | UTC 纳秒/毫秒 | 由网关生成或校验时钟偏差 |
| `15` | `Currency` | `USD` | 与证券、账户和结算币种一致 |
| `18` | `ExecInst` | `M` | 仅使用对手方支持的枚举 |
| `10` | `CheckSum` | `123` | FIX 会话层计算，不能由业务层手填 |

`44=0` 不能简单等价于市价；`OrdType=1` 的市价单需要内部风险保护价格、最大名义金额和价格保护规则，但不一定把保护价直接放进 FIX `44`。对交易所不支持的字段，网关必须在能力矩阵中显式拒绝，而不是静默丢弃。

### 29.3.2 `ExecutionReport (35=8)` 的核心字段

| Tag | 名称 | 含义 |
|---:|---|---|
| `37` | `OrderID` | 交易所/经纪商订单身份，收到后持久化 |
| `11` | `ClOrdID` | 当前客户端订单身份 |
| `41` | `OrigClOrdID` | 撤改单时引用的旧 `ClOrdID` |
| `17` | `ExecID` | 本次执行事实身份，必须幂等 |
| `150` | `ExecType` | `0` New、`4` Canceled、`5` Replaced、`8` Rejected、`F` Trade、`C` Expired 等 |
| `39` | `OrdStatus` | `0` New、`1` Partially Filled、`2` Filled、`4` Canceled、`8` Rejected 等 |
| `32` | `LastQty` | 本条成交增量 |
| `31` | `LastPx` | 本条成交价格 |
| `14` | `CumQty` | 交易所视角累计成交量 |
| `151` | `LeavesQty` | 交易所视角剩余量 |
| `6` | `AvgPx` | 交易所视角累计均价 |
| `60` | `TransactTime` | 外部事实时间 |
| `58` | `Text` | 可解释文本，不能作为机器唯一条件 |
| `103` | `OrdRejReason` | 拒单原因 |
| `377` | `ExecRestatementReason` | 重述/更正原因（若对手方支持） |

处理原则：

1. `ExecType=F` 才按成交增量创建经济事实；`OrdStatus=2` 只是状态摘要，不应再创造成交。
2. `LastQty=0` 的 New/Cancel/Replace 回报也要记录，可能改变冻结或 ClOrdID 链。
3. `ExecID` 在交易所作用域内唯一，不假设跨会话全局唯一；推荐唯一键 `(venue_id, session_id, exec_id)`，若交易所文档承诺跨会话稳定，可另加 canonical key。
4. `OrdStatus` 不可盲目覆盖 OMS 当前状态；以已去重的成交事实、撤改单确认和订单版本归并。
5. `PossDupFlag=Y` 的重传不是“可以忽略”：必须验证原始消息哈希、序号和 `ExecID`，然后幂等处理。

### 29.3.3 会话层：序号、补包和 Gap Fill

FIX 会话头至少关注：

| Tag | 含义 | 生产要求 |
|---:|---|---|
| `8` | BeginString | 固定版本，如 `FIX.4.4` |
| `9` | BodyLength | 按 FIX 编码规则计算 |
| `34` | MsgSeqNum | 每个方向单调递增，持久化而非只放内存 |
| `35` | MsgType | `A/D/8/2/4/1/5/0` 等 |
| `43` | PossDupFlag | 重传消息通常为 `Y`，不能据此删除 |
| `97` | PossResend | 对手方标识重发，按协议处理 |
| `52` | SendingTime | 监控时钟偏差与 replay 窗口 |
| `122` | OrigSendingTime | `PossDupFlag=Y` 时核验原发送时间 |
| `10` | CheckSum | 会话层校验 |

收到 `34=N` 而本地期待 `N-1` 时，不能跳过处理：

```text
收到高序号 N
  → 暂存原始消息（按 session + seq 唯一）
  → 发送 ResendRequest(7=N_expected, 16=N-1)
  → 收到低序号消息按顺序处理
  → 收到 SequenceReset-GapFill(35=4, 123=Y, 36=next_seq)
    仅表示区间无业务消息，不是丢弃任意业务回报的许可证
  → 连续处理到 N
```

边界与防护：

- `BeginSeqNo=0`、`EndSeqNo=0` 的无限补包请求必须有节流和最大范围，不能让对手方/故障代码触发消息风暴。
- 不能对历史业务消息再次发送副作用；入站 `session_seq` 去重和业务 `ExecID` 去重必须同时存在。
- `SequenceReset` 的 `NewSeqNo` 只能按会话协议处理；不允许应用层把它当作“清空数据库序号”。
- `Logout`、`TestRequest`、`Heartbeat` 也占序号（具体以实现/协议为准），不能只存业务消息。
- 日切、ResetSeqNumFlag 和双方的 reset 时间窗必须由配置版本控制；重置序号前要保存旧会话证据，并完成对账/人工确认。

---

## 29.4 撤单、改单与部分成交：两个并发状态机和一套冻结公式

### 29.4.1 撤改不是覆盖原订单

一个交易所工作订单可能经历：

```text
ClOrdID C1: NewOrderSingle(q=10,000, p=100)
  ├─ ExecutionReport(New, OrderID=V1)
  ├─ ExecutionReport(Trade, ExecID=E1, LastQty=3,000, LastPx=99.9)
  └─ ReplaceRequest(OrigClOrdID=C1, ClOrdID=C2, q=8,000, p=99.5)
       ├─ ExecutionReport(Trade, ExecID=E2, LastQty=1,000, LastPx=99.5)
       └─ ExecutionReport(Replace, ClOrdID=C2, OrigClOrdID=C1)
```

建议采用以下语义：

- **聚合订单 `order_id`**：客户视角的一笔订单，贯穿撤改单链。
- **命令版本 `order_version`**：每个 New/Cancel/Replace 一个不可变版本。
- **客户端 ID**：每个发送给交易所的命令有独立 `ClOrdID`；改单使用新 ID，`OrigClOrdID` 引用当前活动 ID。
- **交易所订单 ID `OrderID`**：收到后保存，不能把它当成客户端幂等键。
- **有效数量**：改单后的 `OrderQty` 是累计总数量还是剩余数量，必须按对手方协议转换并原样保存 `orig_qty_semantics`。

最危险的反模式是“收到客户撤单就把 `status=CANCELED`，然后释放全部冻结”。正确流程是：

```text
CancelCommand
  → 锁定 order + 校验当前 active_cl_ord_id
  → 写 PENDING_CANCEL 版本与 outbox
  → 发送 OrderCancelRequest(35=F)
  → 等 ExecutionReport(ExecType=4) 或 OrderCancelReject(35=9)
  → 依据已去重成交事实和最终 leaves 调整冻结
```

如果交易所支持“撤单后成交”的竞态，`PENDING_CANCEL` 期间必须继续接收成交；`CancelReject` 到达后回到 `WORKING/PARTIALLY_FILLED`，而不是恢复旧的冻结快照。

### 29.4.2 部分成交与平均价

对买入订单，成交的累计金额和均价为：

```text
cum_notional_minor = Σ(last_qty_i × last_px_minor_i)
avg_px = cum_notional / cum_qty
```

必须明确价格的 scale、乘积的溢出边界和舍入规则。订单展示均价可以按 tick 舍入，但账本和对账保存未舍入的定点分子/分母或足够精度的 `numeric`。不要用浮点，也不要用 `BigDecimal.equals` 判断数值相等（`1.0` 与 `1.00` scale 不同）。

部分成交后：

- `cum_qty` 只因新的唯一 `Trade` 增加；重复 `ExecID` 不增加；
- `leaves_qty` 由订单总量和已确认取消/失效数量推导，交易所回报的 `151` 用于校验；
- 费用按成交事实计算，不能在撤单时把预估费用当已发生费用；
- 买单释放未成交量对应的预留本金和未发生的费用 buffer；卖单释放未成交证券数量；
- 成交部分转为持仓/应收应付，冻结不能再覆盖已成交部分；
- 回报乱序时先持久化事实，按订单锁顺序归并，不按到达时间覆盖金额。

### 29.4.3 冻结公式：买单、卖单、改单

设：

- `L` = 剩余未成交数量；
- `P` = 当前有效保护价/限价（最小价格单位）；
- `F(L,P)` = 交易所/经纪商费用上限与价格保护 buffer；
- `R_cash = L × P + F(L,P)`；
- 卖出证券冻结 `R_asset = L × contract_multiplier`。

**买单**：新成交 `q` 后，先以成交事实减少 `L`，再计算目标冻结：

```text
target_cash_reservation = max(0, (order_qty - cum_qty - canceled_qty) * price_cap
                                + fee_buffer_for_remaining)
release_or_topup = old_cash_reservation - target_cash_reservation
```

市价单没有确定 `P`，必须在风控层生成 `price_cap` 或最大名义金额；没有保护上限的市价单不应进入生产路由。若改单把价格从 100 提到 105，先检查增量冻结是否可用，再提交 Replace；不能先发交易所改单、收到成功后才发现账户无钱。

**卖单**：

```text
target_asset_reservation = max(0, order_qty - cum_qty - canceled_qty)
```

证券借贷、融券费、交收失败 buffer 应作为独立 reservation bucket，不要把可用证券直接减成一个不可解释数字。

**改单数量**：在已有 `cum_qty` 的情况下，新总量 `new_order_qty` 必须满足：

```text
new_order_qty >= cum_qty
new_leaves = new_order_qty - cum_qty - canceled_qty
```

若交易所把改单数量定义为剩余量，则内部先转换为 canonical total quantity，并保存原始语义。禁止把 `new_order_qty=500` 当成总量覆盖掉已经成交的 700；这会造成负 leaves 或“凭空撤销成交”。

### 29.4.4 资金动作要有 reservation ledger

冻结不是余额列上的一个魔法数字，推荐追加不可变 reservation event：

```text
RESERVE_CREATED   +1,000,000 USD
FILL_CONSUMED       -300,000 USD（成交本金转经济事实，具体账务由账本定义）
RESERVE_REDUCED     -700,000 USD（剩余义务减少）
FEE_BUFFER_RELEASED  -x USD
```

同一 `(order_id, reservation_version, reason)` 幂等；`available + reserved + posted` 的余额恒等式由账户锁和分录保证。成交、释放、补足和撤销回滚必须与订单事实同事务提交，不能先改 Redis 再异步补数据库。

---

## 29.5 乱序、重复、迟到、补包和脑裂恢复

### 29.5.1 三种顺序要分开

1. **FIX 会话顺序**：`MsgSeqNum(34)`，保证消息流可恢复，不保证市场事实业务顺序。
2. **交易所执行顺序**：`ExecID`、交易所 `TransactTime`、交易所订单事件序号（如果有），作用域取决于对手方。
3. **OMS 提交顺序**：数据库事务提交的 `order_event_seq`，是本地可重放顺序，不应冒充交易所发生时间。

一个晚到的成交可能在本地先于撤单确认提交，但这不等于成交发生在撤单之后。保存 `venue_event_time`、`received_at`、`committed_at` 三个时间，并用明确的业务规则解释。

### 29.5.2 回报处理的正确顺序

```text
读取原始 FIX
  → 校验 CheckSum / Data Dictionary / 会话身份
  → 按 session + seq 原样持久化（重复也留审计引用）
  → 序号连续后放入业务处理队列
  → 解析 canonical ExecutionReport
  → ExecID 唯一键去重、哈希校验
  → 锁定 order_id，校验 ClOrdID/OrderID/数量/价格/方向
  → 写 execution + reservation event + order event + outbox（一个事务）
  → 提交后 ACK/推进消费水位
```

如果数据库事务提交成功而进程在 ACK 前崩溃，重投只会命中唯一键；如果原始回报落库但业务事务失败，重放 worker 从原始表继续；如果序号有缺口，业务层只能看到已连续前缀，缺失区间补齐后再推进。

### 29.5.3 `ExecID` 去重与内容冲突

```text
key = H(venue_id | session_scope | exec_id)
if key 不存在:
    insert execution(raw_hash, fields...)
    apply economic effect
else if raw_hash 相同:
    return previous outcome        // 安全幂等
else:
    mark EXEC_ID_CONTENT_CONFLICT
    freeze order / halt route
    require venue query + operator review
```

不能因为 `ExecID` 重复就无条件返回成功：重复 ID 不同内容可能是交易所 bug、会话范围误判、数据污染或攻击。也不能只依赖 `ClOrdID` 去重，改单链上的多个回报可能有相同 `ClOrdID` 但不同 `ExecType`。

### 29.5.4 脑裂恢复：fencing 比“选一个看起来新的主”更重要

网关 A 与 B 同时认为自己是主，会向同一 FIX 会话/同一交易账户发送新单。心跳正常不等于没有脑裂；网络分区时两个实例都可能访问数据库的旧租约。

生产方案：

1. 使用 PostgreSQL advisory lock、租约表或共识系统取得 `gateway_lease`；
2. 每次租约取得产生单调 `fencing_token`，发送前和 outbox claim 时都校验 token；
3. 对手方会话只允许一个连接，旧连接被 Logout/断开；不能仅依赖应用层“我以为自己是主”；
4. 旧主失去 lease 后停止业务发送，即使其 TCP 连接仍然存在；
5. 新主先读取 `last_sent_seq/last_recv_seq`、未确认 outbox、订单 UNKNOWN 集合和 venue query 结果，再恢复发送；
6. 不确定某个 NewOrder 是否已到达时，禁止盲目重发新 `ClOrdID`。先用原 `ClOrdID` 查询、Drop Copy、Execution Report 或人工核验；
7. fencing 只能阻止平台双主，不能消除交易所已接受的历史副作用，故必须设计查询与对账。

### 29.5.5 恢复状态机

```text
SESSION_CONNECTED
  → LOGON_VERIFIED（CompID、心跳、序号策略匹配）
  → SEQUENCE_RECONCILING（补包/GapFill）
  → DROP_COPY_RECONCILING（如有）
  → ORDER_QUERYING（工作订单、未决撤改、成交）
  → READY_TO_SEND

任一阶段异常
  → READ_ONLY / UNKNOWN
  → 只允许查询、补证据和人工确认
  → 不允许新单、撤改或算法继续发单
```

---

## 29.6 `ClOrdID` 幂等：稳定身份、指纹和替换链

### 29.6.1 身份分层

| 身份 | 作用域 | 能否复用 | 说明 |
|---|---|---:|---|
| `order_id` | 平台全局 | 否 | 客户聚合订单身份 |
| `command_id` | 平台全局 | 否 | 一次 New/Cancel/Replace 命令 |
| `ClOrdID` | 账户 + venue + 会话/交易日（按对手方） | 否 | 发送给对手方的客户端订单 ID |
| `OrigClOrdID` | 同一替换链 | 只引用 | 指向当前/上一客户端 ID |
| `OrderID` | venue 账户/会话 | 由 venue 分配 | 不作为平台幂等唯一键 |
| `ExecID` | venue 规定的作用域 | 否 | 一次执行事实 |
| `client_request_id` | API 租户/账户 | 否 | 外部重试入口身份 |

`ClOrdID` 不应由随机 UUID 直接无界增长：要满足对手方长度、字符集、跨日唯一和可排查性。示例：`A1001-261125-00000042-R03`。真正的唯一性由数据库唯一约束保证，字符串格式只是运维辅助。

### 29.6.2 指纹规则

对 `NewOrder` 计算 canonical fingerprint：

```text
H(account | venue | symbol_id | side | qty | ord_type | price_scale_value |
  tif | currency | route | algo_parent_id | risk_version)
```

字段先规范化：金额使用最小单位整数或 canonical decimal，JSON key 排序，枚举大写，忽略不影响语义的 trace 字段。对相同 `ClOrdID`：

- 指纹相同：返回原命令状态，不再次发 FIX；
- 指纹不同：返回 `IDEMPOTENCY_CONFLICT`，写安全事件，不能“以最新请求为准”；
- 状态未知：返回 `ACCEPTED/UNKNOWN` 并进入查询，不因 API 超时重新造一个 ID。

撤单/改单同理。`CancelRequest` 的幂等键应该绑定原 `order_id + target_order_version + command_id`；若当前版本已撤成功，重试返回成功；若当前版本已被替换，引用过期 `ClOrdID` 必须返回明确冲突并引导客户端使用当前活动版本。

### 29.6.3 Outbox 与发送确认的边界

```text
数据库事务：订单命令 + order_version + outbox(ClOrdID, payload_hash)
             ↓ commit
FIX sender claim outbox（带 fencing token）
             ↓ persist sent_at / session_seq
网络发送
             ↓
ExecutionReport / reject / query
```

发送 outbox 成功不等于交易所接受；`sent` 只是会话事实，最终业务状态由回报或查询决定。发送线程超时后不能自动换新 `ClOrdID`，否则一次未知请求可能变成两笔订单。必要时使用 `UNKNOWN`，并以 venue 查询和 Drop Copy 收敛。

---

## 29.7 算法执行：TWAP、冰山、POV 的数量守恒与故障边界

算法执行器是一个受限的订单发射器，不是“定时调用下单 API”。它必须维护母单事实、子单事实、市场数据水位、风险预算和交易所回报。

### 29.7.1 母子订单不变量

设母单目标数量 `Q`：

```text
filled_parent + canceled_parent + working_children + reserved_to_send ≤ Q
0 ≤ each_child_qty ≤ child_cap
sum(child.filled_qty) = parent.accepted_filled_qty
```

`reserved_to_send` 是算法已经决定、但尚未写入/发送成功的数量；没有这个中间量，两个调度 tick 可能同时看到“还剩 1,000”并各发 1,000。创建子单前必须原子地 claim 数量；子单拒单后才能释放 claim，不能用定时任务猜测。

母单的 `Q`、方向、证券、账户、价格保护、截止时间和风险版本不可变；算法参数变更创建 `algo_run_version`，旧版本停止产生新子单但继续收敛已有子单。

### 29.7.2 TWAP

目标是在 `[start, end)` 区间均匀发出总量：

```text
slice_count = ceil((end - start) / interval)
base_i = floor(Q / slice_count)
remainder = Q - base_i * slice_count
slice_i = base_i + 1（前 remainder 个）或 base_i
```

生产修正：

- 不能按 wall clock 每次直接发固定量，要以 `parent_remaining - working_children - reserved_to_send` 为上限；
- 交易所最小手数、lot size 和价格 tick 必须在切片前量化；尾数按确定性规则分配，不能因重启改变；
- 调度延迟时，允许 catch-up 但设置 `max_catch_up_qty`，不能把多个窗口一次性倾倒进市场；
- 部分成交后，未成交子单仍算在 working，不能因时间片结束就当作已消失；
- 到期时策略明确：撤剩余、继续到 grace period，或转人工，不可无限延长。

### 29.7.3 冰山（Iceberg）

冰山把母单拆成可见峰值和隐藏剩余：

```text
display_qty = min(peak_qty, parent_remaining - working_hidden - working_visible)
```

一个子单成交/撤销后，下一峰值的生成必须等待事实闭合；不能只看本地发送成功。若交易所原生支持 `MaxFloor/DisplayQty`，优先使用交易所语义；自建冰山要考虑撤单重挂导致的排队位置损失、价格漂移、最小量和随机化峰值。随机化不是安全边界，必须把随机种子/策略版本写进审计，重放时能解释。

### 29.7.4 POV（Percentage of Volume）

在观察窗口成交量 `V` 中参与目标比例 `r`：

```text
target = floor(r * V * participation_adjustment)
sendable = max(0, target - child_filled_or_working_credit)
```

关键是 `V` 的来源和时间窗：行情 feed 断流、重复 trade、跨交易所聚合和 late print 都会使量虚高。必须：

- 对市场成交事件按 `(venue, trade_id)` 去重；没有 trade ID 时使用受限指纹和窗口容错，并标记不确定性；
- 设置每秒/每分钟硬上限、最大名义金额、价格保护、熔断状态和参与率上限；
- 不能把自家成交量或明显异常量无条件算入 volume；
- volume 突增时不允许正反馈无限加速，使用 EMA、winsorize、异常检测和 `max_rate`；
- 行情延迟/水位落后超过阈值时暂停发单，而不是沿用最后一个 volume；
- 目标量计算后仍需经过母单数量守恒和事前风控。

2010 年闪电崩盘式的教训是：固定 POV 在虚高成交量、没有价格下限和没有时间上限时会形成正反馈。生产 POV 必须把“市场量异常”视为风险事件，而不是执行机会。

### 29.7.5 算法状态机

```text
CREATED
  → RISK_APPROVED
  → RUNNING
      ├─ PAUSED（行情/会话/风控/人工）
      ├─ DRAINING（停止新子单，等待在途回报）
      ├─ CANCELING（撤剩余）
      ├─ COMPLETED（filled=Q 或策略完成）
      └─ UNKNOWN（执行事实未收敛）
```

算法重启必须从数据库重建：母单累计成交、子单工作状态、已 claim 数量、FIX 未决命令、市场数据水位和风险版本。不能从“最后一个 cron tick”推导。`UNKNOWN` 下只能查单/对账，不能为“补足目标”盲目发新子单。

---

## 29.8 衔接：从 OMS/FIX 执行事实到风控、清算、对账和后续章节

本节是本章与金融系统其他边界的接缝。OMS 不应试图吞掉所有资金、持仓和清算逻辑，但必须提供足够稳定、不可变、可重放的执行事实。

### 29.8.1 与前置章节的连接

| 本章产物 | 对接章节 | 接口边界 |
|---|---|---|
| `command_id`、`ClOrdID` 指纹、UNKNOWN | [24 幂等](24-end-to-end-financial-idempotency.md) | API 重试和 FIX 重试共用身份原则，但作用域不同 |
| 订单状态转移、Outbox、补偿 | [25 状态机](25-state-machines-tcc-saga-and-outbox.md) | OMS 状态可重建；外部成交是事实，不用 Saga 猜测交易所结果 |
| 成交、费用、持仓/现金分录 | [22/23 复式账本](22-double-entry-ledger-schema-and-concurrency.md) | OMS 产出 canonical execution，账本决定借贷分录和冲正 |
| 会计日、清算批次、在途 | [26 清结算](26-clearing-settlement-netting-and-cutoff.md) | 成交日、结算日、价值日不可混用 |
| Drop Copy、券商确认、交易所日终报告 | [27 对账](27-multi-tier-reconciliation-and-auto-healing.md) | 原始回报、规范化执行和对账映射三层保存 |
| 价格、金额、数量 tick/lot | [20 定点算术](20-monetary-precision-rounding-and-allocation.md) | 价格/数量用定点或整数 scale，舍入规则版本化 |
| 超量、错误路由、异常成交 | [11 风险与崩盘](11-financial-risk-taxonomy-and-crashes.md) | 算法熔断、限额、价格保护和人工停机 |

### 29.8.2 发送给风险与账本的 canonical event

不要让下游直接解析 FIX 原文。建议发布版本化事件：

```json
{
  "event_type": "OrderExecutionAccepted.v1",
  "event_id": "oev_01J...",
  "order_id": "ord_01J...",
  "parent_order_id": "ord_parent...",
  "command_id": "cmd_01J...",
  "venue": "XNAS",
  "account": "A1001",
  "symbol_id": "US:XYZ",
  "side": "BUY",
  "exec_id": "E123",
  "last_qty": 3000,
  "last_px_minor": 9990,
  "price_scale": 2,
  "cum_qty": 3000,
  "leaves_qty": 7000,
  "fee_estimate_minor": 30,
  "venue_transact_time": "2026-11-25T10:00:00.020Z",
  "oms_commit_seq": 881239,
  "source_fix_hash": "sha256:..."
}
```

账本消费者按 `event_id` 或 `(venue, session, exec_id)` 幂等，但仍应保留 `order_id` 关联。订单状态事件不能替代成交事件；`FILLED` 事件没有 `Execution` 明细时，不能自动记账。

### 29.8.3 向后续 30–34 章节交接的最小契约

后续的事前风控、撮合/路由、清算、行情和高可用章节应消费以下契约，而不是重新猜 OMS 语义：

1. `OrderAccepted`：经过参数、账户、证券和风险校验，但尚未证明交易所接受；
2. `OrderSent`：FIX 消息已按 fencing token 从 outbox 发送，含 session seq 和 payload hash；
3. `OrderVenueAccepted/Rejected`：有可验证的外部回报；
4. `ExecutionAccepted`：唯一成交事实已落库并可驱动账本；
5. `OrderUnknown`：有明确的原因、查询计划、截止时间和冻结策略；
6. `ReservationChanged`：冻结增减的不可变原因、版本和幂等键；
7. `ReconciliationException`：证据、范围、差异金额/数量、当前责任人和修复状态。

如果后续章节实现撮合或高可用主备，应保持本章的两个原则：序号与业务事实分离，fencing 与幂等不能被“低延迟优化”删除。低延迟只允许缩小证据落库路径，不能缩小事实边界。

---

## 29.9 PostgreSQL DDL：事实、投影、会话和冻结

下面 DDL 以 PostgreSQL 16 为例。金额/价格字段在示例中使用 `numeric` 便于展示；极致低延迟市场可用带 scale 的 `bigint`，但必须在证券主数据中保存 scale 和溢出边界。生产环境应按业务日、venue 或数量分区，并为归档设计独立策略。

### 29.9.1 类型与订单主表

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE oms_order_status AS ENUM (
  'RECEIVED', 'VALIDATING', 'PENDING_NEW', 'WORKING',
  'PARTIALLY_FILLED', 'PENDING_CANCEL', 'PENDING_REPLACE',
  'CANCELED', 'FILLED', 'REJECTED', 'EXPIRED', 'UNKNOWN', 'TERMINATED'
);

CREATE TYPE order_side AS ENUM ('BUY', 'SELL');
CREATE TYPE order_kind AS ENUM ('MARKET', 'LIMIT', 'STOP', 'STOP_LIMIT');
CREATE TYPE command_kind AS ENUM ('NEW', 'CANCEL', 'REPLACE', 'SUSPEND', 'RESUME');

CREATE TABLE oms_order (
  order_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  parent_order_id       uuid REFERENCES oms_order(order_id),
  account_id            text NOT NULL,
  client_order_id       text NOT NULL,
  venue_id              text NOT NULL,
  symbol_id             text NOT NULL,
  side                  order_side NOT NULL,
  order_kind            order_kind NOT NULL,
  tif                   text NOT NULL,
  currency              char(3) NOT NULL,
  order_qty             bigint NOT NULL CHECK (order_qty > 0),
  limit_price_minor     bigint,
  price_scale           smallint NOT NULL CHECK (price_scale BETWEEN 0 AND 18),
  price_cap_minor       bigint,
  cum_qty               bigint NOT NULL DEFAULT 0 CHECK (cum_qty >= 0),
  canceled_qty          bigint NOT NULL DEFAULT 0 CHECK (canceled_qty >= 0),
  reported_leaves_qty   bigint,
  avg_price_minor       numeric(38, 18),
  status                oms_order_status NOT NULL DEFAULT 'RECEIVED',
  active_cl_ord_id      text NOT NULL,
  venue_order_id        text,
  version               bigint NOT NULL DEFAULT 0,
  risk_version          text NOT NULL,
  command_fingerprint   text NOT NULL,
  created_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (cum_qty + canceled_qty <= order_qty),
  CHECK (limit_price_minor IS NULL OR limit_price_minor >= 0),
  CHECK (price_cap_minor IS NULL OR price_cap_minor >= 0),
  CHECK (order_kind <> 'LIMIT' OR limit_price_minor IS NOT NULL)
);

-- client_order_id 的真正作用域必须和 venue 文档一致；这里假设 account+venue 足够。
CREATE UNIQUE INDEX ux_order_clord_scope
  ON oms_order(account_id, venue_id, client_order_id);
CREATE INDEX ix_order_account_status ON oms_order(account_id, status, updated_at);
CREATE INDEX ix_order_parent ON oms_order(parent_order_id) WHERE parent_order_id IS NOT NULL;
CREATE INDEX ix_order_venue_id ON oms_order(venue_id, venue_order_id)
  WHERE venue_order_id IS NOT NULL;
```

`CHECK (cum_qty + canceled_qty <= order_qty)` 不能表达所有跨表规则，例如成交事实汇总和 `oms_order.cum_qty` 是否一致，因此必须在事务中用锁和服务层校验，再由对账任务周期性验证。

### 29.9.2 命令、版本、状态事件和 FIX 原文

```sql
CREATE TABLE oms_order_command (
  command_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id            text NOT NULL,
  order_id              uuid REFERENCES oms_order(order_id),
  parent_order_id       uuid,
  command_kind          command_kind NOT NULL,
  client_request_id     text NOT NULL,
  cl_ord_id             text NOT NULL,
  orig_cl_ord_id        text,
  request_fingerprint   text NOT NULL,
  payload_json          jsonb NOT NULL,
  result_status         text NOT NULL DEFAULT 'ACCEPTED',
  error_code            text,
  created_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (account_id, client_request_id),
  UNIQUE (account_id, cl_ord_id)
);

CREATE TABLE oms_order_event (
  order_event_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_id              uuid NOT NULL REFERENCES oms_order(order_id),
  event_type            text NOT NULL,
  from_status           oms_order_status,
  to_status             oms_order_status,
  order_version         bigint NOT NULL,
  source                 text NOT NULL, -- API/FIX/DROPCOPY/RECON/MANUAL
  source_id              text,
  payload_json           jsonb NOT NULL,
  occurred_at            timestamptz NOT NULL,
  committed_at           timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (order_id, order_version)
);

CREATE TABLE fix_message_inbox (
  inbox_id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  venue_id              text NOT NULL,
  session_id            text NOT NULL,
  msg_seq_num           bigint NOT NULL,
  msg_type              text NOT NULL,
  poss_dup              boolean NOT NULL DEFAULT false,
  orig_sending_time     timestamptz,
  sending_time          timestamptz,
  raw_message           bytea NOT NULL,
  payload_sha256        bytea NOT NULL,
  process_status        text NOT NULL DEFAULT 'RECEIVED',
  received_at           timestamptz NOT NULL DEFAULT clock_timestamp(),
  processed_at          timestamptz,
  UNIQUE (venue_id, session_id, msg_seq_num)
);

CREATE TABLE execution_fact (
  execution_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  venue_id              text NOT NULL,
  session_id            text NOT NULL,
  exec_id               text NOT NULL,
  order_id              uuid REFERENCES oms_order(order_id),
  cl_ord_id             text NOT NULL,
  venue_order_id        text,
  exec_type             text NOT NULL,
  venue_ord_status      text,
  last_qty              bigint NOT NULL DEFAULT 0 CHECK (last_qty >= 0),
  last_price_minor      bigint,
  cum_qty_reported      bigint,
  leaves_qty_reported   bigint,
  fee_minor             bigint NOT NULL DEFAULT 0,
  raw_inbox_id          bigint REFERENCES fix_message_inbox(inbox_id),
  payload_sha256        bytea NOT NULL,
  venue_transact_time   timestamptz,
  received_at           timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (venue_id, session_id, exec_id)
);
CREATE INDEX ix_execution_order ON execution_fact(order_id, received_at);
```

命令表不应使用“插入失败就当重复”的粗暴逻辑：先按唯一键读回原记录，比较 `request_fingerprint`，相同才返回原结果，冲突必须报警。

### 29.9.3 冻结、Outbox、算法和会话租约

```sql
CREATE TABLE reservation_event (
  reservation_event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  reservation_id       uuid NOT NULL,
  order_id             uuid NOT NULL REFERENCES oms_order(order_id),
  account_id           text NOT NULL,
  asset_id             text NOT NULL,
  bucket               text NOT NULL, -- CASH / SECURITY / FEE_BUFFER
  delta_minor          bigint NOT NULL,
  reason               text NOT NULL,
  source_id            text NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (reservation_id, source_id)
);
CREATE INDEX ix_reservation_order ON reservation_event(order_id, created_at);

CREATE TABLE outbox_message (
  outbox_id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  aggregate_type        text NOT NULL,
  aggregate_id          uuid NOT NULL,
  message_type          text NOT NULL,
  dedupe_key            text NOT NULL,
  payload_json          jsonb NOT NULL,
  fencing_token         bigint,
  available_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
  claimed_at            timestamptz,
  sent_at               timestamptz,
  attempts              integer NOT NULL DEFAULT 0,
  last_error            text,
  UNIQUE (dedupe_key)
);
CREATE INDEX ix_outbox_ready ON outbox_message(available_at, outbox_id)
  WHERE sent_at IS NULL;

CREATE TABLE algo_parent (
  algo_id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  parent_order_id       uuid NOT NULL REFERENCES oms_order(order_id),
  algo_type             text NOT NULL CHECK (algo_type IN ('TWAP','ICEBERG','POV')),
  target_qty            bigint NOT NULL CHECK (target_qty > 0),
  filled_qty            bigint NOT NULL DEFAULT 0,
  working_qty           bigint NOT NULL DEFAULT 0,
  reserved_to_send_qty  bigint NOT NULL DEFAULT 0,
  config_json           jsonb NOT NULL,
  config_version        bigint NOT NULL,
  status                text NOT NULL,
  market_watermark      text,
  version               bigint NOT NULL DEFAULT 0,
  CHECK (filled_qty + working_qty + reserved_to_send_qty <= target_qty)
);

CREATE TABLE gateway_lease (
  lease_name            text PRIMARY KEY,
  holder_id             text NOT NULL,
  fencing_token         bigint NOT NULL,
  lease_until           timestamptz NOT NULL,
  updated_at            timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE fix_session_state (
  venue_id              text NOT NULL,
  session_id            text NOT NULL,
  next_send_seq         bigint NOT NULL,
  next_recv_seq         bigint NOT NULL,
  last_contiguous_recv  bigint NOT NULL,
  connection_generation bigint NOT NULL DEFAULT 0,
  status                text NOT NULL,
  updated_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (venue_id, session_id)
);
```

实际系统还需要账户余额、证券持仓、币种精度、证券交易日和风控限额表；本章只展示 OMS 边界。`reservation_event` 的余额投影必须由同一账户锁、分录或专门 reservation aggregate 管理，不能直接对事件表 `SUM` 后绕过账户并发控制。

---

## 29.10 内部协议：API、命令和事件的版本化

### 29.10.1 下单命令

```json
{
  "command_id": "cmd_01J...",
  "client_request_id": "mobile-req-7788",
  "account_id": "A1001",
  "venue": "XNAS",
  "symbol_id": "US:XYZ",
  "side": "BUY",
  "order_type": "LIMIT",
  "quantity": 10000,
  "limit_price_minor": 10000,
  "price_scale": 2,
  "currency": "USD",
  "time_in_force": "DAY",
  "idempotency_key": "A1001-req-7788",
  "risk_context": {"policy_version": "risk-2026-11-25.3"}
}
```

字段必须通过 JSON Schema 校验；服务端生成 `order_id`/`ClOrdID`，客户端不能指定另一个订单的 `OrigClOrdID`。返回 `202 Accepted` 只表示命令事实已提交，返回体要有 `command_id`、`order_id`、当前 `status` 和查询 URL。

### 29.10.2 撤改单命令

```json
{
  "command_id": "cmd_cancel_01J...",
  "order_id": "ord_01J...",
  "expected_order_version": 7,
  "target_cl_ord_id": "A1001-261125-000001-R02",
  "kind": "REPLACE",
  "new_quantity": 8000,
  "new_limit_price_minor": 9950,
  "price_scale": 2,
  "idempotency_key": "replace-ord_01J-v7"
}
```

`expected_order_version` 防止客户端拿旧页面误撤新版本；版本冲突返回当前快照而不是静默覆盖。若已进入 `PENDING_CANCEL`，新的 Replace 默认拒绝或排队策略必须显式配置，不能让两个 outbox 同时引用不同的 active `ClOrdID`。

### 29.10.3 事件兼容规则

- 事件类型带版本，如 `ExecutionAccepted.v1`；新增字段可选，改变语义必须升版本；
- 金额和数量带单位、scale、币种/资产，不能仅叫 `price`；
- `source_fix_hash`、`venue_id`、`session_id`、`exec_id` 进入审计；
- 消费者至少一次，事件带 `event_id`；消费者以业务唯一键幂等；
- 删除字段前先经历兼容期，不能让账本消费者依据缺失字段猜测；
- 事件时间、接收时间和提交序号分别传递，避免下游按一个 `timestamp` 排序解决所有问题。

---

## 29.11 Go 核心代码：幂等执行、状态归并和 TWAP claim

代码展示事务边界和不变量，`Repo` 由实际 PostgreSQL 驱动实现；不能把示例中的内存 map 直接用于生产。

### 29.11.1 成交回报幂等处理

```go
package oms

import (
	"context"
	"errors"
	"fmt"
	"math"
)

var (
	ErrExecConflict = errors.New("execution id content conflict")
	ErrInvariant    = errors.New("oms invariant violated")
)

type ExecKey struct {
	Venue, Session, ExecID string
}

type ExecutionReport struct {
	Venue, Session, ExecID string
	OrderID                string
	ClOrdID, VenueOrderID  string
	ExecType, OrdStatus    string
	LastQty, CumQty        int64
	LastPxMinor            int64
	ReportedLeaves         int64
	PayloadHash            [32]byte
}

type Order struct {
	ID, Account, ActiveClOrdID string
	Status                     string
	OrderQty, CumQty           int64
	CanceledQty                int64
	PriceCapMinor              int64
	FeeBufferPerUnitMinor      int64
	ReservedCashMinor          int64
	Version                    int64
}

type ExecRecord struct {
	Key         ExecKey
	PayloadHash [32]byte
}

type Tx interface {
	LockOrder(ctx context.Context, orderID string) (Order, error)
	FindExecution(ctx context.Context, key ExecKey) (*ExecRecord, error)
	InsertExecution(ctx context.Context, r ExecutionReport) error
	UpdateOrder(ctx context.Context, o Order) error // WHERE id AND version
	AppendOrderEvent(ctx context.Context, orderID, typ string, payload any) error
	AppendReservation(ctx context.Context, orderID, reason string, delta int64, source string) error
	PublishOutbox(ctx context.Context, key, typ string, payload any) error
	LockAlgo(ctx context.Context, algoID string) (AlgoState, error)
	UpdateAlgo(ctx context.Context, s AlgoState) error
	AppendAlgoEvent(ctx context.Context, algoID, typ string, payload any) error
	ChildClaimQty(ctx context.Context, childID string) (int64, error)
	Commit() error
	Rollback() error
}

type DB interface {
	Begin(ctx context.Context) (Tx, error)
}

func ApplyExecution(ctx context.Context, db DB, r ExecutionReport) (bool, error) {
	tx, err := db.Begin(ctx)
	if err != nil { return false, err }
	defer tx.Rollback() // commit 后由实现返回 ErrTxDone 或安全忽略

	key := ExecKey{r.Venue, r.Session, r.ExecID}
	old, err := tx.FindExecution(ctx, key)
	if err != nil { return false, err }
	if old != nil {
		if old.PayloadHash != r.PayloadHash {
			// 同一 ExecID 不同内容不能“以最新为准”。
			return false, fmt.Errorf("%w: venue=%s session=%s exec=%s", ErrExecConflict, r.Venue, r.Session, r.ExecID)
		}
		if err := tx.Commit(); err != nil { return false, err }
		return false, nil // duplicate: no second economic effect
	}

	o, err := tx.LockOrder(ctx, r.OrderID)
	if err != nil { return false, err }
	if r.LastQty < 0 || o.CumQty+r.LastQty > o.OrderQty {
		return false, fmt.Errorf("%w: qty order=%d cum=%d last=%d", ErrInvariant, o.OrderQty, o.CumQty, r.LastQty)
	}
	if r.ClOrdID != o.ActiveClOrdID && r.ExecType != "F" {
		// 交易所可能对旧 ClOrdID 回报，保留事实；状态动作需按替换链规则决定。
		return false, fmt.Errorf("stale clordid %s for order %s", r.ClOrdID, o.ID)
	}

	if err := tx.InsertExecution(ctx, r); err != nil { return false, err }
	if r.ExecType == "F" && r.LastQty > 0 {
		oldReserved := o.ReservedCashMinor
		o.CumQty += r.LastQty
		if o.CumQty == o.OrderQty {
			o.Status = "FILLED"
		} else {
			o.Status = "PARTIALLY_FILLED"
		}
		remaining := o.OrderQty - o.CumQty - o.CanceledQty
		if remaining < 0 {
			return false, fmt.Errorf("%w: negative remaining=%d", ErrInvariant, remaining)
		}
		newReserved, err := checkedReservation(remaining, o.PriceCapMinor, o.FeeBufferPerUnitMinor)
		if err != nil { return false, err }
		o.ReservedCashMinor = newReserved
		// delta 是现金最小单位，不是成交股数；负数表示释放，正数表示补足。
		if err := tx.AppendReservation(ctx, o.ID, "FILL_REDUCE_REMAINING", newReserved-oldReserved, r.ExecID); err != nil {
			return false, err
		}
	}
	o.Version++
	if err := tx.UpdateOrder(ctx, o); err != nil { return false, err }
	if err := tx.AppendOrderEvent(ctx, o.ID, "EXECUTION_ACCEPTED", r); err != nil { return false, err }
	if err := tx.PublishOutbox(ctx, o.ID, "order.execution.accepted", r); err != nil { return false, err }
	if err := tx.Commit(); err != nil { return false, err }
	return true, nil
}

func checkedReservation(remaining, priceCap, feePerUnit int64) (int64, error) {
	if remaining < 0 || priceCap < 0 || feePerUnit < 0 || priceCap > math.MaxInt64-feePerUnit {
		return 0, ErrInvariant
	}
	perUnit := priceCap + feePerUnit
	if remaining != 0 && perUnit > math.MaxInt64/remaining {
		return 0, ErrInvariant
	}
	return remaining * perUnit, nil
}
```

真实代码还要处理 Trade Bust/Correct、卖单证券冻结、成交费用、交易所累计量校验、`PENDING_CANCEL` 的 late fill、`OrderID` 绑定和数据库唯一键冲突。示例有意把所有经济动作放在同一个事务接口中，防止“先插入成交再异步更新冻结”。

### 29.11.2 TWAP 的原子 claim

```go
type AlgoState struct {
	Target, Filled, Working, Reserved int64
	Version                           int64
}

// ClaimForSlice 在数据库事务中 SELECT ... FOR UPDATE；返回值是本次唯一拥有的量。
func ClaimForSlice(ctx context.Context, tx Tx, algoID string, desired, childCap int64) (int64, error) {
	if desired <= 0 || childCap <= 0 {
		return 0, nil
	}
	s, err := tx.LockAlgo(ctx, algoID) // 真实接口需提供
	if err != nil { return 0, err }
	remaining := s.Target - s.Filled - s.Working - s.Reserved
	if remaining <= 0 { return 0, nil }
	q := desired
	if q > childCap { q = childCap }
	if q > remaining { q = remaining }

	s.Reserved += q
	s.Version++
	if err := tx.UpdateAlgo(ctx, s); err != nil { return 0, err }
	if err := tx.AppendAlgoEvent(ctx, algoID, "SLICE_CLAIMED", q); err != nil { return 0, err }
	if err := tx.Commit(); err != nil { return 0, err }
	return q, nil
}

// 发送成功后，事务性地把 reserved 转为 working；发送失败则释放 reserved。
func FinalizeChildClaim(ctx context.Context, tx Tx, algoID, childID string, sent bool) error {
	s, err := tx.LockAlgo(ctx, algoID)
	if err != nil { return err }
	q, err := tx.ChildClaimQty(ctx, childID)
	if err != nil { return err }
	if sent {
		s.Reserved -= q
		s.Working += q
	} else {
		s.Reserved -= q
	}
	if s.Reserved < 0 || s.Working < 0 || s.Filled+s.Working+s.Reserved > s.Target {
		return ErrInvariant
	}
	s.Version++
	if err := tx.UpdateAlgo(ctx, s); err != nil { return err }
	return tx.AppendAlgoEvent(ctx, algoID, "CHILD_CLAIM_FINALIZED", map[string]any{
		"child_id": childID, "qty": q, "sent": sent,
	})
}
```

注意：示例 `FinalizeChildClaim` 未 `Commit`，调用者必须把它与 outbox/子单记录放进同一事务；真实实现还应使用 `UPDATE ... WHERE version=?` 或行锁，防止调度器和回报消费者同时改 `Working`。

---

## 29.12 Java 21 + QuickFIX/J 核心代码：映射、会话和幂等入口

### 29.12.1 构造 `NewOrderSingle`

```java
import quickfix.field.*;
import quickfix.fix44.NewOrderSingle;
import quickfix.field.OrdType;
import quickfix.field.Side;
import quickfix.field.TimeInForce;

import java.math.BigDecimal;
import java.time.Instant;

public final class FixOrderMapper {
    public static NewOrderSingle newLimit(
            String clOrdId, String account, String symbol,
            char side, long qty, BigDecimal price, String currency) {
        if (qty <= 0 || price.signum() < 0) {
            throw new IllegalArgumentException("invalid order quantity or price");
        }

        var msg = new NewOrderSingle(
                new ClOrdID(clOrdId),
                new HandlInst(HandlInst.AUTOMATED_EXECUTION_ORDER_PRIVATE),
                new Symbol(symbol),
                new Side(side),
                new TransactTime(),
                new OrdType(OrdType.LIMIT));
        msg.set(new Account(account));
        msg.set(new OrderQty(BigDecimal.valueOf(qty)));
        msg.set(new Price(price));
        msg.set(new TimeInForce(TimeInForce.DAY));
        msg.set(new Currency(currency));
        // 交易所需要 SecurityID、ExDestination、ExecInst 等时由能力矩阵注入。
        return msg;
    }
}
```

不要用 `new BigDecimal(double)`；价格应从字符串/定点整数解析，并在进入 FIX 前做 tick、scale、范围和舍入检查。`TransactTime`、`ClOrdID` 和 `OrderQty` 是业务字段，`MsgSeqNum`、`SendingTime`、`CheckSum` 由 QuickFIX/J 会话层生成，业务代码不应手写。

### 29.12.2 `fromApp` 只负责接收，业务处理要有持久化边界

```java
import quickfix.SessionID;
import quickfix.field.ExecID;
import quickfix.field.ExecType;
import quickfix.field.LastPx;
import quickfix.field.LastQty;
import quickfix.field.OrderID;
import quickfix.fix44.ExecutionReport;
import quickfix.Application;
import quickfix.Message;
import quickfix.field.MsgType;

public final class FixApplication implements Application {
    private final ExecutionInbox inbox;

    public FixApplication(ExecutionInbox inbox) {
        this.inbox = inbox;
    }

    @Override
    public void fromApp(Message message, SessionID sessionID)
            throws quickfix.FieldNotFound {
        if (MsgType.EXECUTION_REPORT.equals(message.getHeader().getString(MsgType.FIELD))) {
            var er = (ExecutionReport) message;
            var canonical = new CanonicalExecution(
                    sessionID.getTargetCompID(),
                    sessionID.toString(),
                    er.getString(ExecID.FIELD),
                    er.getString(OrderID.FIELD),
                    er.getChar(ExecType.FIELD),
                    er.isSetField(LastQty.FIELD)
                            ? er.getDecimal(LastQty.FIELD).longValueExact() : 0L,
                    er.isSetField(LastPx.FIELD) ? er.getDecimal(LastPx.FIELD) : null,
                    message.toString());
            // inbox 必须先把原文、34、hash 写入数据库；只放队列会在进程崩溃时丢证据。
            inbox.accept(sessionID, message, canonical);
        }
    }

    // toAdmin、fromAdmin、toApp、onCreate、onLogout 等实现省略。
    // 生产代码需校验 Logon、ResetSeqNumFlag、心跳和 session 能力矩阵。
}
```

`fromApp` 线程不应在 QuickFIX/J 回调里执行慢 SQL、等待外部服务或同步调用账本；但也不能在原文持久化前直接 ACK/推进会话水位。常见实现是“轻量原文落盘 + 有界业务队列 + 可重放 inbox worker”，并用背压保护内存。

### 29.12.3 Java 端金额与均价

```java
import java.math.BigDecimal;
import java.math.RoundingMode;

public record FillTotals(long qty, BigDecimal notional) {
    public FillTotals add(long lastQty, BigDecimal lastPx) {
        if (lastQty <= 0 || lastPx.signum() < 0) {
            throw new IllegalArgumentException("invalid fill");
        }
        return new FillTotals(qty + lastQty,
                notional.add(lastPx.multiply(BigDecimal.valueOf(lastQty))));
    }

    public BigDecimal average(int priceScale) {
        if (qty == 0) return BigDecimal.ZERO.setScale(priceScale, RoundingMode.UNNECESSARY);
        return notional.divide(BigDecimal.valueOf(qty), priceScale,
                RoundingMode.HALF_EVEN);
    }
}
```

`BigDecimal` 只负责数值，不负责订单幂等和事务；不能因 Java 代码“精确”就把 `ExecutionReport` 直接重复累计。高吞吐网关可在边界把价格转为 `long priceMinor`，但必须处理乘法溢出（使用 `Math.multiplyExact` 或更宽整数）并保存 scale。

---

## 29.13 对账与日终：订单、回报、持仓、现金四层闭环

### 29.13.1 实时对账对象

至少维护以下四种视图：

| 对账层 | 比较对象 | 关键键 | 目标 |
|---|---|---|---|
| 会话层 | 本地 FIX seq vs 对手方 seq/补包结果 | `venue/session/seq` | 没有未解释缺口 |
| 订单层 | OMS working/terminal vs venue open orders | `account/venue/OrderID/ClOrdID` | 没有幽灵订单、双重订单 |
| 成交层 | OMS execution vs Drop Copy/venue trade report | `venue/session/ExecID` | 每笔成交一次且内容一致 |
| 经济层 | 成交/费用 vs 现金、证券冻结/账本 | `order/exec/asset/account` | 数量、金额、费用守恒 |

### 29.13.2 对账差异分类

```text
MISSING_INTERNAL_EXEC       venue 有，OMS 无
MISSING_VENUE_EXEC          OMS 有，venue/DropCopy 无
DUPLICATE_EXEC              同一 ExecID 多经济影响
QTY_MISMATCH                last/cum/leaves 或聚合数量不一致
PRICE_MISMATCH              成交价格/币种/scale 不一致
ORPHAN_ORDER                venue working，OMS 无
STALE_ORDER                 OMS working，venue 已撤/过期
RESERVATION_MISMATCH        冻结与 leaves 不匹配
SESSION_GAP                 FIX 序号未闭合
LATE_CORRECTION             Bust/Correct 到达已发布批次
```

自动修复只允许白名单：例如同 `ExecID` 原文已在 inbox、业务处理失败且没有经济事实，可重放；如果 venue 有成交而 OMS 无法证明是否已经入账，必须进入 `UNKNOWN/MANUAL_REVIEW`，不能直接补一笔。修复操作使用独立 `repair_key`，以追加事实/冲正完成，并重跑受影响窗口。

### 29.13.3 关键 SQL 检查

```sql
-- 订单聚合成交量必须与唯一执行事实一致。
SELECT o.order_id, o.cum_qty, COALESCE(SUM(e.last_qty) FILTER (WHERE e.exec_type = 'F'), 0) AS fact_qty
FROM oms_order o
LEFT JOIN execution_fact e ON e.order_id = o.order_id
GROUP BY o.order_id, o.cum_qty
HAVING o.cum_qty <> COALESCE(SUM(e.last_qty) FILTER (WHERE e.exec_type = 'F'), 0);

-- 未解释的 venue working 订单。
SELECT v.venue_order_id, v.cl_ord_id
FROM venue_open_order_snapshot v
LEFT JOIN oms_order o
  ON o.venue_id = v.venue_id AND o.venue_order_id = v.venue_order_id
WHERE o.order_id IS NULL;

-- 算法数量守恒。
SELECT algo_id
FROM algo_parent
WHERE filled_qty + working_qty + reserved_to_send_qty > target_qty
   OR filled_qty < 0 OR working_qty < 0 OR reserved_to_send_qty < 0;
```

日终报告必须保留原始文件/消息 hash、文件版本、水位、截止时间、补包结果、迟到事件和报告版本。总量相等不能替代逐笔身份匹配：一笔重复成交和一笔漏成交可能在净额上互相抵消。

---

## 29.14 压测、故障注入与混沌工程

### 29.14.1 压测模型

不要只测 HTTP QPS。至少构造四个独立速率：

```text
lambda_command      = 客户新单/撤单/改单速率
lambda_fix_out      = FIX 出站消息速率
lambda_fix_in       = ExecutionReport/行情/补包入站速率
lambda_reconcile    = DropCopy/日终报告批量速率
```

压测维度：

- 订单比例：新单、撤单、改单、重复命令、未知重试；
- 成交比例：无成交、全成、碎片化部分成交、同毫秒多笔成交、late fill；
- 会话：正常、延迟 p99、断线重连、序号缺口、补包风暴、对手方拒绝 Logon；
- 数据：百万账户、热点账户、同一订单高并发撤改、超长 `Text`、极端价格/数量；
- 算法：TWAP 睡眠漂移、POV volume spike、冰山峰值小于最小手数、重启恢复；
- 数据库：锁等待、主从切换、WAL 延迟、唯一键冲突、outbox 堆积。

核心指标：

| 指标 | 目标/告警方向 |
|---|---|
| `command_to_outbox_commit_latency` | p50/p95/p99，不能只看 HTTP 延迟 |
| `fix_send_to_ack_latency` | 按 venue/session 分桶 |
| `exec_received_to_ledger_latency` | 成交到经济事实的长尾 |
| `duplicate_exec_rate` | 异常升高代表对手方/网络问题 |
| `sequence_gap_age` | 最大未闭合缺口年龄 |
| `unknown_order_count/age` | 不能无限增长 |
| `reservation_mismatch_count` | 任何非零都应有责任队列 |
| `algo_overclaim_reject` | 必须为零；非零即熔断 |
| `outbox_lag` | 按优先级、venue、fencing token 分桶 |
| `reconciliation_unexplained_notional` | 按账户/证券/venue 追踪 |

### 29.14.2 混沌场景与预期

| 注入 | 必须观察到的行为 |
|---|---|
| API 响应在提交后丢失 | 客户重试命中同一 `client_request_id`，不产生第二个 ClOrdID |
| FIX 出站 socket 在 send 后断开 | 命令进入 UNKNOWN/查询，不自动换新 ID |
| `ExecutionReport` 重复 100 次 | 一条成交事实、一次资金动作、重复消息有审计记录 |
| 回报乱序 | 按 seq 补包；业务事实不重复，状态最终收敛 |
| `ExecID` 同值不同 payload | 熔断/人工，不产生第二次经济影响 |
| 主节点失去租约但 TCP 未断 | fencing 阻止旧主发送 |
| 数据库在成交提交后进程崩溃 | 重放唯一键安全；订单和冻结同事务一致 |
| Drop Copy 延迟 30 分钟 | 实时层标记 provisional，最终对账等待水位 |
| POV volume 突增 100 倍 | 速率上限/异常检测触发暂停，不无限加速 |
| 账户余额在撤改单并发扣减 | 行锁/版本条件拒绝一方，不能负可用余额 |

### 29.14.3 属性测试和模型测试

对随机生成的事件序列测试以下性质：

```text
任意重复/乱序排列（在可处理的事实集合内）后：
  unique executions 相同
  cum_qty 相同
  reservation event 的净额相同
  parent children total 不超过 Q
  状态可由事件重放得到相同快照
```

但不能宣称“任意乱序都等价”：Bust/Correct、撤单确认和 venue 规则可能要求因果条件。测试模型要把协议允许的因果关系显式编码，违规序列应进入异常而不是被算法强行归并。

---

## 29.15 极端边界与生产检查单

### 29.15.1 数量、价格和时间

- `OrderQty` 大于 32 位、乘以合约乘数溢出 64 位；
- 最小交易单位为 1/1000 股、债券面额、期货合约乘数和奇数手；
- `Price=0`、极大价、负价市场（若产品允许）和 tick 非法；
- 交易所 `CumQty`/`LeavesQty` 为 `0`、缺失或 `NULL`；
- 跨夏令时、交易所本地时间、UTC offset 错误、闰秒和服务器时钟漂移；
- 日切时旧 ClOrdID 是否可复用、序号是否 reset、订单跨交易日 TIF；
- 订单有效期刚好在撤单/成交同一纳秒，必须按 venue 时间和回报事实解释。

### 29.15.2 协议与安全

- 对手方返回未知 Tag、重复 Tag、超长 `Text`、错误 BodyLength/CheckSum；
- `PossDupFlag=Y` 但没有 `OrigSendingTime`；同一 seq 不同 raw hash；
- ResendRequest 范围跨越已归档消息；GapFill 把业务消息错误跳过；
- Logon 身份正确但账户权限变化，不能把会话成功当成可发单；
- 从日志脱敏账户、ClOrdID、订单量和交易策略，但保留足够 hash 做追踪；
- 任何人工“强制成功/强制撤销”都要权限、双人审批、原因和后续对账。

### 29.15.3 撤改和算法

- 部分成交后把新数量设为小于 `CumQty`；
- CancelRequest、ReplaceRequest、late fill、CancelReject 同时到达；
- 子单拒单和母单暂停之间的 claim 释放；
- TWAP 最后一个切片尾数、POV volume 重复、冰山峰值低于 lot size；
- 算法重启后旧子单仍在 venue 工作，数据库却认为没有在途；
- venue 返回 `FILLED` 但 Drop Copy 缺失，或 Drop Copy 有成交而主会话无回报；
- 交易所 Bust 已过日终，账本已结算，必须走更正/冲正流程而不是改历史行。

### 29.15.4 发布前清单

```text
[ ] Data Dictionary、Tag 必填/枚举/长度和 venue capability 已版本化
[ ] New/Cancel/Replace 的 ClOrdID/OrigClOrdID 链能回放
[ ] inbox、execution、order event、reservation、outbox 同事务边界通过测试
[ ] ExecID 重复与内容冲突有不同处理
[ ] seq gap、ResendRequest、GapFill、SequenceReset 有仿真测试
[ ] 失去 lease 的旧主无法发送，fencing token 进入日志/数据库
[ ] market order 有价格/名义保护；POV 有硬速率上限
[ ] 部分成交、撤单拒绝、late fill 不会释放过量冻结
[ ] 算法数量守恒在数据库约束、代码检查和监控三层存在
[ ] Drop Copy/日终报告/持仓现金对账可定位到原始 FIX hash
[ ] UNKNOWN 有查询 SLA、人工边界和停发策略
[ ] 压测覆盖热点账户、序号缺口、补包风暴和数据库锁等待
```

---

## 29.16 术语表

| 术语 | 含义 | 常见误解 |
|---|---|---|
| OMS | Order Management System，订单管理系统 | 不等于撮合引擎，也不等于账本 |
| EMS | Execution Management System，执行管理系统 | 常与 OMS 合并部署，但执行算法边界需明确 |
| FIX | Financial Information eXchange | 既有会话层也有应用层，不能只看 Tag |
| `ClOrdID` | 客户端订单 ID | 不是交易所 `OrderID`，也不是成交 `ExecID` |
| `OrigClOrdID` | 撤改单引用的原客户端 ID | 不是任意历史 ID，通常引用当前活动版本 |
| `ExecID` | 单次执行事实身份 | 必须按 venue 规定作用域去重 |
| `CumQty` | 累计成交量 | 回报字段，不自动等价于 OMS 已入账累计量 |
| `LeavesQty` | 交易所报告的剩余量 | 需与本地成交/取消事实交叉校验 |
| `PENDING_CANCEL` | 已请求撤单、等待外部结果 | 不是 `CANCELED` |
| `UNKNOWN` | 观察不到最终外部结果 | 不是失败，也不是可以重发的许可 |
| PossDup | 可能重复发送标志 | 重复消息仍需校验和幂等处理 |
| Gap Fill | 补齐无业务消息的序号区间 | 不能用来掩盖有业务意义的缺包 |
| Drop Copy | 交易所/经纪商旁路成交回报 | 是证据源，不自动凌驾于所有主会话规则 |
| Fencing token | 单调递增的主租约代号 | 只有心跳/TTL 没有 fencing 不足以防脑裂 |
| Reservation | 资金/证券预留冻结 | 不是最终扣款或持仓事实 |
| TWAP | Time Weighted Average Price 拆单 | 均匀时间不代表无滑点 |
| Iceberg | 冰山订单，峰值可见、剩余隐藏 | 自建冰山会丢排队位置、并有重挂风险 |
| POV | Percentage of Volume，按市场量参与 | 量源异常会造成正反馈 |
| Bust/Correct | 成交撤销/更正 | 应追加冲正或更正事实，不覆盖历史成交 |
| Canonical event | 规范化内部事件 | 不应让下游直接依赖 FIX 原文格式 |

---

## 29.17 面试答题卡

### Q1：为什么收到撤单请求不能立即释放冻结？

因为请求只是命令意图，交易所可能尚未收到、拒绝或与成交竞态。必须进入 `PENDING_CANCEL`，继续接收成交，直到 `CancelAck`/最终查询确认，再按 `CumQty`、`LeavesQty` 和费用 buffer 计算目标冻结。若超时，进入 `UNKNOWN`，保守持有冻结并查询，不能猜失败。

### Q2：`ClOrdID`、`OrderID`、`ExecID` 如何区分？

`ClOrdID` 是平台发给交易所的命令/订单版本身份；`OrigClOrdID` 连接改单链；`OrderID` 是交易所订单身份；`ExecID` 是一次成交/执行事实身份。幂等不能只用其中一个：命令按 `(account, venue, ClOrdID)`，成交按 `(venue, session, ExecID)`，外部 API 另有 `client_request_id`。

### Q3：FIX 序号补包和业务幂等有什么关系？

`MsgSeqNum` 保证会话消息流可恢复，`ExecID` 保证业务成交不重复；前者不能替代后者，因为同一业务回报可能重传、序号可能跨会话，且业务消息可能合法乱序。必须同时保存原始消息、会话序号和执行事实唯一键。

### Q4：如何处理“发送超时，不知道交易所是否收到”的新单？

不能换一个新 `ClOrdID` 盲目重发，会有双单风险。把命令/订单置为 `UNKNOWN` 或 `PENDING_NEW`，用同一 `ClOrdID` 查询、Drop Copy、交易所订单查询和人工 SLA 收敛。若对手方明确支持原 ID 幂等，重试也必须沿用原身份并验证回报。

### Q5：部分成交后如何精确释放资金？

以唯一成交事实更新 `CumQty`，计算剩余 `L=OrderQty-CumQty-CanceledQty`，买单冻结为 `L×price_cap+remaining_fee_buffer`，卖单冻结为剩余证券量；释放/补足写 reservation event，与订单状态、成交和 Outbox 同一事务。不能按“订单结束”一次性释放，也不能按回报到达次数释放。

### Q6：如何防止算法超量发单？

母单行锁/乐观版本下原子 claim `reserved_to_send`，新子单上限为 `Q-filled-working-reserved`；发送成功转 `working`，拒单释放 claim，成交减少 working 增加 filled。重启从数据库重建，不从定时器内存恢复；任何不变量破坏立即停止新单。

### Q7：TWAP、冰山、POV 各自最危险的故障是什么？

TWAP 是调度延迟后 catch-up 倾倒；冰山是峰值重挂导致排队位置损失和重复 claim；POV 是成交量虚高带来的正反馈加速。三者都必须有价格/速率/名义上限、行情水位、熔断和数量守恒。

### Q8：脑裂时为什么数据库租约 TTL 不够？

网络分区可能让旧主看不到续租失败但仍保持 TCP 连接，TTL 只能表达“新主认为过期”，不一定阻止旧主发送。发送路径必须携带单调 fencing token，并由数据库/共享资源在 claim 和发送前拒绝旧 token；同时断开对手方旧会话和查询未知订单。

### Q9：`OrdStatus=FILLED` 是否可以直接记一笔全量成交？

不可以。状态是摘要，不是成交明细。必须有 `ExecType=Trade` 的 `LastQty/LastPx/ExecID` 或可信的交易所成交报告；只有最终状态没有明细时进入对账/UNKNOWN，不能凭状态创造经济事实。

### Q10：怎样证明系统没有重复记账？

保存原始 FIX 并以 `(venue, session, ExecID)` 唯一约束；成交事实、订单累计量、reservation event、账本事件使用同一事务和幂等键；重放测试重复回报、提交后崩溃、ACK 丢失；日终用 Drop Copy、持仓、现金和费用做多层对账。状态最终一致本身不能证明没有重复经济影响。

---

## 29.18 自检题

1. 交易所先发送 `CancelReject`，随后发送一笔 `Trade`，此前 OMS 已处于 `PENDING_CANCEL`。每条消息的状态、`CumQty`、冻结和可发出的下一条命令是什么？
2. 同一 `ExecID` 重传两次，第二次 raw FIX 只有 `Text(58)` 不同，如何处理？为什么不能按字段白名单忽略差异？
3. FIX 收到 `MsgSeqNum=105`，本地期待 103；此时 104 是一条业务 `ExecutionReport`，对手方又发了 `GapFill NewSeqNo=106`。你如何判断是否发生协议错误？
4. 买单总量 10,000，已成交 3,000，已取消 2,000，保护价 100，费用 buffer 500。目标冻结是多少？若 Replace 把总量改成 12,000，先检查什么？
5. 客户重复提交相同 `client_request_id`，第一次事务已提交但 HTTP 响应丢失；第二次 payload 的限价从 100 改为 101。服务应该返回什么？
6. TWAP 的 20 个时间片中，前 5 片每片 1,001 股，母单为 20,000 股、lot size=100。如何分配尾数，如何避免切片数量非法？
7. POV 观测成交量从每分钟 10,000 暴涨到 10,000,000，但行情 feed 延迟 5 秒。发单器应该加速、使用上次 volume，还是暂停？请给出理由和恢复条件。
8. 主节点 A 失去数据库租约但 FIX TCP 连接尚未断；主节点 B 获得新 fencing token。哪些检查必须在 A 的发送路径执行？
9. OMS `cum_qty=5,000`，唯一 `execution_fact` 求和为 4,000，交易所日终报告为 5,000。差异应自动补 1,000 还是进入哪类异常？
10. 为什么 `MsgSeqNum` 幂等、`ClOrdID` 幂等、`ExecID` 幂等必须同时存在？各自防的故障是什么？
11. 设计一个能在 PostgreSQL 重启后恢复的 outbox sender：如何 claim、如何避免两主发送、如何处理发送成功但 `sent_at` 未提交？
12. 解释 `OrderStatus=PARTIALLY_FILLED`、`CumQty`、`LeavesQty` 和 reservation 的关系；哪些字段是事实，哪些是投影？
13. 交易所发来同一 `ClOrdID` 的 `New` 和 `Replace`，没有 `OrigClOrdID`。你会如何结合 venue 文档、raw FIX 和订单版本恢复，而不是凭时间猜？
14. 日终后收到 Bust，账本和清算批次已经发布。为什么不能 `UPDATE execution_fact SET last_qty=0`？正确的冲正链路是什么？
15. 列出至少五个会使 POV 形成正反馈的因素，以及每个因素对应的工程护栏。

### 自检参考要点

1. 先按 `ExecID` 落成交事实，撤单拒绝不能释放；late fill 可能使状态变 `PARTIALLY_FILLED/FILLED`，然后继续等待或重新发撤单策略。2. 同 ID 不同 hash 是内容冲突，停发/人工。3. GapFill 不能跳过有业务消息的 104，需拒绝/核验。4. `L=10,000-3,000-2,000=5,000`，冻结 `500,000+500`；改单先检查新增义务和风险/版本。5. `IDEMPOTENCY_CONFLICT`，绝不改成 101。6. 先按 lot 量化，再按确定性余数分配。7. 暂停/降级，等待水位恢复并通过异常检测。8. A 每次 claim/send 验 token，旧 token 拒绝；B 先查询未决订单。9. `MISSING_INTERNAL_EXEC` 的证据冲突，不能自动创造成交。10–15 需能分别说清序号、命令、成交事实和追加冲正边界。

---

## 29.19 小结：一套可以被重放、对账和停机恢复的执行边界

生产级 OMS/FIX 的核心不是把字段映射完成，而是建立以下可证明链路：

```text
稳定命令身份（client_request_id / command_id / ClOrdID）
  → 事务订单版本、冻结预留和 Outbox
  → 单一 fencing token 的 FIX 发送
  → 会话序号持久化、补包与 GapFill
  → 原始 ExecutionReport 不可变落库
  → ExecID 幂等成交事实
  → 状态、CumQty、LeavesQty、平均价和 reservation 同事务归并
  → TWAP/冰山/POV 按 claim 守恒发出子单
  → Drop Copy/日终报告/现金持仓四层对账
  → UNKNOWN、冲正、更正和人工边界可解释收敛
```

必须牢记四个区分：发送不等于接受，接受不等于成交，成交不等于账本已经完成，状态终态也不等于永远不会有更正。只有把会话事实、市场事实、经济事实和状态投影分开，并用幂等、版本、唯一约束、Outbox、fencing 和对账把它们重新连接，撤改单竞争、部分成交、补包和算法重启才不会变成不可解释的资金事故。
