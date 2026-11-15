# 29. 沙盒与执行隔离

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 运行时、安全、合规与防御 ｜ **编号**：29 / 52
> **本章定位**：这一章讲的是「你亲手把执行权交给了一个会被骗的模型」之后必须做的那件事。它不讲容器怎么用，讲**四档隔离的取舍、调用契约、以及为什么「默认 deny 的网络出口」比「更强的隔离」更重要**。
> **读法**：先看 §29.3 的隔离等级表和 §29.8 的成本算式，这两节决定你的选型。§29.6 的四项配额（文件系统/网络/资源/超时）是可以直接抄成配置的。

---

## 29.1 引子：一次 `while True: pass`，和一次内网扫描

Aurora 有两类 Agent 会执行代码：**编码 Agent**（跑用户给的脚本、跑单元测试）和**数据分析 Agent**（跑 pandas 处理上传的 CSV）。

事故一，2026 年 5 月：

```
14:22  数据分析 Agent 执行用户上传的 Python：
       import pandas as pd; df = pd.read_csv("data.csv"); ...
       但 CSV 里有个字段是注入的代码，Agent「顺手」执行了它
       → 代码里是 while True: pass，起了 8 个进程
14:24  该节点 64 核全部 100%，同节点上还有 11 个租户的 API Pod
14:26  平台 P99 从 1.5s 涨到 22s，告警爆了 40 条
14:31  K8s 节点 NotReady，上面的 Pod 被驱逐 → 12 个租户的会话中断
```

事故二，2026 年 7 月，更严重：

```
用户上传的脚本里有一行：
  socket.connect(("10.0.3.17", 6379)); ...
沙盒容器用的默认 Docker 网络 → 能访问内网
→ 扫到内网 Redis（无密码），dump 了 2.1GB（含 3 个租户的会话缓存）
→ 又访问 169.254.169.254/latest/meta-data/ 拿到了节点 IAM 角色凭证
```

两次事故的根因完全不同：事故一是**资源配额缺失**（没有 cgroup CPU 上限、没有 PID 上限），事故二是**网络出口没管**（默认 allow）。

> 🎯 **本章的核心命题**：
> **沙盒的安全性 = 隔离强度 × 配额完整性 × 出口收敛性。三者是乘法，任何一项为 0，整体为 0。**
> 团队最常犯的错是把 90% 的精力花在「选更强的隔离技术」上（Firecracker 还是 gVisor），却忘了配 `pids.max` 和 `default deny egress`——而后者才是 80% 真实事故的直接原因。

---

## 29.2 沙盒要防的四类东西

| 风险 | 具体形态 | 靠什么防 | 只靠「容器」够吗 |
| --- | --- | --- | --- |
| **资源滥用** | 死循环、fork bomb、内存炸弹、磁盘写满、超长输出 | cgroup v2（`cpu.max`/`memory.max`/`pids.max`/`io.max`）+ 输出截断 + 超时 kill | ✅ 够（但**必须真的配上**） |
| **数据外泄** | 读宿主/其他租户的文件、读环境变量、读云元数据、把数据 POST 出去 | 只读 rootfs + 独立 mount namespace + 无凭证挂载 + **egress 白名单 + 元数据 IP 拦截** | ⚠️ 容器默认共享内核，逃逸面存在 |
| **横向移动** | 访问内网服务、DNS 隧道、SSRF 打内部 API | 独立网络 namespace + **默认 deny 的出向代理** + DNS 也走代理 | ⚠️ 需要网络策略，容器本身不管 |
| **逃逸** | 内核漏洞提权、`/proc` 泄露、`CAP_SYS_ADMIN` 滥用、容器运行时漏洞 | 强隔离（gVisor/Kata/Firecracker）+ 丢弃全部 capability + seccomp | ❌ 不够，共享内核就是共享逃逸面 |

**判断你的沙盒属于哪一档，只需要问三个问题**：

```
Q1: 里面的进程能直接调 Linux syscall 吗？（共享内核 → 是）
Q2: 它的出向网络是白名单还是黑名单？（默认 allow → 你已经输了）
Q3: 它有没有 pids.max？（没有 → 一个 fork bomb 干掉节点）
```

> 💡 Q1 回答「逃逸风险」，Q2/Q3 回答「事故概率」。**从统计上看，Q2/Q3 引发的事故是 Q1 的 20 倍以上**——因为逃逸需要 0day，而 fork bomb 只需要一行代码。

---

## 29.3 四档隔离：一张必须自己算的表

「进程限制 → 容器 → gVisor/Kata → Firecracker/wasm」这四档，本质是**用冷启动时间和成本，换内核攻击面**。

### 29.3.1 隔离等级表

| 等级 | 技术 | 内核共享 | 隔离边界 | 冷启动（参考） | 单机密度（64C/256G） | 兼容性 | 逃逸难度 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **L0** | `rlimit` + `seccomp` + `namespace` + Landlock（`nsjail`/`bubblewrap` 这类） | ✅ 共享宿主内核 | 进程级 | **< 1ms** | 数千进程 | 高（同宿主发行版） | 🔴 低，任何内核提权都直达宿主 |
| **L1** | 容器（runc + cgroup v2 + user namespace + seccomp profile + 只读 rootfs） | ✅ 共享宿主内核 | namespace 级 | **100~500ms**（镜像已预热） | 300~1,000 | 极高（OCI 镜像生态） | 🟡 中，需内核 0day + 容器配置缺陷 |
| **L2a** | **gVisor**（`runsc`，用户态内核 Sentry 拦截 syscall） | ❌ 不共享（syscall 走用户态实现） | syscall 级 | **300ms~1.5s** | 100~300 | 中（部分 syscall/ioctl 不支持） | 🟢 高 |
| **L2b** | **Kata Containers**（每 Pod 一个轻量 VM，QEMU/Cloud Hypervisor） | ❌ 不共享（硬件虚拟化） | VM 级 | **500ms~2s** | 80~200 | 高（OCI 兼容，真内核） | 🟢 高 |
| **L3a** | **Firecracker**（microVM，AWS 开源） | ❌ 不共享 | VM 级（最小设备模型） | **60~150ms** 启动；**snapshot restore 5~30ms** | 500~1,000+（每 VM 内存开销约 5MiB） | 中（需自带 rootfs/内核，无 GPU 直通） | 🟢 很高（攻击面最小） |
| **L3b** | **WASM**（`wasmtime` / `wazero`） | ❌ 不共享（语言级沙箱） | 指令级 | **1~10ms**（实例化） | 数万实例 | 🔴 低（无 fork、无原生扩展、需 WASI，Python/Node 生态基本跑不了真项目） | 🟢 最高 |

> ⚠️ **冷启动数字必须以你自己的环境实测为准**，受镜像大小、内核版本、存储 IO、是否预热影响极大。上表只给量级，用于做取舍判断。
> 另外关于 gVisor 的性能：**syscall 密集型的负载会有明显开销**（常见说法是数倍，具体倍数随负载类型差异巨大，必须以你的 workload 实测）。CPU 密集的纯计算负载开销小得多。

### 29.3.2 怎么选：按「代码来源」而不是按「安全等级」

| 代码来源 | 建议等级 | 理由 |
| --- | --- | --- |
| 平台内置的、你写的工具（如 CSV 解析） | L1 容器 | 代码可信，只需要资源隔离，不需要防逃逸 |
| 租户配置的低代码 DSL（第 49 章） | L1 + 严格 egress | 配置可能被注入，但表达力有限 |
| **模型生成的代码**（编码 Agent） | **L2a/L3a** | 不可信、可能被注入、可能恶意。这是主战场 |
| **用户上传的代码/脚本** | **L3a** | 完全不可信，等同公网输入 |
| 第三方 MCP server 提供的执行能力 | 视同 L3a | 供应链不可控（第 32 章） |
| 只需要「算一下」（数学、字符串处理） | L3b WASM | 不需要真环境，10ms 起，密度高 100 倍 |

> 🎯 **Aurora 的实际选择**：默认 **L3a（Firecracker）+ snapshot 预热**，因为编码/数据类 Agent 是主要场景；低代码 DSL 和内置工具用 L1；纯计算型工具用 L3b。**没有用 gVisor**，原因是：既要真 Linux 环境（pip/npm 装包），又要在 100ms 级启动，Firecracker 的 snapshot restore 是唯一同时满足的选项。

### 29.3.3 一个反直觉的结论

> 💡 **更强的隔离不能替代配额和网络策略。**
> 一个 Firecracker microVM 如果没有 `pids.max`、没有出向白名单，它照样能 fork bomb 打满宿主机（microVM 里 64 核还是 64 核），照样能通过 NAT 访问内网。
> 反过来，一个配置良好的 L1 容器（只读 rootfs + 全 drop capability + seccomp + pids.max=64 + 默认 deny egress）能挡掉 95% 的真实攻击。
> **工程顺序应该是：先做配额和出口（1 周），再升级隔离等级（1 个季度）。做反了，你花了一个季度，事故率只下降 5%。**

---

## 29.4 结构图：控制面 / 数据面分离

```
┌────────────────────────── 控制面（可信） ──────────────────────────┐
│                                                                    │
│  Agent 运行时 ──▶ 沙盒调度器（Scheduler）                            │
│                      │  ① 准入：租户配额 / 等级 / 镜像白名单          │
│                      │  ② 选池：warm pool 命中? → bind : create      │
│                      │  ③ 记录：sandbox_id → (tenant, session, node) │
│                      ▼                                             │
│               ┌──────────────┐        ┌─────────────────┐          │
│               │ Sandbox API  │◀──────▶│  会话状态存储     │          │
│               │ (gRPC, mTLS) │        │ (PG: 绑定关系)   │          │
│               └──────┬───────┘        └─────────────────┘          │
└──────────────────────┼─────────────────────────────────────────────┘
                       │ 只走这条通道：exec / upload / collect / kill
┌──────────────────────▼───── 数据面（不可信） ──────────────────────┐
│  沙盒节点（独立节点池，不与 API Pod 混部）                            │
│  ┌──────────────────────────────────────────────────────────┐     │
│  │ Agent-Sandbox-Agent（宿主侧，特权但最小）                   │     │
│  │  ├─ microVM 生命周期（Firecracker API socket）             │     │
│  │  ├─ vsock 通道（唯一进出路径，无宿主网络直接暴露）           │     │
│  │  ├─ 配额应用（cgroup v2 / jailer）                        │     │
│  │  └─ 强制 kill（cgroup.kill + VM 销毁）                    │     │
│  └───────┬──────────────────────────────────────────────────┘     │
│          │                                                         │
│   ┌──────▼──────┐  ┌─────────────┐  ┌──────────────────┐          │
│   │ microVM #1  │  │ microVM #2  │  │ microVM #N       │          │
│   │ 只读 rootfs │  │             │  │  ...             │          │
│   │ /work 512MB │  │             │  │                  │          │
│   │ 无凭证      │  │             │  │                  │          │
│   └──────┬──────┘  └─────────────┘  └──────────────────┘          │
│          │  出向流量只能走一个口                                     │
│   ┌──────▼────────────────────────────────────────────────┐       │
│   │ Egress Proxy（强制代理）                                │       │
│   │  · 白名单域名（pypi.org / registry.npmjs.org / ...）    │       │
│   │  · 拦截 169.254.169.254、10/8、172.16/12、192.168/16    │       │
│   │  · 记录全部出向请求（审计用，第 31 章）                   │       │
│   │  · 带宽/连接数配额                                       │       │
│   └────────────────────────────────────────────────────────┘       │
└────────────────────────────────────────────────────────────────────┘
```

**为什么一定要 vsock 而不是网络**：microVM 如果走正常网络栈，就得给它一个 IP，就得进 VPC，就有被扫到/被扫出的可能。vsock 是宿主与 guest 之间的**专用通道**，guest 侧没有网卡（或者只有一个只能到宿主 proxy 的网卡），从根上消除了「沙盒能连内网」这个可能性。

---

## 29.5 调用契约：把「不可信」写进接口签名

契约的价值在于**让调用方无法绕过约束**：配额、超时、出口策略都必须在请求里显式给出，并且由服务端强制，不能靠调用方自觉。

```go
// sandbox/contract.go
type Level int

const (
    LevelProcess Level = iota // L0
    LevelContainer            // L1
    LevelUserKernel           // L2a gVisor
    LevelVM                   // L3a Firecracker
    LevelWasm                 // L3b
)

type Spec struct {
    TenantID   string   // 隔离与计费的根
    SessionID  string   // 有状态复用的键（§29.7）
    Image      string   // 必须是 digest：sha256:...，禁止 :latest
    Level      Level
    Limits     Limits
    Egress     Egress
    TTL        time.Duration // 沙盒整体寿命（超过强制销毁，不管有没有在跑）
}

type Limits struct {
    CPUMilli     int64         // 2000 = 2 核；硬上限，超过就 throttle 不是 kill
    MemBytes     int64         // 1GiB；超过 OOM kill（在沙盒内）
    DiskBytes    int64         // /work 可写空间，512MiB
    PIDs         int32         // 256；防 fork bomb
    OpenFiles    int32         // 1024
    ExecTimeout  time.Duration // 单次 exec 30s
    TotalCPUTime time.Duration // 会话累计 CPU 时间上限，5min（防「每次 29s 慢慢磨」）
    StdoutBytes  int64         // 1MiB，超过截断并标记 truncated=true
}

type Egress struct {
    Mode      string   // "deny_all" | "allowlist" | "open"（open 仅限内部工具）
    Allow     []string // ["pypi.org:443", "files.pythonhosted.org:443"]
    Bandwidth int64    // bytes/s
}

type ExecRequest struct {
    SandboxID string
    Cmd       []string
    Env       map[string]string // 白名单 key，禁止 PATH/LD_PRELOAD/PYTHONPATH 注入
    Stdin     []byte
    Timeout   time.Duration     // 不得大于 Spec.Limits.ExecTimeout
    Files     []InputFile       // 显式上传，不接受「挂载宿主目录」
}

type ExecResult struct {
    ExitCode  int
    Stdout    []byte
    Stderr    []byte
    Truncated bool
    Artifacts []Artifact   // 只能来自 /work/output，类型与大小受限
    Usage     Usage        // CPU 时间、峰值内存、网络字节 —— 计费与配额结算的依据
    Killed    KillReason   // NONE / TIMEOUT / OOM / PID_LIMIT / POLICY
}
```

**契约里四个不能妥协的点**：

| 约定 | 为什么 | 不做会怎样 |
| --- | --- | --- |
| `Image` 必须是 digest | tag 可变，等于允许「运行时被换镜像」 | 供应链攻击：攻击者 push 一个同名 tag，你的沙盒就跑他的代码 |
| `ExecResult.Usage` 必须返回 | 计费、配额、审计都要 | 你无法回答「这个租户这个月用了多少 CPU」，也无法做容量规划 |
| `Killed` 必须区分原因 | TIMEOUT / OOM / PID_LIMIT 是完全不同的运维信号 | 只看到「失败」，无法定位是配额给少了还是代码写错了 |
| `Env` 白名单 | `LD_PRELOAD` / `PYTHONPATH` 能直接接管进程 | 一个环境变量就等于一个后门 |

---

## 29.6 四项强制约束：文件系统 / 网络 / 配额 / 超时

### 29.6.1 文件系统：三段式，没有第四段

```
/               只读 rootfs（overlay lower = 镜像，upper = 每次新建的空层）
                → 沙盒内 ./configure 之类的写操作会失败，这是设计，不是 bug
/tmp            tmpfs，大小 = min(DiskBytes/4, 128MiB)，随沙盒销毁
/work           可写，大小 = DiskBytes（512MiB），会话内保留（§29.7）
/work/output    唯一允许回传的目录，单文件 ≤ 50MiB，总 ≤ 200MiB，扩展名白名单
```

**四条规则**：

1. **绝不挂载宿主目录**（包括 `/var/run/docker.sock`——这是容器逃逸的第一入口）。
2. **绝不注入凭证**：不挂 `~/.aws`、不设 `AWS_*` 环境变量、不挂 kubeconfig。需要访问云资源的场景，走**短期 STS 令牌 + 出口代理注入**（凭证不进沙盒）。
3. **产物回传要过滤**：只从 `/work/output` 拷，检查 MIME 与扩展名，`.so`/`.pyc`/可执行文件一律拒绝（防「把恶意二进制带出沙盒再被执行」）。
4. **`/proc` 要遮**：`hidepid=2` 或直接不挂宿主 `/proc`，防信息泄露。

### 29.6.2 网络出口：默认 deny，代理是唯一出口

```yaml
# sandbox/egress-policy.yaml
default: deny
dns:
  mode: proxy            # DNS 也走代理，否则 DNS 隧道能外传数据
  resolver: 10.0.0.53
deny_cidrs:              # SSRF 防护的核心清单
  - 169.254.169.254/32   # AWS/GCP/Azure 元数据（最危险的一个）
  - 100.100.100.200/32   # 阿里云元数据
  - 127.0.0.0/8
  - 10.0.0.0/8
  - 172.16.0.0/12
  - 192.168.0.0/16
  - ::1/128
  - fd00::/8
allow_hosts:
  - { host: pypi.org,              ports: [443] }
  - { host: files.pythonhosted.org, ports: [443] }
  - { host: registry.npmjs.org,    ports: [443] }
limits:
  max_conn_per_sandbox: 16
  bandwidth_bps: 10485760        # 10Mbps
  max_bytes_per_exec: 52428800   # 50MiB，防「把数据慢慢传出去」
audit:
  log_all: true                  # 每一次出向请求都进审计流水（第 31 章）
```

> 🩸 **`169.254.169.254` 必须显式 deny**。云上 90% 的「沙盒读到了不该读的东西」都是从这一个 IP 开始的：拿到实例角色凭证 → 调云 API → 横向扩展到整个账号。如果你的出口策略是黑名单制，你已经在赌「你知道所有该拦的地址」——你不可能知道。

### 29.6.3 资源配额：必须有 PID 上限

```yaml
# L1 容器（K8s Pod 的 securityContext + resources）
securityContext:
  runAsNonRoot: true
  runAsUser: 65534
  readOnlyRootFilesystem: true
  allowPrivilegeEscalation: false
  capabilities: { drop: ["ALL"] }
  seccompProfile: { type: RuntimeDefault }
  procMount: Default
resources:
  limits:   { cpu: "2", memory: "1Gi", ephemeral-storage: "512Mi" }
  requests: { cpu: "100m", memory: "128Mi" }
# ⚠️ K8s 原生没有 pids limit，必须靠 kubelet 的 --pod-max-pids 或 cgroup 直配
```

```bash
# L3a Firecracker 侧：jailer 启动前的 cgroup 配置（宿主侧）
# 这些是「沙盒外面的墙」，比沙盒内的一切都可靠
cgroup=/sys/fs/cgroup/sandbox/$SANDBOX_ID
mkdir -p $cgroup
echo "200000 100000" > $cgroup/cpu.max          # 2 核
echo $((1<<30))      > $cgroup/memory.max       # 1GiB
echo 256             > $cgroup/pids.max         # ★ fork bomb 的唯一解
echo "max"           > $cgroup/memory.swap.max  # 禁 swap，否则内存限制形同虚设
```

**为什么 `pids.max` 是必需品而不是可选项**：

```
没有 pids.max 的 fork bomb（Python，6 行）：
  import os
  while True:
      try: os.fork()
      except: pass

后果：宿主机 PID 耗尽 → 连 sshd 都 fork 不出来 → 节点彻底失联
      → 只能硬重启 → 该节点上所有租户的 Run 全部丢失
```

**为什么 `memory.swap.max = 0`**：开了 swap，内存限制就变成「内存 + swap」限制，一个内存炸弹会把整机的 swap 打满，**全机 IO 停顿**，比 OOM 更糟（OOM 只死一个沙盒）。

### 29.6.4 超时与 kill：必须杀整个 cgroup，不能杀进程

```go
// sandbox/kill.go
// 单杀进程是没用的：它会 fork 出子进程，父进程死了子进程还活着
func (s *Runtime) Kill(ctx context.Context, id string, reason KillReason) error {
    // 1. 先给礼貌窗口：SIGTERM 到 cgroup 内所有进程
    s.signalAll(id, syscall.SIGTERM)
    select {
    case <-s.waitEmpty(id):
    case <-time.After(3 * time.Second):
    }

    // 2. cgroup v2 的 kill：原子地杀掉 cgroup 内所有进程，无法 fork 逃逸
    //    （cgroup v1 时代要遍历 /proc 逐个杀，有竞态，会漏）
    if err := os.WriteFile(filepath.Join(s.cgroupPath(id), "cgroup.kill"), []byte("1"), 0o644); err != nil {
        return fmt.Errorf("cgroup.kill: %w", err)
    }

    // 3. VM 级别：直接销毁 microVM（连内核一起没），这是最彻底的一层
    if s.level == LevelVM {
        s.fc.StopVM(id)
    }

    // 4. 清账：记录 Usage 与 KillReason，销毁资源，通知计费（第 27 章）
    s.recordTermination(id, reason)
    return s.releaseAll(id)
}
```

**三种超时必须分层，不能只有一个**：

| 超时 | 值 | 触发后 | 为什么需要 |
| --- | --- | --- | --- |
| `ExecTimeout` | 30s | 杀进程，返回 `Killed=TIMEOUT` | 单次执行不能无限跑 |
| `TotalCPUTime` | 5min | 杀沙盒，会话失效 | 防「每次 29 秒、跑 50 次的慢性滥用」——这条最容易被漏掉 |
| `Spec.TTL` | 30min | 销毁沙盒（不管在不在跑） | 防「僵尸沙盒」占资源；也是计费的上界 |

**再补一条 `MaxExecPerSession`（比如 200 次）**：防「用极短的执行刷次数」，比如每次执行 1ms、跑 10 万次来绕过 `TotalCPUTime`。

---

## 29.7 有状态会话的沙盒复用：省钱的唯一手段，也是隔离的最大风险

### 29.7.1 为什么必须复用

编码 Agent 的一个会话是这样的：

```
第 1 步：pip install pandas numpy  → 12s（装包）
第 2 步：读 CSV，写脚本           → 3s
第 3 步：跑脚本，报错             → 8s
第 4 步：改脚本，重跑             → 26s（下采样，慢）
第 5 步：再改，再跑               → 31s
```

如果每次 exec 都新建沙盒：5 × (冷启动 150ms + 装包 12s) ≈ 61 秒纯开销，而且 `pip install` 每次都要花钱花流量。**必须复用。**

### 29.7.2 状态机

```
                    ┌──────────┐
       create ─────▶│ CREATED  │  分配资源，加载 rootfs
                    └────┬─────┘
                         │ 预热完成
                    ┌────▼─────┐
                    │   WARM   │  在 warm pool 里等着（CPU 配额已降为 50m）
                    └────┬─────┘
                         │ session 首次 exec
                    ┌────▼─────┐
                    │  BOUND   │  绑定 (tenant, session)，独占，不可被他人复用
                    └────┬─────┘
              ┌──────────┼──────────┐
       有 exec │   idle 90s          │ 超过 TTL / 租户删会话
              │          ▼           ▼
              │    ┌──────────┐  ┌──────────┐
              │    │  IDLE    │  │ EVICTED  │  销毁 + 清盘
              │    └────┬─────┘  └──────────┘
              │         │ idle 5min
              │         ▼
              │    ┌──────────────┐
              └────│  SUSPENDED   │  snapshot 到磁盘（内存也落盘），释放全部 CPU/内存
                   └──────┬───────┘
                          │ 下次 exec → restore（5~30ms）+ 恢复 BOUND
                          └──────────────▶ BOUND
```

**`SUSPENDED` 这一档是成本的关键**：Firecracker 支持把 VM 的内存与设备状态快照到文件，恢复只要几十毫秒。这意味着一个「5 分钟没用」的会话可以从「占 1GiB 内存」变成「占 200MB 磁盘」，成本差 10 倍以上。

### 29.7.3 复用的四条铁律

| 铁律 | 原因 | 违反的后果 |
| --- | --- | --- |
| **同租户才能复用** | 跨租户复用 = 数据泄露 | 一个租户能在 `/work` 里看到上一个租户的文件 |
| **跨租户必须重建（不是清理）** | 「清理脚本」永远清不干净（隐藏文件、扩展属性、内核缓存、被删但被占用的 inode） | 你以为清了，其实没清 |
| **复用要有代数（generation）** | 沙盒被污染后无法回滚 | 只能重建，且要知道重建的是哪个版本 |
| **会话结束必须销毁，不进 warm pool** | 退回 warm pool 等于把脏状态交给下一个人 | 同上 |

```go
// 复用判定：三个条件全部满足才复用，否则新建
func (m *Manager) Acquire(ctx context.Context, tenant, session string) (*Sandbox, error) {
    if sb, ok := m.bySession(session); ok {
        // ① 租户必须一致（这是安全检查，不是优化）
        if sb.TenantID != tenant {
            return nil, ErrTenantMismatch // 不应该发生；发生了说明 sessionID 碰撞或被猜测
        }
        // ② 配额没超
        if sb.CPUUsed() > sb.Spec.Limits.TotalCPUTime { return m.recreate(sb) }
        // ③ 代数没变（镜像/策略更新后强制重建）
        if sb.Generation < m.currentGeneration {
            return m.recreate(sb)
        }
        return m.restoreIfSuspended(ctx, sb)
    }
    return m.createFromWarmPool(ctx, tenant, session)
}
```

> ⚠️ `ErrTenantMismatch` **必须告警而不是静默重建**。它意味着 sessionID 被猜测或被跨租户引用——这是一个安全问题，不是一个缓存 miss。

---

## 29.8 成本与冷启动：把账算给你老板看

### 29.8.1 单次执行成本

```
一次「跑用户脚本」：CPU 2 核 × 30s = 60 核秒 = 0.0167 核时
云上按需价 ≈ ¥0.15/核时（不同区域差异大，按你自己的账单）
→ 纯计算成本 ¥0.0025/次
→ 1000 次 = ¥2.5
```

看起来便宜到可以忽略。**但真实成本不在这里**：

### 29.8.2 复用 vs 不复用：13 倍的差距

```
场景：一个数据分析会话，用户 10 分钟里跑了 3 次脚本，每次 30s

【方案 A：每次新建】
  3 × (冷启动 0.15s + 装包 12s) 的计算开销
  + 3 次冷启动的镜像拉取 IO
  + 用户感知：每次多等 12 秒
  → 计算 3 × 2核 × 42s = 252 核秒 = 0.07 核时 = ¥0.0105
  → 但装了 3 遍包，拉了 3 遍镜像

【方案 B：会话复用，沙盒常驻 10 分钟】
  沙盒全程 2 核 × 600s = 1200 核秒 = 0.333 核时 = ¥0.05
  → 贵 5 倍！因为 90% 的时间在 idle

【方案 C：复用 + idle 降配额 + suspend】
  活跃期 90s × 2 核 = 180 核秒
  idle 期 510s × 0.05 核（降配额）= 25.5 核秒
  → 205.5 核秒 = 0.057 核时 = ¥0.0086   ← 比 A 便宜，比 B 便宜 6 倍
```

**结论：复用必须配 idle 降配额，否则复用比不复用还贵。**

| 方案 | 单会话成本 | 用户等待 | 冷启动次数 | 适用 |
| --- | --- | --- | --- | --- |
| A 每次新建 | ¥0.0105 | +12s × 3 | 3 | 一次性代码执行（低代码工具） |
| B 常驻复用 | ¥0.05 | 无 | 1 | 高频交互的编码会话 |
| **C 复用 + 降配额 + suspend** | **¥0.0086** | 无 | 1 | **默认选这个** |

### 29.8.3 容量：Aurora 的真实数字

```
日均 Run 800 万，其中需要代码执行/沙盒的占 8% = 64 万次
峰值沙盒创建速率 = 400 Run/s × 8% = 32 个/s
会话平均活 3 分钟 → 常驻沙盒 = 32 × 180 = 5,760 个

单沙盒规格：CPU 上限 2 核，平均实际使用 0.15 核
→ 总核数需求 = 5,760 × 0.15 = 864 核
→ 64 核/台 → 14 台（考虑 AZ 冗余 × 2 → 28 台）
→ 内存：5,760 × 平均 300MiB = 1.7TiB → 约 7 台 256G 机器（内存是瓶颈！）
→ 取 max(28, 7×2) = 30 台

加上 warm pool（10% 冗余，冷启动要快）→ 33 台
成本 ≈ 33 × ¥3,000/月 ≈ ¥10 万/月
占平台总成本（¥7,200 万/月）的 0.14%   ← 沙盒真的不贵，贵的是没管好
```

> 💡 **这个数字很重要**：它说明「沙盒隔离」不该是省钱的地方，而该是**多花钱买安全**的地方。你为了省 ¥5 万/月把 L3a 降成 L1，可能换来一次 ¥500 万的泄露事故——这个账没人会算错，但很多人会因为「Firecracker 运维麻烦」而做错。

---

## 29.9 Java / 分布式经验对照

| 你熟悉的 | Agent 沙盒里 | 会咬人的差异 |
| --- | --- | --- |
| Docker / K8s Pod 跑业务代码 | 同一个技术，但威胁模型完全不同 | 🔴 业务代码是你写的；沙盒里的代码是**模型写的、可能被注入的**。默认信任级别要降到最低 |
| `-Xmx` 限制 JVM 堆 | `memory.max` 限制 cgroup | 🟡 JVM 会「看到」宿主机内存来决定堆大小（老版本），必须显式设 `-XX:MaxRAMPercentage` 或 `-Xmx`，否则 OOM 了才发现 |
| 线程池 + `ThreadPoolExecutor` | 沙盒内的进程管理 | 🔴 完全不可控：里面是任意语言、任意进程树。只能靠 cgroup 的 `pids.max` 和 `cgroup.kill` |
| 熔断降级（Sentinel） | 沙盒失败的重试策略 | 🟡 沙盒失败**不能自动重试**（可能是确定性错误，重试只是再花一次钱）；要区分 TIMEOUT（可重试）与 OOM/PID_LIMIT（要改配额，重试无用） |
| 容器镜像 tag | 必须用 digest | 🔴 `:latest` 在沙盒场景下是供应链后门 |
| `@PreAuthorize` 权限注解 | 出口策略 + capability 令牌 | 🟡 沙盒内的进程没有「用户身份」，权限只能靠外部（代理/令牌）施加，不能靠应用层注解 |
| JVM SecurityManager（已废弃） | 沙盒 | 🔴 SecurityManager 在 JDK 17 起被废弃/移除；不要幻想用语言级沙箱替代 OS 级隔离 |
| 灰度与回滚 | 镜像 digest + generation | 🟢 思路一致：换代要能一键切回 |

---

## 29.10 开源坐标

| 项目 | 是什么 | 能抄什么 | 缺什么 / 坑 |
| --- | --- | --- | --- |
| **Firecracker**（AWS 开源，Rust） | microVM monitor，最小设备模型（virtio-net/block/vsock），启动快、内存开销小 | 生产级 microVM 的全部原语；配 `jailer` 做 cgroup/ns 隔离 | 要自己管 rootfs、内核、网络；无 GPU 直通；运维复杂度高于容器 |
| **gVisor**（Google 开源，Go） | 用户态内核（Sentry）+ Gofer 文件代理，`runsc` 兼容 OCI | 「不改镜像就获得强隔离」的最快路径（K8s RuntimeClass 一换就行） | syscall 兼容性有坑（`ioctl`、部分 `mmap`、`/proc` 语义）；性能开销随负载差异大，必须实测 |
| **Kata Containers** | 每 Pod 一个轻量 VM（QEMU/Cloud Hypervisor），OCI 兼容 | 「容器体验 + VM 隔离」；K8s RuntimeClass 直接接 | 启动比 Firecracker 慢；内存开销比 microVM 大 |
| **WASM 运行时**（`wasmtime`、`wazero`） | 指令级沙箱；`wazero` 是纯 Go 实现（无 cgo） | 极致密度与 1~10ms 冷启动；适合「纯计算」工具 | 跑不了真项目（无 fork、无原生扩展、Python/Node 生态受限）；WASI 能力集仍在演进 |
| **E2B** | 面向 AI 的代码沙盒服务，底层 Firecracker；SDK 开源、控制面托管 | 调用契约设计、会话复用模型；可以先用它顶住再自建 | 托管服务的数据驻留与合规要评估（第 31 章）；深度定制受限于其 API |
| **`nsjail` / `bubblewrap`** | 进程级沙盒（namespace + seccomp + rlimit） | L0 档的现成实现；适合「跑一次 shell 命令」这种轻场景 | 共享内核，不能防提权；别用它跑不可信代码 |
| **Landlock LSM** | 无特权进程的文件系统访问限制（Linux 5.13+） | 在 L0/L1 上再加一层「只能读这几个路径」 | 只管文件系统，不管网络；内核版本要求 |
| **K8s RuntimeClass** | 用注解切换容器运行时（runc / runsc / kata） | 同一套 YAML，按租户/场景切隔离等级 | 只解决「用哪个 runtime」，配额与出口策略仍要自己做 |

---

## 29.11 事故清单

- [ ] **事故 A：没有 `pids.max`** → 现象：fork bomb 打满节点 PID，sshd 都起不来，节点失联只能硬重启，该节点全部租户的 Run 丢失。→ 兜底：`pids.max` 强制 256 + 节点级 PID 水位告警（>70% 就隔离节点）
- [ ] **事故 B：沙盒能访问 `169.254.169.254`** → 现象：拿到节点 IAM 凭证，横向扩展到整个云账号。→ 兜底：egress 默认 deny + 元数据 IP 显式拦截 + **节点侧 IMDSv2 强制 + hop limit = 1**
- [ ] **事故 C：容器共享宿主网络** → 现象：沙盒扫内网 Redis，dump 3 个租户的会话缓存。→ 兜底：独立 netns + 唯一出口代理 + 内网网段 deny
- [ ] **事故 D：`memory.swap.max` 没关** → 现象：内存炸弹把整机 swap 打满，全机 IO 停顿 4 分钟，所有租户受影响。→ 兜底：禁 swap；`memory.max` 与 `memory.swap.max` 成对配置
- [ ] **事故 E：只杀主进程** → 现象：exec 超时后 `kill -9 $PID`，但子进程还活着继续跑，配额显示「已结束」实际还在烧 CPU。→ 兜底：`cgroup.kill`（cgroup v2）+ VM 级销毁
- [ ] **事故 F：跨租户复用沙盒** → 现象：会话 ID 碰撞，B 租户在 `/work` 看到 A 租户的 CSV。→ 兜底：复用前强制校验 `TenantID`，不匹配就**告警 + 重建**，绝不静默复用
- [ ] **事故 G：镜像用 `:latest`** → 现象：上游镜像被替换，沙盒开始往外连一个陌生域名。→ 兜底：只允许 digest；镜像签名校验（cosign / Notary）
- [ ] **事故 H：`TotalCPUTime` 缺失** → 现象：恶意用户每次执行 29 秒（刚好不触发 ExecTimeout），跑 200 次，一个会话烧 1.6 小时 CPU。→ 兜底：三层超时（单次/累计/会话 TTL）+ 每会话执行次数上限
- [ ] **事故 I：产物回传不做过滤** → 现象：沙盒里编译出的 `.so` 被带回，之后被另一个工具加载执行。→ 兜底：白名单扩展名 + MIME 校验 + 大小上限
- [ ] **事故 J：沙盒与 API 混部** → 现象：沙盒跑满 CPU，同节点 API Pod 被 throttle，全平台 P99 翻倍。→ 兜底：独立节点池 + taint/toleration + 独立 HPA

---

## 29.12 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 安全公式 | 隔离强度 × 配额完整性 × 出口收敛性，任一为 0 则整体为 0 |
| 四档 | L0 进程（<1ms）/ L1 容器（100~500ms）/ L2 gVisor·Kata（0.3~2s）/ L3 Firecracker·wasm（5~150ms） |
| 选型依据 | 按**代码来源**选，不按「安全等级」选：模型生成/用户上传 → L3a |
| 最重要的两条配置 | `pids.max`（防 fork bomb）和 `default deny egress`（防数据外泄） |
| 元数据 IP | `169.254.169.254` / `100.100.100.200` **必须显式 deny**，这是云上泄露的第一入口 |
| 网络 | 独立 netns + vsock 或唯一出口代理 + DNS 也走代理（防 DNS 隧道） |
| 文件系统 | 只读 rootfs + tmpfs `/tmp` + 有限 `/work`；**绝不挂宿主目录、绝不注入凭证** |
| kill | 杀整个 cgroup（`cgroup.kill`），不是杀进程；VM 级再加一层销毁 |
| 三层超时 | 单次 exec（30s）/ 累计 CPU（5min）/ 会话 TTL（30min），外加执行次数上限 |
| 复用 | 必须同租户 + 带 generation；idle 要降配额或 suspend，否则复用比新建贵 5 倍 |
| 成本 | 单次执行 ¥0.0025；沙盒只占平台成本 0.14%——**别在这里省钱** |
| 一句话 | **先做配额和出口（1 周），再升级隔离等级（1 季度）。做反了，事故率只降 5%。** |

## 29.13 自检题

1. **你已经用了 Firecracker，是不是就可以不管 `pids.max` 和网络策略了？**
   > 要点：不行。Firecracker 隔离的是「内核攻击面」（防逃逸），不隔离「资源」和「网络」。microVM 里 64 核还是 64 核，一个 fork bomb 照样打满宿主机 PID；microVM 走 NAT 出网照样能连内网。配额（cgroup）和出口（代理/白名单）是独立维度的控制，必须单独做。反过来说，一个配置良好的 L1 容器能挡掉 95% 的真实攻击——优先级上，配额与出口应该先于隔离等级。

2. **会话复用能省钱，为什么 Aurora 算出来「常驻复用」比「每次新建」还贵 5 倍？正确做法是什么？**
   > 要点：因为沙盒 90% 的时间在 idle，但 idle 时仍然占着 2 核的配额（或至少占着内存）。常驻 10 分钟 = 1200 核秒 = ¥0.05，而实际只用了 3×30s=180 核秒。正确做法是三件事叠加：① idle 时把 CPU 配额降到 50m（降 40 倍）；② 长时间 idle 直接 suspend（Firecracker snapshot，内存落盘，恢复 5~30ms）；③ 装包结果做层缓存（避免每次重装）。三者合起来能把成本压到每次新建的 80%。

3. **为什么沙盒的 `Killed` 原因必须区分 TIMEOUT / OOM / PID_LIMIT，而不能只返回「失败」？**
   > 要点：三者的处置动作完全不同。TIMEOUT 可能是代码真的慢（可以提示用户优化，或允许重试并延长超时）；OOM 说明内存配额给少了（要提高配额或让用户分批处理）；PID_LIMIT 几乎一定是恶意或 bug（fork bomb），要**告警并可能封禁**，绝不能重试。如果你只返回「失败」，运维看到的就是「沙盒失败率上升」，然后无脑重试——对 PID_LIMIT 重试等于再炸一次节点。

## 29.14 延伸阅读

- 上一章：[28. 大流量不崩：容量、压测与隔离](28-peak-traffic.md)
- 下一章：[30. 内容安全与注入防御](30-injection-content-safety.md)
- 相关：[第 22 章](22-tool-system-mcp.md)（工具系统与 MCP，沙盒是工具的一种执行后端）、[第 32 章](32-defense-hardening.md)（防破解与供应链）、[第 33 章](33-multi-agent-collaboration.md)（子 Agent 的预算与执行隔离）、[第 31 章](31-compliance-audit.md)（沙盒出向流量的审计留存）、[第 44 章](44-multi-tenant.md)（租户隔离三档）
- 总纲：[0-summary.md](0-summary.md)
- 语言/运行时细节：见 [../go/0-summary.md](../go/0-summary.md)（cgroup、进程管理、信号处理）
- 外部：Firecracker 官方设计文档（含 jailer、snapshot/restore）、gVisor 官方文档与兼容性说明、Kata Containers 文档、`wazero`/`wasmtime` 文档、K8s RuntimeClass 文档、Linux cgroup v2 文档（`cgroup.kill`、`memory.swap.max`）
