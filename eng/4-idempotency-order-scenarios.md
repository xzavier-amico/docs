# 4. 订单场景全套：下单、支付回调、退款、库存

> **状态**：`已完成（初稿）` ｜ **所属**：第一部分 · 幂等 ｜ **编号**：4 / 52
> **本章定位**：把幂等从「一个接口」拉到「一条订单生命周期」。7 个状态迁移点各有各的重复源，其中支付回调是最经典的战场：**验签 + 状态机条件更新 + 幂等表**三件套缺一不可。
> **读法**：§4.3 的 7 点表先看一遍，建立全局；§4.4.2 的支付回调代码可以直接改吧改吧上线；§4.4.3 的超卖/少卖根因分析建议在库存评审会上逐条过。

## 4.1 引子：一次回调，1,300 单错发

2025 年 11 月 11 日 00:03，某电商的支付回调入口在 14 分钟内收到同一笔支付的 **6 次通知**（渠道按 1s / 3s / 10s / 30s / 60s / 300s 退避）。订单服务的处理逻辑是：

```sql
UPDATE orders SET status='PAID', paid_at=NOW(3) WHERE order_no=?;
```

没有 `AND status='CREATED'`。其中一单在 23:59:30 已因超时被关单，库存也已释放；回调把它**从 `CLOSED` 改回 `PAID`**，履约系统看到 `PAID` 就推仓库，**错发 1,300 单**。同一时间，库存服务的扣减是「先 `SELECT available` 再 `UPDATE stock=stock-n`」，两个并发请求都读到 1 件，最后库存变成 **-1,860 件**。

结果：错发 1,300 单、超卖 1,860 件、直接损失 **78 万元**、客诉 **640 起**，从第一笔异常到止血用了 **17 分钟**。复盘时发现：创建订单有唯一索引，支付回调没有状态条件，库存扣减没有条件更新——**7 个幂等点只做了 1 个**。

## 4.2 到底是什么：订单幂等 = 每个状态迁移点都可重放

订单不是一个「接口」，是一台状态机。幂等要做的不是给整台机器加一把锁，而是**给每一条迁移边加一个守卫**：

| 要素 | 订单场景的落法 |
| --- | --- |
| 幂等键 | 创建：`(user_id, client_token)`；回调：`(channel, channel_txn_id)`；退款：`(payment_id, refund_request_id)` |
| 条件更新 | `UPDATE ... WHERE order_no=? AND status=期望的前置状态` |
| 副作用记录 | 库存流水、发货单、退款单、Outbox 事件，都要有自己的唯一键 |
| 结果回放 | 重复回调返回渠道要求的成功格式；重复创建返回已有订单 |

**反例清单**：

| 写法 | 会在哪炸 |
| --- | --- |
| 只给「创建订单」加唯一索引 | 回调、退款、关单照样重复 |
| 回调只验签，不判订单状态 | 已关闭订单被改回已支付（本章事故） |
| 库存 `SELECT` 后 `UPDATE stock=stock-n` | 并发超卖 |
| 退款用 `order_no` 当幂等键 | 第二次部分退款被误判为重复 |
| 关单和支付回调各自 `UPDATE`，没有前置状态 | 两个都成功，订单状态取决于最后写入者 |

> 💡 一句话：**订单的幂等不是「防重复下单」，是「每条状态边只允许一个赢家，且赢家必须留下副作用记录」。**

## 4.3 结构图：7 个幂等点

```
                 ①创建                     ②支付回调                ④发货
  client ───────────────▶ CREATED ──────────────────▶ PAID ──────────────▶ SHIPPED
                           │  ▲                       │                     │
                ③扣库存 ◀──┘  │           ⑤退款 ◀─────┘                     │
                           │  │            │                               ▼
                           │  └────────────┴──────────────────────▶ COMPLETED
                           │
                           └────⑥超时关单────▶ CLOSED
                                                  ▲
                                          ⑦补偿（释放库存/退款）
```

| # | 幂等点 | 风险 | 幂等键 | 实现 | 验证 |
| --- | --- | --- | --- | --- | --- |
| ① | 创建订单 | 用户连点、网关重试 | `(user_id, client_token)` | 唯一索引，冲突读回 | 200 并发同 token → 1 单 |
| ② | 支付回调 | 渠道重试 6 次、乱序 | `(channel, channel_txn_id)` | 验签 + 幂等表 + 状态条件 | 重放 10 次 → 1 条分录 |
| ③ | 库存扣减 | 并发扣减、重试重复扣 | `(order_no, 'DEDUCT')` | `UPDATE ... WHERE available>=n` + 流水表 | 200 并发 → 不超卖、不少卖 |
| ④ | 发货 | 履约系统重试 | `(order_no, 'SHIP')` | 状态条件 + 运单号唯一 | 重复发货请求 → 1 个运单 |
| ⑤ | 退款 | 多次申请、部分退款 | `(payment_id, refund_request_id)` | 唯一索引 + 累计退款条件 | 并发退款 → 不超额 |
| ⑥ | 关单 | 超时任务与手动关单并发 | `(order_no, 'CLOSE')` | `WHERE status='CREATED'` | 支付/关单并发 → 只有一方赢 |
| ⑦ | 补偿 | 补偿任务重跑 | `(order_no, 'COMPENSATE', seq)` | 补偿流水唯一键 + 反向操作 | 重跑 10 次 → 只释放一次 |

> ⚠️ 7 个点里，①③⑤⑦ 是「资源型」幂等（钱、库存、权益），②④⑥ 是「状态型」幂等（订单、履约、关单）。资源型必须落流水表，状态型必须带前置状态。两类都做，才叫全套。

## 4.4 怎么实现：七个点逐个落地

### 4.4.1 创建订单：唯一索引 + 同事务预占库存

```sql
CREATE TABLE orders (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_no     VARCHAR(32) NOT NULL,
  user_id      BIGINT      NOT NULL,
  client_token VARCHAR(64) NOT NULL,       -- 客户端生成，重试不变
  status       VARCHAR(16) NOT NULL,
  total_amount BIGINT      NOT NULL,
  version      BIGINT      NOT NULL DEFAULT 0,
  created_at   DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_order_no (order_no),
  UNIQUE KEY uk_user_token (user_id, client_token),  -- ★ 创建幂等键
  KEY idx_status_created (status, created_at)
) ENGINE=InnoDB;
```

```java
@Transactional
public Order create(CreateOrderReq req) {
    String orderNo = idGen.next();
    try {
        jdbc.update("INSERT INTO orders(order_no,user_id,client_token,status,total_amount) " +
            "VALUES(?,?,?,'CREATED',?)", orderNo, req.userId, req.clientToken, req.amount);
    } catch (DuplicateKeyException e) {
        return jdbc.queryForObject("SELECT * FROM orders WHERE user_id=? AND client_token=?",
            Order.class, req.userId, req.clientToken);   // 回放已有订单
    }
    reserveStock(req, orderNo);   // 同事务：stock_op_log 唯一键 + 条件 UPDATE
    return load(orderNo);
}
```

```go
func (s *OrderService) Create(ctx context.Context, req CreateReq) (*Order, error) {
    orderNo := s.idGen.Next()
    err := s.withTx(ctx, func(tx *sql.Tx) error {
        _, err := tx.ExecContext(ctx, `INSERT INTO orders
            (order_no,user_id,client_token,status,total_amount) VALUES(?,?,?,'CREATED',?)`,
            orderNo, req.UserID, req.ClientToken, req.Amount)
        if err != nil {
            return err
        }
        return s.reserveStock(ctx, tx, req, orderNo)
    })
    if isDuplicate(err) {
        return s.findByToken(ctx, req.UserID, req.ClientToken)
    }
    if err != nil {
        return nil, err
    }
    return s.load(ctx, orderNo)
}
```

**验证**：200 并发同 `client_token`，`orders` 只 1 条，`stock_op_log` 只 1 条 `DEDUCT`，库存只减一次。见 §4.7。

### 4.4.2 支付回调：验签 + 幂等表 + 状态机，三件套缺一不可

支付回调是全书的「幂等珠峰」：渠道会重试、会乱序、会重复通知，而且**通知成功不代表业务成功**。三件套的顺序必须是：

1. **验签**：用原始 body（不要反序列化再拼回去），HMAC-SHA256 + 常量时间比较 + 时间窗（防重放）。
2. **幂等表**：`(channel, channel_txn_id)` 唯一，插入与业务在**同一个事务**。
3. **状态机**：`UPDATE ... WHERE status='CREATED'`；0 行要区分「已支付」与「已关闭」。

```sql
CREATE TABLE pay_notify_log (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  channel        VARCHAR(16) NOT NULL,
  channel_txn_id VARCHAR(64) NOT NULL,
  order_no       VARCHAR(32) NOT NULL,
  payload_hash   CHAR(64)    NOT NULL,
  created_at     DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_channel_txn (channel, channel_txn_id)  -- ★ 回调幂等键
) ENGINE=InnoDB;

CREATE TABLE pay_exception (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_no       VARCHAR(32) NOT NULL,
  channel_txn_id VARCHAR(64) NOT NULL,
  reason         VARCHAR(64) NOT NULL,       -- ORDER_CLOSED / AMOUNT_MISMATCH
  created_at     DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_exception (order_no, channel_txn_id)
) ENGINE=InnoDB;
```

```java
@Transactional
public void handlePayNotify(String rawBody, String signature, String timestamp) {
    // 1) 验签：原始 body、常量时间比较、5 分钟时间窗
    if (!paySigner.verify(rawBody, signature, timestamp)) {
        throw new BizException("INVALID_SIGN");
    }
    PayNotify n = json.read(rawBody, PayNotify.class);

    // 2) 幂等表：与业务同事务；0 行说明已处理过
    int inserted = jdbc.update("INSERT IGNORE INTO pay_notify_log" +
        "(channel,channel_txn_id,order_no,payload_hash,created_at) VALUES(?,?,?,?,NOW(3))",
        n.channel, n.channelTxnId, n.orderNo, sha256(rawBody));
    if (inserted == 0) return;   // 重复通知，直接回成功

    // 3) 状态机：只允许 CREATED -> PAID
    int updated = jdbc.update("UPDATE orders SET status='PAID', paid_at=NOW(3), version=version+1 " +
        "WHERE order_no=? AND status='CREATED'", n.orderNo);
    if (updated == 0) {
        String st = jdbc.queryForObject("SELECT status FROM orders WHERE order_no=?",
            String.class, n.orderNo);
        if ("PAID".equals(st)) return;   // 并发重复，另一个事务已处理
        // CLOSED：钱已收到但单已关，不能改状态；记账 + 触发退款，别让渠道无限重试
        jdbc.update("INSERT IGNORE INTO pay_exception(order_no,channel_txn_id,reason) " +
            "VALUES(?,?,?)", n.orderNo, n.channelTxnId, st);
        return;
    }
    outbox.append("order.paid", n.orderNo);   // 同事务，别在这里直接发 MQ
}
```

```go
func (s *PayService) HandleNotify(ctx context.Context, raw []byte, sig, ts string) error {
    if !verify(raw, sig, ts, s.secret) {
        return ErrInvalidSign
    }
    var n Notify
    if err := json.Unmarshal(raw, &n); err != nil {
        return err
    }
    return s.withTx(ctx, func(tx *sql.Tx) error {
        res, err := tx.ExecContext(ctx, `INSERT IGNORE INTO pay_notify_log
            (channel,channel_txn_id,order_no,payload_hash,created_at)
            VALUES(?,?,?,?,NOW(3))`,
            n.Channel, n.ChannelTxnID, n.OrderNo, sha256Hex(raw))
        if err != nil {
            return err
        }
        if affected, _ := res.RowsAffected(); affected == 0 {
            return nil // 重复通知
        }

        res, err = tx.ExecContext(ctx, `UPDATE orders
            SET status='PAID', paid_at=NOW(3), version=version+1
            WHERE order_no=? AND status='CREATED'`, n.OrderNo)
        if err != nil {
            return err
        }
        if affected, _ := res.RowsAffected(); affected == 0 {
            var st string
            if err := tx.QueryRowContext(ctx,
                `SELECT status FROM orders WHERE order_no=?`, n.OrderNo).Scan(&st); err != nil {
                return err
            }
            if st == "PAID" {
                return nil
            }
            _, err = tx.ExecContext(ctx, `INSERT IGNORE INTO pay_exception
                (order_no,channel_txn_id,reason) VALUES(?,?,?)`,
                n.OrderNo, n.ChannelTxnID, st)
            return err
        }
        _, err = tx.ExecContext(ctx, `INSERT INTO outbox
            (event_type,aggregate_id,payload,created_at) VALUES('order.paid',?,?,NOW(3))`,
            n.OrderNo, string(raw))
        return err
    })
}

func verify(raw []byte, sig, ts, secret string) bool {
    t, err := strconv.ParseInt(ts, 10, 64)
    if err != nil || math.Abs(float64(time.Now().UnixMilli()-t)) > 5*60*1000 {
        return false
    }
    mac := hmac.New(sha256.New, []byte(secret))
    mac.Write(raw)
    mac.Write([]byte("." + ts))
    expected := hex.EncodeToString(mac.Sum(nil))
    return hmac.Equal([]byte(expected), []byte(sig))
}
```

**三个容易做错的点**：

| 错误 | 后果 | 正解 |
| --- | --- | --- |
| 用反序列化后的 JSON 再序列化去验签 | 字段顺序/空格变了，验签永远失败或误放行 | 用**原始 body** 验签 |
| 先返回渠道成功，再异步处理 | 进程崩溃 → 渠道不重试，钱丢了 | 事务提交后再回成功；失败回失败让渠道重试 |
| `CLOSED` 订单直接 `UPDATE` 成 `PAID` | 已释放的库存/已退款订单被二次履约 | 记 `pay_exception`，触发退款，不改状态 |

### 4.4.3 库存扣减：一条 `WHERE` 挡住超卖，一条流水挡住少卖

```sql
CREATE TABLE stock (
  sku_id    BIGINT UNSIGNED NOT NULL,
  available INT NOT NULL,
  reserved  INT NOT NULL DEFAULT 0,
  version   BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (sku_id),
  CHECK (available >= 0)
) ENGINE=InnoDB;

CREATE TABLE stock_op_log (
  id       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  op_no    VARCHAR(96) NOT NULL,       -- order_no + ':' + DEDUCT/RELEASE
  sku_id   BIGINT      NOT NULL,
  qty      INT         NOT NULL,
  op_type  VARCHAR(16) NOT NULL,
  order_no VARCHAR(32) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_op_no (op_no)          -- ★ 库存操作幂等键
) ENGINE=InnoDB;
```

**写法 A：条件更新（推荐，一条 SQL 搞定）**

```sql
START TRANSACTION;
INSERT INTO stock_op_log(op_no,sku_id,qty,op_type,order_no)
VALUES ('ORD20260101001:DEDUCT',1001,2,'DEDUCT','ORD20260101001');

UPDATE stock
SET available = available - 2, reserved = reserved + 2
WHERE sku_id = 1001 AND available >= 2;
-- 影响行数 1：扣减成功；0：库存不足，回滚整个事务
COMMIT;
```

**写法 B：乐观锁（需要读版本，适合有复杂校验的场景）**

```sql
UPDATE stock
SET available = available - 2, reserved = reserved + 2, version = version + 1
WHERE sku_id = 1001 AND version = 7 AND available >= 2;
-- 0 行：版本冲突或库存不足；重读后由业务决定重试还是拒绝
```

```java
// Java：先查流水，再扣减；重试不会重复扣
int n = jdbc.update("UPDATE stock SET available=available-?, reserved=reserved+? " +
    "WHERE sku_id=? AND available>=?", qty, qty, skuId, qty);
if (n == 0) {
    StockOp op = jdbc.queryForObject("SELECT * FROM stock_op_log WHERE op_no=?", ...);
    if (op != null) return;                 // 之前已扣成功，重试直接返回
    throw new SoldOutException(skuId);      // 真的不够
}
```

```go
res, err := tx.ExecContext(ctx, `UPDATE stock
    SET available=available-?, reserved=reserved+?
    WHERE sku_id=? AND available>=?`, qty, qty, skuID, qty)
if err != nil { return err }
if n, _ := res.RowsAffected(); n == 0 {
    var exists int
    err := tx.QueryRowContext(ctx,
        `SELECT COUNT(*) FROM stock_op_log WHERE op_no=?`, opNo).Scan(&exists)
    if err != nil { return err }
    if exists > 0 { return nil }            // 已扣过
    return ErrSoldOut
}
```

**超卖的两个根因**：

1. **先查后减**：`SELECT available` 读到 1，两个线程都判断「够」，然后 `UPDATE stock=stock-1` 不带条件 → 库存 -1。修：把判断写进 `WHERE available >= n`，让 InnoDB 行锁裁决。
2. **缓存与库不一致**：Redis 预扣成功、DB 写失败；或 Redis 扣了、DB 没扣，后续以 Redis 为准继续卖。修：Redis 只做「预筛/限流」，最终扣减必须落 DB 条件更新；缓存与库用对账兜底（第 14 章）。

**少卖的两个根因**：

1. **超时重试被当成「库存不足」**：第一次扣减已提交，响应丢了；重试时 `available` 已经不够，代码直接返回 `SoldOut`，订单取消但库存还挂在 `reserved` 里。修：先按 `op_no` 查流水，存在就返回成功（上面的代码）。
2. **关单没有释放预占**：`reserved` 只增不减，越卖越少。修：关单成功后用 `op_no=order:RELEASE` 做条件释放，释放本身也幂等；对账 SQL 查「`reserved > 0` 且订单已关闭」的记录。

> 💡 记住一句话：**`WHERE available >= n` 防超卖，`stock_op_log` 防少卖。** 两个都要，只做一个都会在高峰期出事。

### 4.4.4 发货：状态条件 + 运单号唯一

```sql
UPDATE orders SET status='SHIPPED', shipped_at=NOW(3)
WHERE order_no=? AND status='PAID';

INSERT INTO shipment(order_no, waybill_no, carrier, created_at)
VALUES (?,?,?,NOW(3));   -- shipment 上有 UNIQUE KEY uk_order_no (order_no)
```

履约系统重试时，`UPDATE` 影响 0 行 → 回查状态：已是 `SHIPPED` 就返回已有运单；不是 `PAID` 就报「状态不允许发货」。**不要用「查一下有没有运单」再插入**——那又回到先查后写。运单号本身也要唯一（`uk_carrier_waybill`），防止重复打单。

### 4.4.5 退款：一个支付可以退 N 次

订单层的退款幂等键是 `(order_no, refund_request_id)`，资金层是 `(payment_id, refund_request_id)`。两层都要，因为一个订单可能分多笔支付（组合支付）。

```sql
CREATE TABLE refund_order (
  refund_no  VARCHAR(40) NOT NULL,
  order_no   VARCHAR(32) NOT NULL,
  payment_id VARCHAR(40) NOT NULL,
  request_id VARCHAR(64) NOT NULL,
  amount     BIGINT      NOT NULL,
  status     VARCHAR(16) NOT NULL,
  PRIMARY KEY (refund_no),
  UNIQUE KEY uk_order_request (order_no, request_id),   -- ★ 订单层幂等
  UNIQUE KEY uk_payment_request (payment_id, request_id) -- ★ 资金层幂等
) ENGINE=InnoDB;

UPDATE payment
SET refunded_amount = refunded_amount + ?, version = version + 1
WHERE payment_id = ? AND status='SUCCESS'
  AND refunded_amount + ? <= paid_amount;
```

0 行时先查 `uk_payment_request`：已存在 → 返回已有退款单；不存在 → 超额或状态不对。**退款回调**同样要 `(channel, channel_refund_id)` 唯一 + 状态机 `REFUNDING -> REFUNDED`，不能只验签。

### 4.4.6 关单：超时任务与手动关单只能活一个

```sql
UPDATE orders
SET status='CLOSED', closed_at=NOW(3)
WHERE order_no=? AND status='CREATED'
  AND created_at < NOW(3) - INTERVAL 30 MINUTE;
```

超时任务和运营手动关单都走这一条；影响 1 行的那个负责释放库存（`op_no=order:RELEASE`）和关单事件。影响 0 行 → 回查：已 `PAID` 就不能关，已 `CLOSED` 就返回成功。**支付回调与关单并发**时，两边都带 `WHERE status='CREATED'`，InnoDB 行锁保证只有一个赢；输的那方必须走「已支付→退款」或「已关闭→拒绝支付」，不能强行覆盖。

### 4.4.7 补偿：重跑十次也只生效一次

补偿不是「再执行一次原操作」，而是一个**独立的、有自己幂等键的操作**：

```sql
CREATE TABLE compensation_log (
  compensate_no VARCHAR(96) NOT NULL,   -- order_no + ':' + 补偿类型 + ':' + seq
  order_no      VARCHAR(32) NOT NULL,
  comp_type     VARCHAR(32) NOT NULL,   -- RELEASE_STOCK / REFUND / CANCEL_SHIPMENT
  status        VARCHAR(16) NOT NULL,
  created_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (compensate_no)
) ENGINE=InnoDB;
```

每个补偿动作先插 `compensation_log`（唯一键），再执行反向操作；重跑时唯一键冲突 → 读状态，`DONE` 直接返回。补偿失败要有重试上限（比如 8 次）和死信队列，不能无限重试打爆下游（第 13 章）。

## 4.5 双修对照：订单的 7 个点，Agent 一个不少

Agent 调工具下单、发邮件、删文件、提审批，和订单生命周期是同一张状态机图，只是「谁决定下一步」从人变成了模型。

| 维度 | 订单系统 | Agent 工具调用 | 相同 / 差异根因 |
| --- | --- | --- | --- |
| **机制本体** | 状态机 + 唯一键 + 补偿 | 完全相同 | 一套代码两边用 |
| **身份/唯一键** | `order_no` / `client_token` | `run_id` + `step_id` + `tool_call_id` | 结构同；Agent 多步骤粒度 |
| **重复来源** | 用户连点、渠道回调、网关重试 | 再加模型重试、工具重放、HITL 恢复 | Agent 多两类 |
| **谁决定执行** | 代码 / 用户 | **模型自己决定**（可能反复决定） | 差异根因：非确定性 |
| **恢复方式** | 查订单、退款、补偿 | 查 `step_id` 结果、checkpoint、重新规划 | 相同 + 长事务检查点 |
| **成本** | 退款手续费、错发物流 | 重复工具调用 + 重复 token 计费 | 差异根因：成本显性 |
| **验证** | 200 并发 + 对账 | trace 重放 + 工具调用计数对账 | 方法同构 |

**完全相同的部分**：创建订单的唯一索引、支付回调的三件套、库存条件更新、退款/关单的状态机、补偿流水。Agent 侧不需要发明新表，只需要把 `order_no` 换成 `run_id:step_id:tool_call_id`。

**差异根因**：传统订单的「要不要下单」是人点的，Agent 的「要不要调工具」是模型生成的。所以 Agent 多两个问题：① **模型可能反复决定调用同一个工具**——幂等键必须由编排层生成并透传，不能由模型生成；② **工具调用可能跨越 HITL 审批**，审批前后是两次进程，`tool_call_id` 必须持久化，恢复时复用，不能新生成。第三，Agent 的「补偿」往往是再调一个工具（撤销订单/删文件），这个工具本身也必须幂等——补偿链的终点仍然是幂等。

> ⚠️ 一个真实坑：Agent 在「下单工具」超时后重新规划，生成了新的 `tool_call_id`，结果下了两单。正解：把「用户的一次购买意图」定为一个 `run_id`，工具调用的幂等键由 `run_id + 工具名 + 规范化参数` 生成；模型重试不换键，只有用户发起新意图才换。

## 4.6 会在哪炸：订单场景六个高频点

| 现象 | 根因 | 怎么查 | 怎么修 |
| --- | --- | --- | --- |
| 已关闭订单被改成已支付 | 回调 `UPDATE` 没带 `AND status='CREATED'` | 查 `orders` 状态与 `pay_notify_log` 时间线 | 条件更新 + `pay_exception` + 自动退款 |
| 回调验签总失败 | 用反序列化后的 JSON 重新序列化验签 | 对比原始 body 与重序列化 body 的 hash | 用原始 body 验签 |
| 库存超卖 | 先查后减，或 `UPDATE` 不带 `available>=n` | `SELECT * FROM stock WHERE available<0` | 条件更新 + 流水表 |
| 库存少卖/订单取消但库存没释放 | 重试把「已扣过」当成「库存不足」 | `stock_op_log` 有记录但订单 `CLOSED` | 先查 `op_no`，存在即成功；关单释放 |
| 重复退款 | 用 `order_no` 当退款幂等键 | `SUM(refund.amount) > paid_amount` | `(payment_id, refund_request_id)` 唯一键 |
| 补偿打爆下游 | 补偿失败无限重试 | `compensation_log` 同键重试次数 | 重试上限 + 死信 + 人工 |

## 4.7 怎么验证：并发、重放、对账三件套

**① 200 并发创建**（同一个 `client_token`）：

```bash
seq 1 200 | xargs -P 200 -I{} curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST http://localhost:8080/api/orders \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: ct-20260101-001' \
  -d '{"userId":42,"skuId":1001,"qty":2}'
```

**② 回调重放 10 次**（同一个 `channel_txn_id`，模拟渠道退避）：

```bash
for i in $(seq 1 10); do
  curl -s -o /dev/null -X POST http://localhost:8080/api/pay/notify \
    -H 'Content-Type: application/json' \
    -H 'X-Signature: <valid-signature>' \
    -d '{"channel":"ALIPAY","channelTxnId":"ALI-20260101-001","orderNo":"ORD-001","amount":50000}'
  sleep 1
done
```

**③ 对账 SQL**（每次上线前跑一遍）：

```sql
-- 1. 同一回调只应有一条通知记录
SELECT channel, channel_txn_id, COUNT(*) AS cnt
FROM pay_notify_log GROUP BY channel, channel_txn_id HAVING cnt > 1;

-- 2. 同一 client_token 只应有一单
SELECT user_id, client_token, COUNT(*) AS cnt
FROM orders GROUP BY user_id, client_token HAVING cnt > 1;

-- 3. 库存不能为负，且已关闭订单不应占着 reserved
SELECT sku_id, available, reserved FROM stock WHERE available < 0;
SELECT o.order_no, s.reserved
FROM orders o JOIN stock_op_log l ON l.order_no=o.order_no
JOIN stock s ON s.sku_id=l.sku_id
WHERE o.status='CLOSED' AND l.op_type='DEDUCT'
  AND NOT EXISTS (SELECT 1 FROM stock_op_log r
                  WHERE r.op_no=CONCAT(o.order_no,':RELEASE'));

-- 4. 已支付订单必须有回调记录；有回调必须有订单
SELECT o.order_no FROM orders o
LEFT JOIN pay_notify_log p ON p.order_no=o.order_no
WHERE o.status='PAID' AND p.id IS NULL;
SELECT p.channel_txn_id FROM pay_notify_log p
LEFT JOIN orders o ON o.order_no=p.order_no WHERE o.id IS NULL;
```

期望：前三条 0 行；第四条的两个查询 0 行。**如果第 3 条查出「已关闭但没释放」，不要直接改 `reserved`，走补偿任务重放 `RELEASE`**。

## 4.8 用什么工具

- **数据库**：MySQL 8.0（唯一索引、条件更新、`INSERT IGNORE`）；库存热点可用 Redis 做预筛，但最终扣减必须落 DB。
- **回调验签**：渠道 SDK 的 `verify` 方法优先；自己实现时用 `Mac`/`hmac` + 常量时间比较，时间窗 5 分钟。
- **异步**：Outbox 表 + 投递器（第 10 章），不要在回调事务里直接发 MQ。
- **压测**：k6 / 自研 Go 脚本（第 5 章给完整代码）；渠道沙箱支持重放回调，务必在沙箱里跑 10 次重放。
- **故障注入**：Chaos Mesh 注入 MySQL 延迟/杀 Pod/Redis 故障，观察 `pay_exception` 与补偿任务。

## 4.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 7 个点 | 创建、回调、库存、发货、退款、关单、补偿，一个都不能少 |
| 创建 | `(user_id, client_token)` 唯一；冲突读回已有订单 |
| 回调 | 验签（原始 body）+ 幂等表（同事务）+ 状态条件（`WHERE status='CREATED'`） |
| 已关闭订单收到支付 | 不改状态；记 `pay_exception`，触发退款 |
| 库存 | `WHERE available>=n` 防超卖；`stock_op_log` 防少卖 |
| 退款 | `(payment_id, refund_request_id)`；累计退款 `<= paid_amount` |
| 关单 | 与支付回调抢同一个 `WHERE status='CREATED'`，只有一个赢家 |
| 补偿 | 独立幂等键 + 重试上限 + 死信，不要无限重试 |
| 验证 | 200 并发 + 回调重放 10 次 + 四条对账 SQL |
| 双修 | 同一张状态机；Agent 多「模型决定调用」与 token 成本 |

## 4.10 自检题

1. **支付回调已经验签通过，为什么还必须查订单状态？**
   > 要点：验签只证明「通知来自渠道」，不证明「这笔通知该被应用」。渠道会重试、会乱序；订单可能已关闭、已退款、金额不符。必须用 `UPDATE ... WHERE status='CREATED'` 做状态守卫，0 行时区分「已支付（幂等成功）」与「已关闭（记异常+退款）」。

2. **库存扣减的 `UPDATE ... WHERE available >= n` 已经防了超卖，为什么还需要 `stock_op_log`？**
   > 要点：防少卖。第一次扣减成功但响应超时，重试时 `available` 可能已不足，代码会误判「售罄」并取消订单，而库存其实已扣。`op_no` 流水让重试能识别「已扣过」，直接返回成功；关单释放也需要流水防重复释放。

3. **订单已关闭，支付回调才到，应该把状态改回 `PAID` 吗？**
   > 要点：不能。关闭时库存已释放、可能已退款；改回 `PAID` 会造成二次履约。正确做法：`pay_notify_log` 记下通知，`pay_exception` 记 `ORDER_CLOSED`，触发原路退款/人工处理，向渠道返回成功避免无限重试（钱已收到，退款是另一条幂等链路）。

## 4.11 延伸阅读

- 上一章：[3. 金融级幂等：流水号、分录、冲正与对账](3-idempotency-finance.md)
- 下一章：[5. 幂等的失效模式与测试：10 种翻车方式](5-idempotency-failure-modes.md)
- 相关：[10. 消息表与事务消息](10-outbox-and-transactional-message.md)、[12. 跨服务一致性实战](12-cross-service-consistency.md)、[14. 对账体系](14-reconciliation-system.md)
- 平台侧：[../llm/22-tool-system-mcp.md](../llm/22-tool-system-mcp.md)（工具调用与审批中断）


