# 38. 金融安全、反洗钱（AML/KYT）、防篡改审计与两地三中心容灾

> **状态**：工程篇｜**所属**：第四部分·核心交易架构｜**编号**：38 / 48
>
> **定位**：把第 22 章账本、第 24 章幂等、第 27 章对账、第 35–36 章托管接入同一条安全证据链；为下一部分 AI-Agent 建立不可绕过的执行边界。
>
> **交付**：安全分区、mTLS、信封加密、四眼审批；AML/KYT、制裁筛查、人工调查与 SAR 保密；HMAC 哈希链与外部 WORM 锚点；同步多数派、fencing、RPO/RTO；PostgreSQL DDL、协议、Go/Java 可复算示例与验证方案。
>
> **红线**：加密不等于授权；命中不等于定罪；SAR 不等于自动冻结指令；哈希链不单独证明完整性；两地三中心不天然等于 RPO=0；没有多数派和隔离证据，宁可停止付款。

## 38.1 教学事故：备用机房活了，旧主也还在付款

### 38.1.1 时间线与损失口径

**以下为合成教学事故，不对应某家机构，不是已披露的真实案例。金额仅用于演算。**

| 时间 | 事件 | 错误动作 | 结果 |
|---|---|---|---|
| 09:00 | 外包运维凭据泄露 | 内网即信任，服务共享管理员证书 | 攻击者可读客户资料并调用审批 API |
| 09:02 | 制裁供应商超时 | 把超时当无命中 | 高风险出款直接进入签名队列 |
| 09:03 | 审批配置被修改 | 发起人可给自己第二次审批 | 四眼复核退化为同一人点两次 |
| 09:05 | A 地与 B 地网络分区 | 仅靠 DNS 切流，把异步备库提升 | B 地丢失最近确认的付款记录 |
| 09:06 | A 地旧主仍有渠道网络 | 旧进程不检查 epoch | A、B 对同一业务分别付款 |
| 09:08 | 审计管理员删掉日志尾部 | 校验剩余 HMAC 链仍连续 | 系统错误报告“审计完整” |

账单：10 笔原本各付 10 万元，合法支付合计 100 万元；备用端重复支付 3 笔，额外流出 30 万元。
若追回 8 万元，则待追偿敞口为 22 万元；另有隐私泄露和合规调查成本，不能混算成同一损失数字。
账本恢复必须记录重复支付、应收追偿和后续回款，不能删掉三笔流水伪装成从未发生。

### 38.1.2 唯一真问题与信任边界

> 当身份、名单服务、数据库和机房都可能出故障时，如何证明每次敏感动作有合法授权、每笔付款只有一份经济效果、每段审计有可核验边界，且无法证明的请求停在受控状态？

威胁模型包括外部攻击者、被盗工作负载身份、越权内部人、串谋审批人、恶意数据库管理员和灾难性机房失效。
不承诺“所有管理员与外部见证者同时串谋仍可自证清白”；必须明确哪些信任根由独立团队和司法辖区管理。
不变量：授权绑定不可变意图；筛查结果可追溯版本；审计连续前缀有外部见证；旧执行者无法越过最后一道资金出口。

## 38.2 分区、加密、审批与反洗钱：四道不同的门

### 38.2.1 安全分区与工作负载身份

```text
公网 → WAF/API 边界区 → 业务区 → 账本区 → 渠道执行区
                         │         │          │
                         └──► 合规区         隔离签名区/HSM
                                  │
                                  ▼
                         审计写入区 → 独立归档账户/WORM
管理平面：PAM + 短时授权 + 双人复核；不允许绕过业务区直改账本
```

| 边界 | 身份与网络策略 | 不能偷换的含义 |
|---|---|---|
| 服务到服务 | TLS 1.3/mTLS、短期证书、显式 trust domain、出口白名单 | 握手通过只证明对端持有某身份，不代表可付款 |
| 业务到合规 | 最小字段、地域路由、租户权限、用途标签 | 拿到案件号不等于可读完整证件 |
| 执行到签名 | 固定交易模板、金额/地址白名单、审批摘要 | 有效服务证书不能任意签字节串 |
| 人到管理面 | 硬件 MFA、JIT/PAM、会话录制、紧急权限限时 | 堡垒机记录不能替代账本业务审计 |

服务身份可用 SPIFFE/SPIRE，Go 使用 `crypto/tls` 验证证书链与 SAN，Java 使用 JSSE/受管工作负载身份组件。
授权按“主体、资源、操作、租户、用途、地域”独立判断；禁止跳过证书验证、只信 CN 或永久共享客户端证书。
证书续期、信任根轮换和撤销传播必须演练；长连接需要限制寿命，不能假设吊销后旧连接自动断开。

### 38.2.2 信封加密不是把密钥和密文放在一起

每个对象或受控批次生成随机 DEK，用 AES-256-GCM 加密；由 KMS/HSM 中的 KEK 包裹 DEK。
持久化 `ciphertext, nonce, tag, wrapped_dek, kek_id, algorithm, aad_version`，不保存明文 DEK。
AAD 绑定 `tenant_id + object_id + field_name + schema_version`，防止把甲客户密文挪到乙客户名下。
同一 DEK 下 nonce 绝不重复；采用密码学随机源并设使用上限，不能用进程内自增计数跨重启复用。
KEK 轮换通常重包裹 DEK，DEK 泄露则需重加密数据；轮换不能撤回已经外泄的明文。
解密受 IAM、用途、地域和速率控制，明文不得进入日志、指标标签、trace baggage 或普通分析仓库。
数据库备份、对象版本、缓存和导出副本同样要覆盖；“删 KMS key”不自动满足所有法定删除或留存义务。
KMS 故障时只可按预审策略使用有期限、隔离内存中的缓存密钥，禁止降级为明文持久化。

### 38.2.3 四眼审批绑定意图，而不是绑定一个可修改的单号

发起者不能复核自己；高风险策略可要求两名不同的授权复核者，并与发起者分离。
本章采用更严格的“发起者 + 两名独立复核者”；不是声称所有四眼制度都要求三个人。
审批摘要绑定 `request_id, tenant, amount_minor, currency, payee, purpose, policy_version, expires_at`。
字段规范化后计算 SHA-256；金额采用最小货币单位整数，摘要编码必须版本化，不能任意拼接字符串。
变更收款地址、金额、策略或名单版本使审批失效；执行前重新检查当前授权、有效期与制裁闸门。
审批凭证由受信审批服务签名并含唯一 nonce，执行器以业务幂等键保证只消费一次；客户端不能自己声明 actor。
紧急通道也需要事后独立复核、范围上限和不可删除审计，不能成为永久超级用户接口。

### 38.2.4 AML/KYT 与制裁筛查是相关但不同的任务

| 能力 | 输入 | 输出与处置边界 |
|---|---|---|
| KYC/CDD/EDD | 身份、受益所有人、资金来源、地域 | 风险分层与尽调，不是一次认证终身有效 |
| 制裁筛查 | 姓名、别名、生日、证件、所有权/控制关系 | 潜在命中进入复核；阻断、冻结或拒绝按适用法律执行 |
| AML 监测 | 多账户流水、交易对手、周期、业务目的 | 可疑模式告警，不直接认定犯罪 |
| KYT | 链、地址、交易图、标签来源、暴露路径 | 链上风险证据，不把“若干跳关联”当司法事实 |

姓名先做受控 Unicode/音译/别名规范化，再结合多字段匹配；保留原文，不能只保存模糊匹配分数。
分数阈值须按姓名语言、地区、名单类型验证；同名、转写差异、数据缺失会改变误报与漏报结构。
名单快照记录来源、发布时间、接收时间、摘要与版本；名单更新触发存量客户及待付款的重筛。
所有权聚合不能只看“单一股东是否超过阈值”；例如 OFAC 50 Percent Rule 涉及被封锁人的合计直接或间接所有权。
控制关系及其他法域规则可能不同，具体阻断范围由法务/合规维护，不能硬编码一个全球通用阈值。
监测拆分交易要跨客户关联、时间窗和渠道聚合；不能教用户“低于某金额即可绕过”。
KYT 标签记录证据时间、来源置信度和链上定位，地址聚类只是推断；混币器接触也不能替代个案法律分析。

状态机：`NEW → SCREENING → CLEAR | REVIEW | HOLD`；超时、版本过期、身份字段缺失进入 `HOLD`，不进入 `CLEAR`。
人工调查：`REVIEW → FALSE_POSITIVE | ESCALATED → DECIDED`；每次转移保存证据、理由、操作者与版本。
误报白名单必须限定主体、名单条目、命中原因、有效期与复核者；不能把同名字符串全局永久放行。
告警关闭率不是唯一指标，还要抽检漏报、名单新鲜度、案件积压、复核一致性和真正升级率。
低风险查询可按预审规则提供只读降级；资金释放是否暂停由业务与法律风险策略明确，不因供应商超时自动放行。

### 38.2.5 SAR：人工责任、禁止泄密与地域边界

SAR/STR 是向适用主管机关提交的可疑活动报告，名称、门槛、期限、保存要求均因法域和机构类别不同。
模型或规则可整理证据和草拟文本，**提交、结论与处置必须由具备授权的合规人员按当地制度复核**。
SAR 是否已提交、草稿及相关材料按当地保密规则隔离；不能把报告状态暴露给客户、普通客服或通用 AI 上下文。
面向客户只返回合规允许的通用状态；不要通过错误码、耗时差异或通知文案提示“已报送 SAR”。
报送成功需保留主管机关回执；网络超时为提交未知态，先按主管机关协议查证，不能盲目重复报送。
金融集团内共享、跨境调查、外包筛查均需逐项核查法律依据、接收主体和数据驻留要求，不假设集团内部天然合法。
地域路由覆盖正文、密钥、备份、日志和灾备站点；脱敏摘要仍可能可关联，不能自动视为匿名数据。
法定报告与客户冻结是两个独立决策；既不能因报送自动无限期扣款，也不能因未报送就放行受制裁财产。

## 38.3 HMAC 哈希链：能检出改写，不能凭空证明没有被删

### 38.3.1 字节协议与可证明范围

按租户/地域/业务分片定义不可复用的 `chain_id`；每条链单写者，在同一数据库事务内分配连续序号。
链起点 `T0 = 32 个零字节`；记录包含 payload 的精确字节摘要，不依赖 JSON 对象字段遍历顺序。

```text
D_i = SHA256(payload_bytes)
M_i = ASCII("AUDITv1\0") || BE32(len(chain_utf8)) || chain_utf8
      || BE64(seq) || BE32(key_version) || T_(i-1) || D_i
T_i = HMAC-SHA256(K[key_version], M_i)
```

协议限定 `seq ∈ [1, 2^63-1]`、`key_version ∈ [1, 2^31-1]`、UTF-8 chain 长度 1..1024 字节；前序标签及摘要各 32 字节。
payload 在落链前按明确规范编码；原始字节或可恢复的加密对象必须保留，仅有摘要无法解释业务内容。
轮换密钥时新记录仍引用旧尾标签；验证者保留历史密钥的受控访问，不能重置序号假装新历史。
HMAC 依赖共享秘密，验证者持有密钥时也能伪造；它不是第三方不可否认的数字签名。

| 攻击 | 仅验证当前数据库链 | 还需要什么 |
|---|---|---|
| 修改中间 payload、无密钥 | 标签不匹配 | 受控取证与告警 |
| 删除中间行 | 连续序号/前序标签不匹配 | 期望区间，防止查询漏行 |
| 删除尾部或整条链 | 剩余前缀仍合法，甚至查不到链 | 独立 chain 清单与期望尾序号 |
| 回滚到旧快照 | 旧链全部合法 | 外部单调 checkpoint，拒绝锚点倒退 |
| 攻击者拿到 HMAC 密钥并重写 | 可以重新计算全部标签 | 已发布的外部锚点及权限隔离 |
| 业务从未写审计 | 链无法感知未发生的输入 | 业务事实/Outbox/渠道回执反向对账 |

### 38.3.2 外部锚点、WORM 与证据交接

锚点封装 `chain_id, first_seq, last_seq, tail_tag, key_versions, schema, created_at, previous_anchor_digest`。
由独立见证服务签名并写入独立管理账户的 WORM/合规锁定对象，保存版本 ID、留存策略和外部接收回执。
签名公钥/证书、链清单、签名算法和轮换关系需另行可信发布；只把公钥放在同一被攻陷数据库没有意义。
按期导出加密原始记录及 manifest；WORM 不能证明导入前已经完整，也不能防止所有输入在生成前被篡改。
若每 60 秒锚定一次，未锚定尾部最长约 60 秒加上传延迟处于较弱保护，不能声称实时全量不可篡改。
保护级别更高的操作可要求执行前获得独立接收回执，但这会增加延迟，并在见证服务故障时降低可用性。
归档权限与业务写权限分离，合规锁定的删除/保留行为按平台实际模式验证，不把“开了版本控制”说成 WORM。
审计告警停发也要报警：监测独立时钟下的锚点年龄、序号增长、空链异常与导出遗漏。

### 38.3.3 手算：什么叫“一个合法的短前缀”

设已锚定 `(chain=A, last_seq=120, tail=T120)`，库中本有 1..123。
攻击者删除 121..123，链校验仍通过且锚点 120 也匹配；证明的只是 1..120，不包括未见证的三个事件。
若外部已另锚定到 123，删除后期望数量为 123、实际只有 120，立即发现缺尾 3 条。
若连整条链 A 都被删除，仅遍历数据库现存链无法发现；外部 chain 清单必须仍要求 A 出现。
这些结论不依赖手算 HMAC 的每个字节，下一节双栈程序用相同编码验证标签与缺尾判定。

## 38.4 两地三中心：多数派保证什么，停机又换来了什么

### 38.4.1 拓扑、故障域与同步承诺

示例：A 地 A1、A2 两个独立中心，B 地 B1 一个中心；三个**持久化数据投票副本**，写入以 2/3 多数派提交。
下表讨论具备正确日志匹配、选主和持久化语义的共识系统；不能把三个普通 PostgreSQL 实例直接当作 Raft。
RPO=0 定义为：在声明的故障模型内，所有已经对客户确认成功的提交，在合法恢复后都仍存在。
ACK 必须晚于要求副本的持久化提交；异步复制、只收进内存、无数据见证票都不能自动支撑这个承诺。

| 故障/分区 | 存活票 | 安全动作 | 可用性与数据说明 |
|---|---|---|---|
| A1 单中心失败 | A2+B1=2 | 依法选主并 fencing | 可恢复写入，跨地延迟进入路径 |
| B1 失败 | A1+A2=2 | A 地保持多数派 | 本地已确认数据可保留，但异地保护降级 |
| A 地整体失败 | B1=1 | 停写，不擅自降为单节点集群 | 不能保证 B1 持有所有已确认提交 |
| A/B 地互断 | A 地=2，B 地=1 | 仅 A 地可形成多数派 | B 地不能为了可用性自行接管 |

关键反例：某笔记录只持久化在 A1+A2 就对客 ACK，随后 A 地全部毁损，B1 尚未收到。
因此，“两地三中心 + 多数派”不等于“任意一地毁损仍可零丢失并立即续写”。
若承诺任一地毁损数据 RPO=0，ACK 还必须要求**跨地持久化**，并证明选主/恢复采用包含已确认提交的副本。
这相当于额外的故障域提交约束，不是普通 2/3 ACK；B 地失联时即使 A 地有多数派也必须停止这类新确认。
若还要求任一地失效后自动继续写，需重新设计独立故障域、数据副本与法定人数，通常考虑第三地域及区域分布。
异步灾备可以提供有损恢复选项，但必须事先标注 RPO>0，并由业务、法务、财务批准，不可悄悄修改承诺。

### 38.4.2 Fencing 必须到达最后一个副作用

`fence_epoch` 从共识控制面单调递增，恢复后不能从旧备份回退；租约超时和机器墙钟不单独证明旧主已死。
每次写入在同一事务中锁住 authority 行，比较 token 与当前 epoch；epoch 提升使用同一行锁串行化。
数据库栅栏挡不住已排入渠道的旧请求；渠道执行器、签名器和资金出口也要检查有效 epoch 与业务幂等键。
外部渠道不支持 epoch 时，以唯一受控出口代理、凭据撤销、网络隔离/STONITH 形成物理或权限栅栏。
撤销有传播窗口；缺乏旧出口隔离证据时不得新开付款出口。已经发送的请求进入 UNKNOWN，由渠道查证收敛。
幂等键须跨机房稳定，不能把新 epoch 拼入经济幂等键，否则切换后同笔付款变成“新请求”。

### 38.4.3 RTO 不是“进程几秒启动”

RTO 从服务中断计时，到资金写服务恢复约定能力为止；包含检测、隔离、选主、日志恢复、对账和重新开放。
示例顺序预算：检测 15 秒 + 隔离 20 秒 + 选主/恢复 25 秒 + 关键对账 40 秒 + 灰度 20 秒 = 120 秒。
这只是教学目标预算，不是实测结果；实际并行阶段取关键路径，并报告 p95/p99 与失败样本，不能直接相加冒充测量。
故障切换与回切都走 `DETECT → FREEZE → FENCE → PROMOTE → RECONCILE → CANARY → OPEN`。
快照校验、历史密钥、名单版本、审计锚点与渠道凭据都必须可恢复；只有数据库起来不等于金融业务可恢复。

## 38.5 PostgreSQL DDL 与执行协议

以下为独立示例 schema，覆盖审批、防并发、审计和外部锚点元数据；不能把 SQL 约束当完整授权引擎。
生产应单设 owner/migrator/writer/reader，应用无表所有权；敏感 payload 放地域内加密对象，表内只留引用。

```sql
CREATE TABLE authority (
  scope text PRIMARY KEY,
  epoch bigint NOT NULL CHECK (epoch > 0)
);
CREATE TABLE intent (
  request_id uuid PRIMARY KEY,
  scope text NOT NULL REFERENCES authority(scope),
  intent_hash bytea NOT NULL CHECK (octet_length(intent_hash)=32),
  maker text NOT NULL,
  policy_version text NOT NULL,
  screening_version text NOT NULL,
  expires_at timestamptz NOT NULL,
  state text NOT NULL CHECK (state IN ('REVIEW','APPROVED','EXECUTING','DONE','HOLD')),
  UNIQUE (request_id, intent_hash)
);
CREATE TABLE approval (
  request_id uuid NOT NULL,
  intent_hash bytea NOT NULL,
  reviewer text NOT NULL,
  decision text NOT NULL CHECK (decision IN ('APPROVE','REJECT')),
  decided_at timestamptz NOT NULL,
  PRIMARY KEY (request_id, reviewer),
  FOREIGN KEY (request_id, intent_hash) REFERENCES intent(request_id, intent_hash)
);
CREATE TABLE audit_head (
  chain_id text PRIMARY KEY,
  seq bigint NOT NULL CHECK (seq >= 0),
  tag bytea NOT NULL CHECK (octet_length(tag)=32)
);
CREATE TABLE audit_event (
  chain_id text NOT NULL REFERENCES audit_head(chain_id),
  seq bigint NOT NULL CHECK (seq > 0),
  event_id uuid NOT NULL UNIQUE,
  request_id uuid REFERENCES intent(request_id),
  epoch bigint NOT NULL CHECK (epoch > 0),
  key_version integer NOT NULL CHECK (key_version > 0),
  prev_tag bytea NOT NULL CHECK (octet_length(prev_tag)=32),
  payload_hash bytea NOT NULL CHECK (octet_length(payload_hash)=32),
  payload_ref text NOT NULL,
  tag bytea NOT NULL CHECK (octet_length(tag)=32),
  PRIMARY KEY (chain_id, seq)
);
CREATE TABLE execution_outbox (
  request_id uuid PRIMARY KEY,
  intent_hash bytea NOT NULL CHECK (octet_length(intent_hash)=32),
  epoch bigint NOT NULL CHECK (epoch > 0),
  payload_ref text NOT NULL,
  state text NOT NULL CHECK (state IN ('READY','UNKNOWN','DONE')),
  FOREIGN KEY (request_id, intent_hash) REFERENCES intent(request_id, intent_hash)
);
CREATE TABLE anchor_receipt (
  chain_id text NOT NULL,
  last_seq bigint NOT NULL CHECK (last_seq > 0),
  tail_tag bytea NOT NULL CHECK (octet_length(tail_tag)=32),
  manifest_digest bytea NOT NULL CHECK (octet_length(manifest_digest)=32),
  worm_version text NOT NULL,
  witness_receipt text NOT NULL,
  received_at timestamptz NOT NULL,
  PRIMARY KEY (chain_id, last_seq)
);
```

`anchor_receipt` 只是外部证据的本地索引，不是锚点本体；删除这张表不能删除 WORM 与见证服务的记录。
`reviewer != maker`、两个复核者、角色分离、名单新鲜度涉及跨行与外部事实，不能靠上述 CHECK 自动保证。
审批服务从认证上下文取得 reviewer；执行服务在事务内锁 intent，验证两名独立批准且没有生效拒绝。
追加审计时锁 `audit_head FOR UPDATE`，取 `seq+1`、计算 HMAC、插 event、更新 head；事务回滚不得留下可见空洞。
业务状态、审计事件和 execution_outbox 在同一事务提交；HMAC 服务失败就回滚，不先付款再补日志。
跨库场景需要源库 Outbox 与独立接收回执协议；不得声称跨数据库天然原子。

```text
ExecuteIntent v1 = {request_id, intent_hash, scope, fence_epoch, approval_receipt_ids}
认证主体不从报文自报；金额/收款人从不可变意图取，不接受覆盖字段。
BEGIN → lock authority → compare epoch → lock intent → 校验审批/有效期/重筛
      → 写业务状态 + audit_event + execution_outbox → COMMIT
出口领取 Outbox → 核验当前 epoch/意图 → 用稳定 request_id 调渠道
已知成功：DONE；超时：UNKNOWN → 主动查单/回执；禁止生成新经济幂等键重付。
```

所有会获取两类锁的路径固定 `authority → intent → audit_head` 顺序，避免审批与执行交叉死锁。
“写入 Outbox 时 epoch 正确”不代表几分钟后仍可执行，出口必须再次检查；隔离完成前已发送请求仍需对账。

## 38.6 Go + Java：同一 HMAC 协议、篡改与缺尾断言

以下是可独立运行的**核心算法教学示例**，不是完整审计服务；密钥固定只为复算，生产必须由 KMS/HSM 管理。
选型：Go 标准库 `crypto/hmac`、`crypto/sha256`、`encoding/binary`；Java 21 JCA `Mac`、`MessageDigest`、`ByteBuffer`。
两端输入相同：chain=`tenant-cn/audit`、key=32 个 `0x0b`、版本=1、序号=1/2、payload=`{"event":"approve"}`。
签名帧明确使用大端，Java `ByteBuffer` 默认大端；比较使用 `hmac.Equal` / `MessageDigest.isEqual`，不转十六进制再比较。

### 38.6.1 Go 1.24+，保存为 `main.go`

```go
package main

import (
    "bytes"
    "crypto/hmac"
    "crypto/sha256"
    "encoding/binary"
    "encoding/hex"
    "fmt"
)

func tag(key []byte, chain string, seq uint64, ver uint32, prev, payload []byte) []byte {
    c := []byte(chain)
    if len(key) != 32 || len(c) == 0 || len(c) > 1024 ||
        seq == 0 || seq > (1<<63)-1 || ver == 0 || ver > (1<<31)-1 || len(prev) != 32 {
        panic("invalid audit frame")
    }
    d := sha256.Sum256(payload)
    b := bytes.NewBuffer(nil)
    b.WriteString("AUDITv1\x00")
    mustWrite := func(v any) {
        if err := binary.Write(b, binary.BigEndian, v); err != nil { panic(err) }
    }
    mustWrite(uint32(len(c)))
    b.Write(c)
    mustWrite(seq)
    mustWrite(ver)
    b.Write(prev)
    b.Write(d[:])
    m := hmac.New(sha256.New, key)
    m.Write(b.Bytes())
    return m.Sum(nil)
}

func anchored(rows [][]byte, expectedSeq int, expectedTag []byte) bool {
    return expectedSeq > 0 && len(rows) == expectedSeq &&
        hmac.Equal(rows[len(rows)-1], expectedTag)
}

func main() {
    key := bytes.Repeat([]byte{0x0b}, 32)
    p := []byte(`{"event":"approve"}`)
    zero := make([]byte, 32)
    t1 := tag(key, "tenant-cn/audit", 1, 1, zero, p)
    t2 := tag(key, "tenant-cn/audit", 2, 1, t1, p)
    if hmac.Equal(t1, tag(key, "tenant-cn/audit", 1, 1, zero, []byte("tampered"))) {
        panic("tamper missed")
    }
    if !anchored([][]byte{t1, t2}, 2, t2) || anchored([][]byte{t1}, 2, t2) {
        panic("anchor check failed")
    }
    fmt.Println(hex.EncodeToString(t1))
    fmt.Println(hex.EncodeToString(t2))
}
```

### 38.6.2 Java 21+，保存为 `AuditDemo.java`

```java
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.HexFormat;
import java.util.List;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

public class AuditDemo {
    static byte[] tag(byte[] key, String chain, long seq, int ver,
                      byte[] prev, byte[] payload) throws Exception {
        byte[] c = chain.getBytes(StandardCharsets.UTF_8);
        if (key.length != 32 || c.length == 0 || c.length > 1024 ||
            seq <= 0 || ver <= 0 || prev.length != 32) {
            throw new IllegalArgumentException("invalid audit frame");
        }
        byte[] domain = "AUDITv1\0".getBytes(StandardCharsets.US_ASCII);
        byte[] digest = MessageDigest.getInstance("SHA-256").digest(payload);
        ByteBuffer b = ByteBuffer.allocate(domain.length + 4 + c.length + 8 + 4 + 32 + 32);
        b.put(domain).putInt(c.length).put(c).putLong(seq).putInt(ver).put(prev).put(digest);
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(key, "HmacSHA256"));
        return mac.doFinal(b.array());
    }

    static boolean anchored(List<byte[]> rows, int expectedSeq, byte[] expectedTag) {
        return expectedSeq > 0 && rows.size() == expectedSeq &&
            MessageDigest.isEqual(rows.get(rows.size() - 1), expectedTag);
    }

    public static void main(String[] args) throws Exception {
        byte[] key = new byte[32];
        Arrays.fill(key, (byte) 0x0b);
        byte[] p = "{\"event\":\"approve\"}".getBytes(StandardCharsets.UTF_8);
        byte[] zero = new byte[32];
        byte[] t1 = tag(key, "tenant-cn/audit", 1, 1, zero, p);
        byte[] t2 = tag(key, "tenant-cn/audit", 2, 1, t1, p);
        byte[] bad = "tampered".getBytes(StandardCharsets.UTF_8);
        if (MessageDigest.isEqual(t1, tag(key, "tenant-cn/audit", 1, 1, zero, bad))) {
            throw new IllegalStateException("tamper missed");
        }
        if (!anchored(List.of(t1, t2), 2, t2) || anchored(List.of(t1), 2, t2)) {
            throw new IllegalStateException("anchor check failed");
        }
        System.out.println(HexFormat.of().formatHex(t1));
        System.out.println(HexFormat.of().formatHex(t2));
    }
}
```

运行 `go run main.go` 与 `javac AuditDemo.java && java AuditDemo`，应各输出相同的两行标签；须独立核对输出，不能只看退出码。

```text
022ea5959ade9d50e971f98e9204eaadfbcdc1be62af5a062af0c2013651aa64
411e28479dbf77d2bf62c310ff073fd29286993b85636f2a368da32ebdb0ecd2
```

`anchored` 假设调用前已逐条验证序号、前序标签和 payload；它仅示范外部期望长度/尾标签校验，不是完整链验证器。
示例未实现可信锚点获取、公钥验证、KMS、事务、并发锁与故障切换；不能据此宣称完整性或生产容灾已验证。
编写时实际运行环境为 Go 1.19.8、OpenJDK/javac 17.0.21，双栈断言及独立 Python HMAC 复算一致；目标 Go 1.24+/Java 21+ 仍需另行验证。
本地未执行 PostgreSQL DDL、真实 mTLS/KMS、WORM 留存、跨地断电与压力测试；以上集成及故障项均为待执行方案。

## 38.7 对账、故障演练与压测：先写判据，再按证据验收

### 38.7.1 三层对账与最小测试集

| 层次 | 对拍对象 | 不变量与差异处置 |
|---|---|---|
| 业务/授权 | 已执行意图 ↔ 审批摘要 ↔ 名单快照 | 两名合法复核者、版本有效、无越权；缺证据暂停相关出口 |
| 资金 | Outbox ↔ 渠道回执 ↔ 复式分录 | 稳定 request_id 只形成一次经济支付，UNKNOWN 不得按失败重付 |
| 审计/归档 | 业务事实 ↔ event ↔ chain 清单 ↔ WORM manifest | 无漏写、连续序号、摘要匹配、锚点单调；保全现场后调查 |

单元测试：跨语言黄金向量、修改一个字节、交换顺序、重复序号、删中间、删尾、删整链、回滚快照、密钥轮换。
权限测试：自审、同一人换角色重复审、审批后改收款人、到期审批、撤权后执行、跨租户密文互换与 AAD 失败。
合规测试：同名误报、音译、缺生日、名单更新后重筛、供应商超时、KYT 标签撤回、SAR 状态不出现在客户日志。
集成测试必须验证真实 PostgreSQL 的事务回滚、唯一键竞争、锁顺序和 Outbox 重放；算法断言不能替代数据库验证。

### 38.7.2 故障矩阵与停止条件

| 注入点 | 观察证据 | 必须满足的判据 |
|---|---|---|
| COMMIT 后 ACK 丢失 | 客户重试、同 request_id 查询 | 成功提交仍可查，至多一次经济效果 |
| A/B 网络互断 | 多数派日志、出口请求轨迹 | 单一合法执行域；少数派不付款 |
| A 地整体断电 | B 地日志位置、外部确认清单 | 按已声明 ACK 策略报告缺失，不强行单票提升 |
| 旧主暂停后恢复 | epoch、签名器与渠道代理拒绝日志 | 旧 token 无法产生新付款，已在途请求进入查证 |
| KMS/名单/锚点服务不可用 | HOLD 数量、锚点年龄、错误预算 | 不静默放宽法律、安全或 RPO 承诺 |
| 还原旧备份/删除审计尾部 | 独立 chain 清单与最新外部锚点 | 发现倒退、缺尾；不把校验短前缀当成功 |

每次演练记录开始/结束时间、故障域、确认提交集合、恢复集合、重复支付数和 RTO 分阶段时间。
RPO 验证比较故障前所有**已确认成功 ID**与恢复后的持久化集合，不能只比两边最大流水号。
若出现双主付款、确认记录丢失、审计锚点倒退或 SAR 泄露，立即停止灰度；保留证据，不自动清库重跑掩盖问题。
回切前先同步、重新 fencing、处理 UNKNOWN 和补齐对账；“主机恢复供电”不等于有资格接管。

### 38.7.3 压测预算、故障负载与诚实报告

教学容量：每秒 2,000 事件、每事件归档均摊 1 KiB，则每天约 164.8 GiB 原始数据，不含副本、索引与 WORM 版本。
按 60 秒一批锚定，每批约 120,000 事件；审计链按租户/地域分片避免单条 head 行锁成为全站串行瓶颈。
跨地 RTT 若为 25 ms，同步跨地确认延迟下界受网络与 fsync 约束；不能同时承诺所有写入 p99 小于 1 ms。
方案：按正常峰值、2 倍峰值、十倍名单命中、单中心失效四种负载压测，并保持真实 TLS、KMS 策略和归档链路。
采集吞吐、p50/p95/p99、锁等待、fsync、跨地 RTT、筛查积压、HOLD/UNKNOWN、水位、锚点年龄与恢复追赶时间。
指标标签禁止出现姓名、地址、证件或 SAR 案件内容；异常样本进入合规访问控制的证据库。
验收阈值由业务 SLO 与监管要求确定；**本节为测试设计，未据此声称实际压力、断电或跨地域测试已通过**。

## 38.8 一页速记、面试卡、自检答案与后续衔接

### 38.8.1 术语与十分钟面试卡

| 术语 | 正确定义 | 高频误区 |
|---|---|---|
| mTLS / DEK / KEK | 双向身份验证 / 数据密钥 / 包裹密钥 | 有证书就有业务授权，加密即可不做权限 |
| AML / KYT / SAR | 反洗钱体系 / 交易风险分析 / 可疑活动报告 | 风险分数就是犯罪结论，模型可直接报送 |
| HMAC / WORM / anchor | 共享密钥认证码 / 保留期内受控不可改写存储 / 外部见证检查点 | 数据库内自存尾标签就是独立证据 |
| RPO / RTO / fencing | 可容忍数据丢失 / 恢复时间目标 / 拒绝旧执行者 | 机房数量等于零丢失，切流等于隔离 |

十分钟结构：1 分钟定义资产、攻击者与故障模型；2 分钟画安全分区和资金出口；2 分钟讲审批/筛查/人工职责。
再用 2 分钟解释哈希链截断反例与 WORM 锚点；2 分钟说明多数派、跨地 ACK 和旧主 fencing；最后 1 分钟给出对账与演练判据。
必须主动声明法域、同步承诺与未验证项；说清哪些故障下停写，优于空喊“金融级高可用”。

### 38.8.2 三道自检与答案

1. **最新外部锚点为 120，数据库合法前缀也是 120，能证明从未存在 121 吗？**
   不能；只能证明锚定范围与现存前缀相符。需对拍业务输入、Outbox 或独立接收证据；未锚定尾部是明确的保护窗口。
2. **A1+A2 确认付款，B1 落后；A 地毁损后直接提升 B1 能否承诺 RPO=0？**
   不能。普通多数派没有保证跨地持久化；应停写并取证恢复，或事先采用跨地 ACK 及相应选主约束，接受分区时可用性下降。
3. **同名制裁命中被人工判为误报，随后名单更新且审批尚未执行，该怎么办？**
   复核误报例外的范围与有效期，按新版本重筛；不得复用全局白名单。执行前重新校验审批与授权；疑难案件由合规人员决策并保护报告信息。

### 38.8.3 衔接：Agent 只读证据、提出意图，不拥有安全根

下一部分第 39 章将建立认知、护栏、执行三平面：Agent 可以提出交易或调查建议，但不能持有 HMAC/KEK 根密钥、修改名单策略、代替独立复核或提升灾备主节点。
第 42 章 AML-Agent 只能在获准地域读取最小案件证据，草稿标注引用、时间与不确定性，提交与处置仍经过人工制度。
继续使用 [24 幂等](24-end-to-end-financial-idempotency.md)、[25 状态机与 Outbox](25-state-machines-tcc-saga-and-outbox.md)、[27 对账](27-multi-tier-reconciliation-and-auto-healing.md) 和 [35 托管签名隔离](35-crypto-custody-hd-wallet-mpc-and-sweep.md) 作为执行底座；模型不能绕过这些确定性约束。

### 38.8.4 来源与适用边界

- [NIST SP 800-207：Zero Trust Architecture](https://csrc.nist.gov/pubs/sp/800/207/final)：身份、资源与持续授权边界。
- [RFC 8446：TLS 1.3](https://www.rfc-editor.org/rfc/rfc8446)、[SPIFFE 概念](https://spiffe.io/docs/latest/spiffe-about/overview/)：传输身份不代替业务授权。
- [Google Cloud：信封加密](https://cloud.google.com/kms/docs/envelope-encryption)、[NIST SP 800-38D](https://csrc.nist.gov/pubs/sp/800/38/d/final)：DEK/KEK 与 GCM 使用约束。
- [FATF Recommendations](https://www.fatf-gafi.org/en/publications/Fatfrecommendations/Fatf-recommendations.html)：CDD、可疑交易报告、保密与禁止 tipping-off，具体义务以当地实施规则为准。
- [OFAC 50 Percent Rule FAQ](https://ofac.treasury.gov/faqs/topic/1521)、[FinCEN SAR 保密说明](https://www.fincen.gov/resources/advisories/fincen-advisory-fin-2012-a002)：美国制度示例，不可直接外推全球。
- [RFC 2104：HMAC](https://www.rfc-editor.org/rfc/rfc2104)、[AWS S3 Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html)：认证码性质与 WORM/保留模式。
- [Raft 原论文](https://raft.github.io/raft.pdf)、[etcd 灾难恢复](https://etcd.io/docs/v3.6/op-guide/recovery/)、[PostgreSQL 同步复制](https://www.postgresql.org/docs/current/warm-standby.html#SYNCHRONOUS-REPLICATION)：法定人数、恢复与同步提交的不同适用边界。
- [NIST SP 800-34 Rev.1](https://csrc.nist.gov/pubs/sp/800/34/r1/final)：恢复目标、业务影响分析与应急演练。
