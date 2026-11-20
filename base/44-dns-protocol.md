# 44. DNS 协议剖析：12 字节头部、递归 vs 迭代、TTL 缓存与 EDNS

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 网络：从一根网线到一张证书 ｜ **编号**：44 / 56
> **本篇定位**：Part IV 的第七篇。38 讲分层，40 讲 IP 只承诺尽力送达，41/42 讲 TCP 怎么把不可靠信道补成可靠流，43 讲 UDP 与 QUIC；本篇讲一个**只跑在 UDP 上、固定头部只有 12 字节、却撑起全球命名**的协议。全篇只有一条推论链——`名字必须让任何人在任何地点都能解析` → `所以数据必须分层委派、逐级缓存` → `所以每条记录必须带 TTL` → `所以「改了不生效」是设计的必然，不是故障`。`base/45` 接着讲这个协议天生缺的那三样（认证、完整性、机密性）怎么被攻击、又怎么被补；`base/52` 会用一个手写解析器把本篇的报文再过一遍。
> **读法**：§44.2 的位图与 §44.3 的十六进制 dump 是全篇支点，建议对着 `dig` 的输出一个字节一个字节对一遍；§44.5 的递归/迭代图与 glue 那段解释「为什么 13 组根服务器够用」；§44.6 的三条「改了不生效」可以直接当排障卡；赶时间只读 §44.9、§44.11、§44.12。
> **不在本篇**：缓存投毒、Kaminsky、DNSSEC 的密码学细节、DoH 的隐私边界 → `base/45`；IP 头部、CIDR 与路由 → `base/40`；UDP 与 QUIC 的传输语义 → `base/43`；手写 DNS/IP/TCP 解析器的完整工程 → `base/52`；域名注册、备案、CDN 厂商选型 → 不在本套。

---

## 44.1 引子：切了 IP，为什么 24 小时后还有人访问老机器

2026-07 中旬，某 SaaS 把入口从 `203.0.113.10` 换到 `198.51.100.20`。运维改完 A 记录，`dig` 立刻返回新 IP，监控上「新机器连接数」也确实涨了。但接下来 24 小时，老机器的连接数按一条接近直线的斜率下降，而不是瞬间归零：

| 变更后时间 | 仍打老 IP 的请求占比 | 现场动作 |
| --- | --- | --- |
| 0 min | 100% | 改 A 记录，`dig` 已显示新 IP |
| 10 min | 99.3% | 以为「再等等就好」 |
| 1 h | 95.8% | 开始怀疑健康检查配错 |
| 6 h | 75.0% | 把老机器扩容，准备「长期共存」 |
| 12 h | 50.0% | 回滚方案被否，因为新机器已承载一半 |
| 23 h | 4.2% | 老机器终于可以缩容 |
| 24 h | 0% | 事故复盘 |

这张表的斜率不是玄学。那条 A 记录的 TTL 是 **86400 秒（24 小时）**——模板里的默认值，没人动过。假设全球递归解析器的缓存剩余寿命在 `[0, 86400]` 上大致均匀分布，那么变更后 `t` 秒仍持有旧值的比例就是：

```
stale(t) = (86400 − t) / 86400
t = 600 s   → 85800 / 86400 = 99.31%
t = 3600 s  → 82800 / 86400 = 95.83%
t = 21600 s → 64800 / 86400 = 75.00%
t = 43200 s → 43200 / 86400 = 50.00%
t = 82800 s →  3600 / 86400 =  4.17%
```

这是**量级结论**：真实分布取决于每个解析器上次刷新的时刻、有没有被 anycast 到另一台实例、以及本地还有没有第二层缓存，但「24 小时才彻底干净」是 TTL 直接规定的上界。

更麻烦的是缓存不止一层。一次解析要穿过四级持有者：

```
浏览器 / JVM / Go 的 net.Resolver / 连接池   ← 应用层，规则各语言自己定
        │
systemd-resolved / nscd / stub resolver      ← 本机，可能自己也有 TTL
        │
递归解析器（1.1.1.1、8.8.8.8、10.0.0.53）    ← 真正的大缓存，按 (name,type,class) 存
        │
权威服务器（Cloudflare / Route 53 / BIND）    ← 只负责给原始 TTL
```

四层里任何一层没到期，客户端就会拿到旧 IP，而且**这是正确行为**。DNS 从来不是「读最新值」的数据库，它是「读一个带保质期的副本」的数据库。把这句话记住，本篇后面十三节都只是它的展开：12 字节头部定义副本长什么样（§44.2–44.4），递归与迭代定义副本怎么被生产出来（§44.5），TTL 定义副本能活多久（§44.6），UDP/EDNS 定义副本怎么塞进一个包（§44.7）。

---

## 44.2 报文结构：12 字节头部逐位

DNS 的固定头部只有 12 字节 = 96 位。所有「这个响应是不是我要的」「这是不是权威答案」「能不能信」的判断，都从这 96 位里读出来：

```
                                1  1  1  1  1  1
  0  1  2  3  4  5  6  7  8  9  0  1  2  3  4  5
+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
|                      ID                       |
+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
|QR|   Opcode  |AA|TC|RD|RA|   Z    |   RCODE   |
+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
|                    QDCOUNT                    |
+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
|                    ANCOUNT                    |
+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
|                    NSCOUNT                    |
+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
|                    ARCOUNT                    |
+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
```

逐字段的宽度与「写错会怎样」：

| 字段 | 位宽 | 本次实测响应（`81 80`） | 作用 | 写错的后果 |
| --- | --- | --- | --- | --- |
| `ID` | 16 | `0x1234` | 匹配请求与响应 | 不匹配就丢包；可预测则给伪造响应开门（`base/45`） |
| `QR` | 1 | 1（响应） | 区分查询/响应 | 解析器把响应当查询回，形成环路 |
| `OPCODE` | 4 | 0（QUERY） | 查询类型（QUERY/IQUERY/STATUS/NOTIFY/UPDATE） | 动态更新与区域传送走错分支 |
| `AA` | 1 | 0 | 该服务器是否对此名字有权威 | 缓存把非权威数据当权威数据存 |
| `TC` | 1 | 0 | 响应被截断 | 不重试 TCP 就拿到半个 RRset，校验和对但语义错 |
| `RD` | 1 | 1 | 客户端请求「替我递归」 | 关掉后递归解析器只回 referral，stub 不会自己迭代 |
| `RA` | 1 | 1 | 服务器声明「我支持递归」 | 客户端把纯权威服务器当解析器用，永远拿不到答案 |
| `Z` | 3 | 000 | 保留位；今天拆成 `AD`、`CD`、1 位保留 | `AD` 被误读成权威，DNSSEC 校验状态就错了 |
| `RCODE` | 4 | 0（NOERROR） | 结果码 | 把 NXDOMAIN 当 NODATA 处理，负缓存就写错 |
| `QDCOUNT` | 16 | 1 | 问题段条数 | 几乎所有实现只支持 1，多问会被拒 |
| `ANCOUNT` | 16 | 2 | 答案 RR 条数 | 少读一条就丢一半 A 记录 |
| `NSCOUNT` | 16 | 0 | 权威段 RR 条数 | 否定答案的 SOA 在这一段，漏读就没法负缓存 |
| `ARCOUNT` | 16 | 1 | 附加段 RR 条数 | 漏读 OPT 就不知道对端支持 EDNS |

`81 80` 展开成二进制，每一位都能对上：

```
0x81 = 1000 0001   QR=1  OPCODE=0000  AA=0  TC=0  RD=1
0x80 = 1000 0000   RA=1  Z=000  RCODE=0000
```

注意 `AA=0` 而 `RA=1`：这是一个**递归解析器给出的缓存答案**，不是权威答案。若解析器做了 DNSSEC 校验，`AD` 位会是 1（`0x0020`），字节变成 `81 a0`——真实 `dig` 输出里那一行 `flags: qr rd ra ad` 正是如此，没有 `aa`：数据再正确，它也不是权威的。反过来，直连权威时 `aa` 才会出现。

### 四个段：语法相同，语义不同

头部之后是四个段，顺序固定：

| 段 | 装什么 | 本例条数 | 语义 |
| --- | --- | --- | --- |
| Question | `QNAME + QTYPE + QCLASS`，**没有 TTL** | 1 | 你问什么 |
| Answer | 直接回答该问题的 RRset | 2 | 名字的最终数据 |
| Authority | 指向下一级的 NS，或否定答案的 SOA | 0 | 「去问它」或「这个名字没有」 |
| Additional | 粘合记录（glue）、OPT 伪记录 | 1（OPT） | 让下一跳能直接发 packet 的辅助数据 |

后三个段里每条记录的语法完全一样，都是 **RR**：

```
NAME(variable) | TYPE(2) | CLASS(2) | TTL(4) | RDLENGTH(2) | RDATA(RDLENGTH)
```

`TTL` 只存在于 RR 里，**Question 段没有 TTL**——问题没有「保质期」，答案才有。这也是为什么 `dig` 的 QUESTION SECTION 只显示名字、类型、类，而 ANSWER SECTION 每条都带一个秒数。

`RCODE` 的取值值得单独列一张表，因为排障时第一眼就看它：

| RCODE | 名字 | 含义 | 典型现场 |
| --- | --- | --- | --- |
| 0 | NOERROR | 查询本身成功 | 可能带答案，也可能是 NODATA |
| 1 | FORMERR | 报文格式错 | 压缩指针、长度字段写坏 |
| 2 | SERVFAIL | 解析器内部失败 | 上游超时、DNSSEC 校验失败 |
| 3 | NXDOMAIN | 这个名字不存在 | 拼错域名、记录被删 |
| 4 | NOTIMP | 不支持该操作 | `dig ANY` 被实现拒绝（本机实测 `status: NOTIMP`） |
| 5 | REFUSED | 策略拒绝 | ACL、未授权递归 |
| 9 | NOTAUTH | 不是该区的权威 | 问错服务器 |
| 16 | BADVERS | EDNS 版本不支持 | 老实现遇到 version > 0 |

`NOERROR` 不等于「有答案」。这是 DNS 排障里第一个反直觉点：**RCODE=0 且 ANCOUNT=0 是合法的 NODATA**，名字存在、只是没有这个类型。§44.5 会把 NXDOMAIN 与 NODATA 分开讲，因为它们的负缓存行为完全不同。

---

## 44.3 名字编码：长度前缀标签与 0xC0 压缩指针

DNS 不用 `www.example.com` 这种点分字符串，而是把每个标签变成「1 字节长度 + 内容」，以 0 结尾：

```
www.example.com
→ 03 77 77 77  07 65 78 61 6d 70 6c 65  03 63 6f 6d  00
   └len=3┘     └len=7   "example"──────┘  └len=3┘     └根┘
```

三条硬约束：单个标签 ≤ **63** 字节（长度字节高两位留给压缩指针）；整个名字 ≤ **255** 字节；比较时大小写不敏感（`ExAmPle.COM` 与 `example.com` 同一条记录）。根名字就是一个 `00`。

### 压缩指针：把重复的后缀变成 2 字节

一个响应里 `example.com` 会出现很多次：问题一次，每条 A 记录一次，RRSIG 的签名者名字再一次。于是 RFC 1035 允许把**出现过的后缀**替换成 2 字节指针：

```
+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
| 1  1|            14 位偏移量              |   ← 高两位固定 11
+--+--+--+--+--+--+--+--+--+--+--+--+--+--+
```

`0xC0 | (offset >> 8)` 后跟 `offset & 0xFF`，偏移从**报文第一个字节**算起，最大 16383。拿本机向 `1.1.1.1` 实发一次 `example.com A` 收到的 72 字节响应逐字节标注（发送端就是 §44.9⑤ 的脚本；TTL 254 是那一次命中的剩余值，与 §44.9② 的 39 不是同一次缓存）：

```
0000  12 34 81 80 00 01 00 02 00 00 00 01 07 65 78 61
0010  6d 70 6c 65 03 63 6f 6d 00 00 01 00 01 c0 0c 00
0020  01 00 01 00 00 00 fe 00 04 68 14 17 9a c0 0c 00
0030  01 00 01 00 00 00 fe 00 04 ac 42 93 f3 00 00 29
0040  04 d0 00 00 00 00 00 00
```

| 偏移 | 字节 | 字段 | 解读 |
| --- | --- | --- | --- |
| `0x00–0x01` | `12 34` | ID | `0x1234` |
| `0x02–0x03` | `81 80` | flags | QR=1、RD=1、RA=1、RCODE=0 |
| `0x04–0x05` | `00 01` | QDCOUNT | 1 |
| `0x06–0x07` | `00 02` | ANCOUNT | 2 |
| `0x08–0x09` | `00 00` | NSCOUNT | 0（肯定答案不带 SOA） |
| `0x0A–0x0B` | `00 01` | ARCOUNT | 1（OPT） |
| `0x0C–0x18` | `07 65 78 61 6d 70 6c 65 03 63 6f 6d 00` | QNAME | `example.com.`，13 字节 |
| `0x19–0x1A` | `00 01` | QTYPE | 1 = A |
| `0x1B–0x1C` | `00 01` | QCLASS | 1 = IN |
| `0x1D–0x1E` | `c0 0c` | 答案 1 的 NAME | 指针 → `0x0C`，即 `example.com.` |
| `0x1F–0x20` | `00 01` | TYPE | 1 = A |
| `0x21–0x22` | `00 01` | CLASS | 1 = IN |
| `0x23–0x26` | `00 00 00 fe` | TTL | 254 秒（缓存剩余寿命） |
| `0x27–0x28` | `00 04` | RDLENGTH | 4 |
| `0x29–0x2C` | `68 14 17 9a` | RDATA | `104.20.23.154` |
| `0x2D–0x2E` | `c0 0c` | 答案 2 的 NAME | 同一个指针 |
| `0x2F–0x3C` | `00 01 00 01 00 00 00 fe 00 04 ac 42 93 f3` | 答案 2 余下部分 | `172.66.147.243`，TTL 254 |
| `0x3D` | `00` | OPT 的 NAME | 根名字（OPT 必须用根） |
| `0x3E–0x3F` | `00 29` | TYPE | 41 = OPT |
| `0x40–0x41` | `04 d0` | CLASS | 1232 = 声明的 UDP 载荷 |
| `0x42–0x45` | `00 00 00 00` | TTL 字段 | 扩展 RCODE、版本、标志全 0 |
| `0x46–0x47` | `00 00` | RDLENGTH | 0（没有选项） |

两条 A 记录的名字都只花 2 字节。如果不压缩，每条要写 13 字节，整个响应从 72 字节涨到 **94 字节**——多出 **22 字节，30.6%**。DNSSEC 响应里同一个名字出现得更频繁（A、RRSIG、NSEC 各一次），压缩省的更多，这也是为什么 `+dnssec` 的响应仍然能塞进一个 UDP 包。

### 为什么压缩会制造解析器漏洞（只讲机制）

压缩指针是「报文内偏移」，它把**一个自包含的编码**变成了**带引用的图**。只要图里有环或共享，解析器就得回答三个问题，而每个问题都是一类漏洞的入口：

1. **指针能不能指向前方？** RFC 只保证「指向先前的出现」，但格式上没有任何位能阻止 `offset > 当前位置`。解析器若照做，`A → B → A` 就是一个环。实测一个自指指针（答案名字在偏移 30，内容是 `c0 1e`，指回 30）：没有防护的递归解析会直接栈溢出；加了跳数预算后立刻变成可控错误：

   ```
   answer 名字起始偏移 = 30 = 0x1e
   loop guard = compression loop at offset 30 (ptr=0x1e)
   ```

2. **一次解压能膨胀多少？** 名字上限是 255 字节，但一个 255 字节的名字可以由几十个指针拼出来；若每条 RR 都指向同一个「超长指针链」，解析器对每条记录都要重新走一遍，复杂度从 `O(报文长度)` 变成 `O(记录数 × 链长)`。512 字节的包可以要求几十万次解压步骤——这是**压缩炸弹**，不是理论问题。

3. **指针指向的字节一定是名字吗？** 指针只给偏移，不给「这里是什么」。如果偏移落进另一条记录的 RDATA（比如 A 记录的 4 字节地址），解析器会把 `68 14 17 9a` 当成标签长度与内容读，轻则解析失败，重则把后面的字节解释成完全不同的记录，导致**解析器与验证器看到两份不同的报文**（`base/45` 会讲这种「解析歧义」如何被用于投毒）。

工程上的防护只有四条，缺一不可：**只接受向后的指针**（`ptr < 当前偏移`）、**跳数上限**（典型 16–32）、**已访问偏移集合**（防环）、**输出名字长度上限 255**。下面这个 20 行的解析器把「跳数上限」写在最显眼的位置，另外三条的缺口在代码后面点名：

```python
def read_name(msg, off, budget=16):
    parts, jumps = [], 0
    while True:
        n = msg[off]
        if n == 0:
            return '.'.join(parts) + '.', off + 1
        if n & 0xC0 == 0xC0:                       # 压缩指针
            ptr = ((n & 0x3F) << 8) | msg[off + 1]
            jumps += 1
            if jumps > budget:                     # 跳数上限：防环
                raise ValueError(f'compression loop at offset {off} (ptr=0x{ptr:02x})')
            parts.append(read_name(msg, ptr, budget - 1)[0].rstrip('.'))
            return '.'.join(parts) + '.', off + 2
        parts.append(msg[off + 1:off + 1 + n].decode())
        off += 1 + n
```

注意它没有检查 `ptr < off`，也没有给输出长度封顶——这是刻意的：**跳数上限已经能防死循环，但要防「指针指进 RDATA」还得加边界检查，要防「超长名字」还得数 label 总长**。真实实现（BIND、Unbound、Go 的 `net/dns`）都有这两段，写解析器的人少写一行，攻击面就多一个。

---

## 44.4 记录类型：十几种 RR 撑起全部语义

DNS 的「数据库」只有一张表：`(NAME, TYPE, CLASS) → RDATA + TTL`。所谓「A 记录」「MX 记录」，只是 TYPE 取不同值、RDATA 按不同格式解释。下表全部来自本机实测（§44.9 有命令）：

| 类型（值） | 用途 | 真实例子（实测输出） |
| --- | --- | --- |
| A（1） | 名字 → IPv4 | `example.com. 39 IN A 104.20.23.154` |
| AAAA（28） | 名字 → IPv6 | `example.com. 10 IN AAAA 2606:4700:10::6814:179a` |
| CNAME（5） | 别名 → 规范名 | `www.microsoft.com. 3585 IN CNAME www.microsoft.com-c-3.edgekey.net.` |
| NS（2） | 区的权威服务器 | `example.com. 83260 IN NS hera.ns.cloudflare.com.` |
| SOA（6） | 区的元数据 + 负缓存参数 | `example.com. 376 IN SOA elliott.ns.cloudflare.com. dns.cloudflare.com. 2420077714 10000 2400 604800 1800` |
| MX（15） | 邮件路由，带优先级 | `gmail.com. 2045 IN MX 5 gmail-smtp-in.l.google.com.` |
| TXT（16） | 任意文本（SPF、验证、策略） | `example.com. 300 IN TXT "v=spf1 -all"` |
| SRV（33） | 服务的位置（优先级/权重/端口/目标） | `_imaps._tcp.fastmail.com. 3600 IN SRV 0 1 993 imap.fastmail.com.` |
| PTR（12） | 反向解析：IP → 名字 | `1.1.1.1.in-addr.arpa. 1054 IN PTR one.one.one.one.` |
| CAA（257） | 哪些 CA 可以给这个域签证书 | `google.com. 86400 IN CAA 0 issue "pki.goog"` |
| DS（43） | 父区对子区 DNSKEY 的摘要（信任链的铆点） | `com. 75069 IN DS 19718 13 2 8ACBB0CD…71D7805A` |
| DNSKEY（48） | 区的公钥 | `example.com. 2341 IN DNSKEY 256 3 13 MjyZielP0Gq…` |
| RRSIG（46） | 对某个 RRset 的签名 | `example.com. 66 IN RRSIG A 13 2 300 20261120141051 …` |

几条值得单独说的语义：

- **A/AAAA 是「多值」而不是「一条」**。本例 `example.com` 有 2 条 A、2 条 AAAA；客户端通常按顺序尝试，失败换下一个。RRset 内部的顺序**没有语义**，解析器可以轮转（实测两次查询的返回顺序就不同：`104.20` 在前，随后 `172.66` 在前）。
- **MX 的优先级数字越小越优先**。`example.com` 的实测值是 `0 .`——这是 RFC 7505 的 **null MX**：一个点表示「本域不接收邮件」，比「没有 MX 记录」更明确（没有 MX 时邮件会回退到 A 记录）。
- **TXT 不是「一条字符串」**。RDATA 是一串 `<长度, 文本>`，单段 ≤255 字节，多段按顺序拼接；SPF、DKIM、站点验证都塞在这里，也因此常被拿来做数据外带（`base/45` 讲检测）。
- **SRV 的 `_service._proto.name` 是固定命名**，`_imaps._tcp` 表示 IMAPS over TCP；优先级 + 权重决定选哪个目标，端口告诉客户端连哪里。
- **PTR 在另一棵树里**。`1.1.1.1` 的反向名是 `1.1.1.1.in-addr.arpa`，IPv6 则是 nibble 反转的 `ip6.arpa`。反向树是独立委派的，所以「正向能解析」不代表「反向能解析」，反之亦然。
- **CAA 是证书链的守门人**：`issue "pki.goog"` 表示只允许 Google Trust Services 给 `google.com` 签证书。CA 在签发前必须查它（RFC 8659），查不到就按「无限制」处理——这条记录配错，直接表现为「证书签不下来」。

### SOA：一个区的七个数，其中两个决定缓存

SOA 的 RDATA 固定七个字段。拿实测的 `example.com` 和根区对比：

| 字段 | 含义 | `example.com` 实测 | 根区实测 | 设错的后果 |
| --- | --- | --- | --- | --- |
| MNAME | 主服务器名 | `elliott.ns.cloudflare.com.` | `a.root-servers.net.` | 辅助服务器不知道去哪拉 zone |
| RNAME | 管理员邮箱（`@` 写成 `.`） | `dns.cloudflare.com.` | `nstld.verisign-grs.com.` | 人找不到联系人（不影响解析） |
| SERIAL | 区版本号（32 位） | `2420077714` | `2026111900` | 辅助服务器判断「要不要拉新版本」全靠它 |
| REFRESH | 辅助多久查一次主 | `10000`（2h47m） | `1800`（30m） | 太大 → 变更传播慢；太小 → 主服务器被轮询打爆 |
| RETRY | 主不可达时多久重试 | `2400`（40m） | `900`（15m） | 太小 → 故障期间疯狂重试 |
| EXPIRE | 多久拉不到就停止服务 | `604800`（7d） | `604800`（7d） | 太大 → 主挂了还在给过期数据 |
| MINIMUM | **负缓存 TTL 的上限** | `1800`（30m） | `86400`（1d） | 太大 → 删了记录半小时内还回 NXDOMAIN |

最后一行是全篇最容易被忽略的数字。RFC 2308 规定：否定答案（NXDOMAIN 或 NODATA）的缓存时间 = `min(SOA.MINIMUM, SOA 自身的 TTL)`。根区的 MINIMUM 是 86400，所以一个不存在的 `.invalid` 名字会被缓存**一整天**——实测的否定响应里，权威段正是：

```
. 86400 IN SOA a.root-servers.net. nstld.verisign-grs.com. 2026111900 1800 900 604800 86400
```

而 `example.com` 下不存在的名字，权威段是 `example.com. 1800 IN SOA …`，只缓存 30 分钟。**「我明明刚加了记录，为什么还是 NXDOMAIN」的答案，一半在这个 MINIMUM 里。**

---

## 44.5 解析流程：递归、迭代、根提示与 glue

DNS 只有两种查询模式，区别在「谁负责把问题追到底」：

```
stub（浏览器 / 应用 / libc）
  │ ① 递归查询：RD=1，「你帮我追到底」
  ▼
递归解析器（1.1.1.1 / 8.8.8.8 / 内网 10.0.0.53）
  │ ② 迭代查询：RD=0，「你知道多少给我多少」
  ├──► 根（13 组，anycast）      ── referral：com. NS + glue
  ├──► com TLD（a–m.gtld-servers.net）── referral：example.com NS + glue
  └──► 权威（elliott.ns.cloudflare.com）── answer（AA=1）
  │ ③ 把答案按 TTL 缓存，返回给 stub
  ▼
stub 拿到答案（TTL 已被递归器递减）
```

**递归**是「代理」：客户端只发一次，递归器负责所有后续查询，最后给最终答案或错误。**迭代**是「问路」：被问的服务器只回答「我不权威，但下一跳在 NS 段里」，绝不替你去问。根和 TLD 只做迭代，权威只回答自己区里的数据，只有递归器同时具备「缓存 + 追到底」两个能力。

### 根提示：13 组名字，26 个地址

递归器启动时不能去问 DNS「根在哪」（那是个循环），所以它自带一份 **root hints** 文件：13 个根服务器名字。实测 `dig . NS +short` 给出 `a` 到 `m` 共 13 个名字，对每个名字查 A 与 AAAA，各得 13 个唯一地址——`dig . NS +short | while read n; do dig +short "$n" A; done | sort -u | wc -l` 输出 13（AAAA 同理）。**13 不是「只有 13 台机器」**：每个根运营方在全球部署 anycast 实例，同一个 IP 在不同地区落到不同机房。本机 `dig +nsid` 直接问出了落点：

```
; NSID: 73 69 6e 31 39 ("sin19")
```

`sin19` 是新加坡的某个实例。这也是为什么「根服务器被打垮」在工程上极难成立：攻击一个 IP，实际打的是离攻击者最近的那几个实例，其它地区照常服务。代价是**同一个 IP 的行为可能不一致**——§44.6 会看到同一台 `1.1.1.1` 在几秒内返回不同 TTL，原因就是 anycast 落到了不同实例。

### glue：为什么必须有「地址的副本」

根返回的 referral 里，`com.` 的 NS 是 `a.gtld-servers.net.` 等 13 个名字。问题来了：这些名字在 `.net` 里，要解析 `a.gtld-servers.net` 就得先问 `.net` 的服务器；而 `.net` 的服务器名字又都在 `gtld-servers.net` 下——**循环依赖**。

解决办法是：根区在委派 `com.` 时，**顺便把 `a–m.gtld-servers.net` 的 A/AAAA 一起塞进 Additional 段**。这份「不属于本区、但为了让下一跳能发 packet 而附上的地址」就叫 **glue（粘合记录）**。实测直连根、关掉递归：

```
$ dig @a.root-servers.net www.example.com A +norecurse
;; flags: qr; QUERY: 1, ANSWER: 0, AUTHORITY: 13, ADDITIONAL: 27

;; AUTHORITY SECTION:
com. 172800 IN NS l.gtld-servers.net.
...（共 13 条）

;; ADDITIONAL SECTION:
l.gtld-servers.net. 172800 IN A    192.41.162.30
l.gtld-servers.net. 172800 IN AAAA 2001:500:d937::30
...（共 26 条地址 + 1 条 OPT）
```

`AUTHORITY: 13`、`ADDITIONAL: 27` = 13 个 A + 13 个 AAAA + 1 个 OPT，账对得上。**glue 是层级解析的引导扇区**：没有它，第一跳就死循环，整个 DNS 起不来。

glue 也带来一个结构性风险：它是**父区对子区地址的断言**，父区可以撒谎或过期。所以现代解析器只接受 **in-bailiwick** 的 glue（名字必须在被委派的子区之内），否则就丢弃——这条规则是缓存投毒防御的第一道门（`base/45` 展开）。

### CNAME 链与循环检测

CNAME 是「这个名字是另一个名字的别名」，解析器必须继续追。实测一条两跳的链：

```
www.microsoft.com.            3585 IN CNAME www.microsoft.com-c-3.edgekey.net.
www.microsoft.com-c-3.edgekey.net. 885 IN CNAME e13678.dscb.akamaiedge.net.
e13678.dscb.akamaiedge.net.      5 IN A     104.68.52.121
```

三条记录在**同一个响应**里返回，TTL 各不相同（3585 / 885 / 5）——因为三段缓存的年龄不同。链的规则：

- CNAME 的 owner 不能再有其它类型的数据（RFC 2181）。`example.com CNAME x` 与 `example.com MX` 同时存在是**非法**的，实现行为未定义；邮件会因此随机失败（§44.11 有现场）。
- 链长必须有上限（常见 8–16 跳）。`a CNAME b`、`b CNAME a` 就是环，没有上限的解析器会转到超时。
- 递归器通常替客户端追完整条链并一起返回；如果链跨了区，递归器会发起新的迭代查询，RTT 随之增加。

### NXDOMAIN 与 NODATA：两个都「没答案」，缓存行为不同

| 情形 | RCODE | ANSWER | AUTHORITY | 含义 | 负缓存 |
| --- | --- | --- | --- | --- | --- |
| 名字不存在 | 3（NXDOMAIN） | 0 | 最近 encloser 的 SOA | 整棵树都没有这个名字 | 按 SOA 缓存 |
| 名字存在、类型没有 | 0（NOERROR） | 0 | 该名字所在区的 SOA | 名字有，只是没有 AAAA/CAA… | 按 SOA 缓存 |
| 委派 | 0 | 0 | 子区 NS（无 SOA） | 「去问它」 | 不缓存为答案 |
| 正常答案 | 0 | ≥1 | 空或 NS | 有数据 | 按 RR 的 TTL |

实测两种「没答案」（节选，只保留 status 与权威段）：

```
$ dig no-such-xyz-98765.invalid A
;; ->>HEADER<<- opcode: QUERY, status: NXDOMAIN, id: 38978
;; AUTHORITY SECTION:
. 86400 IN SOA a.root-servers.net. nstld.verisign-grs.com. 2026111900 1800 900 604800 86400

$ dig example.com CAA
;; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 39156
;; AUTHORITY SECTION:
example.com. 1800 IN SOA elliott.ns.cloudflare.com. dns.cloudflare.com. 2420077714 10000 2400 604800 1800
```

两者都 `ANSWER: 0`，但一个是 `NXDOMAIN`、一个是 `NOERROR`。**把 NXDOMAIN 当 NODATA 缓存，或者反过来，是自建 DNS 最常见的 bug**：前者会让「后来加的记录」在缓存里继续「不存在」，后者会让不存在的子域反复打穿到权威。

---

## 44.6 缓存与 TTL：为什么「改了不生效」是设计

### 正缓存：TTL 是倒计时，不是建议

每条 RR 的 TTL 是 32 位无符号秒数（RFC 2181 规定最高位为 0，所以最大约 2³¹ 秒）。递归器存的是 `(name, type, class) → rdata, expire_at`，返回给下游时**只给剩余值**，不给原始值。实测同一时刻问权威与问递归：

```
$ dig @hera.ns.cloudflare.com example.com A +noall +answer
example.com. 300 IN A 172.66.147.243
example.com. 300 IN A 104.20.23.154

$ dig @1.1.1.1 example.com A +noall +answer
example.com. 208 IN A 104.20.23.154
example.com. 208 IN A 172.66.147.243
```

权威给 300，递归给 208——中间那 92 秒就是缓存年龄。**下游拿到的 TTL 永远 ≤ 上游**，所以 TTL 只会越走越小，不会因为经过更多层而延长。

再看连续四次问同一个 `1.1.1.1`：

```
example.com. 212 IN A 104.20.23.154
example.com. 256 IN A 172.66.147.243
example.com. 185 IN A 172.66.147.243
example.com. 210 IN A 172.66.147.243
```

TTL 不单调，而且跳来跳去。原因不是「DNS 坏了」，是 **1.1.1.1 是一个 anycast 地址**：四次查询很可能落到了不同实例（`dig +nsid` 在本机命中 `sin19`），每个实例的缓存条目年龄不同。这条实测同时证明了两件事：**TTL 是 per-cache-entry 的，不是全局的**；以及**「同一个 IP 的解析器」在物理上不是一台机器**。

### 负缓存：SOA MINIMUM 决定的「五分钟阴影」

否定答案也要缓存，否则不存在的名字会把根和 TLD 打穿。规则（RFC 2308）：

```
负缓存 TTL = min(SOA.MINIMUM, SOA 这条记录自己的 TTL)
```

于是「删了记录 → 立刻重建」这件事在 DNS 里不成立：删除会产生一个 NXDOMAIN，它按 MINIMUM 被缓存；重建记录后，缓存里的 NXDOMAIN 仍然有效，直到倒计时结束。托管商常把 MINIMUM 设成 300（5 分钟），这就是「负缓存 5 分钟」这个口口相传的数字的来源；根区设成 86400，所以 `.invalid` 这类不存在的 TLD 会被缓存一天。

### 「改了不生效」的三种原因

排障时不要猜，按层往下问。三种原因对应三个不同的观察点：

| 层 | 现象 | 定位 | 结论 |
| --- | --- | --- | --- |
| 本地缓存 | 本机 `getent` 拿到旧值，`dig @1.1.1.1` 是新值 | `getent hosts name`、`resolvectl query name`、`cat /etc/hosts`、`cat /etc/nsswitch.conf` | 问题在 stub/OS/应用缓存，不在 DNS |
| 递归缓存 | `dig @1.1.1.1` 旧、`dig @权威` 新，且 TTL 还有剩余 | `dig @1.1.1.1 +noall +answer`、`dig +trace` | 等 TTL 到期，或换解析器验证 |
| 权威未更新 | 直连权威也是旧值，或 SOA SERIAL 没变 | `dig @ns1 +noall +answer`、`dig SOA`、查 zone 文件/控制台 | 改根本没落到权威，或 secondary 没拉到新版本 |

再加两种「看起来像 DNS」的：**客户端持有旧连接**（TCP/HTTP keep-alive 到旧 IP，DNS 早已更新）、**split-horizon / CDN 按 geo 返回不同地址**（不同地区拿到不同答案，不是缓存）。排障阶梯可以固化成三行：

```bash
dig +noall +answer name A                     # 本机解析器看到什么
dig @1.1.1.1 +noall +answer name A            # 公共递归看到什么
dig @$(dig +short NS zone | head -1) name A   # 权威看到什么
```

三行结果摆在一起，问题必然落在其中一层。**如果三行都是新值而用户仍是旧的，那就不是 DNS。**

### TTL 设错的两种方向

TTL 太长，变更传播慢（§44.1 的 24 小时）；TTL 太短，权威和递归的 QPS 成比例上升。给一个可推导的账：1 亿次解析/天 ≈ 1157 QPS；若 95% 命中缓存，权威只看到约 `1157 × 5% ≈ 58 QPS`；TTL 缩短让命中率掉到 50%，权威立刻变成 `1157 × 50% ≈ 578 QPS`——**10 倍**。**TTL 是「一致性」与「查询量」之间的汇率**，没有免费的方向。

---

## 44.7 传输：UDP 53、512 字节、TC 位与 EDNS0

DNS 默认走 **UDP 53**：一问一答、无握手、无重传，一个 RTT 就能拿结果。代价是 UDP 没有分片重组之外的可靠性，所以协议自己规定了「响应不能太大」——RFC 1035 的原始上限是 **512 字节**（不含 IP/UDP 头）。

### 超过 512 字节：TC 位与 TCP 回退

响应超过客户端能接收的尺寸时，服务器只回一个**截断的响应**，并把 `TC` 置 1。实测用 512 字节的缓冲问根 DNSKEY：

```
$ dig +bufsize=512 +ignore . DNSKEY
;; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 60154
;; flags: qr tc rd ra; QUERY: 1, ANSWER: 0, AUTHORITY: 0, ADDITIONAL: 1
; EDNS: version: 0, flags:; udp: 1232
;; MSG SIZE  rcvd: 28
```

`flags: qr tc rd ra` 里那个 `tc` 就是截断位。28 字节也能算出来：12 字节头部 + 1 字节根名字 + 4 字节 QTYPE/QCLASS + 11 字节 OPT（NAME 1 + TYPE 2 + CLASS 2 + TTL 4 + RDLENGTH 2）= 28。**服务器连答案都没给，只告诉你「换 TCP」。**

客户端看到 `TC=1` 必须改用 TCP 重问；TCP 的 DNS 报文前面多一个 **2 字节长度前缀**（因为 TCP 是字节流，需要分帧），之后才是完整的 DNS 报文。实测 `dig +tcp` 正常返回两条 A 记录。现代实现还会复用 TCP 连接（RFC 7766），避免每次截断都重新握手。

### EDNS0：把 512 变成 1232，顺便塞进扩展字段

EDNS0（RFC 6891）的办法很巧：**不动头部，加一条伪记录**。它在 Additional 段放一个 `OPT`，TYPE=41，NAME 必须是根（`00`），而原本的 CLASS 和 TTL 字段被重新解释：

```
NAME = 00            （必须是根）
TYPE = 0029          （41 = OPT）
CLASS = 04 d0        （UDP 载荷大小，实测 1232）
TTL  = 00 00 00 00   （扩展 RCODE 8 位 | 版本 8 位 | 标志 16 位）
RDLENGTH = 0000      （没有选项时）
```

TTL 那 4 个字节的拆法：

```
+--------+--------+--------+--------+
| ext-rcode (8)   | version (8)     |
+--------+--------+--------+--------+
| DO(1) | 保留(15)                  |
+--------+--------+--------+--------+
```

`DO`（DNSSEC OK）是最高位 `0x8000`：置 1 表示「请把 RRSIG 一起给我」。实测 `dig +dnssec` 的响应里，`; EDNS: version: 0, flags: do; udp: 1232` 就是这一位。**没有 EDNS，DNSSEC 根本没法工作**——一个 RRSIG 就 100 多字节，512 字节装不下几条。

**1232 这个数字是算出来的，不是拍脑袋**：IPv6 的最小 MTU 是 1280，减去 40 字节 IPv6 头、8 字节 UDP 头，剩 `1280 − 48 = 1232`。这样响应既不会触发 IP 分片，又能比 512 多装一倍多的数据。4096 是很多服务器愿意声明的上限，但它超过常见 MTU，一旦中间设备丢分片就会「大包不通、小包正常」——所以 1232 是目前的工程共识。

EDNS 还能带选项（OPT 的 RDATA 是 `option-code + option-length + data` 的序列）：

| 选项 | code | 作用 | 代价 |
| --- | --- | --- | --- |
| NSID | 3 | 让服务器报出实例标识 | 暴露 anycast 落点（实测 `sin19`） |
| ECS | 8 | 把客户端子网传给权威，做就近调度 | 泄露用户网段；CDN 依赖它，隐私倡导反对它 |
| Cookie | 10 | 轻量防伪造（客户端/服务端 cookie） | 不是认证，只是提高伪造门槛 |
| Padding | 12 | 把响应补齐，抗长度分析 | 浪费带宽 |

实测 `+noedns` 与默认的差别：同一问题，`+noedns` 的 `MSG SIZE` 是 **61**，默认是 **72**，差 11 字节——正好是那条 OPT。**EDNS 不是「可选的优化」，它已经是默认行为**；关掉它，DNSSEC、大 RRset、ECS 全部失效。

### DoT / DoH / DoQ 的位置

这三种只是把 DNS 报文换了个传输壳，**12 字节头部和所有记录语义一个字都没变**：

| 方式 | 端口 | 外层 | 补上了什么 | 没补上什么 |
| --- | --- | --- | --- | --- |
| UDP/TCP 53 | 53 | 无 | 无 | 认证、完整性、机密性全缺 |
| DoT | 853 | TLS over TCP | 机密性 + 完整性 + 服务器认证 | 解析器仍看到明文问题；端点被识别 |
| DoH | 443 | HTTPS | 同上，且混在网页流量里 | 解析器（DoH 服务商）成为新的信任集中点 |
| DoQ | 853 | QUIC | 同上 + 0-RTT、流复用 | 实现少，UDP 被限速时退化 |

关键点：**这里加密的是「客户端到所选解析器」这一段**。它不规定解析器到上游的每一跳采用哪个传输，也不替代 DNSSEC 数据来源验证。所选解析器仍可看到查询；不同观察位置的边界见 [45](45-dns-security.md)。

### HTTPS / SVCB 是 DNS 记录，DoH 是 DNS 的传输方式

两者名字里都有 HTTPS，但做的是不同的事。DNS 的 HTTPS 记录（[RFC 9460](https://www.rfc-editor.org/rfc/rfc9460)）可以向客户端提供服务绑定及 `alpn` 等参数；支持 ECH 的端点还可以通过 `ech` 参数提供配置（[RFC 9848](https://www.rfc-editor.org/rfc/rfc9848)）。这些记录可以通过普通 DNS 或加密解析取得，**记录本身不会自动给这次解析加密**。`alpn` 宣告可用协议也不证明某条 UDP 路径可达。`Alt-Svc` 则是 HTTP 层提供的替代服务信息，不是 DNS 答复，也不是握手实际协商的结果。排查时分开记：解析返回了什么、客户端尝试了什么、端点最终协商了什么。

---

## 44.8 根 / TLD / 权威的实际数字与 RTT 账

把本篇用到的数字集中在一张表里，每个都能用 §44.9 的命令复现：

| 对象 | 实测/口径 | 怎么得到 |
| --- | --- | --- |
| 根服务器组 | **13** | `dig . NS +short` |
| 根的 IP 地址 | **26**（13 A + 13 AAAA） | 对每个 NS 名查 A/AAAA |
| 根 anycast 实例 | 上千（量级） | 各运营方部署口径；本机 NSID 命中 `sin19` |
| TLD 数量 | **1436** | `curl data.iana.org/TLD/tlds-alpha-by-domain.txt`，`grep -vc '^#'` |
| `com.` 的 NS | **13** | 根 referral 的 AUTHORITY 段 |
| 一次 referral 的附加记录 | **27**（13 A + 13 AAAA + 1 OPT） | `dig @a.root-servers.net … +norecurse` |
| `example.com` 的 A 记录 | **2** | `dig example.com A` |
| 无签名 A 响应 | **72 B** | `MSG SIZE rcvd` |
| 带 RRSIG 的权威响应 | **179 B** | `dig +trace` 末段 `Received 179 bytes` |

TLD 的 1436 是 2026-11-19 当天的 IANA 根区列表（文件头写着 `Version 2026111900`），包含 `AAA` 到 `ZUERICH` 以及所有国家代码。**「约 1500 个 TLD」这个常用说法，精确值就在这个文件里。**

### 一次冷查询要几次 RTT

把 §44.5 的图换成账：

| 跳 | 谁 → 谁 | 是否可缓存 | 冷查询 RTT |
| --- | --- | --- | --- |
| ① | stub → 递归器 | 否（每次都要问） | 1 |
| ② | 递归器 → 根 | 是（NS + glue） | 1 |
| ③ | 递归器 → TLD | 是 | 1 |
| ④ | 递归器 → 权威 | 是（答案本身） | 1 |
| 合计 | | | **4 RTT** |

命中缓存时只剩 ①：**1 RTT**；stub 自己也缓存时是 **0 RTT**（不发包）。本机 `dig +trace` 实测四跳为 0 + 0 + 64 + 8 = **72 ms**（完整输出见 §44.9③）：根与根 referral 就近命中 anycast，跨洲的 TLD 一跳 64 ms，权威 8 ms。**冷查询的延迟不是一个 RTT，是四个 RTT 的和**；这也是为什么浏览器要预解析、操作系统要缓存、CDN 要把权威部署到 anycast 上。

### 13 组根为什么够用

一个可推导的容量账（量级结论）：假设全球递归器合计 10 万 QPS 的「未命中根」查询（现实中远低于此，因为根 NS 的 TTL 是 172800 秒），平均分到 13 组、每组约 100 个 anycast 实例，则每个实例约 `100000 / 1300 ≈ 77 QPS`。一台普通服务器处理几万 QPS 的简单查询没有压力，所以瓶颈从来不是「根的算力」，而是**任何一次根区变更都要让全球缓存重新收敛**——这才是 13 组 + 两天 TTL 的真正含义。

---

## 44.9 实测：从 `resolv.conf` 到一个自己构造的报文

本机有外网与 `dig`（BIND 9.18.49），所以前四组是真跑；第五组不依赖网络，用 Python 手工构造查询与响应，把 §44.3 的压缩指针亲手解一遍。

### ① 本机解析器配置

```
$ cat /etc/resolv.conf
# Dynamic resolv.conf(5) file for glibc resolver(3) generated by resolvconf(8)
#     DO NOT EDIT THIS FILE BY HAND -- YOUR CHANGES WILL BE OVERWRITTEN
# 127.0.0.53 is the systemd-resolved stub resolver.
nameserver 1.1.1.1
```

只有一行有效配置：`nameserver 1.1.1.1`。没有 `search`、没有 `options ndots`，所以本机所有名字都按「绝对名」解析——这也是 §44.11 里 `search` 坑的反面样本。

### ② 一次完整查询与头部

```
$ dig example.com
; <<>> DiG 9.18.49-1~deb12u3-Debian <<>> example.com
;; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 59025
;; flags: qr rd ra ad; QUERY: 1, ANSWER: 2, AUTHORITY: 0, ADDITIONAL: 1

;; OPT PSEUDOSECTION:
; EDNS: version: 0, flags:; udp: 1232
;; QUESTION SECTION:
;example.com.			IN	A

;; ANSWER SECTION:
example.com.		39	IN	A	172.66.147.243
example.com.		39	IN	A	104.20.23.154

;; Query time: 4 msec
;; SERVER: 1.1.1.1#53(1.1.1.1) (UDP)
;; MSG SIZE  rcvd: 72
```

`status: NOERROR`、`ANSWER: 2`、`ADDITIONAL: 1`（OPT）、`MSG SIZE 72`，字段布局和 §44.3 的 dump 完全对上（TTL 与 AD 位是不同次查询的差异）。`ad` 表示解析器已经做过 DNSSEC 校验（`1.1.1.1` 默认开启验证），`aa` 不出现则说明这是缓存答案。

### ③ 逐级迭代：`+trace`

`dig +trace` 关掉递归，自己从根开始迭代，于是能把「13 组根、13 个 TLD NS、glue」全打出来。关键行（真实输出，节选）：

```
com.			172800	IN	NS	a.gtld-servers.net.
com.			172800	IN	DS	19718 13 2 8ACBB0CD28F41250A80A491389424D341522D946B0DA0C0291F2D3D7 71D7805A
;; Received 1171 bytes from 192.203.230.10#53(e.root-servers.net) in 0 ms

example.com.		172800	IN	NS	hera.ns.cloudflare.com.
example.com.		172800	IN	NS	elliott.ns.cloudflare.com.
;; Received 506 bytes from 192.52.178.30#53(k.gtld-servers.net) in 64 ms

example.com.		300	IN	A	172.66.147.243
example.com.		300	IN	RRSIG	A 13 2 300 20261120141437 20261118121437 34505 example.com. YiKPY4kTOPAjTU95ZshUy9rEsV7MJOYWKvECMWMyymO/zJOjlH0X1JPj MdVke4/GLvAPzY1DU7NVoElEwb3BIg==
;; Received 179 bytes from 162.159.44.228#53(elliott.ns.cloudflare.com) in 8 ms
```

三个细节值得停一下：`com.` 的 `DS` 出现在根对 `com.` 的委派里（DNSSEC 信任链的下一环）；`RRSIG` 让响应从 72 字节涨到 179 字节；权威的 TTL 是 300，而缓存返回的是 39/208/256 之类的剩余值。

### ④ 否定答案、关闭递归、截断与 TCP

```
$ dig no-such-xyz-98765.invalid A
;; ->>HEADER<<- opcode: QUERY, status: NXDOMAIN, id: 38978
;; flags: qr rd ra ad; QUERY: 1, ANSWER: 0, AUTHORITY: 1, ADDITIONAL: 1

$ dig +norecurse @1.1.1.1 example.com A
;; flags: qr ra ad; QUERY: 1, ANSWER: 2, AUTHORITY: 0, ADDITIONAL: 1

$ dig +bufsize=512 +ignore . DNSKEY
;; flags: qr tc rd ra; QUERY: 1, ANSWER: 0, AUTHORITY: 0, ADDITIONAL: 1
;; MSG SIZE  rcvd: 28

$ dig +tcp example.com A +noall +answer
example.com.		160	IN	A	104.20.23.154
```

第一行是 NXDOMAIN（RCODE=3）；第二行关掉 RD 仍然拿到答案，因为缓存里有——**RD=0 只禁止「替你去追」，不禁止「把已有的给你」**；第三行是 `TC=1` 的截断响应，28 字节；第四行是 TCP 回退成功。再加上 `+dnssec` 的 `flags: do` 与 `NSID ("sin19")`，§44.7 的每个字段都有了现场。

### ⑤ 离线：自己构造查询，再手写解析器解响应

不依赖任何服务器，按 RFC 1035 拼字节：头部 12 字节 + QNAME + QTYPE/QCLASS，再拼一个应答，里面放 CNAME 与一条用压缩指针指向 CNAME RDATA 的 A 记录。下面是**构造部分**，解析函数就是 §44.3 那 20 行：

```python
import struct

def labels(name):
    return b''.join(bytes([len(l)]) + l.encode() for l in name.rstrip('.').split('.')) + b'\x00'

q = struct.pack('!HHHHHH', 0xBEEF, 0x0100, 1, 0, 0, 0)
q += labels('www.example.com') + struct.pack('!HH', 1, 1)

rd1 = 45                                     # CNAME 的 rdata 偏移
r = bytearray(struct.pack('!HHHHHH', 0xBEEF, 0x8180, 1, 2, 0, 0))
r += labels('www.example.com') + struct.pack('!HH', 1, 1)
r += b'\xc0\x0c' + struct.pack('!HHIH', 5, 1, 60, 13) + labels('example.com')
r += struct.pack('!H', 0xC000 | rd1) + struct.pack('!HHIH', 1, 1, 60, 4) + bytes([93, 184, 216, 34])
```

真实运行输出：

```
query  = 33 bytes   response = 74 bytes
answer1 的 rdata 偏移 = 45 = 0x2d
offset 12 名字 = www.example.com.
offset 33 名字 = www.example.com. (用了 1 次指针)
offset 58 名字 = example.com. (指针 -> 0x2d)
A 记录           = 93.184.216.34
loop guard = compression loop at offset 30 (ptr=0x1e)
```

`offset 58` 那条记录的名字只有 2 字节 `c0 2d`，指向 45——也就是 CNAME 的 RDATA。**同一个名字在后半段被复用，这就是压缩的全部机制。** 最后一行来自另一个自指报文（答案名字在偏移 30，内容是 `c0 1e`），验证 §44.3 的跳数预算确实能拦住环。

再补一个真实网络抓下来的 40 字节查询（`example.com A` + OPT）的字节布局：

```
0000  12 34 01 00 00 01 00 00 00 00 00 01 07 65 78 61
0010  6d 70 6c 65 03 63 6f 6d 00 00 01 00 01 00 00 29
0020  10 00 00 00 00 00 00 00
```

`12 34` 是 ID，`01 00` 是「RD=1」，`00 01 00 00 00 00 00 01` 是四段计数（问题 1、答案 0、权威 0、附加 1），`07 example 03 com 00` 是 QNAME，`00 01 00 01` 是 A/IN，末尾 `00 00 29 10 00 …` 就是 OPT（CLASS=`0x1000`=4096）。**把这 40 字节和 §44.3 的 72 字节并排看，头部、名字编码、RR 三段格式全部覆盖。**

---

## 44.10 三列账：DNS 承诺了什么、收了多少、从哪开始崩

| 维度 | 承诺了什么 | 为此付了什么 | 从哪里开始失效 |
| --- | --- | --- | --- |
| 全球命名 | 任何名字在任何地点可解析，无需中央在线 | 分层委派 + 13 组根 + 上千 anycast 实例的运维成本 | 根/TLD 被攻击或配置错误，整片名字同时不可达 |
| 可缓存 | 一次解析可服务千万客户端，延迟降到 1 RTT | **TTL 传播延迟**：改记录最坏等一个 TTL | TTL 设成 86400，变更要等一天（§44.1） |
| 分层自治 | 每个区自己管自己的数据，父区只做委派 | glue 是父区对子区地址的「副本」，可能过期或被伪造 | 过期 glue / 越界 glue → 解析失败或缓存投毒 |
| 小报文 | 12 字节头部 + UDP，一个 RTT 完成 | 512 字节上限、截断、EDNS 协商、分片风险 | 大 RRset / DNSSEC 触发 TC，客户端不回退 TCP 就失败 |
| 多记录类型 | A/AAAA/CNAME/MX/TXT/SRV… 一套报文全装下 | 类型语义靠实现约定（CNAME 与其它类型互斥等） | CNAME 与 MX 并存、通配符边界、负缓存类型混淆 |
| 无状态查询 | 服务器不需要记住客户端，随便 anycast | 没有会话，也就没有认证 | 响应可被伪造、缓存可被污染（`base/45`） |

最后一行就是主线④在本篇的落点。DNS 天生缺三样：

| 缺什么 | 直接后果 | 补法（详见 `base/45`） |
| --- | --- | --- |
| **认证**（响应是不是真的来自权威） | 任意人能伪造响应，只要猜中 ID/端口/五元组 | 源端口 + ID 随机化、0x20 编码、DNS Cookie；根本上靠 DNSSEC |
| **完整性**（数据有没有被改） | 中间人可以改 A 记录、改 TTL、删 RRSIG | DNSSEC（RRSIG/DS/DNSKEY 链） |
| **机密性**（谁在查什么） | 本地网络、ISP、解析器都能看到明文问题 | DoT / DoH / DoQ，但只加密到解析器 |

**DNS 是全书唯一「三样全缺还被全链路依赖」的协议**：缺认证 → 投毒与劫持；缺完整性 → 中间人改答案；缺机密性 → 查询内容裸奔。三种补法各自留下新坑（DNSSEC 的复杂度、DoH 的信任集中），`base/48` 的总表会把它们和 ARP、TCP、HTTP 放在同一张「缺失的认证」表里。

---

## 44.11 踩坑清单：现象 → 根因 → 定位 → 修 → 防

| 现象 | 根因（指回本篇） | 定位 | 修 | 防复发 |
| --- | --- | --- | --- | --- |
| 改了 A 记录，部分用户仍打老 IP | TTL 未到期 + 多层缓存（§44.1、§44.6） | 三行阶梯：`dig` / `dig @1.1.1.1` / `dig @权威` | 等 TTL；紧急时改记录并接受长尾，或让老机器继续服务 | 变更前把 TTL 降到 60–300，等一个旧 TTL，再改值，再升回 |
| 加了 MX，邮件却投递失败 | 同名的 CNAME 与 MX 并存（§44.5） | `dig MX` 与 `dig CNAME` 同时查；看是否同一 owner | 去掉 CNAME，改用 A/AAAA；MX 目标也不要指向 CNAME | 区文件 lint（`named-checkzone`）；禁止 apex CNAME |
| 通配符「没生效」或「误伤」 | `*.example.com` 只匹配**一级**，且不覆盖已存在的名字（§44.4） | `dig a.b.example.com`、`dig exist.example.com` | 需要多级就写多条，或改用明确记录 | 用查询矩阵测试：存在/不存在/多级/其它类型 |
| 删了记录，5 分钟内还返回 NXDOMAIN | 负缓存按 `min(SOA.MINIMUM, SOA TTL)`（§44.6） | `dig` 看权威段 SOA 的最后一个数 | 等负缓存过期；重建记录后无法强推 | 变更窗口避开负缓存期；把 MINIMUM 调到可接受的短值 |
| `ssh db` 解析到了错误的主机 | `search` 域 + `ndots` 把短名补成了 FQDN（§44.9） | `cat /etc/resolv.conf` 看 `search` 与 `options ndots` | 用绝对名 `db.`（带尾点）或写全 `db.corp.example.com` | 应用配置一律写 FQDN 带尾点；`ndots` 明确设置 |
| 大响应超时，小响应正常 | EDNS 声明的载荷大于路径 MTU，分片被丢（§44.7） | `dig +bufsize=1232`、`dig +tcp` 对比 | 把广告值降到 1232；确认防火墙放行 TCP 53 | 监控 `TC=1` 比例与 TCP 回退成功率 |
| DNSSEC 域全部 SERVFAIL | 签名过期或链断（DS/DNSKEY 不匹配） | `dig +dnssec`、`dig DS`、`dig DNSKEY`；看 `ad` 是否消失 | 修签名/补链；临时只能关验证（不推荐） | 签名到期告警；`delv` 定期验证 |

两条最值得展开：

**CNAME 与 MX 的冲突不是「配置难看」，是协议禁止。** RFC 2181 规定 CNAME 的 owner 不能有任何其它类型的数据，因为「别名」意味着「这个名字的所有查询都该去别处」，而 MX 查询如果拿到 CNAME，邮件服务器必须重新解析目标——很多老实现不追，直接退信。现场表现是「A 记录能解析、邮件时好时坏」，因为 A 查询追了 CNAME，MX 查询没追。修法只有一条：apex 用 A/AAAA，CNAME 只给子域用。

**`search` 域的坑在「点」上。** `resolv.conf` 里写了 `search corp.example.com`，你 `ssh db`，glibc 会先试 `db.corp.example.com`；但如果名字里已经有一个点（`api.staging`），`ndots` 默认 1 会让它**先试绝对名**，失败再拼 search。于是同一个短名在不同机器上解析到不同主机，而 `dig`（不做 search）永远是对的——**用 `dig` 复现不了的问题，先看 `resolv.conf` 的 search/ndots**。

---

## 44.12 一页速记

```
【一句话】DNS 是带 TTL 的全球最终一致数据库：读的是副本，不是最新值。

【12 字节头部】
  ID(16) | QR OPCODE(4) AA TC RD RA Z(3) RCODE(4) | QD AN NS AR (各 16)
  RD=客户端请求递归；RA=服务器支持递归；AA=权威；TC=截断；AD=已校验

【名字】
  长度前缀标签 + 00 结尾；单标签 ≤63，全名 ≤255
  0xC0|offset(14) = 压缩指针，指向报文内先前出现的后缀（本例省 11 字节/次）

【四段】
  Question 无 TTL；Answer/Authority/Additional 都是 RR
  RR = NAME TYPE CLASS TTL RDLENGTH RDATA；否定答案的 SOA 在 Authority

【递归 vs 迭代】
  stub →(RD=1)→ 递归器 →(RD=0)→ 根 → TLD → 权威
  13 组根 / 26 个地址 / 1436 个 TLD；glue 是父区给的地址副本，没它第一跳死循环

【TTL】
  正缓存：TTL 递减，下游永远 ≤ 上游
  负缓存：min(SOA.MINIMUM, SOA TTL)；根区 86400，托管商常见 300
  「改了不生效」三层：本地缓存 / 递归缓存 / 权威没更新

【传输】
  UDP 53 优先；>512 或 EDNS 广告值 → TC=1 → TCP 重问（2 字节长度前缀）
  EDNS0：OPT(TYPE=41)，CLASS=UDP 载荷（1232 = 1280−40−8），DO 位要 RRSIG
  DoT 853 / DoH 443 / DoQ 853：只加密到解析器，权威侧仍明文

【三缺】认证 ✗ 完整性 ✗ 机密性 ✗ → base/45、base/48
```

---

## 44.13 自检 3 题

**Q1.** 一个 72 字节的 DNS 响应里，两条 A 记录的名字都写成 `c0 0c`。这个指针指向哪里、为什么合法、如果改成 `c0 0d` 会怎样？请用偏移量说明。

<details><summary>答案</summary>

`c0 0c` 的高两位 `11` 表示压缩指针，剩余 14 位是偏移 `0x00C` = 12。报文第 12 字节正是 Question 段 QNAME 的第一个字节 `07`（`example` 的长度），所以指针指向 `example.com.` 这个完整名字。它合法，因为 RFC 1035 允许指针指向**报文中先前出现过的名字**，而 Question 的名字总是最先出现，所以答案几乎总是指向 `0x0C`。

改成 `c0 0d` 就指向 13，即 `example` 的 `e`（`0x65`）。解析器会把这个字节当长度：`0x65 = 101`，但标签上限是 63，而且高两位 `01` 不是指针前缀——严格的解析器直接报 FORMERR；宽松的解析器会读 101 字节，越过 QNAME 进入 QTYPE/QCLASS 和答案区，把 `00 01 00 01 …` 解释成标签，最终得到乱码名字或越界。**一个字节的偏移差，就是从「合法压缩」到「解析歧义」的分界**，这也解释了为什么解析器必须做边界与长度校验。

</details>

**Q2.** 你把一条 A 记录的 TTL 从 86400 改成 60，为什么当天仍有大量用户解析到旧 IP？给出至少两个不同层的理由，并说明怎么验证「权威已经更新」。

<details><summary>答案</summary>

两个层：

1. **旧 TTL 还在生效**。改 TTL 只影响**之后新写入缓存**的条目；之前已经按 86400 缓存的条目，仍按剩余寿命继续用。要等旧条目自然过期，最坏 24 小时。正确做法是**先把 TTL 降下来，等一个旧 TTL 周期，再改记录值**，这样切换时所有缓存条目的剩余寿命都很短。
2. **本地/应用层还有一层缓存**。浏览器、JVM（`networkaddress.cache.ttl`）、systemd-resolved、连接池都可能持有旧值，它们的过期规则不受你改 TTL 影响；还有 keep-alive 连接直接连在旧 IP 上，DNS 早已更新也没用。

验证权威是否更新：`dig @<该区 NS> name A +noall +answer`，看返回的 TTL 是否等于新配置值（刚改完应是完整 TTL，而不是被递减过的数）；再 `dig SOA` 看 SERIAL 是否变化，确认 secondary 拉到了新版本。如果直连权威仍是旧值，问题在权威/发布流程，不在缓存。

</details>

**Q3.** 为什么 DNS 默认用 UDP，却又要 EDNS0 把载荷抬到 1232？1232 是怎么算出来的？如果中间网络把大于 1280 的 IP 包丢了，会发生什么？

<details><summary>答案</summary>

UDP 省掉了握手与状态：一次查询一个 RTT，服务器无需记住客户端，天然适合 anycast 和海量短查询。代价是报文不能太大——原始上限 512 字节，超过就置 `TC=1` 让客户端换 TCP。

EDNS0 用 Additional 段的一条 OPT 伪记录，把 CLASS 字段重新解释为「我能接收的 UDP 载荷」，于是双方可以协商比 512 大的值。1232 的算法是：IPv6 最小 MTU 1280 − 40 字节 IPv6 头 − 8 字节 UDP 头 = **1232**。选它而不是 4096，是为了**不触发 IP 分片**：分片只要丢一片，整个响应就废了，而 DNS 没有重传，只能超时重问。

如果路径实际 MTU 小于 1280（或中间设备丢弃大包/分片），表现为「小响应正常、大响应超时」：`dig` 可能卡住后重试，DNSSEC 域尤其明显。定位方法是 `dig +bufsize=1232` 与 `dig +tcp` 对比——TCP 能通而 UDP 大包不通，就说明路径 MTU 或分片过滤是根因。修法是降低广告载荷、放行 TCP 53，或让解析器启用「EDNS 黑洞探测」自动回退。

</details>

---

## 44.14 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| DNS 的三缺怎么被攻击、又怎么被补（投毒、Kaminsky、DNSSEC、DoH、隧道、rebinding） | → `base/45` |
| IP 头部、CIDR、NAT 与转发：DNS 报文外面的那层信封 | → `base/40` |
| UDP 与 QUIC：为什么 DNS 能只用一个 RTT，以及 DoQ 的位置 | → `base/43` |
| 手写 DNS/IP/TCP 解析器：把本篇的压缩指针与位域再过一遍 | → `base/52` |
| 网络攻防总表：每个协议缺哪一样、补在哪一层 | → `base/48` |
| TLS 与证书：CAA 记录保护的那条链 | → `base/47` |
| 注册中心与配置下发：DNS 之外的「名字 → 地址」体系 | [comp/38](../comp/38-registry-and-config.md) |
| 全书总纲：四条主线与 56 篇清单 | [0-summary](0-summary.md) |
