# 49. 实例剖析：VLESS + Reality + Vision——加密边界与字节流

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 网络：从一根网线到一张证书（实例篇） ｜ **编号**：49 / 56
> **本篇定位**：用 **Xray-core v1.8.24** 拆一条代理链路。看清 VLESS 请求头指向哪里、Reality 认证通过意味着什么、Vision 切换读写路径时为什么必须交接已缓存的字节。源码、标准、离线测试和算例各自标注，不把其中一种当成另一种证据。
> **读法**：先看 §49.3–§49.5 的字节流和缓冲区交接，再跑 §49.6 的纯内存测试。最后用 §49.7–§49.9 判断密码学开销、端点可见性与故障归属。

## 49.1 引子：一条连接上有几份“身份证”？

同一条 HTTPS 请求经中继传输。客户端到中继一段，后来中继向目标建立另一段传输。假设客户端还与目标建立了自己的 TLS，会同时出现两种身份判断：客户端如何确认外层服务，中继后面的目标如何向应用证明自己。两项确认不是同一信任关系；外层握手成功不能代替内层检查，内层认证亦不能说明中继有权处理任意用户或任意目标。先画出两段通道，再问谁持有哪段密钥，比先给流量加上“加密”标签更有用。

## 49.2 ① 它是什么：把三个职责分开

| 名称 | 此处讨论的职责 | 不应据此推出什么 |
| --- | --- | --- |
| VLESS | 中继通信中的代理控制与载荷承载；本篇讨论由外层保护的组合 | 不能把某一组合的行为当作所有版本的能力；代理控制信息也不等于业务正文 |
| Reality | 这组实现中的外层握手与认证设计；信任模型不同于直接访问受 Web PKI 校验的 HTTPS 源站 | 不表示“拥有”或“借到了”另一个站点的私钥、证书签名能力或业务身份 |
| Vision | 数据路径的处理模式；用于说明重复封装和记录处理的代价 | 不保证任意流量都会走相同分支、一定零拷贝或一定更快 |

**版本边界**：下文所说的“源码”是 [Xray-core v1.8.24 对应的提交 `6baad79`](https://github.com/XTLS/Xray-core/tree/6baad79f9881ee2cf75bdc825b3e2e92b289477a)，Reality 依赖以该版本的 [`go.mod`](https://github.com/XTLS/Xray-core/blob/6baad79f9881ee2cf75bdc825b3e2e92b289477a/go.mod#L19-L24) 为准。这是研读快照，不是版本选型建议。TLS 记录语义来自 [RFC 8446](https://www.rfc-editor.org/rfc/rfc8446)。本文不展开部署、规避检测或改变流量特征的实现。

## 49.3 ② 物理形态：先定位 VLESS 头，再谈加密边界

把中继收到的**已解包字节**摊开：前面是 VLESS 控制头，后面才是被转发的字节。这里的“已解包”很重要：若前面有 TLS，下面这张表不是网卡上能直接看见的内容。请求和响应也不是同一种头：响应没有必要再把目标地址和账号标识发一遍。

| 顺序 | 字段 | 本版本的编码与含义 |
| --- | --- | --- |
| 1 | `Version` | 1 B；本次研究的请求版本是 `0` |
| 2 | 用户标识 | 16 B 原始字节，不是带连字符的 UUID 文本；入站以该值找本地账号 |
| 3 | `Addons` | 1 B 长度 `N`，接着 `N` B；`0` 时没有附加体。非空附加体由 protobuf 解释，不是任意 ASCII 字符串 |
| 4 | 命令 | 1 B：TCP `01`、UDP `02`、Mux `03`。本版本的普通 TCP/UDP 请求带目标地址；Mux 不在这里带同样的地址字段 |
| 5 | 目标端口 | 2 B、大端，本版本的普通请求是**先端口再地址族** |
| 6 | 地址族与地址体 | `01` 后是 4 B IPv4；`02` 后是 1 B 域名长度及域名字节；`03` 后是 16 B IPv6。这个地址族字节不是 EtherType，也不要套用 SOCKS5 数值 |
| 7 | 载荷 | 位置由前面各字段确定，而不是另一个通用“首包长度”；TCP 命令下属于后续字节流 |

来源：[`encoding.go` 的请求编解码](https://github.com/XTLS/Xray-core/blob/6baad79f9881ee2cf75bdc825b3e2e92b289477a/proxy/vless/encoding/encoding.go#L21-L133)、[`地址序列化`](https://github.com/XTLS/Xray-core/blob/6baad79f9881ee2cf75bdc825b3e2e92b289477a/common/protocol/address.go#L174-L253)、[`Addons` 编码](https://github.com/XTLS/Xray-core/blob/6baad79f9881ee2cf75bdc825b3e2e92b289477a/proxy/vless/encoding/addons.go#L15-L61)。用一个**虚构标识与文档保留域名**手算 TCP 头；横线只用于标注，不在线格式中：

```text
00 | 00 01 02 03 04 05 06 07 08 09 0a 0b 0c 0d 0e 0f | 00 | 01 | 01 bb | 02 0b | 65 78 61 6d 70 6c 65 2e 63 6f 6d
 0                            1..16                     17   18   19..20   21 22                  23..33
版本                         教学标识                 N=0  TCP   443     域名 11                 example.com
```

这份头占 `1+16+1+1+2+1+1+11=34 B`。如果增加 `N` 个附加字节，命令和后续偏移都右移 `N`；解析器不能把 `18` 写成所有请求的命令偏移。如果用 IPv4 则没有域名长度字节；无附加字段时总长 `22+4=26 B`。响应只由**版本字节及同样的附加字段长度/内容**构成，空附加字段时为 `00 00`（[响应编解码](https://github.com/XTLS/Xray-core/blob/6baad79f9881ee2cf75bdc825b3e2e92b289477a/proxy/vless/encoding/encoding.go#L135-L174)）。这两个字节并不是 HTTP 状态、TLS `Finished`，也不是“目标已经收到全部数据”的确认。

## 49.4 ③ 握手完成、代理账号命中、目标身份通过：三道不同的门

```text
业务端 A  ==========================================  业务端 B
                 应用自己的安全会话（若存在）

代理端 A  =====================  中继 P  ------------  目标方向
                外层安全会话               另一段传输
```

示意图画的是**密钥控制范围**，不是路由拓扑。若应用没有独立加密，外层在中继终止后，中继可能看到业务内容。若应用独立建立了经正确验证的端到端 TLS，通常只有应用端点有那段会话的密钥；中继仍可能知道其功能所需的目标信息、连接状态和字节量。实际系统也可以是反向代理在中继终止业务 TLS、再独立与上游建 TLS，此时中继有明文可见性；不能把“两个 HTTPS”说成一条贯穿中继的端到端通道。

| 观察位置 | 在各自权限与配置下可能看见 | 不能因此证明 |
| --- | --- | --- |
| 第一段传输的被动点 | 连接、时间、记录长度，以及这一观察位置允许看到的握手元数据 | 应用明文、用户身份、代理实现归属 |
| 终止外层通道的中继 | 外层解包结果及需要处理的代理控制信息 | 不自然拥有独立内层 TLS 的密钥 |
| 内层会话的端点 | 内层 TLS 验证结果和解密后的应用协议 | 不证明途中没有其他独立连接或排队 |

在这个版本中，有三处可以直接沿源码区分：

| 判断 | 在哪里判 | 成功意味着什么 | 不意味着什么 |
| --- | --- | --- | --- |
| 外层握手与 Reality 对端识别 | [`VerifyPeerCertificate` 与 `UClient`](https://github.com/XTLS/Xray-core/blob/6baad79f9881ee2cf75bdc825b3e2e92b289477a/transport/internet/reality/reality.go#L82-L169) | 对端通过该实现专门的校验，并被标记 `Verified` | 普通 X.509 链验证成功就等价于该标记；或对端持有另一个网站的私钥 |
| VLESS 账号与流控权限 | [`DecodeRequestHeader`](https://github.com/XTLS/Xray-core/blob/6baad79f9881ee2cf75bdc825b3e2e92b289477a/proxy/vless/encoding/encoding.go#L82-L129) 查账号；[`inbound.Process`](https://github.com/XTLS/Xray-core/blob/6baad79f9881ee2cf75bdc825b3e2e92b289477a/proxy/vless/inbound/inbound.go#L441-L508) 检查账号允许的流控后才调度 | 中继识别并通过了该账号、该请求的相应检查 | 16 B 用户标识是数字签名；或者该连接后面的应用用户已完成登录 |
| 目标应用自己的 TLS | 内层客户端的证书与名称校验 | 在内层 TLS 信任模型中，连接的是预期的服务 | 中继已获得该会话密钥，或者目标已授权某项业务操作 |

第一个判断容易读错：`VerifyPeerCertificate` 的普通证书验证分支可以返回成功，但不会执行专门分支里的 `Verified = true`；`UClient` 在后面还检查这个标记，不能把 **证书链验证、握手返回和代理连接通过**写成一个布尔值。第二道门也有同样的区分：源码中的 UUID 是对账号表的查找键，比较的是值，不对这次请求做独立签名。即使日志里需要记录账号归属，也应使用单独的审计标识；不应该把完整账号值和原始请求头打进普通日志。

另一个容易混淆的量是**三个名字**：外层握手的服务器名、VLESS 请求里供中继调度的目标地址、内层 TLS 的期望服务名。在某些连接上它们可能有关联，但它们用途不同，一个字段匹配不能替另外两个负责。排查“校验证书到底用了哪个名字”时，应先说明观察的是哪一个端点、哪一段握手。

## 49.5 ③ Vision 交接读写路径：为什么不能直接把底层 socket 接上？

发送端出站的 [`postRequest`](https://github.com/XTLS/Xray-core/blob/6baad79f9881ee2cf75bdc825b3e2e92b289477a/proxy/vless/outbound/outbound.go#L190-L242) 先向缓冲 writer 写请求头，再选择 `EncodeBodyAddons` 的载荷 writer，此后才刷新缓冲并进入复制循环。接收端入站先完成请求头与账号检查，选择目标，再为对应方向创建 reader。响应方向同样先读写响应头，再进入载荷复制。这解释了为什么抓到一次大的 TCP `recv` 时，控制头与数据可能已经在同一缓冲中；但也可能到下一次读取才凑齐头，不能期待“第一读只有头、第二读才有数据”。

设应用先产生一段数据，内层 TLS 把它编码为记录，外层又把这串字节作为普通载荷。TCP 并不保留应用 `write` 的边界：一次读取可能只拿到一条 TLS 记录的前半段，也可能拿到几条拼在一起。这和 [52](52-case-parsing-and-reassembly.md) 里的“粘包”是同一个抽象：**先按当前层格式获得完整输入，再验证和消费，不依据上一次读取的大小切消息**。

```text
TCP 分段（示例） :   [头的前半段] [头剩余+部分密文] [余下密文|下一条记录开头]
记录层接收逻辑  :   缓存头部 → 检查长度上限 → 等足密文 → 验证并解密 → 继续下一条
外层解包后      :   可能才看到内层记录的一部分；内层需要独立的缓冲与解析状态
```

这一点在 [`XtlsRead`](https://github.com/XTLS/Xray-core/blob/6baad79f9881ee2cf75bdc825b3e2e92b289477a/proxy/vless/encoding/encoding.go#L176-L224) 不是抽象比喻。该函数通过 reader 拿到一批字节时，读路径状态可能**在本批**改变。源码在这次交接里把 TLS 连接已存入 `input` 和 `rawInput` 的数据并入待写缓冲，**先交给 writer**，再在后续迭代转到底层连接读取。代码分别取出两个缓冲，因此不能把它简单概括成“把 TLS 读指针换成 TCP 读指针”。`XtlsWrite` 则依据写方向的状态切换 writer；两方向有各自进度，不能假设一条连接上所有方向同时到达同一状态。

举例：底层流按序到达 `ABC DEF GHI`，包装层已经预读 `ABC DEF`，上层当前只消费了 `ABC`。若此时立刻去读底层 socket，下一批读到的是 `GHI`，`DEF` 被留在包装层里，**TCP 没有丢一个字节，应用却已经丢了三个字节**。这能解释“前几次请求很好、切换附近偶发 TLS 解码失败”的一类实现问题，但不能仅凭某个 TLS 告警就归因到这里。还需区分 `input`（已处理、尚未交付的字节）、`rawInput`（包装层读入、尚未交给上层的底层字节），以及根本没被 `read` 拿走的 socket 队列；这三者不可一概重放或拼接。

**直通与零拷贝不是同义词。** [`CopyRawConnIfExist`](https://github.com/XTLS/Xray-core/blob/6baad79f9881ee2cf75bdc825b3e2e92b289477a/proxy/proxy.go#L470-L545) 包含平台、连接类型和会话状态等判断，有进入 `TCPConn.ReadFrom` 的分支，也有走普通缓冲读写的分支。源码证明“有条件地尝试快速路径”，不能证明某次实际发生了 `splice`，更不能证明网卡、内存或接收端没做任何搬运。实际系统调用还要看 Go 版本、平台和底层句柄；完整的复制账见 [37](37-case-zero-copy.md)。

## 49.6 ④ 离线复核：同一份字节，换读取颗粒后有什么变化？

实验选三件能在文件里核验的事：34 B 的边界、头后载荷是否被误吞、长度字段与内容是否跨多次读取。下面两段 Python 3 按顺序合并运行。**不打开 socket，不启代理**。使用的是零附加字段的 TCP 教学头和一个独立的普通 UDP 长度前缀样本；不实现 Reality 握手，也不模拟 Vision 的线路格式。

```python
from io import BytesIO

HEAD = bytes.fromhex(
    '00 000102030405060708090a0b0c0d0e0f 00 01 01bb 02 0b '
    '6578616d706c652e636f6d'
)
TCP = HEAD + b'abcDEF'
UDP_BODY = bytes.fromhex('0003 616263 0002 7879')

class Capped(BytesIO):
    def __init__(self, data, cap):
        super().__init__(data)
        self.cap = cap

    def read(self, n=-1):
        return super().read(min(n, self.cap) if n >= 0 else n)

def exact(f, size):
    out = bytearray()
    while len(out) < size:
        part = f.read(size - len(out))
        if not part:
            raise EOFError('incomplete field')
        out.extend(part)
    return bytes(out)

def tcp_domain_header(f):
    version, _ident, n = exact(f, 1), exact(f, 16), exact(f, 1)[0]
    if version != b'\0' or n or exact(f, 1) != b'\x01':
        raise ValueError('only the zero-addons TCP fixture is supported')
    port = int.from_bytes(exact(f, 2), 'big')
    if exact(f, 1) != b'\x02':
        raise ValueError('expected a domain in this fixture')
    length = exact(f, 1)[0]
    if not length:
        raise ValueError('empty domain')
    return exact(f, length).decode('ascii'), port
```

```python
def datagram(f):
    n = int.from_bytes(exact(f, 2), 'big')
    if not 1 <= n <= 16:
        raise ValueError('outside fixture bound')
    return exact(f, n)

for cap in (64, 3, 1):
    f = Capped(TCP, cap)
    target = tcp_domain_header(f)
    consumed = f.tell()
    rest = f.read()
    g = Capped(UDP_BODY, cap)
    payloads = [datagram(g), datagram(g)]
    assert (target, consumed, rest) == (('example.com', 443), 34, b'abcDEF')
    assert payloads == [b'abc', b'xy'] and not g.read()
    print(cap, target, consumed, rest, payloads)

for data, decode in ((HEAD[:-1], tcp_domain_header),
                     (UDP_BODY[5:-1], datagram), (b'\0\x11', datagram)):
    try:
        decode(Capped(data, 1))
    except (EOFError, ValueError) as err:
        print(type(err).__name__, str(err))
    else:
        raise AssertionError('malformed fixture unexpectedly accepted')
```

```text
64 ('example.com', 443) 34 b'abcDEF' [b'abc', b'xy']
3 ('example.com', 443) 34 b'abcDEF' [b'abc', b'xy']
1 ('example.com', 443) 34 b'abcDEF' [b'abc', b'xy']
EOFError incomplete field
EOFError incomplete field
ValueError outside fixture bound
```

| 改动 | 如果按 `read()` 的返回边界猜 | 看字段时的结论 |
| --- | --- | --- |
| 最多 64 B 一次 | 可能把控制头与随后 6 B 载荷当成同一对象 | 消费 34 B，保留 `abcDEF` |
| 最多 3 B 一次 | 一次连 16 B 标识都读不完 | 格式仍是同一个 34 B 请求头；切片不是消息边界 |
| 最多 1 B 一次 | 连 2 B 长度也被切开 | 读足 2 B 后才知道 UDP 载荷有几字节；仍得到两个数据报 |
| 比声明少一字节 | 可能误把短读当成到达了一条短消息 | 封闭输入到 EOF 时算截断，不能把半条数据交给下一层 |

UDP 样本 `00 03 abc | 00 02 xy` 来自普通 UDP 载荷的**两字节大端长度**思路（[源码读写器](https://github.com/XTLS/Xray-core/blob/6baad79f9881ee2cf75bdc825b3e2e92b289477a/proxy/vless/encoding/addons.go#L86-L189)）。这里人为把上限设为 16 B 以便测拒绝逻辑，**不是实际 Xray 的限制**。本版本的入站流控分支不接受普通 UDP 命令直接配 Vision；Mux/XUDP 是不同路径，不能凭此样本推出其格式。另一方面，VLESS 的 TCP 命令不是“每次读前都有上述两字节长度”，业务消息边界仍由应用协议决定。

同样的 `exact` 思路出现在网络服务里，还需要超时和资源账。一个连接在声明长度之后停着不发数据时，读端“再等一点”许可保留的内存、允许等多久、关闭时清哪些状态不来自格式本身。若每连接最多挂 32 KiB 未完成深层记录，且服务允许 2,000 个同时停在这个位置的连接，仅这项缓冲上限就可达 `32 KiB × 2,000 = 62.5 MiB`，还不含 socket、TLS、队列和运行时对象。这是假设下的容量上界，不是本版本默认配额或机器实测。

### 另一个对照：模拟预读后切换读取对象

把 §49.5 的问题缩成一次内存读取：同一个底层对象上，包装层一次预读 6 B，却只向应用交付前 3 B。对照的是“立即换底层”和“先交接预读”，不是重现 Vision 的控制格式。

```python
from io import BytesIO

def handoff(drain):
    raw = BytesIO(b'ABCDEFGHI')
    prefetched = raw.read(6)
    delivered, pending = prefetched[:3], prefetched[3:]
    return delivered + (pending if drain else b'') + raw.read()

assert handoff(False) == b'ABCGHI'
assert handoff(True) == b'ABCDEFGHI'
print('skip pending:', handoff(False))
print('drain pending:', handoff(True))
```

```text
skip pending: b'ABCGHI'
drain pending: b'ABCDEFGHI'
```

这两行支持的是一个有限结论：读者切换时，省略已经读出来但尚未交付的字节，会让最终内容缺一块；重复交付同一缓冲同样是错。真实的回归条件还应覆盖缓冲为空、只缓存半个上层帧、交接后的第一个读恰好 EOF，以及读写两方向先后交接，不能只断言“连接没报错”。

## 49.7 ④ 单层与双层封装：算了开销，还缺哪些证据？

以下为**假设算例，不是网络抓包、吞吐测试或协议参数推荐**。假设小载荷 `L=1000 B`，TLS 1.3 使用 16 B 标签，无额外填充，内外各刚好封装为单条记录，暂不计握手、代理头和 TCP/IP。每加一层需 `5 B` 外部记录头、`1 B` 内层内容类型和 `16 B` AEAD 标签，共 22 B。16 KiB 是标准对 `TLSPlaintext.fragment` 的上限，不是所有密文记录的统一长度上限；不要把这张账外推到所有套件或任意大小的连写。

| 计量点 | 一层 | 两层，按假设一对一封装 |
| --- | ---: | ---: |
| 应用载荷 | 1000 B | 1000 B |
| 写到最外层字节流的记录长度 | 1022 B | 1044 B |
| 单向数据路径上的记录保护次数 | 加密一次、验证解密一次 | 每层各一对，共四个处理步骤 |
| 处理该载荷的明文可见位置 | 该会话的两个端点 | 每段对自己终止的通道可见；是否能读业务正文仍看内层是否独立 |

如果把同样的字节分两次写，或 TLS 库把多次写合成一次，上表“一对一”就不再成立。线性外推 CPU 也不可靠：固定调用开销、向量化、网卡卸载、数据复制和线程争用都会改变结果。要测自己的系统，应分别计记录数、在飞字节、CPU 时间和端到端耗时；把仅加解密的微基准与完整通路的吞吐分开。拿一个 OpenSSL 基准换算成“某代理在 10 Gbit/s 一定占多少核”并不成立。

### 再算一个反例：业务字节不变，记录数可以不同

仍用同样的 22 B 记录开销假设。若应用的 1,000 B 被内层分成两条各 500 B 的记录，交给外层的是两条各 522 B 的记录，共 1,044 B。外层把这 1,044 B 放进一条记录，最外层共 `1,044+22=1,066 B`；若分别写为两个外层记录，则是 `1,044+2×22=1,088 B`。两种形态解开后，内层看到的是相同的字节序列。反过来，看到一条 1,066 B 的外层记录，不能仅凭长度还原内层到底有几条。

同理，网卡上的**包数**不是 TLS 记录数。同一份外层记录可能横跨多个 TCP 段；TCP 重传会增加线路字节，但应用正常情况下仍只接收有序去重后的流。做账时至少要注明分母：每 MiB 应用载荷对应的记录数、每 MiB 外层字节对应的传输包数，和每 MiB 上游成功交付的数据消耗多少进程 CPU，三者分别回答不同的问题。

| 假设原因 | 对照时保持不变 | 可验证证据 |
| --- | --- | --- |
| 外层记录保护增加 CPU | 相同业务字节、并发、采样窗口与对端行为 | 进程 CPU 时间和记录层操作计数，不用接口包数代替记录数 |
| 复制开销大 | 相同的加密与负载条件 | 对比用户态读写路径、实际系统调用和 CPU 采样；不能以配置名称为证据 |
| 目标或消费者读得慢 | 相同输入、连接复用与服务行为 | 两方向的队列等待、写返回、已交付字节，和下游时间线交叉核对 |

这里还有一个统计陷阱：**除以“尝试发送的字节”不等于除以“成功交付的字节”**。如果一次切换期间丢了上层缓冲的数据，进程甚至可能更快退出，CPU 看起来更低。性能比较之前先保证两个分支得到同一份应用结果，否则衡量的是不同工作量。§49.6 的断言正是在做这一步：读取粒度变化后，目标、头边界和剩余载荷都必须保持一致。

## 49.8 ⑤ 它证伪了谁：常见误判

| 说法 | 更准确的判断 |
| --- | --- |
| “在外面再套一层，内层协议就自动变得更可信” | 只增加了一段保护；内层身份仍由内层校验，信任链不会自动合并 |
| “协商的是同一个 TLS 版本，所以握手完全等价” | 版本、认证方式、对端身份和信任锚不同；版本号不能证明同一业务身份 |
| “两层加密必定按固定倍数增加 CPU” | 只有在限定记录大小、算法与执行路径的模型里能数操作；CPU 要实测 |
| “密文长度集中就能证明某个应用或产品” | 固定心跳、协议帧、链路封装也能导致集中；只有长度，没有独立证据就不能归因 |

## 49.9 防御与排障：按最后一个成功的边界取证

对于自有服务，优先用批准的资产清单、代理日志、连接角色、证书部署档案和端点计数验证“是哪个系统在做什么”。网络侧可见性要分位置：普通被动点在 TLS 1.3 不会直接读到受握手密钥保护的 Certificate；受管理端点可以对自己参与的会话记录证书验证与 ALPN。服务名、IP 与网络运营实体不能一一对应；CDN、负载均衡、共享前端、私有 CA 和内部域名皆会造成看似异常的组合。

| 决策 | 至少补什么材料 |
| --- | --- |
| 初筛异常 | 明确观测点、样本期与正常基线；只输出有置信限制的线索 |
| 归因到具体服务 | 与端点日志、资产信息和正常变更记录交叉核对 |
| 执行控制 | 明确允许策略、误报与回退流程，保留最少必要数据 |

| 现象 | 需要确认的最后一步 | 还不能下的结论 |
| --- | --- | --- |
| TCP 建连成功却没有代理响应 | 外层握手是否完成、专门认证是否通过 | 不说明代理账号一定错，也不说明目标没有监听 |
| 代理能收头，转发没开始 | 版本、头部长度、账号与流控检查、目标调度的结果 | 不能归因到内层证书、应用 HTTP 或内层加解密 |
| 转发启动后在切换处断开 | 两方向各自状态、已读取与已交付字节数、交接处仍在缓冲的内容量 | 不应仅凭 EOF 把它称为 TCP 丢包；也可能是缓冲交接、对端关闭或更高层校验失败 |
| 有延迟抖动，但吞吐大致没变 | 区分握手、目标建立、进/出方向写阻塞；记录字节数和等待时间 | 不能先验假定是 AEAD；队列和短写也会改变尾延迟 |
| 看到服务名、证书或地址不一致 | 先区分哪段会话、哪个观察点；核对资产和代理终止位置 | 单一元数据不是访问者身份或某个实现的证明 |

## 49.10 一页速记

```text
握手兼容 ≠ 信任锚相同。
看见中继的连接 ≠ 获得内层会话密钥。
一次 read ≠ 一条 TLS 记录 ≠ 一条应用消息。
没有额外加密步骤 ≠ 没有拷贝、排队或 CPU 成本。
流量相关性 ≠ 业务身份、用户身份或产品归因。
```

## 49.11 自检题

1. 一条外层 TLS 记录解完，却只有半条内层记录，处理器应该怎么办？
2. 关闭外层会话后，是否能自动读取内层历史 TLS 明文？
3. 被动点看不见 TLS 1.3 证书，能否直接断言源端没验证证书？
4. 如果文件里显示 `00 03 61 62`，可以把 `ab` 作为一个短 UDP 数据报交付吗？如果 `recv()` 返回后连接仍开着呢？
5. 如果缓冲读写切换以后确实不再做外层记录保护，能否用来证明没有用户态复制？

<details><summary>答案</summary>

1. 保存有界的内层状态，继续到下一条外层记录；内层收齐后独立验证，不把部分字节当成完整对象。
2. 不能。外层密钥与独立内层会话密钥服务于不同端点和不同数据，关闭其中一层不给另一层密钥。
3. 不能。加密对被动点隐藏证书和端点成功验证证书可以同时成立。检查授权端点的验证结果。
4. 不能。声明 3 B 而实际只有 2 B。文件到 EOF 是截断；连接仍开着则在有界的缓冲、读取期限内等待第三字节。
5. 不能。少做外层记录保护是密码学操作的变化；是否走内核传输路径是 I/O 实现的变化，各自需要证据。

</details>

## 49.12 延伸阅读

[47 TLS 与证书](47-tls-and-certificates.md) · [48 应用层认证](48-defense-and-app-auth.md) · [50 TLS/H2 实验](50-case-tls-http2-capture.md) · [52 重组](52-case-parsing-and-reassembly.md) · [RFC 8446 §5：记录层](https://www.rfc-editor.org/rfc/rfc8446#section-5)。
