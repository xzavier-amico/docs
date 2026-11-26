# 37. DeFi 后端工程：Multicall、Permit 与稳定币企业收单清结算

> **状态**：工程篇｜**所属**：第四部分·支付、券商、风控、撮合与链上交易核心架构｜**编号**：37 / 48
>
> **定位**：承接 [35 托管与签名隔离](35-crypto-custody-hd-wallet-mpc-and-sweep.md)、[36 扫块、重组与 Nonce](36-chain-indexer-reorg-defense-and-nonce.md)，把企业稳定币收单、自动换币和商户结算接入 [22 复式账本](22-double-entry-ledger-schema-and-concurrency.md)、[24 幂等](24-end-to-end-financial-idempotency.md) 与 [27 对账](27-multi-tier-reconciliation-and-auto-healing.md)。
>
> **覆盖**：合约身份绑定、同块批量读、Permit 边界、余额差、滑点与 MEV、模拟/签名/UNKNOWN/最终性、DDL 与事务协议、Go/Java 核心示例、可复算算例、混沌压测、自检答案和 37.8 衔接。
>
> **红线**：ABI 能解码不代表合约可信；Multicall 不是交易授权；Permit 不是免 Gas，也不是单次转账意图；模拟不是成交承诺；回执成功不是商户最终到账；本地超时不能解除已签名付款义务。

---

## 37.1 教学事故：收单成功，商户却少收了一笔钱

**以下为教学构造事故，不对应任何已确认历史事件；系统、时间及金额均为虚构。**

一家企业收单平台允许客户支付稳定币 A，平台换为稳定币 B，再按日向商户付款。
团队将“调用合约成功”直接映射为“订单结算成功”，把授权、交易、资金与会计混成一件事。

| 时间 | 错误实现 | 资金后果 |
|---|---|---|
| 09:00 | Web3j 绑定一个同名代币地址，未核验网络和代理实现 | 假资产订单进入正常收单 |
| 09:01 | 分别读取 `latest` 的余额、allowance 与报价 | 拼出从未同时存在的可执行状态 |
| 09:02 | Multicall 允许全部失败，把空结果填为零 | 风控误判无敞口，仍发起换币 |
| 09:03 | 宣传 Permit“免 Gas 且只能支付这张订单” | 用户留下可被 spender 使用的余额授权 |
| 09:04 | 用报价替代实际入账，未检查输出余额差 | 扣税代币或异常路由导致商户少收 |
| 09:05 | 私有 RPC 超时，改用新 Nonce 公开重发 | 两笔独立交易都可能支付成功 |
| 09:06 | 第一笔 receipt 为成功即结算 | 重组后链上资产消失，商户款已付出 |

### 37.1.1 业务模型先定清

本章采用**托管型企业收单模型**：付款人支付到受控收单合约/地址，平台确认资产后形成商户应付款。
平台按合规范围与商户合同换币、收手续费，再发起独立结算交易；链上转入不是天然完成法币收单。
非托管直达商户模式不应照搬“平台托管资产/商户负债”分录，需另定服务费与收入确认口径。
KYC/KYT、制裁筛查、稳定币冻结权、赎回资格、脱锚与当地支付牌照仍是独立控制，不因链上可达而消失。

### 37.1.2 六个资金不变量

1. 一张订单只接受约定网络、代币、付款条件和最终性策略下的一份有效经济履约。
2. 同一个订单的 permit nonce、业务 intent nonce、发送者交易 Nonce 各管各的，不互相替代。
3. 换币输出和手续费来自已验证的实际执行事实；显示报价不得直接生成商户可用应付款。
4. 一份结算义务最多被一条 canonical 链上的有效执行解除；替换尝试不能生成第二份义务。
5. 每个账本效果具有稳定 effect key；重组只能追加冻结、冲正和重放，不能删除分录。
6. 不能证明失败的签名、广播和结算保持受控冻结；资金及 Gas 费用均有可解释去向。

---

## 37.2 合约身份绑定：abigen / Web3j 只解决 ABI，不解决信任

### 37.2.1 建立可审计的合约注册表

| 绑定项 | 必须记录与验证 | 常见误区 |
|---|---|---|
| 网络 | 内部 network ID、chainId、genesis/可信 checkpoint、环境 | chainId 相同就是同一生产链 |
| 合约 | 20 字节地址、部署高度、运行时代码哈希 | 地址格式正确即可信 |
| 代理 | 代理类型、实现/Beacon、实现代码哈希、升级权限及监控 | 代理外壳代码没变即可继续用 |
| 接口 | 审核过的 ABI 摘要、生成器与依赖版本、能力矩阵 | 有 `permit` 选择器就符合 ERC-2612 |
| 资产 | 内部资产 ID、decimals、冻结/扣税/rebase/黑名单政策 | symbol 和 decimals 能识别资产 |
| 路由 | spender、router、入口函数、允许路径与收款端 | 任意 router calldata 都可透传 |

`decimals` 是展示换算配置，不是可靠的经济身份；上线审核后固定版本，变化进入隔离流程。
读取代码、代理槽和 domain 时必须绑定同一块；实现或权限异常变动时停止新签名，处理在途风险。
不是所有代理都使用 ERC-1967 槽；Diamond、自定义代理及可变路由应使用对应审核适配器。
代码哈希白名单也不能证明经济安全：可变存储、管理员权限、外部依赖和 delegatecall 仍影响行为。

### 37.2.2 生成代码与运行时校验分离

**命令模板，不可原样运行**：ABI 路径、包名与输出目录须在独立示例项目中替换；版本需锁定并评审。

```bash
abigen --abi reviewed/Token.abi --pkg binding --type Token --out gen/token.go
web3j generate solidity -a reviewed/Token.abi -o gen-java -p demo.binding
```

Go 的 `NewToken(address, backend)` 和 Java 的 `Token.load(...)` 都只是把方法编码器绑定到地址。
它们不会自动检查该地址属于哪个网络、是不是代理、permit 语义是否正确或是否已被升级。
签名器必须独立验证 `chainId/to/value/calldata` 与审批摘要；不能只信生成类名或上游订单号。
构建产物保存 ABI 哈希、依赖锁、生成命令与审核记录；不要运行时从区块浏览器抓 ABI 后立即签名。

---

## 37.3 Multicall3：批量读的同块快照与失败语义

### 37.3.1 三种“批量”不是同一能力

| 方法 | 一致性与限制 | 适用场景 |
|---|---|---|
| JSON-RPC batch + 各自 `latest` | 只是传输批量，不保证同一状态 | 不参与资金判断的监控 |
| 多次 `eth_call` + 固定 blockHash | 同一块末状态，仍需校验节点视图 | 分片大查询、独立验证 |
| 一次 `eth_call` 执行 Multicall3 | 一个 EVM 执行上下文中的顺序子调用 | 已审核只读方法集合 |

流程：取可信头 `(number, hash)` → 用该 hash 查询代码和状态 → 编码 `aggregate3` → 持久化请求及返回摘要。
RPC 支持 EIP-1898 时使用 `{"blockHash":"0x…","requireCanonical":true}`；不支持则拒绝资金关键路径或走明确审批的降级策略。
按高度查后再核对 hash 只能降低风险，不能完全排除查询过程中重组出去又回来的情况。
`requireCanonical` 只代表被询问节点当时的判断，不是共识证明，也不是最终性证明。
多批查询全部固定同一 hash，并保存 provider、时间、网络 epoch；其中一批不可用就不发布完整快照。

### 37.3.2 `aggregate3` 的具体语义

每项为 `Call3{target, allowFailure, callData}`，每项结果为 `Result{success, returnData}`。

| 调用 | allowFailure | 使用原则 |
|---|---|---|
| 付款余额、allowance、permit nonce | `false` | 任一失败，整组决策快照无效 |
| 必需的池状态与风险价格 | `false` | 不得用旧值或零值继续签名 |
| 展示用名称、非关键监控 | `true` | 明确标记 unavailable，禁止参与授权 |

`allowFailure=false` 遇子调用回退，会使整次聚合回退；`true` 仅允许返回失败项，不会把失败变成零值成功。
`success=true` 也要检查返回长度、ABI 类型和数值范围：向无代码地址 CALL 可能成功但返回空字节。
应用层还应验证结果数等于请求数，并用固定位置/调用 ID 对应，禁止跳过失败项后把结果错配。
`aggregate3` 不返回当前块 hash；原版 `tryBlockAndAggregate` 使用 `blockhash(block.number)`，该值为零，不能当快照身份证。
真实快照身份来自外层固定的 RPC blockHash 和已验证区块头，而非聚合结果里的“当前块哈希”。

### 37.3.3 `eth_call` 不等于所有内部调用都是 staticcall

原版 Multicall3 对目标使用 `CALL`，不是 `STATICCALL`；在 `eth_call` 中写入不会持久化，但会影响同次模拟后续子调用。
因此“同块快照”以审核过的只读方法为前提；恶意目标可能先修改模拟状态，再影响后续结果。
通过普通 CALL 进入目标时，目标所见 `msg.sender` 是 Multicall3，不是外部 `from`，不适合直接模拟依赖用户身份的方法。
不要给公共 Multicall3 任何 allowance，也不要向其留存资金：任何人都能调用它代为 transferFrom；有限授权同样可能被盗用。
其公开调用能力不提供订单授权；也不要把私钥签名的任意写调用直接搬入批次。
钱包通过 delegatecall 使用 Multicall3 是另一种上下文，涉及钱包权限和存储风险，不能与普通 CALL 混淆。
限制每批调用数、calldata/返回体字节、执行 Gas 与超时；一个巨量返回或耗 Gas 的目标可能拖垮整批。
按网络核验部署与运行时代码，不能因为常见地址相同就假定所有链都有可信部署。

---

## 37.4 Permit、真实执行和交易保护：授权不是支付

### 37.4.1 ERC-2612 究竟签了什么

标准 Permit 消息包含 `owner/spender/value/nonce/deadline`，EIP-712 domain 绑定链与验证合约等域信息。
有效执行修改 allowance 并消耗 owner 的 permit nonce，**不会自动执行 transferFrom，也不包含商户、订单或换币最小输出**。
签离线消息本身不消耗链上 Gas；提交 permit、转账与换币仍消耗 Gas，由用户、relayer 或其他赞助方承担。
Permit 的 deadline 限制该授权签名何时可被提交，不代表已经写入的 allowance 到期失效。
精确金额授权能缩小敞口，但在消耗前仍是 spender 的可支用额度，不等于“只允许支付一次这张订单”。

| 易混概念 | 本章要求 |
|---|---|
| ERC-2612 / DAI 风格 permit | 分别审核字段、typehash 与语义，不凭函数名通用编码 |
| EIP-712 / ERC-1271 | 前者定义结构化签名；后者定义合约签名验证，代币未必支持后者 |
| Permit2 | 另有额度转移/签名转移模型及授权层，不能当成 ERC-2612 同义词 |
| 三类 nonce | permit 防授权重放；intent 防业务重复；EOA Nonce 排序链上交易 |

付款意图需要另签受审核的结构，例如：

```text
PayIntent(networkDomain, tokenIn, maxInput, tokenOut, minOutput,
          merchantId, settlementReceiver, orderId, intentNonce,
          routerPolicyHash, feeCap, deadline)
```

这只是**自定义协议骨架**，不是现成标准；链上必须验证签名、域、有效期、额度、收款人及已消费 intent。
合约应以订单/intent 为幂等键，先验证权限并设置消费状态，再做受控外部调用；失败整体回退，并考虑重入防御。
如果允许改变 route，签的是受限制的路由政策，不是容许任意 calldata；商户和最终收款人不得由 relayer 任意覆盖。
重试相同意图不能再次扣款；部分履约则需链上累计已消费额度，不能只存一个布尔值。

### 37.4.2 Permit 被抢先提交怎么办

任何人都可能先提交同一份 Permit，消耗 permit nonce，使后续再次调用 permit 回退。
应用不能把这种回退一概解释为付款失败，也不能不加检查地吞掉所有异常。
可审核的入口可尝试 permit，再核验实际 allowance 足够，最终必须验证付款意图并完成 transferFrom。
仅看 allowance 足够就代任何 owner 付款是漏洞；allowance 是给 spender 的权力，不是当前调用者的业务授权。
撤销授权、消耗 nonce 与过期策略均须明确；本地删除签名不阻止已泄露签名上链。

### 37.4.3 余额差、滑点、期限与 MEV 的边界

合约内在受控执行前后读取同一资产余额，以整数计算 `actualReceived = after - before`，再检查其满足 minOutput。
调用者转入扣税资产时也需核验实际输入；对 rebasing、回调、恶意 balanceOf 和不可预测税费资产可直接拒绝接入。
余额差不是万能证明：需白名单资产、隔离会话、重入保护和明确接收地址，避免把第三方转入或 rebase 计入订单收入。
后端“交易前一块余额减后一块余额”会混入同块其他交易，不足以归因单笔执行；应读取可信合约事件/trace 并核对状态效果。
ERC-20 返回 false 或无返回值的处理应采用审核过的安全封装；不能只判断 EVM CALL 没回退。

| 控制 | 能控制什么 | 不能保证什么 |
|---|---|---|
| `minOutput` / `maxInput` | 链上执行的最差数量界限 | 公平价格、固定法币价值、稳定币不脱锚 |
| 链上 `deadline` | 超时拒绝指定入口执行 | 取消所有已签 EOA 交易或已生效 allowance |
| 私有广播 | 减少公开 mempool 暴露 | 中继/构建者不泄露、不审查、不夹击 |
| 模拟与 estimateGas | 对指定状态及输入做预检 | 下一块排序、未来价格或最终成功 |

滑点使用订单约定基准，而非攻击后已经偏离的现价；设置报价新鲜度、可信价格偏差阈值和最大金额。
私有路由超时转公开路由必须走策略审批，优先重广播**相同原始交易**，不能自行创建新付款意图。
同 Nonce 提费替换只改变批准的费用字段，不放宽 minOutput、不改收款人；更换经济内容必须重新授权。
夹子攻击仍可能在容忍范围内抽取价值，回跑和排序也可能存在；“私有”不等于没有 MEV。

---

## 37.5 DDL 与协议：把事实、授权、尝试和结算分开

### 37.5.1 PostgreSQL 核心表

以下 DDL 可在 PostgreSQL 15+ 的空 schema 执行，**仅是教学核心表**，不含完整账本、权限、分区及迁移方案。
地址与 hash 用二进制；金额用受约束的整数 numeric，绝不以浮点保存链上最小单位。

```sql
CREATE DOMAIN evm_address AS bytea CHECK (octet_length(VALUE) = 20);
CREATE DOMAIN hash32 AS bytea CHECK (octet_length(VALUE) = 32);
CREATE DOMAIN uint256 AS numeric(78,0)
  CHECK (VALUE >= 0 AND VALUE < power(2::numeric, 256));
CREATE TABLE contract_binding (
  binding_id uuid PRIMARY KEY, network_id text NOT NULL,
  address evm_address NOT NULL, config_version bigint NOT NULL,
  chain_id numeric(78,0) NOT NULL, checkpoint hash32 NOT NULL,
  runtime_hash hash32 NOT NULL, implementation_hash hash32,
  abi_hash hash32 NOT NULL, policy_hash hash32 NOT NULL,
  UNIQUE (network_id, address, config_version)
);
CREATE TABLE payment_intent (
  intent_id uuid PRIMARY KEY, merchant_id text NOT NULL,
  merchant_order text NOT NULL, network_id text NOT NULL,
  binding_id uuid NOT NULL REFERENCES contract_binding,
  token_in evm_address NOT NULL, token_out evm_address NOT NULL,
  owner evm_address NOT NULL, receiver evm_address NOT NULL,
  max_input uint256 NOT NULL, min_output uint256 NOT NULL,
  intent_digest hash32 NOT NULL, permit_digest hash32,
  deadline timestamptz NOT NULL, state text NOT NULL,
  version bigint NOT NULL DEFAULT 0,
  UNIQUE (merchant_id, merchant_order), UNIQUE (network_id, intent_digest)
);
CREATE TABLE execution_attempt (
  attempt_id uuid PRIMARY KEY, intent_id uuid NOT NULL REFERENCES payment_intent,
  network_id text NOT NULL, sender evm_address NOT NULL,
  tx_nonce uint256 NOT NULL, attempt_no integer NOT NULL CHECK (attempt_no > 0),
  tx_hash hash32 NOT NULL, calldata_hash hash32 NOT NULL,
  simulation_block hash32 NOT NULL, approval_hash hash32 NOT NULL,
  signed_blob_ref text NOT NULL, state text NOT NULL,
  UNIQUE (network_id, tx_hash), UNIQUE (intent_id, attempt_no)
);
CREATE INDEX attempt_nonce_lookup
  ON execution_attempt(network_id, sender, tx_nonce);
CREATE TABLE execution_observation (
  network_id text NOT NULL, block_hash hash32 NOT NULL,
  tx_hash hash32 NOT NULL, event_ordinal integer NOT NULL CHECK (event_ordinal >= 0),
  intent_id uuid NOT NULL REFERENCES payment_intent,
  receipt_status smallint NOT NULL CHECK (receipt_status IN (0,1)),
  actual_input uint256 NOT NULL, actual_output uint256 NOT NULL,
  gas_cost uint256 NOT NULL, canonical boolean NOT NULL,
  finality_evidence text, network_epoch bigint NOT NULL,
  PRIMARY KEY (network_id, block_hash, tx_hash, event_ordinal)
);
CREATE TABLE settlement_obligation (
  settlement_id uuid PRIMARY KEY, intent_id uuid NOT NULL REFERENCES payment_intent,
  effect_key text NOT NULL UNIQUE, asset_id text NOT NULL,
  gross uint256 NOT NULL, fee uint256 NOT NULL, net uint256 NOT NULL,
  state text NOT NULL, version bigint NOT NULL DEFAULT 0,
  CHECK (gross = fee + net), UNIQUE (intent_id, asset_id)
);
CREATE TABLE settlement_outbox (
  effect_key text PRIMARY KEY, aggregate_id uuid NOT NULL,
  expected_version bigint NOT NULL, network_epoch bigint NOT NULL,
  payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
  delivered_at timestamptz
);
```

该表组不单独分配 Nonce：发送者槽位由第 36 章调度器唯一持有 `(network,sender,nonce)`，多次尝试引用同一槽位。
应用必须在事务中核验 intent.network 与 binding.network 一致、精度一致、deadline 对应签名中的秒数。
外键只保证行存在，不保证资产语义、授权合法或状态迁移；生产需状态枚举、CAS、权限和不可变字段保护。
`numeric(78,0)` 可能对小数输入舍入，入口必须先验证十进制整数字符串，不能靠列类型发现小数金额。
已签名原文应加密存储于受控设施，数据库只放引用；permit/intent 签名同样不能进普通日志。

### 37.5.2 状态与短事务协议

```text
CREATED → VALIDATED → RESERVED → SIMULATED → SIGNING → SIGNED
                                                     ↓
                                               BROADCAST / UNKNOWN
                                                     ↓
                               INCLUDED_SUCCESS / INCLUDED_REVERTED
                                                     ↓
                                   FINALIZED → ACCOUNTED → SETTLED
```

这里的 `SETTLED` 表示独立商户出款已按政策最终完成，不是换币交易成功；生产应拆成执行、会计和付款三个状态维度。
`INCLUDED_*` 遇重组转为待重新观测，保留旧分支记录；最终性冲突触发熔断，不自动当普通回滚。

1. **接单事务**：订单键去重，校验相同键请求摘要相同，创建不可变意图；跨服务冻结用幂等命令和回执衔接。
2. **模拟阶段**：事务外获取固定块快照，以真实 `from/to/value/data` 模拟整个执行入口，记录返回值、日志/trace 与策略版本。
3. **签名前事务**：CAS 版本，校验冻结回执、快照新鲜度、network epoch、路由政策；领取唯一 Nonce 槽位与费用预算。
4. **签名阶段**：签名器复核完整交易摘要与独立审批，使用幂等签名请求；签名超时转 SIGNING_UNKNOWN 并查询原请求。
5. **广播前事务**：先持久化 attempt、txHash 和加密原文引用，再允许发送；崩溃后恢复同一尝试，不创建新订单。
6. **广播与观测**：超时进 UNKNOWN，按所有已签尝试查 receipt、发送者槽位及可信索引，允许相同原文重广播。
7. **最终入账事务**：验证 canonical、最终性、实际输出和 epoch，CAS 生成义务与 Outbox；账本按 effect key 去重并回传凭证。
8. **商户付款**：独立审批和冻结，复用第 35/36 章付款协议；最终性后解除应付款，银行侧则按银行结算证据处理。

receipt status=0 通常表示状态效果回退但 Gas 已消耗；须达到对应最终性、确认无其他仍可付款尝试后才释放本金。
查不到交易、`nonce too low`、中继拒收、交易从池中消失都不能单独证明失败；可能已经被别的分支或节点执行。
EOA 交易没有通用过期字段；即使业务 deadline 已过，仍可能上链回退并花 Gas，或触发未带期限检查的其他入口。
有链上期限且所有可执行路径都受其约束时，可按最终链时间证明经济动作已不可执行，但仍须保留 Nonce/Gas 清理责任。

---

## 37.6 Go 与 Java 核心代码：运行样例和集成骨架分开

### 37.6.1 Go：可运行的整数滑点与结算核算

**可独立运行，Go 1.22+，仅标准库**。复制为临时目录的 `main.go` 后执行 `go run main.go`。
它只核算本章数字，不读链、不验签、不发交易，不应被包装成生产收单 SDK。

```go
package main

import (
    "fmt"
    "math/big"
)

func integer(s string) *big.Int {
    x, ok := new(big.Int).SetString(s, 10)
    if !ok || x.Sign() < 0 { panic("invalid unsigned integer") }
    return x
}

func floorBps(x *big.Int, bps int64) *big.Int {
    if bps < 0 || bps > 10000 { panic("invalid bps") }
    return new(big.Int).Quo(
        new(big.Int).Mul(x, big.NewInt(bps)), big.NewInt(10000))
}

func main() {
    quote := integer("999000000")
    minOut := floorBps(quote, 10000-30)
    before, after := integer("5000000000"), integer("5997500000")
    received := new(big.Int).Sub(after, before)
    if received.Sign() < 0 || received.Cmp(minOut) < 0 {
        panic("output below approved minimum")
    }
    fee := floorBps(received, 20)
    net := new(big.Int).Sub(received, fee)
    gasWei := new(big.Int).Mul(integer("210000"), integer("20000000000"))
    // 仅本例：ETH/USD = 2500，B/USD = 1，B 精度为 6。
    gasBUnits := new(big.Int).Quo(
        new(big.Int).Mul(gasWei, integer("2500000000")), integer("1000000000000000000"))
    fmt.Println("minOut", minOut, "received", received)
    fmt.Println("fee", fee, "net", net)
    fmt.Println("gasWei", gasWei, "gasBUnits", gasBUnits)
}
```

输出应为 `minOut 996003000 received 997500000`、`fee 1995000 net 995505000`、`gasWei 4200000000000000 gasBUnits 10500000`。
`big.Int` 会修改接收者，示例每次用新接收者避免意外覆盖输入；生产需明确 uint256 上界及中间乘法溢出语义。

### 37.6.2 Go：Multicall3 固定块调用集成骨架

**以下不可独立编译**：`multicallABI/calls/pinnedHash/client` 是已审核 ABI、Call3 切片、可信头和 go-ethereum RPC 客户端。
结构体字段及 tuple ABI 必须与生成版本一致；正式工程须补网络/代码校验、超时、限流及严格结果解码。

```go
// 调用点骨架；不发送交易，不用 CallContract(..., nil) 的 latest 默认值。
data, err := multicallABI.Pack("aggregate3", calls)
if err != nil { return err }
var raw hexutil.Bytes
err = client.CallContext(ctx, &raw, "eth_call",
    map[string]any{"to": multicallAddress.Hex(), "data": hexutil.Encode(data)},
    map[string]any{"blockHash": pinnedHash.Hex(), "requireCanonical": true})
if err != nil { return fmt.Errorf("snapshot unavailable: %w", err) }
decoded, err := multicallABI.Unpack("aggregate3", raw)
if err != nil { return err }
// 继续验证结果数、每项 success、字节长度、uint256 范围和请求位置。
// decoded 不能直接赋值给余额；任一必需项失败，整个决策快照无效。
_ = decoded
```

### 37.6.3 Java：Web3j 合约绑定与人工网关边界

**以下为 Java 21 / Web3j 集成骨架，不可独立运行**；`Token` 是审核 ABI 生成类，`registry/snapshotRpc/decoder` 为待实现的安全适配器。
示例故意不含私钥，也不调用生成类的 `.send()` 写交易方法。

```java
// 方法体骨架：先验证身份，再建立只读绑定；不是 Token.load 自动验真。
var binding = registry.requireApproved(networkId, tokenAddress, configVersion);
registry.verifyNetworkCheckpoint(web3j, binding);
registry.verifyCodeAndImplementationAtHash(web3j, binding, pinnedHash);
Token token = Token.load(binding.address(), web3j, readOnlyTxManager, gasProvider);

// 生成的 balanceOf(...).send() 通常走默认块，不能据此假设已经固定块。
var fn = new org.web3j.abi.datatypes.Function(
    "balanceOf",
    java.util.List.of(new org.web3j.abi.datatypes.Address(owner)),
    java.util.List.of(new org.web3j.abi.TypeReference<
        org.web3j.abi.datatypes.generated.Uint256>() {}));
String input = org.web3j.abi.FunctionEncoder.encode(fn);
String raw = snapshotRpc.ethCallByHash(binding.address(), input, pinnedHash, true);
java.math.BigInteger balance = decoder.requireUint256(raw);
if (balance.compareTo(requiredInput) < 0) throw new IllegalStateException("insufficient");
// 付款 calldata 交独立策略/签名网关；禁止任意目标+任意 data 透传。
```

`ethCallByHash` 须实际发出 EIP-1898 对象参数；不能把普通高度参数方法改个名字就宣称支持 hash 固定。
Java 金额使用 `BigInteger`，展示用 `BigDecimal` 显式舍入；禁止 `double` 或 `long` 承接任意 uint256。
跨 Go/Java 用同一组 ABI、EIP-712 digest 与签名恢复测试向量，验证字节级一致，而不是只比较最终显示金额。

---

## 37.7 可复算账务、对账、混沌压测与面试自检

### 37.7.1 算例：1,000 A 收单、换 B、扣费与商户结算

假定 A、B 都为 6 位精度、白名单无扣税无 rebase；仅本例按 A、B 均为 1 USD 估值。
付款输入为 `1,000,000,000` 最小单位；报价为 999 B，滑点 30 bps；手续费按实际输出的 20 bps 向下取整。

| 项目 | 可复算公式 | 结果 |
|---|---|---|
| 链上最小输出 | `floor(999,000,000 × 9,970 / 10,000)` | 996,003,000，即 996.003 B |
| 实际输出余额差 | `5,997,500,000 - 5,000,000,000` | 997,500,000，即 997.5 B |
| 平台手续费 | `floor(997,500,000 × 20 / 10,000)` | 1,995,000，即 1.995 B |
| 商户应付款 | `997,500,000 - 1,995,000` | 995,505,000，即 995.505 B |
| 网络费 | `210,000 Gas × 20 gwei` | 0.0042 ETH |
| 网络费估值 | `0.0042 × 2,500 USD/ETH` | 10.5 USD，仅为会计估值 |
| 手续费减本笔 Gas | `1.995 - 10.5` | -8.505 USD，不含其他成本 |

本例约定换币损耗由商户承担，Gas 由平台承担；若合同承诺固定 B 到账或由商户承担 Gas，义务和损益必须重算。
最终收到 1,000 A 时：借托管 A 资产 1,000，贷商户待结算 A 负债 1,000；换币期间先冻结对应负债。
换币最终后：贷 A 资产 1,000、借 B 资产 997.5、借汇兑/执行损失 2.5（按本例统一估值口径）。
按合同重计商户负债：借商户 A 负债 1,000，贷商户 B 应付款 995.505、手续费收入 1.995、执行损失转嫁回收 2.5。
上述损失与回收净额为零，明确表达商户承担的 2.5；不得既少记商户负债，又重复向商户扣一笔换币费。
Gas：借网络费费用 10.5，贷 ETH 资产 0.0042（本例账面值 10.5）；实际账面值差异另入损益，不拿 B 资产抵消。
商户付款最终后：借商户 B 应付款 995.505，贷 B 托管资产 995.505；剩余 B 资产 1.995 对应平台收入。
这组多币种分录是教学口径，正式账套须按功能货币、重估政策和合同确认，不可把不同币种数量直接相加试算平衡。

### 37.7.2 四层对账与处理责任

| 对账层 | 核对键及等式 | 不平时动作 |
|---|---|---|
| 链上事实 | network/blockHash/txHash/事件序号、canonical 与最终性 | 隔离分支，暂停释放，交索引恢复 |
| 执行效果 | intent 与实际输入/输出、fee、Gas、失败原因 | 对拍 trace/受信日志，禁止用报价补差 |
| 会计凭证 | `gross = fee + net`，effect key 与借贷试算 | 重投幂等 Outbox 或追加冲正，不直接改余额 |
| 商户付款 | 义务、冻结、付款尝试、最终凭证与对账单 | UNKNOWN 继续查证，禁止二次独立付款 |

余额对账使用同一 hash 的期初/期末与区间完整流量；只核对 `Transfer` 不足以覆盖 rebase、冻结、手续费等机制。
Outbox 已投递不代表账本已入账，应保存账本业务回执；商户对账单区分收单日、换币日、最终确认日与付款日。
脱锚、拒绝赎回、黑名单冻结属于资产/信用事件，不是靠“重试交易”可以修复的技术错误。

### 37.7.3 混沌与压测验收矩阵

| 注入 | 预期安全结果 | 验收证据 |
|---|---|---|
| batch 中一项回退/空返回/超大返回 | 必需快照整体拒绝，非关键项标不可用 | 零次越权签名，字节/Gas 限额生效 |
| 查询途中重组、代理升级、RPC 换链 | hash/配置校验拒绝，停止新签名 | epoch 屏障与告警完整 |
| permit 被抢跑、nonce 被外部消耗 | 不重复授权，不绕过 intent 校验 | 余额、allowance 与订单消费状态一致 |
| 签名超时、持久化后宕机、广播超时 | 恢复原签名请求/attempt，不换意图付款 | 相同 effect key 仅一个有效经济效果 |
| 私有提交后公开可见、同 Nonce 替换竞赛 | 监控所有尝试，只认 canonical 胜者 | 不因旧 txHash 未找到而释放本金 |
| receipt 重复、事件乱序、最终性停滞 | 去重、等待、不用墙钟补最终性 | 可用应付款不提前增长 |
| Outbox 重投、账本回执丢失、DB 主备切换 | 幂等收敛，未知义务保持冻结 | 链、账、商户单三方最终一致 |

负载至少包含 1/10/100 项批次、不同返回体、热点商户、100% 必需项失败和 RPC 限流；数字是测试维度，不是吞吐承诺。
记录批量读取 P95/P99、签名等待、UNKNOWN 数量/最长年龄/金额、最终性滞后、Outbox 积压和未平差额。
分别统计可用性与正确性：故意熔断会降低成功率，但验收必须保证没有重复扣款、越权付款及未最终资金释放。
恢复测试要证明受控收敛及责任人可接管；外部依赖长期故障时允许保持冻结，不应为追求“最终成功率”强制付款。

### 37.7.4 术语、面试与自检答案

| 问题/术语 | 自检参考答案 |
|---|---|
| abigen / Web3j 提供什么？ | 类型化 ABI 编解码与地址调用包装；不证明合约身份和权限安全。 |
| 同块快照是否就是最终性？ | 不是。它保证查询状态坐标一致，最终性另需链特定证据。 |
| allowFailure 能掩盖失败吗？ | 不能。它只决定聚合是否回退；应用仍要检查逐项结果与 ABI。 |
| Permit 为什么不是免 Gas？ | 离线签名不付 Gas，但链上写 allowance 与执行仍需付费方。 |
| Permit deadline 到了 allowance 会归零吗？ | 不会。标准限制签名提交时间，不自动撤销已生效额度。 |
| 单次 Permit 等于单次付款吗？ | 不等于。还需绑定订单、接收人、限价与链上消费状态的业务意图。 |
| 为什么余额差比报价可靠但仍有限？ | 它反映受控执行实际到账；rebase、重入、第三方转入和恶意代币仍需限制。 |
| 模拟成功为何还能失败？ | 状态、排序、时间、Gas 和代理实现均可能变化，模拟不是预留执行权。 |
| UNKNOWN 能否换 Nonce 重付？ | 不能。先恢复全部已签尝试和槽位事实，排除双付；同原文可按政策重广播。 |
| receipt status=1 能否放商户款？ | 还要验证实际效果、canonical、最终性、风控与账本回执。 |
| 私有广播消灭 MEV 吗？ | 不会；它减少公开暴露，但引入中继信任，且不能保证排序与价格公平。 |
| finality / settlement 的区别？ | 前者是链历史不可逆程度的证据，后者是合同义务按约定方式被解除。 |

---

## 37.8 小结、上线闸门与协议来源

完整闭环是：**可信身份 → 同块状态 → 明确授权 → 模拟与冻结 → 隔离签名 → 可恢复广播 → 实际执行 → 最终性 → 账本 → 商户结算**。
上线前必须能从任意订单反查 ABI/配置、快照 hash、Permit 与 intent 摘要、全部交易尝试、最终性证据、凭证和付款义务。
只要仍靠“模拟成功”“RPC 没查到”“授权签过一次”来推导付款完成，就还没有建立资金系统的证明链。
与后续工程主题衔接时，应复用这里的不可变意图、版本化策略、UNKNOWN 和 effect key，而非另建绕过账本的链上快捷通道。

协议与实现来源（接口需结合锁定版本和目标网络复核）：

- [ERC-20](https://eips.ethereum.org/EIPS/eip-20)：余额、allowance、返回值与事件接口。
- [ERC-2612](https://eips.ethereum.org/EIPS/eip-2612)、[EIP-712](https://eips.ethereum.org/EIPS/eip-712)：签名授权字段、domain 与重放边界。
- [ERC-1271](https://eips.ethereum.org/EIPS/eip-1271)、[ERC-1967](https://eips.ethereum.org/EIPS/eip-1967)：合约签名与标准代理槽，不代表所有实现均采用。
- [EIP-1898](https://eips.ethereum.org/EIPS/eip-1898)：按 blockHash 查询及 requireCanonical 语义。
- [Multicall3 项目](https://github.com/mds1/multicall3)、[aggregate3 原实现](https://github.com/mds1/multicall3/blob/main/src/Multicall3.sol)：CALL、allowFailure、部署及安全说明；生产应固定审核 commit。
- [OpenZeppelin ERC20 工具](https://docs.openzeppelin.com/contracts/5.x/api/token/erc20)：SafeERC20、Permit 抢跑与集成注意事项。
- [Permit2 概览](https://docs.uniswap.org/contracts/permit2/overview)：与 ERC-2612 不同的授权和签名转移模型。
- [Geth Go 合约绑定](https://geth.ethereum.org/docs/developers/dapp-developer/native-bindings)、[Web3j 合约封装](https://docs.web3j.io/4.14.0/smart_contracts/interacting_with_smart_contract/)：生成工具与调用方式，不提供合约信任背书。
- [Ethereum JSON-RPC](https://ethereum.org/en/developers/apis/json-rpc/)、[Flashbots Protect](https://docs.flashbots.net/flashbots-protect/overview)：调用、回执及私有交易服务边界。
