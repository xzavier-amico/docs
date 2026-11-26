# 30. 事前硬风控、实时保证金与强平引擎：从胖手指拦截到 ADL 收敛

> **状态**：工程篇初稿｜**所属**：第四部分·支付、券商、风控、撮合与链上交易核心架构｜**编号**：30 / 48
>
> **本章定位**：承接 [03 市场微观结构与六类角色](03-market-microstructure-and-six-roles.md)、[04 订单簿、订单类型与流动性](04-orderbook-order-types-and-liquidity.md)、[10 永续合约、资金费率与强平](10-perpetual-swaps-funding-and-liquidation.md)、[20 金融精度、舍入与分配](20-monetary-precision-rounding-and-allocation.md)、[22 复式记账核心账本引擎](22-double-entry-ledger-schema-and-concurrency.md)、[24 金融级全链路幂等](24-end-to-end-financial-idempotency.md)、[25 状态机与事务 Outbox](25-state-machines-tcc-saga-and-outbox.md) 和 [26 清结算与不停机日切](26-clearing-settlement-netting-and-cutoff.md)，把订单接入、风险计算、保证金预占和穿仓收敛做成一条可审计的交易风控管线。
>
> **覆盖范围**：事前硬风控与拒单优先级；价格带、胖手指和数量/名义/频率限制；四种自成交保护（STP）；多资产实时保证金与标记价；固定价格分桶的 O(1) 强平索引；逐级强平、保险基金与 ADL 状态机；PostgreSQL DDL、协议、Go 1.24/Java 21 核心代码；并发、延迟预算、故障切换、压测、极端边界、术语、面试答题卡、自检题和 30.8 系统衔接。
>
> **不可妥协的红线**：风控拒单必须发生在撮合前，不能成交后补救；所有价格/数量/金额使用定点整数和明确舍入版本，禁止二进制浮点；订单、风险快照、强平指令和账本事实分离且可重放；标记价不能直接信任单一最新成交；保证金冻结必须与订单接受在同一经济事实边界内；超时是 `UNKNOWN`，不是“肯定没成交”；强平不能直接 `UPDATE balance`，必须通过追加分录和状态机；保险基金和 ADL 只能按规则、版本、审计事件推进。

---

## 30.1 事故引子：一笔零填错，三条防线同时失守

### 30.1.1 事故时间线

某做市账户原本准备提交 `BTC-USDT` 的 `0.1 BTC` 限价卖单，交易员在网页输入数量时多按了三个零，发送成 `100 BTC`。当时 BTC 最新成交价约 `50,000 USDT`，盘口买一到买十的可见深度只有约 `4 BTC`。系统发生了以下事情：

| 时间 | 事件 | 错误实现 | 后果 |
|---|---|---|---|
| 09:30:00.000 | 网关收到 `100 BTC @ 49,900` | 只校验账户有无权限，未校验最大数量和名义 | 远超账户风险额度的订单进入撮合队列 |
| 09:30:00.002 | 风控查询可用余额 | 读到 20 ms 前的余额缓存 | 预占金额少于真实委托义务，出现并发超卖 |
| 09:30:00.004 | 撮合器开始扫盘 | 价格带只检查静态涨跌停，没有检查相对参考价 | 一笔异常单穿过多个价位并改变盘口中价 |
| 09:30:00.006 | 同账户另一策略报买单 | 没有 STP，自己的买卖单互相成交 | 手续费、仓位和成交量虚增，风险快照来不及更新 |
| 09:30:00.020 | 标记价消费者延迟 | 强平服务回退到 `LastPrice` | 一笔薄盘口成交触发大量多头强平 |
| 09:30:00.100 | 强平单集中发出 | 每个仓位独立查库并按当前余额计算 | 锁争用、重复强平、强平单二次自成交 |
| 09:30:01.500 | 撮合/账本网络抖动 | 超时被当成“失败”，重新生成新指令 | 外部已成交而内部重复撤单/补单，进入未知态 |
| 次日对账 | 保险基金少了一笔 | 运维直接把余额改回目标值 | 原始事故被覆盖，无法证明谁承担了滑点和手续费 |

表面上这是“胖手指错误”，真正的故障是**接受边界、经济预占、价格参考、实时风险和清算收敛没有共同的事实身份**。任何一层单独正确都不能补救其他层的越界：

```text
客户端意图
   │  schema/签名/幂等
   ▼
事前硬风控 ──拒绝/接受（不可绕过）──► 预占保证金事实
   │                                      │
   │                                      ▼
   ├─► 价格带、胖手指、频率、头寸上限       撮合器 + STP
   │                                      │
   ▼                                      ▼
实时标记价/保证金 ◄── 成交、资金费、费用 ─── 风险快照
   │
   ├─ 维持保证金充足：继续交易
   └─ 不足：逐级强平 → 保险基金 → ADL → 最终审计
```

### 30.1.2 唯一真问题

> 在订单重复、乱序、突发流量、缓存陈旧、价格插针、跨资产保证金、撮合与账本异步、进程崩溃、网络超时和大面积强平同时发生时，系统能否证明：**没有越过事前硬约束的订单进入撮合；每笔已接受委托都有且只有一份保证金义务；每个触发强平的仓位只会被接管一次；穿仓损失按公开顺序由仓位缓冲、保险基金和 ADL 收敛，而不是靠人工改余额？**

### 30.1.3 反模式与生产替代

| 反模式 | 事故后果 | 生产替代 |
|---|---|---|
| 只校验账户余额，不校验订单名义 | 单笔胖手指吞掉账户及市场风险额度 | 数量、价格、名义、保证金、频率、头寸、集中度逐层硬拦截 |
| `price * quantity` 用 `double` | 边界订单因二进制误差穿过价格带 | 资产最小单位整数 + `Decimal`/定点乘法 + 舍入版本 |
| 风控读 Redis，撮合写数据库 | 并发订单共享同一可用余额 | 风控分区单写者/原子预占，订单接受与 reservation 同事务或同序列 |
| 价格带只看最新成交 | 薄盘口插针可绕过参考价 | 指数/标记价/基准价、时间窗、波动状态和订单类型联合判定 |
| STP 只按用户 ID | 子账户/策略/主账户边界被误合并或漏拦 | 明确 `stp_group`、撮合权限域和四种动作，绑定订单版本 |
| 强平每次重新查询账户 | 同一仓位重复接管、锁争用和顺序漂移 | `LIQUIDATION_CLAIMED` 原子认领 + 风险快照版本 + 强平状态机 |
| 保险基金不足就直接改负余额 | 穿仓成本不可审计，用户与平台责任混乱 | 破产价结算、保险基金拨付、ADL 事件和不可变分录 |
| 请求超时就换新 `client_order_id` | 外部已接受但内部重复下单 | 原身份查单；`UNKNOWN` 进入恢复队列，禁止猜测 |

---

## 30.2 事前硬风控：拒单优先级、价格带、胖手指与四种 STP

### 30.2.1 订单进入撮合前的硬门

风控不是一个“风险分数”，而是按优先级短路的**硬约束集合**。拒绝原因必须稳定、可枚举、可观测；同一输入和同一风险快照重放，必须得到同一决定。

```text
原始请求
  → 认证/权限/签名/时间戳
  → schema、精度、枚举和大小校验
  → 业务身份幂等（client_order_id）
  → 市场/合约/账户状态（交易、只减仓、维护、熔断）
  → 价格、数量、名义、最小变动单位
  → 价格带与胖手指
  → 频率、订单数、撤单比、消息预算
  → 头寸、集中度、杠杆和预估初始保证金
  → 余额/抵押品/冻结/借贷额度原子预占
  → 接受订单 + reservation + outbox
```

推荐的拒单优先级如下。越靠前越便宜、越确定，且不能用后一个结果覆盖前一个结果：

| 优先级 | 检查 | 典型规则 | 失败码 |
|---:|---|---|---|
| 1 | 身份与权限 | API key、账户状态、市场权限、签名时间窗 | `AUTH_DENIED` |
| 2 | 协议合法性 | JSON/Protobuf 字段、未知枚举、请求大小、版本 | `INVALID_REQUEST` |
| 3 | 业务幂等 | `(account, client_order_id)` 已存在时只返回原决定 | `DUPLICATE_ORDER` |
| 4 | 市场状态 | `OPEN`、`HALT`、`REDUCE_ONLY`、结算中 | `MARKET_NOT_OPEN` |
| 5 | 数值边界 | `qty > 0`、价格非负、tick/lot 对齐 | `INVALID_TICK_SIZE` |
| 6 | 价格与名义 | 价格带、最大订单名义、最小名义、胖手指 | `PRICE_BAND` / `FAT_FINGER` |
| 7 | 流量 | 账户/策略/IP/市场的 token bucket 和撤单比 | `RATE_LIMITED` |
| 8 | 仓位 | 最大净仓、最大总仓、方向限制、风险档位 | `POSITION_LIMIT` |
| 9 | 保证金 | 预估成交后 IM、费用、冲击成本、保留金 | `INSUFFICIENT_MARGIN` |
| 10 | 原子预占 | reservation 版本冲突或风险分区不可用 | `RISK_BUSY` / `RISK_UNAVAILABLE` |

安全上宁可拒绝多一些订单，也不能在风控分区不可证明一致时放行。对于不影响经济安全的展示性数据（例如行情延迟），可以降级；对于余额、风险档位、订单接受和强平触发，默认 fail-closed。

### 30.2.2 价格带：参考价不是一个数，而是带状态的策略

给市场 `m` 定义一个经过版本化的参考价 `R`。`R` 可以是指数价、上一结算价、标记价、盘口中价的时间窗组合，但必须在订单接受时记录来源和版本：

```text
R = ReferencePrice(index, mark, last, book_mid, volatility_state, as_of)
buy_limit_price  ≤ R × (1 + up_band(state, side, order_type))
sell_limit_price ≥ R × (1 - down_band(state, side, order_type))
```

生产实现应区分至少四类带：

1. **静态保护带**：相对上一结算价/指数价的最大绝对偏离，用于防明显输错价格。
2. **动态波动带**：根据短窗波动、盘口深度和交易状态扩大或收窄；不能因单笔异常成交瞬间放大。
3. **撮合可成交带**：检查订单能否在允许的保护价格内成交；市价单必须转成带 `slippage_cap` 的保护市价单，禁止“无限市价”。
4. **风险/清算带**：强平单可以有专用的接管规则，但不能因此让普通用户订单绕过价格带。

**跨越价（stop/trigger）与委托价分开**：触发价使用 `mark/index/last` 中明确的一种，触发后产生的订单仍要经过数量、名义和保护价检查。`reduce_only` 也不能绕过数量、价格带、频率和账户状态，只能放宽“不能增加净风险”的方向检查。

### 30.2.3 胖手指：静态值不够，必须有多维限额

胖手指不是只判断 `qty > max_qty`。应至少同时检查：

```text
notional = round_price(price) × round_qty(qty)
post_position = current_position + signed_fill_bound(order)
worst_margin = IM(post_position, worst_allowed_price) + fee_reserve + slippage_reserve
```

| 维度 | 例子 | 说明 |
|---|---:|---|
| 单笔数量 | `max_qty=10 BTC` | 按合约 lot size 对齐后检查 |
| 单笔名义 | `max_notional=500,000 USDT` | 对限价和保护市价都检查 |
| 账户总名义 | `max_gross_notional=2,000,000` | 多空毛名义，不允许用净额掩盖集中风险 |
| 价格偏离 | `±2%` 或动态带 | 相对版本化参考价，不是相对自己输入价 |
| 订单簿冲击 | 最大吃单层数/预计滑点 | 防止“大数量但名义刚好”穿透薄盘口 |
| 短时频率 | `N` 个新单/秒、`M` 次撤单/秒 | 按账户、API key、IP、策略和市场多层限流 |
| 撤单比 | `cancel/new` 滑窗阈值 | 对高频策略单独配置，不阻断风险平仓 |
| 组合风险 | Greeks、相关性、抵押品折扣 | 期权/跨币种账户不能只看单品种 IM |

错误提示不应泄露内部精确限额给未授权调用者；审计日志记录 `rule_id`、规则版本、参考价快照、输入和计算结果。人工提高额度必须有有效期、审批人和可回滚的 policy 版本。

### 30.2.4 四种 STP：同一自成交，四种业务后果

自成交保护作用于**同一 `stp_group`** 内的主动单和被动单。`stp_group` 不是简单的 `user_id`：可以按主账户、子账户、策略、法律实体或监管隔离域配置。每个订单携带冻结的 `stp_mode`，撮合时不能临时读取新配置改变历史决定。

假设主动买单 `T` 将与同组被动卖单 `M` 成交，四种常用模式为：

| 模式 | 全称 | 动作 | 适用/代价 |
|---|---|---|---|
| `CANCEL_TAKER` (`CN`) | Cancel Newest | 撤销主动单，保留挂单 | 保护做市队列；主动策略可能频繁被拒 |
| `CANCEL_MAKER` (`CO`) | Cancel Oldest | 撤销被动单，保留主动单继续扫盘 | 保证主动执行；会损失原有队列优先级 |
| `CANCEL_BOTH` (`CB`) | Cancel Both | 两边冲突部分都撤销 | 最保守、最易理解；会减少成交机会 |
| `DECREMENT_AND_CANCEL` (`DC`) | Decrement and Cancel | 先按冲突数量缩减主动/被动量，较小者归零并撤销 | 允许非冲突部分继续撮合；必须精确定义剩余量与优先级 |

`DC` 示例：`T=5`、`M=2`，先把 `T` 变成 `3`，`M` 变成 `0` 并撤销，`T` 可继续与下一个非同组卖单撮合。所有模式都必须：

- 在订单簿撮合线程内原子决定，不能先产生成交再补写 STP；
- 产生 `STP_PREVENTED` 事件，带两边订单 ID、冲突量、规则版本和剩余量；
- 不收取虚假成交手续费，不改变成交量、价格时间优先级和风险预占之外的余额；
- 与自成交豁免（如同一机构不同监管账户）配置分开，豁免必须可审计且不能被客户端任意声明；
- 对撤单和缩量同样幂等，重放不能再次减少订单数量。

### 30.2.5 事前决定的协议事实

订单接受返回的不是“撮合成功”，而是可重放的接受事实：

```text
OrderAccepted {
  order_id, client_order_id, account_id, stp_group,
  market, side, order_type, price_ticks, qty_lots,
  reduce_only, post_only, risk_policy_version,
  reference_price, reference_seq, reservation_id,
  risk_decision_id, accepted_seq, accepted_at
}
```

`accepted_seq` 是市场分区内的业务顺序，不等于数据库自增 ID；`reservation_id` 绑定冻结的保证金和可释放路径。订单若未产生 `OrderAccepted`，就不应进入撮合器；若已产生，则拒绝/撤单只能产生新事件，不能删除接受事实。

---

## 30.3 实时保证金与标记价：从资产快照到可用权益

### 30.3.1 余额、权益、保证金必须分开

对账户 `a`、资产 `c`、组合 `p`，推荐把以下概念明确分层：

```text
wallet_balance(c)    = 已结算资产余额
available(c)         = wallet_balance - order_reservation - transfer_hold - settlement_hold
equity               = Σ(balance_c × collateral_price_c × haircut_c)
unrealized_pnl       = Σ position_pnl(position, mark_price)
maintenance_margin   = Σ MM(position, mark_price, risk_tier)
initial_margin       = IM(open_positions + worst_case_orders)
free_margin          = equity + unrealized_pnl - initial_margin - fee_reserve - haircuts
margin_ratio         = (equity + unrealized_pnl) / maintenance_margin
```

不能把 `available` 当成 `equity`，也不能用未确认转入、待结算奖励或用户不可提取的保险额度充当抵押品。跨币种抵押品先按价格源、折扣、最大抵押额度和相关性压力测试转换；若价格源失效，采用保守折扣或暂停新增风险。

### 30.3.2 线性永续的整数公式

对 USDT 本位线性合约，数量使用最小 lot `q`，价格使用最小 tick `p`，乘法用 128 位或任意精度中间值：

```text
long_pnl  = q × (mark - entry)
short_pnl = q × (entry - mark)
notional  = q × mark
MM        = round_up(notional × mmr, quote_minor)
```

实盘计算还要扣除已实现资金费、交易费、清算费和预计平仓滑点。`mark` 变化时只更新受影响的仓位和账户分区；不能每个行情 tick 对全账户全表扫描。

### 30.3.3 标记价：防插针但不制造虚假安全

标记价是风险参考，不是用户成交价。一个可落地的组合为：

```text
Index_t = weighted_median(valid_exchanges_t)
Fair1_t = Index_t × (1 + last_funding_rate × time_to_funding / interval)
Fair2_t = Index_t + EMA(book_mid_t - Index_t, 30s)
Mark_t  = median(Fair1_t, Fair2_t, LastPrice_t_clamped)
```

实际参数必须由市场配置版本冻结，关键护栏包括：

1. 成分所先做时间戳、价格范围、签名和成交量有效性检查；过期源不能静默当成零。
2. 离群源相对中位数超过阈值时踢除或降权，剩余权重重新归一；低于最小有效源数时进入 `MARK_DEGRADED`。
3. `LastPrice` 只能在合理范围内参与 `Mark`；不能用一笔异常成交让 mark 直接跳过去触发全市场强平。
4. 标记价每次发布带 `mark_seq`、`as_of`、成分源版本、有效源数和计算规则版本。
5. 标记价回退不能回退到任意旧值：短暂延迟可使用最后有效值并启动保护，超过 TTL 则暂停新增风险、扩大风控保守边界或进入只减仓。
6. 标记价与指数价、最新价必须分别存储；成交、触发价、结算价和报表不能混用同一字段。

### 30.3.4 强平触发和破产价

对逐仓线性多仓，忽略费用的教学公式为：

```text
M0 + q × (Pliq - P0) = mmr × q × Pliq
Pliq = P0 × (1 - 1/L) / (1 - mmr)
Pbk  = P0 - M0/q
```

空仓、全仓和跨资产组合不能套用此简式。生产引擎应使用同一套 `RiskEngine` 计算：

```text
equity_after_mark - maintenance_margin_after_mark <= 0
```

并将 `risk_snapshot_id`、mark 序号、费率、折扣和风险档位一起写入触发事件。强平触发通常先取消普通挂单释放预占，再按风险规则部分减仓；不能因为“当前已经触发”就忽略新的成交、入金或资金费事件。若账户在触发后恢复，是否撤销强平必须由状态机显式规定，不能靠竞态的余额查询决定。

### 30.3.5 风险快照的最小协议

```protobuf
message RiskSnapshot {
  string snapshot_id = 1;
  string account_id = 2;
  string portfolio_id = 3;
  uint64 account_seq = 4;
  uint64 mark_seq = 5;
  string equity_minor = 6;              // 规范化 quote/抵押品单位
  string maintenance_margin_minor = 7;
  string initial_margin_minor = 8;
  string available_margin_minor = 9;
  string margin_ratio_ppm = 10;         // 定点百万分比
  string risk_policy_version = 11;
  string valuation_time = 12;
  repeated PositionRisk positions = 13;
}
```

快照是可重建的读模型，不是余额真相。真正的经济变化仍来自成交、资金费、费用、冻结/释放和清算结算分录。风控消费者必须丢弃 `account_seq` 或 `mark_seq` 逆序的快照，除非执行明确的回补协议。

---

## 30.4 价格分桶 O(1) 强平索引：认领、删除与触发

### 30.4.1 为什么不能每个 tick 扫全仓位

朴素实现是每次 mark 更新都执行：

```sql
SELECT * FROM position WHERE liq_price <= :mark AND status = 'OPEN';
```

在百万级仓位和高频价格更新下，这会重复扫描、产生数据库热点，并把“触发判断”与“强平执行”锁在一起。正确拆分为：

```text
风险计算得到 liq_tick
   → 写入 side-specific price bucket
   → mark 更新只移动触发游标/位图
   → O(1) 找到下一个非空 bucket
   → intrusive list O(1) 摘除并 CAS 认领 position
   → 异步生成 liquidation command
```

### 30.4.2 分桶定义

以市场的固定价格梯度和版本化 tick ladder 将价格映射为整数 `tick`。不能用浮点 `floor(price/tick_size)`：

```text
tick = floor(price_minor / tick_size_minor)
bucket_id = tick - ladder_min_tick
```

每个市场、保证金域和方向各有一个索引：

- `long_liq_index`：标记价向下时，`liq_tick >= current_mark_tick` 的多仓触发；按价格从高到低消费。
- `short_liq_index`：标记价向上时，`liq_tick <= current_mark_tick` 的空仓触发；按价格从低到高消费。

每个 bucket 是一个 intrusive doubly linked list，节点保存在仓位风险对象内：

```text
PositionRisk {
  position_id, side, liq_tick, index_version,
  bucket_id, prev_position_id, next_position_id,
  state, risk_snapshot_id, claim_token
}
```

插入、摘除和同桶移动均为 O(1)。为实现大跳价时快速找到下一个非空 bucket，使用两级或三级 64-bit bitmap：底层 bitmap 标记非空 bucket，上层 bitmap 标记非空 bitmap word。一次 `ctz`/`clz` 即可得到下一个非空桶；桶数固定且层级有限时，查找是有界 O(1)，不把空桶数量作为线性成本。

### 30.4.3 触发方向与一致性

假设旧 mark tick 为 `old`，新 mark tick 为 `now`：

```text
if now < old:
    long buckets in [now, old] become eligible
if now > old:
    short buckets in [old, now] become eligible
```

范围内的非空桶由 bitmap 逐个取出；一次极端跳价仍可能有很多**实际非空仓位**，因此复杂度下界是输出规模 `O(K)`，不能宣传“全路径恒定 O(1)”。准确表述是：

```text
bucket lookup / insert / remove / claim = O(1)
find-next-non-empty = bounded O(1)
mark event = O(number of triggered positions), not O(all positions)
```

必要时按市场/账户风险域分片，给单次 mark 事件设置最大处理预算；未处理的 eligible bucket 保持 `TRIGGERED_PENDING`，下一批继续，绝不能因延迟而把游标向前跳过未认领仓位。

### 30.4.4 原子认领避免重复强平

索引只负责候选定位，不代表强平已经成功。消费节点必须执行一次条件更新：

```sql
UPDATE position_risk
SET state = 'LIQUIDATION_CLAIMED',
    claim_token = :token,
    claimed_by = :worker,
    claimed_at = now(),
    version = version + 1
WHERE position_id = :id
  AND state = 'OPEN'
  AND version = :expected_version;
```

更新行数为 `1` 才能继续；为 `0` 表示已关闭、已被其他 worker 认领或版本过期，必须安全丢弃候选。claim lease 到期时不能直接把仓位改回 `OPEN`：先查询是否已有成交/外部指令结果，再按状态机进入 `RECOVERY` 或继续原 token。

### 30.4.5 风险规则变更与索引重建

`liq_tick` 依赖风险档位、费率、抵押品折扣和 tick ladder。规则变更不能在原桶上原地改值：

1. 创建 `index_version = v2`，双写新旧索引；
2. 对 OPEN 仓位按同一 mark/规则快照重算并校验计数和哈希；
3. 切换 `active_version`，记录 `RiskPolicyActivated`；
4. 旧版本只处理已认领事件，不再接受新插入；
5. 过渡期结束后回收旧索引。

索引丢失可由不可变 `PositionOpened/Adjusted/Closed` 事件重放重建。重建期间必须停止新增风险或使用保守的全量查询保护，不可悄悄放行订单。

---

## 30.5 逐级强平 → 保险基金 → ADL：可恢复状态机

### 30.5.1 强平不是一张市价单

推荐的清算泳道：

```text
OPEN
  │ margin breach at mark_seq
  ▼
TRIGGERED ──取消普通挂单/重新快照──► CLAIMED
                                      │
                         小于账户的部分减仓是否足够？
                           ├─是──► PARTIAL_LIQUIDATING
                           │          └─恢复安全 ─► RECOVERED
                           └─否──► FULL_LIQUIDATING
                                      │
                         破产价接管单成交/结算
                           ├─有剩余 ─► INSURANCE_CREDIT
                           ├─有缺口且基金足够 ─► INSURANCE_DEBIT
                           └─缺口未覆盖 ─► ADL_PENDING
                                                  │
                                      ADL 候选冻结/排序/对敲
                                        ├─足够 ─► ADL_EXECUTING ─► CLOSED
                                        └─不足/超时 ─► EMERGENCY_REVIEW
```

每个状态只允许白名单转移，转移带 `liquidation_id`、前置版本、mark/risk snapshot、原因、操作者或 worker、时间和结果哈希。`CLOSED` 不等于“余额被写成零”，而是强平仓位结算事实已经落账且后续不会再生成普通成交。

### 30.5.2 第一级：取消与逐级减仓

触发后先撤销普通挂单，释放尚未成交的初始保证金；保留 `reduce_only` 风险降低单需遵循市场策略。然后按风险档位、仓位流动性和最大冲击成本做分级减仓：

```text
target_reduce = min(position_qty,
                    qty_allowed_by_liquidity,
                    qty_needed_to_restore_margin)
```

强平单应有明确的 `liquidation_order_id` 和保护价格/执行预算。不能把仓位全部改成零再去“尝试成交”；成交量必须由撮合事实决定。部分成交后重新计算权益和维持保证金，足够安全则 `RECOVERED`；仍不足再扩大下一档。大户可以先 `IOC` 分片、跨多个流动性 venue 或进入清算专用队列，但每一步都要能查到剩余义务。

### 30.5.3 第二级：破产价与保险基金

仓位的 `Pliq` 是触发价，`Pbk` 是权益耗尽价，两者不可混淆。强平接管的会计逻辑是：

```text
execution_pnl = actual_exit_value - opening_value - fees
residual = user_margin + execution_pnl

residual > 0  → 按规则转入 insurance fund 或退回（取决于产品协议）
residual = 0  → 仓位结清
residual < 0  → deficit = -residual，申请 insurance fund
```

保险基金操作必须分别记录 `INSURANCE_CREDIT`、`INSURANCE_DEBIT`、资产、金额、清算批次和审批/规则版本。基金不能被当成平台收入，也不能在数据库中直接 `fund.balance = fund.balance - deficit`；应追加复式分录并由余额投影重建。设置最小安全水位、单市场暴露上限和基金不可用时的 ADL 触发阈值。

### 30.5.4 第三级：ADL 排名与对敲

保险基金不足时，ADL 只在协议允许的对手方集合内执行。候选至少满足：方向相反、同一产品/结算资产、持仓为盈利、未被风控冻结、未处于强平中。一个常见评分为：

```text
pnl_pct       = unrealized_pnl / entry_notional
effective_lev = abs(mark_notional) / max(equity, epsilon)
adl_score     = pnl_pct × effective_lev
```

排序规则、并列打破规则和上限必须版本化。ADL 候选先冻结可平数量，生成 `ADL_NOTICE`，给用户和监控系统可见的原因；真正对敲使用破产仓位的 `Pbk` 或协议指定价，而不是任意瞬时 mark。ADL 对手方获得的是协议定义的结算结果，不是平台自由决定的价格。

### 30.5.5 ADL 状态机不变量

1. 一个 `position_id` 同时只能有一个有效 `liquidation_id`。
2. `INSURANCE_DEBIT` 的金额不超过当时基金可用余额，资金不足必须进入 `ADL_PENDING`，不能产生负基金余额，除非协议明示允许平台垫资科目。
3. ADL 候选冻结总量不超过其可平仓位；同一候选被两个 deficit 批次看到时，用版本 CAS 或候选 reservation 解决。
4. 任何状态重试使用同一个 `liquidation_id`、`claim_token` 和 `idempotency_key`；不能用新订单号掩盖未知成交。
5. 强平、保险基金、ADL 的账本分录按资产分别平衡，且能由 `liquidation_event` 和成交明细反算。
6. 交易暂停或撮合器故障时，风控可以继续冻结和标记 `TRIGGERED`，但不能宣称已完成清算；恢复后按序列重放。

---

## 30.6 DDL、协议与核心代码

### 30.6.1 PostgreSQL DDL：事实、快照、索引和状态

以下 DDL 是最小骨架；金额和数量实际可用 `BIGINT`（若单位范围足够）或拆为 `NUMERIC`，但不能混用未声明的精度。高写入表按 `market_id + business_date` 或时间分区，历史事实不可更新删除。

```sql
CREATE TYPE order_status AS ENUM
  ('RECEIVED','ACCEPTED','PARTIALLY_FILLED','FILLED','CANCEL_PENDING',
   'CANCELED','REJECTED','UNKNOWN');
CREATE TYPE position_state AS ENUM
  ('OPEN','TRIGGERED','LIQUIDATION_CLAIMED','PARTIAL_LIQUIDATING',
   'FULL_LIQUIDATING','INSURANCE_PENDING','ADL_PENDING','ADL_EXECUTING',
   'RECOVERED','CLOSED','RECOVERY');
CREATE TYPE liquidation_state AS ENUM
  ('TRIGGERED','CLAIMED','CANCELING','PARTIAL','FULL','INSURANCE_PENDING',
   'ADL_PENDING','ADL_EXECUTING','SETTLED','FAILED','UNKNOWN');

CREATE TABLE order_fact (
  order_id              UUID PRIMARY KEY,
  account_id            TEXT NOT NULL,
  market_id             TEXT NOT NULL,
  client_order_id       TEXT NOT NULL,
  stp_group              TEXT NOT NULL,
  stp_mode               TEXT NOT NULL CHECK (stp_mode IN ('CN','CO','CB','DC')),
  side                   TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
  order_type             TEXT NOT NULL,
  price_ticks            BIGINT,
  qty_lots               BIGINT NOT NULL CHECK (qty_lots > 0),
  remaining_lots         BIGINT NOT NULL CHECK (remaining_lots >= 0),
  reduce_only            BOOLEAN NOT NULL DEFAULT FALSE,
  risk_policy_version    TEXT NOT NULL,
  reference_price_minor  NUMERIC(38, 0) NOT NULL,
  reference_seq          BIGINT NOT NULL,
  reservation_id         UUID NOT NULL,
  accepted_seq           BIGINT,
  status                 order_status NOT NULL,
  version                BIGINT NOT NULL DEFAULT 0,
  created_at             TIMESTAMPTZ NOT NULL,
  updated_at             TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX uq_order_client
  ON order_fact(account_id, client_order_id);
CREATE UNIQUE INDEX uq_order_accept_seq
  ON order_fact(market_id, accepted_seq) WHERE accepted_seq IS NOT NULL;

CREATE TABLE risk_snapshot (
  snapshot_id            UUID PRIMARY KEY,
  account_id             TEXT NOT NULL,
  portfolio_id           TEXT NOT NULL,
  account_seq            BIGINT NOT NULL,
  mark_seq               BIGINT NOT NULL,
  mark_price_minor       NUMERIC(38, 0) NOT NULL,
  equity_minor           NUMERIC(38, 0) NOT NULL,
  maintenance_minor      NUMERIC(38, 0) NOT NULL CHECK (maintenance_minor >= 0),
  initial_minor          NUMERIC(38, 0) NOT NULL CHECK (initial_minor >= 0),
  available_minor        NUMERIC(38, 0) NOT NULL,
  policy_version         TEXT NOT NULL,
  snapshot_hash          BYTEA NOT NULL,
  created_at             TIMESTAMPTZ NOT NULL,
  UNIQUE(account_id, account_seq, mark_seq, policy_version)
);

CREATE TABLE position_risk (
  position_id            UUID PRIMARY KEY,
  account_id             TEXT NOT NULL,
  market_id              TEXT NOT NULL,
  side                   TEXT NOT NULL CHECK (side IN ('LONG','SHORT')),
  qty_lots               BIGINT NOT NULL CHECK (qty_lots > 0),
  entry_price_ticks      BIGINT NOT NULL,
  liq_tick               BIGINT NOT NULL,
  bankruptcy_tick        BIGINT NOT NULL,
  index_version           TEXT NOT NULL,
  bucket_id              BIGINT NOT NULL,
  prev_position_id       UUID,
  next_position_id       UUID,
  state                  position_state NOT NULL,
  version                BIGINT NOT NULL DEFAULT 0,
  claim_token            UUID,
  claimed_by             TEXT,
  claimed_at             TIMESTAMPTZ,
  updated_at             TIMESTAMPTZ NOT NULL
);
CREATE INDEX ix_position_trigger
  ON position_risk(market_id, side, index_version, liq_tick)
  WHERE state IN ('OPEN','TRIGGERED');

CREATE TABLE liquidation (
  liquidation_id         UUID PRIMARY KEY,
  position_id            UUID NOT NULL REFERENCES position_risk(position_id),
  account_id             TEXT NOT NULL,
  market_id              TEXT NOT NULL,
  state                  liquidation_state NOT NULL,
  claim_token            UUID NOT NULL,
  risk_snapshot_id       UUID NOT NULL,
  trigger_mark_seq       BIGINT NOT NULL,
  bankruptcy_tick        BIGINT NOT NULL,
  requested_lots         BIGINT NOT NULL,
  executed_lots          BIGINT NOT NULL DEFAULT 0,
  deficit_minor          NUMERIC(38, 0) NOT NULL DEFAULT 0,
  version                BIGINT NOT NULL DEFAULT 0,
  created_at             TIMESTAMPTZ NOT NULL,
  updated_at             TIMESTAMPTZ NOT NULL,
  UNIQUE(position_id)
);

CREATE TABLE liquidation_event (
  event_id               BIGSERIAL PRIMARY KEY,
  liquidation_id         UUID NOT NULL REFERENCES liquidation(liquidation_id),
  from_state             liquidation_state,
  to_state               liquidation_state NOT NULL,
  idempotency_key        TEXT NOT NULL,
  payload_hash           BYTEA NOT NULL,
  actor                  TEXT NOT NULL,
  created_at             TIMESTAMPTZ NOT NULL,
  UNIQUE(liquidation_id, idempotency_key)
);

CREATE TABLE insurance_ledger (
  entry_id               BIGSERIAL PRIMARY KEY,
  liquidation_id         UUID NOT NULL,
  asset                  TEXT NOT NULL,
  signed_amount_minor    NUMERIC(38, 0) NOT NULL,
  reason                 TEXT NOT NULL,
  policy_version         TEXT NOT NULL,
  created_at             TIMESTAMPTZ NOT NULL
);
```

`order_fact` 是接受事实，不能用它代替成交表；`risk_snapshot` 是估值快照，不能直接改余额；`liquidation_event` 记录状态事实，清算资金变化仍应进入 [22] 的不可变 posting/ledger。生产表还应增加租户/法律实体、审计操作者、消息序列和分区策略。

### 30.6.2 Go 1.24：定点风控、STP 与 O(1) 桶索引

下面代码展示核心边界；真实服务要补充权限、数据库事务、日志、指标和故障恢复。`int64` 乘法前必须检查溢出，示例用 `math/big` 表达“中间结果不能丢精度”的原则。

```go
package risk

import (
    "context"
    "errors"
    "math/big"
    "sync"
)

type Side uint8
const ( Buy Side = iota; Sell )
type STP uint8
const ( CancelTaker STP = iota; CancelMaker; CancelBoth; DecrementAndCancel )

type Order struct {
    ID, STPGroup string
    Side         Side
    PriceTicks   int64
    QtyLots      int64
    STPMode      STP
}

type Decision struct { Accepted bool; Code string; ReservationID string }

type Limits struct { MaxLots, MaxNotional, MinPrice, MaxPrice int64 }

// Price/quantity are already normalized to integer ticks/lots.
func PreTrade(ctx context.Context, o Order, ref int64, l Limits,
    available, feeReserve int64) Decision {
    if o.QtyLots <= 0 { return Decision{Code: "INVALID_QTY"} }
    if o.PriceTicks < l.MinPrice || o.PriceTicks > l.MaxPrice {
        return Decision{Code: "PRICE_BAND"}
    }
    if o.QtyLots > l.MaxLots {
        return Decision{Code: "FAT_FINGER"}
    }
    n := new(big.Int).Mul(big.NewInt(o.PriceTicks), big.NewInt(o.QtyLots))
    if !n.IsInt64() || n.Int64() > l.MaxNotional {
        return Decision{Code: "MAX_NOTIONAL"}
    }
    if available < n.Int64()+feeReserve {
        return Decision{Code: "INSUFFICIENT_MARGIN"}
    }
    // The caller must atomically persist an accepted fact and reservation.
    return Decision{Accepted: true, Code: "ACCEPTED"}
}

type STPResult struct { TakerQty, MakerQty int64; TakerCancel, MakerCancel bool }
func ApplySTP(taker, maker Order) STPResult {
    if taker.STPGroup == "" || taker.STPGroup != maker.STPGroup {
        return STPResult{TakerQty: taker.QtyLots, MakerQty: maker.QtyLots}
    }
    switch taker.STPMode {
    case CancelTaker: return STPResult{MakerQty: maker.QtyLots, TakerCancel: true}
    case CancelMaker: return STPResult{TakerQty: taker.QtyLots, MakerCancel: true}
    case CancelBoth: return STPResult{TakerCancel: true, MakerCancel: true}
    case DecrementAndCancel:
        if taker.QtyLots <= maker.QtyLots {
            return STPResult{MakerQty: maker.QtyLots - taker.QtyLots,
                TakerCancel: true, MakerCancel: taker.QtyLots == maker.QtyLots}
        }
        return STPResult{TakerQty: taker.QtyLots - maker.QtyLots, MakerCancel: true}
    default: panic("unknown STP mode")
    }
}

type Node struct { PositionID string; Prev, Next *Node; Bucket int; Claimed bool }
type Bucket struct { Head, Tail *Node; NonEmpty bool }
type Index struct { mu sync.Mutex; Buckets []Bucket; words []uint64 }

func (x *Index) Put(n *Node, bucket int) error {
    x.mu.Lock(); defer x.mu.Unlock()
    if bucket < 0 || bucket >= len(x.Buckets) { return errors.New("bucket out of range") }
    b := &x.Buckets[bucket]
    n.Bucket, n.Prev, n.Next = bucket, b.Tail, nil
    if b.Tail != nil { b.Tail.Next = n } else { b.Head = n }
    b.Tail, b.NonEmpty = n, true
    x.words[bucket>>6] |= uint64(1) << uint(bucket&63)
    return nil
}

// PopHead is O(1); bitmap cursor selection is bounded O(1) in the real index.
func (x *Index) PopHead(bucket int) *Node {
    x.mu.Lock(); defer x.mu.Unlock()
    if bucket < 0 || bucket >= len(x.Buckets) { return nil }
    b := &x.Buckets[bucket]; n := b.Head; if n == nil { return nil }
    b.Head = n.Next; if b.Head == nil { b.Tail, b.NonEmpty = nil, false; x.words[bucket>>6] &^= uint64(1)<<uint(bucket&63) } else { b.Head.Prev = nil }
    n.Prev, n.Next, n.Claimed = nil, nil, true
    return n
}

type ClaimStore interface { Claim(context.Context, string, string) (bool, error) }
func ClaimLiquidation(ctx context.Context, s ClaimStore, n *Node, token string) error {
    if n == nil { return nil }
    ok, err := s.Claim(ctx, n.PositionID, token) // SQL UPDATE ... WHERE state=OPEN AND version=...
    if err != nil { return err }; if !ok { return nil } // stale candidate: safe drop
    return nil
}
```

代码中的内存锁只保护单进程索引；多进程必须按市场分区单写者，或把 claim CAS 放在共享存储中。索引不是最终一致性账本，重启时从 position 事件重建，不能把丢失的内存节点当作仓位已安全。

### 30.6.3 Java 21：标记价、保证金和强平状态转移

Java 风控服务可以使用 `long` 表示最小单位，乘法用 `Math.multiplyExact`/`BigInteger`，不要让 `double` 进入边界判断。以下代码展示不可变快照和白名单转移：

```java
package fin.risk;

import java.math.BigInteger;
import java.time.Instant;
import java.util.Comparator;
import java.util.EnumMap;
import java.util.Map;

record Mark(long index, long fair1, long fair2, long last, long seq,
            String policyVersion, Instant asOf) {
    static Mark of(long index, long fair1, long fair2, long last,
                   long seq, String version, Instant at) {
        long[] a = {fair1, fair2, last};
        java.util.Arrays.sort(a);
        return new Mark(index, fair1, fair2, a[1], seq, version, at);
    }
}

record Position(long qtyLots, long entryTicks, boolean longSide,
                long markTicks, long mmrPpm) {
    BigInteger pnl() {
        long diff = longSide ? markTicks - entryTicks : entryTicks - markTicks;
        return BigInteger.valueOf(qtyLots).multiply(BigInteger.valueOf(diff));
    }
    BigInteger maintenance() {
        BigInteger notional = BigInteger.valueOf(qtyLots)
                .multiply(BigInteger.valueOf(markTicks));
        return notional.multiply(BigInteger.valueOf(mmrPpm))
                .divide(BigInteger.valueOf(1_000_000));
    }
}

record Margin(BigInteger equity, BigInteger initial, BigInteger maintenance) {
    boolean liquidationRequired() {
        return equity.subtract(maintenance).signum() <= 0;
    }
}

enum LiqState { TRIGGERED, CLAIMED, CANCELING, PARTIAL, FULL,
                INSURANCE_PENDING, ADL_PENDING, ADL_EXECUTING, SETTLED, UNKNOWN }
record LiqEvent(String id, LiqState from, LiqState to, long expectedVersion,
                long markSeq, String snapshotId, String idempotencyKey) {}

final class LiqMachine {
    private static final Map<LiqState, java.util.Set<LiqState>> ALLOWED =
        Map.of(
          LiqState.TRIGGERED, java.util.Set.of(LiqState.CLAIMED, LiqState.UNKNOWN),
          LiqState.CLAIMED, java.util.Set.of(LiqState.CANCELING, LiqState.UNKNOWN),
          LiqState.CANCELING, java.util.Set.of(LiqState.PARTIAL, LiqState.FULL, LiqState.UNKNOWN),
          LiqState.PARTIAL, java.util.Set.of(LiqState.SETTLED, LiqState.FULL,
                                             LiqState.INSURANCE_PENDING),
          LiqState.FULL, java.util.Set.of(LiqState.INSURANCE_PENDING,
                                          LiqState.SETTLED, LiqState.UNKNOWN),
          LiqState.INSURANCE_PENDING, java.util.Set.of(LiqState.SETTLED,
                                                       LiqState.ADL_PENDING),
          LiqState.ADL_PENDING, java.util.Set.of(LiqState.ADL_EXECUTING),
          LiqState.ADL_EXECUTING, java.util.Set.of(LiqState.SETTLED,
                                                   LiqState.UNKNOWN),
          LiqState.SETTLED, java.util.Set.of(), LiqState.UNKNOWN,
          java.util.Set.of(LiqState.CLAIMED, LiqState.INSURANCE_PENDING,
                           LiqState.ADL_PENDING));

    static LiqEvent transition(LiqEvent prior, LiqState next) {
        if (!ALLOWED.getOrDefault(prior.to(), java.util.Set.of()).contains(next))
            throw new IllegalStateException("illegal liquidation transition");
        return new LiqEvent(prior.id(), prior.to(), next, prior.expectedVersion()+1,
                prior.markSeq(), prior.snapshotId(), prior.idempotencyKey()+":"+next);
    }
}

final class AdlRank {
    static BigInteger score(long pnlPpm, long markNotional, long equity) {
        // PnL% and effective leverage are both fixed-point; denominator checked.
        if (equity <= 0) return BigInteger.ZERO;
        return BigInteger.valueOf(pnlPpm).multiply(BigInteger.valueOf(markNotional))
                .divide(BigInteger.valueOf(equity));
    }
}
```

`Mark.of` 只是示范中位数，生产还必须先验证成分源并完成离群剔除、时间窗和最小源数检查。Java 状态机把 `UNKNOWN` 保留在显式图中，调用方必须有查单/恢复 worker，而不是捕获异常后默认 `FAILED`。

### 30.6.4 订单/风险/清算协议约定

内部协议最少带以下字段，任何跨服务调用都不得只传 `account_id` 和 `amount`：

```json
{
  "event_type": "LiquidationTriggered",
  "event_id": "evt-uuid",
  "idempotency_key": "liq-id:trigger:mark-seq",
  "market_id": "BTC-USDT-PERP",
  "account_id": "acct-42",
  "position_id": "pos-uuid",
  "liquidation_id": "liq-uuid",
  "risk_snapshot_id": "snap-uuid",
  "mark_seq": 9812231,
  "mark_price_ticks": 5000000,
  "bankruptcy_price_ticks": 4490000,
  "policy_version": "risk-2026-11-25.3",
  "state_version": 4,
  "causation_id": "mark-event-id",
  "correlation_id": "order-or-risk-flow-id",
  "occurred_at": "2026-11-25T09:30:00.123Z"
}
```

消费者必须：验证 schema 和版本、检查前置状态/版本、以 `idempotency_key` 去重、提交经济事实和 outbox 后 ACK。消息顺序只在同一 `position_id`/`liquidation_id` 分区内有意义；跨市场不应依赖全局 Kafka 顺序。事件发布至少一次，消费者幂等；不能承诺跨数据库和撮合器的 exactly-once。

---

## 30.7 并发、延迟预算、故障切换与压测

### 30.7.1 分区和锁边界

推荐按 `market_id` 将撮合、订单序列、价格分桶和 mark 事件分区；按 `account_id` 将余额/仓位/保证金预占做单写者或串行 mailbox。二者之间不要用分布式锁实现每笔成交的全局串行：

```text
Market partition: order_seq → price-time matching → fill events
Account partition: balance_seq → reservation/position/risk snapshot
Risk index: market-side bucket → candidate → account CAS claim
Ledger: immutable posting transaction + outbox
```

同一账户跨市场下单需要账户风险分区的原子 reservation；可用两种方案：

1. **账户单写者**：所有风险影响事件进入账户 mailbox，顺序计算并返回 token，延迟低且最易证明。
2. **乐观版本预占**：`available_version` CAS，失败重算重试；设置有限重试次数，超过则 `RISK_BUSY`，不能无限自旋。

订单簿锁不应包住网络调用、数据库慢查询或账本 RPC。撮合线程只提交确定的 fill intent；持久化和下游发布通过同分区序列和 outbox 完成。

### 30.7.2 典型延迟预算（示例目标）

以下是内部生产目标，不是对所有市场的承诺；低流动性市场可配置不同预算，但必须监控 p50/p95/p99 和超预算动作：

| 阶段 | p99 预算 | 说明 |
|---|---:|---|
| 网关认证、schema、幂等查 | 100 µs | 内存路由，慢请求不进入撮合 |
| 事前价格/数量/频率硬门 | 100 µs | 规则预加载，不能同步查多个远端服务 |
| 账户 reservation | 300 µs | 分区单写者或本地原子版本 |
| 接受事实入队 | 100 µs | 序列化事件，不等待下游账本 |
| 撮合到 fill intent | 200 µs | 同市场单线程/低锁路径 |
| mark 计算与发布 | 50 ms | 依赖行情源和窗口；超过 TTL 进入保护 |
| mark 到 liquidation candidate | 5 ms | bitmap + 分区队列，非全表扫描 |
| candidate 到 claim | 20 ms | CAS/事务提交，重复安全 |
| 强平指令发出 | 50 ms | 保护价、分片和限速；不是已成交保证 |
| 风险快照/账本最终可见 | 500 ms～数秒 | 异步但有水位、告警和补偿 |

风控服务的正确性优先级高于延迟 SLA：若不能在预算内得到一致风险结果，应拒绝新增风险或只允许减少风险，不得以旧快照无期限放行。

### 30.7.3 故障切换与未知态

| 故障 | 可继续做 | 必须停止/降级 | 恢复动作 |
|---|---|---|---|
| 行情源少于最小有效数 | 使用最后有效 mark 做有限时间保护 | 新增风险、自动扩大价格带 | 补齐源后按 seq 回补，记录 mark gap |
| 风控分区主节点故障 | 读只读快照、处理减仓/撤单 | 新开仓和增加保证金义务 | 新主从 WAL/事件恢复，校验 account_seq |
| 撮合器不可用 | 撤单请求入队、冻结风险 | 宣称新订单已成交 | 用 market_seq 和 fill log 重放，逐笔查单 |
| 账本/Outbox 不可用 | 内存不应继续产生不可恢复经济事实 | 强平结算、资金费确认、接受需持久化订单 | 恢复后从接受/成交事实补写，幂等提交 |
| 清算 worker 崩溃 | 其他 worker 用同 token 查询恢复 | 新建同仓位新 liquidation_id | lease 到期查成交，再续租或进入 UNKNOWN |
| 保险基金服务不可用 | 标记 deficit、冻结后续风险 | 假设已拨款或直接 ADL | 先恢复账本，再按原批次推进 |

所有“已发送但无回执”的订单、撤单和强平指令都进入 `UNKNOWN`。恢复顺序是：原请求查单 → 读取撮合事实/市场序列 → 对比本地 outbox → 补写缺失事件；禁止生成新身份重发以“碰碰运气”。

### 30.7.4 压测模型与验收指标

压测不能只有吞吐量，应把风控边界和事故注入一起测：

```text
订单流：正常订单 + 胖手指 + 边界 tick/lot + 重复 client_order_id
行情流：单 tick、大跳价、回退、乱序、迟到、单所离群、源全失效
账户流：同时入金、撤单、成交、资金费、跨市场下单、重复回调
清算流：1%/10%/50% 仓位触发，保险基金充足/不足，ADL 候选重复
故障注入：进程 kill、GC pause、网络丢包、DB commit 后断连、Kafka 重平衡
```

必须验证的性质：

1. 任意 `client_order_id` 最多一个接受事实和一个 reservation；重放只返回原决定。
2. 任何拒绝订单都没有成交事实、保证金占用或 outbox 的接受事件。
3. 任意 STP 模式下，同组订单没有成交量，非冲突剩余量符合协议。
4. 任何时刻 `available >= 0`（按资产/法律实体分别计算），不可用数据不能绕过预占。
5. 每个 `position_id` 最多一个活跃 liquidation；重复 mark 不重复 claim。
6. 强平批次的仓位量、成交量、保险基金分录、ADL 对敲量可以逐项勾稽。
7. mark 乱序/重复不产生逆序风险快照；价格分桶重建前后候选集合哈希一致。
8. commit 成功而 ACK 丢失、ACK 成功而下游重试、worker 崩溃三类故障均可重放收敛。

建议验收指标包括：订单接受 p99、硬拒单错误分布、reservation 冲突率、mark 延迟与有效源数、bucket 触发 lag、claim 冲突率、强平从 trigger 到 settle 的 p99、保险基金余额水位、ADL 候选冻结时长、未知态年龄、事实重放差异数和账本平衡差异数。每个指标都要按市场、账户风险档位、规则版本切分。

### 30.7.5 断言、巡检与灾备演练

在线断言：

```text
accepted_order => reservation_exists
reservation_released_or_consumed exactly once
position.state in LIQUIDATING => active_liquidation exists
settled_liquidation => no open position quantity for its claim token
insurance_debit <= available_fund_at_ledger_commit
event.version is monotonic per aggregate
```

每日巡检从原始事件重建：订单接受数、成交数、仓位数量、冻结/释放总额、保险基金余额、ADL 对敲量；与投影逐项比对。灾备演练至少覆盖“主库提交后机房断电”“mark 源全断 10 分钟”“强平峰值超过正常 100 倍”“旧规则索引重建中切主”四类场景，并保存演练批次和差异报告。

---

## 30.8 极端边界、术语、面试答题卡、自检题与系统衔接

### 30.8.1 极端边界清单

| 边界 | 正确处理 |
|---|---|
| 价格正好等于上下价格带 | 明确定义开闭区间；建议 `lower <= price <= upper` 接受，代码和测试一致 |
| 数量正好等于最大 lot | 先按 lot 归一，再比较；不得因 `qty+epsilon` 误拒或放行 |
| `price × qty` 溢出 64 位 | 在乘法前检查或使用大整数，拒绝 `NUMERIC_OVERFLOW` |
| 标记价等于强平价 | 使用协议规定的 `<=`/`>=`，边界测试必须覆盖两侧一个 tick |
| mark 从 50,000 跳到 1 | 只处理实际非空候选；预算耗尽时保持 pending，不跳游标 |
| 账户触发后同时入金 | 由同一账户序列决定先后；入金事实不能被静默丢弃，是否恢复强平由策略明确 |
| 触发后订单成交 | fill 按市场序列进入账户风险重算；旧 liquidation 不能无条件覆盖新仓位 |
| 保险基金刚好等于 deficit | 允许精确归零并进入 settled；下一个 deficit 必须进入 ADL，不允许负数 |
| ADL 候选收益刚好为零 | 不得作为盈利候选；进入下一候选或剩余缺口处理 |
| 反向合约价格接近零 | 公式和价格梯度必须有产品下限，禁止除零；转入保护/暂停 |
| 币种精度不同 | 逐资产最小单位计算，不能把 BTC satoshi 和 USDT cent 直接相加 |
| 资金费扣款与强平同一序列 | 绑定 `account_seq` 和结算 epoch；不能按消费者到达顺序臆断 |
| 规则版本切换时触发 | 事件带 policy/index version；旧仓位按迁移协议，不能一半新一半旧 |
| 成交回报超时 | `UNKNOWN` 查单；不能自动换 `order_id` 重发 |
| 客户重复撤单 | 按原 `order_id` 幂等返回；撤单不是对已成交量的逆操作 |
| 只有一个指数源存活 | 依据市场配置进入 degraded/只减仓，不能假装中位数仍可靠 |
| 浮点格式化后比较 | 禁止；字符串金额先解析成整数并校验精度/舍入方向 |

### 30.8.2 术语表

| 术语 | 含义 |
|---|---|
| `IM` | Initial Margin，初始保证金；用于承担新增/委托风险 |
| `MM` | Maintenance Margin，维持保证金；用于判断仓位是否接近/进入清算 |
| `Equity` | 抵押品价值加已实现/未实现损益后的账户权益，不等于钱包可用余额 |
| `Mark Price` | 由指数、合理价格和最新成交组合得到的风险参考价 |
| `Index Price` | 多个外部现货源经有效性和离群处理后的指数价 |
| `Pliq` | 触发强平的价格；通常仍留有费用和滑点缓冲 |
| `Pbk` | 破产价；仓位权益按协议耗尽的价格 |
| `STP` | Self-Trade Prevention，自成交保护 |
| `Fat Finger` | 防止数量、价格、名义或冲击远超合理范围的事前硬限制 |
| `Reservation` | 与订单接受绑定的保证金/额度预占事实 |
| `Claim` | 清算 worker 对某仓位的原子认领，不等于已经成交 |
| `Insurance Fund` | 用强平残余和规则资金覆盖穿仓缺口的隔离基金 |
| `ADL` | Auto-Deleveraging，保险基金不足时对高风险盈利对手方自动减仓 |
| `Risk Snapshot` | 给定账户序列、mark 序列和规则版本的可重建风险估值快照 |
| `Trigger Cursor` | 价格变化时消费已越过强平阈值的桶的游标 |
| `UNKNOWN` | 外部结果尚不可知的状态，必须查单/恢复，不等于失败 |
| `Fail-closed` | 无法证明安全时拒绝新增风险或只允许减风险 |

### 30.8.3 面试答题卡

**问：为什么事前风控不能放到撮合成交后？**
答：成交后再拒绝会产生不可逆的对手方义务、仓位和账本事实；正确边界是接受订单前完成权限、价格带、胖手指、头寸、保证金和原子 reservation。成交后的检查只能用于监控或触发减仓，不能替代硬门。

**问：价格带、涨跌停和标记价有什么区别？**
答：价格带是订单接受边界，防用户输错和异常订单；涨跌停/熔断是市场交易状态；标记价是未实现盈亏和强平的估值参考。三者来源、时间窗和审计字段不同，不能用最新成交价替代全部。

**问：四种 STP 怎么选？**
答：`CN` 保留 maker 队列、取消主动单；`CO` 让主动单继续、取消旧 maker；`CB` 最保守地取消双方；`DC` 只消除冲突量、保留非冲突量。关键不在名字，而在同一 stp_group、撮合线程原子执行、剩余量和事件可重放。

**问：怎样做到 O(1) 强平索引？**
答：价格转整数 tick，仓位挂入 side-specific bucket 的 intrusive list，插入/摘除/认领为 O(1)，bitmap 找非空桶为有界 O(1)。大跳价的总成本仍是触发仓位数 O(K)，不能声称全量事件恒定时间；索引只定位候选，最终用版本 CAS claim。

**问：强平价和破产价为什么要分开？**
答：强平价在权益跌至维持保证金时触发，破产价是权益耗尽点；中间缓冲用于费用、滑点和接管。强平按 Pliq 触发但通常按 Pbk/协议价结算，混用会错误计算保险基金和用户残余。

**问：保险基金耗尽后为什么不能直接把所有用户按比例扣钱？**
答：那是另一种社会化损失协议，必须在产品合同和规则中明确。当前 ADL 方案只对符合方向、盈利和有效杠杆条件的候选排序，按版本化规则冻结和对敲；所有影响进入不可变账本和通知。

**问：分布式系统能否保证订单和强平 exactly-once？**
答：网络层不能。工程上保证的是稳定身份、至少一次事件、幂等消费者、状态版本 CAS、原始成交/账本事实不可变，以及超时进入 UNKNOWN 查单。经济结果最多一次，消息投递可以多次。

### 30.8.4 自检题

1. 账户有 `10,000 USDT`，`BTC` 价格 `50,000`，下单 `0.2 BTC`，费用预留 `20 USDT`，初始保证金率 `10%`。在价格、数量和频率均合法时，reservation 至少应锁定多少 quote 资产？若订单是保护市价单，为什么还要加入滑点保留？
2. 主动买单 `5 lots` 和同组 maker 卖单 `2 lots` 相遇，四种 STP 分别留下多少主动/被动数量？哪一种模式允许主动单继续寻找下一个非同组卖单？
3. `old_mark_tick=100`、`new_mark_tick=95`，多头仓位的强平桶在 `96、98、110`。本次哪些桶 eligible？若 worker 只处理了 `96` 就崩溃，恢复后为什么不能把游标直接置为 `95` 并假定完成？
4. 逐仓多仓 `q=1 BTC`、开仓价 `50,000`、保证金 `5,000`、`MMR=0.5%`。分别计算教学近似下的 `Pliq` 和 `Pbk`，并说明实际引擎还需要加入什么费用/滑点项。
5. 保险基金可用 `600 USDT`，穿仓缺口 `600 USDT`，下一笔缺口 `1 USDT`。两笔清算的状态和账本动作分别是什么？为什么第二笔不能先把基金写成 `-1` 再等 ADL？
6. 数据库已经提交 `OrderAccepted`，网关在返回前超时。客户用相同 `client_order_id` 重试，系统应返回什么？如果用新 ID 重试，可能产生哪一种经济风险？
7. mark 源从 5 个变成 1 个，仍有最新成交价。哪些服务可以继续，哪些必须 fail-closed？需要记录哪些 degraded 证据？

参考答案要点：1. 至少是名义/产品定义的 IM 加费用和滑点保留，不能只看钱包余额；2. `CN/CO/CB/DC` 分别按协议消除冲突量，`DC` 保留非冲突主动量；3. 96、98，未处理候选仍在 pending，必须从事件/bitmap/claim 状态恢复；4. 约 `45,226.13` 和 `45,000`，生产还需费率、手续费、资金费、折扣和价格精度；5. 第一笔精确耗尽进入 settled，第二笔进入 ADL pending；6. 返回原接受事实/状态，换 ID 会重复经济义务；7. 行情观测和撤单/减仓可按策略继续，新增风险和自动强平估值必须暂停或保守降级，并记录源列表、TTL、mark_seq 和策略版本。

### 30.8.5 与前后章节的衔接

本章不是孤立的“交易所风控代码”，而是连接交易、账本和清算的边界层：

```text
[03/04] 市场角色、订单簿与撮合事实
       ↓ accepted_seq / fill_id / STP event
[30] 事前硬风控、保证金、标记价、清算状态机
       ↓ reservation / liquidation_event / insurance posting
[22/23] 不可变复式账本、分桶余额、冲正与热账户
       ↓ posting / account_seq / ledger outbox
[24/25] 幂等身份、UNKNOWN、状态机、TCC/Saga、Outbox
       ↓ 可重放跨服务事实
[26/27] 清算交收、对账、文件/流证据与自动修复
```

- 对 [10]：本章把永续合约中的标记价、破产价、保险基金和 ADL 从机制说明落到分区、索引、协议和故障恢复；线性/反向合约的产品公式仍必须绑定各自风险版本。
- 对 [20]：所有价格、数量、名义、费率、权益和基金余额沿用最小单位整数、舍入方向和尾差归属；不能在 Go/Java 层偷偷引入浮点。
- 对 [22/23]：reservation、强平结算、保险基金和 ADL 影响都必须落复式 posting；余额是投影，不是可任意覆盖的事实。
- 对 [24/25]：订单、强平和外部成交统一使用稳定业务身份，超时进入 UNKNOWN，状态变化追加 transition/outbox，不能靠新 ID 重试。
- 对 [26/27]：强平/保险/ADL 的成交、分录和外部回执进入清算与多层对账；任何“自动修复”都必须以原始事件、批次水位和修复键为依据。

下一层系统应继续回答：多个市场、多个法律实体和跨币种抵押品如何在集中清算/交收日切中计算净额；风险规则版本如何在不停止交易的情况下迁移；保险基金、ADL 和外部托管资产如何与四方证据完成最终对账。也就是说，30.8 的出口不是“强平结束”，而是把每一笔风险决策、接管、基金拨付和自动减仓都交给后续清算、账本和对账系统验证、结算与证明。

---

## 本章最小生产验收清单

- [ ] 每个订单有稳定 `client_order_id/order_id`，接受事实、reservation 和撮合事实相互可追溯。
- [ ] 价格、数量、名义、保证金和基金余额均为定点整数/大整数，舍入版本写入事实。
- [ ] 价格带、胖手指、频率、头寸、集中度和保证金均在撮合前硬拒绝。
- [ ] STP 四种模式在撮合线程原子执行，冲突量和剩余量有事件证据。
- [ ] mark/index/last 分离，源有效性、离群剔除、TTL、seq 和策略版本可审计。
- [ ] 强平索引插入/摘除/claim O(1)，空桶查找有界；大跳价按输出规模处理且不跳过候选。
- [ ] 强平状态、保险基金和 ADL 有白名单转移、幂等键、版本 CAS 和不可变事件。
- [ ] commit 后超时、ACK 丢失、worker 崩溃和主从切换均以 UNKNOWN/查单/重放收敛。
- [ ] 压测覆盖峰值强平、行情离群、规则切换、保险耗尽、跨市场并发和数据库故障。
- [ ] 任意仓位、基金余额和账本 posting 可从原始事件重建，并通过对账证明没有重复经济影响。
