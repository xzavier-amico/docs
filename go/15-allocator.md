# 15. 内存分配器：mcache / mcentral / mheap

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 数据结构与内存 ｜ **编号**：15 / 54
> **本章定位**：逃逸分析把对象推上堆（第 14 章）之后，落到了这里。Go 的分配器是 TCMalloc 的 Go 版：三级缓存 + size class + span。读懂它，你才能解释「为什么 RSS 是 HeapAlloc 的 6 倍」「为什么 1 亿个 1 字节的对象只花了 100MB 而不是 800MB」「为什么 Pod 被 OOMKill 了但 Go 说我内存很健康」。
> **读法**：15.2 的三级缓存图是本章的地基；15.3.3 的 `mallocgc` 路径要知道每一层"为什么在这里截断"；15.3.7 的 `MemStats` 读法建议直接贴在监控文档里。

---

## 15.1 引子：HeapAlloc 300MB，RSS 8GB

一个 Agent 服务在 K8s 里被 OOMKill，limit 是 8Gi。你拉指标：

```
go_heap_alloc_bytes      300 MiB     ← 存活对象：很健康
go_heap_sys_bytes        7.8 GiB     ← 向 OS 要了 7.8G
go_heap_idle_bytes       7.4 GiB     ← 其中有 7.4G 是"闲置"
go_heap_released_bytes   1.2 GiB     ← 真正还给 OS 的只有 1.2G
go_heap_objects          2.1 M
go_gc_heap_pauses_total  1.1 s
```

于是出现了三个必须回答的问题，它们恰好对应分配器的三层设计：

1. **存活对象 300MB，为什么向 OS 要了 7.8G？** —— 因为「分配出去的 span」和「span 里真正被用的槽」是两回事，size class 的取整、span 的空闲槽、为尖峰预留的 arena 都会让 `HeapSys` 远大于 `HeapAlloc`。
2. **7.4G 闲置，为什么不还给 OS？** —— 因为有 scavenger（后台归还器），但它**慢、渐进、并且受目标约束**（`GOGC` / `GOMEMLIMIT`）。它不是"你闲置了就立刻还"，而是"按目标慢慢还"。
3. **为什么线上偶发 OOMKill，而 Go 内部指标一切正常？** —— 因为 limit 是**容器 RSS**，`HeapSys` 里那 6G 多闲置内存（尤其被 `MADV_FREE` 标过的页）在 cgroup 眼里仍然是"占用"。

> 🎯 **本章主线**：Go 分配器要同时优化三件互相冲突的事 —— **分配要快**（三级缓存）、**碎片要少**（size class + page allocator）、**内存要能还给 OS**（scavenger）。它不是 JVM 那种「分代 + 拷贝」的思路，而是「按尺寸分档 + 位图管理」的思路。**读懂它，你才知道该调 `GOGC`、`GOMEMLIMIT`，还是该改代码。**

---

## 15.2 直觉模型：TCMalloc 的三级缓存

先给你一张能背下来的图（`mallocgc` 的完整分支）：

```
 mallocgc(size, typ, needzero)             ← 所有堆分配的必经之路
   │
   ├─① size == 0 ──────────────────► return unsafe.Pointer(&zerobase)   （零大小不分配）
   │
   ├─② size < 16 && 类型不含指针 ──► tiny allocator：从 mcache.tiny 的
   │                                  16 字节块里切一小片，多个小对象共享一块
   │
   ├─③ size <= 32KB（maxSmallSize）─► 查 size class（68 档）
   │        │
   │        ├─ mcache.alloc[spanClass] 有空槽？ ──► nextFreeFast（位图取位，~十几条指令）
   │        └─ 没有 ──► mcache.refill() ──► mcentral.cacheSpan()（按 size class 加锁）
   │                          └─ 没有空闲 span ──► mheap.alloc()（全局锁）
   │                                                   └─ pageAlloc → sysMap/mmap 向 OS 要
   │
   └─④ size > 32KB（大对象）───────► 直接走 mheap.alloc_m()：分配整页的 span
```

三级缓存的分工，一句话版：

| 层级 | 归谁所有 | 粒度 | 同步成本 | 类比 |
| --- | --- | --- | --- | --- |
| `mcache` | **每个 P** 一个（不是每个 M！） | 按 spanClass 缓存整块 span | **零锁** | TLAB |
| `mcentral` | 全局，每个 spanClass 一个 | span 的空闲槽链表 | 每个 class 一把锁 | 局部仓库 |
| `mheap` | 全局唯一 | 页（8KB）→ span | 一把大锁 + page allocator | 总仓库 |
| OS | — | arena（64MB / mmap 块） | syscall | 供应商 |

```
        ┌────────────────────────────────────────────────────────────┐
        │  P0            P1            P2            P3              │
        │  ┌──────┐     ┌──────┐     ┌──────┐     ┌──────┐           │
        │  │mcache│     │mcache│     │mcache│     │mcache│           │
        │  │class1│     │class1│     │class1│     │class1│           │
        │  │class2│     │class2│     │class2│     │class2│  …136 档  │
        │  │ tiny │     │ tiny │     │ tiny │     │ tiny │           │
        │  └───┬──┘     └───┬──┘     └───┬──┘     └───┬──┘           │
        └──────┼────────────┼────────────┼────────────┼──────────────┘
               │ refill     │            │            │
               ▼            ▼            ▼            ▼
        ┌────────────────────────────────────────────────────────────┐
        │  mcentral[136]  —— 每个 size class 一份                    │
        │  partial[2] (有闲槽的 span)   full[2] (满了的 span)         │
        └───────────────────────────┬────────────────────────────────┘
                                    │ 需要新 span
                                    ▼
        ┌────────────────────────────────────────────────────────────┐
        │  mheap（全局锁）                                            │
        │  pages: pageAlloc（按 8KB 页的位图/trie）                    │
        │  arenas: 64MB 为单位的 arena 数组 → spans[8192]             │
        └───────────────────────────┬────────────────────────────────┘
                                    │ sysMap / mmap
                                    ▼
                                    OS
```

> 💡 **为什么是 P 而不是 M？** 因为 Go 的 goroutine 会在 M 上迁移，而 `mcache` 必须绑定在"不会被抢占的执行上下文"上。P 是调度的资源单位（详见第 18 章），goroutine 在同一时刻只属于一个 P，所以 `mcache` **无需加锁**。代价很真实：**goroutine 换 P 之后，上一次用热的 mcache 就"凉"了**——这是 Go 里没有 TLAB 那种"线程本地亲和性"的根本原因。

### 15.2.1 与 JVM 的直觉映射

| JVM | Go | 关键差异 |
| --- | --- | --- |
| TLAB（Eden 里线程本地块） | `mcache` | TLAB 是"一整块 bump pointer"，mcache 是"136 档 size class 各自的 span 缓存" |
| Eden / Survivor / Old | **没有** | Go 只有"一次堆"，没有分代（详见第 28 章） |
| G1 Region（1~32MB） | **arena（64MB）+ span（1~N 页）** | Region 是回收单位，Go 的回收单位是"对象（mark）+ span（sweep）" |
| 对象头（16B） | **无** | 类型信息靠 span 的 `spanclass` + `type`，不走对象内存 |
| `-Xmx` 硬上限 | 无硬上限，只有 `GOMEMLIMIT`（软限制） | 所以 `HeapSys` 可以一路涨到 `GOMEMLIMIT` 附近 |
| Full GC 后 `uncommit` | scavenger 持续归还 | JVM 归还通常滞后且粗暴，Go 是渐进式 |

---

## 15.3 底层实现

### 15.3.1 size class：68 档，把任意大小"取整"

```go
// runtime/sizeclasses.go（头部注释与表的真实结构，数值节选）
const (
	_NumSizeClasses = 68
)

var class_to_size = [_NumSizeClasses]uint16{
	0, 8, 16, 24, 32, 48, 64, 80, 96, 112, 128, 144, 160, 176, 192, 208, 224, 240,
	256, 288, 320, 352, 384, 416, 448, 480, 512, 576, 640, 704, 768, 896, 1024,
	// …中间省略…
	20480, 21760, 24576, 27264, 28672, 32768, // 最大 32KB
}
```

设计要点（这张表是**离线算出来的**，注释里写了目标）：

- 小尺寸（≤128B）8 字节一档，密集覆盖最热的分配；
- 中等尺寸（≤512B）16 字节一档；
- 大尺寸按"浪费率 ≤ 12.5%"的规则稀疏排布；
- **最大 32768**（`maxSmallSize`），超过就是"大对象"，直接走页分配。

代价就是**内部碎片**：

| 你 `make`/`new` 的大小 | 落到哪档 | 浪费 |
| --- | --- | --- |
| 1 | 8 | 87.5% ⚠️（但会被 tiny allocator 救回来） |
| 9 | 16 | 43.8% |
| 17 | 24 | 29.2% |
| 25 | 32 | 22% |
| 33 | 48 | 31% |
| 100 | 112 | 10.7% |
| 1000 | 1024 | 2.3% |
| 1200 | 1280 | 6.3% |
| 5000 | 5376 | 7% |
| 33000 | 大对象（按页） | 页级取整（8KB 粒度） |

**这解释了一个常见现象**：一个 `[]byte` 从 1000 涨到 1200，成本几乎没变（1024 → 1280）；但从 8192 涨到 8200，成本直接跳（跨页边界，span 多一页）。**做批量 buffer 时，把大小卡在 size class 边界上（如 1024、1280、2048、8192）能省下好几个百分点。**

```go
// 可运行：亲手量出 size class 的"取整浪费"
package main

import (
	"fmt"
	"runtime"
)

type box struct{ b []byte } // 让切片逃逸到堆

var sink []*box

func allocN(size, n int) uint64 {
	var before, after runtime.MemStats
	sink = make([]*box, 0, n)
	runtime.GC()
	runtime.ReadMemStats(&before)
	for i := 0; i < n; i++ {
		b := make([]byte, size) // 元素数据 + 24B 切片 header 在 box 里
		sink = append(sink, &box{b: b})
	}
	runtime.ReadMemStats(&after)
	return (after.HeapAlloc - before.HeapAlloc) / uint64(n)
}

func main() {
	fmt.Printf("%8s %12s\n", "size", "bytes/obj")
	for _, s := range []int{1, 8, 16, 24, 25, 32, 33, 48, 100, 112, 1000, 1024, 1200, 1280, 5000} {
		fmt.Printf("%8d %12d\n", s, allocN(s, 2000))
	}
	runtime.KeepAlive(sink)
	// 你会看到明显的台阶：请求 25 和请求 32 的每对象成本一样（都落在 32 档）
}
```

### 15.3.2 span 与 arena：页之上的两种粒度

- **page**：8KB（`pageShift = 13`），分配器的物理单位。
- **span（`mspan`）**：1~N 个**连续**页，被切成 `nelems` 个**同尺寸**的槽。`mspan` 是"一组同 size class 的槽"的元数据。
- **arena**：64MB 的地址空间块（amd64 上 `logHeapArenaBytes = 6+20`），每个 arena 有一张 `spans[8192]` 表（8192 = 64MB / 8KB）和位图。

```
arena 64MB ┌──────────────────────────────────────────────────────┐
           │  spans[8192]  →  每个 8KB 页对应的 *mspan            │
           │  bitmap       →  GC 标记位（4 bit / 8 字节字）        │
           │  pageInUse / pageMarks / pageSpecials                │
           └──────────────────────────────────────────────────────┘
                    │ 每个 mspan 管 1..N 个连续页
                    ▼
span        ┌───┬───┬───┬───┬───┬───┬───┬───┐   例如 1 页(8KB) / 64B 一档 = 128 个槽
            │ 0 │ 1 │ x │ 3 │ 4 │ x │ x │ 7 │   x = 已分配
            └───┴───┴───┴───┴───┴───┴───┴───┘
              freeindex=2  allocCache 位图里有 0 的位就是空闲槽
```

`mspan` 的真实字段（`runtime/mheap.go`，节选）：

```go
type mspan struct {
	next     *mspan
	prev     *mspan
	startAddr uintptr      // span 首字节地址
	npages    uintptr      // 占了多少页
	nelems    uintptr      // 一共切成多少个槽（= npages*pageSize/size）

	freeindex  uintptr     // 下一个"值得开始找空槽"的位置（字段类型随版本有调整，以本地源码为准）
	allocCache uint64      // ★ freeindex 附近 64 个槽的空闲位图缓存，nextFreeFast 靠它
	allocBits  *gcBits     // 完整的分配位图
	gcmarkBits *gcBits     // GC 标记位图（sweep 用它判断能否回收）

	sweepgen  uint32       // 与 mheap.sweepgen 比较，判断这个 span 是否已清扫
	spanclass spanClass    // ★ = size class 索引 | noscan 标志位
	state     mSpanStateBox// mSpanInUse / mSpanManual / mSpanFree / mSpanDead
}
```

**`spanclass` 把 size class 和"是否含指针"打包进一个字节**：`numSpanClasses = _NumSizeClasses << 1 = 136`。为什么要区分 noscan？因为不含指针的对象（`[]byte`、纯数值 struct）GC **根本不需要扫描**内容，直接整块回收——这是 Go 描述符式 GC 的核心优化（详见第 28 章）。

### 15.3.3 tiny allocator：1 字节对象的救命恩人

如果每个 `*int8` 都占一个 8 字节的 size class 槽，那 87.5% 的内存就白扔了。`mallocgc` 对此有个特例：

```go
// runtime/malloc.go（精简，保留关键判断）
func mallocgc(size uintptr, typ *_type, needzero bool) unsafe.Pointer {
	if size == 0 {
		return unsafe.Pointer(&zerobase) // 零大小对象：全部指向同一个地址
	}
	...
	if size <= maxSmallSize {
		if noscan && size < maxTinySize { // maxTinySize = 16
			off := c.tinyoffset
			// 对齐要求：2/4/8 字节对象要按自身大小对齐
			if size&7 == 0 {
				off = alignUp(off, 8)
			} else if size&3 == 0 {
				off = alignUp(off, 4)
			} else if size&1 == 0 {
				off = alignUp(off, 2)
			}
			if off+size <= maxTinySize && c.tiny != nil {
				x = add(c.tiny, off)   // ★ 从同一个 16 字节块里再切一片
				c.tinyoffset = off + size
				...
				return x
			}
			// 块用完了：申请一个新的 16 字节块（tinySizeClass = 2）
			span := c.alloc[tinySpanClass]
			v := nextFreeFast(span)
			...
			c.tiny, c.tinyoffset = x, size
			return x
		}
		... // 常规 size class 路径
	}
}
```

效果：**16 个 1 字节的对象共用一个 16 字节块**（每对象 1 字节，而不是 8 字节，省 8 倍）。这条路径专为"大量小字符串头、小闭包环境、小结构体指针"设计。限制是：**只能用于 `noscan`（不含指针）且 < 16 字节**的对象——含指针的对象不能混在一起，否则 GC 无法精确扫描。

### 15.3.4 快路径：`nextFreeFast` 与 `allocCache`

命中 `mcache` 时，分配只是"在位图里找一个 0 位"：

```go
// runtime/malloc.go
func nextFreeFast(s *mspan) gclinkptr {
	theBit := sys.TrailingZeros64(s.allocCache) // 找最低位的 1
	if theBit < 64 {
		result := s.freeindex + uintptr(theBit)
		if result < s.nelems {
			freeidx := result + 1
			if freeidx%64 == 0 && freeidx != s.nelems {
				return 0 // 跨 64 位字了，交给慢路径
			}
			s.allocCache >>= uint(theBit + 1)
			s.freeindex = freeidx
			s.allocCount++
			return gclinkptr(result*s.elemsize + s.base())
		}
	}
	return 0
}
```

`allocCache` 是"下一次搜索的 64 个槽的空闲位图"：一条 `TZCNT` 找最低空闲位，一次移位推进，大概十几条指令。**这就是 Go 分配的快路径成本**——比 JVM 的 TLAB bump pointer（2~3 条指令）贵，但比任何加锁方案都便宜几个数量级。

### 15.3.5 `mcentral` 与 `mheap`：真加锁的两层

`mcache` 空了就 `refill()`，向 `mcentral` 要一个"有闲槽的 span"：

```go
// runtime/mcentral.go
type mcentral struct {
	spanclass spanClass
	partial   [2]spanSet // 有闲槽的 span 集合
	full      [2]spanSet // 满了的 span 集合
	// 两个下标是为了和并发 sweep 的两代（sweepgen 当前代/上一代）配合
}
```

`mcentral` 还没货，就找 `mheap`：

```go
// runtime/mheap.go（字段名真实，Go 1.21+ 用 pageAlloc 取代了旧的 free/busy treap）
type mheap struct {
	lock  mutex
	pages pageAlloc  // 页级分配器：位图 + radix trie，能按对齐要求找连续页
	sweepgen uint32  // 清扫代数
	allspans []*mspan
	arenas [1 << arenaL1Bits]*[1 << arenaL2Bits]*heapArena
	central [numSpanClasses]struct {
		mcentral mcentral
		pad [cpu.CacheLinePadSize - unsafe.Sizeof(mcentral{})%cpu.CacheLinePadSize]byte
	}
	scav scavenge   // scavenger 状态
}
```

大对象（> 32KB）走 `allocLarge`：不经过 `mcache`、不经过 `mcentral`，直接向 `mheap` 要 `ceil(size/pageSize)` 个连续页的 span。**这意味着一个 100MB 的 `[]byte` 只有一次 mheap 分配，但一次就锁全局**——所以高并发下频繁分配大对象是灾难。

### 15.3.6 scavenger：渐进式归还

Go 的堆**没有硬上限**（不像 `-Xmx`），所以必须有机制把闲置内存还给 OS。这就是 scavenger（`runtime/mgcscavenge.go`）：

- 它是一个**后台 goroutine**（`scavenger`），在 GC 启动后被唤醒；
- 目标是让 `HeapAlloc + 各种保留量` 逼近 **scavengeGoal**（由 `GOGC` 与 `GOMEMLIMIT` 决定，`GOMEMLIMIT` 优先）；
- 每次只归还一小片（按目标计算速率），并且有"索引"记住上次归还到哪里（`scavengeOffset`），渐进扫描；
- 归还手段是 `madvise(MADV_DONTNEED)`（Linux）。

> 🩸 **一个真实的事故链（Go 1.16 前的经典）**：Go 曾用 `MADV_FREE`，页被标记为"可回收"但**RSS 不下降**，直到内存紧张才真正释放。在 K8s 里 → `container_memory_working_set_bytes` 居高不下 → 被 OOMKill，而 Go 内部指标一切正常。Go 1.16 起 Linux 默认改用 `MADV_DONTNEED`（立即下降 RSS），并把开关保留为 `GODEBUG=madvdontneed=1` 的历史兼容项。**如果你在极老的镜像里看到这个症状，先查 Go 版本。**

紧急情况下还有一个粗暴的开关：

```go
// 强制 GC + 立即归还（会 STW 一次，别放在热路径里）
debug.FreeOSMemory()
```

### 15.3.7 `runtime.MemStats` 读法（最重要的一节）

```go
// 可运行：一次完整的 MemStats 体检
package main

import (
	"fmt"
	"runtime"
)

func mib(b uint64) float64 { return float64(b) / (1 << 20) }

func main() {
	var ms runtime.MemStats
	runtime.ReadMemStats(&ms) // 会 STW 一小下（微秒级），别在高频路径调用

	fmt.Printf("HeapAlloc     %8.1f MiB  存活对象（GC 眼里的“有用内存”）\n", mib(ms.HeapAlloc))
	fmt.Printf("HeapInuse     %8.1f MiB  已被 span 覆盖（含碎片 + 未清扫垃圾）\n", mib(ms.HeapInuse))
	fmt.Printf("HeapIdle      %8.1f MiB  闲置 span（可以归还）\n", mib(ms.HeapIdle))
	fmt.Printf("HeapReleased  %8.1f MiB  已归还 OS（是 Idle 的子集）\n", mib(ms.HeapReleased))
	fmt.Printf("HeapSys       %8.1f MiB  = HeapInuse + HeapIdle\n", mib(ms.HeapSys))
	fmt.Printf("StackInuse    %8.1f MiB  所有 goroutine 栈\n", mib(ms.StackInuse))
	fmt.Printf("MSpanInuse    %8.1f MiB  mspan 结构体自身\n", mib(ms.MSpanInuse))
	fmt.Printf("MCacheInuse   %8.1f MiB  所有 P 的 mcache\n", mib(ms.MCacheInuse))
	fmt.Printf("GCSys         %8.1f MiB  GC 元数据（位图等）\n", mib(ms.GCSys))
	fmt.Printf("OtherSys      %8.1f MiB  runtime 其它（调度器等）\n", mib(ms.OtherSys))
	fmt.Printf("Mallocs/Frees %d / %d\n", ms.Mallocs, ms.Frees)
	fmt.Printf("NumGC         %d，PauseTotal %d ms，GCCPUFraction %.3f\n",
		ms.NumGC, ms.PauseTotalNs/1e6, ms.GCCPUFraction)
	fmt.Printf("NextGC        %8.1f MiB  下一次 GC 的目标 HeapAlloc\n", mib(ms.NextGC))
}
```

核心恒等式与读法：

```
HeapSys = HeapInuse + HeapIdle
HeapIdle ⊇ HeapReleased          ← 已归还的是 Idle 的一部分
HeapInuse = HeapAlloc + 未清扫垃圾 + span 内部碎片 + 已分配未使用的槽
```

| 指标 | 含义 | 什么时候该报警 / 动手 |
| --- | --- | --- |
| `HeapAlloc` | 存活对象字节（≈ live heap） | 单调上涨 = 泄漏；周期性锯齿 = 正常 |
| `HeapInuse` | 被 span 占住的量 | 远大于 `HeapAlloc` = 碎片或未清扫 |
| `HeapIdle` | 可归还的闲置 span | 长期很大但 `HeapReleased` 很小 = scavenger 没跟上 |
| `HeapReleased` | 真还给 OS 的量 | 与 RSS 对照，能看出内存归还是否生效 |
| `HeapSys` | 向 OS 要的总量 | **容器 limit 要按它（更准确说按 RSS）来算，不是 `HeapAlloc`** |
| `StackInuse` | goroutine 栈总和 | 突增 = goroutine 泄漏（详见第 17、25 章） |
| `MSpanInuse` / `MCacheInuse` | 元数据自身 | 通常几十 MB 以内；P 数量越多 `MCacheInuse` 越大 |
| `NumGC` / `PauseTotalNs` | GC 次数与总停顿 | 停顿占比 > 5% 该调 `GOGC` / `GOMEMLIMIT` |
| `NextGC` | 下次触发的目标 | 用 `GOMEMLIMIT` 时要关注它与 limit 的距离 |

> 💡 **想更精细就用 `runtime/metrics`**（1.16+，稳定 API，名字如 `/memory/classes/heap/objects:bytes`、`/memory/classes/heap/unused:bytes`、`/memory/classes/heap/released:bytes`、`/gc/heap/allocs:bytes`、`/gc/pauses:seconds`）。Prometheus 的 Go client 和 OTel 的 runtime instrumentation 都是从这里取的，**别再自己解析 `MemStats` 结构体做监控**。

---

## 15.4 Java 对照：会猜错的四件事

**①「Go 没有 -Xmx，所以内存不可控」是半真半假。** Go 确实没有硬上限，但有 `GOMEMLIMIT`（1.19，软限制）：它告诉 runtime「进程总内存别超过这个数」，GC 会更早触发、scavenger 会更积极。但它是**软**的——如果存活对象本身就超过限制，`GOMEMLIMIT` 会**放弃限制，优先保证程序不 OOM**（然后你可能被容器 OOMKill）。这和 JVM「`-Xmx` 超了就 OOM 异常，但堆一定不会超」是完全相反的哲学。落地姿势见第 52 章。

**② JVM 的分配是「移动指针」，Go 的分配是「填位图」。** TLAB 里分配一个对象是 `top += size`（2~3 条指令）；Go 的 `nextFreeFast` 是"在 64 位位图里找最低 0 位"（十几条）。**这就是为什么「Go 的小对象分配很快」这句话是有条件的**：它比加锁分配快得多，但比 JVM 的 TLAB 慢一截。补偿手段是：Go 用逃逸分析把大部分对象赶到栈上（第 14 章），而 JVM 靠"短命对象在 Eden 里几乎零成本"。

**③ 分代假说 vs 无分代。** JVM 的核心假设是"绝大多数对象朝生夕死"（弱分代假说），于是用复制算法把年轻代做得极便宜。Go 显式**拒绝**分代假说（官方理由是：Go 的很多对象是"长期存活的小对象"，比如 goroutine 栈、buffer 池，分代收益不明显而复杂度/停顿不可控）。代价：**Go 的 GC 每次都要扫全堆**（靠并发标记 + 混合写屏障把停顿压在亚毫秒，详见第 28 章）；收益：没有"对象晋升""跨代引用卡表"这些复杂度，延迟可预测。

**④ 碎片形态不同。** JVM 的碎片主要在**老年代**（不压缩就会碎片化，G1 靠 region 拷贝缓解）；Go 的碎片在 **size class 内部**（取整浪费）和 **span 级**（半满的 span 不能被别的 size class 用）。Go **不做对象移动**（不压缩），所以不会"整理"内存，只能等整个 span 空掉之后归还。

| 问题 | JVM 答案 | Go 答案 |
| --- | --- | --- |
| 内存上限 | `-Xmx`（硬） | `GOMEMLIMIT`（软） |
| 分配快路径 | TLAB bump pointer | mcache + `allocCache` 位图 |
| 小对象优化 | TLAB + 逃逸分析（标量替换） | tiny allocator + 栈上分配 |
| 回收单位 | 对象（copy/mark-sweep-compact） | 对象（mark）+ span（sweep） |
| 碎片处理 | 压缩 / region 拷贝 | 不压缩，靠 size class + span 归还 |
| 归还 OS | GC 后 uncommit，粒度粗 | scavenger 渐进归还（`MADV_DONTNEED`） |
| 观测 | `jcmd GC.heap_info`、JFR | `runtime/metrics` / `MemStats` / pprof |

---

## 15.5 框架与工程落地

**① buffer 大小要卡 size class 边界。** SSE / token 流 / JSON 解析里的 `bufio.Reader`，默认 4096 字节（正好是一个 size class）。如果你要自定义，选 **1024 / 2048 / 4096 / 8192 / 16384**，别选 5000、10000——那会直接跳到 5376/10240 两档，内部碎片变大不说，和页的整除关系也变差。

```go
// ❌ 10000 → 落到 10240 档，且不是页的整数倍
buf := make([]byte, 10000)
// ✅ 8192 = 正好 1 页，1 个 span 装 1 个槽，零内部碎片
buf := make([]byte, 8192)
```

**② 大对象（> 32KB）要池化，而且要知道它有"全局锁"代价。** 这是 `sync.Pool` 真正该用的地方：

```go
var bufPool = sync.Pool{
	New: func() any {
		b := make([]byte, 64*1024) // 64KB → 大对象路径
		return &b
	},
}

func encodeSSE(w io.Writer, payload []byte) error {
	bp := bufPool.Get().(*[]byte)
	defer bufPool.Put(bp)
	buf := (*bp)[:0]
	buf = append(buf, "data: "...)
	buf = append(buf, payload...)
	buf = append(buf, '\n', '\n')
	_, err := w.Write(buf)
	return err
}
```
注意两件事：① 池里放**指针**（`*[]byte`），别放切片本体（会有分配）；② `sync.Pool` 的对象会经过两轮 GC 后被清掉，**不能当持久缓存**（详见第 22 章）。

**③ 监控用 `runtime/metrics`，不是 `MemStats`，更不是自己的计数器。**

```go
import "runtime/metrics"

var samples = []metrics.Sample{
	{Name: "/memory/classes/heap/objects:bytes"},  // ≈ HeapAlloc
	{Name: "/memory/classes/heap/unused:bytes"},   // ≈ HeapInuse - HeapAlloc
	{Name: "/memory/classes/heap/free:bytes"},     // ≈ HeapIdle - HeapReleased
	{Name: "/memory/classes/heap/released:bytes"}, // ≈ HeapReleased
	{Name: "/gc/heap/allocs:bytes"},               // 累计分配（看分配速率）
	{Name: "/gc/pauses:seconds"},                  // 停顿直方图
}
metrics.Read(samples) // 零分配的读取方式（复用 samples 切片）
```
`otel-go` 的 `runtime` instrumentation 与 Prometheus client 的 `collectors.NewGoCollector()` 底层就是这些指标。**容器 limit 要按 `RSS ≈ objects + unused + free + runtime 自身的 stack/other` 来算**，只用 `objects` 会严重低估（就是 15.1 那个事故）。

**④ 什么时候该调参数？**

| 症状 | 动作 |
| --- | --- |
| GC 太频繁（`NumGC` 飙升、CPU 里 GC 占比 > 20%） | 抬 `GOGC`（如 `GOGC=200`）或设 `GOMEMLIMIT` |
| 内存吃紧、Pod 逼近 limit | 设 `GOMEMLIMIT = limit * 0.9`（留 10% 给非堆），并降 `GOGC` |
| 分配速率极高（`/gc/heap/allocs:bytes` 斜率陡） | **改代码**：池化、预分配、去装箱（第 14、30 章）。调 GC 参数是止痛药，不是药 |
| `mscache`/`MSpan` 元数据很大 | 减少 P 数量（`GOMAXPROCS`）通常无效；真正原因一般是 P 太多 + 分配热点分散 |

---

## 15.6 踩坑清单（CR 可直接抄）

| # | 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- | --- |
| 1 | HeapAlloc 300MB 但 RSS 8GB，被 OOMKill | `HeapSys` 远大于存活对象；闲置内存未归还；cgroup 看 RSS | 设 `GOMEMLIMIT`；容器 limit 按 RSS 定；查 `HeapReleased` | 15.1 / 15.3.6 |
| 2 | 大量 1~8 字节的小对象吃掉大量内存 | 目标是含**指针**的小对象，走不了 tiny allocator | 减少指针字段（用索引代替指针）、合并到 struct、用值切片 | 15.3.3 |
| 3 | 内存指标"健康"但仍被 kill | 只监控了 `HeapAlloc`，漏了 `HeapSys` / `StackInuse` / 元数据 | 用 `runtime/metrics` 全套 + `container_memory_working_set_bytes` | 15.5 |
| 4 | `[]byte` 从 8192 改成 8200，性能掉一截 | 跨页：span 从 1 页变 2 页，且不再是 size class 边界 | buffer 尺寸取页/size class 边界（1024/2048/4096/8192） | 15.3.1 |
| 5 | 高并发下偶发延迟尖刺 | 大对象分配走 `mheap` 全局锁 | 大对象池化复用；避免每请求 `make([]byte, 1<<20)` | 15.3.5 |
| 6 | `debug.FreeOSMemory()` 加在定时任务后，服务周期性卡顿 | 它触发完整 GC + 同步归还，会 STW | 别用它做常规归还；靠 scavenger + `GOMEMLIMIT` | 15.3.6 |
| 7 | 以为 `runtime.GC()` 能"释放内存" | GC 只回收不可达对象，**不保证归还 OS** | 归还靠 scavenger；要立刻归还才用 `FreeOSMemory` | 15.3.6 |
| 8 | `sync.Pool` 里放切片值而不是指针 | `Pool.Put(b)` 会装箱，切片 header 被复制/逃逸 | 放 `*[]byte`，`Get` 后 `b = b[:0]` 复用 | 15.5 / 第 22 章 |
| 9 | `GOMAXPROCS` 调到很大后元数据暴涨 | 每个 P 一个 `mcache`，136 个 span 指针 + tiny 状态 | 1.25 起容器感知 `GOMAXPROCS`（第 52 章）；别盲目调大 | 15.2 |
| 10 | goroutine 数量上百万后 `StackInuse` 爆炸 | 栈也走分配器（`stackalloc` 从小 span 切） | 优先限流/池化，别把"能起 100 万 goroutine"当免费午餐 | 第 17 章 |

---

## 15.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 三级缓存 | `mcache`（每 **P**、无锁）→ `mcentral`（每 size class、一把锁）→ `mheap`（全局锁） |
| size class | 68 档，8B~32KB；≤128B 每 8 字节一档；内部碎片最多 87.5%（1 字节对象） |
| `maxSmallSize` | 32768；超过就是大对象，直接走 `mheap` 的页分配 |
| span | 1~N 个连续页，切成 `nelems` 个同尺寸槽；元数据是 `mspan` |
| arena | 64MB（amd64），带 `spans[8192]` 与 GC 位图 |
| `spanclass` | size class 索引 + noscan 标志位打包成一个字节，共 136 档；noscan 对象 GC 不扫内容 |
| 快路径 | `nextFreeFast`：`allocCache` 位图里找最低 0 位，十几条指令 |
| tiny allocator | `noscan && size < 16` → 16 字节块内共切，1 字节对象只花 1 字节 |
| 大对象 | >32KB，绕过 mcache/mcentral，加 `mheap` 全局锁 → 必须池化 |
| scavenger | 后台渐进归还；`MADV_DONTNEED`（1.16+）；目标由 `GOGC`/`GOMEMLIMIT` 决定 |
| MemStats 恒等式 | `HeapSys = HeapInuse + HeapIdle`，`HeapIdle ⊇ HeapReleased` |
| 与 JVM 的核心差异 | 无硬上限（软限制）、无分代、不压缩、无 TLAB（是 mcache）、无对象头 |

## 15.8 自检题

1. **一个 goroutine 分配了 `make([]byte, 100)`，然后被 `time.Sleep` 抢占换到了另一个 P 上再分配。第二次分配走的还是同一个 `mcache` 吗？这对性能意味着什么？**
   > 答案要点：**不是**。`mcache` 绑定在 `P` 上，goroutine 换 P 后用的是新 P 的 `mcache`——同一个 size class 的 span 缓存是冷的，可能触发一次 `refill`（加 `mcentral` 的锁）。影响：① 相比 JVM TLAB（线程本地、迁移不换），Go 没有"线程亲和"这层保障；② 高并发 + 频繁切换会让 mcache 命中率下降；③ 这也是为什么 `runtime.LockOSThread` 在某些极端场景有意义（但一般别用）。缓解手段是减少 goroutine 数量 / 用池化，而不是去碰 mcache。

2. **`HeapAlloc = 300MB`，`HeapInuse = 400MB`，`HeapIdle = 7.4GB`，`HeapReleased = 1.2GB`。这 4 个数字有没有矛盾？该怎么解释？**
   > 答案要点：不矛盾。`HeapInuse` 比 `HeapAlloc` 多出来的 100MB 是"span 内部碎片 + 已分配但未被使用的槽 + 已死待清扫对象"。`HeapSys = Inuse + Idle ≈ 7.8GB` 表示历史上曾经需要这么多。`HeapIdle` 7.4GB 中只有 1.2GB 真的还给了 OS，剩下 6.2GB 是"闲置但还攥着"——scavenger 受 `GOGC`/`GOMEMLIMIT` 目标约束，没那么急。容器里这 6.2GB 仍计入 RSS，所以会被 OOMKill。解法：`GOMEMLIMIT` 让 scavenger 更积极，或减小并发尖峰。

3. **为什么 `make([]byte, 8)` 和 `make([]byte, 16)` 的实际内存代价可能差 3 倍，而 1000 与 1024 几乎一样？**
   > 答案要点：`8` 落在 8 档、`16` 落在 16 档，单看取整没差多少，但**还要加上 `[]byte` 的 24 字节 header（如果在堆上）和 GC 位图/对齐开销**；更关键的是小对象受 **tiny allocator** 与 span 最小开销影响，8 字节的 span 装 1024 个槽 vs 16 字节装 512 个，元数据开销占比不同。而 1000→1024 只是从"取整浪费 24 字节"变成"0 浪费"，同属 1024 档附近，所以曲线是**台阶状**的。**结论不是背公式，而是：改 buffer 大小时沿着台阶走（8/16/32/…/1024/2048/4096/8192），别停在台阶中间。**

## 15.9 延伸阅读

- 源码：`$GOROOT/src/runtime/malloc.go`（`mallocgc` / `nextFreeFast` / `maxTinySize` / `zerobase`）、`runtime/mheap.go`（`mspan` / `mheap` / `heapArena` / `allocSpan`）、`runtime/mcentral.go`（`mcentral.cacheSpan`）、`runtime/mcache.go`、`runtime/sizeclasses.go`（`class_to_size` 与那张表背后的设计注释）、`runtime/mgcscavenge.go`（scavenger）
- 观测：`runtime/metrics` 包文档（指标全集与语义，**强烈建议通读一遍**）、`runtime.MemStats`、`net/http/pprof` 的 `heap`
- 背景论文：Sanjay Ghemawat & Paul Menage, *TCMalloc: Thread-Caching Malloc*（Go 分配器的直系祖先）
- Go 官方文档：`GOMEMLIMIT` 的 proposal 与 runtime 包注释里关于内存限制的说明
- 相关章节：[14. 内存布局与逃逸分析](14-memory-layout-escape.md)（谁被推上堆）、[22. sync 原语实现](22-sync-primitives.md)（`sync.Pool` 三层结构）、[28. GC](28-gc.md)（标记清扫怎么和 span 位图配合）、[52. 部署与 SRE](52-deployment-sre.md)（`GOMEMLIMIT` 与 K8s limit 的对齐）

---

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[14. 内存布局、对齐与逃逸分析](14-memory-layout-escape.md)
- 下一章：[16. unsafe 与 cgo 的边界](16-unsafe-cgo.md)
