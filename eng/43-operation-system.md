# 43. 运营体系：工单、客户健康与补偿

> **状态**：`已完成（初稿）` ｜ **所属**：第七部分 · 运维与运营 ｜ **编号**：43 / 52
> **本章定位**：运维管「服务活着」，运营管「客户满意」。本章给三件可落地的系统：**工单**（问题怎么进来、怎么不丢）、**客户健康分**（谁在流失、为什么）、**补偿与配额**（怎么给、给多少、怎么不重复给）。
> **读法**：§43.4 的工单 DDL 与 SLA 算式可直接用；§43.5 的健康分模型要给业务方校准；§43.6 的补偿审批流是资金安全的关键。

---

## 43.1 事故现场：87 张工单，两周无人闭环，赔了 3.2 万

2025 年 9 月，某 SaaS 因为一次限流策略上线（第 15 章），把某个大客户的 API 配额从 10,000 QPS 改成了 1,000（案例数字为示例）。技术侧认为「只影响 3% 的请求」，但客户侧的真实体验是：

| 客户 | 现象 | 工单 | 处理 |
| --- | --- | --- | --- |
| A（年费 120 万） | 批量同步任务持续 429 | 41 张 | 客服回复「已反馈技术」 |
| B | 报表加载超时 | 23 张 | 转技术，技术说「限流是产品策略」 |
| C | 定时任务失败，数据缺 2 天 | 15 张 | 无回应 → 升级到销售 |
| 其他 | 零散 | 8 张 | 合并处理 |

两周后：

- **87 张工单，闭环 31 张（36%）**，其中 A 客户的 41 张只回了 3 张。
- A 客户以「SLA 违约」为由要求赔偿，最终退费 **3.2 万元** + 承诺扩容。
- C 客户在续费前选择了竞品（年费 48 万流失）。
- 复盘发现：**技术侧有完整的限流监控，运营侧完全不知道**；工单系统里也没有「同一客户多张相似工单」的聚合。

> 🩸 **这次事故的本质不是限流，是「技术信号到不了运营，运营信号回不到技术」**。两边各自的系统都是好的，中间断了一根线。

---

## 43.2 精确定义：运营体系是三个闭环

```text
① 问题接入闭环：客户问题 → 工单 → 分派 → 处理 → 回复 → 满意度
② 主动预防闭环：用量/错误/工单/续费信号 → 健康分 → 干预 → 验证
③ 事后补偿闭环：影响判定 → 方案 → 审批 → 执行 → 账务 → 通知
```

**三个闭环的共同要求**：**可追溯、有时限、有 owner**。缺任何一个，就会退化成「客服转发机器」。

### 与运维的边界（第 41 章的落地）

| 问题 | 归谁 | 判据 |
| --- | --- | --- |
| 服务不可用 | 运维 | 影响面是「所有用户」 |
| 单个客户报错 | 运营（转技术） | 影响面是「某个租户」 |
| 客户不会用 | 运营 | 产品/文档问题 |
| 客户被限流 | 运营 + 技术 | 需要解释 + 可能调额 |
| 数据错误 | 运营 + 技术 + 财务 | 可能涉及补偿 |

---

## 43.3 结构图：工单的全生命周期

```text
 客户提交（Web/邮件/API/IM）
        │
        ▼
 ┌──────────────┐
 │ 自动分类      │  规则 + 模型：账单/技术/功能/投诉/安全
 │ 自动定级      │  影响面 × 紧急度 × 客户等级
 └──────┬───────┘
        ▼
 ┌──────────────┐   超时未认领
 │ 分派（技能组）│──────────────▶ 升级到组长
 └──────┬───────┘
        ▼
 ┌──────────────┐   需要技术
 │ 一线处理      │──────────────▶ 二线/研发（带 trace_id、租户、时间范围）
 └──────┬───────┘
        ▼
 ┌──────────────┐
 │ 回复 + 关闭   │──▶ 满意度评价（1~5）
 └──────┬───────┘
        ▼
   如果涉及影响 → 触发补偿流程（§43.6）
        │
        ▼
   同类工单聚合 → 进入问题管理（Problem）→ 改进项
```

**关键设计：工单（Ticket）≠ 问题（Problem）**。同一个根因可能产生 87 张工单；**必须把它们聚合成一个 Problem**，否则永远在打地鼠。

---

## 43.4 怎么实现：工单系统

### 表结构（完整 DDL）

```sql
CREATE TABLE ticket (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    ticket_no     VARCHAR(32) NOT NULL UNIQUE,       -- TK-20260915-0001
    tenant_id     BIGINT NOT NULL,
    requester_id  BIGINT NOT NULL,
    channel       ENUM('WEB','EMAIL','API','IM','PHONE') NOT NULL,
    category      VARCHAR(32) NOT NULL,              -- BILLING/TECH/FEATURE/COMPLAINT/SECURITY
    sub_category  VARCHAR(64),
    priority      ENUM('P1','P2','P3','P4') NOT NULL,
    status        ENUM('NEW','ASSIGNED','IN_PROGRESS','PENDING_CUSTOMER','RESOLVED','CLOSED','REOPENED') NOT NULL,
    subject       VARCHAR(256) NOT NULL,
    description   TEXT,
    trace_id      VARCHAR(64),                       -- 便于技术排查
    assignee      VARCHAR(64),
    group_name    VARCHAR(64),
    problem_id    BIGINT,                            -- 聚合到问题
    first_response_at DATETIME(3),
    resolved_at   DATETIME(3),
    closed_at     DATETIME(3),
    sla_due_at    DATETIME(3) NOT NULL,              -- 按工作日历算
    sla_breached  TINYINT NOT NULL DEFAULT 0,
    satisfaction  TINYINT,                            -- 1~5
    created_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    KEY idx_tenant (tenant_id, created_at),
    KEY idx_status_due (status, sla_due_at),
    KEY idx_problem (problem_id)
);

CREATE TABLE ticket_event (                          -- 全量审计：谁在何时改了什么
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    ticket_id  BIGINT NOT NULL,
    actor      VARCHAR(64) NOT NULL,
    action     VARCHAR(32) NOT NULL,                 -- CREATE/ASSIGN/REPLY/STATUS/NOTE
    from_value VARCHAR(128),
    to_value   VARCHAR(128),
    note       TEXT,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    KEY idx_ticket (ticket_id, created_at)
);

CREATE TABLE problem (                               -- 一个根因，多个工单
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    title         VARCHAR(256) NOT NULL,
    root_cause    TEXT,
    status        ENUM('OPEN','MITIGATED','CLOSED') NOT NULL,
    owner         VARCHAR(64) NOT NULL,
    incident_id   BIGINT,
    affected_tenants INT NOT NULL DEFAULT 0,
    created_at    DATETIME(3) NOT NULL,
    closed_at     DATETIME(3)
);
```

### SLA 计算（必须用工作日历，不能用自然日）

```text
SLA 到期时间 = 计时起点 + 该优先级的目标时长（按工作日历顺延）

计时起点默认是「工单创建时间」：首次响应与解决两个时钟都从创建起算；
只有合同明确约定「解决时钟从首次响应起算」时才改用首次响应时间。

工作日历要处理：
- 周末/法定节假日（中国：调休，不能简单按周六日）
- 客户所在时区（跨境）
- 暂停（PENDING_CUSTOMER 期间不计时）
```

```java
public class SlaCalculator {
    private final BusinessCalendar calendar;   // 含节假日与工作时间

    /** 从 start 起，加上 businessMinutes 个工作分钟，返回到期时间 */
    public Instant dueAt(Instant start, Duration target) {
        long remaining = target.toMinutes();
        Instant cursor = calendar.nextBusinessMoment(start);   // 若在非工作时间，跳到下一个工作时刻
        while (remaining > 0) {
            long available = calendar.businessMinutesUntilClose(cursor); // 距当日下班还有多少工作分钟
            if (available >= remaining) {
                return calendar.plusBusinessMinutes(cursor, remaining);
            }
            remaining -= available;
            cursor = calendar.nextBusinessDayOpen(cursor);
        }
        return cursor;
    }

    /** 暂停期间不计时：恢复时把 dueAt 顺延暂停时长 */
    public Instant extendForPause(Instant dueAt, Duration paused) {
        return dueAt.plus(paused);   // 简化；严格实现应按工作日历顺延
    }
}
```

### SLA 矩阵（按优先级 × 客户等级）

| 优先级 | 首次响应 | 解决 | 升级点 | 适用 |
| --- | --- | --- | --- | --- |
| **P1** | 15 分钟 | 4 小时 | 30 分钟未响应→组长；2 小时未解决→总监 | 核心功能不可用/数据丢失/安全 |
| **P2** | 1 小时 | 1 个工作日 | 4 小时未响应→组长 | 功能受损但有绕过 |
| **P3** | 4 小时 | 3 个工作日 | 1 天未响应→组长 | 一般问题 |
| **P4** | 1 个工作日 | 5 个工作日 | — | 咨询/建议 |

**客户等级修正**（同一优先级，VIP 客户目标时长 × 0.5）：

```sql
-- 计算工单的 SLA 到期时间（示例：P1=240 工作分钟，VIP 减半）
SELECT t.id,
       DATE_ADD(t.created_at, INTERVAL
           CASE t.priority WHEN 'P1' THEN 240 WHEN 'P2' THEN 480
                           WHEN 'P3' THEN 1440 ELSE 2400 END
           * IF(c.tier='VIP', 0.5, 1) MINUTE) AS sla_due_at
FROM ticket t JOIN customer c ON c.tenant_id = t.tenant_id;
-- 生产实现必须用工作日历函数，而不是 DATE_ADD
```

### 自动分类与聚合（规则优先，模型兜底）

```python
RULES = [
    (r"(发票|账单|退款|扣费|价格)", "BILLING"),
    (r"(报错|超时|失败|无法登录|打不开|(?<!\d)5\d{2}(?!\d))", "TECH"),
    (r"(希望|能否支持|建议增加)", "FEATURE"),
    (r"(投诉|态度|欺骗|投诉到|监管)", "COMPLAINT"),
    (r"(泄露|越权|漏洞|被攻击)", "SECURITY"),
]

def classify(subject: str, body: str) -> str:
    text = subject + " " + body
    for pat, cat in RULES:
        if re.search(pat, text):
            return cat
    return "UNKNOWN"     # 交给人/模型，不要硬猜

def priority(cat: str, tenant_tier: str, affected_users: int) -> str:
    if cat == "SECURITY": return "P1"
    if cat == "TECH" and affected_users > 1000: return "P1"
    if cat == "BILLING" and tenant_tier == "VIP": return "P2"
    if cat == "COMPLAINT": return "P2"
    return "P3"
```

**工单聚合**（把 87 张变 1 个 Problem）：

```sql
-- 同一租户、同类别、24 小时内的高频工单 → 建议合并
SELECT tenant_id, category, COUNT(*) AS cnt,
       MIN(id) AS first_ticket, MAX(created_at) AS last_at
FROM ticket
WHERE created_at >= NOW() - INTERVAL 1 DAY
  AND status NOT IN ('CLOSED')
GROUP BY tenant_id, category
HAVING COUNT(*) >= 5
ORDER BY cnt DESC;
```

> 💡 **聚合阈值不是拍脑袋**：`≥5 张/24h/同租户同类别` 是一个起点，用历史数据校准（看多少次聚合能覆盖 80% 的批量故障）。

---

## 43.5 客户健康分：谁在流失，为什么

### 模型（可解释、可校准，不要用黑盒）

```text
HealthScore = 100 - 扣分项 + 加分项

扣分项（权重可按业务调）：
  错误率       错误请求/总请求 > 1%        -20 × min(1, 错误率/10%)
  工单          P1/P2 工单数（近 30 天）    -10 × min(3, P1数) - 3 × min(5, P2数)
  用量下滑     日活/调用量环比 < -30%       -15
  未闭环工单    超 SLA 未闭环               -5 × min(4, 数量)
  续费风险     距到期 < 30 天且未续         -20
  负面反馈      满意度 ≤ 2 的工单占比        -10 × 占比

加分项：
  用量增长     > +20%                      +10
  深度使用     使用 ≥3 个核心功能           +5
  成功案例     有公开案例/推荐              +5

分档：≥80 健康 | 60~79 观察 | 40~59 风险 | <40 高危
```

### 计算代码（每天跑一次，落快照表）

```go
type Signals struct {
    TenantID      int64
    TotalReq      int64
    ErrorReq      int64
    P1Tickets     int
    P2Tickets     int
    OverdueTickets int
    UsageChange   float64 // -1.0 ~ +1.0 环比
    DaysToRenew   int
    LowSatRatio   float64
    FeatureCount  int
}

func Score(s Signals) (int, map[string]int) {
    detail := map[string]int{}
    score := 100

    errRate := 0.0
    if s.TotalReq > 0 { errRate = float64(s.ErrorReq) / float64(s.TotalReq) }
    if errRate > 0.01 {
        d := int(20 * math.Min(1, errRate/0.10))
        detail["error_rate"] = -d; score -= d
    }
    if s.P1Tickets > 0 {
        d := 10 * min(3, s.P1Tickets); detail["p1_tickets"] = -d; score -= d
    }
    if s.P2Tickets > 0 {
        d := 3 * min(5, s.P2Tickets); detail["p2_tickets"] = -d; score -= d
    }
    if s.UsageChange < -0.30 { detail["usage_drop"] = -15; score -= 15 }
    if s.OverdueTickets > 0 {
        d := 5 * min(4, s.OverdueTickets); detail["overdue"] = -d; score -= d
    }
    if s.DaysToRenew >= 0 && s.DaysToRenew < 30 { detail["renew_risk"] = -20; score -= 20 }
    if s.LowSatRatio > 0 { d := int(10 * s.LowSatRatio); detail["low_sat"] = -d; score -= d }

    if s.UsageChange > 0.20 { detail["usage_growth"] = +10; score += 10 }
    if s.FeatureCount >= 3 { detail["deep_usage"] = +5; score += 5 }

    if score < 0 { score = 0 }
    if score > 100 { score = 100 }
    return score, detail
}
```

**存储与趋势**：

```sql
CREATE TABLE health_snapshot (
    tenant_id   BIGINT NOT NULL,
    snapshot_at DATE NOT NULL,
    score       TINYINT NOT NULL,
    band        ENUM('HEALTHY','WATCH','RISK','CRITICAL') NOT NULL,
    detail      JSON NOT NULL,
    PRIMARY KEY (tenant_id, snapshot_at),
    KEY idx_band (band, snapshot_at)
);
```

### 健康分的三个使用纪律

| 纪律 | 说明 |
| --- | --- |
| **可解释** | 每次扣分必须能说出「因为什么」，`detail` 字段就是给客户成功看的 |
| **可校准** | 用「已流失客户」回溯，看他们的分数在流失前 90 天是否下降（AUC > 0.7 才有预测力） |
| **可行动** | 每个分档对应明确动作（高危 → 客户成功 24h 内联系 + 技术复盘） |

```sql
-- 校准：流失客户在流失前的分数轨迹（应显著下降）
SELECT h.tenant_id, h.snapshot_at, h.score
FROM health_snapshot h
JOIN churned_tenant c ON c.tenant_id = h.tenant_id
WHERE h.snapshot_at BETWEEN c.churned_at - INTERVAL 90 DAY AND c.churned_at
ORDER BY h.tenant_id, h.snapshot_at;
```

---

## 43.6 补偿与配额：怎么给、给多少、不重复给

### 补偿的三类来源

| 来源 | 例子 | 审批 | 账务 |
| --- | --- | --- | --- |
| **SLA 违约** | P1 超过 4 小时 | 自动（按合同） | 退费/抵扣 |
| **主动关怀** | 大客户故障后 | 客户成功 + 财务 | 代金券 |
| **投诉安抚** | 严重投诉 | 主管 | 代金券/延长服务期 |

### 状态机（必须持久化，不能靠人记）

```text
DRAFT → PENDING_APPROVAL → APPROVED → EXECUTING → DONE
                 │                        │
                 ├→ REJECTED              └→ FAILED → RETRY / MANUAL
                 └→ CANCELLED
```

```sql
CREATE TABLE compensation (
    id             BIGINT AUTO_INCREMENT PRIMARY KEY,
    comp_no        VARCHAR(32) NOT NULL UNIQUE,
    tenant_id      BIGINT NOT NULL,
    source         ENUM('SLA','CARE','COMPLAINT') NOT NULL,
    ref_type       VARCHAR(32),          -- INCIDENT/TICKET
    ref_id         BIGINT,
    comp_type      ENUM('CASH','CREDIT','VOUCHER','EXTEND') NOT NULL,
    amount         BIGINT NOT NULL,      -- 分 / 天数
    currency       CHAR(3) NOT NULL DEFAULT 'CNY',
    status         ENUM('DRAFT','PENDING_APPROVAL','APPROVED','EXECUTING','DONE','FAILED','REJECTED','CANCELLED') NOT NULL,
    idempotency_key VARCHAR(64) NOT NULL UNIQUE,   -- ★ 防重复发放
    approver       VARCHAR(64),
    approved_at    DATETIME(3),
    executed_at    DATETIME(3),
    ledger_entry_id BIGINT,              -- 关联账务分录（第 3 章）
    reason         TEXT NOT NULL,
    created_by     VARCHAR(64) NOT NULL,
    created_at     DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    KEY idx_tenant (tenant_id, created_at),
    KEY idx_status (status, created_at)
);
```

### 金额上限与审批矩阵

| 单笔金额 | 审批人 | 时限 |
| --- | --- | --- |
| ≤ 500 元 | 一线客服主管 | 即时 |
| 500 ~ 5,000 元 | 客服经理 | 4 小时 |
| 5,000 ~ 50,000 元 | 运营总监 + 财务 | 1 个工作日 |
| > 50,000 元 | CEO / 财务负责人 | 2 个工作日 |

> 🩸 **必须有上限校验**：`amount > tenant.monthly_fee * 2` 时强制升级审批，防止「一个客服把年费全退了」。

### 执行（幂等 + 账务联动）

```go
func (s *CompService) Execute(ctx context.Context, compNo string) error {
    comp, err := s.repo.GetByNo(ctx, compNo)
    if err != nil { return err }
    if comp.Status != StatusApproved { return ErrInvalidState }

    // 幂等：CAS 抢占，防止并发/重试重复发放
    ok, err := s.repo.CASStatus(ctx, comp.ID, StatusApproved, StatusExecuting)
    if err != nil { return err }
    if !ok { return nil }   // 已被别的协程执行

    // 写账务（同一事务，或 outbox）
    entryID, err := s.ledger.PostCompensation(ctx, comp)
    if err != nil {
        _ = s.repo.CASStatus(ctx, comp.ID, StatusExecuting, StatusFailed)
        return err
    }
    // 发券/退费（外部系统，幂等键 = comp_no）
    if err := s.billing.GrantCredit(ctx, comp.TenantID, comp.Amount, comp.CompNo); err != nil {
        _ = s.repo.CASStatus(ctx, comp.ID, StatusExecuting, StatusFailed)
        return err
    }
    return s.repo.Complete(ctx, comp.ID, entryID)
}
```

### 通知（补偿必须告知，否则白给）

| 场景 | 渠道 | 内容要点 |
| --- | --- | --- |
| SLA 违约 | 邮件 + 站内信 | 影响时段、原因（脱敏）、补偿金额、到账时间 |
| 主动关怀 | 客户成功 1v1 | 道歉 + 补偿 + 后续改进 |
| 批量影响 | 公告 + 定向 | 不点名、不泄露细节、给自助查询入口 |

---

## 43.7 批量公告与定向通知

```text
公告分级：
  L1 全平台（状态页 + 站内信 + 邮件）—— 影响 > 10% 租户
  L2 分群（按套餐/地域/功能）—— 影响部分租户
  L3 定向（单个租户）—— 客服 1v1

发送策略：
  - 频控：同一租户 24h 内 ≤ 2 条公告（防骚扰）
  - 幂等：公告 ID + 租户 ID 唯一
  - 可退订：非事务性通知必须可退订
  - 可追溯：谁发的、什么时候、发给了谁、打开率
```

```sql
CREATE TABLE announcement (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    title       VARCHAR(256) NOT NULL,
    body        TEXT NOT NULL,
    level       ENUM('L1','L2','L3') NOT NULL,
    segment     JSON,                 -- {"plan":["pro"],"region":["cn"]}
    starts_at   DATETIME NOT NULL,
    ends_at     DATETIME NOT NULL,
    status      ENUM('DRAFT','SCHEDULED','SENT','CANCELLED') NOT NULL,
    created_by  VARCHAR(64) NOT NULL
);

CREATE TABLE announcement_delivery (
    announcement_id BIGINT NOT NULL,
    tenant_id       BIGINT NOT NULL,
    channel         ENUM('EMAIL','INBOX','IM') NOT NULL,
    status          ENUM('PENDING','SENT','FAILED','READ') NOT NULL,
    sent_at         DATETIME(3),
    PRIMARY KEY (announcement_id, tenant_id, channel)
);
```

---

## 43.8 双修对照：Agent 运营的特殊工单

| 维度 | 传统 SaaS | Agent 平台 | 相同 / 差异根因 |
| --- | --- | --- | --- |
| **工单类型** | 账单/技术/功能/投诉 | 以上 + **内容质量投诉、额度争议、越权/合规** | 多三类 |
| **技术排查入口** | trace_id + 日志 | 以上 + **run_id + step 序列 + prompt 版本** | 多一层 |
| **「不能用」的定义** | 5xx/超时 | 以上 + **答非所问、拒答过多、输出被审计拦截** | 质量即可用性 |
| **补偿形式** | 退费/代金券 | 以上 + **token 额度返还、延长试用** | 成本单位不同 |
| **健康分信号** | 用量/错误/工单 | 以上 + **任务成功率、采用率、成本超支** | 指标扩展 |
| **解释责任** | 「系统故障」 | **必须能解释「为什么这么答」**（引用/轨迹） | 可解释性要求 |
| **风险** | 服务中断 | 以上 + **模型输出造成客户损失**（错误建议） | 责任边界更复杂 |

**Agent 运营的三条新增纪律**：

1. **质量工单必须能定位到 Run**：工单里带 `run_id`，运营能一键拉起「这次回答用了哪些检索片段、哪个模型、哪个 prompt 版本」。没有这个，质量投诉永远无法闭环。
2. **额度争议要有明细**：客户说「这个月怎么花了这么多」，要能按 Run/功能拆出 token 明细；否则只能免单。
3. **内容投诉要有处置通道**：涉及违规/侵权的输出，要有「下架内容 + 修正知识库 + 通知客户」的流程，不能只回复「模型可能出错」。

---

## 43.9 会在哪炸：八个坑

| # | 坑 | 现象 | 根因 | 怎么修 |
| --- | --- | --- | --- | --- |
| 1 | 工单不聚合 | 同一个故障 87 张工单 | 无 Problem 概念 | 自动聚合 + 关联 incident |
| 2 | SLA 用自然日 | 周五提交，周一就算违约 | 未用工作日历 | 工作日历 + 节假日表 |
| 3 | 无 first_response 记录 | 无法证明是否及时 | 只记 resolved_at | 每次回复写 event |
| 4 | 健康分黑盒 | 销售不认、客户不服 | 不可解释 | 输出扣分明细 |
| 5 | 补偿无幂等 | 同一事故退两次 | 无唯一键 | `idempotency_key` + CAS |
| 6 | 补偿无上限 | 客服越权退全款 | 无审批矩阵 | 金额分级审批 |
| 7 | 公告骚扰 | 客户退订/投诉 | 无频控 | 24h ≤ 2 条 + 可退订 |
| 8 | 质量工单无 run_id | 无法复盘 | 工单与执行链路脱节 | 提交时自动带 trace/run |

---

## 43.10 怎么验证

| 指标 | 定义 | 目标 |
| --- | --- | --- |
| 首次响应达标率 | 在 SLA 内首次响应 / 总工单 | ≥ 95% |
| 解决达标率 | 在 SLA 内解决 / 总工单 | ≥ 90% |
| 一次解决率（FCR） | 无需二次提交 / 总工单 | ≥ 70% |
| 满意度（CSAT） | 平均分 | ≥ 4.5/5 |
| 工单聚合率 | 归入 Problem 的工单 / 总量 | ≥ 30%（故障期） |
| 健康分预测力 | 流失客户 90 天前分数下降的 AUC | ≥ 0.7 |
| 补偿准确率 | 无重复/无超额的补偿 / 总补偿 | 100% |
| 自助解决率 | 通过自助/文档解决 / 总咨询 | ≥ 50% |

```sql
-- 每周运营看板
SELECT DATE_FORMAT(created_at,'%Y-%u') AS wk,
       COUNT(*) AS tickets,
       ROUND(AVG(TIMESTAMPDIFF(MINUTE, created_at, first_response_at)),1) AS avg_frt_min,
       ROUND(SUM(sla_breached)/COUNT(*),3) AS breach_rate,
       ROUND(AVG(satisfaction),2) AS csat
FROM ticket
WHERE created_at >= NOW() - INTERVAL 8 WEEK
GROUP BY wk ORDER BY wk;
```

---

## 43.11 用什么工具

| 能力 | 工具 | 说明 |
| --- | --- | --- |
| 工单 | Zendesk / Freshdesk / 自研 | 自研要保证 SLA 与审计 |
| 客户成功 | Gainsight / 自研 + 数仓 | 健康分是核心 |
| 公告 | 站内信 + 邮件服务（SES/阿里云邮件） | 注意频控与退订 |
| 补偿 | 自研 + 账务系统 | 幂等与审批是重点 |
| 客服知识库 | 与 RAG 共用（第 49 章） | 自助率的关键 |

---

## 43.12 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 三闭环 | 问题接入 / 主动预防 / 事后补偿，都要「可追溯、有时限、有 owner」 |
| 边界 | 全站不可用→运维；单租户→运营；数据错→三方联动 |
| Ticket ≠ Problem | 一个根因 87 张工单，必须聚合成 Problem |
| SLA | 必须用**工作日历**，VIP 目标 × 0.5，暂停期间不计时 |
| 健康分 | 可解释（扣分明细）+ 可校准（流失回溯 AUC）+ 可行动（分档动作） |
| 补偿 | `idempotency_key` + CAS + 金额分级审批 + 账务联动 |
| 公告 | 频控（24h ≤ 2）+ 幂等 + 可退订 + 可追溯 |
| Agent | 工单带 `run_id`；额度可拆明细；内容投诉有处置通道 |
| 一句话 | **运营不是「回复客户」，是「让问题不重复发生，并让客户看见你在改」。** |

## 43.13 自检题

1. **为什么 SLA 必须用工作日历？举一个用自然日会算错的例子。**
   > 要点：周五 17:00 提交 P2（1 个工作日），自然日算法会在周六 17:00 就判违约；实际应从周一 09:00 开始计时。跨境客户还涉及时区，法定节假日还有调休。正解：节假日表 + 工作时间 + 时区，且 PENDING_CUSTOMER 暂停要顺延。

2. **客户健康分如果不给扣分明细，会发生什么？**
   > 要点：销售不认可（「凭什么说我的客户有风险」）、客户成功无法行动（不知道该改什么）、模型无法校准（错了也不知道错在哪）。正解：每次输出 `detail`，列出每项扣分与依据；用流失客户回溯校准权重；分档对应明确动作。

3. **一次故障影响 200 个租户，客服要给每个租户补 100 元。怎么保证不重复、不超额？**
   > 要点：① 生成补偿单时用 `idempotency_key = incident_id + tenant_id + comp_type` 唯一约束；② 执行时 CAS 抢状态（APPROVED→EXECUTING）；③ 账务写分录带同一唯一键；④ 总额校验（≤ 预算/不超过月费倍数）；⑤ 事后对账：补偿单数 vs 账务分录数 vs 实际发放数。

## 43.14 延伸阅读

- 上一章：[42. 运维体系](42-ops-system.md)
- 下一章：[44. 支撑系统：自助运维与对外状态页](44-self-service-and-status-page.md)
- 相关：[41. 运维与运营边界](41-ops-vs-operation.md)、[45. 一次故障的两份报告](45-incident-two-reports.md)、[14. 对账体系](14-reconciliation-system.md)
- 平台侧：[../llm/27-billing-reliability.md](../llm/27-billing-reliability.md)（计费可靠性）、[../llm/44-multi-tenant.md](../llm/44-multi-tenant.md)（多租户）
- 参考：ITIL 4 的事件/问题管理；Zendesk SLA 政策文档；Gainsight 客户健康分实践
- 总纲：[0-summary.md](0-summary.md)
