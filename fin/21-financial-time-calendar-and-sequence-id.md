# 21. 金融时间、交易日历与全局唯一流水号：顺序与身份的物理基座

> **状态**：生产级教程｜**所属**：第三部分·金融工程代码基座｜**编号**：21 / 48
>
> **本章定位**：本章承接 → [20 金额表示、定点算术与尾差分摊](20-monetary-precision-rounding-and-allocation.md)，把“发生在什么时候”“属于哪一个交易日/会计日”“如何唯一识别与排序”写成可执行的规则、DDL、协议和双栈代码。下游 → [22 复式记账核心账本引擎（上）](22-double-entry-ledger-schema-and-concurrency.md) 使用本章的会计日与分录号；→ [24 金融级全链路幂等体系](24-end-to-end-financial-idempotency.md) 使用本章的技术 ID 与幂等边界；→ [26 清结算系统架构](26-clearing-settlement-netting-and-cutoff.md) 使用交易日历、T+N 与 cutoff 计算交收批次。
>
> **唯一真问题**：同一笔金融事实跨地域、跨进程、跨夏令时、跨日切和跨故障重试后，能否仍被唯一识别、正确归属、按规定顺序处理，并由审计者使用原始输入复算？

---

## 21.1 引子：重复的 01:55，错误预留 USD 300,000

以下是一个可复算的教学事故模型。系统在 `America/New_York` 时区运行期权权利金批处理，代码用本地字符串拼批次键：

```text
batch_key = local_date + "-" + local_hhmm
```

2024-11-03 是美国夏令时结束日，凌晨 01:00–02:00 出现两次：

| UTC instant | 本地表示 | offset | 动作 |
|---|---|---:|---|
| 2024-11-03 05:55:00Z | 2024-11-03 01:55:00 | -04:00（EDT） | 批次 A |
| 2024-11-03 06:55:00Z | 2024-11-03 01:55:00 | -05:00（EST） | 批次 B |

两个 instant 相差 `3,600 s`，但字符串相同。恢复逻辑将唯一键冲突误判为“上一批未完成，允许重跑”，1,200 笔订单各重复预留 USD 250：

```text
错误预留 = 1,200 × 250 = USD 300,000
```

风控在 8 分钟后拦住出款，但客户可用保证金被错误降低，触发 37 个误强平告警。根因是把三个不同对象压成了一个字符串：

1. **instant**：事件在 UTC 时间线上的唯一位置；
2. **wall clock**：给人看的民用时间，可能重复、跳跃或不存在；
3. **business date**：按市场/账户规则计算出的日期，不是 timestamp 截断。

反向事故同样危险：2024-03-10 纽约没有 `02:30`。有的库把它推到 03:30，有的库报错，有的库使用旧 offset；不同节点会把同一批账分到不同会计日。

本章的纪律是：**跨系统事实用 UTC instant；本地时间只用于明确时区下的展示或规则输入；交易日和会计日显式落库；耗时和超时使用 monotonic clock；身份、顺序、可读流水号分开设计。**

---

## 21.2 时间正本：UTC、交易所时区、会计日与两种时钟

### 21.2.1 wall clock 与 monotonic clock

| 名称 | 含义 | 能否跨主机比较 | 能否回拨 | 用途 |
|---|---|---:|---:|---|
| **Wall clock** | UTC 或某时区的日历时间 | 只能在误差边界内近似比较 | 能；NTP、虚拟机恢复、人工校时均可能触发 | 事件时间、截止时间、审计、日历换算 |
| **Monotonic clock** | 本机持续时间计数器 | 不能 | 同一进程/主机生命周期内不回退 | RPC timeout、锁 TTL、退避、延迟指标 |
| **Business date** | 按时区、日历和 cutoff 算出的日期 | 它是标签，不是 instant | 不适用 | 交易日、会计日、T+N、日终批次 |

例如 API 在 `12:00:00Z` 发出、`12:00:03Z` 收到，应使用同一进程中两次采样的 monotonic 差判定耗时；不能用两台机器的 wall clock 相减得出精确的 3 秒。若 A 机器 NTP 偏差 `+4 ms`、B 机器偏差 `-3 ms`，真实先后的事件可能出现 7 ms 的 wall 时间反序。

**生产规则：**

1. 业务事实至少保存 `occurred_at_utc`、`received_at_utc`、`trading_date`、`accounting_date`、`tzdb_version`；禁止含义不明的 `created_at`。
2. 计时器同时记录 wall 起点（日志/审计）和 monotonic 起点（耗时计算）。monotonic 数值不能跨主机、跨重启传输。
3. cutoff 距离当前 wall 时间的误差超过规定阈值时，拒绝、挂起或人工处理；不能猜日期继续入账。
4. 业务顺序使用聚合内序列、撮合序列或日志 offset；timestamp 只能辅助观测。

### 21.2.2 UTC instant、offset、IANA zone 与 local time

- **UTC instant**：时间线上的点，例如 `2026-11-25T12:34:56.789Z`。
- **offset**：某个点相对 UTC 的偏移，例如 `-04:00`；它不能代表未来规则。
- **IANA time zone**：规则集合，例如 `America/New_York`，含历史变更和 DST。
- **local date/time**：把 instant 按 zone 展示出的民用值，可能重复或不存在。

纽约证券交易所常规时段是当地 `[09:30, 16:00)`。2026 年的开盘转换如下：

| 日期 | 纽约本地 | offset | UTC |
|---|---|---:|---|
| 2026-03-06 | 09:30 | -05:00 | 14:30 |
| 2026-03-09 | 09:30 | -04:00 | 13:30 |
| 2026-11-02 | 09:30 | -05:00 | 14:30 |

所以不能把“美股开盘 = 14:30 UTC”写死；3 月至 11 月通常是 13:30 UTC，其余时间通常是 14:30 UTC。正式实现必须让 IANA tzdb 和交易所日历共同计算，不能自己判断月份。

DST 回拨时，本地时间可能对应两个 instant：

```text
2026-11-01 01:30 -04:00 = 2026-11-01 05:30Z
2026-11-01 01:30 -05:00 = 2026-11-01 06:30Z
```

若接口只收到 `2026-11-01 01:30:00`，应返回 `AMBIGUOUS_LOCAL_TIME`；`2026-03-08 02:30:00 America/New_York` 应返回 `NONEXISTENT_LOCAL_TIME`。要求调用方传带 offset 的 RFC 3339 时间，服务端规范化保存 UTC。

### 21.2.3 交易日、会计日、自然日不能互换

| 日期 | 计算依据 | 示例 |
|---|---|---|
| 自然日 | UTC 或指定 zone 的午夜到午夜 | `2026-11-26` |
| 交易日 | 交易所开闭市、节假日、半日市、临时休市 | NYSE `2026-11-27` 半日市 |
| 会计日 | 账户主体的会计时区、日切 cutoff、监管口径 | `Asia/Shanghai` 17:00 日切 |

同一笔成交可能属于纽约交易日 `2026-11-27`，但已是 UTC 的 `2026-11-28`；客户账户按上海 17:00 日切也可能属于 `2026-11-28` 会计日。日期字段必须由服务计算并携带规则版本，下游不能再次猜测。

会计日是归属标签，不改写原始事实。原始 `occurred_at_utc` 永久不变；下一会计日发生的冲正，拥有新的 `accounting_date`，并通过 `reversal_of_entry_id` 关联原分录。

---

## 21.3 交易日历：把“能不能交易”建成数据

### 21.3.1 最小数据模型

日历真源可以是交易所公告、清算机构文件或受控供应商；服务缓存未来窗口，但已经用于入账的历史版本不得删除。最小字段包括：

1. `exchange`：市场/清算所，如 `XNYS`、`XNAS`、`XSHG`；
2. `trading_date`：市场本地业务日期；
3. `session`：盘前、常规、午间、盘后、清算等；
4. `open_at_utc`、`close_at_utc`：本地规则转换后的 instant；
5. `is_trading_day`、`status`：正常、休市、临时休市、待确认；
6. `cutoff`：下单、撤单、交收、估值等截止点；
7. `source_ref`、`version`、`published_at`：可审计来源。

发布前校验：session 不重叠、`open < close`、半日市 cutoff 不晚于 close、休市日无活动 session、UTC 转换后的相邻日期没有意外重叠。

### 21.3.2 PostgreSQL DDL

```sql
CREATE TABLE trading_calendar_day (
  exchange          VARCHAR(16) NOT NULL,
  trading_date      DATE NOT NULL,
  zone_name         VARCHAR(64) NOT NULL,
  is_trading_day    BOOLEAN NOT NULL,
  status            VARCHAR(16) NOT NULL CHECK (status IN ('OPEN','CLOSED','PROVISIONAL')),
  calendar_version  VARCHAR(32) NOT NULL,
  source_ref        VARCHAR(256) NOT NULL,
  published_at      TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (exchange, trading_date)
);

CREATE TABLE trading_session (
  exchange          VARCHAR(16) NOT NULL,
  trading_date      DATE NOT NULL,
  session_code      VARCHAR(16) NOT NULL,
  open_at_utc       TIMESTAMPTZ NOT NULL,
  close_at_utc      TIMESTAMPTZ NOT NULL,
  cutoff_at_utc     TIMESTAMPTZ,
  PRIMARY KEY (exchange, trading_date, session_code),
  FOREIGN KEY (exchange, trading_date)
    REFERENCES trading_calendar_day(exchange, trading_date),
  CHECK (open_at_utc < close_at_utc),
  CHECK (cutoff_at_utc IS NULL OR cutoff_at_utc >= open_at_utc)
);

CREATE TABLE accounting_cutoff (
  account_zone      VARCHAR(64) NOT NULL,
  cutoff_code       VARCHAR(32) NOT NULL,
  effective_from    DATE NOT NULL,
  effective_to      DATE,
  cutoff_local      TIME NOT NULL,
  tzdb_version      VARCHAR(32) NOT NULL,
  PRIMARY KEY (account_zone, cutoff_code, effective_from),
  CHECK (effective_to IS NULL OR effective_to > effective_from)
);
```

`TIMESTAMPTZ` 保存 instant，不保存输入的时区名称；因此 `zone_name` 和 `calendar_version` 必须另存。禁止用 `timestamp without time zone` 接收跨服务金融事件。

### 21.3.3 查询协议

```text
GET /v1/calendars/{exchange}/days/{trading_date}

{
  "exchange": "XNYS",
  "trading_date": "2026-11-27",
  "zone": "America/New_York",
  "is_trading_day": true,
  "calendar_version": "nyse-2026.11.01",
  "sessions": [{
    "code": "REGULAR",
    "open_at": "2026-11-27T14:30:00Z",
    "close_at": "2026-11-27T18:00:00Z",
    "cutoff_at": "2026-11-27T17:59:59.999Z"
  }]
}
```

2026-11-27 是感恩节后的半日市；本地 13:00、offset `-05:00` 转为 `18:00Z`。时间字段必须带 `Z` 或明确 offset，ID 在 JSON 中使用字符串，避免 JavaScript `Number` 超过 `2^53-1` 后失去精度。

### 21.3.4 T+N：数交易日，不加 24 小时

定义 `settlement_date(E,T,N)`：先确定有效成交日 `T`，再从 `T` 之后跳过休市日，计数 `N` 个 `is_trading_day=true` 的日期；`N=0` 时返回 `T`。若产品规定 cutoff 后顺延，必须在 T+N 之前明确求出新的 `T`。

采用 XNYS 日历的可核算示例：

| 成交日 | N | 跳过/计数 | 交收日 |
|---|---:|---|---|
| 2026-11-25 周三 | 1 | 11-26 感恩节休市，11-27 为第 1 个 | 2026-11-27 周五 |
| 2026-11-27 周五 | 2 | 11-28/29 周末，11-30 第 1 个，12-01 第 2 个 | 2026-12-01 周二 |

若交易所发布临时休市，日历版本变化应生成可审计的重算任务；已完成的交收不能静默改写。

### 21.3.5 cutoff 使用半开区间

推荐把业务窗口写成 `[start, end)`：

```text
received_at < cutoff_end  → 当前批次
received_at >= cutoff_end → 下一批次或 CUTOFF_EXPIRED
```

例如上海本地 17:00 日切：

- `2026-11-25T08:59:59.999Z` = `16:59:59.999+08:00`，属于 11 月 25 日；
- `2026-11-25T09:00:00Z` = `17:00:00+08:00`，不属于 `[... ,17:00:00)`，按协议进入下一批或拒绝。

不要用 `23:59:59.999` 伪造“当天结束”：数据库精度、截断和舍入会制造缝隙。批次判定使用服务端可信的 wall instant；RPC 是否超时使用 monotonic duration，并分别记录 `accepted_at_utc` 和 `handler_elapsed_ns`。

---

## 21.4 全局唯一流水号：身份、顺序、可读号分离

### 21.4.1 一个数字不能满足三个需求

| 需求 | 推荐机制 | 不能做的事 |
|---|---|---|
| 全局不重复 | Snowflake、数据库号段、UUID/ULID | 用 wall timestamp 拼接 |
| 同一订单/账户严格顺序 | 聚合内 `aggregate_seq`、单写者、日志 offset | 用全局 ID 大小表示业务先后 |
| 人工可读参考号 | 独立格式化编号/审计号 | 把客户、金额、分片信息编码进技术主键 |

Snowflake 的数值大小不等于业务顺序：跨节点时钟偏差、异步消息和重试都可能让后发生的动作拥有更小 ID。订单状态转移应使用 `version` 或聚合内序列，例如 `UPDATE ... WHERE version = expected`，而不是 `WHERE id > last_id`。

若监管要求交易日内连续编号，另建“审计参考号”服务；它与技术主键不是同一字段。预分配号段后宕机必然有空洞，不能一边预分配一边承诺无洞。

### 21.4.2 Snowflake 位布局和可核算样例

本章采用 63 位正整数布局，最高位保持 0：

```text
0 | 41 bits timestamp_ms | 10 bits worker_id | 12 bits sequence
```

epoch=`2025-01-01T00:00:00Z`：

- 每毫秒每 worker `2^12 = 4,096` 个 ID；
- 1024 个 worker 理论峰值 `4,096 × 1,024 = 4,194,304 ID/s`；
- `2^41 ms = 2,199,023,255,552 ms`，从该 epoch 起到 `2094-09-07T15:47:35.552Z`；
- 生产上应在 2090 年前完成 epoch/位宽迁移演练。

`2026-11-25T12:34:56.789Z` 相对 epoch 是 `59,920,496,789 ms`。`worker=17`、`sequence=23` 时：

```text
timestamp_part = 59,920,496,789 << 22 = 251,324,779,364,089,856
worker_part    = 17 << 12            =                 69,632
id             = timestamp_part + worker_part + 23
                = 251,324,779,364,159,511
```

该值小于 `2^63-1`，可放 PostgreSQL `BIGINT`；JSON 中仍必须写成字符串：`"251324779364159511"`。

### 21.4.3 worker 租约和 fencing

`worker_id` 不是 hostname，而是带租期的注册资源。注册中心至少保存 `worker_id`、递增 `fencing_token`、实例 ID、`lease_until`、最后心跳和状态。旧进程经历长时间 GC pause 后恢复，不能凭旧 token 继续发号；下游事件头或租约代理应拒绝旧 fencing token。

回拨策略：

1. 回拨不超过 5 ms：逻辑时间保持 `last_ms`，继续消费 sequence；
2. 当前毫秒 sequence 用尽：用 monotonic timer 等待 wall clock 进入下一毫秒；
3. 回拨超过阈值：停止发号、告警、摘流，返回可识别错误；
4. 租约失效或 fencing token 过期：fail-closed，重新领租约后再发号。

等待能解决单 worker 的小回拨，不能解决两个进程共享 worker ID；后者必须由租约和 fencing 解决。

### 21.4.4 Go 1.24 发号骨架

`time.Now().UnixMilli()` 是 wall 时间，仅用于逻辑毫秒；`time.Since` 使用 Go `Time` 内含的 monotonic 成分，适合测等待和耗时。生产实现还需接租约监控、摘流和指标。

```go
type Snowflake struct {
    mu sync.Mutex
    epoch, lastMS int64
    worker uint64 // 0..1023，由带 fencing 的租约分配
    seq uint64    // 0..4095
}

func (s *Snowflake) Next() (int64, error) {
    s.mu.Lock(); defer s.mu.Unlock()
    now := time.Now().UnixMilli() - s.epoch // wall clock
    if now < s.lastMS {
        if s.lastMS-now > 5 { return 0, ErrClockRollback }
        now = s.lastMS
    }
    if now == s.lastMS {
        s.seq = (s.seq + 1) & 4095
        if s.seq == 0 {
            start := time.Now()
            for now <= s.lastMS {
                runtime.Gosched()
                now = time.Now().UnixMilli() - s.epoch
            }
            metrics.ObserveWait(time.Since(start)) // monotonic elapsed
        }
    } else { s.seq = 0 }
    s.lastMS = now
    return (now << 22) | (int64(s.worker) << 12) | int64(s.seq), nil
}
```

代码是核心骨架，不是完整服务：必须限制自旋时间、暴露回拨和 sequence 溢出指标，并在租约失效时停止。错误不能被上层改写成“换一个幂等键重试”，否则会把一个业务请求发成两个身份。

### 21.4.5 Java 21 发号骨架

Java 的 `currentTimeMillis()` 是 wall clock；`nanoTime()` 只用于同一 JVM 内计算 elapsed，不能写入数据库，也不能跨 JVM 比较。

```java
final class Snowflake {
  private final long epochMs, workerId;
  private long lastMs = -1, seq = 0;
  Snowflake(long epochMs, long workerId) {
    if (workerId < 0 || workerId > 1023) throw new IllegalArgumentException();
    this.epochMs = epochMs; this.workerId = workerId;
  }
  synchronized long next() {
    long now = System.currentTimeMillis() - epochMs;
    if (now < lastMs && lastMs - now > 5) throw new ClockRollbackException();
    if (now < lastMs) now = lastMs;
    if (now == lastMs && (++seq & 4095) == 0) {
      long start = System.nanoTime();
      do { LockSupport.parkNanos(100_000);
        now = System.currentTimeMillis() - epochMs;
      } while (now <= lastMs);
      metrics.recordNanos(System.nanoTime() - start); // monotonic
      seq = 0;
    } else if (now != lastMs) seq = 0;
    lastMs = now;
    return (now << 22) | (workerId << 12) | seq;
  }
}
```

业务代码应注入可控的 `Clock` 测试 wall 时间；timeout 测试应抽象 monotonic source，禁止用不可控的 `Thread.sleep` 作为正确性证明。

### 21.4.6 数据库号段与双 Buffer

数据库号段适合需要可排序、可按命名空间分开的参考号，但不保证无洞。领取号段必须在事务内锁行并递增；服务在当前段低水位时预取下一段。双 Buffer 只能降低申请延迟，不能回收已领取而未消费的号码。

```sql
CREATE TABLE id_segment (
  namespace    VARCHAR(64) PRIMARY KEY,
  next_value   BIGINT NOT NULL CHECK (next_value > 0),
  block_size   INT NOT NULL CHECK (block_size BETWEEN 1 AND 1000000),
  version      BIGINT NOT NULL DEFAULT 0,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

UPDATE id_segment
SET next_value = next_value + block_size,
    version = version + 1,
    updated_at = now()
WHERE namespace = $1
RETURNING next_value - block_size AS segment_start,
          next_value - 1 AS segment_end, version;
```

容量例：每段 10,000 个号、峰值 20,000 id/s，则单实例每秒申请 `ceil(20,000/10,000)=2` 段；100 个均匀实例约为 `200 update/s`。若号段行安全能力为 500 update/s，尚有 2.5 倍余量，但仍要压测锁等待、实例重启和预取风暴。

---

## 21.5 事实表、协议和聚合内顺序

### 21.5.1 事件事实 DDL

```sql
CREATE TABLE financial_event (
  event_id          BIGINT NOT NULL,
  aggregate_type    VARCHAR(32) NOT NULL,
  aggregate_id      VARCHAR(64) NOT NULL,
  aggregate_seq     BIGINT NOT NULL CHECK (aggregate_seq > 0),
  occurred_at_utc   TIMESTAMPTZ NOT NULL,
  received_at_utc   TIMESTAMPTZ NOT NULL,
  trading_exchange  VARCHAR(16),
  trading_date      DATE,
  accounting_zone   VARCHAR(64) NOT NULL,
  accounting_date   DATE NOT NULL,
  calendar_version  VARCHAR(32),
  tzdb_version      VARCHAR(32) NOT NULL,
  event_type        VARCHAR(64) NOT NULL,
  payload           JSONB NOT NULL,
  created_at_utc    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (event_id),
  UNIQUE (aggregate_type, aggregate_id, aggregate_seq)
);

CREATE INDEX idx_event_accounting
  ON financial_event (accounting_date, event_type, event_id);
CREATE INDEX idx_event_trade
  ON financial_event (trading_exchange, trading_date, event_id);
```

`received_at_utc` 不等于 `occurred_at_utc`；离线重传、消息积压和跨时区都会造成差异。日期字段和规则版本入账后不可 update；若日历或时区规则错误，新增修正事件并保留原始值。

### 21.5.2 时间与 ID 的统一事件信封

```json
{
  "event_id": "251324779364159511",
  "aggregate_id": "ord_01JXYZ",
  "aggregate_seq": 7,
  "event_type": "OrderAccepted",
  "event_version": 1,
  "occurred_at": "2026-11-25T12:34:56.789Z",
  "received_at": "2026-11-25T12:34:56.801Z",
  "trading_exchange": "XNYS",
  "trading_date": "2026-11-25",
  "accounting_date": "2026-11-25",
  "calendar_version": "nyse-2026.11.01",
  "tzdb_version": "2026a",
  "producer": "oms-3",
  "trace_id": "01JXYZ...",
  "payload": {"symbol":"ACME","qty":100}
}
```

协议契约：时间必须是 RFC 3339 且带 `Z`/offset；`event_id` 和大整数为字符串；`aggregate_seq` 在一个聚合内从 1 单调递增；同一 `(aggregate_type, aggregate_id, aggregate_seq)` 不允许两个不同事件。消费者可乱序收到消息，但必须按 `aggregate_seq` 缓冲、丢弃已处理序号或进入 gap repair，不能按 `occurred_at` 猜顺序。

### 21.5.3 Go / Java 计算 T+N 的边界

Go 的 `time.AddDate(0,0,n)` 不能用于交易日；Java 的 `plusDays(n)` 也不能用于交易日。两者都应依赖日历接口/版本，并在应用层计数开放日。

```go
func Settlement(cal Calendar, trade time.Time, n int) (time.Time, error) {
    d := cal.TradingDate(trade) // 先按交易所 zone 归属
    for left := n; left > 0; {
        d = d.AddDate(0, 0, 1)
        if cal.IsOpen(d) { left-- }
    }
    return d, nil
}
```

```java
LocalDate settlement(Calendar cal, LocalDate tradeDate, int n) {
  LocalDate d = cal.normalizeTradeDate(tradeDate);
  for (int left = n; left > 0;) {
    d = d.plusDays(1);
    if (cal.isOpen(d)) left--;
  }
  return d;
}
```

`normalizeTradeDate` 是否把休市日成交归到下一开放日，必须由产品契约决定；本示例不允许悄悄吞掉非法成交日。

---

## 21.6 极端边界与故障处理清单

### 21.6.1 DST、闰秒、tzdb 更新

1. DST 前进：不存在的 local time 必须拒绝或由明确定义的“推迟策略”处理，不能依赖库默认行为。
2. DST 回拨：重复的 local time 必须带 offset 或 fold 标记；无 offset 直接返回 `AMBIGUOUS_LOCAL_TIME`。
3. tzdb 升级：记录 `tzdb_version`；升级前回放未来 12 个月的 cutoff/session，比较变化并人工批准。
4. 闰秒：金融系统通常使用 POSIX/UTC smear 约定，不把 `23:59:60` 自己解析成普通秒；若供应商提供 smear，必须写入时间源协议。
5. 服务器 wall clock 大幅漂移：超过 `clock_error_budget` 时拒绝新批次、保留查询能力并告警；不能只依赖 NTP“最终会修好”。

### 21.6.2 ID 与顺序边界

1. sequence 在同一毫秒耗尽：等待下一个 wall 毫秒，等待时长用 monotonic 统计。
2. wall clock 回拨：小回拨逻辑时间不后退，大回拨 fail-closed；禁止把负差直接转成无符号整数。
3. worker 重启：先撤销旧 lease，再以新 fencing token 领 worker；不能由 hostname 复用旧 ID。
4. 多活发号：每地域有独立 worker 空间；若 worker registry 分区，未确认 lease 的地域停止发号，不能两边都自认为 owner。
5. 数据库恢复到旧快照：号段表必须与 WAL/备份一起恢复；恢复后用 fencing epoch 拒绝旧应用继续写，不能依赖 `next_value` 自己变大。
6. 消息乱序/重复：按 `aggregate_seq` 和唯一键处理；技术 ID 唯一不等于业务操作幂等。

### 21.6.3 cutoff、迟到和跨日

1. 请求在 cutoff 前发出、cutoff 后到达：以契约指定的 `received_at`、网关接收时间或交易所确认时间判定，不能在服务间各自选择。
2. cutoff 边界精确命中：统一半开区间，`== cutoff_end` 属于下一批次。
3. 外部文件迟到：原始收到时间不变，业务日按文件协议/原始交易日确定，迟到计数单独监控。
4. 日历临时变更：未结算记录冻结并生成重算版本；已出账记录通过调整/冲正，不直接 update。
5. 会计日与交易日不同：每笔事件同时存两者；报表必须声明使用哪个字段。

---

## 21.7 测试、对账与压测：证明不是“看起来正确”

### 21.7.1 单元与性质测试

必须固定 tzdb 版本并将边界样例纳入 CI：

| 类别 | 最少断言 |
|---|---|
| UTC/zone | `instant -> zone -> instant` 往返不变；offset 正确 |
| DST | 纽约 2026-03-08 的 02:30 拒绝；2026-11-01 的 01:30 无 offset 拒绝；两个 offset 得两个 instant |
| cutoff | `end-1ns` 当前批、`end` 下一批；毫秒截断不改变契约 |
| T+N | 周末、节假日、半日市、临时休市和 N=0；结果可由日历版本重算 |
| Snowflake | 并发生成无重复；同 worker ID 不重复；小回拨不倒退；大回拨 fail-closed |
| 号段 | 并发领取区间不重叠；重启允许空洞但不允许复用；版本递增 |
| 聚合顺序 | 重复 seq 幂等；gap 进入补偿；乱序不会覆盖新状态 |

性质测试的核心断言不是“ID 一定连续”，而是：`unique(all_ids)`、同 namespace 号段两两不交、同 aggregate 的 seq 不重复、相同输入日历版本的 T+N 结果确定。

### 21.7.2 混沌测试矩阵

| 注入 | 观察项 | 通过条件 |
|---|---|---|
| NTP 前后跳 3 ms | 发号等待、重复、错误率 | 无重复；逻辑时间不后退 |
| NTP 回拨 100 ms | 服务状态 | fail-closed，报警在 1 分钟内触发 |
| 进程暂停 10 s | worker lease | 旧 fencing token 的写入全被拒绝 |
| 注册中心分区 30 s | 多活发号 | 未确认 owner 的区域不发号 |
| 日历版本临时休市 | 交收状态 | 未完成批次冻结，生成重算任务 |
| Kafka 延迟 5 min/乱序 | 聚合消费 | 只按 seq 应用，无覆盖和重复分录 |
| tzdb 版本切换 | cutoff 回放 | 变化有 diff、审批和版本号 |

### 21.7.3 压测设计和可复算容量

Snowflake 单 worker 的理论上限是 `4,096 ID/ms = 4,096,000 ID/s`，但锁、CPU 和指标开销会降低实际值；按安全利用率 60% 估算，若目标峰值 `1,000,000 ID/s`，至少需要：

```text
worker 数 = ceil(1,000,000 / (4,096,000 × 0.60)) = 1
```

数学上 1 个足够，但生产至少配置 4 个 worker（每个按 25% 目标压测）以覆盖故障和租约迁移；最终以同一机器规格的 P99、锁竞争、GC pause 实测为准。若每个 worker 实际安全值只有 `500,000 ID/s`，则：

```text
worker 数 = ceil(1,000,000 / 500,000) = 2
容灾后单 worker 承担 = 1,000,000 / 1 = 1,000,000 ID/s → 不合格
```

所以按 N+1 设计至少 3 个，并让单 worker 的实测安全值覆盖故障后目标；这比只看位宽容量可靠。

压测要同时测：并发发号吞吐、P50/P99/P999 延迟、sequence 溢出次数、clock rollback 次数、租约续期延迟、号段行锁等待、数据库 WAL 增长和跨地域网络分区。不能只压健康状态的“每秒多少号”。

### 21.7.4 对账锚点

日终/日内对账至少输出：

```text
事件总数 = 按 event_id 去重后的事实数
聚合末 seq = 每个 aggregate 的最大已应用 seq
日期分布 = trading_date / accounting_date / UTC date 三套计数
ID 范围 = min/max + worker/sequence 分桶 + 重复数
交收数 = 每个 calendar_version、T、N 的结果计数
```

发现日期或 ID 异常时，优先从原始 `occurred_at_utc`、版本、source_ref、fencing token 复算；不要直接修报表。技术 ID 只证明身份，不能证明资金已入账；资金对账仍以 → [22] 的不可变分录和 → [27 多层对账引擎](27-multi-tier-reconciliation-and-auto-healing.md) 为准。

---

## 21.8 与下文的衔接：时间和 ID 只是账本的输入，不是账本

本章交付的接口边界如下：

| 本章产物 | → 22 复式账本 | → 24 幂等 | → 26 清结算 |
|---|---|---|---|
| `occurred_at_utc` / `received_at_utc` | 原始事实与入账事实分离 | 重试不改原时间 | 迟到和批次审计 |
| `accounting_date` + `tzdb_version` | 分录归属日 | 同一请求不跨日漂移 | 日切与报表口径 |
| `trading_date` + `calendar_version` | 成交日字段 | 交收任务唯一键 | T+N、临时休市重算 |
| `event_id` | 分录/凭证关联号 | 幂等去重键的组成 | 事件对账锚点 |
| `aggregate_seq` | 同一账户/订单的应用顺序 | 重放和 gap repair | 结算状态机顺序 |
| Snowflake/号段 ID | 技术主键/参考号 | 客户端重试复用原键 | 事件追踪与审计 |

下篇必须继续遵守三条边界：

1. **不可变事实优先**：错误日期、错误顺序、错误金额都用新事件/冲正表达，不 update 已入账事实。
2. **时间不等于顺序**：账本的余额并发控制用账户字典序加锁和版本/分录序列；不能用 Snowflake 大小替代锁。
3. **未知态不靠时间猜**：跨网络扣款超时是 `UNKNOWN`，即使 timeout 已过也不能发新流水重扣；要用 → [28 支付核心](28-payment-gateway-routing-and-unknown-state.md) 的查单和异步回执收敛。

---

## 21.9 术语表

| 术语 | 正本定义 |
|---|---|
| instant | UTC 时间线上的唯一时点；跨系统事实的主时间 |
| wall clock | 可被校时、回拨或跳跃的民用时间 |
| monotonic clock | 只用于同一计时域的 elapsed，不表达日期，也不能跨主机比较 |
| offset | 某 instant 相对 UTC 的偏移，如 `+08:00` |
| IANA time zone | 含历史和 DST 规则的时区名，如 `Asia/Shanghai` |
| tzdb | 时区规则数据库；版本必须进入审计上下文 |
| trading date | 按交易所日历归属的交易日期 |
| accounting date | 按账户会计时区和 cutoff 归属的会计日期 |
| session | 市场的一个可交易/清算时段 |
| cutoff | 半开时间窗口的结束边界；命中 end 通常进入下一批 |
| T+N | 从有效成交日之后数 N 个开放交易日的交收日 |
| Snowflake | 时间片、worker、sequence 组成的分布式技术 ID |
| clock rollback | wall clock 小于上一次逻辑时间的回拨 |
| worker lease | 分配 worker 身份的租约；必须配 fencing token |
| sequence | 同一时间片或同一聚合内的序号；含义由上下文限定 |
| fencing token | 递增的 owner 代次，用来拒绝旧进程继续写 |
| 号段 | 一次领取、进程内消费的连续 ID 区间；宕机会留下空洞 |
| calendar version | 交易日历规则的不可变版本，支持历史重算 |
| ambiguous local time | DST 回拨造成一个本地时间对应两个 instant |
| nonexistent local time | DST 前进造成一个本地时间不存在 |

---

## 21.10 一页速记与自检题

**一页速记：**

1. 跨系统保存 UTC instant；展示和规则计算使用带名字的 IANA zone。
2. wall clock 讲“几点”，monotonic clock 讲“过了多久”；两者不能互换。
3. 交易日、会计日、自然日是三个字段；它们不能由下游自行推断。
4. T+N 数开放交易日，不是 `+24h`；半日市、临时休市都由版本化日历决定。
5. cutoff 用 `[start,end)`；`end` 归下一批；服务端选择并记录时间源。
6. Snowflake 保证技术 ID 唯一，不保证业务顺序；顺序由聚合 seq/单写者/日志 offset 保证。
7. Snowflake 回拨小则逻辑时间不退，大则 fail-closed；worker 必须有 lease + fencing。
8. 号段可牺牲连续性换吞吐；不回收已领号段，避免复用。
9. 时间、规则版本、ID 和 seq 全进协议与 DDL；异常靠回放和对账修复，不靠改历史。

**自检题：**

1. 某订单在 `2026-03-08 02:30 America/New_York` 到达，为什么不能直接转 UTC？若业务要求“推到下一个有效分钟”，这个策略应在哪一层明确？
2. XNYS 成交日为 2026-11-25，采用 T+2；11-26 休市、11-27 开市、11-28/29 周末、11-30 开市、12-01 开市。交收日是哪天？若 11-25 16:01 本地提交且 cutoff 为 16:00，先改变哪个输入？
3. Snowflake 在同一毫秒生成 4,096 个 ID 后又来第 4,097 个，应该返回什么？若 wall clock 回拨 100 ms，为什么不能把回拨值直接编码？
4. 一个事件 `occurred_at=2026-11-25T23:30Z`、账户时区 `Asia/Shanghai`、17:00 日切，交易所 `America/New_York`。请分别判断 UTC 自然日、上海会计日和纽约交易日，说明还缺哪些日历输入才能最终确定交易日。
5. 两台服务各自生成 ID，A 的 ID 比 B 小，但 B 的业务动作先发生，是否违反 Snowflake 契约？若订单状态不能倒退，应增加什么字段和约束？

**参考答案：**

1. 02:30 是 DST 前进日不存在的本地时间，缺少唯一 instant；“推到下一个有效分钟”是产品/日历策略，应在时间解析和业务接入契约层明确并记录策略版本，不能让库默认决定。
2. T+2 是 2026-12-01；若 cutoff 后顺延，先把有效交易日 `T` 从 11-25 改为下一开放日（需按产品契约确认），再计算 T+2，不能直接给日期加 2 天。
3. 等待下一个 wall 毫秒后把 sequence 置 0；100 ms 回拨可能与已经发出的未来片段冲突，必须 fail-closed/摘流，不能使用无符号回绕。
4. UTC 自然日是 11-25；上海是 `11-26 07:30`，且已过 11-25 17:00，所以会计日为 11-26；纽约本地仍是 11-25 18:30，但是否交易日、session 是否开放还需要 XNYS 日历和具体 session/cutoff。
5. 不违反；Snowflake 只保证唯一和近似时间编码，不保证跨节点业务顺序。应使用订单聚合内 `version`/`aggregate_seq`，以条件更新或单写者拒绝旧版本。

---

## 21.11 面试系统设计 10 分钟答题卡

**第 1 分钟：定义边界。** 先问市场时区、会计时区、交易日历来源、cutoff 采用 received/occurred/交易所确认哪一种，以及是否要求连续流水号。

**第 2–3 分钟：画时间模型。** 画 `instant → IANA zone → local time/date`，指出 DST ambiguous/nonexistent；明确 wall clock 负责事实，monotonic 负责耗时。

**第 4 分钟：画日历和 T+N。** 日历含 holiday、session、half-day、temporary close、version；T+N 先定有效 T，再数开放日。

**第 5–6 分钟：选 ID。** 技术主键用 Snowflake/UUID；同聚合顺序用 seq/version；监管连续号另用号段或逐笔分配。说明 41/10/12 位容量、epoch 到期日和 worker lease。

**第 7 分钟：讲回拨与脑裂。** 小回拨逻辑时间不退，大回拨 fail-closed；租约+fencing 防双 owner；消息乱序按 aggregate seq，不按 ID 大小。

**第 8 分钟：给 DDL/协议。** `TIMESTAMPTZ`、`DATE`、`tzdb_version`、`calendar_version`、`event_id`、`aggregate_seq`；RFC3339 带 offset；大整数 JSON 字符串。

**第 9 分钟：讲测试和容量。** DST、边界、临时休市、回拨、暂停、分区、号段空洞；给出 `4,096/ms/worker` 和 N+1 后实测安全容量。

**第 10 分钟：收口。** 时间是事实与归属，ID 是身份，seq 才是局部顺序；任何未知态不靠生成新 ID 重试；下篇把这些输入接到不可变复式账本。
