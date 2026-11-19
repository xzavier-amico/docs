# 15. 特权级与虚拟化：ring、微内核 vs 宏内核、VM vs 容器

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · CPU 与 GPU：算力的物理形状 ｜ **编号**：15 / 56
> **本篇定位**：Part II 的第六篇，也是这一部分唯一一篇不讲「算力有多快」、只讲「算力的边界在哪」的。10–14 讲一条指令怎么在流水线上飞、数据怎么在缓存与设备之间搬；本篇反过来问：**同一台机器上，凭什么两段代码可以互相当对方不存在**。全篇只有一条推论链——`隔离的物理实体是「特权级 + 页表」` → `所以每次跨越边界都要一次硬件切换` → `切换有固定单价` → `微内核、VM、容器都是在这个单价上做的不同交易`。21 讲一次系统调用的完整路径，33 讲内核攻防，34 手搓容器，[llm/29](../llm/29-sandbox-isolation.md) 讲沙箱四档选型：它们全部复用本篇这一条链。
> **读法**：§15.2 的权限级表与特权指令表、§15.3 的跨界账是全篇支点，后者请对着自己的机器重跑一遍（十分钟）；§15.4 的「IPC 税」公式与 §15.6 的「三件事」表可以直接拿去判断「这个需求该用 VM 还是容器」；赶时间只读 §15.3、§15.6、§15.9、§15.11 四张表。
> **不在本篇**：`syscall` 在 Linux 里的逐行实现（`entry_SYSCALL_64`、`pt_regs`、`syscall_exit_to_user_mode`）→ base/21；页表结构与缺页处理 → base/26、base/27；namespace/cgroup 的手工搭建 → base/34；权限位、缓冲区溢出、ASLR、seccomp 的攻防全谱 → base/33；沙箱四档的产品选型与配额/出口设计 → [llm/29](../llm/29-sandbox-isolation.md)；容器编排、探针与发布 → [go/52](../go/52-deployment-sre.md)。

---

## 15.1 引子：两个租户，一台机器，一份 root

先看三件互不相干、但根因是同一件事的事故。

**第一件：为了 `sysctl` 生效，把隔离整个交了出去。** 某 CI 平台的 runner 里，构建脚本要调 `net.core.somaxconn`，容器里报 `permission denied`。值班的人查到一个「一行修复」：加 `--privileged`。命令生效了，事故两周后到——runner 上任意 job 都拿到了宿主全部 capability 与设备节点，一个清理脚本把宿主数据盘当成了自己的 `/tmp`。复盘最刺眼的一句：**「我们以为容器在保护我们，其实保护我们的是那层被我们关掉的配置。」**

**第二件：一个租户把另一个租户的 CPU 抢光。** 64 核宿主上，A 没设上限，B 设了 `cpu.max = 200000 100000`（2 核）。A 的 `make -j64` 让 B 的 p99 从 40 ms 涨到 1.9 s。两边都「没超配额」——B 严格在那 2 核里，A 严格在「不限」里。**配额是上限，不是预留**：没人给 B 留出那 2 核，调度器只按线程公平分，而 A 有 64 个线程。

**第三件：我自己这台机器。** `uname -a` 说它在 KVM guest 里；`/proc/self/status` 里 `Seccomp: 0`、`NoNewPrivs: 0`、`CapEff: 000001ffffffffff`（41 个 capability 全开），进程还跑在宿主 namespace 里（§15.8 给 inode 证据）。也就是说：**这台机器上唯一的隔离边界是 hypervisor 和它外面的 VPC，内核里一道软墙都没设。** 谁要是以为「有 seccomp 在保护我」，那是把配置当成了机制。

| 事故 | 边界原本画在哪 | 谁付了账 | 本篇哪节解释 |
| --- | --- | --- | --- |
| 加 `--privileged` 换 `sysctl` | 容器（namespace + capability） | 宿主数据盘 | §15.6、§15.10 |
| 两个租户抢 CPU | cgroup 配额 | B 的 p99 | §15.6、§15.10 |
| 我这台开发机的「沙箱」 | hypervisor + 网络 | 无人（暂时） | §15.5、§15.8 |

三件事的共同形状：**隔离从来不是「软件上写了一句 if 禁止你」，而是「硬件在每一次访存、每一条指令上做的一次比较」**。软件只是决定这个比较的参数（页表项、CPL、capability 位），以及决定边界画在哪一层。画在函数调用上，跨界几乎免费；画在 privilege boundary 上，每次跨界要几百 cycle；画在 hypervisor 上，每次要几千 cycle。**微内核之争、容器之争，本质都是「边界画在哪一层、每次跨界付多少钱」——这就是本篇的唯一真问题。**

---

## 15.2 特权级：CPU 只认两个 bit 和一张表

### 当前特权级不是一个寄存器，是 CS 里的两位

x86 有四个环（ring 0–3），ARM64 有四个异常级（EL0–EL3）。但真正被硬件在每个周期检查的，只有一个数：**当前特权级**（Current Privilege Level，CPL）。x86 上它不在任何独立寄存器里，而是代码段寄存器 `CS` 的最低两位：

```c
unsigned short cs;
__asm__ volatile("mov %%cs, %0" : "=r"(cs));
printf("CS = %#06x -> CPL = %u\n", cs, cs & 3);
```

在这台机器上跑出来是：

```
CS = 0x0033 -> CPL = 3
```

`0x33 = 0b110011`，最低两位 `11` 就是 3。所以「我在用户态」这句话的物理含义是：**CS 的低两位等于 3**。当 `syscall` 指令执行时，CPU 从 MSR `STAR` 的高 32 位取出内核代码段选择子写进 CS，CPL 在同一周期变成 0——**没有函数调用，没有栈切换，只有寄存器被微码改写**。返回时 `sysretq` 再从 STAR 的低 32 位取回用户段。

| 层 | x86-64 | ARM64 | 谁住在里面 | 能碰什么 |
| --- | --- | --- | --- | --- |
| 用户 | ring 3（CPL=3） | EL0 | 你的进程、JVM、Go runtime | 只能碰自己页表里 U/S=1 的页 |
| 内核 | ring 0（CPL=0） | EL1 | Linux 内核、驱动、调度器 | 全部物理内存、全部设备、页表根 |
|  hypervisor | ring -1（VMX root） | EL2 | KVM、Xen、Hyper-V | guest 的 VMCS、二级页表（EPT） |
| 固件/安全 | SMM / ring -2 | EL3 | UEFI SMM、ARM Trusted Firmware | 连 hypervisor 都看不见 |

两个细节值得记住：

1. **ring 1/2 在现代系统里是死的。** 它们本是为「驱动」和「内核子系统」准备的中间层，但 64 位下分段被压平（base=0、limit=全空间），中间层失去保护意义；跨界又太贵，于是主流 OS 都退化成「0 和 3 两级」。**四级环是硬件的可能性，两级是经济学的选择。**
2. **ARM64 的四级不是四个环，是四类异常。** EL2 的进入靠 `HCR_EL2` 里的一组路由，把本该进 EL1 的异常「截获」到 EL2；EL3 同理。**x86 的 ring -1 是新增一个维度，ARM 的 EL2 是把异常改道**——效果相同：guest 内核以为自己进了 ring 0，其实被抬到了 hypervisor 手里（§15.5）。

### 特权指令：不是在执行时被拒绝，是在解码阶段就 #GP

「用户态不能执行 `cli`」的准确版本是：**这些指令在 CPL>0 时根本不允许被译码执行**，CPU 直接抛 `#GP(0)`，指令一条都没跑。实测（装一个 SIGSEGV handler 打印 `si_code`）：

```
CS = 0x0033  ->  CPL = 3
read CR3 at CPL3:      signal=11 si_code=128  fault_addr=(nil)
cli at CPL3:           signal=11 si_code=128  fault_addr=(nil)
inb $0x60 at CPL3:     signal=11 si_code=128  fault_addr=(nil)
```

三次都是 `SIGSEGV` + `si_code=128`（`SI_KERNEL`：内核送来的硬件异常，不是访存地址错了），`si_addr` 为空。**`mov %cr3` 改的是页表根，`cli` 关的是全局中断，`in/out` 碰的是设备端口**——三者都在改「整台机器的全局状态」，所以不该由用户态决定。

完整的特权指令清单不用背，按「它改的是哪台机器的全局状态」分类就够：

| 类别 | 代表指令 | 为什么必须 ring 0 | 违例后果 |
| --- | --- | --- | --- |
| 中断开关 | `cli` / `sti` | 全局标志，关了就没有调度与时钟 | `#GP(0)` |
| 停机 | `hlt` | 让物理核停止取指 | `#GP(0)` |
| 地址空间 | `mov %cr3`、`invlpg` | 换页表根 / 使 TLB 项失效 | `#GP(0)` |
| 描述符表 | `lgdt` / `lidt` / `ltr` | 换中断门与段表，等于换「谁能进内核」 | `#GP(0)` |
| 机器状态 | `rdmsr` / `wrmsr` | 读写 LSTAR、EFER、APIC 基址 | `#GP(0)` |
| 调试寄存器 | `mov %dr0..7` | 硬件断点，可窥探任意地址 | `#GP(0)` |
| I/O 端口 | `in` / `out` | 直接对设备寄存器读写 | IOPL<CPL 时 `#GP(0)` |

最后一行不是绝对禁止，而是「看你有没有 IOPL」——`ioperm()` 就是把这个洞开给某个进程；`/dev/port` 与 X server 时代的直接显卡访问都长在这里。

### 第二道锁：页表的 U/S 位（CPL 管不到内存）

特权级只约束**指令**。用户态执行一条合法的 `mov (%rax), %rbx` 时，CPU 并不知道 `%rax` 指向谁的页——它去查页表，看那一页的 `U/S` 位：

```
  虚拟地址 ──► CR3 ──► PML4 ──► PDPT ──► PD ──► PT ──► 物理页
                         每一级表项里都有一位 U/S（User/Supervisor）
                         还有一位 NX（不可执行）、一位 R/W
  规则（4 级都通过才算）：
     CPL == 3 且 任一级 U/S == 0   →  缺页异常（#PF），不是 #GP
```

所以「内核地址对用户态不可见」不是靠「不告诉你地址」——**内核地址是公开的**（x86-64 上固定映射在 `0xffff800000000000` 以上），但所有内核页 `U/S=0`，用户态一碰就 `#PF`，Linux 把它变成 `SIGSEGV`。反过来，U/S=1 的页内核也能访问，于是需要两条反方向的锁：**SMEP** 禁止 CPL=0 *执行* 用户页（防内核跳到用户给的函数指针）；**SMAP** 禁止 CPL=0 *读写* 用户页，除非临时置 `AC` 标志——`copy_from_user` 外面包的 `stac`/`clac`，就是在这条锁上开一个精确的窗口。

**所以「隔离」在硬件上是两个比较器**：CPL 比较（管指令）+ PTE.U/S 比较（管数据），二者的参数都由内核写、由硬件查。软件能做的只有「写参数」和「决定什么时候改参数」。**「用户态」不是哲学概念，就是 CS 低两位等于 3，加上页表里一堆 U/S=0。**

跨过边界的合法入口只有一个：x86 的 `syscall`/`sysret`（MSR `LSTAR` 直接给出目标地址，不查表，约 100–300 cycle；`int 0x80` 要走 IDT 门，约 300–600 cycle，只剩老 ABI 在用）；ARM64 的 `svc #0` 必须查 `VBAR_EL1` 的向量表，基础成本略高但同量级。§15.3 把这个入口拆成逐跳的账。

---

## 15.3 跨界成本：一次 `syscall` 到底买了什么

### 逐跳拆开

以 `getpid()` 为例，把它拆到「哪条指令改了什么」的粒度。下面是伪汇编，注释里的数字是本节要算的账。

```
; ── 用户态 ────────────────────────────────────────────────
    mov  $39, %eax          ; __NR_getpid
    syscall                 ; ★ 这一条就是全部「请求」：不检查参数，只做切换
; ── 硬件（微码，几个周期内完成）────────────────────────────
    RCX  <- RIP             ; 返回地址存进 RCX
    R11  <- RFLAGS          ; 标志寄存器存进 R11
    CS   <- STAR[47:32]     ; 内核代码段，RPL=0 → CPL=0
    RIP  <- LSTAR           ; entry_SYSCALL_64
    RFLAGS &= ~SFMASK       ; 关中断（IF=0）、关方向标志等
; ── 内核 entry_SYSCALL_64 ──────────────────────────────────
    swapgs                  ; 换上内核的 GS base（per-CPU 数据）
    mov  %rsp, TSS.sp2      ; 把用户栈指针存进 TSS
    mov  top_of_stack, %rsp ; 换成内核栈（每线程 16 KiB）
    push ...                ; 造 pt_regs（168 B）：r11→eflags，rcx→ip
    [PTI] 切 CR3            ; 若开了页表隔离，这里多两次 CR3 写（见下）
    call do_syscall_64      ; → sys_call_table[39] → __x64_sys_getpid
; ── 返回 ──────────────────────────────────────────────────
    检查 _TIF_ALLWORK_MASK  ; 有信号？要调度？有 task_work？
    sysretq                 ; 快路径：RCX→RIP，R11→RFLAGS
```

这条路径上有四笔开销：**硬件模式切换**（改 CS/SS、换栈指针、屏蔽中断）、**上下文保存**（造 168 B 的 `pt_regs`、`swapgs`）、**缓解措施**（retpoline、`swapgs` 屏障、`VERW`、返回前的分支预测屏障）、**页表切换**（KPTI 时进出各一次 CR3 写；有 PCID 几十 cycle，无 PCID 是几百）。四笔各自都是几十 cycle 量级，加起来就是下面测出来的那个数。

### 实测账（本机，AMD EPYC 7K62 @2.595 GHz，KVM guest，2 vCPU）

```c
/* 循环 2×10^6 次，取中位数；sched_setaffinity 钉在 CPU0 */
t0 = ns(); for (i = 0; i < N; i++) acc += dummy(i);              t1 = ns();
t0 = ns(); for (i = 0; i < N; i++) acc += syscall(SYS_getpid);   t1 = ns();
t0 = ns(); for (i = 0; i < N; i++) acc += clock_gettime(...);    t1 = ns();
```

| 动作 | 实测 | @2.6 GHz 折算 | 说明 |
| --- | --- | --- | --- |
| 用户态函数调用（`noinline`） | 1.6–5.6 ns | 4–15 cycle | 只有 call/ret 与一层栈 |
| `clock_gettime`（vDSO） | 31–67 ns | 80–175 cycle | **不跨界**：读内核映射的共享页 |
| `syscall(SYS_getpid)` | 110–230 ns | 285–600 cycle | 一次完整跨界 |
| 上下文切换（pipe 乒乓，扣掉 4 次 syscall） | ≈ 2.5 μs | ≈ 6500 cycle | 换线程：栈、寄存器、TLB、调度 |
| 缺页（匿名页首次写） | 1.7–1.8 μs | ≈ 4600 cycle | 分配零页、建 PTE、填 TLB |
| `fork + exit + wait` | 100–115 μs | — | 复制页表、建 `task_struct` |
| `posix_spawn("/bin/true")` | 445–451 μs | — | `execve`：映射 ELF、动态链接、建页表 |

**自己推一遍**：110 ns × 2.595 GHz ≈ 285 cycle；一条普通指令在 4 发射的核上摊到 0.25 cycle，所以 **285 cycle ≈ 一千条指令的吞吐**——一次 `getpid` 等于让 CPU 白算一千条加法。而 `getpid` 是**最便宜的系统调用**（内核侧只读一个字段），它都要 285 cycle，说明开销不在「做事」，在「过边界」。这就是为什么「每次 `read` 一个字节」是灾难、「一次 `read` 64 KiB」才正常——**单次成本与搬运量无关，与次数成正比**（主线③）；真正的 `read`/`write` 是 1–3 μs，其中那 285 cycle 一分不少。

### KPTI：为了让一次跨界安全，多付一次页表切换

2018 年 1 月的 Meltdown 永久改写了这条成本。机制（只讲机制）：乱序执行会在权限检查完成**之前**把数据读进寄存器，若该页在 TLB 里，用户态就能通过缓存侧信道把内核内存「读」出来。防御是 **KPTI**：用户态运行时页表里干脆不映射内核，只留一小段 entry trampoline。

```
用户 CR3 ──syscall──► 切到内核 CR3 ──► 执行 ──► 切回用户 CR3 ──sysretq──►
          （多一次 CR3 写）                    （再多一次 CR3 写）
```

Linux 的实现很省：用户 PGD 与内核 PGD 只差**第 12 位**，切换就是 `CR3 ^ (1<<12)`；配合 PCID（12 位进程标识），TLB 不必清空，只是「同一进程的两套地址空间各占一个 PCID」。没有 PCID 的老 CPU 每次进出都要清 TLB，代价是数量级的。

量级结论：**KPTI 让系统调用密集的负载慢 1.5–3 倍**（随 CPU 代次、PCID、是否另开 retpoline/IBRS 变化）。本机 `meltdown: Not affected`（AMD），PTI 默认关闭，**所以上面那个 110 ns 偏乐观**；强制 `pti=on` 会显著变慢。这也解释了一个常见困惑：**同一段代码在 Intel 与 AMD 上 syscall 单价不同**——不是内核版本不同，是缓解措施不同。

### vDSO：把「不需要跨界」的部分搬到边界这一侧

同一张表里最有用的一行是 `clock_gettime`：31–67 ns，比 `syscall` 快 4–8 倍，而**它仍然是一个「系统调用」**。区别在于内核把一小段代码和一张 `vvar` 数据页**只读映射进了每个进程**，用户态直接调用那段代码、读共享内存里的时间，再用序列号（seqcount）检测「读的过程中内核有没有在更新」——**整个过程 CPL 没变过**。

```
   普通 syscall：用户 ──[trap]──► 内核 ──[sysret]──► 用户     ≈ 285 cycle
   vDSO       ：用户 ────── 读映射页 + 算 ──────► 用户        ≈ 100 cycle
```

**功能、语义完全一样，把跨界去掉，价格掉一个档**——这是主线③最干净的证据。`gettimeofday`、`time`、`getcpu`、`clock_gettime` 都走这条路；`getrandom` 不走（必须进内核消耗熵池，见 §15.12 Q1）。到这里，「微内核为什么慢」的物理来源已经齐了：**IPC 就是「两次跨界 + 一次数据搬运」**。

---

## 15.4 微内核 vs 宏内核：边界画在函数调用上，还是画在 IPC 上

### 两种画法

```
        宏内核（Linux）                          微内核（L4 / seL4 / QNX）
 ┌───────────────────────────────┐        ┌──────────────┐  ┌────────────────┐
 │  VFS │ ext4 │ 页缓存 │ 网络栈  │        │ 文件系统服务 │  │ 网卡驱动（用户）│
 │  驱动 │ 调度 │ mm │ IPC       │ ring0  └──────┬───────┘  └───────┬────────┘
 └───────────────────────────────┘               │  IPC：2 次切换   │
      进程内一次调用 ≈ 1–5 ns                    └────────┬─────────┘
                                                  ┌───────▼─────────┐
                                                  │ 调度 │ mm │ IPC │ ring0
                                                  └─────────────────┘
```

两者的差别不是「代码放在哪个目录」，而是**这段代码运行时 CPL 是几、页表里能看到多少**。宏内核把所有子系统放进同一个 ring 0、同一张页表：子系统之间调用就是 `call`，1–5 ns；代价是任何一个子系统里的一个野指针，都能改写整个内核。微内核只把「地址空间、线程/IPC、调度」留在 ring 0，文件系统、驱动、网络栈变成用户态进程：它们崩了只死自己，代价是每次请求都要 IPC。

### 把「税」算成公式

```
   宏内核一次 open()：  syscall(285) + VFS 查 dentry + ext4 读 inode + 建 file ≈ 2–4 μs
   微内核一次 open()：  syscall(285) + IPC 到 FS 服务(≈500–1000) + FS 内部逻辑
                        + IPC 回来(≈500–1000)                              ≈ 3–6 μs
```

**注意这个对比的诚实之处**：微内核的税不是「1000 倍」，而是「在已经付过 syscall 的基础上，再付 1–2 次 IPC」。对于 `open` 这种本身就要 2–4 μs 的操作，微内核是 **1.5–2 倍**，不是灾难。真正致命的是**单价便宜、次数极多的操作**：

| 操作 | 每秒次数（一台忙机器） | 宏内核单价 | 微内核单价 | 差额吃掉多少 CPU |
| --- | --- | --- | --- | --- |
| 文件 open/stat | 10⁵ | 3 μs | 5 μs | 0.2 核 |
| 文件 open/stat | 10⁷ | 3 μs | 5 μs | **20 核** |
| 缺页（每页一次） | 10⁶ | 1.8 μs | 1.8 μs + 1 IPC ≈ 3 μs | **1.2 核** |
| 网络包（每包一次） | 10⁷ pps | 1 μs | 1 μs + IPC | **10 核以上** |
| `getpid` 类极轻调用 | 10⁸ | 0.11 μs | 0.11 + 0.4 ≈ 0.5 μs | **≈40 核** |

公式只有一行：**`税 = 跨界次数 × 单次 IPC 成本`**。它在 10⁵ 次/秒时听不见，在 10⁷ 次/秒时吃掉半台机器。**所以微内核的性能之争，从来不是「IPC 快不快」，而是「你的负载每秒跨界多少次」。**

### L4/seL4 把 IPC 压到几百 cycle，靠的是四件事

教科书说「微内核 IPC 要几千 cycle」，而 L4 系（Liedtke 1993 起）做到几百 cycle。差额来自四个具体的动作，每一个都是「把不必要的钱删掉」：

| 优化 | 做法 | 省掉的是什么 |
| --- | --- | --- |
| 寄存器传参 | 消息前几个字直接放通用寄存器（x86-64 上 6 个，ARM64 上 8 个），不碰内存 | 一次 64 B–4 KiB 的拷贝 |
| UTCB / 用户态 TCB | 线程控制块的一部分映射在用户态，内核只读写必要字段 | 内核↔用户的结构拷贝 |
| 直接进程切换 | 接收方就绪且优先级合适时，**跳过调度器决策**，直接切过去 | 一次调度器调用 + 运行队列操作 |
| 惰性调度 | 只在「真正需要选下一个线程」时才更新调度状态，reply 时不做 | 两次调度记账 |
| 单地址空间 / 小地址空间 | 共享同一页表或使用小 ASID，IPC 不 flush TLB | 一次 TLB 清空（最贵的一项） |

seL4 是这条路的极点：内核约 1 万行 C，**功能正确性被形式化验证过**（2014 年完成证明，后续做到二进制级），IPC 的 `Call`/`Reply` 在 ARM 上量级是**几百 cycle**，并且有最坏执行时间（WCET）分析——这两件事（可证明、可定时）才是它在无人机、汽车、国防里活着的原因，而不是性能。

### 那为什么 Linux 赢了

如果只看 IPC 数字，微内核应该在 1995 年就赢了。它没赢，原因按重要性排：**① 驱动生态**——驱动占 Linux 内核代码六成以上，且是「硬件怪癖的集合」（某网卡在某种电源状态下要延迟 2 ms 才能写下一条寄存器），重写等于重踩所有坑，而硬件厂商只写一份驱动；**② POSIX / ABI**——应用不为「更优雅的内核」移植，只为「能跑」移植；**③ 税落在最热的路径上**——文件元数据、网络包、缺页恰好都是每秒千万次量级；**④ 混合体证明「搬回去」可行**——Windows NT 把图形子系统从用户态搬回内核，macOS 的 XNU 保留 Mach IPC 作内部机制，却把文件、网络、POSIX 全部放回内核（BSD 层）。**XNU 本身就是「纯微内核在这条负载上不划算」的一份实验记录。**

但微内核的**思想**赢了，只是换了名字：FUSE（文件系统）、DPDK/SPDK（驱动与协议栈）、VFIO（设备直通）、gVisor（整个 syscall 表面，§15.7）、WASM（代码沙箱）都是「把组件搬出内核」。规律是一道算术题：**当「隔离/可重启/可热升级」的价值超过「每秒跨界次数 × 单价」时，搬出去；否则搬回来。**

---

## 15.5 虚拟化：让 Guest OS 以为自己 ring0

### 问题的形状

Guest 内核要执行 `mov %cr3`、要收中断、要写设备寄存器——这些在 CPL=0 才合法。但 guest 只是宿主上的一个进程，它的 CPL 是 3。**虚拟化的全部工作，就是让这四件事同时成立：guest 以为自己 ring 0、宿主仍然控制一切、性能损失可接受、guest 不需要改代码。**

历史上只有三种做法：

| 做法 | 谁改代码 | 特权操作怎么办 | 典型代表 | 单价 |
| --- | --- | --- | --- | --- |
| 全虚拟化（二进制翻译） | 不改 guest | 运行时扫描 guest 代码，把特权指令替换成对 VMM 的调用 | VMware（2006 前）、VirtualBox（部分） | 每次陷入 10³–10⁴ cycle，且翻译有缓存开销 |
| 半虚拟化 | **改 guest 内核** | guest 主动调 hypercall，不执行特权指令 | Xen（PV 模式） | 一次 hypercall ≈ 10³ cycle，但无翻译 |
| 硬件辅助 | 不改 guest | CPU 新增一个特权层，特权操作自动陷入 | VT-x / AMD-V、ARM EL2 | 一次 VM exit ≈ 10³ cycle，但**只在必要时** |

前两条路都靠软件补硬件的缺，第三条是硬件直接长出一个维度。今天的数据中心几乎全是第三条。

### VT-x：ring -1 是字面意思

Intel VT-x 引入两个「操作模式」：**VMX root**（hypervisor 住在里面）与 **VMX non-root**（guest 住在里面）。关键点是：**non-root 模式里仍然有 ring 0–3**。于是 guest 内核确实在 CPL=0 上跑，`mov %cr3` 确实执行成功了——但它改的是 **guest CR3**，指向的是 guest 物理地址；而 guest 物理地址还要再经过一层 EPT 才落到宿主物理地址。**guest 的 ring 0 是真的，只是它统治的领土是虚拟的。**

状态保存在 **VMCS**（Virtual Machine Control Structure）里：一块 4 KiB 的控制块，存 guest 与 host 各自的 RIP/RSP/RFLAGS/CR3/段寄存器、退出原因、异常位图、EPT 指针。`VMLAUNCH`/`VMRESUME` 进入 guest，`VM exit` 回到 host：

```
  ┌────────── VMX root（hypervisor，ring -1）─────────────────┐
  │  VMCS：guest 状态快照 │ host 状态保存区 │ exit reason │ EPT │
  └───────────────────────┬──────────────────────────────────┘
        VMRESUME          │              ▲  VM exit（1000–3000 cycle）
                          ▼              │
  ┌────────── VMX non-root（guest，它以为自己是 ring0）────────┐
  │  guest 内核 CPL=0 → 写 CR3 / hlt / 读 MMIO / 收虚拟中断     │
  └───────────────────────────────────────────────────────────┘
```

一次 VM exit 要付的钱：保存 guest 状态到 VMCS（十几个字段）、加载 host 状态、跳进 hypervisor、判断退出原因、处理、再 `VMRESUME` 保存 host、恢复 guest。**量级 1000–3000 cycle**（0.4–1.2 μs @2.6 GHz），比 syscall 贵 3–10 倍。老一代全虚拟化的「world switch」更贵，因为要切整个地址空间并 flush TLB。

### EPT：第二层页表，和第二层代价

没有 EPT 的时代，VMM 维护「影子页表」：guest 的每一张页表都被 VMM 抄一份并翻译成宿主物理地址，同时把 guest 的页表页设成**只读**——guest 一写页表就触发 page fault，VMM 再同步。代价是**每次 guest 建页表都 exit**，进程一 `fork` 就是成百上千次 exit 的雪崩。

EPT（AMD 叫 NPT，ARM 叫 stage-2 translation）把这件事交给硬件：guest 页表做第一级翻译（虚拟地址 → guest 物理地址），EPT 做第二级（guest 物理 → 宿主物理）：

```
  guest 虚拟地址 ──[guest 页表, 4 级]──► guest 物理地址
                                              │
                                              │ [EPT, 再 4 级]
                                              ▼
                                          宿主物理地址
  一次 TLB miss 的最坏情况：4 级 guest 走查，每级都是一次 guest 物理访问，
  而那次访问本身又要 4 级 EPT 走查 → 最坏 20–24 次访存（原生只要 4–5 次）
```

所以 EPT 并没有让地址翻译变便宜，它只是**把「写页表就 exit」换成了「TLB miss 更贵」**。工程上的补偿是更大的 TLB、更大的页（2 MiB/1 GiB）、以及让热工作集留在 TLB 里。而 EPT violation（guest 第一次碰某页）依然是 μs 级：要 exit、走查、建映射、再回来。

### 什么样的操作会 exit，比 exit 多贵更重要

VM exit 的成本是固定的，**杀伤力来自次数**。三类高频出口：**MMIO 读写**（设备是虚拟的，寄存器在 VMM 里）、**特权指令**（`hlt`/`cpuid`/`wrmsr` 的语义需要 VMM 介入）、**EPT violation 与中断注入**（映射或投递需要 VMM）。解法都朝同一个方向：**virtio 把「每次读写寄存器」换成「往共享环里放一个描述符」，一个 virtqueue 放 256 个描述符、一次 kick 处理一批**；`hlt` 用 `MWAIT` 让出 vCPU，`cpuid` 结果缓存，中断走 posted interrupt/APICv。

**virtio 的设计哲学就是本篇的主线③**：既然单次 exit 便宜不下来，就把「次数」压下去。这和 `epoll` 批处理、`io_uring` 批量提交是同一个动作：**在固定的单次成本上，用批处理摊薄它**。

总结这一节：**VM 的本质是「用一个新的特权层（ring -1）去托管一个假的 ring 0」**。guest 的隔离强度来自「它连自己的页表根都不真正拥有」；代价是每一次它想碰真实世界，都要 exit。

---

## 15.6 容器：不是虚拟化，是同一内核上的三件事

### 没有第二个内核

容器里跑 `uname -r`：

```
$ uname -r
6.12.113+deb12-cloud-amd64
```

这个版本号是**宿主机的**，不是镜像里的。容器没有内核，它调用的是宿主的 `syscall`，跑的是宿主的调度器，用的是宿主的页表机制。**「容器」是三个互相独立的内核功能被同一个工具（runc/containerd）拼在一起的结果**：

| 功能 | 回答的问题 | 具体机制 | 它**不**做什么 |
| --- | --- | --- | --- |
| namespace | 我**看得见**什么 | `pid`/`mnt`/`net`/`ipc`/`uts`/`user`/`cgroup`/`time` 各自一棵命名树 | 不限制用量，不保护内存 |
| cgroup | 我**能用多少** | v2 的 `cpu.max`、`memory.max`、`pids.max`、`io.max` | 不隐藏任何东西 |
| rootfs + `pivot_root` | 我以为自己**在哪** | 换掉 `/` 的挂载点，旧根 `umount2(MNT_DETACH)` | 不是虚拟磁盘，不复制内核 |
| （第四轴）caps/seccomp/LSM | 我**被允许做什么** | capability 位图、BPF 过滤器、AppArmor/SELinux | 不是隔离边界，是策略 |

前三个是「构造容器」的全部材料，第四个是「加固容器」的旋钮。**四者都不提供「第二条内核」**——这就是容器与 VM 的全部差别。

### namespace：改的是「名字到对象的映射表」

namespace 不是「把对象藏起来」，而是**给进程换一张解析表**。实测对比（同一台机器，新建一个 PID namespace）：

```bash
$ readlink /proc/self/ns/pid; ls /proc | grep -c '^[0-9]'
pid:[4026531836]
119
$ unshare --pid --fork --mount-proc \
    sh -c 'readlink /proc/self/ns/pid; ls /proc | grep -c "^[0-9]"'
pid:[4026532352]
4
```

inode 号不同（`4026531836` vs `4026532352`）——**namespace 的身份就是它的 inode**，两个进程「在不在同一个 namespace」等价于「`/proc/<pid>/ns/pid` 的 inode 是否相同」。新建之后，`/proc` 里只剩 4 个进程，但宿主上那 119 个进程一个都没少，只是**这张表里查不到它们**。

每个 namespace 虚拟化的东西都不一样，混淆它们是大多数「容器怎么不生效」的根源：

| namespace | 虚拟化的是 | 一个常见误解 |
| --- | --- | --- |
| `pid` | PID 编号与 `/proc` 视图 | 「容器里的 1 号特殊」——它只是这个 ns 里的 1 |
| `mnt` | 挂载表（`mountinfo`） | 「容器有自己的磁盘」——只有一张不同的挂载表 |
| `net` | 网卡、路由、端口、iptables、`/proc/sys/net` | 「容器有独立带宽」——带宽归 cgroup |
| `uts` | hostname / domainname | 「改了 hostname 就改了机器名」 |
| `ipc` | SysV IPC、POSIX 消息队列 | 共享内存段仍要显式映射 |
| `user` | uid/gid 映射 + **capability 作用域** | 「容器里是 root 就是 root」（见下） |
| `cgroup` | `/sys/fs/cgroup` 的可见根 | 「看不见就等于没配额」——配额照旧 |
| `time` | `CLOCK_MONOTONIC`/`BOOTTIME` 的偏移 | 「容器里改时间影响宿主」——只影响读数 |

### 「容器里的 root 不是 root」——但它也不是「少了几个 bit」

这是全篇最容易被讲错的一句。真实机制是：**capability 没有变少，它被换了作用域。** 在一台普通机器上，把 uid 65534 映射成 namespace 里的 0：

```bash
$ setpriv --reuid=65534 --regid=65534 --clear-groups unshare -r \
    sh -c 'id; cat /proc/self/uid_map; grep CapEff /proc/self/status'
uid=0(root) gid=0(root) groups=0(root)
         0      65534          1
CapEff:	000001ffffffffff
$ sysctl -w net.ipv4.ip_forward=0
sysctl: permission denied on key "net.ipv4.ip_forward"
$ touch /opt/docs/x
touch: cannot touch '/opt/docs/x': Permission denied
```

输出里三件事各说一层：`uid=0` 与全开的 `CapEff` 说明**在 ns 内部它确实是 root，capability 是真的**（在它自己的 netns 里执行同样的 `sysctl`，实测 `rc=0`）；`uid_map` 那行 `0 65534 1` 是「ns 内的 0 映射到外面的 65534，且只映射这 1 个 uid」；`sysctl` 失败是因为这个 key 属于**宿主 netns**，内核检查的是 `ns_capable(current_user_ns(), CAP_NET_ADMIN)`——**capability 只在「拥有该对象的那个 user namespace」里有效**；`touch` 失败是因为 `/opt/docs` 属于宿主 root（uid 0），而 ns 里的 0 在宿主眼里是 65534。

所以准确的句子是：**「容器里的 root 是一个被降维的 root：位图没变，作用域变了。」** 这也解释了 `--privileged` 为什么危险——它做的就是**把作用域拆掉**：给全部 capability、关掉 seccomp/AppArmor、把宿主设备节点全映射进来。此时「容器」只剩一个 mnt namespace，隔离等于没有。

### 逃逸 = 内核漏洞，这是一条结构性的结论

所有容器共享同一个内核，所以**内核的 attack surface 就是所有容器的公共边界**。一个能让任意代码在 CPL=0 执行的漏洞，在这个模型里**不是「容器内提权」，而是「直接到宿主」**——容器里的 root 本来就已经在宿主内核里了，它缺的只是「内核里的一个 bug」。由此推出三条工程结论（防御视角）：

1. **容器的隔离强度 ≤ 宿主内核的安全强度。** 打补丁的紧迫性等同于「所有租户的安全」。
2. **减少内核暴露面等于提高隔离强度**：seccomp 白名单（在 syscall 入口用 BPF 按编号拒绝整次调用，实测被拦的 `getpid` 直接返回 `EPERM`、根本不执行）、丢弃全部 capability、只读 rootfs、`no_new_privs`、禁 `ptrace`/mount——它们直接缩小可被触发的那部分内核代码。注意 seccomp 只能拒绝「整次系统调用」，不能检查指针内容，所以它是「缩小攻击面」，不是「建立隔离」。
3. **当租户不可信且不能打补丁时，必须换边界**：gVisor（syscall 不落到宿主内核）或 microVM（每个租户一个内核），见 §15.7。手工构造见 base/34，攻防全谱见 base/33。

---

## 15.7 隔离强度三角：强度 × 启动 × 密度

### 五个档位，五个数量级

把「边界由谁实现」当成横轴，五档摆开（启动与开销为量级结论；进程档为本机实测，其余取自 [llm/29](../llm/29-sandbox-isolation.md) 的同一套口径）：

| 档位 | 边界由谁实现 | 攻击面 | 启动 | 单实例开销 | 64c/512G 宿主的密度 |
| --- | --- | --- | --- | --- | --- |
| 裸进程 | 无（只有 uid/capability） | 全部宿主 syscall + 全部内核 | 0.1–0.5 ms（实测 fork 0.1、exec 0.45） | 1–10 MiB | 10³–10⁴ |
| 容器 | 宿主内核 + ns/cgroup | 宿主内核 + 未 namespaced 的接口 | 5–20 ms（纯 ns+cgroup+exec）；`docker run` 端到端 0.3–0.8 s | 与进程同量级 + 镜像页缓存 | 按 2 核配额算 32；按 0.5 核算 100+ |
| gVisor | **用户态内核**（Sentry）拦截 syscall | Sentry + 少量真正下发的宿主 syscall | 0.3–1.5 s | +20–100 MiB | 受内存限制，约 30–80 |
| microVM | hypervisor + 最小设备模型 | hypervisor + guest 内核（各自独立） | 60–150 ms；快照恢复 5–30 ms | +5 MiB VMM + 30–50 MiB guest | 按 2 核配额算 32 |
| 物理机 | 固件 + 网闸 | BMC/固件/供应链 | 30 s–数分钟 | 0（整机） | 1 |

三个数字要会自己推：**进程档 = `fork` + `exec`**——fork 复制页表（≈100 μs，且随地址空间线性增长：把同样的测试放进一个已 `mmap` 512 MiB 的进程，单次 fork 涨到 5.7 ms，正好是 131072 个 PTE 的复制成本），exec 映射 ELF + 动态链接（≈450 μs）；**容器档 ≠ 镜像拉取**——ns + cgroup + `pivot_root` + exec 只要 5–20 ms，`docker run` 的 0.3–0.8 s 大半花在镜像层解压、网络配置与运行时握手；**microVM 靠「删设备」**——Firecracker 的设备模型只有 virtio-net/block/vsock 加一个串口，没有 BIOS、没有 PCI 枚举，所以能到 60–150 ms，快照恢复 5–30 ms。

### 三角不是刚性的：两条边都被打弯过

「强度越高、启动越慢、密度越低」是默认趋势，但两次工程突破各打弯了一条边：

- **gVisor 打弯了「强度 vs 启动」**：它不用 VM，而是用一个**用户态内核**（Go 写的 Sentry）接管 guest 的 syscall。syscall 不再进宿主内核，于是攻击面从「整个 Linux」缩到「Sentry + 少量宿主接口」，而启动仍然是「起一个进程」的量级（0.3–1.5 s，比容器慢但比 VM 快）。代价是 syscall 密集负载明显变慢——因为每次 syscall 现在变成了「用户态 trap + Sentry 里的软件实现」。
- **microVM 打弯了「强度 vs 密度」**：通过删设备、删 BIOS、共享内核镜像页（KSM/`MAP_PRIVATE`），单实例额外开销压到 5 MiB 量级，密度接近容器。

所以选型时不要问「哪个更强」，要问**三个独立的乘积项**：

```
   实际安全性 = 隔离强度 × 配额完整性 × 出口收敛性
                  ↑             ↑              ↑
              边界在哪层    cgroup 有没有配  能不能连内网
```

任何一项为 0，整体为 0。一个 microVM 里没有 `pids.max`，照样能 fork bomb 打满宿主 PID 表；一个 gVisor 沙箱能自由出网，照样能把数据送出去。**这是乘法，不是加法**——这也是 [llm/29](../llm/29-sandbox-isolation.md) 反复强调的那句话的物理来源：**隔离技术只买「逃逸难度」，买不到「资源」和「网络」。**

---

## 15.8 实测：把这台机器自己的边界量出来

### ① 我在哪一层：guest、虚拟化厂商、以及内核版本

```bash
uname -a
systemd-detect-virt
grep -m1 -o -E 'hypervisor|vmx|svm' /proc/cpuinfo
awk '/model name/{print; exit}' /proc/cpuinfo
```

```
Linux LegendVPS-... 6.12.113+deb12-cloud-amd64 #1 SMP PREEMPT_DYNAMIC ... x86_64 GNU/Linux
kvm
hypervisor
model name	: AMD EPYC 7K62 48-Core Processor
```

`hypervisor` 这个 CPUID 标志位是**硬件告诉软件「你在一台虚拟机里」**的方式；`systemd-detect-virt` 读到 `kvm`。所以我上面所有 syscall 数字，都是「guest 里的 syscall」——它已经包含了 VM 的成本（但不含 exit，因为 syscall 不 exit）。

### ② 我有什么权限：capability、seccomp、namespace inode

```bash
grep -E 'CapEff|NoNewPrivs|Seccomp' /proc/self/status
capsh --print | head -1
stat -Lc '%-24n inode=%i' /proc/self/ns/*
```

```
CapEff:	000001ffffffffff
NoNewPrivs:	0
Seccomp:	0
Current: =ep
/proc/self/ns/cgroup   inode=4026531835
/proc/self/ns/ipc      inode=4026531839
/proc/self/ns/mnt      inode=4026531841
/proc/self/ns/net      inode=4026531840
/proc/self/ns/pid      inode=4026531836
/proc/self/ns/user     inode=4026531837
/proc/self/ns/uts      inode=4026531838
```

`0x1ffffffffff` 是第 0–40 位全 1，即 41 个 capability 全开；`Seccomp: 0` 表示一个过滤器都没装；所有 namespace inode 都是初始值（`402653183x`，与 `pid 1` 相同）——**这个进程在宿主的全部 namespace 里，没有任何一层容器边界**。它的安全性 100% 来自「它在 KVM guest 里，且外面没有别人」。（把 uid 65534 映射成 ns 内 0 的那份 `uid_map` 实测在 §15.6。）

### ③ 成本阶梯的实测数字

同一个程序循环 2×10^6 次、`sched_setaffinity` 钉在 CPU0、取中位数：

```
call dummy        :    5.58 / 2.10 / 1.61 ns/op
syscall(getpid)   :  230.66 / 109.62 / 112.51 ns/op
clock_gettime     :   39.69 / 67.07 / 31.21 ns/op     ← vDSO，不跨界
pipe()+2*close    : 3827 / 5367 / 3107 ns/op
```

几行合起来就是全篇的成本模型：**同样的「取一个数」，走 vDSO 30 ns，走 syscall 110 ns，走两次上下文切换 2.5 μs，走一次缺页 1.8 μs。**

**跑不出来 / 没有跑的，明确说明**：VM exit 与 EPT violation 的 cycle 数需要 nested virtualization 或 host 侧 `perf kvm`，这台 guest 里量不到，本篇用的是量级结论；KPTI 的 1.5–3× 也测不了，因为本机 `meltdown: Not affected`，PTI 默认关闭；gVisor 与 Firecracker 不在本机，启动时间取 [llm/29](../llm/29-sandbox-isolation.md) 的同一套口径。

---

## 15.9 三列账：承诺了什么、付了什么、从哪开始崩

| 层 | 隔离承诺 | 代价 | 崩点 |
| --- | --- | --- | --- |
| 特权级 + 页表 | 用户态不能执行特权指令、不能读写内核页；比较由硬件每周期完成 | 每次合法跨界 100–300 cycle；页表走 4 级 | 内核里一个 bug 就能改写页表参数；侧信道绕过「不给你看」；SMEP/SMAP 未开时内核可被诱导解引用用户指针 |
| 微内核 | 驱动/文件系统/网络栈崩溃不拖垮内核，组件可热重启 | IPC 税 = 次数 × 500–1000 cycle；跨组件调试要跟消息流 | 高频路径（缺页、收包、元数据）上税吃掉几个核；驱动生态补不齐 |
| 硬件虚拟化 | guest 连自己的页表根都不真正拥有，内核漏洞被关在 VM 里 | VM exit 1000–3000 cycle；EPT 让 TLB miss 从 4 次访存变 20+ 次 | MMIO 与页表操作引发 exit 风暴；设备直通把攻击面交回 guest 驱动；hypervisor 自身漏洞 |
| 容器 | 看得见什么（ns）、能用多少（cgroup）、以为在哪（rootfs）各自可控 | 与进程同量级的开销；镜像与运行时带来供应链面 | **共享内核**：一个内核 LPE = 全宿主逃逸；`--privileged` 拆掉全部边界；cgroup 配额算错 |
| seccomp / caps | 把可达的 syscall 与 capability 缩到最小 | 维护白名单；误杀以 `EPERM` 出现在业务里 | 只拦 syscall 编号，不拦参数内容；被允许的调用仍可作恶 |
| gVisor / microVM | 把边界从「内核」上移到「用户态内核」或「hypervisor」 | 启动 0.1–1.5 s；syscall 密集负载变慢；生态有缺口 | 强度 × 配额 × 出口任一为 0 则整体为 0；只买逃逸难度，买不到资源与网络 |

一句话收束：**这一层买到的是「越界的代价」——对攻击者是代价，对你自己也是代价。** 每一次「加强隔离」都是在提高跨界的单价，所以要问的永远是「这个边界上每秒要过多少次」。

---

## 15.10 踩坑清单：现象 → 根因 → 定位 → 修 → 防复发

| 现象 | 根因（指回本篇） | 定位命令 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 容器里 `sysctl -w` 不生效或报 `permission denied` | 该 key 属于宿主 netns，capability 只在「拥有该对象的 user ns」里有效（§15.6）；`kernel.*` 根本不在 netns 里 | `readlink /proc/self/ns/net`；对比宿主与新 netns 的同名 key | 在**同一个 netns 内**设；或由宿主统一下发 | 把要调的 sysctl 列成清单并标注归属 ns；CI 断言「容器内改完，宿主不变」 |
| 容器里 `nproc` 显示 64，`cpu.max` 只有 2 核 | `nproc` 读 `sched_getaffinity`，cgroup 配额**不改 affinity**，只在 CFS 带宽层限流（§15.6） | `nproc`；`cat /sys/fs/cgroup/cpu.max`；`grep Cpus_allowed_list /proc/self/status` | 显式设 `GOMAXPROCS`/线程池；按配额算并发（2 核 → 4–8 worker，不是 64） | 启动时打印「配额 / affinity / 实际并发」三行；配额写进部署模板 |
| 容器 root 写的文件，挂载卷上变成 `nobody` | 用户映射把 ns 内 0 映射到宿主的非 0 uid（§15.6） | `cat /proc/self/uid_map`；`ls -n` 看数字 uid | 显式 `--user`；用 `fsGroup`/`chown` 对齐；或不用 root 跑 | 镜像禁用 root；CI 用非 0 uid 跑一遍挂载卷读写 |
| `/proc/cpuinfo`、`/proc/meminfo` 显示宿主的 64 核 / 512 GiB | 这些文件是全局的，不归任何 namespace 管（§15.6 的表里没有它们） | `grep -c processor /proc/cpuinfo`；对比 `cpu.max` | 用 lxcfs 之类的 FUSE 覆盖；或让应用读 cgroup 而不是 `/proc` | 统一「读配额」的封装，禁止用 `/proc/meminfo` 做容量决策 |
| 加了 `--privileged` 之后「一切都好了」 | 它拆掉了作用域：全部 cap、全部设备、关 seccomp/AppArmor（§15.6） | `capsh --print`；`ls /dev` 数设备节点；`grep Seccomp /proc/self/status` | 只加需要的那一个 cap（如 `CAP_NET_BIND_SERVICE`），`--device` 精确放行 | 准入控制**禁止** `privileged: true`；评审把「为修问题而放宽隔离」当红线 |
| 虚拟机上跑基准，syscall 比裸机慢一倍 | guest 里的 syscall 不 exit，但缓解措施、vCPU 调度、TLB 竞争都在加钱（§15.3、§15.5） | `systemd-detect-virt`；`perf stat -e cycles,instructions`；对比 `pti=on/off` | 裸机做绝对基准，虚拟机只做相对对比 | 基准报告必须写「虚拟化层 + 缓解措施状态」，否则数字不可比 |
| 设了 `cpu.max` 之后 p99 出现 100 ms 级尖刺 | CFS 带宽「每 period 发一次配额」，用完被 throttle 到下一个 period（§15.1 事故 2） | `cat /sys/fs/cgroup/cpu.stat` 看 `nr_throttled`/`throttled_usec` | quota 调到略高于实际峰值；或改用 `cpu.weight` | 监控 `nr_throttled`；压测必须覆盖「配额边界附近」的稳态 |

这张表有一条共同的元规则：**凡是「改了一个开关就好了」的修复，都要问一句「它拆掉的是哪一层边界」。** `--privileged`、`nopti`、`selinux=0`、关掉 seccomp，都是同一类动作——用隔离换便利，而账单在别处。

---

## 15.11 一页速记

```
【一句话】隔离不是「软件上禁止」，是「CS 低两位 + 页表 U/S 位」；
         微内核/VM/容器之争 = 边界画在哪一层 × 每次跨界付多少钱。

【三个硬件事实】
  ① CPL 在 CS 的低两位；x86 只用 0 和 3，ARM64 用 EL0/EL1/EL2/EL3
  ② 特权指令在解码阶段就 #GP（本机实测：SIGSEGV, si_code=128）
  ③ 内存访问看 PTE.U/S：内核页对用户态「可见但不可碰」

【一条成本阶梯】（本机实测，2.6 GHz）
  函数调用 1.6–5.6 ns  →  vDSO 31–67 ns  →  syscall 110–230 ns
  →  上下文切换 ≈2.5 μs  →  缺页 1.7–1.8 μs  →  VM exit 1000–3000 cycle
  →  fork 100 μs  →  exec 450 μs  →  容器 5–20 ms  →  microVM 60–150 ms
  →  gVisor 0.3–1.5 s  →  物理机 30 s+

【微内核的税】
  税 = 跨界次数 × 单次 IPC（500–1000 cycle）
  10^5 次/s 听不见；10^7 次/s 吃掉几个核
  L4/seL4 压到几百 cycle：寄存器传参 + 直接切换 + 惰性调度 + 不刷 TLB

【VM 的本质】
  用 ring -1 托管一个假的 ring 0；VMCS 存状态，EPT 做第二层翻译
  杀伤力 = exit 次数，不是 exit 单价 → virtio 批处理、posted interrupt

【容器的本质】
  同一内核上的三件事：namespace（看得见什么）+ cgroup（能用多少）
  + rootfs/pivot_root（以为在哪）；加 caps/seccomp/LSM 是策略，不是边界
  容器里的 root：位图没变，作用域变了（uid_map 0→65534 而 CapEff 全开）
  逃逸 = 内核漏洞，因为根本没有第二个内核

【三角】
  强度 × 启动 × 密度 是趋势，不是定律：gVisor 打弯启动，microVM 打弯密度
  实际安全性 = 隔离强度 × 配额完整性 × 出口收敛性（乘法，任一为 0 则 0）
```

---

## 15.12 自检 3 题

**Q1.** `clock_gettime(CLOCK_MONOTONIC)` 走 vDSO 只要 30 ns，强制走 `syscall(SYS_clock_gettime)` 要 150 ns。用「跨界」解释这 5 倍差距，并说明为什么 `getrandom` 没有 vDSO 版本。

<details><summary>答案</summary>

vDSO 的本质是**内核把「读时间」这段代码和一页数据只读映射进了用户地址空间**。调用它时 CPL 一直是 3：没有 `syscall` 指令、没有栈切换、没有 `pt_regs`、没有 CR3 切换（KPTI 下这一项尤其贵）。用户态直接读 `vvar` 页里的 `(sec, nsec)`，再用序列号判断「读的过程中内核有没有在写」（写者前后各加一次 seq，读者发现两次不相等就重试）。30 ns ≈ 一次函数调用 + 两次内存读 + 一次分支；150 ns 里绝大部分不是「读时间」，是「过边界」（§15.3 的 285 cycle 路径）。

`getrandom` 不能这么做，因为 vDSO 的前提是**「只读共享 + 内核单方面更新」**：时间可以被内核周期性写进 `vvar`，读者无副作用；而随机数必须**一次性消费**——放进共享页让用户态自己取，就没有「消耗」这个动作，多个进程会拿到同一个数，安全性归零。判据是：**凡需要内核侧状态改变的调用，都进不了 vDSO。**
</details>

**Q2.** 容器里 `capsh --print` 显示 41 个 capability 全开，但 `sysctl -w net.ipv4.ip_forward=0` 仍然 `permission denied`。矛盾吗？说出内核检查的那一行。

<details><summary>答案</summary>

不矛盾。capability 不是「进程身上的全局开关」，而是**「在某个 user namespace 内有效的权限」**。检查形如：

```c
if (!ns_capable(net->user_ns, CAP_NET_ADMIN)) return -EPERM;
```

关键是第一个参数：**被操作对象（netns）所属的 user namespace**，而不是「当前进程有没有这个 bit」。那 41 个 bit 是在容器**自己的 user ns** 里授予的；去改宿主 netns 的 `ip_forward` 时，`net->user_ns` 是初始 user ns，与 `current_user_ns()` 不是祖先/后代关系，检查失败。实测对照：在**自己的** netns 里执行同样的命令返回 `rc=0`；在 ns 内 `uid=0`、`CapEff` 全开，但 `touch` 宿主 root 的目录仍 `EACCES`——权限位比的是映射后的 65534，不是 ns 内的 0。

所以准确表述是：**「容器里的 root 是作用域被裁剪的 root：位图没变，能作用的对象变了。」** `--privileged` 之所以危险，就是它把作用域这一层拆了。
</details>

**Q3.** 某服务每秒 200 万次 `open()`，现在要把文件系统搬到一个用户态服务里（微内核化 / FUSE 化）。按本篇数字估这笔账，并说出什么条件下划算。

<details><summary>答案</summary>

宏内核一次 `open` ≈ 2–4 μs（syscall 285 cycle + VFS/ext4 逻辑）。搬到用户态后多出「请求 IPC + 回复 IPC」，按 L4 优化后的 500–1000 cycle/次算约 0.4–0.8 μs；若走 FUSE 那种「内核队列 + 两次调度」的实现，单次往返常见 10–30 μs。取乐观值 0.5 μs：

```
每秒多付 = 2×10^6 × 0.5 μs = 1.0 s CPU   → 恰好 1 个核
```

**2×10^6 次/秒的负载，微内核化要额外烧掉 1 个核**（FUSE 口径下是 20–60 个核，直接不可行）。比例上，原来 2×10^6 × 3 μs = 6 核，变成 7 核——**+17%**。这个数不大，说明「微内核一定慢一个数量级」是错的；但它是**纯开销**，不产生任何吞吐。

划算的条件（三条同时成立）：① **跨界频率可控**——能批处理、能缓存、能异步，把 2×10^6 次/s 压到 2×10^4 次/s，税就只剩 1%；② **隔离/可重启的价值 > 17% 的 CPU**——第三方代码、多租户不可信、热升级不重启宿主；③ **单次操作本身够重**——`open` 3 μs 里加 0.5 μs 是 17%，但 `getpid` 0.11 μs 里加 0.5 μs 是 450%，所以绝不能把「轻调用」搬到边界外。反过来，每秒十万级的低频率负载（嵌入式、汽车）税只有零点几个核，这正是 seL4 在那类场景成立的原因。
</details>

---

## 15.13 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 一次系统调用的逐行实现：`entry_SYSCALL_64`、`pt_regs`、返回路径的三条分支（本篇只给成本，路径在那一篇） | → base/21 |
| 页表四级结构、缺页的三种类型、TLB 与 PCID（本篇只用了 U/S 位与 CR3） | → base/26 |
| 权限位、缓冲区溢出、ASLR、seccomp 与逃逸的攻防全谱 | → base/33 |
| 手搓一个容器：`unshare` + cgroup v2 + `pivot_root` 三件套 | → base/34 |
| 三本账合订：位层 / 内核层 / 协议层各自的「承诺·代价·崩点」 | → base/56 |
| 沙箱四档的产品选型、配额与出口设计、microVM 生命周期 | [llm/29](../llm/29-sandbox-isolation.md) |
| 容器的部署侧：探针、limit、`GOMAXPROCS` 与 cgroup 对齐 | [go/52](../go/52-deployment-sre.md) |
| 全书总纲：四条主线与 56 篇清单 | [0-summary](0-summary.md) |
