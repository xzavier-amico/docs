# 34. 测试：表驱动、mock、fuzz 与可重放

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 工程化与服务端 ｜ **编号**：34 / 54
> **本章定位**：把「JUnit + Mockito + WireMock + Testcontainers」这套 Java 测试观，翻译成 Go 的「标准库 + 表驱动 + httptest + 录制回放」。重点解决一件事：**当你的被测代码要调一个又贵、又慢、又非确定性的 LLM 时，怎么测。**
> **读法**：34.3 讲 `testing` 包内部机制（为什么 `t.Cleanup` 比 `defer` 好），34.5 是可复制的四份模板，34.6 是 CR 清单。

---

## 34.1 引子：一个「过了三天就没人敢跑」的测试目录

你的 Agent 服务上线了，`internal/` 下有 12 个包，测试文件只有 3 个。CI 情况是这样的：

- 有一个 `TestAgentLoop` 直接打真实 OpenAI 接口：**每次跑烧 0.3 美元**，还随机 429 失败，团队只好把它注释掉——注释掉的那一刻，这个测试就死了。
- 有一个 `TestRAGPipeline` 需要本地 PostgreSQL + pgvector，新人入职第一天就问「测试怎么跑不了」。
- 剩下的测试全是 `TestFoo(t *testing.T) { ... }` 里 60 行 `if got != want { t.Errorf(...) }`，想加一个 case 就复制 50 行。
- 上周 SSE 流式输出出错，日志里是 `data:` 被 TCP 分包切断了。你想加回归测试，但**不知道从哪下手**。

这些不是「团队不重视测试」，而是**四个工具没装**：表驱动（解决 case 爆炸）、`httptest` + 录制回放（解决外部依赖）、`t.Cleanup/t.TempDir`（解决夹具泄漏）、CI 矩阵（解决「本地绿 CI 红」）。

---

## 34.2 直觉模型：`go test` 是一台「按约定收集函数」的机器

Java 的 JUnit 靠注解发现测试方法；Go **没有任何注解**，靠的是命名约定 + 生成一个假的 `main`：

```
go test ./internal/llm
   │
   ├─ 编译：internal/llm 包 + 一个自动生成的 _testmain.go
   │         _testmain.go 里调用 testing.MainStart(测试表, 基准表, fuzz表, 示例表)
   │         测试表 = []InternalTest{ {Name:"TestChat", F:Fuzzless(TestChat)}, ...}
   │         ← 收集方式：反射扫描所有 "TestXxx(t *testing.T)" 函数
   │
   ├─ 运行：M.Run() → 每个 *T 派生一个 goroutine 跑 tRunner(t, fn)
   │
   └─ 汇总：输出 PASS/FAIL、覆盖率、JSON（-json）
```

所以 Go 的测试就是**普通函数 + 普通包**：没有 `@BeforeEach`、没有全局测试上下文、测试代码就是业务代码的一部分（所以它也会被 `go vet`、也会被 gofmt、也会进覆盖率）。

| JUnit 5 | Go | 会猜错的地方 |
| --- | --- | --- |
| `@Test` | `func TestXxx(t *testing.T)` | 必须是这个签名，否则**静默不执行** |
| `@ParameterizedTest` + `@MethodSource` | `for _, tc := range cases { t.Run(tc.name, ...) }` | 没有参数化框架，表驱动是唯一正统写法 |
| `@BeforeEach` / `@AfterEach` | `t.Cleanup(func(){...})`（LIFO） | 没有「每方法前置」钩子，靠构造函数或 `t.Cleanup` 成对写 |
| `@BeforeAll` / `@AfterAll` | `TestMain(m *testing.M)` | 全包唯一，且要自己 `os.Exit(m.Run())` |
| `@Nested` | `t.Run` 嵌套 | 子测试可独立 `-run` 过滤、可独立 `t.Parallel` |
| `assertThrows` | 显式 `err` 检查 + `errors.Is` | Go 的失败是返回值，不用 try/catch 包 |
| `@Mock` / Mockito | gomock / mockery 生成 + 接口 | **只能 mock 接口**，不能 mock struct、不能 mock 静态方法 |
| `@InjectMocks` | 无 | 手工构造被测对象，把 mock 传进构造函数 |
| WireMock | `httptest.NewServer` | 标准库自带，不需要额外框架 |
| JaCoCo | `go test -coverprofile` + `go tool cover` | 语言内置，无需插件 |

> ⚠️ **第一个必踩的坑**：`go test` 收集测试函数用的是**反射 + 名字前缀**。`Testfoo` 不会跑，`TestFoo(t *testing.T, x int)` 也不会跑，`TestFoo` 放在 `_test.go` 之外的文件里也更不会跑。它们不会报错，只会安静地消失。

---

## 34.3 底层实现：`testing.T` 里到底有什么

`testing/testing.go` 里 `T` 的定义（简化，字段名真实）：

```go
type T struct {
	common                    // 与 B、F 共享
	context  *testContext     // 由 -run 匹配后的 testContext；nil 表示在用父的
	sub      []*T             // 子测试列表（t.Run 创建的）
	parent   *T
	...
}

type common struct {
	mu       sync.RWMutex
	output   []byte           // 缓冲的输出；-v 之前不打印
	w        io.Writer
	failed   bool
	skipped  bool
	done     bool
	helperPCs map[uintptr]struct{}   // t.Helper() 记录"这是辅助函数"
	cleanup  []func()                // t.Cleanup 压栈
	cleanupName []string
	...
}
```

四个关键机制，理解它们你就不会再写错：

**① 每个测试跑在独立 goroutine 里**。`tRunner`（`testing.go`）负责这件事：

```go
func tRunner(t *T, fn func(t *T)) {
	defer func() { /* 处理 panic → 标记失败 → 跑 cleanup → 通知父 */ }()
	...
	fn(t)          // 你的测试函数在这里被调用
	...
	t.runCleanup(recoverAndReturnPanic)
}
```

**后果**：`t.FailNow()` / `t.Fatal()` / `t.Fatalf()` 内部是 `runtime.Goexit()`——它只结束**当前 goroutine**。你在测试里 `go func(){ t.Fatal(...) }()`，那个 goroutine 死了，主测试流程继续跑，最终可能得到「PASS」或一个神秘的 `test executed panic(nil) or runtime.Goexit`。**规则：`t.Fatal` 只在测试主 goroutine 里用；子 goroutine 里只能 `t.Error` + `return`。**

**② `t.Cleanup` 是 LIFO 的栈，且由 `tRunner` 在测试函数返回（或 panic 后）统一执行**。这比 `defer` 强在三点：写在辅助函数里也能生效（`defer` 会跟着辅助函数返回就跑掉）；多个 `t.Run` 共享父级的清理；`t.TempDir()` 就是靠它实现的：

```go
func (c *common) TempDir() string {
	dir, err := os.MkdirTemp("", "Test"+...)
	c.Cleanup(func() {           // 自动清理，且失败重试
		for i := 0; i < 5; i++ {
			if err := os.RemoveAll(dir); err == nil { return }
		}
	})
	return dir
}
```

**③ `t.Helper()` 只是把「调用者的 PC」登记进 `helperPCs`**。它不改变控制流，唯一作用是**报错时跳过这一帧**，让失败行号指向真正的断言处而不是你的 `mustJSON` 工具函数。写任何测试辅助函数，第一行都应该是 `t.Helper()`。

**④ `t.Parallel()` 的语义是「暂停当前测试，让父测试继续启动其他子测试，等父测试全部启动结束后再并行跑我们」**。实现上是 `t.signal <- true` 之后 `<-t.parent.barrier` 的一次汇合。所以：

```go
for _, tc := range cases {
	tc := tc                 // Go 1.22+ 不再需要，但保留无害
	t.Run(tc.name, func(t *testing.T) {
		t.Parallel()         // 在这里调用：所有子测试会先全部注册，再一起并发跑
		...
	})
}
```

一旦并行，测试之间**就不能共享可变状态**（同一个 `*fakeRepo` 实例、同一个环境变量）。`t.Setenv`（1.17+）会自动注册清理，但如果测试 `t.Parallel()` 了，`t.Setenv` 会直接 panic——这是标准库帮你防的。

**Fuzz 的实现**（1.18 起的 `internal/fuzz`）：`f.Fuzz(fn)` 用反射解析 `fn` 的参数类型（支持 `string/[]byte/整数/float64/bool`），把种子和语料喂进去；`-fuzz` 模式下启动 **worker 子进程**，对每个输入插桩统计覆盖率，从覆盖率增长处变异输入（libFuzzer 思路）。语料落在 `testdata/fuzz/FuzzXxx/` 下，**要提交**。

---

## 34.4 Java 对照：为什么 Go 很少用「mock 框架」

这一条是 Java 人最容易翻车的：**你在 Go 里找不到 Mockito 的等价物，因为 Go 不需要。**

Mockito 靠字节码生成（ByteBuddy/cglib）在运行期给具体类造子类。Go 没有运行期代码生成能力（反射不能造类型），也**没有子类型多态**可以拦截方法调用。但 Go 换了个更便宜的路：**接口是隐式实现的**。你只要在使用方定义一个小接口，手写 10 行 fake 就行：

```go
// 使用方（agent 包）自己定义它需要什么，不需要 provider 参与
type Model interface {
	Chat(ctx context.Context, msgs []Message) (Reply, error)
}

// 测试里手写 fake —— 比 Mockito 短，但零魔法、能断点、能 t.Log
type fakeModel struct{ reply Reply; err error; calls int }

func (f *fakeModel) Chat(ctx context.Context, msgs []Message) (Reply, error) {
	f.calls++
	return f.reply, f.err
}
```

什么时候才上 gomock/mockery？**当接口多、调用期望复杂（要断言「第 3 次调用参数是 X」）、或者接口在别人的包里不好改时**。判断标准：手写 fake 超过 80 行 → 上生成器。

---

## 34.5 工程落地：四份可以整段抄的模板

### 34.5.1 表驱动 + 子测试 + 夹具三件套

```go
package agent

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestParseToolCall(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name    string
		in      string
		wantFn  string
		wantErr error // 用哨兵错误比较，别比较错误字符串
	}{
		{name: "正常单工具", in: `{"name":"search","arguments":{"q":"go"}}`, wantFn: "search"},
		{name: "空 name", in: `{"name":"","arguments":{}}`, wantErr: ErrToolNameEmpty},
		{name: "非 JSON", in: `{oops`, wantErr: ErrBadJSON},
		{name: "arguments 是字符串", in: `{"name":"search","arguments":"{}"}`, wantErr: ErrBadJSON},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel() // 与同层子测试并发；因此下面不能用共享可变状态

			got, err := ParseToolCall([]byte(tc.in))
			switch {
			case tc.wantErr != nil:
				if !errors.Is(err, tc.wantErr) {
					t.Fatalf("err = %v, want %v", err, tc.wantErr) // 主 goroutine，可以 Fatal
				}
				return
			case err != nil:
				t.Fatalf("unexpected err: %v", err)
			}
			if got.Name != tc.wantFn {
				t.Errorf("name = %q, want %q", got.Name, tc.wantFn)
			}
		})
	}
}

// 用 t.TempDir + t.Cleanup 管理「会话落盘」这类夹具
func TestSessionStore(t *testing.T) {
	dir := t.TempDir()                     // 测试结束自动 RemoveAll，无需 defer
	path := filepath.Join(dir, "sessions.json")

	st, err := NewFileStore(path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { // LIFO：后注册的先跑
		if err := st.Close(); err != nil {
			t.Errorf("close: %v", err) // 清理里也用 t.Errorf，别 panic
		}
	})

	if err := st.Put(context.Background(), "s1", Session{ID: "s1"}); err != nil {
		t.Fatalf("put: %v", err)
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("file not created: %v", err)
	}
}
```

两个细节：错误用**哨兵值 + `errors.Is`** 比较，而不是字符串（详见 [第 7 章](7-error-handling.md)）；`t.Cleanup` 里的失败用 `t.Errorf`，因为此时 `runtime.Goexit` 已经不该再用了。

### 34.5.2 `httptest`：把 handler 和中间件一起测

```go
package httpapi

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestChatHandler(t *testing.T) {
	h := WithRequestID(WithRecover(http.HandlerFunc(handleChat)))

	// recorder 版本：不起端口，纯内存，最快，适合单测
	req := httptest.NewRequest(http.MethodPost, "/v1/chat", strings.NewReader(`{"q":"hi"}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	if rec.Header().Get("X-Request-Id") == "" {
		t.Error("request id 中间件没生效")
	}

	// server 版本：真起一个 listener，适合测超时、连接复用、流式 flush
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close) // 用 srv.Client() 才不会漏 cookie/超时配置

	resp, err := srv.Client().Post(srv.URL+"/v1/chat", "application/json", strings.NewReader(`{"q":"hi"}`))
	if err != nil {
		t.Fatalf("post: %v", err)
	}
	defer resp.Body.Close()
}
```

### 34.5.3 录播：用 httptest + golden SSE 测 LLM

这是本章最有价值的一段。核心思路：**LLM 客户端的 HTTP 出口是一个接口（`http.RoundTripper`），换掉它就是录制/回放。**

```go
// replay.go（生产代码同包，用 build tag 也可以）
package llm

import (
	"bytes"
	"io"
	"net/http"
	"os"
)

// ReplayTransport 从 testdata 里读已录制的响应体
type ReplayTransport struct {
	Dir string               // 例如 testdata/recorded
	Req *http.Request        // 记录最后一次请求，供断言
}

func (t *ReplayTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	t.Req = req
	// 文件名：方法_路径主语.golden，例如 post_v1_chat.golden
	body, err := os.ReadFile(t.Dir + "/" + goldenName(req))
	if err != nil {
		return nil, err
	}
	return &http.Response{
		StatusCode: 200,
		Header:     http.Header{"Content-Type": {"text/event-stream"}},
		Body:       io.NopCloser(bytes.NewReader(body)),
		Request:    req,
	}, nil
}
```

`testdata/recorded/post_v1_chat.golden` 就是**原样保存的 SSE 字节流**，包含分包被切断的形态：

```text
data: {"choices":[{"delta":{"content":"你"}}]}

data: {"choices":[{"delta":{"content":"好"}}]}

data: {"choices":[{"delta":{"content":"！"}}],"usage":{"prompt_tokens":12,"completion_tokens":3}}

data: [DONE]
```

测试这样写：

```go
func TestStreamChat_Replay(t *testing.T) {
	rt := &ReplayTransport{Dir: "testdata/recorded"}
	c := New("sk-fake", WithTransport(rt)) // 生产 client 构造器的 functional option

	var got []string
	err := c.StreamChat(context.Background(), []Message{{Role: "user", Content: "hi"}},
		func(delta string) error { got = append(got, delta); return nil })
	if err != nil {
		t.Fatalf("stream: %v", err)
	}
	if strings.Join(got, "") != "你好！" {
		t.Fatalf("delta = %v", got)
	}
	if !strings.Contains(rt.Req.URL.Path, "/v1/chat/completions") {
		t.Errorf("path = %s", rt.Req.URL.Path)
	}
}
```

**录制**怎么做：加一个 build tag 为 `record` 的 `RoundTripper`，在真实请求返回后把 `resp.Body` 读出来 `os.WriteFile` 到 `testdata/recorded/`。`go test -tags=record ./...` 只在你手动刷新语料时跑；平日 CI 用回放，**零成本、零 flaky、能测分包**。

> 💡 golden 文件的通用更新姿势：`var update = flag.Bool("update", false, "...")`，在 `TestMain` 里解析，测试里 `if *update { os.WriteFile(...) }`。**CI 上永远不加 `-update`**，否则测试永远绿。

### 34.5.4 fuzz：解析器是你最该 fuzz 的地方

工具调用的参数解析、SSE 解码、JSON Schema 校验——全是「吃不可信输入」的代码，天然适合 fuzz：

```go
func FuzzParseToolCall(f *testing.F) {
	// 种子：正常 + 几个恶意输入，会进 testdata/fuzz 语料库
	f.Add([]byte(`{"name":"search","arguments":{"q":"go"}}`))
	f.Add([]byte(`{}`))
	f.Add([]byte(`{"name":"x","arguments":`))

	f.Fuzz(func(t *testing.T, data []byte) {
		got, err := ParseToolCall(data)
		if err != nil {
			return // 出错是允许的，只要不 panic
		}
		// 不变量：解析成功的 name 一定非空、一定不超长
		if got.Name == "" || len(got.Name) > 128 {
			t.Fatalf("bad name %q from %q", got.Name, data)
		}
	})
}
```

```bash
go test -fuzz=FuzzParseToolCall -fuzztime=60s ./internal/agent   # 找崩溃，写入 testdata/fuzz
go test ./internal/agent                                        # 平日常规跑语料库
```

### 34.5.5 Testcontainers-go：真数据库，但不用手工起

```go
//go:build integration

func TestVectorSearch(t *testing.T) {
	ctx := context.Background()
	pg, err := postgres.Run(ctx, "pgvector/pgvector:pg16",
		postgres.WithDatabase("test"),
		postgres.WithInitScripts("testdata/init.sql"),
		testcontainers.WithWaitStrategy(wait.ForLog("database system is ready to accept connections")),
	)
	if err != nil {
		t.Skipf("docker 不可用，跳过：%v", err)
	}
	t.Cleanup(func() { _ = pg.Terminate(ctx) })

	dsn, _ := pg.ConnectionString(ctx, "sslmode=disable")
	// ... 用 dsn 建连接池，跑你的向量检索断言
}
```

配 `//go:build integration` 是纪律：**单测必须能在无 Docker 环境跑**，集成测试用 `go test -tags=integration ./...` 单独在 CI 的 privileged runner 上跑。

### 34.5.6 覆盖率与 CI 矩阵

```yaml
name: test
on: [push, pull_request]
jobs:
  unit:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        go: ['1.24', '1.25']
        os: [ubuntu-latest, macos-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with: { go-version: '${{ matrix.go }}', cache: true }
      - run: go vet ./...
      - run: go test -race -shuffle=on -count=1 -covermode=atomic \
               -coverpkg=./internal/... -coverprofile=cover.out ./...
      - run: go tool cover -func=cover.out | tail -1
      # 覆盖率门槛：低于 70% 直接红（阈值按团队实际定）
      - run: |
          pct=$(go tool cover -func=cover.out | tail -1 | grep -oE '[0-9.]+%' | tr -d '%')
          awk -v p="$pct" 'BEGIN{ if (p < 70) { print "coverage too low: " p; exit 1 } }'
  integration:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with: { go-version: '1.25' }
      - run: go test -tags=integration -timeout=10m ./...   # Testcontainers 需要 docker
  fuzz:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with: { go-version: '1.25' }
      - run: go test -run=^$ -fuzz=FuzzParseToolCall -fuzztime=120s ./internal/agent
```

`-count=1` 破缓存、`-shuffle=on` 打乱顺序暴露隐式依赖、`-race` 抓数据竞争（详见 [第 25 章](25-concurrency-pitfalls.md)）、`-covermode=atomic` 是并发热点下正确的计数方式。

---

## 34.6 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| 子 goroutine 里 `t.Fatal` 后测试莫名 PASS / 报 `runtime.Goexit` | `Fatal` 只终止当前 goroutine | 子 goroutine 用 `t.Error` + `return`，或把错误回传主 goroutine | 34.3 |
| 测试里的 `t.Fatal` 报错行号指向工具函数 | 辅助函数没标 `t.Helper()` | 所有 helper 第一行 `t.Helper()` | 34.3 |
| 临时文件/端口/容器泄漏，随机挂掉 | 用 `defer` 但函数提前 `t.Fatal` 时部分路径跳过 | 一律 `t.Cleanup`，它由 `tRunner` 兜底执行 | 34.3 |
| `t.Parallel()` + 共享 `fakeDB` → 竞态/串数据 | 并行测试共享可变状态 | 每个子测试自建夹具；用 `-race` 抓 | 34.3 |
| `t.Parallel()` 里调用 `t.Setenv` 直接 panic | 标准库禁止 | 环境变量相关的测试别并行 | 34.3 |
| `go test ./...` 显示 `(cached)`，改了半天以为没生效 | 测试缓存按输入哈希 | 调试时 `-count=1` | 34.5.6 |
| golden 测试永远绿，其实输出早就变了 | CI 上跑了 `-update` | 更新开关默认 false，CI 显式不传；再加一道 `git diff --exit-code` | 34.5.3 |
| CI 上 mock 文件 `undefined: MockFoo` | 生成物没提交，或生成物与接口漂移 | 生成物提交 + CI 里重跑 `go generate ./... && git diff --exit-code` | 34.5.1 |
| 单测依赖本地 Postgres / Docker，新人跑不起来 | 集成测试没打 build tag | `//go:build integration` + `testing.Short()` 双重闸 | 34.5.5 |
| 时间相关的测试 flaky（TTL、退避、超时） | 真实 `time.Sleep` | 注入 `Clock` 接口（fake clock），或 Go 1.25+ 试 `testing/synctest` | 34.5.1 |
| `httptest.NewServer` 忘了 Close → fd 耗尽 | 循环里反复 NewServer | `t.Cleanup(srv.Close)`，并只用 `srv.Client()` | 34.5.2 |
| 断言 mock 调用次数过细，重构就红一片 | 过度断言实现细节 | 优先断言**行为/输出**，只在必要时断言交互 | 34.4 |

---

## 34.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 命名约定 | `TestXxx(t *testing.T)` / `BenchmarkXxx(b *testing.B)` / `FuzzXxx(f *testing.F)` / `ExampleXxx()` |
| 表驱动 | `for + t.Run(name)`，加 case 只加一行，故障可单点 `-run` 重放 |
| `t.Cleanup` | LIFO 栈，由 `tRunner` 兜底执行；比 `defer` 安全 |
| `t.TempDir` | 自动创建 + 自动清理 + 失败重试，占位夹具首选 |
| `t.Helper` | 只为让报错行号指向调用者，纯元数据 |
| `t.Fatal` 边界 | 只能在测试主 goroutine；子 goroutine 用 `t.Error` |
| mock | 手写 fake 优先（接口由使用方定义）；复杂期望才上 gomock/mockery |
| `httptest` | `NewRecorder` 快（无端口）、`NewServer` 真（测超时/流/flush） |
| 录播 | 换 `http.RoundTripper` 即回放；SSE 用 golden 字节流，能测分包 |
| golden | 只做「字节级比较」，`-update` 开关永不进 CI |
| fuzz | `-fuzztime` 跑，崩溃输入自动落 `testdata/fuzz/`，语料要提交 |
| 覆盖率 | `-covermode=atomic` + `-coverpkg=./internal/...`，门槛进 CI |

## 34.8 自检题

1. **你的测试里有个辅助函数 `newTestStore(t)`，它在内部创建临时目录并注册清理。为什么应该用 `t.Cleanup` 而不是 `defer`？**
   > 答案要点：`defer` 绑定在 `newTestStore` 这个函数上，函数一返回清理就跑了——此时测试还没开始用这个 store；`t.Cleanup` 挂在 `*T` 上，由 `tRunner` 在测试函数（含 panic）结束后统一 LIFO 执行。附带好处：多个子测试可共享父测试注册的清理。

2. **给你的 LLM 客户端写测试，为什么「mock 掉 `Model` 接口」和「用 `httptest` 回放 SSE」是两件必须都做的事？**
   > 答案要点：mock 接口只能验证上层编排逻辑（Agent 循环、工具调度），测不到 HTTP 层：SSE 分包、`data:` 拼接、`[DONE]` 判断、header、重试状态码分类。回放层解决后者且零成本零 flaky；两者分层，正是「mock 接口 + fake 边界」的组合。

3. **什么情况下 `t.Parallel()` 会让测试从「偶发失败」变成「频繁失败」？怎么修？**
   > 答案要点：测试共享了可变状态（同一个 fake 实例、全局变量、`t.Setenv`、同一个临时文件路径、同一个数据库表）时，并行把偶发竞态放大成频繁失败。修法：每个子测试自建夹具、用 `t.TempDir()` 隔离路径、需要环境的测试不并行、用 `-race` 主动暴露。

## 34.9 延伸阅读

- `testing/testing.go`：`tRunner` / `common.runCleanup` / `T.TempDir` / `T.Parallel`（读一遍能省掉 80% 的测试坑）
- `testing/fuzz.go` + `internal/fuzz/fuzz.go`：覆盖率引导的 fuzz 引擎与 worker 子进程
- `net/http/httptest`：`NewRecorder`、`NewServer`、`NewTLSServer` 的实现与 `srv.Client()`
- `github.com/stretchr/testify`（`assert`/`require`/`suite`/`mock`）、`go.uber.org/mock`（gomock 的维护分支）、`github.com/vektra/mockery`、`github.com/testcontainers/testcontainers-go`
- `github.com/google/go-cmp/cmp`：结构体深度比较的事实标准（`cmp.Diff` 输出可读得离谱）
- 交叉引用：[第 31 章](31-json-codec.md) 的 `Decoder` 流式解码，是写好 SSE 回放的前提

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 33 章](33-modules-mvs.md) 模块、版本与 MVS
- 下一章：[第 35 章](35-observability.md) 可观测性：slog、OTel、Prometheus
