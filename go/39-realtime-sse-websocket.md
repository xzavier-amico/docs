# 39. 实时通信：SSE / WebSocket / gRPC streaming 怎么选

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 工程化与服务端 ｜ **编号**：39 / 54
> **本章定位**：这是「服务端线」的最后一章，也是「Agent 线」的入口。前面 38 章讲清楚了一次 HTTP 请求怎么写完就结束；这一章讲**永远写不完的响应**——token 一个一个流出来、连接要挂几十分钟、还可能双向说话。选错协议，后面所有架构都会跟着歪。
> **读法**：先看 39.2 的脑图和 39.7 的对比矩阵（15 分钟能决定选型），再回来看 39.3/39.4 的两份可跑 demo。39.8 的 `ctx` 穿透与优雅关停是上线前必读。

---

## 39.1 引子：凌晨两点，聊天框里那个转圈的省略号

你的 Agent 服务上线第三天，四个工单同时飘进来：

1. **「本地好好的，上线后变成一次性吐出来」** —— 你在本机 `go run` 时字符一个个往外冒，部署到 K8s 后面（nginx-ingress）后，用户要等 20 秒，然后整段话「啪」地出现。你以为是前端问题，其实是 **`proxy_buffering` 把 `Flush` 吞了**。
2. **「手机锁屏 30 秒回来，只剩半句话」** —— 连接被中间设备掐断，浏览器 `EventSource` 会自动重连，但你的服务端一重连就**从头再生成一遍**，用户看到内容闪回去重播。你没用 `Last-Event-ID`。
3. **「想要一个『停止生成』按钮」** —— SSE 是单行道，前端没法在同一个通道上告诉后端「别说了」。你只能再开一个 `POST /cancel`，还得把两者用 `request_id` 对上。
4. **「长连接从 1k 涨到 50k，P99 从 40ms 飙到 3s」** —— 你打开监控发现：fd 打满、单 Pod RSS 6GB、GC 每分钟一次。**没人给你算过 100k 长连接的账**。

这四个症状，分别对应 SSE 的缓冲、断点续传、单向性，和长连接的容量。**它们不是四个 bug，是一个选型没想清楚。** 这一章就把选型想清楚。

---

## 39.2 直觉模型：五种「说话姿势」

先别看协议，先记人话版本：

```
短轮询     C ──GET /msgs──▶ S   立刻返回 []（空）
           C ──GET /msgs──▶ S   立刻返回 []          ← 浪费 99% 的请求
长轮询     C ──GET /msgs──▶ S   ……挂住 25s……  ──▶ 有消息才返回
SSE        C ──GET /events──▶ S
           C ◀─── data: 你 ─── S    一条 TCP、一个方向、一直开着
WebSocket  C ◀─── 任意帧 ───▶ S     握手升级后双向，自己定应用协议
gRPC 流    C ◀─── proto 消息 ───▶ S  双向 + 多路复用 + 强类型 + 拦截器
```

- **SSE 像广播电台**：只有一个喇叭，但天线一直开着，插上就能听，断了自己重连（浏览器内建）。
- **WebSocket 像打电话**：先拨号（HTTP Upgrade 握手），接通后双方随时说话，但**协议自己定**、心跳自己发、重连自己写。
- **gRPC 流像公司内部专线**：电话能力 + 强类型 + 拦截器，代价是浏览器基本连不上（要 grpc-web 网关）。
- **长轮询** 是保底方案：兼容性最好，延迟最差。

> 💡 一句话记忆：**单向持续推 → SSE；双向且要低延迟 → WS；服务之间 → gRPC 流；老设备/极简客户端 → 长轮询。**

Java 程序员的对应物：`SseEmitter` / `Flux<ServerSentEvent>` ↔ SSE；`@ServerEndpoint`（JSR-356）/ Spring `WebSocketHandler` ↔ WS；gRPC-Java `StreamObserver` ↔ grpc-go 的 `stream.Send()`。

---

## 39.3 底层实现 I：SSE —— 一个「永远不 return 的 Handler」

### 39.3.1 线上格式只有 5 条语法

SSE 的协议规范是 WHATWG 的 HTML Standard 里 `text/event-stream` 一节（不是 RFC 编号文档），但格式简单到可以背下来：

```
field: value        ← 五个合法 field：event / data / id / retry，以及无 field 的注释
data              ← 只有 "data" 没有冒号，等价于空值
: comment           ← 冒号开头 = 注释（心跳就用它）
                    ← 空行 = 一个事件结束，客户端此时 dispatch
```

几条**必须记住**的细节：

- **`data:` 后面那个空格会被吃掉一个**（`data:  x` → `" x"`）。
- 一个事件里可以出现**多条 `data:` 行**，客户端用 `\n` 拼接。所以 token 里如果有换行，必须拆成多条 `data:`，否则协议被破坏——这是「模型输出带代码块」时的经典 bug。
- `id:` 会变成客户端内部的 `lastEventId`，**重连时浏览器自动放到 `Last-Event-ID` 请求头里**。这是 SSE 唯一免费给的可靠性机制。
- 服务端可以设 `retry: 3000` 告诉浏览器重连间隔（毫秒）。
- `event:` 只是给 `addEventListener("token", ...)` 用的名字，不改变连接语义。

```
id: 12
event: token
data: 你好

id: 13
event: token
data: 世界

event: done
data: [DONE]

```

### 39.3.2 为什么必须 `Flush`：bufio 在中间拦着你

`net/http` 的 `ResponseWriter` 底下是一个 `bufio.Writer`（默认 4KB 缓冲）。你 `Fprintf` 出去的字节先躺在那里，**攒够 4KB 或者 handler return 才会真正写进 socket**。所以「流式」的第一条铁律：

```go
// Go 1.20+
rc := http.NewResponseController(w)
rc.Flush()                 // 把 bufio 里的东西顶出去，并 flush 掉 chunked 编码
```

`ResponseController`（Go 1.20 引入）是官方为了替代「类型断言 `w.(http.Flusher)`」这种丑陋写法而加的；顺带你还能拿到 `SetWriteDeadline` / `SetReadDeadline`（Go 1.21 加了 `EnableFullDuplex`，允许 handler 边写响应边读请求体）。

### 39.3.3 Demo 1：完整可跑的 SSE token 流（含断点续传）

下面是一个文件、`go run` 即跑。它同时启动服务端和一个自己写的 SSE 客户端，**故意在读到第 3 个 token 时断开，然后用 `Last-Event-ID: 3` 重连**，你能亲眼看到续传。

```go
// demo1_sse.go —— go run demo1_sse.go
package main

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// ---------------- 服务端 ----------------

type sseHandler struct{ tokens []string }

func (h *sseHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context() // 客户端一断开，这个 ctx 立刻 Done —— 全链路取消的源头
	rc := http.NewResponseController(w)

	// 1) 响应头：Content-Type 决定浏览器按 EventSource 解析
	head := w.Header()
	head.Set("Content-Type", "text/event-stream; charset=utf-8")
	head.Set("Cache-Control", "no-cache, no-transform") // no-transform：禁止中间层 gzip/转码
	head.Set("X-Accel-Buffering", "no")                 // 让 nginx 别缓冲（等价于 proxy_buffering off）
	w.WriteHeader(http.StatusOK)

	// 2) 断点续传：浏览器自动带 Last-Event-ID
	seq, _ := strconv.Atoi(r.Header.Get("Last-Event-ID"))
	if seq < 0 || seq > len(h.tokens) {
		seq = 0
	}
	fmt.Fprintf(w, "event: meta\ndata: {\"resume_from\":%d}\n\n", seq)

	heartbeat := time.NewTicker(10 * time.Second) // 心跳：穿透 LB / NAT 的空闲回收
	defer heartbeat.Stop()
	pace := time.NewTicker(80 * time.Millisecond) // 假装这是 LLM 的 token 节奏
	defer pace.Stop()

	for seq < len(h.tokens) {
		select {
		case <-ctx.Done(): // 用户关页面 / 点"停止生成"：立刻收工，别再烧上游的钱
			return
		case <-heartbeat.C:
			if _, err := io.WriteString(w, ": ping\n\n"); err != nil {
				return
			}
		case <-pace.C:
			// 注意：id 要在 data 之前发，且 token 内不能有裸换行
			fmt.Fprintf(w, "id: %d\nevent: token\ndata: %s\n\n", seq+1, h.tokens[seq])
			seq++
		}
		// 3) 慢客户端保护 + Flush
		_ = rc.SetWriteDeadline(time.Now().Add(10 * time.Second)) // HTTP/2 下返回 ErrNotSupported，忽略即可
		if err := rc.Flush(); err != nil {
			return // 客户端已经走了
		}
	}
	fmt.Fprint(w, "event: done\ndata: [DONE]\n\n")
	_ = rc.Flush()
}

// ---------------- 客户端 ----------------

func readSSE(base, lastEventID string) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, base, nil)
	if err != nil {
		log.Fatal(err)
	}
	req.Header.Set("Accept", "text/event-stream")
	if lastEventID != "" {
		req.Header.Set("Last-Event-ID", lastEventID) // 续传的唯一凭证
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		log.Fatal(err)
	}
	defer resp.Body.Close()

	sc := bufio.NewScanner(resp.Body)
	sc.Buffer(make([]byte, 0, 4096), 1<<20) // Scanner 默认单行上限 64KB，别信默认值（第 40 章细讲）

	var event, id string
	got := 0
	for sc.Scan() {
		line := sc.Text()
		switch {
		case line == "":
			event, id = "", "" // 空行 = 事件结束，真实项目在这里 dispatch
		case strings.HasPrefix(line, ":"):
			fmt.Println("   <- 心跳（注释行）")
		case strings.HasPrefix(line, "event:"):
			event = strings.TrimSpace(line[len("event:"):])
		case strings.HasPrefix(line, "id:"):
			id = strings.TrimSpace(line[len("id:"):])
		case strings.HasPrefix(line, "data:"):
			fmt.Printf("   <- %-9s %s\n", event+"#"+id, strings.TrimPrefix(line, "data: "))
			if event == "token" {
				got++
			}
		}
		if got == 3 {
			return // 模拟手机锁屏：不读 [DONE]，直接走人
		}
	}
	if err := sc.Err(); err != nil {
		log.Println("scan:", err)
	}
}

func main() {
	h := &sseHandler{tokens: strings.Fields("把 复 杂 的 事 情 做 简 单 ， 把 简 单 的 事 情 做 到 极 致 。")}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		log.Fatal(err)
	}
	srv := &http.Server{Handler: h}
	go func() { _ = srv.Serve(ln) }()

	base := "http://" + ln.Addr().String()
	log.Println("① 第一次连接：读到 3 个 token 就断开")
	readSSE(base, "")
	log.Println("② 第二次连接：带 Last-Event-ID=3，从第 4 个 token 继续")
	readSSE(base, "3")

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
	log.Println("服务端优雅退出")
}
```

跑起来你会看到第二段从「的」而不是「把」开始——**这就是断点续传的全部机制**，比你想象的简单。生产上真正的实现是「`id` = 消息在队列里的偏移量或事件序号」，续传时从存储（Redis Stream / Kafka offset / DB）里重放，而不是像这里用一个内存 slice。

### 39.3.4 反代与 HTTP/2：两个必踩的坑

**坑一：nginx 缓冲。** 默认 `proxy_buffering on`，它会攒满 buffer 或等上游关闭才回给客户端，你的 `Flush` 白干。三个位置任一关掉都行：

```nginx
location /api/stream {
    proxy_pass            http://agent_backend;
    proxy_http_version    1.1;
    proxy_set_header      Connection '';        # 让 nginx → upstream 用 keep-alive
    proxy_buffering       off;                  # 关键
    proxy_cache           off;
    gzip                  off;                  # gzip 会在压缩器里攒数据
    proxy_read_timeout    3600s;                # 默认 60s，SSE 会被它掐死
    add_header            X-Accel-Buffering no;
}
```

代码里再补一个 `X-Accel-Buffering: no`（nginx 认这个响应头），双保险。Cloudflare / ALB / API Gateway 各有各的缓冲，**上线前一定要用 `curl -N` 从最外层验证「首字延迟」**，不要只测内网。

**坑二：HTTP/1.1 的 6 连接上限。** 浏览器对同一个 origin 最多 6 个并发 HTTP/1.1 连接。用户开 7 个标签页聊天，第 7 个就卡住。解法是走 HTTP/2：

- 一条 TCP 上多路复用几十上百个 stream，SSE 不再抢连接；
- Go 的 `http2.Server` 默认 `MaxConcurrentStreams = 250`（未显式配置时），**不要**把它当成无限；
- HTTP/2 下禁止 `Connection: keep-alive` 这类 hop-by-hop 头，也**不支持 `SetWriteDeadline`**（`ResponseController` 返回 `ErrNotSupported`）；
- Go 1.24 起可以用 `http.Server{Protocols: ...}` 直接开启明文 HTTP/2（h2c，prior knowledge），内网服务间就不用再套 TLS 了。相关协议配置详见第 38 章。

> ⚠️ HTTP/2 的 SSE 有个隐藏陷阱：**同一条 TCP 上如果有一个 stream 的接收端不读（背压），TCP 窗口会被占满，其它 stream 一起卡住**（head-of-line blocking 在传输层依然存在）。这就是 QUIC/WebTransport 想解决的问题。所以一个 Pod 上的 SSE 用户量特别大时，别指望「HTTP/2 多路复用」能救容量。

---

## 39.4 底层实现 II：WebSocket —— 握手 + 帧 + 掩码

### 39.4.1 握手：一个 20 行的 HTTP 请求

WebSocket 的全部「握手」就是把一个 HTTP/1.1 请求升级掉：

```http
GET /ws HTTP/1.1
Host: api.example.com
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==
Sec-WebSocket-Version: 13
Sec-WebSocket-Protocol: agent.v1

HTTP/1.1 101 Switching Protocols
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=
Sec-WebSocket-Protocol: agent.v1
```

服务端的 `Sec-WebSocket-Accept` 算法是固定的：`base64(SHA1(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"))`。这个魔术 GUID 存在的唯一目的是**防止普通 HTTP 缓存/代理误判成 WebSocket**——它是协议历史包袱，你永远不用自己算，库都做了。

握手阶段你真正要在意的是三件事：

1. **`Origin` 校验**：WebSocket **不受同源策略保护**（JavaScript 一旦连上就能读所有消息）。浏览器会带 `Origin`，你必须校验白名单，否则就是 CSRF。`gorilla` 的 `Upgrader.CheckOrigin`、`coder/websocket` 的 `AcceptOptions.OriginPatterns` 都默认拒绝跨域。
2. **鉴权**：浏览器 `WebSocket` API **不能自定义请求头**，所以 `Authorization: Bearer` 用不了。三个现实方案：① 用 `Sec-WebSocket-Protocol` 塞 token（略脏但可用）；② 先 `POST /ticket` 换一次性 ticket，连 `wss://.../?ticket=xxx`；③ 依赖 Cookie（要防 CSRF）。
3. **子协议协商**：`Sec-WebSocket-Protocol` 是你自己定版本号的地方（`agent.v1`），升级协议时靠它做灰度。

### 39.4.2 帧结构：为什么客户端必须掩码

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-------+-+-------------+-------------------------------+
|F|R|R|R| opcode|M| Payload len |    Extended payload length    |
|I|S|S|S|  (4)  |A|     (7)     |            (16/64)            |
|N|V|V|V|       |S|             |                               |
| |1|2|3|       |K|             |                               |
+-+-+-+-+-------+-+-------------+ - - - - - - - - - - - - - - - +
|     Extended payload length continued, if payload len == 127  |
+ - - - - - - - - - - - - - - - +-------------------------------+
|                               | Masking-key, if MASK set to 1 |
+-------------------------------+-------------------------------+
```

- `opcode`：`0x1` text、`0x2` binary、`0x0` continuation、`0x8` close、`0x9` ping、`0xA` pong。
- `FIN`：一个应用消息可以拆成多帧（fragmentation），Go 的库帮你拼好，`Read` 返回的是一个**完整消息**。
- `MASK`：**客户端 → 服务端的所有帧必须掩码**（RFC 6455 硬性要求，不掩码服务端必须断开，`1002`）。掩码算法就是拿 4 字节 key 循环 XOR payload。它的目的不是加密（key 明文传输），而是**防止恶意页面构造「看起来像合法 HTTP 请求」的字节流去打缓存代理**——2010 年那批缓存投毒事故的产物。
- 因为要做 XOR，Go 的实现质量直接影响吞吐：`coder/websocket` 声称其纯 Go 掩码比 `gorilla` 快 1.75 倍（gorilla 用了 `unsafe`），作者还在做汇编版本。

### 39.4.3 控制帧与 close code

ping/pong 是**控制帧**，可以夹在数据帧中间，payload ≤ 125 字节。两个反直觉点：

- **收到 ping 必须自动回 pong**——库会做，但**前提是你在读连接**。如果只写不读，控制帧永远不被处理，连接会被对端判定死亡。`coder/websocket` 专门提供了 `CloseRead(ctx)`：它在后台帮你把读方向排空（只处理控制帧），你就可以只写不读。
- **ping/pong 的间隔要小于中间设备的空闲超时**。nginx `proxy_read_timeout` 默认 60s、ALB 默认 60s、很多公司防火墙是 300s。业界惯例：**30 秒一次心跳**，服务端 60 秒没收到任何帧就断。

常用 close code（RFC 6455 §7.4.1）：

| code | 名字 | 用在哪 |
| --- | --- | --- |
| 1000 | NormalClosure | 正常收工 |
| 1001 | GoingAway | 服务端关停/滚动发布（**优雅关停专用**） |
| 1002 | ProtocolError | 协议错误（帧格式、缺掩码） |
| 1003 | UnsupportedData | 只收文本你发了二进制 |
| 1008 | PolicyViolation | 鉴权失败/超限 |
| 1009 | MessageTooBig | 超过 `SetReadLimit` |
| 1011 | InternalError | 服务端内部错误 |
| 1013 | TryAgainLater | 过载/限流 |

### 39.4.4 三个库怎么选

| 维度 | `gorilla/websocket` | `coder/websocket` | `gobwas/ws` |
| --- | --- | --- | --- |
| 现状 | 2022 年归档后于 2023 年**重启维护**，API 十年不变 | 原 `nhooyr.io/websocket`，2024 年由 Coder 接手，现模块路径 `github.com/coder/websocket` | 极低层，作者单人维护 |
| API 风格 | `conn.ReadMessage()/WriteMessage()`，无 `ctx` | **`Read(ctx)/Write(ctx)`，`ctx` 一等公民** | `ws.Upgrade` + `wsutil.ReadClientData`，事件驱动 |
| 并发写 | **不允许**，必须自己串行化（一个写 goroutine + 缓冲 channel） | 允许多 goroutine 并发写 | 自己管 |
| 缓冲区控制 | `ReadBufferSize/WriteBufferSize` 可调，**预写消息**（prepared writes）省分配 | 零分配读写，内部池化 | 完全自己管 |
| 关闭握手 | 需手动 `WriteControl(CloseMessage)` | 内置 `Close` 握手 + `CloseRead` 助手 | 无 |
| 压缩 | 只支持 permessage-deflate 的 no-context-takeover | 两种模式都支持 | 无 |
| 适合 | 已有大量存量代码、需要精细缓冲控制 | **新项目默认选它** | 自己写网关/追求极致吞吐 |

> 💡 我的建议：**新项目用 `coder/websocket`**，理由只有一条但足够：`ctx` 贯穿。你不用再为「连接关了但 goroutine 还在跑」写一堆 `done chan struct{}`。标准库没有 WebSocket，`golang.org/x/net/websocket` 已经被官方标记为 deprecated，别用。

### 39.4.5 Demo 2：WS 双向「工具审批」（HITL 的最小原型）

真实场景：Agent 要执行 `delete_files`，必须弹窗让人点「同意」。**审批请求由服务端发起、结果由客户端返回**——这正是 SSE 做不到、WebSocket 一句话就搞定的地方。

```go
// demo2_ws_approval.go —— 先 go mod init demo && go get github.com/coder/websocket
package main

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"net"
	"net/http"
	"time"

	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
)

type Msg struct {
	Type string          `json:"type"` // approval_request / approval / tool_result
	ID   string          `json:"id,omitempty"`
	Tool string          `json:"tool,omitempty"`
	Args json.RawMessage `json:"args,omitempty"`
	OK   bool            `json:"ok,omitempty"`
	Why  string          `json:"why,omitempty"`
}

// runAgent 是"服务端的 Agent"：它想调工具，但必须先拿到人的批准
func runAgent(ctx context.Context, c *websocket.Conn) error {
	in := make(chan Msg, 1)
	readErr := make(chan error, 1)

	// 一个 goroutine 专门读：WebSocket 的所有消息（含控制帧）都从这个口进来
	go func() {
		for {
			var m Msg
			if err := wsjson.Read(ctx, c, &m); err != nil {
				readErr <- err
				return
			}
			select {
			case in <- m:
			case <-ctx.Done():
				return
			}
		}
	}()

	req := Msg{Type: "approval_request", ID: "call_1", Tool: "delete_files",
		Args: json.RawMessage(`{"path":"/tmp/report.csv","force":false}`)}
	if err := wsjson.Write(ctx, c, req); err != nil {
		return err
	}

	select {
	case <-ctx.Done():
		return ctx.Err() // 超时/取消：读 goroutine 也因 ctx 退出，不泄漏
	case err := <-readErr:
		if websocket.CloseStatus(err) == websocket.StatusNormalClosure {
			return nil // 客户端正常告别
		}
		return err
	case m := <-in:
		if m.Type != "approval" || m.ID != req.ID {
			return errors.New("unexpected message: " + m.Type)
		}
		if !m.OK {
			return errors.New("human rejected: " + m.Why)
		}
	}
	return wsjson.Write(ctx, c, Msg{Type: "tool_result", ID: req.ID, OK: true})
}

func main() {
	mux := http.NewServeMux()
	mux.HandleFunc("/ws", func(w http.ResponseWriter, r *http.Request) {
		// 浏览器跨域要配 OriginPatterns；Dial 不带你自己的 Origin，所以这里 nil 也能过
		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			log.Println("accept:", err)
			return
		}
		defer c.CloseNow()
		c.SetReadLimit(1 << 20) // 1MB：防大消息打爆内存

		// 注意：Accept 之后不要再依赖 r.Context()（HTTP/1 连接已被 hijack）
		ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
		defer cancel()

		if err := runAgent(ctx, c); err != nil {
			log.Println("agent:", err)
			_ = c.Close(websocket.StatusInternalError, "agent failed")
			return
		}
		_ = c.Close(websocket.StatusNormalClosure, "done")
	})

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		log.Fatal(err)
	}
	srv := &http.Server{Handler: mux}
	go func() { _ = srv.Serve(ln) }()
	defer srv.Close()

	// ---- 客户端（相当于浏览器）----
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	c, _, err := websocket.Dial(ctx, "ws://"+ln.Addr().String()+"/ws", nil)
	if err != nil {
		log.Fatal(err)
	}
	defer c.CloseNow()

	for {
		var m Msg
		if err := wsjson.Read(ctx, c, &m); err != nil {
			log.Println("client done:", err)
			return
		}
		switch m.Type {
		case "approval_request":
			log.Printf("【弹窗】允许执行 %s %s ?", m.Tool, m.Args)
			time.Sleep(300 * time.Millisecond) // 假装人在看
			if err := wsjson.Write(ctx, c, Msg{Type: "approval", ID: m.ID, OK: true}); err != nil {
				log.Fatal(err)
			}
		case "tool_result":
			log.Printf("【结果】工具执行完成 id=%s", m.ID)
			_ = c.Close(websocket.StatusNormalClosure, "bye")
			return
		}
	}
}
```

两个工程细节值得单独说：

- **`coder/websocket` 的 `Conn.Read` 只允许一个 goroutine 调用**（其它方法并发安全）。所以「一个读 goroutine + 业务侧 channel」是标准写法；`gorilla` 则连写都要你串行化。
- **不要用 `Accept` 之后的 `r.Context()`**。HTTP/1 的连接被 hijack 后，那个 ctx 的取消语义变得很怪（库的文档明确写了「not recommended」）。自己 `context.WithTimeout(context.Background(), ...)` 才是正道。

---

## 39.5 底层实现 III：gRPC 流、长轮询、WebTransport 的定位

gRPC 流在 Go 里就是两个方法签名的事：

```proto
service AgentService {
  rpc Stream (Request)          returns (stream Chunk);   // server-streaming
  rpc Chat   (stream ClientMsg) returns (stream ServerMsg); // bidi
}
```

```go
// grpc-go 服务端实现（方法名由 protoc-gen-go-grpc 生成）
func (s *agentServer) Stream(req *pb.Request, stream pb.AgentService_StreamServer) error {
	ctx := stream.Context() // 与这条 HTTP/2 stream 同生共死
	for _, tok := range s.tokens {
		select {
		case <-ctx.Done(): // 客户端 RST_STREAM / 超时 → 立刻停
			return status.FromContextError(ctx.Err()).Err()
		default:
		}
		if err := stream.Send(&pb.Chunk{Text: tok}); err != nil {
			return err // Send 出错通常意味着对端没了
		}
	}
	return nil
}

func (s *agentServer) Chat(stream pb.AgentService_ChatServer) error {
	for {
		in, err := stream.Recv() // 阻塞式：想并发就自己 go，语义比 StreamObserver 直白得多
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		if err := stream.Send(&pb.ServerMsg{Text: handle(in)}); err != nil {
			return err
		}
	}
}
```

gRPC 的强项是**服务间**：强类型 IDL、拦截器（鉴权/超时/trace）、四类流（unary / server-stream / client-stream / bidi）、内置 keepalive 与背压（HTTP/2 flow control）。弱项是**浏览器**：`grpc-web` 需要网关或 Envoy 转换，且 bidi 不支持（只能 server-stream 近似）。

**长轮询**（long polling）仍然值得讲，因为它是最可靠的保底：客户端 `GET` 挂 25 秒，没消息就返回 204，客户端立刻再发。它的好处是**穿过一切代理和防火墙**，坏处是每条消息至少一个完整 RTT + HTTP 头开销。老系统对接、受限网络、只支持 HTTP 的客户端（比如某些嵌入式设备）还得靠它。

**WebTransport** 是跑在 HTTP/3（QUIC）上的新 API，同时提供「可靠流」和「不可靠数据报」，天然解决 HTTP/2 的传输层 HOL 阻塞，适合语音、实时协作光标、云游戏。但（Go 1.25 时代）它还不是你能用标准库直接用上的东西——`net/http` 没有 HTTP/3，要 `quic-go` 的 `http3` 包（含 WebTransport，仍在演进）。**知道它解决什么问题，但现在别急着上。**

---

## 39.6 Java 对照：三处你会猜错的地方

**① 「handler 是回调，写不出去也没关系」。** Java 里你 `return new SseEmitter()`，容器托管这个 emitter，你从**任何线程**往里 `send()`。Go 里 handler 是一个**阻塞函数**：`WriteHeader` + 循环 `Flush` + `return`，body 就结束了。想「从别处推消息进来」得自己搭 channel（生产者 → channel → handler 循环写）。这其实是好事：数据流向一目了然，背压天然存在（channel 满了生产者就阻塞）。

```java
// Java: 容器帮你管生命周期
@GetMapping(value="/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
public SseEmitter stream() {
    SseEmitter em = new SseEmitter(0L);
    executor.submit(() -> { em.send(SseEmitter.event().id("1").data("hi")); em.complete(); });
    return em;   // ← return 之后连接还活着
}
```

```go
// Go: handler 不返回，连接就活着；返回 = 结束
func stream(w http.ResponseWriter, r *http.Request) {
    w.Header().Set("Content-Type", "text/event-stream")
    rc := http.NewResponseController(w)
    fmt.Fprint(w, "id: 1\ndata: hi\n\n")
    rc.Flush()  // ← 这一步 Java 里由容器做
}
```

**② Servlet 异步 vs goroutine。** Tomcat 要 `maxThreads` + 异步 Servlet 才扛得住长连接；Go 是「每连接一个 goroutine」，2KB 栈起步。这不是「Go 更省」，而是**账要重新算**——39.7 给算式。goroutine 成本与 netpoller 机制详见第 17、19 章。

**③ gRPC-Java 的 `StreamObserver` vs grpc-go 的同步 `Send/Recv`。** Java 侧你实现三个回调（`onNext/onError/onCompleted`），天然异步；Go 侧 `Send` 是同步阻塞的，你如果想让「读」和「写」并行，必须 `go` 一个 goroutine 去 `Recv`（注意：gRPC 允许一个 goroutine `Recv` 一个 `Send` 并发，两个 `Recv` 不行）。**用 Java 直觉写 grpc-go 最常见的 bug 就是只写了 Recv 忘了并发 Send。**

---

## 39.7 对比矩阵与 100k 长连接的账

### 39.7.1 完整对比矩阵

| 维度 | 长轮询 | SSE | WebSocket | gRPC server-stream | gRPC bidi |
| --- | --- | --- | --- | --- | --- |
| 方向 | 单向（拉） | 服务端 → 客户端 | **双向** | 单向 | **双向** |
| 延迟 | 差（每次一个 RTT） | 好（首字即到） | 好 | 好 | 最好 |
| 浏览器直连 | ✅ | ✅ `EventSource` | ✅ | ❌ 需 grpc-web | ❌ |
| 代理/防火墙穿透 | 最好 | 好（HTTP 语义） | 中（需 Upgrade 支持） | 差（HTTP/2 明文/升级） | 差 |
| 鉴权 | 普通 Header | 普通 Header / Cookie | 握手期受限（不能自定义 Header） | Metadata | Metadata |
| 断线恢复 | 客户端自己重试 | **`Last-Event-ID` 内建** | 完全自己实现 | 自己实现 | 自己实现 |
| 背压 | 天然（一问一答） | 弱（只能靠不读=TCP 窗口） | 中（库帮你阻塞写） | HTTP/2 flow control | 同左 |
| 水平扩展 | 容易（无状态） | 中（要粘性/广播总线） | 难（要广播 + 会话路由） | 中 | 难 |
| 移动端友好度 | 好（省电差） | **好（自动重连）** | 中（后台被杀要自己重连） | 差 | 差 |
| 应用协议 | 自己定 | 固定 5 个 field | 自己定（要版本号！） | proto | proto |
| 典型用途 | 老客户端、极简 | **LLM token 流、通知、进度** | 打断/语音/协作编辑 | 大批量下发 | 实时对话/控制 |

### 39.7.2 100k 长连接：内存与 fd 的账本

先破除一个迷思：**「goroutine 很轻」≠「10 万连接只要 200MB」。** 真正的大头是缓冲区和内核。

| 成本项 | 单连接估算 | 100k 连接 |
| --- | --- | --- |
| goroutine 初始栈 | 2 KB | 200 MB |
| `g` 结构体 + 调度/计时器开销 | ~0.5 KB | 50 MB |
| `net/http` conn + 读写 bufio（4KB 起，按需涨） | 4~9 KB | 0.4~0.9 GB |
| TLS 半连接状态（握手后常驻） | 4~16 KB | 0.4~1.6 GB |
| 应用层状态（id、序列化缓冲、订阅表项） | 1~2 KB | 0.1~0.2 GB |
| 内核 `struct socket` + `sk_buff` + 收发队列 | 3~10 KB | 0.3~1 GB |
| **合计** | — | **约 1.5~4 GB** |

外加 **100k 个 fd**（每个连接 1 个，但还要留出 upstream、日志、pipe，按 1.3 倍留 → 130k~160k）。所以容量规划的checklist 是：

```bash
ulimit -n                 # 单进程 fd 上限，容器里常常是 65536 而不是 1048576
sysctl fs.file-max        # 系统级
sysctl net.core.somaxconn # accept 队列（配合 Go 的 ListenConfig）
sysctl net.ipv4.tcp_rmem  # 4 4096 131072 6291456 —— 接收缓冲自动调优
sysctl net.netfilter.nf_conntrack_max   # 走 iptables/kube-proxy 时这才是真上限
```

然后是三条经验：

1. **100k 连接不要塞一个 Pod。** 一个 8C16G 的 Pod 建议 ≤ 20k~30k 长连接，剩下的靠水平扩展；否则 GC（扫 10 万个 goroutine 栈）、调度器、单连接尾延迟会互相拖死。GC 与 Goroutine 扫描详见第 28 章。
2. **按「连接数 × 平均每连接内存」反推 limit。** 上面表里取 15KB/连接（含 TLS），20k 连接 ≈ 300MB 堆 + 内核 200MB，再留 GC 的 100% headroom（Go 默认 `GOGC=100`）→ 容器 `memory: 1Gi` 比较稳。`GOMEMLIMIT` 与 limit 对齐的方法详见第 52 章。
3. **「每连接一个 goroutine」不是问题，问题是「每连接 N 个 goroutine」。** 一个连接上挂心跳 ticker、写队列、读循环、上游转发，就变成 4 个 goroutine → 40 万个。这是长连接服务最容易失控的地方。

---

## 39.8 Agent 场景选型、`ctx` 取消穿透与优雅关停

### 39.8.1 选型决策树

```
用户在浏览器里等 LLM 说话？
├── 只是"看着输出" ─────────────────────▶ SSE（首选，最省事，自带重连）
│    └── 需要"停止生成"按钮 ──▶ 另开 POST /cancel（同 request_id）或升级成 WS
├── 需要中途打断 / 语音 / 多人协作编辑 ──▶ WebSocket
│    └── 原因：双向、<100ms 延迟、能发二进制音频帧
├── 前端要"边生成边调工具 + 显示审批弹窗" ▶ WebSocket（服务端要能主动发请求）
└── 服务之间（编排器 → 工具服务 / 模型网关） ▶ gRPC streaming（server-stream 就够）
       └── 需要双向（如流式对话 + 中途插话）▶ bidi
```

一句话版本（也写在总纲里）：**对话流式用 SSE，需要中途打断/语音/协作编辑用 WS，服务间用 gRPC。**

### 39.8.2 `ctx` 怎么从 socket 一路穿到 LLM

这是全书最重要的一条链。以 SSE 为例：

```
浏览器关页面
   │  TCP FIN
   ▼
net/http 的 conn.readLoop 发现 EOF → cancelCtx(r.Context())
   │
   ▼
handler 里 <-ctx.Done() 触发（39.3 的 demo 里那个 return）
   │
   ▼
传给上游：llm.ChatStream(ctx, ...)  → HTTP 请求被 cancel → 上游 API 停止生成（也就不计费了）
   │
   ▼
工具调用里：exec.CommandContext(ctx, ...) → 孙进程被杀
```

四个**必须做**的动作：

1. handler 里**永远 `select <-ctx.Done()`**，不要写死循环。
2. 传给 LLM / DB / 下游的**每一个**调用都带 `ctx`（不是 `context.Background()`）。`context` 的取消树详见第 24 章。
3. WebSocket 场景：`ctx` 用 `context.WithTimeout(context.Background(), ...)` 自己造（别用 `r.Context()`），并在读循环里把它传给 `Read`；`coder/websocket` 的好处就是 `Read(ctx)` 能被打断。
4. 关停时主动取消（见下）。

### 39.8.3 优雅关停：`BaseContext` + `Shutdown` 的正确姿势

SSE handler 是长驻的，直接 `Shutdown` 会**一直等**到 `terminationGracePeriodSeconds` 到期然后被 SIGKILL——用户看到的是连接被硬切。正确做法是「先取消、再等」：

```go
rootCtx, cancelAll := context.WithCancel(context.Background())

srv := &http.Server{
	Addr:    ":8080",
	Handler: mux,
	// 关键：所有请求的 ctx 都从 rootCtx 派生，cancelAll() 一按，全部 handler 同时收到 Done
	BaseContext:       func(net.Listener) context.Context { return rootCtx },
	ReadHeaderTimeout: 5 * time.Second,
	// 注意：不要设 WriteTimeout！它会把长连接流一起掐断（详见第 38 章）
}

// 收到 SIGTERM：
cancelAll()                              // ① SSE handler 的 select 命中 ctx.Done()，发完收尾事件就 return
ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
defer cancel()
_ = srv.Shutdown(ctx)                    // ② 停止 accept，等在途请求自己结束
```

WebSocket 这一点单独处理：连接已经被 hijack，`Shutdown` 管不到它，要自己维护一张连接表，关停时逐个 `Close(websocket.StatusGoingAway, "server restart")`，让客户端知道「该重连了」而不是「网络坏了」。K8s 侧还要配合 `preStop` 的 `sleep` 与 `terminationGracePeriodSeconds`，详见第 52 章。

### 39.8.4 框架里长什么样

- **langchaingo**：流式是回调 `llms.WithStreamingFunc(func(ctx context.Context, chunk []byte) error)`——你在回调里往 SSE 写并 `Flush`，回调返回 error 就能中断上游。`ctx` 贯穿得还算干净。
- **eino**：把「流」做成一等公民，`schema.StreamReader[T]` 可以在图节点之间传递增量，你只需要在终点把它读出来转成 SSE 事件。想法比 langchaingo 激进得多，详见第 49 章。
- **Kratos / Gin**：中间件里对 `text/event-stream` 要**跳过 response writer 的包装**（很多日志/压缩中间件会包一层 `ResponseWriter`，导致 `http.Flusher` 断言失败——用 `ResponseController` 就没这个问题，它会帮你穿透包装）。

> **排查场景**：中间件包装 `ResponseWriter` 后，SSE 事件攒到请求结束才送达。先检查包装器是否保留刷新能力：对使用 `ResponseController` 的代码，可以通过 `Unwrap` 暴露底层；直接断言 `http.Flusher` 的代码需要包装器提供该接口。反向代理缓冲也应单独排查。

---

## 39.9 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| 本地流式，线上一次性 | 反代 `proxy_buffering on` / gzip | `X-Accel-Buffering: no` + nginx `proxy_buffering off` + `no-transform` | 39.3.4 |
| SSE 连接 60 秒必断 | nginx `proxy_read_timeout` 默认 60s | `proxy_read_timeout 3600s` + 15~30s 心跳 | 39.3.4 |
| 重连后内容重播/重复计费 | 没用 `Last-Event-ID` | 服务端发 `id:`，重连时按 id 从存储续传 | 39.3.3 |
| 模型输出带换行就断了 | `data:` 里塞了裸 `\n` | 每个换行拆成一条 `data:` | 39.3.1 |
| SSE 接口偶发 panic / 无输出 | 中间件包装了 `ResponseWriter` 没实现 `Flush` | 用 `http.NewResponseController(w).Flush()`，中间件实现 `Unwrap()` | 39.8.4 |
| 打开多个标签页后第 7 个卡住 | 浏览器 HTTP/1.1 同域 6 连接上限 | 上 HTTP/2（注意 `MaxConcurrentStreams` 默认 250） | 39.3.4 |
| WS 连上后几十秒被断 | 没人读连接，控制帧不处理；或没发心跳 | `CloseRead(ctx)` 或用读循环 + 30s ping | 39.4.3 |
| `gorilla` 里并发写 panic | 该库不允许并发 `WriteMessage` | 单写 goroutine + channel；或换 `coder/websocket` | 39.4.4 |
| WS 被任意站点连上 | 没校验 `Origin`（WS 不受同源策略保护） | 配 `AcceptOptions.OriginPatterns` / `CheckOrigin` | 39.4.1 |
| 关停时用户看到「连接重置」 | 直接 `Shutdown` 等 SSE 超时被 SIGKILL | `BaseContext` + 先 `cancelAll()` 再 `Shutdown`；WS 逐个 `Close(1001)` | 39.8.3 |
| 50k 连接后 OOM / fd 打满 | 没算缓冲与 TLS 的账，`ulimit -n` 还是 65536 | 按 15KB/连接 + 1.3 倍 fd 规划，单 Pod ≤ 30k | 39.7.2 |
| goroutine 数 = 连接数 × 4 | 每连接起心跳、写队列、转发等多个 goroutine | 复用 timer/写合并，一个连接只留读写两个 | 39.7.2 |

## 39.10 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| SSE 格式 | 五个 field（`event/data/id/retry/注释`），空行派发，多行 data 用 `\n` 拼 |
| 必须 Flush | `http.NewResponseController(w).Flush()`，否则躺 bufio 里等 4KB |
| 断点续传 | 服务端发 `id:`，浏览器重连带 `Last-Event-ID`，服务端按 id 重放 |
| 心跳 | 注释行 `: ping`，15~30s，小于代理空闲超时 |
| 反代 | `X-Accel-Buffering: no` + `proxy_buffering off` + `proxy_read_timeout 3600s` |
| WS 握手 | `Upgrade: websocket` + `Sec-WebSocket-Accept`(SHA1+GUID) + **必须校验 Origin** |
| WS 帧 | 客户端→服务端**必须掩码**；ping/pong/close 是控制帧；**不读连接就没心跳** |
| 库选型 | 新项目 `coder/websocket`（ctx 一等公民）；存量/精细缓冲 → `gorilla` |
| 关闭码 | 正常 1000，服务端重启 1001，超限 1009，内部错误 1011 |
| 选型 | 单向流 SSE；双向/打断/语音 WS；服务间 gRPC stream；保底长轮询 |
| 取消 | `r.Context()` 断开即取消 → 一路传给 LLM → 上游停止计费 |
| 关停 | `BaseContext` + `cancelAll()` 然后 `Shutdown`；WS 单独遍历发 1001 |
| 容量 | 单连接约 4~16KB（含 TLS）+ fd 1 个；100k ≈ 1.5~4GB；单 Pod ≤ 30k |

## 39.11 自检题

1. **你在 handler 里 `w.Write([]byte("data: hi\n\n"))` 之后 `return`，客户端能实时收到吗？为什么加了 `Flush()` 才有流式效果？如果中间还有一层 nginx 呢？**
   > 答案要点：`return` 才把 bufio 刷出去，但那时连接就结束了，等于「一次性」；`Flush()` 把 bufio + chunked 编码顶到 socket；nginx 还有自己的 `proxy_buffering`，需要 `X-Accel-Buffering: no` 或改配置，否则依然会被攒包。

2. **`Last-Event-ID` 是谁发的？如果客户端是原生 `EventSource`、中间重启了后端，怎么做到「不重不漏」？**
   > 答案要点：浏览器 `EventSource` 自动在重连请求里带上最后一个事件的 `id`；服务端必须把事件持久化（Redis Stream / Kafka offset / DB 序号）并按 id 之后重放，`id` 必须是单调且可定位的，不能只是内存下标。

3. **一个 Agent 前端要「生成中随时点停止」，SSE 能做到吗？三条路怎么选？**
   > 答案要点：① 另发 `POST /cancel {request_id}`，服务端用它 cancel 对应 ctx（最简单，推荐）；② 换 WebSocket，双向天然支持；③ SSE + `AbortController` 直接断连接——服务端 `r.Context()` 会 Done，成本最低但没法区分「误断」与「主动停」。

## 39.12 延伸阅读

- `net/http` 源码：`src/net/http/server.go` 的 `response.wroteHeader`/`chunkWriter.writeHeader`、`src/net/http/responsecontroller.go`（Go 1.20+）
- WHATWG HTML Standard §Server-sent events（`text/event-stream` 的权威定义）
- RFC 6455（WebSocket 协议）、RFC 7692（permessage-deflate）
- `github.com/coder/websocket` 的 `internal/examples/echo`、`internal/examples/chat`（可以照着抄的完整示例）
- `github.com/gorilla/websocket` 的 `examples/chat`（经典 hub 模型）
- grpc-go：`examples/route_guide`、`examples/features/` 下的 streaming 示例
- 上一章：[38. net/http 深潜：Server、中间件与超时](38-net-http.md)
- 下一章：[40. LLM 客户端：抽象、流式与韧性](40-llm-client-streaming.md)

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：第 38 章（net/http 与超时四件套、`ResponseController`）、第 24 章（context 取消树）、第 17/19 章（goroutine 成本与 netpoller）
- 下一章：第 40 章（LLM 客户端的 SSE 解析与韧性）
- 相关：第 32 章（100k 连接的高并发实战）、第 52 章（K8s 优雅关停与容量规划）
