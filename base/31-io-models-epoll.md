# 31. IO 模型全景：阻塞/非阻塞、select/poll/epoll、ET/LT 与 io_uring

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 操作系统：Linux 视角 ｜ **编号**：31 / 56
> **本篇定位**：Part III 的第十一篇。21 讲一次系统调用跨界要付多少钱，22–30 讲进程、内存、文件与 IPC 怎么被内核表示；本篇把「等」这件事单独拎出来：**一个线程凭什么能同时等一万个 fd**。全篇只有一条推论链——`fd 的阻塞属性` → `谁来等` → `等一个还是等一堆` → `怎么在不扫描的前提下知道谁就绪` → `就绪之后谁来拷数据` → `能不能连拷贝也不等`。select / poll / epoll / io_uring 的差别，全落在这条链的不同段上。
> **读法**：§31.3 的五模型表与 §31.5 的 eventpoll 图是全篇支点，建议先看这两处再回头读细节；§31.6 的 ET 复现与 §31.9 的五组实测都能两分钟内跑完，跑完再读后面的推论；§31.10–31.12 是三列账、踩坑表与一页速记，赶时间只读这三节。
> **不在本篇**：一次系统调用的进入/返回成本与 `syscall` 指令细节 → `base/21`；socket 收包的硬中断、软中断、NAPI 与缓冲区全路径 → `base/32`；零拷贝四实现与拷贝次数账 → `base/37`；epoll 与 io_uring 完整的 syscall 账（实例篇）→ `base/36`；Go runtime 的 netpoll 如何把 epoll 包成 goroutine → [go/19](../go/19-scheduler-runtime.md)；SSE / WebSocket 的长连接工程形态 → [go/39](../go/39-realtime-sse-websocket.md)。

---

## 31.1 引子：一万个连接，三种写法，三本不同的账

2026-03，一个消息推送服务要从 2,000 连接扩到 10,000 长连接。机器是 8 核 16 GB，业务是「每条连接平均每 100 ms 收一条 64 B 的小消息，服务端原样回显」。第一版按最自然的写法：`accept` 之后 `pthread_create`，每个线程阻塞在 `read` 上。

| 时间 | 事件 | 观测 |
| --- | --- | --- |
| 03-02 | 一线程一连接，2,000 连接 | p99 12 ms，`load average` 3.1，看起来还行 |
| 03-05 | 扩到 10,000 连接 | p99 380 ms，`load average` 42，`/proc/<pid>/status` 的 `VmSize` 78 GiB |
| 03-06 | 查线程数 | `/proc/<pid>/task` 下 10,003 项；每个线程 8 MiB 栈，但 RSS 只涨到约 210 MB |
| 03-07 | 改 select | 启动即失败：fd 超过 1023 后 `FD_SET` 越界，`select` 返回 `EINVAL` |
| 03-08 | 改单线程 epoll | p99 18 ms，RSS 24 MB，一个核跑 30% |

三行数字先把话说死：**一线程一连接在 1 万连接时并没有「内存爆掉」**——RSS 只有 210 MB。它爆的是另外三样：78 GiB 的虚拟地址空间、每线程 16 KiB 的内核栈（10,000 × 16 KiB ≈ 156 MiB）、以及一条长度一万的 runqueue。真正被 CPU 打满的是 select：它每问一次「谁就绪」都要把全部 fd 扫一遍。

把三种写法的固定成本与每次请求的等待成本摊开（实测环境见 §31.9：2 核、Linux 6.12.113、容器内）：

| 方案 | 1 万连接的固定成本 | 每次请求的等待成本 | 先撞上的墙 |
| --- | --- | --- | --- |
| 一线程一连接 | 78 GiB 虚拟地址空间（8 MiB/线程）；156 MiB 内核栈；约 1 万条可调度实体 | 2 次上下文切换（1,000 线程实测 2.25 次/请求） | `vm.max_map_count=65530`，每线程约 2 个 VMA → 约 3.2 万线程封顶；10 万连接才真的 OOM |
| select | 根本装不下：`FD_SETSIZE=1024` | 每次调用扫全部 fd（1,000 fd 实测 0.20–0.34 ms） | 1024 上限；每秒 3,000 次调用就打满 1 核 |
| epoll 单线程 | 每 fd 一个 epitem，约 160 B → 1.6 MB | 一次 `epoll_wait` 只取就绪项（10,000 fd、1 个就绪实测 0.2–0.5 µs） | 编程复杂度：ET 漏读、惊群、fd 生命周期 |

所以本篇的唯一真问题可以写成一句话：

> **所谓「异步」只是「谁来等」的问题；epoll 快不在读得更快，而在一次问一万个 fd 谁就绪，免掉一万次系统调用。**

这句话有两半，都要能推：

- 「谁来等」决定了线程会不会被挂起、要付几次上下文切换。阻塞 `read` 是**调用线程**等；epoll 是**一个线程替一万个 fd 等**；io_uring 是**内核替调用线程等，连拷贝也不占调用线程**。
- 「一次问一万个」不等于「一次读一万个」。epoll 没有让 `read` 变快：每条消息仍然要一次 `read`、一次 `write`。它省掉的是「为了知道谁有数据，而对一万个 fd 各问一次」的那一万次系统调用与一万次扫描。

§31.9 会把这两半都量出来：同一台机器上，1,000 连接的 epoll 回显是 55.7 µs/请求，一线程一连接是 118.3 µs/请求；而 10,000 个 fd 只有 1 个就绪时，`epoll_wait` 是 0.2–0.5 µs，`poll` 是 4.6 ms——差了四个数量级。

---

## 31.2 四个词的精确含义：阻塞/非阻塞是一维，同步/异步是另一维

先把两组词分开。它们不是同义词，也不能互相推导。

**阻塞 / 非阻塞描述「一次调用不能立刻完成时，调用线程怎么办」。** 它是 fd 上的一个状态位：`O_NONBLOCK`。设置方式只有 `fcntl(fd, F_SETFL, ...)` 或 `open(..., O_NONBLOCK)`、`accept4(..., SOCK_NONBLOCK)` 这类创建时指定。

- 阻塞 fd 上 `read` 没数据：线程进入 `TASK_INTERRUPTIBLE`，从 runqueue 摘下来，等 socket 的等待队列被唤醒；此时它不占 CPU，但占一个线程栈、一次上下文切换。
- 非阻塞 fd 上 `read` 没数据：立刻返回 `-1`，`errno = EAGAIN`（或 `EWOULDBLOCK`，Linux 上两者同值 11）。线程继续跑，代价是你得自己决定「过多久再问一次」。

有一个容易忽略的精度：`O_NONBLOCK` 是**打开文件描述**（`struct file`）的属性，不是 fd 编号的属性。`dup` 出来的两个 fd、`fork` 之后父子进程的同一条连接，共享同一个 `struct file`，所以在一个 fd 上设了非阻塞，另一个也会变成非阻塞。线上「我只把这个 fd 设成非阻塞，怎么另一个线程也收到 EAGAIN」的根因就在这里。

**同步 / 异步描述「数据从内核缓冲区搬到用户缓冲区这件事，由谁完成、调用何时算完成」。** POSIX 的定义很干脆：

- 同步 I/O：调用返回时，数据已经在你的缓冲区里（或者已经写进内核），期间调用线程被这次搬运绑住。
- 异步 I/O：调用只登记「我要读/写这一段」，立刻返回；真正的搬运由内核（或内核安排的工作者）完成，完成之后通过信号、回调或完成队列通知你。

把两维一交叉，四种组合都存在：

| | 同步（返回时数据已就位） | 异步（返回只是登记，完成另通知） |
| --- | --- | --- |
| **阻塞 fd** | 默认 `read`/`write`：睡到有数据并拷完 | 少见：glibc 的 POSIX AIO 对普通文件用线程池，工作线程阻塞在 `pread`，调用线程立刻返回 |
| **非阻塞 fd** | `read` + `O_NONBLOCK`：要么拷完返回，要么 `EAGAIN`；事件循环把它包成「就绪回调」 | io_uring：提交 SQE 后立刻返回，内核完成后写 CQE |

这张表要能推出三个常见误解：

1. **「非阻塞」不等于「异步」。** 非阻塞只是把「等」变成「问」；数据仍然由调用线程在 `read` 里拷。非阻塞 + 轮询是把等待成本换成了空转成本。
2. **「异步编程」不等于「异步 I/O」。** `async/await`、回调、Promise、Go 的 goroutine、Node 的 event loop，绝大多数在系统调用层是**非阻塞 fd + 就绪通知（epoll/kqueue）**。`await` 挂起的是用户态任务，`read` 还是由某个线程同步拷完。它们买到的是「少量线程扛大量并发」，不是「内核替你拷贝」。
3. **POSIX 的「异步 I/O」有明确所指。** `aio_read(3)` 是 POSIX AIO；Linux 上 glibc 对普通文件长期用线程池模拟（`io_setup` 的 native AIO 只对 `O_DIRECT` 友好，且不支持 socket），所以「异步」的等待者从调用线程换成了线程池里的工作者——**等待没有消失，只是换了人**。真正的内核异步 I/O 是 io_uring（§31.8）。

一个可以直接验的推论：**普通文件设 `O_NONBLOCK` 基本没用**。普通文件的「可读」由页缓存决定，缺页时该阻塞还是阻塞；想让它真异步，只能靠 io_uring 的 `IORING_OP_READ` 或 `O_DIRECT` + native AIO。很多「我把日志文件设成非阻塞了怎么还卡」的问题，根因是把 socket 的经验套到了文件上。

---

## 31.3 五种 IO 模型：把「两个阶段」拆开看，谁阻塞一目了然

任何一次网络读，都可以拆成两个阶段：

- **阶段①：等数据就绪。** 数据从网卡到内核 socket 接收缓冲区，或者发送缓冲区有空位。
- **阶段②：把数据从内核缓冲区拷到用户缓冲区。** 这一步是 `copy_to_user`（或 DMA / 零拷贝变体），发生在 `read` 返回之前。

五种模型的差别，就是这两个阶段里「谁在等、谁在拷」。Stevens 的五模型表之所以经典，是因为它把「异步」还原成了这两格的填法：

| 模型 | 阶段① 等数据 | 阶段② 拷数据 | 完成通知 | 典型接口 |
| --- | --- | --- | --- | --- |
| 阻塞 I/O | 调用线程睡在等待队列 | 调用线程（返回时已拷完） | 返回值 | 默认 `read`/`recv` |
| 非阻塞 I/O | 调用线程轮询，`EAGAIN` 立刻返回 | 调用线程 | 返回值 | `O_NONBLOCK` + `read` |
| I/O 多路复用 | 调用线程睡在 `select`/`poll`/`epoll_wait` | 调用线程，随后 `read` | 返回值（哪些 fd 就绪） | `select`/`poll`/`epoll` |
| 信号驱动 I/O | 调用线程继续跑；内核就绪时发 `SIGIO` | 调用线程，随后 `read` | 信号 | `O_ASYNC` + `F_SETOWN` |
| 异步 I/O | **内核**等 | **内核**拷 | 完成队列 / 信号 | io_uring、POSIX AIO |

逐行看代价：

- **阻塞**：最省心，一次 `read` 一次上下文切换（没数据时睡、数据到了醒）。1 万连接就是 1 万个睡眠点，谁来唤醒、唤醒谁，由 socket 的等待队列决定。线程数 = 连接数，这是它唯一的、也是致命的约束。
- **非阻塞轮询**：把「睡」换成「反复问」。单连接时延最低（数据一到，下一次 `read` 就拿到），但空转时 CPU 100%。更糟的是它**不可扩展**：1,000 个 fd 轮询一遍要 1,000 次 `read`，每次都是一次系统调用（约 0.1–1 µs），10,000 个 fd 就是每秒千万次系统调用。它只在「只有一个 fd、且延迟极敏感」时合理（比如自旋等一个硬件寄存器）。
- **I/O 多路复用**：把「问」集中到一个系统调用里。调用线程只睡一个地方（`epoll_wait`），醒来时拿到「哪些 fd 就绪」的列表；阶段②仍然由它自己 `read`。这是现代服务端的默认形态：**一个线程等一堆 fd，再用少量线程做拷贝与业务**。
- **信号驱动**：注册 `SIGIO` 后线程可以干别的，数据到了内核发信号。听起来像异步，实际很难用：信号会合并（标准信号不排队），一次 `SIGIO` 不告诉你是哪个 fd 就绪，你还是得扫一遍；信号处理函数里能做的事极有限。于是它退化成「用信号触发一次 poll」，工程上基本被 epoll 取代。**它证明了一件事：通知机制不携带「是谁」，就等于没通知。**
- **异步 I/O**：阶段①②都归内核。调用线程提交完就回去干活，内核完成搬运后把结果（读了几个字节、错误码）写进完成队列。注意阶段②并没有消失：数据总得从内核缓冲区到用户缓冲区，只是**这次拷贝不占用你的线程**。§31.8 的 io_uring 就是把它做成环形队列。

这张表还解释了「零拷贝」的位置：五种模型都在讨论「谁等、谁拷」，而 `sendfile`/`splice` 攻击的是阶段②的拷贝次数（→ `base/37`），不改变「谁等」。两件事经常被混为一谈：**epoll 解决等待的可扩展性，零拷贝解决搬运的字节数；一个省系统调用，一个省内存带宽。**

---

## 31.4 select/poll 的结构性缺陷：每次调用都从零开始

### select：一张 1024 位的位图，每次都要重建

`select` 的接口长这样：

```c
fd_set rfds;
FD_ZERO(&rfds);
FD_SET(fd, &rfds);                    /* 每一位对应一个 fd 编号 */
int n = select(maxfd + 1, &rfds, NULL, NULL, &tv);
if (n > 0 && FD_ISSET(fd, &rfds)) { /* 就绪 */ }
```

它有三个硬约束：

1. **`FD_SETSIZE = 1024`**（`/usr/include/x86_64-linux-gnu/bits/typesizes.h` 里写死）。`fd_set` 是 1024 位的位图，fd 编号 ≥ 1024 时 `FD_SET` 直接越界写；`select` 的 `nfds > 1024` 返回 `EINVAL`。这不是「调大一个宏」能解决的：内核的 `select` 实现也按这个上限校验。**1 万连接用 select，在接口层就不可能。**
2. **每次调用都要把集合从用户态拷进内核，返回时再拷回来**，而且返回的集合被改写成「就绪的子集」。所以下一轮必须从原始集合重建——这一步是纯用户态开销，却和 fd 数成正比。
3. **内核线性扫描**每个 fd 的 `poll` 方法，用户态再线性扫描返回的位图找哪一位是 1。

### poll：去掉 1024 上限，没去掉 O(n)

`poll` 把位图换成数组：

```c
struct pollfd { int fd; short events; short revents; };   /* 8 字节 */
```

好处是没有 `FD_SETSIZE` 限制、`events/revents` 分离（不用每轮重建）；坏处是**每个 fd 8 字节，每次调用双向拷贝，内核照样线性扫描**。1 万个 fd 就是 80 KiB 的输入数组 + 80 KiB 的输出数组，每次调用搬 160 KiB，再扫两遍。

### 3 × O(n)：1 万 fd 每次调用到底做了什么

把一次 `poll(10,000 个 fd)` 拆开：

| 步骤 | 工作量 | 10,000 fd 的量 |
| --- | --- | --- |
| ① 用户 → 内核拷贝 `pollfd` 数组 | 8 B × n | 80 KiB |
| ② 内核逐个调 `file->poll()` 检查就绪 | n 次函数调用 + 设备/协议栈判断 | 10,000 次 |
| ③ 内核 → 用户拷贝 `revents` | 8 B × n | 80 KiB |
| ④ 用户态扫描哪些 `revents` 非 0 | n 次分支 | 10,000 次 |

每一步都是 O(n)，**而且这四步在「一个 fd 都没就绪」时也照付**。实测（§31.9）：10,000 fd、只有 1 个就绪，`poll` 一次 4.63 ms；20,000 fd 一次 19.9 ms。按 4.63 ms 算，单核每秒最多做 215 次调用——1 万连接、每连接 10 条消息/秒就是 10 万事件/秒，即使每次调用能收 10 个事件，也要 1 万次调用/秒，需要 46 个核。**这就是「select/poll 到 1000 个 fd CPU 打满」的完整算术**：

- 1,000 fd 时 `select` 实测 0.20–0.34 ms/次；
- 若事件到达是分散的，平均每次只收 1 个事件 → 10 万事件/秒需要 10 万次调用/秒 → 20–34 个核；
- 即使批到每次 10 个事件 → 1 万次/秒 → 2–3.4 个核，仍然吃掉整台 2 核机器。

关键不在「一次调用有多慢」，而在「**调用次数 × 每次都要扫全部 fd**」。活跃连接只有 100 个、空闲 9,900 个时，select/poll 仍然为 9,900 个空闲 fd 付费。这就是结构性缺陷：**成本由「监听的 fd 总数」决定，而不是由「发生的事」决定。**

---

## 31.5 epoll 的三个 syscall 与内核结构：把 O(n) 从「每次问」搬到「每次变」

epoll 只加了三个系统调用：

| syscall | 作用 | 复杂度 | 关键点 |
| --- | --- | --- | --- |
| `epoll_create1(EPOLL_CLOEXEC)` | 创建一个 `eventpoll` 对象，返回 epfd | O(1) | `epoll_create(size)` 的 `size` 自 2.6.8 起被忽略，只需 > 0 |
| `epoll_ctl(epfd, ADD/MOD/DEL, fd, &ev)` | 把 fd 挂上/改事件/摘下 | O(log n) + 注册回调 | 重复 ADD 返回 `EEXIST`；`ev.data` 是你会原样拿回来的 64 位 |
| `epoll_wait(epfd, events, maxevents, timeout)` | 取就绪事件 | **O(就绪数)** | 超时单位毫秒；返回 0 是超时，不是 EOF |

### 内核里到底挂了什么

```
用户态                                内核
                                      ┌────────────── struct eventpoll ──────────────┐
 epfd ──────────────────────────────► │ rbr     红黑树：10,000 个 epitem，按 fd+file 排序 │
                                      │ rdllist 就绪链表：只有「现在能读/能写」的 epitem  │
                                      │ wq      epoll_wait 睡眠的等待队列                │
                                      └──────────────────────────────────────────────┘
                                                     ▲
                                                     │ ep_poll_callback()
                                                     │ （数据到达时在软中断/唤醒路径里执行）
                                      ┌──────────────┴──────────────┐
                                      │ socket 的 sk_wq 等待队列      │ ◄── sock_def_readable()
                                      └─────────────────────────────┘
```

四个动作，按时间顺序：

1. **`epoll_ctl(ADD)`**：内核分配一个 `epitem`（64 位内核上约 160 B，见 §31.7 的 `max_user_watches` 换算），把它插入 `eventpoll->rbr` 红黑树，键是 `(fd 编号, struct file *)`。然后调用该文件的 `poll` 方法，通过 `ep_ptable_queue_proc` 把 `ep_poll_callback` **注册进 socket 的等待队列**。如果此刻已经就绪，直接把 epitem 挂到 `rdllist`。
2. **数据到达**：网卡 → 硬中断 → 软中断 → 协议栈把数据放进 socket 接收缓冲区 → `sk_data_ready` → `wake_up(sk_wq)`。等待队列上的 `ep_poll_callback` 被调用：如果这个 epitem 还不在 `rdllist` 上，就 `list_add_tail` 挂上去，并唤醒睡在 `eventpoll->wq` 上的 `epoll_wait`。**这一步只处理「发生变化的那个 fd」，与总 fd 数无关。**
3. **`epoll_wait`**：如果 `rdllist` 非空，就把链表上的事件拷到用户数组（最多 `maxevents` 个）；如果为空，就在 `wq` 上睡到超时或被回调唤醒。**它不遍历红黑树。**
4. **`epoll_ctl(DEL)` / 关闭最后一个引用**：从红黑树摘除，注销等待队列回调，释放 epitem。

### 为什么是 O(1)（严格说是 O(就绪数)）

把工作量记在「谁触发」上，而不是「谁查询」上：

| 操作 | select/poll | epoll |
| --- | --- | --- |
| 注册一个新 fd | 无（每次调用临时带） | `epoll_ctl` 一次 O(log n) |
| 10,000 个 fd 都空闲，每秒问 1,000 次 | 1,000 × 10,000 次检查 = 10⁷ | 1,000 次 `epoll_wait`，每次 O(1) 返回 0 |
| 1 个 fd 变就绪 | 下一次调用扫 10,000 个才发现 | 回调挂 1 个 epitem 到 `rdllist`，O(1) |
| 100 个 fd 变就绪 | 下一次调用扫 10,000 个 | 100 次回调 + 一次拷贝 100 个事件 |
| 事件总成本 | ∝ 查询次数 × 监听总数 | ∝ 状态变化次数 + 就绪数 |

**这就是「一次问一万个 fd 谁就绪」的真实含义**：epoll 并不是在 `epoll_wait` 里同时问一万个，而是在**注册时**就把「谁有变化会通知我」布置好了；`epoll_wait` 只是取走已经发生的通知。O(n) 的工作没有消失，它被拆成了 n 次 O(1) 的回调，并且**只在真的发生事件时才做**；空闲连接的成本趋近于零。

### 但 epoll_wait 不是免费的：它按「就绪数」收费

这一点几乎所有教程都不讲，却是 §31.9 实测里最反直觉的一行：**当几乎所有 fd 都就绪时，epoll 比 poll 还慢。**

原因是水平触发（LT，默认）下，`epoll_wait` 把事件拷给用户之前，要对每个就绪 epitem **重新调一次 `file->poll()` 确认它仍然就绪**（防止把已经失效的事件交出去），然后才决定是否重新挂回 `rdllist`。这一步是每个就绪 fd 一次，不是每个监听 fd 一次。于是：

- 10,000 fd、1 个就绪：`epoll_wait` 只碰 1 个 epitem → 0.2–0.5 µs；
- 5,000 fd 全部就绪：`epoll_wait` 要碰 5,000 个 → 实测 1.97–4.67 ms，而 `poll` 是 0.86–0.92 ms。

所以正确的说法不是「epoll 是 O(1)」，而是：**epoll_wait 的成本 ∝ min(就绪数, maxevents)，与监听总数无关；当就绪数接近监听总数时，它退化成 O(n)，而且常数比 poll 更大。** 这正是「epoll 快不在读得更快」的第二层证据：它优化的是「稀疏就绪」，不是「密集就绪」。

### 挂 10,000 个 fd 要多久、占多少内存

实测 `epoll_ctl(ADD)` 10,000 次：11.2–27.8 ms（1.1–2.8 µs/次，两次运行），`DEL` 6.1–18.6 ms。这是**一次性**成本，摊到连接生命周期里可以忽略；但如果你的服务每秒新建上万连接，`epoll_ctl` 就会重新变成热点。

内存：每个 epitem 约 160 B（内核文档给的是 64 位内核约 160 B，32 位约 90 B），10,000 个约 1.6 MB；再加上 fd 表、socket 缓冲区，这才是 epoll 方案「1 万连接只占几十 MB」的来源。

---

## 31.6 LT 与 ET：一个按「状态」通知，一个按「变化」通知

### 语义差别只有一句话

- **水平触发（LT，Level Triggered，默认）**：只要 fd 处于「可读」状态，每次 `epoll_wait` 都返回它。你没读完，下次还告诉你。
- **边缘触发（ET，Edge Triggered，`EPOLLET`）**：只在状态**从不可读变成可读**的那一刻通知一次。你没读完，它不再提醒；直到下一次新数据到达（新的边缘）才再通知。

```
数据到达        ┌──────────────┐
────────────────┘              └──────────► 时间
LT 通知        ↑  ↑  ↑  ↑  ↑        （只要没读空，每次 wait 都返回）
ET 通知        ↑                    （只在上升沿返回一次；读空后再来才有下一个沿）
```

把同一条 pipe 写 3 字节、不读，连问两次，实测：

```
[LT] pipe 里写 3 字节，不读
  LT       round 1: epoll_wait -> 1
  LT       round 2: epoll_wait -> 1
  LT   只读 1 字节(剩 2 字节未读)后:
  LT       round 3: epoll_wait -> 1
[ET] pipe 里写 3 字节，不读
  ET       round 1: epoll_wait -> 1
  ET       round 2: epoll_wait -> 0
  ET   只读 1 字节(剩 2 字节未读)后:
  ET       round 3: epoll_wait -> 0
  ET   循环读到 EAGAIN 之前，epoll_wait 永远返回 0
```

LT 的三次都返回 1；ET 只有第一次返回 1，之后无论你读不读、读多少，只要没有**新数据到达**，就一直是 0。

### ET 的三条硬规则

1. **fd 必须是非阻塞的。** 因为你要「一直读到没有为止」，最后一次读必然会遇到「没数据」；如果 fd 是阻塞的，这次 `read` 就把线程睡死了，永远回不到 `epoll_wait`。
2. **必须循环读到 `EAGAIN`。** 只读一次就回事件循环，剩下的数据不会再有边缘通知——这就是「少读一次，连接永久卡住」。
3. **`accept` 也要循环到 `EAGAIN`。** 监听 fd 的 ET 只告诉你「有连接进来了」；如果 backlog 里有 3 个连接，你只 `accept` 1 个，剩下 2 个不会触发新边缘（除非又有新 SYN 到达），它们就烂在队列里。

正确的读循环：

```c
for (;;) {
    ssize_t n = read(fd, buf, sizeof buf);
    if (n > 0) { append_and_process(buf, n); continue; }   /* 继续读，别急着回 epoll */
    if (n == 0) { close_conn(fd); break; }                 /* 对端关闭 */
    if (errno == EAGAIN || errno == EWOULDBLOCK) break;    /* 读空了，这才是出口 */
    if (errno == EINTR) continue;                          /* 被信号打断，重试 */
    close_conn(fd); break;                                 /* 真错误 */
}
```

### 「ET 少读一次就永久卡住」的复现

上面的输出已经复现了核心：写 3 字节，ET 只读 1 字节，剩 2 字节；此后再 `epoll_wait` 永远返回 0。这 2 字节会一直躺在接收缓冲区里，直到对端再发新数据。症状是：

- 客户端已经发了完整请求，服务端却「没收到」；
- `ss -tn` 看 `Recv-Q` 一直大于 0；
- `strace` 看不到新的 `read`，因为 `epoll_wait` 根本没返回；
- 只要客户端重发/心跳，连接又「活」了——于是表现为「偶发卡住，重试就好」。

修法只有两个：要么严格循环读到 `EAGAIN`；要么改用 LT。**没有「ET + 只读一次」的正确写法。**

### LT 也不是免费的：它会空转

LT 下如果你收到事件却不读（或者读不动了），`epoll_wait` 会立刻再次返回同一个 fd——因为「可读」这个状态没变。一个死循环就诞生了：

```c
while (1) {
    n = epoll_wait(...);           /* 返回 fd */
    if (busy) continue;            /* 故意不读，想等会儿再处理 */
}
```

CPU 100%，`strace` 里全是 `epoll_wait` 返回同一个 fd。这也是「LT 更简单」的代价：**你必须保证每次事件都被消费掉，或者主动 `EPOLL_CTL_MOD` 摘掉 `EPOLLIN`。**

### 选 LT 还是 ET

| 维度 | LT | ET |
| --- | --- | --- |
| 编程难度 | 低：一次事件读一次也行 | 高：必须非阻塞 + 读到 EAGAIN + accept 循环 |
| 每次事件的 `read` 次数 | 1 次（没读完下次再通知） | 至少 2 次（最后一次必然 `EAGAIN`） |
| `epoll_wait` 次数 | 可能更多（没读完会反复通知） | 更少（一个边缘一批数据） |
| 漏读后果 | 下次还会提醒 | 永久卡住，直到新数据 |
| 适合 | 请求-响应、每连接一条消息 | 高吞吐、批量、想减少唤醒 |

一个常被忽略的账：ET 省下的是 `epoll_wait` 唤醒，付出的是每个事件多一次 `EAGAIN` 的 `read`。§31.9 的实测里，LT 回显服务 4,000 个请求用了 8,200 次 `read`（每请求 2 次：一次读到数据、一次 `EAGAIN`），就是这个代价。如果每条消息很小、`epoll_wait` 本来就要为每条消息醒一次，ET 的收益有限；如果一次边缘能带一批数据，ET 的收益才明显。

---

## 31.7 epoll 的坑：从 `EPOLLONESHOT` 到 `max_user_watches`

### `EPOLLONESHOT`：一个 fd 同时只能有一个处理者

多线程共用同一个 epfd 时，LT 会把同一个 fd 的事件同时发给多个线程（惊群的变体）。`EPOLLONESHOT` 让 fd 在触发一次后**自动 disarm**，直到你显式 `EPOLL_CTL_MOD` 重新武装。实测：

```
oneshot armed : epoll_wait -> 1
not rearmed   : epoll_wait -> 0   (数据仍在 pipe 里)
after MOD     : epoll_wait -> 1
```

注意第二行：数据一直在 pipe 里，但没 rearm 就永远不通知。用 `EPOLLONESHOT` 的代码必须有「处理完 → MOD 重新注册」这一步，且要处理「处理线程崩溃导致 fd 永久 disarm」的泄漏。

### `EPOLLEXCLUSIVE`：只唤醒一个（或一部分）等待者

多个进程/线程各自有 epfd、都监听同一个 listen fd（比如 `SO_REUSEPORT` 或 fork 继承）时，默认行为是**所有 epoll 实例都收到事件**，于是 N 个线程同时 `accept`，只有一个成功，其余 `EAGAIN`——这就是惊群。`EPOLLEXCLUSIVE`（Linux 4.5+）让内核只唤醒其中一个（man 原文是 "one or more"）。限制：

- 只能用于 `EPOLL_CTL_ADD`，用于 `MOD` 返回 `EINVAL`；
- 可与 `EPOLLIN`/`EPOLLOUT`/`EPOLLET` 组合，不能与 `EPOLLONESHOT` 组合；
- 它只减少无效唤醒，**不替代 accept 循环**：被唤醒的线程仍要把 backlog 取空。

### fd 关闭后的「事件残留」

`close(fd)` 并不总是立刻把 epitem 摘掉：只有当这个 `struct file` 的**最后一个引用**被关闭时才摘。`dup` 出来的 fd 还开着，watch 就还在。更阴的是：

1. 你 `close(fd)` 但没 `EPOLL_CTL_DEL`（假设还有 dup 引用）；
2. 新连接恰好复用了同一个 fd 编号；
3. 旧 epitem 的 `event.data.fd` 还是这个编号，事件被投递上来，你的代码按编号去读——读到了**新连接**的数据，或者读到一个已经被复用的错误对象。

修法：**先 `EPOLL_CTL_DEL`，再 `close`**；`data` 里不要只放 fd 编号，放连接对象的指针 + 代际号（generation），处理前校验「这个对象还活着，且代际匹配」。

### `epoll_wait` 返回 0 不是 EOF

`epoll_wait` 返回 0 只表示**超时**（`timeout` 到期，没有任何事件）。EOF 的表现是：返回 `EPOLLIN`，然后 `read` 返回 0；或者直接返回 `EPOLLHUP`。把 0 当「连接关闭」会导致空闲连接被误杀；把 `EPOLLHUP` 当普通可读、却不 `read`/关闭，会导致 `epoll_wait` 永远立刻返回同一个 fd（CPU 100%）。

`EPOLLHUP` 和 `EPOLLERR` 是**总是上报**的，不受你 `events` 里有没有写影响。所以事件循环的第一层判断应该是：

```c
if (ev & (EPOLLHUP | EPOLLERR)) { close_conn(c); continue; }
if (ev & EPOLLIN)  { do_read(c); }
if (ev & EPOLLOUT) { do_write(c); }
```

### `max_user_watches`：不是 fd 上限，是「watch 总数」上限

```
$ cat /proc/sys/fs/epoll/max_user_watches
445195
```

这是**每个真实用户 ID** 在所有 epoll 实例上能注册的 fd 总数。内核文档给的单价：64 位内核约 160 B/watch，默认值约等于「可用低端内存的 4% ÷ 160 B」。按本机 445,195 算，满额约 71 MB 内核内存。超过时 `epoll_ctl` 返回 `ENOSPC`（不是 `EMFILE`），现象是「fd 还有富余，但注册不进 epoll」。

容器里这个值可能被调小；多进程共享同一 uid 时，所有进程的 watch 数合并计算。排查命令：

```bash
cat /proc/sys/fs/epoll/max_user_watches
grep -i epoll /proc/<pid>/status 2>/dev/null
```

### 其余四个小坑

| 现象 | 根因 | 修法 |
| --- | --- | --- |
| `epoll_wait` 返回 `EINTR` 后程序退出 | 信号打断了睡眠 | `if (n < 0 && errno == EINTR) continue;` |
| 注册 `EPOLLOUT` 后 CPU 100% | socket 发送缓冲区几乎总是有空位，LT 下 `EPOLLOUT` 持续就绪 | 只在有数据要写时注册，写完 `MOD` 摘掉 |
| `maxevents` 太小，吞吐上不去 | 一次只取 1 个事件，剩余就绪项留在 `rdllist`，下次调用立刻返回 | 用 64–1024，按批量调 |
| 超时精度只有毫秒 | `epoll_wait` 的 `timeout` 单位是 ms | 需要 ns 级用 `epoll_pwait2` |

---

## 31.8 io_uring：两个环形队列，把「等」和「拷」都交给内核

### 从「就绪」到「完成」

epoll 是**就绪通知**模型：它告诉你「现在可以读了」，但读的动作还是你做。io_uring 是**完成通知**模型：你提交「帮我读 4 KiB 到这个缓冲区」，内核读完之后告诉你「读了 3,912 字节，错误码 0」。差别在于：

- epoll：`epoll_wait` → `read` → 处理。至少两次系统调用，且 `read` 可能阻塞（除非非阻塞）。
- io_uring：填一个 SQE → `io_uring_enter`（可批量）→ 内核完成读 → CQE 里拿结果。提交与完成都不要求调用线程等。

### 环形队列的物理形态

```
用户态                                        内核
┌────────────── SQ ring ──────────────┐
│ head(内核消费) … tail(用户提交)      │  ← 我填 SQE，移动 tail
│ SQ array: [idx0, idx1, …]           │  ← 间接索引，SQE 可以不连续
└─────────────────────────────────────┘
┌────────────── CQ ring ──────────────┐
│ head(用户消费) … tail(内核填写)      │  ← 内核填 CQE，移动 tail
│ CQE: { user_data, res, flags }      │     16 B/个
└─────────────────────────────────────┘
        io_uring_enter(fd, to_submit, min_complete, flags)
```

- **SQE**（submission queue entry）64 B：`opcode`、`fd`、`addr`、`len`、`user_data` 等。
- **CQE**（completion queue entry）16 B：`user_data`（原样带回）、`res`（返回值/负 errno）、`flags`。
- **SQ array** 是一层间接：SQ ring 里存的是「SQE 数组的下标」，所以可以乱序提交、批量提交。
- `io_uring_enter(fd, to_submit, min_complete, IORING_ENTER_GETEVENTS)`：一次调用既提交 `to_submit` 个 SQE，又可以等待至少 `min_complete` 个完成。**提交和收割共用一个系统调用**，这是它相对 epoll 的第一层节省。

最小提交代码（不依赖 liburing，直接走 syscall 425/426）：

```c
struct io_uring_sqe *sqe = get_sqe(&ring);      /* 取一个空位，填 tail */
sqe->opcode = IORING_OP_RECV;
sqe->fd     = conn_fd;
sqe->addr   = (unsigned long)buf;
sqe->len    = sizeof buf;
sqe->user_data = conn_id;
ring.sq_array[tail & *ring.sq_mask] = tail & *ring.sq_mask;
*ring.sq_tail = tail + 1;                       /* 发布：内核可见 */
syscall(__NR_io_uring_enter, ring.fd, 1, 0, 0, NULL, 0);

/* 稍后收割：没有完成就睡，直到至少 1 个 CQE */
syscall(__NR_io_uring_enter, ring.fd, 0, 1, IORING_ENTER_GETEVENTS, NULL, 0);
struct io_uring_cqe *cqe = &ring.cqes[*ring.cq_head & *ring.cq_mask];
printf("res=%d, user_data=%llu\n", cqe->res, (unsigned long long)cqe->user_data);
```

### 实测：提交立刻返回，数据还没到

对一个**空 pipe** 提交 `IORING_OP_READ`，然后看 CQ：

```
io_uring_setup ok: sq_entries=256 cq_entries=512
after submit: CQ head=0 tail=0 (empty=1), enter took 7.59 us
20ms later, still no data: CQ head=0 tail=0
completion: res=14 user_data=0xdead buf="hello-io-uring"
```

三行就是「真正异步」的全部证据：

1. `io_uring_enter` 只花了 7.6 µs 就返回，**没有等数据**；
2. 20 ms 后 CQ 仍然是空的，说明读操作还挂在内部；
3. 往 pipe 写 14 B 后，CQE 出现，`res=14`，缓冲区里是 `hello-io-uring`。

对比 epoll：同样是空 pipe，`epoll_wait` 会睡到有数据；`read` 再拷一次。io_uring 把「睡」和「拷」都放在了调用线程之外。注意 `res=14` 是「读到了 14 字节」，不是「提交成功」——**提交成功只保证 SQE 入队，完成结果在 CQE 里**。

### 批量提交：4,096 个操作 1–2 次系统调用

用 4,096 个 `IORING_OP_NOP` 测批量（NOP 不是 I/O，只用来量「提交/完成」的固定成本）：

```
4096 NOPs: 278.5 us total, 0.068 us/op, 17 io_uring_enter for 4096 ops   # ring=256
4096 NOPs: 590.5 us total, 0.144 us/op, 2 io_uring_enter for 4096 ops     # ring=4096
4096 getpid: 425.8 us total, 0.104 us/op
```

- ring=256 时，4,096 个操作分 17 次 `io_uring_enter`；ring=4096 时只要 2 次（一次提交、一次收割）。
- 单看 NOP，io_uring 的每操作 CPU 并不比 `getpid` 便宜（0.07–0.14 µs vs 0.10–0.47 µs，两次运行波动很大）。**这正说明它的价值不在「单次更快」，而在「把 N 次系统调用压成 1 次」**：4,096 次 `read` 若每次 0.5 µs，光系统调用就是 2 ms；批量提交把这 2 ms 的不必要跨界省掉了，真正的大头（磁盘/网络等待）本来就不占 CPU。
- 完成队列是共享内存，**收割 CQE 本身不需要系统调用**；只有队列空了要睡时，才需要一次 `io_uring_enter(GETEVENTS)`。

### CQ 溢出：我在这台机器上真踩了一次

第一版代码用 8 个 SQ entry、16 个 CQ entry，一次性提交 4,096 个 NOP，然后坐下来等 4,096 个 CQE。结果是**永久挂起**：CQ 只有 16 个槽，内核填满后新的完成项进不来，`cq_overflow` 开始计数，而我等的那 4,096 个永远不会齐。修法是每提交一批就收割一批（`for (k=0;k<n;k++) wait_cqe(...)`），于是 ring=4096 时变成 2 次 `io_uring_enter`。

这条坑的普适形式：**io_uring 的队列是有界环形缓冲，提交速率必须和收割速率匹配。** 生产代码要么把 CQ 设得足够大（默认是 SQ 的 2 倍，`IORING_SETUP_CQSIZE` 可调），要么保证事件循环持续收割，不能「先提交一百万，最后统一等」。

### `IORING_SETUP_SQPOLL`：连「提交」都不用系统调用

默认模式下，用户每提交一批都要 `io_uring_enter`。`IORING_SETUP_SQPOLL` 让内核起一个线程**主动轮询 SQ tail**：用户只管填 SQE、移动 tail，内核线程看到就执行，完全不进系统调用。代价是这个内核线程会占一个核（空闲 `sq_thread_idle` 毫秒后睡）。本机探测：

```
$ ./sqpoll
IORING_SETUP_SQPOLL: ret=3 errno=0 (Success) features=0xffff
SQPOLL+idle=2000ms: ret=3 errno=0 (Success)
```

`ret=3` 是 epfd 式的 ring fd，说明 6.12 内核允许非特权（本容器是 root、`Seccomp: 0`）创建。容器里 `ps` 看不到 `iou-sqp-*` 内核线程，因为它属于宿主 PID namespace——**看不到不等于没起**，判断方法是看 `sq_thread_idle` 到期前的 CPU 占用，或读 `/proc/<pid>/status` 的线程数。

### 注册 buffer / 注册文件：把每次操作的固定开销也省掉

- `IORING_REGISTER_BUFFERS`：提前把用户缓冲区钉住（`get_user_pages`），之后每个 SQE 用 `buf_index` 引用，省掉每次 `read` 的页表遍历与锁定。
- `IORING_REGISTER_FILES`：提前把 fd 注册成固定索引，SQE 里用 `IOSQE_FIXED_FILE` + 索引，省掉每次的 fd 表查找与 `fdget`。
- 这两项对高 IOPS、小操作的负载收益最大；对「每连接每秒几条消息」的推送服务，收益有限。

### 什么时候比 epoll 快，什么时候不值得

| 判据 | 选 epoll | 选 io_uring |
| --- | --- | --- |
| 事件模型 | 就绪通知，业务自己读写 | 完成通知，内核代读代写 |
| 系统调用 | 每个事件至少 1 次 `epoll_wait`（批量好的话摊薄） | 每批 1 次 `enter`，甚至 0 次（SQPOLL） |
| 真正的异步 | 否：`read` 仍由调用线程拷贝 | 是：提交即返回，拷贝不占调用线程 |
| 内核要求 | 2.6 起 | 5.1 起；`SQPOLL`/新 opcode 要更高版本 |
| 工程复杂度 | 低，资料多，调试工具全 | 高：手写 ring、队列溢出、注册生命周期 |
| 被谁挡住 | 几乎没有 | seccomp（Docker 默认 profile 旧版会拦 `io_uring_setup`）、`kernel.io_uring_disabled`、内核 < 5.4 |
| 适合 | 十万级长连接、请求-响应、代理 | 高 IOPS 存储、数据库、批量 send/recv、想省 syscall 的场景 |

一个诚实的结论：**对大多数「一万连接、每连接每秒几条消息」的服务，epoll 已经足够，io_uring 的收益主要来自批量与 SQPOLL，而不是「异步」这个标签。** 只有当系统调用本身成为瓶颈（每秒百万级小 I/O）、或者你要绕开「调用线程必须参与拷贝」时，io_uring 才划算。`base/36` 会用同一个 echo 服务把两者的 syscall 次数摆在一起。

---

## 31.9 实测：五组能复现的数字

环境（全部实测）：

```
$ nproc; uname -r
2
6.12.113+deb12-cloud-amd64
$ gcc --version | head -1
gcc (Debian 12.2.0-14+deb12u1) 12.2.0
$ strace -V | head -1
strace -- version 6.1
$ cat /proc/sys/fs/epoll/max_user_watches
445195
$ cat /proc/sys/kernel/io_uring_disabled
0
$ grep Seccomp /proc/self/status
Seccomp:        0
```

没有 liburing，io_uring 部分全部用 `syscall(__NR_io_uring_setup/enter)` 手写；`perf` 未安装，计时用 `clock_gettime(CLOCK_MONOTONIC)`。**绝对值会随机器负载漂 2–4 倍，比例与量级才是结论。**

### ① 一个 epoll 回显服务的 syscall 账

服务端：单线程、非阻塞、LT、`epoll_wait` 返回后把每个 fd 读到 `EAGAIN`；客户端：1,000 条连接、每条 20 次「发 64 B → 收 64 B」。`strace -c -f`（4000 请求的规模）与服务端自计数（20,000 请求的规模）各跑一次：

```
% time     seconds  usecs/call     calls    errors syscall
------ ----------- ----------- --------- --------- ----------------
 50.12    0.201159          16     12201      4000 read
 30.19    0.121194          15      8001           write
 13.39    0.053728          13      4067           epoll_wait
  1.68    0.006761          16       402           close
  1.49    0.005963          29       200           connect
  0.82    0.003278        3278         1           futex
  0.60    0.002412           6       401           setsockopt
  0.54    0.002161           5       401           epoll_ctl
100.00    0.341260          13     26072      4004 total
```

读这张表要注意三件事：

1. **`read` 12,201 次 = 8,200 次成功 + 4,000 次 `EAGAIN` + 1 次启动**。每个请求 2 次 `read`，因为「读到 EAGAIN」是 LT 下清空缓冲区的代价。`errors=4000` 就是 4,000 次 `EAGAIN`。
2. **`write` 8,001 次 ≈ 4,000 次服务端回显 + 4,000 次客户端发送 + 1 次启动**。这条链路里没有魔法：每个请求仍然一次读、一次写。
3. **`epoll_wait` 4,067 次**，基本是「每个请求醒一次」（客户端严格 ping-pong）。如果客户端批量发送，这个数字会大幅下降（见下）。

`strace` 会把每次系统调用放慢 10–20 倍（表里 `epoll_wait` 13 µs/次，非 strace 是 0.3–0.7 µs），所以**只用它数次数，不用它测时间**。非 strace 的墙钟（20,000 请求）：

| 方案（1,000 连接 × 20 请求） | 墙钟 | 每请求 | 上下文切换/请求 | 服务端 syscall/请求 |
| --- | --- | --- | --- | --- |
| 一线程一连接 | 2,365 ms | 118.3 µs | 2.25 | 约 2（read + write） |
| epoll 单线程，ping-pong | 1,113 ms | 55.7 µs | 约 1.0 | 4.27 |
| epoll 单线程，客户端批量 | 740 ms | 37.0 µs | 约 0.1 | **0.73** |

最后一行是关键：客户端先把 20 条消息全部写出，服务端一次 `epoll_wait` 能收多个 fd、一次 `read` 能收多条消息（20,000 条消息只用了 5,992 次 `read`，平均 3.3 条/次），总 syscall 从 85,399 降到 14,569。**批量比「选哪个模型」更能决定 syscall 数量**——这也是 `base/36` 的主题。

### ② LT vs ET 的行为差异

```
[LT] 写 3 字节不读：wait → 1, 1, 1（读 1 字节后仍然 1）
[ET] 写 3 字节不读：wait → 1, 0, 0（读 1 字节后仍然 0）
[ET-correct] 一次事件内循环读到 EAGAIN：共读 3 字节，末尾 errno=EAGAIN
```

这组输出证明：ET 的「0」不是没数据（pipe 里还有 2 字节），而是**没有新的边缘**。ET 的正确性完全依赖「循环读到 EAGAIN」这条纪律。

### ③ select / poll / epoll 在 1 个就绪 fd 下的耗时

1,000 个 socketpair 的读端，只让 1 个可读；每次调用 `timeout=0`：

| 监听 fd 数 | select | poll | epoll_wait |
| --- | --- | --- | --- |
| 200 | 28.0 µs | 47.9 µs | 1.2 µs |
| 1,000 | 205–338 µs | 221–350 µs | 0.3–0.7 µs |
| 4,000 | 不支持（>1024） | 770 µs | 0.2–0.7 µs |
| 10,000 | 不支持 | 4,633 µs | 0.2–0.5 µs |
| 20,000 | 不支持 | 19,919 µs | 0.2–0.6 µs |

两列对比说明了三件事：

- `poll` 的时间随 fd 数线性涨：10,000 fd 4.6 ms，20,000 fd 19.9 ms（超线性是因为缓存与分支预测都崩了）。
- `epoll_wait` 在「只有 1 个就绪」时几乎恒定在 0.2–0.7 µs，**与监听总数无关**。
- `select` 在 1,000 fd 就已是 0.2–0.34 ms，而且 1,024 是硬上限——1 万连接连参赛资格都没有。

### ④ 全部就绪时，epoll 反而不如 poll

| 全部就绪的 fd 数 | epoll_wait | poll | 每 fd 成本（epoll / poll） |
| --- | --- | --- | --- |
| 1,000 | 176–221 µs | 62–72 µs | 176–221 ns / 62–72 ns |
| 5,000 | 1,971–4,672 µs | 863–921 µs | 394–934 ns / 173–184 ns |

原因在 §31.5 讲过：LT 下 `epoll_wait` 要对每个就绪项重新 `poll` 一次确认，而 `poll` 只是扫一遍数组。**所以「epoll 一定比 poll 快」是错的；正确的说法是「就绪稀疏时 epoll 快，就绪密集时 epoll 没有优势，甚至更慢」。** 这也解释了为什么「把 maxevents 调大」不是万能药：事件多的时候，拷贝与确认的成本本来就存在。

### ⑤ io_uring：提交即返回 + 批量

```
io_uring_setup ok: sq_entries=256 cq_entries=512
after submit: CQ head=0 tail=0 (empty=1), enter took 7.59 us
20ms later, still no data: CQ head=0 tail=0
completion: res=14 user_data=0xdead buf="hello-io-uring"
4096 NOPs: 278.5 us total, 0.068 us/op, 17 io_uring_enter for 4096 ops
4096 getpid: 425.8 us total, 0.104 us/op
sq_dropped=0 cq_overflow=0
```

`io_uring_setup` 返回的 ring 可用；空 pipe 上的读提交 7.6 µs 返回、20 ms 后仍无完成、写入后 CQE 到达——**这才是「调用线程不参与等待与拷贝」的直接证据**。批量提交把 4,096 次系统调用压成 17 次（ring=256）或 2 次（ring=4096）。

没有做到的：`perf` 未安装，所以没有硬件计数器；`SQPOLL` 的内核线程在容器 PID namespace 里不可见，只能确认 `io_uring_setup` 成功；`strace` 会严重扭曲 io_uring 的时序，因此批量数字全部来自非 strace 运行。这些都不影响上面的量级结论。

---

## 31.10 三列账：承诺、代价、崩点

| 维度 | 承诺了什么 | 为此付了什么 | 从哪里开始失效 |
| --- | --- | --- | --- |
| 一个线程管万连接 | `epoll_wait` 一次取回就绪集合；空闲连接几乎零成本 | 10,000 次 `epoll_ctl`（约 11–28 ms 一次性）+ 1.6 MB epitem；每个事件仍要 `read`/`write` | 就绪密集时 `epoll_wait` 退化成 O(就绪数)，甚至慢于 poll；ET 漏读会让连接永久卡住 |
| 免掉一万次系统调用 | 把「问每个 fd」变成「等回调」 | 回调注册、红黑树维护、`data` 生命周期管理 | 回调只在状态变化时来；LT 下不消费会空转；`EPOLLHUP`/`EPOLLERR` 不处理会死循环 |
| ET 更少唤醒 | 一个边缘处理一批数据，`epoll_wait` 次数下降 | 必须非阻塞 + 读到 `EAGAIN` + accept 循环；代码复杂度上一个台阶 | 任何一次「只读一半」都丢事件；多线程读同一 fd 会互相偷数据 |
| 多核扩展 | 多线程共用 epfd 或 `SO_REUSEPORT` 多队列 | 惊群、`EPOLLONESHOT` 的 rearm、连接归属迁移 | 忘记 rearm → fd 永久静默；`EPOLLEXCLUSIVE` 只减少唤醒，不保证负载均衡 |
| io_uring 真异步 | 提交即返回，内核完成拷贝；批量摊薄 syscall | 内核 5.1+、手写 ring、CQ 溢出、buffer/file 注册生命周期 | seccomp 拦截、`kernel.io_uring_disabled`、CQ 满导致完成丢失、调试工具少 |
| 统一事件循环 | 网络、定时器、信号、pipe 都能塞进一个 epoll | 定时器要用 `timerfd`，信号要用 `signalfd`，唤醒要用 `eventfd` | 每个 fd 都是一份状态；fd 泄漏 = 事件循环永久多一个负担 |

一句话收束：**epoll 买到的是「等待的可扩展性」，付出的是「事件生命周期的管理成本」；io_uring 买到的是「连拷贝都不占调用线程」，付出的是「内核版本、工具链与队列管理」。** 两者都没有让数据搬运本身变快。

---

## 31.11 踩坑清单：现象 → 根因 → 定位 → 修

| 现象 | 根因（指回本篇） | 定位命令 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| CPU 100%，`strace` 全是 `epoll_wait` 返回同一个 fd | LT 下没读空，或 `EPOLLHUP`/`EPOLLERR` 没处理（§31.6/31.7） | `strace -c -p <pid>`；`ss -tnp` 看 `Recv-Q` | 事件必须消费；HUP/ERR 先关闭；或改 ET + 读到 `EAGAIN` | 事件循环里禁止「收到不处理」的分支；加 `EPOLLRDHUP` |
| 连接偶发卡住，重发就好 | ET 只读了一次，剩余数据没有新边缘（§31.6） | `ss -tn` 看 `Recv-Q > 0`；`strace` 看不到后续 `read` | 循环读到 `EAGAIN`；accept 也循环 | 代码评审检查每个 ET 分支都有 drain 循环 |
| `epoll_wait` 返回 -1，程序退出 | 被信号打断，`errno=EINTR`（§31.7） | `strace -e trace=epoll_wait` 看 `EINTR` | `if (errno==EINTR) continue;` | 信号处理统一走 `signalfd`，别用默认处理 |
| 空闲连接被误判为关闭 | 把 `epoll_wait` 返回 0 当成 EOF（§31.7） | 打印返回值与 `errno`；对照 `timeout` | 0 只表示超时；EOF 看 `read()==0` 或 `EPOLLHUP` | 事件循环区分「超时 / 可读 / 关闭」三态 |
| 新连接收到旧连接的数据 | `close` 前没 `EPOLL_CTL_DEL`，fd 编号被复用（§31.7） | `strace -e close,epoll_ctl` 对时序 | 先 DEL 再 close；`data.ptr` 放对象 + 代际号 | 封装 `conn_close()`，禁止裸 `close` |
| `epoll_ctl` 返回 `ENOSPC`，但 fd 还够 | 触到 `max_user_watches`（§31.7） | `cat /proc/sys/fs/epoll/max_user_watches` | 减少 watch、拆分 uid、调大 sysctl（评估内存） | 监控 watch 总数；连接池复用 |
| io_uring 程序永久挂起 | CQ 溢出，完成项被丢（§31.8） | 读 `cq_overflow`；看 `io_uring_enter` 返回值 | 每批提交后收割；调大 CQ | 提交/收割速率匹配；压测时盯 overflow |
| io_uring 在容器里 `EPERM`/`ENOSYS` | seccomp 拦截或 `io_uring_disabled`（§31.8） | `grep Seccomp /proc/self/status`；`cat /proc/sys/kernel/io_uring_disabled` | 换 epoll；或调整 seccomp profile（需评估安全面） | 启动时探测一次，失败降级到 epoll |

三行总结：**看到 CPU 100%，先查「有没有事件没消费」；看到连接卡住，先查「ET 有没有读空」；看到 fd 相关怪事，先查「close 与 epoll_ctl 的顺序」。**

---

## 31.12 一页速记

```
【一句话】异步只是「谁来等」；epoll 省的是「问一万个 fd」的系统调用，不是 read 的时间。

【两维四格】
  阻塞/非阻塞 = fd 属性（O_NONBLOCK，struct file 级，dup/fork 共享）
  同步/异步   = 谁拷数据、何时算完成
  非阻塞 ≠ 异步；async/await ≠ 异步 I/O；glibc AIO 是线程池在等

【五模型：两个阶段】
  阻塞     : 线程等 + 线程拷
  非阻塞   : 线程轮询 + 线程拷
  多路复用 : epoll 等 + 线程拷
  信号驱动 : 内核信号 + 线程拷（信号不携带 fd，退化）
  异步     : 内核等 + 内核拷（io_uring）

【select/poll 的三次 O(n)】
  拷进 80 KiB → 内核扫 10,000 → 拷出 80 KiB → 用户扫 10,000
  10,000 fd 实测 4.6 ms/次；FD_SETSIZE=1024，select 直接出局

【epoll 三个 syscall】
  create1 → eventpoll{rbr, rdllist, wq}
  ctl     → 红黑树 O(log n) + 注册 ep_poll_callback
  wait    → 只取 rdllist，O(就绪数)
  回调路径：数据到达 → sk_wq → ep_poll_callback → rdllist → 唤醒

【三个数字】
  10,000 fd、1 个就绪：epoll_wait 0.2–0.5 µs；poll 4.6 ms（约 10^4 倍）
  10,000 个 epitem ≈ 1.6 MB（160 B/个）
  max_user_watches 本机 445,195（约 71 MB 上限）

【ET 三条铁律】
  非阻塞 + 读到 EAGAIN + accept 也循环
  少读一次 = 永久卡住，直到新数据到达

【io_uring】
  SQ/CQ 两个环 + sq_array 间接索引；enter 一次可提交一批、等一批
  真异步证据：提交 7.6 µs 返回，CQ 空；写数据后 CQE res=14
  4096 NOP → 2 次 enter（ring=4096）；CQ 满会丢完成
```

---

## 31.13 自检 3 题

**Q1.** 10,000 个 fd 里只有 1 个就绪时，`epoll_wait` 实测 0.2–0.5 µs，而 `poll` 要 4.6 ms。请从「谁在什么时候做了 O(n) 的工作」解释这个差距；再解释为什么 5,000 个 fd **全部就绪**时，`epoll_wait` 反而比 `poll` 慢。

<details><summary>答案</summary>

稀疏就绪时，O(n) 的工作被搬到了两个别的地方：

1. **注册时**：`epoll_ctl(ADD)` 把 fd 插入红黑树（O(log n)），并把 `ep_poll_callback` 注册到 socket 的等待队列。这是每个 fd 一次的固定成本，之后不再重复。
2. **状态变化时**：数据到达 → `wake_up(sk_wq)` → `ep_poll_callback` 只把「变化的那一个 epitem」挂到 `rdllist`，O(1)。没有变化的 fd 完全不参与。

`epoll_wait` 只做两件事：看 `rdllist` 是否为空；非空就把链表上的事件拷给用户。所以它的成本 ∝ 就绪数（这里是 1），与监听总数 10,000 无关。`poll` 则相反：每次调用都要把 10,000 个 `pollfd` 拷进内核、逐个调 `file->poll()`、再拷回、用户再扫一遍——4 个 O(n) 步骤，即使 9,999 个 fd 什么都没发生也照付。

全部就绪时，`epoll_wait` 的 `rdllist` 上有 5,000 个 epitem。LT 模式下，它在把事件交给用户之前，要对**每个就绪项重新调一次 `file->poll()`** 确认仍然就绪，然后才决定是否重新挂回链表；这是 5,000 次确认 + 5,000 个事件的拷贝。`poll` 只需要扫一遍数组、填 `revents`。所以前者的常数更大，实测 1,971–4,672 µs vs 863–921 µs。结论：**epoll 优化的是「大量监听、少量就绪」，不是「读得更快」，也不是「任何情况下都更快」。**
</details>

**Q2.** ET 模式下，一个连接收到 3 条消息（共 300 字节），你只 `read` 了一次（读到 100 字节）就回事件循环。接下来会发生什么？为什么 ET 必须配非阻塞 fd？如果把 fd 设成阻塞的，但在读循环里「读一次就 break」，会怎样？

<details><summary>答案</summary>

剩下 200 字节留在 socket 接收缓冲区里，但**不会再有边缘通知**：ET 只在「不可读 → 可读」的状态变化时回调。已经可读的 fd 继续可读，不是新的边缘，所以 `epoll_wait` 不会再返回它。连接表现为「卡住」：客户端认为发了，服务端不处理，`ss -tn` 能看到 `Recv-Q` 持续大于 0。直到对端再发新数据（新的边缘）才恢复——这就是「偶发卡住、重试就好」。

ET 必须配非阻塞，是因为「读到没有为止」的最后一次 `read` 一定会遇到「当前没有数据」：
- 非阻塞 fd：这次 `read` 返回 `-1/EAGAIN`，你据此退出循环，回到 `epoll_wait`，线程不被卡住。
- 阻塞 fd：这次 `read` 会把线程睡在 socket 等待队列上，直到下一批数据到达。在此期间该线程无法处理任何其他连接，等于把一个「事件循环」退化成了「一线程一连接」。

如果 fd 是阻塞的、且读一次就 break：第一次 `read` 有数据，返回 100 字节，你 break 回 `epoll_wait`；剩下 200 字节仍然可读，但 ET 不再通知，于是永久卡住。所以「ET + 阻塞 fd + 读一次」是双重错误：既丢边缘，又可能在最后一次读时睡死。
</details>

**Q3.** io_uring 提交一个 `read` 后立刻返回，此时数据在哪？「异步」省掉了两次拷贝中的哪一次？如果内核最后是交给一个 worker 线程去阻塞读，它还算「异步 I/O」吗？

<details><summary>答案</summary>

提交返回时，数据**还在内核的 socket 接收缓冲区（或页缓存）里**，甚至可能还没到达。SQE 只是「登记意图」，`io_uring_enter` 把 SQE 放进 SQ ring 并通知内核；内核此时可能已经注册了 poll 等待，也可能把请求挂起。CQE 出现时，数据才已经被拷到 SQE 里 `addr` 指向的用户缓冲区，`res` 是拷贝的字节数。

「异步」省掉的是**调用线程参与阶段②（内核缓冲区 → 用户缓冲区）的那次拷贝等待**。拷贝本身没有消失：数据总得从内核空间到用户空间（或通过零拷贝映射/发送）。io_uring 只是让这次拷贝由内核在后台完成，调用线程可以继续提交/处理别的事。

如果内核用 worker 线程完成，仍然是异步 I/O——从调用者的接口语义看：提交立即返回、完成通过 CQE 通知、调用线程不被这次操作绑住。区别在「等待者是谁」：不是你的线程，而是内核线程池里的某个工作者。这也回到本篇的主线：**异步从来没有消灭等待，只是把等待从你的线程挪到了别处。** 真正要问的是「那个别处有没有足够的并发度、会不会成为新的瓶颈」。
</details>

---

## 31.14 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 一次系统调用的进入/返回、`syscall` 指令与上下文切换的单价（本篇所有「跨界成本」的底层） | → `base/21` |
| socket 收包全路径：硬中断、软中断、NAPI、socket 缓冲区与唤醒（`ep_poll_callback` 的上游） | → `base/32` |
| 零拷贝四实现：`read/write`、`mmap`、`sendfile`、`splice` 的拷贝次数账（阶段②的另一半） | → `base/37` |
| 实例剖析：同一个 echo 服务下 epoll 与 io_uring 的完整 syscall 账 | → `base/36` |
| Go runtime 的 netpoll：如何把 epoll 包成 goroutine 的「阻塞」假象 | [go/19](../go/19-scheduler-runtime.md) |
| SSE / WebSocket 的长连接工程形态：心跳、背压、连接迁移 | [go/39](../go/39-realtime-sse-websocket.md) |
| 全书总纲：四条主线与 56 篇清单 | [0-summary](0-summary.md) |
