# 37. 代码生成与反射：schema 自动化

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 工程化与服务端 ｜ **编号**：37 / 54
> **本章定位**：Java 的「注解 + 反射 + APT + Lombok」在 Go 里被劈成了两半：**能编译期做完的，用代码生成；必须运行期知道的，用 `reflect`**。这一章教你把「给 LLM 工具写 JSON Schema」这种重复劳动彻底自动化。
> **读法**：37.2 的图给出整体分工，37.5 有三份能直接跑的生成器/Schema 代码，37.6 是取舍清单。

---

## 37.1 引子：第 27 个工具的参数 schema

你给 Agent 写了 27 个工具。每个工具都要手写一段 JSON Schema 塞给 LLM：

```go
{
  "type": "object",
  "properties": {
    "query":       {"type": "string", "description": "搜索关键词"},
    "top_k":       {"type": "integer", "minimum": 1, "maximum": 50},
    "filters":     {"type": "object", "properties": {...}},  // 越写越深
  },
  "required": ["query"]
}
```

三周后你发现：**`top_k` 的 schema 写的是 `integer`，但 Go struct 里是 `string`**——LLM 老老实实传了数字，你的 `json.Unmarshal` 直接报 `cannot unmarshal number into Go struct field ... of type string`。更气人的是，改 struct 字段名之后你忘了改 schema，LLM 一直在传一个已经不存在的参数，然后**默默忽略**（`encoding/json` 默认不报未知字段），工具行为诡异了半个月。

Java 那边这个问题早就被 Spring AI / LangChain4j 的注解解决了：`@Tool(description = "...")` + 一个 `@P` 参数描述，框架用反射生成 schema。Go 里——**你得自己选一条路**：反射，还是生成。

---

## 37.2 直觉模型：编译期把「已知」变成「代码」，运行期把「未知」当「数据」

`reflect` 和 codegen 不是「两个工具」，而是**同一条流水线的两端**：

```
      ┌────────────────────── 编译期 ──────────────────────┐
      │                                                    │
 xxx.go（struct / interface / proto / 常量）
      │
      ▼   go generate ./...
 外部进程（就是普通可执行文件，读文件 → 写文件）
      ├── stringer        → status_string.go        （枚举 String()）
      ├── mockgen/mockery → mock_client.go          （接口 mock）
      ├── wire            → wire_gen.go             （装配代码）
      ├── protoc-gen-go   → agent.pb.go             （protobuf 类型）
      ├── sqlc            → query.sql.go            （类型安全 SQL）
      └── 你的生成器       → toolschema_gen.go       （JSON Schema）
      │
      ▼
 生成物（.go 源码，是普通代码，参与编译、参与 gofmt、参与 review）
      │
      ▼
   go build → 单一二进制（无反射、无启动开销）
      └────────────────────────────────────────────────────┘

      ┌────────────────────── 运行期 ──────────────────────┐
      │ reflect：把已经编译进二进制的类型元数据「当数据读」 │
      │   慢，但能处理「运行时才知道的类型」（插件、通用库）│
      └────────────────────────────────────────────────────┘
```

判断标准只有一句：**如果类型在编译期可知，就不要在运行期用反射**。你的工具参数、你的 API 请求体、你的枚举、你的接口——全都编译期可知。真正需要反射的只有「给别人用的通用库」：`encoding/json`、ORM、校验器、`jsonschema` 生成器本身。

| 维度 | 反射（reflect） | 代码生成（go:generate） |
| --- | --- | --- |
| 何时知道类型 | 运行期 | 编译期 |
| 性能 | 慢（装箱、`Value.Call`、无法内联） | 与手写代码一致 |
| 二进制体积 | 无额外（类型元数据本来就在） | 变大（生成物进二进制） |
| 启动开销 | 读 tag、建 map、缓存 | 0 |
| 错误暴露时机 | 运行期 panic / 返回 error | 编译期 |
| 灵活性 | 任意类型都能处理 | 只能处理生成时看得见的类型 |
| 维护成本 | 零（不用管生成物） | 生成物要提交、要 CI 校验漂移 |
| 典型代表 | `encoding/json`、`jsonschema` 库、`sqlx` | `stringer`、`mockgen`、`wire`、`protoc-gen-go`、`sqlc` |

---

## 37.3 底层实现：`go:generate` 与 `reflect` 的真实代价

### 37.3.1 `go:generate` 是「注释里的 shell 命令」

`cmd/go/internal/generate` 做的事极其简单：

1. 扫包内（不含依赖包）所有 `.go` 文件的注释，匹配 `^//go:generate\s+(.*)$`，**必须是行首、`//` 后无空格**（`// go:generate` **不会**被执行——这是最常见的哑火原因）。
2. 对每条指令，用 `go generate` 的环境变量做占位替换，然后 `exec.Command` 跑它。
3. 可用的环境变量（真实）：`$GOFILE`（当前文件名）、`$GOLINE`（行号）、`$GOPACKAGE`（包名）、`$GOARCH`、`$GOOS`、`$DOLLAR`（一个 `$` 字面量）。
4. 有 `-command` 前缀时，整个命令作为**一个**可执行文件名解析（不经过 shell）——所以管道、重定向在 Windows 上会有差异。
5. `-n` 只打印不执行，`-x` 打印并执行，调生成器时最爱用这两个。

**它不会做**：不管依赖顺序、不并发（同一包内顺序执行）、不检查产物是否最新。所以「生成物必须提交」这条纪律是必需的——否则 CI 就成了一台「每次都在造新文件」的机器。

### 37.3.2 `reflect` 的代价在哪

Go 的类型元数据（`internal/abi/type.go` 的 `Type`、`StructType`、`StructField`，Go 1.21 起从 `runtime/type.go` 迁到 `internal/abi`）本来就编译进二进制，`reflect` 只是它的薄封装。所以 `reflect.TypeOf(x)` 本身很便宜（拿个指针）。**贵的是下面三件事：**

1. **装箱**：`reflect.ValueOf(x)` 要把 `x` 装进 `interface{}`，值类型会逃逸到堆。传结构体进去，先拷贝一份。
2. **`reflect.Value.Call`**：走 `runtime.reflectcall` 汇编 -> 参数按 ABI 打包 -> 结果再拆回来。**比直接调用慢一到两个数量级，而且彻底阻断内联和逃逸分析**。
3. **反射 API 的每一次访问都带边界检查与类型断言**：`v.Field(i).String()` 里每一步都是 `switch v.Kind()`。

反过来，**有些反射是极便宜的**：`reflect.Type.NumField()` / `Field(i).Tag.Get("json")` 一趟下来只是读内存——这就是为什么 `encoding/json` 能给字段元数据建缓存后跑得很快（详见 [第 31 章](31-json-codec.md)）。**结论：反射慢在「调用」和「装箱」，不慢在「读类型信息」。** 所以「反射一次建表 + 之后走表」是标准解法。

`StructTag` 的解析（`reflect/type.go` 的 `StructTag.Lookup`）也值得知道：它按 `key:"value"` 逐个扫描、跳过空格、用 `strconv.Unquote` 反转义，**没有 map 缓存，每次调用都重新解析**。所以热路径上不要反复 `Tag.Get`，读一次存起来。

---

## 37.4 Java 对照：Lombok 的活，Go 让生成器干

| Java | Go | 差异与原因 |
| --- | --- | --- |
| Lombok（编译期改 AST） | 代码生成器写新文件 | Lombok 是「改别人的树」，脆且依赖 IDE 插件；Go 的生成器只写新文件，产物可读可 review |
| APT / `@AutoValue` / MapStruct | `go:generate` + 生成器 | 思路完全一致，Go 只是没有注解这个载体，改用注释指令 |
| 注解 + 反射（Jackson/Hibernate） | struct tag + `encoding/json` 等 | tag 是**字符串**，写错不报错；这是 Go 的代价 |
| 编译期注解处理器 `-processor` | `go generate` 手动/CI 触发 | Go 的生成**不在 `go build` 流程里**，得自己接进 CI |
| 动态代理 + 反射注入 | 生成物 or `reflect` | 见第 36 章 |
| `javax.validation` + `@Valid` | 生成 `Validate()` 或 `go-playground/validator` | Go 更倾向显式 `if` 或用 tag 驱动的校验库 |

> ⚠️ **Java 人最容易忽略的一条**：Go 的 struct tag 是**纯字符串**，编译器不校验。`json:"nmae"` 拼错、`jsonschema:"required"` 写成 `required:true`，都不会有任何提示。所有「tag 驱动的行为」都必须靠测试兜住——这是 34 章那套 golden/回放测试的真实用途之一。

---

## 37.5 工程落地：从 tag 到 LLM 工具 schema

### 37.5.1 `go:generate` 的规范写法

```go
package tool

// 把所有生成指令集中在一处，PR 时一眼能看出加了什么生成物
//go:generate stringer -type=Risk -linecomment -output=risk_string.go
//go:generate mockgen -source=registry.go -destination=mock_registry_test.go -package=tool -typed
//go:generate wire
//go:generate protoc --go_out=. --go_opt=paths=source_relative --go-grpc_out=. agent.proto

// Risk 工具风险等级；-linecomment 让 String() 输出行注释文本
type Risk int

const (
	RiskReadOnly Risk = iota // read_only
	RiskMutating             // mutating
	RiskDangerous            // dangerous
)
```

工具版本必须锁定，否则「同事本地生成的和 CI 生成的不一样」。两种主流做法：

```go
// tools.go —— 老牌做法：用一个 build tag 把工具钉进 go.mod
//go:build tools

package tools

import (
	_ "github.com/google/wire/cmd/wire"
	_ "go.uber.org/mock/mockgen"
	_ "golang.org/x/tools/cmd/stringer"
)
```

```bash
# Go 1.24+ 的新做法：go.mod 的 tool 指令（`go get -tool` 添加，`go tool <名>` 运行）
go get -tool github.com/google/wire/cmd/wire
go tool wire ./...
# 具体行为以本地 `go help tool` 为准
```

### 37.5.2 主力方案：用 `reflect` 生成 JSON Schema（一次反射，处处用）

生产环境最省事的路线是 `github.com/invopop/jsonschema`：它读 `json` / `jsonschema` / `description` tag 和 Go doc 注释，直接产出 Draft 2020-12 的 Schema。

```go
package tool

import (
	"encoding/json"
	"fmt"

	"github.com/invopop/jsonschema"
)

// SearchArgs 是给 LLM 看的契约：字段名 = 参数名，doc 注释 = description
type SearchArgs struct {
	// 搜索关键词，支持 AND 语义
	Query string `json:"query" jsonschema:"required,minLength=1,maxLength=200"`
	// 返回条数
	TopK int `json:"top_k,omitempty" jsonschema:"minimum=1,maximum=50,default=5"`
	// 只在这些来源里检索
	Sources []string `json:"sources,omitempty" jsonschema:"enum=docs,enum=code,enum=wiki"`
	// 时间窗（RFC3339），留空表示不限
	Since string `json:"since,omitempty" jsonschema:"format=date-time"`
}

// ToolSchema 生成 OpenAI function-calling 的 parameters 部分
func ToolSchema(name, desc string, in any) (map[string]any, error) {
	r := &jsonschema.Reflector{
		DoNotReference:            true, // 关键：把所有 $ref 内联，否则 LLM 常看不懂 $defs
		RequiredFromJSONSchemaTags: true, // required 来自 jsonschema tag，而不是"所有非指针字段"
		ExpandedStruct:            true, // 根节点直接展开为 object，不加 $ref
	}
	s := r.Reflect(in)
	b, err := json.Marshal(s)
	if err != nil {
		return nil, err
	}
	var params map[string]any
	if err := json.Unmarshal(b, &params); err != nil {
		return nil, err
	}
	return map[string]any{
		"type": "function",
		"function": map[string]any{
			"name":        name,
			"description": desc,
			"parameters":  params,
		},
	}, nil
}

// 用法：注册工具时顺手生成，schema 与 struct 永远同步
func DefineSearch() Tool {
	schema, err := ToolSchema("search_docs", "在内部文档中检索", &SearchArgs{})
	if err != nil {
		panic(fmt.Sprintf("schema: %v", err)) // 启动期失败，比运行期静默好
	}
	return Tool{Name: "search_docs", Schema: schema, Handler: handleSearch}
}
```

配套的两条纪律：**参数结构体加 `DisallowUnknownFields` 解码**（防 LLM 幻觉参数），以及**用 schema 反向校验**（`santhosh-tekuri/jsonschema` 或 `xeipuuv/gojsonschema`）。`required` 的语义也要说清：默认情况下 `jsonschema` 库会把所有非 `omitempty`、非指针字段当 required，所以要么用 tag 显式声明，要么用 `RequiredFromJSONSchemaTags: true`——**这一条不搞清，你的工具会有 30% 的参数被 LLM 判定必填而它其实只想传一个**。

### 37.5.3 备选方案：自己写 AST 生成器（当反射不够时）

什么时候需要自研？两个真实场景：① 你想生成**静态**的 schema 常量（连一次反射都不想付）；② 你要从代码里提取比 `reflect` 能给更多的信息（比如跨包的结构体、注释里的自定义指令）。

`go/ast` 入门只需要三个 API：`parser.ParseFile`、`ast.Inspect`、`printer.Fprint`。

```go
// gen/main.go —— 扫描 struct，输出每个工具参数的 JSON Schema 常量
package main

import (
	"bytes"
	"fmt"
	"go/ast"
	"go/parser"
	"go/printer"
	"go/token"
	"os"
	"strings"
)

func exprString(fset *token.FileSet, e ast.Expr) string {
	var buf bytes.Buffer
	_ = printer.Fprint(&buf, fset, e)
	return buf.String()
}

// jsonType 把 Go 类型映射成 JSON Schema 类型（真实项目要处理指针/slice/map/泛型）
func jsonType(goType string) string {
	switch {
	case strings.HasPrefix(goType, "[]"):
		return "array"
	case goType == "string":
		return "string"
	case goType == "bool":
		return "boolean"
	case strings.ContainsAny(goType, "int"), strings.ContainsAny(goType, "float"):
		return "number"
	default:
		return "object"
	}
}

func main() {
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, os.Args[1], nil, parser.ParseComments)
	if err != nil {
		panic(err)
	}

	ast.Inspect(f, func(n ast.Node) bool {
		ts, ok := n.(*ast.TypeSpec)
		if !ok {
			return true
		}
		st, ok := ts.Type.(*ast.StructType)
		if !ok {
			return true
		}
		fmt.Printf("// struct %s\n", ts.Name.Name)
		fmt.Printf("var %sSchema = map[string]string{\n", ts.Name.Name)
		for _, field := range st.Fields.List {
			tag := ""
			if field.Tag != nil {
				tag = strings.Trim(field.Tag.Value, "`") // 去掉反引号
			}
			name := ""
			if i := strings.Index(tag, `json:"`); i >= 0 {
				name = tag[i+6:]
				if j := strings.Index(name, `"`); j >= 0 {
					name = name[:j]
				}
			}
			if name == "" || name == "-" {
				continue
			}
			for _, id := range field.Names {
				fmt.Printf("\t%q: %q, // Go 类型 %s\n", name, jsonType(exprString(fset, field.Type)), exprString(fset, field.Type))
				_ = id
			}
		}
		fmt.Println("}")
		return false
	})
}
```

```bash
go run ./gen ./internal/tool/args.go > internal/tool/schema_gen.go
# 生产做法：输出前过一遍 format.Source()，保证 gofmt 通过
```

这个骨架可以直接长成「生成器」：把 `fmt.Printf` 换成往 `bytes.Buffer` 里写 Go 代码、末尾 `format.Source(buf.Bytes())`、`os.WriteFile` 落盘，再在文件头加 `// Code generated by toolgen; DO NOT EDIT.`。

> 💡 什么时候用 `go/ast` 而不用 `reflect` 就够了？**当生成物本身决定了「代码结构」时**（生成方法、生成接口实现、生成常量表）。如果只是为了「读字段名和 tag」，`reflect` 写起来快 10 倍。

### 37.5.4 生成物的 CI 纪律

```yaml
- name: codegen drift
  run: |
    go generate ./...
    if ! git diff --quiet; then
      echo "::error::生成物与源码不同步，请本地跑 go generate 并提交"
      git diff --stat
      exit 1
    fi
- name: gofmt / vet
  run: |
    gofmt -l . | tee /tmp/fmt && test ! -s /tmp/fmt
    go vet ./...
```

---

## 37.6 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| `go generate` 什么都不做 | 写成了 `// go:generate`（`//` 后有空格） | 必须行首紧贴 `//go:generate` | 37.3.1 |
| 同事本地生成物和 CI 不一致 | 工具版本没锁 | `tools.go` 或 go.mod `tool` 指令固定版本 | 37.5.1 |
| 生成物没提交，别人 `go build` 失败 | 把生成物当「构建中间产物」 | 生成物一律提交（除了明确 gitignore 的 proto 产物） | 37.5.4 |
| LLM 传参数被 `encoding/json` 静默忽略 | 默认不报未知字段 | `dec.DisallowUnknownFields()` + schema 校验 | 37.5.2 |
| 所有字段都被判 required，LLM 疯狂报错 | `required` 由 tag 推导的默认规则 | 用 `jsonschema:"required"` 显式声明 + `RequiredFromJSONSchemaTags: true` | 37.5.2 |
| LLM 看不懂 schema 里的 `$ref` / `$defs` | 生成器默认用引用复用定义 | `DoNotReference: true` + `ExpandedStruct: true` 内联展开 | 37.5.2 |
| tag 拼错（`json:"nmae"`）无任何提示 | tag 是字符串，编译器不管 | golden 测试 / 启动时自检「字段数与 tag 数一致」 | 37.4 |
| 热路径用 `reflect`，P99 涨 | 每次调用都装箱 + `Value.Call` | 启动时反射一次建表，之后走表；或改代码生成 | 37.3.2 |
| 生成器没有 `// Code generated ... DO NOT EDIT.` | 少了标准头 | 文件首行必须写，linter 靠这个跳过生成物 | 37.5.3 |
| `wire_gen.go` 里手改了逻辑 | 改错地方 | 改 provider/injector，重新生成 | 37.5.4 |
| 接口改了，mock 没重新生成 → 编译奇怪错误 | 生成物与接口漂移 | CI 里 `go generate && git diff --exit-code` | 37.5.4 |
| 自研生成器读不到跨包类型 | 只用 `go/parser` 没做类型检查 | 换 `golang.org/x/tools/go/packages` + `go/types` | 37.5.3 |

---

## 37.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 分工 | 编译期可知的 → 生成；运行期才知道的 → 反射 |
| `go:generate` | 注释里的命令；`//` 后不能有空格；`$GOFILE/$GOLINE/$GOPACKAGE/$DOLLAR` |
| 不参与 build | `go generate` 不被 `go build` 触发，必须自己接进 CI |
| 工具版本 | `tools.go`（build tag tools）或 go.mod `tool` 指令 |
| reflect 成本 | 贵在装箱与 `Value.Call`（走 `reflectcall`），不贵在读类型信息 |
| tag 解析 | `StructTag.Lookup` 每次重解析、无缓存；热路径读一次存起来 |
| tag 风险 | 纯字符串，写错静默失败 → 必须靠测试兜 |
| LLM 工具 schema | `invopop/jsonschema` 反射生成；`DoNotReference` 内联；`required` 显式声明 |
| `go/ast` | `ParseFile` → `ast.Inspect` → `printer.Fprint`；要类型信息上 `x/tools/go/packages` |
| 生成物纪律 | 提交 + `// Code generated ... DO NOT EDIT.` + CI `git diff --exit-code` |
| 生成 vs 反射判据 | 生成物要决定「代码结构」→ AST；只要「读字段/tag」→ reflect |

## 37.8 自检题

1. **为什么 `encoding/json` 用反射却很快，而你手写的「用 `reflect` 遍历 struct 拼 SQL」的代码很慢？**
   > 答案要点：`encoding/json` 在第一次遇到某类型时用反射建一份「编解码计划」（字段偏移、tag、类型）并缓存，后续只按计划读写；而逐次 `Value.Field().Interface()` 的写法每次都装箱、做类型断言、无法内联。**反射的正确用法是「建表一次」，不是「每次都用」。**

2. **你要把 27 个工具的参数定义暴露给 LLM，选 `reflect` 生成还是写死 JSON 字符串？给出判断依据和两条必须加的防护。**
   > 答案要点：选反射生成（`invopop/jsonschema`）——schema 与 struct 同源，改字段自动同步，测试可覆盖；防护：① 解码侧 `DisallowUnknownFields` + schema 反向校验，杜绝幻觉参数；② `required` 显式声明 + `DoNotReference` 内联，避免 LLM 误解结构与必填性。

3. **`go generate` 为什么不被 `go build` 自动触发？这带来什么工程后果？**
   > 答案要点：生成器是外部可执行文件、可能很慢、可能有副作用，把网络/工具链依赖塞进 `go build` 会毁掉「任何机器上 `go build` 都能过」的承诺。后果是生成物必须提交（否则别人构建失败），且 CI 必须显式校验漂移（`go generate && git diff --exit-code`）。

## 37.9 延伸阅读

- `go help generate`：指令语法、工作目录、环境变量、`-command`/`-n`/`-x`
- `cmd/go/internal/generate/generate.go`：扫描与执行的全部实现（两百行）
- `go/ast` 包文档：`File`/`TypeSpec`/`StructType`/`Field`/`Tag`；`go/printer`、`go/format`、`go/token`
- `golang.org/x/tools/go/packages`：需要类型信息时的加载器（`packages.Load` + `types.Info`）
- `github.com/invopop/jsonschema`：`Reflector` 选项、`jsonschema` tag 语义、`$defs` 与内联
- `golang.org/x/tools/cmd/stringer`、`go.uber.org/mock/mockgen`、`github.com/vektra/mockery`、`github.com/google/wire`、`protoc-gen-go`
- 交叉引用：[第 31 章](31-json-codec.md) 的字段缓存与严格解码；[第 41 章](41-tool-system-function-calling.md) 的 schema 校验全链路

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 36 章](36-dependency-injection.md) 依赖注入与装配：fx / wire / 手工
- 下一章：[第 38 章](38-net-http.md) net/http 深潜：Server、中间件与超时
