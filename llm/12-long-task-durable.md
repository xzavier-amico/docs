# 12. 长任务与持久化执行

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 请求生命周期与队列调度 ｜ **编号**：12 / 52
> **本章定位**：30 秒到 30 分钟的 Run，中途挂掉之后你要么丢掉几十分钟算力，要么重放出三封重复邮件。本章讲怎么让它「必定跑完、重跑最省、副作用不重复」。
> **读法**：先看 §12.2 的分档表，判断你现在到底该不该上工作流引擎——**大部分团队是不该上的**。已经在用 Temporal 的，直接跳 §12.4.4「模型输出非确定性」和 §12.7 事故清单。

---

## 12.1 引子：一次 47 分钟的 Run，和三封重复的邮件

Aurora 的客服运营每周五跑一次「批量售后核查」：把本周 2 万条工单过一遍，逐条查订单、查物流、让模型判定是否符合退款条件、生成结论、给客户发邮件。

单条工单 4~6 步，整批 47 分钟。这条链路你在 Java 时代会写成什么样？

```java
// 你在 Spring 里最可能的第一版
@Scheduled(cron = "0 0 2 * * FRI")
public void batchAudit() {
    List<Ticket> tickets = ticketRepo.findPending();
    for (Ticket t : tickets) {
        auditOne(t);   // 里面有 5 次 HTTP 调模型、2 次查库、1 次发邮件
    }
}
```

上线三个月后，你在监控里看到的是这一串：

| 时间 | 事件 | 后果 |
| --- | --- | --- |
| 周五 02:31 | 发布流水线滚动更新，`kill -TERM` 干掉 12 个 Pod 中的 4 个 | 正在跑的 380 条工单直接消失，且没人知道是哪 380 条 |
| 周五 02:32 | 运维手动重启任务（整批从头跑） | **前 31 分钟里已经发过邮件的 1.2 万客户，又收到一遍** |
| 周五 02:40 | 客户投诉涌入 | 47 封「你们骚扰我」工单 |
| 周五 03:10 | 你加了个 `if (t.getStatus() == DONE) continue;` | 但状态是在发邮件**之后**才写的，中间崩溃仍然重复 |
| 下周 | 你改成「先写状态再发邮件」 | 于是出现了「标记已发但实际没发」的 214 条静默漏单 |

**47 分钟里，只崩了 4 个 Pod，就产生了 1.2 万封重复邮件。** 这就是本章要解决的问题：**执行不是一次函数调用，是一段有进度、有副作用、必须可恢复的状态机**。

> 🩸 一句话记住：**「重跑一遍」在传统批处理里是廉价操作，在 Agent 系统里是昂贵且危险的操作**——因为它会重新花钱调模型，还会重新发邮件。

---

## 12.2 先分档：你的 Run 到底需要什么级别的持久化

不是所有 Run 都需要工作流引擎。**上错档次的代价，比不上档次更大**（Temporal 的运维成本和每次 activity 的持久化写，会让你在 5k QPS 下先被存储打爆）。先按持续时长分档：

| Run 时长 | 典型场景 | 执行模型 | 失败恢复手段 | 单 Run 副作用风险 |
| --- | --- | --- | --- | --- |
| < 3s | 意图识别、单轮问答、改写 | 同步 HTTP | 客户端重试（只读，随便重） | 无 |
| 3~30s | 客服标准 Run（5 步、2 次工具） | 同步 + SSE | `Last-Event-ID` 断线续传（第 7 章） | 低：最多 2 次写工具 |
| 30s ~ 3min | 多步检索 + 多工具 | 异步 Run + 事件订阅 | 每步 checkpoint | 中 |
| 3 ~ 30min | 编码 Agent、长文档处理 | **工作流引擎** | checkpoint + 确定性重放 | 高 |
| 30min ~ 24h | 批量核查、爬取分析 | 工作流 + 分片并行 | + 补偿（Saga） | 很高 |
| > 24h | 持续监控、定时对账 | 调度器 + 工作流 + 人工兜底 | + 人工介入通道（第 34 章） | 极高 |

**分界线在哪？** 我给两个可以拍在评审会上的判据：

1. **单次失败重跑的期望成本 > 一次人工兜底的工时成本** → 该上工作流。Aurora 一次核查 Run 重跑花 0.38 美元、耗 47 分钟；人工兜底 10 分钟。阈值大概在「重跑 > 5 分钟」。
2. **存在不可逆副作用（发邮件/扣款/写外部系统）** → 必须上，哪怕只有 30 秒。因为没有 checkpoint 你就无法回答「这条到底发没发」。

### 账本：滚动更新到底会杀掉多少 Run

这组数字请在评审会上直接念：

```
Aurora 峰值：Run 创建 300/s，平均时长 30s
→ 稳态在途 Run = 300 × 30 = 9,000 个        （Littles 定律，见第 14 章）
→ 其中长任务（>30s）占 8% = 720 个在途

集群规模：96 个 worker Pod，每两周一次滚动更新
→ 每批 25% = 24 个 Pod × 平均 7.5 个长任务 = 180 个 Run 被 SIGTERM
→ 优雅关停 30s 只能救回「恰好快结束的」约 35% → 117 个 Run 硬中断
→ 这 117 个里，已完成至少一次写工具的约 60% = 70 个
→ 若无幂等：70 次重复副作用；整批重跑：多花 47 分钟 + 约 $380 模型费
```

一个月两次发布 = **140 次重复副作用 / 月**。这个量级已经不是「偶发 bug」了，是每周都要给客户道歉。

---

## 12.3 结构图：控制面派发、执行面推进、存储面对账

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 控制面 Control Plane                                                      │
│  ┌────────────────┐   ┌────────────────┐   ┌──────────────────────────┐  │
│  │ Run API        │   │ Workflow       │   │ Timer / Signal           │  │
│  │ Idempotency-Key│   │ Registry       │   │ 延迟唤醒 · 审批回传       │  │
│  └───────┬────────┘   └───────┬────────┘   └────────────┬─────────────┘  │
└──────────┼────────────────────┼─────────────────────────┼────────────────┘
           │ ① 创建 Run          │ ② 派发任务                │ ⑥ 唤醒（挂起 72h 后）
           ▼                    ▼                         ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 执行面 Execution Plane（无状态 worker，可随意扩缩/被杀）                    │
│   ┌──────────────────────────────────────────────────────────────────┐   │
│   │ driver loop:  load CP → 跳过 done → 执行 pending → 写 CP → ...    │   │
│   │               ↑ 拿租约(lease + fence_token)，抢不到就退出          │   │
│   └───┬──────────────┬───────────────┬────────────────┬───────────────┘   │
└───────┼──────────────┼───────────────┼────────────────┼───────────────────┘
        │ ③ 写 CP       │ ④ 调模型       │ ⑤ 调工具(幂等键) │ ⑦ 补偿
        ▼              ▼               ▼                ▼
  ┌───────────┐  ┌────────────┐  ┌────────────┐  ┌──────────────┐
  │checkpoint │  │ 模型网关    │  │ 工具网关    │  │ 补偿栈 Saga   │
  │  (PG)     │  │ 输出落事件  │  │ 去重表      │  │ 逆序执行      │
  └───────────┘  └────────────┘  └────────────┘  └──────────────┘
        │
        ▼
  ┌─────────────────────────────────────────────────────┐
  │ 事件历史（append-only，唯一真相源）→ 投影出 Run 状态  │
  └─────────────────────────────────────────────────────┘
```

三个面各自的职责，一句话说清：

- **控制面**只做「什么时候该谁跑」，不保存执行细节；它必须能承受 9000 个在途 Run 的注册表。
- **执行面必须彻底无状态**。任何「进程内存里的中间结果」都是 bug——这是第 1 章断崖 4 的正面回答。
- **存储面是唯一真相**。`run_checkpoint` 是快照，事件历史是账本；两者冲突时以事件为准（第 6 章）。

---

## 12.4 关键设计

### 12.4.1 checkpoint 表结构：三张表就够了

```sql
-- ① 步骤级快照：driver 只信这张表
CREATE TABLE run_checkpoint (
  run_id        TEXT        NOT NULL,
  seq           BIGINT      NOT NULL,          -- 单调递增步骤号，重放时靠它对齐
  tenant_id     BIGINT      NOT NULL,
  step_kind     TEXT        NOT NULL,          -- llm | tool | wait | approval
  step_name     TEXT        NOT NULL,          -- "LLMJudge" / "send_email"
  status        TEXT        NOT NULL,          -- pending | done | failed | compensated
  input_hash    CHAR(64)    NOT NULL,          -- sha256(规范化后的入参)，构成幂等键
  output_ref    TEXT,                          -- 大对象放 S3，库里只存引用
  output_digest CHAR(64),                      -- 用于校验引用是否被篡改/淘汰
  attempt       SMALLINT    NOT NULL DEFAULT 0,
  side_effect   BOOLEAN     NOT NULL DEFAULT FALSE,  -- 是否已触碰外部世界
  fence_token   BIGINT      NOT NULL,          -- 写这张表的 worker 必须持有 >= 它的租约
  finished_at   TIMESTAMPTZ,
  PRIMARY KEY (run_id, seq)
) PARTITION BY HASH (run_id);

-- ② Run 级租约：防止两个 worker 同时推进同一个 Run
CREATE TABLE run_lease (
  run_id      TEXT PRIMARY KEY,
  owner       TEXT        NOT NULL,            -- worker 实例 ID
  fence_token BIGINT      NOT NULL,            -- 每次抢到租约 +1，单调递增
  expires_at  TIMESTAMPTZ NOT NULL
);

-- ③ 分发索引：只扫「该被推进的」Run，别全表扫
CREATE INDEX idx_runnable ON run_checkpoint (status, finished_at)
  WHERE status IN ('pending', 'failed');
```

**为什么要有 `fence_token`？** 这是分布式锁的经典坑：worker A 的租约过期了，但它自己不知道，还在慢悠悠地写完最后一次 checkpoint；此时 worker B 已经接管。两个 worker 同时推进，同一个步骤被执行两次。解决方式是**每次写 checkpoint 都带上自己的 fence_token，并校验它仍然有效**：

```sql
UPDATE run_checkpoint
   SET status = 'done', output_ref = $4, fence_token = $3
 WHERE run_id = $1 AND seq = $2
   AND fence_token <= $3
   AND EXISTS (SELECT 1 FROM run_lease
                WHERE run_id = $1 AND fence_token = $3 AND expires_at > now());
```

`rows affected = 0` 就说明**你已经被夺权了，立刻停下**。这不是乐观锁，是「过期工人必须自己退场」——没有这一条，你所有的幂等设计都是脆的。

### 12.4.2 driver loop：重放不等于重算

自研状态机的全部精髓就在 20 行里：**已经 `done` 的步骤直接读结果，不再执行**。

```go
type Step struct {
	Seq        int64
	Kind       string // llm | tool | wait | approval
	Name       string
	Run        func(ctx context.Context, in json.RawMessage) (json.RawMessage, error)
	Compensate func(ctx context.Context, out json.RawMessage) error // nil 表示无需补偿
}

func Drive(ctx context.Context, runID string, steps []Step) error {
	lease, ok, err := leaseStore.Acquire(ctx, runID, workerID, 45*time.Second)
	if err != nil || !ok {
		return ErrLeaseHeld // 别人在跑，直接退出，不要抢
	}
	defer leaseStore.Release(ctx, runID, lease.Fence)

	cp, err := cpStore.LoadAll(ctx, runID) // seq -> checkpoint
	if err != nil {
		return err
	}

	var compensations []Step // 补偿栈，逆序执行
	for _, s := range steps {
		if prev, ok := cp[s.Seq]; ok && prev.Status == "done" {
			// ★ 关键：命中已完成的步骤，直接复用输出，绝不重调模型
			if s.Compensate != nil {
				compensations = append(compensations, s)
			}
			continue
		}

		out, err := s.Run(ctx, cp[s.Seq].Input)
		if err != nil {
			if retryable(err) && cp[s.Seq].Attempt < 3 {
				return cpStore.MarkRetry(ctx, runID, s.Seq, err) // 交给下次 tick
			}
			// 不可重试 → 逆序补偿
			for i := len(compensations) - 1; i >= 0; i-- {
				_ = compensations[i].Compensate(ctx, cp[compensations[i].Seq].Output)
			}
			return cpStore.MarkFailed(ctx, runID, s.Seq, err)
		}

		if err := cpStore.SaveDone(ctx, runID, s.Seq, out, lease.Fence); err != nil {
			return err // fence 失效会在这里报错，正确行为就是退出
		}
		if s.Compensate != nil {
			compensations = append(compensations, s)
		}
	}
	return cpStore.MarkCompleted(ctx, runID, lease.Fence)
}
```

这段代码有三处必须是硬约束，你抄的时候别省：

- `s.Run` **绝不能**在内部做重试。重试由外层 driver 决定，否则一次崩溃会放大成 3 次模型调用。
- 每次 `SaveDone` 都校验 fence，失败即退出。
- 补偿栈**只压成功的步骤**，逆序执行；补偿本身也必须幂等（补偿失败要能重试）。

### 12.4.3 Temporal 的写法：把确定性交给引擎

如果你选择 Temporal，同一个流程长这样。**注意所有「读时间、拿 UUID、随机数」都必须走 SDK 提供的确定性 API**，否则重放会直接抛非确定性错误。

```go
func RefundAuditWorkflow(ctx workflow.Context, in AuditInput) (AuditResult, error) {
	logger := workflow.GetLogger(ctx)

	// 工作流版本演进：老 Run 继续走 v1，新 Run 走 v2，不需要停机
	v := workflow.GetVersion(ctx, "add-tax-line", workflow.DefaultVersion, 1)

	ao := workflow.ActivityOptions{
		StartToCloseTimeout: 2 * time.Minute,
		HeartbeatTimeout:    30 * time.Second, // 长 activity 必须心跳，否则引擎判定 worker 失联
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:        time.Second,
			BackoffCoefficient:     2.0,
			MaximumInterval:        30 * time.Second,
			MaximumAttempts:        3,
			NonRetryableErrorTypes: []string{"InvalidArgument", "PolicyViolation"},
		},
	}
	ctx = workflow.WithActivityOptions(ctx, ao)

	var order Order
	if err := workflow.ExecuteActivity(ctx, "FetchOrder", in.OrderID).Get(ctx, &order); err != nil {
		return AuditResult{}, err
	}

	// 模型判定也是一个 activity：输出被写进 workflow 事件历史，
	// 之后任何重放都只是「读历史」，不会再调一次模型、再花一次钱。
	var verdict Verdict
	if err := workflow.ExecuteActivity(ctx, "LLMJudge", JudgeInput{Order: order, Policy: in.PolicyVer}).
		Get(ctx, &verdict); err != nil {
		return AuditResult{}, err
	}

	// 金额超限 → 挂起等人工，最长 72 小时。挂起期间不占 worker、不计超时。
	if verdict.Amount > in.AutoApproveLimit {
		var approval Approval
		ch := workflow.GetSignalChannel(ctx, "approval")
		var timedOut bool
		sel := workflow.NewSelector(ctx)
		sel.AddReceive(ch, func(c workflow.ReceiveChannel, _ bool) { _ = c.Receive(ctx, &approval) })
		sel.AddFuture(workflow.NewTimer(ctx, 72*time.Hour), func(workflow.Future) { timedOut = true })
		sel.Select(ctx)
		if timedOut {
			verdict = Verdict{Decision: "escalate", Reason: "approval_timeout"}
		} else if !approval.Approved {
			verdict = Verdict{Decision: "reject", Reason: approval.Reason}
		}
		logger.Info("approval resolved", "decision", verdict.Decision)
	}

	var sent SendResult
	if err := workflow.ExecuteActivity(ctx, "SendMail", MailInput{
		// 幂等键由 workflow 语义保证：同一 workflow run 的同一 activity 只会成功一次
		IdemKey: workflow.GetInfo(ctx).WorkflowExecution.ID + ":mail",
		To:      order.CustomerEmail,
		Body:    verdict.Letter,
	}).Get(ctx, &sent); err != nil {
		return AuditResult{}, err
	}

	if v >= 1 {
		// v2 新增：补一行税额，老 Run 不受影响
		_ = workflow.ExecuteActivity(ctx, "AppendTaxLine", order.ID).Get(ctx, nil)
	}
	return AuditResult{Verdict: verdict, MailID: sent.ID}, nil
}
```

> ⚠️ Temporal 的三个硬约束，写错就是生产事故：
> 1. **工作流代码必须确定性**。`time.Now()` / `uuid.New()` / `rand` / map 迭代顺序 → 一律换成 `workflow.Now(ctx)`、`workflow.SideEffect(ctx, ...)`。
> 2. **activity 里可以随便不确定性**，但它的返回值会被记进历史。返回巨大对象（比如 200KB 的模型原文）会撑爆历史，**只返回引用**。
> 3. **历史长度有上限**（默认单工作流事件数有阈值），长循环记得用 `workflow.NewContinueAsNewError` 切段。

### 12.4.4 处理「模型输出非确定性」——这是 Agent 特有的那一刀

传统工作流引擎假设「activity 是确定的或者幂等的」，Agent 两条都不满足：**同一个 prompt 调两次，输出不一样；而且每次都要钱**。所以你必须显式选择策略：

| 策略 | 做法 | 代价 | 适用 |
| --- | --- | --- | --- |
| **记录重放（默认）** | 模型输出作为事件落库，重放只读事件，永不重调 | 存储 + 读延迟 | 90% 场景，尤其是判定类 |
| **边界收敛** | 工作流代码只放纯逻辑，所有模型调用都在 activity 边界内 | 0 | 所有场景，这是纪律 |
| **降低不确定性** | `temperature=0` + 固定 seed + 锁定模型快照版本 | 0 | 只能降低，不能消除 |
| **语义幂等** | 允许重算，用业务键在**下游**去重 | 中 | 生成类任务（文案、摘要） |
| **只认已提交** | 一旦步骤状态是 `done` 就绝不重算；要重来必须新建 Run | 0 | 副作用大的场景 |

**为什么「记录重放」是默认？** 算笔账：

```
Aurora 一次核查 Run：5 次模型调用 × 25k input token = 125k input token
按 $3/M input 计：$0.375 / Run
若重放时重调模型：每个被中断的 Run 多花 $0.375
每月 140 次中断 → $52.5/月（钱不多，但）

真正贵的是「不一致」：
重调后模型给出不同判定 → 同一个客户，第一次判定"可退"，第二次判定"不可退"
→ 客户投诉 + 无法举证"当时到底为什么这么判"
```

**输出不一样本身不是问题，问题是你说不清哪一次算数。** 所以模型调用的记录必须包含：

```json
{
  "run_id": "run_01J8...",
  "seq": 3,
  "step": "LLMJudge",
  "model": "claude-sonnet-4-5",
  "model_snapshot": "2026-01-15",
  "prompt_id": "refund-judge",
  "prompt_rev": 37,
  "prompt_hash": "sha256:9f2a...",
  "messages_digest": "sha256:1c07...",
  "params": { "temperature": 0, "max_tokens": 800, "seed": 42 },
  "raw_output_ref": "s3://aurora-trace/2026/05/01/run_01J8/step3.json",
  "usage": { "in": 24810, "out": 412, "cached_in": 21000 },
  "finish_reason": "stop",
  "provider_request_id": "req_011C...",
  "latency_ms": 5210
}
```

这 17 个字段，少任何一个，你都无法在三个月后回答客户的「你们当时凭什么拒绝我」。

---

## 12.5 Java / 分布式经验对照

| 你的老经验 | Agent 长任务里的对应 | 会咬人的差异 |
| --- | --- | --- |
| Spring `@Scheduled` + 全表扫 | 工作流引擎的 timer | 定时任务没有「进度」概念，重启=从头跑；工作流有 |
| Quartz / XXL-Job 分片 | workflow 分片 + 租约 | 分片是「静态切片」，Agent 分片是「动态抢租约」，必须带 fence_token |
| 数据库事务 | 步骤级 checkpoint | **不能用一个事务包住整个 Run**：一次 Run 40 分钟，事务会锁死；只能 Saga |
| RocketMQ 事务消息 | Outbox + 幂等消费 | 同构，但 Agent 的「下游」是模型和外部 API，多数不支持事务（详见第 13 章） |
| `@Retryable` 无脑重试 | 分类重试 | 模型调用重试=重复付费；写工具重试=重复副作用；只有只读才能无脑重试 |
| 分布式锁（Redis Redlock） | lease + fence_token | 锁续期失败时，持锁者自己不知道——**fence token 是唯一可靠的兜底** |
| 状态机（Spring StateMachine） | Run 状态机 | 同构。差别是 Agent 的状态转移**有成本**，每次转移都是一次模型调用 |
| `Thread.sleep` 等待 | `workflow.NewTimer` | 挂起 72 小时等审批不能占线程/goroutine；必须能被持久化并唤醒 |

---

## 12.6 开源坐标

| 项目 | 它给了什么 | 它没给你什么 |
| --- | --- | --- |
| **Temporal** | 事件历史 + 确定性重放 + activity 重试策略 + signal/timer + `GetVersion` 版本演进。工业级最完整 | ① 不解决「模型输出非确定性」——你得自己把输出记进历史（做错了就是重放时重新调模型、重新花钱）；② 补偿（Saga）要自己维护栈；③ 持久层（Cassandra/PostgreSQL）的写入量要单独做容量规划 |
| **Restate** | 轻量运行时，虚拟对象 + 持久化 Promise + 原生延迟调用，部署比 Temporal 简单 | 生态较新，跨语言 SDK 覆盖不如 Temporal；大规模多租户隔离要自己设计 |
| **DBOS** | 把工作流状态直接放进你已有的 Postgres，不走独立集群 | 持久层压力直接落在业务库上，**必须独立库/独立实例**，否则 5k QPS 会把业务库拖死 |
| **LangGraph** | `checkpointer` 抽象（内存/SQLite/Postgres），thread 级别的状态快照 | 它是「图执行器的存档」，不是通用工作流引擎：没有跨天的 timer、没有 activity 级重试策略、没有版本演进语义。**短 Run 够用，>3 分钟就该换** |
| **Kafka** | 至少一次投递 + 分区有序 + 重放 | 没有「步骤状态」，没有定时唤醒（需延迟队列），没有 fence；它只是传输层，不是执行层 |

> 🎯 **选型判据（可以直接抄进 ADR）**：
> - Run 步骤固定、无人工挂起、< 3 分钟 → **自研状态机 + checkpoint 表**（成本 ≈ 一张表 + 200 行 driver）
> - 有跨天挂起、有分支、需要可视化运维 → **Temporal**
> - 只是无状态扇出（发通知、写数仓） → **Kafka**
> - 已经重度依赖 Postgres 且不想加组件 → **DBOS 或自研**（二者本质相同）

---

## 12.7 事故清单

- [ ] **事故 A：重放时重新调了模型。** 现象：一次崩溃恢复后，账单多了 20%，且同一条工单两次判定结论不同。→ 根因：模型调用没被记进事件历史，重放走了「重新执行」分支。→ 兜底：模型调用一律 `output_ref` 落库 + `status=done` 短路；上线前用「强制 kill -9 后重放」做验收测试。
- [ ] **事故 B：两个 worker 同时推进同一 Run。** 现象：客户收到 2 封邮件，日志里两个 Pod 都在处理同一个 run_id。→ 根因：只有租约没有 fence_token，租约过期后旧 worker 仍能写。→ 兜底：所有写 checkpoint 的语句都带 `fence_token <= $n` 条件判断。
- [ ] **事故 C：整批任务从头重跑。** 现象：47 分钟的任务崩在第 45 分钟，重启后从第 1 分钟开始。→ 根因：没有步骤级进度，只有「任务整体成功/失败」。→ 兜底：checkpoint 粒度到「单个业务对象」，且按 run_id 分片可并行恢复。
- [ ] **事故 D：Temporal 历史撑爆。** 现象：`workflow history size exceeds limit`，工作流卡死不推进。→ 根因：activity 返回了完整模型输出（单次 200KB）。→ 兜底：activity 只返回 `{ref, digest, usage}`；长循环用 `ContinueAsNew`。
- [ ] **事故 E：非确定性错误，重放失败。** 现象：worker 重启后工作流报 `nondeterministic workflow`。→ 根因：工作流代码里用了 `time.Now()` 或直接遍历 Go map。→ 兜底：CI 里跑「重放回放测试」；所有时间/随机走 SDK API。
- [ ] **事故 F：补偿把好数据也回滚了。** 现象：一次失败触发了补偿，把之前已成功的 200 条一起撤了。→ 根因：补偿范围是整个批次而不是单个 Run。→ 兜底：补偿栈作用域 = 单个 run_id，永不跨 Run。
- [ ] **事故 G：挂起等审批把 worker 占满。** 现象：审批队列积压 3000 条，所有 worker 都在「等」，新任务全排队。→ 根因：用阻塞 sleep 实现挂起。→ 兜底：挂起必须落库 + 由 timer 唤醒（`workflow.NewTimer` 或 wait 表 + 扫描器）。

---

## 12.8 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 分档判据 | 重跑成本 > 人工兜底工时，或存在不可逆副作用 → 上工作流；否则别上 |
| checkpoint 粒度 | 到「单个业务对象、单个步骤」，不是「整个批次」 |
| 三张表 | `run_checkpoint`（快照）+ `run_lease`（租约）+ 事件历史（真相源） |
| fence_token | 租约过期后旧 worker 必须自己退场；没有 fence 的分布式锁等于没有锁 |
| 重放 ≠ 重算 | 命中 `status=done` 直接读结果；重算模型 = 重复付费 + 结论漂移 |
| 非确定性 | 默认策略是「记录重放」；温度 0 和 seed 只能降低方差，不能消除 |
| 补偿 | 只压成功步骤、逆序执行、作用域限于单 Run、补偿自身要幂等 |
| 确定性纪律 | 工作流代码里不许出现 `time.Now()` / `uuid` / 未排序 map |
| 挂起 | 等审批 72 小时不能占 goroutine/线程，必须持久化 + timer 唤醒 |
| 成本 | 一次 30s Run 约 5 次模型调用 ≈ 125k input token；Temporal 每次 activity 至少一次持久化写 |
| 一句话 | **「重跑一遍」在批处理里是廉价操作，在 Agent 里是昂贵且危险的操作。** |

---

## 12.9 自检题

1. **你的 Run 在第 7 步崩溃了，重启后怎么保证不会重复发邮件、也不会重复付模型钱？**
   > 要点：① 模型调用与工具调用的输出都以 `status=done` 落进 checkpoint（大对象存 S3 存引用），driver 重放时短路读结果，不重调；② 工具调用必须带幂等键 `(run_id, seq, input_hash)`，服务端去重；③ 写 checkpoint 时校验 `fence_token`，防止旧 worker 复活后重复推进；④ 验收方式是用 chaos 强制 `kill -9` 后观察账单与副作用次数，而不是靠 code review。

2. **什么情况下你会选择自研状态机而不是 Temporal？**
   > 要点：步骤固定（< 20 步）、无跨天人工挂起、无复杂分支；此时 Temporal 的运维成本（持久层集群 + 历史服务）和每次 activity 的持久化写不值得。自研只需一张 checkpoint 表 + 200 行 driver，可读可调。反过来：一旦出现「等审批 72 小时」「按条件跳回第 3 步」「需要可视化运维界面」，自研的复杂度会指数上升，就该换 Temporal。

3. **为什么说「把 temperature 设成 0 就能确定性重放」是错的？**
   > 要点：① 浮点累加顺序、推理批处理组成会随并发变化；② 模型厂商会静默滚动版本；③ 上下文里的时间戳、检索结果顺序、记忆写入时点都会变；④ 即使输出字面相同，`usage` 也可能不同（缓存命中与否）。所以确定性只能靠「记录输出、重放读取」拿到，不能靠参数。

---

## 12.10 延伸阅读

- 上一章：[11. 准入控制、排队与公平性](11-admission-and-queue.md)
- 下一章：[13. 事件驱动与异步管道](13-event-driven-pipeline.md)
- Temporal 官方文档：Workflow 确定性约束、Activity 重试策略、`GetVersion` 版本演进、`ContinueAsNew`（以官方最新版为准）
- Restate 官方文档：Virtual Object / Durable Execution
- DBOS 官方文档：Postgres-backed workflow
- 相关：第 6 章（事件溯源与状态模型）、第 8 章（Run API 幂等）、第 22 章（工具幂等键）、第 34 章（审批与中断恢复）、第 27 章（计费预扣结算）

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 语言/运行时细节：见 [../go/0-summary.md](../go/0-summary.md)
- 前置：第 11 章
- 下一章：第 13 章
