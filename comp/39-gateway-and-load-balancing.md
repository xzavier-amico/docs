# 39. 入口网关与负载均衡：Nginx / Envoy / LVS

> **状态**：`已完成（初稿）` ｜ **所属**：第八部分 · 入口与东西向通信 ｜ **编号**：39 / 53
> **本篇定位**：全仓唯一一处把「四层转发（LVS/ipvs）、七层代理（Nginx）、可编程七层代理（Envoy）」这三件入口零件的**物理形态**摆在同一张桌子上对照的篇目——连接状态存在哪、buffer 卡在哪一段、超时链由谁兜底、重载时谁的死活决定连接掉不掉。南北向 RPC 的连接复用与流控归 `→ comp/40`，xDS 控制平面与 sidecar 的账归 `→ comp/41`，注册与配置的推送归 `→ comp/38`（纯文本，尚未写出），幂等重试与投递语义归 `→ eng/24`，K8s 优雅关停的应用侧归 `→ go/52`。本篇一个字不重写它们，只回答一个问题：**入口为什么总是「看着健康、用户就是连不上/收不到」，以及「健康」这个词在四层与七层里根本不是同一个意思。**
> **读法**：救火先读 §39.11 坑清单与 §39.12 指标拐点；被 SSE / WebSocket / gRPC 长连接问题砸中，先读 §39.4 的缓冲链、§39.5 的 Envoy 超时默认值、§39.7 的超时链倒推；发布掉连接读 §39.8 的判定表；做容量与选型先读 §39.3 的「四层够不够」表，再用 §39.9 的三本账算钱。只想知道结论的翻 §39.13。

---

## 39.1 引子：三起「入口看着健康、用户就是连不上/收不到」的事故

三起事故的共同形状是：**仪表盘全绿**。LVS 有 VIP、Nginx 有 200、Envoy 的 cluster 是 healthy、CPU 不到 20%——但用户看到的是转圈、断流、502。原因不是某个组件挂了，而是**入口是「连接、缓冲、超时」这三件事的汇聚点，而这三件事都不在任何单机的健康检查里**。

### 事故一：`proxy_buffering` 一开，SSE 变成「攒够一次发」（2026-04-08）

某 AI 助手产品把流式输出从「WebSocket 直连应用」改成「标准 SSE + Nginx 统一入口」。改造的动机很正当：要统一鉴权、要限流、要灰度。Nginx 从公司模板继承了一整套「JSON API 最佳实践」——`proxy_buffering on`（这本来就是**默认值**）、`proxy_buffers 8 8k`、`gzip on`、`proxy_read_timeout 60s`——然后把它用在了 `/v1/stream/` 上。

上线当天下午的现象极具误导性：**接口 200、`upstream_response_time` 38 s、`request_time` 38 s、错误率 0**。也就是从 Nginx 的账本看，一切都对；但用户从点击到看见第一个字，等了 38 秒。

| 时间 | 事件 | 关键数字 |
| --- | --- | --- |
| 04-08 11:20 | 新入口上线，`/v1/stream/` 走与 JSON 相同的 location 模板（`proxy_buffering on` + `gzip on`） | — |
| 11:26 | 用户反馈「要等半分钟才出字」；客服工单 30 分钟内 210 单 | — |
| 11:40 | 前端打点实测：首字延迟 P50 = 37.6 s、P99 = 58.2 s（改造前 P50 = 0.42 s） | 0.42 s → 37.6 s |
| 12:05 | 「把前端 60 s 超时调到 120 s」——只让问题变得更难看（流到 62 s 时突然弹出一大段） | 无效 |
| 12:30 | 怀疑应用侧：应用日志的第一次 `Write()` 是请求后 180 ms，逐 token 写；**说明出字正常、是出口攒着** | 应用侧无嫌疑 |
| 13:10 | 定位：`proxy_buffers 8 8k` 意味着第一段数据要填满 8 KB 才交给客户端；模型按 22 token/s、中文约 6 B/token ≈ 132 B/s 出字 → **填满 8 KB 需 62 s** | 8192 ÷ 132 ≈ 62 s |
| 13:50 | 修：该 location `proxy_buffering off; gzip off; proxy_cache off;`，后端同时回 `X-Accel-Buffering: no` 兜底 | 首字回到 0.44 s |
| 复盘 | 功能日活 4.2 万，当日下午转化率 3.1% → 2.5%；按客单价折算月损失约 60 万元 | ≈ 60 万元 |

**这起事故最贵的不是那 62 秒，而是「诊断方向被整体带偏」**：Nginx 的 `request_time` 把「攒批时长」算进了响应时间，于是所有基于 Nginx 日志的看板都显示「应用慢」，四个小时都在查应用。真正的读数只有一个：**应用第一次 `Write()` 的时间戳**（180 ms）。凡是「应用侧写得很早、客户端收到得很晚」，八成是某一跳在做缓冲——而入口是唯一一跳默认就开着缓冲的地方。

### 事故二：`nginx -s reload` 之后，长连接要么被旧 worker 拖住、要么直接断（2026-05-19）

某在线协作产品的入口是 6 台 Nginx（8 核 / `worker_processes auto`），承载约 6 万条 SSE/WebSocket 长连接，配置由 ConfigMap 渲染，每次改动走 `nginx -s reload`。这一次发布只改了一个 `proxy_read_timeout`（从 60 s 改到 600 s），属于「理论上零风险」的变更。

真实后果有两副面孔，都出现在同一周：

- **拖住**：`worker_shutdown_timeout` 没有配（默认不设，即**没有强制上限**）。reload 后旧 worker 停止 accept，但它身上的长连接**不会被新 worker 接管**——Nginx 不做连接迁移。于是旧 worker 一直活到最后一个流结束。那一次有一条「永不结束」的监控流，旧 worker 活了 3 小时 40 分，期间它上面挂着 2,400 条连接，**这些连接用的是旧配置**，且它们不在任何新 worker 的连接计数里。
- **直接断**：另一天运维为了「避免拖住」，补上 `worker_shutdown_timeout 30s`。于是超过 30 s 的流被 master 强制关闭，客户端收到 RST/FIN——这不是「优雅重载」，是「定时处决」。

| 时间 | 事件 | 关键数字 |
| --- | --- | --- |
| 05-19 21:02 | 发布：改 `proxy_read_timeout` → `nginx -s reload`（6 台各一次） | 在线连接 60,000 |
| 21:02:03 | 旧 worker 上的 **idle keepalive 连接被立刻关闭**；在途流继续跑 | 立即断约 4% |
| 21:02:05 | 浏览器侧 SSE 客户端**无退避**地立刻重连；6 万台机器里约 2,400 台同时重连 | 重连 2,400/s |
| 21:02:06 | TLS 握手打满：`1.2 ms CPU/次 × 2,400/s ≈ 2.9 CPU·s/s`，8 核里 3 核专用于握手 | CPU 由 18% → 61% |
| 21:02:20 | `listen backlog`（默认 511）溢出 → SYN 被丢 → 客户端 1 s / 3 s / 7 s 退避 | 恢复期 90 s |
| 21:40 | 旧 worker 仍未退出（无 `worker_shutdown_timeout`），`ps` 里两个 worker 世代的 shm 各占一份 | 内存 +18% |
| 05-21 | 补 `worker_shutdown_timeout 300s`：超过 300 s 的流仍然会被杀，但不再「永远拖着」 | 仍非零损伤 |
| 复盘 | 一周 11 次发布，累计异常断连 3.7 万次；3 次客诉「文档停留在旧版本权限」 | 长连接不能靠 reload 发布 |

**结论不是「reload 不安全」，而是「reload 的语义只覆盖到 worker 生命周期，不覆盖你的连接」**：`reload` 保证的是「新的 accept 用新配置」，它对**已经建立的连接**只说了一句「你自己跑完」。所以长连接系统的发布单位必须从「reload 一次配置」升级成「按连接批量迁移 + 客户端带抖动重连 + 有明确 drain 窗口」（§39.8）。

### 事故三：健康检查把所有后端一次摘光，入口 502（2026-06-02）

某电商的入口拓扑是「云 SLB（四层）→ 自建 LVS-DR（keepalived）×2 → Nginx 集群 ×6 → 应用」。LVS 后端的健康检查用的是 keepalived 的 `HTTP_GET /healthz`，`interval 2`、`fall 3`、`rise 3`——也就是**连续 3 次失败（≥6 s）就摘，连续 3 次成功（≥6 s）才挂回去**。

20:14 的一次站内推送让长连接从 4 万涨到 21 万。Nginx 的 `worker_connections 1024 × 8 worker ÷ 2`（反向代理每条连接占两个额度）= 4,096 的上限早就不够，连接数越限后 `/healthz` 请求也排不进 accept 队列——**健康检查与被检查的流量共用同一个 listen 端口、同一批 worker、同一份连接配额**。

| 时间 | 事件 | 关键数字 |
| --- | --- | --- |
| 06-02 20:14 | 站内推送触发长连接风暴，4 万 → 21 万；`worker_connections` 越限，`/healthz` 进不了队列 | 21 万连接 |
| 20:15:30 | 6 台后端的 `/healthz` 全部连续 3 次超时（`fall 3`，间隔 2 s） | 6/6 失败 |
| 20:15:36 | keepalived 把 6 个 `real_server` **全部摘除**；VIP 仍在 announce，director 上没有任何 RS | 可用后端 0 |
| 20:15:37 | ipvs 对新连接直接 RST（无可用 RS），上游 SLB 也把两侧 director 判死 → 客户端 502/504 满天 | 502 率 100% |
| 20:18 | 值班看到的是「LVS 存活、VIP 可达、后端 0 台」——四层视角里这是「全部不健康」，四层没有任何指标能说清为什么 | 定位耗时 9 min |
| 20:24 | 手工 `ipvsadm -a` 强塞回 2 台（绕过检查），先把流量接住 | 部分恢复 |
| 20:31 | 提 `worker_connections` 到 8192、`worker_rlimit_nofile` 到 65535，并把 `/healthz` 挪到独立端口 + 独立 server 块 | 恢复 |
| 复盘 | 全站不可用 16 分钟，订单损失约 210 万元；根因是「健康检查与业务共享同一份连接配额」 | ≈ 210 万元 |

**四层没有「最多摘掉百分之几」的保护，也没有「panic 模式」**：Nginx Plus / Envoy 都有 `max_ejection_percent`（默认 10%）这类「摘到只剩一成就别再摘了」的护栏，而 ipvs + keepalived 的语义是**忠实执行**——你让它摘，它就摘光。这条差异在 §39.6 会算成一张「摘除/恢复时间常数」的表。

> 🩸 **三起事故的共同点**：没有一次是「入口挂了」。事故一是**默认值把流变成了批**（缓冲在入口）；事故二是**重载的语义边界比人们以为的窄**（连接不在 reload 范围内）；事故三是**健康检查的判据与被检查的对象共用了同一份稀缺资源**（检测工具和被测对象一起被饿死）。三者都指向同一句：**入口的健康指标衡量的是「入口自己有没有在跑」，不是「用户的字节有没有在动」。**

---

## 39.2 一句话定位与唯一真问题；入口的三层职责

**定位**：入口（ingress）不是一个组件，是**三层职责的叠加**——四层转发选「哪台机器」、七层路由选「哪个服务/哪个版本」、连接与超时治理决定「一次交互能活多久」。LVS/ipvs 只做第一层，Nginx 通常做第一层（监听）+ 第二层 + 第三层的一部分，Envoy 把三层都做成可编程对象（listener / route / cluster），代价是多一层控制平面（`→ comp/41`）。

### 唯一真问题

> **入口是长连接、超时与缓冲的汇聚点：`proxy_buffering` 一开，SSE 就变成「攒够一次发」；优雅重载决定发布掉不掉连接。**

这句话拆成四条推论，全篇都在给它们找证据：

1. **默认值是给短请求调的，不是给你的流量调的。** `proxy_buffering on`、`proxy_read_timeout 60s`、`keepalive_timeout 75s`、`client_max_body_size 1m`、Envoy 的 `route.timeout 15 s`——每一条的默认值都是一个「平均 HTTP 请求」的形状。长连接、大上传、慢下游，都会撞在这些默认值上，而且撞法各不相同（§39.4、§39.5）。
2. **四层的吞吐优势来自「它不知道自己在转发什么」。** 一旦你需要按 Path 路由、按 Header 灰度、按 stream 复用，你就必须让代理持有每个请求的状态（buffer、超时、重试计数），这部分成本随「在途请求数 × 每请求内存」线性增长（§39.9 账一）。
3. **超时必须从客户端一路倒推，不能各层自定。** 入口是超时链上**最容易悄悄改短**的一跳，因为它的默认值往往小于下游 SLO。倒推顺序：客户端预算 → 入口 → 服务 → 依赖，每层留 30%–40% 余量（§39.7）。
4. **发布掉不掉连接，是「连接归属」问题，不是「配置热加载」问题。** 能热加载配置的组件很多；能把一条已建立的长连接从旧进程迁到新进程的组件，一个都没有。所以答案只能落在「客户端重连的抖动 + 旧进程的 drain 窗口 + 检测/摘除的传播延迟」这三项的和上（§39.8）。

**它明确不承诺什么**（后面逐条兑现）：LVS 不承诺「按请求均衡」（它只按连接/包选路）；Nginx OSS 不承诺「主动健康检查」（只有被动 `max_fails`/`fail_timeout`，主动探测是 Plus 的能力）；Envoy 不承诺「配置一定生效」（它是 xDS 的执行者，控制面挂了它只是维持在旧配置上）；而**它们中没有一家承诺「连接不丢」**——热重启、drain、`worker_shutdown_timeout` 都是在**替你选择丢哪一批**。

### 三层职责表

| 层 | 调度单位 | 它看得见什么 | 它必须管的事 | 它看不见的（所以也管不了） | 典型崩点 |
| --- | --- | --- | --- | --- | --- |
| **① 四层转发**（LVS/ipvs、云 SLB、ECMP） | 一条**连接**（甚至一个包） | 五元组、字节数 | 选 director、选 RS、NAT/DSR 回包路径 | 请求边界、Header、状态码、TLS 内容 | 长连接把流量钉死在一台上；RS 摘光；conntrack 表满；ECMP 重哈希导致存量连接漂移 |
| **② 七层路由**（Nginx、Envoy） | 一次**请求**（HTTP/2 下是一条 **stream**） | Host / Path / Header / method / 状态码 / 时长 | 路由、灰度、鉴权传递、buffering、retry、限流入口 | 业务语义、幂等性、下游真实耗时构成 | 缓冲把流变成批；重试放大；`connection` 复用踩到对被端的 idle 超时 |
| **③ 连接与超时治理**（①②都要做，通常落在七层） | 一次**交互的生命周期** | accept 队列、idle 时长、读写间隔 | keepalive、`*_timeout`、`lingering close`、drain、连接池 | 客户端真实等待耐心、移动网络切换 | 各层超时无序；idle 连接重连风暴；drain 窗口短于流的寿命 |

```text
                        入口的三层职责与"每一层各自数什么"

   客户端 / SDK
      │  ①（连接、五元组）
┌─────▼───────────────────────────────────────────────────────────────┐
│ L4：LVS-DR / 云 SLB / ECMP                                          │
│   计数单位：连接（conntrack 一条）  状态：ipvs 连接表 / 哈希槽        │
│   它认为"健康"= 这条 RS 的 TCP 或 HTTP 探测通了                       │
└─────┬───────────────────────────────────────────────────────────────┘
      │  ②（请求 / HTTP2 stream）
┌─────▼───────────────────────────────────────────────────────────────┐
│ L7：Nginx / Envoy                                                   │
│   计数单位：请求（或 stream）      状态：每请求 buffer + 超时定时器   │
│   它认为"健康"= upstream 没超时、没返回 5xx                          │
│   ★ 唯一同时持有"缓冲"和"超时"两件东西的一层                          │
└─────┬───────────────────────────────────────────────────────────────┘
      │  ③（到实例的池化连接）
┌─────▼───────────────────────────────────────────────────────────────┐
│ 应用实例（长驻进程，自带线程池 / 连接池）                              │
│   计数单位：在途 handler            状态：线程、连接池、队列长度       │
└─────────────────────────────────────────────────────────────────────┘

一次"用户收不到数据"的事故，可以发生在任意一层的"计数单位"里：
  L4 层：连接被钉在坏 RS 上（摘除没生效）——用户侧表现为一直等
  L7 层：请求没超时但字节被 buffer 攒着——所有仪表盘都是绿的（事故一）
  L7 层：超时被上层先掐断——日志里只有 499，没有 5xx
  应用层：handler 在，但写不出去（对端不读，TCP 窗口为 0）——CPU 也是绿的
```

### 解剖卡八问落点（本篇答案）

| 问 | 落在哪节 | 一句话答案 |
| --- | --- | --- |
| ① 承诺什么 | §39.2、§39.10 | 四层承诺「把连接送到一台还活着的机器」；七层承诺「按请求选路 + 在超时内给出结论」；两者都不承诺「字节按时到达」 |
| ② 数据长什么样 | §39.4、§39.5 | Nginx：每连接一个 `ngx_connection_t` + 每请求一条 buffer 链（header buffer + `proxy_buffers` 数组）；Envoy：per-worker 的连接池对象 + 每 stream 的 filter chain 实例 |
| ③ 一次读写走哪条路 | §39.3–§39.5 各自分解表 | 差异最大的一跳是「字节在入口停了多久」：LVS 不停（纯转发）、Nginx 可能停满一个 buffer、Envoy 默认不停但受 `stream_idle_timeout` 约束 |
| ④ 并发与一致 | §39.4、§39.5、§39.9 | Nginx 是「单线程事件循环 + 多 worker」，无共享状态；Envoy 是「主线程 + 每核 worker」，配置经 xDS 全量下发再原子切换 |
| ⑤ 扩展维度 | §39.9 账二、§39.10 | 入口本身就是**分片**（多台 + ECMP/一致性哈希）；垂直切 = 按流量类型拆 listener/location（短请求 vs 长流）；副本 = 多 director；批处理摊薄对入口不成立（它是逐请求路径） |
| ⑥ 系统级失效 | §39.10、§39.11 | 集群级：RS 全摘（事故三）、director 脑裂 + VIP 漂移、连接配额耗尽；语义级：缓冲改变实时性（事故一）、重试把下游打穿；成本级：长连接内存与端口耗尽 |
| ⑦ 看哪几个指标 | §39.12 | `active connections / worker_connections`、`upstream_response_time` 分位、5xx 与 499 比值、`Reading/Writing/Waiting`、每个 upstream 的可用端点数是**入口的第一指标** |
| ⑧ 换掉它的信号 | §39.3、§39.10 | 需要按请求灰度/重试预算/主动摘除 → 四层不够，上 Envoy；需要千万级 pps 或 TLS 卸载下沉到硬件 → 七层太重，退回四层 |

---

## 39.3 四层 vs 七层：LVS 的四种转发形态与「什么时候四层够」

**一句话**：四层转发只有一件事要做——**把包送到一台机器上，并保证回包能回来**。它会做的选择只有两种：改包（NAT）或封装（TUN），以及「回包走不走自己」这两条岔路。四种形态的全部区别就是这两条岔路的组合。

### LVS 的四种转发形态

| 形态 | 请求方向 | 回包方向 | RS 需要什么 | 单机吞吐量级 | 典型崩点 |
| --- | --- | --- | --- | --- | --- |
| **NAT**（含 FULLNAT） | director 做 DNAT，目标改成 RS | 回包必须经过 director 做反向翻译（改回 VIP），或 FULLNAT 额外 SNAT | RS 网关必须指向 director（FULLNAT 可免） | director 是双向瓶颈，Mpps 级但带宽吃满 | conntrack 表满；director 成为单点 + 带宽上限；`expire_nodest_conn` 未开时摘除后旧连接挂死 |
| **DR**（Direct Routing） | 只改 MAC，IP 不变；RS 的 lo 上有 VIP | **直连客户端**绕过 director（非对称路径） | 内核 `arp_ignore=1`、`arp_announce=2`，VIP 绑 `lo`；回包路由可达客户端 | 最高，几个 Mpps 量级（只改二层） | ARP 冲突/双回答；RS 上 `lo` 没起 VIP 就全 502；回包路径不回 director 导致统计失真 |
| **TUN**（IP 隧道） | IPIP 封装后送给 RS | 直连客户端 | 能解 IPIP、`lo` 上有 VIP | 高（多一层包头，MTU 要调） | MTU 1450 引起大响应分片被丢；跨机房几乎不能用 |
| **ECMP + 任意形式** | 多台 director 共享 VIP（BGP/OSPF 宣告） | 视形态 | 上层交换机/路由支持 ECMP | 横向线性扩 | **五元组哈希**：同一条连接永远固定落在一台 director，扩容时哈希重算会让存量连接漂移 |

```text
               NAT 与 DR 的回包路径差异（为什么 DR 快、为什么 DR 挑环境）

① NAT 模式（往返都过 director）
   client ──SYN──▶ [director: VIP→RIP, 改 dst=10.2.3.11] ──▶ RS(10.2.3.11)
   client ◀─ACK── [director: src 改回 VIP] ◀────────────── RS
   ⇒ director 上要有 conntrack 记这条会话；带宽 = 入 + 出（双向都算它的）

② DR 模式（只去程过 director，回包直发）
   client ──SYN──▶ [director: 只改 dst MAC = RS 的 MAC，IP 仍是 VIP] ──▶ RS(lo:VIP)
   client ◀──────────────────────────────────────────────────────── RS 直接回源
   ⇒ 回包不经过 director ⇒ 吞吐 ×2 以上；但 RS 的默认网关必须能把源 IP=VIP 的包发出去
   ⇒ 代价：VIP 同时出现在 director 和每台 RS 上，ARP 必须压住（否则交换机学错 MAC）
```

**为什么这段要和「长连接」扯上关系**：四层的调度单位是连接，所以**一条已建立的长连接在 RS 侧故障后不会自动迁移**——它只会「挂在那儿等超时」。这就是 §39.6 里「摘除」为什么必须配 `expire_nodest_conn`（让 ipvs 主动踢掉指向已摘除 RS 的连接）以及 §39.7 里为什么客户端必须有自己的读超时。

### 什么时候四层够、什么时候必须七层

| 场景 | 四层够吗 | 判据（物理层面） |
| --- | --- | --- |
| 纯 TCP/UDP 服务（MySQL、Redis、MQ、自研 RPC 私有协议） | ✅ 够 | 代理不需要理解协议，只需要选路；七层代理反而会因为解析而变慢 |
| 单一 HTTP 服务、所有实例等价、无灰度需求 | ✅ 够（+ 云 SLB） | 无需按 Path/Host 分流；TLS 可卸载在 SLB |
| 极致的连接建立速率与吞吐（十万级新建/s、Mpps） | ✅ 只有四层能做到 | 七层要解析请求头、分配 buffer、建超时定时器 |
| 需要按 Path/Header/Cookie 路由、灰度、A/B | ❌ 不够 | 四层看不到请求边界（HTTP/1.1 keepalive 或 HTTP/2 复用时更是只看到一条连接） |
| 需要 HTTP 语义的重试（`retry_on: 5xx`）、熔断、主动摘除 | ❌ 不够 | 这些判据都以「一次请求的结果」为单位 |
| 需要 HTTP/2 或 gRPC 的多路复用与流量治理 | ❌ 不够 | 一条 TCP 上跑多条 stream，「按连接均衡」会把整个复用连接的负载全压到一台（`→ comp/40`） |
| TLS 需要按 SNI 分流到不同后端 | ⚠️ 要看 | 能看 SNI 但不解密（passthrough）；要改 Header 必须解密 → 需要七层 |

**一个经常被忽略的组合事实**：**四层 + 七层串联时，四层看到的是「七层节点」的健康，不是业务的健康。** 事故三里 LVS 探的是 Nginx 的 `/healthz`，所以它能表达的语义上限是「这台 Nginx 还能 accept 一个连接」；至于「这台 Nginx 后面的应用是否可用」，四层永远不知道。所以**健康检查的层次决定了你故障域的形状**：四层探活只能挡住「机器宕机」，挡不住「后端全都不健康」。

---

## 39.4 Nginx 物理形态：master/worker、epoll 与 buffer 链

### 进程与事件模型

Nginx 的形态可以压成四句话：

1. **一个 master（不做 IO）+ N 个 worker（各跑一个单线程 epoll 循环）。** `worker_processes auto` 通常等于核数；worker 之间**没有共享的连接状态**，只有一块共享内存（`shm`）用于限流、缓存索引、upstream 状态。
2. **每条连接是一个状态机 + 一个 `ngx_connection_t`，每个请求挂一组 buffer。** 反向代理时，一条客户端连接**对应**一条 upstream 连接（HTTP/1.1），因此 `worker_connections` 的消耗是「每在途请求 2 个额度」——这是账二的分母。
3. **不做抢占式调度**：一个 worker 内所有连接靠事件驱动推进，任何一处同步阻塞（磁盘 IO、DNS 解析、第三方模块）都会把**该 worker 上的其他几万条连接**一起停住。这也解释了为什么 `proxy_buffering on` 在短请求上是优点（慢客户端不阻塞 worker），在长流上是灾难（攒批）。
4. **配置只读一次**：worker 启动时加载完整配置到自己的内存；`reload` 是「新 worker 读新配置 + 旧 worker 停在旧配置上跑完」。**没有「同一进程换配置」这回事**——这就是事故二的物理根因。

### 一次请求在 buffer 链上的位置

```text
   一次 proxied 请求在 Nginx 内部的四条 buffer 与三条队列（尺寸为默认值量级）

  客户端 ──▶ ① client_header_buffer_size 1k  ┐ 头放不下 → large_client_header_buffers 4×8k
            ② client_body_buffer_size 8k/16k ┤ 再放不下 → 落临时文件（client_body_temp_path）
                                             │
                    ┌────────────────────────┴──────────────────────────┐
                    │  upstream 连接（来自 upstream keepalive 池）        │
                    └────────────────────────┬──────────────────────────┘
                                             │
            ③ proxy_buffer_size 4k/8k  ◀─────┤ 只装响应头
            ④ proxy_buffers 8 × 4k/8k ◀─────┤ 装响应体 ★ 攒够才发（事故一）
                                             │   busy buffers 上限 = proxy_busy_buffers_size 8k/16k
                                             │   写满 → 落 proxy_temp_file（proxy_max_temp_file_size）
                                             ▼
                                       ⑤ 发给客户端（sendfile / writev）
                                       客户端太慢 → send_timeout 60s → 499/关闭
                                       客户端半关 → lingering_close（lingering_timeout 5s）

  ★ 关键：③④ 只在 proxy_buffering on（默认）时存在。off 时字节是"读一段→立刻写一段"，
    于是 upstream 的读速度被客户端速度钉住（慢客户端会反压到应用，这是它的代价）。
```

### 关键指令：默认值、改的是哪条物理路径、代价落在谁身上

| 指令 | 默认值（以目标版本文档为准） | 它改变的那条物理路径 | 代价/风险 |
| --- | --- | --- | --- |
| `proxy_buffering` | `on` | 响应是否经 `proxy_buffers` 中转 | 开：实时性丢失（SSE 攒批）；关：慢客户端反压 upstream，且 worker 可能被长写阻塞 |
| `proxy_buffer_size` / `proxy_buffers` | `4k`/`8k`；`8 × 4k`/`8 × 8k` | 每请求缓冲区大小 → 每连接内存 | 越大越抗抖动、越吃内存；SSE 攒批时长 ≈ buffer 大小 ÷ 出字速率 |
| `proxy_read_timeout` | `60s` | **两次成功读之间**的最大间隔（不是总时长） | 长流/长轮询必须放大；但放大后「真卡死」的发现时间也变长 |
| `proxy_connect_timeout` | `60s` | 与 upstream 建连 | 内网应压到 1 s 量级，否则一次网络黑洞能占满在途额度 |
| `keepalive_timeout`（http/server 级） | `75s` | **客户端**空闲 keepalive 连接的存活期 | 大于负载均衡的 idle 超时会话被中途掐（客户端侧表现随机 502）；小于则重连变多 |
| `keepalive_timeout`（upstream 块内） | `60s` | **到后端的**空闲连接存活期 | 若大于后端自己的 idle 超时 → `upstream prematurely closed connection`（偶发 502） |
| `client_max_body_size` | `1m` | 请求体上限 | 大上传直接 413；把它调大要同时看 `client_body_temp_path` 的磁盘 |
| `client_body_timeout` | `60s` | 两次读请求体之间的间隔 | 慢速上传（移动网络）会被误杀 |
| `sendfile` / `tcp_nopush` | `off` / `off` | 静态文件是否走零拷贝 + 攒包 | 静态资源必须开；对代理响应无效（响应体在用户态） |
| `lingering_close` | `on`（`always` 更彻底） | 关闭前是否继续读掉客户端残留数据 | 关掉会让大量半关连接变成 RST，进而污染 upstream 的连接池 |
| `worker_connections` | `512`（打包常改，`nginx -T` 核对） | 每 worker 的连接额度（client + upstream 共用） | 反向代理场景实际容量减半（账二） |
| `worker_shutdown_timeout` | 不设（无强制上限） | 旧 worker 的**最长残留时间** | 不设 = 长连接把旧 worker 拖住；设太小 = 定时处决（事故二） |
| `proxy_next_upstream` / `_tries` | `error timeout` / `0`（不限） | 失败后是否换一台、最多换几次 | `_tries 0` = 无限重试；**非幂等请求的重试必须显式加 `non_idempotent`**（1.9.13+） |
| `max_fails` / `fail_timeout`（upstream） | `1` / `10s` | **被动**健康判定：失败 1 次即冷却 10 s | OSS 没有主动探测；`max_fails=1` 在内网抖一下就会摘掉一台 |

### 一份能同时伺候「短请求」和「长流」的入口配置

```nginx
# Nginx 1.25+/1.27 语法；关键点是两类流量必须分开 location，不要共用一个模板
upstream app_short {
    server 10.2.3.11:8080 max_fails=2 fail_timeout=10s;
    server 10.2.3.12:8080 max_fails=2 fail_timeout=10s;
    keepalive 64;                 # 复用 upstream 连接：省端口、省 TIME_WAIT、省握手
    keepalive_timeout 55s;        # 必须 < 后端 keep-alive idle（常见 60s/65s）
}
upstream app_stream {
    server 10.2.3.11:8081;
    server 10.2.3.12:8081;
    keepalive 32;
}
server {
    listen 443 ssl;
    http2 on;                     # 1.25.1+；更早版本写在 listen 行上
    client_max_body_size 32m;
    keepalive_timeout 65s;        # 故意略小于上游 SLB 的 idle 超时
    location /api/ {
        proxy_pass http://app_short;
        proxy_http_version 1.1;   # 复用 upstream 连接的前提
        proxy_set_header Connection "";
        proxy_buffering on;       # 默认值，短请求要它：慢客户端不阻塞 worker
        proxy_read_timeout 10s;   # 短请求的 SLO 在这里被钉住
        proxy_next_upstream error timeout http_502;
        proxy_next_upstream_tries 2;
    }
    location /v1/stream/ {
        proxy_pass http://app_stream;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;      # ★ 事故一的那一行
        gzip off;                 # gzip 自己也会攒（它是压缩块级输出）
        proxy_cache off;
        proxy_read_timeout 3600s; # 两次读之间，不是总时长
        send_timeout 3600s;
        chunked_transfer_encoding on;
    }
}
```

**两处容易漏掉的细节**：① `proxy_buffering off` 只影响 Nginx 自己，若后端也在框架层缓冲（Servlet 的 `outputStream.flush()` 忘了写），字节还是到不了——所以后端最好显式回 `X-Accel-Buffering: no`，这样**配置的重启不需要碰入口**（§39.10 会把这条写成边界纪律）。② `gzip off` 不是保守，而是必须：压缩算法需要攒够一个块才有压缩比，它天然是第二层 buffer。

---

## 39.5 Envoy 物理形态：listener → filter chain → cluster → endpoint

**一句话**：Envoy 把「入口」拆成了四个可独立配置的对象，并且**每个对象都由控制面（xDS）驱动**（`→ comp/41` 只给这一句，不讲协议细节）。好处是四层能力都变成「数据」而不是「配置文件里的语法」；代价是你必须理解这些对象的**默认值**，因为默认值就是 Envoy 替你做的决策。

### 四个对象与线程模型

| 对象 | 是什么 | 关键状态 | 与 Nginx 的对照 |
| --- | --- | --- | --- |
| **listener** | 一组监听地址 + filter chain 匹配 | accept 连接、TLS、filter chain 选择结果 | ≈ `server` 块 + `listen` |
| **filter chain / HCM** | 每条连接进入的过滤器序列（最常用是 HTTP connection manager） | 每 stream 的解析状态、超时定时器、access log | ≈ `location` 里的指令集合，但可编程 |
| **cluster** | 一组逻辑后端 + 负载均衡策略 + 连接池 + 熔断 + 健康检查 | per-worker 的**连接池**（每个 host 一个池）、熔断计数器 | ≈ `upstream` 块，但多了熔断与 outlier detection |
| **endpoint**（host） | cluster 里的一个具体 `ip:port` | 健康状态（主动 + 被动）、权重、优先级、locality | ≈ `server` 行 |

**线程模型决定了三件运营事实**：Envoy 通常是「1 个 main 线程 + N 个 worker 线程（默认等于核数，`--concurrency`）」，**配置经 xDS 全量下发到每个 worker，并用 RCU 式切换**；连接池与熔断计数器都是 **thread-local**（每个 worker 一套，配置值会被平均分配或按 `per_connection_buffer_limit` 独立生效）；统计先记在线程本地再周期性合并，所以**极短窗口的计数器读数会有 1 个合并周期的滞后**（`stats_flush_interval` 默认 5 s 量级，以目标版本文档为准）。

这条「thread-local」还有两个直接后果：① **熔断阈值是每个 worker 各自的**，所以 `max_connections: 100` 在 8 worker 上并不等于全局 100（除非用 `circuit_breakers` 的 `per_host_thresholds` / runtime 覆盖，**具体语义以目标版本文档为准**，这是最容易算错的一格）；② 连接池是 per-worker 的，所以 **上游看到的连接数 ≈ worker 数 × 每 worker 池里的连接**，而不是「一个客户端一条」。

### 默认值就是决策：Envoy 的一组「会咬人」的默认

| 项 | 默认值（以目标版本文档为准） | 它决定了什么 | 长连接/流式场景要怎么改 |
| --- | --- | --- | --- |
| route `timeout` | `15s` | **一次请求的总时长上限**（HCM 层） | SSE/长轮询/长时间 gRPC 必须显式放大或置 `0`（不设上限） |
| HCM `stream_idle_timeout` | `300s`（5 min） | 一条 stream **没有任何字节流动**的容忍时长 | 心跳间隔必须 < 它；SSE 无心跳会被静默关闭 |
| `request_timeout` | 未设 | 从收到请求头到响应完成 | 与服务端 handler 超时配合使用 |
| cluster `connect_timeout` | `5s` 量级（v3 API 通常显式给） | 建连上限 | 内网压到 0.5–1 s |
| `per_try_timeout` | 未设（继承 route timeout） | 单次重试的上限 | 必须显式设，否则重试会吃满总预算 |
| `num_retries` | `1` | 重试次数 | 与 `retry_on`、重试预算（`retry_budget`）一起配；**重试是放大器**（`→ comp/50` 的同一台放大器） |
| circuit breakers `max_connections` / `max_pending_requests` / `max_requests` / `max_retries` | `1024` / `1024` / `1024` / `3` | 熔断阈值（thread-local） | 高峰要按「单 worker 期望并发」重算，别照抄文档 |
| outlier detection `interval` / `base_ejection_time` / `max_ejection_percent` | `10s` / `30s` / `10` | 被动摘除的节奏与**护栏** | `max_ejection_percent` 就是事故三缺的那个东西 |
| `consecutive_5xx`（需显式配置） | 无默认 | 连续多少个 5xx 摘除 | 与 `consecutive_gateway_failure` 分开配（502/503/504 通常另算） |
| HTTP/2 `max_concurrent_streams` | 极大值（≈ 不限制，实际由对端 SETTINGS 决定） | 一条连接上能跑多少并发 stream | 必须显式压到 100 量级，否则单连接故障域过大（`→ comp/40`） |

### 一次请求在 Envoy 里的路径（含熔断与重试的位置）

| 跳 | 动作 | 关心的计数/超时 | 失败后的第一反应 |
| --- | --- | --- | --- |
| ① | listener accept（可选 `reuse_port`） | `downstream_cx_active`、`listener_manager.connections_accepted` | accept 队列满 → SYN 丢（表现为客户端重试而非 5xx） |
| ② | TLS 握手 / filter chain 匹配 | `ssl.handshake`、握手 CPU | 握手失败 → 直接 reset，不计入 5xx（**指标缺口**） |
| ③ | HCM 解析 + 路由匹配 | `route.timeout` 定时器开始计时 | 无匹配 route → 404/直接 reset |
| ④ | **熔断判定**（cluster 的 pending/active 额度） | `upstream_rq_pending_active`、`circuit_breakers.*.remaining` | 超限 → 立刻回 503（**不排队**，这是 Envoy 与 Nginx 语义最大的差别之一） |
| ⑤ | 从 per-worker 连接池取连接（HTTP/1.1）或开新 stream（HTTP/2） | `upstream_cx_*`、`upstream_rq_active` | 取不到连接 → 进 pending 队列（受 `max_pending_requests` 限制） |
| ⑥ | 发出请求，等响应 | `per_try_timeout` / `route.timeout` | 超时 → 关连接 + 可能触发重试 |
| ⑦ | 记录结果 → outlier detection 计数 | `upstream_rq_5xx`、`ejections_active` | 连续 5xx 达到阈值 → **摘除**（带 `base_ejection_time × 连续次数` 的指数退避） |
| ⑧ | access log / stats flush | `stats_flush_interval` | 指标有滞后，别用 1 s 窗口判断瞬时故障 |

**第 ④ 跳是全篇最重要的语义差**：Nginx 的 `limit_conn`/`worker_connections` 打满时表现为**排队或拒绝新连接**，而 Envoy 的熔断打满时表现为**立刻 503（fail fast）**。前者让延迟无界上升（用户一直等），后者让错误率瞬间归零再爆表。选哪一种取决于你的客户端有没有超时与重试——**没有超时预算的客户端配 fail-fast 入口，是最差的组合**（会让重试风暴在原地打转）。

---

## 39.6 负载均衡算法：选谁、怎么算健康、摘除与恢复的时间常数

### 六种算法与它们的失效形态

| 算法 | 一句话 | 适合 | 失效形态（一定要背下来） |
| --- | --- | --- | --- |
| **RR / WRR** | 轮询 / 加权轮询 | 后端同质、请求耗时方差小 | 长请求 + 短请求混跑时「瞬时在途数」不均；慢节点被均匀地喂满 |
| **least_conn**（Nginx）/ `LEAST_REQUEST`（Envoy） | 选当前在途最少的 | 请求耗时方差大（最常见的好默认） | 「在途少」不等于「快」：一个卡在 IO 上的节点在途数会先降后升，短期看起来最闲 |
| **EWMA**（Envoy 的 `LEAST_REQUEST` 可配 succeed-rate 加权，或客户端侧 p2c-EWMA） | 用响应时延的指数加权移动平均打分 | 后端性能异构（有的机器就是慢 20%） | 需要足够采样量；冷启动期分数抖动，容易把新节点打进「黑洞」 |
| **一致性哈希**（ring hash / Maglev） | 按 key 落到环上，节点增减只影响邻近一段 | 缓存亲和、有状态分片、会话粘 | 热点 key 打爆一个节点；**负载不均而不是不粘**（键分布 ≠ 负载分布） |
| **会话保持**（cookie / IP hash / ipvs `-p`） | 把同一会话钉到同一台 | 有本地 session 的老应用 | 节点故障时那批会话全失效；扩缩容时「粘住的那批」不迁移 |
| **随机（p2c）** | 随机挑两个，取较好的那个 | 大规模无状态服务，实现最省 | 需要「能比较」的指标（在途数/时延），退化成纯随机时表现接近 RR |

**一句话选择法**：无状态且耗时均匀 → RR/WRR；耗时方差大 → least_conn / p2c-EWMA；有缓存命中率诉求 → 一致性哈希（并且要监控**每节点请求量的标准差**，而不是只看总量）；老应用要 session → 优先改造成无状态，会话保持只当过渡。

### 健康检查：主动与被动的分工，以及数字从哪来

| 类型 | 谁在做 | 判据 | 检测延迟（越线到摘除） | 恢复延迟（越线到接流量） | 误判风险 |
| --- | --- | --- | --- | --- | --- |
| **被动**（passive） | 数据面自己看真实请求结果 | Nginx `max_fails`/`fail_timeout`；Envoy outlier detection 的 `consecutive_5xx` | `fail_timeout` 内累计 `max_fails` 次 → 立刻摘，冷却 `fail_timeout`；Nginx 默认 `1` 次 / `10s` ⇒ **一次抖动就摘 10 s** | 冷却期结束自动挂回（无探测） | 低（判据是真实流量），但恢复盲目 |
| **主动**（active） | 独立的探测循环 | keepalived `interval`/`fall`/`rise`；Envoy health check `interval`/`unhealthy_threshold`/`healthy_threshold` | `interval × fall`（事故三：2 s × 3 = **6 s**） | `interval × rise`（2 s × 3 = **6 s**） | **高**：探测与被测流量共用资源时会被一起饿死（事故三） |
| **主动 + 被动**（推荐） | 两者都开，取并集 | Envoy / Nginx Plus / 带 sentinel 的客户端 LB | 被动为主（快），主动兜底（覆盖「没有流量所以没有被动信号」的节点） | 主动探测负责恢复，避免「没流量 → 永远不恢复」 | 中（要做护栏） |

**三个必须配的护栏**（缺一个是事故三的变体）：
1. **摘除上限**：Envoy `max_ejection_percent`（默认 10）；Nginx Plus 类似；**ipvs + keepalived 没有这个护栏**，必须自己在脚本里加「至少保留 N 台」。
2. **探测路径与被测路径分离**：`/healthz` 独立端口、独立 server 块、不查外部依赖（要查依赖就单开一个 `/healthz/deep` 只用于告警，不用于摘除）。
3. **恢复要有梯度**：恢复不是「一上来就全量」，Envoy 用 `slow_start`、Nginx 用 `weight` 缓慢抬升、客户端用小比例灰度。

### 摘除与恢复的时间常数（算一次事故的暴露窗口）

假设：单节点 QPS = 1,200（6 台共 7,200 QPS），被动检查 `max_fails=1 / fail_timeout=10s`，主动检查 `interval=2s / fall=3 / rise=3`。

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 被动摘除的检测窗口 | 第一次失败即摘（`max_fails=1`） | **≈ 0 s**（下一条请求就换节点） |
| 2 | 被动挂回的风险 | 冷却 `fail_timeout=10s` 后不问证据直接挂回 | 若根因未修，**每 10 s 重演一次**（抖动型故障的经典曲线） |
| 3 | 主动摘除的检测窗口 | `interval × fall = 2 × 3` | **6 s**，期间该节点仍接流量：`1,200 × 6 = 7,200` 条请求受影响 |
| 4 | 主动恢复的窗口 | `interval × rise = 2 × 3` | **6 s**（若加 `slow_start` 再放大 3–10 倍） |
| 5 | 全摘的极端情况 | 6 台同时满足 `fall` → RS = 0 | 暴露窗口 = 「发现 + 人工介入」，事故三为 **16 min** |
| 6 | 若把 `fall` 调到 2、`interval` 调到 1 s | 检测窗口 2 s，但误摘率上升（1 s 内两次超时在内网抖动中很常见） | 检测快了 3 倍，误摘多了 1 个量级 |

> **量级结论（非承诺值）**。假设清单：① 每节点 QPS 均匀（一致性哈希下不成立）；② 探测请求本身不产生可观的额外负载（`interval=1s × 6 节点` 才 6 rps，可忽略）；③ 恢复后节点立刻能以满速服务（未开 `slow_start`）；④ 摘除是「立刻生效」的——**实际还要加上控制面传播延迟**（keepalived 到内核 ipvs 是毫秒级，K8s 场景下 EndpointSlice → kube-proxy/Envoy 的传播是秒级，§39.8）。换 `fall/rise/interval` 只需重算第 3、4 步。

---

## 39.7 长连接与超时链：从客户端一路倒推

### 入口上其实有六条「超时」

| 超时 | 谁在计 | 计的是什么 | 默认值量级 | 越线的表现 |
| --- | --- | --- | --- | --- |
| 客户端读超时 | 客户端/SDK | 两个字节之间的间隔 或 整个请求 | 由业务定（常见 30 s / 60 s） | 客户端先断 → 入口记 **499**，日志里没有 5xx |
| 入口 read 超时 | Nginx `proxy_read_timeout` | **两次成功读之间** | 60 s | 504（网关超时），但上游可能还在跑 |
| 入口 connect 超时 | `proxy_connect_timeout` | 建连 | 60 s | 502 |
| 入口 send 超时 | `send_timeout` / `proxy_send_timeout` | 向客户端/上游**写**的间隔 | 60 s | 499/关闭，客户端看到截断 |
| 入口 idle 超时 | `keepalive_timeout` | 空闲 keepalive 连接存活 | 75 s（http 级）、60 s（upstream 级） | 复用连接被对端关掉 → 偶发 502/`prematurely closed` |
| Envoy stream idle | `stream_idle_timeout` | stream 无字节流动 | 300 s | 长流被静默关闭（**没有 5xx**） |

```text
   超时预算必须倒推，不能各层自定（例：目标端到端 P99 ≤ 800 ms 的同步接口）

  客户端预算 1200ms ─┐
                     ├─ 入口 route timeout 800ms ─┬─ 服务 handler 600ms ─┬─ 依赖 A 250ms
                     │  (还要 > 下游总预算)        │ (还要 > 依赖之和)     ├─ 依赖 B 250ms
                     │                            │                       └─ DB 120ms
   规则一：每一层的超时 ≥ 下游各段之和 × 1.3
   规则二：客户端预算 ≥ 入口超时 × 1.5（留出重试与网络抖动）
   规则三：长连接把"总时长超时"换成"间隔超时"：
           SSE/WebSocket 没有"总时长"概念，只有"两次写之间隔多久" ⇒
           心跳间隔 15s → proxy_read_timeout ≥ 45s（3×）→ stream_idle_timeout ≥ 45s
   ★ 违反规则一的典型现场：入口 60s、服务 30s、DB 5s，
     但客户端 10s —— 用户永远看不到那条 504，只看到"请求超时，请重试"。
```

### SSE / WebSocket / gRPC 三种长连接，各自的特殊点

| 形态 | 入口必须做什么 | 最容易踩的默认值 | 观测上的特征 |
| --- | --- | --- | --- |
| **SSE**（HTTP 流） | `proxy_buffering off`、`gzip off`、后端 `X-Accel-Buffering: no`、`proxy_read_timeout` 放到小时级、心跳 15 s | `proxy_buffers 8 8k`（攒批，事故一）、`proxy_read_timeout 60s` | 应用侧 `Write()` 很早，客户端首字很晚；`request_time` 等于整段时长 |
| **WebSocket** | `proxy_http_version 1.1` + `Upgrade`/`Connection` 头透传、`proxy_read_timeout` 放大 | 少了 `Upgrade` 头 → 握手直接 400/426 | `101` 出现的比例；每秒 `active connections` 阶梯变化 |
| **gRPC / HTTP-2 流** | 必须走 HTTP/2 到上游（`grpc_pass` 或 Envoy `http2_protocol_options`）、`max_concurrent_streams` 压到 100 量级、`stream_idle_timeout` 放大 | 入口降级成 HTTP/1.1 → gRPC 直接 502；`stream_idle_timeout 300s` 掐掉长流 | `RST_STREAM` 计数、`upstream_rq_tx_reset` |

### 一个把「读取超时」和「业务心跳」对齐的服务端骨架

```go
// 服务端：SSE 心跳 + 主动 flush + 可观测的"第一个字节时间"
func streamHandler(w http.ResponseWriter, r *http.Request) {
    w.Header().Set("Content-Type", "text/event-stream")
    w.Header().Set("Cache-Control", "no-cache")
    w.Header().Set("X-Accel-Buffering", "no") // 双保险：即使入口忘了 proxy_buffering off
    flusher, ok := w.(http.Flusher)
    if !ok {
        http.Error(w, "streaming unsupported", http.StatusInternalServerError)
        return
    }

    // 心跳必须 < min(入口 proxy_read_timeout, Envoy stream_idle_timeout) / 3
    hb := time.NewTicker(15 * time.Second)
    defer hb.Stop()

    ctx := r.Context()
    for {
        select {
        case <-ctx.Done(): // 客户端断开：这里才是"客户端走了"的唯一可靠信号
            return
        case <-hb.C:
            fmt.Fprint(w, ": keepalive\n\n") // 注释帧，不污染事件流
            flusher.Flush()
        case chunk, ok := <-upstreamChunks:
            if !ok {
                fmt.Fprint(w, "event: done\ndata: {\"ok\":true}\n\n")
                flusher.Flush()
                return
            }
            fmt.Fprintf(w, "data: %s\n\n", chunk)
            flusher.Flush() // 没有这一行，框架层会替你攒批
        }
    }
}
// 判读要点：日志里要打两个时间戳——handler 开始时间、第一次 Flush 时间。
// 两者之差 ≈ 入口/框架的缓冲延迟；这一对数在事故一里是 180ms 与 37.6s。
```

---

## 39.8 优雅重载与发布：连接归属问题，不是热加载问题

### 三种「发布」各自的物理语义

| 发布方式 | 它到底做了什么 | 对已有连接 | 新连接去哪 | 掉不掉连接 |
| --- | --- | --- | --- | --- |
| **`nginx -s reload`** | master 起一批新 worker（读新配置），旧 worker 停止 accept | 旧 worker 继续服务，**不迁移** | 新 worker | 不主动断，但 **idle keepalive 会被立刻关闭**；旧 worker 若配了 `worker_shutdown_timeout` 到点强杀 |
| **Envoy 配置热更新（LDS/CDS）** | 新 listener 接管，旧 listener 进入 drain 序列 | 按 `drain_time` 逐步关闭；`drain_type` 决定「只停新连接」还是「连老连接一起关」 | 新 listener | 可控：drain 窗口内不杀在途 stream |
| **Pod 滚动发布（K8s）** | ① EndpointSlice 标 `terminating` → ② 各层停止送新流量 → ③ `preStop` → ④ SIGTERM → ⑤ `terminationGracePeriodSeconds` 到点 SIGKILL | 应用侧要自己 drain | 其他 Pod | **取决于 ①②③④ 的顺序与耗时**，最常见错在「先 SIGTERM 后摘流量」 |

```bash
# 重载前后各抓一次，把"连接掉没掉"变成可读的数字（不要只看 reload 的退出码）
# ① 当前配置里三个决定性参数
nginx -T 2>/dev/null | grep -E 'worker_(connections|shutdown_timeout|processes)|keepalive_timeout'
# ② 重载前的连接总数与 worker 世代
ss -tni state established '( sport = :443 )' | wc -l
ps -o pid,ppid,etime,cmd -C nginx
# ③ 重载（退出码 0 只代表"新配置语法通过"，不代表旧 worker 退出了）
nginx -t && nginx -s reload && sleep 1
# ④ 旧 worker 是否还在、各挂了几个 fd（fd 数 ≈ 连接数 + 文件）
for p in $(pgrep -f 'nginx: worker'); do
  printf 'worker %s alive=%ss fds=%s\n' "$p" \
    "$(ps -o etimes= -p "$p" | tr -d ' ')" "$(ls /proc/$p/fd 2>/dev/null | wc -l)"
done
# ⑤ 结论口径：影子 worker 的 etimes 还在涨 ⇒ 长连接把它拖住了；
#    reload 后 1s 内 established 减少的比例 ⇒ 被立刻关掉的 idle keepalive 比例（事故二的 4%）。
```

### 「发布掉不掉连接」的判定表

| 条件 | 会掉连接吗 | 为什么 |
| --- | --- | --- |
| 有 `worker_shutdown_timeout`，且流的寿命 > 该值 | **会**（到点强杀） | 旧 worker 被 master 强制终止，TCP 只能 RST/FIN |
| 无 `worker_shutdown_timeout`，且存在永不结束的流 | **不会，但更糟** | 影子 worker 长期存活，旧配置继续服务（事故二：那条流活了 3 h 40 m） |
| 客户端只做短连接（无 keepalive） | 基本不会 | 连接本来就随请求结束 |
| 客户端有 keepalive 但发布瞬间空闲 | **会**（连接被关） | 旧 worker 退出时立刻关闭 idle 连接；客户端若无抖动重连 → 风暴 |
| K8s 里先 SIGTERM 再摘 EndpointSlice | **会**（且是雪崩式） | 流量还在进来、进程已在退出，期间的请求全部 502 |
| 摘 EndpointSlice 但 kube-proxy/Envoy 还没传播（秒级） | **会**，窗口 = 传播延迟 | 控制面到数据面的传播不是原子的（`→ comp/38` 的同一类问题） |
| Envoy drain 且 `drain_time` ≥ 最长流寿命 | 不会（在途 stream 保住） | drain 是「停止接受新 stream + 等在途结束」 |
| 客户端重连**无抖动** | 掉的不多，但**恢复慢** | 万级客户端同秒重连 → TLS 握手打满 + accept 队列溢出（事故二：恢复 90 s） |

**四条纪律**（把它们当发布检查单）：① 长连接的发布单位是「按连接批量」，不是「配置 reload」——能滚动就滚动（一台一台 drain）；② `worker_shutdown_timeout` 必须 **≥ 最长流寿命**，否则是定时处决；③ 客户端重连必须带 `rand(0, 5s)` 抖动 + 指数退避；④ 发布期间用「每 upstream 可用端点数 + 499/5xx 增量」当门禁，而不是看 reload 退出码。

---

## 39.9 三本账：连接内存、并发上限、重载掉连接

### 账一 · 连接内存 = 连接数 × 每连接 buffer

假设（现场口径，事故二的机器）：60,000 条 SSE 长连接、TLS 1.3、`worker_connections 1024 × 8 worker`、`ssl_buffer_size 16k`（默认量级）、`client_header_buffer_size 1k`、`proxy_buffer_size 4k|8k`、响应体按 `proxy_buffering off` 流式。

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 每连接进程内固定开销 | `ngx_connection_t` + 读事件 + 该连接上的 `ngx_http_request_t` + 头缓冲（1k）+ `proxy_buffer_size`（8k，仅响应头） | **10–14 KB/连接** |
| 2 | TLS 写缓冲 | `ssl_buffer_size` 默认量级 16k，长流下几乎总在用小 buffer 发送 | **+16 KB/连接**（可调到 4k，代价是 TLS record 变多） |
| 3 | 若 `proxy_buffering on`（事故前的错误配置） | 每请求 `proxy_buffers 8 × 8k` = 64 KB 上限，慢客户端会吃满 | **+64 KB/连接** |
| 4 | 进程内合计（正确配置） | `60,000 × 26 KB` | **≈ 1.5 GB** |
| 5 | 内核侧 socket 缓冲 | `ss -m` 口径，长流低速下行时每连接仍占 8–16 KB（含 TLS 应用侧待发 + sk_buff） | `60,000 × 12 KB ≈ 0.7 GB` |
| 6 | 总计（正确配置） | `1.5 + 0.7` | **≈ 2.2 GB**（8 核 16 GB 机器剩 13.8 GB 给应用） |
| 7 | 总计（若 buffering on + 慢客户端占三成） | `1.5 + 64 KB × 18,000 ≈ 1.15 GB → 2.65 + 0.7` | **≈ 3.4 GB**，且**长尾**：单个慢客户端可吃满 64 KB |
| 8 | 反向验算：单机 20 万连接（事故三的峰值） | `200,000 × 26 KB` | **≈ 5.2 GB** 仅 Nginx 侧 → 加上应用与内核，直接 OOM 边缘 |

> **量级结论（非承诺值）**。假设清单：① 每连接进程内 10–14 KB 来自「RSS 增量 ÷ 连接数」实测口径，随版本、模块（`--with-*`）、`ssl_buffer_size` 有 ±40% 浮动；② 内核侧按 `ss -m` 的 `skmem` 读数，减少 `tcp_rmem/sndbuf` 可压缩但会影响吞吐；③ 未计 `proxy_cache`/`gzip` 的额外映射；④ 连接分布均匀，忽略「热点 worker 单核内存更高」。**换 `ssl_buffer_size` 4k 或关 TLS（内网回源）能把账一打掉 40%**——这是长连接入口最划算的一个旋钮。

### 账二 · 并发上限 = worker 数 × 每 worker 事件容量

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 理论连接额度 | `worker_processes 8 × worker_connections 1024` | 8,192 个额度 |
| 2 | 反向代理的额度消耗 | 每条在途请求占「client + upstream」两个额度 | **4,096 个并发请求/长连接** |
| 3 | 文件描述符需求 | 8,192 + listen/日志/临时文件 ≈ `worker_connections × 2` 的安全余量 | `worker_rlimit_nofile ≥ 16384`（`65535` 更稳） |
| 4 | **端口耗尽（最容易先撞的墙）** | upstream 若用短连接：`ip_local_port_range` 默认约 28,232 个端口 ÷ `TIME_WAIT` 60 s | **单台后端 ≈ 470 新建/s**；6 台 ≈ 2,820/s |
| 5 | 配 `keepalive 64`×6 后端之后 | 复用连接，端口不再是瓶颈，上限回到第 2 步 | 端口压力降 1–2 个数量级 |
| 6 | TLS 握手 CPU 上限 | 8 核 × 1,200–2,000 握手/s/核（ECDHE-RSA 2048，量级口径） | **≈ 1–1.6 万握手/s** |
| 7 | 长连接的每事件成本 | 60,000 条 × 每 15 s 一次心跳，即 4,000 事件/s × 约 5 μs | ≈ 0.02 CPU·s/s（**长连接几乎不吃 CPU，吃内存与 fd**） |
| 8 | 结论：撞墙顺序 | fd（第 3 步）→ 端口（第 4 步）→ 额度（第 2 步）→ CPU（第 6 步） | 长连接场景：**内存最先撞**（账一），其次是非 `keepalive` 的端口 |

> **量级结论（非承诺值）**。假设清单：① `worker_connections` 默认 512，示例已调到 1024（打包默认值各异，`nginx -T` 核对）；② 反向代理「一请求两额度」在 HTTP/1.1 到上游时成立，HTTP/2 上游会改变这条算术（`→ comp/40`）；③ TLS 握手成本随密钥类型（ECDSA 快一个量级）、会话复用率（`ssl_session_cache` 命中率可达 80%+）大幅变化；④ 未计 `accept` 队列（`backlog`）与 `somaxconn` 的溢出丢包，它会把「上限」表现为客户端重试而不是 503。

### 账三 · 重载掉连接数 = 长连接数 × 旧 worker 存活时长

本账的口径必须写清楚：**「长连接数」是「单位时间内落到旧 worker 上的连接数（条/s）」，乘上「旧 worker 的存活时长（s）」，得到「旧 worker 退出那一刻还挂在它身上的连接数」**。在稳态下，它与「在线长连接总数」在数量级上相等（因为连接寿命 >> 发布间隔），所以现场可以直接用在线总数代入——这正是事故二里 6 万条的用法。

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 在线长连接 | 60,000 条，均匀分布在 8 worker（旧世代全员持有） | 7,500 条/worker |
| 2 | reload 的立即损失 | 旧 worker 关掉 idle keepalive，实测约 4% | **2,400 条立即断** |
| 3 | 影子 worker 存活时长（无 `worker_shutdown_timeout`） | 由最长流决定：实测 3 h 40 m（那条永不结束的监控流） | **13,200 s** |
| 4 | 该窗口内旧 worker 承接的新连接 | 只要旧 worker 还 accept 就会继续收（reload 后它不再 accept，所以此项为 0；**但滚动重启应用时不成立**） | 0 |
| 5 | 掉连接数（设 `worker_shutdown_timeout 30s`） | 存活时长被截到 30 s，所有寿命 > 30 s 的流全断 | **≈ 60,000 条/次发布** |
| 6 | 掉连接数（把窗口按台拆开滚动） | 一次只 drain 1 台，其余 5 台正常 | `60,000 ÷ 6 ≈ 10,000 条/次` |
| 7 | 重连风暴的 CPU | `10,000 × 1.2 ms`（TLS 握手） | **12 CPU·s**，8 核机器 ≈ 1.5 s 满载 |
| 8 | 若客户端无抖动、全量同时重连 | `60,000 × 1.2 ms = 72 CPU·s` → 8 核 ≈ 9 s 满载，`backlog 511` 溢出 → 退避 1/3/7 s | **恢复 90 s**（事故二实测） |
| 9 | 结论 | 掉连接数 ≈ 灵敏度由「drain 粒度」和「客户端抖动」共同决定，**而不是由 reload 本身决定** | 见下方量级结论 |

> **量级结论（非承诺值）**。假设清单：① 连接在 worker 间近似均匀；② 客户端 TLS 会话不复用（`session ticket` 命中可把第 7、8 步打掉 60%–80%）；③ 握手成本按 1.2 ms CPU 估（ECDHE-RSA，较新 Xeon，量级口径，换 ECDSA 或硬件加速可低一个量级）；④ 忽略上游 SLB 的 idle 超时对重连的叠加（它会额外制造 4%–10% 的断连）；⑤ 无 `worker_shutdown_timeout` 时「不掉连接」是假象——是「旧配置继续服务」，这在权限/路由类配置变更上是**比掉连接更危险**的失效。换任何一个假设，只需重算第 5–8 步。

```python
# 三本账压成一个可复算脚本（改假设直接重跑）
def acct1_conn_mem(n, per_conn_kb=26, kernel_kb=12, buffered_kb=64, slow_ratio=0.30):
    proc = n * (per_conn_kb + buffered_kb * slow_ratio) / 1e6      # GB
    kern = n * kernel_kb / 1e6
    return proc, kern, proc + kern

def acct2_capacity(workers=8, worker_conns=1024, backends=6,
                   port_range=28232, tw_s=60, handshakes_per_core=1500, cores=8):
    slots = workers * worker_conns // 2                            # 一请求两额度
    port_per_backend = port_range / tw_s                           # 短连接上游的端口天花板
    tls_per_s = cores * handshakes_per_core
    return slots, port_per_backend * backends, tls_per_s

def acct3_reload_drop(n=60000, drain_s=None, stream_life_s=13200,
                      idle_ratio=0.04, tls_ms=1.2, cores=8, backlog=511):
    immediate = n * idle_ratio                                     # idle keepalive 立刻被关
    window = min(stream_life_s, drain_s) if drain_s else stream_life_s
    killed = n if drain_s and stream_life_s > drain_s else 0       # 到点强杀的长流
    cpu_s, recover_s = n * tls_ms / 1000, None
    full_load_s = cpu_s / cores
    return immediate, window, killed, cpu_s, full_load_s

for n in (60_000, 200_000):
    p, k, t = acct1_conn_mem(n)
    print(f"连接 {n:,} ⇒ 进程内 {p:.1f} GB + 内核 {k:.1f} GB = {t:.1f} GB")

print("额度/端口/握手上限:", acct2_capacity())
for drain in (None, 30, 300):
    print(f"drain={drain} ⇒ (立即断, 窗口s, 强杀条数, 重连CPU·s, 满载s) =",
          acct3_reload_drop(drain_s=drain))
```

---

## 39.10 架构边界：入口不能做业务鉴权、不能重试所有请求、不能缓存一切

| 想要的能力 | 入口给不给 | 形态层面的原因 | 正确落点 |
| --- | --- | --- | --- |
| 四层吞吐、Mpps 级转发 | ✅ 只有 LVS 给 | 它不解析请求，天然零状态 | 大流量 TCP 入口、TLS passthrough |
| 按 Path/Header 灰度 | ✅ 七层给 | 只有七层看得见请求边界 | Nginx `map`/Envoy route |
| **业务鉴权（谁能看哪条数据）** | ❌ 不该给 | 入口只能验「票据是否有效」，验不了「这条数据归不归他」；把数据级判权放在入口 = 把授权逻辑复制 N 份 | 入口只做认证 + 粗粒度限流，细粒度判权在服务（`→ comp/47` 的凭据与令牌） |
| **重试所有请求** | ❌ 危险 | 重试是**放大器**：入口重试 × 客户端重试 × 服务内部重试 = 乘积；且非幂等请求重试会产生重复副作用 | 只对 `error/timeout` 且**显式标注幂等**的请求重试；预算用重试预算（`→ comp/50` 的同一台放大器） |
| **缓存一切响应** | ❌ 危险 | 入口缓存按 URL 键，不认业务身份 → 越权（把 A 的响应发给 B）；且 `Set-Cookie`/`Vary` 常被写错 | 只缓存静态资源与真正的公共 GET；`Cache-Control: private` 一律不缓存 |
| 统一 TLS 卸载 + 证书轮转 | ✅ 入口的本职 | 证书集中管理 | 入口（`→ comp/47` 的轮转纪律） |
| 请求级观测（trace 注入、耗时分解） | ✅ 但要防高基数 | 入口是唯一知道「全链路起止」的一层 | 采样 + 低基数标签（`→ comp/48`） |
| 跨地域容灾（多活切流） | ⚠️ 部分 | 入口能切 DNS/VIP，但**存量连接不迁移** | 连接层切流 + 客户端重连抖动 |

**四层与七层的故障域差异（一句话）**：四层故障是**「整条连接的寿命」**级别——摘一台机器 = 断它身上所有连接，影响面 = 该机器的连接数；七层故障是**「单个请求」**级别——但七层多了一整类「部分失败」：缓冲、重试、超时、路由规则、熔断阈值，每一个都能让「入口活着、请求错了」。**所以四层的 SLO 看可用性（连接能不能建），七层的 SLO 必须看请求级的 5xx/499 与耗时分位。**

**三条边界纪律**：① 入口的配置变更与业务发布**共用同一个变更窗口**是最大的风险来源（事故二的发布同时改了入口超时）；② **任何「入口兜底」的能力都要问一句「它的判据是什么」**——判据来自真实请求（被动）就便宜且准，来自探测（主动）就要独立资源；③ **缓冲是入口唯一一个「静默改变语义」的开关**：它不改状态码、不改路由、不加日志，只把「实时」变成「批量」，所以它只能在「明确知道自己在做流式」的 location 里被关掉，而不能靠「记得关」。

---

## 39.11 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到形态） | 怎么定位 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 接口 200、`request_time` 正常，但用户「等半分钟才出字」（事故一） | `proxy_buffering on` + `proxy_buffers 8 8k`：响应体要攒满一个 buffer 才交给客户端 | 应用侧第一次 `Write()` 时间戳 vs 客户端首字时间戳，差值 ≈ buffer 大小 ÷ 出字速率 | 该 location `proxy_buffering off; gzip off; proxy_cache off;`，后端回 `X-Accel-Buffering: no` | 入口配置模板按「短请求 / 长流」分成两套，流式路径必须显式声明；CI 里 grep `/stream` 的 location 是否含 `buffering off` |
| reload 后偶发 502 `upstream prematurely closed connection` | upstream `keepalive_timeout`（60 s）> 后端 keep-alive idle（常见 5–15 s 或 60 s 临界）：复用到一条对端正在关的连接 | 502 的 `upstream_addr` 是否集中在某些后端；抓包看是否在请求发出后立刻收到 FIN | upstream 块的 `keepalive_timeout` 压到 55 s 以下，并对幂等请求开 `proxy_next_upstream` | 把「入口 idle 超时 < 后端 idle 超时」写进接入规范（差 ≥ 5 s） |
| 发布后 3%–8% 客户端断连并重连风暴（事故二） | reload 立刻关闭 idle keepalive；旧 worker 不迁移长连接 | `ss -tn state established` 在 reload 前后 1 s 的差值；worker 世代的 `etimes` | 滚动 drain（一次一台）+ 客户端重连抖动 + `worker_shutdown_timeout ≥ 最长流寿命` | 发布检查单：drain 粒度、抖动、门禁指标（499/5xx 增量）三项 |
| 旧 worker 活了几个小时，`ps` 里两个世代并存 | 未配 `worker_shutdown_timeout`，长流把旧 worker 拖住 | `ps -o etimes,cmd -C nginx`；worker 数 > `worker_processes` | 配 `worker_shutdown_timeout 300s`；对「永不结束」的流设最大寿命 | 监控「worker 世代数 > 1 持续 5 min」告警 |
| LVS 后端全被摘，VIP 还在但 502/连接被 RST（事故三） | 健康检查与被检流量共享 `worker_connections`/listen 端口；`fall 3` 无护栏 | `ipvsadm -Ln` 看 RS 数量；被摘机器的 `/healthz` 自身能否连通 | `/healthz` 独立端口 + 独立 server；`worker_connections` 与 `worker_rlimit_nofile` 重算 | 「至少保留 N 台」的摘除护栏 + 四层探活只表示「机器活着」的文档化 |
| 大文件上传偶发 413 / 502 | `client_max_body_size 1m` 默认值；或 `client_body_temp_path` 磁盘满 | 413 的 `request_length`；`df` 看 temp 盘 | 按接口设 `client_max_body_size`；temp 盘独立并留 20% 余量 | 上传类接口单独 location，参数与磁盘一起评审（`→ comp/42` 的 multipart） |
| gRPC 全部 502、日志里没有上游请求 | 入口到上游降级成 HTTP/1.1（未配 `grpc_pass` / `http2_protocol_options`），gRPC 需要 HTTP/2 | `upstream_response_time = -`；抓包看是否有 `PRI * HTTP/2.0` | 改用 gRPC 专用 upstream 并开 HTTP/2 | 协议矩阵进配置模板（HTTP/1.1、h2、h2c 各一套 upstream） |
| 长流每 5 分钟整点断一次 | Envoy `stream_idle_timeout` 默认 300 s，期间无字节流动 | 断连时间戳的间隔是否恒为 300 s；`stream_idle_timeout` 配置值 | 心跳 < 1/3 阈值；或显式放大/置 0 | 长流接口必须声明心跳间隔，并在入口配置里断言「心跳 < 超时/3」 |
| 5xx 率在故障瞬间「归零」然后爆表 | 入口熔断 fail-fast（503）把请求挡在门口，上游计数器反而干净 | `circuit_breakers.*.remaining = 0`、`upstream_rq_pending_overflow` | 核对熔断阈值是否按「单 worker 并发」算错；客户端要有退避 | 熔断阈值与客户端超时预算一起评审；`remaining=0` 单独告警 |
| 「入口全绿但用户连不上」 | 四层只探机器、七层只探 upstream 超时，没有任何一层探「端到端字节」 | 端到端探针（真实登录 + 一次真实读）作为独立链路 | 加合成探针（synthetic），与入口自身指标并列 | SLO 用「合成事务成功率」，不用「入口存活」 |

---

## 39.12 观测：五个数就能定位 90% 的入口故障

**前提说明**：Nginx 的 `stub_status` 需要显式开 `location = /basic_status { stub_status; }`（默认不开，且**不要暴露在公网**）；`upstream_response_time` 出现在 access log 的前提是配了 `log_format` 里的 `$upstream_response_time`（多 upstream 时是逗号分隔的列表）；Envoy 的指标名随版本演进（`envoy_http_downstream_rq_5xx`、`envoy_cluster_upstream_cx_active` 等），**指标名与字段存在性以目标版本文档为准**。

| 看什么 | 指标 / 命令 | 拐点（越线就动手） | 它度量的是哪件物理事实 |
| --- | --- | --- | --- |
| **① 连接额度水位（第一指标）** | `active`（stub_status）÷ (`worker_processes × worker_connections`)；Envoy `downstream_cx_active ÷ max_connections` | **> 60% 即 P2；> 85% 即 P1**（事故三的前 90 s） | 离「accept 排不进队、健康检查被饿死」还有多远 |
| **② 5xx 与 499 的比值** | `$status` 分布；`upstream_status` 分布 | 5xx > 1% 即 P2；**499 占多数 = 客户端先放弃**（超时链错位，不是服务错） | 错误发生在入口（5xx）还是客户端预算（499） |
| **③ upstream 耗时分位** | `$upstream_response_time` P50/P95/P99；Envoy `upstream_rq_time` | P99 > 下游 SLO × 1.3 即查；**P50 与 P99 同时抬 = 全量慢，只有 P99 抬 = 尾部（GC/单机）** | 慢在入口之外，还是入口自己在排队 |
| **④ `Reading / Writing / Waiting`** | stub_status 三值 | `Writing` 持续高 → 客户端慢（缓冲/反压）；`Reading` 高 → 请求体慢（上传/慢速攻击）；`Waiting` ≈ `active − 其他` 是 keepalive | 三类阻塞的现场分离，比总连接数有用得多 |
| **⑤ 每 upstream 可用端点数** | Envoy `membership_healthy ÷ membership_total`；`ipvsadm -Ln` 的 RS 数 | **任一 cluster 的 healthy 比例 < 50% 即 P1**；= 0 即全站故障 | 事故三唯一能提前报警的数 |
| ⑥ 重试与熔断 | Envoy `upstream_rq_retry`、`upstream_rq_pending_overflow`、`circuit_breakers.*.remaining` | 重试率 > 5% 或 `remaining = 0` 持续 1 min | 放大器的现场读数（`→ comp/50`） |
| ⑦ 缓冲与临时文件 | `proxy_temp_file` 数量、`client_body_temp` 磁盘使用 | 出现持续落盘 = 响应体超 buffer（或上传超内存缓冲） | 「流被攒成批」的另一种证据 |
| ⑧ worker 世代与 fd | `pgrep -c 'nginx: worker'` vs `worker_processes`；`ls /proc/$pid/fd` 的条目数 | 世代 > 1 持续 5 min；fd > `worker_rlimit_nofile × 0.8` | 影子 worker（事故二）与 fd 泄漏 |

```bash
# 值班第一屏：三个数确定"是不是入口的问题"
curl -s http://127.0.0.1/basic_status   # active / Reading / Writing / Waiting
ipvsadm -Ln --stats | awk '/TCP|UDP/{print}' | head -20   # 每 RS 的连接数与流量
awk '{print $9}' /var/log/nginx/access.log | sort | uniq -c | sort -rn | head   # 状态码分布
# 告警表达式骨架（Prometheus + nginx-vts / envoy-exporter）
# nginx_connections_active / (nginx_worker_processes * nginx_worker_connections) > 0.60
# sum(rate(nginx_http_requests_total{status=~"5.."}[5m])) / sum(rate(nginx_http_requests_total[5m])) > 0.01
# histogram_quantile(0.99, rate(nginx_upstream_response_time_bucket[5m])) > 0.8
# envoy_cluster_membership_healthy / envoy_cluster_membership_total < 0.5
# rate(envoy_cluster_upstream_rq_retry[5m]) / rate(envoy_cluster_upstream_rq_total[5m]) > 0.05
# count(nginx_worker_pid) > on(instance) nginx_worker_processes   # 影子 worker
```

---

## 39.13 一页速记

| 记什么 | 具体结论 |
| --- | --- |
| 形态一句话 | 入口 = **四层选连接 + 七层选请求 + 两者共享超时与缓冲**；它是唯一同时持有「buffer」和「定时器」的一层 |
| LVS 四形态 | NAT（往返都过 director）· DR（回包直发，靠 VIP 在 lo + `arp_ignore=1/arp_announce=2`）· TUN（IPIP 封装，注意 MTU）· ECMP（多 director 共享 VIP，五元组哈希把连接钉死） |
| 四层够不够的判据 | 只需要「选机器」→ 够；需要按 Path/Header 路由、HTTP 语义重试、主动摘除、gRPC 多路复用 → 不够 |
| Nginx 关键默认值 | `proxy_buffering on`、`proxy_read_timeout 60s`、`keepalive_timeout 75s`、`client_max_body_size 1m`、`max_fails 1 / fail_timeout 10s`、`worker_shutdown_timeout` 不设 |
| Envoy 关键默认值 | route `timeout 15s`、`stream_idle_timeout 300s`、`num_retries 1`、熔断 `1024/1024/1024/3`（thread-local）、`max_ejection_percent 10` |
| SSE 三件套 | `proxy_buffering off` + `gzip off` + 后端 `X-Accel-Buffering: no`；心跳 < 超时/3 |
| 缓冲的乘法链 | 攒批时长 ≈ buffer 大小 ÷ 出字速率（8 KB ÷ 132 B/s ≈ 62 s） |
| 超时纪律 | 每层 ≥ 下游之和 × 1.3；客户端 ≥ 入口 × 1.5；长连接用「间隔超时」而不是「总时长」 |
| 发布掉连接的三个乘数 | 掉连接数 = 旧 worker 上的连接速率 × 旧 worker 存活时长；再乘「客户端重连无抖动的放大」（60,000 × 1.2 ms ≈ 72 CPU·s → 恢复 90 s） |
| 健康检查的护栏 | 摘除上限（10%）· 探测与被测资源分离 · 恢复要梯度（`slow_start`/权重） |
| 第一指标 | 连接额度水位（`active ÷ 额度`）· 5xx/499 比值 · upstream P99 · `Writing` 数 · 每 upstream 可用端点数 |
| 最危险的一行 | **`proxy_buffering on` 用在流式接口上**：状态码全对、日志全绿、用户全部超时 |
| 换零件的信号 | 需要按请求灰度/重试预算/主动摘除 → 四层换七层；需要千万级 pps 或 TLS passthrough → 七层退回四层 |

**如果只记五句：**

1. **入口的健康指标衡量的是「入口还活着」，不是「字节在动」。** 所以必须有一个端到端合成探针，否则事故三那种「全摘」永远只有用户先知道。
2. **缓冲是唯一一个静默改变语义的开关。** 它不改状态码、不改路由、不加日志，只把「实时」变成「批量」——流式接口必须在配置里显式关掉，不能靠「记得」。
3. **四层的调度单位是连接，七层是请求。** 这一条同时解释了「为什么长连接不能按请求均衡」、「为什么摘一台机器会断它身上所有连接」、「为什么 gRPC 必须上七层」。
4. **reload 的语义只覆盖到 worker 生命周期，不覆盖你的连接。** 长连接的发布单位是「按连接批量 drain + 客户端抖动重连」，`worker_shutdown_timeout` 只是「决定什么时候处决」。
5. **超时必须从客户端一路倒推，且每层留 30%–40% 余量。** 入口最容易悄悄改短它（默认 60 s 常常小于下游 SLO），于是用户看到的是「请求超时，请重试」，日志里只有 499。

---

## 39.14 自检题（5 题）

**Q1.** 一个 SSE 接口：模型 22 token/s、中文约 6 B/token，入口是默认配置的 Nginx（`proxy_buffering on`、`proxy_buffers 8 8k`、`gzip on`），答案是 38 s 流完。请算出「首字延迟」的量级，并说明为什么所有基于 `request_time` 的看板都会把它判成「应用慢」。

<details><summary>答案</summary>

**出字速率** = `22 × 6 = 132 B/s`。**攒批时长** = 第一个 buffer 装满的时间 = `8,192 ÷ 132 ≈ 62 s`。

由于 62 s > 38 s（整段流只流 38 s），**响应体在流结束前从未填满 buffer**，于是 Nginx 在 `proxy_write` 阶段一次性把整段交给客户端 → 首字延迟 ≈ 38 s（实测 37.6 s）。若答案更长（> 62 s），会出现「先吐 8 KB、再长时间静默」的锯齿。

**为什么看板判错**：`$request_time` 从「收到请求头」到「响应写完」，它把「反压/缓冲造成的等待」算在入口自己的时间里，而 `$upstream_response_time` 也包含「上游一直没写完」的时长，两个数都 ≈ 38 s，看起来像应用慢。真正的判据是**应用第一次 `Write()`/`Flush()` 的时间戳**（180 ms）与客户端首字时间戳之差。**修的时候三件事一起做**：入口 `proxy_buffering off` + `gzip off`；后端 `X-Accel-Buffering: no`；应用每个事件后 `flush()`。量级结论，非承诺值。

</details>

**Q2.** 60,000 条长连接、8 worker 的 Nginx 入口，一次发布用 `nginx -s reload`。分别给出「不配 `worker_shutdown_timeout`」与「配 30 s」两种情况下，掉连接数与用户体感的差异，并说明为什么「不配」在权限类变更上更危险。

<details><summary>答案</summary>

**不配**：旧 worker 立即关闭 idle keepalive（约 4%，即 2,400 条），在途长连接继续跑，旧 worker 一直存活到最后一个流结束（事故二实测 3 h 40 m，窗口 13,200 s）。**掉连接数 ≈ 立即 2,400 条 + 后续因内存/发布第二批而被迫重启的那些**；用户体感是「慢慢变卡 + 行为不一致」。

**配 30 s**：旧 worker 30 s 后被 master 强杀，**所有寿命 > 30 s 的流全断 ≈ 60,000 条/次发布**（滚动到 1 台时约 10,000 条）。重连成本 `10,000 × 1.2 ms ≈ 12 CPU·s`；若无抖动全量重连则 `72 CPU·s` → 8 核满载 9 s，`backlog 511` 溢出 → 退避 1/3/7 s → 恢复 90 s。

**为什么「不配」在权限类变更上更危险**：影子 worker 上的连接**继续用旧配置**——如果这次改的是路由、鉴权头透传、灰度比例，那么「一部分用户的请求走旧规则」这种**语义漂移**没有任何指标能发现（连接在、200 也在），而它恰恰是审计与越权类问题的温床。所以正确解不是二选一，而是**按连接批量 drain**：一次只 drain 一台、`worker_shutdown_timeout` 略大于客户端重连窗口、客户端带抖动。量级结论，非承诺值。

</details>

**Q3.** LVS-DR 后面 6 台 Nginx，健康检查 `HTTP_GET /healthz`（`interval 2 / fall 3 / rise 3`），某次峰值 21 万连接时 6 台全部被摘。请给出至少三条因果链，并说明每条该怎么防。

<details><summary>答案</summary>

① **共享稀缺资源**：`/healthz` 与业务共用 listen 端口和 `worker_connections` 配额（反向代理每条连接 2 个额度，8 × 1024 ÷ 2 = 4,096），超限后 `accept` 队列（`backlog 511`）排不进，探测全部超时 → 6 s 内 6 台全摘。**防**：`/healthz` 独立端口 + 独立 server（甚至独立 worker 进程组），且不查任何外部依赖。

② **没有摘除护栏**：ipvs + keepalived 没有 `max_ejection_percent` 概念，你让它摘它就摘光。**防**：脚本里加「至少保留 N 台」/「一次只允许摘 1/3」的护栏，或改用带 `panic threshold` 的七层 LB。

③ **探测判据太粗**：`HTTP_GET` 只看状态码，一台「能返回 200 但连不上数据库」的机器会一直被认为是健康的（反向误判）。**防**：分层——摘除判据用「本机能否服务」（浅探测），深度依赖检查只用于告警（`/healthz/deep`）。

④ **恢复太慢**：`rise 3 × 2 s = 6 s` 加上人工发现 9 min，暴露窗口被拉长到 16 min。**防**：`expire_nodest_conn=1` 主动踢掉指向已摘除 RS 的旧连接 + 恢复走小比例灰度。量级结论，非承诺值。

</details>

**Q4.** 你的 gRPC 服务在入口后面「偶发 502，且日志里 `upstream_response_time` 是 `-`」，同时高峰期 `Writing` 数很高。请给出最可能的两个根因与各自的验证方法。

<details><summary>答案</summary>

① **入口到上游是 HTTP/1.1 而不是 HTTP/2**：gRPC 需要 HTTP/2（`grpc_pass` 或 Envoy 的 `http2_protocol_options`），降级后上游直接判为非法请求，`upstream_response_time` 为空（`-`）。**验证**：`nginx -T | grep -E 'grpc_pass|http2'`；抓包看是否发出 `PRI * HTTP/2.0` 前言；Envoy 侧看 `upstream_cx_http1_active` 是否 > 0。

② **连接复用踩到对端 idle 超时**：upstream `keepalive_timeout`（默认 60 s）大于后端 keep-alive idle（常见 5–15 s），复用到一条正在被关的连接 → `upstream prematurely closed connection`（即 502）。**验证**：502 是否集中在「间隔 > 后端 idle 超时」的请求上；抓包看请求发出后是否立刻收到 FIN；把 `keepalive_timeout` 压到 55 s 以下后是否消失。

③ **`Writing` 高**说明有大量连接在「向客户端或上游写」阶段阻塞：gRPC 场景常见于 `max_concurrent_streams` 配得过大（单连接跑几百条 stream）导致一次流控窗口阻塞全体，或客户端读得慢造成反压。**验证**：`ss -tmi` 看 send-q 与 cwnd；Envoy 看 `upstream_rq_active` 与 `upstream_cx_active` 的比值（> 100 说明单连接复用过度）。量级结论，非承诺值。

</details>

**Q5.** 老板要求「发布时零断连」。请给出你能承诺的 SLO 口径、做不到的部分，以及必须同时改造的三件事。

<details><summary>答案</summary>

**能承诺的口径**：「**在客户端带抖动重连的前提下，单次发布的端到端不可用时间 < 5 s，且 5xx 增量 < 0.1%**」。这不是「零断连」，而是「断连可被客户端在一次抖动窗口内吸收」。

**做不到的部分**：① 任何进程重启都**不迁移**已建立的 TCP 连接——`reload`/drain/热重启都只是「选择什么时候断」，不是「不断」；② 若存在「永不结束」的流（监控、长轮询无心跳），它一定会被 `worker_shutdown_timeout` 处决，除非允许旧 worker 无限存活（那会带来配置漂移，Q2 已论证更危险）；③ 控制面到数据面的传播（EndpointSlice → kube-proxy/Envoy）是秒级且非原子的，窗口内必然有请求打到正在退出的实例。

**必须同时改造的三件事**：① **入口侧按连接批量 drain**——一次一台、`worker_shutdown_timeout` ≥ 客户端重连窗口、`expire_nodest_conn=1`；② **客户端侧**——重连带 `rand(0, 5s)` 抖动 + 指数退避 + 会话（SSE 用 `Last-Event-ID`、WebSocket 用 resumable 协议）**续传**，让断连对用户不可见；③ **门禁**——发布期间看「每 upstream 可用端点数、499/5xx 增量、upstream P99」，任一项越线自动中止发布。三件事缺一件，「零断连」就只是把风险从「入口」挪到「客户端重试风暴」。量级结论，非承诺值。

</details>

---

## 39.15 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 消息族谱：MQ 的推送模型与「背压阀」在不同模型里的相反后果（本篇 §39.4 的缓冲问题同源） | [31 消息族谱：RocketMQ / Pulsar / RabbitMQ / NATS](31-message-family.md) |
| 任务队列语义：可见性超时、幂等键、死信三因子（本篇 §39.7 的超时链与 §39.8 的发布检查单对偶） | [32 任务队列语义：SQS、Redis Streams、可见性超时与死信](32-task-queue-semantics.md) |
| 南北向 RPC 与连接复用：HTTP/2 多路复用、流控与队头阻塞（本篇 §39.5 的 `max_concurrent_streams` 展开） | `→ comp/40` |
| 服务网格与 xDS：控制平面、sidecar 的两跳成本与 iptables 黑盒（本篇 §39.5 只给了一句） | `→ comp/41` |
| 注册中心与配置中心：健康检查误判、推送风暴、配置无灰度（本篇 §39.6 的上游） | `→ comp/38` |
| 协调与共识：director 脑裂、VIP 漂移、会话失效 | `→ comp/37` |
| 投递语义与重复消费：入口重试为什么一定会产生重复 | `→ eng/24` |
| 限流与熔断：入口层限流与重试预算、放大器的三种形态 | `→ eng/15`、`→ eng/18` |
| LLM 网关与配额代理：入口重试在模型调用上的放大（与 §39.5 的 `num_retries` 同构） | `→ comp/50` |
| 连接池与客户端：为什么 20 个连接就够、超时链的真实瓶颈 | `→ comp/44` |
| 对象存储与云盘：大请求体、multipart 与入口 413/502 的另一半 | `→ comp/42` |
| 可观测底座：高基数标签、采集背压（入口指标最容易基数爆炸） | `→ comp/48` |
| K8s 优雅关停：`preStop`、`terminationGracePeriodSeconds` 与应用侧 drain | `→ go/52` |
| 组件故障谱：本篇三起事故在「慢 / 满 / 脑裂 / 假活 / 语义漂移」里的归类 | `→ comp/52` |
| 解剖卡八问、三本放大账与三条纪律（版本常量 / 指标前提 / 量级结论 vs 承诺值） | [0-summary](0-summary.md) §0.4、§0.7 |
