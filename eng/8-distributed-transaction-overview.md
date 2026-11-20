# 8. 分布式事务全景：六种方案一次讲透

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · 事务与一致性 ｜ **编号**：8 / 52
> **本章定位**：把六种分布式事务方案放在同一张坐标轴上比，而不是逐个背概念。每种方案只回答四个问题：**一致性有多强、代价有多大、边界在哪、失败时现场长什么样**。
> **读法**：§8.3 的六张时序图先扫一遍建立直觉；§8.4 的对比表直接贴进选型文档；§8.4.7 是给架构评审用的「为什么不用 XA」算账页。

---

## 8.1 引子：一次 XA 提交，锁了库存库 79 分钟

2025-06-18 00:07，某电商大促第 7 分钟。订单库和库存库分属两个 MySQL 8.0 实例，团队半年前上了 XA，理由很正当：**超卖是资金事故，必须强一致**。

- 00:07:12，订单服务在订单库 `XA PREPARE` 成功，库存库 `XA PREPARE` 也成功。协调者（订单服务 JVM）在写 commit 日志前遭遇一次 4.2 秒的 Full GC，紧接着被 K8s 滚动发布 `SIGKILL`。
- 两个库的 XA 事务停在 `PREPARED`，**库存行锁不释放**。`XA RECOVER` 里躺着 **6,842 个 in-doubt 事务**。
- 库存库 200 个连接很快被打满：新请求都卡在库存行锁上，P99 涨到 30 秒。（MySQL 8.0.28 及以前，PREPARED 分支还会占用原会话连接；8.0.29+ 默认 `xa_detach_on_prepare`，连接可复用，但行锁仍然不释放。）
- 00:45 人工介入，逐个 `XA COMMIT` / `XA ROLLBACK`。**已 PREPARE 的分支不会因为超时自动回滚**，必须人工裁决；未走到 PREPARE 的分支则可能已按本地事务提交或回滚。现场因此出现两种脏数据：**订单已支付、库存没扣** 和 **库存扣了、订单没建**。
- 01:26 恢复。79 分钟，12.6 万笔订单失败，直接损失 210 万元，1,900 笔需要人工修数。

> 🩸 这不是 XA 的实现 bug，是 XA 的模型在互联网高并发下的必然结果：**它把「原子提交」做成了「跨网络的长时间锁」**。这一章把六种方案摆在一起，就是为了让你在架构评审上说清「为什么不用它」。

---

## 8.2 到底是什么：分布式事务是「约束」，不是「框架」

**定义**：一个业务操作的结果需要落在两个及以上独立资源上（不同库、不同服务、MQ、第三方系统），且这些资源无法靠一个本地事务保证原子性，这时你面对的就是分布式事务。

它只有两个坐标轴：

- **一致性强度**：强一致（提交完成前外部看不到中间态）↔ 最终一致（允许中间态，但必须在可接受时间内收敛）。
- **控制位置**：资源层（数据库 XA）↔ 业务层（TCC / Saga / 消息 / 通知）。

六个方案是这两个坐标轴上的六个落点。先把「什么不是分布式事务」钉死：

| 你以为的分布式事务 | 为什么不是 | 不做会怎样 |
| --- | --- | --- |
| 同一个库、两张表 | 本地事务就能保证，`BEGIN...COMMIT` 足够 | 上 Seata AT 只是多了一次全局锁和 undo_log，白白降吞吐 |
| 事务提交后发一条 MQ | 这是**双写**，不是事务；提交与发送之间没有原子性 | 提交成功、发送失败 → 下游永远收不到；发送成功、提交回滚 → 下游收到幽灵消息 |
| 失败就无限重试 | 重试只解决「暂时失败」，不解决「部分成功」 | 没有幂等 → 重复扣款；没有补偿 → 已成功的分支永远歪着 |
| 最终一致 = 先返回再说 | 最终一致是**工程承诺**：Outbox + 幂等 + 补偿 + 对账，四件套缺一不可 | 没有对账的最终一致，等于「永远不一致也没人知道」 |

用三定律检验一遍，六种方案一个都跑不掉：

1. **状态机定律**：XA 的状态在数据库的 prepared 分支里，Saga 的状态在 saga 表里，Outbox 的状态在 outbox 表里。**没有状态记录，就没有恢复能力。**
2. **稀缺资源定律**：XA 锁的是数据库行和连接；TCC 锁的是预留额度；Saga 不锁资源，但消耗补偿能力；Outbox 消耗的是表和投递吞吐。选型就是选「你愿意让哪个资源成为瓶颈」。
3. **不可回滚定律**：支付、发券、发短信、调第三方，全都不可回滚。链路里有这些，2PC 直接出局：它只能回滚数据库。

---

## 8.3 结构图：六种方案的时序与失败点

```
① 2PC / XA（资源层，强一致，短事务）
 order-svc            order-db              inventory-db
    │ XA START            │                       │
    ├────────────────────>│                       │
    │ INSERT order         │                       │
    │ XA END / PREPARE     │                       │
    ├────────────────────>│  🔒 行锁 + 连接被占住   │
    │ XA START + UPDATE inventory                   │
    ├─────────────────────────────────────────────>│
    │ XA END / PREPARE                             │
    ├─────────────────────────────────────────────>│  🔒 行锁
    │ 两边都 PREPARE OK → 写 commit 日志（关键点）    │
    │ XA COMMIT ──────────>│                       │
    │ XA COMMIT ──────────────────────────────────>│
    │ 任一步失败 → 对所有已 prepare 分支 XA ROLLBACK  │
    │ ⚠️ 协调者死在「写日志」前后 → in-doubt，锁不释放 │

② TCC（业务层，业务级强一致，短事务）
 order-svc                 inventory-svc
    │ Try(冻结 1 件)            │
    ├──────────────────────────>│ INSERT reservation(status=FROZEN)
    │ OK                        │
    │ 本地事务：创建订单（PENDING）│
    │ Confirm                   │
    ├──────────────────────────>│ reservation → CONFIRMED
    │ 任一步失败 → Cancel 所有已 Try 的分支
    │ ⚠️ Cancel 先于 Try 到达 = 空回滚；Cancel 后 Try 重试 = 悬挂

③ Saga（业务层，最终一致，长事务）
 orchestrator      order-svc      inventory-svc      payment-svc
    │ create order     │                │                  │
    ├─────────────────>│                │                  │
    │ reserve stock                     │                  │
    ├──────────────────────────────────>│                  │
    │ charge                                               │
    ├─────────────────────────────────────────────────────>│
    │ 失败 → 反向补偿：refund → release stock → cancel order
    │ ⚠️ 没有隔离性：中间态对外可见，必须靠状态机挡住脏读

④ 可靠消息最终一致（消息层，最终一致）
 order-svc        order-db        MQ          inventory-svc
    │ BEGIN            │            │                │
    │ INSERT order     │            │                │
    │ COMMIT           │            │                │
    │ send msg ────────────────────>│                │
    │  ⚠️ 发送失败/超时 → 消息丢，订单已提交             │
    │                  │            ├───────────────>│ 消费 + 幂等
    │                  │            │  重投 → 可能重复 │

⑤ 本地消息表 / Outbox（DB 层，最终一致）
 order-svc                 order-db              relay            MQ
    │ BEGIN                     │                   │              │
    │ INSERT order              │                   │              │
    │ INSERT outbox_event       │                   │              │
    │ COMMIT                    │                   │              │
    │                           │<── 轮询 / CDC ────┤              │
    │                           │  status=NEW       │              │
    │                           ├──────────────────>│ publish ────>│
    │                           │  status=SENT      │              │
    │ ⚠️ 投递至少一次；消费者必须按 event_id 幂等       │              │

⑥ 最大努力通知（业务层，弱一致）
 biz-svc          notify-svc              第三方
    │ 业务完成        │                       │
    ├───────────────>│ 记录通知任务            │
    │                │ 重试 1 / 2 / 3 ...     │
    │                ├──────────────────────>│
    │                │ 200 / 超时 / 4xx        │
    │                │ 超过上限 → 人工 + 对账   │
    │ ⚠️ 不承诺送达；只承诺「尽力 + 可查 + 可补」  │
```

---

## 8.4 怎么实现：六种方案逐个拆

### 8.4.1 2PC / XA：数据库层的原子提交

**原理**：协调者先让所有参与者 `PREPARE`（锁资源、写 redo/undo），全部成功后写 commit 日志，再逐个 `COMMIT`。

| 维度 | 结论 |
| --- | --- |
| 一致性强度 | 强一致；提交完成前外部看不到中间态 |
| 性能成本 | 多 2 次网络往返；**从 prepare 到 commit 全程持锁**；连接占用看版本（8.0.28- 占原会话，8.0.29+ 默认 `xa_detach_on_prepare`）；热点行吞吐被锁时间卡死 |
| 适用边界 | 短事务（<100ms）、低并发、同厂商数据库、内部账务；链路里没有 MQ/第三方 |
| 失败表现 | 协调者崩溃 → in-doubt，锁不释放；参与者超时 → 启发式提交/回滚；网络分区 → 长时间阻塞 |

```sql
-- 完整可执行：两个库分别执行，应用层做协调者
-- 库 A：订单库
XA START 'tx-20250618-0001';
INSERT INTO orders(order_no, user_id, amount, status)
VALUES ('SO202506180001', 10086, 299.00, 'CREATED');
XA END 'tx-20250618-0001';
XA PREPARE 'tx-20250618-0001';

-- 库 B：库存库
XA START 'tx-20250618-0001';
UPDATE stock SET qty = qty - 1, version = version + 1
 WHERE sku_id = 9001 AND qty >= 1;
XA END 'tx-20250618-0001';
XA PREPARE 'tx-20250618-0001';

-- 两边都返回 OK 后，协调者必须先持久化 commit 决定，再提交：
XA COMMIT 'tx-20250618-0001';   -- 库 A、库 B 各执行一次

-- 协调者崩溃后的现场排查：列出所有 in-doubt 分支
XA RECOVER;
-- 输出含 formatID / gtrid_length / bqual_length / data
-- 人工确认后：XA COMMIT 'xid' 或 XA ROLLBACK 'xid'
```

> ⚠️ `XA RECOVER` 只给你「有哪些分支」，不告诉你「该提交还是回滚」。这个决定必须来自协调者的持久化日志；日志丢了，就只能靠业务对账反推。**这就是协调者单点的真正代价。**

### 8.4.2 TCC：把「锁」挪到业务层

**原理**：Try 预留资源（冻结库存、冻结余额、锁优惠券），Confirm 确认扣减，Cancel 释放预留。三个阶段都是业务接口，因此可以覆盖数据库之外、但**你自己能改**的资源。

| 维度 | 结论 |
| --- | --- |
| 一致性强度 | 业务级强一致：Confirm 之前资源只是冻结，不会被别人用掉 |
| 性能成本 | 3 次业务调用；需要预留表；比 XA 少了长事务数据库锁，但多了网络交互 |
| 适用边界 | 高价值、短链路、资源可预留（库存/余额/券）、所有参与者都能改代码 |
| 失败表现 | Try 超时但实际成功；Confirm 丢失；Cancel 先到（空回滚）；Cancel 后 Try 重试（悬挂）；补偿失败 |

```java
// Seata TCC 骨架：注解与 API 以 io.seata 1.7.x 文档为准
@LocalTCC
public interface InventoryTcc {

    @TwoPhaseBusinessAction(
        name = "deductStock",
        commitMethod = "confirm",
        rollbackMethod = "cancel")
    boolean tryDeduct(BusinessActionContext ctx,
                      @BusinessActionContextParameter(paramName = "skuId") long skuId,
                      @BusinessActionContextParameter(paramName = "qty") int qty);

    boolean confirm(BusinessActionContext ctx);
    boolean cancel(BusinessActionContext ctx);
}

@Service
public class InventoryTccImpl implements InventoryTcc {
    @Resource private ReservationMapper mapper;

    @Override
    public boolean tryDeduct(BusinessActionContext ctx, long skuId, int qty) {
        String xid = ctx.getXid();
        // 幂等 + 防悬挂：先判 CANCELLED 墓碑，再判已存在的预留
        if (mapper.isCancelled(xid)) return false;
        if (mapper.exists(xid)) return true;
        return mapper.insertReservation(xid, skuId, qty, "FROZEN") == 1;
    }

    @Override
    public boolean confirm(BusinessActionContext ctx) {
        return mapper.updateStatus(ctx.getXid(), "FROZEN", "CONFIRMED") >= 0; // 0 行也算成功
    }

    @Override
    public boolean cancel(BusinessActionContext ctx) {
        // 空回滚：Try 没来过也要留下 CANCELLED 墓碑，防止后续 Try 悬挂
        mapper.insertIgnoreCancelled(ctx.getXid());
        return mapper.updateStatus(ctx.getXid(), "FROZEN", "CANCELLED") >= 0;
    }
}
```

```sql
-- TCC 预留表：xid 唯一索引是幂等与防悬挂的地基
CREATE TABLE inventory_reservation (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  xid         VARCHAR(128)    NOT NULL COMMENT '全局事务 ID',
  sku_id      BIGINT UNSIGNED NOT NULL,
  qty         INT             NOT NULL,
  status      ENUM('FROZEN','CONFIRMED','CANCELLED') NOT NULL,
  created_at  DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at  DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_xid (xid),
  KEY idx_sku_status (sku_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 8.4.3 Saga：长事务的补偿链

**原理**：把长流程拆成 N 个本地事务，每个步骤配一个补偿动作；正向失败时按逆序执行已成功步骤的补偿。Saga 不锁资源、不保证隔离性，只保证**最终要么全部完成，要么全部被补偿**。

| 维度 | 结论 |
| --- | --- |
| 一致性强度 | 最终一致；中间态对外可见，必须用状态机/语义锁挡住脏读 |
| 性能成本 | N 次本地事务 + N 次补偿潜力；无全局锁，吞吐高；补偿逻辑是主要开发成本 |
| 适用边界 | 长流程（分钟到天）、跨团队/跨系统、步骤可补偿（取消订单、退库存、退款） |
| 失败表现 | 没有补偿 = 裸奔；补偿失败 = 卡单；补偿重复 = 多退；事件丢失 = 流程停在中间 |

```go
// Go：最小 Saga 执行器。生产上更推荐 Temporal/Seata Saga，但原理就是这段
type Step struct {
	Name string
	Do   func(ctx context.Context) error
	Undo func(ctx context.Context) error
}

func RunSaga(ctx context.Context, sagaID string, steps []Step) error {
	done := make([]Step, 0, len(steps))
	for _, s := range steps {
		if err := s.Do(ctx); err != nil {
			// 逆序补偿；补偿失败不能吞，必须进重试队列 + 告警
			for i := len(done) - 1; i >= 0; i-- {
				if uerr := done[i].Undo(ctx); uerr != nil {
					return fmt.Errorf("saga %s compensate %s: %w (cause: %v)",
						sagaID, done[i].Name, uerr, err)
				}
			}
			return fmt.Errorf("saga %s step %s: %w", sagaID, s.Name, err)
		}
		done = append(done, s)
	}
	return nil
}

// 每个 Do/Undo 都必须带 sagaID + stepName 做幂等键；
// 补偿接口必须能重复执行，且执行前先查正向是否真的成功。
```

### 8.4.4 可靠消息最终一致：用 MQ 解耦，但别在事务里发

**原理**：业务本地事务提交后，把事件可靠地送到 MQ，下游消费并幂等处理。难点不在「发」，在「提交与发送之间的原子性」——这正是 8.4.5 要解决的。

| 维度 | 结论 |
| --- | --- |
| 一致性强度 | 最终一致，通常 100ms~5s |
| 性能成本 | MQ 吞吐高；生产端多一次发送；消费端需要幂等表和重试 |
| 适用边界 | 异步解耦、事件扇出、非实时业务；不适合「提交后立刻要读到自己写」的场景 |
| 失败表现 | 提交后发送失败 → 消息丢；重投 → 重复；分区 → 乱序；消费慢 → 堆积 |

```java
// RocketMQ 事务消息：半消息 + 本地事务 + 回查（API 以 5.x 客户端为准）
TransactionMQProducer producer = new TransactionMQProducer("tx-order");
producer.setNamesrvAddr("mq-namesrv:9876");
producer.setTransactionListener(new TransactionListener() {
    @Override
    public LocalTransactionState executeLocalTransaction(Message msg, Object arg) {
        try {
            orderService.createInLocalTx((OrderCmd) arg);   // 本地事务
            return LocalTransactionState.COMMIT_MESSAGE;
        } catch (Exception e) {
            return LocalTransactionState.ROLLBACK_MESSAGE;
        }
    }

    @Override
    public LocalTransactionState checkLocalTransaction(MessageExt msg) {
        // Broker 回查：按业务主键查订单是否真的落库
        return orderService.exists(msg.getKeys())
            ? LocalTransactionState.COMMIT_MESSAGE
            : LocalTransactionState.ROLLBACK_MESSAGE;
    }
});
producer.start();
producer.sendMessageInTransaction(
    new Message("order-topic", "PAID", orderNo.getBytes(StandardCharsets.UTF_8)), cmd);
```

### 8.4.5 本地消息表 / Outbox：把「发消息」变成「写本地表」

**原理**：业务数据和 outbox 记录在**同一个本地事务**里提交；独立的投递器扫描 outbox，把事件发到 MQ。发送失败就重试，发送成功就标记 `SENT`。它把「双写」变成了「单写 + 至少一次投递」。

| 维度 | 结论 |
| --- | --- |
| 一致性强度 | 最终一致；业务提交与事件写入原子，投递至少一次 |
| 性能成本 | 每事件 +1 次 insert（约 0.5~2ms）；投递器轮询/CDC 成本；表会膨胀 |
| 适用边界 | 任何有数据库的服务；替代「事务里直接发 MQ」；高可靠业务事件 |
| 失败表现 | 投递器挂 → 积压；重复投递；顺序错乱；清理不及时 → 表爆 |

```sql
-- 最小可用 outbox（完整版与三种投递器见第 10 章）
CREATE TABLE outbox_event (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  event_id        CHAR(36)        NOT NULL COMMENT '消费者幂等键',
  aggregate_id    VARCHAR(64)     NOT NULL COMMENT '分区键，如 order_no',
  aggregate_version INT           NOT NULL DEFAULT 0,
  event_type      VARCHAR(64)     NOT NULL,
  payload         JSON            NOT NULL,
  status          ENUM('NEW','SENT','FAILED') NOT NULL DEFAULT 'NEW',
  retry_count     INT             NOT NULL DEFAULT 0,
  next_retry_at   DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  created_at      DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_event_id (event_id),
  KEY idx_status_retry (status, next_retry_at, id),
  KEY idx_aggregate (aggregate_id, aggregate_version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 8.4.6 最大努力通知：只承诺「尽力 + 可查 + 可补」

**原理**：业务完成后，通知服务按阶梯重试调用第三方/用户（1s、5s、30s、5min、30min），超过上限就落人工队列，同时依赖对账兜底。它不承诺送达，承诺的是**可观测、可重试、可人工补发**。

| 维度 | 结论 |
| --- | --- |
| 一致性强度 | 弱一致；可能丢、可能重、可能晚 |
| 性能成本 | 最低；异步、无锁、无预留 |
| 适用边界 | 短信/邮件/Webhook/状态回传；第三方明确不保证幂等时；非资金主链路 |
| 失败表现 | 通知丢失、重复通知、通知内容过期；用户不知道结果 |

```go
// Go：阶梯重试 + 死信，不承诺成功，只承诺「试过且留痕」
func NotifyWithRetry(ctx context.Context, task NotifyTask) error {
	backoff := []time.Duration{time.Second, 5 * time.Second, 30 * time.Second, 5 * time.Minute, 30 * time.Minute}
	for i, wait := range backoff {
		if err := sender.Send(ctx, task); err == nil {
			return nil
		} else if i == len(backoff)-1 {
			// 超过上限：写死信表，生成人工工单，等对账/客服补发
			return deadLetter.Insert(ctx, task, err)
		}
		select {
		case <-time.After(wait):
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return nil
}
```

### 8.4.7 为什么大厂主推最终一致，而不是 XA

假设一个订单链路：30,000 TPS 峰值，订单库和库存库各 200 个连接，XA 事务从 prepare 到 commit 平均 120ms，热点 SKU 的行锁也持有 120ms。

| 指标 | XA 2PC | 最终一致（本地事务 + Outbox + MQ） |
| --- | --- | --- |
| 单笔事务占用连接时间 | 120ms（含 prepare 等待） | 8~15ms（只写本地库） |
| 单库连接吞吐上限 | 200 ÷ 0.12 ≈ **1,666 TPS** | 200 ÷ 0.012 ≈ **16,600 TPS** |
| 热点行锁持有时间 | 120ms → 单 SKU 约 **8.3 TPS** | 8ms → 单 SKU 约 **125 TPS** |
| 协调者故障 | in-doubt，锁不释放，人工 `XA RECOVER` | 消息重试/补偿，自动恢复 |
| 支持 MQ / Redis / 第三方 | **不支持** | 支持（Outbox + 补偿） |

**结论不是「强一致不好」，而是「XA 把强一致的代价付在了最热的那一行上」**：

1. **锁持有时间 = 业务 SQL + 两次网络往返 + 协调者决策**。夹一次 RPC 就从毫秒变几百毫秒；30,000 TPS 下，热点行直接变成串行队列。
2. **热点行的锁被占住**。200 个连接 ÷ 0.12s ≈ 1,666 TPS（8.0.28- 且连接被占的口径）；要扛 30,000 TPS，光连接就要 18 个库。8.0.29+ 连接可 detach，但行锁仍在，热点行照样串行。
3. **协调者单点**。协调者一挂，所有 prepared 分支都卡住；commit 决定必须唯一，加高可用也没用。8.1 的 79 分钟就是这么来的。
4. **覆盖不了外部世界**。支付、发券、短信、第三方 API 没有 XA 接口；有一个，2PC 就不是端到端。
5. **最终一致的异常率可以算**。0.2% 补偿率、99.5% 自动补偿成功率，每天 1,000 万笔有 100 笔需要人工；而 XA 的 in-doubt 是一次几千笔同时卡住。**前者是运营问题，后者是可用性问题。**

> 💡 大厂的选择不是「放弃一致性」，而是把一致性从「数据库的阻塞式保证」换成「工程的收敛式保证」：**Outbox 保证不丢、幂等保证不重、补偿保证能回、对账保证能发现**。这四件事，一件都不能省。

---

## 8.5 双修对照：Agent 侧只剩补偿类方案

| 维度 | 传统系统 | Agent 系统 | 同 or 异 |
| --- | --- | --- | --- |
| 方案集合 | 六种都能用 | **2PC/XA 直接出局**；TCC/Saga/Outbox/最大努力通知保留 | 异：模型调用无法 prepare，也无法 rollback |
| 身份 / 唯一键 | `order_no`、`request_id`、`xid` | `run_id` / `step_id` / `tool_call_id`；`step_id` 必须含工具名 + 参数 hash | 同构，键的组成多一层 |
| Step 级原子性 | 无此概念 | 每个 Step 的「DB 写 + outbox 事件」必须在一个本地事务里 | 新增：Agent 的最小事务单位是 Step |
| 失败模式 | in-doubt、空回滚、悬挂、消息丢 | 上述全部 + **模型已返回但结果没落库**、工具已执行但 Step 标记失败、token 已扣但 Run 失败 | 多一类「不可回滚的模型/工具副作用」 |
| 恢复方式 | 重试 / 补偿 / 人工修数 | **检查点 + 补偿 + 重新规划**；不能「重放模型调用」当作回滚 | 异：恢复要防重复花钱 |
| 验证方法 | 金额对账、状态对账 | 金额对账 + **token/调用次数对账**；模型输出还要做质量抽检 | 框架相同，口径扩一维 |
| 成本结构 | 机器折旧，近似固定 | 每次模型/工具调用都按量计费，重试 = 重复烧钱 | 异：成本显性 |

**哪些完全相同、可以直接复用**：Outbox 表结构与投递器、幂等表、Saga 状态机、补偿重试队列、对账框架、状态机落库套路。**这五样换的只是键名和事件类型。**

Agent 侧要额外写的一段是「Step 级原子性」：

```go
// 每个 Step：先落意图，再执行工具，最后在同一事务里写结果 + outbox
func (e *Executor) RunStep(ctx context.Context, runID, stepID string, tool Tool, args Args) error {
	intent := StepRecord{RunID: runID, StepID: stepID, Tool: tool.Name(), ArgsHash: hash(args), Status: "INTENT"}
	if err := e.repo.InsertIntent(ctx, intent); err != nil { return err } // 崩溃后能看出「可能已调用」
	result, err := tool.Call(ctx, args)                                  // 外部副作用，不可回滚
	return e.repo.InTx(ctx, func(tx *sql.Tx) error {
		if err != nil {
			if merr := e.repo.MarkFailed(tx, stepID, err.Error()); merr != nil {
				return merr
			}
			return fmt.Errorf("step %s failed: %w", stepID, err) // 失败必须向上传播
		}
		if err := e.repo.SaveResult(tx, stepID, result); err != nil { return err }
		return e.outbox.Insert(tx, Event{
			EventID: stepID + ":completed", AggregateID: runID,
			Type: "step.completed", Payload: result,
		})
	})
}
```

**差异根因**（与总纲 §1.5 对齐）：非确定性 → 同一 Step 重放可能不同结果，幂等键必须含模型/提示词版本；长事务 → 一次 Run 秒到分钟级，必须假设随时中断，靠检查点恢复；不可回滚 → 工具副作用只能补偿；成本显性 → 重试前先问「这次重试要花多少钱」。

---

## 8.6 会在哪炸：失效模式清单

| 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 库存库连接池打满、接口全超时 | XA 协调者崩溃，prepared 分支持锁占连接 | `XA RECOVER`；`information_schema.innodb_trx` 看长事务 | 按协调者日志提交/回滚；无日志则对账反推 | 长事务/高并发禁用 XA；协调者日志与业务库同事务落盘 |
| 补偿把没扣的库存又加回去了 | 空回滚：Cancel 先于 Try 到达 | 查预留表：有 CANCELLED 无 FROZEN | 用 xid 墓碑挡住后续 Try；修数据 | 状态表 + 防悬挂标记（详见第 13 章） |
| 订单取消了，库存还被锁着 | 悬挂：Cancel 后 Try 重试成功 | 查 `created_at > cancelled_at` 的预留 | 删除/释放悬挂预留 | Try 前检查 CANCELLED 墓碑 |
| 订单已支付，下游没收到 | 事务提交后发 MQ 失败，异常被吞 | 对比订单表与下游表；查生产端 send 日志 | 补发事件；修数据 | 改 Outbox/事务消息（详见第 10 章） |
| Outbox 表 2,000 万行，投递延迟 40 分钟 | 投递器单实例 + 无索引 + 没清理 | `SELECT COUNT(*) FROM outbox_event WHERE status='NEW'` | 加 `SKIP LOCKED` 并行投递、归档历史 | 监控积压与最老事件年龄（详见第 10 章） |

---

## 8.7 怎么验证：故障注入 + 两条 SQL + 一张告警规则

**① 故障注入**：在 XA 的 `PREPARE` 之后、`COMMIT` 之前 `kill -9` 协调者，观察两库是否进入 in-doubt、连接池是否被打满、告警是否在 1 分钟内触发。TCC 则注入「Cancel 先到」「Try 超时但实际成功」两种乱序（详见第 13 章）。

**② XA**：

```sql
XA RECOVER;
-- XA PREPARED: use XA RECOVER; INNODB_TRX only lists active trx states
SELECT trx_id, trx_state, trx_started,
       TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS age_sec,
       trx_rows_locked, trx_query
  FROM information_schema.innodb_trx
 WHERE TIMESTAMPDIFF(SECOND, trx_started, NOW()) > 10;
```

**③ Outbox 积压**：

```sql
SELECT status, COUNT(*) AS cnt,
       MIN(created_at) AS oldest,
       TIMESTAMPDIFF(SECOND, MIN(created_at), NOW()) AS oldest_age_sec
  FROM outbox_event
 WHERE status IN ('NEW','FAILED')
 GROUP BY status;
```

**④ 告警**：

```yaml
groups:
  - name: distributed-tx
    rules:
      - alert: XaInDoubtDetected
        expr: mysql_xa_in_doubt_transactions > 0
        for: 1m
        labels: { severity: P1 }
        annotations:
          summary: "XA 出现 in-doubt 事务，可能持有行锁"
      - alert: OutboxBacklogHigh
        expr: outbox_new_events > 10000 or outbox_oldest_age_seconds > 300
        for: 2m
        labels: { severity: P1 }
        annotations:
          summary: "Outbox 积压超过 1 万条或最老事件超过 5 分钟"
      - alert: CompensationFailure
        expr: increase(saga_compensation_failed_total[10m]) > 0
        for: 0m
        labels: { severity: P2 }
        annotations:
          summary: "出现补偿失败，需人工介入"
```

**⑤ 压测判据**：2 倍峰值流量下，P99 不超过基线的 2 倍；XA 场景重点看 `row_lock_waits` 和连接池 active；最终一致场景重点看 outbox 最老事件年龄和消费积压。**任何方案，只要「积压年龄」没有告警，就等于没有上线。**

---

## 8.8 用什么工具

| 场景 | 首选 | 边界 / 坑 |
| --- | --- | --- |
| Java 生态一站式 | Seata（AT / TCC / Saga） | AT 只适合短事务且依赖 undo_log；全局锁在热点上会退化；业务幂等仍要自己写 |
| 长流程编排 | Temporal / Cadence | 代码即流程、持久化执行；学习成本高，运维要单独一套 |
| 状态机式 Saga | Seata Saga / 自研状态机表 | 适合步骤固定、需要可视化；复杂分支建议 Temporal |
| 事务消息 | RocketMQ 事务消息 | 半消息 + 回查；回查逻辑必须能按业务主键判断 |
| Outbox 投递 | 自研轮询 / Debezium CDC | 轮询简单但延迟与表压力；CDC 延迟低但运维复杂（详见第 10 章） |
| 数据库层强一致 | MySQL XA / ShardingSphere XA | 只用于短事务、低并发、无外部副作用 |
| 对账兜底 | 自研 + ClickHouse | 任何方案的最后一道防线（详见第 14 章） |

Seata 的最小配置（Spring Boot，键名以 1.7.x 文档为准）：

```yaml
seata:
  enabled: true
  application-id: order-service
  tx-service-group: order_tx_group
  service:
    vgroup-mapping:
      order_tx_group: default
  registry:
    type: nacos
    nacos:
      server-addr: nacos:8848
      namespace: prod
  config:
    type: nacos
    nacos:
      server-addr: nacos:8848
      namespace: prod
```

> ⚠️ 框架能力的边界：Seata 帮你「协调提交/回滚」，**不帮你设计补偿、不帮你做幂等、不帮你对账**。这三件事才是分布式事务里真正会出错的部分。

---

## 8.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 分布式事务定义 | 一个业务结果落在两个以上独立资源上，本地事务管不了 |
| 2PC/XA | 强一致、短事务；prepare 到 commit 全程持锁占连接；协调者单点 → in-doubt |
| TCC | 业务层预留；Try/Confirm/Cancel 三接口都要幂等；空回滚、悬挂、补偿失败是三坑 |
| Saga | 长事务补偿链；无隔离性；必须有状态机 + 补偿 + 重试上限 |
| 可靠消息 | 最终一致的主力；难点是「提交与发送的原子性」 |
| Outbox | 把发消息变成写本地表；与业务同事务；至少一次投递 |
| 最大努力通知 | 只承诺尽力 + 可查 + 可补；必须配对账 |
| 为什么不用 XA | 锁持有 120ms → 热点 SKU 8 TPS；200 连接只能扛 1,666 TPS；协调者挂 → 锁 79 分钟 |
| 最终一致四件套 | Outbox 不丢 + 幂等不重 + 补偿能回 + 对账能发现 |
| Agent 侧 | 2PC 出局；Step 级本地事务 + Outbox + Saga 补偿 + 检查点 + token 对账 |
| 不做会怎样 | 不是「慢一点」，是「一次故障几千笔同时卡死」或「账悄悄错了没人知道」 |

## 8.10 自检题

1. **一个 30 秒的跨库转账，为什么不能直接用 XA？**
   <details><summary>答案要点</summary>
   XA 从 prepare 到 commit 全程持有行锁和连接；30 秒意味着热点账户被串行化 30 秒，连接池被占满，协调者一挂就 in-doubt。应改用 Saga/TCC：本地事务短、状态机推进、失败补偿，并用对账兜底。
   </details>

2. **TCC 的 Try 成功了，Confirm 超时，你应该立刻 Cancel 吗？**
   <details><summary>答案要点</summary>
   不应该。Confirm 可能已经成功只是响应丢了；立刻 Cancel 会造成「已确认又释放」的超卖。正确做法是查全局事务状态/预留表状态，Confirm 接口做成幂等，按 TC 的最终决定重试；Cancel 前必须检查正向是否已执行。
   </details>

3. **Outbox + MQ 只能保证至少一次，业务上怎么做到「恰好一次」的效果？**
   <details><summary>答案要点</summary>
   生产端用本地事务保证「业务数据 + 事件」不丢，消费端用 `event_id` 或业务唯一键做幂等（唯一索引/状态机条件更新/去重表），把重复投递变成无副作用；再用对账发现漏处理。**恰好一次 = 生产端不丢 + 消费端幂等**，不是 MQ 单独能给的。
   </details>

## 8.11 延伸阅读

- MySQL 8.0 Reference Manual：*XA Transactions*、`XA RECOVER` 语法与限制（以 8.0.x 文档为准）
- Seata 官方文档：AT/TCC/Saga 模式与 `@TwoPhaseBusinessAction`（以 1.7.x 为准）
- Apache RocketMQ 文档：事务消息（半消息、回查、`TransactionListener`）
- Martin Kleppmann《Designing Data-Intensive Applications》第 9 章：分布式事务与一致性
- 上一章：[7. 本地事务：ACID 的真实边界与框架陷阱](7-local-transaction-pitfalls.md)
- 下一章：[9. 事务决策树：什么场景用什么](9-transaction-decision-tree.md)
- 交叉引用：Outbox 详见第 10 章；Exactly-once 详见第 11 章；Saga 三坑详见第 13 章；对账详见第 14 章
