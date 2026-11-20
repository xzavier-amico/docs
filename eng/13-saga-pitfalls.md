# 13. Saga 三连坑：空回滚、悬挂、幂等

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · 事务与一致性 ｜ **编号**：13 / 52
> **本章定位**：Saga/TCC 的三个坑不是「代码写错了」，是**协议在乱序和重试下的必然产物**。这一章每种坑都给时序图、现象、检测 SQL、修复 SQL，并把解法收敛成四件套：事务状态表、幂等键、防悬挂标记、补偿重试上限。
> **读法**：§13.3 的三张时序图必须看懂；§13.4.4 的三组 SQL 存成排障脚本；§13.5 是 Agent 工具补偿的对照。

---

## 13.1 引子：三次「补偿跑在正向前面」的事故

**事故一：空回滚，5,000 张券白送（2024-12-19）**

营销活动用 TCC 发券。TC 在 Try 超时后触发 Cancel，Cancel 到达时 Try 还没到；发券服务的 Cancel 发现「没有预留记录」，直接返回成功，**没有留下任何墓碑**。2 秒后 Try 的重试到达，看到没有记录，正常发券。

- 5,000 张 50 元券在订单已取消的情况下发出，损失 **25 万元**。
- 更麻烦的是：券已被用户使用 1,200 张，追回涉及 1,200 笔订单。
- 根因：**Cancel 没有留下「已取消」的标记**，后续 Try 无法判断自己来晚了。

**事故二：悬挂，1,200 件库存锁了 3 天（2025-02-08）**

库存 TCC 的 Cancel 先到，释放了一个不存在的预留；随后 Try 重试成功，冻结了 1,200 件库存。全局事务已经回滚，没有人再来 Cancel 这一次。

- 1,200 件库存被锁 3 天，热门 SKU 无法售卖，超卖 380 单。
- 3 天后盘库才发现，人工释放 + 赔付 **18 万元**。
- 根因：**Try 没有检查「这个 xid 是否已经被 Cancel」**。

**事故三：补偿失败，3,400 笔退款重试 6 小时（2025-05-16）**

退款 Saga 的补偿调用渠道退款接口，渠道超时。补偿 worker 的 16 个线程每秒各重试一次，没有上限、没有退避。

- 6 小时发出 **34 万次**重试请求，渠道把商户号限流，正常退款也失败。
- 3,400 笔退款积压 11 天，人工逐笔处理，赔付 **47 万元**。
- 根因：**补偿失败被当成「临时异常」，没有重试上限和人工兜底**。

> 🩸 三个坑的共同点：**补偿不是正向的逆运算，它是一个独立的、可能乱序、可能失败的远程调用。** 你必须像设计正向一样设计补偿。

---

## 13.2 到底是什么：三个坑的精确定义

| 坑 | 定义 | 触发条件 | 直接后果 | 本质 |
| --- | --- | --- | --- | --- |
| **空回滚** | Cancel 执行时，Try 从未执行过 | Try 超时/丢包，TC 先发 Cancel | Cancel「成功」但没释放任何东西；后续 Try 可能补上 | 补偿缺少「已取消」墓碑 |
| **悬挂** | Cancel 已经执行，Try 之后才到达并成功 | 网络延迟、Try 重试、TC 重试 | 资源被冻结/券被发出，但全局事务已回滚，没人再补偿 | Try 没有检查取消标记 |
| **补偿失败** | Cancel 本身失败（超时、下游不可用、业务拒绝） | 渠道故障、余额不足、状态冲突 | 流程卡在中间，资源不释放/钱不退 | 把补偿当「一定成功」 |

**三个反例，先钉死**：

| 你以为 | 为什么错 | 不做会怎样 |
| --- | --- | --- |
| 「Cancel 没找到记录，返回成功就行了」 | 返回成功不等于「以后不会再来 Try」 | 空回滚后 Try 补上，资源悬挂 |
| 「Try 是正向，Cancel 是反向，顺序一定对」 | 跨网络没有顺序保证；重试会打乱 | Cancel 先到，Try 后到，资源锁死 |
| 「补偿失败就多试几次」 | 无限重试会打爆下游，还会阻塞正常请求 | 渠道限流，正常退款也失败 |
| 「幂等就是唯一索引」 | 唯一索引防重复插入，不防「先取消后插入」 | 需要状态机 + 防悬挂标记，不只是唯一键 |

**三定律检验**：

- **状态机定律**：每个分支必须有 `TRYING/TRIED/CONFIRMED/CANCELLED/CANCEL_FAILED` 状态；没有状态就无法判断「谁来晚了」。
- **稀缺资源定律**：TCC 锁的是预留额度；悬挂会让额度永久占用，空回滚会让额度被重复占用。
- **不可回滚定律**：Cancel 不是「撤销 Try」，是「释放预留/退款/退库存」的新事实；它本身也可能失败。

**三坑不是三个独立 bug，而是一条因果链**：空回滚是「Cancel 没留墓碑」，悬挂是「Try 没看墓碑」，两者是同一个状态缺失的两面；补偿失败则是另一条正交的故障线——即使顺序完全正确，Cancel 也可能因为下游超时/拒绝而失败。所以修复顺序必须是：**先补状态（墓碑 + 防悬挂标记），再修数据（释放悬挂资源），最后补补偿重试与人工兜底**。反过来先修数据，晚到的 Try 会把刚修好的资源再占一次。

---

## 13.3 结构图：三个坑的时序

```
① 空回滚（Cancel 先于 Try）
 TC                    Participant
  │── Cancel(xid) ──────►│  查无 Try 记录
  │◄───── SUCCESS ───────│  直接返回成功，没留墓碑
  │                      │
  │   ... 2 秒后 ...      │
  │── Try(xid) ─────────►│  查无记录 → 正常预留/发券
  │◄───── SUCCESS ───────│  ⚠️ 资源已占用，但全局事务已回滚
  │                      │     没有人会再来 Cancel

② 悬挂（Try 晚于 Cancel）
 TC                    Participant
  │── Cancel(xid) ──────►│  释放预留（可能释放了个寂寞）
  │◄───── SUCCESS ───────│
  │                      │
  │── Try(xid) 重试 ────►│  不检查 cancel_flag → 重新冻结
  │◄───── SUCCESS ───────│  ⚠️ 资源悬挂，直到人工发现

③ 补偿失败（Cancel 自己失败）
 TC                    Participant              渠道
  │── Cancel(xid) ──────►│── refund ───────────►│
  │                      │◄──── timeout ────────│
  │◄───── FAILED ────────│  重试 1s/1s/1s...     │
  │                      │  ⚠️ 无限重试 → 渠道限流 → 正常退款也挂
```

**正确顺序（带墓碑与防悬挂）**：

```
Try:
  INSERT branch(status=TRYING) ON DUPLICATE KEY UPDATE nothing
  SELECT branch FOR UPDATE
  if cancel_flag=1 or status=CANCELLED: return HANGING_REJECTED
  if status in (TRIED, CONFIRMED): return DUPLICATE_OK
  UPDATE status=TRIED; reserve resource

Cancel:
  INSERT branch(status=CANCELLED, cancel_flag=1, cancel_time=now)
    ON DUPLICATE KEY UPDATE cancel_flag=1, cancel_time=now
  SELECT branch FOR UPDATE
  if status=CONFIRMED: return PROTOCOL_VIOLATION (人工)
  if status=TRIED: release resource; UPDATE status=CANCELLED
  if status=CANCELLED: return DUPLICATE_OK
  if release fails: UPDATE status=CANCEL_FAILED; retry with backoff
```

---

## 13.4 怎么实现：四件套 + 三组检测/修复 SQL

### 13.4.1 事务状态表（含防悬挂标记）

```sql
CREATE TABLE saga_branch (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  xid           VARCHAR(128)    NOT NULL COMMENT '全局事务 ID',
  branch_id     VARCHAR(128)    NOT NULL COMMENT '分支 ID（服务+资源）',
  biz_no        VARCHAR(64)     NOT NULL COMMENT '业务键，如 order_no',
  resource_id   VARCHAR(128)    NULL COMMENT '预留资源 ID',
  status        ENUM('TRYING','TRIED','CONFIRMED','CANCELLED',
                     'CANCEL_FAILED','MANUAL') NOT NULL DEFAULT 'TRYING',
  cancel_flag   TINYINT         NOT NULL DEFAULT 0 COMMENT '1=已收到 Cancel，防悬挂',
  try_time      DATETIME(3)     NULL,
  confirm_time  DATETIME(3)     NULL,
  cancel_time   DATETIME(3)     NULL,
  retry_count   INT             NOT NULL DEFAULT 0,
  next_retry_at DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  last_error    VARCHAR(512)    NULL,
  version       BIGINT          NOT NULL DEFAULT 0,
  created_at    DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at    DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_xid_branch (xid, branch_id),
  KEY idx_status_retry (status, next_retry_at),
  KEY idx_biz (biz_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE saga_manual_queue (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  xid         VARCHAR(128)    NOT NULL,
  branch_id   VARCHAR(128)    NOT NULL,
  reason      VARCHAR(512)    NOT NULL,
  status      ENUM('OPEN','DONE') NOT NULL DEFAULT 'OPEN',
  created_at  DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_xid_branch (xid, branch_id),
  KEY idx_status (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 13.4.2 Java：Try / Confirm / Cancel 的并发安全写法

```java
@Service
public class SagaBranchService {
    @Resource private SagaBranchMapper branchMapper;
    @Resource private StockReservationMapper reservationMapper;

    /** Try：先插入 TRYING（不覆盖已存在的 Cancel 墓碑），再锁行判断 */
    @Transactional(rollbackFor = Exception.class)
    public TryResult tryBranch(String xid, String branchId, String bizNo, long skuId, int qty) {
        branchMapper.insertTryIfAbsent(xid, branchId, bizNo);       // ON DUPLICATE KEY UPDATE id=id
        SagaBranch b = branchMapper.selectForUpdate(xid, branchId); // SELECT ... FOR UPDATE
        if (b.getCancelFlag() == 1 || "CANCELLED".equals(b.getStatus())) {
            return TryResult.HANGING_REJECTED;                      // 悬挂：拒绝正向
        }
        if ("TRIED".equals(b.getStatus()) || "CONFIRMED".equals(b.getStatus())) {
            return TryResult.DUPLICATE_OK;                          // 幂等：已经做过
        }
        // 先更新状态，再预留；同一事务，失败一起回滚
        branchMapper.markTried(xid, branchId);
        int rows = reservationMapper.freeze(xid, bizNo, skuId, qty); // 唯一索引 (xid, sku_id)
        if (rows != 1) throw new IllegalStateException("freeze failed");
        return TryResult.SUCCESS;
    }

    /** Confirm：Cancel 之后不允许 Confirm；已 Confirm 幂等返回 */
    @Transactional(rollbackFor = Exception.class)
    public boolean confirm(String xid, String branchId) {
        int rows = branchMapper.markConfirmed(xid, branchId);
        if (rows == 1) return true;
        SagaBranch b = branchMapper.selectForUpdate(xid, branchId);
        if (b == null) return false;                                // 空 Confirm：协议异常
        if (b.getCancelFlag() == 1) return false;                   // 已取消，不能确认
        return "CONFIRMED".equals(b.getStatus());                   // 幂等
    }

    /** Cancel：无论 Try 是否来过，都留下 CANCELLED 墓碑 */
    @Transactional(rollbackFor = Exception.class)
    public boolean cancel(String xid, String branchId, String reason) {
        // 不存在则插 CANCELLED 墓碑；已存在只打 cancel_flag，不改写 TRIED
        branchMapper.insertCancelTombstone(xid, branchId, reason);
        SagaBranch b = branchMapper.selectForUpdate(xid, branchId);
        if (b == null) return true;
        if ("CONFIRMED".equals(b.getStatus())) {
            branchMapper.markManual(xid, branchId, "cancel after confirm");
            return false;                                           // 协议冲突，人工处理
        }
        if ("MANUAL".equals(b.getStatus())) return false;           // 人工已接管，不自动清状态
        if ("CANCELLED".equals(b.getStatus())) return true;         // 幂等
        try {
            reservationMapper.release(xid);                         // 释放预留，幂等
            branchMapper.markCancelled(xid, branchId);
            return true;
        } catch (Exception e) {
            branchMapper.markCancelFailed(xid, branchId, e.getMessage());
            return false;                                           // 交给重试 worker
        }
    }
}
```

对应的关键 SQL：

```sql
-- Try：不存在才插入；已存在（含 Cancel 墓碑）不覆盖
INSERT INTO saga_branch(xid, branch_id, biz_no, status, try_time)
VALUES (?, ?, ?, 'TRYING', NOW(3))
ON DUPLICATE KEY UPDATE id = id;

-- Cancel：不存在就插入墓碑；已存在只打标记，保留 TRIED 让 Cancel 先释放资源
INSERT INTO saga_branch(xid, branch_id, biz_no, status, cancel_flag, cancel_time)
VALUES (?, ?, ?, 'CANCELLED', 1, NOW(3))
ON DUPLICATE KEY UPDATE
  cancel_flag = 1,
  cancel_time = IF(cancel_time IS NULL, NOW(3), cancel_time);

-- Confirm：只有 TRIED 且未 Cancel 才能确认
UPDATE saga_branch
   SET status='CONFIRMED', confirm_time=NOW(3), version=version+1
 WHERE xid=? AND branch_id=? AND status='TRIED' AND cancel_flag=0;

-- 释放预留（幂等：只有 FROZEN 才释放）
UPDATE stock_reservation
   SET status='RELEASED', updated_at=NOW(3)
 WHERE xid=? AND status='FROZEN';
```

### 13.4.3 Go：补偿重试 worker（上限 + 退避 + 人工）

```go
// CancelService 是 Go 侧对补偿能力的抽象；Java 实现可走 RPC，也可用 Go 重写同一状态机
type CancelService interface {
	Cancel(ctx context.Context, xid, branchID, reason string) error
}

type CompWorker struct {
	Repo     BranchRepo
	Service  CancelService
	MaxRetry int
}

func (w *CompWorker) Run(ctx context.Context) {
	for {
		branches, err := w.Repo.ClaimCancelFailed(ctx, 100) // status=CANCEL_FAILED, retry_count<MaxRetry, next_retry_at<=now
		if err != nil { time.Sleep(time.Second); continue }
		for _, b := range branches {
			err := w.Service.Cancel(ctx, b.XID, b.BranchID, "retry")
			if err == nil {
				w.Repo.MarkCancelled(ctx, b.XID, b.BranchID)
				continue
			}
			if b.RetryCount+1 >= w.MaxRetry {
				w.Repo.MarkManual(ctx, b.XID, b.BranchID, err.Error()) // 转人工，不再自动重试
				w.Repo.EnqueueManual(ctx, b.XID, b.BranchID, err.Error())
				continue
			}
			next := time.Now().Add(backoff(b.RetryCount)) // 5s,30s,2m,10m,30m,1h,2h,6h + jitter
			w.Repo.MarkRetry(ctx, b.XID, b.BranchID, err.Error(), next)
		}
		time.Sleep(500 * time.Millisecond)
	}
}

func backoff(n int) time.Duration {
	steps := []time.Duration{5 * time.Second, 30 * time.Second, 2 * time.Minute,
		10 * time.Minute, 30 * time.Minute, time.Hour, 2 * time.Hour, 6 * time.Hour}
	if n >= len(steps) { return steps[len(steps)-1] }
	return steps[n]
}
```

```yaml
# 补偿策略：超过上限必须转人工，不能无限重试
saga:
  compensation:
    max_attempts: 8
    backoff: ["5s", "30s", "2m", "10m", "30m", "1h", "2h", "6h"]
    jitter: 0.2
    manual_queue: saga_manual
    alert_on_attempt: 3       # 第 3 次失败就告警，不等耗尽
```

### 13.4.4 三坑的检测 SQL 与修复 SQL

**① 空回滚：Cancel 已执行，但从未有过 Try**

```sql
-- 检测：有 Cancel 墓碑、try_time 为空，且资源竟然存在（说明是晚到的 Try 造的）
SELECT b.xid, b.branch_id, b.biz_no, b.cancel_time,
       r.id AS reservation_id, r.status, r.created_at
  FROM saga_branch b
  JOIN stock_reservation r ON r.xid = b.xid
 WHERE b.cancel_flag = 1
   AND b.try_time IS NULL
   AND r.status = 'FROZEN';

-- 修复：释放晚到的预留；墓碑保留，防止再次 Try
UPDATE stock_reservation r
  JOIN saga_branch b ON b.xid = r.xid
   SET r.status='RELEASED', r.updated_at=NOW(3)
 WHERE b.cancel_flag = 1 AND b.try_time IS NULL AND r.status='FROZEN';

UPDATE saga_branch
   SET status='CANCELLED', version=version+1
 WHERE xid=? AND branch_id=? AND cancel_flag=1;
```

**② 悬挂：Try 在 Cancel 之后到达并成功**

```sql
-- 检测：try_time > cancel_time，且资源仍 FROZEN
SELECT b.xid, b.branch_id, b.try_time, b.cancel_time,
       r.id AS reservation_id, r.status
  FROM saga_branch b
  JOIN stock_reservation r ON r.xid = b.xid
 WHERE b.cancel_flag = 1
   AND b.try_time > b.cancel_time
   AND r.status = 'FROZEN';

-- 修复：释放悬挂资源，分支回到 CANCELLED
UPDATE stock_reservation r
  JOIN saga_branch b ON b.xid = r.xid
   SET r.status='RELEASED', r.updated_at=NOW(3)
 WHERE b.cancel_flag=1 AND b.try_time > b.cancel_time AND r.status='FROZEN';

UPDATE saga_branch
   SET status='CANCELLED', version=version+1
 WHERE xid=? AND branch_id=? AND status='TRIED' AND cancel_flag=1;
```

**③ 补偿失败：超过重试上限仍未成功**

```sql
-- 检测：CANCEL_FAILED 且达到上限，或 next_retry_at 已过期很久
SELECT xid, branch_id, retry_count, next_retry_at, last_error, updated_at
  FROM saga_branch
 WHERE status='CANCEL_FAILED'
   AND (retry_count >= 8
        OR (next_retry_at IS NOT NULL AND next_retry_at < NOW() - INTERVAL 5 MINUTE))
 ORDER BY updated_at;

-- 修复：转人工队列，停止自动重试；人工处理后回写 DONE
UPDATE saga_branch
   SET status='MANUAL', version=version+1
 WHERE xid=? AND branch_id=? AND status='CANCEL_FAILED';

INSERT INTO saga_manual_queue(xid, branch_id, reason)
VALUES (?, ?, ?)
ON DUPLICATE KEY UPDATE reason=VALUES(reason);
```

> ⚠️ 修复 SQL 必须先 `SELECT` 确认影响行数，再在事务里执行；生产上先 `dry-run`（把 `UPDATE` 改成 `SELECT`），确认只命中预期数据。**不要直接跑 `UPDATE ... JOIN` 不看执行计划。**

**修复顺序（重要）**：先停掉相关补偿 worker 和正向重试，避免边修边写；再按「空回滚 → 悬挂 → 补偿失败」逐类查；每类都先修 `saga_branch` 状态、再修资源表、最后重放/对账。修完必须跑一遍第 14 章的对账 SQL，确认「资源表、分支表、业务表」三方一致，否则只是把问题藏起来了。

---

## 13.5 双修对照：Agent 工具的「三坑」更难逆

| 维度 | 传统系统 | Agent 系统 | 同 or 异 |
| --- | --- | --- | --- |
| 空回滚 | Cancel 先于 Try，券/库存没预留 | **Cancel 先于工具调用**：先执行「取消订单」，工具重试后才「创建订单」 | 同构；Agent 的「资源」是外部副作用 |
| 悬挂 | Try 晚于 Cancel，库存冻结 | **工具调用晚于补偿**：先「删除文件」，重试后才「上传文件」 | 同构；Agent 可能留下孤儿文件/订单 |
| 补偿失败 | 退款接口超时、渠道限流 | **工具不支持撤销**：邮件已发、消息已推送、第三方订单不可取消 | 更常见、更不可逆 |
| 状态表 | `saga_branch` | `run_step`：`step_id/status/cancel_flag/tool_result` | 同套路，多一层工具 |
| 幂等键 | `xid + branch_id` | `run_id + step_id + tool_name + args_hash` | 同构，键多一层 |
| 防悬挂 | Try 前查 `cancel_flag` | **工具调用前查 Step 是否已取消**；已取消则不调用 | 同逻辑，落点不同 |
| 重试上限 | 8 次 + 人工队列 | 同 + **重试前先算成本**：再调一次模型/工具要花多少钱 | 多一个成本闸门 |
| 验证 | 金额/库存对账 | 金额 + **token/调用次数对账** + 工具副作用清单 | 框架相同，口径扩展 |

**哪些完全相同、可以直接复用**：`saga_branch` 表结构、`cancel_flag` 防悬挂、`compensation_id` 幂等、重试上限 + 人工队列、检测/修复 SQL。**Agent 的 Step 补偿就是同一张表换字段名。**

Agent 侧的三条额外纪律：

1. **工具调用前先查 Step 状态**：如果 Step 已被取消（`cancel_flag=1`），直接返回「已取消」，不要执行工具。否则就是悬挂。
2. **不可逆工具先审批/dry-run**：发邮件、发短信、删文件、调第三方下单，补偿往往不可行；执行前必须有人确认或至少记录「不可逆」标记。
3. **补偿失败要告诉用户**：传统系统补偿失败进工单；Agent 还要给用户一个明确的「哪些操作没撤销成功」的说明，不能假装完成。

**最麻烦的是「不可逆工具」**：邮件已发、消息已推送、第三方订单已受理，这些没有真正的 Cancel，只有「发更正/道歉/人工协商」。对这类工具，正确做法不是写一个假补偿，而是：执行前审批（或 dry-run）、执行后记录不可逆标记、失败时进人工队列并在 Run 结果里明确标注「未撤销」。**把不可逆说成已回滚，比补偿失败本身更严重。**

```go
// Agent Step：调用工具前检查取消标记；调用后落结果 + 补偿信息
func (e *Executor) CallTool(ctx context.Context, step Step) (Result, error) {
	if step.CancelFlag == 1 {
		return Result{}, ErrStepCancelled          // 防悬挂：已取消，不再调用
	}
	res, err := e.tools.Call(ctx, step.Tool, step.Args)
	if err != nil { return Result{}, err }
	if err := e.repo.SaveToolResult(ctx, step.StepID, res); err != nil {
		// 工具已执行但结果没落库：标记「可能已执行」，恢复时先查询/补偿
		_ = e.repo.MarkMaybeExecuted(ctx, step.StepID, err.Error())
		return Result{}, fmt.Errorf("save tool result: %w", err)
	}
	return res, nil
}
```

---

## 13.6 会在哪炸：失效模式清单

| 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 券发了，订单已取消 | 空回滚 + Try 晚到 | 查 `cancel_flag=1 AND try_time IS NULL` 且有券记录 | 回收券/退款；补墓碑 | Cancel 必须插墓碑；Try 前查标记 |
| 库存锁了 3 天没人释放 | 悬挂：Try 在 Cancel 后成功 | 查 `try_time > cancel_time` 且 `FROZEN` | 释放库存；修分支状态 | Try 检查 `cancel_flag`；唯一索引 |
| 补偿重试打爆渠道 | 无上限、无退避 | 查 `retry_count`、渠道限流日志 | 停 worker、降频、人工 | 上限 8 次 + 指数退避 + 告警 |
| 补偿成功但状态还是 FAILED | 先调下游、后更新状态，更新失败 | 对比下游退款成功与 `saga_branch.status` | 幂等重放 Cancel；修状态 | 状态更新与补偿同一事务/可重放 |
| Confirm 在 Cancel 后到达 | 协议乱序 | 查 `status=CONFIRMED AND cancel_flag=1` | 人工判断：退款或拒绝 | Confirm 条件更新 `cancel_flag=0` |
| 人工队列没人看 | 只告警、无工单 | 查 `saga_manual_queue`  oldest | 建工单 + SLA | 队列长度告警 + 值班责任 |
| 修复 SQL 误伤正常数据 | 没 dry-run、没看执行计划 | 对比修复前后行数 | 备份恢复 + 重新修 | 先 SELECT、再事务、再审计 |

---

## 13.7 怎么验证：乱序、重复、杀 worker

**① 乱序注入**：让 Cancel 先于 Try 到达（在网关/服务间加 2 秒延迟），断言：

- Cancel 留下 `CANCELLED` 墓碑；
- Try 返回 `HANGING_REJECTED`，不产生预留；
- 没有资源泄漏。

**② 重复注入**：同一 `xid` 重试 Try 100 次、Cancel 100 次，断言状态不变、资源只释放一次。

**③ 杀 worker**：补偿到一半 `kill -9` worker，重启后从 `CANCEL_FAILED` 继续；达到上限转 `MANUAL`，不无限重试。

**④ 观测指标**：

| 指标 | 含义 | 告警线 |
| --- | --- | --- |
| `saga_hanging_rejected_total` | 被防悬挂挡住的 Try | 突增说明乱序严重 |
| `saga_cancelled_without_try_total` | 空回滚次数 | 持续 > 0 需查 TC 超时配置 |
| `saga_compensation_retry_total` | 补偿重试次数 | 5 分钟 > 50 |
| `saga_compensation_exhausted_total` | 达到上限转人工 | > 0 即 P2 |
| `saga_manual_queue_size` | 人工队列 | > 10 |
| `saga_branch_stuck_seconds` | 分支停留时长 | > 10 分钟 |

**⑤ 修复演练**：在影子库造 100 条空回滚/悬挂数据，跑检测 SQL 与修复 SQL，断言只命中目标行、修复后对账为 0。

---

## 13.8 用什么工具

| 需求 | 工具 | 边界 |
| --- | --- | --- |
| TCC 框架 | Seata TCC | 提供 Try/Confirm/Cancel 调度；空回滚/悬挂仍要业务表兜底 |
| Saga 编排 | Seata Saga / Temporal | 提供状态机/重试；补偿逻辑和幂等自己写 |
| 补偿重试队列 | 自研表 + 定时 worker | 简单可控；必须带上限、退避、人工队列 |
| 延迟/乱序测试 | Chaos Mesh / 自研代理 | 注入网络延迟、丢包、重复 |
| 监控 | Prometheus + 告警 | 分支状态、重试、人工队列 |

> ⚠️ 框架能力的边界：Seata 的 TCC 会帮你调用 Confirm/Cancel，但**不会替你判断「Cancel 来晚了」**。空回滚和悬挂是业务状态问题，必须在你的 `saga_branch` 表里解决。

---

## 13.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 空回滚 | Cancel 先到、Try 没来；Cancel 必须留 `CANCELLED` 墓碑 |
| 悬挂 | Cancel 后 Try 才到；Try 必须先查 `cancel_flag`，已取消则拒绝 |
| 补偿失败 | 不是异常，是常态；上限 8 次 + 指数退避 + 人工队列 |
| 四件套 | 状态表 + 幂等键 + 防悬挂标记 + 重试上限 |
| 状态表 | `TRYING/TRIED/CONFIRMED/CANCELLED/CANCEL_FAILED/MANUAL` |
| 幂等键 | `xid + branch_id`；补偿用 `compensation_id` 唯一 |
| 检测 SQL | 空回滚看 `cancel_flag=1 AND try_time IS NULL`；悬挂看 `try_time > cancel_time` |
| 修复 SQL | 先 SELECT、再事务、再审计；释放资源但保留墓碑 |
| Agent 侧 | Step 表 + 工具调用前查取消 + 不可逆工具审批 + 重试算成本 |
| 不做会怎样 | 券白送、库存锁死、渠道被打爆；三件事都是钱 |

## 13.10 自检题

1. **Cancel 到达时没有 Try 记录，为什么不能直接返回成功就完事？**
   <details><summary>答案要点</summary>
   因为 Try 可能只是延迟/重试，之后还会到达。Cancel 必须插入 `CANCELLED` 墓碑并置 `cancel_flag=1`；后续 Try 看到标记就拒绝，否则会「补偿后正向成功」，资源悬挂。
   </details>

2. **Try 在 Cancel 之后到达，正确的处理是什么？**
   <details><summary>答案要点</summary>
   Try 先锁分支行，检查 `cancel_flag=1` 或 `status=CANCELLED`；命中则返回 `HANGING_REJECTED`，不预留资源。如果旧版本已经预留了，用修复 SQL 释放 `FROZEN` 资源并保持分支 `CANCELLED`。
   </details>

3. **补偿一直失败，重试 100 次和重试 8 次有什么区别？**
   <details><summary>答案要点</summary>
   100 次会打爆下游、阻塞正常请求、放大故障；8 次 + 指数退避 + 告警 + 人工队列，把「自动能解决的」和「必须人判断的」分开。补偿失败不是技术异常，是运营事件。
   </details>

## 13.11 延伸阅读

- Seata TCC 文档：`@TwoPhaseBusinessAction`、空回滚/悬挂/幂等（以 1.7.x 为准）
- Seata Saga 文档：状态机、补偿服务、重试策略
- Chaos Mesh：NetworkChaos（延迟/丢包/重复）用于乱序注入
- 上一章：[12. 跨服务一致性实战：订单-库存-支付的编排](12-cross-service-consistency.md)
- 下一章：[14. 对账体系：日终对账、实时核对与差异处理](14-reconciliation-system.md)
- 交叉引用：TCC 概览见第 8 章；Saga 编排见第 12 章；状态机落库见第 30 章
