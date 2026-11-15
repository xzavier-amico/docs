# 46. 多 Agent 协作与持久化执行

> **状态**：`已完成（初稿）` ｜ **所属**：第七部分 · AI-Agent 应用架构 ｜ **编号**：46 / 54
> **本章定位**：当任务从"一次对话"变成"跑 40 分钟、调用 30 次模型、还要等人审批"，你面对的就是**分布式系统问题**，而不是 prompt 问题。这一章讲两件事：多 Agent 怎么组织（拓扑与预算），长流程怎么不丢（Temporal 的确定性重放）。
> **读法**：46.2 的拓扑图 + 46.4 的重放时序图是核心。只做短对话的读者可以只看 46.2 与 46.9；要上长流程的，46.3.3 必须逐行读懂。

---

## 46.1 引子：那个跑了 40 分钟、被杀掉、又重跑一遍的调研 Agent

场景很典型：一个"竞品调研"Agent，流程是「拆解问题 → 并发搜索 12 个来源 → 逐个精读 → 交叉验证 → 产出报告」。跑一次 40 分钟、约 35 次 LLM 调用、成本 4 美元。

上线第一周出了三件事：

1. 发布新版本，K8s 滚动更新把正在跑的 Pod 杀了。**没有断点**，用户重发一次，从头再跑 40 分钟，再花 4 美元。
2. supervisor Agent 把同一个子任务派了三遍（因为它看不到"已完成"的状态），预算无声无息烧掉 3 倍。没有任何一处代码在问"我们还剩多少预算"。
3. 某个子 Agent 的工具一直 500，重试了 8 次之后把整个流程拖死了——**没有失败隔离**，一个子任务毒死了全部。

三件事对应这一章的三个主题：**持久化（不丢）→ 预算（不烧）→ 隔离（不连坐）**。而"多 Agent 拓扑"其实是这三个约束下的形状选择。

---

## 46.2 直觉模型：四种拓扑 + 三种通信

```
① supervisor（主管制，最常用）
        ┌─────────┐
        │Supervisor│  ← 只做路由/汇总，不直接干活
        └──┬───┬──┬─┘
      ┌────┘   │  └────┐
   ┌──▼─┐  ┌──▼─┐  ┌──▼─┐
   │S₁  │  │S₂  │  │S₃  │   共享同一个 tool 池 / 上下文片段
   └────┘  └────┘  └────┘

② hierarchical（分层，主管的主管）
      ┌──────────┐
      │  Top     │
      └──┬────┬──┘
    ┌────▼┐ ┌─▼────┐
    │Sup-A│ │Sup-B │       每层自己有一份预算配额
    └─┬─┬─┘ └─┬──┬─┘
     w1 w2    w3 w4

③ debate（辩论/评审，质量优先）
   ┌────────┐  ┌────────┐
   │Proposer│  │Critic  │   ← 轮流多轮，最后由 Judge 汇总
   └────┬───┘  └───┬────┘
        └────┬─────┘
        ┌────▼────┐
        │  Judge  │
        └─────────┘

④ handoff（移交，客服式）
   ┌────────┐  transfer  ┌──────────┐
   │ 前台Agent├──────────►│ 领域Agent │   ← 控制权整体转移，前台退出
   └────────┘            └──────────┘
```

| 拓扑 | 适合 | 主要失败模式 | 成本特征 | Go 实现要点 |
| --- | --- | --- | --- | --- |
| supervisor | 任务可分解、需要汇总 | 主管死循环派活；重复分派 | 中央瓶颈：主管每轮一次 LLM | 主管只输出"结构化计划"，派发走并发 + 去重表 |
| hierarchical | 大任务多层级 | 预算在层间被"各层都花一遍" | 指数膨胀风险最高 | 每层 `Budget` 向下分配，父层保留 20% 兜底 |
| debate | 答案质量敏感（写作、审计） | 无终止条件，越辩越贵 | 固定轮次成本可控 | 硬性 `maxRounds` + 收敛判据（相似度/无新论点） |
| handoff | 领域分流、SOP 明确 | 移交后上下文丢失；来回踢皮球 | 低（同一时刻只有一个活跃 Agent） | 移交要带**结构化状态快照**，不是一句话 |

通信方式只有三类，选错了后面全是补丁：

| 通信 | 形态 | 优点 | 代价 | 用在 |
| --- | --- | --- | --- | --- |
| 共享状态（黑板） | 一个 `*RunState` + 互斥 | 简单、可全量快照、易恢复 | 耦合高、易写竞态 | supervisor 内同一进程 |
| 消息传递（channel） | 每 Agent 一个 inbox channel | 解耦、天然背压 | 需要协议与超时 | 同进程不同 goroutine |
| 消息总线（Kafka/NATS） | 事件持久化 + 多消费者 | 跨进程、可重放、可审计 | 一致性靠自己去重 | 跨服务、跨团队 |

---

## 46.3 底层实现

### 46.3.1 通信与共享状态：先把"运行状态"变成一个可序列化的结构体

无论是共享黑板还是消息总线，第一件事都是**把状态定义成能存的**（后面 Temporal 的重放、自研状态机的恢复，全靠这个）：

```go
// RunState 是"可持久化的真相"：进程重启后，凭它就能继续跑
type RunState struct {
	RunID    string    `json:"run_id"`
	TenantID string    `json:"tenant_id"`
	Goal     string    `json:"goal"`
	Steps    []StepRec `json:"steps"`     // 已完成的步骤（幂等重放的依据）
	Budget   Budget    `json:"budget"`
	Status   string    `json:"status"`    // running / waiting_human / done / failed
	Version  int64     `json:"version"`   // 乐观锁
}

type StepRec struct {
	Key    string `json:"key"`    // 幂等键：如 "search:kw=xx"，同 key 只执行一次
	Agent  string `json:"agent"`
	Input  string `json:"input"`
	Output string `json:"output,omitempty"`
	Err    string `json:"err,omitempty"`
	Tokens int    `json:"tokens"`
}

type Budget struct {
	MaxLLMCalls int `json:"max_llm_calls"`
	MaxTokens   int `json:"max_tokens"`
	MaxWallMS   int `json:"max_wall_ms"`
	UsedCalls   int `json:"used_calls"`
	UsedTokens  int `json:"used_tokens"`
}

// Done 用 map 做幂等判重（只在单进程内；跨进程要查持久化状态）
func (s *RunState) Done(key string) (StepRec, bool) {
	for _, st := range s.Steps {
		if st.Key == key {
			return st, true
		}
	}
	return StepRec{}, false
}

// Spend 是唯一的扣费入口：所有 LLM 调用必须过这里
func (b *Budget) Spend(calls, tokens int) error {
	if b.UsedCalls+calls > b.MaxLLMCalls {
		return fmt.Errorf("llm call budget exceeded: %d/%d", b.UsedCalls, b.MaxLLMCalls)
	}
	if b.UsedTokens+tokens > b.MaxTokens {
		return fmt.Errorf("token budget exceeded: %d/%d", b.UsedTokens, b.MaxTokens)
	}
	b.UsedCalls += calls
	b.UsedTokens += tokens
	return nil
}
```

> 💡 **纪律**：预算检查必须**和调用发生在同一处、同一把锁里**。散落在各 Agent 里的 `if 预算够` 检查，等于没有检查——两个子 Agent 同时通过检查，就是 2 倍的超支。

### 46.3.2 资源预算与失败隔离

```go
// RunSubAgents：有界并发 + 单 Agent 超时 + panic 隔离 + 预算共享
func RunSubAgents(ctx context.Context, budget *Budget, mu *sync.Mutex,
	tasks []Task, run func(context.Context, Task) (string, int, error)) []Result {

	g, gctx := errgroup.WithContext(ctx)
	g.SetLimit(4) // 并发上限 = 供应商配额 / 单请求成本

	results := make([]Result, len(tasks))
	for i, t := range tasks {
		g.Go(func() error {
			// 每个子 Agent 一个独立的超时：一个慢的不能拖死整批
			actx, cancel := context.WithTimeout(gctx, 3*time.Minute)
			defer cancel()

			out, tokens, err := safeRun(actx, t, run) // panic 隔离
			mu.Lock()
			spendErr := budget.Spend(1, tokens)
			mu.Unlock()
			if spendErr != nil {
				return spendErr // 超预算：整批停止，别继续烧
			}
			results[i] = Result{Task: t, Out: out, Err: err}
			return nil
		})
	}
	_ = g.Wait()
	return results
}

// safeRun 把 panic 变成 error：一个子 Agent 的 bug 不能带走整个进程
func safeRun(ctx context.Context, t Task, run func(context.Context, Task) (string, int, error)) (
	out string, tokens int, err error) {
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("sub-agent panic: %v", r)
		}
	}()
	return run(ctx, t)
}
```

失败隔离的四个层次，从便宜到贵：**超时**（必须有，默认 3 分钟）→ **panic 转 error**（必须有）→ **重试 + 熔断**（工具级，见第 47 章）→ **降级结果**（用缓存/规则兜底，把"失败"变成"质量下降"）。

### 46.3.3 Temporal：让"跑了 40 分钟的任务"变得可恢复

自研状态机也能做，但你很快会撞上这堆问题：步骤间超时、重试退避、定时唤醒、跨重启恢复、人工审批挂起几天、可视化排障、去重执行。Temporal 把这些打包了。要理解它，只需抓住一个词：**确定性重放（deterministic replay）**。

#### 重放时序：代码不会被"重新执行"，只会被"重新走过"

```
第一次执行（真实跑）
  Worker 内存                            Temporal 服务端（History，持久化）
  ─────────────────────────────────────────────────────────────────────
  WorkflowTask #1  ┌────────────────────┐
                  │ ExecuteActivity(LLM)│──► ActivityTaskScheduled(id=7)
                  │ 阻塞等待（不占资源） │
                  └────────────────────┘
                                          ◄── ActivityTaskCompleted(id=7,"答案…")
  WorkflowTask #2  ┌────────────────────┐
                  │ 拿到"答案…"，继续   │──► WorkflowTaskCompleted
                  └────────────────────┘

Worker 崩了 / 被发布杀掉，30 秒后另一台 Worker 接手
  ─────────────────────────────────────────────────────────────────────
  重放 WorkflowTask #1：代码再跑一遍 ExecuteActivity(LLM)，
    但 SDK 查 History 发现 id=7 已完成 → 直接返回记录的"答案…"
    ★ 不会真的再调一次 LLM（不重复花钱、不重复副作用）
  重放 WorkflowTask #2：同上，直到追上进度，才开始执行"新"的代码路径
```

所以铁律是：**Workflow 函数必须是纯的（只用 SDK 提供的确定性 API），所有副作用放进 Activity。**

| 在 Workflow 里 | 允许吗 | 用什么替代 |
| --- | --- | --- |
| `time.Now()` | ❌ | `workflow.Now(ctx)` |
| `time.Sleep` | ❌ | `workflow.Sleep(ctx, d)` / `workflow.NewTimer` |
| `math/rand` | ❌ | `workflow.SideEffect` 取一次随机数并存进历史 |
| `go func(){}` | ❌ | `workflow.Go(ctx, func(wctx workflow.Context){...})` |
| `range map`（顺序不定） | ⚠️ | 排序 key 后再 range |
| HTTP / DB / 文件 IO | ❌ | 写成 Activity |
| 重 CPU 计算（>1s） | ❌ | 拆成 Activity（否则 workflow task 超时） |
| `slog.Info` | ❌ | `workflow.GetLogger(ctx)` |

#### Workflow 骨架（含预算刹车 + 人工审批 + 进度查询）

```go
package agent

import (
	"context"
	"time"

	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/workflow"
)

type ResearchInput struct {
	TenantID  string `json:"tenant_id"`
	Topic     string `json:"topic"`
	MaxSteps  int    `json:"max_steps"`
	MaxTokens int    `json:"max_tokens"`
}

type ResearchResult struct {
	Answer   string `json:"answer"`
	Steps    int    `json:"steps"`
	Tokens   int    `json:"tokens"`
	Rejected bool   `json:"rejected"`
}

// ---------- Activity：有副作用的那一半 ----------

type LLMReply struct {
	Text   string `json:"text"`
	Tokens int    `json:"tokens"`
}

// 必须幂等：Temporal 可能重试它（网络抖动、Worker 崩），
// 幂等键用 activity.GetInfo(ctx).ActivityID，下游据此去重
func PlanStepActivity(ctx context.Context, in ResearchInput, history []string) (LLMReply, error) {
	info := activity.GetLogger(ctx)
	info.Info("llm call", "attempt", activity.GetInfo(ctx).Attempt)
	return LLMReply{Text: "…下一步结论…", Tokens: 850}, nil // 真实实现里调 LLM 客户端（第 40 章）
}

func NotifyActivity(ctx context.Context, tenantID, msg string) error { return nil } // 发通知/写库

// ---------- Workflow：确定性的那一半 ----------

func ResearchWorkflow(ctx workflow.Context, in ResearchInput) (ResearchResult, error) {
	logger := workflow.GetLogger(ctx)

	// 活动级默认参数：超时 + 重试策略
	actCtx := workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
		StartToCloseTimeout: 3 * time.Minute,
		HeartbeatTimeout:    30 * time.Second,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:    time.Second,
			BackoffCoefficient: 2.0,
			MaximumAttempts:    3,
			// 4xx/参数错误不要重试（第 7 章的重试分类学）
			NonRetryableErrorTypes: []string{"InvalidArgument"},
		},
	})

	var res ResearchResult
	// 查询接口：客户端可随时问"跑到哪了"
	_ = workflow.SetQueryHandler(ctx, "progress", func() (ResearchResult, error) { return res, nil })

	// 信号通道：人工审批 / 中途补充信息
	approveCh := workflow.GetSignalChannel(ctx, "approve")

	var history []string
	for step := 0; step < in.MaxSteps; step++ {
		var reply LLMReply
		if err := workflow.ExecuteActivity(actCtx, PlanStepActivity, in, history).
			Get(actCtx, &reply); err != nil {
			return res, err
		}
		history = append(history, reply.Text)
		res.Steps++
		res.Tokens += reply.Tokens

		// 预算刹车：超了就带着已完成的成果正常结束，而不是抛错重试
		if res.Tokens > in.MaxTokens {
			logger.Info("budget exhausted", "tokens", res.Tokens)
			res.Answer = "已达到预算上限，以下为阶段性结论：" + reply.Text
			return res, nil
		}

		// 高危动作要人工审批：挂起等待 signal，不吃 CPU、不怕重启（可以等几天）
		if step == 1 {
			var approved bool
			approveCh.Receive(ctx, &approved) // 阻塞直到 signal 到达（这行本身就是确定性的）
			if !approved {
				res.Rejected = true
				return res, nil
			}
		}
	}

	res.Answer = history[len(history)-1]
	_ = workflow.ExecuteActivity(actCtx, NotifyActivity, in.TenantID, "done").Get(ctx, nil)
	return res, nil
}
```

> ⚠️ **两个致命细节**：① `ExecuteActivity` **不要**传 `workflow.Context` 给 Activity 之外的用途，`Get(actCtx, ...)` 一定要带 `nil` 或带**带 ActivityOptions 的 ctx**（传错 ctx 会导致"没超时"）；② `RetryPolicy` 的默认值是**几乎无限重试 + 指数退避**，遇到 400/参数错误会重试很久——必须显式设 `MaximumAttempts` 和 `NonRetryableErrorTypes`。

#### Worker 与客户端：启动、发信号、查进度

```go
func main() {
	c, err := client.Dial(client.Options{HostPort: "localhost:7233"})
	if err != nil {
		panic(err)
	}
	defer c.Close()

	// Worker：并发度要按"下游 LLM 配额"调，不是按 CPU 核数调
	w := worker.New(c, "agent-tq", worker.Options{
		MaxConcurrentActivityExecutionSize:     16,
		MaxConcurrentWorkflowTaskExecutionSize: 8,
	})
	w.RegisterWorkflow(ResearchWorkflow)
	w.RegisterActivity(PlanStepActivity)
	w.RegisterActivity(NotifyActivity)

	go func() {
		bg := context.Background()
		we, err := c.ExecuteWorkflow(bg, client.StartWorkflowOptions{
			ID:        "research-" + uuid.NewString(),
			TaskQueue: "agent-tq",
			// 总时长上限：40 分钟的活，给 2 小时兜底
			WorkflowExecutionTimeout: 2 * time.Hour,
		}, ResearchWorkflow, ResearchInput{TenantID: "t-1", Topic: "竞品调研", MaxSteps: 12, MaxTokens: 40000})
		if err != nil {
			panic(err)
		}

		// 人工审批：同一天任意进程、任意时间都可以发信号
		_ = we.SignalWorkflow(bg, "approve", nil, true)

		// 进度查询：直接打到运行中的 Workflow 内存状态
		var progress ResearchResult
		val, err := c.QueryWorkflow(bg, we.GetID(), "", "progress")
		if err == nil {
			_ = val.Get(&progress)
		}

		var out ResearchResult
		if err := we.Get(bg, &out); err != nil {
			panic(err)
		}
		fmt.Println(out.Steps, out.Tokens)
	}()

	// Run 会阻塞，Ctrl-C 优雅退出
	if err := w.Run(worker.InterruptCh()); err != nil {
		panic(err)
	}
}
```

Temporal 的四个原语，正好对应 Agent 长流程的四个需求：

| 原语 | 作用 | Agent 场景 |
| --- | --- | --- |
| Workflow | 编排逻辑，确定性、可重放 | "先规划再并发搜索再汇总"的主流程 |
| Activity | 副作用单元，可重试、可心跳、可超时 | 调 LLM、调工具、写库、发通知 |
| Signal | 外部 → Workflow 的单向消息 | 人工审批、用户中途插话、取消 |
| Query | 外部 ← Workflow 内存状态（只读） | 查进度、查已花 token、查当前步骤 |

### 46.3.4 工作流引擎 vs 自研状态机

不是所有项目都该上 Temporal（多一个集群、多一套技能栈）。把边界画清楚：

| 维度 | 自研状态机（PG 一行一 run） | Temporal |
| --- | --- | --- |
| 恢复粒度 | 你自己定义的 step | 每条命令（含 activity 结果）都进历史 |
| 重试/退避 | 手写 | 内建 RetryPolicy / 心跳 / 超时 |
| 挂起等待（等 3 天） | 轮询或定时唤起 | `Receive` signal，天然支持 |
| 可视化排障 | 自己写后台 | Web UI 直接看历史与栈 |
| 运维成本 | 零新组件 | 需要 Temporal 集群（或 Cloud）+ 版本兼容纪律 |
| 确定性约束 | 无 | **有，且很硬**（不能 time.Now / 直接 IO） |
| 版本演进 | 随意改 | 需要 `workflow.GetVersion` / 加新 workflow |
| 适合 | 3~8 个步骤、几分钟内、可重跑成本低 | 多步骤 × 长耗时 × 花钱多 × 有人工环节 |

最小可用的自研状态机长这样（关键在"**已完成的步骤用 key 跳过**"）：

```go
type Engine struct{ DB *sql.DB }

// Resume 幂等地把 run 推进到终态；每次只做一步，然后落库
func (e *Engine) Resume(ctx context.Context, runID string) error {
	for {
		var st RunState
		if err := e.load(ctx, runID, &st); err != nil {
			return err
		}
		if st.Status == "done" || st.Status == "failed" {
			return nil
		}
		next, err := e.nextStep(ctx, &st) // 纯函数：根据 Steps 决定下一步做什么
		if err != nil {
			return e.fail(ctx, runID, err)
		}
		if next == nil {
			return e.finish(ctx, runID)
		}
		if _, ok := st.Done(next.Key); ok {
			continue // ★ 幂等：这一步已经做过，跳过（重启/重复投递都安全）
		}
		out, tokens, err := e.exec(ctx, next) // 调 LLM / 工具
		if err != nil {
			return e.retryLater(ctx, runID, next, err) // 不落 Steps，交给下一轮重试
		}
		if err := st.Budget.Spend(1, tokens); err != nil {
			return e.finish(ctx, runID) // 预算刹车 = 正常结束
		}
		if err := e.saveWithCAS(ctx, &st, StepRec{Key: next.Key, Output: out, Tokens: tokens}); err != nil {
			return err // version 冲突就下一轮重来
		}
	}
}
```

> 💡 如果你只有"一次对话 + 两三个工具调用"，**不要上 Temporal**；如果你有"人工审批 + 跑几十分钟 + 花钱"，**不要自己写**。

### 46.3.5 事件驱动：Kafka / NATS 在 Agent 里的真实位置

```go
// NATS JetStream：轻量、低延迟、支持持久化与 ack，适合 Agent 内部各阶段解耦
nc, _ := nats.Connect(nats.DefaultURL)
js, _ := nc.JetStream()
_, _ = js.AddStream(&nats.StreamConfig{Name: "AGENT", Subjects: []string{"agent.>"}})

// 生产者：投递一个"子任务"
_, _ = js.Publish("agent.task.research", payload)

// 消费者：手动 ack + 幂等键 + 最大投递次数
sub, _ := js.Subscribe("agent.task.research", func(m *nats.Msg) {
	var t Task
	if err := json.Unmarshal(m.Data, &t); err != nil {
		_ = m.Term() // 毒消息：直接终止，别无限重投
		return
	}
	if done, _ := seen(t.Key); done {
		_ = m.Ack()
		return
	}
	if err := handle(context.Background(), t); err != nil {
		return // 不 ack → 会被重投（至少一次）
	}
	_ = m.Ack()
}, nats.Durable("research-worker"), nats.ManualAck(), nats.MaxAckPending(64))

// Kafka（franz-go）适合跨团队、需要长期保留与多消费组的场景
kgoClient, _ := kgo.NewClient(kgo.SeedBrokers("kafka:9092"))
kgoClient.Produce(ctx, &kgo.Record{Topic: "agent.events", Value: b},
	func(r *kgo.Record, err error) { /* 记日志/指标 */ })
```

| 定位 | Temporal | Kafka | NATS / JetStream |
| --- | --- | --- | --- |
| 本质 | 有状态编排（命令式代码） | 持久化日志（事件流） | 轻量消息（请求/订阅） |
| 你写什么 | workflow + activity | 生产者/消费者 | 发布/订阅 |
| 谁负责流程 | 引擎（历史 + 重放） | 你自己 | 你自己 |
| 典型延迟 | 任务调度级别（几十 ms 起） | ms 级 | 亚 ms 级 |
| Agent 场景 | 主流程编排 | 事件溯源、审计、跨团队通知 | 内部解耦、工具结果回传 |

**别把 Kafka 当工作流引擎用**：它保证"消息不丢、按序投递"，不保证"你的第 3 步等第 2 步的结果"。要串流程，要么用 Temporal，要么自己在消费端实现"状态机 + 幂等"。

---

## 46.4 Java 对照

| 你的 Java 直觉 | Go + Temporal 的现实 | 会猜错的地方 |
| --- | --- | --- |
| Spring `@Scheduled` + 数据库状态表 | Workflow + Timer | 定时唤醒不需要外部调度器，`workflow.NewTimer` 就是持久化的定时器 |
| Camunda / Flowable（BPMN 图） | 命令式 Go 代码 + History | Temporal 没有流程图定义，"流程图"是代码执行留下的历史；调试靠重放而不是看 BPMN |
| `ExecutorService` 里跑子任务 | `workflow.Go` / `Future` | **Workflow 里禁止裸 `go`**，并发必须走 SDK，否则重放对不上 |
| `CompletableFuture.allOf` | `workflow.AwaitAll` / 多个 Future 依次 `Get` | 多个 Future 依次 `Get` 即等价于并行发起、阻塞等待 |
| Kafka Streams 做聚合 | 消费端自己维护状态 + 幂等 | 没有"状态存储"这层抽象，状态放 DB |
| 乐观锁 `@Version`（JPA） | `UPDATE ... WHERE version=$old` / Temporal 自带 | Temporal 里你**不需要**自己写版本号，历史就是真相 |

---

## 46.5 框架与工程落地

- **Go SDK 三件套**：`client`（发起/信号/查询）、`worker`（注册与执行）、`workflow`/`activity`（两个包分属"确定性"与"副作用"两侧）。命名空间（Namespace）用来做多租户/多环境隔离；`TaskQueue` 用来做**按能力路由**（比如把"调 OpenAI 的任务"和"调内部工具的任务"分成两个队列，各自扩缩容）。
- **eino / langchaingo 的定位**：它们负责"单个 Agent 的推理循环"（第 43 章），**不负责持久化**。生产中常见的组合是：Temporal Workflow 里跑一个 langchaingo/eino 的 Agent，每次 LLM 调用是一个 Activity——框架管 prompt，Temporal 管恢复。
- **观测**：Temporal 自带 Web UI 可直接看每个 Workflow 的历史与栈；再叠 OTel（第 35 章）把 `run_id` 打进所有 span，才能把"LLM 调用 - 工具调用 - 消息总线"串起来。
- **版本升级**：改 Workflow 代码前先想 `workflow.GetVersion`；或者干脆"改代码 → 用新 workflow type 名"。**已经跑了 3 天的旧 Workflow 会用旧代码重放，改错一行就是 non-determinism panic。**

---

## 46.6 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| `non-determinism error` 反复重试 | Workflow 里用了 `time.Now`/随机/裸 goroutine | 换 `workflow.Now`/`SideEffect`/`workflow.Go` | 46.3.3 |
| 发布后任务全丢 | 无持久化，进程即状态 | Temporal / 自研 step 持久化 | 46.3.3 |
| LLM 被调用两次，钱花两遍 | Activity 不幂等 + 重试 | Activity 幂等键（ActivityID / step key） | 46.3.3 |
| 子任务卡住拖死整批 | 无 per-task 超时 | `context.WithTimeout` 包每个子任务 | 46.3.2 |
| 预算超支 2~3 倍 | 预算检查散落各处、无锁 | 单一 `Budget.Spend` 入口 + 互斥 | 46.3.1 |
| Supervisor 重复派活 | 无幂等 key / 无已完成集合 | `StepRec.Key` + `Done()` 判重 | 46.3.1 |
| Workflow task timeout | Workflow 里做了重 CPU 计算 | 计算搬进 Activity | 46.3.3 |
| 信号发了没反应 | 信号名/Workflow ID 不匹配，或 Workflow 已结束 | 统一 ID 规则；`SignalWithStart` 兜底 | 46.3.3 |
| 4xx 参数错误重试 20 次 | RetryPolicy 默认值 | `MaximumAttempts` + `NonRetryableErrorTypes` | 46.3.3 |
| 消费端重复处理 | Kafka/NATS 至少一次语义 | 幂等键 + 去重表 | 46.3.5 |

---

## 46.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 拓扑选择 | 能汇总就 supervisor；要质量上 debate；要分流上 handoff；层级越深预算越危险 |
| 通信 | 同进程共享状态 + channel；跨进程上总线；状态必须可序列化 |
| 预算 | 单一扣费入口 + 同一把锁；父层留 20% 兜底 |
| 隔离 | 超时 + panic→error + 熔断 + 降级，四层缺一不可 |
| Temporal 铁律 | Workflow 确定性，Activity 副作用 |
| 重放 | 代码重跑，结果来自历史——**不会重复花钱** |
| 四原语 | Workflow（编排）/ Activity（副作用）/ Signal（外部输入）/ Query（读状态） |
| 挂起 | 等 signal 可等几天，不占资源、不怕重启 |
| 版本 | 改 Workflow 必想 `GetVersion` 或换 type 名 |
| 自研 vs 引擎 | 几分钟几步就自研；几十分钟花钱有人工就上引擎 |
| Kafka 定位 | 事件流不是工作流；流程状态自己管 |

---

## 46.8 自检题

1. **Temporal 的"确定性重放"到底保证了什么？为什么 Activity 不需要确定性？**
   > 要点：重放时 Workflow 代码会从头再执行一遍，SDK 用 History 里记录的 activity 结果替换真实调用，从而让代码走到与崩溃前完全相同的分支点。Activity 不会被重放（它的结果已经进历史），所以它可以随意做 IO、拿时间、调外部服务——但必须**幂等**，因为它可能被真实重试。

2. **为什么"预算检查"必须收敛到一个函数里，并且和扣费在同一把锁内？**
   > 要点：分散检查时，多个并发子 Agent 会在同一时刻都读到"还有余额"然后同时花出去，导致超支数倍；集中 `Spend` + 互斥，才能保证"检查-扣减"原子。分布式场景还要把这个计数放 Redis（Lua 原子）或 Temporal 的 Workflow 状态里。

3. **你已经有一个 Kafka，为什么还需要（或不需要）Temporal？**
   > 要点：Kafka 是持久化事件日志，保证投递与顺序，但"第 3 步要等第 2 步的结果、失败要退避重试、人工审批要挂起 3 天"这些是**编排**，Kafka 不提供。若你的流程简单（消费→处理→发事件），Kafka + 幂等消费足够；若有长流程、人工环节、需要可视化与重放，就上 Temporal（或自研状态机 + 状态表）。

---

## 46.9 延伸阅读

- `go.temporal.io/sdk`：`workflow` 包（`ExecuteActivity`/`GetSignalChannel`/`SetQueryHandler`/`SideEffect`/`GetVersion`）、`activity` 包（`GetInfo`/`RecordHeartbeat`）、`worker.Options` 的并发参数
- Temporal 文档：*Workflow Definition*、*Determinism*、*Activity heartbeats*、*Versioning*、*Continue-As-New*
- `github.com/nats-io/nats.go` 的 `jetstream` 子包（`nats.PullSubscribe`、`MaxAckPending`、`Term`）
- `github.com/twmb/franz-go`：`kgo.NewClient`、`Client.Produce`、`PollFetches` 与 `Fetches.EachRecord`
- 上一章：[45. 记忆与会话：状态一致性](45-memory-session.md) ｜ 下一章：[47. Agent 的生产化：多租户、限流与成本](47-agent-productionization.md)

---

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[45. 记忆与会话：状态一致性](45-memory-session.md)；第 26 章（errgroup）、第 43 章（Agent 循环）
- 下一章：[47. Agent 的生产化：多租户、限流与成本](47-agent-productionization.md)
