# 7. 流式传输：SSE / WebSocket / gRPC / 长轮询

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · Agent-Client / Agent-Server 分离架构 ｜ **编号**：7 / 52
> **本章定位**：第 6 章把 Run 拆成了 Event 序列，这一章回答「这些 Event 怎么送到用户屏幕上」。传输层是 Agent 平台里**唯一一处「用户能直接感知到你的架构」**的地方——你选错协议，后面用十倍工程也补不回那 4 秒白屏。
> **读法**：先看 §7.2 的四选一矩阵定协议，再看 §7.4 的四个工程点（续传、合批、反代、连接账）。协议本身是死的，能把它在 10 万连接下跑稳的那四件事才是本章的价值。

---

## 7.1 引子：三个把流式打回原形的现场

**现场 A：早高峰地铁。**

用户问「我的退款到哪了」，Aurora 的模型 900ms 就吐出了第一个 token。但用户盯着屏幕上那个转圈的「思考中…」看了 **4.2 秒**才看到第一个字。你抓包发现：token 早就到了 Nginx，是 `proxy_buffering on`（默认值）把它攒在 4KB/8KB 的缓冲里，等缓冲区满了或者响应结束才一起吐出去。**你花了 900ms 做的 TTFT 优化，被一行默认配置吃掉了 3.3 秒。**

**现场 B：周三凌晨的滚动更新。**

一个正在输出第 600 字的 Run 所在的 Pod 收到 `SIGTERM`。进程退出，TCP 连接 `RST`。用户屏幕上停在「退款会在 3-5 个」，然后前端弹「网络异常，请重试」。用户点了重试——**又从头开始生成了 800 字**，并且这一次因为上下文里多了一轮失败记录，答案还不太一样。

**现场 C：用户点了「停止生成」。**

按钮变灰了，前端不再渲染。但后端那个 Run 还在跑：模型继续吐 token（因为 HTTP 响应写不出去不代表上游停了），工具调用继续执行，最后账单上这一条还是 **25,000 输入 + 1,200 输出**。用户在客服工单里写：「我明明点了取消，为什么还扣我钱？」

> 🎯 这三个现场分别对应本章的四件事：**反代缓冲、断点续传、取消传播、连接账**。
> 记住一句话：**流式不是一个「开关」，是一条从模型 token 到用户像素、中间有 5 跳、每跳都可能攒包或断掉的链路。** 你要设计的是这条链路的可靠性，不是「加个 SSE」。

三条现场的关键数字（Aurora 真实量级）：

| 现场 | 指标 | 事故前 | 事故后（修完） |
| --- | --- | --- | --- |
| A 反代缓冲 | 首字可见延迟 P50 | 4.2s | 1.1s |
| A 反代缓冲 | 用户 3s 内主动关闭率 | 18% | 4% |
| B 滚动更新 | 更新期间流中断率 | 2.8% | 0.03% |
| B 滚动更新 | 重连后重复生成 token 数 | 平均 620 | 0（续传） |
| C 取消 | 取消后继续消耗的 token | 平均 1,850/次 | 90（一个 batch 的尾巴） |

---

## 7.2 断崖：协议选型不是审美问题，是账本问题

### 7.2.1 四选一：先看矩阵，别看教程

| 维度 | **SSE**（`text/event-stream`） | **WebSocket** | **gRPC server-stream** | **长轮询** |
| --- | --- | --- | --- | --- |
| 方向 | 服务端→客户端单向（客户端用普通 POST 上行） | 全双工 | 服务端→客户端流（双向流需 bidi） | 伪推送 |
| 浏览器原生 | ✅ `EventSource` 内建重连 + `Last-Event-ID` | ✅ 但需自己写重连/心跳 | ❌ 需 grpc-web + 代理支持 trailers | ✅ fetch 即可 |
| 二进制 | ❌（只能 base64，多 33% 体积） | ✅ 帧原生支持 | ✅ protobuf | ❌ |
| 断点续传 | ✅ **协议级**：重连自动带 `Last-Event-ID` | ❌ 全靠自己设计 resume 协议 | ❌ 流断了就是断了 | ⚠️ 靠 offset 参数自己实现 |
| 穿反代/CDN | ⚠️ 要关 buffering，但 HTTP/1.1 就能跑 | ⚠️ 要 `Upgrade` 头透传，部分 WAF/企业代理直接掐 | ❌ h2 端到端，中间任何一跳降级就死 | ✅ 最兼容 |
| 负载均衡 | ✅ 普通 L7 即可（每请求独立） | ⚠️ 有状态连接，需 sticky 或连接级路由 | ⚠️ 同 WS | ✅ |
| 每连接成本 | 低（一个 HTTP 请求） | 中（握手 + 帧解析 + 心跳） | 中高（HTTP/2 流） | 高（反复建连） |
| 中断恢复语义 | **天然幂等**（按 seq 续） | 需要自己造 | 无 | 需要自己造 |
| 适用场景 | **单向 token 流（90% 的 Agent 场景）** | 打断/审批/语音/协同编辑 | **服务间**（Gateway↔Worker） | 老网关兜底、极简客户端 |

**Aurora 的结论（也是我给绝大多数团队的建议）**：

```
对外（浏览器/IDE/App）  →  SSE 为主 + 一条独立的控制通道（普通 POST）
对内（Gateway↔Worker）  →  gRPC bidi stream（省 CPU、能带 deadline）
WebSocket               → 只在「真双向」场景开：语音实时对话、多人协同看板、IDE 的 LSP 式交互
长轮询                  → 只作为「企业代理封了 SSE」的降级通道，永远不是主路径
```

为什么「SSE + POST 控制通道」比「一把梭 WebSocket」好？因为**单向流和双向控制是两种完全不同的失败模式**，混在一条连接上会互相污染：

- SSE 断了 → 浏览器自动重连、自动带 `Last-Event-ID`、自动续传，你什么都不用做；
- 控制指令（取消 / 审批 / 打断）走 POST → 天然有 HTTP 状态码、天然可重试、天然可审计、天然能被网关限流。

而 WebSocket 一把梭的话：连接断了你不知道「取消指令到底发出去没有」；审批回传要靠你自己造一套 request/response 关联 ID；WAF 掐了连接你连错误码都拿不到。

> 💡 **判断口诀**：如果一条消息**需要 ACK、需要幂等、需要审计**，它就应该是 HTTP 请求，不该塞进 WS 帧里。

### 7.2.2 不做会怎样：四笔账

| # | 不做的事 | 现象 | 量化后果（Aurora 峰值） |
| --- | --- | --- | --- |
| 1 | 不关反代缓冲 | 首字延迟 = 缓冲填满时间 | TTFT 从 0.9s 变 3~5s，3s 内关闭率 18% |
| 2 | 不做断点续传 | 断线即从头生成 | 每次重连浪费 620 token × 3% 连接/分钟 ≈ 每分钟 18 万 token |
| 3 | 不做事件合批 | 每 token 一个 SSE 帧 | 600k 帧/秒，出口带宽 72MB/s，网关 CPU 打满 |
| 4 | 不预留连接账 | fd/内存耗尽 | 单节点 1024 fd 上限 → 第 1025 个用户 502 |

第 3 条的算式值得单独展开，因为它是「看起来能跑，一上量就死」的典型：

```
Aurora 峰值：15,000 个并发 Run（500 Run/s × 30s）
每 Run 输出：800 汉字 ≈ 1,200 token
token 速率：1,200 / 30s = 40 token/s（每 Run）

方案 A：每 token 一帧
  帧大小 ≈ 120B（id: + event: + data: + 空行）
  帧速率 = 15,000 × 40 = 600,000 帧/秒
  出口带宽 = 600,000 × 120B = 72 MB/s ≈ 576 Mbps（还要 ×反代层数）

方案 B：4 token 合一帧（或 60ms 定时 flush）
  帧速率 = 15,000 × 10 = 150,000 帧/秒
  出口带宽 = 150,000 × 180B = 27 MB/s ≈ 216 Mbps
  单节点（5,000 流）写次数 = 5,000 × 10 = 50,000 次/秒  ← 单机轻松扛
```

**帧率降 4 倍，带宽降 2.7 倍，前端渲染次数降 4 倍（重排更少）。** 而用户根本分辨不出「4 个字一起出现」和「一个字一个字蹦」——60ms 已经是人眼的时间分辨率下限附近了。

### 7.2.3 连接账：10 万长连接到底吃多少资源

这是最容易被低估的一笔账。很多人以为「Go 的 goroutine 很轻，10 万连接没问题」——对，但**连接不只吃 goroutine**。

| 资源 | 每连接开销 | 100,000 连接合计 | 备注 |
| --- | --- | --- | --- |
| goroutine 栈 | 8KB 起，涨到 16~32KB | 0.8~3.2 GB | 用大帧会涨栈，别在 handler 里 `make([]byte, 1<<20)` |
| `bufio.Reader/Writer` | 4KB + 4KB | 0.8 GB | 可以调小到 1KB，SSE 帧很小 |
| TLS 记录缓冲 | ~16KB | 1.6 GB | `tls.Conn` 内部；h2 会更多 |
| 应用层事件缓冲 | 64 帧 × 180B ≈ 11.5KB | 1.15 GB | 背压队列，可调 |
| fd | 1 | 100,000 | 必须 `ulimit -n` 调高 |
| **合计** | **~40KB** | **~4.3 GB** | 单节点 5,000 连接 → 200MB，很宽裕 |

所以 10 万连接的真实账不是「内存不够」，而是**三件事**：

1. **fd 上限**：Linux 默认 `ulimit -n 1024`。第 1025 个连接直接 `accept: too many open files`。
2. **`accept` 队列**：`somaxconn` 默认 4096（老内核 128），突发重连时 SYN 被丢，客户端看到的是「连接超时」而不是「服务繁忙」。
3. **端口耗尽**：如果你的网关是**主动连上游**（SSE Gateway → Worker 的 gRPC），每个客户端连接对应一条上游连接，那还要吃一倍 fd 和本地端口（`ip_local_port_range` 默认约 28,000 个）。

> 🩸 **一个真实踩坑**：网关侧 10 万连接没问题，但网关到 Worker 用短连接 gRPC，`TIME_WAIT` 堆积到 6 万，新连接建不起来。原因是**服务端主动关闭**。解法：连接池 + `tcp_tw_reuse` + 让客户端先关。这类问题在「同步 HTTP」时代不常见，因为请求很短；**长连接把这个坑放大了 100 倍**。

---

## 7.3 结构图：一条 token 的五跳

```
┌──────────┐   ①SSE(HTTP/1.1或h2)   ┌──────────┐   ②透传    ┌──────────────┐
│ Browser  │◀───────────────────────│   CDN    │◀──────────│  反代 / ALB   │
│ / IDE    │   Last-Event-ID 重连    │ (可选)   │           │ buffering=off │
└────┬─────┘                        └──────────┘           └──────┬───────┘
     │ ③ POST /runs/{id}/cancel                                   │
     │    POST /runs/{id}/approvals/{aid}                         │ ④ 路由到
     ▼                                                            ▼   持有连接的
┌─────────────────────────────────────────────────────────────────────────┐
│  SSE Gateway（无状态 × N，每节点 5,000 连接）                             │
│  ┌────────────┐  ┌──────────────┐  ┌───────────────┐  ┌──────────────┐  │
│  │ 连接注册表  │  │ 合批器 Batcher│  │ 慢消费者探测   │  │ 心跳 15s     │  │
│  │ conn→run   │  │ 60ms/4token  │  │ 队列>256 → 断 │  │ `: ping`     │  │
│  └────────────┘  └──────────────┘  └───────────────┘  └──────────────┘  │
└───────────┬──────────────────────────────────▲─────────────────────────┘
            │ ⑤ 订阅 run 事件（Redis Streams     │ 回放：XRange(from, +)
            │    XREAD BLOCK，按 run_id 分片）    │
            ▼                                    │
┌──────────────────────────┐        ┌────────────┴─────────────┐
│  Event Store（热 30min）  │        │  Run Worker（执行内核）    │
│  Redis Streams / PG       │◀───────│  产生 token/tool/status  │
│  key: ev:{run_id}         │  写    │  事件 seq 单调递增         │
└──────────────────────────┘        └──────────────────────────┘

关键：⑤ 和 回放 必须「先订阅、再回放、后去重」，否则中间有 gap。
```

这个图里有一个**唯一正确的顺序**，很多人第一次写都会写错：

```
❌ 错误：回放历史 → 然后订阅
   ↑ 回放结束到订阅生效之间（几毫秒到几十毫秒）产生的事件永久丢失

✅ 正确：订阅（带缓冲）→ 回放历史到 last_seq → 消费缓冲中 seq > last_seq 的事件
   ↑ 缓冲里可能有重复（回放已覆盖），按 seq 去重即可；宁可重不能丢
```

这就是 Event Sourcing 的正确姿势，详见第 6 章。传输层只是它的读者。

---

## 7.4 关键设计

### 7.4.1 事件契约：seq 是唯一的续传坐标

```jsonc
// SSE wire format（一帧）
id: 1042
event: token
data: {"seq":1042,"run_id":"r_8f2a","delta":"退款会在","t":1730000000123}

// retry: 告诉浏览器断线后多久重连（毫秒），一次性下发
retry: 1500
```

| event 类型 | 载荷关键字段 | 可合并 | 可丢弃 | 说明 |
| --- | --- | --- | --- | --- |
| `token` | `seq, delta` | ✅ | ✅（合并后） | 增量文本，唯一的「可以牺牲」的类型 |
| `status` | `seq, phase`（检索中/思考中/调用工具中） | ✅ 只留最新 | ✅ | 给用户看的进度提示 |
| `tool_start` | `seq, name, args_hash` | ❌ | ❌ | 工具开始，UI 要画卡片 |
| `tool_result` | `seq, name, status, truncated` | ❌ | ❌ | 工具结束，含错误 |
| `approval_required` | `seq, approval_id, action, expires_at` | ❌ | ❌ | 挂起等人工，必须可靠送达 |
| `correction` | `seq, target_seq, new_text` | ❌ | ❌ | 撤回已发内容（第 37 章） |
| `error` | `seq, code, retryable` | ❌ | ❌ | 终态错误 |
| `done` | `seq, usage, finish_reason` | ❌ | ❌ | 计费结算的触发点 |

三条铁律：

1. **`seq` 是 Run 内单调递增整数，不是全局的**。全局 ID（如 Snowflake）也能用，但续传时无法做「差多少」的判断。
2. **`seq` 必须连续**。中间缺号 = 有事件丢了 = 客户端应该重新拉全量。这是唯一能让客户端自己发现「我漏了」的机制。
3. **`done` / `error` 是终态**。收到之后连接必须关，否则前端永远在「生成中」。这条听起来废话，但我见过三个团队在这里漏了。

### 7.4.2 Go SSE handler（可直接用的骨架）

```go
// GET /v1/runs/{runID}/events
// 续传：浏览器自动带 Last-Event-ID；自研客户端可用 ?since=
func (s *Streamer) ServeSSE(w http.ResponseWriter, r *http.Request) {
	runID := chi.URLParam(r, "runID")
	orgID := auth.OrgFromCtx(r.Context())

	flusher, ok := w.(http.Flusher)
	if !ok { // 极少数反代/老服务器不支持
		writeJSONErr(w, http.StatusInternalServerError, "stream_unsupported")
		return
	}
	from := parseSince(r) // Last-Event-ID 优先，其次 since，默认 0

	h := w.Header()
	h.Set("Content-Type", "text/event-stream; charset=utf-8")
	h.Set("Cache-Control", "no-cache, no-transform") // no-transform 防运营商/反代压缩改写
	h.Set("Connection", "keep-alive")
	h.Set("X-Accel-Buffering", "no")                 // ★ Nginx 不缓冲
	h.Set("Content-Encoding", "identity")            // 别让中间层 gzip，会攒包
	w.WriteHeader(http.StatusOK)
	flusher.Flush() // ★ 先把手刷出去，客户端才知道"流建立了"

	ctx := r.Context()
	w.Write([]byte("retry: 1500\n\n"))
	flusher.Flush()

	// ① 先订阅（带 2s 缓冲），再回放，保证 gap-free
	sub, err := s.bus.Subscribe(ctx, runID, 512)
	if err != nil { s.writeErrFrame(w, flusher, "subscribe_failed"); return }
	defer sub.Close()

	last := from
	n, err := s.store.Replay(ctx, runID, from, func(e Event) error {
		if e.Seq <= last { return nil } // 去重（订阅缓冲与回放重叠）
		last = e.Seq
		return writeFrame(w, flusher, e)
	})
	_ = n
	if err != nil && !errors.Is(err, ErrRunNotFound) {
		s.writeErrFrame(w, flusher, "replay_failed"); return
	}

	// ② 从回放末端接着消费实时事件
	hb := time.NewTicker(15 * time.Second)   // 心跳：破反代 60s idle 超时
	defer hb.Stop()
	for {
		select {
		case <-ctx.Done():          // 客户端断开 / 服务端关停
			return
		case <-hb.C:
			fmt.Fprint(w, ": ping\n\n") // 注释帧，客户端忽略，但能保活
			flusher.Flush()
		case e, ok := <-sub.C():
			if !ok { return }
			if e.Seq <= last { continue }
			if err := writeFrame(w, flusher, e); err != nil { return }
			last = e.Seq
			if e.Terminal() { return } // done / error 收工
		}
	}
}

func writeFrame(w http.ResponseWriter, f http.Flusher, e Event) error {
	b, _ := json.Marshal(e) // 生产上务必用预分配 buffer 池，避免每帧一次分配
	if _, err := fmt.Fprintf(w, "id: %d\nevent: %s\ndata: %s\n\n", e.Seq, e.Type, b); err != nil {
		return err
	}
	f.Flush() // ★ 不 Flush = 等于没流式
	return nil
}
```

> ⚠️ 三个必须知道的细节：
> 1. `w.WriteHeader` 之后**不能再改状态码**。所以鉴权、Run 不存在这类错误必须在写头之前处理完。
> 2. 这里每个连接一个 goroutine + 一个 `Ticker`。5,000 连接 = 5,000 goroutine，Go 扛得住（见 `../go/` 调度器那章）。但**别每帧起一个 goroutine**，那是灾难。
> 3. `Flush()` 是有 syscall 成本的（写 vDSO 后真的 `write(2)`）。这就是 §7.4.4 要合批的根本原因。

### 7.4.3 Go WebSocket handler（只在真双向时用）

```go
// GET /v1/realtime  (Upgrade)
var up = websocket.Upgrader{
	ReadBufferSize: 4096, WriteBufferSize: 4096,
	CheckOrigin: func(r *http.Request) bool { return originAllowed(r) }, // ★ 别写 return true
}

func (s *Realtime) ServeWS(w http.ResponseWriter, r *http.Request) {
	c, err := up.Upgrade(w, r, nil)
	if err != nil { return }
	defer c.Close()

	c.SetReadLimit(64 << 10) // ★ 防大帧打爆内存
	_ = c.SetReadDeadline(time.Now().Add(60 * time.Second))
	c.SetPongHandler(func(string) error {
		return c.SetReadDeadline(time.Now().Add(60 * time.Second))
	})

	// ★ 单写协程：gorilla/websocket 不允许并发写
	out := make(chan []byte, 64)
	go func() {
		ping := time.NewTicker(20 * time.Second)
		defer ping.Stop()
		for {
			select {
			case b, ok := <-out:
				if !ok { return }
				if err := c.WriteMessage(websocket.TextMessage, b); err != nil { return }
			case <-ping.C:
				if err := c.WriteMessage(websocket.PingMessage, nil); err != nil { return }
			}
		}
	}()

	// 读循环：只收控制指令（打断 / 审批 / 语音上行），一律转成内部事件
	for {
		_, msg, err := c.ReadMessage()
		if err != nil { break }
		var cmd ControlCmd
		if err := json.Unmarshal(msg, &cmd); err != nil { continue } // 脏帧不断连
		switch cmd.Type {
		case "interrupt":
			s.kernel.Interrupt(r.Context(), cmd.RunID, cmd.Reason)
		case "approve":
			s.approvals.Decide(r.Context(), cmd.ApprovalID, cmd.Decision, cmd.Nonce)
		}
	}
	close(out)
}
```

WS 上你**必须自己实现** SSE 免费给你的三样东西：重连、续传、心跳。所以除非业务真的需要「服务端主动推 + 客户端低延迟上行」（语音对话的 20ms 级打断），否则别选它。

### 7.4.4 合批器：把 600k 帧/秒压到 150k

```go
type Batcher struct {
	flushEvery time.Duration // 60ms：人眼分辨不出更细
	maxTokens  int           // 4：一帧最多攒 4 个 token
	maxBytes   int           // 4KB：防大帧
}

func (b *Batcher) Run(in <-chan Token, out chan<- Event) {
	var buf []Token
	var pendingBytes int
	t := time.NewTicker(b.flushEvery)
	defer t.Stop()

	flush := func() {
		if len(buf) == 0 { return }
		out <- Event{
			Type:  "token",
			Delta: joinDeltas(buf),
			Seq:   buf[len(buf)-1].Seq, // ★ 取最后一个 seq，续传从这之后开始
		}
		buf = buf[:0]
		pendingBytes = 0
	}

	for {
		select {
		case tok, ok := <-in:
			if !ok { flush(); close(out); return }
			buf = append(buf, tok)
			pendingBytes += len(tok.Text)
			if len(buf) >= b.maxTokens || pendingBytes >= b.maxBytes {
				flush()
			}
		case <-t.C:
			flush()
		}
	}
}
```

| 合批参数 | 建议值 | 太小的后果 | 太大的后果 |
| --- | --- | --- | --- |
| `flushEvery` | 50~80ms | 帧率暴涨、CPU 打满 | 文字一段段蹦，像卡顿 |
| `maxTokens` | 4~8 | 同上 | 同上，且首字延迟变差 |
| `maxBytes` | 4KB | 大 token 撑爆 | 单帧过大，反代切分 |
| 不可合并帧 | 全部 | — | — | `tool_*`/`approval`/`correction`/`done` 必须立刻发 |
| 背压阈值 | 队列 256 帧 | 内存上涨 | 慢消费者拖垮整节点 |

**背压怎么处理**：SSE 是「服务端一直写、客户端慢慢读」，当 TCP 发送窗口满了，`Write` 会阻塞。这会连锁：写不动 → 订阅队列堆 → 事件的消费者变慢 → 如果共享同一个订阅，**一个慢客户端会拖慢同一分片上的所有 Run**。

三档处理，按优先级：

```
① 队列 > 128 帧：合并更多 token（降帧率），必然有效
② 队列 > 256 帧：标记 slow_consumer，丢弃 token 帧但保留 tool/approval/correction
                  → 下一帧插一条 event: gap，客户端主动断线重连 + Last-Event-ID 补全
③ 队列 > 512 帧：直接 close(conn)。让客户端重连续传，比在服务端维护它便宜得多
```

> 💡 「断开比背压便宜」是这个体系里最重要的一个反直觉结论。**续传已经能保证正确性了，就没必要为了不丢一帧而牺牲整个节点的吞吐。** 这也是 Event Sourcing 给你的底气：任何时刻断开都不丢状态，只是断一次 CDN 边缘而已。

### 7.4.5 反代与 HTTP/2：一行配置能让前面全白做

```nginx
# Nginx：SSE 专用 location
location /v1/runs/ {
    proxy_http_version 1.1;
    proxy_set_header Connection "";      # ★ 清掉 close，否则上游每请求重建连接
    proxy_buffering off;                 # ★ 核心：不缓冲
    proxy_cache off;
    gzip off;                            # ★ 压缩会攒包；要压就压 application/json
    proxy_read_timeout 3600s;            # ★ 长 Run（编码 Agent 90s+）别被 60s 掐
    proxy_send_timeout 3600s;
    chunked_transfer_encoding on;
    proxy_set_header X-Accel-Buffering no;
}
```

```yaml
# ingress-nginx 版本（K8s 里最常见的翻车点）
metadata:
  annotations:
    nginx.ingress.kubernetes.io/proxy-buffering: "off"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-http-version: "1.1"
    nginx.ingress.kubernetes.io/configuration-snippet: |
      proxy_set_header Connection "";
      gzip off;
```

HTTP/2 与流式的三个坑：

1. **h2 没有 chunked，但 `Flush()` 一样能让 DATA 帧立刻发出**——前提是端到端都是 h2。中间任何一跳降级到 h1.1 且开了缓冲，你就回到现场 A。
2. **h2 有流量控制窗口**（默认 64KB per stream / 64KB per connection）。小帧没问题，但一次推 200KB 的大事件（比如工具返回的大 JSON）会卡在窗口上等你 `WINDOW_UPDATE`。Go 的 `http2` 实现会处理，但**你要保证 `Flush` 是在写完整个帧之后**，否则会出现半帧。
3. **浏览器对 h1.1 有 6 连接/域名的限制**。用户开 7 个标签页，第 7 个的 SSE 就排不上队——这是很多人「偶现首字延迟 30s」的真凶。**上 h2 或者把 SSE 走独立子域**（`stream.example.com`），能直接绕过这个限制。

### 7.4.6 10 万连接的落地配置

```bash
# /etc/security/limits.d/agentos.conf（进程级）
agent   soft nofile 65535
agent   hard nofile 65535

# /etc/sysctl.d/99-agentos.conf（内核级）
net.core.somaxconn            = 8192     # accept 队列（突发重连）
net.ipv4.tcp_max_syn_backlog  = 8192
net.ipv4.ip_local_port_range  = 30000 65000  # 上游连接可用端口 ~35k
net.ipv4.tcp_tw_reuse         = 1        # 允许重用 TIME_WAIT（仅出向连接）
net.ipv4.tcp_fin_timeout      = 15
net.ipv4.tcp_keepalive_time   = 60
net.core.netdev_max_backlog   = 16384
fs.file-max                   = 2000000

# 单节点目标：5,000 长连接
# fd 需求 = 5,000(客户端) + 5,000(上游gRPC) + 2,000(文件/socket/日志) ≈ 12,000
# 65535 有 5x 余量，够应对突发重连
```

容量算式（**这是你应该背下来的那一行**）：

```
节点数 N = 峰值连接数 / 单节点连接数 × (1 + 故障余量)
Aurora: N = 100,000 / 5,000 × 1.2 = 24 个 SSE 节点（3 AZ × 8）

单节点带宽 = 5,000 流 × 10 帧/s × 180B = 9 MB/s ≈ 72 Mbps
24 节点合计 = 216 Mbps  ← 出口带宽不是瓶颈，CPU 的 Flush 次数才是

单节点内存 = 5,000 × 40KB = 200MB（连接）+ 800MB（Go runtime + 业务）
```

---

## 7.5 Java / 分布式经验对照

| 你熟悉的 | AgentOS 对应 | 会咬人的差异 |
| --- | --- | --- |
| `SseEmitter`（Spring MVC） | SSE handler | `SseEmitter` 默认超时 30s（`spring.mvc.async.request-timeout`），长 Run 直接 500；还要配 `AsyncTaskExecutor` 的线程池，**Tomcat 200 线程撑不住 5,000 连接**——必须 WebFlux/Netty |
| `DeferredResult` | 异步响应 | 一次性，不能多次 `setResult`；流式要用 `ResponseBodyEmitter` |
| WebFlux `Flux<ServerSentEvent>` | 天然流式 | 背压是真的（Reactive Streams），但**默认 `onBackpressureBuffer` 无界 → OOM**；必须 `onBackpressureDrop` 或限长 |
| Tomcat `maxConnections` | 连接数上限 | 默认 8192，超了排队（`acceptCount`）；**不是拒绝，是静默排队**，比拒绝更难查 |
| Netty `Channel` | 连接 | 一个 Channel 一个 pipeline，写操作要在 EventLoop 线程；跨线程写要 `channel.writeAndFlush` |
| Nginx `proxy_buffering` | 同上 | Java 团队常年在 Nginx 后面，这个坑一样，只是以前响应小、不敏感 |
| gRPC-Java `ServerCallStreamObserver` | 服务间流 | `isReady()` + `setOnReadyHandler` 才是真背压；不检查就 `onNext` 会在 buffer 里堆 |
| `@Async` + `ThreadLocal` | 上下文传递 | Go 的 `context.Context` 是**显式参数**；Java 的 `RequestContextHolder` 在异步线程里是空的，MDC 也丢。trace id 要手动透传 |

**一句话**：Java 侧做流式，**先换掉 Servlet 栈**。用 `SseEmitter` 在 Tomcat 上做 5,000 长连接，你会先撞线程池、再撞内存、最后撞 GC——这不是调参能解决的，是模型错配。

---

## 7.6 开源坐标

| 项目 | 它做了什么 | 它不做什么（你得自己补） |
| --- | --- | --- |
| **Vercel AI SDK** | 定义了 data stream 协议（`x-vercel-ai-data-stream` 头 + 自定义帧格式），前端 `useChat` 直接消费；把 `text-delta`/`tool-call`/`finish` 抽象得很干净 | 没有 `Last-Event-ID` 续传语义（它假设流是一次性的）；没有服务端连接管理；没有背压 |
| **AG-UI 协议** | Agent↔前端的事件协议，事件类型比较全（`TEXT_MESSAGE_CONTENT`、`TOOL_CALL_START/ARGS/END`、`STATE_DELTA`、`RUN_STARTED/FINISHED`、`STEP_*`），支持共享状态增量 | 同样是**协议规范**，不提供网关实现；续传要靠 `sequence` 字段自己实现 |
| **OpenAI / Anthropic 流式 API** | SSE + `data: [DONE]` 终止符；事件分得很细（Anthropic 的 `content_block_delta` / `message_delta`） | 那是「模型→你」这一段。**「你→用户」这一段它不管**，而这一段才是 10 万连接的问题 |
| **MCP** | 传输层有 stdio 和 Streamable HTTP，后者支持 SSE | 面向「Agent↔工具」，不是「Agent↔人」，没有续传/多端一致性 |
| **gRPC / Connect** | 服务间流的标准答案；deadline 会跨跳传播 | 浏览器不原生支持（要 grpc-web + 支持 trailers 的代理） |

> 🎯 一句话总结：**开源生态把「协议」标准化了，把「网关」留给你了。** 而 10 万长连接的成本 90% 在网关：连接账、反代配置、合批、背压、优雅关停。这部分没有开源能替你扛，因为它跟你的部署形态、你的反代、你的 SLO 强耦合。

---

## 7.7 事故清单

- [ ] **反代缓冲没关**：TTFT 从 0.9s 变 4s，用户 3s 内关闭率 18%。
      → 根因：`proxy_buffering on` + gzip。
      → 兜底：`X-Accel-Buffering: no` + 反代配置 + **上线前用 `curl -N` 打时间戳验证**（见自检题 2）。
- [ ] **滚动更新杀掉在途流**：用户看到半句话，重试后答案不一致。
      → 根因：`SIGTERM` 直接退出，没有 `preStop` 摘流量和 `graceful shutdown`。
      → 兜底：`preStop sleep 5s` + `srv.Shutdown(ctx)`（超时 ≥ 最长 Run 的 10%）+ 服务端主动下发 `error{retryable:true}` 让客户端立刻重连续传（第 46 章）。
- [ ] **取消不生效，token 继续烧**：每次取消多花 1,850 token。
      → 根因：取消只断了前端，没传到底层模型调用。
      → 兜底：`ctx` 一路传到底 + 模型侧 abort + 工具侧标记 orphan（第 8 章）。
- [ ] **慢消费者拖垮整节点**：一个 2G 网络下的客户端让同分片 200 个 Run 卡住。
      → 根因：共享订阅队列 + 无背压上限。
      → 兜底：每连接独立有界队列 + 三档背压 + 断开优于缓冲。
- [ ] **重连风暴**：一次网关抖动，10 万客户端同时重连，`somaxconn` 打满。
      → 根因：`retry` 固定 1.5s，无抖动。
      → 兜底：`retry: 1500` + 客户端指数退避 + 随机抖动 ±30%；网关按 IP 段限速接入。
- [ ] **h1.1 六连接上限**：多标签页用户偶现首字延迟 30s。
      → 根因：浏览器对同域 h1.1 限 6 连接。
      → 兜底：上 h2 或 SSE 走独立子域。
- [ ] **事件 seq 断号没人发现**：客户端渲染出「退款会在 3-5 个」后面直接接「工作日」，中间少了一句免责声明。
      → 根因：合批时取了第一个 seq。
      → 兜底：合批帧必须带「覆盖区间」或至少带最后一个 seq；客户端做连续性校验。

---

## 7.8 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 选型 | 对外 SSE + POST 控制通道；对内 gRPC；WS 只给真双向；长轮询只做兜底 |
| SSE 的三个免费礼物 | 自动重连、`Last-Event-ID`、文本可读易调试——别自己造 |
| 续传顺序 | 先订阅 → 再回放 → 后按 seq 去重。搞反了必丢事件 |
| seq 语义 | Run 内单调连续；断号 = 客户端该重拉；合批帧带最后一个 seq |
| 合批 | 60ms / 4 token，帧率降 4 倍、带宽降 2.7 倍；`tool/approval/correction/done` 不可合并 |
| 背压 | 队列 256 → 丢 token 保留控制帧 → 超 512 直接断。**断开比缓冲便宜** |
| 反代 | `proxy_buffering off` + `gzip off` + `read_timeout 3600s` + `Connection ""` |
| HTTP/2 | h2 能破 6 连接限制，但端到端必须都是 h2；大事件注意流控窗口 |
| 连接账 | 每连接 ~40KB；10 万连接 ≈ 4.3GB + 10 万 fd + 1 万端口；单节点 5,000，24 节点 |
| 优雅关停 | `preStop` + `Shutdown` + 主动下发可重试错误，别让用户看半句话 |
| 一句话 | **流式的难点不在「推得出去」，在「断了能接上、接上不重来、重来不算错账」。** |

## 7.9 自检题

1. **你的 SSE 接口在压测里 TTFT 是 800ms，但真实用户反馈「要等四五秒」。列出三个最可能的原因和各自的验证方法。**
   > 要点：① 反代/CDN 缓冲 —— `curl -N -w '%{time_starttransfer}'` 对比直连与过反代的差异；② gzip 或运营商压缩代理攒包 —— 看响应头有没有 `Content-Encoding`；③ 浏览器 h1.1 六连接上限 —— 开 7 个标签页复现。还有一个隐藏项：**前处理/检索在流开始前同步做完了**，那不是传输问题，是第 10 章的延迟预算问题。

2. **为什么「先订阅、再回放」比「先回放、再订阅」正确？给出一个能造成数据丢失的时序。**
   > 要点：`t0` 回放读到 seq=100 结束；`t1`（间隔 8ms）Worker 产生 seq=101；`t2` 订阅才生效。seq=101 永久丢失。先订阅的话，101 落在订阅缓冲里，回放结束后按 `seq > last` 过滤即可，最多重复不会丢。**这是 Event Sourcing「至少一次」语义在传输层的体现。**

3. **一个节点跑到 5,000 连接时开始出现「部分用户卡住但没报错」，你的排查顺序是什么？**
   > 要点：① 看 fd（`lsof -p` 数量，是否接近 `ulimit -n`）；② 看 `accept` 队列（`ss -lnt` 的 `Recv-Q`，`somaxconn` 溢出会静默丢 SYN）；③ 看是否有慢消费者占满订阅队列（查 `slow_consumer` 指标）；④ 看 `TIME_WAIT` 数量（上游连接）；⑤ 看 GC/内存是否触发长停顿。**「卡住但没报错」几乎一定是资源耗尽后的静默排队，不是逻辑 bug。**

## 7.10 延伸阅读

- 上一章：[6. Session / Run / Step / Event 状态模型](6-session-run-event-model.md)
- 下一章：[8. Run API 设计：幂等、取消、审批、回放](8-run-api-design.md)
- SSE 规范：WHATWG HTML Standard 里 `server-sent events` 一节（`Last-Event-ID`、`retry`、注释帧都在这里定义）
- WebSocket：RFC 6455；心跳与关闭码看 §7.1.2 / §7.4
- Vercel AI SDK 的 data stream 协议、AG-UI 协议事件表（以官方文档最新版为准）
- 流式与渲染、结构化增量、工具参数跨包拼接的细节：[36. 流式输出工程](36-streaming-output.md)
- 流式与计费/审计耦合、撤回机制：[37. 输出审计与撤回](37-output-audit-retraction.md)
- 优雅关停与连接预热：[46. 部署、容量与冷启动](46-deployment-capacity.md)
- 长连接压测方法：[28. 大流量不崩](28-peak-traffic.md)
- 语言/运行时细节（goroutine 调度、netpoller）：[../go/0-summary.md](../go/0-summary.md)

---

**交叉引用**：总纲 [0-summary.md](0-summary.md) ｜ 前置 [6-session-run-event-model.md](6-session-run-event-model.md) ｜ 下一章 [8-run-api-design.md](8-run-api-design.md)
