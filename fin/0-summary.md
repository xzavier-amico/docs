# 《金融系统与智能交易架构：从市场原语、核心账本到 AI-Agent 实战》· 总纲

> **一句话**：市面上讲金融的书不懂分布式代码，讲架构的书把账户写成 `UPDATE user SET balance = balance - 100`，讲 AI 炒股的教程敢让大模型裸调下单接口——本专题把**金融底层定价清算机制、Go + Java 双栈核心交易账务工程、生产级金融 AI-Agent 护栏架构**焊成一套完全独立、可直接上生产也能通关顶岗面试的硬核手册。

| 维度 | 设定 |
| --- | --- |
| 建议目录 | `fin/`（全书共 **6 大部分 + 导读 · 48 章**，持续更新完稿统计） |
| 目标读者 | 冲击支付/清结算、互联网券商、数字货币交易所、量化交易基础设施、FinTech AI-Agent 架构师与资深后端工程师 |
| 独立性公约 | **100% 独立成册，零外部依赖**。幂等、复式记账、分布式事务（TCC/Saga/Outbox）、多层对账、高并发推送、Agent 编排/RAG/MCP/HITL 全部在本专题内结合金融场景从底层讲透，绝不省略或推给其他专题 |
| 代码栈约定 | **第 01–19 章（金融基础篇）纯讲业务本质、数学账单与真实崩盘案例，0 代码**；**第 20–48 章（工程与 AI 篇）采用 Go 1.24+ 与 Java 21+ 双栈对照**，配完整 MySQL/PostgreSQL/ClickHouse SQL 与架构图 |

---

## 0.1 全书一把尺子：四个金融原语与五条工程生死红线

一切复杂的传统金融衍生品与链上 DeFi 协议，拆到底只有 **四个金融原语**：

| 原语 | 本质一句话 | 推演出的传统金融形态 | 推演出的加密金融形态 |
| --- | --- | --- | --- |
| **F1 账本与权益** | 谁拥有什么、谁欠谁什么（资产 ≡ 负债 + 权益） | 银行存款、股票登记、复式总账、Nostro 往来户 | UTXO、EVM 状态树、ERC-20 余额映射、稳定币储备 |
| **F2 跨期与贴现** | 今天的 1 块钱和明天的 1 块钱不等价 | 存贷款利率、国债收益率曲线、DCF 估值、期货基差 | 借贷池利用率曲线、质押生息（LSD）、永续合约资金费率 |
| **F3 流动性与做市** | 让你「此刻就能按合理价格买进卖出」是要付钱的 | 连续竞价订单簿（LOB）、做市商双边报价、买卖点差 | AMM 恒定乘积/集中流动性池、DEX 聚合路由、LP 无常损失 |
| **F4 风险切分与杠杆** | 把不确定性剥离、定价并卖给愿意承担的对手方 | 期货套保、期权非线性赔付（Greeks）、CDS、保证金强平 | 永续合约高倍杠杆、超额抵押清算、闪电贷、Delta 中性合成资产 |

一切金融软件与普通互联网软件的分水岭，在于 **五条不可妥协的工程生死红线**：

1. **算术红线**：严禁 `float`/`double` 碰任何一分钱；舍入规则、精度标度、尾差分摊必须在契约层显式写死。
2. **账本红线**：余额只是分录的物化视图；严禁无分录裸改余额，严禁 `UPDATE`/`DELETE` 已入账流水，错账只能反向冲正。
3. **未知态红线**：任何跨网络资金调用（支付扣款、银证转账、链上广播、AI 下单工具），**超时（Timeout）一律视为 `UNKNOWN`（状态未知），绝不允许直接当失败重试或回滚**，必须靠主动查单与异步回执收敛。
4. **风控前置红线**：任何委托进入撮合或上链签名之前，必须经过亚毫秒级、纯确定性代码的**事前硬风控闸门（Pre-Trade Risk Gate）**。
5. **概率隔离红线**：LLM / AI-Agent 只拥有「提案权（Trade Intent Proposal）」，绝不拥有「裸执行权」；模型与真金白银之间必须隔着确定性策略沙箱、幂等网关与可验证回执。

---

## 0.2 Go + Java 金融双栈核心库选型与避坑基准表（代码篇统一坐标）

第 20–48 章所有代码均明确给出生产选型、选它的理由以及最容易踩爆的坑：

| 领域 | Java 21+ 生产技术栈 | Go 1.24+ 生产技术栈 | 双栈必讲的高频深坑 |
| --- | --- | --- | --- |
| **高精度定点算术** | `java.math.BigDecimal` + `MathContext`；超低延迟侧 `long` 定点缩放（`decimal4j`） | `shopspring/decimal`（账务常规）、`cockroachdb/apd/v3`（可控上下文）、`holiman/uint256`（链上 Wei）、自研 `FixedInt64`（撮合零逃逸） | Java `new BigDecimal(0.1)` 精度污染、`equals` 含 scale（`1.0 != 1.00`）、无尽小数 `divide` 抛异常；Go `shopspring` 高频运算产生海量堆对象触发 GC 抖动 |
| **核心账务与存储** | Spring `TransactionTemplate`（禁用隐式注解自调用坑）、MyBatis-Plus / jOOQ、PostgreSQL / MySQL 8.4 | `pgx/v5`、`sqlc`（编译期强类型 SQL）、`database/sql` 显式 `Tx` | RR 隔离级别下间隙锁死锁、乐观锁高并发自旋打爆 CPU、从库延迟导致「查无此单」重复入账、JSON 数字反序列化成 `float64` 丢精度 |
| **极速撮合与无锁队列** | **LMAX Disruptor**、**Agrona** / Eclipse Collections（原始类型无装箱集合）、**Chronicle Queue**、**Aeron Cluster (Raft)** | 预分配紧凑结构体 Slab Arena、Cache-line Padding 单写者 RingBuffer、`google/btree`（泛型无装箱版）、`dragonboat` (Raft) | JVM 自动装箱 `Long` 引发 GC STW 尖刺；Go 指针切片引起三色标记扫描开销、伪共享（False Sharing）导致多核吞吐暴跌 10 倍 |
| **金融协议与行情网关** | **QuickFIX/J**（FIX 协议）、Simple Binary Encoding (**SBE**)、**Netty**（`ByteBuf` 零拷贝扇出） | **quickfixgo**、`gobwas/ws` / `coder/websocket` + `epoll` 多路复用、Protobuf VT | FIX 断线重连 `MsgSeqNum` 脑裂丢单；Netty `ByteBuf` 引用计数泄漏；单个慢客户端堵死 WebSocket 写缓冲拖垮整机内存 |
| **分布式一致性与对账** | RocketMQ 事务消息 / Kafka + Debezium CDC、Temporal Java SDK、ClickHouse JDBC | 自研事务 Outbox + `franz-go` / `sarama`、Temporal Go SDK、`clickhouse-go/v2` | TCC 空回滚与悬挂、Outbox 并发投递乱序、跨日 `23:59:59.999` 切日边界假短款、ClickHouse 未配 `join_use_nulls` 漏检差异 |
| **链上交互与密码学** | **Web3j**、**BitcoinJ**、BouncyCastle（secp256k1 / Ed25519 / Keccak256） | **go-ethereum (`ethclient`/`abigen`)**、`btcsuite/btcd`、`decred/dcrd/dcrec/secp256k1` | 只信 `Transfer` 日志被假代币欺骗、未检测 `parent_hash` 遭遇区块重组（Reorg）双花、并发提币 Nonce 冲突或低 Gas 卡死 Mempool |
| **金融 AI-Agent 栈** | **Spring AI** / **LangChain4j**、MCP Java SDK、Jackson JSON Schema 强校验、Resilience4j | **CloudWeGo Eino** / `langchaingo`、`mark3labs/mcp-go`、`invopop/jsonschema` | RAG 召回未来财报重述数据（未来函数）、流式 Tool Call JSON 截断、工具调用超时重试造成双倍买单、外部新闻隐藏 Prompt 注入 |

---

## 0.3 全局架构地图

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ 第五篇 · 金融 AI-Agent 层（认知与提案面，秒级，非确定性）                       │
│   PIT 时序金融 RAG ──► 多 Agent 红蓝对抗投研 / 组合 VaR 风险预警 / AML 调查     │
│                             │ 仅输出强类型 Trade Intent Proposal               │
├─────────────────────────────▼────────────────────────────────────────────────┤
│ 确定性安全隔离带（Hard Guardrails · 100% 纯代码，绝不允许 Prompt 绕过）          │
│   污点防注入隔离 ─► 仓位/回撤/价格偏离硬风控 ─► HITL 报价签名审批 ─► MCP 幂等网关│
├──────────────────────────────────────────────────────────────────────────────┤
│ 第四篇 · 核心交易与金融业务层（毫秒~微秒级，强确定性）                           │
│   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐ │
│   │ 支付网关/路由 │  │ 券商柜台/OMS │  │ 事前风控/强平│  │ 托管钱包/扫块防分叉│ │
│   └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └────────┬─────────┘ │
│          │                 ▼                 ▼                   │           │
│          │          ┌───────────────────────────────┐            │           │
│          │          │ 内存单线程确定性撮合引擎 (LOB) │──► 实时行情扇出网关     │
│          │          └──────────────┬────────────────┘            │           │
├──────────▼─────────────────────────▼─────────────────────────────▼───────────┤
│ 第三篇 · 金融工程与资金底座（一分不差，RPO = 0）                                │
│   定点算术与流水号 · 复式记账核心账本 · 全链路幂等 · TCC/Saga/Outbox · 清结算与四方对账│
├──────────────────────────────────────────────────────────────────────────────┤
│ 第一 & 二篇 · 金融机制地基（无代码：资产负债表 × 定价公式 × 清算规则）            │
│   传统金融（货币/微观结构/股/债/期/权/永续） × 链上金融（UTXO/EVM/稳定币/DEX/借贷/MEV）│
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 0.4 每章硬交付契约（防注水纪律）

* **基础篇（01–19 章，0 代码）每章必含七件套**：① 真实金融事件/崩盘引子 → ② 唯一真问题与反直觉定义（含误区表） → ③ 角色与资金/筹码流转图 → ④ 核心定价/结算手工算账推演（精确到每一分钱怎么变） → ⑤ 极端行情边界与失效条件 → ⑥ 行业术语正本清源 → ⑦ 一页速记 + 3 道自检推演题。
* **工程与 AI 篇（20–48 章，Go + Java 双栈）每章必含八件套**：① 线上资金事故引子（带时间线与亏损额） → ② 唯一真问题与系统边界 → ③ 架构/状态机/并发时序图 → ④ 完整表结构 DDL / 协议报文定义 → ⑤ **Go + Java 双栈核心生产代码与库选型** → ⑥ **深坑与极端边界拆解清单**（并发竞争、网络分区、切日、重放、回滚） → ⑦ 对账核验与压测验证方案 → ⑧ **面试系统设计 10 分钟答题卡**。

---

## 0.5 逐部分完整章节清单

### 导读（第 01 章｜纯机制，无代码）

| # | 章节 | 唯一真问题 & 核心干货 |
| --- | --- | --- |
| 01 | [一把尺子：四个金融原语、三张资产负债表与五条工程红线](01-four-primitives-and-five-redlines.md) | 账本、贴现、做市、杠杆四原语；用 T 型资产负债表统一拆解银行、券商、CEX、稳定币发行方与 DeFi 借贷池；五条不可妥协的工程红线。 |

### 第一部分 · 传统金融基础：货币、微观结构与衍生品（第 02–11 章｜纯基础，不写代码）

| # | 章节 | 唯一真问题 & 核心干货 |
| --- | --- | --- |
| 02 | [货币、信用、利率与跨境清算网：钱从哪来、怎么定价、如何跨国流转](02-money-credit-rates-and-cross-border.md) | 基础货币 vs 商业银行信贷派生；无风险利率与收益率曲线倒挂；DCF 贴现；SWIFT vs RTGS vs Nostro/Vostro 代理行与卡组四方清算网。 |
| 03 | [市场微观结构与金融机构全景：一笔交易背后的六个角色](03-market-microstructure-and-six-roles.md) | 交易所、券商、做市商、托管行、CCP 中央对手方、CSD 中登/DTCC；交易/清算/结算严格三分；T+2→T+1 与 DVP 券款对付。 |
| 04 | [订单簿、全系委托单与流动性博弈：价格到底是怎么撮合出来的](04-orderbook-order-types-and-liquidity.md) | 连续竞价 CLOB vs 集合竞价 vs RFQ vs 暗池；10 类委托单（Limit/Market/IOC/FOK/Post-Only/Stop-Limit/Trailing/Iceberg/TWAP/VWAP）；做市商点差与逆向选择毒流。 |
| 05 | [股票与权益市场：产权、复权算术、融资融券与公司行动](05-equities-adjustments-margin-and-actions.md) | 普通股/ADR/ETF 申赎套利；除权除息、拆合股与前/后复权精确算式；融资追保（Margin Call）与融券借券逼空机制（GME 复盘）。 |
| 06 | [固定收益、货币市场与回购：金融体系真正的流动性心脏](06-fixed-income-duration-and-repo.md) | 债券净价 vs 全价、计日基准（ACT/360 vs 30/360）与 YTM；久期 DV01 与凸性算账（SVB 破产拆解）；质押回购与流动性螺旋。 |
| 07 | [远期、互换与期货合约：保证金、逐日盯市与负油价交割边界](07-forwards-swaps-futures-and-delivery.md) | 远期、IRS、CDS（2008 AIG）与期货对比；IM/MM 与逐日盯市 MTM 五日推演；基差、Contango 展期损耗与 2020-04-20 WTI 负油价交割边界。 |
| 08 | [期权基础与定价直觉：非线性收益、平价公式与波动率曲面](08-options-pricing-parity-and-iv-surface.md) | Call/Put 四象限损益、内在与时间价值；美式提前行权边界；Put-Call Parity 无套利证明；BSM 动态 Delta 对冲直觉；IV Crush 与波动率偏斜。 |
| 09 | [期权 Greeks、组合策略与极端市场风险：从对冲到伽马挤压](09-options-greeks-strategies-and-gamma-squeeze.md) | Delta/Gamma/Theta/Vega/Rho 损益归因账本；价差/跨式/领口策略；做市商负 Gamma 对冲引发的 Gamma Squeeze；0DTE 收盘钉住风险（Pin Risk）。 |
| 10 | [加密原生衍生品：永续合约、资金费率、三价防插针与强平瀑布](10-perpetual-swaps-funding-and-liquidation.md) | 正向线性 vs 币本位反向合约凸性；资金费率（Funding Rate）与期现套利；指数/标记/最新三价防插针；全仓/逐仓与破产价→风险准备金→ADL 自动减仓瀑布。 |
| 11 | [金融风险全景与经典崩盘解剖：杠杆与流动性枯竭的宿命](11-financial-risk-taxonomy-and-crashes.md) | 四类金融风险、肥尾分布、VaR vs CVaR；四大崩盘复盘：1998 LTCM、2021 Archegos TRS 杠杆、2010 美股闪电崩盘、2012 骑士资本 4.4 亿美元事故。 |

### 第二部分 · 数字货币与链上金融基础：账本共识、智能合约与 DeFi 运作（第 12–19 章｜纯基础，不写代码）

| # | 章节 | 唯一真问题 & 核心干货 |
| --- | --- | --- |
| 12 | [数字货币底层运作原理：密码学产权、UTXO 与账户状态机](12-crypto-identity-utxo-and-account-model.md) | 非对称签名与 BIP-32/39/44 HD 派生直觉；比特币 UTXO 找零模型 vs 以太坊 Account 全局状态树在并发、Nonce、粉尘与审计上的本质差异。 |
| 13 | [公链共识、最终性（Finality）与 Layer2 扩容：链上到账到底要等多久](13-consensus-finality-rollups-and-bridges.md) | Mempool 与 EIP-1559 费用市场；PoW 概率最终性 vs PoS 经济最终性（Casper FFG/Slashing）与区块重组 Reorg 双花；OR 7 天挑战期 vs ZK 证明与跨链桥风险。 |
| 14 | [智能合约与代币运作机制：可编程托管、代币标准与可组合性](14-smart-contracts-token-standards-and-aa.md) | EVM 状态机、Gas 存储账与原子可组合性；ERC-20 `approve + transferFrom` 无限授权隐患与 `Permit`；ERC-4626 份额净值与通胀攻击；代理升级与账户抽象。 |
| 15 | [稳定币与 RWA 运转全解：链上美元怎么印出来、又怎么陷入死亡螺旋](15-stablecoins-mechanisms-depegs-and-rwa.md) | USDT/USDC 法币储备申赎与 SVB 脱锚传导；DAI 超额抵押清算与 PSM；Terra/LUNA-UST 死亡螺旋算账 vs USDe 期现对冲合成美元；链上美债 RWA 架构。 |
| 16 | [去中心化交易（DEX）与自动做市商（AMM）：没有买卖挂单怎么定价](16-dex-amm-concentrated-liquidity-and-il.md) | Uniswap V2 恒定乘积 $x \cdot y = k$、滑点与无常损失 IL 完整推导；Uniswap V3 集中流动性 Tick 区间与类期权敞口；Curve StableSwap；DEX 聚合与 CoW 意图拍卖。 |
| 17 | [链上借贷、闪电贷与质押衍生品：去中心化信用、清算博弈与循环杠杆](17-onchain-lending-flash-loans-and-lsd.md) | 资金池拐点利率模型、LTV 与健康因子清算折价；闪电贷单区块原子借还原理与套利账单；stETH 循环贷脱锚踩踏机制。 |
| 18 | [预言机、MEV 黑暗森林与链上衍生品：谁在透明交易池里收割你](18-oracles-mev-dark-forest-and-onchain-perps.md) | Chainlink 推送 vs Pyth 拉取 vs TWAP 与喂价操纵攻击；MEV 抢跑/尾随/三明治夹子利润算账；Flashbots 与 PBS；GMX 对手池 vs Hyperliquid 专有链订单簿。 |
| 19 | [CeFi vs DeFi 全维度对照：从 FTX 暴雷看托管、效率与储备金证明（PoR）](19-cefi-vs-defi-custody-and-proof-of-reserves.md) | 八维架构对照；FTX 挪用资产穿仓解剖；Merkle Tree 负余额作弊漏洞、Merkle Sum Tree + zk-PoR 证明与表外负债盲区。 |

### 第三部分 · 金融工程代码基座：精度、账本、幂等、一致性与对账（第 20–27 章｜Go + Java 双栈 + SQL）

| # | 章节 | 唯一真问题 & 核心干货 |
| --- | --- | --- |
| 20 | [金额表示、定点算术与尾差分摊：第一行金融代码就不能错](20-monetary-precision-rounding-and-allocation.md) | Java `BigDecimal` 八大坑与 `Money` 封装；Go `shopspring/decimal`/`apd/v3` 与零逃逸 `FixedDec`；银行家舍入；最大余数法尾差分摊与多级手续费截断对齐。 |
| 21 | [金融时间、交易日历与全局唯一流水号：顺序与身份的物理基座](21-financial-time-calendar-and-sequence-id.md) | UTC/交易所时区（美股夏令时坑）/会计日三分；多市场交易日历与 T+N 交收器；带分库基因位金融单号；抗时钟回拨 Snowflake 与双 Buffer 号段器。 |
| 22 | [复式记账核心账本引擎（上）：会计科目、DDL 约束与防透支并发控制](22-double-entry-ledger-schema-and-concurrency.md) | 科目表、多桶账户表、凭证与不可变分录 DDL；下单冻结→部分成交解冻实扣→撤单解冻完整分录；账号字典序加锁防死锁的 Go + Java 核心引擎。 |
| 23 | [复式记账核心账本引擎（下）：热点账户万级 TPS 削峰、冲正红冲与多币种汇兑](23-hot-accounts-reversals-and-fx-ledger.md) | 收支缓冲户、子账户分桶池、内存攒批组提交（Group Commit）突破 1,500 TPS 锁上限；红字冲正与蓝字补记；外币头寸过渡户维持各币种独立借贷平衡。 |
| 24 | [金融级全链路幂等体系：从客户端到渠道网关的零重入防线](24-end-to-end-financial-idempotency.md) | 查重窗口与锁过期穿透复现；预签令牌、同事务唯一索引、SHA-256 防篡改、部分退款派生键与回放状态机；Go/Java 双栈中间件与并发混沌测试。 |
| 25 | [金融状态机与分布式一致性实战：TCC、Saga 与事务 Outbox 的选型与防坑](25-state-machines-tcc-saga-and-outbox.md) | 显式表驱动状态机；资金冻结型 TCC 解决空回滚、防悬挂与幂等重放；事务 Outbox 解决并发跳号乱序；卡单巡检与安全补偿恢复引擎。 |
| 26 | [清结算系统架构：净额轧差、DVP 交收、多级分润与 7×24 不停机日切](26-clearing-settlement-netting-and-cutoff.md) | 多级分润与滚动保证金；双边/多边净额轧差算法；7×24 不停机逻辑日切（原子切日 Epoch、跨零点在途单归属锁定与试算平衡）。 |
| 27 | [多层对账引擎与差错闭环：实时双流核对、亿级批处理与跨日假差异消解](27-multi-tier-reconciliation-and-auto-healing.md) | 四方对账与内部自检；流式文件校验与Staging切换；亿级分桶并发对拍器与 ClickHouse `join_use_nulls=1` 反连接 SQL；跨日滚动挂账池自动消差。 |

### 第四部分 · 支付、券商、风控、撮合与链上交易核心架构（第 28–38 章｜Go + Java 双栈实战）

| # | 章节 | 唯一真问题 & 核心干货 |
| --- | --- | --- |
| 28 | [支付核心与多渠道收单网关：智能路由、未知态收敛、重付退回与逆向拒付](28-payment-gateway-routing-and-unknown-state.md) | EWMA 成功率智能路由与限额熔断；`UNKNOWN` 指数退避查单与回调竞争仲裁；一单多付自动退回挂账流程；部分退款与拒付追偿。 |
| 29 | [金融订单管理系统（OMS）与 FIX 协议网关：撤改竞争、部分成交与算法拆单](29-oms-fix-gateway-and-algo-execution.md) | OMS 状态机与增量成交均价算式；撤改单与部分成交回报乱序的冻结资金精确释放；`QuickFIX/J` & `quickfixgo` 补包与脑裂恢复；TWAP/冰山拆单器。 |
| 30 | [事前硬风控、实时保证金与毫秒级强平引擎：交易所与券商的防穿仓底线](30-pretrade-risk-margin-and-liquidation-engine.md) | 亚毫秒内存硬风控（胖手指价格带、四模式 STP 自成交防范）；价格分桶 $O(1)$ 爆仓索引；中位数标记价；分级强平→保险基金→ADL 减仓引擎。 |
| 31 | [低延迟内存撮合引擎（上）：OrderBook 数据结构、撮合规则与零 GC 实现](31-matching-engine-orderbook-and-zero-gc.md) | 单线程确定性 OrderBook（价格树 + 侵入式双向链表 + 预分配订单槽）；Limit/Market/IOC/FOK/Post-Only 撮合核心；Java Agrona 零 GC & Go Arena 零逃逸。 |
| 32 | [低延迟内存撮合引擎（下）：Disruptor 流水线、WAL 快照与 Raft 主备零丢单](32-matching-engine-disruptor-wal-and-raft.md) | 缓存行填充消伪共享；LMAX Disruptor 与 Go RingBuffer 并行落盘流水线；WAL + CoW 快照 + Aeron/Dragonboat Raft 主备 `RPO=0` 秒级切换。 |
| 33 | [实时行情系统架构：L2 深度增量拼装、流式 K 线聚合与百万 WebSocket 推送](33-market-data-l2-depth-kline-and-ws-fanout.md) | L2 REST 快照 + WS `[U, u, pu]` 断包恢复状态机；流式 K 线聚合与迟到修正；Netty & `gobwas/ws` 预编码零拷贝广播与慢消费者剔除。 |
| 34 | [券商核心业务架构：综合账户（Omnibus）、银证换汇、融资融券与公司行动引擎](34-brokerage-omnibus-fx-margin-and-actions.md) | Omnibus 总分核对与智能路由；TTL 锁汇与融资日终计息；公司行动引擎（派息扣税、拆合股碎股 8 位小数与对冲户、挂单清退、FIFO/洗售核算）。 |
| 35 | [数字货币托管与充提币系统（上）：HD 地址派生、MPC/HSM 签名隔离与冷热归集](35-crypto-custody-hd-wallet-mpc-and-sweep.md) | 离线根私钥 + 在线 `xpub` 派生；冷温热水位自动调拨；EVM ERC-20 归集 Gas 垫付状态机与 `CREATE2` 免部署归集；KMS/MPC 门限签名隔离。 |
| 36 | [数字货币托管与充提币系统（下）：高可靠扫块器、防区块重组（Reorg）与 Nonce 调度](36-chain-indexer-reorg-defense-and-nonce.md) | 假代币/假日志/回滚攻击防御；滑动区块头表检测 `parent_hash` 断裂自动执行 Reorg 冻结冲正；并发提币 Nonce 池与 EIP-1559 RBF 自动提价重发。 |
| 37 | [链上 DeFi 交互与稳定币清结算网关：合约绑定、Multicall、Permit 与防夹实践](37-defi-backend-multicall-permit-and-settlement.md) | 稳定币企业收单清结算架构；`abigen` & `Web3j` 封装；Multicall3 批量读、EIP-2612 `Permit` 免授权 Gas 扣款、`amountOutMin` 与私有防夹广播。 |
| 38 | [金融安全、反洗钱（AML/KYT）、密码学防篡改审计与两地三中心容灾](38-security-aml-hash-chain-and-multi-region-dr.md) | mTLS + 信封加密 + 四眼复核；制裁名单模糊匹配、拆分入账与 KYT 拦截；防篡改 HMAC 哈希链账本；两地三中心 `RPO=0` 与 Fencing Token 防脑裂。 |

### 第五部分 · 金融 × AI-Agent 架构实战：投研、风控、安全下单与链上 DeFAI（第 39–46 章｜Go + Java 双栈实战）

| # | 章节 | 唯一真问题 & 核心干货 |
| --- | --- | --- |
| 39 | [金融 AI-Agent 总架构：为什么概率大模型绝不能直连撮合与账本](39-fin-agent-architecture-and-three-plane-isolation.md) | 五大原生缺陷；「认知推理层 → 确定性硬护栏层 → 事务执行层」三层物理隔离架构；Go Eino 与 Java Spring AI/LangChain4j 强 Schema 金融底座。 |
| 40 | [金融时序感知 RAG（Point-in-Time RAG）：消灭未来函数、口径错位与财报幻觉](40-point-in-time-financial-rag-and-filings.md) | 财报表头单位绑定解析；双时间戳 Point-in-Time 硬过滤消灭财报重述未来函数；行情 SQL 与混合检索路由；数值与引用原文确定性核对器。 |
| 41 | [多智能体协同投研与因子挖掘：机构投委会红蓝对抗与防过拟合沙箱](41-multi-agent-research-debate-and-alpha-sandbox.md) | 多视角投研 + Bull/Bear 红蓝对抗 + 独立风控否决官；因子公式提议→隔离沙箱样本外 RankIC/Deflated Sharpe 检验；Token 预算熔断器。 |
| 42 | [AI 风险分析、组合压力测试与智能反洗钱（AML）引擎](42-neuro-symbolic-risk-var-and-aml-agent.md) | 神经符号混合风控：LLM 抽快讯宏观冲击情景 + Go/Java 确定性算子实时计算 VaR/CVaR/Greeks；自动化降仓开关；图查询取证与零幻觉 SAR 调查报告。 |
| 43 | [AI 下单核心工程（上）：交易意图契约（Trade Intent）、防间接注入与硬风控沙箱](43-ai-order-execution-intent-and-hard-guardrails.md) | `TradeIntent` 不可变契约；双 Agent 污点隔离防御新闻/代币名间接注入；100% 纯代码确定性硬风控沙箱（限额、最大回撤 Kill-Switch、BBO 价格偏离与冷却期）。 |
| 44 | [AI 下单核心工程（下）：金融 MCP 网关、分级审批（HITL）、超时幂等与实盘回执核对](44-financial-mcp-hitl-approval-and-idempotency.md) | 金融 MCP 分级能力网关；风险分级 HITL 审批状态机与**审批等待期价格漂移重算拦截**；`intent_id→ClOrdID` 超时先查单防重买；实盘回执刷新记忆。 |
| 45 | [链上 AI-Agent（DeFAI）工程实战：会话密钥（Session Keys）、EVM 预执行仿真与防钓鱼](45-onchain-defai-session-keys-and-simulation.md) | ERC-4337/Safe Session Keys 链上硬编码额度和合约/Selector 白名单；强制 Pre-flight EVM 仿真检查净资产变动与钓鱼授权检测；私有通道防夹上链。 |
| 46 | [金融 AI-Agent 的防穿透回测、仿真沙箱（Paper Trading）与监管级可解释审计](46-agent-backtesting-paper-trading-and-worm-audit.md) | 破解 LLM 预训练记忆泄露的盲测/样本外评测；L2 盘口级 Paper Trading 仿真撮合；四级实盘放量门禁；监管级 WORM 哈希链决策审计取证。 |

### 第六部分 · 收口：全局架构装配与高阶面试设计题库（第 47–48 章｜全栈收束）

| # | 章节 | 唯一真问题 & 核心干货 |
| --- | --- | --- |
| 47 | [从 0 到 1 搭建金融级系统：三类标杆架构（跨境支付 / 互联网券商 / 加密交易所 + AI）演进蓝图](47-end-to-end-financial-blueprints-and-evolution.md) | 三类金融系统安全分区与端到端装配；Go 与 Java 生产双栈最佳边界与共享 Protobuf 契约；L0→L4 换挡信号；砍掉 80% 预算也绝不能动的四块基石。 |
| 48 | [金融架构面试与疑难攻防全集：20 道压轴系统设计题 + 50 条反模式生死清单](48-interview-20-designs-and-50-antipatterns.md) | 20 道高频顶岗金融系统设计深挖（万级热点账/撮合热备/支付未知态/撤改竞争/连环强平/不停机日切/拆合股/Reorg/亿级对账/安全 AI 下单等）+ 50 条反模式速查。 |

---

## 0.6 阅读路线

| 路线 | 推荐章节顺序 | 读完能交付什么 |
| --- | --- | --- |
| **路线 A · 金融业务与产品通识（0 代码）** | `01` → `02–11`（传统金融）→ `12–19`（数字货币与 DeFi） | 能拆解任意金融衍生品与 DeFi 协议的资产负债表、损益曲面、清算机制与崩盘路径 |
| **路线 B · 支付清结算与核心账务后端** | `01–03` → `20–28` → `37–38` → `47` | 能设计并落地 Go/Java 复式记账引擎、热点账户、全链路幂等、TCC/Outbox、7×24 日切与亿级对账 |
| **路线 C · 券商、交易所与高并发撮合研发** | `04–10` → `20–23` → `29–34` → `38` → `47` | 能实现零 GC/Arena 内存撮合、Disruptor+Raft 热备、OMS/FIX、事前风控强平与百万 WS 行情扇出 |
| **路线 D · 链上托管、DeFAI 与金融 AI-Agent 架构** | `12–19` → `20–25` → `35–37` → `39–46` | 能构建防 Reorg 扫块与 MPC 托管、PIT 时序 RAG、防注入双 Agent 护栏、MCP 幂等下单与 Session Key 仿真网关 |
| **路线 E · 架构终面突击（1 周速通）** | `01` → `20, 22–27` → `28–32, 36` → `39, 43–45` → `48` | 直击 20 道顶岗金融系统设计压轴题与 50 条生死反模式 |

---

## 0.7 章节进度清单（`check.sh` 核对基准）

- [x] 01 一把尺子：四个金融原语、三张资产负债表与五条工程红线
- [x] 02 货币、信用、利率与跨境清算网：钱从哪来、怎么定价、如何跨国流转
- [x] 03 市场微观结构与金融机构全景：一笔交易背后的六个角色
- [x] 04 订单簿、全系委托单与流动性博弈：价格到底是怎么撮合出来的
- [ ] 05 股票与权益市场：产权、复权算术、融资融券与公司行动
- [ ] 06 固定收益、货币市场与回购：金融体系真正的流动性心脏
- [ ] 07 远期、互换与期货合约：保证金、逐日盯市与负油价交割边界
- [ ] 08 期权基础与定价直觉：非线性收益、平价公式与波动率曲面
- [ ] 09 期权 Greeks、组合策略与极端市场风险：从对冲到伽马挤压
- [ ] 10 加密原生衍生品：永续合约、资金费率、三价防插针与强平瀑布
- [ ] 11 金融风险全景与经典崩盘解剖：杠杆与流动性枯竭的宿命
- [ ] 12 数字货币底层运作原理：密码学产权、UTXO 与账户状态机
- [ ] 13 公链共识、最终性（Finality）与 Layer2 扩容：链上到账到底要等多久
- [ ] 14 智能合约与代币运作机制：可编程托管、代币标准与可组合性
- [ ] 15 稳定币与 RWA 运转全解：链上美元怎么印出来、又怎么陷入死亡螺旋
- [ ] 16 去中心化交易（DEX）与自动做市商（AMM）：没有买卖挂单怎么定价
- [ ] 17 链上借贷、闪电贷与质押衍生品：去中心化信用、清算博弈与循环杠杆
- [ ] 18 预言机、MEV 黑暗森林与链上衍生品：谁在透明交易池里收割你
- [ ] 19 CeFi vs DeFi 全维度对照：从 FTX 暴雷看托管、效率与储备金证明（PoR）
- [ ] 20 金额表示、定点算术与尾差分摊：第一行金融代码就不能错
- [ ] 21 金融时间、交易日历与全局唯一流水号：顺序与身份的物理基座
- [ ] 22 复式记账核心账本引擎（上）：会计科目、DDL 约束与防透支并发控制
- [ ] 23 复式记账核心账本引擎（下）：热点账户万级 TPS 削峰、冲正红冲与多币种汇兑
- [ ] 24 金融级全链路幂等体系：从客户端到渠道网关的零重入防线
- [ ] 25 金融状态机与分布式一致性实战：TCC、Saga 与事务 Outbox 的选型与防坑
- [ ] 26 清结算系统架构：净额轧差、DVP 交收、多级分润与 7×24 不停机日切
- [ ] 27 多层对账引擎与差错闭环：实时双流核对、亿级批处理与跨日假差异消解
- [ ] 28 支付核心与多渠道收单网关：智能路由、未知态收敛、重付退回与逆向拒付
- [ ] 29 金融订单管理系统（OMS）与 FIX 协议网关：撤改竞争、部分成交与算法拆单
- [ ] 30 事前硬风控、实时保证金与毫秒级强平引擎：交易所与券商的防穿仓底线
- [ ] 31 低延迟内存撮合引擎（上）：OrderBook 数据结构、撮合规则与零 GC 实现
- [ ] 32 低延迟内存撮合引擎（下）：Disruptor 流水线、WAL 快照与 Raft 主备零丢单
- [ ] 33 实时行情系统架构：L2 深度增量拼装、流式 K 线聚合与百万 WebSocket 推送
- [ ] 34 券商核心业务架构：综合账户（Omnibus）、银证换汇、融资融券与公司行动引擎
- [ ] 35 数字货币托管与充提币系统（上）：HD 地址派生、MPC/HSM 签名隔离与冷热归集
- [ ] 36 数字货币托管与充提币系统（下）：高可靠扫块器、防区块重组（Reorg）与 Nonce 调度
- [ ] 37 链上 DeFi 交互与稳定币清结算网关：合约绑定、Multicall、Permit 与防夹实践
- [ ] 38 金融安全、反洗钱（AML/KYT）、密码学防篡改审计与两地三中心容灾
- [ ] 39 金融 AI-Agent 总架构：为什么概率大模型绝不能直连撮合与账本
- [ ] 40 金融时序感知 RAG（Point-in-Time RAG）：消灭未来函数、口径错位与财报幻觉
- [ ] 41 多智能体协同投研与因子挖掘：机构投委会红蓝对抗与防过拟合沙箱
- [ ] 42 AI 风险分析、组合压力测试与智能反洗钱（AML）引擎
- [ ] 43 AI 下单核心工程（上）：交易意图契约（Trade Intent）、防间接注入与硬风控沙箱
- [ ] 44 AI 下单核心工程（下）：金融 MCP 网关、分级审批（HITL）、超时幂等与实盘回执核对
- [ ] 45 链上 AI-Agent（DeFAI）工程实战：会话密钥（Session Keys）、EVM 预执行仿真与防钓鱼
- [ ] 46 金融 AI-Agent 的防穿透回测、仿真沙箱（Paper Trading）与监管级可解释审计
- [ ] 47 从 0 到 1 搭建金融级系统：三类标杆架构（跨境支付 / 互联网券商 / 加密交易所 + AI）演进蓝图
- [ ] 48 金融架构面试与疑难攻防全集：20 道压轴系统设计题 + 50 条反模式生死清单
