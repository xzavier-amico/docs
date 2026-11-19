# 47. TLS 与证书：密钥交换、证书链、SNI、1-RTT 与「为什么还是会被抓包」

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 网络：从一根网线到一张证书 ｜ **编号**：47 / 56
> **本篇定位**：Part IV 的第十篇。46 篇讲 HTTP 三代怎么解队头阻塞，本篇讲它们共用的那层壳：TLS 只承诺三件事——**机密性、完整性、对端身份**；前两件由 AEAD 与握手签名兑现，第三件完全押在证书链上。全篇一条推论链：`握手协商出一把只有双方知道的密钥` → `AEAD 用它同时买机密与完整` → `证书链证明对面的公钥属于这个名字` → `链上任何一环被跳过，前两件立刻归零`。
> **读法**：§47.3 的 1.2/1.3 时序图与 §47.4 的 ECDHE 概念图是全篇支点，要能默画；§47.7 的「每一环」表可原样贴进上线检查单；§47.11 的四组命令都在本机真跑过；赶时间只读 §47.7、§47.9、§47.12、§47.14。
> **不在本篇**：HTTP/2 帧与队头阻塞 → `base/46`；QUIC 的 0-RTT 与连接迁移 → `base/43`；DNS 信任模型与 DoH → `base/45`；证书签发流程、ACME 与 CA 商务不在本套范围；攻击面总表与用户身份 → `base/48`；椭圆曲线数学 → 本篇只给「它保证了什么、怎么被用错」。

---

## 47.1 引子：一行 `verify = false`，把三件事一起关掉

2026-04，某移动端团队排查「用户反馈账号异常登录」。日志里有个怪组合：**服务端从未收到这批请求，风控却看到设备在正常上报**。抓包发现请求确实发出去了，只是发到了另一个地址，且内容是明文可读的 JSON。

| 时间 | 事件 | 观测 |
| --- | --- | --- |
| 03-18 | 测试环境用自签证书，SDK 加了 `verify = false` | 联调通过，代码进主干 |
| 04-02 | 灰度 3%，一批用户连上公司出口的透明代理 | 代理用自签证书冒充 `api.example.com`，SDK 照样接受 |
| 04-05 | 风控发现「设备上报了，服务端没记录」 | 请求被转发到攻击者控制的中转 |
| 04-06 | 抓包：TLS 版本、套件都正常，issuer 是 `CN=mitm-proxy` | 加密还在，身份早已不是目标服务器 |
| 04-07 | 回滚、加 pinning、CI 禁止生产构建关闭校验 | 事故定级 P1 |

最刺眼的是：**TLS 一次都没「坏」**。版本 TLS 1.3，套件 `TLS_AES_256_GCM_SHA384`，密钥交换 X25519，握手字节一个不差。坏的只有一件事——客户端没有执行「这条链是否通向一个我信得过的根」。本地压成两行命令，效果一样：

```bash
$ curl -sS --resolve demo.local:10611:127.0.0.1 https://demo.local:10611/
curl: (60) SSL certificate problem: unable to get local issuer certificate

$ curl -sSk --resolve demo.local:10611:127.0.0.1 \
      -o /dev/null -w 'http=%{http_code} tls=%{ssl_verify_result}\n' https://demo.local:10611/
http=200 tls=20
```

加一个 `-k`，HTTP 立刻 200，而 `ssl_verify_result=20` 就摆在那里——**`20` 是 `X509_V_ERR_UNABLE_TO_GET_ISSUER_CERT_LOCALLY`，意思是「我不知道签发者是谁」**。请求成功、加密成功、身份校验失败，三件事同时成立。工程上几乎所有「HTTPS 被中间人」，都是这个组合。

所以本篇回答的不是「TLS 怎么加密」，而是：

> **TLS 承诺哪三件事？分别由哪段字节兑现？哪一件一旦缺省失败，会让另外两件变成装饰品？**

---

## 47.2 威胁模型：挡的是路径上的三种坏事，不是世界的全部

把路径攻击者记为 Dolev-Yao 模型：能读、改、删、重放、插入，但不能破解密码学原语。在这个模型里攻击只有三类，TLS 的三件事一一对应：

| 威胁 | 攻击者做什么 | TLS 用什么挡 | 挡不住的形态 |
| --- | --- | --- | --- |
| 窃听 | 复制字节，离线慢慢看 | 对称加密：没有会话密钥，密文就是噪声 | 端点自己交出明文；流量分析 |
| 篡改 | 翻转密文位、插入、删除 | AEAD 标签 + 握手 transcript 的 Finished | 应用层把未签名数据当真 |
| 冒充 | 假装是 `api.example.com` | 证书链 + 私钥 possession | 客户端跳过校验；根库被换 |

第三行有两种成本完全不同的做法：伪造证书签名要攻破 CA 或哈希；而让客户端「不校验」成本是零。**现实中 99% 的 TLS 中间人，攻击的不是密码学，是校验开关。**

它明确不解决的事：

| 不解决 | 为什么 | 真实表现 |
| --- | --- | --- |
| 端点被攻破 | TLS 在端点终止，明文必然出现在某一端内存 | 服务器被 getshell，与 TLS 无关 |
| 流量分析 | IP、端口、SNI、包长、时序全可见 | 看不出内容，看得出你在跟谁、传了多大 |
| 应用层重放 | 只保证「这条连接上的字节没被改」 | 0-RTT early data 可重放；签名请求可重放（→ `base/48`） |
| 用户身份 | TLS 只认证服务器，客户端默认匿名 | 「登录」必须在应用层再做一遍 |
| 可用性 | 打、断、限速都拦不住 | DDoS、BGP 劫持，TLS 管不了 |
| 内容可信 | 只证明域名背后的运营者，不证明它说的是真的 | 钓鱼站也能申请合法证书 |

按全书纪律给出三缺表：

| 协议 | 认证 | 完整性 | 机密性 | 缺的那一样 | 总表 |
| --- | --- | --- | --- | --- | --- |
| TLS 1.3 | ✓ 服务器（依赖证书链）；客户端需 mTLS | ✓ AEAD + Finished | ✓ AEAD | 三样都补了，但认证是**可关的开关** | `base/48` |

一句话：**TLS 是「三样都补，但第三样靠外部信任锚」的协议**。所以它的失效方式不是「加密被破」，而是「证书没人验」「根被换掉」「名字对不上」。

---

## 47.3 握手：1.3 用 1-RTT 做完的事，1.2 要 2-RTT

### 两张时序图

```
TLS 1.2（2-RTT）
Client                                        Server
  │ ClientHello (random_c, suites, SNI) ────────►│
  │◄──── ServerHello, Certificate,             │
  │      ServerKeyExchange*, ServerHelloDone    │  *ECDHE 才有
  │ ClientKeyExchange, CCS, Finished ───────────►│   第 2 趟
  │◄──────────────────── CCS, Finished ─────────┤
  │◄════════════ Application Data ══════════════►│   2-RTT 后才开始

TLS 1.3（1-RTT）
Client                                        Server
  │ ClientHello (…, key_share: 公钥 A) ─────────►│   提前猜好曲线
  │◄──── ServerHello (key_share: 公钥 B)        │
  │      {EncryptedExtensions}                  │   ← 以下全加密
  │      {Certificate} {CertificateVerify}      │   证书在 1.3 里也是密文
  │      {Finished}                             │
  │ {Finished} ─────────────────────────────────►│   客户端第 1 趟就完事
  │◄════════════ Application Data ══════════════►│   1-RTT 后开始
```

差别不在「少发一个包」，而在**密钥什么时候可用**：1.2 要等 ClientKeyExchange 到了才算完；1.3 在 ClientHello 里就递上了 DH 公钥，服务器回完第一条就能算出会话密钥，并把后续握手消息全部加密。叠加 TCP 的 1 RTT，才有「2-RTT 建连」；HTTP/2 省的是流，TLS 1.3 省的是趟。

### 真实字节对一遍

本地 `s_server` 加 `-msg` 抓一次真实握手，ClientHello 整体 311 字节（4 字节握手头 + 307 字节体）。肉眼数字段容易骗自己，所以写个解析器把这段字节走一遍：

```python
p = 4 + 2 + 32                    # 跳过 type/len、legacy_version、random
sid = b[p]; p += 1 + sid           # session_id
sl = int.from_bytes(b[p:p+2], 'big'); p += 2 + sl   # cipher_suites
p += 1 + b[p]                      # compression_methods
el = int.from_bytes(b[p:p+2], 'big'); p += 2
while p < end:                     # 扩展 TLV
    t = int.from_bytes(b[p:p+2], 'big'); l = int.from_bytes(b[p+2:p+4], 'big')
    print(t, l, b[p+4:p+4+l][:8].hex()); p += 4 + l
```

真实输出（节选注释是我加的）：

```
total handshake bytes: 311
type=01 len=307                    ← Handshake type 1 = ClientHello
legacy_version=0303                ← 故意写 1.2
session_id_len=32 (middlebox compat)
cipher_suites_len=62 -> 31 suites  ← 1.3 只有 3 个，其余 28 个是 1.2 的
compression_len=1
extensions_len=172
  ext  0 len= 15 SNI=demo.local    ← 明文域名，§47.8 的主角
  ext 11 len=  4
  ext 10 len= 22
  ext 35 len=  0
  ext 22 len=  0
  ext 23 len=  0
  ext 13 len= 42
  ext 43 len=  9 versions=0304,0303,0302,0301   ← 首选 1.3，三个 1.2 备胎
  ext 45 len=  2
  ext 51 len= 38 key_share group=29 pub=32B     ← 0x001d=x25519，32 字节公钥
```

十个扩展、172 字节，就是 ClientHello 的全部「议价内容」。逐条对上表：

| 片段 | 含义 | 推导 |
| --- | --- | --- |
| `01` / `00 01 33` | ClientHello，体长 307 | 加 4 字节头 = 311 = 记录层 `0x0137` |
| `03 03` | legacy_version = TLS 1.2 | 1.3 故意写 1.2，为了穿过只认 1.2 的中间盒 |
| `00 3e` | 31 个套件（62 字节） | 1.3 只有 3 个（`13 02 13 03 13 01`），其余 28 个是 1.2 的 |
| `00 ac` | 扩展总长 172 | 后面全是 TLV |
| `00 00 00 0f …` + ASCII | **SNI = `demo.local`** | 类型 0，内套「列表长 13、类型 0、名字长 10」 |
| `00 2b … 03 04` | supported_versions | 首选 `0x0304` = TLS 1.3，后跟三个 1.2 备胎 |
| `00 33 00 26 00 24 00 1d 00 20` | key_share | 组 `0x001d` = x25519，公钥 32 字节 |
| `13 02`（ServerHello） | 套件 `TLS_AES_256_GCM_SHA384` | 1.3 套件名**不含**密钥交换与签名算法 |

最后一行是 1.3 最重要的结构变化：**套件只描述「记录层 AEAD + 哈希」，密钥交换与认证算法搬进了扩展**。所以 `openssl ciphers -v` 里 1.3 套件是 `Kx=any Au=any`（只有 3 个），而 1.2 的名字把四件事拼在一起（`ECDHE`+`RSA`+`AESGCM`+`SHA`，共 60 个）：`ECDHE-RSA-AES128-GCM-SHA256  TLSv1.2 Kx=ECDH Au=RSA Enc=AESGCM(128) Mac=AEAD`。**组合爆炸被从套件名里消掉了**，这是 1.3 只留三个套件的原因。

### 完整握手 vs 会话恢复：一个能量出来的数字

同一条命令，第二次带上上次存下的 session：

```
$ openssl s_client -connect 127.0.0.1:10701 -servername demo.local -CAfile root.crt -sess_out s1.pem
SSL handshake has read 2396 bytes and written 396 bytes
New, TLSv1.3, Cipher is TLS_AES_256_GCM_SHA384

$ openssl s_client -connect 127.0.0.1:10701 -servername demo.local -CAfile root.crt -sess_in s1.pem
SSL handshake has read 241 bytes and written 667 bytes
Reused, TLSv1.3, Cipher is TLS_AES_256_GCM_SHA384
```

**2396 → 241 字节**，省掉的就是那 1835 字节证书加两条签名验证。但注意方向：`written` 从 396 涨到 667——客户端要带 PSK ticket。**「恢复省流量」只对服务器→客户端这一半成立**，双向总账 `(2396+396) - (241+667) = 1884` 字节，仍然划算，因为证书只在一个方向传。

### 密钥调度：一把共享秘密摊成三把

ECDHE 只产出一个 32 字节的 `Z`，HKDF 把它摊开：

```
early_secret     = HKDF-Extract(0, PSK)                      # 无 PSK 时为 0
handshake_secret = HKDF-Extract(Derive-Secret(early,"derived",""), Z)
c_hs / s_hs      = Derive-Secret(handshake_secret, "c/s hs traffic", TH)
master_secret    = HKDF-Extract(Derive-Secret(handshake_secret,"derived",""), 0)
c_ap / s_ap      = Derive-Secret(master_secret, "c/s ap traffic", TH)
```

三个要点各推出一个工程现象：① 两个方向两把钥匙，所以 nonce 不会撞；② 每一级都绑定 transcript hash `TH`，握手改一个字节，密钥全变，`Finished` 对不上，**完整性在握手阶段就解决了**；③ `early_secret` 只来自 PSK，不混入本次 ECDHE，所以 0-RTT 的前向保密弱一档。

### 0-RTT：省一个 RTT，买一个重放面

会话恢复时，客户端在第一个 flight 就带上 PSK 加密的 early data，服务器可以立刻处理。代价是**这段数据没有新鲜性证明**：攻击者录下原样重放，服务器仅凭密码学无法区分第一次和第二次——PSK 与 nonce 空间在重放时完全合法。真实恢复握手：

```
New, TLSv1.3, Cipher is TLS_AES_256_GCM_SHA384
Early data was not sent
```

第二行是关键：OpenSSL 默认不发 early data。这个默认是对的——0-RTT 只配幂等请求（GET 静态资源），服务端还要做单次 ticket、时间窗、限速。把「下单」「支付」放进 0-RTT，等于把重放攻击写进业务。

## 47.4 密钥交换：RSA 把历史押在私钥上，ECDHE 不押

静态 RSA 的会话密钥来自「客户端用服务器公钥加密的 premaster」，**服务器长期私钥直接参与密钥生成**。ECDHE 完全不同：

```
Client                                    Server
  a ← 随机标量（用完即弃）                  b ← 随机标量（用完即弃）
  A = a·G  ───────── A（32 字节）────────►
                  ◄──────── B（32 字节）────  B = b·G
  K = a·B = a·b·G                          K = b·A = a·b·G
```

线上只有 `A`、`B`；要算 `a·b·G` 必须解 CDH。长期私钥在这里只做一件事：给 transcript 签名（`CertificateVerify`），**不参与 `K`**。

```
RSA:    K = f(服务器长期私钥, 客户端随机数)   → 私钥泄 = 历史全解
ECDHE:  K = a·b·G                          → 私钥泄 = 只能冒充未来
```

假设攻击者录了两年流量，之后偷到服务器当时的证书私钥：

| 场景 | 手上有 | 能解历史吗 |
| --- | --- | --- |
| 静态 RSA | 密文 + 长期私钥 | **能**：premaster 当时就是用这把公钥加的 |
| ECDHE | 密文 + 长期私钥 + 当时的 `A`、`B` | **不能**：`a`、`b` 握手结束就随内存消失 |

这就是前向保密的全部内容：**不是「更安全」，而是把长期密钥的作用范围限制在签名上**，买到「泄露时间范围隔离」，代价是每次握手两次标量乘。本机实测（`openssl speed -seconds 1`）：

| 运算 | 每秒 | 单次 | 谁付 |
| --- | --- | --- | --- |
| X25519 标量乘 | 27,620.2 | ≈ 36.2 µs | 双方各一次 |
| P-256 ECDH | 18,543.3 | ≈ 53.9 µs | 双方各一次 |
| RSA-2048 签名 | 1,541.4 | ≈ 649 µs | 服务器 |
| ECDSA P-256 签名 | 41,193.9 | ≈ 24.3 µs | 服务器 |
| ECDSA P-256 验签 | 14,030.2 | ≈ 71.3 µs | 客户端 |

这解释了：**服务器爱 ECDSA 证书**（签名 649 → 24 µs，约 27 倍，握手是最贵的操作之一）；**客户端有时嫌弃它**（验签 71 µs vs RSA 验签的 19 µs）。现实折中是「RSA 证书 + ECDHE 交换」或双证书按客户端能力选。

用真实握手验证「有没有临时公钥」——`Server Temp Key` 这一行有没有，直接对应有没有 ECDHE：

```bash
openssl s_client -connect 127.0.0.1:10601 -tls1_2 \
    -cipher 'ECDHE-RSA-AES128-GCM-SHA256' -brief
# Ciphersuite: ECDHE-RSA-AES128-GCM-SHA256
# Server Temp Key: X25519, 253 bits        ← 有临时公钥，前向保密成立

openssl s_client -connect 127.0.0.1:10601 -tls1_2 \
    -cipher 'AES128-SHA:@SECLEVEL=0' -brief
# Ciphersuite: AES128-SHA
# （输出里没有 Server Temp Key 这一行）      ← 静态 RSA，前向保密不成立
```

**「输出里少了一行」就是一条安全结论。** 这也是为什么 TLS 1.3 的套件里根本不存在 `AES128-SHA`：静态 RSA 被整个删了。

---

## 47.5 对称加密与 AEAD：记录层的 5 个字节，和「加密 ≠ 完整性」

### 记录格式

```
 0        1        2        3        4        5 ...
+--------+-----------------+-----------------+------------------+
| type   | legacy_version  |     length      | encrypted_record |
| 1 B    |      2 B        |      2 B        |   length 字节    |
+--------+-----------------+-----------------+------------------+
 0x16 handshake  0x0303（永远写 1.2）      密文 = 明文 + 16 B tag
 0x17 application_data   0x15 alert   0x14 CCS（1.3 只为兼容保留）
```

`-msg` 抓一次真实握手，记录头序列如下：

```
>>> 16 03 01 01 37   ← ClientHello：长度 311；注意 legacy=0x0301
<<< 16 03 03 00 7a   ← ServerHello
<<< 14 03 03 00 01   ← CCS：1 字节，兼容中间盒
<<< 17 03 03 00 17   ← 23 字节「应用数据」，其实是加密的 EncryptedExtensions
<<< 17 03 03 07 4e   ← 1870 字节，里面是证书链
```

三点：① 第一条记录写 `0x0301`（TLS 1.0）、后面写 `0x0303`，这是 middlebox compatibility mode；② **`0x17` 不代表「这是应用数据」**，1.3 把握手消息也塞进 `application_data` 加密，真实内层类型放在解密后的明文末尾，所以被动抓包看到 `17 03 03` 无法区分证书还是 HTTP；③ `0x14` 的 CCS 在 1.3 里没有密码学意义。

上面第 ② 点不该只靠嘴说。把 socket 收到的**原始字节**截下来，搜证书里的名字：

```python
class Spy(socket.socket):                       # 记录服务端发来的每个字节
    def recv_into(self, b, *a):
        n = super().recv_into(b, *a); raw.extend(bytes(b[:n])); return n
ss = ctx.wrap_socket(s, server_hostname="demo.local")   # ctx 不校验，只为观察
ss.sendall(b"GET / HTTP/1.0\r\n\r\n"); ss.recv(65536)
print(ss.version(), b.count(b"Base Lab"), b.count(b"demo.local"))
```

同一个服务端、同一张证书，只改协议版本：

```
negotiated=TLSv1.3 server_bytes=2396
  raw stream contains b'demo.local': 0
  raw stream contains b'Base Lab': 0
  rec[0] type=0x16 len=122     ← ServerHello 明文
  rec[1] type=0x14 len=1       ← CCS
  rec[2] type=0x17 len=23      ← EncryptedExtensions（密文）
  rec[3] type=0x17 len=1870    ← 证书链（密文）

negotiated=TLSv1.2 server_bytes=2479
  raw stream contains b'demo.local': 3
  raw stream contains b'Base Lab': 7
  rec[0] type=0x16 len=65
  rec[1] type=0x16 len=1848    ← Certificate 明文，名字直接可读
```

**1.3 里 `demo.local` 出现 0 次，1.2 里出现 3 次。** 这不是「加密更强」，是 1.3 把握手后半段整体搬进了加密记录——被动观察者从此只能看到 `16 / 14 / 17 17 17` 这几个类型字节和长度。而 SNI 仍在 ClientHello 明文里（那是客户端→服务端方向，本节截的是服务端→客户端方向，所以看不到），这是 §47.8 的主题。

### nonce 与序列号防重放

```
nonce = iv XOR (0…0 || seq)          # iv 12 B，seq 每发一条记录 +1
AAD   = 记录头（type || legacy_version || length）
tag   = AEAD(Key, nonce, plaintext, AAD) 的最后 16 B
```

- **序列号不在线上**。接收方自己维护「我期待第几条」，收发两端按同一规则递增；丢、重、插都会让 nonce 对不上，tag 失败，连接断开。**这就是记录层的防重放**，只保证「同一条连接内不重复」；跨连接要靠上层（0-RTT 的单次 ticket、应用幂等键）。
- **AAD 绑定了记录头**。改 type 或长度，tag 立刻失效——头部也因此被保护，虽然它是明文。
- **每条记录开销**：公式是 `5 + 16 = 21`，本机实测是 **22 字节**（5 字节头 + 16 字节 tag + 1 字节内层 content type）。16 KB 满记录 0.13%，16 字节小记录 137%。

用 Python 的 `MemoryBIO` 直接量（明文 → 线上字节），比背公式硬：

```python
for n in (1, 16, 1024):
    obj.write(b"A" * n); d = out.read()
    ln = int.from_bytes(d[3:5], 'big')
    print(f"plaintext={n:5d} wire={5+ln:5d} overhead={5+ln-n:3d} B")
```

```
plaintext=    1  record_len=   18  wire=   23  overhead=22 B  = 2200.0%
plaintext=   16  record_len=   33  wire=   38  overhead=22 B  =  137.5%
plaintext= 1024  record_len= 1041  wire= 1046  overhead=22 B  =    2.1%
```

三行都是 22 字节——**开销是常数，与包大小无关**，占比随 payload 线性下降。同一次 40000 字节写入被切成 `16401 + 16401 + 7249` 三条记录（1.3 明文上限 2¹⁴=16384 加 17 字节开销），总开销 66 字节 = 0.17%：**大包自动分片，小包自己攒。**

### 为什么「加密」不等于「完整性」

以流密码/CTR 为例：

```
C = P XOR KS            # KS 是密钥流，攻击者不知道
P' = (C XOR Δ) XOR KS = P XOR Δ    # 翻转密文位，明文对应位同样翻转
```

攻击者**读不懂**明文，但可以**精确改**明文——把 `amount=100` 改成 `900` 只需知道 `1` 和 `9` 的位差。CBC 更糟：padding 在明文末尾，「报不报 padding 错」能一位一位问出明文（POODLE/Lucky13）。三种补法只有第三种对：

| 做法 | 完整性怎么来 | 失效方式 |
| --- | --- | --- |
| MAC-then-Encrypt（TLS 1.0/1.1 CBC） | 先 MAC 再加密 | padding oracle：解密先看填充，报错时间/内容泄露 |
| Encrypt-then-MAC | 先加密再对密文算 MAC | 正确，但要两次遍历、两把钥匙，且必须先验 MAC |
| **AEAD（GCM / ChaCha20-Poly1305）** | 加密与认证同一原语，tag 覆盖 AAD+密文 | 解密原子：tag 不对，一个字节明文都不吐 |

**AEAD 的关键不是「用了 MAC」，而是「认证与解密不可分割」。** 这也是 1.3 删掉 CBC、RC4、压缩、重协商的原因：每一个都长出自己的 oracle。

对称加密有多便宜？`openssl speed -evp` 16 KB 块（本机有 AES-NI）：AES-256-GCM **3.19 GB/s**（≈25.5 Gbps），ChaCha20-Poly1305 **1.78 GB/s**（≈14.3 Gbps）。拿 10 Gbps 网卡算：`1.25 GB/s ÷ 3.19 GB/s ≈ 39%` 单核；1 Gbps 上只有 ≈4%。**「加密会不会成为瓶颈」的答案：1 Gbps 几乎不是，10 Gbps 是一个核的四成，100 Gbps 必须上卸载。** 握手是另一本账：本机 `s_time -new` 跑出 1,006 次/秒（同一台机器同时跑 client 和 server，两个进程抢同一批核），即**一次完整握手 ≈ 1 ms CPU**；1 万新建连接/秒，光握手就要 10 个核。

---

## 47.6 证书：一张被签名的「名字 → 公钥」声明

证书不是「信任证明」，而是结构化数据：主体、公钥、有效期、用途，加上「签发者用私钥对前面内容的签名」：

```
Certificate:
    Data:
        Version: 3 (0x2)
        Serial Number:
            59:6f:ba:1e:cb:cb:38:7f:0a:f6:79:4e:13:24:99:46:b3:76:22:f3
        Signature Algorithm: sha256WithRSAEncryption
        Issuer: C = CN, O = Base Lab, CN = Base Lab Intermediate CA I1
        Validity
            Not Before: Nov 19 13:42:51 2026 GMT
            Not After : Dec 21 13:42:51 2027 GMT
        Subject: C = CN, O = Base Lab, CN = demo.local
        Subject Public Key Info:
            Public Key Algorithm: rsaEncryption
                Public-Key: (2048 bit)
        X509v3 extensions:
            X509v3 Basic Constraints: critical
                CA:FALSE
            X509v3 Key Usage: critical
                Digital Signature, Key Encipherment
            X509v3 Extended Key Usage:
                TLS Web Server Authentication
            X509v3 Subject Alternative Name:
                DNS:demo.local, DNS:*.demo.local, IP Address:127.0.0.1
```

| 字段 | 回答什么 | 校验时怎么用 | 写错的后果 |
| --- | --- | --- | --- |
| `Serial Number` | 在签发者处的编号 | 吊销列表按它索引 | 重复序列号会让 CRL 误杀 |
| `Issuer` | 谁签的 | 必须等于上一级的 `Subject` | 链拼不起来 |
| `Validity` | 有效期 | 与本地时间比 | 过期/未生效直接拒 |
| `Subject` | 是谁 | **不再用于域名匹配** | 只写 CN 不写 SAN，现代客户端拒绝 |
| `Subject Public Key Info` | 公钥 | 验下一级签名 / 握手签名 | 换公钥等于换身份 |
| `Basic Constraints` | 是不是 CA、能签几层 | `CA:TRUE` 才能签，`pathlen` 限层 | 非 CA 签出的子证书非法 |
| `Extended Key Usage` | 给什么协议用 | 服务器证书要 `serverAuth` | 拿客户端证书当服务器证书 |
| `SAN` | 哪些名字属于它 | 域名匹配**只看这里** | 漏一个域名就是一次故障 |
| `SKI` / `AKI` | 公钥指纹 | 把「子」和「父」对上 | 链构建变慢，交叉签名时尤甚 |

### 链：叶子 → 中间 → 根

```
┌──────────────────────────────┐
│ Root CA（自签，在系统信任库） │  subject == issuer
└──────────────┬───────────────┘
               │ 签名
┌──────────────▼──────────────┐
│ Intermediate CA              │  CA:TRUE, pathlen:0
└──────────────┬──────────────┘
               │ 签名
┌──────────────▼──────────────┐
│ Leaf（demo.local）           │  CA:FALSE, EKU=serverAuth
└──────────────────────────────┘
```

三条规则各解释一个线上故障：① 服务器必须发 leaf + 所有中间证书，**不发根**（根在客户端信任库里）；漏发中间证书是「浏览器正常、curl/Java 报错」的头号原因。② 每一环都要验签名：用父公钥解子证书的 `signature`，比对 `Issuer`/`Subject` 与 `AKI`/`SKI`。③ **信任锚是「凭据」不是「推导」**：根自签，它可信只因被放进了 `/etc/ssl/certs/ca-certificates.crt`。整条链的安全性 = 根库的完整性。

```bash
$ openssl verify -CAfile root.crt -untrusted int.crt leaf.crt
leaf.crt: OK

$ openssl verify -CAfile root.crt leaf.crt          # 故意不给中间证书
error 20 at 0 depth lookup: unable to get local issuer certificate
```

同一个 leaf、同一个根，**只差一个 `-untrusted int.crt`**，结论从 OK 变成 error 20。这就是「链不全」的全部机制。

服务器侧同样能复现——**只发 leaf、不发中间证书**，再让客户端补：

```
$ openssl s_server -accept 10731 -cert leaf.crt -key leaf.key -www   # 注意没有 -cert_chain
$ curl --cacert root.crt https://demo.local:10731/
curl: (60) SSL certificate problem: unable to get local issuer certificate

$ cat root.crt int.crt > bundle.crt          # 客户端自己把中间证书塞进信任文件
$ curl --cacert bundle.crt https://demo.local:10731/
http=200 verify=0                            ← 服务器没改，客户端「补链」就好了
```

**「浏览器正常、curl/Java 报错」的机制就在这里**：浏览器有 AIA chasing（顺着证书里的 URL 去下载缺失的中间证书）和缓存，curl 不做这件事。所以「链不全」不是「证书错了」，是「客户端拿不到那半张」。修法只有一条：把 `fullchain.pem` 发给客户端。

### 交叉签名：真实世界的链有 4 张

`example.com` 的真实链（本机 `s_client -showcerts`，四张，原样）：

```
 0 s:CN = example.com
   i:C = US, O = SSL Corporation, CN = Cloudflare TLS Issuing ECC CA 3
 1 s:C = US, O = SSL Corporation, CN = Cloudflare TLS Issuing ECC CA 3
   i:C = US, O = SSL Corporation, CN = SSL.com TLS Transit ECC CA R2
 2 s:C = US, O = SSL Corporation, CN = SSL.com TLS Transit ECC CA R2
   i:C = US, O = SSL Corporation, CN = SSL.com TLS ECC Root CA 2022
 3 s:C = US, O = SSL Corporation, CN = SSL.com TLS ECC Root CA 2022
   i:C = GB, ST = Greater Manchester, L = Salford, O = Comodo CA Limited, CN = AAA Certificate Services
```

第 3 张的 issuer 不是它自己，而是 Comodo 的老根 `AAA`——这就是**交叉签名**：新根想被信任，但老客户端根库里没有它，于是让老根给新根签一张「同名同公钥」的证书，随链发出。新客户端认得自签的新根，链在第 3 张结束；老客户端走 `AAA` 这条边也能到达信任锚。

**所以「证书链」在实现里是一张图，不是一条链**：同一主体可能有多张证书，客户端要做的是「从 leaf 出发，在证书图里搜一条到信任锚的路」。这解释了为什么老设备上「换个 CA 就好了」——不是证书错了，是它的根库里没有那条边。

顺带一个可推导的数字：本机 leaf DER 943 B、中间证书 892 B，两张 1835 B；`s_client` 报告 `SSL handshake has read 2396 bytes`——剩下约 560 B 是 ServerHello、CertificateVerify、Finished 与记录头。**首连要传 2 KB 以上证书，这正是会话恢复存在的理由。**

---

## 47.7 校验的每一环：漏掉任何一环，等于把身份交给攻击者

七步，每一步对应一个具体错误码和一种攻击：

| # | 检查 | 失败时 | 错误码 | 跳过会怎样 |
| --- | --- | --- | --- | --- |
| ① | 从 leaf 沿 issuer 找到信任锚 | 拒绝 | 20/21 | 任何自签证书都能冒充 |
| ② | 每环签名用父公钥验得过 | 拒绝 | 7/8 | 可伪造「看起来是 CA 签的」证书 |
| ③ | 当前时间在 `Not Before`–`Not After` 内 | 拒绝 | 10 | 泄露的旧私钥永久可用 |
| ④ | 目标域名匹配 **SAN** | 拒绝 | 62 | 用别的域名的合法证书冒充 |
| ⑤ | `EKU` 含 `serverAuth`（或缺失） | 拒绝 | 26 | 拿客户端/代码签名证书当服务器证书 |
| ⑥ | 中间证书 `CA:TRUE`、`pathlen` 未超 | 拒绝 | 24/25 | leaf 能再签子证书，链被无限延长 |
| ⑦ | 未被吊销（CRL / OCSP / staple） | 视策略 | 23 | 私钥泄露后有效期内仍被接受 |

### 域名只看 SAN，CN 已经不看了

生成一张「CN = `demo.local`，SAN = `other.local`」的证书：

```
$ openssl x509 -in cnonly.crt -noout -subject -ext subjectAltName
subject=C = CN, O = Base Lab, CN = demo.local
X509v3 Subject Alternative Name:
    DNS:other.local

$ curl -sS --resolve demo.local:10612:127.0.0.1 --cacert root.crt \
       https://demo.local:10612/
curl: (60) SSL: no alternative certificate subject name matches target host name 'demo.local'
```

**CN 一模一样，照样被拒。** 这不是洁癖：CN 是自由文本、没有多名字标准写法；SAN 是结构化 `dNSName` 列表，天然支持多域名与通配符。所以现代客户端**只要证书里有 SAN，就完全不看 CN**；只有无 SAN 的老证书才回退（Chrome 从 58 起连回退都删了）。

### 有效期 / 用途 / 自签，各报各的错

把「跳过某一环」逐条做出来，全部是本机真实输出：

```
# ① 过期（notAfter 比当前早一天：-days -1）
$ openssl verify -CAfile root.crt -untrusted int.crt exp.crt
error 10 at 0 depth lookup: certificate has expired

# ③ 时钟错位也能造出「过期/来自未来」，不用改证书
$ openssl verify -CAfile root.crt -untrusted int.crt -attime 1900000000 leaf.crt
error 10 at 0 depth lookup: certificate has expired        # 2030 年看今天
$ openssl verify -CAfile root.crt -untrusted int.crt -attime 1600000000 leaf.crt
error 9 at 2 depth lookup: certificate is not yet valid    # 2020 年看今天

# ④ 域名：CN 对上、SAN 对不上
$ openssl verify -CAfile root.crt -untrusted int.crt -verify_hostname other.local leaf.crt
error 62 at 0 depth lookup: hostname mismatch

# ⑤ EKU 只有 clientAuth，却当服务器证书
$ openssl verify -CAfile root.crt -untrusted int.crt -purpose sslserver eku.crt
error 26 at 0 depth lookup: unsuitable certificate purpose
$ openssl verify -CAfile root.crt -untrusted int.crt -purpose sslclient eku.crt
eku.crt: OK                                   ← 同一张证书，换个用途就合法

# ⑥ pathlen：让 int（pathlen:0）再签一个中间 CA
$ openssl verify -CAfile root.crt -untrusted int.crt -untrusted sub.crt deep.crt
error 25 at 2 depth lookup: path length constraint exceeded

# ⑦ 吊销：把 leaf 的序列号写进 CRL
$ openssl verify -CAfile root.crt -untrusted int.crt -CRLfile int.crl -crl_check leaf.crt
error 23 at 0 depth lookup: certificate revoked
$ openssl verify -CAfile root.crt -untrusted int.crt leaf.crt    # 不加 -crl_check
leaf.crt: OK                                  ← 不查就永远 OK，这就是「尽力而为」

# ⑧ 自签：链的终点不在信任库里
$ openssl verify self.crt
error 18 at 0 depth lookup: self-signed certificate
```

八条输出里藏着三个必须记住的事实：

1. **错误码是分层的**：`10/9` 是时间、`18/20` 是链、`23` 是吊销、`25` 是层级、`26` 是用途、`62` 是名字。看到码就知道该改哪一行配置，不用猜。
2. **`-crl_check` 不加就永远 OK**——吊销是「你要求它才查」，不是默认行为。这就是 §47.7 表里第 ⑦ 行写「视策略」的原因。
3. **同一张证书换个 `-purpose` 结论就翻转**（`error 26` ↔ `OK`）。用途检查不是证书属性，是「你拿它干什么」的匹配。

浏览器那句「你的连接不是私密连接」，背后就是这张码表里的某一个数字。**浏览器没有魔法，它只是把这七步全做了一遍，而且不给你关掉的按钮**（企业策略除外）。

### 吊销：三种机制，一个共同软肋

| 机制 | 谁去查 | 延迟 | 隐私 | 现实 |
| --- | --- | --- | --- | --- |
| CRL | 客户端下整张列表 | 小时–天 | 好 | 列表越来越大，浏览器基本不拉 |
| OCSP | 客户端实时问 CA | 一次 RTT | **差**：CA 知道你访问谁 | 常被软失败，Cloudflare 等已放弃 |
| OCSP stapling | 服务器代问，握手时带上 | 服务器缓存 | 好 | 配错就静默失效 |
| 短有效期 | 不查，过期即失效 | 0 | 好 | 需 ACME 自动化；抓到的真实 leaf 就是 90 天 |

**吊销在现代 TLS 里是「尽力而为」**。真正的兜底是「短有效期 + 自动续期 + CT 监控」：有效期越短，私钥泄露窗口越小；CT 让「有人给你的域名签了一张证书」变成可观测事件。

---

## 47.8 SNI 与多租户：一个 IP 托管一万个域名，靠明文里一个字符串

服务器在第一步就要决定用哪张证书，而这时还没有会话密钥——**鸡生蛋**：要加密 ClientHello 得先有密钥，要有密钥得先知道用哪张证书，要知道用哪张证书得先读 ClientHello。所以 `server_name` 扩展只能明文。

服务端视角的原始字节（`s_server -tlsextdebug`，真实输出）：

```
TLS client extension "server name" (id=0), len=16
0000 - 00 0e 00 00 0b 6f 74 68 65 72 2e 6c 6f 63 61 6c   .....other.local
TLS client extension "supported_versions" (id=43), len=9
0000 - 08 03 04 03 03 03 02 03 01                        .........
TLS client extension "key share" (id=51), len=38
0000 - 00 24 00 1d 00 20 34 9e-44 3d 5c 45 7b c2 a1 d2   .$... 4.D=\E{...
```

`6f 74 68 65 72 2e 6c 6f 63 61 6c` 就是 ASCII 的 `other.local`；旁边的 `key_share` 32 字节公钥也是明文——公钥不怕看，怕的是它旁边那个名字。

同一台服务器挂两张证书（`-cert/-cert2`），SNI 不同，拿到的证书就不同，而服务端日志里 `Switching server context` 那一行就是选择发生的瞬间：

```
$ s_client -servername other.local | grep '^subject='
subject=C = CN, O = Base Lab, CN = other.local
$ s_client -servername demo.local  | grep '^subject='
subject=C = CN, O = Base Lab, CN = demo.local

（服务端 -tlsextdebug）
Hostname in TLS extension: "other.local"
Switching server context.
```

| 做法 | 在哪读 SNI | 优点 | 代价 |
| --- | --- | --- | --- |
| L4 预读（`ssl_preread`） | 不解密，只读 ClientHello | 后端各自终止 TLS；转发极快 | 只能按名字路由，不能改请求 |
| L7 终止（nginx `server_name`） | 自己终止 TLS，按 SNI 选 server 块 | 能改头、缓存、限流 | 私钥集中在入口；入口是单点 |
| 单证书多 SAN | 不选，一张覆盖多个名字 | 运维简单 | SAN 有上限；一张泄露全泄；CT 公开所有名字 |
| 泛域名 `*.example.com` | 同上 | 新增子域不用重签 | 只覆盖一级；`a.b.example.com` 不匹配 |

「只覆盖一级」这条能直接验，不用背：`leaf.crt` 的 SAN 里就有 `DNS:*.demo.local`，拿它去匹配两级子域：

```
$ openssl verify -CAfile root.crt -untrusted int.crt -verify_hostname a.demo.local leaf.crt
leaf.crt: OK                                      ← 一级，通配符命中
$ openssl verify -CAfile root.crt -untrusted int.crt -verify_hostname a.b.demo.local leaf.crt
error 62 at 0 depth lookup: hostname mismatch     ← 两级，通配符不跨点
$ openssl verify -CAfile root.crt -untrusted int.crt -verify_ip 127.0.0.1 leaf.crt
leaf.crt: OK                                      ← IP 走 iPAddress SAN，与 DNS 分开匹配
```

**通配符只吃一个 label**，这是 RFC 6125 的硬规定，不是实现偷懒。所以「一张 `*.example.com` 覆盖所有子域」这个念头，在三级子域面前立刻破功。

反过来的坑更常见：**客户端不发 SNI**（老 SDK、IP 直连、`-noservername`），服务器只能退回默认证书：

```
$ openssl s_client -connect 127.0.0.1:10621 -noservername | grep '^subject='
subject=C = CN, O = Base Lab, CN = demo.local     ← 拿到默认那张
$ openssl s_client -connect 127.0.0.1:10621 -servername other.local | grep '^subject='
subject=C = CN, O = Base Lab, CN = other.local    ← 拿到该名字那张
```

同一个端口、同一个进程，**只差一个 24 字节的扩展，证书就换了**。这就是「一个 IP 托管一万个域名」的全部机制，也是「IP 直连 HTTPS 必报错」的根。

ALPN 在同一个 443 上再分一层：ClientHello 带 `h2`、`http/1.1` 列表，服务器选一个写进 `EncryptedExtensions`（curl 会打 `* ALPN: offers h2,http/1.1` 与 `* ALPN: server accepted h2`）。**所以 443 上有两层键**：SNI 决定哪个站点，ALPN 决定哪种协议。这也是「只放行 443 等于放行一切」的原因——两个键都可以是任意值。

ECH（Encrypted Client Hello）想补掉 SNI：客户端从 DNS 的 HTTPS 记录拿服务器公钥，加密整个 ClientHello，外层只留 cover name。落地慢有三个硬原因：要先有可信 DNS（否则公钥被劫持，→ `base/45`）；中间盒全部失效（防火墙、CDN 都靠 SNI 分流）；需要两端同时升级且失败要能优雅回退。**所以到 2026 年，SNI 明文仍是流量分析的第一根支柱。**

---

## 47.9 为什么还是会被抓包：五条路径，五种检测

**TLS 从没承诺「别人不知道你在干什么」，只承诺「别人改不了、看不懂你发的内容」。** 前三条是身份校验被绕过，后两条是元数据与端点。

| 路径 | 怎么做 | 客户端表现 | 检测 |
| --- | --- | --- | --- |
| ① 跳过校验 | `curl -k`、`verify=False`、`TrustAllCerts`、空 `TrustManager` | 一切正常 | `curl -w '%{ssl_verify_result}'` 应为 0；CI grep `verify.*false` |
| ② 企业 MITM | 出口终止 TLS，用企业根重签 | 一切正常 | `s_client \| grep issuer`：应是目标站 CA，不是 `CN=Corp Root CA` |
| ③ 错签/泄露 | CA 验证出错、域名被抢注、内部人员错签 | 一切正常 | CT 日志订阅 + CAA + pinning |
| ④ 流量分析 | 读 IP/SNI/包长/时序 | 一切正常 | 只看头就能画出访问图：SNI 明文 + 记录长度序列 |
| ⑤ 端点被控 | getshell、木马、扩展读 DOM、内存 dump | 一切正常 | 主机侧 EDR/完整性，TLS 层无解 |

**五行「客户端表现」全是「一切正常」**——TLS 的失效是静默的：它不报错，只让你以为在跟正确的人说话。

第 ① 条用一条命令就能自查，`ssl_verify_result` 是 curl 把 OpenSSL 的 `X509_V_ERR_*` 原样吐出来的字段：

```
$ curl -sSk --resolve demo.local:10611:127.0.0.1 -o /dev/null \
    -w 'http=%{http_code} verify=%{ssl_verify_result}\n' https://demo.local:10611/
http=200 verify=20            ← 20 = UNABLE_TO_GET_ISSUER_CERT_LOCALLY

$ curl -sS --resolve demo.local:10611:127.0.0.1 --cacert root.crt -o /dev/null \
    -w 'http=%{http_code} verify=%{ssl_verify_result}\n' https://demo.local:10611/
http=200 verify=0             ← 0 才是对的
```

**`http=200` 和 `verify=20` 可以同时出现**，这是整篇最该记住的一对数字。所以生产拨测里只断言「HTTP 200」等于什么都没验——必须同时断言 `ssl_verify_result == 0`。

第 ② 条的形态值得画出来，因为它在密码学上完全合法：

```
客户端 ──TLS──► 代理（用企业根签的假证书）──TLS──► 目标站
         ▲
         └── 客户端看到「企业 CA 签发」，不是「目标站的 CA」
```

修法不是「别用代理」，而是**证书固定**：只接受指定公钥（SPKI）或指定 CA。代价是轮换要发版，所以移动端常用「pin 一组备用公钥 + pin 有效期」的折中。

本机能复现，而且结论比文字硬——**同一个 MITM 服务端，客户端的结论完全取决于它的根库**：

```bash
# 造一个「企业根」，用它给 demo.local 签一张完全合法的假证书（SAN 仍是 demo.local）
openssl req -x509 -newkey rsa:2048 -nodes -keyout corp.key -out corp.crt -days 3650 \
  -subj "/C=CN/O=Corp IT/CN=Corp Root CA" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign"
openssl x509 -req -in mitm.csr -CA corp.crt -CAkey corp.key -CAcreateserial \
  -out mitm.crt -days 397 -extfile ext.cnf -extensions v3_leaf
openssl s_server -accept 10671 -cert mitm.crt -key mitm.key -www
```

```
$ curl --cacert root.crt https://demo.local:10671/      # 只信公共根
curl: (60) SSL certificate problem: unable to get local issuer certificate
$ curl --cacert corp.crt https://demo.local:10671/      # 装了企业根
http=200 verify=0                                        ← 七步全过，一切正常
$ s_client -connect 127.0.0.1:10671 -servername demo.local | grep -E '^(subject|issuer)='
subject=C = CN, O = Corp IT, CN = demo.local
issuer=C = CN, O = Corp IT, CN = Corp Root CA            ← 唯一的破绽在这一行
```

`verify=0` 与 `http=200` 同时成立——**这就是 MITM 能静默工作的全部原因**：证书链完全合法，只是链的根不是你以为的那个。检测点只有一个：**`issuer` 必须是你预期的 CA**。这也解释了为什么 pinning 有效：它把「信任锚」从系统根库挪进了应用二进制里。

第 ④ 条能推出什么：

| 可见的 | 从哪来 | 能推出什么 |
| --- | --- | --- |
| 目标 IP/端口 | IP/TCP 头 | 哪家云、哪个 CDN |
| **SNI** | ClientHello 明文扩展 | **确切域名**（除非 ECH） |
| 包长与方向 | IP 长度、TCP seq | 页面大小、下载还是上传、视频码率 |
| 时序与间隔 | 到达时间 | 交互式还是批量、有没有人在打字 |
| 连接时长/次数 | TCP 状态 | 长轮询、SSE、心跳周期（→ `go/39`） |

**加密只加了外壳**：IP、TCP、SNI 全在壳外。这也是 ◆49 的主题——TLS 借用、record 层复用、长度指纹与填充，全是在和这一栏搏斗。

---

## 47.10 mTLS：把「认证」从服务器扩展到客户端

普通 TLS 只认证服务器。mTLS 在握手里加两步：

```
Server                                        Client
  │◄──────────── ClientHello ──────────────────┤
  ├─────────────► ServerHello / Certificate ───┤
  │  CertificateRequest（1.3 里是加密的）        │  ← 我要你的证书
  │◄───────────── Certificate（客户端链）───────┤
  │◄───────────── CertificateVerify ────────────┤  ← 用私钥签 transcript
  │◄───────────── Finished ─────────────────────┤
```

关键是 `CertificateVerify`：不只要「出示证书」，还要**用对应私钥对 transcript 签名**。偷到证书文件不够，还得有私钥；且签名绑定本次握手，**不能跨连接重放**。

服务端要求客户端证书（`-Verify 1 -CAfile root.crt`），客户端不带：

```
$ echo | openssl s_client -connect 127.0.0.1:10630 -servername demo.local -CAfile root.crt
40A75BFD3D7F0000:error:0A00045C:SSL routines:ssl3_read_bytes:
tlsv13 alert certificate required:../ssl/record/rec_layer_s3.c:1602:SSL alert number 116
```

`alert 116` 就是 `certificate_required`。带上客户端证书（`CN=svc-a.base-lab`，EKU=clientAuth），**服务端**日志出现：

```
depth=0 C = CN, O = Base Lab, CN = svc-a.base-lab
verify return:1
```

注意这行是服务端打的——**服务端在验客户端**，方向正好相反。客户端那一侧同时能看到 `Verification: OK`（它验的是服务端证书）。两个方向各自独立验，互不代表。

**同一条 TCP 连接上，服务器现在知道「对面是哪个服务」了**——这是 TLS 层唯一能拿到的、经过密码学证明的客户端身份。

| 维度 | 用户名密码 | Bearer Token | mTLS |
| --- | --- | --- | --- |
| 证明「谁」 | 知道密码的人 | 持有 token 的人 | **持有私钥的进程/主机** |
| 会不会被偷 | 会（撞库、钓鱼） | 会（XSS、日志、URL） | 私钥文件泄露才会 |
| 能否重放 | 能（除非加挑战） | **能**（到期前谁拿到谁用） | 不能（签名绑定本次握手） |
| 用户级语义 | 有 | 有（claim） | **没有**，只到服务/主机 |
| 撤销 | 改密码 | 黑名单/短 TTL | 短 TTL + allowlist，最麻烦 |

**mTLS 的定位不是替代登录，而是服务间身份**：B 需要确认「请求真的来自 A 的进程，而不是任何拿到 token 的人」。它把「内网就可信」换成了「密码学身份可信」。

轮转才是真难点：① 短有效期 + 自动轮转（SPIRE/Istio 给每个 workload 签 24h 证书，进程内热加载）；② 身份用 SAN URI 而不是 CN（`spiffe://cluster/ns/prod/sa/payments`，按 URI 授权）；③ 吊销靠短 TTL 而非 CRL；④ 私钥不进镜像（临时卷、KMS 签名、本地 agent，避免 `docker save` 带走）。

---

## 47.11 实测：一条完整链路，从自签 CA 到 `curl -v`

全部在本机真实执行（OpenSSL 3.0.22）。无外网可把第 ④ 步换成任意本地 `s_server`。

### ① 造链：root → intermediate → leaf

```bash
openssl req -x509 -newkey rsa:2048 -nodes -keyout root.key -out root.crt -days 3650 \
  -subj "/C=CN/O=Base Lab/CN=Base Lab Root CA R1" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign"

openssl req -newkey rsa:2048 -nodes -keyout int.key -out int.csr \
  -subj "/C=CN/O=Base Lab/CN=Base Lab Intermediate CA I1"
openssl x509 -req -in int.csr -CA root.crt -CAkey root.key -CAcreateserial \
  -out int.crt -days 1825 -extfile ext.cnf -extensions v3_int

openssl req -newkey rsa:2048 -nodes -keyout leaf.key -out leaf.csr \
  -subj "/C=CN/O=Base Lab/CN=demo.local"
openssl x509 -req -in leaf.csr -CA int.crt -CAkey int.key -CAcreateserial \
  -out leaf.crt -days 397 -extfile ext.cnf -extensions v3_leaf
```

`v3_leaf` 段就是 §47.6 那张表：`CA:FALSE`、`keyUsage=digitalSignature,keyEncipherment`、`EKU=serverAuth`、`SAN=DNS:demo.local,DNS:*.demo.local,IP:127.0.0.1`。

### ② 起服务，看握手与证书链

```bash
openssl s_server -accept 10601 -cert leaf.crt -cert_chain int.crt \
                 -key leaf.key -www -naccept 8
echo | openssl s_client -connect 127.0.0.1:10601 -servername demo.local \
       -CAfile root.crt -showcerts -brief
```

```
CONNECTION ESTABLISHED
Protocol version: TLSv1.3
Ciphersuite: TLS_AES_256_GCM_SHA384
Peer certificate: C = CN, O = Base Lab, CN = demo.local
Hash used: SHA256
Signature type: RSA-PSS
Verification: OK
Server Temp Key: X25519, 253 bits
```

四个字段各管一件事：`TLSv1.3` 是版本；`TLS_AES_256_GCM_SHA384` 是记录层 AEAD + 哈希；`Server Temp Key: X25519` 证明是 ECDHE（有前向保密）；`Verification: OK` 说明七步全过。`-showcerts` 打出的链见 §47.6——**第 0 张的 `i` 必须等于第 1 张的 `s`，第 1 张的 `i` 必须等于根库里的某个 subject**，这就是「链」的定义。

### ③ `x509 -text` 与 `verify`

```bash
openssl x509 -in leaf.crt -noout -text
openssl verify -CAfile root.crt -untrusted int.crt leaf.crt    # leaf.crt: OK
openssl verify -CAfile root.crt leaf.crt
# error 20 at 0 depth lookup: unable to get local issuer certificate
```

三个关键扩展（`openssl x509 -noout -ext …`）：

```
root.crt:  CA:TRUE, Key Usage: Certificate Sign, CRL Sign
int.crt:   CA:TRUE, pathlen:0
leaf.crt:  CA:FALSE, EKU: TLS Web Server Authentication,
           SAN: demo.local / *.demo.local / 127.0.0.1
```

**`pathlen:0` 的意思是「这个中间 CA 只能签 leaf，不能再签中间 CA」**——多一层多一个失守点，所以公共 CA 的层级被刻意压平。这条不是空话：让 `int` 再签一个 `sub`，`sub` 再签 leaf，`openssl verify` 直接给 `error 25 ... path length constraint exceeded`。

### ④ `curl -v`：真实站点的版本、套件与 ALPN

```bash
curl -sv -o /dev/null https://example.com 2>&1 \
  | grep -E "ALPN|TLSv|SSL connection|subject|issuer|verify ok|HTTP/"
```

节选（只留 TLS 相关）：

```
* ALPN: offers h2,http/1.1
* TLSv1.3 (OUT), TLS handshake, Client hello (1):
* TLSv1.3 (IN), TLS handshake, Server hello (2):
* TLSv1.3 (IN), TLS handshake, Encrypted Extensions (8):
* TLSv1.3 (IN), TLS handshake, Certificate (11):
* TLSv1.3 (IN), TLS handshake, CERT verify (15):
* TLSv1.3 (IN), TLS handshake, Finished (20):
* TLSv1.3 (OUT), TLS handshake, Finished (20):
* SSL connection using TLSv1.3 / TLS_AES_256_GCM_SHA384
* ALPN: server accepted h2
*  subject: CN=example.com
*  expire date: Dec 25 22:56:35 2026 GMT
*  issuer: C=US; O=SSL Corporation; CN=Cloudflare TLS Issuing ECC CA 3
*  SSL certificate verify ok.
< HTTP/2 200
```

这九行就是一次 TLS 1.3 握手的人类可读版。**注意 `Certificate (11)` 在 1.3 里是密文**，抓包看不到 `subject`；那行 `subject` 是 curl 解密后打印的。

### ⑤ 把「1-RTT」量出来

在一对 veth 上双向各加 20 ms 延迟（RTT = 40 ms，`ping` 实测 42–66 ms），然后从 netns 里分别用 1.2/1.3 建连：

```bash
ip netns add t47
ip link add v47h type veth peer name v47n && ip link set v47n netns t47
ip addr add 10.47.0.1/24 dev v47h && ip link set v47h up
ip netns exec t47 ip addr add 10.47.0.2/24 dev v47n
ip netns exec t47 ip link set v47n up
tc qdisc add dev v47h root netem delay 20ms
ip netns exec t47 tc qdisc add dev v47n root netem delay 20ms

ip netns exec t47 curl -sS --tlsv1.2 --tls-max 1.2 \
  --resolve demo.local:10641:10.47.0.1 --cacert root.crt -o /dev/null \
  -w "connect=%{time_connect} appconnect=%{time_appconnect}\n" https://demo.local:10641/
```

| 版本 | connect | appconnect | 握手增量 | 折合 RTT | 总建连 |
| --- | --- | --- | --- | --- | --- |
| TLS 1.2 | 0.0402 | 0.1255 | ≈ 85 ms | 2.1 RTT | 3 RTT |
| TLS 1.3 | 0.0402 | 0.0845 | ≈ 44 ms | 1.1 RTT | **2 RTT** |

三次重复的原始数字（说明这不是单次抖动）：

```
TLS1.3: connect=0.040162 appconnect=0.085103
        connect=0.040162 appconnect=0.084488
        connect=0.040181 appconnect=0.084836
TLS1.2: connect=0.040165 appconnect=0.125519
        connect=0.040147 appconnect=0.127522
        connect=0.040169 appconnect=0.125010
```

`connect ≈ 40 ms` = TCP 1 RTT（20 去 + 20 回）。`appconnect - connect`：1.3 是 44 ms ≈ 1 RTT，1.2 是 85 ms ≈ 2 RTT。**1.3 省掉的就是那一个 RTT**，不是「握手更快」这种模糊说法。跨洋 RTT 150 ms 时，1.2 要多等 150 ms，而 1.3 不用——这就是为什么 1.3 对高延迟链路的价值远大于对同机房的价值。

顺带把 1.2 的「2-RTT」在报文层数出来（`-msg` 里的 flight 分界）：

```
>>> ClientHello
<<< ServerHello / Certificate(1848) / ServerKeyExchange / ServerHelloDone    ← flight 1
>>> ClientKeyExchange / CCS / Finished                                        ← flight 2
<<< NewSessionTicket / CCS / Finished                                         ← flight 3
```

三条 `<<<` flight 里，只有前两条是握手必需，第三条与客户端的 flight 2 在时间上重叠。所以 1.2 的握手本身是 2 RTT，加 TCP 共 3 RTT——和上表一致。

---

## 47.12 三列账：TLS 承诺了什么、收了多少、从哪开始失效

| 维度 | 承诺了什么 | 为此付了什么 | 从哪里开始失效 |
| --- | --- | --- | --- |
| 机密性 | 路径上的第三者看不懂内容 | 22 B/记录；AES-GCM 在 10 Gbps 上约 39% 单核 | 端点被控；密钥泄露；0-RTT early data |
| 完整性 | 改一个字节就断连 | AEAD 的 CPU；序列号状态 | 应用层把未签名数据当真；关掉校验后中间人可任意改 |
| 对端身份 | 对面持有 `example.com` 的私钥 | 首连 2 KB+ 证书；CA 运维；七步校验 | **跳过校验 / 装错根 / SAN 漏域名 / 链不全 / 过期** |
| 前向保密 | 私钥以后泄露，解不开历史 | 每握手两次标量乘（X25519 ≈ 36 µs） | 静态 RSA；PSK 恢复的 early data；临时私钥被 dump |
| 1-RTT 建连 | 1 个 RTT 后就能发数据 | 客户端要预生成 `key_share`，猜错要 HelloRetryRequest（多 1 RTT） | 高 RTT；首连无 PSK；证书链太大导致分包 |
| 会话恢复 | 二次连接省证书传输 | 服务端维护 ticket 密钥 | 0-RTT 重放；ticket 密钥轮转不当 |
| 互操作 | 任何客户端都能连任何服务器 | 兼容老中间盒（legacy_version、CCS、假 session id） | 中间盒只认 1.2；老客户端不认新根（靠交叉签名兜） |

一句话收束：**TLS 买到的是「一条只有双方能读写的管道」，付出的是「握手 RTT + 对称加密 CPU + 一整套证书运维」，而失效点几乎全在「身份」这一列——因为前两列是数学，第三列是信任。**

---

## 47.13 踩坑清单：现象 → 根因 → 定位 → 修 → 防复发

| 现象 | 根因（指回本篇） | 定位命令 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 浏览器能开，`curl`/Java/Go 报 `unable to get local issuer certificate` | 只发了 leaf，没发中间证书（§47.6） | `s_client -showcerts` 数张数；`openssl verify -CAfile root.crt leaf.crt` | 拼成 `fullchain.pem` | 上线检查单加「`s_client` 必须看到 ≥2 张证书」 |
| 同一 IP 多站点，访问 B 拿到 A 的证书 | 客户端没发 SNI（老 SDK、IP 直连、`-noservername`） | `s_client -servername b` 对比 `-noservername`；服务端 `-tlsextdebug` | 客户端强制 SNI；服务端配一张默认「拒绝页」证书 | 禁止用 IP 直连 HTTPS |
| 全站 `certificate has expired`，监控没告警 | 续期失败 / 时钟不同步 / 新证书没 reload（§47.7） | `s_client \| openssl x509 -noout -dates`；`date -u` | 先 reload；修 NTP；补 ACME | 到期前 30/14/7/1 天四道告警；续期后自动 reload；**从外部**拨测 |
| `not yet valid` / 证书「来自未来」 | 客户端时钟慢（VM 暂停、容器无 NTP） | `date -u`；`timedatectl`；对比证书日期 | 校时；容器挂宿主时钟 | 所有节点强制 NTP；监控时钟偏移（→ `base/55`） |
| ACME 续期失败，日志只有 `acme:error:unauthorized` | HTTP-01 的 challenge 路径被重定向/WAF 拦；DNS-01 的 TXT 没生效 | `curl -v http://host/.well-known/acme-challenge/test`；`dig TXT _acme-challenge.host` | 放行 challenge；等 TTL；检查 CAA | 续期走独立路径；优先 DNS-01；CAA 显式列 CA |
| 只能 TLS 1.2，1.3 握手失败 | 中间设备不支持 / 客户端被限制 / 服务端没开 | `s_client -tls1_3` 与 `-tls1_2` 分别试 | 服务端开 1.3；中间设备升级；临时 1.2 兜底 | 监控按协议版本分桶；灰度保留 1.2 |
| 握手偶发慢 1 个 RTT | `key_share` 猜错曲线，触发 HelloRetryRequest | `s_client -msg` 看有没有第二个 ClientHello | 预生成 X25519；服务端支持主流曲线 | 升级客户端库；监控握手 RTT P99 |
| mTLS 上线后大量 `alert 116` | 客户端没配证书，或证书链没发全（§47.10） | 服务端日志看 `certificate required`；`s_client -cert … -cert_chain …` | 加载 cert+chain；`-CAfile` 指对根 | 轮转热加载；上线前双向跑通 |

三点总结：**看到「只有某些客户端报错」，先怀疑链不全或 SNI 缺失；看到「突然全挂」，先看有效期与时钟；看到「握手慢」，先数量 RTT，别猜密码学。**

---

## 47.14 一页速记

```
【一句话】
TLS 只承诺三件事：机密性、完整性、对端身份。
前两件是数学（AEAD + Finished），第三件是信任（证书链）——第三件一断，前两件归零。

【握手的版本账】
1.2 完整握手 2-RTT：ClientHello → ServerHello+Cert → CKE+Finished → Finished
1.3 完整握手 1-RTT：CH+key_share → SH+{EE,Cert,CV,Fin} → {Fin}
加 TCP：1.3 = 2 RTT 到首字节；QUIC 把它压到 1-RTT（base/43）
实测 RTT=40ms：1.2 建连 ≈ 3 RTT，1.3 ≈ 2 RTT

【两把钥匙，三种秘密】
ECDHE：K = a·b·G，长期私钥只签名 → 私钥泄露解不开历史
RSA  ：K 由服务器私钥解出     → 私钥泄露 = 历史全解（已废弃）
HKDF：early → handshake → master，每级绑定 transcript hash
      c/s 各一把；early 只给 0-RTT，可重放，只配幂等请求

【记录层 5 字节】
type(1) || legacy_version(2)=0x0303 || length(2) || 密文+16B tag+1B 内层类型
nonce = iv XOR (0…0||seq)，seq 不上线；AAD = 记录头
实测 22 B/记录（1 B → 2200%，16 B → 137%，1024 B → 2.1%，40 KB → 0.17%）

【1.3 把证书藏起来了】
同一服务端、同一张证书，只改版本：
  1.3：原始字节里 'demo.local' 出现 0 次，记录序列 16 / 14 / 17 17 17
  1.2：出现 3 次，Certificate 记录是明文 0x16，直接可读
握手字节 2396 B（完整）→ 241 B（恢复），省下的正是 1835 B 证书

【证书七步】
① 到信任锚 ② 每环签名 ③ 有效期 ④ SAN 匹配 ⑤ EKU=serverAuth
⑥ CA:TRUE/pathlen ⑦ 吊销（不加 -crl_check 就永远 OK）
错误码：9 未生效 ｜ 10 过期 ｜ 18 自签 ｜ 20 缺签发者
        23 已吊销 ｜ 25 pathlen 超限 ｜ 26 用途不符 ｜ 62 域名不符

【SNI】
ClientHello 明文扩展 id=0；服务器靠它选证书；ECH 未普及
一个 IP 万域名 = L4 ssl_preread 选后端 + 每租户证书 + ALPN 再分层
通配符只吃一个 label：a.demo.local 命中 *.demo.local，a.b.demo.local 不命中

【为什么还是会被抓包】
① 跳过校验 → 看 ssl_verify_result 是否为 0（200 与 20 可同时成立）
② 企业 MITM → 看 issuer 是不是自家 CA（verify=0 也能是假的）
③ 错签/泄露 → CT + CAA + pinning
④ 流量分析 → SNI/包长/时序，加密只加外壳
⑤ 端点被控 → TLS 无解

【三个数字】
首连证书 ≈ 2 KB（leaf 943 B + int 892 B = 1835 B DER）
完整握手 ≈ 1 ms CPU（1006 次/秒）→ 1 万新连接/秒 ≈ 10 核
AES-256-GCM 3.19 GB/s → 10 Gbps 用 39% 单核
```

---

## 47.15 自检 3 题

**Q1.** 一台服务器提供 `a.example.com` 与 `b.example.com`，共用一张 `*.example.com` 证书。攻击者录下所有流量，一年后拿到这张证书私钥。他能解开这一年的流量吗？若当年允许静态 RSA 密钥交换，答案会变吗？

<details><summary>答案</summary>

**默认不能。** 现代 TLS（1.2 的 ECDHE 套件、1.3 全部套件）用临时 ECDHE：`K = a·b·G`，`a`、`b` 在握手结束就丢弃。证书私钥只给 transcript 签名，**不参与 `K`**，所以拿到私钥只能伪造**未来**的握手。

**若当年允许静态 RSA，答案变成「能」**：premaster 是客户端用服务器公钥加密的，私钥一到手就解出 premaster，走同样 KDF 得到会话密钥。同一张证书、同一个私钥，安不安全取决于**握手用了哪种密钥交换**。

两个边界：① 会话恢复 PSK 会削弱这一点——若攻击者还拿到 ticket 加密密钥，他能解开 early data（只用 `early_secret`），但解不开同一连接后续混入 ECDHE 的 1-RTT 数据；② 泛域名证书放大「一张泄露全泄」，两个站同时可被冒充，这也是多租户倾向独立证书的原因。

</details>

**Q2.** 证书由 Cert-Manager 自动续期。某天 03:00 全站 502，`curl` 报 `certificate has expired`，但 `kubectl get certificate` 显示 `READY=True`、`RENEWED` 是昨天。按顺序给出至少四条可能原因与验证命令。

<details><summary>答案</summary>

`READY=True` 只证明「Secret 里的证书对象是新的、签名没问题」，**不证明进程在用新证书**，也不证明客户端认为它有效。

| # | 可能原因 | 验证 | 判据 |
| --- | --- | --- | --- |
| ① | Secret 更新了，进程没 reload | 比 `s_client \| openssl x509 -noout -dates` 与 Secret 里证书的日期 | `notAfter` 不同 → 没 reload |
| ② | reload 了，但只 reload 部分副本 | 对每个 Pod IP 分别 `s_client` | 有的新、有的旧 |
| ③ | 服务器时钟快了一天 | `date -u`；`timedatectl` | 节点时间漂移 |
| ④ | 客户端时钟慢，证书已过期但它以为没到 | 在报错机器上 `date -u` | 客户端视角在 `notAfter` 之前 |
| ⑤ | 网关用了另一份证书（SNI 未命中默认证书） | `s_client -servername h` 对比 `-noservername` | 无 SNI 命中默认证书 |
| ⑥ | 换了中间 CA，旧链残留在某个副本 | `s_client -showcerts` 数张数与 issuer | 新旧链混用 |

最易忽略的是 ① 与 ③：**「证书对象是新的」与「进程在用新证书」是两件事**，中间隔着一次 reload；而「过期」有时是**本地时钟**的问题（→ `base/55`）。防复发：续期后自动 reload + 集群外拨测 + 四道到期告警。

</details>

**Q3.** 有人给 `api.example.com` 签了一张「合法」证书（CA 确认了域名所有权），然后做中间人。客户端七步校验全过。TLS 还能挡吗？工程上还有哪三道防线？

<details><summary>答案</summary>

**TLS 本身挡不住。** 证书链只回答「这个公钥属于 `api.example.com`」。如果攻击者真的让 CA 相信他拥有该域名（DNS 劫持、域名过期被抢注、CA 验证出错、内部错签），这张证书在密码学上就**完全合法**：链通、名字对、时间在范围内、EKU 正确。这正是「第三件事靠信任，不靠数学」的含义——CA 是根，根错了上面全错。

三道防线：① **CT + 监控**：所有公共 CA 必须写 CT 日志，订阅自己域名，出现没申请过的证书就告警（事后，但通常早于业务损失）；② **CAA**：`CAA 0 issue "letsencrypt.org"`，只允许指定 CA 签发；③ **证书固定**：只接受指定 SPKI 或指定 CA，企业 MITM、错签、CA 被入侵全部失效，代价是轮换要发版。

这三道都不是 TLS 协议的一部分，而是围绕「信任锚」的治理机制。**TLS 补了机密性/完整性/身份，但「身份由谁担保」永远是协议之外的事**（→ `base/48`）。

</details>

---

## 47.16 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| TLS 上面那层：HTTP/1.1 → 2 → 3，队头阻塞与帧 | → `base/46` |
| TCP 三次握手、状态机与 TIME_WAIT（TLS 之前那个 RTT） | → `base/41` |
| UDP 与 QUIC：把 TLS 1.3 搬进 QUIC，1-RTT/0-RTT 与连接迁移 | → `base/43` |
| DNS 安全：DoT/DoH、DNSSEC，ECH 的公钥为何必须从 DNS 拿 | → `base/45` |
| 应用层认证总表：TLS 只认证服务器，用户身份要靠 OAuth2/OIDC/JWT/mTLS | → `base/48` |
| 实例：VLESS + Reality + Vision——证书借用、record 层复用、长度指纹 | → `base/49` |
| 实例：抓一次 TLS 1.3 + HTTP/2，逐帧解剖 | → `base/50` |
| 时间与顺序：`not yet valid`、证书过期与 NTP 的根 | → `base/55` |
| 收包全路径：TLS 记录在 socket 缓冲区里长什么样 | → `base/32` |
| 零拷贝：TLS record 必须碰数据，所以无法 `sendfile` | → `base/37` |
| 实时连接：SSE/WebSocket 与心跳、连接时长这些「元数据」 | → [go/39](../go/39-realtime-sse-websocket.md) |
| 防御加固：密钥管理、mTLS、证书轮转在平台侧落地 | → [llm/32](../llm/32-defense-hardening.md) |
| 全书总纲：四条主线与 56 篇清单 | [0-summary](0-summary.md) |
