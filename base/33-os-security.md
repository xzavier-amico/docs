# 33. 操作系统安全与攻防：权限位、缓冲区溢出、ASLR、seccomp 与逃逸

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 操作系统：Linux 视角 ｜ **编号**：33 / 56
> **本篇定位**：Part III 的第十三篇，也是这一部分唯一一篇把前十二篇的机制反过来问「它们各自在防谁」的。21 讲系统调用、23 讲进程、26 讲页表、28 讲文件、30 讲 IPC，都在描述机器怎么工作；本篇只回答——**当有人故意让这些机制按最坏方式工作时，哪一道边界先断**。推论链只有一条：`内核安全 = 权限模型 + 地址空间隔离 + 代码完整性` → `每次提权或逃逸都能指认是哪一根被绕过`。只讲机制与防御验证，**不提供任何可用的利用代码、提权步骤或绕过手法**。
> **读法**：§33.2 的总表是全篇索引；§33.3 的四个 uid 与 capability 危险等级、§33.4 的 ASLR 熵与三档开关、§33.5 的栈帧图是三处支点；§33.9 的五个实验请重跑一遍（都是防御侧观测，十分钟内完成）；赶时间只读 §33.2、§33.10、§33.11、§33.12。
> **不在本篇**：`syscall` 进入内核的逐行路径 → `base/21`；页表与缺页 → `base/26`；ELF 装载与 PIE 链接 → `base/22`；容器三件套的手工搭建 → `base/34`；网络协议攻击与「缺失的认证」总表 → `base/48`；`sudo`/polkit 完整配置、合规基线与渗透测试流程不在本套范围；密码学数学与 TLS 证书链 → `base/47`。

---

## 33.1 引子：一个「只读接口」读到了 /etc/shadow

2025 年第二季度，某内部文档平台的「在线预览」上线：只读、不接受上传、不接受写入，听起来风险很低。三周后，巡检在一个响应体里看到了 `/etc/shadow` 的第一行。核心逻辑缩到五行：

```c
/* 脱敏后的核心逻辑：只读预览，按 path 参数打开文件 */
char full[PATH_MAX];
snprintf(full, sizeof full, "%s/%s", DOC_ROOT, user_path);
if (strncmp(full, DOC_ROOT, strlen(DOC_ROOT)) != 0)
    return 403;                       /* 前缀检查：看起来只允许 DOC_ROOT 下 */
int fd = open(full, O_RDONLY);        /* 内核在这里解析 .. 与符号链接 */
```

| 层 | 看起来做了什么 | 实际发生了什么 | 对应哪根柱子 |
| --- | --- | --- | --- |
| 输入检查 | `strncmp` 确认拼出的路径以 `DOC_ROOT` 开头 | 检查的是**未规范化**字符串；`..` 在检查之后才由内核 VFS 解析，前缀仍成立 | 权限模型：判断对象错了 |
| 进程身份 | 服务由 systemd 拉起，方便读所有用户的文档 | 进程是 `uid=0`，DAC 不设防；连 `CAP_DAC_OVERRIDE` 都不需要 | 权限模型：主体权限过大 |
| 授权粒度 | 「在 `DOC_ROOT` 下」被当成「这个调用者有权看」 | 服务替**所有**调用者读**所有**文件，是典型的混淆代理 | 权限模型：没有「谁」只有「哪」 |
| 文件视图 | 服务以为自己「只在文档目录里工作」 | mount namespace 里整个根文件系统都在，`/etc/shadow` 一直可达 | 地址空间隔离：视图没收窄 |

这次事故**只倒了第一根和第二根柱子**，第三根（代码完整性）根本没被触碰——没有溢出、没有改返回地址。**倒一根就够出事了。** 所以「我们没写危险代码」不是安全论证：危险的不是有没有 `strcpy`，而是这段代码被授予了多大权力、能看见多大的世界。

修复不是「过滤 `..`」。过滤字符串是在和内核路径解析器打一场永远输的仗：编码、符号链接、挂载点、大小写、`//`、尾随点，都能让「检查时的字符串」与「打开时的对象」不是同一个东西。四条正解：服务降到专用 uid 且 `DOC_ROOT` 只读挂载；接口只收文件 ID，路径由服务端查库；用 `openat2` + `RESOLVE_BENEATH` 让内核在解析时拒绝越界（§33.6）；每个对象做属主/租户校验。只有最后一条是权限模型的正解，前三条是「让越权即使发生也无害」。**安全工程的大部分工作不是证明边界不会破，而是让边界破了也不至于直达核心。**

---

## 33.2 三根柱子总表：每一次提权或逃逸，都是其中一根被绕过

三根柱子不是三个产品，而是三个**假设**；系统只要还在运行，就一直在替它们背书。

| 柱子 | 它保证什么 | 它依赖什么 | 绕过它的经典类别 | 本篇 |
| --- | --- | --- | --- | --- |
| **① 权限模型** | 每次访问都能回答「谁、对哪个对象、做哪种操作」，且答案由可信代码给出 | `uid`/`euid`/`fsuid`、mode 位、capability、LSM、服务自身的授权逻辑 | 混淆代理、检查缺失、`setuid` 程序漏洞、TOCTOU、capability 过授、`sudo` 过宽 | 33.3、33.6、33.11 |
| **② 地址空间隔离** | 用户态只能读写自己页表映射的页；内核只碰经过检查的用户指针；进程互不可见 | MMU/TLB、页表 U/S 与 NX 位、ring 3、KPTI、SMEP/SMAP、`copy_from_user`/`access_ok` | 内核漏一次用户指针校验、同进程内越界改指针或返回地址、信息泄露打掉 ASLR、共享内核侧信道 | 33.4、33.5、33.7 |
| **③ 代码完整性** | 执行的指令来自预期映像；控制流不被数据改写；盘上与内存里是同一份 | 签名/校验和、只读段与 RELRO、PIE、canary、CFI/CET、IMA/dm-verity、只读挂载 | 供应链投毒、ROP/JOP 复用已有代码、data-only 攻击、可写可执行映射 | 33.4、33.5、33.8 |

粒度必须分清，否则会得出错误结论：

- **权限模型是逻辑边界**：不依赖硬件，依赖「检查代码有没有写错」。它最灵活，也最容易写错；一个 `if` 写反，整根柱子就没了。
- **地址空间隔离是硬件边界**：MMU 每个访存周期查页表，软件只负责填表。**但它隔离的是进程，不是变量**——同一个栈页里的 `buf[16]` 和返回地址，在硬件看来是同一块可写内存（§33.5）。
- **代码完整性是来源 + 控制流**：来源回答「这段代码是不是我发布的那份」，控制流回答「它有没有被数据带到别处执行」。canary、PIE、RELRO、CFI 都属于后者；它们不阻止写，只让「写到控制流上」变得可检测或不可用。

常见误解是把「缓解手段」当成「柱子」：以为开了 ASLR 就有了地址空间隔离，以为开了 canary 就有了代码完整性。事实相反——**ASLR 是概率，canary 是检测，NX 是权限位；它们都只提高攻击成本，不建立授权关系。** 真正的硬边界只有两条：页表 U/S 位（进程/内核之间）与权限检查的结论（主体/客体之间）。其余都是在这两条边界上加的「绊线」。

---

## 33.3 权限模型：四个 uid、九个位、四十个 capability

### 身份不是一个数，是四个

| ID | 名字 | 回答什么问题 | 什么时候变 |
| --- | --- | --- | --- |
| `ruid` | real uid | 谁启动了我 | `login`/`fork` 继承；`setuid` 可改（需权限） |
| `euid` | effective uid | 现在做权限检查时我是谁 | `execve` 一个 setuid 文件时变成文件属主 |
| `suid` | saved uid | 我「本来」是谁，用来提权后降回去 | `execve` 时保存，可在 `ruid`/`euid`/`suid` 间切换 |
| `fsuid` | filesystem uid | 访问文件时内核实际拿谁去比对 | 通常等于 `euid`；`setfsuid` 可单独改（Linux 特有） |

```console
$ id
uid=0(root) gid=0(root) groups=0(root)
```

四个 ID 全是 0。**在 root 下做「权限实验」没有说服力**——DAC 对 root 基本不设防；要观察权限模型，必须用普通用户跑，或 `setpriv --reuid=1000 --regid=1000 --clear-groups` 降下去。

### 文件权限与三个特殊位

`ls -l` 第一列 10 个字符：类型 + 三组 `rwx`（owner/group/other），每组按 `r=4,w=2,x=1` 相加得八进制一位。`0644` 是属主读写、其他人只读；`0600` 只有属主读写；`0755` 用于目录和可执行文件；`1777` 是所有人可写但只有属主/目录属主/root 可删（`/tmp`）。`/etc/shadow` 是 `0640 root:shadow`（33.9 有实测），普通用户连哈希都拿不到。**这就是为什么 33.1 的只读接口一旦以 root 运行，读 shadow 不需要任何「提权」——它只是在行使已有的权力。**

| 位 | 八进制 | 放在文件上 | 放在目录上 |
| --- | --- | --- | --- |
| `setuid` | `4000` | `execve` 后 `euid` 变成文件属主（脚本无效） | Linux 忽略 |
| `setgid` | `2000` | `execve` 后 `egid` 变成文件属组 | 新文件/子目录继承组，子目录自动带 setgid |
| `sticky` | `1000` | Linux 忽略 | 只有文件属主、目录属主、root 能删除/改名（`/tmp`） |

本机 `/usr/bin/passwd` 是 `-rwsr-xr-x root root`；`/tmp` 是 `drwxrwxrwt`。`passwd` 的 `s` 在 owner 的 `x` 位：普通用户执行它，`euid` 变成 0，于是能写 `/etc/shadow`；退出后回到自己的 uid。**这是 Linux 上唯一一条被设计出来的、从普通用户到 root 的常规通道**——不是漏洞，是功能；也正因如此，它成了攻击面最集中的地方。setuid 对脚本无效（内核实际执行解释器）；sticky 只保护删除/改名，不保护读。

`umask` 是创建时的掩码：`最终权限 = 请求权限 & ~umask`。本机实测 `umask 0022`：`touch` 请求 `0666` 得 `0644`，`mkdir` 请求 `0777` 得 `0755`；`umask 0077` 时 `touch` 得 `0600`。它是**按位与，不是减法**。三条纪律：不影响 `chmod`，只影响创建；子进程继承；多用户机器上 `0022` 是「默认公开」，私密文件要显式 `0600`。

### capability：把 root 的「全有」拆成四十个「单项」

```console
$ capsh --print | grep -E '^(Current|Bounding)' | cut -c1-96
Current: =ep
Bounding set =cap_chown,cap_dac_override,cap_dac_read_search,cap_fowner,cap_fsetid,cap_kill,cap_

$ getcap /usr/bin/ping /usr/bin/mtr-packet
/usr/bin/ping cap_net_raw=ep
/usr/bin/mtr-packet cap_net_raw=ep
```

`Current: =ep` 读作「Effective 与 Permitted 都等于 Bounding set」；`Bounding set` 是 41 个 capability 全开。**容器里的进程可以拥有全部 capability，但它仍在宿主内核的同一个权限模型里；capability 只能限制「能做什么」，不能限制「能看见谁」。** Permitted 是上限，Effective 是当前生效的，Bounding 是 exec 带 file capability 的文件时最多能获得的。

| capability | 打开什么 | 等级 | 为什么 |
| --- | --- | --- | --- |
| `CAP_DAC_OVERRIDE` | 绕过文件读/写/执行权限（执行仍需一个 x 位） | 高 | 改 `/etc/passwd`、写任意配置、读 `/etc/shadow` |
| `CAP_SETUID` / `CAP_SETGID` | 任意改 uid/gid | 高 | 直接变成 root；最短的提权路径之一 |
| `CAP_NET_RAW` | 原始套接字、抓包、构造以太网帧 | 中高 | ARP/DHCP 欺骗、流量嗅探；`ping` 只需要它 |
| `CAP_SYS_PTRACE` | `ptrace` 任意进程、读写其内存 | 高 | 等价于向目标进程注入代码 |
| `CAP_SYS_ADMIN` | 挂载、多数 `unshare`、`pivot_root`、大量 `ioctl`、`keyctl` | **极高** | 「新 root」：不是一个操作，是几十个入口的集合 |
| `CAP_SYS_MODULE` | 加载/卸载内核模块 | **极高** | 内核模块就是 ring 0 代码 |

`CAP_SYS_ADMIN` 危险是因为它把挂载、命名空间、`pivot_root`、设备 `ioctl`、`keyctl`、`quotactl` 这些本该分开授权的入口打包成了一个位。一个只需要「挂个 tmpfs」的服务被授予它，顺带就拿到了「改 mount namespace、进别的 namespace、调大量驱动 ioctl」的能力。**capability 的粒度只到「功能组」，不到「这一个操作」。**

### setuid 程序为什么是提权的高危面

`setuid` 的危险是结构性的：它是唯一一条被设计出来的「低权限 → 高权限」通道（`sudo`、polkit 本质相同，只是把 setuid 换成了 IPC）；它的全部输入都不可信（`argv`、环境变量、stdin、文件内容、当前目录、`TMPDIR`、`HOME`、信号）；它一旦有内存安全 bug（§33.5、§33.6），直接把 `euid=0` 交出去；而且它很难最小权限化。

所以现代发行版减少 setuid，改用 file capability 或提权服务。`ping` 就是例子：老版本 `setuid root`，新版本 `cap_net_raw=ep`（本机实测见上）。前者一旦有溢出就是 root，后者即使被打穿也只拿到「发原始包」这一项。**这不是「更安全一点」，是把「全有或全无」换成了「按需授予」。** 防御者每周用 `find / -xdev -perm -4000 -type f` 与 `getcap -r /` 盘一遍：两张清单加起来，就是这台机器上「不需要密码就能改变身份」的全部入口。

---

## 33.4 地址空间隔离：页表里的一个 bit，和三层「提高成本」的缓解

### 机制：MMU 每个访存周期查表，软件只负责填表

进程看到虚拟地址（VA），CPU 通过 MMU 查页表翻成物理地址（PA）。页表项四个关键位：Present（是否映射）、Writable（是否可写）、User/Supervisor（用户态能否访问）、NX（能否取指执行）。**地址空间隔离不是「内核检查了地址」，而是「MMU 每个访存周期自动比较」。** 用户态代码没有任何办法绕过 U/S 位——它连「读一下页表」都做不到（页表本身也在内核页里）。内核态可以访问用户页，所以内核必须遵守一条纪律：**凡是解引用用户指针，都要先 `access_ok` 再 `copy_from_user`/`copy_to_user`。** 这不是 MMU 能自动保证的，漏掉一次就是「内核替用户读了不该读的地址」（§33.7）。SMEP/SMAP 是硬件补强；KPTI 在用户态运行时把大部分内核页从页表里摘掉，代价是每次进内核换 CR3。

### ASLR：随机化的是基址，不是段内偏移

ASLR 不做隔离，只做一件事：**让攻击者猜不中地址**。每次 `execve`，内核给栈、mmap 区、堆、PIE 可执行文件挑一个随机基址；段内偏移不变。本机实测（同一程序跑两次）：

```console
$ ./aslr
stack=0x7ffd2667a1d8 heap=0x55aab3f2f2a0 data=0x55aa797f4024 text=0x55aa797f1149
$ ./aslr
stack=0x7ffdf85d9228 heap=0x5634ac9ff2a0 data=0x563489eae024 text=0x563489eab149
```

`stack` 两次差了约 `0x7bf...`，`heap`/`data`/`text` 整体换了 `0x5...` 量级，但 `text - data`、`heap - data` 的差值不变。**这正是 ASLR 的全部内容：基址随机，相对布局固定。**

| 区域 | 随机化 | 熵 / 范围（默认） | 说明 |
| --- | --- | --- | --- |
| 栈 | 是 | 22 bit ≈ 4,194,304 个位置，16 GiB | `STACK_RND_MASK` 22 位，页粒度 |
| mmap / 共享库 / PIE | 是 | 28 bit ≈ 2.68×10⁸ 个位置，1 TiB | `vm.mmap_rnd_bits=28`（本机实测） |
| 堆（brk） | `randomize_va_space=2` 时是 | 偏移范围远小于 mmap | 在 brk 起点附近加随机偏移 |
| 非 PIE 的 text | 否 | 固定在 `0x400000` 附近 | `readelf -h` 的 `Type: EXEC`；PIE 要解决的就是它 |
| 内核 text | 是（KASLR，启动时） | 启动时一次 | 独立于用户态 ASLR；`nokaslr` 可关 |

数字可以推导：本机 `mmap_rnd_bits=28`，页大小 4 KiB = 2¹² B，随机范围是 `2²⁸ × 2¹² = 2⁴⁰ B = 1 TiB`；栈是 22 位页号，`2²² × 2¹² = 2³⁴ B = 16 GiB`。**熵是「位置数」的以 2 为底的对数：28 位就是 2²⁸ 种可能，不是 28 种。**

| 值 | 含义 | 随机化了什么 | 典型场景 |
| --- | --- | --- | --- |
| `0` | 关闭 | 什么都不随机 | 调试、某些老式 JIT、复现实验 |
| `1` | 保守 | mmap、栈、vDSO、共享库；**不含** brk 堆 | 少数兼容性要求 |
| `2` | 完整（默认） | 在 1 的基础上加 brk 堆 | 绝大多数生产系统 |

```console
$ cat /proc/sys/kernel/randomize_va_space
2
$ cat /proc/sys/vm/mmap_rnd_bits
28
```

ASLR 的边界：**它是概率，不是隔离**（fork server 每次继承同一布局、服务自动重启布局不变时，猜中概率会被放大）；**挡不住信息泄露**（一个打印指针的格式化字符串、一次越界读，就能把基址泄露出来，而段内偏移固定）；**不保护数据**（data-only 攻击根本不需要知道代码地址）；**不替代 NX/canary**（三者挡的是不同环节：ASLR 挡「知道去哪」，NX 挡「注入的代码能执行」，canary 挡「线性覆盖返回地址」）。

### NX / DEP 与 canary

NX 在 x86-64 上是页表项 bit 63（需 `EFER.NXE`），ARM64 上是 PTE 的 XN 位。它把「可写」和「可执行」拆开：栈堆默认可写不可执行，代码段可执行不可写。ELF 里由 `PT_GNU_STACK` 程序头表达；本机默认二进制是 `RW`，`gcc -z execstack` 会变成 `RWE`（真实输出在 §33.9）。**链接器把意图写进 ELF，内核在 `execve` 装载时据此决定栈的 `VM_EXEC` 标志。** NX 挡「把 shellcode 写进栈/堆然后跳过去执行」；不挡 ROP/JOP 和 `ret2libc`——这些复用已有可执行页里的代码。

### 一张表：每一层挡什么、不挡什么

| 层 | 挡住的攻击类别 | 不挡什么 | 成本 / 性质 |
| --- | --- | --- | --- |
| 页表 U/S + ring 3 | 用户态直接读写内核页、跨进程读写 | 内核自己漏检查用户指针；同进程内越界 | 硬件，必需 |
| ASLR | 硬编码地址的 ret2libc/ROP | 信息泄露、fork server 继承布局、data-only 攻击 | 概率，不是隔离 |
| NX / DEP | 栈/堆注入代码执行 | ROP/JOP、ret2libc、JIT 页 | 硬件位，较硬 |
| canary | 线性覆盖返回地址 | 任意写、堆溢出、信息泄露、非尾声劫持 | 检测，不是阻止 |
| PIE + RELRO | 固定地址覆写 GOT | 相对偏移利用、信息泄露后计算 | 编译/链接选项 |

横着读，没有一行能单独成立；竖着读，只有页表 U/S 是「硬隔离」，其余都是「提高成本」或「事后检测」。**不能把「开了 ASLR」当成「有了隔离」，就像不能把「装了监控」当成「锁了门」。**

---

## 33.5 缓冲区溢出：栈帧里没有「变量边界」这回事

### 栈帧布局：`buf` 和返回地址住在同一页

一个函数的栈帧（x86-64 System V，调用约定与栈帧的完整推导在 `base/12`）：

```
高地址
        ┌──────────────────────────┐
rbp+8   │ 返回地址 (8 B)            │  ← ret 从这里取走 8 字节
        ├──────────────────────────┤
rbp     │ 保存的 rbp (8 B)          │  ← leave 时恢复
        ├──────────────────────────┤
rbp-8   │ canary (8 B)              │  ← 序言从 fs:0x28 读入
        ├──────────────────────────┤
rbp-0x10│ 对齐填充 (8 B)            │
        ├──────────────────────────┤
rbp-0x20│ buf[16]                   │  ← strcpy 从这里向高地址写
        └──────────────────────────┘
低地址
```

**在硬件看来，`buf`、canary、保存的 rbp、返回地址是同一页里的连续字节，没有任何「这是数组、那是控制数据」的标记。** 页表的 Writable 位只说明「这一页可写」，不说明「写到第 17 个字节就越界了」。对象边界是 C 语言和程序员的约定，不是 MMU 的属性。

所以机制只有一句话：**一个没有长度参数的复制函数，从低地址向高地址写，写过 `buf` 末尾，就会依次覆盖 canary、保存的 rbp、返回地址。** `ret` 从栈顶弹 8 字节进 `rip`；那 8 字节若已被数据改写，控制流就去了数据指定的地方。**本节只讲机制，不给出任何偏移计算、利用串或绕过步骤。**

### canary 如何检测：序言与尾声的一对访存

`-fstack-protector-all` 编出来的 `overflow_demo`，反汇编里能直接看到这对访存（本机实测）：

```asm
0000000000001179 <overflow_demo>:
    1185:	mov    %fs:0x28,%rax        ← 序言：从 TLS 取 canary
    118e:	mov    %rax,-0x8(%rbp)      ← 放到 buf 与 saved rbp 之间
    1198:	lea    -0x20(%rbp),%rax     ← buf 的地址
    11a2:	call   1030 <strcpy@plt>   ← 没有长度参数
    11c3:	mov    -0x8(%rbp),%rax     ← 尾声：取回栈上的 canary
    11c7:	sub    %fs:0x28,%rax       ← 与 TLS 原值比较
    11d0:	je     11d7                ← 相等才正常返回
    11d2:	call   1060 <__stack_chk_fail@plt>  ← 不等就 abort
```

**canary 不阻止写，它只检测写。** 进程仍然会死（可用性损失），但控制流没有被劫持。这是「检测型缓解」和「阻止型机制」的区别：前者用「同归于尽」换「不被利用」，后者根本不让你写。

### 为什么 `strcpy` / `gets` 是源头

`gets` 完全没有长度参数（已从 C11 移除）；`strcpy`/`strcat`/`sprintf` 的目标缓冲区大小不在参数里，函数无法知道何时该停；`scanf("%s")` 没有宽度限制；`memcpy(dst, src, n)` 的 `n` 由计算得来，可能溢出或为负（§33.6）。一条通用规则：**任何「目标缓冲区大小不在参数里」的 API，都必须被当作不可信。** 它们把「目标够不够大」的责任推给调用者；调用者算错，函数会忠实地写坏内存。

### 防御验收：在复制前检查边界

栈保护是后备检测，不替代容量检查。下面的接口只在目标容量足够时复制一条已保证以空字符结束的字符串；调用者必须保证源与目标都是有效、互不重叠的对象。若输入来自网络，先用带长度的接口约束读取，再校验文本边界，不能直接对任意网络字节调用 `strlen`。

```c
#include <stddef.h>
#include <string.h>

int copy_name(char *dst, size_t cap, const char *src) {
    size_t n = strlen(src);
    if (n >= cap) return -1; /* 为终止符留一个字节 */
    memcpy(dst, src, n + 1);
    return 0;
}
```

防御回归要覆盖空串、`cap−1`、`cap`、缺少终止符的外部输入，以及转换前的负长度。对于已有二进制，用编译参数与反汇编确认插桩，不通过触发未定义行为来证明生产安全。

要纠正一个流传很广的说法：**`stack smashing detected` 是 glibc 的 `__stack_chk_fail` 输出到 stderr 的消息，不是内核 `dmesg` 的消息。** 内核日志里通常是 `traps:`/`segfault at ...`，或者什么都没有（取决于 `dmesg_restrict`、容器配置、journald）。本机在容器内 `dmesg | grep -i "stack smashing"` 没有输出：这条消息本来就不进内核日志，且容器内 `dmesg` 受 `CAP_SYSLOG` 限制。定位这类崩溃要用 `coredumpctl list`、`journalctl -k`、`ulimit -c unlimited` 后的 core 文件。

加固组合按攻击面选：面向网络的解析器用 `-fstack-protector-all -D_FORTIFY_SOURCE=3 -fPIE -pie -Wl,-z,relro,-z,now`，再加 ASan/UBSan 跑 fuzz；内部批处理至少 `-fstack-protector-strong -fPIE -pie -Wl,-z,relro,-z,now`。

---

## 33.6 整数与边界：检查的算术域，必须等于使用的算术域

缓冲区溢出只是「写过了头」的一种。更常见、更隐蔽的一类是：**长度本身算错了，或者检查时用的类型和使用时用的类型不是同一个。** 共同形状是「检查通过，但被检查的值不是真正被使用的值」。

### 长度计算溢出 → 堆溢出

两个检查函数：`check_bad(int len) { return len + 16 <= 64; }` 与 `check_good(size_t len) { return len <= 64 - 16; }`，输入 `len = 2147483639`（`INT_MAX - 8`）。本机实测：

```console
$ gcc -O0 -o lenbug lenbug.c && ./lenbug
len = 2147483639
check_bad(len)  = 0   (len+16 = -2147483641)
check_good(len) = 0
use  as size_t  = 2147483655

$ gcc -O0 -fwrapv -o lenbug_wrap lenbug.c && ./lenbug_wrap
len = 2147483639
check_bad(len)  = 1   (len+16 = -2147483641)
check_good(len) = 0
use  as size_t  = 2147483655
```

两次相反，原因不在「溢出有没有发生」，而在「编译器怎么解释溢出」：默认情况下有符号溢出是 UB（`base/03`、`base/04`），编译器可以假设它不发生，把 `len + 16 <= 64` 代数化简成 `len <= 48`——反汇编里能直接看到 `cmpl $0x30,-0x4(%rbp)`（`0x30` = 48）。加 `-fwrapv` 后，编译器按补码回绕，`len + 16` 变成负数，`<= 64` 成立，检查返回 1——**检查通过了，但长度是负的**。

两种结果都错：前者把检查换成了另一个检查，后者让负数通过。随后如果这个值传给 `malloc`/`memcpy`（参数是 `size_t`），负值就变成 `2⁶⁴ - n` 量级，堆溢出发生。**根因不是「忘了检查」，是检查的算术域与使用的算术域不一致。** 正确写法是先把长度扩到能容纳最坏中间结果的位宽，用 `len > 64 - 16` 先定上界再减，或 `__builtin_add_overflow` 先问「会不会溢出」。分配侧的 `malloc(n * size)` 同理会乘法溢出，用 `calloc(n, size)`、`reallocarray` 或 `__builtin_mul_overflow`。堆溢出比栈溢出更难检测：**堆上没有 canary**，检测依赖分配器加固与 ASan。

### 符号 / 无符号比较：负长度怎么绕过上界检查

本机实测 `int len = -1`：`len > 4096` 为 `false`，`(size_t)len` 为 `18446744073709551615`，`len < (int)16` 为 `true`。第一行是核心：**`-1` 不大于 4096，所以「只检查上界」的代码会让它通过。** 第二行是后果：同一个 `-1` 传给 `size_t` 参数就是 `2⁶⁴ - 1`。第三行是另一种写法：把上界强转成 `int`，`-1 < 16` 又成立。三种错法同源：**比较域与使用域不一致**——只查上界不查下界、比较用 `int` 使用用 `size_t`、或对无符号做 `len < 0` 的死检查。

正确写法：**在边界处就把长度解析成足够宽的有符号类型，做完整区间检查 `0 <= n && n <= CAP`，再转 `size_t` 使用。** 转换只发生一次，且发生在检查之后。看到 `memcpy`/`malloc`/`read` 的参数是 `size_t`，就往上追来源，确认它在变成 `size_t` 之前已经被区间检查过。

### 路径穿越：`..` 是内核解析的，不是你检查的

回到 33.1。路径穿越的本质不是「`..` 这个字符串很危险」，而是**「检查时的路径字符串」和「打开时的文件对象」之间隔了一次内核解析**。应用看到 `"/data/docs/../../etc/shadow"`，内核看到「从 `/data/docs` 向上两级，再到 `/etc/shadow`」；`strncmp` 检查前者，`open` 用后者。变体有三类：`../`（编码、`//`、大小写、二次编码都能变形）、符号链接（检查时是普通路径，解析时跟随链接）、TOCTOU（检查通过后、打开之前路径分量被替换）。

正确做法不是过滤 `..`，而是**让内核在解析的同时执行边界**。Linux 5.6+ 的 `openat2` 提供 `RESOLVE_BENEATH`；本机实测（`jail/` 内有 `note.txt`，`jail/` 外有 `outside.txt`）：

```c
struct open_how how = { .flags = O_RDONLY, .resolve = RESOLVE_BENEATH };
int fd = syscall(SYS_openat2, dirfd, "../outside.txt", &how, sizeof how);
/* fd = -1, errno = EXDEV (18)；同样路径的普通 openat 会成功 */
```

```console
$ gcc -O0 -o safeopen safeopen.c && ./safeopen
jail/note.txt -> fd=4
jail/../out   -> fd=-1 errno=18 (Invalid cross-device link)
plain openat  -> fd=5 errno=Success
```

`RESOLVE_BENEATH` 拒绝越出 `dirfd` 的解析，返回 `EXDEV`（18）——这是 `openat2` 的历史约定，不是「跨设备」的字面意思。普通 `openat` 则成功打开了外部文件。**差别不在「有没有检查 `..`」，而在「检查和解析是不是同一个原子动作」。** 同族标志还有 `RESOLVE_IN_ROOT`（把 dirfd 当 `/`）、`RESOLVE_NO_SYMLINKS`、`RESOLVE_NO_MAGICLINKS`、`RESOLVE_NO_XDEV`。`chroot` 不是安全边界，它只改「根目录」这个视图；真正能当沙箱用的是 `pivot_root` + mount namespace + 丢弃 `CAP_SYS_ADMIN`，`base/34` 会手工搭一遍。

---

## 33.7 内核攻击面：所有容器共享同一段 ring 0 代码

内核是唯一一段「所有进程共享、且运行在 ring 0」的代码。它的攻击面不是「有多少行代码」，而是「有多少个入口能让用户态递进去一个它控制的值」：

| 入口 | 用户能控制什么 | 内核必须校验什么 | 典型失效 |
| --- | --- | --- | --- |
| 系统调用 | 指针、长度、标志、嵌套结构体 | `access_ok`、`copy_from_user` 返回值、长度算术、标志组合 | 漏一次检查；检查与使用不一致；compat 层结构体大小不同 |
| `ioctl` | 命令号 + 任意结构体 | 命令白名单、`_IOC_SIZE`、版本、并发 | 驱动私有 ABI 没有统一校验，命令号空间巨大 |
| `/proc`、`/sys` | 写入的字符串/整数 | 权限位、解析、范围 | 可写项权限过宽；写入即改变全局安全姿态 |
| `ptrace` | 目标 pid、寄存器、内存 | 关系检查（同 uid / Yama）、capability | 把「同 uid」当成「可信」 |
| `userfaultfd` | 注册区间、缺页事件 | 权限、范围 | 把缺页当成可控暂停点，扩大竞态窗口 |
| eBPF | 程序字节码、map、helper 调用 | verifier 证明「不越界、不死循环、不读内核地址」 | verifier 自身 bug；过度授权 |
| 文件系统 / 网络栈 | 恶意 superblock、包头部、分片 | 每个字段的范围、长度、状态机 | 挂载即攻击；分片重叠、选项解析 |

**系统调用**：内核不能直接解引用用户指针——用户可能传内核地址、未映射地址，或「检查时合法、使用时被换掉」的地址。标准动作是 `access_ok` → `copy_from_user`（返回**没复制成功的字节数**）→ 检查返回值；失败时清零只防泄露，不代表后续逻辑可以继续用这个缓冲区。长度算术同样要防 `len * sizeof(struct)` 溢出与 `len + 1` 回绕。

**`ioctl`** 是设备/文件系统/网络栈的私有控制通道。`cmd` 编码了方向、大小、类型和序号，但**内核没有全局表证明「这个 cmd 对应的结构体一定是这个大小」**；每个驱动自己解析 `arg`。防御：容器里不传设备节点、用 seccomp 收敛 `ioctl` 命令、对必须用的设备走 device cgroup allowlist。

**`/proc` 与 `/sys`** 的可写项是内核变量的视图。`kernel.core_pattern` 可配成管道让内核在崩溃时执行程序，`kernel.modprobe` 决定加载模块时执行什么；`vm.unprivileged_userfaultfd`、`kernel.unprivileged_bpf_disabled` 直接决定两个攻击面开不开。容器里默认把 `/proc/sys` 挂成只读。

**`ptrace`** 让一个进程读写另一个进程的寄存器与内存；默认「同 uid 可 attach」在多租户里就是灾难。Yama 四档 `kernel.yama.ptrace_scope`：`0` 经典（本机实测）、`1` 只能 attach 后代（多数发行版默认）、`2` 只有 `CAP_SYS_PTRACE`（服务器推荐）、`3` 禁止。`--pid=host` + `CAP_SYS_PTRACE` 基本等于宿主 root，**不要同时出现**。

**`userfaultfd`** 让用户态接管缺页，合法用途是用户态文件系统、热迁移；副作用是能把线程精确暂停在「内核正在复制用户数据」的中间，拉长竞态窗口。所以 `vm.unprivileged_userfaultfd` 默认 0（本机实测），只有带 `CAP_SYS_PTRACE` 的进程能用。

**eBPF** 把字节码加载进内核 JIT 执行，安全全靠 **verifier** 证明「不越界、不死循环、不读未初始化内存」。verifier 有 bug，eBPF 就从观测工具变成攻击面。Linux 5.8 起拆出 `CAP_BPF`/`CAP_PERFMON`；本机 `kernel.unprivileged_bpf_disabled = 2`（对非特权永久禁用）。

### 为什么容器逃逸几乎总是内核漏洞

容器 = **namespace（看见什么）+ cgroup（能用多少）+ rootfs（以为自己在哪）+ 一组安全配置（capability/seccomp/LSM）**；它没有自己的内核，容器里的 `syscall` 进入的是宿主内核的同一段代码（`base/34` 会验证）。「逃逸」只有两种：① **本来就没有边界（配置错误）**：`--privileged`、挂载宿主 `/`、挂 `docker.sock`、`--pid=host` + `CAP_SYS_PTRACE`、传设备节点——这不是逃逸，是「门没关」；② **内核漏洞（真逃逸）**：默认配置下，容器只能通过 syscall/`ioctl`/文件系统/网络栈与内核交互，任何一处校验缺失都可能拿到宿主内核权限。**因为内核共享，一次成功的内核利用同时穿过三根柱子**：权限模型（内核自己就是最高权限）、地址空间隔离（ring 0 能访问所有页）、代码完整性（控制流被改到内核 gadget）。

这也解释了为什么容器比 VM 弱：VM 的边界是 hypervisor + 独立 guest 内核，打穿 guest 只拿到 guest；容器打穿内核就是宿主。**隔离强度不由「名字叫容器」决定，而由「边界画在哪一层」决定**（`base/15`）。多租户 hostile 场景：跟上内核安全更新、最小 capability、seccomp allowlist、user namespace、LSM、只读 rootfs、不传设备、不挂 `docker.sock`；再强一档用 gVisor/Kata 把边界重新画到内核外面（选型见 [llm/29](../llm/29-sandbox-isolation.md)）。**没有「默认安全的容器」，只有「配置正确 + 内核及时打补丁」的容器。**

---

## 33.8 隔离与最小权限：四种机制，四张不同的网

权限模型回答「谁能做什么」，隔离回答「看不见/碰不到」，最小权限回答「即使被攻破，最多能造成多大」。三者互补，不能互相替代。

### seccomp-bpf：按系统调用号过滤

seccomp-bpf 允许进程安装一个 BPF 程序，内核在每次系统调用入口执行它，根据返回值决定放行、返回错误、杀进程还是交给用户态 supervisor。能力边界很明确：**能看**系统调用号、6 个参数的值、架构号；**不能看**参数指向的内存内容（内核不会替 BPF 程序解引用用户指针），所以无法做「这个路径是不是在允许目录内」这类检查；**继承**且不能移除；**前提**是非特权进程安装前必须 `PR_SET_NO_NEW_PRIVS`，否则可以用 setuid 程序绕过。返回动作按优先级：`KILL_PROCESS` > `KILL_THREAD` > `TRAP` > `ERRNO` > `USER_NOTIF` > `TRACE` > `LOG` > `ALLOW`；生产上最常见的是「默认 `ERRNO(EPERM)`，白名单放行」。

```c
/* 只拦截 getpid，其他放行；完整程序还要 include 与错误检查 */
struct sock_filter f[] = {
    BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
    BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_getpid, 0, 1),
    BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
    BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
};
struct sock_fprog prog = { .len = 4, .filter = f };
prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0);   /* 非特权装 filter 的前提 */
syscall(__NR_seccomp, SECCOMP_SET_MODE_FILTER, 0, &prog);
```

```console
$ gcc -O0 -o sec sec.c && ./sec
before: getpid() = 3327044
after: getpid() = -1 errno=1 (Operation not permitted)
PR_GET_SECCOMP = 2 (2 = filter mode)
```

filter 装上后 `getpid` 返回 `EPERM`，但进程继续运行——这是 `ERRNO` 动作的语义；`PR_GET_SECCOMP` 返回 2 表示「filter 模式」，也是 `/proc/<pid>/status` 里 `Seccomp: 2` 的来源。**seccomp 的强项是「把不需要的系统调用全部关掉」，弱项是「它看不懂参数指向的内容」**：它能拦 `openat`，但不能判断路径是否越界；路径边界要靠 `openat2`（§33.6）或 LSM。

### namespace、cgroup 与 LSM

namespace 把全局资源包成「每个 namespace 一份视图」：`pid`/`mnt`/`net`/`uts`/`ipc`/`user`/`cgroup`/`time` 分别隔离进程 ID、挂载表、网络栈、hostname、IPC、uid 映射、cgroup 视图、时钟偏移；它们都不隔离「资源本身」和「内核代码」。最容易误解的是 `user` namespace：它让非特权用户在内部变成 uid 0，但**不给你宿主权限**，同时把大量内核路径变成「非特权可到达」，历史上是内核漏洞高发区。

cgroup 管「能用多少」（`cpu.max`、`memory.max`、`pids.max`、`io.max`），**不能当权限边界用**；它限制爆炸半径：被攻破后不能 fork 炸弹拖垮宿主、不能用内存把宿主 OOM。cgroup v1 的 `release_agent` 曾在 user ns + v1 组合下成为逃逸路径，v2 移除了它。

LSM 在对象访问点插入钩子，在 DAC 之外再做一次判断：AppArmor 按路径、SELinux 按标签/类型、Landlock 让进程自我约束。**LSM 是 MAC，叠在 DAC 之上：DAC 说「你是 root 所以你行」，LSM 还可以说「即使你是 root，这个域也不能读这个标签」。它不能替代权限模型，只加一条「必须同时满足」的条件。**

`no_new_privs`：`prctl(PR_SET_NO_NEW_PRIVS, 1)` 之后 `execve` 不得因 setuid/setgid/file capability 获得新权限；继承、不可撤销、是 seccomp 的前提。它只阻止「通过 exec 获得更多」，不阻止已持有的权限；正确用法是在降权之后、exec 不可信程序之前设置。

### 分工表

| 机制 | 挡住哪一类 | 不挡什么 | 代价 |
| --- | --- | --- | --- |
| 降 uid / 丢 capability | 越权读写、越权管理 | 同 uid 的其他进程、内核漏洞 | 需要重新设计启动流程；某些操作必须拆服务 |
| `no_new_privs` | 通过 setuid/file caps 提权 | 已持有的权限、内核漏洞 | 极低；但会让 `sudo` 在进程内不可用 |
| seccomp-bpf | 不需要的系统调用、危险 syscall 族 | 参数指向的内容、允许范围内的逻辑漏洞 | 维护 allowlist；新依赖可能被误杀 |
| namespace | 看见别的进程/挂载/网络/用户 | 内核漏洞、共享内核侧信道、资源耗尽 | 配置复杂；user ns 增加攻击面 |
| cgroup | 资源耗尽、fork 炸弹、OOM 扩散 | 权限、数据泄露 | v2 与委托配置；限得太死影响性能 |
| LSM | 违反策略的访问（即使 root） | 策略没覆盖的路径、内核漏洞 | 策略编写与排障成本高 |
| user namespace | 非特权用户获得「局部 root」 | 宿主权限、内核漏洞 | 内核攻击面变大 |

一个多租户执行环境的最小组合（`base/34` 会手工搭）：**非 root uid + `no_new_privs` + 丢掉全部 capability + seccomp allowlist + mount namespace + `pivot_root` + 只读 rootfs + 独立 net namespace + cgroup 限额 + LSM profile + 及时打补丁的内核**。缺任何一项都不是「不能用」，而是「少了一张网」；安全强度取决于最短的那块板。

---

## 33.9 实测：五个防御侧观测

以下输出来自本机：Linux 6.12.113（Debian 12 cloud 内核）、gcc 12.2.0、glibc 2.36。当前 shell 是容器内的 root，`Seccomp: 0`、capability 全开——**这本身就是一个反面教材：开发环境默认不是沙箱。**

### ① 身份与 umask

```console
$ id
uid=0(root) gid=0(root) groups=0(root)
$ umask
0022
$ touch f1 && stat -c '%a %n' f1
644 f1
$ mkdir -p d1 && stat -c '%a %n' d1
755 d1
$ (umask 0077; touch f2; stat -c '%a %n' f2)
600 f2
$ stat -c '%a %U:%G %n' /etc/shadow /etc/passwd /tmp
640 root:shadow /etc/shadow
644 root:root /etc/passwd
1777 root:root /tmp
```

`0022` 清掉 group/other 写位（`0644`/`0755`），`0077` 清掉全部 group/other 位（`0600`）；`640` 与 `1777` 是「不给读」与「可写但不可删」两个极端。

### ② 用静态检查验证栈保护

§33.5 的反汇编展示了保护值的装入与退出时的核对。应以目标函数及发布构建的实际指令为准；仅在符号表里发现 `__stack_chk_fail` 不能证明每个函数都受保护。进程因栈保护失败而终止时，可调查运行时日志与经授权的诊断记录，不必在生产环境触发错误。

### ③ ASLR 三档与熵

```console
$ cat /proc/sys/kernel/randomize_va_space
2
$ cat /proc/sys/vm/mmap_rnd_bits
28
```

`2` 是完整随机化（含 brk）；`28` 位 mmap 熵意味着 `2²⁸` 个页位置、`2²⁸ × 4 KiB = 1 TiB` 的随机范围；栈的 22 位意味着 `2²² × 4 KiB = 16 GiB`。

### ④ seccomp 状态与一个最小 filter

```console
$ cat /proc/self/status | grep -i seccomp
Seccomp:	0
Seccomp_filters:	0
```

`Seccomp: 0` 表示当前 shell 没有 filter；`./sec`（§33.8）装上 filter 后 `PR_GET_SECCOMP` 变成 2，`getpid` 被 `ERRNO(EPERM)` 拦截。**默认拒绝靠内核强制，不靠应用自觉。**

### ⑤ `GNU_STACK` 的 RWE 标志

```console
$ readelf -lW ./smash | grep GNU_STACK
  GNU_STACK      0x000000 0x0000000000000000 0x0000000000000000 0x000000 0x000000 RW  0x10
$ gcc -O0 -z execstack -o smash_exec smash.c
$ readelf -lW ./smash_exec | grep GNU_STACK
  GNU_STACK      0x000000 0x0000000000000000 0x0000000000000000 0x000000 0x000000 RWE 0x10
```

默认 `RW`（不可执行），`-z execstack` 变成 `RWE`。**这是 ELF 与内核的契约**：链接器声明意图，内核在 `execve` 时据此设置 `VM_EXEC`；生产构建应确认所有二进制都是 `RW`。

---

## 33.10 三列账：承诺、代价、崩点

| 维度 | 承诺了什么 | 为此付了什么 | 从哪里开始崩 |
| --- | --- | --- | --- |
| **权限模型**（uid/mode/cap） | 每次访问都有「谁、对什么、做什么」的答案，且答案由可信代码给出 | 每个对象维护属主/ACL；capability 拆分让启动流程变复杂；`setuid` 程序要单独审计 | 检查写错、检查与使用不一致、混淆代理、capability 过授、`sudo` 过宽 |
| **地址空间隔离**（页表/NX/KPTI） | 用户态碰不到内核页，进程之间互不可见；注入的代码不能执行 | 缺页/TLB miss 的成本；KPTI 每次 syscall 换 CR3；SMEP/SMAP 需要新 CPU | 内核漏一次用户指针校验；同进程内越界（页表不隔离变量）；侧信道 |
| **代码完整性**（canary/PIE/RELRO/CFI） | 控制流不会被数据改写；执行的代码来自预期映像 | 编译选项、性能开销、工具链兼容性；CFI 需要全量重编译 | 信息泄露打掉 ASLR；任意写绕过 canary；ROP 复用已有代码；供应链投毒 |
| **seccomp / namespace / cgroup** | 把「能做什么、能看见什么、能用多少」各自收窄 | allowlist 维护、namespace 配置、cgroup 调参；误杀与性能损耗 | 策略漏项；user ns 增加内核攻击面；共享内核侧信道；cgroup v1 历史接口 |
| **容器整体** | 轻量、启动快、密度高；镜像即交付 | 共享内核意味着「内核补丁 = 全集群重启窗口」；隔离强度取决于配置 | `--privileged`、挂 `docker.sock`、`--pid=host` + `CAP_SYS_PTRACE`、内核漏洞、错误挂载 |

一句话收束：**权限模型买到「授权」，地址空间隔离买到「看不见」，代码完整性买到「改不了」，隔离机制买到「炸不大」；四笔账各有价格，没有一笔可以白拿。** 事故复盘时，先问「哪根柱子被绕过」，再问「为什么没有第二根柱子兜底」——这比「攻击者用了什么工具」有用得多。

---

## 33.11 踩坑清单：现象 → 根因 → 定位 → 修 → 防复发

| 现象 | 根因 | 定位 | 修 | 防复发 |
| --- | --- | --- | --- | --- |
| 服务被 RCE，攻击者直接拿到 root | 服务以 `uid=0` 运行，权限模型第一道就是全开 | `ps -o user,pid,cmd -C app`；`grep Uid /proc/<pid>/status` | systemd `User=app`、`Group=app`、`AmbientCapabilities=` 只留必需项；容器 `runAsNonRoot` | 镜像构建时禁用 root；准入策略拒绝 `runAsUser: 0` |
| 容器里能创建新容器、能挂宿主目录 | 挂了 `/var/run/docker.sock`，等于把宿主 root 的 API 交给容器 | `mount \| grep docker.sock`；`ls -l /var/run/docker.sock` | 去掉挂载；改用 rootless/buildkit；必须用时走只读 socket proxy | 准入策略禁止 hostPath 与 docker.sock；CI 扫描清单 |
| 上传目录里的文件被执行 | `/tmp` 或上传目录挂载时带了 `exec`，webshell 可直接运行 | `findmnt -no TARGET,OPTIONS /tmp`；`mount \| grep <dir>` | 挂载加 `noexec,nosuid,nodev`；上传目录单独分区 | 基础镜像/fstab 模板统一加 `noexec`；上线检查 |
| 日志里出现明文 token / 密码 | 应用把 URL、header、异常对象整体打进日志 | `grep -iE 'token\|password\|authorization\|secret' /var/log/app/*.log` | 日志层字段级脱敏；禁止记录完整请求体；密钥只在内存 | 日志库提供 `redact()`；CI 加 secret 扫描 |
| `sudo` 一条命令就能提权 | `NOPASSWD: ALL`、通配符规则、允许带任意参数的命令 | `sudo -l`；`visudo -c`；检查 `/etc/sudoers.d/` | 改成「特定命令 + 固定参数 + 特定用户」；禁止 `sudo sh`/`sudo vi` | 每周 diff sudoers；变更走评审 |
| 机器上有一堆不认识的 setuid 程序 | 第三方软件安装时留下 setuid root 二进制，长期无人审计 | `find / -xdev -perm -4000 -type f`；`getcap -r /` | 去掉不必要的 setuid，改用 file capability 或提权服务 | 每次装机/镜像构建生成清单并 diff；CIS 基线检查 |
| 容器 `--privileged` 跑得好好的，直到出事 | 为了一个 `sysctl`/设备访问，把全部 capability、设备、LSM 限制都关掉 | `docker inspect -f '{{.HostConfig.Privileged}}' <ctr>`；容器内 `capsh --print` | 改成按需 `--cap-add`、`--device`、`--sysctl`；只读 rootfs | 准入策略拒绝 privileged；评审把 `--privileged` 当红线 |

三条通用判据：① **看到「服务以 root 跑」，先别问它有没有漏洞，先问它为什么需要 root**；大多数服务只需要一个低位端口或一个目录的读写，这两件事都有非 root 解法。② **看到「容器里能访问 X」，先分清是「隔离没配」还是「隔离被绕过」**：前者改配置，后者打补丁。③ **看到「日志里有敏感信息」，把它当漏洞处理**：凭据一旦进了日志，就已经完成了泄露。

---

## 33.12 一页速记

```
【一句话】内核安全只有三根柱子：权限模型、地址空间隔离、代码完整性；
        每一次提权或逃逸，都能指认其中一根被绕过。

【权限模型】
  四个 uid：ruid（谁启动的）/ euid（检查时是谁）/ suid（本来是谁）/ fsuid（文件检查用谁）
  九个位：rwx × owner/group/other；八进制 0644、0755、0600、1777
  特殊位：setuid 4000（exec 后 euid=文件属主）、setgid 2000、sticky 1000（/tmp 防删）
  umask：最终权限 = 请求权限 & ~umask；0022 → 644/755；0077 → 600
  capability：CAP_DAC_OVERRIDE（文件全权）、CAP_NET_RAW（原始包）、
              CAP_SYS_PTRACE（读写任意进程）、CAP_SYS_ADMIN（≈新 root）、
              CAP_SYS_MODULE（= ring 0）
  setuid 是唯一被设计出来的「低 → 高」通道，也是最高危的攻击面

【地址空间隔离】
  页表项：Present / Writable / U-S / NX；MMU 每个访存周期检查
  ASLR：只随机基址，不随机段内偏移；mmap 28 bit（1 TiB）、栈 22 bit（16 GiB）
  randomize_va_space：0 关 / 1 不含 brk / 2 全开（默认）
  NX：栈堆不可执行，挡注入代码，不挡 ROP/ret2libc
  canary：fs:0x28 → [rbp-8]，尾声比较；挡线性溢出，不挡任意写/堆溢出
  页表隔离的是进程，不是变量；buf 和返回地址在同一页里没有硬件边界

【缓冲区溢出】
  strcpy/gets 没有目标长度 → 写过 buf → 踩 canary → 改 saved rbp → 改返回地址
  ret 从栈顶取 8 字节进 rip；控制流被数据改写
  -fstack-protector-all：检测到栈保护值被改写时，运行时会终止；没有保护不保证出现某个信号
  "stack smashing detected" 是 glibc 的 stderr 消息，不是 dmesg

【整数与边界】
  检查的算术域必须等于使用的算术域；先扩宽，再比较，再使用
  len > 4096 不检查下界 → -1 通过 → (size_t)-1 = 2^64-1
  路径穿越：检查字符串 ≠ 打开对象；用 openat2 + RESOLVE_BENEATH
  chroot 不是安全边界；pivot_root + mnt ns + 丢 CAP_SYS_ADMIN 才是

【内核与逃逸】
  容器共享内核；namespace 改视图，不改代码
  "逃逸" 两种：配置错误（本来就没边界）/ 内核漏洞（真跨边界）
  --privileged + docker.sock + --pid=host & CAP_SYS_PTRACE = 没有边界

【隔离机制】
  seccomp：按 syscall 号过滤，看不懂指针内容；默认拒绝 + 白名单
  namespace：pid/mnt/net/user/uts/ipc/cgroup/time；user ns 增加内核攻击面
  cgroup：资源上限，不是授权边界
  LSM：AppArmor（路径）/ SELinux（标签）/ Landlock（自我约束）
  no_new_privs：阻止 exec 获得新权限；不可撤销；seccomp 的前提

【三个数字】
  2^28 页 × 4 KiB = 1 TiB（mmap ASLR 范围）
  2^22 页 × 4 KiB = 16 GiB（栈 ASLR 范围）
  134 = 128 + SIGABRT（canary 检测）；139 = 128 + SIGSEGV（无 canary）
```

---

## 33.13 自检 3 题

**Q1.** 一个服务用 `strncmp(full, "/data", 5)` 检查路径，为什么 `..` 能过？给出两种正确做法，并说明为什么「先 `realpath` 再检查」仍然有竞态。

<details><summary>答案</summary>

`strncmp` 检查的是**拼接后、未规范化**的字符串。`"/data/docs/../../etc/shadow"` 的前 5 个字符确实是 `"/data"`，检查通过；但 `open` 交给内核后，VFS 逐段解析 `..`，最终落到 `/etc/shadow`。**检查的是字符串，打开的是对象，两者之间隔了一次内核解析。**

两种正确做法：① **`openat2` + `RESOLVE_BENEATH`**：把解析与边界检查合并成一个原子操作，越界返回 `EXDEV`（本机实测 errno 18）。② **不接收路径，只接收文件 ID**：路径由服务端从数据库取，用户输入里根本没有 `..`；再叠加属主/租户校验。

`realpath` 之后再检查仍有 TOCTOU：它解析的是**调用那一刻**的路径，返回后到 `open` 之前，路径中的目录/符号链接可能被替换。正确方向是「让内核在打开的同一动作里执行边界」。

</details>

**Q2.** `-fstack-protector-all` 下，33 个字节写进 16 字节的 `buf`，为什么程序是 abort 而不是跳到攻击者指定的地址？如果攻击者已经泄露了 canary，这道防线还剩下什么？

<details><summary>答案</summary>

canary 被编译器放在 `buf` 和保存的返回地址之间（本机反汇编：`buf` 在 `rbp-0x20`，canary 在 `rbp-0x8`，返回地址在 `rbp+8`）。`strcpy` 连续写 33 个字节，必然先踩过 canary；函数尾声比较 `fs:0x28`，不相等就 `__stack_chk_fail` → `abort()`。**控制流还没走到 `ret` 就被终止了；进程死了，但没有跳到攻击者指定的地址。**

如果 canary 已经泄露，这道「检测」就不再是障碍。此时还剩 ASLR、NX（只能 ROP 复用已有代码）、PIE/RELRO、CFI、最小权限。**纵深防御的含义是：每一层都假设前一层会失守，没有哪一层靠「不可能被绕过」存在。**

</details>

**Q3.** 为什么说「`--privileged` 容器里的逃逸」其实不是逃逸？「容器逃逸几乎总是内核漏洞」这句话，怎么用三根柱子解释？

<details><summary>答案</summary>

`--privileged` 会关闭容器运行时的多数限制：给全部 capability、放开 device cgroup、放宽 seccomp/AppArmor/SELinux、允许访问宿主设备。也就是说，**权限模型这根柱子被主动拆掉了，地址空间隔离里「容器 vs 宿主」的那层视图也被放开了**。此时容器里的进程不是「被关在外面然后想办法翻墙」，而是「门根本没关」；讨论「逃逸技术」没有意义，因为不存在需要跨越的边界。这类事件应归类为「配置错误 / 没有隔离」。

默认配置下的真逃逸则不同：容器有自己的 namespace，看不到宿主进程和文件，但**它和宿主共享同一个内核**。容器里的 `syscall` 进入的是宿主内核的同一段 ring 0 代码；namespace 只改变「内核返回什么视图」，不改变「哪段代码在跑」。所以只要内核在某个可达入口（syscall、`ioctl`、文件系统、网络栈、eBPF）漏了一次校验，容器内的代码就能让内核替它做越权操作——一次内核利用同时穿过三根柱子（内核自身是最高权限、ring 0 能访问所有页、控制流被改到内核 gadget）。**共享内核的代价是：隔离强度的上限，等于内核攻击面的上限。**

</details>

---

## 33.14 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 特权级、ring、VM vs 容器的隔离边界（本篇的硬件前提） | → `base/15` |
| 进程 = 地址空间 + 内核簿记；`fork`/COW 与 `task_struct` | → `base/23` |
| 页表、缺页与 U/S 位：地址空间隔离的物理实现 | → `base/26` |
| 一次系统调用的完整路径：`syscall` 如何进入 ring 0 | → `base/21` |
| 栈帧、调用约定与 `ret` 的机器码形态（§33.5 的图从这里来） | → `base/12` |
| 补码、溢出与无符号比较的位级推导（§33.6 的规则从这里来） | → `base/03`、`base/04` |
| 手搓容器：`unshare` + `cgroup` + `pivot_root`，亲手验证「共享内核」 | → `base/34` |
| 沙箱四档、隔离强度三角与多租户选型 | → [llm/29](../llm/29-sandbox-isolation.md) |
| 防御加固：默认拒绝、最小权限、供应链与运行时基线 | → [llm/32](../llm/32-defense-hardening.md) |
| 全书总纲：四条主线与 56 篇清单 | [0-summary](0-summary.md) |
