# 27. 内存分配与 mmap：`malloc` 究竟做了什么、RSS 为什么不降

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 操作系统：Linux 视角 ｜ **编号**：27 / 56
> **本篇定位**：Part III 的第七篇。26 讲的是「缺页发生时 CPU 与内核在干什么」，本篇讲**在那之前**：`malloc(100)` 到底有没有走到内核；如果没走，`free` 之后那块内存又去了哪、为什么 RSS 不跟着降。全篇只有一条推论链——`malloc 是用户态分配器在切自己的货架` → `货架按 arena / bin / tcache 分层，层越细越不共享` → `物理页只在首次写时到来，却只在「顶块够大」时才走得掉` → `于是 RSS 的形状是分配器的形状，不是活对象的形状`。28 讲 inode 与页缓存，29 讲落盘，30 讲共享内存，35 篇把本篇的账本搬到真实进程上复盘，37 篇用 mmap 做零拷贝——它们都默认你已经知道「`free` 不等于还」。
> **读法**：§27.2 的 chunk 图与 §27.3 的 bin 表是全篇的物理底座，请对照自己的机器跑一次 `malloc_usable_size(malloc(100))`；§27.6 的三本账与 §27.7 的归还条件是排障时最常用的两页；§27.8 的五组实验全部在本机跑出、总内存控制在 300 MB 内，建议至少复现 ①③⑤。赶时间只读 §27.2 的 syscall 账、§27.6 的三口径表、§27.9–27.11。
> **不在本篇**：页表四级结构、TLB、缺页中断的完整处理 → `base/26`；`fork` 的 COW 与地址空间复制 → `base/23`；线程与 `clone` 的共享参数 → `base/24`；inode、dentry 与页缓存回写 → `base/28`、`base/29`；共享内存与 futex → `base/30`；容器内存 limit 与 OOM 的治理面 → `base/34`、`base/35`；Go 运行时的分配器与 GC 实现 → [go/15](../go/15-allocator.md)、[go/28](../go/28-gc.md)。

---

## 27.1 引子：一次「RSS 只涨不降，重启就好」的排查

下面这条时间线是把三次同类事故压成一条（C 服务、容器化、无 swap），数字来自本篇 §27.4 与 §27.8 的复现，不是文学修辞：

| 时间 | 事件 | 观测 |
| --- | --- | --- |
| 10-08 | 元数据回填服务把线程池从 32 扩到 200，每个任务 `malloc` 几十个 4 KB 缓冲 | RSS 1.1 GB，进程稳定 |
| 10-11 | 值班发现 RSS 爬到 2.4 GB | 第一反应「内存泄漏」，加日志打印 `malloc/free` 计数 |
| 10-12 | 计数完全配平：`malloc` 次数 = `free` 次数 | 开始怀疑「库函数有 bug」 |
| 10-13 | 想用 `valgrind --leak-check=full`，镜像里没有，临时装又会把容器撑爆 | 改用 `-fsanitize=address` 压测 6 小时：**零泄漏** |
| 10-15 | 容器 limit 4 GiB，被 `OOMKilled` 两次；当次 RSS 3.6 GB | 但 `mallinfo2().uordblks` 只有 480 MB |
| 10-16 | 在线上进程里 `gdb -p` 调 `malloc_trim(0)`，RSS 3.6 GB → 1.3 GB，并稳定 10 分钟不反弹 | 「内存没丢，是没还」 |
| 10-17 | 定位：16 个 arena（2 核 × 8）、每线程 tcache 上限 7 块、`free` 的块因为邻块存活无法合并成顶块 | 线程池回调到 32，另加 60 s 一次的 `malloc_trim(0)` |

三个问题在事故里全出现了，而且被同一句话串起来：

```
「free 都调了，内存为什么还在？」        → 因为 free 的第一个出口是分配器，不是内核
「那分配器为什么不还？」                → 因为归还的最小单位是 top chunk，不是 4 KiB 页
「RSS 到底算谁的？」                    → 算「当时驻留的物理页」，不算「你还在用的对象」
```

**你以为**：`malloc(100)` 向操作系统要 100 字节，`free` 还回去 100 字节。
**其实**：`malloc(100)` 从一块已经属于本进程的 112 字节「货位」上切下来，通常**一次系统调用都不发**；`free` 只是把这块货位插回分配器自己的空闲链表，内核完全不知情。内核只知道一件事：这个进程碰过哪些物理页（RSS），以及它什么时候因为拿不到新页而被 OOM killer 选中。

本篇就按「一次 `malloc` 的完整路径 → 分配器的三层结构 → 多线程 arena → mmap 的真实语义 → 三本账 → 什么时候真的还」走一遍，最后给五组能自己复现的数字。

---

## 27.2 `malloc(100)` 的完整路径：从 tcache 到 top chunk，再到 `brk`

### 先看请求被改成了什么：112 字节的 chunk

glibc 的每个分配单元叫 **chunk**，用户拿到的指针不指向 chunk 开头，而指向「头部之后」：

```
低地址                                                              高地址
┌──────────────┬──────────────┬────────────────────────────────────┐
│ prev_size 8B │ size     8B  │ 用户数据（请求 100 B → 可用 104 B） │
│ （前一块空闲 │ （本块大小 + │                                    │
│   时才有效） │  3 个标志位）│  ▲ malloc 返回这里                  │
└──────────────┴──────────────┴────────────────────────────────────┘
                └──── 112 B 的 chunk ────┘
                            └─ 下一块的 prev_size 可以借给本块用 → 104 B
```

`request2size(100) = (100 + 8 + 15) & ~15 = 112`。8 是 size 字段，15 是对齐掩码（16 字节对齐）。实测：

| 请求 | chunk 大小 | `malloc_usable_size` | 浪费 | 说明 |
| --- | --- | --- | --- | --- |
| 16 B | 32 B | 24 B | 8 B | 最小 chunk 就是 32 B |
| **100 B** | **112 B** | **104 B** | **12 B** | 100 万个就多占 12 MB |
| 1000 B | 1008 B | 1000 B | 8 B | 对齐后刚好 |
| 100 KiB | 102416 B | 102408 B | 8 B | 大块反而几乎不浪费 |
| 132 KiB | 139264 B（mmap） | 139248 B | 4096 B | mmap 要按页取整 |

**第一笔账在这里就出现了**：`malloc(100)` 的 12% 是头部与对齐。100 万个 100 B 的对象，光头部就 12 MB。

### 完整路径：七道门，前六道都在用户态

```
malloc(100)
  │
  ├─① tcache（每线程 64 个 bin，每 bin 最多 7 块）── 命中：~10 ns，0 次 syscall
  ├─② fastbins（≤128 B 的 10 个 bin，单链表 LIFO）── 命中：~20 ns
  ├─③ smallbins（<512 B，62 个双向链表）────────── 命中：~50 ns
  ├─④ unsorted bin（1 个，刚 free 的块先落这里）── 命中：切分/归类
  ├─⑤ largebins（≥512 B，63 个按大小排序）─────── 命中：找最小够用的
  ├─⑥ top chunk（堆顶那块「剩下的」）──────────── 切一块：0 次 syscall
  └─⑦ sysmalloc：top 也不够 → brk（小于阈值）或 mmap（大于等于阈值）
```

前六道门全是**指针操作**，一次跨界都没有。这就是「`malloc(100)` 大概率没向内核要内存」的全部含义：只要堆顶还有 112 字节，内核根本不知道你分配过东西。（图中每个门的 ns 是量级结论：一次 tcache 命中就是两三次指针解引用，与一次 L3 命中同量级；具体数值随 CPU 与缓存状态变化。）

### 只有第 ⑦ 道门才花钱：`brk` 与 `M_MMAP_THRESHOLD`

主线程的堆是一条连续的 `brk` 区间，`brk` 指针往上推就是「向内核要更多地址空间」。glibc 不会每次 `malloc` 都推一点，而是一次推 `nb + M_TOP_PAD`（默认 128 KiB），再按页取整：

```
                  brk 起点                                     brk 当前值
进程堆： [已分配][空闲][已分配][ top chunk .................. ] │
                                                        └─ 切不出来时，brk 往上推 ~132 KiB
```

两个阈值决定第 ⑦ 道门走哪条路：

| 参数 | 默认值 | 作用 | 怎么改 |
| --- | --- | --- | --- |
| `M_MMAP_THRESHOLD` | 128 KiB | 请求 ≥ 它就单独 `mmap` 一段，而不是扩堆 | `mallopt` / `MALLOC_MMAP_THRESHOLD_` |
| `M_TRIM_THRESHOLD` | 128 KiB | 顶块大于它就尝试 `systrim` 还给内核 | `mallopt` / `MALLOC_TRIM_THRESHOLD_` |
| `M_TOP_PAD` | 128 KiB | 每次扩堆多要的余量 | `mallopt` / `MALLOC_TOP_PAD_` |
| `M_ARENA_MAX` | 8 × 核数 | arena 上限 | `mallopt` / `MALLOC_ARENA_MAX` |

`M_MMAP_THRESHOLD` **是动态的**：每当一个 mmap 出来的块被 `free`，glibc 就把阈值上调到「刚释放那块的大小」，并把 trim 阈值设成它的 2 倍（上限 32 MiB）。效果是「大块用一次就不再 mmap，改从堆里切」——这正是下面这组实测：

```c
void *a = malloc(1 << 20); memset(a, 1, 1 << 20);   /* 首次：走 mmap */
free(a);                                             /* 阈值被抬到 1 MiB */
void *b = malloc(256 << 10); memset(b, 1, 256 << 10);/* 256 KiB 现在从堆里切 */
```

```
1 MiB (首次)                     chunk usable=1052656  hblks= 1  hblkhd=1052672
free 1MiB 后 malloc(256 KiB)     chunk usable= 262152  hblks= 0  hblkhd=0
```

第二行 `hblks=0` 就是「没 mmap」。如果没有这个动态上调，256 KiB 每次都 `mmap` + `munmap`，两次系统调用加两次页表改动，代价远高于从堆里切。**分配器在用「记住上次的块有多大」换掉未来的 syscall。**

### 账：10 万个 100 B 的 `malloc` 触发几次 syscall

本机镜像没有 `strace`，用 30 行 `ptrace` 计数（等价于 `strace -c -e trace=brk,mmap,munmap`：在系统调用入口停点读 `orig_rax`，用 `rax == -ENOSYS` 判断入口）：

```text
$ python3 syscount.py ./e1s          # e1s: 循环 10 万次 malloc(100) + 写 100 B
after 1e5 malloc(100)   VmRSS= 13120 kB | heap= 10.7 MB
total=142 | brk=86  mmap=8  munmap=1  mprotect=3      ← 8 个 mmap 来自启动时的动态链接
```

```text
$ python3 syscount.py ./e1           # 同一程序，100 万次
after 1e6 malloc(100)   VmRSS=118580 kB | heap= 106.9 MB
total=881 | brk=831 mmap=8  munmap=1  mprotect=3
```

把 831 次 `brk` 摊到 100 万次 `malloc` 上：

| 量 | 值 | 推导 |
| --- | --- | --- |
| 每个 chunk | 112 B | `request2size(100)` |
| 100 万个 chunk | 106.9 MiB | 112 × 10⁶ ÷ 2²⁰ |
| `brk` 次数 | 831 | 实测 |
| 每次 `brk` 扩多少 | ≈ 128.8 KiB | 106.9 MiB ÷ 831，等于 `M_TOP_PAD` |
| 平均每次 `malloc` 的 syscall | **0.00083 次** | 831 ÷ 10⁶ |
| 平均每次 `malloc` 摊到的 syscall 成本 | **≈ 1 ns 量级** | 一次 `brk` 按 1.5 μs 算，摊到 1203 次分配 |
| 100 万个 chunk 的**缺页次数** | **≈ 27,300 次** | 106.9 MiB ÷ 4 KiB |
| 首触缺页成本（本机量级 5 μs/次） | **≈ 137 ms** | 27,300 × 5 μs，摊到每次 `malloc` ≈ 137 ns |

**结论有两层，第二层才是重点**：`malloc` 的系统调用成本可以忽略（1 ns/次），但**首次触碰物理页的成本是它的 100 倍**（137 ns/次，一次性）。所谓「分配内存」的真实价格是**缺页 + 清零 4 KiB**，不是 `malloc` 那几行指针操作。这也是为什么「先 `malloc` 再 `memset`」和「`calloc` 后立即用」在时间上看起来差不多——都要付缺页。

顺带解释一个容易踩的边界：`malloc(128 KiB)` 在这种进程里**不一定** mmap。`malloc` 先看 top chunk，切得出来就不进 `sysmalloc`。实测这台 glibc 2.36、这个启动状态下，131073 B 仍从堆里切，135168 B 才真的 `mmap`（`hblks` 从 0 变 1）——差的几 KB 就是这个进程启动时留下的 top chunk 余量。**阈值是「要不要问内核」的判据，不是「一定会问」的承诺。**

---

## 27.3 分配器的三层结构：bin、tcache 与「free 不合并」

### 五个 bin + 一个 cache

glibc 把空闲 chunk 按大小分桶，桶的作用是「下次同样大小的请求能 O(1) 捡回来」：

| 结构 | 覆盖范围 | 每 bin 块数上限 | 释放时合并？ | 查找 | 为什么存在 |
| --- | --- | --- | --- | --- | --- |
| **tcache** | ≤ 1032 B，64 个 bin | 7（`TCACHE_FILL_COUNT`） | **不合并** | O(1) LIFO | 2.26 起，把「同线程重复分配」变成两次指针操作 |
| **fastbins** | chunk ≤ 128 B，10 个 bin | 不限 | **不合并** | O(1) LIFO | 小对象释放极快，合并留到 `malloc_consolidate` |
| **smallbins** | < 512 B，62 个 bin | 不限 | 合并 | O(1) 精确匹配 | 小块也要能复用，且必须能拼大块 |
| **largebins** | ≥ 512 B，63 个 bin | 不限 | 合并 | O(log n) 找最小够用 | 大块不能浪费 |
| **unsorted bin** | 刚 free 的块先落这 | 1 | 合并 | 扫描时归类 | 延迟决策：不知道你要多大，就先不归类 |

`tcache` 与 `fastbins` 的共同点是 **释放时不与相邻空闲块合并**。这不是偷懒，是为了快：合并要检查前后两块、改三条链表、可能触发 `systrim`，而「先攒着，等真正需要大块时再统一整理」在绝大多数负载下更划算。

**代价是碎片**。而且碎片的形状很具体：

```
时间 1： [A 112B][B 112B][C 112B][D 112B] ............ top
时间 2： free(B)、free(D) —— 但 A、C 还活着
         [A 在用][B 空闲][C 在用][D 空闲] ........... top
时间 3： 要一个 200 B 的块 —— B、D 都只有 112 B，且不邻接
         → 只能从 top 往上切，堆继续长
```

**关键一句：归还给内核的最小单位是 top chunk，不是 4 KiB 页。** 只要 top 下面还压着一个活对象，它下面的所有空闲页都回不去。这就是「碎片」在 RSS 上的准确含义——不是「内存丢了」，是「空闲的地方不在归还路径上」。

### 实测：交替分配释放不涨，交错存活才涨

```c
/* 模式 A：100 万次 malloc(100) 后立刻 free */
for (int i = 0; i < 1000000; i++) { char *p = malloc(100); p[0]=i; p[99]=i>>8; free(p); }
```

```text
1e6 次 malloc(100)+free 之后  VmRSS=    1 MB | arena= 0.1 MB  uordblks=0.0 MB  fordblks=0.1 MB
```

**100 万次分配释放，RSS 只有 1 MB。** 因为 `free` 把块插回 tcache（7 块满了再进 fastbin），下一次 `malloc` 直接从链表的同一个块上切回来——地址空间一页都没多。**「交替分配释放」是内存最友好的形态：它让分配器的空闲链表永远是热的。**

换一个模式：先让 40 万个块同时存活，再释放一半，然后申请更大的块：

```text
40 万个 100B 存活           VmRSS=  47 MB | arena= 42.8 MB  uordblks=42.7 MB  fordblks= 0.1 MB
释放其中一半（空闲 22 MB）  VmRSS=  47 MB | arena= 42.8 MB  uordblks=21.4 MB  fordblks=21.4 MB
再申请 20 万个 200B         VmRSS=  86 MB | arena= 82.5 MB  uordblks=61.0 MB  fordblks=21.5 MB
```

三行读法：

1. 释放一半后，`fordblks` 已经有 21.4 MB 空闲，但 **RSS 一页没降**——这些空闲块夹在活块之间，不在 top 上。
2. 再要 20 万个 200 B 时，112 B 的空洞装不下 208 B 的 chunk，只能继续扩堆：**arena 从 42.8 MB 涨到 82.5 MB，RSS 从 47 MB 涨到 86 MB**。
3. 最终活对象 61 MB，RSS 86 MB：**25 MB 是碎片**（29%）。如果是 200 个线程、常年 40% 空闲率的服务，这个比例会把 2 GB 的活对象撑成 3 GB 的 RSS。

`tcache` 自己也能压住内存，算一笔上限账：每线程 64 个 bin × 7 块，若都用 1 KiB 的块，就是 `64 × 7 × 1 KiB ≈ 448 KiB/线程`；200 个线程理论上限 **≈ 90 MB**，全部是「free 过但既不合并也不归还」的块。实际只会用到的 bin 少得多，但这解释了为什么「线程数一上去，RSS 就台阶式上涨」。

---

## 27.4 多线程 arena：每个线程一间仓库，仓库之间不借货

### arena 是什么

单线程时全进程只有一个 `main_arena`。第一个额外的线程第一次 `malloc` 时，glibc 会给它**分配一个独立的 arena**：

```
线程 1 ──► main_arena ──► 主堆（brk 区间）
线程 2 ──► arena 2 ──► 64 MiB 的 mmap 段 ──► 自己的 bins + tcache + 一把锁
线程 3 ──► arena 3 ──► 64 MiB 的 mmap 段
  ...
上限 = 8 × nproc（64 位；本机 2 核 → 16 个）
```

三个直接后果：

1. **arena 之间不共享空闲块。** 线程 A 释放的块只回到 A 的 arena；线程 B 要用内存，即使 A 的 arena 里有 100 MB 空闲，B 也只能扩自己的堆。这是「碎片跨线程不可见」的根。
2. **每个 arena 都按 64 MiB 的段预留地址空间。** 只要有一个块活着，整段就不能 `munmap`。2 核机器上 16 个 arena 就是 **1 GiB 的 VSS**，哪怕 RSS 只有几十 MB。实测（200 线程各做 64 次 1 KiB 分配）：

```text
≥32MB 的匿名保留段：15 个，合计 939 MB          ← 默认 16 个 arena
≥32MB 的匿名保留段：0 个，合计 0 MB             ← MALLOC_ARENA_MAX=1
```

3. **线程退出不等于 arena 归还。** 只有当一个 arena 里**一个块都不剩**时，glibc 才会在 `free` 路径上把它 `munmap`；只要 tcache 里还留着 7 块、或者还有一个长生命周期对象，这段就留着。

### 实测一：200 线程 × 100 × 4 KB（arena 路径）

```c
/* 每个线程 malloc 100 个 4 KiB，逐页写；200 线程同时持有；然后 90% free */
void *p[100];
for (int i = 0; i < 100; i++) { p[i] = malloc(4096); p[i][0] = id; }
pthread_barrier_wait(&bar);                       /* 峰值：200 × 400 KiB = 80 MiB */
for (int i = 0; i < 100; i++) if (i % 10) free(p[i]);   /* 只留 10% 长生命周期 */
```

```text
start                          VmSize=     2 MB  VmRSS=    1 MB  VmHWM=    1 MB
200 线程各持 100×4KB（峰值）   VmSize=  2565 MB  VmRSS=   81 MB  VmHWM=   81 MB
线程退出、90% 已 free          VmSize=   997 MB  VmRSS=   80 MB  VmHWM=   81 MB
malloc_trim(0) 之后            VmSize=   997 MB  VmRSS=   17 MB  VmHWM=   81 MB
仍存活的对象 = 2000 个 × 4KB = 7 MB
```

**活对象只剩 7 MB，RSS 还是 80 MB，`malloc_trim(0)` 之后也只降到 17 MB。** 这就是事故现场的形状：`uordblks` 小、`fordblks` 大、RSS 大。注意 `VmSize` 从 2565 MB 降到 997 MB（线程栈释放了），但仍有 **997 MB 的地址空间**——15 个死的 arena 还挂着 64 MiB 的段。

### 实测二：200 线程 × 1 MB（mmap 路径）——传说在这里不成立

同一条流水线，把每次分配换成 1 MiB：

```text
start                          VmRSS=    1 MB  VmHWM=    1 MB
200 线程各持 1MB               VmRSS=  203 MB  VmHWM=  203 MB
线程 free 并退出后             VmRSS=    1 MB
malloc_trim(0) 之后            VmRSS=    1 MB
```

**峰值 203 MB 是真的，`free` 之后回到 1 MB 也是真的。** 因为 1 MiB 高于 `M_MMAP_THRESHOLD`，每块都走 `mmap`；`free` 一个 mmap 块 = 立刻 `munmap`，物理页当场还给内核，跟 arena 一点关系都没有。

所以「线程池 200 线程 × 每个分配 1 MB 会把 RSS 撑爆」这句话**只对了一半**：如果这 1 MB 是**一次性大块**，它反而最安全（mmap + munmap）；真正撑爆 RSS 的是「每线程大量**阈值以下**的小块 + 交错存活」——也就是实测一。这个反直觉正是本篇最值得带走的一条：**决定 RSS 的不是分配总量，而是分配粒度与存活形状。**

### `MALLOC_ARENA_MAX` 的取舍

| 口径 | 默认（16 arena） | `MALLOC_ARENA_MAX=1` |
| --- | --- | --- |
| VmSize（200 线程峰值） | 2565 MB | 1681 MB |
| VmSize（线程退出后） | 997 MB | 112 MB |
| VmRSS（90% free 后） | 80 MB | 80 MB |
| `malloc_trim(0)` 后 | 17 MB | 17 MB |
| 分配锁 | 16 把，冲突少 | 1 把，200 线程抢 |

这张表要说的话：**arena 数量主要买的是 VSS 和锁竞争，不是 RSS。** 把 `MALLOC_ARENA_MAX` 调小能救「`ulimit -v` 被杀」「VSS 超 limit」，但对「碎片导致的高 RSS」帮助有限——那要靠在业务低峰调 `malloc_trim(0)`、控制对象生命周期、或者换分配器（jemalloc/tcmalloc 的 arena 与归还策略不同）。

---

## 27.5 mmap 的语义：地址空间先到，物理页后到

### 参数怎么组合，决定四件事

```c
void *mmap(void *addr, size_t len, int prot, int flags, int fd, off_t off);
```

| 维度 | 取值 | 含义 |
| --- | --- | --- |
| 后备 | `fd = -1` + `MAP_ANONYMOUS` | 匿名：没有文件，首次读全是 0（共享的零页） |
| 后备 | `fd >= 0` | 文件映射：页来自 page cache，写回由回写线程决定（`base/29`） |
| 共享 | `MAP_PRIVATE` | 写时复制：写自己不改变文件/别人 |
| 共享 | `MAP_SHARED` | 写就是改同一批物理页，别的进程立刻看见（`base/30`） |
| 权限 | `PROT_READ/WRITE/EXEC/NONE` | 只决定 PTE 的权限位，可 `mprotect` 改；`PROT_NONE` 常用来做 guard page |
| 时机 | `MAP_POPULATE` | 在 `mmap` 里就把页填好（批量缺页），而不是第一次访问时逐页缺 |

四种组合的语义差别，正好是「谁看见谁的写」这一张表：

| 组合 | 首次读 | 写之后 | 典型用途 |
| --- | --- | --- | --- |
| 匿名 + PRIVATE | 零页（COW） | 只改自己 | `malloc` 的大块、栈、堆 |
| 匿名 + SHARED | 零页 | 同族进程都看见 | `fork` 前的共享缓冲、大页池 |
| 文件 + PRIVATE | 文件的页 | 只改自己（COW，不回写） | 加载 ELF、只读数据 |
| 文件 + SHARED | 文件的页 | 写进 page cache，别人可见 | 共享内存、mmap 读写文件 |

### 关键事实：`mmap` 只改页表，不占 RSS

```text
start                              VmSize=    2468 kB  VmRSS=    1124 kB
mmap 20MB 返回后（未写）           VmSize=   22948 kB  VmRSS=    1360 kB
memset 20MB（每页写一个字节）      VmSize=   22948 kB  VmRSS=   21840 kB
madvise(MADV_DONTNEED) 后          VmSize=   22948 kB  VmRSS=    1360 kB
MADV_FREE 后（惰性回收）           VmSize=   22948 kB  VmRSS=   21840 kB
munmap 后                          VmSize=    2468 kB  VmRSS=    1360 kB
```

- `mmap` 返回时 `VmSize` **+20 MB**，`VmRSS` 只 +236 KB——多出来的是页表页、内核 VMA 结构与程序自己的缓冲，不是那 20 MB 数据。**地址空间是承诺，物理页是到货。**
- 逐页写之后 RSS 才 +20 MB。20 MB ÷ 4 KiB = **5120 次缺页**。
- `MADV_DONTNEED` 立刻把 PTE 拆掉、RSS 掉回去；匿名页的内容丢弃，下次读又是零（**不是 swap，是清零**）。
- `MADV_FREE` 只打「可回收」标记，RSS 在内存压力到来前**照旧算**。这是很多「为什么 RSS 不降」的第二层原因：内核还没缺内存，就不急着收。

缺页的价格（同一台机器，6 轮取最小值）：

```text
20MB 匿名映射，6 轮取最小值：
  普通 mmap 调用      = 0.01 ms 量级
  首次写（5120 次缺页）= 26.02 ms  → 每次缺页 5.08 μs
  二次写（纯 memset）  = 1.37 ms   → 带宽 15.3 GB/s
  MAP_POPULATE 的 mmap = 8.89 ms   → 把缺页搬进调用里，且批量处理更便宜
```

**首次写比二次写慢 19 倍**，这 19 倍就是「5120 次陷入内核 + 清零 4 KiB」的钱。`MAP_POPULATE` 把它压到 8.89 ms——因为内核一次能装好一批 PTE，不用一页一次 trap。这与 `base/26` §26.9 量到的 6.3 μs/页同量级（那台机器负载更重）。

### 文件映射的 RSS 是 page cache，不是匿名内存

```text
mmap 128MB 文件并逐页写     VmRSS: 132404 kB  RssAnon:    100 kB  RssFile: 132304 kB
MADV_DONTNEED 后（页还在 cache）  VmRSS:   1392 kB  RssAnon:    100 kB  RssFile:   1292 kB
```

进程 RSS 里 132 MB 全是 `RssFile`。**它不是泄漏，是可回收的页缓存**：内存紧张时内核先丢它，进程不会被 OOM。所以排障第一步永远是分清 `RssAnon` 与 `RssFile`（§27.6 会给完整字段表）。

### madvise 的四个常用指令

| 指令 | 对匿名页 | 对文件页 | 什么时候用 |
| --- | --- | --- | --- |
| `MADV_DONTNEED` | 立即拆 PTE，内容丢弃，再读为 0 | 拆 PTE，页留在 page cache | 释放大缓冲、`malloc_trim` 内部就用它 |
| `MADV_FREE` | 标记可回收，压力来了才收 | 同左 | 想省一次 TLB shootdown，接受 RSS 暂不降 |
| `MADV_WILLNEED` | 触发预读/预缺页 | 预读进 cache | 即将顺序扫描大文件 |
| `MADV_HUGEPAGE` | 尝试 2 MiB 大页（THP） | 不适用 | 大数组、TLB 压力大；代价是内部碎片与抖动 |

---

## 27.6 RSS / PSS / VSS 三本账：同一批物理页被数了几次

### 三个定义，一句话一个

| 口径 | 定义 | 在哪里看 | 回答什么问题 |
| --- | --- | --- | --- |
| **VSS**（`VmSize`） | 所有 VMA 的长度之和，含从未映射到物理页的部分 | `/proc/PID/status` 的 `VmSize` | 「这个进程占了多少**地址**」——ASLR、`ulimit -v`、arena 预留都算在这里 |
| **RSS**（`VmRSS`） | 页表里挂着、当前驻留的物理页数 × 4 KiB，**共享页在每个进程里都全额计一次** | `status` 的 `VmRSS`、`smaps_rollup` 的 `Rss` | 「现在这一刻，这个进程让多少物理页不能挪」——会被重复计数 |
| **PSS**（`Pss`） | 每个物理页按「映射它的进程数」均摊：`Σ (页大小 ÷ 共享它的映射数)` | `/proc/PID/smaps_rollup` 的 `Pss` | 「这批内存里，真正该算它多少」——**ΣPSS ≈ 物理内存实际占用** |

`smaps_rollup` 里还有五个必须能读的字段：

| 字段 | 含义 | 排障价值 |
| --- | --- | --- |
| `RssAnon` | 匿名页（堆、栈、`malloc`） | 这一项涨才是「真的自己占了」，OOM 主要看它 |
| `RssFile` | 文件页（page cache、共享库、mmap 文件） | 涨了先别慌，可回收；`echo 3 > drop_caches` 能验证 |
| `Shared_Clean` / `Shared_Dirty` | 被多个进程映射的干净/脏页 | 共享库、`fork` 后的 COW 页、`MAP_SHARED` |
| `Private_Clean` / `Private_Dirty` | 只有本进程映射的页 | `Private_Dirty` 是「真正的私有账单」 |
| `Anonymous` | 该 VMA 里匿名部分 | 与 `RssAnon` 对照看，能区分匿名与文件 |

### 实测：一个进程 + 共享库 + 共享内存

程序 A：先映射 8 MiB 私有匿名并写满，再映射 8 MiB 文件（`MAP_SHARED`）并写满，然后 `fork`，父子各打一次逐 VMA 的 `smaps`（只挑 8 MiB 级的段）。程序 B 只映射 8 MiB 文件，子进程逐页读完后打 `smaps_rollup`：

```text
== 父进程 fork 前 ==
7f...0000-7f...800000 rw-s  /tmp/m27-shared.bin
   Rss: 8192 kB   Pss: 8192 kB   Shared_Dirty: 0 kB   Private_Dirty: 8192 kB   Anonymous: 0 kB
7f...800000-7f...000000 rw-p
   Rss: 8192 kB   Pss: 8192 kB   Shared_Dirty: 0 kB   Private_Dirty: 8192 kB   Anonymous: 8192 kB

== 子进程 fork 后（未触碰） ==
7f...0000-7f...800000 rw-s  /tmp/m27-shared.bin
   Rss: 0 kB      Pss: 0 kB      Shared_Dirty: 0 kB   Private_Dirty: 0 kB
7f...800000-7f...000000 rw-p
   Rss: 8192 kB   Pss: 4096 kB   Shared_Dirty: 8192 kB   Private_Dirty: 0 kB   Anonymous: 8192 kB

== 程序 B：子进程逐页读 8 MiB 文件映射之后（smaps_rollup） ==
Rss: 8956 kB   Pss: 4190 kB   Shared_Dirty: 8260 kB
```

三行就是三本账的全部差别：

1. **父进程**：8 MiB 私有匿名，RSS 8192、PSS 8192，`Private_Dirty` 8192 —— 一个进程独占，两个口径相等。
2. **子进程 fork 后**：同一批匿名页被两个进程的页表指着，子进程 RSS 仍然 **8192**（它「看见」8 MiB），但 PSS 变成 **4096**（8 MiB ÷ 2）。**RSS 把共享页数了两遍，PSS 只算一半。**
3. **子进程读文件映射后**：RSS 8956 里有 8260 是 `Shared_Dirty`（与父进程共享的 page cache 页），PSS 只有 4190。**只看 RSS 会把「两个人合租的 8 MB」记成「每人 8 MB」。**

补上共享库这一档：同一个进程的 `smaps_rollup` 里 `Shared_Clean` 约 **1.1 MB**（libc 的只读段等），PSS 按「有多少进程映射了同一页」均摊——机器上跑着几十个进程，libc 的 1.1 MB 在 PSS 里每家只摊几 KB。

### 把三本账放到同一张表

| 对象 | VSS 贡献 | RSS 贡献 | PSS 贡献 | 说明 |
| --- | --- | --- | --- | --- |
| 私有匿名 8 MiB（单进程） | +8 MiB | 8192 kB | 8192 kB | `Private_Dirty` |
| 同一批页被 fork 后共享 | 0（已算过） | 子进程 8192 kB | 子进程 4096 kB | 共享页重复计 |
| `MAP_SHARED` 8 MiB 文件，两进程 | +8 MiB/进程 | 各 8192 kB | 各 ≈4090 kB | page cache |
| libc 只读段 ≈1.1 MiB | +1.1 MiB | ≈1.1 MiB | 每进程几 KB | `Shared_Clean` |
| 15 个空 arena | **+939 MiB** | 0 | 0 | 只有地址空间 |

**实用判据**：`Σ RSS > 物理内存` 是正常的（共享页重复计），`Σ PSS ≈ 物理内存` 才是对的。容器里做容量规划看 `RssAnon` 与 `memory.current`，不要看单个进程的 `VmRSS` 相加。

### cgroup 的账又是第三套

cgroup v2 的 `memory.current` 是「这个 cgroup 里所有进程的 anon + file + slab」，本机实测：

```text
$ cat /sys/fs/cgroup/user.slice/.../memory.current
1161084928                    # 1.08 GiB
$ cat .../memory.max
max                           # 未设上限
$ grep -E '^(anon|file|slab) ' .../memory.stat
anon 803344384                # 766 MiB
file 324632576                # 310 MiB
slab 18762480                 #  18 MiB
```

`memory.current = anon + file + slab + …`，**page cache 也算在 limit 里**。但回收顺序是「先丢干净文件页，再考虑匿名页」，所以「`memory.current` 顶到 limit」不等于「要 OOM」——只有 anon 部分顶到 limit 且回收不动，才会触发 OOM killer。

---

## 27.7 归还：`free` 的三个出口，与「什么时候真还」

### `free` 只保证回到分配器

```
free(p)
  ├─① p 是 mmap 出来的大块（size 的 IS_MMAPPED 位置位）→ munmap：物理页当场还
  ├─② 尺寸落在 tcache / fastbin → 插进链表就 return：不合并、不看阈值、不还
  └─③ 其余 → 与相邻空闲块合并
        ├─ 并到 top：合并后的块 ≥ 64 KiB 才继续
        └─ top ≥ M_TRIM_THRESHOLD → systrim → 真的还
```

**最容易被忽略的是 ② 那个 `return`。** `malloc(100)` 的 112 B 块属于 fastbin 范围，`free` 一万次也只是往 10 条链表里塞一万个节点——**一个页都不会还给内核，连合并代码都没走到**。所以「我明明 free 了 100 MB，RSS 一点没降」的第一层解释不是「阈值没到」，而是**这些块根本没进入会检查归还的那条路径**。

能让它们被「捞出来」的只有两种事件：一次 ≥ 64 KiB 且最终并入堆顶的 `free`（它触发 `malloc_consolidate()`，把 fastbin 全部合并进 top；只有 top 本身 ≥ `M_TRIM_THRESHOLD` 时才继续 `systrim`），或者显式 `malloc_trim(0)`。实测（先在堆底放一个 1 MiB 大块，再分配 20 万个 100 B）：

```c
mallopt(M_MMAP_THRESHOLD, 1 << 30);     /* 强制全部走堆，排除 mmap 干扰 */
char *big = malloc(1 << 20); memset(big, 1, 1 << 20);   /* 先分配大块（在堆底） */
void **p = malloc(N * sizeof *p);
for (i = 0; i < N; i++) { p[i] = malloc(100); memset(p[i], 1, 100); }  /* 20 万个 100 B */
```

```text
1MB + 20 万个 100B 存活      VmRSS=  24 MB  heap= 22.9 MB  free= 0.0 MB
全部小块已 free（大块仍活）  VmRSS=  24 MB  heap= 22.9 MB  free=21.4 MB   ← 21.4 MB 空闲，RSS 不降
最后 free(1MB 大块) 之后     VmRSS=   3 MB  heap=  1.7 MB  free= 1.7 MB   ← 一次就全还了
```

差别只在那一次 1 MiB 的 `free`：它 ≥ 64 KiB，触发 `malloc_consolidate` 把 20 万个 fastbin 节点并进 top，此时 top 有 22 MB ≫ `M_TRIM_THRESHOLD`，于是 `systrim` 把整个堆尾切掉。**归还的触发条件是「一次够大的 free」——不是「累计 free 了多少」。**

顺带记一个反例：把小块换成 8 KiB（不属于 fastbin）也一样不会自动降。实测 1 万个 8 KiB 全部 `free` 后 RSS 仍是 79 MB，`heap=78.5 MB`、`free=78.5 MB`；直到 `malloc_trim(0)` 才回到 1 MB。**只按「大小」猜归还是不可靠的，要按「有没有人触发整理」判断。**

### 显式归还：`malloc_trim(0)`

```text
after 1e6 malloc(100)   VmSize= 119576 kB  VmRSS= 118544 kB  heap= 106.9 MB  free_in_heap=  0.0 MB
after free all          VmSize= 119576 kB  VmRSS= 118544 kB  heap= 106.9 MB  free_in_heap=106.9 MB
malloc_trim(0) -> 1
after malloc_trim(0)    VmSize=  10156 kB  VmRSS=   9168 kB  heap=   0.0 MB  free_in_heap=  0.0 MB
```

`malloc_trim(0)` 让 glibc 遍历所有 arena，把能够归还的顶部空闲段 `madvise(MADV_DONTNEED)` 掉。RSS 从 118 MB 掉到 9 MB（剩下的 9 MB 里有程序自己的 800 万个指针 = 8 MB）。**这是线上最便宜的一次「内存急救」**：不需要重启，不需要改代码，`gdb -p` 或一个信号处理函数就能调。

有一个记账陷阱要记住：8 KiB 那组实验里，`malloc_trim(0)` 之后 RSS 从 79 MB 掉到 1 MB，但 `mallinfo2().arena` **仍然显示 78.4 MB**——trim 改了页表，没改分配器的账。**判断「内核有没有收回」看 RSS，判断「分配器还有多少可复用」看 `fordblks`，两者不能互相替代。**

调参的四个旋钮：

| 参数 | 调大 | 调小 | 适合 |
| --- | --- | --- | --- |
| `M_TRIM_THRESHOLD` | 少归还、少 `systrim`、更快 | 更积极归还、更多 syscall | 内存紧、对象生命周期长的服务调小 |
| `M_TOP_PAD` | 扩堆一次多要，减少 `brk` 次数 | 省地址空间 | 高频分配调大（默认 128 KiB 已够） |
| `M_MMAP_THRESHOLD` | 大块也走堆，减少 mmap/munmap 抖动 | 大块更快归还 | 大量 64–512 KiB 的短命缓冲调大 |
| `M_ARENA_MAX` | 锁竞争少、VSS 大 | VSS 小、锁竞争多 | 线程多但对象小 → 调小；CPU 密集 → 默认 |

### 为什么 Go / Java 的 RSS 也不降

这不是 C 的专利，是「托管运行时都自带分配器」的必然：

- **Go**：`mallocgc` 从 mheap 的 arena（64 MiB 级 span）取内存，`free` 只是把 span 还给 mcache/mcentral。运行时的 **scavenger** 才会用 `MADV_FREE`（Linux 默认）或 `MADV_DONTNEED` 把页还给内核，而它受「下次 GC 的目标堆大小」约束——**堆没缩，就不还**。所以 Go 服务 RSS 停在历史峰值是设计行为，不是泄漏；`GODEBUG=madvdontneed=1` 能强制更激进的归还（代价是更多缺页）。细节在 [go/15](../go/15-allocator.md) 与 [go/28](../go/28-gc.md)。
- **Java**：JVM 堆是启动时 `mmap` 的一大块私有匿名内存，`-Xms` 直接 commit；G1/ZGC 只在「收缩」时才把不用的 region `madvise` 掉。于是 RSS 的形状由**堆参数**决定，不由活对象决定。堆外（metaspace、direct buffer、JNI）走 glibc `malloc`，又是本篇这一套规则。

**共同结论**：RSS 是「分配器 + 运行时愿意保留多少」的函数，不是「活对象多少」的函数。判断泄漏要看 `uordblks`/GC 后的活对象，不要看 RSS 曲线。

---

## 27.8 实测：五组数字，全部在本机跑出来

环境：2 vCPU、1.9 GB 内存、无 swap、glibc 2.36、内核 6.x。所有实验单次峰值 ≤ 210 MB，串行执行。

### ① 100 万个 100 B 分配后的 RSS

```c
#define N 1000000
static void *p[N];                       /* 指针数组本身 8 MB，别忘了它 */
for (int i = 0; i < N; i++) {
    p[i] = malloc(100);
    memset(p[i], i & 0xff, 100);         /* 必须写：不写只是 VSS */
}
/* 打印 /proc/self/status 与 mallinfo2 */
```

```text
start                      VmSize=  10280 kB  VmRSS=   1188 kB | heap=  0.1 MB  free=  0.1 MB
after 1e6 malloc(100)      VmSize= 119576 kB  VmRSS= 118544 kB | heap=106.9 MB  free=  0.0 MB
malloc_usable_size(100) = 104 B
after free all             VmSize= 119576 kB  VmRSS= 118544 kB | heap=106.9 MB  free=106.9 MB
malloc_trim(0) -> 1
after malloc_trim(0)       VmSize=  10156 kB  VmRSS=   9168 kB | heap=  0.0 MB  free=  0.0 MB
```

读法：106.9 MB 的 chunk + 8 MB 指针数组 + 页表/glibc ≈ 118.5 MB。**`free` 全部之后 RSS 一字节不降，`malloc_trim` 一句话降 109 MB。**

### ② alloc/free 交替 100 万次，与「交错存活」的对比

```text
# 模式 A：每次 malloc 后立刻 free
1e6 次 malloc(100)+free 之后  VmRSS= 1 MB | arena=0.1 MB | uordblks=0.0 MB | fordblks=0.1 MB

# 模式 B：40 万个同时存活 → 释放一半 → 再申请 20 万个 200 B
40 万个 100B 存活           VmRSS= 47 MB | arena=42.8 MB | uordblks=42.7 MB | fordblks= 0.1 MB
释放其中一半（空闲 22 MB）  VmRSS= 47 MB | arena=42.8 MB | uordblks=21.4 MB | fordblks=21.4 MB
再申请 20 万个 200B         VmRSS= 86 MB | arena=82.5 MB | uordblks=61.0 MB | fordblks=21.5 MB
```

同样 100 万次量级的操作，A 只用 1 MB，B 用 86 MB。**差别不在次数，在「释放的块能不能被下一次分配用上」。**

### ③ 20 MB 的 mmap 前后 RSS：证明「映射 ≠ 占页」

```c
char *p = mmap(NULL, 20 << 20, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
stat("mmap 返回");          /* VmRSS 只涨 236 kB */
memset(p, 1, 20 << 20);     /* 5120 次缺页 */
stat("逐页写之后");          /* VmRSS +20 MB */
madvise(p, 20 << 20, MADV_DONTNEED);
stat("DONTNEED 之后");       /* 立刻掉回去 */
```

```text
start                       VmSize=  2468 kB  VmRSS=  1124 kB
mmap 20MB 返回后（未写）    VmSize= 22948 kB  VmRSS=  1360 kB
memset 20MB（每页写一个字节）VmSize= 22948 kB  VmRSS= 21840 kB
MADV_DONTNEED 后            VmSize= 22948 kB  VmRSS=  1360 kB
MADV_FREE 后（惰性回收）    VmSize= 22948 kB  VmRSS= 21840 kB
munmap 后                   VmSize=  2468 kB  VmRSS=  1360 kB
```

### ④ 线程分配后 `malloc_trim` 前后

```text
线程退出、90% 已 free    VmSize= 997 MB  VmRSS= 80 MB   ← 活对象只有 7 MB
malloc_trim(0) 之后      VmSize= 997 MB  VmRSS= 17 MB   ← 还了 63 MB，剩下的被活对象挡住
```

**trim 不是万能的**：每个 arena 里只要还有一个活对象压在空闲段上面，下面的页就回不去。17 − 7 = 10 MB 就是这部分「结构性碎片」。

### ⑤ `MALLOC_ARENA_MAX=1` 对比

```text
$ ./e6                          # 默认 16 arena
线程退出、90% 已 free    VmSize= 997 MB  VmRSS= 80 MB
malloc_trim(0) 之后      VmSize= 997 MB  VmRSS= 17 MB

$ MALLOC_ARENA_MAX=1 ./e6
线程退出、90% 已 free    VmSize= 112 MB  VmRSS= 80 MB
malloc_trim(0) 之后      VmSize= 112 MB  VmRSS= 17 MB
```

**同一个进程、同样的 RSS，VSS 差 885 MB。** 如果你的告警或 limit 是按 VSS/`ulimit -v` 设的，这一个环境变量就是生死线；如果按 RSS 设的，它救不了碎片。

---

## 27.9 三列账：承诺、代价、崩点

| 维度 | 承诺了什么 | 为此付了什么 | 从哪里开始失效 |
| --- | --- | --- | --- |
| 分配不跨界 | `malloc` 绝大多数时候只是切用户态链表，~10–50 ns | 进程要预先持有大块地址空间；首次触碰仍要缺页（~5 μs/页） | 多线程各自扩堆 → VSS 与 RSS 都按 arena 数量放大 |
| 小对象快 | tcache/fastbin O(1)，`free` 也是几次指针写 | 释放**不合并**；7 块/线程/bin 的缓存会压住内存 | 长期运行 + 交错存活 → 碎片；`free` 后 RSS 不降 |
| 大块直通内核 | ≥128 KiB 走 mmap，`free` 即 `munmap`，归还干净 | 每次 mmap/munmap 两次 syscall + 页表改动；TLB 抖动 | 高频分配 128 KiB–1 MiB 的短命缓冲 → syscall 成瓶颈（阈值动态上调就是为了救这个） |
| 自动归还 | 一次 ≥64 KiB 且并入堆顶的 `free`、或显式 `malloc_trim` 就能还 | 小对象的 free 直接进 tcache/fastbin 并 `return`，不合并也不检查归还 | 「free 了 100 MB、RSS 不动」；容器 limit 逼近时来不及 |
| 内存账可观测 | `/proc` 三本账 + `mallinfo2` 能分开「谁持有」 | RSS 会重复计共享页，PSS 要逐页算，cgroup 账又是第三套 | 用 `VmRSS` 做容量规划 → 虚高；用 `VmSize` 报警 → 被 arena 吓死 |
| 与运行时共存 | Go/Java 都能跑在 glibc 之上 | 运行时自己的保留策略叠加在 glibc 的保留策略之上 | RSS 停在历史峰值，被误判为泄漏 |

一句话收束：**分配器买到的是「用地址空间换系统调用次数」，付出的是「碎片 + 多 arena + 延迟归还」。** 事故里那三个症状（RSS 涨、`free` 不降、OOM）不是三个 bug，是这笔交易的三个出口。

---

## 27.10 踩坑清单：现象 → 根因 → 定位 → 修 → 防复发

| 现象 | 根因（指回本篇） | 定位命令 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| `free` 后 RSS 不降，重启就好 | 块回到 bin/tcache，没回到内核（§27.7 出口②③） | `mallinfo2`：`uordblks` 小、`fordblks` 大 | `malloc_trim(0)`；低峰定时调；调小 `M_TRIM_THRESHOLD` | 监控 `fordblks/arena`，超过 30% 告警 |
| 内存涨但 ASan 报无泄漏 | 碎片，不是泄漏（§27.3） | `pmap -x`/`smaps` 看 anon 段是否「大而散」 | 池化、改分配粒度、换 jemalloc | 压测时同时看 RSS 与 `uordblks`，两者背离就是碎片 |
| `valgrind` 装不上 / 太慢 | 环境限制 | 用 `-fsanitize=address`（含 LeakSanitizer） | 见下方 ASan 输出 | CI 里常驻 ASan 任务 |
| 容器 `memory.current` 到 limit 但进程 RSS 不大 | cgroup 账含 page cache 与 slab（§27.6 末） | `memory.stat` 看 `anon`/`file` | 区分「file 高」与「anon 高」；file 高不用救 | limit 按 anon 峰值 + 30% 设，不按 RSS 之和 |
| `mmap` 大文件后 RSS 暴涨 | `RssFile` 是 page cache（§27.5） | `status` 看 `RssAnon` vs `RssFile` | `madvise(DONTNEED)` / `posix_fadvise` | 大文件用 `read` + 复用缓冲，或映射后主动丢弃 |
| 线程数一加，RSS 台阶式上涨 | 新 arena 64 MiB 段 + 每线程 tcache（§27.4） | 数 `/proc/PID/maps` 里 ≥32 MB 的匿名段 | `MALLOC_ARENA_MAX=4~8`；线程池固定 | 线程池化，禁止「每请求一线程」 |
| `malloc_usable_size` 比请求大，越界写没崩 | 104 > 100，踩的是 padding 或邻块头（§27.2） | ASan 立刻报 `heap-buffer-overflow` | 永远按请求大小用，不按 usable | 禁用 `malloc_usable_size` 做业务逻辑 |
| RSS 看着 2 GB，PSS 只有 300 MB | 共享页被重复计（§27.6） | `smaps_rollup` 的 `Pss` | 容量按 ΣPSS / `RssAnon` 算 | 报表统一用 `RssAnon` |

### `mallinfo2` 怎么读

```c
struct mallinfo2 m = mallinfo2();
/* arena    : 从系统拿到的非 mmap 堆总字节（含空闲）
   ordblks  : 空闲 chunk 个数
   hblks    : 当前 mmap 区域个数
   hblkhd   : mmap 区域总字节
   uordblks : 已分配（在用）字节，含 chunk 头
   fordblks : 堆内空闲字节
   keepcost : 堆顶可被 malloc_trim 释放的部分            */
```

判据只有三条：`uordblks` 涨 → 真泄漏或缓存无上限；`fordblks` 涨而 `uordblks` 平 → 碎片/未归还；`hblkhd` 涨 → 大块在走 mmap（可能是阈值被动态抬高了）。注意它统计的是**分配器视角**，不含 mmap 文件映射、栈、代码段，所以 `uordblks` 永远小于 RSS。

### `valgrind` 不可用时

```text
$ gcc -g -fsanitize=address -o leak leak.c && ./leak
=================================================================
==3335083==ERROR: LeakSanitizer: detected memory leaks

Direct leak of 1000 byte(s) in 1 object(s) allocated from:
    #0 0x7fbea4eb89cf in __interceptor_malloc ../../../../src/libsanitizer/asan/asan_malloc_linux.cpp:69
    #1 0x55e872aae18a in main /tmp/opencode/m27/leak.c:4
    #2 0x7fbea4c45249  (/lib/x86_64-linux-gnu/libc.so.6+0x27249)

SUMMARY: AddressSanitizer: 1000 byte(s) leaked in 1 allocation(s).
```

三个替代品的分工：**ASan/LSan** 抓「指针丢了」的泄漏与越界（重编译，2–3 倍开销）；**`mtrace`** 用 `MALLOC_TRACE=./log` 记录每次 `malloc/free`，适合不能重编译的进程（开销大）；**`heaptrack`/`bpftrace`** 抓「谁在分配」的调用栈（需要额外工具）。而**碎片**这三样都抓不到——那要靠 `mallinfo2` 与 `smaps` 的账，这也是本篇把它们放在最前面的原因。

---

## 27.11 一页速记

```
【一句话】malloc 是用户态分配器在切自己的货架；free 是放回货架，不是还给房东。

【一次 malloc(100) 的成本】
  chunk = 112 B，usable = 104 B（12% 是头与对齐）
  路径：tcache → fastbin → smallbin → unsorted → largebin → top chunk → brk/mmap
  前六道门 0 次 syscall；100 万次分配只触发 ≈831 次 brk（每次 ≈128 KiB）
  真正的钱在缺页：100 万 × 112 B → 27,300 次缺页 → ≈137 ms

【五个 bin】
  tcache  ≤1032 B  7 块/bin/线程  不合并   ← free 后 RSS 不降的头号嫌疑
  fastbin ≤128 B   10 个          不合并
  small   <512 B   62 个          合并
  large   ≥512 B   63 个          合并
  unsorted          1 个          延迟归类

【三个阈值】
  M_MMAP_THRESHOLD 128 KiB（动态上调，上限 32 MiB）→ 大块走 mmap，free 即还
  M_TRIM_THRESHOLD 128 KiB → 顶块超过它才 systrim
  自动归还只在「一次 ≥64 KiB 且并进堆顶的 free」时发生；否则要 malloc_trim(0)

【三本账】
  VmSize  地址空间，含空 arena 的 64 MiB/段（实测 15 段 = 939 MB）
  VmRSS   驻留物理页，共享页重复计
  Pss     共享页按映射数均摊；ΣPSS ≈ 物理内存
  容器：memory.current = anon + file + slab，page cache 也算 limit

【四个能立刻用的动作】
  1. malloc_trim(0)         free 后不降时，先还一次试试
  2. MALLOC_ARENA_MAX=4~8   线程多、RSS 台阶涨、VSS 吓人时
  3. 看 RssAnon / RssFile   分清「真占用」与「可回收 page cache」
  4. 记录 fordblks/arena    碎片率 >30% 就该动对象生命周期了
```

---

## 27.12 自检 3 题

**Q1.** 一个循环做 10 万次 `malloc(100)` 并写入，中间不 `free`。用 `strace -c -e trace=brk,mmap` 看，`brk` 大概几次？为什么不是 10 万次？如果每次 `malloc` 之后立刻 `free`，RSS 大概是多少？

<details><summary>答案</summary>

`brk` 约 **86 次**（本机实测：10 万个 chunk 共 10.7 MB，每次 `brk` 扩约 128 KiB = `M_TOP_PAD`，10.7 MiB ÷ 128 KiB ≈ 86；另一次运行是 85 次）。不是 10 万次，因为 `malloc` 先从 top chunk 切；只有 top 不够才 `brk`，而一次 `brk` 会把余量一起要回来，够后面 ~1200 次 `malloc` 用。

分摊到每次 `malloc`：86 ÷ 100,000 ≈ **0.00086 次 syscall**，约 1 ns 量级——比起一次缺页（本机 ~5 μs）可以忽略。**真正的成本是 10.7 MiB ÷ 4 KiB ≈ 2,700 次缺页**（约 13 ms），分摊到每次约 130 ns。

如果每次 `malloc` 后立刻 `free`：RSS 约 **1 MB**（本机实测 `1e6 次 malloc(100)+free` 后就是 1 MB）。`free` 把块插回 tcache/fastbin，下一次 `malloc` 从同一个块上切回来，地址空间一页都不多。**「交替分配释放」是内存最友好的形态；杀手是「同时存活 + 交错释放」。**

</details>

**Q2.** 一个 200 线程的 C 服务，`RSS = 3.5 GB`，但 `mallinfo2().uordblks = 480 MB`、`fordblks = 1.9 GB`。`malloc_trim(0)` 之后 RSS 掉到 1.3 GB。请解释这三个数字，并说明为什么 trim 之后不是 480 MB。

<details><summary>答案</summary>

- `uordblks = 480 MB`：真正在用的 chunk（含头）。所以**不是泄漏**。
- `fordblks = 1.9 GB`：已经 `free`、但还挂在各 arena 的 bin/tcache 里的块。200 个线程 → 最多 16 个 arena，每个 arena 都有自己的空闲链表，彼此不能借；tcache 每线程 64 bin × 7 块又压住一部分。
- `RSS = 3.5 GB` 比 `uordblks + fordblks` 还大：因为 `mallinfo2` 不含 mmap 块（`hblkhd`）、线程栈、代码段和页表；另外碎片会让「空闲但不可复用」的页留在 RSS 里。

trim 之后是 1.3 GB 而不是 480 MB，因为 **归还的最小单位是 top chunk**：每个 arena 里只要有一个活对象压在空闲段上面，下面全部回不去。2000 个长生命周期对象散在 16 个 arena 里，就把 1.9 GB 中的 800 MB 挡在了「不可归还」的位置。剩下的 480 MB 活对象 + 栈/代码/页表 ≈ 1.3 GB。

修法按优先级：① 低峰定时 `malloc_trim(0)`（立刻见效）；② 把线程池降到 ≤ 8–16，减少 arena；③ 设置 `MALLOC_ARENA_MAX`；④ 长生命周期对象集中分配（池化），别散落在短命对象之间。

</details>

**Q3.** 某进程 `VmRSS = 2.0 GB`、`VmSize = 6.4 GB`、`Pss = 320 MB`，而容器 `memory.current = 2.1 GB`、limit 2.5 GB。三个数字分别说明什么？现在最该担心哪一个？

<details><summary>答案</summary>

- `VmSize = 6.4 GB`：地址空间账。大概率是 arena 预留（每个 64 MiB）、线程栈（每线程 8 MiB）、以及大块 mmap 的未触碰部分。**它不影响 OOM，只影响 `ulimit -v` 与「看起来吓人」。**
- `Rss = 2.0 GB` 而 `Pss = 320 MB`：这个进程的驻留页里绝大部分是**共享页**——共享库、`MAP_SHARED` 文件、或 `fork` 后与子进程共享的 COW 页。RSS 把共享页全额计入本进程，PSS 只算均摊后的份额。**所以「这个进程占 2 GB」是错的。**
- `memory.current = 2.1 GB` 接近 limit 2.5 GB：这才是真问题。它包含 anon + file + slab。要立刻看 `memory.stat`：如果 `file` 占大头，内核会先回收 page cache，不会 OOM；如果 `anon` 占大头，才是真的危险。

最该担心的是 **`memory.current` 的构成**，不是 `Rss` 或 `VmSize`。动作：`cat memory.stat` 分清 anon/file，再看 `memory.events` 里的 `high`/`max` 计数是否在涨。如果 anon 已接近 limit，先 `malloc_trim(0)`（如果是 glibc 碎片），再考虑降线程数或调 `MALLOC_ARENA_MAX`；如果是真活对象，只能扩容或优化数据结构。

</details>

---

## 27.13 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 缺页、页表、TLB 与 overcommit 的完整机制（本篇只讲「页什么时候到」） | → `base/26` |
| 一次系统调用的全过程：为什么跨界有固定单价 | → `base/21` |
| `fork` 的 COW：为什么父子进程的 PSS 会各减半 | → `base/23` |
| 线程与 `clone`：线程栈、TLS 与每线程 arena 的关系 | → `base/24` |
| 页缓存、回写与 `fsync`：文件映射的「另一半账」 | → `base/28`、`base/29` |
| 共享内存与 futex：`MAP_SHARED` 之后进程间怎么同步 | → `base/30` |
| 容器内存 limit 与 OOM 的治理面（`memory.current` 怎么变成 OOM） | → `base/34`、`base/35` |
| 用 `mmap` 做零拷贝：四种实现的 syscall 与拷贝次数对比 | → `base/37` |
| 排障手册：`pmap`/`smaps`/`perf`/`bpftrace` 各查哪一层 | → `base/54` |
| Go 的 mspan/mcache/mcentral 与逃逸分析 | [go/14](../go/14-memory-layout-escape.md)、[go/15](../go/15-allocator.md) |
| Go GC 的堆目标、scavenger 与 `MADV_FREE` | [go/28](../go/28-gc.md) |
| 用 pprof 看 heap profile 与 alloc_space/inuse_space 的区别 | [go/29](../go/29-pprof-trace.md) |
| 全书总纲：四条主线与 56 篇清单 | [0-summary](0-summary.md) |
