# 14. 内存布局、对齐与逃逸分析

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 数据结构与内存 ｜ **编号**：14 / 54
> **本章定位**：这一章解决三个问题：**一个 struct 在内存里到底占多少、字段怎么排、为什么你的 `atomic.Int64` 悄悄把吞吐砍半；以及怎么让一个对象留在栈上而不是堆上。** 逃逸分析是 Go 唯一的"分配优化器"，读不懂它，你在 Go 里做的所有性能优化都是玄学。
> **读法**：14.2 的对齐规则要能自己算出来；14.3 的规则清单建议对着自己的代码跑一遍 `-gcflags=-m`；14.4 只讲 Java 会猜错的地方。

---

## 14.1 引子：两个反直觉的性能数字

**场景一。** 一个指标聚合结构体，两个 goroutine 分别累加两个计数器：

```go
type Metrics struct {
	Requests atomic.Int64
	Errors   atomic.Int64
}
```

8 核机器上压测，理论应该接近线性扩展，实测却只到单核的 1.6 倍。`pprof` 火焰图显示：`atomic.Int64.Add` 本身占 40% 的 CPU，而它只是 `LOCK XADD` 一条指令。

原因：`Requests` 和 `Errors` 挨着放，**共用一个 64 字节的 cache line**。两个核各自修改自己的字段时，MESI 协议必须让对方的缓存行失效——于是这条 cache line 在两个核之间来回弹乒乓，每一次写都是一次跨核总线事务。这叫 **false sharing（伪共享）**：逻辑上不共享，物理上共享。

修法只有一行：把两个字段推到不同的 cache line 上。

**场景二。** 你把一个 `[]*Item` 改成 `[]Item`（指针数组改成值数组），**什么都没改逻辑**，QPS 涨了 30%。原因：`[]*Item` 遍历时每次都要沿指针跳一次内存（随机访问，几乎必然 cache miss）；`[]Item` 是连续内存，CPU 预取器能把整段拉进 cache。

这两件事的共同点：**它们都不在 Java 程序员的性能直觉里**。Java 里对象的布局由 JVM 决定（你插不进 padding）、对象一定在堆上（你也没法让它去栈上）。Go 把这两件事的**控制权**（和**责任**）都交给你了。

> 🎯 **本章主线**：Go 的内存模型有三条你必须亲手管理的线 —— ① 字段顺序（省内存）；② 缓存行（省 CPU）；③ 逃逸与否（省 GC）。前两条是机械劳动，第三条要读懂编译器。

---

## 14.2 直觉模型：结构体布局与对齐

### 14.2.1 三条对齐规则（背下来就够）

1. **每个字段的地址必须是自身对齐值的整数倍**（`int64` 对齐 8，`int32` 对齐 4，`bool` 对齐 1）。
2. **结构体的对齐值 = 其字段对齐值的最大值**。
3. **结构体的 size 会向上取整到自身对齐值的倍数**（尾部 padding）。

于是同一个逻辑结构，字段顺序不同，大小能差一半：

```
type Bad struct {                 type Good struct {
    a bool   // offset 0             b int64  // offset 0
             // pad 7 bytes                     
    b int64  // offset 8             a bool   // offset 8
    c bool   // offset 16            c bool   // offset 9
             // pad 7 bytes                     // pad 6 bytes
}             size 24              }            size 16

布局图（Bad，24B）：
 0    1                              8                              16   17             24
 ┌────┬───────────────────────┬──────────────────────────────┬────┬──────────────┐
 │ a  │ padding (7)           │ b (8)                        │ c  │ padding (7)  │
 └────┴───────────────────────┴──────────────────────────────┴────┴──────────────┘
                                ↑ 白白浪费 14 字节 = 58%

布局图（Good，16B）：
 0                              8    9                     16
 ┌──────────────────────────────┬────┬──────────────────────┐
 │ b (8)                        │ a  │ c  │ padding (6)     │
 └──────────────────────────────┴────┴──────────────────────┘
```

**规则一句话**：**把字段按对齐值从大到小排**（或者按大小排也行），padding 自然最少。

### 14.2.2 用 `unsafe` 亲手量（可直接跑）

```go
// go run main.go
package main

import (
	"fmt"
	"unsafe"
)

type Bad struct {
	a bool
	b int64
	c bool
}

type Good struct {
	b int64
	a bool
	c bool
}

type Mixed struct {
	name  string  // 16B, align 8
	id    int64   // 8B,  align 8
	age   uint8   // 1B,  align 1
	score float32 // 4B,  align 4
	ok    bool    // 1B,  align 1
}

func dump(name string, v any, size, align uintptr, fields ...struct {
	n string
	o uintptr
}) {
	fmt.Printf("\n%s: size=%d align=%d\n", name, size, align)
	var last uintptr
	for i, f := range fields {
		var gap uintptr
		if i > 0 {
			gap = f.o - last
		}
		fmt.Printf("  %-6s offset=%-3d gap_before=%d\n", f.n, f.o, gap)
		last = f.o
		_ = v
	}
}

func main() {
	var b Bad
	var g Good
	var m Mixed

	dump("Bad", b, unsafe.Sizeof(b), unsafe.Alignof(b),
		struct{ n string; o uintptr }{"a", unsafe.Offsetof(b.a)},
		struct{ n string; o uintptr }{"b", unsafe.Offsetof(b.b)},
		struct{ n string; o uintptr }{"c", unsafe.Offsetof(b.c)},
	)
	dump("Good", g, unsafe.Sizeof(g), unsafe.Alignof(g),
		struct{ n string; o uintptr }{"b", unsafe.Offsetof(g.b)},
		struct{ n string; o uintptr }{"a", unsafe.Offsetof(g.a)},
		struct{ n string; o uintptr }{"c", unsafe.Offsetof(g.c)},
	)
	dump("Mixed", m, unsafe.Sizeof(m), unsafe.Alignof(m),
		struct{ n string; o uintptr }{"name", unsafe.Offsetof(m.name)},
		struct{ n string; o uintptr }{"id", unsafe.Offsetof(m.id)},
		struct{ n string; o uintptr }{"age", unsafe.Offsetof(m.age)},
		struct{ n string; o uintptr }{"score", unsafe.Offsetof(m.score)},
		struct{ n string; o uintptr }{"ok", unsafe.Offsetof(m.ok)},
	)
	fmt.Println("\nZero: sizeof(struct{}{}) =", unsafe.Sizeof(struct{}{})) // 0
}
```

输出会告诉你：`Bad=24`、`Good=16`、`Mixed=40`（`16+8+1+4+1=30` → 对齐 8 后取整到 40）。**`Mixed` 只要把 `age` 和 `ok` 挪到最后就能从 40 降到 32。**

> ⚠️ **不要为了省几个字节去折腾字段顺序**——除非这个结构体是**成百万个实例**的（比如向量检索的命中结果、token 流的事件）。一个 40B 的结构体改成 32B，100 万实例省 8MB，值得；一个 1000 个实例的配置结构体，省 8KB，纯属浪费阅读成本。判断标准永远是 pprof，不是直觉。

### 14.2.3 cache line 与 false sharing

CPU 与内存之间以 **cache line（通常 64 字节）** 为单位搬运数据。

```
                  一个 cache line = 64 字节
        ┌───────────────────────────────────────────────┐
核心0   │  Requests(8B) │  Errors(8B) │   其它 48B       │  ← 同一行！
        └───────────────────────────────────────────────┘
                    ▲                    ▲
                    │ 写                 │ 写
              CPU0 独占               CPU1 想独占 → 必须让 CPU0 的行失效（RFO）
                    └──── 每次写都来回"抢行"：false sharing ────┘

修法：把两者拉开到不同 cache line
        ┌───────────────────────────────────────────────┐
        │  Requests(8B) │ padding(56B)                  │  行 A
        ├───────────────────────────────────────────────┤
        │  Errors(8B)   │ padding(56B)                  │  行 B
        └───────────────────────────────────────────────┘
```

标准做法（`golang.org/x/sys/cpu` 提供 `CacheLinePad` / `CacheLineSize`，amd64 与 arm64 上都是 64）：

```go
import "golang.org/x/sys/cpu"

type Metrics struct {
	Requests atomic.Int64
	_        cpu.CacheLinePad // struct{ _ [CacheLineSize]byte }
	Errors   atomic.Int64
}
```

Go 自己的 runtime 就是这么干的（`runtime/mheap.go` 里 `mheap.central` 的每个 `mcentral` 后面都补了 padding）：

```go
// runtime/mheap.go（节选，真实写法）
central [numSpanClasses]struct {
	mcentral mcentral
	pad      [cpu.CacheLinePadSize - unsafe.Sizeof(mcentral{})%cpu.CacheLinePadSize]byte
}
```

**这里有个更朴素的替代方案，往往比 padding 更划算**：让**每个 goroutine 用自己的计数器**（分片），最后合并。padding 治的是"两个核写同一行"，分片治的是"两个核写同一个变量"——后者通常才是真正的元凶。

```go
// 可运行：量化 false sharing（需在多核机器上跑，GOMAXPROCS>=2）
package main

import (
	"fmt"
	"sync"
	"sync/atomic"
	"time"
)

type tight struct {
	a atomic.Int64
	b atomic.Int64 // 与 a 同一个 cache line
}

type spread struct {
	a atomic.Int64
	_ [64]byte // 把 b 推到下一行；实际用法是 cpu.CacheLinePad
	b atomic.Int64
}

const iters = 20_000_000

func measure(a, b *atomic.Int64) time.Duration {
	var wg sync.WaitGroup
	start := time.Now()
	wg.Add(2)
	go func() { defer wg.Done(); for i := 0; i < iters; i++ { a.Add(1) } }()
	go func() { defer wg.Done(); for i := 0; i < iters; i++ { b.Add(1) } }()
	wg.Wait()
	return time.Since(start)
}

func main() {
	var t tight
	var s spread
	fmt.Println("same cache line:", measure(&t.a, &t.b))
	fmt.Println("padded         :", measure(&s.a, &s.b))
	// 典型结果：大概是 2~4 倍差距（机器越新、核越多，差距越大）
}
```

---

## 14.3 逃逸分析：Go 唯一的分配优化器

### 14.3.1 它到底在做什么

每个 goroutine 的栈初始只有 2KB（详见第 17 章），栈上的对象随函数返回自动释放，**不产生任何 GC 压力**。堆上的对象要 `mallocgc` + 被三色标记清扫（详见第 15、28 章）。

**逃逸分析（escape analysis）就是编译器在回答一个问题：这个对象能不能保证"函数返回后没人再引用它"？** 能 → 放栈；不能 → 放堆。

编译器为此构造一张**位置图（location graph）**：节点是变量、字段、解引用层级，边表示"可能指向"，权重是解引用次数（derefs）。然后跑一个 Bellman-Ford 风格的传播（`cmd/compile/internal/escape/solve.go` 的 `solve`），把"逃逸到堆 / 逃逸到返回值 / 可达调用方"这些状态沿边扩散出去。判定细节分布在 `escape.go`（主逻辑）、`escape_call.go`（函数调用的泄漏摘要）、`escape_builtin.go`（`make`/`new`/`append`）、`escape_unsafe.go`（`unsafe.Pointer` 相关）。

```
           location graph（简化示意）

  p ──derefs=0──► Point{} ──► (X, Y 字段节点)
  │
  └──► 函数返回值 result（是一个"逃逸出口"）
                    │
                    ▼
            传播得到：Point 逃逸到堆 → 用 newobject 分配

  q ──► 局部变量，没有任何通向出口的路径 → 留在栈上
```

### 14.3.2 完整规则清单（能覆盖 95% 的实际情况）

| # | 规则 | 典型代码 | 结论 / `-m` 输出 |
| --- | --- | --- | --- |
| 1 | 返回局部变量地址 | `return &p` | 逃逸：`moved to heap: p` |
| 2 | 赋给包级变量（全局可达） | `sink = p` | 逃逸：`p escapes to heap` |
| 3 | 装箱进 `interface{}` / 非空接口 | `sink = any(p)`、`fmt.Println(p)` | 逃逸：`p escapes to heap` |
| 4 | 闭包捕获，且闭包本身逃逸 | `return func() int { return c }` | `c` 逃逸 |
| 5 | 发到 channel | `ch <- p` | 逃逸（可能被别的 goroutine 拿到） |
| 6 | 交给新 goroutine | `go f(&p)` | 逃逸 |
| 7 | 存进 slice / map 元素 | `s = append(s, &p)` | 逃逸（底层数组可能被共享/扩容） |
| 8 | 长度在运行期才确定 | `make([]byte, n)` | 逃逸：`make([]byte, n) escapes to heap` |
| 9 | 体积过大 | 栈上放不下（隐式变量 > 64KB，显式变量 > 10MB，见 `ssagen` 的 `maxImplicitStackVarSize` / `maxStackVarSize`） | `moved to heap: big` |
| 10 | 传给参数被标记为泄漏 | `f(&p)` 且 `f` 的参数 `leaks to heap` | `parameter x leaks...` |
| 11 | 用了 `unsafe.Pointer` / 无 `//go:noescape` 的汇编函数 | `*(*int)(unsafe.Pointer(u))` | 保守判定为逃逸 |
| 12 | 反射 | `reflect.ValueOf(p)` | 逃逸（装箱） |

**反向结论同样重要**：满足下面这些条件的对象**一定留在栈上**（不产生分配）：
- 只在本函数内使用，且不取地址、不装箱、不进 channel、不进集合；
- `make([]T, 常量)` 且 `常量 * sizeof(T)` 不大（例如 `var buf [4096]byte` 就在栈上，作为局部数组）；
- 通过参数传入的小结构体（按值传，拷贝在栈上）。

### 14.3.3 `-gcflags=-m` 实战

```go
// go build -gcflags='-m -l' main.go   （-l 关掉内联，让逃逸结论更纯）
package main

type Point struct{ X, Y int }

var sink any
var ch = make(chan Point, 1)

func stackOnly() int { // 留在栈上
	p := Point{1, 2}
	return p.X + p.Y
}

func retAddr() *Point { // 规则 1
	p := Point{1, 2}
	return &p
}

func boxed() { // 规则 3
	p := Point{1, 2}
	sink = p
}

func dynamic(n int) []byte { // 规则 8
	return make([]byte, n)
}

func counter() func() int { // 规则 4
	c := 0
	return func() int { c++; return c }
}

func send() { // 规则 5
	p := Point{1, 2}
	ch <- p
}

func main() {
	_ = stackOnly()
	_ = retAddr()
	boxed()
	_ = dynamic(10)
	_ = counter()
	send()
}
```

实际输出（行序与措辞随版本略有差异）：

```
./main.go:12:2:  p does not escape                    ← stackOnly：完美
./main.go:17:2:  moved to heap: p                     ← retAddr：逃逸
./main.go:22:2:  p escapes to heap                    ← boxed：装箱逃逸
./main.go:26:11: make([]byte, n) escapes to heap       ← dynamic：长度不定
./main.go:31:2:  moved to heap: c                     ← counter：闭包捕获
./main.go:36:2:  p does not escape                    ← send：值传给 channel，Point 本身没逃逸
```

最后一条值得停下来看一眼：`ch <- p` 传的是**值**，会被复制进 channel 的缓冲区，所以**被复制的那个 `Point` 不需要逃逸**（channel 的 buf 本来就是堆上的，由 hchan 管）。如果你的版本这里报 `escapes to heap`，多半是因为那个 channel 本身逃逸到了别处，**以本地输出为准**。如果你写成 `ch <- &p`，那就变成规则 1 的变体，`p` 会 `moved to heap`。**这条解释了"发 channel 一定要发值还是发指针"的一半答案**（另一半是拷贝成本，详见第 20 章）。

常用命令行组合：

| 命令 | 用途 |
| --- | --- |
| `go build -gcflags='-m' ./...` | 看逃逸 + 内联决策（最常用） |
| `go build -gcflags='-m -m' ./...` | 更啰嗦，带"为什么"（`escapes as ... with derefs=1`） |
| `go build -gcflags='-m -l' ./...` | **关闭内联**，只想知道逃逸结论时用（内联会改变结论，先关掉看基线） |
| `go build -gcflags='-m=2'` | 等价于 `-m -m`（新版本写法） |
| `go test -bench . -benchmem` | 看真实的 `B/op` 与 `allocs/op`，验证优化是否生效 |
| `go tool pprof -alloc_space` | 在真实负载下看出**谁**在分配（合并了逃逸 + 大小的结论） |

> 💡 **不要迷信 `-m`**：它的结论会随内联、泛型实例化、版本优化而变。**唯一可信的是 `-benchmem` 的 `allocs/op` 和 pprof。** `-m` 只是"为什么"的解释器。

### 14.3.4 "合并分配"：Go 没有 TLAB，但有别的省法

Java 的分配优化主要是两件：TLAB 里 bump pointer（快），以及 C2 的**标量替换**（把对象拆成局部变量，于是对象彻底消失）。**Go 两者都没有**：Go 没有标量替换（对象只能整个在栈或整个在堆），Go 的分配走 `mcache` 的 size class 分级（详见第 15 章）。

所以 Go 里省分配要靠**结构设计**：

```go
// ❌ n 次分配 + n 次指针跳转
type Item struct{ ID int64; Score float64 }
func loadPtrs(n int) []*Item {
	out := make([]*Item, n)
	for i := 0; i < n; i++ {
		out[i] = &Item{ID: int64(i)} // n 次 mallocgc
	}
	return out
}

// ✅ 1 次分配 + 连续内存（CPU 预取友好）
func loadVals(n int) []Item {
	out := make([]Item, n)
	for i := 0; i < n; i++ {
		out[i].ID = int64(i) // 0 次额外分配
	}
	return out
}
```

同理，**AoS（array of structs）vs SoA（struct of arrays）**是同一思路的延伸：如果你的热点循环只扫 `Score` 字段，`[]float64` 单独放一个切片，cache line 利用率能从 12.5% 提到 100%。

| 技巧 | 做法 | 收益 |
| --- | --- | --- |
| 一次分配代替 n 次 | `make([]T, n)` 而不是 `[]*T` + 逐个 `&T{}` | allocs/op 从 n 降到 1 |
| SoA / 字段拆分 | 热点字段单独成切片 | 缓存利用率 ×8 |
| 把 hot 字段聚到同一行 | 高频访问的字段放一起（同时把并发写的字段**分开**） | 少 cache miss，且避免 false sharing |
| 预分配容量 | `make([]T, 0, n)` / `Builder.Grow(n)` | 避免扩容拷贝（详见第 11 章） |
| 避免装箱 | 少用 `any`，接口只用在抽象边界 | 少一次堆分配 + 一次类型断言 |
| 复用对象 | `sync.Pool`（详见第 22 章） | 降低 GC 频率，非零成本 |

---

## 14.4 Java 对照：会猜错的四件事

| 维度 | JVM（HotSpot） | Go |
| --- | --- | --- |
| 对象头 | mark word 8B + klass 指针 4B（压缩 oops）≈ **12B**，对齐后 16B；数组再加 4B 长度 | **没有对象头**。`struct{}{}` 就是 0 字节（指向 `runtime.zerobase`） |
| 类型信息在哪 | 对象自带 klass 指针，运行期随时反射 | 编译期确定；GC 靠 **span 的 type 位图 + size class 对齐**反推指针位置（见第 15 章） |
| 分配 | 每线程 TLAB，bump pointer | 每 **P** 一个 `mcache`，按 size class 分档；goroutine 换 P 就换 mcache |
| 栈上分配 | 靠 C2 的**标量替换**（对象被拆成标量，等于消失） | 靠逃逸分析决定**整个对象**在栈/堆，**没有标量替换** |
| 布局控制 | 你插不进 padding（只能 `@Contended`，且需要 `-XX:-RestrictContended`） | 字段顺序你随便排，padding 自己补 |
| 伪共享解法 | `@Contended` + `LongAdder` 的 cells 数组 | 手动 padding（`cpu.CacheLinePad`）/ 分片 |
| 对象身份 | 有（`identityHashCode`、`synchronized(obj)`） | **没有**（所以可以随便按值拷贝，也没有"每个对象一把锁"） |

**四个会猜错的地方：**

**① "Go 对象更省内存"其实要看情况。** `map[string]int` 的每个 entry 在 Go 里几乎没有 per-object 开销，而 Java 的 `HashMap.Node` 是 **32 字节对象**（16B 头 + hash + key ref + value ref + next ref）。所以元素多了 Java 更亏。但反过来，Java 的 `int[]` 只有 16B 头，Go 的 `[]int` 是 24B header + 独立数组——**独立的小数组在 Go 里反而更贵**。别一概而论。

**② 逃逸分析不是"优化"，是"决策"。** HotSpot 的 EA 是**在有对象的前提下尝试消除它**（属于额外优化，且经常因为方法太大而放弃）；Go 的 EA 是**决定对象放哪**（属于分配的必经之路）。所以 Go 的结论更可预测——`-m` 说逃逸就一定分配——但你也失去了"JIT 在运行时根据真实类型把虚拟调用和内联做到极致"的那部分能力（详见第 27 章）。

**③ Go 的栈会移动，所以"栈上地址"不可长期持有。** Java 的对象地址稳定（GC 会移动，但对 Java 代码透明）；Go 的 goroutine 栈在增长时会**整块复制**（`runtime/stack.go` 的 `copystack`），栈上的对象地址会变。这正是为什么"取地址"经常导致逃逸，也是第 16 章 `uintptr` 陷阱的根源。

**④ TLAB ≠ mcache。** TLAB 是"线程本地的一大块 Eden"，分配就是移动一个指针；`mcache` 是"每个 P 一组按 size class 分好的 span 链表"，分配要走 size class 映射 + span 内空闲位图。前者是一条指令，后者是十几条。这也解释了为什么 Go 的大量小对象分配比很多人想象的贵——**它是"快"，但不是"TLAB 那么快"**。

---

## 14.5 框架与工程落地

**① 高频事件结构体的字段顺序。** Agent 的 token 流事件、OTel span 属性、向量检索命中项，都是"每请求数十次、每次几十字节"的分配大户。这类结构体值得做一次布局审查：

```go
// 布局审查前：三个 bool 各自插在两个 int64/float64 中间，padding 丛生
type Hit1 struct {
	ID      int64
	IsBM25  bool
	Score   float64
	IsFresh bool
	DocLen  int32
	IsPinned bool
} // 40 字节

// 布局审查后：大字段在前，小字段聚尾部
type Hit2 struct {
	ID       int64
	Score    float64
	DocLen   int32
	IsBM25   bool
	IsFresh  bool
	IsPinned bool
} // 24 字节（省 40%）
```

**② 并发写同一结构体的字段一定 padding 或分片。** 典型场景：限流器的计数器、连接池的统计、每租户的 token 账本。看 `golang.org/x/time/rate` 的实现就明白为什么 `Limiter` 只有一个 `mu sync.Mutex`——它选择了**锁**而不是**padding**，因为在中等竞争下锁更简单可靠；只有在超高竞争下才值得上分片 + padding。

**③ 别把 `sync.Pool` 当 TLAB 用。** `sync.Pool` 的 `Get/Put` 有清晰的成本（每 P 的 private/shared、victim cache 的两轮 GC，详见第 22 章）。它的价值是"复用大对象（>1KB，如 buffer）少触发 GC"，不是"让分配变快"。对一个 16 字节的 struct 用 Pool，往往比直接分配更慢。

**④ 用 pprof 的 `alloc_objects` 找到"该做逃逸分析"的地方，别猜。**

```bash
# 在真实负载下采 30 秒
curl -s "http://localhost:6060/debug/pprof/heap?seconds=30" -o heap.pb.gz
go tool pprof -http=:8081 -alloc_objects heap.pb.gz
# 看 alloc_objects 而不是 inuse_space：前者告诉你"谁在频繁分配"，后者告诉你"谁占着内存"
# 找到热点函数后，再回到 go build -gcflags='-m' 看它为什么逃逸
```

---

## 14.6 踩坑清单（CR 可直接抄）

| # | 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- | --- |
| 1 | 多核压测扩展性差，`atomic.Add` 占大量 CPU | false sharing：并发写同一 cache line | 字段加 padding（`cpu.CacheLinePad`）或分片计数 | 14.2.3 |
| 2 | 结构体成百万实例，内存超预算 | 字段顺序导致大量 padding | 按对齐值从大到小排列字段 | 14.2.1 |
| 3 | 热点路径 `allocs/op` 高，GC 频繁 | 循环里 `&T{}`、装箱、`make` 长度不定 | 用 `[]T` 替 `[]*T`、预分配、去掉 `any` | 14.3.4 |
| 4 | `-m` 说"does not escape"，实测仍在分配 | 内联改变了结论 / 是接口方法 / 是反射 | 用 `-gcflags='-m -l'` 看基线，最终以 `-benchmem` 为准 | 14.3.3 |
| 5 | 取地址给 `sync/atomic` 用，结果对象逃逸 | Go 1.19 前 `atomic.AddInt64(&x,1)` 会强制逃逸 | Go 1.19+ 用 `atomic.Int64` 字段（`atomic.Int64` 内部用 `noescape`） | 第 22 章 |
| 6 | 大 struct 按值传参，栈暴涨 | 值语义全量拷贝；大 struct 还可能触发栈增长/复制 | 超过几个 cache line 就传 `*T`（但注意共享语义） | 第 4 章 |
| 7 | 把栈上对象的地址长期存起来，偶发"数据错乱" | 栈会增长并整块复制（`copystack`），指针需要被修正；用 `uintptr` 逃过 GC 就修不了 | 禁止把地址转成 `uintptr` 保存；见 `unsafe` 规则 | 第 16 章 |
| 8 | `struct{ mu sync.Mutex; ... }` 被复制后出诡异问题 | `sync.Mutex` 复制即失效（vet 能查 copylocks） | 含锁结构体一律用指针传递；跑 `go vet` | 第 22 章 |
| 9 | 以为 `struct{}{}` 会分配内存 | 零大小对象：`&struct{}{}` 指向 `runtime.zerobase`，不分配 | 用它做 `map[K]struct{}` 的 set 是正确的省内存方式 | 第 9 章 |
| 10 | SoA/AoS 选错，向量扫描慢 3 倍 | 只想扫一个字段，却把整个 struct 拉进 cache | 热点字段独立成切片（SoA）；只有在全字段都用时才 AoS | 14.3.4 |

---

## 14.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 对齐规则 | 字段地址是自身对齐倍数；结构体对齐 = 字段最大对齐；size 向上取整 |
| 省内存 | 字段按对齐值**从大到小**排，padding 最少 |
| 量布局 | `unsafe.Sizeof` / `Alignof` / `Offsetof`；别猜 |
| cache line | 64 字节（amd64/arm64）；CPU 以行为单位搬运 |
| false sharing | 逻辑不共享、物理同行 → 缓存行乒乓；解法：padding 或分片 |
| 逃逸分析 | 决定对象放**栈**还是**堆**；栈上分配 = 零 GC 成本 |
| 逃逸触发器 | 返回地址、全局变量、装箱接口、闭包逃逸、channel、goroutine、集合、`make` 变量长、体积过大、`unsafe`/反射 |
| 观察手段 | `go build -gcflags='-m'`（结论）、`-benchmem`（真相）、pprof `alloc_objects`（热点） |
| 没有的东西 | 没有标量替换、没有 TLAB、没有对象头、没有对象身份 |
| 合并分配 | `[]T` 替 `[]*T`；SoA；一次性 `make`；避免装箱 |
| 与 JVM 的核心差异 | JVM 帮你决定布局和位置；Go 让你自己决定，代价是你要自己量 |

## 14.8 自检题

1. **`type S struct{ a bool; b int64; c bool }` 在 amd64 上占多少字节？改成什么顺序最小？为什么 Go 不自动帮你重排字段？**
   > 答案要点：`Bad` 是 24 字节（a@0，pad7，b@8，c@16，尾部 pad7）；重排成 `{b int64; a, c bool}` 是 16 字节（b@0，a@8，c@9，尾部 pad6）。Go 不自动重排的原因：① 字段顺序是语言可见的语义（`unsafe.Offsetof`、`reflect`、和 C 结构体互操作、序列化都可能依赖它）；② 重排会破坏"按字面顺序初始化/打印"的可预测性；③ 自动重排需要全局的 profile 信息，而 Go 选择把决定权交给开发者（和 `gofmt` 一样：静态、可预测）。

2. **为什么 `ch <- p`（p 是 `Point` 值）时 `p` 可以"不逃逸"，而 `ch <- &p` 必须逃逸？**
   > 答案要点：channel 发送的是**值拷贝**，`p` 的副本被写进 `hchan.buf`（那段内存在堆上，由 channel 自己持有），原 `p` 生命周期仍限于本函数，所以不需要逃逸。发指针则不同：指针本身进了 channel，别的 goroutine 拿到后会解引用它，编译器必须假设"引用在函数返回后仍有效"，于是 `moved to heap: p`。推论：发小结构体优先发值（避免分配），发大结构体发指针（避免拷贝）——这个权衡在第 20 章还会出现一次。

3. **你在 8 核机器上写了个"两个 goroutine 各累加一个 `atomic.Int64`"的代码，只拿到 1.6 倍扩展。怎么定位、怎么修？**
   > 答案要点：先确认是不是 false sharing —— 用 padding 版本做 A/B 对比（`atomic.Int64` + 56 字节 padding），差距明显就是它。修法优先级：① **分片**（每核/每 P 一个计数器，读时合并），这治的是"同一个变量"的竞争，通常收益最大；② padding（`cpu.CacheLinePad`）到不同 cache line，治的是"同一行"的伪共享；③ 如果只是统计用途，甚至可以"每请求本地累加、批量 flush"。注意 `atomic.Int64` 的对齐是 8，但如果结构体起始地址没对齐到 64，padding 的计算必须基于 `unsafe.Offsetof` 实际验证。

## 14.9 延伸阅读

- 编译器：`$GOROOT/src/cmd/compile/internal/escape/`（`escape.go` 主逻辑、`solve.go` 的位置图求解、`escape_call.go` 参数泄漏摘要、`escape_builtin.go`、`escape_unsafe.go`）
- 栈大小阈值：`$GOROOT/src/cmd/compile/internal/ssagen/ssa.go` 的 `maxStackVarSize` / `maxImplicitStackVarSize`
- 栈复制：`$GOROOT/src/runtime/stack.go`（`copystack` / `newstack`）
- runtime 里的 padding 范例：`$GOROOT/src/runtime/mheap.go`（`mheap.central` 的 `pad` 字段）
- `golang.org/x/sys/cpu`：`CacheLineSize` / `CacheLinePad`
- 延伸读物：Go 官方 wiki「CompilerOptimizations」；Dmitry Vyukov 关于 false sharing 与 sharded counters 的文章
- 相关章节：[11. 数组与切片](11-slice-array.md)、[15. 内存分配器](15-allocator.md)（逃逸出去之后到底怎么分配）、[28. GC](28-gc.md)、[30. 性能优化](30-perf-tuning.md)

---

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[13. string / []byte / rune 与零拷贝](13-string-bytes-rune.md)
- 下一章：[15. 内存分配器：mcache / mcentral / mheap](15-allocator.md)
