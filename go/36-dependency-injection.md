# 36. 依赖注入与装配：fx / wire / 手工

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 工程化与服务端 ｜ **编号**：36 / 54
> **本章定位**：Spring 程序员来到 Go，最先问的问题是「容器在哪」。答案是：**大部分项目根本没有容器，装配就是你亲手写的 `main()`**。本章讲清这个选择为什么成立，以及在项目大到一定程度后，`wire`（编译期）和 `fx`（运行期）各自救什么场。
> **读法**：36.2 的装配图 + 36.4 的对照表先看，36.5 的三份代码是可直接抄的骨架。

---

## 36.1 引子：那行写了 9 个参数的 `New`

项目长到第 8 个月，`main.go` 里出现了这样一行：

```go
svc := service.NewAgentService(cfg, repo, llm, toolReg, cache, logger, tracer, metrics, limiter)
```

你盯着它，脑子里是 Spring 里那句 `@Autowired private AgentService svc;`——一行注解，什么都不用管。于是你干了第一件蠢事：把 `Config` 全局化，把 `logger` 塞进 `context`，把 `repo` 写成包级单例。三个月后，`internal/service` 单测跑不起来，因为它偷偷 import 了 `internal/db` 的全局连接池，而那个池在 `init()` 里就去连数据库了。

第二件蠢事：你引了 `uber/fx`，给每个类型加上 `fx.Provide`，然后发现**服务启动要 300ms，而且一处类型不匹配的错在启动时才 panic**——这个错，编译器本来能抓。

第三件蠢事：你搞不清 `OnStop` 到底按什么顺序执行，于是数据库连接池在 HTTP 服务器还活着的时候被关掉了，发布窗口里出现了 200 个 `sql: database is closed`。

这三件事指向同一个问题：**Go 没有「对象的生命周期」这个概念，所有生命周期都是你显式写出来的。** 你得学会这套写法，或者选一个工具帮你写——但你得知道它在替你做哪一部分。

---

## 36.2 直觉模型：装配图就是一个函数调用树

Spring 的容器是一台**运行期的图解析机**：扫描、注册、拓扑排序、反射注入、代理织入。Go 的等价物是一段**编译期的函数调用树**：

```
func main() {
    cfg    := config.Load()                    ──┐
    logger := obs.NewLogger(os.Stdout)           │
    tp, shutdown := obs.SetupOTel(ctx)           │ 每一行就是
    db     := pgxpool.New(ctx, cfg.DB.URL)       │ 一个 bean
    repo   := store.NewUserRepo(db)              │
    llm    := llm.NewOpenAI(cfg.LLM, httpClient) │
    tools  := tool.NewRegistry(searchTool, calc) │
    svc    := service.NewAgentService(repo, llm, tools, logger, tracer) ──┘
    api    := httpapi.New(svc, logger, tracer)
    srv    := &http.Server{Addr: cfg.HTTP.Addr, Handler: api}

    // 生命周期：手写，一进一出
    go srv.ListenAndServe()
    <-ctx.Done()
    srv.Shutdown(shutdownCtx)
    db.Close()
    shutdown(context.Background())
}
```

```
                          ┌──────────┐
                     ┌───▶│ config   │
                     │    └────┬─────┘
                     │         ▼
   ┌────────┐   ┌──────────┐   ┌──────────┐   ┌──────────────┐
   │ logger │◀──│ pgxpool  │◀──│  repo    │◀──│ AgentService │◀── HTTP handler
   └────────┘   └──────────┘   └──────────┘   └──────┬───────┘
                                                      │
    ┌──────┐   ┌───────────┐   ┌────────────┐        │
    │ llm  │◀──│ tool.Reg  │◀──│  registry  │◀───────┘
    └──────┘   └───────────┘   └────────────┘
     （箭头 = 「被谁依赖」；谁被依赖谁先构造）
```

这就是全部。**没有容器，没有反射，没有代理**——`go vet` 和编译器帮你检查这张图（漏传参数 = 编译不通过；类型不匹配 = 编译不通过）。这就是 Go 社区说「手工装配优先」的底气：**你要的那部分能力，编译器已经给了。**

那 `wire` 和 `fx` 干什么？把这张图**写出来**的活，可以交给机器：

| 需求 | 手工 | `wire`（google/wire） | `fx`（uber/fx） |
| --- | --- | --- | --- |
| 图解析时机 | 人类大脑，编译期 | 编译期生成代码 | 运行期反射 |
| 漏依赖 | 编译失败 | 生成失败（`wire: no provider found`） | 启动 panic |
| 运行时开销 | 0 | 0（就是普通代码） | 反射 + 类型映射，启动百毫秒级 |
| 天生擅长 | 任何规模 | 中大型、provider 稳定 | 插件化、需要「按需注入集合」 |
| 生态 | — | kratos、很多开源库的 cmd 层 | uber 系、go.uber.org 全家桶 |

---

## 36.3 底层实现：三种装配方式的真实机制

### 36.3.1 构造函数注入 = 函数调用

`NewXxx(deps...)` 是 Go 里唯一正规的注入方式。**你们的「依赖注入框架」就是这个函数签名**。它有三个硬性约定：

1. **构造函数返回具体类型**（`*pgRepo`），不是接口。
2. **接受方在需要的地方定义接口**（`store.UserRepo`），提供方无感。
3. **接口满足关系用编译期断言固定**：`var _ UserRepo = (*pgRepo)(nil)`。

> 💬 **关于「构造函数返回接口」**：很多 Java 背景的人会这样做。Go 社区的主流结论是反的：**accept interfaces, return structs**。原因很实在——返回接口会让调用方拿不到具体类型的方法（比如你想调 `pgRepo.Ping`），会引入一层无价值的装箱（详见 [第 5 章](5-interface-internals.md)），而「隐藏实现」这个收益在 Go 里已经由「小写结构体 + 不导出」实现了。
> **什么时候该返回接口**：① 这个包对外只暴露一个能力，且你有多个实现按配置切换（`llm.New(cfg) (ChatModel, error)`——此时返回接口是**对的**，因为调用方不该知道是 OpenAI 还是 Ollama）；② 需要返回 `(T, cleanup func(), error)` 这种带生命周期的东西。

### 36.3.2 functional options：为什么 Go 不用 Builder 和重载

Java 里参数一多就上 Builder。Go 没有重载、没有默认参数、没有命名参数，于是有了 functional options：

```go
type Option func(*options) // 变体：type Option interface{ apply(*options) }

type options struct {
	timeout   time.Duration
	maxSteps  int
	temperature float64
	transport http.RoundTripper
}

// 零值即默认：不传就用这里
func defaultOptions() options {
	return options{timeout: 30 * time.Second, maxSteps: 8, temperature: 0.7, transport: http.DefaultTransport}
}

func WithTimeout(d time.Duration) Option  { return func(o *options) { o.timeout = d } }
func WithMaxSteps(n int) Option           { return func(o *options) { o.maxSteps = n } }
func WithTransport(rt http.RoundTripper) Option { return func(o *options) { o.transport = rt } }

type Client struct {
	opts options
	http *http.Client
}

// 关键：Option 在前，必填参数在后，且返回具体类型
func NewClient(apiKey string, opts ...Option) (*Client, error) {
	o := defaultOptions()
	for _, opt := range opts {
		opt(&o)
	}
	return &Client{opts: o, http: &http.Client{Timeout: o.timeout, Transport: o.transport}}, nil
}
```

**它的成本是什么？** 每次 `New` 构造一个闭包 + 一次函数调用（可内联），可忽略。它的问题是**无法在运行期校验「这个 option 不能和那个 option 一起用」**——要校验就在 `New` 里写 `if`，返回 error。`gRPC`、`otel`、`pq`、`zap` 全是这个模式。

一个更严谨的变体用接口，好处是能区分「设置值」和「应用在之后还要二次处理的值」：

```go
type Option interface{ apply(*options) error }
type optionFunc func(*options) error
func (f optionFunc) apply(o *options) error { return f(o) }
```

### 36.3.3 wire：把装配代码「生成」出来

`wire` 的核心思路：**你只描述「谁能构造谁」，生成器负责把 36.2 那张图的代码写出来。** 产物是普通的、可读的 Go 代码——这意味着零反射、零启动开销、可 debug。

```go
// internal/wire/wire.go —— 只有 build tag 为 wireinject 时才编译
//go:build wireinject
// +build wireinject

package wire

import (
	"github.com/google/wire"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/acme/agent/internal/config"
	"github.com/acme/agent/internal/httpapi"
	"github.com/acme/agent/internal/llm"
	"github.com/acme/agent/internal/service"
	"github.com/acme/agent/internal/store"
)

// ProviderSet：一组「构造函数」的集合，可跨包组合
var StoreSet = wire.NewSet(
	store.NewUserRepo,
	wire.Bind(new(store.UserRepo), new(*store.pgRepo)), // 把实现绑到接口
)

var AgentSet = wire.NewSet(
	llm.NewClient,
	service.NewAgentService,
	httpapi.New,
)

// Injector：wire 读取这个函数的签名和返回类型，生成函数体
func InitApp(cfg config.Config) (*httpapi.API, func(), error) {
	panic(wire.Build(StoreSet, AgentSet, ProvideDB, ProvideLogger))
}

func ProvideDB(cfg config.Config) (*pgxpool.Pool, func(), error) { ... }
```

`wire` 生成 `wire_gen.go`，长这样（这就是 36.2 那张图的代码化）：

```go
//go:build !wireinject

func InitApp(cfg config.Config) (*httpapi.API, func(), error) {
	pool, cleanup, err := ProvideDB(cfg)
	if err != nil {
		return nil, nil, err
	}
	userRepo := store.NewUserRepo(pool)
	client, err := llm.NewClient(cfg.LLM)
	svc := service.NewAgentService(userRepo, client)
	api := httpapi.New(svc)
	return api, func() { cleanup() }, nil
}
```

`wire` 里最值钱的三个工具函数：

- `wire.NewSet(...)`：provider 集合，可嵌套组合（`wire.Build(A, B, CfgSet)`）。
- `wire.Bind(new(Iface), new(*Impl))`：**接口绑定**，这是「返回具体类型 + 使用方要接口」的粘合剂。
- `wire.Struct(new(Server), "*")` / `wire.FieldsOf(new(Config), "DB", "Redis")`：前者自动把 struct 的所有字段当依赖填进去，后者把配置结构体的字段拆成独立依赖。

一个必须知道的细节：`wire.Build` 只能在 injector 函数体里 `panic(wire.Build(...))` 出现；`wire` 跑一遍会检查「是否存在一条从输入到输出的构造链」，找不到就报 `no provider found for X`——**这就是编译期 DI 的全部魔法，没有魔法。**

### 36.3.4 fx：运行期容器 + 生命周期

`fx` 是 `dig`（反射 DI 容器）之上的一层，加了两个东西：**模块化**（`fx.Module`）和**生命周期**（`fx.Lifecycle`）。

```go
func main() {
	app := fx.New(
		fx.Provide(
			config.Load,               // func() (Config, error)
			obs.NewLogger,
			ProvideDB,
			llm.NewClient,
			service.NewAgentService,
			fx.Annotate(httpapi.New, fx.As(new(http.Handler))), // 用接口类型注册
		),
		fx.Module("http",
			fx.Provide(NewHTTPServer),
			fx.Invoke(RegisterRoutes, StartServer), // Invoke 表示「我要用它，不需要返回值」
		),
		fx.NopLogger, // 或者换成 slog 适配的 fx.Logger
	)
	app.Run() // = Start + 等 SIGINT/SIGTERM + Stop
}
```

`dig` 的注入靠 tag 描述复杂依赖：

```go
type handlerParams struct {
	dig.In
	Log      *slog.Logger
	Repos    []Repository `group:"repos"`   // 注入一组
	Optional *Cache       `optional:"true"` // 没有也能启动
	Name     string       `name:"appName"`  // 按名字区分同类型
}
```

生命周期的写法（这是 fx 真正的杀手功能）：

```go
func NewHTTPServer(lc fx.Lifecycle, h http.Handler, log *slog.Logger) *http.Server {
	srv := &http.Server{Addr: ":8080", Handler: h, ReadHeaderTimeout: 5 * time.Second}
	lc.Append(fx.Hook{
		OnStart: func(ctx context.Context) error {
			ln, err := net.Listen("tcp", srv.Addr)
			if err != nil {
				return err // 返回 error → fx 回滚已启动的 hook
			}
			go func() {
				if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
					log.Error("http serve", "err", err)
				}
			}()
			return nil
		},
		OnStop: func(ctx context.Context) error {
			return srv.Shutdown(ctx) // ctx 受 fx.StopTimeout 约束
		},
	})
	return srv
}
```

顺序规则（**记住方向相反**）：`OnStart` 按 Append 顺序执行，`OnStop` **逆序**执行——所以先构造的（DB）最后关闭，先关闭 HTTP，再关 DB。这正是你在 36.1 里踩的那个坑的解药。启动失败时会回滚（把已启动的 hook 逐个 Stop）；单个 hook 的超时受 `fx.StartTimeout`/`fx.StopTimeout`（默认 15s）约束，具体行为以你本地 fx 版本为准。

### 36.3.5 kratos 的装配层

kratos 把「服务」抽象成可 Start/Stop 的单元，装配层非常薄：

```go
import (
	kratos "github.com/go-kratos/kratos/v2"
	"github.com/go-kratos/kratos/v2/transport/grpc"
	"github.com/go-kratos/kratos/v2/transport/http"
)

app := kratos.New(
	kratos.Name("agent"),
	kratos.Version("v0.3.1"),
	kratos.Server(httpSrv, grpcSrv), // 两个 transport 一起管
)
if err := app.Run(); err != nil {
	panic(err)
}
```

`app.Run()` 内部做的是：注册信号处理 → 依次 `Start` 所有 server → 阻塞 → 收到信号后逆序 `Stop`。**它替你写的就是 36.1 里那 6 行手写生命周期。**（`transport.Server` 接口就是 `Start(context.Context) error` + `Stop(context.Context) error` 两个方法。）

---

## 36.4 Java 对照：Spring 的哪些能力 Go 不需要，哪些必须自己补

| Spring | Go 等价物 | 差异与原因 |
| --- | --- | --- |
| `@Component` / `@Service` | 一个 `NewXxx` 构造函数 | 注册是「被调用」而不是「被扫描」；没有包扫描，只有 import |
| `@Autowired` | 构造函数参数 | 编译期检查；不存在「注入失败只能启动时报」 |
| `@Bean` + `@Configuration` | `wire.NewSet` / `fx.Provide` | 两者都是「声明式」的，但 wire 在编译期、fx 在运行期 |
| `@Qualifier` / `@Primary` | `fx.Annotate(..., fx.ResultTags(\`name:"x"\`))` / wire 里写两个不同 provider | Go 里更常见的是「换个类型」，不要靠名字 |
| `@PostConstruct` / `@PreDestroy` | `fx.Hook{OnStart, OnStop}` / kratos `Server.Start/Stop` / 手写 | 生命周期必须显式注册，没有隐式回调 |
| `SmartLifecycle` + `@Order` | Append 顺序（Stop 逆序） | 排序靠代码顺序，不靠注解 |
| `ApplicationContext#getBean` | **没有**（永远别这么干） | 服务定位器是 Go 里的反模式，会毁掉可测性 |
| `prototype` / `singleton` 作用域 | 没有内建作用域 | 通常全进程单例；要「每请求一个」就在 handler 里手写构造 |
| 循环依赖（setter 注入容忍） | **天然不可能** | 构造函数注入要求依赖先于被依赖存在，环 = 无解 = 好事 |
| AOP / `@Transactional` | 中间件、装饰器、显式 `tx` 参数 | 详见 [第 10 章](10-composition-plugin-registry.md) |
| `@ConditionalOnProperty` | 在 `main` 里 `if cfg.Feature.EnableX {}` | 条件装配用 `if` 写，比注解可读 |

> 💡 **一句话总结**：Spring 帮你解决的是「**大团队里谁都不认识谁**」的装配问题；Go 假设的是「**一个服务就几十个类型，且你每天都看得见它们**」。当你的 Go 项目真的长到 200 个 provider 时，`wire` 的收益就开始出现了——它不是因为「注解好看」，而是因为**手工维护一条 200 行的构造链会出错**。

---

## 36.5 工程落地：三份骨架

### 36.5.1 小而美的手工装配（推荐给 90% 的项目）

```go
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

func main() {
	if err := run(); err != nil {
		slog.Error("fatal", "err", err)
		os.Exit(1)
	}
}

func run() error {
	// 1) 信号在最早的入口注册，ctx 贯穿全链路
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	cfg, err := config.Load() // Config 由 env 装配，见第 52 章
	if err != nil {
		return err
	}
	log := obs.NewLogger(os.Stdout, slog.LevelInfo)

	tpShutdown, err := obs.SetupOTel(ctx, cfg.OTel.Endpoint, "agent")
	if err != nil {
		return err
	}

	pool, err := pgxpool.New(ctx, cfg.DB.URL)
	if err != nil {
		return err
	}
	defer pool.Close() // 后构造先释放，与 HTTP 的顺序正确

	repo := store.NewUserRepo(pool)
	llmClient, err := llm.NewClient(cfg.LLM, llm.WithTimeout(30*time.Second))
	if err != nil {
		return err
	}
	svc := service.NewAgentService(repo, llmClient, tool.NewRegistry(tools...), log)
	api := httpapi.New(svc, log)

	srv := &http.Server{
		Addr:              cfg.HTTP.Addr,
		Handler:           api,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      0,  // 0 = 不限，SSE 需要（见第 38 章）
		IdleTimeout:       120 * time.Second,
	}

	errCh := make(chan error, 1)
	go func() {
		log.Info("http listen", "addr", srv.Addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- err
		}
	}()

	select {
	case err := <-errCh:
		return err
	case <-ctx.Done():
	}

	// 2) 优雅关停：先停止接受新连接，再等在途请求，最后收尾导出
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Error("http shutdown", "err", err)
	}
	if err := tpShutdown(shutdownCtx); err != nil {
		log.Error("otel shutdown", "err", err)
	}
	return nil
}
```

### 36.5.2 wire 的 provider set + injector

```go
// internal/ioc/ioc.go（wire 指令集中在包注释里，PR 时好审）
package ioc

//go:generate wire

import (
	"github.com/google/wire"
	"github.com/acme/agent/internal/config"
	"github.com/acme/agent/internal/httpapi"
	"github.com/acme/agent/internal/llm"
	"github.com/acme/agent/internal/service"
	"github.com/acme/agent/internal/store"
)

// StoreSet 提供「数据库 + 仓储」，接口绑定显式写出
var StoreSet = wire.NewSet(
	NewPool,
	store.NewUserRepo,
	wire.Bind(new(store.UserRepo), new(*store.UserRepoPG)),
)

// CoreSet 提供「LLM + 服务层」
var CoreSet = wire.NewSet(llm.NewClient, service.NewAgentService)

// AllSet 是给 cmd 用的总集合
var AllSet = wire.NewSet(StoreSet, CoreSet, httpapi.New)
```

```go
// cmd/agent/wire.go
//go:build wireinject

package main

import (
	"github.com/google/wire"
	"github.com/acme/agent/internal/config"
	"github.com/acme/agent/internal/httpapi"
	"github.com/acme/agent/internal/ioc"
)

func InitAPI(cfg config.Config) (*httpapi.API, func(), error) {
	panic(wire.Build(ioc.AllSet))
}
```

CI 里的纪律（非常重要）：**生成物必须提交，并在 CI 里验证它没漂**。

```yaml
- run: go generate ./...
- run: git diff --exit-code -- '*wire_gen.go' '*mock_*.go'
```

### 36.5.3 fx + 生命周期（推荐给「插件化 + 多 server」的服务）

```go
func main() {
	fx.New(
		fx.Provide(
			config.Load,
			obs.NewLogger,
			ProvidePool,
			llm.NewClient,
			store.NewUserRepo,
			service.NewAgentService,
			httpapi.NewHTTPHandler,
		),
		fx.Provide(fx.Annotate(api.NewEventStream, fx.As(new(api.Streamer)))), // 集合式注入
		fx.Module("http",
			fx.Provide(NewServer),
			fx.Invoke(func(*http.Server) {}), // 只是为了让 Server 被实例化
		),
		fx.Invoke(RegisterMetrics),
	).Run()
}

func ProvidePool(lc fx.Lifecycle, cfg config.Config) (*pgxpool.Pool, error) {
	pool, err := pgxpool.New(context.Background(), cfg.DB.URL)
	if err != nil {
		return nil, err
	}
	lc.Append(fx.Hook{OnStop: func(ctx context.Context) error {
		pool.Close() // Stop 阶段逆序执行：HTTP 先关，DB 后关
		return nil
	}})
	return pool, nil
}
```

---

## 36.6 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| 单测跑不起来，必须连数据库 | 包级单例在 `init()` 里建连接 | 依赖从 `main` 传进来，包内不建全局资源 | 36.5.1 |
| `New` 参数涨到 9 个 | 没拆分职责，或把「全局设施」也当依赖传 | 先拆服务；logger/tracer 用 `WithLogger` option；确实需要就给参数起个 struct（`type Deps struct`） | 36.3.2 |
| fx 启动 panic：`missing type: *store.UserRepo` | `Provide` 里注册的是具体类型，注入点要接口 | `fx.Annotate(New, fx.As(new(Iface)))` 或 `wire.Bind` | 36.3.4 |
| 循环依赖，wire 报 `cycle` | 两个包互相需要 | 抽第三个包或改成事件/回调；Go 不允许环，这是特性 | 36.4 |
| `OnStop` 里 DB 先于 HTTP 关闭 | 没理解「Stop 逆序」 | 保证构造顺序：DB 先 Provide，HTTP 后 Provide | 36.3.4 |
| 优雅关停时请求 502 | `Shutdown` 超时设得太短 / 没等 `Shutdown` 返回 | 超时 > P99 请求时长；`Shutdown` 返回后再退出进程 | 36.5.1 |
| 构造函数返回 nil 接口，`err == nil` 但用起来 panic | typed nil（详见第 5 章） | 出错时 `return nil, err`，绝不 `var p *T; return p, nil` | 第 5 章 |
| 只在 `wire_gen.go` 里改代码，下次生成被覆盖 | 生成物是产物不是源码 | 改 provider/injector，重新 `go generate` | 36.5.2 |
| 到处 `container.Get(...)` 式取依赖 | 服务定位器反模式 | 依赖从构造函数进来，需要什么在参数里写清楚 | 36.4 |
| fx 让启动慢了 300ms+ | 反射构造 + 大量 provider | 冷启动敏感（CLI、Serverless）用 wire 或手工 | 36.2 |
| `fx.Invoke` 里做重活，启动阻塞 | Invoke 是同步的 | 只做注册；真正的启动（listen）放 `OnStart` | 36.3.4 |

---

## 36.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| Go 的默认答案 | 手工装配在 `main()`，编译器帮你检查依赖图 |
| 构造函数 | `func NewX(deps) (*X, error)`，返回具体类型 |
| 接口位置 | 在使用方定义，提供方用 `var _ Iface = (*impl)(nil)` 断言 |
| functional options | `type Option func(*options)`，零值即默认，必填参数放最后 |
| 何时返回接口 | 多实现按配置切换、或返回 `(T, cleanup, error)` 时 |
| wire | 编译期生成装配代码，零反射；`NewSet`/`Bind`/`Struct`/`FieldsOf` |
| fx | 运行期反射容器 + `fx.Module` + `fx.Lifecycle` |
| 生命周期 | `OnStart` 顺序、`OnStop` 逆序；失败会回滚 |
| kratos | `kratos.New(kratos.Server(...))` + `app.Run()`，就是替你写生命周期 |
| 反模式 | 全局单例、`init()` 建资源、Service Locator、把 logger 塞 ctx |
| CR 红线 | 生成物必须提交且 CI 校验 `git diff --exit-code` |

## 36.8 自检题

1. **你的 `NewAgentService` 需要 logger、tracer、metrics 三个「横切设施」，每次新增都要改所有构造函数签名。Go 里怎么优雅解决？**
   > 答案要点：① 用 functional options（`WithLogger`/`WithTracer`）让它们是可选项；② 或者把横切设施打包成一个 `obs.Telemetry` 结构体一次注入；③ 最根本的判断是——如果一个依赖「几乎所有服务都需要」，那它更像基础设施而不是业务依赖，考虑通过中间件/装饰器在边界处使用，而不是往下穿（详见第 10 章）。

2. **为什么 Go 的构造函数注入「天然禁止循环依赖」，而这不被认为是缺陷？**
   > 答案要点：构造函数要求依赖对象先于被依赖对象存在，A 依赖 B、B 依赖 A 就无法排出构造顺序，wire 直接报 cycle，fx 运行期报错。循环依赖通常意味着职责边界错了（两个概念实际是一个，或需要第三个协调者/事件解耦）；在编译期暴露比 Spring setter 注入「能跑但行为微妙」更好。

3. **wire 与 fx 都能做 DI，你怎么选？给三个判断依据。**
   > 答案要点：① 启动延迟敏感（CLI、Lambda、边缘）→ wire/手工；② 需要「注入一组实现」（registry、多 handler、plugin 列表）→ fx 的 group 更省事；③ 团队对启动期 panic 的容忍度与技术栈（已有 uber 生态）→ fx。总体上 provider 数量 < 100 时手工即可，超过后 wire 的编译期检查收益最大。

## 36.9 延伸阅读

- `github.com/google/wire`：`wire.go` 里的 `Build`/`NewSet`/`Bind`/`Struct`/`FieldsOf` 文档与 `internal/wire` 的图解析实现
- `go.uber.org/fx`：`App.Run`/`Lifecycle.Start/Stop`（读一遍 `lifecycle.go` 就懂回滚语义）、`Annotate`/`As`/`Module`
- `go.uber.org/dig`：`dig.In`/`dig.Out`/`group`/`optional`/`name` 的反射解析
- `github.com/go-kratos/kratos/v2`：`app.New`/`app.Run` 与 `transport.Server` 接口
- Dave Cheney, *Functional options for friendly APIs*（2014）——这个模式的原始出处
- 交叉引用：[第 10 章](10-composition-plugin-registry.md) 组合与注册表；[第 38 章](38-net-http.md) 的 `Server.Shutdown` 细节

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 35 章](35-observability.md) 可观测性：slog、OTel、Prometheus
- 下一章：[第 37 章](37-codegen-reflection.md) 代码生成与反射：schema 自动化
