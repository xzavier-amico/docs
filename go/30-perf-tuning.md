# 30. 性能优化：分配、锁、批处理

> **状态**：`已完成（初稿）` ｜ **所属**：第五部分 · 运行时与性能 ｜ **编号**：30 / 54
> **本章定位**：Go 的性能优化 90% 是同一件事的变体——**让每次请求少碰堆、少抢锁、少进 syscall**。这一章把那 90% 的手法按收益排序，配上 benchmark 证据，最后给一张能贴到团队 wiki 的「优化台账」模板。
> **读法**：30.2 记住优先级顺序，30.3 按你的瓶颈挑一节读，30.5 的台账模板直接抄。

---

## 30.1 引子：一次请求分配 12000 次

一个 RAG 问答接口，压测 QPS 死活上不去，`gctrace` 里 GC 占 CPU 28%，火焰图顶部整片是 `runtime.mallocgc` / `runtime.gcAssistAlloc` / `fmt.Sprintf`。用 `-benchmem` 一测单请求路径：

```
BenchmarkRAGQuery-8   1000   2.1 ms/op   412 KB/op   11840 allocs/op
```

**一次请求 11840 次分配、412KB**。逐行扒出来是这些：

- `fmt.Sprintf("你是一个%s助手…%s", role, ctx)` 拼 prompt（第 14 章讲过 `Sprintf` 的参数会逃逸）；
- `strings.Split(doc, "\n")` 然后再 `strings.Join` 拼回去；
- 每个 chunk 一次 `json.Marshal` 到一个新 `[]byte`；
- 每个请求 `new(bytes.Buffer)` 做 SSE 缓冲；
- 日志 `logger.Info("chunk", "text", chunk)` 里的 `chunk` 是 `string`，被装箱成 `any` 逃逸。

只做三件低风险的事——`strings.Builder` 预分配、`sync.Pool` 复用 buffer、日志改成惰性字段——结果：

```
BenchmarkRAGQuery-8   3000   1.3 ms/op   60 KB/op   870 allocs/op
```

**分配次数降了 13 倍，P99 从 210ms 掉到 130ms，`gctrace` 里的 GC CPU 从 28% 到 9%。** 代码可读性几乎没变。

> 🎯 本章的立场：**Go 的性能优化不是「把聪明算法搬到热点上」，而是「把无谓的开销删掉」。** 因为 Go 已经帮你把「聪明的算法级优化」压缩到了很小（第 27 章的 SSA + 内联），剩下的天花板基本由**分配速率、锁竞争、syscall 次数**三件事决定。

---

## 30.2 直觉模型：一次分配的三层账单

```
 buf := new(bytes.Buffer)          ← 你以为的成本：25ns
   │
   ├─ ① 分配本身：mcache 无锁路径，~25ns（tiny/small 对象）
   ├─ ② GC 账单：这个对象活多久？被标记一次要扫它的指针 →
   │        分配速率高 → mark assist 来收税（第 28 章）→ CPU 被吃掉
   └─ ③ 内存局部性：新分配的对象散落在不同 span → cache miss →
            实际读写比预期慢 3~10 倍
```

三层账单里，**① 最便宜，② ③ 才是真正花钱的地方**。所以优化的优先级永远是：

| 优先级 | 手法 | 典型收益 | 风险 |
| --- | --- | --- | --- |
| 🥇 最高 | **不分配**：栈上变量、复用入参、避免转换 | 减少 50%~90% allocs | 低（可能牺牲一点可读性） |
| 🥈 高 | **少分配**：预分配 `cap`、`Grow`、`Builder` | 减少 20%~70% allocs | 低 |
| 🥉 中 | **池化**：`sync.Pool` 复用大对象 | 减少大对象 GC 压力 | 中（会被 GC 清空、生命周期易错） |
| 第四 | **少进 syscall**：`bufio`、批量写、`writev` | 减少 30%~90% 系统调用 | 低 |
| 第五 | **少抢锁**：分片、原子、快照 | 高并发下线性度提升 | 中（正确性风险） |
| 第六 | **缓存友好**：字段重排、连续内存 | 5%~30%（受限于内存带宽） | 低 |

> ⚠️ **最重要的纪律**：这张表的顺序意味着——**先做「减少分配」，别急着「换更快的库」**。换库是买别人的常数因子，减少分配是把常数因子归零。

---

## 30.3 底层实现与手法

### 30.3.1 分配优化：从「为什么逃逸」开始

Go 的分配路径本身极快（`runtime/mallocgc` → `mcache` → `span`，无锁，第 15 章），但**只要上了堆，就要被 GC 扫、被写屏障管**。所以第一步永远是看逃逸：

```go
package main

import "fmt"

type Opts struct{ Name string; N int }

// ❌ 三层罪状：
//  1. fmt.Sprintf 把参数装箱成 []any → 逃逸
//  2. 返回 *Opts 让 Opts 逃逸（若调用方只读，值返回更好）
//  3. 中间 string 再转 []byte，多一次分配
func bad(name string, n int) []byte {
	o := &Opts{Name: fmt.Sprintf("%s-%d", name, n), N: n}
	return []byte(fmt.Sprintf("%+v", o))
}

// ✅ 全栈上 + 一次分配 + 预分配容量
func good(name string, n int) []byte {
	var b []byte
	b = append(b, "name="...)
	b = append(b, name...)      // 无中间 string
	b = append(b, " n="...)
	b = strconv.AppendInt(b, int64(n), 10)
	return b                     // 只有返回的这一块在堆上
}
```

```bash
go build -gcflags='-m' main.go   # 看 escapes to heap 的每一行
```

**四个高频动作**：
1. **预分配**：`make([]T, 0, n)`、`b.Grow(n)`、`m := make(map[K]V, n)`——map 也要预分配，否则会多次扩容（第 12 章）。
2. **`strings.Builder` 替 `+=`**：`s += x` 每次都分配新字符串，O(n²) 字节拷贝；`Builder` 是 O(n)。
3. **`strconv.AppendXxx` 替 `strconv.Itoa`+拼接**：前者直接追加到已有 `[]byte`，零中间对象。
4. **避免 `any` 装箱**：`log.Info("count", n)` 里的 `n` 会进 `[]any`，逃逸；用 `slog.Int("count", n)` 这类强类型 Attr（第 35 章）。

### 30.3.2 `sync.Pool`：只放「可重建的临时缓冲」

```go
var bufPool = sync.Pool{
	New: func() any { return make([]byte, 0, 64<<10) }, // 64KB 缓冲
}

func encodeSSE(w io.Writer, ev Event) error {
	buf := bufPool.Get().([]byte)
	defer func() {
		buf = buf[:0]
		bufPool.Put(buf) // ⚠️ 归还前必须清空长度，也不要留着引用
	}()

	buf = append(buf, "data: "...)
	buf, _ = appendJSON(buf, ev) // 本地辅助函数：把 ev 追加成 JSON 字节，避免中间分配
	buf = append(buf, '\n', '\n')
	_, err := w.Write(buf)
	return err
}
```

三条铁律：① **Pool 会被 GC 清空**（每轮 GC 把 shared 推进 victim，再一轮就丢，机制见第 22 章），所以它只适合「丢了能重建」的缓冲；② **Put 前把 slice 截到 0**，否则你把它整个内容（含敏感数据）留在池里；③ **不要放带 `*uintptr`/外部资源（连接、fd、锁）的对象**，池里的对象可能随时消失。

### 30.3.3 字符串与字节：最容易被忽视的 40%

| 场景 | ❌ 慢写法 | ✅ 快写法 | 说明 |
| --- | --- | --- | --- |
| 拼接定长 | `a + "-" + b` | `strings.Builder` + `Grow` | 前者 2 次分配 |
| 数字转字符串 | `strconv.Itoa(n)` | `strconv.AppendInt(b, n, 10)` | 后者零分配 |
| 读 string 的字节 | `[]byte(s)` | `unsafe.Slice(unsafe.StringData(s), len(s))`（只读） | 后者零拷贝，第 13 章 |
| 只读解析 | `strings.Split` | `strings.Cut` / `strings.FieldsFunc` / 手写 `IndexByte` | Split 分配整个 slice |
| 大小写/前缀 | `strings.ToLower` | `strings.EqualFold` | 前者分配 |
| 流式读 | `io.ReadAll` | `bufio.Scanner`/`Reader.ReadSlice` | 前者整块进内存 |

> 🩸 **一个真实事故**：SSE 转发里写 `full += token` 累积全文，2000 个 token × 平均 4 字节 = 一次请求产生 2000 次分配、8MB 拷贝。改成 `strings.Builder` 后单请求分配从 2000 降到 1。

### 30.3.4 锁优化三板斧

**第一斧：分片（sharding）**。把一个锁拆成 N 个（N 取 2 的幂、按 key 哈希），冲突概率降到 1/N：

```go
type ShardedCounter struct {
	shards [16]struct {
		v atomic.Int64
		_ [56]byte // 填充到 64 字节缓存行，避免 false sharing（第 14 章）
	}
}

func (c *ShardedCounter) Add(key uint64, delta int64) {
	c.shards[key%16].v.Add(delta)
}

func (c *ShardedCounter) Total() int64 {
	var t int64
	for i := range c.shards {
		t += c.shards[i].v.Load()
	}
	return t
}
```

**第二斧：原子替锁**。计数、开关、一次性初始化——能 `atomic` 就别 `Mutex`。1.19 起有泛型化的 `atomic.Int64/atomic.Pointer[T]`，比 `atomic.AddInt64(&x, 1)` 更难写错。

**第三斧：无锁读（快照 / COW）**。读多写少的配置、路由表、工具注册表：写时构造新副本，然后用一次 `atomic.Pointer.Store` 原子换掉。读路径**完全没有锁**：

```go
type Registry struct {
	snap atomic.Pointer[map[string]Tool] // 只读快照
}

func (r *Registry) Get(name string) (Tool, bool) {
	m := r.snap.Load()
	if m == nil {
		return nil, false
	}
	t, ok := (*m)[name]
	return t, ok
}

func (r *Registry) Register(name string, t Tool) {
	old := r.snap.Load()
	next := make(map[string]Tool, len(*old)+1)
	for k, v := range *old {
		next[k] = v
	}
	next[name] = t
	r.snap.Store(&next) // 原子发布，读侧永不阻塞
}
```

注意两条约束：**写路径必须是串行的**（否则两次并发 Register 会丢更新，用一把只为写服务的 `sync.Mutex`），**map 内容发布后不得再修改**（发布不可变对象才有意义，见第 23 章）。

### 30.3.5 IO 批处理：把 syscall 次数降下来

`fmt.Fprintln(w, line)` 在一个 HTTP 响应上就是一次 `write(2)`。 SSE 每 token 一次写 = 2000 次 syscall。三层改法：

```go
// ① bufio：攒够 4KB（DefaultBufferSize）或显式 Flush 才真正写
bw := bufio.NewWriterSize(w, 32<<10)
for _, tok := range tokens {
	bw.WriteString("data: ")
	bw.WriteString(tok)
	bw.WriteString("\n\n")
	// 流式场景必须"攒一小批再 flush"，既降 syscall 又让客户端有渐进感
	if bw.Buffered() >= 512 {
		if err := bw.Flush(); err != nil {
			return err
		}
		f.Flush() // http.Flusher：把 bufio 的内容推给 HTTP 层
	}
}
bw.Flush()

// ② net.Buffers：一次 writev 写多块，避免用户态 concat
bufs := net.Buffers{header, body, trailer}
bufs.WriteTo(conn) // 底层走 writev，内核直接聚合
```

**要点**：Go 的 `io.Copy(dst, src)` 会自动选路径——`src` 实现 `WriterTo` 或 `dst` 实现 `ReaderFrom` 时走零拷贝（`*net.TCPConn.ReadFrom` 对 `*os.File` 会走 **`sendfile(2)`**）。所以**别手写 4KB 循环读写的「优化」**，那会让 `sendfile` 失效。零拷贝的完整讨论在第 32 章。

### 30.3.6 缓存友好：先看顺序，再看字段

- **连续内存 > 指针跳转**：`[]Point` 遍历比 `[]*Point` 快 2~5 倍（每跳一次指针 = 一次可能的 cache miss）。所以「为每个元素分配指针」是性能陷阱，也是分配陷阱。
- **map 遍历 ≠ slice 遍历**：map 的 bucket 散布在堆上，随机访问；热路径上能换成排序 slice + 二分，就换。
- **字段重排减少 padding**、**避免 false sharing**：见第 14 章，`fieldalignment` 分析器能自动指出来。

---

## 30.4 Java 对照：为什么「小对象随便 new」在 Go 里更贵

| 主题 | Java 侧的事实 | Go 的真相 | 你该怎么改习惯 |
| --- | --- | --- | --- |
| 小对象分配 | TLAB 里指针碰撞，~几 ns；年轻人代回收便宜 | mcache 也快（~25ns），但**每次 GC 都要扫所有堆对象**，短命对象同样收费 | 不要"随手造对象"，尤其别在循环里 |
| 标量替换 | C2 能把不逃逸的对象拆成局部变量（连分配都没有） | **Go 不做标量替换**，逃逸就是真分配 | 想省分配只能靠栈变量 + 复用 |
| 字符串拼接 | `+=` 在循环里被 JIT 优化成 `StringBuilder`（有限场景） | `+=` 每次真分配，**没有拯救者** | 一律 `strings.Builder` |
| 线程本地 | `ThreadLocal` 随便用 | 用 `sync.Pool`（会被 GC 清）或显式传递；`context` 不是等价物 | 别把 ThreadLocal 的用法直译 |
| 锁 | `synchronized` 有偏向锁/轻量级锁历史优化（JDK 15+ 移除偏向锁）；`ConcurrentHashMap` 有 CAS | `sync.Mutex` 不可重入、无偏向锁；分片要自己写 | 用 `atomic` + 分片 + 快照 |
| 容器 | `ArrayList` 扩容策略成熟，`HashMap` 惯用 | `append` 扩容公式不同（第 11 章）；map 更要预分配 | 一律给 `cap` |
| 缓存 | JVM 有对象头，偏向"对象即单元" | **无对象头**，struct 更紧凑，数组化收益更大 | 用 `[]struct` 而非 `[]*struct` |

> 💡 一句话：**JVM 用「运行时聪明」替你兜住了一半的分配浪费；Go 用「编译期确定」把这份责任交还给你。** 在 Go 里，「少分配」是一种写代码的习惯，不是一种调优技巧。

---

## 30.5 框架与工程落地

**开源框架里的同款手法**（直接抄思路）：

- **Gin**：用 `sync.Pool` 复用 `*gin.Context`（`Engine.pool`）。所以**不能在 goroutine 里用 `c`**，必须 `c.Copy()`。
- **fasthttp**：把 `RequestCtx`、`URI`、`Args`、`Header` 全部池化复用，因此性能比 `net/http` 高 2~4 倍——代价是**你拿到的所有 `[]byte` 只在 handler 期间有效**，跨 goroutine 持有就是 use-after-free。这是「池化换性能」的最激进样本。
- **sonic**：内部有 buffer 池与 JIT 生成的编解码器（第 31 章），本质也是「复用 + 消除反射」。
- **eino / langchaingo**：流式链路里最该检查的是「每个 chunk 是否都 `json.Marshal` 了一次」以及「message 切片是否每轮都 append 导致扩容」——这两个是 Go 版 Agent 框架最常见的分配热点。

**优化台账**（贴在团队 wiki，每次优化一条，谁都能审）：

| 日期 | 场景 | 指标（前 → 后） | 改动 | 证据 | 风险 / 回滚 |
| --- | --- | --- | --- | --- | --- |
| 03-11 | RAG 请求 | 11840→870 allocs/op；P99 210→130ms | Builder 预分配 + buffer 池 + 日志惰性 | `benchstat old.txt new.txt`，p=0.001 | Pool 归还前清空长度；可单文件回滚 |
| 03-14 | 工具注册表读 | Mutex 延迟占 18%→0.2% | `atomic.Pointer` 快照 + 写侧单锁 | mutex profile `-sample_index=delay` | 写路径必须串行；已加单测并发 Register |
| 03-18 | SSE 转发 | syscall 2000→12 次/请求 | `bufio.Writer` + 512B 阈值 flush | `strace -c -f -p` | 客户端感知延迟略增（可调阈值） |

台账里每一行都必须有**「证据」**和**「回滚方式」**——这是把「优化」和「改装」区分开的东西。

---

## 30.6 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| 加了 `sync.Pool` 反而更慢 | 对象太小（池开销 > 分配开销），或 GC 把池清空了 | 只池化 ≥ 几 KB 的对象；用 benchmark 验证 | 30.3.2 |
| 从 Pool 拿到的 buffer 里有上一个人的数据 | 归还时没截断长度 | Put 前 `buf = buf[:0]`；敏感数据先清零 | 30.3.2 |
| goroutine 里用 Gin 的 `c`，panic 或数据串了 | `*gin.Context` 是池化对象 | `c := c.Copy()` 再传进 goroutine | 30.5 |
| 拆了锁之后计数偶尔少一次 | 快照/分片写路径没串行化 | 写路径一把锁；分片则每片独立原子 | 30.3.4 |
| `strace` 显示每个响应几千次 `write` | 没包 `bufio`，或每个 token flush 一次 | 按阈值批量 flush | 30.3.5 |
| 手写 32KB 循环读写大文件，反而更慢 | 破坏了 `io.Copy` 的 `sendfile` 路径 | 直接用 `io.Copy` / `io.CopyBuffer` | 32.5 |
| 微基准显示快 3 倍，线上毫无变化 | 优化的不是瓶颈（第 29 章归因错误） | 先 trace/pprof 定位瓶颈再动手 | 29.5.3 |
| 为了性能把代码写得没人看得懂 | 数字没有证据支撑 | 台账里必须写 benchstat 结果；收益 <5% 就别改 | 30.5 |

---

## 30.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 优化顺序 | 不分配 > 少分配 > 池化 > 少 syscall > 少抢锁 > 缓存友好 |
| 一次分配的账单 | 分配本身最便宜，GC 扫描与 cache miss 才贵 |
| 预分配 | `make(..., 0, n)`、`Builder.Grow`、`make(map, n)` |
| `+=` | 在 Go 里是真分配，一律换 `strings.Builder` |
| 数字转串 | `strconv.AppendInt` 优于 `Itoa` + 拼接 |
| `sync.Pool` | 只放可重建的临时缓冲；会被 GC 清空；Put 前截断 |
| 锁三板斧 | 分片（hash 取模）/ 原子 / 快照无锁读 |
| 批处理 | `bufio` 攒批、`net.Buffers` 走 writev、别破坏 `sendfile` |
| 缓存友好 | `[]struct` > `[]*struct`；切片遍历 > map 遍历 |
| 纪律 | 先测量（pprof/benchstat）→ 单点改 → 台账记录 → 可回滚 |

## 30.8 自检题

1. **一个函数从 30 次分配降到 3 次，但 benchmark 显示只快了 8%。这个结果合理吗？**
   > 要点：合理。benchmark 只测 CPU 时间，若那 27 次分配是小对象、mcache 命中、且该函数不是整体瓶颈，CPU 层面的收益就有限；真正的收益会出现在**高并发下的 GC 压力**（`gctrace` 的 assist 下降、P99 尾延迟下降）。所以「降低分配」的价值要用 `-benchmem` 的 allocs/op + 线上 GC 指标一起衡量，而不是只看 ns/op。

2. **为什么给 `sync.Pool` 放在 HTTP handler 里安全，把它当业务缓存就不安全？**
   > 要点：Pool 的语义是「可能随时消失、内容不保证」。缓冲这类**丢了能立刻重建**的东西符合语义；业务缓存（会话、embedding 结果）丢了就是 bug，而且 Pool 没有淘汰策略、没有 TTL、还会被 GC 每两轮清空（victim cache）。业务缓存要用 LRU / TTL 结构（第 45 章）。

3. **你要优化一个读多写少的工具注册表。写出「三板斧」里你选哪一斧，以及它的正确性前提。**
   > 要点：选「快照 + `atomic.Pointer`」。前提：① 写路径必须串行（一把只服务写入的 Mutex）；② 发布出去的 map 之后绝不能再修改（构造新副本再原子发布）；③ 读侧拿到的是只读视图，需要线程安全就靠「不可变」而不是「加锁」。这样读路径零锁、零分配（除了写入时复制）。

## 30.9 延伸阅读

- `$GOROOT/src/runtime/malloc.go`（`mallocgc`、tiny allocator）、`runtime/mcache.go`
- `sync.Pool` 的实现：`$GOROOT/src/sync/pool.go`（较新版本把运行时侧部分移到了 `runtime/syncpool.go`，以你本地 `$GOROOT` 为准）
- `$GOROOT/src/strings/builder.go`、`strconv/atoi.go`（`AppendInt` 的实现）
- `$GOROOT/src/io/io.go`（`Copy` 的 `WriterTo`/`ReaderFrom` 分派逻辑）
- fasthttp 的 `README` 关于「ctx 只在 handler 期间有效」的说明；Gin `Context.Copy` 的文档
- `golang.org/x/tools/go/analysis/passes/fieldalignment`

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 29 章 pprof 与 trace 实战](29-pprof-trace.md)
- 相关：逃逸分析与字段对齐见[第 14 章](14-memory-layout-escape.md)；分配器见[第 15 章](15-allocator.md)；`sync.Pool`/`Mutex` 实现见[第 22 章](22-sync-primitives.md)；字符串与零拷贝见[第 13 章](13-string-bytes-rune.md)；GC 与 assist 见[第 28 章](28-gc.md)
- 下一章：[第 31 章 JSON 编解码：性能与协议设计](31-json-codec.md)
