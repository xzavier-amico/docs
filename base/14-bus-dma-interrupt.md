# 14. 总线、DMA、中断与 MMIO：数据怎么在设备与内存之间搬

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · CPU 与 GPU：算力的物理形状 ｜ **编号**：14 / 56
> **本篇定位**：Part II 的第五篇。10–13 讲的是 CPU 内部（流水线、指令、缓存一致性），本篇把 CPU 接到外面：**一条 load/store 怎么碰到一块网卡、一块盘、一张 GPU**。全篇只有一条推论链——`CPU 只有 load 与 store 两种访存动作` → `所以设备必须被映射成地址` → `每次跨界都是一次总线往返` → `所以最贵的不是算力，是 CPU 碰数据的次数` → `DMA、批处理、零拷贝都是在砍这个次数`。`base/15` 接着讲特权级与虚拟化，`base/16`–`base/18` 讲 GPU，`base/32` 把本篇的收包账展开成六道门，`base/37` 把零拷贝拆成四种实现。
> **读法**：§14.1 的数字账是全篇的靶子，建议按自己机器的主频重算一遍；§14.2 的 PCIe 表只需记住「每 lane 每代翻倍」与「GT/s 不是 GB/s」；§14.4 的 1 MB 两本账是全篇支点；§14.5–§14.6 是排障最常用的两张图；赶时间只读 §14.9、§14.11、§14.13。
> **不在本篇**：socket 收发全路径与 NAPI 的完整状态机 → `base/32`；零拷贝四实现的 syscall 对比 → `base/37`；页表与缺页 → `base/26`；ring 与微内核的跨界成本 → `base/15`；GPU 的 SIMT 与显存层级 → `base/16`、`base/17`。

---

## 14.1 引子：40 Gbps 的网卡，为什么只跑出 8 Gbps

先声明：这不是新闻，是一道**可复现的算术题**。把一张 40 Gbps 网卡接进一台 2 核的机器（本环境正好是 2 核 KVM，主机型号 `AMD EPYC 7K62`，`BogoMIPS 5190.24`，即约 2.6 GHz），用最常见的「每包一次中断 + 每包一次拷贝」的老路子收包，实测吞吐停在 **8 Gbps** 左右。运维通常先怀疑「链路没协商上」或「交换机限速」，但 `lspci -vv | grep LnkSta` 明明写着 `Speed 16GT/s, Width x32`——一条 Gen4 x32 的链路，理论单向 63 GB/s，比 40 Gbps（5 GB/s）宽十倍以上。

链路不是瓶颈，算出来一目了然。

| 项 | 数值 | 推导（出生证明） |
| --- | --- | --- |
| 线速 | 40 Gbps = **5 GB/s** | 40 ÷ 8 |
| 线上每帧字节 | 1538 B | 前导 7 + SFD 1 + 以太头 14 + 载荷 1500 + FCS 4 + 帧间隙 12 |
| 包速率 | **3.25 Mpps** | 5×10⁹ ÷ 1538 ≈ 3.25×10⁶ |
| 每包 CPU 成本 | **6250 cycle = 2.4 µs** | 见下表拆分，@2.6 GHz |
| 打满线速需要的核 | **7.8 个** | 3.25×10⁶ × 2.4×10⁻⁶ |
| 2 核能给的吞吐 | **10.3 Gbps** | (2 ÷ 7.8) × 40 |
| 实测 | **8 Gbps** | 有一个核还要跑应用，不能全给收包 |

6250 cycle 不是拍脑袋，它是一张可以在 `perf`（本机没装，§14.8 说明）里逐项对上的拆分表：

| 环节 | 量级（cycle/包） | 为什么这么贵 |
| --- | --- | --- |
| 硬件中断入口/出口（KVM 下含一次 VM exit） | ~2000 | 保存现场、切栈、ACK 中断控制器；虚拟化再叠一层陷入/退出 |
| 软中断调度 + 驱动收包 + 分配 skb | ~1300 | `NET_RX_SOFTIRQ` 被拉起，驱动从 ring 取描述符、建 skb |
| 协议栈（IP 查找、TCP 校验、入队） | ~1400 | 每包都要走一遍哈希查找与状态机 |
| 两次拷贝（ring→skb、skb→用户） | ~800 | 1500 B × 2，按 10 GB/s 拷贝带宽算约 300 ns |
| 唤醒进程 + 调度 | ~750 | 收包线程被唤醒，进 runqueue 再被选上 |

**这张表就是本篇的全部内容。** 40 Gbps 需要每秒处理 325 万个包，每个包不管多大都要交一次「中断 + 协议栈 + 拷贝」的固定税；税是 6250 cycle，乘上 325 万，就是 7.8 个核。你只有 2 个核，于是只能跑出线速的 1/4。

于是所有优化都指向同一个方向：**不是让每个 cycle 更快，而是让每个字节被 CPU 碰的次数更少。**

- 别每个包中断一次 → NAPI、中断合并、多队列（§14.5）；
- 别让 CPU 搬数据 → DMA（§14.4）；
- 别把同一份数据拷两遍 → 零拷贝（§14.6）；
- 别一次只处理一个 → 批处理、scatter-gather、`io_uring`（§14.4、`base/37`）。

记住这个靶子：**CPU 最贵的资源不是算力，是「搬数据的次数」。** 下面每一节都在给这句话补一块物理证据。

---

## 14.2 总线简史与现状：它不是「一根线」，是一个请求-响应网络

「总线」这个词会骗人。它让人想到一根排线，所有设备挂在上面，谁要用谁举手。1990 年代的前端总线（FSB）确实长这样：CPU、北桥、内存控制器共享一组地址/数据/控制线，靠仲裁器决定这一刻谁说话。代价是**同一时刻只能有一个说话者**，且信号要驱动整条线，频率上不去——FSB 停在 200–400 MHz、64 位宽，峰值 3.2–6.4 GB/s。

今天的 PCIe 完全不是这个形状：

```
        CPU
         │  (内存总线 / 片上互连)
   ┌─────┴─────┐
   │  Root     │  ← 根复合体：把 CPU 的访存翻译成 PCIe 事务
   │ Complex   │
   └──┬──┬──┬──┘
      │  │  └──────────────┐
   ┌──┴┐ │              ┌──┴──┐
   │GPU│ │              │ NVMe│       每一条都是点对点、全双工
   └───┘ │              └─────┘       一条 lane = 2 对差分线（收发各一）
      ┌──┴──┐
      │ NIC │
      └─────┘
```

三个根本变化：

1. **点对点**。每条链路只连两个端点，不需要仲裁，不存在「谁抢到总线」这回事。加带宽就加 lane（x1–x16），扩拓扑就加 switch。
2. **串行差分**。一条 lane 用两对差分线，靠 SerDes 在 GHz 级跑，靠编码（8b/10b 或 128b/130b）保证直流平衡与时钟恢复；并行总线「等最慢那根线」的 skew 问题随之消失。
3. **包交换**。地址、数据、控制不再走三组独立的线，而是打包成一个 **TLP**（Transaction Layer Packet）：头部有 Requester ID、Tag、地址、长度，数据段跟在后面，接收方按 Tag 把响应配回请求。**「地址/数据/控制」没有消失，它们变成了头部里的字段。**

这就是为什么说「总线是一个请求-响应网络」：一次 MMIO 读 = 一个 `MemRd` TLP 出去、一个 `CplD` TLP 回来；一次 DMA = 设备发出 `MemWr` TLP，Root Complex 把它转成对 DRAM 的写。**每一次跨界都有一次往返的固定成本**，这个成本在 §14.3 和 §14.4 会反复出现。

### 每 lane 每代的带宽，以及「GT/s 不是 GB/s」

| 代际 | 线速率 | 编码 | 每 lane 有效带宽 | x4 | x16 |
| --- | --- | --- | --- | --- | --- |
| PCIe 1.0 | 2.5 GT/s | 8b/10b（×0.8） | 250 MB/s | 1.0 GB/s | 4.0 GB/s |
| PCIe 2.0 | 5 GT/s | 8b/10b（×0.8） | 500 MB/s | 2.0 GB/s | 8.0 GB/s |
| PCIe 3.0 | 8 GT/s | 128b/130b（×0.985） | ~985 MB/s | 3.9 GB/s | 15.8 GB/s |
| PCIe 4.0 | 16 GT/s | 128b/130b | ~1.97 GB/s | 7.9 GB/s | 31.5 GB/s |
| PCIe 5.0 | 32 GT/s | 128b/130b | ~3.94 GB/s | 15.8 GB/s | 63 GB/s |
| PCIe 6.0 | 64 GT/s | PAM4 + FLIT | ~7.56 GB/s | 30.3 GB/s | 121 GB/s |

两行换算必须自己会做，否则很容易把「16 GT/s」当成「16 GB/s」：

```
每 lane 有效带宽 = 线速率 × 编码效率 ÷ 8
PCIe 4.0：16 GT/s × 128/130 ÷ 8 = 1.969 GB/s   ← 不是 16，也不是 2 的整数倍
```

表里全是**单向**数字。PCIe 是全双工，x16 Gen4 是「每方向 31.5 GB/s、合计 63 GB/s」。

拿它去对 40 Gbps 网卡：40 Gbps = 5 GB/s，x4 Gen3（3.9 GB/s）不够，x8 Gen3（7.9 GB/s）或 x4 Gen4（7.9 GB/s）就够了。**换句话说，一张 40 Gbps 网卡在链路上只需要 8 条 Gen3 lane，剩下的瓶颈全在 CPU 侧。** 这也是为什么「换更宽的插槽」几乎从不解决吞吐问题。

顺带记一个数量级对照：单路服务器 DRAM 带宽约 50–100 GB/s，x16 Gen4 是 31.5 GB/s，x16 Gen5 是 63 GB/s——**当外设链路逼近内存带宽时，DMA 就开始和 CPU 抢同一池 DRAM 带宽**（§14.6 会把这条账算到 40 Gbps 上）。

### 设备怎么被看见：配置空间与 BAR

PCIe 设备上电后，操作系统要回答三个问题：你是谁、你要多少地址空间、你的寄存器在哪。答案是配置空间里的三样东西：

- **BDF**（Bus:Device.Function）：拓扑里的门牌号，如 `0000:06:0a.0`；
- **BAR**（Base Address Register）：设备申请的地址窗口，长度必须是 2 的幂；
- **Capability 链**：MSI/MSI-X、电源管理、PCIe 能力都挂在这条链上。

枚举完成后，内核把每个 BAR 的起始物理地址写回 BAR 寄存器，设备从此就「住进」了物理地址空间。本机的真实布局（`/proc/iomem`，§14.8 有更多）：

```
c0000000-febfffff : PCI Bus 0000:00
  fc000000-fc7fffff : PCI Bus 0000:05
    fc000000-fc1fffff : PCI Bus 0000:09
      fc000000-fc003fff : 0000:09:01.0
        fc000000-fc003fff : virtio-pci-modern
b0000000-bfffffff : PCI ECAM 0000 [bus 00-ff]
```

注意 `virtio-pci-modern` 占了 `fc000000-fc003fff` 共 **16 KB**——现代 virtio 把四组寄存器（common cfg、notify、ISR、device cfg）放在同一个 BAR 的不同偏移上。下一节就讲：内核怎么用一条 `load` 读到这 16 KB 里的某一个字节。

---

## 14.3 MMIO：设备寄存器被映射进物理地址空间，`readl/writel` 就是一次 load/store

x86-64 的物理地址空间不是「内存」。它是**内存 + 一大堆设备的窗口**拼出来的一张地图：

```
0x00000000 ┌──────────────────┐
           │ 低端 RAM / 保留区  │
0x00100000 ├──────────────────┤
           │ System RAM       │  ← 本机 0x00100000–0x7ffd2fff，约 2 GB
0x80000000 ├──────────────────┤
           │ PCIe 窗口         │  ← BAR 落在这里
0xc0000000 ├──────────────────┤
           │ 更多 PCIe 窗口     │
0xfec00000 ├──────────────────┤
           │ IOAPIC 0（1 KB）  │  ← 中断控制器也被映射成地址
0xfee00000 ├──────────────────┤
           │ Local APIC（每核） │
0xffffffff └──────────────────┘
```

所谓 **MMIO**（Memory-Mapped I/O），就是把这台设备的寄存器**当成内存的一个地址**。CPU 不需要新指令：一条普通的 `mov` 就能读它、写它。内核包了两层薄封装：

```c
/* include/asm-generic/io.h 的语义（简化到只剩动作） */
static inline u32 readl(const volatile void __iomem *addr)
{
    u32 v = *(const volatile u32 *)addr;   /* 一条 load，落到设备 BAR */
    return v;
}

static inline void writel(u32 v, volatile void __iomem *addr)
{
    *(volatile u32 *)addr = v;             /* 一条 store，落到设备 BAR */
}
```

`volatile` 只保证「编译器不许把这次访问优化掉、不许合并」，它**不保证顺序**。顺序要靠屏障：

```c
/* 一个典型的发送路径：先写描述符，再敲门铃 */
desc->addr  = dma_addr;      /* 1. 告诉设备数据在哪 */
desc->len   = len;           /* 2. 多长 */
desc->flags = DESC_OWN;      /* 3. ownership 交给设备 */
wmb();                       /* 4. 关键：上面三笔必须先对设备可见 */
writel(head, q->doorbell);   /* 5. 这一条 store 才触发设备去 DMA */
```

这五步里第 4 步最容易被删掉。原因有三层，一层比一层物理：

1. **编译器**可以重排互不相关的 store（`volatile` 只约束被标记的那次访问，不约束它与其他访问的相对次序）；
2. **CPU** 有 store buffer 与乱序执行，store 可能还在缓冲区里没发出去；
3. **PCIe** 的写是 **posted**——`MemWr` TLP 发出后不等回应，Root Complex 可能继续缓冲。设备看到的顺序不一定等于你代码里的顺序。

结果就是经典的「偶发花屏 / 偶发校验错 / 换个负载就好了」：设备读到 `flags = OWN` 但 `addr` 还是上一包的旧值。这类 bug 不会稳定复现，因为它在赌时序。

### 为什么 `readl` 在 x86 上「自带屏障」，在 ARM64 上不是

x86 的 TSO 模型保证 load 之间不重排、load 不越过更早的 store，所以一次 MMIO 读天然起到「把之前的 posted write 冲出去」的作用——很多驱动就靠「写完寄存器再读一次同一个寄存器」来当屏障用。ARM64 是弱序，`readl` 只是 `ldr`，必须显式加 `rmb()`/`wmb()`。**同一段驱动代码在 x86 上跑三年不出事、移植到 ARM 第一天就炸**，根因往往就是这里。

### 一次 MMIO 读到底多贵

它不是一次 cache 命中，是**一次总线往返**：CPU → Root Complex → PCIe 链路 → 设备 → 原路返回。量级：

| 访问 | 量级 | 说明 |
| --- | --- | --- |
| L1 命中 | ~1 ns | 4 cycle |
| L3 命中 | ~13 ns | §07 的坐标系 |
| DRAM 命中 | ~80 ns | 本机 64 MiB memcpy 掉到 5.1 GB/s 也印证了这一点 |
| **MMIO 读（PCIe 设备）** | **~100–500 ns** | 一次 TLP 往返，设备越远越贵 |
| MMIO 写（posted） | ~20–50 ns | 不等回应，但可能被缓冲后乱序 |

**这条数字是全篇第二个支点**：`readl` 比 L1 命中贵两个数量级。所以内核里所有「循环读寄存器等状态位」的代码都必须带 `udelay`/`cpu_relax`，而且绝对不能拿它搬大块数据——那就是下一节的 PIO。

---

## 14.4 PIO vs DMA：一个字节一个字节搬，还是让设备自己搬

### PIO：CPU 当搬运工

PIO（Programmed I/O）最原始的形态是「CPU 执行一条 load 把数据从设备读进寄存器，再执行一条 store 写进内存」。一个 4 字节的字要两条指令；一个 1 MB 的块要 26 万次这样的往返。

```
        CPU                         设备
         │  ① load  (MMIO read)      │
         │ ────────────────────────► │
         │ ◄──────────────────────── │  数据回到寄存器
         │  ② store (写 RAM)         │
         │ ──────► [DRAM]            │
         │  ③ 重复 262,144 次         │
```

每一步都要等总线往返。**PIO 的吞吐上限不是内存带宽，是「一次 MMIO 往返能搬多少字节」乘以「每秒能做多少次往返」。** 一次搬 4 B、往返 100 ns，上限就是 40 MB/s——比 1 Gbps 链路（125 MB/s）还慢。

这不是理论。§14.8 的实验里，用 `volatile` 逐 4 字节 load+store 拷 1 MiB（设备延迟按零算，已是最乐观），实测 **2.72 GB/s**，而同一台机器上 `memcpy` 是 **11.42 GB/s**——**4.2 倍差距，且还没算真实 MMIO 的 100 ns 往返**。

### DMA：CPU 只写一张「委托单」

DMA（Direct Memory Access）把「搬」这个动作从 CPU 手里拿走：CPU 只写一个**描述符**（源地址、目的地址、长度、方向、OWN 位），敲一下门铃，然后就可以去干别的；设备里的 DMA 引擎自己发 `MemRd`/`MemWr` TLP，把数据搬完，最后用一次中断告诉你「好了」。

```
        CPU                         设备 / DMA 引擎
         │  ① 写描述符（16 B）        │
         │ ────────────────────────► │  [RAM 里的 descriptor ring]
         │  ② writel(doorbell)       │
         │ ────────────────────────► │
         │  ③ 立刻返回，去算别的       │  ④ 自己发 MemRd/MemWr
         │                           │ ──────► [DRAM]
         │  ⑤ 中断：完成了            │
         │ ◄──────────────────────── │
```

关键差别不在「快」，在**谁在等**。PIO 时 CPU 被钉在搬运上，什么也干不了；DMA 时 CPU 在设备搬运的一百多微秒里可以跑别的线程。**这就是「异步」的全部物理内容**（`base/31` 会把这个思想推到 epoll/io_uring）。

### 1 MB 数据的两本账

同一份 1 MB，两种搬法，按本机 2.6 GHz、MMIO 往返 100 ns、`memcpy` 10 GB/s 算：

| 项 | PIO（4 B/次） | DMA（一个描述符） |
| --- | --- | --- |
| 访问次数 | 262,144 次读 + 262,144 次写 | 1 次描述符写 + 1 次门铃 |
| 单次成本 | 100 ns（MMIO 往返） | ~10 ns（写 RAM/寄存器） |
| CPU 时间 | **26.2 ms** | **~1 µs**（含中断约 3 µs） |
| CPU cycle | **6,800 万** | **~2,600**（含中断约 8,000） |
| 设备搬运时间 | —（CPU 就是搬运工） | 1 MB ÷ 10 GB/s = **105 µs** |
| 期间 CPU 能做什么 | 什么都不能 | 跑别的线程 |

两本账的比值是 **~8,500 倍**。这个倍数不是「DMA 更快」的同义反复，它精确地等于「CPU 从搬运工变成发包人」省下的往返次数。

再补一刀：1 MB 走 1 Gbps 要 8 ms，**PIO 的 26 ms 比 1 Gbps 线速还慢 3 倍**——PIO 的真正问题不是「比 DMA 慢」，而是**它让 CPU 成了比链路更窄的那道门**。

反过来，DMA 也有它的账：设备搬 1 MB 要占 105 µs 的 DRAM 带宽（读+写各 1 MB），这段时间 CPU 的访存会变慢。**DMA 没有消灭「搬」，它只是把「搬」从 CPU 的指令流里挪到了总线上。** 这句话是 §14.6 那个「2 次拷贝」问题的伏笔。

### 描述符环：批处理的最小单位

真实设备不会「一次传输写一张描述符」，而是维护一个**环**（ring）：驱动批量填 N 个描述符，敲一次门铃，设备连续处理 N 个。这就是最朴素的批处理——把 N 次 MMIO 往返压成 1 次。

```c
/* 简化后的 virtio 发送：一次塞 16 个 buffer，只敲一次门铃 */
for (int i = 0; i < 16; i++) {
    struct desc *d = &ring->desc[(ring->head + i) % ring->size];
    d->addr  = sg[i].addr;      /* 物理地址（或 IOVA，§14.7） */
    d->len   = sg[i].len;
    d->flags = VRING_DESC_F_NEXT;
    d->next  = (ring->head + i + 1) % ring->size;
}
ring->head = (ring->head + 16) % ring->size;
wmb();                          /* 描述符先可见 */
writel(ring->head, q->notify);  /* 只这一次门铃 */
```

`scatter-gather` 是同一个思想的延伸：一个逻辑传输可以由多个不连续的物理页组成，描述符链把它们串起来，设备按链搬，CPU 不需要先把它们拼成一块连续内存（那次拼接就是一次多余的拷贝）。**IOMMU 缺省时，内核只能用 bounce buffer 来拼——这又是一次拷贝**（§14.7）。

---

## 14.5 中断：设备怎么「叫醒」CPU，以及叫得太勤会怎样

### 从一根线到一条消息

最老的中断是一根物理线：设备拉高 `INTR`，中断控制器（8259 → **IOAPIC**）把它翻译成**中断号**，再送给 CPU 的 **Local APIC**，后者按 IDT 查出处理函数入口。线是共享的，内核要遍历线上所有设备问「是你吗」——这就是 `IRQF_SHARED` 与中断风暴的源头。

PCIe 时代改成了 **MSI/MSI-X**：设备不再拉线，而是**向一个特殊物理地址写一个值**（本质是一次 DMA 写），Root Complex 把它转成 CPU 的中断消息。MSI-X 支持最多 2048 个向量，每个向量可以独立指定目标 CPU——**这是多队列网卡能把中断分散到多核的物理基础**。

本机真实输出（`/proc/interrupts`，节选）：

```
           CPU0       CPU1
 31:    5573641          0  PCI-MSIX-0000:06:0a.0   1-edge      virtio2-req.0
 32:          0    6918028  PCI-MSIX-0000:06:0a.0   2-edge      virtio2-req.1
 34:   54767429          0  PCI-MSIX-0000:06:12.0   1-edge      virtio3-input.0
 35:          0   43054379  PCI-MSIX-0000:06:12.0   2-edge      virtio3-output.0
```

读法：`31` 是中断号，两列是每 CPU 计数，`PCI-MSIX-...` 是控制器类型，`1-edge` 是触发方式，末列是名字。**`virtio2-req.0` 全在 CPU0、`virtio2-req.1` 全在 CPU1**——这不是巧合，是 MSI-X 每个队列一个向量、每个向量绑一个核的结果。这就是中断亲和做对了的样子。

### 上半部与下半部：中断上下文里不许睡觉

中断处理函数运行在**中断上下文**：没有进程上下文，不能睡眠、不能拿会睡眠的锁、不能做长拷贝。所以 Linux 把处理劈成两半：

| | 上半部（hardirq） | 下半部（softirq / tasklet / workqueue） |
| --- | --- | --- |
| 运行上下文 | 中断上下文，本地中断被屏蔽 | softirq 在中断返回点或 `ksoftirqd`；workqueue 在进程上下文 |
| 能否睡眠 | **不能** | softirq 不能；workqueue **能** |
| 典型工作 | ACK 设备、取走描述符、拉起下半部 | 协议栈处理、批量回收、可能阻塞的后续 |
| 成本量级 | 1–3 µs（含 VM exit 更贵） | 按包算，可批处理 |
| 超时的后果 | 中断被屏蔽太久 → 丢中断、看门狗报警 | `ksoftirqd` 打满一个核，应用线程饿死 |

网卡收包是这套分工的教科书：上半部只做「关中断、记下有个包、把 NAPI 挂进 poll 列表」，真正的协议栈处理在 `NET_RX_SOFTIRQ` 里做。**上半部越短，能承受的中断率越高。**

### 中断合并：用延迟换 CPU

如果一个包一次中断，40 Gbps 就是 **325 万次中断/秒**。就算每次只要 2000 cycle，也是 65 亿 cycle/s——2.5 个核纯做中断入口。所以必须合并：

| 策略 | 中断次数（40 Gbps） | 额外延迟 | CPU 成本/包 | 适用 |
| --- | --- | --- | --- | --- |
| 每包一中断 | 3.25 M/s | 0 | ~6250 cycle | 低速率、极低延迟 |
| NAPI（收一个包后关中断，poll 最多 64 个） | ~51 K/s | 一次 poll 的排队时间 | ~2000 cycle | 通用默认 |
| 中断合并（等 N µs 或 M 个包） | ≤20 K/s | **+N µs**（常配 50–100 µs） | 更低 | 高吞吐、可容忍延迟 |
| 纯轮询（`io_uring` IOPOLL / DPDK） | 0 | 0（但空转烧核） | 最低 | 极限 PPS、专用核 |

这张表就是「延迟 vs 吞吐」的完整取舍：**合并把 N 次中断摊成 1 次，代价是你多等了 N µs。** 交易系统把 `rx-usecs` 调到 0，不是因为不懂优化，是因为那 50 µs 比几个核更贵。

### 中断亲和：把向量绑到正确的核

```bash
$ cat /proc/irq/31/smp_affinity_list
0
$ cat /proc/irq/32/smp_affinity_list
1
```

这两个文件就是「31 号中断只许 CPU0 处理、32 号只许 CPU1」的全部配置。`irqbalance` 会自动调，但它只看中断计数，不看**数据局部性**：如果 `virtio2-req.1` 的中断在 CPU1，而处理这个队列的应用线程在 CPU0，那么每个包都要跨核搬一次 cache line，NUMA 机器上还要跨 socket。**正确的做法是「队列 i 的中断 + 读队列 i 的线程」绑同一个核**，这也是 `base/25` 讲的亲和性在 I/O 侧的直接推论。

---

## 14.6 一次网卡收包的搬运账：至少 2 次拷贝，以及它为什么在 40 Gbps 撞墙

把 §14.4 和 §14.5 拼起来，一次收包的完整路径是：

```
 ① 网卡 DMA 写 ──► RX ring（内存里的描述符 + 缓冲区）
                        │
 ② 硬中断（MSI-X）──────┘  只做：关中断、挂 NAPI
                        │
 ③ 软中断 NET_RX_SOFTIRQ ─► 驱动 poll：取描述符、建 skb
                        │        └─ 拷贝 #1：ring buffer → skb
 ④ 协议栈 IP/TCP ────────► 查路由、校验、按四元组找 socket
                        │
 ⑤ 入 socket 接收队列 ────► TCP 通常直接挂 skb，不再拷
                        │
 ⑥ 用户 read() ──────────► 拷贝 #2：socket 缓冲区 → 用户缓冲区
```

**结论：常见路径上每个字节至少被 CPU 拷 2 次。** 一次是驱动把数据从 DMA 缓冲区搬进 skb（很多驱动用 `build_skb` + page pool 可以省掉这次，让 skb 直接指向缓冲区），一次是 `read()` 把数据从内核缓冲区搬进用户空间（除非用 `mmap`/`AF_XDP`/`MSG_ZEROCOPY`）。

把拷贝次数换成 40 Gbps 下的内存流量：

| 路径 | CPU 拷贝次数 | 每 GB 数据的内存流量 | 40 Gbps 时需要 |
| --- | --- | --- | --- |
| 收包 + `read()` | 2 | 5 GB（DMA 写 1 + 拷贝读 2 + 拷贝写 2） | **25 GB/s** |
| 发包 + `write()` | 1 | 3 GB | 15 GB/s |
| `sendfile`（明文） | 0 | 2 GB（DMA 读 + DMA 写） | 10 GB/s |
| TLS + `sendfile` | 2（解密 + 加密） | 5 GB | 25 GB/s |

本机实测的 `memcpy` 带宽是 **5.12 GB/s 移动**（64 MiB，即读+写约 10.2 GB/s，§14.8）。也就是说：

- 跑到实测的 **8 Gbps**（1 GB/s）时，内存流量已经是 5 GB/s，**吃掉了这台机器一半的拷贝能力**；
- 想跑到 **40 Gbps**，需要 25 GB/s 的内存流量，是这台机器能力的 **2.5 倍**——**光靠「把中断改成 NAPI」永远到不了，因为墙不在中断，在拷贝。**

这就是零拷贝的全部理由——它不是「高级技巧」，是**带宽账算不下去时的唯一出路**：

- `sendfile(out_fd, in_fd, ...)`：数据从 page cache 直接进网卡，不进用户态，省掉 2 次拷贝；
- `splice()`：用管道做中转，内核里只搬引用（page 指针），不搬字节；
- `mmap()` 把文件页映射进用户地址空间，省掉「内核→用户」那次拷贝；`MSG_ZEROCOPY` / `AF_XDP` / `io_uring` 注册缓冲区则把「拷贝」换成「所有权转移」。

但零拷贝有一个致命的边界：**它只对「不需要看内容」的数据成立。** TLS 要求加密，加密必须看每一个字节，于是 `sendfile` 在 TLS 上直接失效——数据必须先解密到用户态、再加密回内核，两次拷贝一次都省不掉。这条坑在 §14.10 单列一行。四种实现的完整 syscall 账在 `base/37`，收包六道门的完整版在 `base/32`。

---

## 14.7 内存映射与 IOMMU：DMA 能绕过页表，所以必须有第二个 MMU

### 一个被忽略的事实：设备不经过 MMU

CPU 访存要经过页表翻译，页表写着「这一页能不能读/写/执行」。这套保护撑起了进程隔离与 `base/33` 的全部安全模型。

**但设备 DMA 不经过这套页表。** 设备把「物理地址」直接放到总线上，Root Complex 转成 DRAM 访问——中间没有任何「这个地址属不属于当前进程」的检查。也就是说：

> 一个有 bus-master 能力的设备，可以读写**整个物理内存**，包括内核页表、其他进程的密钥、`sudo` 的密码。

这不是理论漏洞，是 PCIe 的设计前提，所以没有额外保护的 VFIO 直通，等于把整台机器的内存交给虚拟机里的驱动。

### IOMMU：给设备用的第二套页表

IOMMU 就是「设备的 MMU」。设备发出的地址不再是物理地址，而是 **IOVA**（I/O Virtual Address）；IOMMU 用它自己的一套页表把 IOVA 翻成物理地址，同时做权限检查。

```
  设备 ──IOVA──► [ IOMMU ] ──物理地址──► DRAM
                    │
                    └─ 页表：这个设备能碰哪些页、读还是写
```

它买到三样东西：

| 能力 | 没有 IOMMU 时 | 有 IOMMU 时 |
| --- | --- | --- |
| 隔离 | 设备可读写全部物理内存 | 只能碰映射给它的页 |
| 地址宽度 | 32 位设备碰不到 >4 GB 内存，只能用 bounce buffer 拷到低地址 | IOVA 可以映射到任意物理页，**省掉一次 bounce 拷贝** |
| 分散聚合 | 设备要求物理连续的大缓冲 | 可以把不连续的物理页映射成连续的 IOVA，scatter-gather 真正生效 |
| 虚拟化直通 | 不安全，QEMU 只能全模拟 | VFIO 把设备安全地交给虚拟机 |

代价有三样：**翻译要走 IOTLB**（miss 时多一次内存访问，某些负载掉 5–15% 带宽）、**页表占内存**、**故障处理复杂**（设备页错误要经过 PRI/ATS）。大页（2 MB/1 GB）能提高 IOTLB 覆盖率，所以高性能网卡/GPU 驱动偏爱大页。

### 本机的真实情况：没有 IOMMU

```bash
$ ls /sys/class/iommu
（空）
$ cat /proc/cmdline
BOOT_IMAGE=/boot/vmlinuz-6.12.113+deb12-cloud-amd64 ... ro console=tty0 ...
$ dmesg | grep -i iommu
（无输出）
```

`/sys/class/iommu` 是空的、命令行没有 `intel_iommu=on`/`amd_iommu=on`、`dmesg` 里没有 DMAR/IVRS——**这台 2 核 KVM 没有向 guest 暴露 IOMMU**。这意味着两件事，都必须诚实说明：

1. 本机的 `virtio` 设备是**半虚拟化**的，DMA 实际由宿主的 QEMU 完成，guest 里看到的「DMA」是一次 virtqueue 通知，不是真实的 bus-master DMA；
2. 本机**做不了 VFIO 直通**，`vfio-pci` 绑定会因为没有 IOMMU group 而失败。

所以在真机上验证 IOMMU，要看的命令是：

```bash
$ ls /sys/kernel/iommu_groups/          # 每个 group 一个目录
$ cat /sys/kernel/iommu_groups/0/devices/*
$ dmesg | grep -i -E "DMAR|AMD-Vi|IOMMU enabled"
```

一个 group 里的所有设备必须一起直通，因为它们共享同一个 IOMMU 翻译域——这条约束是「一张 GPU 加它的音频功能必须一起给虚拟机」的物理原因。`base/15` 会把这条线接到 VM/容器的隔离强度上。

---

## 14.8 实测：能跑的都跑了，跑不了的明说

**先报工具可用性**：本机 `lspci`、`dd`、`gcc` 可用；**`perf` 与 `strace` 未安装**，所以本篇不给 `perf stat`/`strace -c` 的输出，涉及它们的地方只用 `/proc` 与自写基准替代。`ethtool` 也没有，队列合并参数只能读 `/sys`。

### 实验①：中断都落在哪个核

```bash
$ cat /proc/interrupts | head -20
           CPU0       CPU1
  1:          9          0   IO-APIC   1-edge      i8042
  8:          0          0   IO-APIC   8-edge      rtc0
 24:          0          0  PCI-MSIX-0000:00:1c.0   0-edge      PCIe PME, aerdrv
 28:          0          0  PCI-MSIX-0000:06:08.0   0-edge      virtio1-config
 29:          7          0  PCI-MSIX-0000:06:08.0   1-edge      virtio1-virtqueues
 31:    5573641          0  PCI-MSIX-0000:06:0a.0   1-edge      virtio2-req.0
 32:          0    6918028  PCI-MSIX-0000:06:0a.0   2-edge      virtio2-req.1
 34:   54767429          0  PCI-MSIX-0000:06:12.0   1-edge      virtio3-input.0
 35:          0   43054379  PCI-MSIX-0000:06:12.0   2-edge      virtio3-output.0
NMI:          0          0   Non-maskable interrupts
LOC:  148667358  142852708   Local timer interrupts
```

三个结论：

1. **`virtio2-req.0` 与 `virtio2-req.1` 分居两个核**，说明 MSI-X 多队列 + 亲和生效；如果两列都堆在 CPU0，那就是 §14.10 第二行的坑。
2. `LOC`（本地定时器）两边几乎相等（1.487 亿 vs 1.428 亿），说明两个核负载均衡；`TASKLET` 却是 `42,760` vs `3,388,263`（`/proc/softirqs`），差 79 倍——**这是「某个下半部只在一个核上跑」的指纹**。
3. `virtio3-input.0` 有 5476 万次，远高于其它队列，说明这块设备的收包量最大；中断计数就是流量计。

再取 3 秒增量：

```bash
$ awk '/virtio2-req.0/{a=$2} END{print a}' /proc/interrupts; sleep 3; \
  awk '/virtio2-req.0/{print $2}' /proc/interrupts
5574215
5574244
```

3 秒 29 次——盘是空闲的。**空闲设备的中断率接近 0，忙设备的中断率就是它的包速率**，这条对照可以用来快速判断「中断是不是被打满了」。

### 实验②：设备的寄存器住在哪

```bash
$ cat /proc/iomem | head -25
00000000-00000fff : Reserved
00001000-0009fbff : System RAM
0009fc00-0009ffff : Reserved
000a0000-000bffff : PCI Bus 0000:00
00100000-7ffd2fff : System RAM
  12400000-133fffff : Kernel code
  13400000-13ea4fff : Kernel rodata
  14000000-14272bff : Kernel data
7ffd3000-7fffffff : Reserved
80000000-afffffff : PCI Bus 0000:00
b0000000-bfffffff : PCI ECAM 0000 [bus 00-ff]
c0000000-febfffff : PCI Bus 0000:00
  fc000000-fc7fffff : PCI Bus 0000:05
    fc000000-fc1fffff : PCI Bus 0000:09
      fc000000-fc003fff : 0000:09:01.0
        fc000000-fc003fff : virtio-pci-modern
fec00000-fec003ff : IOAPIC 0
```

`fc000000-fc003fff` 就是 §14.3 说的那个 16 KB BAR；`fec00000` 是 IOAPIC，**中断控制器本身也是被映射进地址空间的一个设备**。`b0000000-bfffffff` 是 PCIe ECAM——配置空间同样是 MMIO。

### 实验③：链路协商到了多少

```bash
$ lspci -vv | grep -i -A1 "LnkSta" | head -2
		LnkSta:	Speed 16GT/s, Width x32
			TrErr- Train- SlotClk- DLActive- BWMgmt- ABWMgmt-
```

`16GT/s × 32 lane` = Gen4 x32 ≈ 每方向 63 GB/s。**但这个数字是 QEMU 模拟出来的，不是真实测速**：它只说明「协商结果」和「设备声称的能力」，不代表你能跑出这个带宽。判断真实带宽只能靠压测，不能靠 `lspci`。

### 实验④：`dd` 的单次拷贝带宽（以及它其实不是拷贝）

```bash
$ dd if=/dev/zero of=/dev/null bs=1M count=1000
1048576000 bytes (1.0 GB, 1000 MiB) copied, 0.12545 s, 8.4 GB/s

$ dd if=/dev/zero of=/dev/null bs=4k count=200000
819200000 bytes (819 MB, 781 MiB) copied, 0.205771 s, 4.0 GB/s
```

两组：

- `bs=1M` 的 8.4 GB/s 是「写零到 `/dev/null`」——**没有源读取，也没有内存写入**，所以它测的是系统调用与循环的开销，不是拷贝带宽；
- 换成 `bs=4k` 掉到 4.0 GB/s，因为系统调用次数从 1000 次涨到 20 万次——**同一份数据量，跨界次数多 200 倍，吞吐减半**。这是主线③「单价 = 跨界次数 × 单次成本」最便宜的一个实证。

真正测拷贝要用 `memcpy`：

```c
#include <time.h>
static double now(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec * 1e-9;
}
static void bench(const char *tag, size_t n, int iters) {
    char *a = malloc(n), *b = malloc(n);
    memset(a, 0xA5, n);
    for (int i = 0; i < 8; i++) memcpy(b, a, n);          /* 预热 */
    double t0 = now();
    for (int i = 0; i < iters; i++) {
        memcpy(b, a, n);
        __asm__ volatile("" :: "r"(b) : "memory");         /* 阻止死写被优化掉 */
    }
    double dt = now() - t0;
    printf("%-22s memcpy %4zu KiB x %4d = %7.3f s -> %6.2f GB/s 移动\n",
           tag, n >> 10, iters, dt, (double)n * iters / dt / 1e9);
}
```

把内层换成逐 4 字节 `volatile` load+store、跑同一个 1 MiB，就得到最后一行：

```
1 MiB (L2/L3)          memcpy 1024 KiB x  400 =   0.037 s ->  11.42 GB/s 移动
4 MiB                  memcpy 4096 KiB x  120 =   0.048 s ->  10.58 GB/s 移动
64 MiB (DRAM)          memcpy 65536 KiB x  12 =   0.157 s ->   5.12 GB/s 移动
1 MiB 逐 4B load+store  x  400 =   0.154 s ->   2.72 GB/s 移动, 每 4B 1.47 ns
```

前三行读出三件事：**① 工作集超过 L3 后掉一半**（11.42 → 5.12，§07 的层次结构）；**② 这台机器的 DRAM 拷贝上限约 10 GB/s（读+写）**，这是 §14.6 那本 25 GB/s 账的分母；**③ 逐 4 字节访问比 `memcpy` 慢 4.2 倍**，而这已经是零设备延迟的乐观值——真实 PIO 再加 100 ns/次的 MMIO 往返，就是 §14.4 的 26 ms。

### 实验⑤：软中断与丢包

```bash
$ head -4 /proc/softirqs
                    CPU0       CPU1
          HI:          0          0
       TIMER:   24856084   16698820
      NET_RX:   54520373   43995223

$ cat /proc/net/dev | grep eth0
  eth0: 194588022614 75332482    0 823689    0     0 ...
```

`NET_RX` 两核分别是 5452 万与 4399 万，说明收包软中断确实分散了；但 `eth0` 的 **drop 是 823,689**（75,332,482 个包里丢了约 **1.1%**）。drop 通常意味着 **ring buffer 满**或**协议栈 backlog 满**，不是链路坏了。这是本篇最实用的一条：**吞吐上不去先看 drop，drop 不为零就说明某一层队列已经满了**。

---

## 14.9 三列账：总线与中断承诺了什么、收了多少、从哪开始崩

| 维度 | 承诺了什么 | 为此付了什么 | 从哪里开始失效 |
| --- | --- | --- | --- |
| 统一寻址（MMIO） | 一条 `load`/`store` 就能碰设备，不需要新指令 | 每次 MMIO 是一次总线往返（100–500 ns），不可缓存，顺序要屏障 | 轮询 `readl` 打满一个核；漏 `wmb()` 让设备读到半成品描述符 |
| 总线带宽 | 点对点、全双工、加 lane 就能加带宽 | 上游（Root Complex、DRAM）是共享的；lane 数与代际决定天花板 | 多卡共享同一组 lane；DMA 与 CPU 抢同一池内存带宽 |
| DMA | CPU 只写描述符，搬运交给设备 | 映射/一致性/IOMMU 的复杂度；设备可绕过页表 | 没有 IOMMU 时直通不安全；bounce buffer 偷偷多一次拷贝 |
| 中断 | 事件驱动，不用轮询，CPU 可以空闲 | 每次中断有固定成本；上半部不能睡眠 | 每包一中断 → 325 万 IRQ/s，一个核被 `%soft` 打满 |
| MSI-X 多队列 | 中断可以分散到多核，队列可以并行 | 队列与 CPU 的亲和必须配对 | 配错 → 一个核 100%、其它核看戏；跨核搬 cache line |
| 零拷贝 | 省掉 1–2 次 CPU 拷贝 | 生命周期/所有权/错误处理都变复杂 | TLS 下失效；`splice` 过管道仍可能是一次拷贝 |

一句话收束：**这套机制买到的是「CPU 不必亲自搬数据、也不必亲自等设备」，付出的是「每一次跨界都有固定单价，而且顺序与隔离都要人手动维护」。** 40 Gbps 跑不动的三个原因——中断太密、拷贝太多、内存带宽被吃光——正好是这张表的三行。

---

## 14.10 踩坑清单：现象 → 根因 → 定位 → 修 → 防复发

| 现象 | 根因（指回本篇） | 怎么定位 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 吞吐上不去，`top` 里 `%soft`（`si`）很高，应用线程饿 | 每包一中断 / 单队列，软中断在一个核上排队（§14.5） | `top` 看 `si`；`/proc/softirqs` 看 `NET_RX` 是否只涨一个核；`/proc/net/softnet_stat` 第二列是否在涨 | 开多队列（`ethtool -L`）、开 RPS/RFS、调大 ring、调 coalesce | 队列数 = 核数写进部署模板；压测验收看 `si` 不看 `us` |
| CPU0 100%、CPU1 空闲，但流量明明很大 | MSI-X 向量全落在 CPU0，或 `irqbalance` 把它绑错了（§14.5） | `/proc/interrupts` 看两列分布；`/proc/irq/N/smp_affinity_list` | 手写 affinity，按「队列 i ↔ 核 i」绑定；关掉会乱搬的 `irqbalance` | 用脚本在启动时固定亲和，并纳入配置管理 |
| `/proc/net/dev` 的 drop 持续增长，重传也涨 | ring buffer 或 backlog 满（§14.8 实验⑤） | `ethtool -S` 看 `rx_missed`/`rx_no_buffer`；`/proc/net/softnet_stat` 看 dropped | 加大 ring、提高 NAPI budget、加队列、降中断合并延迟 | 把 drop 做成告警指标，而不是等用户报障 |
| 上了 `sendfile`，CPU 没降、吞吐没涨 | 流量是 TLS，加密必须看内容，零拷贝不成立（§14.6） | `strace -c` 看 `sendfile` 是否真的返回大值；看 CPU 是否花在 `AES`/`copy` 上 | 用 kTLS 把加解密下沉到内核，或改用用户态零拷贝方案 | 在架构评审里写清「零拷贝只对明文成立」 |
| 设备偶发数据错、花屏、校验和不对，换负载就好 | 描述符写完没屏障，设备读到旧值（§14.3） | 开 `CONFIG_DMA_API_DEBUG`；在 doorbell 前加断言/日志 | 补 `wmb()`/`dma_wmb()`，`dma_map`/`dma_unmap` 成对 | Code review 把「doorbell 前必须有屏障」列为检查项 |
| 直通设备后虚拟机不稳，或能读到不该读的内存 | 无 IOMMU 或 `iommu=pt` 直通（§14.7） | `ls /sys/class/iommu`、`ls /sys/kernel/iommu_groups/`、`dmesg \| grep -i DMAR` | 开 `intel_iommu=on iommu=pt`，用 VFIO 绑定 | 直通前验证 IOMMU group；生产机默认开 IOMMU |
| 延迟毛刺周期性出现，p99 比 p50 高一个数量级 | 中断合并等太久，或 `ksoftirqd` 被调度延迟（§14.5） | 看 `rx-usecs`/`rx-frames`；`/proc/interrupts` 增量是否成簇 | 降合并窗口、给收包线程独占核 | 用 p99 而非均值验收；合并参数写进 SLA |

三行总结：**看到 `si` 高，先怀疑中断太密；看到 drop 涨，先怀疑队列满；看到「优化了却没效果」，先怀疑你优化的是拷贝，而瓶颈在内存带宽或 TLS。**

---

## 14.11 一页速记

```
【一句话】CPU 最贵的资源不是算力，是搬数据的次数；DMA/零拷贝/批处理都在砍这个次数。

【三个换算】
  40 Gbps = 5 GB/s；1500 B 帧 → 3.25 Mpps；每包 6250 cycle（2.4 µs @2.6 GHz）
  → 打满线速要 7.8 个核；2 核最多 10.3 Gbps，实测 8 Gbps
  PCIe：每 lane 有效带宽 = 线速率 × 编码效率 ÷ 8
        Gen3 ≈ 1 GB/s、Gen4 ≈ 2、Gen5 ≈ 4（都是单向）

【两本账（1 MB 数据）】
  PIO 4 B/次：262,144 × 100 ns = 26.2 ms，6,800 万 cycle
  DMA：1 张描述符 + 1 次门铃 ≈ 1 µs，设备自己搬 105 µs
  比值 ≈ 8500×；DMA 没消灭搬运，只把 CPU 从搬运工换成发包人

【一次收包的 2 次拷贝】
  ring → skb（驱动）、socket → 用户（read）
  40 Gbps 下 = 25 GB/s 内存流量；本机 memcpy 只有 10 GB/s（读+写）
  → 墙不在中断，在拷贝；这就是零拷贝的唯一理由

【四个必须记住的数字】
  MMIO 读 ≈ 100–500 ns（比 L1 慢两个数量级）
  中断入口 ≈ 2000 cycle（KVM 下含 VM exit）
  每 lane Gen4 ≈ 2 GB/s；x16 Gen4 = 31.5 GB/s 单向
  没有 IOMMU，设备 DMA 可以读写整个物理内存

【三个坑的同一条根】
  %soft 高      = 中断太密              → 多队列 + NAPI + 合并
  drop 增长     = 某一层队列满          → 加 ring / 加队列 / 提 budget
  零拷贝无效    = 数据要被看（TLS）      → kTLS 或用户态方案，别硬套 sendfile
```

---

## 14.12 自检 3 题

**Q1.** 一台 2 核机器（2.6 GHz）接 40 Gbps 网卡，实测 8 Gbps。请用「包速率 × 每包 cycle ÷ 主频」算出它需要几个核，并说明如果只把「每包一中断」改成 NAPI，理论上能到多少；为什么还到不了 40 Gbps？

<details><summary>答案</summary>

按 1500 B 帧、线上 1538 B 算，40 Gbps = 5 GB/s，包速率 = 5×10⁹ ÷ 1538 ≈ **3.25 Mpps**。每包 6250 cycle（硬中断 2000 + 驱动 1300 + 协议栈 1400 + 两次拷贝 800 + 唤醒 750），所以需要的核数：

```
3.25×10⁶ × 6250 ÷ 2.6×10⁹ ≈ 7.8 个核
```

2 核理论上能跑 `2 ÷ 7.8 × 40 ≈ 10.3 Gbps`，实测 8 Gbps（一个核要跑应用）。

改成 NAPI 后，每包只省掉硬中断那约 2000 cycle（6250 → 4250），2 核上限变成 `2 × 2.6×10⁹ ÷ 4250 ÷ 3.25×10⁶ × 40 ≈ 15 Gbps`——**翻了一倍，但离 40 还差一半多**。剩下的墙在 §14.6 那本账里：40 Gbps 的 2 次拷贝需要 25 GB/s 内存流量，而本机 `memcpy` 只有 10 GB/s（读+写）。**中断是第一道墙，拷贝是第二道，而且更硬。** 要真到 40 Gbps，必须同时做零拷贝并把收包线程绑到独占核。
</details>

**Q2.** 驱动在 `writel(doorbell)` 之前漏了 `wmb()`，为什么可能「平时没事、压测才炸、换台 ARM 机器必炸」？请分别从编译器、CPU、PCIe 三层解释，并说明 x86 上为什么 `readl` 有时能「意外」救回来。

<details><summary>答案</summary>

三层各有一个重排源：

1. **编译器**：普通 store 之间可以重排（`volatile` 只保证被标记的访问不被删除/合并，不保证它与其他访问的次序），于是 `flags = OWN` 可能被提到 `addr = ...` 前面；
2. **CPU**：store buffer 与乱序执行会让 store 的**可见顺序**不等于程序顺序，`flags` 可能先到达一致性点；
3. **PCIe**：写是 posted，`MemWr` TLP 发出即返回，Root Complex 和 switch 都可能继续缓冲，设备看到的顺序还可能再变一次。

三层叠加的结果是设备看到 `OWN=1` 但 `addr` 还是旧的——它照着旧地址 DMA，数据就写错地方。平时没事是因为负载轻、缓冲区空、时序恰好对；压测时队列满、流水线深，概率就上来了。

x86 是 TSO，load 不越过更早的 store，所以「写完寄存器再 `readl` 同一个寄存器」会**顺带**把之前的 posted write 冲出去，看起来像「自带屏障」。ARM64 是弱序，`readl` 只是 `ldr`，没有这个副作用，所以同一段代码在 ARM 上第一次跑就暴露。正确写法是显式 `wmb()`（或 `dma_wmb()`）+ 必要时 `readl` 回读确认，不依赖架构的偶然行为。
</details>

**Q3.** 有人给一个 HTTPS 服务加了 `sendfile`，声称「零拷贝能省两次拷贝」，结果 CPU 没降。请解释为什么，并给出两条真正有效的路径；再说明「零拷贝」到底省掉的是哪两次拷贝、省不掉的是哪两次。

<details><summary>答案</summary>

`sendfile` 省的是「page cache → 用户缓冲区 → socket 缓冲区」这两次 CPU 拷贝，前提是**数据不被修改**。TLS 必须对每个字节加密/解密，数据要先读进 CPU 算完再写回，于是：

- 发送路径变成：page cache → 用户态（解密/加密）→ 内核 socket → 网卡，**两次拷贝一次没少**；
- `sendfile` 在内核里根本拿不到明文，通常会退化成普通 `read`/`write`，或者干脆不能用。

两条有效路径：① **kTLS**（`setsockopt(TLS_TX)`）把加解密下沉到内核，配合 `sendfile` 仍可零拷贝；② **用户态零拷贝**（`io_uring` 注册缓冲区、`MSG_ZEROCOPY`、`AF_XDP`），让用户态自己管缓冲区。

「零拷贝」省掉的是 **CPU 执行的 `memcpy`**；省不掉的是 **DMA 本身**（网卡 ↔ 内存）和**加解密**（CPU 必须看数据）。所以它不等于「没有数据流动」，只等于「CPU 不再当搬运工」——这正是本篇那句话的又一次落地。
</details>

---

## 14.13 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 多核缓存一致性、伪共享与内存屏障（本篇 §14.3 的硬件底座） | → `base/13` |
| 特权级、ring、微内核与 VM/容器的隔离边界（IOMMU 是其中一块） | → `base/15` |
| socket 收包全路径：硬中断、软中断、NAPI、缓冲区（本篇 §14.6 的完整版） | → `base/32` |
| 零拷贝四实现：`read/write`、`mmap`、`sendfile`、`splice` 的 syscall 账 | → `base/37` |
| 高并发连接与 netpoll：软中断、socket 缓冲区在工程里的形态 | → [go/32](../go/32-high-concurrency.md) |
| 对象存储与云盘：DMA、块层与远端存储的带宽账 | → [comp/42](../comp/42-object-storage-and-cloud-disk.md) |
| 全书总纲：四条主线与 56 篇清单 | [0-summary](0-summary.md) |
