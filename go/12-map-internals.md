# 12. map 内部结构与 Go 1.24 Swiss Table

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 数据结构与内存 ｜ **编号**：12 / 54
> **本章定位**：`map` 是 Java 程序员最先「以为懂了」、最晚「真懂了」的类型。这一章把 `hmap` / `bmap` 摊开，再讲到 Go 1.24 换成 Swiss Table 之后到底变了什么，最后落到「CR 时能一眼看出这段 map 用法会不会炸」。
> **读法**：12.2 的脑图必须记住；12.3 的字段名和常量值不用背，但要能顺着 `mapassign` 讲出「写一个键发生了什么」；12.5 可以直接抄进项目规范。

---

## 12.1 引子：一次「顺序变了」的线上 bug

凌晨两点，你的 Go 服务对完账，运维报：**同一批数据，两个副本算出来的 prompt 前缀不一样**，导致 prompt cache 命中率从 82% 掉到 3%，成本翻倍。

代码长这样：

```go
// 把工具列表拼成 prompt 前缀。tools 是一个 map，启动时注册，运行期只读。
func buildPrefix(tools map[string]Tool) string {
	var sb strings.Builder
	for name, t := range tools {
		fmt.Fprintf(&sb, "%s: %s\n", name, t.Desc)
	}
	return sb.String()
}
```

在你的 Java 世界观里这段代码是"稳定"的：`HashMap` 虽不保证顺序，但同一份数据、不改结构时**遍历顺序是不变的**。而 Go 的 `map` 遍历**每次都不同**，于是：每次重启前缀字节序都变；prompt cache 要求前缀完全一致才能命中，命中率崩到 3%；更隐蔽的是单元测试会以某个概率通过，让你以为一切正常。

修法只有一行：`for _, name := range slices.Sorted(maps.Keys(tools))`。但这行代码背后藏着四个你一定会再遇到的问题：

1. 为什么 Go 非要随机？
2. 一个 `map[string]int` 在内存里到底长什么样，`make(map[string]int, 1000)` 预分配了什么？
3. 为什么 `delete` 掉 100 万个键，内存不降？
4. 为什么并发写 map 是 **fatal error**，连 `recover` 都救不回来？

> 🎯 这一章的主线：**Java 的 `HashMap` 是「链表 + 红黑树」的教科书实现，Go 的 `map` 是「桶 + tophash + 渐进搬迁」的工程实现，1.24 之后又换成「Swiss Table 开放定址」。三次演化，每一次都在回答同一个问题：怎么让平均查找代价接近 1 次内存访问。**

---

## 12.2 直觉模型：它不是一个「哈希表」，是「哈希表的三种形态」

先给你一张能和 Java 对上号的脑图（以 Go 1.23 及之前的实现为主线，1.24 的差异在 12.3.6 单独讲）：

```
Java HashMap                       Go map（≤1.23）
┌──────────────────────┐           ┌──────────────────────────┐
│ table: Node[] (桶数组)│           │ buckets: bmap[] (2^B 个) │
│  ┌───┐               │           │  ┌────────────────────┐  │
│  │ ──┼──► Node ──► Node  链表    │  │ bucket: 8 个槽      │  │
│  └───┘   (≥8 项树化成  │           │  │ tophash[8] + k×8 +  │  │
│          红黑树)       │           │  │ v×8 + overflow ─────┼──┼─► 溢出桶 ──► …
│  负载因子 0.75         │           │  └────────────────────┘  │
│  一次 rehash 搬完      │           │  负载因子 6.5（81%）     │
└──────────────────────┘           │  每次读写只搬一点点      │
                                    └──────────────────────────┘
```

三个必须建立起来的直觉：

**① 桶的粒度不同。** Java 一个桶装 1 个 entry（冲突了就挂链表）；Go 一个桶装 **8 个**（`bucketCnt = 8`）。这不是为了省内存，而是为了**把 8 个键的比较合并成一次缓存行友好的扫描**——tophash 那 8 个字节刚好塞进一个机器字。

**② Go 没有「树化」。** Java 8 在链表长度 ≥ 8 时退化成红黑树防 hash 攻击；Go 的解法是 `hmap.hash0` 作为**每个 map 独立的随机种子**（`h.hash0 = uint32(rand())`），攻击者构造不出稳定的碰撞集合。代价是：测试里不能"猜顺序"。

**③ 扩容方式不同。** Java 的 `resize()` 是**一次性搬完**，代价是一次长尾；Go 是**渐进式扩容（incremental evacuation）**，每次读写顺手搬一两个桶。这条思路贯穿整个 runtime。

| 维度 | Java `HashMap` | Go `map`（≤1.23） | Go `map`（1.24+） |
| --- | --- | --- | --- |
| 冲突解决 | 链地址 + 红黑树 | 链地址（8 槽桶 + 溢出桶） | 开放定址（Swiss Table） |
| 槽位/桶 | 1 | 8 | 8（一个 group） |
| 负载因子 | 0.75 | **6.5 / 8 = 81%** | **7 / 8 = 87.5%** |
| 扩容 | 一次 rehash 搬完 | 渐进式搬迁 | 按 table 为单位拆表 |
| 遍历顺序 | 不保证但稳定 | **每次随机** | **每次随机** |
| 并发写 | 静默损坏 / JDK7 死循环 | `fatal error` 直接退出 | `fatal error` 直接退出 |
| 键的类型要求 | `equals`/`hashCode` | 必须**可比较**（comparable） | 同左 |

> 💡 **一句话记住 Go map**：**「2^B 个桶，每桶 8 槽，槽上先比 tophash 再比 key，装太满就渐进式翻倍。」**

---

## 12.3 底层实现

### 12.3.1 `hmap`：map 值本身是个指针，指向它

Go 的 `map` 类型在语言层面是"引用语义"，但 `map` 变量的值是**一个指向 `hmap` 的指针**（不是像 slice 那样带 len/cap 的 header）。源码（`runtime/map.go`，Go 1.21~1.23；1.24 起这段代码迁到 `runtime/map_noswiss.go`，字段名保持一致）：

```go
// runtime/map.go
type hmap struct {
	count      int            // 活跃元素个数，len() 直接读它，所以必须放第一个字段
	flags      uint8          // 位标记：iterator / hashWriting / sameSizeGrow
	B          uint8          // buckets 数量 = 2^B
	noverflow  uint16         // 溢出桶数量的近似值
	hash0      uint32         // 哈希种子（随机！遍历随机性的第一层来源）

	buckets    unsafe.Pointer // 指向 2^B 个 bucket 的数组；count==0 时可能为 nil
	oldbuckets unsafe.Pointer // 扩容期间指向旧数组，大小是新的一半
	nevacuate  uintptr        // 渐进搬迁进度：已经搬完的桶数
	extra      *mapextra      // 溢出桶的缓存（nextOverflow / oldoverflow）
}
```

注意 `count` 放在第一个字段：`len(m)` 被编译成一次 `(*hmap).count` 读取，零成本。`bmap` 在源码里只声明了 tophash 部分——后面 8 组 key/value 和溢出指针是**编译器按 key/value 类型动态生成的**（`cmd/compile/internal/reflectdata` 的 `MapBucketType`）：

```go
// runtime/map.go
type bmap struct {
	// 每个槽的 hash 高 8 位。真实布局由编译器生成：
	// tophash [8]uint8; keys [8]keyType; elems [8]elemType; overflow *bmap
	tophash [abi.MapBucketCount]uint8
}
```

内存布局（`map[string]int64`，amd64，`bucketCnt = 8`）：

```
                  一个 bucket（bmap）= 208 字节
 偏移 0                                   8
      ┌───┬───┬───┬───┬───┬───┬───┬───┐
tophash│h0 │h1 │h2 │h3 │h4 │h5 │h6 │h7 │   1B×8：hash 的高 8 位
      ├───┴───┴───┴───┴───┴───┴───┴───┤
keys   │ k0        k1        ...   k7  │   string header，16B×8 = 128B
      ├───────────────────────────────┤
elems  │ v0        v1        ...   v7  │   int64，8B×8 = 64B
      ├───────────────────────────────┤
       │ overflow *bmap                │   8B：溢出桶指针
      └───────────────────────────────┘
```

**为什么要 tophash 而不是直接比 key？** 因为 key 可能是 128 字节的大 struct。先比 1 字节的 hash 高位，绝大多数槽一次比较就被否掉；Python 的 dict、Google 的 Abseil、Rust 的 hashbrown 全用这招。

### 12.3.2 tophash 的五个魔术值

`tophash` 不存"hash 的高 8 位"这么简单，低位几个值被征用为状态机：

```go
// runtime/map.go
const (
	emptyRest      = 0 // 本槽为空，且后续槽（含溢出桶）也全空 —— 遍历可以提前终止
	emptyOne       = 1 // 本槽为空，但后面还有可能有值
	evacuatedX     = 2 // 搬迁过：去了新数组的前半（原桶号）
	evacuatedY     = 3 // 搬迁过：去了新数组的后半（原桶号 + 2^B）
	evacuatedEmpty = 4 // 搬迁过，且本来就是个空槽
	minTopHash     = 5 // 真正的 hash 高位必须 >= 5，小于就加上 minTopHash 偏移
)
```

这张表在 12.3.4（扩容）和 12.3.5（遍历）里都会用到。`emptyRest` 特别妙：插入总是从左往右填，一旦遇到 `emptyRest` 就能判定后面全空，遍历可以提前终止。

### 12.3.3 写一个键：`mapassign` 的完整路径

```go
// runtime/map.go 精简版，保留关键行
func mapassign(t *maptype, h *hmap, key unsafe.Pointer) unsafe.Pointer {
	if h == nil {
		panic(plainError("assignment to entry in nil map")) // ← nil map 写入直接 panic
	}
	if h.flags&hashWriting != 0 {
		fatal("concurrent map writes")                      // ← 并发写检测（不可 recover）
	}
	hash := t.Hasher(key, uintptr(h.hash0))                 // 哈希 + hash0 种子
	h.flags ^= hashWriting

	if h.buckets == nil {
		h.buckets = newobject(t.Bucket)                     // 懒分配第一个桶
	}
again:
	bucket := hash & bucketMask(h.B)                        // 用低位选桶
	if h.growing() {
		growWork(t, h, bucket)                              // ★ 渐进式扩容：顺手搬 1~2 个桶
	}
	b := (*bmap)(add(h.buckets, bucket*uintptr(t.BucketSize)))
	top := tophash(hash)                                    // hash 高 8 位
	// …遍历 8 个槽 + 溢出桶链：先比 tophash，命中再比 key…
	// 找不到就插空槽；没空槽就 newoverflow() 挂溢出桶
}
```

三个要点：**选桶用 hash 低位**（`hash & bucketMask(h.B)`），**桶内区分用 hash 高位**（tophash），两者正交；`t.Hasher` 是编译器生成的函数指针（`string` 走 `runtime.memhash` 系列汇编，整数走 `memhash64` 特化，struct 按字段拼装）；**写操作会"顺手"推进扩容**——把 O(n) 的搬迁平摊到后续每次读写。

### 12.3.4 负载因子 6.5 与渐进式扩容

```go
// runtime/map.go
const (
	loadFactorNum = 13 // 平均每桶 6.5 个元素就扩容 → 13/2
	loadFactorDen = 2
	bucketCnt     = abi.MapBucketCount // 8
)

func overLoadFactor(count int, B uint8) bool {
	return count > bucketCnt && uint32(count) > loadFactorNum*(bucketShift(B)/loadFactorDen)
}
```

**为什么是 6.5 而不是 Java 的 0.75（即 6/8）？** 因为两者的"冲突代价"不同：Java 桶满了可以挂链表（代价是缓存不友好但可控），Go 桶满了就要分配溢出桶（一次 `mallocgc`，贵）。所以 Go 宁可让桶更满一点、少分配溢出桶，把平均查找次数往上顶——这是**用 CPU 换内存分配次数**，典型的 Go 取舍。

扩容有两个触发条件（`runtime/map.go` 的 `hashGrow` 调用点）：

| 触发条件 | 判断函数 | 行为 |
| --- | --- | --- |
| 元素太多 | `overLoadFactor(count, B)` | 桶数翻倍（`B++`），真正变大的扩容 |
| 溢出桶太多 | `tooManyOverflowBuckets(noverflow, B)`，即 `noverflow >= 2^B` | **等量扩容**（`sameSizeGrow`）：桶数不变，重新整理，把碎片化的溢出链挤回主桶 |

第二种是 Go 独有的：大量 `insert + delete` 会留下一堆空洞（`emptyOne`），元素数没涨但溢出桶爆了，这时不需要变大，只需要"压实"。这也解释了一个经典现象：**一个长期被反复增删的 map，内存会涨到某个上限就不再涨，但也不会降。**

扩容过程：

```
扩容前：B=2（4 个桶）           扩容后：B=3（8 个桶）
                              oldbuckets 保留，nevacuate=0 开始搬
 old: [b0][b1][b2][b3]
                                   每次都搬 1~2 个桶（growWork）
 new: [B0][B1][B2][B3][B4][B5][B6][B7]
        ▲        ▲
        └ 原 b0 的元素按 hash 新的一位拆到 B0 或 B4
          （这就是 evacuatedX / evacuatedY 的由来）
```

搬运一个桶时，桶内 8 个槽的 tophash 会被改写成 `evacuatedX`（去了前半）或 `evacuatedY`（去了后半）。所以**扩容期间两个数组同时存在**，每次读写得先看 `h.growing()` 再决定去哪个数组找——这就是那句 `if h.growing() { growWork(...) }` 的全部意义。

> ⚠️ **代价**：扩容期间内存占用约为峰值的 1.5 倍（新数组 + 未搬完的旧数组）。一个 100 万元素的 `map[string]string` 从 2^17 桶扩到 2^18，瞬时多占几十 MB。这也是为什么 `make(map[K]V, hint)` 预分配很有价值：**已知规模就预分配，一次到位，不搬**。

### 12.3.5 遍历随机：`mapiterinit` 里的两行 `rand()`

```go
// runtime/map.go
func mapiterinit(t *maptype, h *hmap, it *hiter) {
	// …
	r := uintptr(rand())
	it.startBucket = r & bucketMask(h.B)        // 随机起始桶
	it.offset = uint8(r >> h.B & (bucketCnt - 1)) // 桶内随机起始槽
	it.bucket = it.startBucket
	it.wrapped = false                          // 搬到一圈结束就置 true
}
```

`hiter` 里还有 `checkBucket`——扩容期间遍历要跳过已搬走/未搬走的桶，保证每个键**恰好被访问一次**。这两行 `rand()` 换来两件事：**防止程序依赖顺序**（依赖顺序是必然发生的 bug，让它在第一时间暴露）、**配合 `hash0` 防 hash 洪泛**。代价是：**你永远不能靠 map 顺序做任何确定性的事情**（签名、prompt 前缀、幂等 key 拼接、diff）。

```go
// 可运行：观察随机性，并给出唯一正确解法
package main

import (
	"fmt"
	"maps"
	"slices"
)

func main() {
	m := map[string]int{"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}
	for i := 0; i < 3; i++ {
		keys := maps.Keys(m)                 // 迭代器（Go 1.23+）
		fmt.Println(slices.Collect(keys))    // 每次顺序都可能不同
	}
	// 需要确定性输出时：先排序
	ordered := slices.Sorted(maps.Keys(m))  // Go 1.23+
	fmt.Println(ordered)                     // 永远 [a b c d e]
}
```

### 12.3.6 并发写为什么是 fatal，而不是 panic

`mapassign` / `mapaccess1` 里各有一道检查：

```go
if h.flags&hashWriting != 0 {
	fatal("concurrent map writes")            // 写-写
}
// mapaccess1 里：
if h.flags&hashWriting != 0 {
	fatal("concurrent map read and map write") // 读-写
}
```

`fatal()` 最终走 `throw()`：打印栈、`exit(2)`，**不经过 defer，`recover()` 抓不到**。设计动机很直白：

- **检测而不是保护**。给 map 加锁会让所有单 goroutine 的 map 变慢，而 Go 的假说是"绝大多数 map 是单 goroutine 本地状态"。
- **fatal 而不是 panic**。因为并发写会导致 `hmap` 处于**内存不安全**的状态（桶数组指针可能正在被换、`count` 可能已错），继续跑下去可能是静默数据损坏 —— 这在生产里比崩溃可怕一百倍。
- 那些用 `flags & hashWriting` 做的是**尽力检测**（不是 100% 能抓到），所以偶尔会以 "unexpected fault address" 的形式炸。**能抓到就赚，抓不到也别指望它兜底。**

```go
// 🩸 复现：这段代码必然 fatal，且 recover 无效
package main

import "fmt"

func main() {
	defer func() {
		if r := recover(); r != nil {
			fmt.Println("不会执行到这里") // fatal error 不走 defer
		}
	}()
	m := make(map[int]int)
	for i := 0; i < 8; i++ {
		go func() {
			for j := 0; ; j++ {
				m[j] = j // fatal error: concurrent map writes
			}
		}()
	}
	select {}
}
```

正确做法见 12.6 的踩坑表；实现见下面这个可直接跑的分片锁 map（分片数量取 2 的幂，用 `sync.RWMutex` 保护每片，写少读多时比 `sync.Map` 更好预测）：

```go
package main

import (
	"fmt"
	"hash/maphash"
	"sync"
)

const shards = 64 // 必须是 2 的幂

type shard[V any] struct {
	mu sync.RWMutex
	m  map[string]V
}

type ShardedMap[V any] struct {
	s    [shards]shard[V]
	seed maphash.Seed // 用随机种子把 key 打散，避免分片倾斜
}

func NewShardedMap[V any]() *ShardedMap[V] {
	sm := &ShardedMap[V]{seed: maphash.MakeSeed()}
	for i := range sm.s {
		sm.s[i].m = make(map[string]V)
	}
	return sm
}

func (sm *ShardedMap[V]) idx(key string) uint64 {
	var h maphash.Hash
	h.SetSeed(sm.seed)
	h.WriteString(key)
	return h.Sum64() & (shards - 1)
}

func (sm *ShardedMap[V]) Set(key string, v V) {
	sh := &sm.s[sm.idx(key)]
	sh.mu.Lock()
	sh.m[key] = v
	sh.mu.Unlock()
}

func (sm *ShardedMap[V]) Get(key string) (V, bool) {
	sh := &sm.s[sm.idx(key)]
	sh.mu.RLock()
	v, ok := sh.m[key]
	sh.mu.RUnlock()
	return v, ok
}

func main() {
	var wg sync.WaitGroup
	sm := NewShardedMap[int]()
	for i := 0; i < 1000; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			sm.Set(fmt.Sprintf("k%d", i), i)
		}(i)
	}
	wg.Wait()
	v, _ := sm.Get("k42")
	fmt.Println(v) // 42，且 go run -race 全绿
}
```

### 12.3.7 `delete` 为什么不释放内存

`mapdelete` 的动作只有三步（源码同名函数）：把槽的 tophash 置成 `emptyOne`、把 key/value 清零（让 GC 能回收 key/value 内部的指针）、`h.count--`。

**它不会**：缩容、释放溢出桶、释放桶数组。原因：

- Go 的 map **从不缩容**。桶数组一旦分配，就跟着 `hmap` 活到 map 被回收（`h = nil` 或被 GC）。
- 唯一一次"整理"是前面说的 `sameSizeGrow`，而它**只能被写操作触发**。所以"先插 100 万再全删"之后，即使再写一次，也只是压实，不会真的变小。

```go
// 可运行：delete 不缩容的量化观察
package main

import (
	"fmt"
	"runtime"
)

func heapAlloc() uint64 {
	var ms runtime.MemStats
	runtime.GC()
	runtime.ReadMemStats(&ms)
	return ms.HeapAlloc
}

func main() {
	const n = 1 << 20
	base := heapAlloc()

	m := make(map[int]int, n)
	for i := 0; i < n; i++ {
		m[i] = i
	}
	full := heapAlloc()

	for i := 0; i < n; i++ {
		delete(m, i)
	}
	empty := heapAlloc()

	fmt.Printf("base=%dKB full=+%dKB afterDelete=+%dKB len=%d\n",
		base/1024, (full-base)/1024, (empty-base)/1024, len(m))
	// 典型输出：删除 100 万项后，桶数组那几十 MB 一分钱没还
	runtime.KeepAlive(m)
}
```

> 🩸 **事故形态**：一个"每天全量刷新"的缓存 map，白天涨到 200 万项、夜里删空、第二天再涨。因为删空后桶数组不缩，第二天直接从 2^21 个桶开始写——**内存曲线是单调上升的台阶**，最后 OOM。
> 解法：① 定期整体替换 `m = make(map[K]V, hint)`（旧的交给 GC）；② 用带淘汰的 `hashicorp/golang-lru/v2` 之类的结构；③ 加容量上限。

### 12.3.8 Go 1.24：Swiss Table 换掉了链地址法

Go 1.24 起，内置 `map` 默认换成基于 **Swiss Table**（Google Abseil 的开放定址哈希表）的实现。代码位置：**兼容的老实现在 `runtime/map_noswiss.go`，新实现在 `runtime/map_swiss.go`，核心数据结构在 `internal/runtime/maps`（`map.go` / `group.go` / `table.go` / `dir.go`）。** 具体文件与字段在版本间仍有微调，**以你本地 `$GOROOT/src` 为准**。

三个关键变化：

**① 桶 → group，槽位还是 8 个，但多了一个 "control word"。**

```
一个 group（map[int64]int64，8 槽，开放定址）

ctrl:  [h2][FF][h2][80][80][80][80][80]     ← 每槽 1 字节，共 8 字节 = 一个机器字
        │   │
        │   └─ 0xFF = ctrlDeleted（墓碑，可复用）
        └───── h2 = hash 的高位截断，用于一次机器字比较筛出候选槽
keys:  [k0][k1][k2][k3][k4][k5][k6][k7]     8 × 8B
elems: [v0][v1][v2][v3][v4][v5][v6][v7]     8 × 8B

     0x80 = ctrlEmpty（空槽）
```

`ctrlGroup` 是一个 `uint64`，配合两个常数做 **SIMD-within-a-register** 位运算：

```go
// internal/runtime/maps/group.go（示意，变量名以本地源码为准）
const (
	lsbs = 0x0101010101010101
	msbs = 0x8080808080808080
)

// matchH2 一次找出 8 个槽里所有 ctrl 等于 h2 的槽
func (g *ctrlGroup) matchH2(h2 uint8) bitset {
	v := uint64(g.load())
	m := v ^ (uint64(h2) * lsbs) // 每字节做一次无借位比较
	m &= msbs                    // 只留最高位
	return bitset(m)
}
```

这就是 Swiss Table 的杀手锏：**判断"这 8 个槽里有没有我要的"只需要 3 条指令**，而且整个 ctrl 数组只有 8 字节，必在同一缓存行里。链地址法要为每个桶读 tophash 数组、再跳指针找溢出桶，每次跳转都可能 cache miss。

**② 三层结构：小 map / 单 table / 目录。**

```
Map{ used, seed, dirPtr, dirLen, globalDepth, globalShift, writing }

 dirLen == 0  →  dirPtr ──► group            （≤ 8 项：整个 map 就一次分配）
 dirLen == 1  →  dirPtr ──► table ──► groups[2^globalDepth]
 一般情况     →  dirPtr ──► [dir[0] … dir[2^globalDepth-1]]
                              └► table(groups) ──► group[0..7]
```

这就是官方说的**小 map 优化**：`make(map[K]V)` 之后插 3 个元素，只分配「一个 `Map` 头 + 一个 group」，而不是老的「`hmap` + 一个 bucket（`map[int64]int64` 要 48 + 144 = 192 字节）+ 可能的 `mapextra`」。省的不只是内存，还省了一次指针间接寻址。

**③ 没有溢出桶了。** 装满了就 `grow`：要么把「一个 table 拆成两个 table、`globalDepth++`」，要么「从小 map 升级成 table」。槽位删除后留下 `ctrlDeleted` 墓碑，靠探测继续往后找。

**性能到底提升了多少？** 官方 release notes 给的整体数字很保守（典型 workload 约 2~3% CPU 改善），但**单点**收益明显：小 map 微基准常见 30%+，内存降约 20%，且不再有溢出指针链。**具体数字随 workload 与版本变化，自己 `go test -bench` 才是真相。**

**行为上你需要注意的变化（1.24）：** 遍历顺序**仍然每次随机**，只是随机方式变了（从"随机起始桶 + 槽偏移"变成采样随机起点）；扩容从"每次搬 1~2 个桶"变成"以 table 为单位拆表"；删除留下的墓碑可以被复用，不会越删越涨；实现挪到了 `map_swiss.go` + `internal/runtime/maps`；过渡期可用 `GOEXPERIMENT=noswissmap` 回退。

> ⚠️ **一条铁律没变**：**永远不要依赖 map 的遍历顺序，无论哪个版本。** 1.24 换了实现，正是"依赖顺序的代码"最容易被打脸的时刻——不过它其实在 1.23 就已经随机了，1.24 只是让你换个随机法。

---

## 12.4 Java 对照：会猜错的四个地方

**① "不保证顺序" ≠ "顺序会变"。** 12.1 已经演过一遍，实践含义只有一条：**凡是用 map 遍历结果产生输出、签名、缓存 key、幂等键的地方，一律先排序。**

**② 没有 `equals`/`hashCode` 可重写。** Java 的 map key 靠 `hashCode()` 定位、`equals()` 判等，语义可以自定义（哪怕和 `==` 不一致）。Go 的 key **必须是可比较类型**（`comparable`：数字、string、指针、channel、interface、纯比较字段的 struct、数组；不能是 slice/map/func），比较由编译期生成的按字段比较完成，**不可定制**。所以"用 `Person` 的 `id` 做 hash"在 Go 里必须写成 `map[int64]*Person`——这其实是好事，逼你显式化身份（详见第 4 章）。

**③ 并发语义是"检测"不是"保护"。** `HashMap` 并发写会静默损坏（JDK 7 还会成环死循环），`ConcurrentHashMap` 给你并发安全。Go 的 `map` **两者都没有**：它选择**检测 + fatal**。`sync.Map` 是另一回事（只适合读多写少、key 集合稳定，详见第 22 章）。绝大多数场景的正确解是 **"加锁" 或 "分片加锁"**。

**④ 内存从不归还。** `HashMap` 删空后表数组同样不缩容（只有手动 `trimToSize`），这点两者同构。但 Go 因为**没有分代 GC**（详见第 28 章），长期存活的大 map 更贵，更需要主动整体替换。

| 你会问的问题 | Java | Go |
| --- | --- | --- |
| 为什么不像 JDK8 那样树化？ | 防 hash 碰撞攻击 | `hash0` 随机种子从源头防，不需要树 |
| 为什么负载因子是 6.5 不是 0.75？ | 0.75 = 12/16 | 6.5/8 = 81%；Go 更怕"分配溢出桶" |
| 扩容会不会卡住？ | 会，`resize()` 单次长尾 | 不会，渐进式；但内存瞬时 1.5 倍 |
| 能不能 `get` 拿 null 判断存在？ | 不能（null 二义） | `v, ok := m[k]`，语言级支持 |
| 删除会释放内存吗？ | 元素对象会，表数组不会 | 都不会（除非整体替换） |

---

## 12.5 框架与工程落地

**① 注册表：map 是 Go 的"注解 + 反射扫描"替代品。** 工具注册、provider 注册、中间件注册，Go 里都是一句 `map[string]T` + 启动时写入。关键是**区分"启动期写"和"运行期写"**：

```go
// ✅ 启动期注册、运行期只读：裸 map 就对，读时零开销、无锁
type Registry struct {
	tools map[string]Tool // 只在 init/装配阶段写
	names []string        // 顺手维护稳定顺序，专治 12.1 那种 bug
}

func (r *Registry) MustRegister(name string, t Tool) {
	if _, dup := r.tools[name]; dup {
		panic("duplicate tool: " + name) // 装配期错误就该早炸
	}
	r.tools[name] = t
	r.names = append(r.names, name)
	slices.Sort(r.names) // 让 List 永远确定
}
```

这个模式在 langchaingo 的 `callbacks` 处理器注册、eino 的组件/工具节点查表、MCP SDK 的 server 端 `tools/list` + `tools/call` 分派里到处都是同一副骨架：**名字 → 实现** 的 map + **稳定顺序**的切片。

**② Agent 场景里 map 的三类用法，坑各不相同**：静态注册表（启动期写完、运行期裸 map）、高频计数（token 账本 → 分片锁或 `atomic.Int64`，别用 `map[tenant]int64` 硬扛）、会话/在途请求（`sync.RWMutex` 包住 + 注意不缩容）。

**③ MCP / JSON-RPC 里的经典需求**：请求 id → 等待中的响应 channel。这是"读一次、删一次"的短生命周期 map，必须配 `sync.Mutex` + 超时清理（`defer cleanup()`），否则客户端断连后会留下一堆 `chan` 直到进程结束。**别用裸 map 赌运气，也别用 `sync.Map`——它的语义是"读多写少"，而这里写和删一样频繁。**

---

## 12.6 踩坑清单（CR 可直接抄）

| # | 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- | --- |
| 1 | 输出/prompt 前缀/签名每次不一样；测试偶发失败 | 遍历 map 顺序随机（每次） | 需要确定序：`slices.Sorted(maps.Keys(m))` | 12.3.5 |
| 2 | `fatal error: concurrent map writes`，`recover` 无效 | map 无内部锁，并发写被检测并直接退出 | `sync.RWMutex` 包住；或分片锁；或每 goroutine 本地 map 后合并 | 12.3.6 |
| 3 | `assignment to entry in nil map` | `var m map[K]V` 是 nil，只能读不能写 | `make(map[K]V)` / `make(map[K]V, hint)` | 12.3.3 |
| 4 | 内存单调上涨、删空也不降 | `delete` 不缩容、桶数组不释放 | 定期整体替换 `m = make(...)`；或用 LRU 设容量上限 | 12.3.7 |
| 5 | 大 map 扩容瞬间 RSS 飙 1.5 倍、偶发超时 | 渐进式扩容期间新旧数组共存 | `make(..., hint)` 预分配到位；注意扩容期的内存余量 | 12.3.4 |
| 6 | `m[k]` 为 `nil`/零值，分不清"不存在" | 缺 comma-ok | `v, ok := m[k]; if !ok {…}` | 12.4 |
| 7 | 用 slice / 含 slice 的 struct 当 key，编译不过 | key 必须 `comparable` | 改用可比较的身份（id / 序列化后的 string） | 12.4 |
| 8 | `for k, v := range m { delete(m, k) }` 让人不放心 | 遍历中删除**是**安全的（Go 规范允许） | 但**不要**在遍历中新增键，行为未定义 | 12.3.5 |
| 9 | 升级 1.24 后遍历顺序"看起来变了" | Swiss Table 换了随机方式与内存布局 | 本来就不该依赖；全局搜一遍遍历 map 的排序点 | 12.3.8 |
| 10 | `map` 当 out 参数传进去，函数里重建后调用方没变 | map 值是指针语义，但**重新赋值 header 不可见** | 返回新 map，或传 `*map[K]V` | 第 9 章 |

---

## 12.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| `hmap` | `count`(第一个字段，`len` 直接读) / `B` / `hash0` / `buckets` / `oldbuckets` / `nevacuate` |
| `bmap` | 8 个槽；`tophash[8]` + keys + elems + `overflow *bmap`，后三者编译器生成 |
| tophash | `emptyRest=0 / emptyOne=1 / evacuatedX=2 / evacuatedY=3 / evacuatedEmpty=4 / minTopHash=5` |
| 选桶 | 用 hash **低位** → `hash & (2^B - 1)` |
| 负载因子 | 6.5/8 = 81%；`loadFactorNum=13, loadFactorDen=2` |
| 扩容触发 | 元素过多（翻倍）或溢出桶 ≥ 2^B（`sameSizeGrow` 等量压实） |
| 渐进式扩容 | `growWork` 在每次读写里搬 1~2 个桶；期间内存 ~1.5 倍 |
| 遍历随机 | `mapiterinit` 里 `rand()` 选起始桶 + 起始槽；**永远别依赖顺序** |
| `delete` | 只置 `emptyOne` + 清零 + `count--`；**不缩容、不还内存** |
| 并发写 | 检测到 `hashWriting` 就 `fatal`，`recover` 无效；用锁/分片/本地聚合 |
| 1.24 Swiss | 开放定址 + `ctrl` 控制字 + 8 槽 group + 7/8 负载 + 小 map 单 group |
| 实现位置 | `runtime/map.go` → 1.24：`map_swiss.go` / `map_noswiss.go` + `internal/runtime/maps` |

## 12.8 自检题

1. **`delete` 掉 100 万个键之后，`runtime.ReadMemStats` 显示 `HeapAlloc` 几乎没降，为什么？有什么解？**
   > 答案要点：`mapdelete` 只把槽标成 `emptyOne`、清空 key/elem（好让 GC 回收 key/value 里的指针）、`h.count--`；**桶数组与 `hmap` 从不缩容**，唯一"整理"是只在写操作触发的 `sameSizeGrow`（尺寸不变）。解法：整体替换 `m = make(map[K]V, hint)` 把旧的交给 GC；或换成带容量上限的 LRU。

2. **为什么 Go 1.24 换成 Swiss Table 后，真实服务里"只有 2~3%"，而微基准里小 map 能快 30%+？**
   > 答案要点：① 真实服务里 map 只占总 CPU 一小块（网络、JSON、GC、锁都要钱）；② 大 map 的收益来自缓存局部性，被已有 cache miss 稀释；③ 小 map 的收益是结构性的（少两次分配 + 去掉溢出指针间接寻址），所以微基准显眼。结论：别指望升级白拿 30%，`benchstat` 自己的热点。

3. **一个 goroutine 在写 `map`，另一个在同时 `range` 它，会发生什么？如果同一个 goroutine 边 `range` 边 `delete` 呢？**
   > 答案要点：前者是数据竞争，runtime 有机会检测到并 `fatal("concurrent map read and map write")`（抓不到则表现为不可预期的崩溃/脏数据）——**不是 panic，`recover` 无效**。后者是规范允许的（遍历中删除是安全的）。但**遍历中新增键**行为未定义，别依赖。

## 12.9 延伸阅读

- 源码：`$GOROOT/src/runtime/map.go`（`hmap` / `bmap` / `mapassign` / `mapaccess1` / `mapdelete` / `mapiterinit` / `hashGrow` / `evacuate` / `growWork`）、`runtime/map_swiss.go`、`runtime/map_noswiss.go`、`internal/runtime/maps/{map,group,table,dir}.go`、`internal/abi/map*.go`
- 生成桶类型的编译器侧：`$GOROOT/src/cmd/compile/internal/reflectdata/reflect.go` 的 `MapBucketType`
- 官方 release notes：Go 1.24「New map implementation based on Swiss Tables」
- Abseil 的 Swiss Table 设计文档（`absl/container/internal/raw_hash_set.h` 的注释是全套算法最好的散文）
- 相关章节：[11. 数组与切片](11-slice-array.md)（slice header 与共享底层数组）、[22. sync 原语实现](22-sync-primitives.md)（`sync.Map` / `Mutex`）、[28. GC](28-gc.md)（为什么"不缩容"在非分代 GC 下更贵）

---

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[11. 数组与切片：header、扩容与共享陷阱](11-slice-array.md)
- 下一章：[13. string / []byte / rune 与零拷贝](13-string-bytes-rune.md)
