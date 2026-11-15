# 4. 类型系统：struct、嵌入与方法集

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · 语言内核 ｜ **编号**：4 / 54
> **本章定位**：Go 的类型系统只有两个零件：**内存布局（struct）**和**方法集（method set）**。这一章把这两个零件和「值语义」「指针/值接收者」「嵌入」三件事一次性焊死。**方法集是接口满足的唯一判据**，所以本章是第 5 章（接口底层）的地基。
> **读法**：`4.2` 的三张图必须记住（方法集格、嵌入层级、接收者调整）；`4.3` 讲源码，可以第二轮再细读；`4.6` 直接进 CR 模板。

---

## 4.1 引子：一个改了三天都没生效的 `SetName`

PR 里躺着这么一段代码，作者说「我在 `SetName` 里明明赋值了，为什么数据库里还是旧名字？」：

```go
package main

import "fmt"

type Agent struct {
	Name string
	role string // 小写：包外不可见，encoding/json 也看不见（第 3 章）
}

func (a Agent) SetName(n string) { a.Name = n } // 值接收者：拿到的是副本

func (a *Agent) SetNamePtr(n string) { a.Name = n } // 指针接收者：改的是原对象

func main() {
	a := Agent{Name: "old"}
	a.SetName("new")
	fmt.Println(a.Name) // "old" —— 改了三天的是个副本

	a.SetNamePtr("new") // 等价于 (&a).SetNamePtr("new")
	fmt.Println(a.Name) // "new"
}
```

Java 老手看到这里会本能地反驳：「方法里改 `this` 怎么可能不生效？」——在 Java 里，`this` 永远是**对象的引用副本**，所以你改的是同一个对象；在 Go 里，**值接收者拿到的是整个结构体的字节拷贝**，`a` 是另一个 `Agent`。

这类 bug 之所以危险，是因为它**编译通过、行为「看起来合理」、只是数据不对**。而它的根源不是语法，是 Go 类型系统的一条核心设计：

> **Go 的值有「身份」，但没有「引用身份」。** 一个 `struct` 值就是那几块字节，赋值即拷贝；所谓「对象」在 Go 里根本没有对应的概念。

---

## 4.2 直觉模型：三张图

### 图一：接收者调整（编译器替你加 `&` 或 `*`）

```
写法                     编译器的处理                              是否合法
────────────────────────────────────────────────────────────────────────────
v.M()   M 是值接收者     直接调用（v 被拷贝一份传给 M）             ✅ 总是合法
p.M()   M 是值接收者     (*p).M()：先解引用再拷贝                   ✅ 总是合法
v.M()   M 是指针接收者    (&v).M()：对 v 取地址                       ⚠️ 仅当 v 可寻址
p.M()   M 是指针接收者   直接调用                                    ✅ 总是合法
```

「可寻址（addressable）」是这里唯一的坑：**变量、指针解引用、slice 索引、可寻址 struct 的字段、复合字面量**可寻址；**函数返回值、map 元素、常量**不可寻址。所以：

```go
var m map[string]Agent
m["x"].SetName("new")   // ❌ 假设 SetName 是指针接收者 → cannot call pointer method SetName on Agent
Agent{}.SetName("new")  // ✅ 复合字面量是「可寻址」的例外（改的是临时值，没意义）
newAgent().SetName("new") // ❌ 返回值不可寻址
m["x"] = Agent{...}     // ✅ 正解：整块取出、改完、整块写回
```

### 图二：方法集格（决定接口满足）

```
        type T struct{}
        func (t T)  ValueM()  {}      ← 值接收者
        func (t *T) PtrM()    {}      ← 指针接收者

    ┌──────────────────────────────┐        ┌──────────────────────────────┐
    │  T 的方法集                   │        │  *T 的方法集                  │
    │  { ValueM }                  │  ⊂     │  { ValueM, PtrM }            │
    └──────────────────────────────┘        └──────────────────────────────┘

  ⇒ 只有 *T 能赋值给含 PtrM 的接口；T 不能。
    var _ interface{ PtrM() } = T{}    // ❌ 编译错误
    var _ interface{ PtrM() } = &T{}   // ✅
```

一句话口诀：**「值接收者的方法，值能调、指针也能调；指针接收者的方法，只有指针（可寻址的值）能调；接口只看方法集，所以指针接收者的方法只属于 `*T`。」**

### 图三：嵌入的层级与遮蔽

```
type Base struct{}            func (b *Base)   ID() int
                              func (b *Base)   Name() string
type Named struct{ Base }     ← 嵌入 Base（值嵌入）

   Named 的「扁平化视图」：
     ┌───────────────────────────────────────────────┐
     │ 字段：Base（深度 0）→ Base.id（深度 1）         │
     │ 方法：ID()     ← 提升自 *Base                   │
     │       Name()   ← 提升自 *Base                   │
     └───────────────────────────────────────────────┘

  深度规则：外层同名字段/方法 → 遮蔽（shadow）内层
            同深度两个同名 → 编译错误 ambiguous selector
```

---

## 4.3 底层实现：类型信息里到底存了什么

### 4.3.1 struct 的内存布局：没有对象头，只有字段

Go 的 struct 在内存里**就是字段按声明顺序依次排列**，中间按需补 padding 满足对齐。没有 mark word、没有 klass 指针、没有 monitor——这也解释了为什么小对象在 Go 里极其便宜（第 14、15 章）。

```go
package main

import (
	"fmt"
	"unsafe"
)

type Bad struct { // 声明顺序不佳
	A int32  // offset 0
	B int64  // offset 8（padding 4 字节）
	C int32  // offset 16
} // size 24，实际有效数据 16

type Good struct {
	B int64  // offset 0
	A int32  // offset 8
	C int32  // offset 12
} // size 16

func main() {
	fmt.Println(unsafe.Sizeof(Bad{}), unsafe.Sizeof(Good{})) // 24 16
	fmt.Println(unsafe.Offsetof(Bad{}.B), unsafe.Offsetof(Bad{}.C)) // 8 16
}
```

**拷贝成本 = `unsafe.Sizeof(T)`**。这就是「值接收者 vs 指针接收者」的性能侧答案：结构体 ≤ 3~4 个机器字，值接收者更快（无间接寻址、利于内联和寄存器分配）；更大就该用指针。但别只算 CPU：指针会让被指向的对象更容易逃逸到堆（第 14 章），在大结构体「只读」场景下，值传递反而可能更划算——**先 benchmark，别猜**。

### 4.3.2 类型的运行时元数据：`_type` / `structtype` / `uncommontype`

每个类型在二进制里都有一份只读的类型描述（`$GOROOT/src/runtime/type.go`），这是反射、接口断言、GC 的共同数据源（Go 1.21+ 起 `runtime._type` 已经是 `internal/abi.Type` 的别名，字段名首字母大写为 `Size_`/`PtrBytes`/`GCData`/`Equal`…，语义不变；以你本地 `$GOROOT/src/internal/abi/type.go` 为准）：

```go
// runtime/type.go（删减）
type _type struct {
	size       uintptr // 大小：GC 与内存分配用
	ptrdata    uintptr // 前 ptrdata 字节里含指针
	hash       uint32  // 类型哈希：接口断言的快速预判
	tflag      tflag
	align      uint8
	fieldAlign uint8
	kind       uint8
	equal      func(unsafe.Pointer, unsafe.Pointer) bool // == 的实现（不可比较类型为 nil）
	gcdata     *byte   // GC 位图
	str        nameOff
	ptrToThis  typeOff
}

type structtype struct { // struct 专有部分
	_r      _type
	pkgPath name
	fields  []structfield // 每个字段：name + 类型 + offset
}

type uncommontype struct { // 「不通用」部分：只有命名类型才有
	pkgPath nameOff
	mcount  uint16 // 方法总数
	xcount  uint16 // 导出方法数
	moff    uint32 // 指向 [mcount]method 的偏移
	_       uint32
}

type method struct {
	name nameOff
	mtyp typeOff
	ifn  textOff // 接口调用入口：itab.Fun 里存的就是这个
	tfn  textOff // 直接调用入口（静态派发用这个）
}
```

两个关键点：

1. **`method.ifn` 和 `method.tfn` 是两个入口**。`tfn` 用于 `x.M()` 这种静态可知的调用（可内联）；`ifn` 用于接口动态派发（第 5 章的 `itab.Fun` 指向它）。编译器为「指针接收者的方法被值类型接口调用」这类情况生成**包装函数**（历史上叫 `-fm` 后缀），把接收者调整一次再跳转。
2. **嵌入方法的提升是编译期生成的包装**，不是运行期查找。`Named.ID()` 会生成一个薄包装：`func (n *Named) ID() int { return n.Base.ID() }`。它对性能的影响基本为零（会被内联），但**不是零成本的语法糖**——深度为 N 的嵌入式字段访问会插 N 层指针偏移。

方法集的判定发生在类型检查阶段（`cmd/compile/internal/types2`，与标准库 `go/types` 同源；`types.NewMethodSet()` / `types.LookupFieldOrMethod()` 就是它的公开版本），**接口满足关系在编译期静态确定**；运行期只剩「找到对应 `itab`」这一件事（第 5 章）。

### 4.3.3 `type A = B` vs `type A B`：一个复制粘贴，一个新类型

```go
package main

import (
	"fmt"
	"time"
)

type UserID = int64 // 别名：UserID 和 int64 是「同一个类型」
type OrderID int64  // 定义类型：新身份，底层类型是 int64

type Duration time.Duration // 定义类型：拿到了 time.Duration 的底层表示

func (d Duration) Seconds() float64 { // ✅ 可以给「本地定义类型」加方法
	return time.Duration(d).Seconds() // 要用时再转回去（丢掉 time.Duration 的方法）
}

func main() {
	var u UserID = 1
	var i int64 = u // ✅ 别名：完全同一个类型，可直接赋值
	fmt.Println(i)

	var o OrderID = 2
	// var j int64 = o // ❌ 编译错误：类型不同（底层相同也不行）
	fmt.Println(int64(o), u)

	d := Duration(1500 * time.Millisecond)
	fmt.Println(d.Seconds()) // 1.5
}
```

规则整理：

| 写法 | 类型身份 | 方法 | 赋值/转换 | 典型用途 |
| --- | --- | --- | --- | --- |
| `type A = B` | **同一个**类型 | 完全继承 | 直接互相赋值 | 渐进式重构、给长名字起短名（`type Model = llms.Model`）、泛型别名（1.24+） |
| `type A B` | **新**类型 | **不继承**（要自己写） | 需显式 `A(x)` 转换（底层类型相同） | 造语义化 ID、加方法、限制能力（`type Handler func(...)`） |

关于转换的一条容易忘的细则：底层类型相同、且**其中一方是未命名类型**（如 `[]int`、`map[string]int`）时可以隐式赋值；两个都是命名类型就必须转换。这也解释了为什么 `type MyMap map[string]int` 之后 `var m map[string]int = MyMap{}` 是合法的——`map[string]int` 是未命名类型。

---

## 4.4 Java 对照

| 维度 | Java | Go | Go 为什么这么选 |
| --- | --- | --- | --- |
| 复用机制 | `extends` / 抽象类 / 接口默认方法 | **只有** struct 嵌入 + 接口组合 | 没有继承体系就没有「脆弱基类」问题，也没有 `super` 调用的时序陷阱 |
| 方法派发 | 虚方法表（对象头里有 klass 指针） | 静态调用为主；只有接口才动态派发 | 没有 per-object vtable → 小对象更小，调用点更可预测（第 27 章内联/去虚拟化） |
| `this` | 引用，可隐式改字段 | 接收者是**参数**，值/指针二选一 | 「改不改得到原对象」变成签名上的显式信息 |
| 方法重载 | 支持（按签名分派） | **不支持**（同名即冲突） | 重载让「这行调用的是哪个」需要推理，Go 直接禁止 |
| 嵌入 | 组合是「有一个」，继承是「是一个」 | 嵌入只是字段+方法提升，**没有 is-a** | 避免把「代码复用」误当成「类型关系」 |
| `instanceof` / cast | 运行期检查 + 可能 CCE | 类型断言 `v.(T)` 带 `ok`，或 type switch | 鼓励用类型分支替代反射式判断（第 5 章） |
| equals/hashCode/toString | `Object` 默认实现 | **没有**祖先类型，没有默认方法 | 打印用 `fmt.Stringer`，比较用 `==`（仅可比较类型）或 `reflect.DeepEqual` |
| 包装/装箱 | `int` ↔ `Integer` | `type MyInt int` 是编译期身份，零运行时开销 | 不存在装箱缓存、不存在 `Integer == Integer` 的坑 |

**最容易被 Java 直觉带偏的一点**：在 Java 里，父类方法内部调 `this.foo()` 会走到子类的 override（多态）。在 Go 里，`Base` 的方法内部调 `b.ID()` **永远**调到 `Base.ID`，它根本不知道外面套了一个 `Named`：

```go
package main

import "fmt"

type Base struct{ id int }

func (b *Base) ID() int { return b.id }
func (b *Base) Describe() string {
	return fmt.Sprintf("base[id=%d]", b.ID()) // 静态绑定到 Base.ID
}

type Named struct {
	Base
	Name string
}

func (n *Named) ID() int { return n.id + 1000 } // 遮蔽，不是 override

func main() {
	n := &Named{Base: Base{id: 1}, Name: "x"}
	fmt.Println(n.ID())        // 1001 —— 外层方法生效
	fmt.Println(n.Describe())  // base[id=1] —— 内部仍用 Base.ID，没有多态链
	fmt.Println(n.Base.ID())   // 1
}
```

想让 `Describe` 有多态，**唯一**的办法是让它依赖接口（把 `ID()` 的调用者换成接口变量），这也是 Go 里「组合 + 接口」替代继承的实际写法（第 10 章）。

---

## 4.5 框架与工程落地

| 场景 | 真实写法 | 你该学到的 |
| --- | --- | --- |
| 标准库：函数类型 + 方法 | `type HandlerFunc func(ResponseWriter, *Request)`，`func (f HandlerFunc) ServeHTTP(...)` | **定义类型 + 值接收者**把一个函数变成接口实现，零分配 |
| 标准库：值接收者的典范 | `func (d Duration) String() string`、`type IntSlice []int` 的 `Len/Less/Swap` | 小类型（1~2 个字）用值接收者，符合直觉且可内联 |
| 标准库：绝不该拷贝的类型 | `sync.Mutex`、`bytes.Buffer` 都用**指针接收者** | 含锁/含内部状态的结构体一律 `*T`（`go vet` 的 `copylocks` 会抓） |
| `gin` | `func(c *gin.Context)`：handler 全程传指针 | 请求上下文含可变状态，不可能用值 |
| `langchaingo` | `openai.LLM` 以**指针接收者**实现 `llms.Model`；构造函数返回 `llms.Model` 接口 | 「构造函数返回接口，实现指针接收者」是 Go 库的标准姿势 |
| `gorm` | `type User struct { gorm.Model; Name string }` | 嵌入的真实用途：**复用字段**（ID/CreatedAt/DeletedAt），不是复用类型关系 |
| 全体 Go 项目 | `var _ llms.Model = (*openai.LLM)(nil)` | 编译期断言「我的实现满足了接口」，重构时立刻炸出来（注意右边用 `(*T)(nil)` 而不是 `T{}`，零开销） |

一条易忽略的实践：**不要为了「以后可能要继承」而在 struct 里嵌一个 `Base`**。Go 的嵌入应该由「这组字段/方法天然属于我」驱动，而不是「我想复用代码」。真正需要扩展点时，用接口 + 组合（第 10 章）。

---

## 4.6 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| 方法里改了字段，调用方看不到变化 | 值接收者拿到副本 | 改状态一律 `func (a *Agent)` | 本章 4.1 |
| `cannot use T{} as I value: T does not implement I` | 方法用了指针接收者，`T` 的方法集里没有它 | 传 `&T{}`，或把该方法改成值接收者 | 第 5 章 |
| `cannot call pointer method on m["k"]` | map 元素不可寻址 | 取出 → 改 → 写回：`v := m[k]; v.M(); m[k] = v` | 本章 4.2 |
| `ambiguous selector x.M` | 同深度两个嵌入类型都有 `M` | 显式写 `x.A.M()` / `x.B.M()` | — |
| 期待「父类方法调到子类实现」，结果没有 | 嵌入不是继承，没有多态链 | 用接口 + 组合表达多态 | 第 10 章 |
| `go vet: passing lock by value` | 按值传了含 `sync.Mutex` 的结构体 | 传 `*T`；锁字段所在结构体永不值拷贝 | 第 22 章 |
| 结构体 size 比预期大 | 字段顺序导致 padding | 大→小排序，或 `fieldalignment` 工具 | 第 14 章 |
| 给类型加了一堆方法，结果发现转换后全丢了 | 用了 `type MyT time.Time` 拿到的是**新类型** | 需要原方法就用别名 `type MyT = time.Time` 或包一层 | 本章 4.3.3 |
| 接口里有方法却「实现不了」 | 该方法是 `func (t T)` 但你在 `*T` 上传给了值 | 记住方法集格：`*T` ⊃ `T`，反过来不成立 | 第 5 章 |
| 嵌入指针 `*Base` 后 nil panic | 嵌入指针未初始化就能调方法（接收者为 nil） | 嵌入值类型，或在构造时务必赋值 | 第 9 章 |

---

## 4.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 值语义 | 赋值即整块字节拷贝；拷贝成本 = `unsafe.Sizeof(T)` |
| 值接收者 | 拿副本，改不动原对象；小类型（≤ 3~4 字）首选 |
| 指针接收者 | 能改状态、不拷贝；含锁、含可变状态必选 |
| 方法集 | `T` 只有值接收者方法；`*T` 全都有；**接口满足只看方法集** |
| 可寻址 | `v.M()`（指针接收者）要求 v 可寻址：变量/解引用/slice 索引/字段可，map 元素/返回值不可 |
| 嵌入 | 字段+方法提升、**不是继承**、没有多态链；内层方法不知道外层 |
| 遮蔽与歧义 | 浅的遮蔽深的；同深度同名 = 编译错误 |
| 运行时元数据 | `_type`（大小/哈希/GC 位图）+ `structtype`（字段+offset）+ `uncommontype`（方法表） |
| `type A = B` | 别名，同一个类型，方法全继承 |
| `type A B` | 新类型，方法不继承，需显式转换 |

## 4.8 自检题

1. **`type T struct{}` 有 `func (t *T) M()`。为什么 `var i interface{ M() } = T{}` 编译失败，而 `var j interface{ M() } = &T{}` 可以？`&j` 的方法集是什么？**
   > 答案要点：方法集规则——`T` 的方法集只含值接收者方法；`*T` 的方法集含两者。所以只有 `*T` 满足 `M()`。`j` 是接口变量，接口类型本身没有方法集可谈（`&j` 只是 `*interface{...}`），接口的「方法集」就是它声明的方法集；接口值内部存的是 `(itab, *T)`。

2. **嵌入 `Base` 后，为什么 `Base` 的 `Describe()` 调不到 `Named.ID()`？如果 Java 里同样的结构会怎样？**
   > 答案要点：Go 的方法调用在编译期静态绑定，`Base.Describe` 的接收者是 `*Base`，看不到外层的 `Named`；Java 里 `this.ID()` 是虚调用会走到子类。想实现多态必须让 `Describe` 依赖接口（组合 + 接口）。

3. **`type OrderID int64` 和 `type OrderID = int64`，在「函数签名、JSON 序列化、方法」三方面各有什么后果？**
   > 答案要点：定义类型 → 函数签名不同（需转换）、可以定义方法、JSON 上表现为独立类型名；别名 → 与 `int64` 完全等价，无法单独加方法（也不能给非本地类型定义方法），重构期不会破坏调用方。语义化 ID 用定义类型（防串号），渐进迁移用别名。

## 4.9 延伸阅读

- `$GOROOT/src/runtime/type.go`：`_type`、`structtype`、`uncommontype`、`method`、`structfield`
- `$GOROOT/src/cmd/compile/internal/types2/`：`methodset.go`（方法集计算）、`lookup.go`（`LookupFieldOrMethod`）
- 标准库 `go/types`：`NewMethodSet`、`LookupFieldOrMethod`（写工具/分析器时直接用）
- Go Spec：*Method sets*、*Type identity / assignability*、*Struct types*、*Selectors* 四节
- `$GOROOT/src/net/http/server.go`：`HandlerFunc` 的定义与 `ServeHTTP`
- 相关章节：[第 5 章 接口底层](5-interface-internals.md)、[第 10 章 组合优于继承](10-composition-plugin-registry.md)、[第 14 章 内存布局](14-memory-layout-escape.md)

---

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 3 章 语法极速通道](3-syntax-crash.md)
- 下一章：[第 5 章 接口的底层：eface / iface / itab](5-interface-internals.md)
- 相关：值语义全景见[第 1 章](1-mindset-shift.md)与[第 9 章](9-zero-value-optionals.md)；修饰符与可见性见[第 3 章](3-syntax-crash.md)
