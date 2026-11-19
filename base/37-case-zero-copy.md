# 37. 实例剖析：零拷贝四实现——`read/write`、`mmap`、`sendfile`、`splice`

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 操作系统：Linux 视角（实例篇） ｜ **编号**：37 / 56
> **本篇定位**：Part III 的最后一块验伪砖。14 篇说「CPU 最贵的资源是搬数据的次数」，32 篇说「收包要过六道门」，本篇把这两句话按在**同一份 200 MiB 文件、同一个 socket、同一台 2 核机器**上，用四种写法各发一遍，把「拷贝几次、切换几次、花多少 CPU」变成一张可以直接对账的表。读完的判据：**再听到「上零拷贝」时，你先问「数据要不要被 CPU 看」，而不是先问「用哪个 API」。**
> **读法**：§37.1 是一个真实形状的故障（明文翻倍、上 TLS 归零）；§37.2–§37.3 是模型（是什么、四张路径图），只讲能推出数字的部分；§37.4 是全篇重心，**所有数字都是一台 2 核机器上真跑出来的**，包含 ④ 的「改前 / 改后」两列；§37.5 用这些数字逐条证伪四句流行话；§37.6 是选型边界，可以直接贴进设计评审；§37.7 把四个 API 对回 Nginx / Kafka / io_uring / gRPC。
> **不在本篇**：系统调用的完整入口与 `syscall` 指令 → `base/21`；page cache、inode、回写 → `base/28`、`base/29`；DMA 与中断的器件视角 → `base/14`；socket 收发六道门与软中断 → `base/32`；epoll 与 io_uring 的 syscall 账 → `base/36`；TLS 记录层与证书链 → `base/47`；TLS 内核卸载（kTLS）只给结论，不展开内核实现。

---

## 37.1 引子：明文翻倍，加了 TLS 又回到原点

一个只读静态文件服务，Nginx 托管一批 200 KiB – 2 MiB 的 JS/CSS 包，机器 2 核，出口 10 Gbps。上线时用的是最朴素的配置——没有 `sendfile`，每次请求 `read()` 进用户缓冲、再 `write()` 进 socket。压测 1800 QPS，`top` 里 `%sy` 常年在 70% 以上。

运维加了一行 `sendfile on;`，同一份压测脚本：

| 指标 | 改前 | 改后 |
| --- | --- | --- |
| QPS | 1800 | 3600 |
| 单核 `%sy` | 72% | 34% |
| `strace -c` 里 `read`+`write` 次数 | 每请求 2 次 | 0 次（全变成 `sendfile`） |

数字很漂亮。于是同一套配置被推到 HTTPS 站点上，期望再翻一倍。结果：

| 指标 | 明文 + sendfile | HTTPS + sendfile（未开 kTLS） |
| --- | --- | --- |
| QPS | 3600 | 1700 |
| 单核 `%sy` | 34% | 78% |
| 用户态 CPU | 12% | 41% |

**加了加密之后，`sendfile` 的收益不但归零，还比最早的明文 `read/write` 更慢。** 三句常见的解释都不对：

- 「`sendfile` 没生效」——生效了，但它被 TLS 库架空了（§37.5 ④）；
- 「TLS 太慢」——AES-NI 的 AES-128-GCM 在这台机器上能跑 3.36 GB/s（§37.4 实测），加密本身不是主因；
- 「零拷贝不适合小文件」——这批文件 200 KiB 起，不算小。

真正的原因是本篇要量的那一件事：**`sendfile` 省的是「CPU 把字节从 A 搬到 B」；而 TLS 要求 CPU 把每个字节读进来、算出密文、再送出去。只要内容必须被看，搬运工就必然回来。**

我把这个场景缩小到一台 2 核机器上复现了：同一份 200 MiB 文件，四种实现各发一遍到本机 socket，再加一个「读进来 + AES-GCM 加密 + 写出去」的变体。下面出现的每一个毫秒、每一次 syscall、每一个缺页数，都是那条命令的输出。

> **本篇量到的是主线 ③**：每层抽象的单价 = 跨界次数 × 单次成本。这里被定价的「跨界」有三种——DMA（设备↔内存）、CPU 拷贝（内核↔用户）、模式切换（用户态↔内核态）。四套 API 的全部差别，就是把这三本账重新分配了一遍。

---

## 37.2 ① 它是什么：把「文件 → 网卡」拆成两次拷贝、两次切换

先给 30 秒版本。

一个进程要把文件内容送到 socket，最直白的写法是两个系统调用：

```c
while ((n = read(fd, buf, 1 << 20)) > 0)   /* 内核 → 用户：copy_to_user */
    write(sock, buf, n);                    /* 用户 → 内核：copy_from_user */
```

这两行代码看起来只是「读一下、写一下」，但在物理上它做了四件事：

1. `read` 陷入内核（模式切换 1 次），把 page cache 里的字节**复制**到你的 `buf`（CPU 拷贝 ①）；
2. `read` 返回用户态（模式切换 1 次）；
3. `write` 陷入内核（模式切换 1 次），把 `buf` 里的字节**复制**到 socket 的发送队列（CPU 拷贝 ②）；
4. `write` 返回用户态（模式切换 1 次）。

也就是说，**每 1 MiB 数据要付 2 次 CPU 拷贝 + 4 次模式切换 + 2 次 syscall**。而这其中，用户态的 `buf` 是一个纯粹的中转站：数据进去、马上又出来，中间没有任何一个字节被程序读过或改过。**零拷贝要砍的就是这一站。**

### 三个必须分开的概念

「拷贝」这个词在同一句话里经常指三件不同的事，混在一起就永远算不清账：

| 名字 | 谁在搬 | 单价（本机实测/量级） | 能不能省 |
| --- | --- | --- | --- |
| **DMA 拷贝** | 设备控制器（磁盘、网卡） | 受内存带宽与 PCIe 限制，CPU 不参与 | **省不掉**，数据总得从设备到内存 |
| **CPU 拷贝** | `memcpy` / `copy_to_user` / `copy_from_user` | 200 MiB 约 65 ms（≈3.1 GiB/s，见 §37.4） | **这才是零拷贝省的东西** |
| **模式切换** | `syscall` / `sysret` 指令 + 内核入口的栈切换 | 约 0.1 µs/次（量级：两条指令 + 保存现场） | 可以减少，不能降到 0 |

三个系数都是可测的常数，不是形容词。`t_copy` 在本机是 **65 ms / 200 MiB**；`t_syscall` 是 **0.6–1 µs**（一次完整调用，含内核里那点工作）；裸模式切换比它小一个量级，200 MiB 分摊到 400 次调用上只值 0.04 ms，可以忽略——**在 1 MiB 这种块大小下，钱几乎全在拷贝里**。这个判断会在块变小时翻转（§37.4 的 4 KiB 管道实验：102400 次调用 × 0.6 µs = 61 ms，和一次拷贝一样贵）。

由此得到本篇唯一的成本模型：

```
T ≈ N_syscall × t_syscall + N_copy × t_copy + N_switch × t_switch
```

### 为什么偏偏是「文件 → socket」能零拷贝

因为这条路径的两端**本来就在内核地址空间里**：

- 文件内容已经在 **page cache** 里（一份内核页）；
- socket 的发送队列（`sk_buff` 的 frags）也**在内核里**。

既然两端都在内核，内核完全可以把「page cache 那一页的引用」直接挂到 skb 上，让网卡的 SG-DMA 去读它——**根本不需要让数据经过用户态**。这就是零拷贝能成立的全部前提，一句话：

> **两端都在内核里，且中间没有任何一步需要 CPU 看内容。**

反过来说，只要有一端在用户态（TLS 要加密、gzip 要压缩、要改 HTTP 头、要算签名），这个前提立刻破产，四种 API 谁都救不了你。

### 什么时候这笔账值得算

单次拷贝 65 ms 听起来很多，但它要和「传输本身要多久」比。用本机实测的 3.1 GiB/s 与常见链路带宽算一笔：

| 链路 | 传 200 MiB 需要 | `read/write` 多出的 2 次 CPU 拷贝（约 130 ms）占比 | 结论 |
| --- | --- | --- | --- |
| 1 Gbps | 1.68 s | 8% | 零拷贝是锦上添花 |
| 10 Gbps | 0.168 s | 77% | 不做就到顶了 |
| 40 Gbps | 0.042 s | 310%（单核不可能） | 必须零拷贝 + 多队列 |

这张表解释了一个常见困惑：**在千兆环境里上零拷贝，测不出差别；在万兆环境里不上，CPU 直接打满。** 不是 API 时灵时不灵，是分母变了。

---

## 37.3 ② 物理形态：总拷贝 4 → 2，CPU 拷贝 2 → 0

下面四张图用同一个口径：**从「文件内容进入 page cache」（含那一次磁盘 DMA）开始，到「字节进入网卡」为止**。磁盘 DMA 四家都要付，所以它是公共项；真正的差别在 page cache 之后。本篇实验的发送前会先 `cat` 一遍文件把 page cache 焐热，理由见 §37.4。

### 图 1：`read` + `write`——4 次拷贝，2 次 CPU 拷贝

```
磁盘 ──DMA①──> page cache ──CPU②──> 用户 buf ──CPU③──> socket sndbuf ──DMA④──> 网卡
                └────────── read() ─────────┘   └────────── write() ──────────┘
```

- 4 次拷贝里，**2 次是 CPU 干的**（②③），这两次就是零拷贝要砍的；
- 每 1 MiB：2 个 syscall、4 次模式切换；
- 用户缓冲的大小决定了「每次搬多少」，但搬的总字节数不变：**发 N 字节，CPU 就搬 2N 字节**。

### 图 2：`mmap` + `write`——3 次拷贝，1 次 CPU 拷贝

```
磁盘 ──DMA①──> page cache ──CPU②──> socket sndbuf ──DMA③──> 网卡
                  ↑
                  └── mmap 只建立页表映射，一个字节都不搬；
                      但每一页第一次访问都要缺页（内核批量预映射，实测每 64 KiB 一次）
```

- `mmap` 把 page cache 的物理页映射进用户地址空间，`write(sock, p, n)` 里的 `p` 指向的就是那页；
- 于是 `copy_to_user` 那一次没了，只剩「从用户虚拟地址读到 socket」这一次 CPU 拷贝（②）；
- 每 1 MiB：1 个 `write` + 一次性 `mmap`，2 次模式切换；
- **代价从拷贝变成了缺页**：本机 200 MiB 实测 **3214 次 minor fault**（每 64 KiB 一次，缺页批处理 16 页）；16 KiB 小文件、每请求重新 `mmap` 的场景，4000 个请求触发 **4015 次缺页**——平均每个请求一次，比数据拷贝还贵。

### 图 3：`sendfile`——2 次拷贝，0 次 CPU 拷贝

```
磁盘 ──DMA①──> page cache ─────────────> socket sndbuf ──DMA②──> 网卡
                       页的引用被挂进 skb，CPU 不碰字节
```

- `sendfile(out_fd, in_fd, &off, count)` 在内核里走 `splice_direct_to_actor`：从 page cache 取出页，交给 `tcp_sendpage` 挂到 skb 的 frags 上；
- 每 1 MiB：1 个 syscall、2 次模式切换；单次调用的 `count` 上限约 2 GiB，但阻塞 socket 会按发送缓冲分段返回，所以代码里仍要循环；
- 前提：`in_fd` 必须是可 mmap 的普通文件（管道、字符设备会 `EINVAL`），`out_fd` 在 2.6.33 之后可以是任意支持写入的文件；
- 隐藏前提：网卡要支持 SG-DMA（scatter-gather）。不支持时内核会把这些页**线性化**成一块连续内存——**零拷贝当场退化成一次拷贝**，而且你在 API 层看不出来。

### 图 4：`splice`——2 次拷贝，0 次 CPU 拷贝，但过一根管子

```
磁盘 ──DMA①──> page cache ──引用──> pipe buffer ──引用──> socket sndbuf ──DMA②──> 网卡
```

- `splice` 规定**两端必须有一端是管道**，所以文件到 socket 要拆成两步：`file → pipe`、`pipe → socket`；
- 每一步只搬「页的引用」（`pipe_buffer` 里记的是 `page* + offset + len`），不搬字节；
- 每 1 MiB：2 个 syscall、4 次模式切换——**比 `sendfile` 多一倍**；
- 管道容量是硬约束：默认 **64 KiB**，`fcntl(F_SETPIPE_SZ)` 最大可到 **1 MiB**（`/proc/sys/fs/pipe-max-size`）。容量越小，同一个文件被切成的段数越多，syscall 越多（§37.4 有 4 KiB / 64 KiB / 1 MiB 三档实测）。

### 一张表把四张图收在一起

| 实现 | CPU 拷贝 | 含 DMA 的总拷贝 | syscall / 1 MiB | 模式切换 / 1 MiB | 前提 |
| --- | --- | --- | --- | --- | --- |
| `read`+`write` | 2 | 4 | 2 | 4 | 无 |
| `mmap`+`write` | 1 | 3 | 1 | 2 | 文件可 mmap；接受缺页 |
| `sendfile` | 0 | 2 | 1 | 2 | 输入是普通文件；NIC 有 SG |
| `splice` | 0 | 2 | 2 | 4 | 一端必须是管道 |

> **口径说明**：这张表里的「拷贝」不含接收端的拷贝。本机回环上，接收进程 `read()` 还要把数据从 skb 拷进自己的缓冲——实测四种实现的接收端 CPU 都在 26–30 ms（§37.4 输出最后一列），**零拷贝只优化发送端**。真实网络里，接收端是另一台机器，这笔账由对端付。

---

## 37.4 ③④ 走一遍 + 改一行：200 MiB 真跑的一张对账表

### 实验装置（为什么这么搭）

要同时满足「可复现」「有 socket」「不依赖网卡」，装置只能长这样：

- **同一台机器、本机回环 TCP**：数据真的经过 socket 发送队列与 skb，只是不走物理网卡；这样四个实现都能跑，且没有外部网络抖动；
- **同一个 200 MiB 文件（209,715,200 字节）、1 MiB 分块**：块足够大，让「每字节成本」压过「每次调用成本」，同时 200 次循环又能让 syscall 计数有意义；
- **先 `cat file > /dev/null` 焐热 page cache**：否则第一个跑的实现要替所有人付磁盘 DMA 的钱（我第一次没焐，`read/write` 那组多读了 200 MiB，CPU 从 104 ms 涨到 137 ms，`inblock` 409600）；
- **发送端与接收端各绑一个核**（`sched_setaffinity`）：2 核机器上不绑核，wall 会被调度器搅成 0.3 s–3.4 s 的随机数（这是实测的抖动范围）；
- **syscall 次数由程序内计数器统计**：本机没有 `strace`、没有 `perf`，所以不假装有；每个 `read/write/sendfile/splice` 调用处 `SC++`，这是精确值，不是估算；
- **CPU 时间用 `getrusage`，缺页用 `ru_minflt`，真实进用户态的字节数用 `/proc/self/io`**（后者有个坑，见 §37.5 ①）。

### 测量骨架（真实代码，去掉参数解析）

```c
struct rusage r0, r1; struct timespec t0, t1;
getrusage(RUSAGE_SELF, &r0);
clock_gettime(CLOCK_MONOTONIC, &t0);
/* ---- 被测循环：下面四种之一 ---- */
send_all(mode, fd, sock, size, chunk);
clock_gettime(CLOCK_MONOTONIC, &t1);
getrusage(RUSAGE_SELF, &r1);

double wall = (t1.tv_sec - t0.tv_sec) + (t1.tv_nsec - t0.tv_nsec) / 1e9;
double cpu  = (r1.ru_utime.tv_sec - r0.ru_utime.tv_sec)
            + (r1.ru_utime.tv_usec - r0.ru_utime.tv_usec) / 1e6
            + (r1.ru_stime.tv_sec - r0.ru_stime.tv_sec)
            + (r1.ru_stime.tv_usec - r0.ru_stime.tv_usec) / 1e6;
printf("wall=%.3fs cpu=%.3fs sc=%ld minflt=%ld\n",
       wall, cpu, SC, r1.ru_minflt - r0.ru_minflt);
```

### ③ 走一遍：四组真实输出

```console
$ ./zc2 rw       big.bin 1024 1024 1
rw       200MiB x1  wall= 0.702s cpu= 0.099s (u=0.000 s=0.099) sc=400  minflt=273
$ ./zc2 mmap     big.bin 1024 1024 1
mmap     200MiB x1  wall= 0.527s cpu= 0.073s (u=0.000 s=0.073) sc=200  minflt=3215
$ ./zc2 sendfile big.bin 1024 1024 1
sendfile 200MiB x1  wall= 0.385s cpu= 0.040s (u=0.000 s=0.040) sc=200  minflt=14
$ ./zc2 splice   big.bin 1024 1024 1
splice   200MiB x1  wall= 0.341s cpu= 0.037s (u=0.000 s=0.037) sc=400  minflt=14
```

上面是 9 次里第 9 次的逐条输出（原样，只删了 `cpu%`、`nvcsw`、吞吐、接收端这四列）。单次运行有 ±30% 的抖动，所以下面这张表用 9 次的统计量：wall 取最快一次（说明没有被别的进程插队），CPU 取中位数（CPU 时间几乎不受排队影响）。

| 实现 | CPU 拷贝 / 总拷贝 | 用户↔内核切换 | syscall 次数 | 耗时（最快） | CPU 时间（中位） | 归一化 |
| --- | --- | --- | --- | --- | --- | --- |
| `read` + `write` | 2 / 4 | 800 | 400 | **0.604 s** | **104 ms** | 100% |
| `mmap` + `write` | 1 / 3 | 400 | 200 | 0.434 s | 76 ms | 73% |
| `sendfile` | 0 / 2 | 400 | 200 | 0.326 s | 40 ms | 38% |
| `splice`（1 MiB pipe） | 0 / 2 | 800 | 400 | **0.324 s** | **39 ms** | 38% |

**逐条读这张表：**

1. **CPU 从 104 ms 掉到 40 ms，是 2.6 倍**，不是 4 倍。理论差是 2 次 CPU 拷贝（约 130 ms），实测只差 64 ms——恰好是**一次** 200 MiB `memcpy` 的钱（本机 61.8–69.9 ms）。差的这一次，一部分被内核在回环路径上的 skb 归属开销吃掉，一部分是因为 `copy_to_user` / `copy_from_user` 的单价与 `malloc` 到 `malloc` 的 `memcpy` 并不相同。**「拷贝次数」是解释方向的模型，不是能拿笔算准的公式——所以要测。**
2. **syscall 400 → 200，模式切换 800 → 400**，两端同时减半；这是 ④ 里最稳定的那一列。
3. **缺页 272 → 14（中位）**（`mmap` 那行的 3214 是它自己的账单），`read/write` 的 272 次来自 1 MiB 用户缓冲的首次触碰（1 MiB / 4 KiB = 256 页，再加十几页启动开销）；
4. **`splice` 的 syscall 是 400 不是 200**：文件到 socket 必须过管道，两步各一次调用。它的 CPU 与 `sendfile` 打平（39 vs 40 ms），说明在 1 MiB 块下，多出来的 200 次 syscall（约 0.2 ms）相比省下的拷贝可以忽略——**但块变小，这个结论就会翻**（见下面管道容量那一段）。
5. **接收端 CPU 四组都是 26–30 ms**：零拷贝没有让接收端变便宜。

### ④ 改一行（真·两列）

把 `read`+`write` 换成 `sendfile`，业务代码只改这几行：

```c
/* 改前：两次拷贝，用户在中间转一手 */
char *buf = malloc(1 << 20);
while ((n = read(fd, buf, 1 << 20)) > 0)
    write(sock, buf, n);

/* 改后：字节不进用户态 */
off_t off = 0;
while (sendfile(sock, fd, &off, 1 << 20) > 0)
    ;
```

| 指标 | 改前 `read`+`write` | 改后 `sendfile` | 变化 |
| --- | --- | --- | --- |
| 每 200 MiB 的 CPU 时间 | 104 ms | **40 ms** | −62% |
| 端到端耗时（最快） | 0.604 s | **0.326 s** | −46% |
| syscall 次数 | 400 | **200** | −50% |
| 模式切换 | 800 | **400** | −50% |
| 进入用户态的字节数（进 + 出） | 200 MiB + 200 MiB | **0** | −100% |
| minor fault | 272 | **14** | −95% |
| 代码行数 | 4 | 3 | −1 |

同一份字节、同一个 socket、同一个接收进程。**唯一的区别是「CPU 有没有当搬运工」**，而这 64 ms 的差价在 10 Gbps 链路上相当于三分之一个核（传完 200 MiB 只要 0.168 s，§37.2 那张表）——这就是「能不能跑满」的差价。

### 第四种改法：把 `write` 换成「加密后再写」，收益当场归零

TLS 不许 `sendfile` 碰数据，所以真实路径一定是「读进用户态 → 加密 → 写回内核」。我用 OpenSSL 的 AES-128-GCM 原样模拟这一步（没有握手、没有证书，只算对称加密的税）：

```c
EVP_EncryptInit_ex(ctx, EVP_aes_128_gcm(), NULL, key, iv);
while ((n = read(fd, buf, 1 << 20)) > 0) {          /* ① page cache → 用户 */
    int ol = 0;
    EVP_EncryptUpdate(ctx, out, &ol, buf, n);        /* ② CPU 看每个字节 */
    write(sock, out, ol);                            /* ③ 用户 → socket */
}
```

| 发送路径 | CPU 时间（中位） | 耗时（最快） | 相对 `sendfile` |
| --- | --- | --- | --- |
| `sendfile`（明文） | 40 ms | 0.326 s | 1.0× |
| `read` + `write`（明文） | 104 ms | 0.604 s | 2.6× |
| `read` + AES-128-GCM + `write` | **158 ms** | 0.578 s | **4.0×** |

**这就是引子里那一幕的微观版本**：158 ms ≈ `read/write` 的 104 ms（两次拷贝 + 400 次 syscall）+ AES-GCM 的 62 ms（3.36 GB/s，200 MiB）。也就是说，零拷贝省下的那 64 ms，被加密的 62 ms 连同原来那两次拷贝一起还了回去——所以它比最早的明文 `read/write` 还贵 52%。

### 第五种改法：只改管道大小，syscall 差 256 倍

`splice` 的块大小受管道容量限制。`fcntl(p[1], F_SETPIPE_SZ, kb << 10)` 一行，三种容量：

| 管道容量 | 每段大小 | syscall 次数 | CPU 时间（中位） | 相对 1 MiB |
| --- | --- | --- | --- | --- |
| 4 KiB（默认下限） | 4 KiB | **102400** | 100 ms | 2.6× |
| 64 KiB（系统默认） | 64 KiB | 6400 | 45 ms | 1.15× |
| 1 MiB（上限） | 1 MiB | 400 | 39 ms | 1.0× |

102400 次 syscall 比 400 次多花 61 ms，折合**每多一次 syscall 约 0.6 µs**——和 §37.2 那个 `t_syscall` 对得上。注意 4 KiB 那组的 CPU 甚至比 `read/write`（104 ms）只低一点点：**管道太小，`splice` 就退化成一个「syscall 更多、拷贝一样少」的版本**，省下的拷贝被调用开销吃光。

### 第六种改法：文件小到 16 KiB，收益从 2.6× 缩到 1.7×

同一套代码，把文件换成 16 KiB、每个连接连发 4000 次（共 64 MiB），模拟小对象服务：

| 实现 | syscall 次数 | CPU 时间 | 每请求 CPU | 相对 `read/write` |
| --- | --- | --- | --- | --- |
| `read` + `write` | 8000 | 31 ms | 7.8 µs | 1.0× |
| `mmap` + `write` | 4000 | 59 ms | 14.8 µs | **0.5×（更慢）** |
| `sendfile` | 4000 | 18 ms | 4.5 µs | 1.7× |
| `splice` | 8000 | 35 ms | 8.8 µs | 0.9× |

小文件上 **`sendfile` 仍然最快，但优势从 2.6× 缩到 1.7×，绝对值只有 3.3 µs/请求**；`mmap` 因为每请求一次 `mmap`/`munmap` + 缺页（4015 次），**直接比 `read/write` 慢一倍**；`splice` 因为固定 2 次 syscall，基本没有优势。这 3.3 µs 放到一次 TLS 握手里（本机 ECDHE P-256 单次约 55 µs，还不含证书验证与网络往返）就是噪声——**这就是「小文件上零拷贝没用」的正确说法：不是变慢，是收益掉进了固定成本的噪声里。**

---

## 37.5 ⑤ 它证伪了谁：四句话，四组数字

### ① 「零拷贝 = 不拷贝」

不对。它省的是 **CPU 拷贝**，DMA 一次都省不掉：

- 200 MiB 从磁盘进 page cache 是 DMA，从 socket 队列进网卡也是 DMA；`sendfile` 只是让 CPU 不再参与中间那两次；
- 本机 `memcpy` 200 MiB 要 61.8–69.9 ms，这就是「一次 CPU 拷贝」的定价；`sendfile` 把 2 次变成 0 次，所以省下的是这个量级，而不是「数据凭空消失」；
- 更狠的一条：网卡没有 SG-DMA 时，内核会把 page cache 的页**线性化**进 skb——**零拷贝在你看不见的地方悄悄退化成一次拷贝**，`strace` 和 `perf` 都未必告诉你。

还有一个常被拿来当证据的计数器也不可靠：`/proc/self/io` 的 `rchar` 在 `read/write` 下是 200 MiB，在 `sendfile` 下**也是 200 MiB**（内核在 `splice_direct_to_actor` 里照样记账），只有 `splice` 是 0。**拿 `rchar` 证明「数据没进用户态」会得出错误结论。**

### ② 「`sendfile` 一定最快」

三处反例，前两处是硬反例：

| 场景 | 实测 | 为什么 |
| --- | --- | --- |
| 要改内容（压缩 / 注入头 / 水印） | `sendfile` 用不了，必须回到用户态 | 数据必须被 CPU 看 |
| 要加密（TLS 未开 kTLS） | 158 ms vs 40 ms（§37.4） | 同上，且多付一次 AES |
| 16 KiB 小文件 | 4.5 µs vs 7.8 µs（§37.4） | 仍快 1.7×，但绝对值只有 3.3 µs |

第二行值得单独说：**「TLS 下 `sendfile` 无效」不是因为 `sendfile` 有 bug，而是因为它承诺「不碰内容」，而 TLS 的第一条要求就是「碰每一个字节」。** 两者在语义上互斥，只能靠把加密下沉到内核（kTLS）来调和。

### ③ 「`mmap` 一定好」

`mmap` 把「拷贝」换成了「缺页」，这笔交易只有在**同一份数据被反复读**时才划算：

- 200 MiB 顺序发一遍：3214 次 minor fault，CPU 76 ms，仍然输给 `sendfile` 的 40 ms；
- 16 KiB × 4000 请求：4015 次缺页 + 4000 次 `mmap`/`munmap`，CPU 59 ms，**比 `read/write` 慢一倍**；
- 还有三个不体现在数字里的坑：**`SIGBUS`**（文件被 truncate 后访问越界页，进程直接收到信号，不是 `EINVAL`）、**TLB 压力**（200 MiB / 4 KiB = 51200 个页表项）、**地址空间碎片**（32 位进程尤其明显）。

`mmap` 的正确用法是「一份文件映射一次、读很多次」（比如索引文件、共享内存），不是「一个请求映射一次、顺序发一遍」。

### ④ 「HTTPS 也能零拷贝」

默认不能。TLS 的记录层（最大 16 KiB 一条）必须在**用户态**完成加密，`sendfile` 拿不到明文也交不出密文：

- 实测：同样 200 MiB，`sendfile` 40 ms，`read`+AES-GCM+`write` 158 ms；
- 引子里那台机器上，`%sy` 从 34% 涨到 78%——内核时间没降，用户态时间还涨了；
- 唯一的结构性出路是 **kTLS**：`setsockopt(SOL_TLS, TLS_TX, ...)` 把对称加密下沉到内核，`sendfile` 把 page cache 的页交给内核 TLS 层加密后再挂 skb。此时「零拷贝」重新成立，但**握手、证书、密钥交换仍然在用户态**，且只有对称部分被卸载（细节见 `base/47`）。

所以「HTTPS + sendfile」的正确问法不是「为什么没变快」，而是「**加密在哪一层做**」。在用户态做，就必然有两次拷贝；在内核做，才有资格谈零拷贝。

---

## 37.6 边界：什么时候不能用，以及用了也没用

| 场景 | 零拷贝为什么失效 | 该走哪条路 |
| --- | --- | --- |
| 要修改内容（gzip、注入 header、转码、水印） | 数据必须进 CPU，`sendfile`/`splice` 语义上不允许 | 用户态处理 + `write`；或 `MSG_ZEROCOPY`（省的是最后一次拷贝） |
| 要加密（TLS 默认路径） | 每个字节都要被加密，且记录层在用户态 | kTLS（`TLS_TX`/`TLS_RX`）+ `sendfile`；或接受两次拷贝 |
| 要签名 / 校验和（每次发送都算） | 同「要修改内容」 | 预计算 + 缓存，别在发送路径上算 |
| 文件很小（< 64 KiB） | 收益是 µs 级，被 syscall / 握手 / 打开文件吃掉 | 用户态缓存整份响应（`writev`），别每请求 `mmap` |
| 需要随机读、多次不同偏移 | 每次都要一次 syscall；`sendfile` 的 `off` 会前进，复用要重置 | `pread` 批量读，或 `mmap` + 一次映射多次读 |
| 源不是普通文件（管道、字符设备、`O_DIRECT`） | `sendfile` 要求可 mmap 的输入 | `splice`（一端是管道）或普通 `read/write` |
| 目标是加密设备 / 压缩文件系统 | 底层仍要解压/加密，零拷贝在文件系统层被拆掉 | 先确认存储层是否透明压缩（btrfs/zfs） |
| 超大文件（> 2 GiB） | 单次 `sendfile` 的 `count` 有上限 | 循环推进 `off`，每次 ≤ 2 GiB |
| 内存紧张、长肥管道 | 页被 pin 在 skb / pipe 里，page cache 无法回收 | 限制在途字节数（应用层背压） |
| 需要 NAT / 内容感知中间件 | 中间件要读 payload | 中间件侧用 `splice` 做转发，别解包 |

两条容易被忽略的边界，单独说：

**边界一：`sendfile` 不是「零 syscall」。** 200 MiB 顺序发只要 200 次调用（1 MiB 一块），但每请求一个小文件就是每请求 1 次。syscall 单价 0.5–1 µs，在 10 万 QPS 下就是 **5%–10% 的一个核**。要再往下压，只能上 `io_uring`（批量提交、共享环，见 `base/36`）或用户态协议栈。

**边界二：零拷贝把「错误处理」变复杂了。** `sendfile` 返回的是「已经写进 socket 的字节数」，不是「对端收到了」；`splice` 的页引用在管道里，源文件被截断时行为由内核保证但语义更绕；`MSG_ZEROCOPY` 要求你等「发送完成通知」（错误队列）才能复用缓冲区，否则会踩到「用户改了、网卡还在读」。**省下的 CPU，一部分要用复杂度来还。**

---

## 37.7 与真实系统连接：四个 API 在工程里长成什么样

### Nginx：`sendfile on` 是最便宜的一行配置

```nginx
location /static/ {
    sendfile on;          # 明文静态文件：走 sendfile
    tcp_nopush on;        # 攒满一个 MSS 再发，配合 sendfile 减少小包
    aio threads;          # 磁盘读慢时把阻塞读丢给线程池
}
```

- 明文静态文件：`sendfile` 直接生效，这就是引子里 QPS 翻倍的那一行；
- HTTPS：默认走 OpenSSL 的 BIO，`sendfile` 被架空；开了 kTLS 才有 `ssl_sendfile` 路径（Nginx 1.21.4+，需要内核与 OpenSSL 同时支持）；
- **一行配置能翻倍，也能在 TLS 下变成一行安慰剂**——差别不在 Nginx，在数据要不要被看。

### Kafka：零拷贝是它能用很小堆扛住大吞吐的原因之一

Kafka 的消费路径是「从 log 段文件读 → 发给 consumer」。它没有把消息读进 JVM 堆再写出去，而是让 `FileRecords.writeTo()` 走到 `FileChannel.transferTo()`，最终落到 `sendfile`：

- 消息写在 page cache 里（producer 刚写完），consumer 立刻来取时**根本不用碰磁盘**；
- broker 的堆里没有这份数据的副本，所以 GC 压力与堆大小解耦——Kafka 的堆可以远小于它每秒搬运的字节数；
- 但一旦开了 SSL，数据必须走 SSL 通道逐块加密写出，零拷贝自动退化成用户态拷贝。**同一个 broker，明文与 TLS 的 CPU 曲线是两条线**，和 §37.5 ④ 是同一件事。

### io_uring：`IORING_OP_SPLICE` 把「过管道」也变成一次提交

`sendfile` 每次调用仍是一次 `syscall` 指令；`io_uring` 把请求写进共享环，内核侧批量消费：

```c
/* 概念示意：SQE 提交，不陷入内核；完成事件从 CQE 取 */
io_uring_prep_splice(sqe, fd_in, off_in, pipe_w, NULL, len, 0);
io_uring_prep_splice(sqe, pipe_r, NULL, sock, NULL, len, 0);
io_uring_submit(ring);
```

- 200 MiB 的 400 次 `splice` 可以变成一个批次；在 `base/36` 那张账里，这是「syscall 10⁶ → 10³」的同一条推论；
- 6.x 还有 `IORING_OP_SEND_ZC`（零拷贝发送）与 `MSG_ZEROCOPY` 的用户态版本，省掉「用户缓冲 → socket」这最后一次拷贝，代价是要处理完成通知。

### gRPC / HTTP/2：TLS 栈在用户态，所以帧必须先落在内存里

HTTP/2 的 DATA 帧最大 16 KiB，每条消息要经过「序列化 → 分帧 → HPACK（头部）→ TLS 记录层加密」。这条链上每一步都要看内容，`sendfile` 从第一步就用不了：

- gRPC 的零拷贝话题实际落在「**避免多余的 `memcpy`**」上：`ByteBuffer`/`slice` 的零拷贝切片、`Netty` 的 `CompositeByteBuf`、Go 的 `net.Buffers`（`writev`）；
- 真正要零拷贝，得让 TLS 下沉（kTLS）或让应用直接管缓冲（`MSG_ZEROCOPY`、`AF_XDP`）；
- 所以「HTTP/2 比 HTTP/1.1 快」从来不靠零拷贝，靠的是多路复用（见 `base/46`）。

---

## 37.8 五问收口（实例篇统一结构）

| 五问 | 本篇的答案 |
| --- | --- |
| **① 它是什么** | 「零拷贝」是一族把「文件 → 网卡」路径上的**用户态中转站**删掉的 API。它删的是 **CPU 拷贝**与随之而来的模式切换，不删 DMA，也不删 syscall 本身 |
| **② 物理形态** | `read/write`：4 次拷贝（2 次 CPU）、4 次切换；`mmap`：3 次拷贝（1 次 CPU）+ 每 64 KiB 一次缺页；`sendfile`：2 次 DMA、0 次 CPU 拷贝，页引用挂进 skb；`splice`：2 次 DMA、0 次 CPU 拷贝，但必须过一根 64 KiB–1 MiB 的管道。尺寸：page 4 KiB、TLS record 16 KiB、`sendfile` 单次上限约 2 GiB、`pipe-max-size` 1 MiB |
| **③ 走一遍** | 200 MiB、1 MiB 块、本机回环：`read/write` 104 ms CPU / 400 syscall / 800 切换；`mmap` 76 ms / 200 / 400 + 3214 缺页；`sendfile` 40 ms / 200 / 400；`splice` 39 ms / 400 / 800。接收端四组都是 ~28 ms——零拷贝只优化发送端 |
| **④ 改一行** | `while(read) write` → `while(sendfile)`：CPU 104 → 40 ms（−62%），耗时 0.604 → 0.326 s（−46%），syscall 400 → 200，进用户态字节 400 MiB → 0。**反向的一行**：`write` → `EVP_EncryptUpdate` + `write`，CPU 40 → 158 ms（4.0×） |
| **⑤ 它证伪了谁** | ①「零拷贝 = 不拷贝」——DMA 仍在，无 SG 时还会偷偷补一次；②「`sendfile` 一定最快」——要改内容/要加密时用不了，16 KiB 时优势只剩 3.3 µs；③「`mmap` 一定好」——缺页与 `SIGBUS` 让它在小文件上比 `read/write` 还慢一倍；④「HTTPS 也能零拷贝」——TLS 必须让 CPU 看每个字节，除非 kTLS |

---

## 37.9 一页速记

```
【一句话】
零拷贝省的是「CPU 当搬运工」的那几次 memcpy，不是数据流动本身。
数据要不要被 CPU 看，决定你能不能零拷贝；看多少，决定你省多少。

【四个数字（本机 2 核，200 MiB，1 MiB 块）】
  read/write   CPU 104 ms   syscall 400   切换 800   缺页 272
  mmap+write   CPU  76 ms   syscall 200   切换 400   缺页 3214
  sendfile     CPU  40 ms   syscall 200   切换 400   缺页 14
  splice(1MiB) CPU  39 ms   syscall 400   切换 800   缺页 14

【三个单价（同样是量出来的）】
  一次 CPU 拷贝 200 MiB   ≈ 65 ms      （memcpy 2.8–3.2 GiB/s）
  一次 syscall            ≈ 0.6–1 µs
  一次 TLS 对称加密 200MiB ≈ 62 ms      （AES-128-GCM 3.36 GB/s）

【一条公式】
  T ≈ N_syscall × 0.6µs + N_copy × 65ms/200MiB （+ 模式切换 0.1µs × N，通常可忽略）
  1 MiB 块：拷贝是主项；4 KiB 块：syscall 是主项

【什么时候别用】
  要改内容（gzip/注入头/水印）｜ 要加密（TLS，除非 kTLS）
  小文件（16 KiB 时收益只剩 3.3 µs/请求）｜ 随机读、多次不同偏移
  mmap 的正确场景是「映一次读很多次」，不是「一请求映一次」

【记错的三个地方】
  rchar 不能证明「没进用户态」：sendfile 也把它记成 200 MiB
  缺页不是免费的：mmap 在 16 KiB×4000 请求下比 read/write 慢一倍
  零拷贝只优化发送端：接收端那一次拷贝永远在
```

---

## 37.10 自检 3 题

**Q1.** 一台单核机器，1 Gbps 网卡，文件全在 page cache 里，`read/write` 用 16 KiB 缓冲。请估算这条路径需要多少内存带宽；换成 `sendfile` 后呢？如果换成 40 Gbps 网卡，`read/write` 还跑得动吗？

<details><summary>答案</summary>

1 Gbps = 125 MB/s。`read/write` 每个字节要被 CPU 搬两次（page cache → 用户、用户 → socket），再加上两次 DMA（磁盘/网卡），内存总流量约为 **4 × 125 MB/s = 500 MB/s**；其中 **250 MB/s 是 CPU 驱动的拷贝**（两次 CPU 拷贝各 125 MB/s）。

`sendfile` 只剩两次 DMA，CPU 不再搬字节：**250 MB/s**，CPU 拷贝为 0。

40 Gbps = 5 GB/s。`read/write` 的 CPU 拷贝需要 `2 × 5 = 10 GB/s` 的拷贝吞吐，而本机 `memcpy` 实测只有 **3.1 GiB/s**（≈3.3 GB/s），**单核连一次拷贝都跑不满 40 Gbps**，更不用说两次。所以 40 Gbps 上 `read/write` 不是「慢」，是「物理上不可能」——这正是 §37.2 那张表第三行的来源。

补充：即使 CPU 够快，`read/write` 还要为每 16 KiB 付 2 次 syscall；5 GB/s ÷ 16 KiB = 305175 次/s，按 0.6 µs 算就是 0.18 个核的纯调用开销。

</details>

**Q2.** 一个 HTTPS 静态服务加了 `sendfile on`，CPU 没降。给你三条命令，判断它到底有没有走零拷贝、瓶颈在哪一层。

<details><summary>答案</summary>

1. **看 syscall 形态**：`strace -c -e trace=sendfile,read,write,recvfrom,sendto -p <pid>`。
   如果 `sendfile` 的调用次数是 0 或极少、而 `read`/`write` 次数与请求数同阶，说明 TLS 库把数据拉回用户态了（OpenSSL 的 BIO 路径），`sendfile` 已被架空。
2. **看内核有没有接管对称加密**：`cat /proc/net/tls_stat`。
   `TlsTxSw` / `TlsRxSw` 为 0 说明没走 kTLS；非 0 才说明加密在内核里做，`sendfile` 有资格生效。
3. **看 CPU 花在哪**：`perf top -p <pid>`（没有 perf 就用 `cat /proc/<pid>/stat` 看 `utime/stime` 比例，再配合 `/proc/<pid>/status` 的 `voluntary_ctxt_switches`）。
   如果 `%sy` 高、`aesni_*` / `copy_user_enhanced_fast_string` 排在前列，说明钱花在拷贝与加密上，不在 syscall 数量上。

结论模板：**`sendfile` 生效但被 TLS 架空 → 上 kTLS；`sendfile` 没被调用 → 检查是不是走了代理/压缩/改写；两者都正常但 CPU 仍高 → 瓶颈在加密或接收端，不在发送路径。**

</details>

**Q3.** 一个 10 万 QPS 的服务，每个响应 4 KiB，全部来自本地文件。你会用 `sendfile` 还是 `splice`？如果目标是再省掉每请求的那一次 syscall，下一步做什么？

<details><summary>答案</summary>

**选 `sendfile`。** 两个理由，都是本篇量出来的：

1. **syscall 结构**：4 KiB 的响应，`splice` 必须「file → pipe → socket」两次调用，`sendfile` 只需一次。10 万 QPS 下这是 20 万 vs 10 万次 syscall，按 0.6 µs 算差 **60 ms/s ≈ 6% 的一个核**；
2. **管道容量帮不上忙**：管道再大，单个响应也只有 4 KiB，一次 `splice` 就搬完了，容量参数在这条路径上是无效的（它只在「一次发送远大于管道容量」时才有用，见 §37.4 的 4 KiB/64 KiB/1 MiB 表）。

**下一步**：上 `io_uring`，把「打开/读/发/关」串成一批 SQE，用共享环消掉大部分 `syscall` 指令；或者用 `IORING_OP_SEND_ZC` 省掉最后一次用户态拷贝。若响应体可以被缓存，直接在用户态 `writev`（甚至 `SO_ZEROCOPY`）往往比走文件系统更快——**4 KiB 的瓶颈从来不是拷贝，是每请求的固定开销**。

</details>

---

## 37.11 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 一次系统调用的入口、`syscall` 指令与模式切换成本 | `base/21` |
| page cache、inode、`write` 成功 ≠ 落盘 | `base/28`、`base/29` |
| DMA、总线、中断：为什么「搬数据」是最贵的资源 | `base/14` |
| socket 收发六道门、skb、软中断与背压 | `base/32` |
| epoll 与 io_uring 的 syscall 账（本篇的姊妹实验） | `base/36` |
| 文件系统与存储器件：为什么冷缓存下四种实现一样慢 | `base/08`、`base/28` |
| TLS 记录层、证书链与 kTLS 的位置 | `base/47` |
| HTTP/2 帧与多路复用（为什么它不是零拷贝） | `base/46` |
| Go 的 `net.Buffers`、`io.Copy` 与 `sendfile` 的自动降级；高并发连接账 | [`go/32`](../go/32-high-concurrency.md) |
| 全书总纲与本篇在 Part III 的位置 | [`0-summary.md`](0-summary.md) §0.4、§0.6.5 |
