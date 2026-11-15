# 3. 金融级幂等：流水号、分录、冲正与对账

> **状态**：`已完成（初稿）` ｜ **所属**：第一部分 · 幂等 ｜ **编号**：3 / 52
> **本章定位**：金融系统的幂等不是「加个去重表」，而是**一套账**：全局唯一流水号定身份，借贷分录记副作用，冲正处理不可回滚，对账兜住静默错误。读完你能设计一张「多次请求只产生一条分录」的表，并说清为什么查了再插一定会死。
> **读法**：§3.4.2 的 DDL 可以直接拿去建表；§3.4.3 的冲正 SQL 与 §3.7 的验证 SQL 建议存成脚本。第 1 章的定义、第 2 章的八种方案是前置，本章只讲金融场景多出来的那三层。

## 3.1 引子：1,284 笔单边账与 642 万

2025 年 12 月 31 日年终结算，某支付机构的日终对账出了 1,284 笔「单边账」：渠道侧显示成功，账务侧没有对应分录，合计 **642 万元**。财务先以为是渠道文件重复，查了 6 小时才发现反了——**账务侧多记了**。

时间线是这样的：

- 12 月 31 日 20:00 跨年活动，支付回调 QPS 从 300 涨到 2,400。
- 回调服务判断重复的方式是 `SELECT ... FROM journal_entry WHERE request_id=?`，读的是**只读从库**。
- 主从延迟在高峰期到了 **800ms**；渠道对同一笔支付重试了 5 次（间隔 1s / 3s / 10s / 30s / 60s）。
- 5 次里有 4 次在从库还没追上时查到「无记录」，于是各插了一条分录。
- 流水号生成器是 `yyyyMMddHHmmss + 4 位随机数`，同一秒内碰撞了 **37 次**；`entry_no` 上又没建唯一索引。

结果：**重复分录 5,136 条、多记 642 万元**。财务三个人手工冲正了 3 天，监管报送延迟 2 天，罚了 80 万。复盘时最扎心的一句话是：

> 「我们先查了呀，查不到才插的。」

**查了再插，在并发和主从延迟面前等于没查。** 这一章讲的就是：金融系统到底靠什么保证「一笔业务只有一条分录」。

## 3.2 到底是什么：金融级幂等的四层结构

普通业务的幂等可以只做「唯一键 + 去重表」；金融级必须四层齐全，少一层就有一类事故：

| 层 | 作用 | 缺了会怎样 |
| --- | --- | --- |
| **全局唯一流水号** | 给每一笔业务一个跨库、跨机构都不重复的身份 | 分库后 ID 碰撞；重试生成新号 → 重复 |
| **借贷分录** | 把「钱怎么动」记成不可变的会计事实 | 只改余额不留痕；对账无法解释差额 |
| **冲正** | 对已入账的错误做反向分录，而不是改原账 | 审计断链；历史余额不可重现 |
| **对账** | 用外部权威数据验证内部账 | 静默错误永远发现不了（第 14 章） |

### 六个反例：这些都不算金融级幂等

| 写法 | 为什么错 |
| --- | --- |
| `if (!exists(requestId)) insert();` | 查询与写入之间有窗口；从库延迟会放大到几百毫秒 |
| 用数据库自增 `id` 当流水号 | 分库后重复；跨机构无法对齐；暴露业务量 |
| 用订单号当退款幂等键 | 一个订单可以多次部分退款，第二次会被误判为重复 |
| `UPDATE journal_entry SET amount=?` | 改了历史事实；审计、对账、报表全部失真 |
| `DELETE FROM journal_entry WHERE duplicate` | 删掉了「发生过」的证据；对账缺口无法解释 |
| 幂等表提交了、分录事务回滚了 | 键被永久占用，重试永远返回「已处理」，钱却没记 |

```java
// 反例：Java 里最常见的「查了再插」
if (journalMapper.countByRequestId(requestId) == 0) {   // 读从库，延迟 800ms
    journalMapper.insertEntry(entry);                    // 主库
    journalMapper.insertLines(lines);
}
```

```go
// 反例：Go 版本，同样死在这个窗口里
var cnt int
err := db.QueryRowContext(ctx,
    `SELECT COUNT(*) FROM journal_entry WHERE request_id=?`, reqID).Scan(&cnt)
if err != nil {
    return err
}
if cnt == 0 {
    _, err = tx.ExecContext(ctx,
        `INSERT INTO journal_entry(entry_no,biz_type,request_id,amount,currency) VALUES(?,?,?,?,?)`,
        entryNo, bizType, reqID, amount, currency)
    if err != nil {
        return err
    }
}
```

> 💡 金融级的判据只有一句：**「这笔业务的身份，是不是在它发生之前就确定了，并且写进了一个带唯一约束的地方？」** 是，才谈得上幂等；不是，后面全是补丁。

## 3.3 结构图：一笔支付从请求到对账

```
 支付请求
    │  request_id = 渠道单号 + 业务类型（客户端/渠道生成，跨重试不变）
    ▼
┌──────────────────┐
│ 流水号生成器       │  雪花 / 号段 / DB 序列 → entry_no
└────────┬─────────┘
         ▼
┌─────────────────────────────────────────────────────┐
│ 账务本地事务（同一个 MySQL 连接）                      │
│  1. INSERT journal_entry  ← uk(biz_type, request_id) │
│  2. INSERT journal_line × 2（借/贷，金额相等）          │
│  3. UPDATE account_balance × 2（带 version 条件）       │
│  4. INSERT outbox（可选，用于通知）                     │
└────────┬────────────────────────────────────────────┘
         │ COMMIT
         ▼
   渠道回调 / 日终对账文件
         │
         ▼
┌──────────────────┐     ┌──────────────────┐
│ 渠道流水表         │────▶│ 三方核对           │
│ uk(channel, txn) │     │ 渠道 vs 分录 vs 余额│
└──────────────────┘     └────────┬─────────┘
                                  ▼
                          差异表 → 自动冲正 / 人工工单
```

### 「查了再插」必死的并发时序

```
时刻        线程 A（回调第 1 次）        线程 B（回调第 2 次）        从库
T0 00.100   SELECT ... request_id=R
            → 从库返回 0 行 ─────────────────────────────────────▶ 延迟 800ms
T1 00.300                                SELECT ... request_id=R
                                         → 从库返回 0 行 ──────────▶ 仍是旧数据
T2 00.400   INSERT entry + 2 lines
            UPDATE balance × 2
            COMMIT（主库）
T3 00.900                                                        从库追上
T4 00.950                                INSERT entry + 2 lines
                                         COMMIT（主库）→ 第 2 条分录
```

窗口 = T1 到 T2 = **100ms**（再叠加 800ms 主从延迟，实际能到 900ms）。只要窗口大于 0，5 次重试就能插 5 条。**唯一索引 `uk(biz_type, request_id)` 把 T2 和 T4 变成一次原子竞争**：B 在 T4 收到 1062，事务回滚，一条都不多。

## 3.4 怎么实现：三件套 + 两个场景

### 3.4.1 全局唯一流水号：身份必须先于业务存在

流水号不是「好看的单号」，它是**幂等键的物理载体**。它必须满足三个条件：全局唯一（跨库、跨机构）、趋势递增（对 B+ 树友好）、生成不依赖业务事务（业务回滚了，号可以浪费，但不能重号）。

**雪花（Snowflake）**：64 位 = 1 位符号 + 41 位毫秒时间 + 10 位机器 + 12 位序列。单机每毫秒 4,096 个，1,024 个节点，41 位可用约 69 年。

| 位段 | 位数 | 含义 | 备注 |
| --- | --- | --- | --- |
| sign | 1 | 恒 0 | 保证正数 |
| timestamp | 41 | 相对自定义 epoch 的毫秒 | 别用 1970，否则 2039 年就耗尽 |
| worker | 10 | 机器/机房编号 | 必须由配置中心分配，不能手填 |
| sequence | 12 | 同毫秒内序号 | 溢出则等下一毫秒 |

```java
public final class Snowflake {
    private static final long EPOCH = 1735689600000L; // 2025-01-01
    private final long workerId;
    private long lastTs = -1L, seq = 0L;

    public synchronized long nextId() {
        long now = System.currentTimeMillis();
        if (now < lastTs) {                       // 时钟回拨
            long back = lastTs - now;
            if (back > 5) throw new IllegalStateException("clock moved backwards " + back + "ms");
            while (now < lastTs) now = System.currentTimeMillis();  // 小回拨：等
        }
        if (now == lastTs) {
            seq = (seq + 1) & 0xFFF;
            if (seq == 0) while (now <= lastTs) now = System.currentTimeMillis();
        } else {
            seq = 0;
        }
        lastTs = now;
        return ((now - EPOCH) << 22) | (workerId << 12) | seq;
    }
}
```

```go
type Snowflake struct {
    mu     sync.Mutex
    epoch  int64
    worker int64
    last   int64
    seq    int64
}

func (s *Snowflake) Next() (int64, error) {
    s.mu.Lock()
    defer s.mu.Unlock()
    now := time.Now().UnixMilli()
    if now < s.last {
        if s.last-now > 5 {
            return 0, fmt.Errorf("clock moved backwards %dms", s.last-now)
        }
        for now < s.last {
            now = time.Now().UnixMilli()
        }
    }
    if now == s.last {
        s.seq = (s.seq + 1) & 0xFFF
        if s.seq == 0 {
            for now <= s.last {
                now = time.Now().UnixMilli()
            }
        }
    } else {
        s.seq = 0
    }
    s.last = now
    return ((now - s.epoch) << 22) | (s.worker << 12) | s.seq, nil
}
```

**时钟回拨怎么处理**（三种策略，按回拨幅度选）：

| 回拨幅度 | 策略 | 代价 |
| --- | --- | --- |
| ≤ 5ms | 自旋等待追平 | 请求延迟增加几毫秒 |
| 5ms ~ 1s | 拒绝生成并告警；或「借未来」：用 `lastTs+1` 并递增序列 | 号段出现小空洞；借多了会透支未来时间 |
| > 1s | **停止发号**，切备用节点，人工介入 | 写入不可用，但绝不重号 |

> ⚠️ 不要用「回拨时把 workerId 加一」这种土办法：workerId 是配置中心分配的，运行期改会导致别的节点撞号。也不要用 `System.nanoTime()` 替代：它没有跨进程/跨重启的绝对意义。

**号段（Segment）**：数据库里存 `max_id`，每次取 `step` 个到内存，用完再取。适合「不想依赖时钟、能接受号段空洞」的账务系统。

```sql
CREATE TABLE id_segment (
  biz_tag    VARCHAR(64) NOT NULL,
  max_id     BIGINT      NOT NULL,
  step       INT         NOT NULL DEFAULT 1000,
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (biz_tag)
) ENGINE=InnoDB;

INSERT INTO id_segment(biz_tag, max_id, step) VALUES ('ledger', 0, 1000)
ON DUPLICATE KEY UPDATE biz_tag = biz_tag;

-- 原子取号：LAST_INSERT_ID(expr) 是连接级的，别用别的连接读
UPDATE id_segment SET max_id = LAST_INSERT_ID(max_id + step) WHERE biz_tag = 'ledger';
SELECT LAST_INSERT_ID() AS max_id;
```

拿到的 `max_id` 是本段上界，可用区间是 `[max_id - step + 1, max_id]`。**重启会丢掉未用完的号段**——这是设计允许的：号可以空洞，不可以重复。生产上做双 buffer（剩 20% 时异步取下一段），避免每次取号都打 DB。

| 方案 | 性能 | 趋势递增 | 时钟敏感 | 依赖 | 适用 |
| --- | --- | --- | --- | --- | --- |
| 雪花 | 单机 400 万/秒 | 是（毫秒内） | **是** | 配置中心发 workerId | 高并发、跨库 |
| 号段 | 每 step 一次 DB | 是（段内） | 否 | DB | 账务、允许空洞 |
| DB 序列 | 每次一次 DB | 是 | 否 | DB 单点/分片 | 低并发、强连续 |

### 3.4.2 复式记账：完整 DDL 与三条不变量

金融账本的核心不是「余额表」，而是**分录**。余额只是分录的物化视图，随时可以从分录重算。下面这张 DDL 是本章的地基：

```sql
CREATE TABLE account_balance (
  account_no   VARCHAR(32) NOT NULL,
  account_type VARCHAR(16) NOT NULL,          -- USER / MERCHANT / FEE / SUSPENSE
  currency     CHAR(3)     NOT NULL DEFAULT 'CNY',
  balance      BIGINT      NOT NULL DEFAULT 0, -- 以「分」为单位，禁用浮点
  version      BIGINT      NOT NULL DEFAULT 0,
  status       TINYINT     NOT NULL DEFAULT 1,
  updated_at   DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (account_no, currency),
  CHECK (balance >= 0 OR account_type IN ('MERCHANT','FEE','SUSPENSE'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE journal_entry (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  entry_no          VARCHAR(40) NOT NULL,      -- 全局唯一流水号
  biz_type          VARCHAR(32) NOT NULL,      -- PAY / REFUND / REVERSAL / SETTLE
  request_id        VARCHAR(64) NOT NULL,      -- 业务请求号，跨重试不变
  entry_type        TINYINT     NOT NULL DEFAULT 1, -- 1 正常 2 冲正
  reversed_entry_no VARCHAR(40) NULL,          -- 冲正时指向原凭证
  amount            BIGINT      NOT NULL,      -- 借方合计（=贷方合计）
  currency          CHAR(3)     NOT NULL,
  status            TINYINT     NOT NULL DEFAULT 1, -- 1 posted
  created_at        DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_entry_no (entry_no),
  UNIQUE KEY uk_biz_request (biz_type, request_id),  -- ★ 幂等的核心
  KEY idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE journal_line (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  entry_no    VARCHAR(40) NOT NULL,
  line_no     INT         NOT NULL,
  account_no  VARCHAR(32) NOT NULL,
  direction   ENUM('D','C') NOT NULL,          -- D=借，C=贷
  amount      BIGINT      NOT NULL,
  currency    CHAR(3)     NOT NULL,
  created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_entry_line (entry_no, line_no),
  KEY idx_account_time (account_no, created_at),
  CHECK (amount > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

三条不变量，应用层必须在同一个事务里校验（别指望触发器兜所有业务）：

| 不变量 | 校验 SQL | 违反的后果 |
| --- | --- | --- |
| 每张凭证借贷平衡 | `SUM(IF(direction='D',amount,-amount)) = 0` | 账不平，日终差额 |
| 一张凭证至少两行 | `COUNT(*) >= 2` | 单边账 |
| 余额可由分录重算 | `balance = SUM(借贷净额)` | 余额被绕过分录改动 |

一笔 500 元支付的入账（用户余额 → 商户待结算）：

```sql
START TRANSACTION;
INSERT INTO journal_entry(entry_no,biz_type,request_id,amount,currency)
VALUES ('PAY20260101000001','PAY','CH20260101000001',50000,'CNY');
INSERT INTO journal_line(entry_no,line_no,account_no,direction,amount,currency) VALUES
 ('PAY20260101000001',1,'USER_1001','D',50000,'CNY'),
 ('PAY20260101000001',2,'MERCHANT_2001','C',50000,'CNY');
UPDATE account_balance SET balance=balance-50000, version=version+1
 WHERE account_no='USER_1001' AND balance>=50000;
UPDATE account_balance SET balance=balance+50000, version=version+1
 WHERE account_no='MERCHANT_2001';
COMMIT;
```

> 💡 注意 `uk_biz_request` 的 `biz_type` 不能省。同一个 `request_id` 可能对应「支付」和「退款」两笔不同业务；只用 `request_id` 做唯一键，退款会被支付记录挡住。

### 3.4.3 冲正而非删除：账只能「反向记一笔」

已入账的分录**永远不能 UPDATE，也不能 DELETE**。发现错账时，插入一张 `entry_type=2` 的反向凭证，借贷方向对调，金额不变，并记下 `reversed_entry_no`。

```sql
START TRANSACTION;
-- 1) 冲正凭证头：request_id 用原请求号 + ':REV'，保证冲正本身也幂等
INSERT INTO journal_entry(entry_no,biz_type,request_id,entry_type,reversed_entry_no,amount,currency)
VALUES ('REV20260101000001','REVERSAL','CH20260101000001:REV',2,'PAY20260101000001',50000,'CNY');

-- 2) 反向分录：原借变贷、原贷变借
INSERT INTO journal_line(entry_no,line_no,account_no,direction,amount,currency)
SELECT 'REV20260101000001', line_no, account_no,
       IF(direction='D','C','D'), amount, currency
FROM journal_line WHERE entry_no='PAY20260101000001';

-- 3) 余额反向更新（带条件，避免把余额冲成负数）
UPDATE account_balance SET balance=balance+50000, version=version+1
 WHERE account_no='USER_1001';
UPDATE account_balance SET balance=balance-50000, version=version+1
 WHERE account_no='MERCHANT_2001' AND balance>=50000;
COMMIT;
```

**为什么不能 UPDATE 原分录**：

1. **审计**：审计要问「当时记的是什么、什么时候改的、谁改的」。UPDATE 把证据抹了。
2. **可重现**：任意时点的余额 = 该时点之前所有分录的净额。改了历史分录，历史余额就重算不出来。
3. **对账**：渠道文件里的流水是「发生过」的事实，内部账改成「应该发生」的样子，差异就永远对不上。
4. **幂等**：UPDATE 不是天然重复安全的；冲正有独立的 `entry_no` 与 `request_id:REV`，重复执行会被唯一键挡住。
5. **合规**：金融账本属于会计档案，保留期通常 5~10 年，删改是违规。

> 🩸 真实事故：某平台为了「省事」，发现重复分录后直接 `DELETE` 了 2,000 条。三个月后监管检查，渠道流水与内部账差了 380 万，既证明不了「删的是重复」，也证明不了「没多删」，最后按少记收入处理。

### 3.4.4 跨机构 / 跨渠道：你的唯一键管不到别人

跨机构幂等有三个残酷事实：**渠道会重复通知、会乱序、会丢通知**；你的数据库事务管不到渠道；渠道的单号和你自己的单号是两套身份。所以必须双轨记录：

```sql
CREATE TABLE channel_txn (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  channel        VARCHAR(32) NOT NULL,          -- ALIPAY / WECHAT / UNIONPAY
  channel_txn_id VARCHAR(64) NOT NULL,          -- 渠道流水号
  our_request_id VARCHAR(64) NOT NULL,          -- 我方请求号
  amount         BIGINT      NOT NULL,
  status         VARCHAR(16) NOT NULL,          -- SUCCESS / FAILED / UNKNOWN
  trade_time     DATETIME(3) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_channel_txn (channel, channel_txn_id),
  UNIQUE KEY uk_our_request (channel, our_request_id)
) ENGINE=InnoDB;

CREATE TABLE recon_file (
  id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  channel    VARCHAR(32) NOT NULL,
  biz_date   DATE        NOT NULL,
  file_hash  CHAR(64)    NOT NULL,              -- 文件内容 SHA-256
  status     VARCHAR(16) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_file (channel, biz_date, file_hash)  -- 同一文件重复下载不重复导入
) ENGINE=InnoDB;

CREATE TABLE recon_file_line (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  file_id        BIGINT      NOT NULL,
  line_no        INT         NOT NULL,
  channel_txn_id VARCHAR(64) NOT NULL,
  amount         BIGINT      NOT NULL,
  status         VARCHAR(16) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_file_line (file_id, line_no)   -- 断点续传/重复导入的幂等键
) ENGINE=InnoDB;
```

导入对账文件时，先算 `file_hash` 落 `recon_file`（唯一键挡重复文件），再逐行 `INSERT IGNORE INTO recon_file_line`（`uk_file_line` 挡重复行）。**不要用 `LOAD DATA` 直接灌生产表**——文件重传一次，账就多一遍。

三方核对的最小 SQL：

```sql
-- 渠道成功、我方无分录：短款/漏记
SELECT c.channel, c.channel_txn_id, c.amount
FROM channel_txn c
LEFT JOIN journal_entry j
  ON j.request_id = c.our_request_id AND j.biz_type = 'PAY'
WHERE c.status = 'SUCCESS' AND j.id IS NULL;

-- 我方有分录、渠道无流水：长款/多记
SELECT j.entry_no, j.request_id, j.amount
FROM journal_entry j
LEFT JOIN channel_txn c
  ON c.our_request_id = j.request_id AND c.channel = 'ALIPAY'
WHERE j.biz_type = 'PAY' AND c.id IS NULL;

-- 金额不一致
SELECT c.channel_txn_id, c.amount AS channel_amt, j.amount AS ledger_amt
FROM channel_txn c
JOIN journal_entry j ON j.request_id = c.our_request_id AND j.biz_type = 'PAY'
WHERE c.status = 'SUCCESS' AND c.amount <> j.amount;
```

> ⚠️ 跨渠道不要用「渠道单号」当全局唯一键：支付宝和微信的单号可能长得一样。唯一键必须带 `channel`。同理，回调验签必须用**渠道的密钥**，不能一套密钥打天下。

### 3.4.5 退款与撤销：同一笔支付可以退多次

退款是「一个支付对应 N 次退款」，所以幂等键不能是 `payment_id`，必须是 `(payment_id, refund_request_id)`。部分退款还要保证「累计退款 ≤ 实付」。

```sql
CREATE TABLE refund_order (
  refund_no    VARCHAR(40) NOT NULL,
  payment_id   VARCHAR(40) NOT NULL,
  request_id   VARCHAR(64) NOT NULL,          -- 客户端/渠道的退款请求号
  amount       BIGINT      NOT NULL,
  status       VARCHAR(16) NOT NULL,          -- PROCESSING / SUCCESS / FAILED
  created_at   DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (refund_no),
  UNIQUE KEY uk_payment_request (payment_id, request_id),  -- ★ 退款幂等键
  KEY idx_payment (payment_id)
) ENGINE=InnoDB;

CREATE TABLE payment (
  payment_id      VARCHAR(40) NOT NULL,
  paid_amount     BIGINT      NOT NULL,
  refunded_amount BIGINT      NOT NULL DEFAULT 0,
  status          VARCHAR(16) NOT NULL,
  version         BIGINT      NOT NULL DEFAULT 0,
  PRIMARY KEY (payment_id),
  CHECK (refunded_amount >= 0 AND refunded_amount <= paid_amount)
) ENGINE=InnoDB;
```

退款事务的顺序很关键：**先占退款单（唯一键），再条件更新累计退款额，最后记分录**。三步在同一个事务里：

```sql
START TRANSACTION;
INSERT INTO refund_order(refund_no,payment_id,request_id,amount,status)
VALUES ('RF20260101000001','PAY20260101000001','RFQ-001',20000,'PROCESSING');

UPDATE payment
SET refunded_amount = refunded_amount + 20000, version = version + 1
WHERE payment_id = 'PAY20260101000001'
  AND status = 'SUCCESS'
  AND refunded_amount + 20000 <= paid_amount;

-- 影响行数为 0：要么已退过（查 uk_payment_request），要么超额/状态不对
INSERT INTO journal_entry(entry_no,biz_type,request_id,amount,currency)
VALUES ('RF20260101000001','REFUND','RFQ-001',20000,'CNY');
INSERT INTO journal_line(entry_no,line_no,account_no,direction,amount,currency) VALUES
 ('RF20260101000001',1,'MERCHANT_2001','D',20000,'CNY'),
 ('RF20260101000001',2,'USER_1001','C',20000,'CNY');
UPDATE account_balance SET balance=balance-20000, version=version+1
 WHERE account_no='MERCHANT_2001' AND balance>=20000;
UPDATE account_balance SET balance=balance+20000, version=version+1
 WHERE account_no='USER_1001';
COMMIT;
```

**退款 vs 撤销**：撤销通常发生在「授权已冻结、尚未结算」，是取消授权，不产生资金分录（或只解冻）；退款发生在「已结算」，必须产生反向资金分录。两者都要幂等，但键不同：撤销用 `(payment_id, void_request_id)`，退款用 `(payment_id, refund_request_id)`。**别把撤销做成「退款 0 元」**——对账时会出现一堆金额为 0 的噪音分录。

## 3.5 双修对照：钱账与 token 账，结构完全同构

Agent 的 token 计费，本质就是一套金融账：调用前预扣、调用后结算、失败冲正。把「钱」换成「token」，表结构和事务边界可以照搬。

| 维度 | 资金账务 | Agent token 账务 | 相同 / 差异根因 |
| --- | --- | --- | --- |
| **账户** | 用户余额 / 商户待结算 / 手续费 | 租户可用 / 预扣 / 已用 | 同构：可用 + 冻结 + 已消耗 |
| **唯一键** | `payment_id` / `request_id` | `run_id` + `step_id` + `usage_id` | 结构同；Agent 多步骤粒度 |
| **分录** | 借 / 贷 | RESERVE / SETTLE / RELEASE | 同构：每笔都有正反两行 |
| **冲正** | 反向分录，不删原账 | 释放预扣，不删 RESERVE | **完全相同** |
| **对账** | 渠道文件 vs 分录 vs 余额 | 供应商账单 vs usage vs 配额 | 框架相同，数据源不同 |
| **失败模式** | 回调重试、重复退款 | 模型重试、流式中断、工具重放 | Agent 多「非确定性」 |
| **计量** | 金额确定 | 预扣按上限，结算按实际 | 差异根因：token 用量不可预知 |

**Agent 侧的三笔账**（每一笔都要幂等）：

```sql
CREATE TABLE token_ledger (
  usage_id   VARCHAR(64) NOT NULL,          -- run_id:step_id:attempt
  run_id     VARCHAR(64) NOT NULL,
  step_id    VARCHAR(64) NOT NULL,
  entry_type ENUM('RESERVE','SETTLE','RELEASE') NOT NULL,
  amount     BIGINT      NOT NULL,          -- token 数
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (usage_id, entry_type)        -- ★ 同一步骤的同一种账只能记一次
) ENGINE=InnoDB;

-- 预扣：按 max_tokens 冻结
INSERT INTO token_ledger(usage_id,run_id,step_id,entry_type,amount)
VALUES ('run-9:step-3:1','run-9','step-3','RESERVE',4096);
UPDATE token_account SET available=available-4096, reserved=reserved+4096,
       version=version+1 WHERE tenant_id='t-42' AND available>=4096;

-- 结算：按实际 usage 扣减，差额释放
INSERT INTO token_ledger(usage_id,run_id,step_id,entry_type,amount)
VALUES ('run-9:step-3:1','run-9','step-3','SETTLE',1732);
UPDATE token_account SET reserved=reserved-4096, spent=spent+1732,
       available=available+(4096-1732), version=version+1 WHERE tenant_id='t-42';

-- 失败冲正：释放全部预扣（不是删除 RESERVE）
INSERT INTO token_ledger(usage_id,run_id,step_id,entry_type,amount)
VALUES ('run-9:step-3:1','run-9','step-3','RELEASE',4096);
UPDATE token_account SET reserved=reserved-4096, available=available+4096,
       version=version+1 WHERE tenant_id='t-42';
```

**完全相同的部分**：复式记账的「有借必有贷、借贷必相等」、唯一约束、冲正而不是删除、对账框架。**差异根因**：钱是确定的，token 是预估的——所以 Agent 必须「先预扣上限，再按实际结算，差额释放」，不能像支付那样「一次记账」。另外，模型重试如果换了 `step_id`，账上就是两笔 RESERVE；所以**重试必须复用 `step_id`**，否则幂等键失效，用户被重复计费。

> 💡 一句话：**Agent 的 token 账不是「类似金融」，它就是金融账的一个实例**。会做预扣/结算/冲正的人，天然会做 Agent 计费；反过来也成立。

## 3.6 会在哪炸：六个账务专属坑

| 现象 | 根因 | 怎么查 | 怎么修 |
| --- | --- | --- | --- |
| 流水号重复 | 时钟回拨 / workerId 冲突 | `SELECT entry_no,COUNT(*) ... HAVING >1`；比对 NTP 偏移 | 回拨拒绝发号；workerId 配置中心化 |
| 流水号乱序 | 号段多实例、时钟回拨 | 按 `created_at` 排序看 `entry_no` 跳变 | 号段按 biz 隔离；不要跨 biz 比较 |
| 借贷不平 | 应用先写一行、第二行失败 | §3.7 的 `diff <> 0` 查询 | 分录与凭证同事务；应用层校验 |
| 冲正重复 | 冲正请求重试，`request_id:REV` 没唯一键 | `SELECT reversed_entry_no,COUNT(*)` | `uk(biz_type, request_id)` 覆盖 REVERSAL |
| 对账文件重复导入 | 文件重传、断点续传无唯一键 | `recon_file` 按 `file_hash` 查重 | `uk(channel,biz_date,file_hash)` + `uk(file_id,line_no)` |
| 退款超额 | 并发退款都读到旧 `refunded_amount` | `SUM(refund.amount) > payment.paid_amount` | 条件 `UPDATE ... WHERE refunded_amount+? <= paid_amount` |

## 3.7 怎么验证：三条 SQL 证明「只产生一条分录」

**第一条：同一业务请求只有一条凭证**（把 `request_id` 换成你要查的）：

```sql
SELECT biz_type, request_id, COUNT(*) AS entries, SUM(amount) AS total
FROM journal_entry
WHERE request_id = 'CH20260101000001'
GROUP BY biz_type, request_id
HAVING COUNT(*) > 1;
-- 期望：0 行。若返回 1 行，entries 就是重复条数。
```

**第二条：所有凭证借贷平衡、至少两行**：

```sql
SELECT entry_no,
       SUM(CASE WHEN direction='D' THEN amount ELSE -amount END) AS diff,
       COUNT(*) AS line_count
FROM journal_line
GROUP BY entry_no
HAVING diff <> 0 OR line_count < 2;
-- 期望：0 行。任何一行都是账不平或单边账。
```

**第三条：余额与分录可重算一致**（抽样某个账户）：

```sql
SELECT b.account_no, b.balance,
       COALESCE(SUM(CASE WHEN l.direction='D' THEN l.amount ELSE -l.amount END), 0) AS ledger_balance
FROM account_balance b
LEFT JOIN journal_line l ON l.account_no = b.account_no
WHERE b.account_no = 'USER_1001'
GROUP BY b.account_no, b.balance
HAVING b.balance <> ledger_balance;
-- 期望：0 行。注意借贷方向要与余额定义一致。
```

**并发验证**：200 个并发打同一个 `request_id`，然后跑第一条 SQL：

```bash
for i in $(seq 1 200); do
  curl -s -o /dev/null -X POST http://localhost:8080/api/pay/notify \
    -H 'Content-Type: application/json' \
    -d '{"requestId":"CH20260101000001","channelTxnId":"ALI-001","amount":50000}' &
done
wait
```

期望：`journal_entry` 1 条、`journal_line` 2 条、`account_balance` 只被加减一次、`idempotency_record` 1 条。**如果 entries=5，不要先删数据，先查唯一键是不是没建/没生效**。

## 3.8 用什么工具

- **发号**：美团 Leaf（号段 + 雪花双模式）、百度 uid-generator、Twitter Snowflake 原版；自研也可以，但 workerId 必须由配置中心分配。
- **账务**：MySQL 8.0 InnoDB（本章 DDL 可直接跑）；余额表加 `version` 做乐观锁；分录表按 `created_at` 月分区，分区删除代替 DELETE。
- **对账**：自研三方核对 + 差异表（第 14 章）；文件落对象存储，`file_hash` 做幂等。
- **不要指望**：Seata 的 AT 模式只保证「回滚」，不保证「业务只发生一次」；TCC 的幂等要业务自己写；Saga 的补偿也必须幂等（第 8、13 章）。

## 3.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 查了再插 | 查询与写入之间有窗口，主从延迟把它放大到几百毫秒 |
| 流水号 | 全局唯一、趋势递增、生成不依赖业务事务；回拨 > 1s 停发 |
| 分录 | 有借必有贷、借贷必相等；余额可由分录重算 |
| 唯一键 | `uk(biz_type, request_id)`，`biz_type` 不能省 |
| 冲正 | 反向记一笔，不 UPDATE、不 DELETE；冲正本身也要幂等 |
| 退款 | 键是 `(payment_id, refund_request_id)`，不是订单号 |
| 跨机构 | 渠道单号 + 我方请求号双轨；对账文件用 hash + 行号幂等 |
| 验证 | 一条 request_id 只出一条凭证；借贷平衡；余额可重算 |
| 双修 | token 预扣/结算/释放 = 金融预扣/结算/冲正，结构完全同构 |
| 终局 | 幂等的终局不是「挡住重复」，是「错了能冲正、差了对得平」 |

## 3.10 自检题

1. **为什么金融系统不能「先查后插」，即使查的是主库？**
   > 要点：主库也有并发窗口——两个事务同时 `SELECT` 都返回 0，然后都 `INSERT`；隔离级别若不是串行化，普通 `SELECT` 不加锁。唯一索引/`SELECT ... FOR UPDATE` 才能把「查」和「插」变成一次原子裁决。主从延迟只是把窗口从毫秒放大到几百毫秒，不是根因。

2. **发现一笔已入账的支付金额记错了，为什么不能直接 `UPDATE journal_entry`？**
   > 要点：破坏审计链与历史可重现性；对账无法解释差额；UPDATE 不天然幂等；合规上属于篡改会计档案。正确做法是插入反向冲正凭证，`entry_type=2`、`reversed_entry_no` 指向原凭证，再按正确金额重新入账。

3. **Agent 的 token 预扣为什么要「预扣上限 + 结算实际 + 差额释放」，而不是调用后直接记实际用量？**
   > 要点：模型调用前不知道实际 token，先记实际会导致并发调用透支配额；预扣上限保证不超卖。调用失败要释放预扣（冲正），不能删除 RESERVE；重试必须复用 `step_id`，否则会重复预扣。三笔账都要有唯一键，保证重复回调只记一次。

## 3.11 延伸阅读

- 上一章：[2. 幂等的实现光谱：8 种方案与选型](2-idempotency-implementations.md)
- 下一章：[4. 订单场景全套：下单、支付回调、退款、库存](4-idempotency-order-scenarios.md)
- 相关：[14. 对账体系](14-reconciliation-system.md)、[13. Saga 三连坑](13-saga-pitfalls.md)、[8. 分布式事务全景](8-distributed-transaction-overview.md)
- 平台侧：[../llm/47-cost-engineering.md](../llm/47-cost-engineering.md)（token 预算与计费）



