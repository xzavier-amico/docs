# 13. string / []byte / rune 与零拷贝

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 数据结构与内存 ｜ **编号**：13 / 54
> **本章定位**：Go 的 `string` 是「16 字节 header + 一堆你无权修改的字节」，不是 Java 的 `String` 对象。这一章把 UTF-8、`len` vs 字符数、`Builder`、`unsafe.String` 零拷贝、拼接性能四条线一次打通，最后落到「流式处理大 prompt」。全章都在回答一个问题：**哪些地方会悄悄多分配一份内存。**
> **读法**：13.2 的两张图标必须记住；13.4 的四种拼接方式对比建议自己跑一遍 bench；13.6 的坑表可以直接当 code review checklist。

---

## 13.1 引子：一次 400 Bad Request 的截断

你的 Agent 网关要限制 prompt 长度。需求很朴素：**超过 4000 字节就截断**。于是你写：

```go
func truncate(s string, n int) string {
	if len(s) > n {
		return s[:n] // 「取前 n 个字符」，对吧？
	}
	return s
}
```

上线一周，中文用户开始报 `400 Bad Request: invalid utf-8 sequence`。你打了个日志：

```
原始: 请帮我总结这篇文档的要点，重点看第三章关于并发调度的部分
截断: 请帮我总结这篇文档的要点，重点看第三章关于并发调度的部<0xE5>
```

最后一个字节 `0xE5` 是一个 3 字节 UTF-8 汉字的首字节，**它的后两个字节被切掉了**。于是你手里拿到了一个"不是合法 UTF-8 的 Go string"——注意，**Go 不会报错，它甚至允许存在非法 UTF-8 的 string**，因为 `string` 本质上就是"只读字节数组"。报错的是下游的 OpenAI API 的 JSON 编码器。

修法三行：

```go
func truncateUTF8Safe(s string, maxBytes int) string {
	if len(s) <= maxBytes {
		return s
	}
	// 从 maxBytes 往前退，直到找到一个 UTF-8 起始字节
	cut := maxBytes
	for cut > 0 && !utf8.RuneStart(s[cut]) {
		cut--
	}
	return s[:cut]
}
```

这个 bug 一次性暴露了本章的全部主题：

1. **`len(s)` 是字节数，不是字符数。** Java 的 `s.length()` 至少还是"UTF-16 code unit 数"，比字节数更接近直觉。
2. **`string` 是不可变 header + 底层字节，`s[:n]` 不复制、不校验。**
3. **`string` 与 `[]byte` 的转换在默认语义下是要复制的**——而这份复制在 Agent 的 token 流水线上是白烧的 CPU。
4. 中文/emoji 用户永远是被 UTF-8 问题打脸的第一批人。

> 🎯 **本章主线**：Java 的 `String` 是"对象"（有 header、有 cached hash、有 charset），Go 的 `string` 是"两个机器字 + 一段没人敢改的内存"。这个差异决定了性能优化空间（零拷贝）和所有的坑（长度、切片、非法 UTF-8）。

---

## 13.2 直觉模型

### 13.2.1 `string` 的内存布局

```go
// runtime/string.go
type stringStruct struct {
	str unsafe.Pointer // 指向底层字节数组的首字节（不是 NUL 结尾！）
	len int            // 字节数
}
```

```
 栈上/结构体里的 string 值（16 字节 @amd64）
 ┌──────────────┬──────────────┐
 │ str (8B) ────┼─► len (8B)=11│
 └──────┬───────┴──────────────┘
        │
        ▼   底层字节（不可变，可能在 rodata 或堆上）
      ┌──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┐
      │G │o │语│  │  │  │  │  │  │  │  │   ← 注意：没有 '\0' 结尾
      └──┴──┴──┴──┴──┴──┴──┴──┴──┴──┴──┘
       ↑     ↑
       s[0]  s[2]（可以用 s[i] 索引，得到 byte）

  s[0:2] = "Go"，s[0:2] 只是复制了 header 的 16 字节，指向同一段内存
```

三个必须内化的结论：

- **赋值 `s2 := s1` 只复制 16 字节 header，不复制字节**。所以把 `string` 传进函数非常廉价（两个机器字）。
- **切片 `s[i:j]` 也零拷贝**——这既是性能红利，也是内存泄漏的来源（见 13.4.3）。
- **没有 NUL 结尾**。传 C 函数必须 `C.CString`（会 `malloc` + 拷贝，还得手动 `C.free`）。

### 13.2.2 UTF-8 与 rune

`rune` 就是 `int32`，表示一个 Unicode 码点（code point）。Go 的 `string` 用 **UTF-8** 编码存 rune，长度可变：ASCII 1 字节，中文 3 字节，emoji 4 字节。

```
字符串 "Go语言" 的字节与 rune 对应关系

 字节索引:  0   1   2   3   4   5   6   7   8
 ASCII:   [G] [o] [ ] [ ] [ ] [ ] [ ] [ ]
 实际:    [G] [o] [E8][AF][AD] [E8][A8][80]
                    └──语────┘ └──言────┘

 len("Go语言")          = 8   （字节）
 utf8.RuneCountInString = 4   （字符）
 len([]rune("Go语言"))  = 4   （但会分配一个 int32 数组！）

 for i, r := range s  →  (0,'G') (1,'o') (2,'语') (5,'言')   // i 是字节偏移！
```

**反直觉点**：`range` 一个 string 时，第一个返回值 `i` 是**字节偏移**，不是第几个字符。所以 `for i := range s` 拿到的是 `0,1,2,5` —— 如果你拿它当"字符下标"去拼另一个数组，必然错位。

**非法 UTF-8 的解码行为**：遇到非法字节，`range` 会吐出一个 `utf8.RuneError`（`U+FFFD`，即 `'\uFFFD'`），并且**宽度算 1 字节**。所以"错误的 UTF-8 不会崩，只会变成一堆 `�`"——这正是它难以发现的原因。

### 13.2.3 Java 对照：两个 `length()` 完全不是一回事

| 维度 | Java `String` | Go `string` |
| --- | --- | --- |
| 本质 | 对象（`byte[] value` + `int hash` + `byte coder`） | 16 字节 header + 裸字节 |
| 单值大小 | 对象头 16B + 字段 + 数组头 16B | **16 字节**，数据不带 header |
| 长度语义 | `length()` = **UTF-16 code unit 数** | `len()` = **字节数** |
| `"🎉"` 的长度 | `length()=2`，`codePointCount()=1` | `len=4`，`utf8.RuneCountInString=1` |
| 哈希缓存 | 有（`hash` 字段首次计算后缓存） | **没有**（每次 map 查找都重算，但有硬件加速） |
| 编码 | 内存里可能是 LATIN1 或 UTF-16（compact strings） | 永远是 UTF-8 字节 |
| 空值 | 可以为 `null` | 不能为 `nil`，零值是 `""` |
| 子串 | JDK7u6 起**复制**，不会泄漏 | `s[i:j]` **零拷贝**，会拖住整个底层数组 ⚠️ |
| 拼接 | 编译器用 `invokedynamic` + `StringConcatFactory` | 调 `runtime.concatstring2/3/4/5` 或 `concatstrings` |

> 💡 **最容易踩的一条**：Java 从 7u6 起 `substring` 是复制的，所以你**从 Java 带过来"子串很安全"的肌肉记忆**。Go 反过来：`s[10:20]` 会让那 10 个字节所在的整个底层数组（可能是 10MB 的原始文档）全都活着，GC 一个字节都收不回来。详见 13.4.3。

---

## 13.3 底层实现：转换与拼接时谁在分配

### 13.3.1 三个 runtime 函数决定一切

```go
// runtime/string.go（简化）
func slicebytetostring(buf *tmpBuf, b []byte) string    // []byte → string：拷贝
func slicebytetostringtmp(b []byte) string             // []byte → string：零拷贝（编译器特例）
func stringtoslicebyte(buf *tmpBuf, s string) []byte   // string → []byte：拷贝
func concatstrings(buf *tmpBuf, a []string) string     // 多段拼接
```

`slicebytetostringtmp` 是编译器**偷偷用的特例**：只在你写 `m[string(b)]`、`switch string(b)`、`string(b) == "x"` 这类"用完即弃"的场景下使用（源码注释里叫 `bytes as string for map lookups`）。这意味着：

```go
key := string(b)          // 复制了一份（因为 key 会被你保存/传出去）
if v, ok := m[string(b)]; ok { } // 不复制！编译器识别为临时值
```

这是 Go 编译器少数"贴心"的地方，也是**为什么查 map 时不用手动优化 `string(b)`**。

### 13.3.2 `strings.Builder` 为什么快

```go
// strings/builder.go（简化，字段名真实）
type Builder struct {
	addr *Builder // 用于检测「Builder 被按值复制」——复制后 CopyCheck 会 panic
	buf  []byte
}

func (b *Builder) WriteString(s string) (int, error) {
	b.copyCheck()
	b.buf = append(b.buf, s...) // 就是 append，摊还 O(1)
	return len(s), nil
}

func (b *Builder) String() string {
	// 关键：零拷贝！把 buf 直接重解释成 string，不再复制一遍
	return unsafe.String(unsafe.SliceData(b.buf), len(b.buf))
}
```

两个设计亮点：

- **`String()` 零拷贝**。因为 `Builder` 后续只会往 `buf` 末尾追加（`len` 以后的内存），已返回的 string 看到的 `buf[0:len]` 内容永不变。
- **`addr` 字段防止值拷贝**。`Builder` 只应该被指针使用；`b1 := *b` 这种复制会让两个 Builder 共享 `buf`，后续写入会"撕开"已返回的 string。所以 `copyCheck()` 一旦发现 `b.addr != b` 就 panic。

对应的 Java 类比：`StringBuilder`（非线程安全，容量翻倍增长，`toString()` **会复制**一份 `String`）。所以 Go 的 `Builder.String()` 比 Java 的 `toString()` **少一次分配**。

### 13.3.3 四种拼接方式，谁在什么时候赢

```go
// 可运行基准：四种拼接方式（go run main.go）
package main

import (
	"bytes"
	"fmt"
	"strings"
	"testing"
)

const n = 1000

var parts = func() []string {
	p := make([]string, n)
	for i := range p {
		p[i] = "token-" // 一段 prompt 片段
	}
	return p
}()

func bench(name string, f func() string) {
	res := testing.Benchmark(func(b *testing.B) {
		for i := 0; i < b.N; i++ {
			f()
		}
	})
	fmt.Printf("%-18s %8d ns/op  %8d B/op  %4d allocs/op\n",
		name, res.NsPerOp(), res.AllocedBytesPerOp(), res.AllocsPerOp())
}

func main() {
	bench("+ 循环", func() string {
		s := ""
		for _, p := range parts {
			s += p
		}
		return s
	})
	bench("strings.Join", func() string {
		return strings.Join(parts, "")
	})
	bench("strings.Builder", func() string {
		var sb strings.Builder
		for _, p := range parts {
			sb.WriteString(p)
		}
		return sb.String()
	})
	bench("bytes.Buffer", func() string {
		var buf bytes.Buffer
		for _, p := range parts {
			buf.WriteString(p)
		}
		return buf.String()
	})
}
```

典型输出（amd64，数量级参考，绝对值随机器变化）：

```
+ 循环               9000000 ns/op  61000000 B/op     999 allocs/op
strings.Join           3000 ns/op      6144 B/op        1 allocs/op
strings.Builder        3500 ns/op      6144 B/op        1 allocs/op
bytes.Buffer           4000 ns/op     12288 B/op        2 allocs/op
```

结论非常干脆：

| 方式 | 复杂度 | 分配次数 | 什么时候用 |
| --- | --- | --- | --- |
| `s += x` | **O(n²)** | n 次 | **只用于 2~3 段的固定拼接**（编译成 `concatstring2/3`，一次分配） |
| `strings.Join` | O(n) | 1 次 | 你手里**已经有 `[]string`**，且需要分隔符 |
| `strings.Builder` | O(n) | 1 次 | **流式**逐段构建（最通用）；`Grow()` 预分配更好 |
| `bytes.Buffer` | O(n) | 1~2 次 | 需要 `Write([]byte)`、`ReadFrom`、`io.Writer` 接口 |
| `fmt.Sprintf` | O(n) 但慢 | 多次 | 只有真的需要格式化时才用；**循环里用它是灾难** |

> ⚠️ `+` 的那一列不是"慢 3000 倍"的夸张修辞：1000 段拼下来是 **61MB 的垃圾**。而这段代码在很多 Java 项目里是 `StringBuilder` 自动帮你优化的（javac 会把循环体内的 `+=` 展开成 `StringBuilder`），**Go 的编译器不做这个改写**——因为"在背后偷偷改变分配行为"违反 Go 的显式原则。

### 13.3.4 `unsafe.String` / `unsafe.StringData`：官方认证的零拷贝

Go 1.20 加了两个函数（`unsafe` 包），把以前只能靠 `*(*string)(unsafe.Pointer(&b))` 这种黑暗仪式做的事，变成了有文档、有 `checkptr` 支持的正规用法：

```go
func String(ptr *byte, len IntegerType) string  // 用 *byte + 长度造一个 string（不拷贝）
func StringData(str string) *byte               // 拿到 string 底层字节的首地址（只读！）
```

配套的还有 1.20 的 `unsafe.SliceData([]T) *T` 和 1.17 的 `unsafe.Slice(*T, len) []T`。

```go
// 可运行：两种方向的无拷贝转换 + 危险点标注
package main

import (
	"fmt"
	"unsafe"
)

// []byte → string，零拷贝。⚠️ 返回的 string 与 b 共享内存：
// 之后修改 b 就等于修改了一个"不可变"的 string（违反语言契约）。
func bytesToString(b []byte) string {
	return unsafe.String(unsafe.SliceData(b), len(b))
}

// string → []byte，零拷贝。⚠️ 返回的 []byte 可以被写，同样违反契约。
func stringToBytes(s string) []byte {
	return unsafe.Slice(unsafe.StringData(s), len(s))
}

func main() {
	b := []byte("hello")
	s := bytesToString(b)
	fmt.Println(s)       // hello
	b[0] = 'H'           // ⚠️ 现在 s 也变了
	fmt.Println(s)       // Hello —— 因为 s 和 b 是同一块内存

	bs := stringToBytes("world")
	bs[0] = 'W'          // ⚠️ 后果自负（若底层是只读 rodata 会直接 SIGSEGV）
	fmt.Println(string(bs))
	_ = unsafe.Sizeof(s) // 16
}
```

**什么时候可以安全地用？** 只在这三条同时满足时：

1. 源数据（`b`）的生命周期 ≥ 目标（`s`）的使用期——**这是最容易出错的地方，底层数组一被 GC 回收，string 就是悬空指针**；
2. 源数据在目标存活期内**不会被修改**；
3. 你清楚目标不会被写（`stringToBytes` 那条路基本只该用在"只读扫描"如解析器里）。

典型的安全场景：
- HTTP body 解析：`buf []byte` 由框架持有到 handler 结束，中间把某段字段零拷贝成 string 当 map key 查——**查询完就丢**，安全。
- 替代 `string(b)` 做**只读**判断：`if bytesToString(b) == "GET"` —— 其实这种场景编译器已经帮你走 `slicebytetostringtmp` 了，不用手写。
- 高性能 JSON 库（sonic）在解析时把 `[]byte` 里的字符串段零拷贝成 string 塞进结构体——它靠"整个 body 的 `[]byte` 由调用方保证存活"来兜底。

> 🩸 **事故形态**：`func parseLine(b []byte) string { return unsafe.String(&b[0], len(b)) }` —— `b` 是 `bufio.Scanner.Bytes()` 返回的切片，下一次 `Scan()` 就复用了同一块缓冲。于是所有"解析出来的字符串"在下一次迭代后全变成同一行内容。**零拷贝不是免费的，它把内存生命周期的责任从 runtime 转移给了你。**

### 13.3.5 流式处理大 prompt

一个 200KB 的 system prompt + 50 轮对话拼接，是 Agent 的日常热点。三条纪律：

```go
// 1) 预分配：先算总长，一次到位，绝不让 Builder 反复翻倍
func buildPrompt(system string, history []string) string {
	n := len(system)
	for _, h := range history {
		n += len(h) + 1 // +1 是 '\n'
	}
	var sb strings.Builder
	sb.Grow(n) // ← 这一行的收益比后面所有优化加起来都大
	sb.WriteString(system)
	for _, h := range history {
		sb.WriteByte('\n')
		sb.WriteString(h)
	}
	return sb.String()
}

// 2) 能不拼就不拼：直接用 io.Writer 流出去（SSE / HTTP body）
func writePrompt(w io.Writer, system string, history []string) error {
	if _, err := io.WriteString(w, system); err != nil {
		return err
	}
	for _, h := range history {
		if _, err := io.WriteString(w, "\n"+h); err != nil {
			return err
		}
	}
	return nil
}

// 3) 从大 string 里只留一段：必须 Clone，否则拖住整个底层数组
func lastUserTurn(bigDoc string) string {
	i := strings.LastIndex(bigDoc, "user:")
	if i < 0 {
		return ""
	}
	return strings.Clone(bigDoc[i:]) // Go 1.18+；等价于 Java 的 new String(sub)
}
```

**`strings.Clone` 是本章最容易被忽略的函数**：它把子串**复制**成独立的一份，切断与原始大 string 的联系。一个 5MB 的文档里 `s[1000:1100]` 拿 100 字节，却让 5MB 永远不可回收——这种 bug 在 pprof 的 `inuse_space` 里表现为"某个小字段吃掉了半个堆"。

---

## 13.4 踩坑清单（CR 可直接抄）

| # | 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- | --- |
| 1 | 中文/emoji 被截断成非法 UTF-8，下游 400 | `s[:n]` 按**字节**切，切断多字节 rune | 退到 `utf8.RuneStart(s[cut])` 为止；或 `[]rune` 后按字符切（有分配） | 13.1 |
| 2 | `len(s)` 算出来的"字符数"对不上 | `len` 是字节数，`range` 的 `i` 是字节偏移 | 字符数用 `utf8.RuneCountInString(s)`；别用 `len([]rune(s))`（会分配） | 13.2.2 |
| 3 | 循环里 `s += x`，CPU 打满 + GC 飙 | 每轮一次 `concatstring2`，O(n²) | `strings.Builder` + `Grow(total)`；已有 `[]string` 就用 `strings.Join` | 13.3.3 |
| 4 | 小字段吃掉半个堆（pprof 里） | `s[a:b]` 零拷贝，拖住整个底层数组 | `strings.Clone(s[a:b])` | 13.3.5 |
| 5 | `unsafe.String` 之后内容"自己变了" | 与 `[]byte` 共享内存，源被改/被复用 | 只在"只读 + 同生命周期"时用；否则老实 `string(b)` | 13.3.4 |
| 6 | `string(int)` 拿到奇怪字符 | Go 把整数当 **rune** 转，不是数字转字符串 | 用 `strconv.Itoa(i)`；`go vet` 会警告 `conversion from int to string` | 13.2.2 |
| 7 | `string(b)` 与 `[]byte(s)` 在热点路径上产生大量分配 | 默认语义就是**复制** | 读 map 的 `string(b)` 编译器已优化；其它情况考虑 `unsafe` 或改写 API 直接吃 `[]byte` | 13.3.1 |
| 8 | 传 C 函数时字符串被截断/乱码 | C 期待 NUL 结尾，Go 的 string 没有 | `C.CString(s)` + `defer C.free()`；注意它是 UTF-8 字节原样拷贝 | 第 16 章 |
| 9 | `Builder` 报 `strings: illegal use of non-zero Builder copied by value` | Builder 被按值复制（含**放进结构体后再复制**） | 永远用 `*strings.Builder`；或用 `bytes.Buffer`（可赋值但有锁开销） | 13.3.2 |
| 10 | JSON/日志里出现 `�`，数据"看起来少了一半" | 上游产生了非法 UTF-8 字节 | 入口处 `utf8.ValidString` 校验；清洗用 `strings.ToValidUTF8` | 13.2.2 |
| 11 | `for i, r := range s` 里用 `s[i+1]` 越界或错位 | `i` 是字节偏移，`r` 宽度是 1~4 字节 | 需要宽度用 `r, size := utf8.DecodeRuneInString(s[i:])` | 13.2.2 |
| 12 | `string` 的 map key 查找比预期慢 | 没有 cached hash（对比 Java），每次重算 | 真热点可把 key 换成 `int64`/固定长度数组；或缓存 hash（如 `maphash`） | 第 12 章 |

---

## 13.5 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| `stringStruct` | `{str unsafe.Pointer; len int}`，16 字节，**无 NUL 结尾、无对象头** |
| 赋值/传参 | 只复制 16 字节 header，不复制字节 → 极廉价 |
| `s[i:j]` | 零拷贝，共享底层数组 → **可能拖住整块内存**（`strings.Clone` 解） |
| `len(s)` | 字节数。字符数用 `utf8.RuneCountInString` |
| `range s` | 按 rune 迭代，`i` 是**字节偏移**；非法字节吐 `RuneError` 且宽度 1 |
| `rune` | `int32`，一个 Unicode 码点；`byte` 是 `uint8` |
| 不可变 | 语言契约，但 `unsafe` 能破；破了就是自己背锅 |
| `strings.Builder` | 摊还 O(1) append；`Grow` 预分配；`String()` **零拷贝**；按值复制会 panic |
| 拼接 | 2~3 段用 `+`；多段用 `Builder`/`Join`；循环里绝不用 `+=` |
| 零拷贝 | `unsafe.String` / `unsafe.StringData` / `unsafe.SliceData`（1.20），**责任转移给调用方** |
| 转换 | `string(b)` / `[]byte(s)` 默认复制；`m[string(b)]` 是编译器特例（不复制） |
| 大 prompt | `Builder.Grow` 一次到位；能流式就别拼；子串一定 `Clone` |

## 13.6 自检题

1. **`len(s)` 和 `utf8.RuneCountInString(s)` 在什么情况下不相等？为什么不能用 `len([]rune(s))` 代替后者？**
   > 答案要点：`len` 是字节数，`utf8.RuneCountInString` 是码点数；只要含非 ASCII（中文 3B、emoji 4B）就不相等，且含非法 UTF-8 时后者按"每字节一个 `RuneError`"计。`len([]rune(s))` 结果通常相同，但会**额外分配**一个 `[]rune`（4×N 字节 + header），在热点路径上是白烧内存；`utf8.RuneCountInString` 只用寄存器遍历，零分配。

2. **为什么 `strings.Builder.String()` 可以零拷贝，而 `bytes.Buffer.String()` 要复制？**
   > 答案要点：`Builder` 的语义是**只追加、不回溯修改**，`String()` 返回后继续 `WriteString` 只会写到 `len(buf)` 之后，已返回 string 覆盖的区间内容不变，所以安全。`bytes.Buffer` 支持 `Read`/`Reset`/`Next` 等改变 `buf` 内容或起点的操作，返回的 string 可能在下一刻被覆盖，因此必须复制（`string(b.buf[b.off:])`）。这也是 `Builder` 禁止按值复制（`copyCheck`）的原因。

3. **一个 HTTP handler 里：`body, _ := io.ReadAll(r.Body)`，然后 `parts := strings.Split(string(body), ",")`，把 `parts` 存进全局缓存。有什么问题？怎么改？**
   > 答案要点：`string(body)` 是**一次必要复制**（body 是 `[]byte`，且要长期持有），这步没问题；但 `strings.Split` 出来的每段都**零拷贝指向那同一块 `string` 的底层数组**——缓存里存了 1000 个 key，就等于把整份 body 永久留在堆上，pprof 上表现为"小字符串占大内存"。改法：对要长期存的段做 `strings.Clone`（或 `unsafe` 读完后立刻转成独立的小 string），并给缓存设容量上限。

## 13.7 延伸阅读

- 源码：`$GOROOT/src/runtime/string.go`（`stringStruct` / `slicebytetostring` / `concatstring2..5` / `rawstringtmp`）、`strings/builder.go`（`copyCheck`）、`strings/strings.go`（`Clone`）、`unicode/utf8/utf8.go`（`RuneStart` / `DecodeRuneInString` / `RuneCount`）
- `unsafe` 包文档里 `String` / `StringData` / `SliceData` 三条的完整约束说明（**必读，短且精确**）
- Go 1.20 release notes：「New `unsafe.String`, `unsafe.StringData`, `unsafe.SliceData`」
- 实践参考：`valyala/fasthttp` 的 `bytesconv.go`（`b2s`/`s2b` 的 1.20 前后两版实现）、`bytedance/sonic` 的零拷贝字符串解码
- 相关章节：[11. 数组与切片](11-slice-array.md)（header 与共享底层数组是同一套心智）、[16. unsafe 与 cgo 的边界](16-unsafe-cgo.md)、[30. 性能优化](30-perf-tuning.md)

---

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[12. map 内部结构与 Go 1.24 Swiss Table](12-map-internals.md)
- 下一章：[14. 内存布局、对齐与逃逸分析](14-memory-layout-escape.md)
