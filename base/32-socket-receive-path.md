# 32. socket 与收包全路径：硬中断、软中断、NAPI、缓冲区与零拷贝

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 操作系统：Linux 视角 ｜ **编号**：32 / 56
> **本篇定位**：Part III 的第十二篇。31 讲「谁来等」，本篇讲「包怎么进来、在哪一层被丢」；它把 `base/14` 的 DMA/中断账展开成六道门，把 `base/31` 的 epoll 唤醒接到 `sk_data_ready` 上，并为 `base/37` 的零拷贝实测提供收包侧的对照。全篇只有一条推论链——`每个包都要排队` → `六道门各有自己的队列与计数器` → `哪个计数器动，就是哪道门满了` → `pps 而不是 bps 决定 CPU 上限`。
> **读法**：§32.2 的六道门图是全篇支点，先记住「①③⑤有数据队列、②④⑥只慢不丢」；§32.6 的 1 µs/包与 §32.7 的 BDP 是两张算账单；§32.10 的六组输出可以原样复现；赶时间只读 §32.11–32.13。
> **不在本篇**：epoll/io_uring 的 API 与 ET/LT 语义 → `base/31`；零拷贝四种实现的 `strace -c` 对比 → `base/37`；TCP 状态机与 TIME_WAIT → `base/41`；拥塞控制与滑动窗口的完整算法 → `base/42`；DMA/总线/MMIO 的物理层 → `base/14`；Go netpoll 的运行时实现 → `go/19`、`go/32`。

---

## 32.1 引子：一条 10 Gbps 的链路，为什么只跑到 3 Gbps

2026-03，某 API 网关从 1 Gbps 升到 10 Gbps。升级后监控一直显示出向 **3.1 Gbps**，丢包 0.8%；交换机端口协商是 10G 全双工，光模块无 CRC 错。值班同学的第一反应是「链路没升上去」，第二反应是「交换机限速」。两种都错。

真正的现场是：`top` 里 **一个核 `%si` 100%**，其余核 20%–30%；`iperf3 -P 1` 只有 3.1 Gbps，`-P 4` 立刻到 8.9 Gbps。单流和四流的差别，把问题从「链路」一把推到了「一个核」。

| 时间 | 动作 | 观测 |
| --- | --- | --- |
| 09:12 | 升级后巡检 | 出向 3.1 Gbps，重传 0.8%，交换机无 CRC |
| 09:20 | `ethtool eth0` / 交换机 | 10G 全双工，链路层干净 |
| 09:35 | `iperf3 -P 1` vs `-P 4` | 3.1 Gbps vs 8.9 Gbps → 不是链路带宽 |
| 09:50 | `top` | **Cpu2 `%si` 100%**，其余核 20%–30% |
| 10:05 | `/proc/interrupts` | RX 队列中断全在 CPU2；`/sys/.../queues` 只有 `rx-0` |
| 10:20 | `/proc/net/softnet_stat` | `time_squeeze` 每秒涨几百，`dropped` 开始涨 |
| 10:35 | `/sys/class/net/eth0/statistics` | `rx_dropped` 已累计 82 万；`ss -m` 的 `r` 接近 `rb` |
| 10:50 | 修复 | 开多队列/ RPS、IRQ 分散、ring 4096、backlog 8192、应用 256 KB 读、`tcp_rmem` 提到 32 MB |
| 11:20 | 复测 | `-P 4` 9.4 Gbps，各核 `%si` 30%–40% |
| 11:30 | 复盘 | 不是带宽不够，是第③道门只有一条 CPU |

按 600 B 的平均包长算一遍：3.1 Gbps ≈ **64.6 万 pps**；10 Gbps ≈ **208 万 pps**。一个核每秒能处理多少包，取决于每包在协议栈里花多少 cycle——本机实测约 **1 µs/包**（§32.6），也就是 260 万 cycle/秒的预算下，单核只能扛约 100 万 pps。3.1 Gbps 的 64.6 万 pps 刚好把单核吃到接近 100%，而 10 Gbps 需要两到三个核，单队列却只有一条中断线。

本篇只回答一个问题：**一次收包要穿过网卡→硬中断→软中断→协议栈→socket 缓冲区→唤醒进程六道门；带宽上不去，几乎总是「某一层队列满了」或「某一层只有一条 CPU」。** 下面先给总图，再逐门拆。

---

## 32.2 六道门总图：每一道门都有自己的队列和计数器

先看全图。从下往上是包进来的方向，从上往下是应用读走的方向；每道门都标了队列名、容量和「满了会怎样」。

```
 应用 read()/recvmsg()  ← ⑥ copy_to_user（CPU 拷贝）
        ▲
        │ sk_data_ready → epoll 回调 → wake_up
┌───────┴──────────────────────────────┐
│ ⑥ 进程就绪队列（runqueue）              │ 容量：CPU 数；满了：P99 涨
└───────▲──────────────────────────────┘
        │ tcp_data_queue / udp_queue_rcv_skb
┌───────┴──────────────────────────────┐
│ ⑤ socket 接收队列 sk_receive_queue      │ 容量：sk_rcvbuf
│   记账：sk_rmem_alloc / sk_rcvbuf      │ 满了：TCP 零窗口 / UDP 丢
└───────▲──────────────────────────────┘
        │ ip_rcv → tcp_v4_rcv（CPU 直接跑，无队列）
┌───────┴──────────────────────────────┐
│ ④ 协议栈：ip_rcv / tcp_v4_rcv          │ 容量：CPU 时间
│   sk_buff 分配、路由、查 socket、校验和  │ 满了：%si 100% / 丢在 backlog
└───────▲──────────────────────────────┘
        │ netif_receive_skb
┌───────┴──────────────────────────────┐
│ ③ NAPI poll（NET_RX 软中断）            │ budget 64/设备
│   softnet_data.poll_list + backlog     │ 满了：time_squeeze / dropped
└───────▲──────────────────────────────┘
        │ napi_schedule()（只登记，不处理）
┌───────┴──────────────────────────────┐
│ ② 硬中断（MSI-X，每队列一条）            │ 合并：rx-usecs / rx-frames
│   中断处理函数只做「记一笔 + 触发 NAPI」  │ 满了：IRQ 风暴 / %hi
└───────▲──────────────────────────────┘
        │ 描述符 DD 位
┌───────┴──────────────────────────────┐
│ ① NIC RX ring（DMA 写内存）             │ 256–4096 个描述符
│   rx-0 / rx-1 … / rx-N                 │ 满了：网卡丢，rx_dropped
└───────▲──────────────────────────────┘
        │ 光/电
```

| 门 | 队列/位置 | 容量（本机或典型值） | 满了会怎样 | 计数在哪 |
| --- | --- | --- | --- | --- |
| ① NIC RX ring | 描述符环，每队列一个 | 256–4096 个描述符；`ethtool -g` | 网卡无空闲描述符，直接丢 | `rx_dropped`、`rx_missed_errors`、`rx_fifo_errors` |
| ② 硬中断 | MSI-X pending / IRQ line | 合并参数 `rx-usecs`、`rx-frames` | IRQ 风暴；CPU 在硬中断里出不来 | `/proc/interrupts`、`%hi` |
| ③ NAPI poll | `softnet_data.poll_list` + `input_pkt_queue` | `dev_weight=64`、`netdev_budget=600`、`netdev_max_backlog=8192` | 预算耗尽 `time_squeeze`；backlog 满 `dropped` | `/proc/net/softnet_stat` |
| ④ 协议栈 | 无队列，CPU 时间就是容量 | 每包约 1 µs（本机实测） | `%si` 100%；包在 backlog 里等 | `top` 的 `si`、`perf` |
| ⑤ socket 接收队列 | `sk_receive_queue` | `sk_rcvbuf`；`rmem_max` 32 MB | TCP 零窗口；UDP 丢包 | `ss -m` 的 `r/rb/d`、`/proc/net/snmp` |
| ⑥ 唤醒进程 | runqueue | CPU 数 | 进程等调度，P99 拉长 | `perf sched`、`runqlat` |

这张表要读出三件事：**会丢包的只有 ①③⑤**（②是事件、④是 CPU、⑥是调度队列，只会慢）；**三个 drop 计数分属三道门**（`rx_dropped`→①、`softnet dropped`→③、`ss d`/`Udp RcvbufErrors`→⑤），混为一谈就会在错的门上拧螺丝；**pps 才是收包路径的货币**——10 Gbps 的 64 B 是 14.88 Mpps，1500 B 只有 0.813 Mpps，差 18 倍。

再给一个「队列能撑多久」的公式，后面每一节都会用到：

```
ring 能撑的时间 = 描述符数 / pps
256 / 2.08e6 = 123 µs        （10 Gbps，600 B 平均包）
256 / 14.88e6 = 17.2 µs      （10 Gbps，64 B 小包）
```

**这就是为什么小包流量一冲就丢：256 个描述符只够 CPU 离开 17 µs。** 一次内存回收、一次调度延迟、一次 VM exit 都可能超过 17 µs。大包给的是 123 µs 甚至 315 µs，同样的 ring 就从容得多。

---

## 32.3 第①道门：NIC、DMA、描述符环与 RSS

网卡收包的物理动作只有一句：**NIC 把线上的字节 DMA 进内存里一个预先挂好的缓冲区，然后把描述符的「完成位」置 1。** CPU 不搬数据，CPU 只读描述符、看完成位、把缓冲区重新挂回去。

```
 driver 预挂 N 个 buffer                 NIC 收到包
 ┌────┬────┬────┬────┐                  │
 │desc│desc│desc│desc│  rx ring          ▼
 │ 0  │ 1  │ 2  │ 3  │  ──────►  DMA 写入 buf[1]，desc[1].DD = 1
 └────┴────┴────┴────┘                  │
      ▲                                  │
      └── poll 消费完 desc[1]，重新挂 buf ─┘
```

门①的容量由三个数字决定：描述符数（`ethtool -g`，典型 256–4096）、每描述符缓冲区大小（1–2 KB，MTU 1500 时一包一个）、以及 CPU 回来消费描述符的频率。DMA 带宽不是瓶颈（`base/14` 已算过）；瓶颈是 CPU 离开 ring 的时间。

**RSS 解决的是「哪个 CPU 来消费」，不是「一条流能不能并行」。** RSS 用四元组（源 IP、目的 IP、源端口、目的端口）做哈希，把包分到不同队列；每条队列有自己的 ring 和 MSI-X 中断。**同一条 TCP 流永远命中同一个队列**——所以 `iperf3 -P 1` 只能用一个核，`-P 4` 才能用四个核。这是 32.1 那场事故的第一层解释。

本机的情况（一台 2 核 KVM，virtio 网卡）：

```
$ ls /sys/class/net/eth0/queues
rx-0  tx-0
$ ethtool -c eth0
bash: ethtool: command not found
$ cat /proc/interrupts | grep virtio3
 34:   55858270          0  PCI-MSIX-0000:06:12.0   1-edge  virtio3-input.0
 35:          0   44017037  PCI-MSIX-0000:06:12.0   2-edge  virtio3-output.0
```

只有 `rx-0`，没有 `rx-1`；只有一条 `input.0`，中断全落在 CPU0 那一列。**这就是「单队列 + 单核」的实物证据**：没有 RSS 可分散，带宽上限就是 CPU0 的 pps 上限。`ethtool` 没装，所以 `ethtool -S` / `ethtool -c` 用不了；等价的计数在 `/sys/class/net/eth0/statistics/`，等价的队列信息在 `/sys/class/net/eth0/queues/`。

`ethtool -S` 的计数器名字随驱动不同，但语义只有几类：`rx_dropped` 是驱动层丢弃（缓冲不足、映射失败等），`rx_missed_errors` / `rx_fifo_errors` 是 NIC FIFO 满，`rx_no_buffer_count` 是驱动没有空闲 buffer 可挂（部分驱动），`rx_crc_errors` / `rx_length_errors` 是物理层/帧长错误。前三类指向门①，最后一类该查线缆/MTU。

本机的真实读数：

```
$ cat /proc/net/dev
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets ...
    lo: 12344378502 8344417    0    0    0     0          0         0 ...
  eth0: 195381098870 76074121    0 824299    0     0          0         0 ...

$ cat /sys/class/net/eth0/statistics/rx_dropped
824299
```

`eth0` 收了 76,074,121 个包，丢了 824,299 个，**1.07%**。这些丢包不会让连接立刻断，只会让 TCP 重传、让 pps 变得更贵——一个已经满了的门①，会把成本转嫁给门④。注意：virtio 的 `rx_dropped` 语义比物理网卡宽，可能包含驱动/host 侧的缓冲失败；要确认是 ring 满，最好同时看 `rx_missed_errors`（这里是 0）和 `softnet_stat`。**计数器的名字是线索，不是判决书。**

**中断合并**是门②的调参旋钮。`ethtool -c` 看三个值：

```
rx-usecs:   等多少微秒再发一次中断
rx-frames:  收多少个包就发一次中断
adaptive-rx: 让驱动按流量自动调上面两个
```

算一笔账：1 Mpps、`rx-frames=1` → 每秒 100 万次中断；每次中断进出 + EOI + MSI-X 约 1–2 µs，**光中断就吃掉 1–2 个核**。把 `rx-frames` 调到 64，中断降到 1.56 万次/秒，代价是最多等 64 个包（或 `rx-usecs` 微秒）才处理——**吞吐换延迟**。这也是 NAPI 存在的原因：高包量时，轮询比中断便宜一个数量级（§32.5）。

---

## 32.4 第②道门：硬中断只做三件事

很多人以为硬中断处理函数会「把包收进协议栈」。不是。现代网卡的硬中断处理函数只做三件事：

1. 读一下中断状态，确认是自己；
2. 把 NAPI 实例挂到当前 CPU 的 `poll_list`，并 raise `NET_RX_SOFTIRQ`；
3. 关掉这个队列的中断（或让后续中断直接返回），等 poll 完再开。

用伪代码表示（省略驱动细节）：

```c
static irqreturn_t nic_msix(int irq, void *data) {
    struct napi_struct *napi = data;
    if (napi_schedule_prep(napi)) {   /* 置 NAPI_STATE_SCHED */
        nic_disable_queue_irq(napi);  /* 多数驱动会关该队列中断 */
        __napi_schedule(napi);        /* 挂到 poll_list + raise 软中断 */
    }
    return IRQ_HANDLED;
}
```

真正的收包在 `NET_RX_SOFTIRQ` 里，由 `net_rx_action()` 调用驱动的 `poll()` 完成。软中断什么时候跑？**从硬中断返回时**，如果当前没有嵌套中断、没有更高优先级的 softirq、且 `need_resched` 没置位；否则推给 `ksoftirqd/N` 内核线程。`ksoftirqd` 是「软中断太多，不能在中断返回路径里一次做完」时的泄洪道。

`/proc/softirqs` 的真实输出（2 核）：

```
                    CPU0       CPU1
          HI:          0          0
       TIMER:   24916476   16731184
      NET_TX:    3244041    1235326
      NET_RX:   55048004   44513647
```

> 这里只保留前四行，是本机同一时刻的真实输出；`BLOCK`/`IRQ_POLL` 等与本篇无关，不展开。

读这一列要记住：**`NET_RX` 是软中断被调用的次数，不是包数。** 一次 `NET_RX` 可能处理 1 个包，也可能处理 64 个（见 §32.5 的 budget）。所以「NET_RX 5500 万」不能除以时间当 pps 用；要算 pps，看 `/proc/net/softnet_stat` 的 `processed`（每包一次）。

看前后差值的方法：

```
$ awk '/NET_RX/{print}' /proc/softirqs
      NET_RX:   55089346   44546495
$ # 跑完 100 MiB echo 后再读
$ awk '/NET_RX/{print}' /proc/softirqs
      NET_RX:   55101087   44556330
```

差值 CPU0 `+11,741`、CPU1 `+9,835`。这是 200 MiB 双向搬运换来的软中断调用次数——**只有两万多次，不是几十万次**，因为每次软中断都批量处理了一批包。批量化正是 NAPI 的全部价值。

`%si` 高说明 CPU 在软中断上花的时间多；软中断太多、超过 `MAX_SOFTIRQ_TIME`（2 ms）或需要调度时，才转给 `ksoftirqd/N`。本机一次 64 B UDP 冲击把聚合 `%si` 从 2.5% 推到 4.4%，而 `ksoftirqd` 的 `%CPU` 并不高——**`%si` 是「软中断这件事」花掉的 CPU，不是「ksoftirqd 这个进程」的 CPU，不要只盯着进程名。**

本机在一次 64 B UDP 小包冲击中，`top` 的聚合 `%si` 从 2.5% 涨到 4.4%：

```
%Cpu(s): 78.9 us, 18.6 sy,  0.0 ni,  0.0 id,  0.0 wa,  0.0 hi,  2.5 si,  0.0 st
%Cpu(s): 74.8 us, 20.4 sy,  0.0 ni,  0.0 id,  0.0 wa,  0.0 hi,  4.4 si,  0.0 st
```

同一时刻 `ps -eLo pid,psr,pcpu,comm | grep ksoftirq` 能看到两个 `ksoftirqd`，但它们的 `%CPU` 都不高——因为这次冲击的 pps 还不够把软中断逼出中断返回路径。**`%si` 不是「软中断线程的 CPU」，它是「软中断这件事花掉的 CPU」；不要只盯着 `ksoftirqd` 的进程名。**

---

## 32.5 第③道门：NAPI 的「关中断 → 轮询一批 → 没完就续」

NAPI 的状态机可以压成一句话：**来一个中断，关掉这个队列的中断，然后一次 poll 一批；这批没处理完，就把自己再挂回 poll_list，等下一轮软中断继续，直到处理完才重新开中断。**

`net_rx_action()` 的骨架（本机参数）：

```c
/* net/core/dev.c，省略锁与 RCU 细节 */
int budget = netdev_budget;                 /* 本机 600，默认 300 */
unsigned long time_limit = jiffies +
    usecs_to_jiffies(netdev_budget_usecs);  /* 本机 8000us，默认 2000us */

while (!list_empty(&sd->poll_list)) {
    struct napi_struct *napi = list_first_entry(&sd->poll_list, ...);
    int work = napi->poll(napi, weight);    /* weight = 64 */
    budget -= work;
    if (budget <= 0 || time_after(jiffies, time_limit)) {
        sd->time_squeeze++;                 /* 预算用完，还有活 */
        break;                              /* 下一轮软中断继续 */
    }
    if (work < weight)
        napi_complete_done(napi, work);     /* 活干完了，重新开中断 */
}
```

门③的四个旋钮（本机值 / 默认值）：`dev_weight` 64/64（一次 poll 最多处理多少包）、`netdev_budget` 600/300（一轮软中断总共处理多少包）、`netdev_budget_usecs` 8000/2000（一轮最多花多少微秒）、`netdev_max_backlog` 8192/1000（每 CPU 的 `input_pkt_queue` 深度，非 NAPI 路径/RPS 用）。

**为什么高包量时轮询优于中断？** 因为中断是「每包一次固定成本」，轮询是「每批一次固定成本」。1 Mpps 时，每包一次中断的固定成本（入口、保存寄存器、EOI、MSI-X、调度检查）约 1–2 µs，直接吃掉 1–2 个核；而一次 poll 处理 64 个包，固定成本被摊成 1/64。更关键的是**活锁**：如果中断率高于 CPU 的处理能力，CPU 会永远在「进中断→处理不完→再进中断」里打转，应用永远得不到调度。NAPI 用「关中断 + 预算」强行给 CPU 一个出口：预算用完就退出软中断，把 CPU 让给应用/调度器，剩下的下一轮再说。

代价是延迟：一个包可能要等当前这批 64 个处理完才被看到；低包量时，NAPI 还会做「中断 + 轮询」的混合，避免空轮询。`napi_defer_hard_irqs` 和 `gro_flush_timeout` 就是给低延迟场景的旋钮（本机都是 0）。

`/proc/net/softnet_stat` 是门③唯一的账本。每行一个 CPU，十六进制：

```
$ head -2 /proc/net/softnet_stat
04be3743 00000000 000021b4 00000000 ...
003b4237 00000000 00000002 00000000 ...
```

逐列解码（本机）：

```
cpu0 processed=79625720 dropped=0 time_squeeze=8629
cpu1  processed=3884877 dropped=0 time_squeeze=2
```

| 列（从 0 开始） | 字段 | 含义 |
| --- | --- | --- |
| 0 | `processed` | 这个 CPU 处理过的包数（**真正能当 pps 用的数**） |
| 1 | `dropped` | `input_pkt_queue` 满导致的丢包（门③的丢） |
| 2 | `time_squeeze` | 预算/时间用完但还有活（软中断被 CPU 卡住） |
| 3 | `cpu_collision` | 发送竞争（很少用） |
| 8 | `received_rps` | RPS 唤醒次数 |
| 9 | `flow_limit_count` | RFS 流表满 |

`time_squeeze` 8,629 次说明这台机器上「软中断预算用完」发生过很多次；`dropped=0` 说明 backlog 还没满（本机把 `netdev_max_backlog` 调到了 8192，默认 1000 时更容易看到 dropped 涨）。**`time_squeeze` 涨是「CPU 不够」，`dropped` 涨是「队列不够」——前者加核/分散 IRQ，后者加 backlog/加队列。**

用 Python 解码（mawk 没有 `strtonum`）：

```python
for i, line in enumerate(open('/proc/net/softnet_stat')):
    f = [int(x, 16) for x in line.split()]
    print(f"cpu{i} processed={f[0]} dropped={f[1]} time_squeeze={f[2]}")
```

---

## 32.6 第④道门：协议栈的固定开销，1 Mpps 要多少 cycle

从驱动 `poll()` 拿到 skb 开始，一个包要走过：

```
eth_type_trans
  → ip_rcv → ip_rcv_finish → 路由查找（dst_input）
  → tcp_v4_rcv → __inet_lookup_skb（查 ehash）
  → tcp_v4_do_rcv → tcp_rcv_established（快路径）
  → 序号检查 / 窗口更新 / ACK 生成
  → tcp_data_queue → sk_data_ready
```

UDP 短一截，但同样要 `udp_rcv → __udp4_lib_rcv → 查 socket → udp_queue_rcv_skb`。每一步都不大，但**每一步都要碰内存**：

| 开销项 | 量级（cycle） | 说明 |
| --- | --- | --- |
| `sk_buff` 分配/释放 | 150–300 | `napi_alloc_skb` 从 per-CPU 缓存批量取；`struct sk_buff` 约 224–256 B |
| 描述符清理 + 重新挂 buffer | 100–200 | 门①的「还债」 |
| 校验和 | 100–300 | 未卸载时；现代网卡通常卸载 |
| 路由查找（dst 缓存命中） | 50–100 | 未命中要走 FIB，贵一个数量级 |
| socket 哈希查找 | 50–100 | ehash 链表 + 四元组比较 |
| TCP 序号/窗口/ACK | 200–400 | 快路径也有分支和状态更新 |
| cache miss（skb 头 + 协议头） | 300–500 | 3–5 条 cache line，每条 ~100 cycle |
| **合计** | **约 1000–2500** | 约 0.4–1.0 µs @2.6 GHz |

这不是纸上估算。§32.10 的 UDP 实验里，`softnet_stat` 的 `processed` 增加了 182,460，`/proc/stat` 的 `NET_RX` 软中断时间增加了 190,184 µs：

```
190184 µs / 182460 包 = 1.04 µs/包
1.04 µs × 2.6 GHz ≈ 2704 cycle/包
```

同一台机器上另一次 423,770 包、434,443 µs 的实验得到 1.03 µs/包，两次一致。**这台 2 核 KVM 的收包栈成本约 1 µs/包（约 2700 cycle），这是后面所有账的锚点。** 注意：`/proc/stat` 的 softirq 字段在本机按微秒计；用「182,460 个包不可能在 2 核 5 秒里花掉 190 秒 CPU」就能反推出单位，别直接当 jiffies 读。

有了 1 µs/包，1 Mpps 的账就很简单：

```
1,000,000 pps × 1.04 µs = 1.04 CPU        （仅 NET_RX 软中断）
再加应用 recv/唤醒/拷贝 1–2 µs/包 → 2–3 CPU
10 Gbps @ 64 B   = 14.88 Mpps → 15–45 CPU
10 Gbps @ 600 B  = 2.08 Mpps  → 2–6 CPU
10 Gbps @ 1500 B = 0.813 Mpps → 1–2.5 CPU
```

**这就是「为什么 10 G 小包必须多核/旁路」的全部算术。** 不是网卡不行，是每个包都要让 CPU 碰一次 skb；`base/14` 的总线账说的是「CPU 碰数据贵」，这里说的是「CPU 碰包头也贵」。GRO（Generic Receive Offload）能把同一流的多个包合并成一个大的 skb 再上交，把 64 B 小包的有效 pps 降下来——但 GRO 只对批量流有效，对海量短连接/小请求无效。

`sk_buff` 本身不是「数据缓冲区」，而是一个**描述符**：链指针、所属 socket、线性区/分片长度、各层头偏移、指向真正 buffer 的 `head/data/tail/end`，外加校验和、时间戳、VLAN 等标记。一个 64 B 的包也要分配一个 224–256 B 的 `sk_buff`，再从 slab 拿一块数据 buffer。**包越小，描述符/有效载荷比越难看**：64 B 包 + 64 B 头 + 256 B skb ≈ 384 B 的内存流量换 64 B 用户数据。这不是浪费，这是「通用栈必须能处理任意包」的代价；DPDK/XDP 之所以快，就是把「通用」换成「专用」，把 skb 的分配/释放整条路径砍掉。

---

## 32.7 第⑤道门：socket 缓冲区，背压从这里开始

协议栈处理完，包被挂到 socket 的接收队列 `sk_receive_queue`，同时记账 `sk_rmem_alloc += skb->truesize`。这个队列的上限是 `sk_rcvbuf`，它由三个旋钮共同决定：

```
$ cat /proc/sys/net/core/rmem_max
33554432
$ cat /proc/sys/net/core/rmem_default
262144
$ cat /proc/sys/net/ipv4/tcp_rmem
4096    87380   33554432
$ cat /proc/sys/net/ipv4/tcp_wmem
4096    65536   33554432
```

`tcp_rmem` 的三个值是 min/default/max，本机 4 KiB / 85 KiB / 32 MiB；`rmem_max` 32 MiB 限制显式 `SO_RCVBUF`，`rmem_default` 256 KiB 是未设置时的默认；`tcp_moderate_rcvbuf=1` 开自动调优，`tcp_adv_win_scale=1` 表示窗口里扣一半给协议开销。

`SO_RCVBUF` 设置的值会被内核**翻倍**（一份数据、一份开销），再被 `rmem_max` 截断；`tcp_moderate_rcvbuf=1` 时，内核会根据消费速度自动把窗口从 85 KiB 往上推，直到 `tcp_rmem[2]`。`tcp_adv_win_scale=1` 还要再扣掉约一半给 skb 开销，所以 32 MiB 的 `rmem_max` 实际广告窗口大约 16 MiB——**你看到的「32 MB 缓冲区」不是「32 MB 可用窗口」。**

BDP 的账：一条流的吞吐上限 ≈ 接收窗口 / RTT。10 Gbps × 50 ms = **62.5 MB** 的管道；`tcp_rmem[2]` 32 MiB 扣掉一半开销后约 16 MiB，单流只能跑到 16 MiB / 50 ms = **2.56 Gbps**。同样 16 MiB 窗口，RTT 10 ms 时能跑 12.8 Gbps，RTT 100 ms 时只剩 1.28 Gbps——**带宽没变、窗口没变，只是 RTT 变了。** 这就是「带宽上不去但 CPU 不满」的第一嫌疑人。

缓冲区满了会怎样？分两种协议：

- **TCP：关窗口。** `tcp_select_window()` 把可用空间算成窗口字段；窗口到 0，发送方就停（零窗口探测）。这是 TCP 唯一的、诚实的背压：**接收方用「我不再收」逼发送方慢下来。** 应用不读，`Recv-Q` 就一直挂着，连接进入 `CLOSE_WAIT` 后也不会消失（§32.12）。
- **UDP：丢。** 没有窗口，没有重传；`sk_rmem_alloc > sk_rcvbuf` 时直接 `atomic_inc(&sk->sk_drops)`，并给 `UDP_MIB_RCVBUFERRORS` 加一。丢包发生在门⑤，不在门①。

本机的 `ss -m` 真实输出（100 MiB echo 传输完成、连接仍保持时）：

```
ESTAB 0 0 127.0.0.1:9090 127.0.0.1:29406
    skmem:(r0,rb5071372,t0,tb3939840,f0,w0,o0,bl0,d6)
    bbr wscale:10,10 rto:204 rtt:0.973/1.599
    mss:65483 cwnd:17 bytes_sent:104857918 bytes_retrans:318
    bytes_acked:104857600 bytes_received:104857600
    segs_out:3745 segs_in:3287 data_segs_out:3207 data_segs_in:2011
    send 9.15Gbps lastsnd:3400 lastrcv:3400 lastack:3396
```

逐个字段读：

| 字段 | 含义 | 本例 |
| --- | --- | --- |
| `r` | `sk_rmem_alloc`，已排队未读的字节 | 0（应用已经读空） |
| `rb` | `sk_rcvbuf`，接收缓冲上限 | 5,071,372（自动调优后的奇数） |
| `t` / `tb` | 发送队列已用/上限 | 0 / 3,939,840 |
| `f` | `sk_forward_alloc`，预分配额度 | 0 |
| `w` | `sk_wmem_queued` | 0 |
| `bl` | `sk_backlog.len` | 0 |
| `d` | `sk_drops`，这个 socket 上的丢包 | 6 |
| `cwnd` | 拥塞窗口（单位 MSS） | 17 × 65,483 ≈ 1.1 MB |
| `rtt` | 平滑 RTT / 方差（ms） | 0.973 / 1.599 |

`r0` 不代表「没有队列」，只代表「应用读得够快，采样时队列是空的」。要抓队列满的瞬间，得在传输中采样。§32.10 的 UDP 实验会给你一张 `r33152,rb32768,d94421` 的现场图——那才是门⑤真的满了。

**背压从这里开始，也从这里结束。** 应用读得慢 → `r` 涨 → 窗口缩 → 发送方被限制 → 吞吐下降；这不是故障，是 TCP 在用延迟换不丢包。你唯一能做的两件事是：让应用读得更快（批量 `recvmsg`、多线程、避免在收包线程里做重活），或者把缓冲区调大（用内存换抖动，但会抬高延迟和内存占用）。**加缓冲区不是修丢包，是把丢包改成排队；队列迟早还是要被消费掉。**

---

## 32.8 第⑥道门：`sk_data_ready` 到进程被调度，这一跳决定 P99

数据进队列后，协议栈调用 `sk->sk_data_ready(sk)`。这个函数指针在 `sock_init_data()` 里被设成 `sock_def_readable`：

```c
void sock_def_readable(struct sock *sk) {
    struct socket_wq *wq;
    rcu_read_lock();
    wq = rcu_dereference(sk->sk_wq);
    if (skwq_has_sleeper(wq))
        wake_up_interruptible_sync_poll(&wq->wait,
            EPOLLIN | EPOLLRDNORM | EPOLLRDBAND);
    sk_wake_async(sk, SOCK_WAKE_WAITD, POLL_IN);
    rcu_read_unlock();
}
```

如果进程用 epoll，`sk_wq` 上挂的不是进程，而是 epoll 的等待项，回调是 `ep_poll_callback`：它把对应的 `epitem` 挂进 epoll 的就绪链表 `rdllist`，再 `wake_up(&ep->wq)` 叫醒正在 `epoll_wait` 的进程。整个过程不碰包数据，只碰等待队列和就绪链表——**所以这一跳的成本与包大小无关，只与「有多少进程在等、CPU 有多忙」有关。**

于是完整的一跳是：

```
软中断收包 → tcp_data_queue → sk_data_ready
  → sock_def_readable → ep_poll_callback → 就绪链表
  → wake_up(epoll_wait 的等待队列) → 调度器选中进程
  → epoll_wait 返回 → read()/recvmsg() → copy_to_user
```

这一跳的每一段都有固定成本：

| 段 | 量级 | 受什么影响 |
| --- | --- | --- |
| 硬中断入口/出口 | 1–5 µs | 虚拟化下更贵（VM exit / MSI-X 注入） |
| NAPI poll 一批 | 10–100 µs | 批量大小、GRO |
| 协议栈每包 | 约 1 µs | §32.6 |
| `sk_data_ready` + epoll 回调 | 0.5–2 µs | 等待队列长度、是否 ET |
| 调度器唤醒 + 上下文切换 | 1–10 µs（空闲） | 负载高时可到 100 µs–10 ms |
| `copy_to_user` | 约 0.1 µs/KB | 内存带宽、页是否在 cache |
| 系统调用返回 | 0.3–1 µs | Spectre 缓解、seccomp |

**平均值由吞吐决定，P99 由这一跳决定。** 协议栈再快，如果进程在 runqueue 里等了 2 ms，P99 就是 2 ms。§32.10 的 loopback 实验里 `ss -ti` 显示 `rtt:0.973/1.599`——一条没有物理线缆的 loopback，RTT 接近 1 ms，就是因为两端进程在同机 2 核上互相抢 CPU、还要被别的负载打断。**先看 `rtt` 和 runqueue，再怀疑协议栈。**

排查这一跳：`perf sched latency` 看每个任务的调度延迟分布，`bpftrace` 挂 `sched_wakeup` 量「唤醒→运行」，`ss -ti` 看 `rcv_rtt/rcv_space`，`/proc/<pid>/schedstat` 看 runqueue 等待时间。低延迟场景还有两个反向旋钮：`SO_BUSY_POLL`（socket 上忙轮询，省掉唤醒，代价是烧 CPU）和 `net.core.busy_poll`（全局）。**它们都是用 CPU 换 P99，和 NAPI 的「用延迟换吞吐」正好相反——网络调优没有免费的方向，只有你愿意付哪种钱。**

---

## 32.9 零拷贝：省掉的是「内核→用户」那次 CPU 拷贝

先把词说清楚：**零拷贝不是「数据不动」，是「CPU 不复制」。** DMA 仍然要把数据从网卡搬到内存，内存仍然要占；省掉的只是 `copy_to_user` / `copy_from_user` 这条 CPU 指令流。

一次普通的「读文件→发 socket」：

```
磁盘/页缓存 ──copy_to_user──► 用户缓冲区 ──copy_from_user──► socket 发送队列 ──DMA──► 网卡
                 ①CPU 拷贝                      ②CPU 拷贝              ③设备搬运
```

`sendfile(out_fd, in_fd, ...)` 把 ① 和 ② 一起省掉：页缓存里的页直接以引用/片段的形式挂到 skb 上，CPU 只搬描述符，不搬字节。`splice` 用 pipe 做中转，也是页引用传递。`mmap` + `write` 只省掉「读」的那次拷贝，`write` 仍然要把用户映射里的字节复制进 socket 缓冲区。`MSG_ZEROCOPY` 则是把用户缓冲区「钉住」（pin），让 skb 直接引用用户页，省掉发送方向的拷贝。

| 路径 | CPU 数据拷贝 | syscall 次数（每块） | 内核→NIC | 能否配 TLS | 适用 |
| --- | --- | --- | --- | --- | --- |
| `read` + `write` | 2 次（页缓存→用户，用户→skb） | 2 | DMA | 可以（用户态加密） | 通用 |
| `mmap` + `write` | 1 次（用户映射→skb） | 2 | DMA | 可以 | 文件发送 |
| `sendfile` | 0 次（页引用） | 1 | DMA | 明文可以；TLS 需 kTLS | 静态文件、反向代理 |
| `splice` | 0 次（页引用） | 2（file→pipe→socket） | DMA | 明文可以；TLS 需 kTLS | 代理转发 |
| `MSG_ZEROCOPY` | 0 次（pin 用户页） | 1 | DMA | 可以（先加密到用户缓冲区） | 大块发送 |
| `kTLS` + `sendfile` | 0 次数据拷贝，内核加密 | 1 | DMA | 可以 | TLS 静态文件 |

几个容易踩的点：

- **TLS 会杀死 `sendfile`。** 用户态 TLS 必须把明文读到用户空间、加密、再写回内核；`sendfile` 只能引用页缓存里的明文，没法在中间插一次加密。要让 `sendfile` 和 TLS 共存，得用 kTLS（内核 TLS，`setsockopt(TLS_TX)`），把加密下沉到内核；但 kTLS 只管 record 层，握手仍在用户态。
- **`MSG_ZEROCOPY` 不是所有写都赚。** 它要 pin 用户页、走错误队列回收完成通知；小于约 10 KB 的写，pin/通知的成本超过拷贝本身。它只适用于大块、长连接的发送。
- **`splice` 的 pipe 有容量。** 默认 pipe 容量 64 KB，大文件要循环；`fcntl(F_SETPIPE_SZ)` 可以调大，但有上限。
- **接收方向的零拷贝更少。** 普通 `read()` 一定要 `copy_to_user`；要避免它，得用 `AF_PACKET` 的 `TPACKET_V3` ring、`io_uring` 的 provided buffers，或者把协议栈整个搬到用户态（DPDK/XDP）。这也是「零拷贝」在收包侧比发送侧难的原因。

算一笔拷贝的账：`copy_to_user` 的有效带宽约 10 GB/s，1 MB 约 100 µs；10 Gbps 线速下 1 MB 每 800 µs 到达一次，**仅这一次拷贝就占 12.5% 的一个核**。如果包更小、拷贝次数更多，比例还会更差。`base/37` 会用 `strace -c` 和 `perf` 把四种实现的 syscall/拷贝次数摆在同一张表上，这里只记住结论：**零拷贝省的是 CPU 的指令流，不是延迟；它让同样的核数能扛更多字节，但不改变「每包仍有固定开销」这条规律。**

---

## 32.10 实测：2 核 KVM 上的六组输出

> 环境：2 vCPU KVM（AMD EPYC 7K62，约 2.6 GHz），Linux 6.12，virtio 网卡，`eth0` 只有一个 `rx-0` 队列，`ethtool` 未安装。测量时同机还有其它负载（`load average` 9–12），所以**绝对吞吐偏低，只看差值与计数方向**；每包成本用「计数 ÷ CPU 时间」交叉算，不受绝对吞吐影响。

### ① 自写 echo server + client，100 MiB

服务端只做一件事：`read()` 多少，`write()` 回多少；客户端发 256 KiB、读回 256 KiB，重复到 100 MiB。两端分别绑在 CPU0/CPU1。

```c
/* echo_srv.c：单连接 echo，收满 64 KiB 就回写 */
#include <arpa/inet.h>
#include <sys/socket.h>
#include <unistd.h>
int main(void) {
    int l = socket(AF_INET, SOCK_STREAM, 0), one = 1;
    setsockopt(l, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    struct sockaddr_in a = {.sin_family = AF_INET, .sin_port = htons(9090),
                            .sin_addr.s_addr = htonl(INADDR_LOOPBACK)};
    bind(l, (struct sockaddr *)&a, sizeof a);
    listen(l, 8);
    int c = accept(l, 0, 0);
    static char b[65536];
    for (ssize_t n; (n = read(c, b, sizeof b)) > 0; ) {
        for (ssize_t o = 0; o < n; ) o += write(c, b + o, n - o);
    }
    sleep(8);                 /* 保持连接，便于 ss -m 观察 */
    close(c); close(l);
    return 0;
}
```

客户端用同样的 `socket`/`connect`，每轮 `write` 256 KiB、再 `read` 回 256 KiB，用 `clock_gettime(CLOCK_MONOTONIC)` 计时，共 400 轮；代码结构与服务端对称，不再重复贴。

真实输出：

```
client: payload=100.0 MiB, echoed=200.0 MiB, wall=0.617 s
client: one-way吞吐=162.0 MiB/s  (1.36 Gbit/s), 双向合计=324.1 MiB/s
```

162 MiB/s 看起来远低于 loopback 的能力，但这不是带宽上限：客户端是「发 256 KiB→等回 256 KiB」的停等模式，400 个来回，0.617 s / 400 ≈ **1.54 ms/来回**，`ss -ti` 的 `rtt:0.973/1.599` 正好对上。**它测的是「RTT + 调度」的上限，不是网卡的带宽上限。** 这也解释了为什么把应用改成流水线/多连接，吞吐会立刻上去。

### ② `/proc/softirqs` 前后差值

```
传输前：
      NET_RX:   55089346   44546495
传输后：
      NET_RX:   55101087   44556330
差值：CPU0 +11,741，CPU1 +9,835
```

200 MiB 双向（约 6,400 个数据段 + ACK）只触发了约 2.1 万次 `NET_RX` 软中断——**每次软中断平均处理了十几个包**，这就是 NAPI 批量的直接证据。

### ③ `/proc/net/softnet_stat` 前后

```
cpu0 processed=79625720 dropped=0 time_squeeze=8629
cpu1 processed=3884877  dropped=0 time_squeeze=2
传输后：
cpu0 processed=79639461 dropped=0 time_squeeze=8630
cpu1 processed=3888073  dropped=0 time_squeeze=2
差值：cpu0 +13,741 / +0 / +1；cpu1 +3,196 / +0 / +0
```

`dropped=0` 说明门③的 backlog 没有满；`time_squeeze` 只涨了 1，说明这一轮软中断基本能在预算内做完。**这次实验的瓶颈不在这里**——所以如果只盯着 `softnet_stat`，会误判。

### ④ `ss -s` 与 `ss -m`

```
$ ss -s
Total: 228
TCP:   133 (estab 92, closed 31, orphaned 1, timewait 31)
$ ss -tmi '( sport = :9090 or dport = :9090 )' | head -3
ESTAB 0 0 127.0.0.1:9090 127.0.0.1:29406
    skmem:(r0,rb5071372,t0,tb3939840,f0,w0,o0,bl0,d6) ... rtt:0.973/1.599
```

`estab 92` 里包含本机测试连接；`timewait 31` 是正常关闭留下的。`ss -s` 只是总量，定位要 `ss -tan` / `ss -tmi` 看单个连接。`r0` 是采样时接收队列空，`rb` 5,071,372 是自动调优后的接收缓冲；完整字段解释见 §32.7。**loopback 也有约 1 ms RTT，说明这一跳的延迟主要来自调度，不是线缆。**

### ⑤ `tc qdisc` 限速：带宽上不去，CPU 却几乎空闲

先加一个 50 Mbit/s 的 token bucket（`burst` 必须大于一个 MSS，否则大包会被 tbf 直接丢掉）：

```
$ tc qdisc add dev lo root tbf rate 50mbit burst 512k latency 100ms
$ tc -s qdisc show dev lo
qdisc tbf 8005: root refcnt 2 rate 50Mbit burst 512Kb lat 100ms
 Sent 42679985 bytes 2478 pkt (dropped 0, overlimits 4214 requeues 0)
$ taskset -c 1 ./echo_cli 9094 20 1
client: payload=20.0 MiB, echoed=40.0 MiB, wall=6.732 s
client: one-way吞吐=3.0 MiB/s  (0.02 Gbit/s), 双向合计=5.9 MiB/s
real 0m7.741s  user 0m0.004s  sys 0m0.015s
```

5.9 MiB/s ≈ 49.5 Mbit/s，正好是 qdisc 的 50 Mbit/s；7.7 秒里进程只花了 0.019 秒 CPU（0.25%）。**这就是「带宽上不去但 CPU 不满」的标准现场：包在 qdisc 的队列里排队，不在你的核上。** `overlimits 4214` 是 tbf 把包延后的次数；`dropped 0` 说明队列还没溢出，只是在限速。`Sent` 是 qdisc 的总计数，包含同机其它 loopback 流量；这一段只看速率与 CPU 两列。

再把 qdisc 换成 5 ms 延迟：

```
$ tc qdisc add dev lo root netem delay 5ms
$ ./echo_cli 9095 20 1
client: payload=20.0 MiB, echoed=40.0 MiB, wall=1.153 s
client: one-way吞吐=17.3 MiB/s  (0.15 Gbit/s), 双向合计=34.7 MiB/s
```

5 ms 单向延迟把 20 MiB 的停等 echo 从「无 qdisc 的约 160 MiB/s」压到 17.3 MiB/s，CPU 依旧闲着。**同一个应用、同一台机器，只改了一个 qdisc，吞吐差 9 倍——这就是 §32.7 的 BDP 和 §32.8 的「这一跳」在起作用。**

### ⑥ 门⑤满了：UDP 接收缓冲区的现场

让接收端把 `SO_RCVBUF` 设成 16 KiB，然后**故意 3 秒不读**；发送端用 `sendmmsg` 每批 64 个 64 B 包猛发 2 秒。

```
$ taskset -c 0 ./udp_sink2 9097 16384 3 &
sink: rcvbuf=32768 sleep_first=3
$ taskset -c 1 ./udp_blast 9097 2
blast: 2.011 s, sent=182400 pkt, 90692 pkt/s, 5.5 MiB/s
$ ss -u -m -a '( sport = :9097 )'
UNCONN 33152 0 127.0.0.1:9097 0.0.0.0:*
    skmem:(r33152,rb32768,t0,tb262144,f0,w0,o0,bl0,d94421)
$ grep '^Udp:' /proc/net/snmp
Udp: ... InErrors 183173 ... RcvbufErrors 183173 ...
$ # softnet_stat 同一窗口
cpu0 processed +5025 dropped +0
cpu1 processed +182460 dropped +0
```

发送端发了 182,400 个包，接收端只读到 **37 个**；`RcvbufErrors` 增加了 182,363，`sk_drops` 采样时已经 94,421，`Recv-Q` 涨到 33,152（`rb` 32,768），而 `softnet_stat` 的 `dropped` 是 **0**。**包不是被网卡丢的，也不是被 backlog 丢的，是在门⑤的 socket 队列入口被丢的。** 如果只看 `softnet_stat`，你会以为一切正常；这就是「计数器 → 门」这张表的价值。

把六组实验压成一张「哪个计数动，查哪道门」：

| 观测到的现象 | 指向的门 | 下一步 |
| --- | --- | --- |
| `rx_dropped` / `rx_missed_errors` 涨 | ① NIC ring | 加 ring、加队列、合并中断、查 CPU 是否离开太久 |
| `%si` 单核 100%，`time_squeeze` 涨 | ③ NAPI 预算 | 分散 IRQ、加队列、GRO、加核 |
| `softnet_stat` 的 `dropped` 涨 | ③ backlog | 加 `netdev_max_backlog`、RPS、修上层消费 |
| `ss -m` 的 `r` 接近 `rb`、TCP 零窗口 | ⑤ socket 接收队列 | 应用加速读、批量读、调 `tcp_rmem`/`rmem_max` |
| `Udp RcvbufErrors` / `d` 涨 | ⑤ socket 接收队列 | 同上；UDP 还要考虑应用丢包策略 |
| `rtt` 高、runqueue 长、`perf sched` 延迟大 | ⑥ 唤醒/调度 | 绑核、降负载、busy poll、批处理 |

---

## 32.11 三列账：承诺、代价、崩点

| 维度 | 承诺了什么 | 为此付了什么 | 从哪里开始失效 |
| --- | --- | --- | --- |
| 可靠、有序的字节流 | 丢包会重传，乱序会重排，应用看到的是连续流 | 序号/ACK/重传/接收队列；每包约 1 µs 的栈成本 | 软中断打满单核：包在门③排队，重传把成本再放大 |
| 流量控制 | 接收方用窗口告诉发送方「我还能收多少」 | 窗口更新、零窗口探测、`sk_rcvbuf` 内存 | 应用不读 → 窗口 0 → 吞吐塌；加大缓冲只是把丢包换成延迟 |
| 中断 + NAPI | 高包量时用轮询摊薄中断成本，低包量时用中断省 CPU | 关中断的延迟、budget 的公平性、`time_squeeze` | IRQ 集中在一核 / ring 太小：`rx_dropped` 涨，包在门①就没了 |
| socket 缓冲区 | 吸收突发，让发送方和接收方解耦 | `rmem_max` × 连接数的内存；队列延迟 | 缓冲区满：TCP 零窗口（背压）、UDP 直接丢（门⑤） |
| 零拷贝 | 省掉内核↔用户的一次 CPU 拷贝 | 页 pin、pipe、kTLS 的复杂度；小包不划算 | TLS 用户态加密、`sendfile` 不可用、页回收/ pin 冲突 |
| 端口复用 | 用四元组区分连接，理论上 6 万+ 端口可用 | 主动关闭方要等 2MSL（TIME_WAIT） | 新建连接速率 > 端口数/60s：端口耗尽，`connect` 失败 |
| 唤醒进程 | 数据到了就叫醒应用，延迟低 | 上下文切换、调度延迟、cache 污染 | runqueue 长/CPU 饱和：P99 从 µs 级掉到 ms 级 |

一句话收束：**收包路径买到的是「可靠、有序、可背压的字节流」，付出的是「每包固定成本 + 多层队列 + 一次唤醒」；崩点全部落在「某一层队列满了」或「某一层只有一条 CPU」。**

---

## 32.12 踩坑清单：现象 → 根因 → 定位 → 修

| 现象 | 根因 | 定位 | 修 | 防复发 |
| --- | --- | --- | --- | --- |
| 一个核 `%si` 100%，其它核闲 | 单队列/IRQ 集中；RSS 对单流无效（§32.3） | `top` 的 `si`；`/proc/interrupts` 看中断落在哪列；`/sys/class/net/*/queues` | 多队列 + `irqbalance`/`smp_affinity`；单队列用 RPS；应用多连接/`SO_REUSEPORT` | 监控 per-CPU `si`；上 10G 前先确认队列数 |
| `rx_dropped` / `rx_missed_errors` 持续涨 | ring 太小或 CPU 离开 ring 太久（§32.3） | `/sys/class/net/eth0/statistics/`；`ethtool -S`；`/proc/net/softnet_stat` | 加大 ring；合并中断；加队列；查 `time_squeeze` | 按 `pps × 中断延迟` 算 ring 深度；小包流量单独压测 |
| 带宽上不去，但 CPU 不满 | 窗口/BDP、qdisc、应用读得慢（§32.7/§32.8） | `ss -ti` 看 `cwnd/snd_wnd/rtt`；`tc -s qdisc` 看 backlog/overlimits；`ss -m` 看 `r/rb` | 调 `tcp_rmem`/`wmem_max`；应用批量读；检查 qdisc/中间设备 | 用 BDP 公式算窗口；压测要测单流和四流 |
| `softnet_stat` 的 `dropped` 涨 | `netdev_max_backlog` 满（§32.5） | 解码 `/proc/net/softnet_stat` 第 1 列 | 加大 backlog；RPS 分散；修上层消费速度 | 监控 dropped 速率；别把 backlog 当万能药 |
| `time_squeeze` 涨 | 软中断预算用完，CPU 被收包占满（§32.5） | 同上第 2 列；`%si` | 加 `netdev_budget`；分散 IRQ；加核；GRO | 以 `time_squeeze/s` 作为扩容信号 |
| `CLOSE_WAIT` 堆积 | 对端已 FIN，本端应用没 `close()`（§32.7） | `ss -tan state close-wait`；`lsof -p`；看 `Recv-Q` | 修代码：所有路径 `defer close`；加超时；检查连接池 | 监控 `CLOSE_WAIT` 数量；代码评审查 fd 生命周期 |
| `TIME_WAIT` 太多，端口不够 | 主动关闭方等 2MSL；新建连接速率高（§32.11） | `ss -s`、`ss -tan state time-wait`；`ip_local_port_range` | 长连接/连接池；扩大端口范围；谨慎开 `tcp_tw_reuse` | 按 `端口数/60s` 算新建连接上限；别用短连接打满 |
| 应用 `Recv-Q` 高但 CPU 低 | 应用读得慢/锁/同步日志（§32.7） | `ss -m`、`strace -c`、`perf` | 批量 `recvmmsg`；收包线程不做重活；多线程/多队列 | 压测时看 `Recv-Q` 曲线，不只看 QPS |

`CLOSE_WAIT` 这一行给一个真实现场：服务端 `accept()` 后既不读也不关，客户端发 6 字节就 `close()`：

```
$ ss -tan state close-wait
Recv-Q Send-Q Local Address:Port Peer Address:Port
6      0          127.0.0.1:9099    127.0.0.1:24998
```

`Recv-Q 6` 就是那 6 个字节还没被读；`CLOSE_WAIT` 不是「内核没关」，是「应用没关」。**看到 `CLOSE_WAIT` 堆积，先查代码里的 `close()`，不要先调内核参数。**

---

## 32.13 一页速记

```
【一句话】收包穿六道门；带宽上不去 = 某道门的队列满了，或某道门只有一条 CPU。

【六道门】
  ① NIC ring     256–4096 desc；256 desc @ 14.88Mpps = 17µs
  ② 硬中断       只做 napi_schedule；合并 rx-usecs / rx-frames
  ③ NAPI poll    weight 64 / budget 600 / 8ms；time_squeeze、dropped
  ④ 协议栈       约 1µs/包 ≈ 2700 cycle；1Mpps ≈ 1 核
  ⑤ socket 队列  rmem_max 32MB；TCP 关窗口，UDP 丢（RcvbufErrors）
  ⑥ 唤醒         sk_data_ready → epoll → 调度；P99 在这里

【三把尺子】
  pps 而不是 bps：10G @64B = 14.88Mpps，@1500B = 0.813Mpps
  BDP = 带宽 × RTT：10G × 50ms = 62.5MB；32MB 窗口只能跑 5.1Gbps
  计数器 → 门：rx_dropped→①，softnet dropped→③，ss d→⑤

【三个不要】
  不要用加大 socket 缓冲区修丢包（把丢换成延迟）
  不要把 softnet dropped 和 rx_dropped 混为一谈
  不要用 sendfile 配用户态 TLS（除非 kTLS）
```

---

## 32.14 自检 3 题

**Q1.** 一条 10 Gbps 链路，平均包长 600 B，单流只能跑 3.1 Gbps，`top` 显示一个核 `%si` 100%。请用「六道门 + pps」解释，并给出至少三条修复方向。

<details><summary>答案</summary>

3.1 Gbps / (8 × 600 B) ≈ **64.6 万 pps**；10 Gbps 同包长 ≈ **208 万 pps**。按本机实测 1 µs/包，64.6 万 pps 约 0.65 核，加上协议栈/应用开销正好把一个核吃到 100%；208 万 pps 需要 2–3 个核。

单流只命中一个 RSS 队列（四元组哈希），而这个队列的中断又全落在一个 CPU 上，所以门③（NAPI poll）和门④（协议栈）只有一条 CPU 可用。`%si` 100% 就是门④满了。

修复方向（任选三条）：

1. **多流/多连接**：`SO_REUSEPORT` + 多 worker，让 RSS 把不同流分到不同队列；单流本身无法靠 RSS 并行。
2. **分散 IRQ**：`/proc/irq/<n>/smp_affinity_list` 或 `irqbalance`；单队列网卡用 RPS（`/sys/class/net/eth0/queues/rx-0/rps_cpus`）把软中断分到其它核。
3. **加队列**：`ethtool -L eth0 combined N`（需驱动/硬件支持；virtio 需多队列配置）。
4. **减小每包成本**：确认 GRO/校验和卸载开启；大包场景合并中断（`rx-frames`/`rx-usecs`）；应用批量 `recvmsg`。
5. **加 ring/backlog**：如果 `rx_dropped` 或 `softnet dropped` 同时在涨，先把门①/③的队列加深，避免丢包放大重传。
</details>

**Q2.** 一台 UDP 服务 `ss -m` 显示 `r` 接近 `rb`、`d` 很大，但 `/proc/net/softnet_stat` 的 `dropped` 是 0，`rx_dropped` 也不涨。包是在哪道门丢的？TCP 在同样情况下会怎样？

<details><summary>答案</summary>

门⑤，socket 接收队列。`r`（`sk_rmem_alloc`）接近 `rb`（`sk_rcvbuf`）说明应用没及时读；UDP 没有流量控制，队列满就 `atomic_inc(&sk->sk_drops)` 并给 `UDP_MIB_RCVBUFERRORS` 加一（`/proc/net/snmp` 的 `RcvbufErrors`）。

`softnet_stat` 的 `dropped` 是门③的 per-CPU backlog 丢包；`rx_dropped` 是门①的网卡/驱动丢包。两者都是 0，说明包已经顺利穿过 ①–④，死在门⑤。

TCP 在同样情况下**通常不丢**：它会缩小/关闭接收窗口（`tcp_select_window`），发送方收到零窗口后停止发送，进入零窗口探测。应用不读，`Recv-Q` 会一直挂着，连接可能停在 `CLOSE_WAIT`；只有在极端内存压力下才会 prune/丢包并计入 `sk_drops`。所以「UDP 丢包」和「TCP 变慢」是同一道门满了的两种表现。
</details>

**Q3.** 为什么 `sendfile` 配用户态 TLS 不能零拷贝？`MSG_ZEROCOPY` 为什么可以配 TLS？如果要让 `sendfile` 和 TLS 共存，需要什么条件？

<details><summary>答案</summary>

用户态 TLS 的加密发生在用户空间：必须先把明文从页缓存读到用户缓冲区，加密后再写回内核 socket。`sendfile` 只能把页缓存里的**明文页**直接引用给 skb，中间没有地方插入「用户态加密」这一步，所以 TLS 下它要么不可用，要么退化成「读+加密+写」。

`MSG_ZEROCOPY` 可以配 TLS，是因为它发送的是**已经加密好的用户缓冲区**：TLS 库把密文写进用户 buffer，`sendmsg(..., MSG_ZEROCOPY)` 把这个 buffer pin 住，skb 直接引用用户页，省掉「用户→内核」那次拷贝。但 pin/完成通知有成本，小写不划算。

让 `sendfile` 和 TLS 共存的条件是 **kTLS**：用 `setsockopt(TLS_TX)` 把 record 层的加密下沉到内核，`sendfile` 仍然引用页缓存，内核在发送路径上加密。代价是握手/密钥管理仍在用户态，且 kTLS 的算法/内核版本支持有限。
</details>

---

## 32.15 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| DMA、总线、MMIO：为什么「CPU 碰数据」贵（本篇的门①物理层） | → `base/14` |
| IO 模型全景：阻塞/非阻塞、epoll 的 ET/LT、io_uring（本篇的门⑥） | → `base/31` |
| 零拷贝四实现：`read/write`、`mmap`、`sendfile`、`splice` 的 `strace -c` 实测 | → `base/37` |
| TCP 建立与关闭：三次握手、状态机与 TIME_WAIT（本篇的门⑤/端口耗尽） | → `base/41` |
| TCP 可靠与拥塞：滑动窗口、接收窗口与拥塞窗口的博弈（本篇的 BDP） | → `base/42` |
| Go 高并发：netpoll、goroutine 与连接账（用户态视角的同一件事） | [go/32](../go/32-high-concurrency.md) |
| 全书总纲：四条主线与 56 篇清单 | [0-summary](0-summary.md) |
