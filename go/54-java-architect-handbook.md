# 54. Java 架构师的 Go 落地清单

> **状态**：`已完成（初稿）` ｜ **所属**：第九部分 · 生产部署与收尾 ｜ **编号**：54 / 54
> **本章定位**：全书最后一章，也是唯一一章**没有任何原理**的章——它就是一本可以打印出来贴在显示器边上的手册。你的 CR（Code Review）评论、CI 配置、PR 模板、团队规范，90% 可以从这里抄。
> **读法**：把 54.4（并发 20 条）、54.5、54.6 三节打印出来；54.9 的 CI 配置直接提交到仓库；54.12 的书单按顺序读。

---

## 54.1 引子：一个 400 行的 Go 服务，CR 里有 11 条「Java 味」

一个从 Java 团队转过来的小组，第一次提交 Go 服务，400 行代码。我 review 完写了 11 条评论，其中 **8 条和技术无关，全是「把 Java 的审美搬过来了」**：

```go
// ① 为分层而分层：三个文件、三个接口、三个 struct，做的事情是「把 A 字段搬到 B 字段」
type UserService interface { GetUser(ctx context.Context, id string) (*User, error) }
type userServiceImpl struct { repo UserRepository }
type UserController struct { svc UserService }
type UserRepository interface { FindByID(ctx context.Context, id string) (*UserEntity, error) }
type UserEntity struct { ID string; Name string }
type UserDTO    struct { ID string; Name string }   // ② 和 Entity 一模一样
type UserVO     struct { ID string; Name string }   // ③ 和 DTO 一模一样

// ④ 一个实现也抽接口，并且接口定义在提供方包里
// ⑤ 用 panic 表达业务错误，靠顶层 recover 兜
// ⑥ 每个 struct 手写 GetName()/SetName()，说"封装"
// ⑦ 共享 map 用 sync.Mutex 保护，但有两处忘了加锁（-race 一跑就红）
// ⑧ go func(){...}() 没有任何退出条件，压测时 goroutine 涨到 12 万
```

这份代码**能编译、能通过 CI、能跑起来**——这才是最危险的地方。Java 的习惯在 Go 里不会报错，只会让代码在六个月后变得没人敢动，然后在某个高并发夜里炸给你看。

> 🎯 本章的立场：**从 Java 带到 Go 的最大风险不是「不会写」，而是「写得太像 Java 也能跑」**。所以这一章全是可以机械执行的判断标准，不是心得。

---

## 54.2 直觉模型：Go 的代码审美 = 三次删除 + 一棵决策树

```
你写的每一段抽象，都要通过这三道删除：

   ① 删掉这一层，代码会变差吗？
      ├─ 不会 → 直接删（分层、包装、接口、DTO 都在这里死掉）
      └─ 会   → 进 ②

   ② 这一层现在有几个实现 / 未来六个月会有第二个吗？
      ├─ 只有一个且无替换需求 → 删掉接口，直接用具体类型
      └─ ≥2 或明确要替换（DB、供应商、协议）→ 进 ③

   ③ 接口定义在哪一边？
      ├─ 在提供方（service 包）→ 挪到使用方，只保留「使用方真正用到的方法」
      └─ 已经在使用方 → 方法数 ≤3 吗？
             ├─ 是 → 合格
             └─ 否 → 拆成多个小接口（io.Reader 只有 1 个方法，你也可以）
```

对照一下 Java 的默认动作是「**先建四层，再往里填**」；Go 的默认动作是「**先写一个函数，被第二个人用到时才提取**」。这句话听起来像鸡汤，但它是可执行的：**每次 CR 看到新文件、新接口、新包，就问「如果删掉，谁疼？」**——答不上来就是过度设计。

| 维度 | Java 团队默认 | Go 团队默认 | 判据 |
| --- | --- | --- | --- |
| 文件数 | 一个类一个文件 | 一个包一组相关文件 | 一个文件 200~600 行是常态 |
| 接口数 | 每层一个 | 只在需要替换/解耦时 | 「一个实现」的接口基本都是噪音 |
| 抽象时机 | 设计期（先想扩展点） | 演化期（第二次出现时才抽） | 出现过两次才算模式 |
| 分层 | controller/service/dao 固定四层 | 按依赖方向分，常只有 2~3 层 | 每一层都要能说出「它挡住了什么变化」 |
| 命名 | `AbstractXxxFactoryBean` | `newXxx` / `Xxx` | 名字里不该有设计模式 |
| 错误 | 异常层层上抛 | `error` 逐层包装返回 | 见 54.5 |

---

## 54.3 别把 Java 习惯带过来：七条具体的「不要」

| # | Java 习惯 | 在 Go 里的症状 | 正确做法 |
| --- | --- | --- | --- |
| 1 | **为分层而分层** | `controller → service → biz → dao` 四层，每层一个 interface + 一个 impl，除了转发什么都不做 | 依赖方向决定层数，常见只有 `transport → app → store`；能合并的层合并，转发层删掉 |
| 2 | **过度抽象** | 一个实现的接口、只有一种形态的 `AbstractBase`、泛型包装三层 | 具体类型优先；接口 ≤3 方法；泛型只在真正需要多类型特化时用（第 6 章） |
| 3 | **接口由提供方定义** | `internal/user/service.go` 里定义一个 12 方法的 `UserService` | 接口定义在**使用方**包，只写它用到的方法；提供方直接返回具体类型（第 5、10 章） |
| 4 | **DTO / VO / Entity 三件套** | 三个结构一模一样的 struct + 三个转换函数 | 一个 struct 用到尾；需要变形就写一个 `toAPI()` 函数，不加类型 |
| 5 | **getter/setter + 封装执着** | 手写 20 个 `GetX()/SetX()`，字段全小写 | 导出字段直接用（`cfg.Timeout = 3*time.Second`）；只有需要不变式/惰性初始化时才用方法 |
| 6 | **注解 / AOP 幻想** | 找「Go 的 `@Transactional`」「Go 的 AOP」；或者上反射实现「声明式」 | 中间件函数链 + 代码生成 + 显式调用（第 1、36、37 章） |
| 7 | **容器 / DI 全家桶** | 引入 fx/dig 做全局容器，构造函数互相 `Inject` | 在 `main` 里手工装配（第 53 章）；规模大了再用 wire 生成 |
| 8 | **线程池思维** | 造一个 `WorkerPool` 包，`Submit(task)` 返回 `Future` | goroutine + errgroup + 有界 channel（第 26 章） |
| 9 | **异常式错误** | 业务错误用 `panic`，或者 `_ = err` 忽略 | 见 54.5 清单 |
| 10 | **代码风格战争** | 争论缩进、命名、import 顺序 | `gofmt` 无争议；其余交给 `.golangci.yml` |

> 💡 **判断「是否过度设计」最快的一问**：*把这个接口删掉，把 interface 换成具体类型，需要改几行？*
> 答案如果是「一行都不用改，因为只有一个实现」，那它就是噪音。
> 如果是「要改 30 个调用方」，那说明**它压根不该由提供方定义**——使用方自己声明一个小接口就够了。

---

## 54.4 并发安全 Code Review 清单（20 条，可直接打印）

用法：CR 时从上往下过一遍，每条都是「一眼能判断」的。第 4 列的 `-race` 指 `go test -race ./...`。

| # | 检查项 | 判断标准（不合规的样子） | 工具 |
| --- | --- | --- | --- |
| 1 | **每个 goroutine 都有明确退出路径** | `go func(){ for { ... } }()`，循环里没有 `ctx.Done()` / `done` / 关闭的 channel / `return` 条件 | goroutine profile |
| 2 | **`go` 之前有 owner 与生命周期说明** | 发起者不等待、不取消、不记录；没有 `errgroup`/`WaitGroup`/`ctx` 任一 | `-race`、pprof |
| 3 | **共享可变状态有 owner** | 包级 `var cache map[...]` 被两个以上 goroutine 写；没有 mutex / 没有单一 writer | `-race` |
| 4 | **`map` 绝不并发写** | 读多写少的场景直接裸 map；或「只在启动时写」但启动是并发的 | `-race` 会 fatal |
| 5 | **`WaitGroup.Add` 在 `go` 之前** | `go func(){ wg.Add(1); ... }()` —— 可能 `Wait` 先返回 | `go vet` |
| 6 | **channel 有唯一关闭方** | 多个 goroutine 都可能 `close(ch)`；或接收方 close；或向已关闭 channel 发送 | `-race` / panic |
| 7 | **向 channel 发送时考虑了接收方消失** | 无缓冲 channel 在 handler 里阻塞发送，客户端断开后 goroutine 永久挂住 | goroutine profile |
| 8 | **循环里 `select` 有 `ctx.Done()` 分支** | `for { select { case v := <-ch: ... } }` 没有退出分支 | 人眼 |
| 9 | **`time.After` 不在循环里** | `for { select { case <-time.After(d): } }` —— 每次都新建 Timer，垃圾堆积 | `-race`、heap |
| 10 | **Timer / Ticker 调了 `Stop()`** | `time.NewTicker` 后没有 `defer t.Stop()` | 人眼 |
| 11 | **锁内不做 IO / 不调外部** | `mu.Lock(); resp, err := client.Do(req); mu.Unlock()` | 人眼 + block profile |
| 12 | **`sync.Mutex` 不被复制** | struct 里有 Mutex 字段，却按值传参 / 按值返回 / 放进 slice 后被 append 复制 | `go vet` 的 `copylocks` |
| 13 | **不依赖 Mutex 可重入** | 方法 A 上锁后调方法 B，B 也上锁 → 死锁（Go 的 Mutex 不可重入） | 人眼 / 死锁 panic |
| 14 | **`RWMutex` 的读锁里不改数据** | `RLock()` 里执行 `m[k] = v` 或 append | `-race` |
| 15 | **循环变量捕获正确**（1.22 前后） | `for _, v := range xs { go func(){ use(v) }() }` —— 1.21 及以前全取最后一个值 | `go vet`（loopclosure） |
| 16 | **`defer` 不在循环里** | `for _, f := range files { defer f.Close() }` —— 句柄攒到函数结束 | `go vet`（部分场景） |
| 17 | **有界并发** | 对 N 条数据 `for { go call() }`，N 来自用户输入；没有 `SetLimit` / 信号量 / worker 数上限 | 人眼 |
| 18 | **`atomic` 用法正确**（1.19+） | 用 `atomic.LoadInt64(&x)` 混着 `x++` 写；或用普通变量与 atomic 混读混写 | `-race`（未必报，靠人眼） |
| 19 | **`context` 从入口一路传到最底层** | 中途出现 `context.Background()`；或把 ctx 存进 struct 字段；或用 `context.TODO()` 当默认值 | `contextcheck` |
| 20 | **goroutine 内的 panic 被隔离** | 后台 goroutine 里裸 `panic` → 整个进程退出；没有 `recover` | 人眼 / `recover` 日志 |

**补充三条「检测手段」清单**（比读代码更靠谱）：

| 手段 | 命令 | 抓什么 |
| --- | --- | --- |
| 数据竞争 | `go test -race ./...`（CI 必跑） | 99% 的共享状态 bug，只会漏「逻辑上错但没竞争」的 |
| 静态检查 | `go vet ./...` + `staticcheck` + `golangci-lint` | copylocks、loopclosure、未使用返回值、错误格式 |
| 运行时诊断 | `pprof` 的 `goroutine`（看栈里 `chan receive`/`select` 堆积）、`block`、`mutex` | 泄漏与锁竞争，`-race` 抓不到的 |

> 🩸 **最贵的遗漏是第 1 条**：`-race` 完全抓不到 goroutine 泄漏。第 25 章讲的四种泄漏形态（无退出条件 / 无缓冲阻塞 / ctx 未取消 / 生产者快于消费者），CR 里只能靠「这个 `go` 什么时候结束」这一问。

---

## 54.5 错误处理清单（12 条）

| # | 检查项 | 判断标准 | 详见 |
| --- | --- | --- | --- |
| 1 | 每个 `err` 都被处理或显式忽略 | 出现裸 `_ = doSomething()` 必须带理由注释 | 第 7 章 |
| 2 | 包装用 `%w` 而非 `%v` | `fmt.Errorf("load: %v", err)` 会断掉 `errors.Is` 链 | 第 7 章 |
| 3 | 包装信息是「动作」，不是「复读」 | ❌ `fmt.Errorf("error: %v", err)`；✅ `fmt.Errorf("load config: %w", err)` | — |
| 4 | 哨兵错误只用于「调用方需要判断」的场景 | 每个函数都定义 5 个 `ErrXxx`，调用方根本不用 | 第 7 章 |
| 5 | 分类可重试 / 不可重试 | 重试逻辑里出现「任何 err 都重试」或「按字符串匹配错误」 | 第 7、40 章 |
| 6 | 库代码不 `panic` | `panic` 出现在 `internal/...` 的普通函数里（`init`、`MustXxx` 除外） | 第 8 章 |
| 7 | `recover` 只在 goroutine 边界 / 顶层 | handler 里 recover 后**必须记日志 + 打监控**，不能静默吞掉 | — |
| 8 | 错误不做控制流里的「等于」比较 | `if err == io.EOF` 而非 `errors.Is(err, io.EOF)`（包装后失效） | — |
| 9 | 返回值顺序固定 `(T, error)` | 出现 `(error, T)` 或 `(T, bool, error)` | — |
| 10 | 错误信息能定位 | 日志里只有 `"failed"`，没有 id / 参数 / 上下文 | 第 35 章 |
| 11 | 错误不重复打印 | 同一个错误在每层都 `log.Error` 一次（一次故障 8 条日志） | — |
| 12 | 错误里的敏感信息 | DSN 带密码、手机号、token 原样进日志/返回给 LLM | 第 47 章 |

```go
// 反例（Java 味）：靠异常控制流 + 忽略错误
func loadOrder(id string) *Order {
	o, _ := db.Find(id)  // ❌ 静默丢错，调用方拿到 nil 再 nil panic
	return o
}

// 正例：分层包装 + 分类 + 不重复打印
func loadOrder(ctx context.Context, id string) (*Order, error) {
	o, err := db.Find(ctx, id)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, fmt.Errorf("order %s: %w", id, ErrNotFound) // 可判断的哨兵
	}
	if err != nil {
		return nil, fmt.Errorf("query order %s: %w", id, err) // 只在这一层包一次
	}
	return o, nil
}
// 只在「进程边界」（handler / 顶层 goroutine）打日志：
//   slog.Error("load order", "order_id", id, "err", err)
```

---

## 54.6 性能与内存清单（12 条）

| # | 检查项 | 判断标准 | 详见 |
| --- | --- | --- | --- |
| 1 | 热路径不靠猜，靠 profile | 有性能争议时没有 pprof 数据（"我觉得这样快"） | 第 29 章 |
| 2 | slice / map 预分配 | 已知长度却 `append` 到底 / `make(map)` 不带 cap（千级以上元素） | 第 11 章 |
| 3 | 不在热路径做字符串拼接 | 循环内 `s += x`；应该 `strings.Builder` 或 `strings.Join` | 第 13 章 |
| 4 | 循环内的 `[]byte(s)` / `string(b)` 转换 | 每条记录都转一次 | 第 13 章 |
| 5 | `range` 大结构体用索引或指针 | `for _, big := range items` 每次拷贝 200 字节以上 | 第 4、14 章 |
| 6 | 高频小对象复用 | 每请求新建 >10KB 的 buffer（用 `sync.Pool` 或 `bytes.Buffer` 复用） | 第 22、30 章 |
| 7 | 反射只在启动期 | `json.Marshal` 之类在热路径每次反射（`encoding/json` 有字段缓存，问题不大；自研反射要缓存） | 第 37 章 |
| 8 | 逃逸可控 | `-gcflags=-m` 里热路径大量 `moved to heap`（不是都要修，但要知道） | 第 14 章 |
| 9 | 连接池参数推导过 | `http.Transport` 用默认值（`MaxIdleConnsPerHost=2`）；`database/sql` 没设 `SetMaxOpenConns` | 第 32、38 章 |
| 10 | `resp.Body` 必关 | `defer resp.Body.Close()` 缺失；或读完不复用（要 `io.Copy(io.Discard, ...)`） | 第 38 章 |
| 11 | 大对象及时降引用 | 解析 100MB 响应后把整体 byte 留在长生命周期 struct 里 | 第 15、28 章 |
| 12 | 优化有台账 | 改完没跑 `benchstat`，没有 before/after 数据 | 第 29、30 章 |

> ⚠️ **Go 特有的第一条纪律：先测量**。Go 的对象很小、分配很快、GC 很猛，所以「Java 直觉里的性能瓶颈」经常不成立（比如「对象池一定更快」——小对象池化常常输给 GC）。**没有 profile 的优化提案，一律打回。**

---

## 54.7 可观测清单（8 条）

| # | 检查项 | 判断标准 | 详见 |
| --- | --- | --- | --- |
| 1 | 日志走 `log/slog`，结构化 | 还在用 `log.Println("user " + id + " failed")` | 第 35 章 |
| 2 | 一条请求一条链路 ID | 日志里没有 `trace_id` / `request_id`，串不起一次故障 | 第 24、35 章 |
| 3 | ctx 里带 logger / trace | 靠全局 logger + 手动传参 | 第 35 章 |
| 4 | 指标覆盖「四黄金信号」 | 缺延迟分位、错误率、饱和度（in-flight / 队列深度）之一 | 第 35、52 章 |
| 5 | 延迟用直方图不用平均值 | 只有 `avg_latency`，看不到 P99 | 第 35 章 |
| 6 | span 不装大字段 | prompt / 工具返回体进了 span attribute | 第 52 章 |
| 7 | 日志量受控 | 请求级 `INFO` 全开、大字段不截断 | 第 52 章 |
| 8 | 有 `/healthz` 与 `/metrics` 且语义正确 | liveness 查了 DB；`/metrics` 被 Ingress 暴露 | 第 52 章 |

---

## 54.8 API 演进与兼容

Go 这边有两套「API」要维护：**库 API（导出符号）** 和 **HTTP API**。破坏性变更的规则不一样。

| 变更类型 | 破坏性 | 处理方式 |
| --- | --- | --- |
| 库：新增导出函数/类型/字段 | ❌ 不破坏 | 直接加，注释里写 `// Deprecated:` 的前置说明 |
| 库：**给导出 struct 加字段** | ⚠️ 半破坏 | 用无 key 字面量 `T{1, 2}` 的调用方会编译失败 → 对外 struct 一律用 key 字面量，或用 functional options |
| 库：改函数签名 | ✅ 破坏 | 加新函数（`NewV2` / 新名字），旧函数保留并 `// Deprecated:` |
| 库：删导出符号 | ✅ 破坏 | 至少一个大版本 + `// Deprecated: use X since v1.8, removed in v2.` |
| 库：接口加方法 | ✅ 破坏（所有实现方） | 用「小接口 + 组合」，或在新接口里加（`Reader` → `ReaderAt` 各管一摊） |
| 模块：不兼容变更 | ✅ | 提升主版本，import 路径加 `/v2`（第 33 章） |
| HTTP：新增可选请求字段 | ❌ | 服务端忽略未知字段（默认行为） |
| HTTP：新增响应字段 | ❌ | 客户端必须容忍未知字段（不要用 `DisallowUnknownFields` 解别人给的响应） |
| HTTP：改字段类型 / 必填性 | ✅ | 加新字段 + 双写过渡，旧字段标 `Deprecated` + `Sunset` header |
| HTTP：删字段 / 改语义 | ✅ | expand → migrate → contract 三段式，跨两个发布周期 |

```go
// 库 API 演进的标准姿势：functional options，加参数永不破坏
type Option func(*Server)

func WithTimeout(d time.Duration) Option { return func(s *Server) { s.timeout = d } }
func WithLogger(l *slog.Logger) Option   { return func(s *Server) { s.logger = l } }

func New(addr string, opts ...Option) *Server {
	s := &Server{addr: addr, timeout: 30 * time.Second, logger: slog.Default()}
	for _, o := range opts {
		o(s)
	}
	return s
}

// 弃用一个导出函数：注释必须告诉人「用什么替代」
// Deprecated: use NewClientWithOptions since v1.9; removed in v2.
func NewClient(addr string) *Client { return New(addr) }
```

> 💡 **契约测试比文档靠谱**：给 HTTP API 写一组「消费者契约」测试（用 golden JSON 断言响应结构），给库写 `example_test.go`（`ExampleXxx` 会出现在 godoc 里，且**编译不过 CI 就红**）。这两样加一起，能挡住 90% 的无意破坏。

---

## 54.9 团队协作：gofmt + CI + PR 模板

### 54.9.1 工具链（三件套，一次配好，永不争论）

| 工具 | 作用 | 是否可争论 |
| --- | --- | --- |
| `gofmt` / `goimports` | 格式化与 import 分组 | **零争议**，CI 里直接 `gofmt -l .` 报错 |
| `go vet` + `staticcheck` + `golangci-lint` | 静态检查 | 少量规则可关，但必须写进配置文件 |
| `go test -race` + `govulncheck` | 正确性与漏洞 | **必跑**，不允许 local pass / CI skip |

`.golangci.yml`（最小可用版；注意 golangci-lint 1.x 与 2.x 的配置 schema 不同，2.x 需要 `version: "2"`，用 `golangci-lint migrate` 迁移你本地的版本）：

```yaml
run:
  timeout: 5m
  tests: true

linters:
  enable:
    - govet
    - staticcheck
    - errcheck
    - ineffassign
    - unused
    - bodyclose        # resp.Body 没关
    - copyloopvar       # 1.22 之后残留的循环变量拷贝
    - errorlint         # %v vs %w、err == 比较
    - contextcheck      # ctx 断链
    - nilerr            # if err != nil { return nil }
    - gosec             # 安全扫描
    - sqlclosecheck     # rows/Stmt 没关
    - rowserrcheck      # rows.Err() 没检查
    - noctx             # http.Get 之类的无 ctx 调用
    - misspell
    - revive

linters-settings:
  errcheck:
    check-blank: true   # _ = f() 也要报，强制加注释说明
  gosec:
    excludes: [G404]    # 允许 math/rand 用于非安全场景（按需）

issues:
  exclude-use-default: false
  max-issues-per-linter: 0
  max-same-issues: 0
```

### 54.9.2 CI：四个 job，一个都不许跳

```yaml
# .github/workflows/ci.yml
name: ci
on:
  pull_request:
  push: { branches: [main] }

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with: { go-version: "1.25", cache: true }
      - name: gofmt
        run: |
          out=$(gofmt -l .); if [ -n "$out" ]; then echo "gofmt needed:"; echo "$out"; exit 1; fi
      - run: go vet ./...
      - uses: golangci/golangci-lint-action@v6
        with: { version: latest }

  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with: { go-version: "1.25", cache: true }
      - run: go test -race -shuffle=on -coverprofile=cover.out ./...
      - run: go tool cover -func=cover.out | tail -1

  vuln:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with: { go-version: "1.25", cache: true }
      - run: go install golang.org/x/vuln/cmd/govulncheck@latest
      - run: govulncheck ./...

  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with: { go-version: "1.25", cache: true }
      - run: CGO_ENABLED=0 go build -trimpath -o /dev/null ./...
```

`-shuffle=on` 是低成本高收益的一项：它能抓出「测试之间互相依赖」的隐形耦合。

### 54.9.3 PR 模板（直接放进 `.github/pull_request_template.md`）

```markdown
## 这个 PR 做了什么
<!-- 一句话；如果是修 bug，附现象与根因 -->

## 影响面
- [ ] 改了对外 HTTP API（破坏性？____）
- [ ] 改了导出库 API（破坏性？____）
- [ ] 改了 DB schema / 迁移（向后兼容？____）
- [ ] 改了并发路径（新增 goroutine？____ 退出条件是什么？____）
- [ ] 改了配置项 / 环境变量（文档同步？____）

## 自查（照第 54 章清单过一遍）
- [ ] `gofmt` / `go vet` / `golangci-lint` 本地通过
- [ ] `go test -race ./...` 通过；新增逻辑有表驱动测试
- [ ] 新增的每个 `go func()` 都有退出条件，且有 owner（errgroup / ctx / done）
- [ ] 错误有 `%w` 包装，没有 `%v`，没有吞错
- [ ] 新接口定义在**使用方**包，方法数 ≤3
- [ ] 新配置项有默认值 + 校验 + 写进 README
- [ ] 日志结构化，含 request_id；大字段已截断
- [ ] 没有为分层而分层的新包 / 新 interface

## 怎么验证
<!-- 测试步骤、截图、压测数据、灰度计划 -->
```

### 54.9.4 三条团队级约定

1. **PR 小于 400 行**（不含生成代码）。超了就拆——Go 代码 review 的瓶颈是「在脑子里跑并发」，大 PR 等于没 review。
2. **一个概念一处实现**：公共工具进 `internal/pkg`，禁止「两个 utils 包」。这条能省掉半年后 80% 的「到底该用哪个」。
3. **规范即代码**：所有约定要么进 `.golangci.yml`、要么进 CI、要么进 PR 模板。**只写在 wiki 里的规范等于不存在。**

---

## 54.10 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 三次删除 | 删掉这层会不会变差 → 只有一个实现吗 → 接口该归谁 |
| 分层与接口 | 按依赖方向分 2~3 层；接口由使用方定义、≤3 方法、一个实现就别抽象 |
| 一个 struct 到底 | 不要 DTO/VO/Entity 三件套；变形用函数不用类型 |
| 并发第一问 | 「这个 `go` 什么时候结束、谁取消它」 |
| `-race` | 必跑，但它抓不到 goroutine 泄漏与「没竞争的逻辑错」 |
| 错误 | `%w` 包装、只在进程边界打日志、库不 panic |
| 性能 | 没有 pprof / benchstat 的优化一律打回 |
| 可观测 | 结构化日志 + trace_id + 直方图 + 四黄金信号 |
| API 演进 | 库用 options + `/v2`；HTTP 用 expand→migrate→contract |
| 团队协作 | gofmt 无争议、CI 四 job、PR 模板自查、PR < 400 行 |

## 54.11 自检题

**1. 同事说：「这个 `UserService` 接口将来可能要换成别的实现，所以先抽出来。」你怎么用一句话反驳，并给出替代方案？**

<details><summary>答案要点</summary>

- 反驳：「将来可能有第二个实现」不是抽象的理由，**「现在已经有了第二个，或者三个月内有明确排期」才是**。抽象的成本是永久性的（每次改都要同步），收益是概率性的。
- 替代方案：① 先写具体结构体 `*UserService`；② 真正出现替换需求时，在**使用方**（比如 handler 包）声明一个只含所需方法的小接口；③ 直接用 Go 的隐式实现，提供方一行都不用改。
- 附带一问：删掉这个 interface，需要改几行？答案是 0 就是噪音。

</details>

**2. 你的 `-race` 全绿、`go vet` 全绿，能证明服务没有并发问题吗？请举出至少两类漏掉的 bug。**

<details><summary>答案要点</summary>

- 不能。`-race` 只检测**运行时实际发生**的竞争，没走到的分支不报。
- 漏掉类型一：**goroutine 泄漏**（无退出条件、无缓冲 channel 阻塞、ctx 从未取消）——没有数据竞争，只有资源不回收。用 goroutine profile / `runtime.NumGoroutine()` 监控抓。
- 漏掉类型二：**逻辑竞态但每次都有锁**（比如「先查后写」的两步操作中间被别人插队，每次都加锁但不原子）——需要改成 single-writer / CAS / 事务。
- 漏掉类型三：**锁顺序不一致导致的偶发死锁**、**panic 后未释放锁**（要用 `defer mu.Unlock()`）。
- 所以第 54.4 的 20 条里有 6 条是工具抓不到的，必须人眼过。

</details>

**3. 团队想把 `GET /v1/orders` 的 `amount` 从「元（float）」改成「分（int）」，怎么发布才不炸客户端？**

<details><summary>答案要点</summary>

- 直接改 = 破坏性变更，客户端解析会静默出错（float→int 解析失败或精度错），必须走三段式：
- ① **expand**：响应里**同时**返回 `amount`（保留元）和新字段 `amount_cents`（int）；文档标注 `amount` 即将弃用；给响应加 `Deprecation` / `Sunset` header（RFC 8594 / RFC 9745）。
- ② **migrate**：监控 `amount` vs `amount_cents` 的消费情况（埋点/访问日志），推动客户端切换；通常给两个发布周期。
- ③ **contract**：确认无人消费后再删 `amount`，并在 CHANGELOG 与版本号里明确。
- 库侧的等价做法：新增 `AmountCents int64` 字段 + 旧字段标 `// Deprecated:`，主版本升到 `/v2` 时再删。

</details>

## 54.12 推荐书籍与源码清单（按顺序读）

| # | 材料 | 为什么读它 | 读法 |
| --- | --- | --- | --- |
| 1 | 《The Go Programming Language》(Donovan & Kernighan) | 唯一一本「讲清楚语言设计意图」的书，接口与并发两章值得逐字读 | 精读第 6、7、8、9 章 |
| 2 | *Effective Go*（官方） | 命名、注释、接口惯例的权威来源 | 通读一遍，写代码前再刷 |
| 3 | *Go Code Review Comments*（官方 wiki） | **本章 54.3~54.7 的原始出处**，一条一条都是 CR 依据 | 当字典用 |
| 4 | *Go 语言设计与实现*（dravenesque） | 数据结构与 runtime 的中文系统梳理，和图解互补 | 对照本书第 11~28 章读 |
| 5 | 《100 Go Mistakes and How to Avoid Them》(Teiva Harsanyi) | 100 个具体坑，按「并发 / 错误 / 内存」分类，团队共读效果最好 | 每周 10 条，读完开会讨论 |
| 6 | Google SRE Book + Workbook | SLO / 错误预算 / 告警降噪 / 复盘的方法论源头（第 52 章） | 精读 Workbook 第 2、5 章 |
| 7 | `$GOROOT/src` 里的 `runtime/proc.go`、`runtime/chan.go`、`sync/mutex.go`、`net/http/server.go` | 比任何书都准确，且注释写得极好 | 配合第 17~23、38 章精读 |
| 8 | `io`、`context`、`errors`、`slog` 四个标准库包的文档 | 把「小接口」这件事看透，你的 API 设计就会变好 | 全读，各约 20 分钟 |
| 9 | langchaingo / eino / MCP Go SDK / gin / kratos 源码 | 第 48~51 章的对象，也是「Go 项目目录长什么样」的答案 | 按第 51 章方法论读 |
| 10 | *Simplicity is Complicated*（Rob Pike）、*Go at Google*（演讲） | 理解「为什么 Go 故意少」，能帮你在设计评审时守住底线 | 各 30 分钟 |

> 🎯 最后一句：**这本书讲的每一条，最终都收敛成一件事——让你的代码在半年后、在凌晨三点、在别人的手里，依然能被读懂、能被打断、能被替换。** 这就是 Go 的全部野心。

---

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 53 章 综合项目：从零构建生产级 Agent 框架](53-build-agent-framework.md)
- 相关：[第 1 章 心智模型重置](1-mindset-shift.md)（本章的价值观底稿）、[第 5 章 接口底层](5-interface-internals.md)、[第 7 章 错误处理](7-error-handling.md)、[第 25 章 并发陷阱](25-concurrency-pitfalls.md)、[第 26 章 结构化并发](26-structured-concurrency.md)、[第 33 章 模块与 MVS](33-modules-mvs.md)、[第 34 章 测试](34-testing.md)、[第 35 章 可观测性](35-observability.md)、[第 52 章 生产部署与 SRE](52-deployment-sre.md)
- 下一章：（最终章，回到 [总纲](0-summary.md) 复习路线图）
