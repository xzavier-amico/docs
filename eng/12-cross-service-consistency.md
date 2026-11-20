# 12. 跨服务一致性实战：订单-库存-支付的编排

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · 事务与一致性 ｜ **编号**：12 / 52
> **本章定位**：用一条真实的订单-库存-支付链路，把「Saga 编排」和「事件协同」两套实现完整写出来，再用三次事故说明它们各自会在哪炸。补偿不是回滚，这一章把「可重复、可逆、可审计」落到表结构和代码上。
> **读法**：§12.1 的三次事故先对号入座；§12.4 两套实现选一套精读；§12.4.3 的对比表是架构评审时最有用的部分。

---

## 12.1 引子：同一条链路，三次事故

**事故一：库存扣了，订单没建（2025-03-05）**

下单链路：订单服务先调库存服务 `deduct`，再写订单库。库存服务成功返回后，订单库插入遇到唯一键冲突（网关重试导致同一 `request_id` 第二次进来），代码直接抛异常，**没有调用库存释放**。

- 1,860 笔订单库存被扣、订单不存在；62 个 SKU 库存为负。
- 4 小时后通过库存流水与订单表对账发现，人工回滚 1,860 条库存，赔付 **38 万元**。
- 根因不是「库存服务不可靠」，是**跨服务调用没有补偿**。

**事故二：支付成功，订单已关闭（2025-06-21）**

订单 30 分钟未支付自动关单。用户在 23:59:58 完成支付，渠道回调在 00:00:12 到达；关单任务在 00:00:10 已经把订单置为 `CLOSED`。回调处理器只接受 `status=CREATED`，看到 `CLOSED` 直接返回「订单已关闭」，**没有退款**。

- 420 笔订单「钱扣了、单关了」；用户投诉 380 起。
- 3 天内完成退款 126 万元，另有 42 笔跨月账务调整。
- 根因：**关单与支付回调之间没有互斥状态机**，且「关闭」不是终态。

**事故三：退款成功，库存没回（2025-08-14）**

退款服务完成退款后，在事务提交后发「退款成功」MQ；当天 MQ 发送超时，异常被吞。库存服务没收到事件，2,300 笔退货库存没有回补。

- 库存少记 2,300 件，热销 SKU 虚卖 11 天，超卖 640 单。
- 19 万元赔付 + 11 天运营手工盘库。
- 根因：**退款与事件不是同一个本地事务**（详见第 10 章）。

> 🩸 三次事故对应跨服务一致性的三个基本问题：**正向失败没补偿、状态冲突没互斥、事件发送没原子性。** 前两个靠 Saga/状态机，第三个靠 Outbox。

---

## 12.2 到底是什么：跨服务一致性 = 状态机 + 补偿 + 幂等

**定义**：跨服务一致性 = 一个业务事实需要多个服务各自写自己的库，且这些写操作要么都生效、要么都被补偿；由于没有全局事务，只能靠**每个服务的本地事务 + 一个推进流程的状态机 + 可重复的补偿**来收敛。

三个反例：

| 你以为的跨服务一致性 | 为什么不是 | 不做会怎样 |
| --- | --- | --- |
| 「接口返回成功 = 数据一致」 | 超时可能成功、可能失败；返回成功也可能只写了一半 | 库存扣了订单没建；支付成功订单关了 |
| 「补偿就是删除/回滚」 | 补偿是**新的业务事实**（退款、退库存、冲正），不是抹掉历史 | 删除流水导致审计断链；重复补偿导致多退 |
| 「Saga 能保证隔离性」 | Saga 没有全局锁，中间态对外可见 | 用户看到「已扣库存、待支付」，或读到半成品订单 |
| 「事件协同没有中心，所以更简单」 | 没有中心 = 没有全局视图；排查要靠 trace 拼 | 出事时不知道流程走到哪、该谁补偿 |

**两套实现范式的本质差异**：

| 维度 | Saga 编排（Orchestration） | 事件协同（Choreography） |
| --- | --- | --- |
| 谁决定下一步 | 中心编排器 | 各服务收到事件后自己决定 |
| 全局状态 | 编排器持有，天然可见 | 分散在各服务，需要拼 |
| 耦合 | 服务依赖编排器协议 | 服务只依赖事件契约 |
| 适合 | 步骤固定、需要可视化和人工干预 | 事件扇出、团队自治、流程简单 |
| 失败恢复 | 编排器重试/补偿，集中处理 | 每个订阅者自己重试/补偿，容易漏 |

> 💡 选择判据：**流程 >5 步、涉及资金、需要「现在走到哪」的运营视图 → 编排；只是通知/扇出、每个消费者独立 → 事件协同。** 两者可以混用：主流程编排，旁路事件协同。

---

## 12.3 结构图：正向链路与补偿链路

```
正向（Saga 编排版）：
┌──────────────────┐
│ OrderOrchestrator│  saga_instance(status=RUNNING)
└────────┬─────────┘
         │ 1. createOrder
         ▼
   ┌───────────┐   本地事务：orders + outbox(order.created)
   │ OrderSvc  │
   └─────┬─────┘
         │ 2. reserveStock
         ▼
   ┌───────────┐   本地事务：stock_reservation(FROZEN)
   │ StockSvc  │
   └─────┬─────┘
         │ 3. charge
         ▼
   ┌───────────┐   本地事务：payment + outbox(payment.succeeded)
   │ PaymentSvc│
   └─────┬─────┘
         │ 4. confirmStock / 5. markOrderPaid
         ▼
      SUCCESS

补偿（任一步失败，逆序）：
   charge 失败 → releaseStock → cancelOrder
   confirmStock 失败 → refund → releaseStock → cancelOrder
   每一步补偿都带 compensation_id，唯一索引防重复

事件协同版：
 OrderSvc ──order.created──► StockSvc（reserve）
     ▲                          │
     │                          ├──stock.reserved──► PaymentSvc（charge）
     │                          │                        │
     └──order.cancelled◄────────┴──stock.released◄──payment.failed
 OrderSvc ──order.closed──► PaymentSvc（refund if paid）
 StockSvc ◄──refund.succeeded── PaymentSvc（return stock）
```

---

## 12.4 怎么实现：编排版与协同版

### 12.4.1 编排版：状态表 + 中心编排器

先建两张表：`saga_instance` 存流程状态，`saga_step` 存每一步的执行/补偿状态。**没有这两张表，编排器重启后就不知道走到哪。**

```sql
CREATE TABLE saga_instance (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  saga_id       VARCHAR(64)     NOT NULL COMMENT '业务唯一 ID，如 order_no',
  saga_type     VARCHAR(64)     NOT NULL COMMENT 'ORDER_FULFILL',
  status        ENUM('RUNNING','SUCCEEDED','COMPENSATING','COMPENSATED','MANUAL') NOT NULL,
  current_step  VARCHAR(64)     NULL,
  payload       JSON            NOT NULL,
  retry_count   INT             NOT NULL DEFAULT 0,
  next_retry_at DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  created_at    DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at    DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_saga_id (saga_id),
  KEY idx_status_retry (status, next_retry_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE saga_step (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  saga_id       VARCHAR(64)     NOT NULL,
  step_name     VARCHAR(64)     NOT NULL,
  step_order    INT             NOT NULL,
  status        ENUM('PENDING','DONE','FAILED','COMPENSATED','COMPENSATION_FAILED') NOT NULL,
  request       JSON            NULL,
  response      JSON            NULL,
  compensation_id VARCHAR(192)  NULL COMMENT '补偿幂等键',
  retry_count   INT             NOT NULL DEFAULT 0,
  last_error    VARCHAR(512)    NULL,
  created_at    DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at    DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_saga_step (saga_id, step_name),
  KEY idx_status (status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

```java
// 中心编排器：每一步「先落状态，再调用；失败逆序补偿」
@Service
public class OrderSagaOrchestrator {
    @Resource private SagaInstanceMapper instanceMapper;
    @Resource private SagaStepMapper stepMapper;
    @Resource private OrderClient orderClient;
    @Resource private StockClient stockClient;
    @Resource private PaymentClient paymentClient;

    public void start(OrderCmd cmd) {
        SagaInstance saga = SagaInstance.start(cmd.orderNo(), cmd);
        instanceMapper.insertIgnore(saga);              // saga_id 唯一，天然幂等
        run(cmd.orderNo());
    }

    public void run(String sagaId) {
        // 短事务认领：UPDATE ... WHERE saga_id=? AND status='RUNNING' AND next_retry_at<=NOW(3)
        // 不要把数据库事务跨在远程调用上，否则又变成「XA 式长事务」
        if (!instanceMapper.tryClaim(sagaId)) return;
        try {
            step(sagaId, "createOrder", 1, () -> orderClient.create(sagaId));
            step(sagaId, "reserveStock", 2, () -> stockClient.reserve(sagaId));
            step(sagaId, "charge", 3, () -> paymentClient.charge(sagaId));
            step(sagaId, "confirmStock", 4, () -> stockClient.confirm(sagaId));
            instanceMapper.markSucceeded(sagaId);
        } catch (Exception e) {
            instanceMapper.markCompensating(sagaId, e.getMessage());
            compensate(sagaId);
        }
    }

    @FunctionalInterface
    interface StepAction { void run() throws Exception; }

    private void step(String sagaId, String name, int order, StepAction action) throws Exception {
        SagaStep existing = stepMapper.find(sagaId, name);
        if (existing != null && existing.getStatus() == DONE) return; // 重放不重复执行
        stepMapper.insertIgnore(sagaId, name, order, "PENDING");
        action.run();                                    // 调下游（下游必须幂等）
        stepMapper.markDone(sagaId, name);
    }

    private void compensate(String sagaId) {
        List<SagaStep> done = stepMapper.listDoneDesc(sagaId);
        for (SagaStep s : done) {
            try {
                compensateStep(sagaId, s);
            } catch (Exception e) {
                // 补偿失败：不吞，留在 COMPENSATION_FAILED，进重试/人工
                stepMapper.markCompensationFailed(sagaId, s.getStepName(), e.getMessage());
                instanceMapper.markManual(sagaId, e.getMessage());
                return;
            }
        }
        instanceMapper.markCompensated(sagaId);
    }

    private void compensateStep(String sagaId, SagaStep s) {
        String cid = sagaId + ":" + s.getStepName() + ":cancel";
        switch (s.getStepName()) {
            case "createOrder" -> orderClient.cancel(sagaId, cid);
            case "reserveStock" -> stockClient.release(sagaId, cid);
            case "charge" -> paymentClient.refund(sagaId, cid);
            case "confirmStock" -> stockClient.unconfirm(sagaId, cid);
            default -> throw new IllegalStateException("no compensation: " + s.getStepName());
        }
        stepMapper.markCompensated(sagaId, s.getStepName());
    }
}
```

**编排版的关键点**：

- `saga_instance.saga_id` 唯一，重复请求不会创建第二个流程。
- 每一步先 `insertIgnore` 再执行；`saga_step` 的 `DONE` 状态让重放跳过已完成步骤。
- 补偿逆序执行，`compensation_id` 固定（`sagaId:step:cancel`），下游用它做幂等。
- 补偿失败**不能吞**：状态置 `COMPENSATION_FAILED`，流程转 `MANUAL`，告警 + 人工工单。
- 编排器自身要能恢复：定时扫描 `RUNNING`/`COMPENSATING` 且 `updated_at` 超时的实例，重新 `run`/`compensate`。
- `tryClaim` 必须是短事务的条件更新（`WHERE status='RUNNING' AND next_retry_at<=NOW(3)`），**不能在整个 `run` 外面包一个大事务**——那会把远程调用锁进数据库事务，退化成 XA。

### 12.4.2 协同版：各服务订阅事件

协同版没有中心编排器，每个服务订阅自己关心的事件，用本地状态机决定「下一步做什么」。下面以库存服务为例（Go）：

```go
// 库存服务：订阅 order.created / order.cancelled / payment.failed / refund.succeeded
func (h *StockHandler) Handle(ctx context.Context, evt Event) error {
	switch evt.Type {
	case "order.created":
		return h.reserve(ctx, evt)
	case "order.cancelled", "payment.failed":
		return h.release(ctx, evt)
	case "refund.succeeded":
		return h.returnStock(ctx, evt)
	default:
		return nil
	}
}

func (h *StockHandler) reserve(ctx context.Context, evt Event) error {
	return h.repo.InTx(ctx, func(tx *sql.Tx) error {
		// 示例按单 SKU 订单；多 SKU 用 order_no + sku_id 做幂等键
		if ok, _ := h.repo.Exists(tx, evt.OrderNo, evt.SkuID); ok { return nil }
		if err := h.repo.Freeze(tx, evt.OrderNo, evt.SkuID, evt.Qty); err != nil {
			if errors.Is(err, ErrInsufficientStock) {
				// 业务失败也要留事件，让订单服务走补偿/取消；不能只返回错误
				return h.outbox.Insert(tx, Event{
					EventID: evt.OrderNo + ":stock.reserve_failed", AggregateID: evt.OrderNo,
					Type: "stock.reserve_failed",
					Payload: map[string]any{"sku_id": evt.SkuID, "qty": evt.Qty},
				})
			}
			return err
		}
		return h.outbox.Insert(tx, Event{
			EventID: evt.OrderNo + ":stock.reserved", AggregateID: evt.OrderNo,
			Type: "stock.reserved", Payload: map[string]any{"sku_id": evt.SkuID, "qty": evt.Qty},
		})
	})
}

func (h *StockHandler) release(ctx context.Context, evt Event) error {
	return h.repo.InTx(ctx, func(tx *sql.Tx) error {
		// 补偿幂等：RELEASED 状态只允许迁移一次
		return h.repo.UpdateStatus(tx, evt.OrderNo, "FROZEN", "RELEASED")
	})
}
```

**协同版的订单服务**：维护订单状态机 `CREATED → STOCK_RESERVED → PAID → CLOSED/REFUNDED`，并允许超时关单 `CREATED → CLOSED`、`STOCK_RESERVED → CLOSED`（同时释放库存）；订阅 `stock.reserved`、`payment.succeeded`、`payment.failed`，每次迁移用条件更新：

```sql
UPDATE orders
   SET status='STOCK_RESERVED', updated_at=NOW(3)
 WHERE order_no=? AND status='CREATED';
-- 影响 0 行：要么已经推进过，要么状态非法；查当前状态再决定
```

### 12.4.3 两套实现的对比

| 维度 | Saga 编排版 | 事件协同版 |
| --- | --- | --- |
| **可观测性** | 一张 `saga_instance` 就能回答「走到哪、卡在哪、补偿到哪」 | 要拼 4 个服务的状态 + trace；没有全局视图 |
| **可运维性** | 重跑/跳过/人工补偿都有统一入口 | 每个服务各自重试；漏订阅/漏补偿很难发现 |
| **失败恢复** | 编排器扫描超时实例，集中补偿 | 依赖每个消费者自己的重试和死信；恢复路径分散 |
| **耦合** | 服务依赖编排器协议和步骤顺序 | 服务只依赖事件契约，新增消费者不改发布方 |
| **延迟** | 多一次编排器调度；可并行优化 | 事件驱动，天然并行；但链路长时延迟叠加 |
| **适用** | 订单履约、支付、退款、跨机构转账 | 通知、积分、搜索索引、风控旁路 |

> 💡 生产建议：**主流程用编排，旁路用协同。** 订单履约用编排器保证「能看、能补偿、能人工」；积分、推荐、数仓用事件协同，失败不影响主流程。

### 12.4.4 补偿设计三原则：可重复、可逆、可审计

**原则一：可重复（Idempotent）**

补偿接口必须能被执行 N 次而结果一致。实现：`compensation_id` 唯一索引 + 状态机条件更新。

```sql
CREATE TABLE compensation_record (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  compensation_id VARCHAR(192)    NOT NULL COMMENT 'sagaId:step:cancel',
  saga_id         VARCHAR(64)     NOT NULL,
  step_name       VARCHAR(64)     NOT NULL,
  action          VARCHAR(64)     NOT NULL COMMENT 'REFUND/RELEASE/REVERSE',
  amount          DECIMAL(18,2)   NULL,
  status          ENUM('DONE','FAILED','MANUAL') NOT NULL,
  operator        VARCHAR(64)     NOT NULL DEFAULT 'system',
  reason          VARCHAR(256)    NOT NULL,
  created_at      DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_compensation (compensation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

**原则二：可逆（Reversible）**

「可逆」不是删除，而是**记一笔反向事实**：退款记退款单、退库存记入库流水、冲正记反向分录。原记录永不删除，审计链完整。

**原则三：可审计（Auditable）**

每次补偿记录：谁触发（system/operator）、为什么（超时/下游失败/人工）、金额多少、dry-run 结果。资金类补偿必须支持 dry-run + 审批，不能由代码静默执行。

```yaml
# 补偿策略：重试上限 + 退避 + 人工队列；资金类默认 dry-run
saga:
  compensation:
    max_attempts: 8
    backoff: ["5s", "30s", "2m", "10m", "30m", "1h", "2h", "6h"]
    manual_queue: compensation_manual
    dry_run_for_money: true
    require_approval_over: 10000   # 超过 1 万元必须人工审批
```

**补偿失败不是异常，是常态**。设计时就要假设「补偿也会失败」：重试上限、人工队列、对账兜底，三件套缺一不可。

---

## 12.5 双修对照：一次 Run 就是一条 Saga

| 维度 | 传统系统 | Agent 系统 | 同 or 异 |
| --- | --- | --- | --- |
| 流程单位 | 订单履约 / 支付链路 | **一次 Run**（多 Step、多工具、多子 Agent） | 同构，Agent 更长 |
| 状态机 | `saga_instance` + `saga_step` | `run` + `step` + `tool_call` 三层 | 同套路，多一层工具 |
| 正向失败 | 补偿已成功的步骤 | 补偿工具副作用（取消订单、删文件、发更正） | 同，但工具补偿更难逆 |
| 身份 / 唯一键 | `saga_id`、`compensation_id` | `run_id`、`step_id`、`tool_call_id` | 同构，键多一层 |
| 失败模式 | 空回滚、悬挂、补偿失败 | 上述全部 + **模型已返回但结果没落库**、工具已执行但 Step 失败 | 多一类不可回滚 |
| 恢复方式 | 重跑/补偿/人工 | **检查点 + 补偿 + 重新规划**；不能靠重放模型当回滚 | 异：路径不确定 |
| 验证方法 | 金额/库存对账 | 金额 + **token/调用次数对账** + 输出质量抽检 | 框架相同，口径扩展 |
| 成本结构 | 补偿一次退款/退库存 | 补偿可能再调一次模型/工具，**再花钱** | 异：成本显性 |

**哪些完全相同、可以直接复用**：`saga_instance/saga_step` 表结构、编排器重试/补偿循环、`compensation_id` 幂等、状态机条件更新、对账 SQL。**Agent 不需要新造一套编排框架。**

Agent 侧的三条额外纪律：

1. **模型调用不放进补偿链**：模型不可回滚，重放还可能产生不同结果；补偿只针对工具副作用。
2. **每个 Step 后写检查点**：`run_id + step_seq + state + tool_result`，崩溃后从检查点恢复，而不是从头重跑（从头重跑 = 重复花钱）。
3. **工具补偿要人工兜底**：删文件、撤消息、取消第三方订单，有些不可逆；不可逆的工具调用前必须审批或 dry-run。

```go
// Agent Run 的 Saga：正向步骤 + 工具补偿 + 检查点
type RunSaga struct {
	RunID  string
	Steps  []RunStep
	Repo   RunRepo
}

func (s *RunSaga) Execute(ctx context.Context) error {
	for _, step := range s.Steps {
		if done, _ := s.Repo.StepDone(ctx, s.RunID, step.Name); done {
			continue                                  // 检查点：已完成的步骤不重跑
		}
		if err := step.Do(ctx); err != nil {
			return s.Compensate(ctx, step.Name, err)  // 只补偿工具副作用
		}
		if err := s.Repo.SaveCheckpoint(ctx, s.RunID, step.Name, step.Result); err != nil {
			return err
		}
	}
	return nil
}
```

---

## 12.6 会在哪炸：失效模式清单

| 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 库存扣了订单没建 | 正向调用成功、后续失败、无补偿 | 库存流水 `LEFT JOIN orders` 为空 | 回滚库存 + 补单/取消 | 每个正向步骤配补偿；Saga 状态表 |
| 支付成功订单已关闭 | 关单与回调并发，状态机没有互斥 | 查 `orders.status=CLOSED` 且有支付成功流水 | 退款或重新开单；修状态机 | 关单条件更新 + 支付回调接受 `CLOSED` 并触发退款 |
| 退款成功库存没回 | 退款与事件不同事务，发送失败 | 退款流水 vs 库存入库流水 | 补发事件/补库存 | Outbox + 消费幂等（第 10 章） |
| 补偿重复退款 | 补偿接口不幂等 | 查 `compensation_record` 重复 | 追回多退；补唯一索引 | `compensation_id` 唯一 + 状态机 |
| 补偿失败后流程卡住 | 只重试 3 次就放弃，没有人工队列 | 查 `saga_instance.status=MANUAL` | 人工补偿 + 工单 | 重试上限 + 告警 + 人工入口 |
| 事件乱序导致状态回退 | 消费者不看版本/状态 | 查状态迁移日志 | 条件更新 + 版本号 | 按 aggregate 分区 + 拒绝旧版本 |
| 编排器重启后重复执行 | 没有 `saga_step` 状态 | 查下游重复调用 | 补状态表；下游幂等 | 先落状态再调用 |

---

## 12.7 怎么验证：杀进程、造差异、对账

**① 故障注入**：在「库存预留成功、支付调用前」`kill -9` 编排器，重启后确认：Saga 从 `reserveStock` 之后继续，不会重复预留；若流程已进入补偿，库存被释放且只释放一次。

**② 对账 SQL（每天跑）**：

```sql
-- ① 库存已扣、订单不存在
SELECT f.order_no, f.sku_id, f.qty, f.created_at
  FROM inventory_flow f
  LEFT JOIN orders o ON o.order_no = f.order_no
 WHERE f.flow_type='DEDUCT' AND o.id IS NULL;

-- ② 订单已支付、库存未扣
SELECT o.order_no, o.paid_at
  FROM orders o
  LEFT JOIN inventory_flow f
    ON f.order_no = o.order_no AND f.flow_type='DEDUCT'
 WHERE o.status='PAID' AND f.id IS NULL;

-- ③ 支付成功、订单关闭
SELECT p.payment_no, p.order_no, p.amount, p.paid_at, o.status
  FROM payments p
  JOIN orders o ON o.order_no = p.order_no
 WHERE p.status='SUCCESS' AND o.status='CLOSED';

-- ④ 退款成功、库存未回补
SELECT r.refund_no, r.order_no, r.amount
  FROM refunds r
  LEFT JOIN inventory_flow f
    ON f.order_no = r.order_no AND f.flow_type='RETURN'
 WHERE r.status='SUCCESS' AND f.id IS NULL;
```

**③ 观测指标**：

| 指标 | 含义 | 告警线 |
| --- | --- | --- |
| `saga_running_age_seconds` | 流程停留时长 | > 业务 SLA（如 5 分钟） |
| `saga_compensation_total{result}` | 补偿成功/失败 | 失败 10 分钟 > 0 |
| `saga_manual_queue_size` | 人工队列长度 | > 10 |
| `order_paid_no_stock` | 已支付未扣库存 | > 0 |
| `payment_success_order_closed` | 支付成功订单关闭 | > 0 |
| `refund_no_return` | 退款未回库存 | > 0 |

**④ 压测**：2 倍峰值下，Saga 成功率 ≥ 99.99%，补偿率 < 0.05%，人工队列 < 10；重点看「编排器调度延迟」和「下游超时率」，不是只看 QPS。

---

## 12.8 用什么工具

| 需求 | 工具 | 边界 |
| --- | --- | --- |
| 代码即流程、持久化执行 | Temporal / Cadence | 学习成本高；Activity 必须幂等；适合长流程 |
| 状态机式 Saga | Seata Saga | 配置状态机 DSL；复杂分支不如 Temporal |
| BPMN 审批流 | Flowable / Camunda | 适合人工审批；高并发交易链路不建议 |
| 自研编排 | 状态表 + 定时扫描 | 可控、轻量；重试/补偿/可视化都要自己写 |
| 事件协同 | Kafka/RocketMQ + Outbox | 扇出方便；全局可观测性差 |
| 对账 | 自研 + ClickHouse | 最后一道防线（第 14 章） |

> ⚠️ 框架能力的边界：Temporal 帮你「持久化执行、重试、定时器」，**不帮你写补偿、不帮你做业务幂等、不帮你对账**。Saga 的难点从来不是「怎么调下一步」，是「失败后怎么退」。

---

## 12.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 跨服务一致性 | 本地事务 + 状态机 + 补偿 + 幂等，没有全局事务 |
| 编排 vs 协同 | 主流程编排（可观测/可运维），旁路协同（解耦/扇出） |
| 三次事故 | 库存扣了订单没建、支付成功订单关闭、退款成功库存没回 |
| 补偿三原则 | 可重复（唯一键）、可逆（反向事实）、可审计（谁/为什么/多少） |
| 补偿不是回滚 | 退款、退库存、冲正都是新事实；原记录不删除 |
| 状态机 | 条件更新 + 版本号；关单/支付回调必须互斥 |
| 编排器恢复 | 扫超时实例 + `saga_step` 状态 + 逆序补偿 |
| Agent 侧 | 一次 Run = 一条 Saga；Step 检查点；模型不可回滚；token 对账 |
| 不做会怎样 | 部分成功永远歪着；补偿重复多退；出事不知道走到哪 |
| 一句话 | **跨服务一致性不是「让两个库同时提交」，是「让流程可恢复、可补偿、可对账」。** |

## 12.10 自检题

1. **订单服务调库存成功、写订单失败，Saga 应该怎么补偿？如果补偿也失败呢？**
   <details><summary>答案要点</summary>
   逆序补偿：释放库存（`compensation_id=orderNo:reserveStock:release`，唯一索引幂等），再取消订单（如果有）。补偿失败不能吞：置 `COMPENSATION_FAILED`、流程转 `MANUAL`、告警 + 人工工单；同时用库存流水对账兜底。
   </details>

2. **支付回调到达时订单已关闭，正确处理是什么？**
   <details><summary>答案要点</summary>
   不能只返回「订单已关闭」。应把「已支付 + 已关闭」视为冲突状态：要么自动退款（原路退回），要么重新开单（库存允许时），并记录补偿流水；关单任务与支付回调都用条件更新，`CLOSED` 不是不可逆终态，要允许 `CLOSED → REFUNDING`。
   </details>

3. **Saga 编排版和事件协同版，什么时候选哪个？**
   <details><summary>答案要点</summary>
   流程长、涉及资金、需要全局视图和人工干预 → 编排；事件扇出、消费者独立、失败不影响主流程 → 协同。生产上常混用：订单履约编排，积分/搜索/数仓协同。协同版必须补全局 trace 和对账，否则出事找不到流程。
   </details>

## 12.11 延伸阅读

- Temporal 文档：*Workflows*、*Activities*、*Retries*、*Compensation* 模式
- Seata Saga 文档：状态机 DSL 与补偿服务
- Chris Richardson《Microservices Patterns》第 4 章：Saga 与补偿事务
- 上一章：[11. Exactly-once 的真相：只有端到端幂等](11-exactly-once-truth.md)
- 下一章：[13. Saga 三连坑：空回滚、悬挂、幂等](13-saga-pitfalls.md)
- 交叉引用：Outbox 见第 10 章；Saga 三坑见第 13 章；状态机落库见第 30 章；对账见第 14 章
