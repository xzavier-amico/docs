# 51. 实例剖析：`tc netem` 注入 1% 丢包——CUBIC 与 BBR 的分道扬镳

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 网络：从一根网线到一张证书（实例篇） ｜ **编号**：51 / 56
> **本篇定位**：Part IV 的验伪出口，也是 42 篇最硬的一张证据。42 篇说「TCP 被接收窗口与拥塞窗口两个上限卡死」，本篇把第二个上限拎出来，在一条可控的坏路上跑两遍：同一条 100 Mbit 链路、同样 20–100 ms RTT、同样 1% 丢包，只改 `net.ipv4.tcp_congestion_control` 一行，CUBIC 掉到带宽的 9%–20%，BBR 稳在 93%。原因不是「BBR 写得快」，而是**两者对同一个丢包事件的解释完全不同**：一个是「路满了」的判决书，一个是「这一份运气不好」的噪声。读完的判据：再看到「网络时快时慢」，你会先量 `p`、`minrtt`、`cwnd` 三个数，而不是先换算法。
> **读法**：§51.1 先看现场与复现的数字落差（含来源标注）；§51.2–§51.3 把 `netem` 这个物件拆到「包在队列里排队」的层面——**不懂 qdisc 挂在哪，就会把 ACK 路径的丢包算成数据丢包**；§51.4 是四组实验（基线 / 1% 丢包 / 加 RTT / 只改一行）与一张「改前·改后」两列表，附本机原样输出；§51.5 用它推翻五条流行说法；§51.6 给选型、云厂商可选项与三条验证命令；§51.7–§51.10 收口。
> **不在本篇**：三次握手、四次挥手与 TIME_WAIT 的状态机 → `base/41`；重传、SACK、滑动窗口与拥塞控制的机制推导 → `base/42`；QUIC 在用户态重造拥塞控制 → `base/43`；完整请求链路上的逐跳归因 → `base/53`；`perf`/`strace`/`tcpdump` 的分层排障总表 → `base/54`。
---
## 51.1 引子：一条 100 Mbit 的跨境专线，单流只跑到 8 Mbit

一家做跨境订单同步的团队，租了一条 100 Mbit 的专线，机房到机房 RTT 约 40 ms，链路质量报告上写着「丢包 1%」。业务侧的现象是：**单条 TCP 连接同步一批 200 MB 的订单快照要 3 分多钟**，而同一台机器上开 20 条连接并行，总吞吐能到 60–70 Mbit。运维的第一反应是「单线程程序写得差」，开发把发送端改成多线程，连接数上去了，总时间却没降多少——因为瓶颈不在应用，在那条连接的拥塞窗口上。

后来有人把发送端的拥塞控制从 `cubic` 换成 `bbr`，同一个程序、同一条链路、同一个对端，**单流从 8 Mbit 涨到 70 Mbit**，快照同步时间从 3 分多钟降到 25 秒。

这个「8% → 70%」是工程界的经验口径（多个跨境/卫星链路的运维报告里反复出现，没有单一出处，属于**量级结论**：一个数量级，不是精确值）。本篇不拿它当证据，而是把它当假设，在同一台机器上复现。复现结果比现场更极端：

| 场景 | 带宽 | RTT | 丢包 `p` | CUBIC | BBR | 口径 |
| --- | --- | --- | --- | --- | --- | --- |
| 现场（跨境专线，运维经验值） | 100 Mbit | ≈40 ms | 1% | 8 Mbit（8%） | 70 Mbit（70%） | **推演/经验口径**，非单一出处 |
| 本篇复现 A（100 Mbit 整形 + veth） | 100 Mbit | 20 ms | 1% | 9.4–20.1 Mbit（9%–20%） | 92.5–93.8 Mbit（93%） | 本机实测，20 s / 12 s×3 |
| 本篇复现 B（同上，RTT 翻倍） | 100 Mbit | 40 ms | 1% | 3.7–4.1 Mbit（4%） | 90.4–90.7 Mbit（90%） | 本机实测，12 s×2 |
| 本篇复现 C（同上，RTT 再翻） | 100 Mbit | 100 ms | 1% | 3.0–4.7 Mbit（3%–5%） | 80.9–84.8 Mbit（81%–85%） | 本机实测，10–12 s |

三行复现给出两个必须同时记住的结论：

1. **方向完全一致**：1% 丢包不是「损失 1% 的吞吐」，而是让 CUBIC 掉 5–25 倍，BBR 只掉 0–19%；
2. **绝对数字不能照抄**：同一条 100 Mbit 链路，RTT 从 20 ms 拉到 100 ms，CUBIC 从 9–20 Mbit 掉到 3–4.7 Mbit，BBR 从 93 Mbit 掉到 81–85 Mbit。**能迁移的是比例关系与判据，不是某个 Mbps。**

为什么 40 ms 这一行 CUBIC 只有 4%，而 20 ms 这一行能到 9%–20%？这不是测量噪声，是 42 篇那条公式的直接后果：

```text
吞吐 ≈ (MSS / RTT) × (1.22 / √p)          ← Mathis 公式，只对丢包驱动的算法成立

MSS=1460, p=0.01:
  RTT=20 ms → 1460/0.020 × 12.2 = 890 kB/s ≈ 7.1 Mbit/s   （实测 9.4–20.1，同量级）
  RTT=40 ms → 1460/0.040 × 12.2 = 445 kB/s ≈ 3.6 Mbit/s   （实测 3.7–4.1，吻合）
  RTT=100ms → 1460/0.100 × 12.2 = 178 kB/s ≈ 1.4 Mbit/s   （实测 3.0–4.7，约 2–3×）
```

40 ms 那一行几乎逐位吻合，是因为 CUBIC 在高丢包下退化成「每次丢包砍窗口、再慢慢爬」的行为，与 Reno 的稳态公式同阶；20 ms 那一行公式偏低，是因为 20 s 的采样里 CUBIC 有相当一部分时间处在「刚丢完、窗口还没被砍到稳态」的阶段，平均值被抬高。**公式给的是量级与 `1/RTT`、`1/√p` 这两个比例，不是一个固定数字。**

本篇的唯一真问题因此可以一句话说完：

> **1% 丢包能让 CUBIC 吞吐塌到 1/10、BBR 几乎不掉，是因为 CUBIC 把丢包读成「路满了」，BBR 把丢包读成「链路噪声」；同一个物理事件，在两套解释里对应两种完全不同的动作。**

这不是修辞。§51.4 的 `ss -ti` 会把两种动作的后果直接打在同一屏上：CUBIC 的 `cwnd` 被钉在 19–22 个 MSS，BBR 的 `cwnd` 跟着 BDP 涨到 392–816 个 MSS。

---

## 51.2 ① 它是什么：在网卡出口队列上装一个「可编程的坏路」

`tc qdisc netem` 是 Linux 流量控制（traffic control）里的一个**排队规则**（qdisc，queueing discipline）。它做的事用一句话说完：

> **在每个要从这块网卡出去的包上，按你给的参数掷骰子：睡多久、丢不丢、重复不重复、打乱顺序、限不限速。**

它不是「模拟器」，不会改协议栈、不会改应用；它只是让**出口队列**这个本来只负责排队的环节，开始表现得像一条坏掉的链路。对 TCP 来说，它和真实链路没有区别：包在 qdisc 里被丢掉，发送方只能靠重传发现；包被延迟，RTT 就真的变长。

| 参数 | 物理含义 | 本次实验取值 | 对测量的影响 |
| --- | --- | --- | --- |
| `delay 10ms` | 每个包在 qdisc 里固定睡 10 ms | 单程 10/20/50 ms | 直接改 RTT；双向挂才是「往返」 |
| `jitter 2ms` | 延迟的随机抖动 | 本次省略 | 让 RTT 方差变大，影响 RTO 与 BBR 的 `minrtt` |
| `loss 1%` | 每个包独立以 1% 概率被丢 | 1%（另做 0.1%/3% 扫描） | 本次的自变量 `p` |
| `loss 1% 25%` | 第二数是相关性，丢包成串 | 本次省略 | 更接近真实链路的 burst loss |
| `duplicate 0.1%` | 复制包 | 本次省略 | 制造伪重传，考验 SACK |
| `reorder 25% 50%` | 25% 的包延迟 50%，造成乱序 | 本次省略 | 触发 dupACK / 伪快速重传 |
| `rate 100mbit` | 令牌桶限速，超出排队 | 100 Mbit | 给链路一个**已知容量**，才能算「占带宽百分比」 |
| `limit 1000` | 队列最多 1000 个包 | 默认 1000 | 1000×1500 B ≈ 1.5 MB，100 Mbit 下是 120 ms 的排队上限 |
| `corrupt 0.01%` | 随机改一个 bit | 本次省略 | 考验校验和与上层重传 |

**为什么用 `netem` 而不是找一条真的坏链路**：因为你没法在真实链路上把 `p` 从 0 调到 1%、再把 RTT 从 20 ms 调到 100 ms，同时保持其它变量不变。`netem` 把「带宽、RTT、丢包率」变成三个独立旋钮——这是**对照实验**的前提。42 篇已经用它量过「窗口 / RTT」这一半；本篇要量的是「算法 × 丢包率」这一半。

三个测量对象必须先定义清楚，否则后面所有数字都会串味：

- **goodput**：单位时间**被对端应用收到**的字节数。`iperf3` 的 `receiver` 行就是它。它排除了重传浪费；
- **cwnd**：发送方允许自己在途的段数。`ss -ti` 里的 `cwnd:`，单位是 MSS，不是字节；
- **retrans rate**：`bytes_retrans / bytes_sent`。它回答「这条连接为了送到这些字节，多发了多少」。

三者一起看，才能区分「链路真的只有这么宽」和「算法不敢发」。

---

## 51.3 ② 物理形态：qdisc 挂在哪，包在队列里经历什么

### 一张图：一个包从应用到网线的最后 100 微秒

```text
   应用 write()                                    对端
      │                                              ▲
      ▼                                              │
 ┌─────────┐   ┌──────────┐   ┌──────────┐   ┌──────────────┐
 │ socket  │──▶│   TCP    │──▶│    IP    │──▶│ egress qdisc │──▶ veth ──▶
 │ 缓冲区  │   │ cwnd /   │   │ 路由/分片 │   │   netem      │     (对端 netns)
 └─────────┘   │ pacing   │   └──────────┘   │              │
               └──────────┘                  │ ① loss 1%    │  ← 掷骰子：丢？
                                             │ ② duplicate  │
                                             │ ③ delay 10ms │  ← 每个包睡 10ms
                                             │ ④ rate 100M  │  ← 令牌桶，超了排队
                                             │ ⑤ limit 1000 │  ← 队列满则 tail-drop
                                             └──────────────┘
                                                    │
                                              排队延迟 = backlog / rate
```

关键点只有三个：

1. **qdisc 在 egress**。它管「从这块网卡出去」的包，不管进来的。所以「路径 1% 丢包」必须在**两端各挂一个** `netem`：`veth0` 管服务端→客户端的数据，`veth1` 管客户端→服务端的 ACK。只在一端挂，丢的是单向，端到端丢包率会落在 0% 与 1% 之间。
2. **`delay` 是固定延迟，排队延迟是另一笔账**。`netem` 的 `delay` 让每个包睡固定时长；而 `rate` 造成的积压体现在 `backlog` 上。`tc -s qdisc` 里的 `backlog 199b 3p` 就是「此刻还有 3 个包、199 字节在排队」。真实链路的 RTT = 传播延迟 + 排队延迟 + 处理延迟，`netem` 只伪造前两项。
3. **`limit` 是这个假链路的缓冲深度**。默认 1000 个包，MTU 1500 时约 1.5 MB。100 Mbit 下排队上限 120 ms——这正是「缓冲膨胀」的物理来源：链路没满，但队列很长，RTT 被自己撑大。

### 回环上注入 vs 真实链路丢包：四个必须说清的差异

很多教程直接在 `lo` 上 `tc qdisc add dev lo root netem loss 1%`，然后拿结果下结论。这样做能复现**比例关系**（42 篇就是这么做的），但有四个坑：

| 差异 | 回环（`lo`） | 真实链路 | 后果 |
| --- | --- | --- | --- |
| MTU / GSO | 65536，MSS 可到 65 KB | 1500，MSS 1448 | Mathis 公式里的 `MSS` 差 45 倍，绝对吞吐不可比；42 篇的 187 Mbit 与 2.04 Gbit 就是这样来的 |
| 带宽上限 | 无（跑的是 CPU 上限，约 5–6 Gbit） | 有明确容量 | 算不出「占带宽百分比」；必须加 `rate` 才是「利用率」 |
| 丢包位置 | 同一个 qdisc 同时丢数据与 ACK | 主要丢在瓶颈队列，ACK 单独走 | 回环上 ACK 丢失会放大 CUBIC 的损伤 |
| 丢包形态 | 默认独立同分布（伯努利） | 常成串（微突发、无线衰落） | 独立丢包对 CUBIC 更「友好」；真实 burst loss 更狠 |

本篇因此**不用 `lo`**，而是建一个 `netns` + `veth` 对，把 MTU 钉在 1500，把容量钉在 100 Mbit。这样得到的数字才能和「100 Mbit 专线」这句话对上。

### 为什么要用 netns 隔离

`tc qdisc add dev lo root ...` 改的是**整台机器的回环**。在一台共享主机上，它会同时影响你自己所有的本机测试、CI、甚至别的同事的进程；更糟的是，如果同一台机器上还有别人在做网络实验，两边会互相污染，谁都量不准。

本机的做法是：建一个命名空间 `rtt`，把 `veth1` 塞进去，所有 `netem` 只挂在这对 veth 上。实验结束 `ip netns del rtt` 一删，主机网络一根毛都不动。**这是实例篇的纪律：先保证不干扰别人，再谈数字。**

### 权限：`netem` 需要 `CAP_NET_ADMIN`

`tc qdisc` 是内核的配置接口，不是普通 socket 选项。它要求进程有 `CAP_NET_ADMIN`：

```bash
$ id -u
0
$ tc qdisc add dev veth0 root netem loss 1% && echo OK
OK
```

在**非特权容器**里，这条命令通常会得到：

```text
RTNETLINK answers: Operation not permitted
```

或者 `tc: cannot open '/etc/iproute2/...'` 之类的次要错误。判断方法只有一个：看 `tc` 的返回码与 `RTNETLINK` 报错，不要看 `which tc`——二进制在不在，和有没有权限是两件事。**如果没有权限，不要硬编一个「跑出来的数字」**，§51.4 末尾给两条替代路线（宿主侧执行 / 应用层代理）并标注哪些量测不到。

---

## 51.4 ③④ 走一遍 + 改一行：四组实验

### ⓪ 先探测环境：内核会什么、我有什么权限

```bash
$ sysctl net.ipv4.tcp_available_congestion_control net.ipv4.tcp_congestion_control
net.ipv4.tcp_available_congestion_control = reno cubic bbr
net.ipv4.tcp_congestion_control = bbr
$ uname -r
6.12.113+deb12-cloud-amd64
$ nproc; iperf3 --version | head -1
2
iperf 3.12 (cJSON 1.7.15)
$ cat /proc/sys/net/ipv4/tcp_rmem; cat /proc/sys/net/ipv4/tcp_wmem
4096	87380	33554432
4096	65536	33554432
```

**这段输出就是本篇的「出生证明」**：内核里有 `cubic` 与 `bbr`，默认已经是 `bbr`，所以后面每个实验都必须显式用 `-C cubic` / `-C bbr`，否则「对照组」会悄悄变成同一组。`tcp_rmem`/`tcp_wmem` 的上限都是 32 MB——比 100 Mbit × 100 ms 的 BDP（1.25 MB）大得多，所以本实验里**接收窗口不是瓶颈**，瓶颈只可能是 `cwnd`。这一点在第 ④ 步会被 `ss` 证实。

如果 `tcp_available_congestion_control` 里**没有 `bbr`**（老内核、或模块没加载），先确认：

```bash
$ modprobe tcp_bbr 2>&1
$ sysctl net.ipv4.tcp_available_congestion_control
```

还是没有就说明内核没编译该模块，只能换内核或换机器。**不要用「改 sysctl 失败但程序还能跑」来假装实验成立**。

### ① 搭台：netns + veth + netem

```bash
# 1. 一条 1500 B MTU 的虚拟链路，两端在不同 netns
ip netns add rtt
ip link add veth0 type veth peer name veth1
ip link set veth1 netns rtt
ip addr add 10.99.0.1/24 dev veth0
ip link set veth0 up mtu 1500
ip netns exec rtt ip addr add 10.99.0.2/24 dev veth1
ip netns exec rtt ip link set veth1 up mtu 1500
ip netns exec rtt ip link set lo up

# 2. 服务端 -> 客户端：100 Mbit 容量 + 单程 10 ms + 1% 丢包
tc qdisc add dev veth0 root netem rate 100mbit delay 10ms loss 1%
# 3. 客户端 -> 服务端（ACK 路径）：只加延迟与丢包，不限速
ip netns exec rtt tc qdisc add dev veth1 root netem delay 10ms loss 1%

# 4. 服务端放进 netns，客户端在根 netns
ip netns exec rtt iperf3 -s -D -p 5202
```

跑一组：

```bash
for cc in cubic bbr; do
  echo "== $cc =="
  iperf3 -c 10.99.0.2 -p 5202 -t 20 -C $cc -f m | tail -3
done
```

### ③ 走一遍：一次测量在六个位置花掉什么

| 步骤 | 命令 | 观察量 | 成本量级 |
| --- | --- | --- | --- |
| 建链路 | `ip netns add` + `veth` | 无 | 每条命令 1–10 ms，一次性 |
| 挂 qdisc | `tc qdisc add` | `backlog`、`dropped` | < 1 ms，改配置立即生效 |
| 建连接 | `iperf3 -c` | 三次握手 | 2×RTT（20 ms 链路 = 40 ms） |
| 慢启动 | 自动 | `cwnd` 每 RTT 翻倍 | 填满 250 KB 的 BDP 需 `log2(172/10) ≈ 4.1` 个 RTT ≈ 82 ms |
| 稳态 | — | CUBIC 锯齿 / BBR 周期探测 | 需要 10–20 s 才能跨过多次丢包事件 |
| 采样 | `ss -ti`、`tc -s` | `cwnd/rtt/retrans/delivery_rate` | 每次读 `/proc` 级，微秒级 |

一次 10 s 的实验里，1% 丢包意味着**每 100 个包就有 1 个被丢**。CUBIC 在 9 Mbit 时约 800 包/秒，也就是每 0.12 s 撞一次墙；BBR 在 93 Mbit 时约 8000 包/秒，每 12.5 ms 就有一次丢包。**同样 1%，对 CUBIC 是每 0.12 秒一次的判决，对 BBR 是每 12.5 毫秒一次可以忽略的噪声**——频率不同，含义更不同。

### A. 基线（无丢包）：两者应当一样快

| 链路 | CUBIC | BBR |
| --- | --- | --- |
| `lo`，无 qdisc（GSO 大段，42 篇口径） | 4936 Mbit | 4848 Mbit |
| `veth`，MTU 1500，无 qdisc | 5111 Mbit | 5494 Mbit |
| `veth` + 100 Mbit 整形 + 20 ms RTT + **0% 丢包** | **94.6 Mbit** | **94.5 Mbit** |

第三行是本节最重要的一行：**没有丢包时，两种算法几乎没有区别**。94.6 vs 94.5，差 0.1%，在测量噪声以内。这排除了「BBR 实现更快」「BBR 的代码路径更短」这类解释——它们在同一条链路上吃同样的慢启动、同样的 `cwnd`、同样的 pacing。

那 94.6 Mbit 为什么不是 100？因为 `rate 100mbit` 是令牌桶，包还要加上以太网头、IP 头、TCP 头（每 1448 B 数据多 54 B，开销约 3.6%），再加上 ACK 与丢包重传的余量。**94–95 Mbit 就是这条 100 Mbit 链路的实际上限**，后面所有百分比都以它为分母。

### B. 1% 丢包：分道扬镳

**20 s 单次采样：**

```text
== cubic ==
[  5]   0.00-20.00  sec  22.6 MBytes  9.47 Mbits/sec  141             sender
[  5]   0.00-20.02  sec  22.4 MBytes  9.40 Mbits/sec                  receiver

== bbr ==
[  5]   0.00-20.00  sec   224 MBytes  94.0 Mbits/sec  1721             sender
[  5]   0.00-20.04  sec   224 MBytes  93.8 Mbits/sec                  receiver
```

**12 s × 3 次重复：**

| 算法 | rep1 | rep2 | rep3 | 区间 |
| --- | --- | --- | --- | --- |
| CUBIC | 12.6 Mbit | 20.1 Mbit | 11.8 Mbit | 11.8–20.1 Mbit（12%–20%） |
| BBR | 92.6 Mbit | 92.5 Mbit | 92.9 Mbit | 92.5–92.9 Mbit（93%） |

CUBIC 的方差比 BBR 大得多——因为它的吞吐由「刚被砍到多低」和「爬回来多少」决定，采样窗口落在锯齿的哪个位置，结果就差一倍。BBR 几乎是常数，因为它不因丢包改变发送速率。

CUBIC 的逐秒轨迹（同一组参数，10 s）把锯齿直接画了出来：

```text
[  5]   0.00-1.00   sec  10.1 MBytes  84.4 Mbits/sec   85    100 KBytes
[  5]   1.00-2.00   sec  1.49 MBytes  12.5 Mbits/sec   22   19.8 KBytes
[  5]   2.00-3.00   sec  1.49 MBytes  12.6 Mbits/sec    2   41.0 KBytes
[  5]   3.00-4.00   sec  1.68 MBytes  14.1 Mbits/sec   13   11.3 KBytes
[  5]   4.00-5.00   sec   573 KBytes  4.69 Mbits/sec    6   19.8 KBytes
[  5]   5.00-6.00   sec  1.12 MBytes  9.38 Mbits/sec    7   14.1 KBytes
[  5]   6.00-7.00   sec   764 KBytes  6.25 Mbits/sec    9   14.1 KBytes
[  5]   7.00-8.00   sec  1.12 MBytes  9.39 Mbits/sec    4   28.3 KBytes
[  5]   8.00-9.00   sec   382 KBytes  3.13 Mbits/sec   14   17.0 KBytes
[  5]   9.00-10.00  sec  1.12 MBytes  9.35 Mbits/sec    6   19.8 KBytes
```

第一秒 84.4 Mbit 是慢启动还没撞墙；从第二秒开始，它就在 3–14 Mbit 之间反复：**丢一个包 → `cwnd` 乘 0.7 → 爬几秒 → 再丢**。这就是「1% 丢包」在 CUBIC 眼里的样子——不是每 100 个包损失 1 个，而是**每 100 个包就把整条管道的容量估计打掉三成**。

### C. 加 RTT：BDP 变大，CUBIC 更惨

同一套 100 Mbit 整形、同样 1% 丢包，只改 `netem delay`：

| RTT | BDP（100 Mbit × RTT） | CUBIC | BBR | CUBIC 占带宽 | BBR 占带宽 | 倍数 |
| --- | --- | --- | --- | --- | --- | --- |
| ≈0（veth，无 delay、无限速） | — | 789 Mbit | 2631 Mbit | — | — | 3.3× |
| 20 ms | 250 KB = 172 MSS | 9.4–20.1 Mbit | 92.5–93.8 Mbit | 9%–20% | 93% | 4.6–10× |
| 40 ms | 500 KB = 345 MSS | 3.7–4.1 Mbit | 90.4–90.7 Mbit | 4% | 90% | 22–24× |
| 100 ms | 1.25 MB = 863 MSS | 3.0–4.7 Mbit | 80.9–84.8 Mbit | 3%–5% | 81%–85% | 17–28× |

两件事同时在发生：

- **CUBIC 的吞吐随 RTT 下降**，因为它的稳态窗口由丢包率决定（`∝ 1/√p`），与 RTT 无关；而吞吐 = 窗口 / RTT，RTT 翻倍，吞吐就减半。Mathis 公式里 `RTT` 在分母，不是修辞。
- **BBR 的吞吐几乎不随 RTT 下降**（93% → 90% → 81%），因为它按「测得带宽 × 最小 RTT」发；RTT 变大，它就把窗口同比放大，而不是掉速。

`ss -ti` 把这两条路线的「体内状态」摊开了。**20 ms RTT、1% 丢包：**

```text
# CUBIC（第 6 秒采样，输出已折行）
cubic wscale:10,10 rto:224 rtt:20.387/0.19 mss:1448 pmtu:1500
 cwnd:22 ssthresh:16 bytes_sent:7428277 bytes_retrans:49232
 bytes_acked:7348638 segs_out:5133 segs_in:2351
 send 12500515bps pacing_rate 15000432bps delivery_rate 11993488bps
 delivered:5077 busy:5928ms unacked:21 retrans:0/34
 rcv_space:14480 notsent:75296 minrtt:20 snd_wnd:375808

# BBR（同一时刻）
bbr wscale:10,10 rto:224 rtt:23.788/0.07 mss:1448 pmtu:1500
 cwnd:392 ssthresh:236 bytes_sent:70068213 bytes_retrans:728344
 bytes_acked:69061854 segs_out:48393 segs_in:5308
 bbr:(bw:97930040bps,mrtt:20,pacing_gain:1,cwnd_gain:2)
 send 190891542bps pacing_rate 96950736bps delivery_rate 95553904bps
 unacked:192 retrans:0/503 minrtt:20 snd_wnd:25559040
```

**这张表是本篇的核心证据，逐项对账：**

| 字段 | CUBIC | BBR | 说明 |
| --- | --- | --- | --- |
| `cwnd` | **22 MSS**（≈32 KB） | **392 MSS**（≈568 KB） | 同样是 20 ms RTT，BBR 的在途量是 CUBIC 的 18 倍 |
| `ssthresh` | 16 | 236 | CUBIC 的慢启动阈值被丢包反复砍到 16 |
| `rtt` | 20.4 ms | 23.8 ms | BBR 排队略多（它主动多塞了 2.3×BDP） |
| `bytes_retrans/bytes_sent` | 49 KB / 7.4 MB = **0.66%** | 728 KB / 70 MB = **1.04%** | **BBR 重传更多，吞吐却高 10 倍** |
| `delivery_rate` | 12.0 Mbit/s | 95.6 Mbit/s | 实际交付速率，与 iperf3 对得上 |
| `minrtt` | 20 ms | 20 ms | 两者对「链路最小 RTT」的观测一致 |
| `snd_wnd` | 375,808 B | 25,559,040 B | 接收窗口远大于实际用量，**不是瓶颈** |

注意 CUBIC 的 `snd_wnd` 是 375 KB，而它的 `cwnd` 只有 32 KB——**接收方允许它发 375 KB，它自己只敢发 32 KB**。这就是「调大窗口没用」的物理证据：限制项在发送方的 `cwnd`，不在接收方的 `rwnd`，更不在 socket 缓冲区大小。

**100 ms RTT、1% 丢包（同一组命令，只改 delay）：**

```text
# CUBIC
cubic rtt:105.105/4.686 cwnd:19 ssthresh:17
 bytes_sent:2667253 bytes_retrans:21720 unacked:19
 notsent:202720 minrtt:100
  → 2.98 Mbit/s

# BBR
bbr rtt:102.14/0.049 cwnd:816 ssthresh:902
 bytes_sent:44106205 bytes_retrans:528520 unacked:1512
 minrtt:100
  → 84.8 Mbit/s
```

BBR 的 `cwnd:816` 正好对上 BDP：`100 Mbit × 102 ms ÷ 8 ÷ 1448 B ≈ 881` 个 MSS。**BBR 把窗口钉在「管道能装多少」，CUBIC 把窗口钉在「丢包率允许多少」**。前者随带宽和 RTT 缩放，后者只随 `p` 缩放——这就是 51.1 那张表里两者走势不同的全部原因。

### D. 改一行：只改 `tcp_congestion_control`

“改一行”在命令层面是 `-C bbr`，在内核层面是 socket 初始化时读的那一个 sysctl：

```bash
# 只影响之后新建的连接；已有连接仍用旧的算法
sysctl -w net.ipv4.tcp_congestion_control=bbr
# 验证某条连接实际用的是哪个
ss -tin dst 10.99.0.2 | grep -oE '(cubic|bbr)' | head -1
```

**改前 / 改后（同一条 100 Mbit、20 ms RTT、1% 丢包的链路，同一条 iperf3 命令）：**

| 指标 | 改前：`cubic` | 改后：`bbr` | 变化 |
| --- | --- | --- | --- |
| 单流吞吐（20 ms RTT） | 9.4 Mbit（20 s 采样） | 93.8 Mbit | **10.0×** |
| 单流吞吐（12 s×3） | 11.8 / 12.6 / 20.1 Mbit | 92.5 / 92.6 / 92.9 Mbit | 4.6–7.9× |
| 单流吞吐（40 ms RTT） | 3.7–4.1 Mbit | 90.4–90.7 Mbit | **22–24×** |
| 单流吞吐（100 ms RTT） | 3.0–4.7 Mbit | 80.9–84.8 Mbit | **17–28×** |
| `cwnd`（20 ms RTT） | 22 MSS | 392 MSS | 17.8× |
| `delivery_rate`（20 ms RTT） | 12.0 Mbit/s | 95.6 Mbit/s | 8.0× |
| 重传率 `bytes_retrans/bytes_sent` | 0.66% | 1.04% | **反而更高** |
| 占 100 Mbit 链路 | 9%–20% | 93% | — |

**这张表最该看的是倒数第二行**：BBR 的重传率比 CUBIC 高 58%，吞吐却高一个数量级。它证明 BBR 不是「更会避免丢包」，而是「丢了也不当回事」；也提醒你，**在按流量计费或共享瓶颈的链路上，这 1% 的额外重传是实打实的成本**（§51.5 会展开）。

再看丢包率本身的作用（100 Mbit、20 ms RTT、8 s 采样）：

| 单向丢包 `p` | CUBIC | BBR | CUBIC 的变化 |
| --- | --- | --- | --- |
| 0% | 94.6 Mbit | 94.5 Mbit | — |
| 0.1% | 34.0 Mbit | 94.6 Mbit | **-64%** |
| 1% | 16.0 Mbit | 94.3 Mbit | -83% |
| 3% | 3.34 Mbit | 91.1 Mbit | -96% |

**0.1% 就砍掉 CUBIC 三分之二的吞吐**——这条比「1% 掉 10 倍」更反直觉。现实中大量「轻微丢包」的链路（0.05%–0.3%）正好落在这个陡坡上；运维看到 ping 丢包 0.2%，通常会说「网络没问题」，而 CUBIC 已经只剩三分之一。

### 量具校准：`tc -s` 的 `dropped` 不能直接当丢包率

同一组 10 s 实验，qdisc 的计数增量是：

```text
 Sent 6547593778 bytes 4328550 pkt (dropped 2800, overlimits 0 requeues 0)
（10 s 前）                                          ↑ 累计值
 Sent 6547593778 → 6547593778 + 21.5 MB，4328550 → 4328550 + 14220 pkt
（10 s 后）                                     dropped 2800 → 2851，增量 51
```

`51 / 14220 = 0.36%`，看起来像「丢包率只有 0.36%」，但同一时刻 `ss` 给的 `bytes_retrans/bytes_sent = 0.66%`。为什么对不上？

- `dropped` 数的是 **qdisc 看到的 skb**，不是 TCP 段；TSO/GSO 下 1 个 skb 可能装着多个段，比例被压缩；
- 计数里还混着 ARP、重传、以及 `limit` 溢出导致的 tail-drop；
- 重传本身也过 qdisc，会被再掷一次骰子。

用一个纯 UDP 的小实验校准就清楚了：同样 `loss 10%`，发 2000 个 1200 B 的包，qdisc 给的是 `Sent 1784 pkt (dropped 217)`，`217 / (1784+217) = 10.8%`，与设定的 10% 在统计误差内。**所以要量端到端丢包率，用 `ss -ti` 的 `bytes_retrans/bytes_sent` 或 `nstat` 的 `TcpRetransSegs/TcpOutSegs`，别拿 `tc` 的 `dropped` 直接除。**

### 没有 `CAP_NET_ADMIN` 怎么办：两条替代路线

**路线一（推荐）：把 `netem` 挂到宿主或特权容器上。** 容器里没有权限，不代表宿主没有。在宿主 netns 里对 `veth` 的宿主端挂 qdisc，容器内的进程照样受影响：

```bash
# 宿主机上（有 root / CAP_NET_ADMIN）
CONTAINER_PID=$(docker inspect -f '{{.State.Pid}}' app)
HOST_VETH=$(ip -o link | awk -v p="$CONTAINER_PID" \
  '$0 ~ "veth" {print $2}' | sed 's/@.*//' | head -1)
tc qdisc add dev "$HOST_VETH" root netem delay 20ms loss 1%
# 实验结束务必删除，否则会影响该容器的所有流量
tc qdisc del dev "$HOST_VETH" root
```

这条路能拿到真正的 `cwnd`/`pacing` 数据，是唯一能复现本篇全部指标的方式。

**路线二：应用层代理随机丢包。** 没有一点权限时，可以在两个进程之间塞一个 UDP/TCP 代理，按 1% 概率丢包。它能让你看到「应用层 goodput 与 RTO 的变化」，但**量不到 `cwnd`、`pacing_rate`，也造不出 100 Mbit 的容量上限**——只能给趋势，不能给本篇的百分比。下面是一个 20 行的 UDP 转发器（`seed=51` 保证可复现）：

```python
import random, select, socket

LISTEN   = ("127.0.0.1", 9000)   # 业务连这里
UPSTREAM = ("127.0.0.1", 9100)   # 真实服务在这里
LOSS     = 0.01

srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
srv.bind(LISTEN)
up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
up.connect(UPSTREAM)
random.seed(51)
client = None

while True:
    ready, _, _ = select.select([srv, up], [], [])
    if srv in ready:                       # 客户端 -> 服务端
        data, client = srv.recvfrom(65535)
        if random.random() >= LOSS:
            up.send(data)
    if up in ready:                        # 服务端 -> 客户端
        data, _ = up.recvfrom(65535)
        if client and random.random() >= LOSS:
            srv.sendto(data, client)
```

用法：业务把目标从 `:9100` 改成 `:9000`，在 `:9100` 起真实服务，再跑一个固定速率的发送端（比如 `iperf3 -u -b 50M`）。**必须在结论里写清这是应用层丢包**：它丢的是「已经交给内核的 UDP 数据报」，TCP 的重传、拥塞窗口、ACK 路径全都没参与；如果业务是 TCP，代理只能看到「连接被重置/超时」，连趋势都给不出。

**路线三（只做推演）：引用公开实测 + 本篇的 42 篇对照。** 如果既没有权限也不能改部署，就明确写「本条为基于公开数据的推演」，并把公式、假设、`MSS`、`RTT`、`p` 都列出来。**推演不是罪，把推演写成实测才是。**

---

## 51.5 ⑤ 它证伪了谁：五条流行说法

### ①「丢包 = 拥塞」——只在丢包驱动的世界里成立

CUBIC 的全部逻辑建立在一个等式上：**丢包意味着队列满了，队列满了意味着我发太快了**。于是它的动作是唯一且正确的：把 `cwnd` 乘 0.7，退一步。

BBR 说：这个等式在今天的链路上经常不成立。跨境链路的 1% 丢包可能来自中间设备的微突发、光模块误码、无线信道衰落、甚至运营商的流量整形器；这些丢包与「你发太快」没有因果关系。你退一步，路不会变宽，只会让自己更慢。

本篇的数字就是这条的判决：同样 1% 丢包，一个掉到 9%–20%，一个稳在 93%。**如果丢包真是拥塞信号，两者不该差 5–25 倍。**

但要说清边界：**BBR 证伪的是「丢包 ⇒ 拥塞」这个蕴含，不是「丢包与拥塞无关」。** 当丢包确实来自你自己的队列溢出时（`tc -s qdisc` 的 `backlog` 长期贴顶、`dropped` 随发送速率线性上涨），BBR v1 的反应比 CUBIC 慢，因为它主要靠 RTT 与交付速率，而不是丢包。此时换 BBR 会让队列更长、延迟更高——这是 §51.5 ③ 要讲的代价。

### ②「调大窗口就能提速」——窗口不是瓶颈，`cwnd` 才是

「单流慢？把 `tcp_wmem`、`tcp_rmem` 调大」是最常见的建议，也是最容易白干的建议。本篇的 `ss` 输出直接给了反例：

- CUBIC 的 `snd_wnd`（接收方通告的窗口）是 **375,808 B**；
- 它的 `cwnd` 只有 **22 MSS ≈ 32 KB**；
- 它的 `notsent` 是 **75,296 B**——有 75 KB 数据已经排队，但因为 `cwnd` 满了发不出去。

接收方说「你能发 375 KB」，发送方说「我只敢发 32 KB」。把接收缓冲区从 375 KB 调到 4 MB，`cwnd` 还是 22，吞吐还是 9 Mbit。本机实测：把客户端 `-w` 从默认调到 4 MB，CUBIC 在 1% 丢包下仍然落在 9–21 Mbit 的区间里抖动，没有任何一次接近 50 Mbit。

原因写在 Mathis 公式里：`吞吐 = MSS/RTT × 1.22/√p`，**没有 `W`**。窗口只决定上限（`W/RTT`），而丢包驱动的算法自己把 `W` 压到了远低于上限的位置。**在丢包链路上，窗口是天花板，`cwnd` 是实际身高；天花板再高，身高不会变。**

例外也要给：如果 `ss` 显示 `rwnd_limited` 占比很高（比如 > 20%），那瓶颈确实在接收窗口，调大有效。**判据是 `rwnd_limited` 这个字段，不是「听说调大有用」。**

### ③「换算法是银弹」——BBR 有自己的失效模式

BBR 在本篇的场景里赢了，但它不是免费午餐。至少四笔账：

1. **多打 1% 的流量**。BBR 的重传率 1.04% > CUBIC 的 0.66%。在按流量计费的专线、或共享的卫星链路上，这 1% 是钱；在拥塞的公共瓶颈上，这 1% 是从别人那里抢来的。
2. **与丢包驱动的流共存时会抢占**。BBR 不因丢包退让，CUBIC 退让；同一个瓶颈上，BBR 流的份额会持续上升，直到把 CUBIC 流挤到很低的窗口。**BBR 对 BBR 公平，对 CUBIC 不公平**——这是部署时最容易被忽略的一条。
3. **浅缓冲 / 深缓冲两端都可能出问题**。缓冲太浅时，BBR 的 pacing 突发会造成排队丢包；缓冲太深时，BBR v1 缺少「用丢包刹车」的机制，可能把队列堆到 RTT 翻几倍。本篇 20 ms RTT 下 BBR 的 `rtt` 是 23.8 ms（CUBIC 20.4 ms），已经能看出它在主动多塞 2.3×BDP。
4. **`ProbeRTT` 的周期打扰**。BBR 每 10 s 把窗口压到 4 个包、持续 200 ms，去重测最小 RTT。对吞吐是小事（< 2%），对**延迟敏感业务**是每 10 秒一次的尖峰。视频会议、实时对战这类业务，BBR v1 不是默认答案，delay-based 的 GCC/BBR v2/v3 更合适。

一句话：**CUBIC 的失效模式是「把噪声当拥塞」，BBR 的失效模式是「把拥塞当噪声」。** 前者在坏链路上自杀，后者在真拥塞时赖着不走。

### ④「延迟只影响体验，不影响吞吐」——RTT 在吞吐的分母里

这条最容易被「我们内网很快」的经验掩盖。本篇的 C 组实验把 RTT 单独拧了一遍，其余全不变：

- 20 ms → 40 ms → 100 ms，CUBIC 从 9.4–20.1 掉到 3.7–4.1 再掉到 3.0–4.7；
- BBR 从 93% 掉到 90% 再掉到 81%–85%。

机制在 42 篇已经推过：`吞吐 ≤ W/RTT`，而 `W` 至少要等于 BDP 才能填满管道。100 Mbit × 100 ms 的 BDP 是 1.25 MB = 863 个 MSS。CUBIC 的 `cwnd` 只有 19 个 MSS——它连管道的 2.2% 都没填满，剩下的 97.8% 全是空气。BBR 的 `cwnd` 是 816 个 MSS，正好把管道填满。

**所以「延迟」不是体验指标，它是吞吐公式里的一个因子。** 跨境链路把 RTT 从 20 ms 拉到 200 ms，不是「慢一点」，是「同样的窗口只能跑十分之一的吞吐」；除非算法愿意把窗口放大十倍（BBR 会，CUBIC 不会）。

### ⑤「1% 丢包只损失 1% 的性能」——损伤是 `1/√p`，不是 `p`

丢包扫描那张表已经把答案写死：0.1% → CUBIC 掉 64%，1% → 掉 83%，3% → 掉 96%。**丢包率涨 30 倍，吞吐掉 28 倍**；而 0.1% 这个「ping 看起来很正常」的值，已经能让 CUBIC 只剩三分之一。

如果损伤是线性的，1% 丢包应该只掉 1% 的吞吐；实际是 5–25 倍。差别来自「一个丢包事件砍掉的是一个窗口，不是一个包」：CUBIC 丢一个包，`cwnd` 从 `W` 掉到 `0.7W`，然后要花好几个 RTT 爬回来；在这几个 RTT 里，管道是空的。**丢包率是事件频率，吞吐损失是事件后果；两者不是同一个量。**

---

## 51.6 工程结论：什么链路选什么、怎么验证

### 选型表：按「丢包的性质」而不是「丢包的数字」选

| 场景 | 典型 RTT / `p` | 首选 | 理由 | 必须验证 |
| --- | --- | --- | --- | --- |
| 跨境 / 洲际专线 | 100–250 ms / 0.1%–1% | BBR（v1 可用，能上 v2/v3 更好） | 丢包多为非拥塞（光衰、中间设备、整形器） | `retrans ≈ p` 且 `cwnd` 被压在 `1.22/√p` |
| 卫星 / 蜂窝 / 无线 | 30–600 ms / 0.5%–5% | BBR | 随机无线丢包，CUBIC 会把噪声当拥塞 | 观察 `ProbeRTT` 造成的延迟尖峰是否可接受 |
| 同机房 / 数据中心内 | < 1 ms / ≈0 | CUBIC（或 DCTCP 等 ECN 方案） | 无随机丢包，BBR 无收益；CUBIC 与交换机 ECN 生态更成熟 | 确认 `p≈0`；否则先查链路/网卡 |
| 公网 Web / 短连接 | 20–100 ms / 0–0.5% | CUBIC 或 BBR 均可 | 短流受慢启动支配，换算法收益小于连接复用/TLS 优化 | 先量「单流是否跑满 BDP」，再谈算法 |
| 实时音视频 / 游戏 | 20–100 ms / 0–2% | delay-based（GCC / BBR v2/v3 / QUIC） | BBR v1 的 `ProbeRTT` 与排队会伤延迟 | 看 p99 RTT，不只看平均吞吐 |
| 共享瓶颈 / 多租户 | 任意 | 谨慎上 BBR | BBR 对 CUBIC 不公平，会挤占 | 同一瓶颈上混合算法的份额变化 |

### 云厂商的拥塞控制可选项：三个必须知道的事实

1. **默认值多为 `cubic`，`bbr` 需要自己开。** 主流云镜像的内核里通常同时编了 `cubic` 与 `bbr`，但 `net.ipv4.tcp_congestion_control` 的默认值多为 `cubic`；`bbr` 在 4.9 以后的主线内核里才有。判断的唯一依据是 `sysctl net.ipv4.tcp_available_congestion_control` 的输出，不是镜像版本号。
2. **`tcp_congestion_control` 是 per-netns 的**（4.x 之后）。容器共享宿主 netns 时，你在容器里改的是宿主的默认值，会影响整台机器上所有新建连接；在独立 netns 里改才只影响该 netns。**改之前先确认自己在哪个 netns。**
3. **托管负载均衡会终结 TCP。** 如果 SLB/ALB/NLB 终结了客户端连接，那么「客户端 → LB」这一段用的是 LB 的拥塞控制，你在后端 VM 上改的只影响「LB → 后端」这一段。用户抱怨慢，可能跟你改的那一段无关。

另外两条部署常识：`sysctl` 只影响**之后新建的连接**，已建立的连接不会切换；BBR 的 pacing 传统上建议出口挂 `fq`（`tc qdisc replace dev eth0 root fq`），新内核即使 `fq_codel` 也能工作，但 `fq` 最稳。

### 三条验证命令：先量，再改

```bash
# 1. 这条连接到底卡在哪：cwnd 还是 rwnd？
ss -tin dst 10.0.0.2 | grep -E 'cwnd:|rwnd_limited|delivery_rate|retrans'

# 2. 全机重传率（分子分母都要，别只看分子）
nstat -az | grep -E 'TcpOutSegs|TcpRetransSegs'

# 3. 队列有没有满：backlog 是否贴顶、dropped 是否随速率上涨
tc -s qdisc show dev eth0
```

**判据（可直接写进 SOP）：**

| 观察 | 结论 | 动作 |
| --- | --- | --- |
| `retrans/out ≈ p`，`cwnd` 远小于 BDP，`rwnd_limited` 很小 | 丢包驱动的算法被非拥塞丢包压住了 | 试 BBR；这是本篇的主场景 |
| `backlog` 长期贴 `limit`，`dropped` 随发送速率线性涨 | 真拥塞 / 队列溢出 | 加带宽、调 buffer、上 ECN；**换 BBR 会更糟** |
| `rwnd_limited` 占比 > 20% | 接收窗口是瓶颈 | 调大对端 `SO_RCVBUF` / `tcp_rmem` |
| `cwnd` 已接近 BDP，`delivery_rate` 仍低 | 瓶颈不在窗口 | 查应用、CPU、网卡（`base/32`） |
| `minrtt` 远小于 `rtt` | 有排队（缓冲膨胀） | 查 qdisc backlog、中间盒 buffer |

### 一张「不要做」清单

- **不要在共享主机上直接对 `lo` 挂 `netem`**：会污染所有本机测试，也会污染别人的实验。用 netns + veth。
- **不要用 `ping` 的丢包率代替 TCP 的丢包率**：本篇 100 个 ping 显示 2%（双向各 1%），而 TCP 的 `bytes_retrans/bytes_sent` 是 0.66%–1.04%。`ping` 的 2% 是「往返任一方向丢」的联合概率，TCP 的 1% 是「发送方向」的单向概率。
- **不要拿 `tc -s` 的 `dropped/Sent` 当丢包率**：见 §51.4 的量具校准。
- **不要在 10 秒的采样上给 BBR 下结论**：CUBIC 的方差可以到 2 倍；至少 3 次重复，取区间而不是单点。
- **不要把「换 BBR」当成「加带宽」的替代**：BBR 只能在**已有容量**里把管道填满；容量不够时，它只会把队列堆得更长。

---

## 51.7 五问收口表

| 五问 | 本篇的答案 |
| --- | --- |
| **① 它是什么** | `tc qdisc netem` 是挂在网卡**出口队列**上的可编程坏路：按参数给每个包加延迟、丢包、重复、乱序、限速。本篇用它把「带宽 / RTT / 丢包率」变成三个独立旋钮，测同一链路上两种拥塞算法的吞吐与延迟 |
| **② 物理形态** | qdisc 在 egress，队列深度 `limit`（默认 1000 包 ≈ 1.5 MB）；`delay` 是固定睡眠，`rate` 是令牌桶、超出部分进 `backlog`；双向各挂一个才构成「路径丢包」。回环的 64 KB MSS、无带宽上限、ACK 同 qdisc 三个差异决定了它只能给比例，不能给百分比 |
| **③ 走一遍** | 建 netns/veth（~10 ms）→ 挂 qdisc（<1 ms）→ 握手（2 RTT）→ 慢启动填 BDP（`log2(BDP/IW)` 个 RTT，20 ms 链路约 82 ms）→ 稳态（10–20 s 采样）→ `ss -ti` / `tc -s` 取数。1% 丢包在 9 Mbit 时每 0.12 s 一次，在 93 Mbit 时每 12.5 ms 一次 |
| **④ 改一行** | 只改 `net.ipv4.tcp_congestion_control=cubic → bbr`：20 ms RTT 下 9.4 → 93.8 Mbit（10×），40 ms 下 3.7–4.1 → 90.4–90.7（22–24×），100 ms 下 3.0–4.7 → 80.9–84.8（17–28×）；`cwnd` 22 → 392 MSS；重传率 0.66% → 1.04%（反而更高） |
| **⑤ 它证伪了谁** | ①「丢包 = 拥塞」——同一个 1% 可以只是噪声；②「调大窗口就能提速」——`snd_wnd` 375 KB 用不上，`cwnd` 只有 32 KB；③「换算法是银弹」——BBR 多打 1% 流量、抢占 CUBIC、`ProbeRTT` 伤延迟；④「延迟只影响体验」——RTT 在吞吐分母里，20→100 ms 让 CUBIC 再掉 2–4 倍；⑤「1% 丢包只损失 1%」——实际是 5–25 倍 |

### 同一件事在各层叫什么

| 你嘴里的词 | 在 qdisc 层 | 在 TCP 层 | 在业务层 |
| --- | --- | --- | --- |
| 「网络抖动」 | `delay` + `jitter` | RTT 方差、RTO 抖动 | p99 延迟、卡顿 |
| 「丢包」 | `loss p`、`dropped` | `bytes_retrans/bytes_sent`、dupACK | 超时、重试、重复请求 |
| 「带宽不够」 | `rate`、`backlog` | `cwnd` / BDP / `delivery_rate` | 吞吐上限、同步耗时 |
| 「时快时慢」 | 队列在 `limit` 附近振荡 | CUBIC 锯齿、BBR 周期探测 | 用户体验波动、SLA 不达标 |
| 「长肥管道」 | RTT × rate 很大 | BDP 大、窗口要跟着大 | 跨境/卫星专线 |

---

## 51.8 一页速记

```text
【一句话】1% 丢包不是「损失 1% 吞吐」，是「换一种对丢包的解释」：
          CUBIC 读成「路满了」→ 砍窗；BBR 读成「链路噪声」→ 照发。

【五个数字（本篇实测，100 Mbit 整形，双向 1% 丢包）】
  RTT 20 ms : CUBIC 9.4–20.1 Mbit（9–20%）  BBR 92.5–93.8（93%）
  RTT 40 ms : CUBIC 3.7–4.1  Mbit（4%）      BBR 90.4–90.7（90%）
  RTT 100ms : CUBIC 3.0–4.7  Mbit（3–5%）    BBR 80.9–84.8（81–85%）
  cwnd@20ms : CUBIC 22 MSS（32 KB）          BBR 392 MSS（568 KB）
  0.1% 丢包 : CUBIC 已从 94.6 掉到 34.0 Mbit（-64%）

【三个公式】
  吞吐 ≈ (MSS/RTT) × 1.22/√p        ← 只对丢包驱动算法成立
  BDP  = 带宽 × RTT                  ← 100 Mbit × 100 ms = 1.25 MB = 863 MSS
  cwnd_BBR ≈ BtlBw × RTprop          ← 实测 816 MSS ≈ 1.18 MB，贴着 BDP

【三条判据】
  retrans≈p 且 cwnd≪BDP        → 非拥塞丢包，试 BBR
  backlog 贴顶 且 dropped 随速率涨 → 真拥塞，换 BBR 会更糟
  rwnd_limited > 20%            → 接收窗口瓶颈，调 buffer

【三个坑】
  回环 MSS 65 KB、无带宽上限 → 只能给比例，给不了「占带宽百分比」
  tc 的 dropped 不是丢包率（skb≠段，还混着 tail-drop 与重传）
  只改 sysctl 不影响已建立的连接
```

---

## 51.9 自检 3 题

**Q1.** 一条 100 Mbit、RTT 60 ms、单向丢包 1% 的链路，CUBIC 实测单流 5 Mbit。运维把发送端 `tcp_wmem` 从 32 MB 调到 256 MB，问能不能到 50 Mbit。请用本篇的公式与判据回答，并给出「要多少丢包率才能到 50 Mbit」。

<details><summary>答案</summary>

不能。三个理由：

1. **瓶颈是 `cwnd`，不是 socket 缓冲区**。32 MB 已经远大于 BDP（100 Mbit × 60 ms = 750 KB = 518 MSS），再调大只是让「发不出去的数据」在内存里排得更长。`ss` 会显示 `snd_wnd` 很大而 `cwnd` 很小，`notsent` 持续增长。
2. **Mathis 公式给量级**：`1460/0.06 × 1.22/√0.01 = 24.3 kB/s × 12.2 ≈ 297 kB/s ≈ 2.4 Mbit/s`。实测 5 Mbit 是该值的 2 倍，说明 CUBIC 的三次增长比 Reno 略激进，但仍在同一量级——**没有任何一项与 `tcp_wmem` 有关**。
3. **要 50 Mbit，需要把 `p` 降两个数量级**。稳态 `cwnd ∝ 1/√p`：从 5 Mbit 到 50 Mbit 要窗口涨 10 倍，`p` 要降到 `1%/100 = 0.01%`。或者换 BBR——它在同样 1% 下按 BDP 发，60 ms 链路只需要 518 MSS 的在途量，33 MB 的缓冲区绰绰有余。

正确动作：先用 `ss -ti` 确认 `cwnd` 与 `rwnd_limited`，再决定是「换算法」还是「修链路」。**调 buffer 是最贵、最没用的一步。**

</details>

**Q2.** 本篇里 BBR 的重传率是 1.04%，CUBIC 是 0.66%，但 BBR 的吞吐高 10 倍。为什么「重传率高」不等于「有效吞吐低」？这个现象在什么情况下会变成真问题？

<details><summary>答案</summary>

重传率是**成本指标**，不是**速率指标**。BBR 的发送速率由 `BtlBw × pacing_gain` 决定，`BtlBw` 来自「实际交付速率」的窗口最大值；丢一个包只让那一段重传，不会改变它对管道容量的估计。所以它多发 1% 的字节，换来的是管道始终被填满。

CUBIC 相反：0.66% 的重传率看着更「干净」，但每一次丢包都把 `cwnd` 乘 0.7，稳态窗口被钉在 22 个 MSS（BDP 的 13%）。**它省下了 0.38% 的重传，代价是 87% 的空窗。**

变成真问题的三种情况：

1. **按流量计费**：多打的 1% 是账单；如果链路 1 Gbit 满跑，1% 就是 10 Mbit 的持续浪费。
2. **共享瓶颈**：BBR 不退让，CUBIC 退让，混合部署时 BBR 会挤占 CUBIC 流的份额；对同一瓶颈上的其他租户不公平。
3. **丢包本身是拥塞信号时**：如果 `backlog` 已贴顶、`dropped` 随速率上涨，BBR 仍然照发，会把队列越堆越长，RTT 被自己撑大——这时「无视丢包」从优点变成缺点。

</details>

**Q3.** 一个团队在非特权容器里执行 `tc qdisc add dev eth0 root netem loss 1%`，得到 `RTNETLINK answers: Operation not permitted`。同事说「那就不加丢包，直接对比 cubic 和 bbr」，在同一容器里跑 `iperf3 -C cubic` 与 `-C bbr`，两者都是 4.9 Gbit/s，于是结论是「换算法没用」。请指出这个实验至少三处错误，并给出最小可行修法。

<details><summary>答案</summary>

三处错误：

1. **没有自变量**。链路本身 `p≈0`，而本篇的 A 组已经证明：无丢包时 CUBIC 94.6、BBR 94.5，本来就该一样。这不是「算法没用」，是「实验没做」。
2. **没有验证实验条件**。看不到 `retrans ≈ p`，也没有 `ss -ti` 的 `cwnd`/`minrtt`，就无法证明「1% 丢包真的注入了」。任何「两者一样」的结论都可能只是「两者都没丢包」。
3. **忽略了 netns 与连接生命周期**。容器共享宿主 netns 时，`sysctl` 改的是宿主默认值；而且只影响新建连接，`iperf3` 每次新建连接还算干净，但如果用长连接池就会测到旧算法。另一个常见错误是只改一端——BBR 只需发送端开启，但接收端的 `rwnd` 与 ACK 行为仍会影响结果。

最小可行修法（任选其一）：

- 在**宿主**上对容器的 veth 挂 `netem`（§51.4 路线一），容器内不用任何权限；
- 用**特权容器**或 `--cap-add=NET_ADMIN` 起一个临时实验容器；
- 完全没有权限时，用应用层 UDP 代理注入 1%（§51.4 路线二），并在结论里注明「量不到 `cwnd`」。

验收标准：`ss -ti` 里 `bytes_retrans/bytes_sent` 落在 `p` 附近，且 `cwnd` 与 BDP 的关系能被解释——**否则这组数据不能用来评价算法。**

</details>

---

## 51.10 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 重传、SACK、滑动窗口与 CUBIC/BBR 的机制推导（本篇的「为什么」） | `base/42`，尤其 §42.6 与 §42.9 |
| 三次握手、四次挥手、TIME_WAIT（本篇只用了握手 2 RTT） | `base/41` |
| QUIC 为什么把拥塞控制搬到用户态（BBR 在 QUIC 里的形态） | `base/43` |
| 一次请求的完整旅程（把本篇的 RTT 放回整条链） | `base/53` |
| 收包六道门与队列：带宽死在最窄那道 | `base/32` |
| `perf`/`strace`/`tcpdump`/`nstat` 各查哪一层 | `base/54` |
| 量级速查与「现象 → 层 → 工具」总表 | `base/54` |
| RPC 连接复用：为什么「多开连接」常常比「换算法」更有效 | [comp/40](../comp/40-rpc-and-connection-reuse.md) |
| BBR 原始设计（公式、状态机与公开实测的出处） | ACM Queue, *BBR: Congestion-Based Congestion Control*, 2016 |
| 全书总纲与本篇在 Part IV 的位置 | [0-summary.md](0-summary.md) §0.4、§0.6.5 |
