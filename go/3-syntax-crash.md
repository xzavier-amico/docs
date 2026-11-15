# 3. 语法极速通道 + 高频陷阱

> **状态**：`已完成（初稿）` ｜ **所属**：第一部分 · 心智迁移：Java → Go ｜ **编号**：3 / 54
> **本章定位**：全书**唯一**允许罗列语法的一章。目标不是让你记住语法（IDE 和 AI 都能补），而是把「Java 语法直觉会在 Go 里静默出错」的地方一次清完。之后的 51 章全部按问题驱动写，不再回头讲语法。
> **读法**：`3.2` 的映射表当字典查；`3.3` 的七个陷阱必须亲手敲一遍（多数配了可直接 `go run` 的完整程序）；`3.6` 直接进 CR 模板。

---

## 3.1 引子：23 个编译错误，其中 20 个不是「语法」问题

把一段 30 行的 Java 翻译成 Go，IDE 甩出 23 条红字。你逐条看下去，会发现：

- 只有 3 条是「我不知道这个符号」——`:=`、`range`、`defer` 这种，五分钟学会。
- 20 条是**语义级别**的：`int32 + int` 报错（Java 会自动提升）、`switch` 不加 `break` 反而报错（Java 里不加会 fallthrough）、`for` 循环里 `v` 改了不生效（Java 里 `for (User u : list) u.setX()` 是生效的）、声明了没用的变量直接编译失败（Java 只是黄线警告）。

**这类错误的可怕之处是：它们编译通过之后依然会静默出错。** 下面这段代码能编译、能跑、结果不对：

```go
// 线上 bug 原型：为什么我的重试逻辑从来没生效过？
func fetch(c *client) ([]byte, error) {
	var err error
	if c.stale() {
		data, err := c.reload() // ⚠️ := 创建了一个新的 err，作用域只在 if 块内
		_ = data
	}
	return nil, err // 永远是 nil —— 外面看到的「成功」其实是失败
}
```

`go vet` 抓不到它，`staticcheck` 也不一定。这就是为什么这章要存在。

> 🎯 本章的立场：**Go 的语法量是 Java 的三分之一，但「直觉映射」的失真率更高。** 因为 Go 把 Java 里靠约定和框架兜住的东西（作用域、拷贝语义、可见性）全部压到了语言层面，一旦你的直觉错了，没有任何框架帮你兜。

---

## 3.2 直觉模型：Go 只有 5 种骨架

忘掉「Java 有 60 个关键字」，Go 的全部语法可以归成五类，你只要把 Java 的对应物映射进去就行：

| Java 写法 | Go 写法 | 你在哪会错 |
| --- | --- | --- |
| `class Foo { private int x; }` | `type Foo struct { x int }` | 小写字段包外不可见；没有构造器 |
| `interface Foo { void bar(); }` + `implements` | `type Foo interface { Bar() }`，**隐式满足** | 不用写 `implements`，方法签名对上就行 |
| `int f() throws X` | `func f() (int, error)` | 多重返回值是语言级特性，不是 Tuple 包装类 |
| `for (int i=0;i<n;i++)` / `for (X x: xs)` / `while(c)` | 全是 `for`：`for i:=0;i<n;i++` / `for _, x := range xs` / `for c` | `while` 不存在；`for c` 就是 while；`for {}` 是死循环 |
| `switch`（默认穿透，靠 `break` 停） | `switch`（默认不穿透，`fallthrough` 才穿透） | **语义正好相反** |
| `try/catch/finally` | `error` 返回值 + `defer` | `finally` 的对应物是 `defer`（第 8 章） |
| `synchronized` / `volatile` | `sync.Mutex` / 无（用 channel、atomic） | 没有 volatile（第 23 章） |
| `x.toString()` | `String()` 方法 + `fmt.Stringer` | 没有祖先 `Object`，没有默认方法 |
| `Integer.MAX_VALUE` | `math.MaxInt64`、`math.MaxInt` | 常量是**无类型**的，算术规则完全不同 |
| `a ? b : c` | **没有** | 只能写 `if/else` 或抽个函数 |

一个必须马上记住的差异：**Go 没有隐式数值转换。** `int32` 和 `int` 是两个不同的类型，`a + b` 直接编译错误，必须 `int(a) + b`。Java 的自动拓宽（`byte → int → long → double`）在 Go 里一条都不存在。好处是：所有「悄悄溢出/悄悄改精度」的 bug 都变成编译错误。

---

## 3.3 七个高频陷阱，逐个解剖

### 3.3.1 `:=` 与作用域：影子变量的杀人现场

`:=` 的规则只有两条，但每条都能咬人：

1. **左侧至少有一个新变量**，否则报 `no new variables on left side of :=`。
2. **`:=` 会尽量「复用」同一作用域里已存在的变量**——但**跨作用域就一定是新建**。`if` / `for` / `switch` 的初始化语句各自开一个新作用域。

```
func f() error {                      ← 作用域 A：err(A)
    var err error
    if cond {                         ← 作用域 B
        data, err := load()           ← err(B) 是新变量！
        _ = data                           ↑ 遮蔽了 err(A)
    }                                 ← 作用域 B 结束，err(B) 消失
    return err                        ← 还是 err(A)，永远 nil
}
```

三行修好它（任选）：

```go
data, err := load()      // ① 把声明提到外层，用 = 而不是 :=
if err != nil { ... }

// ② 或者用 if 的初始化语句，让 err 天然活到 if 结束（这是 Go 最地道的写法）
if data, err := load(); err != nil {
    return fmt.Errorf("load: %w", err)
} else {
    use(data)
}
```

> 💡 **正面用法**：`if err := do(); err != nil {}` 这个模式是 Go 的「局部错误作用域」，能极大缩短变量的生命周期（对逃逸分析友好，详见第 14 章）。不要因为怕影子变量就放弃它——怕的是**和外面的同名变量混用**。

### 3.3.2 `range`：三件你可能全都猜错的事

```go
package main

import "fmt"

type User struct{ Name string }

func main() {
	users := []User{{"a"}, {"b"}}

	for _, u := range users { // ① u 是元素副本
		u.Name = "x" // 改的是副本，users 完全没变
	}
	fmt.Println(users[0].Name) // "a"

	for i := range users {
		users[i].Name = "x" // 要改就按索引改
	}
	fmt.Println(users[0].Name) // "x"

	for i := range 3 { // ② Go 1.22+：range over int
		fmt.Print(i, " ") // 0 1 2
	}
	fmt.Println()

	fns := make([]func(), 0, 3)
	for i := range 3 {
		fns = append(fns, func() { fmt.Print(i) }) // ③ Go 1.22+：每轮独立 i
	}
	for _, f := range fns {
		f() // 012；1.22 之前是 222
	}
}
```

三条要点：

- **值拷贝**：`for _, u := range` 拿到的 `u` 是元素副本（Java 的 `for (User u : list)` 拿的是引用，所以 `u.setName()` 生效）。这条是**最高频**的 Java→Go bug。
- **1.22 的循环变量语义**：`for` 声明的变量改成**每轮迭代新建**。⚠️ 但它由 `go.mod` 的 `go` 行控制——`go 1.21` 的老模块仍然是旧语义。这不是运行时开关，是**编译期按模块版本选择的语义**。
- **额外能力**：`for i := range 10`（1.22）和 `for x := range seq`（1.23 的 `range-over-func`，配合 `iter.Seq`）。map 的遍历顺序是**随机化**的（第 12 章讲原理），string 的 `range` 按 **rune** 走而索引是**字节**偏移（第 13 章）。

### 3.3.3 `switch`、`fallthrough`、没有 `while`

```go
package main

import "fmt"

func classify(n int) string {
	switch {
	case n < 0: // 无表达式的 switch = 更干净的 if/else if 链
		return "neg"
	case n == 0:
		return "zero"
	default:
		return "pos"
	}
}

func main() {
	x := 2
	switch x {
	case 1:
		fmt.Println("one")
		fallthrough // 显式穿透：无条件跳到下一个 case 的**函数体**，不再判断条件
	case 2:
		fmt.Println("two") // 不会自动落到 case 3，Java 老手这点要改
	case 3:
		fmt.Println("three")
	}

outer: // 标签 + break = Java 的 break LABEL
	for i := 0; i < 3; i++ {
		for j := 0; j < 3; j++ {
			if j == 1 {
				break outer
			}
		}
	}

	i := 0
	for i < 3 { // 这就是 while
		i++
	}
	for { // 这就是 while(true)
		break
	}
}
```

要点：`switch` **默认不穿透**（与 Java 相反，`break` 可有可无）；`fallthrough` 只能出现在 case 末尾、不能用于最后一个 case、不能用于 type switch；`switch` 可以带初始化语句 `switch v := f(); v {`；`switch x := v.(type)` 是类型分支（第 5 章）。

**没有三元运算符**，也没有方法重载、默认参数、运算符重载、`do-while`。这些「少」在写小工具时很烦，在读别人代码时很爽——这也是第 1 章那条「可读性 > 表达力」的直接体现。

### 3.3.4 可见性与 struct tag：编译器和运行时的两套规则

可见性看**首字母**，对包级名字、struct 字段、方法、接口方法全都适用：大写 = 导出（public），小写 = 包内（private）。没有 `protected`，没有模块级可见性（`internal/` 是另一层机制，见第 2 章）。

struct tag 是 Go 里唯一「字符串里藏元数据」的地方，也是唯一大量使用**反射**的地方：

```go
package main

import (
	"encoding/json"
	"fmt"
	"reflect"
)

type ChatRequest struct {
	Model     string  `json:"model"`
	MaxTokens int     `json:"max_tokens,omitempty"` // 零值时整个字段不出现在 JSON 里
	Temp      float64 `json:"temperature"`
	apiKey    string  `json:"api_key"` // ⚠️ 小写字段：encoding/json 根本看不见它
	Broken    string  `json:"broken, omitempty"` // ⚠️ 多了一个空格：tag 解析成 key="broken"，option=" omitempty"（无效，静默忽略）
}

func main() {
	b, _ := json.Marshal(ChatRequest{Model: "gpt", Temp: 0.7})
	fmt.Println(string(b)) // {"model":"gpt","temperature":0.7}

	t, _ := reflect.TypeOf(ChatRequest{}).FieldByName("MaxTokens")
	fmt.Println(t.Tag.Get("json")) // max_tokens,omitempty
}
```

三条铁律：**① tag 是反引号原始字符串，写错不报错、静默失效**（`go vet` 的 `structtag` 分析器能抓一部分）；**② JSON 只能序列化导出字段**，小写字段不报错、直接消失；**③ `omitempty` 对 `0`、`""`、`false`、`nil` 一律省略**——所以「用户真的把温度设成 0」和「用户没设置」在 JSON 层是分不开的（这个谎言的完整讨论在第 9、31 章）。

### 3.3.5 `struct{}` 的三种用途

`struct{}` 是**零大小类型**：`unsafe.Sizeof(struct{}{}) == 0`。它在 runtime 里有一个全局单例地址 `runtime.zerobase`，所有零大小对象都指向它，所以**不占内存**。三种用法：

```go
package main

import (
	"fmt"
	"sync"
)

func main() {
	// 用途一：集合（set）。Java 用 HashSet<T>，Go 用 map[T]struct{}
	seen := map[string]struct{}{}
	for _, w := range []string{"a", "b", "a"} {
		if _, ok := seen[w]; !ok {
			seen[w] = struct{}{} // 值不占空间
			fmt.Println("new:", w)
		}
	}

	// 用途二：纯信号 channel。只关心「发生了」，不关心「是什么」
	done := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(1)
	go func() { defer wg.Done(); close(done) }() // close 即可广播给所有接收者
	wg.Wait()
	<-done

	// 用途三：占位。如「不需要状态」的方法接收者、只为了对齐签名的空参数
	type NoState struct{}
	var _ = NoState{}
}
```

比起 `map[string]bool`（每个 value 占 1 字节 + 对齐），`map[string]struct{}` 更省；比起 `chan bool`，`chan struct{}` 语义更准确（**没有「值」的概念，只有事件**）。第三条用途的变体是接口实现：`type empty struct{}` 实现一个无状态接口。

### 3.3.6 `init` 顺序：一条被忽略的启动路径

Go 没有静态初始化块，对应的东西是三层：**包级变量初始化 → `init()` 函数 → `main()`**。顺序规则决定了「配置什么时候可用」这类玄学问题：

```
  main 包
  ├── import 依赖图（深度优先，同一包只初始化一次）
  │     ├── internal/config   ← 先：包级 var → init() → 再返回
  │     └── internal/llm      ← 后
  └── main 包自身
        ├── ① 包级变量：按「依赖关系」排序，被依赖的先算（不是简单的上到下）
        ├── ② init() 函数：同一文件内按声明顺序；不同文件按「文件名」顺序（编译期排序）
        └── ③ main()
```

```go
package main

import "fmt"

var a = b + 1       // 依赖 b，所以 b 先算 → a = 3
var b = computeB()  // 2

func computeB() int { fmt.Println("init: b"); return 2 }

func init() { fmt.Println("init: first init(), a =", a) }
func init() { fmt.Println("init: second init()") } // 一个文件可以有多个 init()

func main() { fmt.Println("main, a =", a) }
// 输出顺序：init: b → init: first init(), a = 3 → init: second init() → main, a = 3
```

要点：包级变量按**依赖**排序而不是书写顺序（所以上例里 `b` 先于 `a`）；`init()` 不能有参数、不能被调用、不能是方法；**`import _ "pkg"` 的唯一作用就是触发对方的 `init()`**（driver 注册就是这么干的）；`init` 里 panic 会直接终止进程，且栈很难读——**别在 `init` 里做 IO、别读配置、别连数据库**，那是 `main` 的活。

### 3.3.7 字符串、rune 与无类型常量

```go
package main

import (
	"fmt"
	"strconv"
	"unicode/utf8"
)

func main() {
	s := "中a"
	fmt.Println(len(s))                    // 4：字节数，不是字符数（UTF-8 下中文 3 字节）
	fmt.Println(s[0])                      // 228（0xe4）：byte，不是 char
	for i, r := range s {                  // i 是字节偏移，r 是 rune（int32）
		fmt.Println(i, r, string(r))
	}
	fmt.Println(string(rune(65)))          // "A"
	fmt.Println(string(65))                // "A"（⚠️ 无类型常量 65 被当 rune，不是 "65"！）
	fmt.Println(strconv.Itoa(65))          // "65"：想把数字变成字符串用这个
	fmt.Println(utf8.RuneCountInString(s)) // 2：真正的字符数
}
```

Go 的 `string` 是**不可变的字节序列 + 长度**（第 13 章讲 header 与零拷贝）。另外无类型常量的算术规则和 Java 不同：`5/2 == 2`（整数常量除法），`1e9` 是浮点常量，`math.MaxInt64` 是无类型常量可以塞进任何够大的整型。这类细节只在写数值/协议代码时要命。

---

## 3.4 Java 对照：为什么这些「少」是有意的

| 维度 | Java | Go | Go 的取舍理由 |
| --- | --- | --- | --- |
| 循环 | `for` / `while` / `do` / foreach 四种 | 只有 `for` | 一种结构覆盖全部语义，读代码不用切换心智 |
| switch | 默认穿透，靠 `break` | 默认不穿透 | 99% 的场景根本不想要穿透，`fallthrough` 显式表达罕见的 1% |
| 表达式 | 三元、`instanceof ?:` 遍地 | 无三元 | 嵌套三元是可读性头号杀手 |
| 可见性 | 4 个修饰符 + 模块 + 包 | 首字母大小写 + `internal/` | 没有 4 个修饰符的排列组合要记 |
| 未使用变量 | 警告 | **编译错误** | 死代码是 bug 的温床；Go 宁可让你用 `_` 显式声明「我故意丢弃」 |
| 隐式数值转换 | 拓宽自动、窄化 `(int)` | 一律显式 | 精度丢失必须被写出来 |
| 泛型 | `<T>` | `[T any]`（第 6 章） | 用方括号是为了不让解析器歧义（`f(x<T>)` 在 Go 里是合法的表达式） |
| 常量 | `static final` | `const` + `iota` + 无类型常量 | 编译期常量折叠，无装箱 |

---

## 3.5 框架落地：这些语法在真实代码里长什么样

- **`gin` 的 handler 就是普通函数**：`func(c *gin.Context)`，`c` 是唯一上下文（不是 `ThreadLocal`，第 24 章对比）。`gin.H` 是 `map[string]any` 的别名，`any` = `interface{}`（1.18 起）。
- **`langchaingo` 的 option 模式**：`llms.WithTemperature(0.7)` 返回 `func(*CallOptions) error`，本质就是「函数是一等公民 + 变参」两件语法的组合（第 10、36 章）。
- **`eino` 的流式**：`schema.StreamReader[T]` 之类泛型容器的对外 API 全靠 `range-over-func` + `iter.Seq` 才会写得出来（1.23 之后）。
- **标准库的 `init` 副作用**：`import _ "github.com/lib/pq"`、`import _ "net/http/pprof"`、`import _ "time/tzdata"`——这仨是 Go 里最著名的三个「空导入」。
- **`struct{}` 在真实框架里**：`sync.WaitGroup` 内部、`context.Context.Done()` 返回 `<-chan struct{}`（标准库最重要的一处「纯信号 channel」）。

---

## 3.6 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| 错误永远为 nil / 变量莫名其妙是旧值 | `:=` 在 `if`/`for` 块里创建了新变量，遮蔽外层 | 提声明用 `=`，或用 `if err := f(); err != nil` 形态 | 本章 3.3.1 |
| 遍历时修改元素不生效 | `range` 给的是元素副本 | `for i := range xs { xs[i].X = ... }` | 第 11 章 |
| goroutine 里全是最后一个值（老项目） | 模块 `go` 行 < 1.22，仍用共享循环变量 | 升 `go` 指令版本，或在循环体内 `v := v` | 第 8 章 |
| `switch` 后面每个 case 都要写 `break` 却报错 | Go 默认不穿透 | 删掉 `break`；要穿透写 `fallthrough` | 本章 3.3.3 |
| JSON 里字段消失了 | 字段名小写（未导出），或 tag 写错 | 首字母大写 + `go vet` 抓 `structtag` | 第 31 章 |
| `omitempty` 把 0/false 也吃掉了 | `omitempty` 语义就是「零值省略」 | 用 `*int`/`Optional[T]` 表达「未设置」 | 第 9 章 |
| 中文 `len()` 不等于字数 | `len(string)` 是字节数 | `utf8.RuneCountInString` / `[]rune(s)` | 第 13 章 |
| `string(65)` 得到 `"A"` 而不是 `"65"` | 无类型常量按 rune 转换 | `strconv.Itoa(65)` | 第 13 章 |
| 启动时连不上数据库却查不到原因 | `init()` 里做了 IO / 读取了配置 | `init` 只做纯注册；装配放 `main` 或 `New*()` | 第 36 章 |
| 多行字面量报 `syntax error: unexpected newline` | 自动分号插入：行尾缺逗号 | 最后一行元素后补 `,` | — |

---

## 3.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| `:=` | 至少一个新变量；跨作用域必定新建 → 影子变量 |
| `range` | 值拷贝；1.22+ 每轮新变量（受 `go` 行控制）；map 顺序随机；string 按 rune |
| `switch` | 默认不穿透，`fallthrough` 才穿透；无表达式 switch = if 链 |
| 循环 | 只有 `for`：`for c` = while，`for {}` = 死循环，`for i := range 10` 合法 |
| 可见性 | 首字母大小写；`internal/` 是包级别的第二层边界 |
| struct tag | 反引号原始字符串，写错静默失效；只对导出字段生效 |
| `struct{}` | 零大小：set 的 value、纯信号 channel、空占位 |
| `init` | 变量（按依赖序）→ `init()`（按文件名/声明序）→ `main`；别做 IO |
| 类型转换 | 无隐式转换，`int32 + int` 编译错误；`string(65)` 是 `"A"` |
| 未使用 | 变量、import 未使用 = 编译错误，用 `_` 显式丢弃 |

## 3.8 自检题

1. **这段代码为什么永远返回 nil 错误？给出三种修法。**
   ```go
   func f() error { var err error; if c { d, err := g(); _ = d }; return err }
   ```
   > 答案要点：`if` 块内的 `:=` 因为 `d` 是新变量、`err` 在该块作用域内不存在，于是**新建了 `err`**（遮蔽外层）。修法：① 用 `=` 并把 `d` 提前声明；② 用 `if d, err := g(); err != nil {...}` 让 `err` 活到 if 之内并立刻处理；③ 抽成独立函数直接 `return`。

2. **`for _, u := range users { u.Name = "x" }` 为什么不生效？如果 `users` 是 `[]*User` 呢？如果是 `map[string]User` 呢？**
   > 答案要点：值是副本 → 不生效；`[]*User` 拿到的是指针副本，`u.Name=` 通过指针改到了原对象 → 生效；map 的 value 也是副本 → 不生效，必须 `m[k] = u` 整体写回。

3. **为什么 Go 把「未使用的变量」定为编译错误，而 Java 只是警告？这对代码有什么实际影响？**
   > 答案要点：Go 把「可读性/可维护性」放在表达力之前，未使用变量几乎总是重构残留或逻辑错位；配合 `_` 空白标识符，强制开发者显式表达「我故意不用」；副作用是调试时得用 `_ = x` 或 `fmt.Println` 临时消音。

## 3.9 延伸阅读

- Go Spec：*Statements*（`for`/`switch`/`select`）、*Declarations and scope*、*Constant expressions` 三节，加起来不到 40 页
- Go 1.22 发布说明「For loop variable scoping」；Go 1.23 发布说明「Range over function types」
- `$GOROOT/src/reflect/type.go` 的 `StructTag.Get` —— tag 解析的真实实现
- `$GOROOT/src/encoding/json/encode.go` —— 为什么只有导出字段能序列化
- `go vet` 的 `structtag`、`loopclosure` 分析器源码：`$GOROOT/src/cmd/vendor/golang.org/x/tools/go/analysis/passes/`

---

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 2 章 工程骨架](2-toolchain-layout.md)（`gofmt`/`go vet` 怎么把本章的坑挡在 CI 之前）
- 下一章：[第 4 章 类型系统：struct、嵌入与方法集](4-type-system-embedding.md)
- 相关：`defer`/闭包捕获见[第 8 章](8-funcs-closures-defer.md)；slice 与 range 的底层见[第 11 章](11-slice-array.md)；类型断言与 type switch 见[第 5 章](5-interface-internals.md)
