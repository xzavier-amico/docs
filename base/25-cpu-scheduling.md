# 25. CPU 调度：CFS→EEVDF、nice、亲和性与 cgroup 配额

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 操作系统：Linux 视角 ｜ **编号**：25 / 56
> **本篇定位**：Part III 的第五篇。21–24 讲「一个执行流怎么被创建、怎么被登记进内核的簿记」，本篇回答紧接着的那个问题：**可运行的执行流比核多时，谁上、谁等、等多久**。全篇只有一条推论链——`调度器只在 runnable > nr_cpu 时才做决策` → `决策依据是权重不是优先级` → `权重只在竞争时兑现` → `所有「CPU 不够」的现象最后都落在三个计数器上：runqueue 长度、vruntime/lag、cgroup 配额`。26 讲缺页时 CPU 在干什么，31 讲 IO 在等谁，34 手搓容器（cgroup 的用法），54 给排障工具表。
> **读法**：§25.2 的三条判据决定你「要不要继续读」；§25.4 的 vruntime 算式与 §25.5 的 lag 是同一个东西的两代写法，看懂一个，另一个只是换了名字；§25.7 的 `cpu.max` 算式与 §25.8 的 load 定义是全篇最常被误用的两处，建议先跳到 §25.9 看真实输出再回头读。赶时间只读 §25.2、§25.10–25.12。
> **不在本篇**：线程与 `clone` 的共享参数 → base/24；一次系统调用进入内核的完整路径 → base/21；页表、缺页与 NUMA 的内存侧 → base/26；namespace/cgroup 的手工搭建 → base/34；perf/strace/bpftrace 的分层排障总表 → base/54；Go runtime 的 GMP 调度器与抢占 → [go/19](../go/19-scheduler-runtime.md)；K8s 的 requests/limits 与发布 → [go/52](../go/52-deployment-sre.md)。

---

## 25.1 引子：一条 `nice` 命令把 p99 拖垮，和一台 load 12 的「空闲」机器

某团队的在线服务与离线报表混跑在同一批机器上。值班同学做了一件看起来完全正确的事：给报表进程加 `nice -n 10`，期望「在线服务优先」。第二周 p99 从 42 ms 涨到 97 ms。复盘第一句话是：**「nice 不是优先级，它是权重；权重只有在有人跟它抢同一个核时才兑现。」**

第二件事更常见：一台 8 核机器，`load average` 常年在 9–12，`top` 里 `%Cpu(s)` 只有 30%。结论「机器扛不住，扩容」。加到 16 核后 load 还是 12 上下，CPU 还是 30%——因为那 12 个「负载」里一大半是 `TASK_UNINTERRUPTIBLE`：进程在等块设备 IO，既不在跑，也不需要 CPU。**load 量的是队列长度，不是 CPU 使用率。**

这两件事我都在本机复现过（2 vCPU KVM，Linux 6.12.113）。第一件的切片：

```
$ ./measure.sh 5 0:0          # 一个死循环，绑在 cpu0，nice 0
pid=3343782 ticks=  97 cpu= 19.40%  schedstat= 19.47%
$ ./measure.sh 5 19:0         # 同一个程序，同一个核，nice 19
pid=3343954 ticks=   1 cpu=  0.20%  schedstat=  0.29%
$ ./measure.sh 5 -20:0        # 再来一个 nice -20
pid=3344112 ticks= 491 cpu= 98.20%  schedstat= 98.07%
```

同一个程序、同一个核、同样的 5 秒：`nice 0` 拿 19.5%，`nice 19` 只拿 0.29%，`nice -20` 拿 98%。**这不是「优先级越高越先跑」，是「权重越大，在同一个核的分配里占的份额越大」。** `nice 0` 只有 19.5% 是因为核上还有别的租户的任务在排队；`nice 19` 的 0.29% 也不是「被禁止运行」，而是权重 15 对上约 5000 的对手权重：15/(15+5157) ≈ 0.29%——**`nice -n 19` 等于把程序限速到 1/345。**

第二件事在同一时刻的账：

```
$ cat /proc/loadavg
8.70 6.00 3.37 16/300 3321243
$ grep -E "^(procs_running|procs_blocked)" /proc/stat
procs_running 15
procs_blocked 0
```

`load1 = 8.70`、`procs_running = 15`、`procs_blocked = 0` 三个数字互相对不上，正因为 **load 是 5 秒采样一次的指数滑动平均，`procs_running` 是此刻的瞬时值**。要看「CPU 忙不忙」，得看 `vmstat` 的 `r`/`b` 两列，而不是 load 那一个数。

本篇要把这三句话推开：调度器什么时候才真的在做决定（§25.2）、按什么决定（§25.3–25.5）、你为什么会被它限住（§25.6–25.7）、以及那三个最常被读错的数字（§25.8）。

---

## 25.2 调度器的存在条件：runnable ≤ 核数 时，它几乎不做决策

把核的 runqueue 想成一个货架。调度器每次只做两件事：**把当前任务放回货架**、**从货架上拿一个**。货架上只有一个时，「拿哪个」这个问题不存在——不管用的是 CFS、EEVDF 还是别的什么，唯一的选择就是不选择。

```
   核 0 的 runqueue                  核 1 的 runqueue
  ┌──────────────────┐             ┌──────────────────┐
  │  A (vruntime 12) │             │  (空)            │
  └──────────────────┘             └──────────────────┘
        ↓ 只有一个候选                     ↓ 没有候选
   调度器不比较、不排序、不记账        调度器直接进 idle
```

这条事实的推论比它看上去强：

1. **「我的服务只有 4 个线程，机器有 8 核」时，任何调度参数都调不出性能**。每个线程各自占住一个核，nice、`sched_latency`、`cpu.weight` 全都不参与分配。
2. **「我只有 2 个进程」不等于「机器上只有 2 个 runnable」**。runnable 数是全机的，含所有租户、容器、内核线程。只要整机 runnable > 核数，你的两个进程就要排**别人的**队。§25.1 那个 `nice 19` 只拿 0.29% 的实验就是这么来的：我只起了 1 个进程，但那个核上还有别人的任务在排队（对手权重合计约 5000）。
3. **调度器从不「让程序变快」，它只决定「谁等」**。总 CPU 时间守恒，调度能改变的只有分摊方式与等待时间的分布（主线②：快慢只有一种解释——排队）。任何「调调度器提升吞吐」的说法，前提都是「原来分摊得不合理」。

一个很干净的反证来自 cgroup 权重：两个任务分别放进 `cpu.weight=1000` 和 `2000` 的两个 cgroup，**不绑核**，2 个 vCPU 正好一人一个：

```
cpu.weight A=1000 B=2000  window=5s
  A usage=1.002 核 (100.2% of 单核)   B usage=1.055 核 (105.5% of 单核)
  A:B = 0.9 : 1   （权重比 1000:2000 = 0.5 : 1）
```

权重差 2 倍，实际 1:1。把两个任务绑到同一个核上再跑：

```
cpu.weight A=1000 B=2000  window=5s
  A usage=0.329 核 (32.9% of 单核)   B usage=0.659 核 (65.9% of 单核)
  A:B = 0.5 : 1   （权重比 1000:2000 = 0.5 : 1）
```

**1:2.00，误差 0.2%。** 同一组权重、同一组进程，唯一的变化是「有没有竞争」——这就是「调度器只在要跑的比核多时才存在」的实测版本。

### 什么时候你才该关心调度：三条判据

| 判据 | 怎么量 | 满足时的含义 |
| --- | --- | --- |
| **① 每核 runnable 长期 > 1** | `vmstat 1` 的 `r` 列 ÷ `nproc`；或 `load1` ÷ 核数（只作粗估） | 队列已经形成，调度开始影响吞吐与延迟 |
| **② runqueue 等待占请求延迟的可观比例** | `/proc/PID/schedstat` 第 2 字段（等待 ns）÷ (runtime + wait) | 慢在「等 CPU」，不在「算得慢」 |
| **③ 存在硬约束**：RT/DEADLINE、cgroup 配额、绑核、NUMA、尾延迟 SLA | `chrt -p`、`cpu.max`、`taskset -p`、`numactl -H` | 调度不再是「公平分配」，而是「能不能满足约束」 |

三条都不满足时，调 `sched_latency`、改 nice、绑核基本是白忙：瓶颈在 IO、锁、GC 或下游。反过来，只要②成立就一定能量化——本机三个任务绑在同一个核上跑 5 秒：

```
== 2 任务绑同一核 ==
pid=3348537 runtime=1.46s wait=3.55s  wait/(run+wait)=70.9%
pid=3348538 runtime=1.46s wait=3.54s  wait/(run+wait)=70.8%
== 3 任务绑同一核 ==
pid=3349116 runtime=1.15s wait=3.85s  wait/(run+wait)=77.0%
```

外负载太重，绝对值不干净，但方向清楚。值得记住的是这条式子：**`schedstat` 的第二个字段就是「我在货架上躺了多久」，排队时间可以直接读出来，不需要猜。**

---

## 25.3 调度类与优先级：`stop` > `deadline` > `realtime` > `fair` > `idle`

### 五个类，一条单向的「谁压谁」

Linux 不是「一个调度器」，是**五个调度类串成的一条链**。每次选下一个任务，从最高类开始问，谁先给出候选就选谁：

| 类 | 内核符号 | 用户接口 | 典型使用者 | 能否被用户设置 |
| --- | --- | --- | --- | --- |
| stop | `stop_sched_class` | 无 | 迁移线程、CPU 热插拔（`migration/N`） | 否 |
| deadline | `dl_sched_class` | `SCHED_DEADLINE`（`sched_setattr`） | 硬实时、音视频管线 | 是（需权限，有准入控制） |
| realtime | `rt_sched_class` | `SCHED_FIFO` / `SCHED_RR`（`chrt`、`sched_setscheduler`） | 中断线程化、低延迟采集 | 是（需 `CAP_SYS_NICE`） |
| fair | `fair_sched_class` | `SCHED_OTHER` / `SCHED_BATCH`（nice） | 99% 的普通进程 | 是 |
| idle | `idle_sched_class` | `SCHED_IDLE` | 只在 CPU 空闲时跑的后台活 | 是 |

**这张表是「优先级」唯一正确的读法：类之间是硬隔离，类内部才是分配。** `SCHED_FIFO` 的任务只要 runnable，同核上所有 fair 任务一个都跑不了——不管那个 fair 任务的 nice 是 -20 还是 19。nice 只活在 fair 类里面，这就是「nice -20 也不等于实时」。

### RT 的危险：它能把内核线程一起饿死

`SCHED_FIFO` 没有时间片：prio 50 的 FIFO 任务会一直跑，直到自己阻塞、让出，或被更高优先级的 RT 抢占。先看这台机器上本来就跑着什么：

```
$ ps -eLo pid,cls,rtprio,ni,comm | awk '$2!="TS"' | head -5
    PID CLS RTPRIO  NI COMMAND
     21  FF     99   - migration/0
     22  FF     50   - idle_inject/0
     25  FF     50   - idle_inject/1
     26  FF     99   - migration/1
```

`migration/0`、`migration/1` 是内核的 CPU 迁移线程，优先级 FF 99。**它们优先级高，不等于不会被饿死**：如果用户起一个 prio 99 的 FIFO 任务并绑在 cpu0 上不放手，cpu0 上的 fair 任务（`ksoftirqd/0`、`rcu_preempt`、`kworker/*`）全部停摆，网络软中断、RCU 回调、块设备完成统统不推进，最后表现为 `rcu: INFO: rcu_preempt detected stalls`、`task xxx blocked for more than 120 seconds`，或者「网卡坏了」——其实只是中断只进不出。

内核给自己留了一道保险，本机实测的默认值：

```
$ cat /proc/sys/kernel/sched_rt_period_us /proc/sys/kernel/sched_rt_runtime_us
1000000
950000
```

**每个 1 秒周期，RT 任务最多用 950 ms，剩下 50 ms 留给 fair 类。** 超过预算后内核打印 `sched: RT throttling activated`，把 RT 任务按在原地等下个周期。它救的是「系统还能响应」，救不了你的 p99——50 ms/秒的缺口足够让任何低延迟服务崩掉。这也是本篇的实验里**我坚决不设 RT 优先级**的原因：在一台共享的 2 核 KVM 上，一个 prio 99 的 spinner 有可能让 SSH 都进不来。

一层更隐蔽的差别：`SCHED_DEADLINE` 有准入控制（admission control）。你申请 `runtime=8ms, deadline=10ms, period=10ms`（80% 的核），内核会算全系统带宽，装不下就直接 `EBUSY`。**DEADLINE 因此不会超额预订，RT 则没有任何准入检查。**

### `nice` 与 `rtprio` 是两套坐标

| | nice | rtprio |
| --- | --- | --- |
| 取值范围 | -20 … 19（40 级） | 1 … 99（FIFO/RR） |
| 生效的类 | 只有 fair | 只有 RT |
| 语义 | **权重**（相对份额） | **优先次序**（严格抢占） |
| 改它的命令 | `nice -n`、`renice`、`setpriority(2)` | `chrt -f/-r`、`sched_setscheduler(2)` |
| 需要权限 | 提高优先级（减小 nice）需 `CAP_SYS_NICE` | 一律需要 `CAP_SYS_NICE` |
| 拿不到 CPU 时 | 份额变小，但仍会跑（除非被压到 1/345） | 完全不跑，直到高优先级者让出 |

`chrt` 的可用范围本身就把这条边界画出来了：

```
$ chrt -m
SCHED_OTHER min/max priority	: 0/0
SCHED_FIFO min/max priority	: 1/99
SCHED_RR min/max priority	: 1/99
SCHED_BATCH min/max priority	: 0/0
SCHED_IDLE min/max priority	: 0/0
SCHED_DEADLINE min/max priority	: 0/0
```

`SCHED_OTHER` 的 min/max 都是 0——**fair 类里没有「优先级」这个字段可填**，只有 nice 这个权重旋钮。`ps` 的 `PRI` 列是内核 prio 的一个投影（TS 段与 RT 段刻度不同），判类别看 `CLS` 和 `RTPRIO`。

一句话：**类是「能不能跑」，nice 是「跑多少」。** 把 nice 当优先级用，就会得到 §25.1 那个 0.29%。

---

## 25.4 CFS 的账：`vruntime` 是「加权之后的 CPU 时间」

### 一条公式推完整个公平性

CFS（Completely Fair Scheduler，2.6.23 – 6.5）的全部魔法只有一个变量：**虚拟运行时间 `vruntime`**。任务每实际运行 `delta` 纳秒，`vruntime` 就增加：

```
vruntime += delta × (NICE_0_LOAD / weight)        NICE_0_LOAD = 1024
```

即 `vruntime` 的单位是「**nice 0 的纳秒**」：nice 0 跑 1 ms，`vruntime` 涨 1 ms；nice 10 跑 1 ms，`vruntime` 涨 1024/110 = 9.31 ms。

调度规则只有一句：**永远选 `vruntime` 最小的那个**。于是「公平」被定义成「大家的 `vruntime` 尽量相等」，而不是「时间片一样长」。两个任务的 `vruntime` 同步增长时：

```
delta_1 × 1024/w_1 = delta_2 × 1024/w_2
        ⟹  delta_1 / delta_2 = w_1 / w_2
```

**CPU 时间之比 = 权重之比。** 这就是 nice 的全部数学，没有第二层。

权重来自内核的 40 项查表 `prio_to_weight[]`，每级差 1.25 倍：

| nice | 权重 | 与 nice 0 的比 | 说明 |
| --- | --- | --- | --- |
| -20 | 88761 | 86.7× | 只有 root 能设，能压住整机 |
| -10 | 9548 | 9.32× | |
| -5 | 3121 | 3.05× | |
| 0 | 1024 | 1.00× | 基准 |
| 1 | 820 | 0.80× | 每 +1 级 ÷1.25 |
| 5 | 335 | 0.327× | |
| 10 | 110 | 0.107× | 约 1/9.3 |
| 15 | 36 | 0.035× | |
| 19 | 15 | 0.0146× | 约 1/68 |

为什么是 1.25？为了「nice 每差 1 级，份额差约 10%」：1.25¹⁰ ≈ 9.3，正好让 nice 0 对 nice 10 是 9.3:1；1.25²⁰ ≈ 87，让 -20 对 0 也是 87:1。**一条等比数列同时定义了两端，这是这张表唯一的设计约束。**

### 两个算例

**两个 nice 0 的任务，一个核。** 权重 1024 + 1024，各占 50%。本机实测（绑同一核，5 秒采样 `utime+stime`，HZ=100）：

```
$ ./measure.sh 5 0:0 0:0
pid=3297522 ticks= 112 cpu= 22.40%  schedstat= 22.38%
pid=3297523 ticks= 111 cpu= 22.20%  schedstat= 22.44%
```

112 : 111 = **1.009 : 1**。绝对百分比只有 22% 是因为核上还有外部任务，但**比值**是干净的——这也是本篇所有实验都用比值下结论的原因。

**nice 0 对 nice 10，一个核。**

```
份额 = 1024/(1024+110) : 110/(1024+110) = 90.30% : 9.70%
比值 = 1024/110 = 9.309 : 1
```

实测两次：

```
$ ./measure.sh 5 0:0 10:0
pid=3323289 ticks=  85 cpu= 17.00%  schedstat= 17.14%
pid=3323290 ticks=   9 cpu=  1.80%  schedstat=  1.83%
$ ./measure.sh 5 0:0 10:0
pid=3297723 ticks=  96 cpu= 19.20%  schedstat= 19.19%
pid=3297724 ticks=  11 cpu=  2.20%  schedstat=  2.07%
```

17.14/1.83 = **9.37**，19.19/2.07 = **9.27**，理论 9.31，误差 1% 以内。顺带一个观察：用 `utime` 的 jiffies 算比值（85:9 = 9.44、96:11 = 8.73）比用 `schedstat` 抖得多，因为 10 ms 记账粒度对 1.8% 这种份额只有个位数 tick。**要量比例读 `/proc/PID/schedstat` 的纳秒字段；要证明「它跑了」，`utime` 够了。**

### 调度周期：`sched_latency` 与 `min_granularity`

CFS 不是每纳秒都比一次，它按周期发时间片。两个经典参数（6.6 之前的默认值）：`sched_latency_ns = 6 ms`（目标周期，每个 runnable 任务每周期至少被调度一次）、`sched_min_granularity_ns = 0.75 ms`（单次最少跑多久，防止切换开销吃掉一切）。

```
period = max(sched_latency, nr_running × min_granularity)
slice_i = period × w_i / Σw
```

| runnable 数 | 周期 | 每个 nice 0 的 slice |
| --- | --- | --- |
| 2 | 6 ms | 3 ms |
| 4 | 6 ms | 1.5 ms |
| 8 | 6 ms | 0.75 ms（正好触到下限） |
| 16 | 12 ms | 0.75 ms |
| 64 | 48 ms | 0.75 ms |

**这张表就是「机器一忙，延迟就抖」的算术版本**：64 个 runnable 时，一个任务从醒来到被调度最多要等 48 ms，与它自己算得快不快毫无关系。

再代进上面的算例：nice 0 的 slice = 6 × 1024/1134 = 5.42 ms，nice 10 的 slice = 6 × 110/1134 = 0.58 ms。**0.58 ms 低于 `min_granularity` 0.75 ms**，所以在 CFS 上它实际会被抬到 0.75 ms 左右，理论比值从 9.31:1 退化成 (6−0.75)/0.75 = 7:1。这是模型的推论，不是本机实测——本机是 6.12/EEVDF，量到的是 9.3:1（§25.5 解释为什么）。**这条差异本身就是 CFS 被替换的理由之一：低权重任务的 slice 被下限抬升，高权重任务却拿不到更短的延迟，权重旋钮在两端都失真。**

### 切换的代价

时间片不能无限小，因为每次切换都有固定成本：保存/恢复寄存器与栈、换页表（若跨进程）、污染 L1/L2 与分支预测器。本机 `/sys/kernel/debug/sched/migration_cost_ns = 500000`（0.5 ms）——这是内核对「把一个任务搬到另一个核」估的价，也是负载均衡要求的最小收益。对比 `base_slice_ns = 1400000`（1.4 ms）：一次迁移的价格是 1/3 个时间片。这就是「绑核能提速」的物理来源（§25.6），也是「核越多、调度越贵」的原因。

---

## 25.5 EEVDF（6.6+）：从「谁欠谁」到「谁最该先跑」

### CFS 的三个结构性毛病

CFS 只维护一个量（`vruntime`），因此它只能回答「谁欠谁多少 CPU」：

1. **延迟不公平**。CFS 的周期是全队列共享的：runnable 越多，每个任务的等待越长。你无法表达「我这个任务每 2 ms 必须跑一次」，只能通过加权重去「买」更频繁的调度——而权重同时会改变份额，两件事绑死。
2. **睡眠补偿过头**。睡了 100 ms 的任务醒来时 `vruntime` 停在原处，远小于正在跑的任务，于是会连续抢占一大段直到追平。对交互 shell 是好事（打字跟手），对 1 万 QPS 的服务就是抖动源：任何一个偶发唤醒的线程都能制造一次几十毫秒的抢占。
3. **权重与 slice 耦合**。低权重任务的 slice 被 `min_granularity` 抬高，高权重任务的 slice 却不会相应缩短，于是「高优先级 = 更低延迟」这个直觉在 CFS 上并不成立。

### EEVDF 用两个量替代一个量

EEVDF（Earliest Eligible Virtual Deadline First）在 `vruntime` 之外给每个任务两样东西：

```
lag_i        = 应得的加权 CPU 时间 − 已得的加权 CPU 时间      （> 0 表示被欠）
vdeadline_i  = vruntime_i + slice_i × (1024 / weight_i)      （虚拟截止时间）
```

- **lag** 是「公平账」：lag > 0 是债主，lag < 0 欠着别人。
- **vdeadline** 是「紧迫度」：把「我还欠你多少」和「你要求多快跑一次（slice）」合成一个可比较的数。权重越低，`1024/weight` 越大，截止时间被推得越远——权重语义就这样被复现。
- **选择规则**：先筛出 **eligible**（lag ≥ 0 附近，即 `vruntime` 不落后于队列平均虚拟时间）的任务，再在其中选 **vdeadline 最小**的。名字就是这么来的：最早（Earliest）合格（Eligible）虚拟截止时间（Virtual Deadline）。

关键收益是**解耦**：`slice` 变成每个任务可以单独请求的东西（`sched_setattr(2)` 的 `sched_runtime` 字段，`SCHED_NORMAL` 也能用），权重只管份额。于是「我要低延迟」和「我要低权重」终于可以分开表达——这正是 CFS 做不到的那件事。

本机的内核把 EEVDF 的开关摊在了 debugfs 里：

```
$ cat /sys/kernel/debug/sched/base_slice_ns
1400000
$ cat /sys/kernel/debug/sched/features
PLACE_LAG PLACE_DEADLINE_INITIAL PLACE_REL_DEADLINE RUN_TO_PARITY PREEMPT_SHORT ...
```

`base_slice_ns = 1.4 ms` 是这台机器上 nice 0 任务的基本时间片（CFS 的 6 ms/0.75 ms 那套参数在这里已经不存在）。`PLACE_LAG` 就是「睡眠任务醒来时把它的 lag 放回队列」这个策略——它给 CFS 那种无上限的补偿设了上界：新唤醒任务的 lag 至多值一个 slice。

### 为什么实测是 9.3:1 而不是 87:1

回到 §25.4 的算例。如果 EEVDF 的 slice 也按权重同比缩小，nice 10 的份额会是 (110/1024)² ≈ 1.2%，即 86:1；实测 9.3:1，与 `weight` 的一次方吻合。**这说明 slice 基本恒定（1.4 ms 量级），权重只作用在虚拟截止时间上**——`vdeadline` 里的 `1024/weight` 是唯一的权重因子。这条「用实测反推实现」的推理，比背一遍源码更能让你相信数字。

### 对你意味着什么

| 变化 | 你该做什么 |
| --- | --- |
| nice 语义不变（仍是权重，仍是 1.25 一级） | 旧的 nice 经验全部有效；但别指望它降延迟 |
| 默认行为变了（睡眠补偿有上界，延迟更可预测） | 交互型负载的尾延迟通常变好；靠「唤醒抢占」的老优化要重新量 |
| `sched_runtime` 可以请求 slice | 真正需要低延迟的线程可用 `sched_setattr` 把 slice 压到 0.5–1 ms，**不必**加权重 |
| 观测口径变了 | `sched_latency_ns` 已不存在，看 `/sys/kernel/debug/sched/base_slice_ns`；`/proc/PID/schedstat` 仍然有效 |
| 升级内核要重新验 | 6.5→6.6 的切换会改变尾延迟分布，基准测试必须重跑，不能拿旧曲线外推 |

---

## 25.6 亲和性：绑核为什么有时更快、有时更慢

### 三个层次，别混

| 机制 | 接口 | 作用范围 | 说明 |
| --- | --- | --- | --- |
| 任务亲和性 | `sched_setaffinity(2)`、`taskset`、`pthread_setaffinity_np` | 单个线程 | 位掩码，最常见 |
| cpuset cgroup | `/sys/fs/cgroup/.../cpuset.cpus` | 整棵进程树 | 还管内存节点 `cpuset.mems` |
| NUMA 策略 | `set_mempolicy(2)`、`numactl --membind` | 进程的内存分配 | 绑 CPU 不绑内存，仍可能拿到远端页 |

`sched_setaffinity` 在内核里只做一件事：把 `task_struct` 的 `cpus_allowed` 掩码换掉，若当前 CPU 不在新掩码里就触发一次迁移。所以它是**允许集合**，不是「钉死」——内核仍可在集合内部迁移你（`cpuset` 才是更强的边界）。

```c
#define _GNU_SOURCE
#include <sched.h>
#include <stdio.h>

int main(void)
{
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(1, &set);                    /* 只允许 cpu1 */
    if (sched_setaffinity(0, sizeof(set), &set) != 0) {
        perror("sched_setaffinity");
        return 1;
    }
    for (volatile long i = 0; i < 100000000L; i++) { }
    return 0;
}
```

命令行等价物，以及它留下的痕迹：

```
$ taskset -c 0 ./burn 300000000 &
$ grep -E "^(Name|Pid|Cpus_allowed_list|Mems_allowed_list)" /proc/$!/status
Name:	burn
Pid:	3320515
Cpus_allowed_list:	0
Mems_allowed_list:	0
$ taskset -p 3320515
pid 3320515's current affinity mask: 1
$ grep Cpus_allowed_list /proc/self/status
Cpus_allowed_list:	0-1
```

`Cpus_allowed_list` 是排查亲和性问题的第一现场：**它显示的是掩码，不是「实际跑了哪些核」**。

### 超线程：兄弟核不是核

SMT 让两个逻辑 CPU 共享一个物理核的执行单元。本机是 AMD EPYC 7K62 的 2 vCPU，两者不是兄弟：

```
$ cat /sys/devices/system/cpu/cpu0/topology/thread_siblings_list
0
$ lscpu | grep -E "Thread|Core|Socket"
Thread(s) per core: 1
Core(s) per socket: 2
```

真机上你更可能看到 `0-1`、`2-3` 这样的成对关系。**绑到兄弟核的两个线程不会得到 2 倍算力**，因为 ALU/FPU 只有一份：典型收益 1.2–1.3 倍（靠填充内存停顿），遇到 AVX 重负载甚至接近 1 倍。判断方法只有一条：看 `thread_siblings_list`。

### NUMA：绑核不绑内存，等于只做了一半

多路机器上内存挂在节点上，跨节点访问延迟明显更高（量级见 base/07 的存储层次表）。只做 `taskset` 不做 `numactl --membind`，线程在 node0 的核上跑、内存却可能被分配到 node1，「绑核」带来的缓存收益被远端内存抵消。`/sys/devices/system/node/node*/cpulist` 给节点拓扑，`numastat -p PID` 给命中分布。

### 为什么绑核有时更快，有时更慢

**更快**：① 缓存与 TLB 变热（本机 L1d 合计 128 KiB、L2 合计 1 MiB），迁移一次就要重新填，内核对迁移的估价是 0.5 ms；② 分支预测器与预取器都是 per-core 状态，跨核迁移等于清零；③ 干扰可预测——固定核上你能算出有几个别的任务，不绑则会被均衡器随机丢进任意核，p99 变成「哪个核最忙」的函数。

**更慢**：① 没有负载均衡，绑死后这个核上任务再多也不挪；② 绑错核——cpu0 默认承接大量中断（`/proc/interrupts` 里 `LOC`、`eth0` 的列），把延迟敏感线程绑到 cpu0 是最常见的错误；③ 把 N 个 worker 绑到同一个核，总吞吐直接变成 1/N。本机实测（2 个固定工作量的进程，单位秒）：

| 绑法 | 第 1 轮 | 第 2 轮 | 第 3 轮 | 最小值 |
| --- | --- | --- | --- | --- |
| 各绑一个核（`split`） | 8.29 | 10.40 | 8.90 | **8.29** |
| 都绑 cpu0（`one`） | 10.00 | 12.48 | 12.57 | **10.00** |

只有 1.21 倍，不是理论上的 2 倍。原因是这台机器上还有 15 个外部 runnable，**两个核早就被别人占满了**：绑核只是把两个任务塞进一个同样拥挤的队列，收益差被稀释。空闲机器上这个比值会接近 2.0。一条工程经验：**绑核的收益永远要除以「这个核本来有多忙」。**

---

## 25.7 cgroup 配额：有两个核，却只能用半个

### `cpu.max` 的算术

cgroup v2 用一行两个数表达 CPU 上限：`<quota> <period>`，单位微秒，period 默认 100000（100 ms），范围 1000–1000000。

```
1 个核     = 100000 / 100000   （每 100 ms 最多用 100 ms CPU）
0.5 个核   =  50000 / 100000
0.1 个核   =  10000 / 100000
2 个核     = 200000 / 100000   （在 2 核机器上等于不限）
不限       =     max 100000
```

**这个比值是「每个周期的总量」而不是「瞬时速率」**：cgroup 里所有线程共享额度，可以在一个周期里用 5 ms 吃掉 1 核额度的 5%，然后剩下 95 ms 全体停摆。本机实测，2 个死循环放进 quota=1 核的 cgroup：

```
cpu.max=100000/100000 (1.00 核)  tasks=2  window=4.0s
  usage=1.00 核时/秒  (占 100% 单核)   periods=40 throttled=5 (12%)  throttled_ms=8
```

**两个任务、两个核可用，但总用量精确停在 1.00 核。** `periods=40` 对应 4 秒 ÷ 100 ms，`throttled=5` 说明有 5 个周期被限流。把额度减半：

```
cpu.max=50000/100000 (0.50 核)  tasks=2  window=4.0s
  usage=0.50 核时/秒  (占 50% 单核)   periods=40 throttled=40 (100%)  throttled_ms=2449
```

**40 个周期全部被限流**，这是 quota < 需求的必然结果：每个周期一开始就把额度烧完，剩下的时间所有线程一起等下一个周期。这就是「明明有核却跑不满」的完整机制——不是调度器不给你核，是 cgroup 的带宽控制器在每个周期末把整个队列摘了下来。

### 限流的形态：锯齿与 p99

被限流的线程不是「变慢」，是**在周期边界上被整体冻结**：

```
额度 50ms / 周期 100ms
CPU
50ms ┤      ██                ██                ██
     │      ██                ██                ██
  0  └──────┴───────┬─────────┴───────┬─────────┴──→ t
        跑满 50ms   被冻结 50ms        跑满 50ms
                   ↑ 这段里所有线程一起停
```

所以症状是：**平均 CPU 看起来「没满」，但每个请求的尾延迟里多了一段最多 1 个周期的等待**。请求越短，这段固定等待占比越大——一个本该 2 ms 完成的请求，可能稳定地变成 52 ms。而且这段等待不体现在 `top` 的 CPU 百分比里——只有 `cpu.stat` 的 `nr_throttled` 在涨。

| 观测点 | 命令 | 判读 |
| --- | --- | --- |
| 限流次数 / 周期数 | `cat .../cpu.stat` 的 `nr_throttled`/`nr_periods` | 比例 > 5% 就该怀疑 p99 |
| 被冻结的总时长 | `throttled_usec` | 除以请求数 ≈ 每个请求摊到的固定延迟 |
| 排队压力 | `cat .../cpu.pressure` | `some avg10` 高说明有任务在等 CPU |

### `cpu.weight` 与 `cpu.max` 是两件事

| | `cpu.max` | `cpu.weight` |
| --- | --- | --- |
| 语义 | **上限**（绝对） | **份额**（相对同级 cgroup） |
| 取值 | quota/period，或 `max` | 1–10000，默认 100 |
| 竞争时 | 到额度就停，不管别人用多少 | 按权重分，没人抢时可用满 |
| 典型用途 | 防超卖、保证成本上限 | 多租户之间的公平 |
| v1 等价物 | `cpu.cfs_quota_us` / `cpu.cfs_period_us` | `cpu.shares`（默认 1024） |

`cpu.weight` 本机实测（两个任务都绑 cpu0，形成真竞争）：

```
cpu.weight A=1000 B=2000  window=5s
  A usage=0.329 核 (32.9% of 单核)   B usage=0.659 核 (65.9% of 单核)
  A:B = 0.5 : 1   （权重比 1000:2000 = 0.5 : 1）

cpu.weight A=100 B=10000  window=5s
  A usage=0.008 核 (0.8% of 单核)    B usage=0.838 核 (83.8% of 单核)
  A:B = 0.0 : 1   （权重比 100:10000 = 0.0 : 1）
```

0.8% : 83.8% ≈ 1 : 105，理论 1 : 100。**这与 §25.1 那个 nice 19 的 1/345 是同一个机制的两个入口**：nice 改的是任务在 fair 类里的权重，`cpu.weight` 改的是 cgroup 在层级里的权重，最后都折算成同一张相对份额表。

还有一点必须说清：**`cpu.weight` 在没有竞争时完全无效**（§25.2 那个 1:1 的实验）。所以「给核心服务设高权重」只有在同级其他 cgroup 真在抢 CPU 时才有意义；机器空闲时它一个字节的作用都没有。**weight 管分配，max 管上限，两者不互相替代。**

---

## 25.8 load average 的真相：排队长度，不是使用率

### 定义：`nr_running + nr_uninterruptible`

内核每 5 秒（`LOAD_FREQ`）采样一次，把「此刻可运行的任务数 + 处于 `TASK_UNINTERRUPTIBLE` 的任务数」混进三个指数滑动平均：

```
load(t+5s) = load(t) × e^(-5/60) + n × (1 - e^(-5/60))      n = nr_running + nr_uninterruptible
```

三个窗口分别是 1、5、15 分钟，衰减常数 60/300/900 秒。几个能直接算出来的数字：

| 事实 | 数值 | 怎么来的 |
| --- | --- | --- |
| 单次采样的新信息权重 | 约 8% | 1 − e^(−5/60) = 0.0799 |
| 4 任务突发在 1 分钟窗口里第一步只贡献 | 约 0.32 | 4 × 0.0799 |
| 1 分钟 load 的半衰期 | 41.6 s | 60 × ln2 |
| 持续 4 个 runnable 一分钟后的 load1 | 约 2.53 | 4 × (1 − e^(−1)) |
| load5 的半衰期 | 208 s | 300 × ln2 |

**所以 load 是滞后指标，对「刚刚发生的拥塞」几乎没有反应。** 扩容之后 load 还会涨一会儿，缩容之后还会掉一会儿——它记的是过去。

`/proc/loadavg` 的第 4 字段 `16/300` 值得单独看：分子是**当前** runnable 的线程数，分母是总线程数。本机同一时刻 `load1 = 8.70` 而分子是 16、`procs_running` 是 15，三个数字互相都对不上，正是因为一个是 1 分钟平均，两个是瞬时。

### 为什么「load 高、CPU 低」= 一堆进程卡在 IO

`TASK_UNINTERRUPTIBLE`（状态字符 `D`）是「在等内核里的某个东西，连信号都不处理」：块设备 IO、某些驱动、`rwsem` 之类的内核锁。这些任务**不在跑，也不消耗 CPU，但会计入 load**。于是：

```
load 高 + CPU 低 + vmstat 的 b 列 > 0   ⟹   瓶颈在 IO/驱动，不在 CPU
load 高 + CPU 高 + r 列 ≈ 核数          ⟹   真的 CPU 饱和
load 高 + CPU 低 + b 列 = 0             ⟹   看是不是容器里读了整机的 loadavg
```

本机制造了一组真实数据（6 个 `O_DIRECT + fsync` 的 `dd` 同时写）：

```
$ vmstat 1 3
procs -----------memory---------- ---swap-- -----io---- -system-- ------cpu-----
 r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st
20  1      0  72104   4248 469108    0    0   336    34  192  236  1  0 98  0  0
17  2      0  72104   4248 469108    0    0   324 1555456 2848 58180 74 26  0  0  0
18  1      0  72104   4248 469108    0    0    88 1529920 2806 56287 74 26  0  0  0

$ cat /proc/loadavg
13.58 11.13 6.72 19/350 3339841

600 次采样：procs_blocked 最大=4 平均=1.11
```

`b` 列出现 1 和 2、`procs_blocked` 最大到 4，就是那 6 个 `dd` 在 `fsync` 时进入 `D` 的瞬间：**这些瞬间它们对 CPU 的贡献是 0，对 load 的贡献是 1。** 反过来，纯 CPU 的例子（4 个死循环，2 核）是 `r = 15–16`、`b = 0`、`us = 87–89%`：**这才是 CPU 饱和的样子**。此时 load 值本身反而次要，`r / nproc` 才是「超卖几倍」的度量。

### 容器里的 load 是整机的

`loadavg` 是**全局**量，cgroup 不隔离它。容器里 `cat /proc/loadavg` 看到的是宿主机（或至少整个 VM）的负载；K8s 里要靠 lxcfs 才能伪造出容器视角。所以「我的 Pod load 很高」在默认配置下是错的：那是邻居的 load。容器里要看自己，用 `cpu.stat` 的 `usage_usec`、`cpu.pressure`，以及应用自己的队列深度。

---

## 25.9 实测：2 核 KVM 上的五个实验

**环境**：Linux 6.12.113（Debian），2 vCPU（AMD EPYC 7K62，无 SMT：`thread_siblings_list = 0`），HZ=100（`getconf CLK_TCK = 100`）。**前提**：这台机器是共享的，实验时 `procs_running` 常在 13–19（外部租户），所以绝对百分比只有理论值的 1/5 左右，所有结论只看比值。`perf`、`pidstat`、`mpstat`、`sar`、`stress-ng` 在本机全部不存在（`command -v` 只找到 `top`/`ps`），所以全部用 `/proc` 与 `/proc/PID/schedstat` 完成；`taskset`、`nice`、`chrt` 可用。

```c
/* burn.c：跑固定圈数后退出；volatile 防止整段被优化掉 */
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv)
{
    unsigned long long n = (argc > 1) ? strtoull(argv[1], NULL, 10) : 100000000ULL;
    volatile unsigned long long x = 0;
    for (unsigned long long i = 0; i < n; i++)
        x += i ^ (x >> 3);
    printf("done %llu (x=%llu)\n", n, x);
    return 0;
}
```

采样脚本读 `/proc/PID/stat` 的第 14、15 字段（`utime`、`stime`，单位 jiffies）与 `/proc/PID/schedstat` 第 1 字段（runtime，纳秒）：

```bash
#!/bin/bash
# measure.sh <秒> <nice:cpu> ...   cpu 用 - 表示不绑定
T=$1; shift; pids=()
for spec in "$@"; do
  n=${spec%%:*}; c=${spec##*:}
  if [ "$c" = "-" ]; then nice -n "$n" ./burn 40000000000 >/dev/null 2>&1 &
  else nice -n "$n" taskset -c "$c" ./burn 40000000000 >/dev/null 2>&1 & fi
  pids+=($!)
done
sleep 0.5
for p in "${pids[@]}"; do echo "$p $(awk '{print $14+$15}' /proc/$p/stat) \
  $(awk '{print $1}' /proc/$p/schedstat)" >> t0; done
sleep "$T"
for p in "${pids[@]}"; do echo "$p $(awk '{print $14+$15}' /proc/$p/stat) \
  $(awk '{print $1}' /proc/$p/schedstat)" >> t1; done
for p in "${pids[@]}"; do kill -9 "$p" 2>/dev/null; done
paste t0 t1 | awk -v T="$T" '{d=$4-$2; r=$5-$3;
  printf "pid=%s ticks=%4d cpu=%6.2f%%  schedstat=%6.2f%%\n", $1, d, d*100/(T*100), r*100/(T*1e9)}'
```

### 实验①：nice 相同 vs 不同（绑同一核，制造真竞争）

| 配置 | `utime+stime`（tick） | schedstat 份额 | 实测比值 | 理论比值 |
| --- | --- | --- | --- | --- |
| nice 0 : nice 0 | 112 : 111 | 22.38% : 22.44% | 1.003 | 1.000 |
| nice 0 : nice 5 | 86 : 28 | 17.35% : 5.65% | 3.07 | 3.06 |
| nice 0 : nice 10 | 85 : 9 | 17.14% : 1.83% | 9.37 | 9.31 |
| nice 0 : nice 10（复跑） | 96 : 11 | 19.19% : 2.07% | 9.27 | 9.31 |
| nice 0 : 0 : 10（三任务） | 87/88 : 9 | 17.51/17.64 : 1.96 | 17.9 | 18.6 |

三任务的理论值 = (1024+1024)/110 = 18.62，实测 17.9，偏差 4%，主要来自 tick 粒度（nice 10 只有 9 个 tick，±1 tick 就是 ±11%）。**这一组证明了两件事**：`vruntime` 的权重公式是对的；nice 的效应是**连续**的，不是「跨过某条线才生效」。

### 实验②：`taskset` 绑核，总耗时怎么变

两个进程、每个 1.5×10⁹ 圈、总工作量相同，只有绑法不同：

| 绑法 | 第 1 轮 | 第 2 轮 | 第 3 轮 | 最小值 |
| --- | --- | --- | --- | --- |
| 分核（cpu0 / cpu1） | 8.29 s | 10.40 s | 8.90 s | 8.29 s |
| 同核（都 cpu0） | 10.00 s | 12.48 s | 12.57 s | 10.00 s |

方向正确（同核更慢），但只有 1.21 倍。理论是 2 倍，被稀释的原因：**两个核本来就被 15 个外部任务占着**，我们的进程无论怎么绑都只能分到残羹。这也解释了为什么很多人「绑核之后没感觉」——绑核改变的是分配，不改变总量。

### 实验③：`loadavg` 与 `vmstat`

```bash
$ vmstat 1 3
procs -----------memory---------- ---swap-- -----io---- -system-- ------cpu-----
 r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st
20  1      0  72104   4248 469108    0    0   336    34  192  236  1  0 98  0  0
17  2      0  72104   4248 469108    0    0   324 1555456 2848 58180 74 26  0  0  0
18  1      0  72104   4248 469108    0    0    88 1529920 2806 56287 74 26  0  0  0
$ cat /proc/loadavg
13.58 11.13 6.72 19/350 3339841
```

读法：`r=17–20` 说明队列里有 17–20 个可运行任务（我们只有 4 个，其余是外部租户）；`b=1–2` 是 `D` 状态的 `dd`；`us=74%` 是 2 个核的总账（约 1.48 核）。三个视角各说一半，缺一个就会误判。

### 实验④：cgroup 配额

```bash
$ ./cg_test.sh 100000 100000 2 4      # quota=1 核，2 个任务，4 秒
cpu.max=100000/100000 (1.00 核)  tasks=2  window=4.0s
  usage=1.00 核时/秒  (占 100% 单核)   periods=40 throttled=5 (12%)  throttled_ms=8
$ ./cg_test.sh 50000 100000 2 4       # quota=0.5 核
cpu.max=50000/100000 (0.50 核)  tasks=2  window=4.0s
  usage=0.50 核时/秒  (占 50% 单核)   periods=40 throttled=40 (100%)  throttled_ms=2449
```

`usage` 精确等于 quota，误差 < 2%，这是 `cpu.max` 最值得信任的地方：**它是一个硬上限，不受外部负载影响**。也正因如此，它最容易造成「有核用不上」。

### 实验⑤：谁在读核数（`nproc` 与配额不一致）

```
$ cat /sys/fs/cgroup/schedtest/cpu.max
100000 100000
$ nproc
2
$ ./gp                      # Go 1.19.8
NumCPU=2 GOMAXPROCS=2
$ java P                    # OpenJDK 17.0.21
availableProcessors=1
$ echo "200000 100000" > /sys/fs/cgroup/schedtest/cpu.max && java P
availableProcessors=2
```

同一个 cgroup 里，`nproc` 说 2，Go 说 2，Java 说 1。**三者读的不是同一个东西**：`nproc` 读 `sched_getaffinity`（CPU 掩码）；Go 1.19 的 `GOMAXPROCS` 也只读掩码；Java 的 `UseContainerSupport`（JDK 10+）会额外读 cgroup 配额。Go 1.25 起 `GOMAXPROCS` 默认值开始考虑 cgroup 限额（本机装的是 Go 1.19，所以量到 2）。**这就是「配额只有 1 核，程序却起了 8 个 P/8 个线程池」的根因**——不是程序写错，是它问的接口不包含配额信息。

---

## 25.10 三列账：调度承诺了什么、收了多少、从哪开始失效

| 维度 | 承诺了什么 | 为此付了什么 | 从哪里开始失效 |
| --- | --- | --- | --- |
| 公平（fair 类） | 每个 runnable 按权重分 CPU，长期比例 = 权重比 | 每次唤醒/切片的记账（`vruntime`/lag）、周期时钟中断、切换时换栈换页表 | 权重差 10 倍以上时低权重任务被 `min_granularity` 抬升；`nr_running` 大时周期拉长，延迟不可控 |
| 低延迟（EEVDF） | 用 `vdeadline` 保证「该跑的先跑」，睡眠补偿有上界 | `lag`/`vdeadline` 计算、按 deadline 排序的红黑树（O(log n)） | 一旦被 cgroup 限流或绑到忙核，deadline 再早也得等；slice 设得过小则切换开销反噬 |
| 优先级（RT/DEADLINE） | RT 严格抢占；DEADLINE 有准入控制、可证明 | RT 无准入，能饿死一切（含内核线程），只能靠 950/1000 的 throttling 兜底 | prio 99 spinner 绑核 → RCU stall / hung task；RT throttling 的 50 ms/秒缺口毁掉 p99 |
| 亲和性 | 把任务限制在指定 CPU，缓存与 TLB 保持热 | 放弃负载均衡；绑错核（cpu0 的 IRQ）或绑到兄弟核（SMT）收益归零 | 绑死后该核被邻居占满；NUMA 只绑 CPU 不绑内存 |
| cgroup 配额 | 硬上限与相对份额，多租户可隔离 | 周期边界上的整体冻结；嵌套 cgroup 让有效周期叠加 | quota 低于需求 → 每个周期都 throttled；`cpu.weight` 无竞争时完全无效 |
| `load` 作为信号 | 一个跨平台的「队列长度」粗指标 | 5 秒采样 + 1/5/15 分钟平均，天然滞后；含 `D` 状态 | 高 load 低 CPU = IO；容器里读到的是整机；核数不同不可直接比 |

一句话收束：**调度买到的是「按权重分摊 + 可表达的截止时间」，付出的是「记账与切换的固定成本 + 一个必然滞后的观测口径」。** 所有调度事故，本质上都是「你以为你在买优先级，其实你买的是份额」或者「你以为 load 是使用率，其实它是队列」。

---

## 25.11 踩坑清单：现象 → 根因 → 定位 → 修

| 现象 | 根因 | 定位命令 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 容器里起了 8 个 worker，实际只有 1 核配额 | `nproc`/`GOMAXPROCS` 读掩码，不读 `cpu.max` | `cat .../cpu.max`、`java -XshowSettings:system` | 显式设 `GOMAXPROCS`/`-XX:ActiveProcessorCount`，或由配额算并发度 | 发布模板里按配额算，不写死 |
| 绑核后单核 100%，其它核空闲 | N 个 worker 绑到同一个核；或绑到 cpu0（IRQ 集中） | `ps -o psr -L -p PID`、`cat /proc/interrupts` | 分散到不同物理核；避开 cpu0 | 绑核清单写「物理核」，检查 `thread_siblings_list` |
| `load` 高但 CPU 闲，扩容无效 | load 含 `TASK_UNINTERRUPTIBLE` | `vmstat 1` 看 `b` 列；`ps -eo stat,pid,comm \| grep '^D'` | 查块设备/驱动/网络存储；`iostat -x`、`cat /proc/PID/stack` | 告警用 `r/nproc` 与 `cpu.pressure`，不用裸 load |
| `nice -n 10` 之后服务更慢 | nice 是权重；降低权重只让批处理拖更久，占用窗口更长 | `renice` 前后对比 `schedstat` 的 runtime 与完成时间 | 用 cgroup `cpu.max` 限死批处理，`cpuset` 做核隔离 | 离线任务一律进独立 cgroup，不靠 nice |
| p99 周期性毛刺（几十 ms） | cgroup 配额在 period 边界冻结 | `cpu.stat` 的 `nr_throttled`/`nr_periods` | 提高 quota、缩短 period（如 10000/10000）、或拆 cgroup | 监控 `container_cpu_cfs_throttled_periods_total` |
| 整机「假死」，SSH 卡，日志停在 RCU | RT 任务饿死内核线程（`ksoftirqd`、`rcu_preempt`） | `chrt -p`、`dmesg \| grep -i "rt throttling\|rcu.*stall"` | 去掉 RT 或用 DEADLINE（有准入控制）；至少降 prio | 生产禁止 `chrt -f 99` |
| 容器 load 很高，但容器里没几个进程 | `loadavg` 是全局的，不隔离 | 对比 `cat /proc/loadavg` 与 `cpu.stat` 的 `usage_usec` | 用 `cpu.pressure` + 应用队列深度做 SLO | 容器挂 lxcfs，或干脆不采集 load |
| 单线程延迟稳定，多线程反而抖 | 线程数超过核数后进入排队；或跨 NUMA 取内存 | `schedstat` 第 2 字段、`numastat -p PID` | 控制并发 ≤ 物理核；`numactl --cpunodebind --membind` | 压测时把「每核 runnable」作为必看指标 |

---

## 25.12 一页速记

```
【一句话】调度器只在 runnable > 核数 时存在；nice 是权重不是优先级；load 是队列长度不是使用率。

【三个数】
  权重：nice 0 = 1024，每 +1 级 ÷1.25（nice 10 = 110，nice 19 = 15，nice -20 = 88761）
  周期：CFS sched_latency 6 ms / min_granularity 0.75 ms；EEVDF base_slice 1.4 ms（本机实测）
  配额：cpu.max = quota/period，100000/100000 = 1 核，50000/100000 = 0.5 核

【三条公式】
  ① vruntime += delta × 1024/weight   ⟹   CPU 份额 = weight / Σweight
  ② vdeadline = vruntime + slice × 1024/weight  （EEVDF 选 eligible 中最小者）
  ③ lag = 应得 − 已得；lag > 0 是债主，PLACE_LAG 给睡眠唤醒的补偿设上界

【三个不等式（判断该不该管调度）】
  r / nproc > 1 ........................................ 队列已形成
  schedstat.wait / (runtime + wait) 可观 ............... 慢在等 CPU
  cpu.stat nr_throttled / nr_periods > 5% .............. 被配额限流

【三个致命默认值】
  sched_rt_runtime_us 950000 / period 1000000 → RT 每秒只能占 950 ms
  cpu.max 默认 max（不限）→ 但上一层父 cgroup 可能限了
  loadavg 含 D 状态 + 1/5/15 分钟平均 → 既滞后又不纯

【三列账】
  承诺：按权重分摊、截止时间可表达、租户可隔离
  代价：记账与切换、周期边界冻结、滞后观测
  崩点：RT 饿死内核、quota 每周期限流、绑核失衡、NUMA 远端
```

---

## 25.13 自检 3 题

**Q1.** 一个核上只有两个任务，nice 分别是 0 和 10，都在死循环。按 CFS（`sched_latency = 6 ms`、`min_granularity = 0.75 ms`）算：理想份额比是多少？一个周期里各该跑多久？为什么 6.12 上实测是 9.3:1 而不是 7:1？

<details><summary>答案</summary>

权重：nice 0 → 1024，nice 10 → 110，总和 1134。

- 理想份额：1024/1134 = 90.30%，110/1134 = 9.70%，比值 1024/110 = **9.31 : 1**。
- 一个 6 ms 周期内的理想 slice：nice 0 拿 6 × 0.903 = **5.42 ms**，nice 10 拿 6 × 0.097 = **0.58 ms**。
- 0.58 ms < `min_granularity` 0.75 ms，所以在 CFS 上低权重任务会被抬到约 0.75 ms，nice 0 剩 5.25 ms，实际比值退化为 5.25/0.75 = **7 : 1**。

6.12 是 EEVDF，没有 `sched_latency`/`min_granularity` 那套周期，基本单位是 `base_slice_ns`（本机 1.4 ms），权重只体现在 `vdeadline = vruntime + slice × 1024/weight` 上，没有「把 slice 抬到下限」这一步，所以量到 9.27–9.37，与 9.31 一致。**落点：权重公式两代通用，被替换掉的是「周期 + 下限」这套节奏。**
</details>

**Q2.** 一台 8 核机器，`load average = 12`，`top` 显示 CPU 30%，`vmstat` 的 `r = 2`、`b = 9`。瓶颈在哪一层？该看哪三个数？

<details><summary>答案</summary>

`b = 9` 是关键：9 个任务处于 `TASK_UNINTERRUPTIBLE`，在等内核里的东西（多半是块设备 IO，也可能是驱动或内核 `rwsem`），它们贡献 load 但不贡献 CPU 使用率。`r = 2` 说明真正可运行的只有 2 个，8 核完全够用。**瓶颈在 IO 层，不在 CPU 层；扩容只会让 load 的分母变大，问题原地不动。**

三个数：① `vmstat` 的 `b` 列（或 `procs_blocked`），确认是 `D` 而不是 `R`；② `iostat -x 1` 的 `%util`、`await`、`aqu-sz`，确认哪个设备、排队多深；③ `ps -eo stat,pid,comm | grep '^D'` 加 `cat /proc/PID/stack`，确认卡在哪个内核栈。顺带：`load1 = 12` 与 `r + b = 11` 大致对得上，但 load 是 1 分钟平均，两者对不上也不奇怪——这正是 §25.8 那条「滞后」的代价。
</details>

**Q3.** 一个 cgroup 设了 `cpu.max = 100000 100000`，里面有 2 个常驻线程，机器有 4 核。`nproc` 输出几？Go 的 `GOMAXPROCS` 几？两个线程合计的 CPU 上限是多少？为什么平均 CPU 看起来「没打满」但 p99 很差？

<details><summary>答案</summary>

`nproc` 输出 **4**：它读 `sched_getaffinity` 的掩码，而 `cpu.max` 不改变掩码。Go 1.19 的 `GOMAXPROCS` 也是 4；Go 1.25+ 会读 cgroup 限额，输出 1；Java 17 的 `availableProcessors` 会读配额，输出 1。本机实测就是这组数字：`nproc = 2`、Go 1.19 `GOMAXPROCS = 2`、Java `availableProcessors = 1`。

两个线程的 CPU 总和上限是 **1.00 核**（100000 µs / 100000 µs），与机器有 4 核无关。平均「没打满」是因为 1 核 ÷ 4 核 = 25%，从整机视角当然不满；p99 差是因为额度在**每个 100 ms 周期的边界**被整体冻结：两个线程跑满额度后一起停到下个周期，任何跨边界的请求都要多等最多一个周期。`cpu.stat` 的 `nr_throttled / nr_periods` 很高、`throttled_usec` 在涨——这就是证据。
</details>

---

## 25.14 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 线程与调度单元：`clone` 的共享参数、用户态线程与协程（本篇的前提） | → base/24 |
| 进程状态机：`R/S/D/Z` 是怎么来的、`D` 为什么连 `kill -9` 都杀不掉 | → base/23 |
| 缺页时 CPU 到底在干什么：为什么「等内存」也会进 `D` | → base/26 |
| 手搓容器：`cpu.max` / `cpuset` 在 cgroup v2 里怎么被真正用起来 | → base/34 |
| 排障总表：perf / strace / bpftrace 各查哪一层，`r`/`b`/`load` 在表里的位置 | → base/54 |
| 缓存与一致性：绑核为什么能保住 L1/L2、迁移为什么贵 | → base/13 |
| Go 的 GMP 调度器与抢占：OS 线程之上那一层怎么复用 M | → [go/19](../go/19-scheduler-runtime.md) |
| K8s 的 requests/limits、CPU throttling 与发布策略 | → [go/52](../go/52-deployment-sre.md) |
| 全书总纲：四条主线与 56 篇清单 | [0-summary](0-summary.md) |
