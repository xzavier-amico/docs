# 30. 状态机建模与落库

> **状态**：`已完成（初稿）` ｜ **所属**：第五部分 · 编排与状态机 ｜ **编号**：30 / 52
> **本章定位**：第 29 章回答了「谁拥有下一步的决定权」，本章回答「这个决定怎么变成一条不可争议的数据库记录」。五要素是语法，三种落库姿势是存储结构，并发迁移和迁移 trace 是让它经得起生产的两条腿。
> **读法**：§30.2 的形式化定义值得逐字读；§30.4 先看三种姿势的对比表再选型；§30.7 的 200 并发测试可以直接搬进 CI。

---

## 30.1 事故现场：三个团队、三个状态字段、12,400 笔订单

2025 年 3 月，某 SaaS 电商的订单表长这样：

```sql
orders(id, status, pay_status, fulfill_status, refund_status, ...)
```

支付团队只写 `pay_status`，履约团队只认 `fulfill_status`，售后团队退款时把 `status` 改成 `REFUNDING`。三套字段各自都有「状态机」，但**没有任何一个字段能单独回答「这笔订单现在到底是什么状态」**。

3 月 18 日，支付回调延迟导致 12,400 笔订单的 `status` 已经是 `PAID`，`pay_status` 却还是 `PAYING`；履约服务按 `fulfill_status='READY'` 继续发货。更糟的是，退款服务看到 `status='PAID'` 就允许退款，于是出现「已退款、已发货、支付状态还没确认」的三重矛盾。

- 持续 **43 小时**才发现；
- 客服收到 **1,860 张**工单；
- 重复发货与优惠券补偿合计 **86 万元**；
- 修复时人工更新 **3,200 行**，其中 217 行改错，二次返工。

复盘时大家的第一反应是「加分布式锁」。但锁解决不了这个问题：**锁只能让写入串行，不能让三个字段变成同一个真相**。真正的根因是：状态没有单一真相源，迁移没有守卫，变更没有审计。

> 🩸 **记住这句话**：状态机不是「一个 status 字段」，而是「**谁在什么条件下、因为什么事件、把哪个实体从哪个状态推到哪个状态**」的完整契约。缺了后半句，status 只是一个枚举。

---

## 30.2 到底是什么：五要素与形式化定义

### 定义

一个状态机可以写成五元组：

```text
M = (S, E, T, G, A)

S  有限状态集合，S = S_active ∪ S_terminal，终态是吸收态（absorbing）
E  事件集合（已经发生的事实，过去式命名）
T  迁移关系，T ⊆ S × E × S
G  守卫，G: T → {true, false}，对上下文求值
A  动作，A: T → Action[]，分「本地动作」与「外部副作用」两类
```

一次合法迁移 `t = (s, e, s')` 必须同时满足：

```text
1. (s, e, s') ∈ T                —— 这条边被定义过
2. G(t)(context) = true          —— 业务条件成立
3. 当前持久化状态 = s            —— 数据库里的真相没被别人改过
4. 迁移与本地动作在同一事务提交   —— 状态和账本不能分开
```

第 3 条最容易被忽略：**定义里允许，不等于此刻允许**。并发下「读到 s」和「写入 s'」之间有窗口，所以必须用条件更新把「当前状态 = s」也放进 `WHERE` 里。这是下一节三种落库姿势的共同底线。

### 五个要素各是什么、不是什么

| 要素 | 是什么 | 不是什么 | 反例 |
| --- | --- | --- | --- |
| **状态 State** | 实体在生命周期中的位置，互斥且可枚举 | 业务属性的容器 | `status='REFUND_100_VIP'` |
| **事件 Event** | 已经发生的事实，不可变，过去式 | 命令、请求、意图 | `PayOrder`（这是命令） |
| **迁移 Transition** | 一条被允许的 `from + event → to` 边 | 任意 if-else 分支 | 20 个服务里各写一段 `if status==...` |
| **守卫 Guard** | 迁移前的业务条件，必须可判定、可测试 | 一段有副作用的代码 | 在 guard 里发短信 |
| **动作 Action** | 迁移成功后的副作用 | 迁移的替代品 | 用「发消息」代替改状态 |

### 三个反例：这些都不是状态机

```text
反例 1：只有枚举，没有事件与守卫
  orders.status IN ('CREATED','PAID','SHIPPED')
  → 这是「状态字典」。谁都能改，改之前该满足什么条件没人知道。

反例 2：状态机只活在内存里
  new OrderStateMachine(order).fire(PAY_CONFIRMED)
  → 进程一重启，状态回到数据库里的旧值，内存里的迁移全部丢失。

反例 3：流程图是真相
  Confluence 上画了 12 个框，代码里有 17 个分支
  → 图会腐烂。真相必须是「数据库里的状态 + 可执行的迁移定义」，图只是它的投影。
```

### 状态、标志位、业务字段：三者必须分开

| 类型 | 回答的问题 | 例子 | 存储方式 |
| --- | --- | --- | --- |
| **状态** | 生命周期走到哪了 | `PAYING`、`PAID`、`REFUNDING` | 一个 `status` 字段，受迁移约束 |
| **标志位** | 某个正交事实是否成立 | `is_invoiced`、`is_gift` | 独立 boolean 字段，可随时变 |
| **业务字段** | 业务数据是多少 | `paid_amount`、`refund_amount` | 数值/字符串字段，不参与状态判断 |

> ⚠️ **不做会怎样**：把三者塞进一个 `status`，状态数会随维度乘积爆炸（3 个支付态 × 4 个履约态 × 3 个退款态 = 36 个值），每加一个维度就要改所有 switch，测试矩阵永远补不全。

---

## 30.3 结构图：订单状态机与迁移责任

```text
                    PAY_SUBMITTED              PAY_CONFIRMED
   [CREATED] ───────────────────▶ [PAYING] ───────────────────▶ [PAID]
       │                             │                             │
       │ CANCEL                      │ PAY_FAILED                  │ REFUND_REQUESTED
       ▼                             ▼                             ▼
  [CANCELLED]                  [PAY_FAILED]                  [REFUNDING]
   (终态)                        │     │                           │
                                 │     │ PAY_RETRIED               │ REFUND_SUCCEEDED
                                 │     └──────────▶ [PAYING]      ▼
                                 │                           [REFUNDED]
                                 │                              (终态)
                                 │ REFUND_FAILED
                                 ▼
                            [REFUND_FAILED] ── RETRY_REFUND ──▶ [REFUNDING]
```

对应的迁移表（可以直接进设计文档）：

| From | Event | Guard | Action（本地/外部） | To | 决策者 |
| --- | --- | --- | --- | --- | --- |
| CREATED | PAY_SUBMITTED | 金额 > 0 且库存已锁 | 写支付单 / 发支付命令 | PAYING | 订单服务 |
| PAYING | PAY_CONFIRMED | 支付单号匹配、金额相等 | 记支付流水 / Outbox 通知履约 | PAID | 订单服务 |
| PAYING | PAY_FAILED | 失败码可重试 | 释放库存 / 记录失败原因 | PAY_FAILED | 订单服务 |
| PAID | REFUND_REQUESTED | 退款金额 ≤ 实付、无进行中退款 | 创建退款单 / 冻结额度 | REFUNDING | 售后编排者 |
| REFUNDING | REFUND_SUCCEEDED | 账务返回成功 | 记退款分录 / Outbox 通知 | REFUNDED | 售后编排者 |
| REFUNDING | REFUND_FAILED | 重试次数用尽 | 释放冻结 / 告警 | REFUND_FAILED | 售后编排者 |

注意两件事：

1. **每个转移只有一个决策者**（第 29 章的铁律）。表里写「订单服务」的地方，别的服务只能发命令，不能直接 UPDATE。
2. **外部副作用不写在 Action 里直接调用**。Action 只写本地表和 Outbox，由投递器去发消息/调第三方；否则数据库提交成功、消息发送失败，状态和副作用就分叉了（详见第 10 章）。

---

## 30.4 怎么实现：三种落库姿势

### 先给结论

| 姿势 | 真相源 | 当前态查询 | 审计能力 | 并发控制 | 写入放大 | 适合 |
| --- | --- | --- | --- | --- | --- | --- |
| ① 当前状态字段 + 条件更新 | 一行记录 | 主键查询，最快 | 弱（要另加日志） | `WHERE status + version` | 1 行 | 90% 的业务单据 |
| ② 事件表 + 重放 | 事件序列 | 重放或投影表 | 最强（完整历史） | 唯一键 `(aggregate_id, seq)` | 每事件 1 行 | 金融账本、需要审计/回溯 |
| ③ 状态机定义表 | 定义表 + 实例表 | 同 ① | 中（定义可版本化） | 同 ① | 定义读多写少 | 流程频繁变、多租户可配置 |

> 💡 **第三种经常被误解**：它不是「把订单状态存到定义表」，而是「把**允许哪些迁移**存到表里，让引擎按表执行」。实例状态仍然要落在 ① 或 ② 里。定义表解决的是「流程可变」，不是「状态可变」。

### 姿势 ①：当前状态字段 + 条件更新

DDL：

```sql
CREATE TABLE orders (
  id            BIGINT       NOT NULL PRIMARY KEY,
  order_no      VARCHAR(32)  NOT NULL,
  status        VARCHAR(24)  NOT NULL,
  version       INT          NOT NULL DEFAULT 0,
  paid_amount   DECIMAL(18,2) NOT NULL DEFAULT 0.00,
  refund_amount DECIMAL(18,2) NOT NULL DEFAULT 0.00,
  updated_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                             ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_order_no (order_no),
  KEY idx_status_updated (status, updated_at),
  CONSTRAINT chk_status CHECK (status IN (
    'CREATED','PAYING','PAID','PAY_FAILED','REFUNDING',
    'REFUNDED','REFUND_FAILED','CANCELLED'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

Java 迁移服务（Spring `JdbcTemplate`，核心只有一条 SQL）：

```java
public enum Result { OK, ILLEGAL, CONFLICT }

public Result transition(long id, String from, String event, String to,
                         int expectVersion, Consumer<Connection> localAction) {
    return tx.execute(conn -> {
        int n = new JdbcTemplate(new SingleConnectionDataSource(conn, true)).update("""
            UPDATE orders
               SET status = ?, version = version + 1, updated_at = NOW(3)
             WHERE id = ? AND status = ? AND version = ?
            """, to, id, from, expectVersion);
        if (n == 0) {
            // 0 行有两种可能：状态不合法，或版本被别人改了。必须区分。
            Map<String,Object> row = jdbc.queryForMap(
                "SELECT status, version FROM orders WHERE id = ?", id);
            return from.equals(row.get("status")) ? Result.CONFLICT : Result.ILLEGAL;
        }
        localAction.accept(conn);   // 本地动作与状态同事务；外部副作用写 Outbox
        return Result.OK;
    });
}
```

**并发分析**：InnoDB 会对这一行加排他锁，200 个并发 UPDATE 排队执行；第一个匹配 `status='PAYING' AND version=0` 的请求成功并把 version 改成 1，其余 199 个的 `WHERE` 不再匹配，返回 0 行。**不需要分布式锁，也不需要先 SELECT 再 UPDATE**。先查后写反而会引入窗口：两个线程都查到 `PAYING`，都以为自己合法。

**这个姿势的代价**：只有当前态，没有历史。解决方式是同一事务里再写一条迁移日志（§30.4.5），或者上 CDC 把 binlog 变成事件流。前者更简单，推荐。

### 姿势 ②：事件表 + 重放

DDL：

```sql
CREATE TABLE order_events (
  id           BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  aggregate_id BIGINT       NOT NULL,
  seq          INT          NOT NULL,
  event_type   VARCHAR(48)  NOT NULL,
  payload      JSON         NOT NULL,
  actor        VARCHAR(64)  NOT NULL,
  trace_id     CHAR(32)     NOT NULL,
  created_at   DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_agg_seq (aggregate_id, seq),
  KEY idx_agg_created (aggregate_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE order_snapshots (
  aggregate_id BIGINT      NOT NULL PRIMARY KEY,
  seq          INT         NOT NULL,
  state_json   JSON        NOT NULL,
  updated_at   DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

追加事件（`seq` 就是乐观锁）：

```sql
-- 应用层先算出 next_seq = current_seq + 1，再插入。
-- 并发下靠 uk_agg_seq 兜底：后到的那个会拿到 DuplicateKeyException。
INSERT INTO order_events (aggregate_id, seq, event_type, payload, actor, trace_id)
VALUES (?, ?, ?, CAST(? AS JSON), ?, ?);
```

Go 重放（纯函数，不允许读时钟/随机数/网络）：

```go
type Event struct {
    Seq     int             `json:"seq"`
    Type    string          `json:"type"`
    Payload json.RawMessage `json:"payload"`
}

func Replay(events []Event) (Order, error) {
    var o Order
    for _, e := range events {
        switch e.Type {
        case "OrderCreated":
            if o.Status != "" { return o, ErrIllegalTransition }
            o.Status = "CREATED"
        case "PaymentSubmitted":
            if o.Status != "CREATED" { return o, ErrIllegalTransition }
            o.Status = "PAYING"
        case "PaymentConfirmed":
            if o.Status != "PAYING" { return o, ErrIllegalTransition }
            var p struct{ Amount int64 `json:"amount"` }
            if err := json.Unmarshal(e.Payload, &p); err != nil { return o, err }
            o.PaidAmount += p.Amount
            o.Status = "PAID"
        case "RefundSucceeded":
            if o.Status != "REFUNDING" { return o, ErrIllegalTransition }
            o.Status = "REFUNDED"
        default:
            return o, fmt.Errorf("unknown event %q at seq %d", e.Type, e.Seq)
        }
    }
    return o, nil
}
```

**并发分析**：事件表是 append-only，没有「更新同一行」的锁竞争；并发控制点是 `(aggregate_id, seq)` 唯一键。两个请求同时算到 `seq=7`，只有一个能插入成功，另一个收到重复键错误后重新读取、重算、重试。这本质上是一个 CAS 循环，和版本号同构。

**代价**：查询当前态要么重放全部事件（慢），要么维护投影表（多一份写）。快照每 100 个事件打一次，能把重放成本压到常数级；但快照本身也要版本化，否则事件 schema 升级后旧快照读不出来。**事件 schema 只能加字段，不能改语义**，这条规矩要写进评审清单。

### 姿势 ③：状态机定义表

DDL：

```sql
CREATE TABLE sm_definition (
  id          BIGINT      NOT NULL AUTO_INCREMENT PRIMARY KEY,
  machine_key VARCHAR(64) NOT NULL,
  version     INT         NOT NULL,
  status      VARCHAR(16) NOT NULL DEFAULT 'DRAFT',
  created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_machine_version (machine_key, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE sm_transition (
  id            BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  definition_id BIGINT       NOT NULL,
  from_state    VARCHAR(32)  NOT NULL,
  event_type    VARCHAR(48)  NOT NULL,
  to_state      VARCHAR(32)  NOT NULL,
  guard_expr    VARCHAR(512) NOT NULL DEFAULT 'true',
  action_key    VARCHAR(64)  NOT NULL DEFAULT '',
  priority      INT          NOT NULL DEFAULT 0,
  UNIQUE KEY uk_from_event_priority (definition_id, from_state, event_type, priority),
  KEY idx_definition (definition_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

Java 侧只做「查表 + 校验」，不 `eval` 任意代码：

```java
public TransitionPlan plan(String machine, int version, String from,
                           String event, Map<String, Object> ctx) {
    List<TransitionRow> rows = cache.get(machine, version).stream()
        .filter(r -> r.fromState().equals(from) && r.eventType().equals(event))
        .sorted(Comparator.comparingInt(TransitionRow::priority))
        .toList();
    for (TransitionRow r : rows) {
        if (guardEvaluator.eval(r.guardExpr(), ctx)) {   // 只支持白名单表达式
            return new TransitionPlan(r.toState(), r.actionKey());
        }
    }
    throw new IllegalTransitionException(machine, from, event);
}
```

**并发分析**：定义表是只读缓存，实例迁移仍然走 ① 的条件更新。真正要小心的是**定义版本漂移**：实例创建时绑定 `definition_version`，升级定义不能影响在途实例；新版本只对新建实例生效，老实例跑完后自然淘汰。否则一次「把 REFUNDING→REFUNDED 改成 REFUNDING→MANUAL_REVIEW」的配置变更，会让 3 万个在途实例集体非法。

### 并发迁移：乐观锁 + `WHERE status IN (...)`

真实系统里，一个事件常常有多个合法来源状态。比如支付回调可能落在 `PAYING`，也可能落在 `PAY_FAILED`（用户重试后回调才到）。这时用 `IN`：

```sql
UPDATE orders
   SET status = 'PAID', version = version + 1, updated_at = NOW(3)
 WHERE id = ?
   AND status IN ('PAYING', 'PAY_FAILED')
   AND version = ?;
```

Java 重试循环只重试「版本冲突」，不重试「状态非法」：

```java
public Result payConfirmed(long id, int seenVersion) {
    for (int i = 0; i < 3; i++) {
        int n = jdbc.update("""
            UPDATE orders SET status='PAID', version=version+1
             WHERE id=? AND status IN ('PAYING','PAY_FAILED') AND version=?
            """, id, seenVersion);
        if (n == 1) return Result.OK;
        Row row = jdbc.queryForObject(
            "SELECT status, version FROM orders WHERE id=?", Row.class, id);
        if (!Set.of("PAYING", "PAY_FAILED").contains(row.status())) return Result.ILLEGAL;
        seenVersion = row.version();   // 只有版本变了才重读重试
    }
    return Result.CONFLICT;
}
```

> ⚠️ **`IN` 的陷阱**：如果 `PAY_CONFIRMED` 和 `CANCEL` 都允许从 `PAYING` 出发，那么「先支付成功、再取消成功」会连续发生，两个都合法。这不是并发 bug，是**业务规则缺失**。解法是给取消加守卫「未收到支付成功回调」，或者把两个事件收敛成一个「支付结果裁决」事件，由同一个决策者按优先级处理。

### 200 并发测试（Java，可直接进 CI）

```java
@Test
void onlyOneTransitionWinsUnder200Concurrency() throws Exception {
    long id = createOrder("PAYING", 0);
    int workers = 200;
    ExecutorService pool = Executors.newFixedThreadPool(32);
    CountDownLatch start = new CountDownLatch(1);
    CountDownLatch done  = new CountDownLatch(workers);
    AtomicInteger ok = new AtomicInteger();

    for (int i = 0; i < workers; i++) {
        pool.submit(() -> {
            try {
                start.await();
                int n = jdbc.update("""
                    UPDATE orders SET status='PAID', version=version+1
                     WHERE id=? AND status='PAYING' AND version=0
                    """, id);
                if (n == 1) ok.incrementAndGet();
            } catch (Exception e) {
                throw new RuntimeException(e);
            } finally {
                done.countDown();
            }
        });
    }
    start.countDown();
    assertTrue(done.await(30, TimeUnit.SECONDS));
    pool.shutdown();

    assertEquals(1, ok.get());                 // 只有一次迁移成功
    assertEquals("PAID", jdbc.queryForObject(
        "SELECT status FROM orders WHERE id=?", String.class, id));
    assertEquals(1, jdbc.queryForObject(
        "SELECT version FROM orders WHERE id=?", Integer.class, id));
}
```

这条测试证明的不是「UPDATE 写得好」，而是**「状态迁移是幂等的」**：同一事件投递 200 次，只有第一次改变状态，其余全部被 `WHERE` 拦下。第 29 章用 Go 给过同构版本，语言不同，断言相同。

---

## 30.5 可观测：每次迁移都要有 trace

状态机最贵的故障不是「报错」，而是「**静默地停在某个状态**」。所以每一次迁移都必须留下五样东西：`from / to / event / actor / reason`，再加 `trace_id` 和 `version`。

```sql
CREATE TABLE state_transition_log (
  id             BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  aggregate_type VARCHAR(32)  NOT NULL,
  aggregate_id   BIGINT       NOT NULL,
  from_state     VARCHAR(32)  NOT NULL,
  to_state       VARCHAR(32)  NOT NULL,
  event_type     VARCHAR(48)  NOT NULL,
  actor          VARCHAR(64)  NOT NULL,   -- user:123 / svc:order / job:stuck-scan
  reason         VARCHAR(256) NOT NULL DEFAULT '',
  trace_id       CHAR(32)     NOT NULL,
  version        INT          NOT NULL,
  created_at     DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  KEY idx_agg_created (aggregate_type, aggregate_id, created_at),
  KEY idx_to_state_created (to_state, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

三条纪律：

1. **和状态更新同事务**。先改状态再写日志，中间崩了就会出现「状态变了但没人知道为什么」。
2. **append-only**。应用账号只有 `INSERT`/`SELECT` 权限，没有 `UPDATE`/`DELETE`；按月分区，保留 13 个月。
3. **写进 trace**。每次迁移开一个 span，属性至少包含 `aggregate.type`、`aggregate.id`、`state.from`、`state.to`、`event.type`、`state.version`。这样排障时从一笔订单的 trace 就能看到完整路径，而不是去 grep 五个服务。

指标至少四个：

| 指标 | 类型 | 用途 | 告警 |
| --- | --- | --- | --- |
| `state_transition_total{from,to,event,result}` | Counter | 迁移量、非法迁移比例 | `result="illegal"` 突增 |
| `state_illegal_transition_total` | Counter | 代码/配置不一致 | > 0 持续 5 分钟 |
| `state_age_seconds{state}` | Histogram | 每个状态的停留时长 | P99 超 SLA |
| `state_stuck_total{state}` | Gauge | 卡单数 | 按流程分级（第 33 章） |

---

## 30.6 状态设计四错误：每条都给反例与重构

| # | 错误 | 反例 | 后果 | 重构方法 |
| --- | --- | --- | --- | --- |
| 1 | **状态爆炸** | `PAID_UNSHIPPED`、`PAID_SHIPPED`、`PAID_SHIPPED_INVOICED`… 3 个维度乘出 36 个状态 | 每加一个维度要改所有 switch；测试矩阵补不全；状态名越来越长 | 按正交维度拆成多个状态机（支付单/履约单/退款单），各自独立迁移；需要总览时用视图或投影拼 |
| 2 | **状态与标志位混用** | `status='PAID'` + `is_refunded=1` + `is_closed=1`，三个字段互相矛盾 | 出现「已退款但未关闭」「已关闭但可退款」等不可能组合 | 一个聚合只保留一个生命周期 `status`；正交事实用独立 boolean；跨聚合用各自状态机 |
| 3 | **缺终态** | 只有 `CREATED/PAYING/PAID/REFUNDING`，没有 `REFUNDED/FAILED/CANCELLED` | 流程永远「在途」，卡单巡检无法判定；报表无法收敛 | 显式定义终态集合，终态吸收（不允许再迁移）；每条路径必须能到终态或到有 SLA 的等待态 |
| 4 | **状态与业务字段混用** | `status='REFUND_100'`、`status='VIP_PAID'` | 金额/等级一变就要加状态；历史数据无法回算 | 状态只表达生命周期位置；金额、等级、渠道放普通字段；用 `reason_code` 表达原因 |

### 重构示例：把 36 个状态拆成 3 个状态机

```text
重构前：order.status ∈ {PAID_UNSHIPPED, PAID_SHIPPED, REFUNDING_UNSHIPPED, ...}  (36 个)

重构后：
  payment.status  ∈ {INIT, PAYING, PAID, PAY_FAILED, REFUNDED}
  fulfillment.status ∈ {NOT_READY, ALLOCATED, SHIPPED, DELIVERED}
  refund.status   ∈ {NONE, REQUESTED, REFUNDING, REFUNDED, REJECTED}

订单总览 = 三个状态机的投影（只读视图，不作为写入真相）
```

> 💡 **判断标准**：如果两个状态可以同时成立，它们就不该在同一个字段里。`PAID` 和 `SHIPPED` 可以同时成立，所以它们属于两个维度。

---

## 30.7 状态机评审表（设计评审直接念）

| # | 检查项 | 不通过的表现 | 不做的后果 |
| --- | --- | --- | --- |
| 1 | 状态数 ≤ 9，且两两互斥 | 出现 `PAID_AND_REFUNDING` 这类复合名 | 状态爆炸，测试矩阵失控 |
| 2 | 每个状态都有进入事件和离开事件 | 某状态只能靠人工 SQL 离开 | 卡单 |
| 3 | 终态集合显式定义且吸收 | `REFUNDED` 还能被改成 `PAID` | 账目反复 |
| 4 | 每个转移只有一个决策者 | 两个服务都能 UPDATE 同一行 | 丢失更新、互相覆盖 |
| 5 | 守卫可在数据库条件中表达 | 守卫里查了三个服务 | 迁移不可重放、不可测试 |
| 6 | 外部副作用走 Outbox | Action 里直接发 MQ/调第三方 | 状态与副作用分叉 |
| 7 | 迁移日志与状态同事务 | 日志靠事后补 | 审计断链 |
| 8 | 有 `version` 或 `seq` 乐观锁 | 只靠 `WHERE status` | 同状态多事件并发时丢失更新 |
| 9 | 每个非终态有 SLA | 不知道「多久没动算卡住」 | 卡单发现不了（第 33 章） |
| 10 | 迁移矩阵有自动化测试 | 只测了 happy path | 非法迁移线上才暴露 |
| 11 | 定义版本与实例绑定 | 改配置影响在途实例 | 批量非法迁移 |
| 12 | 状态名用过去式/形容词，事件名用过去式 | `DO_PAY` 当状态 | 语义混乱，新人读不懂 |

---

## 30.8 双修对照：订单状态机 vs Agent Run 状态机

| 维度 | 传统系统（订单） | Agent 系统（Run） | 相同 / 差异根因 |
| --- | --- | --- | --- |
| **身份/唯一键** | `order_no` | `run_id` + `step_id` + `tool_call_id` | 结构相同，粒度更细 |
| **状态集合** | CREATED/PAYING/PAID/REFUNDING/REFUNDED | CREATED/QUEUED/PLANNING/ACTING/WAITING_HUMAN/WAITING_EXTERNAL/VERIFYING/SUCCEEDED/FAILED | 多两个等待态，差异根因：长事务+随时中断 |
| **事件** | PAY_CONFIRMED、REFUND_SUCCEEDED | TOOL_RETURNED、APPROVAL_GRANTED、EXTERNAL_CALLBACK | 事件语义相同，Agent 事件更密 |
| **守卫** | 金额、状态、库存 | 预算、步数上限、工具白名单、审批策略 | 守卫维度不同，机制相同 |
| **动作/副作用** | 扣库存、记账、发短信 | 调工具、发邮件、扣 token | 都不可回滚，只能补偿（第三定律） |
| **失败模式** | 重复支付、超卖、状态卡住 | 重复工具调用、死循环、模型自称完成 | Agent 多「非确定性」与「循环」 |
| **恢复方式** | 重跑、补偿、人工修 | checkpoint + 重放 + 工具幂等查询 | 机制相同，Agent 必须假设随时中断 |
| **验证方法** | 200 并发、迁移矩阵 | 200 并发 + kill worker + 重放一致 | 方法相同，Agent 多一层重放测试 |
| **观测指标** | 状态停留时长、非法迁移 | 加 token/成本/工具成功率 | 指标集扩展 |
| **成本结构** | 机器折旧，近似固定 | 每次模型/工具调用都计费 | 差异根因：成本显性且按量 |

**哪些部分完全相同，可以直接复用**：

1. **条件更新 + 版本号**：`UPDATE ... WHERE id=? AND status=? AND version=?`，Agent Run 表一字不改。
2. **迁移日志结构**：`from/to/event/actor/reason/trace_id`，只是 `actor` 从 `user:123` 变成 `agent:planner` 或 `human:approver`。
3. **终态负责人原则**：Run 是否成功不能由模型说「我完成了」决定，必须由代码校验产物/工具返回值（第 29 章）。
4. **卡单巡检**：Agent 的「卡住的 Run」和「卡住的订单」是同一张巡检表（第 33 章）。

**差异的四个根因**（对应总纲 §1.5）：非确定性导致「同输入不同路径」；长事务导致必须有两个等待态；成本显性导致守卫里必须加预算；输出面向人导致终态需要质量校验。第 34 章会把 Run 状态机完整展开，这里只确认：**落库套路不需要为 Agent 改任何一行**。

---

## 30.9 会在哪炸：失效模式清单

| # | 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- | --- |
| 1 | 状态被改回旧值 | 先 SELECT 再 UPDATE，两线程都拿到旧状态 | 对比 `version` 与迁移日志 | 改条件更新 + version | 代码评审禁「先查后写」 |
| 2 | 同一事件重复执行 | 消费重试/回调重放，没有守卫 | 按业务键 group by 看重复副作用 | 加 `WHERE status IN (...)` + 幂等表 | 第 24 章的消费幂等 |
| 3 | 状态合法但业务错 | 守卫缺失，只校验了状态 | 对账：状态与金额/库存是否一致 | 补守卫 + 数据修复 | 迁移矩阵测试 |
| 4 | 在途实例被新定义打挂 | 定义表热更新，实例未绑定版本 | 查实例的 `definition_version` | 回滚定义 + 修实例 | 定义版本化，只对新实例生效 |
| 5 | 日志与状态不一致 | 日志异步写/事后补 | 对比 `updated_at` 与日志时间 | 同事务重写 | 同事务 + append-only 权限 |
| 6 | 终态还能被改 | 没有吸收态约束 | 查 `from_state IN (终态)` 的日志 | 加守卫 + 修数据 | 评审表第 3 条 |
| 7 | 状态字段变成 36 个值 | 多维度混在一个字段 | `SELECT status, COUNT(*)` 看分布 | 拆状态机 | 评审表第 1 条 |
| 8 | 卡单无人发现 | 没有状态年龄指标/SLA | 查最老的非终态记录 | 补巡检（第 33 章） | 每个非终态定义 SLA |

---

## 30.10 怎么验证：四类测试 + 三个指标

| 验证项 | 方法 | 通过标准 |
| --- | --- | --- |
| **并发安全** | §30.4 的 200 并发测试 | 成功数 = 1，version = 1 |
| **迁移矩阵** | 从 `sm_transition` 生成「状态 × 事件」用例，逐条断言允许/拒绝 | 未定义迁移 100% 被拒绝 |
| **重放确定性** | 同一事件序列重放 100 次，比较最终状态 hash | 100 次完全一致 |
| **中断恢复** | 在「改状态后、写 Outbox 前」kill 进程（同一事务会回滚） | 重启后要么全成、要么全不成，无中间态 |
| **卡单发现** | 人为把订单停在 `PAYING` 超过 SLA | 巡检在 5 分钟内发现并告警 |
| **观测完整性** | 随机抽 100 笔订单，检查迁移日志是否覆盖每次状态变化 | 覆盖率 100%，无断链 |

迁移矩阵测试的 SQL 取数：

```sql
-- 找出「定义表里有、但测试用例没覆盖」的迁移
SELECT t.from_state, t.event_type, t.to_state
FROM sm_transition t
JOIN sm_definition d ON d.id = t.definition_id
WHERE d.machine_key = 'order' AND d.version = 3
  AND NOT EXISTS (
    SELECT 1 FROM test_coverage c
    WHERE c.from_state = t.from_state
      AND c.event_type = t.event_type
      AND c.to_state = t.to_state
  );
```

> 💡 **验证的底线**：状态机的测试不是「跑通下单」，而是「**把每条非法路径都走一遍，确认它真的被拒绝**」。只测 happy path 的状态机，等于没测。

---

## 30.11 用什么工具：库与自研的边界

| 方案 | 能给你什么 | 不给你什么 | 什么时候用 |
| --- | --- | --- | --- |
| **Spring Statemachine** | 状态/事件/守卫/动作的 Java DSL，层级状态 | 落库并发、卡单巡检、运维界面 | 单体内、流程复杂但量不大；注意它与 Spring 生命周期绑定较重 |
| **looplab/fsm（Go）** | 轻量 FSM，回调式 | 持久化、版本、审计 | 进程内状态流转、配置解析 |
| **qmuntal/stateless（Go）** | 类型安全的 FSM，支持 guard | 同上 | 同上 |
| **DB + 自研迁移服务** | 完全可控的落库、审计、巡检 | 需要自己写引擎 | **90% 的业务单据**，推荐 |
| **Temporal / BPMN** | 持久化执行、可视化、定时器 | 业务幂等、补偿正确性 | 流程跨天/跨服务，见第 32 章 |

> ⚠️ **最常见的误判**：以为引入状态机库就解决了状态机问题。库只帮你管「内存里的转移合法性」，**落库那层的并发、幂等、审计、卡单，一行都不会替你写**。所以本系列的默认建议是：库可以用，但真相源必须是数据库里的 `status + version + transition_log`。

---

## 30.12 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 定义 | 状态机 = 状态 + 事件 + 迁移 + 守卫 + 动作，五要素缺一不可 |
| 真相源 | 数据库里的 `status + version`，不是内存对象、不是流程图、不是模型上下文 |
| 落库① | 当前状态字段 + 条件更新，90% 场景首选 |
| 落库② | 事件表 + 重放，审计最强，查询最贵，必须配快照/投影 |
| 落库③ | 状态机定义表，解决「流程可变」，不解决「实例状态」 |
| 并发 | `WHERE status IN (...) AND version=?`；0 行要区分非法迁移与版本冲突 |
| 幂等 | 同一事件投递 N 次，只有第一次改变状态；200 并发断言成功数 = 1 |
| 终态 | 必须显式、必须吸收；没有终态的流程一定会卡 |
| 四错误 | 状态爆炸、状态与标志位混用、缺终态、状态与业务字段混用 |
| 观测 | 每次迁移记 `from/to/event/actor/reason/trace_id`，同事务、append-only |
| 双修 | 订单与 Run 的落库套路完全相同；Agent 多两个等待态与预算守卫 |
| 验证 | 并发 200、迁移矩阵、重放 100 次、中断恢复、卡单巡检 |

## 30.13 自检题

1. **为什么「先 SELECT 查状态，再 UPDATE」在并发下是错的？条件更新为什么能解决？**
   > 要点：SELECT 与 UPDATE 之间有窗口，两个线程可能都读到 `PAYING`，都认为自己合法，后写的覆盖先写的（lost update）。条件更新把「当前状态 = 期望状态」放进 `WHERE`，由 InnoDB 的行锁保证「检查 + 写入」原子；受影响行数为 0 就代表这次迁移没赢。再叠加 `version` 是为了区分「状态不对」和「同状态多事件并发」。

2. **事件表 + 重放听起来最「正确」，为什么大多数订单系统不用它？**
   > 要点：查询当前态要重放，成本随事件数增长；必须维护投影/快照，写放大；事件 schema 演进受约束（只能加不能改语义）；团队要接受一套完全不同的事件溯源心智。它适合金融账本、需要完整审计与回溯的场景；普通订单用「当前状态 + 迁移日志」就能同时拿到性能和审计。

3. **`status` 里出现 `PAID_SHIPPED`、`PAID_UNSHIPPED`、`REFUNDING_SHIPPED`，说明设计出了什么问题？怎么改？**
   > 要点：把「支付」「履约」「退款」三个正交维度塞进了一个字段，导致状态数按乘积爆炸。改法：拆成三个独立状态机（支付单/履约单/退款单），各自有状态、事件、守卫、终态；订单总览用只读投影拼出来。判断标准：能同时成立的状态，不该在同一个字段里。

## 30.14 延伸阅读

- 上一章：[29. 编排是什么：Orchestration vs Choreography](29-orchestration-vs-choreography.md)
- 下一章：[31. DAG 编排与依赖管理](31-dag-orchestration.md)
- 相关：[32. 编排引擎选型](32-workflow-engine-selection.md)、[33. 卡单、重跑、跳过与人工干预](33-stuck-order-and-rerun.md)、[34. Agent 场景的状态流转](34-agent-state-flow.md)
- 幂等与条件更新：[2. 幂等的实现光谱](2-idempotency-implementations.md)、[4. 订单场景全套](4-idempotency-order-scenarios.md)
- 外部副作用：[10. 消息表（Outbox）与事务消息](10-outbox-and-transactional-message.md)
- 事件模型：[../llm/6-session-run-event-model.md](../llm/6-session-run-event-model.md)（Run/Step/Event 的状态模型）
- 官方文档（以版本为准）：MySQL 8.0 `UPDATE`/`CHECK CONSTRAINT`/`SKIP LOCKED`；Spring Statemachine 参考文档；`looplab/fsm` 与 `qmuntal/stateless` 的 README
- 总纲：[0-summary.md](0-summary.md)
