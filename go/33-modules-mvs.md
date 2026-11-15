# 33. 模块、版本与 MVS

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 工程化与服务端 ｜ **编号**：33 / 54
> **本章定位**：把 Java 工程师脑子里的「依赖仲裁」插头拔下来，换成 Go 的「最小版本选择」。Maven 的最近者胜是**仲裁**，Go 的 MVS 是**计算**——这一个字的差别，决定了两边完全不同的升级策略与事故形态。
> **读法**：如果你只想知道「为什么 go.mod 里的版本看起来那么旧」，直接看 33.3 的 MVS 图和 33.6 的坑表；要治理依赖安全的，看 33.5。

---

## 33.1 引子：从 Maven 过来的人，第一次被 go.sum 打脸

场景一，版本号：

同事把 `github.com/sirupsen/logrus` 从 v1.8.1 升到 v1.9.0，你 `go build` 通过，本地跑得好好的，推到 CI 上直接红：

```
missing go.sum entry for module providing package github.com/sirupsen/logrus;
to add: go mod download github.com/sirupsen/logrus
```

你心想：升级一个库还要手动改一个校验文件？这文件是二进制的？我得 commit 吗？更迷惑的是——**你明明没改 `go.mod` 里别的行，为什么 `go mod tidy` 之后有一堆带 `// indirect` 的行冒出来了？** 在 Maven 里，`pom.xml` 只有你写的那些，传递依赖是运行 `mvn` 时才拉出来给你看的。

场景二，私有仓库。你把公司内部库 `gitlab.acme.com/llm/sdk` 写进 `require`，然后：

```
fatal: could not read Username for 'https://gitlab.acme.com': terminal prompts disabled
```

或者更诡异的：能拉下来，但报 `410 Gone` / `checksum mismatch`，因为它去公网 `proxy.golang.org` 或 `sum.golang.org` 找你的私有库了。**Go 默认假设全世界的代码都是公开的**——这是它和 Maven 私服模型最不一样的地方。

这两个场景指向同一件事：Go 的模块系统不是「一个构建工具的功能」，而是一套**版本控制协议**。它规定了 `import` 路径怎么映射到版本、版本怎么比大小、冲突怎么解决、内容怎么验证。你得先接受它的规则，才能不跟它打架。

---

## 33.2 直觉模型：import 路径就是坐标

先建立一个脑图。Go 里**没有 `<groupId>/<artifactId>` 这套东西**，模块路径（module path）一个字符串干掉了 Maven 的三个概念：

```
import "github.com/acme/agent/internal/llm"
        └────────────┬──────────────┬──────────┘
                     │              │
              module path          包相对路径（子目录）
        github.com/acme/agent        internal/llm
                     │
        ┌────────────┴─────────────┐
        │ 版本：由 go.mod 决定      │
        │ v0.3.1 / v1.4.0 / v2.1.0  │
        └──────────────────────────┘
```

关键规则只有四条：

1. **一个模块 = 一个版本单元**。`github.com/acme/agent` 这个模块整体打一个 tag `v0.3.1`，里面的所有包都跟着走。不再有「每个 artifact 独立版本」。
2. **import 路径与版本解耦**。`go.mod` 里写 `require github.com/acme/agent v0.3.1`，代码里 import 不带版本号。升级 = 改 `go.mod` 一行，全仓库生效。
3. **语义化版本是硬约定**。`vMAJOR.MINOR.PATCH`，MAJOR 变了就**必须**改模块路径（加 `/v2`）。
4. **没有 scope、没有 exclusions 传递、没有 optional**。依赖只有一个维度：`require`。

| Maven 概念 | Go 对应 | 会猜错的地方 |
| --- | --- | --- |
| `groupId` + `artifactId` | module path（含域名） | 路径就是 import 路径，改名=全世界无法编译 |
| `version` | `require x v1.4.0` | 没有 `1.4.0-SNAPSHOT`，快照用伪版本 `v0.0.0-20250101120000-abcdef123456` |
| `<dependencyManagement>` / BOM | **无对应物** | Go 靠 MVS 计算，想「钉版本」就在主模块 `require` 里写高版本 |
| `<scope>test</scope>` | **无对应物** | 依赖就是依赖，测试依赖也进 `go.mod` |
| `<exclusions>` | `exclude`（只对主模块生效） | 治不了「依赖里塞了我不想要的库」 |
| 私服 Nexus/Artifactory | `GOPROXY` + `GOPRIVATE` | 换的是「代理协议」不是「仓库坐标」 |
| `mvn dependency:tree` | `go mod graph` / `go list -m all` | Go 输出的是「最终选版」，没有「被 omit」的分支 |

---

## 33.3 底层实现：go.mod / go.sum / MVS 三件套

### 33.3.1 go.mod 的真实形态

`go.mod` 语法极简，但每条指令都有坑。这是一份带全部指令的样板：

```go
module github.com/acme/agent

go 1.24            // 语言版本（1.21 起 = 语言特性开关 + 工具链行为）
toolchain go1.25.0 // 建议使用的最低工具链版本（1.21 引入）

require (
	github.com/cloudwego/eino v0.4.0
	github.com/openai/openai-go v1.12.0
	go.opentelemetry.io/otel v1.35.0
)

require (
	github.com/bytedance/sonic v1.13.2 // indirect：不是我直接 import 的
	golang.org/x/sync v0.13.0          // indirect
)

// 本地多仓库联调：只在本地生效，千万不要提交
replace github.com/acme/internal-llm => ../internal-llm

// 上游某个版本打坏了 wire 协议，整个跳过
exclude github.com/acme/legacy-sdk v1.4.2

// Go 1.24+：登记命令行工具依赖，用 `go tool mockery` 运行
tool github.com/vektra/mockery/v2
```

几个必须刻进肌肉记忆的点：

- `go 1.24` 不是「我用 Go 1.24 编译」，而是**语言版本**。模块里想用 1.22 的 range 语义，`go` 行必须 ≥ 1.22，否则编译器按老语义处理（这是 Go 少见的「同一个编译器、多种语言」设计）。
- `// indirect` 是**注释**，`go mod tidy` 自动维护。判断「这个依赖是谁拉进来的」用 `go mod why -m <path>`。
- `replace` **不传递**。你 replace 了 A，依赖你的模块不会看到这个 replace。所以「库作者用 replace 修 bug 让别人受益」是不成立的。
- `exclude` 只在主模块生效，而且只是让 MVS **不选**这个版本——如果它是唯一可用版本，构建就失败。

### 33.3.2 go.sum：不是「锁文件」，是「校验账本」

这是 Java 人最容易误解的一点。`go.sum` **不锁定版本**（版本在 `go.mod` 里），它只记录**内容哈希**，每个模块两行：

```
github.com/cloudwego/eino v0.4.0 h1:3Yr0mK7VvQzZ1kQ5…（整个 zip 的哈希，dirhash）
github.com/cloudwego/eino v0.4.0/go.mod h1:Pq8xM2nT9…（只有 go.mod 文件的哈希）
```

为什么要两行？因为计算完整依赖图时**只需要 go.mod**，不需要下载 zip。`/go.mod` 行让你能在不下载源码的情况下验证图，这对 MVS 的性能至关重要（只拉 `.mod` 文件，这是 `go mod graph` 能飞快的原因）。

`go.sum` 里有一条铁律：**它防的是「内容被篡改」，不是「版本被换掉」**。所以：

- 有人偷偷把 `v1.4.0` 的 tag 内容改了 → 哈希不匹配 → 构建失败。这是 Merkle 树的形式保证。
- 你自己改 `go.mod` 升版本 → `go.sum` 需要新增条目 → 否则 `missing go.sum entry`。
- 主模块自己的代码不在 `go.sum` 里（`go.sum` 管不着你的仓库，那归 git 管）。

默认启用 `GOSUMDB=sum.golang.org`（Google 运营的透明日志），每次首次拉取新模块会去核对哈希是否与全球日志一致——这叫 **checksum database**。私有库当然不该被核对，所以有 `GOPRIVATE`。

### 33.3.3 MVS：不是「仲裁」，是「算出一个不动点」

Russ Cox 在 *Minimal Version Selection*（2016）里的核心洞察：**版本冲突之所以在 Maven 里是噩梦，是因为「选版本」被设计成了一次带优先级的仲裁；如果改成「选所有要求里的最大者」，问题就退化成一次简单的图计算。**

先看两个模型的差异（这是本章最重要的图）：

```
【Maven 最近者胜 / nearest-wins：看「谁离根近」】
  agent ──► llm v1.4.0 ──► core v1.2.0
    └────► rag v2.1.0 ──► core v1.5.0
    └────► tool v0.9.0 ─► core v1.3.0
  深度：core v1.2.0 距离 2（经 llm）；core v1.5.0 距离 2（经 rag）；core v1.3.0 距离 2
  → 同深度取「先声明者」⇒ 选 core v1.2.0 ⚠️ 于是 rag 拿到一个它没测过的 core

【Go MVS：取所有「要求的下限」中的最大值】
  agent require: llm v1.4.0, rag v2.1.0, tool v0.9.0
  llm  v1.4.0 的 go.mod: require core v1.2.0
  rag  v2.1.0 的 go.mod: require core v1.5.0
  tool v0.9.0 的 go.mod: require core v1.3.0
                        ↓
       selected[core] = max(v1.2.0, v1.5.0, v1.3.0) = v1.5.0 ✅
       每个库拿到的 core 都 ≥ 它自己声明的下限 ⇒ 语义化版本承诺成立
```

再看它的「不动点」性质，这是 MVS 最优雅的地方：

```
        ┌──────────────────────────────────────────┐
        │ 输入：主模块 require 列表（起点）         │
        └────────────────┬─────────────────────────┘
                         ▼
        ┌──────────────────────────────────────────┐
        │ 取每个模块的「最大被要求版本」            │
        │ selected[m] = max{ req(v) | v 在图中 }    │
        └────────────────┬─────────────────────────┘
                         ▼
        ┌──────────────────────────────────────────┐
        │ 读取 selected[m] 自己的 go.mod，把它的    │
        │ require 加进图；若抬高了某模块版本，回边  │
        └────────────────┬─────────────────────────┘
                         ▼        没有变化
                    ┌────────────┴────────────┐
                    │ 有变化 → 回到第一步（迭代）│
                    └────────────┬────────────┘
                                 ▼
                        输出：go.sum 之外的完整 build list
```

**注意一个反直觉后果**：MVS **只会抬高版本，永远不会降低**。你想降级某个库，`go get x@v1.0.0` 之后它可能还是被别人的 `require` 抬回来——这时唯一的手段是 `replace x => x v1.0.0`（强制替换，绕过 MVS）。这就是「Go 没有 `dependencyManagement`」的代价：降级是特权操作。

官方实现就在 `golang.org/x/mod/mvs` 包里（`cmd/go` 通过 `cmd/go/internal/modload` 的 `mvsReqs` 适配它），核心 API 是 `mvs.BuildList`（选版）、`mvs.Downgrade`、`mvs.Upgrade`、`mvs.Graph`。想把它跑起来，最简单的办法是自己写 40 行：

```go
package main

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
)

// reqs[module][version] = 该版本自己的 go.mod 里声明的 require 下限
var reqs = map[string]map[string]map[string]string{
	"example.com/llm":  {"v1.4.0": {"example.com/core": "v1.2.0"}},
	"example.com/rag":  {"v2.1.0": {"example.com/core": "v1.5.0"}},
	"example.com/tool": {"v0.9.0": {"example.com/core": "v1.3.0"}},
	"example.com/core": {"v1.2.0": {}, "v1.3.0": {}, "v1.5.0": {}},
}

func parse(v string) [3]int {
	var out [3]int
	for i, s := range strings.Split(strings.TrimPrefix(v, "v"), ".") {
		if i > 2 {
			break
		}
		out[i], _ = strconv.Atoi(s)
	}
	return out
}

func less(a, b string) bool {
	x, y := parse(a), parse(b)
	for i := 0; i < 3; i++ {
		if x[i] != y[i] {
			return x[i] < y[i]
		}
	}
	return false
}

// mvs 从主模块的 require 出发，迭代到不动点
func mvs(root map[string]string) map[string]string {
	sel := map[string]string{}
	queue := make([]string, 0, len(root))
	for m, v := range root {
		sel[m], queue = v, append(queue, m)
	}
	for len(queue) > 0 {
		m := queue[0]
		queue = queue[1:]
		for rm, rv := range reqs[m][sel[m]] {
			if cur, ok := sel[rm]; !ok || less(cur, rv) { // 只抬不降
				sel[rm] = rv
				queue = append(queue, rm) // 版本变了，它的要求要重读
			}
		}
	}
	return sel
}

func main() {
	sel := mvs(map[string]string{
		"example.com/llm":  "v1.4.0",
		"example.com/rag":  "v2.1.0",
		"example.com/tool": "v0.9.0",
	})
	keys := make([]string, 0, len(sel))
	for k := range sel {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		fmt.Printf("%-20s %s\n", k, sel[k]) // core 会输出 v1.5.0
	}
}
```

真实世界里 `mvs.BuildList` 还要处理 `go` 指令兼容性、`exclude`、`retract`、模块图裁剪（Go 1.17+ 只加载「能影响构建」的那部分 go.mod），但骨架就是这个循环。

> 💡 **一句话记住 MVS**：**Maven 是在吵架中选一个赢家；Go 是所有人的底线取最大值，然后闭嘴。**

---

## 33.4 Java 对照：为什么 Go 敢「不锁版本」

| 维度 | Maven | Gradle | Go |
| --- | --- | --- | --- |
| 冲突解决 | 最近者胜（深度优先，同深度先声明者胜） | 默认取最高版本 | MVS：取所有下限的最大值 |
| 能否降级 | 能（直接声明低版本 or `dependencyManagement`） | 能（`resolutionStrategy.force`） | 不能，只能 `replace` |
| 依赖图规模 | 全图（含所有 optional/exclusions） | 全图 | **裁剪过**的图（1.17+ pruning） |
| 「为什么这个版本被选中」 | `dependency:tree -Dverbose` 看 omitted | `dependencies --configuration` | `go mod why -m x` + `go list -m all` |
| 内容验证 | 校验和 `_remote.repositories`（弱） | 同左 | `go.sum` + 透明日志 `sum.golang.org` |
| 版本不可变性 | SNAPSHOT 可变，release 一般假设不可变 | 同 | **强制不可变**：tag 内容一变，哈希对不上就报错 |

**Go 为什么这样选？** 因为 Go 的野心是「`go build` 在任何机器上重放出同一个二进制」，这是静态链接 + 可重复构建的必要条件。要做到这点，必须满足两条：① 内容不可变且有密码学校验；② 版本选择是**纯函数**——同样的 `go.mod` 一定算出同样的 build list，不依赖本地 `~/.m2` 里恰好有什么、也不依赖解析顺序。

代价也说清楚：**MVS 放弃了「优雅降级」的能力**。当你依赖的 A 库非要用 `x v2`，而你依赖的 B 库在 `x v1` 上还没适配时，Maven 能靠最近者胜给你个「凑合能跑」的树，Go 会直接把 `x v2` 拿给你，**B 库在编译期就炸**。这不是 bug，是设计：Go 认为「让不兼容在编译期暴露」比「运行期玄学失败」便宜得多。

---

## 33.5 工程落地：四张必须会写的配置

### 33.5.1 语义化版本与 `/v2` 的硬规则

规则很短，但错误率极高：

- `v0.x.y`：**不承诺兼容**，随便破。
- `v1.x.y`：承诺兼容，只加不改。
- `v2.0.0` 及以上：模块路径**必须**以 `/v2` 结尾，且 `go.mod` 第一行也要带。

所以一个库升 v2 有两条路：

```text
方案 A（推荐）：同仓库建子目录 v2/
  repo/                    module github.com/acme/sdk      → tag v1.9.0
  repo/v2/                 module github.com/acme/sdk/v2   → tag v2.0.0
  import "github.com/acme/sdk/v2"   ← 编译期就能并存 v1 与 v2

方案 B（不推荐）：仓库无 go.mod 时代的老包 → 版本号加 +incompatible
  require github.com/old/lib v2.1.0+incompatible
```

方案 A 的妙处：v1 与 v2 **可以在同一个二进制里共存**，这对渐进升级太友好了。Java 里想同时用 Jackson 2 和 3 得靠 shading，Go 里改个 import 路径就行。

伪版本（pseudo-version）也顺手记一下，`go get repo@master` 生成的：

```
v0.0.0-20250101120000-abcdef123456     ← 没有任何 tag 时
v1.4.1-0.20250101120000-abcdef123456   ← 基于 v1.4.0 之后的提交
```

它按 semver 排序天然小于「下一个小版本」，所以 `go get -u` 能正常越过它。

### 33.5.2 私有仓库：GOPRIVATE 三兄弟

```bash
# 一条命令解决 90% 的私有库问题：让匹配的模块绕过 proxy 与 sumdb
go env -w GOPRIVATE='gitlab.acme.com/*,github.com/acme/*'

# 它其实等价于同时设置下面两个（用得更细时才需要单独写）
go env -w GONOPROXY='gitlab.acme.com/*'   # 不走 proxy，直接 git clone
go env -w GONOSUMDB='gitlab.acme.com/*'   # 不查 sum.golang.org

# 自建 Athens / JFrog 作为公司级 proxy，并用 fallback 直连
go env -w GOPROXY='https://athens.acme.com,direct'
go env -w GOSUMDB='sum.golang.org'        # 或自建 sumdb，或 off（不推荐）
```

`GOPRIVATE` 的本质是**声明「这些路径不是公共代码」**，于是 Go 换用 `git`/`hg` 协议直接克隆（走你本机的 SSH key 或 `~/.netrc`），并跳过透明日志核对。CI 里通常这样喂凭证：

```yaml
# GitHub Actions 片段：把 SSH key 与 git insteadOf 配好，go mod download 就不会卡在终端提示
- name: Configure private modules
  run: |
    mkdir -p ~/.ssh && echo "${{ secrets.GITLAB_DEPLOY_KEY }}" > ~/.ssh/id_ed25519
    chmod 600 ~/.ssh/id_ed25519
    ssh-keyscan gitlab.acme.com >> ~/.ssh/known_hosts
    git config --global url."git@gitlab.acme.com:".insteadOf "https://gitlab.acme.com/"
    go env -w GOPRIVATE='gitlab.acme.com/*'
- run: go mod download
```

### 33.5.3 go.work：多模块本地联调（不要提交）

你同时改 `agent` 和它依赖的 `sdk`，不想每次 `replace` 加一行再删掉：

```bash
go work init ./agent ./sdk          # 生成 go.work
go work use ./eino-ext              # 再加一个目录
go work sync                        # 把 workspace 的选版结果写回各模块 go.mod
GOWORK=off go build ./...            # 想验证「CI 视角」时临时关掉
```

`go.work` **必须加进 `.gitignore`**。它里面是相对路径，只对你机器有意义；提交上去会让所有同事的构建都失败。

### 33.5.4 依赖治理：retract + govulncheck

**库作者撤回坏版本**：发一个新版本，在新版本的 `go.mod` 里写 `retract`（旧版本的 go.mod 是历史，改不了）：

```go
module github.com/acme/sdk

go 1.24

retract (
	v1.4.2            // 手滑删了一个导出字段
	[v1.5.0, v1.5.3]  // 整批依赖了有 CVE 的库
)
```

`retract` 不会删除已下载的代码（那不可能），它让 `go get -u` / `go list -m -u` 明确告诉你「不要用这个版本」，并给出替代建议。发布方之外的人不能 retract。

**安全扫描**：`govulncheck` 和 Maven 的 OWASP dependency-check 最大的区别是——它做**可达性分析**：

```bash
go install golang.org/x/vuln/cmd/govulncheck@latest
govulncheck ./...              # 只报告「你的代码真的会调用到」的漏洞
govulncheck -mode=binary ./bin/agent   # 扫编译产物，用于已上线二进制的审计
govulncheck -show verbose ./...
```

它把「依赖里有 CVE」和「你的调用链真的会执行到那个脆弱函数」分开报告，噪音比单纯扫版本号小一个数量级——因为 Go 的脆弱性数据库（vuln.go.dev）记录了每个漏洞的**受影响符号**。

CI 里最省心的四行「模块体检」：

```yaml
- name: module hygiene
  run: |
    go mod tidy -diff                    # 1.23+：只校验不落盘，脏了就退出非 0
    test -z "$(git status --porcelain go.mod go.sum)" || (echo "go.mod/go.sum 不同步"; exit 1)
    go list -m -u all                    # 有可用升级的模块会被标出来
    govulncheck -test ./...
```

---

## 33.6 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| `missing go.sum entry` | 改了 `require` 没同步 `go.sum` | `go mod tidy` 后**提交 go.sum**；CI 用 `go mod tidy -diff` 卡住 | 33.3.2 |
| CI 能过、本地报 `checksum mismatch` | 上游 tag 内容被改，或本地 `GOMODCACHE` 脏 | `go clean -modcache` 后重下；向库作者举报 | 33.3.2 |
| 升级 A 库后 B 库编译报错 | MVS 把共享依赖抬到了 B 没适配的版本 | 找 B 的兼容版本，或临时 `replace` 兜底，别指望自动降级 | 33.3.3 |
| `go get x@v2.1.0` 报 `invalid version` | v2+ 模块路径没带 `/v2` | 用 `x/v2@v2.1.0`；老无 go.mod 仓库用 `+incompatible` | 33.5.1 |
| 私有库 401 / 410 / 超时 | 走了公网 proxy 与 sumdb；git 凭证缺失 | 设 `GOPRIVATE` + `insteadOf` + 部署密钥 | 33.5.2 |
| 提交了带 `../sdk` 的 `replace` | 本地联调残留 | `replace` 用相对路径一律不进主分支；联调改用 `go.work`（且 gitignore） | 33.5.3 |
| 刚上线的版本有严重 bug 想让大家别用 | 以为能删 tag | 不能删（不可变），只能 `retract` + 发新版本 | 33.5.4 |
| `go mod tidy` 删掉了「没人 import」的依赖 | 某依赖只被 build tag / 生成代码用 | 补 `//go:build` 或用 `tool` 指令/tools.go 显式保留 | 33.5.1 |
| 依赖数量失控、`go.sum` 上千行 | 没有准入机制 | 定期 `go mod graph` 查重、`govulncheck`、库 PR 里要求说明新增依赖理由 | 33.5.4 |

---

## 33.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| module path | 就是 import 路径前缀；改它等于对全世界做破坏性变更 |
| `go.mod` | 声明 `module/go/toolchain/require/replace/exclude/retract/tool`，越少越健康 |
| `go.sum` | 内容哈希账本，不是锁文件；必须提交；`/go.mod` 行让 MVS 不必下 zip |
| 伪版本 | `v0.0.0-时间-提交哈希`，无 tag 时的版本表达式，按 semver 排序 |
| `/v2` | MAJOR ≥ 2 必须进模块路径，v1 与 v2 可同时存在于一个二进制 |
| **MVS** | 取所有下限的最大值，迭代到不动点；**只会抬高，不会降低** |
| 降级 | 唯一手段是 `replace`；`exclude` 只是「不许选这个版本」 |
| 裁剪 | Go 1.17+ 只加载影响构建的 go.mod，`go.sum` 因此瘦身 |
| `go.work` | 多模块本地联调，相对路径，**必须 gitignore** |
| 私有库 | `GOPRIVATE` 一条搞定 proxy + sumdb 绕过；git 凭证另配 |
| `govulncheck` | 按「可达符号」报漏洞，不是按版本号刷噪音 |
| retract | 库作者撤回坏版本的唯一合法手段，发个新版本写进去 |

## 33.8 自检题

1. **`agent` 依赖 `llm v1.4.0` 与 `rag v2.1.0`，两者分别要求 `core v1.2.0` / `core v1.5.0`。Maven 和 Go 各选哪个版本？如果 `core v1.5.0` 让 `llm` 编译失败，谁该被修？**
   > 答案要点：Maven 若两者深度相同取先声明者，可能选 v1.2.0（更浅的路径优先）；Go 一定选 v1.5.0。如果 llm 在 v1.5.0 下编译失败，说明 llm 违反了「v1 内部必须兼容」的语义化版本承诺，应修 llm（或其 go.mod 里的下限声明），而不是让 Go 去降级。

2. **`go.sum` 和 Maven 的锁有什么本质不同？为什么它是「必须提交」的？**
   > 答案要点：`go.sum` 不含选版信息（选版由 `go.mod` + MVS 计算），只记录模块内容的 `h1:` 哈希，用于检测内容篡改与中间人替换；不提交会导致 CI 无法验证依赖完整性并报 `missing go.sum entry`。真正的「锁」在语义上由 `go.mod` 的完整 require 列表（含 indirect）承担。

3. **你在本地给 `sdk` 加了 `replace => ../sdk` 方便联调，为什么绝对不能推到主分支？正确的联调姿势是什么？**
   > 答案要点：`replace` 使用相对路径时只对本机有意义，别人检出后路径不存在直接构建失败；而且 `replace` 不传递给下游模块。正确做法是 `go.work`（相对路径同样只对本地有效，但独立文件可 gitignore），或者用 CI 发布 pre-release tag + 显式 `go get`。

## 33.9 延伸阅读

- Russ Cox, *Minimal Version Selection*（2016）与 *Our Software Dependency Problem*（2019）——MVS 与依赖安全的原始论证
- `golang.org/x/mod/mvs`：`BuildList` / `Graph` / `Downgrade` / `Upgrade` 的实现（几百行，值得整篇读）
- `cmd/go/internal/modload`：`type mvsReqs` 如何把 go.mod 世界适配成 `mvs.Reqs`
- 官方文档：`go help modules`、`go help mod tidy`、`go help module-auth`、`go help vcs`、`go help work`
- `golang.org/x/vuln/scan` + `vuln.go.dev`：漏洞库的数仓与可达性分析原理
- 交叉引用：[第 2 章](2-toolchain-layout.md) 讲 `go.mod` 在工程骨架中的位置与 Docker 构建

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 32 章](32-high-concurrency.md) 高并发实战（依赖治理是上线前的最后一道闸）
- 下一章：[第 34 章](34-testing.md) 测试：表驱动、mock、fuzz 与可重放
