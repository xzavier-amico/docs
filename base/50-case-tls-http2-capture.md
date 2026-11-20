# 50. 实例剖析：TLS 1.3 与 HTTP/2、HTTP/3，从握手到 DATA

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 网络：从一根网线到一张证书（实例篇） ｜ **编号**：50 / 56
> **本篇定位**：用两组端点实验对照 HTTPS over TCP 与 HTTP/3。前者用 `MemoryBIO` 观察 TLS 记录与 HTTP/2 帧，后者用真实回环 UDP 和 QUIC 端点 qlog 观察握手、请求和流交付。两组都不改主机网络配置。
> **读法**：§50.5 手算 TLS 记录开销，§50.6 看 ALPN 对照，§50.11 看同一条 QUIC 连接上两条流在出现缺口后各自如何交付。
> **不在本篇**：TCP 状态机见 [41](41-tcp-handshake-and-close.md)，HTTP/2 语义与压缩见 [46](46-http-evolution.md)，TLS 信任与密钥见 [47](47-tls-and-certificates.md)。

## 50.1 引子：抓到了记录，不等于抓到了 HTTP 帧

一次 HTTPS 请求慢了 300 ms。客户端说“TLS 慢”，网关说“HTTP/2 流控”，抓包的人只看到若干 `Application Data`。三者不一定互相矛盾，也不一定有一个人的判断已经成立。`Application Data` 是抓包器根据外层 TLS 记录类型做出的分类，不是明文应用状态机的判定。TLS 1.3 在 `ServerHello` 后加密的握手消息也可以用这一外层类型装载。先把观察证据分类：**传输包什么时候到、TLS 把哪些字节放在同一条记录、HTTP/2 在解密后又切出哪些帧**。这三层经常不是一对一。

## 50.2 ①② 它是什么、字节边界在哪里

| 层 | 切分依据 | 本次实验能看到什么 |
| --- | --- | --- |
| IP / TCP | IP 长度、TCP 序号与数据偏移；TCP 交付有序字节流 | 不经 socket，不能声称抓到了 SYN、ACK、MTU 或重传 |
| TLS 记录 | 5 B 外头中的类型、兼容版本、密文长度 | `MemoryBIO` 中的密文字节及记录长度 |
| TLS 握手消息 | 握手类型与长度；TLS 状态机解析 | 按规范解释顺序；不把单条密文记录长度当成消息标识 |
| HTTP/2 帧 | 9 B 帧头：长度 3、类型 1、标志 1、流 ID 4 | 端点读到的明文和库解析出的事件 |
| HTTP 语义 | 伪头、普通头、DATA 组成消息 | 流 1 上的状态 `200` 与正文 `OK` |

```text
普通线上 HTTP/2: Ethernet → IP → TCP 段 → 重组 TCP 字节流
                                                ↓
                                  按 TLS 长度读出完整记录 → 认证解密
                                                ↓
                         明文缓冲 → 按 H2 帧长切分 → 按流 ID 解释
```

HTTP/2 客户端连接前言的固定 24 B 文本不是一个帧头；服务端没有这段文本前言。两个方向各有自己的 SETTINGS；帧类型与 flags 要一起解读，`0x1` 不能在所有帧里都解释成同一件事。HTTP/2 帧可以跨 TLS 记录，一条 TLS 记录也可以装多个帧；TCP 还可能把一条记录拆开。因此实际解析器应分别对 TCP 流、TLS 记录和 H2 帧做增量处理，不能假定一次 `read()` 足够。

## 50.3 ③ 走一遍：真实的 TLS 握手，内存里的传输

准备 Python 3、OpenSSL 和 `h2`。以下三段 Python 按顺序放入同一文件。证书只在临时目录生成，期限一天，信任仅限实验客户端上下文，不安装到系统信任库。客户端仍校验主机名；两次对照复用同一张实验用证书。运行的循环次数有上限，不启动后台服务。

```python
import ssl, struct, subprocess, tempfile
from pathlib import Path
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import RequestReceived, ResponseReceived, DataReceived

def records(data):
    pos, result = 0, []
    while pos < len(data):
        kind, _, length = struct.unpack_from('!BHH', data, pos)
        assert pos + 5 + length <= len(data)
        result.append((kind, length))
        pos += 5 + length
    return result

def handshake(cert, key, offer):
    s = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    s.load_cert_chain(cert, key)
    s.set_alpn_protocols(['h2'])
    c = ssl.create_default_context(cafile=str(cert))
    c.set_alpn_protocols([offer])
    s.minimum_version = c.minimum_version = ssl.TLSVersion.TLSv1_3
    ci, co, si, so = [ssl.MemoryBIO() for _ in range(4)]
    a = c.wrap_bio(ci, co, server_hostname='localhost')
    b = s.wrap_bio(si, so, server_side=True)
    done, wire = [False, False], [bytearray(), bytearray()]
    for _ in range(8):
        for i, (end, out, dest) in enumerate(((a, co, si), (b, so, ci))):
            if not done[i]:
                try:
                    end.do_handshake()
                    done[i] = True
                except ssl.SSLWantReadError:
                    pass
            data = out.read()
            wire[i].extend(data)
            if data:
                dest.write(data)
        if all(done):
            return a, b, ci, co, si, so, wire
    raise RuntimeError('handshake did not complete')
```

本例把 TLS 两端的输出 BIO 喂给对端输入 BIO，报告两个方向真实产生的 TLS 记录，不含任何二三四层包头。线上 TCP 建连先发生，通常是 SYN、SYN-ACK、ACK；没有网络观测就不能从下面的数据推断经过几台路由器、有没有丢包或哪个 TCP 段携带哪条记录。

## 50.4 ③ 走一遍：握手之后真的发送一条 HTTP 请求

```python
def h2_exchange(a, b, ci, co, si, so):
    x = H2Connection(H2Configuration(client_side=True))
    y = H2Connection(H2Configuration(client_side=False))
    x.initiate_connection()
    y.initiate_connection()
    x.send_headers(1, [(':method', 'GET'), (':scheme', 'https'),
                       (':authority', 'localhost'), (':path', '/')], end_stream=True)

    def move(hs, src, out, dest, target, hr):
        plain = hs.data_to_send()
        src.write(plain)
        ciphertext = out.read()
        dest.write(ciphertext)
        received = target.read(16384)
        assert received == plain  # 本例两次写入均很短，不是通用流解析器
        off = 24 if received.startswith(b'PRI * HTTP/2.0') else 0
        frames = []
        while off < len(received):
            length = int.from_bytes(received[off:off+3], 'big')
            stream = int.from_bytes(received[off+5:off+9], 'big') & 0x7fffffff
            frames.append((received[off+3], received[off+4], stream, length))
            off += 9 + length
        assert off == len(received)
        print('h2 frames', frames, 'TLS records', records(ciphertext))
        return hr.receive_data(received)

    events = move(x, a, co, si, b, y)
    assert any(isinstance(event, RequestReceived) for event in events)
    y.send_headers(1, [(':status', '200'), ('content-length', '2')])
    y.send_data(1, b'OK', end_stream=True)
    events = move(y, b, so, ci, a, x)
    print('status', next(e.headers for e in events if isinstance(e, ResponseReceived)),
          'body', next(e.data for e in events if isinstance(e, DataReceived)))
```

```python
with tempfile.TemporaryDirectory() as temp:
    crt, key = Path(temp) / 'cert.pem', Path(temp) / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec',
                    '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes', '-days', '1',
                    '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost',
                    '-keyout', str(key), '-out', str(crt)],
                   check=True, capture_output=True, timeout=3)
    for offer in ('h2', 'http/1.1'):
        a, b, ci, co, si, so, wire = handshake(crt, key, offer)
        print([offer], a.version(), a.selected_alpn_protocol())
        print('c->s', records(wire[0]))
        print('s->c', records(wire[1]))
        if a.selected_alpn_protocol() == 'h2':
            h2_exchange(a, b, ci, co, si, so)
```

## 50.5 两组实测输出，分别按层解释

以下为本环境运行等价脚本得到的记录。证书重新生成、ECDSA 签名、OpenSSL 版本和会话票据策略均可能影响长度；不要把下列数字当作协议常量。TLS 记录表是 `(外层类型, 载荷长)`，H2 帧表是 `(类型, flags, 流 ID, 载荷长)`。

```text
['h2'] TLSv1.3 h2
c->s [(22, 247), (20, 1), (23, 69)]
s->c [(22, 122), (20, 1), (23, 32), (23, 437), (23, 96), (23, 69), (23, 250), (23, 250)]
h2 frames [(4, 0, 0, 42), (1, 5, 1, 11)] TLS records [(23, 112)]
h2 frames [(4, 0, 0, 42), (4, 1, 0, 0), (1, 4, 1, 4), (0, 1, 1, 2)] TLS records [(23, 101)]
status [(b':status', b'200'), (b'content-length', b'2')] body b'OK'
['http/1.1'] TLSv1.3 None
c->s [(22, 253), (20, 1), (23, 69)]
s->c [(22, 122), (20, 1), (23, 23), (23, 437), (23, 97), (23, 69), (23, 250), (23, 250)]
```

### 先读握手，再读帧

| 阶段 | 这份实验能证明什么 | 规范告诉了什么 |
| --- | --- | --- |
| ClientHello / ServerHello | 两方向各有明文握手类型的第一条记录 | 客户端给出支持版本、密钥份额以及 ALPN 提议；服务端选定 TLS 参数 |
| 外层类型 20 | 两方向各有一条载荷 1 B 的兼容记录 | TLS 1.3 中兼容用的 CCS 不代表正式切换密钥，也不应按 1.2 状态机解读 |
| ServerHello 后的类型 23 | 被动观察只见密文记录长度 | TLS 1.3 全握手后续通常包含 EncryptedExtensions、证书、CertificateVerify、Finished；不能仅靠长度为每条记录认领这些名字 |
| 上层连接成功 | 端点返回 `TLSv1.3` 与 `h2`，库解析出状态和正文 | HTTP/2 是已协商的应用协议，不是从 TCP 端口号或 TLS 版本猜出来的 |

客户端的明文为 `24 + (9+42) + (9+11) = 95 B`。实测记录载荷是 `112 B`，差 17 B，与本例 TLS 1.3 无额外填充时的 `1 B 内层类型 + 16 B AEAD tag` 相符；线上该记录还要加外层 5 B，即 117 B。服务端四帧明文是 `(9+42) + 9 + (9+4) + (9+2) = 84 B`，记录载荷为 `101 B`，差仍是 17 B。服务端 DATA 的 `stream=1, flags=1` 表示结束该流的发送方向。握手之后有 SETTINGS ACK，但它确认的是对端 SETTINGS，不是 TCP ACK、更不是请求已完成。

## 50.6 ④ 改一处：ALPN 不匹配，不等于证书错误

| 条件 | 客户端只提议 `h2` | 客户端只提议 `http/1.1` |
| --- | --- | --- |
| 实验服务端支持的 ALPN | `h2` | `h2` |
| TLS 版本 | 1.3 | 1.3 |
| 本机接口返回的 ALPN 结果 | `h2` | `None` |
| 是否继续发送 H2 | 是；收到响应 | 否；实验显式停在握手阶段 |

这是**这个 OpenSSL/Python 配置的实测结果**，不意味着所有服务器在没有共同 ALPN 时都继续握手；严格实现或服务策略可以用告警结束连接。也不能把 `None` 解释成服务器已选 `http/1.1`。同理，TLS 1.3 典型全握手是 1 RTT 的协议时序，但本实验未测 RTT，不包含 TCP 建连，不证明线上没有 HelloRetryRequest，也未测 TLS 1.2 或会话恢复。把这些条件写在对比图旁边，比把本机延迟贴到生产预算里重要。

## 50.7 ⑤ 它证伪了谁：同一条记录不等于一个请求

- **“TLS 外层类型是 23，就一定是 HTTP 明文的密文。”** 不成立。许多 TLS 1.3 握手消息也放在外层类型 23 里。
- **“一个 TCP 包里能看到一帧 HTTP/2。”** 即使你在授权端点拿到了明文，也要先分别重组和切帧；从纯网络包看根本没有帧边界的直接证据。
- **“看见 TLS 1.3 就表示用户已认证。”** TLS 通道、服务身份、应用用户身份是不同的断言；见 [48](48-defense-and-app-auth.md)。
- **“加密后什么都看不到。”** 被动点通常仍见其位置允许的连接端点、时间和长度，普通 ClientHello 的部分字段也可能可见；TLS 1.3 的服务器证书不应被列为普通被动抓包必见项。ECH、代理和隧道会改变观察边界。流量特征只能支持概率性的诊断，不等同于解密或识别用户。

## 50.8 诊断与一页速记

如果排查的是自有服务连接，优先使用双方应用日志或服务端 TLS 诊断；只有需要确认重传、分片或线上时间线时，才在授权接口上做有限抓取。端点看到的应用明文、网络上的 TLS 密文和 TCP 包时间线要标不同来源。若确需用临时会话密钥帮助本地诊断，应视为敏感数据，只用于隔离测试、不进共享日志或代码仓库。

| 现象 | 首查 | 不要做的推断 |
| --- | --- | --- |
| TLS 可用，HTTP/2 不可用 | ALPN 最终结果、服务端 h2 是否启用、中继在哪终止 TLS | 不凭 `https://` 宣称使用 HTTP/2 |
| 握手记录多 | 区分初始握手、恢复、HelloRetryRequest 与新会话票据 | 不把记录条数等同于 RTT 数 |
| HTTP/2 慢但 TLS 快 | 帧/流的发送、流控、服务端处理及 TCP 层队头阻塞 | 不只看连接级吞吐就给单个流归因 |

**速记**：读网络包看传输，读 TLS 记录看分段与加密外壳，读端点解密结果才能解释 H2 帧。不要跨视角替换证据。

## 50.9 自检 3 题

1. `TLS record.length=112` 是否意味着 112 B 的 HTTP/2 数据？
2. 服务端响应所在的一条 TLS 记录为什么不是只有一条 DATA 帧？
3. 哪些日志可以区分证书错误与 ALPN 不一致？

<details><summary>答案</summary>

1. 不。该次实验有 95 B H2 明文，其余 17 B 来自记录保护；密文载荷长度还不含外层 5 B 头。
2. 本实验一次向 TLS 写入包含 SETTINGS、SETTINGS ACK、HEADERS、DATA 的 84 B；切帧时按 H2 帧头反复推进，不按记录条数判断。
3. TLS 验证输出说明链及名字是否可信；端点所见的 ALPN 结果或协议告警说明应用协议是否谈成，二者都要核对。

</details>

## 50.10 延伸阅读

[46 HTTP/2 与 HPACK](46-http-evolution.md) · [47 TLS](47-tls-and-certificates.md) · [52 解析与重组](52-case-parsing-and-reassembly.md) · [RFC 8446：TLS 1.3](https://www.rfc-editor.org/rfc/rfc8446) · [RFC 9113：HTTP/2](https://www.rfc-editor.org/rfc/rfc9113) · [RFC 7301：ALPN](https://www.rfc-editor.org/rfc/rfc7301)。

## 50.11 HTTP/3 对照：同一连接上，A 有缺口，B 能先完成吗？

前半篇的 `MemoryBIO` 能看到 TLS over TCP 路径的**记录字节**，但没有跑 TCP，也看不到它是怎样分段的。这一节换成完整的 `aioquic` 两端、真实的本机 UDP socket 和 HTTP/3 状态机。**观测来自端点 qlog 和应用事件，不是网卡抓包。** 实验不启用 ECH，也不测 RTT、吞吐或丢包恢复性能。

### 复现条件

在仓库根目录运行；需要 Python 3、OpenSSL 和 Linux 的 `timeout`。脚本 [h3-loopback.py](examples/h3-loopback.py) 只绑定 `127.0.0.1` 的临时端口，在同一进程内驱动两个端点。自签证书仅被这次实验的客户端信任，校验的名称为 `localhost`。退出时关闭 socket 并清理临时证书，不启动后台服务。安装依赖需要能访问包源，**实验自身不访问外网**。

```bash
python3 -m venv /tmp/docs-h3-venv
/tmp/docs-h3-venv/bin/python -m pip install 'aioquic==1.3.0'
timeout 6s /tmp/docs-h3-venv/bin/python base/examples/h3-loopback.py
```

### 第一步：确认这是 HTTP/3，不是“有 UDP 响应”

脚本等到两端都报告 `HandshakeCompleted` 且 ALPN 为 `h3`，才发送两个 `GET`。服务端在两条客户端发起的双向 QUIC 流（ID 为 `0` 和 `4`）上接收到请求头，再分别发回响应。两端 qlog 都记录了 `Initial / CRYPTO`、`Handshake / CRYPTO` 和 `1-RTT / STREAM`。这与 [RFC 9001](https://www.rfc-editor.org/rfc/rfc9001) 的分工一致：TLS 握手字节通过 `CRYPTO` 传输；HTTP/3 帧通过应用流发送，不在 `STREAM` 外再套一层 TLS record。实验中已用于请求的**应用流 0**，也不是握手通道。

### 第二步：让流 0 缺一段，观察流 4

| 时点 | 端点做了什么 | 客户端应当能交付什么 |
| --- | --- | --- |
| T0 | 两条流的响应头已发送；暂存包含流 0 第一段 DATA 的一个 UDP 数据报 | 只收到已交付的响应头，流 0 的正文起点还缺字节 |
| T1 | 在后续数据报发送流 0 的 `A2` | 流 0 已收到后段，仍不能跳过 `A1` 向应用交付 |
| T2 | 发送流 4 的 `B` 并结束该流 | 流 4 可完整交付 `B`，不因流 0 的缺口等待 |
| T3 | 将暂存的数据报发给客户端 | 流 0 按顺序交付 `A1A2` 并结束 |

这里的“暂存”发生在实验进程，数据报尚未写入 socket；不是在主机上随机丢包。短观察窗口内不把客户端 ACK 送回服务端，避免丢失恢复提前补掉人为制造的缺口。脚本断言暂存包只含本实验关心的流 0 数据帧，并校验流内容与完成顺序。端点使用虚拟时钟避免把机器调度抖动误当成协议行为，因此不引用墙钟时间作为性能结论。

本机运行输出（`aioquic 1.3.0`）：

```text
ALPN: h3 h3
server STREAM (id, offset, length): [(0, 5, 4), (0, 9, 4), (4, 5, 3)]
while held: [4] {0: b'', 4: b'B'}
after delivery: [0, 4] {0: b'A1A2', 4: b'B'}
client [('1RTT', 'stream'), ('handshake', 'crypto'), ('initial', 'crypto')]
server [('1RTT', 'stream'), ('handshake', 'crypto'), ('initial', 'crypto')]
```

`offset` 是**各自 QUIC 流的字节偏移**，含同一流前面的 HTTP/3 帧，既不是全连接偏移，也不是 TCP 序列号。`A1` 是 2 B 正文，在本例中连同 HTTP/3 DATA 的类型和长度字段占 4 B，所以在 qlog 看到 `length=4`，应用只读到 2 B。最后两行是从 qlog 提取的**类型集合**（已排序），不是按时间排列的抓包。没有单独观测 QUIC 包保护密钥、路径 MTU 或 ECH 接受确认。

**实验支持的结论**：流 0 的后段已经到达，但不能越过自己流内的缺口；流 4 可以先完成。这反驳的是“QUIC 和单条 TCP 一样，必须等连接里所有更早的字节”。它**不证明**任意丢包只影响一条流，也不证明总延迟一定下降。若同一个包装了两条流的必要字节，或若两条流都在等同一个 QPACK 条目，还是可能一起等；连接流控和拥塞预算也仍共享。对照 [43](43-udp-and-quic.md) 与 [46](46-http-evolution.md) 区分这些不同的等待机制。
