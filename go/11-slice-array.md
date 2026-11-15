# 11. 数组与切片：header、扩容与共享陷阱

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 数据结构与内存 ｜ **编号**：11 / 54
> **本章定位**：slice 是 Go 里"最像 Java 但是最不一样"的东西。三个字段的 header、一个两段式的扩容公式、一份共享的底层数组——三点搞明白，Go 里 80% 的数据错乱 bug 就失业了。
> **读法**：11.3 是源码精读（`runtime/slice.go`），11.6 的坑表建议放进团队 CR checklist。数组部分很短但别跳（11.4 有 Java 会猜错的地方）。

---

## 11.1 引子：三天后才发现的"数据串台"

一个批量 embedding 的任务，切片批次是这么取的：

```go
package main

import "fmt"

func main() {
	for i := range data {
		data[i] = i
	}

	first := data[:3]        // [0 1 2]，cap 仍是 10
	second := data[3:6]      // [3 4 5]

	// 有个分析函数想"扩展一下"first
	first = append(first, 99) // 没超 cap 10 → 直接写进 data[3]！

	fmt.Println(second) // [99 4 5]  ← 串台了
	fmt.Println(data)   // [0 1 2 99 4 5 6 7 8 9]
}
```

`append` 没有"分配新数组"，因为 `cap(first) == 10 > 3`，它就地写入 `data[3]`——而那块内存正被 `second` 看着。

换成 Java 的 `List.subList(0, 3)`，`add` 会抛 `UnsupportedOperationException`（视图不允许结构修改）；Go 的 slice 什么都不会抛，它只是**默默共享一块内存**。

第二个更贵的事故，叫切片泄漏：

```go
// 从一个 200MB 的日志缓冲里取出前 10 行返回
func head(buf []byte) []byte {
	return buf[:10] // ❌ 只返回了 10 字节，但整块 200MB 底层数组都活着
}
```

`buf[:10]` 的 header 里，`array` 指针仍然指向那 200MB 的首地址。GC 看到"有人指着这块内存"，于是 200MB 永久驻留。一个接口返回 `[]byte`，内存涨到 OOM，排查三天。

**这两个 bug 同源：你需要精确知道 slice header 里有什么、`append` 什么时候分家。**

---

## 11.2 直觉模型：header 是"三个字"的遥控器

### 图一：slice header 与底层数组

```
var s = make([]int, 3, 5)     // 元素 8 字节

  s (24 字节，在栈上或被拷贝来拷贝去)               底层数组（堆上，5 个 int = 40 字节）
  ┌──────────────┬──────┬──────┐                 ┌────┬────┬────┬────┬────┐
  │ array        │ len  │ cap  │                 │ 0  │ 0  │ 0  │ ?  │ ?  │
  │ 0xc0000b4000 │  3   │  5   │────────────────►│[0] │[1] │[2] │[3] │[4] │
  └──────────────┴──────┴──────┘                 └────┴────┴────┴────┴────┘
     8 字节        8 字节  8 字节                ↑ 可以越 len 访问（s[:5]），
                                                  ↑ 但 s[3] 会 panic（越界检查看 len）
```

三条铁律：

1. **slice 是值**。赋值/传参 = 拷贝这 24 字节 header，**底层数组不拷贝**。
2. **`len` 管越界检查，`cap` 管扩容**。`s[3]` panic 因为 `len==3`；`s[:5]` 合法因为 `cap==5`。
3. **`array` 指针 + `len` 解耦**：`s[1:3]` 生成新 header（`array+8`、`len=2`、`cap=4`），**没分配任何内存**。

### 图二：`append` 的两条命运

```
s := []int{1,2,3}   cap=4（字面量通常 cap==len，这里假设有富余）

   len < cap：就地写                            len == cap：搬家
   ┌─┬─┬─┬─┐                                   ┌─┬─┬─┐        ┌─┬─┬─┬─┬─┬─┬─┬─┐
   │1│2│3│ │  s = append(s, 4)                 │1│2│3│        │1│2│3│4│ │ │ │ │
   └─┴─┴─┴─┘  → 原地改，返回同一个 array       └─┴─┴─┘        └─┴─┴─┴─┴─┴─┴─┴─┘
              → 所有共享者都看得见                            ↑ 新数组，老数据 memmove 过来
                                                             → 老数组不再被这个 s 引用
                                                             → 但别的 slice 可能还指着它！
```

**"分家"是 slice 语义里最反直觉的一点**：同一个 `s`，`append` 前后可能指向不同数组，于是"我改了 s，为什么别人没变 / 为什么别人也变了"取决于当时 `len` 和 `cap` 的关系。

### 图三：数组是"一整块值"，切片是"一张门票"

```
var a [3]int            // 一个值，24 字节，赋值即整块拷贝
var s []int = a[:]      // 一张门票，24 字节 header，指向 a

  函数传 [3]int → 拷 24 字节，改不到原数组
  函数传 []int  → 拷 24 字节 header，改 s[i] 能改到原数组
```

---

## 11.3 底层实现：`runtime/slice.go`

### 11.3.1 定义与 `makeslice`

```go
// runtime/slice.go
type slice struct {
	array unsafe.Pointer // 指向底层数组首元素
	len   int
	cap   int
}
```

`make([]T, len, cap)` 走 `makeslice`（注意它拿的是**元素类型元数据** `et`，所以同一个函数能处理所有元素类型）：

```go
func makeslice(et *abi.Type, len, cap int) unsafe.Pointer {
	mem, overflow := math.MulUintptr(et.Size_, uintptr(cap)) // cap * sizeof(T)，带溢出检查
	if overflow || mem > maxAlloc || len < 0 || len > cap {
		// len < 0 / len > cap 是编译期抓不到的动态值
		// → panic: makeslice: len out of range / cap out of range
		panicmakeslicelen()  // 或 panicmakeslicecap()
	}
	return mallocgc(mem, et, true) // 真分配，走第 15 章的三级缓存
}
```

关键点：**`make` 一定分配**（`cap==0` 时返回 `zerobase`，不分配），并且 `make([]T, 5)` 的 5 个元素是**已经清零的、合法的零值**——不是 Java 的 `new Object[5]`（里面是 5 个 null）。

### 11.3.2 `growslice`：扩容公式（Go 1.18+）

`append` 在 `len+num > cap` 时调用 `growslice`。它的核心是一段"两段式"增长策略，Go 1.22 起被抽成了独立函数 `nextslicecap`：

```go
// runtime/slice.go（Go 1.22+，函数名 nextslicecap）
func nextslicecap(newLen, oldCap int) int {
	newcap := oldCap
	doublecap := newcap + newcap
	if newLen > doublecap {
		return newLen          // ① 一次性要得太多，直接给足
	}
	const threshold = 256
	if oldCap < threshold {
		return doublecap       // ② 小切片：翻倍（老策略）
	}
	for {
		// ③ 大切片：每次增加约 1/4，从 2x 平滑过渡到 1.25x
		newcap += (newcap + 3*threshold) / 4
		if uint(newcap) >= uint(newLen) { // 一次比较同时处理溢出
			break
		}
	}
	if newcap <= 0 {           // 溢出兜底
		return newLen
	}
	return newcap
}
```

为什么要"从 2 倍过渡到 1.25 倍"？因为切片越大，翻倍浪费的绝对内存越多（1GB → 2GB 的瞬间峰值），而 1.25 倍会让拷贝次数变多。**256 是官方的折中点**：小切片追求"少拷贝"，大切片追求"少浪费"。

### 图四：一次 `append` 触发的完整决策链

```
append(s, x...)  →  len+num > cap ?
        │
        ▼
newcap = nextslicecap(newLen, oldCap)
        │
        ├─ newLen > 2*oldCap ──────► newcap = newLen
        ├─ oldCap < 256 ───────────► newcap = 2*oldCap
        └─ 否则 ───────────────────► newcap 循环 +(newcap+768)/4，直到 ≥ newLen
        │
        ▼
 capmem = roundupsize(newcap * sizeof(T))   ← 向上取整到分配器的 size class（第 15 章）
 newcap = capmem / sizeof(T)                ← ★ 最终 cap 通常比你算的更大
        │
        ▼
 mallocgc(capmem) → memmove 老数据 → 写屏障 → 返回新 slice header
        │
        ▼
 老数组失去这个 slice 的引用 → 交给 GC（但可能还有别的 slice 指着它！）
```

第三步的 `roundupsize` 是"为什么 cap 序列看起来毫无规律"的元凶——内存分配器只提供固定档位的 size class（8/16/32/48/64/80/96/112/128…），所以 cap 会被向上取整：

```go
package main

import "fmt"

func main() {
	var b []byte
	prev := 0
	fmt.Print("[]byte  cap: ")
	for i := 0; i < 400; i++ { // 只在 cap 变化时打印
		b = append(b, 'x')
		if cap(b) != prev {
			prev = cap(b)
			fmt.Print(prev, " ")
		}
	}
	fmt.Println()
	// []byte  cap: 8 16 32 64 128 256 512

	var n []int64
	prev = 0
	fmt.Print("[]int64 cap: ")
	for i := 0; i < 40; i++ {
		n = append(n, 1)
		if cap(n) != prev {
			prev = cap(n)
			fmt.Print(prev, " ")
		}
	}
	fmt.Println()
	// []int64 cap: 1 2 4 8 16 32
}
```

| 元素类型 | 从 nil 连续 append 时的 cap 变化点 | 观察 |
| --- | --- | --- |
| `[]byte`（1B） | 8, 16, 32, 64, 128, 256, 512, 896, 1408, … | 最小 size class 是 8，所以一个字节也要 8 字节空间 |
| `[]int64`（8B） | 1, 2, 4, 8, 16, 32, … | 元素大小正好等于档位，扩容"看起来最规则" |
| 超过 256 元素后 | 每次约 ×1.25 再向上取整 | 序列变得不规则（512 → 896 → 1408），**别去背具体数字** |

> ⚠️ 这段代码在你自己机器上跑出来的数字**可能不同**：Go 版本、平台（`goarch`）、size class 表、以及编译器对 `append` 的优化都会影响。**记住趋势，不要记数字。**

### 11.3.3 三索引切片 `s[a:b:c]`

普通切片 `s[a:b]` 的 `cap` 是"从 a 到底"，这给了 `append` 污染邻居的机会。三索引切片把 `cap` 也锁死：

```go
// s[a:b:c] → len = b-a, cap = c-a
// 约束：0 <= a <= b <= c <= cap(s)，否则 panic
s2 := s[0:3:3]   // len=3, cap=3 → append 必然重新分配 → 不可能污染 s[3:]
```

**内存图对比**：

```
data       [0][1][2][3][4][5]   cap=6

a := data[:3]       header{ptr→0, len=3, cap=6}
    a = append(a, 9)   → 就地写 data[3] ⚠️

b := data[:3:3]     header{ptr→0, len=3, cap=3}
    b = append(b, 9)   → cap 不够 → 分配新数组 → data 完全不受影响 ✅
```

用法原则：**函数返回一个"子切片"给外部使用时，永远用三索引把 cap 夹住**。这是库作者的防御性写法，也是 Go 里唯一能"冻结" cap 的手段。

### 11.3.4 数组：三个必须记住的事实

```go
var a [3]int             // 值类型，24 字节，全零
b := a                   // 整块拷贝！改 b[0] 不影响 a
c := [...]int{1, 2, 3}   // 长度由字面量推导
d := [5]int{0: 1, 4: 9}  // 指定下标初始化 → [1 0 0 0 9]

// 数组可比较（元素可比较时），可作 map key
m := map[[2]string]int{{"a", "b"}: 1}

// 传大数组给函数 = 拷贝全部内容（这就是为什么需要 slice）
func sum(a [1 << 20]int64) int64 // ❌ 8MB 拷贝
func sum(a []int64) int64        // ✅ 24 字节 header
```

---

## 11.4 Java 对照：`ArrayList` 与 slice 的本质差异

| 维度 | `ArrayList<T>` | Go `[]T` | 会咬人的地方 |
| --- | --- | --- | --- |
| 内存结构 | 对象（16 字节对象头）+ `Object[] elementData` + `int size` | 24 字节 header + 裸数组，**没有对象头** | slice 变量本身没有"对象身份"，Java 里那种 `synchronized(list)` 按对象加锁的玩法在 Go 里不存在（Go 的锁不依附对象） |
| 元素存储 | 存**引用**（`Object[]`），元素在堆上散落 | 存**值**，连续排列 | Go 遍历 `[]struct` 是连续内存（缓存友好）；Java 是随机跳指针。但 Go 存大 struct 时 `append` 拷贝成本高 |
| 扩容 | `oldCapacity + (oldCapacity >> 1)`，**固定 1.5x** | 2x → 1.25x 过渡 + size class 取整 | 别把 1.5x 的经验带过来 |
| 视图 | `subList()` 返回**视图**，结构修改抛 `ConcurrentModificationException` / `UnsupportedOperationException` | `s[a:b]` 就是**新 header 共享数组**，毫无保护 | 这是最高频 bug 来源，没有之一 |
| `null` 元素 | 可以放 null | `[]*T` 可以放 nil，`[]T` 不能"什么都没有" | 见第 9 章 |
| 长度语义 | `size()` 是唯一长度 | `len` 与 `cap` 分离 | `cap` 是"还能塞多少而不搬家" |

**Go 为什么这样选？** `ArrayList` 是"对象 + 数组"两层间接，好处是能在数组上做视图校验、能触发 fail-fast；slice 是"一个裸 header"，好处是**零开销、可控内存布局、能直接映射到 C 数组/网络缓冲/`syscall`**。代价全部转移给你：**没有视图保护，共享是默认行为**。

> 💡 换个角度：slice 的设计目标不是"安全的集合"，而是"**一块内存的窗口**"。`bytes.Buffer`、`bufio`、`net`、`unsafe`、序列化库全靠"窗口"这个语义做零拷贝。要安全集合，用 `slices` 包的函数 + 明确的所有权约定。

---

## 11.5 工程落地：预分配、复用、清理

### 11.5.1 预分配 cap（最便宜的优化）

```go
// ❌ 2 万次 append → 十几次扩容 + 十几次 memmove + 十几次 GC 压力
var out []Result
for _, item := range items {
	out = append(out, convert(item))
}

// ✅ 一次分配到位（已知规模时）
out := make([]Result, 0, len(items))
for _, item := range items {
	out = append(out, convert(item))
}

// ✅ 更激进：按索引写，连 append 的 len 检查都省了（微优化，先测量）
out := make([]Result, len(items))
for i, item := range items {
	out[i] = convert(item)
}
```

`make([]T, n)` 与 `make([]T, 0, n)` 的区别是**语义性的**：前者有 n 个零值元素（`append` 会接在后面，写错就是 bug），后者 len 为 0（`append` 从索引 0 开始填）。这两个混用是"结果前面多了一堆零值"的经典原因。

### 11.5.2 复用缓冲：`s = s[:0]`

```go
buf := make([]byte, 0, 4096)
for {
	buf = buf[:0]            // 重置 len，保留 cap → 不分配
	buf = append(buf, line...)
	process(buf)
}
```

注意 `s = s[:0]` **不清内存**：底层数组里还留着旧数据的引用。如果元素是 `[]byte` 或指针、而且这块 buffer 会活很久，要显式 `clear(s)`（Go 1.21+ 内置函数）避免"看不见的引用把大对象钉住"。

### 11.5.3 切片泄漏的三种修法

```go
// ❌ 泄漏：10 字节的返回值钉住 200MB
func head(buf []byte) []byte { return buf[:10] }

// ✅ 修法一：拷贝出来（最简单，绝大多数场景的最优解）
func head(buf []byte) []byte { return slices.Clone(buf[:10]) }

// ✅ 修法二：三索引 + 明确文档（调用方保证不 append，但仍钉住内存）
func head(buf []byte) []byte { return buf[:10:10] }

// ✅ 修法三：只保留需要的数据（最省内存，多一次分配）
out := make([]byte, 10)
copy(out, buf[:10])
```

`head(buf)` 修法一用的是 `slices.Clone`（Go 1.21+）。同族的还有 `slices.Grow`（预留 cap）、`slices.Concat`（1.22）。

删除元素也有同样的泄漏陷阱：

```go
// ❌ 移位删除：末尾那个槽位仍然持有被删元素的引用（超出 len 但仍在底层数组里）
s = append(s[:i], s[i+1:]...)

// ✅ 用 slices.Delete：它会把尾部残留元素置零，避免底层数组继续持有引用
s = slices.Delete(s, i, i+1)
```

### 11.5.4 Agent 场景里的两个具体落点

- **流式 token 拼装**：SSE 每一帧 `append` 到 buffer 里，务必 `make([]byte, 0, 4096)` 起步；单次对话的 `content` 建议超过阈值后转 `strings.Builder`（第 13 章）。
- **批量 embedding 分批**：`batch := items[i:min(i+n, len(items))]` 之后**只读不改**；如果需要 `append`，用三索引 `items[i:end:end]`，否则你的批次处理和下一批的数据会互相污染（就是 11.1 的事故）。

---

## 11.6 踩坑清单（CR 可直接抄）

| # | 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- | --- |
| 1 | 改了 A 切片，B 切片的数据也变了 | `s[a:b]` 共享底层数组 | 需要独立副本就 `slices.Clone`；对外返回用三索引 `s[a:b:b]` | 11.1 |
| 2 | 函数内 `append` 后调用方看不到新元素 | slice header 是值，`len` 没传回去 | `s = append(s, x)` 并**返回**新 slice | 11.2 |
| 3 | 内存莫名不释放、RSS 只涨不跌 | 小切片钉住大底层数组 | `slices.Clone` / `copy` | 11.1 |
| 4 | 结果前面多出一堆零值 | `make([]T, n)` 后又 `append` | 用 `make([]T, 0, n)` 或按下标赋值 | 11.5 |
| 5 | 对象已被删除却还被 GC 认为是活的 | 手写移位删除，尾部残留引用 | `slices.Delete` / `clear(s)` | 11.5 |
| 6 | `append` 后的容量大得离谱 | `roundupsize` 向上取整到 size class | 正常现象；按规模预分配 cap 更稳 | 11.3 |
| 7 | 并发 `append` 同一个 slice 丢数据 | slice 无并发保护 | 加锁 / 每 goroutine 独立 slice 再合并 | 第 25 章 |
| 8 | 传大数组进函数后 CPU 飙高 | 数组是值，整块拷贝 | 传切片或 `*[N]T` | 11.3 |
| 9 | `nil` slice 与空 slice 混用导致 JSON 出现 `null` | `nil` 序列化成 `null` | 初始化成 `[]T{}` 或前端容错 | 第 31 章 |
| 10 | `s[i:j]` panic: slice bounds out of range | `i/j` 越了 `len` 或 `cap` | 边界检查 + 明确 `len`/`cap` 语义 | 11.3 |

---

## 11.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| slice header | `{array unsafe.Pointer, len int, cap int}`，24 字节（64 位），按值传递 |
| `len` vs `cap` | `len` 管越界，`cap` 管"还能塞多少不搬家" |
| `s[a:b]` | 新 header 共享底层数组，`cap` 从 a 数到底 |
| `s[a:b:c]` | len=b-a、cap=c-a，**冻结 cap**，防污染的标准手段 |
| 扩容公式 | >2 倍直接给足；<256 翻倍；≥256 每次 `+ (cap+768)/4` → 渐近 1.25x |
| size class | 最终 cap 被 `roundupsize` 向上取整，所以数字看着不规则 |
| `append` 分家 | `len<cap` 就地写（共享！），否则新数组 + memmove |
| 切片泄漏 | `big[:n]` 钉住整块数组 → `slices.Clone` |
| 数组 | 值类型，整块拷贝，可比较、可作 map key |
| `make([]T,n)` vs `(0,n)` | 前者有 n 个零值元素，后者 len 为 0 |
| 常用工具 | `slices.Clone/Grow/Delete/Concat`（1.21+）、`clear`（1.21+） |

---

## 11.8 自检题

1. **`a := data[:3]` 之后 `a = append(a, 9)`，什么时候会改到 `data[3]`？怎么彻底避免？**
   > 要点：当 `cap(a) > len(a)`（即 `cap(data) > 3`）时就地写入，会改到 `data[3]`；当 `cap(a) == len(a)` 时触发 `growslice`，分配新数组，`data` 不受影响。彻底避免的方式是切出来时就夹住 cap：`a := data[:3:3]`，让 `cap(a) == 3`，`append` 必然重新分配。

2. **为什么 `append` 的返回值必须接？`append(s, x)` 不接会怎样？**
   > 要点：`append` 可能返回一个**全新的 header**（新 array 指针 + 新 cap），而 s 自己是值。不接返回值 = 丢弃新 header，于是"元素没加上/加了但看不到"。极端情况下数据其实已经写进了老数组（未扩容时），于是出现"有时生效有时不生效"的幽灵 bug。

3. **Go 1.18 把扩容从"小于 1024 翻倍、之后 1.25 倍"改成了"小于 256 翻倍、之后平滑过渡到 1.25 倍"。这个改动在优化什么？**
   > 要点：老策略的 1024 阈值让中等规模切片（几百到一千元素）负担过重：翻倍意味着 2 倍内存峰值 + 大块 memmove。新公式 `newcap += (newcap + 3*256)/4` 让增长率随规模**连续**下降，既避免小切片频繁拷贝，也避免中等切片的瞬时内存翻倍；边界从一个硬台阶变成一条平滑曲线。代价是公式变复杂、cap 序列更难预测——所以 Go 官方从不在文档里承诺具体 cap。

---

## 11.9 延伸阅读

- `$GOROOT/src/runtime/slice.go`：`slice` 结构、`makeslice`、`growslice`、`nextslicecap`
- `$GOROOT/src/runtime/msize.go`：`roundupsize`（早期版本在 `malloc.go`）、`$GOROOT/src/runtime/sizeclasses.go`
- `$GOROOT/src/slices/slices.go`：`Clone`/`Grow`/`Delete`/`Concat`/`Sort` 的语义与"置零"注释
- `$GOROOT/src/cmd/compile/internal/ssagen`：`append` 的内联与边界检查消除
- Go 官方博客 *Slices: usage and internals*、*Arrays, slices (and strings): The mechanics of 'append'*
- `go doc slices`、`go doc builtin.append`、`go doc builtin.clear`
- 下一章：[12. map 内部结构与 Go 1.24 Swiss Table](12-map-internals.md)

---

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：第 10 章 [组合优于继承：插件化与注册表设计](10-composition-plugin-registry.md)
- 相关：第 1 章 1.5 节（"slice 是半引用"的直觉模型）、第 13 章（string / `[]byte` 零拷贝）、第 14 章（大 struct 的拷贝成本与逃逸）、第 15 章（size class 与 `roundupsize`）、第 30 章（预分配与对象复用）
- 下一章：[12. map 内部结构与 Go 1.24 Swiss Table](12-map-internals.md)
