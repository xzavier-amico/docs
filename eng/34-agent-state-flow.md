# 34. Agent 场景的状态流转与扩展点

> **状态**：`已完成（初稿）` ｜ **所属**：第五部分 · 编排与状态机 ｜ **编号**：34 / 52
> **本章定位**：把第 29~33 章的五件东西（编排权、状态机、DAG、引擎、卡单处置）全部落到 Agent 上。核心增量只有两个：**两个传统状态机里不存在的等待态**，以及**「重放一次 = 重新花一次钱」带来的检查点纪律**。
> **读法**：§34.3 的状态图是本章的地基；§34.4.3 的检查点与恢复必须逐行读，它是"进程随时会死"的唯一答案；§34.4.6 的扩展点清单建议直接做成 PR 模板。

---

## 34.1 事故现场：3,200 个 Run 停在 `RUNNING`，214 封邮件发了两次

### 事故一：状态在库里，但没人负责推进

2025 年 10 月 14 日，某 Agent 平台的合同审阅业务。Run 表里有 `state` 字段，状态机画得很漂亮，但有一个致命细节：**没有租约（lease）字段**。

**02:30**，发布新的 prompt 版本，worker 组滚动重启（6 个 pod，`maxSurge=2`）。

| 时刻 | 事件 |
| --- | --- |
| 02:30 | 滚动重启开始，旧 pod 收到 SIGTERM。代码里没处理这个信号，进程直接退出 |
| 02:31 | **3,200 个 Run** 停在 `RUNNING`。它们的 `run_step` 里最后一条是 `llm.called`，没有 `llm.returned` |
| 02:32 | 新 pod 启动，只从队列领**新** Run。没有任何人会去看"那些已经 `RUNNING` 但没人在跑的" |
| 08:50 | 第一个客户投诉："合同分析转了一晚上，还显示进行中" |
| 09:10 | 值班查 `run` 表：`SELECT state, COUNT(*) FROM run GROUP BY state` → `RUNNING 3200`，但 worker 的活跃 goroutine 数是 0 |
| 09:40 | 值班执行"修复"：`UPDATE run SET state='QUEUED' WHERE state='RUNNING' AND updated_at < NOW() - INTERVAL 1 HOUR` |
| 09:41 | worker 重新领取。**但检查点没更新，最后一个 `llm.called` 对应的模型调用没有结果**，代码选择"重放这一步" |
| 09:44 | 重放时上下文已经变了（新 prompt 版本），模型输出了不同的工具调用序列 |
| 10:05 | **214 封客户邮件被第二次发出**（第一次的 `tool_call` 已成功，但结果没落检查点） |
| 11:30 | 全部恢复。**累计 3,200 个 Run 平均卡 6 小时 12 分，214 次重复外发，37 起投诉，赔偿 8.6 万元** |

三个根因，一条比一条深：

1. **没有租约**：`RUNNING` 是一个"声明"，不是一个"占位"。进程死了，声明还在，但权利义务早就没了。
2. **没有恢复协议**：`UPDATE ... SET state='QUEUED'` 只是把状态改回去，它不知道"上次执行到哪、哪些副作用已经发生"。**恢复不是改状态，是重建执行上下文。**
3. **工具副作用没有账本**：模型调了发邮件的工具，成功了，但结果没落库——于是恢复时无法判断"这封邮件到底发了没有"。**不可回滚定律在这里的具体形态就是：你必须能查。**

### 事故二：外部回调重试 6 次，同一份合同被分析了 6 遍

两周后，同一个平台的 OCR 链路。架构是标准的异步：提交任务 → 进 `WAITING_EXTERNAL` → 等 OCR 服务商回调。

| 时刻 | 事件 |
| --- | --- |
| 10/08 14:02:11 | 回调 #1 到达 `POST /callbacks/ocr`。handler 开始处理，写 checkpoint 时撞上另一个 job 持有的行锁，阻塞 |
| 14:02:19 | 服务商 8 秒没收到 200，按策略（2s/4s/8s/16s/32s）重试 #2 |
| 14:02:19~14:02:40 | #2~#6 全部到达。每个 handler 都执行 `SELECT * FROM run WHERE external_id = ?`，**因为 #1 还没提交，全部查不到** |
| 14:02:40 | 6 个 handler 各自 `INSERT INTO run(...)` 新建了一个 Run，都用同一份合同、同一个 `external_id` |
| 14:02:41~15:10 | 6 个 Run 并行分析同一份合同，**6 倍成本**（单次 ¥4.2 → ¥25.2） |
| 15:10 | 6 个 Run 全部完成，其中 2 个结论不一致（上下文采样不同） |
| 15:30 | 下游取"最新一条"（`ORDER BY created_at DESC LIMIT 1`），取了其中一条 |
| 16:20 | 客户投诉："你这个结论和我上一份合同自相矛盾" |
| 10/09 | 排查发现 860 份合同里 **214 份**遇到重试，多花 ¥4,500，其中 3 份结论被错发 |

根因只有一句：**回调处理没有幂等键，且用了"查不到就创建"这种最危险的模式。**

> 🩸 **两个事故的共同点**：**状态机只记录了"是什么"，没记录"凭什么"和"到哪了"。**
> 前者需要租约与守卫，后者需要检查点与副作用账本。缺任何一个，你的 Agent 在重启面前就是个玩具。

---

## 34.2 到底是什么：Run 状态机 = 第 30 章的五要素 + 两个等待态

### 完整状态定义

| 状态 | 含义 | 终态 | 典型停留 | 唤醒源 |
| --- | --- | --- | --- | --- |
| `CREATED` | 已受理，未占资源 | 否 | ms | 准入 |
| `QUEUED` | 已准入，等 worker 领取 | 否 | 秒 | worker poll |
| `RUNNING` | 有 worker 持有租约，正在推进 | 否 | 秒~分 | 模型返回 / 工具返回 |
| `WAITING_HUMAN` | **等人做决定**（审批、补信息、接管） | 否 | 分钟~天 | 人点同意/拒绝/撤回；超时定时器 |
| `WAITING_EXTERNAL` | **等外部系统回调**（异步工具、第三方审核） | 否 | 分钟~天 | 回调 / 轮询命中 / 超时定时器 |
| `VERIFYING` | 代码校验产物（不信模型自述） | 否 | 秒 | 校验结果 |
| `FINALIZING` | 输出审计 + 计费结算 | 否 | 秒 | 审计通过 |
| `SUCCEEDED` | 成功，产物已校验、账已结 | **是** | — | — |
| `FAILED` | 失败，已补偿/已告警 | **是** | — | — |
| `CANCELLED` | 用户取消，已补偿 | **是** | — | — |
| `EXPIRED` | 等待超时（人没批 / 外部没回） | **是** | — | — |
| `BUDGET_EXCEEDED` | 预算耗尽，返回部分结果 | **是** | — | — |

### 为什么传统状态机没有这两个状态

| 差异 | 传统系统 | Agent 系统 | 结论 |
| --- | --- | --- | --- |
| **等人的方式** | BPMN 的 user task 是"流程里的一个节点"，引擎天然支持挂起 | 没有 BPMN，只有代码；"等人"必须自己建模 | 需要 `WAITING_HUMAN` |
| **等外部的方式** | 通常是同步 RPC 或消息回调，失败即重试 | 外部工具耗时不可预测（OCR 可能 7 天），且回调会重试 | 需要 `WAITING_EXTERNAL` |
| **等待期间占什么** | 引擎的 task 表，不占线程 | 如果写成 `for { sleep }`，占着 worker | **等待态必须落库并释放 worker** |
| **等待的代价** | 几小时后继续，成本几乎为 0 | 等待期间上下文可能失效（文档 TTL、模型版本变更） | 等待态必须记录"依赖的生命周期" |

> 💡 **一句话判据**：**如果你的代码里出现了"阻塞等某个东西"，而那个东西不由你控制，那它就该是一个落库的等待态，而不是一个 `select`/`await`。**

### 三个反例：这些都不是"等待态"

```go
// ❌ 反例 1：占着 goroutine 等审批
func (r *Run) WaitApproval() bool {
    ch := make(chan bool)          // 进程一死，ch 没了，审批结果无处可去
    notifySupervisor(ch)
    return <-ch                    // 占着一个 worker slot，可能几小时
}

// ❌ 反例 2：sleep 轮询
for !approved() {
    time.Sleep(30 * time.Second)   // 占资源、重启即丢、无法水平扩展
}

// ❌ 反例 3：把"等外部"当"失败重试"
// OCR 提交超时 → 直接重试提交 → 服务商那边有两个任务在跑 → 重复计费
```

正解的形状永远是：**写一行状态 + 释放 worker + 由外部事件唤醒 + 有超时兜底**。

### 与 `../llm/6` 的状态命名对齐

`../llm/6-session-run-event-model.md` 用的是小写命名，本章用大写。**它们是同一台机器的两种拼写，不是两套模型**：

| 本章（eng） | `../llm/6` | 说明 |
| --- | --- | --- |
| `CREATED` / `QUEUED` | `created` / `admitted` | 准入前 / 准入后 |
| `RUNNING` | `running` | 有 worker 在推进 |
| `WAITING_HUMAN` | `waiting_approval` | 本章多覆盖了"补信息/接管"，不止审批 |
| `WAITING_EXTERNAL` | `waiting_tool` | 本章强调"外部回调"，含非工具类异步 |
| `VERIFYING` | （在 `finalizing` 内） | 本章把它独立出来，因为"校验"可能重跑模型 |
| `FINALIZING` | `finalizing` | 输出审计 + 计费 |
| `SUCCEEDED` / `FAILED` / `CANCELLED` | `completed` / `failed` / `cancelled` | 终态 |
| `EXPIRED` / `BUDGET_EXCEEDED` | `expired` / `budget_exceeded` | 两个"非错误"的终止方式 |

---

## 34.3 结构图：Run 状态机与恢复路径

```text
                       ┌──────────┐
                       │ CREATED  │
                       └────┬─────┘
                 admit ─────┤──── reject/limit
                            ▼              ▼
                       ┌─────────┐    ┌──────────┐
                       │ QUEUED  │    │ REJECTED │ (终态，也可并进 FAILED)
                       └────┬────┘    └──────────┘
              worker 领取   │  （租约 + fence epoch）
                            ▼
        ┌──────────────────────────────────────────────┐
        │                 RUNNING                      │◀──────────┐
        │  守卫：budget / steps / 工具白名单 / 租约有效   │           │
        └──┬──────────┬───────────┬──────────┬─────────┘           │
   需要审批│   调异步工具│    无工具调用│  用户取消 │                  │
           ▼          ▼           ▼          ▼                     │
   ┌──────────────┐ ┌───────────────┐ ┌──────────┐ ┌──────────┐    │
   │WAITING_HUMAN │ │WAITING_EXTERNAL│ │VERIFYING │ │CANCELLED │    │
   │ 唤醒：审批决策 │ │ 唤醒：回调/轮询 │ └────┬─────┘ └──────────┘    │
   │ 兜底：超时→EXP │ │ 兜底：超时→FAILED│      │ 通过/修正          │
   └──────┬───────┘ └───────┬───────┘      ▼                     │
          │ 同意/拒绝        │ 结果到达   ┌───────────┐             │
          └────────┬─────────┘           │ FINALIZING│ 审计+结算    │
                   │                     └─────┬─────┘             │
                   └──────────▶ RUNNING ────────┘                   │
                                                 │                 │
                        ┌────────────┬───────────┼──────────┐      │
                        ▼            ▼           ▼          ▼      │
                  ┌──────────┐ ┌────────┐ ┌──────────┐ ┌─────────┐ │
                  │SUCCEEDED │ │ FAILED │ │ EXPIRED  │ │BUDGET_  │ │
                  │  (终态)   │ │ (终态) │ │ (终态)   │ │EXCEEDED │ │
                  └──────────┘ └────────┘ └──────────┘ └─────────┘ │
                                                                  │
   恢复路径：worker 崩溃 → 租约过期 → reaper 把 RUNNING 改回 QUEUED ─┘
              ↑ 注意：改回 QUEUED 之前，必须先确认"最后一个工具调用的结果"是已知的
```

### 恢复流程（这张图决定你重启后会不会重复发邮件）

```text
worker 启动 / 领取到 Run
        │
        ▼
┌─────────────────────────┐
│ 1. 读 run 行（含 fence） │
└───────────┬─────────────┘
            ▼
┌─────────────────────────────────────────────┐
│ 2. 读最新 checkpoint（seq 最大的那条）        │
│    校验：code_version 与当前 worker 一致？    │
│          checkpoint.v 与反序列化器一致？      │
└───────────┬─────────────────────────────────┘
     不一致  │  一致
     ┌───────┴────────┐
     ▼                ▼
┌─────────┐   ┌──────────────────────────────────────┐
│ 拒绝恢复 │   │ 3. 重建上下文：plan + 已完成步骤 + 预算  │
│ 派生新 Run│  └──────────────┬───────────────────────┘
└─────────┘                  ▼
              ┌──────────────────────────────────────┐
              │ 4. 找出"最后一个未完成的动作"           │
              │    a) pending 里有 approval → WAITING_HUMAN
              │    b) pending 里有 tool_call → 查副作用账本
              │    c) 都没有 → 从 cursor 继续下一步     │
              └──────────────┬───────────────────────┘
                             ▼
              ┌──────────────────────────────────────┐
              │ 5. 对每个未完成 tool_call：            │
              │    用 idem_key 查 tool_call_ledger     │
              │    ├─ status=SUCCEEDED → 直接取结果，不重调
              │    ├─ status=FAILED    → 按策略重试或放弃
              │    └─ 无记录          → 调一次，先占位再执行
              └──────────────┬───────────────────────┘
                             ▼
                      继续 RUNNING
```

---

## 34.4 怎么实现

### 34.4.1 表结构：run / run_step / tool_call_ledger / approval_request

```sql
-- 主表：一次 Agent 执行。注意 lease_until + fence_epoch，这是"谁能推进"的唯一答案
CREATE TABLE agent_run (
  id              BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  run_id          CHAR(26)     NOT NULL COMMENT 'ULID，对外暴露，单调可排序',
  tenant_id       BIGINT       NOT NULL,
  session_id      CHAR(26)     NOT NULL,
  agent_type      VARCHAR(48)  NOT NULL COMMENT 'contract-review / support-bot',
  state           VARCHAR(24)  NOT NULL DEFAULT 'CREATED',
  version         INT          NOT NULL DEFAULT 0 COMMENT 'CAS 用',
  state_seq       INT          NOT NULL DEFAULT 0 COMMENT '事件序号，见 run_event',
  lease_owner     VARCHAR(64)  NULL COMMENT 'worker id',
  lease_until     DATETIME(3)  NULL,
  fence_epoch     BIGINT       NOT NULL DEFAULT 0 COMMENT '每次换主 +1，旧 worker 写不进来',
  checkpoint_seq  INT          NOT NULL DEFAULT 0 COMMENT '已落盘的最新检查点',
  step_cursor     INT          NOT NULL DEFAULT 0 COMMENT '计划内游标',
  steps_used      INT          NOT NULL DEFAULT 0,
  max_steps       INT          NOT NULL DEFAULT 40,
  budget_usd      DECIMAL(12,4) NOT NULL DEFAULT 0.0000,
  budget_limit    DECIMAL(12,4) NOT NULL DEFAULT 5.0000,
  code_version    VARCHAR(48)  NOT NULL COMMENT 'worker 代码版本，恢复时校验',
  prompt_version  VARCHAR(48)  NOT NULL,
  wait_reason     VARCHAR(64)  NULL COMMENT 'WAITING_* 的原因码',
  wake_deadline   DATETIME(3)  NULL COMMENT '等待态硬超时，到点由定时器推进',
  result_ref      VARCHAR(256) NULL COMMENT '产物在对象存储的 key，不存正文',
  error_code      VARCHAR(64)  NULL,
  created_at      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                  ON UPDATE CURRENT_TIMESTAMP(3),
  finished_at     DATETIME(3)  NULL,
  UNIQUE KEY uk_run_id (run_id),
  KEY idx_claim (state, tenant_id, id),
  KEY idx_reaper (state, lease_until),
  KEY idx_wait_deadline (state, wake_deadline),
  CONSTRAINT chk_run_state CHECK (state IN (
    'CREATED','QUEUED','RUNNING','WAITING_HUMAN','WAITING_EXTERNAL',
    'VERIFYING','FINALIZING','SUCCEEDED','FAILED','CANCELLED',
    'EXPIRED','BUDGET_EXCEEDED'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 步骤表：每一步的输入输出引用、耗时、成本。它是"审计"与"成本对账"的共同底座
CREATE TABLE run_step (
  id           BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  run_id       CHAR(26)     NOT NULL,
  seq          INT          NOT NULL,
  kind         VARCHAR(16)  NOT NULL CHECK (kind IN ('llm','tool','approval','verify','human')),
  name         VARCHAR(96)  NOT NULL,
  state        VARCHAR(16)  NOT NULL CHECK (state IN ('STARTED','SUCCEEDED','FAILED','SKIPPED')),
  input_digest CHAR(64)     NOT NULL COMMENT '输入摘要，用于"同输入不同结果"分析',
  output_ref   VARCHAR(256) NULL COMMENT '大对象只存引用，不进 run 表',
  tokens_in    INT          NOT NULL DEFAULT 0,
  tokens_out   INT          NOT NULL DEFAULT 0,
  cost_usd     DECIMAL(12,6) NOT NULL DEFAULT 0.000000,
  latency_ms   INT          NOT NULL DEFAULT 0,
  error_code   VARCHAR(64)  NULL,
  started_at   DATETIME(3)  NOT NULL,
  finished_at  DATETIME(3)  NULL,
  UNIQUE KEY uk_run_seq (run_id, seq),
  KEY idx_run_kind (run_id, kind, state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 副作用账本：这是"不可回滚定律"的工程实现。每个外部副作用一行，永远不删
CREATE TABLE tool_call_ledger (
  id           BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  run_id       CHAR(26)     NOT NULL,
  step_seq     INT          NOT NULL,
  tool_call_id VARCHAR(40)  NOT NULL COMMENT '模型给的 id，或我们自己生成',
  tool_name    VARCHAR(96)  NOT NULL,
  idem_key     VARCHAR(128) NOT NULL COMMENT 'run_id:step_seq:input_hash',
  input_hash   CHAR(64)     NOT NULL,
  status       VARCHAR(16)  NOT NULL CHECK (status IN ('RESERVED','RUNNING','SUCCEEDED','FAILED','UNKNOWN')),
  result_ref   VARCHAR(256) NULL,
  error_code   VARCHAR(64)  NULL,
  attempt      INT          NOT NULL DEFAULT 0,
  created_at   DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at   DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
               ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_idem (idem_key),
  UNIQUE KEY uk_run_call (run_id, tool_call_id),
  KEY idx_run_step (run_id, step_seq)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 审批请求：不是"一条状态"，是"一次两阶段提交"。详见 ../llm/34-hitl-approval.md
CREATE TABLE approval_request (
  id            VARCHAR(40)  NOT NULL PRIMARY KEY COMMENT 'ap-91f3',
  run_id        CHAR(26)     NOT NULL,
  step_seq      INT          NOT NULL,
  tool_call_id  VARCHAR(40)  NOT NULL,
  action        VARCHAR(96)  NOT NULL COMMENT 'refund.create',
  params_digest CHAR(64)     NOT NULL COMMENT '参数快照的 hash，防"批的和执行的不是一回事"',
  level         VARCHAR(8)   NOT NULL COMMENT 'R2/R3/R4',
  status        VARCHAR(16)  NOT NULL DEFAULT 'PENDING'
                CHECK (status IN ('PENDING','APPROVED','REJECTED','WITHDRAWN','EXPIRED')),
  approver      VARCHAR(64)  NULL,
  delegate_of   VARCHAR(64)  NULL COMMENT '代理审批时，原审批人',
  decision_note VARCHAR(512) NULL,
  expires_at    DATETIME(3)  NOT NULL,
  decided_at    DATETIME(3)  NULL,
  created_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_run_step_call (run_id, step_seq, tool_call_id),
  KEY idx_pending_expire (status, expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

> ⚠️ **`tool_call_ledger` 的 `UNKNOWN` 状态是最重要的一个**。当 worker 在"已发出请求、未收到响应"时崩溃，你不知道对方做没做。此时**不能假设没做并重试**（可能重复），也**不能假设做了并跳过**（可能漏掉）。正确做法：标 `UNKNOWN`，用幂等键去对方系统**查询**（发邮件类没有查询接口的，就必须要求服务商支持 `Idempotency-Key` 或提供查询）。这是"不可回滚定律"最疼的一格。

### 34.4.2 状态迁移（Go）：CAS + 守卫 + 事件追加

```go
type TransitionReq struct {
	RunID      string
	From       []string   // 允许的来源状态（一个事件常有多个合法来源）
	ExpectVer  int
	To         string
	Event      string
	Actor      string     // user:123 / svc:worker / job:reaper
	Reason     string
	Patch      map[string]any // 同事务要写的字段：wait_reason / wake_deadline / result_ref
}

var terminal = map[string]bool{
	"SUCCEEDED": true, "FAILED": true, "CANCELLED": true,
	"EXPIRED": true, "BUDGET_EXCEEDED": true,
}

// Transition 是唯一的写入口。任何绕过它的 UPDATE 都是事故（第 33 章审计的意义就在这）。
func (s *Store) Transition(ctx context.Context, req TransitionReq) (err error) {
	return s.inTx(ctx, func(tx *sql.Tx) error {
		// 1) 守卫：终态不可迁出；进入等待态必须有原因和超时
		if terminal[req.From[0]] {
			return ErrTerminalState
		}
		if strings.HasPrefix(req.To, "WAITING_") {
			if req.Patch["wait_reason"] == nil || req.Patch["wake_deadline"] == nil {
				return fmt.Errorf("%w: waiting state needs reason + deadline", ErrGuard)
			}
		}
		// 2) CAS：状态 + 版本 + 租约必须都还对得上
		sets, args := buildSet(req.Patch)
		q := fmt.Sprintf(`UPDATE agent_run
			SET state = ?, version = version + 1, state_seq = state_seq + 1,
			    updated_at = NOW(3) %s
			WHERE run_id = ? AND state IN (%s) AND version = ?`,
			sets, placeholders(len(req.From)))
		args = append([]any{req.To}, args...)
		args = append(args, req.RunID)
		for _, f := range req.From {
			args = append(args, f)
		}
		args = append(args, req.ExpectVer)

		res, err := tx.ExecContext(ctx, q, args...)
		if err != nil {
			return err
		}
		if n, _ := res.RowsAffected(); n != 1 {
			// 0 行有两种可能：状态非法，或版本冲突。必须区分，否则会把 bug 当并发。
			var cur string
			var ver int
			_ = tx.QueryRowContext(ctx,
				`SELECT state, version FROM agent_run WHERE run_id = ?`, req.RunID).
				Scan(&cur, &ver)
			if ver != req.ExpectVer && cur == req.From[0] {
				return ErrConflict
			}
			return fmt.Errorf("%w: %s -> %s", ErrIllegalTransition, cur, req.To)
		}
		// 3) 事件与状态同事务：状态是投影，事件才是真相源（../llm/6）
		_, err = tx.ExecContext(ctx, `INSERT INTO run_event
			(run_id, seq, type, actor, reason, payload, trace_id)
			SELECT run_id, state_seq, ?, ?, ?, ?, ? FROM agent_run WHERE run_id = ?`,
			req.Event, req.Actor, req.Reason, mustJSON(req.Patch), traceID(ctx), req.RunID)
		return err
	})
}
```

**守卫清单**（缺一条就会在某个深夜炸）：

| 守卫 | 检查什么 | 违反后的正确行为 | 不检查会怎样 |
| --- | --- | --- | --- |
| **终态吸收** | 目标状态不能是终态的来源 | 拒绝，返回 `ErrTerminalState` | `SUCCEEDED` 被改回 `RUNNING`，账重算 |
| **预算** | `budget_usd + 预估 < budget_limit` | 转 `BUDGET_EXCEEDED`，返回部分结果 | 一个死循环烧掉一个月预算 |
| **步数** | `steps_used < max_steps` | 转 `FAILED`（`STEP_LIMIT`） | 模型在 A→B→A 循环里永远不出来 |
| **租约** | `lease_owner = me AND lease_until > now` | 拒绝写，打 WARN | 旧 worker 复活覆盖新 worker 的结果 |
| **等待态** | 必须有 `wait_reason` + `wake_deadline` | 拒绝迁移 | 无限期等待，永远没人发现（第 33 章） |
| **工具白名单** | 该状态下允许调用的工具集 | 拒绝，让模型换路 | 越权调用（如 `RUNNING` 里调 `delete`） |
| **审批参数一致性** | `params_digest` 与执行时一致 | 拒绝执行，重新申请审批 | 批的是 A，执行的是 B |

### 34.4.3 检查点：序列化格式 + 恢复伪代码

**检查点不是"状态快照"，是"恢复所需的最小充分信息"。** 它必须能让一个全新的进程，在不问任何人的前提下，继续把这件事做完。

```json
{
  "v": 3,
  "run_id": "01JQ8Z3K7M9V2X4B6N8P0R2T4W",
  "seq": 42,
  "state": "WAITING_HUMAN",
  "created_at": "2026-02-14T10:02:11.482Z",
  "code_version": "agent@2.14.3",
  "prompt_version": "prompt/contract-review@41",
  "plan": {
    "digest": "sha256:9f2c...",
    "steps_total": 7,
    "cursor": 3
  },
  "context": {
    "digest": "sha256:4a71...",
    "tokens_used": 18422,
    "window_limit": 128000,
    "dropped_segments": [12, 13]
  },
  "budget": {
    "usd_used": "0.8421",
    "tokens_in": 91234,
    "tokens_out": 12044,
    "steps_used": 3
  },
  "steps": [
    {"seq": 1, "kind": "llm",  "state": "SUCCEEDED", "out_ref": "s3://runs/01JQ.../s1.json"},
    {"seq": 2, "kind": "tool", "state": "SUCCEEDED", "tool": "crm.lookup",
     "tool_call_id": "tc-76", "idem_key": "01JQ...:2:8be1", "out_ref": "s3://runs/01JQ.../t2.json"},
    {"seq": 3, "kind": "approval", "state": "PENDING", "approval_id": "ap-91f3"}
  ],
  "pending": [
    {"kind": "approval", "id": "ap-91f3", "tool_call_id": "tc-77",
     "expires_at": "2026-02-14T10:32:11Z"}
  ],
  "fence": {"epoch": 17, "worker_id": "w-3", "lease_until": "2026-02-14T10:07:11Z"}
}
```

四条设计规则：

| 规则 | 为什么 | 反例 |
| --- | --- | --- |
| **只存引用，不存正文** | 一份合同 200KB，40 步 × 200KB = 8MB/run，10 万 run 就是 800GB | 把模型输出全文塞 `steps[].output` |
| **必须带版本** | 代码升级后旧 checkpoint 可能反序列化失败 | 没有 `v` 字段，加字段就炸历史数据 |
| **必须有 digest** | 恢复时要验证"计划没被篡改、上下文没丢关键段" | 只存 cursor，不存计划 hash |
| **必须带 fence** | 判断"我是不是还有资格推进" | 只存 worker_id，不存 epoch |

**恢复伪代码（Go，去掉业务细节后可直接抄）**：

```go
// Resume 是恢复的唯一入口。它的契约：要么完整恢复，要么明确拒绝（不猜）。
func (w *Worker) Resume(ctx context.Context, runID string) error {
	run, err := w.store.GetRun(ctx, runID)
	if err != nil {
		return err
	}
	// 1) 版本闸门：代码/prompt 版本不一致时，不要"尽力恢复" —— 会产出不可复现的结果
	if run.CodeVersion != w.BuildVersion {
		return w.forkNewRun(ctx, run, "CODE_VERSION_MISMATCH")
	}
	cp, err := w.store.LatestCheckpoint(ctx, runID)
	if err != nil {
		return err
	}
	// 2) 拿租约 + 提升 fence epoch：从此之后，旧 worker 的写全部失效
	epoch, err := w.lease.Acquire(ctx, runID, 90*time.Second)
	if err != nil {
		return err // 别人在跑，本轮放弃
	}

	// 3) 重建运行时上下文（不调用模型、不调工具，纯读）
	rt := &Runtime{
		Run:      run,
		Plan:     w.plans.Load(cp.Plan.Digest),
		Context:  w.ctxStore.Load(cp.Context.Digest),
		Budget:   cp.Budget,
		Steps:    cp.Steps,
		Fence:    epoch,
	}

	// 4) 处理"最后一个未完成的动作"
	for _, p := range cp.Pending {
		switch p.Kind {
		case "approval":
			// 审批未决 → 回到等待态，不动模型
			return w.store.Transition(ctx, TransitionReq{
				RunID: runID, From: []string{"RUNNING"}, ExpectVer: run.Version,
				To: "WAITING_HUMAN", Event: "run.resumed_waiting_human",
				Actor: "svc:worker", Reason: "pending approval",
				Patch: map[string]any{
					"wait_reason":   "APPROVAL:" + p.ID,
					"wake_deadline": p.ExpiresAt,
				},
			})
		case "tool_call":
			// 关键：先查账本，再决定调不调
			led, err := w.ledger.Get(ctx, p.IdemKey)
			switch {
			case err == nil && led.Status == "SUCCEEDED":
				rt.AppendResult(p.ToolCallID, led.ResultRef) // 直接复用，不重调
			case err == nil && led.Status == "UNKNOWN":
				// 不知道对方做没做 → 只能查（对方必须提供查询接口）
				ok, ref, err := w.tools.Query(ctx, p.ToolCallID, p.IdemKey)
				if err != nil || !ok {
					return w.fail(ctx, run, "TOOL_STATE_UNKNOWN")
				}
				rt.AppendResult(p.ToolCallID, ref)
			default:
				// 确实没做过 → 先 RESERVED 再执行（两阶段，防止并发重复）
				if err := w.ledger.Reserve(ctx, p.IdemKey, p.ToolCallID); err != nil {
					return err // 抢占失败 = 别人在做
				}
				if err := w.executeTool(ctx, rt, p); err != nil {
					return err
				}
			}
		}
	}

	// 5) 从游标继续；每步都先落检查点，再执行下一步
	return w.loop(ctx, rt)
}

// 每步的固定顺序：占位 → 执行 → 落结果 → 落检查点。顺序反了就会重复副作用。
func (w *Worker) loop(ctx context.Context, rt *Runtime) error {
	for rt.Plan.Cursor < rt.Plan.Total {
		if rt.Budget.USDUsed >= rt.Run.BudgetLimit {
			return w.store.Transition(ctx, TransitionReq{
				RunID: rt.Run.RunID, From: []string{"RUNNING"},
				ExpectVer: rt.Run.Version, To: "BUDGET_EXCEEDED",
				Event: "run.budget_exceeded", Actor: "svc:worker", Reason: "budget",
			})
		}
		step := rt.Plan.Next()
		if err := w.execStep(ctx, rt, step); err != nil {
			return w.handleStepError(ctx, rt, step, err)
		}
		if err := w.store.SaveCheckpoint(ctx, rt.Snapshot()); err != nil {
			return err
		}
		rt.Plan.Cursor++
	}
	return w.finish(ctx, rt)
}
```

> 🩸 **顺序铁律**：**占位 → 执行 → 落结果 → 落检查点**。
> 反过来（先落检查点再执行）→ 崩溃后以为做过了，**漏做**；
> 不做占位直接执行 → 两个 worker 并发，**重复做**；
> 执行了不落结果 → 崩溃后不知道做没做，只能标 `UNKNOWN`（这是最后一道，也是最贵的一道）。

### 34.4.4 HITL 的三个中断点：超时、撤回、代理

审批不是"弹个框"，是**一次带外部副作用的两阶段提交**（详见 `../llm/34-hitl-approval.md`）。这里只讲状态机视角的三个分支：

| 分支 | 触发 | 状态机动作 | 审计必须记录 | 不做会怎样 |
| --- | --- | --- | --- | --- |
| **超时** | `wake_deadline` 到点仍 `PENDING` | `WAITING_HUMAN → EXPIRED`（默认拒绝），或先升级再超时 | 超时时刻、升级链、最终决定 | 审批单永远挂着，Run 永远不结束 |
| **撤回** | 申请人/上级在决策前撤回 | `PENDING → WITHDRAWN`；Run 回到 `RUNNING` 重新规划 | 谁撤回、撤回理由 | 批准了一个已经不需要的动作 |
| **代理** | 原审批人不可达（离职/休假） | `approver = 代理人`，`delegate_of = 原审批人` | 代理依据（组织架构/值班表） | 越权审批，审计过不了 |
| **重复决策** | 网关重试/双击 | `PENDING → APPROVED` 的 CAS 只成功一次 | `idem_key` 与第二次请求的结果 | 同一次审批被执行两次（第 33 章事故） |
| **决策与执行不一致** | 批的参数 ≠ 执行的参数 | 校验 `params_digest`，不一致则拒绝执行 | 两个 digest | 批了 500 元，执行了 50,000 元 |

```yaml
# policies/hitl/refund.yaml —— 审批策略即代码，随版本进 Git
version: 7
defaults:
  on_timeout: reject          # 默认拒绝：不可逆动作，宁可不做
  withdraw_window: 10m

actions:
  - match: { tool: "refund.create", amount_gte: 50000 }
    level: R3
    approvers:
      roles: [supervisor]
      resolve: by_tenant      # 按租户解析具体人，不写死工号
    timeout:
      after: 30m
      action: escalate        # 30 分钟升级
      escalate_to: [finance_lead]
      then: 2h
      on_expire: reject
    delegate:
      allowed: true
      max_depth: 1            # 只允许代理一层，防止无限转授
      require_reason: true
    withdraw:
      allowed: true
      by: [requester, supervisor]

  - match: { tool: "email.send", external: true }
    level: R3
    approvers: { roles: [owner] }
    timeout: { after: 15m, on_expire: reject }
    withdraw: { allowed: false }   # 外发类：决策即生效，不给撤回窗口
```

**Java：审批决策的消费（幂等 + CAS + 唤醒）**

```java
@Service
public class ApprovalService {

    private final JdbcTemplate jdbc;
    private final TransactionTemplate tx;

    /**
     * 消费一次审批决策。网关重试、用户双击都必须安全。
     * 返回 ACCEPTED 表示"这次真的推进了"；DUPLICATE 表示"之前已经消费过"。
     */
    public Outcome consume(String approvalId, String decision, String actor,
                           String note, String idemKey) {
        return tx.execute(status -> {
            // 1) 幂等闸门：同一个 (approval_id, idem_key) 只消费一次
            try {
                jdbc.update("""
                    INSERT INTO approval_decision_log (approval_id, idem_key, actor, decision)
                    VALUES (?, ?, ?, ?)
                    """, approvalId, idemKey, actor, decision);
            } catch (DuplicateKeyException dup) {
                return Outcome.DUPLICATE;          // 重试：直接返回，不改任何状态
            }

            // 2) CAS：只有 PENDING 才能被决策；顺便校验没超时
            int n = jdbc.update("""
                UPDATE approval_request
                   SET status = ?, approver = ?, decision_note = ?,
                       decided_at = NOW(3)
                 WHERE id = ? AND status = 'PENDING' AND expires_at > NOW(3)
                """, decision, actor, note, approvalId);
            if (n == 0) {
                // 区分"已决策"与"已超时"：超时要走 EXPIRED 分支，不是静默失败
                String st = jdbc.queryForObject(
                    "SELECT status FROM approval_request WHERE id = ?", String.class, approvalId);
                return "PENDING".equals(st) ? Outcome.EXPIRED : Outcome.DUPLICATE;
            }

            // 3) 写事件 + 唤醒 Run（同一事务：事件与状态不能分叉）
            jdbc.update("""
                INSERT INTO run_event (run_id, seq, type, actor, reason, payload, trace_id)
                SELECT r.run_id, r.state_seq + 1, 'approval.decided', ?, ?, ?, ?
                  FROM agent_run r
                  JOIN approval_request a ON a.run_id = r.run_id
                 WHERE a.id = ?
                """, actor, note, "{\"decision\":\"" + decision + "\"}", traceId(), approvalId);

            int woken = jdbc.update("""
                UPDATE agent_run
                   SET state = 'QUEUED', version = version + 1,
                       state_seq = state_seq + 1, wait_reason = NULL,
                       wake_deadline = NULL, updated_at = NOW(3)
                 WHERE run_id = (SELECT run_id FROM approval_request WHERE id = ?)
                   AND state = 'WAITING_HUMAN'
                """, approvalId);
            if (woken == 0) {
                // Run 可能已被取消/超时：审批结果仍然记下，但不唤醒
                log.warn("approval decided but run not waiting, approval={}", approvalId);
            }
            return Outcome.ACCEPTED;
        });
    }
}
```

> ⚠️ **`Outcome.EXPIRED` 这一支最容易被写成"静默返回成功"**。审批人点了同意，但已经超时 3 分钟——如果你返回"成功"，主管会以为批了，实际 Run 已经 `EXPIRED` 并走了默认拒绝。**必须明确告诉他不生效，并给出下一步（重新发起/走人工补单）。**

### 34.4.5 长任务：检查点粒度与"重放不要重花钱"

| 决策 | 错误做法 | 正确做法 | 为什么 |
| --- | --- | --- | --- |
| **检查点粒度** | 每个 Run 一个检查点，结束时写 | **每个 step 一次**（毫秒级写，用 append-only） | 崩溃时最多丢一步，不是丢整个 Run |
| **模型输出** | 重放时重新调模型 | 落 `out_ref`，重放直接读 | 重放 = 重新花钱（一次 ¥0.3~3） |
| **上下文** | 每次重放重新拼 prompt | 存 `context.digest` + 段列表，恢复时校验 | 拼装逻辑变了，结果就不可复现 |
| **长 Run** | 一个 Run 跑 8 小时、history 无限长 | 每 50 步或 30 分钟 `continue-as-new` / 开子 Run | history 膨胀、单点内存爆 |
| **版本** | 恢复时用最新代码 | `code_version` 不一致就 fork 新 Run | 用新代码跑老上下文 = 不可复现的结论 |
| **成本** | 只在结束时记账 | 每步 `cost_usd` 累加，进守卫 | 预算守卫必须是"实时"的，不是"事后"的 |

**Java：reaper —— 收尸时必须先问"副作用到底做了没有"**

```java
@Component
public class RunReaper {

    @Scheduled(fixedDelay = 30_000)
    public void reap() {
        // 外层 SELECT 只是"候选集"（autocommit 下 SKIP LOCKED 的锁即刻释放，不要指望它去重）；
        // 真正的并发安全靠 reclaim() 里的 CAS：state='RUNNING' AND lease_until < NOW(3)。
        // 一次 200 条，避免长事务锁住整个表。
        List<Long> ids = jdbc.queryForList("""
            SELECT id FROM agent_run
             WHERE state = 'RUNNING'
               AND lease_until < NOW(3) - INTERVAL 15 SECOND
             ORDER BY lease_until
             LIMIT 200
             FOR UPDATE SKIP LOCKED
            """, Long.class);
        for (long id : ids) {
            try {
                reclaim(id);
            } catch (Exception e) {
                log.error("reclaim failed id={}", id, e);   // 单条失败不能拖垮整批
            }
        }
    }

    @Transactional
    public void reclaim(long id) {
        // 关键分支：如果最后有"已发出、未确认"的副作用，禁止自动重启 —— 只能转人工
        Integer uncertain = jdbc.queryForObject("""
            SELECT COUNT(*) FROM tool_call_ledger l
              JOIN agent_run r ON r.run_id = l.run_id
             WHERE r.id = ? AND l.status IN ('RESERVED','RUNNING','UNKNOWN')
            """, Integer.class, id);
        if (uncertain != null && uncertain > 0) {
            jdbc.update("""
                UPDATE agent_run
                   SET state = 'WAITING_HUMAN', wait_reason = 'TOOL_STATE_UNKNOWN',
                       wake_deadline = NOW(3) + INTERVAL 30 MINUTE,
                       fence_epoch = fence_epoch + 1, lease_owner = NULL,
                       lease_until = NULL, version = version + 1, updated_at = NOW(3)
                 WHERE id = ? AND state = 'RUNNING'
                """, id);
            alert.p0("run needs manual tool-state verification", "run_db_id", id);
            return;
        }
        // 无未知副作用：可以安全重排。fence_epoch +1 让旧 worker 的所有写入失效
        int n = jdbc.update("""
            UPDATE agent_run
               SET state = 'QUEUED', lease_owner = NULL, lease_until = NULL,
                   fence_epoch = fence_epoch + 1, version = version + 1, updated_at = NOW(3)
             WHERE id = ? AND state = 'RUNNING' AND lease_until < NOW(3)
            """, id);
        if (n == 1) {
            metrics.inc("run_lease_reaped_total");
        }
    }
}
```

> 🩸 **`reclaim` 里那个 `if (uncertain > 0)` 是本章最重要的一行判断**。绝大多数"恢复框架"在这里直接 `QUEUED`，然后靠"工具幂等"兜底——但**"发邮件"这类副作用根本没有幂等键可用**（对方系统不支持）。所以：**能自动恢复的前提是"最后一步的状态是已知的"；状态未知就必须交给人。** 宁可多 30 分钟人工，不要多一封错误邮件。

### 34.4.6 扩展点清单：新增一个东西要改几处

这是本章最实用的一张表。**Agent 平台的"迭代速度"取决于这张表有多短。**

| 扩展 | 必须改的地方 | 容易漏的地方 | 验收标准 |
| --- | --- | --- | --- |
| **新增状态** | ① `chk_run_state` 约束 ② 状态机迁移表 ③ 前端状态映射 ④ 卡单 SLA 配置 ⑤ 终态集合 | ⑥ 指标 label 白名单（新状态没加 → 指标丢失）⑦ 报表的 `CASE WHEN` | 新状态在"状态 × 事件"矩阵测试里全覆盖；巡检能扫到 |
| **新增事件** | ① event schema（含 `v`）② 投影函数 `Apply` ③ 幂等键 ④ 事件订阅方 | ⑤ 老版本反序列化（只加字段，不改语义） | 用 3 个历史版本的 history 重放都通过 |
| **新增补偿** | ① 补偿动作注册表 ② 补偿的幂等键 ③ 重试上限 ④ 人工兜底入口 | ⑤ 补偿失败的告警（不能静默） | 注入"补偿失败"后，5 分钟内有人收到告警 |
| **新增工具** | ① 工具 schema（JSON Schema）② 权限/审批等级 ③ 幂等策略声明 ④ 超时与重试 ⑤ 成本模型 | ⑥ **是否需要 `WAITING_EXTERNAL`** ⑦ 是否可查询（决定 `UNKNOWN` 能否恢复） | 工具在"未声明幂等"时拒绝注册（fail fast） |
| **新增审批级别** | ① 策略 YAML ② 审批人解析 ③ 通知通道 ④ 超时默认动作 | ⑤ 审计字段（`delegate_of`/`params_digest`） | 演练：超时/撤回/代理/重复决策四条路径全跑通 |
| **新增等待类型** | ① `wait_reason` 枚举 ② 唤醒源（回调/定时器/人工）③ 超时动作 | ④ **TTL 与外部依赖的生命周期对齐** | 外部依赖过期后，恢复能明确失败而不是静默错 |

### 34.4.7 与三篇 llm 章节的衔接（只给结论）

| 章节 | 它解决什么 | 本章怎么用 | 一句话结论 |
| --- | --- | --- | --- |
| [`../llm/6-session-run-event-model.md`](../llm/6-session-run-event-model.md) | Session/Run/Step/Event 五层模型，事件溯源是唯一真相源 | 本章的 `run_event`、`state_seq`、投影出的 `agent_run.state` 全部照它的模型 | **状态是投影，事件才是账本**；不要把 `UPDATE state` 当成事实。 |
| [`../llm/12-long-task-durable.md`](../llm/12-long-task-durable.md) | 长任务的检查点、租约、fence、恢复 | 本章 §34.4.3 的 `Resume` 与 `loop` 是它的业务化版本 | **已经 `done` 的步骤绝不重算；要重来必须新建 Run。** |
| [`../llm/34-hitl-approval.md`](../llm/34-hitl-approval.md) | 五级动作模型、审批即两阶段提交、审批疲劳 | 本章 §34.4.4 的 `consume` 是它的状态机实现 | **审批决策只能被消费一次；审批不是 UI，是一次分布式事务。** |

---

## 34.5 双修对照：订单状态机 vs Agent Run 状态机

| 维度 | 传统系统（订单/退款） | Agent 系统（Run） | 相同 / 差异根因 |
| --- | --- | --- | --- |
| **机制本体** | 状态 + 事件 + 迁移 + 守卫 + 动作 | 同构，五要素一字不改 | **完全相同** |
| **唯一键/身份** | `order_no` / `refund_no` | `run_id` + `step_id` + `tool_call_id` | 结构相同，粒度更细 |
| **状态集合** | 7~9 个 | 12 个，多两个等待态 | 差异根因：长事务 + 随时中断 |
| **"一步"的成本** | 毫秒~秒，几毛钱 | 秒~分钟，几块到几十块 | 差异根因：成本显性且按量 |
| **失败模式** | 重复支付、超卖、卡单 | 上者 + 死循环、工具重复副作用、模型自称完成 | Agent 多三类 |
| **恢复方式** | 重跑任务（覆盖写） | checkpoint + 副作用账本 + 结果复用 | 机制相同，**Agent 必须先查账本** |
| **验证方法** | 200 并发 + 迁移矩阵 + 对账 | 上者 + kill -9 恢复 + 重放一致性 + 成本对账 | 框架相同，多两维 |
| **观测指标** | 状态停留、非法迁移 | 加 token、成本、工具成功率、TTFT | 指标集扩展 |
| **审计内容** | 谁/何时/为什么/改了什么 | 加：上下文摘要、prompt 版本、每次调用的成本 | 维度扩展 |

**哪些部分完全相同，可以直接复用**：

1. **`UPDATE ... WHERE run_id=? AND state IN (...) AND version=?`**——第 30 章的 CAS 一字不改，只是表名变了。
2. **迁移日志结构**（`from/to/event/actor/reason/trace_id`）——`actor` 从 `user:123` 变成 `agent:planner` / `human:approver` / `job:reaper`。
3. **卡单巡检与 SLA**——第 33 章的 `sla_config` / `stuck_alert` / `intervention_audit` 三张表，`flow_key` 换成 `agent_type` 即可。
4. **终态负责人原则**——`SUCCEEDED` 必须由代码校验（产物存在、schema 通过、断言成立），**不能由模型说"我完成了"**。
5. **租约 + fence**——第 31 章 DAG 任务的 `lease_until` / `attempt`，在 Agent 里就是 `lease_until` / `fence_epoch`。

**Agent 特有的四条（差异根因）**：

| 特有 | 差异根因 | 具体表现 | 对策 |
| --- | --- | --- | --- |
| **路径不确定** | 非确定性 | 同一个 goal，两次执行的 step 数可能 3 vs 12 | 图只约束"允许"，实际路径看 trace；测试用"性质断言"而非精确比对 |
| **重放要花钱** | 成本显性 | 恢复一次 = 重调模型 | 检查点里存模型输出引用，恢复时短路 |
| **产物不可信** | 输出面向人 | 模型说"已生成报告"，文件不存在 | `VERIFYING` 独立成状态，代码校验后进 `FINALIZING` |
| **等待是常态** | 长事务 | 70% 时间在等人/等外部 | 两个等待态 + 释放 worker + 超时兜底 |

---

## 34.6 会在哪炸：失效模式清单

| # | 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- | --- |
| 1 | 发布后 Run 全部停在 `RUNNING` | 没有租约；进程死了状态还是 RUNNING | `SELECT COUNT(*) FROM agent_run WHERE state='RUNNING' AND lease_until < NOW() - INTERVAL 5 MINUTE` | 加租约 + reaper | 租约/ddl 进建表模板；reaper 常开 |
| 2 | 恢复后重复发邮件 | 工具结果没落账本，恢复时重放 | 按 `idem_key` group by 数调用 | 补 `tool_call_ledger` + 先查后调 | 所有写类工具必须注册 `idem_key` 策略 |
| 3 | 回调重试创建 6 个 Run | 回调没有幂等键 + "查不到就创建" | 按 `external_id` group by 数 run | 加 `uk_external_event`；改为"查不到就返回 409" | 所有回调 handler 先插幂等表再处理 |
| 4 | 审批通过但没执行 | 决策消费非原子（`SELECT` 后 `UPDATE`） | 对比审批时间与 Run 唤醒时间 | CAS + 同事务写事件 | 审批走 `consume()` 唯一入口 |
| 5 | 预算超了 10 倍才发现 | 只在结束时记账 | 按 run 看 `budget_usd / budget_limit` | 每步累加 + 守卫 | 预算守卫放在 `Transition` 里，不放在业务代码里 |
| 6 | 恢复后结论和之前不一致 | 代码/prompt 版本变了还硬恢复 | 对比 `code_version` 与最终结论 hash | 版本不一致 → fork 新 Run | `code_version` 落库 + 恢复时校验 |
| 7 | `WAITING_EXTERNAL` 永远不结束 | 没有 `wake_deadline`；外部依赖过期了 | 查最老的等待实例 | 补超时 + 失效检测 | 等待态必须填 deadline（守卫强制） |
| 8 | history/checkpoint 膨胀到 8MB | 存了正文；没有 continue-as-new | 看 checkpoint 大小 P99 | 改存引用 + 每 50 步开子 Run | 单 checkpoint > 256KB 告警 |
| 9 | 两个 worker 同时推进 | 只有租约没有 fence | 日志里两个 worker 交替写同一 run | 加 `fence_epoch`，所有写带 epoch | 租约与 fence 成对出现，禁止只用其一 |

---

## 34.7 怎么验证：kill -9 是最便宜的测试

### ① 崩溃恢复：每 5 秒杀一次 worker，跑 100 次

```bash
#!/usr/bin/env bash
# chaos-resume.sh —— 在 Run 执行期间随机杀 worker，验证"不丢、不重"
set -euo pipefail
RUN_ID=${1:?run id required}
ROUNDS=${ROUNDS:-20}
PSQL="psql -h 127.0.0.1 -U agent -d agent -At"

for i in $(seq 1 "$ROUNDS"); do
  POD=$(kubectl get pod -l app=agent-worker -o jsonpath='{.items[0].metadata.name}')
  echo "round=$i killing $POD"
  kubectl delete pod "$POD" --grace-period=0 --force >/dev/null
  sleep 5   # 让 reaper 有机会接管（租约 90s，测试时可临时调小到 10s）

  # 断言：同一 (run_id, tool_call_id) 在账本里只有一行，且不会出现两个 SUCCEEDED
  DUP=$($PSQL -c "SELECT COUNT(*) FROM (
       SELECT tool_call_id FROM tool_call_ledger
        WHERE run_id='$RUN_ID' GROUP BY tool_call_id HAVING COUNT(*) > 1) x")
  [ "$DUP" = "0" ] || { echo "FAIL: duplicated tool calls ($DUP)"; exit 1; }
done

# 最终断言：Run 到达终态，且步骤序号连续无空洞
$PSQL -c "SELECT state FROM agent_run WHERE run_id='$RUN_ID'"
$PSQL -c "SELECT COUNT(*) FROM generate_series(1,
       (SELECT MAX(seq) FROM run_step WHERE run_id='$RUN_ID')) g
       LEFT JOIN run_step s ON s.run_id='$RUN_ID' AND s.seq=g
       WHERE s.id IS NULL"
# 期望：0（没有丢步）
echo "OK: crash-resume passed"
```

### ② 重放一致性：同一 checkpoint 重放 100 次，结果必须一致

```sql
-- 用"同一个 checkpoint 重放出的 step 序列"做比对：长度、类型序列、工具调用集合必须一致
WITH replay AS (
  SELECT run_id, jsonb_agg(kind || ':' || name ORDER BY seq) AS sig
    FROM run_step
   WHERE run_id = :run_id AND state = 'SUCCEEDED'
   GROUP BY run_id
)
SELECT run_id, md5(sig::text) AS sig_hash FROM replay;
-- 100 次重放后只有一个 hash 才算通过（模型调用必须命中缓存/检查点）
```

### ③ 成本对账：Run 的实际花费必须等于 step 累加

```sql
SELECT r.run_id, r.budget_usd AS ledger_sum,
       COALESCE(SUM(s.cost_usd), 0) AS step_sum,
       ABS(r.budget_usd - COALESCE(SUM(s.cost_usd), 0)) AS diff
  FROM agent_run r
  LEFT JOIN run_step s ON s.run_id = r.run_id
 WHERE r.created_at >= NOW() - INTERVAL 1 DAY
 GROUP BY r.run_id, r.budget_usd
HAVING diff > 0.0001
 ORDER BY diff DESC
 LIMIT 50;
-- 期望：0 行。任何一行都是"有成本没落账"或"重复计费"
```

### ④ 观测指标

| 指标 | 类型 | 用途 | 告警阈值 |
| --- | --- | --- | --- |
| `run_state_total{state}` | Gauge | 各状态存量 | `WAITING_*` 突增 3 倍 |
| `run_stuck_total{state}` | Gauge | 超 SLA 未推进 | P0 状态 > 0 持续 5 分钟 |
| `run_lease_expired_total` | Counter | 租约过期（worker 在崩） | > 0 持续 10 分钟 |
| `tool_call_unknown_total{tool}` | Counter | 状态未知的副作用 | > 0 立即排查（可能要人工核） |
| `run_cost_usd_sum{agent_type}` | Counter | 成本 | 单 run > 预算 80% 预警 |
| `run_verify_failed_total` | Counter | 产物校验失败 | 5 分钟 > 5% |

---

## 34.8 用什么工具

| 需求 | 首选 | 备选 | 不要用 |
| --- | --- | --- | --- |
| Run 状态机（自研可控） | 业务库 + `Transition` 唯一写入口 | Temporal（长任务多时） | 内存里的状态机库 |
| 跨天等待 + 定时器 + 重放 | Temporal（activity 包模型调用） | 自研 + 延迟队列 | 纯 MQ（没有状态） |
| 短 Run（<3 分钟）、步骤固定 | 自研 + 检查点表 | LangGraph（原型） | 上 Temporal（运维不值） |
| 人审批 | 自研 `approval_request` + 通知通道 | 飞书/钉钉审批 API | 在群里口头批（无审计） |
| 检查点存储 | 关系库（小 JSON）+ 对象存储（大产物） | Redis（只做缓存，不做真相源） | 全塞进 Redis（会丢） |
| 恢复编排 | 自研 `Resume`（本章） | Temporal 的 workflow replay | "重启进程让它自己再跑一遍" |

---

## 34.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 状态机 | 与第 30 章同构，五要素不变；Agent 只多两个等待态 |
| 两个等待态 | `WAITING_HUMAN`（等人）/ `WAITING_EXTERNAL`（等回调）——**必须落库、释放 worker、有 deadline** |
| 判据 | 代码里"阻塞等一个不由你控制的东西" → 它应该是一个落库的状态 |
| 租约 | `RUNNING` 不是声明，是占位；没有 `lease_until` 就没人收尸 |
| fence | 只有租约没有 fence，旧 worker 复活会覆盖新结果；两者必须成对 |
| 检查点 | 每步一次；只存引用；带 `v` / digest / fence / code_version |
| 顺序 | **占位 → 执行 → 落结果 → 落检查点**；反了就漏做或重做 |
| 副作用账本 | 每个工具调用一行，`idem_key` 唯一；`UNKNOWN` 是最重要的状态 |
| 恢复 | 先查账本，再决定调不调；`SUCCEEDED` 直接复用，绝不重调 |
| 审批 | 一次两阶段提交：CAS 消费一次 + 同事务写事件 + 唤醒 Run |
| 终态 | `SUCCEEDED` 必须由代码校验，不信模型自述 |
| 双修 | 落库套路一字不改；Agent 多"重放=重花钱"与"产物不可信" |

## 34.10 自检题

1. **为什么 `WAITING_HUMAN` 不能用一个阻塞的 goroutine 来实现？说出三个具体的失败点。**
   > 要点：① **进程重启即丢**：channel 在内存里，pod 一死，审批结果无处投递，Run 永远卡住（事故一）；② **占资源**：一个等待 72 小时的审批会占住一个 worker slot，100 个待审批就吃光并发；③ **无法水平扩展/无法观测**：你没法用 SQL 查"现在有多少人在等审批、最老的等了多久"。正解是写一行 `state='WAITING_HUMAN' + wake_deadline`，释放 worker，由审批回调唤醒，并由定时器兜底超时。

2. **`tool_call_ledger` 里的 `UNKNOWN` 状态，为什么不能简单地"重试一次"或"当作成功跳过"？**
   > 要点：`UNKNOWN` 的语义是"请求已发出，但不知道对方做没做"（worker 在两者之间崩了）。当成成功跳过 → 可能漏掉一次真实副作用（客户没收到退款）；直接重试 → 可能重复副作用（退两次）。唯一正确的做法是**用幂等键向对方查询**（要求外部系统支持 `Idempotency-Key` 或提供查询接口）；查不到就人工介入。这也是为什么"新增工具"的扩展点里必须声明"是否可查询"。

3. **恢复时发现 `code_version` 与 checkpoint 不一致，为什么不能"尽力恢复"？**
   > 要点：因为 Agent 的输出依赖 prompt 拼装、工具 schema、解析逻辑——这些都在代码里。用新代码跑老上下文，会产出**不可复现、也无法解释**的结论（客户问"为什么这次和上次不一样"，你答不出来）。正确做法是 fork 一个新 Run：老 Run 标 `FAILED(CODE_VERSION_MISMATCH)` 并保留全部证据，新 Run 用新版本从头跑（或从最后一个"语义稳定"的步骤续）。可复现性比"省一次重跑的钱"重要得多。

## 34.11 延伸阅读

- 上一章：[33. 卡单、重跑、跳过与人工干预](33-stuck-order-and-rerun.md)
- 下一章：[35. 数据模型决定存储：八类存储的本质](35-storage-model-selection.md)
- 相关：[29. 编排是什么](29-orchestration-vs-choreography.md)（终态负责人）、[30. 状态机建模与落库](30-state-machine-modeling.md)（五要素与 CAS）、[31. DAG 编排](31-dag-orchestration.md)（租约与 fence）、[32. 编排引擎选型](32-workflow-engine-selection.md)（Temporal 的 activity 边界）
- 平台侧（本章的三块地基）：
  - [../llm/6-session-run-event-model.md](../llm/6-session-run-event-model.md) —— 状态是投影，事件是账本
  - [../llm/12-long-task-durable.md](../llm/12-long-task-durable.md) —— 已完成的步骤绝不重算
  - [../llm/34-hitl-approval.md](../llm/34-hitl-approval.md) —— 审批决策只能被消费一次
- 幂等与补偿：[2. 幂等的实现光谱](2-idempotency-implementations.md)、[13. Saga 三连坑](13-saga-pitfalls.md)
- 官方文档（以版本为准）：Temporal 的 `Workflow.await` / `SignalExternalWorkflow` / `ContinueAsNew`；MySQL 8.0 `CHECK CONSTRAINT` 与 `INSERT ... SELECT`；PostgreSQL `jsonb` 与 `generate_series`
- 总纲：[0-summary.md](0-summary.md)
