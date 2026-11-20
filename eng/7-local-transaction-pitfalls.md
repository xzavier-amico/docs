# 7. 本地事务：ACID 的真实边界与框架陷阱

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · 事务与一致性 ｜ **编号**：7 / 52
> **本章定位**：把「我以为开了事务」这件事钉死。不给 ACID 长篇推导，只回答三件事——**Spring 事务在什么条件下真的生效**、**MySQL 隔离级别在真实并发下到底是什么表现**、**大事务为什么会把整库拖死**。
> **读法**：§7.4.1 的十坑表直接贴进代码评审清单；§7.7 的四条 SQL 存成排障脚本；其余按需跳读。后面 8~14 章讨论的所有分布式方案，都建立在「本地事务这一层是可靠的」这个前提上——这一层不牢，上面全是空中楼阁。

---

## 7.1 引子：三场「明明写了事务」的事故

**事故一：重复发券 8,600 张，损失 43 万元（2025-03-12）**

会员日 20:00，营销服务发券接口 QPS 冲到 1,200。`CouponService` 的定时批处理 `batchGrant()` 直接 `this.grant()` 调用——同类自调用绕过代理，`grant()` 上的 `@Transactional` 从未生效；里面的三条 `INSERT` 各自自动提交，而券表的 `(user_id, batch_id)` 唯一索引当时因为「先上线再补」没建。网关超时重试 + 客户端重试让同一请求进来两次。

结果：8,600 张 50 元券重复发放，直接损失 43 万元，T+1 与财务对账才发现，客诉 213 起。**根因不是并发的错，是「事务从没生效」加「没有唯一索引兜底」的叠加。**

**事故二：大事务锁表，全站 9 分钟不可用（2025-07-08）**

运营后台「批量改价」在一个事务里对 `sku` 表 `UPDATE` 了 12 万行，执行 4 分 12 秒。期间该表所有行锁被持有，商品详情页的 `SELECT` 走的是主库（读写未分离），连接池 200 个连接 40 秒内被打满，订单接口雪崩。监控上表现为「数据库 CPU 只有 18%，但应用超时率 97%」——典型的**锁等待假死**。

**事故三：受检异常不回滚，库存扣了订单没建（2025-09-02）**

下单链路在同一个本地事务里写 `orders` 和 `order_items`，最后调用 `couponFacade.verify()`，该方法签名 `throws CouponExpiredException`。Spring 默认只对 `RuntimeException` 和 `Error` 回滚，受检异常被抛出后**事务照样提交**。当天 37 笔订单在优惠券已过期的情况下仍然落库并进入履约，3 小时后才发现。

> 🎯 三个事故没有一个是「数据库坏了」。全部是**你以为事务管住了它，其实没有**。这一章就是把「以为」变成「确定」。

---

## 7.2 到底是什么：Spring 本地事务生效的四个前提

先给定义：**一个本地事务 = 一个数据库连接上的一段受 `BEGIN ... COMMIT/ROLLBACK` 保护的语句序列。** 它的能力边界由数据库决定，不由框架决定。

Spring 的声明式事务不是「魔法」，它只是三样东西的组合：

1. **代理**：`@Transactional` 标注的方法必须被**外部调用**，才会经过 `TransactionInterceptor`（CGLIB/JDK 动态代理）。
2. **ThreadLocal 绑定**：事务上下文存在 `TransactionSynchronizationManager` 的 ThreadLocal 里，线程一换就丢。
3. **一个连接**：整个事务期间复用同一个 `Connection`，跨线程/跨数据源就断。

由此推出**生效的四个前提**，缺一个就白写：

| 前提 | 违反时的现象 |
| --- | --- |
| 方法被代理对象调用（非 `this.`、非 `@PostConstruct`、非内部 `@Async` 线程） | 全程无 `BEGIN`，日志里看不到事务上下文 |
| 方法 `public` 且非 `final`/`static` | CGLIB 无法覆写，注解被静默忽略 |
| 异常能传播到代理边界，且是 `RuntimeException`/`Error`（或显式 `rollbackFor`） | 事务提交了，数据脏了 |
| 所有操作在同一 `DataSource` / 同一线程 | 后半段操作脱离事务，各自提交 |

**反例清单（这些都不受本地事务保护）**：

| 你以为是事务的一部分 | 真实情况 |
| --- | --- |
| 发 MQ 消息 | 不受保护。提交前发出去可能「消息发了、数据回滚」；提交后发可能「数据成功、消息丢了」→ 详见第 10 章 |
| 写 Redis | 不受保护，且不可回滚 |
| 调第三方支付/发短信 | 完全不受保护，外部副作用不可回滚（三定律之三） |
| `@Async` 里的数据库写 | 运行在新线程，**新连接、新事务或根本没有事务** |
| 跨库 / 跨服务的两个写 | 本地事务对此无能为力 → 详见第 8、12 章 |

> 💡 一句话记住：**本地事务保证的是「同一个连接上的多条语句要么全生效要么全不生效」，此外它什么都不保证。** 不做这条边界划分，你会拿它去兜 MQ 和外部接口，然后在下一次重试时炸。

---

## 7.3 结构图：调用链、传播栈与大事务的锁

```
① 代理是否介入（决定事务有没有开始）

  Controller  ──►  [Spring AOP 代理]  ──►  OrderService.place()
                          │                    │
                          │ 开事务、绑连接       │  this.create()  ← 同类自调用
                          ▼                    ▼
                  TransactionInterceptor    OrderService.create()  @Transactional
                          │                    │    ✗ 不过代理
                          │                    ▼    ✗ 注解被静默忽略
                          └──►  Connection #17 (ThreadLocal 绑定)  ← 只有走代理的这段在事务里

② 传播行为＝在同一个 ThreadLocal 栈上怎么切事务

  外层 REQUIRED ──────────────────────────────┐
    ├─ 内层 REQUIRED      → 复用外层事务（一起回滚）      │
    ├─ 内层 REQUIRES_NEW  → 挂起外层，新连接 #18 ────────┤ 外层回滚不影响内层（⚠️ 内层已提交）
    └─ 内层 NESTED        → 外层连接上的 SAVEPOINT ──────┘ 内层回滚到存档点，外层可继续

③ 大事务锁的扩散路径（事故二）

  UPDATE 12万行（4分12秒）
        │
        ├─► 行锁/间隙锁被持有 4 分钟 ──► 其他会话 row_lock_waits 堆积
        ├─► undo log 膨胀到 6.8 GB ───► purge 线程追不上，历史版本读变慢
        ├─► binlog 单事务 1.2 GB ─────► 从库单线程回放，延迟 210 秒
        └─► 应用连接池 200/200 占用 ──► Tomcat 线程全在等 DB，接口超时 97%
```

---

## 7.4 怎么实现：十坑、正确写法、隔离级别、大事务拆分

### 7.4.1 `@Transactional` 十坑一张表

把这张表打印出来贴到 review 清单上。**每一行都是我在线上见过至少一次的**。

| # | 坑 | 现场现象 | 根因 | 怎么修 |
| --- | --- | --- | --- | --- |
| 1 | **同类自调用** | 加了注解但无 `BEGIN`，异常后数据半截 | 代理只在外部调用时介入 | 拆类 / 注入自身代理 `self.doXxx()` / `AopContext.currentProxy()`（需 `exposeProxy=true`） |
| 2 | **private / final / static 方法** | 同上，且无任何报错 | CGLIB 无法覆写 | 改 `public`；必要时用 `TransactionTemplate` 显式包裹 |
| 3 | **异常被吞** | 日志有 error，数据却提交了 | 拦截器只看到正常返回 | `catch` 里必须 `throw`；用 `TransactionAspectSupport.currentTransactionStatus().setRollbackOnly()` 兜底 |
| 4 | **受检异常不回滚** | 抛了 `IOException`，事务提交 | 默认只回滚 `RuntimeException`/`Error` | 统一 `@Transactional(rollbackFor = Exception.class)`，并在规范里强制 |
| 5 | **只读事务里写** | 有的环境报错、有的环境静默成功 | `readOnly=true` 只是提示：Connector/J 8.x 默认下推服务器端（`readOnlyPropagatesToServer=true`），5.1.x 及部分中间件只在客户端标记（以你的驱动版本为准） | 不要把 `readOnly` 当护栏；写入必须走明确标注写事务的方法 |
| 6 | **异步线程丢事务** | `@Async` 里写库「偶发」不回滚 | 上下文在 ThreadLocal，新线程没有 | 事务不要在异步方法里开始；异步只做「事务提交之后」的事（`TransactionSynchronization.afterCommit`） |
| 7 | **传播行为误用** | 「内层回滚了外层还在跑」/「日志表回滚了」 | `REQUIRES_NEW` 需要新连接，`NESTED` 需要 savepoint | 见 §7.4.2 决策表；审计日志用 `REQUIRES_NEW`，业务分支用 `REQUIRED` |
| 8 | **嵌套事务与 savepoint** | 内层 rollback 后外层仍提交、但内层数据没了 | `NESTED` 依赖 JDBC savepoint，Spring 的 `DataSourceTransactionManager` 需显式 `setNestedTransactionAllowed(true)`（默认 `false`，以版本文档为准） | 要么显式开启 savepoint，要么别用 `NESTED`；回滚到存档点后只允许「继续，不再重试内层」 |
| 9 | **超时与死锁重试** | 死锁后数据错乱；`timeout` 设了但语句还在跑 | `@Transactional(timeout)` 落到语句超时，且**死锁时 MySQL 已经回滚了整个事务** | 重试必须放在事务边界**之外**（见 Go 代码）；超时同时配 `innodb_lock_wait_timeout` |
| 10 | **大事务锁表** | 见事故二 | 长事务持锁 + undo/binlog 膨胀 + 连接占用 | 分批 + 移出事务 + Outbox + 状态机推进（见 §7.4.4） |

### 7.4.2 正确写法：Java 与 Go

Java 侧的三个关键点：**事务边界只放在「需要原子性」的那一层**、**永远写 `rollbackFor`**、**回滚之外的审计日志用 `REQUIRES_NEW`**。

```java
@Service
public class CouponService {

    @Resource private CouponService self;          // 注入自身代理，解决坑 1
    @Resource private CouponMapper couponMapper;
    @Resource private CouponAuditMapper auditMapper;

    /** 外层：只做参数校验和幂等预检，不开事务，尽量短 */
    public GrantResult grant(GrantCmd cmd) {
        if (cmd.batchId() == null || cmd.userId() == null) throw new IllegalArgumentException("bad cmd");
        try {
            self.doGrant(cmd);                     // 走代理 → 事务真的生效
            return GrantResult.ok();
        } catch (DuplicateKeyException e) {        // 唯一索引兜底 = 幂等的最后一道防线
            return GrantResult.duplicated();
        }
    }

    @Transactional(
        rollbackFor = Exception.class,             // 坑 4：受检异常也要回滚
        propagation = Propagation.REQUIRED,
        isolation   = Isolation.READ_COMMITTED,    // 高并发发券用 RC，减少间隙锁
        timeout     = 3)                           // 坑 9：业务事务禁止超过 3 秒
    public void doGrant(GrantCmd cmd) {
        int inserted = couponMapper.insertIgnore(cmd);   // INSERT IGNORE + 唯一索引
        if (inserted == 0) return;                       // 已发过 → 静默幂等返回
        couponMapper.decrStock(cmd.batchId());           // 必须 >0：UPDATE ... WHERE remain > 0
    }

    /** 审计日志：业务回滚了，日志也要留下来（独立事务，坑 7） */
    @Transactional(propagation = Propagation.REQUIRES_NEW, rollbackFor = Exception.class)
    public void writeAudit(String bizNo, String reason) {
        auditMapper.insert(bizNo, reason);
    }
}
```

Go 侧没有注解、没有代理，**事务边界是显式的 `BeginTx` / `Commit`**，所以「事务没生效」这类坑在 Go 里换了个形态出现：**忘了 `defer tx.Rollback()`**、**在事务里做 RPC**、**死锁后不重试**。

```go
// 死锁重试必须是「整个事务重来」，不能在事务内重试单条语句
func (r *OrderRepo) InTx(ctx context.Context, fn func(context.Context, *sql.Tx) error) error {
	const maxAttempt = 3
	var lastErr error
	for i := 0; i < maxAttempt; i++ {
		tx, err := r.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
		if err != nil {
			return fmt.Errorf("begin tx: %w", err)
		}
		if err = fn(ctx, tx); err != nil {
			_ = tx.Rollback()
			if isRetryableTxErr(err) && i < maxAttempt-1 {
				lastErr = err
				sleepBackoff(ctx, i) // 20ms / 60ms / 180ms + jitter
				continue
			}
			return err
		}
		if err = tx.Commit(); err != nil {
			if isRetryableTxErr(err) && i < maxAttempt-1 {
				lastErr = err
				sleepBackoff(ctx, i)
				continue
			}
			return fmt.Errorf("commit: %w", err)
		}
		return nil
	}
	return fmt.Errorf("tx retry exhausted after %d: %w", maxAttempt, lastErr)
}

// MySQL: 1213 = deadlock found, 1205 = lock wait timeout
func isRetryableTxErr(err error) bool {
	var me *mysql.MySQLError
	if errors.As(err, &me) {
		return me.Number == 1213 || me.Number == 1205
	}
	return false
}
```

**传播行为决策表**（只在真有跨事务需求时才用，默认 `REQUIRED`）：

| 传播行为 | 用在哪 | 不做会怎样 |
| --- | --- | --- |
| `REQUIRED`（默认） | 99% 的业务方法 | — |
| `REQUIRES_NEW` | 审计日志、失败记录、独立计费流水 | 用 `REQUIRED` 则业务回滚连日志一起没了，事后无法复盘 |
| `NESTED` | 可部分失败的子步骤（少用） | 用 `REQUIRED` 则子步骤失败拖死整单 |
| `NOT_SUPPORTED` | 事务内调慢接口（先挂起事务） | 用 `REQUIRED` 则持锁等 RPC，锁时间 = RPC 延迟 |
| `MANDATORY` | 强制要求被上层事务调用 | 独立调用时会报错，适合「必须在一个事务里」的写方法 |

### 7.4.3 隔离级别：教科书没说的真实表现

MySQL InnoDB 默认 `REPEATABLE READ`。**关键区分是「快照读」和「当前读」**，这是所有幻读讨论的根源：

- **快照读**：普通 `SELECT`，走 MVCC，读事务开始时的快照。RR 下快照在**第一次快照读**时建立，之后整事务一致。
- **当前读**：`SELECT ... FOR UPDATE` / `LOCK IN SHARE MODE` / `UPDATE` / `DELETE` / `INSERT`，读最新提交版本，并加锁（RR 下用 next-key lock 防幻读）。

| 隔离级别 | 快照读 | 当前读 | 幻读 | 应用层「先读后写」丢更新 | 生产建议 |
| --- | --- | --- | --- | --- | --- |
| READ UNCOMMITTED | 脏读 | 加锁 | 有 | 有 | 永不使用 |
| **READ COMMITTED** | 每条语句一个新快照 | 加锁，无间隙锁 | **有**（同一事务两次读到不同行集） | **有** | **互联网业务首选**：间隙锁少、并发高、死锁率低 |
| **REPEATABLE READ**（MySQL 默认） | 事务级快照 | 加锁 + 间隙锁 | 快照读无，当前读无（被锁挡住） | **有** | 金融账务、需要读一致性的事务 |
| SERIALIZABLE | 隐式加读锁 | 加锁 | 无 | 无（读加共享锁，写会互斥/死锁回滚） | 只在极小热点上短期使用 |

> 🩸 **最容易被误解的一点**：RR **不防丢更新**。两个事务先 `SELECT balance=100`，再各自 `UPDATE account SET balance=110`，RR 下第二个事务照样把第一个的更新覆盖掉——因为它写的是**绝对值**。解法只有三个：`UPDATE ... SET balance = balance - 50 WHERE balance >= 50`（表达式写）、乐观锁 `WHERE version = ?`、`SELECT ... FOR UPDATE` 悲观锁。详见第 4 章幂等与库存扣减。

代码：**永远不要「先查后算再写」绝对值**。

```sql
-- ❌ 丢更新：RR 也拦不住
SELECT balance FROM account WHERE id = 1001;           -- 读到 100
UPDATE account SET balance = 50 WHERE id = 1001;       -- 另一个事务也是这句

-- ✅ 表达式写（数据库层原子）
UPDATE account SET balance = balance - 50, version = version + 1
 WHERE id = 1001 AND balance >= 50;

-- ✅ 乐观锁（需要版本号 + 重试）
UPDATE account SET balance = 50, version = version + 1
 WHERE id = 1001 AND version = 7;

-- ✅ 悲观锁（只在热点行上短期持有，务必走索引，否则锁全表）
SELECT balance FROM account WHERE id = 1001 FOR UPDATE;
```

### 7.4.4 大事务：危害、判据与四种拆法

**判据（超过任意一条就是大事务）**：单事务 > 1,000 行写入 / > 200 ms 执行 / 涉及 > 1 张核心热点表 / 事务内有 RPC 或 MQ 调用。

| 危害 | 量化后果（事故二的实测） |
| --- | --- |
| 行锁持有时间长 | 4 分钟内 `row_lock_waits` 每秒 8,400 次 |
| undo log 膨胀 | 单事务 6.8 GB，purge 延迟拖慢所有快照读 |
| binlog 单事务过大 | 1.2 GB，从库延迟 210 秒 |
| 连接/线程占用 | 200 连接打满，接口超时率 97% |
| 回滚代价 | 回滚比执行更慢（要写 undo 反向操作） |

**四种拆法**（按优先级）：

1. **分批提交**：`UPDATE ... WHERE id > ? ORDER BY id LIMIT 500` 循环，每批独立事务 + 每批之间让出 CPU。
2. **把非核心操作移出事务**：发 MQ / 发短信 / 刷缓存 → Outbox 表（详见第 10 章），事务只写一条 outbox 记录。
3. **状态机推进替代长事务**：把「一次性做完」变成「多次短事务推进状态」（详见第 30 章）。
4. **读写分离 + 加只读副本承接查询**：让写事务不再和首页查询抢同一批锁。

```sql
-- 分批更新模板（可反复执行、可中断、可观测）
-- 注意：主键必须单调可用；无主键的宽表用 (create_time, id) 组合游标
SET SESSION innodb_lock_wait_timeout = 3;

UPDATE sku
   SET price = price * 1.05, version = version + 1
 WHERE batch_id = ? AND id > ?
 ORDER BY id
 LIMIT 500;
-- 第一批 cursor=0；后续 cursor=上一批的 MAX(id)，每批独立提交，批间 sleep 50ms

-- 查当前长事务（判断是否有人在拖库）
SELECT trx_id, trx_state, trx_started,
       TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS age_sec,
       trx_rows_locked, trx_rows_modified, trx_query
  FROM information_schema.innodb_trx
 WHERE TIMESTAMPDIFF(SECOND, trx_started, NOW()) > 5
 ORDER BY age_sec DESC;
```

---

## 7.5 双修对照：本地事务在 Agent 系统的位置

本地事务这一层**在两侧几乎逐字相同**——`@Transactional` 的十个坑，写 Agent 平台后端的人一个都不会少踩。差异全部来自一件事：**Agent 的一次「业务操作」里混着不可回滚的外部副作用**。

| 维度 | 传统系统 | Agent 系统 | 同 / 异 |
| --- | --- | --- | --- |
| **机制本体** | 一个连接上的 `BEGIN...COMMIT` | 完全相同：Run 记录、Step 记录、用量记录都在同一个 MySQL 库里 | **同**，可直接复用 |
| **身份 / 唯一键** | `order_no`、`request_id` | `run_id` / `step_id` / `tool_call_id` | 结构同构，键的构成变了（`step_id` 必须含 `tool_name + args_hash`） |
| **失败模式** | 自调用、异常被吞、大事务锁表 | 上述全部 + **一次 Run 内多次 DB 写之间夹着工具调用**：DB 回滚了，但「已经发出的邮件」回不来 | 多一类**不可回滚副作用** |
| **恢复方式** | 重试整个事务 / 人工修数 | 只能是**补偿**（撤销下单、删除文件）+ 重新规划；且重放要防「重复花钱」 | 差异根因：**模型不可回滚** |
| **验证方法** | 死锁日志、锁等待、对账 SQL | 相同 + **token / 调用次数 / 成本对账**（详见第 14 章） | 框架相同，口径扩一维 |
| **成本结构** | 事务越长约等于锁越久 | 越长约等于**越贵**：每多一步工具调用/模型调用都在真花钱 | 成本从「隐性折旧」变「显性按量」 |
| **观测指标** | 事务时长、锁等待、连接池饱和度 | 相同 + 单 Run token 消耗、工具调用成功率、单 Run 成本分布 | 相同 + 三个新指标 |

**哪些完全相同、可以直接复用**：

- 事务边界纪律（业务方法短、RPC 移出事务、审计日志 `REQUIRES_NEW`）；
- `rollbackFor` / 传播行为 / 超时 / 分批这四套规范；
- 死锁重试模板（`InTx` 那段 Go 代码可原样用于 Agent 平台的 Step 落库）；
- 「读-改-写」必须表达式写或乐观锁的规则。

**差异根因（四条，跟总纲 §1.5 对齐）**：非确定性 → 同一 Step 重放可能产生不同结果，所以幂等键必须含模型与提示词版本；长事务 → Agent 的一次业务操作 30 秒到几分钟，**必须假设随时被中断**，每一次 DB 提交都要能作为恢复点；不可回滚 → 外部副作用只能补偿（详见第 8、12、13 章）；成本显性 → 重试不只是重复写库，而是重复烧钱。

> 💡 **Agent 侧的额外铁律（传统系统没有）**：**先落库意图，再执行副作用。** 每次工具调用前，Step 记录里必须先写入「我打算调用 `send_email(to=X)`」，调用完成后再写入结果。这样即使进程被 KILL，恢复时能看出「这一步可能已经发出去了」——这是 checkpoint 与 Saga 补偿的前提（详见第 12、34 章）。

---

## 7.6 会在哪炸：失效模式清单

| 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 加了注解但数据半截 | 同类自调用 / `private` | `SHOW PROCESSLIST` 看不到长事务；开 `logging.level.org.springframework.transaction=DEBUG`，没有 `Creating new transaction` 就是没生效 | 拆类或注入自身代理 | 单测断言「业务抛异常后表里 0 行」——**事务生效必须有测试** |
| 偶发「日志有 ERROR 但数据提交」 | 异常被 catch 吞掉 | 在 `catch` 里打 `TransactionSynchronizationManager.isActualTransactionActive()` | `throw` 出去或 `setRollbackOnly()` | Review 规则：`catch` 块内不允许只有 `log.error` |
| 受检异常后数据提交 | 默认不回滚受检异常 | 复现：抛 `IOException` 后查表 | `rollbackFor = Exception.class` | 团队统一封装 `@Tx` 组合注解，禁止裸用 `@Transactional` |
| `@Async` 里写库不回滚 | ThreadLocal 上下文丢失 | 看日志两条连接 ID 不同 | 异步只做提交后的事 | 代码扫描：`@Async` + `@Transactional` 同方法直接报错 |
| 死锁后数据错乱 | 在事务**内**重试了单条语句 | `SHOW ENGINE INNODB STATUS` 的 `LATEST DETECTED DEADLOCK` | 重试整个事务（见 `InTx`） | 统一 repo 层 `InTx`，业务代码不许自己 `BeginTx` |
| 事务超时了但语句还在跑 | `@Transactional(timeout)` 不中断已发往 DB 的语句 | `innodb_trx` 里 age 远超 timeout | 同时设 `innodb_lock_wait_timeout` + 语句级 `setQueryTimeout` | 事务超时设为 P99 的 3 倍，并在压测中验证 |
| 高峰期表被锁死 | 大事务 + 读写同库 | 看 `data_lock_waits` 与连接池 active | kill 长事务 + 分批重跑 | 批量操作强制分批，上线前用 `SELECT COUNT(*)` 估算影响行数 |

---

## 7.7 怎么验证：四条能立刻执行的 SQL + 一次压测

**① 锁等待现场（MySQL 8.0）**

```sql
SELECT r.trx_id AS waiting_trx, r.trx_mysql_thread_id AS waiting_thread,
       r.trx_query AS waiting_query,
       b.trx_id AS blocking_trx, b.trx_mysql_thread_id AS blocking_thread,
       b.trx_query AS blocking_query
  FROM performance_schema.data_lock_waits w
  JOIN information_schema.innodb_trx r ON r.trx_id = w.requesting_engine_transaction_id
  JOIN information_schema.innodb_trx b ON b.trx_id = w.blocking_engine_transaction_id;
```

**② 死锁史**

```sql
SHOW ENGINE INNODB STATUS\G            -- 看 LATEST DETECTED DEADLOCK 段
SELECT * FROM performance_schema.events_statements_history_long
 WHERE message_text LIKE '%1213%' ORDER BY timer_start DESC LIMIT 20;
```

**③ 死锁复现（两个会话对着跑，5 秒内必现）**

```sql
-- 会话 A                                    -- 会话 B
BEGIN;                                        BEGIN;
UPDATE account SET balance=balance-1 WHERE id=1;   UPDATE account SET balance=balance-1 WHERE id=2;
UPDATE account SET balance=balance-1 WHERE id=2;   UPDATE account SET balance=balance-1 WHERE id=1;
-- 其中一个报 ERROR 1213 (40001): Deadlock found
```

**④ 事务是否真的生效（回归测试的写法）**

```java
@Test
void 抛异常后不应留下任何数据() {
    assertThatThrownBy(() -> orderService.place(badCmd)).isInstanceOf(IllegalStateException.class);
    assertThat(orderMapper.countByNo(cmd.orderNo())).isZero();       // 主表
    assertThat(itemMapper.countByOrderNo(cmd.orderNo())).isZero();   // 子表
}
```

**并发压测**：`wrk -t4 -c200 -d60s --latency -s post_json.lua http://api/orders`，同时盯四个指标。**观测指标（Prometheus/JMX）**：

| 指标 | 阈值（经验值） | 说明 |
| --- | --- | --- |
| `rate(innodb_row_lock_time[1m]) / rate(innodb_row_lock_waits[1m])` | > 50 ms 报警 | 当前平均行锁等待（不是实例启动以来的累计均值） |
| `longest_trx_age_seconds` | > 5 s 报警 | 自己写巡检：从 `innodb_trx` 采集 |
| `hikaricp_connections_active / max` | > 85% 持续 30 s | 连接池即将打满 |
| 事务 P99 耗时 | > 200 ms | 超过就该拆了 |
| binlog 单事务最大字节 | > 100 MB | 从库延迟风险 |

---

## 7.8 用什么工具

| 需求 | 开源/标准做法 | 你必须自己写的部分 |
| --- | --- | --- |
| 声明式事务 | Spring `@Transactional` / `TransactionTemplate` | 规范：`rollbackFor`、超时、边界纪律 |
| 显式事务（Go） | `database/sql` + `BeginTx`，或 `sqlx`/`pgx` | `InTx` + 死锁重试封装 |
| 事务审计 | `TransactionSynchronization#afterCommit` | 提交后事件（发 MQ/刷缓存）的统一入口 |
| 长事务巡检 | `information_schema.innodb_trx` + 定时任务 | 告警阈值与 kill 脚本（需审批） |
| 数据库代理/分库分表 | ShardingSphere | 跨分片事务**不等于**分布式事务，见第 36 章 |
| 监控 | Micrometer + Prometheus + Grafana | 大盘与告警规则 |

> ⚠️ **框架能力的边界**：Spring 帮你「开/提交/回滚」，**不帮你判断该不该回滚**。Seata 也一样，它不解决你的业务幂等。凡是「框架自动搞定一致性」的说法，都值得你把它的源码翻到实现那一行。

---

## 7.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 生效前提 | 外部调用 + `public` 非 `final` + 异常能抛出 + 同一连接/线程，缺一不可 |
| 自调用 | `this.xxx()` 不过代理；拆类或注入自身代理 |
| 受检异常 | 默认不回滚；统一切 `rollbackFor = Exception.class` |
| 吞异常 | `catch` 里只有日志 = 事务照常提交；必须 `throw` 或 `setRollbackOnly` |
| readOnly | 只是提示，不是护栏；不同驱动行为不同 |
| 异步 | ThreadLocal 不跨线程；异步只做「提交后」的事 |
| 传播 | 默认 `REQUIRED`；审计日志用 `REQUIRES_NEW`；慢调用用 `NOT_SUPPORTED` |
| 死锁 | MySQL 已回滚整个事务；重试必须在事务外，指数退避 |
| 隔离级别 | RC 更适合高并发；RR 不防丢更新，只能靠表达式写/乐观锁/FOR UPDATE |
| 大事务 | > 1,000 行 或 > 200 ms 即违规；分批 + Outbox + 状态机 |
| 不做会怎样 | 事务不生效 → 数据半截、重复发放，T+1 对账才发现，账已经错了 |
| 一句话 | **本地事务只保证「一个连接上的语句组原子」，边界外的世界（MQ、Redis、第三方、异步线程）一律不受保护。** |

## 7.10 自检题

1. **你的 `@Transactional` 方法被同类调用，`@Async` 里又写了一次库。异常抛出后，两张表分别是什么状态？**
   > 要点：外层根本没有事务（不过代理），第一条语句已自动提交；`@Async` 里的写在另一个线程、另一个连接，也没有事务或自成事务。所以两张表都**不回滚**。修法：拆类/自身代理 + 异步只做提交后动作。

2. **RR 隔离级别下，两个并发请求各扣 50 元，为什么还会超扣？**
   > 要点：它们写的是绝对值（`SET balance=50`），RR 只保证「读到的快照一致」，不阻止后写覆盖先写。修法：表达式写 `balance=balance-50 WHERE balance>=50`，或乐观锁 `WHERE version=?`，或 `FOR UPDATE`。

3. **一次 `UPDATE` 影响 12 万行的事故，为什么数据库 CPU 只有 18% 却全站雪崩？**
   > 要点：瓶颈不是 CPU 是**锁 + 连接**。行锁被持有数分钟 → 其他会话全部进入锁等待 → 应用连接池打满 → Web 线程全阻塞 → 超时率飙升。修法：分批提交（每批 ≤ 500 行）、读写分离、批次间让出、批量接口强制 dry-run 报影响行数。

## 7.11 延伸阅读

- Spring Framework 官方文档 *Data Access → Transaction Management*（传播行为与 `@Transactional` 语义以你使用的版本为准）
- MySQL 官方手册 *InnoDB Locking*（next-key lock 与 `REPEATABLE READ` 的真实行为）、`information_schema.innodb_trx`、`performance_schema.data_lock_waits`
- Martin Kleppmann《Designing Data-Intensive Applications》第 7 章（并发写异常的分类，看一遍就懂为什么不只是「幻读」）
- 上一章：[6. 幂等的可观测与工具化](6-idempotency-observability.md)
- 下一章：[8. 分布式事务全景：六种方案一次讲透](8-distributed-transaction-overview.md)
- 交叉引用：ID 生成与幂等键见第 3 章；Outbox 见第 10 章；长事务与状态机见第 30 章；Go 事务与错误处理细节见 [../go/0-summary.md](../go/0-summary.md)
