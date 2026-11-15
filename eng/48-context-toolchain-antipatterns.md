# 48. 工具链与反模式

> **状态**：`已完成（初稿）` ｜ **所属**：第八部分 · 工程效能 ｜ **编号**：48 / 52
> **本章定位**：第 46 章讲「为什么」，第 47 章讲「怎么做」，本章讲「用什么工具把这两件事固化下来」，以及**七个会让你前功尽弃的反模式**。
> **读法**：§48.2 的六类工具按需取用；§48.4 的七个反模式建议做成团队 checklist。

---

## 48.1 引子：同一个团队，两套结果

两个小组用同样的模型、同样的项目，三个月后：

| 指标 | A 组 | B 组 |
| --- | --- | --- |
| 模型生成代码的合并率 | **78%** | 31% |
| 平均返工次数 | 1.2 次 | 4.7 次 |
| 「模型又造了个重复类」事故 | 0 | 6 |
| 新人上手时间 | 3 天 | 3 周 |

差别不在模型，在**工具链**：

- A 组有 `AGENTS.md`（技术栈、目录约定、已有基础设施清单）、`make verify`、repo map、以及「先搜再写」的硬规矩。
- B 组只有一句「你是一个资深 Java 工程师」。

> 💡 **工具链的作用不是让模型更聪明，是让它少犯「本可以不犯」的错。**

---

## 48.2 六类工具：让模型「看懂仓库」

### ① 规则文件（最高性价比）

一份 `AGENTS.md` 放在仓库根目录，每次会话自动注入。**它替代的是「老员工口口相传」**。

```markdown
# AGENTS.md

## 技术栈
- Go 1.23 / Java 21 + Spring Boot 3.3 / MySQL 8.0 / Redis 7 / Kafka 3.7
- 禁止引入新的 ORM、新的 HTTP 框架、新的日志库（先问）

## 目录约定
- `internal/<domain>/`：领域逻辑，不依赖 `infra/`
- `infra/`：DB/缓存/MQ 适配器
- `api/`：HTTP handler，只做参数校验与转换
- 依赖方向：api → internal → infra（反向禁止）

## 已有基础设施（不要重复造）
- `internal/id`：`id.Next()` 雪花 ID；`id.Segment()` 号段
- `internal/result`：统一返回结构 `result.OK/Err`
- `internal/trace`：`trace.From(ctx)` 取 traceID
- `internal/lock`：Redis 分布式锁，`lock.Acquire(ctx, key, ttl)`

## 硬规矩
- 金额一律用「分」（int64），字段名带 `Cents`
- 所有 DB 查询必须带 `tenant_id`
- 所有写接口必须声明幂等键
- 错误必须用 `errors.Is` 可判定，不要 `fmt.Errorf("...%v")`
- 提交前必须 `make verify` 通过

## 常用命令
- `make verify`：编译 + vet + lint + 单测(-race)
- `make test-integration`：需要本地 docker compose up
- `make mock`：重新生成 mock
```

**写法要点**：

| 要点 | 说明 |
| --- | --- |
| 短 | ≤ 150 行；太长会被忽略（也可能挤占上下文） |
| 具体 | 「金额用分」比「注意精度」有用 100 倍 |
| 可执行 | 每条都能被 grep/CI 检查 |
| 有「不要」 | 明确列出禁用项，比只列推荐项有效 |
| 常更新 | 每次踩坑后补一条（把它当事故台账） |

### ② repo map（全局地图）

用 1~2k token 给模型一张「有什么、在哪」的地图。生成方式：

```bash
# Go：用 ctags 或 gopls 生成符号表
ctags -R --fields=+n --extras=+q -f - ./internal | head -200
# 或用 tree-sitter 自研：文件 → 类型/函数签名 + 一行注释
```

**地图只放签名，不放实现**——实现按需再读。这是「按需加载」的核心。

### ③ 代码搜索（先搜再写）

在规则文件里写死：

```markdown
## 改代码前必须做的三件事
1. `rg "type <你要新建的类型名>"` —— 确认没有重复
2. `rg "<关键方法名>" internal/` —— 找现有实现
3. `rg "TODO|FIXME" <目标目录>` —— 看有没有已知坑
```

**为什么有效**：模型最大的浪费是「重新发明一个已存在的东西」。一次 grep 只要 200ms，能省掉一次重复实现。

### ④ 依赖图（防止反向依赖）

```bash
# Go：检查是否有反向依赖（api 不应被 internal 引用）
go list -deps ./internal/... | grep '/api/' && echo "❌ 反向依赖" && exit 1
# Java：ArchUnit 测试
```

```java
@ArchTest
static final ArchRule layerRule = layeredArchitecture()
    .layer("Api").definedBy("..api..")
    .layer("Domain").definedBy("..internal..")
    .layer("Infra").definedBy("..infra..")
    .whereLayer("Api").mayNotBeAccessedByAnyLayer()
    .whereLayer("Domain").mayOnlyBeAccessedByLayers("Api");
```

### ⑤ 验证脚本（把「对不对」变成一条命令）

见第 47 章 §47.4。核心是**模型自己能跑**，不用等人。

### ⑥ 子任务并行（按模块隔离上下文）

```text
主会话：定契约、拆任务、review
  ├── 子会话 A：实现 refund 模块（只给 refund 相关文件）
  ├── 子会话 B：实现 coupon 模块（只给 coupon 相关文件）
  └── 子会话 C：写集成测试（只给契约 + 启动脚本）
```

**关键**：子会话之间**不共享上下文**，只共享契约。这样不会互相污染，也不会把上下文撑爆。合并时由主会话统一 review。

---

## 48.3 工具链全景图

```text
┌─────────────────────────────────────────────────────────────┐
│                        规则层                                │
│  AGENTS.md（约定/禁用项/已有基础设施/命令）                    │
├─────────────────────────────────────────────────────────────┤
│                        感知层                                │
│  repo map · 符号索引 · 代码搜索 · 依赖图                       │
├─────────────────────────────────────────────────────────────┤
│                        执行层                                │
│  编辑 · 生成 · 重构 · 子任务并行                               │
├─────────────────────────────────────────────────────────────┤
│                        验证层                                │
│  编译 · 类型 · lint · 单测(-race) · 集成 · 契约测试            │
├─────────────────────────────────────────────────────────────┤
│                        治理层                                │
│  CI 门禁 · review 清单 · 提交规范 · 回滚策略                   │
└─────────────────────────────────────────────────────────────┘
```

**五层缺一层，效果就会打折**：

| 缺哪层 | 后果 |
| --- | --- |
| 规则层 | 每次都从零解释，风格漂移，重复造轮子 |
| 感知层 | 模型靠猜，幻觉 API，忽略已有抽象 |
| 执行层 | （通常不缺） |
| 验证层 | 「看起来对」，上线才炸 |
| 治理层 | 错误代码被合并，事故反复 |

---

## 48.4 七个反模式（团队 checklist）

| # | 反模式 | 典型话术 | 后果 | 正解 |
| --- | --- | --- | --- | --- |
| 1 | **一次生成千行** | 「你直接把整个模块写了」 | 编译错误上百，越修越乱 | 拆到 200~600 行/次 |
| 2 | **无测试驱动** | 「先写出来，测试后面补」 | 「后面」永远不会来 | 人先写测试（红），模型实现 |
| 3 | **上下文漂移** | 聊了 20 轮还在改 | 命名/风格/方案全变，前后矛盾 | 每 5 轮摘要一次；新任务新会话 |
| 4 | **忽略已有抽象** | 「这个工具类我新写一个」 | 重复实现，行为不一致 | 规则文件列基础设施 + 先 grep |
| 5 | **幻觉 API** | 「用 `client.fetchAsync()` 就行」 | 编译不过，或运行时 NoSuchMethod | 给版本 + 让模型读源码；编译兜底 |
| 6 | **忘记清理** | 留下调试日志、注释代码、临时文件 | 技术债 + 泄露风险 | 提交前 checklist + lint |
| 7 | **无 review 合并** | 「能跑就行」 | 事务/并发/权限漏洞上线 | 四处必看（第 47 章） |

### 反模式自检（提交前 30 秒）

```text
□ git diff --stat：改动是否 ≤ 3 个文件、≤ 600 行？
□ 是否新增了「本该存在」的类/函数？（grep 过吗）
□ 是否新增了测试？测试是「先写」的还是「照着实现补」的？
□ 有没有空 catch、忽略的 err、共享 map、缺 tenant_id？
□ make verify 是否通过（含 -race）？
□ 有没有留下 TODO/调试日志/注释掉的代码？
```

---

## 48.5 双修对照：工具服务两种「读者」

| 工具 | 人用它做什么 | 模型用它做什么 | 相同 / 差异 |
| --- | --- | --- | --- |
| 规则文件 | 新人 onboarding | 每次会话的「记忆」 | **同一份文档，两种读者** |
| repo map | 快速了解项目结构 | 按需定位文件 | 相同 |
| 代码搜索 | 找实现、找引用 | 避免重复造轮子 | 相同 |
| 依赖图 | 架构守护 | 判断改动的爆炸半径 | 相同 |
| 验证脚本 | 本地自测 | 自我纠错的闭环 | 相同 |
| review 清单 | 人工把关 | （模型自查） | 模型可先自查一遍 |

> 💡 **本章与第 46 章呼应**：**为模型做的工程改造，全都是在还「代码库可维护性」的债。** 规则文件就是 onboarding 文档，repo map 就是架构图，验证脚本就是「怎么跑起来」——这些本来就是新人需要的。

---

## 48.6 会在哪炸

| # | 坑 | 现象 | 解 |
| --- | --- | --- | --- |
| 1 | 规则文件太长 | 模型忽略后半部分 | 压到 150 行内，细节放子文件按需引用 |
| 2 | 规则与代码不一致 | 模型按旧规则写，编译不过 | 规则文件进 CI 检查（如禁止的 import） |
| 3 | repo map 过期 | 模型找不存在的文件 | 每次提交自动重新生成 |
| 4 | 子任务冲突 | 两个子会话改了同一文件 | 按目录切分，合并前 diff |
| 5 | 验证脚本太慢 | 模型不跑，人也懒得跑 | 分层：快检查 < 30s，全量放 CI |
| 6 | 过度依赖模型自查 | 它说「已检查」但没检查 | 只信命令输出，不信自述 |

---

## 48.7 怎么验证工具链有效

| 指标 | 定义 | 目标 |
| --- | --- | --- |
| 生成代码合并率 | 合并的生成代码 / 总生成 | ≥ 60% |
| 平均返工次数 | 每个任务被退回次数 | ≤ 2 |
| 重复实现事故 | 新增了已有功能的次数 | 0 |
| 验证通过率 | 首次 `make verify` 通过比例 | ≥ 70% |
| 从任务到合并时长 | 端到端 | 持续下降 |

---

## 48.8 实现一：repo map 生成器（完整可跑）

工具链里最容易「说得漂亮但没人做」的就是 repo map。它其实只是一个 80 行的 Go 程序：用标准库 `go/parser` 遍历目录，抽「包 → 类型/函数签名 + 首行注释」，按目录树输出。

```go
// cmd/repomap/main.go —— 生成 repo map：只输出签名，不输出实现
package main

import (
	"bytes"
	"fmt"
	"go/ast"
	"go/parser"
	"go/printer"
	"go/token"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

type Sym struct {
	Kind string // "type" | "func" | "method" | "const" | "var"
	Name string
	Sig  string // 仅签名（类型名 + 参数 + 返回值）
	Doc  string // 首行注释，截断到 80 字
}

func main() {
	root := "."
	if len(os.Args) > 1 { root = os.Args[1] }
	skip := map[string]bool{".git": true, "vendor": true, "node_modules": true, "testdata": true}

	out := map[string][]Sym{}   // dir -> symbols
	fset := token.NewFileSet()

	err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil { return nil }
		if info.IsDir() {
			if skip[info.Name()] || strings.HasPrefix(info.Name(), ".") && info.Name() != "." {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") { return nil }

		file, perr := parser.ParseFile(fset, path, nil, parser.ParseComments)
		if perr != nil { return nil } // 解析失败的跳过，不阻塞

		dir := filepath.Dir(path)
		for _, d := range file.Decls {
			switch n := d.(type) {
			case *ast.FuncDecl:
				kind := "func"
				name := n.Name.Name
				if n.Recv != nil && len(n.Recv.List) > 0 {
					kind = "method"
					name = recvName(n.Recv.List[0].Type) + "." + name
				}
				sig := render(fset, n.Type)
				sig = strings.ReplaceAll(sig, "\n", " ")
				out[dir] = append(out[dir], Sym{kind, name, sig, doc(n.Doc)})
			case *ast.GenDecl:
				for _, spec := range n.Specs {
					switch s := spec.(type) {
					case *ast.TypeSpec:
						out[dir] = append(out[dir], Sym{"type", s.Name.Name, render(fset, s.Type), doc(n.Doc)})
					case *ast.ValueSpec:
						k := strings.ToLower(n.Tok.String())
						for _, id := range s.Names {
							out[dir] = append(out[dir], Sym{k, id.Name, "", doc(n.Doc)})
						}
					}
				}
			}
		}
		return nil
	})
	if err != nil { panic(err) }

	dirs := make([]string, 0, len(out))
	for d := range out { dirs = append(dirs, d) }
	sort.Strings(dirs)

	var buf bytes.Buffer
	buf.WriteString("# repo map (auto-generated, do not edit)\n")
	for _, d := range dirs {
		buf.WriteString("\n## " + d + "\n")
		sort.Slice(out[d], func(i, j int) bool { return out[d][i].Name < out[d][j].Name })
		for _, s := range out[d] {
			fmt.Fprintf(&buf, "- %s %s%s", s.Kind, s.Name, s.Sig)
			if s.Doc != "" { fmt.Fprintf(&buf, "  // %s", s.Doc) }
			buf.WriteByte('\n')
		}
	}
	os.Stdout.Write(buf.Bytes())
}

func doc(cg *ast.CommentGroup) string {
	if cg == nil { return "" }
	t := strings.TrimSpace(cg.Text())
	if i := strings.IndexByte(t, '\n'); i > 0 { t = t[:i] }
	if len([]rune(t)) > 80 { t = string([]rune(t)[:80]) + "…" }
	return t
}

func recvName(e ast.Expr) string {
	switch t := e.(type) {
	case *ast.StarExpr: return recvName(t.X)
	case *ast.Ident:   return t.Name
	}
	return "?"
}

func render(fset *token.FileSet, node any) string {
	if node == nil { return "" }
	var b bytes.Buffer
	_ = printer.Fprint(&b, fset, node)
	s := strings.TrimSpace(b.String())
	if i := strings.LastIndexByte(s, '}'); i >= 0 { s = s[:i] } // 类型体只留开头
	return s
}
```

**用法与产出**：

```bash
go run ./cmd/repomap . > .ai/repo-map.md
wc -c .ai/repo-map.md          # 目标：< 60KB（约 15k token）
```

```markdown
# repo map (auto-generated, do not edit)

## internal/id
- func Next  func() (int64, error)  // 雪花 ID，进程内单调
- type Segment struct { ... }  // 号段分配器

## internal/order
- func (s *Service) Create  func(ctx context.Context, req CreateReq) (*Order, error)  // 创建订单，幂等键 requestID
- func (s *Service) Get  func(ctx context.Context, id int64) (*Order, error)  // 不存在返回 ErrNotFound
```

**三条工程纪律**：

| 纪律 | 原因 |
| --- | --- |
| **不进版本库，构建时生成** | 手写的地图必然腐烂；生成物进 git 会天天冲突 |
| **只放签名 + 首行注释** | 实现细节会撑爆上下文，且会过期 |
| **限制体积（< 60KB）** | 超过就说明该按子目录拆成多份，按需加载 |

**什么时候该拆**：一个 20 万行的单体，repo map 仍有 300KB。此时不要硬塞，而是**先按领域拆成 5~8 份**（`order.map.md`、`payment.map.md`），主 map 只列「每个领域有哪些入口」，让模型按需读第二层。这与第 46 章的「分治」是同一个动作。

---

## 48.9 实现二：把规则变成 CI 门禁

写在 `AGENTS.md` 里的规则，如果没有机器检查，三个月后一定失效（新人不知道、模型不看）。**规则要能被 CI 判真假**。

### 三类可自动化的规则

| 规则类型 | 例子 | 检查手段 |
| --- | --- | --- |
| **依赖方向** | `api` 不许被 `internal` 引用 | `go list -deps` / ArchUnit |
| **禁用依赖** | 不许引入新 ORM/日志库 | `go list -m all` diff + 白名单 |
| **代码模式** | 金额字段必须带 `Cents`；禁止 `context.TODO()` 当默认值 | 自定义 lint（`go/analysis`）或 `rg` 门禁 |
| **契约存在性** | 每个对外错误码必须有文案 | 扫描常量 vs 字典文件 |
| **规则时效** | `AGENTS.md` 超过 90 天未更新且发生了 P1 | 脚本比对 git log |

### 脚本一：依赖方向 + 禁用依赖

```bash
#!/usr/bin/env bash
# scripts/arch-check.sh —— 在 CI 的 lint 阶段执行
set -euo pipefail

echo "== 1. 依赖方向：internal 不得引用 api =="
if go list -deps ./internal/... 2>/dev/null | grep -q '/api/'; then
  echo "❌ internal 反向依赖 api，违反分层"
  go list -deps ./internal/... | grep '/api/' | head
  exit 1
fi

echo "== 2. 禁用依赖白名单 =="
ALLOWED="github.com/go-sql-driver/mysql github.com/redis/go-redis/v9 github.com/segmentio/kafka-go"
# 列出直接依赖（排除标准库与已允许的）
NEW=$(go list -m -f '{{if not .Indirect}}{{.Path}}{{end}}' all \
      | grep -v -E '^(github.com/yourorg/)' \
      | grep -v -F -f <(echo "$ALLOWED" | tr ' ' '\n') || true)
if [ -n "$NEW" ]; then
  echo "⚠️ 检测到未在白名单的直接依赖："
  echo "$NEW"
  echo "如确需引入，请更新 AGENTS.md 与 scripts/arch-check.sh 的白名单，并在 PR 说明理由"
  exit 1
fi

echo "✅ 架构检查通过"
```

### 脚本二：代码模式门禁（金额单位 / ctx 误用）

```bash
#!/usr/bin/env bash
# scripts/pattern-check.sh
set -euo pipefail
fail=0

# 金额字段必须带单位后缀（Cents/Yuan），禁止裸 Amount
if rg -n --type go '\bAmount\s+(int64|float64)' --glob '!**/*_test.go' internal/ ; then
  echo "❌ 金额字段必须带单位：用 AmountCents / AmountYuan"
  fail=1
fi

# 禁止把 context.TODO() 当默认参数
if rg -n 'ctx\s*:=\s*context\.TODO\(\)' --glob '!**/*_test.go' . ; then
  echo "❌ 不要用 context.TODO() 当默认 ctx，从调用方传入"
  fail=1
fi

# 禁止空的 error 分支
if rg -n -U 'if err != nil \{\s*\}' --type go . ; then
  echo "❌ 空的 err 分支会吞错，至少 return 或记录"
  fail=1
fi

exit $fail
```

### 脚本三：契约存在性（错误码必须有文案）

```go
// cmd/errcheck/main.go —— 扫描代码里的 ErrXxx 常量，检查文案表是否覆盖
package main

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"strings"
)

func main() {
	// 1) 收集代码中所有对外错误码（约定：Err 前缀且全大写常量）
	codes := map[string]bool{}
	fset := token.NewFileSet()
	_ = filepathWalk(".", func(path string) {
		f, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil { return }
		ast.Inspect(f, func(n ast.Node) bool {
			if vs, ok := n.(*ast.ValueSpec); ok {
				for _, id := range vs.Names {
					if strings.HasPrefix(id.Name, "Err") && id.Name == strings.ToUpper(id.Name[3:]) {
						codes[id.Name] = true
					}
				}
			}
			return true
		})
	})

	// 2) 读文案表（YAML/JSON 皆可，这里用逐行 "CODE=文案"）
	b, err := os.ReadFile("internal/errcode/messages.txt")
	if err != nil { panic(err) }
	for _, line := range strings.Split(string(b), "\n") {
		if i := strings.IndexByte(line, '='); i > 0 {
			delete(codes, strings.TrimSpace(line[:i]))
		}
	}

	if len(codes) > 0 {
		fmt.Println("❌ 以下错误码缺少对外文案：", keys(codes))
		os.Exit(1)
	}
	fmt.Println("✅ 错误码文案齐全")
}
```

> 💡 **这三个脚本加起来不到 150 行，但它们把 `AGENTS.md` 从「文档」变成了「约束」**。没有它们，规则文件只是「建议」。

### CI 接线

```yaml
# .github/workflows/verify.yml
name: verify
on: [pull_request]
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with: { go-version: '1.23' }
      - run: go build ./...
      - run: go vet ./...
      - run: ./scripts/arch-check.sh
      - run: ./scripts/pattern-check.sh
      - run: go run ./cmd/errcheck
      - run: go test ./... -race -count=1
      - run: go run ./cmd/repomap . > /dev/null   # 确保 repomap 不会崩
```

---

## 48.10 事故：两个子 agent 并行，把同一件事做成了两套抽象

2025 年 12 月，一个团队用「子任务并行」加速开发：子会话 A 做「退款模块」，子会话 B 做「对账模块」。两者都需要「按金额分转元展示」，都自行实现了：

| 子会话 | 实现 | 位置 |
| --- | --- | --- |
| A | `money.CentsToYuan(c int64) string` | `internal/refund/money.go` |
| B | `util.FormatAmount(c int64) string` | `internal/reconcile/util.go` |

两者行为不同：A 用 `math.Round`（四舍五入），B 用截断。三个月后对账报表出现 0.01 元差异，排查 6 小时才发现是两套格式化。

**根因不是「模型笨」，而是「并行时只共享了契约，没共享『已有基础设施清单』」**——`money` 包其实早就存在，只是两个子会话的上下文里都没有它。

**修复方案（可复用）**：

```text
① 并行前，主会话先给出「可复用能力清单」（从 repo map 里筛出 util/money/id/lock 等）
② 子会话的 prompt 里硬性写：「禁止新建以下能力：金额格式化、ID 生成、分布式锁、日志、错误码」
③ 合并时跑 `rg "func .*(Yuan|FormatAmount|CentsTo)"` 检查是否出现第二套实现
④ 把这条加进 AGENTS.md 的「硬规矩」
```

```bash
# 合并前检查：同类能力是否出现两个以上实现
rg -n 'func\s+\w*(Yuan|Cents|Amount|Money)\w*\(' --type go internal/ | \
  sed -E 's/.*func +([A-Za-z0-9_]+).*/\1/' | sort | uniq -c | sort -rn | head
```

> 🩸 **并行的代价是「一致性」**。串行时模型会看到前面的实现并复用；并行时它看不到。**所以并行必须靠「清单 + 契约 + 合并检查」来补偿。**

---

## 48.11 规则文件的演进史（一张真实的 v1→v4）

规则文件不是一次写完的，它是「每次踩坑后加一条」的产物。某团队 12 个月的演进：

| 版本 | 触发事件 | 新增内容 | 字数 |
| --- | --- | --- | --- |
| v1 | 项目启动 | 技术栈、目录结构、`make verify` | 40 行 |
| v2 | 模型重复实现 `IdGenerator` | 「已有基础设施清单」+「先 rg 再建」 | 65 行 |
| v3 | 金额单位错（元/分）导致对账差 100 倍 | 「金额一律分，字段带 Cents」+ pattern-check | 90 行 |
| v4 | 并行子会话造出两套金额格式化 | 「禁止新建能力清单」+ 合并前 grep 检查 | 110 行 |

**四条经验**：

1. **每条规则都对应一次真实损失**（不是「最佳实践」抄来的）。没付出代价的规则，团队不会遵守。
2. **规则增长有上限**：超过 150 行就要拆分（`AGENTS.md` + `docs/agents/*.md` 按需引用）。
3. **规则必须有检查**：没有 CI 检查的规则，三个月后必然失效。
4. **规则要能删**：技术栈升级后（如从 MySQL 换 PG），旧规则要及时删，否则会误导模型。

---

## 48.12 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 规则文件 | 最高性价比；≤150 行；写「禁用项」比写「推荐项」有效 |
| repo map | 1~2k token 换全局视野；只放签名不放实现 |
| 先搜再写 | 一次 grep 200ms，省掉一次重复实现 |
| 依赖图 | 防反向依赖；ArchUnit / go list |
| 子任务 | 按模块隔离上下文，只共享契约 |
| 五层 | 规则/感知/执行/验证/治理，缺一层效果打折 |
| 七反模式 | 千行生成、无测试、上下文漂移、重复造轮、幻觉 API、不清理、无 review |
| 双修 | 同一份文档服务新人和模型；工具链是「可维护性」的显性化 |
| 一句话 | **工具链不提升模型智商，只消除「本可以不犯」的错。** |

## 48.13 自检题

1. **`AGENTS.md` 里最该写的是什么？为什么「禁用项」比「推荐项」重要？**
   > 要点：最该写「已有基础设施清单 + 硬规矩 + 常用命令」。禁用项重要是因为模型的默认行为是「生成一个看起来合理的实现」——你不禁止，它就会引入新的 ORM/日志库/ID 生成器，造成重复与不一致。推荐项它可能不采纳，禁用项可以被 CI/grep 检查。

2. **为什么子任务要「只共享契约，不共享上下文」？**
   > 要点：共享上下文会互相污染（A 的风格影响 B、历史越长越漂移）、撑爆窗口、且并行时无法隔离。只共享契约（接口/schema/测试）能保证可组合，合并时用契约测试验证，冲突面最小。

3. **团队说「我们用了模型，但合并率只有 30%」，你先查什么？**
   > 要点：① 有没有规则文件（没有 → 风格漂移、重复造轮）；② 有没有 `make verify`（没有 → 「看起来对」）；③ 单次任务粒度（是否一次上千行）；④ 测试谁写的（模型照实现写 → 假绿）；⑤ 有没有先 grep（重复实现）。按这五项打分，通常能定位到 1~2 个主因。

## 48.14 延伸阅读

- 上一章：[47. 工程方法：契约先行、小步验证](47-engineering-method-context.md)
- 下一章：[49. RAG 的本质与技术谱系](49-rag-essence-selection.md)
- 相关：[46. 小上下文写大项目](46-small-context-large-project.md)、[52. 落地清单与反模式集](52-landing-checklist-antipatterns.md)
- 平台侧：[../llm/15-context-budget.md](../llm/15-context-budget.md)（上下文预算）、[../llm/16-prompt-as-asset.md](../llm/16-prompt-as-asset.md)（提示词资产化）
- 总纲：[0-summary.md](0-summary.md)
