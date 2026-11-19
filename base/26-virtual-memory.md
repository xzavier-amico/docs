# 26. 虚拟内存与页表：缺页时 CPU 到底在干什么

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 操作系统：Linux 视角 ｜ **编号**：26 / 56
> **本篇定位**：Part III 的第六篇。21–25 讲的是「内核怎么被调用、进程怎么被造出来、谁先上 CPU」；本篇回答一个更底层的问题：**两个进程凭什么都以为自己独占一整块内存**。全篇只有一条推论链——`物理内存只有一份` → `于是地址必须被翻译` → `翻译表就是页表` → `页表可以指向「还没有的页」` → `于是缺页中断成了「按需分配」的唯一入口` → `三类缺页（minor / major / COW）就是这套幻觉的全部账单`。27 讲 `malloc` 怎么在这套幻觉上盖楼，◆35 讲内存账本怎么查，29 讲页缓存怎么落盘。
> **读法**：§26.3 的位切分与逐级查表、§26.5 的三类缺页是全篇支点，建议对着自己的机器把 §26.9 的四个实验跑一遍（十分钟，内存占用不超过 200 MiB）；§26.6 的流程图可以原样贴进排障笔记；赶时间只读 §26.5、§26.7、§26.10、§26.12 四张表。
> **不在本篇**：`malloc` 的 arena / brk / mmap 阈值与 RSS 治理 → `base/27`；`fork` 的进程语义与状态机（本篇只讲它引发的页表动作）→ `base/23`；页缓存回写、`fsync` 与崩溃一致性 → `base/29`；零拷贝四实现的 syscall 账 → `base/37`；cgroup/namespace 的手工搭建 → `base/34`；NUMA 的 first-touch 与迁移策略 → `base/07`、`base/25`；文件系统 inode 与 dentry → `base/28`。

---

## 26.1 引子：RSS 只有 210 MB 的进程，被 1.5 GiB 的 cgroup 杀了

先看一份值班记录里最常见的五个数字。容器 `memory.max = 1.5 GiB`，节点总内存 1.9 GiB，没有 swap：

| 记到哪本账上 | 大小 | 在被杀进程的 `VmRSS` 里吗 |
| --- | --- | --- |
| 该进程的匿名页（堆、栈、JIT 代码） | 210 MB | **是** |
| 另一个进程写进 `/dev/shm` 的 tmpfs 文件 | 800 MB | 否（若没映射进它） |
| 日志与模型文件留下的 page cache | 505 MB | 否（未被映射的 page cache 不算进任何进程的 RSS） |
| `memory.current`（cgroup v2 的合计） | 1.48 GiB | — |
| `memory.max` | 1.50 GiB | — |

一分钟后，进程 A 第一次写一块新 `mmap` 的 40 MB 缓冲区，cgroup 越过 `memory.max`；内核先回收，能回收的只有一部分脏页缓存，tmpfs 在没有 swap 时基本不可回收，于是 memcg OOM killer 在 cgroup 里挑了一个进程——就是 RSS 只有 210 MB 的 A。**它被杀，不是因为它占得多，而是因为「RSS」根本不是「它让机器少了多少内存」的计量单位。**

RSS 只回答一个问题：**此刻这个进程的页表里，挂着多少物理页**。它不回答：这些页是不是和别人共享的、page cache 算谁的、虚拟地址有没有真的花掉内存、被换出的页在哪。三个反例都能在一台机器上当场量出来：

- `mmap` 200 MiB 匿名内存，一页不碰：`VmSize` 涨到 207,268 kB，`VmRSS` 仍是 1,376 kB（§26.9 实验①）。**虚拟地址不是内存。**
- 两个进程 `MAP_SHARED` 同一个 64 MiB 文件：两边 `Rss` 各 65,536 kB，`Pss` 各 32,768 kB；把两个 `Rss` 相加得 128 MiB，物理页只有 64 MiB（§26.8）。**RSS 相加会重复计数。**
- 同一页文件数据：在 page cache 里被映射是 minor fault（本机 ~2.6 μs），不在 cache 里是 major fault（本机摊到 ~0.5 ms）。**同一行代码，差 100–1000 倍，只取决于页在不在。**

这三件事合起来就是本篇的唯一真问题：**每个进程的「独占内存」是页表 + 缺页中断维持的幻觉；三类缺页决定了你的程序是「慢」还是「炸」。** 下面先把幻觉拆开（26.2–26.4），再逐类算账（26.5–26.8），最后把账本合上（26.9–26.12）。

---

## 26.2 如果没有虚拟内存：三个具体问题，而不是三句口号

「虚拟内存让内存看起来更大」是一句正确的废话。真正逼出这套机制的是三件没有它就做不成的事。

**问题一：程序没法被装载到「一个它自己不知道的地址」。** 没有地址翻译，可执行文件里的每一条 `mov 0x404000, %eax` 都是物理地址。于是链接器必须为每个程序算好它将要住的物理位置；两个程序想用同一个地址就冲突；动态库想被多个进程共用，必须让所有进程接受「它固定住在某段物理内存」；ASLR 更不可能——随机化一个物理地址，等于让程序自己改自己。**页表把「程序看到的地址」和「数据实际在哪」解耦，重定位、ASLR、共享库才有地方长。**

**问题二：连续物理内存既不够，也碎得没法用。** 一个 `malloc(8 GiB)` 要求 8 GiB **连续**物理内存；机器跑上两天，物理页被分配释放切成碎片，最大的连续块可能只剩几百 MiB。没有页表，分配器只能做「连续块 compaction」——搬动正在被使用的页，而搬动期间所有指向它的指针都失效。有了页表，进程看到的连续 8 GiB 可以由 200 万个散落的 4 KiB 物理页拼出来，**「连续」变成页表里的一个约定，而不是物理事实**。

**问题三：隔离、共享、按需分配、文件映射是同一个开关的四个面。** 没有页表时：进程越界写会直接改到别人的数据（隔离失败）；`fork` 只能把父进程的每一页都复制一遍（共享失败）；`malloc(1 GiB)` 必须立刻拿到 1 GiB 物理页（按需失败）；把一个 1 GiB 文件读进内存必须先分配 1 GiB 再逐块 `read`（映射失败）。页表让这四件事都变成「改一个 PTE」：

| 需求 | 没有虚拟内存的做法 | 代价 | 有页表之后 |
| --- | --- | --- | --- |
| 进程隔离 | 靠程序员自觉 + 分段基址 | 越界即灾难 | 每个进程一套页表，别人的物理页不在你的映射里 |
| 共享 | 各自复制一份 | N 份内存、N 份 cache | 同一个物理页映射进多个页表，引用计数管理 |
| 按需分配 | 立刻分配并清零 | 启动慢、峰值内存 = 申请量 | PTE 先留空，第一次访问才缺页分配 |
| 文件映射 | `read` 到用户缓冲区 | 两次拷贝、一次大分配 | 把 page cache 的页直接映射进地址空间 |
| 换出 | 无法表达「这页在盘上」 | 内存只能增不能减 | PTE 存 swap entry，访问时缺页读回 |

代价也在同一张表里：**每次访存都要翻译**（26.3），**翻译结果要缓存**（26.4），**页不在时要进内核**（26.5–26.6），**承诺过多时要在写入现场还债**（26.7）。虚拟内存不是免费的「更多内存」，它是用「翻译 + 缺页」这两笔固定开销，买下隔离、共享与按需分配。至于 overcommit，它不是第五个需求，而是「按需分配」的记账方式：既然页可以晚点给，内核就必须回答「到底允许多晚、允许多少」——这个答案就是 26.7 的三档策略。

---

## 26.3 地址翻译：48 位虚拟地址怎么变成物理地址

### 位切分：9 + 9 + 9 + 9 + 12

本机 `grep 'address sizes' /proc/cpuinfo` 给出 `48 bits physical, 48 bits virtual`，页大小也直接可查：

```text
$ getconf PAGESIZE
4096
$ grep -m1 'address sizes' /proc/cpuinfo
address sizes	: 48 bits physical, 48 bits virtual
```

48 位虚拟地址被切成五段：低 12 位是页内偏移（一页 4096 字节），上面四段各 9 位，正好索引 512 个页表项：

```
 63        48 47        39 38        30 29        21 20        12 11          0
┌────────────┬────────────┬────────────┬────────────┬────────────┬─────────────┐
│ 符号扩展16 │ PGD/PML4 9 │ PUD/PDPT 9 │  PMD/PD  9 │  PTE/PT  9 │ 页内偏移 12 │
└────────────┴────────────┴────────────┴────────────┴────────────┴─────────────┘
   全 0 或全 1     512 项       512 项       512 项       512 项       4096 B
```

512 × 512 × 512 × 512 × 4096 B = 2⁴⁸ B = 256 TiB，其中用户态占低半区 128 TiB。**位数不是随便定的**：每个页表项 8 字节，一页正好放 512 项，所以每级索引 9 位——`8 B × 2⁹ = 4096 B`，一级页表恰好一页，不多不少。这就是「页大小 4 KiB」这个器件约束一路推出来的表结构。

### 逐级查表：四次内存读，每次都可能落空

CPU 的 `CR3` 寄存器存着当前进程 PGD 的**物理**地址（页表本身也必须是物理地址，否则翻译自己还要再翻译一次，递归没有底）。一次翻译是四次「读一个 8 字节的表项」：

```
CR3 ──► PGD[254] ──► PUD[190] ──► PMD[176] ──► PTE[271] ──► 物理页 0x317e1000
         │ 8 B/项       │ 8 B/项       │ 8 B/项       │ 8 B/项       + 偏移 0x000
         └ 512 项/页     └ 512 项/页    └ 512 项/页    └ 512 项/页
           4 KiB          4 KiB          4 KiB          4 KiB
```

把本机实测的几组地址按这个格式拆开（真实输出，程序见本节末）：

```text
mmap 页 A va=0x00007f2f9610f000
  PGD[47:39]=254  PUD[38:30]=190  PMD[29:21]=176  PTE[20:12]=271  off[11:0]=  0
  pagemap=0x81800000000317e1 present=1 pfn=0x317e1 -> phys=0x317e1000
mmap 页 B va=0x00007f2f96110000
  PGD[47:39]=254  PUD[38:30]=190  PMD[29:21]=176  PTE[20:12]=272  off[11:0]=  0
  pagemap=0x8180000000036a62 present=1 pfn=0x36a62 -> phys=0x36a62000
栈变量 va=0x00007ffd3276eb4c
  PGD[47:39]=255  PUD[38:30]=500  PMD[29:21]=403  PTE[20:12]=366  off[11:0]=2892
  pagemap=0x818000000001d98f present=1 pfn=0x1d98f -> phys=0x1d98fb4c
```

三组输出说明三件事：

1. 两个**虚拟地址相邻**的页（`...0f000` 与 `...10000`），PTE 索引只差 1（271 → 272），但物理页帧号是 `0x317e1` 与 `0x36a62`——**物理上毫不相邻**。「连续内存」是页表编出来的。
2. 栈地址的 PGD 索引是 255，mmap 区是 254：用户态地址的高段落在 PGD 的最后几项里，这是 48 位地址空间的形状，不是巧合。
3. `pagemap` 的 present 位为 1，PFN 能被读出来，说明这些页已经真实存在于物理内存。内核 4.0 之后，非特权进程读 `/proc/PID/pagemap` 时 PFN 一律填 0；本机能读到是因为在容器里仍有 `CAP_SYS_ADMIN`。**如果你的容器里 `pfn=0x0`，不是页不在，是权限被收走了**。

`pagemap` 的每一项 64 位：bit 63 = present，bit 62 = swapped，bit 61 = file/shared，bit 55 = soft-dirty，bit 0–54 = PFN（页帧号）。物理地址 = `PFN << 12 | offset`。这也是为什么「虚拟地址连续」不能推出「物理地址连续」，更不能推出「NUMA 节点相同」。

### 页表本身要占多少内存

| 映射规模 | PTE 个数 | PTE 页表内存 | 加上高层页表 | 占映射量 |
| --- | --- | --- | --- | --- |
| 1 GiB，4 KiB 页 | 262,144 | 2 MiB | 约 2 MiB + 12 KiB | **0.195%** |
| 1 GiB，2 MiB 页（THP） | 512 | 4 KiB | 约 8 KiB | 0.0008% |
| 1 GiB，1 GiB 页 | 0（PUD 直接指向） | 0 | 约 4 KiB | ≈0 |

「页表占内存 ~0.2%」这个常用数字就是这么来的：4 KiB 页、每页 8 字节的 PTE，比例恒为 `8 / 4096 = 0.195%`。它小，但不是零：一个 64 GiB 的 Redis 进程，光页表就是 128 MiB；`fork` 时这 128 MiB 要被复制一遍，这就是 26.5 里「fork 变慢」的第一笔账。换成 2 MiB 巨页，页表降到 256 KiB，但每次 COW 要复制 2 MiB——**同一笔账，只是换了个位置收。**

### 把地址拆开跑一遍

```c
#define _GNU_SOURCE
#include <stdio.h>
#include <stdint.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/mman.h>

static uint64_t entry(void *p) {
    int fd = open("/proc/self/pagemap", O_RDONLY);
    uint64_t e = 0; off_t off = ((uintptr_t)p >> 12) * 8;
    if (pread(fd, &e, 8, off) != 8) e = 0;
    close(fd); return e;
}
int main(void) {
    char *p = mmap(NULL, 8192, PROT_READ | PROT_WRITE,
                   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    madvise(p, 8192, MADV_NOHUGEPAGE);
    p[0] = 42; p[4096] = 43;
    for (int k = 0; k < 2; k++) {
        uintptr_t v = (uintptr_t)(p + k * 4096);
        uint64_t e = entry((void *)v), pfn = e & ((1UL << 55) - 1);
        printf("va=0x%016lx PGD=%lu PUD=%lu PMD=%lu PTE=%lu off=%lu "
               "present=%lu pfn=0x%lx phys=0x%lx\n",
               v, (v >> 39) & 511, (v >> 30) & 511, (v >> 21) & 511,
               (v >> 12) & 511, v & 4095, (e >> 63) & 1, pfn,
               (pfn << 12) | (v & 4095));
    }
    return 0;
}
```

编译：`gcc -O0 -o addr addr.c && ./addr`。输出就是上面那三行（地址每次运行会变，索引与 PFN 的关系不变）。

---

## 26.4 TLB：为什么 99% 的访存不用查页表

### 账：一次访存 100 ns，四次查表 400 ns

按 §26.3，一次没有缓存的翻译要读 4 个页表项，每一项都在内存里：

```
一次普通 load：     地址 → 数据            1 次内存访问   ≈ 100 ns
一次 TLB miss：     地址 → PGD → PUD → PMD → PTE → 数据   5 次内存访问 ≈ 500 ns
```

也就是说，如果每次访存都老实查四级页表，**程序的访存延迟会变成 5 倍**。CPU 不可能接受这个，于是在 MMU 里放了一个相联存储器（CAM）：TLB。它缓存「虚拟页号 → 物理页号 + 权限」的**最终结果**，命中时一次访存还是 1 次内存访问。

| 层级 | 典型容量（x86-64，本机同量级） | 4 KiB 页覆盖 | 2 MiB 页覆盖 |
| --- | --- | --- | --- |
| L1 dTLB | 64 项 | 256 KiB | 128 MiB |
| L2 TLB（统一） | 2,048 项 | **8 MiB** | **4 GiB** |
| 页表（内存） | 全部 | 整个地址空间 | 整个地址空间 |

这张表就是「工作集超过 8 MiB 就开始漏 TLB」的来源（`base/07` 给过同一个结论）。但**漏了不等于每次多 400 ns**：现代 CPU 还有 page-walk cache（PWC），缓存 PGD/PUD/PMD 这些中间项。真正每次都要重读的，通常只有最靠近叶子的那一级。所以 TLB miss 的代价要按「页表落在哪一层」算：

| 页表项在哪 | 一次 walk 的额外内存访问 | 额外延迟 |
| --- | --- | --- |
| PGD/PUD/PMD/PTE 全在 cache | 0（只走片上 walker） | 10–20 ns（十几个周期） |
| 只有 PMD 在 cache，PTE 在 DRAM | 1 次 | ~100 ns |
| 中间层也全落空（最坏） | 4 次 | ~400 ns |

这解释了一个常被忽略的现象：**在小工作集上测「TLB 开销」往往测不出来**。本机在 64 MiB（还在 L3 内）上做随机页访问，4 KiB 页 17.4 ns/次、2 MiB 页 14.4 ns/次，只差 3 ns——数据本身在 L3，缺的只是页表 walk 的几级 cache 命中。等数据真的落到 DRAM，差距才会变成几十到几百 ns。

### 巨页：把「一项覆盖一页」改成「一项覆盖 512 页」

x86-64 允许在 PMD 层直接放一个「大页」表项（PS 位），一个 TLB 项就覆盖 2 MiB；PUD 层还能放 1 GiB 页。覆盖面积按 `2 MiB / 4 KiB = 512` 放大：

```
4 KiB 页：  2048 项 × 4 KiB  =   8 MiB 覆盖
2 MiB 页：  2048 项 × 2 MiB  =   4 GiB 覆盖   ← 512×
1 GiB 页：  2048 项 × 1 GiB  =   2 TiB 覆盖   ← 512× 再 512×
```

Linux 的透明巨页（THP）在 `THP=always` 时对匿名页自动尝试 2 MiB；本机 `/sys/kernel/mm/transparent_hugepage/enabled` 就是 `[always] madvise never`。注意「尝试」：内存碎片或回收压力下会退回 4 KiB。§26.9 的实验里同一段代码跑三次，THP 一次给出 8 次缺页、两次给出 1,030 次——**巨页是概率事件，不是开关承诺**。

### 一个 1 GiB 数组随机访问的 TLB 账

设数组 1 GiB、随机访问、每次只读 1 字节，基线的 DRAM 延迟按 100 ns 算：

| 页大小 | 页数 | L2 TLB 命中率（随机） | 1,000,000 次访问的 miss 数 | 额外 walk（每次 1 级） | 额外总时间 |
| --- | --- | --- | --- | --- | --- |
| 4 KiB | 262,144 | 8 MiB / 1 GiB ≈ **0.78%** | ≈ 992,000 | ~100 ns | **≈ 99 ms** |
| 4 KiB（最坏，4 级全落空） | 262,144 | 0.78% | ≈ 992,000 | ~400 ns | ≈ 397 ms |
| 2 MiB | 512 | 512 项 < 2,048 → **100%** | ≈ 0 | 0 | ≈ 0 |
| 1 GiB | 1 | 100% | 0 | 0 | 0 |

再算页表内存：4 KiB 页要 2 MiB 的 PTE 页，2 MiB 页只要 8 KiB。**所以「1 GiB 数组随机访问慢」这件事，可能根本不是 cache 的问题，而是几乎每次访问都要走页表（命中率不到 1%）。** 反过来说，把大数组按 2 MiB 对齐、用 `madvise(MADV_HUGEPAGE)`，或者干脆顺序访问（硬件预取 + 页表项连续，walk 也被摊薄），都能把这一项消掉。

TLB 还有第二笔账：**失效（shootdown）**。一个进程 `munmap` 或 `mprotect` 一大段地址，内核要通知所有跑过这个 `mm` 的 CPU 把对应 TLB 项刷掉；多核上这是一次 IPI（核间中断）。所以「频繁 `madvise(DONTNEED)` / `mprotect` 大段内存」的多线程程序，慢的不只是缺页，还有核间同步。`fork` 之后子进程写 COW 页也要做 TLB 失效——这些都不出现在 `strace` 里，只出现在 `perf` 的 IPI 计数里。

---

## 26.5 三类缺页：minor、major、COW

「缺页」是一个统称。Linux 把它分成几条完全不同的路径，成本差三个数量级。判断你的程序是「慢」还是「炸」，第一步就是把 `minor` 和 `major` 分开看。

| 类型 | 触发条件 | 内核做什么 | 本机实测（详见 §26.9） | 典型量级 |
| --- | --- | --- | --- | --- |
| **minor（匿名首次触碰）** | PTE 为空，VMA 是匿名映射 | 分配一个清零页（或 2 MiB 巨页），装 PTE | 16 MiB：**4,102 次**，0 major，6.3–7.0 μs/次 | 0.5–2 μs |
| **minor（page cache 命中）** | PTE 为空，文件页已在 page cache | 装 PTE；`fault-around` 一次映射 16 页 | 16 MiB：**257 次**（64 KiB/次），0 major，~2.6 μs/次 | 0.5–2 μs |
| **COW（写时复制）** | PTE 存在但只读，访问是写 | 复制 4 KiB 到新页，改 PTE 为可写 | 32 MiB：**8,198 次**，0 major，12–14 μs/次 | 1–5 μs |
| **major（文件）** | PTE 为空，页不在 page cache | 发起块设备 I/O，进程睡眠等读完成 | 12,800 次随机访问：**2,045 次 major** | 100 μs–10 ms |
| **major（swap）** | PTE 是 swap entry | 从 swap 设备读回，改 PTE | 本机无 swap，跑不出来 | 100 μs–10 ms |

三点必须说清：

**① 「minor」不等于「不分配内存」。** Linux 的 `minflt` 是「不需要 I/O 的缺页」，匿名页第一次触碰要分配、清零一页，仍然记 minor。实测 16 MiB 匿名内存：`minor=4102, major=0`，RSS 从 1.4 MiB 涨到 66.9 MiB（§26.9 实验①）——**页是这时才有的，不是 `mmap` 时有的。**

**② 文件映射的 minor 是「批发」的。** 内核的 `fault_around_bytes` 默认 64 KiB：一次缺页不只装一个 PTE，而是把周围 16 个页一起映射进来。所以顺序扫 16 MiB 文件只产生 **257 次**缺页（`16 MiB / 64 KiB = 256`，加 1 次边界），而不是 4,096 次。这个数字在 `strace -c` 里看不到，只在 `/proc/PID/stat` 的 `minflt` 里出现。

**③ COW 是「已经映射了，但不能写」。** `fork` 时内核复制页表，把父子双方的**私有可写** PTE 都改成只读，物理页引用计数 +1。任何一方第一次写，触发**保护性缺页**（error code 的 P=1），内核复制一页、把写方的 PTE 改成可写。32 MiB 堆、`fork` 后子进程逐页写：**8,198 次** minor fault，每次 ~12–14 μs（本机）。注意它在 `/proc` 里记的是 minor，不是 major——**没有 I/O，但一点都不便宜**。

### 为什么 Redis `fork` 之后第一次写会抖

Redis 的 `BGSAVE` 用 `fork` 造一个子进程，子进程只读地遍历内存写 RDB，父进程继续服务。两笔账：

- **`fork` 本身**要复制页表。64 GiB 堆、4 KiB 页 → 约 1,680 万个 PTE × 8 B = **128 MiB 页表**要复制；即使不复制数据，这 128 MiB 的拷贝也要几十毫秒。堆越大，`fork` 越慢，而且慢在「与数据量无关的页表」上。
- **COW**：父进程每写一页，就要复制 4 KiB。若 `BGSAVE` 期间父进程写了 30% 的页，就要多占 30% 内存，并且每次写都多一次 ~1–5 μs 的缺页 + 一次 TLB 失效。**抖动不是磁盘造成的，是父进程自己在写。**

THP 同时改了两笔账的大小：页表变成 1/512，`fork` 快 512 倍；但 COW 的粒度可能从 4 KiB 变成 2 MiB——一次写触发复制 2 MiB，延迟从微秒级跳到毫秒级。本机一次 2 MiB 巨页缺页实测 **3.34 ms**（§26.9 的第三次运行），而 4 KiB 页是 6–7 μs。**这就是「THP 让 Redis 延迟尖刺」的完整机制**：不是 THP 慢，是它把 512 次小拷贝合并成了一次大拷贝。Redis 官方建议关 THP，关的是这个合并。

但要补一句本机的实测：②b 里 THP 映射的 32 MiB 在做 COW 时仍然产生了 **8,198 次 4 KiB 缺页**——要么 THP 这次没分配成功，要么内核在 COW 时把 PMD 拆成了 PTE；无论哪种，结果都是 4 KiB 一次。也就是说，THP 的收益（缺页次数、页表大小）不会自动延伸到 `fork` 之后的写；具体是复制 2 MiB 还是 4 KiB，取决于内核版本、PMD 状态与是否还有别的映射者。**「开了 THP」不等于「COW 一定按 2 MiB 算」，这一条必须自己量。**

---

## 26.6 缺页处理流程：从 #PF 到「重试同一条指令」

CPU 在翻译失败时不会「返回一个错误值」，而是抛一个异常，让内核来决定怎么办。x86-64 的缺页异常是向量 14（`#PF`），它做四件事：

1. 把出错虚拟地址写进 `CR2`；
2. 把错误码压栈（下面这张表）；
3. 压入被打断指令的 `RIP`、`CS`、`RFLAGS`、`RSP`、`SS`；
4. 跳到内核的缺页入口。

错误码的五个位就是「内核判断该走哪条路」的全部输入：

| 位 | 名字 | = 0 | = 1 |
| --- | --- | --- | --- |
| 0 | P | 页不存在 | 保护违规（页存在但权限不符） |
| 1 | W/R | 读 | 写 |
| 2 | U/S | 内核态访问 | 用户态访问 |
| 3 | RSVD | 保留位正常 | 页表项保留了不该保留的位 |
| 4 | I/D | 数据访问 | 取指访问（NX 违规会走这里） |

流程本身：

```
用户态指令访问 *p
   │  MMU 查 TLB：命中 → 直接访存，结束（99% 的情况）
   ▼ 未命中
四级页表 walk：PTE 为空，或权限不符
   │
   ▼  CPU 抛 #PF（向量 14），CR2 = 出错虚拟地址，压入 error_code
内核入口 exc_page_fault（arch/x86/mm/fault.c）
   ├─ 中断/原子上下文里出错？ → 不能睡眠，能修就修，不能修就 oops
   ├─ 内核态地址（≥ TASK_SIZE_MAX）→ do_kern_addr_fault（vmalloc、ioremap）
   └─ 用户态地址（含 copy_from_user 传进来的）→ do_user_addr_fault
        ├─ 查 VMA（6.1 起是 mm->mm_mt 这棵 maple tree）：没有 → SIGSEGV
        ├─ 权限不符（写只读 VMA、执行 NX 页）→ SIGSEGV
        └─ handle_mm_fault
             ├─ PTE 为空 + 匿名 VMA → do_anonymous_page（分配清零页 / 2 MiB 巨页）
             ├─ PTE 为空 + 文件 VMA → do_fault（page cache 有 = minor；没有 = major 读盘）
             ├─ PTE 只读 + 写     → do_wp_page（COW：复制一页，改可写）
             └─ PTE 是 swap entry → do_swap_page（major：从 swap 读回）
   │
   ▼  装 PTE（present / writable / dirty / NX / young），必要时刷 TLB
返回用户态 → CPU 重新执行刚才那条指令（RIP 没有前进）
```

最后一行是整条链的关键：**内核不「替」指令完成访存，它只把页准备好，然后让 CPU 重试。** 因此：

- 缺页可以发生在一条指令的中间（比如 `rep movsb` 搬到一半），内核必须保证重试时已经完成的部分不会被重复计算；
- 如果页永远准备不好（地址不属于任何 VMA、权限永远不符、OOM 杀进程），重试就变成信号：`SIGSEGV`（地址错误/权限）、`SIGBUS`（文件映射超出文件尾）、`SIGKILL`（OOM）；
- 内核态访问用户指针（`copy_from_user`）也会缺页。这时内核不能把「用户地址错误」变成内核 oops，而是走 `fixup_exception` 返回 `-EFAULT`——**用户态传坏指针不应该杀内核**，这条规则写在异常表里。

还有两类容易混淆的「让页消失」的入口：

| 操作 | 对 PTE | 对 page cache | 下次访问 |
| --- | --- | --- | --- |
| `madvise(MADV_DONTNEED)`（私有匿名） | 删除 PTE，释放物理页 | — | 重新分配清零页（minor） |
| `madvise(MADV_DONTNEED)`（文件） | 删除 PTE | **保留** | 重新装 PTE（minor，若页还在） |
| `posix_fadvise(DONTNEED)` | 不动 PTE | 丢弃干净页 | 重新读盘（major） |
| `madvise(MADV_FREE)` | 标记可回收，PTE 先留着 | — | 未回收则数据还在；回收后读到 0 |

**「丢映射」和「丢缓存」是两件事**：前者改页表，后者改 page cache。把 `madvise` 当 `fadvise` 用，就会得到「内存没降、磁盘又读了一遍」或者「缓存没了、页表还在」的怪现象（§26.11 有对应条目）。

---

## 26.7 overcommit 与 OOM：为什么「申请成功」却在写入时被杀

### 三档策略

Linux 对「虚拟内存承诺」有三种态度，开关是 `/proc/sys/vm/overcommit_memory`：

| 值 | 名字 | 行为 | 适合 |
| --- | --- | --- | --- |
| 0 | 启发式（本机默认） | 明显超过 `RAM + swap` 的请求拒绝，其余放行；**不精确记账** | 通用 |
| 1 | 永远允许 | 任何 `mmap`/`malloc` 都成功，直到物理页真的不够 | Redis 这类需要 `fork` 的服务 |
| 2 | 严格记账 | 每次承诺都从 `CommitLimit` 扣额度，超额直接 `ENOMEM` | 不能接受 OOM 的关键系统 |

`CommitLimit` 的公式只有一行：

```
CommitLimit = swap 总量 + RAM × vm.overcommit_ratio / 100
```

本机没有 swap、`overcommit_ratio = 50`、`MemTotal = 2,021,692 kB`，所以 `2,021,692 × 50% = 1,010,846 kB`，与实际读到的 `1,010,844 kB` 只差 2 kB（内核按页对齐取整）。但同一时刻 `Committed_AS = 1,654,140 kB`，**已经是 `CommitLimit` 的 1.63 倍**——因为模式 0 根本不执行这个上限。这就是「超卖」的字面含义。

实测（§26.9）：在 `MemAvailable` 只有 578,724 kB 的机器上，`malloc(1200 MiB)` 返回了非空指针，`VmSize` 立刻涨到 1,231,272 kB，而 `VmRSS` 仍是 1,396 kB；只写前 8 MiB 后，RSS 才涨到 10,376 kB。

```text
vm.overcommit_memory = 0
CommitLimit:     1010844 kB
Committed_AS:    1654140 kB
MemAvailable:     578724 kB
malloc(1200 MiB) -> 0x7efdf953a010
--- malloc 返回后（还没写） ---
VmSize:	 1231272 kB
VmRSS:	    1396 kB
VmSwap:	       0 kB
--- 只写了前 8 MiB 后 ---
VmSize:	 1231272 kB
VmRSS:	   10376 kB
VmSwap:	       0 kB
```

**`malloc`/`mmap` 买到的是「地址空间的承诺」，不是物理页。** 物理页在第一次写（或读一个未映射的文件页）时才由缺页路径分配。所以「申请成功」与「写入被 kill」之间隔着一整个 26.5–26.6 的流程。模式 1 把这段距离拉到最大：承诺永远成功，债全部堆到缺页现场。

### OOM 杀谁

当内核在缺页路径上分配不出页、回收也回收不动时，`out_of_memory()` 会按分数挑一个进程：

- `/proc/PID/oom_score`：0–1000，越高越容易被杀；内核按 `RSS + swap + 页表` 估算。
- `/proc/PID/oom_score_adj`：-1000–1000，用户可调；`-1000` 等于「别杀我」（仍可能因内核 panic 策略被杀）。
- cgroup v2：`memory.max` 是硬上限，`memory.current` 是当前计费；超限时 **memcg OOM killer 只在这个 cgroup 里挑**，通常就是触发缺页的那个任务。`memory.oom.group=1` 则整组一起杀。

这解释了两个常见困惑：

- **「节点还有 free，为什么 OOM？」** 因为限制不在节点，在 cgroup；`free` 看到的是节点，`memory.current` 才是容器。
- **「RSS 最大的进程没死，最小的死了？」** 因为 `oom_score` 看的是「计费 + 可调权重（`oom_score_adj`）」，不是单纯的 RSS；tmpfs/page cache 计在 cgroup 里却不在进程 RSS 里，真正触发缺页的那个进程反而分数最高。

`vm.overcommit_memory=2` 还有一条副作用值得记住：任何一次看起来「不花内存」的操作——`fork`、新建线程栈、映射文件——都要先过额度这一关。严格记账 + 无 swap 的机器上，大进程在接近 `CommitLimit` 时 `fork` 可能直接 `ENOMEM`。Redis 文档要求 `overcommit_memory=1`，防的就是 `BGSAVE` 的 `fork` 在额度边缘失败，而不是防「内存不够」。

---

## 26.8 内存映射的三种：匿名、文件、共享

### 三种映射的物理形态

「三种」是按**页从哪来**分的：匿名、文件、共享内存。文件映射再按 `MAP_PRIVATE` / `MAP_SHARED` 分成两种组合，所以表里是四行。

| 形态 | 怎么产生 | 页从哪来 | 写时发生什么 | 持久性 |
| --- | --- | --- | --- | --- |
| **匿名私有** | `brk`、栈、`mmap(MAP_ANONYMOUS\|MAP_PRIVATE)` | 缺页时分配清零页（或 THP） | 直接写自己的页 | 进程退出即消失；可被 swap |
| **文件私有** | `mmap(fd, MAP_PRIVATE)` | 读：page cache 的页；写：COW 出一份私有副本 | 触发 COW，**不写回文件** | 私有修改随进程消失 |
| **文件共享** | `mmap(fd, MAP_SHARED)` | page cache 的页，多个进程共用 | 直接改 page cache，脏页由回写线程落盘 | 随 `msync`/`fsync` 持久化 |
| **共享匿名** | `mmap(MAP_SHARED\|MAP_ANONYMOUS)`、`/dev/shm`、`memfd_create` | tmpfs 的页 | 直接改，所有映射者可见 | 随最后一个映射消失（具名 shm 随 unlink）；无 swap 时不可回收 |

`/dev/shm` 不是「内存文件系统」的新魔法，它就是 tmpfs：页存在 page cache（shmem）里，有 swap 时可以换出，没有 swap 时只能占着。**它计入 cgroup 的 `memory.current`，却不一定出现在任何进程的 RSS 里**——26.1 的事故就是这么来的。

### 读一个 1 GiB 文件：read 与 mmap 的拷贝账

| 动作 | `read` 到 1 MiB 缓冲区（1 GiB） | `mmap` 后逐页读（1 GiB） |
| --- | --- | --- |
| 系统调用次数 | 1,024 次 `read` | 1 次 `mmap` + 262,144 次缺页（fault-around 后约 16,384 次） |
| 磁盘 → page cache | DMA，1 次 | DMA，1 次 |
| page cache → 用户缓冲区 | **CPU 拷贝 1 GiB** | **0 次**（用户直接读 page cache） |
| 用户缓冲区 → 业务 | 在用户缓冲区上算 | 在映射上算 |
| 额外内存 | 1 MiB 缓冲区 | 2 MiB 页表（4 KiB 页） |
| 主要成本 | 每次 `read` 的 syscall + 拷贝 | 每次缺页 + TLB 压力 |

`read` 的两份拷贝是「设备 DMA 进 page cache」+「内核把 page cache 拷进你的缓冲区」；`mmap` 省掉第二份，但把成本换成了缺页。**这不是「mmap 一定快」**，本机实测（200 MiB 文件、每次先 `posix_fadvise(DONTNEED)` 把缓存赶掉）：

| 方式 | 总耗时 | 吞吐 | minor | major |
| --- | --- | --- | --- | --- |
| `read`，1 MiB 缓冲，顺序 | 0.49–0.55 s | 366–407 MiB/s | 261–262 | **0** |
| `mmap`，顺序扫 4 KiB | 0.53–0.57 s | 350–374 MiB/s | 3,206–3,207 | **1** |
| `pread`，随机 4 KiB × 12,800 | 1.09–1.62 s | 85–127 μs/次 | 8 | **0** |
| `mmap`，随机 4 KiB × 12,800 | 0.42–1.10 s | 33–86 μs/次 | 1,737–1,738 | **2,045** |

四个数字各自说明一件事：

- **`read` 的 `major` 是 0，不是因为没有读盘**：I/O 发生在 `read()` 系统调用里，不在缺页路径上。`majflt` 统计的是「缺页导致的 I/O」，不是「所有 I/O」。拿 `majflt` 判断「有没有读盘」会漏掉整个 `read` 路径。
- **`mmap` 顺序只剩 1 次 major**：第一次缺页触发内核预读（通常 128 KiB 窗口），之后扫描速度慢于预读，页到的时候已经被读进来了，剩下的 3,206 次都是「装 PTE」的 minor。**顺序 mmap 的 major 被预读摊薄了。**
- **`mmap` 随机有 2,045 次 major（16%）**：随机跳页让预读失效，每 6 次访问就有一次要等盘。每次 major 摊到约 0.5 ms（本机这块盘 + 同机负载），这就是 33–86 μs/次的来源。
- **随机场景 `mmap` 反而比 `pread` 快**：`pread` 每次都要一次 syscall + 一次同步读（4 KiB），`mmap` 的 fault-around 一次读 64 KiB，把 I/O 放大成批发。**顺序读用 `read` 通常更稳，随机读/写大文件用 `mmap` 更省拷贝**——但前提是你能接受缺页的尾延迟。

写路径的账更悬殊：`read` 一块再 `write` 回去是「盘 → page cache → 用户缓冲 → page cache → 盘」，CPU 要碰两遍数据、至少两次 syscall；`mmap(MAP_SHARED)` 直接改 page cache，CPU 拷贝为零，只剩缺页、脏页标记与回写（回写的债在 `base/29`，省 syscall 的极致是 `base/37` 的零拷贝）。代价是**写共享映射等于写文件**：没有 `msync`/`fsync` 就没有持久性承诺。

### 共享映射：RSS 相加为什么会翻倍

本机实测（§26.9）：父进程 `MAP_SHARED` 一个 64 MiB 文件并写满，`fork` 后子进程读一遍，子进程的 `smaps`：

```text
Rss:               65536 kB
Pss:               32768 kB
Shared_Clean:       8192 kB
Shared_Dirty:      57344 kB
Private_Dirty:         0 kB
```

- `Rss = 65,536 kB`：子进程页表里挂着 64 MiB 物理页；
- `Pss = 32,768 kB`：这 64 MiB 被父子两个进程映射，按 1/2 均摊；
- 父进程同时也有 `Rss = 65,536 kB`；两个 `Rss` 相加 = 128 MiB，**物理只有 64 MiB**。

`Pss` 就是为这件事发明的：**要算「这个进程实际让机器少了多少内存」，用 PSS，不要用 RSS 相加**。共享库、大模型文件、`/dev/shm` 里的缓冲，都会让 RSS 之和远大于物理占用。◆35 会把这条账展开成一套排查流程；`base/27` 讲 `malloc` 的 arena 为什么不还给 OS。

写共享映射还有一笔账：`MAP_SHARED` 的写直接改 page cache，脏页由内核回写线程在几秒到几十秒内落盘；要「现在就在盘上」必须 `msync(MS_SYNC)` 或对 fd `fsync`——这就是 29 篇「`write` 成功不等于落盘」的另一半。零拷贝的 `sendfile`/`splice` 则是把「page cache → socket」这段也省掉，账在 `base/37`。

---

## 26.9 实测：四个实验，全部在本机跑出来

**环境**：AMD EPYC 7K62（2 vCPU）、内核 `6.12.113+deb12-cloud-amd64`、`MemTotal 2,021,692 kB`、**无 swap**、`THP=[always]`、`vm.overcommit_memory=0`、`overcommit_ratio=50`、`PAGESIZE=4096`。这是一台共享机器（load average 6 左右），**绝对耗时被同机负载放大了 2–5 倍；缺页次数是精确的，时间只看量级与比例**。

### ① `VmPeak` / `VmRSS` / `VmSwap`：虚拟地址不是内存

```c
size_t len = 200UL << 20;
char *p = mmap(NULL, len, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS, -1, 0);
/* 打印 /proc/self/status；然后只碰前 64 MiB，再打印；最后 munmap */
```

```text
--- mmap 之后（一页都没碰） ---
VmPeak:	  207268 kB
VmSize:	  207268 kB
VmHWM:	    1376 kB
VmRSS:	    1376 kB
VmSwap:	       0 kB
--- 碰过前 64 MiB 之后 ---
VmPeak:	  207268 kB
VmSize:	  207268 kB
VmHWM:	   66912 kB
VmRSS:	   66912 kB
VmSwap:	       0 kB
--- munmap 之后 ---
VmPeak:	  207268 kB
VmSize:	    2468 kB
VmHWM:	   66788 kB
VmRSS:	    1376 kB
VmSwap:	       0 kB
```

三行结论：`VmSize` 是「地址空间承诺」（200 MiB 映射立刻到账）；`VmRSS` 是「页表里真的挂着的物理页」（碰多少涨多少）；`VmPeak` 是**历史最高水位，永不下降**——`munmap` 后 `VmSize` 回到 2,468 kB，`VmPeak` 仍是 207,268 kB。**拿 `VmPeak` 报警等于拿「这辈子最胖的时候」判断现在超重。**

### ② minor / major / COW：`/proc/self/stat` 的 `minflt` 与 `majflt`

```text
①a 16 MiB 匿名, MADV_NOHUGEPAGE: minor=4102 major=0, 每缺页 6.34 us, 每 4KiB 6.35 us, 615 MiB/s
①b 16 MiB 匿名, MADV_HUGEPAGE  : minor=1030 major=0, 每缺页 14.54 us, 每 4KiB 3.66 us, 1068 MiB/s
②  子进程 COW 写 32 MiB: minor=8198 major=0, 每缺页 14.30 us
   父进程这期间: minor=10 major=0
②b 子进程 COW(THP) 写 32 MiB: minor=8198 major=0, 每缺页 13.82 us
③  顺序扫 16 MiB 文件映射(热): minor=257 major=0, 每缺页 2.63 us, sum=8
```

同一程序第三次运行的 ①b 是 `minor=8, 每缺页 3341.68 us`——THP 这次真的给到了 2 MiB 页：8 次缺页覆盖 16 MiB，每次 3.34 ms。**同一个 `madvise(MADV_HUGEPAGE)`，一次是 8 次缺页，另外两次是 1,030 次（大部分退回 4 KiB）**，这就是「巨页是概率事件」的现场证据。①a 的 4,102 与 ② 的 8,198（±2，来自程序自身页）稳定复现：**4 KiB 页就是每页一次缺页，一个不多一个不少**。

### ③ 200 MiB 文件：`read` vs `mmap`

命令：`./rwmmap read` / `./rwmmap mmap` / `./rwmmap randread` / `./rwmmap mmaprand`，每次进入程序先 `posix_fadvise(fd, 0, 200MiB, POSIX_FADV_DONTNEED)` 把该文件的干净页从 page cache 赶掉。

```text
read(1 MiB): 200 MiB, 0.49 s, 406.6 MiB/s, minor=262 major=0
mmap 顺序扫 4 KiB/次: 200 MiB, 0.57 s, 349.9 MiB/s, minor=3207 major=1
pread 随机 4 KiB x 12800 次: 1.088 s, 85.0 us/次, minor=8 major=0
mmap 随机扫 4 KiB x 12800 次: 1.097 s, 85.7 us/次, minor=1738 major=2045
```

这是其中一次运行；多次重复的区间是 `read` 0.49–0.55 s、`mmap` 顺序 0.53–0.57 s、`pread` 随机 85–127 μs/次、`mmap` 随机 33–86 μs/次。读法见 §26.8：`read` 的 major 恒为 0（I/O 在 syscall 里）；`mmap` 顺序靠预读把 major 压到 1；随机 mmap 有 16% 的访问撞上 major，每次约 0.5 ms。

### ④ swap 与 overcommit：本机没有 swap

```text
$ swapon --show
（无输出）
$ free -m
               total        used        free      shared  buff/cache   available
Mem:            1974        1410         113           1         594         563
Swap:              0           0           0
$ grep -E '^(pswpin|pswpout)' /proc/vmstat
pswpin 0
pswpout 0
$ cat /proc/sys/vm/swappiness
10
$ grep -E '^(CommitLimit|Committed_AS)' /proc/meminfo
CommitLimit:     1010844 kB
Committed_AS:    1654140 kB
```

**没有 swap 意味着匿名页不可回收**：内存压力下内核只能丢 page cache、回收干净文件页，一旦这些也不够，就直接进 OOM。所以本机无法演示 `do_swap_page`，只能给出「major fault 从 swap 读回」这条路径的结构（26.5 表最后一行）与量级（100 μs–10 ms）。`swappiness=10` 调的是「回收时更愿意动匿名页还是文件页」；没有 swap 时它基本只会影响文件页回收的倾向，不会产生任何 swap I/O。

把四个实验的读数压成一句话：**`VmSize` 可以吓人，`VmRSS` 可以骗人（共享页重复计），`minflt` 精确到页，`majflt` 只统计缺页路径上的 I/O，而 OOM 看的是 cgroup 合计，不是任何一个 RSS。**

---

## 26.10 三列账：承诺、代价、崩点

按 §0.2 的四条主线，本篇量到的主要是**主线③**：虚拟内存的每一笔开销都能写成「跨界次数 × 单次成本」——一次缺页是一次用户/内核跨界，一次 TLB miss 是一次页表内存访问。表里每一行的「代价」都是这两个乘数之一。

| 维度 | 承诺了什么 | 为此付了什么 | 从哪里开始失效 |
| --- | --- | --- | --- |
| **隔离** | 每个进程一套页表，越界写碰不到别人的物理页 | 每次访存要翻译；进程切换要换 `CR3`（PCID 可省刷新） | 共享映射、`/proc/PID/mem`、侧信道（Meltdown 类）绕过的不是页表而是权限检查 |
| **按需分配** | `mmap` 只占地址空间，写到哪页才给哪页 | 每次首次触碰一次 minor fault（0.5–7 μs） | major fault 抖动；写入现场才 OOM（26.7） |
| **共享** | 一个物理页可映射进多个地址空间 | 引用计数、COW、PSS 记账；写共享页要同步 | RSS 相加翻倍；共享页写冲突；`fork` 后 COW 风暴 |
| **容量** | 不用的页可换出/丢弃，内存可小于地址空间 | 换回是 major fault（100 μs–10 ms） | 无 swap 时匿名页不可回收 → OOM |
| **TLB** | 99% 访存不用走页表 | 2,048 项 × 4 KiB = **8 MiB 覆盖**；失效要 IPI | 工作集 > 8 MiB 且分散 → 每次多 1–4 次内存读 |
| **页表内存** | 按页粒度映射，想用多少建多少 | 全量映射约 **0.2%**（8 B / 4 KiB） | 稀疏大地址空间页表膨胀；`fork` 复制页表变慢 |
| **巨页** | 2 MiB 页把 TLB 覆盖放大 **512×**，页表缩到 1/512 | 缺页/COW/压缩按 2 MiB 计，单次可到 ms | 延迟尖刺、碎片导致退回 4 KiB、`khugepaged` 抢 CPU |
| **NUMA** | 本地内存访问更快 | 远端延迟 1.4–2.2×、带宽减半 | first-touch 落在错误节点；迁移导致页表抖动 |

一句话收束：**虚拟内存买到的是「隔离 + 按需 + 共享」，付出的是「每次访存的翻译、每次首次触碰的缺页、每 GB 约 2 MiB 的页表，以及一套必须靠 OOM 兜底的承诺体系」。** 三类缺页就是这三笔代价的收费站：minor 收「分配」，COW 收「共享」，major 收「容量」。

---

## 26.11 踩坑清单：现象 → 根因 → 定位 → 修 → 防复发

| 现象 | 根因（指回本篇） | 定位命令 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 服务 `free` 后 RSS 不降，以为泄漏 | `free` 只还到 glibc arena，没 `munmap`；页表与 arena 都还在 | `pmap -x PID`、`/proc/PID/smaps` 看 `Heap`/`Anon`；`malloc_trim(0)` 试一次 | 大块用 `mmap` 阈值以下才走 arena；必要时 `malloc_trim` | 监控 `Pss` 与 `Rss` 双指标；区分「分配器缓存」与「真泄漏」（`base/27`、`base/35`） |
| `VmPeak` 几个 GB，报警了 | `VmPeak` 是历史最高水位，永不回落 | `grep VmPeak /proc/PID/status` 对照 `VmSize`/`VmRSS` | 用 `VmRSS`/`Pss` 做容量告警 | 仪表盘不采 `VmPeak`；只采 `VmRSS`、`Pss`、`memory.current` |
| 延迟周期性尖刺，CPU 不高 | THP 的 2 MiB 缺页/COW/`khugepaged` 压缩 | `smaps` 看 `AnonHugePages`；`/sys/kernel/mm/transparent_hugepage/enabled` | 对延迟敏感进程 `madvise(MADV_NOHUGEPAGE)`；或全局改 `madvise` | 压测时同时跑 THP on/off 两组；把 THP 状态写进基线 |
| `mlock` 之后系统更容易 OOM | 锁住的页不可回收、不可换出，OOM 时只能杀别人 | `ulimit -l`、`smaps` 的 `Locked`、`/proc/meminfo` 的 `Unevictable` | 只锁真正关键的几 MB；提高 `RLIMIT_MEMLOCK` 要谨慎 | 给 `mlock` 做容量预算；禁止「全量锁堆」 |
| 随机读大文件延迟抖 100 倍 | 页被 evict，随机访问每次 major fault；预读失效 | `/proc/PID/stat` 的 `majflt` 增量；`smaps` 的 `Rss` 与文件的 `Cached` | 预读/预热（`MAP_POPULATE`、`madvise(WILLNEED)`）；或改 `read` 大块 | 容量规划时把 page cache 算进工作集；监控 major fault/s |
| 容器 OOM，但进程 RSS 很小 | cgroup 计的是 `memory.current`（含 page cache、tmpfs），不是 RSS | `cat /sys/fs/cgroup/.../memory.current`、`memory.stat`、`memory.events` | 给 tmpfs 限额、控制 page cache、调 `memory.max` | 告警用 `memory.current / memory.max`；把 shmem 单列 |
| 共享内存「占了两倍」 | 两个进程的 RSS 都计入同一物理页 | `smaps` 的 `Rss` vs `Pss` | 用 `Pss` 求和；或用 `memory.stat` 的 `anon`/`file` | 容量评审统一用 PSS；共享库/模型文件单独标注 |
| `madvise(DONTNEED)` 后内存没降 | 只丢了 PTE，page cache 还在；或反之用了 `fadvise` | `smaps` 的 `Rss` + `/proc/meminfo` 的 `Cached` 对照 | 匿名内存用 `madvise`；文件缓存用 `posix_fadvise` | 代码评审把两个 API 的语义写进注释 |
| 大稀疏映射把页表撑爆 | 每 4 KiB 一个 PTE，全量映射约 0.2% | `/proc/meminfo` 的 `PageTables`、`smaps` 的 `KernelPageSize` | 用 THP、减少预留、按需映射 | 容量模型里加「页表 = 映射量 × 0.2%」这一项 |

### 怎么读 `/proc/PID/smaps`

`smaps` 是**按 VMA 分段**的账本，每一段先一行头（地址范围 + 权限 + 偏移 + 设备 + inode + 文件名），后面跟一串计数器。最该看的是这几项：

| 字段 | 含义 | 用途 |
| --- | --- | --- |
| `Rss` | 这段映射当前挂着的物理页 | 「进程占了多少」的粗账 |
| `Pss` | 共享页按映射数均摊后的份额 | 多进程/共享库场景的唯一正确口径 |
| `Shared_Clean` / `Shared_Dirty` | 与别人共享的干净/脏页 | 判断「是不是共享库/共享内存」 |
| `Private_Dirty` | 只有自己用的脏页（通常是堆） | 判断「真泄漏」还是「共享」 |
| `Swap` | 这段被换出的量 | 有 swap 时看抖动来源 |
| `AnonHugePages` / `THPeligible` | 巨页命中情况 | 判断 THP 是否真的生效 |

一个典型的误判：`Rss` 很大、`Pss` 只有一半，说明一半是共享页（共享库、模型文件、`/dev/shm`），**不是泄漏**；`Rss` 不大但 `Private_Dirty` 持续涨，才是堆在长。`smaps_rollup` 给出整个进程的汇总，采样开销比逐段读小得多。

---

## 26.12 一页速记

```text
【一句话】每个进程的「独占内存」= 页表（映射）+ 缺页（惰性）+ 回收（容量）
          维持的幻觉；三类缺页决定「慢」还是「炸」。

【地址】48 位 = 9(PGD) + 9(PUD) + 9(PMD) + 9(PTE) + 12(offset)
        每级 512 项 × 8 B = 4 KiB，正好一页
        相邻虚拟页 ↛ 相邻物理页（pagemap 可验）

【三类缺页】
  minor  页在内存/刚分配，只装 PTE      0.5–7 μs    （Linux 记 minflt）
  COW    页在但只读，复制一页再改可写    1–5 μs      （也是 minflt）
  major  页不在，要读盘/读 swap          100 μs–10 ms（记 majflt）

【三个数字】
  4 KiB 页：每 GiB 映射约 2 MiB 页表 = 0.195%
  2,048 项 TLB × 4 KiB = 8 MiB 覆盖；2 MiB 页 → 4 GiB（512×）
  1 GiB 数组随机访问：99% 的访问 miss L2 TLB，1M 次多 ~0.1–0.4 s

【overcommit】
  CommitLimit = swap + RAM × ratio%；mode 0 不执行它，mode 2 才执行
  malloc 成功 ≠ 有页；页在第一次写时缺页分配；分配不出 → OOM killer
  无 swap 时匿名页不可回收，只能丢 page cache，丢完就 OOM

【三种映射】
  匿名私有（堆/栈）｜文件私有（COW，不写回）｜文件共享（page cache，写回）
  RSS 相加会重复计共享页 → 用 PSS

【三个坑】
  VmPeak 是历史水位，不是当前占用
  THP 把 512 次小缺页换成 1 次 2 MiB 缺页：吞吐好，尾延迟差
  read 的 major 恒为 0（I/O 在 syscall 里），别拿 majflt 判断有没有读盘
```

---

## 26.13 自检 3 题

**Q1.** 一个进程 `VmSize = 20 GiB`、`VmRSS = 200 MiB`，机器 1.9 GiB、无 swap、`overcommit_memory=0`。它现在危险吗？什么情况下会 OOM？

<details><summary>答案</summary>

**现在不危险，写入时才危险。** `VmSize` 是地址空间的承诺：20 GiB 的 `mmap` 只说明内核给你记下了一个 VMA，页表项一个都还没建，更没有物理页。`VmRSS = 200 MiB` 才是当前真正占用的物理页。

危险发生在「第一次写那些还没碰过的页」时：每次写触发一次 minor fault，内核要分配清零页。无 swap 时匿名页不可回收，`MemAvailable` 会一路掉到 0，然后 OOM killer 按 `oom_score` 挑人。它不一定杀这个进程（如果 cgroup 里别的进程分更高），但触发缺页的那个通常最危险。

要区分三种「20 GiB」：① 真的 `mmap` 了 20 GiB 但没碰——只要 overcommit 放行就没事；② 碰了 20 GiB——物理页必须真有，1.9 GiB 机器上必然 OOM 或被杀；③ `VmSize` 大是因为大量文件映射——那些页在 page cache 里，可回收，压力下会先被丢掉。**判断依据永远是「已触碰的匿名页 + 不可回收的 shmem」而不是 `VmSize`。**

</details>

**Q2.** 一个 1 GiB 数组做随机访问。用 4 KiB 页和 2 MiB 页，TLB 账各是多少？为什么改成 2 MiB 页之后 minor fault 数也会下降？

<details><summary>答案</summary>

**TLB 账**：4 KiB 页需要 262,144 个页，L2 TLB 2,048 项只能覆盖 8 MiB，随机访问命中率约 0.78%；每次 miss 至少多一次页表内存读（PWC 只挡住上层），约 100 ns，最坏 4 级全落空约 400 ns。1,000,000 次访问 → 约 99 万次 miss → 额外 0.1–0.4 s。2 MiB 页只需要 512 个页，512 < 2,048，全部装得下，miss 率≈0，额外 walk≈0。

**minor fault**：缺页是「每页一次」的。4 KiB 页首次触碰要 262,144 次缺页；2 MiB 页只要 512 次，每次多清零 2 MiB。总清零字节一样，但**进内核的次数少 512 倍**——省的是每次缺页的固定开销（trap、查 VMA、装 PTE、可能的 TLB 失效），不是清零本身。本机 16 MiB 实测：4 KiB 页 4,102 次缺页，THP 成功时 8 次，相差 512 倍。

代价是尾延迟：一次 2 MiB 缺页要连续清零 2 MiB，本机实测 3.34 ms；4 KiB 页是 6–7 μs。所以「吞吐型」负载喜欢 THP，「延迟敏感型」负载经常关掉它。

</details>

**Q3.** Redis 64 GiB 堆，`BGSAVE` 时 `fork`。为什么 `fork` 会慢？为什么 `BGSAVE` 期间父进程写内存会抖？THP 分别把这两件事变好还是变坏？

<details><summary>答案</summary>

**`fork` 慢在页表，不在数据。** `fork` 不复制 64 GiB 数据，但要复制页表：4 KiB 页 → 约 1,680 万个 PTE × 8 B = 128 MiB；内核还要把父子双方的私有可写 PTE 都改成只读、增加页引用计数。128 MiB 的拷贝 + 约 1,680 万次 PTE 改写，就是几十毫秒级的 `fork` 延迟。堆越大，这段与业务数据量无关的固定成本越大。

**写内存抖在 COW。** `BGSAVE` 子进程只读，父进程继续处理写请求；父进程每写一页，触发一次保护性缺页：复制 4 KiB、改 PTE、刷 TLB。一次 1–5 μs，写 30% 的页就是约 500 万次缺页，而且内存会多占 19 GiB。抖动来自父进程自己的写，不是磁盘。

**THP 把两件事往相反方向推**：页表从 128 MiB 降到 256 KiB，`fork` 快约 512 倍（好）；但在支持 PMD 级 COW 的内核上，COW 粒度会从 4 KiB 变成 2 MiB，单次写要复制 2 MiB，延迟从微秒跳到毫秒（坏）。本机实测更微妙：THP 映射的 COW 仍被拆成 4 KiB（8,198 次缺页，§26.5），所以 THP 的收益未必延伸到写路径。Redis 官方建议关 THP，是宁愿 `fork` 慢一点，也不要写请求的 P99 出现毫秒级尖刺——同一笔账换个位置收，没有免费选项。

</details>

---

## 26.14 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 存储层次、cache line、TLB 覆盖与延迟坐标系（本篇的上一层） | → `base/07` |
| `fork` / COW 的进程语义、`task_struct`、僵尸与状态机 | → `base/23` |
| `malloc` 的 arena、brk/mmap 阈值、RSS 为什么不降 | → `base/27` |
| 页缓存回写、`write` 成功与 `fsync` 的三层谎 | → `base/29` |
| 内存账本现场：RSS/PSS/swap/THP 四本账怎么分 | → `base/35` |
| 零拷贝四实现：`read/write`、`mmap`、`sendfile`、`splice` 的拷贝与 syscall 账 | → `base/37` |
| Go 分配器：mcache/mcentral/mheap 怎么在 `mmap` 之上分层 | → [go/15](../go/15-allocator.md) |
| 全书总纲：四条主线与 56 篇清单 | [0-summary](0-summary.md) |
