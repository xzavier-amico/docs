# 30. IPC 与内核同步原语：管道、共享内存、信号与 futex

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 操作系统：Linux 视角 ｜ **编号**：30 / 56
> **本篇定位**：Part III 的第十篇。23 讲「进程 = 地址空间 + 内核簿记」，24 讲「线程 = `clone` 的共享参数组合」，26 讲「每个进程的地址空间是页表维持的幻觉」。本篇把这三句话接起来，回答一个更硬的问题：**两个地址空间之间，到底还能共享什么、通信要付几笔钱**。全篇只有一条推论链——`地址空间彼此不可见` → `通信只能靠内核搬运，或靠把同一段物理页映射两次` → `共享映射没有同步语义` → `同步必须自己搭` → `无竞争时把同步留在用户态、有竞争才进内核` → `这就是 futex，也是所有锁库快路径的原型`。管道、共享内存、信号、eventfd、mutex、condvar，全是这条链上不同位置的工程答案。
> **读法**：§30.2 的全景表先建立「谁搬数据、搬几次」的坐标系；§30.3 与 §30.4 是全篇两个支点——一个讲内核替你搬的代价，一个讲不搬之后你必须自己补的同步；§30.6 的 futex 状态机是理解「锁为什么不慢」的唯一入口，建议对着 §30.9 的 0 次 futex 实测读一遍；§30.7 的判据表与 §30.11 的坑表可以直接贴进评审清单。赶时间只读 §30.2、§30.6、§30.10、§30.12。
> **不在本篇**：进程地址空间、`fork` 与 COW 的完整机制 → `base/23`；线程与 `clone` 的共享参数 → `base/24`；页表、缺页与 `mmap` 的虚拟内存细节 → `base/26`、`base/27`；缓存行、伪共享与内存屏障 → `base/13`；`epoll`/`io_uring` 如何等 fd → `base/31`；Go channel、`sync.Mutex` 的运行时实现 → [go/20](../go/20-channel-internals.md)、[go/22](../go/22-sync-primitives.md)。
---
## 30.1 引子：一条管道卡在 30 万条/秒

2026-03，一个指标采集 agent 要做「本机聚合 → 旁路压缩」：采集线程把每条 1 KB 的样本写进管道，压缩进程从另一头读。上线后单机卡在 **30 万条/秒**，约 293 MiB/s；CPU 的 `sys` 时间占了大头，用户态反而不忙。

第一反应是「压缩太慢」。但把压缩进程换成一个只 `read` 不干活的空循环，吞吐几乎没变。真正的原因在写入侧：**一条样本一次 `write`**，30 万条/秒就是 30 万次系统调用/秒。后来改成每 64 条拼成一次 64 KB 写，调用次数降到 4,687 次/秒，同一台机器上的管道吞吐从 400 MiB/s 量级抬到 1,094 MiB/s（§30.9 的真实测量）。业务代码一行没改，只是**换了一个写粒度**。

同一周还有第二个反直觉现象。有人把一把 `pthread_mutex_t` 换成自旋锁，理由是「锁会进内核，自旋不会」。实测无竞争时，`pthread_mutex` 一次加解锁约 **27.7 ns**，自旋锁约 **12.8 ns**——mutex 确实慢一点，但两者是同一量级；而本机一次空系统调用（`getpid`）要 **235 ns** 以上。**27.7 ns 根本不可能是「每次加锁都进内核」的价钱。** 用 `ptrace` 数 syscall 得到的结果更直接：1,000 万次无竞争加解锁，`futex` 调用 **0 次**。

这两件事指向同一个底层事实，也是本篇的唯一真问题：

> **进程之间不共享内存，共享的只是同一段被映射进来的物理页；锁在无竞争时不进内核，靠的是 futex「先自旋、打不过再找内核」。**

把这句话拆成可验证的三段，就是本篇的全部内容：

1. **通信的两条路**：要么让内核搬（管道、消息队列、socketpair，每次两趟拷贝 + 两次系统调用），要么把同一段物理页映射进两个地址空间（共享内存，零拷贝，但同步责任全归你）。
2. **同步的两种态**：无竞争时只在用户态做一次原子读改写；有竞争时才用 `FUTEX_WAIT` 睡、用 `FUTEX_WAKE` 醒。内核不提供「锁」，只提供「等/唤醒一个 32 位整数」。
3. **失效的四个口**：管道死锁、`SIGPIPE` 默认杀进程、共享内存无生命周期、信号会合并。每一个都能从上面的物理事实推出来，不需要背。

---

## 30.2 IPC 全景：十种机制，先看它们在哪儿花钱

「进程间通信」不是一个东西，是一张按「谁搬数据、搬几趟、有没有边界」排列的表。先建立坐标系，后面每一节都只是其中一格的特写。

| 机制 | 方向 | 跨机 | 数据拷贝次数 | 默认阻塞 | 消息边界 | 典型用途 |
| --- | --- | --- | --- | --- | --- | --- |
| 管道 `pipe` | 单向 | 否 | 2（用户→内核页，内核页→用户） | 是（满/空即睡） | 无，字节流 | shell 管道、父子进程、日志旁路 |
| 命名管道 `FIFO` | 单向 | 否 | 2 | 是 | 无 | 无亲缘关系进程的字节流 |
| 消息队列 `mq_*` | 双向 | 否 | 2 | 是（可 `O_NONBLOCK`） | 有，按消息 | 带优先级的小消息、解耦 |
| 共享内存 `shm` | 双向随机访问 | 否 | 0（映射后直接读写） | 否（缺页可能睡） | 无，自己定协议 | 大块数据、零拷贝、共享计数器 |
| 信号 `signal` | 单向通知 | 否 | 0（只传编号） | 异步打断 | 无（标准信号不带数据） | 终止、`SIGCHLD`、超时提醒 |
| `eventfd` | 单向计数 | 否 | 8 B 值一次读写 | 是（计数为 0） | 无，就是一个 64 位计数器 | 唤醒事件循环、信号→poll 桥 |
| `signalfd` | 单向 | 否 | 1（读 `siginfo`） | 是 | 每条信号一条记录 | 把异步信号变成可 `poll` 的 fd |
| `timerfd` | 单向 | 否 | 0（读到期次数） | 是（未到期） | 每次读一个计数 | 定时器接入事件循环 |
| `socketpair` | 双向 | 否 | 2（经 socket 缓冲区） | 是 | `SOCK_STREAM` 无 / `SOCK_DGRAM` 有 | 父子全双工、传递 fd |
| Unix domain socket | 双向 | 否 | 2 | 是 | 流/数据报/`SEQPACKET` | 本机服务、`SCM_RIGHTS` 传 fd |

读这张表只需要抓住三列：

- **拷贝次数**决定「大块数据」该走哪条路。管道和 socket 每次都要把数据从用户缓冲区抄进内核缓冲区，再抄到对端；共享内存只在 `mmap` 时建立页表映射，之后读写同一组物理页，拷贝次数是 0。主线③「每层抽象的单价 = 跨界次数 × 单次成本」在这里第一次变成一张可以直接数的表。
- **阻塞语义**决定「谁来等」。管道写满会睡、读空会睡；共享内存没有 `read`/`write`，等不等由你的同步原语决定；信号是异步打断，不参与「等」。
- **消息边界**决定「一次读能不能读到半条消息」。管道是字节流，`read` 可能只拿到半个结构体；消息队列和 `SOCK_SEQPACKET` 保留边界；共享内存连「流」都不是，边界和版本号都得自己写。

表里所有机制都只在本机内核里成立：Unix domain socket 不出协议栈、不走网卡，它的优势是省掉 TCP/IP 头、校验和与网卡队列，不是「socket 这个 API 更快」。真正的跨机通信从 `base/31` 之后的 socket 开始。

---

## 30.3 管道：64 KB 环形缓冲与 `PIPE_BUF` 的原子边界

### 内核里的管道长什么样

`pipe()` 返回两个 fd，内核里只有**一个** `pipe_inode_info`，外加两个 `struct file`：一个挂 `pipe_read` 的 `file_operations`，一个挂 `pipe_write`。两个 fd 的 `file->private_data` 指向同一个 pipe 对象；`fork` 只是把这两个 fd 的引用计数加一，不会复制缓冲区。

```
        用户 A                         内核                         用户 B
   write(fd_w, buf, n)  ──►  ┌──────────────────────────┐
                              │ pipe_inode_info           │
                              │  bufs[]：16 个 pipe_buffer│
                              │  head/tail：环形下标       │
                              │  rd_wait / wr_wait 等待队列│
                              └──────────┬───────────────┘
                                         │  每个 buffer 指向一个 4 KB 页
   read(fd_r, buf, n)   ◄──   copy_page_to_iter()
```

默认容量是 **16 个页 × 4 KB = 65,536 B**。本机 `fcntl(fd, F_GETPIPE_SZ)` 与 `/proc/sys/fs/pipe-max-size` 的实测：

```
F_GETPIPE_SZ = 65536
/proc/sys/fs/pipe-max-size = 1048576
```

上限 1 MiB，可以用 `F_SETPIPE_SZ` 往上调（超过上限会被拒绝），但**默认就是 64 KB**。这个数字决定了两件事：一次写超过 64 KB 必然要分段；写满 64 KB 后写者必须睡。

### 写满会阻塞、读空会阻塞

`pipe_write` 在缓冲区没有空槽时，会释放 pipe 互斥锁、挂到 `wr_wait` 上睡；`pipe_read` 在缓冲区为空时挂到 `rd_wait` 上睡。对端一读/一写，就 `wake_up_interruptible` 唤醒。用非阻塞 fd 能把「睡」换成 `EAGAIN`：

```c
int p[2]; pipe(p);
fcntl(p[1], F_SETFL, O_NONBLOCK);
char *b = malloc(1 << 20); memset(b, 'x', 1 << 20);
ssize_t k = write(p[1], b, 1 << 20);   /* 只收下容量那么多 */
```

真实输出：

```
F_GETPIPE_SZ = 65536
O_NONBLOCK write(1048576) = 65536 errno=none
second write = -1 errno=Resource temporarily unavailable
```

第一次写返回 **65,536**，不是 1,048,576：内核只抄得下环里的部分；第二次直接 `EAGAIN`。阻塞模式下，同样的写会**睡到读者腾出空间**。反过来，读空管道且所有写端还开着，`read` 会睡；只有**所有写端 fd 都关闭**时才返回 0（EOF）。`fork` 后子进程那份写端没关，父进程就永远等不到 EOF——这是 §30.11 第一条死锁的常见形态。

### `PIPE_BUF = 4096`：一次 `write` 什么时候是原子的

POSIX 只保证一件事：**写入长度 ≤ `PIPE_BUF` 时，这次写是原子的**——要么一个字节都不写，要么全部写进去，不会和另一个写者的数据交错。Linux 的 `PIPE_BUF` 是 4096（等于页大小）。

超过 4096 就不保证。机制在 `pipe_write` 的循环里：它一次能抄多少抄多少；环满了就释放互斥锁去睡，等读者腾出空间再继续。**在它睡着的那段时间，另一个写者可以进来插一段**。所以「1 MB 一次写」在读者看来可能被切成多段，段与段之间夹着别人的数据。

这不是理论。两个写者各写 40 个 1 MiB 块，读者按 1 MiB 对齐读，检查每块是否只含一个写者的字节：

```
block_size=4096    blocks=4000  mixed_blocks=0    (0.0%)
block_size=1048576 blocks=80   mixed_blocks=71   (88.8%)
```

4 KB 一块时 **4,000 块零交错**；1 MiB 一块时 **80 块里 71 块被掺了别人的数据**。中间还测过 64 KB 一块：600 块零交错，因为 64 KB 正好等于管道容量，写者一次就能把整块塞进空管道、不需要中途睡。**交错的条件不是「大于 4096」本身，而是「写的过程中被迫让出过管道」**；`PIPE_BUF` 是 POSIX 给出的保守安全线。

工程结论两条：**要原子，就把一次 `write` 控制在 4096 B 以内**（或自己加长度前缀 + 应用层重组）；**要大吞吐，就批量写，但别假设对端一次 `read` 拿得到整批**——`read` 只保证「至少 1 字节、至多你给的缓冲大小」，所有按消息边界解析管道的代码都必须自己处理半包。

### 为什么批量写能快 2.7 倍

同一条管道、同样的 1 GiB 数据，只改每次写的长度：

| 每次写 | 总写次数 | 耗时 | 吞吐 | 每次写平均 |
| --- | --- | --- | --- | --- |
| 1 KB | 1,048,576 | 2.559 s | 400.1 MiB/s | 2.44 µs |
| 64 KB | 16,384 | 0.936 s | 1,094.5 MiB/s | 57.1 µs |

写调用少了 **64 倍**，吞吐只涨 **2.74 倍**——因为 64 KB 那次每次要抄 64 KB，单次成本从「系统调用为主」变成「拷贝为主」。这笔账可以拆开：1 KB 写每次 2.44 µs，其中一次空 `write` 到 `/dev/null` 的本机成本约 0.28–0.54 µs，剩下的是两次拷贝、唤醒读者、以及管道锁；64 KB 写每次 57.1 µs，摊到每字节反而更低。**批量化的收益来自摊薄固定成本，不来自「内核更喜欢大包」这种拟人化说法。**

---

## 30.4 共享内存：两个地址，一页物理内存

### `shm_open` + `mmap(MAP_SHARED)` 到底做了什么

`shm_open("/foo", O_CREAT|O_RDWR, 0600)` 在 tmpfs（通常是 `/dev/shm`）里创建一个文件；`ftruncate` 定大小；`mmap(..., MAP_SHARED, fd, 0)` 把这个文件的页缓存页映射进当前进程的页表。第二个进程 `shm_open` 同名文件再 `mmap`，内核把**同一批物理页**映射进它的页表。

关键点是：**共享的是物理页，不是虚拟地址**。两个进程的虚拟地址可以完全不同，页表项指向同一个 `struct page`。本机两个独立进程（不是 `fork`，各自 `exec`）映射同一个 `/dev/shm` 对象：

```
creator pid=3345134 addr=0x7fac219fa000 val=42
opener  pid=3345148 addr=0x7f59ff4b9000 val=42
```

两个地址相差约 329 GiB，值都是 42。**指针不能跨进程传**——你传过去的 `0x7fac...` 在对方地址空间里什么都不是。共享结构里只能放偏移量（相对段首的 `size_t`），不能放绝对指针；这是共享内存协议设计的第一条纪律。

`MAP_SHARED` 与 `MAP_PRIVATE` 的差别也在这里：`MAP_PRIVATE` 写时触发 COW，给写者一份私有副本，别人看不见；`MAP_SHARED` 直接改页缓存页，对所有映射者可见。`fork` 之后子进程继承的 `MAP_SHARED` 映射仍然指向同一批页，所以父子进程用共享内存通信时，连 `shm_open` 都不需要。

### 零拷贝的代价：同步责任全部转移给你

管道替你做了两件事：搬数据、在满/空时阻塞。共享内存一件都不做。它只给你一块可读写的内存，**没有原子性、没有顺序、没有互斥、没有生命周期管理**。

最经典的错误是把 `counter++` 当成「一个操作」：

```c
struct shared { volatile long plain; _Atomic long atom; };
/* 两个进程各自执行 N 次 */
for (long i = 0; i < N; i++) {
    s->plain++;                                          /* load → add → store */
    atomic_fetch_add_explicit(&s->atom, 1, memory_order_relaxed);
}
```

`volatile` 只保证「每次都真的去内存读/写」，不保证「读和写之间没人插进来」。x86 上它可能编译成：

```asm
mov    (%rbx),%rax      # 读
add    $0x1,%rax        # 改
mov    %rax,(%rbx)      # 写
```

三条指令之间都可以被另一个核插进来。两个进程各加 2,000 万次，预期 4,000 万：

```
mode=plain n=20000000 expected=40000000 got=29277287 lost=10722713 (26.81%)
mode=atomic n=20000000 expected=40000000 got=40000000 lost=0 (0.00%)
```

一次真实运行丢了 **1,072 万次更新（26.81%）**；换成 `_Atomic long` 的 `fetch_add` 后一次不丢。紧循环的竞争窗口只有一条 `load→store` 的距离（约 1 ns），所以**丢多少完全取决于调度器让两个进程重叠多久**：同一台机器上换一次运行，可能只丢 1,063 次，也可能一次不丢。把窗口人为拉长（在 load 与 store 之间插一段 `pause` 忙等），结果就稳定在 50% 左右：

```
mode=plain_wide n=500000 expected=1000000 got=500000 lost=500000 (50.00%) time=6.095 s
mode=atomic     n=500000 expected=1000000 got=1000000 lost=0 (0.00%) time=0.020 s
```

这段 `plain_wide` 的时间不能拿来和 `atomic` 比：6 秒里绝大部分是故意插进去的忙等，不是锁的成本。它只证明一件事——**窗口一拉长，丢失就从「偶发」变成「必然」**。共享内存上的非原子读改写不是「概率 bug」，是「窗口大小的函数」。

### 共享内存必须配同步：三条路

1. **进程共享的 pthread 锁**：`pthread_mutexattr_setpshared(&a, PTHREAD_PROCESS_SHARED)`，把 mutex 放进共享段；适合临界区较长、有竞争的场景，代价是慢路径要走 futex。
2. **原子指令**：`_Atomic` / `__atomic_fetch_add`，适合计数器、标志位、序号；无竞争时就是一条 `lock` 前缀指令（§30.9 实测约 11.5 ns），比进内核便宜两个数量级。
3. **无锁数据结构**：环形缓冲 + 原子 head/tail，单生产者单消费者可以两端都不加锁；多生产者要处理 CAS 重试与 ABA。内存序与伪共享账在 `base/13`、`base/19`。

还有一条容易被忽略：**共享内存没有「淘汰」**。管道满了会反压；共享内存写满了就是越界（或你自己定的环形缓冲覆盖旧数据）。没有背压、没有 GC、没有引用计数，槽何时可复用、旧数据何时过期，全部要协议回答。`/dev/shm` 里的对象在最后的 `shm_unlink` + 所有映射关闭前一直占内存；进程崩了不会自动清，名字留在 `/dev/shm` 里——这就是 §30.11 的「shm 残留」。`msync` 只对文件-backed 的持久化有意义，tmpfs 上的共享内存重启即失；要跨重启得自己写文件 + `fsync`（`base/29`）。

---

## 30.5 信号：异步通知与「会合并的 SIGCHLD」

### 信号是什么，不是什么

信号是内核向进程投递的一个**整数通知**。它不携带用户数据（实时信号可以带 `siginfo` 和少量字节），不保证排队（标准信号），也不保证「你处理的时候世界没变」。它唯一的语义是：**在某个不确定的时刻，打断当前指令流，跳到一个你注册的函数**。

这带来三个必须分开看的性质：

| 性质 | 含义 | 工程后果 |
| --- | --- | --- |
| 异步 |  handler 在任何两条指令之间运行 | 不能假设共享数据处于一致状态；必须用 `volatile sig_atomic_t` 或原子量 |
| 不可靠（标准信号） | 同一种标准信号在 pending 位图里只占 1 bit，多次到达合并成一次 | `SIGCHLD` 丢、`SIGIO` 丢、`SIGALRM` 丢 |
| 可屏蔽 | `sigprocmask` / `pthread_sigmask` 可以挡住投递 | 挡住期间信号 pending，解除后最多投递一次（标准信号） |

实时信号（`SIGRTMIN`..`SIGRTMAX`）是另一套：带 `siginfo`、按值排队、不会合并。但通知型场景大多用标准信号，所以合并是默认配置，不是边角情况。

### `signal` vs `sigaction`

`signal()` 在 glibc 里是 BSD 语义：handler 执行期间自动屏蔽本信号、被中断的系统调用自动重启（相当于 `SA_RESTART`）、handler 不会重置。但 POSIX 给 `signal` 留了余地，不同 Unix 不一样；**要可移植、要精确控制，就用 `sigaction`**：

```c
struct sigaction sa = {0};
sa.sa_handler = on_sigchld;
sigemptyset(&sa.sa_mask);          /* handler 执行期间额外屏蔽哪些信号 */
sa.sa_flags = SA_RESTART | SA_NOCLDSTOP;
sigaction(SIGCHLD, &sa, NULL);
```

四个 flag 值得记住：`SA_RESTART` 让被该信号打断的慢系统调用自动重启（`read`/`write` 阻塞在管道/socket 上最常见），没有它就返回 `-1`/`EINTR`；但 `nanosleep`、`poll` 不会被重启，仍要自己处理 `EINTR`。`SA_NOCLDSTOP` 让子进程暂停/继续不产生 `SIGCHLD`；`SA_SIGINFO` 把 handler 签名换成 `void (*)(int, siginfo_t *, void *)`，能拿发送者 pid 与 `si_code`；`SA_NOCLDWAIT` 让内核自动回收子进程，等价于 `SIGCHLD` 设 `SIG_IGN`。

### 处理函数里只能调 async-signal-safe 函数

这是最容易被违反的一条。handler 运行时，主流程可能正停在 `malloc` 中间、正持有一把内部锁、`stdio` 缓冲区正被改写。handler 里再调 `printf` / `malloc` / `free` / `pthread_mutex_lock`，就是**在别人的临界区里重入同一把锁**，轻则死锁，重则堆结构损坏。

POSIX 的 async-signal-safe 表里，常用的是 `read`、`write`、`open`、`close`、`_exit`、`kill`、`waitpid`、`sigaction`、`sigprocmask`、`sem_post`、部分 `fcntl`；`printf`、`malloc`、`free`、`syslog`、`pthread_*`（除少数）都不在表里。

信号 handler 的黄金模板是「**只做一件事：把事件写到某个 fd**」：

```c
static int sigfd;                        /* 全局，handler 只写 8 字节 */
static void on_term(int sig) {
    uint64_t one = 1;
    int saved = errno;
    write(sigfd, &one, sizeof one);      /* async-signal-safe */
    errno = saved;
}
```

主循环用 `poll`/`epoll` 等这个 fd（`eventfd` 或 self-pipe），所有复杂逻辑都在正常上下文里做。`signalfd` 是同一思路的内核版：把信号阻塞掉，创建 `signalfd`，然后像读文件一样读 `siginfo`。区别是 `signalfd` 不需要 handler，也不会在任意指令处打断你。

### `SIGCHLD` 丢失导致僵尸：一个可复现的账

标准信号会合并，所以「三个子进程退出」不保证「handler 运行三次」。下面这段程序先阻塞 `SIGCHLD`，`fork` 三个立刻退出的子进程，再解除阻塞；handler 里只调一次 `waitpid(WNOHANG)`：

```c
static volatile sig_atomic_t hits, reaped;
static int use_while;
static void on_chld(int sig) {
    (void)sig; hits++;
    if (use_while) { while (waitpid(-1, NULL, WNOHANG) > 0) reaped++; }
    else           { if (waitpid(-1, NULL, WNOHANG) > 0) reaped++; }
}
/* main：sigaction(SIGCHLD, ...); 阻塞 SIGCHLD; fork 3 个; 等 200ms; 解除阻塞 */
```

真实输出：

```
mode=if    handler_hits=1 reaped_in_handler=1 reaped_after=2 zombies_left=2
mode=while handler_hits=1 reaped_in_handler=3 reaped_after=0 zombies_left=0
```

**三个子进程，handler 只跑了 1 次。** 三个 `SIGCHLD` 在 pending 位图里只占同一个 bit，解除阻塞后只投递一次。`if` 版本收掉 1 个，剩下 2 个变成僵尸（`Z` 状态，`task_struct` 还在，占 pid 和内核栈）；`while` 版本一次收掉 3 个。

正确写法只有一条：**handler 里循环 `waitpid(-1, ..., WNOHANG)` 直到返回 0 或 `-1/ECHILD`**，不要假设「一次信号对应一个孩子」。不想收尸就用 `SA_NOCLDWAIT`/`SIG_IGN` 让内核自动回收；想同步处理就用 `signalfd`/`sigtimedwait` 把信号变成可读事件（僵尸生命周期在 `base/23`）。

---

## 30.6 futex：内核只借你一张等待队列

### 内核提供的不是锁，是「等一个 32 位整数」

`futex` 的系统调用原型（简化）：

```c
long futex(uint32_t *uaddr, int op, uint32_t val,
           const struct timespec *timeout, uint32_t *uaddr2, uint32_t val3);
```

它只做两件事：

- `FUTEX_WAIT`：**如果 `*uaddr == val`，就把当前线程挂到这个地址的等待队列上睡；否则立刻返回 `EAGAIN`。**
- `FUTEX_WAKE`：唤醒这个地址等待队列上的至多 `val` 个线程。

内核里没有「锁对象」，没有「持有者」，没有「重入计数」。有的只是一张哈希表：键是 `(mm, 虚拟地址)`（私有 futex）或 `(文件 mapping, 偏移)`（共享 futex），值是一条等待队列。**锁的语义完全由用户态的那 32 位整数和一套约定实现。**

这个设计的关键在 `FUTEX_WAIT` 的「比较」：它不是「无条件睡」，而是「值还是我预期的那个才睡」。这堵住了经典的 lost wakeup：

```
线程 A：读 lock == 1（被占）
线程 B：unlock，lock = 0，FUTEX_WAKE（此时 A 还没睡，队列空，wake 白做）
线程 A：FUTEX_WAIT(lock, 1) —— 内核发现 *lock 已经是 0，不等于 1，返回 EAGAIN
线程 A：重新尝试 CAS，成功
```

如果 `FUTEX_WAIT` 是无条件睡，A 就会永远睡下去。**「带预期值的等待」是 futex 协议正确的核心**，也是所有基于 futex 的锁都必须遵守的握手顺序。

### 一把 futex mutex 的完整状态机

glibc 的 `pthread_mutex_t` 快路径大致是这样（`lock` 字：0 空闲、1 已锁无等待者、2 已锁有等待者）：

```c
static int futex_lock(int *f) {
    int c = 0;
    if (atomic_compare_exchange_strong(f, &c, 1)) return 0;   /* 快路径：1 条 CAS */
    do {
        if (c == 2 || atomic_exchange(f, 2) != 0)
            futex(f, FUTEX_WAIT_PRIVATE, 2, NULL, NULL, 0);   /* 值仍是 2 才睡 */
        c = 0;
    } while (!atomic_compare_exchange_strong(f, &c, 2));
    return 0;
}
static void futex_unlock(int *f) {
    if (atomic_exchange(f, 0) > 1)                            /* 原来有等待者 */
        futex(f, FUTEX_WAKE_PRIVATE, 1, NULL, NULL, 0);
}
```

逐行读：

1. **无竞争**：CAS 0→1 成功，返回；`unlock` 把 1 换回 0，旧值不大于 1，**两边都没有系统调用**。
2. **第一次竞争**：A 持有（字=1），B 的 CAS 失败、`c` 被写成 1；B 用 `exchange(f,2)` 把字改成 2（告诉 A「有人等」），旧值 1 非 0，于是 `FUTEX_WAIT(f,2)` 睡下。
3. **解锁**：A 的 `exchange(f,0)` 返回 2（大于 1），说明有等待者，于是 `FUTEX_WAKE(f,1)`；B 醒来重新 CAS，把 0 抢成 2，进入临界区。
4. **为什么 unlock 先 exchange 再判断**：先读再写的话，读写之间可能有人把字改成 2 并睡下，unlock 会漏掉 wake。`exchange` 把「读旧值」和「写 0」合成一个原子动作，旧值就是「写 0 之前有没有人挂上去」的准确快照。

`FUTEX_WAIT_PRIVATE` 的 `PRIVATE` 是给内核的优化提示：这个 futex 只在本进程的线程间用，键可以只按 `(mm, 地址)` 算，不必查共享 mapping。跨进程的 process-shared mutex 用非 private 版本，键按共享页/文件算。**用错 private/shared，轻则多一次哈希查找，重则两个进程等在两个不同的队列上，永远唤不醒。**

### 账：0 次 syscall vs 1 次 wait + 1 次 wake

用 `ptrace(PTRACE_GET_SYSCALL_INFO)` 数一个进程的全部 syscall 入口（本机没有 `strace`，这个计数器是自己写的；它只数入口，不数出口）：

| 场景 | 总 syscall 入口 | `futex` 调用 | 说明 |
| --- | --- | --- | --- |
| 1 线程，1,000 万次 lock/unlock | 29 | **0** | 全部走 CAS 快路径；29 次是启动、`mmap`、退出 |
| 2 线程，各 500 万次，临界区 1 条 `++` | 29 | **0** | 临界区太短，本轮采样中两个线程的持锁窗口大多错开 |
| 2 线程，各 200 次，临界区 `nanosleep(200 µs)` | 1,163 | **704** | 350 次 `FUTEX_WAIT` + 351 次 `FUTEX_WAKE` + 3 次线程退出相关 |

第三行是重点：**400 次加锁，350 次真的睡了**，几乎每次阻塞都对应一次 wait + 一次 wake。每次阻塞的代价是「两次系统调用 + 两次上下文切换」；无竞争时是「一条 CAS + 一条 store」。按本机空 syscall 0.24–0.54 µs、一次调度 1–5 µs 估算，阻塞路径比快路径贵 **两个数量级**。这就是为什么所有锁库都把「无竞争」当成一等公民，而不是「有锁就进内核」。

> 顺带解释 704 而不是 701：多出来的 3 次是 `pthread_join` / 线程退出时 glibc 内部用的 `FUTEX_WAIT_BITSET`（op=9），不是 mutex 本身。做账时要把「库的内部同步」和「你的锁」分开，否则会把启动成本算到锁头上。

### 为什么 `pthread_mutex` 无竞争时这么便宜

因为它的快路径根本不碰 futex。一次无竞争加解锁只有：

```
lock:    lock cmpxchg  [mutex], 0 → 1      ; 1 条带 lock 前缀的原子指令
unlock:  xchg          [mutex], 0          ; 1 条原子写（或普通 store + 屏障）
```

`lock cmpxchg` 在缓存行独占时约 10–20 ns（本机 atomic_fetch_add 11.5 ns，含循环开销），不需要陷入内核、调度器或等待队列。**「锁」这个词让人联想到系统调用，但无竞争的锁原语只是一条 CPU 指令。** 这也解释了 §30.1：mutex 27.7 ns、自旋锁 12.8 ns、原子加 11.5 ns 是同一量级；差别来自库函数、owner 字段和自适应自旋，不是「一个用户态、一个内核」。如果每次加锁都进内核，它应该是 235 ns 以上。

---

## 30.7 锁的家族：临界区长度决定你该睡还是该转

### 五种同步原语的分工

| 原语 | 等的方式 | 能否睡眠 | 读并发 | 典型临界区 | 关键约束 |
| --- | --- | --- | --- | --- | --- |
| mutex | 用户态 CAS 快路径，失败则 futex 睡（adaptive 类型会先自旋） | 是 | 否（排他） | 数百 ns 到数 ms | 不可递归（默认）；跨进程要 `PROCESS_SHARED` |
| spinlock | 原地 `pause` 轮询 | 否 | 否 | < 100 ns | 持锁期间不能睡、不能调可能阻塞的函数；内核里还要关抢占 |
| rwlock | 读共享、写排他 | 是 | 是 | 读多写少，临界区中等 | 写者可能饿死；读锁不是「无锁」 |
| seqlock | 读者重试，写者不等 | 否（读者不睡） | 是（乐观读） | 很小的小对象 | 数据必须能一次性拷出；读期间不能解引用可能失效的指针 |
| RCU | 读者标记后直接读，写者等宽限期 | 否（读者） | 是（无锁） | 读极多写极少 | 写者延迟释放；读者临界区不能睡 |

### 判据：临界区长度 vs 等待成本

一次 futex 阻塞路径 = 2 次 syscall + 2 次调度，量级 **2–6 µs**；一次 CAS 失败重试 = 几十 ns。所以「该转还是该睡」的分界线在**「预期等待时间是否超过一次阻塞的成本」**，而不是「锁的名字好不好听」。

| 临界区/预期等待 | 首选 | 理由 |
| --- | --- | --- |
| < 50 ns（单写者计数器、序号） | `_Atomic` / `atomic_fetch_add` | 一条 `lock` 指令，无锁对象 |
| 50–200 ns，竞争者少 | 自旋锁（用户态带 `pause`，内核态 `spin_lock`） | 省掉两次 syscall；但持锁不能睡 |
| 200 ns–5 µs | 自适应 mutex（glibc 默认会先自旋） | 短等待用自旋吃掉，长等待才睡 |
| > 5 µs，或临界区里可能阻塞 | mutex / 条件变量 | 自旋会白烧 CPU，睡让出核 |
| 读远多于写，临界区小 | seqlock | 读者零原子、零等待；写者只递增序号 |
| 读极多写极少，临界区可复制 | RCU（内核）/ `liburcu` | 读者无锁；代价是写者等宽限期、内存延迟释放 |
| 跨进程 | 放在共享段里的 `PTHREAD_PROCESS_SHARED` mutex / 信号量 | 进程间没有「关抢占」这回事，自旋锁极易死锁 |

这张表里最容易被误用的是自旋锁。用户态自旋锁有两个硬约束：

1. **持锁线程被抢占，其他线程会在核上空转到时间片用完**。单核上更糟：持锁者被换下，自旋者永远等不到，直到调度器把持锁者换回来——这不是死锁，但可能是几十毫秒的停顿。内核的 `spin_lock` 因此要关抢占（`preempt_disable`），`spin_lock_irqsave` 还要关中断，防止中断 handler 在同一个核上抢同一把锁。
2. **临界区里不能调用可能睡眠的函数**（`read`、`malloc`、`mutex_lock`、`fsync`）。一旦睡了，持锁者不跑，其他自旋者把 CPU 烧光。

用户态没有「关抢占」的权限，所以用户态自旋锁只在「临界区极短 + 竞争极少 + 核数足够」时成立；一旦不确定，mutex 是更稳的默认值。**这也是「无锁一定快」的反例：无锁数据结构在竞争下要处理 CAS 失败、内存回收（ABA、hazard pointer），复杂度换来的可能只是把 futex 的等待换成了自旋的空转。**

---

## 30.8 条件变量与虚假唤醒：为什么必须是 `while`

### `pthread_cond_wait` 到底做了什么

`pthread_cond_wait(&cond, &mutex)` 的语义是三步，且第一步和第二步是一个**原子**动作：

1. 释放 `mutex`；
2. 把当前线程挂到 `cond` 的等待队列上睡；
3. 被唤醒后，重新获取 `mutex`，再返回。

「释放锁 + 睡」必须原子，否则会丢唤醒：如果先释放锁、还没睡，另一个线程拿到锁、改条件、`signal`，此时等待队列还是空的，信号白丢，等待者永远睡。glibc 用一组内部计数器与 futex 字来保证这个顺序。

### 为什么判断条件必须用 `while`

教科书的标准写法：

```c
pthread_mutex_lock(&m);
while (!ready) {                 /* 必须是 while，不是 if */
    pthread_cond_wait(&c, &m);
}
/* 到这里 ready 为真，且我们持有 m */
take_work();
pthread_mutex_unlock(&m);
```

三个独立的原因，任何一个都足以让 `if` 出错：

1. **虚假唤醒（spurious wakeup）**：POSIX 明确允许 `pthread_cond_wait` 在没有 `signal` 的情况下返回。实现可能因为信号、内部竞态、 futex 的假唤醒而返回。`if` 会直接往下走，条件其实还是假。
2. **条件被别的线程先消费**：`pthread_cond_signal` 只唤醒至少一个等待者；你被唤醒、抢到锁时，条件可能已经被先拿到锁的线程消费了（`ready` 又变回假），必须回去睡。
3. **条件在「被唤醒」和「重新拿到锁」之间变了**：`cond_wait` 返回前要重新获取 mutex，这中间可能有另一个线程拿到锁、改条件、再释放。你拿到锁时看到的世界，不一定是 signal 那一刻的世界。

`while` 的作用不是「多检查一次」，而是**把「被唤醒」当成「可能可以继续」的提示，而不是「条件已成立」的保证**。条件是否成立，永远以持有 mutex 时重新读到的谓词为准。

### 丢失唤醒：`signal` 发早了也会丢

条件变量本身不保存「信号」。如果没有任何线程在等，`pthread_cond_signal` 什么也不做。所以「先检查条件、再等」必须放在同一把 mutex 里：

```c
/* 错：检查与等待之间没有保护 */
if (!ready) pthread_cond_wait(&c, &m);   /* 检查时没持锁，或先解锁再等 */

/* 对：检查与等待都在锁内，生产者改 ready 也必须持锁 */
pthread_mutex_lock(&m);
while (!ready) pthread_cond_wait(&c, &m);
pthread_mutex_unlock(&m);
```

生产者一侧同样要在持锁时改 `ready`，再 `signal`；否则「改值」和「检查」之间没有 happens-before，等待者可能看到旧值。`signal` 在锁内还是锁外调用都可以（锁内调用会让被唤醒者立刻又阻塞在锁上，通常性能差一点，但语义正确）。

### `eventfd`：把「事件」变成可 `poll` 的计数器

条件变量只能在同一进程的线程间用（跨进程要 `PTHREAD_PROCESS_SHARED` 的 condvar，且放共享段）。如果事件需要被 `epoll`/`poll` 等，`eventfd` 更合适：

```c
int efd = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
uint64_t one = 1;
write(efd, &one, 8);          /* 计数器 += 1，可在信号 handler 里调用 */
uint64_t v;
read(efd, &v, 8);             /* 读出当前计数并清零；EFD_SEMAPHORE 时每次减 1 */
struct pollfd p = { .fd = efd, .events = POLLIN };
poll(&p, 1, -1);              /* 计数 > 0 时 POLLIN */
```

四个要点：

- `eventfd` 的内核对象就是一个 64 位无符号计数器；`write` 加、`read` 取（默认取走全部并清零）。
- 它**可 poll**：计数从 0 变正时唤醒等待者，适合把「信号 handler / 中断回调 / 其他线程」的事件汇入一个事件循环。
- `write` 是 async-signal-safe 的，所以 handler 里写 `eventfd` 是安全的（写 self-pipe 也一样，但要多一个 fd 和一次 `read` 的字节流解析）。
- 它**不携带数据**，只表达「发生了几次」。要传数据，得另配一个环形缓冲，`eventfd` 只当门铃。

`signalfd` 是另一条路：把信号阻塞，创建 `signalfd`，然后 `read` 出 `struct signalfd_siginfo`。它保留 `siginfo`（发送者、信号值、`si_code`），不会在任意指令处打断你，但要求你先用 `sigprocmask` 把信号挡住，否则默认处置（终止/忽略）仍会发生。`timerfd` 同理：定时器到期变成「fd 可读」，读出一个到期次数。

---

## 30.9 实测：2 核机器上的四组账

**环境**：Linux 6.12，2 vCPU（AMD EPYC 7K62，QEMU 虚拟化），`/dev/shm` 可用；`gcc -O2`；需要绑核的实验用 `taskset -c 0,1`。宿主机有 steal，绝对时间会抖，所以每项取多次运行的**最小值**，并标明这是量级结论而不是承诺值。`strace`/`perf` 在本机不可用，syscall 计数用自写的 `ptrace(PTRACE_GET_SYSCALL_INFO)` 完成。

### ① 管道：1 KB/次 vs 64 KB/次

程序：`pipe()` + `fork()`，子进程循环 `read` 到 EOF，父进程按固定块长 `write` 1 GiB，`clock_gettime(CLOCK_MONOTONIC)` 计时。

```c
size_t chunk = atol(argv[1]), total = 1UL << 30;
int p[2]; pipe(p);
if (fork() == 0) { close(p[1]); while (read(p[0], buf, chunk) > 0) {} _exit(0); }
close(p[0]);
double t0 = now();
for (size_t sent = 0; sent < total; ) sent += write(p[1], buf, chunk);
close(p[1]); wait(NULL);
printf("%.1f MiB/s\n", total / 1048576.0 / (now() - t0));
```

真实输出：

```
pipe capacity = 65536 bytes
chunk=1024  total=1073741824 sent=1073741824 write_calls=1048576 time=2.559 s throughput=400.1 MiB/s calls/s=409696 child_ok=1
chunk=65536 total=1073741824 sent=1073741824 write_calls=16384   time=0.936 s throughput=1094.5 MiB/s calls/s=17512 child_ok=1
```

1 KB 每次平均 **2.44 µs**，64 KB 每次平均 **57.1 µs**；写调用少 64 倍，吞吐涨 2.74 倍。这不是「大包更快」的玄学：1 KB 时固定成本（syscall、锁、唤醒）占主导，64 KB 时数据拷贝占主导。**批量化的本质是把固定成本摊薄。**

### ② 共享内存：原子 vs 不加锁

两个进程映射同一个 `/dev/shm` 对象，各执行 2,000 万次 `plain++` 与 `atomic_fetch_add`：

```
mode=plain  n=20000000 expected=40000000 got=29277287 lost=10722713 (26.81%)
mode=atomic n=20000000 expected=40000000 got=40000000 lost=0 (0.00%)
```

`plain` 版本丢了 1,072 万次更新。换成「load 后插一段忙等、再 store」把竞争窗口拉长到微秒级，结果稳定在 50%：

```
mode=plain_wide n=500000 expected=1000000 got=500000 lost=500000 (50.00%) time=6.095 s
mode=atomic     n=500000 expected=1000000 got=1000000 lost=0 (0.00%) time=0.020 s
```

两个进程的虚拟地址（独立 `exec`，不是 `fork`）：

```
creator pid=3345134 addr=0x7fac219fa000 val=42
opener  pid=3345148 addr=0x7f59ff4b9000 val=42
```

**地址不同、值相同**——共享的是物理页，不是地址。

### ③ 无竞争 mutex：1,000 万次加解锁

```
pthread_mutex           threads=1 ops=10000000 time=0.2773 s  27.73 ns/op  36.06 Mops/s
spinlock(pause)         threads=1 ops=10000000 time=0.1282 s  12.82 ns/op  77.99 Mops/s
atomic_fetch_add        threads=1 ops=10000000 time=0.1147 s  11.47 ns/op  87.17 Mops/s
```

作为参照，本机一次空 syscall：

```
getpid: 235.1 ns/call
write(/dev/null, 1B): 280.7 ns/call
```

`pthread_mutex` 的 27.7 ns 只有一次 syscall 的 **12%**。它不可能是「每次加锁都进内核」；自写 ptrace 计数器也确认了 1,000 万次加解锁期间 `futex` 调用为 **0**。

### ④ 有竞争：2 线程与长临界区

2 线程各 500 万次，临界区只有一条 `++`（总操作数仍是 1,000 万）：

```
pthread_mutex    threads=2 ops=10000000 time=0.1614 s  16.14 ns/op  61.98 Mops/s
spinlock(pause)  threads=2 ops=10000000 time=0.1092 s  91.54 Mops/s
atomic_fetch_add threads=2 ops=10000000 time=0.0684 s 146.16 Mops/s
```

这里有个**度量陷阱**：`ops` 是两线程之和，`time` 是墙钟。2 线程时 16.14 ns/op 看似比 1 线程的 27.73 ns/op 还快，但每线程的实际速率是 `61.98 / 2 = 30.99 Mops/s`，与单线程 36.06 Mops/s 相近——**总墙钟被两个核分摊了，不代表单线程变快**。真正的加速比是 `61.98 / 36.06 = 1.72×`（2 核，临界区有缓存行争用，达不到 2×）。

把临界区拉长到 `nanosleep(200 µs)`，强制真正的等待：

```
nt=2 per=200 time=0.145 s ns/op=363592 cnt=400
syscall_entries=1163 futex=704 FUTEX_WAIT=350 FUTEX_WAKE=351
```

400 次加锁里 **350 次睡了**，对应 350 次 `FUTEX_WAIT` + 351 次 `FUTEX_WAKE`。而临界区只有一条 `++` 时，同样的 2 线程跑了 1,000 万次加锁，`futex` 仍然是 **0**——临界区太短，两个线程的持锁窗口大多错开，没走到「打不过」那一步。这也说明 futex 调用数不是「线程数」的函数，而是「真正睡了几次」的函数。

---

## 30.10 三列账：承诺、代价、崩点

| 机制 | 承诺了什么 | 为此付了什么 | 从哪里开始崩 |
| --- | --- | --- | --- |
| 管道 / FIFO | 字节流、内核替你缓冲、满/空自动阻塞 | 每次 2 次拷贝 + 2 次 syscall；容量默认 64 KB；只在本机 | 两端都先写后读 → 互相等满；写端没关 → 读端等不到 EOF；无读者 → `SIGPIPE` |
| 共享内存 | 零拷贝、随机访问、大块数据 | 同步全自己做；无边界、无生命周期、无背压；缓存行争用 | 非原子 `++` 丢更新；崩溃后段/锁残留；结构体 ABI/指针跨进程失效；无淘汰导致覆盖或越界 |
| 信号 | 异步通知、能打断阻塞、实现简单 | 标准信号会合并；handler 只能调 async-signal-safe；`EINTR`/`SA_RESTART` 复杂 | `SIGCHLD` 丢 → 僵尸；handler 里 `printf`/`malloc` → 死锁；信号在错误线程投递 |
| futex + mutex | 无竞争零 syscall；有竞争才睡；跨进程可用 | 需要用户态协议（CAS + 状态字）；慢路径 2 次 syscall + 2 次调度；只有 32 位字 | 死锁（忘 unlock / 锁顺序相反）；优先级反转；private/shared 用错；等待队列键不匹配 |
| eventfd / signalfd / timerfd | 把事件变成可 `poll` 的 fd；能接进事件循环 | 只传计数或 `siginfo`，不传业务数据；多一个 fd；`eventfd` 计数会溢出 | 计数溢出 `EAGAIN`；`signalfd` 忘了先屏蔽信号；fd 泄漏 |

一句话收束：**内核替你搬数据，你付拷贝和 syscall；内核不搬数据，你付同步和生命周期。** 没有第三种选择。共享内存不是「更快的管道」，它是「把管道拆成两半，中间那段缓冲和同步交给你自己实现」。

---

## 30.11 踩坑清单：现象 → 根因 → 定位 → 修

| 现象 | 根因 | 怎么定位 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 两个进程互相 `write` 后都卡住 | 管道容量 64 KB，双方都写满后等对方读；或读端等 EOF 但写端没关 | `cat /proc/<pid>/stack` 看是否停在 `pipe_write`/`pipe_read`；`lsof` 看还有几个写端 fd | 固定「一方只读、一方只写」；或先 `poll` 再读写；或改用非阻塞 + 缓冲 | 画清数据流方向；`fork` 后立刻关掉不用的端；评审时检查 EOF 条件 |
| 进程莫名收到 `SIGPIPE` 退出 | 写一个没有读端的管道/socket，默认处置是终止进程 | `dmesg`/`wait` 状态是 signal 13；`strace` 看到 `write` 前被杀 | `signal(SIGPIPE, SIG_IGN)`，然后处理 `write` 的 `EPIPE`；socket 用 `MSG_NOSIGNAL` | 在进程启动时统一忽略 `SIGPIPE`；所有写路径检查 `EPIPE` |
| `/dev/shm` 越用越满，重启才恢复 | `shm_open` 创建的名字没有 `shm_unlink`；进程崩了对象还在 | `ls -l /dev/shm`；`df -h /dev/shm`；`fuser` 看谁还映射着 | 创建后立刻 `shm_unlink`（fd/mapping 仍有效）；或改用 `memfd_create` | 封装一个 `shm_create()`，内部保证 unlink；监控 `/dev/shm` 使用率 |
| `fork` 后子进程一碰锁就死 | 父进程是多线程，`fork` 时别的线程正持锁；子进程只复制了「锁被占」的状态，没有持锁线程 | `gdb` 看子进程卡在 `pthread_mutex_lock`；`/proc/<pid>/task` 只有一个线程 | 不要在多线程进程里 `fork` 后调 `malloc`/锁；用 `pthread_atfork`；或 `fork` 后立刻 `exec` | 多线程程序改用 `posix_spawn`；必须 fork 时只做 async-signal-safe 操作 |
| 条件变量偶尔永远等下去 | `while` 写成 `if`，虚假唤醒/信号被抢后直接往下走；或 `signal` 在无人等待时发出 | 在 `cond_wait` 前后打印谓词与线程 id；看是否少了「重新检查」 | 改成 `while (!pred) cond_wait(...)`；生产者持锁改谓词再 signal | Code review 禁止 `if (cond_wait)`；封装成 `wait_until(pred)` |
| 共享内存里的计数器偶尔少几次 | `volatile long++` 是 load/add/store，不是原子读改写 | 两个进程各加 N 次，比较总和；反汇编看是否有 `lock` 前缀 | 用 `_Atomic`/`__atomic_fetch_add`；或放进程共享 mutex | 共享段里的所有可变字段标注同步方式；CI 跑并发计数测试 |
| 跨进程 mutex 行为诡异/死锁 | mutex 放在私有内存，或没设 `PTHREAD_PROCESS_SHARED`；两个进程各锁各的副本 | 打印 mutex 地址，确认是否在同一共享段；检查 `pthread_mutexattr_getpshared` | `mmap(MAP_SHARED)` + `pthread_mutexattr_setpshared`；或改用原子/信号量 | 共享段结构体第一字段放 magic + version；启动时校验 |
| `SIGCHLD` 只处理了一个孩子 | 标准信号合并；handler 只 `waitpid` 一次 | `ps -o stat,pid,ppid` 看 `Z` 状态；数 handler 进入次数 | handler 里 `while (waitpid(-1, ..., WNOHANG) > 0)` | 统一用 `signalfd` 或 `SA_NOCLDWAIT`；禁止「一次信号一个孩子」的假设 |

这张表里最值得单独讲的是「管道死锁」和「`fork` 后锁状态复制」，因为它们都不是 API 用错，而是**物理事实的推论**：

- 管道死锁的根因是「容量有限 + 双方都等对方先动」。只要画得出「谁在等谁」，就能看出环在哪里；`poll` + 非阻塞只是把「睡」换成「可查询」，并没有消除环。
- `fork` 后锁状态复制的根因是「锁是由『持有者线程』和『状态字』共同定义的，而 `fork` 只复制状态字，不复制持有者」。子进程继承的是一把「永远有人持有、但那个人不存在」的锁。`pthread_atfork` 只能在 fork 前后抢/放锁，治标；真正的解法是 fork 后只 `exec`，或改用 `posix_spawn`。

---

## 30.12 一页速记

```
【一句话】
  进程不共享内存，只共享被映射的物理页；锁无竞争不进内核，靠 futex 先自旋后睡。

【两条通信路】
  内核搬：pipe/FIFO/mq/socketpair   → 2 次拷贝 + 2 次 syscall，内核给边界/阻塞
  映射共享：shm_open + mmap(MAP_SHARED) → 0 拷贝，同步/生命周期全自己做

【管道三个数】
  容量 65536 B（16 页）；PIPE_BUF 4096 B（≤它才原子）；上限 1 MiB（pipe-max-size）
  写满睡 wr_wait，读空睡 rd_wait；所有写端关闭才 EOF

【futex 三句话】
  内核只提供 FUTEX_WAIT(地址, 预期值) / FUTEX_WAKE(地址, 个数)
  无竞争：CAS 0→1 成功，unlock 1→0，0 次 syscall
  有竞争：状态字置 2，FUTEX_WAIT；unlock exchange 到 0 看到 2，FUTEX_WAKE

【实测四个数（本机 2 vCPU，取最小）】
  pipe 1 KB: 400 MiB/s, 2.44 µs/次；64 KB: 1094 MiB/s, 57.1 µs/次
  shm 非原子 ++: 丢 26.81%；_Atomic: 丢 0
  mutex 无竞争: 27.7 ns/op；空 syscall: 235 ns；无竞争 futex 调用: 0
  2 线程长临界区: 400 次加锁 → 350 WAIT + 351 WAKE

【三个坑的根】
  管道死锁   = 容量有限 + 双方都等对方先动
  SIGCHLD 丢 = 标准信号 pending 只有 1 bit，多次合并
  fork 死锁   = 锁 = 状态字 + 持有者，fork 只复制状态字
```

---

## 30.13 自检 3 题

**Q1.** 两个进程各往同一个管道写 1 MiB，读者按 1 MiB 读，88.8% 的块被掺了对方的字节；换成每次 4 KB 写，0 块被掺。请用 `PIPE_BUF` 和 `pipe_write` 的睡眠行为解释这个差异，并说明「每次写 64 KB」为什么也可能不掺。

<details><summary>答案</summary>

`PIPE_BUF = 4096` 是 POSIX 保证「单次写原子」的上限。Linux 的 `pipe_write` 在写入超过 `PIPE_BUF` 时，会循环把数据抄进环形缓冲；环满时它**释放 pipe 互斥锁**、挂到 `wr_wait` 上睡，等读者腾出空间再继续。就在它睡的这段时间，另一个写者可以拿到锁、往环里塞自己的数据，于是同一个逻辑写被切成多段，段间夹着别人的字节。1 MiB 远大于 64 KB 容量，写者必然中途睡，所以 80 块里 71 块被掺。

4 KB ≤ `PIPE_BUF`，内核在持有 pipe 锁的情况下一次性完成：空间够就全部写入，空间不够就让写者睡到够为止（非阻塞则返回 `EAGAIN`），不会写一半再让出，所以 4,000 块零交错。

64 KB 恰好等于默认管道容量：如果写时管道是空的，一次就能把整块塞进去，不需要中途睡，所以也可能零交错。但这是**容量恰好足够**的巧合，不是原子性保证；只要管道里已有数据、或容量被调小，64 KB 写同样会被切。**安全线是 4096，不是 64 KB。**

</details>

**Q2.** 1,000 万次无竞争 `pthread_mutex_lock/unlock` 只花了 0.277 s（27.7 ns/次），而本机一次空 `getpid` 要 235 ns。请说明 mutex 在这条路径上做了什么、为什么没有 `futex` 调用；再说明有竞争时账目会变成什么样。

<details><summary>答案</summary>

无竞争时走快路径：`pthread_mutex_lock` 对 mutex 字做一次 `atomic_compare_exchange_strong(&lock, 0, 1)`，成功就直接返回；`unlock` 做一次 `atomic_exchange(&lock, 0)`，发现旧值是 1（没有等待者），不调用 futex。全程只有一条带 `lock` 前缀的原子指令和一次原子写，不陷入内核。27.7 ns 约等于 55 个 2 GHz 周期，符合「缓存行独占时的 CAS + 函数调用开销」，远小于一次 syscall 的 235 ns。ptrace 计数也验证了 1,000 万次加解锁期间 `futex` 调用为 0。

有竞争时：CAS 失败的一侧把状态字改成 2（标记「有等待者」），然后 `FUTEX_WAIT(lock, 2)` 睡；持锁者 unlock 时 `exchange` 看到旧值 2，调用 `FUTEX_WAKE` 唤醒一个等待者。每个「阻塞 episode」对应 1 次 WAIT + 1 次 WAKE，外加两次上下文切换。实测 400 次加锁、临界区 200 µs 时，得到 350 次 WAIT + 351 次 WAKE，约 1.76 次 futex/加锁——这就是慢路径的价钱。若临界区很短（一条 `++`），自旋/交接在用户态完成，futex 仍可能是 0。

</details>

**Q3.** 三个子进程几乎同时退出，父进程阻塞 `SIGCHLD` 后再解除，handler 只运行了 1 次，`waitpid` 只收了一个孩子，剩下两个僵尸。请解释信号合并的机制，并给出两种正确修法。

<details><summary>答案</summary>

标准信号（`SIGCHLD` 是标准信号）在进程的 pending 集合里按**信号编号**占一个 bit，不按次数排队。三个孩子退出时，内核三次置位同一个 bit；父进程解除阻塞后，内核只投递一次，所以 handler 只进入一次。handler 里只调一次 `waitpid(WNOHANG)`，只回收一个；另外两个已经退出但没人 `wait`，`task_struct` 留在内核里，成为僵尸（`Z`）。

修法一：handler 里循环收尸，直到没有可收的：

```c
while (waitpid(-1, NULL, WNOHANG) > 0) { /* reap all */ }
```

返回 0 表示还有孩子活着但没退出；返回 -1/`ECHILD` 表示没有孩子了。这样一次 handler 能把当前所有已退出的孩子全收掉。

修法二：不要依赖信号计数。用 `signalfd` 把 `SIGCHLD` 变成可读 fd，事件循环里读一条处理一条；或者设 `SA_NOCLDWAIT` / `SIG_IGN`，让内核自动回收，根本不产生僵尸。若必须用 handler，也可以在 handler 里只 `write(eventfd)`，由主循环统一 `waitpid` 循环收尸。

</details>

---

## 30.14 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 缓存行、伪共享、内存屏障：共享内存上的计数器为什么会在缓存行上排队 | → `base/13` |
| 进程、`fork`/COW、僵尸与 `task_struct` 的生命周期 | → `base/23` |
| 线程与 `clone`：哪些资源被共享、哪些被复制 | → `base/24` |
| `epoll`/`io_uring`：`eventfd`、`signalfd`、`timerfd` 如何被等 | → `base/31` |
| 管道、socket 的零拷贝替代：`sendfile`、`splice`、`vmsplice` | → `base/37` |
| Go channel 的底层：`hchan`、`sudog` 与等待队列 | [go/20](../go/20-channel-internals.md) |
| Go `sync.Mutex`、`atomic` 与内存序的工程语义 | [go/22](../go/22-sync-primitives.md) |
| 全书总纲：四条主线与 56 篇清单 | [0-summary](0-summary.md) |
