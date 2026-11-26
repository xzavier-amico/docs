# 25. 资金订单状态机：TCC、Saga 与事务 Outbox 的生产级收敛

> **状态**：生产级工程篇｜**所属**：第三部分·金融工程代码基座｜**编号**：25 / 48
>
> **本章定位**：承接 [24 金融级全链路幂等体系](24-end-to-end-financial-idempotency.md) 的稳定身份、请求指纹、`UNKNOWN` 和渠道查单，进一步把“资金订单允许做什么”落实为显式状态机，把跨服务扣款/冻结/解冻拆成 TCC 与 Saga 可证明的边界，并用事务 Outbox 解决数据库提交与消息发布之间的断裂。本文覆盖资金订单状态机、TCC Try/Confirm/Cancel 的防空回滚/防悬挂/幂等、Saga 补偿边界、Outbox 并发跳号乱序、PostgreSQL DDL、Go 1.24 与 Java 21 核心代码、超时 `UNKNOWN`、巡检与安全补偿、测试压测、极端边界、术语、面试答题卡和自检题。
>
> **不可妥协的红线**：状态转移必须由白名单和版本条件控制；状态不是事实账本，资金变化必须落不可变分录；TCC 的三个分支都必须幂等，且 `Cancel` 不能覆盖已成功的 `Confirm`；Saga 只补偿已完成的业务动作，不能把“未执行”当成“需要冲正”；Outbox 与业务事实同库同事务提交，发布者只能至少一次投递，消费者必须幂等；业务序号只在提交事实时分配，不能把自增 ID 当作事件顺序；超时只能得到 `UNKNOWN`，不能猜成失败或成功。

---

## 25.1 事故引子：状态显示成功，钱却被释放两次

### 25.1.1 典型时间线

某支付平台将“支付订单”“库存冻结”“账本扣款”分散在三个服务。订单服务用 `UPDATE payment SET status = 'SUCCESS'` 作为流程推进，消息通过应用线程直接发送。渠道在扣款后连接断开，随后发生了如下事故：

| 时间 | 事件 | 系统行为 | 后果 |
|---|---|---|---|
| 10:00:00.000 | 创建支付订单 | `INIT -> ACCEPTED` | 正常 |
| 10:00:00.020 | Try 冻结余额成功 | 只写 Redis 标志，未写冻结事实 | 事实与锁分离 |
| 10:00:00.030 | 事务协调器超时 | 认为 Try 失败，调用 Cancel | Cancel 释放了尚未持久化的冻结 |
| 10:00:00.040 | 原 Try 重试到达 | 再次冻结，未检查悬挂标记 | 同一订单出现两次冻结 |
| 10:00:00.300 | 渠道已扣款，响应丢失 | 订单被标记 `FAILED` | 真实状态被错误覆盖 |
| 10:00:01.000 | 客户重试生成新订单 | 新订单再次调用渠道 | 可能二次扣款 |
| 10:00:03.000 | 迟到 Confirm 到达 | 服务只看当前 `FAILED`，拒绝 Confirm | 渠道与平台分裂 |
| 次日 | 对账发现一笔渠道成功 | 运维手工改订单为成功 | 无法证明账本是否已入账 |

根因不是“锁不够多”，而是把以下不同概念混为一谈：

1. **业务状态**：订单当前允许哪些动作，例如 `PROCESSING` 是否能接收渠道成功回执。
2. **参与者状态**：余额冻结、库存预留、渠道扣款各自的 Try/Confirm/Cancel 结果。
3. **经济事实**：不可变的借贷分录、渠道流水和补偿流水。
4. **消息事实**：某一业务提交后应发布的事件，以及它是否已被投递。
5. **观测结果**：请求是否收到响应；超时不等于业务失败。

### 25.1.2 状态覆盖更新为什么危险

```sql
-- 反模式：没有前置状态、版本、操作者和转移原因
UPDATE payment_order SET status = 'SUCCESS' WHERE order_id = :id;
```

这行 SQL 可能覆盖并发中的 `REFUNDING`，也可能把已经 `CANCELED` 的旧订单重新变成成功。更严重的是，无法回答“谁在什么版本上、基于哪一个回执、经过哪条白名单转移”。正确做法是：

```text
命令（command）
  → 校验当前状态、版本、业务前置条件
  → 同事务追加状态转移（transition）和经济事实（若有）
  → 同事务写 Outbox
  → 提交后异步驱动外部副作用
```

### 25.1.3 本章的唯一真问题

> **在重复命令、乱序消息、参与者超时、协调器崩溃、数据库提交成功但发布失败、回执丢失和人工介入下，能否保证一笔资金订单最多产生一个经济结果，并最终把所有参与者收敛到一个可解释的终态？**

不能承诺网络世界“只执行一次”。能承诺的是：同一业务身份只能接受一次经济事实；每个状态转移有严格前置条件；每个外部动作有可查询、可重试或可人工终止的边界。

---

## 25.2 资金订单状态机：状态、事件、命令与不变量

### 25.2.1 四种东西必须分开

| 对象 | 例子 | 是否可重放 | 是否代表钱已变动 |
|---|---|---:|---:|
| 命令 `Command` | `SubmitPayment`、`CancelPayment` | 是 | 否，意图而已 |
| 事件 `Event` | `PaymentAccepted`、`ChannelSucceeded` | 是 | 可能描述已发生事实 |
| 状态 `State` | `PROCESSING`、`UNKNOWN` | 是，读模型可重建 | 不一定 |
| 经济事实 `Fact` | 借贷分录、银行扣款流水 | 只追加 | 是 |

状态机的数据库字段是当前快照，转移日志是审计证据，账本分录才是资金事实。三者必须能由 `order_id/operation_id` 关联，但不能相互替代。

### 25.2.2 推荐支付订单状态

状态要表达**下一步允许做什么**，而不是表达所有历史。以下是一组可落地的最小状态：

```text
INIT
  └─ ACCEPT ───────────────► ACCEPTED
                               ├─ START ───────► PROCESSING
                               └─ CANCEL ──────► CANCELED

PROCESSING
  ├─ ALL_CONFIRMED ────────► SUCCEEDED
  ├─ VERIFIED_REJECT_WITH_PRIOR_FACTS ─► COMPENSATING ─► FAILED
  ├─ TIMEOUT_UNKNOWN ──────► UNKNOWN
  └─ VERIFIED_REJECT_WITH_NO_PRIOR_FACTS ─────────────► FAILED

UNKNOWN
  ├─ QUERY_SUCCESS ────────► SUCCEEDED
  ├─ VERIFIED_NOT_EXECUTED + NO_IN_FLIGHT ─► COMPENSATING ─► FAILED
  ├─ QUERY_PENDING / 证据不足 ─────────────► UNKNOWN
  └─ MANUAL_TERMINATE ─────► TERMINATED

SUCCEEDED
  └─ REFUND ───────────────► REFUNDING ───► REFUNDED
```

`UNKNOWN` 是一个有 SLA、有查询策略、有人工边界的业务状态，不是错误码；人工审批也不能替代“未执行且无在途”的证据。若查到扣款成功，应先归并成功事实；需要退回时另建退款，而非直接解冻。`TERMINATED` 只表示平台停止自动推进，不表示渠道没有副作用。成功付款后退款必须是新 `operation_id`，禁止把原订单从 `SUCCEEDED` 改成 `FAILED`。

### 25.2.3 转移白名单和不变量

设订单当前状态为 `s`，命令为 `c`，参与者摘要为 `p`，新状态为 `s'`。转移只在策略函数成立时提交：

```text
allow(s, c, p, version) = true
  且 UPDATE ... WHERE order_id = ? AND state = s AND version = version
  影响行数 = 1
```

核心不变量：

1. **单调终态**：`SUCCEEDED`、`REFUNDED`、`TERMINATED` 不能被普通重试改回处理中；纠正只能创建新操作或追加冲正。
2. **乐观版本**：同一订单的两个转移不能都在同一个旧 `version` 上成功。
3. **事件前置**：`ChannelSucceeded` 只有在渠道引用、金额、币种、签名和幂等键校验通过后，才能驱动成功转移。
4. **参与者闭合**：进入 `SUCCEEDED` 前，所有必需参与者必须是 `CONFIRMED`；进入 `FAILED` 前，已完成参与者必须已补偿。补偿结果未知时保持 `COMPENSATING` 并记录步骤 `COMPENSATION_UNKNOWN`，不能宣称资金已释放。
5. **事实先于投影**：订单状态可重建，账本与渠道事实不能由“看起来正确的状态”推导出来。
6. **金额不变**：状态重试不允许改变金额、币种、收款人、费率版本和渠道幂等键；修改必须创建新业务身份。

### 25.2.4 状态转移表

| 当前状态 | 事件/命令 | 前置条件 | 下一状态 | 副作用 |
|---|---|---|---|---|
| `INIT` | `Accept` | 请求指纹首次绑定 | `ACCEPTED` | 写订单、Outbox |
| `ACCEPTED` | `Start` | 风控通过、未过期 | `PROCESSING` | 发布执行命令 |
| `PROCESSING` | `ParticipantConfirmed` | 全部必需参与者成功 | `SUCCEEDED` | 追加入账/成功事件 |
| `PROCESSING` | `ParticipantRejected` | 权威证明当前步未执行、无在途，且已完成前步可补偿 | `COMPENSATING` | 发布补偿命令 |
| `PROCESSING` | `DeadlineExceeded` | 查单窗口未闭合 | `UNKNOWN` | 冻结自动重试，进入巡检 |
| `UNKNOWN` | `QuerySuccess` | 外部引用可核验 | `SUCCEEDED` | 仅一次入账 |
| `UNKNOWN` | `VerifiedNotExecuted` | 权威证明未执行、无在途调用/可发送命令 | `COMPENSATING` | 仅补偿已知完成前步 |
| `COMPENSATING` | `Compensated` | 所有必需补偿有成功事实 | `FAILED` | 追加失败事件 |
| `SUCCEEDED` | `Refund` | 退款金额不超过未退金额 | `REFUNDING` | 创建退款操作 |

“前置条件”必须在同一事务内依据持久化事实检查。不要先在应用层 `SELECT` 判断，再隔离连接执行 `UPDATE`。

---

## 25.3 TCC：Try、Confirm、Cancel 的正确边界

### 25.3.1 TCC 不是三次普通 RPC

TCC 把一个参与者的业务动作拆成：

- **Try**：预留资源、校验条件、写可确认的中间事实；不能产生最终不可逆的外部经济结果。
- **Confirm**：把预留转为最终成功；必须在 Try 成功后执行，且可被重复调用。
- **Cancel**：释放 Try 预留；只对确认未成功的资源执行，且可被重复调用。

例如余额账户支付：

```text
Try：AVAILABLE - 100，FROZEN + 100，reservation=R1
Confirm：FROZEN - 100，POSTED 扣款分录 + 账本幂等键 pay:R1:confirm
Cancel：FROZEN - 100，AVAILABLE + 100，reservation=R1
```

若 Try 阶段就把钱转给商户或调用不可撤回的银行扣款，后面的 Cancel 不再是补偿，而是另一笔退款；这不符合 TCC 的资源预留语义，应改成 Saga 或显式 `UNKNOWN` 的外部支付流程。

### 25.3.2 三类故障：空回滚、防悬挂、幂等

#### 空回滚（empty rollback）

协调器在 Try 请求丢失或超时后直接调用 Cancel。参与者从未成功 Try，但 Cancel 到达。若 Cancel 直接执行“释放 100”，可能凭空增加可用余额。

**防护**：以 `(global_tx_id, branch_id)` 持久化分支记录。Cancel 先插入“Cancel 已见”标记；若没有 Try 成功记录，只记录取消意图，不产生释放分录。

#### 防悬挂（hanging/suspension）

Try 请求因网络延迟，在 Cancel 之后才到达。若 Try 不检查取消标记，就会在事务已经决定取消后又冻结资源。

**防护**：Try 在创建/更新分支前锁定分支行或利用唯一键检查 `cancel_seen`；已见 Cancel 时只返回幂等成功，不执行预留。

#### 幂等（idempotency）

协调器会重试 Try、Confirm、Cancel；响应丢失时参与者也无法知道协调器是否收到结果。每个分支必须按 `(global_tx_id, branch_id, action)` 唯一化，并返回此前结果。

### 25.3.3 TCC 分支状态与合法动作

```text
NEW
 ├─ TrySuccess ─► TRIED ─► ConfirmSuccess ─► CONFIRMED
 │                   └────► CancelSuccess ───► CANCELED
 ├─ CancelSeen ─► CANCELED_EMPTY
 └─ TryReject ─► REJECTED
```

推荐约束：

| 已持久化事实 | 再来 Try | 再来 Confirm | 再来 Cancel |
|---|---|---|---|
| 无记录 | 创建 Try 记录并执行 | 创建“Confirm 先到”标记或拒绝，不能直接扣款 | 写空回滚标记，不释放 |
| `TRIED` | 返回原结果 | 执行一次确认 | 执行一次取消 |
| `CONFIRMED` | 返回已确认 | 返回原结果 | 返回已确认，禁止反向释放 |
| `CANCELED` | 返回已取消，禁止悬挂 | 返回冲突/进入人工 | 返回原结果 |
| `CANCELED_EMPTY` | 返回已取消，禁止 Try | 返回冲突/人工 | 返回原结果 |

`Confirm` 先到是实现选择点：可以安全记录 `confirm_seen` 后等待 Try，或直接拒绝让协调器重试；绝不能跳过 Try 直接创造资金事实。高价值资金建议拒绝并报警，因为“协调器认为 Try 成功”不等于参与者有可确认的预留。

### 25.3.4 TCC 伪代码：同事务锁定与事实写入

```go
// Go 1.24 风格；核心是事务边界，不是框架 API。
func (s *AccountParticipant) Try(ctx context.Context, txID, branchID string, uid int64, amt int64) error {
	return s.db.WithTx(ctx, func(ctx context.Context, tx *sql.Tx) error {
		resourceKey := fmt.Sprintf("account:%d:USD", uid)
		if err := insertBranchIfAbsent(ctx, tx, txID, branchID, resourceKey, "NEW", false); err != nil { return err }
		b, err := loadBranchForUpdate(ctx, tx, txID, branchID)
		if err != nil { return err }
		if b.ResourceKey != resourceKey { return ErrBranchConflict }
		if b.CancelSeen || b.State == "CANCELED" || b.State == "CONFIRMED" {
			return branchReplayOrConflict(b, "TRY")
		}
		if b.State == "TRIED" { return nil } // 幂等重放

		// 资源检查、扣减和 reservation 事实在同一事务、同一锁序中完成。
		bal, err := lockBalance(ctx, tx, uid, "USD")
		if err != nil { return err }
		if bal.Available < amt { return ErrInsufficientFunds }
		if err := moveAvailableToFrozen(ctx, tx, uid, "USD", amt); err != nil { return err }
		if err := insertReservation(ctx, tx, txID, branchID, uid, amt); err != nil { return err }
		return markBranchTried(ctx, tx, txID, branchID)
	})
}

func (s *AccountParticipant) Cancel(ctx context.Context, txID, branchID, resourceKey string) error {
	return s.db.WithTx(ctx, func(ctx context.Context, tx *sql.Tx) error {
		if err := insertBranchIfAbsent(ctx, tx, txID, branchID, resourceKey, "CANCELED_EMPTY", true); err != nil { return err }
		b, err := loadBranchForUpdate(ctx, tx, txID, branchID)
		if err != nil { return err }
		if b.ResourceKey != resourceKey { return ErrBranchConflict }
		if b.State == "CONFIRMED" { return nil } // 不能反向释放已确认事实
		if b.State == "CANCELED" || b.State == "CANCELED_EMPTY" { return nil }
		if b.State == "TRIED" {
			if err := releaseReservation(ctx, tx, txID, branchID); err != nil { return err }
		}
		return markBranchCanceled(ctx, tx, txID, branchID)
	})
}
```

这里的 `insertBranchIfAbsent` 必须使用 `INSERT ... ON CONFLICT (global_tx_id, branch_id) DO NOTHING`，五个列为 `global_tx_id, branch_id, resource_key, state, cancel_seen`，随后用独立语句 `SELECT ... FOR UPDATE` 重读，具体 SQL 见 25.6.3。不能捕获普通 INSERT 的唯一键异常后在已 aborted 的 PostgreSQL 事务中继续 SELECT。Try 和 Cancel 都必须先走此入口；Cancel 的真实 `resourceKey` 来自已认证的分支路由参数，不以 `"unknown"` 冒充资源身份。生产代码还要处理数据库死锁退避、Try 指纹中的金额/币种校验、分支租约和 Confirm 先到策略。

### 25.3.5 TCC 什么时候不该用

不要因为框架支持 TCC 就把所有流程包装成 TCC：

- 已经发送银行扣款、链上交易或短信等不可撤销副作用时，不能假装 Cancel 能回滚；
- 长时间人工审核、跨日清算不适合持有数据库锁或长事务；
- 参与者不能可靠持久化分支状态、没有幂等键或没有查单能力时，TCC 只会隐藏风险；
- 资源预留本身不存在，例如读取汇率、生成通知，不应伪造 Try/Confirm。

这类流程应使用 Saga 或“Outbox + 外部状态机 + UNKNOWN + 对账”。

---

## 25.4 Saga：补偿边界不是数据库回滚

### 25.4.1 Saga 的语义

Saga 是一串本地事务 `T1, T2, ..., Tn`。若 `T1...Tk` 已成功，而 `T{k+1}` 有权威证据证明未执行且不存在在途调用/待发命令，才可按逆序执行已完成步骤的补偿 `Ck...C1`；超时或普通 `FAILED` 返回码本身不是此证据：

```text
T1: 创建支付订单       C1: 关闭未结算订单/发起退款
T2: 冻结余额           C2: 解冻余额
T3: 锁定优惠额度       C3: 归还优惠额度
T4: 渠道扣款           C4: 退款或人工对账（不一定可自动）
```

Saga 只保证业务流程最终尝试收敛，不保证中间没有观察到部分成功，也不保证每个动作有真正的逆操作。每一步必须声明：完成事实、补偿动作、补偿幂等键、补偿的最大金额/范围、超时和人工边界。

### 25.4.2 补偿矩阵

| 原动作 | 是否可自动补偿 | 补偿动作 | 风险边界 |
|---|---:|---|---|
| 建单 | 是 | 关闭/作废订单 | 不能删除审计事实 |
| 余额冻结 | 有条件 | 原 reservation 解冻 | 只解冻未消耗的预留；后续扣款 UNKNOWN/在途时禁止解冻 |
| 账本确认扣款 | 通常是 | 追加冲正分录或退款操作 | 冲正不是修改原分录 |
| 银行扣款 | 取决于渠道 | 原路退款/查单 | 退款可能再次超时，进入 UNKNOWN |
| 链上转账 | 否（原交易不可撤回） | 新交易退回 | 需要收款方、手续费、确认数和人工边界 |
| 发送邮件/SMS | 否 | 发送更正通知 | 不能让用户“未收到”成为资金回滚依据 |

补偿的前提不是“流程失败”，而是**原动作已提交且确实产生了可补偿事实**。消息重复可能让协调器误以为某步成功，因此每个步骤都需要业务事实查询，而不是只相信 RPC 返回码。

### 25.4.3 编排式与协同式 Saga

- **编排式（Orchestration）**：一个 Saga Coordinator 保存步骤、超时、重试、补偿策略。适合资金流程，审计路径集中，但协调器是高价值状态机，必须持久化和高可用。
- **协同式（Choreography）**：参与者通过事件互相触发下一步。耦合低，但流程可视性、补偿顺序、循环事件和版本兼容更难控制。

支付、提现、清算这类高价值流程优先使用持久化编排器；低价值通知、营销积分可采用事件协同。无论哪种方式，不能依赖内存中的“当前步骤”。

### 25.4.4 Saga 步骤状态

```text
PENDING -> RUNNING -> SUCCEEDED
                    ├-> FAILED
                    └-> UNKNOWN

UNKNOWN -> 查单/对账（持久化等待，不 execute、不补偿、不推进后步）
  ├-> SUCCEEDED                         # 权威成功事实，恢复正向流程
  ├-> FAILED                            # 权威未执行且无在途
  └-> UNKNOWN / MANUAL                  # 仍无充分证据

FAILED + 权威未执行证据 + 无在途 -> Saga COMPENSATING
  └-> 已知 SUCCEEDED 前步：COMPENSATING -> COMPENSATED
                                       └-> COMPENSATION_UNKNOWN -> MANUAL
```

上图的 `FAILED` 是当前执行步骤失败，`COMPENSATING` 是 Saga 模式；不是“对失败/未知步骤本身执行补偿”。`UNKNOWN` 必须先持久化结果、原幂等键、外部引用和查证任务并停止；重启恢复也不能对它再次 `execute`。已持久化 `RUNNING` 的步骤在执行者崩溃后同样按 UNKNOWN 查证，而不是重置 PENDING。即使外部支持同键提交，也只能由显式恢复协议证明安全后授权，不能让普通 run 自动重发。

“无在途”包括已领取的 worker、排队命令和可能晚到的渠道请求；本地 lease 过期或查询 `NOT_FOUND` 不能证明这些请求不会生效。需要取消/栅栏与下游终局证据闭合。否则冻结可能已被未知扣款消耗，不能释放。补偿也有 UNKNOWN，不能因为“退款接口超时”就把订单标为已退款。

### 25.4.5 Java 21 Saga 编排核心代码

```java
public record StepResult(Status status, String externalRef) {
    enum Status { SUCCEEDED, FAILED, UNKNOWN }
}

public interface SagaStep {
    String name();
    StepResult execute(SagaContext c);      // 必须使用 operationId + stepKey 幂等
    StepResult compensate(SagaContext c);   // 只补偿已确认完成的动作
    boolean canCompensate();
}

public final class PaymentSaga {
    private final SagaRepository repo;
    private final List<SagaStep> steps;

    public void run(UUID operationId) {
        // 短事务领取带 fence 的协调权，不持有数据库锁跨越外部调用。
        SagaContext c = repo.claimRun(operationId);
        if (c == null) return; // 终态、查证等待或已被其他协调器领取
        if (repo.isCompensating(operationId)) {
            compensateReverse(operationId, c, repo.confirmedCompletedSteps(operationId, steps));
            return; // 恢复补偿模式，绝不回到正向 execute
        }
        List<SagaStep> completed = new ArrayList<>();
        for (SagaStep step : steps) {
            StepState old = repo.stepState(operationId, step.name());
            if (old == StepState.SUCCEEDED) { completed.add(step); continue; }
            if (old == StepState.UNKNOWN || old == StepState.RUNNING) {
                repo.persistUnknownAndEnqueueQuery(operationId, step.name(), c);
                return; // 包含崩溃恢复；既不重 execute，也不释放前步冻结
            }
            StepResult r;
            if (old == StepState.FAILED) {
                r = repo.savedResult(operationId, step.name()); // 不重 execute
            } else if (old == StepState.PENDING) {
                // 先提交 RUNNING 再调用；超时/异常转 UNKNOWN 并持久化结果。
                r = repo.runIdempotently(operationId, step, c);
            } else {
                throw new IllegalStateException("not an executable step: " + old);
            }
            if (r.status() == StepResult.Status.SUCCEEDED) {
                completed.add(step);
                continue;
            }
            if (r.status() == StepResult.Status.UNKNOWN) {
                repo.persistUnknownAndEnqueueQuery(operationId, step.name(), c);
                return;
            }
            // 原子检查持久化权威未执行证据、无在途、fence，再切换补偿模式。
            if (!repo.enterCompensatingIfSafe(operationId, step.name(), c)) {
                repo.persistUnknownAndEnqueueQuery(operationId, step.name(), c);
                return; // 普通 FAILED 返回码不足以授权补偿
            }
            compensateReverse(operationId, c, completed);
            return;
        }
        repo.markSucceeded(operationId);
    }

    private void compensateReverse(UUID id, SagaContext c, List<SagaStep> done) {
        for (var i = done.size() - 1; i >= 0; i--) {
            SagaStep step = done.get(i);
            if (!step.canCompensate()) {
                repo.openManualCase(id, step.name(), "NO_AUTOMATIC_COMPENSATION");
                return; // 阻断依赖链，不能越过此步释放更早的资源
            }
            StepResult r = repo.compensateIdempotently(id, step, c);
            if (r.status() != StepResult.Status.SUCCEEDED) {
                repo.openManualCase(id, step.name(), "COMPENSATION_" + r.status());
                return; // 结果未闭合，不继续逆序补偿
            }
        }
        repo.markFailedAfterAllCompensated(id, c);
    }
}
```

这里的 Repository 是协议伪代码，不是可直接运行的框架实现。`runIdempotently` 和 `compensateIdempotently` 不能只是 JVM 内的缓存；应在数据库中以 `(operation_id, step_key, action)` 唯一约束保存结果、外部引用、请求指纹和重试次数，结果提交成功后才能返回。补偿恢复应重放 COMPENSATED 原结果；已有 RUNNING/COMPENSATION_UNKNOWN 时只查证，不重新调用补偿。

`persistUnknownAndEnqueueQuery` 须在短事务中同时持久化 EXECUTE 步骤 UNKNOWN、订单 UNKNOWN 和查证 Outbox；无法持久化时退出，保留 RUNNING 供恢复查证。查证器只能依据权威证据把 EXECUTE 更新为 SUCCEEDED 或“有未执行证据的 FAILED”。`enterCompensatingIfSafe` 在同一事务中校验原请求身份、证据、无在途及协调器 fence，再写订单 COMPENSATING 与补偿意图；证据和 fence 可作为生产扩展列，最小 DDL 中的 `result_json` 至少保存证据引用，不能只存一个布尔值。补偿异常/超时须持久化 COMPENSATION_UNKNOWN 与工单；所有必需补偿成功前不得置订单 FAILED。

---

## 25.5 事务 Outbox：原子提交，非原子发布

### 25.5.1 为什么不能“先提交再发消息”或“先发消息再提交”

```text
先 DB commit，再 MQ publish：commit 成功、进程崩溃 → 永久丢消息
先 MQ publish，再 DB commit：publish 成功、DB 回滚 → 消费者看到幽灵事件
```

事务 Outbox 把业务事实和“应该发布的消息”写进同一个数据库事务：

```text
BEGIN
  订单状态/版本更新
  TCC 分支或账本事实更新
  INSERT outbox_event(...)
COMMIT
        │
        └─ publisher 租约领取、发布、标记 SENT
```

Outbox 解决的是**数据库事实与待发布意图的原子性**，不解决消息 exactly-once、全局顺序、跨库原子性或消费者幂等。

### 25.5.2 DDL：订单、转移、TCC 分支、Saga 步骤、Outbox

以下为 PostgreSQL 15+ 示例；金额是最小单位整数，`numeric` 只用于超出 `bigint` 范围的特殊场景，普通资金交易应按币种上限选 `bigint` 并在应用层校验。

```sql
CREATE TYPE payment_state AS ENUM (
  'INIT', 'ACCEPTED', 'PROCESSING', 'UNKNOWN', 'SUCCEEDED',
  'FAILED', 'CANCELED', 'COMPENSATING', 'TERMINATED',
  'REFUNDING', 'REFUNDED'
);

CREATE TABLE payment_order (
  operation_id       uuid PRIMARY KEY,
  tenant_id          varchar(64) NOT NULL,
  client_key         varchar(192) NOT NULL,
  request_hash       bytea NOT NULL CHECK (octet_length(request_hash) = 32),
  amount_minor       bigint NOT NULL CHECK (amount_minor > 0),
  currency           char(3) NOT NULL,
  state              payment_state NOT NULL DEFAULT 'INIT',
  version            bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
  channel_key        varchar(256) NOT NULL,
  deadline_at        timestamptz NOT NULL,
  state_reason       varchar(128) NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (tenant_id, client_key),
  UNIQUE (channel_key)
);

CREATE TABLE payment_transition (
  transition_id      bigserial PRIMARY KEY,
  operation_id       uuid NOT NULL REFERENCES payment_order(operation_id),
  from_state         payment_state,
  to_state           payment_state NOT NULL,
  event_type         varchar(96) NOT NULL,
  expected_version   bigint NOT NULL,
  actor              varchar(128) NOT NULL,
  event_id           uuid NOT NULL,
  reason             varchar(256) NOT NULL,
  occurred_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (event_id),
  UNIQUE (operation_id, expected_version)
);

CREATE TABLE tcc_branch (
  global_tx_id       varchar(128) NOT NULL,
  branch_id          varchar(128) NOT NULL,
  resource_key       varchar(256) NOT NULL,
  state              varchar(32) NOT NULL CHECK (state IN
                     ('NEW','TRIED','CONFIRMED','CANCELED','CANCELED_EMPTY','REJECTED')),
  cancel_seen        boolean NOT NULL DEFAULT false,
  try_fingerprint    bytea,
  result_json        jsonb NOT NULL DEFAULT '{}'::jsonb,
  lease_until        timestamptz,
  created_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (global_tx_id, branch_id)
);

CREATE TABLE saga_step (
  operation_id       uuid NOT NULL REFERENCES payment_order(operation_id),
  step_key           varchar(128) NOT NULL,
  action             varchar(16) NOT NULL CHECK (action IN ('EXECUTE','COMPENSATE')),
  state              varchar(32) NOT NULL,
  attempt            integer NOT NULL DEFAULT 0,
  external_ref       varchar(256),
  result_json        jsonb NOT NULL DEFAULT '{}'::jsonb,
  next_retry_at      timestamptz,
  updated_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (operation_id, step_key, action)
);

CREATE TABLE outbox_event (
  event_id           uuid PRIMARY KEY,
  aggregate_type     varchar(64) NOT NULL,
  aggregate_id       uuid NOT NULL,
  aggregate_version  bigint NOT NULL,
  event_type         varchar(128) NOT NULL,
  ordering_key       varchar(256) NOT NULL,
  sequence_no        bigint NOT NULL,
  payload            jsonb NOT NULL,
  schema_version     integer NOT NULL,
  status             varchar(16) NOT NULL DEFAULT 'READY'
                     CHECK (status IN ('READY','CLAIMED','SENT','DEAD','BLOCKED')),
  available_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
  claimed_until      timestamptz,
  publish_attempts   integer NOT NULL DEFAULT 0,
  last_error         text,
  created_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
  sent_at            timestamptz,
  UNIQUE (aggregate_type, aggregate_id, aggregate_version),
  UNIQUE (ordering_key, sequence_no)
);

CREATE INDEX outbox_ready_idx ON outbox_event (status, available_at, created_at)
  WHERE status IN ('READY','CLAIMED');
CREATE INDEX outbox_order_idx ON outbox_event (ordering_key, sequence_no);
```

`UNIQUE (aggregate_id, aggregate_version)` 防止同一订单版本产生两条转移事件；`event_id` 防止应用重试插入重复事件。Outbox 的 `sequence_no` 不应由数据库全局 sequence 简单生成后再假定连续，它只用于同一 `ordering_key` 的可比较顺序，分配策略见下一节。

### 25.5.3 并发跳号和乱序：三个顺序不是一个顺序

生产系统至少有三种顺序：

1. **提交顺序**：数据库事务获得 commit timestamp 的先后。
2. **业务顺序**：同一订单/账户状态机允许的版本 `0,1,2...`。
3. **投递顺序**：发布者把消息送到 broker、消费者实际处理的先后。

它们不能天然相等。数据库 sequence 在事务回滚时会跳号；两个事务分别拿到 `100`、`101`，后拿到 `101` 的事务可能先提交，导致按 `sequence_no` 观察到乱序。多个 publisher 并发领取时也会先发布高号消息。

**正确规则**：

- 不要求全局连续号；金融审计需要的是不可重复、可关联、可解释，不是没有空洞的整数。
- 对同一聚合使用事务内 `version = version + 1` 的条件更新，并把该版本写入 Outbox；`aggregate_version` 是业务顺序。
- 若 broker 要求分区顺序，使用稳定 `ordering_key = tenant_id + ':' + operation_id`，让同一 key 进入同一分区。
- 发布者可以并发处理不同 key；同一 key 必须按最小未发送版本逐步放行，或由消费者按版本缓冲/拒绝旧版本。
- 事件带 `event_id`、`aggregate_version`、`occurred_at`、`causation_id` 和 `correlation_id`；消费者以 `(aggregate_id, aggregate_version)` 去重并检查版本跳跃。
- 事务分配的技术 sequence 允许跳号；不要通过回收号、加全局锁制造“连续”，那会把吞吐和故障恢复拖进单点。

同一聚合的发布查询可以使用行锁：

```sql
WITH candidates AS (
  SELECT event_id
  FROM outbox_event
  WHERE status = 'READY'
    AND available_at <= now()
    AND NOT EXISTS (
      SELECT 1 FROM outbox_event prior
      WHERE prior.ordering_key = outbox_event.ordering_key
        AND prior.sequence_no < outbox_event.sequence_no
        AND prior.status NOT IN ('SENT', 'DEAD')
    )
  ORDER BY created_at
  FOR UPDATE SKIP LOCKED
  LIMIT 100
)
UPDATE outbox_event o
SET status = 'CLAIMED', claimed_until = now() + interval '30 seconds',
    publish_attempts = publish_attempts + 1
FROM candidates c
WHERE o.event_id = c.event_id
RETURNING o.*;
```

这段 SQL 只表达数据库侧的“尽量按 key 放行”。如果事件在 broker 已成功但 `SENT` 更新前进程崩溃，事件会再次发布；消费者必须幂等，不能把发布重试误解成业务重做。

### 25.5.4 Go Outbox publisher

```go
type Event struct {
	ID       uuid.UUID
	Key      string
	Version  int64
	Type     string
	Payload  []byte
}

func (p *Publisher) RunOnce(ctx context.Context, limit int) error {
	events, err := p.repo.ClaimReady(ctx, limit, 30*time.Second)
	if err != nil { return err }
	for _, e := range events {
		// Broker 的 message key 必须稳定；发送可能重复，不能依赖返回只一次。
		err := p.broker.Publish(ctx, e.Key, e.ID.String(), e.Payload,
			map[string]string{"aggregate_version": strconv.FormatInt(e.Version, 10)})
		if err != nil {
			_ = p.repo.ReleaseWithBackoff(ctx, e.ID, err, time.Now().Add(jitterBackoff(e.ID)))
			continue
		}
		// SENT 更新失败并不允许撤销 broker 已收到的消息。
		if err := p.repo.MarkSent(ctx, e.ID); err != nil {
			p.metrics.Inc("outbox_mark_sent_error")
		}
	}
	return nil
}

func (c *Consumer) Handle(ctx context.Context, m Message) error {
	return c.db.WithTx(ctx, func(ctx context.Context, tx *sql.Tx) error {
		if c.repo.AlreadyApplied(ctx, tx, m.ID) { return nil }
		v, err := c.repo.CurrentVersion(ctx, tx, m.AggregateID)
		if err != nil { return err }
		if m.Version <= v { return c.repo.MarkDuplicate(ctx, tx, m.ID) }
		if m.Version != v+1 {
			return ErrVersionGap // 进入重试/补拉，不得盲目应用
		}
		if err := c.repo.ApplyProjection(ctx, tx, m); err != nil { return err }
		return c.repo.MarkApplied(ctx, tx, m.ID, m.Version)
	})
}
```

Outbox publisher 应有死信而非无限高速重试；死信意味着“自动发布暂时停止”，不意味着业务事件消失。处理死信必须依据事件版本、业务身份和下游查单，禁止复制 payload 后手工再发一条新 `event_id` 绕过去重。

---

## 25.6 DDL 驱动的状态转移与 Go/Java 核心实现

### 25.6.1 同事务接受订单和写 Outbox

```sql
-- 先锁定幂等身份，参数冲突由应用比较 request_hash 后返回 409。
INSERT INTO payment_order
  (operation_id, tenant_id, client_key, request_hash, amount_minor, currency, version,
   channel_key, deadline_at, state, state_reason)
VALUES
  (:op, :tenant, :key, :hash, :amount, :currency, 1,
   :channel_key, :deadline, 'ACCEPTED', 'CLIENT_ACCEPTED')
ON CONFLICT (tenant_id, client_key) DO NOTHING;

-- 只有 INSERT 影响 1 行时，才追加版本 1 的转移和 Outbox。
INSERT INTO payment_transition
  (operation_id, from_state, to_state, event_type, expected_version, actor, event_id, reason)
VALUES (:op, 'INIT', 'ACCEPTED', 'PaymentAccepted', 0, :actor, :event_id, 'CLIENT_ACCEPTED');

INSERT INTO outbox_event
  (event_id, aggregate_type, aggregate_id, aggregate_version, event_type,
   ordering_key, sequence_no, payload, schema_version)
VALUES (:event_id, 'PAYMENT', :op, 1, 'PaymentAccepted', :op::text, 1, :payload, 1);
```

冲突时必须读取已有 `request_hash`、状态和原始响应：哈希相同返回同一操作；哈希不同返回 `409 IDEMPOTENCY_KEY_REUSED`。不要 `ON CONFLICT DO UPDATE` 覆盖金额或收款人。

### 25.6.2 Go：条件转移服务

```go
type Transition struct {
	From, To string
	Event    string
	Reason   string
}

var allowed = map[string]map[string]string{
	"INIT":       {"Accept": "ACCEPTED"},
	"ACCEPTED":   {"Start": "PROCESSING", "Cancel": "CANCELED"},
	"PROCESSING": {"Confirmed": "SUCCEEDED", "VerifiedRejected": "COMPENSATING", "Timeout": "UNKNOWN"},
	"UNKNOWN":    {"QuerySuccess": "SUCCEEDED", "VerifiedNotExecuted": "COMPENSATING", "Terminate": "TERMINATED"},
	"COMPENSATING": {"Compensated": "FAILED"},
}

func (s *OrderService) Transition(ctx context.Context, op uuid.UUID, cmd string, actor string) error {
	return s.db.WithTx(ctx, func(ctx context.Context, tx *sql.Tx) error {
		o, err := s.repo.GetOrderForUpdate(ctx, tx, op)
		if err != nil { return err }
		to, ok := allowed[o.State][cmd]
		if !ok { return fmt.Errorf("invalid transition %s + %s", o.State, cmd) }
		// 白名单只检查形状；还需在本事务校验成功/未执行/无在途/补偿闭合证据。
		if err := s.repo.CheckEvidence(ctx, tx, o, cmd); err != nil { return err }
		newVersion := o.Version + 1
		eventID := uuid.New()
		if err := s.repo.UpdateState(ctx, tx, op, o.State, to, o.Version, newVersion, reason(cmd)); err != nil {
			return err // WHERE state/version 不匹配则事务失败，调用方重读
		}
		if err := s.repo.InsertTransition(ctx, tx, op, o.State, to, cmd, o.Version, actor, eventID); err != nil { return err }
		payload := mustJSON(struct{ OperationID uuid.UUID; Version int64; State string }{op, newVersion, to})
		return s.repo.InsertOutbox(ctx, tx, Event{ID: eventID, Key: op.String(), Version: newVersion,
			Type: "Payment" + cmd, Payload: payload})
	})
}
```

若 `GetOrderForUpdate` 后进程暂停，另一个事务无法更新同一订单；若采用不锁读，也必须依靠 `UPDATE ... WHERE version = oldVersion` 并在影响行数为 0 时重试/重读。状态白名单与数据库约束应双层存在，防止绕过应用的脚本写入非法状态。

### 25.6.3 Java 21：Saga/TCC 结果的持久化边界

```java
@Service
public final class BranchService {
    private final JdbcTemplate jdbc;

    @Transactional
    public BranchResult tryBranch(String txId, String branchId,
                                  String resourceKey, long amountMinor) {
        if (amountMinor <= 0) throw new IllegalArgumentException("amountMinor");
        ensureBranch(txId, branchId, resourceKey, "NEW", false);
        Branch b = findForUpdate(txId, branchId);
        requireSameResource(b, resourceKey);
        if (b.cancelSeen() || Set.of("CANCELED", "CANCELED_EMPTY").contains(b.state()))
            return BranchResult.replay("CANCELED");
        if ("TRIED".equals(b.state()) || "CONFIRMED".equals(b.state()))
            return BranchResult.replay(b.state());

        accountRepository.moveToFrozen(resourceKey, amountMinor); // 同一事务
        reservationRepository.insertOnce(txId, branchId, resourceKey, amountMinor);
        jdbc.update("UPDATE tcc_branch SET state='TRIED', updated_at=clock_timestamp() " +
                    "WHERE global_tx_id=? AND branch_id=?", txId, branchId);
        return BranchResult.success("TRIED");
    }

    @Transactional
    public BranchResult cancel(String txId, String branchId, String resourceKey) {
        // 首建也走唯一索引仲裁；Try 抢先提交时必须重读 TRIED 并释放一次。
        ensureBranch(txId, branchId, resourceKey, "CANCELED_EMPTY", true);
        Branch b = findForUpdate(txId, branchId);
        requireSameResource(b, resourceKey);
        if ("CONFIRMED".equals(b.state())) return BranchResult.replay("CONFIRMED");
        if ("CANCELED".equals(b.state()) || "CANCELED_EMPTY".equals(b.state()))
            return BranchResult.replay(b.state());
        if ("TRIED".equals(b.state())) reservationRepository.releaseOnce(txId, branchId);
        jdbc.update("UPDATE tcc_branch SET state='CANCELED', cancel_seen=true, " +
                    "updated_at=clock_timestamp() WHERE global_tx_id=? AND branch_id=?", txId, branchId);
        return BranchResult.success("CANCELED");
    }

    private void ensureBranch(String txId, String branchId, String resourceKey,
                              String initialState, boolean cancelSeen) {
        jdbc.update("""
            INSERT INTO tcc_branch
              (global_tx_id, branch_id, resource_key, state, cancel_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (global_tx_id, branch_id) DO NOTHING
            """, txId, branchId, resourceKey, initialState, cancelSeen);
    }

    private Branch findForUpdate(String txId, String branchId) {
        return jdbc.queryForObject("""
            SELECT resource_key, state, cancel_seen
            FROM tcc_branch
            WHERE global_tx_id=? AND branch_id=?
            FOR UPDATE
            """, (rs, row) -> new Branch(rs.getString("resource_key"),
                    rs.getString("state"), rs.getBoolean("cancel_seen")), txId, branchId);
    }

    private static void requireSameResource(Branch b, String resourceKey) {
        if (!b.resourceKey().equals(resourceKey))
            throw new IllegalArgumentException("branch resource mismatch");
    }

    private record Branch(String resourceKey, String state, boolean cancelSeen) {}
}
```

`ensureBranch` 的五个参数与 25.5.2 DDL 的五个显式列一一对应，其余 NOT NULL 列有默认值；不向 `tcc_branch` 假传不存在的金额列。金额/币种等 Try 参数需另以 `try_fingerprint` 绑定并校验（示例资源写入仍由 account/reservation repository 完成）。Cancel 从认证后的分支路由携带真实 resourceKey，不用 `"unknown"` 占位。

默认 `READ COMMITTED` 下，`ON CONFLICT DO NOTHING` 等待冲突事务结束，之后**另一条 SQL** 的 `SELECT ... FOR UPDATE` 获取最新提交快照并锁定分支；不能合成同一语句快照下的“插入或查询”。分支标记不得在协议重试窗口内删除。Try 先提交：Cancel 重读 TRIED，释放一次；Cancel 先提交：Try 重读 CANCELED_EMPTY，不冻结；双 Try/双 Cancel 也由同一入口仲裁。发生真正 SQL 错误、死锁或更高隔离级别的序列化失败，应退出并重试整个本地事务；不能 catch `DuplicateKeyException` 后继续使用已 aborted 的 PostgreSQL 事务。

真实项目要确保通过 Spring 代理调用的 `@Transactional` 覆盖 `accountRepository` 和 `reservationRepository` 的同一数据源/事务传播；跨库时不能仅靠注解宣称原子性，应拆成 Outbox/Saga 或使用经过评估的分布式事务协议。

---

## 25.7 超时 UNKNOWN、巡检、安全补偿与验证体系

### 25.7.1 超时决策表

| 场景 | 能否证明未执行 | 正确状态 | 下一动作 |
|---|---:|---|---|
| 本地事务明确回滚，未写 Outbox | 是 | `FAILED`/未接受 | 同键可重试 |
| Outbox 已提交，worker 尚未发布 | 是（外部未调用） | `PROCESSING` | 继续发布 |
| 外部调用返回权威未执行终态，且无在途请求 | 是 | `COMPENSATING`（有已完成前步） | 按证据补偿，完成后 FAILED |
| 外部调用超时/连接断开 | 否 | `UNKNOWN` | 原 key 查单，不新建订单 |
| 渠道查单返回成功 | 是 | `SUCCEEDED` | 以渠道引用幂等入账 |
| 渠道查单返回不存在，但仍在最终一致窗口 | 否 | `UNKNOWN` | 延迟后重查/对账 |
| 退款请求超时 | 否 | `REFUNDING`/`UNKNOWN` | 查退款流水，禁止重复退款 |

关键规则：

1. 本地超时是观测结果，不是业务结果；HTTP `504` 不得映射成 `FAILED`。
2. `UNKNOWN` 只用同一 `operation_id/channel_key` 查单/对账，普通恢复不得重新 execute；同键提交也须由显式安全恢复协议授权。如果渠道不支持幂等和查单，必须停止自动执行并进入人工队列。
3. 任何“成功”都要绑定外部引用、金额、币种、商户号和签名；只有“没有找到流水”不等于“明确失败”。
4. 状态推进和账本入账需要独立幂等键，例如 `operation_id:channel_success:v1`，回执重复只能读原结果。

### 25.7.2 巡检任务

每分钟/每小时巡检不是简单扫 `updated_at`，而是按证据关系发现不闭合：

```sql
-- 订单处于 UNKNOWN 太久：进入人工/查单队列
SELECT operation_id, channel_key, deadline_at
FROM payment_order
WHERE state = 'UNKNOWN'
  AND updated_at < now() - interval '10 minutes'
FOR UPDATE SKIP LOCKED;

-- Outbox 租约过期：回收为 READY，但保留发布次数和错误
UPDATE outbox_event
SET status='READY', claimed_until=NULL,
    available_at=now() + interval '1 second', last_error='CLAIM_EXPIRED'
WHERE status='CLAIMED' AND claimed_until < now();

-- 同一聚合版本缺事件或重复事件
SELECT aggregate_id, aggregate_version, count(*)
FROM outbox_event
GROUP BY aggregate_id, aggregate_version
HAVING count(*) <> 1;
```

建议的巡检指标和动作：

| 指标 | 告警条件 | 动作 |
|---|---|---|
| `unknown_age_p99` | 超过渠道 SLA | 查单、冻结新动作、升级人工 |
| `outbox_ready_age` | 超过发布 SLA | 检查 DB/连接器/分区，不能直接补发新事件 |
| `version_gap_count` | 非零持续 5 分钟 | 停止该 key 后续消费，补拉/人工确认 |
| `tcc_tried_without_terminal` | 超过租约窗口 | 查询协调器，必要时按原分支 Cancel |
| `compensation_unknown` | 任意高价值单 | 双人复核，不允许脚本批量冲正 |
| `ledger/order mismatch` | 任意 | 冻结自动推进，生成差异工单 |

### 25.7.3 安全补偿 Runbook

安全补偿遵循“先证据、后动作；先冻结、后修复；只追加、不覆盖”：

1. **冻结自动推进**：对 `operation_id` 加维护标记/栅栏版本，阻止旧 worker 继续发送；不要只杀进程，因为队列可能自动拉起。
2. **收集证据**：订单快照、所有 transition、TCC/Saga 步骤、Outbox payload/hash、渠道请求/响应、账本 entry、对账文件、trace/correlation ID。
3. **确定事实**：以渠道查单/银行回执和账本不可变分录为准，不能以当前订单状态或日志最后一行猜测。
4. **选择动作**：权威证明未执行且无在途，才补偿已完成前步的未消耗预留；已扣款未入账则用原成功事实幂等入账；已入账但应退则创建新退款/冲正操作；证据不足保持 `UNKNOWN`，不解冻。
5. **双人审批和限额**：人工补偿需 maker-checker、金额上限、收款人白名单、原因码、工单号和过期时间；禁止 SQL 直接改余额/状态。
6. **验证收敛**：再次查询渠道、重算账本、核对订单状态、检查是否有重复 Outbox/分录，最后解除维护标记。

补偿脚本必须是可审计、可重入、带 dry-run 的程序；输入是稳定的 `operation_id` 和证据版本，不接受任意 `UPDATE payment_order SET state=...`。

### 25.7.4 测试、压测和故障注入

**单元/性质测试**：

- 枚举所有状态×事件，确认非法边不改变任何持久化数据；
- 随机生成重复/乱序 `Try/Confirm/Cancel`，验证最终最多一个 `CONFIRMED` 或 `CANCELED`，不会空回滚或悬挂；
- 任意重放同一 Outbox/回执，账本 entry 数和金额保持不变；
- 任何成功状态都有对应不可变事实和外部引用；任何失败状态都有释放/补偿证据或人工状态；
- 账本从分录重放后的余额与投影一致，借贷平衡始终成立。

**集成测试**：

```text
DB commit 前宕机 / commit 后宕机
Outbox publish 成功但 MarkSent 失败
Try 请求延迟到 Cancel 之后
Confirm 重复、Cancel 重复、Confirm/Cancel 交叉
渠道已扣款但同步响应丢失，webhook 与对账文件乱序到达
两个 worker 同时领取同一订单/同一 outbox
数据库死锁、主从切换、broker 分区、时钟跳变
```

**关键回归示意（本次修订未连接或运行 PostgreSQL）**：以下测试应在真实 PostgreSQL 上用两个独立连接、显式提交屏障执行；内存 mock 无法验证 aborted transaction、唯一索引等待及行锁可见性。

| 用例/调度 | 必须断言 |
|---|---|
| 新分支，连接 A Try 首建后暂停提交；B Cancel 首建等待；A 提交 | B 继续后重读 TRIED；冻结/释放各一次，最终 CANCELED，available 回原值；无 SQLSTATE 25P02 |
| 新分支，A Cancel 首建后暂停提交；B Try 等待；A 提交 | B 读 CANCELED_EMPTY，不冻结、不释放；两连接均成功结束 |
| 双 Try、双 Cancel 同时首建；以及首个插入者回滚 | 仅一个分支；等待者重读或接管插入，最终最多冻结一次、释放一次；回滚者无残留资金事实 |
| 同一分支换 resourceKey / Try 指纹 | 拒绝冲突，不因 DO NOTHING 而静默接受变参；Cancel 不用伪造资源键 |
| 冻结已成功，渠道扣款成功但响应丢失 | 当前步和订单持久化 UNKNOWN，查证任务存在；前步 compensate 调用数为 0、冻结不释放、下一步不执行 |
| 上述 UNKNOWN 重启，或 RUNNING 后崩溃重启 | execute 调用数不增加；仅查单。查到成功后归并 SUCCEEDED，按流程完成或另建退款，不直接解冻 |
| 当前步返回 FAILED，但无权威未执行证据/仍有在途/NOT_FOUND 尚在一致窗口 | 持久化 UNKNOWN 并停止；compensate 调用数为 0 |
| 当前步 FAILED 且权威未执行、无在途证据已持久化 | 进入 COMPENSATING；仅逆序补偿已知 SUCCEEDED 前步，绝不补偿 FAILED/UNKNOWN 本步；全部闭合后订单 FAILED |
| 补偿过程中崩溃，或补偿返回 UNKNOWN | 恢复补偿模式，不再正向 execute；未知补偿先查证，阻断更早步骤释放，订单不得提前 FAILED |

Saga 用例可先用记录调用次数的 fake repository/step 验证分支选择，再在 PostgreSQL 中注入“RUNNING 已提交但结果未落库”“UNKNOWN 与查证 Outbox 原子提交失败”等窗口；此处给出的是测试规约，不宣称数据库集成已通过。

**压测指标**：不要只看 TPS。至少观察状态转移 p95/p99、同 key 冲突率、锁等待、死锁重试率、Outbox ready age、重复投递率、版本 gap、UNKNOWN 比例、补偿成功率、账本重放耗时和人工工单积压。压测必须包含热点账户、同一订单 1000 个并发重试、10% 消息重复和发布后随机崩溃。

### 25.7.5 极端边界清单

| 边界 | 处理要求 |
|---|---|
| 金额为 0、负数、超过币种上限 | 创建前拒绝；不能用 `max(x,0)` 静默修正 |
| 同 key 不同金额/币种/收款人 | `409`，保留第一次指纹，不覆盖 |
| 客户端 key 过期但渠道仍可查 | 业务身份和查单保留期覆盖最长结算/监管窗口 |
| 时钟回拨/夏令时 | 过期用数据库时间与单调计时；业务日期单独建模 |
| sequence 跳号 | 正常；用聚合版本和事实审计，不补洞 |
| sequence 乱序 | broker key 分区或消费者版本缓冲，不盲目应用 |
| Cancel 早于 Try | 写 `CANCELED_EMPTY`，禁止迟到 Try |
| Confirm 早于 Try | 记录冲突/等待策略，禁止直接确认 |
| 渠道成功但订单已退款 | 以两笔独立操作核实，不能覆盖原状态 |
| 部分退款 | 每个退款使用独立 operation/幂等键，累计不超过可退金额 |
| 账户被冻结/合规拦截 | 状态进入人工/受限，不用技术失败自动释放所有资产 |
| 数据库恢复到旧时间点 | 使用 WAL/Outbox/对账重放，不能按旧状态再次扣款 |

---

## 25.8 衔接：从订单状态机到全局资金事实与清结算

本章解决的是“订单流程如何安全推进”，但它不替代账本、渠道和对账。与前后章节的边界如下：

```text
24 全链路幂等
  身份、请求指纹、UNKNOWN、渠道查单
          │
          ▼
25 订单状态机 / TCC / Saga / Outbox
  允许的转移、预留与补偿、可靠发布、版本顺序
          │
          ▼
22/23 复式账本与冲正
  不可变分录、热点账户、FX、冲正、对账
          │
          ▼
后续清结算与风控章节
  批次、净额、银行文件、监管报表、异常终止
```

落地时应形成一条可查询证据链：

```text
client_key
  → operation_id
  → transition(version)
  → saga_step / tcc_branch
  → outbox(event_id, aggregate_version)
  → channel_key / external_ref
  → ledger_entry / reversal_of
  → reconciliation_case
```

下一阶段实现清结算或跨币种流程时，沿用四条规则：

1. **状态机只决定流程，不伪造资金事实**；成功入账必须由账本原子事实证明。
2. **Outbox 只保证待发布意图不丢**；跨数据库、跨银行、跨链的收敛依赖查单、回执、对账和人工边界。
3. **Saga 补偿是新业务动作**；退款、冲正、退回都应有新操作身份、独立幂等键和双重记账证据。
4. **版本和序号是局部有序，不追求全局无洞**；需要全局审计时使用数据库提交时间、WAL/L SN、事件 ID 和不可变转移日志共同重建。

如果下一章要接入批量清算，建议以 `settlement_batch_id` 作为新的聚合边界：批次内订单引用不可变，批次状态同样走条件版本更新，银行文件导入通过文件 hash、行号和渠道流水幂等，文件部分成功必须进入 `PARTIAL/UNKNOWN`，不能用一个批次状态覆盖每笔订单证据。

---

## 25.9 术语表

| 术语 | 含义 |
|---|---|
| 状态机 | 由状态、事件、前置条件和合法转移组成的持久化流程模型 |
| 业务状态 | 描述当前允许动作的投影，不等于资金事实 |
| TCC | Try 预留、Confirm 确认、Cancel 取消的参与者协议 |
| 空回滚 | Try 未成功却收到 Cancel，错误地执行释放/回滚 |
| 悬挂 | Cancel 已决定后迟到 Try 仍执行，产生孤儿预留 |
| Saga | 多个本地事务及其补偿动作组成的长流程 |
| 补偿 | 对已完成业务动作追加反向或修正动作，不是数据库撤销 |
| Outbox | 与业务事实同事务写入、提交后异步发布的消息表 |
| 聚合版本 | 同一业务聚合的局部业务顺序，通常从 0/1 单调递增 |
| 跳号 | 事务回滚、并发分配等造成技术序号有空洞，通常可接受 |
| 乱序 | 消息到达/处理顺序不同于业务版本顺序 |
| UNKNOWN | 无法证明外部副作用是否发生的业务状态 |
| 查单 | 使用原幂等键/外部引用向渠道查询真实结果 |
| 栅栏令牌 | 让旧租约持有者无法继续提交副作用的递增版本/令牌 |
| 经济事实 | 对余额、债务、所有权产生影响的不可变分录或外部流水 |
| 安全补偿 | 有证据、审批、限额、审计和幂等边界的修复动作 |

---

## 25.10 面试答题卡

### Q1：为什么订单状态不能直接 `UPDATE status`？

答：因为它丢失前置状态、版本、事件来源和并发证据，可能覆盖成功/退款/补偿并发。应使用状态白名单、条件版本更新、不可变 transition 日志和同事务 Outbox；状态只是投影，资金变化由不可变账本事实证明。

### Q2：TCC 如何防空回滚、防悬挂、幂等？

答：参与者按 `(global_tx_id, branch_id)` 持久化分支；Cancel 先到就写 `CANCELED_EMPTY/cancel_seen`，不释放不存在的资源；迟到 Try 检查取消标记后拒绝执行；Try/Confirm/Cancel 都按分支状态重放原结果，资源和分支状态在同事务处理。

### Q3：TCC 和 Saga 怎么选？

答：TCC 适合可预留、可确认、可取消的资源，要求参与者可持久化分支且三个动作幂等；Saga 适合长事务和不可持有锁的本地事务，靠补偿动作收敛。银行扣款、链上转账等不可撤回动作不能伪装成 TCC，通常是 Saga + 查单 + UNKNOWN + 对账。

### Q4：Outbox 是否实现 exactly-once？

答：不实现。它保证业务事实与待发布意图同库同事务提交；发布者崩溃可能重复发送，broker 也通常至少一次。必须使用稳定 `event_id`、聚合版本、消费者去重和版本 gap 检查。真正 exactly-once 的经济效果依赖业务幂等，不是消息系统标签。

### Q5：数据库 sequence 跳号和消息乱序怎么处理？

答：跳号正常，不回收、不加全局锁补洞；同一聚合用事务版本表达业务顺序，broker 用稳定 key 分区，消费者按版本去重/缓冲/拒绝 gap。提交顺序、业务顺序、投递顺序不能混成一个全局序号。

### Q6：RPC 超时后为什么必须 UNKNOWN？

答：因为请求可能已到达并产生副作用，只是响应丢失。映射成失败会导致重复扣款，映射成成功又可能未扣款。进入 UNKNOWN 后用原幂等键查单、接收回执和对账；渠道不支持这些能力时停止自动重试并人工处理。

### Q7：补偿是不是把状态改回去？

答：不是。补偿是对已发生动作追加一个新的业务事实，例如退款、解冻、冲正；原订单和原分录保留不变。补偿本身也可能超时，必须有独立操作身份、幂等键、状态机和 UNKNOWN 边界。

### Q8：如何证明一笔订单没有重复扣款？

答：沿 `operation_id` 关联客户端 key、请求 hash、订单 transition、渠道 key/流水、账本 posting 幂等键和对账结果；检查同一经济动作的唯一约束、不可变分录数量和金额、渠道流水唯一性。不能只看订单最终状态。

---

## 25.11 自检题

1. `Cancel` 先于 `Try` 到达时，为什么不能直接把账户 `available += amount`？应写什么分支事实？
2. `Confirm` 在 `Try` 响应丢失后到达，参与者如何区分“Try 成功但响应丢失”和“Try 从未发生”？
3. Outbox 已发布但 `MarkSent` 失败，重启后会发生什么？消费者靠什么保证不重复扣款？
4. 为什么 `FAILED` 不能作为渠道请求超时的统一映射？请写出至少两个后续动作差异。
5. 同一订单收到 `version=4`，当前版本为 `2`，消费者应该应用、丢弃还是缓存？为什么？
6. 一个 Saga 步骤返回 `UNKNOWN`，下一个步骤能否继续？需要查哪些证据？
7. 已 `CONFIRMED` 的 TCC 分支再次收到 `Cancel`，为什么返回幂等成功比执行释放安全？
8. 如何设计部分退款的幂等键和“累计退款不超过原扣款”的并发约束？
9. 为什么技术序号允许跳号，而审计仍然可以证明没有事件丢失？
10. 运维发现订单 `SUCCEEDED` 但账本没有分录，第一步为什么是冻结自动推进并收集证据，而不是直接改状态？

**参考要点**：空回滚标记；分支持久化和查询；至少一次发布+消费者去重；UNKNOWN/查单；版本 gap；先闭合未知步骤；确认事实不可反向释放；退款操作独立身份并锁定可退额度；用不可变事件/提交记录/对账证明；防止二次副作用和破坏审计链。

---

## 25.12 交付前自检清单

- [ ] 所有状态转移有白名单、前置状态、版本条件、操作者和原因。
- [ ] 订单状态、TCC/Saga 状态、账本事实、Outbox 事件可按同一操作身份关联。
- [ ] Try/Confirm/Cancel 都能重复调用；已 Cancel 禁止迟到 Try，已 Confirm 禁止 Cancel 释放。
- [ ] 每个 Saga 步骤写明是否可补偿、补偿键、最大范围和人工边界。
- [ ] UNKNOWN/RUNNING 恢复只查证不重 execute；FAILED 仅凭权威未执行且无在途证据进入前步补偿。
- [ ] TCC Try/Cancel 首建均为 ON CONFLICT DO NOTHING 后独立 SELECT FOR UPDATE，无异常后继续使用 aborted 事务的路径。
- [ ] 业务事实与 Outbox 在同一数据库事务；publisher 和 consumer 都按至少一次设计。
- [ ] 未把 DB sequence 连续性当作业务顺序；同一聚合有版本 gap 检测。
- [ ] 外部超时进入 UNKNOWN，查单和对账使用原 key；没有“超时即失败”的代码路径。
- [ ] 账本分录只追加，补偿是新操作；没有人工 SQL 改余额或覆盖终态。
- [ ] 巡检覆盖过期租约、UNKNOWN、版本 gap、补偿 UNKNOWN、账本/订单不一致。
- [ ] 测试包含进程崩溃、重复乱序、网络分区、commit/publish 窗口和热点并发，并验证经济结果而非只验证 HTTP 状态。

> 最终验收标准不是“所有请求都返回 200”，而是：任何一笔订单在最坏时刻都能回答“发生了什么、事实在哪里、下一步为什么安全、谁可以批准补偿”，并且重试、乱序和故障不会凭空创造第二笔钱。
