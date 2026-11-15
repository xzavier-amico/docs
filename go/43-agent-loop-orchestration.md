# 43. Agent 循环与编排引擎

> **状态**：`已完成（初稿）` ｜ **所属**：第七部分 · AI-Agent 应用架构 ｜ **编号**：43 / 54
> **本章定位**：第 40 章会说话，第 41 章会动手，第 42 章能连外部世界。这一章把它们串成一个**能自己决定下一步做什么、并且在失控前停下来**的循环。Agent 的「智商」来自模型，**「可靠」几乎全部来自这一章的工程约束**。
> **读法**：43.3/43.4 是主干（状态模型 + 主循环），43.5 的终止条件表请直接抄进代码评审清单，43.9 的 HITL 恢复是「产品能不能上线」的分水岭。

---

## 43.1 引子：一个跑了 47 轮、烧了 3 美元、什么都没做出来的 Agent

事故报告写得很长，核心就五行：

```
22:14:03  step 1   think   → tool_calls: [list_files("/data")]
22:14:05  step 2   think   → tool_calls: [list_files("/data")]      ← 又是一样的
22:14:07  step 3   think   → tool_calls: [list_files("/data")]      ← 还是
...
22:31:40  step 47  think   → tool_calls: [list_files("/data")]
22:31:41  error    context_length_exceeded（历史里 47 条一模一样的工具输出）
```

四个问题叠在一起：

1. **没有最大步数**（或者设了 50，太多），也没有「重复调用检测」——模型卡在同一步上反复试探，永远不会自己说「我放弃了」。
2. **每一轮都把完整历史重发**，包括 47 份完全相同的大目录列表。**上下文是有限的，而你的循环没有任何「压缩 / 裁剪」的出口**（第 45 章）。
3. **用户 22:15 就点了「停止」**，但你的 `ctx` 只在 HTTP handler 里检查了一次，工具还在跑、模型还在调——**取消没有穿透到循环内部**。
4. **整个循环跑在一个 goroutine 里，状态全在局部变量里**。想要「暂停等人确认」？不可能，因为一旦 handler 返回，状态就没了。

这一章把这四件事一次解决：**把状态从局部变量里搬出来变成可序列化的值，把循环变成一个显式状态机，把每一步的出口都写清楚。**

---

## 43.2 直觉模型：Agent = 一个只有 6 个状态的状态机

```
                    ┌──────────┐
        ┌──────────▶│  THINK   │  调模型（流式），拿到 content + tool_calls
        │           └────┬─────┘
        │                │
        │      ┌─────────┴──────────┐
        │      │                    │
        │  有 tool_calls        没有 tool_calls
        │      │                    │
        │      ▼                    ▼
        │  ┌────────┐          ┌──────────┐
        │  │  ACT   │          │   DONE   │  最终回答，退出
        │  └───┬────┘          └──────────┘
        │      │ 并行执行工具（第 41 章）
        │      ▼
        │  ┌────────────┐  有危险工具且未获批准
        │  │ OBSERVE    │──────────────▶ ┌───────────────────┐
        │  └───┬────────┘                │ WAITING_HUMAN     │ 保存状态后"挂起"
        │      │ 回填 tool 消息           └────────┬──────────┘
        └──────┘                                  │ 人批准/拒绝（可能隔几小时）
                                                   └──────▶ 回到 THINK
   任意状态 ──步数超限／预算超限／循环检测／ctx 取消──▶ FAILED
```

**「状态机」不是学术洁癖，它给你三个具体的工程好处**：

1. **每个状态都是一个可以存盘的快照**（`Status` 字段），所以「暂停 / 恢复 / 重放」变成可能；
2. **每条边都是一次「检查点」**，你可以在这里统一做预算、步数、循环检测；
3. **状态迁移是可视化的**，线上出问题你能一眼看出「它卡在哪个状态」。

下面这张表把四种主流编排范式一次说清（选型就看它）：

| 范式 | 循环长什么样 | 步数/成本 | 可控性 | 适合 |
| --- | --- | --- | --- | --- |
| **ReAct**（Reason+Act） | 想一步做一步，每步都看结果 | 中等，动态 | 中（容易绕圈） | 大多数工具型 Agent：查资料、操作后台 |
| **Plan-and-Execute** | 先出完整计划，再逐步执行，必要时重规划 | 计划一次性 + 执行 N 步 | 高（计划可审） | 步骤多、可预先拆解的任务：批量处理、多文档 |
| **Router** | 不分循环，分类后转交子 Agent/流程 | 最低（1~2 次） | 最高 | 意图分流：客服分诊、任务分发 |
| **Reflection / Critic** | 生成 → 自我批评/外部批评 → 修订 | **2~3 倍** | 中 | 高价值产出：代码、报告、法务文本 |

---

## 43.3 底层实现 I：消息与状态模型

**第一原则：状态必须是一个「值」，不是一个活的 goroutine。**

```go
package agent

type Status string

const (
	StatusRunning Status = "running"
	StatusDone    Status = "done"
	StatusMaxStep Status = "max_steps"
	StatusWaiting Status = "waiting_human"
	StatusFailed  Status = "failed"
)

// State 是唯一真值（source of truth）：可 JSON 序列化、可存 Redis/PG、可重放。
type State struct {
	ID       string        `json:"id"`
	Tenant   string        `json:"tenant"`   // 从鉴权注入，绝不来自模型
	Role     string        `json:"role"`
	Step     int           `json:"step"`     // 放在状态里，恢复时能接着数
	Status   Status        `json:"status"`
	Messages []llm.Message `json:"messages"` // 完整对话历史（含 tool 消息）
	Plan     []string      `json:"plan,omitempty"`
	Seen     map[string]int `json:"seen,omitempty"` // 工具调用指纹 → 出现次数，防死循环
	Pending  []Pending     `json:"pending,omitempty"`
	Usage    Usage         `json:"usage"`
}

type Pending struct { // HITL：等人批准的工具调用
	Call   llm.ToolCall `json:"call"`
	Reason string       `json:"reason"`
}

type Usage struct {
	PromptTokens, CompletionTokens, CachedTokens int
	CostUSD                                      float64
}
```

三个设计判断，每一个都用血换过：

- **`Messages` 存扁平数组，不存「结构化对话树」。** 模型 API 吃的是扁平消息列表，你搞复杂的树结构最后一定要 flatten，只会多一层 bug。工具调用的父子关系靠 `tool_call_id` 表达就够了。
- **`Seen` 这个「指纹计数表」必须有。** 它是 43.1 那场 47 轮事故唯一有效的解药。指纹 = `tool_name + 规范化参数` 的 hash（第 41 章 41.8 的幂等键是同一个东西，但用途不同：一个是防重复执行，一个是防死循环）。
- **事件（Event）与状态（State）必须分开。** `State` 是给「恢复」用的真值，`Event` 是给「UI 看」的流。**绝不要试图从事件流重建状态**——你会漏事件、会重放、会顺序错。

---

## 43.4 底层实现 II：ReAct 主循环（能直接跑的那 40 行）

```go
var (
	ErrMaxSteps     = errors.New("agent: max steps exceeded")
	ErrBudget       = errors.New("agent: budget exceeded")
	ErrLoopDetected = errors.New("agent: repeated identical tool call")
)

// Emitter 是"往外吐事件"的函数。它必须是同步的：返回值非 nil 表示上游不该继续。
type Emitter func(Event) error

type Agent struct {
	LLM       llm.Model
	Tools     *tool.Registry
	System    string
	MaxSteps  int
	BudgetUSD float64
	Price     Price // 每百万 token 的输入/输出单价
	MaxPar    int
}

func (a *Agent) Run(ctx context.Context, st *State, emit Emitter) error {
	if st.Seen == nil {
		st.Seen = map[string]int{}
	}
	if st.Status == "" {
		st.Status = StatusRunning
	}
	for st.Step < a.MaxSteps {
		// 闸门 1：取消（用户点了停止 / HTTP 断开 / 上游超时）
		if err := ctx.Err(); err != nil {
			st.Status = StatusFailed
			return err
		}
		// 闸门 2：预算
		if st.Usage.CostUSD > a.BudgetUSD {
			st.Status = StatusFailed
			return ErrBudget
		}
		if err := emit(Event{Type: EvStepStart, Step: st.Step}); err != nil {
			return err
		}

		// ① THINK：流式调模型，token 边到边 emit
		msg, err := a.think(ctx, st, emit)
		if err != nil {
			st.Status = StatusFailed
			return fmt.Errorf("step %d think: %w", st.Step, err)
		}
		st.Messages = append(st.Messages, *msg)
		if err := emit(Event{Type: EvMessage, Step: st.Step, Text: msg.Content}); err != nil {
			return err
		}

		// ② 终止判定：没有工具调用 = 这是最终回答
		if len(msg.ToolCalls) == 0 {
			st.Status = StatusDone
			return nil
		}

		// ③ 死循环检测：同一个指纹第 3 次出现就判卡死
		for _, c := range msg.ToolCalls {
			fp := fingerprint(c)
			st.Seen[fp]++
			if st.Seen[fp] > 2 {
				st.Status = StatusFailed
				return fmt.Errorf("%w: %s", ErrLoopDetected, c.Name)
			}
		}

		// ④ ACT + OBSERVE：并行执行，结果按原始下标回填（第 41 章）
		outcomes := a.Tools.RunAll(ctx, toCalls(msg.ToolCalls), a.MaxPar)

		// ⑤ HITL 闸门：危险工具先挂起（详见 43.9）
		if need := pick(needsApproval(outcomes)); len(need) > 0 {
			st.Pending = need
			st.Status = StatusWaiting
			return emit(Event{Type: EvInterrupt, Step: st.Step, Text: "需要人工审批"})
		}
		for _, o := range outcomes {
			st.Messages = append(st.Messages, o.AsMessage())
			if err := emit(Event{Type: EvToolResult, Step: st.Step, Res: &o.Res, Err: o.Err}); err != nil {
				return err
			}
		}
		st.Step++ // 只有真正推进了一轮才 +1
	}
	st.Status = StatusMaxStep
	return ErrMaxSteps
}

// think 调模型并拼接流式增量。
func (a *Agent) think(ctx context.Context, st *State, emit Emitter) (*llm.Message, error) {
	stream, err := a.LLM.ChatStream(ctx, &llm.Request{
		System:   a.System,
		Messages: st.Messages,
		Tools:    a.Tools.Visible(st.Role), // 只发该角色能用的工具（第 41 章）
	})
	if err != nil {
		return nil, err
	}
	defer stream.Close()

	var sb strings.Builder
	var calls []llm.ToolCall
	for {
		ch, err := stream.Recv()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, err
		}
		if ch.Delta != "" {
			sb.WriteString(ch.Delta)
			if err := emit(Event{Type: EvToken, Step: st.Step, Text: ch.Delta}); err != nil {
				return nil, err // 前端断了 → 中断上游（第 39/40 章的 ctx 链）
			}
		}
		if len(ch.ToolCalls) > 0 {
			calls = ch.ToolCalls // 累积快照，取最后一帧即可（第 40 章 40.4.3）
		}
		if ch.Usage != nil {
			st.Usage.Add(*ch.Usage, a.Price)
		}
	}
	return &llm.Message{Role: llm.RoleAssistant, Content: sb.String(), ToolCalls: calls}, nil
}
```

注意 `for st.Step < a.MaxSteps` 而不是 `for i := 0; ...`：**步数放在 `State` 里**，这样 HITL 恢复时不会从 0 重新数，也不会因为一次 `Resume` 就白送一遍步数预算。

---

## 43.5 终止条件：六种出口，少一个就是事故

| 出口 | 判定 | 动作 | 备注 |
| --- | --- | --- | --- |
| 正常完成 | `tool_calls` 为空且有文本 | `StatusDone` | 还要检查是不是「我再想想」这类空转回答 |
| 输出截断 | `finish_reason == "length"` | 续写一次，或标记不完整 | 别把半句话当结论 |
| 步数上限 | `Step >= MaxSteps` | `StatusMaxStep` | **返回已有的部分结果 + 说明**，不要只报错 |
| 预算上限 | `cost > Budget` | `ErrBudget` | 记审计，给用户「预算耗尽」话术 |
| 死循环 | 同指纹出现 >2 次 | `ErrLoopDetected` | 43.1 的唯一解药 |
| 用户取消 | `ctx.Err() != nil` | 直接返回 | **不算错误**，不要打 error 日志污染告警 |
| 显式终止 | 模型调用 `final_answer` 工具 | `StatusDone` | **推荐**：结构化收尾，比「猜它是不是说完了」靠谱得多 |

`MaxSteps` 到底设多少？经验值：**工具型任务 6~10 步；深度检索 15~20；超过 30 步几乎一定是设计问题**（任务该拆成子 Agent，见第 46 章）。宁可 `MaxSteps=12` + 「步数耗尽时让模型基于已有信息作答」，也不要 `MaxSteps=50` 然后 3 美元一条。

> 💡 **「步数耗尽」的处理比步数本身更重要。** 好的实现是：达到上限时追加一条 system 消息「你必须现在就基于已有信息给出结论，并说明哪些部分未完成」，再跑**最后一次不带工具的调用**。用户体验从「系统错误」变成「有缺口的答案」，观感差一个数量级。

---

## 43.6 编排范式：Plan / Route / Reflect / Critic

### 43.6.1 Plan-and-Execute

```
 Planner（一次调用，输出 JSON 计划）
    │ plan = ["读 q3.pdf", "读 q4.pdf", "对比营收", "输出表格"]
    ▼
 ┌──▶ Executor 取第 i 步 → 可能调工具 → 结果入 Scratch
 │        │
 │        ├── 成功？ ──否──▶ Replanner（只重写剩余步骤，已完成的不动）
 │        └── 是 ──▶ i++ ，直到 i == len(plan)
 └────────┘
    ▼
 Summarizer（可选，把 Scratch 汇总成最终答案）
```

优点：**计划可审、可暂停、可并行**（步骤之间无依赖时用 `errgroup`）。缺点：**计划可能一开始就错**，所以必须有 replanner，且要用「结构化输出」保证计划是合法 JSON（第 40 章 40.5）。

### 43.6.2 Reflection / Critic

```go
// 最简反思循环：生成 → 批评 → 修订。最多两轮，再多就是烧钱。
func (a *Agent) SolveWithReflection(ctx context.Context, st *State, emit Emitter) (*llm.Message, error) {
	var answer *llm.Message
	for round := 0; round < 2; round++ {
		ans, err := a.Run(ctx, st, emit) // 一轮完整的 ReAct
		if err != nil {
			return nil, err
		}
		answer = ans

		critique, ok, err := a.critique(ctx, st, ans) // 结构化输出：{ok:bool, issues:[...]}
		if err != nil {
			return nil, err
		}
		if ok {
			break
		}
		// 关键：把批评作为一条 **user/system** 消息注入，而不是重写历史
		st.Messages = append(st.Messages,
			llm.Message{Role: llm.RoleUser, Content: "上一版答案有以下问题，请修正后重新作答：\n" + critique})
	}
	return answer, nil
}
```

三条经验：

1. **批评必须是结构化的**（`{"ok":false,"issues":["未引用来源","第二段结论与数据矛盾"]}`），否则你无法判断「它到底认不认可」，只能再让模型自己解读——大概率每次都认可。
2. **成本是 2~3 倍**。只给高价值任务开（写代码、出报告、法务），别给「今天天气」开反思。
3. **批评者最好换一个模型**（或至少换一个 prompt / 温度），否则同温层互相吹捧，「自我批评」变成「自我表扬」。

### 43.6.3 Router

一次分类调用 + 分发，不做循环：

```go
type Route struct {
	Name    string `json:"name"`    // "rag" | "sql" | "tool_agent" | "escalate"
	Conf    float64 `json:"conf"`
	Args    map[string]any `json:"args"`
}
```

`Conf < 0.6` 就走兜底（问用户澄清 / 转人工）。**Router 的准确率是产品体验的上限**，值得单独做一个离线评测集（第 47 章）。

---

## 43.7 状态机 vs DAG：什么时候该上「图」

| 维度 | 状态机（手写 for/switch） | DAG / 图引擎（eino `graph`、langgraph 风格） |
| --- | --- | --- |
| 表达循环 | 天然（`for` 就是循环） | 需要显式「有环边」+ 迭代上限 |
| 表达并行分支 | 手写 `errgroup` | 一等公民（fan-out / fan-in） |
| 可视化 | 差（得自己画） | 好（图就是文档） |
| 断点恢复 | 需要自己写 checkpoint | 引擎给（节点级 checkpoint） |
| 动态路由 | 最灵活（`switch` 任意跳） | 靠条件边，复杂路由会很难读 |
| 学习成本/依赖 | 零 | 一个框架 + 它的心智模型 |
| 适合 | **Agent 主循环**（99% 的场景） | RAG 管线、多阶段数据处理、固定流程编排 |

我的判断很直接：**Agent 主循环用手写状态机，业务管线用图引擎。** 因为主循环的本质是「不定的圈数 + 高度动态的路由」，你把它塞进图里，最后会得到一张谁也不敢改的蜘蛛网。反过来，RAG 那种「解析 → 切分 → embedding → 检索 → rerank → 拼装」的固定 DAG，用图引擎能白拿并行、可视化和缓存。

eino 的做法值得看一眼（第 49 章细读）：它用 `Graph` + 条件边（branch）实现 DAG，用「环」+ `MaxRunSteps` 支持 Agent 循环，用 `schema.StreamReader` 把「流」在节点间传递。**它是把两种范式揉在一个抽象里**——代价是你必须理解它的流传播规则。

---

## 43.8 流式事件模型：token / tool / step

```go
type EventType string

const (
	EvStepStart  EventType = "step_start"  // 一轮开始（前端画"第 n 步"）
	EvToken      EventType = "token"       // 增量文本
	EvMessage    EventType = "message"     // 一轮完整消息（含 tool_calls）
	EvToolCall   EventType = "tool_call"   // 要调工具了（前端显示"正在查询…"）
	EvToolResult EventType = "tool_result" // 工具结果（可能被截断）
	EvInterrupt  EventType = "interrupt"   // 需要人工介入
	EvDone       EventType = "done"
	EvError      EventType = "error"
)

type Event struct {
	Type EventType
	Step int
	Text string
	Call *llm.ToolCall
	Res  *tool.Result
	Err  error
}
```

`Event` 一路 `emit` 出去，最后一个环节往往就是第 39 章的 SSE handler：

```go
func (s *api) chat(w http.ResponseWriter, r *http.Request) {
	rc := http.NewResponseController(w)
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)

	emit := func(ev agent.Event) error {
		b, _ := json.Marshal(ev)
		// token 内的换行必须转义（SSE 的 data 不能有裸换行，第 39 章）
		if _, err := fmt.Fprintf(w, "event: %s\ndata: %s\n\n", ev.Type, b); err != nil {
			return err // 客户端走了 → 一级级往上冒泡，最终 cancel 上游 LLM
		}
		return rc.Flush()
	}
	if err := s.agent.Run(r.Context(), st, emit); err != nil && !errors.Is(err, context.Canceled) {
		_ = emit(agent.Event{Type: agent.EvError, Text: err.Error()})
	}
}
```

两个必须处理的性能问题：

**① 事件爆炸。** 一个 2000 token 的回答 = 2000 个 SSE 事件 = 前端 2000 次 `setState`，React 直接卡死。服务端合批：

```go
// batchEmitter：把连续的 token 事件攒够 maxBytes 再发一次。
// 生产里再配一个 50ms 的 ticker 兜底（长尾单个字符也要及时出去）。
func batchEmitter(inner agent.Emitter, maxBytes int) agent.Emitter {
	var mu sync.Mutex
	var buf strings.Builder
	flush := func() {
		mu.Lock()
		s := buf.String()
		buf.Reset()
		mu.Unlock()
		if s != "" {
			_ = inner(agent.Event{Type: agent.EvToken, Text: s})
		}
	}
	return func(ev agent.Event) error {
		if ev.Type != agent.EvToken {
			flush() // 非 token 事件前必须冲掉，保证顺序
			return inner(ev)
		}
		mu.Lock()
		buf.WriteString(ev.Text)
		n := buf.Len()
		mu.Unlock()
		if n >= maxBytes {
			flush()
		}
		return nil
	}
}
```

**② 背压。** `emit` 必须是**同步阻塞**的（写阻塞 = 背压，模型输出自然变慢）。**千万不要 `go emit(ev)`**——你会得到「事件乱序 + 无界 goroutine + 前端显示错乱」三件套（第 25 章）。

---

## 43.9 HITL：中断与恢复

```
  Agent                Checkpoint(Redis/PG)         用户(WebSocket/前端)
    │  step 3: 要调 delete_files（危险工具）            │
    │                                              │
    ├── st.Pending = [...]，st.Status=waiting ─────▶│
    ├── Save(ctx, st) ─────────────▶ [状态快照]     │
    ├── emit(interrupt) ───────────────────────────▶│ 弹窗："允许删除 32 个文件？"
    └── return nil（挂起，goroutine 结束）           │
                                                    │ 用户点"同意"（可能是 3 小时后）
    ┌───────────────────────────────────────────────┘
    │  POST /resume {state_id, decisions:[{approved:true}]}
    ▼
  Load(ctx, id) ◀──── [状态快照]
    │
    ├── 对 approved 的调用【只执行一次】 → 生成 tool 消息
    ├── 对 rejected 的调用 → 生成"用户拒绝"的 tool 消息（模型必须知道）
    ├── st.Pending = nil，st.Step++ 
    └── 继续 Run()（从 THINK 开始，历史完整，不用重跑已完成的工具）
```

```go
type Checkpoint interface {
	Save(ctx context.Context, st *State) error
	Load(ctx context.Context, id string) (*State, error)
}

type Decision struct {
	Approved bool   `json:"approved"`
	Reason   string `json:"reason,omitempty"`
}

func (a *Agent) Resume(ctx context.Context, id string, decisions []Decision, emit Emitter) error {
	st, err := a.ckpt.Load(ctx, id)
	if err != nil {
		return err
	}
	if st.Status != StatusWaiting || len(st.Pending) != len(decisions) {
		return fmt.Errorf("agent: state %s not resumable", id)
	}
	for i, p := range st.Pending {
		d := decisions[i]
		if !d.Approved {
			// 拒绝也要回一条 tool 消息！否则协议坏掉（第 41 章 41.5）
			st.Messages = append(st.Messages, llm.Message{
				Role: llm.RoleTool, ToolCallID: p.Call.ID, Name: p.Call.Name,
				Content: `{"error":"user_rejected","reason":` + strconv.Quote(d.Reason) + `}`,
			})
			continue
		}
		res, err := a.Tools.Call(ctx, p.Call) // 批准后才执行，且只执行一次
		if err != nil {
			st.Messages = append(st.Messages, errMessage(p.Call, err))
			continue
		}
		st.Messages = append(st.Messages, llm.Message{
			Role: llm.RoleTool, ToolCallID: p.Call.ID, Name: p.Call.Name, Content: res.Content,
		})
	}
	st.Pending = nil
	st.Step++
	st.Status = StatusRunning
	if err := a.ckpt.Save(ctx, st); err != nil {
		return err
	}
	return a.Run(ctx, st, emit) // 接着跑
}
```

四条硬规则：

1. **恢复绝不能重放已完成的工具**。它们的结果已经在 `Messages` 里了，再跑一次就是重复副作用（第 41 章 41.8 的幂等键是第二道保险）。
2. **拒绝也必须生成一条 `role:"tool"` 消息**。模型需要知道「人说不」，否则它会换个说法再试一遍。
3. **`State` 必须能跨进程读**。所以 checkpoint 要落 Redis/Postgres（第 45 章），而且要带 `Version`/`UpdatedAt`，做乐观锁防止两个请求同时 resume。
4. **恢复是有权限的**。`/resume` 必须校验「这个状态属于这个用户/租户」，否则 `state_id` 就是越权入口。

> 💡 如果你需要「跑几小时、跨机器、失败自动重试」的持久化执行，别自己造这套：用 Temporal（第 46 章）。它把「状态保存 + 确定性重放」做成了运行时语义，代价是你得接受它的编程模型（workflow 里不能有随机数和真实时间）。

---

## 43.10 失败重规划：换工具、改计划、还是认输

| 失败层级 | 例子 | 处理 |
| --- | --- | --- |
| 工具级 | 超时、5xx | 局部重试（幂等时）→ 换等价工具 → 报错给模型 |
| 计划级 | 计划里的第 2 步根本没有可用数据 | **重规划**：丢弃未执行步骤，保留已有观察 |
| 目标级 | 用户要的东西不存在 | 直接说明，别硬凑（最容易被忽视、也最重要） |

重规划的实现在「怎么给模型喂失败信息」上：

```go
// 反例：把全部失败历史原样留在上下文里 → 模型被"污染"，反复纠结同一个坑
// 正解：把失败压缩成一条"经验"，然后清掉废步骤
func (a *Agent) replan(ctx context.Context, st *State, cause error) error {
	st.ReplanCount++
	if st.ReplanCount > 2 {
		st.Status = StatusFailed
		return fmt.Errorf("%w: replan budget exhausted", cause)
	}
	obs := summarizeFailures(st.Messages, 2000) // 只留结论，不留原始报错
	st.Messages = append(st.Messages, llm.Message{
		Role: llm.RoleUser,
		Content: "之前的计划失败了，原因是：" + obs +
			"\n请输出一份新计划（JSON 数组），不要重复已经失败的步骤；若任务无法完成，直接说明。",
	})
	st.Plan = nil
	return nil
}
```

**重规划次数必须有限**（2 次足矣）。没有上限的重规划会把「工具失败」放大成「无限循环」，而且比死循环检测更难发现——因为每次参数都不同。

---

## 43.11 Java 对照：四处会猜错的地方

1. **没有 Spring StateMachine 那套框架，也不需要。** `for` + `switch` 就是最好的状态机；引入框架只会让你在「动态路由」时和它的 DSL 打架。**Go 里状态机的价值在于 `State` 是个可序列化的 struct，而不是在于某个库。**
2. **`CompletableFuture` 链无法表达「不定的圈数」。** Java 里想把多轮工具调用串起来，很容易写成递归 `thenCompose`，栈越深越难调。Go 的 `for` 循环是等价的、可中断的、可断点的——**这是 Go 写 Agent 编排比 Java 舒服的真正原因**。
3. **线程中断 ≠ `ctx`。** `Thread.interrupt()` 只能打断阻塞点，且谁都可以忽略它。`ctx` 是**显式传递的值**，你不传就没有——所以「取消穿透」在 Go 里是「设计出来的」而不是「碰巧的」（第 24 章）。
4. **持久化状态的样子不一样。** Java 侧你可能习惯 JPA 实体 + 数据库列；这里 `State` 就是一个 JSON blob（PG 的 `jsonb` 列），按 `id` + `version` 乐观锁更新。**别为 Agent 状态设计 12 张表**，那样的迁移成本会杀死你的迭代速度。

---

## 43.12 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| 模型把同一个工具调 47 次 | 没有重复调用检测 | `Seen[指纹]++ > 2` 即失败 | 43.4 |
| 一次请求烧掉 3 美元 | `MaxSteps` 太大、无预算闸门 | `MaxSteps≤12` + `cost>budget` 中断 | 43.5 |
| 用户点"停止"没反应 | 只在 handler 检查了一次 ctx | 每轮循环开头 + 每次 emit 失败都检查 | 43.4 |
| 暂停后恢复，工具执行了两遍 | 恢复时重放了已完成调用 | 结果已在 `Messages` 里，只执行 `Pending` | 43.9 |
| 恢复时报 400 | 拒绝的那条没回 `role:"tool"` 消息 | 拒绝也要生成 tool 消息 | 43.9 |
| 前端卡死 | 2000 个 token 事件逐个 setState | 服务端合批 + 前端 rAF 合并 | 43.8 |
| 事件乱序 / goroutine 爆炸 | `go emit(...)` | `emit` 必须同步 | 43.8 |
| 达到步数上限只报"系统错误" | 没有「收尾」流程 | 追加 system 消息 + 最后跑一次无工具调用 | 43.5 |
| Agent 状态存在内存里，重启就丢 | 状态在局部变量/结构体字段里 | `State` 序列化 + checkpoint | 43.9 |
| 反思循环停不下来 | 批评不是结构化输出 | `{"ok":bool,"issues":[]}` + 最多 2 轮 | 43.6 |
| 重规划无限循环 | 没有 replan 预算 | ≤2 次，超了就认输 | 43.10 |

## 43.13 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 状态模型 | `State` 是可 JSON 化的值：`Step/Status/Messages/Seen/Usage`，绝不留局部变量 |
| 事件模型 | `Event` 与 `State` 分离；`emit` 同步、可返回 error = 天然背压 |
| 主循环 | `for st.Step < MaxSteps`：闸门（ctx/预算）→ think → 终止判定 → 循环检测 → 并行工具 |
| 终止 | 6 个出口：完成/截断/步数/预算/死循环/取消；**推荐显式 `final_answer` 工具** |
| 死循环 | 指纹 = 工具名 + 规范化参数 hash，同一指纹 >2 次即失败 |
| 范式 | ReAct 主循环；Plan-Execute 可审可并行；Router 最省；Reflection 2~3 倍成本 |
| 状态机 vs DAG | 主循环用手写状态机，固定管线用图引擎 |
| 流式 | token 要合批（前端 setState 会死）；工具/步骤事件让 UI 有进度感 |
| HITL | 保存 State → 挂起 → 批准后**只执行一次** → 拒绝也要回 tool 消息 |
| 恢复 | 状态要跨进程（Redis/PG）+ 乐观锁 + 校验归属，防越权 resume |
| 重规划 | 压缩失败信息成"经验"，丢弃废步骤，预算 ≤2 次 |

## 43.14 自检题

1. **为什么 `Step` 要放在 `State` 里，而不是主循环的局部变量 `i`？**
   > 答案要点：① HITL/失败恢复要从同一个步数继续，局部变量一重置就白送预算、也会让 `MaxSteps` 失效；② 步数要进审计与 UI（"第 3 步"）；③ 状态机要求「所有会影响下一步决策的东西都在状态里」——这是可重放的前提。

2. **用户点了「停止」，你的 Agent 还在跑工具。列出从浏览器到工具进程这条链上「必须检查 ctx」的四个位置。**
   > 答案要点：① SSE handler 的 `emit` 写失败 → 返回 error 冒泡；② 主循环每轮开头的 `ctx.Err()`；③ `LLM.ChatStream(ctx, ...)`（取消后 HTTP 请求被 cancel，上游停止计费）；④ `Tools.RunAll` 里每个工具的 `WithTimeout(ctx, ...)` + `exec.CommandContext`（连子进程一起杀）。任何一环用了 `context.Background()` 就断了。

3. **你的 Agent 在第 3 步要调 `deploy_prod`，暂停等审批，用户 3 小时后批准。恢复时必须保证哪三件事？**
   > 答案要点：① **不重放**第 1~3 步已执行的工具（结果已在 `Messages`，否则重复副作用）；② 拒绝的那条必须生成 `role:"tool"` 的拒绝消息，否则下一轮 400；③ 恢复请求要校验 `state_id` 的归属（用户/租户/过期），并且 `deploy_prod` 本身要有幂等键做第二道保险（3 小时内可能被点了两次批准）。

## 43.15 延伸阅读

- `github.com/tmc/langchaingo/agents`：`executor.go`（几乎是最小的 ReAct 实现，200 行内读懂）
- `github.com/cloudwego/eino`：`compose/graph.go`（条件边、并行、checkpoint）、`schema/stream.go`
- 原始论文：ReAct（Yao et al., 2022）、Reflexion（Shinn et al., 2023）、Plan-and-Solve（Wang et al., 2023）
- Anthropic「Building effective agents」：把 workflow 与 agent 分开讨论的那篇，架构判断很清醒
- Temporal Go SDK 文档（持久化执行，第 46 章）
- 上一章：[42. MCP：协议与 Go SDK 实战](42-mcp.md)
- 下一章：[44. RAG 管线：从文档到上下文](44-rag-pipeline.md)

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：第 40 章（流式调用）、第 41 章（工具执行与结果回填）、第 42 章（MCP 工具源）、第 24 章（ctx 取消树）
- 下一章：第 44 章（RAG 作为工具/节点接入）
- 相关：第 39 章（`emit` → SSE）、第 45 章（历史裁剪与记忆）、第 46 章（多 Agent 与持久化执行）、第 49 章（eino 的图编排）
