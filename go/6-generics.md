# 6. 泛型：约束、type set 与实现原理

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · 语言内核 ｜ **编号**：6 / 54
> **本章定位**：Go 1.18 引入的泛型不是「Java 泛型的 Go 版」，也不是「C++ 模板的 Go 版」——它是第三种路线：**GC shape stenciling + dictionary**。这一章讲清约束与 type set 怎么写、编译器怎么实现、代价在哪，以及**什么时候你根本不该用泛型**（这条比前三条都重要）。
> **读法**：`6.3` 的实现原理值得细读（它决定了性能和调试体验）；`6.5` 与 `6.6` 是日常写代码要用的；`6.4` 的对照表能一次性校正你从 Java 带来的错误直觉。

---

## 6.1 引子：三份复制粘贴的代码，和一次失败的抽象

项目接了三个 LLM 供应商，信封结构几乎一样：

```go
type OpenAIResp struct {
	ID      string `json:"id"`
	Content string
	Err     error
}
type AnthropicResp struct { /* 字段名全不一样 */ }
type OllamaResp struct { /* 又不一样 */ }
```

你抄了三份 `if r.Err != nil { return r.Err }`、三份重试、三份日志。于是你想抽象，第一反应（Java 肌肉记忆）是「写个基类/接口」——在 Go 里就是：

```go
func Unwrap(r any) (string, error) { // 用 any 装，再断言回来
	resp, ok := r.(interface{ GetContent() string })
	...
}
```

结果你得到了：一堆 `any`、一堆类型断言、每次装箱一次堆分配、以及「参数类型错了要跑起来才知道」的运行时惊喜。

第二种选择是泛型：

```go
type Result[T any] struct {
	Value T
	Err   error
}
```

类型安全回来了，装箱消失了。但代价也来了：编译产物里多了一堆 `go.shape.*` 符号、反射/调试时的类型名变长、有些写法（泛型方法、类型 switch on T）直接不被允许。

**这就是本章要回答的问题：Go 的泛型是用什么换来的，你在什么场景该付这个价、什么场景不该。**

---

## 6.2 直觉模型：约束（constraint）就是「类型集合」

Java 的 `<T extends Comparable<T>>` 说的是「T 必须实现某个接口」；Go 的说法更本质：**约束描述了 T 可以取哪些类型——它就是一个类型集合（type set）**。

```
 type Number interface{ ~int | ~int64 | ~float64 }
                 │
                 ├── type set = {所有底层类型是 int 的类型}
                 │              ∪ {所有底层类型是 int64 的类型}
                 │              ∪ {所有底层类型是 float64 的类型}
                 │
                 └── ~T 的意思是「底层类型为 T」：包含 T 自己，也包含 type MyInt int

 合法实参：int, MyInt(底层 int), int64, OrderID(底层 int64), float64 ...
 非法实参：int32, string, *int
```

三条规则必须记住：

| 概念 | 写法 | 含义 | Java 对应 |
| --- | --- | --- | --- |
| 类型参数 | `func Max[T cmp.Ordered](a, b T) T` | `T` 是类型变量，`[T ...]` 用方括号（避免与 `<` 比较歧义） | `<T>` |
| 基本约束 | `[T any]` | 不限制（`any` = `interface{}`） | `<T>`（无界） |
| 接口约束 | `[T interface{ String() string }]` | T 必须实现该方法 | `<T extends X>` |
| 类型集约束 | `[T ~int \| ~float64]` | T 的底层类型必须是其中之一 | 无直接对应（Java 擦除后只能靠装箱） |
| `comparable` | `[T comparable]` | 支持 `==`/`!=` | `==` 的隐式要求 |
| 单类型约束 | `[T int]` | 只允许 `int` 一个类型 | `<T extends Integer>` 的形状 |

⚠️ 两个高频误解：

- **带 type set 的接口不能当普通类型用**。`var n Number` 是编译错误——「非基本接口」只能用于约束，不能作为变量/字段/参数类型（它可能包含 `~int | ~string`，没法确定内存布局）。
- **`~T` 里的 `T` 必须是底层类型**，不能是另一个约束，也不能是接口。

```go
package main

import (
	"cmp"
	"fmt"
)

// 自定义约束：底层类型是 int / int64 / float64 的都能用
type Number interface {
	~int | ~int64 | ~float64
}

type Score int // 底层 int

// 注意：写 a+b 需要类型集里所有类型都支持 +，所以 constraint 里不能混进 string 再对它做减法
func Sum[T Number](xs []T) T {
	var total T // T 的零值：不用 new(T)、不用断言
	for _, x := range xs {
		total += x
	}
	return total
}

// 用标准库约束：cmp.Ordered = ~int | ~int8 | ... | ~string
func MaxAll[T cmp.Ordered](xs []T) (T, bool) {
	if len(xs) == 0 {
		var zero T
		return zero, false
	}
	m := xs[0]
	for _, x := range xs[1:] {
		if x > m { // > 来自 Ordered 的类型集
			m = x
		}
	}
	return m, true
}

func main() {
	fmt.Println(Sum([]int{1, 2, 3}), Sum([]Score{1, 2}), Sum([]float64{1.5, 2.5}))
	fmt.Println(MaxAll([]string{"a", "c", "b"}))
}
```

---

## 6.3 底层实现：gcshape stenciling + dictionary

官方设计文档是 `golang/proposal` 里的 `design/generics-implementation-dictionaries-go1.18.md`（配套文档 `generics-implementation-gcshape.md`）。它的核心决定只有两条：

> **① 不为每个具体类型生成一份代码（不做单态化），而是为每个「GC shape」生成一份实例化。**
> **② 同一 shape 下不同具体类型的差异，全部塞进一个编译期算好的只读「字典」，作为隐藏参数传进去。**

### 6.3.1 什么是 gcshape

**gcshape（GC shape grouping）= 一组可以共享同一份实例化代码的类型。** 当前实现的定义非常保守：

> 两个具体类型属于同一 gcshape **当且仅当它们有相同的底层类型，或者它们都是指针类型**。

于是：

```
 func F[T any](x T) { ... }

 类型实参           gcshape 类型（编译器在 go.shape 这个内建包里造出来的）
 ────────────────────────────────────────────────────────────────
 int, MyInt         go.shape.int_0
 int64, OrderID     go.shape.int64_0
 string             go.shape.string_0
 *User, *Config     go.shape.*uint8_0      ← 所有指针共享一个 shape！
 float64            go.shape.float64_0
────────────────────────────────────────────────────────────────
 int / float64 都是 8 字节无指针，但底层类型不同 → 不同 shape（要保证 + - * / 语义正确）
 后缀的 _0 是「这是第几个类型参数」：同一函数里 T1、T2 都用指针，也必须是两个 shape
```

编译产物的符号表里能直接看到它们：`go tool nm ./bin | grep -c 'go.shape'`，或者 `go build -gcflags=-S` 看汇编里的 `F[go.shape.int_0]`。

### 6.3.2 字典里装什么

一个字典对应「一个调用点 + 一组具体类型」，命名是 `.dict.Max[float64]` 这种形式，**整块放在只读数据段，永不变更**：

```
 .dict.Max[int]      ← 编译期算好，只读
 ┌────────────────────────────────────────────────────────────┐
 │ T1 *_type        → int 的 runtime 类型描述                  │  ← 类型实参
 │ D1 *_type        → []int（函数体里派生出的类型，如 []T）      │  ← 派生类型
 │ S1 *dictionary   → 内部调用别的泛型函数的子字典               │  ← 子字典
 │ itab[T→I]        → 当 T 需要转成非空接口 I 时用的 itab        │  ← itab 项
 └────────────────────────────────────────────────────────────┘
        │
        │  .dict 作为**第一个参数**传入（在方法接收者之前）
        ▼
   F(go.shape.int_0) 的签名：F(dict, x)
```

「dictionary pass」这一趟编译器专门改写四类操作，让它们变成**读字典第 k 项**：

1. 调用约束里的方法（`T` 上的方法调用 → 先把接收者转成约束接口，再走 `itab.Fun[i]`）；
2. 把 `T` 转成非空接口（需要 `itab`）；
3. 从非空接口断言到具体类型（需要 `itab`）；
4. 对派生类型做 type switch / 断言（需要 `*runtime._type`）。

`x == nil`、`x.(SomeType)`、`switch x.(type)` 这类「依赖具体类型」的操作在泛型代码里要么被禁止，要么走字典——**这就是泛型代码有时比你手写的具体版本慢的原因**。

### 6.3.3 编译链路（四趟）

```
 源码（泛型声明 + 调用点）
   │
   ├─① types2 类型检查        cmd/compile/internal/types2
   │     完整校验类型参数、约束、方法集；把结果交给编译器
   ├─② noder 生成 IR          cmd/compile/internal/noder
   │     泛型函数体**不做**类型相关变换（因为还没有具体类型）；
   │     顺便算 gcshape：typecheck/subr.go 的 shapify()
   ├─③ scan pass（crawler）   cmd/compile/internal/noder/crawler.go
   │     扫到泛型引用 → 算 shape → 若该 shape 实例不存在则创建；
   │     把调用点改写成「shape 实例 + 传入字典」；记录字典格式（proto-dictionary）
   ├─④ dictionary pass
   │     按③记录的格式，把「调约束方法/转接口/断言」改写成读字典项
   ▼
 SSA → 机器码
```

几个由此推出的「反直觉事实」：

- **泛型函数的函数体是必须导出的**（哪怕是未导出的泛型函数，只要被内联引用也一样），因为跨包实例化时需要它。这也是「泛型让编译变慢」的一部分原因。
- **字典必须在编译期完全算出来**，所以**不支持「非单态化（non-monomorphisable）」的递归泛型**——比如用泛型类型不断自我嵌套、生成无限多新类型的程序，会直接编译失败（golang/go#48018）。
- **`.dict` 参数占一个参数位**，所以泛型函数的 ABI 和普通函数不同；泛型方法的价值就是「receiver 类型带参数」，而不是「方法自己带参数」。

### 6.3.4 为什么没有泛型方法

```go
type Box[T any] struct{ v T }

func (b *Box[T]) Get() T { return b.v }      // ✅ 可以：方法用 receiver 的类型参数
func (b *Box[T]) Map[U any](f func(T) U) *Box[U] { ... } // ❌ 编译错误：方法不能引入新的类型参数
```

原因是实现层的连锁反应：方法带自己的类型参数后，**接口满足关系就不再有穷举的可能**（同一个类型对同一个接口可能有无限多种实例化），`itab` 也不再有唯一解；同时 `itab` 是「接口 × 类型」的全局缓存（第 5 章），加上方法级类型参数后，这个二维表就要变成 N 维。

变通写法：把方法改写成函数。

```go
func Map[T, U any](b *Box[T], f func(T) U) *Box[U] { return &Box[U]{v: f(b.v)} }
```

另一个相关事实：**泛型类型别名**（`type A[T any] = B[T]`）在 Go 1.24 才正式放开（1.23 需要 `GOEXPERIMENT=aliastypeparams`）；在此之前只能给具体类型起别名（`type IntBox = Box[int]`）。

---

## 6.4 Java 对照：擦除 / 单态化 / gcshape+dict，三条路线

| 维度 | Java（擦除） | C++ / Rust（单态化） | Go（gcshape + dict） |
| --- | --- | --- | --- |
| 编译产物 | 一份（`List<T>` → `List`） | 每套类型一份（代码膨胀） | **每个 gcshape 一份** + 每组合一份字典 |
| 基本类型 | 必须装箱（`List<Integer>`） | 原生特化 | 原生（`int` 不装箱） |
| 运行期拿到真实类型 | **拿不到**（`T.class` 不合法，靠 `Class<T>` 传参） | 可（编译期单态） | 可（字典里有 `*runtime._type`） |
| 性能 | 装箱 + 指针追逐 | 最优 | 接近最优；`T` 上的运算/方法调用可能多一层字典间接 |
| 代码体积 | 最小 | 最大 | 居中 |
| 编译速度 | 快 | 慢 | 中（且泛型函数体必须导出） |
| 特有代价 | 无法 `new T()`、无法 `instanceof T`、数组泛型受限 | 模板错误信息爆炸 | 不支持泛型方法；非单态化程序不支持 |

**为什么 Go 不做擦除？** 三个硬理由：① 擦除后 `int` 必须装箱，直接违背 Go「不做暗箱分配」的性能模型（`[]int` 和 `[]any` 的性能差 5~10 倍）；② Go 的反射/`fmt` 依赖真实的 `*runtime._type`，擦除会让 `%v` 一类行为退化；③ Go 没有 `Class<T>` 这种「把类型当值传」的文化（那本质上是运行期反射传参），而字典正好在编译期就把这件事办完了。

**为什么 Go 不做完全单态化？** 编译时间与代码体积。一个被 20 种类型实例化的泛型工具函数，单态化就是 20 份机器码；Go 的价值观是「编译要快、二进制要可预测」——所以选了「粗粒度 shape + 细粒度字典」的折中。

> 💡 一个能立刻用上的判断：**如果某个泛型函数里全是「数据搬运」（赋值、切片遍历、传参），它和手写版本一样快；如果里面全是「对 T 做运算、调 T 的方法、把 T 塞进接口」，它可能比手写慢一点**（多一次字典读取/间接调用，且更难内联）。

---

## 6.5 框架与工程落地

### 6.5.1 标准库里已经在用的泛型

| API | 引入 | 用途 |
| --- | --- | --- |
| `sync/atomic.Pointer[T]` | 1.19 | 类型安全的原子指针（替代 `atomic.Value` 的断言） |
| `cmp.Ordered` / `cmp.Compare` | 1.21 | 排序/比较的标准约束 |
| `slices.Sort` / `slices.Contains` / `slices.Clone` | 1.21 | 替代 `sort.Slice` 的反射实现 |
| `maps.Keys` / `maps.Clone` | 1.21/1.23 | 后者返回 `iter.Seq`（1.23） |
| `min` / `max` / `clear` | 1.21 | 内建函数，泛型化的语法糖 |
| `sync.OnceValue[T]` / `OnceValues[T1,T2]` | 1.21 | 只执行一次并缓存结果 |
| `iter.Seq[T]` | 1.23 | `range-over-func` 的迭代器协议 |
| `weak.Pointer[T]` | 1.24 | 弱引用（配合缓存） |

### 6.5.2 Agent 项目里的三种正当用法

**① 结果信封（消除 `any` + 装箱）**

```go
package core

// Result[T] 是最小可用泛型：类型安全的「值或错误」，且支持链式组合
type Result[T any] struct {
	Value T
	Err   error
}

func OK[T any](v T) Result[T]   { return Result[T]{Value: v} }
func Fail[T any](err error) Result[T] { return Result[T]{Err: err} }

func (r Result[T]) IsOK() bool { return r.Err == nil }

// 组合子：注意必须是**函数**不是方法（方法不能引入新的类型参数）
func MapResult[T, U any](r Result[T], f func(T) U) Result[U] {
	if r.Err != nil {
		return Fail[U](r.Err)
	}
	return OK(f(r.Value))
}

func AndThen[T, U any](r Result[T], f func(T) Result[U]) Result[U] {
	if r.Err != nil {
		return Fail[U](r.Err)
	}
	return f(r.Value)
}
```

**② 工具/组件的输入输出类型（结构化工具调用的标准姿势）**

```go
package mcpish

import (
	"context"
	"encoding/json"
)

type Tool[In, Out any] struct {
	Name        string
	Description string
	Handler     func(ctx context.Context, in In) (Out, error)
}

// 统一入口：SDK 负责 JSON 解编 → 类型安全的 Handler → JSON 编码
// 官方 modelcontextprotocol/go-sdk 的 mcp.AddTool[In, Out] 就是这种形态（API 随版本演进，以你 go.mod 为准）
func (t *Tool[In, Out]) Invoke(ctx context.Context, raw json.RawMessage) (json.RawMessage, error) {
	var in In
	if err := json.Unmarshal(raw, &in); err != nil {
		return nil, err
	}
	out, err := t.Handler(ctx, in)
	if err != nil {
		return nil, err
	}
	return json.Marshal(out)
}
```

**③ 通用容器与链式 API**：`eino` 的编排层（`compose.Runnable[I, O]` 形态）、流式管道、`Set[T]`、LRU 缓存、`Batch[T, R]` 并发映射器。这类代码的共同特征：**里的逻辑是「搬运 + 组合」，不带类型特化运算**。

### 6.5.3 泛型 vs 接口：一张判决表

| 你的需求 | 用泛型 | 用接口 |
| --- | --- | --- |
| 容器/集合（`[]T`、`map[K]V`、栈、队列、池） | ✅ | ⚠️ 只能 `any` + 断言 |
| 避免装热路径里的装箱分配 | ✅ | ❌ |
| 需要按元素类型做运算（`<`、`+`、`==`） | ✅（约束 type set） | ❌ 接口没有运算 |
| 多态行为/可替换实现（DB、LLM provider） | ❌ | ✅（**接口是唯一答案**） |
| 需要运行期动态选择/注册表/插件 | ❌ | ✅ |
| 只有一个实现、或只是「让代码看起来更高级」 | ❌ | ❌（直接写具体类型） |

---

## 6.6 踩坑清单 + 什么时候**不该**用泛型

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| `cannot use type switch on x (variable of type T)` | 类型参数不是接口值 | `switch any(x).(type)`（会走字典，有成本） | 本章 6.3.2 |
| `invalid operation: x == nil` | `T` 可能不是指针/接口 | 用 `var zero T; x == zero`（要求 comparable）或加约束 | — |
| `invalid operation: x < y` | 约束里没声明支持 | 用 `cmp.Ordered` 或自定义 `~int \| ~string` | — |
| `Number does not implement ...` / 无法声明变量 | 带 type set 的接口只能当约束 | 换成具体类型或基本接口 | 6.2 |
| 方法里想引入新的类型参数 | Go 不支持泛型方法 | 改成顶层泛型函数 | 6.3.4 |
| 泛型代码基准比手写慢一截 | 字典间接 + 无法内联 + 运算走字典 | 热路径上为高频类型手写特化版本 | 6.4 |
| 编译变慢 / 二进制变大 | 每 shape 一份实例化 + 函数体强制导出 | 泛型用在真正复用的地方，别为「一次性代码」引入类型参数 | — |
| 报错信息看不懂（`cannot use x (variable of type T) as ...`） | 类型参数推导失败 | 显式给类型实参 `F[int](x)`；简化约束 | — |

**什么时候不该用泛型**（这是我更希望你记住的部分）：

1. **只有一个调用点**。泛型是为复用付费，一次性的代码直接写具体类型。
2. **核心需求是「多态行为」**。要用「不同实现做不同事」，那是接口的活。泛型是「同一份逻辑对不同类型做同一件事」。
3. **抽象层次比 `any` 还低**。比如「任意可 JSON 序列化的东西」——约束写不出来，最后还是要 `any`。
4. **团队还不熟 `~T`/type set**。一段读起来要多想 30 秒的泛型代码，往往不如两段直白的具体代码。
5. **你其实只想要「少写几个 `int64` 转换」**。给 ID 类型写泛型函数，收益远小于给 ID 用 `type OrderID int64`（第 4 章）。

---

## 6.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 声明 | `func F[T Constraint](...)`，**方括号**；类型参数属于函数/类型，不属于方法 |
| 约束 = type set | `interface{ ~int \| ~string }`；`~T` 表示「底层类型为 T」 |
| 非基本接口 | 带 type set 的接口只能当约束，不能当变量/字段类型 |
| gcshape | 相同底层类型，或都是指针 → 共享一份实例化；符号形如 `go.shape.int_0` |
| dictionary | 编译期算好的只读表：类型实参 + 派生类型 + 子字典 + itab；命名 `.dict.F[int]` |
| 传参 | `.dict` 是**第一个参数**（在 receiver 之前） |
| 改写点 | 调约束方法 / 转非空接口 / 断言 / type switch 都要读字典 |
| 不支持 | 泛型方法、非单态化的递归泛型；泛型类型别名 1.24 才有 |
| 与 Java 差异 | 不擦除 → 基本类型不装箱、反射能拿到真实类型 |
| 时机 | 容器/算法/避免装箱 → 泛型；多态/可替换实现 → 接口 |

## 6.8 自检题

1. **`F[*User]` 和 `F[*Config]` 会不会生成两份机器码？`F[int]` 和 `F[type MyInt int]` 呢？为什么？**
   > 答案要点：指针共享一个 gcshape（`go.shape.*uint8_0`），只有一份代码 + 两份字典；`int` 与 `MyInt` 底层类型相同 → 同一 gcshape（`go.shape.int_0`），也是一份代码两份字典。而 `F[int32]` 与 `F[int]` 底层类型不同 → 两份代码。

2. **为什么 Go 不允许 `func (b *Box[T]) Map[U any](f func(T) U) *Box[U]`？给出等价写法。**
   > 答案要点：方法引入新类型参数会破坏接口满足的可穷举性与 `itab` 的唯一性（第 5 章的「接口 × 类型」全局表会退化）。等价写法：顶层泛型函数 `func MapBox[T, U any](b *Box[T], f func(T) U) *Box[U]`；或把 `U` 提到类型上——但那会改变 `Box` 的语义。

3. **一个只有两个实现、且需要「不同实现做不同事」的组件（比如 LLM provider），你该用泛型还是接口？如果需求改成「给任意 provider 的返回值统一加一层重试/计量」，呢？**
   > 答案要点：前者用接口（多态、可替换、可 mock、运行期注册）；后者用泛型或「接口 + 装饰器」——注意装饰器模式在 Go 里更常见（`type retryingModel struct{ next Model }`），泛型在这里的收益主要是省掉 `any` 装箱与断言。

## 6.9 延伸阅读

- 设计文档：`golang/proposal` → `design/generics-implementation-dictionaries-go1.18.md`、`design/generics-implementation-gcshape.md`（本章 6.3 的一手来源）
- 语言规范：Go Spec *Type parameter declarations*、*Type sets*、*Type inference* 三节
- 官方博客：*Generics in Go*（系列）、*All your comparable types*、*Fixing the loop variable* 无关但同期值得看
- 源码：`$GOROOT/src/cmd/compile/internal/types2/`（约束与类型集）、`cmd/compile/internal/typecheck/subr.go`（`shapify`）、`cmd/compile/internal/noder/crawler.go`（scan pass）
- 观察手段：`go build -gcflags=-S`、`go tool nm ./bin | grep go.shape`、`go build -gcflags='-d=ssa/dump=off'`
- 相关章节：[第 5 章 接口与 itab](5-interface-internals.md)、[第 11 章 slice](11-slice-array.md)、[第 27 章 编译链路](27-compile-pipeline.md)、[第 41 章 工具系统](41-tool-system-function-calling.md)

---

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 5 章 接口的底层：eface / iface / itab](5-interface-internals.md)
- 下一章：[第 7 章 错误处理哲学：error、wrap、panic 边界](7-error-handling.md)
- 相关：方法集与接收者见[第 4 章](4-type-system-embedding.md)；反射与代码生成见[第 37 章](37-codegen-reflection.md)；工具系统的类型化 handler 见[第 41 章](41-tool-system-function-calling.md)
