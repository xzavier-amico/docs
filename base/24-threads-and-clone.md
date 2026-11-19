# 24. 线程与调度单元：`clone`、用户态线程与协程

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 操作系统：Linux 视角 ｜ **编号**：24 / 56
> **本篇定位**：Part III 的第四篇。23 讲进程是「地址空间 + 内核簿记」，本篇接着回答：既然线程和进程在 Linux 里都只是一个 `task_struct`，那「线程」这个词到底买到了什么、又卖掉了什么；以及 goroutine / 虚拟线程把调度器搬进用户态之后，省下的到底是哪一笔钱。全篇只有一条推论链——`clone(flags)` 决定共享哪些指针 → 共享的用锁打架、私有的用指针传递 → 切换成本 = 保存寄存器 + 换栈 + 可能的换页表 → 把切换挪到用户态就只剩「保存寄存器 + 换栈」。
> **读法**：§24.2 的 flag 表与 §24.4 的共享/私有表是全篇支点；§24.5 的实测表请对着自己的机器重跑一遍（十分钟）；§24.6 的手写切换只有三十行汇编，建议抄一遍，抄完你对「协程快」就不会再有神秘感；赶时间只读 §24.8–24.11 的四张表。
> **不在本篇**：`task_struct` 的完整字段与 `fork` 的 COW 细节 → base/23；CFS/EEVDF、nice、亲和性与 cgroup 配额 → base/25；futex 的内核实现、管道与共享内存 → base/30；页表与缺页 → base/26、base/27；一次系统调用的入口/出口 → base/21；容器三件套 → base/34；Go GMP 源码、抢占与 netpoll 实现 → [go/18](../go/18-gmp-scheduler-source.md)。

---

## 24.1 引子：一万个线程会崩，一万个 goroutine 不会

同一台机器（2 vCPU、1.9 GB、Linux 6.12），两个程序都只做一件事：创建 N 个「什么都不做、只阻塞」的执行单元。

先看线程：

```bash
$ ./spawn 10000
第 5001 个线程创建失败：Resource temporarily unavailable
成功创建 = 5000，请求 = 10000，pid = 3303025
```

第一反应通常是「内存不够」。但这台机器的 `pids.max` 是 5154，起测前已有 154 个任务：

```bash
$ cat /sys/fs/cgroup/user.slice/user-0.slice/pids.max
5154
$ cat /sys/fs/cgroup/user.slice/user-0.slice/pids.current
154
```

5154 - 154 = 5000。**第 5001 个线程不是被内存挡住的，是被「任务数配额」挡住的。** 8 MB 栈只是虚拟地址，尚未提交；真正先到顶的是 cgroup 的 pids 控制器。

再看同样一万个 goroutine：

```bash
$ ./gomany 10000
请求 goroutine = 10000，NumGoroutine = 10001，创建耗时 0.069 s（6897.6 ns/个）
OS 线程数(Threads) = 5，GOMAXPROCS = 2
VmSize = 711772 kB，VmRSS = 29444 kB
StackSys = 19.8 MiB，StackInuse = 19.8 MiB，Sys = 36.5 MiB
每个 goroutine 摊到 StackSys = 2074 B，Sys = 3826 B
```

一万个执行单元，**只有 5 个 OS 线程**，RSS 29 MB，每个 goroutine 的栈约 2 KB。没有魔法：只是把「8 MB 的栈」换成了「2 KB 的栈」，再把「谁来决定下一个跑谁」从内核搬进了 Go runtime。

| 执行单元 | 单元栈 | 一万个的栈账 | 本机实测 |
| --- | --- | --- | --- |
| `pthread` 线程 | 8 MB 虚拟地址（默认 `RLIMIT_STACK`） | 80 GB VA | 第 5001 个被 `pids.max` 拒绝 |
| goroutine | ~2 KB 起，按需增长 | ~20 MB | 10 000 个 RSS 29 MB，5 个 OS 线程 |

但要把话说完整：**在这台 1.9 GB 的机器上，100 万个 goroutine 也会死。** 实测这个进程直接收到 SIGKILL（exit 137）——因为 2 KB × 100 万 = 2 GB，已经超过整机内存，还没算 `g` 结构体、堆和页表。所谓「开 100 万个 goroutine 没事」，前提是那台机器有十几 GB 内存。**协程把上限从「内核任务数」换成了「内存 ÷ 单栈大小」，上限变高了，但没有消失。**

这一篇要拆的就是这两笔账：**同一性**（线程与进程只是 `clone` 的参数不同）与**位置**（切换发生在内核还是用户态）。

---

## 24.2 内核里没有「线程」这个类型：只有 `task_struct` 和一组 flag

Linux 内核里没有 `struct thread`，也没有「进程表」和「线程表」两套东西。**唯一的调度实体是 `task_struct`**；调度器（base/25）看到的永远是一个个 task，不关心它们共享了什么。用户态说的「进程」和「线程」，只是 `task_struct` 之间**哪些指针相同**的两种常见组合。

创建 task 的底层原语是 `clone`。它和 `fork` 的差别不在「复制得多不多」，而在**复制还是共享**：

```c
/* glibc 的 pthread_create 最终落到一次 clone（新内核上可能是 clone3） */
clone(child_fn, stack_top,
      CLONE_VM | CLONE_FS | CLONE_FILES | CLONE_SIGHAND |
      CLONE_THREAD | CLONE_SYSVSEM | CLONE_SETTLS |
      CLONE_PARENT_SETTID | CLONE_CHILD_CLEARTID,
      arg);
```

| flag | 值 | 置位后共享/新建什么 | 不置位时 |
| --- | --- | --- | --- |
| `CLONE_VM` | `0x00000100` | 共享 `mm_struct`（同一套页表） | 复制一份地址空间（`fork` 的 COW） |
| `CLONE_FS` | `0x00000200` | 共享 `fs_struct`：cwd、root、umask | 复制一份 |
| `CLONE_FILES` | `0x00000400` | 共享 `files_struct`：fd 表 | 复制 fd 表（指向同一批 file） |
| `CLONE_SIGHAND` | `0x00000800` | 共享 `sighand_struct`：信号处理函数 | 复制处理函数表 |
| `CLONE_THREAD` | `0x00010000` | 放进同一个线程组，共用 `tgid` | 新线程组（新 tgid） |
| `CLONE_NEWPID` | `0x20000000` | 为**之后创建的子进程**新建 pid namespace | 继承当前 pid namespace |
| `CLONE_NEWNS` | `0x00020000` | 新建 mount namespace | 继承当前 mount namespace |
| `CLONE_VFORK` | `0x00004000` | 共享地址空间，且父进程挂起直到子进程 `exec`/退出 | 不挂起 |

几条必须记住的约束：

- `CLONE_THREAD` 要求 `CLONE_SIGHAND`，`CLONE_SIGHAND` 又要求 `CLONE_VM`。因为「同一个线程组」意味着信号处理函数表和地址空间必须是同一份，否则信号语义无法成立。
- `CLONE_NEWPID` **只影响调用之后创建的子进程**；调用者自己仍留在旧 namespace。容器运行时必须先 clone 出子进程，子进程才是新 pid namespace 里的 1 号。
- `fork()` 在 glibc 里就是 `clone(SIGCHLD)`；`vfork()` 是 `clone(CLONE_VM | CLONE_VFORK | SIGCHLD)`；`pthread_create` 是上面那一长串。**三个 API，一个原语。**

把三种常见组合摆在一起：

| 资源 | `fork()` 出来的进程 | `pthread_create` 出来的线程 | 容器里的进程 |
| --- | --- | --- | --- |
| 地址空间 `mm` | 复制（COW），不共享 | 共享同一 `mm_struct` | 独立（新进程） |
| fd 表 `files` | 复制 | 共享 | 独立（除非显式继承/传递） |
| cwd/root/umask `fs` | 复制 | 共享 | 独立（`pivot_root` 后不同） |
| 信号处理函数 | 复制 | 共享 | 独立 |
| 线程组 `tgid` | 新 tgid | 同 tgid | 新 tgid |
| pid namespace | 继承 | 继承 | `CLONE_NEWPID` 新建 |
| mount namespace | 继承 | 继承 | `CLONE_NEWNS` 新建 |
| 调度实体 | 1 个 `task_struct` | 每个线程 1 个 `task_struct` | 每个进程/线程 1 个 `task_struct` |

用指针画出来，差异只有一行：

```
fork():       task_A[mm1, files1, fs1, sighand1]   task_B[mm2, files2, fs2, sighand2]
pthread:      task_A[mm1, files1, fs1, sighand1]   task_B[mm1, files1, fs1, sighand1]
container:    task_A[mm1, nsproxy1]                task_B[mm2, nsproxy2]   ← 新 namespace + 独立 mm
```

注意最后一行：**容器不是 `clone` 的某个特殊 flag，而是「一组 namespace flag + cgroup + 根目录切换」的组合**（base/34）。`task_struct` 本身完全不知道自己「在容器里」，它只知道自己的 `nsproxy` 指向哪组 namespace。

这也解释了为什么 Linux 的 `/proc` 把线程叫 `task`：`/proc/PID/task/` 下每一个条目都是一个 `task_struct`，内核并不给它们发「线程证」。

---

## 24.3 `pthread_create` 到底做了什么：clone + 栈 + TLS + 线程组

`pthread_create` 不是一个系统调用，它是 glibc 里的一段用户态代码。按顺序做五件事：

1. **分配栈**：默认 `mmap` 一块 `RLIMIT_STACK` 大小的区域（本机 8 MB），底部留一页 guard page；`pthread_attr_setstacksize` 可以改。
2. **放 TCB/TLS**：把线程控制块（`struct pthread`，也就是 `pthread_t` 指向的东西）和静态 TLS 块放在栈映射的高端；x86-64 用 fs 段寄存器指向它。
3. **调用 `clone`**：带上 `CLONE_VM | CLONE_FS | CLONE_FILES | CLONE_SIGHAND | CLONE_THREAD | CLONE_SETTLS | CLONE_CHILD_CLEARTID` 等 flag，子任务从 glibc 的启动 trampoline 开始跑。
4. **设置线程组**：`CLONE_THREAD` 让新 task 的 `tgid` 等于创建者的 `tgid`；`pid`（内核里的 tid）则是新的。
5. **准备 join**：`CLONE_CHILD_CLEARTID` 让线程退出时把用户态那个 `tid` 字段清零并 futex 唤醒；`pthread_join` 就在这个 futex 上等。

第 4 步是「线程」这个词的全部技术内容。看真实输出：

```bash
$ ./taskview
main: getpid=3297744 gettid=3297744
  worker 0: gettid=3297758
  worker 1: gettid=3297759
  worker 2: gettid=3297760
  worker 3: gettid=3297761
/proc/self/task 条目数 = 5
  Name:	taskview
  Tgid:	3297744
  Pid:	3297744
  Threads:	5
worker 0 的 /proc/self/task/3297758/status：
  Name:	taskview
  Tgid:	3297744
  Pid:	3297758
  Threads:	5
```

四个 worker 的 `Tgid` 都是 3297744，`Pid` 各不相同；主线程的 `Tgid == Pid`，因为它是组长。**`getpid()` 返回的是 `tgid`，`gettid()` 返回的才是内核的 `pid`。** 老代码里「一个进程一个 pid」的假设，在多线程程序里只对组长成立。

| 概念 | 内核字段 | 谁在用 | 备注 |
| --- | --- | --- | --- |
| 线程组 ID | `task->tgid` | `getpid()`、`/proc/PID`、`kill(pid)` | 全组相同，等于组长的 tid |
| 任务 ID | `task->pid` | `gettid()`、`/proc/PID/task/TID` | 每个 task 唯一 |
| `pthread_t` | glibc 的 `struct pthread *` | 用户态 pthread API | 不是内核 tid，别直接打印当 pid 用 |
| `Threads:` | `/proc/PID/status` | 监控、排障 | 该线程组的 task 数 |

两个容易踩的细节：

- **`/proc/PID` 是组长，不是「整个进程目录」的另一个名字**。组长先退出时，它会以僵尸（`ps -L` 里状态为 `Zl`）的形态保留，`/proc/PID/task/` 仍能看到存活的兄弟线程；判断「进程还在不在」不能只看组长的状态。实测：主线程 `pthread_exit` 后，`/proc/3328649/task` 里仍有僵尸组长和存活的 worker 两条。`getpid()` 拿到的仍是一个已经死掉的组长的 `tgid`，这会让不少只认 `pid` 的监控脚本误判。
- **栈不是「8 MB 都花了」**。本机 1000 个线程的实测：`VmSize = 8,198,732 kB`（约 8.19 GB），`VmRSS = 9,760 kB`（约 9.5 MB）。8 MB 是虚拟地址，RSS 只算真正碰过的页；但每个线程另有一块 16 KB 的内核栈和 `task_struct`，这部分不进 `VmRSS`，5000 个线程就是约 80 MB 内核内存。

如果嫌 8 MB 太大，`pthread_attr_setstacksize(&attr, 256 * 1024)` 能把 VA 降到 1/32；但内核栈、`task_struct`、调度开销一分不少，且栈变小后深递归更容易撞 guard page。**线程的贵，不只贵在栈大小。**

---

## 24.4 共享什么、私有什么：一把锁能打架，一个指针能传值

`clone` 的 flag 表可以背，但工程上真正要记的是下面这张「谁会打架」表：

| 资源 | 线程间 | 后果 | 典型事故 |
| --- | --- | --- | --- |
| 地址空间（全局变量、堆、`mmap`） | **共享** | 任何写都可能被另一个线程看见；需要锁/原子 | 数据竞争、计数丢失 |
| fd 表 | **共享** | 一个线程 `close`，其他线程的 fd 立即失效 | 日志 fd 被关、连接被误关 |
| 文件偏移 | **共享**（同一 open file description） | 两个线程 `read` 同一个 fd 会抢偏移 | 读到对方的一半 |
| cwd / root / umask | **共享**（`CLONE_FS`） | 一个线程 `chdir`，全组 cwd 都变 | 相对路径忽然找不到文件 |
| 信号处理函数 | **共享**（`CLONE_SIGHAND`） | `sigaction` 影响全组 | 库改了 handler，业务收不到 |
| 信号屏蔽字 | **私有**（`task->blocked`） | 每个线程可以有自己的 blocked 集合 | 以为 `pthread_sigmask` 是全局的 |
| 栈 | **私有** | 局部变量天然隔离；栈溢出只死一个线程 | 深递归 SIGSEGV |
| 寄存器 / 程序计数器 | **私有** | 切换时必须保存/恢复 | — |
| `errno` | **私有**（TLS） | 系统调用失败不会串到别的线程 | 多线程下 errno 被覆盖的错觉 |
| 其他 TLS（`__thread`） | **私有** | 每线程一份副本 | 线程池复用导致脏数据 |
| nice / 调度优先级 | **私有**（Linux 的 `task_struct`） | `nice()` 只改当前线程 | 以为调了「进程优先级」 |
| CPU 亲和性 | **私有** | `sched_setaffinity` 只改当前线程 | 只绑住了一个线程 |
| `robust_list` | **私有** | `pthread_mutex` 的 owner 死亡处理 | 线程崩溃后锁卡死 |

表里最反直觉的是：**Linux 的 nice、亲和性、信号屏蔽都是 per-task，不是 per-process。** POSIX 说「进程优先级」，Linux 实现成了「线程优先级」；`nice(5)` 只影响调用它的那个线程，`sched_setaffinity(0, ...)` 也只绑当前线程。多线程程序里想调「整个进程」，必须遍历 `/proc/self/task/` 或让每个线程自己设。

反过来，`chdir` 和 `umask` 是**全组共享**的，因为它们挂在 `fs_struct` 上。一个线程为了读相对路径 `chdir("/tmp")`，另一个线程的 `fopen("config.ini")` 就会去 `/tmp` 找——这类 bug 在日志里表现为「路径随机变化」。

共享与私有的分界，用一段真实输出看得最清楚：

```c
int g = 1;

static void *worker(void *arg) {
    printf("worker: &g=%p  &errno=%p\n", (void *)&g, (void *)&errno);
    g = 2;            /* 共享：改的是同一个变量 */
    errno = ENOENT;   /* 私有：改的是本线程的 TLS 副本 */
    return NULL;
}

int main(void) {
    pthread_t t;
    errno = E2BIG;
    printf("main  : &g=%p  &errno=%p\n", (void *)&g, (void *)&errno);
    pthread_create(&t, NULL, worker, NULL);
    pthread_join(t, NULL);
    printf("join 后: g=%d, errno=%d\n", g, errno);
    return 0;
}
```

```text
main  : &g=0x5620e63d3030  &errno=0x7fe4533036c0
worker: &g=0x5620e63d3030  &errno=0x7fe453302640
join 后: g=2（被 worker 改成 2）, errno=7（仍是 main 自己的 7）
```

`&g` 两行完全相同，`&errno` 两行相差 0x80 字节（TLS 块里的不同副本）。**共享 = 一把锁就能打架；私有 = 要传指针/消息才能通信。** 线程模型的所有便利与所有痛苦，都从这条分界线长出来。

还有一个常被忽略的组合：`CLONE_VM` 可以**不配** `CLONE_THREAD`。这样得到两个 task，共享地址空间但属于不同线程组——历史上 LinuxThreads 就是这么干的，每个「线程」有独立 pid，信号和 `/proc` 语义都很别扭。NPTL 加上 `CLONE_THREAD` 之后，才有了今天的线程组。**「线程」不是开关，是 flag 的组合；组合变了，语义就变了。**

---

## 24.5 上下文切换的真实成本：保存寄存器、换栈、换页表

一次切换，内核至少要做四件事；线程和进程的差别只在第三件：

| 组件 | 同进程线程切换 | 跨进程切换 | 量级 |
| --- | --- | --- | --- |
| 保存/恢复通用寄存器、PC、SP | 要做 | 要做 | 百纳秒级 |
| 保存/恢复 FPU/AVX 状态 | 要做（`xsave`/`xrstor`，视 CPU 状态大小） | 要做 | 数百纳秒，视状态大小 |
| 换内核栈、切 `task_struct` | 要做 | 要做 | 百纳秒级 |
| 换页表基址（CR3 / PCID） | **不做**（`mm` 相同） | **要做** | 数百纳秒起 |
| TLB / 缓存污染 | 较轻 | 较重 | 微秒到毫秒，取决于工作集 |
| 调度器记账、选下一个 task | 要做 | 要做 | 百纳秒到微秒 |

关键在第 4 行：**线程共享 `mm_struct`，所以切线程不用换 CR3；进程各有各的页表，切进程必须换。** 现代 CPU 有 PCID/ASID，换 CR3 不一定全量 flush TLB，但新地址空间的工作集仍然是冷的——这部分成本不体现在「切换指令」里，而体现在切换后那几微秒的 cache miss 上。工作集越大，这笔账越贵，所以「切换次数」本身不是全部，**切换后还要把数据重新焐热**才是。

本机实测（2 vCPU、AMD EPYC 7K62、负载 5.5–8.2，取多次运行的最小值）：

| 操作 | 一次往返 | 单程 | 说明 |
| --- | --- | --- | --- |
| 手写用户态栈切换 | 103–157 ns | **52–78 ns** | 两次 `ctx_switch`，纯用户态 |
| 两线程自旋 ping-pong | 557 ns | 279 ns | 无系统调用，cache line 在两核间弹跳 |
| Go channel ping-pong | 714 ns | 357 ns | 含 runtime 调度与 channel 锁 |
| `pipe` 跨进程 ping-pong | 5.34 µs | 2.67 µs | 4 次 syscall + 2 次调度 |
| `futex` 线程 ping-pong | 54.2 µs | 27.1 µs | 空 `futex_wake` 只要 0.6 µs，差价是排队 |
| `getpid` 系统调用 | — | 0.34 µs | syscall 基线 |
| `pthread_create` + `join` | — | 73 µs | 含 mmap/munmap 8 MB 栈 |

`pipe` 那行的 2.67 µs/单程可以拆开：4 次 syscall × 0.34 µs ≈ 1.36 µs，剩下约 4 µs 是两次唤醒加两次调度的钱，折合**每次内核调度约 2 µs**。这是本机（虚拟化 + 高负载）的量级；裸机上通常更低，但不会低到几十纳秒。**用户态切换的 52–78 ns 与内核调度的约 2 µs，差的是「进没进内核」，不是「线程还是协程」这个名字。**

`futex` 那行还藏着一个重要教训：一次空的 `futex_wake` 只要 0.6 µs，但完整的 ping-pong 要 27 µs/单程。**系统调用本身不贵，贵的是「唤醒之后等对方被调度上去」。** 这正是主线②「快慢只有一种解释——排队」在调度层的落点。

这也解释了为什么「线程池」通常比「来一个请求创建一个线程」快：创建线程要 mmap 栈、初始化 TLS、进内核 clone、退出再 munmap，一轮 73 µs；池化之后，请求只需要一次 futex 唤醒。但池化把「创建成本」换成了「共享状态 + 队列」，并不是白送。

---

## 24.6 用户态线程与协程：把「换栈」从内核手里拿回来

一个执行单元要能被切换，最少需要保存什么？在 x86-64 上：

- 6 个 callee-saved 通用寄存器：`rbx`、`rbp`、`r12`–`r15`；
- 栈指针 `rsp`；
- 返回地址（也就是 `rip`，靠 `ret` 弹出）。

就这些。浮点/向量状态由 ABI 规定为 caller-saved 或按需保存；地址空间不变，所以不用换 CR3；内核根本不需要知道发生了切换。下面这段三十行的程序，就是「协程切换」的全部物理内容：

```c
typedef struct { void *sp; } Ctx;
static Ctx mainc, coroc;

__asm__(
".globl ctx_switch\n"
"ctx_switch:\n"
"  push %rbp\n  push %rbx\n  push %r12\n  push %r13\n  push %r14\n  push %r15\n"
"  mov %rsp, (%rdi)\n"      /* 保存旧栈顶 */
"  mov (%rsi), %rsp\n"      /* 换到新栈 */
"  pop %r15\n  pop %r14\n  pop %r13\n  pop %r12\n  pop %rbx\n  pop %rbp\n"
"  ret\n"                    /* 弹出新上下文的返回地址 */
);

void coro_entry(void) { for (;;) ctx_switch(&coroc, &mainc); }

int main(void) {
    char stack[65536] __attribute__((aligned(16)));
    uintptr_t top = (uintptr_t)(stack + sizeof stack);
    uintptr_t sp = top - 64;
    uint64_t *p = (uint64_t *)sp;
    for (int i = 0; i < 6; i++) p[i] = 0;       /* 6 个假寄存器 */
    p[6] = (uint64_t)(uintptr_t)coro_entry;     /* ret 的目标 */
    coroc.sp = (void *)sp;

    long n = 10000000;
    double t0 = now();
    for (long i = 0; i < n; i++) ctx_switch(&mainc, &coroc);
    printf("手写用户态切换: %.2f ns/往返\n", (now() - t0) / n);
    return 0;
}
```

```text
手写用户态切换: 103.33 ns/往返, 51.67 ns/单程
手写用户态切换: 142.72 ns/往返, 71.36 ns/单程
手写用户态切换: 156.68 ns/往返, 78.34 ns/单程
```

15 条指令，52–78 ns。没有 syscall，没有内核栈，没有调度器，没有 CR3。**「协程快」不是因为叫协程，是因为它把切换压缩成了「push/pop + ret」。**

但只有「能切」还不够，还要回答三个问题：**谁来选下一个？栈从哪来？阻塞了怎么办？** 这三个问题决定了三种模型：

| 模型 | 谁调度 | 一个阻塞 syscall 的后果 | 并发上限 |
| --- | --- | --- | --- |
| 1:1（pthread） | 内核 | 只阻塞当前线程 | 栈 8 MB × N + 内核任务数 |
| N:1（早期 green thread） | 用户态库 | **整个进程都卡住**（没有其他 M 可跑） | 栈小，但只能用一个核 |
| M:N（Go、Java 虚拟线程） | 运行时 + 内核 | 运行时把 P 交给别的 M | 栈小 + 能用多核，代价是运行时复杂度 |

M:N 的核心是「三层」：

```
G  G  G  G  ...  （几十万个 goroutine，每个 2 KB 栈）
 \ |  |  /
  P0     P1        （GOMAXPROCS=2 个逻辑处理器，各带本地 runqueue）
  |       |
  M0     M1        （2 个 OS 线程，真正被内核调度）
```

- **G**：用户态执行单元，保存自己的栈和寄存器；
- **M**：OS 线程，真正占 CPU 的实体；
- **P**：调度上下文，持有 runqueue、内存缓存等；一个 M 必须绑定一个 P 才能执行 G。

Go 的 GMP 就是这张图；Java 的虚拟线程是「Continuation + 一组 carrier thread（ForkJoinPool）」，本质也是 M:N。区别在 API 和运行时实现，不在物理模型。**它们都允许「N 个执行单元」远大于「M 个内核线程」，因为切换和栈都在用户态。**

栈大小直接决定并发上限：

| 方案 | 单栈 | 1 万 | 10 万 | 100 万 |
| --- | --- | --- | --- | --- |
| pthread 默认 | 8 MB VA | 80 GB VA | 800 GB VA | 8 TB VA |
| pthread 调到 64 KB | 64 KB | 640 MB | 6.4 GB | 64 GB |
| goroutine | ~2 KB 起 | ~20 MB | ~200 MB | ~2 GB |
| Java 虚拟线程 | 几 KB 起，按需增长 | 几十 MB | 几百 MB | 几 GB |

表里算的是**执行单元自己的栈账**，不含 Go runtime 保留的几百 MB 地址空间，也不含 `g` 结构体与堆元数据。这张表要分两列读：**VA 账**和**RSS 账**。8 MB 是 VA，1000 个线程实测 8.19 GB VA、9.5 MB RSS；但内核栈 16 KB × 1000 = 16 MB 是实打实的内存，5000 个线程约 80 MB，且 `task_struct` 还没算。**线程的「8 MB」不是都花了，但「每线程几十 KB 的硬开销」一定花了。** 所以「把线程栈调小就能开一百万个线程」是半个谎言：VA 能过，内核任务数和内核内存仍然会拦你。

---

## 24.7 协程的边界：阻塞 syscall、CPU 密集与栈增长

用户态调度器有一个天然盲区：**它看不见内核里发生了什么。** 一个 goroutine 调用 `read` 阻塞了，runtime 如果还傻等，绑定的 M 就废了，P 也跟着废了。Go 的解法是把阻塞分成两类：

**第一类：可以被 netpoll 接管的 fd。** socket、`os.Pipe`（Go 1.9 起）等会被设成非阻塞并注册到 epoll。`read` 返回 `EAGAIN`，runtime 把 G 标记为等待并挂到 poller 上，M 立刻去跑别的 G。数据到达时，epoll 唤醒 netpoll，netpoll 把 G 放回 runqueue。**整个过程没有 M 被阻塞。**

**第二类：不可 poll 的阻塞调用。** 比如 `syscall.Read` 一个普通 pipe、`os.OpenFile` 一个 FIFO、某些设备 ioctl。runtime 只能 `entersyscallblock`：交出 P，让别的 M 接管；sysmon 发现 P 空闲而其他 M 都在阻塞，就再创建一个 M。等 syscall 返回，G 尝试抢回一个 P，抢不到就挂到全局队列，自己所在的 M 去 sleep。

真实输出把两类分得很清楚：

```bash
$ ./block net 1000
net   N=1000 goroutine=1000 OS 线程=5
$ ./block pipe 200
pipe  N=200  goroutine=200  OS 线程=5
$ ./block rawpipe 200
rawpipe N=200 goroutine=200 OS 线程=204
```

- `net.Listen` + `Accept`：1000 个 goroutine 停在 netpoll 上，OS 线程仍是 5 个；
- `os.Pipe` + `Read`：200 个 goroutine 停在 netpoll 上，仍是 5 个；
- `syscall.Pipe` + `syscall.Read`：200 个 goroutine 各阻塞一个 M，OS 线程涨到 204 个。

**「goroutine 很轻」的前提是「阻塞能被 runtime 接管」。** 一个 `syscall.Read` 就能把 M:N 打回 1:1：每个阻塞 G 都要占住一个独立的 M，连带 M 自己的栈、内核任务和 runtime 记账。工程上的对策是：能用 `net.Conn`/`os.File` 的 pollable 接口就别直接 `syscall.Read`；必须在 goroutine 里做阻塞调用时，给它配一个上限（worker pool / semaphore），别让它无限长。

CPU 密集是另一条边界。Go 1.14 之后有基于信号的异步抢占，sysmon 每 10 ms 左右检查一次，长时间不让出的 G 会被打断；但 `cgo`、手写汇编、`//go:nosplit` 的临界区仍然可能长时间占住 P。**一个死循环的 goroutine 不会杀死进程，但会让同 P 上的其他 G 一起挨饿。** Java 虚拟线程同理：`synchronized` 块、native 调用会把虚拟线程 pin 在 carrier 上，JDK 21 的文档专门列了 pinning 清单。

栈增长是第三条边界。Go 的栈是「连续栈」：初始 2 KB，不够时分配一块两倍大的新栈，把旧栈内容拷过去，再把指向栈的指针全部修正。好处是 goroutine 可以「按需长大」，坏处是：

- 拷贝成本与栈大小成正比，深递归 + 频繁增长会明显变慢；
- 指针修正需要 runtime 知道所有栈上指针，`unsafe.Pointer` 和 cgo 边界要特别小心；
- 默认最大栈 1 GB，超过就 `fatal error: stack overflow`；`debug.SetMaxStack` 可以调，但调大只是把崩溃推迟。

所以「协程里可以随便递归」也是错觉。**2 KB 的初始栈买到的是「大量并发」，不是「免费的大栈」。**

把「一个 goroutine 阻塞在 `read` 上，runtime 怎么把它挪走」压成五步（不展开 GMP 源码，细节看 [go/18](../go/18-gmp-scheduler-source.md)）：

1. `read` 在非阻塞 fd 上返回 `EAGAIN`；
2. `gopark` 把 G 的状态改成 waiting，挂到 poller 的等待队列；
3. 当前 M 释放 P，P 被其他 M 抢走继续跑别的 G；
4. 数据到达，epoll 就绪，netpoll 把 G 改成 runnable，放回 runqueue；
5. 某个 M 拿到 P，调度到 G，`read` 重试并成功返回。

第 2–4 步就是「把阻塞从 M 上挪走」的全部魔法；没有内核参与调度决策，只有 epoll 在通知「这个 fd 好了」。

---

## 24.8 实测：2 核、1.9 GB 上的四组数

环境：Debian 12、Linux 6.12.113、glibc 2.36、Go 1.19.8、AMD EPYC 7K62（2 vCPU、2.6 GHz）、1.9 GB 内存、无 swap。负载 5.5–8.2，所有延迟取多次运行的最小值。**先看限额，再动手**：本机 `pids.max=5154`、`threads-max=15621`、`max_map_count=65530`，所以线程实验只推到失败就停，没有把机器打挂。

### 实验①：一万线程 vs 一万 goroutine

```bash
$ ./spawn 10000
第 5001 个线程创建失败：Resource temporarily unavailable
成功创建 = 5000，请求 = 10000，pid = 3303025

$ ./gomany 10000
请求 goroutine = 10000，NumGoroutine = 10001，创建耗时 0.069 s（6897.6 ns/个）
OS 线程数(Threads) = 5，GOMAXPROCS = 2
VmSize = 711772 kB，VmRSS = 29444 kB
StackSys = 19.8 MiB，StackInuse = 19.8 MiB，Sys = 36.5 MiB
每个 goroutine 摊到 StackSys = 2074 B，Sys = 3826 B
```

线程失败不是 `ENOMEM`，是 `EAGAIN`：`pids.max` 先到。goroutine 成功不是「内核更偏爱 Go」，是 10 000 × 2 KB = 20 MB，账算得过来。

### 实验②：切换与创建的成本

```bash
$ ./uswitch
手写用户态切换: 103.33 ns/往返, 51.67 ns/单程
$ ./pingpong spin
spin  ping-pong: 2000000 次往返, 1.115 s, 557.5 ns/往返, 278.8 ns/单程
$ ./pingpong pipe
pipe  ping-pong: 20000 次往返, 0.107 s, 5337.2 ns/往返, 2668.6 ns/单程
$ ./pingpong futex
futex ping-pong: 100000 次往返, 5.415 s, 54150.7 ns/往返, 27075.3 ns/单程
$ ./pingpong create
pthread create+join: 2000 次, 0.146 s, 73206.0 ns/次
$ ./goswitch 1000000
chan ping-pong: 1000000 次往返, 0.714 s, 713.8 ns/往返, 356.9 ns/单程
```

手写切换 52 ns，`pthread` 创建 73 µs：**差三个数量级**。这不是「协程比线程快三倍」，是「不需要内核的切换」与「需要内核的创建」在比。

### 实验③：`/proc/self/task` 与线程组

```bash
$ ./taskview
main: getpid=3297744 gettid=3297744
  worker 0: gettid=3297758
  worker 1: gettid=3297759
  worker 2: gettid=3297760
  worker 3: gettid=3297761
/proc/self/task 条目数 = 5
  Name:	taskview
  Tgid:	3297744
  Pid:	3297744
  Threads:	5
worker 0 的 /proc/self/task/3297758/status：
  Name:	taskview
  Tgid:	3297744
  Pid:	3297758
  Threads:	5
```

`/proc/self/task` 的条目数就是 task 数，不是「进程数」；`Tgid` 相同、`Pid` 不同，就是线程组的定义。

线程的栈账：

```bash
$ ./stackcost 0
Threads:	1
VmSize:	    2468 kB
VmRSS:	    1424 kB
$ ./stackcost 1000
Threads:	1001
VmSize:	 8198732 kB
VmRSS:	    9760 kB
```

1000 个线程多出 8.19 GB 虚拟地址，但只多出约 8.3 MB RSS。**「8 MB 栈」是 VA 账单，不是 RSS 账单；但内核栈和 task_struct 是另一本账。**

### 实验④：阻塞能不能被 runtime 接管

```bash
$ ./block net 1000
net   N=1000 goroutine=1000 OS 线程=5
$ ./block pipe 200
pipe  N=200  goroutine=200  OS 线程=5
$ ./block rawpipe 200
rawpipe N=200 goroutine=200 OS 线程=204
```

同样的 200 个 goroutine，走 netpoll 只要 5 个 OS 线程，走 `syscall.Read` 要 204 个。**「M:N」不是免费的，它依赖「阻塞能被 poller 接管」这个前提。**

还有一条共享/私有的直接证据：

```bash
$ ./shared
main  : &g=0x5620e63d3030  &errno=0x7fe4533036c0
worker: &g=0x5620e63d3030  &errno=0x7fe453302640
join 后: g=2（被 worker 改成 2）, errno=7（仍是 main 自己的 7）
```

同一个 `g` 地址，不同的 `errno` 地址——共享与私有在地址空间里就是这么分的。

---

## 24.9 三列账：并发抽象承诺了什么、收了多少、从哪开始失效

| 维度 | 承诺了什么 | 为此付了什么 | 从哪里开始失效 |
| --- | --- | --- | --- |
| 并发抽象 | 让「同时处理很多事」看起来像写顺序代码 | 每个执行单元一份栈、寄存器、TLS | 单元数撞 pids/内存/内核任务上限（线程爆炸） |
| 资源共享 | 线程间传指针就能共享数据，不用序列化 | 共享即竞争：锁、原子、缓存行 | 锁竞争、死锁、优先级反转；伪共享见 base/13 |
| 切换 | 用户态几十纳秒就能换一个执行流 | 保存寄存器 + 换栈；缓存/TLB 不预热 | 阻塞 syscall、CPU 密集、cgo 把 M 占死 |
| 栈 | 协程初始 2 KB，按需增长 | 栈拷贝、指针修正、最大 1 GB 的硬顶 | 深递归、大局部变量、栈溢出 |
| 调度 | runtime 在用户态做 M:N，避免每次切换进内核 | 调度器看不见内核阻塞，需要 netpoll/抢占配合 | 不可 poll 的 fd、`//go:nosplit`、native 调用 |
| 隔离 | 线程共享地址空间，通信零成本 | 一个线程踩坏内存，全组一起死 | 越界写、use-after-free、信号处理错误 |
| 可观测性 | `Threads:`、`/proc/task`、runtime 指标都能看 | 线程/协程数量与真实并发不是一回事 | 只看线程数，忽略 runqueue 排队 |

一句话收束：**线程买到的是「共享地址空间的并行」，付的是「栈 + 内核任务 + 锁」；协程买到的是「便宜的海量并发」，付的是「运行时复杂度 + 阻塞边界」。** 两者不是替代关系，是同一笔账在不同位置的取舍。

---

## 24.10 踩坑清单：现象 → 根因 → 定位 → 修 → 防复发

| 现象 | 根因（指回本篇） | 定位命令 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| `pthread_create: Resource temporarily unavailable` / Java `unable to create native thread` | 撞 pids cgroup、`RLIMIT_NPROC`、`threads-max` 或内核内存（§24.1、§24.8） | `cat /proc/<pid>/status \| grep Threads`；`cat /sys/fs/cgroup/.../pids.{current,max}`；`dmesg` | 用线程池/协程；调大 pids.max；减栈；排查泄漏的线程 | 给线程数设硬上限；监控 `Threads` 与 `pids.current` |
| 线程结束后内存/任务数不降 | joinable 线程未 `join` 也未 `detach`，glibc 保留描述符/栈缓存（§24.3） | `ls /proc/<pid>/task \| wc -l`；`pmap -x` 看 8 MB 映射 | `pthread_join` 或 `pthread_detach`；池化复用 | 每个创建点配一个明确的回收路径；Code review 查 `pthread_create` |
| 线程池里的请求偶尔读到上一个请求的数据 | `__thread`/TLS 或 `pthread_setspecific` 的缓冲区没清（§24.4） | 在 TLS 变量写入/读取处打印线程 id 与内容 | 每次任务开始清零；用 `pthread_key` 析构器释放 | TLS 只放「无状态缓存」，不放请求数据 |
| `fork()` 后子进程偶发死锁 | 多线程程序 fork 后只剩调用线程，其他线程持有的 malloc/日志锁永远不会释放 | `gdb` attach 子进程看 `pthread_mutex_lock` 栈；`strace -f` | fork 后立刻 `exec`；用 `posix_spawn`；`pthread_atfork` 加锁/解锁 | 禁止在多线程进程里「fork 后长时间跑业务代码」 |
| 协程里做 CPU 密集，其他协程全部变慢 | 长任务占住 P，抢占要等 sysmon 的检查周期（§24.7） | `go tool pprof` 看 CPU；`GODEBUG=schedtrace=1000` 看 runqueue | 拆成小段让出；放到 worker pool/独立 M；cgo 另算 | 约定「协程里不做超过 X ms 的纯计算」 |
| 协程里 `syscall.Read` 导致线程数暴涨 | 不可 poll 的阻塞调用，runtime 只能为每个阻塞 G 配一个 M（§24.7） | `/proc/<pid>/status` 的 `Threads`；`block` 实验对照 | 改用 pollable 接口；给阻塞调用加并发上限 | 代码规范禁直接 `syscall.Read`，统一走封装 |
| 以为 `nice()`/亲和性改了整个进程 | Linux 的 nice、affinity、signal mask 是 per-task（§24.4） | `ps -L -o pid,tid,ni,psr`；`taskset -pc <tid>` | 遍历 `/proc/self/task/*` 逐个设，或让线程自己设 | 文档里写清「进程级」在 Linux 上是「线程组级」 |
| 深递归/大局部变量导致某个线程 SIGSEGV | 线程栈固定 8 MB，guard page 触发（§24.3） | `dmesg` 看 segfault 地址；`gdb` 看 `rsp` | 改递归为迭代；调大 `pthread_attr_setstacksize`；协程改用堆上状态 | 栈上不放大数组；静态检查大局部变量 |

这张表可以压成三句话：**创建失败先看配额，不降先看有没有 join，行为诡异先分清共享与私有。**

---

## 24.11 一页速记

```text
【一句话】Linux 没有线程类型，只有 task_struct + clone(flags)；
         线程 = 共享 mm/files/fs/sighand 的 task，进程 = 不共享的那套组合。

【clone 五个关键 flag】
  CLONE_VM      0x100      共享地址空间（不设 = fork 的 COW）
  CLONE_FS      0x200      共享 cwd/root/umask
  CLONE_FILES   0x400      共享 fd 表
  CLONE_SIGHAND 0x800      共享信号处理函数（要求 CLONE_VM）
  CLONE_THREAD  0x10000    同一线程组/tgid（要求 CLONE_SIGHAND）
  CLONE_NEWPID  0x20000000 给之后创建的子进程新 pid namespace

【三个数】
  线程默认栈 8 MB（VA）；1000 个线程 = 8.19 GB VA、9.5 MB RSS
  goroutine 初始栈 ~2 KB；10 000 个 = 20 MB 栈、29 MB RSS、5 个 OS 线程
  手写用户态切换 ~52–78 ns；内核调度一次 ~2 µs；pthread 创建 ~73 µs

【共享 vs 私有】
  共享：地址空间、fd 表、cwd/umask、信号处理函数、tgid
  私有：栈、寄存器、errno、TLS、信号屏蔽字、nice、亲和性

【协程快的真正原因】
  不是名字，是「切换只需 push/pop/ret + 换栈」，且不进内核、不换 CR3。
  代价：阻塞 syscall 要 runtime 接管（netpoll），CPU 密集要抢占，栈要增长。

【两条边界】
  不可 poll 的 read → 每个阻塞 G 拖一个 M（200 goroutine → 204 线程）
  栈账：8 MB × 1 万 = 80 GB VA；2 KB × 100 万 = 2 GB，仍会 OOM
```

---

## 24.12 自检 3 题

**Q1.** 本机 `pids.max=5154`、起测前 `pids.current=154`，为什么 `./spawn 10000` 恰好死在「第 5001 个」？`ulimit -u` 显示 7810，为什么没有生效？如果把这台机器的 pids.max 调到 20000，10000 个线程就一定能创建成功吗？

<details><summary>答案</summary>

第一问是减法：5154 - 154 = 5000，当前线程组加上 5000 个新 task 正好到顶，第 5001 个 `clone` 返回 `EAGAIN`。

第二问是权限：`RLIMIT_NPROC` 对 root（更准确地说，拥有 `CAP_SYS_RESOURCE`/`CAP_SYS_ADMIN` 的进程）不生效；本机是 root，所以 `ulimit -u 7810` 只是显示值，真正拦人的是 cgroup v2 的 pids 控制器。**限额不止一层，先撞到的那层才是答案。**

第三问是不一定。pids 放开后，10 000 仍在 `threads-max=15621`、`max_map_count=65530`（每线程栈约 2 个 VMA，10 000 线程约 20 000 个）之下，80 GB 用户 VA 在 48 位地址空间里也放得下；真正可能拦住的是内存：10 000 ×（16 KB 内核栈 + `task_struct` 等）约 200–300 MB，而本机 `MemAvailable` 只剩两百多 MB，且 `threads-max` 是全局共享的。**所以「调大 pids.max」只是拆掉第一道闸，不等于一定能开一万个；按本机余量，失败风险仍然很高。** 稳妥做法是换小栈 + 池化，或者直接用 goroutine/虚拟线程。

</details>

**Q2.** 同样是 200 个 goroutine，为什么 `os.Pipe` + `Read` 只产生 5 个 OS 线程，而 `syscall.Pipe` + `syscall.Read` 产生 204 个？请说出 runtime 在这两种情况下分别走了哪条路径。

<details><summary>答案</summary>

`os.Pipe` 返回的 fd 在 Go 1.9 之后会被设成非阻塞并注册到 netpoll（Linux 上是 epoll）。`Read` 返回 `EAGAIN` 时，runtime 走 `gopark`：把 G 挂到 poller 等待队列，释放 P，M 去跑别的 G。数据到达后 epoll 就绪，netpoll 把 G 放回 runqueue。**M 从来没有被阻塞，所以 200 个 G 只需要 5 个 M。**

`syscall.Pipe` 创建的是普通阻塞 pipe，`syscall.Read` 直接进内核睡眠。runtime 只能走 `entersyscallblock`：交出 P，并让 sysmon 在需要时创建新的 M 来接管 P。每个阻塞的 G 都拖着一个 M，200 个 G 就长出约 200 个 OS 线程（204 = 200 + runtime 自带的几个）。

这条边界是 M:N 的命门：**goroutine 的「轻」依赖「阻塞可被 poller 接管」；一旦进了不可 poll 的系统调用，就从 M:N 退回 1:1，每个阻塞 G 还占着一个 M 的栈和内核任务。** 细节看 [go/18](../go/18-gmp-scheduler-source.md)。

</details>

**Q3.** 一个服务原来用 1000 个 pthread（默认 8 MB 栈），`VmSize` 约 8.2 GB、`RSS` 约 1.2 GB；改成 1000 个 goroutine 后，线程栈那 8 GB 的 VA 账没了，`RSS` 却仍是 1.15 GB，而不是按「8 MB → 2 KB」降 4000 倍。为什么？

<details><summary>答案</summary>

因为 8 MB 是**虚拟地址**，不是 RSS。本机 1000 个空闲线程的实测是 `VmSize = 8.19 GB`、`VmRSS = 9.5 MB`——8 GB 的栈映射里绝大多数页从未被触碰。那 1.2 GB RSS 的大头是应用堆/数据，不是线程栈；换成 goroutine 只能把 8 GB 的 VA 账消掉，动不了堆，所以 RSS 几乎不变。

就算每个线程真的碰过 1 MB 栈，这 1 GB 也是真实 RSS；而 goroutine 做同样的工作也会把栈长到约 1 MB，仍然不会出现 4000 倍下降。本机 10 000 个空闲 goroutine 的 RSS 是 29 MB，折合每个约 2.9 KB（含 `g` 结构体与堆开销），这已经是「几乎不干活」的下限。

所以正确的比较是 **VA 账 vs RSS 账**：VA 上 8 MB → 2 KB 是 4000 倍；RSS 上还要加上内核栈、`task_struct`、堆元数据，倍数会小得多。**「换成协程省 4000 倍内存」是把 VA 账单当成了 RSS 账单。** 真正的收益是「同样内存下能开的并发数」和「切换不进内核」，不是「每个单元只花 2 KB」。

</details>

---

## 24.13 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 进程是什么：`task_struct`、`fork`/COW、僵尸与状态机（本篇的前置） | → base/23 |
| 调度器：CFS→EEVDF、nice、亲和性、cgroup 配额（本篇的下一层） | → base/25 |
| futex、管道、共享内存：锁在没有竞争时为什么不进内核 | → base/30 |
| 缓存一致性与伪共享：共享地址空间的隐藏成本 | → base/13 |
| 虚拟内存与缺页：线程共享的页表到底长什么样 | → base/26、base/27 |
| 容器：namespace + cgroup + `pivot_root` 三件套 | → base/34 |
| goroutine 的内部结构：栈、`g`、状态机 | [go/17](../go/17-goroutine-internals.md) |
| GMP 调度器源码：P 的 runqueue、sysmon、netpoll、抢占 | [go/18](../go/18-gmp-scheduler-source.md) |
| 结构化并发：怎么让「创建了就一定回收」 | [go/26](../go/26-structured-concurrency.md) |
