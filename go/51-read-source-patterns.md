# 51. 读源码方法论 + 框架设计模式提炼

> **状态**：`已完成（初稿）` ｜ **所属**：第八部分 · 源码精读 ｜ **编号**：51 / 54
> **本章定位**：前三章是「读别人」，这一章是「把方法交给你」。前半篇是一套可复用的读码流程与工具链，后半篇把 Go 生态里 90% 的框架骨架拆成 8 个模式，最后把前 50 章织成一张图。
> **读法**：51.3 的工具清单建议边读边在终端敲一遍；51.4 的八段模板可以直接抄进你的项目；51.7 的知识地图建议打印出来贴在显示器旁。

---

## 51.1 引子：你以为你读了源码

三种典型的「假读源码」：

**假读①**：打开 `gin-gonic/gin`，从 `main` 找错（它没有 `main`），最后停在 `gin.go` 里 2000 行的 `Engine` 结构体上，看了半天字段，合上电脑，什么也没记住。

**假读②**：老板让你「参考 kratos 做个插件体系」，你 `git clone` 后 `find . -name "*.go" | wc -l` → 800+ 文件，`internal/` 下面每个包都只有 200 行，看谁都不像「核心」，两小时后放弃，回去自己拍脑袋写了一个全局 map。

**假读③**：你想知道「`context` 取消到底怎么传到 socket」，于是从 `context/context.go` 开始读，读到 `cancelCtx.cancel()` 就停了——因为后面 `net/http` 的 `conn.serve` 到 `netFD.Read` 的链条有十几层，没有工具的支撑根本串不起来。

三个失败共享同一个原因：**读源码不是「从第一行读到最后一行」，而是「先建立坐标，再沿着一条调用链打穿」。** 这一章给的就是建立坐标和打穿链条的方法。

---

## 51.2 直觉模型：源码只有三件套

任何 Go 框架，无论多大，都只有三样东西：

```text
        ┌──────────────────────────────────────────────────────────┐
        │  ① 接口（接缝）   2~5 个方法的小接口，定义「可替换点」    │
        │       io.Reader · http.Handler · Runnable[I,O] · Tool   │
        ├──────────────────────────────────────────────────────────┤
        │  ② 装配（构造）   NewXxx(opts ...Option) 链 + 注册表     │
        │       func options · registry · 显式 main 里串起来       │
        ├──────────────────────────────────────────────────────────┤
        │  ③ 执行（循环）   一个 for 循环 + 一个 goroutine 模型    │
        │       accept 循环 · runnable 主循环 · handler 队列       │
        └──────────────────────────────────────────────────────────┘
```

**读码路线图**（照着走，两小时内必有所得）：

```text
 1. README + doc.go 的包注释       ← 先知道它想解决什么问题（10 分钟，别跳）
            │
 2. go doc <pkg> 看导出符号        ← 认路：谁是我的入口
            │
 3. 找「接口」                     ← 用 go doc -all 或直接 grep "^type .* interface"
            │
 4. 读 *_test.go                   ← tests 即文档！同一个接口的最短正确用法在测试里
            │
 5. 找「装配函数」 NewXxx / Compile ← 参数怎么流进去，可选能力怎么开
            │
 6. 顺着一条调用链打穿（delve/加日志/读栈）
            │
 7. 写下「它本可以怎么做」          ← 这一步才叫读完；不做这步等于白读
```

> 💡 **Java 老手的常见误区**：Java 框架（Spring）读源码的入口是「注解 + Bean 生命周期」，Go 框架的入口永远是 **`NewXxx` 构造函数 + `main` 里的装配代码**。找不到注解没关系——**Go 没有容器在偷偷替你干活**（第 36 章）。

---

## 51.3 工具链：七个命令打穿一个仓库

| 命令 | 解决什么问题 | 典型用法 |
| --- | --- | --- |
| `go doc` | 「这个包有什么」 | `go doc net/http.Server`；`go doc -all sync`（含未导出注释）；`go doc -u` 看未导出符号 |
| `go list` | 「依赖关系长什么样」 | `go list -deps ./...`；`go list -f '{{.Imports}}' ./compose` 找分层是否被破坏 |
| `go test -run` | 「最短正确用法」 | `go test ./compose -run TestInterrupt -v`；再 `-cover` 看哪些分支没人测 |
| `dlv` | 「运行时到底谁在跑」 | `dlv test ./compose` → `break compose/graph_run.go:120` → `goroutines`/`bt`/`frame` |
| `-gcflags='-m'` | 「这行为什么分配」 | `go build -gcflags='-m -m' ./... 2>&1 \| grep escaped`（第 14 章） |
| `-gcflags='-S'` | 「编译器怎么理解这行」 | `go build -gcflags='-S' -l ./x 2>&1 \| less`，看内联与 bounds check（第 27 章） |
| `git log -S` | 「这行为什么会变成这样」 | `git log -S 'BindTools' --oneline` 找 API 演进的动机 |

再加两个「不出声的杀手」：

- **模块缓存就是读到手的源码**：`go mod download -x` 之后代码在 `$GOMODCACHE`（`go env GOMODCACHE`）。比在 GitHub 网页上翻快十倍，而且可以就地 `dlv` 断点。
- **`GOROOT/src`**：标准库永远是最值得读的 Go 代码。`runtime/proc.go` 的 `schedule()`、`net/http/server.go` 的 `conn.serve`、`context/context.go` 的 `cancelCtx`——全书反复引用的就这几处。

一段真实的「用工具求证」示例（假设你想确认 `http.Server` 到底有没有兜底超时）：

```bash
$ go doc net/http.Server | grep -E "Timeout|Idle"
	ReadHeaderTimeout time.Duration
	ReadTimeout       time.Duration
	WriteTimeout      time.Duration
	IdleTimeout       time.Duration
# 结论：四个独立超时，没有「总超时」。所以 ctx 必须自己加（第 38 章）
```

> ⚠️ **不要一开始就用 IDE 的「Find Usages」**。它会给你 300 个调用点，而你需要的是「**主链路上的那 3 个**」。先靠调用链图找到主干，再用 usages 找分支。

---

## 51.4 Go 世界的 8 个高频模式（可复用模板）

这 8 个模式撑起了 Go 生态 90% 的框架代码。**每个都给你一段能编译的骨架 + 一句「什么时候别用」**，因为模式误用比不会用更贵。

### ① Functional Options：替代「构造函数参数爆炸」

```go
type Server struct {
	addr         string
	maxConns     int
	readTimeout  time.Duration
	logger       *slog.Logger
}

type Option func(*Server)          // 关键：Option 是函数，不是接口

func WithMaxConns(n int) Option {
	return func(s *Server) { if n > 0 { s.maxConns = n } }
}
func WithLogger(l *slog.Logger) Option {
	return func(s *Server) { s.logger = l }
}

func NewServer(addr string, opts ...Option) *Server {
	s := &Server{addr: addr, maxConns: 100, logger: slog.Default()}
	for _, o := range opts { o(s) }     // 顺序即语义：后面的覆盖前面的
	return s
}
// 调用：NewServer(":8080", WithMaxConns(500), WithLogger(l))
```

同构于 Java 的 Builder，但**没有 Builder 类**：一个 `func(*T)` 就够了。这就是「组合优于继承」在装配层的体现（第 10、36 章）。gin、eino（`compose.WithCheckPointID`）、MCP SDK（`ServerOptions` 混用字段与函数）全都在用。
**别用**：参数只有 1～2 个且永远不变时——直接两个位置参数更清楚；`Option` 会掩盖「必填项」。

### ② Middleware：装饰器链，AOP 的 Go 版

```go
type HandlerFunc func(ctx context.Context, req Request) (Response, error)
type Middleware func(HandlerFunc) HandlerFunc   // ← 所有中间件框架的核心签名

func Chain(h HandlerFunc, ms ...Middleware) HandlerFunc {
	for i := len(ms) - 1; i >= 0; i-- {  // 倒序包：ms[0] 在最外层
		h = ms[i](h)
	}
	return h
}

func Recovery() Middleware {
	return func(next HandlerFunc) HandlerFunc {
		return func(ctx context.Context, req Request) (resp Response, err error) {
			defer func() {
				if r := recover(); r != nil {
					err = fmt.Errorf("panic: %v", r)   // 库不许 panic 出去（第 7 章）
				}
			}()
			return next(ctx, req)
		}
	}
}
```

**判断**：`func(T) T` 这个形状是 Go 最重要的抽象——HTTP、RPC、gRPC、eino、MCP 全是它。**关键差异**：Java 的 AOP 靠动态代理在运行期织入，Go 的中间件在**装配期**就把函数拼成了一棵树，运行时零额外开销（第 27 章）。

### ③ Registry：没有注解扫描，就自己注册

```go
var (
	mu        sync.RWMutex
	providers = map[string]func() Provider{}
)

func Register(name string, f func() Provider) {
	mu.Lock(); defer mu.Unlock()
	if _, dup := providers[name]; dup { panic("dup provider: " + name) }  // 启动期 panic 可以接受
	providers[name] = f
}

func Open(name string) (Provider, error) {
	mu.RLock(); f, ok := providers[name]; mu.RUnlock()
	if !ok { return nil, fmt.Errorf("unknown provider %q", name) }
	return f(), nil
}
// 各实现包的 init() 里：func init() { Register("openai", func() Provider { return &openaiP{} }) }
```

**为什么 Go 用 `init()` 注册而不是注解扫描**：因为 `import _ "xxx/driver"` 这一行就是显式的、可 grep 的、编译期确定的「我要启用它」。`database/sql`、`image`、`net/http/pprof` 都是这个套路。
**别用**：注册表 + `init()` 会让依赖隐藏。**在 `main` 里显式调用 `Register` 更好**——除非是「驱动型」插件（第 10 章）。

### ④ Actor / Single Writer：用「一个人写」消灭锁

```go
type counter struct{ ch chan op }          // 所有变更都进这个 mailbox
type op struct{ delta int; resp chan int }

func newCounter() *counter {
	c := &counter{ch: make(chan op, 16)}
	go func() {                              // 唯一的 owner goroutine
		var n int
		for o := range c.ch {
			n += o.delta
			o.resp <- n
		}
	}()
	return c
}
func (c *counter) Add(d int) int {
	r := make(chan int, 1)
	c.ch <- op{d, r}
	return <-r
}
```

底座就是你熟悉的东西：**一个 goroutine + 一个 channel = 一个无锁对象**（第 20 章）。eino 的 `channelManager`、MCP 的 `handleAsync`、`runtime` 自己的 `work.span` 分配，本质都是「single writer」。
**别用**：纯读多写少的热路径——actor 每次读也要过 channel，`sync.RWMutex` + 分片更快（第 22 章）。

### ⑤ Pipeline：把长流程拆成 stage

```go
func stage(ctx context.Context, in <-chan Item) <-chan Item {
	out := make(chan Item)
	go func() {
		defer close(out)                 // 铁律：每个 stage 都要关自己出口
		for it := range in {
			select {
			case <-ctx.Done(): return    // 取消能穿透整条链
			case out <- transform(it):
			}
		}
	}()
	return out
}
```

**四个必守规则**：① 每个 stage 关自己的 `out`；② 上游 close 才能让 `range` 退出；③ 所有阻塞发送都要 `select ctx.Done()`；④ 有 fan-out 时必须「谁负责关」。违反第 ③ 条就是第 25 章那类 goroutine 泄漏。

### ⑥ Worker Pool：有界并发，保护下游

```go
func Pool(ctx context.Context, n int, jobs <-chan Job, handle func(context.Context, Job) error) error {
	g, ctx := errgroup.WithContext(ctx)
	for i := 0; i < n; i++ {
		g.Go(func() error {
			for {
				select {
				case <-ctx.Done(): return ctx.Err()
				case j, ok := <-jobs:
					if !ok { return nil }
					if err := handle(ctx, j); err != nil { return err }  // 第一个错就停
				}
			}
		})
	}
	return g.Wait()
}
```

**Why not 无限 goroutine**：LLM/DB 调用是「外部资源受限」型任务，起 10 万个 goroutine 会先把对端打挂、把自己 OOM（第 26、32 章）。也可以用 `errgroup.SetLimit(n)` 或 `x/sync/semaphore`。

### ⑦ errgroup：结构化并发的标准答案

```go
g, ctx := errgroup.WithContext(ctx)
for _, t := range tools {
	t := t
	g.Go(func() error { return t.Call(ctx) })   // 任一失败 → ctx 取消 → 其他自动退出
}
if err := g.Wait(); err != nil { return err }   // 只返回第一个错，但全部已退出
```

**Go 1.22 之后**循环变量已按轮隔离（第 8 章），`t := t` 可以删，但显式写出来对 reviewer 更友好。**`errgroup` 与 `WaitGroup` 的区别只有两点**：① 错误传播，② 首个错误自动取消。**别用**：需要「全部结果」或「任一成功即返回」时（那要自己写 `select` 汇聚，见第 1 章 1.7）。

### ⑧ Broker：pub/sub 扇出

```go
type Broker struct {
	mu   sync.RWMutex
	subs map[*Subscriber]struct{}
}
func (b *Broker) Publish(ev Event) {
	b.mu.RLock(); defer b.mu.RUnlock()
	for s := range b.subs {
		select {
		case s.ch <- ev:              // 有缓冲
		default:                       // 慢消费者：丢事件 or 断开，别阻塞发布者
			s.drop()
		}
	}
}
```

**判断**：broker 的难点永远不是「广播」，而是**慢消费者策略**（丢弃 / 阻塞 / 断开 / 落盘）与**退订时的关闭竞态**（往已关闭 channel 发送会 panic）。生产级做法是「发布者不持有订阅者 channel 的所有权」——退订时用 `sync.Once` 关，或干脆永不 close。

### 八模式速查

| 模式 | 解决什么 | 生态里的例子 | 误用症状 |
| --- | --- | --- | --- |
| Functional Options | 构造函数可选参数 | gin、eino `Option`、MCP `ServerOptions` | 必填项被藏进 Option，启动才报错 |
| Middleware | 横切关注点 | `http.Handler`、kratos、eino callback | 中间件里改共享状态、顺序依赖隐式 |
| Registry | 可插拔实现 | `database/sql` driver、eino 组件 | `init()` 满天飞，依赖图无法静态看清 |
| Actor | 并发安全的状态 | eino channelManager、single-writer 缓存 | 热读走 channel，性能崩 |
| Pipeline | 多阶段数据流 | SSE token 转发、ETL | 忘 close、忘 `ctx.Done()` → 泄漏 |
| Worker Pool | 有界并发 | LLM/embedding 批处理 | 池大小拍脑袋，无队列入参 `ctx` |
| errgroup | 子任务协作 | 并行工具调用 | 用它的 ctx 传给「不该被取消」的后台任务 |
| Broker | 一对多通知 | 会话广播、事件总线 | 慢消费者拖死发布者、close 后 panic |

---

## 51.5 Gin / Kratos / go-zero：三种装配哲学

这三个框架代表 Go 服务端的三条路线，**差别不在功能，而在「谁负责装配」**。

```text
Gin（库）            : 你自己写 main → New Engine → Use(mw) → 注册路由
Kratos（框架）        : 你写 Provider → wire 生成装配 → kratos.New(app) 管生命周期
go-zero（生成器）     : 你写 .api → goctl 生成 handler/logic/routes → rest.Server 跑
```

### 中间件签名对比（这是最本质的差异）

```go
// Gin：net/http 风格，靠 c.Next() / c.Abort() 控制链
r := gin.New()
r.Use(func(c *gin.Context) {
	start := time.Now()
	c.Next()                                  // 显式「执行后续」
	if c.IsAborted() { return }               // 被下游 Abort 了
	slog.Info("http", "path", c.FullPath(), "cost", time.Since(start))
})
r.GET("/v1/chat", func(c *gin.Context) { c.JSON(200, gin.H{"ok": true}) })
```

```go
// Kratos：传输无关的「业务中间件」，一套代码同时给 HTTP 与 gRPC
// 签名：func(middleware.Handler) middleware.Handler
httpSrv := khttp.NewServer(
	khttp.Address(":8000"),
	khttp.Middleware(
		recovery.Recovery(),   // 来自 middleware/recovery
		tracing.Server(),      // 来自 middleware/tracing
		// jwt.Server(...)     // 来自 middleware/auth/jwt
	),
)
app := kratos.New(kratos.Name("demo"), kratos.Server(httpSrv))
if err := app.Run(); err != nil { panic(err) }   // 生命周期由框架统一管：Start/Stop/信号
```

```go
// go-zero：装配代码是「生成」的，你只填 logic
// handler/routes 由 goctl 从 .api 生成，典型 main 只剩四行：
server := rest.MustNewServer(c.RestConf)
defer server.Stop()
handler.RegisterHandlers(server, svc.NewServiceContext(c))
server.Start()
// 中间件在 rest.Server 上挂（对之后注册的路由生效），参数校验在 handler 里 httpx.Parse
```

### 对照表

| 维度 | Gin | Kratos v2 | go-zero |
| --- | --- | --- | --- |
| 定位 | HTTP 库（最小内核） | 微服务框架（HTTP+gRPC+DI+配置） | 代码生成 + 全家桶 |
| 装配方式 | 手写 `main` | `wire` 编译期生成 + `kratos.New` 生命周期 | `goctl` 从 `.api` 生成 |
| 中间件签名 | `func(*gin.Context)` + `Next/Abort` | `func(Handler) Handler`，**传输无关** | `func(http.HandlerFunc) http.HandlerFunc` |
| 一套中间件跨协议 | ❌ | ✅（HTTP & gRPC 共用） | 部分（rest / zrpc 各一套） |
| 元数据/上下文 | `c.Set/Get`（类型不安全） | `ctx` + `metadata` 包（显式） | `ctx` + `httpx` 工具 |
| 学习曲线 | 极低 | 中（要懂 wire / 分层） | 中（要懂 goctl 与模板） |
| 适合 | 单体 API、要绝对可控 | 多协议微服务、团队统一治理 | 大量 CRUD 服务、追求一致性 |
| 要警惕 | 中间件顺序靠读代码、`gingonic` 生态版本碎片 | 抽象层多，小项目显得重 | 生成代码不可手改，边界场景要 fight 模板 |

**给你的判断标准**：**「装配复杂度」要与「团队规模 × 服务数量」匹配**。3 个人 2 个服务 → Gin + 手工装配（第 36 章）；20 个人 30 个服务 → Kratos/go-zero 的「强制一致性」才回本。**过早引入框架，等于把简单问题换成框架问题。**

---

## 51.6 踩坑清单（读码与用模式时）

| # | 现象 | 原因 | 正确做法 | 详见 |
| --- | --- | --- | --- | --- |
| 1 | 读了两天还是没有全局观 | 从细节切入，没有先建「三件套」坐标 | 先 README + `go doc` 认接口，再打穿一条链 | 51.2 |
| 2 | 改一处崩三处 | 没搞清「接口是谁定义的」 | 找 `type X interface` 的定义方，看方法集最小化程度 | 51.2 |
| 3 | 自己写的 Option 让零值不可用 | `Option` 里做了非幂等初始化 | `NewXxx` 内先填默认值，`Option` 只覆盖 | 51.4① |
| 4 | 中间件顺序错了导致鉴权被绕过 | 装饰器是**倒序**包裹，第一个最先执行、最后返回 | 写出 `Chain` 的注释说明顺序，并加集成测试 | 51.4② |
| 5 | `init()` 注册导致测试里驱动缺失 | import 副作用不显式 | 测试文件里 `_ "pkg/driver"`；或用显式 `Register` | 51.4③ |
| 6 | 微基准里模式开销巨大 | 每请求走 actor/channel 而非原子操作 | 按「写频率」选原语；先 profile 再优化 | 51.4④ |
| 7 | goroutine 数随 QPS 线性上涨 | pipeline 漏 `ctx.Done()` / 忘 close | 四个必守规则 + `goleak` 测试 | 51.4⑤ |
| 8 | 池子满了请求堆积但没人知道 | worker pool 无队列长度指标 | 暴露 `queue_len`、`in_flight` 指标（第 35 章） | 51.4⑥ |
| 9 | errgroup 的 ctx 传给了写库任务，结果被取消 | 后台任务继承了可取消的 ctx | 后台任务用 `context.WithoutCancel(ctx)`（1.21） | 51.4⑦ |
| 10 | 退订后进程 panic: send on closed channel | broker 中发布者与退订者竞态 | 发布者不 close；用 `sync.Once` + 引用计数 | 51.4⑧ |

---

## 51.7 把前 50 章织成一张网

### 四条生产线 + 三条粘合剂

```text
                       ┌─────────────────────────────────────────────┐
                       │  54 Java 架构师的 Go 落地清单（CR 手册）     │
                       └────────────────────▲────────────────────────┘
        ┌───────────────────────────────────┴───────────────────────┐
        │  53 综合项目（把 1~50 合成一个 3000 行框架）               │
        │  52 部署与 SRE（CGO=0 · distroless · K8s · 探针 · 优雅关停）│
        └───────────────────────────────────▲───────────────────────┘
                                            │
   ┌──────────────┬──────────────┬──────────┴─────────┬──────────────┐
   │ 🚀 高性能线   │ 🌐 服务端线   │ 🤖 Agent 线         │ ⚙️ 调度器线   │
   ├──────────────┼──────────────┼────────────────────┼──────────────┤
   │ 11 slice     │ 38 net/http  │ 40 LLM 客户端(SSE) │ 17 g/栈      │
   │ 12 map       │ 39 SSE/WS/gRPC│ 41 工具/并行/沙箱  │ 18 GMP 主循环 │
   │ 13 string    │ 35 可观测     │ 42 MCP（50 深读）   │ 19 抢占/netpoll│
   │ 14 布局/逃逸 │ 36 DI/装配    │ 43 循环/图编排(49)  │ 20 hchan     │
   │ 15 分配器     │ 34 测试       │ 44 RAG             │ 21 select    │
   │ 16 unsafe    │ 33 模块/MVS   │ 45 记忆/会话        │ 22 sync      │
   │ 22 sync 原语 │ 37 代码生成   │ 46 多 Agent/持久化  │ 23 内存模型   │
   │ 27 编译/PGO  │               │ 47 生产化/成本      │ 24 context   │
   │ 28 GC        │               │ 48 langchaingo     │ 25 并发陷阱   │
   │ 29 pprof     │               │                    │ 26 errgroup  │
   │ 30 调优      │               │                    │              │
   │ 31 JSON      │               │                    │              │
   │ 32 100k 连接 │               │                    │              │
   └──────┬───────┴───────┬──────┴─────────┬──────────┴──────┬───────┘
          │               │                │                 │
   ┌──────▼───────────────▼────────────────▼─────────────────▼───────┐
   │ 语言内核（4-10）：类型/接口 itab/泛型/error/defer/零值/组合注册表 │
   └────────────────────────────────┬───────────────────────────────┘
                                    │
   ┌────────────────────────────────▼───────────────────────────────┐
   │ 心智底座（1-3）：10 条价值观 · if err != nil 三层解释 · 无注解   │
   └────────────────────────────────────────────────────────────────┘
```

### 三条「粘合剂」（跨所有线的重复出现的三个主题）

| 粘合剂 | 出现章节 | 一句话规律 |
| --- | --- | --- |
| **`context` 是万能插座** | 19、24、39、40、42、47、50 | 任何「可能慢」的 API 第一个参数都是 ctx；取消必须能穿透到 syscall |
| **流是一等公民** | 13、31、39、40、43、49、50 | `io.Reader` / `StreamReader` / SSE 是同一个思想的三种规模；背压要自己设计 |
| **接口是唯一的缝** | 5、10、36、48、49、50、51 | 全书的框架都靠 2~5 个方法的接口组装；**接口由使用方定义** |

### 把知识串成三条「你可以这样讲给别人听」的故事线

1. **一次用户请求的一生**（适合面试讲）：`net/http` accept → goroutine（17/18）→ 中间件链（38/51）→ ctx 贯穿（24）→ LLM 流式（40）→ 工具并行（26/41）→ MCP 调用（42/50）→ 图编排与中断（43/49）→ SSE 上屏（39）→ 指标与 trace（35）→ 部署与探针（52）。
2. **一次 `go f()` 的一生**：`newproc`（17）→ runq（18）→ `schedule()`（18）→ 栈增长（17）→ 抢占（19）→ 阻塞在 channel（20）→ 被 netpoll 唤醒（19）→ 退出（17）。这条线读完，你就能手画 goroutine 状态机。
3. **一次内存分配的一生**：逃逸分析（14）→ `mcache` 拿 span（15）→ 对象头/对齐（14）→ 写屏障（28）→ 三色标记（28）→ scavenger 归还（15）。这条线读完，`GOMEMLIMIT` 调参就不是玄学了。

---

## 51.8 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 源码三件套 | 接口（接缝）· 装配（NewXxx + Option）· 执行（一个循环） |
| 读码七步 | README → `go doc` → 找接口 → 读测试 → 找装配 → 打穿一条链 → 写出取舍 |
| 必用工具 | `go doc -all` · `go doc -u` · `dlv` · `-gcflags=-m/-S` · `git log -S` · `$GOMODCACHE` |
| 万金油形状 | `func(T) T`（中间件）· `func(*T)`（Option）· `chan T`（边界） |
| 装配哲学 | Gin 手写 · Kratos wire 生成 · go-zero goctl 生成；**装配复杂度要匹配团队规模** |
| 并发三选一 | 有界并发 → errgroup/pool；状态所有权 → actor；广播 → broker |
| 铁律 | 谁创建谁关闭；阻塞必带 `ctx.Done()`；库不许 panic |
| 判断力 | 读完源码要能说出「它本可以怎么做，为什么没那样做」 |
| 知识网 | 四条生产线（高性能/服务端/Agent/调度器）+ 三条粘合剂（ctx/流/接口） |

---

## 51.9 自检题

1. **让你评估一个新框架（比如某个刚火起来的 Go Agent 框架），给你两个小时。你的读码计划是什么？产出物是什么？**
   > 要点：① 10 分钟 README + `doc.go` 弄清定位与不做什么；② `go doc -all` 列出导出符号，画出「接口 / 装配 / 执行」三件套；③ 读 2~3 个 `_test.go` 拿到最短正确用法；④ 用 `dlv` 或加日志打穿一条主链路（比如一次工具调用）；⑤ **产出物：一页纸的调用链图 + 三个「设计取舍判断」+ 两个「我会踩的坑」**。没有第三项就是白读。

2. **为什么 Go 的中间件几乎都是 `func(Handler) Handler` 而不是「注解 + 反射」？这对性能和可读性各自意味着什么？**
   > 要点：装配期把函数拼成树 → 运行时零开销、可内联（27）；调用链在 `main` 里可读、可断点；代价是没有「声明式配置」的便利，中间件顺序必须读代码。Java AOP 的运行期织入换来声明式，付的是代理对象与栈污染。

3. **你要给团队的 LLM 网关写一个「同一进程内按租户广播取消」的组件。用本章的哪些模式组合？为什么不用它之外的？**
   > 要点：**registry**（租户 → 组件实例）+ **broker**（广播事件给订阅者）+ **actor/single writer**（持有订阅集合，避免锁与 close 竞态）；取消本身用 `context.WithCancelCause`（24）把原因带出去。**不用** worker pool（这里没有「排队干活」的语义）、不用 pipeline（无多阶段）。判断标准是：**需求是「一对多通知」，不是「一对一执行」**。

---

## 51.10 延伸阅读

- 标准库必读三处：`runtime/proc.go` 的 `schedule()`、`net/http/server.go` 的 `conn.serve`、`context/context.go` 的 `cancelCtx.cancel`
- 模式源头：`database/sql/driver`（registry）、`net/http`（middleware）、`x/sync/errgroup`（结构化并发）、`golang.org/x/sync/semaphore`
- 装配对照：`gin-gonic/gin`（`engine.go` 的 `Use`/`handleHTTPRequest`）、`go-kratos/kratos`（`middleware/` + `transport/http`）、`zeromicro/go-zero`（`rest/engine.go` + `goctl` 模板）
- 深读样板：本书第 48/49/50 章（langchaingo / eino / MCP SDK）本身就是三份「调用链图 + 取舍判断」的范例
- 演讲：Rob Pike *Simplicity is Complicated*（讲的就是本章这套审美的出处）
- 下一章：[52. 生产部署与 SRE：把服务活到线上](52-deployment-sre.md)

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[50. MCP Go SDK 源码精读](50-mcp-sdk-source.md)；同组精读：[48. langchaingo](48-langchaingo-source.md) · [49. eino](49-eino-source.md)
- 相关：第 6 章（泛型约束了接口形状）、第 10 章（组合与注册表）、第 20/26 章（pipeline/pool/errgroup）、第 22 章（sync）、第 24 章（ctx）、第 36 章（三种 DI）、第 38 章（net/http 中间件）
- 下一章：[52. 生产部署与 SRE](52-deployment-sre.md)
