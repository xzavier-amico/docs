# 35. 数字货币托管与充提币系统（上）：HD 钱包、MPC/HSM 签名隔离与归集工程

> **状态**：生产级工程篇｜**所属**：第四部分·支付、券商、风控、撮合与链上交易核心架构｜**编号**：35 / 48
>
> **本章定位**：承接 [12 账户模型](12-crypto-identity-utxo-and-account-model.md)、[14 智能合约与账户抽象](14-smart-contracts-token-standards-and-aa.md)、[19 托管与储备证明](19-cefi-vs-defi-custody-and-proof-of-reserves.md)、[22 复式账本](22-double-entry-ledger-schema-and-concurrency.md)、[24 全链路幂等](24-end-to-end-financial-idempotency.md) 和 [27 多层对账](27-multi-tier-reconciliation-and-auto-healing.md)，把地址分配、资金水位、审批、签名与归集变成可以审计、恢复和停机的资金控制系统。
>
> **覆盖范围**：HD 硬化隔离、xpub 与子私钥组合泄露、MPC/HSM 信任边界、冷热温水位、提币审批签名、Gas 垫付、CREATE2 限制、PostgreSQL DDL 与协议、Go/Java 核心示例、UNKNOWN、对账、混沌压测、极端风险、术语、面试卡、自检答案和 35.8 衔接。
>
> **红线**：不记录真实助记词、私钥、份额或备份材料；MPC 不等于没有私钥风险；HSM 不替代业务授权；CREATE2 不自动免 Gas；签名或广播超时不等于失败；归集不是客户收入；已生成签名的付款义务不能靠删除任务解除。

---

## 35.1 教学事故：归集成功率上升，资产却在流失

### 35.1.1 组合事故时间线

**以下为教学构造案例，不对应任何已确认历史事故；金额、时间及系统均为虚构。**

| 时间 | 事件 | 错误实现 | 资金后果 |
|---|---|---|---|
| 09:00 | 地址服务上线 | 在线保存跨业务父级 xpub | 泄露整个业务地址图谱 |
| 09:10 | 运维调试签名 | 把一个普通派生子私钥写入日志 | 与父 xpub 组合，可能恢复该普通分支父私钥 |
| 09:20 | 批量归集代币 | 给每个地址固定补相同 Gas | 尘埃攻击耗尽补气预算 |
| 09:21 | 广播超时 | 创建新付款、新 Nonce 再试 | 原付款仍可能成交，形成双付 |
| 09:22 | 交易审批完成 | 签名器只检查订单号 | 被替换的收款地址也获得合法签名 |
| 09:30 | 热钱包余额不足 | 温钱包自动无限补热 | 热区失陷扩大成温区损失 |
| 10:00 | 财务对账 | 把地址间转账当新充值 | 客户负债重复增加 |

问题不是“密码算法不够强”，而是地址、控制权、授权、交易与会计事实没有共同约束。

### 35.1.2 必须能回答的资金不变量

1. 每个充值地址唯一绑定网络、钱包域、派生路径与客户分配历史，旧地址不能因注销而重新分配。
2. 每个签名结果绑定不可变经济意图；任何改收款人、资产或金额的操作必须重新授权。
3. 一笔提币可以有多次费用替换尝试，但不能因此获得第二份独立付款权限。
4. 归集前后平台资产总额仅按实际网络费、代币机制及可解释调整变化，不增加客户负债。
5. 补热、补 Gas、归集均有独立额度和审批，不允许沿一条全自动链路耗尽全部储备。
6. 不能证明失败的外部动作保留冻结、证据和责任人，不能凭超时回滚成“未发生”。

---

## 35.2 HD 与签名设施：先画清失陷半径

### 35.2.1 BIP32 的能力与泄露边界

HD 钱包从种子生成主扩展密钥，再按路径派生。扩展公钥 `xpub` 包含公钥、链码及序列化元数据，不是一个普通地址公钥；扩展私钥 `xprv` 还包含私钥材料。二者均不应进入普通日志。

对 secp256k1 的 BIP32 普通子派生，简化关系为：

```text
I = HMAC-SHA512(c_parent, serP(K_parent) || ser32(index))
k_child = (parse256(I_left) + k_parent) mod n
K_child = point(parse256(I_left)) + K_parent

已知父 xpub、对应普通子私钥以及 index：
k_parent = (k_child - parse256(I_left)) mod n
```

公式忽略无效派生结果的重试分支，不能直接当作实现；实际库必须处理越界、零值和路径编码规则。
父 xpub 可计算普通子公钥及其链码，因此“xpub + 对应普通子私钥”可以恢复父私钥；从祖先 xpub 沿已知普通路径，也可能逐层回推。
这不是“xpub 单独就能盗币”，也不意味着任何不同钱包的子私钥都能配对使用。

硬化派生把父私钥信息用于派生输入，索引最高位置位，记作 `'` 或 `h`；父 xpub 无法跨越该边界继续推导。
**硬化隔离的是跨边界派生能力，不会保护已经泄露的该分支 xprv，也不能消除分支内部普通子密钥泄露风险。**

### 35.2.2 分域路径与地址服务

```text
根种子（离线仪式、不可由在线服务访问）
 ├─ 业务域 A' / 网络域' / 账户域' → 受控导出该域 xpub → /0/index
 └─ 业务域 B' / 网络域' / 账户域' → 另一套权限、额度与备份
```

这是隔离示意，不是自创路径标准的推荐。采用 BIP44 时通常为 `m/purpose'/coin_type'/account'/change/index`，仍需验证币种、曲线、脚本类型与实际库的派生约定。
某些曲线方案不支持 BIP32 式普通公钥派生；不能将 secp256k1 的公式套给所有链。

- 在线地址服务只持有最小账户域 xpub；客户、业务线、生产/测试环境采用明确隔离策略。
- 路径索引由持久化序列/CAS 分配，唯一约束兜底；分配可有空洞，不可重用已暴露地址。
- 地址先落库再返回，幂等重试返回原分配；不能靠扫描“最近未使用地址”猜分配结果。
- 地址绑定 `network_id`，同一 EVM 字节地址跨链相同不代表可跨网络记账或签名。
- 备份必须保存路径、脚本描述、网络配置与分配水位；只恢复种子、默认 gap limit 扫描可能漏掉稀疏地址。
- xpub 读权限单独审计：即使不能单独签名，泄露也会暴露资产、用户关联与未来地址。

### 35.2.3 MPC、HSM 与链上多签不是同一件事

| 方案 | 密钥/控制方式 | 主要信任边界 | 不能保证什么 |
|---|---|---|---|
| 单钥 HSM | 私钥在设备内生成或受控导入，通常不明文导出 | 硬件、固件、管理员、调用身份、备份机制 | 有权限的恶意调用仍可能被签名 |
| 阈值 MPC/TSS | 多方份额参与协议，合成有效签名 | 阈值、协议实现、参与者独立性、会话与恢复 | 达阈值串谋、实现漏洞、终端劫持仍可失窃 |
| 链上多签 | 合约/脚本验证多个授权 | 链上代码、签名人、升级与恢复规则 | 合约漏洞、签名人共同受控、费用与可用性风险 |
| MPC + HSM | 份额或参与运算再受硬件保护 | 上述边界的组合及接口设计 | 不会自动获得两套边界中所有安全属性 |

MPC 可以在正常协议中避免重构完整私钥，但仍存在“私钥对应的签名权”；不能宣传为无私钥风险。
阈值 `t-of-n` 的安全假设必须写明：攻击模型、主动/被动安全、可识别中止、曲线与签名算法、预处理材料和备份恢复模型。
ECDSA 随机数、预签名材料或协议会话状态复用可能泄露秘密；签名重试必须由受审计实现处理，不能复制半完成会话跨实例重跑。
三份份额若共享云管理员、镜像仓库、CI 凭证或恢复口令，并不构成三个独立失陷域。
份额轮换不等于地址换钥；旧份额达到旧阈值、快照回滚及备份残留的风险必须由具体协议分析，不能假定旧备份已失效。

### 35.2.4 密钥生命周期也是协议

密钥仪式应包括双人见证、设备来源验证、固件基线、熵来源与生成证明、份额分发、离线备份、灾难恢复和销毁验收。
演练在隔离测试资产/网络上完成，只记录材料的清单、校验承诺及保管责任，不记录任何真实秘密。
HSM 备份解封、MPC 恢复、固件升级、地址白名单变更和签名策略升级，都必须有不弱于日常大额提币的审批边界。

---

## 35.3 冷热温水位与提币审批签名

### 35.3.1 分层不是三个余额标签

```text
客户请求 → 身份/KYT/限额 → 账本冻结 → 审批证据 → 交易构建
                                                    ↓
独立策略服务 ← 不可变意图与策略快照 → 隔离签名器 → 广播网关
                                                    ↓
冷库 ──人工仪式/延时──► 温库 ──有限额补热──► 热库 → 链上收款人
                           归集地址 ──受控任务──► 指定库位
```

热库承担短期出款；温库缩短补给时间但仍有独立审批/限额；冷库强调离线、低频与人工恢复能力。
层级应由签名权限、网络连通性、人员、额度和控制域共同定义，而不是“余额超过多少就叫冷钱包”。

令 `D(T)` 为补给延迟 T 内的压力出款需求，`F` 为费用储备，`R` 为已签未最终交易的保守剩余义务：

```text
热库可调度余额 = 经认可最终性资产 - 已预占本金 - 已预占费用 - 其他锁定
补热触发线 L ≈ 压力分位数 D(T) + F + 运维缓冲
补热目标 U > L；单次补给及滚动窗口补给均受独立上限约束
```

这些是规划关系，不是保证兑付的数学定理；保留义务如何计入余额应统一，不能将 R 重复扣两次。
链拥堵、冷库响应变慢、客户集中提款与稳定币冻结应进入联合压力场景，不仅看历史平均流量。
热库失陷时必须能停补热；“低水位自动无限补款”会把风险限额变成装饰。

### 35.3.2 审批必须绑定完整意图

经济意图至少包含 `withdrawal_id/network/asset/sender/recipient/amount/customer_fee_rule`；交易尝试还要绑定 Nonce、Gas 上限、费率上限、交易类型、data 与有效性约束。
EVM 代币转账的 `tx.to` 通常是代币合约，不是最终收款人；签名器必须解码 calldata 并校验接收者、金额和白名单合约。
审批界面不能只显示订单号、代币 symbol 或缩略地址；显示完整网络、资产身份、收款地址与金额单位。

1. 请求去重并校验地址格式、网络及资产白名单，完成 KYT、额度和异常行为检查。
2. 本金/最大费用预算通过账本预占，预占回执与意图版本绑定，事务 Outbox 发送审批请求。
3. 审批人独立认证，职责分离；拒绝发起人自批，必要时启用白名单冷静期和大额延时。
4. 构建器分配 Nonce/UTXO，产生规范化载荷；独立校验器从真实交易字节重算承诺并解码经济含义。
5. 签名器复核审批、预占、策略版本、epoch/fencing、截止时间与闸门；只签明确允许的交易类型。
6. 签名结果和交易摘要先持久化，再交广播器；广播者不能改签名字节，且与审批人分权。

规范化承诺应有域分离、schema 版本、长度编码、完整单位和网络身份；禁止直接拼接字符串或哈希不稳定 JSON。
交易摘要不等于经济意图摘要；费用替换可沿同一意图产生新尝试，但必须受预授权费用区间与累计预算约束。
任何换地址、换合约、增加金额、放开无限 allowance 的操作都不能伪装成“提价重发”。
对普通 EVM EOA 交易，本地审批截止时间不使已签交易在链上失效；只有链上验证规则才能提供相应有效期约束。

---

## 35.4 归集、Gas 垫付与 CREATE2 的现实限制

### 35.4.1 归集是资产位置迁移

独立充值地址方便归属识别，但增加小额余额、Gas、Nonce 和运维成本。归集计划绑定 `network/source/asset/destination/policy_version`，目标必须来自受控库位注册表。
充值入账依据链上事件及最终性，归集依据可支配资产与成本；二者不能互相替代，也不能等归集后再次给客户入账。
对有冻结、黑名单、转账税、rebasing、暂停或回调的代币，应单独建模实际收发数量与差额原因，不默认 `requested = received`。

```text
可归集量 = 经认可的源地址余额 - 已占用出款 - 留存/协议锁定
触发条件 = 可归集量达到阈值 且 风险允许 且 费用预算可接受
经济性评估 = 资产可实现价值 与 最坏执行成本/失败成本/集中风险比较
```

价值估计只决定是否值得执行；不能用浮点估值替代账本中的最小单位整数。
归集会集中风险，目标库位不可因“自己地址”免审批。归集本金、Gas 垫付与热库补给必须使用不同意图类型及权限。

### 35.4.2 EVM 代币归集与补气顺序

| 步骤 | 动作 | 必须防止 |
|---|---|---|
| 规划 | 核验真实代币余额、白名单与地址控制权 | 假代币诱导垫付、尘埃刷任务 |
| 预算 | 按固定链视图估算 gas，加政策余量与费用上限 | 把 estimateGas 当执行保证 |
| 垫付 | Gas 资助账户向源地址转原生币 | 并发重复补气、绕过日累计额度 |
| 依赖确认 | 记录资助交易与源地址余额证据 | 只见广播成功就无条件开始归集 |
| 签名执行 | 源地址签代币转账，跟踪所有尝试 | 外部签名消耗余额、Nonce 冲突 |
| 清理 | 达到经济阈值再处理残留原生币 | 为扫走极小尘埃付出更大费用 |

典型代币归集最多预算 `gas_limit × max_fee_per_gas`；普通 EVM 原生币转账还必须覆盖转出 value。
EIP-1559 执行单价一般为 `min(maxFeePerGas, baseFee + priorityFee)`，实际费用来自回执 `gasUsed × effectiveGasPrice`；L2 还可能有 L1 数据费等额外收费。
资助账本记录预算、实际消耗、余额残留和可回收性；不能把垫付额立即全部记作已消耗网络费。
一个资助任务只能有一个活动付款意图，行锁/CAS 与唯一键共同防重；UNKNOWN 时不再创建第二个独立资助付款。
普通 EOA 地址收到垫付款后，仍可能被已失陷的密钥转走；补气不是安全控制，失败次数和累计补贴必须熔断。

### 35.4.3 CREATE2 能预测地址，不能取消执行成本

```text
address = last20(keccak256(0xff || deployer || salt || keccak256(init_code)))
```

CREATE2 地址取决于部署者、salt 和 init code 哈希；相同公式不保证不同链上的工厂代码、状态、权限与资产语义一致。
可以先向未部署地址充值，再由工厂部署受控归集合约；部署、调用、代币转移仍需 Gas，通常只是由工厂/执行者统一支付。
“用户地址不用预放原生币”不等于全链路免 Gas，也不等于任意 EOA 都能被 CREATE2 接管。

- 固定并审计工厂代码、构造参数、受益人、授权和部署 salt 的分配；不能允许调用者把归集目标替换成自己。
- 未部署地址的资产可用性依赖未来成功部署；init code 不兼容、工厂失陷、费用飙升都可能造成资产长期滞留。
- 在适用 EIP-684 的 EVM 规则下，目标地址已有非零 Nonce 或代码会导致创建冲突；已到账余额本身并不必然阻止部署。
- 采用 EIP-6780 的网络不能把 SELFDESTRUCT 当作任意删码重部署工具；同交易创建并销毁等例外也不能泛化为稳定升级方案。
- ERC-4337 paymaster/relayer 可改变谁付费，但有适用账户、验证、存款与拒绝服务边界，并非 CREATE2 自带能力。
- UTXO 归集涉及输入选择、找零、脚本、隐私、费率和 dust；不能复用 EVM 的“补 Gas 到充值地址”流程。

---

## 35.5 持久化、协议与可执行代码边界

### 35.5.1 PostgreSQL 事实表骨架

以下仅演示 EVM 地址与整数金额，不存密钥材料；多链生产版需另建链特定地址、UTXO、权限、审批、预算、账本回执与审计表。

```sql
CREATE TABLE custody_address (
  id uuid PRIMARY KEY, network_id text NOT NULL, wallet_domain text NOT NULL,
  address bytea NOT NULL CHECK (octet_length(address) = 20),
  derivation_path text NOT NULL, allocation_key text NOT NULL,
  key_epoch bigint NOT NULL CHECK (key_epoch >= 0),
  UNIQUE(network_id, address), UNIQUE(wallet_domain, derivation_path),
  UNIQUE(network_id, allocation_key)
);
CREATE TABLE custody_intent (
  id uuid PRIMARY KEY, kind text NOT NULL
    CHECK (kind IN ('WITHDRAWAL','SWEEP','GAS_FUND','REBALANCE')),
  network_id text NOT NULL, idempotency_key text NOT NULL,
  economic_digest bytea NOT NULL CHECK (octet_length(economic_digest) = 32),
  amount numeric(78,0) NOT NULL CHECK (amount > 0 AND amount < power(2::numeric,256)),
  fee_budget numeric(78,0) NOT NULL CHECK (fee_budget >= 0),
  reservation_id uuid NOT NULL, policy_version bigint NOT NULL,
  state text NOT NULL CHECK (state IN ('HELD','APPROVED','SIGNING','SIGNED',
    'UNKNOWN','INCLUDED','FINALIZED','REJECTED','CANCEL_PENDING','SETTLED')),
  version bigint NOT NULL DEFAULT 0, UNIQUE(network_id, idempotency_key)
);
CREATE TABLE custody_attempt (
  id uuid PRIMARY KEY, intent_id uuid NOT NULL REFERENCES custody_intent(id),
  attempt_no integer NOT NULL CHECK (attempt_no > 0),
  tx_digest bytea NOT NULL CHECK (octet_length(tx_digest) = 32),
  sign_session_id uuid NOT NULL UNIQUE, signed_blob_ref text,
  state text NOT NULL CHECK (state IN ('PREPARED','SIGNING','SIGN_UNKNOWN',
    'SIGNED','BROADCAST_UNKNOWN','INCLUDED','ORPHANED','FINALIZED','REVERTED')),
  UNIQUE(intent_id, attempt_no)
);
CREATE TABLE custody_outbox (
  event_id uuid PRIMARY KEY, aggregate_id uuid NOT NULL,
  aggregate_version bigint NOT NULL, event_type text NOT NULL,
  payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(aggregate_id, aggregate_version, event_type)
);
```

`numeric(78,0)` 允许 uint256 之外的某些 78 位数字，因此金额显式限制 `< 2^256`；其他字段按链规则补上上界。
这些约束不能独立保证状态合法迁移。以 `WHERE id=? AND version=? AND state=?` 做 CAS，在同一事务追加 Outbox；消费者保存 inbox/effect key 防重。
Nonce 槽位表必须在 `(network,sender,nonce)` 上唯一，再关联一个意图和多次尝试；UTXO 锁定表必须对未释放 outpoint 唯一，细节见下一章。
签名对象引用指向加密、受控、不可变存储；已签 raw transaction 虽不是私钥，泄露仍可能提前执行付款，应作为敏感可执行材料管理。

### 35.5.2 跨边界协议

| 消息 | 最少绑定字段 | 接收方必须独立验证 |
|---|---|---|
| `ApprovalGranted` | intent/digest/policy/approvers/epoch/expiry | 审批签章、职责分离、有效期、撤销状态 |
| `SignRequested` | intent/attempt/session/payload hash/reservation | 实际载荷重算摘要、经济语义、额度、闸门 |
| `SignCompleted` | session/tx digest/artifact ref/key epoch | 签名验证、恢复出的发送者/脚本匹配 |
| `BroadcastObserved` | attempt/tx hash/source/result | 本地算 hash，不把 RPC 文本当最终结算 |
| `FinalityObserved` | network/tx/block hash/checkpoint/epoch | 主链归属、执行结果、最终性证据 |

重试使用原消息身份与原语义；同一幂等键遇到不同摘要必须拒绝。审批凭证是给签名器的授权，不应成为任意客户端可反复使用的 bearer token。
签名服务按会话支持查询并持久化结果；查询不到时先判断会话是否可能已执行，不能简单切换新会话再签。

### 35.5.3 Go：审批闸门的最小可运行示例

**边界**：下例只验证已经由可信解码器重算的摘要与金额预算，不实现派生、哈希规范化、交易解码、身份认证或真实签名；不能直接接入生产签名 API。

```go
package main

import (
    "errors"
    "math/big"
)

type Request struct {
    Digest [32]byte // 由隔离校验器从真实载荷重算，非客户端自报
    Epoch uint64
    Amount, Fee *big.Int
}
type Approval struct {
    Digest [32]byte
    Epoch uint64
    ExpiresAt int64
    AmountCap, FeeCap *big.Int
}

func gate(r Request, a Approval, currentEpoch uint64, now int64) error {
    if r.Epoch != currentEpoch || a.Epoch != currentEpoch || now >= a.ExpiresAt {
        return errors.New("stale authorization")
    }
    if r.Digest != a.Digest {
        return errors.New("payload changed")
    }
    if r.Amount == nil || r.Fee == nil || a.AmountCap == nil || a.FeeCap == nil {
        return errors.New("missing budget")
    }
    if r.Amount.Sign() <= 0 || r.Fee.Sign() < 0 ||
        r.Amount.Cmp(a.AmountCap) > 0 || r.Fee.Cmp(a.FeeCap) > 0 {
        return errors.New("budget exceeded")
    }
    return nil
}

func main() {
    d := [32]byte{1} // 合成测试承诺，不是密钥
    r := Request{d, 7, big.NewInt(100), big.NewInt(3)}
    a := Approval{d, 7, 200, big.NewInt(100), big.NewInt(5)}
    if gate(r, a, 7, 100) != nil { panic("valid request rejected") }
    r.Digest[0] = 2
    if gate(r, a, 7, 100) == nil { panic("mutation accepted") }
    r.Digest = d
    if gate(r, a, 8, 100) == nil { panic("stale epoch accepted") }
    if gate(r, a, 7, 200) == nil { panic("expiry accepted") }
    r.Fee = big.NewInt(6)
    if gate(r, a, 7, 100) == nil { panic("fee overrun accepted") }
}
```

生产实现还需消费一次性授权/维护费用替换授权范围、锁定预占、检查滚动额度和当前停机闸门；金额对象不得被其他 goroutine 并发修改。
本地通过 gate 不能阻止旧主稍后签名，签名设施入口还必须执行持久化 fencing/epoch 校验。

### 35.5.4 Java 21：代币归集补气预算

**边界**：示例仅计算单笔普通 EVM 代币归集的最大执行 Gas 垫付缺口；不包含 L2 附加费、并发预占、原生币 value、估算误差或真实链操作。

```java
import java.math.BigInteger;

public final class SweepBudget {
    private static final BigInteger MAX = BigInteger.ONE.shiftLeft(256).subtract(BigInteger.ONE);
    private static void uint256(BigInteger x) {
        if (x == null || x.signum() < 0 || x.compareTo(MAX) > 0)
            throw new IllegalArgumentException("uint256");
    }
    static BigInteger topUp(BigInteger gasLimit, BigInteger maxFee,
                            BigInteger availableNative, BigInteger subsidyCap) {
        uint256(gasLimit); uint256(maxFee);
        uint256(availableNative); uint256(subsidyCap);
        if (gasLimit.signum() == 0 || maxFee.signum() == 0)
            throw new IllegalArgumentException("zero gas budget");
        BigInteger cost = gasLimit.multiply(maxFee);
        uint256(cost);
        BigInteger missing = cost.subtract(availableNative).max(BigInteger.ZERO);
        if (missing.compareTo(subsidyCap) > 0)
            throw new IllegalStateException("subsidy cap");
        return missing;
    }
    public static void main(String[] args) {
        BigInteger gas = BigInteger.valueOf(60_000);
        BigInteger fee = BigInteger.valueOf(30_000_000_000L);
        BigInteger cost = gas.multiply(fee);
        if (!topUp(gas, fee, BigInteger.ZERO, cost).equals(cost))
            throw new AssertionError("wrong budget");
        if (topUp(gas, fee, cost, BigInteger.ZERO).signum() != 0)
            throw new AssertionError("duplicate topup");
        try {
            topUp(gas, fee, BigInteger.ZERO, cost.subtract(BigInteger.ONE));
        } catch (IllegalStateException expected) { return; }
        throw new AssertionError("cap bypass");
    }
}
```

`availableNative` 必须是扣除活动预占后的余额；即使计算缺口为零，也要在事务里占用费用预算，防止多个任务复用同一余额。
BigInteger 避免 long 溢出，不代表输入可信；资产精度来自版本化注册表，禁止 Double、科学计数法歧义和错误单位混入签名载荷。

---

## 35.6 状态机、UNKNOWN、对账与混沌压测

### 35.6.1 意图与尝试分层

```text
意图：HELD → APPROVED → SIGNING → SIGNED → INCLUDED → FINALIZED → SETTLED
                         │          │          │
                         └─ UNKNOWN ┘          └─重组→ UNKNOWN/重新观察
未签前明确拒绝 → REJECTED；已签后申请取消 → CANCEL_PENDING

尝试：PREPARED → SIGNING → SIGNED → 广播/查链 → INCLUDED → FINALIZED
                  │                  │             └─ ORPHANED
                  └─ SIGN_UNKNOWN    └─ BROADCAST_UNKNOWN
执行回滚：保留 REVERTED 证据，仍结算实际 Gas，不按成功交付本金记账
```

`UNKNOWN` 是证据不足，不是“等待一会儿就失败”。持久化最后已知动作、session、原始交易引用、查询来源、重试计划、最大暴露与升级时限。
签名未知先查签名会话及产物；广播未知先按本地 tx hash、多来源链视图和相关 Nonce/输入查询，不能生成新经济意图。
同 Nonce 费用替换记录为新尝试，旧尝试仍可能胜出；链上取消交易也必须等待其足够最终性，不能本地标取消即解冻。
普通 EVM 交易无天然 TTL：即使多家 RPC 都查不到，若发送者 Nonce 未被最终占用，旧签名仍可能有效。

### 35.6.2 三层对账与会计口径

| 层次 | 对账对象 | 不平处理 |
|---|---|---|
| 控制权层 | 地址注册、派生域、签名公钥、工厂/合约权限、备份清单 | 发现未知地址或权限漂移立即停签 |
| 链上事实层 | 各库位余额、全部交易尝试、实际费、归集前后差额 | 固定块哈希与最终性，隔离 RPC/重组差异 |
| 账本层 | 托管资产、客户负债、预占、费用与在途 | 追加可解释分录，不直接改余额抹平 |

概念分录：归集仅在内部资产库位间迁移；垫付先形成内部位置变化/待核费用，实际执行后按回执确认网络费；客户手续费收入依合同独立确认，不能与链费净额混记。
账户桶、科目方向和最终性口径由账套统一定义。资产负债相等不代表钱包安全，也不能用 PoR 代替完整负债和控制权核验。
自动修复仅限幂等重放已证明的事实；未知链上出款、余额缺口、密钥域异常必须人工升级并保留证据。

### 35.6.3 测试不是只看签名 TPS

| 注入点 | 故障/压力 | 必须验证的结果 |
|---|---|---|
| 地址分配 | 多实例并发、提交后断连接、索引跳跃 | 唯一地址、幂等返回、恢复扫描不漏 |
| 审批入口 | 重放、改 calldata、跨网络、过期/旧 epoch | 拒绝签名，留下可审计拒绝原因 |
| 签名设施 | 完成后丢响应、阈值节点宕机、旧快照恢复 | 不盲目重签；会话收敛且无预处理材料复用 |
| 广播/索引 | 超时、RPC 分叉、重组、旧交易迟到 | 不双付、不提前释放冻结、胜出尝试唯一 |
| 归集/补气 | 尘埃洪泛、转账税、Gas 涨百倍、重复调度 | 预算熔断、实际数量可解释、无无限补贴 |
| 库位补给 | 热库被盗、冷库暂不可用、集中提款 | 停补热、降额/排队，风险不蔓延 |
| 存储 | 主备切换、Outbox 重投、事务中进程死亡 | 版本单调、消息防重、账本效果恰好一次 |

报告包含审批至最终结算 p50/p99、签名并发与排队、UNKNOWN 数量/金额/年龄、预算占用、归集成本、对账差额和恢复时间。
在隔离网络/仿真器执行故障注入，不导入真实密钥；性能通过条件必须同时包括安全不变量零违反及预算上限不突破。

---

## 35.7 极端风险、术语与面试自检

### 35.7.1 低频但不可省略的边界

- **阈值人员同时失联**：安全与活性有取舍；恢复方案必须提前验证，不能临时降低阈值或传输份额凑签。
- **设备/供应链共因失陷**：同固件、云租户或身份根同时失效，需异构审查、独立权限域与紧急迁移预案。
- **链暂停、最终性冲突、费用极端上涨**：停止自动释放与补贴；排队、披露和限额不能假装成技术重试。
- **代币发行人冻结、黑名单与脱锚**：密钥完好也可能无法转移/兑付；分散地址不能消除发行人控制风险。
- **恢复后状态倒退**：旧审批、旧会话和已花 UTXO 可能重新出现；恢复先对账、升 fencing epoch，再开签名。
- **盲签复杂合约**：模拟依赖当时状态且可能失真；禁止把模拟成功作为任意 calldata 的唯一授权依据。
- **紧急迁移**：提高审批强度、预留费用、分批验证目标控制权；换设备/轮份额不等于消除已外泄私钥的旧地址风险。

### 35.7.2 核心术语速查

| 术语 | 工程含义 |
|---|---|
| xpub / chain code | 可继续普通公钥派生的扩展公钥材料 / 派生链码，不是签名随机数 |
| Hardened derivation | 父 xpub 无法跨越的硬化派生边界，不代表分支永不泄露 |
| MPC / TSS | 多方计算 / 阈值签名；安全取决于阈值与协议假设 |
| HSM | 执行密钥保护与密码运算的硬件边界，不理解全部资金业务 |
| Sweep / Rebalance | 分散地址归集 / 库位之间调拨，均非新客户收入 |
| Gas sponsorship | 由资助方承担或垫付费用，不代表费用消失 |
| Intent / Attempt | 不可变经济目的 / 一次具体交易与签名尝试 |
| Fencing / UNKNOWN | 拒绝旧主继续操作的代际隔离 / 外部结果不能证明的状态 |

### 35.7.3 面试答题卡

| 面试追问 | 高质量回答的骨架 |
|---|---|
| xpub 可以放心发给所有服务吗？ | 不能单独签名，但泄露地址图谱；结合对应普通子私钥可恢复父私钥，最小域导出并硬化隔离 |
| 有 MPC 是否可取消审批？ | 不可；阈值只约束签名控制，恶意合法请求、共因失陷和恢复路径仍需独立授权 |
| 为什么代币归集先补原生币？ | 普通 EOA 执行需原生币付费；也可用受控合约/代付，但费用和权限不会消失 |
| 签名超时能释放冻结吗？ | 不能；先查会话和产物，已签交易可能迟到执行，保留预算和 UNKNOWN |
| 冷库为何不能实时自动补热？ | 自动权限会贯通风险边界；需要独立限额、延时、审批与热区失陷熔断 |
| 对账发现少币就重跑归集？ | 先固定链视图区分费、税、重组、遗漏与盗转；未证明原因不得自动创造付款 |

### 35.7.4 自检题与参考答案

1. **泄露账户 xpub 与其普通孙私钥，风险是否仅限孙地址？** 不一定。若中间路径均可由该 xpub 普通派生并获知索引，可逐级回推到该账户私钥；不能跨硬化边界凭空推导更高层。
2. **MPC 三方部署在三个可用区就是三方独立吗？** 不是。共同 IAM、CI、恢复口令、管理人员或同源漏洞可能一次控制足够份额。
3. **CREATE2 地址已收币但未部署，是否已证明可以归集？** 没有。还需验证工厂、init code、权限、目标碰撞、链规则及部署执行预算。
4. **已签提币过了审批截止时间可以退还本金吗？** 不能仅据此退款；普通 EVM 签名不会因本地审批过期失效，须证明付款路径已最终排除或结算。
5. **归集给客户多记一笔充值能否用反向转账修复？** 不能。客户负债错误通过有依据的账本冲正修复；额外链上付款只会增加另一份风险。
6. **补气预算为零是否代表任务无需锁定资源？** 否。现有余额也可能被其他任务占用，仍须预占、状态 CAS 与资助幂等约束。

---

## 35.8 衔接：签名正确之后，还要证明链上事实正确

本章解决“谁能生成地址、谁有权签什么、钱在哪个库位、如何付得起归集成本”，但签名成功不能证明已广播，广播成功不能证明入块，入块也不能证明最终交付。
下一章 [36. 高可靠扫块器、防区块重组与 Nonce 调度](36-chain-indexer-reorg-defense-and-nonce.md) 将把这些意图落到区块身份、共同祖先、最终性与唯一 Nonce 槽位：旧分叉上的充值如何冲正，UNKNOWN 如何查询收敛，同 Nonce 费用替换如何仍然只支付一次。
带着三个问题继续：**哪个块承载这笔事实？哪个尝试最终胜出？哪个账本效果可以在重启和重组后仍被证明只执行一次？**
