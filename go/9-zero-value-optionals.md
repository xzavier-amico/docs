# 9. 零值哲学、指针 vs 值与「可选值」设计

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · 语言内核 ｜ **编号**：9 / 54
> **本章定位**：零值可用是 Go 最被低估的设计，也是 Java 脑袋最容易水土不服的一处。这一章讲清三件事：零值什么能用、nil 到底有几种含义、以及"字段没传"和"字段传了空值"怎么区分。
> **读法**：9.4 的五种 nil 和 9.6 的可选值，建议直接对照你的 API 层代码看一遍。

---

## 9.1 引子：一个 PATCH 请求改不动的字段

需求很简单：更新用户资料，允许只传要改的字段。

```go
type UpdateReq struct {
	Name  string `json:"name,omitempty"`
	Age   int    `json:"age,omitempty"`
	Bio   string `json:"bio,omitempty"`
}

func (h *Handler) Update(w http.ResponseWriter, r *http.Request) {
	var req UpdateReq
	json.NewDecoder(r.Body).Decode(&req)

	if req.Name != "" {   // ❌ 用户想清空名字（改成 ""），这里判断为"没传"
		user.Name = req.Name
	}
	if req.Age != 0 {     // ❌ 用户想改成 0 岁？基本不会，但"改成 0"就是做不到
		user.Age = req.Age
	}
	// ...
}
```

三个 bug 一次凑齐：

1. **"没传"和"传了空值"不可区分**。`string` 的零值就是 `""`，无从判断这是"用户清空"还是"字段缺席"。
2. **`omitempty` 在序列化侧又撒了一次谎**：`Age: 0` 会被直接省掉，客户端看到的 JSON 里根本没有 `age` 字段，于是它无法区分"年龄是 0"和"没有年龄概念"。
3. 如果这个 handler 里还有个 `var attrs map[string]string`，一句 `attrs["x"] = "1"` 就是**直接 panic**——nil map 可读不可写。

前两个是"可选值"的设计问题，第三个是"零值"的语义问题。它们同源：**Go 没有 `null` 这个东西（只有 nil），而 nil 在不同类型上含义完全不同**。

> 🩸 事后复盘时有人说"要不我们把字段都改成指针吧"。结果 `Age *int` 一上路，`*req.Age` 解引用忘了判空，一周后线上又来一次 nil panic。指针不是免费的，这一章就是要给你一张"什么时候用什么"的决策表。

---

## 9.2 直觉模型：零值 = "能用的空"，null = "什么都没有"

Java 的字段默认值：`int` → 0，`boolean` → false，**对象 → null**。null 意味着"这个引用不指向任何东西"，任何解引用都是 NPE。

Go 的零值不是一个"缺失标记"，而是一个**确定的、合法的、设计上应该能用的值**：

| Java | Go | 会咬人的地方 |
| --- | --- | --- |
| `int` 默认 0 | `int` 零值 0 | 一样 |
| `String` 默认 `null`（字段） | `string` 零值 `""` | **Go 的 string 永远不会是 null** |
| `boolean` 默认 false | `bool` 零值 false | 一样 |
| `List<T>` 默认 null | `[]T` 零值 nil，但 `len==0`、可 range、可 append | nil slice 能用！ |
| `Map<K,V>` 默认 null，`put` 才 NPE | `map[K]V` 零值 nil，**读可以、写 panic** | 最经典的坑 |
| 对象默认 null，方法调用 NPE | `struct` 零值 = 所有字段零值，可以直接用 | 没有"未初始化对象"概念 |
| — | `func` 零值 nil，调用 panic | 但可以当"未设置"用 |
| — | `chan` 零值 nil，收发永久阻塞 | select 里的"禁用分支"技巧 |
| — | `interface` 零值 nil，但"装了 nil 指针"就不等于 nil | 第 5 章的老朋友 |

一句话记住：**Java 的对象默认是"不存在的对象"，Go 的值默认是"内容为零的对象"**。这条差异下游造出了三件事：零值可用性设计、五种 nil、以及"可选值必须显式表达"。

---

## 9.3 零值可用性：Go 库设计的第一原则

标准库里有大量类型**声明完就能用**，一个构造函数都不用：

```go
var mu sync.Mutex
mu.Lock()                // ✅ 零值可锁

var buf bytes.Buffer
buf.WriteString("hi")    // ✅ 零值可写，内部按需分配

var m sync.Map
m.Store("k", 1)          // ✅

var once sync.Once
once.Do(f)

var wg sync.WaitGroup
wg.Add(1)

var c http.Client
c.Get("https://example.com") // ✅ 用 DefaultTransport（但没有超时，别学）

var g errgroup.Group
g.Go(f)                  // ✅

var n atomic.Int64       // Go 1.19+
n.Add(1)
```

反过来，**有些类型零值是不可用的**，你必须用构造函数。这是有意的取舍——不可用的类型必须让"忘记初始化"尽早暴露成编译错误或清晰的 panic，而不是静默产生错误行为。

| 类型 | 零值可用？ | 说明 |
| --- | --- | --- |
| `sync.Mutex` / `sync.RWMutex` / `sync.WaitGroup` / `sync.Once` / `sync.Map` / `sync.Pool` | ✅ | 全部零值可用，这是刻意设计 |
| `bytes.Buffer` / `strings.Builder` | ✅ | 但**拷贝后使用**会出问题（`strings.Builder` 会 panic 举报自己） |
| `http.Client` / `http.Server` | ✅（有坑） | `Client` 零值没有超时；`Server.Handler` 为 nil 时用 DefaultServeMux |
| `atomic.Int64` / `atomic.Pointer[T]` | ✅ | 1.19 起的泛型原子类型 |
| `errgroup.Group` | ✅ | 零值即用 |
| `bufio.Scanner` / `bufio.Reader` / `bufio.Writer` | ❌ | 必须 `NewScanner(r)`，零值会 nil 解引用 |
| `time.Timer` / `time.Ticker` | ❌ | 零值的 `C` 是 nil，`<-t.C` 永久阻塞；用 `NewTimer` |
| `database/sql.DB` / `sql.Rows` | ❌ | 必须 `sql.Open` + `Ping` |
| `json.Encoder` / `json.Decoder` | ❌ | 需要 `io.Writer` / `io.Reader` |
| `regexp.Regexp` | ❌ | 用 `regexp.Compile` / `MustCompile` |
| `context.CancelFunc` 之外的任何 `context` | — | 只用 `Background/TODO/WithXxx` 派生 |

**怎么自己写出零值可用的类型？** 三条规则：

1. 内部状态用"零值即合法"的表示：`nil` slice 而不是 `[]T{}`，懒初始化而不是构造函数里初始化。
2. 惰性分配放在方法里：`bytes.Buffer` 就是 `if b.buf == nil { b.buf = make([]byte, 0, smallBufferSize) }`。
3. 一旦你在 `NewXxx` 里做"必须执行"的初始化，就说明这个类型零值不可用——那就**别提供零值路径**，让构造函数成为唯一入口，并考虑把类型字段全部私有。

> ⚠️ 拷贝陷阱：含 `sync.Mutex`、`sync.WaitGroup` 等字段的 struct **不能按值传递**。`go vet` 的 `copylocks` 检查器会抓这个；`sync` 里的类型内部都嵌了 `noCopy` 结构体来触发它。要用指针，或者把锁放在堆上（`&T{}`）。

---

## 9.4 五种 nil：一张必须背下来的表

`nil` 在 Go 里是个**无类型字面量**，它可以赋值给指针、slice、map、chan、func、interface，但**每种类型的 nil 行为都不一样**。

```
        nil
         │
   ┌─────┼──────┬────────┬────────┬────────┐
   ▼     ▼      ▼        ▼        ▼        ▼
 *T     []T    map     chan     func    interface
 │      │      │        │        │        │
解引用   len=0  读=零值   收发       调用     ==nil 仅当
→panic  可range 写=panic 永久阻塞  →panic   类型字+数据字
        可append delete可   close    但可当   都为 nil（第5章）
        可len   可range   =panic   "未设置"
```

| 类型 | 零值下 `len` | 零值下读 | 零值下写 | 零值下 `range` | 有用性 |
| --- | --- | --- | --- | --- | --- |
| `[]T` | 0 | 索引 panic | `append` ✅（自动分配），`s[i]=` ❌ | ✅ 零次迭代 | 非常好用，**nil slice 就是空集合** |
| `map[K]V` | 0 | ✅ 返回零值（不 panic） | ❌ **panic: assignment to entry in nil map** | ✅ 零次迭代 | 半好用，**写必须先 make** |
| `chan T` | 0 | 接收永久阻塞 | 发送永久阻塞 | ✅ 永久阻塞直到关闭（不会退出） | **故意设计**，见第 21 章的 select 技巧 |
| `func()` | — | — | 调用 → panic | — | 可当"可选回调"用：`if f != nil { f() }` |
| `*T` | — | 解引用 panic | 解引用 panic | — | 可当"可选值"用，但要判空 |
| `interface{}` | — | 方法调用 panic | — | — | **装了 nil 指针也不等于 nil**，见第 5 章 |

```go
package main

import "fmt"

func main() {
	var s []int        // nil slice
	fmt.Println(len(s), s == nil)  // 0 true
	s = append(s, 1)               // ✅ nil slice 可以直接 append
	fmt.Println(s)                 // [1]

	var m map[string]int           // nil map
	fmt.Println(m["nope"], len(m)) // 0 0  ← 读是安全的
	// m["x"] = 1                  // ❌ panic: assignment to entry in nil map

	var ch chan int                // nil channel
	select {
	case v := <-ch:                // 这个分支永远不会被选中
		fmt.Println(v)
	default:
		fmt.Println("nil chan 在 select 里等于禁用该分支")
	}

	var fn func()
	fmt.Println(fn == nil)         // true
	// fn()                        // ❌ panic: invalid memory address or nil pointer dereference
	if fn != nil {
		fn()
	}
}
```

这段代码可以直接 `go run`，是理解"五种 nil"最快的路。**注意 `m["nope"]` 不 panic 而 `m["x"]=1` panic**——这是 map 读路径里有 `if h == nil { return zero }` 的保护，写路径里没有（写要分配 bucket）。

---

## 9.5 指针 vs 值：四种判断，不要凭感觉

Go 的赋值就是拷贝。所以"该用 `*T` 还是 `T`"本质在回答："我要拷贝它吗？"

| 场景 | 选择 | 理由 |
| --- | --- | --- |
| 方法需要修改接收者 | `*T` | 值接收者改的是副本 |
| 类型里有 `sync.Mutex`/`WaitGroup`/`Buffer` | `*T`（必须） | 拷贝锁 = 拷贝了锁的状态，`go vet` 会报 |
| struct 很大（经验值 > 3~4 个机器字 / > 64B） | `*T` | 每次调用一次 `memmove`，热路径上很贵 |
| 需要表达"可能不存在"（可空语义） | `*T` | `T` 没有"不存在"状态 |
| 需要满足接口，而方法是指针接收者 | `*T` | `T` 的方法集不含 `*T` 的方法（第 4 章） |
| 小结构体 + 只读 + 无指针字段 | `T` | 避免逃逸、避免 GC 扫描、缓存友好 |
| map/slice 元素 | 通常 `T` | `map[string]*User` 会让 GC 多扫一层，且元素不是"对象引用"而是值 |
| 长生命周期、被多处共享 | `*T` | 但要想清楚并发安全 |

几条容易误解的事实：

1. **`*T` 不一定逃逸到堆**。逃逸分析（第 14 章）会判断指针是否活着离开函数；`func sum(p *Point)` 里 `&Point{}` 通常仍在栈上。
2. **返回 `*T` 也不一定昂贵**。真正贵的是"分配 + GC 压力"，不是"多一次解引用"。用 `go build -gcflags='-m'` 看。
3. **值接收者 + 接口 = 装箱拷贝**。`var i fmt.Stringer = myBigStruct{}` 会把整个 struct 复制到接口的数据字里（若超过一个字则分配，见第 5 章）。
4. **一致性比最优更重要**：同一个类型的方法集不要一半值接收者一半指针接收者——那会让"哪些方法能在值上调用"变成一场猜谜。

```go
type Counter struct{ n int64 }

func (c Counter) IncWrong() { c.n++ }   // ❌ 改的是副本
func (c *Counter) Inc()     { c.n++ }   // ✅

// Java: 类的所有方法天然都是"指针接收者"（引用调用），Go 要你显式选
```

---

## 9.6 可选值：三种武器和一个谎言

回到 9.1 的 PATCH 需求。Go 没有 `Optional<T>`，你有三条路。

### 武器一：`*T`（最常用，也最容易翻车）

```go
type UpdateReq struct {
	Name *string `json:"name"`
	Age  *int    `json:"age"`
}
// nil  = 字段没传（不修改）
// 非 nil = 字段传了，哪怕是 ""
```

代价写在每个调用点：`if req.Name != nil { user.Name = *req.Name }`。**忘了判空就是 nil panic**。

### 武器二：`Valid` 布尔（数据库界的标准答案）

```go
// database/sql 的经典形态
type NullString struct {
	String string
	Valid  bool // Valid 为 false 时 String 无意义
}

// Go 1.22 起标准库直接给了泛型版本：
var n sql.Null[int64]  // 字段 V T + Valid bool
```

好处：**没有指针、不逃逸、零值就是"不存在"**。坏处：每个字段多一个 bool，JSON 里会难看（除非实现 `MarshalJSON`）。

### 武器三：`Option[T]`（自己写，语义最清楚）

```go
type Option[T any] struct {
	v  T
	ok bool
}

func Some[T any](v T) Option[T] { return Option[T]{v: v, ok: true} }
func None[T any]() Option[T]    { return Option[T]{} }

func (o Option[T]) Get() (T, bool) { return o.v, o.ok }
func (o Option[T]) Or(def T) T {
	if o.ok {
		return o.v
	}
	return def
}

// 用法：三态清晰 —— 没传 / 传了零值 / 传了非零值
type UpdateReq struct {
	Name Option[string]
	Age  Option[int]
}
```

`Option[T]` 是最"Java 味"的写法（对应 `java.util.Optional`），但注意两点：它**不能直接 JSON 编解码**（需要自己实现 `UnmarshalJSON`，否则 `Some("")` 和 `None` 分不开），而且 Go 社区普遍认为**业务代码里引入 `Option` 是过度设计**——因为 `*T` 就够了。它真正有价值的场景是**库的公共 API**：显式表达"这个参数是三态的"。

### 那个谎言：`omitempty`

`encoding/json` 的 `omitempty` 不是"值为空则省略"，而是"**零值则省略**"——它连 `0`、`false`、`""` 一起省略，而且对 slice/map/string 用的是 `len(v) == 0`：

```go
package main

import (
	"encoding/json"
	"fmt"
)

type Config struct {
	Temperature float64  `json:"temperature,omitempty"`
	MaxTokens   int      `json:"max_tokens,omitempty"`
	Tags        []string `json:"tags,omitempty"`
	Note        string   `json:"note,omitempty"`
}

func main() {
	b, _ := json.Marshal(Config{Temperature: 0, MaxTokens: 0, Tags: []string{}, Note: ""})
	fmt.Println(string(b)) // {}
	// 灾难：用户明确要求 temperature=0（确定性输出），结果字段被整个吞掉，
	// 上送后服务端按默认值 0.7 处理 —— 这就是 LLM 客户端里最经典的隐性 bug。
}
```

Go 1.24 新增了 **`omitzero`**，语义是"值是**零值**才省略"，并且优先调用类型的 `IsZero() bool` 方法：

```go
type Config2 struct {
	Temperature *float64 `json:"temperature,omitzero"` // nil 省略，0.0 保留 ✅
	Tags        []string `json:"tags,omitzero"`        // nil 省略，[]（空但非 nil）保留 ✅
}
```

| tag | `0` / `""` / `false` | 空 slice `[]T{}` | nil slice | 说明 |
| --- | --- | --- | --- | --- |
| `omitempty` | 省略 | 省略（`len==0`） | 省略 | 老语义，**会吞掉合法零值** |
| `omitzero`（1.24+） | 省略（就是零值） | **保留** | 省略 | 能区分"空集合"和"没有集合" |
| 不加 tag | 输出 | 输出 `[]` | 输出 `null` | nil slice 序列化成 `null` 是另一个坑 |

> 💡 `encoding/json/v2`（Go 1.25，需 `GOEXPERIMENT=jsonv2`）重新设计了这套选项（`omitzero`、`omitempty` 语义分开，还引入 `format`/`inline` 等），但在它 GA 之前，**线上还是用 1.24 的 `omitzero` + 指针**。详见第 31 章。

### 还有一种：延迟解析（`json.RawMessage`）

当"字段是否存在"必须 100% 确定（比如 webhook 签名验证、工具调用参数校验），最稳的做法是两段解码：

```go
var raw map[string]json.RawMessage
if err := json.Unmarshal(body, &raw); err != nil {
	return err
}
if v, ok := raw["name"]; ok {  // ok 才是"客户端真的传了这个 key"
	_ = json.Unmarshal(v, &req.Name)
}
```

---

## 9.7 Java 对照：null 文化的五个错位

| 你的 Java 直觉 | Go 的真相 | 为什么 |
| --- | --- | --- |
| `Optional<User>` 表示"可能没有" | 只在返回"可能不存在的单个值"时偶尔用 `*T`；集合场景用 nil slice 更自然 | Go 的零值可用性大幅降低了 `Optional` 的必要性 |
| 对象字段天然可为 null，判空靠 `@Nullable` | `struct` **不存在 null 状态**，只有指针/接口/引用类型能 nil | 值语义：struct 字段一定有值 |
| `Collections.emptyList()` vs null 要区分 | `nil` slice 就是合法空集合，`len`/`range`/`append` 都正常 | 所以"返回 nil slice 还是空 slice"通常只在 JSON 上有差异 |
| `Map.put` 前不用判空 | **nil map 写直接 panic** | 必须 `make`；或用 `map[string]struct{}` 建个空 map |
| "null 是十亿美元的错误" → 我们也该消灭 nil | Go 把 nil 保留成**显式的、有类型的**状态，而不是隐藏的默认值 | nil 的语义是"这个后门没接东西"，你得显式处理 |

**Go 为什么这样选？** 因为 Go 有值语义和零值可用性：既然 `struct` 可以是"内容全零但合法"，就不需要 null 来表达"未初始化"；而"确实不存在"这种语义，交给指针/接口/`Valid` 显式表达。**代价是：可选值的表达成本被摊到了调用方**——这就是 9.6 三种武器的由来。

一个在 Agent 项目里反复出现的具体例子：`openai-go`（官方 SDK）用 `param.Field[T]` 区分"未设置"和"设置为零值"——`Temperature: openai.Float(0)` 会被发出去，而未赋值的 `param.Field` 会被省略。这就是武器三在真实 SDK 里的落地形态。

---

## 9.8 框架与工程落地

1. **protobuf / gRPC**：`proto3 optional` 生成的 Go 字段是 `*T`，正是为了区分"未设置"。Java 侧同样用 `hasXxx()`——两边语义一致，这是个难得的对齐点。
2. **工具（Tool）参数**：LLM 给的 JSON 参数天然是"可能缺字段"的，**千万别直接解到 struct 里当完整数据用**。用 `json.RawMessage` + schema 校验（`invopop/jsonschema`）+ 显式默认值填充，详见第 37、41 章。
3. **配置加载**：`config.Port == 0` 到底是"没配"还是"配了 0"？答案是——**用 `*int` 或在加载后立刻校验+填默认值**，不要让零值语义穿透到业务层。
4. **`sync.Mutex` 放进 struct 字段时用指针接收者**，跑 `go vet ./...` 让 `copylocks` 兜底；CI 里加一道 `staticcheck`（`SA5001` 一类）。

---

## 9.9 踩坑清单（CR 可直接抄）

| # | 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- | --- |
| 1 | `assignment to entry in nil map` | map 零值是 nil，写要分配 | `m := make(map[K]V)` 或 `m == nil` 时先 make | 9.4 |
| 2 | 客户端抱怨"我明明设成 0，服务器按默认处理" | `omitempty` 吞掉了零值 | 改 `*T` + `omitzero`，或用 `param.Field` 式设计 | 9.6 |
| 3 | `omitempty` 后空数组变成缺失 | `len(v)==0` 也被当作空 | `omitzero`（1.24+）或自定义 `MarshalJSON` | 9.6 |
| 4 | nil slice 序列化成 `null`，前端崩 | `json.Marshal(nil slice)` → `null` | 初始化成 `[]T{}`，或前端容错，API 文档写清 | 第 31 章 |
| 5 | `*req.Field` 解引用 panic | `*T` 可选值忘了判空 | 统一 helper：`if v := req.Name; v != nil` | 9.6 |
| 6 | 加了 Mutex 的 struct 按值传参后锁失效 | 拷贝了锁 | 用指针接收者 + `go vet` 的 `copylocks` | 9.3 |
| 7 | `service` 结构体零值 panic | 内部依赖未初始化 | 要么零值可用，要么强制走 `NewService()` 且字段私有 | 9.3 |
| 8 | `if err != nil` 明明 err 是 nil 却成立 | 接口里装了 typed nil | 出错时 `return nil, err`，别 `return (*T)(nil), err` | 第 5 章 |

---

## 9.10 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 零值哲学 | 零值是"能用的空"，不是"缺失"；库设计要让 `var t T` 直接可用 |
| 零值不可用 | `bufio.Scanner`/`time.Timer`/`sql.DB`/`json.Encoder` 必须构造函数 |
| nil slice | 完全可用：len 0、可 range、可 append |
| nil map | 读安全、写 panic；`range` 零次迭代 |
| nil chan | 收发永久阻塞；正好用来在 select 里"禁用分支" |
| nil func | 调用 panic；可用作可选回调 |
| nil 接口 | 只有类型字和数据字都为 nil 才 `== nil`（第 5 章） |
| 指针 vs 值 | 要改状态 / 有锁 / 大结构 / 需可空 → `*T`；小只读值 → `T` |
| 可选值 | `*T`（常态）/ `Null[T]`（数据库味）/ `Option[T]`（公共 API） |
| `omitempty` | 撒谎：吞掉合法零值；1.24 起用 `omitzero` |

---

## 9.11 自检题

1. **`var m map[string]int` 之后 `m["a"]` 和 `m["a"] = 1` 分别会发生什么？为什么设计成这样？**
   > 要点：读返回零值 0（读路径有 `h == nil` 保护，零值语义一致），写 panic（写要么分配 bucket，要么命中间接层，无法"静默成功"）。设计取向是"读零值友好、写必须显式初始化"，这样 `var m map[string]int` 可以安全地当"空集合"用，但你要往里放东西时必须 `make`——比"隐式创建"更不容易掩盖忘初始化。

2. **`json:"name,omitempty"` 的 `Name string` 字段，客户端传 `{"name": ""}` 后服务端怎么知道是"清空"还是"没传"？给出两种方案及代价。**
   > 要点：① 改成 `*string`（或 `Option[string]`）：能区分，代价是每个使用点判空 + JSON 里 nil 会输出 null（要配 `omitzero` 或 `omitempty`）；② 两段解码 `map[string]json.RawMessage`：最精确，代价是多一次解码和手写赋值。若在 1.24+ 且只是"零值不该被省略"，`omitzero` 只解决序列化侧，解决不了"没传"的判定。

3. **为什么 `var buf bytes.Buffer` 能用，而 `var s bufio.Scanner` 不能用？这对你写库有什么启发？**
   > 要点：`bytes.Buffer` 内部用 nil slice 懒分配，所以零值即合法状态；`bufio.Scanner` 需要 `io.Reader` 才能工作，零值缺少"必需依赖"，无法在方法里凭空造出。启发：零值可用的前提是"所有必需依赖都能在首次使用时推导或懒创建"；做不到就砍掉零值路径，强制 `NewXxx` 并把字段全私有，让误用变成编译错误。

---

## 9.12 延伸阅读

- `$GOROOT/src/sync/mutex.go`、`$GOROOT/src/bytes/buffer.go`、`$GOROOT/src/strings/builder.go`（零值可用与 `noCopy`/copyCheck）
- `$GOROOT/src/database/sql/sql.go`：`Null[T]`（Go 1.22 泛型版）
- `$GOROOT/src/encoding/json/encode.go`：`isEmptyValue`（`omitempty` 的真实实现）、Go 1.24 的 `omitzero`/`IsZero`
- Go 官方 Wiki：*CodeReviewComments* 的 "Receiver Type"、"Zero value" 两节
- `go vet` 的 `copylocks`；`staticcheck` 的 `SA5xxx` 系列
- `openai/openai-go` 的 `param.Field[T]`（可选参数的工程化解法）
- 下一章：[10. 组合优于继承：插件化与注册表设计](10-composition-plugin-registry.md)

---

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：第 8 章 [函数、闭包与 defer/panic 实现](8-funcs-closures-defer.md)
- 相关：第 4 章（值/指针接收者与方法集）、第 5 章（nil 接口的真相）、第 12 章（nil map 的底层原因）、第 14 章（逃逸分析）、第 31 章（JSON tag 全解）
- 下一章：[10. 组合优于继承：插件化与注册表设计](10-composition-plugin-registry.md)
