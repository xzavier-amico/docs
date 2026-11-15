# 52. 生产部署与 SRE：把服务活到线上

> **状态**：`已完成（初稿）` ｜ **所属**：第九部分 · 生产部署与收尾 ｜ **编号**：52 / 54
> **本章定位**：前面 51 章教你「写对」，这一章教你「活下来」。一个 Go 服务上线后 80% 的事故不在业务代码里，而在镜像、探针、limit、关停、发布这五件「运维小事」上——而它们恰好每一条都和 runtime 有耦合，是 Go 独有的坑。
> **读法**：明天要上线，就读 52.3 → 52.5 → 52.6 → 52.14（checklist 直接抄）；SLO、录制采样、混沌演练（52.10~52.13）等团队有 SLO 需求时再回来读。

---

## 52.1 引子：凌晨两点，三个 Pod 一起 CrashLoopBackOff

`go-deep-agent` 服务第一次上生产，Go 写得挺漂亮：接口小、ctx 贯穿、-race 全绿。上线第三天，值班电话响了：

```
🩸 02:07  alert: agent-api 可用副本 1/4，5xx 率 37%
   kubectl get pods
   agent-api-7d9c8f-2xk4p   0/1  CrashLoopBackOff   12   4m31s   ← OOMKilled
   agent-api-7d9c8f-9vq7m   0/1  Running            0    42s     ← 刚被 liveness 重启完
   agent-api-7d9c8f-bn2zt   1/1  Running            3    9m       ← 活着，但一直重启
```

复盘三件事，全是本章的主题：

1. **OOMKilled，但 heap 才 600MB，limit 是 1Gi。** 因为 `GOMAXPROCS` 按宿主机 32 核算，32 个 P 各自抓一个 mcache、各自有本地分配缓存；GC 目标 `GOGC=100` 意味着「堆翻倍才回收」，瞬时尖峰直接顶穿 limit。这不是内存泄漏，是**三个账本没对齐**。
2. **liveness 探针里查了数据库。** 数据库抖了一下 → 四个 Pod 同时 liveness 失败 → 全部重启 → 冷启动期又把数据库连接打满 → 雪崩。**探针写错比没探针更致命。**
3. **每次发布都有 40 秒 5xx。** 因为进程收到 SIGTERM 直接 `os.Exit(0)`，而 Ingress 还在往这个 Pod 转发——**优雅关停是一条接力，你只跑了第一棒**。

> 🎯 本章的立场：**Go 部署的核心不是「打包」，是「对齐」**——把 runtime 的三个旋钮（`GOMAXPROCS` / `GOMEMLIMIT` / `GOGC`）和 Kubernetes 的三个账本（request/limit、probe、gracePeriod）对齐。对齐了，一个 20MB 的静态二进制可以在 5MB 镜像里跑得比 fat jar 稳得多。

---

## 52.2 直觉模型：一个 Go 二进制的三段人生 + 三个账本

先建立脑图。Java 的世界是「容器里跑 JVM，JVM 里跑 jar」，你知道 JVM 有一堆 `-Xmx` 要调。Go 的世界是「容器里直接跑一个进程」，**没有虚机层，所以 runtime 必须自己去问 cgroup 我能用多少**——问不到的时候，它就按物理机算，然后就炸了。

```
 源码                静态二进制              OCI 镜像                 Pod
┌──────┐   go build  ┌──────────────┐  COPY  ┌──────────┐  schedule ┌──────────────┐
│ .go  │ ──────────► │ 15~25MB      │ ─────► │ 5~8MB    │ ────────► │ 1 个进程      │
│ go.mod│  CGO=0     │ 无动态依赖    │        │ 无 shell │           │ + cgroup     │
└──────┘  trimpath   │ 无 JVM 预热  │        │ 无 glibc │           │ + 探针       │
                     └──────────────┘        └──────────┘           └──────────────┘
                                                                          │
                                            三个账本必须同时对齐 ◄────────┘
                      ┌────────────────┬─────────────────┬──────────────────────┐
                      │ 内存账          │ CPU 账           │ 时间账                │
                      │ limit.memory    │ limit.cpu        │ terminationGrace     │
                      │   ≥ GOMEMLIMIT  │   ≈ GOMAXPROCS   │   ≥ preStop          │
                      │   + 非堆(15%+)  │   (1.25 自动)     │   + Shutdown 预算     │
                      └────────────────┴─────────────────┴──────────────────────┘
```

三个账本各自的错法，构成三种典型事故：

| 账本 | 不齐会怎样 | 症状 |
| --- | --- | --- |
| 内存账 | GC 目标超过 cgroup limit | 不 OOM 但一直高 GC（CPU 打满）；或者突然 `OOMKilled`，`kubectl describe` 里 `Last State: OOMKilled, Exit Code: 137` |
| CPU 账 | `GOMAXPROCS` 远大于 quota | 大量 `sched_*` 延迟、P99 抖动、`container_cpu_cfs_throttled_seconds_total` 飙升，但 CPU 平均利用率只有 20% |
| 时间账 | grace 小于「摘流量 + 关停」耗时 | 每次发布/缩容都有 5xx，日志里是 `connection reset by peer` |

> 💡 **Java 对照的一句话**：Java 那边 `-Xmx` 是你显式给的、容器感知从 JDK 10 起就有 `UseContainerSupport`；Go 这边 `GOMEMLIMIT` 是**软限制**（超过它也不会 OOM，只是更卖力 GC），真正的硬墙还是 cgroup。所以你必须**两个都设**，而不是二选一。详见 52.5。

---

## 52.3 编译产物与镜像：把 400MB 的 fat jar 换成 8MB 的层

### 52.3.1 `CGO_ENABLED=0` 到底改变了什么

Java 打个 fat jar 就是「一堆 class + 一个 JRE」，Go 编出来是**一个静态可执行文件**——但前提是关掉 cgo，否则你会得到一个依赖 `libc.so.6` 的动态链接 ELF。

| 维度 | `CGO_ENABLED=1`（默认，有 gcc 时） | `CGO_ENABLED=0`（生产推荐） |
| --- | --- | --- |
| 链接方式 | 动态链接 glibc | 全静态，`ldd` 显示 `not a dynamic executable` |
| DNS 解析 | 可能走 glibc 的 `getaddrinfo`（NSS、`/etc/nsswitch.conf`） | 纯 Go resolver，读 `/etc/resolv.conf`，不依赖 NSS |
| `os/user` | cgo 版，走 NSS/PAM | 纯 Go 版，读 `/etc/passwd` |
| 基础镜像 | 必须带 glibc（debian-slim / distroless base） | `scratch` / `distroless/static` 都行 |
| 启动 | 无 JIT 预热，几十毫秒 | 一样 |
| 代价 | — | 用不了 SQLite 的 cgo 版、某些推理库、libpcap 等 |

> ⚠️ 那个「DNS 解析行为变了」的坑值一条命：有人从 alpine 换到 scratch 后发现内网域名解析失败，原因是纯 Go resolver 不读 `/etc/nsswitch.conf`、也不支持 mDNS/NetBIOS。所以**生产上纯 Go resolver 的行为要在预发环境验证过一次**，别等上线才发现。
>
> 反过来，如果你确实需要 cgo（比如接了 onnxruntime、sqlite3），那就老老实实用 `distroless/base` 或 `debian:*-slim`，并且**在镜像里把 `libgomp`、`libstdc++` 这些依赖装齐**——`ldd ./server` 一个个核。

### 52.3.2 完整 Dockerfile（可直接抄）

```dockerfile
# syntax=docker/dockerfile:1.7
# ---------- 阶段 1：编译 ----------
FROM golang:1.25-alpine AS build
WORKDIR /src

# 只拷依赖清单，让 go mod download 这层能被缓存住
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod \
    go mod download

COPY . .

ARG VERSION=dev
ARG COMMIT=unknown
ARG BUILD_TIME=unknown

ENV CGO_ENABLED=0 \
    GOOS=linux \
    GOFLAGS=-trimpath

# -trimpath：去掉 /src 这类本地路径，产物可复现，也避免泄露构建机路径
# -buildvcs=false：builder 里通常没有 .git，省掉一次无用的版本探测
# -s -w：去符号表与 DWARF，体积小 25%~30%（要 delve 现场调试就别加）
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    go build -buildvcs=false \
      -ldflags="-s -w -X main.version=${VERSION} -X main.commit=${COMMIT} -X main.buildTime=${BUILD_TIME}" \
      -o /out/server ./cmd/server

# ---------- 阶段 2：运行 ----------
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/server /server
# 时区/证书：代码里 import _ "time/tzdata" 就不需要拷 zoneinfo（见 52.3.3）
USER nonroot:nonroot
EXPOSE 8080 9090
ENTRYPOINT ["/server"]
```

顺手把 `.dockerignore` 也加上，不然 `COPY . .` 会把 `.git`、`node_modules`、`*.log` 全塞进构建上下文：

```
.git
.idea
*.log
tmp/
docs/
examples/
```

镜像构建完，做三件事自证：

```bash
docker build --build-arg VERSION=v1.7.3 --build-arg COMMIT=$(git rev-parse --short HEAD) -t agent-api:v1.7.3 .
docker run --rm agent-api:v1.7.3 -version       # 版本注入成功了吗
docker create --name x agent-api:v1.7.3 && docker export x | tar -tvf - | head
go version -m ./server                          # 反查这个二进制到底用哪些模块编的（排 supply chain 问题神器）
```

`go version -m` 能打出 `build` 信息表（`-ldflags`、`vcs.revision`、所有依赖版本），**这是 Go 相对 jar 的一个隐形优势**：二进制自己带着 SBOM 的一半信息。

### 52.3.3 时区与证书：两个「本地能跑线上就挂」的经典

| 问题 | 现象 | 正确写法 |
| --- | --- | --- |
| 时区 | `time.LoadLocation("Asia/Shanghai")` 报 `unknown time zone Asia/Shanghai` | ① `import _ "time/tzdata"`（Go 1.15+，把 IANA 库嵌进二进制，多 ~450KB，一劳永逸）② 镜像里带 `/usr/share/zoneinfo` ③ 只设 `TZ=Asia/Shanghai` 环境变量 |
| CA 证书 | 调 https 接口报 `x509: certificate signed by unknown authority` | ① `distroless/static` 自带 `ca-certificates`；② 用 `scratch` 就得从 builder 拷 `/etc/ssl/certs/ca-certificates.crt`；③ 内部 CA / mTLS 要自己注入 bundle |
| 用户 | `runAsNonRoot` 校验失败 | `USER nonroot:nonroot`（distroless 里 uid=65532），`securityContext.runAsUser: 65532` 对齐 |

```go
package main

import (
	_ "time/tzdata" // 把 IANA 时区数据库编进二进制：scratch/distroless 里没有 /usr/share/zoneinfo
)
```

> 🩸 时区坑的真实版本是这样的：本地 mac 有 zoneinfo，测试全绿；上线后所有「当日汇总」都算错了 8 小时。**Go 的 `time.Now()` 在 UTC 下是对的，但 `time.Now().In(loc)` 需要库文件。** 加一行 blank import 就再也不会疼。

### 52.3.4 基础镜像怎么选

| 基础镜像 | 大小 | shell | CA 证书 | tzdata | 适合 |
| --- | --- | --- | --- | --- | --- |
| `scratch` | 0 | ✗ | 需自拷 | 需自嵌 | 极致小，但一切都得自己带，排障为 0 |
| `gcr.io/distroless/static:nonroot` | ~2MB | ✗ | ✓ | 视 tag 而定 | **Go 服务默认选它** |
| `gcr.io/distroless/base` | ~20MB | ✗ | ✓ | ✓ | 需要 glibc（cgo） |
| `alpine:3.21` | ~8MB | ✓ busybox | ✓ | 需 `apk add tzdata` | 要现场 `sh` 排障 |
| `debian:*-slim` | ~75MB | ✓ | ✓ | ✓ | 复杂 cgo 依赖 |

⚠️ **distroless 没有 shell，这会影响你的 preStop 和排障姿势**——`kubectl exec -it pod -- /bin/sh` 会失败。要么换 `kubectl debug`（ephemeral container），要么本地备一个 `:debug` 变体镜像（同一二进制，基础镜像换成 alpine）。下面 52.6 的 preStop 会专门处理这一点。

---

## 52.4 配置与密钥：12-factor 在 Go 里的正确姿势

Go 没有 Spring 的 `@ConfigurationProperties` + `@RefreshScope`，配置文件全靠自己读。这反而是好事：**配置读取是纯函数，可以在 `main` 里一次读完、校验完、再开始干活**。

### 52.4.1 一条铁律

```
启动时一次性读 → 结构体 → 校验（fail fast）→ 传下去（依赖注入）
       │
       └── 业务代码里禁止 os.Getenv / 禁止再读文件
```

为什么禁止在业务代码里 `os.Getenv`？因为那等于把配置变成**全局可变隐式依赖**，测试时只能靠 `t.Setenv` 打补丁，而且拼错 key 不会有任何提示，只会在凌晨两点返回空字符串。

### 52.4.2 完整 Load：env + 文件密钥 + 校验

```go
package config

import (
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	Addr       string
	LogLevel   string
	LLMTimeout time.Duration
	MemRatio   float64 // GOMEMLIMIT 占容器 limit 的比例
	APIKey     string  // 只从文件读，不从 env 读
	DBDSN      string
}

// Load 必须满足：纯函数、无全局状态、缺关键项立刻报错。
func Load() (Config, error) {
	c := Config{
		Addr:       env("ADDR", ":8080"),
		LogLevel:   env("LOG_LEVEL", "info"),
		LLMTimeout: envDur("LLM_TIMEOUT", 60*time.Second),
		MemRatio:   envFloat("GOMEMLIMIT_RATIO", 0.85),
	}

	// 密钥走 *_FILE（K8s Secret 挂成文件 / Docker secret / Vault agent 都遵守这个约定）
	if p := os.Getenv("OPENAI_API_KEY_FILE"); p != "" {
		b, err := os.ReadFile(p)
		if err != nil {
			return c, fmt.Errorf("read OPENAI_API_KEY_FILE: %w", err)
		}
		c.APIKey = strings.TrimSpace(string(b))
	}

	if err := c.validate(); err != nil {
		return c, err
	}
	return c, nil
}

func (c Config) validate() error {
	var problems []error
	if c.MemRatio <= 0.5 || c.MemRatio >= 0.95 {
		problems = append(problems, fmt.Errorf("GOMEMLIMIT_RATIO=%v out of (0.5,0.95)", c.MemRatio))
	}
	if c.LLMTimeout <= 0 {
		problems = append(problems, fmt.Errorf("LLM_TIMEOUT must be > 0"))
	}
	if c.APIKey == "" {
		problems = append(problems, fmt.Errorf("OPENAI_API_KEY_FILE not set"))
	}
	return errors.Join(problems...) // 1.20+：一次把问题全报出来，别让运维改一轮重启一轮
}
```

配套小工具函数（三行一个，不必引库；要更全可以用 `caarlos0/env` 或 `kelseyhightower/envconfig`）：

```go
func env(k, def string) string {
	if v, ok := os.LookupEnv(k); ok && v != "" {
		return v
	}
	return def
}

func envDur(k string, def time.Duration) time.Duration {
	if v, ok := os.LookupEnv(k); ok {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}

func envFloat(k string, def float64) float64 {
	if v, ok := os.LookupEnv(k); ok {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			return f
		}
	}
	return def
}
```

> 💡 **`errors.Join` 是 Go 1.20 给的礼物**，用来做「配置校验一次报全」特别顺手，比 Java 那种 `BindingResult` + 一堆 `@NotNull` 省事。详见第 7 章。

### 52.4.3 env vs 文件 vs 密钥系统

| 方式 | 适合 | 风险 / 注意 |
| --- | --- | --- |
| 环境变量 | 普通配置（超时、日志级别、比例参数） | **密钥别放这里**：`/proc/<pid>/environ`、crash dump、`kubectl describe`、子进程继承都会泄露 |
| Secret 挂载成文件 | 密钥、证书、token | 轮转要注意：kubelet 更新挂载文件有延迟（分钟级），程序得自己定期重读或监听变化 |
| Vault + Agent Injector | 动态短 TTL 凭据、数据库临时账号 | 启动链路多一跳，要处理好 Vault 不可用时的降级 |
| External Secrets Operator | 把云 KMS / Vault / AWS SM 同步成 K8s Secret | 明文最终还是落进 etcd，要开 etcd 加密 |
| SealedSecret / SOPS | GitOps：密文进 Git 仓 | 单向、与集群密钥绑定，换集群要重新加密 |

### 52.4.4 配置热更新：多数时候你不需要

Kubernetes 世界的标准做法是**改配置 → 滚动重启**（`kubectl rollout restart`），因为重启成本就是 1~2 秒（静态二进制没有 JVM 预热，这是 Go 的红利，详见第 53 章）。

只有当「重启有状态损失」时才值得做热更新：长连接（WebSocket 会话）、正在跑的 Agent 长任务、限流阈值、日志级别。

```go
// 只对「无状态、可原子替换」的配置做热更新：用 atomic.Pointer 整体换掉，读侧零锁
var cfgPtr atomic.Pointer[config.Runtime]

func reload(ctx context.Context, path string) error {
	b, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	var next config.Runtime
	if err := yaml.Unmarshal(b, &next); err != nil {
		return fmt.Errorf("parse %s: %w", path, err) // 解析失败就保留旧配置，绝不半更新
	}
	if err := next.Validate(); err != nil {
		return err
	}
	cfgPtr.Store(&next) // 一次原子替换，读侧 cfgPtr.Load() 永不加锁
	return nil
}
```

在 `main` 里挂 `signal.Notify(hup, syscall.SIGHUP)` 或 `fsnotify` 触发 `reload` 即可。⚠️ 热更新的最大风险是**同一时刻不同 goroutine 读到不同配置**导致行为不一致；用「单指针整体替换」可以消灭大部分这类问题，但**跨字段不变式**（`a ≥ b`）依然只能靠迁移设计规避。

---

## 52.5 K8s 落地：把 runtime 的三个旋钮和 limit 对齐

这一节是本章的技术核心。Java 那边你调 `-Xmx`；Go 这边你要同时理解 **cgroup 硬墙** 和 **runtime 软目标**。

### 52.5.1 requests / limits 的真实语义（别照搬 Java 时代的经验）

| 资源 | request 影响什么 | limit 影响什么 | Go 特有的坑 |
| --- | --- | --- | --- |
| CPU | 调度决策、HPA 的 CPU 利用率分母 | CFS quota，超出被 **throttle**（不是杀） | `GOMAXPROCS` 默认 `NumCPU`（1.24 及以前）会远超 quota |
| 内存 | 调度决策 | 超出 **OOMKill**（exit 137） | `GOMEMLIMIT` 只是软限制，救不了超 limit 的尖峰 |

> ⚠️ **CPU limit 在 Go 服务上是个有争议的选择**：设了 limit 就有 CFS 节流，100ms 一个周期，一旦某个周期用完 quota，后面几十毫秒内所有 goroutine 被强制挂起——延迟长尾就是这么来的。常见三种策略：
>
> | 策略 | 配置 | 适合 |
> | --- | --- | --- |
> | 不设 CPU limit，只设 request | `requests.cpu: 500m`，无 limit | 延迟敏感、集群资源充足、能容忍偶发抢占 |
> | 设 limit = 2~4× request | `requests: 500m`, `limits: 2` | 折中，最常见 |
> | 设 limit ≈ request | `500m / 500m`（Guaranteed） | 强确定性，但 must 让 `GOMAXPROCS` 跟上 |

### 52.5.2 `GOMAXPROCS`：Go 1.25 的容器感知（以及 1.24 之前怎么办）

Go 1.24 及以前，`runtime.GOMAXPROCS(0)` 默认等于 **`NumCPU`（宿主机的核数）**，`sched_getaffinity` 在容器里通常也不受 cpu quota 限制。于是：

```
节点 32 核，Pod limit = 1 核
   GOMAXPROCS = 32 → 32 个 P + 32 个 m 抢 1 核的 quota
   → 每个 100ms 周期开头猛干，用完 quota 集体被 throttle
   → 请求 P99 抖动，CPU 利用率看起来只有 20%
```

Go 1.25 起，runtime 会把 **cgroup CPU quota 纳入 `GOMAXPROCS` 的默认值**（这就是「容器感知 GOMAXPROCS」）。按官方说明，这一行为在检测到容器 CPU 限制时默认启用，可用 `GODEBUG=containermaxprocs=0` 关掉；CPU limit 变化时是否动态更新由 `updatemaxprocs` 控制，默认不跟随。（`GODEBUG` 开关名与默认值随版本可能调整，以你本地的 release notes 为准。）

那么三种情况：

```go
// 情况 A：Go 1.25+，什么都不用做，默认就是对的。
//          只想确认值对不对，启动时打一行日志：
slog.Info("runtime", "gomaxprocs", runtime.GOMAXPROCS(0), "numcpu", runtime.NumCPU())

// 情况 B：Go 1.24 及以前 —— 引入 uber 的 automaxprocs，一行 blank import
import _ "go.uber.org/automaxprocs"

// 情况 C：显式钉死（可预测性最高，但换机型要改配置）
//   环境变量 GOMAXPROCS=2，或在 main 里：
runtime.GOMAXPROCS(2)
```

> 💡 **自动化 + 显式，永远选显式里的「自动化」**：优先靠 1.25 的默认行为，其次 `automaxprocs`，最后才是写死 `GOMAXPROCS` 环境变量。写死的唯一理由是「你的服务对 P 的数量极端敏感」（比如纯 CPU 密集的 rerank 服务）。

### 52.5.3 `GOMEMLIMIT` 与 memory limit 的对齐公式

`GOMEMLIMIT`（Go 1.19+）是**软限制**：runtime 会尽力把 Go 管理的堆总量控制在这个值以下，超了不会杀你，而是提高 GC 频率（Pacer 会牺牲 CPU 换内存）。`GOGC=100`（默认）是**增长比例**：堆翻倍就回收一次。

```
容器 memory limit = 2Gi
                     │
        ┌────────────┴────────────┐
        │                         │
   GOMEMLIMIT = 0.85 × limit  非堆（≤15%）
   ≈ 1700MiB                  ≈ 300MiB
   ┌──────────────┐           ┌───────────────────────────┐
   │ Go heap      │           │ goroutine 栈（每个 2KB 起） │
   │ + runtime 元 │           │ mcache / span / bitmap     │
   │   数据        │           │ mmap 的 arena 未归还部分     │
   │              │           │ 调用方 buffer / cgo 内存     │
   └──────────────┘           │ TLS/连接缓冲 / zstd 窗口    │
                              └───────────────────────────┘
```

比例怎么定？经验值 **0.8 ~ 0.9**，然后按下面三步校准：

1. 先设 `GOMEMLIMIT = 0.85 × limit`，跑一周压测；
2. 看 `go_gc_gogc_percent`、`go_memstats_heap_alloc_bytes`、`container_memory_working_set_bytes`，如果 working set 长期贴近 limit 且 `go_gc_cycles_total` 频繁，说明非堆部分大于 15%，把比例降到 0.75；
3. 如果 GC 吃掉太多 CPU（`go_gc_cpu_fraction > 0.25`），说明你该加内存或者减对象分配，而不是继续压 GOMEMLIMIT。

一段可直接用的启动自检（两个旋钮二选一，别都配——env 会在 init 阶段生效，随后 `debug.SetMemoryLimit` 会覆盖它，容易造成「我明明设了却没生效」）：

```go
package main

import (
	"log/slog"
	"os"
	"runtime"
	"runtime/debug"
	"strconv"
	"strings"
)

// tuneRuntime 让 runtime 的软目标和 cgroup 硬墙对齐，并把结果写进日志便于事后归因。
func tuneRuntime(ratio float64) {
	if limit := cgroupMemoryLimit(); limit > 0 && os.Getenv("GOMEMLIMIT") == "" {
		debug.SetMemoryLimit(int64(float64(limit) * ratio))
	}
	slog.Info("runtime tuned",
		"gomaxprocs", runtime.GOMAXPROCS(0),
		"gomemlimit_mib", debug.SetMemoryLimit(-1)>>20, // 传负数 = 只读当前值，不修改
		"gogc", os.Getenv("GOGC"),
		"cgroup_limit_mib", cgroupMemoryLimit()>>20,
	)
}

// cgroupMemoryLimit 读 cgroup v2 的 memory.max，回退到 v1。读不到返回 0（裸机/本地开发）。
func cgroupMemoryLimit() int64 {
	for _, p := range []string{
		"/sys/fs/cgroup/memory.max",                    // cgroup v2
		"/sys/fs/cgroup/memory/memory.limit_in_bytes",  // cgroup v1
	} {
		b, err := os.ReadFile(p)
		if err != nil {
			continue
		}
		s := strings.TrimSpace(string(b))
		if s == "max" {
			continue
		}
		if n, err := strconv.ParseInt(s, 10, 64); err == nil && n > 0 && n < 1<<60 {
			return n
		}
	}
	return 0
}
```

> 🩸 **一个真实的反模式**：「怕 OOM，所以 `GOGC=20`」。这会把 GC 频率提高 5 倍，CPU 全烧在标记上，吞吐直接腰斩。**正确的顺序是：先设 `GOMEMLIMIT`，再考虑 `GOGC`；`GOGC=off` 只在「我已经用 GOMEMLIMIT 精确控住内存」时才敢用。** 详见第 28 章。

### 52.5.4 完整 Deployment YAML（可直接抄）

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agent-api
  labels: { app: agent-api }
spec:
  replicas: 4
  revisionHistoryLimit: 5
  minReadySeconds: 10            # 新 Pod ready 后观察 10s 才算真可用，避免"秒挂"版本吃掉全部流量
  progressDeadlineSeconds: 300
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0          # 一个都不许少：宁可慢，不可错
  selector:
    matchLabels: { app: agent-api }
  template:
    metadata:
      labels: { app: agent-api }
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "9090"
        prometheus.io/path: "/metrics"
    spec:
      terminationGracePeriodSeconds: 60   # 时间账：preStop(10s) + Shutdown(≤30s) + 余量(20s)
      securityContext:
        runAsNonRoot: true
        runAsUser: 65532                  # = distroless 的 nonroot
        runAsGroup: 65532
        seccompProfile: { type: RuntimeDefault }
      topologySpreadConstraints:          # 打散到多可用区，别让一次 AZ 故障带走全部副本
        - maxSkew: 1
          topologyKey: topology.kubernetes.io/zone
          whenUnsatisfiable: ScheduleAnyway
          labelSelector:
            matchLabels: { app: agent-api }
      containers:
        - name: app
          image: registry.example.com/agent-api:v1.7.3
          imagePullPolicy: IfNotPresent
          ports:
            - { name: http,    containerPort: 8080 }
            - { name: metrics, containerPort: 9090 }
          env:
            - { name: LOG_LEVEL, value: "info" }
            - { name: OTEL_SERVICE_NAME, value: "agent-api" }
            - { name: OTEL_TRACES_SAMPLER, value: "parentbased_traceidratio" }
            - { name: OTEL_TRACES_SAMPLER_ARG, value: "0.05" }
            - { name: GOMEMLIMIT_RATIO, value: "0.85" }   # 由程序自己算，不写死 GOMEMLIMIT
            - name: POD_NAME
              valueFrom: { fieldRef: { fieldPath: metadata.name } }
          envFrom:
            - configMapRef: { name: agent-api-config }
          volumeMounts:
            - { name: secrets, mountPath: /var/run/secrets/agent, readOnly: true }
          resources:
            requests: { cpu: "500m", memory: "1Gi" }
            limits:   { cpu: "2",    memory: "2Gi" }
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: { drop: ["ALL"] }
          startupProbe:                 # 管"启动慢"：成功后 liveness/readiness 才开始计时
            httpGet: { path: /healthz/startup, port: http }
            periodSeconds: 2
            failureThreshold: 30        # 最多容忍 60s 启动（连不上 DB/向量库时合理）
          readinessProbe:               # 管"能不能接流量"：失败 = 从 Service Endpoints 摘掉，不重启
            httpGet: { path: /healthz/ready, port: http }
            periodSeconds: 5
            timeoutSeconds: 2
            failureThreshold: 2
          livenessProbe:                # 管"要不要重启我"：只查进程自身死活
            httpGet: { path: /healthz/live, port: http }
            periodSeconds: 10
            timeoutSeconds: 2
            failureThreshold: 3
          lifecycle:
            preStop:                    # distroless 没有 sh，所以用 HTTP 钩子而不是 exec sleep
              httpGet: { path: /internal/drain, port: http }
      volumes:
        - name: secrets
          secret:
            secretName: agent-api-secret
            defaultMode: 0400
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: agent-api
spec:
  minAvailable: 75%               # 节点排空/升级时最多走 1/4
  selector:
    matchLabels: { app: agent-api }
```

> ⚠️ **`preStop: exec: ["/bin/sh","-c","sleep 10"]` 在 distroless 里必然失败**（hook 报 `exec: "/bin/sh": stat no such file or directory`），而且失败后 kubelet 会**立刻继续**发 SIGTERM——你以为是「等 10 秒摘流量」，实际上是「一秒都没等」。三个正确替代：
> 1. `preStop.httpGet` 打一个 `/internal/drain`（本章采用，需要 K8s 支持 lifecycle httpGet，1.19 之后都支持）；
> 2. `exec` 直接调自己的二进制：`["/server", "-prestop-sleep=10s"]`；
> 3. 不用 preStop，把「摘流量延迟」写进程序：收到 SIGTERM 后先睡 8 秒 `setDraining(true)`，再 `Shutdown`。

---

## 52.6 探针语义与优雅关停：一条必须跑完的接力

### 52.6.1 三种探针到底各管什么

```
         ┌─────────────── Pod 生命周期 ───────────────┐
t=0s  ┌──┴──┐
      │启动 │  startupProbe 每 2s 打一次：失败 → 继续等（不重启）
      │中   │ 成功 → 关掉 startupProbe，启用下面两个探针
      └──┬──┘
t=8s     ▼ startupProbe 成功
      ┌──────────────────────────────────────────────┐
      │ 运行中                                        │
      │  readinessProbe(每5s) 失败 → 从 Endpoints 摘掉 │  ← 不重启，只断流量
      │  livenessProbe(每10s) 失败×3 → kubelet 重启容器 │  ← 会重启！
      └──────────────────────────────────────────────┘
```

| 探针 | 语义 | 失败后果 | 该查什么 | **绝对不该查什么** |
| --- | --- | --- | --- | --- |
| `startupProbe` | 「我还没启动完，别催」 | 超过 `failureThreshold×periodSeconds` 才重启 | 监听端口起来了、配置加载完、migration 完成 | 外部依赖的细粒度健康 |
| `livenessProbe` | 「我还活着，别管我」 | **重启容器** | 进程没死锁、`/healthz/live` 只返回 200 | ❌ 数据库/Redis/LLM API（依赖抖 → 全量重启 → 雪崩） |
| `readinessProbe` | 「我能接流量了」 | 从 Service Endpoints 摘掉，**不重启** | 依赖可用、连接池预热完、未在 draining | ❌ 查询类接口（有成本、有延迟） |

### 52.6.2 常见误配清单（每条都见过血）

| 误配 | 后果 | 正确做法 |
| --- | --- | --- |
| liveness 里查 DB / 下游 | 依赖抖动 → 全部 Pod 反复重启 → 雪崩 | liveness 只做进程自检 |
| readiness 里跑重查询 | 探针自己变成负载 | readiness 只查内存态标志位 + 轻量 ping（带 200ms 超时） |
| 用 `initialDelaySeconds: 60` 代替 startupProbe | 启动慢的版本在 60s 内被 liveness 杀掉，越慢越死 | 用 `startupProbe`，`failureThreshold` 给足预算 |
| `terminationGracePeriodSeconds` 用默认 30s，而 `Shutdown` 预算也是 30s | 永远在最后一刻被 SIGKILL，连接被 reset | grace = preStop + Shutdown + 余量（20s） |
| 探针 `timeoutSeconds: 1` 而 GC 峰值能到 300ms | 偶发假失败，抖动放大 | timeout ≥ 2s，并让探针端点不做任何分配 |
| 探针端口 = 业务端口，且业务端口在高负载下排队 | 高负载时探针超时 → 被摘流量 → 负载集中到剩余 Pod → 更糟 | 探针走独立端口（8080 vs 9091），或做成零依赖的纯内存 handler |
| readiness 永远返回 200 | 发布时把坏版本当好的用 | 至少检查「依赖客户端已初始化 + 不在 draining」 |

### 52.6.3 一份能抄的 healthz 实现

```go
package health

import (
	"encoding/json"
	"net/http"
	"sync/atomic"
	"time"
)

type Checker struct {
	started   atomic.Bool // 配置加载、依赖客户端构造完成
	draining  atomic.Bool // 收到 SIGTERM / preStop 时置 true
	ready     atomic.Bool // 后台预热（连接池、索引加载）完成
	startedAt time.Time
}

// Drain 是 preStop 钩子的落点：先置 draining（readiness 立刻 503），再阻塞到流量摘完。
func (c *Checker) Drain(dur time.Duration) {
	c.draining.Store(true)
	time.Sleep(dur) // 等 kube-proxy / Ingress 的 Endpoint 变化传播，经验 5~10s
}

func (c *Checker) Mount(mux *http.ServeMux) {
	// liveness：只证明"进程还能响应"，绝不含外部依赖
	mux.HandleFunc("GET /healthz/live", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})

	// startup：启动期状态
	mux.HandleFunc("GET /healthz/startup", func(w http.ResponseWriter, r *http.Request) {
		if !c.started.Load() {
			http.Error(w, "starting", http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
	})

	// readiness：在途 + draining + 预热
	mux.HandleFunc("GET /healthz/ready", func(w http.ResponseWriter, r *http.Request) {
		body := map[string]any{
			"started":  c.started.Load(),
			"warmed":   c.ready.Load(),
			"draining": c.draining.Load(),
			"uptime_s": int(time.Since(c.startedAt).Seconds()),
		}
		w.Header().Set("Content-Type", "application/json")
		if c.draining.Load() || !c.ready.Load() {
			w.WriteHeader(http.StatusServiceUnavailable) // 503 → 从 Endpoints 摘掉
			_ = json.NewEncoder(w).Encode(body)
			return
		}
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(body)
	})

	// preStop 落点（注意：不要暴露到公网 Ingress 路径上）
	mux.HandleFunc("GET /internal/drain", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		go c.Drain(10 * time.Second)
	})
}
```

> 💡 Go 1.22 起 `net/http` 的 `ServeMux` 支持 `"GET /path"` 这种「方法 + 路径」模式，上面那几行才这么干净。详见第 38 章。

### 52.6.4 优雅关停：`SIGTERM → Shutdown` 的接力时序

```
t0   kubectl delete pod / rollout 开始
      │  ① Pod phase → Terminating
      │  ② EndpointSlice 把该 Pod 标 terminating（K8s 1.22+ serving=terminating, terminating=true）
      │     kube-proxy / Ingress / Service Mesh 各自更新规则 —— 这里有 1~10s 传播延迟！
      ├─ ③ kubelet 执行 preStop hook：GET /internal/drain → draining=true，readiness 立刻 503
      │     （同时 sleep 10s，等上面 ② 的传播完成）
t10  ④ preStop 结束 → kubelet 发 SIGTERM
      │     signal.NotifyContext 取消 → ctx.Done()
      │     setDraining(true) → srv.Shutdown(ctx)：
      │        关闭 listener（不再 accept 新连接）
      │        关掉空闲 keep-alive 连接
      │        等在途请求跑完（最多 we 自己设的 30s）
      │     WebSocket 等 hijacked 连接 Shutdown 不管，得自己 RegisterOnShutdown 里关
      │     关连接池 / flush traces / 关 DB
t18  ⑤ 进程 exit 0，Pod 消失
t60 ⑥ 如果到 t0+60s 还没退出 → SIGKILL（数据丢，连接 reset）
```

完整 `main` 骨架（这段可以直接当你所有 Go 服务的模板）：

```go
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	_ "time/tzdata"
)

var (
	version   = "dev"
	commit    = "unknown"
	buildTime = "unknown"
)

func main() {
	if err := run(); err != nil {
		slog.Error("fatal", "err", err)
		os.Exit(1)
	}
}

func run() error {
	// 1) SIGTERM/SIGINT 一转成 ctx，全链路（http、db、llm）自动收到取消
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	cfg, err := config.Load()
	if err != nil {
		return fmt.Errorf("config: %w", err) // 配置错就直接死，别带病运行
	}
	slog.SetDefault(newLogger(cfg.LogLevel))

	tuneRuntime(cfg.MemRatio)

	app, err := newApp(ctx, cfg)
	if err != nil {
		return fmt.Errorf("init: %w", err)
	}

	mux := http.NewServeMux()
	app.health.Mount(mux)
	app.routes(mux)

	srv := &http.Server{
		Addr:              cfg.Addr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second, // 防 Slowloris
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      0,               // SSE/流式响应不能有统一写超时，用 ResponseController 单独控
		IdleTimeout:       90 * time.Second,
		BaseContext:       func(net.Listener) context.Context { return ctx },
	}

	errc := make(chan error, 1)
	go func() {
		slog.Info("http listening", "addr", cfg.Addr, "version", version, "commit", commit, "build", buildTime)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errc <- err
		}
	}()

	// 预热：连接池、向量库客户端、模型元数据。预热完才 open readiness 门
	app.health.started.Store(true)
	if err := app.warmup(ctx); err != nil {
		slog.Warn("warmup failed, serving anyway", "err", err)
	}
	app.health.ready.Store(true)

	select {
	case err := <-errc:
		return fmt.Errorf("serve: %w", err)
	case <-ctx.Done():
	}

	// 2) 关停：先关闸（readiness 503），再排空
	app.health.draining.Store(true)
	slog.Info("shutdown signal received, draining")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	if err := srv.Shutdown(shutdownCtx); err != nil {
		slog.Warn("graceful shutdown incomplete, forcing close", "err", err)
		_ = srv.Close() // 兜底：超时就硬关，总比被 SIGKILL 好
	}
	app.close() // 关 DB/Redis、flush batcher、shutdown tracer provider
	slog.Info("bye")
	return nil
}
```

`app.close()` 里最容易被忘的三件事：

```go
func (a *app) close() {
	if a.tr != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = a.tr.Shutdown(ctx) // OTel：不 Shutdown 最后一批 span 会丢
	}
	_ = a.db.Close()          // database/sql：等在途事务结束再关
	_ = a.redis.Close()
}
```

> ⚠️ **WebSocket / SSE 的关停是另一个坑**：`http.Server.Shutdown` 明确**不等待 hijacked 连接（WebSocket）**。长连接服务要自己维护连接表，在 `srv.RegisterOnShutdown(func(){ ... })` 里给每个连接发 `close frame`。详见第 39 章。

---

## 52.7 发布策略：滚动、蓝绿、金丝雀与自动回滚

| 策略 | 资源成本 | 回滚速度 | 流量控制粒度 | 适合 |
| --- | --- | --- | --- | --- |
| 滚动更新（RollingUpdate） | 低（1~2 个额外 Pod） | 慢（再滚一次，几分钟） | 无（只能靠 `maxUnavailable` 近似） | 绝大多数无状态服务，**默认选它** |
| 蓝绿（Blue-Green） | 高（2 倍） | 极快（切 Service selector） | 全有或全无 | 不能容忍混跑的版本（DB schema 迁移、协议变更） |
| 金丝雀（Canary） | 中（1 个 canary + 原版本） | 快（删 canary） | 按 % / header / 用户 | **Agent 服务强烈推荐**（prompt/模型一变，效果就得小流量验证） |
| 影子流量（Shadow / Mirror） | 中偏高 | 无需回滚 | 复制流量，响应丢弃 | 验证新 prompt/新模型的质量，不影响用户 |

### 52.7.1 金丝雀 + 按指标自动回滚的时序

```
         ┌──────── Ingress / Service ────────┐
用户 ───►│ 95% → stable (v1.7.2, 4 replicas) │
         │  5% → canary (v1.7.3, 1 replica)  │
         └──────────────┬────────────────────┘
                        │ 每 60s 分析一次 Prometheus 查询
                        ▼
     ┌───────────────────────────────────────────────┐
     │ 判据（全部满足才继续放量）：                    │
     │   error_rate < 1%      （canary vs stable）     │
     │   p99_latency < 1.2 × baseline                 │
     │   cost_per_request < 1.1 × baseline（Agent 特有）│
     └──────────────┬────────────────────────────────┘
              ┌─────┴─────┐
        通过  │           │ 失败（1 分钟内连续 2 次）
              ▼           ▼
       5%→25%→50%→100%  自动回滚：canary 权重归 0 + 告警
```

K8s 原生 Deployment 不支持按指标自动回滚，要上 **Argo Rollouts** 或 **Flagger**。Argo Rollouts 的 `AnalysisTemplate` 长这样（片段，字段名以你用的 CRD 版本为准）：

```yaml
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: agent-api-success-rate
spec:
  metrics:
    - name: error-rate
      interval: 60s
      count: 5
      failureLimit: 2
      successCondition: result[0] < 0.01
      provider:
        prometheus:
          address: http://prometheus.monitoring:9090
          query: |
            sum(rate(http_requests_total{job="agent-api",status=~"5.."}[2m]))
            / sum(rate(http_requests_total{job="agent-api"}[2m]))
```

> 💡 **Agent 服务的金丝雀要加一个 Java 时代没有的判据：成本**。模型或 prompt 一换，token 消耗可能涨 30%，但延迟和错误率都看不出问题——账单下周才给你一巴掌。所以 SLO 里除了错误率、延迟，必须有 `cost_per_request` 和 `tokens_per_request`（详见第 47 章）。

### 52.7.2 回滚设计三条军规

1. **数据库迁移必须向后兼容**（expand → migrate → contract）：先加字段、双写、再切读、最后删旧字段。这样「回滚代码」不需要「回滚数据」。
2. **`revisionHistoryLimit` 别设 0**：否则 `kubectl rollout undo` 无版本可回。
3. **回滚演练**：每季度手动 `kubectl rollout undo` 一次到预发，确认真的能 30 秒回退。没演练过的回滚等于没有。

---

## 52.8 弹性伸缩与冷启动

### 52.8.1 HPA 指标怎么选（Go 服务的特殊性）

| 指标类型 | 配置方式 | 对 Go 服务是否好用 | 说明 |
| --- | --- | --- | --- |
| CPU 利用率 | `type: Utilization, averageUtilization: 70` | ⚠️ 一般 | 分母是 `requests.cpu`；Go 的 CPU 曲线「GC 尖峰 + 空闲」不平滑，容易抖；且 IO 密集服务 CPU 上不去 |
| 内存 | `type: AverageValue` | ❌ 别用 | Go 的 RSS 只涨不跌（scavenger 归还慢），扩出去就缩不回来 |
| QPS / 队列深度 | Prometheus Adapter / KEDA | ✅ 最好 | `http_requests_per_second`、`queue_depth`、`inflight_requests` |
| 并发数（in-flight） | KEDA + Prometheus | ✅✅ Agent 首选 | LLM 调用是「少量并发、长耗时」，用 in-flight 最能反映压力 |
| 外部事件（Kafka lag 等） | KEDA `ScaledObject` | ✅ | 批处理、embedding 队列 |

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: agent-api
spec:
  scaleTargetRef: { apiVersion: apps/v1, kind: Deployment, name: agent-api }
  minReplicas: 4
  maxReplicas: 40
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 30
      policies: [{ type: Percent, value: 100, periodSeconds: 30 }]  # 30s 内最多翻倍
    scaleDown:
      stabilizationWindowSeconds: 300   # 缩容观察 5 分钟，别刚扩容就缩回去
      policies: [{ type: Percent, value: 25, periodSeconds: 60 }]
  metrics:
    - type: Pods
      pods:
        metric: { name: inflight_requests }
        target: { type: AverageValue, averageValue: "20" }
```

> 💡 为什么「内存 HPA」是反模式：Go 的 GC 不会立刻把内存还给 OS，`runtime.MemStats.Sys` 上去就不太下来（scavenger 是渐进的）。你按内存扩容，等于按「历史峰值」扩容，永远缩不回来。详见第 15、28 章。

### 52.8.2 冷启动：Go 已经赢了一半，剩下的一半在哪

Go 的冷启动通常是 **几十到几百毫秒**（静态二进制 + 无 JIT），比 JVM 的 10 秒快两个数量级。但 Agent 服务的「业务冷启动」远不止进程启动：

| 阶段 | 典型耗时 | 优化手段 |
| --- | --- | --- |
| 进程启动 | 20~100ms | 已足够快，别乱加 `init()` 里的重活 |
| 配置/密钥加载 | 1~50ms | 本地缓存 + 并行读；Vault 拿不到时用退避而非阻塞 |
| DB / Redis 连接池预热 | 50~500ms | `db.Ping()` + `SetMaxOpenConns` 预建连接；预热失败降级而非退出 |
| 向量库 / 索引加载 | 0.5~10s | 懒加载 + 后台 `warmup`，readiness 卡住直到完成（这正是 startupProbe 的用途） |
| TLS 握手 / DNS | 10~100ms | 长连接 + `Transport` 复用；DNS 缓存（详见第 32 章） |
| 缩到 0 后再拉起（KEDA scale-to-zero） | + 镜像拉取时间 | 镜像小（8MB vs 400MB 在这里差 30 秒）+ 节点镜像预拉取（daemonset pre-puller） |

---

## 52.9 日志：stdout + fluent-bit 多行

### 52.9.1 三条铁律

1. **只写 stdout/stderr，不写文件。** 容器文件系统是临时的，日志落盘 = 日志丢失 + 磁盘写满被驱逐。kubelet 会把 stdout 收进 `/var/log/containers/*.log`（CRI 格式），由 fluent-bit/DaemonSet 采集。
2. **一行一条 JSON。** `slog` 的 `JSONHandler` 天生满足；**panic 栈自己处理**（见下），不要指望采集端做多行。
3. **日志级别和采样要能在线调。** 高 QPS 下 `INFO` 全开一天能烧掉几十 GB；用 `slog.Handler` 包一层采样器，或在网关层开 trace 采样。

> ⚠️ CRI 日志会被 kubelet 按 `containerLogMaxSize`（默认 10Mi）和 `containerLogMaxFiles`（默认 5）轮转。**一次 200KB 的大 JSON 日志会把你挤掉 20 个可用文件位**——超长字段（prompt、工具返回）必须先截断再打。详见第 35 章。

### 52.9.2 panic 栈：应用侧解决，别甩给采集端

```go
// 把所有 goroutine 的 panic 栈压成「一条 JSON」，采集端就完全不需要多行解析
func logPanic(err any) {
	stack := debug.Stack() // 多行字节串
	slog.Error("panic recovered",
		"panic", fmt.Sprint(err),
		"stack", strings.ReplaceAll(string(stack), "\n", "\\n"), // ← 关键：换行转义
		"goroutines", runtime.NumGoroutine(),
	)
}
```

### 52.9.3 fluent-bit 侧（备用的多行兜底）

如果某些第三方库确实往 stderr 吐多行（比如 cgo 库），可以在 fluent-bit 里配多行解析器兜底（`parsers_multiline.conf` 里自带 `docker`、`cri`、`java`、`python` 等，1.8+ 用 `Multiline.parser`，2.x 用 `multiline.parser` / `multiline` filter，字段名随版本有变化，**以你集群里的版本为准**）：

```ini
[INPUT]
    Name              tail
    Path              /var/log/containers/*agent-api*.log
    multiline.parser  docker, cri
    Tag               kube.*
    DB                /var/log/flb_kube.db
    Mem_Buf_Limit     32MB
    Skip_Long_Lines   On

[FILTER]
    Name              kubernetes
    Match             kube.*
    Kube_URL          https://kubernetes.default.svc:443
    Merge_Log         On
    Keep_Log          Off
    K8S-Logging.Parser On

[OUTPUT]
    Name              loki
    Match             kube.*
    Host              loki.monitoring.svc
    Labels            job=agent-api, namespace=$kubernetes['namespace_name'], pod=$kubernetes['pod_name']
```

---

## 52.10 SLO / 错误预算 / 告警降噪

### 52.10.1 从 SLI 到 SLO 到错误预算

```
SLI（指标）      = 好事件 / 总事件
                   例："非 5xx 且 P99 < 3s 的请求 / 全部请求"

SLO（目标）      = 99.9% / 30 天滚动窗口
错误预算（预算）  = 1 - SLO = 0.1%  →  30 天可失败 43.2 分钟

燃尽速率         = 当前错误率 / 预算允许错误率
                   燃尽率 1  = 刚好在 30 天用完预算
                   燃尽率 14.4 = 1 小时用完 2% 预算 → 该叫人起床了
```

### 52.10.2 Agent 服务该定义哪几个 SLI

| SLI | 表达式（示意） | 目标 | 为什么重要 |
| --- | --- | --- | --- |
| 可用性 | `1 - sum(rate(http_requests_total{status=~"5.."}[5m])) / sum(rate(http_requests_total[5m]))` | 99.9% | 基础 |
| 首 token 延迟（TTFT） | `histogram_quantile(0.95, rate(llm_ttft_seconds_bucket[5m]))` | P95 < 1.5s | **用户体感就这一条** |
| 完整回答时延 | `histogram_quantile(0.95, ...llm_e2e_seconds_bucket)` | P95 < 20s | 长任务 SLO 和短任务分开定 |
| 工具成功率 | `sum(rate(tool_calls_total{result="ok"})) / sum(rate(tool_calls_total))` | 99% | 工具抖动比模型抖动更常见 |
| 单位成本 | `sum(rate(llm_cost_usd_total[1h])) / sum(rate(requests_total[1h]))` | < $0.02/req | 只有预算约束，不是 SLO 但是硬约束 |

### 52.10.3 多窗口多燃尽率告警（照抄 Google SRE Workbook）

| 严重级别 | 长窗口 | 燃尽率 | 短窗口 | 短窗口燃尽率 | 含义 |
| --- | --- | --- | --- | --- | --- |
| Page（叫醒） | 1h | 14.4 | 5m | 14.4 | 1 小时烧掉 2% 月预算 |
| Page（叫醒） | 6h | 6 | 30m | 6 | 6 小时烧掉 5% |
| Ticket（工单） | 3d | 1 | 6h | 1 | 3 天烧掉 10% |
| Ticket（工单） | 3d | 1 | — | — | 慢速持续劣化 |

```promql
# 1h/5m 双窗口燃尽率告警（error budget 剩余 0.1% 时，1h 燃尽率 14.4 表示"1 小时就把 2% 月预算烧完"）
(
  sum(rate(http_requests_total{job="agent-api",status=~"5.."}[1h]))
  / sum(rate(http_requests_total{job="agent-api"}[1h]))
) / 0.001 > 14.4
and
(
  sum(rate(http_requests_total{job="agent-api",status=~"5.."}[5m]))
  / sum(rate(http_requests_total{job="agent-api"}[5m]))
) / 0.001 > 14.4
```

### 52.10.4 告警降噪六条（一条能省半个值班人力）

1. **只对 SLO 燃尽率和「即将发生的不可逆损失」告警**，其余全进工单/看板。
2. **一个故障只发一条告警**（`alertname` + `service` 聚合，用 `group_by` + `group_interval`）。
3. **加 `for: 5m`**，并给 `severity` 分级；没有 `for` 的告警等于没有。
4. **抑制（inhibit）**：`KubePodCrashLooping` 触发时抑制该 Pod 的所有其他告警；上游挂了抑制下游。
5. **静默必须带过期时间和 issue 号**，禁止永久静默。
6. **每次误报都要删规则或调阈值**——告警规则是需要维护的代码，放 Git 里 review。

---

## 52.11 追踪采样与成本

### 52.11.1 采样策略对号入座

| 策略 | 做法 | 优点 | 坑 |
| --- | --- | --- | --- |
| 恒定比例（head-based） | `parentbased_traceidratio` + 0.01~0.1 | 实现简单、开销可控 | 慢请求、错误请求大概率采不到 |
| 尾部采样（tail-based） | OTel Collector `tail_sampling` processor | **能保证「所有错误 + 所有慢请求」都被采到** | Collector 要缓存整条 trace，内存和延迟成本高 |
| 只在边界采样 | 入口服务决定，下游继承 | 一条 trace 要么全有要么全无 | 必须严格传播 `traceparent`，否则链路断裂 |

```yaml
# 入口 Pod 的环境变量（otel-go SDK 读这组变量；版本较老就自己 new sampler，别赌）
env:
  - { name: OTEL_TRACES_SAMPLER,     value: parentbased_traceidratio }
  - { name: OTEL_TRACES_SAMPLER_ARG, value: "0.05" }   # 5%
```

### 52.11.2 成本账：span 是要花钱的

- 一个 Agent 请求的 span 数量可能是 **50~200 个**（每次 LLM 调用、每个工具、每次检索、每条消息）。
- 存储成本 ≈ `QPS × spans/req × 采样率 × 平均 span 大小`。10 QPS × 100 span × 5% × 1KB ≈ **4.3 GB/天**。
- 三个省钱手段：① 尾部采样兜住错误和长尾，其余按 1%；② **不记录大字段**（prompt / 工具返回只记长度和 hash，走日志关联）；③ 短链路的内部函数不建 span（span 本身有 CPU 和内存开销）。

> 💡 语义约定用起来：`gen_ai.*`（新的 GenAI semconv）而不是自己拍脑袋的 `llm.model`。字段名随 semconv 版本演进（`gen_ai.system` / `gen_ai.request.model` / `gen_ai.usage.input_tokens` 等），**升级时要 diff 一遍 semconv 版本**。详见第 35 章。

---

## 52.12 故障演练：把事故提前到工作日的下午

| 演练类型 | 手段 | 该验证什么 | 通过标准 |
| --- | --- | --- | --- |
| 依赖超时注入 | 让 LLM API / DB 延迟 5s | 超时是否生效、是否降级、有没有 goroutine 堆积 | 错误率上升但**没有**雪崩；P99 有界 |
| 依赖故障 | 杀掉 Redis / 让向量库 500 | 熔断是否打开、降级模型是否接管 | 请求降级返回，可用性不掉到 SLO 以下 |
| 实例 kill | 随机删 Pod / `stress-ng` 打满 CPU | PDB 是否生效、重启是否有 5xx | 5xx < 0.1%，恢复 < 60s |
| 网络分区 | chaos-mesh `NetworkChaos` partition | 重试与幂等 | 无重复副作用（幂等键生效） |
| 节点排空 | `kubectl drain` | grace 是否足够、PDB 是否阻挡 | 排空期间无 5xx |
| 流量突增 | 3~10 倍压测 | HPA 反应时间、连接池上限、限流是否先于崩溃生效 | 限流拒绝而非超时堆积 |
| 配置错误 | 故意下发坏配置 | fail fast + 不半更新 | 进程拒绝启动或保留旧配置，绝不带病运行 |

chaos-mesh 的超时注入（片段）：

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: llm-api-latency
spec:
  action: delay
  mode: one
  selector:
    namespaces: [prod]
    labelSelectors: { app: agent-api }
  delay: { latency: "3s", jitter: "500ms" }
  direction: to
  target:
    mode: all
    selector:
      namespaces: [prod]
      labelSelectors: { app: llm-gateway }
  duration: "10m"
```

**游戏日（Game Day）纪律**：先在预发演练 → 生产只演练「已验证过」的剧本 → 必须有回滚按钮和观察人 → 演练结束输出「发现的问题 + 修复 owner + 截止日期」，否则等于没练。

---

## 52.13 runbook 与事故复盘

### 52.13.1 runbook 模板（每个告警必须链一个）

```markdown
## 告警：AgentApi 5xx 燃尽率 > 14.4（1h/5m）

**影响**：用户对话失败；错误预算 1 小时内消耗 2%。

**判断（2 分钟内）**
1. `kubectl -n prod get pods -l app=agent-api` → 副本数与 Ready 数
2. `kubectl -n prod top pods -l app=agent-api` → 是否 OOM/CPU 打满
3. Grafana「Agent / 依赖」看板 → LLM API 错误率、DB 连接数、Redis 命中率
4. 最近 30 分钟是否有发布：`kubectl rollout history deploy/agent-api`

**处置**
- 若是新版本引入 → `kubectl rollout undo deploy/agent-api`（预计 40s）
- 若是 LLM 供应商故障 → 打开降级开关：`kubectl -n prod set env deploy/agent-api DEGRADE_MODE=fallback-model`
- 若是 DB 连接耗尽 → 临时 `kubectl scale --replicas=2` 降低压力，同时查慢查询
- 若是单节点问题 → `kubectl drain <node>`（PDB 会保护剩余副本）

**验证**：5xx 率 5 分钟内回到 < 0.1%，燃尽率回落。

**升级**：15 分钟未缓解 → 找 @oncall-backend；30 分钟 → @arch。

**事后**：24 小时内写复盘（见模板）。
```

好的 runbook 有三个特征：**能在 2 分钟内判断方向**、**步骤可以直接复制粘贴**、**写了「如果没用怎么办」**。

### 52.13.2 无指责复盘模板

| 部分 | 要回答的问题 |
| --- | --- |
| 时间线 | 从「第一个异常信号」到「恢复」的每一分钟做了什么（用日志和 trace 对齐，不靠记忆） |
| 影响 | 多少用户、多少请求失败、烧掉多少错误预算、损失多少成本 |
| 触发因素 | 直接原因（某次发布）+ 潜在原因（探针误配、没有熔断） |
| 5 Whys | 一直问到「为什么我们的流程允许这件事发生」，而不是「谁按了那个按钮」 |
| 可行动项 | 每条都有 owner + 截止日期 + 验证方式；分为「今晚就改」和「下个迭代」 |
| 沉淀 | 是否要新增告警 / runbook / 演练剧本 / 自动化检查 |

> 🎯 **复盘的唯一 KPI 是「同类事故不再发生」**，不是「写得多深刻」。所以行动项里最高优先级的是**自动化拦截**（CI 检查、默认配置、准入 webhook），其次是文档，最次是「加强培训」。

---

## 52.14 上线 Checklist（可打印，逐条打勾）

**编译与镜像**

- [ ] `CGO_ENABLED=0`，`ldd ./server` 显示 `not a dynamic executable`
- [ ] 多阶段构建，运行镜像 ≤ 20MB（distroless/static 或 scratch）
- [ ] `-trimpath` + `-ldflags "-X main.version=..."`，`./server -version` 能打出版本
- [ ] `import _ "time/tzdata"`，或镜像内有 zoneinfo（`time.LoadLocation` 实测过）
- [ ] HTTPS 出口可用（CA bundle 存在），内部 CA 已注入
- [ ] `USER nonroot`，`securityContext.runAsUser` 与之一致
- [ ] `.dockerignore` 排除 `.git` / `docs` / 大文件

**配置与密钥**

- [ ] 所有配置在 `main` 里一次读完并 `Validate()`，错误信息能指出具体 key
- [ ] 密钥来自挂载文件或密钥系统，**不在环境变量里**
- [ ] 缺关键配置时进程拒绝启动（fail fast），不是带着默认值跑
- [ ] 配置变更流程明确：重启 or 热更新（不要两者混着来）

**资源与 runtime**

- [ ] `requests.cpu` / `requests.memory` 有值（否则 HPA 和调度都没依据）
- [ ] `limits.memory ≥ GOMEMLIMIT / 0.85`，并压测验证过 working set
- [ ] `GOMAXPROCS ≈ CPU limit`（Go 1.25 默认；1.24 及以前用 automaxprocs）
- [ ] 启动日志里有 `gomaxprocs` / `gomemlimit` / `cgroup_limit` 三个字段
- [ ] `GOGC` 保持默认 100 或按压测调过（禁止无脑 `GOGC=20`）

**探针与关停**

- [ ] liveness **不查**任何外部依赖
- [ ] readiness 在 draining / 未预热时返回 503
- [ ] `startupProbe` 覆盖最慢启动路径，`failureThreshold × periodSeconds` 足够
- [ ] `terminationGracePeriodSeconds ≥ preStop + Shutdown 预算 + 20s`
- [ ] preStop 在 distroless 里可用（httpGet 或调自己二进制，不是 `/bin/sh`）
- [ ] 收到 SIGTERM 后：置 draining → `srv.Shutdown(ctx)` → 关池 → flush trace → exit 0
- [ ] WebSocket / SSE 长连接在 `RegisterOnShutdown` 里主动关闭（如有）

**发布与弹性**

- [ ] `maxUnavailable: 0` + `minReadySeconds` + PDB
- [ ] 数据库迁移向后兼容（先 expand 后 contract），回滚不需要改数据
- [ ] 金丝雀/蓝绿有明确判据（错误率 + 延迟 + **每请求成本**）
- [ ] `kubectl rollout undo` 在预发演练过
- [ ] HPA 用 in-flight / QPS，不用内存；`scaleDown.stabilizationWindowSeconds ≥ 300`

**可观测与值班**

- [ ] stdout JSON 日志，字段含 `trace_id` / `request_id` / `tenant_id`
- [ ] 大字段（prompt / 工具结果）截断后再打日志
- [ ] 有 SLO（可用性 + TTFT + 工具成功率），有燃尽率告警
- [ ] 每个 Page 级告警都链了 runbook
- [ ] trace 采样率已按成本核算过（含尾部采样策略）
- [ ] `/metrics` 端口不被 Ingress 暴露

---

## 52.15 踩坑清单（CR / 上线评审可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| `OOMKilled` exit 137，但 heap 远小于 limit | `GOGC=100` 让堆翻倍才回收，瞬时尖峰顶穿 cgroup | 设 `GOMEMLIMIT ≈ 0.85 × limit`，并压测校准 | 52.5.3 |
| CPU 利用率 20% 却一直 P99 抖动 | `GOMAXPROCS=NumCPU`（宿主机核数）远超 quota，CFS throttle | Go 1.25 默认容器感知；老版本 `automaxprocs` | 52.5.2 |
| 发布时 40 秒 5xx，日志 `connection reset` | 收到 SIGTERM 直接退出，Ingress 还在转发 | preStop 延迟摘流量 + `srv.Shutdown` | 52.6.4 |
| preStop `exec sleep` 报 `no such file` | distroless 没有 `/bin/sh` | 用 `httpGet` 钩子或调自己二进制 | 52.5.4 |
| 依赖抖一下，全部 Pod 重启 | liveness 查了 DB/下游 | liveness 只做进程自检 | 52.6.2 |
| 慢启动版本永远起不来 | 用 `initialDelaySeconds` 代替 `startupProbe` | 上 `startupProbe`，预算给足 | 52.6.2 |
| `unknown time zone Asia/Shanghai` | 镜像里没有 zoneinfo | `import _ "time/tzdata"` | 52.3.3 |
| `x509: certificate signed by unknown authority` | scratch 里没有 CA | 用 distroless/static 或拷 CA bundle | 52.3.3 |
| `kubectl exec` 进不去 | distroless 没 shell | `kubectl debug -it --image=busybox` | 52.3.4 |
| 日志缺一半，只有最后几行 | kubelet 轮转把大日志挤掉 | 截断大字段；调大 `containerLogMaxSize` | 52.9.1 |
| 缩容后内存不降、扩出去缩不回来 | 用内存做 HPA 指标 | 用 in-flight / QPS | 52.8.1 |
| 每次改配置都要重新打包 | 配置被编进二进制 / 用了 `go:embed` 的可变内容 | 配置走 env/ConfigMap，重启用滚动 | 52.4.4 |
| Secret 出现在 `kubectl describe` 里 | 密钥放在 env | 挂成文件，`*_FILE` 约定 | 52.4.3 |
| 回滚后发现数据不兼容 | 迁移非向后兼容 | expand→migrate→contract | 52.7.2 |
| Trace 账单暴涨 | 采样 100% + 大字段入 span | 尾部采样 + 只记长度/hash | 52.11.2 |

---

## 52.16 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| `CGO_ENABLED=0` | 静态二进制、纯 Go resolver，才能塞进 scratch/distroless |
| `-trimpath -ldflags "-s -w -X main.version="` | 可复现 + 注入版本，体积再小 25% |
| distroless/static | Go 服务默认基础镜像；**没有 shell**，preStop 不能用 `sh` |
| `time/tzdata` | 一行 blank import 消灭所有时区问题 |
| 三个账本 | 内存（limit ≥ GOMEMLIMIT/0.85）、CPU（limit ≈ GOMAXPROCS）、时间（grace ≥ preStop+Shutdown+20s） |
| `GOMEMLIMIT` | 软限制、换 CPU 保内存；必须和 cgroup 硬墙一起设 |
| Go 1.25 GOMAXPROCS | cgroup 感知，默认就对了；老版本用 automaxprocs |
| 探针分工 | startup 管启动慢、readiness 管摘流量、liveness 只证明进程活着 |
| 优雅关停接力 | 摘流量（preStop 延迟）→ SIGTERM → Shutdown → 关池 → flush → exit 0 |
| 告警与复盘 | 只对 SLO 燃尽率告警、一个故障一条、每条链 runbook；复盘 KPI 是「同类事故不再发生」 |

## 52.17 自检题

**1. 你的 Go 服务 `limits.memory = 2Gi`，`GOMEMLIMIT` 设成多少？为什么不能设成 `2Gi`？**

<details><summary>答案要点</summary>

- 建议 `≈ 0.85 × limit ≈ 1700MiB`。
- 因为 `GOMEMLIMIT` 只约束 **Go runtime 管理的堆**，而进程的 RSS 还包括：goroutine 栈、runtime 元数据（mcache/span/bitmap）、mmap 后未归还的 arena、cgo 内存、TLS/zstd 缓冲等。
- 设成等于 limit，等于把「非堆部分」挤到 cgroup 墙外 → `OOMKilled`（exit 137），而且 GC 会更早进入高频状态，白烧 CPU。
- 正确做法：先按 0.85 设，再用 `container_memory_working_set_bytes` vs `go_memstats_heap_alloc_bytes` 的差值校准比例。

</details>

**2. 为什么 liveness 探针里绝对不能查数据库？如果确实想反映「依赖不可用」，应该怎么做？**

<details><summary>答案要点</summary>

- liveness 失败 = **重启容器**。依赖抖动时，所有 Pod 同时失败 → 全部重启 → 冷启动又打满依赖 → 雪崩（故障放大）。
- 依赖不可用属于「我暂时不能服务」，语义上是 **readiness 失败（摘流量）**，不是「我死了」。
- 正确姿势：readiness 里做带短超时（≤200ms）的依赖 ping 或读内存态标志位；配熔断/降级（第 47 章），让请求降级返回而不是让 Pod 重启；真需要「依赖长期不可用就重启」时，用**单个** Pod 的手动操作或专门的自愈控制器，而不是 liveness。

</details>

**3. 发布时出现 30 秒 5xx，你怎么用「接力」模型定位是哪一棒掉了？**

<details><summary>答案要点</summary>

- 接力顺序：① EndpointSlice 标记 terminating → ② kube-proxy/Ingress 规则传播（1~10s）→ ③ preStop（延迟/sleep）→ ④ SIGTERM → ⑤ `srv.Shutdown` 等在途请求 → ⑥ exit 0；总时长必须 < `terminationGracePeriodSeconds`。
- 定位方法：看 Pod 日志里「收到 SIGTERM 到 exit」的时间差（第 ④⑤ 棒），看 `terminationGracePeriodSeconds` 是否够（第 ⑥ 棒），看有没有 preStop（第 ③ 棒），看 5xx 集中在发布后前 10 秒（第 ② 棒传播窗口）。
- 常见三种错：没 preStop（流量还在打，进程已退出）、`Shutdown` 用 `context.Background()` 没超时（卡到被 SIGKILL）、grace 30s 但 shutdown 预算也 30s（永远最后一刻被打断）。
- 另外别忘了 hijacked 连接：WebSocket 不在 `Shutdown` 的等待范围里，需要 `RegisterOnShutdown` 主动关闭。

</details>

## 52.18 延伸阅读

- Go 官方：`runtime/debug` 包文档（`SetMemoryLimit`）、`runtime` 包 `GOMAXPROCS` 说明、`time/tzdata` 包文档
- Go 1.25 Release Notes 中 「runtime: container-aware GOMAXPROCS」一节（`containermaxprocs` / `updatemaxprocs` GODEBUG）
- Kubernetes 官方文档：*Configure Liveness, Readiness and Startup Probes*、*Pod Lifecycle: Container termination*、*Resource Management for Pods and Containers*
- Google SRE Workbook：第 2 章 *Implementing SLOs*、第 5 章 *Alerting on SLOs*（多窗口多燃尽率的原始出处）
- `gcr.io/distroless` 仓库 README（各镜像包含什么、`nonroot` 的 uid）
- Argo Rollouts 文档：*Analysis & Progressive Delivery*；chaos-mesh 文档：`NetworkChaos` / `StressChaos`
- `net/http` 源码：`server.go` 的 `Shutdown` / `RegisterOnShutdown` / `closeIdleConns`（关停行为的权威定义）

---

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 51 章 读源码方法论 + 框架设计模式提炼](51-read-source-patterns.md)；同属上线视角的 [第 32 章 高并发高性能实战](32-high-concurrency.md)
- 相关：[第 2 章 工程骨架](2-toolchain-layout.md)（多阶段构建入门）、[第 28 章 GC 与调参](28-gc.md)（`GOGC`/`GOMEMLIMIT` 原理）、[第 35 章 可观测性](35-observability.md)（slog/OTel/Prometheus）、[第 38 章 net/http 深潜](38-net-http.md)（超时四件套与 `Shutdown`）、[第 39 章 实时通信](39-realtime-sse-websocket.md)（长连接关停）、[第 47 章 Agent 生产化](47-agent-productionization.md)（限流/熔断/成本）
- 下一章：[第 53 章 综合项目：从零构建生产级 Agent 框架](53-build-agent-framework.md)
