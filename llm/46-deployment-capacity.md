# 46. 部署、容量与冷启动

> **状态**：`已完成（初稿）` ｜ **所属**：第九部分 · 平台工程底座 ｜ **编号**：46 / 52
> **本章定位**：前 45 章讲「系统长什么样」，这一章讲「**它跑在什么上、能扛多少、扩容要多久、重启会不会把用户的话截断**」。这些是架构评审里最容易被跳过、上线后最先炸的部分。
> **读法**：先看 §46.2 的三形态拆分（这是全章的地基），再抄 §46.4 的 Deployment 片段与优雅关停代码。**如果你只有 10 分钟：读 §46.4(1) 的容量算式和 §46.4(4) 的优雅关停。**

---

## 46.1 引子：周一 9:00，HPA 一动不动

周一早上 8:56，QPS 还是 400。9:00，企业客户上班，9:04，QPS 到了 **5,000**——4 分钟涨 **12.5 倍**。

你的平台看起来「应该能自动扩容」：HPA 配了 CPU 80% 触发。但监控上：

| 时间 | QPS | Pod 数 | CPU | 在途 Run | 客户端 5xx |
| --- | --- | --- | --- | --- | --- |
| 08:56 | 400 | 6 | 12% | 12,000 | 0% |
| 09:00 | 1,800 | 6 | 15% | 54,000 | 0.3% |
| 09:02 | 3,400 | 6 | 17% | 102,000 | 6% |
| 09:04 | 5,000 | **6** | **18%** | 150,000 | 12% |
| 09:07 | 5,000 | 6 → 24（人工） | 15% | 150,000 | 9% |
| 09:11 | 5,000 | 24 | 14% | 150,000 | 0%（恢复） |

**HPA 全程没动**，因为 CPU 从没超过 20%。

根因很朴素，但几乎每个 Agent 平台都会踩：**你的 Pod 大部分时间在「等模型」，不在「算」**。一次 Run 30 秒，其中 18 秒在等上游 token，CPU 只花了 300ms。所以：

- **CPU 利用率不是负载信号，是「你在不在干活」的信号**——而 Agent 服务的活儿主要是「等」。
- 真正的瓶颈是**在途连接数 / 在途 Run 数**：400 QPS × 30s = 12,000；5,000 QPS × 30s = 150,000，**涨了 12.5 倍，跟 QPS 同比例**。
- 你只有 6 个 Pod，每个 Pod 的 SSE 连接上限设成了 6,000 → 总容量 36,000 → **超了 4 倍**，多出来的只能排队或拒绝。

更糟的是 9:07 人工扩容之后还得等：新 Pod 冷启动 **94 秒**（镜像 1.8 GB 无节点缓存 18s + 连接池与配置预热 3s + 探针稳定 73s）。也就是说 9:07 到 9:08:34 之间，新容量是 0。

> 🩸 **这一章的核心命题**：
> Agent 服务的部署有三个反直觉点：
> ① **扩容信号是「在途数」不是 CPU**；② **冷启动是分钟级的，不是秒级的**——因为模型和连接都要预热；③ **重启会截断用户的半句话**——流式请求让「优雅关停」从 nice-to-have 变成正确性问题。
> 这三条不解决，你的自动扩缩容就是「在事故发生时，优雅地不工作」。

---

## 46.2 三种形态：无状态 API + 有状态 Worker + 模型网关

很多人第一版会写成一个「全能 Pod」：收请求、跑 Agent、调模型、发流。它在 100 QPS 时很好，在 5,000 QPS 时你会同时失去三样东西：**扩容粒度、故障隔离、发布能力**。

| 维度 | ① 无状态 API（Edge/BFF） | ② 有状态 Worker（执行器） | ③ 模型网关（Model Gateway） |
| --- | --- | --- | --- |
| 职责 | 鉴权、限流、SSE 终结、Run 提交与订阅、事件转发 | 真正执行 Run 的 Step 循环、调工具、checkpoint | 多 provider 适配、重试/failover、限流、计量前置、缓存前缀 |
| 状态 | **无状态**（SSE 连接是「会话态」但不是「数据态」） | **有 Run 亲和**（同一 Run 必须回同一 Worker 或支持状态外置） | 无状态（缓存除外） |
| 瓶颈资源 | **连接数 / fd / 内存** | **并发 Run 数 / goroutine / 内存** | **上游 QPS / 并发 / 配额** |
| 扩容信号 | `in_flight_connections` | `in_flight_runs` + 队列深度 | 上游 RPM/TPM + 本地队列 |
| 典型副本数 | 5k QPS → **36** | 150k 在途 → **84** | 5k QPS → **9** |
| 发布影响 | 断连接（必须优雅关停） | 丢 Run（必须 checkpoint） | 短暂 5xx（必须 failover） |
| 故障域 | 用户看到「连不上」 | 用户看到「Run 卡住」 | 用户看到「模型报错」 |
| 伸缩速度 | 秒级（快，连接可迁移） | 分钟级（要预热工具连接） | 秒级（但要预热上游连接池） |

**为什么必须拆开**，三条硬理由：

1. **扩容粒度不同**。API 的副本按「连接数」算，Worker 的副本按「Run 并发」算，两者的比例随业务变化（长任务多 → Worker 多；短问答多 → API 多）。绑在一起，你永远有一个维度是浪费的。
2. **发布节奏不同**。API 改的是协议与鉴权，可以一天发五次；Worker 改的是执行逻辑，涉及在途 Run，一天发一次都嫌多。
3. **故障隔离**。Worker 被长任务占满时，API 还能正常接收请求、排队、告诉用户「前方还有 42 位」；绑在一起就只能 502。

```
                          ┌─────────── 客户端（Web/IDE/IM）───────────┐
                          │  SSE：GET /runs/{id}/events              │
                          │       Last-Event-ID 断点续传（第 7 章）   │
                          └───────────────────┬──────────────────────┘
                                              ▼
                                    ┌──────────────────┐
                                    │  L4 LB / NLB      │  TCP，不做 HTTP 缓冲
                                    │  连接空闲 900s     │  反代会破坏流式（第 36 章）
                                    └────────┬─────────┘
                                             ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  ① 无状态 API  × 36（3 AZ × 12）                                │
        │  鉴权 → 限流 → 建 Run（写 PG）→ 投 Kafka → 订阅事件 → SSE 转发    │
        │  每 Pod 6,000 条 SSE 连接；上游用 HTTP/2 多路复用，TCP 只 500 条   │
        └───────────┬───────────────────────────────────┬────────────────┘
                    │ 投递（Kafka，按 org_id 分区）       │ 读事件（Redis Stream / Kafka）
                    ▼                                   ▼
        ┌───────────────────────────────┐   ┌───────────────────────────────┐
        │  ② 有状态 Worker × 84          │   │  Run 事件存储                  │
        │  Step 循环 / 工具 / checkpoint │   │  Redis（热）+ CH（明细）        │
        │  每 Worker 2,000 并发 Run      │   └───────────────────────────────┘
        └───────────┬───────────────────┘
                    │ 模型调用（统一出口，不允许 Worker 直连 provider）
                    ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  ③ 模型网关 × 9                                                │
        │  provider 适配 / failover / 限流(TPM,RPM) / 计量 / 前缀缓存命中   │
        └───────────┬──────────────────────────────┬─────────────────────┘
                    ▼                              ▼
        ┌────────────────────┐        ┌──────────────────────────────────┐
        │  外部 provider API  │        │  自建推理池（vLLM on GPU）        │
        │  （85% 的 token）   │        │  （15% 的 token，120 卡，池化）   │
        └────────────────────┘        └──────────────────────────────────┘
```

> ⚠️ 图中那条注释很重要：**Worker 绝不允许直连 provider**。一旦允许，你就失去了「统一限流、统一计量、统一 failover、统一缓存键」四个能力，而这四个能力是第 24、25、26、27 章的全部内容。

---

## 46.3 容量算术：从 QPS 反推每一个副本数

先立三条公式，后面所有数字都由它们推出来。**不要凭经验拍数字，算。**

```
① Little's Law：在途数 = 到达率 × 停留时间
   inflight_runs = QPS × Run 时长
   150,000 = 5,000 × 30s

② 副本数 = ceil(在途数 ÷ 单副本容量 ÷ AZ 冗余系数)
   单副本容量 = 单 Pod 并发上限 × 目标利用率（0.7~0.8）

③ 连接数账：SSE 连接 = 在途 Run 数（一问一流）
   每连接 fd = 1(客户端) + 0~1(上游，HTTP/2 复用后趋近 0) + 少量定时器
```

```
【① API 层：按连接数算】
  在途 SSE 连接 = 5,000 QPS × 30 s = 150,000
  单 Pod 连接上限 = 6,000（实测：8 核 16 GB，Go netpoll，每连接 6~8 KB 缓冲）
  目标利用率 80%  →  有效 4,800 / Pod
  基础副本 = 150,000 ÷ 4,800 = 31.25 → 32
  AZ 冗余（3 AZ，每 AZ 至少 N+1）→ 36（每 AZ 12 个）
  fd 预算 = 36 × 6,000 × 2.2 ≈ 475,000  → ulimit -n 需 ≥ 1,000,000（含突发）

【② Worker 层：按 Run 并发算】
  并发 Run = 150,000（与 API 的 SSE 连接一一对应）
  单 Worker 并发 Run = 2,000
    内存：2,000 × (4 KB 栈 + 40 KB 状态 + 20 KB 缓冲) ≈ 128 MB  ← 不是瓶颈
    瓶颈是 goroutine 调度与 GC 扫描面：> 3,000 时 P99 抖动明显
  基础副本 = 150,000 ÷ (2,000 × 0.8) = 94
  考虑批处理独立池（20% 槽位，第 44 章）→ 交互 76 + 批量 19 = 95 → 取 96（3 AZ × 32）

【③ 模型网关：按上游 QPS 算】
  上游调用 QPS = 5,000 QPS × 3 次模型调用 / Run = 15,000
  单网关 Pod = 8,000 转发 QPS（含限流判定、计量、日志，纯 IO 密集）
  基础副本 = 15,000 ÷ (8,000 × 0.7) = 2.7 → 3
  HA（每 AZ 1 个）→ 9

【④ GPU 池：按输出 token 算】
  日输出 token = 3,000 万 Run × 1,200 token = 360 亿/天
  自建承接 15%  →  54 亿/天  →  54e8 ÷ 86,400 = 62,500 tok/s（均值）
  自建部分峰均比 3×（大客户固定负载，波动小）→ 187,500 tok/s（峰值）
  单卡吞吐（70B-AWQ，vLLM continuous batching，8k 上下文）≈ 2,500 output tok/s
  目标利用率 75%  →  1,875 tok/s / 卡
  所需卡数 = 187,500 ÷ 1,875 = 100
  N+1 AZ 冗余 + 预热池 20%  →  120 卡（h100-80g × 120）

【⑤ 存储连接数】
  PG 连接：API 每 Pod 20 + Worker 每 Pod 10 = 36×20 + 96×10 = 1,680
           → 必须上 PgBouncer（transaction 模式），PG 侧 max_connections 800
  Redis 连接：每 Pod 8 → 1,056，用连接池共享，实际 ~300
```

| 组件 | 副本 | 单副本资源 | 扩容信号 | 扩容速度 |
| --- | --- | --- | --- | --- |
| API | 36 | 8C / 16 GB | `in_flight_connections` / Pod | 20~40s |
| Worker | 96 | 4C / 8 GB | `in_flight_runs` + 队列深度 | 30~60s（要预热工具连接） |
| 模型网关 | 9 | 4C / 8 GB | 上游 TPM / 本地队列 | 15~30s |
| GPU 池 | 120 卡 | 1 卡 1 副本 | `num_requests_waiting` | **3~10 分钟（要加载模型）** |
| PgBouncer | 3 | 2C / 2 GB | 连接数 | 秒级 |

> 💡 注意 GPU 那一行：**它的扩容速度比 CPU 组件慢一个数量级**。所以 GPU 容量规划不能靠 HPA 临时扩，必须「按周预留 + 池内弹性」。这就是下一节的事。

---

## 46.4 GPU 池化：四种共享方式，成本差 3 倍

GPU 是唯一「按卡计费、按分钟启动」的资源。池化的目标不是「提高利用率」这么笼统，而是**让利用率从 25% 提到 65%，同时保住 P99**。

| 共享方式 | 做法 | 隔离强度 | 利用率 | 适用 |
| --- | --- | --- | --- | --- |
| **① 独占整卡** | 一 Pod 一卡 | 最强 | 20~30% | 铂金客户 / 大模型 / 训练 |
| **② 时间分片** | 多 Pod 抢同一卡（CUDA MPS 或时间片） | 弱（显存不隔离，会相互 OOM） | 40~55% | 小模型 / 低频 |
| **③ MIG 硬切分** | A100/H100 切成 7 个实例（1g.10gb 等） | 强（显存/算力硬隔离） | 45~60% | 中模型（7B~14B）/ 多租户 |
| **④ 单进程多模型 + 连续批处理** | 一个 vLLM 进程服务同一模型的所有租户，靠请求级调度 | 逻辑隔离（靠限流与配额） | **60~75%** | **自建推理的主力** |

Aurora 的组合是 `④ 为主 + ③ 兜底 + ① 给铂金`：

```
┌─────────────────────────── GPU Node Pool A（共享，96 卡）──────────────────┐
│  4 个 vLLM Deployment：qwen-7b / llama-8b / rerank / embedding            │
│  每个 Deployment 按 --max-num-seqs 与显存切分，同一模型服务所有租户          │
│  租户隔离 = 网关层的 TPM 配额 + 请求级优先级（第 23 章）                    │
└───────────────────────────────────────────────────────────────────────────┘
┌──────────── GPU Node Pool B（MIG 切分，16 卡 → 112 个实例）───────────────┐
│  给「要求物理隔离但付不起整卡」的企业客户；1g.10gb 跑 7B 模型              │
└───────────────────────────────────────────────────────────────────────────┘
┌──────────── GPU Node Pool C（独占，5 客户 × 24 卡 = 120 卡）──────────────┐
│  铂金/受监管客户；tolerations + nodeAffinity 绑定到专属节点组              │
│  合同允许「闲置算力可被 A 池借用，5 分钟内可抢占回来」                      │
└───────────────────────────────────────────────────────────────────────────┘
```

vLLM 的关键启动参数（**这几个数字直接决定吞吐和 P99，别抄默认值**）：

```yaml
args:
  - "--model=/models/qwen2.5-7b-instruct-awq"
  - "--tensor-parallel-size=1"
  - "--max-num-seqs=256"            # 并发序列上限：太小吞吐低，太大 P99 炸
  - "--gpu-memory-utilization=0.90" # KV cache 吃掉剩余显存；0.95 容易 OOM
  - "--max-model-len=32768"         # 与业务上下文预算对齐（第 15 章），别设成模型最大值
  - "--enable-prefix-caching"       # 前缀缓存：System Prompt 复用能省 30%+（第 18 章）
  - "--enable-chunked-prefill"      # 长 prompt 分块，避免单个长请求卡住整个 batch
  - "--swap-space=8"                # CPU 换出，防突发 OOM kill
  - "--disable-log-requests"        # 高 QPS 下日志本身是瓶颈
```

> 🩸 `--max-num-seqs` 是这块唯一必须做实验的参数。设 512 时吞吐最高，但 TTFT P99 从 1.2s 涨到 4.8s；设 128 时 P99 好但 GPU 空转。**正确做法是：按你的 SLO（TTFT P95 ≤ 3.4s）反推，而不是按利用率反推。**

---

## 46.5 冷启动：94 秒是怎么来的，怎么压到 5 秒

冷启动的每一段都要单独量。**不分解，你就会以为「是镜像太大」，然后优化错地方。**

| 阶段 | 典型耗时 | 怎么测 | 优化手段 | 优化后 |
| --- | --- | --- | --- | --- |
| 调度 + 挂载卷 | 2~8s | `kubelet` 事件时间戳 | 预热节点池、本地 PV | 2s |
| 镜像拉取（1.8 GB） | **18s**（registry 限 100 MB/s）| `container_start` - `scheduled` | 分层镜像、节点镜像缓存、P2P 分发（Dragonfly） | **2s**（有缓存） |
| 解压 + 起进程 | 1~3s | 启动日志首行 | 静态编译 Go 二进制（无 JRE 启动） | 0.4s |
| **模型加载（40 GB 权重）** | **80s**（网络盘 500 MB/s）| 进程日志 | **节点本地 NVMe 缓存**（3 GB/s → 13.5s）+ 权重预取 DaemonSet | **14s** |
| CUDA context + 图捕获 | 15~25s | `torch.cuda` 计时 | `torch.compile` 缓存、CUDA graph 落盘 | 8s |
| 配置 + 连接池预热 | **1.5s**（PG 50 / Redis 200 / 网关 HTTP2 20，并行）| 启动探针日志 | 并行预热 + 只建最小连接数 | 1.5s |
| 探针稳定（readiness 连 3 次 × 1s） | 3s | 探针日志 | 首次成功即 ready（`failureThreshold: 1` 配合 startupProbe） | 1s |
| **合计（无缓存）** | **≈ 94s** | — | — | — |
| **合计（全优化）** | — | — | — | **≈ 29s（GPU）/ 7s（API）** |

但 29 秒依然太慢。**真正把冷启动变成「无感」的不是优化，是预热池：**

```
        ┌──── Warm Pool（预热池）────┐
        │ 常驻 10% 的空闲 Pod        │
        │ 已加载镜像、已建连接、未接流量 │
        │ 标记为 paused（不 ready）    │
        └──────────┬────────────────┘
                   │ 扩容时：直接 ready（< 2s），再从池里补一个新的
                   ▼
             流量立刻接上
```

代价很实在：常驻 10% 的空 Pod 意味着 **10% 的钱买「扩容速度」**。GPU 场景更贵（120 卡 × 10% = 12 卡常年空转）。所以：

| 场景 | 值不值 | 原因 |
| --- | --- | --- |
| API 层（36 Pod，每 Pod 8C16G） | ✅ 值 | 便宜，且流量最不可预测 |
| Worker（96 Pod） | ✅ 值 | 中等成本，扩容需求频繁 |
| GPU（120 卡） | ⚠️ 只预热 5%（6 卡） | 单卡贵，且 GPU 扩容本来就该提前规划 |
| 批处理 Worker | ❌ 不值 | 可以等 |

---

## 46.6 流式服务的连接与超时：六层，逐层收紧

流式请求的超时不是「设一个 60 秒」，而是**六层各自负责不同的事**。任何一层设错，表现都是「用户看到半句话然后卡住」。

| 层 | 超时项 | 建议值 | 作用 | 设错的后果 |
| --- | --- | --- | --- | --- |
| 客户端 | 首字节超时 | 10s | 用户耐心 | 太长：用户干等；太短：误杀慢 Run |
| 客户端 | 空闲超时 | 90s | 检测「服务端死了」 | 太长：断网时永远转圈 |
| LB / NLB | idle timeout | 900s | TCP 空闲回收 | **短于心跳间隔 → 静默断连** |
| 反代（Nginx） | `proxy_read_timeout` | 600s | 上游读超时 | **默认 60s → 流式中断** |
| 反代 | `proxy_buffering` | **off** | 不缓冲 | 开着 = 用户等 30 秒一次性看到全部（第 36 章） |
| API | 单次写超时 | 5s | SSE 写阻塞检测 | 太长：慢消费者拖死 goroutine |
| API | Run 总超时 | 30min | 兜底 | 无上限 → 泄漏连接 |
| Worker | 模型首 token 超时 | 30s | 上游卡住 | 太长：占着槽位；太短：长 prompt 误杀 |
| Worker | 工具调用超时 | 3~30s（按工具级） | 外部依赖 | 统一 30s → 快工具也等 30s |
| Worker | Step 总超时 | 120s | 防单步失控 | 无 → 一个死循环占满槽位 |

**心跳是流式的生命线**：

```
每 15 秒发一条注释行（不是 event，不触发前端渲染）：
   : keep-alive\n\n
```

为什么是 15 秒？因为要**小于最严格的那一层的空闲超时 / 3**。如果 NLB 是 900s、反代 600s、客户端 90s，取最小的 90s ÷ 3 = 30s，所以 15 秒是安全值。**如果你的心跳间隔大于客户端空闲超时，用户会在「模型正在思考」时看到断线。**

---

## 46.7 K8s 探针与优雅关停：让流式请求不中断

这是全章最容易出错、后果最直接的一节。**滚动更新时，默认行为会截断所有正在流式输出的 Run。**

### （1）三个探针各管一件事

| 探针 | 检查什么 | 失败后果 | 建议参数 | 常见错误 |
| --- | --- | --- | --- | --- |
| `startupProbe` | 进程是否起来了（含模型加载） | 重启容器 | `failureThreshold: 60`、`periodSeconds: 5`（给 5 分钟） | 不配 → 慢启动被 liveness 杀掉，无限重启 |
| `readinessProbe` | 能不能接流量 | **摘流量**（不重启） | `/readyz` 检查：配置已加载 + 上游连接池 ≥ 最小 | 检查「DB 能连」→ DB 抖一下全站摘流量 |
| `livenessProbe` | 进程是否卡死 | **重启容器** | `/healthz` 只检查事件循环是否转（`atomic` 时间戳） | 检查下游依赖 → 下游挂了你把自己重启光 |

> 🩸 铁律：**liveness 绝不检查外部依赖。** 你见过太多次「Redis 抖了 20 秒 → liveness 失败 → 全站 Pod 重启 → 恢复要 5 分钟」的雪崩。

### （2）Deployment 关键片段

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agent-api
spec:
  replicas: 36
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 25%
      maxUnavailable: 0          # 关键：先起新的，绝不先杀旧的
  template:
    spec:
      terminationGracePeriodSeconds: 600   # 必须 ≥ 最长 Run；短 Run 也用 600 兜底
      topologySpreadConstraints:           # 3 AZ 均匀打散
        - maxSkew: 1
          topologyKey: topology.kubernetes.io/zone
          whenUnsatisfiable: DoNotSchedule
          labelSelector: { matchLabels: { app: agent-api } }
      containers:
        - name: api
          image: registry/aurora-api:2026.03.12
          ports: [{ containerPort: 8080 }]
          resources:
            requests: { cpu: "4", memory: "8Gi" }
            limits:   { memory: "16Gi" }   # 不设 CPU limit：避免被 CFS 限流拖尾
          env:
            - { name: GOMEMLIMIT, value: "14GiB" }
            - { name: MAX_SSE_CONNS, value: "6000" }
          startupProbe:
            httpGet: { path: /startupz, port: 8080 }
            periodSeconds: 5
            failureThreshold: 60           # 最多等 5 分钟
          readinessProbe:
            httpGet: { path: /readyz, port: 8080 }
            periodSeconds: 5
            failureThreshold: 2
          livenessProbe:
            httpGet: { path: /healthz, port: 8080 }   # 只检查事件循环
            periodSeconds: 10
            failureThreshold: 6
          lifecycle:
            preStop:
              exec:
                # ① 先睡 10s：等 kube-proxy / LB 把本 Pod 从 endpoints 摘掉
                #    不等这段，"摘流量"和"SIGTERM"之间的竞态会丢请求
                command: ["/bin/sh", "-c", "sleep 10"]
```

```yaml
# 配合 Deployment 的 PDB：滚动更新与节点驱逐时，至少保留 90% 容量
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata: { name: agent-api-pdb }
spec:
  minAvailable: 90%        # 不是 maxUnavailable: 0 —— 那会让节点排空永远卡住
  selector: { matchLabels: { app: agent-api } }
---
# HPA 用「在途连接数」这个自定义指标，不是 CPU
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: { name: agent-api-hpa }
spec:
  scaleTargetRef: { apiVersion: apps/v1, kind: Deployment, name: agent-api }
  minReplicas: 36
  maxReplicas: 120
  metrics:
    - type: Pods
      pods:
        metric: { name: in_flight_sse_connections }
        target: { type: AverageValue, averageValue: "4800" }   # 6,000 × 80%
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 0      # 扩容要快，别犹豫
      policies: [{ type: Percent, value: 100, periodSeconds: 30 }]   # 30s 内最多翻倍
    scaleDown:
      stabilizationWindowSeconds: 600    # 缩容要慢，防抖动
      policies: [{ type: Pods, value: 4, periodSeconds: 60 }]
```

### （3）优雅关停：Go 侧必须做的四件事

```go
func main() {
	srv := &http.Server{Addr: ":8080", Handler: mux, ReadHeaderTimeout: 10 * time.Second}
	// ① 维护「本进程是否还能接新请求」的标志；readiness 读它
	healthy.Store(true)

	// ② SIGTERM：立刻把 readiness 置 false（preStop 的 10s 之外再加一道保险）
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGTERM, syscall.SIGINT)
	go func() {
		<-sig
		healthy.Store(false)         // readinessProbe 立刻失败，流量停止进入
		time.Sleep(5 * time.Second)  // 给 LB 一点时间收敛（与 preStop 叠加可能过长，见下）
		shutdown()
	}()

	_ = srv.ListenAndServe()
	select {} // 等 shutdown
}

func shutdown() {
	// ③ 停止接受新连接，但让在途请求继续；超时上限 = grace - 已用时间
	ctx, cancel := context.WithTimeout(context.Background(), 540*time.Second)
	defer cancel()

	// ④ 通知所有 SSE 连接：不是"掐断"，而是发一个结束事件再关
	sseHub.CloseAll(sse.CloseFrame{Reason: "server_draining", Reconnect: true})
	//    前端收到后会用 Last-Event-ID 重连到另一个 Pod（第 7 章）

	// ⑤ 等 Run 收尾：短 Run 等它跑完；长 Run 存 checkpoint 后交回队列
	if err := runner.Drain(ctx, runner.DrainPolicy{
		WaitFor:   45 * time.Second,  // 覆盖 P95（46s）的绝大部分
		Checkpoint: true,             // 超时的 Run 落 checkpoint（第 12 章）
		Requeue:    true,             // 交回 Kafka，由别的 Worker 续跑
	}); err != nil {
		log.Error("drain timeout, forcing close", "err", err)
	}

	_ = srv.Shutdown(ctx)             // 此时剩下的都是短连接，会很快结束
	metrics.Flush()
}
```

**这段代码里最关键的不是 `Shutdown()`，是第 ④ 步。** `http.Server.Shutdown` 会**等**在途请求结束，但一个 SSE 连接可能挂 30 分钟——等不完。所以必须主动给 SSE 客户端发一个「我下线了，去别处重连」的帧，让它**主动断开并重连**，而不是被 TCP RST 掐断（后者用户会看到「网络错误」）。

### （4）关停时序：每一步都在抢时间

```
t=0      K8s 开始删 Pod：从 endpoints 摘除 + 调 preStop（并发）
         ┌─ endpoints 摘除传播到所有 kube-proxy / LB：2~10s（不是瞬时！）
t=0      preStop: sleep 10 ────────────────────────────────┐
                                                            │
t=10     SIGTERM → healthy=false（readiness 立刻失败）        │
         ├─ 此时新流量基本已停止（两道保险叠加）              │
t=15     SSEHub.CloseAll(reconnect=true)                     │
         ├─ 客户端收到 "server_draining" → 用 Last-Event-ID 重连
t=15~60  runner.Drain：等在途 Run 跑完（P95 46s，多数已完成） │
         ├─ 超时的 Run：写 checkpoint + requeue              │
t=60     srv.Shutdown()：剩余短连接 graceful 关闭             │
t=65     进程退出                                             │
                                                            ▼
        terminationGracePeriodSeconds = 600 → 有 535s 余量，安全
```

> ⚠️ 一个常见 bug：`preStop sleep 10` **加上** Go 里再 `sleep 5`，看起来更保险，实际是把关停拖到 15s，而 `maxUnavailable: 0` + `maxSurge: 25%` 的情况下这会让滚动更新总时长从 8 分钟变成 20 分钟。**选一处做就够了**——推荐 preStop 做（因为它先于 SIGTERM）。

### （5）长任务（30 分钟）怎么办

短 Run 靠「等」，长 Run 只能靠「状态外置 + 重新调度」：

| Run 时长 | 关停策略 | 用户体验 |
| --- | --- | --- |
| < 60s | 等它跑完 | 无感 |
| 1~5 min | 等 + 超过 45s 就 checkpoint | 可能多等几秒 |
| 5~30 min | **不等**：立刻 checkpoint + requeue，客户端重连续订阅 | 短暂停顿后继续，事件流不丢 |
| 批处理 / 离线 | 直接抢占，任务回队列 | 无人在看 |

---

## 46.8 Java / 分布式经验对照

| 你熟悉的 | AgentOS 对应 | 同构点 | 会咬人的地方 |
| --- | --- | --- | --- |
| Tomcat 200 线程 / `server.tomcat.max-threads` | 6,000 条 SSE 连接 | 都是并发连接 | 线程模型下 6,000 连接 = 6,000 线程 = 12 GB 栈；Go 的 goroutine 是 4 KB 起 |
| `server.shutdown=graceful` + `spring.lifecycle.timeout-per-shutdown-phase` | `srv.Shutdown(ctx)` + Drain | 等都优雅关停 | Spring 的 graceful 只等 HTTP 请求，**不知道你的 SSE 客户端需要「重连提示」** |
| JVM 冷启动 + JIT 预热 | Go 二进制启动 | 都要预热 | JVM 还要 CDS/AppCDS；Go 启动 0.4s，但连接池与 tokenizer 仍要预热 |
| HPA on CPU | HPA on `in_flight` | 都是水平扩容 | 见引子：Agent 服务 CPU 不反映负载 |
| `@Async` 线程池 | Worker goroutine | 异步执行 | 线程池队列会「静默堆积」；goroutine 会「静默泄漏」，都要显式上限 |
| Ribbon/Hystrix 的连接池隔离 | HTTP/2 多路复用 + 每上游独立池 | 隔离慢调用 | HTTP/2 单连接会被队头阻塞（TCP 层），要配 `MaxConcurrentStreams` |
| K8s readiness + preStop | 同上 | 完全一样 | 唯一区别：你的「请求」可能持续 30 分钟，所以 grace 要按 Run 时长设 |
| Nacos 配置客户端启动依赖 | 配置本地快照 | 启动不能硬依赖 | 见第 43 章：配置中心挂了也要能起来 |
| 数据库连接池（HikariCP） | PgBouncer + Go `pgxpool` | 连接数管控 | 36 Pod × 20 = 720，直接打死 PG；必须 PgBouncer |

---

## 46.9 开源坐标

| 项目 | 它解决什么 | 缺什么 |
| --- | --- | --- |
| **K8s HPA / KEDA** | 水平扩容；KEDA 支持 Kafka lag、Prometheus 自定义指标 | 不内置「在途连接数」这类业务指标，要自己导出；对 GPU 扩容无能为力 |
| **K8s PDB / priorityClass / topologySpread** | 可用性兜底、优先级抢占、AZ 打散 | 需要和 `maxUnavailable: 0` 一起调，配错会卡住排空 |
| **KServe** | `InferenceService` 抽象、predictor/transformer、scale-to-zero（Knative）、canary 流量 | scale-to-zero 对 Agent 不友好（冷启动 90s）；不理解 SSE 长连接 |
| **vLLM** | continuous batching、PagedAttention、prefix caching、chunked prefill | 没有多租户配额（要在网关层做）；`--max-num-seqs` 要自己调 |
| **NVIDIA GPU Operator / MIG** | 驱动、设备插件、MIG 切分 | MIG 切分后不能动态改；收益与调度复杂度要权衡 |
| **Knative / KEDA HTTP add-on** | 请求驱动的伸缩（含缩到 0） | 冷启动是硬伤；流式长连接会被误判为「活跃」而无法缩容 |
| **Dragonfly / Kraken** | P2P 镜像分发 | 只解决镜像，不解决模型权重加载（那要 DaemonSet 预取 + 本地 NVMe） |
| **Argo Rollouts** | 灰度与自动回滚（第 42 章） | 不管容量与冷启动 |

---

## 46.10 事故清单

- [ ] **事故 A：HPA 按 CPU 扩容，全程没动（本章引子）。**
  现象：QPS 涨 12.5 倍，CPU 从 12% 到 18%，Pod 数不变，12% 请求 502。根因：Agent 服务的瓶颈是在途连接数，不是 CPU。兜底：HPA 用 `in_flight_sse_connections` 自定义指标 + 预扩（按时间表的 scheduled scaling）+ 预热池。
- [ ] **事故 B：滚动更新截断了 8 万个正在流式输出的回答。**
  现象：发布期间大量用户看到半句话 + 「网络错误」。根因：没配 `preStop`、grace 只有 30s、SSE 被 TCP RST 掐断。兜底：`maxUnavailable: 0` + preStop sleep 10 + grace 600s + `CloseAll(reconnect=true)` 发结束帧 + 前端 `Last-Event-ID` 重连。
- [ ] **事故 C：Redis 抖了 20 秒，全站 Pod 重启。**
  现象：一次 Redis 主从切换，所有 Pod 的 liveness 失败被重启，恢复花 6 分钟。根因：liveness 检查了 Redis。兜底：liveness 只查事件循环（`atomic` 时间戳），依赖健康只进 readiness。
- [ ] **事故 D：GPU 扩容要 5 分钟，流量峰值只有 4 分钟。**
  现象：扩容完成时峰值已过，白花钱还挨骂。根因：GPU 扩容速度（模型加载 80s + 调度）与流量上升速度（4 分钟）不匹配。兜底：按周预留 baseline + GPU 池化（MIG/连续批处理）提高单卡吞吐 + 排队 + 优先级调度（第 11 章）、降级到外部 provider 兜住溢出。
- [ ] **事故 E：`proxy_buffering` 没关，用户等 30 秒看到全部答案。**
  现象：TTFT 从 1.2s 变成 28s。根因：Nginx 默认缓冲。兜底：`proxy_buffering off` + `X-Accel-Buffering: no` + 压测里必须有 TTFT 指标（第 28 章）。
- [ ] **事故 F：PgBouncer 没上，PG 连接打满。**
  现象：`too many clients already`，全站 500。根因：36 API + 96 Worker 各开 20 连接 = 2,640 > PG 的 800。兜底：PgBouncer（transaction 模式）+ 每 Pod 连接上限 + 连接数告警。
- [ ] **事故 G：预热池的 Pod 被当成「空闲」缩掉了。**
  现象：扩容时发现没有预热 Pod，冷启动照旧。根因：预热 Pod 的 `paused` 标记不被 HPA 识别，被 scaleDown 清掉。兜底：预热池用独立 Deployment（`minReplicas` 固定，不挂 HPA），由扩容控制器在消费它之后异步补货。

---

## 46.11 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 三形态 | 无状态 API（连接数）/ 有状态 Worker（Run 并发）/ 模型网关（上游 QPS），必须拆 |
| Little's Law | 在途 = 5,000 QPS × 30s = **150,000**；所有副本数从这里推 |
| 副本数 | API 36 / Worker 96 / 网关 9 / GPU 120 卡 / PgBouncer 3 |
| 扩容信号 | **不是 CPU**，是 `in_flight_sse_connections` 与 `in_flight_runs` |
| fd 预算 | 36 × 6,000 × 2.2 ≈ 475k → ulimit ≥ 1M；上游靠 HTTP/2 复用 |
| GPU 池化 | 独占 25% / MIG 50% / 连续批处理 65~75%；成本差 3 倍 |
| `max-num-seqs` | 按 TTFT P95 SLO 反推，不按利用率反推 |
| 冷启动 | 94s（无缓存）→ 29s（NVMe 权重缓存 + 预热）→ 5s（预热池） |
| 预热池 | 10% 常驻 = 10% 的钱买扩容速度；GPU 只预热 5% |
| 超时六层 | 客户端 / LB 900s / 反代 600s + `buffering off` / API 5s / 首 token 30s / 工具分级 |
| 心跳 | 15s 一条注释行；必须小于「最严格空闲超时 ÷ 3」|
| 探针 | startupProbe 给 5 分钟；readiness 摘流量；**liveness 绝不查外部依赖** |
| 优雅关停 | preStop 10s + `maxUnavailable: 0` + grace 600s + **给 SSE 发 reconnect 帧** |
| 长任务 | 不等，checkpoint + requeue + 客户端按 `Last-Event-ID` 重连 |
| 一句话 | **Agent 服务的扩容信号是「在途」，冷启动是分钟级，重启会截断用户的话。** |

## 46.12 自检题

1. **你的 API 层 HPA 配了 CPU 70%，为什么上线后从不扩容？给出三个正确的扩容信号。**
   > 要点：① Agent 服务 90% 的时间在等上游 token，CPU 只占 15%，CPU 不是负载信号；② 正确信号：`in_flight_sse_connections`（连接数）、`in_flight_runs`（Worker）、队列深度 / 上游 TPM（网关）；③ 还要加 scheduled scaling（按历史曲线提前扩）与预热池，因为 HPA 的反应速度（30s）+ 冷启动（7~29s）跑不赢 4 分钟涨 12.5 倍的流量。

2. **滚动更新时，你如何保证 8 万个正在流式输出的连接不被截断？请按时间顺序说出每一步。**
   > 要点：① `maxUnavailable: 0` + `maxSurge: 25%`（先起后杀）；② `preStop: sleep 10`（等 endpoints 摘除传播，这步是竞态的解药）；③ 收到 SIGTERM 后立刻 `healthy=false` 让 readiness 失败；④ 调 `SSEHub.CloseAll(reconnect=true)` 给每个连接发「server_draining」帧，让客户端用 `Last-Event-ID` 主动重连到别的 Pod；⑤ `runner.Drain`：45s 内等 Run 跑完，超时的写 checkpoint + requeue；⑥ `srv.Shutdown`；⑦ `terminationGracePeriodSeconds ≥ 600` 保底。

3. **GPU 容量该按什么算？为什么不能用 HPA 临时扩？**
   > 要点：① 按输出 token 算：日输出 token × 自建占比 ÷ 86400 × 峰均比 ÷ 单卡有效吞吐（`2,500 × 75%`）= 卡数；Aurora 是 360 亿/天 × 15% → 62,500 tok/s 均值 × 3 峰均 → 187,500 ÷ 1,875 = 100 卡，加冗余取 120。② 不能用 HPA：模型加载 40 GB（网络盘 80s）+ 调度 + CUDA 预热 = 3~10 分钟，而流量峰值可能只有 4 分钟。③ 正确做法：按周预留 baseline + 池化提高单卡吞吐 + 溢出走外部 provider + 排队与优先级。

## 46.13 延伸阅读

- 上一章：[45. 存储架构](45-storage-architecture.md)（Worker 的数据亲和与连接数预算）
- 下一章：[47. 成本工程（FinOps）](47-cost-engineering.md)（容量与单位经济模型的关系）
- 总纲：[0-summary.md](0-summary.md)（§1「cgroup 限额 → 配额/限流/预算刹车」）
- 相关：[7. 流式传输](7-streaming-transport.md)（SSE 与断点续传）、[10. 一次请求的 12 站旅程](10-request-lifecycle.md)（超时预算）、[11. 排队与公平性](11-admission-and-queue.md)（扩容不及时的兜底）、[14. 调度器设计](14-scheduler-design.md)（并发度标定）、[25. 降级与容错](25-degradation-failover.md)（GPU 溢出到外部 provider）、[28. 大流量不崩](28-peak-traffic.md)（压测与连接账）
- 外部：K8s `PodDisruptionBudget` / `topologySpreadConstraints` / 探针文档、vLLM `--max-num-seqs` 与 prefix caching 文档、KServe `InferenceService` 文档、NVIDIA MIG 用户指南（版本与参数以官方最新文档为准）
- 语言细节：[../go/17-goroutine-internals.md](../go/17-goroutine-internals.md)（goroutine 栈与 150k 并发的内存账）、[../go/32-high-concurrency.md](../go/32-high-concurrency.md)（连接数、fd、TIME_WAIT）、[../go/39-realtime-sse-websocket.md](../go/39-realtime-sse-websocket.md)（SSE 实现细节）
