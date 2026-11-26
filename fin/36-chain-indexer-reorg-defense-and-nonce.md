# 36. 数字货币托管与充提币系统（下）：高可靠扫块器、防区块重组（Reorg）与 Nonce 调度

> **状态**：生产级工程篇｜**所属**：第四部分·支付、券商、风控、撮合与链上交易核心架构｜**编号**：36 / 48
>
> **本章定位**：承接第 35 章的 HD 地址、签名隔离与归集体系，以及 [12 账户模型](12-crypto-identity-utxo-and-account-model.md)、[13 共识与最终性](13-consensus-finality-rollups-and-bridges.md)、[14 合约与代币标准](14-smart-contracts-token-standards-and-aa.md)、[22 复式账本](22-double-entry-ledger-schema-and-concurrency.md)、[24 全链路幂等](24-end-to-end-financial-idempotency.md)、[25 状态机与 Outbox](25-state-machines-tcc-saga-and-outbox.md) 和 [27 多层对账](27-multi-tier-reconciliation-and-auto-healing.md)，解决“链会改历史，网络会丢回执，多实例会抢 Nonce，但客户的钱不能因此多一份或少一份”。
>
> **覆盖范围**：扫块事故；滑动区块头窗口、`parent_hash` 连续性与共同祖先定位；最终性与入账分层；假代币、假日志和回滚攻击防御；EVM EOA Nonce 池、并发提币与 EIP-1559 同 Nonce 提价重发；PostgreSQL DDL；Go 1.24 / Java 21 核心代码；状态机、对账、压测、故障演练、极端边界、术语、面试答题卡、自检题和 36.8 衔接。
>
> **不可妥协的红线**：高度不是区块身份；日志不是到账授权；RPC 多数票不是共识证明；`safe` 不等于 `finalized`；广播超时不是发送失败；Nonce 分配不是靠 `pending` 查询抢号；替换交易不能改变提币经济意图；重组冲正只能追加分录，禁止删账或直接改余额；签过名的交易没有天然过期时间，不能因本地取消就释放全部资金义务。

---

## 36.1 事故引子：余额已经可提，充值却从链上消失了

### 36.1.1 一次组合事故

某平台接入一条 EVM 兼容链，扫块器每秒执行 `eth_getLogs`，匹配 `Transfer` 的 topic 后直接入账。热钱包由三个实例并发提币，每个实例用 `eth_getTransactionCount(address, "pending")` 获取 Nonce。

| 时间 | 事件 | 错误实现 | 后果 |
|---|---|---|---|
| 10:00:00 | 攻击者部署名为 USDT 的代币 | 按 `symbol` 和事件签名识别资产 | 无价值资产被当成真实 USDT |
| 10:00:02 | RPC 返回一段真实合约的 Transfer 日志 | 没验证 receipt 所属块与本地主链 | 把旧分叉上的充值视为有效 |
| 10:00:03 | 充值达到“2 个确认” | 高度差满足即释放可用余额 | 攻击者立即换币、提走其他资产 |
| 10:00:05 | 节点切到另一分叉 | 只保存 `last_height`，不保存 hash | 扫块继续前进，没有撤销孤块充值 |
| 10:00:06 | 三个实例同时发币 | 都拿到 Nonce 817 | 不同提币意图争抢同一个槽位 |
| 10:00:07 | RPC 广播超时 | 换 Nonce、换 txHash 再发一次 | 同一客户可能收到两笔付款 |
| 10:02:00 | base fee 上升 | 只提高 `maxFeePerGas`，tip 不变 | 节点拒绝 replacement，整个队列卡住 |
| 次日 | 财务发现资产少于客户负债 | 人工删除充值记录 | 原始事实与冲正路径丢失，不能追责 |

这里不是“确认数调大一点”就能修好：资产身份、链分支、最终性、会计入账和 Nonce 所有权都缺少明确边界。

### 36.1.2 唯一真问题与资金不变量

> 在分叉切换、节点撒谎或落后、日志重复与漏返、进程重启、数据库主备切换、签名延迟、广播未知、Nonce 空洞和费用暴涨同时发生时，能否证明每笔真实充值只形成一份有效客户负债，每笔提币意图最多支付一次，任何不能证明的状态都停在受控的冻结或未知态？

必须持续成立的不变量：

1. 每个 `(network, height)` 最多有一个当前 canonical 区块，所有已发布 canonical 区块父子衔接。
2. 充值释放可用余额的块，必须属于指定链视图，且达到该资产、金额与网络的最终性策略。
3. 每个充值生效周期的入账、解冻、冲正分别幂等；孤块历史仍可查。
4. 每个 `(network, sender, nonce)` 只有一个持久化槽位；该槽位正常替换的所有交易绑定同一经济意图。
5. 一个槽位可有多个签名尝试，但在一条 canonical 链上最多有一个胜出交易；任何尝试都可能成为胜者。
6. 本金冻结、最大 Gas 预算、账本实际费用和链上消耗分别记录，不能重复冻结或重复扣费。
7. 没有最终性证据，不释放可再次付款的权限；没有原始分录，不凭空做“冲正”。

---

## 36.2 架构与事实模型：采集可以并行，主链发布必须有序

### 36.2.1 五条管线

```text
受控节点 / 独立 RPC / 共识层或 L1 最终性证据
       │
       ▼
Header Collector ──► 候选头与原始证据（可并行、可乱序）
       │
       ▼
Canonicalizer（每网络逻辑单写者、epoch、fencing）
       │ parent_hash / 共同祖先 / 最终性边界
       ├──► Block / Receipt / Log 校验与解析
       │            │
       │            ▼
       │       Deposit Observation → 风控/KYT → 账本 Outbox
       │
       └──► Reorg Coordinator → 停止释放 → 冻结/冲正 → 新分支重放

提币请求 → 审批 + 本金/Gas 预占 → Nonce Slot → 隔离签名器
                                                 │
                                                 ▼
                       Attempt Journal → 广播 → Receipt Watcher
                                                 │
                                     同 Nonce RBF / 最终性 / 账本
```

**采集成功不等于发布成功，发布成功不等于可用余额到账。** 把耗时 RPC 放在数据库事务之外；把结果以区块 hash 固定，再在短事务中检查当前 head、epoch 和 fencing token 后发布。

### 36.2.2 四种水位与两种身份

| 水位 | 含义 | 不可替代的检查 |
|---|---|---|
| `observed_head` | 节点报告的候选最高块 | 节点健康、时钟、分叉视图 |
| `canonical_head` | 本地已发布、父子连续的头 | 祖先关系与唯一发布者 |
| `indexed_head` | receipt/log 完整解析且落库的头 | 范围完整、无跳块、哈希一致 |
| `released_head` / finality checkpoint | 允许资金释放的连续前缀及证据 | 风控、最终性、Outbox/账本确认 |

不能用 `MAX(height)` 代表连续完成：100、102 都在库里不代表 101 已处理。资产停充、单笔人工复核还会让资金释放落后于解析水位；两种进度不要混用。

两个身份空间也必须分开：

- **链上出现身份**：`network + block_hash + tx_hash + event_kind + receipt_event_ordinal`。保存这次执行发生在哪个分支。
- **业务身份**：`deposit_id` 或 `withdrawal_id`，以及它的账本 effect key。保存平台承担了哪份义务。

`network` 是内部不可变网络标识，应绑定 `chainId + genesis/checkpoint + 环境配置`；只凭 `chainId` 不足以区分误配置的私链或分叉。合约地址必须是 20 字节值，不能依赖大小写文本或代币名字。

---

## 36.3 滑动区块头窗口与 Reorg 检测

### 36.3.1 窗口保存什么

每个头保存 `height/hash/parent_hash/timestamp/receipts_root`、来源、验证等级、canonical 状态、最终性证据引用与原始响应摘要。最近 `W` 个头放在热表或缓存，完整证据异步归档。

```text
本地： ... → A(100) → B(101) → C(102) → D(103)
远端： ... → A(100) → X(101) → Y(102) → Z(103) → N(104)

N.parent = Z.hash ≠ 本地 D.hash
向 N.parent 按 hash 追溯，直到发现 A 是本地 canonical 祖先。
旧分支孤立高度：101..103；新分支：X,Y,Z,N。
```

`W` 不是安全确认数，也不是“只要重组不超过 W 就不会亏钱”。它是热路径可自动恢复的最大搜索范围，应至少覆盖计划容忍的重组深度、RPC 落后、停机追赶和诊断余量；未最终块不能仅因超出窗口被删除。找不到祖先，进入 `DEEP_REORG_HALT`，从可信 checkpoint/归档证据重建，**不得猜一个回退高度**。

### 36.3.2 不只检查“新块 parent 是否等于旧 head”

逐项处理以下输入：

| 输入 | 判断 | 动作 |
|---|---|---|
| 同高同 hash | 重复通知 | 幂等返回 |
| 高度 `head+1` 且 parent 相等 | 直接延伸 | 校验、解析、CAS 发布 |
| 高度跳跃 | WS 丢通知/追赶 | 按 hash 拉齐中间父链，不能直接跳水位 |
| 同高不同 hash | 分叉或节点视图不一致 | 收集证据、验证来源、定位祖先 |
| 更低旧头 | RPC 落后或乱序 | 不能仅因此回退；与可信 fork-choice 视图核验 |
| 父链高度不连续/父 hash 不符 | 数据损坏或不可信 RPC | 拒绝候选、切源并告警 |
| 候选不包含本地 finalized checkpoint | 最终性冲突或节点错误 | 全网充提熔断、人工取证 |

不要自行用“高度更大”或“几个 RPC 赞成”实现 PoS fork choice。生产上由经过验证的全节点/共识客户端给出主链视图，独立来源用来发现异常。多个 RPC 可能共享同一家上游；它们的一致不能代替共识安全。

即使没有新块事件，也要定期用可信节点核验本地 head 和最近若干 canonical hash，否则“同高度替换后链暂时不出新块”可能一直不被发现。

### 36.3.3 重组恢复的事务边界

重组通常跨很多块，不能持有一个巨型数据库事务等 RPC 和账本完成。采用**持久化恢复任务 + epoch 屏障**：

1. 锁定网络控制行，验证 fencing，写入 `REORGING`、`epoch+1` 和恢复任务；记录旧 head、候选 head、共同祖先。所有释放 worker、提币终结 worker必须检查该 epoch。
2. 立即阻止受影响充值转可用、受影响提币释放本金，以及依赖这些资金的后续出款；按风险决定全网暂停还是影响集暂停。
3. 在隔离候选视图中下载并校验新分支。祖先不得低于已承认的 finalized checkpoint；否则转事故流程，不自动“反最终性”。
4. 按旧分支**从高到低**标记 orphan，生成充值冻结/冲正与提币撤销 inclusion 的幂等指令。账本消费时复核 epoch；已存在的效果通过补偿收敛，而不是删消息。
5. 旧分支所有经济效果获得账本回执后，按新分支**从低到高**重放。分批写入可重入 checkpoint，恢复过程对外不可见为正常链。
6. 核验新连续前缀、金额与账本 effect 对拍，原子发布新 head/indexed 水位并恢复 `RUNNING`。资金释放按新策略重新评估，不能恢复旧的确认计数。

账本与索引库不在同一事务域时，网络状态是安全闸门：账本按 `network + epoch` 拒绝过期的新释放指令，并接收明确的补偿指令。仅在扫块库写一个 `paused=true`、让账本继续执行积压的旧释放消息是不安全的。冻结闸门本身也需要落在消费授权边界。

---

## 36.4 最终性、入账状态与假充值防线

### 36.4.1 确认数不是统一的资金政策

约定某块自身算第 1 次确认：`confirmations = canonical_head.height - block.height + 1`。如果业务使用“后续块数”，必须另起字段，避免 off-by-one。

| 网络/场景 | 观测层 | 释放可用余额条件示例 | 特别风险 |
|---|---|---|---|
| Ethereum PoS 大额 | included / safe / finalized | 经验证的 finalized checkpoint 覆盖该块且其为祖先 | Finality 停滞不能用墙钟时间替代 |
| EVM 兼容 PoS 链 | 按该链真实共识适配 | 链特定最终性与金额分层策略 | 相同 RPC 字段名不保证相同安全语义 |
| PoW 链 | canonical + 深度 | 资产/金额/算力风险矩阵，必要时人工复核 | 深度仅降低概率，不构成绝对最终性 |
| Optimistic Rollup | sequencer / L1 included / L1 finalized | 区分普通充值信用政策与跨域退出完成 | L2 确认不等于挑战期结束或桥款可用 |
| ZK Rollup | L2 included / proof accepted / L1 finalized | 按证明提交、验证与 L1 最终性检查 | Sequencer 软确认不是 L1 最终结算 |

策略必须版本化，记录 `policy_version/amount_tier/checkpoint_hash/released_at`。既不能承诺固定“等 6 块就安全”，也不能把理论上可用的最终性标签当作服务一定支持。无法获取可靠最终性证据时，默认停在 pending，而不是静默降级。

### 36.4.2 充值状态机与分录

```text
OBSERVED → VALIDATED → PENDING_FINALITY → READY_TO_CREDIT
    │           │             │                │
    └───────────┴─────────────┴──→ ORPHANED     ▼
                                      CREDIT_PENDING → CREDITED_HELD
                                                               │
                                         最终性+合规+资金闸门 ──┤
                                                               ▼
                                                         AVAILABLE

任何有效性冲突 → QUARANTINED
已入账后发现 orphan → REORG_FROZEN → REVERSAL_PENDING → REVERSED
```

这里的 `READY_TO_CREDIT` 不等于 `AVAILABLE`。一种保守方案是最终性后才写客户负债；另一种方案是先写不可消费的待确认负债，最终性后做负债桶迁移。二者只能选定一套会计口径，不能把“观测的充值”也算成已确认托管资产。

以下为待确认桶模型的概念分录，借贷方向以平台账套为准：

| 阶段 | 借方 | 贷方 | 是否可提 |
|---|---|---|---|
| 验证入块但未最终 | 链上待确认资产 | 客户待确认负债 | 否 |
| 最终性成立 | 托管可用链上资产；客户待确认负债 | 链上待确认资产；客户可用负债 | 是，仍受 KYT/额度控制 |
| pending 充值孤立 | 原分录的反向分录 | 原分录的反向分录 | 不产生可用额 |
| 已释放后异常回滚 | 冻结后冲正；不足部分进应收/损失科目 | 按原凭证逐笔反向 | 不允许硬扣出无依据的可用余额 |

“已释放后回滚”意味着信用政策风险、深度不足或最终性重大事故；不是普通扫块重试。用户可能已交易或提现，应冻结关联风险暴露、保留负债和损失的合法会计表达，升级风险/财务/合规，而不是让用户余额悄悄变负后继续放行。

### 36.4.3 假代币、假日志与回执校验矩阵

| 攻击/错误 | 为什么简单实现会信 | 必须的防线 |
|---|---|---|
| 同名/同 symbol 代币 | 名字与精度容易复制 | 白名单绑定 network、合约字节地址、资产 ID 与配置版本 |
| 其他合约 emit 同样 Transfer topic | topic 只代表 ABI 形状 | 检查日志 emitter 必须等于白名单合约；不能看 `tx.to` 代替 |
| RPC 编造白名单合约日志 | JSON 看起来都正确 | 受控节点校验；高保障场景用完整 receipts 重建 receipt root/证明并绑定已验证头 |
| 旧分叉上的真实日志 | 交易确实执行过 | receipt.blockHash、log.blockHash、canonical membership 与 epoch 一致 |
| 交易失败仍按 calldata 记账 | 用户输入只代表意图 | receipt.status=1；同时核验执行结果和资产净变动语义 |
| ERC-721 Transfer 被当 ERC-20 | 事件签名相同 | ERC-20 按支持 ABI 验证 topics 数、data 长度与资产类型 |
| `removed=false` 被当永久有效 | 依赖 WS 推送完整性 | `removed` 只是提示，最终依赖区块头与主链重建 |
| 回滚攻击后同 txHash 再入块 | 只用 txHash 去重 | 出现身份、业务效果周期分离；先冲正旧周期，再评估新执行 |
| 扣税/Rebase/代理升级 | Transfer 不一定等于可归属实收 | 独立适配器、升级闸门、按明确实收/份额口径；不支持则停充 |

对支持的标准 ERC-20，至少校验：

1. RPC 连接网络、白名单合约、配置生效高度正确；保存代码/实现版本策略。代理的实现变更不能仅靠代理地址不变而放行。
2. receipt 与交易 hash、block hash、交易索引匹配；状态成功；log 在该 receipt 内真实存在，非仅来自一次范围查询。
3. `topics[0]` 为 `Transfer(address,address,uint256)`，标准布局恰好 3 个 topics，data 恰好 32 字节；地址高位填充、长度和整数范围合法。
4. `to` 属于该网络、该账套的历史地址分配区间；`from=0` 的 mint 是否支持由资产策略决定，不凭“像转账”默认支持。
5. 金额使用原始 uint256 最小单位；`decimals` 是配置元数据，不信任前端或每次 RPC 返回值；零额、超限额、粉尘、未知归属进入独立路径。
6. 归集、热钱包调拨与客户充值分类互斥。内部地址转移不能再次制造客户负债。

对原生币，普通转账也不能只看 `tx.value`：需确认交易成功。合约内部转入依赖 trace 或受控合约事件的专门适配器，trace API 非标准且可能被裁剪，必须有完整性水位。禁止以“某块地址余额增加”替代逐笔归属。

**重要限制**：receipt 内存在日志，只能排除“范围日志与 receipt 不一致”，不能排除同一个恶意 RPC 同时伪造二者。头部的 `receiptsRoot` 也只有在该头本身经过共识验证时才有意义。余额差额同样不是通用的逐笔证据：一块内可有多笔进出、扣税或 Rebase，必须按支持的资产语义归因。

### 36.4.4 重入块与日志身份：避免“修复后又多记一笔”

RPC 的 `logIndex` 是**整块日志序号**。同一交易在另一分支的交易位置变化时，它的 `logIndex` 可能变化；同一交易在不同前置状态下执行，其日志数量、收款人或金额也可能变化。

因此：

- 保存原始 `block_global_log_index` 用于核验和取证，不把它当跨分叉稳定业务键。
- 以 receipt 内日志序号 `receipt_event_ordinal` 标识本次执行中的事件，带 `event_kind` 区分原生币、ERC-20、trace 等来源。
- 保存语义指纹 `H(asset, emitter, from, to, amount, adapter_version)`。相同 txHash 不代表执行语义永远相同；变化需重新校验，异常进入隔离。
- 维护唯一活跃业务槽 `(network, tx_hash, event_kind, receipt_event_ordinal)`，旧生效周期必须先完成冲正才能让新周期生效。重新执行多出的日志按新观测校验，不套旧金额。
- 连同一个 `block_hash` 都可能先 canonical、后 orphan、再 canonical。新生效周期必须增加 `activation_no`；不能因原来的 `credit_key` 已存在就永远不再入账，也不能再次使用旧释放键。

幂等键示例：

```text
credit:{deposit_id}:{activation_no}
release:{deposit_id}:{activation_no}
reverse:{original_journal_id}:{activation_no}
```

唯一键只解决某个效果不重复。跨分支的“先补偿旧效果，再激活新效果”由状态机和网络 epoch 共同保证，不能期望一个 `ON CONFLICT DO NOTHING` 自动完成。

---

## 36.5 EVM Nonce 池：并发提币不是并发抢链上计数

### 36.5.1 一个账户，一条序列，一个持久化所有者

对普通 EOA，Nonce 是该发送账户在该链上的顺序号。`latest` 代表节点当前 canonical 状态里的已消费计数，`pending` 只是该节点本地 pending 视图，可能缺交易、被替换、受 txpool 限制，且不同节点可能不同。

```text
业务请求 W1 ─┐
业务请求 W2 ─┼─► DB 原子分配器 → nonce 817 → 同槽所有 attempts
业务请求 W3 ─┘                  nonce 818 → 同槽所有 attempts
                                nonce 819 → 同槽所有 attempts

链上期望 817：如果 817 未确认，818/819 通常不能先执行。
```

生产规则：

1. 每个发送账户由一个调度域控制，签名器拒绝绕过该域的请求；同私钥人工转账、归集和提币都纳入同一序列。
2. 数据库账户行锁串行配号，`UNIQUE(network,sender,nonce)` 最终兜底。分布式锁只用于降低争用，不作为唯一正确性来源。
3. 分配前，业务请求已完成幂等、风控、地址检查与资金/Gas 预占。分配和绑定业务意图同事务；不能先拿号再远程扣余额。
4. 账户 `next_nonce` 只前进；已分配槽不能因 worker 超时随意回收给另一笔付款。
5. 可以并行准备多个槽和签名，广播并发受 `max_inflight`、余额、txpool queued 限制与 head-of-line 策略约束。
6. 对同一热钱包做负载分片只能换多个发送账户；把同账户拆成多套独立 Redis 计数器不会增加链上执行并行度。

初始启用账户时，用可信 canonical 状态计数建立基线，并核查本地签名记录和历史交易。不能反复执行 `next_nonce = max(db, latest, pending)`：这会掩盖未知外部签名、跳过待恢复槽或形成不可解释的空洞。

### 36.5.2 槽、尝试与业务请求是三种对象

| 对象 | 不变内容 | 可变状态 |
|---|---|---|
| `WithdrawalIntent` | 客户、资产、实付数量、收款地址、审批、业务幂等键 | 冻结、处理中、成功、取消、异常 |
| `NonceSlot` | network、sender、nonce、原始业务意图 | 分配、签名中、广播未知、入块、最终、人工处置 |
| `TxAttempt` | 签名原文、raw tx、txHash、费用参数、签名策略版本 | 广播结果、候选 receipt、orphan、canonical winner |

正常 RBF 只修改被允许的 Gas 参数，`chainId/nonce/to/value/data` 与访问列表等执行意图字段保持一致；`gasLimit` 变更必须走明确的重估和审批策略。ERC-20 提币的 `to` 是代币合约，客户收款地址在 calldata 中，签名策略必须解码后校验。

用户发起的业务取消不等于普通 RBF。零额自转账取消是独立的 `CANCEL` 尝试，明确改变执行意图，需更高授权并绑定原槽；在取消交易达到策略最终性前，不能释放本金冻结，因为原付款仍可能胜出。

### 36.5.3 崩溃与未知态恢复

| 崩溃点 | 持久化证据 | 安全恢复 |
|---|---|---|
| 配号事务提交前 | 无槽 | 重试原业务身份 |
| 配号后、未请求签名 | 已绑定槽 | 原槽继续签名，不改 Nonce |
| 签名 RPC 超时 | sign_request_id 与原文摘要 | 查询签名器；禁止另建付款槽 |
| 拿到 raw tx、未广播 | attempt 已落库 | 广播同一 raw tx |
| 广播超时 | txHash、raw tx 已落库 | 查所有来源，必要时重播同字节，记 UNKNOWN |
| 返回 nonce too low | nonce 可能已被任一交易消费 | 找 canonical winner，不能直接当作成功 |
| 节点查不到交易 | 仅证明该节点没看到 | 继续查所有 attempts、账户历史与可信节点 |
| 最终性前 receipt 消失 | 可能 Reorg | 槽退回待观察/广播，不再扣一次本金 |

签名器必须支持按 `sign_request_id` 审计与幂等查结果；同一业务原文可以因签名随机性产生不同签名字节，不能把“原文一样”当作“txHash 一定一样”。只有已持久化的签名交易才允许出签名域广播。签名器若直接广播，也必须提供等价的持久化 outbox 与完整审计。

---

## 36.6 EIP-1559 RBF：同 Nonce 提价，不是再付款一次

### 36.6.1 两个费用上限必须分清

类型 2 交易：

```text
F = maxFeePerGas
P = maxPriorityFeePerGas
B = 该区块 baseFeePerGas

可被正常纳入的必要条件之一：F ≥ B，且 F ≥ P
effective_tip   = min(P, F - B)
effective_price = B + effective_tip = min(F, B + P)
actual_gas_fee  = gasUsed × effective_price
最大普通执行 Gas 预算 = gasLimit × F
```

还应分开核算交易 `value`、平台提币手续费、L2 的 L1 data fee 等链特定费用；普通 ERC-20 calldata 交易不是 blob 交易，但不能由此忽略 Rollup 的额外计费。`gasLimit × F` 是预算上界，不是最终从客户实扣的 Gas。

### 36.6.2 提价规则与预算

客户端 txpool 的 replacement 门槛不是 EIP-1559 规定的统一常数，节点实现、版本、交易类型和 RPC 网关策略都可能不同。需配置与压测；“提高 10% 一定通过”不是协议保证。

设受控节点要求至少提高比例 `r`，对该槽**所有已签名尝试的费用分量上界**提价，避免只超过最近一次却没超过另一节点持有的旧交易：

```text
ceil_bump(x,r) = max(x+1, ceil(x × (1+r)))
P_new = max(ceil_bump(max_seen_P,r), tip_quote)
F_new = max(ceil_bump(max_seen_F,r), B_budget + P_new)
```

`B_budget` 可按目标确认块数对 base fee 保守外推。在标准 EIP-1559 参数下，满块导致单块 base fee 最多约增长 12.5%；兼容链参数可能不同，不能硬编码为所有网络的事实。`tip_quote` 来自受控 fee history 和成交延迟目标，不完全信任单个 RPC 的建议。

```text
if new_fee_budget > approved_budget:
    → NEEDS_FEE_APPROVAL，优先维护队首槽，告警
else:
    原子提高资金预占上限 → 产生新 attempt → 签名 → 持久化 → 广播
```

不能先签高费用交易、再发现账户不够钱；已签交易可能被广播。RBF 不能累计冻结多份本金，Gas 冻结应提升到同槽可胜出尝试的最大风险预算；旧高价尝试仍有效时，不能按新低价预算解冻。

### 36.6.3 队首优先与旧交易胜出

先处理链上期望的最小未消费 Nonce，再处理后续槽。不断加速 819 却不修复 817 不会解锁账户。

每次巡检查询**所有 txHash**。假设 attempt A 与 B 同 Nonce，B 的费用更高，但 A 已到某个出块者：A 完全可能先上链。系统以经过验证的 canonical receipt 选 winner，而不是以“最后签名的 attempt”为 winner。

取消与付款竞赛也如此：

```text
原付款胜出 → 确认实付与费用 → 最终后成功扣账；取消请求返回未生效
取消胜出   → 最终后释放本金，按政策计取消 Gas → 业务取消完成
其他未知交易消费该 nonce → SIGNER_SCOPE_INCIDENT，隔离发送账户
receipt.status=0 → nonce 已消费、Gas 已花，但付款不算成功
```

失败执行若需重新支付，必须在旧槽最终结果明确后，以有审计关联的新业务尝试/新槽继续；不能把 status=0 当作“交易从未发送”，也不能把原槽换个 Nonce 盲发。

---

## 36.7 PostgreSQL DDL：把正确性落实为持久化约束

以下是单网络逻辑控制、分支证据、充值周期和 EOA Nonce 池的最小核心。UUID 由应用生成；金额、Nonce、chainId 均用足够精度整数，禁止浮点。`NUMERIC(78,0)` 足以容纳 uint256 的十进制位数，但仍需上界检查。

```sql
CREATE DOMAIN evm_hash AS BYTEA CHECK (octet_length(VALUE) = 32);
CREATE DOMAIN evm_address AS BYTEA CHECK (octet_length(VALUE) = 20);
CREATE DOMAIN uint256 AS NUMERIC(78,0)
  CHECK (VALUE >= 0 AND VALUE <=
    115792089237316195423570985008687907853269984665640564039457584007913129639935);
CREATE DOMAIN evm_nonce AS NUMERIC(20,0)
  CHECK (VALUE >= 0 AND VALUE < 18446744073709551616);

CREATE TABLE chain_control (
  network_id       BIGINT PRIMARY KEY,
  chain_id         uint256 NOT NULL,
  genesis_hash     evm_hash NOT NULL,
  mode             TEXT NOT NULL CHECK
    (mode IN ('RUNNING','REORGING','DEEP_REORG_HALT','FINALITY_CONFLICT')),
  epoch            BIGINT NOT NULL CHECK (epoch >= 0),
  fencing_token    BIGINT NOT NULL CHECK (fencing_token >= 0),
  head_height      BIGINT NOT NULL CHECK (head_height >= 0),
  head_hash        evm_hash NOT NULL,
  indexed_height   BIGINT NOT NULL CHECK (indexed_height >= 0),
  finalized_height BIGINT NOT NULL CHECK (finalized_height >= 0),
  finalized_hash   evm_hash NOT NULL,
  policy_version   TEXT NOT NULL,
  CHECK (finalized_height <= indexed_height AND indexed_height <= head_height)
);

CREATE TABLE chain_header (
  network_id       BIGINT NOT NULL REFERENCES chain_control(network_id),
  block_hash       evm_hash NOT NULL,
  height           BIGINT NOT NULL CHECK (height >= 0),
  parent_hash      evm_hash NOT NULL,
  receipts_root    evm_hash NOT NULL,
  block_time       TIMESTAMPTZ NOT NULL,
  canonical        BOOLEAN NOT NULL DEFAULT FALSE,
  evidence_ref     TEXT NOT NULL,
  verification     TEXT NOT NULL,
  PRIMARY KEY (network_id, block_hash)
);
CREATE UNIQUE INDEX uq_canonical_height
  ON chain_header(network_id, height) WHERE canonical;
CREATE INDEX ix_header_parent ON chain_header(network_id, parent_hash);

CREATE TABLE chain_observation (
  observation_id   UUID PRIMARY KEY,
  network_id       BIGINT NOT NULL,
  block_hash       evm_hash NOT NULL,
  tx_hash          evm_hash NOT NULL,
  event_kind       TEXT NOT NULL,
  receipt_ordinal  INTEGER NOT NULL CHECK (receipt_ordinal >= 0),
  block_log_index  INTEGER CHECK (block_log_index >= 0),
  asset_id         TEXT NOT NULL,
  emitter          evm_address,
  receiver         evm_address NOT NULL,
  amount_raw       uint256 NOT NULL,
  semantic_hash    evm_hash NOT NULL,
  adapter_version  TEXT NOT NULL,
  raw_ref          TEXT NOT NULL,
  UNIQUE (network_id, block_hash, tx_hash, event_kind, receipt_ordinal),
  FOREIGN KEY (network_id, block_hash)
    REFERENCES chain_header(network_id, block_hash)
);

-- 一个逻辑事件可以有多个分支出现；同一时刻只允许一个未结束的经济周期。
CREATE TABLE deposit_activation (
  activation_id    UUID PRIMARY KEY,
  deposit_id       UUID NOT NULL,
  activation_no    BIGINT NOT NULL CHECK (activation_no > 0),
  observation_id   UUID NOT NULL REFERENCES chain_observation(observation_id),
  network_id       BIGINT NOT NULL,
  tx_hash          evm_hash NOT NULL,
  event_kind       TEXT NOT NULL,
  receipt_ordinal  INTEGER NOT NULL CHECK (receipt_ordinal >= 0),
  epoch            BIGINT NOT NULL,
  policy_version   TEXT NOT NULL,
  state            TEXT NOT NULL CHECK (state IN (
    'PENDING_FINALITY','CREDIT_PENDING','CREDITED_HELD','AVAILABLE',
    'REORG_FROZEN','REVERSAL_PENDING','REVERSED','ORPHANED','QUARANTINED')),
  active           BOOLEAN NOT NULL,
  version          BIGINT NOT NULL DEFAULT 0,
  UNIQUE (deposit_id, activation_no),
  CHECK (state NOT IN ('REVERSED','ORPHANED') OR NOT active)
);
CREATE UNIQUE INDEX uq_active_deposit_event ON deposit_activation
  (network_id, tx_hash, event_kind, receipt_ordinal) WHERE active;

CREATE TABLE wallet_nonce_state (
  network_id       BIGINT NOT NULL REFERENCES chain_control(network_id),
  sender           evm_address NOT NULL,
  next_nonce       evm_nonce NOT NULL,
  mode             TEXT NOT NULL CHECK (mode IN ('ACTIVE','PAUSED','INCIDENT')),
  fencing_token    BIGINT NOT NULL,
  version          BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (network_id, sender)
);

-- 每行对应已审批、已预占的一个业务意图。重试必须校验完整规范化指纹。
CREATE TABLE withdrawal_intent (
  withdrawal_id    UUID PRIMARY KEY,
  network_id       BIGINT NOT NULL,
  sender           evm_address NOT NULL,
  business_key     TEXT NOT NULL UNIQUE,
  payload_hash     evm_hash NOT NULL,
  state            TEXT NOT NULL CHECK (state IN (
    'RESERVED','PROCESSING','FINAL_SUCCESS','FINAL_FAILED','CANCELED','UNKNOWN')),
  reservation_ref  TEXT NOT NULL,
  FOREIGN KEY (network_id, sender)
    REFERENCES wallet_nonce_state(network_id, sender)
);

CREATE TABLE nonce_slot (
  slot_id          UUID PRIMARY KEY,
  withdrawal_id    UUID NOT NULL UNIQUE REFERENCES withdrawal_intent(withdrawal_id),
  network_id       BIGINT NOT NULL,
  sender           evm_address NOT NULL,
  nonce_value      evm_nonce NOT NULL,
  intent_hash      evm_hash NOT NULL,
  state            TEXT NOT NULL CHECK (state IN (
    'ALLOCATED','SIGNING','BROADCAST_UNKNOWN','PENDING','INCLUDED',
    'FINAL_SUCCESS','FINAL_REVERT','FINAL_CANCEL','INCIDENT')),
  winner_hash      evm_hash,
  version          BIGINT NOT NULL DEFAULT 0,
  UNIQUE (network_id, sender, nonce_value),
  FOREIGN KEY (network_id, sender)
    REFERENCES wallet_nonce_state(network_id, sender)
);

CREATE TABLE tx_attempt (
  attempt_id       UUID PRIMARY KEY,
  slot_id          UUID NOT NULL REFERENCES nonce_slot(slot_id),
  attempt_no       INTEGER NOT NULL CHECK (attempt_no > 0),
  attempt_kind     TEXT NOT NULL CHECK (attempt_kind IN ('PAYMENT','CANCEL')),
  sign_request_id  UUID NOT NULL UNIQUE,
  unsigned_hash   evm_hash NOT NULL,
  tx_hash          evm_hash UNIQUE,
  raw_tx_ref       TEXT,
  max_fee         uint256 NOT NULL,
  priority_fee    uint256 NOT NULL,
  gas_limit       NUMERIC(20,0) NOT NULL CHECK (gas_limit > 0),
  state           TEXT NOT NULL CHECK (state IN (
    'SIGN_REQUESTED','SIGNED','BROADCAST_UNKNOWN','PENDING','INCLUDED',
    'ORPHANED','FINAL','SUPERSEDED','REJECTED')),
  version         BIGINT NOT NULL DEFAULT 0,
  UNIQUE (slot_id, attempt_no),
  CHECK (max_fee >= priority_fee),
  CHECK ((tx_hash IS NULL) = (raw_tx_ref IS NULL))
);

CREATE TABLE reorg_job (
  job_id          UUID PRIMARY KEY,
  network_id      BIGINT NOT NULL REFERENCES chain_control(network_id),
  epoch           BIGINT NOT NULL,
  old_head        evm_hash NOT NULL,
  new_head        evm_hash NOT NULL,
  ancestor_hash   evm_hash NOT NULL,
  phase           TEXT NOT NULL CHECK (phase IN (
    'DETECTED','FROZEN','REVERSING','REPLAYING','VERIFYING','DONE','HALTED')),
  checkpoint      JSONB NOT NULL,
  evidence_ref    TEXT NOT NULL,
  UNIQUE (network_id, epoch)
);

-- 与状态变化同事务追加；账本自己还要以 effect_key 建唯一凭证。
CREATE TABLE chain_outbox (
  event_id        UUID PRIMARY KEY,
  effect_key      TEXT NOT NULL UNIQUE,
  network_id      BIGINT NOT NULL,
  epoch           BIGINT NOT NULL,
  aggregate_id    UUID NOT NULL,
  aggregate_seq   BIGINT NOT NULL,
  event_type      TEXT NOT NULL,
  payload         JSONB NOT NULL,
  payload_hash    evm_hash NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  delivered_at    TIMESTAMPTZ,
  UNIQUE (aggregate_id, aggregate_seq)
);
```

这些约束仍不能证明所有跨表语义，应用发布事务还须检查：activation 的 network/tx/ordinal 与 observation 完全一致；slot 的 network/sender 与 intent 一致；winner 属于该 slot；原始交易经本地解码和恢复签名后与授权一致；canonical 连续、epoch 有效、最终性单调。

`SUPERSEDED` 只是调度上不再优先广播，不意味着链上失效。`raw_tx_ref` 指向受控持久化对象，广播 worker 先核验内容摘要和本地计算的 txHash。签名交易不是私钥，但具有可执行付款能力，不能暴露到普通应用日志。

生产扩展还需资产白名单、历史地址分配、receipt/trace 证据、账本 effect 回执、审计变更表与恢复任务租约。大表分区时，PostgreSQL 跨分区唯一键约束有要求；不可为了按月分区而丢掉原来的全局幂等约束，应保留独立的全局注册/幂等表。

---

## 36.8 运行时状态机、恢复屏障与对账：全章的闭环接口

### 36.8.1 四台状态机的联动

| 状态机 | 正常路径 | 异常回边 | 终结依据 |
|---|---|---|---|
| 网络 | RUNNING → REORGING → RUNNING | 深重组/最终性冲突 → HALT | 新链连续且补偿完成 |
| 充值 | VALIDATED → HELD → AVAILABLE | 任一孤块 → FROZEN/ORPHANED → REVERSED | 指定最终性 + 账本回执 |
| Nonce 槽 | ALLOCATED → PENDING → INCLUDED → FINAL | INCLUDED → PENDING；未知消费 → INCIDENT | 任一 attempt 的 canonical 最终结果 |
| 提币业务 | RESERVED → PROCESSING → FINAL_SUCCESS | UNKNOWN/失败/取消 | 本金、费用、链上交付三者一致 |

`INCLUDED` 不是提币最终成功；`receipt.status=1` 也不保证 ERC-20 实际付款成功：不规范代币可能返回 false 而不 revert，适配器还需验证收款净变动或预期事件。多调用合约还可能只成功一部分，不能用外层 status 替代每个业务动作结果。

一条需要写进代码审查模板的规则：**不确定性可以扩大，但不能凭超时收缩成成功或失败。** UNKNOWN 是合法的受控状态，不是等待运维手工改成某个终态的“脏数据”。

### 36.8.2 四层对账与快照口径

每个对账批次固定 `(network, canonical_epoch, finalized_height, finalized_hash, adapter_version, ledger_watermark)`；涉及多链时分别记录每条链的快照，不存在天然统一块高。

1. **头部对账**：canonical parent 连续、唯一高度、finalized checkpoint 祖先关系；范围解析无缺口。
2. **证据对账**：支持资产的 receipt/log 完整性、地址归属、语义指纹、activation 与当前分支一致；范围日志数量与逐块采集交叉核验。
3. **业务对账**：充值 observation → activation → credit/release/reverse；提币 intent → slot → 所有 attempts → winner → 本金/Gas/平台费。
4. **资产负债对账**：同一最终快照下，受控地址链上余额、内部钱包资产子账、客户负债与平台资金、在途/暂收/手续费/归集应收分别桥接。

不要比较“链上当前余额”和“昨夜账本余额”。使用支持按 hash 固定状态的 RPC，例如可用时的 EIP-1898 形式，并验证 canonical 要求；否则在读取前后核验块 hash，发现变化整批作废。归档能力不足时不能偷偷用 latest 代替历史状态。

普通原生币地址可先做守恒核验：

```text
closing = opening + canonical_in_value - canonical_out_value - actual_gas_paid
          + 链特定奖励/系统转账/其他明确支持的调整
```

代币需按该资产协议建立独立方程；不能用一条 ERC-20 Transfer 求和公式包办 Rebase、扣税、黑名单和代理升级。平台内部归集会改变地址余额，不应改变客户负债总量。

### 36.8.3 Nonce 对账与空洞修复

在**已锁定的可信链视图**读取账户计数 `N`，将本地槽分成：

- `nonce < N`：当前链状态认为已经消费；逐槽找到支付、取消、revert 或未知外部交易的证据。`N` 增大本身不能证明付款成功。
- `nonce = N`：当前队首，优先查 pending、重播、提价或受审批的取消。
- `nonce > N`：未来槽，受限保留；不要为每个槽同时启动无界 RBF。

若账户只用了三年后才接入本系统，历史基线之前的 Nonce 不要求凭空建立本地槽，但基线之后任何未知消耗都必须解释。重组可能让 `latest` 计数降低，此时重新打开未最终槽；**不回收本地已分配号，也不让 `next_nonce` 后退**。

空洞修复顺序：找已分配槽 → 查签名记录 → 恢复同槽交易 → 校验账户资金 → 重播/RBF → 必要时授权取消。所谓“零额填洞”不是无害操作：它花 Gas、改变序列，且可能与原付款竞争，需要同槽与审计绑定。

### 36.8.4 恢复门禁与可审计人工操作

人工只能提交状态机指令，例如 `REPLAY_RANGE`、`APPROVE_FEE_BUDGET`、`REQUEST_CANCEL`、`ACK_DEEP_REORG_PLAN`。每个指令记录操作者、审批人、证据、旧版本、范围与幂等键；禁止直接改 `next_nonce`、winner 或可用余额。

恢复成功的验收不是“日志不再报错”，而是：旧 epoch 释放被阻挡，旧分支效果有反向凭证，所有活跃周期对应新主链，Nonce 无未知消费，账本与最终快照一致，重跑恢复任务不产生任何新增经济影响。

---

## 36.9 Go 核心代码：父链规划与原子 Nonce 分配

以下代码是可测试的核心，不是把整个节点与账本实现塞进一个函数。RPC 认证、共识验证、数据库驱动、地址解码与指标接在接口外；不得因为示例短而省掉接口契约。

### 36.9.1 按 hash 追溯共同祖先，失败则停机

```go
package chaincore

import (
    "context"
    "errors"
    "fmt"
)

type Hash [32]byte
type Header struct {
    Height uint64
    Hash   Hash
    Parent Hash
}

type View interface {
    // 必须返回同一只读快照；获取失败不能伪装成“不在主链”。
    CanonicalHash(ctx context.Context, height uint64) (Hash, bool, error)
    // 必须按 hash 获取，且已验证响应 hash/头部编码与可信节点链视图。
    HeaderByHash(ctx context.Context, hash Hash) (Header, error)
}

type Plan struct {
    Ancestor Header
    Forward  []Header // 从祖先的下一块到 candidate，正序
}

func BuildPlan(ctx context.Context, v View, candidate Header,
    finalized Header, maxNewHeaders uint64) (Plan, error) {
    var reverse []Header
    cur := candidate // candidate 必须已通过网络与头部验证
    for {
        if err := ctx.Err(); err != nil {
            return Plan{}, err
        }
        local, exists, err := v.CanonicalHash(ctx, cur.Height)
        if err != nil {
            return Plan{}, err
        }
        if exists && local == cur.Hash {
            // 候选不得连到已 finalized 之前的旧祖先再改写 checkpoint。
            if cur.Height < finalized.Height {
                return Plan{}, errors.New("FINALITY_CONFLICT")
            }
            for i, j := 0, len(reverse)-1; i < j; i, j = i+1, j-1 {
                reverse[i], reverse[j] = reverse[j], reverse[i]
            }
            return Plan{Ancestor: cur, Forward: reverse}, nil
        }
        if cur.Height <= finalized.Height {
            return Plan{}, errors.New("FINALITY_CONFLICT")
        }
        if uint64(len(reverse)) >= maxNewHeaders {
            return Plan{}, errors.New("WINDOW_EXCEEDED_HALT")
        }
        reverse = append(reverse, cur)
        parent, err := v.HeaderByHash(ctx, cur.Parent)
        if err != nil {
            return Plan{}, fmt.Errorf("parent unavailable: %w", err)
        }
        if parent.Hash != cur.Parent || parent.Height != cur.Height-1 {
            return Plan{}, errors.New("INVALID_PARENT_LINK")
        }
        cur = parent
    }
}
```

调用者还必须检查**旧 head 到共同祖先的深度**未超过可自动恢复范围，并验证本地 finalized 本身属于当前只读快照。`BuildPlan` 只证明候选父链与本地前缀如何衔接，不决定哪个分叉获胜。

发布时执行短事务：

```text
SELECT chain_control FOR UPDATE
assert expected_epoch/head_hash/fencing_token 与当前行一致
assert 候选仍被受控节点认作 canonical，最终性证据未冲突
如是简单延伸：写 header + observation + outbox + 连续水位
如是替换：先写 REORGING/job，交由 36.8 恢复协议推进
COMMIT
```

网络在事务后仍可能重组，因此这里不是声称“锁数据库即可锁住区块链”。它保证本地发布顺序；真正的资金安全仍依赖最终性和补偿屏障。

### 36.9.2 使用行锁分配 Nonce

下面与 36.7 DDL 对齐。所有调度器统一先锁业务行，再锁账户行，避免相反锁序形成死锁；调用前业务冻结已获得可靠回执。示例用字符串承载超出有符号 64 位的 Nonce，运算用 `big.Int`。

```go
package chaincore

import (
    "bytes"
    "context"
    "database/sql"
    "errors"
    "math/big"
)

func AllocateNonce(ctx context.Context, db *sql.DB, withdrawalID, newSlotID string,
    expectedPayload []byte, expectedFence int64) (string, error) {
    if len(expectedPayload) != 32 {
        return "", errors.New("invalid payload hash")
    }
    tx, err := db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
    if err != nil { return "", err }
    defer tx.Rollback()

    var network int64
    var sender, payload []byte
    var state string
    err = tx.QueryRowContext(ctx, `
        SELECT network_id, sender, payload_hash, state
          FROM withdrawal_intent WHERE withdrawal_id=$1 FOR UPDATE`,
        withdrawalID).Scan(&network, &sender, &payload, &state)
    if err != nil { return "", err }
    if !bytes.Equal(payload, expectedPayload) {
        return "", errors.New("IDEMPOTENCY_PAYLOAD_CONFLICT")
    }

    var existing string
    err = tx.QueryRowContext(ctx, `
        SELECT nonce_value::text FROM nonce_slot WHERE withdrawal_id=$1`,
        withdrawalID).Scan(&existing)
    if err == nil {
        if err = tx.Commit(); err != nil { return "", err }
        return existing, nil // 即使业务已终态，原身份重试仍返回原槽
    }
    if !errors.Is(err, sql.ErrNoRows) { return "", err }
    if state != "RESERVED" { return "", errors.New("NOT_RESERVED") }

    var next, mode string
    var fence int64
    err = tx.QueryRowContext(ctx, `
        SELECT next_nonce::text, mode, fencing_token
          FROM wallet_nonce_state
         WHERE network_id=$1 AND sender=$2 FOR UPDATE`,
        network, sender).Scan(&next, &mode, &fence)
    if err != nil { return "", err }
    if mode != "ACTIVE" || fence != expectedFence {
        return "", errors.New("WALLET_FENCED_OR_PAUSED")
    }
    n, ok := new(big.Int).SetString(next, 10)
    if !ok || n.Sign() < 0 { return "", errors.New("INVALID_NONCE") }
    nextN := new(big.Int).Add(n, big.NewInt(1))
    // 保守拒绝上限附近账户；不让 next_nonce 溢出其数据库域。
    limit := new(big.Int).Lsh(big.NewInt(1), 64)
    if nextN.Cmp(limit) >= 0 { return "", errors.New("NONCE_EXHAUSTED") }

    _, err = tx.ExecContext(ctx, `
        INSERT INTO nonce_slot(slot_id,withdrawal_id,network_id,sender,
                               nonce_value,intent_hash,state)
        VALUES($1,$2,$3,$4,$5::numeric,$6,'ALLOCATED')`,
        newSlotID, withdrawalID, network, sender, next, payload)
    if err != nil { return "", err }
    _, err = tx.ExecContext(ctx, `
        UPDATE wallet_nonce_state SET next_nonce=$3::numeric, version=version+1
         WHERE network_id=$1 AND sender=$2`, network, sender, nextN.String())
    if err != nil { return "", err }
    _, err = tx.ExecContext(ctx, `
        UPDATE withdrawal_intent SET state='PROCESSING' WHERE withdrawal_id=$1`,
        withdrawalID)
    if err != nil { return "", err }
    if err = tx.Commit(); err != nil {
        // COMMIT 断线可能已经提交：上层按原 withdrawalID 查询/重试。
        return "", err
    }
    return next, nil
}
```

代码返回的是已提交槽位，不发送 RPC，也不签名。待签名 worker 可以轮询 `ALLOCATED`；若依赖消息唤醒，必须同事务写 Outbox。`intent_hash` 在这里指审批通过的规范化业务载荷摘要，签名器还需将其映射为 chainId、to、value、calldata 等严格授权字段；不能签“只要 hash 相同就接受任意交易”的空白授权。

账户暂停与分配使用同一账户行锁。网络重组时，分配出的槽可以继续保持冻结，但签名/广播闸门必须检查网络策略；原本已签的交易无法靠数据库状态撤回。签名器也要校验 fencing，而不能只在配号阶段检查一次。

---

## 36.10 Java 核心代码：严格费用提价与候选回执仲裁

### 36.10.1 BigInteger 费用规划，不使用 double

```java
import java.math.BigInteger;
import java.util.Collection;

public final class RbfPlanner {
    private static final BigInteger BPS = BigInteger.valueOf(10_000);
    private static final BigInteger MAX_U256 =
        BigInteger.ONE.shiftLeft(256).subtract(BigInteger.ONE);

    public record Fees(BigInteger maxFee, BigInteger priority) {}

    private static void uint256(BigInteger x) {
        if (x == null || x.signum() < 0 || x.compareTo(MAX_U256) > 0) {
            throw new IllegalArgumentException("uint256 out of range");
        }
    }

    private static BigInteger bump(BigInteger old, int bumpBps) {
        BigInteger numerator = old.multiply(BPS.add(BigInteger.valueOf(bumpBps)));
        BigInteger ceiling = numerator.add(BPS).subtract(BigInteger.ONE).divide(BPS);
        return ceiling.max(old.add(BigInteger.ONE));
    }

    public static Fees plan(Collection<Fees> signedAttempts,
                            int bumpBps,
                            BigInteger baseFeeBudget,
                            BigInteger quotedTip,
                            BigInteger gasLimit,
                            BigInteger approvedGasBudget) {
        if (signedAttempts.isEmpty() || bumpBps <= 0 || bumpBps > 100_000) {
            throw new IllegalArgumentException("invalid replacement policy");
        }
        uint256(baseFeeBudget);
        uint256(quotedTip);
        uint256(gasLimit);
        uint256(approvedGasBudget);
        if (gasLimit.signum() == 0) throw new IllegalArgumentException("zero gas");

        BigInteger maxF = BigInteger.ZERO;
        BigInteger maxP = BigInteger.ZERO;
        for (Fees old : signedAttempts) {
            uint256(old.maxFee());
            uint256(old.priority());
            if (old.maxFee().compareTo(old.priority()) < 0) {
                throw new IllegalArgumentException("invalid old fee caps");
            }
            maxF = maxF.max(old.maxFee());
            maxP = maxP.max(old.priority());
        }
        BigInteger p = bump(maxP, bumpBps).max(quotedTip);
        BigInteger f = bump(maxF, bumpBps).max(baseFeeBudget.add(p));
        uint256(p);
        uint256(f);
        if (gasLimit.multiply(f).compareTo(approvedGasBudget) > 0) {
            throw new IllegalStateException("NEEDS_FEE_APPROVAL");
        }
        return new Fees(f, p);
    }

    private RbfPlanner() {}
}
```

例：旧上限 `F=40 gwei/P=2 gwei`，配置提高 12.5%，`B_budget=60 gwei`，建议 tip 为 `3 gwei`，得到 `P_new=3`、`F_new=63 gwei`。旧交易即使 fee cap 过低而暂时不能入块，也未因此失效；base fee 回落后仍可能被执行。

费用规划之后要锁 slot，检查 version 与所有签名尝试集合未改变，原子提高预占预算并登记 `SIGN_REQUESTED`。否则两个 worker 都按过期上界生成新 attempt，可能既不满足替换门槛又重复消耗签名额度。

### 36.10.2 只从当前链视图选候选 winner

下列仲裁核心使用规范化的 32 字节 hash 十六进制字符串作为键；它不直接信任 Web3j 返回的 Java 对象。适配层负责严格解析长度、receipt 状态与交易归属。

```java
import java.util.List;
import java.util.Map;

public final class InclusionArbiter {
    public record Receipt(String txHash, String blockHash, long height,
                          boolean executionSucceeded) {}

    public record ChainView(long epoch, Map<Long, String> canonicalHashes,
                            long finalizedHeight, String finalizedHash) {}

    public static Receipt choose(List<Receipt> verifiedSlotReceipts, ChainView view) {
        Receipt winner = null;
        for (Receipt r : verifiedSlotReceipts) {
            if (!r.blockHash().equals(view.canonicalHashes().get(r.height()))) {
                continue; // 旧分叉或未完整索引的块，不授权终结
            }
            if (winner != null && !winner.txHash().equals(r.txHash())) {
                // 同一个 EOA nonce 的两个不同交易不能同时 canonical。
                throw new IllegalStateException("IMPOSSIBLE_DOUBLE_WINNER_HALT");
            }
            winner = r;
        }
        return winner; // null 代表无可证明候选，不代表付款失败
    }

    private InclusionArbiter() {}
}
```

接口契约：传入的每个 receipt 必须已核验交易签名恢复得到的 sender、chainId、nonce 与 slot 一致，且原始交易/业务语义属于已授权 attempt。`canonicalHashes` 是同一 epoch 的不可变、父子连续视图，不是多次读 RPC 拼出的 map。

`choose` 只认定 `INCLUDED` 候选。终结事务还需核验当前网络为 RUNNING、epoch 未变、checkpoint 身份和祖先关系真实成立、winner 达到所需最终性。`height <= finalizedHeight` 单独不够。普通链的 `executionSucceeded=false` 仍然是有效 nonce 消耗，但业务结果应为 `FINAL_REVERT`，费用照实记账；代币付款还要做资产效果校验。

---

## 36.11 压测、可观测性与故障演练

### 36.11.1 测试不是“每秒扫了多少块”

分为模型测试、回放、集成和压力四层：

1. **模型/属性测试**：随机生成区块树、重复/乱序通知、重组和进程崩溃点。每一步断言唯一 canonical、高度连续、有效账本效果不重复、槽所有权不变。
2. **确定性回放**：相同原始头、receipt、配置版本与人工指令，重建相同的链视图、activation、slot winner 与会计净效果；随机 UUID/观测时间不参与业务结果比较。
3. **集成测试**：本地可控 EVM/devnet 或多节点网络制造分叉、替换和出块暂停。开发链的 `snapshot/revert` 可以模拟应用回滚，但不等价于真实共识重组，仍要做多节点切链演练。
4. **压力测试**：分开测 RPC 下载、解析、数据库发布、账本消费、签名吞吐和 txpool。使用高日志块、热点地址、单钱包队首阻塞与大量 future Nonce，而不是只测空块。

量级目标要从网络峰值推导。例如：

```text
峰值 20,000 条支持资产日志/块，12 秒/块
持续输入约 1,667 条/秒；要求解析+落库至少 3 倍余量
停机 2 小时约积压 12,000,000 条
若恢复净追赶能力为 5,000 条/秒，约需 40 分钟清空
```

净追赶能力是总吞吐减去实时新增吞吐。不能拿离线全速数字承诺在线追赶时间；还应预留重组、签名、对账和数据库副本追赶的容量。

### 36.11.2 指标、告警与自动熔断

| 指标 | 含义 | 推荐动作 |
|---|---|---|
| `head_lag/indexed_gap/release_lag` | 链视图、解析和资金释放的分层落后 | 分层告警，不合并成“扫块正常” |
| `parent_mismatch/reorg_depth` | 分叉频率、深度 | 超策略阈值暂停释放 |
| `finality_age/finality_conflict` | 最终性停滞或冲突 | 冲突立即 P0；停滞禁止超时放行 |
| `rpc_disagreement/receipt_root_mismatch` | 来源不一致或证据错误 | 隔离源、留证、受控停充 |
| `old_epoch_effect_rejected` | 过期释放流量 | 确认重组屏障起效；异常持续则查积压 |
| `nonce_head_age/nonce_gap/inflight` | 队首阻塞、空洞、未来队列 | 队首先恢复，限制继续配号 |
| `replacement_reject/fee_budget_exhausted` | 提价策略或余额不足 | 不无界重签；升级预算审批 |
| `unknown_nonce_consumption` | 非本域交易消费 Nonce | 隔离账户与签名域，安全事故 |
| `outbox_age/ledger_ack_lag` | 状态到经济效果的落后 | 停止依赖未确认效果继续出款 |
| `asset_liability_diff/reversal_pending_age` | 资产缺口与未闭合补偿 | 风险/财务联合处置 |

指标标签只保留有限 network、资产、状态等维度；txHash、地址和 withdrawal_id 放到受控追踪系统，避免高基数拖垮监控并泄漏客户行为。

### 36.11.3 故障演练表与验收断言

| 故障注入 | 预期行为 | 必须验证 |
|---|---|---|
| 连续 3 块重组，含充值与提币 | epoch 切换，冻结、反向分录、新链重放 | 旧链净经济效果归零，新链只生效一次 |
| 重组超过窗口 | DEEP_REORG_HALT | 无自动猜祖先，无新增资金释放 |
| 已 finalized hash 被替换 | FINALITY_CONFLICT | 不自动反最终性，保存全部证据 |
| RPC 少返一页日志或范围超限 | 拆分/重试并校验完整性 | indexed 水位不越过不完整块 |
| RPC 返回假日志与一致假 receipt | 证据级校验拒绝/来源隔离 | 不能只凭两个 JSON 相同就入账 |
| 在各个事务提交/签名/广播边界杀进程 | 原 ID 恢复、同槽重播 | 无二次付款、无丢失冻结 |
| 20 个实例同时提交同一/不同提币 | 同业务唯一槽，不同业务唯一 Nonce | 无重号；数据库约束未被绕过 |
| 同槽旧低价交易先上链 | 旧 attempt 成 winner | 不等待“最新 hash”而卡死或再付 |
| base fee 连续上涨，预算封顶 | NEEDS_FEE_APPROVAL，限制队列 | 无越权签高价交易 |
| 取消与原付款竞赛 | 按真实 winner 终结 | 本金不因取消请求提前释放 |
| 主备切换、旧 leader 复活 | fencing 拒绝旧写/签名 | 只有一个调度域可产生有效指令 |
| 账本消费者先收到旧 release 再收到 reorg | epoch 门禁与补偿闭环 | 不产生可消费的无担保余额 |

对重放测试应同时断言“最终余额正确”和“所有凭证正确”：一笔多记与另一笔少记可能总额抵消，不能只看总余额通过。

---

## 36.12 极端边界与工程决策表

| 边界 | 错误直觉 | 正确决策 |
|---|---|---|
| 同高度换块但没有新高度 | 下一块来了再说 | 定时核验 head hash，必要时立即进入分叉调查 |
| 原 txHash 在新分支重新执行 | 重复，直接忽略 | 重新验证执行；旧周期冲正后新周期激活 |
| 同 blockHash 再变 canonical | credit 唯一键存在，永不再入账 | activation_no 增长，净效果按周期收敛 |
| ERC-20 经路由/批量合约入金 | 只接收 tx.to 等于 token | 以真实 emitter 与 receipt 验证，不误拒合法调用路径 |
| RPC 没有 `removed` 或重连漏通知 | 没收到移除就没回滚 | 头链检测负责真相，WS 只作唤醒 |
| 交易已成功却没有预期 token 转账 | status=1 就打款成功 | 资产适配器验证实际交付，差异进隔离 |
| 余额足够本金但不足 Gas | 到时再补 | 预留受控最大 Gas 和并发义务；队首缺 Gas 会阻塞全账户 |
| 旧高价 raw tx 外泄 | 新签低价即可覆盖风险 | 旧交易仍可执行，保留上界预算，调查广播能力泄漏 |
| 签名后用户取消 | 数据库删任务即可 | 原付款仍有效；同槽取消需链上胜出并最终 |
| pending 计数忽高忽低 | 修正本地 next_nonce | 只作为线索；逐槽核验，不跳号不回收 |
| nonce too low + 查不到所有 txHash | 可能付了，先标成功 | UNKNOWN/INCIDENT，查 canonical 历史与签名域 |
| 第 1 个槽签名失败，后续已签 | 把后续 Nonce 减 1 | 签名内容不可改写；恢复队首或授权取消 |
| 出块重组使已包含 Nonce 回退 | 分配器也回退 | 保留所有本地槽；恢复原槽执行与观察 |
| 链停摆但墙钟过了 1 小时 | 时间够久就最终 | 最终性未增加，保持冻结，告警 |
| L2 sequencer 收到交易 | 与 L1 完成清算等价 | 按 L2/L1/桥不同状态分别记账与释放 |
| 收到未知代币、垃圾空投 | 地址是我的所以给用户 | 不支持资产记原始证据/暂收，不生成可用负债 |
| 代理代币升级或被暂停/冻结 | 合约地址不变就安全 | 暂停适配器，重审实现与转账语义 |
| 数据库恢复到旧备份，链仍前进 | 从链上最新 nonce 开始就行 | 恢复签名审计、raw tx、槽与冻结；证据不全不得重新付款 |
| 同私钥在其他链也用 | chainId 足够，其他不用管 | 签名绑定 chainId，Nonce 按网络隔离；运营上避免跨链共用热钥 |
| EIP-7702/智能账户/ERC-4337 | 都是 EVM，套普通 EOA 池 | 分离授权 Nonce、EOA 发送 Nonce、EntryPoint keyed nonce；单独适配与审计 |

普通 EOA 模型还假定发送地址不会被外部授权路径影响 Nonce。启用 EIP-7702 等新交易能力前，应评估授权签名及其执行对账户序列的影响；不能让签名域悄悄开放新消息类型。账户抽象的多路 Nonce 不是“取消唯一所有者”，而是将所有者与唯一键细化到相应 lane/key。

---

## 36.13 术语表、面试答题卡与自检题

### 36.13.1 术语表

| 术语 | 准确定义 |
|---|---|
| Canonical | 在某个经过验证的链视图中被选为主链；不代表永不改变 |
| Reorg | 主链从共同祖先之后切换到另一分支 |
| Orphaned observation | 本次执行所属块不再 canonical；历史证据仍存在 |
| Parent hash | 指向父块身份，配合高度构成连续性检查 |
| Finality | 按具体共识或结算协议达到的最终性，不是统一固定确认数 |
| Safe / Finalized | 语义由网络与客户端定义；在 Ethereum 上属于不同保证等级 |
| Confirmation depth | 某次入块相对当前 canonical 头的深度，需要明确是否含自身 |
| Receipt root | 区块头承诺的 receipts trie 根；验证日志还需可信头与正确编码 |
| Nonce slot | 平台持久化占有的某网络、某发送账户、某序号 |
| Tx attempt | 同槽一次具体签名交易；hash 与其他 attempt 可不同 |
| RBF | 同 Nonce 替换尝试，受节点费用策略影响，不是共识层“撤回旧交易” |
| Fee cap / Tip cap | maxFeePerGas / maxPriorityFeePerGas，上限不同于最终实付 |
| Head-of-line blocking | 队首 Nonce 未消费导致后续交易不能按序执行 |
| Epoch / Fencing | 链视图版本与写者代际；前者隔离旧事实授权，后者阻止旧实例写入 |
| Activation | 一次观测在某个生效周期内对业务/账本产生作用 |
| UNKNOWN | 尚无充分证据判定外部结果，资金义务必须受控保留 |

### 36.13.2 面试答题卡

**Q1：如何实现一个高可靠扫块器？**

先按 hash 保存区块头、receipt 与原始证据；并行采集、有序发布，父 hash 连续，水位只推进完整前缀。检测分叉后通过共同祖先、epoch 屏障、旧链补偿和新链重放恢复。充值还要经过资产白名单、日志语义、最终性和账本幂等，不是一个 `getLogs` 循环。

**Q2：为什么有 txHash 去重还会重复入账？**

一笔交易可产生多笔 Transfer；同交易重入块后日志位置和执行结果可能变化；同块也可能反复 canonical。需分开出现身份、业务槽和 activation 周期，使用原分录绑定的冲正与新周期幂等。

**Q3：`parent_hash` 不匹配一定是 Reorg 吗？**

不一定，也可能是通知跳块、节点落后、乱序、错误网络或不可信数据。先验证头部与来源、拉齐父链并确认可信 fork-choice；不能见一次不匹配就回退账本。

**Q4：怎样防假 USDT 充值？**

绑定网络与官方合约白名单，不看 symbol。严格校验 emitter、ABI、receipt、canonical 块和最终性；保留实现升级闸门及非标准代币适配。若威胁模型包括恶意 RPC，仅比对日志和 receipt 不足，还需受控验证节点或对可信头的 receipt 证明。

**Q5：多实例如何并发发币且不冲突？**

用持久化 nonce_state 行锁与槽唯一约束分配，同一业务键返回原槽，签名与广播通过独立 attempts。链上 pending 计数只能辅助观察，不能当抢号服务。吞吐通过多发送账户分片和有界 in-flight 提高。

**Q6：广播超时怎么办？**

先持久化 raw tx 和本地计算的 txHash，再广播。超时进入 UNKNOWN，查所有尝试与 canonical receipt，必要时重播同字节或同槽 RBF。不能换 Nonce 再付一次。

**Q7：EIP-1559 提价为什么 tip 也要改？**

费用上限与优先费上限都影响 txpool 替换和实际竞争力。按受控客户端策略、所有已签尝试上界、base fee 预算与 tip 目标同时规划；比例不是 EIP-1559 统一常数，越预算先审批。

**Q8：旧交易被“替换”后还会成功吗？**

会。替换只改变某些节点的 mempool，旧交易可能被其他节点或出块者保留。所有同槽 attempts 都要观察，以 canonical winner 为准；取消请求也不能提前释放本金。

**Q9：重组后为什么不直接减用户余额？**

账本是不可变责任记录，可能已有关联交易和出款。应冻结、按原凭证追加冲正，不足部分走应收/损失科目，并通过 epoch 屏障阻止过期释放。直接改余额破坏审计和后续重放。

**Q10：怎样证明恢复真正完成？**

固定 epoch、最终块 hash 与账本水位，核验 canonical 连续、旧分支净效果归零、新分支活跃周期唯一、每槽 winner 可解释、资产负债差额闭合。再重复整个恢复任务，证明没有新增经济影响。

### 36.13.3 自检题

1. 设计一个 3 块重组，包含旧块的两笔充值和一笔已入块提币。逐步列出网络、activation、slot、账本状态与允许的操作。
2. 同一 ERC-20 交易从旧分支 `logIndex=81` 变为新分支 `logIndex=4`，但 receipt 内序号未变，如何避免再次无条件入账？若金额也改变呢？
3. 相同区块先 canonical、后 orphan、再 canonical，为什么永久唯一的 `credit:txHash` 不够？设计 activation 与冲正键。
4. 节点返回的日志、receipt 和 blockHash 相互一致，但节点整体恶意，剩余安全边界是什么？
5. 三个实例同时对同一业务和不同业务调用 Go 分配器，写出可能锁序与提交结果；COMMIT 返回断线如何恢复？
6. 旧 attempt 的费率为 `F=40/P=2`，新 attempt 为 `F=63/P=3`，旧交易先最终确认，槽与业务应如何终结？
7. 取消交易已入块但尚未最终，原付款还可能发生什么？为什么不能马上释放全部本金？
8. 账户 latest nonce 比昨天少 2，next_nonce 是否也减 2？说明重组与重复付款风险。
9. 给定一个失败执行的 ERC-20 提币 receipt，区分 Nonce、本金、Gas、平台费与重试授权如何处理。
10. 账本消费者在重组前收到 release、重组后才执行，怎样保证旧 epoch 不会继续制造可用余额？
11. 模拟 RPC 少返日志但不报错，写出能够发现范围不完整的测试与对应水位断言。
12. 数据库回档后丢了最近 100 个 nonce_slot，但签名器可能签过这些交易，为什么仅从 latest nonce 重新起步不安全？

---

## 36.14 与 36.8 的衔接：从闭环接口走向多链托管运行时

本章 **36.8** 是恢复、对账和状态机的闭环接口，不是“后续再补监控”的附录。更深入的 36.8 扩展专题，以及后续 DeFi 清结算与安全容灾工程，都应复用以下契约：

1. **输入契约**：`network + epoch + checkpoint_hash + evidence_ref + adapter_version`。跨链充值、桥退出、稳定币结算不能只传一枚 txHash。
2. **资金契约**：`operation_id + reservation_ref + effect_key + original_journal_ref`。签名、发送、入块、最终交付与费用是不同事实，不能提前合并成一个 SUCCESS。
3. **恢复契约**：候选视图隔离、共同祖先、过期授权拒绝、逐步补偿、新分支重放与最终资产负债核验；任何新适配器必须实现这些回边。
4. **Nonce 契约**：普通 EOA、智能账户、ERC-4337、EIP-7702 或其他链的序列模型分别适配；允许多 lane，但不允许绕过单 lane 的持久化所有权。
5. **出款契约**：第 35 章归集和 Gas 垫付也必须进入同一个 Nonce 调度域；第 37 章合约调用需要扩展资产效果校验、滑点与权限白名单，不能只验证外层 receipt.status。
6. **安全契约**：第 38 章的签名隔离、KYT、审批、审计和容灾，应消费本章事实与 fencing；恢复后的数据库、签名记录、raw tx 和账本必须共同确定“哪些旧交易仍可执行”。

36.8 扩展的第一条验收标准：给定同一批区块树、签名尝试、业务请求和故障注入，增加新链、新 RPC 或新调度器后，原有资产的有效入账、冲正、实付和费用结果保持一致；不能以吞吐优化为理由弱化最终性或允许第二份付款义务。

---

## 36.15 上线前落地清单

- [ ] 网络身份绑定 chainId、genesis/checkpoint 与环境，RPC 误连能被检测。
- [ ] 保存滑动头窗口与完整证据，canonical 高度唯一、parent 连续，所有水位为完整前缀。
- [ ] 同高换块、跳块、低头乱序、窗口外重组和 finalized 冲突都有明确路径。
- [ ] 资产白名单、receipt/log 验证、原生币/trace 与非标准代币边界已明确。
- [ ] 最终性策略按网络、资产和金额分层，并保存版本与 checkpoint 证据。
- [ ] 出现身份、业务槽、activation 和账本 effect 分离，覆盖重复入块与同块再生效。
- [ ] 重组 epoch 闸门延伸到账本消费授权，旧消息不能越过冻结屏障。
- [ ] Nonce 分配同事务、同槽唯一；所有签名来源受统一调度和 fencing 约束。
- [ ] 签名请求与 raw tx 持久化，广播超时、签名超时、COMMIT 未知都能原身份恢复。
- [ ] RBF 检查两种 fee cap、全部已签上界、预算与队首阻塞；所有 attempts 可被选为 winner。
- [ ] 取消是单独审批的竞赛过程，最终前保留原付款资金义务。
- [ ] 提币终态验证真实资产交付、最终性、实际 Gas 和账本回执，而非只看 receipt.status。
- [ ] 充值与提币在同一最终快照、epoch 和账本水位下完成逐笔及总额对账。
- [ ] 演练覆盖深重组、假 RPC、旧 attempt 胜出、主备切换与数据库回档，重复恢复无新增经济效果。
- [ ] 所有人工修复走幂等状态机与审计，不允许 SQL 改余额、抢改 Nonce 或删除孤块证据。

扫块器的职责不是“尽快看到转账”，而是持续证明哪些链上事实有资格成为平台的资金责任；Nonce 池的职责也不是“尽快发出去”，而是在任何未知与重试下仍能证明每份付款意图只承担一份可解释的交付义务。
