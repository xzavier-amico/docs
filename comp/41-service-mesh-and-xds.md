# 41. 服务网格与 xDS：sidecar 的账

> **状态**：`已完成（初稿）` ｜ **所属**：第八部分 · 入口与东西向通信 ｜ **编号**：41 / 53
> **本篇定位**：全仓唯一一处把「一次普通 RPC 在注入 sidecar 之后到底多走了哪几步、多付了哪几笔钱」拆到 netns、iptables 链、listener 与 xDS 版本号这一层的篇目。南北向入口的 listener/cluster/缓冲链归 [39 入口网关与负载均衡](39-gateway-and-load-balancing.md)，gRPC/HTTP2 的连接复用、流控与重试放大归 [40 RPC 与连接复用](40-rpc-and-connection-reuse.md)，地址列表从哪来、多久刷新归 [38 注册中心与配置中心](38-registry-and-config.md)，探针与优雅关停的应用侧归 [go/52 部署与 SRE](../go/52-deployment-sre.md)，幂等与投递语义归 [eng/24 投递语义](../eng/24-delivery-semantics.md)。本篇一个字不重写它们，只回答一个问题：**把治理从 SDK 挪到代理，你买到了什么、又签下了哪三笔账。**
> **读法**：正被「上网格后变慢」折磨，先读 §41.4 的两跳延迟分解与 §41.9 账一，再看 §41.12 的第一个指标（CFS 限流）；正被「注入后连不上」砸中，直接跳 §41.7 的规则链与排除项；正被「配置一推全站 503」砸中，读 §41.5 的 ACK/NACK 语义与 §41.8 的故障域；做「要不要上网格」的决策，读 §41.2 的推论与 §41.10 的边界表，再用 §41.9 的三本账算钱。只想知道结论的翻 §41.13。

---

## 41.1 三起「上网格后更慢 / 更脆」事故

网格事故有一个共同形状：**业务代码一行没改，指标也没变红，但延迟形状变了、连接行为变了、故障半径变了**。它表现为「注入后 P99 翻倍」「某个命名空间 Pod 全部重启」「一次配置变更 10 秒内全站 503」。根因从来不在业务逻辑里，而在三件事的接缝上：**每一次调用多出来的两跳、一张没人 review 的 iptables 规则链、一份只在语法上被校验过的配置**。

### 事故一：注入 sidecar 之后 P99 翻倍，P50 只涨了 0.6 ms（2026-03-11）

某 SaaS 的订单服务 A（Java / Spring Cloud）调库存服务 B（Go），各 40 个实例，A→B 稳定 6,000 QPS。注入前 A 侧自打点：P50 3.0 ms、P99 12 ms。注入用的是社区 chart 的默认模板，但平台团队为了「代理只是转发、别浪费资源」把 `resources.limits.cpu` 从 chart 默认的 2000m 手工改成了 **200m**（request 100m 未动）。

| 时间 | 事件 | 关键数字 |
| --- | --- | --- |
| 10:02 | 3 个命名空间批量注入 sidecar，业务镜像与配置零改动 | 80 Pod |
| 10:05 | SLO 看板：P50 3.0 → 3.6 ms，**P99 12 → 31 ms**，错误率、成功率都没变 | +0.6 ms / +19 ms |
| 10:20 | 先怀疑 JVM GC，再怀疑 B 的数据库，两边都正常；抓一条链路看，app 自打点与 `istio_request_duration_milliseconds` 几乎重合 | 方向错 40 分钟 |
| 10:35 | 放大 P99 曲线：呈**严格的 100 ms 周期锯齿**——100 ms 正是 CFS 的 `cpu.cfs_period_us` 默认值 | 100 ms |
| 10:40 | 查 cgroup：`container_cpu_cfs_throttled_periods_total ÷ container_cpu_cfs_periods_total` = **34%** | 34% 周期被限流 |
| 10:55 | 算账：`limits.cpu: 200m` ⇒ 每 100 ms 只有 20 ms 配额；每 Pod 300 rps × 0.6 ms ≈ 180 ms/s，**贴着配额跑** | 180m vs 200m |
| 11:10 | 改 `limits.cpu: 2000m`（或删掉 limit、只留 request 100m），业务代码仍然零改动 | — |
| 11:15 | P50 3.7 ms、P99 14 ms；P99 回到「注入前 + 2 ms」的水平 | 收敛 |

**乘法链（左侧是算式，右侧是可以直接换假设复算的数）**：

| 步骤 | 算式 | 结果 |
| --- | --- | --- |
| 每 Pod 请求数 | `6,000 ÷ 40`（出向，A 侧）+ `6,000 ÷ 40`（入向，B 侧） | 150 + 150 = **300 rps** |
| 单请求代理 CPU | 本版本/本配置实测：mTLS AES-GCM 解密 + HTTP/1.1 编解码 + 约 1 KB body | **0.6 ms**（区间 0.3–1.2 ms） |
| 所需 CPU | `300 rps × 0.6 ms` | **180 ms/s = 180m** |
| CPU 配额 | `limits.cpu: 200m` ⇒ 每 100 ms 只有 20 ms；一个周期内 30 个请求需 18 ms | 余量 **2 ms（10%）** |
| 结论 | 稳态就已贴顶 ⇒ 任何突发（重试、批量、探活、一次长尾拷贝）都把本周期剩余请求推到下个周期 | 尾部 **+10~90 ms** |

> **量级结论（非承诺值）**。假设：① 单请求代理 CPU 取 0.6 ms，实测区间 0.3–1.2 ms，随 body 大小、协议（HTTP/1.1 vs HTTP/2）、是否开压缩、是否开 access log 而变；② mTLS 用 AES-GCM，若换成国密或大证书链，这一项会明显上升；③ 请求在 100 ms 周期内近似均匀到达（真实流量是突发的，所以实际更早撞顶）。**换假设要复算：CPU 成本翻倍，则同样配额下 P99 的锯齿会更早出现，但「P50 几乎不动、P99 翻倍」这个形状不变。**

这起事故最贵的一课是：**sidecar 的延迟不是「代理有多快」，而是「你有没有给代理 CPU」**。代理在 CPU 充足时单跳 0.1–0.5 ms，在 CFS 限流时单跳 10–90 ms，两者差两个数量级，而限流只打尾部——所以看 P50 会得出「网格几乎没成本」的错误结论。

### 事故二：iptables 把健康检查与出站依赖一起劫持，240 个 Pod 全挂（2026-04-22）

某物流平台有 120 个服务、2,800 个 Pod，安全合规要求「东西向默认拒绝」。于是执行了一次「收口」变更，三件事同时生效：① `meshConfig.outboundTrafficPolicy.mode` 由默认的 `ALLOW_ANY` 改为 **`REGISTRY_ONLY`**；② 全局 `PeerAuthentication` 改为 **`STRICT`**；③ 新增命名空间级 `AuthorizationPolicy` 默认拒绝。目标是「未注册的外部依赖一律不许连」。

先灰度到 3 个命名空间（240 个 Pod），20:12 开始出现现象：

| 时间 | 事件 | 关键数字 |
| --- | --- | --- |
| 20:10 | 变更生效，240 Pod 的 netns 规则已是 `REGISTRY_ONLY` + `STRICT` | 240 Pod |
| 20:12 | 服务 C 的 Pod 开始 `CrashLoopBackOff`：**kubelet 的 `readinessProbe: GET :8080/healthz` 走 Pod IP 入向，被 `ISTIO_INBOUND` 劫持到 15006，而 kubelet 不在网格里、没有 SPIFFE 证书**，`STRICT` 直接拒 | 探针 100% 失败 |
| 20:14 | `failureThreshold: 3 × periodSeconds: 5` ⇒ 15 s 后重启；重启后探针再失败，进入退避 | 15 s/轮 |
| 20:16 | 同时：服务 C 直连的外部 HTTPS 合作方与自建 Kafka（9092）不在注册表里，出向被 `REGISTRY_ONLY` 打到 blackhole cluster | 502 `UF` |
| 20:20 | 3 个命名空间 240 Pod 全部 NotReady；上游调用方开始 503 | 可用副本 0 |
| 20:24 | 值班看到的是「Pod 一直在重启、业务日志干净、容器 CPU 极低」——完全不像网络问题 | 定位耗时 14 min |
| 20:31 | 在 `istio-proxy` 容器里 `iptables -t nat -S` 才看清入向 8080 被 REDIRECT；`config_dump` 里出向目标是 blackhole | — |
| 20:34 | 回滚 `REGISTRY_ONLY` → `ALLOW_ANY`；补 `traffic.sidecar.istio.io/excludeInboundPorts: "8080"` 并把探针挪到独立端口；外部依赖补 `ServiceEntry` | — |
| 20:52 | 恢复；总不可用 42 分钟，3.1 万单顺延，时效承诺违约赔付 | ≈ 180 万元 |

**这起事故的乘法链**：

| 步骤 | 算式 | 结果 |
| --- | --- | --- |
| 爆炸半径 | `被改到的命名空间 Pod 数 × 每个 Pod 的两条路径（探针 / 出站依赖）` | **240 Pod × 2 类路径** |
| 探针路径 | 被 kubelet 的 `failureThreshold × periodSeconds` 变成「15 s 一轮重启」 | 15 s/轮 |
| 出站路径 | 被 `REGISTRY_ONLY` 变成「100% blackhole」——不是慢，是立刻断 | 100% 失败 |
| 为什么查不出 | 探针失败让 Pod 起不来 ⇒ 业务日志里看不到出向被拦；出向被拦让人以为下游挂了 ⇒ 先去查下游 | 两错互相掩盖 |

这两条路径的错误发生在**同一个 netns** 里，而 netns 里的规则不在任何 Git 仓库、不在任何 code review、也不出现在任何业务指标里——这就是「黑盒」这个词的完整含义。

> **量级结论（非承诺值）**。假设：① 探针 `failureThreshold=3`、`periodSeconds=5`（K8s 常见默认量级）；② 判定 `STRICT` 下 kubelet 探针必失败（只要入向端口被接管且未做探针重写/排除）；③ `REGISTRY_ONLY` 对未注册目标立刻 blackhole 而不是超时。**换假设会改变恢复时长，不改变「一次变更同时改三个全有或全无的开关，爆炸半径 = 整个命名空间」这个结论。**

关键认知：**iptables 劫持没有粒度**。它不区分「业务调用」与「kubelet 探针」「监控采集」「apt 更新」；它只知道 UID、GID、端口、IP 段这几样粗粒度的东西。凡是「默认拒绝 + 全量劫持」的组合，第一次上线几乎必然带走探针与外部依赖——这两类流量恰好都不归你写代码管。

### 事故三：xDS 推了一份「语法全对、语义全错」的配置，2,000 个 sidecar 10 秒内全 503（2026-05-08）

某电商已全量网格化，约 2,000 个业务 Pod。一次「把手写 `VirtualService` / `DestinationRule` 收进 Helm 模板」的重构里，模板里 subset 的关键字写错了一个字母：应用 Pod 的标签是 `version: v2`，模板渲染出的是 `app-version: v2`。

后果的可怕之处在于**没有任何一道闸门会拦它**：YAML 合法、CRD schema 校验通过、istiod 接受、2,000 个 sidecar 全部 **ACK**（因为「一个端点都匹配不上」是完全合法的配置）。

| 时间 | 事件 | 关键数字 |
| --- | --- | --- |
| 14:02:11 | `kubectl apply`，istiod 接受，无告警 | — |
| 14:02:19 | `pilot_proxy_convergence_time` P99 = 7.8 s，2,000 个 sidecar 全部换成新配置并 ACK | 7.8 s 全网生效 |
| 14:02:20 | 入口 5xx 由 0.2% → **63%**；日志里是 `no healthy upstream` / `503 UF` | 63% |
| 14:05 | 值班先查数据面：Pod CPU 正常、JVM 无异常、GC 无变化——**方向完全错** | 定位耗时 7 min |
| 14:09 | 想到看控制面：`pilot_xds_eds_reject` **没有涨**（说明不是 NACK），转去看端点 | reject = 0 |
| 14:11 | `istioctl proxy-config endpoint` 显示该 subset **0 个端点**；`cluster` 的 `edsClusterConfig` 指向的端点集合为空 | 0 endpoints |
| 14:12 | 回滚 Helm 值；14:15 全量恢复 | 回滚生效 3 min |
| 复盘 | 8,000 QPS × 780 s × 0.63 ≈ **390 万次失败请求**；SLO 当月预算被打穿 | ≈ 390 万次 |

**乘法链**：

| 步骤 | 算式 | 结果 |
| --- | --- | --- |
| 失败请求数 | `入口 QPS × 受影响时长 × 5xx 比例` = `8,000 × 780 s × 0.63` | **≈ 393 万次** |
| 全网生效时延 | `pilot_proxy_convergence_time` P99 | 7.8 s |
| 变更 → 告警 | 5xx 5 分钟窗口/1 分钟粒度下第一次触发 | ≈ 9 s |
| 变更 → 找对方向 | 数据面全绿，先查 Pod/GC/数据库 | **7 min**（真正的成本项） |
| 爆炸半径 | 全网格所有「到该 subset 的边」 | 拓扑级 |

> **量级结论（非承诺值）**。假设：① 8,000 QPS 为该时段入口量级；② 63% 为观测到的 5xx 比例（未打到该 subset 的流量正常）；③ 配置全网收敛 7.8 s 取 `pilot_proxy_convergence_time` P99 量级。**这张账的意义在于「时长」这一项：同样的错误，回滚越慢代价线性增加；而回滚的前提是你在 7 分钟内意识到该去查控制面。**

> 🩸 **三起事故的共同点**：没有一次是业务逻辑写错。① 是**给代理的 CPU 配额**；② 是**劫持规则没有粒度**；③ 是**配置只被校验语法、没被校验语义**。三者的爆炸半径依次扩大：一个 Pod 的尾部延迟 → 一个命名空间全部 Pod → 全网格所有边。这正是本篇的账本结构：**数据面按「跳」付钱，拦截层按「命名空间」付钱，控制面按「全网」付钱。**

---

## 41.2 一句话定位与唯一真问题

**服务网格的本质不是「更好的 RPC」，而是「把治理代码从 N 个 SDK 里拔出来，塞进 M 个代理里」，而这个挪动必须在每一次调用的热路径上收费。**

网格有两个面，缺一不可：

- **数据面（data plane）**：每个 Pod 里那个 sidecar 代理（Envoy 类），它劫持进出该 Pod 的全部 TCP，做 mTLS、鉴权、路由、重试、超时、熔断、限流、指标。它按「请求」收费：CPU、内存、延迟。
- **控制面（control plane）**：istiod / xDS server 类进程，它不转发一个字节流量，只负责把「配置」批发给成千上万个 sidecar，并充当证书签发机构（CA）。它按「配置变更 × 代理数」收费。

> **唯一真问题：网格把治理从 SDK 挪到代理，代价是每次调用多两跳 + 一份黑盒 iptables；收益在异构与统一策略，不在性能。**

这句话拆成四条可验证的推论，全篇都在给它们找证据：

1. **两跳的成本是加法，收益是乘法。** 每次调用固定多 0.2–1.2 ms（§41.4、§41.9）；收益是「N 种语言 × M 条策略」不再各写一遍。所以「上了网格变慢」与「上了网格治理成本下降」可以同时为真——它们不在同一本账上，而很多人只算了一本。
2. **代理只看得见协议，看不见语义。** 它能把 mTLS、重试、超时、熔断做成跨语言能力（因为它只看 HTTP/gRPC/TCP 元数据），却做不了幂等键、业务超时预算、租户记账（那些要读 body、要懂业务）。这就是 §41.6 的分工线，也是「网格重试 × SDK 重试」会叠成 3×3 的原因（→ [40 RPC 与连接复用](40-rpc-and-connection-reuse.md) §40.6）。
3. **拦截是全有或全无的。** iptables 一旦开，覆盖的是整个 netns 的进出站；它没有「只拦业务流量」这个粒度（§41.7）。于是「网格的故障」经常表现为「一个和业务无关的东西坏了」：探针、DNS、外部依赖、本地调试。
4. **控制面是配置的批发商，不是流量的搬运工。** 它挂了，数据面继续用最后一份配置跑（这是它比注册中心温和的地方，→ [38 注册中心与配置中心](38-registry-and-config.md)）；但它推错一份配置，能在 10 秒内让**全网一致地错**（事故三）。它的故障模式是「拓扑级全局错误」，比数据面的「局部坏」更难防，因为你没有对照组。

**它明确不承诺什么**（后面逐条兑现）：不承诺降低延迟（§41.4、§41.9）；不承诺 sidecar 是透明的（hostNetwork、特权容器、`exec` 调试、DNS、UDP 都会漏，§41.7）；不承诺控制面高可用等于业务高可用（§41.8）；不承诺 xDS 会替你校验业务意图（§41.5，事故三）。

### 解剖卡八问（服务网格版）

| # | 问题 | 本篇的回答（一句话） |
| --- | --- | --- |
| ① 承诺什么 | §41.2、§41.10 | 承诺「跨语言的同一套身份、策略与流量控制」；不承诺更快、不承诺透明、不承诺配置语义正确 |
| ② 数据长什么样 | §41.3、§41.5 | 数据面：每 Pod 一份 Envoy 配置（listener/route/cluster/endpoint 树）+ 每个在途请求的对象；控制面：一份全量配置快照 + 每个代理的 version/nonce 状态 |
| ③ 一次读写走哪条路 | §41.3、§41.4 | app → netns iptables → 本机 sidecar outbound → 对端 sidecar inbound → netns → app；两跳、4 次用户态/内核切换 |
| ④ 并发与一致 | §41.5、§41.8 | 控制面到数据面是「最终一致 + 每代理独立确认（ACK/NACK）」；没有全局原子切换，只有收敛时间 |
| ⑤ 扩展维度 | §41.9 | 数据面：按 Pod 水平扩（sidecar 数与业务副本同步增长）；控制面：按代理数×变更频率扩；二者瓶颈不同 |
| ⑥ 系统级失效 | §41.8 | 慢（sidecar 排队/CFS 限流）、满（控制面推送带宽与 CPU）、脑裂（控制面多副本各自算出一份不同配置）、假活（sidecar 在但证书过期）、语义漂移（配置合法但语义错） |
| ⑦ 看哪几个指标 | §41.12 | sidecar CFS 限流率、xDS NACK/not-synced 数、mTLS 握手失败率、单跳额外延迟、连接数/端点数为 0 |
| ⑧ 换掉它的信号 | §41.10 | 单次调用预算 < 1 ms、单 Pod 吞吐 > 数千 rps、团队无控制面运维能力、需要按业务语义治理 → 回到 SDK 或分场景混合 |

---

## 41.3 物理形态：Pod 内三个容器、一张规则链、两对 listener

### 一个 Pod 里到底多了什么

注入之后，Pod 里不再是一个业务容器，而是：

| 组件 | 类型 | 生命周期 | 它占什么 |
| --- | --- | --- | --- |
| `app` | 业务容器 | 你的进程 | 业务端口（如 8080） |
| `istio-init`（或 CNI 插件） | init container | **启动一次就退出**（`install-cni` 模式下不存在） | 只在启动时写 iptables 规则 |
| `istio-proxy` | sidecar 容器 | 与 Pod 同生共死 | Envoy 进程 + pilot-agent + 若干固定端口 |

三个容器**共享同一个 network namespace**。这一条事实推出三个后果，全部是一线故障源：

1. **回环可用**：app 连 `127.0.0.1:15001` 就是连本机 Envoy，不需要任何网络配置。这就是「两跳里有一跳不出机器」的原因。
2. **端口空间共享**：sidecar 占用的端口 app 不能用（见下表）。历史上不少「本地起个 mock 服务在 15021」的调试笑话，就是忘了这件事。
3. **进程共享 netns 但不共享 PID/IPC namespace**：所以 app 看不到 Envoy 进程（默认），`exec` 进去 `curl localhost:8080` 会命中自己的入向劫持规则——这就是 §41.7「本地调试连不上」的物理原因。

### sidecar 占用的固定端口（以 Istio 目标版本文档为准）

| 端口 | 谁在听 | 干什么 | 被 iptables 排除吗 |
| --- | --- | --- | --- |
| 15000 | Envoy | admin（`/config_dump`、`/stats`、`/clusters`） | 出向默认排除 |
| 15001 | Envoy | virtual outbound listener（所有被劫持的出向流量入口） | 是 |
| 15006 | Envoy | virtual inbound listener（所有被劫持的入向流量入口） | — |
| 15020 | pilot-agent | 合并 Prometheus 指标 | 是（入/出都排除） |
| 15021 | pilot-agent | `/healthz/ready`（含 Envoy 就绪判断） | 是 |
| 15053 | pilot-agent（DNS proxy） | 劫持后的 DNS 解析 | — |
| 15090 | Envoy | stats（Prometheus） | 是 |

### 四种拦截实现：同一件事的四种物理做法

| 做法 | 拦在哪 | 源 IP 可见吗 | 代价 | 典型代表 |
| --- | --- | --- | --- | --- |
| **iptables REDIRECT** | nat 表 PREROUTING / OUTPUT | ❌ 被 NAT 成 127.0.0.1 | 规则数随排除项增长；conntrack 条目翻倍 | Istio 默认（`istio-init`） |
| **TPROXY** | mangle 表 + `IP_TRANSPARENT` | ✅ 保留原始目的地址 | 需要额外权限与 socket 选项；内核版本敏感 | 透明代理、部分 CNI |
| **eBPF（socket-level）** | cgroup/sock hook，在 socket 层重定向 | ✅ | 不依赖 iptables 规则数；要求内核与 CNI 支持 | Cilium 的 L4 策略（无 sidecar） |
| **节点级代理（ambient）** | 节点上每节点一个 ztunnel | ✅ | 无 sidecar ⇒ 无每 Pod 跳数；L7 需要额外 waypoint 代理 | Istio ambient（较新版本引入，行为以目标版本文档为准） |

> **纪律**：这四种做法在「L4 是否加密、L7 策略在哪执行、调试命令长什么样」上完全不同。把网上的排障命令套到自己集群前，先确认拦截实现是哪一种——**这决定了 `iptables -t nat -S` 里有没有东西可看**。

### 一次调用的包路径（注入后）

```text
Pod（app 与 sidecar 共享同一个 network namespace，localhost 直通、端口空间共用）

  外部调用方
      │  dst = PodIP:8080
┌─────▼──────────────────────── Pod netns ──────────────────────────────┐
│  nat / PREROUTING → ISTIO_INBOUND                                     │
│      │ 8080 在"接管列表"里 ⇒ REDIRECT --to-port 15006                  │
│      ▼                                                                │
│  sidecar(istio-proxy) inbound listener :15006                         │
│      │ 解 mTLS（对端是谁）→ 读 AuthorizationPolicy → 匹配 route        │
│      ▼ 转发到 original_dst（127.0.0.1:8080，明文，回环）               │
│  app 容器 (:8080)                                                     │
│      │ 出站：dst = inventory:8080                                      │
│      ▼ nat / OUTPUT → ISTIO_OUTPUT → REDIRECT --to-port 15001          │
│  sidecar outbound listener :15001                                     │
│      │ 按 EDS 的端点列表选一个、发起 mTLS、套超时/重试/熔断/指标        │
└──────┼────────────────────────────────────────────────────────────────┘
       ▼
   对端 Pod 的 sidecar :15006 …（两跳 = 调用方 sidecar + 被调方 sidecar）
```

**两对 listener 的分工**是理解全篇的钥匙：

| listener | 它解决什么问题 | 它的路由依据 | 失败时你看到什么 |
| --- | --- | --- | --- |
| `:15001` outbound | 「这个目标地址该不该由我代理、该发到哪个端点」 | CDS 的 cluster + EDS 的端点；未知目标 → passthrough 或 blackhole | `502 UF` / `no healthy upstream` |
| `:15006` inbound | 「这个请求是谁发的、允许访问哪些方法」 | mTLS 身份 + AuthorizationPolicy + 原始目的端口 | `403 RBAC: access denied` / TLS 握手失败 |

---

## 41.4 两跳的账：为什么 P99 翻倍而 P50 只涨一点

### 一次 A→B 调用的路径与「切换次数」

```text
注入后（把"两跳"数成两次代理边界）：

  A[app] ──①──► sidecarA[out] ══网络(1 次 RTT)══► sidecarB[in] ──②──► B[app]
         2 次切换                                2 次切换
       (write + read)                          (write + read)

  ① 与 ② 各是一段回环上的明文 TCP：用户态→内核→用户态 = 2 次切换
  ⇒ 两跳合计 4 次用户态/内核切换，每跳另有一次额外的 socket 读写与内存拷贝
  （把跨节点网络的进出也算进来的话，tcpdump 视角是 6 次 syscall 边界；
    但"两跳"的账记的是 4 —— 因为网络那一次 RTT 你在注入前也要付）

  同时多出来的两样东西：
   · 两份连接池：app 的池（连 127.0.0.1:15001）+ sidecar 的池（连对端 sidecar）
   · 两次 TLS 状态：只有 sidecarA↔sidecarB 那段是加密的（一次握手，可摊销很多请求）
```

### 延迟分解表（同 AZ、千兆网、1 KB body）

| 段 | 注入前 | 注入后 | 何时变坏 | 能不能省 |
| --- | --- | --- | --- | --- |
| 应用序列化 | 0.1–0.5 ms | 0.1–0.5 ms | 大对象、反射序列化 | 换 protobuf、缩小消息 |
| app → 本机 sidecar（回环） | — | 0.03–0.15 ms | iptables 规则过多、Unix socket 未启用 | 无需省：这段是「本地」的 |
| sidecar A 处理（解密、路由、指标） | — | 0.05–0.3 ms | 开 access log、开 tracing 采样、规则多 | 关全量日志、降采样 |
| 跨节点网络（含 mTLS 记录层） | 0.5–2 ms（同 AZ） | 0.5–2 ms（同 AZ） | 跨 AZ/跨区；丢包 | 同 AZ 部署、连接复用 |
| sidecar B 处理（鉴权、限流、路由） | — | 0.05–0.3 ms | AuthorizationPolicy 条目多、JWT 校验 | 精简策略、缓存认证结果 |
| sidecar → app（回环） | — | 0.03–0.15 ms | 同上 | — |
| 响应回程 | 同上 | 同上 | 大响应 | 压缩、分页 |
| **合计增量（P50）** | — | **+0.16 ~ +1.2 ms** | — | 见 §41.10 |

> **量级结论（非承诺值）**。假设：① 同 AZ、无丢包；② Envoy 类代理、CPU 充足（未被 CFS 限流）；③ 请求小；④ mTLS 连接复用，握手摊薄到可以忽略。**换假设要复算**：CPU 一旦被限流，这一行会从 0.1 ms 跳到 10–90 ms（事故一）；跨 AZ 时网络那一行会盖过所有代理开销，此时讨论「网格的额外延迟」没有意义。

### 为什么 P99 翻倍而 P50 只涨一点

这是本篇最该背下来的一段推理，因为它决定了「网格适不适合你的延迟预算」。

**P50 只看均值，P99 只看排队。** 两跳带来的固定成本是加法（+0.2~1.2 ms，直接加到 P50 上），但两跳带来的**排队风险是乘法**，原因有三层：

1. **串联的服务台模型**：两个串联的排队系统，其尾部分位数的放大远大于均值。若每跳在利用率 ρ 下的排队延迟按 `ρ/(1−ρ) × 服务时间` 增长，那么当 ρ 从 0.4 涨到 0.7，单跳的排队期望涨约 3.5 倍，两跳串联后尾部叠加的不只是「3.5 + 3.5」，而是两个独立尾部事件的联合概率——**P99 处两个队列同时长的情形占主导**。
2. **在途请求数翻倍（Little 定律）**：`L = λ × W`。原来 A→B 的每个在途请求只有一份；注入后每一跳各持一份在途状态（app 等 sidecar、sidecar 等对端）。同样的 QPS 与延迟，在途对象数变成 2 倍，**突发吸收能力（buffer）相对变小**，排队更容易发生在尾部。
3. **同一个 CPU 上的邻居**：app 与 sidecar 常共享节点的有限核数；sidecar 的 CPU 成本是纯粹的加成，在节点整体利用率高时，它会把节点从「有余量」推到「接近饱和」——而 ρ 接近 1 时尾部爆炸。

| 你的延迟预算 | 下游本身占 | +1 ms 的意义 | 判定 |
| --- | --- | --- | --- |
| P99 = 5 ms | 1 ms | +20%~+100% | ❌ 不要在两跳里做，回到 SDK |
| P99 = 20 ms | 15 ms | +5%~+8% | ✅ 可接受 |
| P99 = 100 ms | 85 ms | +1% | ✅ 完全无感 |
| P99 = 500 µs（同机高频交易类） | 300 µs | +200% | ❌ 物理上不可接受 |

**一句话**：**网格的延迟成本不是一个常数，而是一个「固定项 + 排队项」的二元组；固定项可控，排队项只在尾部出现，而 SLO 通常写在尾部。**

---

## 41.5 xDS：控制面怎么把配置「批发」给一万个代理

### 五种资源，一首字母歌

| 资源 | 全称 | 内容 | 谁消费 | 变更频率 |
| --- | --- | --- | --- | --- |
| **LDS** | Listener Discovery | 监听哪些端口、每个端口挂哪些 filter chain | Envoy | 低（配置结构变更时） |
| **RDS** | Route Discovery | 虚拟主机 + 路径匹配 + 超时/重试/权重 | Envoy（HTTP filter） | 中（路由/灰度变更） |
| **CDS** | Cluster Discovery | 有哪些上游集群、用什么协议、熔断参数 | Envoy | 中（服务增删） |
| **EDS** | Endpoint Discovery | 每个集群背后有哪些 `IP:port` | Envoy | **极高**（扩缩容、发布、探活抖动） |
| **SDS** | Secret Discovery | 证书、私钥、信任根 | Envoy | 低但关键（轮转） |

**ADS（Aggregated Discovery Service）** 是把五条流合并到一条 gRPC 流上的机制，目的是保证顺序：`CDS → EDS → LDS → RDS`。顺序错了会出现「路由指向一个还不存在的集群」这类瞬时错误——这就是为什么「配置生效」总是「先有端点、后有路由」。

### SotW 与 Delta：一份全量 vs 一份增量

| 维度 | SotW（State of the World） | Delta xDS |
| --- | --- | --- |
| 每次推送内容 | 该类型的**完整**资源集合 | 只推变更的资源 + 被移除资源的名字 |
| 版本粒度 | 类型级（一份当前版本） | 资源级（每个资源各有版本） |
| 网络开销 | 与资源总数成正比 | 与变更数成正比 |
| 首次订阅 | 全量，一样 | 全量，一样 |
| 典型问题 | 大集群里 EDS 全量推送把控制面 CPU 与出口带宽打满 | 需要代理支持，排障时「为什么我还有旧资源」更难答 |
| 判断依据 | `pilot_xds_pushes`、单次推送字节数 | 同上，再看单代理的资源版本号是否落后 |

> **量级结论（非承诺值）**：EDS 的推送成本约等于「端点总数 × 每端点序列化字节 × 代理数 × 变更频率」（§41.9 账三）。Delta 能把前三项中的「端点总数」换成「本次变更数」，在 5,000 端点以上的集群里通常能把控制面出口带宽降一个数量级——**但前提是你用的代理版本真的支持**，以目标版本文档为准。

### version / nonce / ACK / NACK：一次推送的完整对话

```text
SotW 的一次推送（Envoy × 1，资源类型 = EDS）

  istiod                                   Envoy(sidecar)
    │  DiscoveryResponse(v=7, nonce=n7, EDS 全量)
    ├───────────────────────────────────────►│  校验 → 应用
    │◄───────────────────────────────────────┤  ACK(v=7, nonce=n7)
    │                                        │
    │  DiscoveryResponse(v=8, nonce=n8)       │  ✗ 校验失败
    ├───────────────────────────────────────►│
    │◄───────────────────────────────────────┤  NACK(v=7, nonce=n8, error_detail)
    │                                        │  继续用 v=7（last known good）
    │  DiscoveryResponse(v=9, nonce=n9)       │
    ├───────────────────────────────────────►│  校验 → 应用
    │◄───────────────────────────────────────┤  ACK(v=9, nonce=n9)

  nonce   = "这一次响应"的编号：ACK/NACK 必须原样回带，控制面才知道你确认的是哪一份
  version = "这份配置"的编号：SotW 下同一类型只有一份当前版本，新版本覆盖旧版本
  NACK    = 我拒了，但我还在用上一份好的 —— 这是 Envoy 最重要的一条自保
```

**NACK 保护只覆盖「语法/schema/引用完整性」错误，不覆盖「语义」错误。** 事故三里那份 subset 写错的配置是**合法**的：schema 通过、引用存在（cluster 存在，只是端点集合为空），所以 2,000 个代理老老实实 ACK 并立刻把流量导到一个 0 端点的集群。**这是本篇最容易被误解的一条**：很多人以为「有 NACK 兜底所以推错配置不会全站挂」，实际上能全站挂的恰恰都是不 NACK 的那一类。

### 一条命令定位「配置没生效」

```bash
# xDS 三层排查：谁的配置旧了（STALE/NOT SENT）、谁 NACK 了、谁 0 端点
istioctl proxy-status                      # SYNCED / NOT SENT / STALE 一览
istioctl proxy-status deploy/checkout      # 单代理逐类型看 version 与 last ACK/NACK
istioctl proxy-config endpoint deploy/checkout --port 8080
                                           # 0 个端点 ⇒ 客户端看到的就是 "no healthy upstream"
istioctl proxy-config cluster deploy/checkout \
  --fqdn inventory.default.svc.cluster.local -o json | jq '.[].edsClusterConfig'
istioctl proxy-config secret deploy/checkout           # SDS 证书链与过期时间
# 控制面视角（指标名随版本变化，以目标版本为准）
#   pilot_xds_pushes{type="eds"}          推送次数
#   pilot_xds_{cds,eds,lds,rds}_reject    NACK 次数
#   pilot_proxy_convergence_time          配置变更 → 代理生效的时延（事故三的 7.8 s）
# 数据面视角（比控制面更早暴露问题）
#   envoy 的 config_dump 里 "last_updated" 与 "version_info" 落后 ⇒ 没收到/被拒
```

### 控制面挂了，数据面还能不能工作

这是每个网格方案评审必被问的一句，正确答案是**分能力、分时长**：

| 能力 | 控制面挂掉后 | 还撑多久 | 靠什么 |
| --- | --- | --- | --- |
| 已建立连接的转发 | ✅ 继续 | 无限（配置不变） | 代理内存里的最后一份配置 |
| 新建连接（mTLS） | ⚠️ 取决于证书 | 到证书过期为止 | SDS 已下发的证书；TTL 是倒计时 |
| 新 Pod 加入网格 | ❌ 不能 | 立刻 | 没有证书、没有 listener 配置，Pod 卡在未就绪 |
| 新策略/新路由生效 | ❌ 不能 | 立刻 | 没有推送源 |
| 端点变更（扩缩容/发布） | ❌ 不能 | 立刻 | EDS 不再更新 ⇒ 流量继续打向已删 Pod 的 IP |
| 证书轮转 | ❌ 不能 | 到 TTL | 轮转失败 ⇒ 新连接握手失败（§41.8） |

> **一句话**：**控制面挂掉不是「立刻雪崩」，而是「你被冻结在最后一份配置上，同时有一只看不见的倒计时（证书 TTL）在走」。** 这只倒计时的长度，就是「控制面可容忍的不可用时长」；它必须写进预案，而不能靠「istiod 有 2 个副本」这种直觉。

---

## 41.6 治理能力：哪些该网格做、哪些必须留给 SDK

### 八项能力的落点

| 能力 | 物理落点 | 谁最合适 | 代价 / 陷阱 |
| --- | --- | --- | --- |
| **mTLS / 身份** | sidecar 之间的 TLS 握手 + SDS 证书 | **网格**（唯一正确答案） | 证书 TTL 决定控制面故障容忍度；`STRICT` 下没有明文退路 |
| **AuthorizationPolicy** | 入向 sidecar 的 filter | **网格** | 全有或全无（默认拒绝）⇒ 第一次上线必带走探针/监控（事故二） |
| **重试** | outbound sidecar 的 route | 网格可以，但要**只留一处** | 与 SDK 重试相乘（见下） |
| **超时** | route / cluster 上 | 网格（配合 SDK 的 deadline 传播） | 网格超时必须 ≥ app 超时，否则代理先断、app 日志干净 |
| **熔断（连接池上限）** | cluster 的 `connectionPool` | **网格** | 溢出错误是 `upstream_reset_before_response_started{overflow}`，容易被误判为下游挂 |
| **异常点摘除（outlier detection）** | 被调方集群的被动健康检查 | **网格** | `maxEjectionPercent` 默认量级 10%（→ [39 入口网关](39-gateway-and-load-balancing.md) §39.6 的摘除护栏） |
| **流量切分 / 灰度** | route 的权重 | **网格**（异构场景唯一可行） | 权重变了但端点没变时，长连接会「不按权重走」——要配合连接数或 `maxRequestsPerConnection` |
| **故障注入 / 镜像** | route 的 fault / mirror | 网格（测试环境） | 注入的延迟与真实延迟叠加，别在生产长开 |
| **幂等键 / 业务预算 / 租户记账** | 业务代码 | **SDK（唯一正确答案）** | 代理看不见 body 语义；硬塞进代理会变成 `Lua`/`Wasm` 里的一坨业务逻辑 |

### 网格 vs SDK：判定骨架

```go
// 判定一个治理能力该放网格还是放 SDK：问三件事（骨架，非可编译成品）
type Capability struct {
	Name        string
	NeedsBody   bool          // 是否需要看业务载荷/语义（幂等键、租户、业务预算）
	NeedsIdent  bool          // 是否需要"我是谁"（SPIFFE 身份、命名空间、ServiceAccount）
	LatencyCost time.Duration // 单次额外开销量级
}

func Placement(c Capability) string {
	switch {
	case c.NeedsBody:
		return "SDK：代理只懂 HTTP/gRPC 语义，不懂你的幂等键与业务预算"
	case c.NeedsIdent:
		return "网格：身份由 mTLS 证书给出，比任何 header 都可信（header 可伪造）"
	case c.LatencyCost > 200*time.Microsecond:
		return "两边都要算总账：网格里做会乘以 2 跳，先问值不值"
	default:
		return "网格：协议层策略，跨语言统一，升级不用改业务代码"
	}
}
```

### 最危险的一处叠加：网格重试 × SDK 重试

设 SDK 侧最多尝试 `a_sdk` 次，网格侧 route 上配了 `attempts: a_mesh`（Istio 类默认量级为 2），失败概率 `p`，最坏情况下游始终没恢复：

| 步骤 | 算式 | 结果 |
| --- | --- | --- |
| 期望尝试次数 | `A = (1 − p^(r+1)) / (1 − p)`，`r = 尝试次数 − 1`（推导见 [40 RPC 与连接复用](40-rpc-and-connection-reuse.md) §40.6） | — |
| SDK 侧 | `A_sdk = (1 − 0.2³) / 0.8` | 1.24 |
| 网格侧 | `A_mesh = (1 − 0.2²) / 0.8` | 1.20 |
| 平均到达下游 | `Q × A_sdk × A_mesh = 10,000 × 1.24 × 1.20` | **≈ 14,880/s（+49%）** |
| 瞬时脉冲（全部同步重试） | `Q × 3 × 2` | **60,000/s** |

> **量级结论（非承诺值）**。假设：① 失败独立同分布；② 两侧都无退避抖动；③ 下游无熔断兜底。**结论：重试必须只留一处**——要么 SDK 关掉重试、由网格统一做（跨语言一致），要么网格不配 retry、只做超时与熔断。两处都开，等于给过载的下游乘两次。

---

## 41.7 黑盒 iptables：一张没人 review 的规则链

### 规则链的职责分工

| 链 | 挂在哪 | 它决定什么 | 典型内容 |
| --- | --- | --- | --- |
| `ISTIO_INBOUND` | nat / PREROUTING | 入向端口**要不要**被接管 | 排除端口 `RETURN`；其余跳 `ISTIO_IN_REDIRECT` |
| `ISTIO_IN_REDIRECT` | — | 入向重定向到哪 | `REDIRECT --to-ports 15006` |
| `ISTIO_OUTPUT` | nat / OUTPUT | 出向**要不要**被接管 | 排除回环、UID/GID、15020/15090；其余跳 `ISTIO_REDIRECT` |
| `ISTIO_REDIRECT` | — | 出向重定向到哪 | `REDIRECT --to-ports 15001` |

### 四类排除：你能控制的全在这里

| 排除手段 | 写法 | 解决什么问题 | 副作用 |
| --- | --- | --- | --- |
| UID / GID | `-m owner --uid-owner 1337 -j RETURN` | 代理自身流量不被劫持（否则死循环） | 固定 UID 是硬编码，改镜像需注意 |
| 端口 | `traffic.sidecar.istio.io/excludeInboundPorts`、`excludeOutboundPorts` | 探针端口、指标端口、特殊协议不劫持 | 排除即「不加密、不受策略管」，是合规漏洞的口子 |
| IP 段 | `excludeOutboundIPRanges`（默认常含 `169.254.169.254/32` 元数据地址，以目标版本为准） | 云元数据、外部存储、DNS 服务器 | 逐个维护，容易漏 |
| 工作负载类型 | `hostNetwork: true` 的 Pod 默认不注入 | 避免劫持节点网络 | 这类 Pod **完全没有网格能力**，是最常见的「策略漏网」 |

### 在 Pod 里看规则

```bash
# 1) 看本 Pod 的劫持规则（不是看节点的！iptables 在 Pod 自己的 netns 里）
kubectl exec -it deploy/checkout -c istio-proxy -- iptables -t nat -S ISTIO_OUTPUT
#   -A ISTIO_OUTPUT -d 127.0.0.1/32 -j RETURN
#   -A ISTIO_OUTPUT -m owner --uid-owner 1337 -j RETURN      ← 代理自己不被劫持
#   -A ISTIO_OUTPUT -m owner --gid-owner 1337 -j RETURN
#   -A ISTIO_OUTPUT -p tcp --dport 15020 -j RETURN
#   -A ISTIO_OUTPUT -j ISTIO_REDIRECT                        ← 其余全部跳走
iptables -t nat -S ISTIO_REDIRECT
#   -A ISTIO_REDIRECT -p tcp -j REDIRECT --to-ports 15001
iptables -t nat -S ISTIO_INBOUND
#   -A ISTIO_INBOUND -p tcp --dport 8080 -j ISTIO_IN_REDIRECT ← 事故二里探针撞的就是这一条
# 2) 看注入时给你的排除项（annotation 就是规则的来源）
kubectl get pod checkout-0 -o jsonpath='{.metadata.annotations}' | tr ',' '\n' | grep traffic.sidecar
kubectl get pod checkout-0 -o jsonpath='{.metadata.annotations}' | tr ',' '\n' | grep proxy.istio.io
# 3) 一条连接到底被劫持没有：看 socket 落在哪个端口上
ss -tnp | grep -E '15001|15006'
```

### 启动顺序：为什么第一次请求经常失败

app 与 sidecar 是**并行启动**的两个容器。K8s 只保证「容器都起来了」，不保证「代理已经能转发」。于是出现经典的「Pod Ready 之后第一批请求 503」，因为：

- app 的启动探针通过 ⇒ kubelet 认为 Pod 可以接流量 ⇒ 端点被加入 EDS；
- 但 sidecar 的 listener 可能还在初始化（尤其是配置很大、或者 `Pilot` 推送慢）⇒ 入向请求被 REDIRECT 到 15006 却没人接 ⇒ `connection refused`。

三件套解决方案（字段与默认值随版本变化，以目标版本文档为准）：

```yaml
# ① 让 app 等代理就绪再启动（meshConfig.defaultConfig 级别，非 Pod 字段）
#    holdApplicationUntilProxyStarts: true
#    该开关在 1.7 引入，1.12 之后多个发行版默认开启；未开启时首请求失败率明显更高
# ② 关停时先摘流量、再关进程（应用侧的小动作，配合端点摘除的传播延迟）
spec:
  containers:
    - name: app
      lifecycle:
        preStop:
          exec:
            command: ["sh", "-c", "sleep 5"]     # 给 endpoint 摘除留出传播时间
# ③ 代理侧：排空到零连接后自行退出，避免僵尸 sidecar 挂住 Pod 终止流程
#    proxy.istio.io/config: |
#      proxyMetadata:
#        EXIT_ON_ZERO_ACTIVE_CONNECTIONS: "true"
```

> **纪律**：这三件事解决的是三个不同的空窗——① 解决「代理未就绪」，② 解决「端点未摘除」，③ 解决「代理不退出」。**只做其中一个，你会得到另外两个的空窗**；而它们的症状几乎一样：发布期间零星的 503。

### 为什么「本地调试连不上」

这是网格最高频的工单，根因有四层，从物理到操作依次是：

| 现象 | 根因 | 怎么确认 | 怎么绕 |
| --- | --- | --- | --- |
| `kubectl exec` 进去 `curl localhost:8080` 不通 | 你的 shell 在 app 容器里，出向被 `ISTIO_OUTPUT` 劫持到 15001，而 15001 按**目标地址**匹配 cluster，`127.0.0.1` 不是注册的端点 | `ss -tnp` 看到连的是 15001 | 用 `-c istio-proxy` 进代理容器；或临时 `excludeOutboundPorts` |
| `curl http://service:8080` 慢或 502 | 目标是注册表里没有的名字 ⇒ `REGISTRY_ONLY` 下 blackhole | `config_dump` 里 cluster 名为 `PassthroughCluster`/`BlackHoleCluster` | 加 `ServiceEntry` 或临时改 `ALLOW_ANY` |
| `dig` 结果与节点上不同 | DNS 被劫持到 15053 的 DNS proxy，ServiceEntry 里的域名由控制面解析 | 对比 app 容器与节点上的 `dig` | 认这个事实：**在网格里，DNS 也是被代理的** |
| 端口转发/本地进程连不上 Pod | 入向被 15006 接管，源不是网格成员，`STRICT` 拒绝 | 报错是 TLS/握手类而非 TCP 拒绝 | `port-forward` 走 15021 或临时排除入向端口 |

---

## 41.8 故障域：四个新单点，四种不同的爆炸半径

```text
四个故障域，各自的"最后一层保护"是什么

  ① sidecar 挂          → 就绪探针失败 → Pod 被摘 / 容器重启
     └ 保护：代理的 resources 要有下限；app 不要与代理共享同一份 CPU 配额
  ② 控制面挂（istiod）  → 数据面继续用最后一份配置（Envoy 永不丢弃 LKG）
     └ 保护：证书 TTL（轮转窗口）就是你的容忍时长；端点陈旧 = 打向已删 IP
  ③ 证书轮转失败        → 新连接握手失败；STRICT 模式下没有明文退路
     └ 保护：SDS 证书过期时间必须单独告警；CA 要按"全站单点"对待
  ④ 升级 sidecar        → 等于把全部服务滚动一遍（网格最大的爆炸半径）
     └ 保护：按 revision 金丝雀，先一个命名空间、再一批业务
```

| 故障域 | 触发条件 | 爆炸半径 | 兜底与时限 |
| --- | --- | --- | --- |
| **sidecar 挂** | OOM、CPU 限流到僵死、配置错误、Envoy 自身 bug | **一个 Pod 的全部进出流量**（app 活着但网络全废） | 就绪探针摘除 + 容器重启；没有跨 Pod 影响 |
| **控制面挂** | istiod 全部副本不可用、xDS 流被打满 | 全网**新变更冻结**，已建连接无感 | 最后一份配置 + 证书 TTL 倒计时（§41.5） |
| **证书轮转失败** | CA 不可用、时钟漂移、信任根切换、SDS 流断 | **全网 mTLS 握手失败**（STRICT 下 100% 不可用） | 只能预置更长的 TTL 与双信任根过渡 |
| **升级 sidecar** | 网格版本升级（所有代理重启） | **全部服务同时滚动一遍** | revision 金丝雀 + 分批 + 与业务发布错峰 |

### 证书这条倒计时怎么算

先列假设（Istio 类默认量级，以目标版本文档为准）：workload 证书 TTL = 24 h，轮转在剩余 50% 时发起（即每 12 h 一次）；**已建立连接不重新握手，因此不受证书过期影响**；**新连接（含每一次连接池重建、每一次发布后的新 Pod）必须握手，因此必须持有有效证书**。

| 时刻 | 控制面状态 | 数据面表现 | 为什么 |
| --- | --- | --- | --- |
| T+0 | 全挂 | 已建连接照跑，指标正常 | Envoy 持有 LKG 配置与已下发证书 |
| T+12 h | 该轮转的一批失败 | **仍然正常** | 旧证书还在有效期内，没有新握手需求 |
| T+24 h | 证书全部过期 | 任何新建连接 100% mTLS 失败 | `STRICT` 无明文退路 |
| T+0 后的任意一次发布 | 证书未必过期 | **立刻出问题** | 发布把「少数新连接」变成「多数新连接」 |

**结论一句话**：真正的「全站 mTLS 失败」通常不是「控制面挂了 24 小时之后」，而是**控制面挂了之后的下一次发布**——发布把「新连接」这个少数派变成了多数派。

> **量级结论（非承诺值）**。假设：① TTL 24 h、轮转阈值 50%；② 连接池空闲回收周期在分钟级；③ `PeerAuthentication` 为 `STRICT`（无明文回退）。**换假设会直接改变容忍时长：TTL 改 12 h，你的容忍窗口就减半；把 STRICT 改成 PERMISSIVE 能换来「过期后降级为明文」的可用性，代价是安全承诺作废。**

> **一条纪律**：**控制面的高可用指标不是「副本数」，而是「最后一次成功推送距今多久」与「最早到期的证书还有多久」。** 前者超过一分钟、后者低于 25% TTL，就必须按故障处理，而不是等业务报错。

---

## 41.9 三本账：额外延迟、连接数、控制面推送

### 账一 · 额外延迟 = 2 × 单跳代理 + TLS（摊薄）

假设：同 AZ、1 KB body、CPU 充足、mTLS 连接复用；单跳代理（含解密、路由、指标、两次回环）取 0.12 ms，TLS 摊销 0.04 ms/请求。

| 项 | 公式 | 结果 |
| --- | --- | --- |
| 每跳固定成本 | 0.12 ms | 0.12 ms |
| TLS 摊薄 | 0.04 ms（一次握手 ~1 ms，摊到 ~25 个请求/连接） | 0.04 ms |
| 两跳合计 | `2 × (0.12 + 0.04)` | **0.32 ms** |
| 乐观区间 | 同 AZ、小 body | 0.16–0.6 ms |
| 悲观区间 | 开 access log + tracing 全采样 + 大 body + 规则多 | 1.2–4 ms |
| 限流区间（事故一） | CPU 配额不足 ⇒ CFS 排队 | **10–90 ms** |

> **量级结论（非承诺值）**。假设清单见上；**换假设请复算**：access log 全开会把每跳成本抬到 0.3–0.6 ms（磁盘写 + 格式化），tracing 100% 采样在长链路上经常是「网格上线后变慢」的真凶；CPU 被限流则进入第三区间，此时谈「代理效率」没有意义。

### 账二 · 连接数 ≈ 原连接数 × 2（再加一份 fd 与 conntrack）

假设：A 有 40 个 Pod，每个 Pod 到 B 的客户端连接池 20 条；B 有 40 个 Pod。

| 步骤 | 算式 | 结果 |
| --- | --- | --- |
| 注入前 | `40 Pod × 20 条池` | **800 条 TCP** |
| 注入后第一段 | `app → 127.0.0.1:15001`（回环明文） | 800 条 |
| 注入后第二段 | `sidecar → 对端 sidecar`（mTLS） | 800 条 |
| 合计 TCP | `800 + 800` | **1,600 条** |
| socket 数 | `每条 TCP 两端各一个 socket` = `1,600 × 2` | **3,200 个** |
| 缓冲内存 | `3,200 × 32 KB`（回环段与网络段各一份 socket 缓冲） | **≈ 100 MB** |
| conntrack | 网络段每条连接 1 条（回环段是否记账取决于内核与配置） | 800 条/组 |
| TLS 状态 | 网络段每条连接一份（会话票据、密钥、证书链） | 800 份 |

| 规模 | 原连接 | 注入后 TCP | socket 数 | 需要关注的资源 |
| --- | --- | --- | --- | --- |
| 40 Pod × 20 | 800 | 1,600 | 3,200 | 无感 |
| 400 Pod × 20 | 8,000 | 16,000 | 32,000 | 每 Pod fd 上限、内存 |
| 2,000 Pod × 40 | 80,000 | 160,000 | 320,000 | conntrack 表、节点端口、TLS 内存 |

> **量级结论（非承诺值）**。假设：① 连接池大小不变（很多 SDK 不会因为注入而调小池）；② 回环段也走完整 TCP（不是 Unix socket）；③ 无连接复用优化。**这张账的意义**：注入后你的 fd、conntrack、TLS 内存全部约 ×2，而「连接数」这个指标平时根本不在业务容量模型里。发布时最容易撞上的就是它。

### 账三 · 控制面推送 = 端点总数 × 代理数 × 变更频率

假设：2,000 个 sidecar；某服务 4,000 个端点；每次 EDS 全量推送约 200 KB（4,000 端点 × ~50 B 序列化）；滚动发布 10 分钟内产生 200 次端点变更。

| 项 | 公式 | 结果 |
| --- | --- | --- |
| 单次推送出口字节 | `2,000 代理 × 200 KB` | **400 MB** |
| 10 分钟内总推送 | `400 MB × 200 次` | **80 GB** |
| 平均出口带宽 | `80 GB ÷ 600 s` | **≈ 133 MB/s ≈ 1.07 Gbps** |
| 控制面 CPU | 每份配置都要序列化 + 每个代理一份流控状态 | 常见瓶颈，先于带宽撞顶 |
| SotW 与 Delta 的差别 | 全量 vs 只推变更资源 | 线性 → 接近常数 |

> **量级结论（非承诺值）**。假设：① SotW 全量推送；② 每次变更都触发对所有代理的推送（Istio 的实现里会做批处理与去重，实际次数常小于 200）；③ 端点数与字节数是量级估计，**真实 payload 请用 `pilot_xds_pushes` 与单次推送字节数实测**。**结论：控制面的成本由「变更频率」主导，不是由「端点数」单独决定——这就是为什么大集群里最重要的治理动作是「减少端点抖动」（固定副本数、减少无意义滚动发布、把探活抖动收敛掉）。**

### 三本账复算脚本

```python
# 41.9 三本账：改任一假设直接重跑（量级结论，不是承诺值）
def extra_latency_ms(hops=2, per_hop_ms=0.12, tls_amortized_ms=0.04):
    """额外延迟 = hops × (单跳代理 + TLS 摊薄)"""
    return hops * (per_hop_ms + tls_amortized_ms)

def sockets(app_conns, segments_per_conn=2, sides_per_conn=2):
    """连接数 ×2（两段）×2（两端 socket）"""
    return app_conns * segments_per_conn, app_conns * segments_per_conn * sides_per_conn

def xds_egress_MBps(sidecars, endpoints, bytes_per_endpoint=50,
                    pushes_per_sec=0.33):
    """控制面出口 = 代理数 × (端点数 × 每端点字节) × 推送频率"""
    payload = endpoints * bytes_per_endpoint
    return sidecars * payload * pushes_per_sec / 1e6

print("额外延迟 ms:", round(extra_latency_ms(), 2))
tcp, socks_ = sockets(40 * 20)
print(f"648 业务连接 → {tcp} 条 TCP / {socks_} 个 socket")
print("控制面出口 MB/s:", round(xds_egress_MBps(2_000, 4_000), 1))
```

---

## 41.10 架构边界：什么时候不该上网格

| 你想要的东西 | 网格给不给 | 物理原因 | 正确落点 |
| --- | --- | --- | --- |
| 更低延迟 | ❌ | 每次调用多两跳、4 次切换、两份连接池 | 别用网格做性能优化（§41.4） |
| 统一 mTLS / 零信任东西向 | ✅ **这是最强理由** | 证书由控制面签发，身份来自 TLS 而非 header | 网格（无替代品） |
| 异构语言统一策略 | ✅ | 策略在协议层执行，与语言无关 | 网格 |
| 策略审计与合规（谁访问了谁） | ✅ | 每个入向请求都在 sidecar 里留痕 | 网格 + 集中日志 |
| 灰度/流量切分 | ⚠️ | 权重按连接建立时生效，长连接不跟随 | 网格 + 缩短连接寿命/`maxRequestsPerConnection` |
| 按业务语义治理（幂等、租户配额、业务预算） | ❌ | 代理看不见 body 语义 | SDK / 业务代码（§41.6） |
| 极低延迟内部调用（< 1 ms 预算） | ❌ | 固定 +0.2~1.2 ms 就是全部预算 | SDK 直连；或 ambient/eBPF 形态评估 |
| 单 Pod 极高吞吐（数千 rps 以上） | ⚠️ | 代理 CPU 成线性成本，且与业务抢核 | 算账；必要时按接口分级、大流量链路绕开 |

**不该上网格的四种情形**（每一条都对应一张账）：

1. **延迟预算比两跳还薄**：P99 目标 < 1 ms 的内部 RPC。此时 +0.3 ms 是 30% 预算，且尾部放大让 SLO 直接不成立。
2. **语言与技术栈单一、SDK 已成熟**：Java 单栈 + Dubbo/Spring Cloud 已经统一了治理。网格的收益（异构统一）在这里是零，成本（两跳 + 控制面）全额支付。
3. **规模太小**：几十个服务、上百个 Pod。控制面运维成本（升级、证书、CRD 治理、值班）是固定的，摊不下去。
4. **团队没有控制面运维能力**：网格把「SDK 的 bug」换成了「控制面的运维」。没有能读 `config_dump`、能定位 xDS NACK、能规划证书轮转的人，上网格就是把故障从「代码里」搬到「你看不见的地方」。

**真收益（值得付账的三条）**：

- **统一 mTLS 与身份**：跨语言、跨团队的零信任东西向，在应用层重写一遍的成本远高于两跳的延迟。
- **异构语言统一策略**：Java、Go、Python、Node 混布时，让所有语言的超时/重试/熔断/鉴权行为一致——这是 SDK 路线永远做不到的（每个语言的 SDK 成熟度不同、升级节奏不同）。
- **策略审计**：合规要求「谁能访问谁」有明确证据链。sidecar 的入向日志 + 身份是唯一能覆盖所有语言的证据来源。

**判定顺序**（写进架构决策记录）：

1. 有没有异构语言 / 合规审计的**硬需求**？无 → 结束，继续用 SDK。
2. 调用延迟预算是否有 `> 5 × 两跳成本`（即 > 1.5 ms 余量）？否 → 分场景混合，热路径绕开网格。
3. 有没有人能值班控制面（xDS、证书、升级）？无 → 先用托管控制面，或推迟。
4. 端点抖动是否可控（副本数稳定、发布频率可控）？否 → 先治理发布节奏，再上网格。
5. 以上都满足 → 上，并按 §41.12 建好那五个指标。

---

## 41.11 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（物理形态） | 定位 | 修复 | 防复发 |
| --- | --- | --- | --- | --- |
| 注入后 P50 微涨、P99 翻倍，曲线呈 100 ms 周期锯齿 | sidecar CPU 配额被 CFS 限流（`cfs_period_us` = 100 ms） | `container_cpu_cfs_throttled_periods_total ÷ periods` | 提高/去掉 `limits.cpu`，保留 request | sidecar CFS 限流率纳入告警（§41.12） |
| Pod 反复重启、业务日志干净 | 探针入向被劫持 + `STRICT` 无证书 | 代理容器里 `iptables -t nat -S ISTIO_INBOUND` | 探针端口排除 / 探针重写 / 独立端口 | 变更 `STRICT` 前先扫探针路径 |
| 外部依赖全部连不上、报 `502 UF` | `REGISTRY_ONLY` 下未注册目标被 blackhole | `config_dump` 里的 `BlackHoleCluster` | 补 `ServiceEntry` 或临时 `ALLOW_ANY` | 出向策略变更必须配「外部依赖清单」 |
| 发布期间零星 503 | 代理未就绪 / 端点未摘除 / 代理不退出，三个空窗 | 对比 Pod Ready 时间与首批请求时间 | `holdApplicationUntilProxyStarts` + `preStop sleep` + 排空退出 | 三项一起上，别只做一项 |
| 一次配置变更 10 秒内全站 5xx，`reject` 指标为 0 | 配置合法但语义错（selector 拼错 ⇒ 0 端点） | `istioctl proxy-config endpoint` 看端点数为 0 | 回滚；改为灰度命名空间先行 | 变更前用 `proxy-config` 断言端点数 > 0 |
| 网格上线后带宽/CPU 上涨、延迟没降 | 控制面推送放大（端点抖动） | `pilot_xds_pushes`、控制面出口带宽 | 减少发布频率、稳定副本数、启用 Delta | 把「端点变更次数」当容量指标 |
| 服务间偶发 TLS 错误 | 证书轮转失败 / 时钟漂移 / 信任根切换 | `proxy-config secret` 看过期时间与 issuer | 修 CA 可达性、统一时钟 | 证书剩余 TTL < 25% 告警 |
| 灰度权重不生效 | 长连接在建立时就绑定了端点 | 看单连接上的请求数与 `maxRequestsPerConnection` | 缩短连接寿命或按请求数轮换 | 灰度方案里写明「连接寿命」前提 |
| `exec` 进容器调不通本地服务 | 出向被劫持到 15001，回环也被接管 | `ss -tnp` 看目标端口 | 进代理容器 / 临时排除端口 | 把这条写进值班手册第一页 |
| 升级网格版本后全站抖动 | 升级 = 全部 sidecar 重启 = 全部服务滚动 | 升级窗口与业务 SLO 对齐看 | revision 金丝雀、分批升级 | 升级视为「全网发布」，走发布流程 |

---

## 41.12 观测：五个数覆盖 90% 的网格故障

| 指标 | 看什么 | 拐点 | 前提 |
| --- | --- | --- | --- |
| sidecar CFS 限流率 | 代理是否被自己的 CPU 配额卡住 | > 5% 周期 | 需要 cAdvisor 按 container 名过滤 |
| xDS 未收敛 / NACK 数 | 配置有没有生效、被谁拒了 | 任一代理 STALE、`*_reject` > 0 | 指标名随版本变化 |
| mTLS 握手失败率 | 证书、`STRICT` 误配、信任根 | > 0 且持续 | 需要 Envoy listener 级 ssl 指标 |
| 单跳额外延迟 | 代理开销是否吃掉预算 | 入向时长 − 上游时长 P99 > 2 ms | 需区分「代理耗时」与「上游耗时」 |
| 端点数为 0 / 健康比例 | 事故三的直接信号 | 任一关键 cluster 为 0 | `envoy_cluster_membership_total` |
| 连接数与 fd | ×2 之后是否撞上限 | fd > 80%、连接数环比异常 | 按 Pod 与按目标地址分别看 |

```bash
# 值班第一屏：五个数（指标名与标签随 Istio / Envoy 版本变化，以目标版本为准）
# 1) sidecar 被 CFS 限流 —— "注入后 P99 翻倍"最常见的原因
rate(container_cpu_cfs_throttled_periods_total{container="istio-proxy"}[5m])
  / rate(container_cpu_cfs_periods_total{container="istio-proxy"}[5m]) > 0.05
# 2) xDS 没收敛 / 被拒
pilot_xds_eds_reject > 0   或   istioctl proxy-status | grep -v SYNCED
# 3) mTLS 握手失败（证书轮转、STRICT 误配）
rate(envoy_listener_ssl_connection_error[5m]) > 0
# 4) 额外延迟：入向时长 − 上游时长 ≈ 本跳代理开销
histogram_quantile(0.99, rate(istio_request_duration_milliseconds_bucket[5m]))
  - histogram_quantile(0.99, rate(istio_request_duration_milliseconds_bucket{reporter="destination"}[5m]))
# 5) 端点数为 0 = "no healthy upstream" 的直接来源（事故三）
envoy_cluster_membership_total == 0
```

**三个必须写进告警表达式的前提**（写错一个，读者会认为全篇没验证过）：

1. `container="istio-proxy"` 这个标签名取决于你的注入模板与 cAdvisor 配置；有的发行版容器名是 `istio-proxy`，ambient 形态下根本没有这个容器。
2. `pilot_*` 指标只在自建控制面暴露；用托管控制面时你只能看到数据面指标，此时「未收敛」的唯一信号是 `istioctl proxy-status` 或代理侧配置版本号落后。
3. 「额外延迟」如果用 `reporter="source"` 与 `reporter="destination"` 相减，要确认两个 reporter 都开启了——只开 source 时这一项恒为 0，会给你「网格没有成本」的假象。

---

## 41.13 一页速记

| 问题 | 一句话答案 |
| --- | --- |
| 网格的本质是什么 | 把治理代码从 N 个 SDK 挪进 M 个代理，在热路径上按请求收费 |
| 一次调用多走了什么 | app → 本机 sidecar → 对端 sidecar → app；两跳、4 次用户态/内核切换 |
| 多付多少延迟 | P50 +0.16~1.2 ms；CPU 被限流则 +10~90 ms |
| 为什么 P99 翻倍而 P50 不动 | P50 只看固定项，P99 只看排队；两跳串联 + 在途翻倍 + CFS 限流 |
| 会多多少连接 | ×2（每条业务连接变两段），fd / conntrack / TLS 状态同步 ×2 |
| 控制面的成本由什么决定 | 代理数 × 端点总数 × 变更频率（不是请求量） |
| xDS 五种资源 | LDS 监听 / RDS 路由 / CDS 集群 / EDS 端点 / SDS 证书 |
| SotW 与 Delta 差在哪 | 全量 vs 只有变更；大集群差一个数量级 |
| ACK/NACK 保护什么 | 只保护语法与引用错误；语义错（0 端点）照样 ACK 并全站挂 |
| 控制面挂了还能跑吗 | 能跑最后一份配置；倒计时是证书 TTL，且下一次发布会提前引爆 |
| 证书轮转全失败会怎样 | `STRICT` 下新连接 100% 握手失败，没有明文退路 |
| iptables 的粒度 | UID/GID、端口、IP 段；不区分业务、探针与监控 |
| 重试该放哪 | 只放一处；网格与 SDK 都开 = 尝试次数相乘 |
| 什么时候不该上 | 延迟预算 < 1 ms、语言单一、规模小、没人能值班控制面 |
| 最该看的第一个指标 | sidecar 的 CFS 限流率 |

---

## 41.14 自检题（5 题，答案要点）

**1. 注入 sidecar 后 P50 从 3.0 ms 涨到 3.6 ms，P99 从 12 ms 涨到 31 ms，且 P99 曲线呈 100 ms 周期锯齿。先说最可能的原因，再说你会按什么顺序排除。**

最可能是 sidecar 的 CPU `limits` 太小被 CFS 限流：100 ms 是 `cpu.cfs_period_us` 默认值，配额用完的周期里剩下的请求被推到下个周期，尾部 +10~90 ms，而均值几乎不动。排除顺序：① 算单 Pod 请求数 × 单请求代理 CPU，对比配额（§41.1 事故一）；② 看 `container_cpu_cfs_throttled_periods_total ÷ periods`；③ 排除 access log/tracing 全采样（它们抬高单请求成本）；④ 最后才怀疑业务与下游。

**2. 为什么「有 NACK 兜底，所以推错配置不会全站挂」这句话是错的？**

NACK 只在配置**不合法**时发生（schema 错、引用不存在的资源）。事故三里 subset 标签拼错是合法配置：cluster 存在、端点集合为空，2,000 个代理全部 ACK 并立刻把流量导到 0 端点集群，7.8 秒全网 503。**能全站挂的恰恰是不 NACK 的那一类**——所以需要在变更流程里加语义断言（端点数为 0 就不许推、灰度命名空间先行）。

**3. 控制面挂掉之后，数据面能撑多久？这个时长由哪三个数决定？**

能继续转发已建立的连接，配置冻结在最后一份。时长由三个数决定：① 证书 TTL（SDS 下发证书的有效期）；② 轮转阈值（剩余多少比例时发起轮转）；③ **下一次发布/扩容的时间**——因为它会把「少数新连接」变成「多数新连接」，让证书过期从局部问题变成全站问题。所以真正的容忍窗口常远小于 TTL。

**4. 网格重试与 SDK 重试都开着，各 3 次和 2 次尝试，失败率 20%，平均放大约多少？为什么必须只留一处？**

`A_sdk = (1−0.2³)/0.8 = 1.24`，`A_mesh = (1−0.2²)/0.8 = 1.20`，相乘 ≈ 1.49，即到达下游的请求平均 +49%；同步无抖动时瞬时脉冲是 `3 × 2 = 6` 倍。只留一处的原因是：重试的收益是「提高成功率」，成本是「给已经过载的下游再加负载」，两处叠加等于把成本平方化而收益几乎不增（第二次重试的边际成功率极低）。

**5. 一个团队说「我们服务都是 Java、用 Dubbo、治理很成熟，想上网格统一 mTLS」。你会怎么回答？**

唯一无可替代的收益就是 mTLS/零信任身份与审计。所以先问：① 合规是否硬性要求东西向加密与可审计的「谁访问谁」？若是，网格确实是当前最完整的方案（每个语言的证书签发与轮转自己写一遍成本更高）；② 延迟预算是否有 > 1.5 ms 的余量？③ 谁值班控制面（证书轮转、升级、xDS 排障）？④ 端点抖动是否可控？四条都过再上，并按 §41.12 建指标；若只有 ① 成立而 ③ 不成立，优先选托管控制面，而不是自建。

---

## 41.15 延伸阅读（锚点式）

| 方向 | 去哪 |
| --- | --- |
| 入口的 listener/cluster、缓冲链、摘除护栏与优雅重载 | [39 入口网关与负载均衡](39-gateway-and-load-balancing.md) §39.5、§39.6、§39.8 |
| 连接复用、流控、重试放大公式（本篇 §41.6 的乘法链来源） | [40 RPC 与连接复用](40-rpc-and-connection-reuse.md) §40.6、§40.7 |
| 地址列表从哪来、推送风暴与健康检查误判 | [38 注册中心与配置中心](38-registry-and-config.md) |
| 探针、优雅关停与发布期空窗的应用侧细节 | [go/52 部署与 SRE](../go/52-deployment-sre.md) |
| 幂等与投递语义（为什么重试必须配幂等） | [eng/24 投递语义](../eng/24-delivery-semantics.md) |
| 对象存储/云盘的请求模型（网格里最先被 `REGISTRY_ONLY` 打中的外部依赖） | → comp/42 |
| 连接池与客户端超时链 | → comp/44 |
| 密钥与凭据轮转（证书 TTL 与 CA 单点的另一面） | → comp/47 |
| 组件故障谱：慢/满/脑裂/假活/语义漂移 | → comp/52 |
| 本套总纲：解剖卡八问、三本放大账与写作纪律 | [0-summary](0-summary.md) §0.4、§0.7 |
