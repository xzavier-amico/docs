# 31. 撮合引擎与订单簿：单线程确定性、零 GC 核心与可恢复的价格时间优先

> **状态**：生产级工程篇｜**所属**：第三部分·金融工程代码基座｜**编号**：31 / 48
>
> **本章定位**：承接 [03 市场微观结构与六类角色](03-market-microstructure-and-six-roles.md)、[04 订单簿、订单类型与流动性](04-orderbook-order-types-and-liquidity.md)、[24 金融级全链路幂等](24-end-to-end-financial-idempotency.md)、[25 状态机与事务 Outbox](25-state-machines-tcc-saga-and-outbox.md)、[29 OMS 与 FIX 协议网关](29-oms-fix-gateway-and-algo-execution.md) 和 [30 事前硬风控与强平引擎](30-pretrade-risk-margin-and-liquidation-engine.md)，把“已通过风控的订单”落实为单写者、可重放、可证明数量守恒的中央限价订单簿（CLOB）。
>
> **覆盖范围**：撮合事故引子；单线程确定性 OrderBook；价格树、侵入式链表与预分配订单槽；Limit/Market/IOC/FOK/Post-Only；价格时间优先与自成交防范（STP）；Go Arena 与 Java Agrona 的零分配核心代码；事件协议与 PostgreSQL DDL；快照恢复、对账、压测与延迟预算；极端边界、术语、面试答题卡、自检题和 31.8 衔接。
>
> **不可妥协的红线**：撮合器只有一个逻辑写者，任何订单簿状态不得被旁路线程直接修改；成交价取被动方价格，且价格/数量全用定点整数；同一输入事件序列、规则版本和初始快照必须得到同一输出；`FOK` 不能先成交再发现深度不足；`Post-Only` 不能偷偷吃单；自成交防范必须发生在成交前；订单、成交、余额和行情视图不是同一个事实表；进程崩溃后只能从已持久化序列重放，不能靠内存状态猜测；所谓“零 GC”只表示撮合热路径在预热后不产生对象分配，不表示整个 JVM/Go 进程永远没有 GC。

---

## 31.1 事故引子：撮合器没有错，市场却被打穿了

### 31.1.1 事故时间线

某交易对 `ETH-USDT` 的撮合服务在发布了一个“优化版”订单簿后，出现了 170 ms 的尖峰。代码将每个价位的订单保存到 `map[price][]*Order`，撮合线程把成交回报通过 goroutine 异步发送；为了“提高并发”，撤单、行情发布和风控回调也可以直接修改订单对象。事故发生在一笔 `Market` 卖单进入时：

| 时间 | 事件 | 错误实现 | 后果 |
|---|---|---|---|
| `10:00:00.000` | 买一 `2,000.00 × 3`、买二 `1,999.99 × 4` | 价位 map 的最佳价通过遍历计算 | 每次市价扫盘都有不可预测的 O(价位数) 开销 |
| `10:00:00.012` | 收到卖出 `100 ETH Market` | 没有保护价/最大扫盘档数 | 订单继续扫到极薄的远端买盘，滑点失控 |
| `10:00:00.013` | 撤单线程移除买一中的一单 | 直接改共享 slice，撮合线程仍持有旧下标 | 跳过一单，或重复访问已释放订单 |
| `10:00:00.014` | 撮合部分成交 | 成交价格读取主动单价格 | 买方被错误地按 `1,999.98` 结算，违反被动方定价 |
| `10:00:00.015` | STP 回调在撮合后异步执行 | 先写 `Trade`，再判断同组账户 | 自成交已进入成交量、费用和持仓，无法无损撤回 |
| `10:00:00.020` | Java GC 与 Go 临时对象集中出现 | 每笔订单分配对象、字符串和 JSON | p99 从 300 µs 跳到 170 ms，队列继续堆积 |
| `10:00:00.170` | 主进程重启 | 只恢复 `orders` 表当前状态 | 已发出但未落库的成交消失，盘口和账本不一致 |

表面上是“市价单太大”和“一次 GC”，真正的根因是撮合器没有被设计成一个**确定性状态机**：订单接纳顺序、价位遍历、订单存活、STP 决定、成交输出和持久化边界都没有共同的单调序列。

### 31.1.2 唯一真问题

> 对同一市场、同一初始快照和同一组已排序命令，系统能否证明：每个成交只发生一次，价格时间优先没有被破坏，主动/被动数量守恒，FOK 不会部分成交，Post-Only 不会吃单，STP 不会产生自成交，进程重启后仍可通过快照加事件重建完全相同的订单簿？

### 31.1.3 生产替代架构

```text
客户端 / FIX / WebSocket
        │ 认证、schema、幂等、事前风控、reservation
        ▼
单一市场分区输入队列 ── sequence ──► Matching Core（单线程单写者）
                                      │
                     ┌────────────────┼─────────────────┐
                     ▼                ▼                 ▼
              Trade/Reject       BookDelta          OrderEvent
                     │                │                 │
                     └──────────────┬─┴─────────────────┘
                                    ▼
                         durable journal / outbox
                                    │
               ┌────────────────────┼──────────────────┐
               ▼                    ▼                  ▼
             账本/持仓             L2/L3 行情          快照/恢复/对账
```

`Matching Core` 只接受已经分配好的市场序列 `engine_seq`。网络线程可以并发接收，但不能直接触碰订单簿；它们只能把规范化命令放入分区队列。撮合器每处理一个命令，按固定顺序完成校验、撮合、状态变更和输出，随后才推进序列。

---

## 31.2 领域模型与不变量：订单状态不是成交事实

### 31.2.1 五类对象必须分离

| 对象 | 含义 | 可否改变订单簿 | 事实身份 |
|---|---|---:|---|
| `OrderCommand` | 客户/风险服务的意图，如新单、撤单 | 否，排队后才执行 | `(market, command_seq)` 与幂等键 |
| `Order` | 订单簿内的规范化快照 | 是，仅撮合线程 | `order_id` + `slot_generation` |
| `Execution` | 一次真实成交 | 否，追加事实 | `trade_id` / `match_seq` |
| `BookDelta` | 盘口变化的投影 | 否 | `book_seq` |
| `Reservation` | 事前风险冻结 | 否，走风险/账本事实 | `reservation_id` |

不要用一个 `orders.status` 同时表示“客户请求已收到”“订单在簿”“已成交”“余额已扣减”。推荐链路是：

```text
OrderCommand
  → 风控接受 + reservation
  → OrderAccepted（带 accepted_seq）
  → MatchingCommand（带 engine_seq）
  → Trade / OrderBookChanged / OrderDone
  → 账本消费成交事实并按 trade_id 幂等入账
```

### 31.2.2 核心不变量

以下不变量必须在每个命令边界、快照恢复和定时审计中检查；发现违反时停写该市场，不能“修一个字段继续跑”。

1. **数量守恒**：对任意订单 `original_qty = filled_qty + leaves_qty + canceled_qty + expired_qty`。正常流程下各项均为非负整数。
2. **成交守恒**：一笔 `Trade` 的 `qty > 0`，且不超过主动单和被动单的当前 `leaves_qty`；同一 `(market, match_seq)` 不能重复。
3. **价格合法**：所有 `price_ticks` 在市场 tick 范围内；成交价等于被动订单的价格，且满足主动订单限价。
4. **簿面合法**：撮合完成后 `best_bid < best_ask`；价位为空时不能留在价格树；订单只能出现在一个价位链表中。
5. **队列合法**：同价位订单按不可变 `arrival_seq` 递增；修改导致价格变化或增加数量时必须重新排队。
6. **槽位合法**：`handle = (generation, slot_index)`；释放槽位后旧 handle 永远不能访问新订单。
7. **单写者**：订单簿、价格树、订单链表、订单槽的写入者只有撮合线程；读者看快照或通过顺序化查询。
8. **可重放**：输出事件中的输入版本、规则版本、`engine_seq` 和订单参数足够重建决定；不能依赖系统时钟、map 遍历顺序或随机数。
9. **幂等**：同一 `client_order_id` 的规范化指纹不变；重试返回原接受/拒绝结果，指纹冲突拒绝。
10. **余额边界**：撮合器只产生成交义务和释放/调整 reservation 的事件，不能绕过账本直接改现金或仓位。

### 31.2.3 事件顺序与时间字段

使用以下时间/序列，不能把它们混成一个自增 ID：

| 字段 | 生成者 | 用途 |
|---|---|---|
| `client_ts` | 客户端 | 诊断和时钟偏差，不参与优先级 |
| `accepted_seq` | 风控/接入分区 | 证明订单被接受的顺序 |
| `engine_seq` | 市场单写者 | 撮合的唯一输入顺序 |
| `arrival_seq` | 撮合器 | 价位内时间优先，必须单调 |
| `match_seq` | 撮合器 | 每次成交的顺序与幂等键 |
| `event_seq` | journal | 事件持久化顺序 |
| `event_time_ns` | 撮合器时钟 | 观测延迟；不参与价格时间优先 |

若两笔订单在物理网络上同时到达，先进入该市场输入队列并得到较小 `engine_seq` 的订单先排队；不能用客户端时间或线程调度顺序事后推断。

---

## 31.3 单线程确定性 OrderBook：为什么不把一个市场拆成多把锁

### 31.3.1 单线程不是低性能，而是把共享可变状态压到一个点

对于一个市场分区，撮合循环是：

```text
while command := input.next():
    assert command.engine_seq == last_seq + 1
    validate_static(command)
    if command.type == NEW:
        precheck_tif_and_stp(command)
        match(command)
        maybe_rest(command)
    else if command.type == CANCEL:
        cancel(command)
    else if command.type == REPLACE:
        cancel_old_then_new(command)  // 规则明确，不是原子魔法
    emit_events_in_fixed_order()
    last_seq = command.engine_seq
```

网络、风控、持久化和行情发布都可并发；只有 `OrderBook` 的逻辑状态由这个循环修改。这样做的好处是：

- 不需要在“扫描最优价、摘除订单、更新数量、生成成交”之间持有多把锁；
- 价格时间优先的判定顺序直接等于循环顺序；
- 同一输入可在开发机、主机和恢复程序上得到相同输出；
- p99 延迟由常数级内存访问和事件数决定，不会被锁竞争放大；
- 事故时可以暂停接收、导出当前序列和快照，而不是尝试冻结一组互相依赖的锁。

“单线程”不是“每个市场一个 OS 线程”这一硬性要求；可以用固定线程池将多个互不相关的市场分片，但一个市场在任一时刻只能有一个逻辑写者。跨市场组合策略、撮合后清算和行情广播不应在撮合临界路径内同步执行。

### 31.3.2 价位树 + 侵入式 FIFO 链表

推荐的内存结构如下：

```text
Book
 ├─ bids: PriceTree<PriceLevel>   // key 递增，best 指向最大 key
 ├─ asks: PriceTree<PriceLevel>   // key 递增，best 指向最小 key
 ├─ orders[slot]                  // 预分配订单槽
 ├─ levels[slot]                  // 预分配价位槽
 └─ free_slots / free_levels      // 整数栈，非指针队列

PriceLevel
 ├─ price_ticks
 ├─ head_order, tail_order        // intrusive doubly linked list 的 slot index
 ├─ total_qty
 ├─ left, right, parent           // 红黑树节点的 level index
 └─ color

OrderSlot
 ├─ generation, state
 ├─ order_id, account_id, stp_group
 ├─ side, price_ticks, original_qty, leaves_qty
 ├─ arrival_seq
 ├─ prev, next, level_index       // 只存 index，不存对象指针
 └─ flags / reserved fields
```

**为什么不是 `map[price][]Order`：**

1. map 遍历不能提供最佳价的稳定顺序；
2. slice 删除队首通常要移动元素，或留下 tombstone 造成延迟抖动；
3. 每笔订单对象和切片扩容制造分配、写屏障和 GC 扫描；
4. 撤单需要从订单 ID 反查队列位置，侵入式链表可 O(1) 摘除；
5. 价位从空变非空、非空变空时，树节点的插入/删除边界明确，便于重放和审计。

红黑树、AVL、跳表都可用。生产选择的关键不是理论上的“最快”，而是固定上界、实现可验证、分配可控、最佳价更新简单。若市场 tick 数固定且价格范围有限，也可以用 radix tree 或分桶数组；但不能为了 O(1) 牺牲过大的内存上界和极端价格保护。

### 31.3.3 handle 与代数：防止撤单复用旧槽

```text
handle = (generation << 32) | uint32(slot_index)
```

撤单命令携带 `order_id`，内部索引映射到 `(slot, generation)`。订单释放后 generation 加一；迟到的撤单或重复命令若带旧 generation，返回 `ALREADY_DONE` 或 `UNKNOWN_ORDER`，绝不把同一槽位中新订单误删。映射表可按 `order_id` 做开放寻址或外部持久化查询，但撮合热路径不能依赖随机 map 遍历。

### 31.3.4 最优价和空价位处理

每侧维护 `best_level` 索引，而不是每次从树根搜索。插入：

```text
if side == BID and (best == NIL || p > best.price): best = level
if side == ASK and (best == NIL || p < best.price): best = level
```

删除价位时，用树的 successor/predecessor 更新最佳价。价位链表成为空后必须先从价格树摘除，再释放 level 槽；不能保留空价位等待“以后可能复用”，否则 FOK 深度扫描、L2 行情和恢复校验会把空档误认为真实价位。

---

## 31.4 撮合算法：Limit、Market、IOC、FOK、Post-Only

### 31.4.1 规范化订单字段

```text
market_id       string / int
side            BUY | SELL
type            LIMIT | MARKET
time_in_force   GTC | IOC | FOK
post_only       bool
price_ticks     int64       // Market 不填；Limit 必须 tick 对齐
qty_lots        int64       // 必须 lot 对齐，严格大于 0
max_slippage    int64?      // 保护市价；转换为可接受价格边界
stp_mode        CN | CO | CB | DC
stp_group       int64
client_order_id string
```

`Limit`、`Market` 是价格/成交意图；`IOC`、`FOK` 是剩余数量生命周期；`Post-Only` 是主动性约束。不能把它们建成互斥枚举。例如 `LIMIT + IOC` 合法，`LIMIT + FOK + POST_ONLY` 通常直接拒绝，因为“必须全成”和“绝不能吃单”只有在当前盘口没有交叉且未来立即成交的情况下才可能满足，语义容易被误用。市场规则必须在协议层明确拒绝组合，而不是在撮合中临时猜测。

### 31.4.2 价格可成交条件

```text
BUY  taker crosses ASK iff ask_price <= buy_limit_price
SELL taker crosses BID iff bid_price >= sell_limit_price
```

每次成交：

```text
trade_price = maker.price_ticks
trade_qty   = min(taker.leaves_qty, maker.leaves_qty)
```

主动买单按卖方价位从低到高扫描；主动卖单按买方价位从高到低扫描。一个价位内严格从 `head_order` 到 `tail_order` 扫描。主动订单的价格只是保护上限/下限，绝不能作为成交价。

### 31.4.3 Limit

1. 校验价格、数量、风险预占、市场状态和 STP 策略；
2. 若与对手盘交叉，逐价位成交；
3. 成交后仍有 `leaves_qty`：普通 GTC 限价单按原始 `arrival_seq` 入对应价位队尾；
4. 若没有成交且价格不交叉，直接入簿；
5. 事件顺序固定为 `Trade* → OrderRested` 或 `OrderDone`，不能一边广播一边继续修改订单。

### 31.4.4 Market

纯市价单不入簿，等价于带保护边界的 IOC：

- 买单最多扫到 `protection_price`，卖单最低扫到 `protection_price`；
- 保护边界可以由参考价、最大滑点、最大名义、最大档数和市场状态共同决定；
- 深度不足时剩余部分按市场规则 `CANCELED` 或 `REJECTED_NO_LIQUIDITY`，不得静默变成挂单；
- 订单进入撮合前就必须检查最坏可成交名义和手续费冻结，不能“先扫再看余额”；
- 市价单不等于无限价格，也不等于保证全部成交。

### 31.4.5 IOC

IOC 先按限价（或保护市价）撮合，剩余量立即取消。`IOC` 的取消事件必须带 `canceled_qty`；不要把未成交余额从内存中抹掉，否则账本无法释放剩余 reservation。部分成交和零成交都属于正常结果，状态例如 `PARTIALLY_FILLED/CANCELED` 或 `CANCELED`。

### 31.4.6 FOK：先预扫描，再执行

FOK 的关键是不允许在第一次成交后才发现深度不足。对于限价买单，预扫描所有 `ask_price <= limit` 的有效深度：

```text
available = 0
for level from best_ask upward while level.price <= limit:
    for maker in FIFO(level):
        if stp_would_block(maker, taker):
            available += stp_policy_available(maker, taker)
        else:
            available += maker.leaves_qty
        if available >= taker.qty: break

if available < taker.qty:
    emit OrderDone(reason=FOK_NOT_FILLED, filled=0, canceled=qty)
else:
    execute_exactly_once(taker)
```

这里的 `available` 必须与真实执行规则一致：STP、冻结订单、保护价、市场状态和撮合权限都要参与预扫描。更稳妥的做法是预扫描只计算“可能成交量”，执行阶段仍在同一线程重新验证；若内部不变量变化，整笔命令进入严重错误，不允许部分提交。预扫描不能调用会修改订单簿的外部回调。

### 31.4.7 Post-Only

Post-Only 的语义是“该订单成功后必须成为 Maker”。有两种明确策略：

| 策略 | 会与对手盘交叉时 | 优点 | 风险 |
|---|---|---|---|
| `REJECT` | 整单拒绝，零成交 | 语义最清楚，适合做市 | 快速行情中拒单率高 |
| `SLIDE` | 买价退到 `best_ask - 1 tick`，卖价升到 `best_bid + 1 tick` | 尽量保留挂单意图 | 改价可能改变策略价格，需重新过价格带 |

`SLIDE` 后若价格越界、价格变为负数、市场无合法 tick 或与新盘口仍交叉，则拒绝。Post-Only 的主动性判断必须在撮合器内、与入簿原子完成；不能先发送一条 `Trade` 再补发拒绝。

### 31.4.8 修改、撤单和优先级

生产规则应显式版本化：

- 只减少数量且价格不变：可保留时间优先；
- 增加数量、改变价格、改变 side 或由 IOC 改为 GTC：取消旧订单并以新 `arrival_seq` 入队；
- 修改与成交竞争时，以市场 `engine_seq` 先后决定；不能用 API 请求到达时间猜测；
- 撤单已经在撮合队列之后时，若订单先成交，返回 `ALREADY_FILLED` 或部分撤单结果；
- 撤单幂等，不应第二次减少 `leaves_qty`；
- 冰山单补量通常获得新的队尾优先级，除非交易规则明确采用隐藏层规则。

---

## 31.5 价格时间优先与自成交防范

### 31.5.1 价格优先、时间优先的精确定义

排序键不是“价格 + 本机时间”，而是：

```text
maker_priority = (
    better_price(side),
    arrival_seq,
    order_id       // 只用于理论上不应发生的同 seq 稳定破局
)
```

买盘价格从高到低，卖盘从低到高；同价位按 `arrival_seq` 从小到大。`arrival_seq` 由撮合器分配，不接受客户端伪造。价格树只负责档间排序，侵入式链表只负责档内 FIFO；不能用数据库 `created_at` 替代，因为数据库时钟精度、提交顺序和重放顺序都可能不同。

### 31.5.2 STP：在成交前决定冲突动作

`stp_group` 是冻结在订单上的风险域，可以按主账户、子账户、策略或法律实体配置；不能在撮合时读取一个随时变化的用户配置。主动单与被动单同组时，不产生 `Trade`，而是产生 `STP_PREVENTED`：

| 模式 | 动作 | 队列后果 |
|---|---|---|
| `CANCEL_TAKER (CN)` | 撤掉主动单 | 被动单保持原优先级 |
| `CANCEL_MAKER (CO)` | 撤掉冲突被动单 | 主动单继续向下一单扫盘 |
| `CANCEL_BOTH (CB)` | 两边冲突部分取消 | 主动单通常结束，规则需固定 |
| `DECREMENT_AND_CANCEL (DC)` | 按冲突量减少，较小者归零并取消 | 主动单的非冲突剩余可继续 |

必须记录：两边 `order_id`、`stp_group`、模式版本、冲突数量、处理后剩余量、`engine_seq`。STP 不收手续费、不增加成交量、不生成持仓，不应调用成交回报路径。`DC` 的减量是否改变被动单队列位置必须固定；通常只减少数量且不改价，可以保留其剩余时间优先。

### 31.5.3 STP 与 FOK 的关系

FOK 的“可成交深度”不能把同组订单当成普通流动性。若 `CANCEL_TAKER`，同组订单使 FOK 无法完成，FOK 必须整单取消；若 `CANCEL_MAKER`，预扫描可以把该 maker 视为可删除，再看后续订单，但执行过程必须仍不产生自成交；若 `DC` 导致可成交量不足，仍然是 `FOK_NOT_FILLED`。这正是“先预扫描、后执行”不能省略的原因。

### 31.5.4 防止“自成交后回滚”的反模式

以下实现不可接受：

```text
先扣 maker/taker 数量 → 写 Trade → 异步查询账户是否同组 → 再撤销 Trade
```

成交一旦进入 `Trade`，可能已经被行情、手续费、账本和外部监管报告消费，所谓回滚会制造第二次经济事实。STP 必须在 `maker`、`taker` 和规则版本都在撮合线程可见时完成。

---

## 31.6 Go Arena 与 Java Agrona：预分配、索引化、无热路径分配

### 31.6.1 “零 GC”边界

生产承诺应写成可测量的形式：

> 在启动时预热市场配置、订单槽、价位槽、journal buffer 和事件编码器；稳定容量内，单市场撮合热路径不调用 `new/make/append 扩容`，不创建字符串、闭包、interface 装箱或 JSON 对象；周期性快照和慢路径可以分配，但不能阻塞撮合线程。

Go 的 GC 仍可能扫描预分配切片；Java 的 JVM 仍可能因日志、网络和其他线程触发 GC。因此验收指标应是 `allocs/op = 0`、`Allocation Rate` 接近零、热路径无 safepoint 抖动，而不是口号式的“绝对零 GC”。

### 31.6.2 Go：Arena 风格整数槽位核心

下面代码省略红黑树旋转细节，展示真正关键的所有权、槽位和撮合顺序。实际项目应把 `OrderID`、账户、规则版本和事件编码接入固定宽度的 journal buffer；不要在 `match` 内返回 `[]Trade` 的动态切片。

```go
package matching

const Nil uint32 = ^uint32(0)

type Side uint8
const (
    Buy Side = iota
    Sell
)

type OrderState uint8
const (
    Free OrderState = iota
    Live
    Done
)

type OrderSlot struct {
    Generation uint32
    State      OrderState
    Side       Side
    Price      int64 // ticks
    Leaves     int64 // lots
    Arrival    uint64
    Account    uint64
    STPGroup   uint64
    Prev, Next uint32
    Level      uint32
}

type Level struct {
    Used                    bool
    Price                   int64
    Head, Tail              uint32
    Total                   int64
    Left, Right, Parent     uint32
    Red                     bool
}

// Arena 在启动时一次性保留容量；热路径只取整数索引，不分配订单对象。
type Arena struct {
    orders []OrderSlot
    levels []Level
    freeOrders []uint32
    freeLevels []uint32
}

func NewArena(maxOrders, maxLevels int) *Arena {
    a := &Arena{
        orders: make([]OrderSlot, maxOrders),
        levels: make([]Level, maxLevels),
        freeOrders: make([]uint32, maxOrders),
        freeLevels: make([]uint32, maxLevels),
    }
    for i := 0; i < maxOrders; i++ { a.freeOrders[i] = uint32(maxOrders-1-i) }
    for i := 0; i < maxLevels; i++ { a.freeLevels[i] = uint32(maxLevels-1-i) }
    return a
}

func (a *Arena) allocOrder() (uint32, bool) {
    n := len(a.freeOrders); if n == 0 { return Nil, false }
    idx := a.freeOrders[n-1]; a.freeOrders = a.freeOrders[:n-1]
    s := &a.orders[idx]; s.Generation++; s.State = Live
    s.Prev, s.Next, s.Level = Nil, Nil, Nil
    return idx, true
}

func (a *Arena) freeOrder(idx uint32) {
    s := &a.orders[idx]; s.State = Free; a.freeOrders = append(a.freeOrders, idx)
}

type Book struct {
    a *Arena
    bestBid, bestAsk uint32
    nextArrival uint64
    // bid/ask tree 根节点、order_id 索引和事件 writer 未展开。
}

// cross 只在单线程内调用。emit 不允许保留指向可复用槽的指针。
func (b *Book) cross(taker uint32, limit int64, emit func(maker, taker uint32, px, qty int64)) {
    t := &b.a.orders[taker]
    for t.Leaves > 0 {
        maker := b.bestOpposite(t.Side)
        if maker == Nil { return }
        m := &b.a.orders[maker]
        if t.Side == Buy && m.Price > limit { return }
        if t.Side == Sell && m.Price < limit { return }

        // STP 必须在 Trade 之前决定；这里示意 CN，其他模式同理。
        if t.STPGroup != 0 && t.STPGroup == m.STPGroup {
            b.preventSelfTrade(taker, maker)
            if t.State != Live { return }
            continue
        }
        qty := t.Leaves; if m.Leaves < qty { qty = m.Leaves }
        px := m.Price // 永远采用 maker 价格
        t.Leaves -= qty; m.Leaves -= qty
        emit(maker, taker, px, qty) // emit 复制值，不保存槽指针
        if m.Leaves == 0 { b.removeFromLevel(maker); b.a.freeOrder(maker) }
    }
}
```

上例的 `append` 只出现在 `freeOrder`；生产版本必须保证 free-list 容量等于订单槽上限，或使用固定数组和 top 指针，避免释放槽时也可能扩容。事件输出应采用固定结构体写入预分配 ring buffer；`emit func` 只是教学接口，真正热路径可用内联 writer 以避免函数值逃逸。Go 还要用 `go test -gcflags=-m` 检查逃逸，用 `-benchmem` 和 `pprof` 检查分配与 GC。

### 31.6.3 Go 交易事件的固定编码

```go
type TradeEvent struct {
    MatchSeq uint64
    MakerID  uint64
    TakerID  uint64
    Price    int64
    Qty      int64
    MakerFee int64
    TakerFee int64
}

type EventRing struct {
    buf []TradeEvent
    head, tail uint64
}

func (r *EventRing) Push(e TradeEvent) bool {
    if r.tail-r.head == uint64(len(r.buf)) { return false } // 背压，不扩容
    r.buf[r.tail%uint64(len(r.buf))] = e
    r.tail++
    return true
}
```

ring 满不能在撮合线程中 `append`；应该停止接受新命令、持久化当前边界或切换到有界降级状态。若事件发布者跟不上，继续撮合只会造成“内存成功、外部事实丢失”。

### 31.6.4 Java 21 + Agrona：DirectBuffer 与索引槽

```java
final class OrderArena {
    // 每个订单槽固定 64 bytes；slot 0 保留为 NIL，避免对象引用。
    static final int SLOT_BYTES = 64;
    static final int NIL = -1;
    final org.agrona.concurrent.UnsafeBuffer memory;
    final int[] free;
    int freeTop;

    OrderArena(int maxOrders) {
        memory = new org.agrona.concurrent.UnsafeBuffer(
            java.nio.ByteBuffer.allocateDirect((maxOrders + 1) * SLOT_BYTES));
        free = new int[maxOrders];
        for (int i = 0; i < maxOrders; i++) free[i] = maxOrders - i;
        freeTop = maxOrders;
    }

    int alloc() {
        if (freeTop == 0) return NIL;
        int slot = free[--freeTop];
        memory.putInt(slot * SLOT_BYTES, 1);       // generation
        memory.putInt(slot * SLOT_BYTES + 4, 1);   // LIVE
        memory.putInt(slot * SLOT_BYTES + 40, NIL);// prev
        memory.putInt(slot * SLOT_BYTES + 44, NIL);// next
        return slot;
    }

    void free(int slot) {
        memory.putInt(slot * SLOT_BYTES + 4, 0);   // FREE
        free[freeTop++] = slot;
    }
}

final class MatchCore {
    final OrderArena arena;
    final org.agrona.concurrent.ManyToOneRingBuffer commands;
    final org.agrona.concurrent.ManyToOneRingBuffer events;
    long engineSeq;

    void onCommand(long seq, int commandType, int slot) {
        if (seq != engineSeq + 1) throw new IllegalStateException("sequence gap");
        // 只在该线程修改 tree、链表和 DirectBuffer；禁止 lambda、装箱和日志格式化。
        switch (commandType) {
            case 1 -> matchNew(slot);
            case 2 -> cancel(slot);
            default -> reject(slot, "UNKNOWN_COMMAND");
        }
        engineSeq = seq;
    }
}
```

Agrona 的 `ManyToOneRingBuffer` 适合作为有界命令/事件通道；若使用 `ExpandableArrayBuffer` 或动态集合，要证明它们不在撮合热路径扩容。`UnsafeBuffer`/DirectBuffer 可以减少对象引用和 GC 扫描，但必须明确内存布局、字节序、边界检查和生命周期。Java 端还应配置固定堆上限、记录 JFR/GC pause，并让慢消费者造成背压而不是无限堆积。

### 31.6.5 预分配容量与拒绝策略

`max_live_orders`、`max_price_levels`、事件 ring 容量都应是市场配置的一部分，并在开市前校验：

```text
可接受新单 ⇔ free_order_slots > 0
             ∧ 预计价位数 < max_price_levels
             ∧ event_ring_remaining ≥ worst_case_events(command)
```

容量不足时返回 `BOOK_CAPACITY`，不是偷偷分配，也不是覆盖最老订单。若需要扩容，必须停市、生成新快照、切换新实例并做一致性验证；不要在线在一个实时树上做不可重放的扩容副作用。

---

## 31.7 协议、持久化与 PostgreSQL DDL

### 31.7.1 内部命令协议

建议内部使用 Protobuf/FlatBuffers 或固定二进制 envelope；JSON 只用于边缘 API。每条命令至少包含：

```protobuf
message MatchingCommand {
  string market_id = 1;
  uint64 engine_seq = 2;
  bytes command_id = 3;       // 幂等身份
  uint32 kind = 4;            // NEW/CANCEL/REPLACE
  uint64 order_id = 5;
  uint64 account_id = 6;
  uint64 stp_group = 7;
  uint32 side = 8;
  uint32 order_type = 9;
  uint32 tif = 10;
  int64 price_ticks = 11;
  int64 qty_lots = 12;
  uint32 stp_mode = 13;
  uint64 accepted_seq = 14;
  uint32 risk_policy_version = 15;
  bytes payload_hash = 16;
}
```

规则：

1. `market_id + engine_seq` 唯一；缺口不能跳过，重复序列必须校验 payload hash；
2. `order_id` 在市场内不可复用；客户端 ID 与订单 ID 分离；
3. `price_ticks`、`qty_lots` 是整数，不在线路层传浮点；
4. enum 未知值拒绝；协议版本和市场规则版本一起写入事件；
5. 入站命令原文/哈希进入 durable journal，规范化命令才交给撮合器；
6. 取消不存在订单可以是幂等成功 `ALREADY_DONE`，但必须返回可核验的原状态/序列。

### 31.7.2 输出事件顺序

对一条新单，推荐固定顺序：

```text
CommandAccepted
  → (STP_PREVENTED | Trade)*
  → OrderRested | OrderCanceled | OrderFilled
  → BookDelta / TopOfBook
```

持久化时，事件要么全部写入同一个 journal record batch，要么记录一个明确的 `transaction_boundary`。下游若只收到 `Trade` 没收到 `OrderDone`，可以通过 `engine_seq` 重放补齐；下游不能把行情快照当成成交事实。

### 31.7.3 DDL：命令、成交、快照和对账

以下是 PostgreSQL 参考 DDL。撮合热路径通常写本地顺序 journal，再由异步落库；如果直接写 PostgreSQL，必须证明 WAL 延迟不会阻塞市场单写者。金额和数量依然用整数最小单位。

```sql
CREATE TABLE matching_command (
    market_id              text        NOT NULL,
    engine_seq             bigint      NOT NULL,
    command_id             uuid        NOT NULL,
    kind                   text        NOT NULL CHECK (kind IN ('NEW','CANCEL','REPLACE')),
    order_id               bigint      NOT NULL,
    account_id             bigint      NOT NULL,
    accepted_seq           bigint      NOT NULL,
    payload_hash           bytea       NOT NULL,
    payload                bytea       NOT NULL,
    received_at            timestamptz NOT NULL,
    PRIMARY KEY (market_id, engine_seq),
    UNIQUE (market_id, command_id),
    UNIQUE (market_id, order_id, kind, command_id)
);

CREATE TABLE matching_event (
    market_id              text        NOT NULL,
    event_seq              bigint      NOT NULL,
    engine_seq             bigint      NOT NULL,
    event_type             text        NOT NULL,
    order_id               bigint,
    match_seq              bigint,
    payload_hash           bytea       NOT NULL,
    payload                bytea       NOT NULL,
    created_at              timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (market_id, event_seq),
    UNIQUE (market_id, match_seq)
);

CREATE TABLE execution_fact (
    market_id              text        NOT NULL,
    match_seq              bigint      NOT NULL,
    trade_id               uuid        NOT NULL,
    maker_order_id          bigint      NOT NULL,
    taker_order_id          bigint      NOT NULL,
    maker_account_id        bigint      NOT NULL,
    taker_account_id        bigint      NOT NULL,
    price_ticks              bigint      NOT NULL CHECK (price_ticks > 0),
    qty_lots                 bigint      NOT NULL CHECK (qty_lots > 0),
    fee_maker_minor          bigint      NOT NULL,
    fee_taker_minor          bigint      NOT NULL,
    rule_version             integer     NOT NULL,
    event_seq                bigint      NOT NULL,
    created_at               timestamptz NOT NULL,
    PRIMARY KEY (market_id, match_seq),
    UNIQUE (trade_id),
    FOREIGN KEY (market_id, event_seq)
      REFERENCES matching_event(market_id, event_seq)
);

CREATE TABLE order_snapshot (
    market_id              text        NOT NULL,
    order_id               bigint      NOT NULL,
    generation              integer     NOT NULL,
    side                    text        NOT NULL CHECK (side IN ('BUY','SELL')),
    price_ticks             bigint,
    original_qty_lots       bigint      NOT NULL,
    filled_qty_lots         bigint      NOT NULL,
    leaves_qty_lots         bigint      NOT NULL,
    canceled_qty_lots       bigint      NOT NULL,
    arrival_seq             bigint,
    status                  text        NOT NULL,
    last_engine_seq         bigint      NOT NULL,
    PRIMARY KEY (market_id, order_id),
    CHECK (original_qty_lots = filled_qty_lots + leaves_qty_lots + canceled_qty_lots)
);

CREATE TABLE book_snapshot_manifest (
    market_id              text        NOT NULL,
    snapshot_seq            bigint      NOT NULL,
    journal_event_seq       bigint      NOT NULL,
    sha256                  bytea       NOT NULL,
    rules_hash              bytea       NOT NULL,
    order_count             integer     NOT NULL,
    level_count             integer     NOT NULL,
    created_at              timestamptz NOT NULL,
    PRIMARY KEY (market_id, snapshot_seq)
);

CREATE TABLE reconciliation_checkpoint (
    market_id              text        NOT NULL,
    as_of_event_seq         bigint      NOT NULL,
    sum_open_qty_lots       bigint      NOT NULL,
    sum_execution_qty_lots  bigint      NOT NULL,
    ledger_trade_count      bigint      NOT NULL,
    book_hash                bytea      NOT NULL,
    status                   text       NOT NULL,
    discrepancy              jsonb,
    checked_at               timestamptz NOT NULL,
    PRIMARY KEY (market_id, as_of_event_seq)
);
```

`execution_fact` 的唯一键必须先于账本消费，账本以 `(market_id, match_seq)` 或 `trade_id` 做幂等。`matching_event` 是事件证据，`order_snapshot` 是可丢弃的投影；快照不能替代 journal。生产表还应按市场/日期分区、设置保留期和归档哈希，但不能因为归档删除审计所需的成交事实。

---

## 31.8 恢复、对账与故障切换

### 31.8.1 快照 + journal 恢复

快照文件必须包含完整的确定性状态，而不只是 L2 汇总：

```text
snapshot_seq / engine_seq / event_seq
规则版本与 hash
订单槽（含 generation、arrival_seq、prev/next、level）
价位槽（树链接、head/tail、total_qty、best）
order_id → handle 索引
最后成交价、match_seq、风险/市场状态
```

恢复步骤：

1. 校验快照签名/hash、market_id、规则版本和容量配置；
2. 从快照的 `event_seq + 1` 读取 journal，严格按序验证命令 hash；
3. 在隔离的 shadow book 重放，逐事件检查价格、数量、STP 和队列顺序；
4. 将 shadow book 的 `book_hash`、`best_bid/ask`、订单数、总量与快照 manifest 对比；
5. 只有校验通过才取得该市场的 fencing token 并接收新命令；
6. 恢复期间对外状态为 `RECOVERING` 或 `HALT`，不能让半恢复簿接受新单。

快照间隔按恢复目标决定，例如要求 2 秒内恢复且每秒 50 万事件，就不能只做每小时快照。快照本身也应在单线程一致边界生成：可以暂停一个短窗口复制槽数组，或将快照命令排入撮合序列，不能在另一个线程无锁遍历正在修改的链表。

### 31.8.2 主备与 fencing

主备切换至少需要：

- 市场分区租约/fencing token；旧主失去 token 后即使进程还活着，也不能发送成交或接受输入；
- journal 的最后已提交 `event_seq` 和 `engine_seq`；
- 备机重放到相同位置并计算相同 hash；
- 新主先向输入层声明接管边界，拒绝边界前后的重复/缺口命令；
- 旧主重新连接时进入只读 `STALE`，不能靠“自认为还持有租约”继续撮合。

主备不能以“两个节点都看到了相同盘口”作为一致性证明。必须以持久化序列和单一 fencing 事实证明只有一个写者。

### 31.8.3 三层对账

1. **簿内对账**：订单总量、每价位总量、所有订单链表量、`filled + leaves + canceled` 守恒；树遍历得到的 best 与缓存 best 一致。
2. **事件对账**：每个 `Trade` 有唯一 `match_seq`，主动/被动订单的累计成交与 trade 聚合一致；`BookDelta` 可由事件重建。
3. **经济对账**：成交事实与账本分录、费用、持仓、reservation 释放一致；撮合器不自行修余额，差异进入 [27 多层对账与自动修复](27-multi-tier-reconciliation-and-auto-healing.md) 的人工/自动流程。

典型 SQL 检查：

```sql
SELECT market_id, order_id
FROM order_snapshot
WHERE original_qty_lots <> filled_qty_lots + leaves_qty_lots + canceled_qty_lots;

SELECT e.market_id, e.match_seq
FROM execution_fact e
LEFT JOIN ledger_trade l
  ON l.market_id = e.market_id AND l.match_seq = e.match_seq
WHERE l.match_seq IS NULL;
```

对账发现成交事实缺账本分录时，不能删除成交或补写一个“当前余额”；应以 `match_seq` 重新投递账本 outbox，若账本已存在不同金额则进入不可自动修复的人工队列。

### 31.8.4 未知状态与停市

以下情况必须停接新单或转只减仓：journal fsync 超时、事件 ring 满、规则 hash 不一致、序列缺口、快照 hash 不一致、双主 fencing 失败、余额消费确认延迟超过保守阈值。网络超时不是“没有成交”；订单仍保留 `UNKNOWN`/保守 reservation，先查询或重放事实再释放。

---

## 31.9 压测、延迟预算与可观测性

### 31.9.1 延迟预算示例

以下是单市场、同机房、已预热的示例预算，不是所有市场的承诺：

| 阶段 | p50 预算 | p99 预算 | 约束 |
|---|---:|---:|---|
| 网关 schema/鉴权 | 20 µs | 80 µs | 不进入撮合线程 |
| 风控与 reservation | 40 µs | 150 µs | 失败则不投递 |
| 分区排队/序列 | 5 µs | 30 µs | 有界 ring，满则背压 |
| 撮合核心 | 5 µs | 50 µs | 预热后 0 alloc，深度有界 |
| journal append/commit | 30 µs | 200 µs | 不能用未持久化事实对外确认 |
| 事件广播 | 20 µs | 100 µs | 异步，不阻塞核心 |
| **端到端接受到成交事件** | **120 µs** | **600 µs** | 明确定义观测点 |

如果交易规则要求同步 durable commit，而磁盘 p99 已是 2 ms，就不能在文档里宣称撮合 p99 50 µs 代表客户看到成交的 p99 50 µs。必须区分：`gateway_to_engine`、`engine_match`、`engine_to_journal_commit`、`trade_to_ledger`，并记录排队时间、GC pause、上下文切换和 ring 水位。

### 31.9.2 压测矩阵

| 场景 | 目标 | 必须验证 |
|---|---|---|
| 同价位 FIFO | 价格时间优先 | 100 万笔同价订单，撤队首/队中/队尾 |
| 多价位扫盘 | 深度与滑点 | Limit/Market/IOC 的成交价、保护边界 |
| FOK 深度不足 | 原子性 | 零成交，盘口和 sequence 无副作用 |
| Post-Only 交叉 | 主动性 | Reject/Slide 规则稳定，不产 Trade |
| STP 四模式 | 自成交防范 | 每种剩余量、队列位置和事件 |
| 撤单洪峰 | 槽位安全 | 无旧 handle 误删、无 tombstone 失控 |
| 热点价位 | 延迟上界 | 链表 1、10 万、最大队列长度 |
| 事件环满 | 背压 | 拒绝新命令而不是扩容/丢事件 |
| 崩溃重启 | 可重放 | 快照 + journal hash 完全一致 |
| 重复/乱序命令 | 幂等 | 相同结果、冲突 hash 拒绝 |
| 极端整数 | 溢出 | 价格、数量、成交额、费用不溢出 |
| 长时间运行 | 内存稳定 | 24/72 小时 alloc、RSS、GC、槽位回收 |

### 31.9.3 生成式测试与差分测试

建立一个慢但显然正确的参考模型：`TreeMap<price, deque<Order>>`，用同一随机命令流驱动生产模型和参考模型，逐事件比较：

- `Trade` 的顺序、价、量、maker/taker；
- 订单剩余量、状态、队列顺序；
- best bid/ask 和各价位总量；
- FOK/Post-Only/STP 的决策和原因码；
- 快照 hash 与重放 hash。

随机生成器必须记录 seed，失败样例固化为回归测试。再用模型检查器验证短序列的“撤单与成交竞争”“STP 与 FOK”“槽位复用与迟到撤单”等组合。性能测试不能只测空簿单笔单，要测最坏但合法的深度、热点价位和事件风暴。

### 31.9.4 关键指标与告警

```text
matching_command_lag{market}
matching_core_duration_us{market,kind}
matching_p99 / p999
book_live_orders / book_live_levels
event_ring_used_ratio
journal_commit_lag
gc_pause_ms / allocations_per_command
stp_prevented_total{mode}
fok_not_filled_total
market_protection_reject_total
recovery_replay_events
reconciliation_discrepancy_total
```

告警阈值要有动作：事件环超过 70% 预警、90% 开始拒绝低优先级新单；槽位不足进入 `BOOK_CAPACITY`；p99 连续超标暂停高频账户或扩大背压；对账差异直接停市，不用“差一点正常”掩盖经济事实。

---

## 31.10 极端边界与工程决策表

| 边界 | 必须发生 | 不可发生 |
|---|---|---|
| `qty=0`、负价、非 tick/lot | schema 拒绝 | 进入 FOK 预扫描后才拒绝 |
| `price * qty` 溢出 | 使用 128 位/大整数检查并拒绝 | int64 溢出成负数后放行 |
| best bid/ask 相等或交叉 | 停止该市场并审计 | 继续撮合制造隐藏成交 |
| 价位树达到上限 | `BOOK_CAPACITY`，保留现有簿 | 动态扩容或覆盖旧 level |
| 订单槽耗尽 | 拒绝新单，撤单仍可处理 | 复用 live 槽位 |
| 重复撤单 | 返回原终态，零副作用 | 第二次扣减 leaves |
| 迟到旧 handle | generation 校验后拒绝 | 删除新订单 |
| 市价无保护价 | 拒绝或转保护市价 | 无限扫盘 |
| FOK 差 1 lot | 0 成交并取消整单 | 先成交部分再取消 |
| Post-Only 恰好跨价 | Reject/Slide 按版本执行 | 默认当普通 Limit |
| STP 同组 | 记录 `STP_PREVENTED` | 先写 Trade 再回滚 |
| 规则版本变化 | 新单使用新版本，旧订单保留冻结版本 | 重放时读取当前配置 |
| snapshot/journal hash 不同 | 隔离实例、停市、人工调查 | 选择“看起来更新”的一份 |
| journal commit 超时 | 保守 `UNKNOWN`/停接 | 返回成交成功后猜测落盘 |
| 费用计算精度边界 | 明确舍入方向、记录 fee version | 用 float 累计 |
| 日志格式化/指标标签爆炸 | 慢路径采样和固定标签 | 在撮合循环拼接字符串 |

对极端行情的保护还应在撮合器外层完成：价格带、单笔/账户名义、最大档数、熔断、只减仓和流量限制。撮合器不能成为最后一个“什么都接受”的垃圾桶。

---

## 31.11 术语表：把“挂单”“成交”“接受”说准确

| 术语 | 精确定义 |
|---|---|
| CLOB | Central Limit Order Book，中央限价订单簿 |
| Maker | 在成交发生前已挂在簿内、被主动单消耗的一方 |
| Taker | 进入撮合并跨越点差、主动消耗流动性的一方 |
| Tick | 最小价格变动单位；`price_ticks` 是整数格数 |
| Lot | 最小交易数量单位；`qty_lots` 是整数格数 |
| Price-Time Priority | 先比较更优价格，再比较同价位的到达序列 |
| Arrival Sequence | 由撮合器分配的单调序列，不是客户端时间 |
| TIF | Time in Force，订单剩余量生命周期策略 |
| IOC | Immediate-or-Cancel，立即撮合，余量取消 |
| FOK | Fill-or-Kill，要么全额成交，要么零成交 |
| Post-Only | 成功后必须成为 Maker；交叉则拒绝或退一跳 |
| STP | Self-Trade Prevention，自成交防范 |
| Intrusive List | 链表链接字段嵌入订单槽，不另分配节点对象 |
| Arena | 预先获得并管理固定槽位的内存区域 |
| Book Hash | 对规范化订单/价位/序列状态做确定性哈希，用于恢复校验 |
| Journal | 按序追加、可重放的命令/事件日志 |
| Snapshot | 某一序列边界的完整状态投影，不替代 journal |
| Fencing Token | 防旧主继续写入的单调接管令牌 |
| Zero Allocation | 热路径不产生新的堆分配；不等于整个进程绝对无 GC |
| Reservation | 风控为未成交义务预留的现金、资产或保证金 |
| Book Capacity | 订单槽、价位槽、事件缓冲等有界容量 |

---

## 31.12 面试答题卡：十个高频问题

### Q1：为什么撮合核心通常单线程？

因为一个市场的簿面是高频共享可变状态。单写者把价格比较、FIFO、撤单、成交和 STP 放在一个确定性序列中，避免多锁竞态和不可重放顺序；并发放在市场分区、输入、journal、账本和行情发布层。

### Q2：订单簿为什么用价格树加侵入式链表？

价格树提供最佳价和价位增删，侵入式链表提供同价 FIFO 与 O(1) 撤单；订单 ID 映射到槽位后，整个热路径不需要移动 slice 元素或分配节点对象。

### Q3：成交价为什么是 maker 价格？

主动单的限价只是愿意接受的边界，先挂在簿内的被动单提供价格。按 taker 价格会破坏订单簿的价格发现和 maker/taker 费用语义。

### Q4：FOK 如何保证不部分成交？

在任何簿面修改前，按同样的价格、STP、保护价和可用深度规则预扫描；深度不足立即零成交取消。执行时在同一单线程再次验证，不允许外部回调改变簿面。

### Q5：Post-Only 和 Limit 的区别？

Limit 可以跨点差，部分成交后余量入簿；Post-Only 的成功结果必须是 Maker，交叉时只能按版本拒绝或退到不交叉的 tick，不能暗中成交。

### Q6：为什么不能成交后再做 STP？

Trade 可能已经被行情、账本、手续费和监管报告消费；事后回滚会产生第二个经济事实。STP 必须在 maker/taker 配对、写 Trade 之前原子决定。

### Q7：Go/Java 如何做到零 GC？

预分配订单/价位槽和有界 ring，使用整数索引、固定宽度事件和 DirectBuffer，避免对象、字符串、闭包、装箱与扩容；用 `allocs/op`、JFR、pprof 验证。不能承诺整个进程永不 GC，只能承诺撮合热路径稳定无分配。

### Q8：撤单如何防止槽位复用误删？

订单 handle 包含 generation 和 slot index。释放时 generation 递增，迟到旧 handle 校验失败；不能只按数组下标判断订单身份。

### Q9：重启后如何证明盘口一致？

加载完整快照，从边界后的 journal 严格按 engine_seq 重放；比较规则 hash、订单/价位数量、best、match_seq 和 book hash，校验通过并取得 fencing token 后才恢复接单。

### Q10：撮合器能不能直接更新余额？

不能。撮合器产生带唯一 match_seq 的成交事实和 reservation 调整事件，账本以成交事实幂等入账。余额是经济账本投影，不是订单簿的临时字段。

---

## 31.13 自检题：读完必须能写出不变量

1. 给出买一 `100×5`、买二 `99×5`，卖一 `101×3`，提交 `BUY LIMIT 102×7`，写出两笔成交的价格、数量、maker/taker、剩余订单和最终盘口。
2. 同一价位有 arrival sequence `10,11,12` 三笔订单，撤掉 sequence 11 后新单 sequence 20 进入，链表顺序是什么？
3. 订单 A 的 FOK 买量为 10，卖盘有 `100×6`、`101×3`，另有同 STP group 的 `101×5`。分别在 `CANCEL_TAKER` 和 `CANCEL_MAKER` 下判断结果，并说明预扫描如何处理。
4. Post-Only 买价等于卖一价时，`REJECT` 和 `SLIDE` 的事件输出分别是什么？退一跳后若跌出价格带怎么办？
5. 一个撤单引用已经释放且被新订单复用的 slot index，但 generation 过期，系统必须返回什么，为什么不能返回新订单状态？
6. 解释 `accepted_seq`、`engine_seq`、`arrival_seq`、`match_seq` 的差别，并说明哪一个决定同价位优先级。
7. 预分配槽位耗尽时，为什么不能从堆上临时分配一个“救急订单”？如果允许扩容，必须经过什么停市/切换流程？
8. journal 已持久化 `Trade`，账本服务超时；应该把成交标成失败还是 UNKNOWN？恢复动作是什么？
9. 写一个差分测试的最小状态机，至少覆盖新单、撤单、IOC、FOK、Post-Only、STP 和槽位复用。
10. 对账发现 `sum(execution.qty)` 大于订单的 `filled_qty`，应该删除哪条记录？为什么正确答案通常是“都不删除，隔离市场并按 match_seq 查事实链”？

**参考要点**：所有题都要能落到整数数量、固定序列、单线程边界、事件幂等和恢复 hash；只讲“加锁”“重试”“最终一致”而说不出经济事实和顺序，不能算答对。

---

## 31.14 与 31.8 的衔接：从单市场撮合到市场级运行时与撮合扩展

本章把一个市场的 CLOB 核心约束为单写者、确定性事件和有界内存。**31.8 应在此基础上继续讨论市场级运行时与扩展撮合能力**，而不是重新发明一个可变共享订单簿。建议衔接以下接口：

1. **分区与路由**：如何将市场分配到固定线程/NUMA，处理市场迁移、热市场拆分与跨市场命令边界；
2. **快照与实时行情**：L2/L3 增量、断点重订阅、校验和、慢消费者和盘口权限视图；
3. **集合竞价与熔断**：开收盘批量出清、价格笼子、停牌/只减仓与连续竞价状态切换；
4. **高级订单**：冰山、隐藏量、Peg、Stop、Trailing、拍卖和条件订单如何转化为本章的规范化 Limit/IOC 子命令；
5. **跨市场风险与清算**：成交事实如何进入 30 的实时保证金、强平接管和 26 的清结算，不允许在撮合核心内同步查全账户；
6. **灾备和在线升级**：双集群 journal、版本兼容、规则升级的生效边界和无脑裂切换；
7. **市场完整性**：异常订单、spoofing、延迟套利、行情回放和监管审计如何消费本章事件而不改变历史事实。

31.8 的第一条验收标准应是：给定本章产生的 `engine_seq`、规则版本和初始快照，扩展运行时必须保持相同的 `Trade`、`STP_PREVENTED`、订单终态和 `book_hash`；任何性能优化都不能以牺牲确定性、价格时间优先、FOK 原子性或恢复可证明性为代价。

---

## 31.15 本章落地清单

- [ ] 每个市场有唯一逻辑写者、连续 `engine_seq` 和 fencing token。
- [ ] 价格、数量、费用使用整数最小单位，乘法有溢出保护和舍入版本。
- [ ] 价格树、价位 FIFO 链表、订单 ID 映射和槽位 generation 均有不变量检查。
- [ ] Limit/Market/IOC/FOK/Post-Only 的组合语义、保护价和失败原因已协议化。
- [ ] FOK 预扫描与真实执行的 STP、保护价、容量规则一致。
- [ ] 四种 STP 在成交前决定并生成可重放事件，不产生虚假 Trade。
- [ ] Go/Java 热路径通过分配测试、GC/JFR/pprof 和有界 ring 背压测试。
- [ ] command、event、execution、snapshot、reconciliation DDL 分层，成交按 `match_seq` 幂等入账。
- [ ] 快照包含完整树/链表/槽位状态，恢复后 `book_hash` 与 shadow replay 一致。
- [ ] 压测覆盖热点价位、FOK、STP、撤单洪峰、槽位耗尽、journal 满和崩溃重启。
- [ ] 对账差异不通过改余额或删成交掩盖，所有异常有停市/UNKNOWN 路径。
- [ ] 31.8 的扩展能力只消费本章事件，不绕过单市场撮合不变量。

这份清单全部通过，才可以把“能撮合几笔订单”称为生产级撮合引擎；否则它只是一个在正常行情下看起来工作的 demo。
