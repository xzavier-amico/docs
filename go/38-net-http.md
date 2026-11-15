# 38. net/http 深潜：Server、中间件与超时

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 工程化与服务端 ｜ **编号**：38 / 54
> **本章定位**：`net/http` 是「标准库即框架」的最佳样本，也是最容易用错的标准库。本章把 `Server` 的 goroutine-per-conn 模型、超时四件套、`Shutdown`、`Transport` 的 `MaxIdleConnsPerHost=2`、`ReverseProxy`、`ResponseController` 一次讲透，并和 Tomcat 线程模型正面比一次。
> **读法**：38.3 的两张图（accept 循环 / 连接复用）建议背下来；38.6 的坑表可以直接贴进 CR 模板。

---

## 38.1 引子：三个「我明明写了超时」的夜晚

**事故一**：服务上线后 P99 从 200ms 涨到 9s，CPU 和内存都正常，QPS 却掉了一半。查了两小时发现：调用下游 LLM API 的那个 `http.Client` 写的是 `http.Client{Timeout: 60 * time.Second}`——注释写着「已经设了超时」。但那个 client 用的是**默认 Transport**，而账号的并发配额是 200；客户端并发 200、每条连接串行——本该有 200 条连接，实际只有 200 个请求排队在 **2 条连接**上。因为 `DefaultMaxIdleConnsPerHost = 2`。

**事故二**：SSE 流式输出，用户抱怨「回答总在 30 秒整断掉」。原因是你给 `Server` 设了 `WriteTimeout: 30 * time.Second`——**它是从「读到请求头」开始算的总时长**，不是「单次写」的时长，流式响应必死。

**事故三**：滚动发布时，监控上每波发布都有 500 个 502。你的优雅关停只写了 `srv.Close()` 吗？还是写了 `Shutdown(ctx)` 但 `ctx` 是 `context.Background()` 而且 K8s 的 `terminationGracePeriodSeconds: 30` 比你 `Shutdown` 的 20s + preStop 还紧？

这三个事故都来自同一个认知缺口：**`net/http` 的默认值是为「hello world」调的，不是为生产调的。** 而它把「可调」做得很克制——九个字段，你得知道每个都管什么。

---

## 38.2 直觉模型：一条连接一个 goroutine，一个请求一次函数调用

`net/http` 的心智模型简单到不可思议：

```
HTTP 请求的生命周期 = 一次函数调用
    func ServeHTTP(w http.ResponseWriter, r *http.Request)

并发模型 = goroutine-per-connection（不是 per-request！）
```

对比 Java：

```
【Tomcat / Servlet】
  Acceptor 线程 ──accept──▶ Poller 线程 ──▶ 从 maxThreads=200 的线程池取一个 worker
                                            （池满了 → 排队到 acceptCount=100 → 还满就拒）
  成本：每个线程 ~1MB 栈 + 上下文切换；连接数 ≈ 并发线程数上限

【net/http】
  Serve 循环 ──Accept──▶ go c.serve(ctx)   ← 一个连接一个 goroutine，起始栈 2KB
  goroutine 阻塞在 read 时由 netpoller 挂起（epoll），不占 M、不占线程
  成本：每连接 ~几 KB 内存 + 一个 goroutine 结构（详见第 17、19 章）
  并发上限：没有 maxThreads，只有内存和 fd
```

| Tomcat 概念 | net/http 对应物 | 差异 |
| --- | --- | --- |
| `maxThreads=200` | **不存在** | 想限并发得自己上信号量/令牌桶（第 32 章） |
| `acceptCount=100`（等待队列） | `net.Listen` 的 backlog（内核参数 `somaxconn`） | Go 侧不可调，得改内核 |
| `maxConnections=8192` | 无 | 只有 `ulimit -n` 和内存 |
| `connectionTimeout` | `ReadHeaderTimeout` + `IdleTimeout` | 拆成两个，更精确 |
| `keepAliveTimeout` | `IdleTimeout` | 语义等价 |
| Servlet `Filter` 链 | `func(http.Handler) http.Handler` 中间件 | 手写组合，无 XML |
| `@WebServlet` | `http.ServeMux` / Gin 路由 | 1.22 起标准库支持方法与路径参数 |
| Servlet 3.0 Async / WebFlux | 不需要：handler 本来就能阻塞 | 一个 goroutine 阻塞在 LLM 调用上完全没问题 |
| `server.shutdown=graceful` | `srv.Shutdown(ctx)` | 一样，但你要自己接信号 |
| Tomcat JMX 指标 | `expvar` / Prometheus（第 35 章） | 没有内建 MBean |

> 💡 一个必须纠正的直觉：Java 里「阻塞一个线程」是昂贵的，所以大家拼命上异步；Go 里「阻塞一个 goroutine」是廉价的（2KB 栈，netpoller 帮你把 M 让出来）。**所以 Go 的 HTTP 服务不需要 Reactor 模式**——直接写同步代码，反而更好维护。真正需要担心的不是「阻塞」，而是「没有上限」。

---

## 38.3 底层实现：两张图看懂 Serve 与 Transport

### 38.3.1 Server：accept 循环 + 每连接一个 goroutine

```
srv.ListenAndServe()
  └─ net.Listen("tcp", addr) → l
  └─ srv.Serve(l)                      // net/http/server.go
       │
       │  for {                                       ← ① accept 循环（单 goroutine）
       │      rw, err := l.Accept()                   ← 阻塞在 netpoll，不占 OS 线程
       │      c := srv.newConn(rw)                    ← 每个连接一个 *conn
       │      c.setState(rw, StateNew)
       │      go c.serve(connCtx)                     ← ② 每连接一个 goroutine
       │  }
       ▼
  conn.serve(ctx)                       // 一个 conn 上的 keep-alive 循环
       │
       │  for {                                       ← ③ 同一连接可以跑 N 个请求
       │      w, err := c.readRequest(ctx)             ← 读请求行+header
       │      │   （超过 ReadHeaderTimeout / MaxHeaderBytes 直接 431/400）
       │      serverHandler{c.server}.ServeHTTP(w, w.req)   ← ④ 中间件链 → 你的 handler
       │      w.finishRequest()                        ← flush 输出缓冲
       │      if !w.shouldReuseConnection() { break }   ← 客户端 Connection: close？
       │      c.setState(rw, StateIdle)                ← ⑤ 空闲等下一个请求（IdleTimeout）
       │  }
       └─ 连接关闭 → goroutine 退出 → fd 释放
```

三个「所以」：

- **所以 `r.Body` 必须在 handler 返回前关掉/读完**——不读完就关闭连接，keep-alive 直接作废（见 38.3.3）。
- **所以 handler 里的 `panic` 会杀死这条连接**：`net/http` 内部 recover 后，如果 panic 值是 `http.ErrAbortHandler` 就静默中止，否则打日志并关闭连接。你的 recover 中间件必须做得更早。
- **所以「并发数」不来自线程池**：每个进来的连接都立刻拿到一个 goroutine，没有排队、没有拒绝、没有背压。**要想不被打爆，必须自己加限流。**

### 38.3.2 超时四件套：每个字段管的是哪个时间段

这是最容易记混的地方，画成时间轴就清楚了：

```
conn 建立          请求头读完          请求体读完       handler 返回     下一次请求到来
    │                  │                  │               │                │
    │◀─ ReadHeaderTimeout ─▶│              │               │                │
    │◀───────── ReadTimeout ──────────────▶│               │                │
    │                                      │◀─ WriteTimeout▶│                │
    │                                                       │◀─ IdleTimeout ─▶│
```

| 字段 | 起止 | 治什么 | 生产建议 |
| --- | --- | --- | --- |
| `ReadHeaderTimeout` | accept → header 读完 | **Slowloris 攻击**（连上不发头） | 5s，必设 |
| `ReadTimeout` | accept → body 读完 | 慢速上传的大请求 | 15~30s；上传接口单独调大 |
| `WriteTimeout` | header 读完 → 响应写完 | 慢客户端拖死 goroutine | 30s；**SSE/流式必须设 0** |
| `IdleTimeout` | 一次响应结束 → 下次请求 | 空闲连接占 fd | 60~120s（要小于上游 LB 的空闲回收时间） |
| `MaxHeaderBytes` | — | 超大 header 内存攻击 | 默认 1MB，通常调小到 64KB 以内 |

另外两个补充工具：`http.TimeoutHandler(h, 10*time.Second, "timeout")`（整体请求超时，但**会缓冲响应，不能用于流式**）；以及 `Server.BaseContext`（给每个 conn 的 ctx 挂值/超时源头）。

> 🩸 **最经典的一刀**：`WriteTimeout` 是**从读完请求头开始算的一次性的总预算**，不是「每次 Write 的超时」。做 SSE 时设了 `WriteTimeout: 30s`，流必然 30 秒断。**流式场景的正确姿势是 `WriteTimeout: 0` + 用 `ResponseController` 给每次写单独设 deadline**（38.5.5）。

### 38.3.3 Transport：连接复用才是性能的来源

```
client.Do(req)                          // net/http/client.go
   └─ t.roundTrip(req)                  // net/http/transport.go
        │
        ├─ pconn, err := t.getConn(treq)          ← ① 拿一条连接
        │     ├─ queueForIdleConn()               ← 有空闲？复用（上限 MaxIdleConnsPerHost，默认 2！）
        │     │     └─ 命中 → 直接 roundTrip
        │     └─ 没有 → queueForDial()            ← 新建 TCP（+TLS 握手）
        │           （受 MaxConnsPerHost 限制，排队等连接）
        │
        ├─ 在 persistConn 上 roundTrip             ← ② 一个读循环 goroutine 常驻
        │
        └─ resp.Body.Close()                      ← ③ 决定「复用 or 丢弃」
              ├─ body 已读到 EOF → tryPutIdleConn → 回 idle 池 ✅
              └─ body 没读完就 Close → 直接关 TCP ❌ → TIME_WAIT 暴涨、QPS 雪崩
```

`DefaultMaxIdleConnsPerHost = 2` 这条常量在 `net/http/transport.go` 里，含义是**每个 host 最多保留 2 条空闲连接**。注意它限制的**不是并发数**，而是「池子里躺着的连接数」——但没有空闲连接就得新建，新建完用完又被关掉（超过 2 条的部分），于是每条请求都在做 TCP + TLS 握手。QPS 500 的服务，每秒 500 次握手，延迟直接翻倍。

> ⚠️ 而 `http.DefaultClient` / `http.Get` 用的都是 `DefaultTransport`（`MaxIdleConns: 100, IdleConnTimeout: 90s`，但 **`MaxIdleConnsPerHost` 仍是默认的 2**）。**这是在 Go 里调外部 API 的第一大坑，没有之一。**

---

## 38.4 Java 对照：为什么 Go 没有「连接池框架」

Java 里 HTTP 客户端池化是显式配置（`HttpClient.newBuilder().executor(...)`、OkHttp 的 `ConnectionPool(5, 5, MINUTES)`、Apache `PoolingHttpClientConnectionManager`）。Go 把它做进了 `Transport`，**默认开、默认小**：

| 维度 | Tomcat / HttpClient（Java） | net/http（Go） | Go 为什么这样选 |
| --- | --- | --- | --- |
| 并发原语 | 线程池（有界，池满拒绝） | goroutine（无界，来者不拒） | goroutine 便宜到不需要池；**上限交给业务自己管** |
| 队列/背压 | `acceptCount` + 队列 | **无内建** | Go 的信条：不替你猜业务容量（第 32 章） |
| 连接池 | 显式配置 | `Transport` 字段，默认存在但偏小 | 保守默认值 + 「你要为自己的流量负责」 |
| 超时 | `connectionTimeout` / `socketTimeout` / `requestTimeout` | 四个字段 + `Client.Timeout` + `ctx` | 更细粒度，但也更容易配错 |
| 优雅关停 | `server.shutdown=graceful` + `Lifecycle` | `Shutdown(ctx)` + `signal.NotifyContext` | 组件化，需要自己接线 |
| 中间件 | Filter / Interceptor（声明式） | `func(Handler) Handler`（组合式） | 没有容器，组合就是全部（第 10 章） |
| 上下文传播 | ThreadLocal + MDC | `r.Context()` 显式传递 | 见第 35 章 |

> 💡 一句话：**Tomcat 是「帮你挡住流量」，`net/http` 是「如实反映流量」。** 前者让你在有限资源下体面地降级，后者让你在小流量下几乎没有开销——代价是上限管理全在你手上。

---

## 38.5 工程落地：五份可直接抄的代码

### 38.5.1 生产级 Server + 优雅关停

```go
package main

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"os/signal"
	"syscall"
	"time"
)

func serve(ctx context.Context, h http.Handler, log *slog.Logger) error {
	srv := &http.Server{
		Addr:              ":8080",
		Handler:           h,
		ReadHeaderTimeout: 5 * time.Second,  // 防 Slowloris
		ReadTimeout:       30 * time.Second, // 含 body
		WriteTimeout:      0,                // 0 = 不限：SSE/流式必须（用 ResponseController 控）
		IdleTimeout:       90 * time.Second,
		MaxHeaderBytes:    64 << 10, // 64KB
		BaseContext:       func(net.Listener) context.Context { return ctx },
		ErrorLog:          slog.NewLogLogger(log.Handler(), slog.LevelWarn),
		ConnState: func(c net.Conn, s http.ConnState) {
			if s == http.StateNew || s == http.StateClosed {
				connGauge.Add(context.Background(), mapState(s)) // 连接数指标
			}
		},
	}

	// 优雅关停的一部分：让长连接知道该走了（HTTP/2 GOAWAY 等）
	srv.RegisterOnShutdown(func() { log.Info("server shutting down") })

	ln, err := net.Listen("tcp", srv.Addr)
	if err != nil {
		return err
	}

	errCh := make(chan error, 1)
	go func() {
		if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- err // ErrServerClosed 是 Shutdown 的正常产物，不算错
		}
	}()
	log.Info("listening", "addr", ln.Addr().String())

	select {
	case err := <-errCh:
		return err
	case <-ctx.Done():
	}

	// Shutdown：先关 listener（不再接新连接），再等空闲连接关闭、在途请求完成
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		return err // 超时说明还有请求卡着，让它返回错误让上层告警
	}
	return nil
}

func main() {
	// 注意：信号 ctx 只用于「通知」，Shutdown 必须用新的、带 deadline 的 ctx
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if err := serve(ctx, newRouter(), slog.Default()); err != nil {
		slog.Error("exit", "err", err)
	}
}
```

`Shutdown` 的语义要精确记住三点：① 它**不会**关闭被 `Hijack()` 的连接（WebSocket 自己负责，见第 39 章）；② 它会一直等，直到 `ctx` 到期，到期返回 `ctx.Err()` 但**不会**强杀在途请求（要强杀只能 `srv.Close()`）；③ 在 K8s 里它必须与 `terminationGracePeriodSeconds` 和 preStop 睡眠接力，否则流量还在打进来你就已经不接了（详见 [第 52 章](52-deployment-sre.md)）。

### 38.5.2 中间件：`func(http.Handler) http.Handler`

```go
type Middleware func(http.Handler) http.Handler

// Chain 从外到内依次包裹：Chain(a, b)(h) 执行顺序 a → b → h → b → a
func Chain(h http.Handler, mws ...Middleware) http.Handler {
	for i := len(mws) - 1; i >= 0; i-- {
		h = mws[i](h)
	}
	return h
}

func Recover(log *slog.Logger) Middleware {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			defer func() {
				if v := recover(); v != nil {
					if v == http.ErrAbortHandler { // 标准库用它「主动中止连接」，别当错误
						panic(v)
					}
					log.ErrorContext(r.Context(), "panic", "v", v, "path", r.URL.Path,
						"stack", string(debug.Stack()))
					http.Error(w, "internal error", http.StatusInternalServerError)
				}
			}()
			next.ServeHTTP(w, r)
		})
	}
}

func RequestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := r.Header.Get("X-Request-Id")
		if id == "" {
			id = newID() // 自己对 ulid/uuid 的实现
		}
		w.Header().Set("X-Request-Id", id)
		// 只放请求域的非业务数据；业务值请用显式参数（第 24 章）
		next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), reqIDKey{}, id)))
	})
}

func AccessLog(log *slog.Logger) Middleware {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			start := time.Now()
			sw := &statusWriter{ResponseWriter: w, status: http.StatusOK}
			next.ServeHTTP(sw, r)
			log.InfoContext(r.Context(), "http.access",
				"method", r.Method, "path", r.URL.Path, "status", sw.status,
				"dur_ms", time.Since(start).Milliseconds(), "bytes", sw.n)
		})
	}
}

// statusWriter 必须实现 Unwrap，否则 ResponseController 的 Flush/Hijack 会失效
type statusWriter struct {
	http.ResponseWriter
	status int
	n      int
}

func (w *statusWriter) WriteHeader(c int) { w.status = c; w.ResponseWriter.WriteHeader(c) }
func (w *statusWriter) Write(p []byte) (int, error) {
	n, err := w.ResponseWriter.Write(p)
	w.n += n
	return n, err
}
func (w *statusWriter) Unwrap() http.ResponseWriter { return w.ResponseWriter } // ← 关键

// 组装：Recover 最外层，AccessLog 紧跟，然后业务路由
h := Chain(mux, Recover(log), RequestID, AccessLog(log), MaxBytes(4<<20))
```

`Unwrap()` 那一行是 Go 1.20 之后的新约定：`ResponseController` 会通过 `Unwrap` 一层层往下找支持 `Flush`/`Hijack`/`SetWriteDeadline` 的原始 `ResponseWriter`。**中间件包装了 `ResponseWriter` 却忘了 `Unwrap`，你的 SSE 会静默失效**——这是最隐蔽的一类 bug。

### 38.5.3 Transport：把 `MaxIdleConnsPerHost` 调到能用

```go
// 记住：一个 Transport 在全进程共享，不要每次请求 new 一个（否则连接池形同虚设）
var httpClient = &http.Client{
	Timeout: 60 * time.Second, // 兜底：整个请求（含读 body）的总预算
	Transport: &http.Transport{
		Proxy: http.ProxyFromEnvironment,
		DialContext: (&net.Dialer{
			Timeout:   3 * time.Second,
			KeepAlive: 30 * time.Second,
		}).DialContext,
		ForceAttemptHTTP2:     true,
		MaxIdleConns:          200,           // 全进程空闲连接总数
		MaxIdleConnsPerHost:   100,           // ★ 默认只有 2，必须按并发改
		MaxConnsPerHost:       120,           // 每 host 硬上限（含正在跑 + 空闲）
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   5 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
		ResponseHeaderTimeout: 30 * time.Second, // 只等响应头：区分「慢」和「不响应」
	},
}
```

`MaxIdleConnsPerHost` 怎么定？经验公式：**它 ≈ 你打这个下游的峰值并发数**。峰值并发 = 目标 QPS × 平均延迟（秒）。打 LLM 的场景，一个请求 3~10 秒，QPS 20 就是 60~200 并发，**2 是绝对不够的**。

### 38.5.4 Body 的关闭与连接复用（唯一正确的 6 行）

```go
func doJSON(ctx context.Context, req *http.Request, out any) error {
	resp, err := httpClient.Do(req.WithContext(ctx))
	if err != nil {
		return fmt.Errorf("do: %w", err)
	}
	// defer 一定要在 err 判断之后立刻写，且必须 Close
	defer resp.Body.Close()

	// 关键：即使你不需要 body，也要把它读干净，否则连接不复用
	if resp.StatusCode >= 400 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<10))
		return fmt.Errorf("status=%d body=%s", resp.StatusCode, b)
	}
	// 限制大小 + 流式解码（不要 io.ReadAll 到内存，第 31 章）
	return json.NewDecoder(io.LimitReader(resp.Body, 8<<20)).Decode(out)
}
```

三条铁律：① `defer resp.Body.Close()` 紧跟 `Do` 之后；② **非 2xx 也要读 body**（不然丢弃连接）；③ 大响应别 `ReadAll`，用 `io.Copy(io.Discard, resp.Body)` 或流式 `Decoder`。想主动放弃一个响应，就用 `http.MaxBytesReader` 或读完丢弃，不要「关掉不读」。

### 38.5.5 `ResponseController`（1.20）：流式写入的正确姿势

```go
func sseHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("X-Accel-Buffering", "no") // 关掉 nginx 缓冲（第 39 章）

	rc := http.NewResponseController(w) // 能穿透中间件（靠 Unwrap）拿到真正的 writer
	// 只给「这一次写」设 deadline，而不是给整个连接设 WriteTimeout
	_ = rc.SetWriteDeadline(time.Now().Add(15 * time.Second))
	defer rc.SetWriteDeadline(time.Time{}) // 清掉 deadline，避免影响后续

	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming unsupported", http.StatusInternalServerError)
		return
	}

	for chunk := range chunks(r.Context()) {
		if _, err := fmt.Fprintf(w, "data: %s\n\n", chunk); err != nil {
			return // 客户端断了
		}
		_ = rc.SetWriteDeadline(time.Now().Add(15 * time.Second)) // 每次写后重置
		flusher.Flush()
	}
	// rc.Flush() 是等价的 go1.20+ 写法：不依赖 w 是否实现了 Flusher
}
```

`ResponseController` 提供 `Flush()`、`Hijack()`、`SetReadDeadline()`、`SetWriteDeadline()`、`EnableFullDuplex()`（1.21，允许在 handler 里边读边写），失败返回 `http.ErrNotSupported`。**它的存在意义就是「不用做类型断言、还能穿透中间件」**——这就是 38.5.2 里那个 `Unwrap()` 的价值。

### 38.5.6 `ReverseProxy`：给 LLM 做网关

```go
func newLLMProxy(target string) (http.Handler, error) {
	u, err := url.Parse(target)
	if err != nil {
		return nil, err
	}
	p := httputil.NewSingleHostReverseProxy(u)

	// 1.20+ 用 Rewrite 代替 Director（Director 会保留旧语义，容易误用）
	p.Rewrite = func(pr *httputil.ProxyRequest) {
		pr.SetURL(u)                                  // 设置 scheme/host/path（不做反向路径拼接）
		pr.Out.Host = u.Host
		pr.SetXForwarded()                            // X-Forwarded-For/Proto/Host
		pr.Out.Header.Set("Authorization", "Bearer "+secrets.LLMKey) // 密钥只在网关侧
	}

	p.Transport = httpClient.Transport                // 复用同一个连接池
	p.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		slog.ErrorContext(r.Context(), "proxy", "err", err, "upstream", u.Host)
		http.Error(w, "upstream unavailable", http.StatusBadGateway)
	}
	// SSE / 流式：立即 flush，否则要等 buffer 满
	p.FlushInterval = -1
	p.ModifyResponse = func(resp *http.Response) error {
		resp.Header.Set("X-Proxy", "agent-gw")
		return nil
	}
	return p, nil
}
```

`ReverseProxy` 会自动处理 hop-by-hop header、`101 Switching Protocols` 的双向拷贝（所以 WS 也能反代），但对 SSE 必须 `FlushInterval = -1`，否则客户端只看到「一直没响应」。

---

## 38.6 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| 下游 QPS 上不去，延迟翻倍 | `MaxIdleConnsPerHost` 默认 2，请求都在建连 | 按峰值并发设置（≥ QPS × 延迟） | 38.3.3 |
| TIME_WAIT 暴涨、端口耗尽 | 响应 body 没读完就 Close；或每请求 new Client | 读干净再 Close；Client/Transport 全局复用 | 38.5.4 |
| SSE 30 秒整断流 | `WriteTimeout` 是整请求预算 | 流式接口 `WriteTimeout: 0` + `ResponseController` 逐次设 deadline | 38.3.2 |
| 慢客户端拖死服务 | 没设 `WriteTimeout` | 普通接口设 30s（流式单独处理） | 38.3.2 |
| Slowloris 打满 goroutine | 没设 `ReadHeaderTimeout` | 必设 5s | 38.3.2 |
| 发布时 502 / 请求被重置 | `Shutdown` 太快、没等返回；或 K8s grace 太短 | `Shutdown(ctx)` 超时 > P99，返回后再退出；配 preStop | 38.5.1 |
| 本地 `err == http.ErrServerClosed` 被当异常打日志 | `Shutdown` 的必然产物 | `errors.Is(err, http.ErrServerClosed)` 忽略 | 38.5.1 |
| 中间件里加了包装，SSE/`Hijack` 失效 | `ResponseWriter` 包装没实现 `Unwrap()` | 加 `func (w *W) Unwrap() http.ResponseWriter` | 38.5.2 |
| `http.TimeoutHandler` 用在流式接口 | 它会缓冲整个响应 | 流式用 `ResponseController` 或 ctx 超时 | 38.3.2 |
| WS/升级请求关不干净 | `Shutdown` 不管 hijacked 连接 | 自己维护连接集合，事后逐个 Close（第 39 章） | 38.5.1 |
| 上传大文件 OOM | 没限 `MaxBytesReader` / `MaxHeaderBytes` | `http.MaxBytesReader(w, r.Body, n)` + 调小 header 上限 | 38.5.2 |
| handler 里 `panic` 导致连接被掐 | 没有 Recover 中间件（或顺序错了） | Recover 放在链的最外层 | 38.5.2 |
| 反代 SSE 一直不出字 | `FlushInterval` 没设；上游/nginx 缓冲 | `FlushInterval = -1` + `X-Accel-Buffering: no` | 38.5.6 |

---

## 38.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 并发模型 | goroutine-per-connection（不是 per-request），无内建上限 |
| 一个连接 | `conn.serve` 里 keep-alive 循环跑 N 个请求 |
| 请求 = 函数 | `ServeHTTP(w, r)`；没有线程池、没有 Filter XML |
| 超时四件套 | `ReadHeaderTimeout` / `ReadTimeout` / `WriteTimeout` / `IdleTimeout` |
| `WriteTimeout` | 整请求预算，流式必须设 0 |
| `Shutdown` | 关 listener → 等空闲与在途；不管 hijacked；`ErrServerClosed` |
| `MaxIdleConnsPerHost` | **默认 2**，Go 调外部 API 第一大坑 |
| 连接复用条件 | body 读到 EOF + Close；否则直接关 TCP |
| 中间件 | `func(http.Handler) http.Handler`，包装 `ResponseWriter` 必须 `Unwrap()` |
| `ResponseController` | 1.20+；Flush/Hijack/SetWriteDeadline/EnableFullDuplex，穿透中间件 |
| `ReverseProxy` | `Rewrite` + `SetXForwarded`；SSE 需 `FlushInterval: -1` |
| 对比 Tomcat | 没有 maxThreads/acceptCount，上限由内存与 fd 决定，限流自己加 |

## 38.8 自检题

1. **`WriteTimeout: 30s` 的 SSE 接口为什么一定在 30 秒断流？正确做法是什么？**
   > 答案要点：`WriteTimeout` 的计时从「读完请求头」开始，覆盖到「响应写完」为止，是一次性的整请求预算，不是每次 Write 的超时。流式接口应设 `WriteTimeout: 0`，用 `http.NewResponseController(w).SetWriteDeadline(...)` 在每次写之前/之后单独设 short deadline，并配合 `Flush()`。

2. **`DefaultMaxIdleConnsPerHost = 2` 限制的是并发数吗？为什么它会毁掉你的吞吐？**
   > 答案要点：它限制的是「每 host 保留的空闲连接数」，不是并发上限。没有空闲连接时 `Transport` 会新建连接，用完超出的部分被关闭，于是高 QPS 下几乎每个请求都要 TCP+TLS 握手，延迟与 CPU 双双上升、TIME_WAIT 增多。修法：按峰值并发（QPS × 平均延迟）设置 `MaxIdleConnsPerHost`，并全局复用 `Transport`。

3. **你在 `AccessLog` 中间件里把 `ResponseWriter` 包了一层，之后 SSE 的 `Flush` 全部静默失效。原因和修法？**
   > 答案要点：包装类型只实现了 `http.ResponseWriter`，丢掉了原始 writer 的 `http.Flusher`/`Hijacker`/deadline 能力；Go 1.20 的 `ResponseController` 通过 `Unwrap() http.ResponseWriter` 逐层向下寻找能力。修法：在包装类型上实现 `Unwrap()`（或显式转发 `Flush()`）。

## 38.9 延伸阅读

- `net/http/server.go`：`Server.Serve`、`conn.serve`、`response.finishRequest`、`Server.Shutdown`、`Server.RegisterOnShutdown`
- `net/http/transport.go`：`Transport.getConn`、`queueForIdleConn`、`tryPutIdleConn`、常量 `DefaultMaxIdleConnsPerHost`
- `net/http/httputil/reverseproxy.go`：`ProxyRequest.SetURL/SetXForwarded`、`FlushInterval`、双向拷贝
- `net/http/responsecontroller.go`：`NewResponseController`、`ErrNotSupported`、`EnableFullDuplex`
- Go 1.22 路由增强：`ServeMux` 的 `"GET /path/{id}"` 模式与 `r.PathValue("id")`
- 交叉引用：[第 32 章](32-high-concurrency.md) 连接池容量推导与压测；[第 39 章](39-realtime-sse-websocket.md) SSE/WS 实战；[第 52 章](52-deployment-sre.md) 探针与 K8s 优雅关停接力

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 37 章](37-codegen-reflection.md) 代码生成与反射：schema 自动化
- 下一章：[第 39 章](39-realtime-sse-websocket.md) 实时通信：SSE / WebSocket / gRPC streaming 怎么选
