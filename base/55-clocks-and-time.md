# 55. 时钟、时间与顺序：四种时钟、NTP、闰秒与时间倒流

> **状态**：`已完成（初稿）` ｜ **所属**：第五部分 · 收口 ｜ **编号**：55 / 56
> **本篇定位**：第五部分的第三篇，也是全书唯一一篇把「时间」当成**一个会漂、会被拨回、会被闰秒撕开的硬件读数**来拆的地方。全篇只有一条推论链——`时间不是事实，是从一个会漂的振荡器上读出来的数` → `所以同一个瞬间有好几个读数` → `所以「过了多久」和「现在几点」必须用不同的钟` → `所以跨机器的时间戳不能当顺序`。55.1 的事故是靶子，55.2–55.6 拆四种时钟、晶振、NTP、闰秒与虚拟化，55.7 把结论推到分布式顺序，55.8 落成规范，55.9 全部跑一遍。
> **读法**：§55.2 的对照表与 §55.4 的「三层语义」是全篇支点，建议先看这两张表再回头读引子；§55.3 的 ppm 换算与 slew 时间是唯一需要动笔算的地方；§55.9 的五个实验都能原样复现（其中 step 实验需要 `CAP_SYS_TIME`）；赶时间只读 §55.10–55.12。
> **不在本篇**：CPU 主频那个「时钟」→ `base/10`；`clock_gettime` 的 syscall 入口与 vDSO 完整路径 → `base/21`；一次请求的端到端耗时怎么拆 → `base/53`；perf/strace/tcpdump 各查哪层 → `base/54`；Snowflake 的位布局与三种回拨处置 → [`comp/45`](../comp/45-distributed-id.md)；trace 里的时间戳怎么用 → [`go/35`](../go/35-observability.md)。

---

## 55.1 引子：一次「锁过期了，任务还没做完」的重复结算

先给结论：**这起事故里没有一行并发代码写错。** 加锁、续租、释放的逻辑都对；错的是「30 秒」这个数被写成了一个**墙上时间的绝对时刻**，而两台机器对「现在几点」的理解差了 12 秒。

2026-02 某清结算系统的复盘（数据脱敏）。任务是把前一天的交易逐笔与渠道对账，单轮 45 秒。锁不是 Redis 的 `PX` 过期，而是团队自己实现的「租约表」：**持有者用自己的时钟**算出一个到期时刻写进共享存储，**其他节点用自己的时钟**判断「现在是否已过这个时刻」。这段设计在评审里看起来毫无问题——直到 NTP 把其中一台的钟往回拨了 12 秒。

| 真实时刻（UTC） | A 的 `CLOCK_REALTIME` | 事件 |
| --- | --- | --- |
| 02:10:00.000 | 02:10:00.000 | A 抢到锁，写入 `expire_at = 02:10:30.000`，开始 45 s 的对账 |
| 02:10:12.000 | 02:10:00.000 | chrony 判定 A 快 12 s，超过 `makestep` 阈值，**直接 step 回拨** |
| 02:10:30.000 | 02:10:18.000 | B 的钟是准的，看到 `expire_at` 已过，抢到锁，开始同一批对账 |
| 02:10:30.000 | 02:10:18.000 | A 本地检查 `now < expire_at`：还剩 12 s，**继续持有** |
| 02:10:45.000 | 02:10:33.000 | A 跑完第一遍，写库；A 的释放操作打到一把已经属于 B 的锁上 |
| 02:11:15.000 | 02:11:03.000 | B 跑完第二遍；同一批 1,204 笔结算入账两次 |
| 02:19 | — | T+0 对账发现重复入账 1,204 笔、金额 38.7 万元，事故定级 P1 |

注意这张表里 A 的钟并不是「坏了」：它在 02:10:12 之前一直快 12 秒，只是**从没人和它对齐过**。step 之后 A 的读数回到正确，但它自己感知不到「刚才少了 12 秒」——`clock_gettime` 只返回一个数，不返回「这个数刚刚被谁改过」。A 的租约判断没有错，错在它拿一个**可被外部调整的变量**当成了「还剩多久」的度量。

同一个根在日志上长得是另一副样子：三台机器里有一台慢 800 ms，聚合系统按时间戳排序，于是「收到请求」排在「发出响应」后面，值班的人第一反应是「代码在时光倒流」。**日志对不上、锁提前过期、`elapsed` 为负、定时器提前触发，这些症状的共同根就是一句话：时间是读数，不是事实。**

本篇要交付的，就是把这个「读数」拆开：先看有几种钟（§55.2），再看读数为什么不准（§55.3），然后看它倒退时会把哪些东西弄坏（§55.4–55.5），接着把机器换成虚拟机和容器再看一遍（§55.6），最后推到「跨机器的顺序」（§55.7）与工程规范（§55.8）。

---

## 55.2 四种时钟：同一个瞬间，四个不同的数

先纠一个用词：本篇的「时钟」是**时间源**（clock source），不是 CPU 主频（`base/10` 里的 clock cycle）。前者回答「现在几点 / 过了多久」，后者回答「一秒能跑多少个周期」。两者唯一的共同点是都会漂。

在任意一台 Linux 上，`clock_gettime` 至少能问出下面五个读数。它们读的是同一个底层计数器（本机是 `kvm-clock`），但**起点、修正方式、是否含休眠**各不相同：

```text
CLOCK_REALTIME        1795094268.135892941   ← 2026-11-19T13:17:48Z（墙上时间）
CLOCK_MONOTONIC       1394198.939384612      ← 开机后 1,394,198.94 秒（16.14 天）
CLOCK_MONOTONIC_RAW   1394198.913667643      ← 比 MONOTONIC 少 25.717 ms
CLOCK_BOOTTIME        1394198.939385463      ← 与 MONOTONIC 差 70 ns（这台机器没睡过）
CLOCK_TAI             1795094268.135895576   ← 与 REALTIME 几乎相等：tai offset 没设
```

五个读数，五个用途。把它们的差别摊成一张表：

| 时钟 | 起点 / 依据 | 会倒退吗 | 受 NTP step 影响 | 受 NTP 频率修正 | 含 suspend | 典型用途 |
| --- | --- | --- | --- | --- | --- | --- |
| `CLOCK_REALTIME` | RTC 初值 + 后续修正，Unix epoch | **会**（`clock_settime` / step） | 是 | 是 | 是（墙上时间一直在走） | 日志、证书、业务日期、审计 |
| `CLOCK_MONOTONIC` | 开机为 0，之后只增 | 不会（同一 boot 内） | 否 | **是**（slew 会改频率） | 否 | 耗时、超时、定时器 |
| `CLOCK_BOOTTIME` | 开机为 0，**含休眠** | 不会 | 否 | 是 | **是** | 「30 分钟后叫醒我」、跨挂起的超时 |
| `CLOCK_MONOTONIC_RAW` | 原始硬件计数，开机为 0 | 不会 | 否 | **否** | 否 | 量晶振、调试 NTP、做基准 |
| `CLOCK_TAI` | TAI 纪元 + 内核 `tai offset` | 不会因闰秒跳 | 否 | 是 | 是 | 需要「连续、无闰秒」的时间轴 |

### 每个钟存在的理由，都是它排除了另一种失效

**`CLOCK_REALTIME`** 是唯一能和别人对表的钟。它必须可改——否则机器时间错了就永远修不回来。代价也正来自「可改」：任何有 `CAP_SYS_TIME` 的进程、任何一次 NTP step、任何一次虚拟机迁移，都能让它在两次读之间倒退。它的分辨率是 1 ns，准确度却取决于上游同步（§55.3）。

**`CLOCK_MONOTONIC`** 是「过了多久」的钟。它从开机起算，同一 boot 内不倒退，所以耗时、超时、重试退避都用它。两个误解要拆掉：① 它**不是**「不受 NTP 影响」——slew 改的是频率，MONOTONIC 会跟着快慢，只是不会跳；② 它的起点是**开机**，不是进程启动，所以容器里读到的可能是宿主的 16 天（§55.6）。

**`CLOCK_BOOTTIME`** 与 MONOTONIC 只差一件事：**休眠期间它继续走**。本机从未 suspend，所以两者只差 70 ns；一旦合盖 10 分钟，BOOTTIME 会多出 600 秒，MONOTONIC 不动。Android 的 `SystemClock.uptimeMillis()` 是 MONOTONIC，`elapsedRealtime()` 是 BOOTTIME；「30 分钟后提醒我」该用后者——用户不在乎机器睡没睡。

**`CLOCK_MONOTONIC_RAW`** 是唯一不经过 NTP 频率修正的钟，适合量晶振的真实漂移，以及检验「NTP 到底改了什么」。本机实测 `MONOTONIC - RAW = +25.717 ms`，除以 1,394,354 秒得 **+18.4 ppb**——**这条差值就是「MONOTONIC 会被 slew」最硬的证据**，不是文档里的一句话。

**`CLOCK_TAI`** 给「不能有闰秒」的系统：TAI 没有 23:59:60，时间轴连续。但它不会自动比 UTC 快 37 秒——本机实测 `TAI - REALTIME = 0`，因为 `tai offset` 必须由 `clock_adjtime(ADJ_TAI)` 或 NTP 显式设置。**没设 offset 的 `CLOCK_TAI` 就是 `CLOCK_REALTIME` 的别名。**

### 读一次有多贵：vDSO 与 syscall 的账

`clock_gettime` 正常情况下**不进内核**：glibc 通过 vDSO 在用户态读 `timekeeper`（一个 seqlock 保护的共享页），所以能便宜到百纳秒级。细节属于 `base/21`；本机实测 vDSO 为 110–203 ns、`syscall(SYS_clock_gettime)` 为 595–1078 ns，**差 5–6 倍**，且同一台机器上单次成本能抖 2 倍（2 vCPU、load 11 的共享虚拟机，§55.6）。Python 的 `time.monotonic()` 实测 713 ns/次，又贵一个数量级——**语言封装也是成本**。

> **便宜不等于准。** 下一节讲「准」由什么决定。

---

## 55.3 硬件与校准：ppm、slew 与「刚启动的机器为什么不准」

### 晶振：一个机械谐振器的制造公差

石英晶振的标称频率（32,768 Hz 或 100 MHz）靠切割尺寸决定，切割、温度、老化都会带来误差，单位是 **ppm**（parts per million，百万分之一）。1 ppm 的意思是「每走 1 秒差 1 微秒」，换算成日常尺度：

| 误差 | 每秒 | 每天 | 每年 | 典型来源 |
| --- | --- | --- | --- | --- |
| 1 ppm | 1 µs | 86.4 ms | 31.5 s | 温补晶振（TCXO）的好状态 |
| 20 ppm | 20 µs | 1.73 s | 10.5 min | 主板 RTC 常温 |
| 50 ppm | 50 µs | 4.32 s | 26.3 min | 廉价晶振 |
| 100 ppm | 100 µs | 8.64 s | 52.6 min | 温度失控 / 老化 |
| 500 ppm | 500 µs | 43.2 s | 4.4 h | 内核频率修正的上限 |

推导只有一行：`1 ppm = 10⁻⁶`，所以每天偏 `86,400 × 10⁻⁶ = 0.0864 s`，每年偏 `31,536,000 × 10⁻⁶ = 31.5 s`。这张表要记住的不是五行数字，而是**量级**：一个「看起来很正常」的晶振，一天就能差几秒，一年能差半小时。**「时间不准」不是故障，是默认状态。**

### NTP：不是「对一次表」，是一个控制回路

NTP 客户端每隔一段时间和服务器交换四个时间戳：

```text
        t1 ────── 请求 ──────► t2
        A                        B
        t4 ◄───── 响应 ─────── t3

   offset = ((t2 - t1) + (t3 - t4)) / 2
   delay  =  (t4 - t1) - (t3 - t2)
   误差上界 ≈ delay / 2      （往返路径不对称时）
```

`offset` 是「我的钟比服务器快多少」，`delay` 是往返时延。公式成立的前提是**去程和回程一样快**；一旦不对称（回程排队更久，或 VM 的 vCPU 在中间被抢走 30 ms），误差就直接进 `offset`。所以同机房 LAN 的误差约 0.1–0.5 ms，公网 5–50 ms，被抢占的虚拟机可能到几十毫秒且**不可预测**。

NTP 按「层」组织，叫 stratum：

```text
stratum 0   GPS / 铯钟 / 氢钟（参考源，不直接对外服务）
   │
stratum 1   直接接参考钟的服务器（自身误差 ~µs）
   │
stratum 2   从 stratum 1 同步（叠加一跳网络误差）
   │
stratum 3 … 每跳再加一段误差
   │
stratum 15  上限；stratum 16 = 未同步
```

**stratum 不是质量分。** 一台同机房的 stratum 3 通常比一台跨洲的 stratum 2 准，因为误差主要是网络路径，不是层数。

收敛也不是瞬间的：客户端从 64 秒的轮询间隔开始，逐步退到 1024 秒；冷启动要经过若干次交换才能把 offset 压到毫秒级，之后靠**频率修正**维持。**「机器刚起来时间不准」不是因为它笨，是因为它还没测完。**

### step 与 slew：两种修正，两种后果

内核有两种改时间的办法：

- **step**：直接把 `CLOCK_REALTIME` 设成新值。瞬间完成，但时间轴出现**不连续**——可能向前，也可能向后。
- **slew**：不改读数，只把时钟**频率**调快或调慢几百 ppm，让读数慢慢追上。时间轴连续，但追赶期间「一秒」不是标准的一秒。

算一笔账：内核的频率修正上限是 **500 ppm**（`MAXFREQ`），所以

```text
追赶 100 ms 需要  0.100 / 500e-6 =   200 秒  （3.3 分钟）
追赶 1 s   需要  1.000 / 500e-6 =  2,000 秒  （33 分钟）
追赶 24 s  需要 24.000 / 500e-6 = 48,000 秒  （13.3 小时）
```

最后一行是关键：**偏差大到一定程度，slew 在工程上不可接受。** 于是所有 NTP 实现都有「panic 阈值」：经典 ntpd 默认 128 ms，超过就 step；chrony 的 `makestep 1.0 3`（多数发行版默认配置）表示前三次更新里偏移超过 1 秒就直接 step。§55.1 的 12 秒回拨，就是这条策略的产物——**它不是为了害你，它是为了不让你的钟错 13 个小时。**

### 现场证据：一台没同步的机器长什么样

本机（Debian 12 / Linux 6.12 / 2 vCPU KVM）没有任何 NTP 服务在跑。三条命令就能看出「时间是读数」：

```text
$ timedatectl
               Local time: Thu 2026-11-19 08:20:47 EST
           Universal time: Thu 2026-11-19 13:20:47 UTC
                 RTC time: Thu 2026-11-19 13:21:11
                Time zone: US/Eastern (EST, -0500)
System clock synchronized: no
              NTP service: n/a
```

```text
$ ./adj
adjtimex state=5 (TIME_ERROR)
freq=0 ppb  offset=0 us  maxerror=16000000 us  esterror=16000000 us
status=0x40  PLL=0  FLL=0  UNSYNC=1
tick=10000 us  constant=2
```

两段输出要连起来读：

1. **`RTC time` 比 `Universal time` 快 24 秒。** RTC 是主板上带电池的晶振，`clock_settime` 只改内核里的墙上时间，**不写回 RTC**（要 `hwclock --systohc`）。如果上次校准是 30 天前，24 秒只对应 `24 / (30 × 86,400) = 9.3 ppm`——完全在一颗廉价晶振的正常范围内。
2. **`maxerror = 16,000,000 µs`、`UNSYNC = 1`。** 这是内核「我不知道现在有多准」的上界，初始值就是 16 秒；没人来校，它就停在 16 秒。**`synchronized: no` 不是摆设，它是这台机器上所有时间戳的准确度声明。**

这就是「刚启动的机器时间不准」的全部原因：它把 RTC 的读数当成了真相，而 RTC 在关机期间照样漂，还没人告诉它漂了多少。**在第一次 NTP 收敛之前，这台机器的墙上时间只有一个 ±16 秒的承诺。**

---

## 55.4 时间倒退之后：负数、584 年与「锁还没到期」

### 谁能让时间倒退

`CLOCK_REALTIME` 本质上是一个**可写的全局变量**。能写它的至少有四类：NTP 的 step（§55.3）、管理员 `date -s` / `hwclock --hctosys`、虚拟机迁移或宿主改时间（§55.6）、**任何有 `CAP_SYS_TIME` 的进程**。本机 `CapEff = 000001ffffffffff`，第 25 位（`CAP_SYS_TIME`）是 1，所以 §55.9 的实验里我们真的把墙上时间往回拨了 5 秒——**「只有 NTP 能改时间」是幻觉，一个跑飞的容器就能。**

MONOTONIC 挡得住这四类，因为它根本不看墙上时间。**所以「时间倒退」的事故，几乎都能追溯到某处用了 REALTIME 去算一个本该用 MONOTONIC 的量。**

### 后果一：TTL 凭空变长

租约、缓存、限流窗口都长成 `deadline = now + TTL` 的样子。如果 `deadline` 用 REALTIME 存、判断时再读一次 REALTIME，那么回拨 Δ 秒后，剩余时间就凭空多出 Δ 秒：

```text
回拨前 ttl = 10 s
回拨后 ttl = 30 s   <-- 凭空多出 20 s
若用 uint64 存「now - deadline」：18446744073709551586 s（≈ 584,942,417,355 年）
```

最后一行是另一个常见写法：`uint64_t elapsed = now - deadline;`。当 `now < deadline`，有符号差是负数，转成无符号就绕到 `2⁶⁴ - x`——**一个 30 秒的早到，变成了 5849 亿年。** 任何 `if (elapsed > timeout)` 都会永远为真或永远为假，取决于比较的两边怎么解释。

| 用法 | 回拨时发生什么 | 线上症状 |
| --- | --- | --- |
| `TTL = deadline - now` | TTL 变大 | 锁/租约过期晚了，缓存不刷新，重复执行 |
| `elapsed = now2 - now1` | 负数 | 耗时统计出现负值，P99 桶错位 |
| `uint64` 差值 | 回绕到 1.8×10¹⁹ | 超时 584 年，连接永远不超时 |
| 限流窗口 `(now-last)/interval` | 窗口不前进或倒退 | 配额翻倍 / 永久封禁 |
| 日志按时间戳排序 | 因果倒置 | 「响应早于请求」 |
| 证书 / Token 过期判断 | 有效期被延长 | 已过期的凭证还能用 |
| 单调递增假设（ID） | 重复 / 乱序 | 主键冲突（[`comp/45`](../comp/45-distributed-id.md)） |

### 后果二：用 REALTIME 测耗时

最常见的写法，也是错得最不容易被发现的一种：

```c
/* 错误：用墙上时间测耗时 */
struct timespec a, b;
clock_gettime(CLOCK_REALTIME, &a);
do_work();
clock_gettime(CLOCK_REALTIME, &b);
long ms = (b.tv_sec - a.tv_sec) * 1000L + (b.tv_nsec - a.tv_nsec) / 1000000L;

/* 修复：只改一个常量 */
clock_gettime(CLOCK_MONOTONIC, &a);
do_work();
clock_gettime(CLOCK_MONOTONIC, &b);
```

这段代码 99.99% 的时间是对的——所以它能活到线上。§55.9 的实验把 REALTIME 往回拨 5 秒再跑一次同样的等待，得到：

```text
REALTIME  elapsed = -3.999902 s   <-- 负的
MONOTONIC elapsed = +1.000106 s   <-- 真实经过的时间
```

**同一段代码、同一次等待，两个钟给出两个符号。** 更阴的是它不总是报错：如果耗时被写进 `long`，负值只是让均值变小；如果被写进直方图，负值会落进第一个桶，把 P99 悄悄拉低。**你没有看到异常，你看到的是「性能变好了」。**

各语言的对应写法：

| 语言 | 错误（墙上时间） | 正确（单调时间） | 备注 |
| --- | --- | --- | --- |
| C | `clock_gettime(CLOCK_REALTIME)` | `CLOCK_MONOTONIC` | 跨 suspend 用 `CLOCK_BOOTTIME` |
| Go | `t2.UnixNano() - t1.UnixNano()` | `time.Since(start)` | 序列化会丢掉单调读数 |
| Java | `System.currentTimeMillis()` | `System.nanoTime()` | `nanoTime` 的绝对值无意义 |
| Python | `time.time()` | `time.monotonic()` | `perf_counter` 更适合基准 |
| Shell | `date +%s` 相减 | 没有内建单调钟 | 脚本里的耗时天生不可靠 |

Go 那一行值得单独记：`time.Now()` 返回的 `Time` 同时带**墙上读数**和**单调读数**，`t2.Sub(t1)` 优先用后者（Go 1.9 之后的设计）。但一旦你把 `t` 存进 JSON、数据库或日志再读回来，单调读数就没了，`Sub` 会静默退回墙上时间。**「我用了 `time.Now()` 所以没问题」是一个半真的说法。**

### 后果三：把「时刻」和「时长」混为一谈

把三件事分开，很多争论就消失了：

| 语义 | 问的问题 | 该用什么 | 例子 |
| --- | --- | --- | --- |
| **instant**（时刻） | 这件事发生在哪一刻？ | `CLOCK_REALTIME`，存 UTC | 日志、审计、证书有效期 |
| **duration**（时长） | 过了多久？ | `CLOCK_MONOTONIC` | 耗时、超时、重试退避、限流 |
| **schedule**（日程） | 当地时间几点做？ | 带时区的日历规则 + `CLOCK_REALTIME` | cron、账单日、营业时间 |

三者的失效方式完全不同：instant 怕**不同步**，duration 怕**用错钟**，schedule 怕**时区/DST**。§55.1 的事故是拿 instant 算 duration；下一节的闰秒是 instant 本身不连续；§55.11 的 cron 坑是 schedule 撞上 DST。

---

## 55.5 闰秒：23:59:60 与「一天不是 86400 秒」

### UTC 不是「原子时」，是「折中时」

TAI 是原子钟的均匀时间，UTC 是 TAI 加上闰秒、让它和地球自转（UT1）保持在大约 0.9 秒以内。所以 UTC 的「一天」不总是 86,400 秒：

```text
正常日：  23:59:59 → 00:00:00                    86,400 s
正闰秒：  23:59:59 → 23:59:60 → 00:00:00         86,401 s
负闰秒：  23:59:58 → 00:00:00                    86,399 s（从未发生过）
```

从 1972 年到 2016 年底，一共插入 **27 次**正闰秒，TAI−UTC 从 10 秒变成 37 秒。最后一次是 **2016-12-31 的 23:59:60**；2022 年 CGPM 决议计划最迟到 2035 年取消闰秒插入，但在那之前，所有存时间的地方都得能处理它。

麻烦在于 **POSIX 的 `time_t` 不承认闰秒**：它规定每天恰好 86,400 秒，`time_t` 是「从 epoch 起的秒数」。于是闰秒那一刻，内核只能让同一个 `time_t` 值出现两次（23:59:59 停一秒），或者用别的办法「消化」这一秒。**对上层来说，时间轴在闰秒处要么重复、要么被抹平——两种都不是「连续」的。**

### 2012 与 2016：两次真实的宕机

- **2012-06-30**：闰秒插入时，Linux 内核的 `hrtimer` 进入忙等，多台机器 CPU 100%。Reddit、Mozilla、LinkedIn、Yelp 等都报告了故障。根因不是「多了一秒」，而是**高精度定时器假设时间只会平滑前进**，遇到不连续时算出了负的到期差，于是「立即到期」变成自旋。
- **2016-12-31**：Cloudflare 的 DNS 服务在闰秒窗口出现 TTL 计算异常（`expire - now` 为负），触发 panic。形状完全一样：**把「两个墙上时刻之差」当成非负的时长。**

这两起事故的共同点值得抄在值班手册第一页：**闰秒不会让业务逻辑变慢，它会让所有「时间差一定为正」的隐含假设同时失效。**

### smear：把 1 秒摊成 20 小时

大厂的做法不是「在 23:59:60 那一秒里小心一点」，而是**根本不插入这一秒**：让本地时钟在闰秒前后跑快一点点，把 1 秒摊平。Google 的 leap smear 用 20 小时窗口：

```text
多出来的 1 秒 / 20 小时 = 1 / 72,000 = 13.9 ppm
```

时钟在这 20 小时里以 **+13.9 ppm** 运行，累积正好 1 秒。13.9 ppm 远低于内核 500 ppm 的上限，所以不需要 step，时间轴保持连续。代价是：① 期间本地时钟与 UTC 有界偏差，以闰秒时刻为中心摊平最大约 0.5 秒（单侧摊平则到 1 秒）；② **所有系统必须用同一种 smear**——A 机房 smear、B 机房不 smear，两边日志在同一真实时刻能差出最多 1 秒，**这比闰秒本身更难查**，因为它不报错，只有对不上的时间戳。

| 策略 | 做法 | 最大偏差 | 代价 |
| --- | --- | --- | --- |
| **step**（内核默认 / POSIX） | 23:59:59 重复一秒 | 1 秒的不连续 | 依赖时间连续的代码崩（2012/2016） |
| **slew**（小偏移） | 调频率慢慢追 | 无跳变 | 追赶期间「一秒」不是标准秒 |
| **smear**（Google / AWS） | 20–24 小时线性摊平 | 约 0.5–1 秒 | 必须全网统一策略，否则跨域差 1 秒 |
| **TAI** | 不插入闰秒，固定偏移 | 0（在 TAI 域内） | 全链路都要支持，和 UTC 转换要查表 |

**为什么大厂选 slew/smear 而不是 step？** 因为 step 会让时间倒退或重复，而「倒退」会同时打断 TTL、定时器、单调假设和日志排序（§55.4）。slew 的代价只是「一段时间内时钟速率不标准」，这个代价可以用「有界偏差」描述、可以监控、可以在设计里预留；step 的代价是「不连续」，它在任何依赖顺序的地方都是一个洞。**用可度量的误差换不可度量的断裂，这是工程上的正确交易。**

顺带一个计费坑：`(end - start) / 86400` 算「天数」在闰秒日会少算 1 秒；按秒计费的系统如果假设「每天 86,400 秒」，一年下来会差几十秒。**「秒数除以 86400」不是天数，是「POSIX 天」，两者在闰秒日不相等。**

---

## 55.6 虚拟机与容器：为什么你的钟会「跳一下」

### kvm-clock：让 guest 不进内核就能读到时间

本机的时钟源是 `kvm-clock`（`available_clocksource` 为 `kvm-clock tsc acpi_pm`）。它是半虚拟化时钟：KVM 在 guest 内存里放一页只读结构，每个 vCPU 一个 32 字节的 `pvclock_vcpu_time_info`：

```c
struct pvclock_vcpu_time_info {   /* guest 只读，KVM 维护；共 32 字节 */
    u32 version;          /* 读前读后各读一次，奇数表示正在更新 */
    u32 pad0;
    u64 tsc_timestamp;    /* 上次更新时的 TSC 值 */
    u64 system_time;      /* 与之对应的 guest 纳秒数 */
    u32 tsc_to_system_mul;
    s8  tsc_shift;
    u8  flags;
    u8  pad[2];
} __attribute__((__packed__));
```

guest 读到 `(tsc_now - tsc_timestamp) × mul >> shift + system_time` 就是当前纳秒，**全程不触发 VM exit**。这就是为什么 `clock_gettime` 在虚拟机里也能做到百纳秒级——它读的是内存，不是 I/O 端口。

三种时钟源的层次也很清楚：`kvm-clock` 是半虚拟化（最懂虚拟化，能在迁移时被宿主修正）；`tsc` 是裸机的时间戳计数器（快，但跨核/跨机可能不同步）；`acpi_pm` 是 ACPI 电源管理定时器（约 3.58 MHz，读一次要几微秒，只在前面都不可用时兜底）。**时钟源是分层降级的，降级的原因几乎总是「不可靠」而不是「不够快」。**

### vCPU 被抢占：时间不会停，但你的线程会

这是虚拟机里最反直觉的一点：**vCPU 被宿主调度走 100 ms，guest 的 MONOTONIC 照样前进 100 ms。** 因为时间是从 TSC/pvclock 算出来的，不是从「执行了多少条指令」数出来的。于是你测到的「耗时 100 ms」里，可能 90 ms 是别人在用 CPU：

```text
你在 guest 里：  t0 ── 读时钟 ── [被宿主抢占 90 ms] ── 读时钟 ── t1
你看到的：       elapsed = 100 ms
真实发生的：     你只跑了 10 ms，另外 90 ms 在 runqueue 里排队
```

`/proc/stat` 的 `steal` 字段就是这件事的账本。本机：

```text
cpu  3126395 12346 816294 274076578 20729 0 271667 21815 0 0
                                                       ↑ steal
```

`steal = 21,815` ticks，总 ticks ≈ `278,345,824`，占比 `7.84×10⁻⁵ = 0.0078%`；按 100 Hz 折算，16.14 天里累计约 **218 CPU-秒**（约 109 秒墙上时间）。这个数字很小，但本机 `load average` 是 **11.01 / 2 vCPU**——压力主要来自同宿主其它租户的排队。两种情况的共同后果是：**`clock_gettime` 的单次成本抖 2 倍（110–203 ns），任何「耗时」测量都自带几十微秒到几毫秒的噪声。** 所以在虚拟机上做微基准，**第一条纪律是报告环境（vCPU 数、load、steal），否则数字没有出生证明。**

### 容器：共享宿主的钟，连 boot 都是宿主的

容器没有自己的时钟：`CLOCK_REALTIME` 就是宿主的墙上时间；`CLOCK_MONOTONIC` 是**宿主开机**到现在的秒数，不是容器启动到现在的秒数；`/proc/uptime` 和 `CLOCK_BOOTTIME` 也同样带宿主的历史。

Linux 5.6 之后有 **time namespace**，可以给容器一个 `monotonic`/`boottime` 的偏移（主要用于 CRIU checkpoint/restore）。本机 `readlink /proc/self/ns/time` 是 `time:[4026531834]`，`/proc/self/timens_offsets` 里两个偏移都是 0，说明没有做时间虚拟化。关键限制是：**time namespace 只能虚拟化 MONOTONIC 和 BOOTTIME，不能虚拟化 REALTIME**。墙上时间是全局的，容器改不了（除非有 `CAP_SYS_TIME`），也不应该改。

所以「在容器里跑一个 ntpd」是典型的反模式：几十个容器各自去调同一个宿主时钟，互相打架，还可能在某个容器里把整台机器的时间拨回去。**时间同步是宿主的事，容器只消费。**

---

## 55.7 分布式时间：为什么不能用本地时钟排全局顺序

### 先把「精度」这四个字拆开

| 概念 | 定义 | 典型值 | 常见误解 |
| --- | --- | --- | --- |
| 分辨率（resolution） | 两个读数最小能差多少 | 1 ns（`clock_getres`） | 「能读到 1 ns 就是 1 ns 准」 |
| 准确度（accuracy） | 读数与真值的偏差 | NTP 1–50 ms；PTP 0.1–1 µs | 「NTP 同步了就是准的」 |
| 稳定度（stability） | 一段时间内漂移多少 | 晶振 1–100 ppm | 「校准一次能管一年」 |
| 不确定度（uncertainty） | 真值落在多宽的区间 | TrueTime ε ≈ 1–7 ms | 「时间是一个点」 |

`clock_getres` 对四个钟都返回 1 ns，但本机 `MONOTONIC - RAW` 在 16 天里差了 25.7 ms——**分辨率是 1 ns，准确度是几十毫秒，两者差七个数量级。** 说「我们有纳秒时间戳」却不给准确度，等于没说。

### 一个 5 ms 的偏差如何颠倒因果

设 A 的钟快 2 ms，B 的钟慢 3 ms，A 发一个请求给 B，真实单程 1 ms：

```text
真实时间轴：   A 发出 10:00:00.004 ──1 ms──► B 收到 10:00:00.005
时间戳：       A 记 10:00:00.006              B 记 10:00:00.002
按时间戳排序： B 的「收到」在 A 的「发出」之前 4 ms —— 因果被颠倒
```

这不是「日志不好看」，这是**任何基于时间戳的判断都会错**：谁先谁后、哪个请求是重试、哪条消息该覆盖哪条。要保证顺序，必须让两台机器的偏差之和小于两个事件的真实间隔；A、B 各差 10 ms，任何间隔小于 20 ms 的事件都无法排序。**而互联网 NTP 的典型误差就是 1–50 ms。**

### 五种「排序」手段，各自买的是什么

| 手段 | 保证 | 精度 / 代价 | 典型用处 |
| --- | --- | --- | --- |
| **NTP** | 近似同步的墙上时间 | 1–50 ms，成本极低 | 日志、监控、告警时间 |
| **PTP**（IEEE 1588） | 亚微秒级同步 | 需要硬件时间戳和边界时钟 | 金融交易、5G、工业控制 |
| **TrueTime** | 一个有界的不确定区间 `[earliest, latest]` | ε ≈ 1–7 ms；提交要等 `2ε` | Spanner 的全局事务 |
| **Lamport 逻辑钟** | 只保证因果序（happened-before） | 无物理时间，只是一个计数器 | 因果一致的复制 |
| **中心发号器** | 全序 | 一次 RTT + 单点瓶颈 | 数据库序列、Kafka offset |
| **HLC**（混合逻辑钟） | 物理时间 + 逻辑计数，保因果 | 接近物理时间，实现复杂 | CockroachDB 等 |

TrueTime 的做法值得多看一眼：它不承诺「现在是几点」，只承诺「真值一定落在这个区间里」。事务提交时**等过 `2ε`**（约 14 ms），确保所有可能更早的时间戳都已落盘，然后才分配提交时间。**它用 14 ms 的延迟买了「时间戳顺序 = 真实顺序」。**

### 为什么本地时钟不能做全局顺序

把上面的结论收成一句：**本地时钟给出的是「读数」，不是「事实」；两个读数之差包含两个钟各自的误差。** 所以用 `last_write_wins` + 墙上时间做冲突解决，回拨时就丢更新；用时间戳判断分布式锁到期，step 时就双持有（§55.1）；用时间戳做 Snowflake 序列，回拨时就重复（[`comp/45`](../comp/45-distributed-id.md) 里有完整的 801 个重复 ID 复盘）。

**正确的顺序来自「因果」或「中心」，不来自「钟」。** 需要因果就用逻辑钟/HLC；需要全序就用中心发号器或 TrueTime；只有「给人看的时刻」才用 NTP 同步的墙上时间。

---

## 55.8 工程规范：把「时刻」「时长」「日程」分开

| 场景 | 用什么 | 理由 | 反例 |
| --- | --- | --- | --- |
| 日志时间戳 | UTC + RFC 3339（带 `Z` 或偏移） | 可排序、可跨时区比较 | 本地时间不带偏移，DST 一换就歧义 |
| 进程内排序 | 单调时间 + 序号 | 不受 NTP step 影响 | 按 `time.time()` 排序 |
| 请求耗时 | `CLOCK_MONOTONIC` | 只增，差值非负 | `System.currentTimeMillis()` |
| 超时 / 重试退避 | `CLOCK_MONOTONIC` | 与墙上时间解耦 | `deadline = now + 30s` 用 REALTIME 存 |
| 跨 suspend 的提醒 | `CLOCK_BOOTTIME` | 休眠期间继续计时 | 用 MONOTONIC，合盖后不响 |
| 定时器 / `timerfd` | `CLOCK_MONOTONIC`（或 BOOTTIME） | 内核保证单调 | `timerfd` 用 REALTIME，改时间就乱 |
| 缓存 TTL | 由缓存服务自己判定 | 单一时钟权威 | 客户端算 `expire_at` 再传给别人 |
| 分布式锁 | 锁服务判 TTL + fencing token | 不依赖持有者时钟 | 客户端各算各的到期时刻 |
| 证书 / JWT | `CLOCK_REALTIME` + 允许小偏差 + 撤销 | 有效期是绝对时刻 | 回拨后仍接受过期凭证 |
| 计费周期 | 带时区的日历规则 | 「一个月」不是固定秒数 | `(end-start)/86400` 算天数 |
| cron / 定时任务 | UTC 或显式时区 + 幂等 | 本地时间在 DST 会重复/跳过 | `0 2 * * *` 在春季跳变日不触发 |

几条值得展开的纪律：

**① 日志用 UTC，但保留时区信息。** `2026-11-19T13:20:47Z` 和 `2026-11-19T08:20:47-05:00` 是同一时刻，前者更适合排序，后者更适合人读；最糟的是不带偏移的 `2026-11-19 08:20:47`。跨机器排序时，**时间戳只能当线索，不能当因果**，真正的顺序靠 trace_id + span 的父子关系（[`go/35`](../go/35-observability.md)）。

**② 耗时只用 MONOTONIC，且不要序列化。** Go 的 `time.Time` 带单调读数，但 JSON 一编解码就丢；Java 的 `nanoTime()` 绝对值无意义，只能做差；Python 的 `monotonic()` 不保证跨进程可比。**单调时间的适用范围是「同一台机器、同一次 boot、同一个进程内的差值」。**

**③ 业务时间用 REALTIME，但要做「倒退保护」。** 对证书、Token、租约的过期判断，可以在进程内维护 `last_seen = max(last_seen, now)`，用 `last_seen` 比较，避免一次回拨延长有效期。但这只是缓解：它挡不住重启，也挡不住向前跳。**真正的撤销靠短 TTL + 撤销列表，不靠时钟。**

**④ 定时器不要用 REALTIME。** 用 REALTIME 的定时器在 NTP step 后会提前或延后触发；用 MONOTONIC 的定时器只受「机器睡了多久」影响，而这个问题正好由 BOOTTIME 解决。cron 按**本地日历**触发，所以 `0 2 * * *` 在春季跳变日的那一小时根本不存在（本机实测 `date -d '2026-03-08 02:30:00'` 直接报 `invalid date`），在秋季回拨日则会触发两次。**日程用日历规则，间隔用单调钟，两者不要混。**

---

## 55.9 实测：把上面每句话跑出来

环境：Debian 12 / Linux 6.12 / 2 vCPU KVM / `kvm-clock` / load average 11.01。所有输出都是本机真实结果；step 实验在特权容器里完成，实验后已把墙上时间拨回。

### ① 四种时钟的身份证

```python
import time
for n in ("time", "monotonic", "perf_counter", "process_time"):
    print(n, time.get_clock_info(n))
```

```text
time         namespace(implementation='clock_gettime(CLOCK_REALTIME)', monotonic=False, adjustable=True, resolution=1e-09)
monotonic    namespace(implementation='clock_gettime(CLOCK_MONOTONIC)', monotonic=True, adjustable=False, resolution=1e-09)
perf_counter namespace(implementation='clock_gettime(CLOCK_MONOTONIC)', monotonic=True, adjustable=False, resolution=1e-09)
process_time namespace(implementation='clock_gettime(CLOCK_PROCESS_CPUTIME_ID)', monotonic=True, adjustable=False, resolution=1e-09)
```

`monotonic=False, adjustable=True` 是 REALTIME 的全部危险所在；`perf_counter` 直接映射到 MONOTONIC，所以它适合测耗时，不适合测「现在几点」。四行都写 `resolution=1e-09`——**再次说明分辨率不是准确度。**

### ② REALTIME 与 MONOTONIC 的对比（含一次 step）

```c
#define _GNU_SOURCE
#include <stdio.h>
#include <time.h>
#include <errno.h>
#include <string.h>
static double sec(const struct timespec *t){ return (double)t->tv_sec + t->tv_nsec/1e9; }
int main(void){
    struct timespec r0,r1,m0,m1;
    printf("%-4s %14s %14s %10s\n","i","REALTIME(s)","MONOTONIC(s)","diff(us)");
    for (int i=0;i<5;i++){
        clock_gettime(CLOCK_REALTIME,&r0); clock_gettime(CLOCK_MONOTONIC,&m0);
        struct timespec d={0,100*1000*1000}; nanosleep(&d,NULL);
        clock_gettime(CLOCK_REALTIME,&r1); clock_gettime(CLOCK_MONOTONIC,&m1);
        printf("%-4d %14.6f %14.6f %10.1f\n", i,
               sec(&r1)-sec(&r0), sec(&m1)-sec(&m0),
               ((sec(&r1)-sec(&r0))-(sec(&m1)-sec(&m0)))*1e6);
    }
    clock_gettime(CLOCK_REALTIME,&r0); clock_gettime(CLOCK_MONOTONIC,&m0);
    struct timespec d={1,0}; nanosleep(&d,NULL);
    clock_gettime(CLOCK_REALTIME,&r1);
    struct timespec back=r1; back.tv_sec -= 5;          /* 模拟 NTP 回拨 5 s */
    if (clock_settime(CLOCK_REALTIME,&back)!=0){ perror("clock_settime"); return 1; }
    clock_gettime(CLOCK_REALTIME,&r1); clock_gettime(CLOCK_MONOTONIC,&m1);
    printf("REALTIME  elapsed = %+.6f s\n", sec(&r1)-sec(&r0));
    printf("MONOTONIC elapsed = %+.6f s\n", sec(&m1)-sec(&m0));
    struct timespec fix=r1; fix.tv_sec += 5; clock_settime(CLOCK_REALTIME,&fix);
    return 0;
}
```

```text
i       REALTIME(s)   MONOTONIC(s)   diff(us)
0          0.103477       0.103477       -0.1
1          0.101442       0.101442        0.0
2          0.100066       0.100066        0.1
3          0.100063       0.100063        0.0
4          0.100084       0.100084        0.1

REALTIME  elapsed = -3.999902 s   <-- 负的
MONOTONIC elapsed = +1.000106 s   <-- 真实经过的时间
```

前五行是「正常情况下两者一致」——这就是这个 bug 能活下来的原因。最后两行是同一个 1 秒等待在回拨 5 秒后的两个读数：**一个负数，一个正数。** 注意第 0 行等了 103.5 ms 而不是 100 ms，这是 §55.6 说的调度噪声，不是 `nanosleep` 不准。

### ③ 内核的定时器队列（可能不可读）

```text
$ cat /proc/timer_list | head -14
Timer List Version: v0.10
HRTIMER_MAX_CLOCK_BASES: 8
now at 1394177668710536 nsecs

cpu: 0
 clock 0:
  .base:       00000000d3976ba8
  .index:      0
  .resolution: 1 nsecs
  .get_time:   ktime_get
  .offset:     0 nsecs
active timers:
 #0: <00000000a4d55493>, tick_nohz_handler, S:01
 # expires at 1394177672000000-1394177672000000 nsecs [in 3289464 to 3289464 nsecs]
```

`now at 1,394,177,668,710,536 nsecs` ≈ 16.14 天，正是 MONOTONIC 的纳秒表示；每个 `expires at` 也是单调时间。**内核自己的定时器全部挂在单调时间轴上**——这是最有说服力的示范：如果内核用 REALTIME 排定时器，一次 NTP step 就会让所有定时器提前或延后。

### ④ 系统时间同步状态

```text
$ timedatectl
               Local time: Thu 2026-11-19 08:20:47 EST
           Universal time: Thu 2026-11-19 13:20:47 UTC
                 RTC time: Thu 2026-11-19 13:21:11
                Time zone: US/Eastern (EST, -0500)
System clock synchronized: no
              NTP service: n/a
          RTC in local TZ: no
```

`RTC` 比 `Universal time` 快 24 秒，`synchronized: no`——这台机器的墙上时间只有一个 ±16 秒的承诺（§55.3）。**注意 `clock_settime` 不会改 RTC：** 实验②把系统时间拨回又拨正，`RTC time` 仍然是那个快 24 秒的值，因为写回 RTC 需要 `hwclock --systohc`。

### ⑤ 时间戳的精度

```text
$ date +%s.%N; date +%s.%N; date +%s.%N
1795094245.972066537
1795094245.991079090
1795094245.994360555
```

连续三次，间隔分别约 19.0 ms 和 3.3 ms（进程启动本身的开销，不是时钟精度）。**纳秒位是真实的读数，但它的后几位是噪声**：真正的准确度由上游 NTP 决定，可能是几十毫秒。把 `%N` 的后三位当「微秒级准确」是常见的误读。

### ⑥ 一次读有多贵

```c
#define _GNU_SOURCE
#include <stdio.h>
#include <time.h>
#include <sys/syscall.h>
#include <unistd.h>
static double sec(const struct timespec *t){ return t->tv_sec + t->tv_nsec/1e9; }
int main(void){
    const int N = 5000000; struct timespec a,b,t;
    clock_gettime(CLOCK_MONOTONIC,&a);
    for (int i=0;i<N;i++) clock_gettime(CLOCK_MONOTONIC,&t);
    clock_gettime(CLOCK_MONOTONIC,&b);
    printf("vDSO    : %6.1f ns/call\n",(sec(&b)-sec(&a))/N*1e9);
    clock_gettime(CLOCK_MONOTONIC,&a);
    for (int i=0;i<N/10;i++) syscall(SYS_clock_gettime,CLOCK_MONOTONIC,&t);
    clock_gettime(CLOCK_MONOTONIC,&b);
    printf("syscall : %6.1f ns/call\n",(sec(&b)-sec(&a))/(N/10)*1e9);
    return 0;
}
```

```text
vDSO    :  146.5 ns/call
syscall :  729.8 ns/call
```

同一程序连跑三次，vDSO 在 110–203 ns、syscall 在 595–1078 ns 之间波动，比值稳定在 **5–6 倍**。这个比值就是「跨界次数 × 单次成本」在时间 API 上的一个实例（全书主线③）：**能不进内核就不进内核。**

---

## 55.10 三列账：时间承诺了什么、收了多少、从哪开始失效

| 维度 | 承诺了什么 | 为此付了什么 | 从哪里开始失效 |
| --- | --- | --- | --- |
| **可比较的墙上时间** | 全世界对「同一时刻」有共同读数 | RTC + NTP/PTP 的同步成本；每台机器都要维护 | 不同步 / step / 时区混淆 → 日志对不上 |
| **单调的流逝** | 同一 boot 内时间只增，差值非负 | 只在单机单 boot 有效；不能跨机器比较 | 重启、跨机、容器共享宿主 uptime |
| **高分辨率** | 纳秒级读数 | 分辨率 ≠ 准确度；晶振漂 ppm；每次读有成本 | 把 `%N` 的小数位当准确度 |
| **全局顺序** | 分布式系统需要「谁先谁后」 | 逻辑钟 / 中心发号器 / TrueTime 的等待 | 用本地时间戳排序 → 因果倒置 |
| **闰秒可表示** | UTC 与地球自转对齐 | `time_t` 不连续；应用要处理 23:59:60 | 内核/应用假设「时间连续」→ 崩 |
| **虚拟化时间** | guest 看到一个连续的时间轴 | 依赖宿主 pvclock/TSC、迁移时的修正 | vCPU 抢占、迁移、时钟源降级 → 跳变 |

一句话收束：**时间买到的是「可比较」，付出的是「永远在同步」；一旦停止同步，它就退回成「每台机器各自的读数」。** 本篇列的所有事故，都是这句话的不同出口。

---

## 55.11 踩坑清单：现象 → 根因 → 定位 → 修

| 现象 | 根因 | 定位命令 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 日志乱序，「响应早于请求」 | 各机时钟偏差 + 按时间戳排序（§55.7） | `timedatectl`、`chronyc tracking`、`ntpq -p` | 统一 UTC + 记录偏移；排序改用 trace 因果 | 监控每台机器的 offset，超过阈值告警 |
| 锁提前过期 / 任务重复执行 | 到期时刻是墙上时间，被 step（§55.1、§55.4） | `journalctl -u chronyd` 看 step；对比两台机器的 `date -u` | TTL 由锁服务判定；加 fencing token | 禁止客户端用自己的钟算跨机 deadline |
| 耗时出现负值 / P99 突然变好 | 用 REALTIME 测间隔（§55.4） | 在耗时点打印两种钟的差值 | 改 `CLOCK_MONOTONIC` / `nanoTime` / `time.Since` | Code review 禁 `currentTimeMillis` 做差值 |
| 超时变成 584 年 | 无符号差值回绕（§55.4） | 打印 `uint64` 的差值与符号 | 先判负再转，或全程有符号 `duration` | 时间差类型统一用有符号 64 位 |
| cron 在 DST 重复 / 跳过 | 本地日历时间不唯一 / 不存在（§55.8） | `zdump -v <TZ>`、`date -d '<本地时刻>'` | 用 UTC 或显式时区；任务幂等 | 定时任务默认 UTC，夏令时前演练 |
| 限流窗口错乱 / 配额翻倍 | 窗口边界用 REALTIME 算（§55.4） | 打印窗口起止与 `now` | 窗口用 MONOTONIC；跨机配额用中心计数器 | 限流状态不落墙上时间 |
| 容器里 `MONOTONIC` 是 16 天 | 容器共享宿主时钟（§55.6） | `cat /proc/uptime`、`readlink /proc/self/ns/time` | 不要假设 MONOTONIC 从 0 开始；需要时用 time namespace | 容器内不跑 ntpd，时间同步交给宿主 |
| 闰秒窗口 CPU 100% / 服务 panic | 时间不连续 + 负时长（§55.5） | 查 `dmesg`、服务日志的时间戳 | 升级内核/库；用 smear 或 TAI；时长判负 | 全网统一闰秒策略，闰秒前演练 |

三行总结：**看到「负数」，先怀疑用了 REALTIME 算差值；看到「差几秒」，先查 NTP 是否刚 step；看到「重复/跳过」，先看是不是本地日历时间。**

---

## 55.12 一页速记

```text
【一句话】时间是读数不是事实；「几点」和「多久」是两个问题，必须用两个钟。

【四个钟】
  REALTIME        墙上时间，会跳；日志/证书/业务日期用它，但别用它算差值
  MONOTONIC       开机起算，不受 step 影响（但受 slew 频率修正）；耗时/超时/定时器
  BOOTTIME        同 MONOTONIC，但含 suspend；「30 分钟后叫我」用它
  MONOTONIC_RAW   原始硬件计数，不受 NTP；量晶振、查 NTP 改了什么

【三个数字】
  1 ppm = 86.4 ms/天 = 31.5 s/年         （1e-6 × 86400）
  500 ppm 追 24 s 要 13.3 小时           （24 / 500e-6）
  闰秒 smear：1 s / 20 h = 13.9 ppm      （最大偏差约 0.5 s）

【三条规范】
  ① 耗时/超时/定时器 → CLOCK_MONOTONIC（跨 suspend 用 BOOTTIME）
  ② 日志 → UTC + 偏移；跨机排序靠 trace，不靠时间戳
  ③ 分布式锁 → 锁服务判 TTL + fencing token，客户端不各算各的

【三个坑】
  用 REALTIME 算差值   → 回拨后 elapsed 为负（本机实测 -3.999902 s）
  无符号存负差值       → 回绕成 1.8e19 秒 ≈ 5849 亿年
  本地日历排日程       → DST 春季不存在、秋季触发两次
```

---

## 55.13 自检 3 题

**Q1.** `CLOCK_MONOTONIC` 在 NTP 调整后不会倒退，但 `CLOCK_REALTIME` 会。如果 NTP 只做 slew、不做 step，`CLOCK_REALTIME` 还会倒退吗？`CLOCK_MONOTONIC` 又会不会受这次 slew 影响？

<details><summary>答案</summary>

**只做 slew 时，`CLOCK_REALTIME` 不会倒退。** slew 不改当前读数，只改「一秒有多长」——它让时钟暂时跑快或跑慢几百 ppm，读数仍然单调前进，只是相邻两次读的差可能比真实经过的时间多或少一点点。

**`CLOCK_MONOTONIC` 会受影响。** 这是最容易被说错的一点：MONOTONIC 免疫的是 **step**，不是 **频率修正**。NTP 调频率时，内核把同一个修正同时作用到 MONOTONIC 上，所以它也会跟着快一点或慢一点（但不会跳）。本机实测 `MONOTONIC - RAW = +25.717 ms / 16.14 天 = +18.4 ppb`，这 25.7 ms 就是频率修正在 16 天里累积出来的。

所以严格的说法是：`CLOCK_MONOTONIC` **不倒退、不跳变，但速率会被 NTP 调整**；`CLOCK_MONOTONIC_RAW` 连速率都不调整，适合量晶振和做基准。要「完全不受 NTP 影响」，只有 RAW。
</details>

**Q2.** 一段代码用 `deadline = now + 30s` 做租约，`now` 取自本机 `CLOCK_REALTIME`。NTP 把这台机器的钟往回拨了 12 秒。① 在这台机器看来，租约晚了多少秒释放？② 如果改成用 `CLOCK_MONOTONIC` 存 `deadline`，跨机器还能用吗？③ 正确做法是什么？

<details><summary>答案</summary>

① **晚 12 秒。** 回拨后 `now` 比真实时间小 12 秒，`deadline - now` 就多出 12 秒，本地检查会认为自己还有 12 秒。但在**别的机器**看来（它们的钟是准的），`deadline` 早就到了——于是出现「A 认为自己还持有、B 认为已经过期」的双持有窗口，§55.1 的重复结算就是这么来的。

② **不能。** MONOTONIC 的起点是**各机器自己的开机时刻**，频率也各自略有差异，所以两台机器的 MONOTONIC 读数根本不在同一个坐标系里，相减没有意义。MONOTONIC 只能解决「同一台机器上的时长」，解决不了「跨机器的到期时刻」。

③ 正确做法有两层：**让锁服务用自己的时钟判定 TTL**（Redis 的 `PX`、etcd 的 lease、数据库的 `NOW()` 都是在服务端算），客户端只负责续租；再加一枚 **fencing token**（单调递增的整数），每次写存储都带上，存储端拒绝比已见过的更小的 token。这样即使旧持有者还活着，它的写也会被挡掉。**不依赖任何客户端的钟，才是真正的修法。**
</details>

**Q3.** 2012 年的闰秒为什么能让 Linux 机器 CPU 100%？smear 为什么能避免它，代价又是什么？

<details><summary>答案</summary>

**2012 年的根因是「时间不连续」遇上了「时间只会平滑前进」的假设。** 闰秒插入时，内核要把 `CLOCK_REALTIME` 拨 1 秒；高精度定时器（`hrtimer`）在重新计算到期时间时，拿到一个为负的时间差，于是「立即到期」被算成了「永远到期」或忙等，CPU 自旋到 100%。**真正出错的不是闰秒，是那段代码假设了「两个时刻之差一定非负」。**

**smear 避免它的办法是不插入这一秒**：让本地时钟在 20 小时里以 +13.9 ppm 的速度运行（`1 s / 72,000 s = 13.9 ppm`），把 1 秒摊平。时间轴始终连续，定时器看到的差值始终非负，所以不会触发那条路径。

**代价有三条**：① 期间本地时钟与 UTC 有界偏差，最大约 0.5 秒（单侧摊平则到 1 秒）；② 全网必须用**同一种** smear，否则 A 机房和 B 机房的日志在同一真实时刻能差出最多 1 秒，而且不报错；③ 需要额外的监控与文档，否则「为什么我的时间和 `time.gov` 差半秒」会变成一个反复出现的工单。**用可度量的偏差换不可度量的断裂，这就是这笔交易。**
</details>

---

## 55.14 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| `clock_gettime` 的 syscall 入口、vDSO 与 seqlock（本篇只给成本账） | → `base/21` |
| 一次请求从敲回车到屏幕像素，每一毫秒归到哪一层 | → `base/53` |
| perf / strace / tcpdump / bpftrace 各查哪层（时间问题的工具选择） | → `base/54` |
| 收口：位层 / 内核层 / 协议层三本账合订 | → `base/56` |
| Snowflake 的时钟回拨、三种处置与「有序/单调/可用」三角 | → [`comp/45`](../comp/45-distributed-id.md) |
| trace 里的时间戳、span 因果与日志关联（为什么时间戳不能当顺序） | → [`go/35`](../go/35-observability.md) |
| CPU 主频那个「时钟」：流水线、周期与频率 | → `base/10` |
| 虚拟化与特权级：VM exit、pvclock 与容器隔离 | → `base/15` |
| 调度器与 `load average`：为什么 vCPU 会被抢占 | → `base/25` |
