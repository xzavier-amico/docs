# 1. 从 Java 到 Go：心智模型重置

> **状态**：`已完成` ｜ **所属**：第一部分 · 心智迁移：Java → Go ｜ **编号**：1 / 50
> **本章定位**：全书唯一一章「讲价值观」的内容。它不教你语法，而是把 Java 世界观里那些**默认成立的假设**逐个拆掉，换成 Go 的假设。后面 49 章都建立在这一章的地基上。
> **读法**：先通读一遍（30 分钟），然后每读完一个部分，回来重读一次。你会发现自己每次读到的重点都不一样。

---

## 1.1 引子：一个 Java 老手的崩溃 30 分钟

假设你写了八年 Java，Spring Boot 玩得很熟，今天第一次写 Go。你打开编辑器，开始凭直觉下手：

```go
// 你的第一版（来自 Java 的肌肉记忆）
package main

public class Agent {          // ❌ 没有 public class，没有文件即类
    private final String name; // ❌ 可见性靠首字母大小写
    @Autowired                 // ❌ 没有注解，也没有容器
    private LlmClient client;
}

func run() {
    try {                                    // ❌ 没有 try/catch
        result := callLlm()
    } catch (Exception e) {                  // ❌ 没有 Exception 体系
        log.error("failed", e);
    }
}
```

编译器会一口气甩给你十几条红字。**关键不在于你不知道语法，而在于你的每一条直觉都是「错的但有道理」**：

- 你想加注解，因为 Java 用注解做了 DI、校验、事务、路由、序列化——**Go 的世界里这些全靠代码生成和显式函数调用**。
- 你想 try/catch，因为 Java 的异常能跨越 10 层调用栈把错误"送上来"——**Go 要求你一层层地把它"传上去"**。
- 你想写 interface + 实现类 + 抽象类，因为 Java 靠继承体系做复用——**Go 连 `extends` 这个关键字都没有**。

这一章的目的，就是让你把这几条「错的但有道理」的直觉，一次性换成 Go 的直觉。

> 🎯 **一句话总结这一章的立场**：
> **Java 的设计目标是「让大团队用丰富的抽象造大系统」；Go 的设计目标是「让普通工程师在五年后还能读懂别人的代码」。**
> 所有语法差异，都是这条目标差异的下游产物。

---

## 1.2 十条设计价值观（每条都对应一个具体的语言决策）

这张表是这一章的核心。**背下来它，比背任何语法都有用**，因为你以后每遇到一个「Go 为什么不做 X」的问题，都能从这张表里推出来。

| # | 价值观 | 对应的语言决策 | Java 里的对应做法 | 代价（Go 付了什么） |
| --- | --- | --- | --- | --- |
| 1 | **显式优于隐式** | 没有注解、没有 AOP、没有反射注入；依赖在 `main` 里手动传 | Spring 的 `@Autowired`、CGLIB 动态代理 | 装配代码变长，框架"魔法"消失 |
| 2 | **组合优于继承** | 没有 `extends`，只有 struct 嵌入和 interface 满足 | 抽象类 + 模板方法 + 多态 | 无法用父类"钩子方法"控制子类 |
| 3 | **值语义优先** | 赋值 = 拷贝；`struct` 是值，不是引用 | 对象引用随手传递，天然共享 | 大结构体拷贝有成本，得主动用指针 |
| 4 | **错误是值，不是控制流** | `error` 是普通返回值，`if err != nil` 显式检查 | 受检异常 / 运行时异常 + `finally` | 代码里大量重复的错误判断 |
| 5 | **并发是一等公民** | `go` 关键字 + channel + 内建调度器 | `ExecutorService` + 并发包（库级别） | 语言层面多了复杂度，团队要懂 GMP |
| 6 | **接口由使用方定义** | 隐式实现，接口通常 1~3 个方法 | 接口由提供方定义（`UserService`） | 找不到"这个类实现了哪些接口"的全局视图 |
| 7 | **零值即可用** | `var buf bytes.Buffer` 直接能用；`sync.Mutex{}` 直接能锁 | `new` + 构造器 + 非空校验 | 有时要区分"零值"和"未设置" |
| 8 | **可读性 > 表达力** | 没有三元运算符、没有方法重载、没有泛型方法、只有一种循环（`for`） | 语法糖丰富，DSL 能力强 | 写起来"啰嗦"，抽象层级上不去 |
| 9 | **编译快、部署简单** | 静态链接单一二进制、无 JVM、秒级全量编译 | JVM 生态、fat jar、JIT 预热 | 峰值性能靠 AOT + PGO 追赶 JIT |
| 10 | **工具统一，禁止审美战争** | `gofmt` 是标准，格式没得选 | Checkstyle / Spotless / 团队争论 | 你无法自定义代码风格 |

> 💡 **一个能让你少走三年弯路的心智锚点**：Go 不是"少了几个特性的 Java"，它是**另一条路线**。就像 Rust 用所有权换安全、Erlang 用不可变换容错，Go 用「抽象能力的上限」换「任何水平的工程师都能维护」。

---

## 1.3 核心断层一：`if err != nil` 的三层解释

这是每个 Java 程序员遇到的第一道墙。大多数人第一反应是：

> "每调一个函数就要写三行 if，这代码还能看？Go 是不是设计缺陷？"

**这个判断在表层成立，在深层完全错误。** 我把这件事拆成三层，你听完会发现它是 Go 最精妙的设计之一。

### 第一层（表层）：为什么不用异常？

因为**异常是一种"看不见的 goto"**。看这段 Java：

```java
// Java：调用者完全看不出这行会抛出什么、会从哪一层冒出来
var resp = client.chat(request);   // 可能抛 IOException / ApiException / NPE ...
```

再看 Go：

```go
resp, err := client.Chat(ctx, req)  // 错误写在签名里：你必须处理它
if err != nil {
    return fmt.Errorf("chat: %w", err)
}
```

在 Java 里，一个方法能抛什么异常，要么靠 `throws`（受检异常，大家嫌烦就全用 RuntimeException 绕过去了），要么只能看文档或看源码。**Go 把"可能失败"这件事提升到了类型系统里**：`(T, error)` 这个返回签名本身就在告诉你"我可能失败"。

### 第二层（中层）：错误是值，所以错误可以被"编程"

因为 `error` 只是一个 interface：

```go
type error interface {
    Error() string
}
```

所以它能被**比较、包装、分类、聚合、写入数据库、发给 LLM 让它自己分析**。这正是 Agent 场景的刚需：

```go
resp, err := client.Chat(ctx, req)
if err != nil {
    switch {
    case errors.Is(err, context.Canceled):
        return nil                   // 用户自己取消的，不作错误上报
    case errors.Is(err, ErrRateLimited):
        return retryWithBackoff(ctx, req)   // 可重试
    case isTransient(err):
        return retryOnce(ctx, req)          // 5xx，重试一次
    default:
        return fmt.Errorf("chat failed: %w", err)  // 不可重试，直接上抛
    }
}
```

这段代码在 Java 里也能写，但你得先定义一个 `ErrorCategory` 枚举、一个自定义异常层次、一个分类器……而在 Go 里，**错误分类学是语言级的、零成本的习惯**。这就是第 7 章的完整主题。

### 第三层（深层）：把控制流降级为数据流

这是最重要的一层，也是"Java 直觉"最容易漏掉的：

| | Java 异常 | Go error |
| --- | --- | --- |
| 错误的流动方式 | 沿着**控制流**逆向跳跃（栈展开） | 沿着**数据流**正向返回 |
| 编译器视角 | 需要 finally / 异常表 / 栈展开元数据 | 和普通返回值毫无区别 |
| 对优化器的友好度 | 有异常的方法难以内联、难以重排 | 内联无阻碍（`error` 是接口，但小函数仍可内联） |
| defer 的执行 | `finally` 块，可读性差（嵌套 try 时） | `defer` 语句就近注册、LIFO 执行 |
| 中间层能否干预 | 只能 catch 完再 throw，容易丢栈 | 每层都能 `%w` 包装，**栈信息是显式构造的** |

在 Go 里，"错误处理"就是"写代码"，没有任何隐藏机制在背后跳来跳去。**可预测性 > 便利性**，这就是取舍。

> ⚠️ **反直觉提醒**：Go 的 `panic` 不是 Java 的 `RuntimeException`。`panic` 只用于"程序处于不可恢复的非法状态"（数组越界、nil 解引用、明确不可恢复的编程错误）。**库代码永远不该用 panic 表达业务失败**——详见第 7、8 章。

---

## 1.4 核心断层二：没有注解与容器，那"魔法"去哪了？

Java 程序员的抽象能力，大量建立在**运行期反射 + 注解 + 动态代理**上：

```
@Service → 容器发现 → 反射注入 → 事务/缓存/鉴权/路由/参数校验 → AOP 织入
```

这套机制的好处是"写业务代码时不写基础设施代码"；坏处是**你在 IDE 里点不进任何实现，出错时栈里全是代理类**：`UserServiceImpl$$EnhancerBySpringCGLIB$$3f2a1b`。

Go 把"魔法"从**运行期**搬到了**编译期**：

| 需求 | Java 的做法 | Go 的做法 |
| --- | --- | --- |
| 依赖注入 | `@Autowired` + 容器 | 在 `main` 里手动传参，或用 `wire` **生成**装配代码（第 34 章） |
| 参数校验 | `@Valid` + Hibernate Validator（反射） | 显式 `if err := req.Validate(); err != nil`，或代码生成 |
| JSON 映射 | Jackson 反射 + 注解 | struct tag + 编解码器**缓存的字段元数据**（第 30 章） |
| ORM | Hibernate 全自动映射（代理对象） | 手写 SQL / `sqlc` **生成类型安全代码** / GORM 谨慎使用 |
| AOP 切面 | 动态代理 | **中间件函数链**（手写 5 行 `func(http.Handler) http.Handler`，第 36 章） |
| 接口实现多份 | 一个类多个接口，反射适配 | 显式写适配器 struct |

所以：**Java 的抽象是"藏起来的机器"，Go 的抽象是"摆在你面前的齿轮"。** 前者上手快、理解慢；后者上手慢、理解快。

---

## 1.5 核心断层三：值语义 —— 你手里的不是遥控器，是复印件

这是最容易写出 bug 的一条。Java 里：

```java
User a = repo.find(1);
User b = a;          // 传的是引用，b 和 a 是同一个对象
b.setName("x");
System.out.println(a.getName()); // "x" —— 同一个对象
```

Go 里：

```go
a := repo.Find(1)    // a 是一个 User 结构体值
b := a               // 整个结构体被复制了一份
b.Name = "x"
fmt.Println(a.Name)  // 原来的名字 —— a 完全没变
```

**"Go 里一切皆拷贝"**，但要精确地知道哪些东西"看起来像引用其实不是"：

| 类型 | 赋值时复制什么 | 感觉上像 |
| --- | --- | --- |
| `struct` / 数组 `[8]int` | 全部字段（深拷贝，逐字节） | 值 |
| **指针** `*T` | 地址（8 字节） | 引用 |
| **slice** `[]T` | **header 三字段**（指针+len+cap），底层数组仍共享 ⚠️ | "半引用" |
| **map** | 指向 `hmap` 的指针 ⚠️ | 引用 |
| **channel** | 指向 `hchan` 的指针 ⚠️ | 引用 |
| **函数值** | 指针 + 闭包环境 | 引用 |
| **interface** | 类型信息 + 数据字（可能触发装箱拷贝）⚠️ | 混合 |
| `string` | 指针 + 长度（不可变，不会有可见问题） | 值 |
| **数组** `[3]int` | 全部元素 | 值（和 Java 完全不同！） |

> 🩸 **真实事故现场**：`func process(users []User)` 里修改了 `users[0].Name`，调用方发现数据变了（因为 slice 共享底层数组）；但如果你写 `for _, u := range users { u.Name = "x" }`，**一个都不会变**，因为 `u` 是元素的副本。这类 bug 在第 11 章（slice）和第 9 章（零值与指针）会被彻底解剖。

记住这条口诀：**"Go 是值语义的语言，指针、slice、map、channel、接口是它留给你的五个后门。"**

---

## 1.6 核心断层四：没有继承，那复用靠什么？

Java 的复用阶梯是：`interface` → `abstract class` → `extends` → 模板方法钩子 → `super.xxx()`。

Go 只有两个工具：

1. **嵌入（embedding）**：把另一个 struct 塞进来，字段和方法被"提升"，但**没有覆盖、没有动态分派**。

```go
type Base struct{ id int }
func (b *Base) ID() int { return b.id }

type Agent struct {
    Base            // 嵌入：Agent 自动获得 ID() 方法
    Name string
}

func (a *Agent) ID() int { return 100 } // 遮蔽（shadow），但不是 override

var x interface{ ID() int } = &Agent{}  // 用 Agent 的
var b *Base = &Agent{}.Base             // 拿到嵌入的 Base，用 Base 的
```

**关键区别**：在 Java 里，父类方法内部 `this.foo()` 会调到子类的 override 版本（多态）；在 Go 里，`Base` 的方法内部调 `b.ID()` 永远调 `Base.ID`，**它根本不知道外面套了个 Agent**。这就是"组合 ≠ 继承"的技术本质（第 4 章详解方法集，第 10 章讲怎么用组合做插件化）。

2. **接口（隐式实现）**：你不需要写 `implements`，只要方法集匹配就自动满足。

```go
// 定义在使用方（这是 Go 的惯例）
type ChatModel interface {
    Generate(ctx context.Context, msgs []Message) (Reply, error)
}

// 任何有这个方法的结构体都能被塞进来——哪怕它压根没 import 这个包
type OpenAIClient struct{ /* ... */ }
func (c *OpenAIClient) Generate(ctx context.Context, msgs []Message) (Reply, error) { ... }
```

**没有继承后，代码会怎样演化？** 你会从"设计类层次"转向"设计小接口 + 组装"。这也是为什么 Go 代码里会有大量 1~2 个方法的接口——`io.Reader`、`io.Writer`、`http.Handler`、`llms.Model` 全都是小接口。**小接口可以拼装出无限组合，大抽象类只能纵向延伸。**

---

## 1.7 核心断层五：并发是一行代码的事（以及它的账单）

Java 里起一个异步任务：

```java
CompletableFuture
    .supplyAsync(() -> llm.chat(prompt), executor)
    .thenApply(r -> r.trim())
    .exceptionally(e -> "fallback")
    .join();
```

Go 里：

```go
ch := make(chan string, 1)
go func() {
    r, err := llm.Chat(ctx, prompt)
    if err != nil {
        ch <- "fallback"
        return
    }
    ch <- strings.TrimSpace(r)
}()
out := <-ch
```

直观感受：**Java 的 `CompletableFuture` 是"数据流的管道组合"，Go 的 goroutine + channel 是"手工接线"**。前者抽象度高、组合子丰富但错误处理和取消语义容易失控；后者原始、啰嗦，但**每一步都看得见**。

真正体现差距的是这段（一次并行调用三个工具，任一完成即返回，全部带超时与取消）：

```go
func firstTool(ctx context.Context, tools []Tool, args Args) (Result, error) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()

	type res struct {
		r   Result
		err error
	}
	ch := make(chan res, len(tools)) // buffered：防止 goroutine 泄漏
	for _, t := range tools {
		go func(t Tool) {
			r, err := t.Call(ctx, args) // ctx 取消时会立刻返回
			ch <- res{r, err}
		}(t)
	}
	var lastErr error
	for range tools {
		select {
		case <-ctx.Done():
			return Result{}, ctx.Err()
		case r := <-ch:
			if r.err == nil {
				return r.r, nil // 拿到第一个成功结果，其余 goroutine 因 ctx 取消而退出
			}
			lastErr = r.err
		}
	}
	return Result{}, lastErr
}
```

这段代码里，**goroutine、channel、select、context 四个东西就构成了 Go 并发的全部原语**——没有线程池、没有 Future、没有 `anyOf`、没有 `@Async`。它们的底层实现（GMP 调度、hchan、取消树）在第 17~25 章会被完整解剖，其中包括"为什么 goroutine 只花 2KB 栈"和"为什么 `ctx` 能一路传染到 socket 层"。

> 🧾 **并发的账单**：Go 让小任务并发变得极其便宜，于是**你会不自觉地滥用它**。goroutine 泄漏、无缓冲 channel 死锁、`for` 循环里闭包捕获变量、共享 map 并发写——这些坑在 Java 里因为"起线程很贵"反而不常见。第 24 章整章讲这些坑。

---

## 1.8 你会踩的 8 个坑（先给你地图，别急着走）

这 8 个坑是 Java 程序员的"必经之路"，**每一个都不是语法问题，而是心智模型问题**：

| # | 坑 | 你的 Java 直觉 | Go 的真相 | 详见 |
| --- | --- | --- | --- | --- |
| 1 | 影子变量 | `int x = 1; { int x = 2; }` 想的是"块作用域" | `:=` 会静默创建新变量，`err` 被吃掉导致空指针 | 第 3 章 |
| 2 | nil 接口 | 一个 nil 的 `interface` 判断应该为 nil | `interface` 非 nil 但内部指针为 nil，`err != nil` 成立 | 第 5 章 |
| 3 | nil map 写入 | `new HashMap<>()` 可以直接 put | `var m map[string]int` 是 nil，**写入直接 panic** | 第 12 章 |
| 4 | slice 共享 | List 传给方法是"引用传递" | slice 是 header 拷贝，底层数组共享，`append` 后可能"分家" | 第 11 章 |
| 5 | defer 在循环里 | `finally` 出了 try 就执行 | `defer` 在**函数**返回时才执行，循环里累积 1 万个就是内存炸弹 | 第 8 章 |
| 6 | 值接收者改不动 | 方法里 `this.x = 1` 一定生效 | 值接收者拿到的是副本，改了个寂寞 | 第 4 章 |
| 7 | 循环变量捕获 | lambda 捕获的是"最终变量" | 1.22 前所有迭代共享同一个变量，goroutine 里全是最后一个值（1.22+ 已修复） | 第 8 章 |
| 8 | 泄漏 | 线程池有界、GC 管对象 | goroutine 泄漏没人回收；`resp.Body` 不关会导致连接池耗尽 | 第 24、36 章 |

---

## 1.9 第一个"像 Go"的程序

别急着深入，先建立"手感"。一个完整可运行（`go run main.go`）的小程序：

```go
package main

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"sync"
	"time"
)

// 1) 包级错误：哨兵值，供调用方 errors.Is 判断
var ErrTooSlow = errors.New("too slow")

// 2) 结构体 + 小写字段 = 包内可见（相当于 Java 的 private）
type probe struct {
	name string
	url  string
}

// 3) 指针接收者：不拷贝，且能改状态
func (p *probe) check(ctx context.Context) (int, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, p.url, nil)
	if err != nil {
		return 0, fmt.Errorf("build request: %w", err) // %w 保留错误链
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return 0, fmt.Errorf("%s: %w", p.name, err)
	}
	defer resp.Body.Close() // 4) defer 就近注册，LIFO 执行
	return resp.StatusCode, nil
}

func main() {
	probes := []probe{
		{"github", "https://github.com"},
		{"example", "https://example.com"},
	}

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	var wg sync.WaitGroup
	for _, p := range probes { // Go 1.22+：每轮迭代拥有独立的 p
		wg.Add(1)
		go func() { // 5) goroutine 是语言级原语
			defer wg.Done()
			code, err := p.check(ctx)
			if err != nil {
				fmt.Printf("%-8s ERR %v\n", p.name, err)
				return
			}
			fmt.Printf("%-8s %d\n", p.name, code)
		}()
	}
	wg.Wait() // 6) 没有 Future.join()，用 WaitGroup 汇合
}
```

对照一下同样的 Java 代码你会怎么写（`CompletableFuture` + `HttpClient` + `ExecutorService`），你会发现**Go 版本没有一处"框架"，全是语言原语**。这就是 Go 的审美：**标准库就是框架，语言特性就是基础设施**。

---

## 1.10 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| Go 的设计目标 | 可维护性 > 表达力：让普通工程师五年后还能读懂 |
| 没有注解 | 魔法从运行期搬到编译期（代码生成 / 显式调用） |
| `if err != nil` | 错误是值，不是控制流；换来可分类、可包装、可编程 |
| 没有继承 | 只有 struct 嵌入 + 隐式接口；嵌入不是 override |
| 值语义 | 赋值即拷贝；指针 / slice / map / chan / 接口是五个后门 |
| 接口 | 隐式实现，由使用方定义，通常 ≤ 3 个方法 |
| 并发 | `go` + chan + select + context 四个原语构成全部 |
| 可见性 | 首字母大小写，没有 `public/private` |
| 格式 | `gofmt` 一统天下，无可争议 |
| 性能模型 | AOT 编译无预热；峰值靠 PGO 追 JIT；GC 是非分代非压缩 |
| 学习陷阱 | 影子变量、nil 接口、nil map、slice 共享、defer 循环、值接收者、循环捕获、泄漏 |

## 1.11 自检题

1. **你的 Java 同事说"Go 连异常都没有，错误处理靠 if 判断，这不是倒退吗？"** 你怎么用三句话反驳？
   > 答案要点：① 错误是值 → 可分类/包装/聚合，Agent 场景的重试分类学因此变成常态而非框架；② 控制流降级为数据流 → 对编译器友好、可预测、不丢栈；③ 异常真正的问题是把"可能失败"藏起来，而 Go 把它写进签名。

2. **`func f(u User)` 里 `u.Name = "x"` 会生效吗？如果参数是 `[]User` 呢？如果是 `map[string]User` 呢？**
   > 答案要点：值 → 不生效；slice → `u[0].Name = "x"` 生效（共享底层数组），但 `for _, u := range` 不生效；map → 生效（map 是指针语义）。

3. **为什么 Go 社区说"接口应该由使用方定义"，而 Java 里的 `interface` 常常由提供方定义？**
   > 答案要点：隐式实现解除耦合——使用方只声明自己需要的最小能力（1~3 个方法），提供方无需感知；避免了 Java 那种"先在 service 包里定义一个 20 方法的接口"导致的实现绑定与包依赖倒挂。

## 1.12 延伸阅读

- 官方 FAQ「Why does Go not have exceptions/classes/generics?」——每一条都是本章价值观的官方版本
- Rob Pike: *Simplicity is Complicated*、*Less is exponentially more*（两场演讲）
- `go/doc/go_spec.html` 的 "Types" 与 "Declarations and scope" 两节
- 下一章：[2. 工程骨架：module、目录布局与工具链](2-toolchain-layout.md)
