# 10. 组合优于继承：插件化与注册表设计

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · 语言内核 ｜ **编号**：10 / 54
> **本章定位**：Go 没有 `extends`，那"扩展点"到底怎么留？本章给出三件套：**小接口 + 注册表/工厂 + 装饰器链**，最后落成一个可插拔的 Tool/Provider 注册系统。
> **读法**：10.3 / 10.4 / 10.7 是可直接抄进项目的代码。10.6 是一张"别再给 Go 装 OSGi"的劝退表。

---

## 10.1 引子：一次失败的"框架移植"

你把 Java 项目里那套 `AbstractToolHandler` 搬了过来，因为它太好用了：

```java
// Java：模板方法模式，父类定义骨架，子类只填两个钩子
public abstract class AbstractToolHandler {
    public final ToolResult execute(Args args) {
        validate(args);              // 父类固定流程
        ToolResult r = doExecute(args);
        audit(args, r);              // 父类固定流程
        return r;
    }
    protected abstract void validate(Args args);
    protected abstract ToolResult doExecute(Args args);
}
```

Go 里你照着写：

```go
type BaseTool struct{ name string }

func (b *BaseTool) Call(args Args) Result {
	b.validate(args)      // ❌ 永远调用 BaseTool.validate，不会调到子类的
	r := b.doExecute(args) // ❌ 同理
	b.audit(args, r)
	return r
}
func (b *BaseTool) validate(args Args)  {}
func (b *BaseTool) doExecute(args Args) Result { return Result{} }

type WeatherTool struct{ BaseTool }
func (t *WeatherTool) doExecute(args Args) Result { return Result{Text: "sunny"} }
```

`WeatherTool{}.Call(args)` 返回空结果——因为 Go **没有虚方法表**，`BaseTool` 里的 `b.validate` 是静态绑定的直接调用，`b` 的静态类型就是 `*BaseTool`，它根本不知道外面套了个 `WeatherTool`（第 4 章已论证）。

这不是语法缺陷，而是**设计信号**：Go 在告诉你"别用继承表达流程复用"。你要的"固定骨架 + 可变步骤"，在 Go 里叫**装饰器**（或者叫高阶函数）。

---

## 10.2 直觉模型：从"纵向继承树"到"横向装配"

```
Java 的扩展方式                           Go 的扩展方式

   AbstractTool                              Tool (interface, 1~3 方法)
   ├── AbstractHTTPTool                        ▲        ▲          ▲
   │     ├── WeatherTool        继承树          │        │          │
   │     └── StockTool        （纵向，编译期定死）│        │          │
   └── AbstractDBTool                          │        │          │
         └── SqlTool                    weatherTool  stockTool  mockTool
                                        （互不相识，各自独立实现接口）

   扩展新维度（加审计/加限流/加重试）？       扩展新维度？
   → 再开一棵继承树，或者改父类            → 套一层装饰器，不动任何已有类型
```

三条设计法则，替换掉 Java 的"继承 + 模板方法 + 抽象类"：

| Java 的做法 | Go 的对应物 | 关键差异 |
| --- | --- | --- |
| 抽象类 + 模板方法 | **装饰器（高阶函数）** | 流程在外层，被装饰者无感知 |
| `implements` 多态 | **隐式接口 + 依赖倒置** | 接口定义在使用方，1~3 个方法 |
| `ServiceLoader` / Spring `@Component` | **显式注册表 + 工厂** | 注册是代码，不是魔法；可测、可审计 |
| 运行时插件（OSGi） | **编译期内置 / 子进程 / wasm** | Go 的 `plugin` 包基本不能用（见 10.6） |
| `instanceof` 判能力 | **类型断言探测能力接口** | `if f, ok := w.(http.Flusher); ok` |

---

## 10.3 注册表 + 工厂：一张 map 搞定 90% 的扩展点

核心就一句话：**把"类型名 → 构造函数的映射"做成显式数据结构**。

```go
package registry

import (
	"fmt"
	"slices"
	"sync"
)

// 泛型注册表：T 是被注册的接口类型，C 是配置类型
type Registry[T any, C any] struct {
	mu        sync.RWMutex
	factories map[string]func(C) (T, error)
}

func New[T any, C any]() *Registry[T, C] {
	return &Registry[T, C]{factories: make(map[string]func(C) (T, error))}
}

// Register 重复注册直接报错：静默覆盖是线上最难查的事故之一
func (r *Registry[T, C]) Register(name string, f func(C) (T, error)) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, dup := r.factories[name]; dup {
		return fmt.Errorf("registry: %q already registered", name)
	}
	r.factories[name] = f
	return nil
}

func (r *Registry[T, C]) Build(name string, cfg C) (T, error) {
	r.mu.RLock()
	f, ok := r.factories[name]
	r.mu.RUnlock()
	if !ok {
		var zero T
		return zero, fmt.Errorf("registry: unknown %q (have %v)", name, r.Names())
	}
	return f(cfg)
}

// Names 返回稳定排序的名字列表——LLM 的 tool 列表顺序必须稳定，
// 否则 prompt cache 全部失效（顺序一变，前缀就变了）
func (r *Registry[T, C]) Names() []string {
	r.mu.RLock()
	out := make([]string, 0, len(r.factories))
	for k := range r.factories {
		out = append(out, k)
	}
	r.mu.RUnlock()
	slices.Sort(out)
	return out
}
```

用法：

```go
var Models = registry.New[Model, ProviderConfig]()

func init() { // 或 main() / fx 的 module 里显式调用，见下
	_ = Models.Register("openai", newOpenAI)
	_ = Models.Register("ollama", newOllama)
}

m, err := Models.Build(cfg.Provider, cfg) // cfg.Provider = "openai"
```

四个别踩的点：

1. **`Register` 在 `init()` 里很常见，但优先显式调用**。`init` 注册意味着"import 了才有"（副作用导入），单测里想注入 mock 就得跟全局状态搏斗。推荐：`Register` 由 `main` 或装配层显式调（第 36 章的 fx/wire 就是干这个的）。
2. **重复注册要报错，不要覆盖**。这条能救你一次"两个同名 Tool 互相顶掉"的排查夜。
3. **`Names()` 必须排序**。工具列表进 prompt 时顺序不稳定 = prompt 缓存命中率归零 = 成本翻倍。
4. **注册表要能导出**（`GET /internal/registry`）——线上事故第一个问题永远是"这个 Tool 到底注册上了没"。

---

## 10.4 装饰器 / 中间件链：Go 唯一的"切面"

Go 里说"中间件"，本质就是**一个把 A 变成 A 的高阶函数**：

```go
// 任何"输入 T 输出 T"的能力，都能被套娃
type Middleware func(Model) Model

func Chain(m Model, mws ...Middleware) Model {
	// 逆序套：mws[0] 在最外层，最先被调用
	for i := len(mws) - 1; i >= 0; i-- {
		m = mws[i](m)
	}
	return m
}

// 一个真实的装饰器：重试 + 计时 + 日志，全都不改被装饰者的代码
func WithRetry(max int) Middleware {
	return func(next Model) Model {
		return ModelFunc(func(ctx context.Context, req Request) (Response, error) {
			var lastErr error
			for i := 0; i < max; i++ {
				resp, err := next.Generate(ctx, req)
				if err == nil || isFatal(err) { // 分类器来自第 7 章
					return resp, err
				}
				lastErr = err
			}
			return Response{}, lastErr
		})
	}
}
```

`ModelFunc` 是"把函数适配成接口"的经典技巧（标准库同款：`http.HandlerFunc`、`HandlerFunc.ServeHTTP`）：

```go
type ModelFunc func(context.Context, Request) (Response, error)

func (f ModelFunc) Generate(ctx context.Context, req Request) (Response, error) {
	return f(ctx, req)
}
```

于是装配变成一行：

```go
m := Chain(openaiModel,
	WithMetrics(),          // 最外层：观测一切
	WithLogging(),
	WithRetry(3),
	WithRateLimit(bucket),  // 最内层：贴着真实调用
)
```

```
调用顺序（洋葱模型）：
  请求 →  Metrics → Logging → Retry → RateLimit → OpenAI
  响应 ←  Metrics ← Logging ← Retry ← RateLimit ← OpenAI
```

**Gin 的中间件是完全一样的结构，只是它用 `c.Next()` 显式控制"下一个"**：

```go
func Auth() gin.HandlerFunc {
	return func(c *gin.Context) {
		if !check(c) {
			c.AbortWithStatusJSON(401, gin.H{"error": "unauthorized"})
			return // Abort + return：不再往下走
		}
		c.Next() // 执行后续 handler，返回后可继续收尾（相当于"环绕"）
	}
}
```

`gin.Context` 内部就是 `handlers Chain`（`type HandlersChain []HandlerFunc`）+ 一个 `index` 游标，`Next()` 是 `for c.index < len(c.handlers) { c.index++; c.handlers[c.index]() }` 的循环——和 10.4 的 `Chain` 语义等价，只是把"递归嵌套"换成了"切片 + 游标"（省栈、能在中间 `Abort`）。

---

## 10.5 依赖倒置：接口定义在使用方 + 能力探测

Go 的接口是**结构性**的（第 5 章），所以"依赖倒置"不需要任何框架：

```go
// ❌ provider 侧定义大接口：所有实现都被迫跟上它的演化
type Storage interface {
	Get(ctx, key) ([]byte, error)
	Put(ctx, key, val) error
	Delete(ctx, key) error
	List(ctx, prefix) ([]string, error)
	Close() error
}

// ✅ consumer 侧只声明自己用得到的那一点
type getter interface {
	Get(context.Context, string) ([]byte, error)
}

func LoadPrompt(ctx context.Context, g getter, name string) (string, error) {
	b, err := g.Get(ctx, "prompt/"+name)
	...
}
```

**能力探测**是这套设计的高级玩法：不要为了"可能支持 Flush"就把 `Flush()` 塞进主接口，而是运行时探测。

```go
if f, ok := w.(http.Flusher); ok { // 只在真的支持时才 flush
	f.Flush()
}
```

标准库处处是这套：`io.WriterTo`（`io.Copy` 的快路径）、`io.StringWriter`（`WriteString` 快路径）、`interface{ Unwrap() error }`（错误链，第 7 章）、`interface{ Is(error) bool }`（错误分类钩子）。**设计原则：主接口越小越好，可选能力用独立的小接口 + 断言探测。**

---

## 10.6 `plugin` 包为什么几乎没人用

Go 确实有 `plugin` 标准库：`plugin.Open(path)` 加载 `.so`，`(*Plugin).Lookup(sym)` 取符号。但它的限制多到只有极端场景才值得用：

| 限制 | 后果 |
| --- | --- |
| 需要 `go build -buildmode=plugin` | 插件和主程序是**不同构建模式**的产物，主程序也必须对应编译 |
| 只支持 Linux / FreeBSD / macOS | **Windows 完全不支持** |
| 主程序与插件的 **Go 版本、所有共享依赖版本必须完全一致** | 升级一次依赖就得全量重编译，版本漂移直接崩 |
| 无法卸载（没有 `Close`） | 热更新做不到，语义上是"启动时加载" |
| 符号冲突 / `init` 重复执行 | 两个插件用同一个库的不同版本？直接崩 |
| 对 CGO / 交叉编译不友好 | 容器镜像构建复杂度暴涨 |

所以真实世界的"插件化"是这四个层次：

| 方案 | 热更新 | 隔离性 | 典型用途 |
| --- | --- | --- | --- |
| **编译期注册表**（本章 10.3） | ❌ 需重启 | 无（同进程） | 99% 的业务扩展点：多 Provider、多 Tool |
| **子进程 + 协议**（`hashicorp/go-plugin`、gRPC、**MCP**） | ✅ | 强（独立内存/权限/崩溃域） | 第三方工具、不可信代码、多语言 |
| **WebAssembly**（`wazero`，纯 Go 无 cgo） | ✅ | 强（沙箱） | 用户自定义脚本 / 规则引擎 |
| **`plugin` 包** | ❌ 实际不可用 | 弱 | 几乎不用；除非是内网固定环境的特殊场景 |

> 💡 Agent 系统的"插件化"答案几乎总是**子进程 + 协议**：MCP 就是这么干的（stdio / streamable-http），一个工具崩了不带走主进程，还能用别人的语言写。详见第 42 章。

---

## 10.7 实战：一个可插拔的 Tool 注册系统

把 10.3 + 10.4 拼起来，就是 Agent 骨架的一半。

```go
package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"sync"
)

// 1) 小接口：四个方法足够描述一个工具
type Tool interface {
	Name() string
	Description() string
	InputSchema() json.RawMessage // JSON Schema，喂给 LLM
	Call(ctx context.Context, args json.RawMessage) (Result, error)
}

type Result struct{ Text string }

// 喂给 LLM 的工具描述（function calling 的 tools[].function）
type ToolSpec struct {
	Name        string          `json:"name"`
	Description string          `json:"description"`
	Parameters  json.RawMessage `json:"parameters"`
}

// 2) 注册表：显式、可枚举、可审计
type Registry struct {
	mu     sync.RWMutex
	tools  map[string]Tool
	prefix string // MCP 聚合时用来打命名空间，避免同名冲突（第 42 章）
}

func NewRegistry(prefix string) *Registry {
	return &Registry{tools: map[string]Tool{}, prefix: prefix}
}

func (r *Registry) Register(t Tool) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, dup := r.tools[t.Name()]; dup {
		return fmt.Errorf("tools: %q already registered", t.Name())
	}
	r.tools[t.Name()] = t
	return nil
}

// 3) 稳定排序：prompt 缓存命中率靠它
func (r *Registry) Specs() []ToolSpec {
	r.mu.RLock() // 读锁要罩住整个读过程，别提前 RUnlock 再继续读 map
	defer r.mu.RUnlock()

	names := make([]string, 0, len(r.tools))
	for n := range r.tools {
		names = append(names, n)
	}
	slices.Sort(names)

	out := make([]ToolSpec, 0, len(names))
	for _, n := range names {
		t := r.tools[n]
		out = append(out, ToolSpec{
			Name:        r.prefix + n,
			Description: t.Description(),
			Parameters:  t.InputSchema(),
		})
	}
	return out
}

// 4) 沙箱化调用：任何 panic 都变成 error 返回给 LLM（第 7、8 章）
func (r *Registry) Call(ctx context.Context, name string, args json.RawMessage) (res Result, err error) {
	r.mu.RLock()
	t, ok := r.tools[name]
	r.mu.RUnlock()
	if !ok {
		return Result{}, fmt.Errorf("tools: unknown tool %q", name)
	}
	defer func() {
		if p := recover(); p != nil {
			err = fmt.Errorf("tools: %q panicked: %v", name, p)
		}
	}()
	return t.Call(ctx, args)
}

// 5) 编译期断言：接口改了就编译失败，而不是运行时才发现
var _ Tool = (*WeatherTool)(nil)

type WeatherTool struct{ apiKey string }

func (w *WeatherTool) Name() string                { return "get_weather" }
func (w *WeatherTool) Description() string         { return "查询指定城市的当前天气" }
func (w *WeatherTool) InputSchema() json.RawMessage {
	return json.RawMessage(`{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}`)
}
func (w *WeatherTool) Call(ctx context.Context, args json.RawMessage) (Result, error) {
	var in struct{ City string `json:"city"` }
	if err := json.Unmarshal(args, &in); err != nil {
		return Result{}, fmt.Errorf("get_weather: bad args: %w", err) // 错误回传给 LLM 让它自己改
	}
	return Result{Text: in.City + ": sunny, 24℃"}, nil
}
```

这个骨架里每个决策都能对应到本章/前几章的原则：

- **`Tool` 是 4 个方法的"中接口"**——再大就该拆（`SchemaProvider`、`Caller`）了。
- **`Register` 用 `:=` 前的显式错误**：重复注册是 bug，不是 feature。
- **`Specs()` 排序**：这是"工程细节决定成本"的典型。
- **`Call` 里 `recover`**：LLM 会生成任意参数，工具代码里任何越界/nil 都可能被触发。
- **`var _ Tool = (*WeatherTool)(nil)`**：Go 特有的编译期契约检查，写在实现文件里，谁改接口谁编译失败。

---

## 10.8 框架落地对照

| 框架 | 扩展点机制 | 可借鉴处 |
| --- | --- | --- |
| `database/sql` | `sql.Register(name, driver)` + 驱动包 `init()` 副作用导入（`_ "github.com/lib/pq"`） | 最经典的"注册表 + 工厂"，也是"副作用导入"的原罪源头 |
| `net/http` | `http.Handler` 中间件是 `func(Handler) Handler` | 装饰器的最小形态，5 行就能自己写 |
| **Gin** | `engine.Use(mws...)` + `c.Next()` / `c.Abort()` + `HandlersChain` | 切片 + 游标式的链，可在中间终止；比递归嵌套灵活 |
| **eino** | 组件接口化（`model.BaseChatModel` / `tool.InvokableTool`）+ `compose.Graph` 编排；`eino-ext` 提供各供应商实现 | "组件是接口，编排是图"；插件 = 换一个实现 + 换一个节点 |
| **kratos** | `contrib/` 目录下按中间件/注册中心/日志分插件；`transport.Server` 接口聚合多个 server；HTTP 侧用 `http.Filter` 链 | "一切皆插件"的组织方式，依赖倒置做得最彻底 |
| **MCP** | 跨进程的能力注册（tools/resources/prompts + capabilities 协商） | 真·热插拔，且语言无关 |

---

## 10.9 踩坑清单（CR 可直接抄）

| # | 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- | --- |
| 1 | 子类型的方法"没被调到" | 用嵌入模拟模板方法，静态绑定 | 改用装饰器/高阶函数 | 10.1 |
| 2 | 两个包注册了同名 Tool，行为随机 | 注册表静默覆盖 | `Register` 返回 error + 冲突检测 | 10.3 |
| 3 | LLM 的 tool 列表每次顺序不同，缓存全 miss | map 遍历无序直接进 prompt | 导出前 `slices.Sort` | 10.3 |
| 4 | 单测里替换不了实现 | 全局 `init()` 注册 + 包级变量 | 注册表作为依赖注入，或显式 `Register` | 10.3 |
| 5 | 接口加了第 6 个方法，全仓编译失败 | 接口定义在 provider 侧且过大 | 接口由使用方定义，≤3 方法 + 能力探测 | 10.5 |
| 6 | `plugin.Open` 线上报版本不匹配 | Go 版本/依赖版本必须完全一致 | 别用 `plugin`；子进程或编译期注册 | 10.6 |
| 7 | 一个工具 panic 打挂整个服务 | 没有沙箱边界 | 调用点 `recover` + 第三步起子进程 | 10.7 |
| 8 | 装饰器套了 5 层，排查时不知道谁改了请求 | 装饰器顺序/职责不清 | 固定洋葱顺序 + 每层打 span | 10.4 |
| 9 | 同事问"这个 Tool 注册上了吗"没人答得出 | 注册表不可观测 | 启动日志 + `/internal/registry` 端点 | 10.3 |

---

## 10.10 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 嵌入 ≠ 继承 | 没有虚方法表，父 struct 看不到外层的"子类" |
| 流程复用的正解 | 装饰器（高阶函数）/ 中间件链，不是模板方法 |
| 注册表 | `map[string]func(Cfg) (T, error)` + `RWMutex` + 重复检测 |
| 工厂 | 名字 → 构造函数；配置作参数，返回接口而非具体类型 |
| 枚举必须排序 | 进 prompt 的列表顺序要稳定，否则缓存全 miss |
| 接口位置 | 定义在使用方，1~3 个方法；可选能力用类型断言探测 |
| 编译期断言 | `var _ Tool = (*T)(nil)` |
| `plugin` 包 | 版本锁定 + 不支持 Windows + 无法卸载 → 基本弃用 |
| 真·插件化 | 子进程 + 协议（MCP / gRPC）或 wasm 沙箱 |
| 装配方式 | 显式注册优先于 `init()`；fx/wire 只是把显式注册写得更省事 |

---

## 10.11 自检题

1. **为什么 `func (b *BaseTool) Call()` 里调 `b.doExecute()` 永远走不到子类型的实现？Go 里"固定骨架 + 可变步骤"该怎么写？**
   > 要点：Go 没有虚方法表，方法调用是静态绑定；`b` 的静态类型是 `*BaseTool`，编译器直接生成 `BaseTool.doExecute` 的调用，与运行时套了什么类型无关（嵌入只做"方法提升/遮蔽"，不做动态分派）。正解：把"可变步骤"提升成接口/函数参数（装饰器），由外层组合顺序决定行为，例如 `WithAudit(WithRetry(tool))`。

2. **注册表用 `init()` 自动注册有什么问题？什么情况下它反而是对的？**
   > 要点：问题有三——① 副作用导入让依赖不可见、编译产物里"凭空多出"实现；② 单测难替换（全局状态）；③ 注册时机/顺序不可控（`init` 顺序由包依赖决定）。它反而是对的情况：**驱动式生态**（`database/sql`、`image`、`sqlite` 之类），库里无法要求用户显式调注册函数，只能靠 `init`。规则：应用层显式注册，基础设施层可以用 `init`。

3. **你的 Agent 要支持"用户上传自定义工具脚本"，用编译期注册表还是子进程？为什么？**
   > 要点：必须子进程（或 wasm）。编译期注册做不到运行时加载（`plugin` 不支持 Windows、版本锁定、无法卸载）；用户脚本是**不可信代码**，需要独立的崩溃域、资源限制（CPU/内存/超时）、权限边界与语言自由，这些只有进程/沙箱级隔离能给。协议层直接复用 MCP 的 stdio transport（第 42 章）比自己发明 RPC 划算。

---

## 10.12 延伸阅读

- `$GOROOT/src/database/sql/sql.go`：`Register`、`driver.Driver`（注册表范式原型）
- `$GOROOT/src/plugin/plugin.go`：看看限制说明就知道为什么没人用
- `$GOROOT/src/net/http/server.go`：`Handler`、`HandlerFunc`、`Flusher`（小接口 + 能力探测）
- gin-gonic/gin：`Context.Next` / `Abort` / `HandlersChain`
- cloudwego/eino 的 `components/` 与 `compose/`；go-kratos/kratos 的 `contrib/`
- `hashicorp/go-plugin`、`tetratelabs/wazero`、`modelcontextprotocol/go-sdk`
- Rob Pike: *Less is exponentially more*（"接口越大，抽象越弱"）
- 下一章：[11. 数组与切片：header、扩容与共享陷阱](11-slice-array.md)

---

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：第 9 章 [零值哲学、指针 vs 值与「可选值」设计](9-zero-value-optionals.md)
- 相关：第 4 章（嵌入与方法集，为什么没有 override）、第 5 章（接口与隐式实现）、第 36 章（fx/wire 装配）、第 41 章（工具系统全链路）、第 42 章（MCP：真·热插拔）、第 51 章（八个高频模式的提炼）
- 下一章：[11. 数组与切片：header、扩容与共享陷阱](11-slice-array.md)
