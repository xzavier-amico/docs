# 6. Session / Run / Step / Event 状态模型

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · Agent-Client / Agent-Server 分离架构 ｜ **编号**：6 / 52
> **本章定位**：定义 Agent 平台的「进程/线程/系统日志」——五层对象模型、Run 与 Step 的状态机、事件溯源为什么是唯一正确姿势，以及事件 schema 的版本演进与幂等去重。
> **读法**：§6.2 的四种存储姿势对比决定你的整个数据架构；§6.4 的 Go struct 与 SQL 可以直接抄；§6.4.4 的 upcaster 链是可以救命的一段代码。

---

## 6.1 引子：一个无法回答的问题

客服主管在群里 @你：「客户说 5 月 1 日上午 10 点 23 分那次对话，我们的 Agent 承诺给他全额退款，现在不肯认。请提供当时的完整依据。」

你打开数据库，看到的是这个：

```json
{
  "session_id": "s_8f2a...",
  "messages": [ ... 一堆历史 ... ],
  "updated_at": "2026-05-01T10:23:41Z",
  "context_length": 43821
}
```

**这行 JSON 回答不了任何一个必要问题**：当时上下文里到底有哪 7 段内容？检索到了哪 3 篇文档、它们的版本号是多少？模型是哪个版本？提示词是 v37 还是 v38？为什么它认为可以退款——是知识库里有旧政策，还是提示词没写清？审核是谁放行的？钱是怎么算的？

更糟的是，`messages` 是被**覆盖写**的。第 3 轮那次覆盖，已经把第 2 轮的真实状态永久抹掉了。你不是「查不到」，你是**从来没存过**。

> 🩸 这就是本章要解决的问题：**Agent 平台的每一次执行，必须留下一条不可变、可回放、可举证的事实序列。** 做不到这一点的平台，在第一次正经投诉面前就失去公信力——而这件事发生的时间，通常在上线后第 2~4 个月。

---

## 6.2 断崖：四种存储姿势，只有一种能活过第二年

| # | 姿势 | 典型写法 | 能回答「当时发生了什么」吗 | 主要问题 | 适用 |
| --- | --- | --- | --- | --- | --- |
| 1 | 进程内存 | `sync.Map[runID]*State` | ❌ | 滚动更新即丢；多副本不一致 | 单机 Demo |
| 2 | 覆盖快照（KV） | `SET run:123 {state}` / `UPDATE runs SET state=...` | ❌ | 历史被抹掉；并发覆盖丢更新；无法审计 | 短任务、无合规要求 |
| 3 | **只有 checkpoint**（框架内置） | LangGraph checkpointer 之类 | ⚠️ 只能回答「最后一次状态」 | 为「继续执行」设计，不为「举证」设计 | 恢复执行可用，审计不行 |
| 4 | **事件溯源**（append-only + 投影 + 快照） | 下文 §6.4 | ✅ | 重放成本、schema 演进成本 | **生产平台唯一正确姿势** |

用 Aurora 的规模算一遍这笔账，你就知道为什么姿势 1/2 一定会塌：

```
日均 Run 216 万，每 Run 42 个事件 → 9,072 万事件/日
若把每个 token 也落成事件：1,200 delta/Run × 216 万 = 26 亿/日 → 3.1 TB/日（不可接受）
所以：delta 只走流式通道，不落事件库；落库的是 llm.responded（聚合文本 + token + 成本 + 哈希）
实际落库：42 事件/Run，13.6 GB/日（ClickHouse + zstd）
峰值事件写：在途 Run 3,600 × (42/30s) ≈ 5,040 事件/秒
```

> 💡 **一个必须先钉死的判断**：事件溯源不是「把所有东西都记下来」。它是「把所有**改变了系统状态**的东西记下来」。token 增量不改状态（它是同一份输出的碎片），所以它走传输层，不进真相源。混淆这两者，你的事件库会在一周内爆掉。

### 6.2.1 覆盖写杀死的四件事

| 被杀死的能力 | 具体后果 | 代价 |
| --- | --- | --- |
| **审计举证** | 无法证明「当时系统做了什么」 | 监管处罚 / 诉讼不利（第 31 章） |
| **精确计费** | 只知道总数，不知道每一步的模型与 token | 对账差 7~11%（第 27 章） |
| **失败重放** | 复现不了 bug，只能猜 | 故障定位从分钟级变小时级 |
| **上下文重建** | 无法回答「第 3 步时上下文里有什么」 | 提示词调优靠玄学（第 15 章） |

---

## 6.3 结构图

### 6.3.1 五层对象模型

```
┌──────────────────────────────────────────────────────────────────┐
│ L1  Agent（定义 + 版本快照）                                        │
│     agent_id · version · 角色卡版本 · 工具集版本 · 模型策略版本        │
│     不参与执行，但每次 Run 都要钉住它的一个版本（否则无法复现）          │
│     1 个 Agent  ──▶  N 个 Session                                 │
└───────────────────────────┬──────────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│ L2  Session（会话：用户感知的「这段对话」）                          │
│     session_id · tenant_id · user_id · 记忆命名空间 · 状态           │
│     1 个 Session ──▶ N 个 Run（用户每发一次意图 = 一个 Run）          │
└───────────────────────────┬──────────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│ L3  Run（一次执行：可取消、可恢复、可计费的最小单位）                  │
│     run_id · 状态机 · 预算 · 版本五元组 · 输入输出                   │
│     1 个 Run ──▶ N 个 Step（典型 5，编码 Agent 可达 80）            │
└───────────────────────────┬──────────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│ L4  Step（推进单元：一次模型调用 或 一次工具调用 或 一次审批等待）      │
│     step_id · kind(llm|tool|approval|retrieve) · 输入摘要 · 产物     │
│     1 个 Step ──▶ N 个 Event                                      │
└───────────────────────────┬──────────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│ L5  Event（原子事实：不可变、有序、可回放）                          │
│     event_id · (run_id, seq) · type · v · at · actor · payload     │
│     ★ 唯一真相源。Run/Step 的状态是事件的投影（projection）           │
└──────────────────────────────────────────────────────────────────┘
```

| 层 | 基数（Aurora 日均） | 保留期 | 存储 | 可否变更 |
| --- | --- | --- | --- | --- |
| Agent | 约 4.2 万个定义，含版本约 31 万条 | 永久 | Postgres | 可（新版本，不改旧版） |
| Session | 约 380 万 | 3 年（可配置） | Postgres | 可（仅元数据） |
| Run | **216 万/日** | 永久（热 90 天转冷） | Postgres + S3 | **仅状态字段**（由投影写入） |
| Step | 约 1,080 万/日 | 90 天 | ClickHouse | ❌ |
| Event | **9,072 万/日** | 90 天热 + 审计子集 3 年 | ClickHouse + WORM | ❌ **append-only** |

### 6.3.2 Run 状态机

```
                        ┌──────────┐
                        │ created  │ 已受理，未占资源
                        └────┬─────┘
                   admit ────┤──── reject / queue_timeout
                             ▼                    ▼
                        ┌─────────┐         ┌──────────┐
                        │ admitted│         │ rejected │ 终态
                        └────┬────┘         └──────────┘
                     dispatch│
                             ▼
        ┌───────────────────────────────────────────────┐
        │                  running                      │◀──────┐
        └──┬────────┬──────────┬──────────┬─────────────┘       │
   tool    │        │ approval │ cancel   │ 无工具调用           │
   ┌───────┘        └──────┐   │          ▼                     │
   ▼                       ▼   │     ┌────────────┐             │
┌──────────────┐  ┌──────────────┐   │ finalizing │ 输出审计    │
│ waiting_tool │  │waiting_approval│  └──────┬─────┘             │
└──────┬───────┘  └──────┬───────┘          │ pass/corrected    │
       │ 结果回传         │ 批/拒             ▼                   │
       └──────────────────┴──────────────▶ running（下一步）─────┘
                             │
                             ▼
        ┌───────────┬────────┬─────────┬──────────────┐
        │ completed │ failed │ cancelled│budget_exceeded│
        └───────────┘────────┴─────────┴──────────────┘  均为终态
```

| 迁移 | 触发 | 守卫条件 | 必须产出的事件 | 失败兜底 |
| --- | --- | --- | --- | --- |
| `created → admitted` | 准入通过 | 配额足、在途未超限、租户未冻结 | `run.admitted` | 拒绝 → `run.rejected` + 明确原因码 |
| `created → rejected` | 准入拒绝 | — | `run.rejected` | 返回可重试建议（排队 vs 限流 vs 余额） |
| `admitted → running` | worker 领取 | 租约获取成功、预算预扣成功 | `step.started` | 领取失败超 3 次 → 回队列 |
| `running → waiting_tool` | 模型返回 tool_calls | 工具在白名单、参数过 schema | `tool.requested` | 工具超时 → `step.failed` + 重试策略 |
| `running → waiting_approval` | 命中高危动作 | 审批策略命中 | `approval.requested` | 审批超时（默认 30 min）→ `run.expired` |
| `running → finalizing` | 无 tool_calls 且输出完整 | 输出非空、结构化校验通过 | `output.audited` | 校验失败 → 重修一次 → 仍失败 → `failed` |
| `finalizing → completed` | 审计通过 | 计费已结算 | `billing.settled` + `run.completed` | 结算失败 → 进补偿队列（账不能丢） |
| `* → cancelled` | 用户取消 | 非终态 | `run.cancelled` | 取消 1s 未生效 → 强制标记 + 补偿 |
| `* → budget_exceeded` | 预算耗尽 | 成本或步数达上限 | `run.budget_exceeded` | 落差额账单，返回部分结果 |

**一条设计要点**：`finalizing` 这个状态很多人不设，直接 `running → completed`。后果是「输出审计」和「计费结算」变成 completed 之后的异步动作——**那意味着你的 `completed` 是不可信的**。Aurora 把它做成一等状态，就是为了让「已完成的 Run = 已审过、已算过账」。

---

## 6.4 关键设计

### 6.4.1 Go：五层对象与事件信封

```go
// domain/model.go
package domain

type AgentRef struct {
	AgentID       string `json:"agent_id"`
	Version       int    `json:"version"`
	PersonaVer    string `json:"persona_ver"`     // 角色卡版本
	ToolsetVer    string `json:"toolset_ver"`
	PromptVer     string `json:"prompt_ver"`
	ModelPolicy   string `json:"model_policy_ver"`
	// ★ 这五个版本号必须进每一次 Run，否则无法复现（第 42 章）
}

type Session struct {
	TenantID     string    `json:"tenant_id"`
	SessionID    string    `json:"session_id"` // ulid
	Agent        AgentRef  `json:"agent"`
	UserID       string    `json:"user_id"`
	State        string    `json:"state"`      // active|archived|deleted
	MemoryNS     string    `json:"memory_ns"`  // 记忆命名空间，删除时按此级联
	CreatedAt    time.Time `json:"created_at"`
	LastRunAt    time.Time `json:"last_run_at"`
	ContextVer   int64     `json:"context_ver"` // 客户端持有的版本指针（第 5 章）
}

type Run struct {
	TenantID   string   `json:"tenant_id"`
	RunID      string   `json:"run_id"`     // ulid
	SessionID  string   `json:"session_id"`
	Agent      AgentRef `json:"agent"`      // 创建时钉住的版本快照

	State      RunState `json:"state"`
	StateVer   int64    `json:"state_ver"`  // 乐观锁；每次迁移 +1

	Seq        int64    `json:"seq"`        // 已分配的最大事件序号
	StepCount  int      `json:"step_count"`
	CostUSD    float64  `json:"cost_usd"`
	TokensIn   int64    `json:"tokens_in"`
	TokensOut  int64    `json:"tokens_out"`

	Budget     Budget   `json:"budget"`
	Deadline   time.Time `json:"deadline"`  // 硬超时（含排队）
	OwnerLease string    `json:"owner_lease"` // worker 租约，防双跑

	IdemKey    string   `json:"idem_key"`
	TraceID    string   `json:"trace_id"`
	CreatedAt  time.Time `json:"created_at"`
	EndedAt    *time.Time `json:"ended_at,omitempty"`
}
```

```go
// domain/event.go —— 事件信封（所有事件共用，payload 各自 schema）
type Event struct {
	EventID   string          `json:"event_id"`   // ulid：单调可排序，含时间
	TenantID  string          `json:"tenant_id"`  // 多租户贯穿（第 44 章）
	RunID     string          `json:"run_id"`
	SessionID string          `json:"session_id"`
	Seq       int64           `json:"seq"`        // 每 Run 内单调递增，从 1 开始
	Type      string          `json:"type"`       // "llm.responded" 等
	V         int             `json:"v"`          // payload schema 版本
	At        time.Time       `json:"at"`
	Actor     Actor           `json:"actor"`      // user|agent|system|tool|reviewer
	TraceID   string          `json:"trace_id"`
	IdemKey   string          `json:"idem_key,omitempty"` // 业务幂等键
	Payload   json.RawMessage `json:"payload"`
}

type Actor struct {
	Kind string `json:"kind"` // user|agent|system|tool|reviewer
	ID   string `json:"id"`
}

// RunState 由事件投影得出，任何时刻都能从空状态重放重建
type RunState string

const (
	StateCreated        RunState = "created"
	StateAdmitted       RunState = "admitted"
	StateRunning        RunState = "running"
	StateWaitingTool    RunState = "waiting_tool"
	StateWaitingApprove RunState = "waiting_approval"
	StateFinalizing     RunState = "finalizing"
	StateCompleted      RunState = "completed"
	StateFailed         RunState = "failed"
	StateCancelled      RunState = "cancelled"
	StateExpired        RunState = "expired"
	StateBudgetExceeded RunState = "budget_exceeded"
)

var terminal = map[RunState]bool{
	StateCompleted: true, StateFailed: true, StateCancelled: true,
	StateExpired: true, StateBudgetExceeded: true, "rejected": true,
}

// Apply 是唯一的纯函数状态迁移。所有状态都从这里长出来。
func Apply(s RunState, ev Event) (RunState, error) {
	switch ev.Type {
	case "run.admitted":
		if s != StateCreated { return s, ErrIllegalTransition }
		return StateAdmitted, nil
	case "step.started":
		if s != StateAdmitted && s != StateRunning && s != StateWaitingTool {
			return s, ErrIllegalTransition
		}
		return StateRunning, nil
	case "tool.requested":
		if s != StateRunning { return s, ErrIllegalTransition }
		return StateWaitingTool, nil
	case "approval.requested":
		return StateWaitingApprove, nil
	case "run.completed":
		if s != StateFinalizing { return s, ErrIllegalTransition }
		return StateCompleted, nil
	case "run.cancelled":
		if terminal[s] { return s, ErrAlreadyTerminal } // 终态不可再迁
		return StateCancelled, nil
	}
	return s, nil
}
```

### 6.4.2 事件的 42 条清单（Run 的事件契约）

| 类别 | 事件类型 | 条数/Run | 是否进审计链 |
| --- | --- | --- | --- |
| 生命周期 | `run.created` `run.admitted` `run.started` `run.suspended` `run.resumed` `run.finalizing` `run.completed`/`failed`/`cancelled`/`expired` | 8~11 | ✅ |
| 模型 | `llm.requested` `llm.responded`（含 model、prompt_ver、tokens、cost、ttft、内容哈希） | 5 × 2 = 10 | ✅ |
| 工具 | `tool.requested` `tool.responded` `tool.rejected` `tool.timeout` | 4 × 2 ≈ 8 | ✅ |
| 上下文 | `context.assembled`（预算分配、检索命中、压缩比） | 5 | ⚠️ 摘要进链 |
| 审批 | `approval.requested` `approval.granted` `approval.denied` | 0~3 | ✅ |
| 输出 | `output.audited` `output.corrected` | 1~2 | ✅ |
| 计费 | `billing.reserved` `billing.settled` `billing.refunded` | 3 | ✅ |
| 合计 | — | **≈ 42** | — |

> ⚠️ 注意这里**没有** `llm.delta`。delta 走 SSE 传输通道，由 `llm.responded` 汇总落库。这条边界如果搞错，事件量会从 9,072 万/日 变成 26 亿/日，成本差 29 倍。

### 6.4.3 SQL：真相源与投影

```sql
-- ① 事件表：唯一真相源。append-only，无 UPDATE，无 DELETE。
CREATE TABLE events (
    tenant_id   text        NOT NULL,
    run_id      text        NOT NULL,
    seq         bigint      NOT NULL,
    event_id    text        NOT NULL,
    session_id  text        NOT NULL,
    type        text        NOT NULL,
    v           smallint    NOT NULL,
    at          timestamptz NOT NULL,
    actor_kind  text        NOT NULL,
    actor_id    text        NOT NULL,
    trace_id    text,
    idem_key    text,
    payload     jsonb       NOT NULL,
    -- 哈希链：h = sha256(prev_h || 规范化后的本行)  ← 见第 31 章
    prev_hash   bytea,
    row_hash    bytea       NOT NULL,
    PRIMARY KEY (tenant_id, run_id, seq)
) PARTITION BY RANGE (at);

CREATE TABLE events_2026_05 PARTITION OF events
    FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');

-- 幂等：同一事件只能存在一次
CREATE UNIQUE INDEX events_event_id_uq ON events (event_id);
-- 按会话回放：一次会话的所有事件
CREATE INDEX events_session_seq_idx ON events (tenant_id, session_id, at, seq);
-- 按幂等键查重（工具调用、结算）
CREATE UNIQUE INDEX events_idem_uq ON events (tenant_id, run_id, idem_key)
    WHERE idem_key IS NOT NULL;

-- ② Run 投影表：从事件推导，可随时重建（DROP 重放即可）
CREATE TABLE runs (
    tenant_id   text        NOT NULL,
    run_id      text        NOT NULL,
    session_id  text        NOT NULL,
    state       text        NOT NULL,
    state_ver   bigint      NOT NULL DEFAULT 0,   -- 乐观锁
    seq         bigint      NOT NULL DEFAULT 0,   -- 已分配最大事件序号
    step_count  int         NOT NULL DEFAULT 0,
    cost_usd    numeric(12,6) NOT NULL DEFAULT 0,
    tokens_in   bigint      NOT NULL DEFAULT 0,
    tokens_out  bigint      NOT NULL DEFAULT 0,
    deadline    timestamptz NOT NULL,
    owner_lease text,
    agent_ver   jsonb       NOT NULL,             -- 五元组版本快照
    created_at  timestamptz NOT NULL,
    ended_at    timestamptz,
    PRIMARY KEY (tenant_id, run_id)
);
-- 只查「我还没跑完的」→ 部分索引，命中率极高
CREATE INDEX runs_active_idx ON runs (tenant_id, state, deadline)
    WHERE state NOT IN ('completed','failed','cancelled','expired','budget_exceeded','rejected');
-- 按会话取最近 Run
CREATE INDEX runs_session_idx ON runs (tenant_id, session_id, created_at DESC);

-- ③ 幂等表：跨 Run 的去重（建 Run、结算、webhook）
CREATE TABLE idempotency (
    scope       text        NOT NULL,  -- 'create_run' | 'settle' | 'webhook'
    idem_key    text        NOT NULL,
    result_hash text        NOT NULL,  -- 首次响应的哈希，重放时直接返回
    result_body jsonb       NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    PRIMARY KEY (scope, idem_key)
);
```

**事件的追加必须是原子的两条 SQL**（投影可以异步，真相源不能）：

```sql
-- 事务 1：抢占序号 + 落事件（用 runs 行锁保证 seq 单写者）
BEGIN;
SELECT seq FROM runs WHERE tenant_id=$1 AND run_id=$2 FOR UPDATE;   -- 串行化点
INSERT INTO events (tenant_id, run_id, seq, event_id, session_id, type, v, at,
                    actor_kind, actor_id, trace_id, idem_key, payload, row_hash)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
ON CONFLICT (tenant_id, run_id, seq) DO NOTHING;                   -- 幂等：重复追加静默丢弃
UPDATE runs SET seq = $3, state = $15, state_ver = state_ver + 1 WHERE tenant_id=$1 AND run_id=$2;
COMMIT;
```

### 6.4.4 Schema 版本演进：upcaster 链

事件一旦写进库就永远是那个样子。你唯一能做的是**在读出时把它升级到当前版本**。

```go
// event/upcast.go
package event

type Upcaster func(raw []byte) ([]byte, error)

// 每个 type 一条升级链：v1→v2→v3。新增字段只能「加」，不能「改语义」。
var upcasters = map[string]map[int]Upcaster{
	"llm.responded": {
		1: v1to2LLMResponded, // v1 只有 text；v2 加了 tool_calls
		2: v2to3LLMResponded, // v2 的 cost 是 float；v3 改成 micro_usd int64（避免浮点误差）
	},
	"tool.responded": {
		1: v1to2ToolResponded, // v1 无 duration_ms；v2 补 0 并标记 estimated
	},
}

// Decode 把任意历史版本升级到 current，业务代码只见最新结构。
func Decode(typ string, v int, raw []byte, current int) ([]byte, error) {
	for v < current {
		up, ok := upcasters[typ][v]
		if !ok {
			return nil, fmt.Errorf("no upcaster: %s v%d->v%d", typ, v, v+1)
		}
		var err error
		if raw, err = up(raw); err != nil {
			return nil, fmt.Errorf("upcast %s v%d: %w", typ, v, err)
		}
		v++
	}
	return raw, nil
}

func v2to3LLMResponded(raw []byte) ([]byte, error) {
	var in struct {
		Text     string  `json:"text"`
		ToolCall []any   `json:"tool_calls,omitempty"`
		CostUSD  float64 `json:"cost_usd"`
	}
	if err := json.Unmarshal(raw, &in); err != nil { return nil, err }
	out := map[string]any{
		"text": in.Text, "tool_calls": in.ToolCall,
		"cost_micro_usd": int64(math.Round(in.CostUSD * 1e6)), // 浮点 → 整数微美元
		"cost_estimated": true,                                // 标记为换算来的
	}
	return json.Marshal(out)
}
```

三条 schema 演进铁律（写进团队的 ADR）：

| # | 铁律 | 违反后的具体故障 |
| --- | --- | --- |
| 1 | **只加不改**：字段只增不删，语义只扩不缩 | 删字段 → 旧代码反序列化静默变零值 → 计费算成 0 |
| 2 | **未知字段必须保留**：用 `json.RawMessage` 存 payload，不信 `map[string]any` 丢字段 | 上游加了字段 → 下游回写时丢失 → 数据静默损坏 |
| 3 | **金额用整数**：`micro_usd int64`，永不用 float | 累计 100 万次后浮点误差累积到可见（对账差） |

### 6.4.5 幂等与去重：三个层次

| 层 | 幂等键 | 冲突时行为 | 状态码 |
| --- | --- | --- | --- |
| HTTP | `Idempotency-Key` header（客户端生成 UUID） | 返回首次响应体（`idempotency` 表） | 200 + 首次结果 |
| 事件追加 | `(tenant_id, run_id, seq)` | `ON CONFLICT DO NOTHING`，静默丢弃 | — |
| 业务副作用 | `(run_id, step_id, tool_name, args_hash)` | 返回缓存的结果，不重复执行 | 200 + 首次结果 |

```go
// 工具网关的幂等实现骨架
func (g *ToolGateway) Invoke(ctx context.Context, c ToolCall) (*ToolResult, error) {
	key := fmt.Sprintf("%s/%s/%s/%s", c.RunID, c.StepID, c.Tool,
		sha256hex(c.Args)) // args 要先规范化（key 排序）再 hash
	if r, ok := g.idem.Get(ctx, key); ok {
		return r, nil // 重复调用：直接返回，不产生第二次副作用
	}
	// 先占位（防止并发双跑），再执行
	if !g.idem.Claim(ctx, key, 30*time.Second) {
		return nil, ErrInFlight
	}
	res, err := g.exec(ctx, c)
	if err != nil {
		g.idem.Release(ctx, key) // 失败释放，允许重试
		return nil, err
	}
	g.idem.Put(ctx, key, res, 24*time.Hour)
	return res, nil
}
```

> ⚠️ `args` 的规范化（JSON key 排序、浮点格式统一、空白折叠）是幂等失效最常见的原因。两个语义相同但字节不同的 args 会算出两个 hash，于是「幂等」变成了「双跑」。

### 6.4.6 快照：让重放回到毫秒级

纯事件溯源的问题是重放成本。一个 80 步的编码 Agent Run 有 ~340 个事件，`Apply` 跑 340 次虽然只要 2~3 ms，但你要**同时**重建上下文视图、预算、工具状态，实际 30~80 ms。每次恢复都付这个钱，P99 就上去了。

```go
// 每 20 个事件打一个快照；恢复时从最近快照开始重放 ≤20 个事件
const snapshotEvery = 20

func (s *Store) Append(ctx context.Context, evs ...Event) error {
	// ... 追加事件 ...
	if last := evs[len(evs)-1]; last.Seq%snapshotEvery == 0 {
		snap, err := Project(ctx, last.RunID, last.Seq) // 投影到当前状态
		if err == nil {
			s.snapshots.Put(ctx, last.RunID, last.Seq, snap) // S3/Redis，存 gzip JSON
		}
	}
	return nil
}
```

| 策略 | 恢复耗时 | 存储成本 | Aurora 取舍 |
| --- | --- | --- | --- |
| 纯重放（无快照） | 30~80 ms | 0 | ❌ P99 不可接受 |
| 每 20 事件快照 | **≤ 5 ms** | 每 Run 约 2~3 个快照 × 40 KB ≈ 100 KB | ✅ 默认 |
| 每步快照 | ≤ 2 ms | 每 Run 约 5 × 40 KB = 200 KB | ⚠️ 仅长任务 |
| 只存最后一次 | ~0 ms | 最小 | ❌ 那是 checkpoint，不是事件溯源 |

---

## 6.5 Java / 分布式经验对照

| 你的经验 | 迁移判断 | 会咬人的地方 |
| --- | --- | --- |
| `UPDATE ... SET state=?`（状态覆盖） | 🔴 **必须改掉** | Agent 的状态不可重算（模型输出不可逆推），覆盖 = 永久丢失 |
| CQRS（读写分离） | 🟢 直接可用 | 但写侧不是「更新实体」，是「追加事件」；读侧是投影表 |
| Event Sourcing（支付/账务系统） | 🟢 **最接近的经验** | 账务事件是确定性的；Agent 事件含非确定性模型输出，重放时**不能重新调模型** |
| Kafka 作为事件存储 | 🟡 要改造 | Kafka 有保留期、不支持按 `(run_id, seq)` 随机读；真相源要落库，Kafka 只做传输（第 13 章） |
| 乐观锁 `version` 字段 | 🟢 直接可用 | 用在 `runs.state_ver` 上防并发迁移 |
| 数据库行锁 `SELECT FOR UPDATE` | 🟢 直接可用 | 用于保证「同一 Run 的 seq 单写者」——这是最容易漏的一点 |
| Airflow / XXL-Job 的任务状态 | 🟡 要改造 | 它们的状态是「调度状态」；Agent 需要「语义状态 + 预算 + 输出审计」 |
| 「日志就是证据」 | 🔴 必须改掉 | 应用日志会被轮转、会被改、会丢；**审计是数据，不是日志**（第 31 章） |

有一条最值得强调：**「重放时绝不能重新调用模型」。** 重放是「把已记录的事实按顺序重新应用」，不是「重新跑一遍」。所以 `llm.responded` 事件里必须存**完整输出**（或对象存储的引用），而不是「当时的入参」——否则重放会得到另一个答案，你的「可复现」就是假的。

---

## 6.6 开源坐标

| 参照 | 它是什么 | 能抄什么 | 缺什么 |
| --- | --- | --- | --- |
| **LangGraph checkpointer**（`thread_id` + `PostgresSaver` 等） | 状态**快照**，语义是「从这里继续跑」 | 恢复执行的工程细节；`thread_id` 作为会话键的做法 | 不是事件历史：无法回答「第 3 步时上下文有什么」「每步花了多少」；快照随代码版本变化而失效 |
| **Temporal 事件历史** | 事件**历史** + 确定性重放 | 最接近的参照：`WorkflowID` + 事件历史 + Activity 重试 + Signal（对应审批/取消） | ① 重放要求**确定性**，而模型调用非确定 → 必须把模型调用包成 Activity 并记录结果，代价是每个 LLM call 都要落历史；② 历史有大小上限（约 5 万事件 / 50 MB 量级，以官方文档为准），超了要 continue-as-new；③ 没有「租户/成本/审计链」语义 |
| **Kafka** | 有序日志 | 分区有序、可重放的思路 | 无状态机、无按主键随机读、有保留期 |
| **EventStoreDB / Axon** | 通用事件溯源框架 | upcaster、快照、投影的成熟模式 | Agent 的事件语义（llm/tool/audit）要自己定义 |
| **Langfuse / Phoenix** | Trace 观测 | span 结构与 `gen_ai.*` 约定 | **trace 是观测，不是真相源**：采样、可丢、不可用于举证与计费 |
| **OpenTelemetry** | 遥测标准 | `trace_id` / `span_id` 贯穿；事件与 span 的关联 | 同上，OTel 管道允许丢数据 |

> 🎯 一句话总结这个坐标：**Temporal 是「执行引擎的事件历史」，LangGraph 是「图的状态快照」，Langfuse 是「观测的 trace」，而 Agent 平台需要的是「可举证的事实序列」——三者都只是局部答案，你要在它们之上定义自己的事件契约。**

---

## 6.7 事故清单

- [ ] **事故 1：覆盖写抹掉历史。** 现象：监管来查三个月前的一次 Run，只有最新状态。→ 根因：快照覆盖。→ 兜底：事件 append-only；DB 账号无 `UPDATE/DELETE` 权限（权限层禁止，不靠自觉）。
- [ ] **事故 2：seq 并发冲突。** 现象：同一 Run 出现两个 `seq=7`，投影错乱。→ 根因：两个 worker 同时写（租约过期 + 无行锁）。→ 兜底：`owner_lease` + `SELECT FOR UPDATE` + `PRIMARY KEY(tenant_id, run_id, seq)` 三重保险。
- [ ] **事故 3：把 delta 当事件落库。** 现象：事件库一周涨到 TB 级，写入被打满。→ 根因：混淆「传输流」与「真相源」。→ 兜底：delta 只走 SSE；落库用聚合事件；事件白名单在代码里硬编码。
- [ ] **事故 4：schema 改字段导致账单归零。** 现象：某天所有 Run 成本显示 $0。→ 根因：把 `cost_usd` 改名成 `cost`，旧代码反序列化得零值。→ 兜底：只加不改 + 双写过渡 + 消费端 upcaster + 上线前跑「历史事件回放测试」。
- [ ] **事故 5：幂等键 args hash 不稳定。** 现象：同一个工具被调用两次，客户收到两封邮件。→ 根因：JSON key 顺序不同导致 hash 不同。→ 兜底：规范化后再 hash；对高危写操作再加「业务唯一键」二次校验（第 22 章）。
- [ ] **事故 6：终态被再次迁移。** 现象：已 `completed` 的 Run 又收到 `run.completed`，计费被结算两次。→ 根因：状态机没有防终态。→ 兜底：`Apply` 里显式拒绝终态迁移；计费结算用幂等键（第 27 章）。
- [ ] **事故 7：重放时重新调用模型。** 现象：「回放」出来的答案跟原来不一样，无法举证。→ 根因：`llm.responded` 只存了入参没存输出。→ 兜底：事件存完整输出 + 内容哈希；重放路径与执行路径在代码上物理隔离。

---

## 6.8 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 五层 | Agent（版本快照）→ Session → Run → Step → Event；Event 是唯一真相源 |
| 三种状态别混 | 「跑完了」「审过了」「算过账了」是三件事，`finalizing` 状态就是为了把它们分开 |
| 事件溯源 | 不是「记下一切」，是「记下所有改变状态的东西」 |
| 不该落库的 | token delta（传输层）；该落库的是聚合后的 `llm.responded` |
| 数字 | 42 事件/Run；9,072 万/日；13.6 GB/日；峰值 5,040 事件/秒 |
| 快照 | 每 20 事件一个，恢复 ≤5 ms；快照不是真相源 |
| 三条铁律 | 只加不改 / 未知字段保留 / 金额用整数（micro_usd） |
| 幂等三层 | HTTP `Idempotency-Key` / 事件 `(run_id, seq)` / 副作用 `(run_id, step, tool, args_hash)` |
| 最容易错 | args 未规范化导致 hash 不稳；重放时重新调模型 |
| 一句话 | **Run 的状态是「算出来的」，不是「存出来的」——存的是事件，状态是投影。** |

## 6.9 自检题

1. **LangGraph 的 checkpointer 已经能恢复执行了，为什么还要事件日志？**
   > 要点：两者服务的目标不同。checkpointer 回答「从哪继续跑」，事件日志回答「发生过什么」。四个 checkpointer 做不到的：① 审计举证（只留最新快照）；② 精确计费（缺每步 token 与成本明细）；③ 上下文考古（回答不了「第 3 步时上下文里有哪些段」）；④ 跨版本回放（快照结构随代码变）。正确做法是双写：事件为真相源，快照（可复用 checkpointer 的存储）只为加速恢复。

2. **为什么「重放」绝对不能重新调用模型？那怎么保证可复现？**
   > 要点：重放是把已记录的事实按序重新应用，不是重新执行。模型非确定性 + provider 版本滚动会导致同一输入不同输出，那「回放」就失去了举证价值。所以 `llm.responded` 必须存完整输出（大对象存 S3 引用）与内容哈希；重放代码路径与执行路径物理隔离（不同包、不同入口）。真正需要「重跑模型」的场景叫 **re-run**，是一个新 Run，必须带新的 run_id 与新的版本快照。

3. **事件表已经有 `(tenant_id, run_id, seq)` 主键做去重了，为什么还要 `idempotency` 表和工具侧的 `args_hash`？**
   > 要点：三者防的是不同层的东西。`(run_id, seq)` 防的是「同一事件的重复写入」（存储层）；`Idempotency-Key` 防的是「客户端重试导致的重复 Run」（API 层，跨 Run）；`args_hash` 防的是「同一 Run 内工具被重试导致重复副作用」（业务层，跨 Step）。少了第三个，客户会收到两封邮件——因为那是两个**不同的 seq**，存储层完全不认为是重复。

## 6.10 延伸阅读与交叉引用

- 上一章：[5. 为什么必须分离：职责边界与四种部署形态](5-client-server-protocol.md)
- 下一章：[7. 流式传输：SSE / WebSocket / gRPC / 长轮询](7-streaming-transport.md)
- 总纲：[0-summary.md](0-summary.md) §1「进程/线程 → Run/Step/Event」
- 长任务与持久化执行（Temporal 对照、Saga 补偿）：第 12 章
- 事件管道与 Kafka 用法：第 13 章；存储选型与分区设计：第 45 章
- 审计哈希链：第 31 章；计费账本：第 27 章
- Go 侧实现：见 [../go/46-multi-agent-durable.md](../go/46-multi-agent-durable.md)、[../go/45-memory-session.md](../go/45-memory-session.md)
