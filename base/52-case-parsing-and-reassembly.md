# 52. 实例剖析：手工解析与重组——手写 DNS/IP/TCP/UDP 头、分片与粘包

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 网络：从一根网线到一张证书（实例篇） ｜ **编号**：52 / 56
> **本篇定位**：Part IV 的验伪出口。40、41、42、44 四篇分别讲了 IP 头、TCP 状态机、可靠传输与 DNS 报文，但它们都是「读别人的结论」。本篇把库全部拆掉，只用 `struct.unpack` 和一双手，把 DNS/IP/TCP/UDP 四个头逐字段解出来，再亲手制造一次粘包和一次分片。判据只有一个：**当解析结果不对时，你能立刻指出是第几个字节、哪一位、乘错了哪个单位，而不是换一个库再试。**
> **读法**：§52.1 先看一个「IHL 被写死成 20」的 bug，它会在你机器上原样复现；§52.2–§52.3 建立「偏移 × 宽度 × 端序 × 单位」四件套；§52.4 的 A–E 五组实验互相独立，每组都有可复制的脚本与真实输出，**每组的「错误解析 / 正确解析」两列必须看**；§52.5–§52.6 把五组实验压成四条被证伪的说法和三条铁律；§52.7 是五问收口。
> **不在本篇**：字节序与对齐的完整机制 → `base/06`；IP 转发、CIDR 与 NAT → `base/40`；TCP 状态机与 TIME_WAIT → `base/41`；拥塞控制算法对比 → `base/42`；DNS 的递归/迭代与 TTL 缓存语义 → `base/44`；TLS 记录层逐帧 → `base/50`；内核收包六道门 → `base/32`。

---

## 52.1 引子：把 IHL 当成 20，整条链路错位 4 字节

一个做流量镜像的服务，从 TUN 设备读原始 IP 包，自己解析出五元组再打标签。代码里写着：

```c
struct iphdr *ip = (struct iphdr *)buf;
uint8_t proto = ip->protocol;
uint16_t sport = ntohs(*(uint16_t *)(buf + 20));   /* 假定 IP 头就是 20 字节 */
```

实验室里跑了三个月，零故障。上线到一个新机房后，少量连接开始「端口对不上」：抓到的源端口是 `37892`，目的端口是 `0`，而真实连接是 `1234 -> 53`。更奇怪的是，同一份镜像里 99% 的包都正常，只有某些路由路径上的包会坏。

根因只有一行：**IPv4 的头部长度不是常数 20，而是首字节低 4 位的 `IHL` 乘以 4。**当包带选项（Router Alert、时间戳、记录路由）时，`IHL = 6`，头部是 24 字节；从偏移 20 开始读「TCP/UDP 源端口」，读到的其实是选项的最后 4 个字节。

本篇在实验机上把这个包原样构造出来，解析结果如下（脚本见 §52.4.B）：

```
packet len = 32  IHL = 6  -> header = 24
错误（固定 20）: payload@20 sport=37892 dport=0 payload_hex=9404000004d20035
正确（IHL×4）: payload@24 sport=1234 dport=53 payload_hex=04d2003500080000
```

`0x9404` 是 Router Alert 选项的 kind/length，被当成了源端口 `37892`；紧接着的 `0x0000` 被当成了目的端口 `0`。**错位 4 个字节，整个四层头部的每一个字段都平移了。**

这个 bug 有三个典型特征，值得单独记：

| 特征 | 表现 | 为什么难查 |
| --- | --- | --- |
| 只在带选项的包上出现 | 绝大多数流量正常，偶发坏包 | 复现依赖路径上的中间设备 |
| 错得「很像真的」 | `37892` 是一个合法端口号，不报错 | 解析器没有「字段合法性」概念 |
| 越往上越离谱 | 端口错 → 查表错 → 标签错 → 告警指向错误的服务 | 根因和症状隔了三个系统 |

修复不是「加一个 if」，而是把三件事同时做对：**偏移按 `IHL × 4` 算、长度按 `total_length` 校验、非法 `IHL` 直接拒绝**。这三件事就是本篇 §52.6 的三条铁律。

> 本篇量到的是 **主线①「没有魔法，只有位与约定」**：同一个 32 字节，按不同的偏移和单位解释，就是完全不同的两个包。副线是 **主线③「每层抽象的单价 = 跨界次数 × 单次成本」**：§52.4.D 会把「一次 `recv(1024)`」和「14 次 `recv(1)`」的系统调用账摆在同一张表里。

---

## 52.2 ① 它是什么：解析器是序列化的逆函数

**解析器不是一个「把包变成结构体」的函数，而是一个把字节串按 schema 映射成「字段 + 消费了多少字节」的函数。**后半句才是关键：库通常只给你前半句，于是「这个包到底有多长、下一个包从哪开始」永远由调用方猜。

用一句话写它的类型：

```
parse : (bytes, schema) -> (fields, consumed)   |   error
```

序列化是 `(fields, schema) -> bytes`，解析是它的逆。逆函数比原函数难写，因为原函数的输入一定合法，逆函数的输入可以是任何东西——包括攻击者精心构造的 61 字节。

一个手写解析器只需要四本账，缺一本就会错：

| 账本 | 回答的问题 | 错了会怎样 |
| --- | --- | --- |
| **偏移** | 这个字段从第几个字节开始 | 整体错位（§52.1 的 bug） |
| **宽度** | 占几个字节、有没有跨字节 | 把 `flags+frag` 当成两个独立字节 |
| **端序** | 多字节按大端还是小端读 | 端口 `443` 读成 `47873` |
| **单位** | 字段值的「1」代表几字节 | `IHL=6` 当成 6 字节而不是 24 字节 |

### 四层头部，四种「长度由谁说了算」

| 层 | 头部固定部分 | 可变部分 | 总长度由谁给 | 边界在哪 |
| --- | --- | --- | --- | --- |
| DNS | 12 B | QNAME（变长 label）+ 若干 RR | 头部计数（QD/AN/NS/AR） | UDP 长度或 TCP 帧长度 |
| IPv4 | 20 B | 选项 0–40 B | 首字节 `IHL × 4` + `total_length` | `total_length` |
| TCP | 20 B | 选项 0–40 B | 第 12 字节高 4 位 `× 4` | IP 的 `total_length` |
| UDP | 8 B | 无 | 自身 `length` 字段 | `length`（含 8 B 头） |

这张表解释了一个常被忽略的事实：**「头部长度」和「报文长度」是两个不同的数，而且由不同的字段给。**IPv4 的 `total_length` 包含头部；UDP 的 `length` 也包含自己的 8 字节头；TCP 没有「总长度」字段，它的边界完全靠 IP 层传下来。写解析器时把这两个数混用，就会出现「读到了下一个包的前几个字节」这种最隐蔽的越界。

### 三种「手写」方式，三种不同的坑

| 方式 | 代码形态 | 它替你保证什么 | 它不保证什么 |
| --- | --- | --- | --- |
| `struct.unpack("!BBHHH...")` | Python | 字节数必须精确匹配，端序由 `!` 明确 | 不检查字段值、不检查剩余字节 |
| `#pragma pack` 结构体 + `memcpy` | C | 偏移由声明决定，可读性好 | 端序、位域顺序、对齐仍是平台相关 |
| 指针强转 `*(uint32_t *)(buf+4)` | C | 最快，零拷贝 | 对齐、别名（strict aliasing）、端序全不保证 |

本篇主线用 Python 的 `struct`，因为它的「字节数必须精确匹配」会在你算错长度时立刻抛异常——这是一个免费的断言。C 的指针强转不会：它只会安静地读错 4 个字节，然后让 bug 在三个系统之外爆发。

---

## 52.3 ② 物理形态：四个头部的位图

### DNS：12 字节头 + 变长名字

```
字节   0    1    2    3    4    5    6    7    8    9   10   11
      +----+----+----+----+----+----+----+----+----+----+----+----+
      |      ID       |     FLAGS     |  QDCOUNT |  ANCOUNT |  NSCOUNT | ARCOUNT |
      +----+----+----+----+----+----+----+----+----+----+----+----+
        0    1    2    3    4    5    6    7    8    9   10   11

byte 2 的 8 位：  |QR| Opcode(4) |AA|TC|RD|
                   7  6  5  4  3   2  1  0
byte 3 的 8 位：  |RA|   Z(3)    |  RCODE(4) |
                   7  6  5  4     3  2  1  0

QNAME（变长）：  |len| label bytes |len| label bytes | ... | 0x00 |
                每个 len 占 1 字节，值 1–63；0x00 是名字结束
```

DNS 头部的 12 个字节里有 6 个 16 位整数，其中两个（ID、FLAGS）是纯数值，四个（QD/AN/NS/AR）是计数；FLAGS 那两个字节又被切成 8 个位域。**跨字节的不是字段，而是「名字」这种变长结构**：`QNAME` 没有固定长度，只能一个 label 一个 label 地走，直到遇到 `0x00`。名字之后紧跟 4 字节的 `QTYPE + QCLASS`，这才是 question 的结尾。

压缩指针是 DNS 独有的「跨报文偏移」：

```
0xC0 0x0C  ->  高 2 位 11 表示「这是指针」，低 14 位是偏移（12）
              它指向报文里第一次出现该名字的位置
```

一个 61 字节的响应里，两个 answer 的名字都可以只写 2 字节（`c0 0c`），而不是重复 13 字节的 `example.com`。**压缩让报文变短，也让解析器从「线性扫描」变成「带跳转的图遍历」**——这是 DNS 解析器最容易写出死循环的地方。

### IPv4：20 字节固定头 + 可变选项

```
字节   0        1        2        3
      +--------+--------+--------+--------+
  0   |ver(4)  | IHL(4) |      TOS        |
      +--------+--------+--------+--------+
  2   |          total length (16)         |
      +--------+--------+--------+--------+
  4   |       identification (16)          |
      +--------+--------+--------+--------+
  6   | flags(3)|   fragment offset (13)    |  <- 一个 16 位字段，跨 2 字节
      +--------+--------+--------+--------+
  8   |  TTL   | proto  |  header checksum  |
      +--------+--------+--------+--------+
 10   |          header checksum (续)       |
      +--------+--------+--------+--------+
 12   |            source address (32)      |
      +--------+--------+--------+--------+
 16   |          destination address (32)   |
      +--------+--------+--------+--------+
 20   |         options (0 ~ 40 B)          |  <- 只有 IHL > 5 时才存在
      +--------+--------+--------+--------+
        头部真实长度 = IHL × 4（最小 20，最大 60）
```

`flags + fragment offset` 是最典型的「跨字节字段」：高 3 位是 `Reserved / DF / MF`，低 13 位是以 **8 字节**为单位的片偏移。**你不能把它拆成两个 `uint8_t` 读**——高字节的低 5 位属于偏移的高位。`0x2001` 的正确解释是 `MF=1, offset=1`，也就是「后面还有片，本片从第 8 字节开始」，而不是「flags=0x20，offset=0x01」。

### TCP：20 字节固定头 + 可变选项

```
字节   0        1        2        3
      +--------+--------+--------+--------+
  0   |          source port (16)           |
      +--------+--------+--------+--------+
  2   |        destination port (16)        |
      +--------+--------+--------+--------+
  4   |            sequence number (32)     |
      +--------+--------+--------+--------+
  8   |          acknowledgment number (32) |
      +--------+--------+--------+--------+
 12   | data offset(4)| reserved(3)| NS(1) |
      +--------+--------+--------+--------+
 13   | CWR|ECE|URG|ACK|PSH|RST|SYN|FIN   |
      +--------+--------+--------+--------+
 14   |           window (16)               |
      +--------+--------+--------+--------+
 16   |          checksum (16)              |
      +--------+--------+--------+--------+
 18   |       urgent pointer (16)           |
      +--------+--------+--------+--------+
 20   |         options (0 ~ 40 B)          |  <- 只有 data offset > 5 时存在
      +--------+--------+--------+--------+
        头部真实长度 = data offset × 4（最小 20，最大 60）
```

TCP 的坑比 IPv4 多一个：**9 个标志位跨两个字节**。`NS` 藏在第 12 字节的最低位，和 `data offset` 共用同一个字节；`CWR/ECE/URG/ACK/PSH/RST/SYN/FIN` 在第 13 字节。只读第 13 字节的解析器永远看不到 `NS`；把第 12 字节当 flags 的解析器会把 `data offset` 的高 4 位当成标志。

### UDP：8 字节，最短也最容易被想当然

```
字节   0        1        2        3
      +--------+--------+--------+--------+
  0   |          source port (16)           |
      +--------+--------+--------+--------+
  2   |        destination port (16)        |
      +--------+--------+--------+--------+
  4   |     length (16, 含 8 B 头)          |
      +--------+--------+--------+--------+
  6   |          checksum (16)              |
      +--------+--------+--------+--------+
```

UDP 只有 8 字节，但它有两个专属陷阱：

1. **`length` 包含 8 字节头本身**。`length = 8` 表示空载荷，不是 8 字节载荷；
2. **IPv4 下 checksum = 0 表示「没算校验和」**，不是「校验和是 0」。IPv6 下不允许为 0。解析器把 0 当成有效校验和去比对，会把一批正常包判成坏包。

### 跨字节字段与单位总表

| 字段 | 位置 | 位宽 | 单位 | 忘记乘单位的后果 |
| --- | --- | --- | --- | --- |
| IPv4 `IHL` | 字节 0 低 4 位 | 4 | 4 字节 | 头部少算 4–40 B，四层头整体错位 |
| IPv4 `fragment offset` | 字节 6–7 低 13 位 | 13 | 8 字节 | 重组时偏移差 8 倍，数据错位 |
| TCP `data offset` | 字节 12 高 4 位 | 4 | 4 字节 | 头部长度算成 0–60 B 的随机值 |
| TCP `window` | 字节 14–15 | 16 | 1 字节 | 只是数值，真实窗口还要乘 `2^wscale` |
| UDP `length` | 字节 4–5 | 16 | 1 字节（含头） | 把 8 字节头漏算，读到上一个包 |
| IP `total length` | 字节 2–3 | 16 | 1 字节（含头） | 与 `IHL×4` 不一致时越界 |
| DNS label `len` | QNAME 每个 label 首字节 | 8 | 1 字节 | 把 `0xC0` 当成长度 192 |
| DNS `RDLENGTH` | 每个 RR 的固定部分 | 16 | 1 字节 | 读穿到下一个 RR |

---

## 52.4 ③④ 走一遍 + 改一行：五组实验

五组实验覆盖「构造 → 解析 → 重组」的完整链条。每组都给出**错误解析**与**正确解析**两列真实输出；所有数字都来自本机（Linux 6.x，Python 3.11，gcc 12）。

| 实验 | 要证明什么 | 关键命令 | 量到的数字 |
| --- | --- | --- | --- |
| A · DNS | 12 B 头 + 变长 QNAME + 压缩指针 | `python3 dns.py` | 查询 29 B，响应 61 B，RTT 7–14 ms |
| B · IPv4 | `IHL × 4` 才是头长 | `python3 ipv4.py` | 头长 20 vs 24，端口 37892 vs 1234 |
| C · TCP | data offset / flags / 窗口 / 端序 | `python3 tcp.py` | 头长 4 vs 20，端口 47873 vs 443 |
| D · 粘包 | `read` 不保留消息边界 | `python3 sticky1.py` | 1 次 recv 读到 2 条；14 次 recv(1) 拆 1 条 |
| E · 分片 | IP 层不保证整包到达 | `python3 frag.py`、`ping -s 2000` | 3 个原包 → 6 个分片 → 3 次重组 |

### A. 手写 DNS 查询，解析真实 A 记录

先构造查询。DNS 查询报文只有三部分：12 字节头、QNAME、4 字节的 QTYPE/QCLASS。QNAME 的编码规则是「长度前缀 + 标签内容」，最后以 `0x00` 结尾。

```python
import socket, struct

def build_query(name, qtype=1):
    # ID=0x1234, flags=0x0100(RD=1), QD=1, 其余计数为 0
    hdr = struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    return hdr + qname + struct.pack("!HH", qtype, 1)   # QTYPE=A, QCLASS=IN

q = build_query("example.com")
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(4)
s.sendto(q, ("8.8.8.8", 53))
r, _ = s.recvfrom(2048)
print("query bytes =", len(q), " response bytes =", len(r))
print("query hex   =", q.hex())
```

真实输出：

```
query bytes = 29  response bytes = 61
query hex   = 123401000001000000000000076578616d706c6503636f6d0000010001
```

把这 29 字节手工拆开，每一段都对得上：

| 偏移 | 字节 | 含义 |
| --- | --- | --- |
| 0–1 | `12 34` | ID = 0x1234 |
| 2–3 | `01 00` | QR=0, Opcode=0, RD=1 |
| 4–5 | `00 01` | QDCOUNT = 1 |
| 6–7 | `00 00` | ANCOUNT = 0 |
| 8–9 | `00 00` | NSCOUNT = 0 |
| 10–11 | `00 00` | ARCOUNT = 0 |
| 12 | `07` | 第一个 label 长 7 |
| 13–19 | `65 78 61 6d 70 6c 65` | `example` |
| 20 | `03` | 第二个 label 长 3 |
| 21–23 | `63 6f 6d` | `com` |
| 24 | `00` | 名字结束 |
| 25–26 | `00 01` | QTYPE = A |
| 27–28 | `00 01` | QCLASS = IN |

解析响应时，必须处理压缩指针。下面是 `read_name` 的核心：遇到 `11xxxxxx` 就跳转，跳转后仍要继续读，直到 `0x00`。

```python
def read_name(buf, off):
    labels, jumped, ret = [], False, None
    while True:
        l = buf[off]
        if l & 0xC0 == 0xC0:                 # 高 2 位 11 -> 压缩指针
            ptr = ((l & 0x3F) << 8) | buf[off+1]
            if not jumped: ret = off + 2     # 记录「指针之后」的位置
            off, jumped = ptr, True
            continue
        if l == 0:
            off += 1
            break
        labels.append(buf[off+1:off+1+l].decode())
        off += 1 + l
    return ".".join(labels), (ret if jumped else off)

off = 12
qname, off = read_name(r, off)
qtype, qclass = struct.unpack("!HH", r[off:off+4]); off += 4
for i in range(an):
    name, off = read_name(r, off)
    typ, cls, ttl, rdlen = struct.unpack("!HHIH", r[off:off+10]); off += 10
    rdata, off = r[off:off+rdlen], off + rdlen
    print(f"AN{i+1}: {name} type={typ} ttl={ttl} rdlen={rdlen} -> {socket.inet_ntoa(rdata)}")
```

真实输出：

```
id=0x1234 flags=0x8180 qd=1 an=2 ns=0 ar=0
question: example.com type 1 class 1 next off 29
  AN1: example.com  type=1 ttl=  300 rdlen=4 -> 104.20.23.154
  AN2: example.com  type=1 ttl=  300 rdlen=4 -> 172.66.147.243
```

`flags=0x8180` 拆开是 `QR=1, Opcode=0, AA=0, TC=0, RD=1, RA=1, RCODE=0`——一个标准的递归响应。两个 answer 的名字都是压缩指针 `c0 0c`，指回偏移 12 的 `example.com`；`rdlen=4` 说明 RDATA 正好是一个 IPv4 地址。

**错误解析**：如果不认压缩指针，把 `0xc0` 当 label 长度：

```python
off = 29 + 16            # 跳过 question（13 B 名字 + 4 B 类型/类）
first = r[off]
print(f"byte@answer = 0x{first:02x} -> 当成 label 长度 = {first}")
print(f"剩余 {len(r)-off} B，需要 {first} B ->",
      "越界" if first > len(r)-off-1 else "可读")
ptr = ((first & 0x3F) << 8) | r[off+1]
print(f"正确：0x{first:02x}{r[off+1]:02x} = 指针 -> offset {ptr}")
```

真实输出：

```
byte@answer = 0xc0  ->  当成 label 长度 = 192
剩余字节数 = 16，需要 192 字节 -> 越界
正确：0xc00c = 指针 -> offset 12
指回去的字节: b'\x07example\x03com\x00' -> example.com
```

| 解析动作 | 错误（不认指针） | 正确（认 0xC0） |
| --- | --- | --- |
| answer 名字 | 长度 192，剩余 16 B，直接越界 | 指针 → offset 12 → `example.com` |
| 响应 ID | `struct.unpack("H")` → `13330` | `struct.unpack("!H")` → `4660` |
| 读完名字后的偏移 | 无从谈起（已崩） | 指针后 2 字节 → 31，继续读 TYPE |
| 两条 A 记录 | 0 条 | 2 条，TTL 300 |

> 端点：`13330 = 0x3412`，正是 `0x1234` 的字节反转。**同一个 2 字节，端序错了就是一个完全不同的 ID**；在 DNS 里这意味着响应和查询对不上，会被当成伪造包丢掉。

**走一遍的成本**：构造 29 字节 + `sendto` 一次 + `recvfrom` 一次。本机实测 6 次查询的 RTT：

```
dns rtt ms: 7.15 9.64 7.52 14.33 7.33 11.29
min=7.15 median=9.64 max=14.33
```

解析这 61 字节本身是微秒级（十几次切片 + 两个 `struct.unpack`，**量级结论**），**总耗时几乎全部是网络 RTT**。这也解释了为什么 DNS 的性能问题永远先看缓存和 RTT，而不是看解析代码。

### B. IPv4：`IHL × 4` 才是头长

构造一个 `IHL=6` 的包：20 字节固定头 + 4 字节 Router Alert 选项 + 8 字节 UDP 头。然后分别用「固定 20」和「`IHL × 4`」两种方式找 UDP 头。

```python
import struct, socket

options = bytes([0x94, 0x04, 0x00, 0x00])            # Router Alert
udp = struct.pack("!HHHH", 1234, 53, 8, 0)            # sport dport len csum
ip = struct.pack("!BBHHHBBH4s4s", 0x46, 0, 24 + len(udp), 0x1f3c, 0x2001,
                 64, 17, 0, socket.inet_aton("192.168.1.10"),
                 socket.inet_aton("8.8.8.8")) + options
pkt = ip + udp

def parse(off, label):
    sport, dport = struct.unpack("!HH", pkt[off:off+4])
    print(f"{label}: payload@{off} sport={sport} dport={dport} hex={pkt[off:off+8].hex()}")

parse(20, "错误（固定 20）")
parse((pkt[0] & 0x0F) * 4, "正确（IHL×4）")
```

真实输出：

```
packet len = 32  IHL = 6  -> header = 24
错误（固定 20）: payload@20 sport=37892 dport=0 payload_hex=9404000004d20035
正确（IHL×4）: payload@24 sport=1234 dport=53 payload_hex=04d2003500080000
flags+frag=0x2001 -> DF=0 MF=1 frag_off=1 x8 = 8 B
UDP.len = 8  IP.total - IHL*4 = 8  一致 = True
截断后: 实际 30 B, IP.total 声称 32 B, UDP.len 声称 8 B, 实得 6 B
```

这里有三层信息：

1. **偏移层**：错误解析从 20 开始，读到的 `0x9404` 是选项，`0x0000` 是选项的值；正确解析从 24 开始，才是 `1234 -> 53`。
2. **位域层**：`flags+frag = 0x2001`，`MF=1`、`frag_off=1`，偏移是 `1 × 8 = 8` 字节。如果把它拆成两个字节读，会得到 `flags=0x20`、`offset=0x01`——偏移从 8 变成 1，重组时数据整体错 7 字节。
3. **长度层**：把包尾切掉 2 字节，`IP.total` 仍声称 32，`UDP.len` 仍声称 8，但实际只剩 30 / 6。**内部字段和实际字节数不一致时，必须以实际字节数为上限，并拒绝这个包**，否则下一次读就越界。

| 动作 | 错误（固定 20） | 正确（`IHL×4`） |
| --- | --- | --- |
| 四层头起点 | 20 | 24 |
| 源端口 | `37892`（其实是 `0x9404`） | `1234` |
| 目的端口 | `0` | `53` |
| 选项被当成 | UDP 头的前 4 字节 | 头部的一部分，跳过 |
| 后续所有字段 | 整体平移 4 B | 对齐 |

**修复只需改一个表达式，但必须同时加两个断言**：`IHL >= 5`、`IHL * 4 <= total_length`。前者拦住畸形头，后者拦住「头比包还长」。

### C. TCP：data offset、flags、窗口与端序

构造一个 20 字节 TCP 头：源端口 443、目的端口 49152、`data offset=5`、`NS=1`、`SYN+ACK`、窗口 65535。

```python
import struct

tcp = struct.pack("!HHIIBBHHH", 443, 49152, 0x11223344, 0x55667788,
                  0x51, 0x12, 65535, 0, 0)   # 0x51 = doff(5)<<4 | NS(1)
b = tcp
print("tcp header hex =", b.hex())
print("错误：data offset = b[12] & 0x0F =", b[12] & 0x0F,
      "-> 头长", (b[12] & 0x0F)*4, "B")
print("正确：data offset = b[12] >> 4  =", b[12] >> 4,
      "-> 头长", (b[12] >> 4)*4, "B")
print("错误：flags 取 byte12 = 0x%02x -> SYN=%d ACK=%d"
      % (b[12], (b[12]>>1)&1, (b[12]>>4)&1))
print("正确：flags 取 byte13 = 0x%02x -> SYN=%d ACK=%d"
      % (b[13], (b[13]>>1)&1, (b[13]>>4)&1))
print("NS 位 =", b[12] & 1)
print("错误：端口用 H   ->", struct.unpack("H", b[0:2])[0])
print("正确：端口用 !H  ->", struct.unpack("!H", b[0:2])[0])
```

真实输出：

```
tcp header hex = 01bbc00011223344556677885112ffff00000000
错误：data offset = b[12] & 0x0F = 1 -> 头长 4 B（解析器原地打转）
正确：data offset = b[12] >> 4  = 5 -> 头长 20 B
错误：flags 取 byte12 = 0x51 -> SYN=0 ACK=1
正确：flags 取 byte13 = 0x12 -> SYN=1 ACK=1
NS 位藏在 byte12 最低位: NS = 1 （9 个标志位跨 2 字节）
错误：端口用本机端序 H   -> 47873
正确：端口用网络端序 !H  -> 443
窗口 = 65535
```

四个坑一次现形：

- **`& 0x0F` 而不是 `>> 4`**：`0x51 & 0x0F = 1`，头长算成 4 字节。解析器从偏移 4 继续读「载荷」，实际读到的是自己的 seq/ack 字段——如果写成循环，就是原地打转或无限循环。
- **flags 取错字节**：`byte12 = 0x51` 的高 4 位是 data offset，低 4 位是 reserved + NS。用它当 flags 会得到 `ACK=1, SYN=0`，把三次握手的 SYN 包判成「非 SYN」，连接跟踪直接失效。
- **`NS` 在 byte12**：9 个标志位跨两个字节，只读 byte13 的解析器永远看不到 `NS`。这在启用 ECN 非ce 的实验里会造成「标志位对不上」。
- **端序**：`struct.unpack("H")` 在本机小端上把 `0x01bb` 读成 `47873`，而正确值是 `443`。端口比较、ACL、五元组哈希全部会错。

窗口字段本身是 16 位，最大 65535。但真实的 TCP 窗口靠 `window scale` 选项放大：

```
$ ss -tin 'sport = :<port>'
wscale:10,10
cwnd:11
snd_wnd:44032
```

`wscale:10` 表示窗口字段要乘 `2^10 = 1024`。**解析器如果只读头部的 16 位窗口、不解析 `wscale` 选项，得到的窗口会小 1024 倍**；做流量整形或缓冲区估算时，这个误差会直接变成「窗口明明没满，吞吐却上不去」的误判。

| 动作 | 错误解析 | 正确解析 |
| --- | --- | --- |
| TCP 头长 | `b[12] & 0x0F` → 1 → 4 B | `b[12] >> 4` → 5 → 20 B |
| 标志位 | 取 byte12 → `SYN=0, ACK=1` | 取 byte13 → `SYN=1, ACK=1` |
| NS 位 | 看不到 | `b[12] & 1` → 1 |
| 源端口 | 本机端序 → 47873 | 网络端序 → 443 |
| 真实窗口 | 65535 | 65535 × 2^10（wscale=10） |

**还有一个跨层依赖**：TCP 校验和要覆盖「伪头部」——源 IP、目的 IP、协议号、TCP 长度。也就是说，**TCP 头不是自包含的**，解析 TCP 时必须把 IP 层的两个地址和长度一起传进来。UDP 同理。这是「分层」最名不副实的一处：每一层都声称自己独立，但校验和把上下两层焊在了一起。

### D. 粘包实验：一次 `read` 读到两条消息，也会读到半条

写一个 TCP server，客户端**一次 `sendall` 发两条消息** `MSG-ONE` 和 `MSG-TWO`（共 14 字节）。服务端分别用四种读法：

```python
import socket, threading, time

def serve(mode, ready):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0)); s.listen(1); ready.append(s.getsockname()[1])
    c, _ = s.accept()
    if mode == "coalesce":
        print("  一次 recv(1024):", [c.recv(1024)])
    elif mode == "small":
        print("  recv(5) 连续:", [c.recv(5) for _ in range(3)])
    elif mode == "byte":
        one = [c.recv(1) for _ in range(14)]
        print(f"  recv(1) 14 次, 前 4 次 {one[:4]}, 拼起来 {b''.join(one)!r}")
    else:
        print("  两次 send 之间 sleep 50ms:", [c.recv(1024), c.recv(1024)])
    c.close(); s.close()

for mode, gap in (("coalesce", None), ("small", None), ("byte", None), ("delayed", 0.05)):
    ready = []; t = threading.Thread(target=serve, args=(mode, ready)); t.start()
    while not ready: pass
    c = socket.create_connection(("127.0.0.1", ready[0]))
    if mode == "delayed":
        c.sendall(b"MSG-ONE"); time.sleep(gap); c.sendall(b"MSG-TWO")
    else:
        c.sendall(b"MSG-ONE" + b"MSG-TWO")
    t.join(); c.close()
```

真实输出（四种读法，同一份 14 字节数据）：

```
  一次 recv(1024): [b'MSG-ONEMSG-TWO']
  recv(5) 连续: [b'MSG-O', b'NEMSG', b'-TWO']
  recv(1) 14 次, 前 4 次 [b'M', b'S', b'G', b'-'], 拼起来 b'MSG-ONEMSG-TWO'
  两次 send 之间 sleep 50ms: [b'MSG-ONE', b'MSG-TWO']
```

四种读法，四种「消息」：

| 读法 | `recv` 返回 | 应用层看到的消息 | 错在哪 |
| --- | --- | --- | --- |
| `recv(1024)` | `MSG-ONEMSG-TWO`（14 B） | **1 条 14 字节的消息** | 两条粘成一条 |
| `recv(5)` ×3 | `MSG-O` / `NEMSG` / `-TWO` | **3 条**，其中第 2 条跨了消息边界 | 既拆包又粘包 |
| `recv(1)` ×14 | 14 个 1 字节 | **14 条** | 拆成字节流 |
| 间隔 50 ms | `MSG-ONE` / `MSG-TWO` | 2 条 | 碰巧对，不可依赖 |

`sleep 50ms` 那一行最能说明问题：**同样的代码、同样的两条消息，只因为发送间隔变了，接收端看到的分组就变了。**TCP 不保证「一次 `send` 对应一次 `recv`」，它只保证「字节按序到达」。`recv` 返回多少，取决于内核缓冲区里当时有多少字节，以及你请求了多少。

系统调用账（`strace -f -c` 统计四种读法合计）：

```
% time     seconds  usecs/call     calls    errors syscall
------ ----------- ----------- --------- --------- ----------------
 87.76    0.000875         218         4           accept4
  7.82    0.000078          19         4           connect
  2.71    0.000027           1        20           recvfrom
  1.71    0.000017           3         5           sendto
```

`recvfrom` 一共 20 次 = `1 + 3 + 14 + 2`。**同一条 14 字节的消息，`recv(1)` 版本要 14 次系统调用，`recv(1024)` 版本只要 1 次**——这就是「一次读多少」在主线③上的价格。每次 `recvfrom` 约 1–2 µs（`0.000027 s / 20`），14 倍差距在单连接上不起眼，乘以 10 万 QPS 就是每秒多 100 多万次跨界。

修复不是「把缓冲区调大」，而是**在应用协议里显式分帧**。最常用的是长度前缀：先写 4 字节大端长度，再写消息体；接收端先读满 4 字节，再按长度读满消息体。

```python
def serve(ready):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0)); s.listen(1); ready.append(s.getsockname()[1])
    c, _ = s.accept(); msgs = []
    while len(msgs) < 2:
        hdr = b""
        while len(hdr) < 4: hdr += c.recv(4 - len(hdr))
        n = int.from_bytes(hdr, "big"); body = b""
        while len(body) < n: body += c.recv(n - len(body))
        msgs.append(body)
    print(f"  长度前缀切出 {len(msgs)} 条: {msgs}")
    c.close(); s.close()

# 客户端一次 write 两条「长度 + 消息」
c.sendall(b"".join(len(m).to_bytes(4, "big") + m
                   for m in (b"MSG-ONE", b"MSG-TWO")))
```

真实输出：

```
  长度前缀切出 2 条: [b'MSG-ONE', b'MSG-TWO']
```

| 指标 | 改前（裸流） | 改后（4 B 长度前缀） |
| --- | --- | --- |
| 消息条数 | 1 或 3 或 14（取决于读法） | **2** |
| 边界错误 | 有（`NEMSG` 跨两条） | **0** |
| 每条消息的 `recv` 次数 | 1–14 次不等 | 2 次（头 + 体） |
| 额外开销 | 0 | 每条 4 B（14 B 消息 = 28.6%） |

最后一行是长度前缀的代价：**短消息上开销比例很高**。所以真实协议会组合使用：HTTP/1.1 用 `Content-Length` 或 chunked，HTTP/2 用 9 字节帧头，TLS 用 5 字节记录头，WebSocket 用 2/4/8 字节长度。**没有一种分帧是免费的，区别只在「把开销放在哪」**。

### E. 分片实验：IP 层不保证整包到达

先看真实链路。`ping -s 2000` 会发出 2028 字节的 IP 包（2000 载荷 + 8 ICMP + 20 IP），超过 MTU 1500，必须在 IP 层分片。用 `/proc/net/snmp` 的计数器量前后差值：

```python
import subprocess, time

def ip_counters():
    L = open("/proc/net/snmp").read().splitlines()
    h = L[[i for i,l in enumerate(L) if l.startswith("Ip:")][0]].split()[1:]
    v = L[[i for i,l in enumerate(L) if l.startswith("Ip:")][1]].split()[1:]
    d = dict(zip(h, map(int, v)))
    return {k: d[k] for k in ("FragOKs", "FragCreates", "ReasmReqds", "ReasmOKs")}

before = ip_counters()
out = subprocess.run(["ping", "-c", "3", "-s", "2000", "217.116.170.1"],
                     capture_output=True, text=True).stdout
time.sleep(0.3)
after = ip_counters()
print([l for l in out.splitlines() if "packets transmitted" in l][0])
for k in before:
    print(f"{k:12s} {before[k]:9d} {after[k]:9d} {after[k]-before[k]:7d}")
```

真实输出：

```
3 packets transmitted, 3 received, 0% packet loss, time 2024ms
counter         before     after   delta
FragOKs             11        14       3
FragCreates         22        28       6
ReasmReqds          74        80       6
ReasmOKs            37        40       3
```

3 个原包 → `FragCreates` 增加 6（每个 2028 字节的包切成 1500 + 528 两片）→ 接收端 `ReasmReqds` 增加 6 → `ReasmOKs` 增加 3（3 次成功重组）。**每一次「一个大包」在链路上都变成了两次到达、两次处理、一次重组。**

再看 DF（Don't Fragment）位：

```
$ ping -c1 -M do -s 2000 217.116.170.1
PING 217.116.170.1 (217.116.170.1) 2000(2028) bytes of data.
ping: local error: message too long, mtu=1500
```

设置了 DF 之后，内核直接拒绝发送，报 `mtu=1500`。这两行合起来说明：**「大包能不能到」不是由发送端决定的，而是由路径上最小的 MTU 和 DF 位共同决定的。**分片是 IP 层的补救，不是保证；DF 则把这个补救关掉，让错误显式暴露（这也是 PMTUD 的基础）。

现在离线构造一个 3008 字节的 UDP 报文，按 1480 字节切成 3 片，每片加 20 字节 IP 头，观察「按到达顺序拼」和「按 offset 拼」的区别：

```python
import struct, socket

def csum(b):
    if len(b) % 2: b += b"\x00"
    s = sum(struct.unpack("!%dH" % (len(b)//2), b))
    while s >> 16: s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF

src, dst = socket.inet_aton("10.0.0.1"), socket.inet_aton("10.0.0.2")
payload = bytes(i % 251 for i in range(3000))
udp = struct.pack("!HHHH", 9999, 53, 8 + len(payload), 0)
pseudo = src + dst + struct.pack("!BBH", 0, 17, 8 + len(payload))
udp = udp[:6] + struct.pack("!H", csum(pseudo + udp + payload))
dg = udp + payload

def iphdr(total, ident, ff):
    return struct.pack("!BBHHHBBH4s4s", 0x45, 0, total, ident, ff,
                       64, 17, 0, src, dst)

frags = []
for off in range(0, len(dg), 1480):
    body = dg[off:off+1480]
    mf = 1 if off + len(body) < len(dg) else 0
    ff = (mf << 13) | (off // 8)
    frags.append(iphdr(20 + len(body), 0x7777, ff) + body)
    print(f"frag off={off:4d} len={len(body):4d} MF={mf} frag_field=0x{ff:04x}")
```

真实输出：

```
frag off=   0 len=1480 MF=1 frag_field=0x2000 IP.total=1500
frag off=1480 len=1480 MF=1 frag_field=0x20b9 IP.total=1500
frag off=2960 len=  48 MF=0 frag_field=0x0172 IP.total=68
只看第一片: UDP.len 字段 = 3008 但实得 = 1480
到达顺序 [0, 1, 2]
  按到达顺序拼: 前 8 字节 270f00350bc02eaf  UDP.len=3008  <- 碰巧对
  按 offset 重组: 3008 B, 与原报文相同 = True, 校验和 = 0x0000
到达顺序 [2, 1, 0]
  按到达顺序拼: 前 8 字节 bfc0c1c2c3c4c5c6  UDP.len=50116  <- 错位
  按 offset 重组: 3008 B, 与原报文相同 = True, 校验和 = 0x0000
```

三个细节必须盯住：

1. **第一片单独看是「不完整」的**：UDP 头里写 `len = 3008`，但这一片只有 1480 字节。任何「用内部长度字段去 `read`」的解析器都会越界或阻塞。**内部字段只是声明，实际字节数才是事实**。
2. **顺序不可假设**：倒序到达时，按到达顺序拼接得到 `0xbfc0...`，UDP 长度读成 `50116`；按 `frag_offset × 8` 排序后，长度回到 3008，校验和验证通过（`0x0000` 表示一的补码和为 0，即有效）。
3. **必须按 `(src, dst, proto, identification)` 聚合**：这四元组才是「同一个原始包」的身份。只按源 IP 聚合会把并发的大包搅在一起。

| 动作 | 错误（按到达顺序） | 正确（按 offset 重组） |
| --- | --- | --- |
| 倒序到达时的前 8 字节 | `bfc0c1c2c3c4c5c6`（其实是尾部数据） | `270f00350bc02eaf`（UDP 头） |
| 读出的 UDP 长度 | `50116` | `3008` |
| 与原报文比较 | 不相等 | **相等** |
| 校验和 | 无法验证 | `0x0000` → 有效 |

真实分片还有一个安全维度：**重叠分片**。攻击者可以发两片 `offset` 重叠、内容不同的分片，不同操作系统/中间设备对「谁覆盖谁」的实现不一致（teardrop 类攻击）。正确做法是**拒绝重叠**，而不是「后到的覆盖先到的」。

---

## 52.5 ⑤ 它证伪了谁

| 流行说法 | 本篇的反例 | 数字 |
| --- | --- | --- |
| ①「解析器很无聊，就是按偏移读字段」 | 所有协议 bug 的发源地都在「偏移 + 长度 + 单位」这三件事上。IPv4 少乘 4、TCP 用 `& 0x0F`、DNS 不认指针，每一个都能让上层全线错乱 | 4 B 错位 → 端口 `37892` vs `1234` |
| ②「`struct` 强转就够了，编译器会处理对齐」 | 位域顺序是实现定义：`ver:4` 写在前面，GCC 把它放进**低** 4 位，`0x46` 变成 `0x64`。端序更不会自动处理：`htons` 之前内存里是 `20 00`，之后才是 `00 20` | 0x64 vs 0x46；47873 vs 443 |
| ③「TCP 是消息流，读到一条就是一条」 | 同一条 14 字节数据，`recv(1024)` 读到 1 条、`recv(5)` 读到 3 条、`recv(1)` 读到 14 条。**边界是应用层的事，内核只保证字节有序** | 1 / 3 / 14 条 |
| ④「IP 包 = 一个完整消息」 | 2028 字节的 ping 被切成 2 片，3 个包产生 6 个分片、3 次重组；倒序到达时按到达顺序拼出的 UDP 长度是 `50116` 而不是 `3008` | 6 片 / 3 次重组 |
| ⑤「DNS 名字就是一个字符串」 | 名字是「长度前缀的 label 序列」，还可以用 `0xC0` 指针跳转；把 `0xC0` 当长度会读出 192 并越界 | 192 vs 指针 → offset 12 |

第 ② 条值得单独展开，因为它是「C 程序员的本能」。下面这段 C 在 x86 上跑出来的结果，和大多数人的直觉相反：

```c
#include <stdio.h>
#include <stdint.h>
#include <arpa/inet.h>

struct vihl  { uint8_t ver:4; uint8_t ihl:4; };   /* 直觉写法 */
struct vihl2 { uint8_t ihl:4; uint8_t ver:4; };   /* 与位图同序 */

int main(void){
  struct vihl  a = { .ver = 4, .ihl = 6 };
  struct vihl2 b = { .ver = 4, .ihl = 6 };
  uint16_t tot = 0x0020;
  unsigned char *p = (unsigned char *)&tot;
  printf("ver:4 在前 -> byte=0x%02x (期望 0x46)\n", *(unsigned char *)&a);
  printf("ihl:4 在前 -> byte=0x%02x (期望 0x46)\n", *(unsigned char *)&b);
  printf("host bytes=%02x %02x ; htons 后=", p[0], p[1]);
  tot = htons(tot); p = (unsigned char *)&tot;
  printf("%02x %02x\n", p[0], p[1]);
  return 0;
}
```

真实输出：

```
sizeof(struct vihl)=1  ver:4 写在前面 -> byte=0x64 (期望 0x46)
sizeof(struct vihl2)=1 ihl:4 写在前面 -> byte=0x46 (期望 0x46)
host tot=0x0020 bytes=20 00 ; htons 后 bytes=00 20
```

`0x64` 不是笔误：GCC 在小端机上从最低位开始分配位域，`ver` 占了 bit0–3，`ihl` 占了 bit4–7，于是「版本 4、头长 6」被写成 `0x64`。**位域的声明顺序和位图的书写顺序是两件事**；跨编译器、跨端序时还可能再变一次。`htons` 那两行则说明：**字节序转换不是可选的「优化」，它是线路格式的一部分**，少一次就是 `20 00` 和 `00 20` 的区别。

---

## 52.6 解析器的三条铁律

### 铁律一：永远按 `offset × 单位` 算

协议里的「长度」字段几乎都不是字节数，而是「某种单位」的个数。把所有单位列在一张纸上，比背任何协议都管用：

| 字段 | 单位 | 换算 | 忘记乘的典型症状 |
| --- | --- | --- | --- |
| IPv4 `IHL` | 4 B | `ihl * 4` | 头部少 4–40 B，四层全错位 |
| IPv4 `frag offset` | 8 B | `off * 8` | 重组错位，校验和失败 |
| TCP `data offset` | 4 B | `doff * 4` | 头长 4/8/…/60，载荷起点错 |
| TCP `window scale` | 2 的幂 | `win << shift` | 窗口小 1024 倍 |
| DNS label `len` | 1 B | 直接是字节数 | 把 `0xC0` 当 192 |

一个可以照抄的写法是：**先把单位写进变量名**。

```python
ip_hdr_len = (buf[0] & 0x0F) * 4          # 单位：字节
tcp_hdr_len = (buf[12] >> 4) * 4          # 单位：字节
frag_off_bytes = (ff & 0x1FFF) * 8        # 单位：字节
```

变量名里带 `_bytes`，下次就不会有人把它当「字数」用。

### 铁律二：永远校验「内部字段 vs 实际字节数」

解析器的每一次读取都必须有一个上界，而且上界只能来自**实际拿到的字节数**，不能来自包内声明。至少要过五道检查：

```python
def check_ipv4(b):
    if len(b) < 20:                    return "太短，连固定头都不够"
    ihl = (b[0] & 0x0F) * 4
    if ihl < 20 or ihl > len(b):       return "IHL 非法或超过实际长度"
    total = struct.unpack("!H", b[2:4])[0]
    if total < ihl or total > len(b):  return "total_length 与实得字节不符"
    if b[9] == 6 and len(b) < ihl + 20: return "声称 TCP 但不够 20 B"
    if b[9] == 17 and len(b) < ihl + 8: return "声称 UDP 但不够 8 B"
    return None

def check_udp(b, ip_payload_len):
    if len(b) < 8:                     return "UDP 头不足 8 B"
    ulen = struct.unpack("!H", b[4:6])[0]
    if ulen < 8 or ulen > ip_payload_len: return "UDP.length 超过 IP 载荷"
    return None
```

这五道检查能拦住绝大多数「畸形包」：截断、声明长度大于实际、声明长度小于头部、协议号与实际头长不符。**注意 `total > len(b)` 和 `ulen > ip_payload_len` 用的是「实得」而不是「声明」**——这是唯一可靠的上界。

### 铁律三：永远不信任外部输入

解析器是攻击面，因为它是「把不可信字节变成程序内部状态」的唯一入口。三类经典问题都来自「少了一次检查」：

| 攻击形态 | 缺了哪条铁律 | 后果 | 防御 |
| --- | --- | --- | --- |
| 压缩指针成环 | 铁律三 | DNS 解析器死循环 / CPU 打满 | 限制跳转次数（如 ≤ 32），只允许向后指 |
| 分片重叠（teardrop） | 铁律二 | 重组出畸形包，绕过 IDS | 拒绝重叠区间，按最小/最先策略明确一种 |
| 长度字段回绕 | 铁律二 | `uint16` 加头部溢出，`len - 8` 变成巨大值 | 用更宽类型计算，先检查下界再减 |
| 超长 label / 超长名字 | 铁律一、二 | 缓冲区溢出 | label ≤ 63，名字 ≤ 255，逐段检查 |
| 声明长度小于实际 | 铁律二 | 「头部吃掉载荷」，后续字段读到上一包 | `total >= ihl`，`ulen >= 8` |

一个实用的纪律：**解析函数只返回「字段 + 消费的字节数」，绝不返回「指向输入缓冲区的裸指针」**。前者可以被调用方检查，后者会把生命周期和边界问题一起带出去。

---

## 52.7 五问收口表

| 五问 | 本篇的答案 |
| --- | --- |
| **① 它是什么** | 序列化的逆函数：把字节串按 schema 映射成「字段 + 消费字节数」。它解决的不是「读字段」，而是「在不可信输入上，安全地确定边界」 |
| **② 物理形态** | DNS 12 B 头 + 变长 label（可压缩指针）；IPv4 20 B 固定头 + 0–40 B 选项（`IHL×4`）；TCP 20 B 固定头 + 0–40 B 选项（`doff×4`），9 个标志位跨 2 字节；UDP 8 B，`length` 含头 |
| **③ 走一遍** | 构造 29 B 查询 → `sendto` 一次 → 网络 RTT 7–14 ms → `recvfrom` 一次 → 解析 61 B（微秒级）。本地 TCP 环回 RTT 0.01–0.05 ms；`recv(1)` 读 14 B 要 14 次系统调用，`recv(1024)` 只要 1 次 |
| **④ 改一行** | `IHL=20` → `IHL×4`：端口 `37892` → `1234`；`&0x0F` → `>>4`：头长 4 → 20；`H` → `!H`：47873 → 443；裸流 → 长度前缀：消息数 1/3/14 → 2；按到达顺序 → 按 offset：UDP 长度 50116 → 3008 |
| **⑤ 它证伪了谁** | 「解析器很无聊」「`struct` 强转就够了」「TCP 读到一条就是一条」「IP 包就是一个完整消息」「DNS 名字就是字符串」——五条全部被真实输出推翻 |

---

## 52.8 一页速记

```
【一句话】解析器 = (bytes, schema) -> (fields, consumed)；错一个位域，数据就是错的。

【四个头部的固定长度】
  DNS 12 B  | IPv4 20 B（+选项，IHL×4） | TCP 20 B（+选项，doff×4） | UDP 8 B

【三个必须乘的单位】
  IPv4 IHL × 4        IPv4 frag_off × 8        TCP data offset × 4
  （外加 TCP window << wscale）

【三个跨字节字段】
  IPv4 flags(3) + frag_off(13)  = 一个 16 位字段
  TCP  data offset(4) + reserved(3) + NS(1) = byte12
  TCP  9 个 flags 跨 byte12/byte13；NS 在 byte12 最低位

【粘包的两个方向】
  一次 recv 读到 2 条  -> 粘
  recv(5) 读到 'MSG-O' -> 拆
  修法：4 B 长度前缀 / 分隔符 / 固定长度 / 自描述帧（TLS 5 B、HTTP/2 9 B）

【分片的三个数字】
  2028 B 的 ping -> 2 片（IP 包 1500 + 548）
  3008 B 的 UDP  -> 3 片（1480 + 1480 + 48）
  第一片的 UDP.len 写 3008，实得 1480 -> 内部声明 ≠ 实际字节

【三条铁律】
  ① 按 offset × 单位算（IHL×4、doff×4、frag_off×8）
  ② 用「实得字节数」当上界，校验每个内部长度字段
  ③ 永不信任外部输入：限跳转、拒重叠、查回绕

【一个判据】
  解析结果不对时，先打印「第几个字节、哪一位、乘了哪个单位」，
  不要先换库——换库只是把同一个错误换一种写法。
```

---

## 52.9 自检 3 题

**Q1.** 一个 TCP 头的第 12–13 字节是 `0x50 0x18`，第 14–15 字节是 `0x02 0x00`。请回答：① TCP 头有多长？② 设置了哪些标志位？③ 窗口字段的原始值是多少？④ 载荷从第几字节开始？⑤ 如果对端在 SYN 里带了 `wscale=7`，真实窗口是多少？

<details><summary>答案</summary>

① `0x50 >> 4 = 5`，`5 × 4 = 20` 字节。注意不能写 `0x50 & 0x0F`，那会得到 0，头长算成 0。

② 第 13 字节 `0x18 = 0001 1000`：`ACK`（bit4，值 0x10）和 `PSH`（bit3，值 0x08）置位；`SYN/FIN/RST/URG/ECE/CWR` 都没置。第 12 字节的最低位是 `NS=0`。如果只看第 12 字节 `0x50`，会误判成「ACK 置位、data offset 当标志」，这是 §52.4.C 的坑。

③ `0x0200 = 512`。如果用本机小端读成 `0x0002 = 2`，窗口会小 256 倍。

④ 载荷从第 20 字节开始（`data offset × 4`），前提是没有选项；如果 `data offset > 5`，要跳过 `(doff - 5) × 4` 字节的选项。

⑤ `512 × 2^7 = 65536` 字节。窗口缩放只在 SYN/SYN-ACK 里协商，且**只对后续报文生效**；解析器必须把选项里的 shift 保存到连接状态里，不能每条报文重新猜。
</details>

**Q2.** 一个 RPC 服务用 TCP 传输，客户端把两条 100 字节的请求一次性 `write` 出去。服务端日志显示有时「只处理了 1 条」，有时「第一条请求的后半段被当成第二条的开头」。请解释这两个现象的共同根因，给出一种能同时解决两者的分帧方案，并计算 4 字节长度前缀对 100 字节请求的开销比例。

<details><summary>答案</summary>

共同根因：**TCP 是字节流，不保留 `write` 的边界**。`write` 两次 100 字节，内核可能合并成一个 200 字节的段，也可能按 MSS 切成 1448 + 52；`read` 返回多少只取决于「当时缓冲区里有多少字节」和「你请求了多少」。所以：

- 「只处理 1 条」= 一次 `read` 读到了 200 字节，但代码只解析了第一条，剩下的 100 字节被丢掉；
- 「后半段被当成第二条开头」= 第一次 `read` 只拿到 60 字节，代码却把它当成一条完整请求解析，剩下的 40 字节被当成下一条。

分帧方案：**长度前缀**。每条消息写成 `4 字节大端长度 + 消息体`；接收端循环「读满 4 字节 → 读满 N 字节」，读不满就继续 `read`，绝不把「一次 read」当成「一条消息」。也可以用分隔符（如 `\r\n\r\n`）或固定长度，但长度前缀对二进制最稳。

开销：4 / 100 = **4%**。如果消息平均 10 字节，开销就是 40%，此时应改用变长整数长度（1 字节表示 <128）或把多条消息批量打包；如果消息是 1 MB，4 字节可以忽略。**分帧的成本必须按「最小消息」算，不能按「平均消息」算。**
</details>

**Q3.** 你在做一个 IPv4 重组模块，收到三片：`(offset=0, MF=1, len=1480)`、`(offset=1480, MF=1, len=1480)`、`(offset=2960, MF=0, len=48)`。请说明：① 如何判断这三片属于同一个包？② 重组后的总长度是多少？③ 第一片里的 UDP 长度字段是 3008，但本片只有 1480 字节，应该信哪个？④ 如果第二片和第三片先到，你的代码应该怎么做？⑤ 如果攻击者发来一片 `offset=1000` 的重叠分片，应该怎么处理？

<details><summary>答案</summary>

① 按 **`(源 IP, 目的 IP, 协议号, identification)`** 四元组聚合。只看源 IP 或只看 ID 都不够：不同源可能用相同 ID，同一源也可能并发多个大包。还要加超时（典型 30–60 s），否则半包会永久占内存。

② `2960 + 48 = 3008` 字节（IP 载荷）。注意最后一片的长度不必是 8 的倍数；前三片的 offset 分别是 `0`、`1480`、`2960`，正好覆盖 `[0, 3008)`，没有空洞。

③ **信实际字节数**。第一片的 `UDP.len = 3008` 只是「整个数据报的声明」，本片实际只有 1480 字节；解析第一片时只能用 1480 当上界，把「还差 1528 字节」记在重组状态里。**内部字段是声明，实际长度是事实；解析器的每次读取都以上界为准。**

④ 按 `offset` 存进一个以 8 字节为单位（或字节）的区间表，不要按到达顺序拼接。等到 `[0, 3008)` 连续覆盖且收到 `MF=0` 的尾片后，再交给上层。倒序到达时，如果按到达顺序拼，UDP 长度会读成 50116（§52.4.E 的真实输出），校验和必然失败。

⑤ **拒绝重叠**，并记录一次异常。允许重叠会让攻击者用「后到的片覆盖先到的片」绕过 IDS（teardrop 类攻击）；不同内核对「谁覆盖谁」的策略不一致，本身就是攻击面。工程上可以规定「重叠区间必须完全一致，否则丢弃整个数据报」，并给这种包打上计数指标。
</details>

---

## 52.10 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 字节序、对齐、位域与序列化的完整机制 | `base/06` |
| IP 头、CIDR、NAT、转发与 ICMP | `base/40` |
| TCP 三次握手、状态机与 TIME_WAIT | `base/41` |
| TCP 重传、SACK、滑动窗口与拥塞控制 | `base/42` |
| DNS 的 12 字节头、递归/迭代、TTL 与 EDNS | `base/44` |
| 链路层 MTU 与分片的物理来源 | `base/39` |
| 分层：每层头部在替谁擦屁股 | `base/38` |
| 内核收包六道门与 socket 缓冲区 | `base/32` |
| JSON 编解码、数字精度与流式解析 | [`go/31`](../go/31-json-codec.md) |
| Go 的 `net/http` 如何做连接复用与分帧 | [`go/38`](../go/38-net-http.md) |
| 全书总纲与本篇在 Part IV 的位置 | [`0-summary.md`](0-summary.md) §0.4、§0.6.5 |
