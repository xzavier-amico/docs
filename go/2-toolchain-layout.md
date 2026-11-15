# 2. 工程骨架：module、目录布局与工具链

> **状态**：`已完成（初稿）` ｜ **所属**：第一部分 · 心智迁移：Java → Go ｜ **编号**：2 / 54
> **本章定位**：把「Maven + Checkstyle + SpotBugs + Jib + openjdk 基础镜像」这套 Java 工程栈，一次性映射到 Go 的「`go mod` + `gofmt/vet/staticcheck` + 多阶段构建 + distroless」。读完你能从空目录开始，搭出一个明天就能进 CI/CD 的 Go 仓库。
> **读法**：先扫 `2.2` 的映射表建立坐标系；`2.3`、`2.5` 里的命令和 Dockerfile 可以直接抄；`2.6` 建议贴进团队的 CR 模板。

---

## 2.1 引子：周一上午，一个空目录和七个问题

老板说：「用 Go 把那个 LLM 网关重写一遍，两周上线。」你 `mkdir agentd && cd agentd`，然后卡住了——不是不会写 Go，而是**你熟悉的每一件工程「家具」都不在原位**：

1. 依赖在哪声明？`pom.xml` 的对应物是什么格式？为什么同事说 `go.sum` 绝对不能手改？
2. 包怎么分层？Java 的 `controller/service/repository` 三件套还要不要？为什么 Go 项目里有一堆 `internal/`？
3. 代码风格谁管？团队里 Checkstyle 那 200 条规则，在 Go 里怎么落地？——答案可能让你不舒服：**只有一条，`gofmt`，没得商量**。
4. 静态检查呢？SpotBugs/PMD 换成什么？
5. 版本号怎么注入？`application.yml` 里的 `version: @project.version@` 换成什么？
6. 交叉编译怎么搞？我要给 linux/amd64 和 linux/arm64 各出一份。
7. 镜像怎么做？`FROM openjdk:21-jre` 是 250MB 起步，Go 这边能压到多少？

这一章的答案会串成一条完整的流水线。先给你结论：**Go 的工程骨架不是一个框架，而是「约定 + 一个命令行工具」**。`go` 这个命令同时是 Maven、编译器、测试框架、包管理器、profile 工具和交叉编译器的入口，它的行为被 `go.mod` 这一份文件锚定。

> 🎯 本章的一句话立场：**Java 的工程能力来自「插件生态 + 约定」，Go 的工程能力来自「官方工具链 + 极少约定」。前者灵活但需要治理，后者死板但零争议。**

---

## 2.2 直觉模型：Maven 三件套 → Go 的「一份文件 + 一个命令」

先建坐标系。下面这张表是本章的骨架，**建议边看边对照你手里的 Java 项目**：

| Java 世界 | Go 世界 | 关键差异（会咬人的地方） |
| --- | --- | --- |
| `pom.xml` / `build.gradle` | `go.mod` | 只有十几行；不写构建脚本，构建逻辑是 `go build` 本身 |
| `mvn dependency` 拉下的 jar + 本地 `~/.m2` | 模块缓存 `$GOMODCACHE`（默认 `~/go/pkg/mod`） | 缓存的是**源码**，不是 class 文件；不可变、按版本分目录 |
| `mvnrepository` / Nexus 私服 | `GOPROXY`（默认 `proxy.golang.org`） | 协议是 HTTP，任何静态文件服务器都能当私服 |
| `pom.xml` 里的 `<dependencyManagement>` | **没有**（MVS 自动算最小可行版本） | 详见第 33 章，冲突解决哲学完全不同 |
| `SNAPSHOT` 版本 | **不存在** | 已发布的版本不可变，改了就发新版本 |
| `mvn -T 4 clean install` | `go build ./...` | 全量编译秒级完成，构建缓存内容寻址 |
| `mvnw` / JDK 版本管理 | `go.mod` 的 `go` + `toolchain` 行 | `GOTOOLCHAIN=auto` 会让 `go` 命令自动下载指定工具链 |
| Checkstyle + Spotless | `gofmt` / `goimports` | **无配置**，格式争议到此结束 |
| SpotBugs / PMD | `go vet` + `staticcheck` + `golangci-lint` | vet 是官方内置，staticcheck 是事实标准 |
| JUnit + Surefire | `go test ./...` | 测试是语言内置的，不需要依赖 |
| Jib / `spring-boot:build-image` | 多阶段 Dockerfile + distroless | 从小到大 10 倍差距，见 2.5 |
| `@project.version@` 注入 | `-ldflags "-X main.version=..."` | 链接期改字符串变量，见 2.3 |

再看目录布局。**Go 官方没有强制布局**，但社区收敛出了这么一套（`gin`、`eino`、`langchaingo`、`mcp go-sdk` 都是这个味道）：

```
agentd/
├── go.mod                      # module github.com/you/agentd
├── go.sum
├── cmd/
│   └── agentd/
│       └── main.go             # 唯一的 main：只做装配与启动
├── internal/                   # 编译器强制：外部模块 import 不进来
│   ├── agent/                  # 领域逻辑：ReAct 循环、编排
│   ├── llm/                    # provider 适配（openai / ollama）
│   ├── tool/                   # 工具注册表
│   └── httpapi/                # 路由、中间件、SSE handler
├── pkg/                        # 只在「别人要 import 你」时才存在
│   └── sseclient/
├── api/                        # openapi.yaml / proto / JSON Schema
├── testdata/                   # go 工具链会忽略的测试数据目录
├── deploy/                     # Dockerfile、k8s、helm
└── examples/                   # 可运行示例（也是最好的文档）
```

`internal/` 是 Go 唯一一条**由编译器强制执行**的布局规则：

```
github.com/you/agentd/internal/tool
        ▲                     ▲
        │ 允许                │ 禁止
        │                     │ 编译报错：
        │                     │ use of internal package ... not allowed
github.com/you/agentd/cmd/agentd      github.com/other/app
```

规则细节：路径中任何一段叫 `internal` 的目录，只允许「以该 `internal` 的父目录为前缀的包」导入。所以 `a/internal/b` 只有 `a/...` 能 import。这条规则由 `go` 命令的包加载器在 `cmd/go/internal/load` 里检查，**不是**编译器后端做的。

关于 `pkg/`：Java 老手最容易滥用它。**Go 官方仓库从来不用 `pkg/`**，社区共识是「没有外部用户就不要建 `pkg/`」——放 `internal/` 里，等真的有人要 import 了再挪出来。

`internal/` 与 `cmd/` 的配合长这样（`cmd/agentd/main.go` 里只做装配，逻辑都在 `internal/` 下）：

```go
// internal/tool/registry.go —— 包名是 tool，import 路径是 github.com/you/agentd/internal/tool
package tool

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"sync"
)

var ErrNotFound = errors.New("tool not found")

type Tool interface {
	Name() string
	Call(ctx context.Context, args map[string]any) (string, error)
}

type Registry struct {
	mu    sync.RWMutex
	tools map[string]Tool
}

func NewRegistry() *Registry { return &Registry{tools: map[string]Tool{}} }

func (r *Registry) Register(t Tool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.tools[t.Name()] = t // 注册表模式详见第 10 章
}

func (r *Registry) Names() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	names := make([]string, 0, len(r.tools))
	for n := range r.tools {
		names = append(names, n)
	}
	sort.Strings(names) // map 遍历顺序随机（第 12 章），要稳定输出必须排
	return names
}

func (r *Registry) Call(ctx context.Context, name string, args map[string]any) (string, error) {
	r.mu.RLock()
	t, ok := r.tools[name]
	r.mu.RUnlock()
	if !ok {
		return "", fmt.Errorf("tool %q: %w", name, ErrNotFound)
	}
	return t.Call(ctx, args)
}
```

`internal/` 之外的人**即使知道这个 import 路径也编译不过**——这就是 Go 版的「模块可见性」，比 Java 的 `package-private` 更硬，比 `module-info.java` 更轻。

---

## 2.3 底层实现：一个 `go.mod` 到底锁住了什么

`go.mod` 的语法少到一小时能背完（版本语义和 MVS 的算法详见第 33 章，这里只说「工程骨架」相关的部分）：

```go
// go.mod
module github.com/you/agentd        // 模块路径 = import 路径前缀，也是别人引用的坐标

go 1.25                            // 语言版本（决定语法与标准库行为），不是「要求装 1.25」
toolchain go1.25.3                 // 1.21+：没装这个版本时，go 命令自动去下载（GOTOOLCHAIN=auto）

require (
	github.com/google/uuid v1.6.0
	golang.org/x/sync v0.10.0
)

require github.com/some/indirect v0.3.1 // indirect  // 仅被依赖的依赖用到

// 1.24+：把「开发期工具」也纳入模块管理，替代 go install xxx@latest 的版本漂移
tool golang.org/x/tools/cmd/stringer

replace github.com/you/agentd/internal/fork => ../fork  // 本地调试用，别提交
```

`go.sum` 是另一回事，它**不是**「版本锁」，而是**内容校验和**，每个模块两行：

```
github.com/google/uuid v1.6.0 h1:NIvaJDMOsjHA8n1jAhLSgzrAzy1Hgr+hNrb57e+94F0=
github.com/google/uuid v1.6.0/go.mod h1:TIyPZe4MgqvfeYDBFedMoGGpEw/LqOeaOT+nhxU+yHo=
```

第一行校验模块 zip 内容，第二行校验 `go.mod` 文件本身（用来在**不下载源码**的情况下算出整个依赖图）。校验和来自 `GOSUMDB`（默认 `sum.golang.org`）这个透明日志，所以「某人在你喝咖啡时偷偷重发了一个同版本号的包」在 Go 里会直接构建失败——这是 Go 比 Maven 强的地方，也是私服场景要配 `GOPRIVATE`/`GONOSUMDB` 的原因。

几个必须知道的机制：

- **构建缓存是内容寻址的**（`GOCACHE`，`go env GOCACHE` 可看）。同样的源码 + 同样的编译参数 → 直接命中缓存，不做任何编译。所以 `go build ./...` 在 CI 里第二遍几乎免费，但**必须缓存 `GOCACHE` 目录**（Java 里对应缓存 `~/.m2` + `target/`）。
- **模块缓存只读**（`$GOMODCACHE`）。`go mod download` 下来的源码是只读的，`-mod=readonly` 是 Go 1.16 起的默认行为：**改了 import 但没更新 `go.mod` 就会直接报错**，而不是偷偷帮你改。这是 Java 程序员最不习惯的一点：`go.mod` 永远是被 `go mod tidy`「算」出来的，不是手写的。
- **`-X` 是怎么生效的**：`-ldflags "-X importpath.Var=value"` 并不是运行时反射，而是**链接器直接改写符号数据**——`cmd/link/internal/ld/data.go` 里的 `addstrdata()` 会找到那个包级 `string` 变量对应的符号，把它的内容替换成新字符串（所以目标只能是「包级 `string` 变量」，`const`、结构体字段、非 string 一律不接受）。
- **构建信息是内建的**：Go 1.18 起 `go build` 默认把 VCS 信息（commit、时间、是否 dirty）写进二进制，`go version -m ./agentd` 就能读出来，代码里也可以用 `runtime/debug.ReadBuildInfo()` 拿。

```go
// cmd/agentd/main.go —— 版本注入 + 优雅启动的最小骨架
package main

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"runtime/debug"
	"syscall"
	"time"
)

// 关键：包级 string 变量、给个默认值。链接期用 -X main.version=... 覆盖
var version = "dev"

func buildVersion() string {
	// 从构建信息里拿 VCS 信息，比自己拼更可靠
	if bi, ok := debug.ReadBuildInfo(); ok {
		for _, s := range bi.Settings {
			if s.Key == "vcs.revision" {
				return version + " " + s.Value[:min(8, len(s.Value))]
			}
		}
	}
	return version
}

func main() {
	// internal/httpapi.NewServer 之类的装配都在这里手写——Go 没有容器，DI 详见第 36 章
	srv := &http.Server{Addr: ":8080", Handler: http.NewServeMux()}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		slog.Info("starting", "version", buildVersion(), "addr", srv.Addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			slog.Error("listen", "err", err)
			stop()
		}
	}()

	<-ctx.Done() // 等 SIGTERM：k8s 滚动发布时就是这条路
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		fmt.Fprintln(os.Stderr, "shutdown:", err)
	}
}
```

---

## 2.4 Java 对照：同构点、差异点，以及 Go 为什么这么选

**同构点**（可以放心套用直觉）：依赖按版本不可变地缓存；构建产物与源码分离；CI 里 lint/测试/构建三个阶段；镜像尽量剥离运行时。

**差异点只有四个，但每个都影响架构决策**：

| 维度 | Maven | Go | Go 为什么这样选 |
| --- | --- | --- | --- |
| 依赖声明 | 手写/IDE 写，可以写范围 `[1.0,2.0)` | `go mod tidy` **算**出来，只允许精确版本 | 可重现构建是硬要求；范围版本 = 不可重现 |
| 冲突解决 | 最近者胜（nearest wins），可 `<exclusions>` | MVS：取所有约束里**最高**的最小版本 | 结果不依赖「谁先被声明」，避免依赖顺序影响行为（第 33 章） |
| 版本只读 | 私服允许重发同版本 | 校验和透明日志，重发即失败 | 供应链安全；也逼你把 `SNAPSHOT` 换成本地 `replace` |
| 构建脚本 | XML/Groovy 图灵完备 | 无脚本，`go build` 参数即全部 | 不走 XML 编程那条路：构建逻辑写进 Makefile/CI |

再说一个容易忽略的同构点：**Go 也没有「包管理器生命周期脚本」**。`npm install` 会跑 `postinstall`，Maven 会跑插件 goal，`go mod download` 只下载、只校验、不执行任何东西——这也是为什么 `go build` 在受控环境里更容易审计。

---

## 2.5 框架与工程落地：从目录到镜像

### 2.5.1 主流仓库怎么摆（去读它们的目录，比读任何规范都有用）

| 仓库 | 布局特征 | 你能抄什么 |
| --- | --- | --- |
| `gin-gonic/gin` | 根包就是 `gin`，配 `binding/`、`render/`、`internal/bytesconv/`、`internal/json/` | 「核心类型放根包，能力拆子包」；`internal/` 只放不想暴露的工具 |
| `tmc/langchaingo` | `llms/openai`、`llms/ollama`、`chains/`、`agents/`、`schema/`、`memory/`、`vectorstores/pgvector` | provider 适配器**一个供应商一个子包**，上层只依赖 `schema` |
| `cloudwego/eino` | `compose/`（编排图）、`components/`（ChatModel/Tool/Prompt）、`schema/`（消息与流）、`callbacks/`、`internal/` | 「组件 + 图编排」分层，`internal/` 装内核 |
| `modelcontextprotocol/go-sdk` | `mcp/`、`jsonrpc/`、`auth/`、`examples/`、`internal/` | 协议层（`jsonrpc`）与能力层（`mcp`）拆开 |

共同点：**没有 `controller/service/repository` 三件套**。Go 的包按「领域/能力」切，不按「技术分层」切——一个 `internal/agent` 包里可能同时有 HTTP handler、领域逻辑和存储访问。这个反直觉的点在第 54 章会被反复强调。

### 2.5.2 工具链分工：三件套 + 一个聚合器

```bash
# 1. 格式化：唯一标准，CI 里只做「有没有人没跑」的检查
gofmt -l .            # 列出不合规文件；非空即失败
gofmt -w .            # 就地修
goimports -l -local github.com/you .   # gofmt + import 分组整理（golang.org/x/tools）

# 2. go vet：官方内置的「可疑代码」检查器，零配置，默认可信
go vet ./...
#   printf          格式化串与参数不匹配
#   copylocks       拷贝了 sync.Mutex / sync.Map（第 4 章「值语义」的直接后果）
#   lostcancel      context.WithCancel 的 cancel 没被调用（第 24 章）
#   loopclosure     goroutine 里捕获循环变量（1.22 后基本绝迹）
#   structtag       struct tag 写错（json:"name, omitempty" 中间那个空格！）
#   httpresponse    resp.Body.Close 前没读完 body
#   slog / errorsas / unusedresult ...

# 3. staticcheck：vet 的超集，事实标准（honnef.co/go/tools）
go install honnef.co/go/tools/cmd/staticcheck@latest
staticcheck ./...      # SA 类是真 bug，S 类是简化建议，ST 类是风格，QF 类是快速修复

# 4. golangci-lint：把上面这些和 100+ linter 聚合、并行、缓存
golangci-lint run --timeout=5m
```

分工原则一句话：**`gofmt` 管格式（无争议）、`go vet` 管「几乎肯定是 bug」、`staticcheck` 管「更细的语义问题」、`golangci-lint` 只负责把前三者和团队偏好聚合起来**。注意别把 `golangci-lint` 配成 500 条规则——Go 社区对 lint 噪声的容忍度极低，默认集 + `staticcheck` 就够。

### 2.5.3 交叉编译与版本注入

```bash
# 目标平台列表（Go 一行命令能列出全部组合）
go tool dist list | grep linux

# 静态交叉编译：CGO_ENABLED=0 是关键，它决定产物能不能扔进 scratch/distroless
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath \
  -ldflags "-s -w -X main.version=$(git describe --tags --always)" \
  -o dist/agentd-linux-amd64 ./cmd/agentd

CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath \
  -ldflags "-s -w -X main.version=$(git describe --tags --always)" \
  -o dist/agentd-linux-arm64 ./cmd/agentd
```

三个参数解释清楚：

- `CGO_ENABLED=0`：不链接 libc，产物是**真正静态**的 ELF（`ldd` 会告诉你 "not a dynamic executable"）。附带的坑：DNS 走纯 Go 解析器（行为差异极小但存在），`os/user` 拿不到 NSS，需要时加 `-tags netgo,osusergo`。
- `-trimpath`：把源码绝对路径从二进制里抹掉，构建可重现（`/Users/you/...` 不进产物）。
- `-ldflags "-s -w"`：`-s` 去符号表，`-w` 去 DWARF，二进制小 20~30%。**代价**：pprof 里函数名还在，但 delve 调试和部分崩溃栈的精度会下降。生产镜像这么干，本地调试别干。

### 2.5.4 多阶段构建 + distroless：镜像从 100MB 到 10MB

```dockerfile
# deploy/Dockerfile
# ---- builder ----
FROM golang:1.25 AS build
WORKDIR /src

# 先只拷依赖清单：只要 go.mod/go.sum 没变，这一层就能命中缓存
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download

COPY . .
ARG VERSION=dev
# static + trimpath：产物无动态依赖，可以扔进 distroless/static
RUN --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=linux go build -trimpath \
      -ldflags "-s -w -X main.version=${VERSION}" \
      -o /out/agentd ./cmd/agentd

# ---- runtime ----
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/agentd /agentd
# distroless 自带 ca-certificates 与 /etc/passwd 里的 nonroot 用户；没有 shell、没有包管理器
EXPOSE 8080
# 必须用 exec 形式：shell 形式会多一层 sh，SIGTERM 收不到，优雅关停失效
ENTRYPOINT ["/agentd"]
```

体积账（amd64、`-s -w`、静态链接，量级参考）：

| 方案 | 大小 | 说明 |
| --- | --- | --- |
| `FROM golang:1.25`（直接跑） | ~800MB+ | 编译器和工具链全在，只适合本地 |
| `FROM eclipse-temurin:21-jre`（Java 对照） | ~250MB | JVM 本身就重，jar 另算 |
| `FROM debian:12-slim` | ~75MB + 二进制 | 有包管理器 = 有 CVE 面 |
| `FROM alpine:3.20` | ~7MB + 二进制 | 需要 `CGO_ENABLED=0`；musl 差异要注意 |
| `FROM gcr.io/distroless/static-debian12` | ~2MB + 二进制 | 无 shell，攻击面最小，**推荐** |
| `FROM scratch` | 0 + 二进制 | 时区/证书要自己拷，除非你确定不需要 |

时区坑：镜像里没有 `/usr/share/zoneinfo` 时，`time.LoadLocation("Asia/Shanghai")` 会失败。两个解法：拷时区文件进镜像，或者 `import _ "time/tzdata"`（Go 1.15+，把约 450KB 的 tzdata 嵌进二进制）。

### 2.5.5 CI 里的最小流水线

```yaml
# .github/workflows/ci.yml（节选，GitLab CI 同理）
env: { GOFLAGS: -mod=readonly, CGO_ENABLED: "0" }
steps:
  - run: go mod download
  - run: go build ./...
  - run: test -z "$(gofmt -l .)"        # 格式门禁
  - run: go vet ./...
  - run: go test -race -shuffle=on -coverprofile=cover.out ./...
  - run: go mod tidy -diff              # Go 1.23+：tidy 后是否有 diff（等价于「有没有人忘了 tidy」）
  - run: govulncheck ./...              # 依赖 + 调用链级别的漏洞扫描（第 33 章）
```

---

## 2.6 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| 改了 import 后 `go build` 报 `missing go.sum entry` | 只读了 `go.mod` 没更新校验和，默认 `-mod=readonly` | 跑 `go mod tidy`，**不要手写 `go.sum`** | 第 33 章 |
| CI 里 `gofmt -l .` 一屏文件 | 本地没装 pre-commit hook | `gofmt -w .` + pre-commit 钩子；IDE 开 format-on-save | — |
| 镜像里 `SIGTERM` 不生效，k8s 只能等 `SIGKILL` | 用了 `ENTRYPOINT /app`（shell 形式）或 `sh -c` 包一层 | `ENTRYPOINT ["/app"]`；进程自己是 PID 1 且实现 `Shutdown` | 第 52 章 |
| `-X main.version` 不生效，永远是 `dev` | 变量是 `const`、或名字写错成包别名 | 包级 `var version = "dev"`，符号名用完整 import path | — |
| 交叉编译出来跑不了：`no such file or directory` | `CGO_ENABLED=1` 编出动态链接产物，塞进 distroless 缺 libc | `CGO_ENABLED=0`，用 `ldd` 验证 "not a dynamic executable" | — |
| 二进制莫名 40MB+ | 忘了 `-s -w`，或 fmt 的格式化表、时区库被完整链接 | `-ldflags="-s -w"`；必要时 `-tags netgo,osusergo` | — |
| `internal/xxx` 别人 import 不了还以为是权限问题 | `internal` 是**编译期强制**的可见性边界 | 真要让外部用就挪到 `pkg/` 或独立模块 | — |
| 每个人都往 `pkg/` 塞东西 | 把 Java 的「公共模块」直觉带过来了 | 默认 `internal/`；`pkg/` 只在确有外部消费者时创建 | 第 54 章 |
| CI 每次都全量编译，慢 | 没缓存 `GOCACHE` 和 `$GOMODCACHE` | 缓存 `~/go/pkg/mod`、`~/.cache/go-build` | — |
| `go test` 在容器里随机超时 | 容器 CPU limit 下默认 `GOMAXPROCS` 与 `-race` 的内存放大 | 1.25 起 cgroup 感知 `GOMAXPROCS`；`-race` 内存约 5~10 倍 | 第 28、52 章 |

---

## 2.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| `go.mod` | 模块路径 + 语言版本 + `require`；由 `go mod tidy` 算出来，不手写 |
| `go.sum` | 内容校验和（不是版本锁），手改必炸；私服场景配 `GOPRIVATE`/`GONOSUMDB` |
| 目录布局 | `cmd/`（main）+ `internal/`（强制私有）+ `pkg/`（确有外部用户才建） |
| `internal` | 唯一的强制布局规则：只有父目录前缀的包能 import |
| 格式化 | `gofmt` 是唯一标准；`goimports` 顺手管 import 分组 |
| 静态检查 | `go vet`（官方、必跑）+ `staticcheck`（事实标准）+ `golangci-lint`（聚合） |
| 交叉编译 | `CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build`；`go tool dist list` 看全组合 |
| 版本注入 | `-ldflags "-X main.version=..."`，目标是包级 `string` 变量（链接期改写符号） |
| 构建信息 | `debug.ReadBuildInfo()` / `go version -m` 读 VCS 信息，1.18 起默认内建 |
| 镜像 | 多阶段 + `distroless/static` + exec 形式 `ENTRYPOINT`，100MB+ → 10MB 量级 |

## 2.8 自检题

1. **同事说「我把 `go.sum` 里对这个包的校验删了，构建就过了」——为什么这是事故级操作？**
   > 答案要点：`go.sum` 是内容校验和，删掉等于关闭供应链校验；`GOSUMDB` 透明日志是为了防「同版本号重发」；正确做法是 `go mod tidy` 让工具重算，或用 `replace`/`exclude` 显式表达意图。

2. **你的镜像有 800MB，同事说「换个 slim 基础镜像就好」，为什么这不是主要矛盾？**
   > 答案要点：Go 产物是静态二进制，真正的解法是**多阶段构建**——builder 阶段用 `golang` 镜像编译，runtime 阶段只 `COPY` 二进制进 `distroless/static`（~2MB）；再加 `-s -w`、`-trimpath`、`CGO_ENABLED=0`。

3. **`go vet` 默认会报「copylocks」，这跟第 4 章讲的值语义有什么关系？**
   > 答案要点：Go 里赋值即拷贝，`sync.Mutex` 被拷贝后是两份独立的锁，等于没锁；vet 的 `copylocks` 分析器专门抓「按值传递/嵌入含锁结构体」；这也是「锁字段所在结构体一律用指针」的原因。

## 2.9 延伸阅读

- `go help mod`、`go help build`、`go help environment`（`go env` 的每一项都值得看一遍）
- 官方文档 *Managing dependencies*、*Module version numbering*：`go.dev/doc/modules`
- `cmd/go/internal/load/pkg.go`——`internal` 规则的实际执行点
- `cmd/link/internal/ld/data.go`——`addstrdata()`：`-X` 的实现
- `runtime/debug.ReadBuildInfo`（`$GOROOT/src/runtime/debug/mod.go`）
- distroless 仓库 `GoogleContainerTools/distroless`：`static-debian12` 里到底有什么

---

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 1 章 从 Java 到 Go：心智模型重置](1-mindset-shift.md)
- 下一章：[第 3 章 语法极速通道 + 高频陷阱](3-syntax-crash.md)
- 相关：模块语义与 MVS 见[第 33 章](33-modules-mvs.md)；构建链路与编译参数见[第 27 章](27-compile-pipeline.md)；镜像与部署见[第 52 章](52-deployment-sre.md)
