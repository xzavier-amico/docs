# 47. 密钥与凭据：Vault / KMS / OIDC 与轮转

> **状态**：`已完成（初稿）` ｜ **所属**：第九部分 · 横切基础件 ｜ **编号**：47 / 53
> **本篇定位**：全仓唯一一处把「凭据」当成**有寿命的租约**而不是「一段配置字符串」来讲的篇目。ZK/etcd 的 session 与 lease 语义、watch 的一次性归 [37 ZooKeeper vs etcd](37-zookeeper-vs-etcd.md)；「租约被误判后旧主人还在写」的锁侧形态归 [46 分布式锁与选主](46-distributed-lock-and-leader-election.md)；凭据轮转期间把连接池与超时链打爆的账归 [44 连接池与客户端](44-connection-pool-and-client.md)；K8s Secret 怎么落盘、ConfigMap 怎么滚更归 → comp/38。本篇只回答一个问题：**Agent 系统最贵的泄露不是数据是凭据——必须一次一租、可吊销、有 TTL；而 seal/unseal 与 lease 的物理形态，决定故障那天你会不会全站等锁。**
> **读法**：救火先读 §47.9 的踩坑清单与 §47.10 的指标拐点；被追问「我们 Vault 为什么一重启全站挂」直接读 §47.3 的 sealed 状态机与 §47.7 账一；要落地轮转读 §47.6 的三类差异表与 §47.8 的成本表；算容量先读 §47.7 三本账（续租 QPS、KMS 并发与美元、轮转爆炸半径是三个不同的乘号）；只背结论翻 §47.11。

---

## 47.1 引子：三起「凭据系统自己成了单点」的事故

凭据系统有一个别处少见的性质：**它平时不出现在任何一条业务链路图里，出事时同时是所有链路的前置条件。** 数据丢了可以回滚，凭据取了不该取的、或者干脆取不到，系统会以「503 / 401 / 连不上库」的形状整体失效——而且失效曲线**不是阶跃的，是随 TTL 倒计时逐段扩大的**。下面三起事故分别长在三条线上：**seal 与 quorum**（事故一）、**静态长寿命密钥**（事故二）、**token 同步过期**（事故三）。

### 事故一：三个 Vault 节点一起 sealed，48 分钟后全站才回来（2026-02-03）

某 Agent 平台自建 Vault 三节点 Raft 集成存储（手动 Shamir unseal，`-key-shares=5 -key-threshold=3`），所有业务凭据（MySQL 主库/只读账号、对象存储 STS、三家 SaaS 的 API key）全部是 **1 小时 TTL 的动态租约**，应用侧只在进程内存里缓存到过期为止，没有本地文件缓存、没有 Vault Agent。

| 时刻 | 事件 | 关键数字 |
| --- | --- | --- |
| 03:14:02 | 机房 ATS 切换，一排不在 UPS 覆盖内的机柜断电，Vault-1/2/3 与 2 台 etcd 同时掉电 | 3/3 节点 `Sealed=true` |
| 03:14:40 | 应用侧第一批 `503 service unavailable`（来自 Vault 的 `sys/health` 探针） | 此时业务错误率仍是 **0%**——所有 Pod 的缓存凭据还没到期 |
| 03:16 | 值班确认 `vault status` 显示 `Sealed true`、`Storage Type raft`，需要 3 份 unseal key | 3 位 key holder 分布在两城，其中 1 位在休假，第 5 份确实没人持有 |
| 03:20–03:36 | 缓存凭据按各自剩余寿命陆续到期：短 TTL 的那批（5 分钟缓存的 SaaS key）先死 | 12 分钟时 1,100 / 4,000 Pod 开始 5xx |
| 03:36:11 | 3 位 key holder 到齐，`vault operator unseal` ×3 完成，节点转 active | sealed 时长 **22 分 9 秒** |
| 03:36:12 | 4,000 Pod 同时来要凭据：每个 Pod 3 个凭据，无 single-flight、无抖动 | 12,000 次签发请求在一秒内到达 |
| 03:36:12–03:36:40 | 每一次「动态数据库凭据签发」= Vault 侧 1 次 Raft 写 + **目标库一条 `CREATE USER`**；MySQL 侧 `Threads_running` 从 30 冲到 900，`Too many connections` | 签发 P99 4.2 s，随后 Vault 侧 `503 standby` |
| 03:37–03:52 | 客户端 3 次重试（无退避）⇒ 36,000 次请求，把刚恢复的 Vault 又打回不可用 | 全站错误率 **42%**，最长队列 60 s 超时 |
| 03:52 | 紧急加「客户端令牌桶 50/s + single-flight + 分批放行」 | 队列 9 分钟排空 |
| 04:02 | 业务恢复 | 总影响 **48 分钟**、失败订单 21 万笔、赔付与补偿 ≈ 130 万元 |

**这起事故的关键不是「断电」，而是那个 22 分钟之后才开始的 16 分钟。** 断电只让 `Sealed=true`，真正把全站打死的是两件事的乘积：**① TTL 决定失效是渐进的**（seal 那一刻起，每个凭据都在自己的寿命上倒计时，事故在 12 分钟里从 0% 长到 100%）；**② 恢复瞬间是齐射**（4,000 个进程在同一秒要同一个东西，而「签发动态凭据」在物理上是**对目标库做 DDL**，不是读一个缓存）。把 unseal key 放到更多人口袋里只缩短了前 22 分钟，完全没有缩短后 16 分钟——后者需要的是客户端侧的退避、single-flight 与本地缓存。

> **「quorum」在这里有两层含义，别混**：Raft 的 quorum（3 节点要 2 票）决定存储能不能读写；Shamir 的 threshold（5 份份额要 3 份）决定**谁能把 barrier 打开**。第一层是机器，第二层是**人**。凭据系统的可用性 = `min(基础设施可用性, 有钥匙的人同时在场的概率)`。

### 事故二：一套 2019 年的 AK 用到了 2026 年，泄露 11 天后才发现（2026-01-27）

某团队的备份链路用一对静态 AWS access key（IAM user，2019-11 创建），权限是 `s3:*` on `*`，凭据硬编码在 `deploy/values.yaml`，后来该仓库被镜像到公开的 GitLab 实例；同一对密钥也被写进了 CI 容器镜像层（`~/.aws/credentials`），因为某次 `aws s3 cp --debug` 把它打进了构建日志。

| 时刻 | 事件 | 关键数字 |
| --- | --- | --- |
| 2019-11 | 创建 IAM user + AK，此后**从未轮转**（没有轮转流程，也没有人知道它存在） | 静态凭据年龄 **5 年 2 个月** |
| 2025-12 下旬 | 镜像仓库外泄，攻击者从镜像层与 CI 日志里各拿到同一对 AK | 泄露点 **2 个** |
| 2026-01-16 | 攻击者 `ListBuckets` → `ListObjectsV2` → 批量 `GetObject`，并新建 3 个 AK 做持久化 | 下载 **14 TB** |
| 2026-01-16 ~ 01-27 | 无告警：S3 server access logging 未开、CloudTrail **data events 未开**、无 `GetObject` 量异常看板 | MTTD **11 天** |
| 2026-01-27 | 财务发现当月出网流量费异常（14 TB × 约 $0.09/GB ≈ $1,260）才触发排查 | 靠账单发现 |
| 事后 | 14 TB 内含 2.4 亿条会话记录与租户文件；取证 6 人 × 3 周 + 通知 3.2 万客户 + 监管问询 + 3 家客户解约 | ≈ 480 万元 |

**三条根因都是「凭据属性」的缺失，不是「加密不够强」**：① **没有 TTL** ⇒ 泄露窗口 = 凭据寿命 = 43,800 小时（5 年），而不是「攻击者用了多久」；② **不可吊销** ⇒ 事后虽然能删 AK，但攻击者已建的 3 个新 AK 还得单独找出来（这也是为什么工作负载身份只能换取「短寿命」，不能防止「已签发者再来要一次」——那要靠审计）；③ **不可审计** ⇒ MTTD 11 天，这 11 天里旧凭据一直是有效的。三者相乘才是 480 万元：`暴露窗口 × 每单位时间可导出数据量 × 检出延迟`。

### 事故三：8,000 个 Pod 的 OIDC token 在同一分钟过期，续租风暴把 STS 与业务一起打死（2026-03-11）

某平台用 IRSA 做工作负载身份：Pod 里的投影 ServiceAccount token（`expirationSeconds=3600`）换 `sts:AssumeRoleWithWebIdentity` 拿 1 小时 STS 凭据，SDK 在 TTL 走到 80% 时刷新。

| 时刻 | 事件 | 关键数字 |
| --- | --- | --- |
| 10:40–11:00 | 大促扩容：同一批 Deployment 一次性扩容 8,000 Pod，kubelet 把投影 token 写进容器 | 8,000 个 token 的 `exp` 落在同一 **60 秒**窗口内 |
| 11:48（= 创建 + 80% TTL） | 所有 SDK 同时刷新：8,000 次 `AssumeRoleWithWebIdentity` | 到达 **8,000 次/s**（常态 2.2 次/s，放大 3,600×） |
| 11:48:03 | STS 账户级配额被打满，返回 `Throttling`；SDK 的 `MaxRetries` 被设成 10 且**不遵守 `Retry-After`** | 到达量 ×10 ⇒ **80,000 次/s** |
| 11:48:05 | 同时 8,000 个进程各自去拉 JWKS（每进程 JWKS 缓存 TTL 60 s），K8s API server 的 OIDC discovery 被打 | `/.well-known/...` QPS 从 1 涨到 6,000 |
| 11:48:20 | token 刷新全部失败 ⇒ 各服务调 S3/DynamoDB 收到 `ExpiredToken`，代码把它当「可重试的 5xx」做指数退避 | 业务 5xx 从 0.2% 涨到 63% |
| 12:25 | 手工摘流量 + 分批重启 + 临时把刷新窗口改成「TTL 的 50%–80% 随机」 | 抖动摊到 18 分钟 ⇒ 峰值 7.4 次/s |
| 事后 | 37 分钟，SLA 违约 + 大促 GMV 损失 | ≈ 96 万元 |

**这起事故里没有任何一个 credential 泄露，全都是「有效凭据被同时换掉」。** 它的形状与 [37 ZooKeeper vs etcd](37-zookeeper-vs-etcd.md) §37.9 账二的 watch 风暴是同一个：**成本 = 变更次数 × 订阅者数**，只不过这里的「变更」是**过期**，而过期时刻由「创建时刻 + TTL」决定——**批量创建必然导致批量过期**。任何「所有实例同一时刻启动、TTL 完全相同、在 TTL 的固定比例处刷新」的组合，都会产生一个理论峰值 `N ÷ (刷新窗口宽度)`；抖动（jitter）不是优化，是正确性的一部分。

> 🩸 **三起事故的共同形状**：事故一是**控制面（seal）失效后，数据面靠 TTL 的倒计时逐段死亡，且恢复瞬间齐射**；事故二是**凭据没有寿命也没有审计，于是「泄露窗口」等于「凭据年龄」**；事故三是**寿命太整齐，于是每 1 小时全站同时去换一次命**。三件事的根是同一句话：**凭据不是一个值，是一条有起点、有寿命、有吊销通道、有审计记录的租约；它的风险不写在加密算法里，写在「寿命 × 使用方数量 ÷ 缓存与抖动」这三个数上。**

---

## 47.2 定位与唯一真问题

**定位**：Vault / KMS / OIDC 三件东西看起来是「三种产品」，物理上它们是**同一条链路上的三段**：**谁能打开保险箱（seal）→ 密钥在哪生成与包装（KMS）→ 谁有资格来要（工作负载身份）→ 要到的这份东西活多久（lease / TTL）→ 过期与吊销怎么生效（revoke / rotation）**。任何一段缺失，其余四段都会退化成「一段长期共享的静态字符串」——而那时候你的安全性就等于**这段字符串的年龄**。

### 凭据的四个属性（缺一个，另外三个都白给）

| 属性 | 物理落点 | 它回答的问题 | 缺了会怎样（真实形状） | 观测它 |
| --- | --- | --- | --- | --- |
| **机密性** | barrier 加密（Vault）/ KEK 在 HSM 内不出（KMS）/ mTLS 与 TLS 通道 | 「拿到盘、拿到日志、拿到镜像，能不能读出凭据」 | 镜像层与 CI 日志里躺着 `.aws/credentials`（事故二） | 存储后端是否明文、镜像扫描命中数 |
| **可吊销** | 显式 revoke 通道（`vault lease revoke` / IAM `DeleteAccessKey` / 证书 CRL、OCSP 或短寿命） | 「确认泄露后，多久能让它失效」 | 只能等自然过期；若寿命 5 年，那就是 5 年 | 吊销到生效的延迟（秒～小时） |
| **短 TTL** | lease / STS / 投影 token / 证书 notAfter | 「它自己会死吗，多久」 | 失效窗口 = 凭据年龄（事故二 43,800 小时） | `credential_age_seconds` 分布 |
| **可审计** | Vault 审计设备（file/syslog/socket）+ 云侧 data event 日志 | 「谁在什么时候、从哪个身份，取了什么」 | MTTD 11 天，靠账单才发现（事故二） | 审计写入失败数、`GetObject` 异常速率 |

> **顺序很重要**：评审上最常见的一句话是「我们加密了」，但**加密只解决第 1 条**。事故二的盘是加密的、TLS 是开的、密钥强度也够——它死在「另外三条全是 0」上。**可吊销 + 短 TTL 才是真正把「泄露」从「灾难」降级成「事故」的两个旋钮，而它们与加密强度无关。**

### 唯一真问题

> **Agent 系统最贵的泄露不是数据是凭据：必须一次一租、可吊销、有 TTL；seal/unseal 与 lease 决定故障时会不会全站等锁。**

这句话拆成五条推论，全篇都在给它们找证据：

1. **凭据泄露的定价与数据泄露不同：数据泄露赔一次，凭据泄露是「持续的输出管道」。** 数据泄露的量有上界（那批数据），凭据泄露的量是 `单位时间导出能力 × 有效时长`；所以「缩短有效时长」比「加密更用力」的收益高一到两个量级（§47.7 账二的对照）。
2. **TTL 是一个双刃旋钮，拧的是同一根轴**：`泄露窗口 = TTL`，而 `故障容忍 = TTL − (检测 + 修复时间)`。TTL 越短越安全、越短的 TTL 越要求你的 MTTR 更短。**这就是「一次一租」不是免费的原因**，也是为什么大多数团队最后落在一个区间（§47.6 表、§47.8 成本表）。
3. **seal 是凭据系统自己的单点，且 quorum 里有人**：手工 unseal 的 Vault，其可用性上限等于「3 个 key holder 同时在线的概率」；auto-unseal 把这个人因素换成 KMS，但**引入了一条新的、更深的依赖链**（Vault 依赖 KMS，业务依赖 Vault）——循环依赖不会消失，只会换位置（§47.3）。
4. **lease 把「安全性」换成了「流量」，而流量有三个乘号**：续租 QPS = `凭据数 ÷ TTL`；KMS 并发 = `解密 QPS × 延迟`；轮转爆炸半径 = `使用方数 × 重叠窗口`。这三个乘号分别打在 Vault、KMS、你的发布系统上，**任何一个都可以成为下一个单点**（§47.7）。
5. **「同时过期」是批量部署的必然产物，不是偶发**：`exp = 创建时刻 + TTL`，而创建时刻由发布决定。所以「抖动 + single-flight + 本地缓存」是**凭据客户端的必备件**，与「服务发现怎么重试」同级，不是可选优化（§47.5）。

**它明确不承诺什么**：不承诺「存进 Vault 就安全」（Vault 也是软件，也会 sealed、也会被打死）；不承诺「短 TTL 就没有泄露」（短 TTL 只压缩窗口，不阻止泄露那一刻）；不承诺「KMS 加密就是端到端」（KMS 只管 KEK，你的进程里有明文 DEK）；不承诺「轮转可以无人值守」（大多数轮转的真正工作量在**使用方的重启与灰度**上）；不承诺「吊销立刻生效」（见 §47.6 的「旧密钥还能用多久」表：缓存、连接池、长连接、CRL 的失效延迟各有量级）。

### 三个卖给你的东西，与它们真实交付的东西

| 广告词 | 真实交付的 | 缺口由谁补 |
| --- | --- | --- |
| 「Vault 集中管理所有密钥」 | 一个**加了 barrier 的 KV + 一堆 secrets engine + 一个租约管理器** | 应用侧的缓存、退避、single-flight、TTL 预算（事故一的后 16 分钟） |
| 「Vault 高可用（3 节点 Raft）」 | 存储层的多数派可用；**但每个节点都要 unseal 才能服务** | unseal 流程（人或 KMS）、以及「sealed 期间业务靠什么活着」 |
| 「KMS 托管密钥，永不出 HSM」 | KEK 不出；**但它是一个有配额、有延迟、按次计费的网络服务** | DEK 本地缓存与信封加密（§47.4、账二） |
| 「OIDC 工作负载身份免密钥」 | 用「短期 token 换短期凭据」替代静态密钥 | token 的刷新抖动、JWKS 缓存、`aud` 校验（§47.5） |
| 「动态凭据一次一租最安全」 | 每次签发在目标库做一次 `CREATE USER` + 一次 Raft 写 | 签发与回收的 QPS 预算、目标库的 DDL 承受力（事故一 03:36） |
| 「自动轮转」 | 密钥材料换新；**使用方是否切换是另一件事** | 双密钥重叠窗口、灰度、回滚、漏改检测（§47.6） |

### 解剖卡八问落点（本篇答案）

| 问 | 落在哪节 | 一句话答案 |
| --- | --- | --- |
| ① 承诺什么 | §47.2、§47.8 | 承诺「凭据有寿命、可吊销、有审计」；不承诺「Vault 永不 sealed」「轮转零风险」 |
| ② 数据长什么样 | §47.3、§47.4 | Vault：storage backend 里的一堆 **barrier 密文**（含 keyring、lease 条目、token 条目）；KMS：HSM 里一个**不可导出的 KEK + 版本号**；落盘的总是 `{key_id, version, wrapped_dek, nonce, ciphertext}` |
| ③ 一次读写走哪条路 | §47.3、§47.7 | 取凭据 = 1 次 TLS RTT + 1 次认证 + 1 次 barrier 解密 + 1 次 Raft 写（+ 动态凭据还要 1 次对目标库的 DDL）+ 1 次审计写 |
| ④ 并发与一致 | §47.3、§47.6 | Raft 保证「lease 只被一个人持有」；**barrier 的开启靠 Shamir quorum 或 KMS**；轮转靠「双密钥重叠」而不是锁 |
| ⑤ 扩展维度 | §47.7、§47.8 | **分片（按业务域拆 Vault/密钥）+ 缓存（DEK/凭据本地缓存）+ 延长 TTL 换取流量**；Vault 单集群写能力不可横向扩 |
| ⑥ 系统级失效 | §47.9、→ comp/52 | sealed（假活/不可用）、续租风暴（满）、轮转半途而废（语义漂移）、KMS 限流（慢）、凭据泄露（正确性以外的一类） |
| ⑦ 看哪几个指标 | §47.10 | `unsealed`、`expire.num_leases`、续租失败数、KMS 限流计数、轮转失败/凭据年龄、token 刷新错误率 |
| ⑧ 换掉它的信号 | §47.8 | 续租 QPS 逼近 Vault 写能力、凭据数 × 1 次签发 DDL 打爆目标库、KMS 调用超配额且缓存不可缓存、或「我们有 600 个使用方却没人能保证它们都改配置」 |

---

## 47.3 Vault 的物理形态：seal / barrier / storage / lease

Vault 只有四个部件，但它们的依赖顺序决定了全部故障形状：**barrier 要用 root key 打开 → root key 来自 unseal 流程 → 打不开 barrier 就读不到 storage 里的任何东西（包括 lease 表）→ 于是所有凭据签发、续租、吊销同时停摆。** 理解这一条，「重启后为什么全站等」就不需要再解释了。

```text
图 1 · Vault 进程里的四层，以及 sealed 时到底是哪一层没了

  客户端 ──HTTPS──▶ ┌──────────────────────────────────────────────────────┐
                    │ ① API 层：认证方法（token / k8s / jwt / cert / aws）    │
                    │    路由（/v1/database/creds/app）+ 审计设备（每请求一条）│
                    │    认证通过 ⇒ 拿到一个 token（带 policy 列表 + TTL）    │
                    ├──────────────────────────────────────────────────────┤
                    │ ② barrier：AES-256-GCM 加解密层（所有落盘都要过它）     │
                    │    keyring（多代加密钥，term 递增） ← root key          │
                    │    root key ← Shamir(3/5 份额)  或  KMS 解封（auto）    │
                    │    ⚠ sealed 时拿不到 root key ⇒ ②③④ 全部不可用，       │
                    │       只剩 sys/seal-status、sys/unseal、sys/init 可答   │
                    ├──────────────────────────────────────────────────────┤
                    │ ③ 逻辑层：secrets engine（kv / database / pki / transit │
                    │    / aws / jwt）+ expiration manager（lease 堆 + 定时器）│
                    ├──────────────────────────────────────────────────────┤
                    │ ④ storage backend：Raft(integrated) / Consul / S3 / …   │
                    │    盘上全是 barrier 密文：拿到磁盘 ≠ 拿到凭据            │
                    └──────────────────────────────────────────────────────┘
```

### sealed / unsealed：一个只有两个状态、但状态切换代价极高的机器

| 状态 | Vault 能响应什么 | 客户端看到什么 | 恢复条件 | 期间业务靠什么 |
| --- | --- | --- | --- | --- |
| **sealed**（初始、重启后、`operator seal` 后） | 只有 `sys/seal-status`、`sys/unseal`、`sys/init`；其余一律 **503** | 签发/续租全失败；**已签发的凭据仍按自己的 TTL 继续有效** | Shamir：凑够 `key-threshold` 份份额；auto-unseal：KMS 解封成功 | 已有的本地缓存 / 静态凭据 / 什么都靠不上 |
| **unsealed / standby** | 可转发请求，Raft 参与投票 | `503 standby`（客户端需重定向到 active） | 无需额外操作 | 正常 |
| **unsealed / active** | 全部 API | 正常 | — | 正常 |

三个必须记住的物理事实：

1. **每个节点都要 unseal**。3 节点集群不是「unseal 一次全好」——每个节点的 barrier 是独立打开的（因为 root key 在每个节点的内存里各自存在）。运维脚本必须遍历全部节点，而**遍历期间 active 可能漂移**，这就是「unseal 完前两个、第三个却成了新 leader」的经典踩坑（§47.9 第 3 条）。
2. **sealed 不等于「已经出事」**。它是**倒计时开始**：所有已签发凭据仍然有效，直到 TTL 到期。所以 seal 的实际影响面 = `随时间增长的过期凭据比例`（事故一：12 分钟 27%，60 分钟 100%）。
3. **auto-unseal 把「人」换成「KMS」，把单点往下挪了一层**。KMS 挂了 / KMS 权限被改坏 / 跨区网络断，Vault 就起不来；而且此时 Vault 无法帮你诊断，因为它自己没起来。**依赖链变成 `业务 → Vault → KMS → (云 IAM/网络)`，故障时一定要能从最里层往外查。**（开 `seal "awskms"` 一类 auto-unseal 后，`init` 产出的是 **recovery keys**，用于 `generate-root`/rekey，**不再用于 unseal**；别把两套钥匙的用途搞混，以目标版本文档为准。）

### barrier 与 storage backend：加密发生在「写盘之前」，所以备份是无害的

| 你担心的事 | 物理上是否成立 | 依据 |
| --- | --- | --- |
| 「备份被拖走就完了」 | 基本不成立 | storage 里的每个条目都是 barrier 密文；没有 root key，密文与随机串无异 |
| 「拿到 root key 就能读历史密文」 | 成立 | root key 解 keyring，keyring 里保留旧代密钥（为读旧条目）；所以 **root key 泄露 = 全部历史泄露** |
| 「`operator rotate` 之后历史都换了新密钥」 | **不成立**（要看清语义） | 轮换让**新写**用新代密钥，旧条目按访问/写入逐步重新包装（rewrap）；「轮换」≠「全量重加密完成」 |
| 「Vault 挂了数据就丢了」 | 取决于 backend | integrated storage（Raft）是自带持久化的；Consul/S3 类 backend 的数据在外部，Vault 只是加解密层 |

### lease：Vault 真正的「一等公民」

动态 secrets engine 签发凭据时不返回「一个值」，而是返回**一个值 + 一个 lease_id + 一个 TTL + 一个 renew 通道**。expiration manager 是 Vault 内部的一个「租约堆 + 定时器」，它在启动时要**从 storage 里恢复全部 lease**——这条事实推出了两个运维后果：

| lease 的物理事实 | 直接后果 |
| --- | --- |
| lease 条目存在 storage 里（barrier 密文） | `vault.expire.num_leases` 就是「你的凭据数」，它是**重启恢复时间**的主要项 |
| 每次续租 = 1 次「更新 lease 条目」= 1 次写 | 续租不是读操作，它消耗 Raft 写能力（§47.7 账一） |
| 到期/吊销时，Vault 执行该 engine 的 revocation 语句 | 数据库 engine 的回收是 **`DROP USER`**：一次凭据回收 = 一次对目标库的 DDL |
| lease 可以被 `-prefix` 批量吊销 | 「按 prefix 吊销」是事故响应里的第一招（比逐个找 lease_id 快两个量级） |

### 五种 secrets engine 各自在物理上做了什么

| engine | 签发时物理上做了什么 | 到期时物理上做了什么 | 它带来的独有风险 |
| --- | --- | --- | --- |
| **kv**（v1/v2） | 读一条 barrier 密文（v2 还要读 metadata 拿版本号） | 什么都不做（**没有租约**） | 静态凭据，寿命 = 你多久才想起换（事故二） |
| **database** | 对目标库执行 `CREATE USER` + `GRANT`（role 里配的 `creation_statements`） | 执行 `DROP USER`（`revocation_statements`） | 签发/回收都是 **DDL**：目标库的 DDL 承受力成为上限（事故一 03:36） |
| **aws / gcp / azure** | 调云 API 生成 STS / 服务账号密钥 | 调云 API 吊销（STS 靠自然过期） | 又多一层云 API 的配额与延迟 |
| **pki** | 用 CA 私钥签一张证书（私钥不出 Vault） | 无动作（靠 notAfter）；吊销靠 CRL | CRL 分发与信任链是另一套工程 |
| **transit** | **不签发凭据**，只做加解密/签名（`encrypt`/`decrypt`/`datakey`） | 不适用 | 它是「KMS 的自建版」：一次 `decrypt` = 一次网络 + 一次 barrier 读 |

```bash
# Vault 关键操作骨架（Vault 1.15+ 量级，CLI 形态；版本差异以目标版本文档为准）
vault operator init -key-shares=5 -key-threshold=3            # 只做一次！输出 5 份 unseal key + root token
vault status                                                   # Sealed / HA Mode / Storage Type 三个字段最先看

# unseal：每个节点都要各做一遍，凑够 threshold 份
for n in vault-1 vault-2 vault-3; do
  VAULT_ADDR=https://$n:8200 vault operator unseal "$KEY1"
  VAULT_ADDR=https://$n:8200 vault operator unseal "$KEY2"
  VAULT_ADDR=https://$n:8200 vault operator unseal "$KEY3"   # 第 3 份到位即 unsealed
done

vault secrets enable -path=database database
vault write database/config/app \
  plugin_name=mysql-database-plugin connection_url='{{username}}:{{password}}@tcp(db:3306)/' \
  allowed_roles='app-rw' username="$VAULT_DB_USER" password="$VAULT_DB_PASS"
# default_ttl/max_ttl 就是账一里的 TTL 与硬上限
vault write database/roles/app-rw db_name=app \
  creation_statements="CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}'; GRANT SELECT,INSERT,UPDATE ON app.* TO '{{name}}'@'%';" \
  revocation_statements="DROP USER IF EXISTS '{{name}}'@'%';" \
  default_ttl=1h max_ttl=24h

vault read database/creds/app-rw                              # 拿到 lease_id + lease_duration
vault lease renew database/creds/app-rw/<lease_id>            # 续租（一次写）
vault lease revoke -prefix database/creds/app-rw              # 事故响应：按前缀批量吊销
vault operator rotate                                          # barrier keyring 换代（≠ 全量重加密）
vault audit enable file file_path=/var/log/vault/audit.log     # 审计设备：每请求一条，注意磁盘
```

### 动态凭据的客户端骨架：缓存 + 提前续租 + single-flight

```go
// Go 1.21+，vault/api。三条纪律：本地缓存、抖动提早续租、single-flight 防齐射
type CredSource struct {
	cli *api.Client
	mu  sync.Mutex
	cur *Cred // {User, Pass, LeaseID, TTL, ExpiresAt}
	sf  singleflight.Group
}

func (s *CredSource) Get(ctx context.Context) (*Cred, error) {
	s.mu.Lock()
	c := s.cur
	s.mu.Unlock()
	// 在 TTL 的 50%~70% 之间续租：抖动窗口把"齐射"摊成均匀流（事故三）
	if c != nil && time.Now().Before(c.ExpiresAt.Add(-jitter(c.TTL, 0.3, 0.5))) {
		return c, nil
	}
	v, err, _ := s.sf.Do("renew", func() (any, error) { return s.reissue(ctx) })
	if err != nil {
		// 关键：未过期就先返回旧凭据，不能"重试到成功为止"，否则恢复瞬间齐射（事故一）
		if c != nil && time.Now().Before(c.ExpiresAt) {
			return c, nil
		}
		return nil, err
	}
	return v.(*Cred), nil
}

func (s *CredSource) reissue(ctx context.Context) (*Cred, error) {
	sec, err := s.cli.Logical().WriteWithContext(ctx, "database/creds/app-rw", nil)
	if err != nil {
		return nil, err
	}
	ttl := time.Duration(sec.LeaseDuration) * time.Second
	c := &Cred{User: sec.Data["username"].(string), Pass: sec.Data["password"].(string),
		LeaseID: sec.LeaseID, TTL: ttl, ExpiresAt: time.Now().Add(ttl)}
	s.mu.Lock()
	s.cur = c // 旧 lease 交给 Vault 自然过期：立刻 revoke 会在目标库多一次 DROP USER
	s.mu.Unlock()
	return c, nil
}
```

> **两个反直觉的纪律**：① **旧凭据不要立刻 revoke**，让它自然到期——立刻 revoke 会在目标库上产生一次 `DROP USER`，而轮转/续租本来就是 DDL 密集场景，收尾动作容易把库打抖；② **续租失败时优先返回未过期的旧凭据**，而不是直接报错——「过期」才是硬边界，「续租失败」只是软告警，把它当硬错误会把一次 Vault 抖动放大成全站不可用（事故一 03:37–03:52）。代码里的 `jitter(ttl, lo, hi)` 返回 `[lo, hi] × ttl` 上的随机量；没有它，4,000 个进程会在同一秒续租。

---

## 47.4 KMS 与信封加密：KEK / DEK、版本与轮转

KMS 常被当成「一个加密函数」，这是最贵的误解。它是一个**有配额、有延迟、按次计费、跨网络、受 IAM 策略与审计约束的远端服务**；它能做的事只有一件：**用一把永不出 HSM 的密钥去包装/解包一小段数据**。真正加密你数据的，是本地生成、只活在进程内存里的 DEK。

```text
图 2 · 信封加密：KEK 永不出 HSM，DEK 一对象一份，落盘的是"包好的钥匙"

  写路径
   ① GenerateDataKey(KEK, AES-256) ──网络 1 跳──▶ KMS / HSM
         ◀── PlaintextDEK（只在内存，用完即弃）
         ◀── WrappedDEK = Encrypt(KEK, DEK)   （KEK 版本号写在密文头里）
   ② AES-256-GCM(PlaintextDEK, 数据) ── 纯 CPU，零网络 ──▶ Ciphertext
   ③ 落盘：{ key_id, key_version, WrappedDEK, nonce, tag, Ciphertext }

  读路径
   WrappedDEK ──Decrypt(KEK)──▶ KMS（这一次就是账二里的"1 次 KMS 调用"）
        └─▶ PlaintextDEK ──▶ AES-GCM 解数据（本地，微秒级）
```

### KMS 与本地加密函数的三处硬差异

| 维度 | 本地 `AES-256-GCM(密钥, 数据)` | KMS 的 `Encrypt/Decrypt` |
| --- | --- | --- |
| 延迟 | 微秒级（GB/s 吞吐） | **单跳 5–30 ms 量级**（含 TLS、鉴权、HSM 操作；跨区更高） |
| 单次能做多大 | 任意大（流式） | **对称密钥单次只包几 KB 级的小数据**（AWS KMS 对称 `Encrypt` 明文上限 4 KB；要大就信封加密） |
| 失败模式 | 无 | **配额 + 限流**（`ThrottlingException`）、IAM 权限变更、区域故障 |
| 计费 | 0 | **按次计费**（每万次请求一个价，量级以当期价目为准） |
| 审计 | 无 | 每次调用可入日志（这既是优点也是成本） |

> 一句话：**KMS 的正确用法是「加密钥匙的钥匙」，不是「加密数据的加密器」。** 任何「每个对象每次读写都直接调 KMS」的设计，都会同时撞上延迟、配额和账单三堵墙（账二）。

### 三种密钥粒度：隔离强度与调用次数的对赌

| 粒度 | KMS 调用次数 | 隔离强度 | 轮转成本 | 适用 |
| --- | --- | --- | --- | --- |
| **全局一把 KEK** | 只随 DEK 缓存失效而调用 | 最弱：一次 KEK 泄露 = 全库 | 最低（换一把 KEK，旧版仍可解） | 单一租户的内部系统 |
| **每租户一把 KEK** | `活跃租户数 ÷ DEK 缓存 TTL`（账二第 7 步） | 中：租户间不可互解，可按租户销毁（crypto-shredding） | 中：轮转可分批、可灰度 | **多租户 Agent 平台的标准答案** |
| **每对象一个 DEK**（每次写都 `GenerateDataKey`） | `对象写入数`（每写 1 次调用 1 次） | 强：单对象独立密钥 | 低（DEK 本来就用完即弃） | 大对象、低频写（备份、模型权重） |

### key version 与轮转：三个必须分清的动作

| 动作 | 物理含义 | 旧数据还能读吗 | 你需要做什么 |
| --- | --- | --- | --- |
| **换 KEK 版本**（如 AWS KMS 的自动轮转，周期可配、默认按年） | 后端生成新的 backing key，成为新版本；**旧版本保留** | 能（密文头里带版本号，KMS 自动选对版本） | 什么都不用做；但**「轮换完成」不代表旧数据已用新版本重包** |
| **重包装（rewrap / re-encrypt）** | 把 `WrappedDEK` 用新版本重新包一遍（数据本身不动） | 能 | 要自己写批处理作业，且**它是账二里的一次 KMS 调用洪峰** |
| **换 KEK（新 key_id）** | 换一把新密钥，旧 key 进入「仅解密」状态 | 能（只要旧 key 还在） | 必须做**读旧写新**的双写过渡，且旧 key **不能立刻删**（否则历史密文永久不可解） |

> **版本与开关前提**：AWS KMS 对称密钥的自动轮转默认按年（可配周期），且**旧版本长期保留以便解密**；GCP/Azure 的轮转语义与保留策略不同；自建 Vault `transit` 的 `auto_rotate_period` 在较新版本才提供（以目标版本文档为准）。**唯一不能想当然的一条**：任何「轮转」都不会自动把已存密文重写成新密钥，那是另一个作业，且它有自己的 QPS 与账单。

### Vault transit：把 KMS 搬进你自己的机房

`transit` 是「自建 KMS」：密钥材料不出 Vault，只给 `encrypt`/`decrypt`/`sign`/`datakey`。取舍是一张干净的对照——**信任根**从「云厂商 HSM + IAM」换成「你自己的 barrier + unseal quorum」；**延迟**从 5–30 ms 降到同区 1–5 ms（但要过一次 barrier 读 + Raft）；**故障域**从「云区域」变成「与业务 Vault 同生共死」。

> **最常见的架构错误**：把 `transit` 和 `database` 放在**同一个** Vault 集群上，然后指望它高可用。它们共享同一个 barrier 和同一次 unseal——**seal 一次，签发和加解密同时停**。要么按故障域拆集群，要么明确接受同一个单点。

---

## 47.5 OIDC 与工作负载身份：JWT、到期与续租风暴

OIDC 工作负载身份把「一份长期密钥」换成「一份很短的令牌换一份较短的凭据」。它消灭了「密钥躺在镜像里」这一类事故，但**把风险从「存储」搬到了「时间」**：现在你的可用性取决于「所有进程能不能在各自到期前完成一次刷新」，而刷新是**外部服务的网络调用**。

### JWT 的物理形态，与六条不许省的校验

JWT 是 `base64url(header) + "." + base64url(payload) + "." + base64url(signature)`。header 里 `alg`/`kid` 决定怎么验签；payload 里 `iss`（谁发的）、`aud`（发给谁的）、`sub`（发给谁的身份）、`exp`/`iat`/`nbf`（寿命）。验签用的公钥从 `issuer + /.well-known/openid-configuration` → `jwks_uri` 取，按 `kid` 索引。

| 校验 | 不做的后果 | 生产事故形状 |
| --- | --- | --- |
| **`aud` 必须精确匹配自己** | A 服务的 token 能直接用来冒充访问 B 服务 | 横向越权，且日志里一切正常 |
| **`iss` 与 discovery 的 issuer 逐字符相等** | 用「长得像」的伪造 issuer 骗过校验 | 尾斜杠差异是最常见的配置坑 |
| **`exp` / `nbf` 必检，容差 ≤ 60 s** | 过期 token 永生效 | 事故三的另一面：一旦放过就再也不会失败 |
| **按 `kid` 取 key，未知 `kid` 时限速刷新 JWKS** | 攻击者用随机 `kid` 打爆 IdP 的 JWKS | 一次「JWKS 风暴」打死 API server（事故三 11:48:05） |
| **拒绝 `alg: none`，拒绝对称/非对称混用** | 算法混淆攻击 | 用公钥当 HMAC 密钥验签成功 |
| **`sub` 的命名空间/服务账号要绑死** | 任何同名 SA 都能拿到同一角色 | K8s 侧 `system:serviceaccount:<ns>:<name>` 必须写进 `bound_subject`/`bound_claims` |

> **一个极易错的实现细节**：K8s 投影出来的 token 文件是被 **原地替换（symlink 交换）** 的，不是「同一个文件内容被改写」。任何「启动时读一次、缓存在全局变量里」的实现，都会在 TTL 到期后拿着**已失效的 token 字符串**去换凭据，而文件里其实早就换了新的。**要么每次读文件，要么用官方 SDK 的 token source。**

### 静态密钥 vs 工作负载身份 vs mTLS

| 维度 | 静态 AK / 密码 | OIDC 工作负载身份（IRSA / Vault JWT / SPIFFE-JWT） | mTLS（SPIFFE X.509） |
| --- | --- | --- | --- |
| 凭据寿命 | 月～年（常常是「永久」） | 15 分钟–12 小时（K8s 投影 token 默认 3600 s，最小 600 s） | 小时级（SPIRE 默认 1 小时量级） |
| 泄露窗口 | **= 凭据年龄**（事故二：43,800 h） | = 剩余 TTL（≤ 1 h） | = 剩余 TTL |
| 吊销 | 手工删（还要找到所有副本） | 让 TTL 自然过（无需吊销）；或删 role binding | 短 TTL 自然过期；CRL 是补充 |
| 谁能拿到 | 任何能看到镜像/配置/日志的人 | 只有「能通过该 issuer 验签的运行中工作负载」 | 只有持有私钥且能完成握手的进程 |
| 审计粒度 | 无（同一把 key 所有人共用） | 每次 `AssumeRole*` 一条记录，含 `sub` | 每次握手可记录（含 SPIFFE ID） |
| 主要失效模式 | 泄露后长期有效（事故二） | **同步过期 + 刷新风暴**（事故三） | 信任链/时间不同步、证书轮换窗口 |

### 为什么「同时过期」是批量部署的数学必然

`exp = 创建时刻 + TTL`，`创建时刻 = 发布/扩容时刻`。所以「同一批拉起 8,000 个 Pod」直接等于「8,000 个 `exp` 落在同一个窗口」。刷新策略把这个窗口放大或摊平：

| 刷新策略 | 峰值表达式 | 事故三的参数（N=8,000，TTL=3,600 s） |
| --- | --- | --- |
| 固定比例刷新（TTL 的 80%，无抖动） | `N ÷ 刷新判定窗口宽度`（窗口 ≈ 定时器粒度，秒级） | 8,000 次/s（**放大 3,600×**） |
| 刷新加 0–10% 抖动 | `N ÷ (0.1 × TTL)` | 8,000 ÷ 360 s ≈ **22 次/s** |
| 刷新在 TTL 的 50%–80% 区间随机（本文推荐） | `N ÷ (0.3 × TTL)` | 8,000 ÷ 1,080 s ≈ **7.4 次/s** |
| 再叠加进程级 single-flight + 本地 token 文件缓存 | 与上面同量级，但**同机多进程只发 1 次** | 若 8,000 Pod 分布在 800 台节点且节点级缓存 → **0.74 次/s** |

> **「抖动窗口宽度 ≥ 发布批次跨度」是这条设计的不变式**：如果你的扩容在 20 分钟内完成，而抖动窗口只有 10 分钟，那么抖动之后仍然会叠出一段尖峰——**因为刷新时刻的分布由 `创建时刻分布 + 抖动窗口` 卷积决定**，抖动只能抹平比它更窄的尖峰。

```java
// Java 17。OIDC/STS 凭据刷新：抖动 + 单飞 + 遵守 Retry-After + 每次重读 token 文件
final class StsRefresher {
    private final ScheduledExecutorService ses;      // 独立于业务线程池
    private final Duration ttl;                       // 从响应里取，别假设 3600
    private final AtomicReference<Cred> cur = new AtomicReference<>();
    private final Object lock = new Object();         // 单飞：同进程只发一个刷新
    private final Path tokenFile = Path.of("/var/run/secrets/tokens/oidc/token");

    void start() {
        long delayMs = ThreadLocalRandom.current().nextLong(ttl.toMillis() / 2, ttl.toMillis() * 80 / 100);
        ses.schedule(this::refresh, delayMs, TimeUnit.MILLISECONDS);   // 首次即抖动
    }

    void refresh() {
        synchronized (lock) {
            if (cur.get() != null && !cur.get().needsEarlyRefresh()) return; // 别人已刷过
            try {
                String jwt = Files.readString(tokenFile);   // 每次重读：kubelet 原地换文件
                Cred c = sts.assumeRoleWithWebIdentity(jwt); // 一次外部调用
                cur.set(c);
                ses.schedule(this::refresh, jitteredNextDelay(c.ttl()), TimeUnit.MILLISECONDS);
            } catch (TooManyRequestsException e) {
                // 必须遵守服务端给的等待时间，否则重试会变成放大器（事故三 11:48:03）
                ses.schedule(this::refresh, backoffWithJitter(e.retryAfter()), TimeUnit.MILLISECONDS);
            } catch (Exception e) {
                // 关键：仍有未过期凭据时，失败只降级不致命
                ses.schedule(this::refresh, backoffWithJitter(Duration.ofSeconds(30)), TimeUnit.MILLISECONDS);
            }
        }
    }
}
// 三条纪律：① 刷新时间必须抖动，且抖动窗口 ≥ 发布批次跨度；
//           ② 并发调用必须单飞（同进程），跨进程用 sidecar/Agent 合并；
//           ③ 429/Throttling 必须遵守 Retry-After，且重试次数有上限（默认 3）。
```

---

## 47.6 轮转：双密钥重叠窗口、灰度与回滚

「轮转」这个词被用得太笼统，实际包含三件独立的事：**① 生成新材料；② 让新旧同时有效；③ 让所有使用方切过去并撤掉旧的。** 绝大多数轮转事故发生在 ②③，而自动化工具（Vault 的 `rotate`、KMS 的自动轮转、IAM 的「新建 AK」）只做了 ①。

```text
图 3 · 双密钥重叠窗口：一条只靠"时间"换安全的曲线

  时间 ──────────────────────────────────────────────────────────────────▶
  旧凭据可用   ████████████████████████████████████▓▓▓▓▓▓▓▓▓░░░░░（▓ 重叠期 ░ 已宣布废弃但仍有效）
  新凭据可用                          ████████████████████████████████████████
  使用方切换            ── 灰度 10% ──▶ 50% ──▶ 100%（每批一个发布周期 PP）
                                             ▲                        ▲
                                        切换完成 T_sw            旧凭据失效 T_dead

  窗口 W = T_dead − 首次可用
    ├─ 漏改/失败风险 = f(W)：W 越长，漏掉的使用方越有可能在 T_dead 前被补上
    └─ 暴露风险     = g(W)：W 越长，泄露出去的旧凭据可用的时间越长
  两条曲线方向相反 ⇒ W 只能被"优化"，不能被"消除"
```

### 三类凭据的轮转差异：能不能零停机取决于「服务端支不支持两个同时有效」

| 类型 | 服务端是否支持双密钥 | 生效延迟的真实形态 | 关键坑 | 建议窗口 W |
| --- | --- | --- | --- | --- |
| **数据库密码** | MySQL **8.0.14+ 支持双密码**（`RETAIN CURRENT PASSWORD`）；PostgreSQL **不支持**（`ALTER ROLE ... PASSWORD` 立即生效） | 改密码**不会踢掉已建立的连接**——旧连接能一直用到连接关闭 | PG 的唯一办法是「双账号 + 应用切账号」，或接受一次重连风暴（→ [44 连接池与客户端](44-connection-pool-and-client.md) §44.5） | 分钟～小时（有双密码）／一个发布周期（无） |
| **TLS 证书** | **天然支持多张并行**（服务器可同时持有新旧链、客户端信任库可同时信任新旧 CA） | 新证书立即生效；旧证书到 `notAfter` 才失效；**吊销靠 CRL/OCSP，有缓存延迟** | CA 轮换要「信任库先扩后收」；时钟偏了 5 分钟就会 `certificate expired` | 天级（且越短寿命越安全） |
| **API key** | **取决于对方**：AWS IAM 每 user 最多 2 个 AK；OAuth client secret 多数 IdP 支持多 secret；第三方 SaaS 常常**只有 1 个槽** | 单槽厂商：改 key 瞬间旧 key 失效，**必然有失败窗口** | 只能「停写 → 改配置 → 重启 → 恢复」，或用网关做一层自己签发的内部凭据（把单槽问题挡在内网之外） | 双槽：小时～天；单槽：**只能约定停机窗口** |

### 轮转失败时「旧密钥还能用多久」——这是事故响应里第一个要问的数

| 旧凭据所在的位置 | 继续有效的时长 | 依据 / 备注 |
| --- | --- | --- |
| 已建立的长连接、连接池里的连接 | **直到连接关闭**（可能数小时～数天） | 会话已认证，改密码/删 key 不影响；受 `wait_timeout`、`maxLifetime` 钳制 |
| 应用进程内存缓存 | 直到进程重启 | 这是「吊销」在你系统里的真实下限 |
| 客户端 SDK 的 token 缓存 | 直到 `exp` | 短 TTL 在这里救你一命 |
| 容器镜像层里的凭据 | **永久**（直到镜像被删） | 事故二：镜像层 + CI 日志两个泄露点 |
| 证书 | 直到 `notAfter` | 所以短寿命证书是唯一「自愈」的形态 |
| CRL / OCSP 缓存 | 直到 `nextUpdate`（可数小时） | **吊销真正生效的时间不可控**——这是「证书吊销靠不住」的根本原因 |
| KMS 的旧 key version（用于解 `WrappedDEK`） | **永久**，除非 `Disable`/`ScheduleKeyDeletion` | 删 key 等于让历史密文永久不可解，必须有「保留期」制度 |
| 被裁剪的分支 / 回滚前的镜像 / 旧 ConfigMap 副本 | 永久 | 所以「轮转」必须包含「搜全仓与全部镜像」这一步 |

```sql
-- MySQL 8.0.14+：双密码轮转，可以做到零停机（PostgreSQL 没有等价物）
-- ① 加新密码，旧密码继续有效（此时两套都能登录）
ALTER USER 'app'@'%' IDENTIFIED BY 'N3w-P@ss' RETAIN CURRENT PASSWORD;

-- ② 观察期：让所有应用分批重启/重建连接池，确认新密码可登录
SELECT user, host, plugin FROM mysql.user WHERE user = 'app';
SELECT COUNT(*) AS conns_using_old FROM information_schema.PROCESSLIST WHERE USER = 'app';
-- （真实判定要看应用侧上报的"当前凭据版本"，而不是猜测）

-- ③ 撤掉旧密码（这一步不可逆，务必在②确认之后）
ALTER USER 'app'@'%' DISCARD OLD PASSWORD;

-- 关键前提：改密码不会踢掉已有连接，所以"应用是否已切换"必须靠应用自报，
-- 不能靠数据库侧看连接数——旧连接会一直存活到 wait_timeout 或池主动回收。
```

### 灰度与回滚：轮转的发布纪律

| 阶段 | 做什么 | 通过的判据 | 失败怎么回滚 |
| --- | --- | --- | --- |
| 1 生成 | 建新凭据，**不激活** | 新凭据能完成一次真实调用 | 直接删 |
| 2 双活 | 新旧同时有效，窗口 = `2 × 最长发布周期 + 回滚预算` | 所有使用方上报「我用的是新版本」的比例 ≥ 99% | 旧凭据仍在，零动作回滚 |
| 3 灰度切换 | 按租户/实例分批，每批一次发布 | 每批错误率不升、连接池重建成功 | 把该批配置指回旧凭据 |
| 4 收尾 | 撤旧凭据 | 新凭据承载 100% 流量 **且持续时间 > 一个发布周期** | **不可回滚**——所以第 4 步必须放到下一轮轮转时做（保留「上一代」） |

> **一条能省下大量事故的纪律：永远保留「上一代」凭据**。轮转的收尾不是「删掉旧的」，而是「把旧的降级为上一代，等到下次轮转时再删」。这样任何一次轮转都有一次「用上一代」的回滚机会，代价只是窗口翻倍（而窗口的代价见账三）。

---

## 47.7 三本账：续租 QPS、KMS 并发、轮转爆炸半径

三个算例都按「乘法链 + 假设清单 + 量级结论」写。**先说清每本账打的是谁**：账一打在 **Vault**（续租是写操作，会打满 Raft）；账二打在 **KMS**（解密是网络调用，会撞配额与账单）；账三打在你的 **发布系统与使用方清单**（轮转失败不是技术问题，是枚举问题）。

### 账一 · lease 续租 QPS = 凭据数 ÷ 续租间隔（尖刺与 TTL 无关）

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | Pod 数 | 平台规模 | 4,000 |
| 2 | 每 Pod 凭据数 | MySQL 主库 / 只读 / 对象存储 STS | 3 |
| 3 | 凭据数 `N` | `4,000 × 3` | **12,000** |
| 4 | TTL `T` | `database/roles` 的 `default_ttl` | 3,600 s |
| 5 | 续租间隔 `h` | 惯例 `T/2`（留出一次失败重试余量） | 1,800 s |
| 6 | **平均续租 QPS** | `N ÷ h`（恒等式） | **6.67 次/s** |
| 7 | 每次续租的物理成本 | 1 次 barrier 加密 + 1 次 Raft 写（含 fsync）+ 1 次审计写 | 1 次写 / 次续租 |
| 8 | Vault 单集群写能力 | 3 节点 Raft、NVMe，按 **300–800 次/s 量级**（以目标版本与硬件为准） | 取 500 |
| 9 | 常态占用 | `6.67 ÷ 500` | **1.3%**，完全无事 |
| 10 | 齐射峰值（无抖动） | `N ÷ 定时器精度`，取 10 s 桶 | **1,200 次/s** = 平均的 180× |
| 11 | 齐射占用 | `1,200 ÷ 500` | **240%** ⇒ 503 / 超时（事故一 03:36） |
| 12 | 客户端 3 次立即重试 | `1,200 × 3` | **3,600 次/s** = 720% |
| 13 | 加抖动（续租时刻在 `[0.5T, 0.8T]` 均匀随机） | 抖动窗宽 `0.3 × 3,600 = 1,080 s` ⇒ 每个 10 s 桶的期望 `12,000 ÷ 108 = 111`，泊松 P99 ≈ `111 + 2.33√111` | **≈ 136 次/s**（1,200 → 136，降 8.8×） |
| 14 | 把 TTL 缩到 300 s（「更安全」） | 平均 `12,000 ÷ 150` = 80 次/s；**尖刺仍是 1,200 次/s**（尖刺只看 N 与桶宽） | 平均占用 16%，尖刺不变 |
| 15 | 结论 | 平均续租 QPS 由 TTL 决定；**尖刺由「对齐程度」决定，与 TTL 无关** | 缩短 TTL 只抬高地基，不削峰 |

> **量级结论（非承诺值）**。假设：① 4,000 Pod × 3 凭据 = 12,000 个 lease；② `T=3,600 s`、在 `T/2` 续租；③ 每次续租是一次 Raft 写（Vault 的 lease 更新不区分「读」与「写」，一律过 barrier + 存储）；④ 3 节点 Raft 在 NVMe 上的持续写能力 300–800 次/s（**以目标版本与硬件文档为准**，SSD 与跨 AZ 网络差异很大）；⑤ 审计设备不额外成为瓶颈；⑥ 抖动窗口 `[0.5T, 0.8T]`。第 6 步是恒等式（给定 N 与 h，续租 QPS 必须等于 `N/h`），可以当承诺值用；第 8–13 步是量级。**这张账最有用的一行是第 14 步**：很多团队把「TTL 从 1 小时缩到 5 分钟」当成安全改进，结果**尖刺一点没变，地基抬高 12 倍**——真正削峰的是抖动与 single-flight，不是 TTL。

### 账二 · KMS 解密 QPS × 延迟 = 需要多少并发（以及多少美元）

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 对象读取 QPS | 会话记录/文档读取 | 1,200 次/s |
| 2 | 每次读取的 KMS 调用 | 1 次 `Decrypt(WrappedDEK)`（无缓存时） | 1,200 次/s |
| 3 | KMS 延迟 | 同区 p50 8 ms / **p99 25 ms**（含 TLS + 鉴权 + HSM） | 取 p99 = 0.025 s |
| 4 | 需要的在飞并发（Little 定律） | `1,200 × 0.025` | **30** 个并发请求 |
| 5 | 连接池/并发上限 | `30 × 2`（headroom） | **60**（Go 的 `MaxIdleConnsPerHost` 默认 2，**必须调**） |
| 6 | 区级配额 | 对称密钥密码学操作按区域共享配额，「数千到数万 req/s 量级」（以目标区域文档为准） | 取 10,000 |
| 7 | 常态占用 | `1,200 ÷ 10,000` | **12%**，有余量 |
| 8 | 叠加一次批量作业 | 索引重建 20,000 对象/s 各自 1 次 Decrypt | 合计 **21,200 次/s = 212%** ⇒ `Throttling` |
| 9 | 加租户级 DEK 缓存（TTL 300 s） | 活跃租户 200 ⇒ `200 ÷ 300` | **0.67 次/s**（降 1,800×） |
| 10 | 美元：无缓存 | `1,200 × 86,400 = 1.037 亿次/天`；每万次约 $0.03 | **≈ $311/天 ≈ $9,300/月** |
| 11 | 美元：有缓存 | `0.67 × 86,400 ≈ 5.8 万次/天` | **≈ $0.17/天** |
| 12 | 缓存带来的新约束 | 缓存 TTL ≤ 「轮转后旧 DEK 还能解」的容忍窗口；且缓存的是**明文 DEK**，落在进程内存里 | 用 KMS 的 `EncryptionContext` 绑定租户，防止串租户 |

> **量级结论（非承诺值）**。假设：① 1,200 次对象读/s，每读 1 次 KMS 调用；② KMS 同区 p99 25 ms（跨区会到 40–80 ms 量级）；③ 配额 10,000 次/s（**以目标区域与密钥类型文档为准**：非对称密钥的 Sign/Decrypt 配额低一到两个量级，只有数百次/s）；④ 每万次请求 $0.03（**以当期价目为准**）；⑤ 缓存按活跃租户数计，命中率随租户活跃度分布良好。**第 9 步的降幅不是「优化技巧」而是「架构必需」**：没有 DEK 缓存，KMS 就同时是延迟瓶颈、配额瓶颈和账单瓶颈；有了缓存，KMS 只在缓存未命中时出现——而**它的调用次数变成了 `活跃密钥数 ÷ 缓存 TTL`，与数据量解耦**。

### 账三 · 轮转爆炸半径 = 使用方数量 × 重叠窗口

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 使用方数量 `U` | 内部 597 服务 + 3 个第三方 | **600** |
| 2 | 每方部署实例中位数 | 平均 12 个实例需要改配置/重启 | 12 |
| 3 | 需要改动的配置点 | `600 × 12`（**计数恒等式**） | **7,200 处** |
| 4 | 漏改/失败率 `p` | 经验值：无强制上报机制时 1%–3% | 取 2% ⇒ **144 处** |
| 5 | 每处 QPS | 中小服务 | 20 次/s ⇒ **2,880 次/s** |
| 6 | 旧凭据失效瞬间的失败率 | 这些请求 **100%** 401/认证失败 | 2,880 次/s |
| 7 | MTTD（发现时间） | 告警延迟 + 值班响应，取 8 分钟 | 480 s |
| 8 | **失败请求数** | `2,880 × 480` | **≈ 138 万次** |
| 9 | 反向风险：暴露时长 | 重叠窗口 `W` = 泄露的旧凭据可继续使用的时间 | `W = 72 h` ⇒ 泄露后 3 天可用 |
| 10 | 加「凭据版本上报」强制校验 | `p` 从 2% 降到 0.2% ⇒ 14 处 ⇒ 280 次/s | 失败请求降 10×（≈ 13.4 万次） |
| 11 | 按类型优化 `W` | DB 密码用双密码 ⇒ `W` 可压到分钟级；证书 `W` = 旧证书剩余寿命（天级）；第三方单槽 API key **无法压** | 混合策略，按使用方分类设 `W` |
| 12 | 结论 | **爆炸半径 = `U × p × 每方 QPS × MTTD`；而 `W` 同时是安全成本** | 两个方向共用一个旋钮 |

> **量级结论（非承诺值）**。假设：① 600 个使用方、每个 12 个实例；② 无「凭据版本上报」时漏改率 2%；③ 漏改方平均 20 次/s；④ MTTD 8 分钟（如果没做「401 单独打点」可以到小时级）；⑤ 重叠窗口 72 小时。第 3 步是**计数恒等式**（可以当承诺值），第 4–8 步是量级。**这张账的真正结论在第 10 与 11 步**：降低爆炸半径最有效的动作不是「把窗口调长」，而是**让使用方必须上报凭据版本**（把 `p` 降下来），以及**按凭据类型分别设窗口**（DB 密码有双密码机制，可以短；第三方单槽 key 只能停机）。

**敏感性（哪几个假设最值得改）**：

| 假设 | 变动 | 账一（续租峰值） | 账二（KMS/美元） | 账三（失败请求） |
| --- | --- | --- | --- | --- |
| 抖动 | 无 → `[0.5T,0.8T]` | **1,200 → 136 次/s（8.8×）** | 不变 | 不变 |
| TTL | 3,600 → 300 s | 平均 6.67 → 80 次/s（尖刺不变） | 缓存 TTL 缩短则调用上升 | 不变 |
| 凭据版本上报 | 无 → 有 | 不变 | 不变 | **138 万 → 13.4 万（10×）** |
| DEK 缓存 | 无 → 300 s | 不变 | 1,200 → 0.67 次/s；$9,300 → $5/月 | 不变 |
| 批量作业并发 | 20,000 → 2,000 对象/s | 不变 | 212% → 32% 配额（不再限流） | 不变 |
| 重叠窗口 | 72 h → 6 h | 不变 | 不变 | 失败请求不变，**暴露时长降 12×**，但漏改率上升 |

> **量级结论（非承诺值）**。评审上最常被问的三个数就是上表前三行：**「加了抖动会怎样」**（账一降 8.8 倍，几乎免费）、**「TTL 再短一点行不行」**（账一地基抬 12 倍、尖刺不变，而账三的暴露窗口跟着缩短）、**「不做版本上报会怎样」**（账三的 `p` 从 0.2% 变 2%，失败请求 ×10）。**三个数必须一起报，只报一个都是误导。**

---

## 47.8 边界：Vault 不能当什么，KMS 不能做什么，「一次一租」贵在哪

### 第一张表：把凭据系统当别的东西用，会怎么坏

| 想拿它做什么 | 物理上为什么不行 | 会怎么坏 | 该用什么 |
| --- | --- | --- | --- |
| **Vault 当配置中心** | 每次读都要认证 + barrier 解密 + 一次存储读 + **一条审计写**；没有 watch/推送，只有轮询 | 轮询把 Vault 读打满；审计日志按 QPS 线性涨盘；配置推送延迟取决于轮询周期 | 配置中心（→ comp/38），Vault 只放「配置里的密钥指针」 |
| **Vault 当数据库 / KV 存储** | 没有查询、没有索引、没有事务；写要过 Raft 多数派 + fsync | 写吞吐几百次/s 就满（账一第 8 步）；lease 数一多，重启恢复时间线性涨 | 真正的库（→ comp/02、→ comp/17）；Vault 只存指针 |
| **Vault 当缓存** | 每次读都是「网络 + 认证 + 解密 + 审计」四跳 | 延迟 1–5 ms 量级，且随 QPS 排队 | 本地进程缓存 + 短 TTL（§47.3 的 Go 骨架） |
| **Vault 当消息队列 / 事件总线** | 没有消费位点、没有回放、没有 ack | 断线即丢；没有重试 | Kafka / 任务队列（→ comp/28、→ comp/32） |
| **KMS 直接加密大文件** | 对称 `Encrypt` 单次只接受几 KB；大文件必须信封加密 | 调用次数 = 分块数，延迟与账单同时爆炸 | 本地 AES-GCM + KMS 只包 DEK（§47.4） |
| **KMS 当 HSM 用来存私钥** | KMS 的密钥不可导出，也不给你做任意密码学运算（只给固定几种操作） | 需要自定义运算的场景直接无解 | 云 HSM / Vault `transit` + PKCS#11 |
| **Vault 当「所有人共用一个 root token」** | root token 不受 policy 约束；审计里所有人同一身份 | 审计等于没有；一次泄露 = 全部 | 每工作负载一个 policy + 短 TTL token（或直接不用 token，走 JWT/OIDC 认证） |
| **把 `transit` 与 `database` 放同一个集群** | 共享同一个 barrier 与 unseal | seal 一次，签发与加解密同时停 | 按故障域拆集群，或明确接受同一单点 |

### 第二张表：「一次一租」的成本清单（每一项都要有人付）

| 成本项 | 具体是什么 | 量级 | 谁来付 |
| --- | --- | --- | --- |
| **续租流量** | `N ÷ TTL` 次写/s，且尖刺 = `N ÷ 桶宽` | 账一：12,000 凭据 ⇒ 6.67 次/s，尖刺 1,200 次/s | Vault 集群（加节点不解决尖刺，抖动才解决） |
| **签发 DDL** | 动态 DB 凭据的每次签发/回收都是 `CREATE USER`/`DROP USER` | 恢复瞬间 12,000 次 DDL（事故一 03:36） | 目标数据库（`Threads_running`、元数据锁） |
| **客户端复杂度** | 缓存 + 抖动 + single-flight + 过期兜底 + 版本上报 | 每个接入 SDK 2–5 人日量级 | 每个使用方 |
| **故障容忍下降** | `可用性 ≈ TTL`（超时 MTTR 就全挂） | TTL 1 h ⇒ MTTR 必须 < 1 h | 值班与运维（auto-unseal、多 key holder） |
| **轮转工作量** | 使用方数量 × 配置点（账三第 3 步：7,200 处） | 人日级 × 使用方数量 | 平台团队 + 每个使用方 |
| **审计存储** | 每请求一条日志 | 按 1 KB/条、1,000 req/s 计 ≈ 86 GB/天 | 日志系统（要单独配保留期与限流） |

> **这句话在评审上最有用**：**「一次一租」不是免费的，它把「泄露风险」换成了「续租流量 + DDL + 客户端复杂度 + 故障容忍度」四项负债。** 所以正确的问法不是「要不要一次一租」，而是「**每一类凭据分别该有多短的 TTL**」——数据库主库口令可以 1 小时，第三方单槽 API key 可能只能 90 天，对象存储的 STS 可以 15 分钟。**用同一档 TTL 覆盖所有凭据，是最常见也最贵的偷懒。**

### 决策表：按顺序排除，五步走完

| 步 | 问 | 是 → | 否 → |
| --- | --- | --- | --- |
| 1 | 这份凭据泄露后，攻击者能持续做什么？（读数据 / 花钱 / 横向移动） | 有任一 → 必须有 TTL 与吊销通道 | 只读公开数据 → 用普通配置即可，结束 |
| 2 | 签发方支持短 TTL 吗？（云 STS、Vault 动态凭据、证书） | 支持 → 走「一次一租」，跳到 4 | 不支持 → 下一步 |
| 3 | 服务端支持双密钥/双密码吗？（MySQL 8.0.14+ 双密码、IAM 双 AK、多证书） | 支持 → 走「长 TTL + 自动轮转 + 双活窗口」 | 都不支持 → 只能「约定停机窗口 + 网关代理内部凭据」，并在风险清单里写明 |
| 4 | 客户端有缓存 + 抖动 + single-flight 吗？（账一第 10–13 步） | 有 → 缩 TTL 是安全的 | 没有 → **先补客户端，再缩 TTL**；顺序反了就是事故一的后 16 分钟 |
| 5 | 使用方清单能枚举、能上报版本吗？（账三第 10 步） | 能 → 窗口可以压到发布周期量级 | 不能 → 窗口必须 ≥ `2 × 最长发布周期 + 回滚预算`，并接受暴露时长 |

> **这张表的用法**：从 1 往下走，**遇到的第一个分支就是你的档位**。最坏的分支是第 3 步的「都不支持」——那时你买的不是安全性，是「一个写在纸上的已知风险」，至少要是诚实的。**而第 4 步的顺序错误是全篇最贵的一条**：先缩 TTL 再补客户端，等于主动把事故一的形状装进系统。

---

## 47.9 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到物理形态） | 定位 | 修复 | 防复发 |
| --- | --- | --- | --- | --- |
| Vault 重启后业务全挂，日志只有 `503` | 节点 `Sealed=true`，barrier 打不开（§47.3 图 1 的第 ②层） | `vault status` 看 `Sealed` / `HA Mode`；逐个节点查 | 凑够 unseal 份额；或上 auto-unseal | 把「unseal 需要几个人」写进值班表；告警盯 `unsealed` |
| unseal 完两个节点，第三个却成了 leader，只有一半流量正常 | 每个节点必须各自 unseal；unseal 期间 active 可能漂移 | `vault status` 在三个节点上分别看 | 遍历全部节点 + 客户端跟随 standby 重定向 | unseal 脚本必须幂等且遍历全节点 |
| 恢复瞬间 Vault 被打死第二次 | 4,000 进程同步来签发，且动态凭据签发 = 目标库 DDL（事故一） | `vault.core.handle_request` 延迟尖刺 + 目标库 `Threads_running` | 客户端退避 + single-flight + 分批放行 | 把「恢复演练」当常规演练，且限流默认开 |
| 凭据在半夜「集体」失效一次 | `exp = 创建时刻 + TTL`，批量部署 ⇒ 批量过期（事故三） | 查凭据过期时间分布是否呈一根尖峰 | 刷新时刻抖动，窗口 ≥ 发布批次跨度 | 客户端 SDK 强制抖动，禁止固定比例刷新 |
| 每小时的整点 KMS 账单尖峰 | 每个对象每读一次都调 `Decrypt`（账二第 2 步） | KMS 调用数与对象读 QPS 完全同形 | 加租户级 DEK 缓存（TTL ≤ 轮转容忍窗口） | 把「KMS 调用数 ÷ 数据读 QPS」作为架构指标 |
| 批量作业把线上 KMS 调用打成 `Throttling` | 配额是按区域共享的（账二第 8 步） | 云侧配额与限流指标，按 `key_id` 分组 | 批量作业独立 key + 独立配额预算 + 队列削峰 | 在架构上隔离「在线 key」与「批量 key」 |
| 轮转后 3 个服务 401，其他正常 | 漏改使用方（账三漏改率 `p`） | 按「凭据版本」上报排查；401 单独打点 | 把漏改方配置指回旧凭据（保留上一代） | **强制凭据版本上报**，未上报者不允许进下一轮 |
| 改完数据库密码，旧连接还在用旧密码 | 改密码不影响已建立的会话（§47.6 表） | 应用侧连接池重建日志；库侧 `PROCESSLIST` 看连接年龄 | 滚动重启/重建池；MySQL 用双密码过渡 | 「凭据切换」必须包含「连接池重建」这一步 |
| 证书吊销了，客户端还在用 | CRL/OCSP 有缓存，生效时间不可控 | 抓包看是否仍在用旧证书；CRL `nextUpdate` | 缩证书寿命（小时～天级），让吊销变成兜底 | 默认只用短寿命证书，不依赖吊销 |
| 明文 DEK 在进程里被 dump 出来 | 缓存的是 plaintext DEK（账二第 12 步） | 内存 dump / core 文件 | 缓存加密形式 + 用后清零；必要时 `mlock` | 把 DEK 生命周期写进代码评审清单 |
| 镜像层里躺着 `.aws/credentials` | 凭据被打进构建产物（事故二） | 镜像层扫描 + `docker history` | 换工作负载身份（IRSA/SPIFFE），彻底删掉静态 AK | CI 门禁：镜像/日志出现凭据模式即拒绝 |
| 审计日志把磁盘写满，Vault 自己挂了 | 审计设备每请求一条，且默认阻塞 | 审计文件所在盘的使用率 | 独立盘 + 保留期 + 限流；审计写到远端 socket | 审计盘与数据盘分离，且监控**审计写失败** |
| root token 在某人电脑的 `.bash_history` 里 | root token 绕过所有 policy | 查 audit log 里的 `display_name` | `revoke` root token，改走 JWT/OIDC 认证 | 生产禁止 root token；`generate-root` 走双人流程 |
| lease 数量涨到几十万后重启恢复要十几分钟 | expiration manager 启动要恢复**全部** lease | `vault.expire.num_leases` 与启动耗时同形 | 砍凭据数（合并凭据/放长 TTL）+ 分集群 | 把 `num_leases` 当容量指标纳入规划 |
| 把 Vault 的 `rotate` 当成了「全量重加密」 | 轮换只让新写用新密钥，旧条目靠 rewrap 逐步迁 | 检查旧条目是否仍带旧 key 版本 | 写批处理 rewrap 作业（它是一次 KMS/Vault 调用洪峰，要限速） | 术语纪律：区分「换密钥」与「重加密」 |

---

## 47.10 观测：五个数覆盖 90% 的凭据故障

| 指标 | 看什么 | 拐点阈值 | 版本 / 开关前提 |
| --- | --- | --- | --- |
| **`vault.core.unsealed`**（1=unsealed，0=sealed） | **有没有 sealed**——这是全站级开关 | 任意节点为 0 持续 30 s ⇒ 立即告警（不是「关注」） | 需在 Vault 配置开 `telemetry { prometheus_retention_time = "30s" }`，`/v1/sys/metrics` 默认需要 token（可设 listener 的 `unauthenticated_metrics_access`） |
| **`vault.expire.num_leases`** | 凭据数 = 续租 QPS 的分子、重启恢复时间的主项 | 与容量模型偏离 30% 即查（谁在灌短 TTL 凭据） | 同上；注意它是 gauge，需按实例看（standby 也有值） |
| **续租失败数**（应用侧 `cred_renew_fail_total` + Vault 侧 5xx） | 是否已经「靠缓存活着」——最接近事故的一个数 | `> 0` 且 5 分钟持续 ⇒ 告警 | 需客户端 SDK 埋点；Vault 侧看 `vault.core.handle_request` 的 5xx 分位 |
| **`vault.runtime.*` / Raft 写延迟** | 续租与签发是否在排队 | Raft apply/fsync P99 > 25 ms，或写 QPS 逼近账一第 8 步的上限 | integrated storage 才有 Raft 指标；Consul backend 看 Consul 侧 |
| **KMS 限流与调用速率** | 配额是否被打满、账单是否异常 | `Throttling` 计数 `> 0`；调用数 / 数据读 QPS 的比值 > 1（说明缓存失效） | 云侧指标名因厂商而异（AWS `UserRequestsThrottled`，需在 CloudWatch 里按 key 分组） |
| **凭据年龄与刷新错误率** | 静态凭据还剩多长寿命；OIDC 刷新失败 | `credential_age_seconds` P99 > 90 天即告警；刷新错误率 > 1% | 需自建「凭据台账」（谁、何时创建、何时轮转、谁在用）；OIDC 侧看 `sts:*WithWebIdentity` 的 5xx/429 |

```yaml
# 告警骨架（Prometheus 3.x；Vault 指标名需与目标版本的 telemetry 对齐）
groups:
  - name: secrets-and-credentials
    rules:
      - alert: VaultSealed
        expr: vault_core_unsealed == 0
        for: 30s
        annotations: {summary: "有节点 sealed：barrier 打不开，签发与续租同时停；按 unseal 流程处置"}
      - alert: LeaseRenewFailing
        expr: increase(cred_renew_fail_total[5m]) > 0
        for: 5m
        annotations: {summary: "续租失败：业务正在靠缓存活着，剩余寿命 = 最早过期凭据"}
      - alert: LeaseCountSpike
        expr: vault_expire_num_leases > 1.3 * vault_expire_num_leases offset 1d
        annotations: {summary: "lease 数 1 天内涨 30%：有人用短 TTL 灌凭据，账一会先打满"}
      - alert: KmsThrottled
        expr: increase(kms_throttled_total[5m]) > 0
        annotations: {summary: "KMS 限流：查缓存命中率与批量作业是否共用同一把 key"}
      - alert: RotationStale
        expr: credential_age_seconds > 90 * 86400
        annotations: {summary: "存在 90 天未轮转的凭据：确认它是否仍被使用（事故二的形状）"}
      - alert: TokenRefreshErrorRate
        expr: rate(oidc_refresh_errors_total[10m]) / rate(oidc_refresh_total[10m]) > 0.01
        for: 10m
        annotations: {summary: "token 刷新错误率 > 1%：查配额、Retry-After 与抖动窗口"}
```

> **三条判读纪律**：① **`unsealed` 是唯一一个「单指标即全站事故」的数**，它的告警级别应该和「数据库主库挂了」同级；② **续租失败不等于事故，凭据过期才是**——必须先看「最早过期时间还剩多久」，再决定是抢修还是降级（这就是 §47.3 里「续租失败时返回未过期旧凭据」的观测含义）；③ **`credential_age_seconds` 这类指标必须来自凭据台账而不是猜**，没有台账的团队通常连自己有多少把静态凭据都不知道，事故二正是这样发生的。

---

## 47.11 一页速记

| 记什么 | 具体结论 |
| --- | --- |
| **一句话本质** | 凭据不是一段字符串，是**有起点、有寿命、有吊销通道、有审计记录的租约** |
| **唯一真问题** | 最贵的泄露不是数据是凭据：**一次一租、可吊销、有 TTL**；seal/unseal 与 lease 决定故障时会不会全站等锁 |
| **四个属性** | 机密性、可吊销、短 TTL、可审计；**加密只解决第一个** |
| **Vault 四层** | API（认证+审计）→ barrier（AES-256-GCM + keyring）→ 逻辑层（engine + lease）→ storage（密文） |
| **sealed 的含义** | 只剩 `sys/seal-status`/`sys/unseal` 可用；**已签发凭据仍按自己的 TTL 有效** ⇒ 失效是渐进的 |
| **两层 quorum（别混）** | Raft 多数派（机器）决定存储可读写；Shamir 份额（**人**）决定 barrier 能不能开 |
| **auto-unseal 的真相** | 把人换成 KMS，依赖链变 `业务→Vault→KMS→IAM/网络`；恢复期产出的 recovery keys **不用于 unseal** |
| **动态凭据的物理动作** | 签发 = Raft 写 + `CREATE USER`；回收 = `DROP USER` ⇒ **签发 QPS 就是目标库的 DDL QPS** |
| **信封加密** | KEK 不出 HSM，DEK 一对象/一租户一份；KMS 是「加密钥匙的钥匙」，不是数据加密器 |
| **KMS 三堵墙** | 延迟（5–30 ms）、配额（区域共享，非对称低 1–2 个量级）、账单（按次）⇒ 必须缓存 DEK |
| **续租 QPS** | 平均 = `凭据数 ÷ TTL/2`；**尖刺 = `凭据数 ÷ 桶宽`，与 TTL 无关**（抖动是必需件） |
| **KMS 并发** | 在飞并发 = `解密 QPS × p99 延迟`；缓存后调用数 = `活跃密钥数 ÷ 缓存 TTL` |
| **轮转爆炸半径** | `使用方数 × 每个使用方的配置点 × 漏改率 × 每方 QPS × MTTD`；窗口 `W` 同时是安全成本 |
| **三类轮转** | DB 密码（MySQL 8.0.14+ 双密码可零停机，PG 不行）／证书（天然并行，**吊销不可靠**）／API key（单槽只能停机） |
| **旧密钥还能用多久** | 长连接直到关闭、内存缓存直到重启、证书到 `notAfter`、CRL 到 `nextUpdate`、镜像层**永久** |
| **最该监控的两个数** | `vault.core.unsealed`（全站开关）与「最早过期凭据还剩多久」（真正的倒计时） |
| **换零件的信号** | 续租 QPS 逼近 Vault 写能力、签发 DDL 打爆目标库、KMS 不能缓存、使用方枚举不出来 |

**如果只记五句：**

1. **凭据的价值在于「它多久会自己死」和「出事时能不能立刻弄死它」，不在于它有多长多复杂。** 加密强度高的永久密钥，安全性等于零。
2. **sealed 不是「已经出事」，是「倒计时开始」**：已签发的凭据继续有效到 TTL，所以 seal 的影响面是时间的函数；而恢复瞬间的齐射往往比 sealed 本身更致命。
3. **动态凭据把「读一个字符串」变成了「对目标库做一次 DDL」**，所以「一次一租」的容量约束不在 Vault，在目标库的元数据锁与连接数上。
4. **KMS 不是本地函数，是网络 + 配额 + 账单**；用它加密数据是架构错误，用它包 DEK 且缓存起来才是正解。
5. **轮转真正难的不是生成新密钥，是枚举使用方**：600 个使用方、7,200 个配置点，漏 2% 就是百万次失败请求——所以「凭据版本上报」比「自动轮转工具」更值得先做。

---

## 47.12 自检题（5 题）

**Q1.** 你们的 Vault 三节点集群在某次机房维护后全部 `Sealed=true`。老板问：「不是高可用吗？为什么三个节点一起挂？」请回答：① Raft 高可用到底保证了什么、没保证什么；② 从 seal 那一刻起，业务是怎么一步步坏掉的；③ 恢复后为什么会「再坏一次」；④ 最少要做哪四件事才能把这类事故的损失压到十分之一。

<details><summary>答案</summary>

**① 保证了什么**：Raft 保证的是**存储层的多数派与一致性**——只要还有 ≥ ⌈3/2⌉ = 2 个节点在线且已 unseal，就能读写；它**没有保证**「节点重启后能自己服务」，因为 barrier 的钥匙（root key）不在盘上，必须由 Shamir 份额或 KMS 解锁。三个节点一起断电重启 ⇒ 三个都 sealed，可用性归零。

**② 怎么一步步坏掉**：seal 只让 Vault 不再签发/续租；**已签发的凭据仍按自己的 TTL 有效**。所以业务错误率从 0% 开始，随每个凭据的剩余寿命逐段上升：短 TTL 的（5 分钟缓存）先死，1 小时的 60 分钟内死光（事故一：12 分钟 27%，60 分钟 100%）。**这解释了为什么「seal 了但没人报错」的前 12 分钟最危险。**

**③ 为什么再坏一次**：unseal 完成的瞬间，所有还活着的进程（4,000 个）发现续租失败，同时来要新凭据 ⇒ 12,000 次签发请求在一秒内到达，而**动态数据库凭据的每次签发都是在目标库上 `CREATE USER`** ⇒ Vault 与目标库一起被打满；客户端的 3 次立即重试再把它放大 3 倍（事故一 03:36–03:52，全站错误率 42%）。

**④ 四件事**：a. **客户端本地缓存 + 续租抖动 + single-flight**（把恢复齐射削掉一个量级）；b. **auto-unseal（KMS）或多地多 key holder**（把「人」这个 quorum 缩短到分钟级），并把 KMS 依赖单独做可用性预案；c. **恢复限流与分批放行**（Vault 侧令牌桶 + 客户端退避，不许「重试到成功为止」）；d. **把 TTL 分层**（主库口令 1 h、STS 15 min、第三方 key 90 天），避免所有凭据同时到期。

量级结论，非承诺值；参数见 §47.1 事故一与 §47.7 账一。
</details>

**Q2.** 有人提议：「KMS 调用太贵、太慢，我们干脆把 KEK 直接从 KMS 导出到本地，用本地函数加密。」这个方案在哪一步错了？如果必须降低 KMS 调用量，正确的三条动作是什么？

<details><summary>答案</summary>

**错在「导出 KEK」这一步**：KMS 的全部价值就是**KEK 永不出 HSM**——它把「密钥泄露」变成「调用权限泄露」。一旦 KEK 导出到本地，你把「一个有配额、有审计、可吊销的网络服务」退化成了「一个躺在磁盘/内存里的长期密钥」，安全性回到事故二那一档，而且**你以为自己还在用 KMS**。

**三条正确动作**：① **缓存 DEK，不缓存 KEK**：租户级 DEK 明文缓存在进程内存，TTL 设为「轮转容忍窗口」以内（账二第 9 步：1,200 → 0.67 次/s）；② **降粒度**：从「每对象一个 DEK」改成「每租户一个 KEK + 每对象一个 DEK」，其中只有 DEK 需要 KMS 参与，且用 `GenerateDataKey` 一次拿到明文+包装两份；③ **隔离与削峰**：在线 key 与批量作业 key 分开（避免批量任务吃掉在线配额），批量侧用队列限速并把 rewrap 作业放到低谷。

补充一条常被忽略的：**缓存的是明文 DEK，就得处理内存泄露**（不落盘、不写日志、必要时清零），并且要用 KMS 的 `EncryptionContext` 把 DEK 与租户绑定，防止缓存串租户。

量级结论，非承诺值；见 §47.4 与 §47.7 账二。
</details>

**Q3.** 8,000 个 Pod 用 IRSA，今天上午 8,000 个 STS 凭据在同一分钟过期，把 STS 与业务一起打死。请给出：① 峰值为什么正好是 8,000 次/s；② 三条修复各自把峰值降到多少；③ 为什么「把 TTL 调长到 6 小时」不是解法。

<details><summary>答案</summary>

**① 为什么是 8,000**：`exp = 创建时刻 + TTL`。批量扩容让 8,000 个 Pod 的创建时刻落在同一批（20 分钟内），而 SDK 在**固定比例**（如 TTL 的 80%）刷新，且刷新判定窗口是秒级定时器 ⇒ 8,000 次刷新落在同一个秒级窗口 ⇒ 8,000 次/s，与常态 2.2 次/s 相比放大 3,600×（事故三 11:48）。

**② 三条修复的降幅**：a. **刷新时刻抖动到 `[0.5T, 0.8T]`**：8,000 ÷ (0.3 × 3,600) ≈ **7.4 次/s**（降 1,080×）；b. **进程级 single-flight + 节点级 token 缓存**（sidecar/Agent 合并同节点多进程）：若 8,000 Pod 在 800 节点，≈ **0.74 次/s**；c. **遵守 `Retry-After` + 重试上限 3 次**：把「失败时的放大器」从 ×10 降到 ×3，并让恢复是渐进的。

**③ 为什么调长 TTL 不是解法**：它只把尖峰**推迟**，峰值高度不变（`N ÷ 刷新窗口宽度`，与 TTL 只在「抖动窗口宽度 = 比例 × TTL」时才间接相关）；而且 TTL 变长会扩大泄露窗口与吊销延迟。**真正改变形状的是「抖动 + 合并 + 遵守退避」，不是 TTL。**

量级结论，非承诺值；见 §47.5 表与 §47.7 账一第 10–14 步。
</details>

**Q4.** 你要把一套用了 6 年的对象存储静态 AK 换成工作负载身份，但还有 200 个使用方没改。老板给了两条路：A. 直接把旧 AK 停掉，逼所有人迁移；B. 给旧 AK 加 90 天期限，到期再说。请用本篇的三本账判断，并给出第三条更好的路。

<details><summary>答案</summary>

**A（立刻停）**：`爆炸半径 = 使用方 × 漏改率 × 每方 QPS × MTTD`。200 个使用方 × 12 实例 = 2,400 个配置点，未改造方就是 100% 漏改 ⇒ 全量失败直到被发现（账三第 6–8 步的形状），且**长连接能活到连接关闭**，所以故障会分批出现、看起来像随机抖动，MTTD 会被拉得很长。

**B（加 90 天期限）**：失败被推迟到第 91 天，形状完全一样（甚至更糟：届时没人记得为什么坏），而且这 90 天里**旧 AK 仍然完全可用**——**暴露窗口没有缩短一分钟**，它只是被改名成「到期日」。

**第三条路**（推荐）：**先测量、再收紧、最后关闸**——
1. **让使用方必须上报凭据版本**：在网关/SDK 打点，把「还在用旧 AK 的调用方」列出来（这就是把账三的 `p` 从「未知」变成可测量）；这一步能直接决定后面所有动作；
2. **把静态 AK 换成「你自己签发的内部短期凭据 + 静态 AK 只作为 bootstrap」**：旧 AK 权限收窄到「只能换一次短期凭据」，于是即使泄露，可做的事也被限死（这是「网关代理内部凭据」挡单槽厂商那一招）；
3. **双活窗口按 `2 × 最长发布周期 + 回滚预算` 设**，并对未上报方逐周发清单、逐周收紧（每周把「旧 AK 允许的 API 集合」砍一块）；
4. **保留上一代，收尾放到下一轮**：真正的删除发生在下一轮轮转，且删除前必须有「连续一个发布周期 100% 流量走新凭据」的证据。

量级结论，非承诺值；见 §47.6 表与 §47.7 账三。
</details>

**Q5.** 一个同事说：「我们把 TTL 从 1 小时缩到 5 分钟，安全性提升 12 倍。」这句话在哪些地方是对的、哪些地方会带来新问题？请给出至少四个必须同时满足的前提。

<details><summary>答案</summary>

**对的部分**：泄露窗口从 ≤ 60 分钟压到 ≤ 5 分钟，`泄露量 = 单位时间导出能力 × 有效时长` 直接降 12×，这是真实的收益。

**新问题（三本账各一条）**：
1. **账一**：平均续租 QPS 从 `12,000/1,800 = 6.67` 涨到 `12,000/150 = 80` 次/s（12×），常态占用从 1.3% 涨到 16%；而**尖刺不变**（1,200 次/s，`N ÷ 桶宽`），所以如果没有抖动，缩 TTL 一点忙都帮不上。
2. **账一第 7 步**：`lease` 条目与签发/回收次数 ×12 ⇒ 目标库的 `CREATE USER`/`DROP USER` 也 ×12，元数据锁与 `Threads_running` 跟着 ×12。
3. **故障容忍**：`可用性 ≈ TTL − MTTR`。TTL 5 分钟意味着**任何超过 5 分钟的 Vault 不可用都会让全站凭据过期**——事故一的 22 分钟会从「48 分钟事故」变成「22 分钟即全挂」。

**必须同时满足的四个前提**：① 客户端已有**本地缓存 + 抖动 + single-flight**（顺序不能反，否则就是事故一的后 16 分钟）；② Vault 集群写能力有余量（账一第 8–9 步留 ≥ 3× headroom）；③ 目标库能承受 ×12 的 DDL（或改用「预建一组账号 + 池化分配」代替每次 `CREATE USER`）；④ unseal/auto-unseal 的 MTTR 明显小于 TTL（否则 TTL 越短越脆）。

**结论**：TTL 是一个**双刃旋钮**，正确的说法是「在客户端与基础设施满足前提后，把 TTL 压到 MTTR 的 1/2 以下」。

量级结论，非承诺值；见 §47.7 账一与 §47.8 决策表第 4 步。
</details>

---

## 47.13 延伸阅读（锚点式）

| 方向 | 去哪 |
| --- | --- |
| session / lease 的语义差异、心跳为什么不落盘（本篇 §47.3 只给结论） | [37 ZooKeeper vs etcd](37-zookeeper-vs-etcd.md) §37.5 |
| 「租约被单方面判定死亡」后旧主人还在写：fencing token 的完整推导 | [46 分布式锁与选主](46-distributed-lock-and-leader-election.md) §46.5、§46.6 |
| 选主撤权协议（凭据吊销与撤权是同一类问题：如何让旧资格失效） | [46 分布式锁与选主](46-distributed-lock-and-leader-election.md) §46.7 |
| 凭据切换时连接池重建、超时链与重连风暴 | [44 连接池与客户端](44-connection-pool-and-client.md) §44.5、§44.6 |
| 连接与会话的内存账（动态凭据的 `CREATE USER` 会打爆什么） | [44 连接池与客户端](44-connection-pool-and-client.md) §44.8 账一 |
| 写次数而非数据量：为什么「小而热」的写会打穿协调/元数据组件 | [37 ZooKeeper vs etcd](37-zookeeper-vs-etcd.md) §37.10 |
| K8s Secret / ConfigMap 的落盘与滚更、以及「配置推送风暴」 | → comp/38 注册中心与配置中心 §38.6 |
| 对象存储的鉴权模型、预签名 URL 与 STS 的使用边界 | → comp/42 对象存储与云盘 §42.5 |
| 事务组件里 in-doubt 分支占住连接（凭据回收与长事务的相互放大） | → comp/43 事务组件 §43.3 |
| 幂等键与「至少一次 × 幂等 = 恰好一次」（凭据重试的安全前提） | → eng/24 投递语义 §24.2 |
| LLM 网关侧的多供应商 key 管理、配额与成本归集 | → comp/50 LLM 网关与配额代理 §50.4 |
| 沙箱与执行面：不可信代码为什么必须拿「一次一租」的凭据 | → comp/51 执行面 §51.6 |
| 组件故障谱：「假活」与「满」在凭据系统里的具体信号 | → comp/52 组件故障谱 §52.4 |
| 多租户隔离与 crypto-shredding 的治理视角 | → llm/27 多租户隔离 |
| 本套写作纪律：八问卡、三本放大账、数字性质 | → comp/0 §0.4、§0.7 |
