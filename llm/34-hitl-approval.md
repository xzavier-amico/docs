# 34. 人机协同（HITL）：审批、中断与恢复

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 运行时、安全、合规与防御 ｜ **编号**：34 / 52
> **本章定位**：HITL 看起来是「弹个框让人点同意」，实际上是**把一次长事务切开、持久化、再无损接回去**——这是 Agent 平台里最难做对的一致性工程之一。本章只关心三件事：谁必须批、挂起期间资源怎么放、恢复时怎么不重放副作用。
> **读法**：§34.2 的分级表可以照着抄；§34.5 的恢复一致性是本章命门，代码要逐行看。

---

## 34.1 引子：批了一次，退了两次

Aurora 的客服 Agent 有一个退款工具，高于 ¥500 需要主管审批。设计得很规矩：模型发起 `refund.create`，Run 挂起，飞书推一张卡片给主管，主管点「同意」，Run 恢复执行。

上线第 6 周，财务对账发现一笔 ¥1,280 的退款**退了两遍**。复盘时间线：

```
10:02:11  模型调用 refund.create(amount=1280, order=SO-7781)
10:02:11  Run 挂起，写 approval_request(id=ap-91f3, status=pending)
10:02:14  飞书卡片推送成功
10:04:30  主管点「同意」→ 回调 POST /approvals/ap-91f3/decision
10:04:30  服务端：UPDATE approval SET status='approved'   ✅
10:04:30  ★ 此时该实例正好被滚动更新（K8s 摘流中），恢复请求打到已销毁的 Pod
10:04:31  客户端 3 秒未见响应 → 网关层重试（幂等键没覆盖审批回传）
10:04:31  重试打到新 Pod → 新 Pod 看到 status='approved' 且「未执行」
          → 再次执行 refund.create  ✅ 第二次退款
10:04:31  第一次执行的结果在旧 Pod 的日志里，事件流里没有
```

三个 bug，每一个单独看都「不算大」：

1. **审批决策的消费不是原子的。** `SELECT status` 与 `UPDATE` 之间的窗口，加上网关重试，构成双重执行。
2. **执行结果没有先进事件流。** 旧 Pod 已经调了退款接口，但 `tool_result` 事件没落库，新 Pod 认为「还没执行」。
3. **恢复时没有用幂等键。** 退款接口本身支持 `Idempotency-Key`，但 Agent 侧没传——因为「同一步骤只有一次」这个假设在上面的场景里破了。

> 🎯 **本章要建立的第一个共识**：
> **审批不是「一个 UI 交互」，是「一次带外部副作用的分布式事务的两阶段提交」。** 你必须像对待支付一样对待它：决策只能消费一次、执行必须幂等、结果必须先进事件流再回传。

---

## 34.2 哪些动作必须审批：五级动作模型

审批的第一版设计通常是「凡是写操作都审批」。结果：审批量爆炸，主管 10 分钟点 60 次同意，全部点「通过」——**审批疲劳让审批变成橡皮章，比不审批更危险**（因为你以为有人在看）。

正确做法是**按「副作用不可逆程度 × 金额/影响面」分级**：

| 级别 | 定义 | 例子 | 审批要求 | 超时默认 | 审计粒度 |
| --- | --- | --- | --- | --- | --- |
| **R0 只读** | 不改外部状态 | 查订单、查物流、检索知识库 | 无 | — | 采样（1%） |
| **R1 幂等写** | 写但可重复执行、结果一致 | 更新会话备注、打标签、写草稿 | **无审批**，事后审计 | — | 全量 |
| **R2 写（有副作用）** | 改外部状态，重复执行有影响 | 改收货地址、改工单状态、发站内消息 | **单人或自动规则审批** | 自动拒绝 | 全量 + 参数快照 |
| **R3 高危写** | 涉钱、涉外发、涉外部系统 | 退款 > ¥500、发邮件/短信给客户、调第三方下单、改权限 | **必须人工审批**（按金额可要求双人） | 自动拒绝 | 全量 + 上下文快照 + 审批人 |
| **R4 不可逆/超阈值** | 无法撤回或影响面过大 | 批量退款、删除数据、对外发布、转账 | **双人审批 + 二次确认 + 冷静期** | 自动拒绝 + 告警 | 全量 + 双人 + 冷静期记录 |

三条补充判据，用来处理「表里没有」的情况：

1. **能撤回吗？** 不能撤回的一律 ≥R3。
2. **出圈了吗？** 数据/消息离开你的系统（外发）≥R3，因为出去了就收不回（第 37 章的「撤回」在这里几乎无效）。
3. **能量化损失吗？** 单次最大损失 > 该租户日预算的 5% → ≥R3。

> 💡 **一个被忽略的设计**：**R2 也可以不弹框，用「延迟执行 + 可撤回窗口」替代审批。** 发一条站内消息，先等 30 秒，期间用户/客服可以撤销——这比让人点同意体验好得多，成本也低。**审批应该只留给「不能撤回」的动作。** 详见第 37 章的撤回机制。

### 34.2.1 谁有权批？审批人解析

审批人不是「固定的主管」，要能按上下文解析：

| 决策因素 | 解析规则 | 兜底 |
| --- | --- | --- |
| 租户配置 | 租户可自定义审批人组（`approver_group`） | 平台默认组 |
| 金额档位 | ¥500~2000 组长；>2000 部门经理；>2 万财务 | 无人在线 → 升级到上一级 |
| 业务域 | 退款→客服主管；发合同→法务；代码执行→平台 oncall | 域 owner 离线 → 值班人 |
| 时段 | 非工作时间只允许 R3 以下；R4 一律等到工作时间（冷静期自然满足） | 紧急通道需两人 |

---

## 34.3 结构图：打断 → 挂起 → 通知 → 回传 → 恢复

```
 Agent 执行线程                 审批服务                     通知通道            审批人
 ─────────────                 ────────                     ────────           ──────
 ① 模型决定调 refund.create
    （R3，amount=1280）
        │
 ② 检查策略 → 需要审批
        │
 ③ 写 approval_request(pending)
    并写 event: approval.requested
        │
 ④ ★ checkpoint 到 step 边界
    然后**销毁执行上下文**
    （释放 goroutine / 连接 / 模型配额）
        │
        ├──▶ ⑤ 入队 + 推卡片 ─────────────▶ 飞书/邮件/App ──▶ ⑥ 主管看到
        │                                                       │
        │                                          ⑦ 点「同意」并附备注
        │                                                       │
        │◀── ⑧ 回调（带 decision_token，一次性）◀────────────────┘
        │
 ⑨ CAS 消费决策：UPDATE ... WHERE status='pending'
        │
 ⑩ 决策写入事件流（event: approval.decided）
        │
 ⑪ 重新调度：加载 checkpoint → 注入决策 → 从「审批后的下一步」继续
        │
 ⑫ 执行 refund.create(idempotency_key=hash(run,step,tool,args))
        │
 ⑬ tool_result 先落事件流，再回传前端
```

第 4 步是整个设计的核心，也是最容易被写错的一步：

```go
// ❌ 大多数人第一版都这么写：卡住一个 goroutine 等审批
if needApproval {
    decision := <-approvalChan   // 等 4 分钟
    // 这 4 分钟里：goroutine 占着、DB 连接占着、模型配额占着、HTTP 连接占着
}

// ✅ 正确做法：持久化 + 释放 + 事件驱动恢复
if needApproval {
    cp := checkpoint(run)                      // ① 落 checkpoint
    writeEvent(run, Event{Type: "approval.requested", ReqID: reqID})
    enqueueApproval(req)                       // ② 进审批队列
    return ErrSuspended                        // ③ 返回，释放所有资源
}
```

算一下为什么必须这样：Aurora 峰值 5k QPS，其中 **8% 的 Run 会触发审批**（约 400 QPS），审批平均耗时 4 分 20 秒（260 秒）。如果挂起时占住资源：

```
并发挂起的 Run = 400 × 260 = 104,000 个
每个挂起占：1 goroutine(8KB) + 1 DB 连接 + 1 HTTP 连接 + 1 fd
→ 内存 850MB（还行）+ 10.4 万个 DB 连接（直接打死数据库）+ 10.4 万个 fd
```

**结论：挂起必须「释放一切」，恢复必须「从 checkpoint 重建」。** 这不是优化，是能不能上线的问题。

---

## 34.4 状态机与数据模型

### 34.4.1 状态机

```
                         ┌──────────────┐
     创建 ──────────────▶│   PENDING    │
                         └──┬───┬───┬───┘
                            │   │   │
              ┌─────────────┘   │   └──────────────┐
              │                 │                  │
     决策=approve        决策=reject          超时/取消
              │                 │                  │
     ┌────────▼───────┐  ┌──────▼──────┐   ┌───────▼───────┐
     │   APPROVED     │  │  REJECTED   │   │   EXPIRED     │
     │ (一次性消费)    │  │ (终态)      │   │ (终态)        │
     └────────┬───────┘  └─────────────┘   └───────────────┘
              │  CAS: status='approved' AND executed_at IS NULL
     ┌────────▼───────┐
     │   EXECUTING    │──── 失败 ──▶ ┌──────────────┐
     └────────┬───────┘              │ APPLY_FAILED │（需人工介入，不自动重试）
              │ 成功                  └──────────────┘
     ┌────────▼───────┐
     │    APPLIED     │（终态）
     └────────────────┘

     任何非终态 ──取消──▶ CANCELLED（用户撤回请求 / Run 被取消）
     任何 PENDING ──升级──▶ ESCALATED（换审批人，状态仍为 pending）
```

| 状态 | 谁能进入 | 可停留多久 | 恢复时做什么 |
| --- | --- | --- | --- |
| `PENDING` | 创建时 | 到 `expires_at` | 只等，不执行 |
| `APPROVED` | 决策=同意 | **必须立即消费**（有 TTL，如 5 分钟） | CAS 后执行 |
| `EXECUTING` | 消费决策后 | 到工具超时 | **不能重新进入**，只能查结果 |
| `APPLIED` | 工具成功 | 永久 | 直接读结果，不重跑 |
| `APPLY_FAILED` | 工具失败 | 等人工 | **绝不自动重试**（可能已经执行了） |
| `REJECTED/EXPIRED/CANCELLED` | 终态 | 永久 | 注入「被拒绝」的 tool_result，让模型继续走拒绝分支 |

### 34.4.2 审批请求的 schema：审批的对象必须是「具体动作」

```json
{
  "approval_id": "ap-91f3c4",
  "run_id": "r-8f2c1a",
  "step_id": "s-14",
  "tenant_id": 10231,
  "agent_id": "cs-supervisor",
  "level": "R3",
  "tool": "refund.create",
  "args": { "order_id": "SO-7781", "amount_cny": 1280, "reason": "商品破损" },
  "args_hash": "sha256:5c1e...",          // ★ 审批的就是这个哈希，执行时必须一致
  "preview": "为订单 SO-7781 退款 ¥1,280（商品破损），退款至原支付渠道",
  "context_snapshot": {
    "model": "m-large-2026-01",
    "prompt_version": "cs-v37",
    "retrieval_snapshot_id": "rs-77a1",
    "history_digest": "sha256:9ab0...",   // 证明「批的是这段对话」
    "estimated_cost_usd": 0.041
  },
  "risk": { "score": 62, "reasons": ["金额超 ¥500", "首次退款该订单"] },
  "approvers": ["group:cs-lead"],
  "created_at": "2026-05-01T10:02:11Z",
  "expires_at": "2026-05-01T10:32:11Z",
  "on_timeout": "reject",
  "decision_token": "dt-3f9a...",          // 一次性消费凭证，随通知下发
  "idempotency_key": "r-8f2c1a:s-14:refund.create"
}
```

两个字段值得单独说：

- **`args_hash`**：恢复执行时，必须重新计算 args 的哈希并与审批时比对。不一致 → 拒绝执行。这是防「批准了 A、执行了 B」的唯一手段（模型的 plan 在下一次调用时可能变化）。
- **`context_snapshot`**：证明「审批人当时看到的是什么」。如果恢复时提示词版本、检索快照、历史摘要任一变了，你应该**重新审批**而不是继续执行——因为主管批的是另一个语境下的动作（第 31 章的审计链需要这个）。

### 34.4.3 表结构

```sql
CREATE TABLE approval_requests (
  approval_id      TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL,
  step_id          TEXT NOT NULL,
  tenant_id        BIGINT NOT NULL,
  level            SMALLINT NOT NULL,               -- 2=R2 3=R3 4=R4
  tool             TEXT NOT NULL,
  args             JSONB NOT NULL,
  args_hash        BYTEA NOT NULL,
  context_snapshot JSONB NOT NULL,
  status           TEXT NOT NULL,                   -- pending/approved/rejected/...
  approver_group   TEXT NOT NULL,
  decided_by       BIGINT,
  decided_at       TIMESTAMPTZ,
  decision_note    TEXT,
  decision_token   TEXT UNIQUE,                     -- 一次性，消费即失效
  executed_at      TIMESTAMPTZ,                     -- ★ CAS 的目标字段
  apply_result     JSONB,
  expires_at       TIMESTAMPTZ NOT NULL,
  on_timeout       TEXT NOT NULL DEFAULT 'reject',
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (run_id, step_id)                          -- 同一步骤只能有一个审批
);
CREATE INDEX ON approval_requests (status, expires_at) WHERE status = 'pending';
CREATE INDEX ON approval_requests (tenant_id, created_at DESC);

-- ★ 决策消费：一条 SQL 完成「原子取用」，行数=0 表示已被别人消费
UPDATE approval_requests
   SET status = 'executing', executed_at = now()
 WHERE approval_id = $1
   AND status = 'approved'
   AND executed_at IS NULL
RETURNING args, args_hash, context_snapshot;
```

> 🩸 `UNIQUE (run_id, step_id)` 这条约束救过 Aurora 两次：它把「重复创建审批」变成数据库错误，而不是「主管收到两张卡片、批了两次」。

---

## 34.5 恢复时的状态一致性：不重放副作用

这是本章的技术核心。恢复一个挂起的 Run，你要回答三个问题：

1. 从**哪一步**继续？（不能从头重放）
2. 已经执行的副作用，怎么保证**不被执行第二遍**？
3. 审批的**决策**，怎么让后续所有重放都看到？

答案分别是：**step 边界 checkpoint**、**事件流 + 幂等键**、**决策即事件**。

```go
// hitl/resume.go —— 恢复一个挂起的 Run
func (e *Engine) Resume(ctx context.Context, approvalID string, dec Decision) error {
	// ① 原子消费决策：CAS 失败说明已被消费（重复回调、网关重试、并发审批）
	ar, err := e.store.ConsumeDecision(ctx, approvalID, dec) // 见上面那条 UPDATE
	if errors.Is(err, ErrAlreadyExecuted) {
		// 不是错误：返回已有结果，保持幂等（幂等回传是审批 API 的硬要求）
		return e.store.ReturnExisting(ctx, approvalID)
	}
	if err != nil {
		return err
	}

	// ② 决策先落事件流，再执行 —— 顺序不能反
	//    这样即使执行到一半崩溃，重放时也能看到「已批准」而不会重复询问用户
	if err := e.events.Append(ctx, Event{
		RunID: ar.RunID, StepID: ar.StepID,
		Type: "approval.decided",
		Payload: map[string]any{
			"approval_id": approvalID, "decision": dec.Kind,
			"decided_by": dec.Actor, "note": dec.Note,
			"args_hash": hex(ar.ArgsHash),
		},
	}); err != nil {
		return err // 落库失败绝不能继续执行
	}

	// ③ 加载 checkpoint（step 边界），并从「审批步骤之后」恢复游标
	cp, err := e.store.LoadCheckpoint(ctx, ar.RunID)
	if err != nil {
		return err
	}
	cp.Cursor = NextStepAfter(cp.Cursor) // 关键：不重跑审批那一步之前的东西

	// ④ 处理「拒绝」分支：注入一条 tool_result，让模型自己走替代路径
	if dec.Kind == DecisionReject {
		e.events.Append(ctx, Event{
			Type: "tool.result", StepID: ar.StepID,
			Payload: map[string]any{
				"status": "rejected_by_human",
				"reason": dec.Note,
				"hint":   "用户未批准该操作，请给出替代方案或说明原因",
			},
		})
		return e.schedule(ctx, cp) // 继续跑，模型会生成「无法退款，建议…」
	}

	// ⑤ 执行前校验：args 必须与审批时一致（防「批 A 执行 B」）
	if !bytes.Equal(HashArgs(cp.PendingArgs), ar.ArgsHash) {
		e.events.Append(ctx, Event{Type: "approval.args_mismatch", StepID: ar.StepID})
		return ErrArgsDrift // 走重新审批，不执行
	}

	// ⑥ 执行高危工具：带幂等键，且结果先落事件流再回传
	res, err := e.tools.Call(ctx, ar.Tool, cp.PendingArgs, ToolOpts{
		IdempotencyKey: ar.RunID + ":" + ar.StepID + ":" + ar.Tool, // ★
		Timeout:        20 * time.Second,
	})
	if err != nil {
		// ★ 不自动重试：失败可能是「已执行但响应丢了」
		e.store.MarkApplyFailed(ctx, approvalID, err)
		e.events.Append(ctx, Event{Type: "tool.apply_failed", StepID: ar.StepID})
		return err
	}
	if err := e.events.Append(ctx, Event{Type: "tool.result", StepID: ar.StepID, Payload: res}); err != nil {
		return err // 结果必须先持久化
	}
	e.store.MarkApplied(ctx, approvalID, res)
	return e.schedule(ctx, cp)
}
```

### 34.5.1 「不重放副作用」的三重保险

| 保险 | 作用 | 失效场景 |
| --- | --- | --- |
| **事件溯源 + 游标** | 已完成的 step 有 `tool.result` 事件，重放时直接读结果不重跑 | 事件没落库就崩了（所以要「先落库再回传」） |
| **幂等键** | 即使重跑，下游按 `(run, step, tool, args)` 去重 | 下游不支持幂等键（必须要求：所有 R2+ 工具都支持） |
| **决策一次性消费（CAS）** | 同一份审批不会被执行两次 | 网关重试打到不同分区且隔离级别不够（需要强一致存储或唯一约束） |

三重保险要**同时有**。Aurora 的事故里三重全缺：事件没落库、没幂等键、CAS 用 `SELECT` 后 `UPDATE` 实现。

### 34.5.2 挂起期间的「世界变了」问题

挂起 30 分钟，外面可能发生了很多事：

| 变化 | 后果 | 处置 |
| --- | --- | --- |
| 订单状态变了（已被别的渠道退款） | 重复退款 | 恢复时**重新做前置条件校验**（工具层幂等 + 业务状态检查） |
| 提示词/模型版本更新 | 「批的是旧语境」 | `context_snapshot` 哈希不一致 → 重新审批 |
| 审批人权限被回收 | 无效审批 | 决策时校验权限（不是创建时） |
| 用户取消了会话 | 不该再执行 | Run 级取消传播到 pending 审批（→ `CANCELLED`） |
| 租户余额不足 | 执行了但结算失败 | 执行前做一次配额预检（第 23 章） |

**一句话原则**：**审批的「有效期」必须小于「业务状态的有效期」。** 退款金额的有效期是分钟级，那审批 TTL 就该是 30 分钟；如果业务状态 5 分钟就会变，审批 TTL 只能是 3 分钟，否则每次恢复都要重新校验、等于白批。

---

## 34.6 超时与代理审批

### 34.6.1 超时策略：默认必须是「拒绝」

`on_timeout` 只有四种取值，**没有一种叫「通过」**：

| 策略 | 语义 | 适用 | 风险 |
| --- | --- | --- | --- |
| `reject`（默认） | 超时即拒绝，模型走替代路径 | 所有 R3/R4 | 用户体验差（要重来），但安全 |
| `escalate` | 超时转给上一级/值班人 | 有明确升级链的场景 | 升级链断了会一直挂着（要有二次超时） |
| `proxy` | 由**代理审批人**按预设规则决策 | 低金额、高频、规则明确 | 代理规则错 = 批量错，必须有额度上限 |
| `degrade` | 不执行该动作，把 Run 降级为「生成草稿供人确认」 | 外发类（邮件/消息） | 用户要多一步操作 |

**为什么默认是拒绝**：如果默认通过，攻击者只需要「让审批超时」就能绕过审批（给主管发 500 条审批请求把他淹掉）。这在风控上叫**审批洪水攻击**，是真实存在的。

### 34.6.2 代理审批：必须有「额度 + 白名单 + 可追溯」

代理审批（proxy approval）是指：不是人来批，而是按规则自动批。它只应该用于**低风险 + 高频 + 规则清晰**的动作：

```yaml
# policy/proxy-approval.yaml
version: 6
rules:
  - id: PX-001
    match: {tool: refund.create, level: R3}
    conditions:
      all:
        - amount_cny <= 500                       # 额度上限（硬）
        - order.risk_score < 30
        - order.refund_count_30d == 0             # 首次退款
        - tenant.trust_tier >= 2                  # 租户信誉等级
    limits:
      per_run_cny: 800
      per_day_cny: 5000                           # ★ 日累计上限，防「化整为零」
      per_day_count: 40
    on_exceed: escalate_to_human
    audit: {sample_rate: 1.0, tag: auto_approved}

  - id: PX-002
    match: {tool: email.send, level: R3}
    conditions:
      all:
        - recipient.domain in allowlist            # 只能发给已知客户域
        - template_id in approved_templates         # 只能用已审模板
        - body.pii_count == 0
    limits: {per_day_count: 200}
    on_exceed: reject
```

三条铁律：

1. **代理审批也是一条审计记录**，`decided_by` 写 `system:proxy/PX-001`，不能伪装成人。
2. **必须有日累计上限**，否则「每次 499 元 × 100 次」就绕过了单次阈值。
3. **抽样人工复核**（Aurora 抽 10%），并且代理审批的**误批率**要进指标看板——它是你最危险的黑盒。

---

## 34.7 审批的审计与 SLA

### 34.7.1 审计：审批链要能回答「谁在什么信息下批了什么」

| 必须记录 | 为什么 |
| --- | --- |
| 请求时的完整参数 + `args_hash` | 证明批的是什么 |
| 上下文快照（模型/提示词版本/检索快照/历史摘要） | 证明批的时候看到了什么 |
| 审批人身份 + 权限来源 + 认证强度（密码/MFA） | 证明他有资格批 |
| 决策时间 + 从请求到决策的耗时 | SLA 与「是否认真看」的代理指标 |
| 决策备注（可选但强烈建议） | 争议时唯一的语义证据 |
| 执行结果 + 实际参数哈希 | 证明执行的和批的一致 |
| 代理审批命中的规则 id 与版本 | 证明是规则批的，不是人 |

这些字段要进 **append-only 审计链**（第 31 章），并且与 Run 的事件流用 `run_id + step_id` 关联。**审计日志和业务库分开存**——业务库会被改，审计库不会。

### 34.7.2 SLA 与指标

| 指标 | 定义 | Aurora 目标 | 破了会怎样 |
| --- | --- | --- | --- |
| **审批时延 P50** | 创建 → 决策 | < 60s | 用户等太久流失 |
| **审批时延 P95** | 同上 | < 5min | 客服「这单卡住了」投诉 |
| **超时率** | `EXPIRED / total` | < 3% | 高了说明审批人配置错/通知没到 |
| **代理审批比例** | `proxy / total` | 40%~60% | 太低=人累死；太高=橡皮章 |
| **代理审批误批率** | 抽样人工判定 | < 1% | 直接的钱与合规风险 |
| **审批一致性** | 同一类请求不同审批人，通过率差异 | ≤ 15% | 差异大 = 标准不清，要么培训要么改规则 |
| **审批疲劳指数** | 单审批人日均审批数 | < 80 | 超了通过率会虚高（橡皮章效应） |
| **重复执行数** | 同一 `(run, step)` 执行 >1 次 | **0** | 这就是 §34.1 的事故 |

> 💡 **「审批疲劳指数」是最该看的指标**。Aurora 的实测：日均审批数从 40 涨到 110 之后，通过率从 91% 涨到 99.2%，而同期被批准的高风险退款里的坏账率涨了 3 倍。**通过的越多，看得越少。**

---

## 34.8 审批体验：把「打断」设计成「不打断」

审批是唯一必须让人类介入的环节，所以它天然是体验杀手。三个实用设计：

1. **先说一句再挂起。** 挂起前先流式输出一句「这笔退款需要主管确认，我先把材料整理好」，用户不会以为卡死（第 36 章的 `status` 事件）。
2. **让审批人一次看到全部决策所需信息。** 卡片里要有：参数摘要、风险原因、历史同类处理、一键「同意/拒绝/要求补充」。审批人要点开 3 个系统才能判断 → 他会直接点同意。
3. **批量审批但不同批高风险。** R2 可以批量（勾选 20 条一起通过）；R3/R4 禁止批量。这条规则救过很多公司。

---

## 34.9 Java / 分布式经验对照

| 你的 Java 经验 | HITL 里对应什么 | 会咬人的地方 |
| --- | --- | --- |
| 工作流引擎（Flowable/Activiti）的用户任务 | 审批节点 | 那边的「任务」是**流程内的阻塞节点**；这里的「挂起」必须**释放所有资源**，不能靠线程等 |
| `wait()/notify()`、`CompletableFuture.get(timeout)` | 等审批结果 | 全都不能用——审批可能几小时，进程会被重启。必须持久化 + 事件驱动 |
| 分布式事务的两阶段提交 | 决策 + 执行 | 这里没有「回滚」的概念，只有「不重放」；靠幂等键而不是 XA |
| Temporal 的 Signal | 审批回传 | `workflow.GetSignalChannel(ctx, "approval")` 是标准做法；信号可以重复投递，**消费侧必须自己去重**（这就是 CAS） |
| 支付系统的「一次且仅一次」 | 审批决策消费 | 同构；用唯一约束 + 状态机 CAS，不要用 `SELECT FOR UPDATE` 后跨网络调用 |
| MQ 的「至少一次」 | 审批通知 | 卡片可能推两次，审批人看到两张 → 用 `UNIQUE(run_id, step_id)` 保证库里只有一条 |
| 状态机的幂等转换 | 状态迁移 | 每个迁移条件都要写进 `WHERE` 子句，而不是 `if` 判断后 `UPDATE` |

> 🩸 **最典型的错误**：用 `@Transactional` 包住「消费决策 + 调外部退款接口」。外部调用不在事务里，事务回滚了但钱已经退了。**正确姿势：先本地 CAS 消费（短事务）→ 落事件 → 再调外部（带幂等键）→ 落结果。**

---

## 34.10 开源坐标

| 能力 | Temporal | LangGraph | 其他 | 缺什么 |
| --- | --- | --- | --- | --- |
| 挂起/恢复 | ✅ Signal + 持久化工作流（最成熟） | ✅ `interrupt()` + `Command(resume=...)` + checkpointer | — | Temporal 通用但重；LangGraph 轻但需要自己接 checkpointer |
| 审批状态机 | ❌ 只有「等信号」，没有审批语义 | ⚠️ 有 interrupt，但状态模型要自己建 | — | **审批的 CAS/一次性决策/超时策略必须自研** |
| 审批 UI | ❌ | ❌ | 各公司接自己的工单系统 | 卡片内容设计没有标准答案 |
| 代理审批 | ❌ | ❌ | — | 规则引擎（OPA/CEL/Drools 可借用） |
| 审计 | ⚠️ 事件历史可查但非合规级 | ❌ | 接第 31 章的审计链 | 「上下文快照」这类业务字段要自己定义 |
| 超时策略 | ✅ Timer | ⚠️ 要自己实现 | — | 语义（拒绝/升级/代理）要自己建 |

> **一句话**：**Temporal 给了你「暂停一年的工作流」，但没给你「审批」这个概念。** 审批的领域模型（分级、代理、SLA、审计）全是你自己的。

---

## 34.11 事故清单

- [ ] **事故 A：批一次退两次** —— 现象：重复退款 ¥1,280。→ 根因：决策消费非原子 + 网关重试 + 无幂等键。→ 兜底：CAS 消费 + `UNIQUE(run_id, step_id)` + 工具幂等键。
- [ ] **事故 B：挂起拖死数据库** —— 现象：峰值时连接池耗尽，全站 5xx。→ 根因：挂起时占着 DB 连接与 goroutine 等审批。→ 兜底：checkpoint + 释放 + 事件驱动恢复。
- [ ] **事故 C：批 A 执行 B** —— 现象：主管批准退款 ¥500，实际退了 ¥5,000。→ 根因：恢复时重新生成了参数，没校验。→ 兜底：`args_hash` 比对，不一致即拒绝并重新审批。
- [ ] **事故 D：审批洪水绕过风控** —— 现象：攻击者批量触发审批把主管淹没，主管全部点通过。→ 根因：`on_timeout=approve` + 无审批疲劳限流。→ 兜底：超时默认拒绝 + 单审批人日配额 + R3 禁批量。
- [ ] **事故 E：代理审批被化整为零** —— 现象：499 元 × 80 次，全部自动批。→ 根因：只有单次阈值，没有日累计。→ 兜底：`per_day_cny` + 抽样复核 + 误批率看板。
- [ ] **事故 F：恢复时世界已变** —— 现象：挂了 40 分钟后恢复，订单已被其他渠道退款。→ 根因：恢复只重放，不重校验。→ 兜底：恢复时做前置条件校验 + 业务幂等 + `context_snapshot` 不一致则重批。
- [ ] **事故 G：审批记录不可举证** —— 现象：客户投诉「没人批准过这笔退款」。→ 根因：审批记录存在业务库且可改。→ 兜底：append-only 审计链 + 决策备注 + 认证强度记录（第 31 章）。

## 34.12 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 本质 | 审批 = 带外部副作用的两阶段提交；决策只能消费一次、执行必须幂等 |
| 分级 | R0 只读 / R1 幂等写 / R2 写 / R3 高危 / R4 不可逆；能撤回的先别审批，用撤回窗口 |
| 挂起 | **必须释放 goroutine/连接/fd/配额**，从 checkpoint 恢复；10 万挂起会打死数据库 |
| CAS | `UPDATE ... WHERE status='approved' AND executed_at IS NULL`，行数=0 即已被消费 |
| 顺序 | 决策先落事件流 → 再执行 → 结果先落库 → 再回传（顺序反了就会重放） |
| args_hash | 审批对象是「具体参数」，恢复时必须比对，防批 A 执行 B |
| context_snapshot | 模型/提示词/检索快照变了 → 重新审批 |
| 超时 | 默认 `reject`，绝不默认通过（否则被审批洪水打穿） |
| 代理审批 | 单次额度 + **日累计额度** + 白名单 + 抽样复核 + 记规则 id |
| 疲劳 | 单审批人日均 >80 次 → 通过率虚高，坏账率上升 |
| 一句话 | **审批最贵的东西不是那一次点击，是「挂起期间的世界」和「恢复时的一致性」。** |

## 34.13 自检题

1. **你的审批接口被网关重试了两次，怎么保证不会退两次款？**
   > 要点：三层缺一不可——① 决策消费用 CAS（`status='approved' AND executed_at IS NULL`），第二次返回「已执行」的既有结果而不是报错；② 执行带幂等键 `(run_id, step_id, tool, args_hash)`，下游去重；③ `tool_result` 先落事件流再回传，保证任何一次重放都能读到「已经执行过」。另外 `UNIQUE(run_id, step_id)` 防止同时存在两条审批。

2. **为什么审批挂起不能「卡一个 goroutine 等信号」？**
   > 要点：Aurora 峰值 8% 的 Run 触发审批（400 QPS），平均挂起 260 秒 → 10.4 万个并发挂起。每个占 goroutine + DB 连接 + HTTP 连接 + fd，直接打死数据库和文件描述符。而且进程重启会丢失全部挂起状态（用户的审批点了也没用）。正确做法：checkpoint → 落审批请求 → 释放资源 → 事件驱动恢复。

3. **代理审批（自动批）最危险的失效模式是什么？怎么防？**
   > 要点：不是「批错一笔」，而是「按规则批量批错」——规则本身错了，一天可能错几千笔。防线：① 单次额度 + **日累计额度**（防化整为零）；② 白名单（工具/模板/收件域）；③ 强制抽样人工复核（10%）+ 误批率看板；④ 规则版本化 + 灰度（先 1% 流量）+ 一键关；⑤ `decided_by` 写 `system:proxy/<rule_id>`，绝不伪装成人。

## 34.14 延伸阅读

- 上一章：[33. 多 Agent 协作：拓扑、总线与预算](33-multi-agent-collaboration.md)
- 下一章：[35. 前处理层：把脏输入挡在花 token 之前](35-pre-processing.md)
- 总纲：[0-summary.md](0-summary.md)
- 相关：[8. Run API 设计](8-run-api-design.md)（审批回传的 API 语义与幂等）
- 相关：[12. 长任务与持久化执行](12-long-task-durable.md)（checkpoint 与确定性重放）
- 相关：[22. 工具系统与 MCP](22-tool-system-mcp.md)（工具分级与幂等键）
- 相关：[31. 合规、审计与取证](31-compliance-audit.md)（审批链进审计）
- 相关：[37. 输出审计与「撤回已说出去的话」](37-output-audit-retraction.md)（用撤回窗口替代审批）
- 外部参考：Temporal 文档（Signals、Timers、Workflow 持久化）、LangGraph 文档（`interrupt`、`Command(resume=...)`、checkpointer），以官方文档最新版为准
