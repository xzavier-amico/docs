# 21. 内核与两种态：一次系统调用的全过程

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 操作系统：Linux 视角 ｜ **编号**：21 / 56
> **本篇定位**：Part III 的第一篇。15 讲清了「隔离的物理实体是特权级 + 页表」，本篇把那条结论压到一次具体的 `write(2)` 上：**同一颗 CPU 怎么从 ring3 走到 ring0、又怎么回来，每一步各花多少钱**。全篇只有一条推论链——`内核是另一个特权级里的函数集合` → `每次进入都要换栈、换 GS、查表、校验、拷贝` → `这套动作有固定的几十到几百纳秒单价` → `所以内核 API 只能长成批处理、映射、共享环形队列三种形态`。22–27 接着讲内核替你维护的簿记（进程、线程、内存、文件），31–32 讲 IO 与网络怎么用满这条路径，◆36 用同一套 syscall 账对比 epoll 与 io_uring。
> **读法**：§21.3 的十步旅程是全篇支点，建议对照 `objdump -d` 与自己机器上的 `/proc/kallsyms` 走一遍；§21.4 的分层成本表与 §21.7 的三种 API 形态是同一条结论的两面，赶时间只读这两节；§21.8 的四个实验十分钟内可重跑，环境与噪声都标注在原处。
> **不在本篇**：`fork`/`execve` 怎么造出进程与地址空间 → `base/22`、`base/23`；页表结构与缺页处理 → `base/26`；页缓存回写与 `fsync` 到底保证什么 → `base/29`；epoll/io_uring 的完整接口与就绪语义 → `base/31`；socket 收发的六道门 → `base/32`；seccomp、ASLR、提权与逃逸 → `base/33`；零拷贝四种实现的横向对比 → `base/37`。

---

## 21.1 引子：日志行数没变，吞吐掉了 17 倍

某日志采集 sidecar 做了一次「提高实时性」的重构：原来用 C 标准库的全缓冲 `fwrite`，攒满 4 KiB 才落一次 `write(2)`；重构后改成「来一行就写一行」，理由是「这样进程崩了也不丢日志」。上线后行数、字节数、写入顺序、磁盘都没变，单实例吞吐从 38 万行/秒掉到 2.2 万行/秒。

把这段逻辑单独拿出来，在同一台机器上跑 100 万行、每行 64 字节（合计 64 MiB）：

| 写法 | 100 万行 · 64 MiB 的耗时 | 吞吐 | 相对 |
| --- | --- | --- | --- |
| 每行一次 `write(2)` | 2421.71 ms | 26.4 MiB/s | 1.0× |
| 4 KiB 用户态缓冲 | 167.90 ms | 381.2 MiB/s | 14.4× |
| 64 KiB 用户态缓冲 | 139.74 ms | 458.0 MiB/s | 17.3× |

代码上的差别只有一个缓冲区：

```c
write(fd, line, 64);                       /* 每行一次：100 万次跨界 */

memcpy(buf + used, line, 64); used += 64;  /* 攒批：16384 次跨界 */
if (used == 4096) { write(fd, buf, used); used = 0; }
```

不是日志库慢，也不是磁盘慢：数据只进了页缓存，`iowait` 几乎为零，涨上去的是 `%sys`；`strace -c -e trace=write` 里 `write` 调用数从 16,384 变成 1,000,000，其他什么都没变。

另一组对照把「同样的字节数」压到「只差 syscall 次数」一个变量上：100 万字节用 100 万次 1 字节 `write` 写要 **1630 ms**，而一次 `write` 写 1 MiB（多 4.8% 数据）只要 **0.44 ms**——差 **3700 倍**。

两个数字指向同一件事：**一次系统调用的成本里，绝大部分与数据量无关**。每次 `write`，CPU 都要从 ring3 切到 ring0、换内核栈、保存寄存器、查表、把用户缓冲区拷进内核、记账、再切回来；写 1 字节这套动作一步不少，写 1 MiB 也只做一次。

所以本篇只回答一个问题：**内核不是「更底层的库」，而是另一个特权级里的一套代码；每次跨界都有固定成本，这决定了 API 为什么长成批处理 / `mmap` / `io_uring`。**

---

## 21.2 内核是什么：不是库，不是进程，没有 main

先把三个最常见的误解拆掉。它们各自对应一个物理事实，而不是三个说法。

### 不是「更底层的库」

库（`libc`、`libssl`、你自己写的 `.so`）与你的代码在**同一个特权级、同一套页表、同一个地址空间**里。调用库函数就是一条 `call`，返回就是一条 `ret`，指针可以直接解引用。它的成本是纳秒级：`call` + `ret` 在 L1 命中时约 1–3 ns（本机空循环实测 2.25 ns/迭代，含一次 load-add-store）。

内核不是这个形状。它运行在 **ring0**、用**内核栈**、访问用户指针要经过专门的拷贝函数、进入它靠一条特权指令而不是 `call`：

| 维度 | 用户态库（libc） | 内核 |
| --- | --- | --- |
| 运行权限 | ring3 | ring0 |
| 地址空间 | 与你同一个 | 高半部分映射（开启 PTI 时是另一套页表） |
| 进入方式 | `call` / `ret` | `syscall` / `int` / 中断（换栈、换 GS） |
| 指针可信度 | 你自己负责 | 一律不可信，必须校验并拷贝 |
| 出错方式 | 返回 -1 + `errno` | 返回 `-errno`，必要时给你发信号 |
| 能否睡眠、被调度 | 由你自己的线程决定 | 进程上下文可以，中断上下文绝对不行 |
| 单次成本 | 1–3 ns | 0.3–1.5 µs（本机实测，见 §21.4） |

最后一行是全篇的题眼：**边界不是免费的，而且价格与「你搬了多少数据」几乎无关。**

### 不是进程，也没有 main

内核里有 `start_kernel()`（`init/main.c`），但它只在开机那个 CPU 上跑一次：初始化内存、中断、调度器，最后把控制权交给调度器，自己变成 idle 线程。从那以后，内核里**没有主循环、没有 `main`、没有「内核进程」在等你调用**。每一段内核代码都由三类事件之一驱动：

| 驱动源 | 例子 | 运行上下文 | 能否睡眠 | 用谁的栈 |
| --- | --- | --- | --- | --- |
| 系统调用 | `write`、`read`、`mmap` | 进程上下文（process context） | 能 | 当前进程的内核栈 |
| 中断 / 异常 | 网卡中断、缺页、除零 | 中断上下文（interrupt context） | 不能 | 当前 CPU 的栈（不属于某个进程） |
| 内核线程 | `kswapd`、`kworker`、io_uring 的 SQPOLL | 进程上下文，但 `mm == NULL` | 能 | 自己的内核栈 |

关键在第二、四列：**系统调用是「内核借用你的进程在跑」**。它用你的内核栈、`cred`、fd 表；`current` 宏（x86-64 上经 GS 基址取每 CPU 的 `current_task`）指向你的 `task_struct`，所以同一段内核代码替你干活时记在你的 `sys` 时间里，替内核线程干活时记在 `kworker` 头上。中断连「替谁干活」都说不清——它打断的是恰好正在跑的那条指令。

### 两种态在地址空间里长什么样

x86-64 的 4 级页表把 48 位虚拟地址劈成两半：低半部分用户空间（128 TiB），高半部分内核空间（128 TiB），中间是不可用的非规范地址洞。

```
        用户态（ring3）                          内核态（ring0）
 0x0000_7fff_ffff_ffff ┌───────────────┐   0xffff_ffff_ffff_ffff ┌───────────────┐
                       │ 你的代码/栈/堆 │                         │ 内核代码        │
                       │ [vdso] [vvar]  │   ─── syscall ───►      │ 内核数据        │
                       │ fd 表、mm      │   ◄── sysret ────       │ 每 CPU 数据     │
 0x0000_0000_0000_0000 └───────────────┘   0xffff_8000_0000_0000 └───────────────┘
        ▲ 未开 PTI 时，两半在同一套页表里        ▲ 高半部分页表项 U/S=0：
        ▲ 开 PTI 时，用户页表的高半只剩跳板      ▲ ring3 一碰就是 #PF
        └── 进入内核时 RSP 从用户栈切到内核栈（TSS / 每 CPU top_of_stack）
```

三个细节决定了后面的所有成本：

1. **内核代码一直在你的地址空间里**（未开 PTI 时）。它不是「另一个程序」，而是同一套页表里被标成「只有 ring0 能访问」的那一半；ring3 访问它，MMU 直接报页错误，不靠软件检查。
2. **硬件不换栈**。`syscall` 只改权限、改 RIP，RSP 仍是你的用户栈。内核必须在入口第一条指令起就抢在「用用户栈」之前把栈换掉。
3. **内核栈只有 16 KiB**（x86-64 上 4 个页）。一次系统调用保存的 `pt_regs` 是 168 字节（21 个 8 字节字段），约占 1%。所以内核禁止深递归、禁止在栈上放大数组；`-Wframe-larger-than=1024` 是硬约束。

### 门：硬件怎么知道该跳去哪

内核启动时把入口地址写进 `MSR_LSTAR`（0xC0000082）、把内核/用户 CS/SS 写进 `MSR_STAR`、把要屏蔽的 RFLAGS 位写进 `MSR_FMASK`；`syscall` 那一刻硬件照着做。本机 `/proc/kallsyms` 里能直接看到这条链路的符号（`T` = 代码，`D` = 数据）：

```
ffffffff82600080 T entry_SYSCALL_64
ffffffff82488ed0 T do_syscall_64
ffffffff828003c0 D sys_call_table
ffffffff81c43de0 T __x64_sys_write
ffffffff81c43ce0 T ksys_write
ffffffff81f27750 T _copy_from_user
```

`entry_SYSCALL_64` 是入口汇编，`do_syscall_64` 是 C 派发器，`sys_call_table` 是函数指针表，`__x64_sys_write → ksys_write → _copy_from_user` 是 `write` 的内核侧链路。§21.3 沿这六行符号走一遍。

---

## 21.3 一次 `write(1, "hi\n", 3)` 的完整旅程

### 先看用户态发出去的是什么

把 `write` 写成内联汇编，`gcc -O2 -c` 后反汇编，只有五条有效指令：

```asm
0000000000000000 <main>:
   0:	b8 01 00 00 00       	mov    $0x1,%eax        # rax = __NR_write = 1
   5:	48 8d 35 00 00 00 00 	lea    0x0(%rip),%rsi    # rsi = "hi\n" 的地址
   c:	ba 03 00 00 00       	mov    $0x3,%edx        # rdx = 3
  11:	48 89 c7             	mov    %rax,%rdi        # rdi = 1（stdout）
  14:	0f 05                	syscall
  16:	31 c0                	xor    %eax,%eax
  18:	c3                   	ret
```

参数约定是硬性的：**系统调用号放 `rax`，参数依次放 `rdi、rsi、rdx、r10、r8、r9`**。第 4 个参数是 `r10` 而不是 `rcx`，因为 `syscall` 自己要用 `rcx` 存返回地址、用 `r11` 存 RFLAGS——glibc 的 `syscall()` 包装会在发指令前把第 4 个参数从 `rcx` 挪到 `r10`。这不是风格，是硬件占用了那两个寄存器。

### 十步旅程

| # | 发生什么 | 谁在做 | cycle 量级 | ns @2.595 GHz |
| --- | --- | --- | --- | --- |
| 1 | 参数落寄存器，`rsp` 仍指向用户栈 | 用户态 / glibc | 5–15 | 2–6 |
| 2 | `syscall`：RCX←下一条指令，R11←RFLAGS，CS/SS←STAR，RIP←LSTAR，按 FMASK 清标志 | CPU 硬件 | 30–60 | 12–23 |
| 3 | 入口汇编：`swapgs` 拿 per-CPU 区，存用户 RSP，换内核栈，压 15 个通用寄存器凑成 168 B 的 `pt_regs` | 内核入口 | 80–150 | 31–58 |
| 4 | 派发：检查 `nr < NR_syscalls`，`array_index_nospec` 挡越界推测，经 retpoline 调 `sys_call_table[1]` | `do_syscall_64` | 50–100 | 19–39 |
| 5 | 服务函数：`__x64_sys_write → ksys_write → vfs_write → write_iter`；`fdget`、inode 锁、页缓存查找 | 内核 / VFS / 文件系统 | 100–800 | 39–308 |
| 6 | 拷贝：`access_ok` → `stac` → `rep movsb` → `clac`；中途缺页由异常表接住 | `copy_from_user` | 30–80 + 数据 | 12–31 + 数据 |
| 7 | 记账：标脏页、更新 mtime/ctime、可能要 `balance_dirty_pages` 触发回写 | 内核 / 文件系统 | 200–800 | 77–308 |
| 8 | 返回：`rax = 3`；`syscall_exit_to_user_mode` 检查 `TIF_NEED_RESCHED`、待处理信号、单步标志 | 内核出口 | 40–80 | 15–31 |
| 9 | 出口汇编：`swapgs`，`sysretq`（RIP←RCX、RFLAGS←R11） | 内核出口 | 40–80 | 15–31 |
| 10 | glibc 检查 `rax ∈ [-4095, -1]`：是则 `errno = -rax` 返回 -1，否则返回 `rax` | 用户态 / glibc | ~5 | ~2 |

第 1–4 步加第 8–10 步，按表算是 250–490 cycle；再加上 §21.4 的缓解开销与「用户态/内核态各冷一次」的缓存代价，合计 **400–800 cycle**，对应实测空系统调用 **298 ns（773 cycle）**。第 5–7 步是「写到文件」比「问一句 getpid」多出来的约 3400 cycle。同一套入口/出口服务所有系统调用，**差异从第 5 步才开始**——这就是「固定成本」的准确含义。

### 第二步：`syscall` 到底改了什么

它在硬件里做五件事：`RCX ←` 下一条指令地址（返回地址放寄存器，不压栈）；`R11 ←` RFLAGS；CS/SS 从 `MSR_STAR` 换成内核选择子（CPL 3→0）；`RIP ← MSR_LSTAR`；RFLAGS 按 `MSR_FMASK` 清位（清 IF 关中断、清 DF、清 AC 关掉 SMAP 旁路）。

**它不换栈，也不保存通用寄存器。** 这两件事由内核入口自己做，而「用哪只栈」正是攻击面：若内核先用用户 RSP 压栈，用户就能把栈指到内核想写的地方。

### 第三步：入口汇编的谨慎

入口要解决三件事：找到当前 CPU 的数据、找到当前进程的内核栈、保存现场。

- `swapgs`：用户 GS 指向 TLS，内核 GS 指向 per-CPU 区；一条指令交换，之后靠 GS 相对寻址拿 `current`、`top_of_stack`。
- 换栈：用户 RSP 存进 TSS（或每 CPU 变量），再从 `top_of_stack` 取内核栈顶；开 PTI 的机器先进「跳板栈」，切完 CR3 再换真正的内核栈。
- `PUSH_AND_CLEAR_REGS`：15 个通用寄存器压成 `pt_regs`（168 字节），同时清零——一半防内核数据顺着寄存器漏回用户态，一半压住 Spectre v1 的推测窗口。

`pt_regs` 的布局是 ABI 的一部分：`strace`、信号处理、`ptrace` 读到的寄存器都是这个结构。

### 第四步：从表里取函数

`sys_call_table` 是定长函数指针数组，下标就是系统调用号。派发器只做三件事：

```c
nr = syscall_enter_from_user_mode(regs, nr);
if (likely(nr < NR_syscalls)) {
    nr = array_index_nospec(nr, NR_syscalls);   /* 挡住越界推测执行 */
    regs->ax = sys_call_table[nr](regs);        /* 间接调用，retpoline 保护 */
}
syscall_exit_to_user_mode(regs);
```

`array_index_nospec` 挡住 Spectre v1 的越界推测；`sys_call_table[nr](regs)` 是由用户间接控制的间接调用，所以套着 retpoline（或 IBRS）——这笔开销每个 syscall 都付。

### 第五、六步：`ksys_write` 与那次拷贝

`__x64_sys_write` 取出 `pt_regs` 里的参数，`ksys_write` 做真正的文件写：`fdget` 查当前进程的 fd 表 → `vfs_write` → `file->f_op->write_iter`。之后分叉：普通文件进 ext4/xfs，管道进 `pipe_write`，socket 进 `sock_write_iter`，终端进 tty 驱动。**第 5–7 步的成本由「写到哪」决定，第 1–4、8–10 步与「写到哪」无关。**

对普通文件，数据要先进入页缓存，于是有一次 `copy_from_user`：`access_ok` 查范围、`stac/clac` 临时打开 SMAP 窗口、`rep movsb` 搬数据。这次拷贝是**必须**的，理由在 §21.5。

### 第八、九步：返回，以及 `sysret` 的坑

`rax` 放返回值。约定是 **`-4095` 到 `-1` 表示 `-errno`**，其余是正常值——这就是 `mmap` 失败返回 `(void*)-1` 而不是 `NULL` 的原因，也是内核绝不能把 `-4095..-1` 当合法指针返回的原因。

出口检查 `TIF_NEED_RESCHED`：若这次调用跑了很久、或唤醒了更高优先级的任务，内核会在返回用户态前切一次调度器。所以「一次系统调用」可能变成「一次上下文切换」，这是延迟抖动的常见来源，`strace -T` 里偶尔出现的毫秒级 `write` 多半是它。

`sysretq` 用 RCX 当 RIP、R11 当 RFLAGS。RCX 不是规范地址会在 ring0 触发 #GP（2012 年那类提权漏洞的形状）；现代内核先检查，不合法就退回 `iretq`。

### 最后：`write` 成功不等于落盘

`write(1, "hi\n", 3) == 3` 的全部含义是：这 3 个字节被内核接受、拷进页缓存、标成脏页。设备什么时候拿到它，是回写与 `fsync` 的事（`base/29`）。

---

## 21.4 成本账：固定成本与数据成本

先摆本机的基准。环境：2 vCPU KVM（AMD EPYC 7K62，标称 2595.122 MHz）、Linux 6.12.113、gcc 12.2.0；测量时 `load average` 在 3–14 之间（2 核机器上还有别的负载），每个用例跑 5 轮取最小值。同一份代码换一次跑会差 1.5–2 倍——这本身就是「vCPU 被抢走」的物理事实。

| 操作 | 5 轮最小值 | 折算 cycle | 这笔钱花在哪 |
| --- | --- | --- | --- |
| `getpid()` × 1000 万 | 298 ns/次 | ~773 | 纯跨界：入口、派发、出口 |
| `syscall(SYS_getpid)` × 1000 万 | 336 ns/次 | ~872 | 同上，绕过 glibc 包装 |
| `write(1 B)` → `/dev/null` × 100 万 | 485 ns/次 | ~1258 | 跨界 + VFS 骨架，驱动什么都不做 |
| `write(1 B)` → ext4（热页、定址） | 1111 ns/次 | ~2883 | 再加 inode 锁、时间戳、脏页记账 |
| `write(1 B)` → ext4（增长文件） | 1498 ns/次 | ~3888 | 再加文件扩展、页分配 |
| `gettimeofday()` 走 vDSO × 1000 万 | 145–173 ns/次 | ~376–449 | 不进内核，读共享数据页 |
| `gettimeofday()` 强制 syscall × 1000 万 | 571–996 ns/次 | ~1482–2585 | 完整跨界 + 时间换算 |

### 为什么是 298 ns，而不是教科书上的 50–200 ns

教科书那个数字来自「无缓解、裸金属、同频」的机器。本机不是：

```
meltdown            : Not affected              ← 因此 PTI（页表隔离）未开启
retbleed            : Mitigation: untrained return thunk; SMT disabled
spectre_v1          : Mitigation: usercopy/swapgs barriers and __user pointer sanitization
spectre_v2          : Mitigation: Retpolines; IBPB: conditional; RSB filling
```

`retbleed` 的「untrained return thunk」把内核里每条 `ret` 换成一段训练过的跳板，retpoline 把间接调用换成捕鼠笼，`spectre_v1` 的屏障加在每次用户/内核数据交换处。三项都在系统调用的必经之路上。硬件往返（`syscall` + `sysret`）本身约 100–200 cycle，剩下的 500 多 cycle 是入口/出口代码、屏障，以及「用户态和内核态各冷一次」的 icache/iTLB 代价。

**KPTI 是另一个开关，本机没法实测**：`meltdown: Not affected` 意味着内核没有启用页表隔离。在开启 PTI 的机器上，每次系统调用要切两次 CR3（用户页表 ↔ 内核页表）；有 PCID 时是两次 CR3 写加 TLB 标记切换，没有 PCID 时还会冲刷 TLB。量级上空系统调用会从 300 ns 抬到 600–1000 ns。这解释了一个常见现象：**同一份代码换一台机器，syscall 密集的服务慢一倍**，而标称主频相同。

### 把一次文件写拆成四层

| 层 | 你付的钱 | 本机证据 |
| --- | --- | --- |
| ① 特权切换 | 0.26–0.30 µs/次 | `getpid` 298 ns；4 KiB 热写循环里每多一次 syscall 258 ns |
| ② 进入文件系统固定路径 | +0.5–0.9 µs/次 | `write 1 B` 到预分配文件 1111 ns |
| ③ 扩展文件 / 分配页 | +0.4–0.6 µs/次 | 增长文件 1498 ns，比定址多约 390 ns |
| ④ 搬数据 | ≈0.13 ns/B（约 7.9 GB/s） | 1 MiB 热写 135 µs；用户态 `memcpy` 1 MiB 37.4 µs（28 GB/s） |

这张表解释两个看似矛盾的现象：**1 MiB 一次写（442 µs 冷 / 135 µs 热）比 100 万次 1 字节（1630 ms）快 3700 倍**，因为后者付了 100 万次①②③只搬 1 MB 数据；**1 MiB 到 `/dev/null` 只要 0.44 µs**，因为④根本没发生。反过来，1 字节写的成本几乎全在固定项：298 ns 的① + 800 ns 的② + 400 ns 的③ ≈ 1.5 µs，那 1 个字节可以忽略。这就是「小写靠批、大写靠带宽」的根据。

### 那张 17 倍的账，可以精确到每一次调用

1,000,000 次 `write` 与 16,384 次的差是 983,616 次跨界，时间差 2421.71 − 167.90 = 2253.81 ms：

```
2253.81 ms / 983,616 次 = 2.291 µs/次
```

用 64 KiB 缓冲再算一遍：(2421.71 − 139.74) / (1,000,000 − 1,024) = **2.284 µs/次**。两条路径独立算出同一个数，说明模型是对的：**在这个负载下，每省掉一次 `write`，省 2.28–2.29 µs**。其中约 0.3 µs 是特权切换，其余近 2 µs 是「每次调用都要重走一遍 VFS 与文件系统固定路径」。缓冲把 2 µs 摊销到 64 行上，每行只付 0.036 µs。

这就是主线③「每层抽象的单价 = 跨界次数 × 单次成本」在本篇的落点：**性能不是一个连续量，它是「跨界次数」乘以一个几乎不变的单价。**

---

## 21.5 参数拷贝与校验：内核为什么不能相信用户指针

`write` 的第二个参数是一个用户态地址。内核拿到它时，这个数字只是一个 64 位整数，至少有四种不可信：

| 不可信的性质 | 具体形态 | 内核的应对 |
| --- | --- | --- |
| 值可能是内核地址 | `buf = 0xffff_8000_0000_0000` | `access_ok` 检查是否落在用户半区、且 `addr + len` 不回绕 |
| 可能没映射 | `buf = (void*)0x1` | `copy_from_user` 的异常表把 #PF 变成「返回未拷贝字节数」 |
| 可能只读、或权限不符 | 指向 `PROT_READ` 的页 | 同上，缺页/PF 由 fixup 接住 |
| 检查后可能被改 | 另一个线程 `munmap` 或改写内容 | **只读一次**：先拷进内核缓冲，之后只用内核副本 |

`access_ok` 只查「范围像不像用户地址」，不查映射。真正的搬运由 `copy_from_user` 完成：`stac` → 字符串拷贝 → `clac`。SMAP 规定内核默认不能读写用户页，`stac` 临时置 AC 标志打开窗口，`clac` 立刻关上。SMEP 是另一半：内核不能执行用户页，挡住 `ret2usr` 这类把返回地址指回用户代码的手法。

### 缺页发生在拷贝中途会怎样

内核不会让你的进程吃 SIGSEGV，而是走异常表：拷贝指令旁边登记了「如果这条指令 fault，跳到 fixup」。fixup 把「已拷多少」算出来，返回剩余字节数。于是 `write` 的返回值是「实际写了多少」，不是「要么全成要么全败」。本机实测：

```
write(fd, (void*)0x1, 4)              -> -1, errno=14 (Bad address)
write(pipe, 8192B crossing hole)      -> 4096
```

第一行：`access_ok` 通过（`0x1` 确实在用户半区），但页没映射，拷贝第一条就 fault，返回 `-EFAULT`（14）。第二行：缓冲区横跨两页，第一页有效、第二页被 `munmap` 掉，内核拷完第一页的 4096 字节后停住，**返回 4096**——这叫部分成功，是 `write` 契约的一部分，不是 bug。

### 内核侧的标准写法

```c
/* 示意，不是源码原文 */
static long demo_ioctl(struct file *f, unsigned int cmd, unsigned long arg)
{
    struct demo_req req;

    if (copy_from_user(&req, (void __user *)arg, sizeof req))
        return -EFAULT;                 /* 返回未拷贝字节数，非 0 即失败 */

    if (req.len > sizeof f->buf)        /* 先校验，再使用 */
        return -EINVAL;

    memcpy(f->buf, req.data, req.len);  /* 之后只用内核副本 */
    return 0;
}
```

反过来，漏掉返回值检查就是一类真实漏洞的标准形状：

```c
struct demo_req req;
copy_from_user(&req, arg, sizeof req);   /* 漏检查 */
if (req.len > MAX) ...                   /* 读的是未初始化的栈内容 */
```

`copy_from_user` 失败时**不保证目标缓冲区被写过**。于是 `req.len`、`req.data` 里是上一个函数留在内核栈上的旧数据；把它当命令、当长度、当指针用，轻则逻辑错乱，重则把内核栈内容顺着返回值送回用户态。**「拷贝失败」必须当作「数据不存在」，而不是「数据可能有点旧」。**

### TOCTOU：为什么不能「先查再读」

```c
/* 错的：用户内存被读了两次 */
if (get_user(len, &u->len)) return -EFAULT;
buf = kmalloc(len, GFP_KERNEL);
if (copy_from_user(buf, u->data, len)) return -EFAULT;
/* len 在两次读之间可能被另一个线程改大，buf 装不下 */

/* 对的：一次拷结构体，之后只信内核副本 */
struct demo_req req;
if (copy_from_user(&req, u, sizeof req)) return -EFAULT;
if (req.len > sizeof f->buf) return -EINVAL;
```

用户指针指向的内存是**活的**：另一个线程可在两次访问之间改写它，甚至 `munmap`。内核的安全模型是「**只读一次**」：需要的东西一次性拷进内核缓冲，之后所有校验和使用都针对副本。任何「检查用户内存 → 再读同一块用户内存」的写法都叫 double fetch。

### `strncpy_from_user` 的边界

```c
char name[16];
long n = strncpy_from_user(name, u->name, sizeof name);
if (n < 0) return -EFAULT;                 /* 页错误 */
if (n == sizeof name) return -ENAMETOOLONG; /* 填满了还没见到 NUL */
```

返回值是「不含结尾 NUL 的长度」（内核文档的约定）；在 `count` 内没找到 NUL 时返回 `count`（缓冲区被填满、没有结尾符），**不是负错误码**。所以 `n == sizeof name` 必须按「截断」处理，否则 `strlen(name)` 会读出界。**长度参数不是建议，是硬上限；返回值的每种取值都要有分支。**

| 检查 | 防什么 | 漏了会怎样 |
| --- | --- | --- |
| `access_ok` | 内核半区地址、`addr+len` 回绕 | 内核替用户读内核内存 |
| `copy_from_user` 返回值 | 未映射页、只读页、中途 fault | 使用未初始化缓冲，泄漏内核栈 |
| 长度 clamp | 用户传 `len = SIZE_MAX` | 栈/堆溢出 |
| 一次拷贝（TOCTOU） | 用户线程中途改值 | 校验的和使用的不是同一份数据 |
| SMAP / SMEP | 内核误读写/误执行用户页 | 把用户页当内核数据或代码 |

---

## 21.6 三种入口：`syscall`、`int 0x80`、vDSO

同一个「从用户态请求内核服务」的意图，x86-64 上有三条路，成本差一个数量级。

| 入口 | 指令 | 编号放哪 | 参数放哪 | 内核入口 | 本机实测 |
| --- | --- | --- | --- | --- | --- |
| x86-64 快速路径 | `syscall` | `rax` | `rdi, rsi, rdx, r10, r8, r9` | `entry_SYSCALL_64` | 298–527 ns |
| 32 位历史路径 | `int $0x80` | `eax` | `ebx, ecx, edx, esi, edi, ebp` | `entry_INT80_compat` | 本机无 32 位工具链，未测 |
| vDSO | 普通 `call` | 没有 | 普通 C 调用约定 | 无（用户态代码） | 145–173 ns |
| vsyscall（遗留） | `call` 到固定地址 | 没有 | 普通 C 调用约定 | 模拟执行 | 不建议使用 |

### `int 0x80`：为什么慢，为什么还在

它走中断描述符表（IDT）第 0x80 项：硬件查门描述符、做权限检查、压一个完整的 `iret` 帧（5 个字），返回时 `iret` 逐项恢复并做段检查；`syscall`/`sysret` 只用寄存器传返回地址和标志。代价上 `int 0x80` 通常是 `syscall` 的 2–3 倍 cycle。更麻烦的是语义：64 位进程执行它时，内核按 **32 位 ABI** 解释参数，指针只有 32 位，超过 4 GiB 的地址会被截断——这就是有些老程序必须用 `MAP_32BIT` 的原因。它至今还在，是兼容性承诺。

### vDSO：把「读时间」变成一次普通函数调用

内核在 `execve` 时往每个新进程映射一段只读代码（本机 2 页）和一段只读数据（`[vvar]`，本机 4 页），合称 vDSO：

```
7f32f0f61000-7f32f0f63000 r-xp 00000000 00:00 0   [vdso]
```

glibc 从辅助向量（auxv）里拿到地址，解析出 `__vdso_clock_gettime`、`__vdso_gettimeofday` 等符号。之后你调 `gettimeofday()`，执行的是这段用户态代码：它从 vvar 读 pvclock 参数（版本号、TSC 基准、倍率与位移），配上当前 TSC 换算，用 seqlock 重试保证一致。整个过程**不换 ring、不换栈、不查表、不拷贝**。本机实测：

```
gettimeofday (vDSO)      :  145.17 ns/call
gettimeofday (syscall)   :  571.06 ns/call     ← 同一次运行的强制 syscall 版本
```

差 425 ns，与空 syscall 同量级。强制版本是显式调用 `syscall(SYS_gettimeofday, ...)`，绕过 vDSO。它会退回 syscall 的情况：内核没为当前 clocksource 提供 vDSO 实现、程序自己写 `syscall(...)`、动态链接器解析失败；`strace -c -e trace=gettimeofday` 里出现调用就说明没走 vDSO。安全上的推论：**seccomp 拦不住 vDSO**，因为它根本不进内核。

### `strace` 不可用时怎么观测

本机探测时 `strace` 先是不在 PATH，重测时 `/usr/bin/strace` 6.1 可用；`perf`、`bpftrace`、`ltrace` 未安装。可用时的账：

```
$ strace -c -e trace=write ./demo          # 每行一次 write
100.00    0.014136           1     10000           write
$ strace -c -e trace=write ./demo b        # 4 KiB 缓冲
100.00    0.000000           0       157           write
```

调用数 10000 → 157，缓冲把跨界次数压到 1/64。注意 `usecs/call` 显示 1 µs，比 §21.4 的 0.49 µs 大——`strace` 在每次系统调用前后停住进程，这是「被观测后的时间」；`strace -T` 的 `<0.000009>` 更夸张，同理。

`strace`/`perf` 都没有时，退到 `/proc`：

```
$ cat /proc/self/syscall
0 0x3 0x7fe2206a5000 0x20000 0x7fe2206d8b60 0xffffffff 0x0 0x7ffe9bba8c88 0x7fe2207c12ad
```

九个字段：调用号、六个参数、用户栈指针、返回地址。`0` 是 `read`，`0x3` 是 fd，`0x20000` 是 128 KiB 读缓冲——**你读到这一行时，进程正卡在读这个文件的 `read` 里**，所以必然显示 `read`。

它只对「停住」或「阻塞」的任务有意义，正在跑的进程显示 `running`：

```
$ cat /proc/PID/syscall
running
$ cat /proc/PID/syscall        # 同一个进程，改成阻塞在空管道 read 上
0 0x3 0x7ffcf83e601f 0x1 0x7f31871e8b60 0x0 0x7f31873ce6d0 0x7ffcf83e6008 0x7f31872d12ad
$ cat /proc/PID/wchan
pipe_read
$ cat /proc/PID/schedstat
49707780 157868305 16
```

`wchan` 说明卡在哪个内核函数；`schedstat` 三个数是「runqueue 等待（ns）、实际运行（ns）、时间片数」。三者合起来足以回答「它在跨界还是在等 CPU」。再不够就自己写 ptrace 追踪器：`PTRACE_SYSCALL` 让子进程每次进出系统调用停一次，停的次数除以二就是调用数。

---

## 21.7 内核 API 的三种形态：一次一个、一次一批、映射共享

现在可以把「固定成本」翻译成 API 设计。同一个需求——把 1 MiB 数据写进文件——有六种做法，区别只在「跨了几次界、拷了几次」：

| 做法 | syscall 次数 | 用户 ↔ 内核拷贝 | 本机实测（1 MiB） | 适合什么 |
| --- | --- | --- | --- | --- |
| `write` 1 B × 1,000,000 | 1,000,000 | 100 万次调用，共 1 MB | 1630 ms | 交互式、一次一条 |
| `write` 4 KiB × 256 | 256 | 256 次，共 1 MiB | 533 µs 冷 / 200 µs 热 | 有缓冲的流式写 |
| `write` 1 MiB × 1 | 1 | 1 次，1 MiB | 442 µs 冷 / 135 µs 热 | 手上有连续大块 |
| `writev` 1024 × 1 KiB | 1 | 1 次遍历 1024 段 | 612 µs（冷） | 数据分散在多个缓冲 |
| `mmap` + `memcpy` | 3（open/mmap/munmap） | 1 次 CPU 直写 + 256 次缺页 | 258 µs | 反复访问同一区域 |
| `write` 1 MiB → `/dev/null` | 1 | 0 | 0.44 µs | 只测 syscall 成本 |

表里的 syscall 次数和拷贝次数是**两个独立的轴**。

- 从 1 字节改成 4 KiB：syscall 少了 3906 倍，拷贝的**次数**也少了 3906 倍，但拷贝的**字节数**一点没少（都是 1 MiB）。省的是每次调用的固定成本。
- 从 `write` 改成 `writev`：syscall 从 1024 次降到 1 次，但内核仍要逐段拷贝 1024 次。`writev` 买到的是「不用在用户态把散块拼成连续内存」，不是「不用拷贝」。
- 从 `write` 改成 `mmap`：拷贝的字节数没变，但**每次调用**的拷贝没了；代价是第一次触碰每一页都要缺页（1 MiB / 4 KiB = 256 次），映射的建立与撤销各一次 syscall。
- 真正把拷贝次数降到 0 的是 `sendfile`/`splice`（数据不进用户态），见 `base/37`。

`writev` 的写法与上限：

```c
struct iovec iov[1024];
for (int i = 0; i < 1024; i++) {
    iov[i].iov_base = chunks[i];
    iov[i].iov_len  = 1024;
}
ssize_t n = writev(fd, iov, 1024);   /* IOV_MAX = 1024（Linux） */
```

`IOV_MAX` 是硬上限（`sysconf(_SC_IOV_MAX)` 通常返回 1024）。超过就分批，或者换 `io_uring`。

### 为什么 `io_uring` 是第三种形态，而不是「更快的 `writev`」

`io_uring` 把提交队列（SQ）和完成队列（CQ）放在两块 `mmap` 出来的共享内存里。用户态把「要做什么」写进 SQ 环，然后**一次** `io_uring_enter` 提交 N 个请求；内核把结果写进 CQ 环，用户态自己读。于是：

- **跨界**：N 次 → 1 次；`IORING_SETUP_SQPOLL` 让内核线程替你轮询 SQ，连这 1 次也能省。
- **解析**：注册过的文件与缓冲区（`IORING_REGISTER_FILES` / `BUFFERS`）不用每次查 fd 表、重新 pin 页。
- **拷贝**：`IORING_OP_WRITE_FIXED` 仍要拷数据，但省掉每次的地址校验与映射；真正的零拷贝要配合 `sendfile`/`splice`。

所以它属于第三形态：**把「一次调用」变成「共享内存里的一个条目」**。完整接口与就绪语义在 `base/31`，syscall 账的实例对比在 `base/36`。

### 选型只看两个问题

| 你的数据形态 | 首选 | 理由 |
| --- | --- | --- |
| 小、交互式、一次一条 | `read`/`write` | 代码最简单，单次 0.3–1.5 µs 可以接受 |
| 多条独立小消息（日志、RPC 批） | `writev` / `sendmmsg` | 一次跨界处理 N 条，省 (N−1)×固定成本 |
| 大块、反复访问、随机 | `mmap` | 省掉每次调用的拷贝，缺页成本一次摊销 |
| 文件到 socket | `sendfile` / `splice` | 数据不进用户态，拷贝次数最少 |
| 超高 IOPS、批量提交 | `io_uring` | 环共享 + 批量提交 + 可选零 syscall |

一个容易忽略的约束：**批处理有延迟代价**。攒 64 KiB 再写，意味着最坏情况下最后一行要等 64 KiB 攒满（或等定时器 flush）。日志场景通常用「攒满或每 N 毫秒」双条件；数据库的 group commit 是同一个交易的另一面（`base/29`）。**吞吐与延迟的取舍，本质是「你愿意让多少数据陪一次跨界」。**

---

## 21.8 实测：四个实验

环境：2 vCPU KVM、AMD EPYC 7K62（标称 2595.122 MHz）、Linux 6.12.113+deb12-cloud-amd64、gcc 12.2.0。测量期间 `load average` 3–14，每个用例跑 5 轮取最小值。`strace` 6.1 可用；`perf`、`bpftrace` 未安装。

### ① 空系统调用：`getpid` 循环 1000 万次

```c
#define _GNU_SOURCE
#include <stdio.h>
#include <unistd.h>
#include <time.h>
#include <sys/time.h>
#include <sys/syscall.h>
static double now(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec*1e9+t.tv_nsec;}
int main(void){
    volatile long s=0; double b; struct timeval tv;
    b=1e18; for(int r=0;r<5;r++){double t=now();for(long i=0;i<10000000;i++)s+=getpid();t=(now()-t)/1e7;if(t<b)b=t;}
    printf("getpid x 10,000,000      : %7.2f ns/call\n", b);
    b=1e18; for(int r=0;r<5;r++){double t=now();for(long i=0;i<10000000;i++)s+=syscall(SYS_getpid);t=(now()-t)/1e7;if(t<b)b=t;}
    printf("syscall(SYS_getpid)      : %7.2f ns/call\n", b);
    b=1e18; for(int r=0;r<5;r++){double t=now();for(long i=0;i<10000000;i++){gettimeofday(&tv,0);s+=tv.tv_usec;}t=(now()-t)/1e7;if(t<b)b=t;}
    printf("gettimeofday (vDSO)      : %7.2f ns/call\n", b);
    b=1e18; for(int r=0;r<5;r++){double t=now();for(long i=0;i<10000000;i++){syscall(SYS_gettimeofday,&tv,0);s+=tv.tv_usec;}t=(now()-t)/1e7;if(t<b)b=t;}
    printf("gettimeofday (syscall)   : %7.2f ns/call\n", b);
    return 0;
}
```

一次真实运行（`load average` 约 14，属于偏差的一侧）：

```
getpid x 10,000,000      :  526.69 ns/call
syscall(SYS_getpid)      :  559.46 ns/call
gettimeofday (vDSO)      :  173.27 ns/call
gettimeofday (syscall)   :  995.84 ns/call
```

同一天负载较低时的最小值分别是 298 / 336 / 130 / 688 ns。**同一台机器、同一份代码，负载不同就能差 1.8 倍**——所以本篇结论都以「最小值 + 量级」表述。

### ② 1 字节写 100 万次 vs 1 MiB 写 1 次

```c
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <time.h>
static double now(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec*1e9+t.tv_nsec;}
int main(void){
    char *buf=malloc(1<<20); memset(buf,'x',1<<20); double b;
    int fd=open("/tmp/e2.dat",O_CREAT|O_TRUNC|O_WRONLY,0644);
    b=1e18; for(int r=0;r<5;r++){lseek(fd,0,0);ftruncate(fd,0);double t=now();
        for(long i=0;i<1000000;i++)write(fd,"x",1); t=now()-t; if(t<b)b=t;}
    printf("write 1B x 1,000,000     : %8.2f ms  (%6.2f ns/call)\n", b/1e6, b/1e6);
    b=1e18; for(int r=0;r<5;r++){lseek(fd,0,0);ftruncate(fd,0);double t=now();
        write(fd,buf,1<<20); t=now()-t; if(t<b)b=t;}
    printf("write 1MiB x 1           : %8.3f ms\n", b/1e6);
    close(fd); return 0;
}
```

一次真实运行：

```
write 1B x 1,000,000     :  3381.91 ms  (3381.91 ns/call)
write 1MiB x 1           :     0.355 ms
```

这次负载高，1 字节单价被抬到 3.38 µs；低负载时另一份同样的程序测得 1629 ms / 442 µs。两组数据的比值都在 4000–9500 倍之间：**同样的字节数，只因为跨界次数不同，时间差三到四个数量级。**

### ③ `gettimeofday`：vDSO vs 强制 syscall

代码在实验①的后两行。真实输出见上：vDSO 145–173 ns，强制 syscall 571–996 ns，比值 **3.9–5.7 倍**。这个差值就是「换 ring、换栈、查表、返回」的整套成本，因为两边做的时间换算几乎一样。

### ④ `cat /proc/self/syscall`

```
$ cat /proc/self/syscall
0 0x3 0x7fe2206a5000 0x20000 0x7fe2206d8b60 0xffffffff 0x0 0x7ffe9bba8c88 0x7fe2207c12ad

$ cat /proc/self/maps | grep vdso
7f32f0f61000-7f32f0f63000 r-xp 00000000 00:00 0   [vdso]
```

第一行：`0` = `read`（`cat` 正在读 `/proc/self/syscall`）、`0x3` = 文件描述符、`0x20000` = 128 KiB 缓冲区、最后两个十六进制数是用户栈指针与返回地址。**这个文件永远在展示「读它自己」的那次 `read`**，这是它的语义。第二行是 vDSO 的代码映射：2 页可执行代码；它前面还有一个 `[vvar]` 只读数据映射。两者地址都随机化。

---

## 21.9 三列账：承诺、代价、崩点

| 维度 | 承诺了什么 | 为此付了什么 | 从哪里开始崩 |
| --- | --- | --- | --- |
| 隔离 | ring3 碰不到内核页、执行不了特权指令；一个进程崩不拖垮内核 | 每次跨界换栈、换 GS、保存 168 B 现场、校验参数 | 内核里一个漏检的指针就是提权面；内核 bug 直接是 root（`base/33`） |
| 统一接口 | 「一切皆文件」：`read`/`write`/`ioctl` 覆盖文件、管道、socket、设备 | 每次调用都要走 VFS 抽象，固定成本 0.8–1.3 µs | syscall 密集的服务被固定成本卡死：每请求 20 次写 ≈ 46 µs |
| 资源记账 | 内核统一记 CPU、内存、fd，能限额、能隔离、能统计 | 每次 `write` 都更新 inode、脏页、时间戳；记账本身要时间 | 元数据风暴（`stat`/`open`/`close`）；脏页比例触发同步回写，延迟尖刺 |
| 兼容与演进 | 系统调用号一旦发布不改，三十年前的二进制还能跑 | 旧入口不能删，只能加新接口 | `int 0x80`、vsyscall 成了性能陷阱与攻击面；32 位指针截断 |
| 安全缓解 | Spectre/Meltdown/Retbleed 类攻击被压制 | 每次跨界多付 retpoline、屏障、可能的 CR3 切换 | 同一行代码换台机器慢两倍；容量规划必须按最慢机型算 |

一句话收束：**内核买到的是「隔离 + 统一接口 + 统一记账」，付出的是「每次跨界一笔固定费用」。** 三种 API 形态不是三种风格，是这笔费用逼出来的三种付法。

---

## 21.10 踩坑清单：现象 → 根因 → 定位 → 修 → 防复发

| 现象 | 根因（指回本篇） | 定位 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 日志/审计服务吞吐只有同规格的 1/17，`%sys` 高、`iowait` 低 | 每行一次 `write`，每次付 2.28 µs 固定成本（§21.4） | `strace -c -e trace=write` 看 calls；`pidstat -u` 看 `%sys` | 用户态缓冲 4–64 KiB 再一次写；或 `writev` | 代码评审禁止在循环里直接 `write`/`print`；统一封装 buffered writer 并默认开启 |
| 每条日志后 `fsync`，P99 从 2 ms 涨到 20 ms | `fsync` 要等设备确认，单次毫秒量级（`base/08`、`base/29`） | `strace -T -e trace=fsync` 看每次耗时；`iostat -x` 看 `f_await` | 批量 `fsync`（group commit）；或明确降低持久性要求 | 把「允许丢多少秒日志」写成业务决策，写进 SLO，而不是让代码默认每条都 `fsync` |
| `stat` 风暴：请求路径里反复 `stat` 同一个配置 | 每次 `stat` 一次跨界 + 路径解析；dentry 缓存未命中还要读盘 | `strace -c -e trace=stat,newfstatat`；看 calls 数 | 缓存 `stat` 结果（带 TTL）；用 `inotify` 代替轮询 | 热加载做成事件驱动，禁止「每请求 stat 一次」 |
| 信号处理后 `read`/`write` 返回 -1，程序当致命错误退出 | 慢系统调用被信号打断，返回 `-EINTR`；`SA_RESTART` 只覆盖部分调用 | `errno == 4`；`strace` 里出现 `= ? ERESTARTSYS` | `while (r < 0 && errno == EINTR)` 重试；装信号时考虑 `SA_RESTART` | 封装 `xread`/`xwrite`，统一处理 `EINTR` 与短读写 |
| `write` 返回小于请求长度，代码当成失败或当成全成功 | 内核缓冲满、管道容量、跨未映射页；`write` 的契约是「写了多少」（§21.5） | 检查返回值与 `count` 的差；大块写打日志 | 循环推进指针与剩余长度；记录部分进度 | 所有 IO 封装成 `write_all`/`read_all`，返回值不等于请求长度就继续 |

三行总结：**看到 `%sys` 高而 `iowait` 低，先数 syscall；看到 `-1` 先看 `errno` 是不是 4；看到返回值小于请求长度，先想「部分成功」而不是「失败」。**

---

## 21.11 一页速记

```
【一句话】内核是另一个特权级里的函数集合；一次调用 = 换栈 + 换 GS + 查表 + 拷贝 + 换回。

【一次 write 的十步】
参数进寄存器 → syscall（RCX/R11/CS/SS/LSTAR） → swapgs + 换内核栈 + 保存 168 B pt_regs
→ 查 sys_call_table[nr]（nospec + retpoline） → ksys_write → VFS
→ copy_from_user（access_ok + stac/clac + 异常表） → 脏页/时间戳
→ exit_to_user_mode（可能调度） → sysret → glibc 检查 rax ∈ [-4095, -1]

【分层单价（本机 2 vCPU KVM，多次最小值）】
① 特权切换            0.26–0.30 µs
② + VFS 固定路径      再加 0.5–0.9 µs
③ + 扩展文件/分配页   再加 0.4–0.6 µs
④ + 搬数据            约 0.13 ns/B（≈7.9 GB/s）
参考：空 getpid 0.30 µs；1 B→/dev/null 0.49 µs；1 MiB 热写 135 µs

【三种 API 形态】
一次一个：N×F + 数据        → read/write，N 大就死
一次一批：F + N×f + 数据    → writev/sendmmsg/epoll_wait
映射共享：F + 缺页 + 数据   → mmap/sendfile/io_uring ring

【两个独立轴】
要减少的是「跨界次数」，不是「代码行数」；
「syscall 次数」和「拷贝字节数」要分开优化，别混着算。

【一条安全底线】
用户指针一律不可信：access_ok → 只拷一次 → 用内核副本 → 检查返回值。
```

---

## 21.12 自检 3 题

**Q1.** 内核为什么不能直接解引用 `write` 传进来的用户指针？给出三条独立理由，并说明哪一条对应 SMAP、哪一条对应异常表。

<details><summary>答案</summary>

1. **值不可信**：那个整数可能是内核地址（≥ `TASK_SIZE`）、可能未映射、可能只读；直接解引用等于把「读哪里」交给用户。`access_ok` 只查范围（用户半区、`addr+len` 不回绕），不保证映射。
2. **缺页必须可控**：内核在普通指令上触发 #PF，处理不当就是 oops；异常表把「拷贝指令 fault」重定向到 fixup，返回未拷贝字节数，`write` 于是得到部分成功。**这一条对应异常表。**
3. **检查之后还会变**：用户内存是活的，另一个线程可在 `access_ok` 之后 `munmap` 或改写（TOCTOU）。所以只读一次，之后只信副本。

SMAP 对应另一件事：**地址合法、页也在，内核默认仍不许读写用户页**；`copy_from_user` 用 `stac/clac` 开关窗口。SMEP 是姊妹：禁止内核执行用户页，挡住 `ret2usr`。

</details>

**Q2.** 同一颗 CPU 上，`gettimeofday` 从 571–996 ns 降到 145–173 ns。这 400–800 ns 差在哪？什么情况下会退回去？为什么说「seccomp 拦不住 vDSO」？

<details><summary>答案</summary>

vDSO 把「一次系统调用」换成「一次普通函数调用」。内核在 `execve` 时映射一页只读代码（`[vdso]`）和一页 vvar 数据；`gettimeofday` 执行 `__vdso_gettimeofday`，从 vvar 读「时间基准 + TSC」并用 seqlock 重试。不换 ring、不换栈、不查表、不校验指针、不拷贝——这五件事就是那 400–800 ns。

退回 syscall：内核没为当前 clocksource 提供 vDSO 实现、程序显式写 `syscall(SYS_gettimeofday, ...)`、链接器解析失败；`strace -c -e trace=gettimeofday` 出现调用就说明没走 vDSO。

seccomp 只挂在系统调用入口上；vDSO 不进内核，所以过滤器看不到它。

</details>

**Q3.** 一个服务每请求调 20 次 `write(2)` 写日志，业务逻辑本身耗时 50 µs。老板要求 QPS 提高到 10 倍。用本篇的三个量给出方案与收益上限。

<details><summary>答案</summary>

按本机日志路径单价 2.28 µs/次：

- 现在：50 + 20 × 2.28 = 95.6 µs/请求，上限约 10,460 QPS。
- 合并成 1 次写：52.3 µs/请求，上限约 19,120 QPS。

**加速比只有 1.83 倍，不是 10 倍**——业务逻辑已占一半以上；收益上限是 `(W + N·F)/(W + F)`。要真的到 10 倍，必须动 `W`，或把日志改成异步：业务线程只把条目塞进无锁队列，刷盘线程批量 `write`，此时 `W` 里不再含 syscall 成本。

三个量各自的角色：① 固定成本 `F` 决定合并能省多少，省的是 `(N−1)·F`；② 合并不减少总拷贝字节数，只减少拷贝次数与每次固定开销；③ 若写管道/socket 且下游读得慢，`write` 会在缓冲区满时阻塞，瓶颈从 CPU 变成等待——正确做法是非阻塞 fd + 有界队列 + 独立刷盘线程，并明确「队列满时丢日志还是阻塞业务」。

所以不是「加缓冲就快 10 倍」，而是：**先算天花板，再把同步写改成异步批量，最后处理阻塞语义。**

</details>

---

## 21.13 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 特权级、ring、页表隔离、微内核 vs 宏内核（本篇的上游） | → `base/15` |
| ELF 装载：内核怎么把文件映射成进程，下一次跨界的对象 | → `base/22` |
| 进程与线程：`task_struct`、`clone` 的共享参数组合 | → `base/23` |
| 缺页：`copy_from_user` 撞上未映射页之后发生什么 | → `base/26` |
| 页缓存与 `fsync`：`write` 成功之后的三层账 | → `base/29` |
| IO 模型全景：epoll/io_uring 的接口与就绪语义 | → `base/31` |
| socket 收包六道门与零拷贝 | → `base/32` |
| 零拷贝四实现的 syscall 与拷贝账 | → `base/37` |
| 系统调用账的实例：epoll vs io_uring | → `base/36` |
| 内核攻防：权限位、缓冲区溢出、ASLR、seccomp | → `base/33` |
| Go 调度器如何把阻塞 syscall 变成「P 被抢走」 | [go/19](../go/19-scheduler-runtime.md) |
| 全书总纲：四条主线与 56 篇清单 | [0-summary](0-summary.md) |
