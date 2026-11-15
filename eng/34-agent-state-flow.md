# 34. Agent 场景的状态流转与扩展点

> **状态**：`已完成（初稿）` ｜ **所属**：第五部分 · 编排与状态机 ｜ **编号**：34 / 52
> **本章定位**：把第 30 章的落库套路、第 33 章的卡单/重跑/审计，原样落到 Agent Run 上。唯一的新东西是两个等待态（`WAITING_HUMAN`、`WAITING_EXTERNAL`）和「工具调用可能处于未知状态」这条恢复规则。
> **读法**：§34.2 的状态集合先背下来；§34.4.3 的「未知不是失败」是本章命门；§34.4.6 的扩展点清单可以直接当上线检查表。

---

## 34.1 事故现场：一次工具超时，退了 1,200 笔款

2025 年 7 月 8 日 14:20，某电商的售后 Agent 处理一批「物流延迟退款」。它的 Run 状态只有四个值：`RUNNING / DONE / FAILED / CANCELLED`。

事故链条：

1. Agent 调用 `create_refund` 工具，30 秒超时。**下游其实已经退款成功**，只是响应丢了。
2. Agent 把超时当失败，重试了 3 次；工具没有幂等键，**同一笔退了 4 次**。
3. 批量 300 笔，最终产生 **1,200 笔重复退款，86 万元**。
4. 更糟的是 HITL 审批：所有风险等级共用「30 分钟未处理自动通过」。当天有 **17 笔高风险退款**在无人审批的情况下自动放行。
5. 14:40 worker 重启，内存里的 plan 丢了；**1,900 个 Run 卡在 `RUNNING`**，既没有 `WAITING_EXTERNAL` 可查，也没有检查点可恢复。直到 20:30 才被客服发现。

复盘时三个问题没人答得上来：

```text
这笔 Run 现在到底在等什么？         → 状态只有 RUNNING，看不出来
那次 create_refund 到底成功没有？    → 没有 tool_call 记录，只能查下游
为什么高风险退款会被自动通过？       → 审批超时策略不分级
```

> 🩸 **Agent 的状态机不是「多几个枚举」**。它必须能回答三个传统订单状态机不用回答的问题：**在等谁、工具调用是否已产生副作用、模型说的「完成」算不算完成**。

---

## 34.2 Run 状态机：完整定义

### 状态集合

```text
S = { CREATED, QUEUED, PLANNING, ACTING, OBSERVING,
      WAITING_HUMAN, WAITING_EXTERNAL, VERIFYING,
      SUCCEEDED, FAILED, CANCELLED, EXPIRED }

终态：SUCCEEDED / FAILED / CANCELLED / EXPIRED（吸收，不可再迁移）
```

| 状态 | 含义 | 谁推进 | 传统订单有对应吗 |
| --- | --- | --- | --- |
| `CREATED` | Run 已创建，尚未入队 | API | 有（下单） |
| `QUEUED` | 等待 worker 认领 | 调度器 | 有（待支付） |
| `PLANNING` | 模型正在生成/更新计划 | worker | **没有**（模型专属） |
| `ACTING` | 正在执行一个工具调用/步骤 | worker | 部分有（调用下游） |
| `OBSERVING` | 正在处理工具结果、更新计划 | worker | 没有 |
| `WAITING_HUMAN` | 等待人工审批 | 人 | **没有**（传统审批是独立流程） |
| `WAITING_EXTERNAL` | 等待外部异步回调/长任务 | 外部系统 | **没有**（传统多用同步或 MQ） |
| `VERIFYING` | 代码校验产物/终态条件 | worker | 部分有（对账） |
| `SUCCEEDED` | 校验通过，终态 | 代码 | 有 |
| `FAILED` | 不可恢复失败/预算耗尽/超步数 | 代码 | 有 |
| `CANCELLED` | 用户/运营取消 | 人/API | 有 |
| `EXPIRED` | 超过 deadline，未完成 | 巡检 | 有（超时关单） |

> 💡 **两个新状态的意义**：`WAITING_HUMAN` 和 `WAITING_EXTERNAL` 把「等待」从「占用一个 worker/线程」变成「释放资源、持久化、等事件」。没有它们，等待就只能表现为 `ACTING` + 超时，最后变成卡单。

### 事件集合

```text
RUN_QUEUED, WORKER_CLAIMED, PLAN_READY,
TOOL_REQUESTED, TOOL_RETURNED, TOOL_FAILED, TOOL_UNKNOWN,
REPLAN_REQUIRED,
APPROVAL_REQUIRED, APPROVAL_GRANTED, APPROVAL_DENIED,
APPROVAL_EXPIRED, APPROVAL_REVOKED,
EXTERNAL_PENDING, EXTERNAL_CALLBACK,
VERIFY_PASSED, VERIFY_FAILED,
CANCEL_REQUESTED, DEADLINE_EXCEEDED, BUDGET_EXCEEDED, STEP_LIMIT_EXCEEDED
```

事件命名统一用**过去式**：`TOOL_RETURNED` 是「工具已经返回」，不是「请调用工具」。后者是命令，应该叫 `CALL_TOOL`，由编排器发出，不写进 Run 的事件表。

### 迁移表（核心 15 条）

| From | Event | Guard | Action | To |
| --- | --- | --- | --- | --- |
| CREATED | RUN_QUEUED | 租户配额足够 | 写队列消息 | QUEUED |
| QUEUED | WORKER_CLAIMED | 租约获取成功 | 写 lease/heartbeat | PLANNING |
| PLANNING | PLAN_READY | 计划非空、步数 ≤ max_steps | 落 `plan_json` + checkpoint | ACTING |
| ACTING | TOOL_REQUESTED | 工具在白名单、预算足够 | **先写 tool_call=PENDING**，再调用 | ACTING |
| ACTING | TOOL_RETURNED | 结果可解析 | 落结果，写 checkpoint | OBSERVING |
| ACTING | TOOL_FAILED | 可重试且 attempt < max | 退避重试 | ACTING |
| ACTING | TOOL_UNKNOWN | 超时/连接断 | 标记 UNKNOWN，转查询 | WAITING_EXTERNAL |
| OBSERVING | REPLAN_REQUIRED | 工具结果要求改计划 | 写 checkpoint，保留旧计划 | PLANNING |
| OBSERVING | APPROVAL_REQUIRED | 风险等级 ≥ 阈值 | 创建 approval，释放租约 | WAITING_HUMAN |
| OBSERVING | EXTERNAL_PENDING | 工具返回 async | 记 external_ref | WAITING_EXTERNAL |
| WAITING_HUMAN | APPROVAL_GRANTED | 审批未过期、args_hash 一致 | 落审批人，恢复执行 | ACTING |
| WAITING_EXTERNAL | EXTERNAL_CALLBACK | external_ref 匹配 | 落结果，写 checkpoint | OBSERVING |
| OBSERVING | VERIFY_REQUIRED | 计划已无下一步 | 进入代码校验 | VERIFYING |
| VERIFYING | VERIFY_PASSED | 校验通过 | 写终态、结算成本 | SUCCEEDED |
| VERIFYING | VERIFY_FAILED | 未超重规划上限 | 记录失败原因 | PLANNING |

任何非终态都可以接 `CANCEL_REQUESTED → CANCELLED`，但**要先处理「正在飞行的工具调用」**：不能取消 Run 却让工具继续产生副作用。正确顺序是：标记 `CANCEL_PENDING` → 查询/等待工具结果 → 写补偿任务 → `CANCELLED`。

### 三个反例

```text
反例 1：只有 RUNNING/DONE/FAILED
  → 无法区分「在等审批」「在等外部回调」「在跑工具」，卡单只能靠猜。

反例 2：WAITING_HUMAN 仍然占着 worker 线程
  → 100 个审批等 2 小时，worker 池被占满，其他 Run 全部排队。
  正确做法：持久化审批请求，释放租约，人来之前不占任何执行资源。

反例 3：模型说「已完成」就进 SUCCEEDED
  → 模型可能没调工具、调失败了、或产物不满足要求。
  正确做法：必须经过 VERIFYING，由代码检查工具返回值/产物/断言。
```

---

## 34.3 结构图：Run 状态机与检查点

```text
                    RUN_QUEUED              WORKER_CLAIMED
   [CREATED] ─────────────────▶ [QUEUED] ─────────────────▶ [PLANNING]
                                                                  │
                                                          PLAN_READY│
                                                                  ▼
        ┌──────────────────────────────────────────────────▶ [ACTING]
        │                                                      │    ▲
        │  APPROVAL_GRANTED                         TOOL_*    │    │
        │                                                      ▼    │
   [WAITING_HUMAN] ◀── APPROVAL_REQUIRED ── [OBSERVING] ◀──────────┘
        │                                      │    │
        │ APPROVAL_DENIED/EXPIRED              │    │ EXTERNAL_PENDING
        ▼                                      │    ▼
     [FAILED]                                  │ [WAITING_EXTERNAL]
                                               │    │ EXTERNAL_CALLBACK
                                               │    └──────────┐
                                               │ VERIFY_REQUIRED
                                               ▼               │
                                          [VERIFYING] ◀────────┘
                                               │
                              VERIFY_PASSED    │    VERIFY_FAILED（有界重规划）
                                               ▼
                                          [SUCCEEDED]

   每个箭头旁都写 checkpoint：seq / state / pending_tool_call / budget
   终态：SUCCEEDED / FAILED / CANCELLED / EXPIRED（吸收）
```

---

## 34.4 怎么实现：表、迁移、恢复、审批、检查点

### 34.4.1 Agent Run 表 DDL

```sql
CREATE TABLE agent_run (
  id               BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  run_id           CHAR(26)     NOT NULL,
  tenant_id        BIGINT       NOT NULL,
  session_id       CHAR(26)     NOT NULL,
  parent_run_id    CHAR(26)     NULL,          -- 子 Agent 场景
  status           VARCHAR(24)  NOT NULL,
  version          INT          NOT NULL DEFAULT 0,
  current_step_id  CHAR(26)     NULL,
  plan_json        JSON         NULL,
  checkpoint_json  JSON         NULL,
  waiting_reason   VARCHAR(64)  NULL,          -- APPROVAL/EXTERNAL_TOOL/...
  waiting_ref      VARCHAR(128) NULL,          -- approval_id / external_ref
  budget_tokens    BIGINT       NOT NULL,
  used_tokens      BIGINT       NOT NULL DEFAULT 0,
  cost_usd         DECIMAL(12,6) NOT NULL DEFAULT 0,
  max_steps        INT          NOT NULL DEFAULT 50,
  step_count       INT          NOT NULL DEFAULT 0,
  deadline_at      DATETIME(3)  NOT NULL,
  lease_owner      VARCHAR(64)  NULL,
  lease_until      DATETIME(3)  NULL,
  last_heartbeat_at DATETIME(3) NULL,
  last_step_at     DATETIME(3)  NULL,
  error_code       VARCHAR(64)  NULL,
  created_at       DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at       DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                                ON UPDATE CURRENT_TIMESTAMP(3),
  terminal_at      DATETIME(3)  NULL,
  UNIQUE KEY uk_run_id (run_id),
  KEY idx_status_updated (status, updated_at),
  KEY idx_tenant_created (tenant_id, created_at),
  KEY idx_parent (parent_run_id),
  CONSTRAINT chk_run_status CHECK (status IN (
    'CREATED','QUEUED','PLANNING','ACTING','OBSERVING',
    'WAITING_HUMAN','WAITING_EXTERNAL','VERIFYING',
    'SUCCEEDED','FAILED','CANCELLED','EXPIRED'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE agent_step (
  id              BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  run_id          CHAR(26)     NOT NULL,
  step_id         CHAR(26)     NOT NULL,
  seq             INT          NOT NULL,
  state           VARCHAR(24)  NOT NULL,
  tool_name       VARCHAR(64)  NULL,
  idempotency_key VARCHAR(160) NOT NULL,
  args_hash       CHAR(64)     NULL,
  result_json     JSON         NULL,
  error_code      VARCHAR(64)  NULL,
  started_at      DATETIME(3)  NULL,
  finished_at     DATETIME(3)  NULL,
  UNIQUE KEY uk_step (run_id, step_id),
  UNIQUE KEY uk_run_seq (run_id, seq),
  UNIQUE KEY uk_step_idem (idempotency_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE agent_tool_call (
  id              BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  run_id          CHAR(26)     NOT NULL,
  step_id         CHAR(26)     NOT NULL,
  tool_call_id    CHAR(26)     NOT NULL,
  tool_name       VARCHAR(64)  NOT NULL,
  args_json       JSON         NOT NULL,
  args_hash       CHAR(64)     NOT NULL,
  idempotency_key VARCHAR(160) NOT NULL,
  state           VARCHAR(24)  NOT NULL,   -- PENDING/SENT/SUCCEEDED/FAILED/UNKNOWN
  attempt         INT          NOT NULL DEFAULT 0,
  external_ref    VARCHAR(128) NULL,
  result_json     JSON         NULL,
  error_code      VARCHAR(64)  NULL,
  created_at      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                               ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_tool_call (tool_call_id),
  UNIQUE KEY uk_tool_idem (idempotency_key),
  KEY idx_run_state (run_id, state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

> 💡 **为什么 `status` 用 `VARCHAR` 不用 `ENUM`**：`ENUM` 加值要 `ALTER TABLE`，在千万级表上代价高；`VARCHAR + CHECK` 加值只需改约束（MySQL 8.0.16+ 支持 CHECK），且应用层状态机可以先灰度。以你的 MySQL 版本为准。

### 34.4.2 状态迁移代码（Java）

```java
public enum RunState {
    CREATED, QUEUED, PLANNING, ACTING, OBSERVING,
    WAITING_HUMAN, WAITING_EXTERNAL, VERIFYING,
    SUCCEEDED, FAILED, CANCELLED, EXPIRED
}

public record Transition(RunState from, String event, RunState to) {}

// 实际代码里加：import static com.example.agent.RunState.*;
public final class RunStateMachine {
    private static final Set<Transition> ALLOWED = Set.of(
        new Transition(CREATED, "RUN_QUEUED", QUEUED),
        new Transition(QUEUED, "WORKER_CLAIMED", PLANNING),
        new Transition(PLANNING, "PLAN_READY", ACTING),
        new Transition(ACTING, "TOOL_REQUESTED", ACTING),
        new Transition(ACTING, "TOOL_RETURNED", OBSERVING),
        new Transition(ACTING, "TOOL_UNKNOWN", WAITING_EXTERNAL),
        new Transition(OBSERVING, "REPLAN_REQUIRED", PLANNING),
        new Transition(OBSERVING, "APPROVAL_REQUIRED", WAITING_HUMAN),
        new Transition(OBSERVING, "EXTERNAL_PENDING", WAITING_EXTERNAL),
        new Transition(WAITING_HUMAN, "APPROVAL_GRANTED", ACTING),
        new Transition(WAITING_EXTERNAL, "EXTERNAL_CALLBACK", OBSERVING),
        new Transition(OBSERVING, "VERIFY_REQUIRED", VERIFYING),
        new Transition(VERIFYING, "VERIFY_PASSED", SUCCEEDED),
        new Transition(VERIFYING, "VERIFY_FAILED", PLANNING)
    );

    public static boolean allowed(RunState from, String event, RunState to) {
        return ALLOWED.contains(new Transition(from, event, to));
    }

    /** 条件更新：状态 + 版本 + 守卫一次完成；0 行要区分非法迁移与并发冲突。 */
    public Result transition(String runId, RunState from, RunState to,
                             String event, int expectVersion, String checkpointJson) {
        int n = jdbc.update("""
            UPDATE agent_run
               SET status=?, version=version+1, checkpoint_json=CAST(? AS JSON),
                   updated_at=NOW(3), last_step_at=NOW(3)
             WHERE run_id=? AND status=? AND version=?
            """, to.name(), checkpointJson, runId, from.name(), expectVersion);
        if (n == 1) {
            audit.append(runId, from, to, event, "svc:agent-runtime");
            return Result.OK;
        }
        RunState current = jdbc.queryForObject(
            "SELECT status FROM agent_run WHERE run_id=?", RunState.class, runId);
        return current == from ? Result.CONFLICT : Result.ILLEGAL;
    }
}
```

Go 侧的恢复入口与 Java 共用同一张迁移表：

```go
// Resume: crash recovery entry; same transitions as the Java state machine.
func (r *Runner) Resume(ctx context.Context, runID string) error {
	run, err := r.store.LoadForUpdate(ctx, runID)
	if err != nil {
		return err
	}
	if run.Status.Terminal() {
		return nil
	}
	switch run.Status {
	case StateActing:
		tc := run.Checkpoint.PendingToolCall
		if tc == nil {
			return r.transition(ctx, run, "TOOL_RETURNED", StateObserving)
		}
		res, err := r.tools.Query(ctx, tc.IdempotencyKey, tc.ExternalRef)
		switch {
		case err != nil:
			return r.parkExternal(ctx, run, tc)
		case res.Status == "SUCCEEDED":
			return r.recordAndObserve(ctx, run, res)
		case res.Status == "NOT_FOUND" && tc.State == "PENDING":
			return r.send(ctx, run, tc)
		default:
			return r.markUnknown(ctx, run, tc)
		}
	case StateWaitingHuman:
		return r.resumeAfterApproval(ctx, run)
	case StateWaitingExternal:
		return r.resumeAfterCallback(ctx, run)
	default:
		return r.continuePlan(ctx, run)
	}
}
```

**并发分析**：同一 Run 可能被「worker 心跳」「用户取消」「审批回调」三方同时推进。条件更新让只有一个赢；其余拿到 0 行，读当前状态后决定是重试（版本冲突）还是拒绝（非法迁移）。**不要用分布式锁**：锁的持有时间覆盖模型调用会直接拖垮并发。

### 34.4.3 多步工具调用：悬挂与恢复

#### 检查点数据结构

```json
{
  "run_id": "run_01J8Z3K4Q5",
  "seq": 17,
  "state": "ACTING",
  "plan": {
    "next_step": 3,
    "steps": [
      {"step_id": "st_01", "tool": "query_order", "status": "SUCCEEDED"},
      {"step_id": "st_02", "tool": "check_policy", "status": "SUCCEEDED"},
      {"step_id": "st_03", "tool": "create_refund", "status": "PENDING"}
    ]
  },
  "messages_digest": "sha256:9f2c...",
  "pending_tool_call": {
    "tool_call_id": "tc_01J8Z3M7",
    "tool_name": "create_refund",
    "args_hash": "sha256:7a1b...",
    "idempotency_key": "run_01J8Z3K4Q5:st_03:create_refund",
    "attempt": 1,
    "external_ref": "refund_88213",
    "state": "UNKNOWN"
  },
  "budget": {"tokens_used": 12000, "cost_usd": 0.42},
  "created_at": "2025-07-08T14:20:03.412+08:00"
}
```

#### 工具调用的五个状态

| 状态 | 含义 | 恢复动作 |
| --- | --- | --- |
| `PENDING` | 已写意图，尚未发出 | 可以直接重发（还没产生副作用） |
| `SENT` | 已发出，等响应 | 查下游；查不到就转 `UNKNOWN` |
| `SUCCEEDED` | 下游确认成功 | 落结果，转 `OBSERVING` |
| `FAILED` | 下游明确失败 | 按错误类型重试或失败 |
| `UNKNOWN` | 超时/断连，**不知道成功没有** | **先查询，绝不盲目重试** |

> 🩸 **`UNKNOWN` 是 Agent 状态机里最重要的一个状态**。传统 RPC 超时你可以重试，因为下游多半幂等；Agent 的工具调用经常不幂等（发邮件、退款、删文件），超时后重试就是重复副作用。正确做法：**调用前写 `PENDING` + 幂等键，超时后标 `UNKNOWN`，恢复时先按幂等键/外部引用查询下游，确认没成功才重发。**

#### 恢复伪代码

```text
func resume(run_id):
    run = load_run_for_update(run_id)          # 行锁，防两个 worker 同时恢复
    if run.status in TERMINAL: return          # 终态不恢复
    cp = run.checkpoint_json
    switch run.status:
      case QUEUED:
          claim_and_plan(run)
      case PLANNING:
          # 计划可能已生成但没落库；从 messages_digest 重新规划
          plan = model.plan(run.goal, run.context)   # 记录本次模型输出
          save_plan_and_goto_acting(run, plan)
      case ACTING:
          tc = cp.pending_tool_call
          if tc == null: goto OBSERVING
          switch query_tool(tc.idempotency_key, tc.external_ref):
            case SUCCEEDED: record_result(tc); goto OBSERVING
            case FAILED:    retry_or_fail(tc)
            case NOT_FOUND: if tc.state == PENDING: send(tc) else mark_unknown(tc)
            case UNKNOWN:   park_as_waiting_external(run, tc)  # 不重试
      case WAITING_HUMAN:
          ap = load_approval(run.waiting_ref)
          if ap.state == APPROVED and not expired(ap) and ap.args_hash == cp.args_hash:
              goto ACTING
          if ap.state in (DENIED, EXPIRED, REVOKED): goto FAILED
      case WAITING_EXTERNAL:
          r = query_external(run.waiting_ref)
          if r.done: record_result(r); goto OBSERVING
          if now > run.deadline_at: goto EXPIRED
      case OBSERVING:
          if needs_replan(run): goto PLANNING
          if plan_has_next_step(run): goto ACTING
          else: goto VERIFYING
      case VERIFYING:
          if verify(run): goto SUCCEEDED else goto PLANNING
```

三条纪律：

1. **先写意图，再调用**。`PENDING` 必须在 HTTP 请求之前提交；否则进程崩在「已发出、未记录」的窗口，恢复时完全不知道发生过什么。
2. **`UNKNOWN` 不自动重试**。要么查询下游，要么转人工；自动重试只允许在 `PENDING`（确定没发出）或下游明确 `FAILED` 且错误可重试时。
3. **恢复也要抢锁**。两个 worker 同时恢复同一个 Run，会各自发一次工具调用；用 `SELECT ... FOR UPDATE` 或条件更新 `WHERE status=? AND version=?` 抢所有权。

### 34.4.4 HITL 审批：中断点设计

审批不是「弹个框」，而是**把 Run 切开、持久化、释放资源、等事件、再无损接回去**。

```sql
CREATE TABLE agent_approval (
  id            BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  approval_id   CHAR(26)     NOT NULL,
  run_id        CHAR(26)     NOT NULL,
  step_id       CHAR(26)     NOT NULL,
  tool_call_id  CHAR(26)     NOT NULL,
  risk_level    VARCHAR(8)   NOT NULL,      -- LOW/MEDIUM/HIGH/CRITICAL
  args_hash     CHAR(64)     NOT NULL,      -- 审批绑定具体参数，换参数即失效
  state         VARCHAR(16)  NOT NULL,      -- PENDING/APPROVED/DENIED/EXPIRED/REVOKED
  requested_at  DATETIME(3)  NOT NULL,
  expires_at    DATETIME(3)  NOT NULL,
  decided_at    DATETIME(3)  NULL,
  decided_by    VARCHAR(64)  NULL,
  reason        VARCHAR(512) NULL,
  UNIQUE KEY uk_approval (approval_id),
  KEY idx_run_state (run_id, state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

#### 超时策略：按风险分级，绝不「一刀切自动通过」

| 风险等级 | 例子 | 超时 | 超时后默认 |
| --- | --- | --- | --- |
| LOW | 只读查询、生成草稿 | 30 分钟 | 自动通过（仅限无副作用） |
| MEDIUM | 发通知、改非关键字段 | 2 小时 | 升级给主管 |
| HIGH | 退款、发券、改地址 | 15 分钟 | **拒绝/过期**，转人工重提 |
| CRITICAL | 转账、删文件、对外合同 | 不允许超时自动处理 | 只能显式批准；超时 = 失败 |

> ⚠️ **「30 分钟自动通过」只适用于无副作用的低风险操作**。事故现场的 17 笔高风险退款，就是被这条策略放行的。

策略本身也要落成配置，不能散落在代码里：

```yaml
# agent-policy.yaml
run:
  max_steps: 50
  budget_tokens: 200000
  deadline: 2h
tools:
  query_order:
    risk: LOW
    timeout: 5s
    approval: none
  create_refund:
    risk: HIGH
    timeout: 30s
    idempotency: required
    approval: required
approval:
  LOW:      {timeout: 30m, on_timeout: approve}
  MEDIUM:   {timeout: 2h,  on_timeout: escalate}
  HIGH:     {timeout: 15m, on_timeout: deny}
  CRITICAL: {timeout: 0,   on_timeout: deny}
```

#### 撤回与代理审批

- **撤回**：`APPROVED` 不是永久有效。审批绑定 `tool_call_id + args_hash + expires_at`；执行前重新校验三者，任一不匹配就回到 `PENDING` 或 `FAILED`。
- **代理审批**：委托人可以指定代理人，但必须限制 `scope`（哪些工具/金额上限）、`expires_at`、`risk_level ≤ MEDIUM`；CRITICAL 不允许代理。代理关系与每次决策都要写审计。
- **四眼原则**：HIGH/CRITICAL 要求 `requester != approver`；金额超过阈值要求两个审批人。

审批状态机：

```text
PENDING ──APPROVE──▶ APPROVED ──EXECUTE(校验 args_hash/expiry)──▶ 工具调用
   │                    │
   │ DENY               │ REVOKE（执行前）
   ▼                    ▼
DENIED              REVOKED
   │
   │ TIMEOUT
   ▼
EXPIRED（按风险等级决定：失败/升级/仅 LOW 自动通过）
```

### 34.4.5 长任务的检查点与幂等重放

| 检查点时机 | 写什么 | 为什么 | 成本 |
| --- | --- | --- | --- |
| 每次状态迁移 | `status/version/checkpoint_json` | 恢复的基准 | 1 行更新 |
| 工具调用前 | `tool_call=PENDING` + 幂等键 + args_hash | 防「发出去了但没记录」 | 1 行插入 |
| 工具调用后 | 结果引用/错误码 | 防重复调用 | 1 行更新 |
| 模型调用后 | 模型输出/用量/成本 | 重放不重调模型 | 1 行插入 |
| 每 N 步 | 计划快照/消息摘要 | 防上下文丢失 | 可控 |

**重放规则**：

1. **模型调用不重放**。模型输出已经落 `model_call` 表，重放时读结果，不重新调用；否则同输入可能不同输出，还会重复花钱。
2. **工具调用按幂等键重放**。先查 `agent_tool_call` 和下游，确认副作用是否已发生；`UNKNOWN` 只查询不重发。
3. **校验器必须确定性**。`VERIFYING` 里的代码不能依赖当前时间/随机数；否则同一 Run 两次校验结果不同。
4. **检查点只存引用，不存大对象**。`checkpoint_json` 控制在 64KB 以内；大产物（文件、长文本）放对象存储，检查点存 `artifact_ref`。

> 💡 **一句话**：**Agent 的「重放」不是「再跑一遍」，而是「从检查点继续，模型读缓存、工具查幂等、校验跑确定性代码」。**

### 34.4.6 扩展点清单：新增状态/事件/补偿/工具

| 扩展 | 改哪里 | 必须同步改什么 | 漏了会怎样 |
| --- | --- | --- | --- |
| **新增状态** | 枚举/`CHECK` 约束、迁移表、状态目录 | SLA、owner、UI、指标、测试矩阵、恢复 switch、文档 | 非法迁移、卡单无人认领、UI 显示空白 |
| **新增事件** | 事件 schema、生产者、消费者 | 版本号、幂等键、Outbox、重放兼容、审计 | 重复消费、旧实例不识别、事件丢失 |
| **新增补偿** | 补偿动作注册表、回滚计划 | 幂等键、审批策略、审计字段、对账规则 | 补偿重复执行、无法回滚、账不平 |
| **新增工具** | 工具 schema、白名单、风险等级 | 超时、幂等键、审批策略、成本预算、沙箱、审计 | 越权调用、重复副作用、成本失控 |
| **新增等待态** | 状态机 + 唤醒机制 | 超时策略、巡检、通知、资源释放 | 永远等不到、worker 被占满 |

新增状态的迁移 SQL（示例）：

```sql
-- 1) 先加状态目录（SLA/owner 必须一起填）
INSERT INTO state_catalog
  (process_key, state, is_terminal, sla_seconds, severity, owner, enabled)
VALUES
  ('agent_run', 'WAITING_EXTERNAL', 0, 1800, 'P2', 'agent-runtime', 1);

-- 2) 再改 CHECK 约束（MySQL 8.0.16+）
ALTER TABLE agent_run DROP CHECK chk_run_status;
ALTER TABLE agent_run ADD CONSTRAINT chk_run_status CHECK (status IN (
  'CREATED','QUEUED','PLANNING','ACTING','OBSERVING',
  'WAITING_HUMAN','WAITING_EXTERNAL','VERIFYING',
  'SUCCEEDED','FAILED','CANCELLED','EXPIRED'));

-- 3) 上线前确认：没有实例停在「新状态但旧代码不认识」的组合
SELECT status, COUNT(*) FROM agent_run GROUP BY status;
```

### 34.4.7 与 llm 系列的衔接（只给链接与一句结论）

> 按本仓库实际编号：多 Agent 在 `llm/33`，HITL 在 `llm/34`；没有单独的 `llm/34-multi-agent.md`。

| 文档 | 一句话结论 | 本章只用到 |
| --- | --- | --- |
| [../llm/6-session-run-event-model.md](../llm/6-session-run-event-model.md) | Session/Run/Step/Event 是 Agent 平台的五层对象模型，事件溯源是唯一正确姿势 | Run/Step 的字段与状态命名 |
| [../llm/12-long-task-durable.md](../llm/12-long-task-durable.md) | 长任务要先分档，再决定用检查点还是工作流引擎 | 检查点频率与恢复策略 |
| [../llm/33-multi-agent-collaboration.md](../llm/33-multi-agent-collaboration.md) | 多 Agent 的拓扑、总线与预算，先算账再上 | `parent_run_id` 与子 Run 归属 |
| [../llm/34-hitl-approval.md](../llm/34-hitl-approval.md) | HITL 的本质是把长事务切开、持久化、再无损接回去 | 审批中断点与超时分级 |

> 这四篇讲「为什么」和「平台怎么设计」，本章只讲「状态表怎么落、代码怎么写」。不重复展开。

---

## 34.5 双修对照：订单状态机 vs Agent Run 状态机

| 维度 | 传统订单 | Agent Run | 相同 / 差异根因 |
| --- | --- | --- | --- |
| **身份** | `order_no` | `run_id` + `step_id` + `tool_call_id` | 结构相同，粒度更细 |
| **状态数** | 7~9 个 | 12 个（多两个等待态 + VERIFYING） | 差异根因：长事务 + 非确定性 |
| **等待** | 等支付回调（通常是 MQ） | 等人审批、等外部工具回调 | Agent 的等待是**一等状态** |
| **守卫** | 金额、库存、状态 | 预算、步数、工具白名单、风险等级 | 机制相同，维度扩展 |
| **终态判定** | 支付/账务返回值 | **代码校验**，不信模型自述 | 差异根因：模型会说「我完成了」 |
| **失败模式** | 重复支付、超卖 | 重复工具调用、死循环、假活跃 | Agent 多两类 |
| **恢复** | 重跑/补偿 | checkpoint + 模型缓存 + 工具幂等查询 | 机制相同，Agent 不能重放模型 |
| **审计** | 谁改了订单 | 谁批了工具、谁终止了 Run | 同一张审计表 |
| **成本** | 机器折旧 | token + 工具费，按 Run 计 | 差异根因：成本显性 |
| **观测** | 状态年龄、非法迁移 | 加 token、成本、工具成功率、无进度时长 | 指标集扩展 |

**哪些部分完全相同，可以直接复用**：

1. **条件更新 + 版本号**：`UPDATE ... WHERE run_id=? AND status=? AND version=?`。
2. **迁移日志**：`from/to/event/actor/reason/trace_id`，只是 actor 变成 `agent:planner`/`human:approver`。
3. **卡单巡检**：`state_catalog` + 状态年龄 SQL（第 33 章），加一个「无进度」条件。
4. **重跑/审计/审批**：作业表、dry-run、计划哈希、四眼原则，全部复用。

**三定律检验**：

- **状态机定律**：Run 的真相源是 `agent_run` 表，不是模型上下文；上下文丢了可以从检查点重建，表丢了就真丢了。
- **稀缺资源定律**：Agent 限的是 token/TPM/单 Run 预算/工具配额，不是线程数；`budget_tokens` 和 `cost_usd` 是守卫，不是报表字段。
- **不可回滚定律**：每次工具调用都是外部副作用；`UNKNOWN` 状态 + 幂等键 + 查询恢复，是这条定律在 Agent 上的具体实现。

---

## 34.6 会在哪炸：失效模式清单

| # | 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- | --- |
| 1 | Run 卡在 ACTING | worker 崩了，plan 只在内存 | 查 `last_step_at`/checkpoint | 从检查点恢复 | 每个 step 边界写检查点 |
| 2 | 重复退款/发邮件 | 工具超时被当失败，直接重试 | 查 `agent_tool_call` 的 attempt/幂等键 | 标 `UNKNOWN`，先查询 | 先写 PENDING + 幂等键 |
| 3 | 高风险操作被自动通过 | 审批超时策略不分级 | 查 `agent_approval` 的 risk/decided_by | 改分级策略 + 补审计 | HIGH 超时拒绝 |
| 4 | 模型说完成但实际没做 | 没有 VERIFYING | 查终态前的工具结果/产物 | 加代码校验 | 终态必须过校验器 |
| 5 | 重放又调了一次模型 | 模型调用写在 workflow/恢复逻辑里 | 查 `model_call` 次数 vs step 数 | 模型输出落库，重放读缓存 | 模型调用只走 activity |
| 6 | 检查点膨胀到几 MB | 把大产物塞进 `checkpoint_json` | 查 JSON 长度 | 改存 `artifact_ref` | 64KB 上限告警 |
| 7 | 取消后工具还在跑 | 取消只改状态，没处理飞行中的调用 | 查终态后的 tool_call | 补偿/等待结果 | `CANCEL_PENDING` 中间态 |
| 8 | 外部回调到了，Run 已终态 | 回调与取消竞争 | 查 `external_ref` 与终态时间 | 幂等丢弃/补偿 | 终态吸收 + 回调去重 |

---

## 34.7 怎么验证：并发、杀进程、重复回调、重放

| 验证项 | 方法 | 通过标准 |
| --- | --- | --- |
| **并发迁移** | 同一 Run 200 并发 `TOOL_RETURNED` | 只有 1 次成功，version 只 +1 |
| **杀进程恢复** | 在 PLANNING/ACTING/WAITING_* 各 kill worker | 30s 内被接管，副作用不重复 |
| **重复回调** | 同一 `external_ref` 回调 3 次 | 只处理 1 次，其余幂等丢弃 |
| **审批超时** | 构造 HIGH 审批超时 | 转 `EXPIRED`/`FAILED`，绝不自动通过 |
| **重放一致** | 同一 Run 重放 100 次 | 终态一致，模型调用次数不增加 |
| **成本对账** | `SUM(model_call.cost)` vs `agent_run.cost_usd` | 差异 < 1% |

卡单 SQL（加「无进度」条件）：

```sql
SELECT run_id, status, tenant_id,
       TIMESTAMPDIFF(SECOND, last_step_at, NOW()) AS no_progress_seconds,
       used_tokens, cost_usd
FROM agent_run
WHERE status IN ('PLANNING','ACTING','OBSERVING')
  AND last_step_at < NOW() - INTERVAL 10 MINUTE
ORDER BY no_progress_seconds DESC
LIMIT 500;
```

> ⚠️ **不要只看心跳**：模型思考时心跳正常，但可能 20 分钟没推进一个 step。`last_step_at` 才是「有没有进度」的真相；心跳只证明进程活着。

---

## 34.8 用什么工具：Agent 运行时的三种落法

| 方案 | 给你什么 | 不给你什么 | 适合 |
| --- | --- | --- | --- |
| **Temporal + 自研 Run 表** | 持久化执行、定时器、信号、重试 | 业务状态投影、审批 UI、token 预算 | 生产级长任务 |
| **LangGraph checkpointer/interrupt** | 图编排、检查点、人工中断（原型友好） | 多租户运维、跨集群、统一审计 | 原型/单团队 |
| **自研 Run/Step 表 + 队列** | 完全可控、贴合业务、成本可见 | 重放/定时器要自己写 | 4~10 步、团队小 |
| **纯 prompt 里写流程** | 上手最快 | 状态、恢复、审计、预算全没有 | 不要用于生产 |

> 💡 **结论**：Agent 运行时 = **状态机（第 30 章）+ 检查点（本章）+ 审批（HITL）+ 预算（第 18 章成本熔断）**。引擎可以换，这四样不能少。

---

## 34.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 状态集 | 12 个状态，终态 4 个；多 `WAITING_HUMAN`/`WAITING_EXTERNAL`/`VERIFYING` |
| 等待 | 等待是状态，不是占着线程；持久化后释放租约 |
| 工具调用 | 先写 `PENDING` 再调用；超时标 `UNKNOWN`，**先查询不重试** |
| 恢复 | 从检查点继续；模型读缓存、工具查幂等、校验跑确定性代码 |
| 审批 | 按风险分级超时；HIGH 超时拒绝；CRITICAL 不自动 |
| 审批绑定 | `tool_call_id + args_hash + expires_at`，换参数即失效 |
| 终态 | 必须过 VERIFYING，不信模型自述 |
| 检查点 | 每步边界 + 工具前后；只存引用，64KB 上限 |
| 扩展 | 新状态/事件/补偿/工具都要同步改 SLA、审计、测试、UI |
| 双修 | 落库/巡检/重跑/审计完全复用第 30、33 章；Agent 多等待态、非确定性、成本 |
| 验证 | 200 并发、kill worker、重复回调、审批超时、重放 100 次、成本对账 |

## 34.10 自检题

1. **为什么 Agent Run 需要 `WAITING_HUMAN` 和 `WAITING_EXTERNAL`，而订单状态机通常不需要？**
   > 要点：Agent 的流程是分钟到小时级，经常等人审批、等外部异步工具回调；如果都用 `ACTING` 表示，就会占着 worker/线程、无法区分「在等谁」、超时后只能算失败。两个等待态把「等待」变成持久化状态：释放执行资源、记录 `waiting_ref`、由事件唤醒，才能支撑长事务和卡单巡检。订单流程的等待通常由 MQ/回调表处理，不需要占用订单状态机。

2. **工具调用超时后，为什么不能直接重试？应该怎么恢复？**
   > 要点：超时只说明「没收到响应」，不代表下游没执行；退款/发邮件/删文件都不幂等，直接重试会重复副作用。正确做法：调用前写 `PENDING` + 幂等键 + `args_hash`；超时标 `UNKNOWN`；恢复时按幂等键/`external_ref` 查询下游，确认成功就落结果，确认没成功才重发，查不到就转人工。自动重试只允许在确定没发出的 `PENDING` 或明确可重试的 `FAILED`。

3. **审批已经通过了，执行前还需要校验什么？为什么？**
   > 要点：要校验 `tool_call_id`、`args_hash`、`expires_at`，以及审批状态是否被撤回。审批绑定的是「当时那组参数」；如果模型在等待期间改了参数（金额、收款人、文件路径），旧审批不能复用。过期审批也不能执行。高风险操作还要四眼原则和二次确认。

## 34.11 延伸阅读

- 上一章：[33. 卡单、重跑、跳过与人工干预](33-stuck-order-and-rerun.md)
- 下一章：[35. 数据模型决定存储：八类存储的本质](35-storage-model-selection.md)
- 相关：[29. 编排是什么](29-orchestration-vs-choreography.md)、[30. 状态机建模与落库](30-state-machine-modeling.md)、[31. DAG 编排](31-dag-orchestration.md)、[32. 编排引擎选型](32-workflow-engine-selection.md)
- 平台侧：[../llm/6-session-run-event-model.md](../llm/6-session-run-event-model.md)（Run/Step/Event 模型）、[../llm/12-long-task-durable.md](../llm/12-long-task-durable.md)（长任务与检查点）、[../llm/33-multi-agent-collaboration.md](../llm/33-multi-agent-collaboration.md)（多 Agent）、[../llm/34-hitl-approval.md](../llm/34-hitl-approval.md)（HITL）
- 幂等：[2. 幂等的实现光谱](2-idempotency-implementations.md)；补偿：[13. Saga 三连坑](13-saga-pitfalls.md)；对账：[14. 对账体系](14-reconciliation-system.md)
- 官方文档（以版本为准）：Temporal 的 Activity 超时/重试与 Workflow 确定性；LangGraph 的 checkpointer/interrupt；OpenTelemetry 的 span attributes 约定
- 总纲：[0-summary.md](0-summary.md)
