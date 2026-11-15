# 33. 卡单、重跑、跳过与人工干预

> **状态**：`已完成（初稿）` ｜ **所属**：第五部分 · 编排与状态机 ｜ **编号**：33 / 52
> **本章定位**：前四章讲「流程应该怎么走」，本章讲**「流程没走下去怎么办」**。这是生产环境最常被跳过、也最容易出人命的一章——因为它的每个动作都在改真实数据。
> **读法**：§33.4.2 的扫描 SQL 与 §33.4.5 的跳过决策表必须逐字读；§33.4.8 的补偿脚本模板可以直接抄进你的运维仓库。

---

## 33.1 事故现场：3,417 笔退款卡了 9 天，然后被一条 SQL 改成"成功"

### 事故一：没有"停留时长"这个指标

2025 年 8 月 11 日，某支付平台的退款链路。架构完全合规：状态机、幂等表、Outbox、对账，一个不缺。

**02:00**，退款补偿 worker 因为内存泄漏被 K8s OOMKill，进入 `CrashLoopBackOff`，47 次重启全部失败（镜像里有个死循环，重启治不好）。

| 时刻 | 事件 |
| --- | --- |
| 02:00 | 补偿 worker 停止推进。此刻在途 `REFUNDING` 单 **3,417 笔** |
| 02:00~09:00 | 退款成功率曲线从 99.2% 掉到 41%（只有新单能走同步链路），**但没有任何告警** |
| 09:12 | 第一个用户投诉："退款申请 3 天了，钱没到" |
| 09:30 | 客服主管在群里 @ 值班，无人应答（值班在看另一条支付告警） |
| 10:05 | 值班查监控：`refund_success_total` 确实掉了，但告警规则是"失败率 > 5%"——**失败的请求根本没发生，是"没有请求"** |
| 10:40 | 发现 worker 在 CrashLoop，重启并回滚镜像 |
| 11:00 | 链路恢复。积压 3,417 笔进入人工复核队列，按 200 笔/小时处理 |
| 8/12~8/18 | 逐日消化，最老一笔卡了 **9 天 4 小时** |
| 8/31 | 财务月结：**218 万元**退款"应退未退"（钱在平台账上，用户没收到） |

最终：客服工单 **1,240 张**，延迟赔付 3.6 万元，监管报备一次。

复盘时最反直觉的一句是：

> **我们的监控全是"失败了会怎样"，没有一个是"没发生会怎样"。** 补偿 worker 死了，它不报错——它只是不干活。**"不干活"在指标上表现为"一切正常"。**

### 事故二：一条 `UPDATE`，把 41 笔没退的钱标成"已退"

8 月 14 日，事故一还没消化完，运维为了"让客服后台别一直显示退款中"，执行了这条 SQL：

```sql
-- 事后从 binlog 里挖出来的原句（没有工单号，没有审批，没有 WHERE 限制条件）
UPDATE refund_order
   SET status = 'REFUNDED', updated_at = NOW()
 WHERE status = 'REFUNDING';
-- Affected rows: 3417
```

| 时刻 | 事件 |
| --- | --- |
| 8/14 11:20 | SQL 执行，3,417 行状态变成 `REFUNDED`；version 字段**没动**，状态迁移日志**没写** |
| 11:21 | 客服后台全部显示"退款成功"，工单量骤降，运维在群里被表扬 |
| 8/15 09:40 | 41 个用户打电话："显示成功，但银行卡没收到钱"。其中 3 笔是银行侧实际失败（卡已注销） |
| 8/15 14:00 | 对账发现 41 笔"状态已退、资金未退"，涉及 **12.4 万元** |
| 8/16 | 人工补打 41 笔 + 冲正 3 笔失败单，财务手工做凭证 |
| 8/20 | 内审问"谁批的、为什么改"，**答不出来**：没有审计表，只能从 binlog 里按时间捞，且 binlog 只保留 7 天，8/14 的那条已经在临界点 |
| 8/22 | 复核发现另外 **217 笔**的 `updated_at` 被刷新，导致卡单巡检永远扫不到它们 |

这个事故的教训不是"不该跳过"，而是：

> **跳过的本质是「用一个人的判断替代一次系统执行」。** 既然是一次判断，就必须有**判断人、判断依据、判断时间、影响范围**。直接改状态是把"判断"伪装成"事实"——**这是数据系统里最严重的撒谎方式**。

---

## 33.2 到底是什么：三个动作，三种风险等级

### 三个定义

> **卡单（stuck）**：实体处于**非终态**，且在该状态的停留时间**超过其 SLA**，且**当前没有任何在途的推进动作**。三个条件缺一不可。
>
> **重跑（rerun）**：用**同一个业务键**、从**同一个逻辑步骤**重新执行一次推进。它不是"重新发起"——重跑不会产生新的业务单号。
>
> **跳过（skip）**：用一个**外部裁定**（人的判断、上游的补偿、数据修复）替代某一步的实际执行，并把状态推进到"视为完成"。

风险等级完全不同：

| 动作 | 改动什么 | 可逆性 | 风险 | 谁能做 |
| --- | --- | --- | --- | --- |
| **发现（巡检）** | 只读 + 告警 | 不涉及 | 低（误报浪费人力） | 机器，自动 |
| **重跑** | 重新执行一次，可能产生副作用 | **可逆**（幂等前提下等价于没发生） | 中（幂等没做好就翻倍） | 机器/人，需幂等前置 |
| **跳过** | 状态前进但副作用没发生 | **不可逆**（除非有补偿任务） | 高（账实不符） | 人，需审批 + 审计 |
| **强制改状态** | 直接 UPDATE | **不可逆且无痕迹** | **极高** | **不应该存在这个动作** |

### 四个反例：这些都不是你以为的那件事

```text
反例 1：把"慢"当"卡"
  P99 是 3 分钟，第 99.9 百分位的单子跑了 40 分钟 —— 那是长尾，不是卡单。
  判据：卡单必须同时满足「超 SLA」+「无在途推进动作」。只看时间会天天误报。

反例 2：把"重跑"做成"重新发起"
  新建一个退款单、生成新的 refund_no —— 这叫重复申请，会退两次钱。
  重跑必须复用原单号 + 原幂等键，只是"再推一次"。

反例 3：把"跳过"做成"UPDATE 状态"
  事故二。跳过必须留下：谁批的、为什么、影响了哪些行、谁来补副作用。

反例 4：把"人工干预"做成"找 DBA 改库"
  没有审计的干预 = 无法复盘 = 同一类事故必然复发。而且它绕过了所有守卫，
  下次状态机做迁移校验时，这些"野生状态"会变成无法解释的脏数据。
```

### 三件事的关系

```text
        ┌─────────────┐
        │  卡单发现    │  只读：谁超时了、超了多久、有没有人在推
        └──────┬──────┘
               ▼
        ┌─────────────┐
        │  处置决策    │  这是本章唯一需要"人"的环节
        └──┬───┬───┬──┘
           │   │   └──────────────┐
           ▼   ▼                  ▼
      ┌─────┐ ┌─────┐        ┌─────────┐
      │ 重跑 │ │ 跳过 │        │ 放弃/关闭 │
      └──┬──┘ └──┬──┘        └────┬────┘
         │       │                │
         └───────┴────────────────┘
                     ▼
              ┌─────────────┐
              │  审计 + 补偿  │  没有这一步，前两步都是事故
              └─────────────┘
```

---

## 33.3 结构图：卡单巡检的完整闭环

```text
┌──────────────────────────────────────────────────────────────────────────┐
│ 1. 巡检（每 1 分钟，单实例 leader）                                        │
│    SLA 配置表 ──▶ keyset 分页扫描 ──▶ 分组聚合 ──▶ 告警去重表              │
│    (flow,state,sla)    (id > lastId)     (state, 严重度)   (agg+bucket 唯一)│
└───────────────────────────────┬──────────────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 2. 分级（P0/P1/P2）                                                       │
│    P0：终态资金类卡单 > 100 笔 或 最老 > 4h   → 电话 + 值班长             │
│    P1：普通卡单 > 500 笔 或 最老 > 2h         → IM 群 + 工单              │
│    P2：单笔/少量，未超硬 SLA                  → 日报聚合                  │
└───────────────────────────────┬──────────────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 3. 处置（人 + 工具，不允许直连数据库）                                     │
│    单笔重跑 ─┐                                                            │
│    批量重跑 ─┼─▶ 全部走 RerunService：dry-run → 审批 → 分批 → 审计         │
│    跳过     ─┘   （跳过额外要求：补偿任务已创建 + 双人审批）                │
└───────────────────────────────┬──────────────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 4. 审计与回看                                                             │
│    intervention_audit（谁/何时/为什么/改了什么/前后快照/脚本版本）          │
│    ──▶ 每周复盘：卡单 Top3 状态 ──▶ 补 SLA 或修根因 ──▶ 防复发             │
└──────────────────────────────────────────────────────────────────────────┘
```

**这张图里有两个"必须由代码保证"的点**：

1. **巡检只能有一个实例在跑**（否则同一笔单被告警 3 次，人会开始忽略告警）。用 DB 锁选主，不要用"部署时只起 1 个副本"——那个副本重启时巡检就断了。
2. **处置入口只能有一个**（不允许 DBA 直连）。这不是流程洁癖：**只要还有第二条改数据的路，审计就是假的**。

---

## 33.4 怎么实现

### 33.4.1 SLA 配置：按「流程 × 状态」配，不要配全局

```sql
CREATE TABLE sla_config (
  id           BIGINT      NOT NULL AUTO_INCREMENT PRIMARY KEY,
  flow_key     VARCHAR(64) NOT NULL COMMENT '如 refund / order / payout',
  state        VARCHAR(32) NOT NULL COMMENT '非终态才有 SLA',
  sla_seconds  INT         NOT NULL COMMENT '超过即视为"疑似卡单"',
  hard_seconds INT         NOT NULL COMMENT '超过即必须人工介入（不可自动处置）',
  severity     TINYINT     NOT NULL CHECK (severity IN (1,2,3)) COMMENT '3=P0 2=P1 1=P2',
  auto_action  VARCHAR(24) NOT NULL DEFAULT 'ALERT'
               CHECK (auto_action IN ('ALERT','AUTO_RERUN','AUTO_FAIL')),
  enabled      TINYINT(1)  NOT NULL DEFAULT 1,
  updated_by   VARCHAR(64) NOT NULL,
  updated_at   DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
               ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_flow_state (flow_key, state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO sla_config
  (flow_key, state, sla_seconds, hard_seconds, severity, auto_action, updated_by) VALUES
  ('refund','AUDITING',        1800,  7200,  2, 'ALERT',      'ops'),
  ('refund','REFUNDING',        900,  3600,  3, 'AUTO_RERUN', 'ops'),
  ('refund','REFUND_FAILED',  86400, 172800, 1, 'ALERT',      'ops'),
  ('order','PAYING',            600,  1800,  3, 'ALERT',      'ops'),
  ('order','FULFILLING',      172800, 432000, 1, 'ALERT',      'ops'),
  ('payout','SUBMITTED',       3600, 14400,  3, 'ALERT',      'ops');
```

> ⚠️ **为什么不能配全局「30 分钟」**：退款 15 分钟该催，履约 2 天正常，审批 3 天也正常。全局阈值只有一个结局：要么全是误报（人开始忽略），要么真卡单被淹没（事故一）。**SLA 必须由"这个状态正常的业务时长"反推，不是由"我想多快发现"决定。**

### 33.4.2 扫描 SQL：分页要 keyset，不要 OFFSET

```sql
-- 单页扫描。:lastId 是上一页的最大 id，首页传 0。
SELECT r.id,
       r.biz_no,
       r.status,
       r.updated_at,
       TIMESTAMPDIFF(SECOND, r.updated_at, NOW(3)) AS age_sec,
       s.sla_seconds,
       s.hard_seconds,
       s.severity,
       s.auto_action
  FROM refund_order r
  JOIN sla_config s
    ON s.flow_key = 'refund'
   AND s.state = r.status
   AND s.enabled = 1
 WHERE r.status NOT IN ('REFUNDED','REFUND_FAILED','CANCELLED')
   AND r.updated_at < DATE_SUB(NOW(3), INTERVAL s.sla_seconds SECOND)
   AND r.id > :lastId
 ORDER BY r.id
 LIMIT :pageSize;
```

三个细节，每个都是"踩过才知道"：

| 细节 | 错误写法 | 为什么错 | 正确做法 |
| --- | --- | --- | --- |
| **分页方式** | `LIMIT 500 OFFSET 10000` | OFFSET 要扫过前 10,000 行才丢弃，越翻越慢（O(n²)）；且扫描期间有新数据插入时会漏行/重行 | `WHERE id > :lastId ORDER BY id LIMIT 500`（keyset） |
| **时间比较** | `age_sec > s.sla_seconds` 写在 `WHERE` 里 | 无法用索引（除非 MySQL 8.0.13+ 的函数索引），退化成全表扫 | 用 `updated_at < DATE_SUB(...)`，让 `(status, updated_at)` 索引生效 |
| **时限来源** | 代码里写死 `if age > 900` | 改阈值要发版，业务变化跟不上 | 配置表 JOIN，改一行立即生效 |

> 💡 **索引怎么选**：非终态行数 < 100 万时，`KEY idx_status_updated (status, updated_at)` 足够（走 index range，再按 `id` 过滤）。非终态行数上千万时（如大促期间的 `PAYING`），把它改成 `KEY idx_status_id (status, id)`，先按 id 分页、再在应用层算 age——**因为分页的稳定性比"一次扫完"更重要**，分页漏行会导致"有些卡单永远扫不到"。

**告警去重表**（同一笔单不重复轰炸，这是"告警不被忽略"的前提）：

```sql
CREATE TABLE stuck_alert (
  id             BIGINT      NOT NULL AUTO_INCREMENT PRIMARY KEY,
  aggregate_type VARCHAR(32) NOT NULL,
  aggregate_id   BIGINT      NOT NULL,
  state          VARCHAR(32) NOT NULL,
  severity       TINYINT     NOT NULL,
  bucket         VARCHAR(24) NOT NULL COMMENT '按 P 级分桶：P0/P1/P2，升级时换桶',
  first_seen_at  DATETIME(3) NOT NULL,
  last_seen_at   DATETIME(3) NOT NULL,
  notify_count   INT         NOT NULL DEFAULT 0,
  resolved_at    DATETIME(3) NULL,
  UNIQUE KEY uk_agg_bucket (aggregate_type, aggregate_id, bucket),
  KEY idx_unresolved (bucket, resolved_at, last_seen_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 幂等上报：新插入返回 1（该发告警），已存在返回 2（只更新 last_seen，不再发）
INSERT INTO stuck_alert
  (aggregate_type, aggregate_id, state, severity, bucket, first_seen_at, last_seen_at, notify_count)
VALUES
  ('refund', :id, :state, :severity, :bucket, NOW(3), NOW(3), 1)
ON DUPLICATE KEY UPDATE
  last_seen_at = NOW(3),
  severity     = GREATEST(severity, VALUES(severity)),
  notify_count = notify_count + 1;
```

### 33.4.3 巡检任务（Go）：分页 + 分级 + 单实例

```go
package stuck

import (
	"context"
	"database/sql"
	"fmt"
	"time"
)

type Row struct {
	ID          int64
	BizNo       string
	State       string
	AgeSec      int64
	SLA         int
	HardSeconds int
	Severity    int
	AutoAction  string
}

type Stats struct{ Scanned, Stuck, Notified, Escalated int }

// Scan 全量分页扫描。只读，可重复执行（幂等）。
func (s *Scanner) Scan(ctx context.Context, flowKey string) (Stats, error) {
	var st Stats
	var lastID int64
	for {
		rows, err := s.db.QueryContext(ctx, scanSQL, flowKey, lastID, s.pageSize)
		if err != nil {
			return st, fmt.Errorf("scan page after %d: %w", lastID, err)
		}
		batch, err := collect(rows)
		rows.Close()
		if err != nil {
			return st, err
		}
		if len(batch) == 0 {
			break
		}
		st.Scanned += len(batch)

		for _, r := range batch {
			st.Stuck++
			// 分级：超 hard_seconds 必须升级；否则按配置的 severity
			sev := r.Severity
			if r.AgeSec > int64(r.HardSeconds) && sev < 3 {
				sev = 3
				st.Escalated++
			}
			// 去重上报：只有第一次插入才真的发通知
			first, err := s.alert.Once(ctx, "refund", r.ID, r.State, sev)
			if err != nil {
				s.log.Warn("alert dedupe failed", "id", r.ID, "err", err)
				continue
			}
			if first {
				st.Notified++
				s.notify(sev, r)   // P0 电话 / P1 IM / P2 日报
			}
			// 自动处置只允许"重跑"，且必须有幂等保护；跳过永远不自动
			if r.AutoAction == "AUTO_RERUN" && r.AgeSec > int64(r.SLA)*2 {
				s.metrics.Inc("stuck_auto_rerun_total", "state", r.State)
				go s.rerun.SafeSubmit(ctx, r.ID, "auto:stuck-scan", "sla exceeded")
			}
		}

		lastID = batch[len(batch)-1].ID
		if len(batch) < s.pageSize {
			break // 最后一页
		}
		// 每页之间让一下，避免长时间占用从库 IO
		select {
		case <-ctx.Done():
			return st, ctx.Err()
		case <-time.After(50 * time.Millisecond):
		}
	}
	return st, nil
}
```

**单实例：用数据库锁选主（不要用"只部署 1 个副本"）**

```go
// Run 是常驻循环。MySQL 的 GET_LOCK 是"连接级"锁：
// 必须从池里取出专用连接并全程持有，否则 unlock 可能落在另一条连接上，
// 锁永远不释放（这是最常见的误用）。
func (s *Scanner) Run(ctx context.Context) {
	conn, err := s.db.Conn(ctx) // 专用连接，不归还池
	if err != nil {
		s.log.Error("acquire conn", "err", err)
		return
	}
	defer conn.Close()

	tick := time.NewTicker(time.Minute)
	defer tick.Stop()
	leader := false

	for {
		select {
		case <-ctx.Done():
			if leader {
				_, _ = conn.ExecContext(context.Background(),
					"SELECT RELEASE_LOCK('stuck-scan:refund')")
			}
			return
		case <-tick.C:
			var got sql.NullInt64
			if err := conn.QueryRowContext(ctx,
				"SELECT GET_LOCK('stuck-scan:refund', 0)").Scan(&got); err != nil {
				s.log.Error("get_lock", "err", err)
				continue
			}
			leader = got.Valid && got.Int64 == 1
			if !leader {
				s.metrics.Gauge("stuck_scan_is_leader", 0)
				continue // 别的实例在跑，本轮跳过
			}
			s.metrics.Gauge("stuck_scan_is_leader", 1)
			if _, err := s.Scan(ctx, "refund"); err != nil {
				s.log.Error("scan failed", "err", err)
			}
		}
	}
}
```

> 🩸 **Postgres 用户注意**：等价物是 `pg_try_advisory_lock(key)`，同样是**会话级**的，必须用专用连接。另外，锁只在"同一数据库实例"内有效——如果你的库是分片的，每片要单独选主，别指望一把锁管全部分片。

### 33.4.4 重跑：三个前提，一个工具

**重跑的三条前提**（缺一条就不要做批量重跑）：

| 前提 | 检查方法 | 不满足会怎样 |
| --- | --- | --- |
| **幂等** | 同一业务键跑 N 次，副作用只发生 1 次（有唯一索引/状态守卫） | 重跑 3 次 = 退 3 次钱 |
| **可定位** | 能说清"从哪一步重跑"，而不是"整个流程重来" | 已成功的步骤被重做，副作用翻倍 |
| **无在途** | 没有正在执行的推进动作（`lease_until > now` 或消息在途） | 两个执行者并发写同一行（第 31 章的 fence 问题） |

**Java：单笔 + 批量 + dry-run 一体**

```java
public record RerunPlan(long id, String state, String action,
                        boolean executable, String blockedBy) {}

public record RerunResult(long id, String outcome, String detail) {}

@Service
public class RerunService {

    private final JdbcTemplate jdbc;
    private final TransactionTemplate tx;
    private final AuditService audit;

    /** dry-run：只读。必须能回答"会做什么"和"为什么不能做"，这是审批的依据。 */
    public List<RerunPlan> dryRun(List<Long> ids) {
        return ids.stream().map(this::plan).toList();
    }

    private RerunPlan plan(long id) {
        Map<String, Object> o = jdbc.queryForMap(
            "SELECT status, version, updated_at FROM refund_order WHERE id = ?", id);
        String state = (String) o.get("status");

        if (Set.of("REFUNDED", "REFUND_FAILED", "CANCELLED").contains(state)) {
            return new RerunPlan(id, state, "NONE", false, "TERMINAL_STATE");
        }
        // 关键：太"新"的单不要动，可能正在被正常推进（避免双写）
        Timestamp updated = (Timestamp) o.get("updated_at");
        if (updated.after(new Timestamp(System.currentTimeMillis() - 120_000))) {
            return new RerunPlan(id, state, "NONE", false, "TOO_FRESH_MAYBE_IN_FLIGHT");
        }
        Integer lease = jdbc.queryForObject(
            "SELECT COUNT(*) FROM outbox WHERE aggregate_id = ? AND status = 'SENDING'",
            Integer.class, id);
        if (lease != null && lease > 0) {
            return new RerunPlan(id, state, "NONE", false, "OUTBOX_IN_FLIGHT");
        }
        return new RerunPlan(id, state, "REPLAY_REFUND_STEP", true, "");
    }

    /** 执行单笔：独立事务 + 幂等键 + 审计。批量时逐笔调用，一笔失败不影响其他。 */
    public RerunResult executeOne(long id, String operator, String reason, String batchNo) {
        RerunPlan p = plan(id);
        if (!p.executable()) {
            return new RerunResult(id, "SKIPPED", p.blockedBy());
        }
        String idemKey = "rerun:" + batchNo + ":" + id;   // 批量内每笔一个键
        try {
            return tx.execute(status -> {
                // 幂等闸门：同一 (id, step, idemKey) 只允许一条
                int n = jdbc.update("""
                    INSERT INTO rerun_task (aggregate_id, step_key, idem_key, operator, state)
                    VALUES (?, 'REFUND_STEP', ?, ?, 'RUNNING')
                    ON DUPLICATE KEY UPDATE id = id
                    """, id, idemKey, operator);
                if (n == 0) {                       // 已存在 → 之前跑过
                    return new RerunResult(id, "DUPLICATE", idemKey);
                }
                jdbc.update("""
                    UPDATE refund_order
                       SET state = 'REFUNDING', version = version + 1, updated_at = NOW(3)
                     WHERE id = ? AND version = ? AND state = ?
                    """, id, plan(id), p.state());   // 条件更新：状态/版本必须没变
                audit.record(operator, "RERUN", id, p.state(), "REFUNDING",
                             reason, idemKey, null);
                return new RerunResult(id, "OK", idemKey);
            });
        } catch (Exception e) {
            audit.record(operator, "RERUN_FAILED", id, p.state(), p.state(),
                         reason, idemKey, e.getMessage());
            return new RerunResult(id, "ERROR", e.getMessage());
        }
    }
}
```

**批量工具的四条硬规矩**：

| 规矩 | 具体做法 | 为什么 |
| --- | --- | --- |
| **先 dry-run，再执行** | dry-run 输出每笔的 `executable` 与 `blockedBy`，必须人工确认 | 让审批人看到"影响面"，而不是听一句"就几千笔" |
| **分批 + 逐笔独立事务** | 每批 200~500，每笔一个事务 | 一笔失败不拖垮整批；MySQL 大事务锁表 |
| **有速率上限** | 每秒 ≤ 50 笔，且与线上流量共用限流器 | 重跑是"补历史"，不能挤占"当下的用户" |
| **可中断可续跑** | `rerun_job` 记录 `last_id`，中断后从断点继续 | 跑到一半被打断，不要从头再来（重跑不是免费的） |

### 33.4.5 跳过：什么能跳，什么绝对不能跳

**这是全章最重要的一张表。** 打印出来贴在运维工位。

| 步骤类型 | 能跳过吗 | 前置条件 | 跳过必须做的事 | 跳错的后果 |
| --- | --- | --- | --- | --- |
| **只读数据抽取** | ✅ 能 | 记录缺失的时间区间与数据源 | 打 `data_gap` 标记 + 通知下游 | 下游算错数（可重跑修复） |
| **通知/短信/邮件** | ✅ 能 | 有延迟重发队列 | 进重发队列，不是"发过就算了" | 用户不知道（可补发） |
| **归档/报表生成** | ✅ 能 | 报表可回溯重算 | 标记 `report_degraded` | 报表缺一天（可补） |
| **库存回补** | ⚠️ 有条件 | 有实物盘点/日终对账兜底 | 挂补偿任务 + 通知仓储 | 少卖或多卖（对账能发现） |
| **风控/合规校验** | ❌ 不能 | — | — | 合规事故，无法事后补救 |
| **资金扣款/退款/转账** | ❌ **绝对不能** | — | — | 账实不符，法律风险（事故二） |
| **对账差异确认** | ❌ 不能 | — | — | 把资金差异"确认掉"，等于掩盖 |
| **人工审批节点** | ⚠️ 仅代理审批 | 有明确代理规则 + 记录 `delegate_of` | 记录代理人、原审批人、代理依据 | 越权审批（审计过不了） |
| **外部系统的写操作** | ❌ 不能 | — | — | 你根本不知道对方做没做（第 29 章不可回滚定律） |

**跳过的三件套（缺一件就等于事故二）**：

1. **写审计**：谁、何时、为什么、原状态/新状态、影响行数、依据（工单号/审批单号）。
2. **挂补偿任务**：如果被跳过的步骤有副作用，必须创建一条"待补"记录，进入独立的补偿队列，**并且有 SLA**。没有补偿任务的跳过只是撒谎。
3. **打标记**：在业务实体上留一个 `skipped_steps` 字段（JSON 数组），让下游和报表知道"这笔的数据是不完整的"。**不要指望别人记得去审计表里查。**

### 33.4.6 人工干预审计表

```sql
CREATE TABLE intervention_audit (
  id              BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  intervention_no VARCHAR(40)  NOT NULL COMMENT 'IV-20260814-0007',
  aggregate_type  VARCHAR(32)  NOT NULL,
  aggregate_id    BIGINT       NOT NULL,
  action          VARCHAR(24)  NOT NULL
                  CHECK (action IN ('RERUN','SKIP','COMPENSATE','CANCEL','FORCE_STATE')),
  approval_id     VARCHAR(40)  NULL COMMENT '审批单号；SKIP/FORCE_STATE 必填',
  operator        VARCHAR(64)  NOT NULL COMMENT 'user:zhangsan / svc:rerun-job',
  operator_ip     VARCHAR(64)  NOT NULL,
  reason_code     VARCHAR(32)  NOT NULL COMMENT 'STUCK_TIMEOUT/UPSTREAM_FAIL/BANK_REJECT/...',
  reason_text     VARCHAR(512) NOT NULL COMMENT '人话，禁止填"修复数据"',
  before_state    VARCHAR(32)  NOT NULL,
  after_state     VARCHAR(32)  NOT NULL,
  before_snapshot JSON         NOT NULL COMMENT '改动前整行快照（脱敏后）',
  after_snapshot  JSON         NULL,
  script_version  VARCHAR(64)  NOT NULL COMMENT '补偿脚本的 git sha 或 manifest id',
  idem_key        VARCHAR(96)  NOT NULL,
  dry_run         TINYINT(1)   NOT NULL DEFAULT 0,
  batch_no        VARCHAR(40)  NULL COMMENT '批量操作的批次号，便于整批回滚',
  trace_id        CHAR(32)     NOT NULL,
  created_at      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_intervention_no (intervention_no),
  UNIQUE KEY uk_idem (idem_key),
  KEY idx_agg (aggregate_type, aggregate_id, created_at),
  KEY idx_operator (operator, created_at),
  KEY idx_batch (batch_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

四条纪律：

1. **`before_snapshot` 存整行**（脱敏后）。出问题时你需要的是"当时那行长什么样"，而不是"我记得是 REFUNDING"。
2. **`reason_text` 禁止占位符**。可以加一个 CHECK：`reason_text NOT IN ('修复数据','手动处理','临时修复')`——听起来像玩笑，但它是唯一能拦住"复制粘贴同一句理由 3,000 次"的手段。
3. **`dry_run=1` 也记录**。预演本身就是一次"决定"，它证明你评估过影响面。
4. **`batch_no` 必须有**。整批回滚时，`WHERE batch_no = ?` 比按时间范围捞可靠一万倍。

### 33.4.7 可视化与运营界面：必须展示的字段

| 区域 | 字段 | 为什么必须有 | 缺了会怎样 |
| --- | --- | --- | --- |
| **列表** | 单号、当前状态、**状态停留时长**、SLA 剩余 | 一眼看出"谁快超时" | 只能按创建时间排序，老单被淹没 |
| **列表** | 是否有在途动作（worker/消息/审批） | 区分"卡住"和"正在跑" | 把正在跑的单重跑 = 双写 |
| **详情** | 完整迁移历史（时间/事件/actor/reason） | 回答"怎么走到这的" | 只能 grep 日志 |
| **详情** | 每个等待态的**下一次唤醒条件** | 知道"在等谁" | 无限期等下去（事故一） |
| **详情** | 关联的补偿任务状态 | 跳过之后有没人补 | 跳过 = 永久丢失 |
| **操作** | dry-run 结果（影响行数 + blockedBy） | 审批依据 | 批量操作变赌博 |
| **操作** | 审批单号 + 双人确认 | 责任可追溯 | 审计过不了（事故二） |
| **看板** | 卡单数按状态分组 + 最老单龄 + 趋势 | 发现"批量卡"而不是"个别卡" | 只能等投诉 |
| **看板** | 人工干预次数 / 按操作人 | 发现"某个人天天在改库" | 系统性缺陷被个人兜住 |

> 💡 **一个反直觉的设计**：运营界面**不要**提供"强制改状态"按钮。提供"跳过（需审批 + 生成补偿任务）"，把最危险的动作包装成**有代价的动作**。当一件事需要填理由、找两个人点同意，人才会认真想。

### 33.4.8 补偿脚本的工程化：四个必备属性

```text
dry-run      能在不写任何数据的前提下，算出"影响多少行、哪些行、为什么"
可回滚       每一步都有逆操作，且逆操作能定位到"这一批"（batch_no）
有审计       谁跑的、什么版本、改了什么、前后快照
有审批       超过阈值必须双人；阈值按"影响金额"而不是"影响行数"
```

**补偿脚本的 manifest（YAML）**：脚本本体可以是一段 SQL，但**元信息必须是结构化的**，否则你的"审批"批的是一段没人看得懂的 SQL。

```yaml
# compensations/2026-08-14-refund-stuck.yaml
id: comp-2026-08-14-refund-stuck
title: 修复 8/14 退款卡单（REFUNDING 超 6h 且无在途任务）
ticket: OPS-4471
author: zhangsan
reviewed_by: [lisi, wangwu]          # 双人评审，缺一不许上线
risk: HIGH                            # LOW/MEDIUM/HIGH，决定审批层级
approval:
  required_roles: [owner, finance]    # 必须两个角色都点
  min_approvers: 2
  expire_hours: 24                    # 审批 24h 未执行自动作废，防止"批了忘了"
scope:
  datasource: refund_db
  select: |
    SELECT id FROM refund_order
     WHERE status = 'REFUNDING'
       AND updated_at < '2026-08-14 09:00:00'
       AND NOT EXISTS (SELECT 1 FROM outbox o
                        WHERE o.aggregate_id = refund_order.id AND o.status = 'SENDING')
  expected_rows: {min: 3000, max: 3600}   # 影响面守卫：超出区间直接拒绝执行
  max_amount: 3000000                     # 影响金额上限（分），超过必须走变更评审
plan:
  - step: mark
    sql: |
      UPDATE refund_order
         SET status = 'RERUN_PENDING', version = version + 1, updated_at = NOW(3)
       WHERE id IN (:ids) AND status = 'REFUNDING'
    expect_affected: ":scope_count"       # 断言：影响行数必须等于 scope 数
  - step: enqueue
    action: mq.publish
    topic: refund.retry
    rate_limit: 50/s
    body:
      refund_id: "${id}"
      idem_key: "comp-2026-08-14:${id}"
rollback:
  - step: revert_mark
    sql: |
      UPDATE refund_order
         SET status = 'REFUNDING', version = version + 1
       WHERE id IN (:ids) AND status = 'RERUN_PENDING'
    guard: "只回滚还没被消费的（outbox 里没有对应记录）"
verify:
  - name: no_stuck_left
    sql: |
      SELECT COUNT(*) FROM refund_order
       WHERE id IN (:ids) AND status = 'RERUN_PENDING'
    expect: 0
  - name: money_reconciled
    sql: |
      SELECT COUNT(*) FROM refund_order r
        LEFT JOIN ledger_entry l ON l.biz_no = r.biz_no AND l.type = 'REFUND'
       WHERE r.id IN (:ids) AND r.status = 'REFUNDED' AND l.id IS NULL
    expect: 0
```

**执行器骨架（bash，可直接改成 Python）**：它做的事只有四件——**校验审批、算影响面、分批执行、写审计**。

```bash
#!/usr/bin/env bash
# comp-run.sh <manifest.yaml> [dry-run|execute|rollback]
set -euo pipefail
MANIFEST=${1:?manifest required}
MODE=${2:-dry-run}
BATCH=${BATCH:-200}

# 1) 解析 manifest（yq 读字段；生产上建议用 Go/Python 解析成强类型）
ID=$(yq -r '.id' "$MANIFEST")
RISK=$(yq -r '.risk' "$MANIFEST")
MIN_ROWS=$(yq -r '.scope.expected_rows.min' "$MANIFEST")
MAX_ROWS=$(yq -r '.scope.expected_rows.max' "$MANIFEST")
SELECT_SQL=$(yq -r '.scope.select' "$MANIFEST")

# 2) 审批校验：未审批的脚本一票否决（不是"提示"，是 exit 1）
if [ "$MODE" = "execute" ]; then
  APPR=$(curl -fsS "http://approval.internal/api/v1/approvals?biz_id=${ID}" \
         | jq -r 'select(.status=="approved") | .id' | head -n1)
  [ -n "${APPR:-}" ] || { echo "REFUSE: no approved ticket for $ID"; exit 1; }
  echo "approved by ticket $APPR"
fi

# 3) 影响面计算 + 守卫（dry-run 与 execute 都要跑，防止 scope 漂移）
IDS=$(mysql -N -B refund_db -e "$SELECT_SQL")
COUNT=$(printf '%s\n' "$IDS" | grep -c . || true)
echo "scope=$COUNT expected=[$MIN_ROWS,$MAX_ROWS]"
if [ "$COUNT" -lt "$MIN_ROWS" ] || [ "$COUNT" -gt "$MAX_ROWS" ]; then
  echo "REFUSE: scope out of range (possible data drift)"; exit 2
fi
if [ "$MODE" = "dry-run" ]; then
  printf '%s\n' "$IDS" | head -n 20
  echo "DRY-RUN OK: would update $COUNT rows in batches of $BATCH"
  exit 0
fi

# 4) 分批执行：每批独立事务 + 每批写审计（中途挂了也知道改到哪）
BATCH_NO="${ID}-$(date +%Y%m%d%H%M%S)"
echo "$IDS" | split -l "$BATCH" - /tmp/comp.$$
for f in /tmp/comp.$$*; do
  IDLIST=$(paste -sd, "$f")
  mysql refund_db <<SQL
START TRANSACTION;
UPDATE refund_order
   SET status='RERUN_PENDING', version=version+1, updated_at=NOW(3)
 WHERE id IN (${IDLIST}) AND status='REFUNDING';
INSERT INTO intervention_audit
  (intervention_no, aggregate_type, aggregate_id, action, approval_id, operator,
   operator_ip, reason_code, reason_text, before_state, after_state,
   before_snapshot, script_version, idem_key, dry_run, batch_no, trace_id)
VALUES ('IV-${BATCH_NO}-$(basename "$f")', 'refund', 0, 'RERUN', '${APPR:-NONE}',
        'svc:comp-run', '$(hostname -I | awk '{print $1}')', 'STUCK_TIMEOUT',
        'compensate stuck refund, see ${ID}', 'REFUNDING', 'RERUN_PENDING',
        JSON_OBJECT('batch','${BATCH_NO}'), '${ID}', '${ID}:$(basename "$f")',
        0, '${BATCH_NO}', '${TRACE_ID:-00000000000000000000000000000000}');
COMMIT;
SQL
  echo "batch $(basename "$f") done"
done
rm -f /tmp/comp.$$*
```

> ⚠️ **上面审计那条 INSERT 的 `aggregate_id` 写 0 是偷懒**，生产上要**逐行写**（用 `INSERT ... SELECT` 从临时表展开），否则"哪一行被改过"仍然查不到。**审计的粒度必须等于数据的粒度**，不能等于"批次"。

---

## 33.5 双修对照：卡住的订单 vs 卡住的 Run

| 维度 | 传统系统（订单/退款） | Agent 系统（Run） | 相同 / 差异根因 |
| --- | --- | --- | --- |
| **机制本体** | SLA 扫描 + 分级 + 人工处置 | 同构：扫描 `RUNNING/WAITING_*` 超时 | **完全相同**，巡检代码可以一份两边跑 |
| **唯一键/身份** | `biz_no`（退款单号） | `run_id` + `step_id` | 结构相同 |
| **"卡住"的定义** | 非终态 + 超 SLA + 无在途动作 | 同上，但多一类：**在跑但在烧钱**（死循环） | Agent 多一个"成本速度"维度 |
| **重跑的代价** | 一次 RPC，几毛钱 | **重新调模型，几块到几十块** | 差异根因：成本显性且按量 |
| **重跑的前提** | 幂等 + 可定位 + 无在途 | 同上 + **必须命中模型结果缓存** | Agent 多一层缓存 |
| **跳过的后果** | 账实不符（可补） | 产物缺失/质量降级，**且可能没人发现** | 差异根因：输出面向人 |
| **审计内容** | 谁/何时/为什么/改了什么 | 加：**改了哪段上下文、重放了几次、花了多少 token** | 指标集扩展 |
| **验证方法** | 对账（钱）+ 巡检   发现率 | 对账（token/调用次数）+ 产物断言 | 框架相同，核对物变了 |
| **可视化** | 流程节点 + 停留时长 | 加：每步的 token/成本/工具成功率 | 指标扩展 |

**哪些部分完全相同，可以直接复用**：

1. **`sla_config` / `stuck_alert` / `intervention_audit` 三张表**——把 `flow_key` 换成 `agent_type`，字段一个不用改。
2. **keyset 分页 + 单实例选主 + 告警去重**——巡检的并发安全问题与业务无关，代码级复用。
3. **重跑的三条前提**（幂等/可定位/无在途）——Agent 侧只是多了一条"预算检查"。
4. **"跳过必须有补偿任务 + 打标记"**——Agent 跳过一个工具调用后，产物就是不完整的，必须让下游知道。

**Agent 特有的四条**：

| 特有 | 现象 | 对策 |
| --- | --- | --- |
| **"在跑"不等于"在进展"** | Run 状态是 `RUNNING`，但连续 20 步都在调同一个工具 | 加"步数/成本速度"指标：5 分钟内 token 增速 > 阈值且无新产物 → 判卡 |
| **重跑 = 重复花钱** | 一次重跑 3,200 个 Run，账单多 4 万 | 重跑前必须校验预算；命中缓存的部分不重新计费 |
| **跳过 = 产物降级** | 跳过"事实核查"步骤，输出可能带幻觉 | 跳过必须打 `degraded=true`，并在输出里显式声明 |
| **审计要能解释"为什么"** | 只记录"重启了 Run"没用，要能回答"模型当时看到了什么" | 审计里存上下文摘要 hash + checkpoint 版本 |

---

## 33.6 会在哪炸：失效模式清单

| # | 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- | --- |
| 1 | 卡单 9 天没人发现 | 只有成功率指标，没有停留时长 | `SELECT state, MAX(age) FROM ... GROUP BY state` | 建 SLA 表 + 巡检 | 每个非终态必须有 SLA（第 30 章评审表第 9 条） |
| 2 | 巡检漏掉一批单 | 用了 `OFFSET` 分页，期间数据变动导致跳行 | 对比扫描总数与 `COUNT(*)` | 改 keyset 分页 | 巡检的"应扫数 vs 实扫数"作为指标 |
| 3 | 同一笔单被告警 20 次 | 多实例巡检 + 没有去重表 | 看告警条数与去重表行数 | 加选主 + `uk_agg_bucket` | 去重表进代码库，不靠"记得" |
| 4 | 批量重跑把库打挂 | 一次性 5,000 笔、单事务 | 看 DB CPU/锁等待与重跑时间重合 | 分批 200 + 限速 | 重跑工具强制 `rate_limit`，不可配成 0 |
| 5 | 重跑退了两笔钱 | 幂等键每次重新生成 | 按业务键 group by 数分录 | 用 `(biz_no, step)` 做幂等键 | 幂等键必须由业务键派生，不能含时间戳 |
| 6 | 跳过后账不平 | 跳过只改状态，没挂补偿任务 | 对账：状态已完成的 vs 有分录的 | 补补偿任务 + 修数据 | 跳过接口强制要求 `compensation_task_id` |
| 7 | 审计查不到谁改的 | 有人直连数据库 | binlog + 账号审计 | 收回 DBA 写权限，走工具 | 应用账号只有 `INSERT` 权限，无 `UPDATE/DELETE` |
| 8 | 回滚脚本把别人的改动也回滚了 | 回滚条件只有状态，没有 `batch_no`/`version` | 对比回滚前后的 version | 用 `batch_no` + 期望 version 回滚 | 每个干预都带 `batch_no`（表里已有） |
| 9 | 审批批了但没人执行 | 审批单没有有效期 | 查审批时间与执行时间差 | 加 `expire_hours` | 审批即执行（点同意直接触发），不搞人工复制粘贴 |

---

## 33.7 怎么验证：必须能被"故意弄坏"

### ① 故障注入：让 worker 停 30 分钟，看巡检多久发现

```bash
# 1) 停掉退款补偿 worker（模拟事故一）
kubectl scale deploy/refund-worker --replicas=0

# 2) 手工造一笔"卡单"：把一条记录的时间往前推
mysql refund_db -e "UPDATE refund_order SET updated_at = NOW() - INTERVAL 3 HOUR
                     WHERE biz_no = 'RF20260814001'"

# 3) 等一个巡检周期（1 分钟），断言告警已产生
sleep 70
mysql refund_db -e "SELECT aggregate_id, severity, notify_count, first_seen_at
                      FROM stuck_alert WHERE aggregate_type='refund'
                       ORDER BY id DESC LIMIT 5"
# 通过标准：severity=3，且 first_seen_at 在 90 秒内

# 4) 恢复并把数据改回去
kubectl scale deploy/refund-worker --replicas=2
```

### ② 重跑幂等：同一笔重跑 10 次，副作用必须只有 1 次

```bash
for i in $(seq 1 10); do
  curl -sS -X POST http://ops.internal/api/rerun \
    -H 'Content-Type: application/json' \
    -d "{\"id\":88421,\"operator\":\"test\",\"reason\":\"idempotency-check\",\"batchNo\":\"idem-$i\"}" \
    | jq -c '{outcome,detail}'
done

# 断言：账上只有一条退款分录
mysql refund_db -e "SELECT COUNT(*) FROM ledger_entry
                     WHERE biz_no='RF20260814001' AND type='REFUND'"
# 期望：1（不是 10）
```

### ③ 审计完备性：每次状态变更都必须有对应的干预记录或系统日志

```sql
-- 找出"状态变了但没有系统事件、也没有人工审计"的行（这是最强的脏数据信号）
SELECT r.id, r.biz_no, r.status, r.updated_at
  FROM refund_order r
 WHERE r.updated_at >= NOW() - INTERVAL 7 DAY
   AND NOT EXISTS (SELECT 1 FROM state_transition_log l
                    WHERE l.aggregate_id = r.id
                      AND l.created_at >= r.updated_at - INTERVAL 5 SECOND)
   AND NOT EXISTS (SELECT 1 FROM intervention_audit a
                    WHERE a.aggregate_id = r.id
                      AND a.created_at >= r.updated_at - INTERVAL 5 SECOND)
 LIMIT 100;
-- 期望：0 行。任何一行都意味着"有人绕过了状态机改数据"（事故二就是这个形状）
```

### ④ 观测指标

| 指标 | 类型 | 用途 | 告警阈值 |
| --- | --- | --- | --- |
| `stuck_total{flow,state,severity}` | Gauge | 卡单存量 | P0 > 0 持续 5 分钟 |
| `stuck_oldest_age_seconds{flow}` | Gauge | 最老卡单年龄 | > hard_seconds |
| `stuck_scan_duration_seconds` | Histogram | 巡检自身耗时 | P99 > 30s（说明分页失效） |
| `stuck_scan_is_leader` | Gauge | 是否有实例在选主 | 为 0 持续 3 分钟 = 巡检挂了 |
| `rerun_total{result}` | Counter | 重跑结果分布 | `DUPLICATE` 突增（幂等键设计有问题） |
| `manual_intervention_total{action,operator}` | Counter | 人工干预频率 | 单日 > 50 次（说明系统性缺陷） |

> 💡 **最后这条最有价值**：`manual_intervention_total` 突增，说明**系统有个洞，被人用人力堵着**。你该去修洞，而不是表扬那个堵洞的人。

---

## 33.8 用什么工具

| 能力 | 自研（推荐） | 现成方案 | 注意 |
| --- | --- | --- | --- |
| 卡单巡检 | 定时任务 + SLA 表 + 本文 SQL | 各引擎自带的 timeout/timer（Temporal 的 `WorkflowTimeout`、Argo 的 `activeDeadlineSeconds`） | 引擎只管"自己的实例"，业务状态卡住它看不见 |
| 单实例选主 | MySQL `GET_LOCK` / PG `pg_try_advisory_lock` | K8s Lease / etcd | K8s Lease 更稳（有续约语义），但要额外依赖 |
| 重跑工具 | 一个带 dry-run 的 HTTP 接口 + 审计表 | 引擎的 `reset`/`retry` API | 引擎的 retry 常常"从头开始"，不是"从某步开始" |
| 补偿脚本管理 | Git 仓库 + manifest YAML + 审批系统 | 数据库变更工具（Flyway/Liquibase）不适合（它们是 schema 迁移） | 补偿脚本必须是**一次性、可审计、可回滚**的，不能塞进常规迁移 |
| 运营界面 | 内部工具平台（低代码即可） | 商业 BPM 控制台 | 必须有 dry-run 与审批入口，不能只有"改状态" |

---

## 33.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 卡单定义 | 非终态 + 超 SLA + **无在途推进动作**，三个条件缺一不可 |
| 监控盲区 | "没发生"不会报错。必须有 `state_age` 与 `stuck_total`，不能只有成功率 |
| SLA | 按「流程 × 状态」配，不要全局；由业务正常时长反推 |
| 扫描 | keyset 分页（`id > lastId`），不要 `OFFSET`；否则漏行且越扫越慢 |
| 选主 | DB 会话锁（`GET_LOCK`/advisory lock）+ 专用连接；不要靠"只部署一个副本" |
| 告警 | 去重表 + `uk_agg_bucket`，同一笔单不重复轰炸 |
| 重跑三前提 | 幂等、可定位、无在途；缺一条就不要批量 |
| 重跑工具 | dry-run 必填、分批、限速、可续跑、逐笔独立事务 |
| 跳过 | **资金/合规/对账/外部写 绝对不能跳**；跳过必须有补偿任务 + 标记 |
| 审计 | 谁/何时/为什么/前后快照/脚本版本/批次号；dry-run 也要记 |
| 反模式 | 直连数据库改状态 —— 它把"判断"伪装成"事实"，是最严重的撒谎 |
| 双修 | 巡检三张表可原样复用到 Agent；Agent 多"烧钱式卡住"与"重跑=重复花钱" |

## 33.10 自检题

1. **一个订单在 `PAYING` 停留了 40 分钟，P99 是 3 分钟。它一定是卡单吗？你要看哪三个东西才能下结论？**
   > 要点：不一定。三件事：① **SLA**——`PAYING` 的 SLA 是 30 分钟还是 2 小时？② **有没有在途动作**——支付网关的请求还在（`outbox.status='SENDING'`、worker 持有租约），那就是"慢"不是"卡"；③ **是否终态可达**——如果支付回调已经失败但状态没推进，那才是真卡。只看时间会导致大量误报，误报多了告警就会被忽略，然后真卡单被淹没。

2. **为什么"跳过"必须创建补偿任务？不创建会怎样？举个具体例子。**
   > 要点：跳过的本质是"这一步的副作用没发生，但我把状态标成完成了"。没有补偿任务，这个"没发生"就永久变成了"没发生且没人知道"。例：跳过了"库存回补"，订单显示已退款，但库存没加回来——超卖 200 件，直到下次盘点才发现，而这期间已经卖了。补偿任务的价值不是"一定会执行"，而是**把"欠的账"显式化，让它进入一个有 SLA 的队列**，而不是消失在状态字段里。

3. **批量重跑为什么要逐笔独立事务，而不是一个大事务？**
   > 要点：三条。① 大事务锁行时间长，会阻塞线上正常交易（重跑本该是后台动作）；② 一笔失败（比如某笔已被别的流程推进）会导致整批回滚，前面 4,999 笔白跑；③ 大事务在 binlog/undo 上开销巨大，MySQL 上可能直接触发 `innodb_log_file_size` 不足。逐笔事务 + `batch_no` 审计，既能续跑，也能定位"改到第几笔挂了"。

## 33.11 延伸阅读

- 上一章：[32. 编排引擎选型](32-workflow-engine-selection.md)
- 下一章：[34. Agent 场景的状态流转与扩展点](34-agent-state-flow.md)
- 相关：[30. 状态机建模与落库](30-state-machine-modeling.md)（迁移日志与 SLA 评审）、[31. DAG 编排](31-dag-orchestration.md)（租约回收）、[29. 编排是什么](29-orchestration-vs-choreography.md)（终态负责人）
- 幂等与补偿：[2. 幂等的实现光谱](2-idempotency-implementations.md)、[13. Saga 三连坑](13-saga-pitfalls.md)、[14. 对账体系](14-reconciliation-system.md)
- 运营视角：[43. 运营体系：工单、客户健康与补偿](43-operation-system.md)（补偿的审批与账务联动）
- 平台侧：[../llm/12-long-task-durable.md](../llm/12-long-task-durable.md)（检查点与租约）、[../llm/34-hitl-approval.md](../llm/34-hitl-approval.md)（审批即两阶段提交）
- 官方文档（以版本为准）：MySQL 8.0 `GET_LOCK` / `FOR UPDATE SKIP LOCKED` / `DATE_SUB` + `INTERVAL` 表达式；PostgreSQL `pg_try_advisory_lock`；Temporal 的 `WorkflowExecutionTimeout` / `WorkflowRunTimeout`；Argo 的 `activeDeadlineSeconds`
- 总纲：[0-summary.md](0-summary.md)
