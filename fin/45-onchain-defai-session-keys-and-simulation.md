# 45. 链上 DeFAI：会话密钥、执行授权与交易前 EVM 仿真

> **状态**：深入工程篇｜**所属**：第五部分·金融 × AI-Agent 架构实战｜**编号**：45 / 48
>
> **定位**：承接 [14 账户抽象](14-smart-contracts-token-standards-and-aa.md)、[18 MEV](18-oracles-mev-dark-forest-and-onchain-perps.md)、[36 重组与 nonce](36-chain-indexer-reorg-defense-and-nonce.md)、[37 DeFi 后端](37-defi-backend-multicall-permit-and-settlement.md)、[39 三平面](39-fin-agent-architecture-and-three-plane-isolation.md)。研究如何限制自动化执行，而不是教模型保管钱包。
>
> **代码契约**：PostgreSQL 15+ DDL 与 Go、Java 标准库核心例只演示证据绑定、整数预算和状态边界；不实现签名、ABI 解码器、节点客户端或可部署钱包。所有业务名称与事故均为虚构。
>
> **已验证范围**：独立预算例在 Go 1.19、Java 17 编译运行，输出均为 `3506000000 494000000`、`true`；不代表目标版本、数据库集成、EVM 仿真或链上执行已验证。
>
> **红线**：不配置钱包、不生成或记录真实私钥、不部署合约、不发交易；模型只有提案权。仿真通过不等于安全保证，私有广播不等于成交，授权撤销不等于已撤回全部在途效果。

## 45.1 虚构教学事故：交换成功，授权却留在了账户上

### 45.1.1 一条看似合理的自动执行链

**以下为合成教学事故，不对应任何已证实机构、漏洞或损失事件。**
假设 Agent 被允许通过固定适配器把 2,000.00 USDC 换为 WETH，所有价格仅用于本章演算。
工程团队声称“用了 ERC-4337、Safe、session key、仿真和私有 RPC，因此资金安全”。
实际只检查最外层路由地址，没有检查批次内部的授权、最终收款人和操作类型。

| 时间 | 事件 | 被误认的安全边界 |
|---|---|---|
| 09:00 | Owner 安装会话执行模块 | 把模块当成标准自带的低权限账户 |
| 09:01 | 模型建议调用可信 Router 的批处理入口 | 只检查最外层目标与 selector |
| 09:02 | 内层调用先给另一个 spender 无限授权，再交换 | 只看余额，不看授权状态差 |
| 09:03 | 固定区块仿真成功，收到 0.99 WETH | 把本次成功当作未来不会被转走 |
| 09:04 | 私有提交响应丢失，服务记为 FAILED | 释放在途预算，允许新意图 |
| 09:05 | 首次操作已上链，spender 另行拉走 3,000 USDC | 旧 allowance 不受应用侧 session 限额保护 |
| 09:06 | 团队停掉 Agent，仍以为权限全部失效 | 未处理链上 allowance、模块与在途请求 |

这里不是说任何 Router 都能任意控制钱包，而是假设该批处理模块确实允许钱包执行这些内层调用。
可利用性必须由具体账户、模块、路由编码和调用上下文证明，不能只凭函数名称推断。
攻击后的资产转移是 spender 使用既有 allowance 的独立调用，不再经过钱包的会话入口。
因此，即使后来补上会话入口的交易频率限制，也不会自动撤销已经授出的 token 权限。

### 45.1.2 本章要建立的可检验不变量

1. 会话有效权限是用户授权、链上模块规则与应用策略的交集，不是模型自述。
2. 授权覆盖完整调用语义：链、账户、目标、操作类型、参数、收款人、金额与有效期。
3. 所有会话共享的链上累计限额在执行时原子更新，不能靠单实例内存计数。
4. 仿真绑定固定状态和确切执行字节，余额、债务、授权、配置变化都进入证据包。
5. 广播未知与重组期间保留对应义务，不以超时为由创造第二个经济意图。
6. 模型、签名执行服务、策略管理者和紧急撤销者不共享超级权限。

## 45.2 ERC-4337、Safe 与 EIP-7702：先分清谁提供什么

### 45.2.1 ERC-4337 不内置统一的 session key 安全策略

ERC-4337 定义 UserOperation、EntryPoint、账户验证、Bundler 与可选 Paymaster 等机制。
账户实现自行解释签名与执行逻辑；标准没有替所有账户规定统一的会话密钥权限模型。
`validateUserOp` 验证通过，只能说明指定账户的验证逻辑接受了这份操作。
它不天然证明收款人正确、经济价值合理、调用树安全或未来不会出现 allowance 滥用。
Bundler 的准入仿真主要服务验证规则与费用可支付性，不能替代完整业务执行仿真。
部署时必须固定网络、EntryPoint 地址与版本、账户实现、模块版本和编码协议。
不能把不同 EntryPoint 版本的 UserOperation 哈希、字段和 RPC 示例混用。

### 45.2.2 Safe 的扩展能力不是免费获得的限制能力

Safe 基础账户的 owner/threshold 机制与启用模块的执行路径需要分别审计。
会话密钥通常由具体模块、验证器或权限框架实现，不是 Safe 核心默认附送的标准功能。
模块可通过模块执行入口产生交易，权限过大的模块本身可能接管资产。
“需要多个 owner 才能安装”只约束安装动作，不代表模块每次执行仍需相同阈值批准。
普通交易 Guard 与模块交易的检查覆盖可能不同，必须核对具体 Safe 版本及 module guard 支持。
不能假定给 owner 交易安装一个 Guard，就自动覆盖所有模块入口、fallback 或其他执行器。
列出全部可达路径：owner 交易、模块交易、4337 适配入口、fallback handler、自调用和升级入口。
LLM 会话通道不能启用新模块、修改 owner/threshold、替换 Guard、升级实现或提高自己的额度。
恢复与撤销路径由独立 owner 权限控制，并测试暂停状态下仍能执行必要的撤权操作。

### 45.2.3 EIP-7702 是持久代码委托，不是执行后清空

EIP-7702 允许 EOA 通过授权列表设置指向代码地址的 delegation indicator。
**最终规范中的委托会持久保留，绝不是一笔交易结束后自动清除。**
早期草案曾讨论临时代码，不能用早期介绍解释最终规范。
有效授权列表在交易执行前处理；后续执行 revert，也不会回滚已处理的委托设置。
委托地址为零的有效授权可清除代码委托，但不会神奇地清除全部 storage 或 token allowance。
改变委托代码也要考虑旧存储布局、初始化重放、代理实现与跨版本迁移风险。
7702 的协议级授权是代码委托，不是原生的“每日 100 USDC 会话许可”。
细粒度限制必须由所委托代码及其模块正确实现，错误实现可能暴露整个 EOA。
授权 tuple 的 chain ID 可为零或当前链；本章策略拒绝零值跨链通用授权，避免扩大重放范围。
tuple 本身不承诺全部业务 calldata，业务签名还必须另行绑定目标、value、数据和防重放域。
这里的代码委托与 EVM `DELEGATECALL` 是不同概念，不能因名称相似就视为相同权限机制。

| 机制 | 提供的基础能力 | 不自动提供的保证 |
|---|---|---|
| ERC-4337 | 可编程账户验证与 UserOperation 执行流程 | 统一 session 策略、盈利或无损保证 |
| Safe 模块 | 扩展账户执行逻辑 | 默认最小权限、所有路径都经过同一 Guard |
| EIP-7702 | EOA 持久代码委托 | 交易后自动复原、协议内建会话额度 |
| Paymaster | 代付费用的机制 | 用户无需承担 token 费用或信用风险 |

## 45.3 链上授权：限额、selector 与目标白名单必须组合使用

### 45.3.1 先固定可授权的动作语言

教学策略只开放一个审计过的定额交换适配器，不开放任意合约与任意 calldata 执行。
可信编译器把提案编译为结构化动作，再生成实际字节；模型不能提交可直接执行的 ABI 字节。
默认只许 `CALL`，且原生 `value=0`；原生币交换另建独立策略，不借例外放开全部 value。
验证 token、spender、recipient、amountIn、minAmountOut、deadline、路由和费用接收者。
recipient 必须为账户自身或明确白名单收款账户；不能只限制输入资产而允许任意出金地址。
空 calldata、短于 4 字节的数据、fallback、未知 selector、未知代理实现默认拒绝。
selector 只有 4 字节，可能碰撞，也不能表达动态参数约束；必须结合目标与正确 ABI 解码。
ABI 解码器检查偏移、长度、尾随字节、数组上界和规范编码，不能靠正则寻找地址。
白名单绑定 chain ID、目标地址和已审计实现；只存一个展示名称没有授权意义。
固定 code hash 仍不够：代理壳不变时 implementation 可以变化，还要识别升级与治理控制面。

### 45.3.2 递归路由、批次与 delegatecall 如何穿透表面白名单

| 入口形态 | 表面检查 | 可能遗漏的效果 | 默认处置 |
|---|---|---|---|
| `multicall(bytes[])` | 外层地址可信 | 内层 approve、任意收款、配置修改 | 用版本化解码器递归校验 |
| 通用 Router 命令流 | Router 在白名单 | 子命令指定任意目标、permit 或转账 | 仅开放少量明确命令 |
| 账户执行批次 | 每笔金额不超过单笔限额 | 多笔合计突破额度，某笔失败被吞掉 | 聚合检查并规定原子性 |
| `DELEGATECALL` | 目标库地址可信 | 在账户 storage 上下文改 owner 或限额 | 会话路径默认禁止 |
| 回调与重入 | 初始调用合法 | 回调递归重用剩余额度 | 重入防护、先计量后外调 |
| 代理与动态目标 | 外层 code hash 没变 | 实现升级、动态分派改变语义 | 锁定实现与升级策略 |

“递归解码”指静态解析已知编码中的内层动作，不意味着能预先枚举任意 EVM 动态调用。
动态路由、未知 hook、任意插件、无法证明限制的回调都应拒绝，或转为独立人工审查流程。
把仿真中的一次调用树当作未来全部可能调用树，是把观察样本误当成权限证明。
Safe 的某些批处理工具内部使用 delegatecall；若要例外支持，必须固定库版本并验证每个内层操作。
本章最小方案不实现这个例外，也不把“允许所有 delegatecall 到白名单 Router”当成补丁。
钱包外部合约内部的 delegatecall 属于另一审计面，应用通常无法逐条链上拦截其内部调用。
因此链上策略优先限制可达适配器与资产效果，而不是假设外部调用一定没有间接执行。

### 45.3.3 额度不是一个 amount 字段

- **单次额度**：输入资产最大扣减、原生 value、最大费用与所有批次子动作的聚合。
- **累计额度**：按账户、资产、窗口和策略版本计量，并防止多个 session 拆分绕过共享预算。
- **授权额度**：spender、token、允许的最大 allowance、到期和是否必须执行后归零。
- **配置额度**：会话没有 owner、模块、Guard、delegate、实现地址等管理权限。
- **频率与有效期**：调用次数、冷却期、链上时间窗口、撤销 epoch、nonce 域。

链上预算优先以每种资产最小单位计量；不能把 USDC 原子与 wei 直接相加。
若链上采用 USD 等值额度，必须另设可信价格、陈旧检测、保守折价和预言机失败处置。
“过去净支出”可能被闪电借入或短时回款粉饰；本例按输入资产总扣减计，不以回款恢复日额度。
每日窗口应固定链上时间边界；短期 session、换验证器或换 nonce key 不得清零账户共享累计额度。
链上累计扣减在外部调用前生效并做重入防护；同一批次只执行部分成功时必须按真实语义计量。
执行 revert 一般回滚账户执行范围内的状态，但 gas 仍可能消耗，不能把失败当作免费重试。
验证阶段与执行阶段的存储和回滚边界取决于具体 EntryPoint/账户流程，不能凭经验混算。
应用预算预占负责在途并发，链上模块负责最终执行约束；二者不是可以相互替代的副本。

### 45.3.4 授权差与撤销的边界

授权快照至少覆盖 ERC-20 allowance、Permit2、ERC-721/1155 operator、模块与验证器配置。
`approve(spender, max_uint)` 即使不立即扣币，也给未来创造了可消费能力，应作为高风险变化。
permit 签名可脱离当前调用单独使用；不能只检查本次交易中的 `approve` selector。
本例禁止无限授权，只允许精确金额的原子 approve→spend→归零，且验证 token 的实际语义。
对要求先归零的 token、非标准返回值、转账税、rebasing 和回调 token，需专用适配器或拒绝。
归零失败必须让整个业务执行回滚；“尽力清理”不满足本策略，但费用仍需另行核算。
session 过期、服务停机或删除本地 key 都不等于撤销链上已有的 token 授权。
紧急处置按权限路径分别撤销：session epoch、模块、allowance、operator、未消费签名与在途意图。
撤权交易本身也可能未上链、被重组或与攻击竞速；完成前对外只能报告撤销处理中。

## 45.4 Preflight EVM 仿真：固定 block，检查资产与权限差

### 45.4.1 可信证据包，而不只是一个 success=true

仿真从可信执行候选生成，覆盖实际账户入口、验证和执行路径，不能只模拟池子的 swap 函数。
固定 `chain_id + block_number + block_hash + state_root`，记录节点客户端、EVM fork 和仿真器版本。
优先按区块哈希读取；若接口只接受高度，前后校验哈希并验证所有子查询来自同一状态。
RPC 支持能力有差异，不能声称每个节点都支持同一 trace 或 state override 参数。
记录 from/to/value/data、gas 字段、nonce、EntryPoint、账户/模块实现、Paymaster 条件及批次顺序。
7702 候选还需覆盖授权列表处理与执行上下文；只对已委托账户做普通 eth_call 可能漏测初始化。
报告若使用签名占位或状态覆盖，必须显式列出，不能假装真实验签已经通过。
状态覆盖仅用于受控估计，不得以“补足余额、跳过权限、替换实现”产生可执行 PASS。
生产放行前，对最终可提交候选重验；任何经济字节、授权或费用语义变化都使旧证据失效。

```text
Proposal → 可信主数据与策略编译 → 不可变执行候选
                                │ canonical_bytes + intent_hash
固定区块状态 → 完整 EVM 执行 → trace + state diff + 错误与覆盖清单
                                │
资产/债务差 + 授权差 + 配置差 + gas/费用上界 → 确定性策略
                                │
链下预占 + 最终撤销/新鲜度检查 → 独立签名边界 → 受控广播
```

### 45.4.2 净资产变化必须与授权变化并列

对受支持资产集合，在同一固定价格快照 `p_B` 下计算：

```text
N_before = Σ(balance_before[a] × p_B[a]) − debt_before
N_after  = Σ(balance_after[a]  × p_B[a]) − debt_after
ΔN       = N_after − N_before − 未体现在余额差中的费用
ΔAuth    = 新增/扩大的 allowance、operator、模块、验证器与管理员权限
```

只有费用未进入资产差时才额外扣除；仿真若已扣真实 gas，再扣一遍会重复计算。
LP、借贷凭证、负债、抵押锁定资产不能只用钱包 ERC-20 余额估值；避免凭证和底层重复计价。
固定外部可信价格，不用被候选交易操纵后的池子 spot price 给自己定价。
同时检查逐资产毛流出、净流出、最低输出、收款人、债务增长和清算健康度。
净资产不变也可能把流动资产换成不可赎回或带锁定风险的凭证，需额外流动性策略。
未识别资产、无法估值债务、缺失 trace、未知 token 语义或解析失败，均标为 INCOMPLETE 并拒绝新风险。
不能只依赖 Transfer/Approval 日志：日志可能遗漏、伪造或不反映合约实际状态。
通用节点的原始 storage diff 也不会自动变成完整授权清单，需针对已支持合约解码与状态读取。
授权枚举不完整时必须披露覆盖缺口，不能声称检查过“链上所有授权”。

### 45.4.3 TOCTOU：检查时的世界不是执行时的世界

固定 block 的结果只说明给定状态、区块环境和候选执行条件下的行为。
从检查到使用之间，池子储备、预言机、nonce、余额、权限、实现、Paymaster 和排序均可变化。
恶意代码还能根据 timestamp、coinbase、basefee 或外部状态选择与仿真不同的分支。
重新仿真、短 TTL、最大状态陈旧窗口和多节点比较只能缩短风险窗口，不能消除 TOCTOU。
关键保护落在链上执行条件：minOut、deadline、固定 recipient、费用上限、精确授权、执行时累计额度。
链上暂停与撤销 epoch 在执行入口重验；链下看到“未撤销”不能保证下一块仍未撤销。
无法用链上条件表达的风险应保守限制规模或拒绝自动化，不用“多跑几次仿真”替代授权约束。

## 45.5 协议与 DDL：证据、预算、nonce 和 UNKNOWN

### 45.5.1 封存协议是内部契约，不是 RPC 发送样例

以下逻辑引用由可信主数据解析到地址；没有真实地址、签名、私钥或可广播 calldata。
全部原子金额用十进制字符串；哈希覆盖规范编码与引用解析后的完整清单，而非展示 JSON。

```json
{
  "schema_version": 1,
  "intent_id": "DEMO-45-001",
  "chain_id": "1",
  "account_ref": "DEMO_ACCOUNT",
  "session_ref": "DEMO_SESSION_E7",
  "policy_version": "P45-V1",
  "action": "FIXED_ADAPTER_EXACT_IN",
  "token_in_ref": "DEMO_USDC_6",
  "amount_in_atomic": "2000000000",
  "token_out_ref": "DEMO_WETH_18",
  "min_out_atomic": "985000000000000000",
  "recipient_ref": "DEMO_ACCOUNT",
  "gas_cap_wei": "3000000000000000",
  "snapshot_ref": "BLOCK_B_HASH_AND_STATE_ROOT",
  "expires_at_epoch_s": "2000000030"
}
```

链、账户、session 归属来自认证上下文，不能信任模型填写的 account_ref。
拒绝未知字段、重复 JSON 键、尾随对象、负数、指数写法、溢出、未知版本和未解析引用。
规范清单还绑定目标/实现、操作类型、calldata、value、授权 epoch、nonce 域与仿真摘要。
签名域必须绑定具体链、账户及对应验证协议；摘要算法与 UserOperation 哈希按版本分别定义。
内部 intent_hash 不冒充 userOpHash，费用替换形成新候选摘要并重新通过闸门。

### 45.5.2 PostgreSQL 教学骨架

此处预算固定 USD 微元，资产原子另存协议清单；生产还需逐资产额度表与完整账本。
`numeric(78,0)` 只提供容量，不自动保证 uint256 范围，因此额外添加上下界约束。

```sql
CREATE DOMAIN uint256 AS numeric(78,0)
  CHECK (VALUE >= 0 AND VALUE < power(2::numeric, 256));
CREATE TABLE defai_intent (
  tenant text NOT NULL, intent_id text NOT NULL,
  chain_id uint256 NOT NULL, account bytea NOT NULL CHECK (octet_length(account)=20),
  canonical_bytes bytea NOT NULL,
  intent_hash bytea NOT NULL CHECK (octet_length(intent_hash)=32),
  policy_version text NOT NULL, auth_epoch bigint NOT NULL CHECK (auth_epoch>=0),
  expires_at timestamptz NOT NULL,
  PRIMARY KEY (tenant, intent_id)
);
CREATE TABLE defai_preflight (
  tenant text NOT NULL, intent_id text NOT NULL, run_id text NOT NULL,
  candidate_hash bytea NOT NULL CHECK (octet_length(candidate_hash)=32),
  block_no bigint NOT NULL CHECK (block_no>=0),
  block_hash bytea NOT NULL CHECK (octet_length(block_hash)=32),
  state_root bytea NOT NULL CHECK (octet_length(state_root)=32),
  engine_version text NOT NULL, evidence_bytes bytea NOT NULL,
  evidence_hash bytea NOT NULL CHECK (octet_length(evidence_hash)=32),
  verdict text NOT NULL CHECK (verdict IN ('PASS','REJECT','INCOMPLETE')),
  PRIMARY KEY (tenant, intent_id, run_id),
  FOREIGN KEY (tenant, intent_id) REFERENCES defai_intent
);
CREATE TABLE defai_budget (
  tenant text NOT NULL, account bytea NOT NULL CHECK (octet_length(account)=20),
  bucket text NOT NULL, limit_micro bigint NOT NULL CHECK (limit_micro>=0),
  spent_micro bigint NOT NULL DEFAULT 0 CHECK (spent_micro>=0),
  reserved_micro bigint NOT NULL DEFAULT 0 CHECK (reserved_micro>=0),
  PRIMARY KEY (tenant, account, bucket),
  CHECK (spent_micro::numeric + reserved_micro::numeric <= limit_micro)
);
CREATE TABLE defai_execution (
  tenant text NOT NULL, intent_id text NOT NULL,
  chain_id uint256 NOT NULL, account bytea NOT NULL CHECK (octet_length(account)=20),
  nonce_scope text NOT NULL, nonce_value uint256 NOT NULL,
  reservation_micro bigint NOT NULL CHECK (reservation_micro>=0),
  state text NOT NULL CHECK (state IN
    ('RESERVED','READY','UNKNOWN','INCLUDED','FINALIZED','REVERTED','CANCELLED')),
  PRIMARY KEY (tenant, intent_id),
  UNIQUE (chain_id, account, nonce_scope, nonce_value),
  FOREIGN KEY (tenant, intent_id) REFERENCES defai_intent
);
CREATE TABLE defai_attempt (
  tenant text NOT NULL, intent_id text NOT NULL, attempt_no integer NOT NULL CHECK (attempt_no>0),
  candidate_hash bytea NOT NULL CHECK (octet_length(candidate_hash)=32),
  operation_hash bytea CHECK (operation_hash IS NULL OR octet_length(operation_hash)=32),
  tx_hash bytea CHECK (tx_hash IS NULL OR octet_length(tx_hash)=32),
  channel text NOT NULL, observed_at timestamptz NOT NULL DEFAULT now(),
  observation_bytes bytea NOT NULL,
  PRIMARY KEY (tenant, intent_id, attempt_no),
  FOREIGN KEY (tenant, intent_id) REFERENCES defai_execution
);
```

封存表与证据表禁止普通服务 UPDATE/DELETE，通过角色权限及不可变触发器落实；DDL 未展开权限配置。
执行状态更新用受控转换和版本 CAS；attempt 记录追加式观察，不能覆盖旧广播证据。
接单事务：先按唯一意图锁定，再锁预算行，检查摘要一致与可用额度，写预占、execution 和 Outbox。
同意图同摘要返回既有结果，同意图不同摘要报冲突；唯一键冲突回滚整个事务，不留下额外预占。
Outbox 与预占同事务提交；消费者持久化候选哈希及提交意图后，才跨越网络广播边界。
数据库与链无法原子提交，崩溃窗口必须通过 UNKNOWN、稳定执行标识与对账处理，而非宣称 exactly-once。
nonce_scope 区分 EOA 交易 nonce 与 EntryPoint/nonce key；EOA 交易 nonce 不因换通道而换命名空间。
4337 还绑定 EntryPoint 版本与 nonce key；nonce 防重放不是业务意图幂等，换 nonce 仍可能重复交易。

### 45.5.3 私有广播与最终性

私有通道减少公开 mempool 暴露，但 builder、relay、Bundler 仍有信任边界，不能承诺绝对保密或无 MEV。
服务特定的“不上链回滚交易”等承诺不能推广到所有网络、入口或公共回退路径。
私有广播 ACK 只说明服务接受请求；超时、查询无记录或取消 ACK 都不证明链上永远不会执行。
UNKNOWN 保留本金和费用义务；只读查 operation receipt、交易 receipt、账户 nonce 与规范链状态。
禁止超时后自动转公开广播、换 nonce 或换 intent_id；隐私策略和替换语义必须再次授权。
同一执行的费用替换复用原 nonce 约束，保留全部候选哈希；4337 的替换还受具体 Bundler 规则约束。
`INCLUDED` 记录 block hash、操作级成功与效果，不以外层 bundle receipt status 代替 UserOperation 结果。
`FINALIZED` 按指定链的最终性策略确认；L2 排序器接受、L2 收录、L1 提交与提款完成不是同一事件。
重组移除收录时回到待确认/UNKNOWN，冲销临时账务投影并继续持有义务，不删除历史观察。
只有权威证据证明已消费/失效且无未决替换，或执行达到结算条件，才释放对应预占。
本章 REVERTED 指达到所需确认条件的失败执行，仍结算实际费用；状态名本身不构成证明。

## 45.6 Go 与 Java 核心例：预算只做确定性算术

### 45.6.1 Go：差额比较避免溢出，先检查再预占

本例金额固定 USD 微元，且仅支持非负 int64；不拿 int64 假装覆盖链上 uint256。
两个语言使用同一算术口径；调用者仍须完成认证、仿真、签名协议与数据库串行化。

```go
package main

import "fmt"

type Budget struct { Limit, Spent, Reserved int64 }

func (b Budget) Free() (int64, error) {
    if b.Limit < 0 || b.Spent < 0 || b.Reserved < 0 || b.Spent > b.Limit {
        return 0, fmt.Errorf("invalid budget")
    }
    rest := b.Limit - b.Spent
    if b.Reserved > rest { return 0, fmt.Errorf("overcommitted") }
    return rest - b.Reserved, nil
}

func Reserve(b Budget, principal, fee int64) (Budget, error) {
    free, err := b.Free()
    if err != nil { return b, err }
    if principal < 0 || fee < 0 || principal > free || fee > free-principal {
        return b, fmt.Errorf("budget rejected")
    }
    b.Reserved += principal + fee // 已证明不超过 Limit，故不会溢出
    return b, nil
}

func main() {
    b := Budget{10_000_000_000, 6_000_000_000, 1_500_000_000}
    next, err := Reserve(b, 2_000_000_000, 6_000_000)
    if err != nil { panic(err) }
    free, err := next.Free()
    if err != nil { panic(err) }
    fmt.Println(next.Reserved, free) // 3506000000 494000000
    _, err = Reserve(next, 1_000_000_000, 3_000_000)
    fmt.Println(err != nil) // true
}
```

### 45.6.2 Java：同口径，预算结果不能成为授权令牌

```java
public class Budget45 {
    record Budget(long limit, long spent, long reserved) {
        long free() {
            if (limit < 0 || spent < 0 || reserved < 0 || spent > limit)
                throw new IllegalArgumentException("invalid budget");
            long rest = Math.subtractExact(limit, spent);
            if (reserved > rest) throw new IllegalArgumentException("overcommitted");
            return Math.subtractExact(rest, reserved);
        }
    }
    static Budget reserve(Budget b, long principal, long fee) {
        long free = b.free();
        if (principal < 0 || fee < 0 || principal > free || fee > free - principal)
            throw new IllegalArgumentException("budget rejected");
        long total = Math.addExact(principal, fee);
        return new Budget(b.limit(), b.spent(), Math.addExact(b.reserved(), total));
    }
    public static void main(String[] args) {
        Budget b = new Budget(10_000_000_000L, 6_000_000_000L, 1_500_000_000L);
        Budget next = reserve(b, 2_000_000_000L, 6_000_000L);
        System.out.println(next.reserved() + " " + next.free());
        // 3506000000 494000000
        try {
            reserve(next, 1_000_000_000L, 3_000_000L);
            throw new AssertionError("must reject");
        } catch (IllegalArgumentException expected) {
            System.out.println(true);
        }
    }
}
```

这些函数不是线程安全额度服务；两个请求从同一旧对象计算成功，仍会在没有事务时超卖。
链上原子金额在 Go 用 big.Int、Java 用 BigInteger 等严格无符号范围校验，禁止浮点解析。
生产十进制词法固定为 `0|[1-9][0-9]*`，拒绝前导加号、空格、负数、指数及超长输入。

## 45.7 可复算预算、测试、压测与对账

### 45.7.1 固定价格演算：资金效果与授权风险分开

设 USDC 有 6 位小数，WETH 与 ETH 为 18 位，教学价格为 USDC=1 USD、ETH/WETH=2,000 USD。
交易前资产是 10,000 USDC、0 WETH、1 ETH，无负债；净资产为 **12,000.00 USD**。
交换扣 2,000 USDC，收到 0.99 WETH，实际 gas 为 0.0024 ETH，即 4.80 USD。
交易后为 8,000 USDC、0.99 WETH、0.9976 ETH，净资产 **11,975.20 USD**。
故 `ΔN=−24.80 USD`，其中交换价差 20.00，gas 4.80；不是亏掉全部 2,000 本金。
minOut=0.985 WETH 只提供最低输出保护，不是承诺收到 0.99，也不是防止以后授权被滥用。
若同时出现无限 allowance，即使本次数值过闸，也因授权差独立拒绝。
事故中后续额外拉走 3,000 USDC，则已知净资产再减 3,000.00，合计下降 3,024.80 USD。
这里额外调用的 gas 假设由攻击方支付且不计入受害账户；不得把未计费用隐含为零风险。

应用日预算为 10,000 USD，已用 6,000、其他在途预占 1,500，可用 **2,500 USD**。
费用上限 0.003 ETH=6 USD，本意图预占 `2,000+6=2,006 USD`，剩余 **494 USD**。
再申请本金 1,000、费用上限 3，应拒绝；UNKNOWN 时也不能借走原预占。
按“输入本金总额+费用”口径结算 2,004.80，日已用变为 **8,004.80**。
其他预占仍为 1,500，可用 **495.20**；本意图释放费用差额 **1.20**，不按收到 WETH 恢复额度。
若把绕过入口的额外 3,000 也纳入事后异常支出观察，已用达到 11,004.80，超过日限额 1,004.80。
违规事实不能被预算表 CHECK 隐藏：写独立异常台账并冻结新风险，不篡改已发生金额以满足约束。

### 45.7.2 测试矩阵与教学压测门槛

| 类别 | 输入或故障 | 必须断言 |
|---|---|---|
| 解析/金额 | 重复键、负值、uint256 边界、语言 int64 边界 | 拒绝且不预占、不签名 |
| 授权 | 可信 Router 内嵌无限 approve 或任意 recipient | 递归语义拒绝，不能只验外层 |
| 执行上下文 | CALL 改为 DELEGATECALL、代理升级、恶意 callback | 旧许可不可复用或执行回滚 |
| 累计额度 | 多 session、不同 nonce key、重入、跨窗口 | 不突破共享链上限制 |
| 仿真 | 只验证未执行、trace 缺失、状态覆盖、授权枚举缺口 | 标为 INCOMPLETE，不放行 |
| TOCTOU | 仿真后储备变化、撤权、nonce 被消费 | 链上保护生效或重新拒绝 |
| 网络/崩溃 | ACK 丢失、广播后进程退出、取消响应丢失 | UNKNOWN 保留预占和稳定意图 |
| 最终性 | bundle 成功而 UserOperation 失败、区块被重组 | 按操作级效果对账，不提前结清 |
| 对账 | 直接 transferFrom 绕过 session、token 日志异常 | 资产/授权独立差异触发冻结 |

教学压测输入：100 个并发 worker 各提交 100 个独立意图，每个预占 10.03 USD。
独立空预算上限 100.00 USD，消费端暂停、没有释放，则恰好 9 个成功，占用 90.27，余 9.73。
对每个成功意图再重复投递 20 次，仍只有 9 条 execution 与 9 份经济预占。
重复同键不同摘要全部冲突；死锁重试必须保留原意图键，不能换键增加成功数。
分别测纯预算事务与隔离仿真队列：p50/p95/p99、锁等待、超时、队列年龄、RPC 错误及证据缺失率。
“预算事务 p99<50ms、仿真 p99<2s”只作练习目标，不是实测结果或生产 SLA。
压测不可连接真实钱包或主网广播；使用离线固定区块夹具、内存状态机与受控测试数据库。
按意图、候选、operation、交易、block hash 和资产事件多级对账，区分操作失败与资产转移失败。
资产账核对原子余额、债务与费用，权限账核对 allowance/operator/模块，预算账核对 spent/reserved。
三本账有差异时冻结相应自动化范围并保存证据；模型只能解释工单，不能自行补交易“抹平”。

## 45.8 术语、面试自检与官方来源

### 45.8.1 术语速查

| 术语 | 本章准确含义 |
|---|---|
| Session key | 具体账户实现授予的限域会话能力，不是 ERC-4337 统一规定的权限对象 |
| Delegation indicator | EIP-7702 写入账户的持久代码指针标记，不是交易结束即清空 |
| Selector / allowlist | 函数选择器与许可集合，必须结合参数、上下文及可达路径解释 |
| Preflight / state diff | 提交前执行检查及状态变化证据，不是只查一次 gas estimate |
| Authorization diff | 本次新增或扩大的未来可消费权限，不等价于本次余额变化 |
| TOCTOU | 检查与实际使用之间的状态变化窗口，仿真无法独自消除 |
| UNKNOWN / finality | 前者是结果不确定，后者是按指定链规则确认不可轻易回滚的程度 |

### 45.8.2 面试与上线自检

1. ERC-4337 的签名解释由谁定义？为什么 Bundler 验证通过不等于完整经济安全检查？
2. Safe 启用模块后，哪些路径可能不经过普通 Guard？版本与模块 Guard 怎样核实？
3. EIP-7702 委托是否持久？执行 revert、清零委托分别对代码、存储、allowance 有何影响？
4. 允许一个 Router 地址和一个 selector，为何仍可能开放无限授权、任意收款和配置修改？
5. 为什么默认禁止会话 delegatecall？静态递归 ABI 解码又为什么不等于分析全部 EVM 行为？
6. 净资产只下降 24.80 USD，为什么仍应因无限 allowance 拒绝？资产账与权限账如何分开？
7. 固定区块仿真需要保存什么？block number 相同但 block hash 不同时能否复用结论？
8. 私有广播超时后为何不能自动公开重发？receipt、nonce、UserOperation 成功与最终性怎样组合？
9. 用本章数字复算 494、495.20、1.20 与 1,004.80，分别属于哪个口径？
10. 10,000 个并发请求为何只应成功 9 个？重复投递、重组、撤权竞速怎样注入测试？

### 45.8.3 官方来源与核验边界

- [ERC-4337：账户抽象规范](https://eips.ethereum.org/EIPS/eip-4337)：核对账户接口、nonce、UserOperation、验证仿真及版本差异。
- [EIP-7702：Set Code for EOAs](https://eips.ethereum.org/EIPS/eip-7702)：核对持久委托、授权处理、执行失败不回滚委托及安全注意事项。
- [Safe Modules 官方说明](https://docs.safe.global/advanced/smart-account-modules)：核对模块执行入口、安装权限及恶意模块风险。
- [Safe Guards 官方说明](https://docs.safe.global/advanced/smart-account-guards)：结合具体合约版本核对 Guard 的实际覆盖范围。
- [Safe 智能账户官方合约](https://github.com/safe-global/safe-smart-account)：以固定 tag/commit 核对 ModuleManager、GuardManager 与执行路径。
- [EIP-712：类型化结构签名](https://eips.ethereum.org/EIPS/eip-712)：签名域隔离不是业务权限检查，也不自动提供完整重放保护。
- [EIP-1898：按区块哈希读取状态](https://eips.ethereum.org/EIPS/eip-1898)：核对固定状态读取语义及节点实际支持。
- [Ethereum JSON-RPC 官方说明](https://ethereum.org/en/developers/apis/json-rpc/)：核对 eth_call、receipt 与区块标签，不把调用成功解释为广播成功。
- [Flashbots Protect 官方说明](https://docs.flashbots.net/flashbots-protect/overview)：核对私有传播的产品边界、设置与信任假设，不能泛化为最终性保证。

规范与文档会演进；生产审查必须记录获取时间、链部署地址、合约 tag/commit 和文档内容摘要。
本章的拒绝策略与压测阈值是教学设计，不冒充 ERC 强制条款、Safe 默认配置或服务商承诺。
