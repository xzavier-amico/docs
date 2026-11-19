# 35. 实例剖析：内存账本现场——RSS 只涨不降，到底涨在哪

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 操作系统：Linux 视角（实例篇） ｜ **编号**：35 / 56
> **本篇定位**：Part III 内存线的验伪出口。26 篇说「独占内存是页表 + 缺页维持的幻觉」，27 篇说「`malloc(100)` 大概率没向内核要内存」。本篇把这两句话按在同一个现场：一个 RSS 从 300 MB 涨到 1.5 GB 的服务上，用 `/proc`、`pmap`、`smaps`、`cgroup memory.stat`、`malloc_info` 五本账，把泄漏、碎片、页缓存、巨页、共享映射、统计口径六件事**逐一分账**，每一本账都给真实输出与「改前 / 改后」两列数字。读完的判据只有一个：**再看到「内存涨了」，你先问「谁持有」，而不是先问「哪里 malloc 了」。**
> **读法**：§35.1 先看事故形态与那句唯一真问题；§35.2 建立「六种含义」的分类；§35.3 是四口径与 `smaps` 逐行读法，**这一节必须动手对照自己的 `/proc/PID/smaps`**；§35.4 是五步排查流程，可以当 SOP 直接抄；§35.5 的四个实验是本篇的硬证据，建议按 A→B→C→D 顺序复现（峰值内存不超过 210 MB）；§35.6–§35.9 是结论、手册与速记。
> **不在本篇**：页表与缺页的硬件过程 → `base/26`；`malloc`/`brk`/`mmap`/OOM 的完整机制 → `base/27`；页缓存回写与 `fsync` 三层谎 → `base/29`；线程与 `clone` 的共享参数 → `base/24`；容器隔离的原语 → `base/34`；Go 分配器与 GC 的完整实现 → [go/15](../go/15-allocator.md)、[go/28](../go/28-gc.md)。

---

## 35.1 引子：一个真实形态的事故——300 MB 涨到 1.5 GB，`valgrind` 却说没事

某订单服务（下称 `order-svc`）是单实例常驻约 300 MB 的 C++ 有状态服务。一次发布后，RSS 在 36 小时内爬到 1.5 GB，随后被容器 OOM 杀掉；重启回到 300 MB，再过一天又爬回去。压测 20 分钟可复现。团队的第一反应是泄漏，第二反应是上 `valgrind`：

```bash
$ which valgrind
$                       # 开发机和生产机都没有装；这不是罕见情况
```

开发同学在本地跑了一遍，报告是 `definitely lost: 0 bytes`；争论转向「是不是内核记账有问题」，又花了两周，直到有人把 `/proc/PID/smaps_rollup` 和 `malloc_info` 摆在一起：

| 时刻 | 动作 | 看到什么 |
| --- | --- | --- |
| T+36h | 告警 | RSS 1.5 GB，容器 OOM；重启回 300 MB，36h 后复现 |
| T+2d | 本地 `valgrind` | `definitely lost: 0 bytes` |
| T+9d | `smaps_rollup` + `malloc_info` | `Private_Dirty` 1.5 GB，但 `uordblks` 320 MB、`fordblks` 1.1 GB |

真相不是泄漏，而是**分配器持有**：16 个工作线程各自从 glibc 拿到一个 arena，每个 arena 的高水位被线程里的一个长期缓存块钉住；`free()` 只是把页还给 arena 的空闲链表，页表项还在，RSS 就还在。`valgrind` 看的是「有没有不可达的堆块」，它当然看不见「可达的空闲页被谁攥着」。

这就是本篇要反复用的一句话：

> **内存问题的答案永远在「谁持有」，不在「谁分配」。**
> 「谁分配」只能告诉你调用点；「谁持有」才能解释这块内存为什么**不能被回收**。

同一句「RSS 涨了」，至少有六种物理含义。它们在同一台机器上可以同时发生，而 `pmap`、`smaps`、`/proc/PID/status` 这三本账，恰好能把它们分开：

```
分配动作                持有者                        记在哪本账
─────────────────────  ────────────────────────────  ─────────────────────────
malloc → arena → VMA    进程页表（私有匿名页）        smaps Private_Dirty / status VmRSS
mmap 文件 → page cache   inode 的 page cache + PTE    smaps Shared_Clean / cgroup file
THP → 2MB huge page     进程页表（匿名巨页）          smaps AnonHugePages
read() → page cache     内核页缓存（不进程页表）      /proc/meminfo Cached / cgroup file
共享库 / shmem         多个进程页表共同映射           smaps Pss / Shared_Clean
只 mmap 不碰            VMA 预留，无物理页            VmSize，不是 RSS
```

**本篇量到的主线**：以主线③「每层抽象的单价 = 跨界次数 × 单次成本」为主——RSS 的每一页都对应一次缺页、一次 `mmap`、一次 `madvise` 或一次回收；再叠加主线①「没有魔法，只有位与约定」——所谓「进程独占的内存」只是页表里的一组 PTE 约定。

---

## 35.2 ① 它是什么：「内存涨了」这句话其实有六种含义

先给结论：**RSS 不是「活对象的总和」，它是「当前驻留在物理内存里的页表项集合」。** 一个字节的活对象可以对应 0 个驻留页（在 swap 或被压缩），也可以对应 2 MB 的驻留页（THP）；一块早已 `free` 的内存可以继续驻留（arena 持有），一块从未被进程 `malloc` 的文件页也可以记在系统的 `Cached` 里。

六种含义的判别表：

| # | 你嘴里的说法 | 物理上是什么 | 第一本账看什么 | 关键判据 | 处置方向 |
| --- | --- | --- | --- | --- | --- |
| ① | 「泄漏了」 | 不可达或长期可达的匿名页，随请求量单调增长 | `smaps` 的 `Anonymous`/`Private_Dirty` + `malloc_info` 的 `uordblks` | 空闲后不回落；`uordblks` 与 RSS 同向增长 | 修引用链 / 周期释放 |
| ② | 「碎片了」 | 空闲页存在，但尺寸不合、位置不对，不能复用或归还 | `malloc_info` 的 `fordblks` / `system`，`malloc_trim` 前后 RSS | `fordblks` 很大、`uordblks` 不大、RSS 高 | 定长池 / size class / `malloc_trim` |
| ③ | 「缓存吃了」 | 文件页在 page cache，可回收，不在进程 RSS | `/proc/meminfo` 的 `Cached`、cgroup 的 `file` | `MemAvailable` 基本不降；`fadvise(DONTNEED)` 后立刻降 | 不用治；要控就 `fadvise` / `memory.max` |
| ④ | 「巨页放大了」 | THP 以 2 MB 为单位记账，碰 1 字节占 2 MB | `smaps` 的 `AnonHugePages`、`/proc/vmstat` 的 `thp_*` | RSS 以 2 MB 为阶梯跳；`AnonHugePages` > 0 | `MADV_NOHUGEPAGE` / 调 `khugepaged` |
| ⑤ | 「映射变多了」 | 共享库、`mmap` 文件、shmem 被多个进程映射 | `smaps` 的 `Pss`/`Shared_Clean` | 各进程 RSS 都涨，但 `Pss` 之和不涨 | 用 PSS 记账；看 mapcount |
| ⑥ | 「统计口径不对」 | VSS/RSS/PSS/USS、host/container、`free` 的 buff/cache 各说各话 | `status` 的 `VmSize`/`VmRSS`、cgroup `memory.current` | 同一时刻两个数差一个量级 | 先统一「看哪个账」 |

### 为什么这六种必须分开

因为它们**互相伪装**：

- 页缓存（③）让 `free -m` 的 `buff/cache` 和容器 `memory.current` 涨，却不进进程 RSS；运维看到「内存 95%」就重启，缓存重新读入，问题原样回来。
- 碎片（②）让 RSS 高而 `valgrind` 干净：所有块都「可达」——可达于 arena 的空闲链表，不在任何业务指针上。
- 巨页（④）让一个只碰了 32 字节的数组占 12 MB；`pmap` 只给总数，只有 `AnonHugePages` 能揭穿它。
- 共享映射（⑤）让 10 个进程各报 200 MB、合计 2 GB，而物理内存只有 200 MB；口径（⑥）则让「`VmPeak` 1 GB」被当成故障，而同一时刻 `VmRSS` 只有 4 MB。

所以本篇的流程不是「找泄漏」，而是**先分账，再定位**。分账的抓手就是下一节的四个口径与 `smaps` 的每一行。

---

## 35.3 ② 物理形态：VSS / RSS / PSS / USS 四个口径，与 `smaps` 的每一行

### 四个口径

四个口径的定义不是命名习惯，而是**页表与共享关系**的直接推论：

| 口径 | 定义 | 是否可跨进程相加 | 回答什么问题 |
| --- | --- | --- | --- |
| **VSS**（`VmSize`） | 所有 VMA 的 `Size` 之和，含从未触碰的预留 | 否 | 「地址空间订了多大」——不是内存 |
| **RSS**（`VmRSS`） | 当前驻留的页 × 页大小，含共享页的**全额** | **否** | 「这个进程现在摸得到多少物理页」 |
| **PSS** | 每个共享页按映射它的进程数**均摊**后求和 | **是** | 「这些页里有多少该算它的账」 |
| **USS** | `Private_Clean + Private_Dirty`（+ 私有 hugetlb） | 是 | 「只属于它、别人动不了的页」 |

三行关系就够：

```
RSS_i = USS_i + (该进程分摊到的共享页全额)
PSS_i = USS_i + Σ(共享页大小 / 映射该页的进程数)
Σ_i PSS_i = 这些进程映射到的物理页总量（每页只算一次）
```

`USS ≤ PSS ≤ RSS` 恒成立；`RSS` 在多个进程之间会重复计数，`PSS` 不会。**「容器内存超了」看的是 cgroup 的 `memory.current`，它既不是 RSS 也不是 PSS，而是这个 cgroup 里所有进程的 anon + file + kernel 的电荷。**

### 一份真实的 `smaps` 片段

下面是在 1.9 GB 内存、glibc 2.36、THP=`always` 的机器上，对一个「`malloc(24 MB)` + `mmap(32 MB 文件)` + 每 MB 读一页」的进程抓的 `/proc/PID/smaps`。为了对照，只保留本节要讲的字段：

```
7f9222800000-7f9224800000 r--s 00000000 fe:01 429069   /tmp/memlab/big64.bin
Size:              32768 kB
Rss:                1924 kB
Pss:                1924 kB
Shared_Clean:          0 kB
Private_Clean:      1924 kB
Private_Dirty:         0 kB
Swap:                  0 kB

7f922518d000-7f9226991000 rw-p 00000000 00:00 0
Size:              24592 kB
Rss:               24588 kB
Pss:               24588 kB
Shared_Clean:          0 kB
Private_Clean:         0 kB
Private_Dirty:     24588 kB
Anonymous:         24588 kB
AnonHugePages:         0 kB
Swap:                  0 kB
```

逐行读：

| 字段 | 含义 | 这段输出说明什么 |
| --- | --- | --- |
| `Size` | 这个 VMA 的虚拟长度 | 文件映射 32 MB；`malloc(24 MB)` 的匿名映射 24,592 KB（24 MB + 16 KB 头） |
| `Rss` | 已驻留的页，含共享页全额 | 文件段只驻留 1,924 KB——**不是 32 MB**；匿名段驻留 24,588 KB，比 `Size` 少 4 KB（最后一页没碰） |
| `Pss` | 按共享进程数均摊 | 单进程时 `Pss = Rss`；若同一文件被 3 个进程映射，`Pss` 会变成 1/3（见下表） |
| `Shared_Clean` | 被多个进程映射的干净页 | 文件段这里是 0——**单进程映射时，文件页也会被记成 `Private_Clean`** |
| `Private_Clean` | 只被本进程映射的干净页 | 文件页 1,924 KB 全在这里；`private` 描述的是「当前共享计数 = 1」，不是 `MAP_PRIVATE` |
| `Private_Dirty` | 只被本进程映射的脏页 | 匿名段 24,588 KB：这是「真实需求」最硬的一个数 |
| `Anonymous` | 匿名页总量（含 THP） | 文件段的 `Anonymous` 为 0；匿名段等于 `Private_Dirty` |
| `AnonHugePages` | 其中由 THP 提供的大页 | 这次是 0（THP 分配失败，下一轮可能变成 12 MB，见实验 E） |
| `Swap` | 被换出的页 | 本机无 swap，恒为 0；有 swap 时它才是「被挤出去的真实需求」 |

三个最容易读错的点：

1. **`Size` 不是内存。** 32 MB 文件映射只驻留 1.9 MB；`malloc(1 GB)` 一页不碰，RSS 仍是 1 MB 级。
2. **文件页只碰 32 个点，RSS 却是 1.9 MB。** fault-around 在缺页时把周围最多 16 页一起映射：32 × 16 × 4 KB ≈ 2 MB，实测 481 页。**RSS 的粒度是页，不是访问粒度。**
3. **`Private` 不等于 `MAP_PRIVATE`。** 一个页只要当前只被一个地址空间映射，就算 private；第二个进程映射同一文件页后，同样的页在两个进程里都变 `Shared_Clean`，`Pss` 各减半。

### 把「共享」量出来：同一份文件，1 个进程 vs 3 个进程

同一份 32 MB 文件、同样每 MB 读一页，分别在 1 个和 3 个进程里映射，抓其中一个进程的 `smaps`：

| 字段 | 单进程 | 三进程（每个进程） | 三进程合计 |
| --- | --- | --- | --- |
| `Rss` | 1,924 KB | 1,924 KB | 5,772 KB（**重复计数**） |
| `Pss` | 1,924 KB | 641 KB | 1,923 KB（**可加总**） |
| `Shared_Clean` | 0 | 1,924 KB | — |
| `Private_Clean` | 1,924 KB | 0 | — |

物理上只有约 1.9 MB 的文件页，`RSS` 却报了 5.8 MB。这就是「10 个进程各报 200 MB 共享库」的由来。**判断「是不是真的占了这么多物理内存」，永远看 PSS 或 cgroup 的电荷，不看 RSS 的裸和。**

`pmap -x` 是 `smaps` 的快速版，三列数字是 `Kbytes / RSS / Dirty`：

```
00007f95b8600000   32768    2048       0 r--s- big64.bin
00007f95ba600000    8192    8192    8192 rw---   [ anon ]
00007f95bafdb000   24592   24588   24588 rw---   [ anon ]
00007fffa445b000     132      12      12 rw---   [ stack ]
```

第一行 `Kbytes=32768` 对应 `Size`，`RSS=2048`，`Dirty=0`；第三行是 `malloc(24 MB)` 的匿名段。`pmap` 适合一眼看出「哪一段在涨」，但**没有 PSS、没有 Shared/Private 拆分**，要分账必须回到 `smaps`。

---

## 35.4 ③ 走一遍：从「RSS 涨了」到「定位到代码行」的五步

这一步的目标不是「找一个大数字」，而是**按顺序排除五种不配叫泄漏的东西**。每一步都给出命令与判据，任何一步命中就停在那一步，不要跳。

### 第 1 步：先看总账，确认「涨的是哪个数」

```bash
PID=$(pgrep -f order-svc | head -1)
grep -E 'VmPeak|VmSize|VmRSS|VmHWM|VmData|VmStk|VmPTE|VmSwap' /proc/$PID/status
cat /proc/$PID/smaps_rollup
```

判据：

- `VmHWM ≫ VmRSS`：曾经高过、现在回落了——这是**历史峰值**，不是当前泄漏；
- `VmSize ≫ VmRSS`：地址空间订得大，物理页没跟上——先怀疑 arena 预留、`mmap` 未触碰、THP 失败（实验 D 的 1098 MB vs 4 MB）；
- `VmData ≈ VmRSS` 且两者一起单调涨：真实匿名需求在涨，进入第 2 步；
- `VmSwap > 0` 或 `smaps` 的 `Swap` 大：工作集已经超过内存，性能问题会先于 OOM 出现；
- `smaps_rollup` 的 `AnonHugePages > 0`：RSS 里有 2 MB 粒度的账（实验 E）。

真实例子（实验 A，200 轮后）：`VmPeak = VmRSS = VmHWM = 207 MB`，一路涨、从未回落；`VmData 205,824 KB ≈ VmRSS`，涨的是私有匿名页，不是文件映射。

### 第 2 步：看 `maps`/`smaps`，确认「哪一段在涨」

```bash
pmap -x $PID | sort -k3 -n | tail -10
# 按 RSS 排序每个 VMA，并打印它的名字
awk '/^[0-9a-f]+-[0-9a-f]+ /{h=$0} /^Rss:/{print $2, h}' /proc/$PID/smaps \
  | sort -rn | head -10
```

判据：

- `[heap]` 或一个大的匿名段线性涨：`malloc`/`brk` 路径，进第 4 步；
- 许多 64 MB 的匿名段：glibc 的 arena（实验 D），不是业务对象；
- 一个文件 VMA 的 `Rss` 涨而 `Anonymous=0`：page cache/mmap，进第 3 步；
- `[stack]` 或每线程 8 MB 的匿名段涨：线程栈/递归，和堆无关。

### 第 3 步：区分匿名页与文件页——这是「真需求」与「可回收」的分界

```bash
grep -E '^(Rss|Pss|Anonymous|Private_Dirty|Shared_Clean|Swap|AnonHugePages)' \
  /proc/$PID/smaps_rollup
CG=/sys/fs/cgroup/$(cat /proc/$PID/cgroup | cut -d: -f3)
grep -E '^(anon|file|kernel|slab|pagetables|file_dirty|file_writeback) ' \
  $CG/memory.stat
```

判据：

- `Anonymous` 与 cgroup `anon` 同向涨：真实匿名需求，可能是泄漏或碎片；
- `file` 涨而 `anon` 平：page cache 或文件映射，**可回收**；`memory.current` 高不等于要 OOM；
- `kernel + slab + pagetables` 涨：内核对象（dentry/inode、页表本身），不在进程 RSS 里；
- `file_dirty` / `file_writeback` 高：脏页正在回写，可能有 IO 抖动，但仍是可回收账。

实验 C 实测：读 64 MB 文件，进程 RSS 只涨 1 MB，cgroup `file` 涨 64.0 MiB，`anon` 始终 0。**同一件事，进程账上是 1 MB，cgroup 账上是 64 MB。**

### 第 4 步：看分配器——`uordblks` 与 `fordblks` 把「泄漏」和「碎片」分开

```bash
# 方式一：程序里周期打印（推荐，零侵入地看趋势）
malloc_info(0, stdout);        /* glibc：每个 arena 一个 <heap> */
malloc_stats();                /* 更短，输出到 stderr */

# 方式二：gdb 挂上去调一次（注意：会暂停进程）
gdb -p $PID -batch -ex 'call (int)malloc_info(0, stderr)' 2>/dev/null | head -40

# 方式三：mallinfo2（单 arena 下够用；多线程以 malloc_info 为准）
struct mallinfo2 m = mallinfo2();
printf("arena=%zu uordblks=%zu fordblks=%zu\n", m.arena, m.uordblks, m.fordblks);
```

判据（三个数就够）：

| 观察 | 结论 |
| --- | --- |
| `uordblks` 随时间单调涨，`fordblks` 很小 | 真泄漏：活块在涨 |
| `uordblks` 平稳，`fordblks`/`system` 很大，RSS 高 | 碎片或 arena 持有：空闲页没还 |
| `system` 很小但 RSS 高 | 不是堆：去看 page cache / THP / 栈 |
| `malloc_info` 里 `<heap>` 数量 = 线程数 | 每个线程一个 arena，准备做实验 D |

实验 D 的真实 `malloc_info`（16 线程、默认 arena、关闭 trim）：

```
<heap nr="0">
<system type="current" size="8437760"/>
<heap nr="1">
<system type="current" size="8396800"/>
...
<heap nr="15">
<system type="current" size="8396800"/>
```

16 个 heap，每个 8.0 MB——而业务存活对象只有 512 KB。**持有者是 16 个 arena，不是 16 个业务对象。**

### 第 5 步：采样与插桩——把「哪个调用点」钉死

前三步回答「哪一类」，这一步回答「哪一行」。四种手段按侵入性从低到高：

```bash
# ① 缺页采样：谁在不停触碰新页
perf record -e page-faults -g -p $PID -- sleep 30 && perf report --stdio

# ② 分配点采样（需要 root 与符号）
bpftrace -e 'uprobe:/lib/x86_64-linux-gnu/libc.so.6:malloc
             { @[ustack] = count(); }'

# ③ LD_PRELOAD 拦截 malloc/free，记录 size→ptr 与调用栈
LD_PRELOAD=./libmalloc_trace.so ./order-svc

# ④ 程序内埋点：每 N 次请求打印一次 malloc_info + smaps_rollup
```

判据：

- `inuse`（在用的字节）随请求量涨、`alloc` 也涨：真泄漏，按调用栈定位；
- `alloc` 涨但 `inuse` 平：高频分配/释放的抖动，可能伴随碎片；
- 堆的 `inuse` 平、RSS 涨：不在堆里——回到第 3 步看 file/THP/arena；
- Go 服务把 `-inuse_space` 与 `-alloc_space` 两个 pprof 都看：前者是「现在持有」，后者是「历史累计」，只看后者会把正常的高频分配误判成泄漏。

---

## 35.5 ④ 改一行：四个实验，把「涨」拆成四种持有者

四个实验都在同一台机器上真跑：2 核、1.9 GB 内存、无 swap、glibc 2.36、THP=`always`。每个实验都用 `ulimit -v` 或 cgroup `memory.max` 兜底，峰值不超过 210 MB。

### 实验 A：泄漏——每轮 `malloc(1 MB)` 不 `free`，200 轮

```c
static long rss_kb(void) {              /* 读 /proc/self/status 的 VmRSS */
    FILE *f = fopen("/proc/self/status", "r");
    char l[256]; long v = -1;
    while (f && fgets(l, sizeof l, f))
        if (!strncmp(l, "VmRSS:", 6)) sscanf(l + 6, "%ld", &v);
    if (f) fclose(f); return v;
}
int main(void) {
    size_t sz = 1UL << 20;
    printf("0,%ld\n", rss_kb());
    for (int i = 1; i <= 200; i++) {
        char *p = malloc(sz);
        if (!p) return 1;
        memset(p, 0x5a, sz);            /* 每页都碰；只 malloc 不碰只涨 VSS */
        if (i % 40 == 0) printf("%d,%ld\n", i, rss_kb());
    }
    printf("final,%ld\n", rss_kb());
    return 0;
}
```

真实输出（RSS 单位 KB）：

```
round    VmRSS    增量/轮
0         1,392      —
40       42,632    1,031.0
80       83,752    1,028.0
120     124,872    1,028.0
160     165,992    1,029.0
200     207,112    1,028.6
```

每轮 1 MB 请求涨 1,028.6 KB；多出的 4.6 KB 是 chunk 头、页对齐与页表。200 轮共 200 MB 载荷、RSS 涨 205.7 MB（0.45%）。**斜率恒定、无平台、不随空闲回落——这是「真需求」的教科书形状。**

改前 / 改后：

| 版本 | 200 轮后 RSS | `VmHWM` | cgroup `memory.peak` | 每轮增量 |
| --- | --- | --- | --- | --- |
| 每轮 `malloc` 不 `free` | 207,112 KB | 207,032 KB | 201.5 MiB | +1,028.6 KB |
| 每轮 `malloc` 后 `free` | 1,472 KB | 1,472 KB | 336 KiB | 0 |

改后的程序只动了一行（`free(p)`），RSS 从 207 MB 回到 1.5 MB。注意 cgroup 的峰值也一起回落——**真泄漏在每一本账上都会留下同向的印子**，这是它区别于碎片、缓存、THP 的地方。

### 实验 B：碎片——随机尺寸 alloc/free 交替，总量不变

做法：先分配 2,048 块、每块 4–32 KB 随机（合计约 36 MB）；**隔一个释放一个**，留下 1,024 个 4–32 KB 的空洞；再用 32 KB 请求回填一半。对照组改成定长 32 KB，其他动作完全相同。

```c
#define NB 2048
int main(int argc, char **argv) {
    int fixed = argv[1][0] == 'f';
    void *v[NB] = {0}; srand(35);
    for (int i = 0; i < NB; i++) {
        size_t s = fixed ? 32*1024 : (size_t)(4 + rand() % 29) * 1024;
        v[i] = malloc(s); memset(v[i], 1, s);
    }
    report("alloc 2048");
    for (int i = 0; i < NB; i += 2) { free(v[i]); v[i] = NULL; }
    report("free every other");
    for (int i = 0; i < NB; i += 2) {        /* 用同尺寸回填空洞 */
        v[i] = malloc(32*1024); memset(v[i], 2, 32*1024);
    }
    report("refill 32KB");
}
```

真实输出（RSS / 分配器在用的 `uordblks`，单位 MB）：

```
随机 4~32KB：  alloc 2048   rss=38 live=36
                free 一半   rss=38 live=18     ← 释放了 18MB，RSS 一 MB 没降
                refill 32KB rss=69 live=50     ← 多出 19MB 是塞不进空洞的新页

定长 32KB：    alloc 2048   rss=65 live=64
                free 一半   rss=65 live=32
                refill 32KB rss=65 live=64     ← 空洞被同尺寸请求复用
```

改前 / 改后：

| 版本 | free 一半后 | 回填后 | `RSS / 有效载荷` | cgroup 峰值 |
| --- | --- | --- | --- | --- |
| 随机 4–32 KB | 38 MB / 18 MB | 69 MB / 50 MB | **1.38** | 68.1 MiB |
| 定长 32 KB 池 | 65 MB / 32 MB | 65 MB / 64 MB | **1.02** | 64.5 MiB |

两个结论：

1. **`free` 不降 RSS 是正常的**：随机组释放一半后 RSS 仍是 38 MB。被释放的页夹在存活块之间，`free()` 只能把它们挂回 arena 的空闲链表，不能 `brk` 收缩——**持有者是「夹住空洞的存活块」，不是「忘记 free」**。
2. **碎片可以量化**：同样回填 32 KB，随机组每 50 MB 有效载荷付 19 MB RSS（1.38 倍），定长池只付 1 MB（1.02 倍）。请求尺寸继续漂移，1.38 还会涨。

边界：把所有块都 `free` 掉、让堆顶连续空闲时，glibc 会把内存还给内核（实测回落到 1 MB 量级）。**「free 了不还」只发生在「释放的页不在堆顶、或被 arena 钉住」时**。

### 实验 C：页缓存——读一个大文件，RSS 与 page cache 是两本账

做法：把 64 MB 文件先 `posix_fadvise(DONTNEED)` 归零，再用三种方式读：`read()` 到 1 MB 缓冲区、`mmap` 后逐页触碰、以及两者各自 `DONTNEED` 之后。每一步同时读进程 RSS、`/proc/meminfo` 的 `Cached`、cgroup 的 `file`。

```bash
./c_cache big64.bin d          # 先读一遍再 fadvise(DONTNEED)，把账归零
bash -c 'echo $$ > /sys/fs/cgroup/memlab/cgroup.procs; exec ./c_cache big64.bin r'
grep -E '^(anon|file) ' /sys/fs/cgroup/memlab/memory.stat
bash -c 'echo $$ > /sys/fs/cgroup/memlab/cgroup.procs; exec ./c_cache big64.bin m'
grep -E '^(anon|file) ' /sys/fs/cgroup/memlab/memory.stat
```

真实数字（RSS 为进程账，`Cached` 为系统账，`file` 为 cgroup 账）：

| 步骤 | 进程 RSS | `/proc/meminfo` `Cached` | cgroup `file` | `MemAvailable` |
| --- | --- | --- | --- | --- |
| `fadvise` 后（基线） | 2 MB | 338.1 MiB | 0 | 339.6 MiB |
| `read()` 读完 64 MB | 2 MB | 402.3 MiB | 64.0 MiB | 360.8 MiB |
| `fadvise(DONTNEED)` | 2 MB | 346.7 MiB | 0 | 362.1 MiB |
| `mmap` 后逐页触碰 | 65 MB | 407.3 MiB | 64.0 MiB | 370.8 MiB |
| `madvise(DONTNEED)` | 1 MB | 407.4 MiB | 64.0 MiB | 370.4 MiB |

四个判据：

- **`read()`**：RSS 只从 1 MB 涨到 2 MB，64 MB 全在 `Cached`/cgroup `file` 里；`MemAvailable` 不降反升，因为它把 page cache 当可回收。
- **`fadvise`**：cgroup `file` 从 64.0 MiB 精确回到 0；系统 `Cached` 回落 55.6 MiB。**这是「清缓存」唯一不重启服务的做法。**
- **`mmap`**：RSS 涨 64 MB，但 cgroup `anon` 仍是 0——**涨的是同一批文件页的 PTE，不是新内存**。
- **`madvise`**：RSS 从 65 MB 回到 1 MB，`file` 仍是 64.0 MiB——`madvise` 只拆页表，不丢 page cache。

改前 / 改后：

| 做法 | RSS | cgroup `file` | 结论 |
| --- | --- | --- | --- |
| 把 `Cached` 当泄漏，扩容/重启 | 2 MB | 64 MiB | 误判，重启后缓存重新读入 |
| `posix_fadvise(DONTNEED)` | 2 MB | 0 | 缓存归还，不动业务 |
| 把 mmap 后的 RSS 当泄漏 | 65 MB | 64 MiB | 误判，`anon` 为 0 |
| `madvise(DONTNEED)` | 1 MB | 64 MiB | 只释放 PTE，缓存仍可复用 |

### 实验 D：arena——多线程分配，`MALLOC_ARENA_MAX=1` 前后

做法：16 个线程，每个线程分配 8 MB（256 块 × 32 KB），然后只保留第一块 32 KB 作为「线程缓存」，其余全部 `free`；所有线程在屏障处对齐后再退出。glibc 在 2 核机器上 `arena_max = 8 × 2 = 16`，正好一线程一个 arena。

```c
static void *worker(void *arg) {
    long id = (long)arg;
    char *p[NCH];
    for (int i = 0; i < NCH; i++) { p[i] = malloc(32*1024); memset(p[i], 1, 32*1024); }
    anchor[id] = p[0];                  /* 每个线程留一个 32KB 缓存块 */
    for (int i = 1; i < NCH; i++) free(p[i]);
    pthread_barrier_wait(&bar);         /* 全部到位后再量 RSS */
    pthread_barrier_wait(&bar2);
    return NULL;
}
```

真实输出（`heaps` 来自 `malloc_info` 的 `<heap>` 个数）：

```
默认：          heaps=16  vmsize=1098MB  rss=129MB  peak=135,467,008 (129.2MiB)
ARENA_MAX=1：   heaps=1   vmsize=150MB   rss=22MB   peak=22,650,880  (21.6MiB)
```

这组数字是在 `MALLOC_TRIM_THRESHOLD_=1 GiB`（关闭自动 trim）下测的，原因必须讲清楚：**glibc 2.36 在默认 trim 下会把空闲页 `madvise` 掉，arena 的高水位不一定留在 RSS 里**。同一程序、默认 trim 下再测一次：

| 条件 | arena 数 | `VmSize` | 线程结束后 RSS | cgroup `memory.peak` |
| --- | --- | --- | --- | --- |
| 默认 trim 关 | 16 | 1,098 MB | **129 MB** | 129.2 MiB |
| `ARENA_MAX=1`，trim 关 | 1 | 150 MB | **22 MB** | 21.6 MiB |
| 默认 trim 开 | 16 | 1,090 MB | 4 MB | 28.9 MiB |
| `ARENA_MAX=1`，trim 开 | 1 | 157 MB | 28 MB | 41.6 MiB |

三个反直觉的结论：

1. **arena 的账首先是 VSS**：16 个 arena 各预留 64 MB（16 × 64 = 1,024 MB），加主映射与库，`VmSize` 合计 1,098 MB，RSS 只有 129 MB。`VmPeak` 大不是问题。
2. **`ARENA_MAX=1` 能否省 RSS 取决于 trim**：trim 关时 129 → 22 MB（5.9 倍）；trim 开时同一开关反而让稳态 RSS 从 4 MB 涨到 28 MB——所有线程挤进一个 arena，`free` 出的碎片互相不可复用。**把它当 RSS 开关是迷信。**
3. **谁持有**：业务活对象只有 512 KB，却钉住 129 MB。持有者是「每线程 32 KB 锚点 + 它所在的 8.4 MB arena」——锚点在堆底，arena 就无法整体收缩。先找锚点（thread-local cache、连接池、日志缓冲），再谈 `ARENA_MAX`，最后才是 `malloc_trim`。

### 实验 E（补充）：巨页——碰 32 个字节，RSS 涨 12 MB

THP=`always` 时，匿名页的最小记账单位可能是 2 MB。做法：`mmap` 64 MB 匿名区，每 2 MB 只写 1 字节（共 32 字节），分别开 `MADV_HUGEPAGE`、`MADV_NOHUGEPAGE`，再对照「每 4 KB 写 1 字节」的密集组。

```c
char *p = mmap(NULL, 64UL<<20, PROT_READ|PROT_WRITE,
               MAP_PRIVATE|MAP_ANONYMOUS, -1, 0);
if (mode == 0) madvise(p, len, MADV_HUGEPAGE);
if (mode == 1) madvise(p, len, MADV_NOHUGEPAGE);
for (size_t i = 0; i < len; i += (mode == 2 ? 4096 : 2UL<<20)) p[i] = 1;
```

真实输出：

```
mode=0 THP         touched=32B     rss=13,768KB  anon_huge=12,288KB
mode=1 NOHUGEPAGE  touched=32B     rss= 1,496KB  anon_huge=0KB
mode=2 每 4KB 1B    touched=16,384B rss=66,896KB  anon_huge=4,096KB
```

改前 / 改后：

| 做法 | 触碰字节 | RSS | `AnonHugePages` | RSS 增量（基线 1.4 MB） |
| --- | --- | --- | --- | --- |
| THP on，每 2 MB 写 1 B | 32 | 13,768 KB | 12,288 KB | **+12.3 MB** |
| `MADV_NOHUGEPAGE`，同 | 32 | 1,496 KB | 0 | +0.1 MB |
| THP on，每 4 KB 写 1 B | 16,384 | 66,896 KB | 4,096 KB | +65.4 MB |

注意：RSS 的 2 MB 阶梯不是泄漏，只是记账粒度；且 THP 分配会失败——同一程序在内存碎片化时 `AnonHugePages` 可以是 0，于是「RSS 尖刺」忽高忽低。`MADV_NOHUGEPAGE` 让行为可预测，代价是丢失大页的 TLB 收益。

---

## 35.6 ⑤ 它证伪了谁：四句流行话，四种反例

### ① 「RSS 涨就是泄漏」

反例：实验 C 的 mmap 路径，RSS 从 1 MB 涨到 65 MB，cgroup `anon` 始终 0，`file` 64.0 MiB；实验 E 只碰 32 个字节，RSS 涨 12 MB。**RSS 只是「驻留页表项」的计数，它同时包含匿名页、文件页、共享页和巨页。** 真正的判据是「anon 是否单调涨 + 空闲后是否回落 + `uordblks` 是否同向」，不是 RSS 一个数。

### ② 「`free()` 了内存就该还」

反例：实验 B 随机组释放一半（18 MB），RSS 从 38 MB 纹丝不动；实验 D 业务只剩 512 KB，RSS 129 MB。`free()` 的语义是「把块还给分配器」，不是「把页还给内核」；页要回到内核，得让空闲区连成片、位于堆顶，或显式 `malloc_trim`/`madvise`。**「还给了谁」比「有没有 free」重要。**

### ③ 「`valgrind` 没报就没问题」

`valgrind` 的 memcheck 模型里只有「堆块 + 指针可达性」：它抓 `definitely lost` / `indirectly lost`，对以下四类**结构上不可见**：

- page cache 与文件映射（实验 C）：不是堆块；
- 碎片与 arena 保留（实验 B/D）：块已 `free`，只是没还给内核；
- THP（实验 E）：不是 `malloc` 的块；
- `still reachable` 的全局缓存：默认不算错误。

这台 1.9 GB 的机器上 `which valgrind` 就是空的——**生产机没有 valgrind 是常态，`/proc` 三本账才是常备工具**。反过来，实验 A 那种真泄漏它一定能抓；**报干净只能证明「不是①」，不能证明「没有内存问题」。**

### ④ 「`VmPeak` 大是问题」

反例：实验 D 默认 trim 开的组，`VmSize` 1,090 MB、`VmRSS` 4 MB。`VmSize` 是地址空间的预订额，Linux 允许 overcommit，预订不等于物理页。**该看的是 `VmHWM`（峰值 RSS）与 `VmRSS` 的差**：`VmHWM ≫ VmRSS` 说明「曾经高、现在回落」，那是一次性峰值或缓存；`VmHWM ≈ VmRSS` 且持续上涨才是真需求（实验 A）。

### 顺手证伪：「RSS 高就会 OOM」

OOM killer 与 cgroup OOM 看的是「回收之后仍然不够」的 anon + 内核不可回收部分，不是 RSS 裸值。实验 C 的 65 MB RSS 里 64 MB 是可回收文件页；实验 A 的 200 MB 全是 `Private_Dirty`、无 swap 可退，才是 OOM 的正选目标。

---

## 35.7 生产排查手册：现象 → 先看哪个文件 → 结论方向

| 现象 | 先看 | 判据 | 结论方向 |
| --- | --- | --- | --- |
| RSS 单调涨，重启回落 | `/proc/PID/status` 的 `VmHWM/VmRSS`；`smaps_rollup` 的 `Anonymous`；`malloc_info` 的 `uordblks` | 三者同向涨；空闲不回落 | 泄漏（实验 A）；按第 5 步钉调用点 |
| RSS 高但 `VmHWM` 已回落 | `VmHWM` vs `VmRSS` | `VmHWM ≫ VmRSS` | 历史峰值/一次性缓存，不是当前泄漏 |
| RSS 高、`uordblks` 低 | `malloc_info` 的 `fordblks/system`；`malloc_trim(0)` 前后 | 空闲块大、RSS 不降 | 碎片/arena（实验 B/D）；定长池、trim、arena 上限 |
| 容器 `memory.current` 接近 `memory.max`，进程 RSS 不高 | `/sys/fs/cgroup/.../memory.stat` 的 `file`；`memory.events` | `file` 占大头；`high/max` 计数涨 | 页缓存（实验 C）；可回收，必要时 `fadvise` 或调 limit |
| OOM kill | `dmesg` 的 `anon-rss`/`file-rss`/`pgtables`；cgroup `memory.events` 的 `oom_kill` | `anon-rss` 为主、无 swap | 真需求或泄漏；先降 heap/并发，再谈扩容 |
| swap 抖动、延迟尖刺 | `VmSwap`、`smaps` 的 `Swap`、`vmstat 1` 的 `si/so` | `si/so` 持续非零 | 工作集 > 内存；先减常驻，再调 `swappiness` |
| RSS 以 2 MB 阶梯跳 | `smaps_rollup` 的 `AnonHugePages`；`/proc/vmstat` 的 `thp_fault_alloc/thp_collapse_alloc` | `AnonHugePages` 跟着跳 | THP（实验 E）；`MADV_NOHUGEPAGE` 或调 `khugepaged` |
| `free` 显示没内存，但服务正常 | `/proc/meminfo` 的 `MemAvailable` vs `MemFree`；`buff/cache` | `MemAvailable` 充足、cache 大 | 页缓存，不是故障 |
| 重启后进程 RSS 降了，容器内存没降 | cgroup `memory.current` / `memory.stat` | `file` 仍高 | 缓存记在 cgroup，不在进程；等回收或 `fadvise` |

三条纪律：

1. **先定账本再取数**：host `free`、cgroup `memory.current`、进程 `VmRSS` 是三个量；报告里写清「哪个账、哪个字段、哪个时间窗」。
2. **看趋势不看单点**：泄漏的判据是「斜率 + 不回落」，不是「超过某个阈值」。
3. **改一个变量，留一组前后数字**：`malloc_trim`、`ARENA_MAX`、`MADV_NOHUGEPAGE`、`fadvise` 各改一次，分别记录 RSS / cgroup `anon` / cgroup `file`。

---

## 35.8 五问收口表

| 五问 | 本篇的答案 |
| --- | --- |
| **① 它是什么** | 一场「RSS 只涨不降」的现场：同一句话背后有泄漏、碎片、页缓存、巨页、共享映射、统计口径六种机制；本篇是一套把它们分开的流程 |
| **② 物理形态** | 内存不是一块，是 `(VMA, page, PTE)` 三元组：`smaps` 的 `Size` 是 VMA，`Rss` 是驻留页，`Pss` 是按 mapcount 均摊，`Private_*` 是「当前只被一个地址空间映射」；cgroup 再按 `anon/file/kernel` 收一次电荷 |
| **③ 走一遍** | 五步：`status`+`smaps_rollup` 定口径 → `maps`/`pmap` 定 VMA → `Anonymous`/cgroup `file` 分匿名与文件 → `malloc_info` 分活块与空闲块 → `perf`/`bpftrace`/`pprof` 钉调用点。每步都有「一票否决」信号，命中就停 |
| **④ 改一行** | 实验 A：`free` 一行，RSS 207 MB → 1.5 MB；实验 B：定长池，`RSS/有效载荷` 1.38 → 1.02；实验 C：`fadvise`，cgroup `file` 64 MiB → 0；实验 D：`ARENA_MAX=1` + trim 关，129 MB → 22 MB（trim 开则反向）；实验 E：`MADV_NOHUGEPAGE`，32 字节触碰的 RSS 13.8 MB → 1.5 MB |
| **⑤ 它证伪了谁** | ①「RSS 涨就是泄漏」；②「`free` 了就该还」；③「`valgrind` 没报就没问题」；④「`VmPeak` 大是问题」；外加⑤「RSS 高就会 OOM」 |

**本篇量到的主线**：主线③——每一页 RSS 都是一次缺页/映射/回收的「跨界单价」；主线①——「进程独占内存」只是页表里的一组约定。把这两条合起来，就是那句唯一真问题：**答案在「谁持有」，不在「谁分配」。**

---

## 35.9 一页速记

```
【一句话】
  内存涨了，先问「谁持有」，再问「谁分配」。
  持有者只有四种：进程页表 / page cache / 分配器 arena / cgroup 电荷。

【四个口径】
  VSS = Σ Size          地址空间预订，不是内存
  RSS = Σ Rss           驻留页，含共享页全额，不可跨进程相加
  PSS = Σ Rss/mapcount  可加总；Σ PSS = 物理页总量
  USS = Private_Clean + Private_Dirty   只属于本进程

【六个含义与第一判据】
  泄漏   anon 单调涨、uordblks 同向涨、空闲不回落
  碎片   uordblks 平、fordblks/system 大、free 后 RSS 不降
  缓存   Cached / cgroup file 涨、MemAvailable 不降、anon 平
  巨页   AnonHugePages > 0、RSS 以 2MB 跳
  共享   Pss < Rss、Shared_Clean > 0、RSS 之和 > 物理页
  口径   VmSize/RSS/PSS/current 混用；先统一账本

【五步】
  status+smaps_rollup → maps/pmap → anon vs file → malloc_info → 采样

【本机真实数字】
  A 泄漏：200 轮 1MB → RSS 1.4MB→207MB，+1028.6KB/轮
  A 修复：每轮 free → 1.5MB，cgroup peak 336KiB
  B 碎片：free 一半 RSS 38MB 不降；随机 1.38× vs 池 1.02×
  C 缓存：read 64MB → RSS 2MB、cgroup file 64MiB；fadvise → 0
  C mmap：RSS 65MB、anon 0；madvise → RSS 1MB、file 仍 64MiB
  D arena：16 heaps/1098MB VSS/129MB RSS → 1 heap/150MB/22MB（trim 关）
  E THP：碰 32B → RSS 13.8MB（THP）vs 1.5MB（NOHUGEPAGE）
```

---

## 35.10 自检 3 题

**Q1.** 一个服务 RSS 800 MB，`smaps_rollup` 显示 `Rss 800 MB, Pss 350 MB, Private_Dirty 150 MB, Private_Clean 50 MB, Shared_Clean 600 MB, Swap 0, AnonHugePages 0`。这是泄漏吗？下一步看什么？

<details><summary>答案</summary>

不是泄漏的典型形状。`Pss` 只有 `Rss` 的 44%，`Shared_Clean` 600 MB，说明 800 MB 里一大半是**被多个进程共享的干净文件页**（共享库、`mmap` 的只读文件、shmem）。真正独占的是 `Private_Dirty 150 + Private_Clean 50 = 200 MB`，这才是「可能泄漏」的上界。按 4 个进程共享那 600 MB 计算，`Pss = 150 + 50 + 600/4 = 350 MB`，与输出对得上。下一步：

1. `pmap -x` 或按 `Rss` 排序 `smaps`，找到 600 MB 所在的文件 VMA（大概率是 `.so`、字典文件或 mmap 的数据文件）；
2. 数一下映射同一文件的进程数：4 个进程各报 600 MB 的 `Shared_Clean`，物理上只有一份；
3. 看 cgroup `memory.stat` 的 `file`；若 `file` 占大头且 `anon` 只有 200 MB，就确认是页缓存/共享映射；
4. 真要看「有没有泄漏」，盯 `Private_Dirty` 与 `uordblks` 的时间斜率，而不是 `Rss`。

补充：600 MB 若来自同一个 `.so`，这是正常的；若来自业务 `mmap` 的数据文件且持续变大，要查「谁在写这个文件」。
</details>

**Q2.** 一个 8 线程服务，每线程 `malloc(4 MB)` 后立即 `free`，循环压测。`VmRSS` 稳在 32 MB，`VmSize` 512 MB，`malloc_info` 显示 8 个 `<heap>`、每个 `system current` 约 4 MB。请解释这三个数，并给出两种改法及各自的风险。

<details><summary>答案</summary>

- **512 MB `VmSize`**：8 个 arena × 64 MB 的地址空间预留（2 核时 `arena_max = 8 × 2 = 16`，8 线程各拿到一个 arena）。这是 VSS，不是物理页。
- **32 MB RSS**：每个 arena 的堆高水位约 4 MB，8 个合计 32 MB；`free` 把页还给了 arena，但堆没有整体收缩（没有 `malloc_trim`，或空闲页不在堆顶）。
- **8 × 4 MB `system current`**：`malloc_info` 的 per-arena 证据；业务活对象接近 0，持有者是 arena 的空闲链表。

两种改法：

1. **`malloc_trim(0)` 周期性调用**：把每个 arena 顶部的连续空闲页还给内核。风险：`malloc_trim` 会遍历 arena、可能持有 arena 锁，高频调用会带来延迟毛刺；适合「低峰期/每 N 分钟」而不是每请求。
2. **`MALLOC_ARENA_MAX=2`（或 1）+ 定长对象池**：减少 arena 数量、让同尺寸请求复用空洞。风险：arena 少了锁竞争上升；`ARENA_MAX=1` 在 trim 开启时可能让稳态 RSS 反而变大（实验 D 的 4 MB → 28 MB），必须用前后数字验证。

如果 4 MB 的块是长生命周期的（如每线程一个缓存），先改「谁持有」——换成全局池或按需分配，比调 allocator 参数更根本。
</details>

**Q3.** 容器 `memory.max = 1 GiB`，`memory.current = 1 GiB`，但进程 `VmRSS` 只有 200 MB，也没有发生 OOM。剩下的 800 MB 在哪？接下来会发生什么？该看哪几个文件？

<details><summary>答案</summary>

剩下的大概率是 **cgroup 的 `file` 账**：page cache（读过的文件、镜像层）、`shmem` 和少量 `kernel/slab/pagetables`。`memory.current` 统计 cgroup 内所有进程的 anon + file + kernel，不是进程 RSS 之和；`VmRSS` 只算进程页表里的驻留页。

接下来：内核在 `memory.current` 接近 `memory.max` 时会先**回收**（clean file pages 直接丢，dirty 页回写），所以不一定 OOM；如果回收后 anon + 不可回收部分仍超限，才触发 cgroup OOM。`memory.events` 里的 `high`/`max` 计数会先涨，`oom`/`oom_kill` 最后才涨。

该看：

```bash
cat /sys/fs/cgroup/<cg>/memory.current
cat /sys/fs/cgroup/<cg>/memory.max
grep -E '^(anon|file|kernel|slab|pagetables|file_dirty|file_writeback|shmem) ' \
  /sys/fs/cgroup/<cg>/memory.stat
cat /sys/fs/cgroup/<cg>/memory.events
```

若 `file` 占 700 MB 且 `file_dirty` 很低，结论是「缓存可回收，不用扩 limit」；若 `anon` 占 700 MB，则要回到 Q2 的分配器账，或查真实泄漏。注意 `memory.current` 在进程退出后不会立刻下降——page cache 还记在 cgroup 上，这正是「重启后容器内存没降」的常见原因。
</details>

---

## 35.11 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 虚拟内存、页表与三类缺页（RSS 的物理底座） | `base/26` |
| `malloc`/`brk`/`mmap`/arena/OOM（本篇的机制篇） | `base/27` |
| 线程与 `clone` 的共享参数（多 arena 的由来） | `base/24` |
| 页缓存、回写与 `fsync` 三层谎（实验 C 的下一层） | `base/29` |
| 存储层次与延迟量级（为什么一次缺页比一次 cache 命中贵 100 倍） | `base/07` |
| 器件物理：DRAM 刷新与 SSD 写放大（内存/磁盘的物理约束） | `base/08` |
| Go 分配器：mcache/mcentral/mheap 与 arena 的对照 | [go/15](../go/15-allocator.md) |
| Go GC：`inuse_space` 与 `alloc_space` 的口径 | [go/28](../go/28-gc.md) |
| pprof / trace：第 5 步的采样工具 | [go/29](../go/29-pprof-trace.md) |
| 总纲与本篇 | [0-summary.md](0-summary.md) §0.6.5 |
