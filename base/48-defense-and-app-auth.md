# 48. 网络攻防与应用层认证：一张「缺失的认证」总表

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 网络：从一根网线到一张证书 ｜ **编号**：48 / 56
> **本篇定位**：Part IV 的收口篇。39–47 逐个拆了链路层、IP、TCP、UDP、DNS、HTTP、TLS；本篇不再介绍新协议，只做一件事——**把「每个协议缺哪一样」压成一张可打印的总表，再把表里最后一格（应用层身份）补完**。全篇只有一条推论链——`认证 / 完整性 / 机密是三种独立的能力` → `每个协议只买了其中几样` → `缺哪样就长出一类攻击` → `防御 = 把缺的那一样补在该补的层` → `TLS 补全了通道的三样，但没有认证用户` → `所以 mTLS / OAuth2·OIDC / JWT / 签名防重放是这条链的最后一环`。
> **读法**：§48.2 的总表是全篇支点，建议打印出来贴在排障本第一页；§48.3–§48.5 是「攻击面 → 补法」的三组展开，可按自己系统暴露的层跳读；§48.7 占全篇约四成，是应用层认证的完整链路，赶时间也要读完 ①③④；§48.9 的三个实验都在本机真跑过，输出原样贴出；§48.11 的坑表可直接进评审清单。
> **不在本篇**：TLS 握手逐字段、密钥交换与证书链验证细节 → `base/47`；DNS 协议与 DNS 攻击的完整展开 → `base/44`、`base/45`；TCP 状态机、重传与拥塞控制 → `base/41`、`base/42`；限流算法与配额系统 → [eng/15](../eng/15-rate-limit-basics.md)；幂等键的存储与实现 → [eng/2](../eng/2-idempotency-implementations.md)；提示注入与内容安全 → [llm/30](../llm/30-injection-content-safety.md)；沙箱与加固 → [llm/32](../llm/32-defense-hardening.md)。**攻击脚本、提权步骤、绕过教程一律不写；本篇只讲机制、检测信号与防御落点。**

---

## 48.1 引子：TLS 全站绿锁，订单还是被刷了

先看一份脱敏后的事故时间线（金额与订单数按量级改写，链路与字段名保留原样）。这是一家电商的复盘记录，事故等级 P1。

| 时间 | 事件 | 当时的观测 |
| --- | --- | --- |
| T-30d | 全站切 TLS 1.3，开 HSTS `max-age=31536000; includeSubDomains; preload`，证书链评分 A+ | 安全团队认为「传输层已闭环」 |
| T+0 | 风控发现同一批收货地址在 6 小时内下单 1,842 笔，来自 37 个不同账号 | 每单都走了正常支付 |
| T+2h | 网关日志：所有请求都带 `Authorization: Bearer …`，签名校验全部通过 | 没有伪造签名，也没有解密流量 |
| T+3h | 抽查 20 枚 token，`sub` 各不相同，`iss` 都是自家 IdP，`exp` 都在有效期内 | 看起来「每个用户都在正常下单」 |
| T+4h | 发现这 20 枚 token 的 `aud` 全是 `svc-report`（报表服务），却被订单服务接受了 | 订单服务只校验「签名 + exp」，没校验 `aud` |
| T+6h | 攻击链还原：报表服务的一个只读 token 泄露在埋点 URL 里；因受众未校验，被拿去调用下单接口 | token 本身是真的，只是发错了对象 |
| T+8h | 修复：每个资源服务强制校验 `aud`/`iss`/`scope`；下单接口加 per-user 幂等键与二次风控 | 订单量回落 |

这条时间线里，TLS 没有一处是坏的：握手成功、证书有效、密文完整、HSTS 生效。事故仍然发生，因为 **TLS 认证的是「我连到的这台服务器是不是真的」，从来没有认证「这个请求是谁发的」**。服务器的证书证明它自己是 `order.example.com`；但请求头里那枚 token 证明的是「某个客户端在某时刻拿到过一份授权」——这两件事之间，隔着一整层应用层认证。

把这句话再压紧一点：

> **传输层的三样东西（认证、完整性、机密）是「通道」的属性；应用层要回答的是「身份」和「授权」的属性。通道修好了，不代表门禁修好了。**

本篇接下来先给总表（§48.2），再把表里的每一行拆开（§48.3–§48.6），最后用四成篇幅补最后一格：应用层认证（§48.7）。

---

## 48.2 总表：每个协议缺哪一样，就长哪一类攻击

先定义三个词，本篇全部用法都严格按这三个定义：

| 能力 | 一句话定义 | 判据（能问出 yes/no） |
| --- | --- | --- |
| **认证**（authentication） | 确认「对面是谁」或「这条消息是谁发的」 | 攻击者能不能在不持有密钥/身份的情况下，让接收方相信他是别人？ |
| **完整性**（integrity） | 确认「消息在途中一个 bit 都没被改」 | 攻击者改了内容后，接收方能不能发现？ |
| **机密性**（confidentiality） | 确认「只有预期的接收方能读懂」 | 路径上的旁听者能不能读出明文？ |

三种能力**互相独立**：有完整性不等于有机密性（DNSSEC 签名了，但答案仍是明文）；有机密性不等于有认证（匿名加密通道，谁都能进来）；有认证也不等于有完整性（认证了发送者，但消息可以被中间人改）。

下表按协议排，`✓` = 协议自带，`△` = 只覆盖一部分，`✗` = 天生没有。「补在哪一层」一栏是本篇的结论栏——**注意没有任何一格写「加防火墙」**，因为防火墙只减少暴露面，不补这三种能力。

| 层 | 协议 | 缺认证 | 缺完整性 | 缺机密 | 对应攻击 | 补在哪一层 | 补法的代价 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| L2 | ARP | ✗ | ✗ | ✗ | ARP 欺骗、同网段中间人、瞬断 | 交换机 DAI + DHCP snooping、静态绑定；上层 TLS 兜底 | 交换机特性与网段规划；静态表要人维护；跨网段无效 |
| L3 | IP | ✗（源地址可伪造） | ✗（首部校验和只查传输错误，可重算） | ✗ | 源地址伪造、反射放大、分片重叠 | 入口过滤 BCP38 / uRPF；IPsec AH/ESP | 要在源网络部署才有效；IPsec 加密钥运维与 MTU 开销 |
| L3 | ICMP | ✗ | ✗ | ✗ | 重定向攻击、不可达伪造、ICMP 隧道 | 类型白名单、禁 `redirect`、速率限制 | 会丢掉 PMTUD 诊断，排障变难 |
| L7 | DNS | ✗ | ✗ | ✗ | 缓存投毒、Kaminsky、劫持、隧道外带 | DNSSEC 补完整性；DoT/DoH 补机密；0x20 + 端口随机化 | 签名与轮转运维；加密后本地不可见；首包多一次 RTT |
| L4 | TCP | ✗ | ✗（校验和是弱校验，可重算） | ✗ | 序列号猜测注入、伪造 RST、SYN flood | TLS 补三样；ISN 随机化；RFC 5961 challenge ACK；SYN cookie | 握手 RTT；中间盒不兼容；on-path 攻击者仍可 RST |
| L4 | UDP | ✗ | ✗ | ✗ | 反射放大、源地址欺骗、DNS/NTP 滥用 | BCP38 + 关闭开放服务；RRL；QUIC 自带 TLS 1.3 | 无连接难做状态限速；QUIC 的 CPU 成本更高 |
| L7 | HTTP | ✗ | ✗ | ✗ | 窃听、篡改、会话劫持、CSRF | HTTPS + HSTS；Cookie 属性；CSRF token；CSP | 证书运维；混合内容；preload 不可逆 |
| L5–6 | TLS | △（默认只认证服务器，客户端证书可选） | ✓（AEAD） | ✓ | 证书链绕过、企业 MITM、降级、SNI 泄露、应用层重放 | 严格证书校验 / pinning / CT；TLS 1.3；mTLS；ECH | 中间盒可见性下降；pinning 要随证书轮转发版；ECH 部署门槛 |
| L7 | 应用层会话 / Token | △（取决于实现） | △（取决于签名） | △（依赖 TLS） | 越权、重放、token 泄露、JWT 混淆、CSRF | OAuth2/OIDC + PKCE；严格 JWT 校验；HMAC + 时间窗 + nonce；Cookie 属性；幂等键 | 每请求校验成本；密钥轮转；登录/同意页影响体验 |

这张表要能直接被打印，所以再给三条读法：

1. **先找「缺哪一样」，再找「补在哪一层」。** 看到「被中间人改了页面」，先问是 ARP/DNS/BGP/证书哪个入口；看到「越权下单」，先问应用层有没有认证「用户」；看到「打不开」，先分清是可用性问题还是完整性问题。
2. **补法的层越低，覆盖面越广，但代价越不可控。** BCP38 在运营商侧能一次性干掉大量反射放大，但你控制不了运营商；TLS 在端到端能一次补三样，但依赖证书校验不被跳过；应用层认证最灵活，也最容易写漏一个 `aud`。
3. **同一个攻击可以落在多行。** 「中间人」不是一个协议，是 ARP、DNS、BGP、证书链四个入口的集合（§48.3）；「DDoS」也不是一个协议，是带宽、协议状态、应用请求三层（§48.5）。

一句话收束这张表：**攻击不是「坏人很厉害」，是「协议把某一样能力留空了」；防御不是「再加一台设备」，是「把那一格填上，并接受它的价钱」。**

---

## 48.3 中间人的四个入口：ARP、DNS、BGP、证书链

「中间人」（MITM）听起来像一个动作，其实是四个不同层的入口。它们的共同点是：**攻击者先让「你以为的目的地」变成「他控制的目的地」**，然后才谈得上窃听或篡改。TLS 能挡住后面两步，但挡不住第一步——除非证书校验把「目的地不对」翻译成「握手失败」。

### ① ARP 欺骗：同网段里没有「谁是谁」，只有「谁回答得快」

**机制。** 基础 ARP 不验证地址绑定的密码学来源；主机通常维护邻居缓存，接受、更新或拒绝映射取决于实现和网络策略。错误映射可能改变同一广播域中流量的下一跳，导致流量被改道或连接中断。

**检测信号。** 同一 IP 的 MAC 频繁变化；一个 MAC 对应多个 IP；网关 MAC 突然变成某台主机的 MAC；`ip neigh` 里出现 `FAILED`/`INCOMPLETE` 反复跳变。交换机侧的 DHCP snooping 日志、`arpwatch` 类工具的「changed Ethernet address」告警都是同一类信号。

**防御。** 交换机开 DHCP snooping + 动态 ARP 检测（DAI），只允许 DHCP 租约绑定的 MAC/IP 组合通过；关键服务器做静态 ARP 绑定；端口隔离与 802.1X 把「谁能接入这个广播域」先收窄；最上层仍然靠 TLS——**ARP 被骗了，只要证书校验还在，攻击者拿到的只是一段解不开的密文**。

### ② DNS 劫持：一个「三缺」协议的必然结果

**机制。** DNS 查询只有 16 位事务 ID + 源端口提供「猜不中」的概率，应答本身没有签名，也没有加密（`base/44`、`base/45` 已展开）。攻击者可以在路径上抢先回一个伪造应答（ID 与端口猜中即可），也可以投毒递归解析器的缓存，还可以直接改本机 `hosts`、路由器 DNS、DHCP 下发的 DNS 地址。DNS 劫持的杀伤力在于：它不碰你的服务器，只把「域名 → IP」这一步换掉。

**检测信号。** 解析结果与权威服务器不一致；同一域名从不同 resolver 得到不同答案；TTL 异常短或异常长；DNSSEC 验证失败（`SERVFAIL`）；证书 SAN 与预期域名不符（这已经是最后一道防线在报警）。

**防御。** DNSSEC 补完整性（签名 RRset，但不加密）；DoT/DoH 补机密性（把查询塞进 TLS，但把信任转移给了 resolver）；0x20 编码与端口随机化提高猜中成本；客户端严格校验证书是兜底——**DNS 被劫持到错误 IP 后，攻击者拿不出 `example.com` 的有效证书，TLS 会先断**。

### ③ BGP 劫持：互联网的「路由」本身没有认证

**机制。** BGP 的核心动作是「我（AS X）可以到达前缀 P」。这个宣告默认被相信：没有密码学签名，也没有「这个前缀真的属于你」的强制校验。于是更具体的前缀宣告（/24 比 /16 更具体）可以吸走流量，或者一条错误配置的路由可以泄漏到全世界。BGP 劫持的规模可以大到国家级，也可以小到「某个 /24 被邻居配错」。

**检测信号。** RPKI 校验为 `Invalid`（起源 AS 与 ROA 不符）；路由监控平台（RouteViews、RIPE RIS 一类）出现前缀起源变更；`traceroute` 在境外突然绕行；同一前缀的 AS_PATH 异常变长或变短。

**防御。** 发布 ROA（Route Origin Authorization），让邻域做 ROV（Route Origin Validation）丢弃 `Invalid`；前缀过滤与 `max-prefix` 限制；RPKI 只验证「起源」，不验证「整条路径」，路径级防护（BGPsec）部署率仍很低。**对应用来说，BGP 劫持的最终兜底仍是 TLS 证书校验**：流量被吸走，攻击者也伪造不了证书。

### ④ 证书链：唯一一个「补丁本身可以被合法拆掉」的入口

**机制。** TLS 的身份认证完全依赖「终端信任哪些根 CA」。企业 MITM 的合法形态是：管理员把企业根 CA 装进员工电脑/手机的信任库，防火墙用这张根 CA 给每个站点现签一张证书，于是「解密—检查—重加密」在终端看来完全合法。恶意形态则是：攻击者把根 CA 装进受害者设备，或利用某个被信任但校验宽松的 CA。**这两种形态在协议层无法区分**——它们都通过了链验证。

**检测信号。** 证书 issuer 是企业内部 CA 而不是公共 CA；证书链里出现「组织名 = 自己公司」的中间 CA；Certificate Transparency 日志里查不到这张证书的 SCT；应用层 pinning 报「证书指纹不匹配」；同一个域名在不同网络下证书 issuer 不同。

**防御。** 应用层 pinning（把证书或公钥指纹写进客户端，代价是轮转要发版）；CT 监控（证书必须出现在公开日志里，私有 CA 签的不会）；企业环境里把「解密白名单」收窄到明确域名，并对私有根 CA 做审计；服务间用 mTLS 把「谁可以连」从「网络位置」改成「证书身份」（§48.7②）。**证书链是 TLS 唯一的信任根，根被换掉，前面两样（完整性、机密性）就一起归零**——这正是 `base/47` 的结论。

四个入口可以用一句话串起来：**ARP 骗的是「同一网段里的下一跳」，DNS 骗的是「域名到 IP」，BGP 骗的是「IP 到 AS 的路径」，证书链骗的是「这个公钥属于这个域名」。前三个都能被第四个兜住，第四个只能靠 pinning/CT/私有 CA 审计兜。**

---

## 48.4 TCP 注入与 RST：序列号是「约定」，不是「凭证」

TCP 序列号为可靠交付服务，不是消息来源的密码学凭证。异常重置可能来自远端进程或沿途设备；仅靠应用报出的 `ECONNRESET` 不能断定是谁发出。对比受管理的客户端、服务端与代理日志，必要时在授权的观察点对齐同一连接的时间线。观察到校验和正确的报文，不代表它来自预期的程序。

**防御的三层。**

| 层 | 措施 | 挡什么 | 代价 |
| --- | --- | --- | --- |
| TCP | ISN 随机化（RFC 6528），用四元组 + 密钥 + 时钟做哈希 | 盲注入、盲 RST 的猜测空间 | 无额外 RTT；实现要对时钟与密钥做保护 |
| TCP | RFC 5961：在适用的已同步状态中，窗口内但序号不精确匹配的 RST 可触发 challenge ACK；窗口外的丢弃 | 缩小对盲 RST 的接受范围 | 取决于接收实现及状态；不提供密码学身份 |
| 传输层以上 | TLS 记录层 MAC/AEAD；QUIC 的包号与加密 | 注入的伪段无法构成合法记录，内容进不了应用 | 握手 RTT；仍挡不住 on-path 的 RST（可用性攻击） |

关键结论要说得不含糊：**TLS 能让「注入的数据」失效，但不能让「连接不被重置」**。前者是完整性问题，后者是可用性问题。把这两件事混为一谈，就会在事故复盘里写出「上了 TLS 怎么还会断」这种错判。

---

## 48.5 DDoS 三分类：带宽、协议状态、应用请求

DDoS 不是一种攻击，是三种资源被同一种方式耗尽。分类的意义在于：**每一类的防御落点完全不同**，用错层就是白花钱。

| 类别 | 耗尽的是什么 | 典型形态 | 防御落点 | 关键判据 |
| --- | --- | --- | --- | --- |
| **volumetric**（流量型） | 链路带宽 / 清洗容量 | UDP 反射放大、直接洪水 | 上游清洗、anycast、源地址过滤 | 入向 bps/pps 是否超过链路容量 |
| **protocol**（协议型） | 连接表 / 半开队列 / CPU | SYN flood、ACK flood、分片洪水 | SYN cookie、backlog 调优、SYN 代理 | `SYN_RECV` 数量、半开队列是否满 |
| **application**（应用型） | 工作线程 / 数据库 / 业务配额 | CC 攻击、慢速攻击、请求洪水 | L7 限流、超时、并发上限、WAF | QPS 与每请求成本，而非单纯 bps |

### 反射放大：把「源地址可伪造」变成杠杆

反射放大需要三个条件同时成立：**UDP 无连接**（不需要握手，伪造源地址就能发）、**响应比请求大**、**服务对公网开放**。攻击者伪造受害者的源 IP 发一个小请求，放大后的响应全部砸向受害者。

| 防御对象 | 优先核对 |
| --- | --- |
| DNS | 递归客户端 ACL、响应限速、权威服务的实际吞吐和错误率 |
| 时间同步与设备发现服务 | 是否必须对公网开放、版本以及访问控制是否符合资产基线 |
| 缓存与目录服务 | 不必要的无连接监听、暴露范围、服务端的响应预算 |

放大比必须在同一层、同一计量口径下用响应字节除以请求字节。历史记录中的倍率不能作为本系统的容量保证；观察到链路饱和，应先区分直达流量、放大流量与正常业务，再据受管理的服务端数据评估响应上限。

防御只有三条真正有效：**在源网络做 BCP38 入口过滤**（让伪造源地址出不去）、**关闭/限制开放反射服务**（memcached 不监听 UDP、NTP 关 monlist、DNS 开 RRL）、**上游 anycast + 清洗**（把流量分散并丢弃异常部分）。三条都不便宜，但缺一条，另外两条的效果都会打折。

### SYN flood：半开队列是一个「必须先分配」的账

TCP 三次握手要求服务器在收到 SYN 后先分配一个传输控制块（TCB）并放进半开队列，等对方的 ACK 来了再移到 accept 队列。这个「先分配」就是攻击面：攻击者发大量伪造源 IP 的 SYN，服务器为每个 SYN 占一份内存，直到超时。

算一笔账：半开队列上限 `tcp_max_syn_backlog = 4096`，攻击者每秒发 10,000 个 SYN，服务器等 ACK 的超时约 63 秒，则需要容纳 `10,000 × 63 = 630,000` 个半开连接——是上限的 150 倍。队列一满，新 SYN 被丢弃，正常用户连不上。

**SYN cookie 的机制**：不在 SYN 时分配状态，而是把「四元组 + 时间 + 一个秘密」哈希成 24 位左右的值，塞进 SYN-ACK 的序列号；只有收到合法 ACK（序列号回显正确）才分配 TCB。代价是序列号里能编码的选项空间有限（MSS 只能选几档，窗口缩放/时间戳等选项可能丢失），所以它是「过载时的降级模式」，不是默认最优。`net.ipv4.tcp_syncookies=1` 在现代内核里通常是默认，但队列长度与 `somaxconn` 仍要按业务调。

### 应用层（L7）：限流的落点比算法更重要

CC 攻击和慢速攻击的共同点是：**每个请求都很便宜，但乘上并发就不便宜**。慢速攻击（Slowloris 一类）用极慢的头部/正文发送占住连接；CC 用大量「看起来合法」的完整请求打穿到应用。防御不在网络层，因为包本身完全合法。

落点要分三层：

- **边缘（CDN/WAF）**：按 IP/ASN/地理做粗粒度限流，挡掉明显的机器人；对慢速连接设「头部/正文超时 + 最小速率」。
- **网关（认证之后）**：按 `sub`/`tenant`/`scope` 做令牌桶，因为 NAT 后面可能是几千个用户，按 IP 限流要么误伤要么形同虚设（§48.11 会展开）。
- **服务（业务层）**：按接口成本做并发上限与队列，队列满了就快速失败（shed load），不要让它堆到数据库。

限流算法本身（令牌桶、漏桶、滑动窗口、分布式配额）在 [eng/15](../eng/15-rate-limit-basics.md)；TCP 侧的队列与拥塞行为在 `base/42`。本篇只强调一句：**DDoS 防御的成败不在「峰值扛住」，在「峰值时还能不能让正常用户完成关键路径」**。

---

## 48.6 中间盒与流量分析：加密了，为什么还是能被识别

**先区分观察点。** 路径上的被动观察者、能影响传输的中间盒和终止 TLS 的受管代理，看到的不是同一份信息。普通 TLS 1.3 保护服务器证书消息和 HTTP 明文；路径仍可能看到 IP、端口、数据报或记录长度、时序，以及未受 ECH 保护的握手字段。知道某个字段没有被保密，不等于知道它已经被哪台设备用于拦截。

| 特征 | 在哪里可见 | 加密后还可见吗 | 能支持什么判断 | 不能证明什么 |
| --- | --- | --- | --- | --- |
| SNI | 未受 ECH 保护的 ClientHello；QUIC Initial 需按协议恢复握手字节 | 普通 TLS 可见；ECH 成功时内层 SNI 不对路径公开 | 客户端在该握手里宣告的名称 | 证书已通过验证、用户访问了哪个 URL，或连接确实被按该名称阻断 |
| 证书链 | 参与会话的端点；普通 TLS 1.3 的 `Certificate` 受握手密钥保护 | 被动点不能直接读取证书内容 | 从授权端点核对信任链与名称 | 仅凭密文记录长度知道链里有几张证书 |
| 版本、套件和扩展 | 观察位置可见的 ClientHello 字段 | 视协议阶段与 ECH 而定 | 对照已知客户端样本、定位协商差异 | 唯一识别程序版本、用户或设备 |
| 长度、时序与流量 | 包头与观察点时间戳 | 外层元数据仍可见 | 判断停在哪个阶段、建立异常基线 | 直接读出应用明文、按趋势给身份定性 |

**JA3/JA4 是什么。** 它们用可见握手字段构造特征，用于比对与分组，不是客户端身份证明。不同程序可能得出相同值，同一程序也可能因配置或版本变化得出不同值。下面是**格式示意**（不是某台真机的实测值）：

```
JA3  = MD5("771,4865-4866-4867,0-23-65281-10-11-35,29-23-24,0")
JA4  = "t13d1516h2_8daaf6152771_02713d6af862"
        └┬┘└┬┘└┬┘└┬┘  └─────┬─────┘ └─────┬─────┘
         │  │  │  └ ALPN=h2    密码套件哈希      扩展哈希
         │  │  └ 扩展个数
         │  └ 密码套件个数
         └ TLS1.3 / 域名形态 / 流式
```

`User-Agent` 与握手指纹都不能代替客户端认证。即使风险评分使用这些特征，访问决策也要依据独立凭据和授权策略；关联不等于身份。

加密后的包仍可能暴露观察点能看见的端点、长度与时间；这不等于能够识别用户或业务操作。[49](49-case-vless-reality-vision.md) 讨论中继前后各自的信任与加密边界。[50](50-case-tls-http2-capture.md) 则区分被动记录与端点解密后的帧。

### SNI 阻断：能读到什么，在哪一步拒绝？

SNI（[RFC 6066 §3](https://www.rfc-editor.org/rfc/rfc6066#section-3)）是客户端在 TLS 握手中声明的服务器名；服务端可以用它选择证书或虚拟主机，路径设备也可能据可见名称执行连接策略。这里要分开两条因果：**读到 SNI 不需要先解密 HTTP；根据 SNI 拒绝连接，也不代表设备知道请求路径或已认证访问者。** 同一个 IP 可服务多个名字，因此「能连到这个 IP」与「这个名字的握手能完成」是不同结论。

| 问题发生在哪 | 可能看到的现象 | 在自己管理的环境中怎么核对 |
| --- | --- | --- |
| DNS 解析或策略 | 预期名字未得到可用地址，或解析结果与服务记录不一致 | 对照应用使用的解析器、期望的权威记录与解析日志；先别把解析错误叫 SNI 阻断 |
| IP / TCP / UDP 路径 | TCP 未完成建连，或 UDP 无响应 | 查目标监听、路由、ACL 与两端计数；UDP 无响应也可能是丢包或未监听 |
| ClientHello 与名称策略 | TCP 已建连，握手在发出 ClientHello 后超时、重置或拒绝 | 对照同一次请求的客户端、授权代理与入口日志；同一测试入口上用自有测试域名作对照，并保持目标地址、传输和客户端配置一致。只有拒绝日志或多点证据支持，才能归因到该设备的名称规则 |
| 证书与协议协商 | 显示证书链错误、名字不匹配或 ALPN 失败 | 分开检查期望服务名、服务端返回的证书和协商结果；同 IP 不同名字时，默认虚拟主机也可能产生这类差异 |
| HTTP 策略或业务授权 | 在已建立的 TLS 通道中读到 `403` 等应用响应 | 查 HTTP 网关与服务授权日志。收到了 `403` 不等于 TCP/TLS 被阻断 |

举一个**诊断推演**：两个人都说“访问超时”，A 的 TCP 握手没完成，B 的 TCP 已通但 ClientHello 后无响应。前者首先查传输路径，后者再检查 TLS 入口与名称策略。**B 的现象仍不足以坐实 SNI 过滤**：入口故障、ClientHello 分段后的丢包、路径 MTU、服务器不接受该名字，都可能给出相似的客户端症状。单侧抓包看到 RST 也不能单独证明由谁发出。若客户端尝试了 h3 后转为 h2，则要对这两个连接分别查证，不能把它们合并成一次握手。

**QUIC 并不会天然隐藏 SNI。** HTTP/3 建在 QUIC 上；有足够初始报文的路径观察者可以按公开规则读取 QUIC Initial 中承载的普通 ClientHello。不应把「UDP 载荷受包保护」误读为「初始服务名已对路径保密」。[ECH（RFC 9849）](https://www.rfc-editor.org/rfc/rfc9849) 成功保护的是内层 ClientHello 的敏感字段；外层公开名、目标 IP、报文尺寸与时序仍可能暴露，明文 DNS 在另一观察点也可能显示原域名。ECH 不保证可达，也不是全面匿名。对受管网络而言，应明确规则依据的是地址、可见外层信息还是受管端点的审计，而不是把缺失的内层名称判为恶意。

**怎样验证隐私保护真的发生？** 先核对客户端到所选解析器的 DNS 查询走了什么传输，再看授权端点是否记录真实 ECH 已被接受，以及证书是否匹配最终的服务名。三者分别回答「DNS 问题是否在这一跳暴露」「握手目标名是否以普通 SNI 出现」「是否连到了正确端点」。握手扩展出现并不等于 ECH 成功；只看到外层公开名也不足以推断目标名。内外层与重试的机制见 [47 §47.8](47-tls-and-certificates.md)，HTTP/3 的端点观测见 [50 §50.11](50-case-tls-http2-capture.md)（该实验不启用 ECH）。

---

## 48.7 应用层认证：通道对端、应用用户与资源权限是三件事

这一节是全篇的重点。前面所有层补的都是「通道」；这一节补的是「身份」。

### ① 起点：从通道身份到请求身份

在常见的单向认证 HTTPS 中，**客户端**校验服务器证书与期望主机名。TLS 还会协商密钥并验证握手，随后保护记录；不能把这些步骤概括成“只验一次证书”。服务端此时知道自己接受了一条连接，但 IP、端口与时间不能确认业务主体，地址也可能由多个用户共享。代理终止 TLS 时，验证边界在终止点；代理与上游是否另建受保护通道须单独判断。

服务端可用 mTLS 认证证书主体，或使用经过验证的会话、令牌或应用签名识别请求主体。**认证通过后仍须授权**：同一个服务身份是否可以操作这个租户的这个对象，得由资源服务按策略判断。

### ② mTLS：把「谁可以连」从网络位置改成证书身份

mTLS（mutual TLS）就是在服务器证书之外，再让客户端出示一张证书。它适合**服务间**通信：服务 A 调服务 B，B 不需要知道「A 的 IP 是多少」，只需要知道「A 的证书是由信任的 CA 签的，且 SAN 是 `spiffe://prod/ns/pay/sa/order`」。

握手层面的动作：

1. 服务器在 `CertificateRequest` 里列出可接受的 CA；
2. 客户端发 `Certificate`（自己的证书链）+ `CertificateVerify`（用私钥对握手 transcript 签名）；
3. 服务器验证链、有效期、EKU（必须是 `clientAuth`）、SAN/身份，然后才继续。

一张客户端证书的生命周期，比「签出来就完事」长得多：

| 阶段 | 做法 | 代价 / 坑 |
| --- | --- | --- |
| 签发 | CSR → CA 签名；SAN 写 SPIFFE ID 或服务名；EKU 必须含 `clientAuth` | CA 的私钥是最高价值目标；签发接口要强认证 |
| 分发 | 通过密钥管理/Secret 挂载/工作负载身份注入 | 落到磁盘就是长期泄露面；要限权限、限读取 |
| 轮转 | 短有效期（小时级）+ 自动续期；新旧双证书重叠窗口 | 时钟不同步会导致「刚签就过期」；轮转失败要有告警 |
| 吊销 | CRL / OCSP / 短有效期自然过期 | CRL 体积大且滞后；OCSP 有隐私与可用性问题；**短有效期是最实用的吊销** |
| 身份 | SPIFFE：`spiffe://trust-domain/ns/<ns>/sa/<sa>`，由工作负载 attested | 需要平台侧做 attestation；跨集群要统一 trust domain |

**为什么用 URI SAN 而不是 CN。** CN 是自由文本，重名、编码、大小写都能做文章；SPIFFE 把身份写成结构化 URI：`spiffe://example.org/ns/pay/sa/order`，trust domain、namespace、service account 三段都可被策略化。工作负载启动时由平台做 attestation（证明「我是这个 namespace 里的这个 SA」），再签发短有效期 SVID。这样「证书里的身份」不依赖 IP、不依赖 DNS 名，也不依赖人手填的字符串。

**三种「服务间认证」的取舍：**

| 方式 | 认证的是什么 | 撤销粒度 | 适合 | 主要代价 |
| --- | --- | --- | --- | --- |
| API key | 一个共享字符串 | 换 key（全量） | 低风险内部工具 | 泄露即长期有效；无法区分调用者 |
| mTLS | 证书身份（SAN） | 单张证书 / 短 TTL | 服务网格、数据库、B2B | CA 与轮转运维 |
| OAuth2 client credentials | client_id + secret → token | 单个 client / scope | 跨域服务调用、需要 scope | 多一次 token 请求；依赖 IdP |

**适合谁：** 服务网格内的东西向流量、数据库/消息队列的客户端认证、B2B 专线、需要「不依赖网络位置」的零信任入口。

**不适合谁：** 面向普通用户的 Web 登录（证书分发与丢失成本太高）、需要「用户同意/撤销授权」的场景（证书不表达授权范围）。

**最大的坑：** 「开了 mTLS，但信任所有 CA」或「只检查有没有证书，不检查 SAN」。前者等于任何人拿一张自签证书都能进；后者等于「有身份证就能进，不看是谁」。mTLS 的价值全在**校验链 + 校验身份**这两步。

### ③ OAuth2 / OIDC：把「授权」从「密码」里拆出来

用户登录这件事，最原始的做法是「把密码给每个服务」。OAuth2 把它拆成四个角色：

| 角色 | 是谁 | 在本篇事故里 |
| --- | --- | --- |
| Resource Owner | 用户 | 被盗刷的买家 |
| Client | 想访问资源的应用 | 前端/App/报表服务 |
| Authorization Server（IdP） | 发 token 的一方 | 自家 IdP |
| Resource Server | 持有资源的 API | 订单服务、报表服务 |

OIDC 是在 OAuth2 之上加了一层「身份」：多了 ID Token（JWT，描述「谁登录了」）、UserInfo 端点、发现文档（`/.well-known/openid-configuration`）。**OAuth2 管授权（能做什么），OIDC 管认证（你是谁）**，两者经常一起用，但不是一回事。

**授权码 + PKCE 的时序**（这是目前公共客户端的标准姿势）：

```
用户        Client(浏览器/App)        Authorization Server          Resource Server
 |                |                          |                          |
 |-- 点击登录 --->|                          |                          |
 |                |-- ① /authorize?response_type=code&client_id=...     |
 |                |     &redirect_uri=...&scope=order:read             |
 |                |     &state=<随机>&code_challenge=<S256(verifier)>  |
 |                |----------------------------------------->|          |
 |<---------------|  ② 登录 + 同意页                        |          |
 |-- 同意 ------->|----------------------------------------->|          |
 |                |<-- ③ 302 redirect_uri?code=...&state=... |          |
 |                |-- ④ POST /token: code + code_verifier ----------->|
 |                |<-- ⑤ access_token / refresh_token / id_token -----|
 |                |-- ⑥ GET /orders  Authorization: Bearer access --->|
 |                |<-- ⑦ 200（资源服务器校验 token）------------------|
```

四个细节，每一个都是事故来源：

- **`state`**：防 CSRF。登录请求里带一个随机值，回调时比对；不带就可能被「登录 CSRF」——攻击者把自己的授权码塞给受害者，受害者登录后绑到攻击者账号。
- **PKCE**：公共客户端（SPA、移动 App）存不住 `client_secret`，授权码又可能从浏览器历史、Referer、日志、自定义 scheme 里泄露。PKCE 让 Client 先生成 `code_verifier`，把 `S256(verifier)` 放进授权请求；换 token 时必须出示原像。攻击者偷到 code 但偷不到 verifier，换不出 token。**这不是可选项，OAuth 2.1 已把它变成所有客户端的默认要求**。
- **`redirect_uri` 精确匹配**：不能通配。否则攻击者可以把 code 引到自己的域名。
- **ID Token 不是 access token**：ID Token 是给 Client 看「用户是谁」的，`aud` 是 Client ID；拿去调 API 会被资源服务器拒绝（或者更糟：资源服务器不校验 `aud`，于是它被当成万能 token——这正是 §48.1 的事故形态）。

Token 的分工：

| Token | 谁消费 | 生命周期 | 关键校验 |
| --- | --- | --- | --- |
| access token | Resource Server | 分钟级（5–60 min） | 签名、`iss`、`aud` = 本服务、`exp`、`scope` |
| refresh token | Authorization Server | 天/月级，必须轮转 | 绑定 client、一次性使用、重用检测（reuse detection） |
| ID token | Client | 分钟级 | 签名、`iss`、`aud` = client_id、`nonce`、`exp` |

**四种流程怎么选：**

| 流程 | 谁用 | 要不要 secret | 关键点 |
| --- | --- | --- | --- |
| authorization code + PKCE | Web / SPA / 移动 App | 公共客户端不需要 | 默认选择；PKCE 必开 |
| client credentials | 服务间 | 需要 | 没有用户，只有 client 身份 |
| device code | 电视 / CLI | 不需要 | 用户在另一台设备上确认 |
| implicit | 历史遗留 | 不需要 | 已废弃：token 直接出现在 URL 与历史里 |

**token 校验放在哪。** 资源服务器要么本地验签（缓存 IdP 的 JWKS），要么调 introspection 端点（多一次 RTT，但能拿到撤销状态）。前者的关键是 JWKS 轮转：IdP 换钥匙时 `kid` 会变，缓存必须能「按 `kid` 未命中时主动刷新」，否则要么验不过、要么一直用旧 key；后者适合高风险操作，不适合每个请求。

**scope 是粗粒度授权，不是对象级权限。** `order:read` 只说明「能读订单」，不说明「能读哪一笔订单」。资源服务器在 scope 之后还要做「这个 `sub` 是否有权访问这个 `order_id`」的判断——这一步经常被漏掉，表现为「有 token 就能改别人订单」。

### ④ JWT：三段结构、必须校验的字段、三个经典坑

JWT 是「签名的 JSON」，不是「加密的 JSON」。它的结构是：

```
base64url(header) . base64url(payload) . base64url(signature)
      {"alg":"HS256",     {"sub":"user-42",        HMAC-SHA256(
       "typ":"JWT"}        "aud":"svc-order",       base64url(header)+"."+base64url(payload),
                           "exp":1795100599}       key)
```

**base64url 是编码，不是加密。** 任何人拿到 token 都能解开前两段。真实输出（本机 PyJWT 生成后直接解）：

```
header  = {"alg":"HS256","typ":"JWT"}
payload = {"iss":"https://id.demo","aud":"svc-billing","sub":"user-42","scope":"order:read","exp":1795099999}
sig 前 16 字符 = C0jt49uLESiAmRyp
```

所以：**JWT 里不放密码、身份证号、内部密钥；它只放「可以被公开读、但不能被篡改」的声明。** 机密性由 TLS 提供，完整性由签名提供。

必须校验的字段，一个都不能少：

| 字段 | 校验什么 | 漏了会怎样 |
| --- | --- | --- |
| `alg` | 必须是**服务端白名单**里的固定算法，不能信 header 自报 | `none` 算法、HS/RS 混淆（下面三个坑） |
| `iss` | 必须**精确等于**预期 issuer（不是前缀匹配、不是包含） | 别的 IdP 签的 token 被接受 |
| `aud` | 必须**包含本服务**（字符串或数组） | 发给报表服务的 token 能调订单服务（§48.1） |
| `exp` / `nbf` | 当前时间在区间内；允许 ≤60 s 时钟偏移 | 过期 token 永久有效，或刚签就失效 |
| `iat` | 不能在未来太远；可用于「签发时间早于改密时间」的判断 | 旧 token 在改密后仍可用 |
| `kid` | 只能解析到**受信任的密钥集**里的 key | 攻击者用 `jku`/`x5u` 注入自己的公钥 |
| `sub` + `scope` | 认证之后还要做**对象级授权** | 有 token 就能读别人的订单 |

**三个经典坑，每一个都能单独写成事故：**

**坑一：错把算法声明当信任策略。** 需要签名的 token 不得接受无签名算法。验证方按受信签发者、用途及密钥类型配置算法允许列表，不从不可信 header 自行扩大允许集合。以下是文档已有的拒绝结果（PyJWT 2.6.0，固定 `algorithms=["HS256"]`）：

```
none token 段数 = 3 签名段长度 = 0
algorithms=['HS256']: 拒绝 —— InvalidAlgorithmError
```

**坑二：HS256 与 RS256 算法混淆。** RS256 使用非对称签名，HS256 使用共享密钥认证，两类密钥不可互用。防御测试要断言受信签发者的算法和密钥类型不匹配时拒绝，并按各自用途管理密钥及轮转策略。

**坑三：把 JWT 当 session 用。** JWT 的卖点是「无状态」，代价是**签发后在 `exp` 之前无法撤销**。用户点了「退出所有设备」、密码被改、账号被封，旧 token 仍然能用到过期。工程上的补法只有三条（通常组合）：把 access token 有效期压到 5–15 分钟；用 refresh token 轮转 + 重用检测；在资源服务器侧维护一个「token 版本/`jti` 黑名单」，撤销时改版本或写黑名单。**「无状态」不是免费的，它把「撤销」这件事从数据库搬到了过期时间上。**

验签的顺序也有讲究（先廉价后昂贵）：

```python
def verify(tok, jwks, now):
    h = json.loads(b64url_decode(tok.split(".")[0]))
    if h["alg"] not in {"RS256"}:                  # ① 先钉死算法
        raise Reject("alg")
    key = jwks.get(h.get("kid"))                   # ② 只从受信 JWKS 取 key
    if key is None:
        raise Reject("kid")
    claims = jwt.decode(tok, key, algorithms=["RS256"],   # ③ 再验签
                        audience="svc-order", issuer="https://id.demo",
                        options={"require": ["exp", "iat", "aud", "iss"]})
    if not (now - 60 <= claims["iat"] <= now + 60):       # ④ 再查时间语义
        raise Reject("iat")
    return claims
```

顺序的意义：`alg` 与 `kid` 是纯字符串比较，先做可以避免把任意 token 送进昂贵的非对称验签；`aud`/`iss` 放在验签之后，是因为**未验签的 claim 不可信**——先信 claim 再验签，等于让攻击者用伪造的 `kid` 触发任意 key 加载。

**钥匙本身怎么管。** HS256 的 secret 至少 32 字节随机（不是口令），只放在 KMS/Secret 里；RS256/ES256 的私钥放 HSM/KMS，JWKS 只暴露公钥。轮转用 `kid` 做灰度：新 key 先进入 JWKS 并开始签发，旧 key 保留至少一个 token 生命周期再移除。反过来，`kid` 不能直接拼进文件路径或 URL（防路径穿越），也不能让 token 里的 `jku`/`x5u` 决定去哪取 key（SSRF + 信任注入）。

### ⑤ 签名与防重放：把「一次请求」变成「一次性的请求」

mTLS/OAuth2/JWT 解决「谁在调用」，但不解决「这同一个请求被重放 100 次」。支付、下单、改密码这类接口必须再加一层：**签名 + 时间窗 + nonce + 幂等键**。

一个可落地的签名串（服务间 HTTPS 之外再加一层）：

```
CANONICAL =
  method        + "\n" +      GET
  path          + "\n" +      /v1/orders
  sorted_query  + "\n" +      amount=100&currency=CNY&user=42
  timestamp     + "\n" +      1795100000
  nonce         + "\n" +      9f2c...（每次请求唯一）
  sha256(body)                e3b0c442...（空 body 也要算）
SIGNATURE = hex(HMAC-SHA256(secret, CANONICAL))
```

服务端校验顺序：时间戳在 ±300 s 内 → nonce 未见过（缓存 TTL ≥ 2×时间窗） → 重算签名一致 → 业务幂等键未处理过。四个都过，才执行。

**为什么必须「规范化」**：签名是把字节串哈希，两边只要有一个字节不同就对不上。常见的不一致来源：

| 不一致来源 | 例子 | 规范做法 |
| --- | --- | --- |
| 参数顺序 | `a=1&b=2` vs `b=2&a=1` | 按 key 的字节序排序 |
| 编码 | 空格写成 `%20` vs `+`；`~` 是否转义 | 统一 RFC 3986，固定大小写 |
| 数字格式 | `1` vs `1.0` vs `1e0` | 协议层固定为字符串或整数 |
| JSON 空白/键序 | `{"a":1,"b":2}` vs `{"b":2,"a":1}` | 固定序列化器（JCS 一类） |
| 数组顺序 | `[1,2]` vs `[2,1]` | 明确「有序」还是「按值排序」 |
| 头字段 | `Content-Type` 大小写、多个空格 | 头名小写、值 trim、只签白名单头 |
| 签名自身 | 把 `signature` 字段也算进去 | 先剔除签名字段，再规范化 |

**nonce 与时间窗是一对。** 只验时间窗不验 nonce，攻击者可以在 5 分钟内重放；只验 nonce 不验时间窗，nonce 缓存要无限大。两者一起，缓存只需要保存「最近 2×窗口内见过的 nonce」，过期即可删。

算一笔 nonce 缓存的账：10,000 QPS、时间窗 ±300 s（总跨度 600 s），需要保存的 nonce 上限是 `10⁴ × 600 = 6×10⁶` 个；按每个 32 B key + 16 B 元数据算，约 288 MB。单机 Redis 放得下，但要注意：**缓存被清空 = 重放窗口重新打开**，所以要么持久化，要么在清空时把时间窗收紧。高 QPS 场景常用「时间分片 + 每片一个 Bloom filter 前置，再用精确集合兜底」，代价是极小的误判率与更复杂的运维。

**幂等键解决的是另一件事**：客户端重试（超时、断网、LB 重发）不应该产生第二笔订单。幂等键由客户端生成（UUID），服务端把「key → 首次响应」存一段时间（比如 24 h），重复请求直接返回首次结果。它防的是「无意重试」，nonce 防的是「恶意重放」；两者不能互相替代。实现细节（存储、并发、过期、和事务的关系）在 [eng/2](../eng/2-idempotency-implementations.md)。

### ⑥ 会话与 Cookie：浏览器里那一点点「身份」

浏览器场景下，身份通常放在 Cookie 里。三个属性是底线：

| 属性 | 作用 | 不设的后果 | 代价 |
| --- | --- | --- | --- |
| `HttpOnly` | JS 读不到（`document.cookie`） | XSS 一打一个准，session 直接被偷走 | 前端不能再用 JS 读它（本来也不该） |
| `Secure` | 只在 HTTPS 上发送 | 一次 HTTP 请求就泄露 | 本地 HTTP 调试要另开环境 |
| `SameSite` | 跨站请求是否带 Cookie | CSRF 面变大 | `Strict` 影响外链跳转后的登录态；`None` 必须配 `Secure` |

**CSRF 与 SameSite 的关系要说清。** CSRF 的本质是「浏览器会自动带上目标站点的 Cookie」，攻击者不需要读到 Cookie，只需要让受害者的浏览器发一个请求。`SameSite=Lax` 会阻止跨站的 POST 带 Cookie，但**允许顶级导航的 GET**——所以「用 GET 做状态变更」的接口仍然能被 CSRF。正确姿势是三者叠加：状态变更只用 POST/PUT/DELETE；`SameSite=Lax` 起步；再加同步器 token（session 里存一个随机值，表单/头里带回）或双提交 Cookie；最后用 `Origin`/`Referer` 做兜底校验。

**会话固定（session fixation）**：攻击者先拿到一个 session ID，想办法让受害者用它登录；受害者登录后，服务器如果没有换 ID，攻击者手里那个 ID 就成了「已登录会话」。修复只有一条：**在任何权限变化（登录、提权、切换租户）时重新生成 session ID**，并且从不接受 URL 里的 session ID。

**轮转与超时**：空闲超时（如 30 分钟）+ 绝对超时（如 12 小时）；refresh token 每次使用都换新，旧 token 立即失效，检测到「旧 token 被再次使用」就吊销整条链（说明泄露）。Cookie 用 `__Host-` 前缀（强制 Secure、无 Domain、Path=/）可以防子域写入。

**会话状态放在哪：**

| 方式 | 状态在哪 | 撤销 | 代价 |
| --- | --- | --- | --- |
| 服务端 session | 内存 / Redis | 立即 | 有状态、要共享存储 |
| 不透明 token | IdP / 资源服务器 | 立即（查表） | 每请求一次查询或缓存 |
| 自包含 JWT | 客户端 | 到期前不可撤 | 无状态，但撤销要额外机制 |

浏览器侧最稳的是「只存一个不透明句柄」：JWT 放进 `HttpOnly` Cookie 也能防 JS 读取，但它体积大、每次请求都带、且服务端无法单方面作废；不透明 session id 把「身份内容」留在服务端，泄露面更小。

**认证、授权、审计是三件事。** 认证回答「你是谁」，授权回答「你能做什么」，审计回答「你做了什么」。mTLS/OIDC 解决第一件，scope + 对象级检查解决第二件，日志与不可篡改存储解决第三件。三件事混在一起写，最常见的形态就是「登录成功 = 有权访问一切」。

### 把六件事串起来：一次下单请求要过几道门

| 门 | 问的问题 | 用什么回答 | 失败时 |
| --- | --- | --- | --- |
| 1 通道 | 我连到的是真服务器吗 | TLS 证书链 + 域名匹配 | 握手失败 |
| 2 对端 | 这个调用方是谁（服务间） | mTLS 证书 SAN / SPIFFE | 握手拒绝 |
| 3 用户 | 这个请求代表谁 | OIDC 登录 + access token 的 `sub` | 401 |
| 4 受众 | 这枚 token 是发给我的吗 | `aud` == 本服务 | 401 |
| 5 权限 | 他能对这个对象做这件事吗 | `scope` + `owner_id` 校验 | 404 |
| 6 一次性 | 这个请求是不是重放/重试 | 时间窗 + nonce + 幂等键 | 409 / 拒绝 |

前两道门在 TLS 层，后四道在应用层。任何一道被跳过，前面的密码学都只是在保护一个「不知道是谁」的通道。§48.1 的事故就是第 4 道门没关：token 是真的、用户是真的、权限也对——只是它不是发给这个服务的。

**四个最常见的误解，逐条对回去：**

- 「有了 mTLS 就不需要 token」——mTLS 只证明「哪个服务在调」，不证明「代表哪个用户、能做什么」；服务替用户办事时仍然需要用户级授权。
- 「JWT 签名对了就安全」——签名只保证「内容没被改、是 IdP 签的」，`aud`/`iss`/`alg`/撤销任何一个漏了都可能越权。
- 「scope 就是权限」——scope 是粗粒度能力，对象级归属（这笔订单是不是他的）必须单独查。
- 「全站 HTTPS 就不用 CSRF token」——Cookie 仍会被浏览器自动带上；HTTPS 不改变「跨站请求会自动携带凭据」这件事。

---

## 48.8 防御落地清单：四栏，按层放，不按产品放

| 层 | 措施 | 代价 | 能挡住哪类攻击 |
| --- | --- | --- | --- |
| **网络层** | BCP38 / uRPF 入口过滤 | 要在源网络配置；可能误伤多宿主 | 源地址伪造、反射放大（治本） |
| 网络层 | DHCP snooping + DAI + 端口隔离 | 交换机特性与网段规划 | 同网段 ARP 欺骗、私接 DHCP |
| 网络层 | RPKI ROA + ROV、前缀过滤 | 需要与运营商/IRR 协同 | BGP 劫持/路由泄漏（部分） |
| 网络层 | anycast + 上游清洗 + 连接数限制 | 持续费用；清洗有误杀 | volumetric DDoS |
| **传输层** | TLS 1.3 + 严格证书校验 + HSTS preload | 证书运维；preload 不可逆；中间盒可见性下降 | 窃听、篡改、SSL stripping、部分 MITM |
| 传输层 | CT 监控 + 应用层 pinning | pinning 轮转要发版；CT 要有人看 | 私有 CA MITM、错误签发 |
| 传输层 | SYN cookie + backlog/somaxconn 调优 + 超时 | 极端过载时丢选项；调参要压测 | SYN flood、半开队列耗尽 |
| 传输层 | 关闭开放 UDP 反射服务、DNS RRL | 可能影响合法大响应 | 反射放大 |
| **应用层** | OAuth2/OIDC + PKCE + `state` | 登录跳转多一次 RTT；IdP 成为关键路径 | 密码扩散、授权码拦截、登录 CSRF |
| 应用层 | JWT 固定 alg、校验 `iss/aud/exp/nbf/kid`、短 TTL + 撤销 | 每请求验签 CPU；无状态性打折 | token 混淆、越权、过期 token 重用 |
| 应用层 | HMAC + 时间窗 + nonce + 幂等键 | 时钟同步；nonce 存储；规范化实现成本 | 重放、重复下单 |
| 应用层 | `HttpOnly`/`Secure`/`SameSite` + CSRF token + 登录轮转 | 前端改造；跨站流程要适配 | XSS 盗 session、CSRF、会话固定 |
| 应用层 | L7 限流按 `sub`/`tenant`/接口成本 | 需要身份感知；配额要调 | CC、慢速攻击、资源耗尽 |
| **身份层** | mTLS + SPIFFE + 短有效期轮转 | 证书分发/轮转/CA 运维；无用户语义 | 服务间冒充、横向移动、网络位置信任 |
| 身份层 | 最小权限 scope + 对象级授权 | 每个接口都要写授权判断 | 越权访问（有 token ≠ 有权限） |
| 身份层 | 审计日志（谁、何时、对哪个对象、做了什么） | 存储与隐私；日志本身要防篡改 | 事后归因、异常检测 |

这张表的使用方式：**先按「你能控制哪一层」划边界，再按「攻击落在哪一行」选措施。** 不要从产品目录倒推——「买了 WAF」不等于补了认证，「上了 mTLS」不等于做了授权。

---

## 48.9 实测：mTLS 两次握手、JWT 的 `aud`、HSTS 响应头

实验环境：Linux 6.12（x86-64），OpenSSL 3.0.22，Python 3.11.2 + PyJWT 2.6.0，curl 7.88.1。以下输出均为本机真实运行结果。

### 实验①：自签一套 CA，做一次 mTLS 握手

先生成根 CA、服务器证书（SAN 必须是 `localhost`/`127.0.0.1`，EKU 为 `serverAuth`）、客户端证书（EKU 为 `clientAuth`）：

```bash
# 1) 自签根 CA（2 天有效期，只为实验）
openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt \
  -days 2 -subj "/CN=Demo Root CA"

# 2) 服务器证书：SAN 必须匹配访问名，EKU=serverAuth
openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr -subj "/CN=localhost"
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out server.crt -days 2 \
  -extfile <(printf "subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n")

# 3) 客户端证书：CN 就是服务身份，EKU=clientAuth
openssl req -newkey rsa:2048 -nodes -keyout client.key -out client.csr \
  -subj "/CN=svc-order/O=demo"
openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out client.crt -days 2 -extfile <(printf "extendedKeyUsage=clientAuth\n")
```

```
$ openssl x509 -in client.crt -noout -subject -ext extendedKeyUsage
subject=CN = svc-order, O = demo
X509v3 Extended Key Usage:
    TLS Web Client Authentication
```

服务端起在 `127.0.0.1:55969`，`-Verify 1` 表示要求客户端证书并验证到深度 1，`-verify_return_error` 表示验证失败直接终止握手而不是继续：

```bash
openssl s_server -accept 127.0.0.1:55969 -cert server.crt -key server.key \
  -CAfile ca.crt -Verify 1 -verify_return_error -www
```

**A. 客户端不出示证书：**

```bash
$ printf 'GET / HTTP/1.0\r\n\r\n' | openssl s_client -connect 127.0.0.1:55969 \
    -CAfile ca.crt -servername localhost 2>/dev/null | grep -a 'HTTP/1.0'
（无输出：没有拿到任何 HTTP 响应）

$ tail -2 server.log
...error:0A0000C7:SSL routines:tls_process_client_certificate:
peer did not return a certificate:../ssl/statem/statem_srvr.c:3517
```

**B. 客户端出示证书：**

```bash
$ printf 'GET / HTTP/1.0\r\n\r\n' | openssl s_client -connect 127.0.0.1:55969 \
    -CAfile ca.crt -servername localhost \
    -cert client.crt -key client.key 2>/dev/null | grep -a 'HTTP/1.0'
HTTP/1.0 200 ok

$ tail -2 server.log
depth=0 CN = svc-order, O = demo
verify return:1
```

同一个端口、同一份服务端代码，差别只在客户端有没有出示证书：A 在 TLS 1.3 的 `CertificateRequest` 之后就被服务端拒绝，HTTP 层根本没进；B 的服务器日志里直接出现 `CN = svc-order`，这就是「服务身份」在应用层可见的形态。**注意：两次握手的 `Verify return code` 在客户端侧都可能是 0——那是客户端在验证服务器，不是服务器在验证客户端。双向认证要看服务端日志。**

### 实验②：JWT 少校验一个 `aud`，真 token 也能越权

```python
import jwt, time
KEY = "demo-hmac-key-not-a-real-secret"

# 签发端：这枚 token 只发给 svc-billing
now = int(time.time())
tok = jwt.encode({"iss": "https://id.demo", "aud": "svc-billing",
                  "sub": "user-42", "scope": "order:read",
                  "iat": now, "exp": now + 600}, KEY, algorithm="HS256")
print("token 段数 =", len(tok.split(".")))
print("header    =", jwt.get_unverified_header(tok))

def naive(t):
    # 只验签名与 exp，不验 aud —— 最常见的漏检
    return jwt.decode(t, KEY, algorithms=["HS256"], options={"verify_aud": False})

def strict(t):
    return jwt.decode(t, KEY, algorithms=["HS256"], audience="svc-order")

print("naive  接受?  ", naive(tok)["sub"], "/ scope =", naive(tok)["scope"])
try:
    strict(tok); print("strict 接受?  是")
except jwt.InvalidAudienceError as e:
    print("strict 接受?  否 ——", type(e).__name__)
```

```
token 段数 = 3
header    = {'alg': 'HS256', 'typ': 'JWT'}
naive  接受?   user-42 / scope = order:read
strict 接受?  否 —— InvalidAudienceError
```

这四行就是 §48.1 事故的最小复现：**签名是真的、`sub` 是真的、`exp` 没过期，唯一错的是「这枚 token 不是发给你的」**。`aud` 不是可选项，它是「受众」这一维度的认证。

### 实验③：算法的白名单校验，以及 HSTS 只是一个响应头

前两段是明文这件事，§48.7④ 已经用同一份输出证明过；这里只看两件容易漏的：算法必须钉死，HSTS 必须真的出现在响应头里。

```
$ python3 alg_demo.py
none token 段数 = 3 签名段长度 = 0
algorithms=['HS256']: 拒绝 —— InvalidAlgorithmError
```

最后看 HSTS。它只是服务器返回的一个响应头，浏览器在**收到过一次 HTTPS 响应之后**才记住：

```bash
$ curl -sS -D - -o /dev/null https://github.com | grep -i strict-transport
strict-transport-security: max-age=31536000; includeSubdomains; preload

$ curl -v -o /dev/null https://github.com 2>&1 | grep -E 'SSL connection|ALPN: server'
* SSL connection using TLSv1.3 / TLS_AES_128_GCM_SHA256
* ALPN: server accepted h2
```

`max-age=31536000` 是 365 天；`includeSubDomains` 指示将策略用于子域；`preload` 本身不证明已纳入任何浏览器的预加载列表。动态 HSTS 依赖浏览器先经有效 HTTPS 接受策略；预加载列表如果确实包含该站，则可在首次访问前生效。

---

## 48.10 三列账：补上的能力、付掉的成本、失效的那个点

| 措施 | 承诺（把缺的补上） | 代价 | 崩点 |
| --- | --- | --- | --- |
| TLS + 严格校验 | 通道的机密性、完整性、服务器身份 | 握手 1–2 RTT；证书签发/轮转/监控；中间盒可见性下降 | 校验被跳过（`-k`、自签信任、企业根）、证书过期、时钟错 |
| mTLS | 服务间「对端是谁」不依赖 IP | 证书分发与轮转；CA 高可用；无用户语义 | 信任所有 CA、只看「有证书」不看 SAN、轮转失败 |
| OAuth2/OIDC + PKCE | 用户身份与授权范围分离；code 不惧泄露 | 登录多一次跳转；IdP 是关键路径；同意页影响转化 | `state` 缺失、`redirect_uri` 通配、ID token 当 access token |
| JWT 严格校验 | 无状态地传递「谁、能做什么、到何时」 | 每请求验签 CPU；密钥轮转；短 TTL 带来刷新流量 | 不校验 `aud`/`iss`、信 header 的 `alg`、无法撤销 |
| HMAC + 时间窗 + nonce | 请求不可重放、内容不可改 | 时钟同步（NTP）；nonce 存储；规范化实现 | 两端规范化不一致、时间漂移、nonce 缓存被清 |
| 会话 Cookie 属性 + 轮转 | 浏览器侧身份不被偷、不被固定 | 前端改造；跨站流程适配；`Strict` 影响体验 | XSS 仍然能代发请求、`SameSite=None` 滥用、登录不换 ID |
| L7 限流按身份 | 峰值时保住关键路径 | 需要认证上下文；配额调参；可能误伤大客户 | 只按 IP（NAT 后全员连坐）、只按总量（一个租户打满） |

一句话收束：**这一层买到的是「请求可归因、可撤销、不可重放」，付出的是「每一次请求都要多一次密码学校验 + 一套密钥运维 + 一点用户体验」，崩点几乎全部落在「校验被跳过」和「时间/密钥不同步」上。**

---

## 48.11 踩坑清单：现象 → 根因 → 定位 → 修

| 现象 | 根因 | 定位 | 修 |
| --- | --- | --- | --- |
| 有 token 就能调所有服务 | 只验签名/`exp`，不验 `aud`（§48.1） | 打印 token payload，对比调用方服务名与 `aud` | 每个资源服务固定自己的 audience；`aud` 不匹配直接 401 |
| 只信 `sub`，不看 `scope`/对象归属 | 把「认证」当成了「授权」 | 用 A 用户的 token 读 B 用户的 `order_id`，看是否 200 | 每个对象查询都带 `owner_id = sub` 条件；越权返回 404 而不是 403 |
| 刚签的 token 立刻过期 / 过期了还能用 | `exp`/`nbf` 用了本地时间或时区错误；时钟漂移 | 对比 token 的 `iat` 与服务器 `date -u`；看 NTP 同步状态 | 统一 UTC 秒；允许 ≤60 s skew；监控时钟偏移 |
| JWT 出现在 URL / Referer / 日志里 | 把 token 当查询参数传 | 搜访问日志的 `?token=`；看 Referer | 只放 `Authorization` 头或 `HttpOnly` Cookie；日志脱敏 |
| 开了 mTLS，任何人拿自签证书都能进 | 信任所有 CA / 不校验 SAN / 不校验 EKU | 用一张不受信 CA 的客户端证书试握手 | 固定 `CAfile`；校验 SAN 与 `clientAuth`；禁用 `InsecureSkipVerify` 类开关 |
| 限流只按 IP，活动时正常用户全被限 | NAT/CGNAT 后是几千人；botnet 用海量 IP | 看被限流 IP 的活跃 `sub` 数 | 认证后按 `sub`/`tenant` 限；IP 只做粗粒度第一层 |
| 两端签名总是不一致 | 规范化不同：键序、编码、数字格式、数组顺序 | 两端各打印待签字节串的 hex，逐字节对比 | 固定序列化器与字段白名单；签名测试进 CI |
| 用户退出后旧 token 还能用 | JWT 无状态，签发后不可撤销 | 退出后拿旧 token 调接口，看是否 200 | 短 TTL + refresh 轮转 + `jti`/版本黑名单 |
| 登录后 session 被攻击者复用 | 会话固定：登录不换 ID | 记录登录前后 Cookie 的 session ID | 权限变化即轮转 ID；不接受 URL 里的 session ID |
| HSTS 配了但首次访问仍被降级 | 明文 HTTP 的首个请求没有 HSTS | 清空浏览器状态后用 `http://` 访问一次 | 提交 preload；HTTP 端只做 301 且不承载任何内容 |

---

## 48.12 一页速记

```
【一句话】攻击 = 协议缺了认证/完整性/机密中的某一样；防御 = 把缺的那一样补在该补的层。
          TLS 补全了通道的三样，但没有认证用户；应用层认证是最后一环。

【总表九行】
  ARP   ✗✗✗ → 欺骗/MITM        → DAI/静态绑定 + TLS 兜底
  IP    ✗✗✗ → 伪造/反射放大    → BCP38/uRPF + IPsec
  ICMP  ✗✗✗ → 重定向/不可达    → 类型白名单/限速
  DNS   ✗✗✗ → 投毒/劫持/隧道   → DNSSEC + DoT/DoH + 随机化
  TCP   ✗✗✗ → 注入/RST/SYN     → TLS + ISN 随机化 + SYN cookie
  UDP   ✗✗✗ → 反射放大         → BCP38 + 关开放服务 + RRL
  HTTP  ✗✗✗ → 窃听/篡改/CSRF   → HTTPS + HSTS + Cookie 属性
  TLS   △✓✓ → 证书链/MITM/SNI  → 严格校验/pinning/CT + mTLS + ECH
  应用  △△△ → 越权/重放/混淆   → OIDC+PKCE + JWT 严格校验 + 签名防重放

【MITM 四入口】ARP（下一跳 MAC）/ DNS（域名→IP）/ BGP（IP→路径）/ 证书链（公钥→域名）
              前三个都能被第四个兜住；第四个只能靠 pinning/CT/私有 CA 审计。

【DDoS 三分类】volumetric（带宽）/ protocol（SYN 半开队列）/ application（CC、慢速）
              反射放大 = UDP + 源地址可伪造 + 响应比请求大
              memcached ~5×10⁴×，NTP monlist ~200×，DNS ~50×，CharGen ~358×

【应用层六件套】
  mTLS        服务间身份，SAN=SPIFFE，短有效期轮转
  OAuth2      四角色；授权码 + PKCE（S256）；state 防登录 CSRF
  OIDC        ID token 给 Client，access token 给 API，别混用
  JWT         三段；base64url 不是加密；钉死 alg + 校验 iss/aud/exp/nbf/kid
  签名防重放   HMAC + 时间窗 ±300s + nonce + 规范化
  会话         HttpOnly/Secure/SameSite + CSRF token + 登录轮转

【三个数字】
  2³² = 4.29×10⁹；64 KiB 窗口 → 盲猜命中率 1/65536 ≈ 1.5×10⁻⁵
  10k SYN/s × 63 s = 630k 半开连接，是 backlog=4096 的 150 倍
  1 Mbps × 5×10⁴ 放大 = 50 Gbps

【三条铁律】
  ① 校验被跳过 = 前面所有密码学归零（证书、JWT、签名都一样）
  ② 时间不同步 = 一半的认证逻辑随机失效（exp/nbf/时间窗）
  ③ 有 token ≠ 有权限；认证之后必须再做对象级授权
```

---

## 48.13 自检 3 题

**Q1.** 一个内部 API 已经全站 HTTPS，为什么还需要 mTLS 或 OAuth2？请从「TLS 认证了谁」和「NAT 之后服务器能看到什么」两个角度回答。

<details><summary>答案</summary>

TLS 默认只认证**服务器**：客户端验证 `order.example.com` 的证书，确认「我连到的是真服务器、通道没被旁听/篡改」。客户端证书是可选的，Web 世界几乎不要求，所以服务器**不知道请求属于哪个用户/哪个服务**。

服务器在应用层能看到的只有源 IP、源端口、时间。NAT/CGNAT 让一个 IP 后面可能是几千个用户，云函数出口更是共享的，所以「按 IP 认人」在身份层面不成立。

mTLS 补的是「服务间对端身份」：客户端出示证书，服务器校验链、EKU=`clientAuth`、SAN（如 `spiffe://prod/ns/pay/sa/order`），把「谁可以连」从网络位置改成证书身份。OAuth2/OIDC 补的是「用户身份与授权范围」：授权码 + PKCE 拿到 access token，资源服务器校验 `iss/aud/exp/scope`，再做对象级授权。两者都不是 TLS 的替代品，而是 TLS 之上必须补的那一层。
</details>

**Q2.** 一枚 JWT 的签名完全正确、`exp` 也没过期，为什么还可能被拒绝？如果服务端漏了 `aud` 校验，会发生什么？请用 §48.1 的链路说明。

<details><summary>答案</summary>

签名正确只证明「这枚 token 是 IdP 签的、内容没被改」，不证明「它是发给这个服务的」。JWT 还有 `iss`（谁签的）、`aud`（发给谁）、`nbf`（何时生效）、`kid`（用哪把钥匙验）等约束；任何一个不匹配都应拒绝。

漏了 `aud` 校验时，发给 `svc-report` 的只读 token 会被 `svc-order` 接受。§48.1 的事故正是：报表 token 泄露在埋点 URL 里，攻击者拿它调下单接口；订单服务只验「签名 + exp」，于是「真 token」完成了越权。修复是每个资源服务固定自己的 audience，`aud` 不匹配直接 401，并在认证之后继续做 `scope` 与对象归属检查。
</details>

**Q3.** 为什么「限流只按源 IP」在真实系统里几乎一定出错？请给出两种相反方向的失败，并说明正确的落点。

<details><summary>答案</summary>

两种失败方向：

1. **误伤**：NAT/CGNAT/企业出口/云函数出口让一个 IP 后面有几千个用户，按 IP 限 100 QPS 会让整栋楼的人一起被限；活动期间正常用户先被打挂。
2. **漏放**：botnet/代理池用海量 IP，每个 IP 只打几 QPS，按 IP 限流永远触发不了；攻击总量却足以打穿数据库。

正确落点是分层：边缘按 IP/ASN 做粗粒度（挡明显机器人、抗流量型）；认证之后按 `sub`/`tenant`/`scope` 做令牌桶（因为此时已知身份）；服务内按接口成本做并发上限与队列，队列满就快速失败。限流算法本身见 [eng/15](../eng/15-rate-limit-basics.md)，但**落点选错，算法再好也没用**。
</details>

---

## 48.14 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 链路层：MAC、ARP、交换机、VLAN 与 MTU（ARP 欺骗的物理土壤） | → `base/39` |
| IP 与路由：源地址、分片、ICMP 与转发（反射放大的前提） | → `base/40` |
| TCP 建立/关闭与 TIME_WAIT（RST 与半开队列的状态机） | → `base/41` |
| TCP 可靠与拥塞：重传、SACK、窗口（DDoS 与限流的队列视角） | → `base/42` |
| DNS 协议：12 字节头部、递归/迭代、TTL 与 EDNS | → `base/44` |
| DNS 安全：三缺协议的攻击面与补法（DNSSEC/DoT/DoH/隧道） | → `base/45` |
| TLS 与证书：密钥交换、证书链、SNI、1-RTT 与「为什么还是会被抓包」 | → `base/47` |
| 流量形态与 record 层复用：加密之后还能看出什么 | → `base/49` |
| 限流算法、配额与分布式令牌桶 | [eng/15](../eng/15-rate-limit-basics.md) |
| 幂等键：存储、并发、过期与事务边界 | [eng/2](../eng/2-idempotency-implementations.md) |
| 提示注入与内容安全（应用层认证之后的下一道门） | [llm/30](../llm/30-injection-content-safety.md) |
| 沙箱、加固与最小权限（身份之后的执行面） | [llm/32](../llm/32-defense-hardening.md) |
| 全书总纲：四条主线与 56 篇清单 | [0-summary](0-summary.md) |
