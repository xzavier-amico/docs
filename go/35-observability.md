# 35. 可观测性：slog、OTel、Prometheus

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 工程化与服务端 ｜ **编号**：35 / 54
> **本章定位**：一次用户请求会拆成 4 次 LLM 调用 + 7 次工具调用 + 3 次检索。**没有 trace id 的日志等于没有日志。**本章把 Java 侧的 Logback+MDC / Micrometer / Sleuth 三件套，映射到 slog + OTel + Prometheus，并给出 Agent 专属的 span 语义。
> **读法**：35.3 讲 `slog` 的设计为什么值一整天，35.5 是四份能直接落地的代码，35.6 是踩坑表。

---

## 35.1 引子：一个「慢」字，四条日志，零个线索

线上告警：P99 从 2.1s 涨到 8.7s。你打开日志：

```
time=2026-01-11T09:12:01.104Z level=INFO msg="llm call start" model=gpt-4o-mini
time=2026-01-11T09:12:01.121Z level=INFO msg="tool call start" tool=search
time=2026-01-11T09:12:03.882Z level=INFO msg="llm call start" model=gpt-4o-mini
time=2026-01-11T09:12:08.501Z level=INFO msg="tool call start" tool=calc
```

问题来了：**哪几条属于同一个用户请求？** 日志按时间排序，混杂着几百个并发请求。你唯一能做的推断是「大概时间上挨得近」，然后祈祷。三个月前在 Spring 里这不是问题——`logback.xml` 里一个 `%X{traceId}` 全解决了，因为 Sleuth 把 traceId 塞进了 MDC，MDC 是 ThreadLocal，`log.info()` 自动带上。

**这就是 Go 的第一个断层：没有 ThreadLocal 帮你兜底。** `context.Context` 是显式参数，你必须自己把它从 HTTP handler 一路传到最里的那个 `client.Chat(ctx, ...)`，并且**自己决定**在打日志时从 ctx 里把 trace id 捞出来。

第二个断层：Java 那边 `Timer.builder("llm.latency").publishPercentiles(0.5, 0.95, 0.99)` 一行就有分位数；Go 里你只有一个 histogram，分位得在 Prometheus 侧用 `histogram_quantile()` 算——**这个差异不是便利性差异，是正确性差异**（后面 35.6 会讲为什么客户端算分位是错的）。

---

## 35.2 直觉模型：三支柱 + 一根线

```
        用户请求 (ctx 诞生)
              │
    ┌─────────┴──────────────────────────────────────────────┐
    │  M E T R I C S（聚合，便宜，先告警）                    │
    │  llm_requests_total{model,status}   histogram 延迟      │
    │  llm_tokens_total{model,type}       → 成本核算          │
    └────────────────────────────────────────────────────────┘
              │
    ┌─────────┴──────────────────────────────────────────────┐
    │  T R A C E S（单次请求的因果树，贵，靠采样）            │
    │  span: POST /v1/chat                      [========]    │
    │   └─ span: llm.request (chat)             [===]         │
    │   └─ span: tool.call (search)                [==]       │
    │   └─ span: llm.request (chat)                     [===] │
    └────────────────────────────────────────────────────────┘
              │
    ┌─────────┴──────────────────────────────────────────────┐
    │  L O G S（离散事实，最贵，只在 span 上打补丁）          │
    │  level=ERROR trace_id=4bf9… span_id=9c1e… tool=calc    │
    │            err="context deadline exceeded"              │
    └────────────────────────────────────────────────────────┘
```

三者共用同一个 `ctx`：trace 从 ctx 里长出 span，log 从 ctx 里捞出 trace_id，metric 的 attribute 也从 ctx 里取（tenant、model）。**ctx 是那根线。**

| Java | Go | 关键差异 |
| --- | --- | --- |
| SLF4J + Logback | `log/slog`（1.21 起标准库） | slog 只管「结构化日志 API + 可插拔 Handler」，不管 JSON 之外的格式 |
| MDC（ThreadLocal） | `context.Context` + 自定义 `slog.Handler` | 显式传递；Handler 负责把 ctx 里的值注入 Record |
| Sleuth / Micrometer Tracing | `otelhttp` + `otel.Tracer` | W3C `traceparent` 传播，OpenTelemetry 是唯一事实标准 |
| Micrometer `Timer` | OTel `Float64Histogram` / `client_golang` HistogramVec | 客户端算分位 vs 服务端算分位 |
| Actuator `/actuator/prometheus` | `promhttp.Handler()` 挂到 `/metrics` | 自己挂路由，没有全家桶 |
| `log.info("x {}", y)` | `slog.Info("x", "y", y)` 或 `LogAttrs` | 键值对而非占位符；键必须**常量**，否则分配 |
| `@Async` 上下文透传（ThreadLocal 丢失） | goroutine 必须显式传 ctx | 同一个问题，Go 反而更早暴露 |

---

## 35.3 底层实现：slog 的 Handler 契约与零分配路径

### 35.3.1 四个类型的契约

`slog` 的设计是整个标准库里最「接口化」的：Logger 只是一个薄壳，所有真正的实现都在 Handler 上。

```go
// log/slog/logger.go —— Logger 只有两个字段，几乎没有状态
type Logger struct {
	handler Handler
}

// log/slog/handler.go —— 核心接口，只有 4 个方法
type Handler interface {
	Enabled(context.Context, Level) bool
	Handle(context.Context, Record) error
	WithAttrs(attrs []Attr) Handler   // 派生：带上前置属性，返回新 Handler
	WithGroup(name string) Handler    // 派生：进入组
}
```

`Record` 是「一次日志事件」的载体，注意它的字段布局（真实）：

```go
// log/slog/record.go
type Record struct {
	Time    time.Time
	Message string
	Level   Level
	PC      uintptr       // 调用点，供 AddSource 用
	front   [5]Attr       // 5 个以内内联在栈上，不分配
	back    []Attr        // 超出的部分
	nFront  int
	nBack   int
}
```

`front [5]Attr` 这个设计就是为了**零分配**：大多数日志不超过 5 个属性，直接存栈上。`Attr` 是 `{Key string, Value Value}`，而 `Value` 是一个 `any + uint64` 的双字段优化结构（`log/slog/value.go`），用 `Kind()` 区分 `KindString / KindInt64 / KindFloat64 / KindBool / KindTime / KindDuration / KindGroup / KindAny / KindLogValuer`。**`KindAny` 是唯一的「慢路径」**——它要求 Handler 自己去反射或 JSON 序列化。

### 35.3.2 从 Info 到写出字节，发生了什么

```
slog.Info("llm call", "model", m, "ms", d)
   │
   ├─ ① 每个键值对转成 Attr：Value 里装 any(int64/string) → 可能触发装箱
   │
   ├─ ② logger.log(ctx, level, msg, args...) → r := NewRecord(now, level, msg, pc)
   │      每个 Attr 走 r.AddAttrs → 前 5 个进 front[5]
   │
   ├─ ③ if !h.Enabled(ctx, level) { return }   ← 级别过滤在构造之后，所以 Enabled 要能快速返回
   │
   ├─ ④ h.Handle(ctx, r)  ← JSONHandler.Handle：
   │      buf := bufPool.Get()               ← sync.Pool 复用 []byte（log/slog/handler.go 的 bufPool）
   │      buf.WriteByte('{')
   │      appendJSONTime(buf, r.Time)         ← 默认 RFC3339 毫秒，可被 ReplaceAttr 改
   │      appendAttr(buf, a)                  ← 字符串要转义，这里是主要开销
   │      ...
   │      h.mu.Lock(); h.out.Write(buf); h.mu.Unlock()   ← commonHandler.mu 保护 out
   │      bufPool.Put(buf)
   │
   └─ ⑤ 写到底层 io.Writer —— 同步！阻塞的是你的业务 goroutine
```

三个能直接改成钱的结论：

1. **`slog.String(k, v)` 的 k 必须是编译期常量**，否则 `With`/`Info` 会把整串参数逃逸到堆。用 `slog.LogAttrs(ctx, level, msg, attrs...)` 可以直接传 `[]Attr`，完全绕过 `...any` 装箱——热路径上这是 3~5 倍的差距。
2. **`Handle` 是同步写**。stdout 被 pipe 阻塞（比如容器日志驱动卡住）时，你的 request goroutine 一起阻塞。生产环境要么用 OTel log bridge 走批量导出，要么自己套一层 channel + 单个 writer goroutine（这就是 Logback 的 `AsyncAppender`）。
3. **`commonHandler.mu` 是共享的**：同一个 Handler 被多 goroutine 使用时输出串行化。所以 Handler 派生（`With`）出来的子 Handler 会共享这把锁——想并行就别在每行日志里塞 20 个属性。

### 35.3.3 `Record.Clone()` 的坑

`Record` 是值传递，但里面的 `back []Attr` 是切片。如果你要把 `Record` 存起来（异步打日志、批量导出），**必须 `r = r.Clone()`**，否则原切片被复用，你写出去的是别人的数据或者是乱码。这是 `slog` 文档里明写的契约，也是自定义 Handler 第一号事故。

---

## 35.4 Java 对照：为什么「客户端算分位」是错的

| 维度 | Micrometer | OTel（Go） | 为什么不一样 |
| --- | --- | --- | --- |
| 分位计算 | 客户端可算（`publishPercentiles`），也可发 histogram | 只发 histogram/summary，分位在 Prometheus `histogram_quantile()` 算 | 分位数**不可聚合**；多实例各自算 p99 再求平均是错的 |
| 指标类型 | `Counter/Timer/Gauge/DistributionSummary` | `Int64Counter/Float64Histogram/Int64UpDownCounter/ObservableGauge` | 命名更接近 Prometheus |
| 标签 | `Tag`/`tags` | `attribute.KeyValue` | 语义相同，爆炸风险相同 |
| 追踪 | Micrometer Tracing + Brave/OTel | OTel 原生 | Go 侧没有 Brave 历史包袱 |
| 日志关联 | MDC（ThreadLocal 自动） | `ctx` + Handler | Go 必须显式传，成本是「多写一个参数」，收益是「协程切换不丢」 |
| 好用的默认值 | Actuator 一键开 | 每个 exporter 都自己配 | Go 的世界是组装式 |

> 🔪 **一个 Java 老手会做错的决定**：把 `publishPercentiles(0.5, 0.95, 0.99)` 的思维搬到 Go，直接用 `Float64ObservableGauge` 报「当前 p99」。这是错的：网关侧做聚合时会算成一个无意义的数字。**Go 侧统一发 histogram，分位一律在查询端算。**

---

## 35.5 工程落地：四段能直接粘的代码

### 35.5.1 一个把 trace_id 注入每条日志的 Handler

这是「Sleuth 等价物」，20 行，全章最实用：

```go
package obs

import (
	"context"
	"log/slog"

	"go.opentelemetry.io/otel/trace"
)

// traceHandler 把 OTel 的 trace_id / span_id 写进每条日志
type traceHandler struct {
	slog.Handler // 嵌入：Enabled 直接复用
}

func WithTraceID(h slog.Handler) slog.Handler { return traceHandler{h} }

func (h traceHandler) Handle(ctx context.Context, r slog.Record) error {
	if sc := trace.SpanContextFromContext(ctx); sc.IsValid() {
		r = r.Clone() // ⚠️ 必须克隆：Record.back 是共享切片
		r.AddAttrs(
			slog.String("trace_id", sc.TraceID().String()),
			slog.String("span_id", sc.SpanID().String()),
		)
	}
	return h.Handler.Handle(ctx, r)
}

// WithAttrs / WithGroup 必须「重新包一层」，否则派生之后 trace 注入就丢了
func (h traceHandler) WithAttrs(as []slog.Attr) slog.Handler {
	return traceHandler{h.Handler.WithAttrs(as)}
}

func (h traceHandler) WithGroup(name string) slog.Handler {
	return traceHandler{h.Handler.WithGroup(name)}
}

// 构造：注意 ReplaceAttr 是唯一能「改内置字段名」的钩子
func NewLogger(w io.Writer, lvl *slog.LevelVar) *slog.Logger {
	h := slog.NewJSONHandler(w, &slog.HandlerOptions{
		Level:     lvl,
		AddSource: false, // 开了每次都要 runtime.CallersFrames，热路径很贵
		ReplaceAttr: func(groups []string, a slog.Attr) slog.Attr {
			if len(groups) == 0 && a.Key == slog.TimeKey {
				a.Key = "ts" // 对齐公司日志平台约定
			}
			return a
		},
	})
	return slog.New(WithTraceID(h))
}
```

用法就一句话，**永远传 ctx**：

```go
slog.InfoContext(ctx, "llm call done", "model", m, "dur_ms", d.Milliseconds())
```

> ⚠️ 不要用 `slog.Info(...)`（无 ctx 版本）——那不是「少写一个参数」，而是「这条日志永远没有 trace_id」。约定优于配置：团队里禁用无 ctx 的日志调用，用 `staticcheck` 的自定义规则或 CR 卡住。

### 35.5.2 OTel 三件套初始化

```go
package obs

import (
	"context"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
)

// SetupOTel 返回一个 shutdown 函数，在 main 里 defer 调用
func SetupOTel(ctx context.Context, endpoint, service string) (func(context.Context) error, error) {
	exp, err := otlptracegrpc.New(ctx,
		otlptracegrpc.WithEndpoint(endpoint), // 例：otel-collector:4317
		otlptracegrpc.WithInsecure(),         // 内网；跨网必须走 TLS
	)
	if err != nil {
		return nil, err
	}

	res, err := resource.Merge(resource.Default(),
		resource.NewWithAttributes(semconv.SchemaURL,
			semconv.ServiceName(service),
			semconv.ServiceVersion("0.3.1"),
			semconv.DeploymentEnvironment("prod"),
		))
	if err != nil {
		return nil, err
	}

	tp := sdktrace.NewTracerProvider(
		sdktrace.WithResource(res),
		// 采样：父级已采样就跟着采样；否则 10%
		sdktrace.WithSampler(sdktrace.ParentBased(sdktrace.TraceIDRatioBased(0.1))),
		// 批量导出：队列满了会丢 span（这是采样的第二道闸，别调太大）
		sdktrace.WithBatcher(exp, sdktrace.WithMaxQueueSize(4096)),
	)
	otel.SetTracerProvider(tp)
	// W3C traceparent + baggage：跨服务、跨 MCP/HTTP 边界的唯一通用格式
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{}, propagation.Baggage{},
	))

	return func(ctx context.Context) error {
		c, cancel := context.WithTimeout(ctx, 5*time.Second)
		defer cancel()
		return tp.Shutdown(c) // 必须等它返回，否则最后一批 span 丢失
	}, nil
}
```

配套的 HTTP instrumentation（这一行让 span 自动长出来）：

```go
mux.Handle("/v1/chat", otelhttp.NewHandler(chatHandler, "POST /v1/chat"))
client := &http.Client{Transport: otelhttp.NewTransport(http.DefaultTransport)}
```

### 35.5.3 Agent 语义约定：`llm.request` / `tool.call`

官方 OTel 已有 `gen_ai.*` 语义约定（仍在演进，属性名可能变，以你本地 semconv 版本为准）。我们的做法是**span 名用自己的一套，属性名对齐 `gen_ai.*`**，这样既好读又能兼容将来：

```go
var tracer = otel.Tracer("acme/agent")

// LLM 调用：模型名进 span 名（便于按模型看延迟分布），token 数进属性（成本分析）
func TraceLLM(ctx context.Context, model string) (context.Context, trace.Span) {
	ctx, span := tracer.Start(ctx, "llm.request "+model,
		trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(
			attribute.String("gen_ai.system", "openai"),
			attribute.String("gen_ai.operation.name", "chat"),
			attribute.String("gen_ai.request.model", model),
			attribute.String("tenant.id", TenantFrom(ctx)),
		),
	)
	return ctx, span
}

// 调用方收尾：把 usage 与错误落到同一个 span 上
func FinishLLM(span trace.Span, in, out int, err error) {
	span.SetAttributes(
		attribute.Int("gen_ai.usage.input_tokens", in),
		attribute.Int("gen_ai.usage.output_tokens", out),
	)
	if err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, err.Error())
	}
	span.End() // 忘 End 的 span 永远不会导出，只会在内存里躺着
}

// 工具调用：工具名进 span 名 + 属性，参数只放「形状」不放内容（防 PII 泄漏）
func TraceTool(ctx context.Context, name string, argBytes int) (context.Context, trace.Span) {
	return tracer.Start(ctx, "tool.call "+name,
		trace.WithAttributes(
			attribute.String("gen_ai.operation.name", "execute_tool"),
			attribute.String("tool.name", name),
			attribute.Int("tool.args.bytes", argBytes),
			attribute.Bool("tool.idempotent", true),
		))
}
```

加上指标与 `/metrics`：

```go
var (
	llmLatency, _ = otel.Meter("acme/agent").Float64Histogram("llm.request.duration",
		metric.WithUnit("s"), metric.WithDescription("LLM 调用端到端延迟"))
	tokens, _ = otel.Meter("acme/agent").Int64Counter("llm.tokens",
		metric.WithDescription("token 消耗，用于成本核算"))
)

// 业务代码里
start := time.Now()
ctx, span := TraceLLM(ctx, model)
defer span.End()
reply, err := c.Chat(ctx, req)
llmLatency.Record(ctx, time.Since(start).Seconds(), metric.WithAttributes(
	attribute.String("model", model),
	attribute.String("status", statusOf(err)),
))
tokens.Add(ctx, int64(reply.Usage.PromptTokens), metric.WithAttributes(
	attribute.String("model", model), attribute.String("type", "prompt")))
tokens.Add(ctx, int64(reply.Usage.CompletionTokens), metric.WithAttributes(
	attribute.String("model", model), attribute.String("type", "completion")))
```

暴露给 Prometheus（用 OTel 的 prometheus exporter，指标口径与 trace 同源）：

```go
import (
	otelprom "go.opentelemetry.io/otel/exporters/prometheus"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

exp, _ := otelprom.New()
mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(exp))
otel.SetMeterProvider(mp)

mux.Handle("/metrics", promhttp.Handler())
```

查询端算分位（永远不要客户端算）：

```promql
histogram_quantile(0.99,
  sum by (le, model) (rate(llm_request_duration_bucket[5m])))
```

### 35.5.4 零分配日志：热路径的唯一写法

```go
// ❌ 每次调用都构造 []any，参数逃逸到堆；19 ns → 约 300 ns
logger.Info("tool call", "tool", name, "ms", ms, "ok", ok)

// ✅ Attr 数组 + LogAttrs：无装箱、无切片增长，只有字符串转义
attrs := [4]slog.Attr{
	slog.String("tool", name),
	slog.Int64("ms", ms),
	slog.Bool("ok", ok),
	slog.String("tenant", tenant),
}
logger.LogAttrs(ctx, slog.LevelInfo, "tool call", attrs[:]...)
```

同理，**别用 `slog.Any("cfg", bigStruct)`**：它落到 `KindAny`，JSON Handler 会走反射（详见 [第 37 章](37-codegen-reflection.md)）。要打结构体，就实现 `slog.LogValuer`：

```go
func (c Config) LogValue() slog.Value {
	return slog.GroupValue(
		slog.String("model", c.Model),
		slog.Int("max_steps", c.MaxSteps), // 只打安全字段，别整个吐出去
	)
}
```

### 35.5.5 采样与成本：三档预算，别让观测把自己搞穷

Agent 服务的可观测成本有三条独立的账单，必须分开设预算：

| 账单 | 计量单位 | 典型量级 | 控制手段 |
| --- | --- | --- | --- |
| 日志 | 字节×条数（prompt 全文是炸弹） | 一条带 prompt 的日志 8KB，10 QPS 就是 7GB/天 | 只打长度/哈希；ERROR 打全文；handler 侧截断 |
| Trace | span 条数（一次请求 15~40 个 span） | 全采样 = 每请求 ~40 span | `ParentBased(TraceIDRatioBased(0.05~0.1))` + collector 侧尾部采样保错误 |
| 指标 | 活跃时间序列数（cardinality） | label 组合爆炸是内存杀手 | 只放低基数维度（model/status/tenant_tier） |

三个在 Agent 场景特别值钱的做法：

- **错误必采、成功抽样**：用 `ParentBased` 让上游已采样的继续采样，再用 collector 的尾部采样（tail sampling）按 `status=error`、`duration > 5s` 强制保留。这样 5% 的采样率下，你看得到所有事故现场。
- **token 就是钱，必须成为一等指标**：`llm.tokens{model,type}` 加上 `tenant.id` 之后，`sum by (tenant) (increase(llm_tokens_total[1h]))` 就能直接对账，也是第 47 章「预算刹车」的数据源。
- **低采样率下的分位仍然可信**：histogram 是聚合量，采样只影响样本量不影响无偏性；而如果你只在采样后的 span 里算延迟，样本会有偏（慢请求更容易被尾部采样留下）。**延迟指标从 histogram 取，不要从 trace 取。**

---

## 35.6 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| 日志里没 trace_id | 调了无 ctx 的 `slog.Info` | 一律 `slog.InfoContext/LogAttrs`，CR 卡住 | 35.5.1 |
| 派生 logger 后 trace_id 消失 | 自定义 Handler 的 `WithAttrs` 忘了重新包装 | 两个派生方法都 `return traceHandler{...}` | 35.5.1 |
| 异步导出日志内容是乱码/串台 | 存了 `Record` 没 `Clone()` | `r = r.Clone()` | 35.3.3 |
| 日志量翻倍导致 QPS 掉 30% | `Handle` 同步写 stdout + 属性太多 | 减少属性、关 `AddSource`、上异步 writer | 35.3.2 |
| 内存涨、GC 频繁 | `slog.Any` + 键名用变量 + 每请求 `With` 链 | `LogAttrs` + 常量键 + 复用 logger | 35.5.4 |
| trace 断裂（一次请求变多个 trace） | 出站请求没用 `otelhttp.NewTransport` | 出站 instrument + `propagator` 必须设置 | 35.5.2 |
| span 名字爆炸，看板全是 `/v1/user/{id}` 的千百个变体 | 用了原始 URL 当 span 名 | 用路由模板命名（`POST /v1/chat`） | 35.5.2 |
| 最后一批 span 丢失 | `tp.Shutdown` 没调用或没等待 | 在 `main` 里 defer 并等返回 | 35.5.2 |
| span 内存泄漏 | 某条分支没 `span.End()` | `defer span.End()` 或确保所有路径 | 35.5.3 |
| 指标基数爆炸（Prometheus 内存爆） | 把 `user_id` / `session_id` / prompt 当 label | 只放低基数标签（model/status/tenant） | 35.5.3 |
| p99 数字看着不对劲 | 客户端算了分位再聚合 | 只发 histogram，查询端 `histogram_quantile` | 35.4 |
| 日志里有 prompt 原文 → 合规事故 | 直接打了请求体 | 只打长度/哈希，或走脱敏 `ReplaceAttr` | 35.5.4 |

---

## 35.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| `slog` 四个类型 | `Logger`（薄壳）/ `Handler`（4 方法）/ `Record`（`front [5]Attr` 内联）/ `Value`（Kind 区分） |
| Handler 契约 | `Enabled/Handle/WithAttrs/WithGroup`；派生方法必须重新包装自己 |
| 零分配 | `LogAttrs` + `[]slog.Attr` + 常量键；避开 `slog.Any` |
| trace 注入 | 自定义 Handler 从 `ctx` 取 `SpanContextFromContext`，`Record` 先 `Clone` |
| 同步陷阱 | `Handle` 阻塞业务 goroutine；日志量大必须异步 |
| OTel 三件套 | API（`otel`）+ SDK（`sdk/trace`、`sdk/metric`）+ Exporter（`otlptracegrpc`） |
| 传播 | `propagation.TraceContext` 的 `traceparent` 是跨语言唯一通用格式 |
| span 命名 | `llm.request <model>` / `tool.call <tool>`，属性对齐 `gen_ai.*` |
| 采样 | `ParentBased(TraceIDRatioBased(0.1))`；成本敏感场景上尾部采样（collector 侧） |
| 指标 | 只发 histogram/counter；分位查询端算；label 必须低基数 |
| 成本账本 | `llm.tokens{model,type}` + `tenant.id` → 计费与预算刹车 |
| 关停 | `tp.Shutdown(ctx)` 必须在进程退出前完成 |

## 35.8 自检题

1. **`slog.InfoContext(ctx, "x", "k", v)` 和 `slog.Info("x", "k", v)` 除了「多/少一个参数」，还有什么实质区别？**
   > 答案要点：`ctx` 是唯一能携带 trace_id 的通道。自定义 Handler 从 `ctx` 里取 `SpanContext` 注入 `trace_id`/`span_id`；无 ctx 版本让这条日志无法与任何 trace 关联。此外 ctx 还能带 tenant、采样决策等信息。

2. **为什么「客户端算 p99，再让 Prometheus 聚合」是错的？Go 应该怎么做？**
   > 答案要点：分位数不是可加/可平均的量，多实例各自 p99 的平均值没有统计意义；正确做法是发送 histogram（bucket 是可加的），在查询端用 `histogram_quantile(0.99, sum by (le) (rate(..._bucket[5m])))` 统一计算。

3. **你的自定义 Handler 里，为什么 `Handle` 修改 `Record` 前必须 `Clone()`？而 `WithAttrs` 为什么必须返回新的包装？**
   > 答案要点：`Record` 的 `back []Attr` 是切片，值传递只复制了 header，多个 goroutine/多层 Handler 可能共享底层数组，不 Clone 会互相覆写；`WithAttrs/WithGroup` 返回的是**被包装的底层 Handler**，如果你直接 `return h.Handler.WithAttrs(as)`，外层包装就丢了，之后派生的 logger 不再注入 trace_id。

## 35.9 延伸阅读

- `log/slog/logger.go`、`handler.go`（`commonHandler`/`jsonHandler`/`bufPool`）、`record.go`（`front [5]Attr`）、`value.go`（`Kind` 与 `LogValuer`）
- `go.opentelemetry.io/otel` 官方文档：Signals → Traces / Metrics / Logs；Semantic Conventions（含 `gen_ai.*` 命名空间）
- `go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp` 与 `bridges/otelslog`
- `github.com/prometheus/client_golang/prometheus/promhttp`：`Handler()` 与 `HandlerFor(reg, opts)`
- Google SRE Book 第 6 章《Monitoring Distributed Systems》：为什么「四个黄金信号」比一堆指标有用
- 交叉引用：[第 29 章](29-pprof-trace.md) 的 `runtime/trace` 与分布式 trace 的分工；[第 47 章](47-agent-productionization.md) 的成本核算

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 34 章](34-testing.md) 测试：表驱动、mock、fuzz 与可重放
- 下一章：[第 36 章](36-dependency-injection.md) 依赖注入与装配：fx / wire / 手工
