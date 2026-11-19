# 22. 从源码到进程：编译、链接、ELF、装载与地址空间

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 操作系统：Linux 视角 ｜ **编号**：22 / 56
> **本篇定位**：Part III 的第二篇，也是这一部分唯一讲「进程出生之前」的一篇。21 讲进程怎么跨进内核，本篇反过来问：**内核第一次见到你的程序时，手里究竟拿到了什么**。全篇只有一条推论链——`ELF 是「同一份字节的两套视图」` → `链接器按节视图拼字节、内核按段视图做 mmap` → `把「符号地址」这个洞留到运行期填` → `于是有了动态链接、PLT/GOT、ASLR`。23 讲进程控制块，26 讲页表与缺页，33 讲 ASLR 的攻防面：三篇都从这条链上分叉。
> **读法**：§22.3 的「两套视图」与 §22.4 的 `R_X86_64_PC32` 手算全篇支点，后者建议拿纸算一遍；§22.4 末尾「同一个 `_start`，静态是直接 call、动态是查 GOT」那两条反汇编是全篇最硬的证据，只读一处就读它；§22.6 的布局图与 §22.7 的启动序列可以直接拿去解释「`main` 之前崩了」；赶时间只读 §22.9–22.13。
> **不在本篇**：进程控制块、`fork` 与 COW → `base/23`；页表四级结构与缺页处理 → `base/26`；`malloc`/`mmap` 在用户态的分配器 → `base/27`；ASLR/NX/canary 的攻防全谱 → `base/33`；Go 的编译流水线与工具链布局 → [go/27](../go/27-compile-pipeline.md)、[go/2](../go/2-toolchain-layout.md)。

---

## 22.1 引子：一次「二进制的 `main` 还没执行就崩了」的事故

2026-03，某团队的 API 服务升级基础镜像（Debian 12 → 13）。代码一行没改，镜像构建成功，容器起不来：

```
$ docker run --rm api:2.7.1 --version
api: /usr/lib/x86_64-linux-gnu/libssl.so.3: version `OPENSSL_3.2.0' not found (required by ./api)
```

值班同学的第一反应是「库没装」，于是进容器跑 `ldd`：每一行 `=>` 都有落点，退出码 0，**看起来一切正常**。结论于是变成「`ldd` 说没问题，那大概是 `-Wl,--as-needed` 玄学」，两个小时耗在链接参数上。

真相只有一句：**`ldd` 检查「这个库文件在不在」，运行期检查「这个库里有没有那个版本的符号」**。两者不是同一个判据，而 `ldd` 的退出码连前者失败都不反映——本机实测（§22.8⑥）：

```
$ ldd ./use_norpath        # 依赖库确实找不到
	libfoo.so => not found
	libc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x00007f71f8fef000)
$ echo $?
0                          ← 找不到库，退出码仍是 0
```

所以「`ldd` 正常」这句话本身不构成证据。整条时间线：

| 时间 | 动作 | 观测 | 当时的错误结论 |
| --- | --- | --- | --- |
| T+0 | 升级基础镜像，启动容器 | `version 'OPENSSL_3.2.0' not found` | 「库没装」 |
| T+15m | `ldd ./api` | 全部 `=>` 有落点，`exit 0` | 「依赖完整，是链接参数问题」 |
| T+2h | `objdump -T ./api \| grep OPENSSL` | 要求 `OPENSSL_3.2.0` | 「到底谁要 3.2？」 |
| T+2h10m | `objdump -T` 那个 `libssl.so.3` | 只提供到 `OPENSSL_3.0.0` | 镜像里的 libssl 比构建机旧 |

每一环按回物理层，得到四句话：

| 直觉说法 | 真实机制 | 一句话原因 |
| --- | --- | --- |
| 「`./api` 是运行一个文件」 | 内核把 4 个 `PT_LOAD` 各 `mmap` 一次，再跳到 `e_entry` | 运行的从来不是「文件」，是**被映射进新地址空间的字节** |
| 「`printf` 在某个地址上」 | 未链接时它是 0，靠重定位表事后填 | `.o` 里的 `call` 后面**没有地址，只有一条待填记录** |
| 「`ldd` 正常 = 依赖 OK」 | `ldd` 只做「找文件 + 列出来」，不校验符号版本 | 版本约束在 `ld.so` 解析符号时才检查 |
| 「地址是编译时定死的」 | PIE 的每段都被随机偏置，`AT_ENTRY` 就是偏置过的入口 | 地址在**每次 exec 时**才确定 |

---

## 22.2 四个阶段：每一步都只是「读一种文本、写另一种文本」

`gcc hello.c -o hello` 是一条命令，四个程序。用 `-E/-S/-c` 拆开，每一步的输出都是人能打开看的。被测文件有 9 行：一个已初始化全局、一个未初始化全局、一个指向字符串常量的指针、一个 `main`。

| 阶段 | 命令 | 输入 | 输出 | 谁在做 |
| --- | --- | --- | --- | --- |
| ① 预处理 | `gcc -E hello.c -o hello.i` | 175 B 的 `.c` | **40,514 B** 的 `.i`（2,007 行） | `cc1 -E`，只做文本替换 |
| ② 编译 | `gcc -S hello.i -o hello.s` | 40,514 B 的 `.i` | 1,176 B 的 `.s`（73 行） | `cc1`，前端 + 优化器 |
| ③ 汇编 | `gcc -c hello.s -o hello.o` | 1,176 B 的 `.s` | 1,888 B 的 `.o`（`ET_REL`） | `as`，一指令一编码 |
| ④ 链接 | `gcc hello.o -o hello` | `.o` + `crt*.o` + `libc.so` | 17,480 B 的 `hello`（`ET_DYN`） | `ld`，拼节、填符号、生成段 |

**9 行源文件，预处理后 2,007 行**——多出来的全来自两个 `#include`。预处理器不知道 `printf` 是什么，它只做三件事：把 `#include` 的目标**原样粘进来**、展开 `#define`、条件编译。**所以 C 的「编译单元」从来不是你写的那几行，而是那 2,007 行。**

第②步的 `.s` 已经写明「数据住在哪个节」：`g_init` 是 `.data` 里的 `.long 42`——**42 这个值真的在文件里**；`g_zero` 是 `.bss` 里的 `.zero 4`——**只有「占 4 字节」，没有值**。`.bss` 因此在文件里不占一个字节，这直接决定它的段类型是 `NOBITS`，也解释了为什么全局 `char buf[1<<20]` 不会让二进制大 1 MB。

第③步的 `size` 说明 `.o` 里只有你写的东西：

```
$ size hello.o hello
   text	   data	    bss	    dec	    hex	filename
    201	     12	      4	    217	     d9	hello.o
   1524	    608	      8	   2140	    85c	hello
```

201 字节的 `text` 就是 `main` 一个函数；链接后涨到 1,524，多出来的 1,323 字节里没有一行是你写的（`.init`/`.plt`/`.fini` 与 `crt*.o` 的 `_start`）。**「链接」的实际动作是「把你的节和一批别人的节按地址排好，再把所有互相引用的地方填上数字」。**

---

## 22.3 ELF 结构：同一份字节，两套互不相干的视图

ELF 里有两个「目录」，指向**同一片字节**，组织方式完全不同：

- **program header table（段视图）**：给**内核/装载器**用。回答「文件里哪一段映射到内存哪个虚拟地址、给什么权限」。
- **section header table（节视图）**：给**链接器/调试器**用。回答「这段字节属于 `.text` 还是 `.rodata`、符号表在哪、重定位表在哪」。

`hello` 的 ELF header 给出：`e_phoff=64`、`e_phnum=13`、`e_phentsize=56`、`e_shoff=15112`、`e_shnum=37`、`e_shentsize=64`。于是 17,480 字节的文件被完整切分，一字节不差：

```
        文件字节 0x0000 ──────────────────────────────────────────
   ┌────────────────────────────────────────────────┐ 0
   │ ELF header                            64 B     │ 7f 45 4c 46 …
   ├────────────────────────────────────────────────┤ 64 = e_phoff
   │ program header[0..12]    13 × 56 = 728 B       │ ← 段视图：PT_LOAD ×4
   ├────────────────────────────────────────────────┤ 792 = 0x318
   │ .interp .dynsym .dynstr .rela.dyn .rela.plt    │ ← 只有这一层是「内容」
   │ .init .plt .text .rodata .init_array .got .data │    内核直接映射它
   ├────────────────────────────────────────────────┤ 15112 = e_shoff
   │ section header[0..36]    37 × 64 = 2368 B      │ ← 节视图：名字/类型/属性
   └────────────────────────────────────────────────┘ 15112+2368 = 17480
```

### 段视图：内核只认这 4 个 `PT_LOAD`

```
$ readelf -lW hello | sed -n '/Program Headers:/,/Section to Segment/p'
  Type         Offset   VirtAddr            FileSiz  MemSiz   Flg Align
  LOAD         0x000000 0x0000000000000000  0x000668 0x000668 R   0x1000
  LOAD         0x001000 0x0000000000001000  0x0001a9 0x0001a9 R E 0x1000
  LOAD         0x002000 0x0000000000002000  0x00011c 0x00011c R   0x1000
  LOAD         0x002dd0 0x0000000000003dd0  0x000260 0x000268 RW  0x1000
```

其余九个（`PHDR`/`INTERP`/`DYNAMIC`/`NOTE`×2/`GNU_*`）是**给装载器看的元信息**，不产生新的可访问内存。

| # | 权限 | 覆盖的节 | FileSiz | MemSiz | 差值从哪来 |
| --- | --- | --- | --- | --- | --- |
| 0 | `R` | ELF 头、`PHDR`、`.interp`、`.dynsym`、`.rela.*` | 0x668 | 0x668 | — |
| 1 | `R E` | `.init` `.plt` `.text` `.fini` | 0x1a9 | 0x1a9 | — |
| 2 | `R` | `.rodata` `.eh_frame_hdr` `.eh_frame` | 0x11c | 0x11c | — |
| 3 | `RW` | `.init_array` `.dynamic` `.got` `.data` `.bss` | 0x260 | 0x268 | **8 字节 = `.bss`** |

两处细节，都是「不这样会怎样」推出来的：

**① `MemSiz − FileSiz = 8`，这 8 字节就是 `.bss`。** 内核把 `[vaddr, vaddr+FileSiz)` 指向文件页，剩下 `[vaddr+FileSiz, vaddr+MemSiz)` 用匿名页补上并清零。`.bss` 类型正是 `NOBITS`——**文件里没有偏移也没有大小，只有一个虚拟地址和长度**。所以「不占磁盘」不是优化，是「初始值恒为 0」的必然。

**② `Offset ≠ VirtAddr`（0x2dd0 vs 0x3dd0），但两者低 12 位都是 `0xdd0`。** `mmap` 只能按页对齐，所以 ELF 规定 `p_offset ≡ p_vaddr (mod p_align)`；内核只需 `mmap(file, p_offset & ~0xfff, …, p_vaddr & ~0xfff, …)`，页内偏移由硬件加法吸收——**这就是「段视图能直接变成 `mmap` 参数」的全部秘密**。

### 节视图：链接器只认这些

| 节 | 属性 | 谁写 | 谁读 | 为什么是这个属性 |
| --- | --- | --- | --- | --- |
| `.text` `.init` `.plt` `.fini` | `AX` | 编译器/链接器 | CPU 取指 | 必须可执行；**不该可写**，否则溢出能改代码 |
| `.rodata` `.dynsym` `.dynstr` `.rela.*` | `A` | 编译器/链接器 | 任何代码 | 只读：字符串常量被改写就是 bug |
| `.data` `.got` `.got.plt` `.dynamic` | `WA` | 编译器/链接器 | 任何代码 | 运行期要改（GOT 要填真实地址） |
| `.bss` | `WA`（`NOBITS`） | 内核清零 | 任何代码 | 要可写，但文件里没有内容 |
| `.symtab` `.strtab` `.debug_*` | **无 `A`** | 编译器 | 链接器/gdb/`nm` | **不参与装载**，`strip` 掉程序照跑 |

最后一行是分界线：**没有 `A`（alloc）属性，所以这些节根本不会被映射进内存**。`nm hello` 能读出所有函数名，因为它读的是**文件**；进程内存里从来没有那张表。于是「strip 之后为什么还能跑」「为什么 release 版 core dump 只有地址」「为什么 `-g` 让文件涨到 1.2 MB 而内存占用没变」是同一句话：**节视图里有一半东西从不进入地址空间**。

---

## 22.4 链接：把一个「洞」推迟填，还是当场填

`hello.o` 里 `main` 的反汇编，**每一条访问外部符号的指令都是残缺的**：

```
$ objdump -dr hello.o | sed -n '/<main>:/,/^$/p'
   0:	push   %rbp
   d:	mov    0x0(%rip),%r13d        # 14 <main+0x14>
			10: R_X86_64_PC32	g_zero-0x4
  14:	mov    0x0(%rip),%r12d        # 1b <main+0x1b>
			17: R_X86_64_PC32	g_init-0x4
  22:	call   27 <main+0x27>
			23: R_X86_64_PLT32	getpid-0x4
  41:	call   46 <main+0x46>
			42: R_X86_64_PLT32	printf-0x4
```

三条 `mov ...(%rip)` 的位移**全是 0**，两条 `call` 的目标是「下一条指令自己」。编译器此刻**不可能知道**地址：`g_zero` 落在哪取决于链接器排第几、以及装载时被偏置多少。它只留一件事：**一条「这里有个 4 字节的洞，要填 `g_zero` 相对本指令的偏移」的记录**。

`R_X86_64_PC32` 的含义一句话：**填入值 = `S + A − P`**，`S` 是符号最终地址、`A` 是加数、`P` 是**这条记录所在字段的地址**。拿 `g_zero` 手工验算：

```
链接后：main 在 0x1149，该指令在 0x1156（7 字节），位移字段在 0x1159
        g_zero 被排在 .bss 起点 0x4034
        S=0x4034, A=-0x4, P=0x1159
        S + A − P = 0x4034 − 0x4 − 0x1159 = 0x2ED7   ← 与链接结果逐字节一致

CPU 执行时（x86-64 的 RIP 相对寻址）：
        目标 = 下一条指令地址 + 位移 = 0x115D + 0x2ED7 = 0x4034  ✓ g_zero
```

链接后的真实编码是 `mov 0x2ed7(%rip),%r13d`，位移字段就是 `d7 2e 00 00`。**加数为什么是 `-4`？** 因为 `P` 是字段自己的地址，而 RIP 相对位移是相对**下一条指令**算的：字段占 4 字节，下一条指令 = `P+4`，要让 `S + A − P` 等于「相对下一条指令的偏移」，必须 `A = -4`。

### 静态链接：所有洞当场填完

静态链接只有三件事：**① 合并同类节**（所有 `.o` 的 `.text` 拼成一段、`.data` 一段、`.bss` 求总长，地址从 `0x400000` 起分配）；**② 解析符号**（把每个 `.o` 的 `.symtab` 合起来，`U` 去已定义的里找，找不到就报 `undefined reference`）；**③ 逐条重定位**（拿 `.rela.text` 的每一行按 `S + A − P` 算数字，写进那个洞）。

③ 必须在 ① 之后，因为 `S` 依赖最终布局——**单趟不可能，文件开头的 `call` 可能指向文件末尾的函数**。完成后类型从 `ET_REL` 变成 `ET_EXEC`，且没有 `PT_INTERP`（`readelf -lW s_static | grep -c INTERP` 输出 `0`，入口固定 `0x401550`），**「装载」退化成「按固定地址 `mmap`」。**

### 动态链接：把解析推迟到运行期

动态链接面对一个硬矛盾：`libc.so` 里 `printf` 的地址取决于**装载时的随机偏置**，链接时不可能知道。于是它把「填地址」拆成两步：链接时只在 `.dynamic` 里写一条 `NEEDED libc.so.6`、在 `.rela.plt` 里留一条待填记录；装载时或首次调用时由 `ld.so` 查到真实地址写进 GOT。默认懒绑定下，GOT 的初值**故意指向 PLT 内部的下一条指令**：

```
$ objdump -d -j .plt hello
0000000000001020 <getpid@plt-0x10>:            ← PLT0：公共跳板
    1020:	push   0x2fca(%rip)      # 3ff0 ← link_map
    1026:	jmp    *0x2fcc(%rip)     # 3ff8 ← _dl_runtime_resolve
0000000000001030 <getpid@plt>:
    1030:	jmp    *0x2fca(%rip)     # 4000 ← GOT 槽
    1036:	push   $0x0                    ← 重定位下标 0
    103b:	jmp    1020 <_init+0x20>
0000000000001040 <printf@plt>:
    1040:	jmp    *0x2fc2(%rip)     # 4008 ← GOT 槽
    1046:	push   $0x1                    ← 重定位下标 1
```

GOT 里的初值，dump 出来正好是「PLT+6」：

```
$ objdump -s -j .got.plt hello
 3fe8 e03d0000 00000000 00000000 00000000   ← [0]=_DYNAMIC [1]=link_map [2]=resolver
 3ff8 00000000 00000000 36100000 00000000   ← [2]=0待填 [0x4000]=0x1036=getpid@plt+6
 4008 46100000 00000000                     ← [0x4008]=0x1046=printf@plt+6
```

**`0x1036` 就是 `getpid@plt` 的第二条指令**，`0x1046` 是 `printf@plt` 的第二条。这是最值得盯着看的一处：GOT 初值不是 0，而是「跳回自己后面的 `push`」——**一次调用被设计成第一次必然失败、然后自动修复**。

```
   第一次调用 printf：
   main: call 1040 → 1040: jmp *0x4008（GOT 里是 0x1046，不是 libc）
        → 1046: push $0x1（重定位下标）→ 104b: jmp 1020
        → 1020: push link_map → 1026: jmp *0x3ff8（_dl_runtime_resolve）
        → ld.so：查 .rela.plt[1] → 符号 printf → 在 libc 的 .dynsym 里查到地址
        → *0x4008 = libc 里的真实地址 → jmp 过去
   第二次调用 printf：
   main: call 1040 → 1040: jmp *0x4008（现在直接是 libc 地址）→ 到底
```

**第一次多付「一次 `push` + `jmp` + 符号查找」，之后每次只多付「一次间接跳转」。**

`-fPIC` 的动机正在这里：同一份 `libc.so` 的只读页要被**所有进程共享**，所以既不能有写死的绝对地址（装载时要改代码），也不能有按装载地址改写的 `.data` 项（每进程一份私有副本）——于是有了 **GOT**（变量地址放表里）与 **PLT**（`call` 本模块内的桩，桩再查 GOT）。副作用是 ELF 默认允许**符号插入**，编译器不能假设「本文件的 `foo` 就是最终 `foo`」，**库内互调也要过一次 PLT**：`LD_PRELOAD` 能生效全靠这条语义。

### 同一个 `_start`，两种机器码

`_start`（来自 `crt1.o`，源码一模一样）在两种链接方式下编出**结构不同**的代码：

```
静态链接 s_static（ET_EXEC，固定 0x401550）
0000000000401550 <_start>:
  401555: pop  %rsi                  ← argc（内核已摆在栈顶）
  401564: mov  $0x401530,%rdi        ← &main，绝对地址立即数
  40156b: call 402800 <__libc_start_main>   ← 直接 call，目标写死在指令里

动态链接 hello（ET_DYN，入口 0x1060）
0000000000001060 <_start>:
    1065: pop  %rsi
    1074: lea  0xce(%rip),%rdi       ← &main，RIP 相对（PIE 不能写绝对地址）
    107b: call *0x2f3f(%rip)         ← 查 GOT 的间接 call，目标 0x3fc0
```

`0x3fc0` 在 `.got` 里，对应 `R_X86_64_GLOB_DAT  __libc_start_main@GLIBC_2.34`。也就是说：

> **动态链接的程序，连「进入 `main` 之前要调的那个函数」都得先查一次 GOT。**

`call 402800` 是 5 字节、目标在指令流里、分支预测器一眼认出；`call *0x2f3f(%rip)` 是 6 字节、目标在数据里、**必须先读一次 GOT 才知道去哪**。这次读之所以安全，是 `ld.so` 在装载期就把它填好了。**`-fPIE` 强制所有取地址走 RIP 相对——这是「能被搬到任何地方」在指令层的成本。**

---

## 22.5 装载：`execve` 到底做了什么

`./hello` 在 shell 里是 `fork` + `execve("./hello", ["./hello"], envp)`。`fork` 之后那个子进程还拥有 shell 的地址空间；`execve` 是**唯一一个会把地址空间整个换掉、还能返回成功的系统调用**——它不返回旧的 `rip`，而是返回到新的 `e_entry`。

内核侧（`fs/exec.c` → `fs/binfmt_elf.c`）压成九步，前四步决定「认不认这个文件」，后五步决定「新地址空间长什么样」：

| # | 动作 | 关键判断 / 字段 | 失败会怎样 |
| --- | --- | --- | --- |
| 1 | `open_exec`，读前 128 字节 | `BINPRM_BUF_SIZE = 128` | `EACCES` / `ENOEXEC` |
| 2 | 认 magic `7f 45 4c 46` | 不匹配就试 `binfmt_script`（`#!`） | `Exec format error` |
| 3 | 校验 `e_ident`/`e_type`/`e_machine` | `ELFCLASS64`、`EM_X86_64` | `ENOEXEC` |
| 4 | 读 program headers，找 `PT_INTERP` | 有则读出解释器路径，再 `open_exec` 一次 | `ELIBBAD` |
| 5 | `begin_new_exec` → 释放旧 `mm` | `exec_mmap()`，旧 VMA 全 `unmap` | — |
| 6 | 定 base：`ET_EXEC` 用 `p_vaddr`；`ET_DYN` 在 `ELF_ET_DYN_BASE` 附近取随机值 | `arch_mmap_rnd()` | — |
| 7 | 每段 `PT_LOAD` 一次 `elf_map` → `vm_mmap` | `PF_R→PROT_READ`、`PF_W→PROT_WRITE`、`PF_X→PROT_EXEC` | `ENOMEM` |
| 8 | `MemSiz > FileSiz` 的尾部：`set_brk` 建匿名 VMA | 内核保证清零 | — |
| 9 | `create_elf_tables` 铺栈 + `start_thread(regs, entry, sp)` | `AT_ENTRY = e_entry + load_bias` | — |

第 4 步与第 9 步回答一个常见误解：**有 `PT_INTERP` 时，内核跳的不是 `e_entry`，而是 `ld.so` 的入口。** `readelf -h hello` 说 `0x1060`，那是 `_start`；但内核先跳到 `ld-linux-x86-64.so.2`，`ld.so` 装完 `libc` 才「转到」`AT_ENTRY`。证据是 `LD_DEBUG=libs` 最后一行：

```
   3296715:	calling init: /lib64/ld-linux-x86-64.so.2
   3296715:	calling init: /lib/x86_64-linux-gnu/libc.so.6
   3296715:	initialize program: ./hello
   3296715:	transferring control: ./hello     ← 到这一行，_start 才第一次执行
```

`transferring control` 之前，`hello` 的代码一条都没跑过。**所以 `_start` 那句 `pop %rsi` 之前，已经发生过：读 ELF、建 `mm_struct`、4 次 `mmap`、`libc` 的 `mmap`、以及 83 次重定位。**

### 入口那一刻：栈上有什么、没有什么

内核在 `create_elf_tables` 里往新栈顶摆的东西，顺序固定（从高到低）：参数字符串与环境字符串 → `AT_NULL` / `AT_ENTRY`(主程序入口+偏置) / `AT_BASE`(ld.so 基址) / `AT_PHDR` / `AT_RANDOM`(16 字节随机，canary 的种子) → `envp[]`、`argv[]` 两个指针数组 → `argc`，**`rsp` 就钉在 `argc` 上**。

`getauxval` 读出的真实值（每次运行都不同）。三个关系全部对得上：`AT_ENTRY − 0x1060 = 0x556c9e9d7000` 就是 PIE 装载基址；`AT_PHDR − 基址 = 0x40 = e_phoff`；`AT_PHNUM = 13` 正是 `readelf` 报的条数——**内核没有额外存「基址」，它只是把 `e_phoff` 加到偏置上，反解一次就能得到偏置**：

```
AT_PHDR   = 0x0000556c9e9d7040     AT_PHNUM = 13
AT_ENTRY  = 0x0000556c9e9d8060     ← 0x1060 + 偏置 0x556c9e9d7000
AT_BASE   = 0x00007f199e425000     ← ld.so 的基址，和主程序完全不同
AT_RANDOM = 0x00007ffeccf090f9     AT_PAGESZ = 4096
```

关于「入口的栈」，有一句常见说法需要修正：

> 常见说法：**「此时栈上还没有 `argc`/`argv`，入口汇编才铺栈。」**
> 准确说法：**内核已经把 `argc/argv/envp/auxv` 全摆在栈顶了**（`AT_RANDOM` 就在里面）；`_start` 做的是**把这堆东西整理成 ABI 能用的帧**——`pop %rsi` 取走 `argc`、`mov %rsp,%rdx` 记下 `argv` 起点、`and $-16,%rsp` 对齐、再 `push` 两个参数。

`hello` 的 `_start` 里关键的几行就是这个动作：

```asm
1060: xor  %ebp,%ebp                 ← 标记「最外层帧」，backtrace 到此为止
1065: pop  %rsi                      ← argc：从栈顶弹出，rsp 从此 +8
1066: mov  %rsp,%rdx                 ← argv：现在 rsp 指向 argv[0]
1069: and  $0xfffffffffffffff0,%rsp  ← 16 字节对齐（SysV ABI 硬要求）
106d: push %rax                      ← 补 8 字节，使 call 时 (rsp+8)%16==0
106e: push %rsp                      ← 第七个参数 stack_end
1074: lea  0xce(%rip),%rdi           ← &main
107b: call *0x2f3f(%rip)             ← __libc_start_main（查 GOT）
1081: hlt                            ← 理论上不返回
```

**为什么 `pop` 之后才对齐？** 内核把 `argc` 钉在栈顶时只保证 `rsp ≡ 8 (mod 16)`，`pop` 掉 8 字节恰好回到 16 对齐——这两条的顺序**是「内核给的栈布局」与「ABI 要求」之间那 8 字节差额的补丁**。

---

## 22.6 地址空间布局：一张图解释「为什么地址每次都不一样」

x86-64 在四级页表下可用 47 位用户态地址（`0x0000_7fff_ffff_ffff` 以下），典型动态链接进程的布局：

```
 0xFFFFFFFFFFFFFFFF ┌──────────────────────────────┐
                    │  内核空间（用户态不可访问）     │  高半区
 0xFFFF800000000000 ├──────────────────────────────┤ ← 中间那道「洞」
                    │  （非规范地址，访问即 #GP）     │
 0x00007FFFFFFFFFFF ├──────────────────────────────┤
                    │  [stack]  ↓ 向低地址增长      │ ← 从顶部往下
                    │  …                            │
                    │  mmap 区 ↓ 向低地址分配        │ ← libc / ld.so / 匿名
                    ├──────────────────────────────┤
                    │  （大段空洞）                  │
                    ├──────────────────────────────┤
                    │  [heap]  ↑ brk 向上长         │
                    ├──────────────────────────────┤
                    │  .bss .data .got .got.plt     │ RW
                    │  .rodata                      │ R
                    │  .text .plt .init             │ R E
                    │  ELF header / PHDR / .interp  │ R
 0x0000555555554000 └──────────────────────────────┘ ← PIE 的默认基址（未随机时）
 0x0000000000000000
```

### ASLR 随机化的是哪几段

`/proc/self/maps` 跑两次，同一个程序：

```
$ ./maps        # 第 1 次
55dc22aca000-55dc22acb000 r--p 00000000 … /tmp/opencode/elf/maps   ← PIE 基址
55dc22acb000-55dc22acc000 r-xp 00001000 … /tmp/opencode/elf/maps
55dc22ace000-55dc22acf000 rw-p 00003000 … /tmp/opencode/elf/maps
55dc2c3cb000-55dc2c3ec000 rw-p 00000000 00:00 0        [heap]
7f344197d000-7f34419a3000 r--p 00000000 … /usr/lib/…/libc.so.6
7f34419a3000-7f3441af9000 r-xp 00026000 … /usr/lib/…/libc.so.6
7f3441b6e000-7f3441b6f000 r--p 00000000 … /usr/lib/…/ld-linux-x86-64.so.2
7ffd18389000-7ffd183aa000 rw-p 00000000 00:00 0        [stack]

$ ./maps        # 第 2 次（同一程序）
559a6227b000-559a6227c000 r--p 00000000 … /tmp/opencode/elf/maps
559a81144000-559a81165000 rw-p 00000000 00:00 0        [heap]
7f2f3ce09000-7f2f3ce0a000 r--p 00000000 … /usr/lib/…/ld-linux-x86-64.so.2
7fffe3619000-7fffe363a000 rw-p 00000000 00:00 0        [stack]
```

| 区域 | 第 1 次 | 第 2 次 | 熵来源 |
| --- | --- | --- | --- |
| PIE 基址 | `0x55dc22aca000` | `0x559a6227b000` | `arch_mmap_rnd()`，约 28 位页号随机 |
| heap | `0x55dc2c3cb000` | `0x559a81144000` | 紧跟 PIE 之后 `brk`，**继承 PIE 的随机** |
| libc 基址 | `0x7f344197d000` | `0x7f2f3cc18000` | mmap 基址随机 |
| `ld.so` 基址 | `0x7f3441b6e000` | `0x7f2f3ce09000` | mmap 基址随机 |
| 栈顶 | `0x7ffd183aa000` | `0x7fffe363a000` | 栈顶随机，约 22 位 |

**① 随机粒度是页**（`2²⁸ × 4 KiB = 1 TiB` 里选）。**② heap 跟着 PIE 走**：`0x55dc2c3cb000 − 0x55dc22aca000 = 0x9901000` 两次一样——**泄露 PIE 基址就等于泄露 heap 基址**。**③ libc 与 ld.so 独立随机**。**④ 两次的偏移列完全一致**：随机化只改基址、不改段内相对结构，**这就是「相对布局编译期定、绝对位置装载期定」的证据**。`AT_ENTRY − 0x1060` 反解出的基址与第一行吻合——**「基址」从不被保存，它是被算出来的**。

---

## 22.7 从入口到 `main`：`main` 之前到底跑了什么

`_start` 只做一件事：调 `__libc_start_main`。真正的启动序列在 libc 里：

```
_start
  └─ __libc_start_main(main, argc, argv, init, fini, rtld_fini, stack_end)
       ├─ ①② 保存 rtld_fini、初始化 TLS（__libc_setup_tls）、__libc_init_first
       ├─ ③ _dl_catch_exception 包住下面全部（异常能安全穿过）
       ├─ ④ 走 .init_array：for (i…) init_array[i](argc, argv, envp)
       │        ← C++ 全局构造、__attribute__((constructor))、frame_dummy
       ├─ ⑤ *main(argc, argv, envp)
       ├─ ⑥ exit(result)
       │     ├─ 走 .fini_array（逆序）：C++ 全局析构、destructor
       │     ├─ 走 atexit 注册的 handler（**逆序**，LIFO）
       │     ├─ _IO_cleanup：flush 所有 stdio 缓冲
       │     └─ _exit(2)：真正进内核
       └─ ⑦ 不会返回
```

第 ④ 步就是「`main` 之前的崩溃」的全部来源，而且它**有物理载体**——`.init_array` 是一个装着函数指针的数组。`hello` 里只有一项（`crtbegin.o` 放的 `frame_dummy`，读出来是 `0x1140`）；带构造函数的程序就多一项：

```
$ readelf -x .init_array crash
  0x00003dc8 40110000 00000000 49110000 00000000
             └ 0x1140 frame_dummy  └ 0x1149 boot ← 我写的构造函数
$ objdump -d crash | sed -n '/<boot>:/,+6p'
0000000000001149 <boot>:
    1157: call   1030 <puts@plt>
    116b: mov    0x2ebe(%rip),%rax    # 4030 <p>
    1172: movl   $0x1,(%rax)          ← *p = 1，p 是 NULL
```

于是「`main` 之前就崩」有完整解释：

```
$ ./crash
[ctor] about to touch config
Segmentation fault (core dumped)
$ echo $?
139                                 ← 128 + 11 (SIGSEGV)
```

`[main] never printed` 一次都没出现。`gdb` 的 backtrace 是 `boot` ← `call_init` ← `__libc_start_main` ← `_start`——**栈上根本没有 `main` 这一帧**，因为 `main` 还没被调。这足以否掉「`main` 是程序入口」：**`main` 只是 `__libc_start_main` 的第 4 个参数。**

### 构造顺序：`.init_array` 的顺序就是链接顺序

比崩溃更隐蔽的是「没崩，但值是错的」。两个编译单元各带一个构造函数：`boot_a.c` 在构造里 `later = 42`，`boot_b.c` 在构造里打印 `later`，`main` 也打印一次。谁先跑取决于**链接命令行里 `.o` 的顺序**：

```
$ gcc -O0 boot_a.c boot_b.c -o boot_ab && ./boot_ab
[ctor A] later := 42 / [ctor B] later = 42 / [main ] later = 42
$ gcc -O0 boot_b.c boot_a.c -o boot_ba && ./boot_ba
[ctor B] later = 0 / [ctor A] later := 42 / [main ] later = 42      ← 读到的是 0！
```

**同一份源码、同样两个文件，只把命令行顺序换一下，`boot_b` 就看到了未初始化的 0。** 这就是 C++ 著名的「静态初始化顺序灾难」的 C 版本。根因不是 bug：`.init_array` 的语义就是**一个按链接顺序排列的函数指针数组**，libc 只负责从头调到尾，不知道也不关心它们之间的依赖。

---

## 22.8 实测：把上面每句话跑出来

环境：`Linux 6.12 x86-64`、`gcc 12.2.0`、`glibc 2.36`、`AMD EPYC 7K62`（2 vCPU，2.0 GHz）。**`strace` 在本机不存在**（`which strace` 无输出），所以第 ⑤ 项用 `LD_DEBUG` + `AT_*` + `/proc/self/maps` 替代，并明确标出哪一条是推断。

### ① 三种 ELF 类型一次看全，② `maps` 只看形状

```
$ for f in hello.o hello s_static; do printf "%-11s " $f; readelf -h $f | awk -F: '/Type:/{print $2}'; done
hello.o     REL (Relocatable file)
hello       DYN (Position-Independent Executable file)
s_static    EXEC (Executable file)

$ ./maps | awk '{print $2}' | sort | uniq -c | sort -rn | head -3
      9 rw-p        ← 匿名可写段（heap / 栈尾 / .bss 尾）
      5 r-xp        ← 可执行段：maps / libc / ld.so / vdso
      4 r--p        ← 只读段
```

`readelf` 可用，无需 `objdump -h` 兜底（后者输出更短，适合快速对比 `.text` 与 `.bss` 的量级差）。**行数会随 glibc 版本变化，但「每个 `.so` 恰好一段 `r-xp`」不变**——这就是「代码段可以跨进程共享」的观测形式。

### ③ `LD_DEBUG`：装载器自己报账

```
$ LD_DEBUG=libs ./hello 2>&1 | grep -vE '^\s*$'
   3296715:	find library=libc.so.6 [0]; searching
   3296715:	 search cache=/etc/ld.so.cache
   3296715:	  trying file=/lib/x86_64-linux-gnu/libc.so.6
   3296715:	calling init: /lib64/ld-linux-x86-64.so.2
   3296715:	calling init: /lib/x86_64-linux-gnu/libc.so.6
   3296715:	initialize program: ./hello
   3296715:	transferring control: ./hello
   3296715:	calling fini: /lib64/ld-linux-x86-64.so.2
```

第一行是搜索顺序的实证：**先查 `ld.so.cache`，没有再走 `LD_LIBRARY_PATH`/`RUNPATH`/默认路径**；`init` 与 `fini` 顺序严格相反，这是 §22.7 第 ⑥ 步的证据。再让它报时间账：

```
$ LD_DEBUG=statistics ./hello 2>&1 | grep -E 'total|relocation|load objects|number of'
	  total startup time in dynamic loader: 49431 cycles
	            time needed for relocation: 10329 cycles (20.8%)
	                 number of relocations: 83
	           time needed to load objects: 11749 cycles (23.7%)
	           final number of relocations: 86
```

换算：`49,431 cycles ÷ 2.0 GHz ≈ 24.7 µs`。其中重定位 10,329 cycles ≈ 5.2 µs、装载对象 11,749 cycles ≈ 5.9 µs，两项合计 44.5%——**剩下 55% 花在符号查找与 `ld.so` 自身初始化上**。

### ④ 静态 vs 动态：文件大小、`ldd`、启动开销

```
$ ls -lh s_dyn s_static
-rwxr-xr-x 1 root root  16K s_dyn
-rwxr-xr-x 1 root root 745K s_static          ← 762,424 B / 15,960 B ≈ 47.8 倍
$ ldd s_dyn
	libc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x00007fe84916c000)
$ ldd s_static
	not a dynamic executable
```

文件大 47.8 倍，但**映射进地址空间的部分差距更大**（按 §22.3 的 `MemSiz` 逐段相加）：

| 版本 | 文件字节 | 4 个 `LOAD` 的 `MemSiz` 之和 | `ldd` | `PT_INTERP` |
| --- | --- | --- | --- | --- |
| 动态 `s_dyn` | 15,960 | 2,965（≈ 2.9 KiB） | 3 个依赖 | 有 |
| 静态 `s_static` | 762,424 | 697,757（≈ 681 KiB） | `not a dynamic executable` | 无 |

**235 倍差距**来自一处：静态版本把 `libc.a` 里被引用到的目标文件全塞进自己的 `R E` 段（`.text` 从 425 B 涨到 490,753 B）。动态版本那 2.9 KiB 只是「外壳」，真正的 `printf` 在 `libc.so` 里，**被这台机器上所有进程共享**。

启动开销用 `fork + execve + waitpid` 逐次计时，各 400 次、重复 3 轮（p5 代替 min 以避开调度抖动）：

```
$ for i in 1 2 3; do ./exectime ./s_dyn 400; ./exectime ./s_static 400; done
./s_dyn      n=400  p5=411.1us  median=434.2us  p95=3284.0us
./s_static   n=400  p5=285.7us  median=303.9us  p95=3990.7us
./s_dyn      n=400  p5=407.6us  median=432.4us  p95=4677.2us
./s_static   n=400  p5=289.7us  median=308.5us  p95=3741.7us
./s_dyn      n=400  p5=410.0us  median=446.7us  p95=5464.9us
./s_static   n=400  p5=290.3us  median=317.9us  p95=4649.1us
```

三轮几乎完全重合：**动态 ≈ 434 µs，静态 ≈ 305 µs，差 ≈ 130 µs / 次**。这 130 µs 可以拆（量级结论）：

| 项 | 量级 | 依据 |
| --- | --- | --- |
| `ld.so` 初始化 + 重定位 | ≈ 25 µs | `LD_DEBUG=statistics` 报的 49,431 cycles |
| `libc.so` 首次触碰的缺页 | 22 次 minor fault × 1–2 µs ≈ 30 µs | `getrusage`：动态 70–73 次 vs 静态 49–50 次 |
| 额外 `mmap`/`mprotect` | 十几次 × 0.5–1 µs ≈ 10 µs | 主程序 4 段 + `libc` 4 段 + `ld.so` 5 段 + RELRO `mprotect` |
| 符号查找（哈希 + 字符串比较） | 几十 µs | 83 次重定位摊到 `libc` 的 `.gnu.hash` 上 |

**共享库省磁盘、省内存，但每次启动付一次装载税**：跑 30 天的服务无所谓；每秒 fork 一万次的 CGI/`xargs -P` 场景，130 µs × 10⁴ = 1.3 s/s，直接压满一个核。

### ⑤ `strace -e trace=execve`：本机没有，用三件套替代

**明确声明：`strace` 未安装**（Debian 12 默认不装）。所以「一次 `execve` 是否发生」不靠 syscall 跟踪，而靠三个可观测物交叉验证。三条**直接**证据：`LD_DEBUG=libs` 的 `transferring control: ./hello` 是最后一行（入口前代码没跑过）；`AT_ENTRY − e_entry` 与 `maps` 第一行吻合（入口被偏置了多少）；`/proc/self/maps` 显示主程序 4 段 + 1 行 `[stack]`、无 `rwxp`（每段 `LOAD` 各映射一次、权限来自 `p_flags`）。一条**推断**：「`mmap` 被调用了几次」无法直接数（需 `strace -c`）；补这一项要在装了 `strace` 的机器上跑 `strace -f -e trace=mmap,mprotect,execve ./hello`，应当看到 **1 次 `execve` + 约 4 次主程序 `mmap` + `libc`/`ld.so` 各若干次 + 1–2 次 `mprotect`（RELRO）**。

### ⑥ 一个能自证的实验：`ldd` 的退出码不可信

```
$ ldd ./use_norpath            # 依赖库已被移走
	libfoo.so => not found
$ echo $?
0
$ env -u LD_LIBRARY_PATH ./use_norpath
./use_norpath: error while loading shared libraries: libfoo.so: cannot open shared object file: No such file or directory
$ echo $?
127
```

**`ldd` 列出 `not found` 却返回 0。** 它只是把 `LD_TRACE_LOADED_OBJECTS=1` 传给链接器让它「打印依赖然后退出」——那个模式下 `ld.so` 打印完就正常退出，**根本没走到「没找到 → `_exit(127)`」那一步**。所以把 `ldd ./app` 当 CI 健康检查全部无效；正确写法是 `ldd ./app | grep -q 'not found' && exit 1`。

---

## 22.9 三列账：这套机制承诺了什么、收了多少、从哪开始失效

| 维度 | 承诺了什么 | 为此付了什么 | 从哪里开始失效 |
| --- | --- | --- | --- |
| **一次编译，到处 mmap** | 同一份 ELF 在任何 x86-64 Linux 上按 `PT_LOAD` 描述映射 | 装载器要实现完整语义：`p_offset ≡ p_vaddr (mod p_align)`、`NOBITS` 补零、`PT_GNU_RELRO` 二次 `mprotect` | `GNU_PROPERTY`/`NOTE` 的新语义、`i386` 兼容层；跨发行版还要过符号版本 |
| **代码共享** | 1000 个进程跑同一个 `libc.so`，内存里只有一份 `R E` 页 | 可执行段必须只读，于是引入 `GOT`——**每进程私有的可写表** | GOT 一改就不能共享；`-z now` + 大 GOT 把「共享」变成每进程一份 |
| **符号延迟解析** | 库可独立升级，只要符号还在，可执行文件不用重链 | 每次调用多一次间接跳转；首次调用多一次符号查找；需要 `PLT` | 符号版本不匹配、`LD_LIBRARY_PATH` 污染、`RUNPATH` 不传给传递依赖 |
| **ASLR** | 代码注入类攻击不知道往哪跳 | 调试、core dump、`perf` 采样、JIT 都要额外处理（`setarch -R` 才可复现） | 信息泄露一次就全废；heap 继承 PIE 偏移，泄露一处等于泄露两处 |
| **`main` 之前有构造** | C++ 全局对象、`constructor` 属性、库自注册能自动跑 | 启动期多一段不可控代码；顺序依赖链接顺序 | 顺序灾难、`main` 前崩溃、`stdio`/`iostream` 未就绪 |
| **静态链接** | 一个文件拷到任何同架构机器就能跑 | 47.8 倍文件、235 倍地址空间；升级 `libc` 要重编 | `getaddrinfo`/`dlopen`/NSS 在静态程序里仍需动态加载 |

一句话收束：**ELF 买到的是「编译器只需约定、装载器只需读表」这个分层，付出的是「地址到最后一刻才确定」，于是所有不确定性（重定位、PLT、ASLR、库版本）都被赶到运行期。**

---

## 22.10 踩坑清单：现象 → 根因 → 怎么定位 → 怎么修 → 怎么防复发

| 现象 | 根因（指回本篇） | 定位命令 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| `version 'GLIBC_2.34' not found` | `.gnu.version_r` 里记了版本需求，目标机的库没有该版本（§22.3） | `objdump -T ./app \| grep GLIBC` 与 `objdump -T /lib/.../libc.so.6 \| grep GLIBC` 两边对 | 在**目标发行版**里构建；或降低工具链的最低 glibc 版本 | CI 加一步「在最终镜像内 `./app --version`」 |
| `cannot open shared object file` | 装载器按 `DT_RPATH`→`LD_LIBRARY_PATH`→`DT_RUNPATH`→`ld.so.cache`→默认路径都找不到（§22.8③） | `LD_DEBUG=libs ./app 2>&1 \| grep -A2 'find library'` | 设 `RUNPATH`（`-Wl,-rpath,$ORIGIN/../lib`）、装到 `/usr/lib` 并 `ldconfig`、或 `patchelf --set-rpath` | 别用 `LD_LIBRARY_PATH` 解决部署问题 |
| `ldd` 说没问题，程序起不来 | `ldd` 退出码不反映失败，且不校验符号版本（§22.8⑥） | `ldd ./app \| grep 'not found'`；然后 `./app --version` | 把「能启动」作为判据 | CI 健康检查直接执行二进制；禁写 `ldd ./app \|\| exit 1` |
| `RPATH` 改了不生效 / 传递依赖找不到 | `DT_RPATH` 对传递依赖生效但会被 `LD_LIBRARY_PATH` 覆盖；`DT_RUNPATH` 只管本模块的直接 `NEEDED`，**不传给依赖的依赖**（§22.4） | `objdump -p ./app \| grep -E 'RPATH\|RUNPATH'`；`--disable-new-dtags` 生成 `RPATH` | 需要覆盖传递依赖就用 `RPATH`（`-Wl,--disable-new-dtags,-rpath,...`）；否则把依赖摊平到同一目录 | 链接后检查是哪一个；两者语义不同，不能混称 |
| `-static` 之后 `getaddrinfo` 失效 / 返回 `EAI_SYSTEM` | `getaddrinfo` 走 NSS，NSS 模块（`libnss_dns.so.2` 等）**只能 `dlopen` 加载**；静态二进制没有 `ld.so`（§22.9 末行） | `./app` 看 `EAI_SYSTEM` + `errno`；有 `strace` 时 `strace -e openat ./app \| grep nss` | 改 `-static-pie` 并保留 `libnss_*`；或用纯数字 IP、自带解析（c-ares）；`nsswitch.conf` 的 `hosts` 只留 `files` | 静态链接前先问「会不会 `dlopen`、查 DNS、读 `/etc/passwd`」——三者都依赖 NSS |
| `main` 前崩溃，backtrace 里没有 `main` | 崩在 `.init_array` 里的构造函数（§22.7） | `gdb ./app` 看栈顶是不是 `call_init`；`readelf -x .init_array ./app` 翻符号 | 把初始化搬到 `main` 第一行 | 构造函数只做 POD 赋值；禁止 `printf`/分配/加锁 |

---

## 22.11 一页速记

```
【一句话】./a.out 不是「运行一个文件」：内核把它按 program header 拆成几段 mmap 进新地址空间，
          再跳到 e_entry（动态链接时先跳 ld.so，由 ld.so 转到 AT_ENTRY）。

【四个阶段】
  预处理 .c → .i   只做文本：粘 include、展开 define（9 行 → 2007 行）
  编译   .i → .s   .data 里有值，.bss 里只有 .zero
  汇编   .s → .o   ET_REL；跨符号引用全留成 relocation（位移填 0）
  链接   .o → ELF  合并节 → 布局 → 按 S+A−P 填洞；ET_EXEC（静态）或 ET_DYN（PIE/so）

【两套视图，同一份字节】
  program header 13 条：内核用，4 个 PT_LOAD 决定 4 次 mmap 与权限（R/RX/R/RW）
  section header 37 条：链接器用；无 A(alloc) 的节（.symtab/.debug_*）不进内存
  硬约束：p_offset ≡ p_vaddr (mod p_align)；MemSiz−FileSiz = .bss（内核补零）
  17480 = 64 + 13×56 + 内容 + 37×64     ← 一字节不差，可用来验算任何 ELF

【重定位】填入值 = S + A − P；PC32 的 A = −4（相对下一条指令）
  同一个 _start：静态 call 402800（写死）；动态 call *0x2f3f(%rip)（先读 GOT）

【两次调用 printf】
  第一次：jmp *GOT(=plt+6) → push idx → jmp PLT0 → _dl_runtime_resolve → 回填 GOT
  之后：  jmp *GOT(=libc 地址) → 到底             （每次多一次间接跳转）

【ASLR 随机的是「基址」】
  PIE 基址（页粒度，~28 位）、mmap 区（libc/ld.so）、栈顶（~22 位）
  heap 不独立随机：它 = PIE 基址 + 固定偏移 → 泄露一处等于泄露两处
  反解基址：base = AT_ENTRY − e_entry；AT_PHDR − base = e_phoff

【启动顺序】
  _start → __libc_start_main → .init_array → main → exit → .fini_array → atexit → _exit
  main 之前崩 = 崩在 .init_array；main 帧不在栈上

【三个数字】
  动态 hello：4 段 LOAD，映射 2,965 B；装载器自报 49,431 cycles ≈ 25 µs @2 GHz
  静态：文件 47.8×，地址空间 235×，每次启动省约 130 µs
```

---

## 22.12 自检 3 题

**Q1.** 一个 17,480 字节的 PIE，`readelf -h` 报 `e_phoff=64, e_phnum=13, e_phentsize=56, e_shoff=15112, e_shnum=37, e_shentsize=64`。算出 program header table 之后的第一个字节偏移，以及 section header table 之后还有没有字节。再解释：为什么 `MemSiz − FileSiz = 8` 的那 8 个字节不需要存在于文件里？

<details><summary>答案</summary>

**第一问**：`64 + 13 × 56 = 792 = 0x318`，恰好是 `.interp` 的文件偏移——program header table 紧跟 ELF header、第一节数据紧跟它，中间没有填充。

**第二问**：`15112 + 37 × 64 = 17480`，正好等于文件大小——**section header table 之后一个字节都没有**。

**第三问**：那 8 字节是 `.bss`，类型 `NOBITS`——只有虚拟地址和长度。因为**它的初始值按定义全是 0**，值恒为 0 就不需要存。内核在 `elf_map` 之后对 `[vaddr+FileSiz, vaddr+MemSiz)` 建匿名 VMA，缺页时给零页。**「不占磁盘」不是压缩技巧，是「值恒为 0」的必然后果。**
</details>

**Q2.** `hello.o` 里 `main` 有一条 `mov 0x0(%rip),%r13d`，跟着 `R_X86_64_PC32 g_zero-0x4`（字段偏移 0x10）。链接后 `main` 落在 `0x1149`、`g_zero` 落在 `0x4034`。算出填进那 4 个字节的值，并解释为什么加数是 `-0x4`。再说明：为什么动态链接不能像静态那样把 `call __libc_start_main` 写成直接跳转？

<details><summary>答案</summary>

**第一问**：`P` 是**字段自己的地址**，`0x1149 + 0x10 = 0x1159`。代入得 `S + A − P = 0x4034 − 0x4 − 0x1159 = 0x2ED7`，与 `objdump` 里 `mov 0x2ed7(%rip),%r13d` 的编码 `d7 2e 00 00` 一致。验证：目标 = `0x115D + 0x2ED7 = 0x4034`，正是 `g_zero`。

**第二问**：RIP 相对位移是相对**下一条指令**算的，而 `P` 是字段地址。字段占 4 字节，下一条指令 = `P + 4`，要让 `S + A − P` 等于「相对下一条指令的偏移」，必须 `A = −4`。

**第三问**：`__libc_start_main` 在 `libc.so.6` 里，映射到哪由装载时的随机偏置决定，**链接时不可能知道**。静态链接时 `libc.a` 的代码被直接拷进可执行文件，链接器按 `0x400000` 算出 `0x402800`，于是能写 `call 402800`。动态链接只留下 `R_X86_64_GLOB_DAT`，由 `ld.so` 在装载期把真实地址写进 GOT（`0x3fc0`）。**`call *GOT` 不是优化选择，是「地址在链接时不存在」的唯一可行写法。**
</details>

**Q3.** 一个 C++ 服务在容器里偶发启动即崩，日志只有最后一行业务输出，没有 stack trace；`gdb` 挂在 core 上发现栈顶是 `_dl_init`。另一个服务报 `version 'GLIBC_2.34' not found`，而运维坚称「`ldd` 检查过，全部 OK」。分别给出根因、一条定位命令、一条修复方案。

<details><summary>答案</summary>

**第一个：崩在 `main` 之前。** 栈顶是 `_dl_init`（或 `call_init`）说明崩溃发生在 `.init_array` 的构造函数里，`main` 还没进入——所以栈上没有 `main` 帧。定位：`readelf -x .init_array ./app` 拿函数指针数组，用 `addr2line -e ./app -f -C <地址>` 翻成符号；或在 `gdb` 里对 `_dl_init` 下断点逐个 step。修复：把「依赖其它全局对象 / 需要 `main` 之后状态」的逻辑搬到 `main` 第一行。

**第二个：`ldd` 通过不代表符号版本可用。** `ldd` 只回答「库文件找没找到」，而 `GLIBC_2.34` 是记在 `.gnu.version_r` 里的**符号版本需求**，由 `ld.so` 解析符号时校验——§22.8⑥ 实测：`ldd` 连 `not found` 都返回 0。定位：`objdump -T ./app | grep GLIBC` 看需求，`objdump -T /lib/x86_64-linux-gnu/libc.so.6 | grep GLIBC_2.3` 看提供。修复：在**与运行环境同版本的基础镜像**里构建，或降低工具链的最低版本；CI 加一步「在最终镜像里执行 `./app --version`」——**这是唯一能真正验证的判据**。
</details>

---

## 22.13 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 指令编码、寄存器与调用约定：本篇 `_start` 里每条指令的来历 | → base/11 |
| 读汇编：把 `mov/call/lea` 与栈帧读成语言概念（本篇只给结论） | → base/12 |
| 页表四级结构、缺页时 CPU 到底在干什么（`mmap` 之后的事） | → base/26 |
| `malloc`/`brk`/`mmap` 在用户态怎么用这块地址空间 | → base/27 |
| ASLR、NX、canary 的攻防全谱与绕过手法（本篇只讲机制） | → base/33 |
| 进程控制块与 `fork`/COW：`execve` 的另一半 | → base/23 |
| 收口：位层 / 内核层 / 协议层三本账合订 | → base/56 |
| Go 的编译流水线：`go build` 如何把包变成可执行文件 | [go/27](../go/27-compile-pipeline.md) |
| Go 的工具链布局：`GOROOT`、`pkg`、链接器与 `-ldflags` | [go/2](../go/2-toolchain-layout.md) |
| 全书总纲：四条主线与 56 篇清单 | [0-summary](0-summary.md) |
