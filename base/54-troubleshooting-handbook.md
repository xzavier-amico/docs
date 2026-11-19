# 54. 量级速查与排障手册：perf / strace / tcpdump / bpftrace 各查哪层

> **状态**：`已完成（初稿）` ｜ **所属**：第五部分 · 收口 ｜ **编号**：54 / 56
> **本篇定位**：Part V 的第二篇。53 把一次请求拆成一条链，本篇给这条链装上一把「按层选工具」的尺子：**排障不是猜，是先定位慢在哪一层，再用那一层唯一的工具**。全篇交付三样东西——一张五层 × 四现象的矩阵、一张工具 × 层的对照表、二十条「现象 → 层 → 工具 → 结论」的速查行；最后用一台真机器上的自写慢服务，把三层耗时逐个量出来。
> **读法**：§54.2 与 §54.3 是全篇支点，先建立「层」的概念再看工具；§54.4 是查表用的，不必顺序读；§54.5 的决策树建议打印贴墙；§54.6 的陷阱决定你会不会把工具的输出读反；§54.7–§54.8 是真实环境与完整案例，赶时间可直接读 §54.9 的一页速记。
> **不在本篇**：单条命令的完整选项与发行版差异 → `man 1 strace` 等手册；组件内部（InnoDB、Kafka、vLLM）的排障 → `comp/`；分布式链路的止血与复盘流程 → [eng/51](../eng/51-troubleshooting-handbook.md)；Go 的 pprof / trace 用法 → [go/29](../go/29-pprof-trace.md)；时钟与时间倒流 → `base/55`；一次请求的完整链路 → `base/53`。

---

## 54.1 引子：四小时里，每个人都找到了「一个原因」

某订单查询服务在 14:02 触发 P99 告警：从 80 ms 涨到 4.2 s，QPS 仍是 5000，错误率只有 0.3%。四个人分别给出了四个「原因」，每一个都听起来有证据：

- DBA 说「数据库慢查询」：确实有几条 200 ms 的 SQL；
- 语言侧说「GC」：确实有 12 ms 的停顿；
- 运维说「网络抖动」：确实抓到 0.1% 的重传；
- 值班说「机器负载高」：`load average` 确实到了 14。

四小时后，真正的原因是一行正则：昵称校验写成了 `^(\w+\s?)*$`，某个 38 字符的输入让它发生灾难性回溯。那个线程在用户态烧掉一个核，接口就卡在那里；**数据库、GC、网络、load 全都是真的，但没有一个是这个请求变慢的原因。**

| 时刻 | 谁的猜测 | 查了什么 | 看到什么 | 本可以更早用 |
| --- | --- | --- | --- | --- |
| 14:02 | 告警：P99 4.2 s | 大盘 | QPS 不变、错误率 0.3% | 先确认「慢」不是「错」 |
| 14:10 | DBA：慢查询 | 慢查询日志 | 最慢 200 ms，不足以解释 4 s | `strace -T -e trace=network` 看请求是否卡在 DB |
| 14:50 | 语言侧：GC | GC 日志 | 单次 12 ms，频率正常 | GC 日志与 P99 尖刺对齐 |
| 15:40 | 运维：网络 | `ss -ti` / 抓包 | 重传 0.1%，无队列积压 | 看单条连接的 `cwnd` 与 `Retr` |
| 16:30 | 值班：机器 | `top` / `loadavg` | load 14，但进程只占 1 个核 | 看**本进程**的 CPU 与状态 |
| 17:20 | 有人跑了一次 | `strace -c` | 系统调用时间占比 0.4%，墙钟 4 s | **用户态时间**才是大头 |
| 17:35 | 抓栈 | `gdb -p` + `thread apply all bt` | 线程停在 `regexec` | 有栈就先看栈，不要先猜 |
| 18:00 | 改一行 | 把嵌套量词改成原子组/加长度上限 | P99 回到 90 ms | 用 RE2 或超时兜底 |

这场复盘最贵的不是那行正则，而是**四小时里没有人先问一句「这 4 秒花在哪一层」**。四个猜测分别落在内核、应用、网络、CPU 四层，每个人都在用自己熟悉的那层的工具，于是每个人都找到了「一个真实但不相关」的现象。load 14 是真的，重传 0.1% 也是真的，但它们和这个请求的 4 秒没有因果关系。

> **本篇的唯一真问题**：面对「慢 / 丢 / 崩 / 泄漏」，先把它定位到**位层、CPU、内核、网络、应用**中的哪一层，再用那一层唯一的证据形式去证实或证伪。工具不是越多越好；**一个层只需要一种证据**。

---

## 54.2 分层排障法：五层不是 OSI，是「五种证据」

这里的五层不是网络七层模型，而是按「你能拿到什么证据」划的：

| 层 | 它承诺什么 | 你能直接看到的量 | 越界的第一信号 | 首选证据 |
| --- | --- | --- | --- | --- |
| **位层**（表示层） | 同样的字节在同样的约定下解释成同样的值 | `xxd` 的字节、`objdump` 的指令、`printf("%a")` 的位模式 | 值突然变号、变极大、出现替换字符 | 位模式与边界值 |
| **CPU 层** | 指令按序退休，周期可数 | IPC、cache miss、branch miss、频率、每指令周期 | 同样指令数变慢、核闲着但活没干完 | 周期与事件计数 |
| **内核层** | 系统调用、调度、内存、IO 有明确语义 | syscall 时间、runqueue、缺页、D 状态、fd 数 | 墙钟远大于 CPU；`sys` 高；队列增长 | 系统调用账与队列长度 |
| **网络层** | 包能到、序能对、窗口能滑 | 重传、`cwnd`、`Recv-Q`/`Send-Q`、RTT、状态机 | 带宽上不去、连接堆积、RST | 包与 socket 状态 |
| **应用层** | 一次请求在预算内完成 | 调用栈、锁等待、GC、对象数、队列深度 | 同一函数越跑越慢；重试放大 | 栈与对象账 |

**四类现象其实是同一条队列的四种读数**：

- **慢**：请求在某一层排队或停留太久；
- **丢**：请求/包/事件没有进入下一层（队列满、校验失败、被丢弃）；
- **崩**：某一层的约定被违反（非法指令、空指针、OOM、证书校验失败）；
- **泄漏**：该释放的没有释放（fd、内存、连接、goroutine）。

所以「先分层」不是分类游戏，而是**先确定该看哪一个队列**。CPU 层的队列是 runqueue，内核层是各种 wait queue 与 backlog，网络层是 socket 缓冲区与网卡环形缓冲，应用层是线程池/连接池/消息队列。

### 5 × 4 矩阵：现象落在哪一格

| 层 | 慢 | 丢 | 崩 | 泄漏 |
| --- | --- | --- | --- | --- |
| **位层** | 精度不够导致反复重算；字符串按字节拼接膨胀 | 截断、溢出、UTF-8 被切开 | 对齐错误、UB、非法位模式 | 位图/引用计数回绕，资源永不释放 |
| **CPU** | IPC 低、cache miss、分支预测失败、伪共享 | 采样丢事件、降频丢周期、SIMD 精度丢失 | 除零、浮点异常、非法指令 | 自旋/忙等长期占核 |
| **内核** | runqueue 排队、缺页、IO wait、`fsync` 慢 | backlog 溢出、fd 队列满、丢中断 | OOM kill、panic、段错误 | fd、slab、页缓存、僵尸进程 |
| **网络** | RTT 高、重传、窗口小、队头阻塞 | 丢包、MTU/分片、accept 队列溢出 | RST、端口耗尽、证书/握手失败 | `TIME_WAIT`、`CLOSE_WAIT`、半开连接 |
| **应用** | 锁竞争、GC、正则回溯、序列化、N+1 | 队列丢消息、采样丢失、日志丢失 | panic、栈溢出、未捕获异常 | goroutine、连接池、缓存、监听器 |

**用法**：先填「现象」列，只填一格；如果填不出，说明你还没有现象，只有情绪。填出格子后，表里那格的工具就是第一顺位；**不要跨格找证据**——用 `tcpdump` 去解释用户态 CPU 高，和用 `top` 去解释证书错误一样，都是把「真实但不相关」当成原因。

---

## 54.3 工具 × 层 对照表：每个工具只能回答一个问题

| 工具 | 主查层 | 能给你什么 | 不能给你什么 | 需要什么权限 | 性能影响 |
| --- | --- | --- | --- | --- | --- |
| `objdump` / `readelf` | 位层 + CPU | 指令、地址、节区、重定位、调用目标 | 运行时值、哪个分支真的走了 | 读文件即可 | **零**（不运行程序） |
| `perf` | CPU + 内核 | 采样栈、IPC、cache/branch 事件、调度延迟 | 精确的逐次调用（采样有偏差）、加密内容 | `perf_event_paranoid` ≤ 1；内核符号另需权限 | 通常 1%–5%，高频事件可更高 |
| `strace` | 内核边界 | 每次 syscall 的参数、返回值、耗时；`-c` 汇总 | 用户态内部在干什么；锁的自旋；cache miss | `ptrace`（同 uid 或 `CAP_SYS_PTRACE`） | syscall 密集时 **10×–100×**；稀疏时接近 1% |
| `ltrace` | 应用/库边界 | PLT 库调用、参数、返回值 | syscall、内联函数、静态链接内部 | `ptrace` + 动态库符号 | 通常 10× 以上，且常因符号缺失而失真 |
| `bpftrace` | 内核 + 应用 | 任意内核/用户探针、聚合、延迟直方图 | 无探针处的历史数据；需要 BTF/头文件 | root 或 `CAP_BPF`/`CAP_PERFMON` | 设计上低，但每事件都挂探针会很高 |
| `tcpdump` | 网络 | 原始包、握手/挥手、重传、分片、时序 | 加密后的应用语义；进程内部的等待 | `CAP_NET_RAW`（通常 root） | 中等；高 PPS 下会丢包，抓包本身会改时序 |
| `ss` | 网络 + 内核 | socket 状态、`Recv-Q`/`Send-Q`、`cwnd`、RTT、重传 | 包内容、历史趋势 | 读本 netns 即可，无特殊权限 | 可忽略（netlink 查询） |
| `/proc`（procfs） | 内核 | 进程状态、RSS、fd、`smaps`、中断、softirq、PSI | 历史；跨进程因果 | 同 uid 或 root；`hidepid` 会隐藏 | 多数可忽略；`smaps` 大进程可达 ms 级 |
| `vmstat` | 内核 | runqueue、swap、IO、上下文切换、CPU 分解 | 哪个进程、哪个函数 | 无 | 可忽略（采样） |
| `sar` | 内核（历史） | 过去某时刻的 CPU/IO/网络/内存趋势 | 没提前开 `sysstat` 就没有数据 | 读 `/var/log/sa` | 采集时很低 |
| `iostat` | 内核（块层） | 设备 `await`、`%util`、吞吐、队列 | 文件系统层、应用层归因 | 无 | 可忽略 |
| `free` | 内核（内存） | `available`、page cache、swap 使用 | 谁在持有、是否泄漏 | 无 | 可忽略 |
| `top` / `ps` | CPU + 应用 | 进程/线程的 CPU、RSS、状态、累计时间 | syscall、锁、cache 事件 | 无 | 很小（刷新间隔采样） |
| `dmesg` | 内核（崩） | OOM、段错误、驱动、hung task、RCU stall | 用户态逻辑；默认环形缓冲会丢早期日志 | `kernel.dmesg_restrict=0` 或 root | 零（事后读） |

三条使用纪律：

1. **先静态后动态**：能 `objdump`/`readelf` 看出的问题，不要先上 `strace`；能读配置与代码看出的问题，不要先抓包。
2. **先边界后内部**：先量「跨层花了多少」（syscall、包、队列），再量层内细节。边界账对不上，层内工具只会给你更多噪声。
3. **先计数后采样**：`strace -c`、`ss -s`、`/proc/net/softnet_stat` 是计数，采样工具（`perf`、`top`）是估计；能用计数定案时不要用采样猜。

```bash
# 边界账：先看每个 syscall 的墙钟（-w 才是墙钟，见 §54.6）
strace -f -c -w -o /tmp/sys.txt ./server
# 网络账：不看内容，只看队列、窗口、重传
ss -tin state established
# 内核丢包账：每个 CPU 一行，第 2 列是 dropped
awk '{print $1, $2, $3}' /proc/net/softnet_stat
```

> **「唯一」的含义**：不是只能用一个二进制，而是这一层只有一种**判决性证据**。位层判决是位模式，CPU 层判决是周期/事件，内核层判决是 syscall 与队列，网络层判决是包与状态机，应用层判决是栈与对象。拿不到判决性证据时，说明你还没定位到层。

---

## 54.4 二十条「现象 → 层 → 工具 → 结论」

下表每行都可以独立使用。**判据**一栏写的是「看到这个就定案」的最小条件，不是全部解释；不满足判据时，回到 §54.5 的决策树，不要硬套。

| # | 现象 | 先看哪层 | 工具与字段 | 一句话判据（结论） |
| --- | --- | --- | --- | --- |
| 1 | CPU 高但 QPS 低 | CPU / 应用 | `top -H`、`perf top`、`pidstat -u 1` | `user` 高、`sys` 低、syscall/s 不变 → 用户态自旋、GC 或正则回溯 |
| 2 | load 高但 CPU 闲 | 内核 | `vmstat 1` 的 `r`/`b`、`ps -eo stat` | `b > 0` 且 `wa` 高、进程多在 `D` → 不可中断睡眠（IO/驱动/锁） |
| 3 | load 高、CPU 也高，吞吐不涨 | CPU / 内核 | `perf stat -e cycles,instructions`、`vmstat` 的 `cs` | IPC < 1 且 `cs` 暴涨 → 上下文切换或内存墙，不是「算力不够」 |
| 4 | load 低但 P99 高 | 应用 / 网络 | `ss -lnt` 的 `Recv-Q`、pprof、连接池指标 | 队列有积压而 CPU 不高 → 下游、锁或连接池上限 |
| 5 | RSS 不降 | 内核 | `cat /proc/PID/smaps_rollup`、`free -m` | `Anonymous` 不降 = 堆/arena；`file` 高 = page cache，可回收 |
| 6 | RSS 稳定阶梯上涨 | 应用 | heap profile、`pmap -x` 的段变化 | GC/释放后仍阶梯上升，且斜率与请求量成正比 → 持有引用 |
| 7 | 短命进程多、`sys` 高 | 内核 | `vmstat 1` 的 `cs`/`in`、`pidstat -w` | `cs` 与进程创建率同步 → fork/exec 或线程池反复创建 |
| 8 | 连接堆积、端口耗尽 | 网络 | `ss -tan state close-wait`、`ss -s` | `CLOSE_WAIT` 涨 = 应用没 `close`；`TIME_WAIT` 涨 = 主动关闭多 |
| 9 | 带宽上不去、单流慢 | 网络 | `ss -ti` 的 `cwnd`/`retrans`、`tcpdump -w` | `cwnd` 小且重传高 = 拥塞/丢包；`Send-Q` 满 = 应用不写 |
| 10 | 延迟周期性尖刺 | 内核 / 应用 | `perf sched`、GC 日志、`/proc/pressure` | 尖刺与 GC、THP、缺页或重传周期对齐 → 不是「随机抖动」 |
| 11 | 磁盘 IO 高但业务不写 | 内核 | `iostat -x 1`、`/proc/diskstats` | `await` 高且 `%util` 高 = 设备饱和；写量与应用不符 = 回刷或邻居 |
| 12 | `fsync` 慢 | 内核 / 器件 | `strace -T -e trace=fsync` | 单次 `fsync` > 1 ms 且 CPU 不高 → 日志/器件/写缓存，不是代码 |
| 13 | 系统调用异常多 | 内核 | `strace -c -w` | 单请求 syscall > 1000，或 99% 墙钟集中在 1–2 个 syscall |
| 14 | 越跑越慢，重启就好 | 应用 | pprof、heap、`/proc/PID/status` 的 `Threads` | 数据结构/线程/连接数与延迟同斜率 → 无界增长 |
| 15 | 偶发错误、日志无异常 | 位层 / 网络 | `xxd`、`tcpdump`、校验和 | 只出现在边界长度、特定字段或特定包长 → 表示/分片问题 |
| 16 | 容器内存与宿主对不上 | 内核 | `memory.current`、`memory.stat`、`/proc/meminfo` | RSS 只是 cgroup 的子集；page cache 计入 cgroup，不计入进程 RSS |
| 17 | GC 频繁、CPU 被吃掉 | 应用 | GC 日志、`pprof` | GC CPU > 10%，或分配速率长期高于回收速率 → 分配风暴 |
| 18 | 网卡无错但丢包 | 内核 | `/proc/net/softnet_stat` 的 `dropped` | `dropped` 列增长 → backlog/环形缓冲溢出，不是网线坏 |
| 19 | 句柄耗尽 | 内核 | `ls /proc/PID/fd`（数条目）、`ulimit -n` | fd 数贴近上限，且集中在 socket/文件 → 泄漏或池太小 |
| 20 | 日志时间对不上、超时乱 | 时钟 / 应用 | `clock_gettime`、`ntpq -p`、`dmesg` | `MONOTONIC` 与 `REALTIME` 漂移不一致 → 时钟问题，不是业务超时 |

几条容易被忽略的判据展开：

- **第 8 行**：本机实测 `ss -s` 给出 `TCP: 3261 (estab 2188, timewait 1046)`，`/proc/net/sockstat` 给出 `inuse 2183, tw 1046`，而 `ss -tan` 按状态统计是 `ESTAB 1842 / TIME-WAIT 700 / FIN-WAIT-2 346 / CLOSE-WAIT 346`。三个数字不同不是工具坏了：`ss -s` 的 `TCP:` 行是 TCP 控制块计数（含未建立完成的），`sockstat` 的 `inuse` 是已分配 socket，`ss -tan` 只列当前状态可枚举的项。**判据要用同一个口径**，否则「连接数对不上」会变成第二个假问题。
- **第 9 行**：`Send-Q` 满而 `cwnd` 正常，说明瓶颈在应用不写；`Send-Q` 不大但 `cwnd` 卡住，才是网络。两者都看，才能把「网络慢」与「应用慢」分开。
- **第 12 行**：`fsync` 的耗时是**器件与日志层**的证据，不是 CPU 的证据；把它拿去和 `perf` 的指令数对账，永远对不上。
- **第 18 行**：`softnet_stat` 每行对应一个 CPU，第 2 列是 `dropped`。它增长时，`tcpdump` 可能一个丢包都抓不到——因为包在进入协议栈之前就被丢了。

---

## 54.5 排查顺序：四个门，一次只过一个

排障最怕「并行猜」。正确顺序是四个串联的门，**每一门都有明确的出口条件**；出口条件不满足，不许进入下一门。

```
故障（慢 / 丢 / 崩 / 泄漏）
│
├─ 门 1：是否可复现？
│   ├─ 否 → 固化现场：版本、时间窗、输入、指标、3 次样本；提高采样率
│   └─ 是 → 缩到最小：单请求 / 单连接 / 单文件 / 单条 SQL
│
├─ 门 2：资源饱和，还是逻辑问题？
│   ├─ 饱和 → 找队列：CPU runqueue / 内存 / IO await / 连接池 / backlog
│   └─ 不饱和 → 逻辑：锁、算法、重试、序列化、正则、GC
│
├─ 门 3：哪一层？
│   ├─ 墙钟 ≫ CPU，syscall 少     → 阻塞（内核/网络/下游）
│   ├─ CPU ≈ 墙钟，syscall 少     → CPU/应用（cache、分支、锁自旋、GC）
│   ├─ syscall 时间占大头         → 内核边界（IO、fsync、syscall 太多）
│   └─ 包/连接状态异常            → 网络层
│
└─ 门 4：哪个函数 / 哪条语句？
    ├─ 有符号与栈 → perf / pprof / gdb
    ├─ 无符号       → strace -T 的 syscall 边界 + 代码内分段计时
    └─ 无工具       → 二分注释 / 开关实验 / 最小复现
```

四个门的出口条件：

| 门 | 出口条件 | 不满足时最常见的错误 |
| --- | --- | --- |
| 1 可复现 | 能在同一输入/同一窗口重复出同一条曲线 | 拿一次偶发当结论，改完「好像好了」 |
| 2 资源/逻辑 | 能说出是哪个队列满，或哪个操作不饱和却慢 | 把「CPU 不高」当成「没问题」 |
| 3 哪一层 | 该层有判决性证据（§54.3 末） | 用相邻层的工具解释，例如用 `top` 解释证书 |
| 4 哪个函数 | 能指出一行代码/一个 syscall/一个包 | 停在「大概是数据库」这种不可执行的结论 |

两个补充规则：

1. **对照实验优先于更多工具**：同一份输入，只改一个变量（关掉正则、把锁换成局部、把随机访问换成顺序），看墙钟是否按预期移动。**能用一个开关证伪的假设，不要用三个工具去支持**。
2. **先止血后归因**：线上先限流/回滚/摘流量，再慢慢查。止血不属于本篇，属于 [eng/51](../eng/51-troubleshooting-handbook.md)；但止血前至少留下三样证据：当时的 `top`/`ss`/`vmstat` 快照、进程栈、以及故障时间窗。

---

## 54.6 采样与观测的陷阱：工具会改变你要测的东西

### 陷阱 1：`strace` 让 syscall 密集的程序慢 10–100 倍

同一段「调用 `getppid` 20,000 次」的代码，在本机实测：

```bash
$ bash -c 'time ./slow syscall 20000'
real    0m0.023s
$ bash -c 'time strace -c -o /dev/null ./slow syscall 20000'
real    0m1.788s
```

**77.7 倍**。原因不是 `getppid` 变慢，而是每次 syscall 都要停两次（进入/退出各一次 `ptrace` 停），再加格式化与写文件。对 syscall 稀疏的程序则几乎无感：`sleep` 模式每请求 20 ms，裸跑 20.61 ms、`strace` 下 20.83 ms，差约 **1%**。所以「`strace` 一定慢 10 倍」是错的；正确的说法是「**`strace` 的代价与 syscall 密度成正比**」。

`strace -c` 的汇总还会骗人：

```bash
$ strace -c ./slow sleep 5        # 默认按 CPU 时间
  0.00    0.000000      0    5    clock_nanosleep
$ strace -c -w ./slow sleep 5     # -w = 墙钟
 99.20    0.103838  20767    5    clock_nanosleep
```

**同一次睡眠，默认显示 0.00 秒，`-w` 显示 0.1038 秒。** 如果你用 `-c` 判断「时间花在哪个 syscall」，阻塞型调用会被你读成「不花时间」。判据：**只要关心等待，就必须加 `-w`**；只关心 CPU 消耗，才用默认。

### 陷阱 2：`-f` 的汇总是各线程之和，可能大于墙钟

4 线程抢锁的实测：

```bash
$ strace -f -c -w -o lk.txt ./slow lock 4
$ grep -E 'futex|total' lk.txt
 99.43    3.650091   1027   3551  1134 futex
100.00    3.671132   1011   3629  1135 total
```

而同一进程的墙钟只有 **1.201 s**。`futex` 的 3.65 s 是四个线程各自等待时间的**总和**，不是墙钟。`-f` 的输出只能看「谁在等、等了多少次」，不能把秒数相加当请求延迟。

### 陷阱 3：`vmstat` 第一行是「自开机以来」，不是现在

本机真实输出（`vmstat 1 3`）：

```
 r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st
21  1      0 128860   1780 433732    0    0   336    33  192  236  1  0 98  0  0
15  1      0 128860   1780 433732    0    0  2644  3316 5585 46350 81 19  0  0  0
16  1      0 128860   1780 433732    0    0  1584     0 6387 68830 80 20  0  0  0
```

第一行 `id=98`，后两行 `id=0`。**第一行是开机以来的平均值**，不是第一个采样间隔。只跑 `vmstat 1 1` 的人会得出「CPU 很闲」的相反结论。同理，`top` 的第一屏是自启动以来的累计值，按一次刷新后才是区间值。

### 陷阱 4：`/proc/PID/stat` 的时间粒度是 10 ms

`getconf CLK_TCK` 在本机是 **100**，即 `utime`/`stime` 的 1 个 tick = 10 ms。一个只烧 3 ms CPU 的请求，在 `/proc` 里可能显示 `utime=0`；这不是「没花 CPU」，是**计数器分辨率不够**。要看短请求，用 `getrusage(RUSAGE_THREAD)`（微秒）或 `perf`，不要拿 `/proc` 的 tick 去算 P99。

### 陷阱 5：`load average` 包含不可中断睡眠

`loadavg` 统计的是 `R`（可运行/运行中）+ `D`（不可中断睡眠）的任务数，不只是「在跑的任务」。本机 `load 14`、`nproc 2`，但 `top` 头部没有哪个业务进程把两个核吃满；`vmstat` 的 `b` 列有 1，说明有一部分是 `D` 状态。**load 高 + CPU 闲 = 先看 `D` 状态与 IO，而不是加 CPU**。反过来，load 低也不代表没排队：cgroup 配额、连接池、锁队列都不进 loadavg。

### 陷阱 6：`%CPU` 是累计值还是瞬时值？

- `ps -o %cpu`（以及 `ps aux` 的 `%CPU`）是**进程生命周期内的平均**：一个刚启动就烧满的进程会显示接近 100%，跑了一小时后会降到 2%；
- `top` 的**第一屏是自启动以来的累计值**，按一次刷新后才是刷新间隔内的增量；线程级可按 `H` 切换，但仍受刷新窗口影响；
- 两者都不是「这个请求花了多少 CPU」。请求级 CPU 只能靠 `getrusage`、`perf` 或代码内计时。

### 陷阱 7：容器里的 `/proc` 与 `ss` 可能看到的是宿主

本机实测：

```bash
$ readlink /proc/self/ns/net ; readlink /proc/1/ns/net
net:[4026531840]
net:[4026531840]
$ readlink /proc/self/ns/pid ; readlink /proc/1/ns/pid
pid:[4026531836]
pid:[4026531836]
```

两个命名空间都与 PID 1 相同，而本机 PID 1 是宿主 `systemd`（`ps -p 1`：`systemd`，已运行 16 天），说明这个「容器」共享了宿主的 net/pid 命名空间。于是 `ss -s` 的 TCP 计数是 3261，`ss -tan` 里还有 346 条 `CLOSE_WAIT`，而我们的测试程序一条连接都没建。**先 `readlink /proc/self/ns/*` 确认视角，再解释计数**；否则你会把邻居的连接算到自己头上。

### 陷阱 8：采样偏差与「丢样本」

`perf` 是采样：热点函数可能因为采样周期恰好错过而被低估；短命进程可能在两次采样之间结束。`/proc` 轮询是 1 s 一次，1 s 内的 200 ms 尖刺会被平均掉。`tcpdump` 在高 PPS 下会丢包，抓包本身还会改变时序。判据：**任何「没看到」都要先问「采样率够不够、有没有丢样本」**；`perf` 要看 `lost` 计数，`tcpdump` 要看内核 drop 计数，`/proc` 轮询要先把间隔降到小于故障周期。

---

## 54.7 本机实测：一台 2 核、load 14 的共享机

以下输出全部来自本次写作环境，未做删改（只合并了重复行）。

```bash
$ uname -a
Linux LegendVPS-20260712140708acd8 6.12.113+deb12-cloud-amd64 #1 SMP \
PREEMPT_DYNAMIC Debian 6.12.113-1~deb12u1 (2026-10-18) x86_64 GNU/Linux

$ cat /proc/loadavg
14.00 10.81 6.40 14/362 3338282

$ nproc
2

$ grep -m1 'model name' /proc/cpuinfo
model name : AMD EPYC 7K62 48-Core Processor

$ vmstat 1 3
 r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st
21  1      0 128860   1780 433732    0    0   336    33  192  236  1  0 98  0  0
15  1      0 128860   1780 433732    0    0  2644  3316 5585 46350 81 19  0  0  0
16  1      0 128860   1780 433732    0    0  1584     0 6387 68830 80 20  0  0  0
```

```bash
$ free -m
               total        used        free      shared  buff/cache   available
Mem:            1974        1567         114           1         431         406
Swap:              0           0           0

$ head -6 /proc/meminfo
MemTotal:        2021692 kB
MemFree:          117012 kB
MemAvailable:     416160 kB
Buffers:            1792 kB
Cached:            421796 kB
SwapCached:             0 kB

$ head -4 /proc/interrupts
           CPU0       CPU1
  1:          9          0   IO-APIC   1-edge      i8042
  8:          0          0   IO-APIC   8-edge      rtc0
  9:          0          0   IO-APIC   9-fasteoi   acpi

$ ulimit -a | head -6
real-time non-blocking time  (microseconds, -R) unlimited
core file size              (blocks, -c) 0
data seg size               (kbytes, -d) unlimited
scheduling priority                 (-e) 0
file size                   (blocks, -f) unlimited
pending signals                     (-i) 7810
```

关键读数与解释：

| 读数 | 本机值 | 说明 |
| --- | --- | --- |
| CPU 核数 | 2（`nproc`） | `load 14` 意味着平均每核 7 个任务在排队或等 IO |
| load | 14.00 / 10.81 / 6.40 | 1/5/15 分钟都在涨，说明不是瞬时尖刺 |
| runqueue | `r=15..21`、`b=1` | 有大量可运行任务，另有不可中断睡眠 |
| CPU 分解 | `us 81% / sy 19% / id 0%`（后两行） | 第一行 `id=98` 是开机以来均值，见 §54.6 |
| 内存 | total 1974 MB、available 406 MB、swap 0 | 无 swap，内存压力直接表现为回收/OOM |
| PSI | `cpu some avg10=100.00` | 过去 10 s 内 100% 的时间有任务因 CPU 而停顿 |
| 时钟源 | `kvm-clock` | 虚拟机；`CLOCK_REALTIME` 可能被宿主调整（→ `base/55`） |

### 本机缺哪些工具

`which` 的结果比任何教程都重要：

| 工具 | 本机 | 缺了它，替代路径 |
| --- | --- | --- |
| `strace` | **有**（6.1） | — |
| `ss` | 有 | — |
| `vmstat` / `free` / `top` | 有 | — |
| `objdump` / `gcc` | 有 | — |
| `perf` | **缺** | `/proc/PID/stat`、`getrusage`、`strace -c -w`、代码内分段计时 |
| `tcpdump` | **缺** | `ss -ti`、`/proc/net/tcp`、`/proc/net/softnet_stat` |
| `ltrace` | **缺** | `strace` 看 syscall 边界；符号表 `nm -D` |
| `bpftrace` | **缺** | 无动态探针；退回 `/proc` 计数与本地日志 |
| `sar` / `iostat` | **缺** | `/proc/diskstats`、`/proc/stat`、自己按秒采样 |
| `perf` 权限 | `perf_event_paranoid=3` | 即使装了 `perf`，非特权也拿不到事件 |

这台机器恰好有 `strace`，所以 §54.8 的案例能走到 syscall 层；`perf` 与 `tcpdump` 缺失，正好演示「**工具不全时，判据仍然成立**」：用 `/proc` 状态、`getrusage` 与代码内计时，仍能把三层拆开。**先确认工具存不存在、权限够不够，再决定排查路径**，这本身就是排障的第一步。

> 这台机器 load 14、PSI CPU 100%，所以下面所有绝对值都偏大；案例里用**同一台机器上的前后对比**和**进程内部分段计时**，不用跨机器的绝对阈值。

---

## 54.8 完整案例：一个自写「慢服务」的三层归因

### 场景与代码

按 `base/53` 的链路法，把一个请求拆成三段：**等待（睡）→ 同步（锁）→ 访存（cache/TLB）**，再逐段问「这 28 ms 由谁承接」。写一个最小服务，每个「请求」做三件事，每件都故意慢：

```c
/* 每个请求：睡 20ms → 抢一把有竞争的锁 → 在 64MiB 环上随机走 20000 步 */
static void serve_once(int r) {
    usleep(20000);                                  /* ① 阻塞：模拟下游/定时等待 */

    pthread_mutex_lock(&mu);                        /* ② 锁：另一线程每 2.5ms 持锁 2ms */
    pthread_mutex_unlock(&mu);

    uint32_t p = (uint32_t)(r * 7919u);
    for (int i = 0; i < 20000; i++) p = ring[p];    /* ③ cache/TLB miss */
}
```

另外准备一个「修复版」`fast`：不睡、不抢锁、把 20000 步改成顺序访问。三次运行的真实输出：

```
serve: setup=1.32s n=10 平均每请求 sleep=21.47ms lock=1.39ms cache=7.57ms 合计=30.42ms
serve: setup=1.55s n=10 平均每请求 sleep=22.02ms lock=0.51ms cache=5.56ms 合计=28.09ms
serve: setup=1.63s n=10 平均每请求 sleep=21.14ms lock=0.69ms cache=8.78ms 合计=30.62ms
fast:  n=10 wall=0.000s 每请求 0.019ms（仅访存段）
```

### 第 1 步：先看总账，判断「资源还是逻辑」

- 每请求约 **28–31 ms**，其中 `sleep` 约 21 ms、锁约 0.5–1.4 ms、访存约 5.6–8.8 ms；
- 进程整体 `user` 约 0.48 s、`sys` 约 0.06 s（10 个请求，含 1.3–1.6 s 的一次性建环）；
- 系统 load 14，但本进程不是「抢不到 CPU」：`sleep` 期间它根本不在 runqueue。

**门 2 的结论**：不是 CPU 饱和，是**一个阻塞 + 一个锁等待 + 一段内存延迟**。如果只看 load，会误判为「机器太忙」——这正是 §54.1 的翻版。

### 第 2 步：`strace -f -c -w` 定位 syscall 边界

```
$ strace -f -c -w -o sv.txt ./slow serve 10
$ grep -E 'nanosleep|futex|total' sv.txt
 96.28    0.501241   2409   208   clock_nanosleep
  2.26    0.011777    420    28   futex
100.00    0.520623   1826   285   total
```

读法（注意 §54.6 的陷阱）：这是 `-f` 的**多线程之和**，所以 0.52 s 大于主线程的 0.30 s——锁线程自己也在睡。它能定案的是**比例与对象**：

- `clock_nanosleep` 占 96%，说明绝大部分墙钟在**主动睡眠**，不是在算；
- `futex` 只有 28 次、0.012 s，说明锁**不是**主因（但单次等待可达 2 ms）；
- **5.6–8.8 ms 的访存完全不出现**：`strace` 只看 syscall，用户态内存延迟对它不可见。

### 第 3 步：逐层取证

| 嫌疑 | 工具/字段 | 实测 | 判决 |
| --- | --- | --- | --- |
| 睡眠 | `/proc/PID/status` 的 `State`、`/proc/PID/wchan` | `S (sleeping)`、`hrtimer_nanosleep`；CPU 占比 0.1% | 确认：阻塞在定时器，不是 CPU |
| 锁 | `strace -f -c -w` 的 `futex`、`getrusage` 的 CPU/墙钟 | 4 线程 200k 次：墙钟 0.49–1.9 s、CPU 0.40–0.42 s，CPU/墙钟 0.21–0.81 | 确认：有等待，但总量小 |
| 访存 | 64 MiB 随机追逐 vs 顺序扫描 | 随机 **486–572 ns/步**，顺序 **1.2–1.7 ns/元素**，差约 350× | 确认：cache/TLB miss 是第三块 |
| 启动建环 | 代码内计时 | **1.32–1.63 s**（一次性） | 不是请求延迟，别混进 P99 |

访存这一行值得展开：64 MiB 工作集远超 LLC，且 4 KiB 页意味着 16,384 个页表项，随机走一步常常是「TLB miss → 页表 walk → DRAM」三件事叠加。同一台机器上 `cache` 模式的 `/proc/PID/stat` 显示 `minflt=16485`（首次触碰 64 MiB 的缺页），状态为 `R`；而 `sleep` 模式 `minflt=105`、状态 `S`。**「R + 高 minflt + 几乎没有 syscall」就是内存层的指纹。**

### 第 4 步：归因与修复（前后两列数字）

| 原因 | 归属层 | 证据 | 修复 | 改前 | 改后（实测/折算） |
| --- | --- | --- | --- | --- | --- |
| 每个请求睡 20 ms | 内核/应用（阻塞） | `State=S`、`wchan=hrtimer_nanosleep`、CPU 0.1% | 事件驱动/批量合并，去掉固定等待 | 21.1–22.0 ms | 设计目标 < 0.1 ms |
| 抢一把有竞争的锁 | 应用（锁） | `futex` 28 次、CPU/墙钟 0.21–0.81 | 分片锁/每线程队列/无锁批处理 | 0.5–1.4 ms | 设计目标 < 0.05 ms |
| 64 MiB 随机访存 | CPU/内存 | 486–572 ns/步 vs 1.2–1.7 ns/元素 | 连续布局、缩小工作集、预取/批量 | 5.6–8.8 ms | **0.019 ms**（顺序版实测，仅访存段） |
| 建环 1.3–1.6 s | 应用（启动） | 代码内计时 | 启动时惰性构建/文件映射/预热 | 1.3–1.6 s | 与请求 P99 无关，单列 |

最后一行是排障里最常见的二次误判：**把一次性启动成本算进请求延迟**。如果监控只报「进程启动后第一分钟很慢」，那不是请求路径的问题，是预热问题。

### 第 5 步：这个案例证明了什么

1. **同一个 28 ms，三个层各有各的证据**：`sleep` 只能从进程状态看，锁只能从 `futex` 与 CPU/墙钟比看，访存只能从「无 syscall 但 CPU 高 + 微基准」看。**没有任何一个工具能同时给出三个答案。**
2. **工具缺失不阻断判据**：本机没有 `perf`，但有 `strace`、`/proc`、`getrusage`；访存层用「同机微基准 + 代码内计时」补上。判据是「这一层有没有判决性证据」，不是「有没有装某个软件」。
3. **观测者效应要主动检查**：`strace` 下 `serve` 的 `sleep` 仍是 20 ms 级（syscall 稀疏），但 `getppid` 循环慢了 77 倍。**同一个工具，对不同程序的影响可以差两个数量级。**

---

## 54.9 三列账 + 一页速记

| 维度 | 承诺了什么 | 为此付了什么 | 从哪里开始失效 |
| --- | --- | --- | --- |
| 分层 | 任何「慢/丢/崩/泄漏」都能落到一层 | 必须先做分类，不能直接改代码 | 现象跨层（如「慢 + 丢」）时，需要按队列拆成两个问题 |
| 工具 | 每层有一种判决性证据 | 工具各有权限、开销与盲区 | `perf` 缺权限、`tcpdump` 丢包、`strace` 改变时序 |
| 采样 | 用少量样本推断整体 | 采样偏差、周期错配、丢样本 | 故障短于采样间隔，或热点被采样错过 |
| 计数 | `/proc`、`ss`、`softnet_stat` 给确定数 | 只有当下，没有历史；口径可能不同 | 容器共享 netns、`hidepid`、计数器回绕 |
| 归因 | 「现象 → 层 → 工具 → 结论」可复述 | 每次都要重新验证，不能套模板 | 把「相关」当「因果」，把邻居的负载当自己的根因 |

```
【一句话】排障不是猜，是先定位慢在哪一层，再用那一层唯一的工具。

【五层与证据】
  位层：位模式（xxd / objdump / printf %a）
  CPU ：周期与事件（perf stat / IPC / cache-miss）
  内核：syscall 与队列（strace -T / vmstat r,b / /proc/PID）
  网络：包与状态机（ss -ti / tcpdump / softnet_stat）
  应用：栈与对象（pprof / gdb bt / 锁等待 / GC 日志）

【四个门】
  可复现 → 资源还是逻辑 → 哪一层 → 哪个函数
  每门都要有出口条件；不满足就停下补证据。

【三个数字（本机）】
  getppid 20k：裸跑 0.023s，strace 1.788s（77.7×）
  sleep 5 次：strace -c 显示 0.00s，-c -w 显示 0.1038s
  64MiB 随机 486–572 ns/步，顺序 1.2–1.7 ns/元素（约 350×）

【三个陷阱】
  第一行 vmstat 是开机均值；/proc 的 tick 是 10ms
  load 含 D 状态；容器里先 readlink /proc/self/ns/*
  -f 的秒数是线程之和，可能大于墙钟
```

---

## 54.10 自检 3 题

**Q1.** 一台 2 核机器的 `load average` 是 14，但 `top` 显示 CPU 有 60% 空闲，业务 QPS 也正常。请解释「load 高 + CPU 闲」如何同时成立，并给出两条最先要看的证据。

<details><summary>答案</summary>

`loadavg` 统计的是 `R`（可运行/运行中）+ `D`（不可中断睡眠）的任务数，不是 CPU 使用率。`D` 状态的任务通常卡在磁盘 IO、NFS、驱动或某些内核锁上，它们**不消耗 CPU，但算进 load**。所以「load 14 + CPU 空闲」最常见的解释是：一批任务在等 IO/内核资源。

两条最先看的证据：

1. `vmstat 1` 的 `r` 与 `b` 列：`r` 大是真正的 runqueue，`b` 大是不可中断睡眠；再看 `wa` 是否高。
2. `ps -eo pid,stat,wchan:24,comm | awk '$2 ~ /D/'`：列出 `D` 状态进程与它们等待的内核函数（`wchan`）。

如果 `r` 很小、`b` 很大，就不要加 CPU；去查 IO/存储/驱动。反过来，如果 `r` 很大而 `id` 也高，才要怀疑调度配置、cgroup 配额或 CPU 亲和性。**千万别用 load 单独下结论**：它把两种完全不同的等待混在一个数里。

</details>

**Q2.** 一个请求墙钟 2 s，`strace -c -w` 显示所有 syscall 加起来只有 30 ms，`top` 显示该进程 CPU 约 15%。接下来该看哪一层？列出两条能继续缩小范围的证据。

<details><summary>答案</summary>

syscall 只占 30 ms、CPU 只有 15%，说明**大部分时间既不在内核 syscall，也不在用户态烧 CPU**。剩下三类可能：

1. **阻塞在非 syscall 的等待**：锁（futex 是 syscall，但可能只占很小比例）、条件变量、连接池、下游响应；这类等待在 `strace` 里可能只有几次 futex；
2. **被调度器换出**：进程在 runqueue 里等 CPU，或被 cgroup 限流；此时 `top` 的 CPU 低是因为**没轮到**，不是不需要；
3. **时间被算到别的线程/进程**：`-f` 没开、或请求跨进程（网关/DB），本进程的 2 s 里有一部分在等别人。

两条证据：

- `cat /proc/PID/status` 看 `State`、`voluntary_ctxt_switches`、`nonvoluntary_ctxt_switches`；`cat /proc/PID/wchan` 看内核等待点；`cat /proc/PID/stack`（需权限）看内核栈。
- `strace -f -T -e trace=futex,epoll_wait,poll,read,write` 看**单次**等待的时长，而不是汇总；如果一次 `futex` 就等了 1.8 s，锁就是主因；如果 `epoll_wait` 等了 1.8 s，是事件循环没被唤醒。

若两者都正常，再上 `perf sched`（有权限时）看调度延迟，或检查 cgroup 的 `cpu.stat` 里 `nr_throttled` 是否在涨。**核心判据：墙钟 − CPU − syscall 的差值，必须找到承接者。**

</details>

**Q3.** 容器里 `free -m` 显示用了 1.5 GB，宿主侧 cgroup 的 `memory.current` 显示这个容器用了 8 GB；容器内 `ss -s` 还看到 2000 多条 TCP，但容器里只跑了一个 worker。请解释这三个数字为什么可以同时为真，并说明排障时该怎么确认视角。

<details><summary>答案</summary>

三个数字口径不同：

1. **容器内 `free`** 读的是 `/proc/meminfo`，在共享内核的容器里它通常是**宿主全局内存**（除非有 lxcfs 之类的虚拟化）；它既不是 cgroup 用量，也不是本进程 RSS。
2. **宿主侧 cgroup 的 8 GB** 是 `memory.current`，它包含 page cache、内核内存、匿名页；而 `free` 的 `used` 不含可回收的 page cache，所以两者天然不等。
3. **`ss` 看的是网络命名空间**，不是「本容器进程创建的 socket」。如果容器共享宿主 netns（`readlink /proc/self/ns/net` 与 PID 1 相同），`ss -s` 数的是整个 netns，2000 条 TCP 可能全是邻居的。

确认视角的顺序：

```bash
readlink /proc/self/ns/net /proc/self/ns/pid /proc/self/ns/mnt
cat /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.stat
cat /proc/self/cgroup
ss -tanp | head            # -p 看进程，能不能对上自己的 worker
```

如果 `ns` 与 PID 1 相同，`ss` 的数字只能当宿主背景；要看自己的连接，用 `ss -tanp` 按进程过滤，或读 `/proc/PID/net/tcp`（在对应 netns 内）。内存则应以 cgroup 的 `memory.current`/`memory.stat` 为准，并把 `file`（page cache）与 `anon` 分开。**判据：先确认「这个文件/命令属于哪个命名空间、哪个 cgroup」，再解释数字。**

</details>

---

## 54.11 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 存储层次、cache line、局部性与延迟量级（解释 486 ns/步 的物理来源） | → `base/07` |
| 缓存一致性、伪共享与内存屏障（「CPU 高但吞吐低」的硬件根因） | → `base/13` |
| 虚拟内存、页表与缺页（TLB miss 与 `minflt` 的机制） | → `base/26` |
| IO 模型与 epoll/io_uring（「阻塞还是就绪」的判据） | → `base/31` |
| socket 收包全路径、软中断、backlog 与零拷贝（带宽上不去的六道门） | → `base/32` |
| 一次请求的完整旅程（本篇的前置：先把链路画出来） | → `base/53` |
| 时钟、NTP 与时间倒流（`CLOCK_REALTIME` 与 `MONOTONIC` 的分工） | → `base/55` |
| Go 的 pprof / trace / 调度延迟分析（应用层的栈与采样） | [go/29](../go/29-pprof-trace.md) |
| 线上故障的止血、分级与复盘流程（本篇只管「查哪层」） | [eng/51](../eng/51-troubleshooting-handbook.md) |
| 全书总纲：四条主线与 56 篇地图 | [`0-summary.md`](0-summary.md) §0.2、§0.7、§0.8 |
