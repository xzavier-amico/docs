# 23. 进程：`task_struct`、`fork` / COW 与状态机

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 操作系统：Linux 视角 ｜ **编号**：23 / 56
> **本篇定位**：Part III 的第三篇。21 讲一次系统调用怎么跨过特权级，22 讲一个 ELF 怎么被装进地址空间；本篇接着回答一个更朴素的问题——**内核到底记了多少账，才敢说自己「有一个进程」**。全篇只有一条推论链：`进程 = 一批资源的持有者（task_struct + mm + files + …）+ 一个可被调度的实体` → `所以创建进程 = 复制指针、给共享对象加引用计数、抄一份页表` → `所以 fork 便宜，便宜在它不复制物理页` → `但所有可写的页表项必须先改成只读，于是写的第一次要付 COW 的钱` → `退出只是把状态改成 ZOMBIE，收尸是父进程的义务，不是内核的义务`。24 讲 `clone` 的参数组合，25 讲调度，26 讲缺页，27 讲 malloc 与 OOM，30 讲 IPC，34 手搓容器：它们全部复用本篇这一条链。
> **读法**：§23.4 与 §23.5 是全篇支点（fork 的账记在页表项上，不在物理页上），请对着 §23.9 的四个实验重跑一遍（十分钟，峰值 200 MB）；§23.7 的状态图与 §23.8 的 `waitpid` 模板可以直接贴进值班手册；赶时间只读 §23.5、§23.7、§23.10、§23.12 四节。
> **不在本篇**：线程、`clone` 的共享参数组合、用户态线程与协程 → `base/24`；调度器、nice 与 cgroup 配额 → `base/25`；页表结构与缺页处理的完整机制 → `base/26`；`malloc`、RSS 与 OOM killer 的账本 → `base/27`；管道、共享内存、信号与 futex → `base/30`；namespace/cgroup 手工搭建 → `base/34`；每次系统调用的逐行实现 → `base/21`。

---

## 23.1 引子：三万个僵尸与一次 `EAGAIN`

**第一件事：机器没满，但 `fork` 已经失败。**

2025-11 某 CI 平台的 runner 集群做了一次「去 shell 化」改造：原来包一层的 bash supervisor，换成自研的 Go 小程序。新 supervisor 的逻辑很干净——拉起 worker、`waitpid(worker_pid)`、退出码原样上报。上线第六天，一台 runner 上所有新任务开始报：

```
fork: Resource temporarily unavailable
```

值班的人先看 CPU 和内存：CPU 12%，内存用了 38%，都不像瓶颈。再看进程数：

```
$ ps -eo stat= | grep -c '^Z'
31204
$ cat /sys/fs/cgroup/pids.max      # 该 runner 的 cgroup
32768
$ cat /sys/fs/cgroup/pids.current
32761
```

**三万一千个僵尸进程，把 cgroup 的 pid 配额吃光了。** 而它们每一个的 `RSS` 都是 0，不占 CPU、不占内存、`kill -9` 也没反应——所以监控上一切正常。事后复盘确认：worker 自己还会 fork 一批编译子进程，supervisor 只 `wait` 了主 pid，**其余子进程退出后无人收尸**，全部留在 `Z`。修复只有两处：把 `waitpid(main_pid, …)` 换成 `while (waitpid(-1, &st, WNOHANG) > 0) ;`，并把 `pids.max` 从 32k 提到 64k 只当缓冲。**僵尸不是「没死」，是「死了但没人来收尸」**——它已经把地址空间、打开的文件、几乎全部资源交还了，只留下一具 `task_struct`、一个 pid 号、一条 `/proc` 目录项，等父进程来签收。

**第二件事：一次差点批下去的扩容。**

同一周另一条线，一个 Python 服务要在请求里调外部命令。评审会上有人贴了 `ps`：

| PID | RSS | 命令 |
| --- | --- | --- |
| 1207 | 205,936 kB | api-server（父） |
| 1208 | 205,708 kB | api-server（子，刚 fork） |

「一个进程 200 MB，fork 一下变 400 MB，1.9 GB 的机器只能扛四五个并发，必须扩容。」

这个结论错在把 `RSS` 当成了「物理内存占用」。实测同一台机器（2 核 / 1.9 GB / 无 swap）上，一个已经触碰过 200 MB 匿名内存的进程 fork 一次，**`MemAvailable` 只掉了 756 kB**：

| 指标 | 父进程 | 子进程（未写任何页） | 读法 |
| --- | --- | --- | --- |
| `Rss` | 205,936 kB | 205,708 kB | **共享页在每个进程里各算一次**，相加没有意义 |
| `Pss` | 204,977 kB | 102,526 kB | 按共享者数量摊薄，**加起来才是真实物理占用** |
| `Shared_Clean` | 1,024 kB | 792 kB | 文件映射那点零头 |
| `Private_Dirty` | 204,912 kB | 44 kB | 子进程 fork 后只碰过自己栈上的几页 |
| `MemAvailable` | — | 掉 756 kB | 一次 fork 的真实代价 |

三个误解合成一条根因：**`fork` 复制的是页表项，不是物理页；RSS 把共享页在每个进程里各算一次；僵尸不占地址空间，但占账本（`task_struct`、内核栈、pid），且计入 `RLIMIT_NPROC` 与 `pids.max`。**

本篇就交付这三句话的推导：**进程是内核的一本账**（§23.2–23.3）；**`fork` 是抄账本，不是复印内容**（§23.4–23.5）；**退出是交账，收尸是父进程的事**（§23.7–23.8）。

---

## 23.2 内核里没有「进程」这个对象

在 Linux 源码里搜 `struct process`，什么也搜不到。内核只有一个 `task_struct`，加上它用指针牵着的一串对象；所谓「进程」，是这一串对象的**组合方式**给出的名字。

```
                     ┌───────────────────────────────────────────────┐
                     │            task_struct（几 KB 级）             │
                     │  __state  pid  comm  prio  sched_entity        │
                     │  parent / real_parent / children / sibling     │
                     └──┬────────┬────────┬─────────┬─────────┬───────┘
        ┌───────────────┘        │        │         │         └──────────────┐
        ▼                        ▼        ▼         ▼                        ▼
   mm_struct               files_struct  fs_struct  signal_struct        nsproxy
   页表 + VMA 树            fd 表        cwd/root   handler/统计          ns 集合
        │                        │
        ▼                        ▼
  PGD → 页表 → 物理页      struct file（offset、refcount）
```

`fork` 到底复制什么、共享什么，全写在这张图的边上。**图的下半部分是「资源的持有者」（mm/files/fs/signal/nsproxy），上半部分的 `__state`/`prio`/`sched_entity` 是「一个可被调度的实体」——两者合起来才是一个进程。** 把每个指针的「复制策略」列出来，本篇的目录就出来了：

| 字段 | 管什么 | `fork` 时怎么处理 | 线程（`clone`）是否共享 |
| --- | --- | --- | --- |
| `mm` / `active_mm` | 地址空间：页表 + VMA | **抄页表项并把可写项改成只读**（COW） | 共享（`CLONE_VM`） |
| `files` | fd 表（`fd → struct file`） | 复制 fd 数组，每个 `struct file` 引用计数 +1 | 共享（`CLONE_FILES`） |
| `fs` | cwd、root、umask | 引用计数 +1，父子指向同一个 `fs_struct` | 共享（`CLONE_FS`） |
| `signal` | 信号处置、统计、定时器 | 新建一份 `signal_struct`（线程组共享同一份） | 同线程组共享 |
| `sighand` | 信号 handler 表 | 引用计数 +1 | 共享（`CLONE_SIGHAND`） |
| `nsproxy` | pid/mnt/net/uts/ipc/cgroup/user ns | 引用计数 +1（除非带 `CLONE_NEW*`） | 共享 |
| `pid` / `thread_pid` | pid 号（每个 ns 一层） | `alloc_pid` 新分配 | 新分配 |
| `__state` | 状态机（§23.7） | 新设为 `TASK_RUNNING` | 独立 |
| `stack` | 内核栈（x86-64 上 16 KB） | 新分配并复制内容 | 独立 |
| `sched_entity` / `sched_class` | 调度实体与调度类 | 新初始化，入 runqueue | 独立 |
| `cred` | uid/gid/capabilities | 引用计数 +1 | 共享 |
| `rlimit` | 资源上限 | 复制一份（可各自改） | 独立 |

**这张表的读法是「四种动作」**：新建（`task_struct`、内核栈、pid、调度实体）、复制数组（fd 表、rlimit）、引用计数 +1（`fs`、`sighand`、`cred`、`nsproxy`）、以及唯一一个昂贵的动作——**抄页表**（`mm`）。`fork` 贵不贵，只取决于最后这一项。

### `/proc/PID` 就是这本账的只读投影

`/proc/<pid>/` 下每个条目都对应 `task_struct` 里的一个指针或一段统计。本机实测，一个普通进程的 `/proc/PID/status` 有 **61 个带冒号的字段**：

```
$ grep -c ':' /proc/self/status
61
$ ls /proc/self | wc -l
58
```

所以排障时不需要内核调试器：**你能在 `/proc` 里读到的每一个数字，都是内核账本上的一行**——`status` 是 `task_struct`/`mm`/`signal` 的摘要，`maps`/`smaps_rollup` 是 VMA 树，`fd/` 与 `fdinfo/` 是 `files_struct` 和 `struct file`，`ns/`、`cwd/`、`task/` 分别是 `nsproxy`、`fs_struct`、线程组。

### pid 是稀缺资源，不是身份证号

`struct pid` 里存的是**每个 namespace 一层**的编号，所以同一个进程在宿主里是 `10023`、在容器里可以是 `1`。本机 `pid_max = 4,194,304`，但 pid 号只有被 `release_task` 释放后才能复用；僵尸正卡在「已退出、未 `release_task`」的窗口里，**它占着号**。

三万个僵尸 = 三万个不可复用的 pid + 三万个 `task_struct` + 三万个内核栈。按 20 KB/个算，量级是 **600 MB**——「僵尸不占内存」只对了一半：它不占**地址空间**，但占**账本**。

---

## 23.3 地址空间：`mm_struct` → VMA → 页表

`mm_struct` 是「一个地址空间」的全部账本，核心是三类东西：页表、VMA 集合、以及几个计数。

| 字段 | 含义 | 规模（本机 200 MB 进程的实测/推算） |
| --- | --- | --- |
| `pgd` | 顶级页表物理地址（寄存器 `CR3` 指向它） | 1 个页（4 KB），512 项 |
| `mmap` / `mm_mt` | VMA 集合：链表用于顺序遍历，索引结构用于查找 | 实测 22 个 VMA |
| `mm_users` / `mm_count` | 用这个地址空间的线程数 / 持有 `mm_struct` 的计数 | `fork` 后 1，`clone(CLONE_VM)` 后 2 |
| `rss_stat` | 按类别（anon/file/shmem）记的常驻页数 | 与 `smaps_rollup` 对得上 |
| `total_vm` / `start_stack` / `brk` / `mmap_base` | 各段的边界 | `VmSize` 的来源 |

VMA（`vm_area_struct`）是**一段连续、权限相同的虚拟地址**，字段是 `vm_start`、`vm_end`、`vm_flags`、`vm_file`、`vm_pgoff`、`vm_ops`。Linux 6.1 起，VMA 的索引结构从红黑树换成了 maple tree（`mm_mt`），但 `mm->mmap` 链表仍保留，因为 `/proc/PID/maps` 和 `for_each_vma` 要按地址顺序走。**这解释了一件事：VMA 的数量影响 `mmap`/缺页的查找成本，而页表的数量影响 `fork` 的成本——它们是两本不同的账。**

### 页表：四级（或五级）间接

x86-64 的 4 KB 页、四级页表（开启 5-level paging 时多一级 P4D；4 级时 P4D 被折叠，图中保留它是为了对上内核代码里的 `p4d_offset`）：

```
CR3 ──► PGD ──► P4D ──► PUD ──► PMD ──► PTE ──► 物理页（4 KB）
        512 项   512 项   512 项   512 项   512 项
        每级一张 4 KB 表，每项 8 字节
```

每一级的一项覆盖的地址范围，可以直接乘出来：

| 级别 | 一项覆盖 | 一张表（512 项）覆盖 | 200 MB 需要几项 |
| --- | --- | --- | --- |
| PTE | 4 KB | 2 MB | 51,200 项（分布在 100 张表里） |
| PMD | 2 MB | 1 GB | 100 项（1 张表就够） |
| PUD | 1 GB | 512 GB | 1 项 |
| PGD | 512 GB | 256 TB | 1 项 |

于是 200 MB 数据的页表开销可以算出来：**100 张 PTE 表 × 4 KB = 400 KB，加一张 PMD 表 4 KB ≈ 404 KB，占 200 MB 的 0.2%**。这个数字在 §23.4 会再出现一次——它正好是 fork 时真正被复制的「内容」量级。

### `/proc/PID/maps` 真实输出

一个 `malloc(200 MB)` 并逐页触碰过的进程，`/proc/PID/maps` 共 **22 行**（22 个 VMA）：

```
55677adce000-55677adcf000 r--p 00000000 fe:01 428857   /tmp/opencode/forklab/big
55677adcf000-55677add0000 r-xp 00001000 fe:01 428857   /tmp/opencode/forklab/big
5567a55bb000-5567a55dc000 rw-p 00000000 00:00 0        [heap]
7f09b4677000-7f09c0e7b000 rw-p 00000000 00:00 0
7ffd487fc000-7ffd4881d000 rw-p 00000000 00:00 0        [stack]
```

挑五行，逐字段读：

| 行 | 地址范围 | 权限 | offset | 含义 |
| --- | --- | --- | --- | --- |
| 第 1 行 | `…ce000`–`…cf000`（4 KB） | `r--p` | `0` | ELF 头与程序头：只读、私有映射 |
| 第 2 行 | 相邻 4 KB | `r-xp` | `0x1000` | 代码段：可执行、私有；`p` = private（写时复制） |
| `[heap]` | 132 KB | `rw-p` | `0` | `brk` 堆；小分配走这里 |
| 第 4 行 | **200 MB** | `rw-p` | `0` | `malloc` 超过阈值走 `mmap`，一整条匿名 VMA |
| `[stack]` | 132 KB | `rw-p` | `0` | 主线程栈，带 `VM_GROWSDOWN` |

`maps` 回答的是「**有哪些区间、各自的权限是什么**」，它**不回答「这些页现在在不在内存里」**。后者要看 `smaps`（每个 VMA 的 `Rss`/`Pss`/`AnonHugePages`）或 `pagemap`。这条区别是 §23.4 那个「RSS 会骗人」的根源：**VMA 是承诺，PTE 是事实，RSS 是对事实的计数，PSS 是对事实的摊薄**。

顺带记一个实测细节：本机 `/sys/kernel/mm/transparent_hugepage/enabled` 是 `[always]`，但这个 200 MB 区域里 `AnonHugePages` 只有 12,288 kB（约 6%），其余仍是 4 KB 页。**THP 是「尽量」，不是「保证」**——后面算 COW 放大时要用到这个区别。

---

## 23.4 `fork` 的真实动作：抄页表，不抄页

`fork()` 在 glibc 里落到一次 `clone()`（带 `SIGCHLD`，不带任何资源共享标志；现代 glibc 还会加 `CLONE_CHILD_SETTID`/`CLONE_CHILD_CLEARTID` 来维护 TID），内核走 `kernel_clone` → `copy_process`。按顺序把关键步骤列出来，每一步的「复制策略」都不一样：

| 步骤 | 关键函数 | 动作 | 成本量级 |
| --- | --- | --- | --- |
| 1 | `dup_task_struct` | 新 `task_struct` + 新内核栈（16 KB），内容复制 | ~1–2 µs |
| 2 | `copy_creds` / `copy_fs` / `copy_sighand` | `cred`、`fs_struct`、handler 表引用计数 +1 | ~0.1 µs 各 |
| 3 | `sched_fork` | 初始化调度实体，分配调度类 | ~1 µs |
| 4 | `copy_files` | 新 fd 数组（浅拷贝），每个 `struct file` 引用计数 +1 | O(打开的 fd 数) |
| 5 | `copy_signal` | 新 `signal_struct`（统计、定时器、线程组） | ~1 µs |
| 6 | **`copy_mm`** | `dup_mm`：复制 VMA，**遍历页表，复制 PTE 并写保护** | **O(页数) ← 大头** |
| 7 | `copy_namespaces` / `copy_io` / `alloc_pid` | `nsproxy` 引用计数 +1、新 `io_context`、每层 ns 一个 pid | ~2 µs |
| 8 | `wake_up_new_task` | 子进程入 runqueue，等调度 | ~1 µs |

### 第 6 步到底做了什么

`copy_page_range` 沿 VMA 走，对每一个**存在的**页表项执行三件事：

1. `page_dup_rmap`：把物理页的 `mapcount` 加一（现在有两个 PTE 指向它）；
2. `ptep_set_wrprotect`：把**父子两侧**的 PTE 都清掉可写位；
3. 标记父进程需要 TLB flush（父进程 TLB 里那条可写的旧映射必须失效，否则父进程还能直接写共享页）。

注意这三件事**没有一件在复制 4 KB 数据**。物理页一个都没动，只是「指向它的 PTE 从一条变成两条，并且两条都不许写」。所以：**复制的是页表项**（200 MB → 51,200 个 PTE → 约 404 KB）；**共享的是物理页**（两个 PTE 指向同一批 `struct page`，`mapcount = 2`）；**写保护是 COW 的开关**（谁先写，谁触发缺页，谁付复制钱，§23.5）。

### 账面证据：756 kB 去哪了

回到 §23.1 那张表。fork 一个 200 MB 进程，`MemAvailable` 掉 756 kB，可以拆开对账：**子进程页表 ~404 KB**（100 张 PTE 表 + PMD 表，§23.3 算过）、**`task_struct` + 内核栈 ~20 KB**、**`signal_struct`/`io_context`/各段 VMA 的页表杂项 ~几十 KB**，合计 0.5–0.8 MB，与实测 756 kB 同量级。

同时，子进程的 `Private_Dirty` 只有 **44 kB**——这是它 fork 后自己碰过的几页栈与堆元数据（页表记在内核账上，不进 `smaps`），而 200 MB 数据在它账上全是共享的（`Pss` 102,526 kB ≈ 205 MB / 2）。**如果 fork 真的复制了 200 MB 物理页，`Private_Dirty` 会是 200 MB 量级，`MemAvailable` 会掉 200 MB。两个数字差三个数量级，这就是 COW 的全部证据。**

顺带把 VSS 也对上：**`fork` 不改变 VSS**——子进程继承同样大小的地址空间，`VmSize` 与父进程同量级（父进程实测 207,272 kB）；变的只是页表项背后的**物理页归属**。所以「fork 前后 VSS 不变、RSS 各自都像满的、只有 PSS 之和反映真实占用」是三句必须一起说的话。

### fd 表：复制的是数组，共享的是 `struct file`

这一步最容易被误解成「文件也被复制了」。实测：父进程打开文件读到偏移 5，fork 后子进程再读 5 字节，父进程看到的偏移是 10：

```c
int fd = open("fdsh.c", O_RDONLY);
char b[8];
read(fd, b, 5);                       /* 父：off = 5 */
pid_t c = fork();
if (c == 0) { read(fd, b, 5); _exit(0); }   /* 子：off 从 5 接着读 */
waitpid(c, NULL, 0);
printf("off=%ld\n", lseek(fd, 0, SEEK_CUR));
```

```
父：先读 5 字节，off=5
子：再读 5 字节，off=10
父：子读完后 off=10
```

原因：`copy_files` 复制的是 **fd 数组**（`fd → struct file *` 的指针表），`struct file` 本身引用计数 +1、父子共用一份——而**文件偏移 `f_pos` 存在 `struct file` 里**。所以父子共享偏移，但不共享「关闭」：一方 `close` 只减引用计数，另一方仍可读写。这条规则同时解释了管道为什么能当 IPC（同一根管道两端共享同一组 `struct file`）和日志为什么会被父子进程写乱（共享偏移 + 各自缓冲）。

### 为什么 `fork` 会有「两次返回」

子进程不是「从 `main` 重新开始」，而是**从 `ret_from_fork` 返回用户态**，返回值被构造成 `0`；父进程从系统调用返回，拿到子进程 pid；失败返回 `-1` 且不创建任何东西。所以用户态只能靠 `pid == 0` 区分父子——这是「一次调用、两处返回」的全部机制。

---

## 23.5 COW 的账单：第一次写，4 KB 起步

### 一次写缺页的五步

```
① 用户态执行 mov [addr], 1
② MMU 查 TLB/页表：PTE 存在（present=1）但可写位=0
③ 触发 #PF，错误码 = present|write（不是「页不存在」，是「页存在但不许写」）
④ 内核 do_wp_page → wp_page_copy：
     分配一个新物理页 → 复制旧页 4 KB → 新 PTE 指向新页、置可写
     旧页 mapcount 减一（若只剩父进程，恢复可写）
⑤ 返回用户态，重试那条指令（这次成功）
```

关键点：**这不是「缺页异常」里的「页不在」，而是「页在、但被 COW 写保护」**。`base/26` 会把三类缺页分清楚；本篇只关心它的单价。

### 单价与规模

§23.9 实验②的实测（200 MB 进程 fork 后，子进程写若干页）：

| 写入页数 | 总耗时 | 每页 | `Private_Dirty` | 子进程存活期间 `MemAvailable` 下降 |
| --- | --- | --- | --- | --- |
| 1 页 | 0.003 ms | 3.06 µs | 44 kB | 0 kB（落在噪声里） |
| 1,000 页（4 MB） | 1.6–5.8 ms | 1.6–5.8 µs | 4 MB 量级 | 几 MB（被宿主噪声淹没） |
| 51,200 页（200 MB） | 176–593 ms | 3.4–11.6 µs | 204,844 kB | 192,632–204,792 kB |

写 1 万页（40 MB）的实测是 95–106 ms（每页 9.5–10.6 µs；那一轮宿主更忙，干净轮的单页成本约 3.4–5.9 µs）。绝对值随负载漂，但**总耗时 ∝ 写页数**这条比例关系不变——1 万页正好落在 1 页与 5 万页之间。

每页 3–12 µs 是什么组成的？4 KB 的 `memcpy` 在 10 GB/s 下只要 **409 ns**，剩下的 3–11 µs 是：缺页陷入/返回（~1 µs）、新页分配与清零、`rmap` 更新、PTE 改写、以及**每页一次 TLB 刷新**（跨核时还是 IPI）。所以「COW 很便宜」只在**写得少**时成立：写一页 3 µs，写满 200 MB 要 0.2–0.6 秒。

### 放大倍数：一次写可能复制 4 KB，也可能复制 2 MB

| 你写的字节数 | 被复制的量 | 放大倍数 | 备注 |
| --- | --- | --- | --- |
| 1 字节（4 KB 页） | 4 KB | 4,096× | 最常见的 COW 粒度 |
| 1 字节（2 MB 大页） | 2 MB | 2,097,152× | THP 命中时；本机 200 MB 中只有 6% 命中 |
| 1 KB（跨两页） | 8 KB | 8× | 跨页边界 |
| 200 MB 全写 | 200 MB | 1× | COW 退化成真复制 |

**这就是「Redis 大实例 `bgsave` 之后延迟尖刺」「JVM 大堆进程 fork 子进程后卡顿」的同一个根因**：fork 本身只花几毫秒，但 fork 之后每一次写都在替内核还 COW 的债。按 4.5–11 µs/页算，一个 4 GB 的堆写满一遍是 `1,048,576 × (4.5–11) µs ≈ 5–11 s` 的纯内核时间，还不算它挤掉的业务 CPU。

### `fork` 为什么随内存变大而变慢

§23.9 实验②的另一半——同一个程序，RSS 不同时的 `fork` 最小耗时：

| 进程 RSS | 页数 | fork 最小耗时 | 折算每 PTE |
| --- | --- | --- | --- |
| 0 MB | 0 | 0.032 ms | — |
| 50 MB | 12,800 | 0.052–0.057 ms | ~2 ns |
| 100 MB | 25,600 | 0.46–0.47 ms | ~18 ns |
| 200 MB | 51,200 | 2.9–4.1 ms | 57–80 ns |

50 MB 那一行快得反常，因为此时页表还在 CPU 缓存里、VMA 也少；到 200 MB，页表页本身已经超出 L2，`copy_page_range` 变成一次**内存带宽 + 随机访问**的行走。**结论不是「线性」，而是「O(页数)，且常数随工作集变大而变大」**——这正是「fork 大内存进程慢」的准确说法：慢的不是数据量，是**页表项的数量与它们的缓存局部性**。

对照 §23.3 的 404 KB 页表：如果只是 `memcpy` 404 KB，按 10 GB/s 只要 40 µs；实测 3 ms 是它的 75 倍。**多出来的钱花在「逐项 walk、更新 rmap、写保护、记账」上，不是花在拷字节上。** 这条差异很重要：它解释了为什么 `fork` 的成本与「页表占多少字节」不成正比，而与「有多少个 PTE」成正比。

---

## 23.6 `execve` 为什么需要 `fork`

### `execve` 只换地址空间，不创建进程

`execve` 做的事可以一句话说完：**保留 `task_struct`（pid、fd、signal、cgroup 都不变，`FD_CLOEXEC` 的 fd 除外），把 `mm` 整个换掉**——卸载旧 VMA、读 ELF、映射段、重建栈、把入口地址写进 `pt_regs`，返回用户态时从 `_start` 开始跑。它**不创建**新的调度实体，所以它不能「凭空启动一个程序」：总得有一个已有的进程上下文来承载这次替换。

这就推出了 Unix 的两段式：**`fork` 造一个上下文，`execve` 换掉它的内容**。副作用是两个动作各自独立——可以在 `fork` 和 `execve` 之间改 fd、改信号、`chdir`、`setuid`，这正是 shell 重定向与 `sudo` 的实现空间。

### 代价：一次白干的页表复制

`fork` + `execve` 的浪费是结构性的：`fork` 花 3 ms 抄了 200 MB 进程的页表，`execve` 第一件事就是把整个 `mm` 扔掉。**这笔钱 100% 白花。**

`vfork` 就是为这笔浪费设计的：它用 `clone(CLONE_VM | CLONE_VFORK | SIGCHLD)`，子进程**共享父进程的地址空间**（不复制页表），父进程则被挂起，直到子进程 `execve` 或 `_exit`。实测父进程在 `vfork` 期间的状态：

```
    PID    PPID STAT WCHAN                  COMMAND
3296607 3296598 D    kernel_clone           vf2
```

`D` 是 `TASK_UNINTERRUPTIBLE`（§23.7）——父进程连信号都不响应，因为它**借出了自己的地址空间**，此时若让它继续跑，父子会互相踩栈。所以 `vfork` 的子进程有两条铁律：**只能调 `execve` 或 `_exit`，不能 `return`、不能改任何变量**。

`posix_spawn` 是这件事的工程封装：glibc 用 `CLONE_VM | CLONE_VFORK` 起子进程，在共享地址空间里做完 fd 重定向、信号复位，然后 `execve`。同一个 200 MB 进程里各跑 100 次 `/bin/true`：

```
fork+exec  /bin/true ×100: 322.35 ms 单次 3223.5 µs
posix_spawn/bin/true ×100:  37.23 ms 单次  372.3 µs
比值: 8.7×
```

（首次冷跑 `fork+exec` 是 11.2 ms/次，稳定后 3.2–3.6 ms；`posix_spawn` 稳定在 0.37–0.39 ms，比值 8.7–9.4×。**冷热差本身也是页表与 dentry 缓存的账。**）

| 方式 | 复制页表 | 地址空间 | 父进程 | 适用 |
| --- | --- | --- | --- | --- |
| `fork` | 是（O(页数)） | 独立（COW） | 继续运行 | 需要真正的并发子任务 |
| `vfork` | 否 | 共享 | **阻塞到子进程 exec/exit** | 只想立刻 exec |
| `posix_spawn` | 否（`CLONE_VM`） | 共享到 exec | 阻塞（短） | 库函数里启动外部程序的首选 |
| `clone3` | 看 flags | 看 `CLONE_VM` | 看 `CLONE_VFORK` | 需要精细控制时 |

### `clone3` 的 flags 就是「进程 / 线程 / 容器」的旋钮

`clone3` 把「共享哪些资源」变成了一张位图（`struct clone_args`）。同一套内核代码，靠 flags 组合出三种东西：

| flags 组合 | 得到什么 | 关键共享 |
| --- | --- | --- |
| `SIGCHLD` | 普通进程 | 什么都不共享 |
| `CLONE_VM \| CLONE_FS \| CLONE_FILES \| CLONE_SIGHAND \| CLONE_THREAD \| CLONE_SYSVSEM` | 线程（同一线程组） | 地址空间、fd、cwd、handler、信号队列 |
| 上述 + `CLONE_NEWPID \| CLONE_NEWNS \| CLONE_NEWNET \| CLONE_NEWUTS \| CLONE_NEWIPC` | 容器进程 | 命名空间隔离，其余照旧 |
| `CLONE_VM \| CLONE_VFORK` | `vfork` 语义 | 地址空间 + 父阻塞 |

所以「线程是轻量级进程」不是比喻，是**字面事实**：`task_struct` 是同一个结构体，`clone` 只决定哪些指针共享。共享 `mm` 就是线程；不共享就是进程；共享 `mm` 但不共享 `CLONE_THREAD`，就是 `vfork` 那种「两个进程一个地址空间」的组合。细节 → `base/24`；namespace 组合成容器 → `base/34`。

---

## 23.7 状态机：内核只关心「在等什么」

`task_struct.__state` 只有几个位，但每一位都对应一个**等待队列**与一条**唤醒路径**。先把常量与 `/proc`、`ps` 的三种表示对齐：

| 状态 | 值 | `ps` STAT | `/proc/PID/status` | 含义 | 谁把它唤醒 |
| --- | --- | --- | --- | --- | --- |
| `TASK_RUNNING` | `0x0000` | `R` | `R (running)` | **在 runqueue 里，或正在 CPU 上** | 调度器（不需要唤醒） |
| `TASK_INTERRUPTIBLE` | `0x0001` | `S` | `S (sleeping)` | 等事件，**可被信号打断** | 事件 + 信号 |
| `TASK_UNINTERRUPTIBLE` | `0x0002` | `D` | `D (disk sleep)` | 等 I/O，**不响应信号** | 只有事件（I/O 完成） |
| `TASK_KILLABLE` | `0x0102` | `D` | `D (disk sleep)` | 等 I/O，但致命信号可打断 | 事件或 `SIGKILL` |
| `__TASK_STOPPED` | `0x0004` | `T` | `T (stopped)` | 作业控制暂停 | `SIGCONT` |
| `__TASK_TRACED` | `0x0008` | `t` | `t (tracing stop)` | 被 `ptrace` 停住 | 调试器 |
| `EXIT_ZOMBIE` | `0x0020` | `Z` | `Z (zombie)` | 已退出，等父进程 `wait` | 父进程 `wait` |
| `EXIT_DEAD` | `0x0040` | `X` | `X (dead)` | 正在被释放 | `release_task` |
| `TASK_IDLE` | `0x0402` | `I` | `I (idle)` | 内核线程空闲，**不计 load** | 事件 |

### 一张图

```
                    ┌───────────────────────────┐
   fork ───────────►│     TASK_RUNNING (R)      │◄──────────────┐
                    └──┬─────────┬─────────┬────┘               │
       wait_event()    │         │         │  SIGSTOP           │ SIGCONT
                       ▼         ▼         ▼                    │
                ┌──────────┐ ┌──────────┐ ┌──────────┐          │
                │ S 可打断 │ │ D 不可打断│ │ T 暂停   │──────────┘
                └────┬─────┘ └─────┬────┘ └────┬─────┘
       事件/信号      │             │ I/O 完成   │
                      └─────────────┴────────────┘
                                    │
                                    │ do_exit（任何状态都可能走到这里）
                                    ▼
              EXIT_ZOMBIE (Z) ── 父进程 waitpid ──► EXIT_DEAD (X) ──► 释放
```

三个实测片段，对应三种最容易混的状态：

```
# vfork 的父进程：等子进程 exec/exit，不可打断
    PID    PPID STAT WCHAN                  COMMAND
3296607 3296598 D    kernel_clone           vf2
State:	D (disk sleep)

# kill -STOP 之后
    PID STAT COMMAND
3296384 T    sh
State:	T (stopped)

# 子进程退出、父进程不 wait
    PID    PPID STAT   RSS COMMAND
3320165 3320158 S     1468 zomb2
3320166 3320165 Z        0 zomb2
State:	Z (zombie)
```

### 为什么 `RUNNING` 与 `RUNNABLE` 是同一个状态

教科书把「运行」和「就绪」分成两个状态，Linux 把它们合并成 `TASK_RUNNING`。原因是**调度器只需要回答一个问题：这个实体在不在 runqueue 里？** 在，就有资格被选中；至于此刻是否真的在 CPU 上，由 `rq->curr` 这个指针回答，不需要再占一个状态位。所以：

- `ps` 里的 `R` 可能是「正在跑」，也可能是「排着队」；
- 单核机器上 `R` 的进程数可以大于 1（都排着）；
- `load average` 统计的是 `R` + `D` 的**数量**，不是 CPU 利用率——这条在 `base/25` 展开。

### `D` 为什么必须存在

`TASK_UNINTERRUPTIBLE` 看着像设计缺陷（连 `kill -9` 都杀不掉），但它保护的是**内核数据结构的一致性**：一个进程在等块设备 I/O 时，它可能正持有文件系统锁、正处在事务中间态；如果此时允许信号把它叫醒去执行 handler，handler 看到的就是一个「更新到一半」的世界。所以内核选择「宁可让进程不可杀，也不让它带着半截状态返回用户态」。

代价是运维熟悉的那个场景：**NFS 服务器失联、坏盘、云盘挂起时，一堆进程卡在 `D`，`load average` 飙升，`kill -9` 无效**。解法不是杀进程，是修 I/O 路径或重启机器。`TASK_KILLABLE` 是折中：等 I/O，但 `SIGKILL` 可以在安全点把它拆出来（NFS 客户端大量使用）。

---

## 23.8 僵尸与孤儿：退出是两步，不是一步

### 两步退出

```
进程调用 exit_group / 从 main 返回
        │
        ▼
 do_exit()：释放 mm、files、fs、signal 的份额；把退出码/信号/rusage 写进 task_struct
        │  exit_notify()：给父进程发 SIGCHLD；把自己的孩子 reparent 给 init/subreaper
        ▼
 __state = EXIT_ZOMBIE  ── 此后不再被调度，只剩一具 task_struct + 内核栈 + pid
        │
        │  父进程 waitpid() / wait4()
        ▼
 release_task()：释放 task_struct、内核栈、pid 号、/proc 目录项
```

**为什么必须两步**：退出码、终止信号、`rusage`（CPU 时间、缺页数）要交给父进程；pid 也不能立刻复用，否则父进程还没 `wait`，这个号就可能被新进程占用，`waitpid(pid)` 会认错人。所以内核选择「先交资源、后交尸体」，中间那段就是僵尸。

实测（完整输出见 §23.9 实验③）：`STAT=Z`、`RSS=0`、`PPid` 仍指向父进程；`/proc/<child>/status` 里 `VmRSS`/`VmSize` **连这一行都没有**，`cmdline` 是 0 字节——地址空间与用户态内存已经释放。但 `/proc/<child>` 目录还在，`pid` 还被占着，`task_struct` 还挂着。

### 孤儿：父先死，孩子被收养

如果父进程先退出，它的孩子会被 `reparent` 给**最近的 subreaper**（用 `prctl(PR_SET_CHILD_SUBREAPER, 1)` 声明的祖先进程），没有 subreaper 就交给该 pid namespace 的 **PID 1**。收养者会收到 `SIGCHLD`；正常的 init 会 `wait`，所以孤儿通常不会变成僵尸——除非 PID 1 自己不 `wait`（见下一节）。

### `waitpid` 的正确写法

```c
for (;;) {
    int st;
    pid_t r = waitpid(-1, &st, WNOHANG);   /* -1：任意子进程；WNOHANG：不阻塞 */
    if (r > 0) {
        if (WIFEXITED(st))       log_info("exit %d", WEXITSTATUS(st));
        else if (WIFSIGNALED(st)) log_info("signal %d", WTERMSIG(st));
        continue;                          /* 还有别的孩子，继续收 */
    }
    if (r == 0) break;                     /* 有孩子，但都还没退出 */
    if (errno == EINTR) continue;          /* 被信号打断：重试 */
    if (errno == ECHILD) break;            /* 没有孩子了：正常收工 */
    log_error("waitpid: %s", strerror(errno));
    break;
}
```

四个返回值必须都处理，实测：

```
round 0: 有子进程但都还没退出 (WNOHANG 返回 0)
round 2: 收到 pid=3310591 status=2560 退出码=10
round 3: 收到 pid=3310592 status=2816 退出码=11
round 4: 没有子进程了 (errno=10 No child processes)
$ echo $((2560 >> 8)) $((2816 >> 8))
10 11
```

三个细节：**`status` 不是退出码**（退出码在 bit 8–15，必须用 `WEXITSTATUS`；直接判非零会把「退出码 1」和「被信号 9 杀」混为一谈，`system()` 的返回值同理）；**`ECHILD` 不是错误**，是「收完了」，很多程序把它打成 ERROR，在日志里制造假故障；**`EINTR` 必须重试**，否则一次信号打断就会漏掉一个僵尸。

`SIGCHLD` 的三种处理方式要分清：默认（`SIG_DFL`）只是「不理会通知」，子进程仍会变僵尸，必须自己 `wait`；`SIG_IGN` 或 `SA_NOCLDWAIT` 会让内核**自动回收**，代价是**拿不到退出码与 rusage**；装 handler 并在里面 `waitpid` 能及时回收又保留状态，但 handler 里只能用 async-signal-safe 函数，且不要和主循环的 `waitpid` 抢同一个孩子。

### 容器里 PID 1 不收割，就是僵尸工厂

容器里的 PID 1 是应用本身（`docker run` 的 entrypoint），也是 namespace 里所有孤儿的收养者。如果它只 `wait` 自己 fork 的孩子、不处理 `SIGCHLD`、也不 `wait(-1)`，那么它自己的子进程退出会变僵尸，孙子进程被 reparent 给它后也会变僵尸——一条稳定的僵尸生产线。

实测一个「故意不 wait」的伪 init，五秒钟内五个孩子全部变 `Z`：

```
伪 init pid=3297906
    PID    PPID STAT COMMAND
3297907 3297906 Z    initlike
3297908 3297906 Z    initlike
3297909 3297906 Z    initlike
3297910 3297906 Z    initlike
3297911 3297906 Z    initlike
僵尸数: 5
```

修法有三层：应用自己写对 `wait` 循环；`docker run --init`（注入 `tini`，它就是一个小 `wait(-1)`）；K8s 里用 `shareProcessNamespace` 或 sidecar 时要确认谁负责收割。**不要用「重启容器」当解法**——它只是把账本清零。

### 为什么 `fork` 会返回 `EAGAIN`

`fork` 失败有四个独立来源，报错经常长得很像：

| 来源 | 触发 | 定位 | 备注 |
| --- | --- | --- | --- |
| `RLIMIT_NPROC` | 该 uid 的进程数到顶 | `ulimit -u`、`/proc/PID/limits` | **僵尸也计入** |
| cgroup `pids.max` | 该 cgroup 的 task 数到顶 | `cat /sys/fs/cgroup/pids.current pids.max` | 容器里最常见 |
| `pid_max` | 全局 pid 号用尽 | `cat /proc/sys/kernel/pid_max` | 本机 4,194,304 |
| 内存/overcommit | 连 `task_struct` 都分配不出 | `dmesg`、`/proc/meminfo` | 可能是 `ENOMEM` |

实测把 `RLIMIT_NPROC` 压到 60（以非 root 身份），第 60 个 `fork` 失败：

```
成功 fork 59 次后失败: errno=11 (Resource temporarily unavailable)
```

`errno 11` 就是 `EAGAIN`。**注意它和「内存不够」的 `ENOMEM` 是两回事**：看到 `EAGAIN` 先数 task，不要先加内存。

---

## 23.9 实测：四个实验，一台 2 核 1.9 GB 的机器

所有数字来自同一台机器：Linux 6.12、2 核、1,974 MB 内存、无 swap、4 KB 页、`pid_max=4,194,304`、`ulimit -u=7810`、THP=`[always]`。宿主还有其他负载，所以耗时给的是**区间**；结论只保证量级。

| # | 实验 | 命令 | 规模 | 证明了什么 |
| --- | --- | --- | --- | --- |
| ① | fork 200 MB 进程，父子账面 | `./cow2` | 200 MB | COW 不复制物理页 |
| ② | 写 1 页 vs 写全部页；fork 耗时 vs RSS | `./cost3`、`./cost` | 200 MB | COW 单价、fork 的 O(页数) |
| ③ | 制造僵尸并观察 | `./zomb2` + `ps` | 1 个僵尸 | `Z` 的账面形态 |
| ④ | `ulimit -u` 与 fork 失败 | `setpriv … bash -c 'ulimit -u 60; ./forktest'` | 60 | `EAGAIN` 的来源 |

### 实验①：fork 一个 200 MB 的进程

程序：`malloc(200 MB)` → `memset` 逐页触碰 → 读 `/proc/self/smaps_rollup` → `fflush(NULL)` → `fork` → 子进程不写任何页、只读自己的账面 → 父进程读 `/proc/meminfo` 的 `MemAvailable` 前后差。

```
父：fork 前         Rss=205936 kB  Pss=204977 kB  Shared_Clean= 1024 kB  Private_Dirty=204912 kB
子：未写任何页  Rss=205708 kB  Pss=102526 kB  Shared_Clean=  792 kB  Private_Dirty=    44 kB
MemAvailable: fork 前 318684 kB → fork 后 317928 kB（差 756 kB）
```

**读法**：父子 RSS 相加 402 MB 是假账，PSS 相加 307,503 kB ≈ 300 MiB 才是真账（拆解见 §23.4）；`Private_Dirty` 44 kB 说明没有复制任何数据页；`MemAvailable` 掉的 756 kB 正好对上「页表 404 KB + `task_struct`/栈 20 KB + 杂项」。

### 实验②：写 1 页 vs 写全部页

```c
size_t pages = (200UL << 20) / 4096;          /* 51,200 */
pid_t c = fork();
if (c == 0) {
    double t0 = now();
    for (size_t i = 0; i < pages; i++) p[i * 4096] = 2;   /* 每页写 1 字节 */
    printf("写 51200 页: %.3f ms，每页 %.2f µs\n", now() - t0, (now() - t0) * 1000 / pages);
    _exit(0);
}
```

`modes = {1, 1000, 51200}` 各跑一次 fork；另有一个 `fork` 计时程序对 `{0, 50, 100, 200}` MB 各取 5 次最小值。

```
写      1 页:     0.003 ms  每页   3.06 µs
       子存活期间 MemAvailable 下降 0 kB
写  51200 页:   176.042 ms  每页   3.44 µs
子：写完全部   Rss=206024 kB Pss=205037 kB Private_Dirty=204844 kB
       子存活期间 MemAvailable 下降 204792 kB
```

同一份数据的另一半——`fork` 耗时随 RSS 变化：

```
RSS=  0 MB  fork 最小耗时   0.032 ms  (页数 0)
RSS= 50 MB  fork 最小耗时   0.052 ms  (页数 12800)
RSS=100 MB  fork 最小耗时   0.460 ms  (页数 25600)
RSS=200 MB  fork 最小耗时   4.058 ms  (页数 51200)
```

**读法**：写 1 页只花 3 µs、内存几乎不动；写满 200 MB 花 176 ms、内存真的多出 200 MB（`Private_Dirty=204,844 kB`）。这是干净的一轮；宿主忙时同一实验可到 590 ms。fork 本身从 32 µs（空进程）涨到 4 ms（200 MB），涨的是页表项的数量。**一次 fork + 写满 = 4 ms + 176 ms；写满那部分才是真正的账单。**

### 实验③：制造一个僵尸

程序：`fork` 后子进程立刻 `_exit(7)`，父进程打印双方 pid 后 `sleep(20)`，期间不 `wait`。用 `ps` 与 `/proc/<child>/status` 观察。

```
parent=3320165 child=3320166
    PID    PPID STAT   RSS COMMAND
3320165 3320158 S     1468 zomb2
3320166 3320165 Z        0 zomb2
$ grep -E '^(Name|State|PPid|Threads)' /proc/3320166/status
Name:	zomb2
State:	Z (zombie)
PPid:	3320165
Threads:	1
```

**读法**：`STAT=Z`、`RSS=0`、`PPid` 仍指向父进程、`Threads=1`（僵尸的线程组只剩一个空壳）。`kill -9 3320166` 不会有任何效果——它已经不在调度器的世界里了；能清掉它的只有父进程的 `waitpid`，或者杀掉父进程让它被 init 收养。

### 实验④：`ulimit -u` 与 fork 失败

root 不受 `RLIMIT_NPROC` 约束，所以要用非 root 身份跑：

```
# setpriv --reuid=65534 --regid=65534 --clear-groups \
    bash -c 'ulimit -u 60; /tmp/opencode/forklab/forktest'
成功 fork 59 次后失败: errno=11 (Resource temporarily unavailable)
```

59 = 60 − 1（自己也算一个 task）。**这个实验的价值在于把「进程数上限」从抽象概念变成一条可复现的 `EAGAIN`**：容器里的 `pids.max` 是同一套逻辑的另一把锁，只是账记在 cgroup 上。

---

## 23.10 三列账：进程承诺了什么、收了多少、从哪开始崩

| 维度 | 承诺了什么 | 为此付了什么 | 从哪里开始失效 |
| --- | --- | --- | --- |
| 独立地址空间 | 每个进程看到一整片连续的虚拟内存，互不可见 | 每个进程一套页表（200 MB 数据 ≈ 404 KB 页表） | 页表本身也是内存；VMA 数量、THP、KSM 都会改变账本 |
| `fork` 创建 | 「复制一个进程」像复制一个变量一样简单 | `O(页表项数)` 的复制；物理页靠 COW 共享 | 大内存进程 fork 要几毫秒；fork 风暴直接打满 pid/task 配额 |
| COW | 不写就不复制，父子共享物理页 | 第一次写触发缺页：分配 + 复制 4 KB + 改 PTE + 刷 TLB | 写放大 4,096×（THP 下 2,097,152×）；写满即真复制，内存翻倍 → OOM killer 介入 |
| 调度实体 | 每个进程都能被公平调度、被抢占 | 每次上下文切换 1–3 µs（本节末段实测推导） | 线程/进程过多时，切换本身吃掉吞吐（`base/25`） |
| 退出与收尸 | 父进程能拿到退出码、信号、资源统计 | 僵尸占着 `task_struct`、内核栈、pid，直到 `wait` | 父进程不 `wait` → 僵尸堆积 → `pids.max`/`RLIMIT_NPROC` 到顶 → `fork` 失败 |
| 资源隔离（容器） | 一个 namespace + cgroup 组合 = 一个「像 VM 的盒子」 | 每个盒子仍有自己的 PID 1，仍要自己收尸 | PID 1 不 `wait` → 盒子内僵尸堆积；`pids.max` 是硬墙 |

把这笔账压成一句话：**进程买到的是「隔离 + 独立地址空间 + 可被调度」，付出的是「页表复制、上下文切换、以及一切跨进程通信都要绕内核」**。

还有一项不在表里，但同样重要：进程之间**不共享内存**（除非显式 `mmap(MAP_SHARED)`），所以管道、socket、共享内存、信号、futex 全都是「绕路」的方案，每一种都要多一次或多次跨界。这条账在 `base/30` 展开。

上下文切换的单价，可以用一个最小的同步往返测出来：父子两进程用两根管道 ping-pong 20,000 次，单次往返 4.36–10.36 µs。一次往返含「父阻塞→子被唤醒→子写→父被唤醒」两组动作，按 4 次切换估，单次约 **1.1–2.6 µs**（量级结论，含管道读写与调度延迟）。这个数字乘以每秒的阻塞唤醒次数，就是「线程/进程开太多」的真实成本。**fork 风暴、僵尸、OOM、COW 翻倍、`D` 卡死不是五种病，而是同一笔账的五个出口**——它们的定位命令在 §23.11 的踩坑表里。

---

## 23.11 踩坑清单：现象 → 根因 → 定位 → 修 → 防复发

| 现象 | 根因（指回本篇） | 定位命令 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| `fork: Resource temporarily unavailable` | task 数到顶：`RLIMIT_NPROC`/`pids.max`/`pid_max`（§23.8） | `ulimit -u`；`cat /sys/fs/cgroup/pids.{current,max}`；`ps -eo stat= \| grep -c '^Z'` | 先清僵尸/泄漏；再评估提限额 | 监控 `pids.current` 占用率 > 80% 告警 |
| 僵尸越积越多 | 父进程不 `wait` 或只 `wait` 部分孩子（§23.8） | `ps -eo stat,ppid,pid,comm \| awk '$1 ~ /Z/'` | 改成 `waitpid(-1, …, WNOHANG)` 循环 | 容器加 `--init`；supervisor 用现成库 |
| 「fork 之后内存翻倍」 | RSS 重复计数共享页（§23.4） | `grep -E 'Rss\|Pss\|Private_Dirty' /proc/PID/smaps_rollup`；看 `MemAvailable` | 用 PSS 之和做容量；别拿 RSS 相加 | 容量评审统一用 PSS 口径 |
| fork 大内存进程卡顿 | 页表复制 O(页数)（§23.5） | `time` 一次 fork；`strace -T -e trace=clone` | 改用 `posix_spawn`/`vfork`；减少常驻；拆小进程 | 避免在大堆进程里频繁 fork+exec |
| 多线程程序 fork 后子进程死锁 | fork 只复制调用线程；子进程可能继承别人持有的 malloc/stdio 锁 | `gdb -p` 看子进程卡在 `__lll_lock_wait` | fork 后只调 async-signal-safe 函数，立刻 `execve`；或改用 `posix_spawn` | 代码规范禁止在业务进程里裸 `fork` |
| `system()` 返回值判断错 | 返回的是 wait status，不是退出码（§23.8） | 打印 `status` 的十六进制；对照 `WEXITSTATUS` | 用 `WIFEXITED`/`WEXITSTATUS`；或改用 `posix_spawn` + 自己 `waitpid` | 封装 `run_cmd()`，禁止裸用 `system` |
| `nohup` 之后进程还是没了 | `nohup` 只忽略 `SIGHUP`、重定向输出，**不 `setsid`**；仍可能被 `SIGINT`、`SIGTTIN` 或 cgroup kill 干掉 | `ps -o pid,ppid,sid,pgid,stat -p PID`；`systemctl status` | `setsid` + 重定向 + 脱离 cgroup；或写成 systemd unit | 长驻进程一律交给 init 系统管理 |
| 子进程继承了不该继承的 fd | fd 表被复制（§23.4） | `ls -l /proc/PID/fd`；对比父子 | `O_CLOEXEC`、`close_range`、exec 前显式关闭 | 默认 `O_CLOEXEC`，代码评审检查 |
| fork 后同样的日志打了两遍 | 子进程复制了父进程未 flush 的 stdio 缓冲区 | 看重复行是否正好是 fork 前的输出 | fork 前 `fflush(NULL)`；或改用 `write` | 在 fork 前后统一 flush |

其中一行值得补一句机制：**多线程 `fork` 的死锁**不是「运气不好」，是 POSIX 的明确规定——`fork` 后子进程里只有**调用 `fork` 的那个线程**存在，其他线程可能在持有 `malloc`、`stdio` 锁的瞬间消失，子进程再去调 `malloc`/`printf` 就可能永远等一把不会再被释放的锁。Java 与 Go 的运行时有专门应对（`posix_spawn`/`CLONE_VFORK` 或 fork 后只做 exec），**业务代码不要自己跨过这条线**。

---

## 23.12 一页速记

```
【一句话】进程 = 地址空间 + 一堆内核簿记 + 一个可被调度的实体。
          fork 便宜是因为它抄的是页表项，不是物理页；
          僵尸是「死后没人收尸」，不是「没死」。

【fork 的四种动作】
  新建：task_struct、内核栈(16 KB)、pid、sched_entity
  复制数组：fd 表、rlimit
  引用计数 +1：fs(cwd/root)、sighand、cred、nsproxy
  昂贵：抄页表（O(PTE 数)），并把两侧 PTE 都改成只读

【三个数字】
  200 MB = 51,200 个 4 KB 页 → 页表约 404 KB（0.2%）
  fork 200 MB ≈ 3–4 ms；写满 200 MB ≈ 176–593 ms（3.4–11.6 µs/页，随负载漂）
  空进程 fork ≈ 32 µs；一次上下文切换 ≈ 1–3 µs

【COW】
  写 → PTE 只读 → #PF(present|write) → 新页 + 复制 4 KB + 改 PTE + 刷 TLB
  放大：4 KB 页 4,096×；THP 2 MB 放大 2,097,152×
  账要看 PSS 之和，不要看 RSS 之和

【状态机】
  RUNNING(0) 合并 running/runnable —— 调度器只问「在不在 runqueue」
  INTERRUPTIBLE(S) 可被信号叫醒 / UNINTERRUPTIBLE(D) 不可 / KILLABLE 可被 SIGKILL 拆
  STOPPED(T) / TRACED(t) / ZOMBIE(Z) / DEAD(X) / IDLE(I，不计 load)

【退出两步】
  do_exit：放 mm/files，留 task_struct+pid，发 SIGCHLD → EXIT_ZOMBIE
  release_task：父 wait 之后才释放 → 不留僵尸的唯一办法是 wait

【waitpid 四件事】
  WNOHANG 不阻塞；EINTR 重试；ECHILD 是收工；status 要用 WEXITSTATUS 解

【fork 失败的四个来源】
  RLIMIT_NPROC / cgroup pids.max / pid_max / 内存
  僵尸计入前两项 —— 所以「僵尸不占资源」是假话
```

---

## 23.13 自检 3 题

**Q1.** 一个已经用满 200 MB 匿名内存的进程 `fork` 一次。`ps` 显示父子 RSS 各约 200 MB，但 `MemAvailable` 只掉了不到 1 MB。请解释这三个数字为什么不矛盾；如果子进程随后写了 1,000 页（4 MB），父/子/系统的账分别怎么变？

<details><summary>答案</summary>

三个数字量的是三样东西。**RSS** 是「这个进程的页表里有多少页 present」：fork 后父子 PTE 指向同一批物理页，所以每个进程的 RSS 都还是 200 MB——同一批页被数了两遍。**MemAvailable** 是「系统还能再分配多少」：fork 只新增页表（≈404 KB）、`task_struct`、16 KB 内核栈，合计 0.5–0.8 MB，与 756 kB 吻合。**PSS** 是「按共享者摊薄后的占用」：父子各 ~102 MB，相加 ≈ 205 MB，等于真实物理占用。

子进程写 1,000 页（4 MB）之后：

| 账 | 父 | 子 | 系统 |
| --- | --- | --- | --- |
| 共享页 | 196 MB | 196 MB | 196 MB（物理只一份） |
| 私有页 | 4 MB（原来的） | 4 MB（COW 复制出来的） | 8 MB |
| PSS | 196/2 + 4 = 102 MB | 196/2 + 4 = 102 MB | **PSS 之和 = 204 MB** |
| `MemAvailable` | — | — | 比 fork 前少约 4 MB |

关键是：**写 4 MB 只让系统多花 4 MB，而不是 8 MB**——父进程那 4 MB 本来就是它的私有页；COW 只为「子进程新写的那 4 MB」分配新页。PSS 之和始终等于真实物理占用，这是它比 RSS 更适合做容量口径的原因。

</details>

**Q2.** 一个僵尸进程 `RSS=0`、`cmdline` 是 0 字节，为什么还能把 `fork` 拖垮？`kill -9` 为什么对它无效？清掉它的两种正确做法是什么？

<details><summary>答案</summary>

僵尸已经走完了 `do_exit`：地址空间（`mm`）、fd 表、`fs` 都释放了，所以 `RSS=0`、`cmdline` 为空，甚至 `/proc/PID/status` 里连 `VmRSS` 这一行都没有。但它**没有走 `release_task`**：`task_struct`、内核栈、pid 号还挂在账上，并且计入该 uid 的 `RLIMIT_NPROC`、所属 cgroup 的 `pids.current` 与全局 `pid_max`。所以三万个僵尸 = 三万个不可复用的 task/pid，`fork` 在配额处直接返回 `EAGAIN`。

`kill -9` 无效，是因为信号只对「还能被调度、还能返回用户态」的任务有意义；僵尸已经不在 runqueue 里，也没有用户态上下文可以接收信号。它唯一剩下的状态转换是「被父进程 `wait` 掉」。两种正确做法：**修父进程的 `waitpid(-1, …, WNOHANG)` 循环**（根治）；或**杀掉/重启父进程**，让僵尸被 reparent 给 subreaper/PID 1 回收——容器里更稳妥的是 `--init`（tini）。`SIGCHLD` 设成 `SIG_IGN` 也能自动回收，但拿不到退出码。

</details>

**Q3.** 在一个 200 MB 的进程里，`fork` + `execve("/bin/true")` 实测 3.2 ms，`posix_spawn` 只要 0.37 ms。差在哪？为什么 `vfork` 的父进程会处于 `D` 状态？`vfork` 的子进程为什么不能 `return`？

<details><summary>答案</summary>

差在**页表复制**：`fork` 必须遍历父进程的 51,200 个 PTE，复制并写保护（约 3 ms），然后 `execve` 立刻把整个 `mm` 扔掉——这 3 ms 是纯浪费。`posix_spawn` 用 `clone(CLONE_VM | CLONE_VFORK)`，子进程直接共享父进程的地址空间，**一个 PTE 都不复制**，只做 fd 重定向与 `execve`，所以只要 0.37 ms，差约 9 倍。

`vfork` 的父进程处于 `D`（`TASK_UNINTERRUPTIBLE`，实测 `wchan=kernel_clone`），因为它把自己的地址空间借给了子进程：此时若父进程继续跑，父子会共用同一个栈和同一批变量，任何一方写内存都会踩坏另一方。内核选择让父进程**不可中断地等待**，直到子进程 `execve` 或 `_exit`。

`vfork` 的子进程不能 `return`，因为 `return` 会用到父进程的栈帧并返回到调用者——而它共享的正是父进程的栈；一旦 `return`，父进程被唤醒后看到的是一个被改坏的栈。合法动作只有两个：`execve`（换掉整个地址空间）或 `_exit`（直接销毁自己）；也不能修改任何变量、不能调用会在父进程栈上分配内存的函数。

</details>

---

## 23.14 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 一次系统调用的完整路径：`syscall` 入口、`pt_regs`、返回用户态（本篇所有跨界成本的底座） | → `base/21` |
| ELF 怎么被映射进地址空间：`execve` 的另一半 | → `base/22` |
| 线程与 `clone` 的参数组合、用户态线程与协程 | → `base/24` |
| 调度器：CFS→EEVDF、nice、亲和性、`load average` 为什么算 `R`+`D` | → `base/25` |
| 页表与缺页：三类缺页、`do_wp_page` 的完整路径、THP | → `base/26` |
| `malloc`、RSS 为什么不降、OOM killer 的选择逻辑 | → `base/27` |
| fd、`struct file` 与终端：为什么子进程会继承 fd、为什么重定向能改变输出 | → `base/28` |
| 进程不共享内存：管道、共享内存、信号与 futex 的绕路账 | → `base/30` |
| 手搓容器：namespace + cgroup + `pivot_root`，以及 PID 1 的收割责任 | → `base/34` |
| 排障手册：`ps`/`strace`/`perf` 各自查哪一层 | → `base/54` |
| 收口：内核层「承诺 / 代价 / 崩点」三本账合订 | → `base/56` |
| Go 的 GMP 与 goroutine：M:N 调度凭什么能绕开「一个线程一个内核栈」 | [go/17](../go/17-goroutine-internals.md) |
| 全书总纲：四条主线与 56 篇清单 | [0-summary](0-summary.md) |
