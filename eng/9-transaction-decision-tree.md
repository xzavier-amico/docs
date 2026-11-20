# 9. 事务决策树：什么场景用什么

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · 事务与一致性 ｜ **编号**：9 / 52
> **本章定位**：选型不是「哪个方案先进」，而是「哪条约束先把你逼到墙角」。这一章给你一棵能走完的决策树、一张判定表、四个真实场景的完整选型，以及四种最常见的选型反模式。
> **读法**：先看 §9.3 的树，再用 §9.4 的四个场景对号入座；§9.6 的反模式清单建议直接放进架构评审 checklist。

---

## 9.1 引子：两个「选型选错」的现场

**事故一：把 90 秒的物流调用包进全局事务（2024-10-11）**

某电商的履约服务为了「保证订单和物流单一致」，把「创建订单 → 调用第三方物流下单 → 写物流单」整体包进 Seata AT 全局事务。第三方物流接口平时 800ms，那天下午超时到 90 秒。

- 全局锁从订单创建一直持有到物流返回，**平均 92 秒**。
- 订单库 200 个连接，170 个卡在全局事务里；下单接口 P99 从 220ms 涨到 45 秒。
- 18 分钟里 1,700 笔订单超时，其中 640 笔出现「订单已建、物流单没建」；人工补单 + 赔付共 **92 万元**。
- 更讽刺的是：第三方物流本身就不支持回滚，Seata 的全局回滚只能回滚订单库，物流那边的单已经下了。

**事故二：Saga 只写了正向，没写补偿（2025-01-06）**

旅游平台「机票 + 酒店」套餐，Saga 正向三步：锁机票 → 锁酒店 → 扣款。酒店锁失败时，代码只打了日志，没有释放机票。当天 260 个客户机票被占、套餐没成，航空公司 48 小时后自动释放，客诉 91 起，赔付 **38 万元**。

> 🎯 两个事故的共同点：**不是技术不会用，是选型时没先问「这条链路到底能不能回滚、能接受多长时间的中间态」。** 决策树的价值就是逼你先回答这两个问题。

---

## 9.2 到底是什么：决策树是「排除法」，不是「方案大全」

**定义**：事务选型 = 在「一致性强度、事务时长、参与者可控性、吞吐与延迟、补偿能力」五个约束下，找到**代价最小且失败可收敛**的方案。

它不是让你把六种方案都试一遍，而是用一串问题快速排除掉不成立的选项。先钉死四个常见误解：

| 你以为的选型 | 为什么错 | 不做会怎样 |
| --- | --- | --- |
| 「跨服务就必须上分布式事务」 | 很多跨服务调用只是**查询/通知**，不需要原子性；真正需要原子性的写集合往往可以缩小 | 把只读调用、日志、通知全包进事务，锁时间放大 10 倍 |
| 「最终一致 = 降级、不可靠」 | 最终一致是**有承诺的收敛**：不丢、不重、可补偿、可对账 | 不做 Outbox/幂等/对账，最后变成「永远不一致」 |
| 「TCC 比 Saga 高级」 | TCC 要求资源可预留、参与者可改；Saga 适合长流程、不可预留的资源 | 给一个 3 天的审批流上 TCC，预留资源锁 3 天 |
| 「XA 最安全，上就完了」 | XA 只覆盖支持 XA 的数据库，且把锁持有到 commit | 链路里一个第三方支付，XA 就变成「半截强一致」 |

**先问三个边界问题，比选方案重要**：

1. **最小写集合是什么？** 把「必须一起成功」的写操作列出来；能合并到同一个库的，先合并。
2. **中间态能被业务接受多久？** 0（强一致）、100ms~5s（准实时）、分钟~天（长流程），答案直接砍掉一半方案。
3. **失败后能不能补偿？** 能补偿（取消、退款、释放）才配谈最终一致；不能补偿的，只能前置校验或串行化。

用三定律过一遍：**状态机**决定你有没有恢复能力；**稀缺资源**决定瓶颈落在锁、连接还是补偿队列；**不可回滚**决定 2PC 能不能覆盖整条链路。

---

## 9.3 结构图：一棵能走完的决策树

```
Q0. 这些写操作是否真的必须原子？
 │     （把通知、日志、可异步的派生数据先剔出去）
 ├─ 否 → 不需要分布式事务：本地事务 + 异步事件 + 对账，结束
 └─ 是
     │
Q1. 是否在同一个数据库、同一个连接？
 ├─ 是 → 本地事务（第 7 章），结束
 └─ 否
     │
Q2. 是否跨服务？
 ├─ 否（同库多表 / 多数据源）→ 同库多表用本地事务；
 │      多数据源且必须原子 → XA（仅限短事务、低并发、同厂商 DB）
 └─ 是
     │
Q3. 业务能接受多长的中间态？
 ├─ 不能接受（强一致，秒级）→ Q4
 ├─ 100ms ~ 5s → Q5
 └─ 分钟 ~ 天（长流程）→ Saga / 编排引擎（Temporal、Seata Saga）
     │
Q4. 所有参与者都能改代码，且资源可预留？
 ├─ 是 → TCC（库存/余额/券）；链路短、价值高
 └─ 否 → 放弃强一致：改流程（预扣/冻结/串行化）或缩小事务边界
     │
Q5. 链路里有没有第三方/不可控系统？
 ├─ 有 → 可靠消息 + 状态机 + 对账 + 最大努力通知
 │       （跨机构转账、支付回调、外部履约）
 └─ 无 → Q6
     │
Q6. 数据量与延迟要求？
 ├─ 高吞吐、100ms~5s、事件扇出 → Outbox + MQ（第 10 章）
 ├─ 低延迟、单库、要求不丢 → Outbox + CDC（Debezium）
 └─ 强顺序、按聚合根 → Outbox + 按 aggregate_id 分区
     │
Q7. 有没有补偿能力与对账兜底？
 ├─ 有 → 上线，并配置积压/补偿/差异告警
 └─ 没有 → 不要上线；先补补偿和对账，再谈最终一致
```

**判定表**（把树压成一张能贴进设计文档的表）：

| 场景特征 | 首选 | 备选 | 明确禁用 | 关键判据 |
| --- | --- | --- | --- | --- |
| 同库多表 | 本地事务 | — | 任何分布式方案 | 一个连接能搞定的事，别引入协调者 |
| 同库多数据源、短事务、低并发 | XA | 本地事务 + 对账 | Saga | 事务 <100ms、无外部副作用 |
| 跨服务、资源可预留、秒级 | TCC | 可靠消息 + 状态机 | XA | 库存/余额可冻结、参与者可改 |
| 跨服务、高吞吐、可接受 1~5s | Outbox + MQ | 事务消息 | XA | 事件可异步、消费端能幂等 |
| 跨服务、长流程、可补偿 | Saga/编排 | 状态机 + 事件 | TCC | 步骤分钟~天、每步有 Undo |
| 有第三方支付/银行 | 可靠消息 + 状态机 + 对账 | 最大努力通知 | XA/TCC 包住第三方 | 第三方不可控、只能查询/冲正 |
| 缓存与库 | 缓存旁路 + 失效 + CDC | 延迟双删 | 强一致 | 缓存是派生数据，不是真相源 |
| Agent 工具链 | Step 本地事务 + Outbox + Saga | Temporal 检查点 | 2PC | 工具副作用不可回滚 |

---

## 9.4 四个典型场景的完整选型

### 9.4.1 订单 + 库存：防超卖，但不许把下单拖死

**约束**：订单库和库存库分开；峰值 30,000 TPS；热门 SKU 单行每秒上千次扣减；超卖是资金事故；用户最多等 3 秒。

**选型**：`Redis 预扣（Lua）→ 订单本地事务 + Outbox → 库存消费幂等扣减 → 预留超时释放 → 对账`。热门 SKU 可升级为 TCC 预留。

**理由**：

- 库存是**可预留资源**，天然适合「先冻结、后确认」；但全链路 TCC 三接口成本高，普通 SKU 用「Redis 挡 + DB 条件更新」足够。
- 数据库是真相源：`UPDATE stock SET qty=qty-? WHERE sku_id=? AND qty>=?` 是防超卖的最后一道墙；Redis 只是减少无效请求。
- 订单创建与 `order.created` 事件同事务写 Outbox，库存服务消费；消费端用 `order_no` 唯一索引幂等。
- 预留要有 TTL：订单 30 分钟未支付，释放库存；释放动作也要幂等。

```sql
-- 库存扣减：表达式写 + 条件更新，RR/RC 下都不会超卖
UPDATE stock
   SET qty = qty - 2, version = version + 1, updated_at = NOW(3)
 WHERE sku_id = 9001 AND qty >= 2;

-- 预留表：TCC/预扣模式共用，xid 或 order_no 唯一
CREATE TABLE stock_reservation (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  biz_no      VARCHAR(64)     NOT NULL COMMENT 'order_no 或 tcc xid',
  sku_id      BIGINT UNSIGNED NOT NULL,
  qty         INT             NOT NULL,
  status      ENUM('FROZEN','CONFIRMED','RELEASED') NOT NULL,
  expire_at   DATETIME(3)     NOT NULL,
  created_at  DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_biz_sku (biz_no, sku_id),
  KEY idx_expire (status, expire_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

> ⚠️ 不要用「先查库存再更新」：查询与更新之间的窗口足够让 200 个并发请求同时看到 `qty=1`。必须把判断写进 `WHERE`。

### 9.4.2 支付 + 账务：第三方不可控，账必须平

**约束**：支付渠道回调可能重复、乱序、延迟；账务必须借贷平衡；支付成功不能丢；可以接受 1~3 秒的入账延迟。

**选型**：`回调验签 + 支付单本地事务 + Outbox(payment.succeeded) → 账务消费（唯一 biz_no + 复式分录）→ 渠道对账`。内部余额冻结可用 TCC，但**不要试图用 XA 包住第三方支付**。

**理由**：

- 第三方支付没有 XA 接口，最多给你「查询 + 退款/冲正」；强一致只能做到「我方支付单」这一层。
- 支付单与事件同事务落库，保证「支付成功」不丢；账务消费用 `(biz_no, entry_no)` 唯一索引，重复回调不新增分录（借贷两条整体幂等）。
- 复式记账：一笔支付 = 借「银行存款」、贷「商户待结算」，金额相等；任何单边分录都是脏数据。
- 日终用渠道对账文件核对，差异进差异表（详见第 14 章）。

```sql
-- 账务分录：biz_no 唯一，借贷必须成对
CREATE TABLE ledger_entry (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  biz_no       VARCHAR(64)     NOT NULL COMMENT '支付单号/退款单号',
  entry_no     VARCHAR(64)     NOT NULL COMMENT '分录号；冲正可用 REV...',
  account_id   BIGINT UNSIGNED NOT NULL,
  direction    ENUM('DEBIT','CREDIT') NOT NULL,
  amount       DECIMAL(18,2)   NOT NULL,
  currency     CHAR(3)         NOT NULL DEFAULT 'CNY',
  remark       VARCHAR(128)    NULL,
  created_at   DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_biz_entry (biz_no, entry_no),
  KEY idx_account_time (account_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 入账：同一事务里插借贷两条，任何一条冲突则整体回滚
INSERT INTO ledger_entry(biz_no, entry_no, account_id, direction, amount)
VALUES ('PAY202506180001', 1, 1001, 'DEBIT', 299.00),
       ('PAY202506180001', 2, 2001, 'CREDIT', 299.00);
```

### 9.4.3 跨机构转账：你控制不了对方，只能控制状态机

**约束**：对方是银行/清算机构；接口可能超时、可能成功但没响应；监管要求可追溯；用户看到「处理中」是正常的。

**选型**：`状态机（INIT → DEBIT_SUCCESS → CREDIT_PENDING → SUCCESS/FAILED）+ 可靠消息 + 主动查询 + 冲正 + 对账 + 人工挂账`。**不要用 TCC**：你无法冻结对方账户；也不要用 XA：对方根本不参与。

**理由**：

- 跨机构的核心不是「原子提交」，而是「**任何一步都能被查询、被冲正、被对账**」。
- 先记「受理成功」，再异步推进；每一步状态迁移都带条件更新，防止并发重复推进。
- 对方超时：不能假设失败，必须查询；查询也失败就保持 `CREDIT_PENDING`，进人工/对账，不允许自动退款（可能对方已入账）。
- 冲正而不是删除：保留原分录，反向记一笔，审计可追溯。

```sql
-- 转账状态机：条件更新，谁先推进谁赢
UPDATE transfer_order
   SET status = 'CREDIT_PENDING', version = version + 1, updated_at = NOW(3)
 WHERE transfer_no = 'TR202506180001'
   AND status = 'DEBIT_SUCCESS';

-- 卡单巡检：超过 10 分钟仍在处理中的，进人工/查询队列
SELECT transfer_no, status, retry_count, updated_at
  FROM transfer_order
 WHERE status IN ('INIT','DEBIT_SUCCESS','CREDIT_PENDING')
   AND updated_at < NOW() - INTERVAL 10 MINUTE
 ORDER BY updated_at
 LIMIT 500;
```

### 9.4.4 缓存与库：这不是分布式事务，是「派生数据什么时候失效」

**约束**：缓存是派生数据，数据库是真相源；允许 100ms~几秒的脏读；不能接受缓存永久脏。

**选型**：`Cache-Aside + 提交后删除缓存 + 延迟双删/CDC 失效 + 版本号/TTL`。**不要试图让缓存和库强一致**：那等于给每次读加一把分布式锁。

**理由**：

- 写路径：先写库、提交后删缓存；删除失败就靠 CDC/binlog 补偿，不能只做一次。
- 读路径：缓存 miss 读库回填；回填前检查版本号，避免旧值覆盖新值。
- 延迟双删解决「读请求在删缓存后、提交前把旧值回填」的窗口；CDC 是更可靠的兜底。
- 业务能接受「读到旧值」的前提要写清楚；不能接受的读（余额、库存）直接读库或走强一致接口。

```java
// 写路径：事务提交后删缓存；删除失败交给 CDC 兜底
@Transactional(rollbackFor = Exception.class)
public void updateProduct(Product p) {
    productMapper.update(p);
    TransactionSynchronizationManager.registerSynchronization(
        new TransactionSynchronization() {
            @Override public void afterCommit() {
                cacheDeleteWithRetry("product:" + p.getId()); // 失败进重试队列
            }
        });
}

// 读路径：回填时带版本，旧版本不覆盖新版本
public Product get(long id) {
    Product cached = redis.get("product:" + id);
    if (cached != null) return cached;
    Product db = productMapper.selectById(id);
    // setIfVersionGreater 为自研封装：Lua 比较版本后再写，避免旧值覆盖新值
    redis.setIfVersionGreater("product:" + id, db, db.getVersion(), Duration.ofMinutes(10));
    return db;
}
```

> 💡 缓存选型只问一句：**「这个读能不能接受 1 秒前的数据？」** 能，就用缓存旁路 + 失效；不能，就别缓存，或者缓存只做降级兜底。

---

## 9.5 双修对照：决策树在 Agent 侧多两个维度

传统决策树问「跨不跨库、能不能最终一致、有没有第三方」；Agent 还要多问两句：**这次模型调用能不能重放？这笔钱能不能退？**

| 维度 | 传统系统 | Agent 系统 | 同 or 异 |
| --- | --- | --- | --- |
| 事务单位 | 一次业务请求 / 一个本地事务 | **一个 Step**：一次模型调用或一次工具调用 | 异：粒度更细、数量更多 |
| 身份 / 唯一键 | `order_no`、`request_id` | `run_id` + `step_id` + `tool_call_id`；`step_id` 含工具名 + 参数 hash | 同构，键多一层 |
| 强一致可行性 | 可用 XA/TCC 锁资源 | **模型不可 prepare、不可回滚**；只能对「预算/配额」做 TCC | 异：2PC 出局 |
| 长流程 | Saga/编排，分钟~天 | 同样是 Saga/编排，但每一步可能**重新规划** | 同框架，路径不确定 |
| 失败模式 | 空回滚、悬挂、消息丢 | 上述全部 + 模型已返回但结果没落库、工具已执行但 Step 失败、token 已扣但 Run 失败 | 多一类不可回滚副作用 |
| 验证方法 | 金额对账 | 金额 + **token/调用次数对账** + 输出质量抽检 | 框架相同，口径扩展 |
| 成本结构 | 机器折旧 | 每次调用按量计费；重试 = 重复花钱 | 异：成本显性 |

**完全相同、可直接复用**：决策树的骨架（先缩小写集合、再问一致性、再问补偿）、Outbox、幂等表、Saga 状态机、对账框架。**Agent 不需要重新发明一套，只需要把「资源」换成「预算/配额」，把「金额」换成「token/调用次数」。**

Agent 侧的选型映射：

| Agent 场景 | 选型 | 理由 |
| --- | --- | --- |
| 调用下单/退款/发邮件等工具 | Step 本地事务 + Outbox + 工具补偿 | 副作用不可回滚，只能补偿 |
| 调用付费模型 | **至少一次 + 结果缓存/幂等键**，不做 TCC | 模型不能预留，重复调用就是重复计费 |
| 预扣 token 预算 | TCC：Try 冻结额度 → Confirm 扣减 → Cancel 释放 | 预算是可预留资源，和余额同构 |
| 多工具长任务 | Temporal/自研 Saga + 检查点 | 秒~分钟级，随时中断，必须能恢复 |
| 多 Agent 协作 | 编排（第 29 章）+ Outbox 事件 | 谁拥有「下一步」的决定权要明确 |
| 工具调用结果对账 | 工具侧流水 vs 本地 Step 记录 | 发现「调了没记 / 记了没调」 |

```go
// 把决策树编码成函数：输入约束，输出方案。生产上用于设计评审，不是运行时
type Constraints struct {
	SameDB            bool
	SameService       bool
	StrongConsistency bool
	Reservable        bool
	HasThirdParty     bool
	LongRunning       bool
	AcceptLag         time.Duration
}

func Decide(c Constraints) string {
	switch {
	case c.SameDB:
		return "LOCAL_TX"
	case c.SameService && c.StrongConsistency && !c.LongRunning && !c.HasThirdParty:
		return "XA" // 同一服务、多个数据源、短事务、无第三方
	case !c.SameService && c.StrongConsistency && c.Reservable && !c.LongRunning:
		return "TCC"
	case !c.SameService && c.StrongConsistency && !c.Reservable:
		return "REDESIGN_BOUNDARY" // 缩小写集合或改流程
	case c.LongRunning:
		return "SAGA"
	case c.HasThirdParty:
		return "RELIABLE_MSG+STATE_MACHINE+RECONCILIATION"
	case c.AcceptLag >= 100*time.Millisecond:
		return "OUTBOX+MQ"
	default:
		return "OUTBOX+CDC"
	}
}
```

---

## 9.6 会在哪炸：选型反模式清单

| 反模式 | 现场长什么样 | 根因 | 正解 |
| --- | --- | --- | --- |
| **分布式事务包一切** | 把 RPC、发短信、第三方调用、日志都包进全局事务；锁 90 秒，连接池打满 | 把「需要原子性的写」和「可异步的副作用」混在一起 | 先缩小写集合；副作用移出事务，用 Outbox/补偿 |
| **TCC 滥用** | 低价值、短链路也写 Try/Confirm/Cancel 三套接口；4 个服务 12 个端点，3 个月没上线 | 把 TCC 当「高级方案」，忽略预留资源与改造成本 | 只有「资源可预留 + 价值高 + 链路短」才用 TCC |
| **无补偿的 Saga** | 正向失败只打日志；机票占了、酒店没订；用户投诉才发现 | 把 Saga 当「异步流程」，忘了 Undo | 每个正向步骤必须配可重复、可审计的补偿；没有补偿就不要用 Saga |
| **用 XA 做长事务** | 事务里夹第三方调用/人工审批；prepared 分支锁几十分钟 | 把 XA 当「强一致万能钥匙」 | 长事务改 Saga；XA 只用于 <100ms 的纯 DB 短事务 |
| **把最终一致当异步** | 发完 MQ 就返回，没有 Outbox、没有幂等、没有对账 | 只做了「异步」，没做「一致」 | 四件套：Outbox + 幂等 + 补偿 + 对账 |
| **缓存当真相源** | 库存只写 Redis，DB 异步落；Redis 挂了下单全乱 | 分不清「派生数据」和「权威数据」 | DB 是真相源；缓存只做加速/兜底 |

**每条反模式的「不做会怎样」**：包一切 → 锁时间放大 10 倍；TCC 滥用 → 三套接口的 bug 面 ×3；无补偿 Saga → 部分成功永远歪着；XA 长事务 → in-doubt 锁库；假最终一致 → 静默错误；缓存当真相 → 数据丢失不可恢复。

---

## 9.7 怎么验证：用场景压测，而不是用 PPT 评审

**① 决策表评审**：每个跨服务写链路填一行：`写集合 / 中间态时长 / 参与者可控性 / 补偿动作 / 对账口径`。填不出来的，方案不成立。

**② 故障注入矩阵**（每个方案至少测三种）：

| 方案 | 注入 | 期望 |
| --- | --- | --- |
| TCC | Try 成功后 Confirm 超时 | 不重复扣、不空回滚；查状态后幂等 Confirm |
| Saga | 第二步失败 | 第一步被补偿；补偿失败进重试队列并告警 |
| Outbox | 投递器 kill | 重启后继续投递；消费端只生效一次 |
| 可靠消息 | 生产端发送超时 | 消息不丢（Outbox/事务消息兜底），下游最终收到 |
| 缓存 | 删缓存失败 | CDC/延迟双删兜底；TTL 内收敛 |

**③ 压测判据**：2 倍峰值下 P99 ≤ 基线 2 倍；热点行锁等待不抬升；Outbox 最老事件年龄 < 5 秒（或业务约定值）；补偿队列长度 < 100。

**④ 观测指标**：

| 指标 | 含义 | 告警线 |
| --- | --- | --- |
| `tx_decision_total{plan}` | 各方案实际使用量 | 与设计不符即查 |
| `saga_running_age_seconds` | 流程停留时长 | > 业务 SLA |
| `outbox_oldest_age_seconds` | 事件积压年龄 | > 5 分钟 |
| `compensation_failed_total` | 补偿失败数 | 10 分钟 > 0 |
| `reconciliation_diff_rate` | 对账差异率 | > 0.01% |

```yaml
# 按业务线声明一致性策略，部署时校验；防止「口头最终一致，代码里没有 Outbox」
consistency:
  order_create:
    plan: OUTBOX_MQ
    max_lag: 5s
    idempotency_key: order_no
    compensation: inventory_release
  payment_ledger:
    plan: OUTBOX_MQ
    max_lag: 3s
    idempotency_key: biz_no
    reconciliation: channel_file_daily
  transfer:
    plan: STATE_MACHINE
    max_lag: 10m
    manual_queue: transfer_pending
  cache_product:
    plan: CACHE_ASIDE
    max_stale: 2s
    fallback: db_read
```

---

## 9.8 用什么工具

| 需求 | 工具 | 选型建议 |
| --- | --- | --- |
| 短事务强一致（纯 DB） | MySQL XA / ShardingSphere XA | 只在低并发、<100ms、无外部副作用时用 |
| TCC | Seata TCC | 参与者可改、资源可预留；三接口幂等要自己写 |
| Saga/长流程 | Temporal、Seata Saga、自研状态机 | 流程 >5 步或 >1 分钟，优先 Temporal |
| 可靠消息 | RocketMQ 事务消息、Kafka 事务 | 事务消息适合「本地事务 + 发消息」；Kafka 事务只覆盖 Kafka 内部 |
| Outbox 投递 | 自研轮询、Debezium | 轮询简单；CDC 低延迟但运维重（第 10 章） |
| 缓存一致性 | Redis + CDC + 延迟双删 | 不追求强一致；TTL 是最后兜底 |
| 对账 | 自研 + ClickHouse | 任何最终一致方案的必备兜底（第 14 章） |

> ⚠️ 选型工具的第一原则：**先确定「谁是真相源」**。真相源只能有一个；其它存储都是派生数据，派生数据只能靠「重建 + 对账」保证，不可能靠分布式事务保证。

---

## 9.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 选型顺序 | 先缩小写集合 → 再问一致性时长 → 再问参与者可控性 → 最后选方案 |
| 同库 | 本地事务；别引入协调者 |
| 短事务强一致 | XA（纯 DB、低并发）或 TCC（资源可预留） |
| 长事务 | Saga/编排；每步必须有补偿 |
| 高吞吐最终一致 | Outbox + MQ；消费端幂等 |
| 第三方 | 可靠消息 + 状态机 + 对账；不要 XA/TCC 包住对方 |
| 缓存 | 派生数据；Cache-Aside + 失效 + CDC；不追求强一致 |
| 反模式 | 包一切、TCC 滥用、无补偿 Saga、XA 长事务 |
| Agent 扩展 | 多问「模型能否重放、成本能否退」；Step 本地事务 + Outbox + 检查点 |
| 不做会怎样 | 锁时间放大、三套接口 bug×3、部分成功永远歪着、静默错误没人发现 |

## 9.10 自检题

1. **订单库和库存库分开，峰值 30,000 TPS，要求不超卖、用户 3 秒内看到结果。你选什么？为什么不是 XA？**
   <details><summary>答案要点</summary>
   选 Redis 预扣 + DB 条件更新 + 订单本地事务/Outbox + 库存消费幂等 + 预留超时释放。XA 在热点 SKU 上锁 100ms+，单 SKU 吞吐被压到个位数，连接池也扛不住 30,000 TPS；而且库存扣减是可预留资源，没必要用全局锁。
   </details>

2. **跨机构转账，对方超时没响应，你能直接退款吗？**
   <details><summary>答案要点</summary>
   不能。超时 ≠ 失败，对方可能已入账。应保持 `CREDIT_PENDING`，主动查询/对账，确认失败后才冲正；自动退款会造成「对方已入账 + 我方又退一笔」的双重支付。跨机构只能靠状态机 + 查询 + 对账 + 人工挂账。
   </details>

3. **为什么「缓存与库的一致性」不适合用 TCC/分布式事务？**
   <details><summary>答案要点</summary>
   缓存是派生数据，不是真相源；为每次读写加强一致会把读路径变成分布式锁，成本远超收益。正确做法是 Cache-Aside + 提交后失效 + 延迟双删/CDC + 版本号 + TTL，接受秒级脏读，并用「读库兜底」覆盖不能脏读的场景。
   </details>

## 9.11 延伸阅读

- Seata 官方文档：AT/TCC/Saga 模式与适用场景（以 1.7.x 为准）
- Temporal 文档：Workflow/Activity 的持久化执行与补偿模式
- MySQL 8.0 Reference Manual：*XA Transactions*、`SELECT ... FOR UPDATE` 与隔离级别
- 上一章：[8. 分布式事务全景：六种方案一次讲透](8-distributed-transaction-overview.md)
- 下一章：[10. 消息表（Outbox）与事务消息](10-outbox-and-transactional-message.md)
- 交叉引用：本地事务见第 7 章；Saga 三坑见第 13 章；对账见第 14 章；编排选型见第 29、32 章
