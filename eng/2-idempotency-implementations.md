# 2. 幂等的实现光谱：8 种方案与选型

> **状态**：`已完成（初稿）` ｜ **所属**：第一部分 · 幂等 ｜ **编号**：2 / 52
> **本章定位**：把「加个唯一索引」这句正确的废话拆成 8 种可落地的方案，逐一看清它们的**裁决点**、**并发窗口**和**被绕过的条件**。读完你该能在设计评审上五分钟选对方案，并说清为什么不用另一个。
> **读法**：§2.4 八段结构统一（原理 → 代码 → 窗口 → 边界），可按需跳读；§2.8 的选型矩阵可以直接抄进设计文档。定义与边界见第 1 章，本章不重复。

## 2.1 引子：两场「都做了幂等」的事故

**事故一：TTL 比重试窗口短，重复入账 217 万（2025-06-18）**

某电商大促，支付渠道按 1m / 5m / 15m / 30m / 1h 退避重试回调。订单服务用 Redis `SETNX callback:{channelTxnId}` 做幂等，TTL 设了 10 分钟——理由是「回调一般 1 分钟内就到」。当天渠道批量出款延迟，首笔回调 00:07 到达，第二次 00:41 才来，key 已经过期。**重复入账 1,842 笔、217 万元**，T+1 对账才发现。这里不是「没做幂等」，是**幂等记忆比重复窗口短**。

**事故二：唯一索引冲突被当成「重复请求」，超卖 3,100 件（2025-09-02）**

库存服务的 `stock_op_log` 上有唯一键 `(sku_id, order_no)`。一次批量导入把订单号生成器的前缀配错，20 万个订单号碰撞到 1.2 万个。代码里：

```java
try {
    stockMapper.deduct(skuId, qty, orderNo);
} catch (DuplicateKeyException e) {
    return true;   // 「重复请求，已处理」
}
```

8,600 次扣减被静默吞掉，实际卖出 3,100 单没扣库存，**超卖 3,100 件、损失 96 万元**。唯一索引冲突只说明「这个键已存在」，**不说明「这次请求是重复的」**——键本身可能撞了。

> 🎯 两场事故的共同点：方案都「上了」，但没人回答三个问题——**键是什么、谁先裁决、输了的人看到什么**。这就是本章的全部内容。

## 2.2 到底是什么：幂等方案的三要素

一个能用的幂等方案，必须同时具备三样东西，缺一样就是假幂等：

| 要素 | 要求 | 缺了会怎样 |
| --- | --- | --- |
| **幂等键 Identity** | 由客户端生成、跨重试不变、唯一标识一次业务意图 | 每次重试都是新键 → 永远不命中 |
| **裁决点 Arbiter** | 一个原子操作决定「谁先」：唯一索引、`SETNX`、条件 `UPDATE` | 先查后写 → 并发窗口 |
| **结果存储 Outcome** | 记住第一次的结果，第二次能原样回放 | 第二次返回 500/409，调用方继续重试 |

反例：只有 Redis 锁（有裁决、无结果）→ 锁释放后重试再执行一遍；只有结果表但先查后写（有结果、无原子裁决）→ 两个请求都查到「没有」；键里带 `timestamp`（有裁决、键不稳）→ 每次都新建一条。

> 💡 一句话判据：**「第二个请求到达时，它能不能在不执行业务的前提下，拿到和第一个请求一样的答案？」** 能，才是幂等方案；只能「挡住」，那叫去重。

## 2.3 结构图：裁决点在哪，窗口就在哪

```
POST /orders
     │
     ▼
┌─────────────┐   键：tenant + client_token + scope
│ 提取幂等键   │   （绝不含 timestamp / trace_id / 随机数）
└──────┬──────┘
       ▼
┌──────────────────────────┐
│ 原子占位（裁决点）          │  ← 唯一索引 / SETNX / 条件 UPDATE / token 核销
│ 只有一个请求能拿到「首次」  │
└────┬────────────────┬────┘
  首次│                │重复
     ▼                ▼
┌─────────────┐  ┌──────────────────────────────┐
│ 执行业务     │  │ 读结果：                      │
│ 同事务写结果 │  │  DONE       → 回放第一次响应   │
│             │  │  PROCESSING → 409 / 让客户端等 │
└──────┬──────┘  │  FAILED     → 按策略重试       │
       ▼         └──────────────────────────────┘
    COMMIT → 返回
```

把「先查后写」的窗口画出来，就是本章所有事故的母题：

```
T0   A: SELECT ... WHERE request_id='R'   → 0 行
T1   B: SELECT ... WHERE request_id='R'   → 0 行     ← 窗口就在这里
T2   A: INSERT ... COMMIT                 → 成功
T3   B: INSERT ... COMMIT                 → 第二条
```

**窗口的大小 = T1 到 T2 的时间**。单机 1ms，走从库 800ms，跨机房 30ms——只要窗口大于 0，并发就一定能踩中。唯一索引把 T2/T3 合并成一次原子裁决，窗口才消失。

## 2.4 八种方案：原理、代码、窗口、边界

### 2.4.1 唯一索引冲突：让数据库当裁判

**原理**：不查，直接插；唯一索引保证只有一个能成功，输家捕获 1062 后去读赢家的记录。裁决与业务写入在同一个事务里，没有窗口。

```sql
CREATE TABLE orders (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_no      VARCHAR(32)  NOT NULL,
  user_id       BIGINT       NOT NULL,
  client_token  VARCHAR(64)  NOT NULL,
  status        VARCHAR(16)  NOT NULL,
  created_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_order_no (order_no),
  UNIQUE KEY uk_user_token (user_id, client_token)
) ENGINE=InnoDB;
```

```java
// Java：插入即裁决；冲突后必须返回已有订单，不能空手 return success
try {
    jdbc.update("INSERT INTO orders(order_no,user_id,client_token,status) VALUES(?,?,?,'CREATED')",
                orderNo, userId, clientToken);
} catch (DuplicateKeyException e) {
    return jdbc.queryForObject(
        "SELECT * FROM orders WHERE user_id=? AND client_token=?", Order.class, userId, clientToken);
}
```

```go
// Go：用错误码判断，不要匹配错误字符串
_, err := tx.ExecContext(ctx, `INSERT INTO orders(order_no,user_id,client_token,status)
    VALUES(?,?,?,'CREATED')`, orderNo, userID, token)
if isDuplicate(err) { // *mysql.MySQLError.Number == 1062
    return s.findByToken(ctx, userID, token)
}
if err != nil { return err }
```

**并发窗口**：无。A、B 同时插，InnoDB 的唯一索引让一个成功、一个阻塞后报 1062；输家读到的是赢家已提交的行（若赢家事务未提交，输家会等锁）。**能被绕过的条件**：键列可空（MySQL 唯一索引允许多个 `NULL`）；collation 把 `ABC` 和 `abc` 判成同一个；软删除后重建；跨库写入；以及上面事故里的「catch 后不查结果」。

**适用边界**：单库、键不可变、副作用在同一个库。跨库/调三方时，唯一索引只能保证「本地不重复」，保证不了「外部只发生一次」。

### 2.4.2 去重表（request_id 表）：把「记忆」做成一张表

**原理**：单独一张 `idempotency_record`，主键 `(scope, idem_key)`。先 `INSERT` 占位（状态 `PROCESSING`），成功者执行业务，完成后把响应写回同一行；重复者读这一行决定回放还是等待。

```sql
CREATE TABLE idempotency_record (
  scope             VARCHAR(64)  NOT NULL,
  idem_key          VARCHAR(128) NOT NULL,
  request_hash      CHAR(64)     NOT NULL,
  status            ENUM('PROCESSING','DONE','FAILED') NOT NULL,
  response_code     INT          NULL,
  response_body     MEDIUMTEXT   NULL,
  lease_expires_at  DATETIME(3)  NOT NULL,
  created_at        DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (scope, idem_key),
  KEY idx_status_lease (status, lease_expires_at)
) ENGINE=InnoDB;
```

```java
// Java：INSERT IGNORE 占位；0 行说明已存在，读状态
int n = jdbc.update("INSERT IGNORE INTO idempotency_record" +
    "(scope,idem_key,request_hash,status,lease_expires_at) VALUES(?,?,?,'PROCESSING'," +
    "DATE_ADD(NOW(3), INTERVAL 30 SECOND))", scope, key, hash);
if (n == 0) {
    Rec r = load(scope, key);
    if (r.status == DONE) return replay(r);
    if (r.status == PROCESSING) throw new InProgressException();
    // FAILED 或 lease 过期：用条件 UPDATE 抢占，再执行
}
```

```go
// Go：同一事务内占位；RowsAffected=0 表示已存在
res, err := tx.ExecContext(ctx, `INSERT IGNORE INTO idempotency_record
    (scope,idem_key,request_hash,status,lease_expires_at)
    VALUES(?,?,?,'PROCESSING', NOW(3) + INTERVAL 30 SECOND)`, scope, key, hash)
if err != nil { return err }
if n, _ := res.RowsAffected(); n == 0 {
    rec, err := load(ctx, scope, key)
    if err != nil { return err }
    switch rec.Status {
    case "DONE":       return replay(rec)
    case "PROCESSING": return ErrInProgress
    }
}
```

**并发窗口**：插入本身原子，窗口被转移到「占位提交」与「业务提交」之间。**同事务**：崩溃一起回滚，重试可重来；**独立事务**：崩溃留下 `PROCESSING`，必须靠 `lease_expires_at` 回收，否则这个键永久卡死。**能被绕过**：键生成不稳定；`INSERT IGNORE` 把非唯一键错误也吞了（比如列超长被截断后插入成功）；TTL 清理跑在重试窗口内。

**适用边界**：跨库、跨服务、需要回放响应的通用场景。成本是每请求一次 DB 写；QPS 5k 以上要考虑按 `scope` 分表或 Redis 前置，但**钱的场景最终仍要落 DB**。

### 2.4.3 状态机前置 `WHERE status`：让状态迁移本身当锁

**原理**：不单独判重，把「当前状态」写进 `UPDATE` 的 `WHERE`。影响行数 1 = 这次迁移归你；0 = 别人已经迁过，或状态不合法。

```sql
UPDATE orders
SET status='PAID', paid_at=NOW(3), version=version+1
WHERE order_no=? AND status='CREATED';
```

```java
// Java：0 行不是失败，要区分「已支付」和「状态非法」
int n = jdbc.update("UPDATE orders SET status='PAID', paid_at=NOW(3), version=version+1 " +
    "WHERE order_no=? AND status='CREATED'", orderNo);
if (n == 0) {
    String st = jdbc.queryForObject("SELECT status FROM orders WHERE order_no=?", String.class, orderNo);
    if ("PAID".equals(st)) return;                 // 重复回调，幂等成功
    throw new IllegalStateException("order " + orderNo + " in " + st);
}
```

```go
// Go：RowsAffected=0 后必须回查，不能一律当成功
res, err := tx.ExecContext(ctx, `UPDATE orders SET status='PAID', paid_at=NOW(3), version=version+1
    WHERE order_no=? AND status='CREATED'`, orderNo)
if err != nil { return err }
if n, _ := res.RowsAffected(); n == 0 {
    var st string
    if err := tx.QueryRowContext(ctx, `SELECT status FROM orders WHERE order_no=?`, orderNo).Scan(&st); err != nil { return err }
    if st == "PAID" { return nil }
    return fmt.Errorf("order %s in %s", orderNo, st)
}
```

**并发窗口**：InnoDB 行锁保证 CAS 原子，两个回调只有一个能把 `CREATED` 改成 `PAID`。但它只保护「状态」——更新成功后发 MQ 失败，事件就丢了，必须 Outbox（第 10 章）。**能被绕过**：`WHERE` 漏了合法前置状态（如 `PAYING`）导致卡单；终态又出现在 `IN` 列表里导致重复迁移；把「更新 0 行」一律当成功，掩盖了 `CLOSED` 订单收到支付的回调。

**适用边界**：有明确状态机的实体（订单、退款、工单）。无状态动作（发短信、调三方查询）没有「前置状态」可用。

### 2.4.4 乐观锁版本号：防丢失更新，不防重复创建

**原理**：读的时候带出 `version`，写的时候 `WHERE version=?` 且 `version=version+1`。0 行说明有人改过，重读后由业务决定重试还是拒绝。

```sql
UPDATE account_balance
SET balance = balance - ?, version = version + 1
WHERE account_no = ? AND version = ? AND balance >= ?;
```

```java
int n = jdbc.update("UPDATE account_balance SET balance=balance-?, version=version+1 " +
    "WHERE account_no=? AND version=? AND balance>=?", amt, acct, expectedVersion, amt);
if (n == 0) throw new OptimisticLockException("retry or reject");
```

```go
res, err := tx.ExecContext(ctx, `UPDATE account_balance SET balance=balance-?, version=version+1
    WHERE account_no=? AND version=? AND balance>=?`, amt, acct, expectedVersion, amt)
if err != nil { return err }
if n, _ := res.RowsAffected(); n == 0 { return ErrOptimisticLock }
```

**并发窗口**：CAS 原子。**能被绕过**：重试逻辑重新读版本后再次扣款——如果「一次业务意图」被拆成多次 CAS，每次都能成功，钱就多扣了；ABA（版本回绕，实际极少，但用 `BIGINT` 别用 `INT`）；把乐观锁当幂等键，客户端每次重试都带新版本。

**适用边界**：更新已有行、并发编辑、余额扣减。**不适用于 insert，不适用于外部副作用**；它和幂等是正交的两件事，别混为一谈。

### 2.4.5 幂等 token：先取后核销，一次性票据

**原理**：客户端先调 `POST /idempotency-tokens` 拿一个一次性 token（绑定用户 + 操作 + 过期时间），业务请求带上它；服务端用 Lua 原子地「查 + 删」，只有拿到 1 的请求能执行。

```lua
-- KEYS[1]=idem:token:{token}  ARGV[1]=user_id  ARGV[2]=scope
local v = redis.call('GET', KEYS[1])
if not v then return 0 end
local owner, scope = string.match(v, '([^|]+)|(.+)')
if owner ~= ARGV[1] or scope ~= ARGV[2] then return -1 end
redis.call('DEL', KEYS[1])
return 1
```

```java
// Java：取 token 时写入归属，核销时校验归属，防止 token 被换用户重放
String token = UUID.randomUUID().toString();
redis.opsForValue().set("idem:token:" + token, userId + "|order.create", Duration.ofMinutes(30));

Long r = redis.execute(CONSUME_SCRIPT, List.of("idem:token:" + token), userId, "order.create");
if (r == null || r == 0) throw new InvalidTokenException();
if (r == -1) throw new TokenOwnerMismatchException();
// 核销成功后执行业务；业务失败要把 token 标成 FAILED 或允许重放，不能直接烧掉
```

```go
// Go：go-redis 的 Script 会自动 EVALSHA，失败回退 EVAL
n, err := consumeScript.Run(ctx, rdb, []string{"idem:token:" + token}, userID, "order.create").Int()
if err != nil { return err }
switch n {
case 0:  return ErrInvalidToken
case -1: return ErrTokenOwnerMismatch
}
```

**并发窗口**：核销是原子的，同一 token 只有一个请求能拿到 1。但「取 token」与「核销」是两次交互：核销后业务失败，token 已烧掉，用户必须重新取；如果服务端先执行业务再核销，又会出现「业务成功、token 没核销」的重复。**能被绕过**：token 不绑用户/操作（被换用）；Redis 主从异步复制丢 token（故障切换后同一 token 可用两次）；token 过期时间短于用户填写表单的时间。

**适用边界**：客户端可控、低频、人工操作（提交订单、发起支付）。**不适合回调**——调用方不会先来取 token；也不适合高 QPS 机器调用。

### 2.4.6 请求指纹 hash：没有业务键时的兜底

**原理**：把请求体规范化（字段排序、去空白、统一数字格式），加上租户和 API 名做 SHA-256，拿 hash 当幂等键。适合「调用方给不出业务单号」的 API。

```java
// Java：规范化后 hash；绝不能把 timestamp / nonce / trace_id 算进去
String canonical = JsonCanonicalizer.canonicalize(req);   // 字段排序、去空白、统一 0.10 → 0.1
String fp = DigestUtils.sha256Hex(tenantId + "|" + apiName + "|" + canonical);
```

```go
// Go：encoding/json 对 map 的 key 排序是确定的，但要注意 float 与 null
func fingerprint(tenant, api string, body []byte) (string, error) {
    var v any
    if err := json.Unmarshal(body, &v); err != nil { return "", err }
    b, err := json.Marshal(v) // map key 已排序
    if err != nil { return "", err }
    sum := sha256.Sum256(append([]byte(tenant+"|"+api+"|"), b...))
    return hex.EncodeToString(sum[:]), nil
}
```

**并发窗口**：hash 在写之前算好，配合唯一索引就没有窗口。**能被绕过**：同意图不同指纹（字段顺序、`0.1` vs `0.10`、可选字段缺省、i18n 文本、数组顺序）→ 重复；不同意图同指纹（用户真的想下两单同样的商品）→ 误杀。前者靠规范化 + 客户端显式 key，后者靠时间窗或业务规则。

**适用边界**：无自然业务键的 API 去重、机器重试。**不适合**「同参多次是合法」的场景（比如同一账户连续两笔相同金额的充值）。

### 2.4.7 分布式锁（Redis / ZooKeeper）：只串行化，不记忆

**原理**：锁键 = 幂等键，只有持锁者执行；Redis 用 `SET NX PX` + Lua 安全释放，ZooKeeper 用临时顺序节点。它保证「同一时刻只有一个执行者」。

```java
// Java：加锁 + 只删自己的锁；锁的 TTL 必须大于业务 P99
String lockKey = "lock:order:" + userId + ":" + clientToken;
Boolean ok = redis.opsForValue().setIfAbsent(lockKey, requestId, Duration.ofSeconds(10));
if (!Boolean.TRUE.equals(ok)) throw new InProgressException();
try {
    // 先查结果表；没有再执行；执行完写结果表
} finally {
    redis.execute(UNLOCK_SCRIPT, List.of(lockKey), requestId);
}
```

```go
// Go：SetNX + Lua 解锁；不要用 DEL 直接删，会误删别人的锁
ok, err := rdb.SetNX(ctx, lockKey, requestID, 10*time.Second).Result()
if err != nil || !ok { return ErrInProgress }
defer unlockScript.Run(ctx, rdb, []string{lockKey}, requestID)
```

**并发窗口**：锁只保证互斥，不保证「只执行一次」。A 执行完释放，B 的重试 100ms 后到达 → 再执行一遍；锁 TTL 5s、业务 8s → A、B 同时持有；Redis 主从切换 → 锁丢失。**能被绕过**：任何「锁 + 无结果表」的组合。

**适用边界**：短临界区、防止并发写冲突、抢任务。**永远不要单独拿它做幂等**——锁是「排队」，幂等是「记忆」，两件事。

### 2.4.8 幂等中间件 / SDK：把重复劳动收口

**原理**：注解或 HTTP 中间件统一提取键、占位、回放、异常处理。业务只声明「键是什么、作用域是什么、TTL 多长」。

```java
@Target(ElementType.METHOD) @Retention(RetentionPolicy.RUNTIME)
public @interface Idempotent {
    String key();                 // SpEL，如 "#req.requestId"
    String scope();               // 业务类型，如 "order.create"
    long ttlSeconds() default 86400;
}

@Around("@annotation(idem)")
public Object around(ProceedingJoinPoint pjp, Idempotent idem) throws Throwable {
    String key = spel.eval(idem.key(), pjp.getArgs());
    Acquire a = store.acquire(idem.scope(), key, idem.ttlSeconds());
    if (a.state == DONE) return a.result();
    if (a.state == PROCESSING) throw new InProgressException();
    try {
        Object r = pjp.proceed();
        store.complete(idem.scope(), key, r);   // 必须与业务同事务
        return r;
    } catch (Throwable t) {
        store.fail(idem.scope(), key, t);
        throw t;
    }
}
```

```go
// Go：中间件 + Store 接口；注意响应体缓存会占内存，流式接口不适用
func Idempotent(store Store, scope string, keyFn KeyFunc) Middleware {
    return func(next http.Handler) http.Handler {
        return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
            key := keyFn(r)
            st, cached, err := store.Acquire(r.Context(), scope, key, 24*time.Hour)
            if err != nil { http.Error(w, "idem store unavailable", 503); return }
            switch st {
            case StateDone:
                w.Header().Set("Idempotent-Replay", "true")
                w.WriteHeader(http.StatusOK); _, _ = w.Write(cached); return
            case StateProcessing:
                http.Error(w, "request in progress", http.StatusConflict); return
            }
            rw := &recorder{ResponseWriter: w, status: 200}
            next.ServeHTTP(rw, r)
            if rw.status >= 500 { _ = store.Fail(r.Context(), scope, key) } else { _ = store.Complete(r.Context(), scope, key, rw.body.Bytes()) }
        })
    }
}
```

**并发窗口**：SDK 的 `acquire` 与业务 `proceed` 之间没有事务绑定 → 崩溃留 `PROCESSING`；`complete` 在业务提交后写 → 业务成功、结果没存 → 重试重复。正确做法是把 store 绑到业务事务（同一 `DataSource`），或者干脆用「业务表唯一键 + 结果表同事务」。**能被绕过**：注解的 SpEL 取错字段；流式/长任务把响应缓存在内存；`fail` 把业务异常吞掉。

**适用边界**：平台统一治理、大量同构接口。**不能替代业务状态机**，也不适合流式输出与长任务（第 33 章）。

## 2.5 双修对照：同一套裁决点，两种身份

| 维度 | 传统系统 | Agent 系统 | 相同 / 差异根因 |
| --- | --- | --- | --- |
| **身份/唯一键** | `request_id` / `order_no` / `payment_id` | `run_id` + `step_id` + `tool_call_id` | 结构同构；Agent 多一层步骤粒度 |
| **裁决点** | 唯一索引 / 去重表 / 条件 `UPDATE` / token | **完全相同**，外加模型结果缓存 | 机制可 100% 复用 |
| **键的稳定性** | 客户端生成，跨重试不变 | prompt hash 会因时间戳、检索顺序、模型版本漂移 | 差异根因：非确定性 + 输入不稳定 |
| **失败模式** | 回调重试、网关重试、MQ 重投 | 再加模型重试、工具重放、HITL 恢复 | Agent 多两类重复源 |
| **恢复方式** | 查结果表回放；对账兜底 | 查 `step_id` 结果、从 checkpoint 恢复 | 相同 + 长事务检查点 |
| **成本结构** | 一次额外写；重复执行浪费 CPU | 重复调用**直接烧 token**；缓存命中即省钱 | 差异根因：成本显性且按量 |
| **验证方法** | 200 并发同参 + 对账 | 同左 + trace 重放 + token 用量对账 | 方法同构 |

**完全相同的部分**：唯一索引、`idempotency_record` 表结构、状态机条件更新、锁原语、结果回放逻辑。这些没有「Agent 版」，一套代码两边用。

**差异根因只有一个**：传统系统的「同输入」是稳定的，Agent 的不是。所以 Agent 的幂等键必须显式包含 `run_id`（业务意图）、`step_id`（步骤）、`tool_call_id`（工具调用）、模型版本、提示词版本；**不能用 prompt 内容 hash 当唯一键**。另外，Agent 的重复调用即使被拦下，**被浪费的 token 也已经花掉了**——所以幂等要和计费绑定：预扣、结算、冲正三笔账都要幂等（详见第 3 章 §3.5）。

> ⚠️ 一个常见错误：把「模型重试」当成「同一个 `step_id` 的重试」。如果重试换了 `step_id`，工具就会再执行一次；如果没换，模型可能返回不同结果，但工具副作用必须只发生一次。**粒度按「业务意图」定，不按「模型调用」定**（第 1 章已给判据）。

## 2.6 会在哪炸：五个高频点

| 现象 | 根因 | 怎么修 |
| --- | --- | --- |
| 同一业务单两条记录 | 键含 `timestamp` / `trace_id`，每次重试都新键 | 键只用客户端生成的稳定 ID；键 schema 进评审 |
| 接口 200 但业务没落库 | 唯一索引冲突被 `catch` 后直接返回成功 | 冲突后必须查并回放已有结果；禁止空 catch |
| 重试永远 409 | 占位后进程崩溃，`PROCESSING` 没人回收 | `lease_expires_at` + 定时回收；或同事务占位 |
| 锁过期后重复执行 | 锁 TTL < 业务 P99；锁释放后重试才到 | 看门狗续期；锁 + 结果表双保险 |
| 过期后重复 | TTL < 最大重试窗口 | TTL ≥ 重试窗口 × 2；永久表 + 归档 |

完整的 10 种失效模式与测试方法见第 5 章，这里不展开。

## 2.7 怎么验证：200 并发同参，只看三个数

```bash
# 200 个并发，全部带同一个 Idempotency-Key；预期：业务表 1 条，其余 200/409
seq 1 200 | xargs -P 200 -I{} curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST http://localhost:8080/api/orders \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: it-20260101-001' \
  -d '{"userId":42,"skuId":1001,"qty":1}'
```

```sql
-- 断言 1：同一幂等键只有一条业务记录
SELECT client_token, COUNT(*) AS cnt
FROM orders WHERE client_token = 'it-20260101-001'
GROUP BY client_token HAVING cnt > 1;

-- 断言 2：幂等表没有同键多行（主键本身保证，这里是巡检）
SELECT scope, idem_key, COUNT(*) AS cnt
FROM idempotency_record GROUP BY scope, idem_key HAVING cnt > 1;

-- 断言 3：没有卡在 PROCESSING 超过 5 分钟的记录
SELECT COUNT(*) FROM idempotency_record
WHERE status='PROCESSING' AND lease_expires_at < NOW(3) - INTERVAL 5 MINUTE;
```

指标看三个：`idempotency_requests_total{result="hit_completed"}` 的占比、`result="error"` 的速率、`PROCESSING` 超时数。**第一个涨说明上游在重试，第二个涨说明幂等层自己坏了，第三个涨说明恢复逻辑没跑**。指标设计见第 6 章。

## 2.8 选型矩阵与工具

| 方案 | 成本（每请求） | 可靠性 | 侵入性 | 适用场景 | 不适用 / 会被谁绕过 |
| --- | --- | --- | --- | --- | --- |
| 唯一索引 | 1 次 insert | ★★★★★（单库） | 低 | 创建订单、流水、关系表 | 跨库、键可空、软删除 |
| 去重表 | 1 次 insert + 1 次 update | ★★★★☆ | 中 | 跨服务、需要回放响应 | 高 QPS 单表、TTL 管理不善 |
| 状态机 `WHERE` | 1 次 update | ★★★★★ | 低 | 订单/退款/工单迁移 | 无状态动作、漏前置状态 |
| 乐观锁 | 1 次 update | ★★★★☆ | 低 | 余额、库存、并发编辑 | 创建、外部副作用 |
| 幂等 token | 2 次 Redis 往返 | ★★★☆☆ | 中 | 客户端提交、支付发起 | 回调、高 QPS、Redis 丢数据 |
| 请求指纹 | 1 次 hash + 1 次写 | ★★★☆☆ | 低 | 无业务键的 API 去重 | 同参合法的场景、字段不稳定 |
| 分布式锁 | 2 次 Redis 往返 | ★★☆☆☆ | 低 | 短临界区、防并发写 | **单独做幂等**、长事务 |
| 中间件/SDK | 1 次 store 往返 | ★★★☆☆ | 低（业务）/ 高（平台） | 统一治理、同构接口 | 流式、长任务、跨库事务 |

**工具坐标**：MySQL 8.0 唯一索引与 `INSERT ... ON DUPLICATE KEY UPDATE`（注意后者会更新行，不适合纯占位）；Redis 7.x `SET key val NX PX`、`EVAL`/`EVALSHA`；ZooKeeper 3.8 临时顺序节点；Spring `@Transactional` 与 `DuplicateKeyException`；Go 标准库 `database/sql` + `go-sql-driver/mysql`（1062）、`go-redis/v9`。**框架只提供原语，键的设计和事务边界必须你自己写**——这条在第 8 章 Seata、第 10 章 Outbox 还会再出现。

## 2.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 三要素 | 幂等键 + 原子裁决点 + 结果存储，缺一不可 |
| 唯一索引 | 插入即裁决；冲突后要**读回**赢家结果，不能空手返回成功 |
| 去重表 | 占位与业务同事务；独立事务必须配 lease 回收 |
| 状态机 | `UPDATE ... WHERE status=旧态`，0 行要回查，不能一律当成功 |
| 乐观锁 | 防丢失更新，不防重复创建；重试别重新读版本再扣一次 |
| token | 一次性票据；核销后业务失败要允许重放；金融场景落 DB |
| 指纹 | 只适合无业务键的 API；规范化做不好会「同意图不同键」 |
| 分布式锁 | 只串行化，不记忆；单独用一定重复 |
| SDK | 统一实现，但 `acquire`/`complete` 的事务边界是生死线 |
| 验证 | 200 并发同参 → 业务表 1 条、幂等表 1 条、无超时 PROCESSING |
| 双修 | 表结构完全复用；Agent 键必须含 `run_id`/`step_id`/`tool_call_id` + 版本 |

## 2.10 自检题

1. **为什么「Redis 分布式锁」不能单独作为幂等方案？请给出一个具体的重复时序。**
   > 要点：锁只保证互斥，不记忆结果。A 持锁执行 200ms 后释放；客户端的重试在 250ms 到达，B 拿到锁再执行一遍。锁 TTL 小于业务耗时时还会出现 A、B 同时持有。正解：锁 + 结果表/唯一索引，锁只负责减少并发冲突。

2. **唯一索引方案在哪些情况下会被绕过？至少说四种。**
   > 要点：① 键含可变字段（时间戳/随机数）；② 键列可空，MySQL 允许多个 `NULL`；③ collation 大小写/尾空格把不同键判成同一个；④ 软删除后重建；⑤ 跨库写入，唯一索引只管本库；⑥ 冲突异常被 `catch` 后直接返回成功，没有读回结果。

3. **Agent 系统里，为什么不能拿「prompt 内容的 hash」当幂等键？**
   > 要点：prompt 里的时间戳、检索结果顺序、系统提示词版本都会变，同一业务意图每次 hash 不同 → 永远不命中；反过来，不同意图可能 hash 相同 → 误杀。正确做法是用 `run_id` + `step_id` + `tool_call_id` 作为稳定身份，把模型/提示词版本显式放进键，内容 hash 只做辅助校验。

## 2.11 延伸阅读

- 上一章：[1. 幂等的精确定义与边界](1-idempotency-definition.md)
- 下一章：[3. 金融级幂等：流水号、分录、冲正与对账](3-idempotency-finance.md)
- 相关：[4. 订单场景全套](4-idempotency-order-scenarios.md)、[5. 幂等的失效模式与测试](5-idempotency-failure-modes.md)、[10. 消息表与事务消息](10-outbox-and-transactional-message.md)
- 平台侧：[../llm/22-tool-system-mcp.md](../llm/22-tool-system-mcp.md)（工具调用的幂等与重放）



