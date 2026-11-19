# 12. 读汇编：一个函数调用在机器码里到底长什么样

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · CPU 与 GPU：算力的物理形状 ｜ **编号**：12 / 56
> **本篇定位**：Part II 的第三篇。11 讲「指令集是硬件与编译器的合同」，本篇只做一件事——**把那份合同逐条读出来**。全篇只有一条推论链：`指令只有四类动作 → 操作数只有三种来源 → 内存操作数写成「基址 + 索引×比例 + 偏移」 → 于是数组下标、结构体字段、栈上变量全部塌缩成偏移 → 于是语言里的每个概念都能在机器码里指出一个地址`。这是全书的验伪器：13 讲缓存时要读 `addl (%rdi),%eax`，22 讲 ELF 时要读 `call ...@plt`，27 讲 `malloc` 时要读 `sub $0x18,%rsp`，它们都默认你已经会读这 20 条指令。
> **读法**：§12.2 的表只抄「副作用」那一列（抄完你就知道为什么 `cmp` 后面能接 `jne`）；§12.4 的 21 条汇编请对着注释逐行读，读到 `call` 那行停下来，自己在纸上画一遍栈；§12.5 与 §12.8 的数字请在自己机器上重跑（gcc 版本不同，指令会变，结论不变）；§12.6 的四个练习先遮住答案猜一遍——猜错的那一个才是你真正没懂的地方。
> **不在本篇**：调用约定的完整 ABI（哪些寄存器 callee-saved、第 7 个参数放哪） → `base/11`；流水线、分支预测、`cmov` 为什么能省一次预测失败 → `base/10`；缓存行与访存代价 → `base/13`；ELF、装载与 PLT/GOT 怎么把 `call` 变成一个地址 → `base/22`；`malloc`、栈与堆的边界 → `base/27`。

---

## 12.1 引子：一次「`-O2` 把空循环删了」的复盘

2025-11 上旬，一个团队给 RPC 框架做容量压测。他们的计时脚本里有一段「校准空循环」，用来把机器折算成统一的「等效迭代数」，好让不同代次的机器能横向比较：

```c
/* 校准：跑 2 亿次空循环，量出这台机器 1 秒能跑多少圈 */
static void calibrate(void) {
    for (int i = 0; i < 200000000; i++) { }
}
```

发布构建用的是 `-O2`。上线前有人顺手跑了一次校准，得到 **0.000001 秒**，于是「1 秒 = 2×10¹⁴ 圈」。当天所有机器的压测结果都被乘上了一个荒唐的系数，「新版本比旧版本快 6 倍」的结论直接进了季度汇报。

我把同一份 `bench.c` 按两个优化级别各编了一遍，实测：

| 构建 | `empty(2×10⁸)` | `vol(2×10⁸)`（把循环变量改成 `volatile`） |
| --- | --- | --- |
| `-O0` | 0.517965 s | 0.577311 s |
| `-O0`（再跑一次） | 0.498179 s | 0.580471 s |
| `-O2` | **0.000001 s** | 1.166261 s |
| `-O2`（再跑一次） | **0.000001 s** | 0.726706 s |

`-O0` 下 2 亿圈要 0.50 秒（≈2.5 ns/圈），`-O2` 下同样的 2 亿圈只要 1 微秒——那 1 微秒基本就是 `clock_gettime` 自己的开销。原因只有一条，看 `gcc -S -O2` 的输出就明白了：

```asm
empty_loop:
	ret
```

**整个循环被删了。** 循环体没有副作用，编译器按 C 标准有权认为「这段代码什么都没做」，于是连边界比较都不留。这不是编译器耍赖，是 C 的语义允许；而 `volatile` 把那 2 亿圈救了回来，代价是每圈都必须真的读一次内存、写一次内存。

顺带记一笔：`vol` 那一列在 `-O2` 下两次跑出 1.17 s 与 0.73 s，同一个二进制差 60%——那是调频与调度抖动，**不是代码变了**。任何没标误差范围的时间数字，都只配当量级。

同类事故还有两种形态：「OOM 但只找到一个 4 KB 缓冲」，根因是 `vector::push_back` 被内联进循环，pprof 把分配点归到不相干的 `for`；「加了日志就不复现」，因为 `printf` 把本该被优化掉的读写变成了有副作用的访问。

三起事故的共同点：**你读的是源码，跑的是编译产物**，中间隔着一个只对编译器负责的优化器。把产物读出来，是唯一能验证「我以为机器在做的事」与「机器真的在做的事」是否一致的手段。

**本篇要交付的只有一句话**：只要会读 `mov / cmp / jne / call / ret` 与栈帧，语言里的概念（变量、数组、结构体、函数、返回值、递归、闭包捕获）就全部落地成**地址与偏移**。会了这条，任何「性能玄学」都能当场验伪；不会这条，后面 44 篇都只能靠背。

---

## 12.2 先学 20 条指令：每一行的「副作用」才是重点

一条指令只有三样东西：**做什么（操作码）+ 对谁做（操作数）+ 留下什么痕迹（标志位 / 内存 / 寄存器）**。第三样最容易被忽略，而它恰恰是读懂控制流的关键——`cmp` 之所以能接 `jne`，唯一的原因是 `cmp` 会写标志位。

先把这 20 条（`push`/`pop` 算一对）过一遍。「真实用途」那一列全部来自本篇后面真实出现的汇编，不是教科书例句。

| # | 指令 | 一句话语义 | 本篇里的真实用途 | 副作用 |
| --- | --- | --- | --- | --- |
| 1 | `mov` | 搬运：寄存器↔寄存器、寄存器↔内存、立即数→寄存器 | `movl %edi,-20(%rbp)` 参数落栈 | **不改标志** |
| 2 | `lea` | 只算地址、**不访存** | `leal 23(%rdi,%rdi),%eax` 算 `2x+23` | **不改标志**（可当免费计算器） |
| 3 | `add` | 整数加 | `addl %edx,%eax` 算 `x + base` | ZF SF CF OF |
| 4 | `sub` | 整数减（电路上是加补数） | `subq $32,%rsp` 开栈帧 | ZF SF CF OF |
| 5 | `imul` | 有符号乘 | `imull %esi,%eax`（两个操作数都不确定时） | CF OF；ZF/SF 未定义 |
| 6 | `and` | 按位与 | `andl $-4,%edx` 把 `n` 向下取整到 4 的倍数 | ZF SF；CF=OF=0 |
| 7 | `or` | 按位或 | `orl $1,%eax` 置最低位 | ZF SF；CF=OF=0 |
| 8 | `xor` | 按位异或 | `xorl %eax,%eax` 清零（2 字节，比 `mov $0` 短） | ZF SF；CF=OF=0 |
| 9 | `shl` | 左移，等于 ×2ⁿ | `salq $4,%rdx` 乘 16，算向量块字节数 | CF=移出的位；ZF SF |
| 10 | `shr` | 逻辑右移（高位补 0），等于无符号 ÷2ⁿ | `shrl $2,%edx` 算「4 个一组有几组」 | CF；ZF SF |
| 11 | `cmp` | 做减法但**不写回**，只留标志 | `cmpq %rdx,%rdi` 比较指针是否到末尾 | ZF SF CF OF |
| 12 | `test` | 做与但**不写回**，只留标志 | `testl %esi,%esi` 判 `n <= 0` | ZF SF；CF=OF=0 |
| 13 | `jmp` | 无条件跳 | 尾调用 `jmp add3@PLT` | 无（只改 `rip`） |
| 14 | `je` / `jz` | ZF=1 时跳 | `je .L12`：4 个一组刚好除尽就跳过标量尾巴 | 无 |
| 15 | `jne` / `jnz` | ZF=0 时跳 | `jne .L5`：循环回边 | 无 |
| 16 | `jl` | **有符号**小于（SF≠OF）时跳 | `jl .L6`：`i < n` | 无 |
| 17 | `jg` | **有符号**大于（ZF=0 且 SF=OF）时跳 | `if (a > b)` 的分支版 | 无 |
| 18 | `call` | `rsp -= 8` 压返回地址再跳 | `call add3@PLT` | 写 `rsp`、写栈 |
| 19 | `ret` | 弹出返回地址，跳回去 | 每个函数末尾 | 写 `rsp` |
| 20 | `push` / `pop` | `rsp -= 8` 再存 / 读再 `rsp += 8` | `pushq %rbp` 保存调用者的帧基址 | 写 `rsp`、写/读栈 |

三件事要说清：

**① 只有 ALU 类指令写标志位。** `mov / lea / jmp / call / ret / push / pop` 都不碰。所以看到 `jne`，往上找**最近一条写标志的指令**，那才是「谁在决定跳不跳」；中间夹的 `mov` 全是搬运。

**② `cmp a, b` 在 AT&T 里算的是 `b - a`，不是 `a - b`。** 这是读汇编最致命的坑：`cmpq %rdx,%rdi` 后接 `jne` 是「两者不等」，接 `jb` 才是「`rdi < rdx`（无符号）」。Intel 语法顺序相反，`cmp rdi,rdx` 才是算 `rdi - rdx`——**同一个条件，换语法就换读法**（§12.3 给对照）。

**③ 条件跳转分「有符号」与「无符号」两套。** 看到哪一套，就知道编译器把操作数当成什么类型：

| 语义 | 有符号 | 无符号 | 看哪些标志 |
| --- | --- | --- | --- |
| 相等 / 不等 | `je` / `jne` | `je` / `jne` | ZF |
| 小于 | `jl` / `jle` | `jb` / `jbe` | 有符号看 SF⊕OF；无符号看 CF |
| 大于 | `jg` / `jge` | `ja` / `jae` | 同上 |
| 为零 / 非零 | `jz` / `jnz` | 同左 | ZF |

`base/03` 里那句「`(int)-1 < (unsigned)1` 为假」在汇编层的证据就是这里：编译器生成的必须是 `setb`（无符号），不可能是 `setl`（有符号）。**类型系统的全部工作，最后都落在这张表的一次选择上。**

---

## 12.3 寄存器与操作数语法：先分清「谁的源、谁的目」

### AT&T 与 Intel：同一行，两种写法

GNU 工具链默认输出 AT&T，Intel 手册与多数教程用 Intel 语法。差别是机械的：

| 维度 | AT&T（GNU 默认） | Intel（手册风格） |
| --- | --- | --- |
| 操作数顺序 | **源在前**：`movq %rsi,%rdi` | **目在前**：`mov rdi,rsi` |
| 寄存器前缀 | `%rax` | `rax` |
| 立即数前缀 | `$0x3` | `3` |
| 内存操作数 | `-0x14(%rbp)`、`0x10(%rdi,%rax,4)` | `[rbp-0x14]`、`[rdi+rax*4+0x10]` |
| 宽度后缀 | 指令带后缀：`movb/movw/movl/movq` | 由寄存器名决定宽度，指令不带后缀 |
| 怎么切 | 默认 | `objdump -M intel`、`gcc -S -masm=intel` |

拿 `work` 在 §12.8 那个 `demo` 里的版本做对照——**同一段二进制，两种输出**（注意它的栈偏移是 `-0x14`，与 §12.4 的 `-20` 不同：这正是「偏移是实现细节」的第一个例证）：

```asm
# AT&T
    1155:	push   %rbp
    1156:	mov    %rsp,%rbp
    1159:	sub    $0x18,%rsp
    115d:	mov    %edi,-0x14(%rbp)
    1160:	movl   $0xa,-0x4(%rbp)
```

```asm
# Intel（objdump -d -M intel）
    1155:	push   rbp
    1156:	mov    rbp,rsp
    1159:	sub    rsp,0x18
    115d:	mov    DWORD PTR [rbp-0x14],edi
    1160:	mov    DWORD PTR [rbp-0x4],0xa
```

`sub` 那两行**恰好同向**（都是减 24），`mov` 那两行方向完全相反。所以看汇编前必须先确认语法，否则会把「把参数存进栈」读成「从栈里加载到参数」，整段推理全反。

### 一个寄存器的五个名字

x86-64 有 16 个通用寄存器。`rax` 家族的命名规则是「同一块硅片，按访问宽度切片」：

```
            63                              32 31        16 15    8 7     0
          ┌─────────────────────────────────┬────────────┬───────┬───────┐
   rax    │            高 32 位              │   eax      │  ax   │  al   │
          └─────────────────────────────────┴────────────┴───────┴───────┘
                                                (32 位)     (16 位)  (8 位)
                                                                     ah = 8..15 位
```

- `rax`：64 位，返回值与 `syscall` 调用号；
- `eax`：低 32 位。**写 `eax` 会把高 32 位清零**（x86-64 的硬性规则，不是编译器习惯）；
- `ax`：低 16 位；`al`：低 8 位；`ah`：第 8–15 位（只在没有 REX 前缀时可用）。

这一条「写 32 位就清零高 32 位」在汇编里看得见，而且**正好是有符号/无符号扩宽的分界线**：

```asm
# long w(int x)              { return x; }        ← 有符号：补符号位
w:
	movslq	%edi, %rax
	ret
# unsigned long wu(unsigned u) { return u; }      ← 无符号：写 eax 即清零高位
wu:
	movl	%edi, %eax
	ret
# int low8(int x)            { return x & 0xff; }  ← 只取低 8 位，零扩展
low8:
	movzbl	%dil, %eax
	ret
```

`movslq` 与 `movl` 的差别就是「高 32 位填 1 还是填 0」，`movzbl` 是「取字节并零扩展」。`base/03` 说的「换解释不换位」，在这三条指令里变成三个动词：**补符号、清零、截取**。

其余寄存器（`base/11` 给完整 ABI 表）：

| 寄存器 | 角色 | 谁负责保存 |
| --- | --- | --- |
| `rax` | 返回值；`syscall` 号；乘除法隐含操作数 | 调用者 |
| `rdi` `rsi` `rdx` `rcx` `r8` `r9` | 第 1–6 个整型/指针参数 | 调用者 |
| `rsp` | 栈顶指针，`push/pop/call/ret` 都在动它 | 硬件约定 |
| `rbp` | 帧基址（`-O0` 用它定位局部变量；`-O2` 常被省掉） | 被调用者 |
| `rbx` `r12`–`r15` | 通用，跨调用必须保持 | **被调用者**（要用就先 `push`） |

### 操作数的三种来源，与「地址算术」这一种特殊形式

操作数只可能是三样：**立即数**（`$0x3`）、**寄存器**（`%rdi`）、**内存**（`-0x14(%rbp)`）。内存操作数的完整形式是

```
偏移(基址寄存器, 索引寄存器, 比例)      比例只能是 1 / 2 / 4 / 8
```

于是 `0x10(%rdi,%rax,4)` = 「地址 `rdi + rax×4 + 16`」。这条寻址方式**是为编译器设计的**：`a[i]` 里 `a` 是基址、`i` 是索引、`sizeof(元素)` 是比例，一条指令既算地址又访存；`base/06` 的「元素大小决定 stride」在这里就是硬编码的 1/2/4/8。

而 `lea` 把这条寻址方式**只用来算数、不访存**——于是它成了编译器最喜欢的计算器：一条 `lea` 能同时做「加偏移」和「乘 1/2/4/8」，不写标志、不占访存端口、延迟 1 周期。§12.5 与 §12.6 会反复见到它。

---

## 12.4 认识栈帧：一段 C 到 21 条指令

一个最小但五脏俱全的函数：局部变量、算术、真实调用、返回值。

```c
int add3(int a, int b, int c);      /* 定义在另一个编译单元 */

int work(int x) {
    int base = 10;
    int y = x + base;
    int z = add3(y, x, 3);
    return z + base;
}
```

`gcc -S -O0 -fno-asynchronous-unwind-tables work.c` 的**完整输出**（21 条指令）：

```asm
	.file	"work.c"
	.text
	.globl	work
	.type	work, @function
work:
	pushq	%rbp
	movq	%rsp, %rbp
	subq	$32, %rsp
	movl	%edi, -20(%rbp)
	movl	$10, -4(%rbp)
	movl	-20(%rbp), %edx
	movl	-4(%rbp), %eax
	addl	%edx, %eax
	movl	%eax, -8(%rbp)
	movl	-20(%rbp), %ecx
	movl	-8(%rbp), %eax
	movl	$3, %edx
	movl	%ecx, %esi
	movl	%eax, %edi
	call	add3@PLT
	movl	%eax, -12(%rbp)
	movl	-12(%rbp), %edx
	movl	-4(%rbp), %eax
	addl	%edx, %eax
	leave
	ret
	.size	work, .-work
	.ident	"GCC: (Debian 12.2.0-14+deb12u1) 12.2.0"
	.section	.note.GNU-stack,"",@progbits
```

逐行注释（同一条不差，只是加了话）：

```asm
work:
	pushq	%rbp                  # ① 保存调用者的帧基址（rsp -= 8，返回地址之下）
	movq	%rsp, %rbp            # ② 本帧的基准点：之后局部变量全部写成 -N(%rbp)
	subq	$32, %rsp             # ③ 开 32 字节栈空间（只需 20 字节，但要 16 字节对齐）
	movl	%edi, -20(%rbp)       # ④ 第 1 个参数 x 在 edi 里 → 先倒到栈上（-O0 不做寄存器分配）
	movl	$10, -4(%rbp)         # ⑤ int base = 10        → 偏移 -4
	movl	-20(%rbp), %edx       #    x
	movl	-4(%rbp), %eax        #    base
	addl	%edx, %eax            #    x + base（结果在 eax）
	movl	%eax, -8(%rbp)        # ⑥ int y = x + base     → 偏移 -8
	movl	-20(%rbp), %ecx       #    第 2 个实参：x
	movl	-8(%rbp), %eax        #    第 1 个实参：y
	movl	$3, %edx              #    第 3 个实参：3
	movl	%ecx, %esi            #    x → esi
	movl	%eax, %edi            #    y → edi（edx 已是 3）
	call	add3@PLT              # ⑦ 压返回地址并跳转；返回值约定在 eax
	movl	%eax, -12(%rbp)       # ⑧ int z = 返回值       → 偏移 -12
	movl	-12(%rbp), %edx       #    z
	movl	-4(%rbp), %eax        #    base
	addl	%edx, %eax            # ⑨ 返回值放回 eax
	leave                         # ⑩ = mov %rbp,%rsp; pop %rbp（撤帧）
	ret                           # ⑪ 弹出返回地址并跳回调用者
```

### 栈帧的形状

`-O0` 下的栈是一张完全静态的图。`rbp` 钉在中间，正方向是调用者留下的东西，负方向是本函数的局部变量：

```
        高地址
        ┌──────────────────────────┐
        │ 调用者的栈帧              │
        ├──────────────────────────┤
 rbp+8  │ 返回地址（call 压入）     │
 rbp+0  │ 旧的 rbp（push %rbp 保存）│
        ├──────────────────────────┤
 rbp-4  │ base = 10                │  ← 局部变量
 rbp-8  │ y                        │
 rbp-12 │ z                        │
 rbp-20 │ x（参数的栈副本）         │  ← 注意 -16 是空的
 rbp-24 │ （未使用）                │
 rbp-32 │ （对齐填充）              │
        ├──────────────────────────┤
        │ …                        │  ← rsp 指向这里
        低地址
```

三个要自己推的细节：

**① 为什么是 `sub $32` 而不是 `sub $20`？** 因为 ABI 要求「执行 `call` 的那一刻 `rsp` 是 16 的倍数」。进函数时 `rsp ≡ 8 (mod 16)`（`call` 刚压了返回地址），`push %rbp` 后 `≡ 0`，再减 20 就变成 `≡ 12`——下一次 `call` 会踩到未对齐的栈，SSE 的 `movaps` 直接段错误。减 32 既够用又对齐，**`-O0` 汇编里所有「奇怪的圆整」都是这条约束**。

**② 为什么偏移是 `-4 / -8 / -12 / -20`？** 编译器把 4 字节对齐的 `int` 从 `-4` 起向下排，`x` 落在 `-20`，`-16` 空着。**偏移是实现细节，不是 ABI**：同一个 `work` 加个 `-g`、换个编译单元，GCC 12.2 就给 `subq $24` 和 `-0x14`（§12.8 实测）。所以「偏移算错就崩」与「换编译器 exploit 失效」是一回事。

**③ `leave` ≡ `mov %rbp,%rsp; pop %rbp`。** `-O2` 下没有 `rbp`，就用 `add $N,%rsp` 或 `pop` 收尾。

### 从栈帧读出的五件事

| C 里的概念 | 汇编里的落点 | 本段的证据 |
| --- | --- | --- |
| 参数 | 前 6 个在 `rdi/rsi/rdx/rcx/r8/r9`，第 7 个起在栈上 | `movl %edi,-20(%rbp)` |
| 局部变量 | `rbp` 减一个常量偏移 | `-4` `-8` `-12` |
| 算术表达式 | ALU 指令 + 中间结果在寄存器 | `addl %edx,%eax` |
| 函数调用 | 参数就位 + `call`（压返回地址）+ 返回值在 `eax` | `call add3@PLT` → `movl %eax,-12(%rbp)` |
| 返回 | 结果放 `eax`，然后 `leave; ret` | 倒数三行 |

**这就是「函数调用在机器码里长什么样」的全部答案**：没有「调用」这个抽象动作，只有「把三个值放进三个约定好的寄存器、把下一条指令的地址压到栈上、跳过去、回来时从 `eax` 取一个值」。

---

## 12.5 同一个函数 `-O0` vs `-O2`：语言性能幻觉的来源

同一个 `work`，`gcc -S -O2` 的全部输出是：

```asm
work:
	leal	23(%rdi,%rdi), %eax
	ret
```

**21 条指令变成 2 条。** 两条都不是「优化技巧」，是代数：`z + base = (y + x + 3) + base = (x + 10 + x + 3) + 10 = 2x + 23`，而 `leal 23(%rdi,%rdi)` 正好是「`rdi + rdi×1 + 23`」。`call` 没了（`add3` 被内联），栈没了（不需要），`base` 和 `y` 和 `z` 都没了（它们只存在于源码里）。

把源码概念在两列里的落点并排放：

| 源码概念 | `-O0` 的落点 | `-O2` 的落点 |
| --- | --- | --- |
| 参数 `x` | `edi` → 栈 `-20(%rbp)` | 一直留在 `rdi`，从未落栈 |
| 局部变量 `base` | 栈 `-4(%rbp)`，每次用都重新 load | 被常量折叠进 `23` |
| 局部变量 `y` | 栈 `-8(%rbp)` | 被代数消掉 |
| 局部变量 `z` | 栈 `-12(%rbp)` | 被代数消掉 |
| 函数 `add3` | 一次 `call` + 栈帧 | 内联成一条 `addl`，再被折叠 |
| 返回值 | `eax` | `eax`（唯一没变的东西） |
| 栈帧 | `push/mov/sub/leave` | 完全没有 |

再往大一点看，三种「循环的消失方式」在同一份源码上同时发生。源码：

```c
void empty_loop(int n) { for (int i = 0; i < n; i++) { } }
int  sum(int *a, int n) { int s = 0; for (int i = 0; i < n; i++) s += a[i]; return s; }
void fill(int *a, int n) { for (int i = 0; i < n; i++) a[i] = i * 3; }
```

**① 被删除**：`empty_loop` 在 `-O2` 下只剩 `ret`（§12.1 那 0.5 秒就是这么没的）。

**② 被向量化**：`sum` 在 `-O3` 下的内层循环是 4 个 `int` 一起加：

```asm
.L6:
	movdqu	(%rax), %xmm2        # 一次读 16 字节 = 4 个 int
	addq	$16, %rax
	paddd	%xmm2, %xmm0         # 4 路同时累加
	cmpq	%rax, %rdx
	jne	.L6
```

而 `-O0` 下同一个循环每次迭代都要重新算一遍地址、把累加器写回内存：

```asm
.L6:
	movl	-8(%rbp), %eax       # i
	cltq
	leaq	0(,%rax,4), %rdx     # i*4
	movq	-24(%rbp), %rax      # a
	addq	%rdx, %rax           # a + i*4
	movl	(%rax), %eax         # a[i]
	addl	%eax, -4(%rbp)       # s += a[i]（每次都写回栈）
	addl	$1, -8(%rbp)         # i++（每次都写回栈）
```

`-O2` 把上面 8 条压成 4 条、把 `i` 换成指针游走：

```asm
.L5:
	addl	(%rdi), %eax
	addq	$4, %rdi
	cmpq	%rdx, %rdi
	jne	.L5
```

**③ 被展开成常量**：`fill4`（固定 4 次）在 `-O2` 下连循环都没有，四次赋值变成「把 4 个常量装进一个 128 位寄存器，再一次写出去」：

```asm
fill4:
	movdqa	.LC0(%rip), %xmm0
	movups	%xmm0, (%rdi)
	ret
	.size	fill4, .-fill4
	.section	.rodata.cst16,"aM",@progbits,16
	.align 16
.LC0:
	.long	0
	.long	3
	.long	6
	.long	9
```

### 同一份源码，三个数字

`bench_sum.c`：16 Mi 个 `int`（64 MiB），取 5 次里最快的一次：

| 构建 | 最快耗时 | 相对 `-O0` | 汇编特征 |
| --- | --- | --- | --- |
| `-O0` | 44,209,787 ns（44.2 ms） | 1.0× | 每轮 8 条指令全走栈 |
| `-O2` | 4,644,041 ns（4.64 ms） | **9.5×** | 4 条指令的指针循环 |
| `-O3` | 4,528,124 ns（4.53 ms） | **9.8×** | SIMD 4 路并行 |

两个结论都能自己算：

**① 9.5 倍不是「C 比 C 快」，是「同一份 C 的两个产物差 9.5 倍」。** 源码一字未改：`-O0` 每轮都把 `i` 与 `s` 写回栈（store-to-load 有延迟），`-O2` 把它们留在寄存器里。差距全部来自「数据放在哪」。

**② `-O3` 只比 `-O2` 快 2.5%，因为两者撞在同一堵墙上。** 64 MiB ÷ 4.53 ms ≈ **14.8 GB/s**，一条单通道 DDR4 的量级（量级结论）。SIMD 让 ALU 闲了下来，但 64 MiB 还得从内存搬一遍，**带宽成了唯一上限**——`base/07` 与 `base/20` 会反复用到这条。

所以「语言性能」是幻觉，准确说法是：**一份编译产物，在给定输入上的指令条数 × 访存模式 × 分支可预测性。** 换语言只是换默认的产物形态；换 `-O`、换布局、换规模，任何一个都能推翻结论。有人说「C 比 Java 快 3 倍」，先反问：哪个 `-O`？多大的数据？热了没有？

---

## 12.6 从汇编反推 C：四个练习

下面四段都是 `gcc -O2` 的真实输出。先遮住答案，只看指令猜源码。

### 练习① `lea` 推数组下标

```asm
idx:
	leaq	(%rdi,%rdi,4), %rax      # rax = i + i*4 = i*5
	leaq	16(,%rax,8), %rax        # rax = rax*8 + 16 = i*40 + 16
	ret
```

<details><summary>答案</summary>

```c
long idx(long i) { return i * 40 + 16; }
```

关键在**比例只能是 1/2/4/8**：`i*40` 一条 `lea` 做不完，于是拆成 `i*5` 再 `*8`。若是 `p + i` 这样的指针算术，`40` 会以「结构体大小」出现在系数位置——`leaq 16(%rdi,%rsi,8)` 里的 `8` 就是元素大小。
</details>

### 练习② `cmov` 推三元表达式

```asm
pick:
	cmpl	%edi, %esi              # 算 esi - edi，即 b - a
	movl	%edi, %eax              # 先把 a 放进结果
	cmovge	%esi, %eax              # 若 b >= a（有符号），结果换成 b
	ret
```

<details><summary>答案</summary>

```c
int pick(int a, int b) { return a > b ? a : b; }
```

`cmovge` 是「条件传送」：不跳转，只按标志位决定要不要覆盖目标寄存器。它出现的唯一理由是**分支可能预测错**——一次失败要清空流水线（`base/10` 量到十几到二十个周期），而 `cmov` 恒定 1 周期。

但它**不是免费的**：两个操作数都得先算出来。这里 `a`、`b` 已在寄存器里无所谓；如果一边是昂贵表达式，编译器就不会用 `cmov`——这就是「无分支」经常反而更慢的原因。
</details>

### 练习③ `call` 推函数调用

```asm
0000000000001150 <caller>:
    1150:	sub    $0x8,%rsp          # 对齐：进来时 rsp≡8，减 8 后 ≡0
    1154:	mov    $0x3,%edx          # 第 3 个参数
    1159:	mov    $0x2,%esi          # 第 2 个参数
    115e:	call   1170 <add3>        # 第 1 个参数原样留在 edi 里
    1163:	add    $0x8,%rsp
    1167:	add    $0x1,%eax          # 返回值 + 1
    116a:	ret
```

<details><summary>答案</summary>

```c
int add3(int a, int b, int c);
int caller(int v) { return add3(v, 2, 3) + 1; }
```

三处值得注意：

- **第 1 个参数没有任何搬运**：`v` 进来在 `edi`，`add3` 的第 1 个参数也在 `edi`，「传参」退化成「什么都不做」。看到「少一个参数」，先怀疑它是被原样透传的。
- **`sub $8,%rsp` 纯粹为了对齐**，一个局部变量都没有也要动 `rsp`。
- **返回后还有 `addl $1,%eax`**，所以不是尾调用。若源码改成 `return add3(v, 2, 3);`，GCC 会生成 `jmp add3@PLT`——**栈帧少一层，backtrace 就少一帧**（§12.10）。

顺便：`call 1170 <add3>` 是链接后的最终地址；在 `.o` 里它是 `call 13 <caller+0x13>`，目标地址还没填，留了重定位项等链接器补（`base/22`）。
</details>

### 练习④ `mov` + `add` 推结构体字段

```asm
sget:
	movslq	16(%rdi), %rax          # 读 4 字节、符号扩展到 64 位
	addq	8(%rdi), %rax           # 读 8 字节、直接相加
	ret
```

<details><summary>答案</summary>

```c
struct S { int a; long b; int c; };
long sget(struct S *s) { return s->b + s->c; }
```

`%rdi` 是 `s`，两个偏移 `8` 和 `16` 就是字段偏移。实测这台机器上：

```
sizeof(struct S) = 24
offsetof a=0 b=8 c=16
```

`b` 是 `long`（8 字节对齐），所以 `a` 后面空了 4 字节 padding；`c` 在 16，结构体总大小 24（尾部再补 4 字节，保证数组里每个元素都对齐）。

三件事顺势可推：

- `movslq 16(%rdi)` 中的 `slq` = sign-extend long，因为 `c` 是 `int`、返回值是 `long`：**字段类型决定加载指令的宽度与扩宽方式**。
- 如果把声明改成 `struct T { int a; int c; long b; };`，实测变成 `sizeof = 16, a=0, c=4, b=8`——**字段顺序直接决定大小**，24 → 16，省 33%。这就是「调整结构体字段顺序」这条优化建议的全部原理，一行汇编就能验。
- 若看到 `mov 8(%rdi),%rax; add 16(%rdi),%rax`（没有 `sl`），`c` 就是 `long` 而非 `int`。
</details>

---

## 12.7 系统调用：`syscall` 指令与「库函数 ≠ 系统调用」

### 一条指令越过特权级

`syscall` 是 x86-64 上进入内核的主入口（`int $0x80` 只服务 32 位 ABI；`base/21` 讲内核在里面做什么）。约定很紧凑：

| 项目 | 约定 |
| --- | --- |
| 调用号放哪 | `rax` |
| 参数放哪 | `rdi`、`rsi`、`rdx`、`r10`、`r8`、`r9`（最多 6 个） |
| 返回值放哪 | `rax`；失败时是 `-errno`（`-4095 … -1`） |
| 被硬件破坏的寄存器 | `rcx`（存返回的 `rip`）、`r11`（存 `rflags`） |

第 4 个参数用 `r10` 而不是 `rcx`，原因就在最后一行：`syscall` 指令自己要拿 `rcx` 存返回地址，拿 `r11` 存标志位，所以 ABI 必须绕开它们。**这不是历史包袱，是「这条指令需要两个暂存寄存器」的直接推论。**

调用号写在内核头文件里，这台机器上：

```bash
$ grep -E '__NR_(read|write|exit|openat|mmap|futex) ' /usr/include/x86_64-linux-gnu/asm/unistd_64.h
#define __NR_read 0
#define __NR_write 1
#define __NR_mmap 9
#define __NR_exit 60
#define __NR_futex 202
#define __NR_openat 257
```

### 写一个不用 libc 的 `write(1,"hi\n",3)`

```c
/* 不链接 libc：只用一条 syscall 指令把 3 个字节写到 fd 1 */
static long sys_write(int fd, const void *buf, long n) {
    long ret;
    __asm__ volatile ("syscall"
                      : "=a"(ret)
                      : "a"(1L), "D"(fd), "S"(buf), "d"(n)
                      : "rcx", "r11", "memory");
    return ret;
}
static void sys_exit(int code) {
    __asm__ volatile ("syscall" :: "a"(60L), "D"(code) : "rcx", "r11", "memory");
    __builtin_unreachable();
}
void _start(void) {
    sys_write(1, "hi\n", 3);      /* 没有 printf、没有 stdio、没有 libc */
    sys_exit(0);
}
```

```bash
$ gcc -nostdlib -static -O2 -fno-stack-protector nosyscall.c -o nosyscall
$ ./nosyscall; echo "exit=$?"
hi
exit=0
```

它真的打印了 `hi`，而整个可执行文件里没有一行 libc 代码。`_start` 只有四个 `mov`、一个 `lea`、一个 `xor`、两个 `syscall`：

```asm
0000000000401000 <_start>:
  401000:	b8 01 00 00 00       	mov    $0x1,%eax        # write 的调用号 = 1
  401005:	bf 01 00 00 00       	mov    $0x1,%edi        # fd = 1（stdout）
  40100a:	ba 03 00 00 00       	mov    $0x3,%edx        # 长度 = 3
  40100f:	48 8d 35 ea 0f 00 00 	lea    0xfea(%rip),%rsi # 缓冲区地址（.rodata 里的 "hi\n"）
  401016:	0f 05                	syscall
  401018:	b8 3c 00 00 00       	mov    $0x3c,%eax       # exit 的调用号 = 60
  40101d:	31 ff                	xor    %edi,%edi        # 状态码 = 0
  40101f:	0f 05                	syscall
```

`lea 0xfea(%rip),%rsi` 是**指令指针相对寻址**：地址 = 下一条指令的 `rip` + `0xfea`（`0x401016 + 0xfea = 0x402000`）。为什么不用绝对地址？因为 PIE/ASLR 会让绝对地址在每次运行时都不同（`base/22`、`base/33`）；`rip` 相对则与装载位置无关。

### 「库函数 ≠ 系统调用」的三个硬证据

**证据一：体积。**

```bash
$ ls -l hello_libc nosyscall hello_libc_static
-rwxr-xr-x 1 root root  15968 hello_libc          # 动态链接 libc 的 printf("hi\n")
-rwxr-xr-x 1 root root   9008 nosyscall           # 手写 syscall，不链接任何库
-rwxr-xr-x 1 root root 762432 hello_libc_static   # 静态链接 libc
```

同样打两个字符：静态链接 762 KB，手写 9 KB，**差 84 倍**。那 750 KB 是 stdio、locale、malloc 与 libc 启动代码。

**证据二：`main` 里根本没有 `write`。**

```asm
0000000000001050 <main>:
    1050:	sub    rsp,0x8
    1054:	lea    rdi,[rip+0xfa9]        # 2004 <_IO_stdin_used+0x4>
    105b:	call   1030 <puts@plt>
    1060:	xor    eax,eax
    1062:	add    rsp,0x8
    1066:	ret
```

源码写 `printf`，编出来是 `puts`（格式串没有占位符，换成更便宜的），而且是 `call ...@plt`——`puts` 的真实地址都还没定。`write` 一次都没出现。

**证据三：libc 是被装载进来的，不是长在二进制里的。**

```bash
$ LD_DEBUG=libs ./hello_libc 2>&1 | head -4
   2615844:	find library=libc.so.6 [0]; searching
   2615844:	 search cache=/etc/ld.so.cache
   2615844:	  trying file=/lib/x86_64-linux-gnu/libc.so.6
```

那 3 个字节什么时候进内核？`exit` 的时候：`printf` 先写进**用户态 FILE 缓冲区**，`main` 返回后 libc 清理（`exit` → `fflush`）才真正调用一次 `write(1, "hi\n", 3)`。于是有两个工程后果：崩溃时日志丢（缓冲区没 flush）、重定向到文件后行为变（终端行缓冲、文件全缓冲，`base/28` 展开）。

一句话收束：**`write()` 是 libc 的一个函数，`syscall` 是一条指令，两者之间隔着缓冲区、errno 翻译与一次两态切换。** 一次 `syscall` 的固定成本是百纳秒量级，普通函数调用是纳秒量级——主线③「单价 = 跨界次数 × 单次成本」在 CPU 层最朴素的一次落账（`base/21`、`base/36`）。

---

## 12.8 实测：把工具链跑一遍

档案：`x86_64`、gcc 12.2.0（Debian 12.2.0-14+deb12u1）、binutils 2.40。以下输出全部来自本机。

三把工具：`gcc -S` 出汇编（`-masm=intel` 切语法）、`objdump -d` 出反汇编（`-M intel` 切语法、`-S` 交错源码）、`objdump --dwarf=decodedline` 出行号表。

### `objdump -d -S`：行号与指令交错，但 `-O2` 下会骗你

这段来自把 `work`、`add3`、`main` 放进同一个 `demo.c` 加 `-g` 编出的 `demo`。注意偏移是 `-0x14`、`sub $0x18`，与 §12.4 的 `-20`/`$32` 不同（原因见 §12.4 ②）。`-O0` 下交错是诚实的：函数头下面就是建栈四条，`int base = 10;` 下面正好是那条 `movl`（完整 21 条见 §12.4）：

```asm
0000000000001155 <work>:
int work(int x) {
    1155:	push   rbp
    1156:	mov    rbp,rsp
    1159:	sub    rsp,0x18
    115d:	mov    DWORD PTR [rbp-0x14],edi
    int base = 10;
    1160:	mov    DWORD PTR [rbp-0x4],0xa
```

`-O2` 下同一个命令，`work` 只剩这么多：

```asm
0000000000001170 <work>:
    return z + base;
    1170:	lea    eax,[rdi+rdi*1+0x17]
}
    1174:	ret
```

五行源码被标成了**一行**：`base`、`y`、`z` 在行号表里已没有独立地址。这就是「`-O2` 下断点跳来跳去」的全部原因——你在一行源码上设的断点，可能对应 0 个地址（被删）、1 个（被合并）或 n 个不连续的地址（被展开）。

### `--dwarf=decodedline`：把「源码行 → 地址」的映射直接打出来

```bash
$ objdump --dwarf=decodedline looptrace
looptrace.c:
File name                            Line number    Starting address    View    Stmt
looptrace.c                                    2              0x1190               x
looptrace.c                                    3              0x1190       1       x
looptrace.c                                    4              0x1190       2       x
looptrace.c                                    4              0x1190       3       x
looptrace.c                                    4              0x1190       4       x
looptrace.c                                    4              0x1197
looptrace.c                                    3              0x1197       1
looptrace.c                                    4              0x11a0               x
looptrace.c                                    4              0x11a0       1
looptrace.c                                    4              0x11a2               x
looptrace.c                                    4              0x11a2       1       x
looptrace.c                                    4              0x11a6
looptrace.c                                    4              0x11b0
looptrace.c                                    3              0x11b0       1
looptrace.c                                    5              0x11b2               x
looptrace.c                                    6              0x11b2       1
looptrace.c                                    -               0x11b3
```

三处必须看懂：

- **`0x1190` 同时对应第 2、3、4 行**（`View` 列区分同一地址上的多个条目）——三行被合并进一条指令。
- **第 3 行在 `0x1197` 与 `0x11b0` 各出现一次**，源码里却只有一处——行号在地址轴上**不单调**，循环重排后源码顺序与地址顺序无关。
- **`-` 那一行 `0x11b3`** 是「没有对应源码」的编译器收尾代码（`nop`、填充、异常表清理）。

（这里 `sum` 只做了指针游走；`-O3` 下还会有「一个源码行对应一整段 SIMD 块」。）

---

## 12.9 三列账：汇编承诺了什么、收了多少、从哪开始失效

| 维度 | 承诺了什么 | 为此付了什么 | 从哪里开始失效 |
| --- | --- | --- | --- |
| **指令语义** | 每条指令的行为在架构手册里写死，可逐条推演 | 指令集庞大、向后兼容包袱重；x86 变长指令难解码 | 一旦跨出架构（ARM64、RISC-V），指令名与寄存器名全换（`base/11`） |
| **地址与偏移** | 语言里的变量/字段/下标都能落到一个确定的偏移上 | 偏移是编译器实现细节，换版本/换优化级别就变 | 依赖「栈上偏移」的代码（内联汇编、手写 exploit、序列化内存布局）在换编译器后失效 |
| **栈帧可读** | 有 `rbp` 链就能画出调用栈，`backtrace` 就能工作 | `-O2` 常省掉 `rbp`（frame pointer omission），只能靠 `.eh_frame` 展开 | 尾调用（`jmp`）、内联、`-fomit-frame-pointer` 都会让「栈上帧数」少于「源码调用层数」 |
| **优化不改语义** | 优化必须保持可观察行为（as-if 规则） | 「可观察行为」只覆盖 C 标准定义的行为；UB 不在其中 | 有符号溢出、空指针解引用、越界访问、别名违规 → 编译器可以生成任意代码 |
| **验伪能力** | 任何「性能」断言都能落到指令条数与访存次数上 | 需要会读、会对照源码行、会区分语法 | 读错语法（AT&T 与 Intel 反了）会把结论读反，而且自信满满 |

三条最贵的代价，各配一个本篇真实输出：

**① UB 被优化成任意行为。** `int overflow_check(int x) { return x + 1 < x; }` 在 `-O2` 下：

```asm
overflow_check:
	xorl	%eax, %eax        # return 0，永远
	ret
```

编译器假设「有符号加法不会溢出」，于是 `x+1 < x` 恒假。这不是 bug，是标准授权的推导（`base/03`、`base/04`）。**汇编里是「一个永远返回 0 的函数」，源码里却是一个溢出检测。**

**② 内联改变栈帧假设。** `work` 在 `-O2` 下只剩一条 `lea`。若有代码依赖「调用 `work` 一定出现一帧栈」——`backtrace()` 数层数、用栈地址做哈希、用 `alloca` 算剩余栈——它在 `-O2` 下就失效了。

**③ 优化改变「语义边界」的直觉。** 2 亿次空循环，`-O0` 是 0.5 秒，`-O2` 是 1 微秒，两者都符合标准（前者「恰好没删」，后者「有权删」）。**所以「代码做了什么」在 C 里没有唯一答案，只有「在这份产物里做了什么」。**

一句话收束：**汇编买到「可验证」，代价是「可验证的对象是编译产物，不是源码」**——这正是 `base/01` 那把尺子的最后一级证据。

---

## 12.10 踩坑清单：现象 → 根因 → 定位 → 修 → 防复发

| 现象 | 根因（指回本篇） | 定位 | 修 | 防复发 |
| --- | --- | --- | --- | --- |
| `-O2` 下断点乱跳、单步时行号来回跳 | 行号表多对一、乱序、甚至为空（§12.8 的 `decodedline`） | `objdump --dwarf=decodedline` 看某一行的地址集合；`objdump -d -S` 看交错 | 在 `-O0`/`-Og` 下定位逻辑，在 `-O2` 下复现性能问题；断点改设在指令地址上 | 调试构建固定 `-Og -g`；发布构建保留 `.debug_*` 单独存放 |
| 加了 `printf` 之后 bug 不复现 | `printf` 是真实函数调用，阻止内联、逼变量落栈、改变时序（§12.7 的缓冲区） | `gcc -S` 对比加日志前后的汇编，看多出来的 `call` 与栈写 | 改用 `__builtin_trap()`、断言或环形缓冲日志（不在热路径 `write`） | 调试输出走可关闭的编译期开关；性能问题用 `perf`/`bpftrace` 而非 `printf` |
| `volatile` 加了还是错，或者加了就慢 3 倍 | `volatile` 只保证「每次都真的读写内存」，**不保证原子、不保证顺序**（§12.1 的 `vol` 列） | `gcc -S` 看每轮是否都有 load/store；`objdump` 确认没有 `lock` 前缀 | 计数/标志用 `atomic_*`；只有 MMIO、信号处理、`setjmp` 才用 `volatile` | Code review 规则：`volatile` 只允许出现在驱动与信号代码里 |
| 性能归因到一行「什么都没做」的代码 | 内联后 callee 的指令被记在 caller 的行号上（§12.5 的 `work`） | `objdump -d -S` 看那行展开成多少指令；`-fno-inline` 编译对照 | 用 `-fno-omit-frame-pointer -g` 重采；或对热点函数加 `noinline` 做 A/B | 性能报告必须附「函数 → 指令数」而非只有「源码行 → 百分比」 |
| 基准测试快得离谱（微秒级） | 结果没被使用，整段被 DCE 删掉（§12.1） | `gcc -S -O2` 看目标函数是否只剩 `ret` | 把结果写到 `volatile` 全局或作为返回值打印；用 `asm volatile("" ::: "memory")` 制造屏障 | 基准框架里内置「结果校验 + 防 DCE」的写法（如 Google Benchmark 的 `DoNotOptimize`） |

三句话：**行号不对，先怀疑优化合并或删了源码行；加日志就好，先怀疑日志改变了代码生成；基准太快，先怀疑那段代码没被生成。**

---

## 12.11 一页速记

```
【一句话】汇编里没有「变量」和「函数」，只有寄存器、内存偏移、和一个压着返回地址的栈。

【20 条指令的四类】
  搬运：mov  lea（lea 只算地址不访存，不改标志）
  运算：add sub imul and or xor shl shr
  比较跳转：cmp test  +  jmp je jne jl jg（有符号 jl/jg，无符号 jb/ja）
  调用：call（压返回地址）ret（弹回）push/pop（动 rsp）

【操作数三种】$立即数   %寄存器   disp(base,index,scale)
  0x10(%rdi,%rax,4) = rdi + rax*4 + 16     ← 数组下标、结构体字段都长这样
  AT&T：源在前（mov %rsi,%rdi）；Intel：目在前（mov rdi,rsi）；切换 objdump -M intel

【寄存器】rax 返回值 / rdi rsi rdx rcx r8 r9 参数 / rsp 栈顶 / rbp 帧基址
  写 eax 会把高 32 位清零 → movl 是无符号扩宽，movslq 是有符号扩宽

【栈帧三件套】
  push %rbp; mov %rsp,%rbp; sub $N,%rsp      ← N 要满足 16 字节对齐
  局部变量 = -4(%rbp)、-8(%rbp)…（偏移是实现细节，会变）
  leave = mov %rbp,%rsp; pop %rbp

【四个数字（本机实测）】
  work：-O0 = 21 条指令，-O2 = 2 条（leal 23(%rdi,%rdi),%eax; ret）
  sum(16Mi int)：-O0 44.2 ms，-O2 4.64 ms（9.5×），-O3 4.53 ms（撞 14.8 GB/s 内存墙）
  empty(2e8)：-O0 0.50 s，-O2 0.000001 s（循环被删）
  syscall write = 1，exit = 60；libc 版 762 KB，手写版 9 KB（差 84 倍）

【验伪口诀】
  性能断言 → 先看 -O 级别 → 再看指令条数 → 再看每轮访存次数 → 最后才谈语言
```

---

## 12.12 自检 3 题

**Q1.** 这段 `-O2` 输出对应的 C 是什么？写出完整表达式，并说明为什么 `-O0` 下是 21 条指令。

```asm
work:
	leal	23(%rdi,%rdi), %eax
	ret
```

<details><summary>答案</summary>

对应的就是 §12.4 的 `work`：

```c
int work(int x) {
    int base = 10;
    int y = x + base;
    int z = add3(y, x, 3);
    return z + base;
}
```

代数化简：`z + base = (y + x + 3) + base = (x + 10 + x + 3) + 10 = 2x + 23`。`leal 23(%rdi,%rdi)` 就是 `rdi + rdi×1 + 23`，一条指令既做了乘 2 又做了加 23，且不访存、不改标志。

`-O0` 是 21 条，因为它**不做寄存器分配、常量折叠、内联**：每个局部变量占一个栈槽，每次使用都 load、算完都 store；`add3` 是真实 `call`，还要 `push/mov/sub/leave` 维护栈帧。两个产物的差别不是「技巧」，而是「源码里的名字在产物里还存不存在」。
</details>

**Q2.** 一段汇编是 `movslq 16(%rdi),%rax; addq 8(%rdi),%rax`。请写出结构体定义与函数，并解释：如果你把 `int c` 改成 `long c`，这两条指令会怎么变？结构体大小会怎么变？

<details><summary>答案</summary>

```c
struct S { int a; long b; int c; };
long sget(struct S *s) { return s->b + s->c; }
```

`b` 在偏移 8（`long`，8 字节对齐，所以 `a` 后面有 4 字节 padding），`c` 在偏移 16。`movslq` 是因为 `c` 是 32 位 `int`、返回值是 64 位 `long`：**读 4 字节 + 符号扩展到 64 位**。

改成 `long c` 之后：`c` 本来就在 16（`b` 占满 8..15），偏移不变，只是 `movslq` 变成 `mov 16(%rdi),%rax`（宽度 8、无需扩宽）；大小仍是 24（`c` 恰好填满尾部，不再需要尾部 padding）。实测 `struct U{int,long,long}` 是 `sizeof=24 a=0 b=8 c=16`——**改类型不一定改大小**，要看 padding 落在哪。
</details>

**Q3.** ① 为什么 `syscall` 的第 4 个参数放在 `r10` 而不是 `rcx`？② 为什么 `printf("hi\n")` 在 `main` 的汇编里看不到 `write`，程序却真的输出了？③ 为什么 `-O2` 下 `empty(2×10⁸)` 只要 1 微秒，而 `-O0` 要 0.5 秒？

<details><summary>答案</summary>

**①** 因为 `syscall` 自己要用 `rcx` 存返回的 `rip`、用 `r11` 存 `rflags`，这两个寄存器被硬件覆盖，所以 ABI 必须绕开 `rcx`，把第 4 个参数挪到 `r10`——「指令需要两个暂存寄存器」的直接推论。

**②** 因为 `printf` 先写进用户态 FILE 缓冲区，真正落到内核的是 `main` 返回后 libc 清理时的一次 `write(1, buf, 3)`。证据：`main` 里是 `call puts@plt`、`LD_DEBUG=libs` 显示 `libc.so.6` 是运行时装载的、静态链接版 762 KB vs 手写版 9 KB。顺带解释了「崩溃丢日志」与「重定向后缓冲行为不同」。

**③** 循环体没有副作用，编译器按 as-if 规则认定「什么都没做」，把它删成一条 `ret`；`-O0` 不删，2 亿圈真实执行 ≈ 0.5 秒。两者都合法——**标准只约束「可观察行为」**。所以基准必须让结果「被观察到」，否则你测的是优化器的想象力。
</details>

---

## 12.13 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 指令集与调用约定：callee-saved、第 7 个参数、x86 与 ARM64 的差异 | → `base/11` |
| 流水线、分支预测、`cmov` 为什么省一次预测失败 | → `base/10` |
| 缓存与一致性：`addl (%rdi),%eax` 这条访存付了多少钱 | → `base/13` |
| ELF、装载、`PLT/GOT`：`call ...@plt` 的地址怎么被填上 | → `base/22` |
| `malloc`、brk/mmap 与栈/堆边界 | → `base/27` |
| Go 编译流水线：从源码到机器码，中间的 SSA 与内联决策 | [go/27](../go/27-compile-pipeline.md) |
| `pprof` 与 trace：为什么归因必须配合 `-g` 与行号表 | [go/29](../go/29-pprof-trace.md) |
| 全书总纲：四条主线与 56 篇清单 | [0-summary](0-summary.md) |
