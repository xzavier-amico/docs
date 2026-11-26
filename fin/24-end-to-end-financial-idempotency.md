# 24. 金融级全链路幂等体系：从客户端到渠道网关的零重入防线

> **状态**：生产级工程篇｜**所属**：第三部分·金融工程代码基座｜**编号**：24 / 48
>
> **本章定位**：承接 [22 复式记账核心账本引擎（上）](22-double-entry-ledger-schema-and-concurrency.md) 的不可变分录与业务事实边界，以及 [23 复式记账核心账本引擎（下）](23-hot-accounts-reversals-and-fx-ledger.md) 的分桶、冲正和对账思想，把“重试不会重复扣款”扩展成客户端、API、订单服务、账本、Outbox、渠道和回执查询之间的一条完整证据链。
>
> **唯一真问题**：当任意一跳在“副作用已经发生”和“结果已经被调用方知道”之间崩溃、超时或断网时，系统能否在不创造第二个经济事实的前提下，凭同一个业务身份重新取得唯一结果，并最终让账本、渠道和对账状态收敛？

---

## 24.1 事故引子：一笔支付，收了两次，三套系统都说自己没错

### 24.1.1 时间线

某商户收款接口在大促期间发生一次典型事故。用户只点击了一次“支付”，最终银行卡扣款两次，平台账本入账两笔，商户却只看到一笔订单。

| 时间 | 事件 | 系统行为 | 事故后果 |
|---|---|---|---|
| 12:00:00.000 | 客户发送 `POST /payments` | 客户端生成 `request_id=A` | 正常开始 |
| 12:00:00.080 | API 生成内部订单 `P1` | 订单服务把 `P1` 写成 `PAYING` | 订单身份与客户端身份没有绑定 |
| 12:00:00.420 | 渠道已扣款 | 渠道响应在代理连接池中丢失 | 平台看不到结果 |
| 12:00:01.000 | API 超时 | 网关把超时翻译成 `500` | 客户端误以为可以新建请求 |
| 12:00:01.300 | 客户端重试 | 重试生成 `request_id=B` | 服务端创建 `P2` |
| 12:00:01.700 | 渠道二次请求 | 另一个连接、另一个 `merchant_order_no` | 银行再次扣款 |
| 12:00:02.000 | 两个回执抵达不同消费者 | 两个消费者各自写账 | 账本产生两个入账事实 |
| 次日对账 | 渠道只按商户订单号导出部分字段 | 平台无法证明哪笔是原请求 | 人工退款、商户投诉、审计缺证 |

技术团队最初提出了三个修复：加一张 Redis 去重表、把客户端超时改成长轮询、在渠道调用前加分布式锁。这三个办法都没有触及根因：

1. Redis 的 TTL 过期后，迟到的第一次请求可以穿透；Redis 成功但数据库提交失败时，去重记录与经济事实分离。
2. 长轮询只能提高拿到响应的概率，不能把“渠道已经扣款但响应丢失”变成已知结果。
3. 分布式锁只保护一段执行时间，锁过期、进程暂停、网络分区后，旧持有者仍可能继续调用渠道；没有栅栏令牌和下游幂等，它不是资金安全边界。

### 24.1.2 如果只看状态，会得到错误结论

事故后的数据库可能同时出现：

```text
订单 P1 = PAYING
订单 P2 = SUCCESS
渠道流水 C1 = SUCCESS（异步对账才发现）
账本事实 L1 = POSTED
账本事实 L2 = POSTED
API 日志 = 两次 500
```

“API 返回失败”不代表资金操作失败；“当前订单是成功”也不代表没有另一个成功事实。金融系统不能用最后一次状态覆盖历史事实，必须保存每一层的身份、请求指纹、外部引用、原始响应和状态转移证据。

### 24.1.3 事故红线

> **同一个业务意图只能有一个经济事实；重试只能复用原身份；参数改变必须拒绝；超时必须进入 UNKNOWN；外部副作用必须在事务外通过可重放的状态机驱动；最终成功必须以账本和渠道对账共同证明。**

这里的“零重入”不是保证网络世界真的只发出一个 TCP 包，而是保证同一经济事实不会被第二次接受、记账或结算。渠道若不提供幂等能力，平台不能凭空制造 exactly-once，只能把风险显式隔离为 UNKNOWN，并停止自动二次扣款。

---

## 24.2 唯一真问题：身份、参数、结果和副作用必须分别可证明

### 24.2.1 幂等不是“接口调用两次返回一样”

一个金融操作至少有四种身份：

| 身份 | 例子 | 作用 | 是否可被重试改变 |
|---|---|---|---:|
| **业务事实身份** `operation_id` | `pay_01J...` | 平台内部唯一经济意图 | 否 |
| **调用幂等键** `idempotency_key` | `merchant-42:checkout-991:v1` | 客户端到 API 的重试关联 | 否 |
| **请求指纹** `request_hash` | SHA-256 规范化请求 | 防止同键换金额、收款人或币种 | 否 |
| **渠道幂等键** `channel_key` | `pay_01J...:acquirer:charge:v1` | 让渠道拒绝同一副作用的重复提交 | 否 |

随机的 HTTP `request_id` 适合链路追踪，不适合作为业务幂等键。每次重试可以有新的 trace ID，但必须携带同一个业务幂等键和同一份规范化请求。

### 24.2.2 幂等的可证明性质

对命名空间 `N` 和键 `K`，系统应保证：

1. `N + K` 最多对应一个 `operation_id`。
2. 第一次接受的规范化请求指纹 `H` 被永久绑定；之后 `H' != H` 必须返回 `409 IDEMPOTENCY_KEY_REUSED`。
3. 任意并发请求最终只能读取同一结果，或者读取同一个尚未收敛的状态；不能各自创建业务事实。
4. `POSTED`、`SUCCEEDED` 等终态必须带原始响应或可重新构造结果的资源引用。
5. 客户端重试、消息重复投递、消费者重启和数据库死锁重试都复用 `N+K`，不得生成新的经济身份。
6. 原始请求、请求哈希、策略版本、渠道响应、账本凭证和回执事件能通过关联 ID 串成一条审计链。

因此，幂等不是数据库一列 `UNIQUE` 就结束，而是：

```text
稳定身份
  + 不可篡改的请求指纹
  + 同事务唯一占位
  + 有栅栏的执行租约
  + 同键状态机重放
  + 渠道查单/幂等键
  + 账本事实唯一键
  + 对账与人工终止边界
```

### 24.2.3 不能承诺的事情必须写进协议

以下承诺不能在没有下游配合时宣称成立：

- **“网络请求只会发送一次”**：TCP 重传、代理重试、客户端重发都会让服务端看到多次请求。
- **“分布式锁保证只调用一次渠道”**：锁租约过期或持有者暂停后，旧进程可能继续执行。
- **“数据库事务覆盖渠道”**：PostgreSQL commit 与外部银行扣款不是一个原子事务。
- **“HTTP 500 就是没扣款”**：响应丢失时，副作用状态可能是成功。
- **“消息 exactly-once”**：多数消息系统只能提供至少一次投递；业务必须幂等消费。

生产协议应明确：在平台内部，经济事实按同键至多接受一次；跨越不可控的外部系统，使用同键提交、查单、回执对账和 UNKNOWN 人工边界实现最终收敛。

---

## 24.3 客户端 → API → 账本 → 渠道：全链路幂等图

### 24.3.1 推荐全链路

```text
┌────────┐       同一业务键 K、规范化请求 H       ┌──────────────┐
│ 客户端 │ ─────────────────────────────────────► │ API 幂等网关  │
└───┬────┘                                        └──────┬───────┘
    │ 超时重试：K 不变，trace_id 可变                       │ 同事务 reserve
    │                                                       ▼
    │                                             ┌────────────────┐
    │                                             │ idempotency_request│
    │                                             │ operation P      │
    │                                             └──────┬─────────┘
    │                                                    │
    │                                                    │ Outbox，同一 DB commit
    │                                                    ▼
    │     ┌──────────────────── 查询/重放 ◄─────┌────────────────┐
    │     │                                      │ 业务状态机     │
    │     │                                      └──────┬─────────┘
    │     │                                             │ 事务内唯一事实
    │     │                                             ▼
    │     │                                      ┌────────────────┐
    │     │                                      │ 账本 posting    │
    │     │                                      │ operation_id=K  │
    │     │                                      └──────┬─────────┘
    │     │                                             │ 提交后才发布
    │     │                                             ▼
    │     │                                      ┌────────────────┐
    │     └──────────────────────────────────────│ channel worker  │
    │                                            │ lease+fence     │
    │                                            └──────┬─────────┘
    │                                                   │ channel_key
    │                                                   ▼
    │                                            ┌────────────────┐
    │                                            │ 渠道/银行       │
    │                                            │ accept/query    │
    │                                            └──────┬─────────┘
    │                                                   │ 回执/超时
    │                                                   ▼
    │                                            ┌────────────────┐
    └───── GET 同 K ◄───────────────────────────│ 回执与对账      │
                                                 │ SUCCESS/UNKNOWN │
                                                 └────────────────┘
```

关键边界如下：

1. **客户端到 API**：API 先绑定 `namespace + idempotency_key + request_hash`，再创建业务操作；不能先创建订单再补做幂等绑定。
2. **API 到账本**：账本 posting 的幂等键由业务事实派生，例如 `operation_id:debit:v1`，与支付 HTTP 重试完全同源。
3. **账本到渠道**：账本提交、渠道调用不能放入一个数据库事务。使用事务 Outbox 记录“应发送”的事实，worker 负责租约、栅栏、发送和回执。
4. **渠道到平台**：同步响应、异步 webhook、主动查单和日终文件都必须归并到同一个 `operation_id/channel_key`；任何入口都不能直接“再入账”。
5. **客户端查询**：重试后的正确动作是 `GET /payments/{operation_id}` 或用原 `K` 查询，而不是创建新的 POST 意图。

### 24.3.2 API 协议

请求头：

```http
POST /v1/payments
Idempotency-Key: merchant-42:checkout-991:v1
X-Client-Request-Id: 7e9b...       # 仅追踪，可每次变化
Content-Type: application/json

{"merchant_id":"m42","order_id":"checkout-991","currency":"USD","amount_minor":12500,"payer_ref":"u9"}
```

响应语义：

| HTTP | 语义 | 客户端动作 |
|---:|---|---|
| `201` | 本次或此前已成功创建，返回同一 `operation_id` | 可缓存并结束 |
| `200` | 同键已完成，返回原始或等价结果 | 不再 POST |
| `202` | 已占位/处理中/UNKNOWN，返回查询地址 | 轮询或订阅，不换键 |
| `409` | 同键请求指纹不同，或业务冲突 | 修正业务错误，禁止复用该键 |
| `429` | 限流，业务身份未改变 | 使用相同键退避重试 |
| `5xx`/网络超时 | 结果未知，不代表业务失败 | 用相同键查单 |

API 不应把 `IN_PROGRESS` 当成可新建请求，也不应把 `UNKNOWN` 映射为 `FAILED`。响应体至少包含 `operation_id`、当前状态、`retryable`、`query_url`、`server_time` 和 `request_hash` 的可审计摘要。

---

## 24.4 幂等键、请求指纹、同事务唯一索引与预签令牌

### 24.4.1 幂等键的设计规则

幂等键应来自业务事实，而非一次网络尝试。推荐格式：

```text
<租户>:<稳定业务号>:<动作>:<版本>
merchant-42:checkout-991:pay:v1
merchant-42:checkout-991:refund:part-0007:v1
```

规则：

1. 长度限制 1–192 字节，按字节而不是 Unicode 字符数限制；禁止空格、控制符和不可见分隔符。
2. 服务器保存原文和规范化后的键；不要静默 trim 后把两个不同键合成一个。
3. 命名空间必须包含租户、环境和动作作用域，避免测试环境或退款动作与支付动作碰撞。
4. 键一旦被接受，不能因为 TTL 到期而重新代表另一笔资金。保留期至少覆盖最大重试窗口、渠道最长结算窗口、对账窗口和监管留存要求。
5. 不要使用 `timestamp`、随机 UUID、线程 ID 或 Kafka offset 作为唯一业务键；它们可以做技术关联 ID，但重试时无法复用。

### 24.4.2 请求指纹：同键不同参数必须硬拒绝

请求指纹计算输入应是一个版本化、规范化的业务对象，而不是原始 JSON 字节：

```text
canonical_request_v1 = {
  "action": "PAY",
  "merchant_id": "m42",
  "order_id": "checkout-991",
  "currency": "USD",
  "amount_minor": 12500,
  "payer_ref": "u9",
  "policy_version": "pay-v7"
}
H = SHA-256(UTF-8(RFC-8785 canonical JSON(canonical_request_v1)))
```

必须明确：

- 金额使用最小单位整数；`125.00 USD` 不在哈希前以浮点数表示。
- 字段缺省、`null`、空字符串、大小写和 Unicode 归一化要有协议；不能让 Go 与 Java 产生不同哈希。
- 影响经济结果的风控版本、汇率快照、费率版本、收款账户、币种和渠道路由选择必须进入指纹，或进入不可变的业务事实版本。
- 不把 trace ID、重试次数、HTTP Date、服务器时间等非业务字段放进指纹。
- 哈希只用于发现冲突，不是保密机制；原始敏感字段仍需加密、脱敏和访问控制。

同键再来时：

```text
旧记录不存在                    → 原子占位并接受
旧记录存在，H_new == H_old      → 返回/继续旧 operation
旧记录存在，H_new != H_old      → 409 IDEMPOTENCY_KEY_REUSED
```

### 24.4.3 为什么必须同事务唯一索引

错误做法是：

```text
SELECT idempotency_key       -- 没查到
调用支付服务
INSERT idempotency_key       -- 其中一个可能晚到
```

两个并发事务都可能在 `SELECT` 时看不到记录。正确做法是把**业务占位、订单初始状态、必要的账本请求和 Outbox** 放在同一个 PostgreSQL 事务中，并让唯一索引成为最终裁判：

```sql
INSERT INTO idempotency_request (...)
VALUES (...)
ON CONFLICT (namespace, idempotency_key) DO NOTHING;
```

如果插入受唯一索引阻塞，提交后必须再次读取现有行并比较 `request_hash`。不能使用 `ON CONFLICT DO UPDATE` 无条件覆盖原哈希，也不能以 Redis 的“先 setnx 后写库”替代数据库事实。

### 24.4.4 预签令牌：把可执行意图锁定到一次副作用

预签令牌不是 OAuth access token，也不是“拿到令牌就必然成功”。它是平台在本地事务中为一个已占位操作签发的、短期、绑定参数和栅栏代数的能力证明，供渠道适配器或内部执行器使用。

推荐载荷：

```json
{
  "typ": "FIN-EXEC-v1",
  "operation_id": "pay_01J...",
  "idempotency_key": "merchant-42:checkout-991:pay:v1",
  "request_hash": "base64url(sha256)",
  "action": "CAPTURE",
  "amount_minor": 12500,
  "currency": "USD",
  "channel_key": "pay_01J...:acquirer:charge:v1",
  "fence": 4,
  "issued_at": "2026-11-25T12:00:00Z",
  "expires_at": "2026-11-25T12:05:00Z",
  "kid": "exec-key-2026-10"
}
```

令牌使用 Ed25519/JWS 或 HMAC（按组织密钥管理标准）签名，数据库保存 `token_hash`、`kid`、签发代数和撤销/使用状态。验证器必须检查：签名、过期时间、操作身份、金额币种、请求哈希、渠道键、fence 与当前代数。令牌不应放入 URL，不应记录完整 token 到日志。

预签令牌解决的是“旧 worker 在租约过期后继续提交”的一部分问题：执行器和支持栅栏的渠道都可以拒绝旧 `fence`。但如果外部渠道只接受普通 API key、没有幂等键也没有 fence，预签令牌不能让它获得 exactly-once；此时只能依赖查单、对账和 UNKNOWN 隔离。

### 24.4.5 租约、栅栏和锁过期穿透

```text
worker A 获得 lease_until=T，fence=7
        │ GC pause / 网络阻塞，时间超过 T
worker B 发现过期，获得 fence=8
        │ B 执行并提交成功
worker A 恢复，带 fence=7 写成功
```

如果表面上只有 `lease_until`，A 可能覆盖 B 的状态。生产实现至少需要：

1. 领取时事务内 `SELECT ... FOR UPDATE`，过期后 `fence = fence + 1`。
2. 所有本地状态更新都带 `WHERE operation_id=? AND fence=?`，影响行数不是 1 就拒绝旧 worker。
3. 外部调用携带 `channel_key` 和 fence/预签令牌；渠道支持时拒绝旧代数。
4. 调用开始后不能靠“释放锁”证明调用未发生；锁过期后的旧调用必须按 UNKNOWN 处理，禁止无条件发第二次。
5. watchdog、GC pause、网络延迟和时钟漂移都要纳入租约预算。租约不是时间真相，数据库提交和渠道查单才是事实来源。

---

## 24.5 PostgreSQL DDL：幂等事实、账本请求、渠道尝试与 Outbox

以下是可执行的基线 DDL。生产环境应结合分区、归档、分片键、密钥管理、监管保留期和实际 PostgreSQL 版本评审。金额均为最小单位整数；不要把 `NUMERIC`、浮点或“金额字符串”当作内部金额。

### 24.5.1 类型与顶层业务操作

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE operation_state AS ENUM (
  'RESERVED', 'PROCESSING', 'SUBMITTED', 'UNKNOWN',
  'SUCCEEDED', 'FAILED', 'CANCELLED', 'MANUAL_REVIEW'
);

CREATE TYPE attempt_state AS ENUM (
  'READY', 'DISPATCHING', 'SENT', 'UNKNOWN',
  'SUCCEEDED', 'FAILED', 'QUARANTINED'
);

CREATE TYPE outbox_state AS ENUM ('PENDING', 'PUBLISHED', 'DEAD');

CREATE TABLE financial_operation (
  operation_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id                 TEXT NOT NULL,
  action                    TEXT NOT NULL CHECK (action IN ('PAY', 'CAPTURE', 'REFUND', 'TRANSFER')),
  business_ref              TEXT NOT NULL,
  state                     operation_state NOT NULL DEFAULT 'RESERVED',
  currency_code             TEXT NOT NULL CHECK (currency_code ~ '^[A-Z0-9]{3,12}$'),
  amount_minor              BIGINT NOT NULL CHECK (amount_minor > 0),
  policy_version             TEXT NOT NULL,
  request_hash              BYTEA NOT NULL CHECK (octet_length(request_hash) = 32),
  parent_operation_id       UUID REFERENCES financial_operation(operation_id),
  result_code               TEXT,
  result_body               JSONB,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at                TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  completed_at              TIMESTAMPTZ,
  CHECK ((state IN ('SUCCEEDED', 'FAILED', 'CANCELLED')) = (completed_at IS NOT NULL))
);

CREATE INDEX financial_operation_business_idx
  ON financial_operation (tenant_id, business_ref, created_at DESC);

CREATE INDEX financial_operation_parent_idx
  ON financial_operation (parent_operation_id)
  WHERE parent_operation_id IS NOT NULL;
```

`business_ref` 是订单、转账或原支付的稳定引用，不取代 `operation_id`；同一订单可以有合法的支付、撤销、退款等不同动作。终态的 `result_body` 应保存脱敏后的原始协议结果或可重放结果，不应只保存一个布尔值。

### 24.5.2 幂等键表：哈希和操作绑定不可覆盖

```sql
CREATE TABLE idempotency_request (
  namespace                 TEXT NOT NULL,
  idempotency_key           TEXT NOT NULL,
  operation_id              UUID NOT NULL REFERENCES financial_operation(operation_id),
  request_hash              BYTEA NOT NULL CHECK (octet_length(request_hash) = 32),
  state                     operation_state NOT NULL DEFAULT 'RESERVED',
  response_http_status      INTEGER CHECK (response_http_status BETWEEN 100 AND 599),
  response_body             JSONB,
  lease_owner               TEXT,
  lease_until               TIMESTAMPTZ,
  fence                     BIGINT NOT NULL DEFAULT 0 CHECK (fence >= 0),
  created_at                TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at                TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (namespace, idempotency_key),
  UNIQUE (operation_id)
);

CREATE INDEX idempotency_lease_idx
  ON idempotency_request (state, lease_until)
  WHERE state IN ('RESERVED', 'PROCESSING', 'SUBMITTED', 'UNKNOWN');

-- 同一经济事实的同一账务动作最多一份请求。
CREATE TABLE ledger_posting_request (
  posting_request_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  operation_id              UUID NOT NULL REFERENCES financial_operation(operation_id),
  posting_type              TEXT NOT NULL,
  request_hash              BYTEA NOT NULL CHECK (octet_length(request_hash) = 32),
  status                    TEXT NOT NULL CHECK (status IN ('PENDING', 'POSTED', 'REJECTED')),
  voucher_id                BIGINT,
  response_body             JSONB,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at                TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (operation_id, posting_type)
);
```

真实账本的 `voucher_id`、借贷平衡、不可变分录和余额投影沿用第 22 章的模型；这里的 `ledger_posting_request` 是把支付业务事实与账本事实连接起来的唯一门闩。它不能让账本服务绕过自身的唯一约束。

### 24.5.3 渠道尝试、预签令牌与不可变回执

```sql
CREATE TABLE channel_attempt (
  attempt_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  operation_id              UUID NOT NULL REFERENCES financial_operation(operation_id),
  channel_code              TEXT NOT NULL,
  channel_key               TEXT NOT NULL,
  attempt_no                INTEGER NOT NULL CHECK (attempt_no > 0),
  state                     attempt_state NOT NULL DEFAULT 'READY',
  fence                     BIGINT NOT NULL CHECK (fence >= 0),
  token_hash                BYTEA CHECK (token_hash IS NULL OR octet_length(token_hash) = 32),
  provider_request_id       TEXT,
  provider_transaction_id   TEXT,
  provider_code             TEXT,
  request_sent_at           TIMESTAMPTZ,
  response_received_at      TIMESTAMPTZ,
  raw_response              JSONB,
  error_class               TEXT,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at                TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (operation_id, channel_code, channel_key),
  UNIQUE (channel_code, provider_request_id),
  UNIQUE (channel_code, provider_transaction_id)
);

CREATE INDEX channel_attempt_query_idx
  ON channel_attempt (channel_code, channel_key, state);

CREATE TABLE execution_token (
  token_id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  operation_id              UUID NOT NULL REFERENCES financial_operation(operation_id),
  channel_key               TEXT NOT NULL,
  fence                     BIGINT NOT NULL CHECK (fence >= 0),
  token_hash                BYTEA NOT NULL CHECK (octet_length(token_hash) = 32),
  key_id                    TEXT NOT NULL,
  issued_at                 TIMESTAMPTZ NOT NULL,
  expires_at                TIMESTAMPTZ NOT NULL,
  revoked_at                TIMESTAMPTZ,
  UNIQUE (operation_id, channel_key, fence),
  CHECK (expires_at > issued_at)
);

CREATE TABLE channel_receipt (
  receipt_id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  channel_code              TEXT NOT NULL,
  channel_key               TEXT NOT NULL,
  provider_transaction_id   TEXT,
  observed_state             TEXT NOT NULL,
  raw_payload               JSONB NOT NULL,
  payload_hash              BYTEA NOT NULL CHECK (octet_length(payload_hash) = 32),
  observed_at                TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (channel_code, channel_key, payload_hash)
);
```

`channel_attempt` 记录尝试，不代表每次尝试都能安全重新发送。`attempt_no=2` 仍然必须复用同一 `channel_key`；新增 attempt 行只是审计“又一次获得执行资格”，不是新的经济意图。

### 24.5.4 部分退款：派生键和金额上限

部分退款不是把原支付的金额直接改小，而是一个有父引用的新经济事实：

```sql
CREATE TABLE refund_request (
  refund_operation_id       UUID PRIMARY KEY REFERENCES financial_operation(operation_id),
  original_operation_id     UUID NOT NULL REFERENCES financial_operation(operation_id),
  refund_key                TEXT NOT NULL,
  request_hash              BYTEA NOT NULL CHECK (octet_length(request_hash) = 32),
  amount_minor              BIGINT NOT NULL CHECK (amount_minor > 0),
  state                     operation_state NOT NULL DEFAULT 'RESERVED',
  created_at                TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (original_operation_id, refund_key),
  UNIQUE (refund_operation_id)
);

CREATE INDEX refund_original_idx
  ON refund_request (original_operation_id, state);
```

推荐派生规则：

```text
refund_key = merchant_refund_id                     # 商户提供的稳定退款事实
refund_idempotency_key =
  <tenant>:<original_operation_id>:refund:<refund_key>:v1
channel_key = <refund_operation_id>:<channel>:refund:v1
```

如果业务没有稳定的退款号，服务端可以用原支付键加**客户端提供且不可变的退款项序号**派生：`original_key:refund:part-0007:v1`。禁止用金额、当前时间或数组下标（数组可重排）作为唯一退款身份。金额上限必须在同一事务中锁定原支付的累计退款汇总或使用可串行化的租户/订单行：

```text
0 < new_refund <= captured_amount - successful_or_reserved_refund_sum
```

同一个 `refund_key` 同金额重试返回原退款；同一个 `refund_key` 换金额返回 409；不同退款键可以合法产生多次部分退款，但总和不能超过可退金额。退款成功后必须生成独立的冲减账本事实，不能覆盖原支付分录。

### 24.5.5 Outbox：提交后可重复发布，提交前不能发渠道

```sql
CREATE TABLE outbox_event (
  event_id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  aggregate_type            TEXT NOT NULL,
  aggregate_id              UUID NOT NULL,
  event_type                TEXT NOT NULL,
  event_key                 TEXT NOT NULL,
  schema_version            INTEGER NOT NULL CHECK (schema_version > 0),
  payload                   JSONB NOT NULL,
  state                     outbox_state NOT NULL DEFAULT 'PENDING',
  attempts                  INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  available_at              TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  locked_by                 TEXT,
  locked_until              TIMESTAMPTZ,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  published_at              TIMESTAMPTZ,
  UNIQUE (event_type, event_key)
);

CREATE INDEX outbox_poll_idx
  ON outbox_event (available_at, created_at)
  WHERE state = 'PENDING';
```

业务状态、`outbox_event` 和幂等占位在同一个本地事务中提交。Relay 可以重复发布；消费者以 `event_type + event_key` 或操作状态版本幂等消费。绝不能在数据库事务尚未 commit 时先调用银行，再寄希望于回滚能撤销银行副作用。

**事件身份不能丢失命名空间**：API 的唯一键是 `(namespace, idempotency_key)`，裸 `idempotency_key` 并不全局唯一。本章统一使用 `event_key = operation_id` 的规范 UUID 文本（Go 读取数据库 UUID，Java 使用 `UUID.toString()`），配合 `event_type` 唯一化。不同 namespace 的相同客户端 key 会绑定不同 operation，因此 reserve 和完成事件均不会互相吞掉；同一 operation 的同类事件重放仍只保留一条。不要改成有分隔歧义的 `namespace + ':' + key`。若同类事件可在同一 operation 多次合法发生，应另加稳定的动作/状态版本；本文的 reserve 与终态事件每类仅一次。

### 24.5.6 同事务 reserve 的 SQL 轮廓

```sql
BEGIN;

-- 1. 业务操作和幂等绑定必须在同一事务中创建。
INSERT INTO financial_operation (
  tenant_id, action, business_ref, currency_code, amount_minor,
  policy_version, request_hash
) VALUES ($1, $2, $3, $4, $5, $6, $7)
RETURNING operation_id;

INSERT INTO idempotency_request (
  namespace, idempotency_key, operation_id, request_hash, state, fence
) VALUES ($8, $9, $10, $7, 'RESERVED', 0)
ON CONFLICT (namespace, idempotency_key) DO NOTHING;

-- 若幂等插入冲突，不能使用上面新建但未绑定的 operation；
-- 事务应回滚新 operation，再 SELECT 旧行比较 hash 并返回其 operation_id。

INSERT INTO outbox_event (
  aggregate_type, aggregate_id, event_type, event_key,
  schema_version, payload
) VALUES ('financial_operation', $10, 'OperationReserved', $10::text, 1, $11)
ON CONFLICT (event_type, event_key) DO NOTHING;

COMMIT;
```

工程实现通常先插入 `idempotency_request` 并在冲突分支复用已有 `operation_id`，避免“先建孤儿 operation”造成无意义对象。无论采用哪种 SQL 顺序，都必须让唯一约束和事务回滚保证不会留下第二个可执行事实。

---

## 24.6 UNKNOWN 超时与查单状态机

### 24.6.1 UNKNOWN 的精确定义

`UNKNOWN` 不是失败，也不是成功，更不是“稍后随便重试”。它表示：

> 当前证据不足以证明副作用未发生；自动系统必须先获得更多证据，或把操作交给对账/人工队列。

典型来源：

- HTTP 请求在服务端写出响应前断开；
- 渠道已接受请求但平台连接超时；
- worker 在发送后、记录响应前崩溃；
- webhook 已到渠道但平台消费失败；
- 数据库 commit 结果对调用者未知；
- 渠道返回“处理中”但没有明确最终状态。

网络层错误分类不能直接映射资金结论：`timeout`、`connection reset`、`EOF`、代理 `502` 都先进入 UNKNOWN，除非渠道的协议文档明确证明请求未到达且该证明本身可审计。

### 24.6.2 状态机

```text
                         ┌──────────────┐
                         │   RESERVED   │
                         └──────┬───────┘
                                │ worker lease
                                ▼
                         ┌──────────────┐
                    ┌───►│  PROCESSING  │◄──┐
                    │    └──────┬───────┘   │ lease retry
                    │           │ send      │
                    │           ▼           │
                    │    ┌──────────────┐    │
                    │    │  SUBMITTED   │────┘ provider pending
                    │    └───┬──────┬───┘
                    │        │      │ timeout/EOF
                    │        │      ▼
                    │        │ ┌──────────────┐
                    │        │ │   UNKNOWN    │──查单/对账──┐
                    │        │ └──────┬───────┘             │
                    │        │        │                     │
                    │        ▼        ▼                     ▼
                    │ ┌──────────┐  ┌──────────┐      ┌────────────┐
                    └─│ SUCCEEDED│  │  FAILED  │      │MANUAL_REVIEW│
                      └──────────┘  └──────────┘      └────────────┘
```

允许的核心转移：

| 当前 | 事件 | 下一状态 | 约束 |
|---|---|---|---|
| `RESERVED` | 获得 lease | `PROCESSING` | 增加 fence，不能覆盖终态 |
| `PROCESSING` | 发送前本地失败 | `RESERVED` | 同键可重试，保留尝试日志 |
| `PROCESSING` | 发送后无结论 | `UNKNOWN` | 不得假设未发送 |
| `SUBMITTED` | 渠道确认成功 | `SUCCEEDED` | 先写渠道事实，再幂等入账 |
| `SUBMITTED` | 渠道明确拒绝 | `FAILED` | 保存 provider code |
| `UNKNOWN` | 查到成功 | `SUCCEEDED` | 用原 channel key 归并 |
| `UNKNOWN` | 查到未找到且渠道保证未接受 | `RESERVED` | 仅在协议证据充分时重试 |
| `UNKNOWN` | 超过查单/对账 SLA | `MANUAL_REVIEW` | 停止自动扣款 |

状态转移必须是带版本或带 fence 的条件更新：

```sql
UPDATE idempotency_request
SET state = 'SUCCEEDED', response_http_status = $1,
    response_body = $2, updated_at = clock_timestamp()
WHERE namespace = $3
  AND idempotency_key = $4
  AND state IN ('PROCESSING', 'SUBMITTED', 'UNKNOWN')
  AND fence = $5;
```

### 24.6.3 超时后的标准查单算法

```text
1. 客户端用原 K 查询 API；API 返回 UNKNOWN/处理中与 query_url。
2. worker 使用原 operation_id 和 channel_key 调用渠道 query。
3. 若渠道 SUCCESS：写不可变 receipt；条件更新为 SUCCEEDED；账本 posting 用 operation_id:debit:v1 去重。
4. 若渠道 FAILED/NOT_FOUND：只有在渠道文档证明 NOT_FOUND 表示“未接受”时，才重新获得执行资格；仍用原 channel_key。
5. 若渠道 PENDING/UNKNOWN：指数退避继续查单，不能换 channel_key。
6. 超过重试次数或最长窗口：转 MANUAL_REVIEW，并进入日终文件/人工差错队列。
7. 日终对账发现成功：以原 channel_key 归并，补齐回执和账本；不能创建“对账专用第二笔支付”。
```

查单本身也要幂等：相同 `operation_id + channel_key` 的并发查单可以重复，但回执写入使用 `(channel_code, channel_key, payload_hash)` 唯一键，状态更新使用单调优先级，不能让迟到的 `PENDING` 覆盖 `SUCCESS`。

### 24.6.4 账本入账顺序

渠道成功并不自动等于平台账本已经成功。建议顺序：

```text
渠道成功证据
  → 写 channel_receipt（去重）
  → 在账本事务中插入 ledger_posting_request(operation_id, posting_type)
  → 借贷平衡的不可变 voucher/lines 同事务写入
  → 更新 operation 为 SUCCEEDED
  → 同事务写 OperationSucceeded outbox
```

如果账本事务失败，操作仍然是“渠道成功、平台账本待补记”的差异状态，而不是把渠道改成失败。补记重试使用相同 `operation_id` 和账本派生键；如果已经入账，唯一索引返回原凭证。

---

## 24.7 Go 1.24 核心代码：原子占位、栅栏租约与同键完成

下面代码展示核心控制面。省略 HTTP 路由、认证和指标注册，但保留了不能省略的数据库边界。真实项目应使用 `pgx/v5`、连接池、结构化日志和经过审计的 RFC 8785 canonical JSON 实现；示例中的 `CanonicalJSON` 接口必须由跨语言一致的实现提供，不能直接对任意 map 使用未约定的序列化。

### 24.7.1 数据结构与指纹

```go
// Go 1.24
package idempotency

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var (
	ErrKeyReused = errors.New("idempotency key reused with different request")
	ErrFenceLost = errors.New("execution fence lost")
	ErrTerminal  = errors.New("operation is terminal")
)

type PaymentIntent struct {
	TenantID      string `json:"tenant_id"`
	Action        string `json:"action"`
	BusinessRef   string `json:"business_ref"`
	Currency      string `json:"currency"`
	AmountMinor   int64  `json:"amount_minor"`
	PolicyVersion string `json:"policy_version"`
	PayerRef      string `json:"payer_ref"`
}

// CanonicalJSON 必须是跨 Go/Java 的 RFC 8785 实现，且只接受已验证的金额整数。
type CanonicalJSON func(any) ([]byte, error)

func RequestHash(canonical CanonicalJSON, in PaymentIntent) ([32]byte, error) {
	b, err := canonical(in)
	if err != nil {
		return [32]byte{}, fmt.Errorf("canonical request: %w", err)
	}
	return sha256.Sum256(b), nil
}

type Reservation struct {
	OperationID string
	Namespace   string
	Key         string
	Hash        [32]byte
	State       string
	Fence       int64
	ReplayBody  []byte
	ReplayCode  *int32
}

func validateKey(namespace, key string) error {
	if len(namespace) == 0 || len(namespace) > 128 || len(key) == 0 || len(key) > 192 {
		return fmt.Errorf("invalid idempotency namespace/key")
	}
	for i := 0; i < len(key); i++ {
		if key[i] <= 0x20 || key[i] == 0x7f {
			return fmt.Errorf("idempotency key contains control/space")
		}
	}
	return nil
}
```

### 24.7.2 Reserve：唯一索引负责并发，哈希负责防篡改

生产实现可以用数据库函数减少往返；下面代码故意把冲突路径展开，便于审计。注意：如果新操作插入后发现幂等冲突，事务会回滚，新操作不会留下可执行孤儿。

```go
func Reserve(ctx context.Context, db *pgxpool.Pool, namespace, key string,
	in PaymentIntent, hash [32]byte, owner string, lease time.Duration) (Reservation, error) {
	if err := validateKey(namespace, key); err != nil {
		return Reservation{}, err
	}
	if in.AmountMinor <= 0 || in.Currency == "" || in.TenantID == "" {
		return Reservation{}, fmt.Errorf("invalid payment intent")
	}

	tx, err := db.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return Reservation{}, err
	}
	defer tx.Rollback(ctx) // commit 后自动返回 ErrTxClosed，可忽略

	var operationID string
	err = tx.QueryRow(ctx, `
		INSERT INTO financial_operation
		  (tenant_id, action, business_ref, currency_code, amount_minor,
		   policy_version, request_hash)
		VALUES ($1,$2,$3,$4,$5,$6,$7)
		RETURNING operation_id`, in.TenantID, in.Action, in.BusinessRef,
		in.Currency, in.AmountMinor, in.PolicyVersion, hash[:]).Scan(&operationID)
	if err != nil {
		return Reservation{}, fmt.Errorf("create operation: %w", err)
	}

	var inserted bool
	err = tx.QueryRow(ctx, `
		WITH inserted AS (
		  INSERT INTO idempotency_request
		    (namespace, idempotency_key, operation_id, request_hash,
		     state, lease_owner, lease_until, fence)
		  VALUES ($1,$2,$3,$4,'PROCESSING',$5,
		          clock_timestamp()+$6 * interval '1 millisecond',1)
		  ON CONFLICT (namespace, idempotency_key) DO NOTHING
		  RETURNING true
		)
		SELECT COALESCE((SELECT true FROM inserted), false)`,
		namespace, key, operationID, hash[:], owner, lease.Milliseconds()).Scan(&inserted)
	if err != nil {
		return Reservation{}, fmt.Errorf("reserve key: %w", err)
	}
	if inserted {
		// 这三个写入和上面的 operation/幂等占位同事务。
		_, err = tx.Exec(ctx, `
			INSERT INTO outbox_event
			  (aggregate_type, aggregate_id, event_type, event_key,
			   schema_version, payload)
			VALUES ('financial_operation',$1,'OperationReserved',$2,1,$3)
			ON CONFLICT (event_type,event_key) DO NOTHING`,
			operationID, operationID, fmt.Sprintf(`{"operation_id":%q}`, operationID))
		if err != nil {
			return Reservation{}, fmt.Errorf("reserve outbox: %w", err)
		}
		if err := tx.Commit(ctx); err != nil {
			return Reservation{}, fmt.Errorf("commit reserve: %w", err)
		}
		return Reservation{OperationID: operationID, Namespace: namespace,
			Key: key, Hash: hash, State: "PROCESSING", Fence: 1}, nil
	}

	// 冲突时整个事务回滚新 operation，然后读取已经提交的唯一事实。
	_ = tx.Rollback(ctx)
	var r Reservation
	var oldHash []byte
	var code *int32
	err = db.QueryRow(ctx, `
		SELECT operation_id, request_hash, state, fence,
		       response_http_status, response_body
		FROM idempotency_request
		WHERE namespace=$1 AND idempotency_key=$2`, namespace, key).
		Scan(&r.OperationID, &oldHash, &r.State, &r.Fence, &code, &r.ReplayBody)
	if errors.Is(err, pgx.ErrNoRows) {
		// 可能是对方事务尚未提交后又回滚；调用方可用同一键重试。
		return Reservation{}, fmt.Errorf("reservation disappeared: %w", err)
	}
	if err != nil {
		return Reservation{}, fmt.Errorf("read existing reservation: %w", err)
	}
	if len(oldHash) != 32 || !equal32(oldHash, hash[:]) {
		return Reservation{}, ErrKeyReused
	}
	r.Namespace, r.Key, r.Hash, r.ReplayCode = namespace, key, hash, code
	return r, nil
}

func equal32(a, b []byte) bool {
	if len(a) != 32 || len(b) != 32 { return false }
	for i := range a { if a[i] != b[i] { return false } }
	return true
}

func HashHex(h [32]byte) string { return hex.EncodeToString(h[:]) }
```

`INSERT ... ON CONFLICT` 的冲突等待和提交可见性由 PostgreSQL 唯一索引保证。高并发下不能把“查不到旧行”直接当成可以新建；必须让同一事务继续等待/重试，或回到同一个键的保守查询路径。

### 24.7.3 租约续期和带 fence 的完成

```go
func RenewLease(ctx context.Context, db *pgxpool.Pool, r Reservation,
	owner string, lease time.Duration) error {
	ct, err := db.Exec(ctx, `
		UPDATE idempotency_request
		SET lease_until=clock_timestamp()+$1 * interval '1 millisecond',
		    updated_at=clock_timestamp()
		WHERE namespace=$2 AND idempotency_key=$3
		  AND operation_id=$4 AND state IN ('PROCESSING','SUBMITTED','UNKNOWN')
		  AND lease_owner=$5 AND fence=$6
		  AND (lease_until IS NULL OR lease_until > clock_timestamp())`,
		lease.Milliseconds(), r.Namespace, r.Key, r.OperationID, owner, r.Fence)
	if err != nil { return err }
	if ct.RowsAffected() != 1 { return ErrFenceLost }
	return nil
}

func Complete(ctx context.Context, db *pgxpool.Pool, r Reservation,
	owner, state string, httpCode int32, body []byte) error {
	if state != "SUCCEEDED" && state != "FAILED" {
		return fmt.Errorf("invalid terminal state %q", state)
	}
	tx, err := db.Begin(ctx)
	if err != nil { return err }
	defer tx.Rollback(ctx)

	ct, err := tx.Exec(ctx, `
		UPDATE idempotency_request
		SET state=$1, response_http_status=$2, response_body=$3::jsonb,
		    lease_owner=NULL, lease_until=NULL, updated_at=clock_timestamp()
		WHERE namespace=$4 AND idempotency_key=$5 AND operation_id=$6
		  AND lease_owner=$7 AND fence=$8
		  AND state NOT IN ('SUCCEEDED','FAILED','CANCELLED')`,
		state, httpCode, body, r.Namespace, r.Key, r.OperationID, owner, r.Fence)
	if err != nil { return err }
	if ct.RowsAffected() != 1 { return ErrFenceLost }

	_, err = tx.Exec(ctx, `
		UPDATE financial_operation
		SET state=$1, result_code=$2, result_body=$3::jsonb,
		    completed_at=clock_timestamp(), updated_at=clock_timestamp()
		WHERE operation_id=$4 AND state NOT IN ('SUCCEEDED','FAILED','CANCELLED')`,
		state, fmt.Sprintf("HTTP_%d", httpCode), body, r.OperationID)
	if err != nil { return err }
	_, err = tx.Exec(ctx, `
		INSERT INTO outbox_event
		  (aggregate_type,aggregate_id,event_type,event_key,schema_version,payload)
		VALUES ('financial_operation',$1,$2,$3,1,$4::jsonb)
		ON CONFLICT (event_type,event_key) DO NOTHING`,
		r.OperationID, "Operation"+state, r.OperationID, body)
	if err != nil { return err }
	return tx.Commit(ctx)
}
```

真实支付 worker 不应在 `Complete(SUCCEEDED)` 前直接把渠道成功当作平台成功；应先把原始回执落在 `channel_receipt`，再在账本事务中用 posting 唯一键完成入账，最后以同一事务更新终态或设置“渠道成功待记账”的中间状态。上段代码是 API 幂等控制面，不是替代账本事务的简化。

### 24.7.4 Go 的重试边界

```go
func RetryableDatabase(err error) bool {
	// pgconn.PgError 的 Code 在真实实现中检查 40001、40P01、55P03。
	// 仅数据库事务失败可以用同一业务键重试；外部副作用超时不能套用这个判断。
	var pe interface{ SQLState() string }
	if errors.As(err, &pe) {
		switch pe.SQLState() {
		case "40001", "40P01", "55P03":
			return true
		}
	}
	return false
}

// 重试伪代码：指数退避 + 抖动，永远复用 namespace/key/hash。
// maxAttempts 到达后返回 UNKNOWN/人工队列，不生成新 key。
func backoff(attempt int) time.Duration {
	if attempt > 8 { attempt = 8 }
	return time.Duration(1<<attempt)*100*time.Millisecond + time.Duration(attempt*17)*time.Millisecond
}
```

数据库死锁重试不能包住渠道调用；应只重做失败的本地事务。若数据库 commit 结果未知，查询同一幂等键；不能为了“确认是否写入”而再执行一遍扣款逻辑。

---

## 24.8 Java 21 核心代码：JDBC 事务与状态机条件更新

Java 服务使用 `BigInteger` 或最小单位 `long`，绝不能使用 `double`。如果资产最小单位可能超过 `long`，应使用经过边界校验的 `BigInteger`，数据库字段也要相应扩展；通常支付单笔金额用 `BIGINT` 并在 API 层拒绝溢出。

### 24.8.1 规范化指纹与类型安全金额

```java
// Java 21
package com.example.fin.idempotency;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HexFormat;

public final class Fingerprints {
    private Fingerprints() {}

    public record PaymentIntent(
            String tenantId,
            String action,
            String businessRef,
            String currency,
            long amountMinor,
            String policyVersion,
            String payerRef) {
        public PaymentIntent {
            if (tenantId == null || tenantId.isBlank()) throw new IllegalArgumentException("tenantId");
            if (businessRef == null || businessRef.isBlank()) throw new IllegalArgumentException("businessRef");
            if (currency == null || !currency.matches("[A-Z0-9]{3,12}")) throw new IllegalArgumentException("currency");
            if (amountMinor <= 0) throw new IllegalArgumentException("amountMinor");
        }
    }

    // 生产实现应由 RFC-8785 canonical JSON 组件生成，字段顺序、Unicode、数字格式必须跨语言一致。
    public static byte[] canonical(PaymentIntent p) {
        String s = "{"
                + "\"action\":\"" + esc(p.action()) + "\","
                + "\"amount_minor\":" + p.amountMinor() + ","
                + "\"business_ref\":\"" + esc(p.businessRef()) + "\","
                + "\"currency\":\"" + esc(p.currency()) + "\","
                + "\"payer_ref\":\"" + esc(p.payerRef()) + "\","
                + "\"policy_version\":\"" + esc(p.policyVersion()) + "\","
                + "\"tenant_id\":\"" + esc(p.tenantId()) + "\""
                + "}";
        return s.getBytes(StandardCharsets.UTF_8);
    }

    private static String esc(String s) {
        if (s == null) return "";
        return s.replace("\\", "\\\\").replace("\"", "\\\"")
                .replace("\n", "\\n").replace("\r", "\\r");
    }

    public static byte[] sha256(PaymentIntent p) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(canonical(p));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    public static String hex(byte[] hash) { return HexFormat.of().formatHex(hash); }

    public static boolean sameHash(byte[] a, byte[] b) {
        return MessageDigest.isEqual(a, b);
    }
}
```

上例为固定字段的演示，生产 JSON 转义必须覆盖完整 JSON 字符集，不能复制这段简化的 `esc` 去处理任意对象。更稳妥的做法是组织级 canonical JSON 库，Go 和 Java 用跨语言测试向量验证同一意图的哈希完全一致。

### 24.8.2 JdbcTemplate 事务内 reserve

```java
package com.example.fin.idempotency;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.support.TransactionTemplate;

import java.sql.Timestamp;
import java.time.Duration;
import java.time.Instant;
import java.util.UUID;

public final class IdempotencyService {
    public record Reservation(UUID operationId, String namespace, String key,
                              byte[] requestHash, String state, long fence,
                              Integer replayCode, String replayBody) {}

    private final JdbcTemplate jdbc;
    private final TransactionTemplate tx;

    public IdempotencyService(JdbcTemplate jdbc, TransactionTemplate tx) {
        this.jdbc = jdbc;
        this.tx = tx;
    }

    public Reservation reserve(String namespace, String key,
                               Fingerprints.PaymentIntent intent,
                               String owner, Duration lease) {
        validateKey(namespace, key);
        byte[] hash = Fingerprints.sha256(intent);
        return tx.execute(status -> {
            UUID operationId = UUID.randomUUID();
            try {
                jdbc.update("""
                    INSERT INTO financial_operation
                      (operation_id, tenant_id, action, business_ref, currency_code,
                       amount_minor, policy_version, request_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, operationId, intent.tenantId(), intent.action(),
                        intent.businessRef(), intent.currency(), intent.amountMinor(),
                        intent.policyVersion(), hash);

                int n = jdbc.update("""
                    INSERT INTO idempotency_request
                      (namespace, idempotency_key, operation_id, request_hash,
                       state, lease_owner, lease_until, fence)
                    VALUES (?, ?, ?, ?, 'PROCESSING', ?, ?, 1)
                    ON CONFLICT (namespace, idempotency_key) DO NOTHING
                    """, namespace, key, operationId, hash, owner,
                        Timestamp.from(Instant.now().plus(lease)));

                if (n == 0) {
                    // 让当前事务回滚新 operation；外层 catch 后在新连接查询旧事实。
                    status.setRollbackOnly();
                    throw new ExistingKeyException(namespace, key, hash);
                }

                jdbc.update("""
                    INSERT INTO outbox_event
                      (aggregate_type, aggregate_id, event_type, event_key,
                       schema_version, payload)
                    VALUES ('financial_operation', ?, 'OperationReserved', ?, 1, ?::jsonb)
                    ON CONFLICT (event_type, event_key) DO NOTHING
                    """, operationId, operationId.toString(), "{\"operation_id\":\"" + operationId + "\"}");

                return new Reservation(operationId, namespace, key, hash,
                        "PROCESSING", 1, null, null);
            } catch (ExistingKeyException e) {
                throw e;
            }
        });
    }

    // 调用方捕获 ExistingKeyException 后，必须在新事务/连接读旧行并比对 hash。
    public Reservation replayExisting(String namespace, String key, byte[] expectedHash) {
        return jdbc.queryForObject("""
            SELECT operation_id, request_hash, state, fence,
                   response_http_status, response_body::text
            FROM idempotency_request
            WHERE namespace=? AND idempotency_key=?
            """, (rs, row) -> {
                byte[] actual = rs.getBytes("request_hash");
                if (!Fingerprints.sameHash(actual, expectedHash)) {
                    throw new KeyReusedException(namespace, key);
                }
                return new Reservation(rs.getObject("operation_id", UUID.class),
                        namespace, key, actual, rs.getString("state"),
                        rs.getLong("fence"),
                        (Integer) rs.getObject("response_http_status"),
                        rs.getString("response_body"));
            }, namespace, key);
    }

    private static void validateKey(String namespace, String key) {
        if (namespace == null || namespace.isBlank() || namespace.length() > 128
                || key == null || key.isBlank() || key.length() > 192
                || key.chars().anyMatch(c -> c <= 0x20 || c == 0x7f)) {
            throw new IllegalArgumentException("invalid idempotency key");
        }
    }

    public static final class ExistingKeyException extends RuntimeException {
        final String namespace, key; final byte[] hash;
        ExistingKeyException(String n, String k, byte[] h) { namespace=n; key=k; hash=h; }
    }
    public static final class KeyReusedException extends RuntimeException {
        KeyReusedException(String n, String k) { super("key reused: " + n + ":" + k); }
    }
}
```

应用框架的事务异常传播要特别注意：`ExistingKeyException` 不能被同一个标记为 rollback-only 的事务捕获后继续查旧行，否则后续 SQL 可能得到“事务已失败”。应让事务结束，再用独立查询读取冲突行。上面示例使用 `setRollbackOnly()` 明确表达这个边界。

### 24.8.3 Java 条件状态转移与 fence

```java
public int complete(UUID operationId, String namespace, String key,
                    String owner, long fence, String state,
                    int httpStatus, String body) {
    if (!state.equals("SUCCEEDED") && !state.equals("FAILED")) {
        throw new IllegalArgumentException("not terminal");
    }
    return tx.execute(status -> {
        int n = jdbc.update("""
            UPDATE idempotency_request
            SET state=?, response_http_status=?, response_body=?::jsonb,
                lease_owner=NULL, lease_until=NULL, updated_at=clock_timestamp()
            WHERE namespace=? AND idempotency_key=? AND operation_id=?
              AND lease_owner=? AND fence=?
              AND state NOT IN ('SUCCEEDED','FAILED','CANCELLED')
            """, state, httpStatus, body, namespace, key, operationId, owner, fence);
        if (n != 1) throw new IllegalStateException("fence lost or already terminal");

        jdbc.update("""
            UPDATE financial_operation
            SET state=?, result_code=?, result_body=?::jsonb,
                completed_at=clock_timestamp(), updated_at=clock_timestamp()
            WHERE operation_id=? AND state NOT IN ('SUCCEEDED','FAILED','CANCELLED')
            """, state, "HTTP_" + httpStatus, body, operationId);

        jdbc.update("""
            INSERT INTO outbox_event
              (aggregate_type, aggregate_id, event_type, event_key,
               schema_version, payload)
            VALUES ('financial_operation', ?, ?, ?, 1, ?::jsonb)
            ON CONFLICT (event_type, event_key) DO NOTHING
            """, operationId, "Operation" + state, operationId.toString(), body);
        return n;
    });
}
```

`complete` 返回影响行数 0 时，不能把它翻译为“调用失败”。它可能是旧 fence、已被另一个 worker 完成、状态已终态或数据库重试后的重复完成；调用方应读取同键最终状态。

---

## 24.9 部分退款与派生业务键的完整规则

### 24.9.1 退款不是原支付的重放

支付键 `pay:v1` 的重试只能得到原支付结果，不能通过把 action 改成 refund 复用它。退款是新的业务事实，但必须指向父支付：

```text
支付：  P = (tenant, order, PAY, pay_key, pay_hash)
退款：  R1 = (tenant, P, REFUND, refund_key_001, refund_hash_001)
退款：  R2 = (tenant, P, REFUND, refund_key_002, refund_hash_002)
```

派生关系：

```text
refund_operation_id       = server-generated UUID/ULID
refund_idempotency_key    = tenant + ":" + parent_operation_id
                            + ":refund:" + stable_refund_ref + ":v1
refund_request_hash       = H(parent_operation_id, refund_ref, amount, currency, reason, policy)
refund_channel_key        = refund_operation_id + ":" + channel + ":refund:v1
refund_ledger_key         = refund_operation_id + ":refund_posting:v1
```

服务端不应把“退款金额”放进 key 以规避冲突，因为同一稳定退款号改金额必须被识别为篡改，而不是被当成两笔退款。金额属于 hash；身份属于 key。

### 24.9.2 退款并发扣减

两个不同退款键同时请求剩余 1000，分别退款 700 和 500，不能都先读到 1000 再通过。可选方案：

- 锁定原支付聚合行和退款汇总行，按固定顺序计算 `captured - reserved_or_succeeded_refund`；
- 使用单独的 `refund_quota` 行做原子 `UPDATE ... SET consumed=consumed+$amount WHERE consumed+$amount<=captured`；
- 对跨分片父支付采用顺序化 actor/分布式配额服务，但最终仍需账本和对账校验。

无论哪种方案，同一退款键必须先走幂等唯一索引；否则一次退款请求在等待额度锁期间超时，再次进入可能产生第二笔退款事实。

### 24.9.3 退款、冲正和拒付的差异

| 动作 | 业务含义 | 是否覆盖原支付 | 账本表达 |
|---|---|---:|---|
| 退款 | 商户/平台主动返还客户 | 否 | 新的反向支付事实 |
| 冲正 | 修复本平台错误事实 | 否 | 原凭证的反向新凭证 |
| 拒付 | 银行/卡组织争议结果 | 否 | 渠道证据驱动的新争议事实 |

三者都应有独立幂等键、独立状态机和父引用，不能把原支付状态直接从 `SUCCEEDED` 改成 `REFUNDED` 后丢掉金额和时间线。

---

## 24.10 并发、重试、锁过期穿透与运行时纪律

### 24.10.1 并发矩阵

至少验证以下并发组合：

| 场景 | 预期 |
|---|---|
| 同键同 hash，1000 并发 | 一个操作，其他全部重放同一结果或处理中状态 |
| 同键不同金额 | 一个接受，其余 409；无第二 operation、无第二 outbox |
| 不同键同订单 | 按订单业务约束拒绝或串行，不能仅依靠幂等键 |
| 同操作支付与退款 | 按父事实和额度锁序列化，退款不能超过成功支付 |
| 同键跨 API 实例 | 唯一索引裁决，不依赖实例本地内存 |
| 不同 namespace、相同客户端 key | 两个独立 operation，各有自己的 reserve 与完成事件；不可被 Outbox 全局唯一键吞掉 |
| worker A/B 竞争 lease | 只有当前 fence 能更新状态；旧 worker 完成返回 fence lost |
| webhook 与主动查单同时到达 | 回执去重，状态单调，最终只入账一次 |
| 账本提交与 API 响应并发崩溃 | 用同键查到已提交结果，不新建事实 |
| Outbox 重复发布 | 消费者幂等；不重复发渠道或入账 |
| 数据库 deadlock/serialization failure | 只重做本地事务，复用原键，不重复外部副作用 |

### 24.10.2 固定锁序和短事务

涉及多个行时统一锁序，例如：

```text
租户/父业务聚合行 → 幂等请求行 → 额度/余额桶 → posting request → outbox
```

按稳定的二进制 ID 排序锁多账户，避免 A 锁 Alice 等 Bob、B 锁 Bob 等 Alice。事务内不调用渠道、不等待 Kafka、不做大批量 HTTP、不执行不可控的远程 DNS。事务越长，锁等待越长，超时重试越容易放大为重入风暴。

### 24.10.3 超时和重试分类

| 失败点 | 证据 | 动作 |
|---|---|---|
| API 参数校验 | 明确未进入业务事务 | 修正请求，不重试同一错误 |
| DB 事务死锁/序列化失败 | 本次本地事务回滚 | 同键、同 hash 重试事务 |
| Outbox publish 失败 | 本地事实已提交 | 重试发布，不重做业务 |
| 渠道连接建立前失败 | 需协议证明未发送 | 可同 `channel_key` 重试；保守时先查单 |
| 渠道发送后超时 | 无最终证据 | UNKNOWN，查单，不换 key |
| webhook 处理失败 | 渠道事实可能已存在 | 重投递/主动查单，回执去重 |
| 账本 commit 超时 | 提交未知 | 同账本 key 查 posting，不再新建 |

重试器必须区分 `local_retryable` 和 `external_unknown`。把所有 `IOException` 都塞进统一重试队列，是支付重复扣款最常见的代码级根因之一。

### 24.10.4 过期锁不是取消信号

数据库 lease 到期只表示“当前 worker 不再拥有写入资格”，不表示进程停了、不表示 HTTP 请求取消、不表示银行没收到。必须同时实现：

- context cancellation 和 HTTP client deadline，降低旧调用继续完成的概率；
- fence 条件更新，阻止旧 worker 修改本地终态；
- 渠道幂等键/预签令牌，阻止下游接受旧执行意图（若渠道支持）；
- 超时后的查单和人工边界，覆盖渠道不支持 fence 的情况。

### 24.10.5 监控指标

必备指标不只看成功率：

```text
idempotency.reserve.conflict_total{namespace}
idempotency.key_reused_total{namespace}
idempotency.replay_total{state}
operation.unknown_age_seconds{channel}
operation.manual_review_total{reason}
lease.fence_lost_total{worker}
channel.query_total{result}
channel.duplicate_rejected_total{channel}
ledger.posting_duplicate_total{posting_type}
outbox.publish_duplicate_total{event_type}
reconciliation.unmatched_amount_minor{currency}
```

告警应按金额、币种、租户和 UNKNOWN 年龄分层；只监控请求数会掩盖“一笔大额 UNKNOWN”比一万笔小额成功更严重的风险。

---

## 24.11 混沌工程与压测：证明重试安全，而不是只证明吞吐

### 24.11.1 必测故障注入点

在测试渠道/沙箱中注入：

1. DB `INSERT idempotency_request` 后、commit 前进程 kill。
2. DB commit 成功后、HTTP response 写出前进程 kill。
3. Outbox 写入后 relay kill，重复发布同一 event。
4. 渠道请求发送前 kill、发送后响应前 kill、响应收到后落库前 kill。
5. webhook 延迟、重复、乱序、签名失败和 payload 字段缺失。
6. Redis/锁服务不可用、锁提前过期、时钟偏移、网络分区和 DNS 抖动。
7. worker 停顿超过 lease（模拟 STW/GC pause），旧 worker 恢复后继续写。
8. PostgreSQL `40001`、`40P01`、连接断开和 commit 结果未知。
9. 渠道查单返回 `PENDING → SUCCESS`、`SUCCESS → PENDING` 乱序，以及同 key 不同金额异常响应。
10. 日终对账文件重复、漏行、延迟、跨日和币种/小数位错误。

每个注入点都要验证经济不变量，不只是 HTTP 状态码：

```text
同一 operation_id 的成功扣款事实 <= 1
同一 channel_key 的渠道成功归属 <= 1
同一 operation_id + posting_type 的 voucher <= 1
退款累计 <= 可退款金额
每币种借贷平衡
终态不可被迟到低优先级事件覆盖
```

### 24.11.2 并发压测模型

建议分四组压测，而非只打一个平均 QPS：

| 压测组 | 流量 | 目标 |
|---|---|---|
| 热键组 | 1 个键的 10k 并发重试 | 唯一索引等待、重放延迟、无重复事实 |
| 冷键组 | 百万不同键，1% 重试 | 写入吞吐、索引膨胀、Outbox 吞吐 |
| UNKNOWN 组 | 渠道 30% 发送后超时 | 查单队列、人工边界、无盲重试 |
| 组合组 | 同订单支付/退款/查单/webhook | 锁序、状态单调、金额上限 |

压测报告至少包含 p50/p95/p99/p999、锁等待、连接池、唯一冲突率、DB WAL、Outbox lag、UNKNOWN 年龄、查单成功率和每币种差错金额。吞吐高但 `request_hash` 冲突、UNKNOWN 堆积或对账不平的系统不能上线。

### 24.11.3 性质测试和模型检查

用随机生成的事件序列验证参考状态机：

```text
Reserve(K,H) × N
Retry(K,H) × N
Retry(K,H')
Send(channel_key) / Timeout / Query
Webhook(SUCCESS|PENDING|FAILED) 乱序
LeaseExpire / FenceSteal
LedgerCommit / Crash
Refund(R1) / Refund(R2)
```

无论事件排列如何，最终在所有外部证据可见且重试停止后都应满足同一终态和同一金额。把状态机事件持久化后，可以用 Jepsen 风格测试、TLA+、QuickCheck/rapid 或基于数据库快照的 property test 检查“旧事件不能覆盖新状态”“同键不分叉”。

### 24.11.4 验收门槛示例

- 1000 并发同键：`operation_id` 只有 1 个；`outbox_event` 只有 1 个；API 响应最多一个新建，其余为重放/处理中。
- 1% 故障注入下连续 24 小时：账本和渠道对账差异为 0；UNKNOWN 必须全部有 owner 和 SLA。
- 任何 `IDEMPOTENCY_KEY_REUSED` 不得产生第二个 operation、channel attempt 或 posting。
- 旧 fence 的更新影响行数必须为 0，并产生指标和审计日志。
- 迟到 `PENDING` 不得覆盖已确认 `SUCCESS`；迟到旧 webhook 不得重复入账。
- 全链路请求和回执可由 `operation_id`、`channel_key`、voucher ID 互相查询，敏感数据已脱敏。

### 24.11.5 跨命名空间事件身份回归

以下是供 Go/Java 共用的**集成测试示意，未在本次修订中连接或运行 PostgreSQL**；内存集合只能验证键设计，不能代替唯一索引等待、事务回滚和 commit 可见性测试。

```text
对 Go Reserve/Complete 与 Java reserve/complete 分别执行：
  A = reserve(namespace="tenant-A:prod:PAY", key="checkout-001", hash=HA)
  B = reserve(namespace="tenant-B:prod:PAY", key="checkout-001", hash=HB)
  assert A.operation_id != B.operation_id
  assert 各自同 namespace/key/hash 重试返回原 operation_id
  assert 同 namespace/key 换 hash 被拒绝，且无额外 operation/event

  complete(A, SUCCEEDED); complete(B, SUCCEEDED)
  assert 每个 operation 各有 1 条 OperationReserved 和 1 条 OperationSUCCEEDED
  assert 上述每条 event_key == aggregate_id 的规范 UUID 文本
  assert 两个 namespace 共 4 条事件（旧裸 key 实现只会留下 2 条）
  再读终态并重复投递原事件：事件数仍为 4，消费者经济事实数不增加
  另建一组操作，以 FAILED 完成，验证 OperationFAILED 同样不跨 namespace 碰撞
```

混合部署时还应让 Go/Java 交替重放同一 namespace/key，检查返回同一 operation；不要让两个进程各自生成的候选 UUID 成为事件键，必须使用**成功绑定到幂等记录**的 operation。历史裸 key 事件若已发布，迁移需核对消费者去重身份和已执行事实，不能简单改 key 后全量重发。

---

## 24.12 极端边界：最容易被“正常流程”遗漏的情况

### 24.12.1 金额与键

- `amount_minor=0`、负数、`int64` 溢出、币种精度不匹配必须在 API 拒绝。
- `USD 1.00` 与 `USD 1.0` 经过同一金额解析后应得到同一最小单位；不能一处按字符串比较、一处按数值比较。
- Unicode 同形字符、前后空白、大小写和超长 key 要么按协议规范化，要么拒绝，不能静默合并。
- 哈希算法、canonical JSON 版本升级必须版本化；旧键重放仍使用原版本，不能用新算法把旧事实判成篡改。
- SHA-256 冲突在工程上极低但不是协议的唯一安全性；必须同时比对作用域、资源字段和长度，敏感数据不能只靠哈希保护。

### 24.12.2 时间与状态

- 租约使用数据库时间或明确的单调时钟预算；客户端时间不能决定过期。
- 闰秒、NTP 回拨、跨时区、夏令时不应影响键和状态；保存 UTC `timestamptz`，业务日另存。
- `UNKNOWN` 跨自然日不能被日切程序当成失败；必须进入跨日未决和对账队列。
- 已成功操作不能因退款、冲正或拒付把原状态改成“从未成功”；用子事实表达后续动作。
- 终态返回后，重试仍要能查到原结果；不能因为响应缓存过期就重新执行。

### 24.12.3 渠道差异

- 支持幂等键的渠道：固定 `channel_key`，重复提交应返回同一 provider transaction。
- 只支持商户订单号的渠道：把稳定 `channel_key` 映射为商户订单号，并验证渠道是否保证全局/商户范围唯一。
- 既不支持查单又不支持幂等的渠道：付款后超时必须人工或对账，不得自动发第二次；产品上要接受可用性降低。
- 渠道成功但平台账本失败：补记账本，不退款；渠道失败但平台账本已提交：进入差错补偿/冲正状态，不静默覆盖。
- webhook 签名正确不等于内容新鲜；校验时间窗、nonce、channel key、金额、币种和状态单调。

### 24.12.4 数据治理

- 原始渠道响应常含卡号、账户号、姓名等敏感字段：加密存储、字段脱敏、最小权限和独立审计。
- 日志不得打印完整预签 token、支付凭证或完整请求体；打印 `operation_id`、哈希摘要和安全截断键。
- 幂等记录不能过早 GC。若必须归档，线上查询需能安全读取归档事实，并保留“此键已使用”的墓碑，避免同键被未来重新解释。
- 分区表归档前，必须保证父操作、posting、receipt、refund 和 outbox 的引用关系可追溯。
- 数据修复禁止直接 `UPDATE balance` 或删除幂等行；使用补偿事实、冲正、回执归并和审计工单。

### 24.12.5 部署与版本

- 新旧服务并行时，canonical JSON、状态枚举、渠道键算法和响应格式必须向后兼容。
- 数据库唯一索引创建要评估锁和线上写入，可先建 `CONCURRENTLY` 索引再切约束；不能以短时维护窗口掩盖重复事实。
- worker 版本升级不可改变同一 operation 的 channel key；若必须切换渠道，建立有明确父子关系的新 attempt/路由事实，而不是覆盖旧 key。
- 重放工具默认只读；生产执行需要工单、审批、原键、原哈希和预期影响金额，禁止“按状态批量重发”。

---

## 24.13 术语表：把几个经常混淆的词分开

| 术语 | 精确定义 | 不是它 |
|---|---|---|
| 幂等键 | 重试时复用的稳定业务身份 | 随机 trace ID |
| 请求指纹 | 规范化业务参数的哈希 | 加密后的完整请求 |
| 业务事实 | 一次有经济含义、可审计的动作 | 一次 HTTP 尝试 |
| operation | 平台内部对业务事实的聚合对象 | 数据库连接 |
| 同事务唯一索引 | 并发创建同一事实时的最终裁判 | Redis TTL 去重 |
| reserve | 原子占位并绑定键、哈希和 operation | 已经扣款 |
| lease | worker 的暂时执行资格 | 能取消外部 HTTP 的锁 |
| fence/栅栏 | 单调递增的执行代数，阻止旧 worker 写回 | 时间戳锁 |
| 预签令牌 | 绑定 operation、哈希、金额、渠道键和 fence 的短期能力凭证 | 用户登录 token |
| channel key | 渠道侧同一副作用的稳定身份 | 每次 HTTP 的 request ID |
| UNKNOWN | 没有足够证据判定副作用结果 | FAILED |
| 查单 | 通过原渠道身份询问外部最终状态 | 再发一笔新支付 |
| Outbox | 与本地事实同事务保存的待发布事件 | 事务外先发消息 |
| 回执 | 渠道/银行观察到的结果证据 | API 访问日志 |
| 重放 | 用原键读取/推进同一事实 | 创建新业务请求 |
| 部分退款 | 指向原支付的新反向经济事实 | 把原支付金额改小 |
| 冲正 | 用新凭证反向修复错误事实 | 删除原凭证 |
| exactly-once | 在受控边界内对经济事实的至多一次接受/记账 | 全网络只发送一个包 |
| 对账 | 比较平台事实与外部事实并修复差异 | 业务实时状态机的替代品 |

---

## 24.14 面试答题卡：被追问时按这条主线回答

### Q1：为什么 Redis `SETNX(key, value, 5m)` 不够？

**答**：它与经济事实不在同一事务，TTL 会造成迟到请求穿透，进程暂停会造成锁过期穿透，Redis 成功而数据库失败还会留下假占位。正确做法是数据库同事务唯一索引绑定业务键、请求哈希和 operation；Redis 只能做性能层，不能做最终事实。执行租约还要有 fence，外部副作用要用渠道幂等键和查单。

### Q2：客户端超时后能不能生成新幂等键再试？

**答**：不能。超时是 UNKNOWN，因为渠道可能已经扣款。必须用原键查询 API，再由平台用原 `channel_key` 查渠道；只有渠道明确证明未接受时才能同键重试。新键会把一次不确定的经济事实变成两个独立事实。

### Q3：数据库事务能不能包住银行调用？

**答**：不能把普通 PostgreSQL 事务和银行调用当成一个原子事务。事务内写 operation、posting request 和 Outbox，commit 后 worker 调渠道；发送后超时进入 UNKNOWN，靠查单、webhook、日终对账收敛。若渠道不支持幂等和查单，系统只能降级为人工确认，不能承诺 exactly-once。

### Q4：同键不同金额怎么办？

**答**：第一次接受时保存规范化请求的 SHA-256 和版本。后来同键同 hash 返回原结果；同键不同 hash 返回 `409 IDEMPOTENCY_KEY_REUSED`，不能以最新请求覆盖，也不能删除旧键。金额、币种、收款人、费率/策略版本等经济字段必须进 hash。

### Q5：锁过期后旧 worker 恢复，如何防止它覆盖新结果？

**答**：领取时递增 fence，所有本地更新带 `WHERE fence=current`，影响行数为 0 就拒绝旧 worker；外部请求带渠道幂等键和预签令牌/fence（渠道支持时由渠道拒绝旧代数）。旧 worker 在发送后恢复仍按 UNKNOWN，不因为“自己以为没成功”而重新扣款。

### Q6：账本已经成功，API 返回超时，怎么处理？

**答**：客户端用原键重试，API 读取 `idempotency_request` 和原 response/resource；账本 posting 的 `(operation_id, posting_type)` 唯一索引保证不会第二次入账。不能新建订单或凭证。响应丢失是可恢复的 UNKNOWN/重放，不是新业务。

### Q7：部分退款如何防止重复和超退？

**答**：退款是带父支付引用的新 operation。用商户稳定退款号派生 `refund_idempotency_key`，金额进入 hash；同号同金额重放，同号换金额 409。不同退款号在同一父聚合/配额行上串行校验 `累计退款 <= 可退金额`，账本和渠道使用退款 operation 的独立派生键。

### Q8：消息系统说 exactly-once，还需要幂等吗？

**答**：需要。broker 的 exactly-once 通常只覆盖其内部生产/消费语义，不能覆盖数据库、HTTP 渠道和外部银行。Outbox relay、webhook、查单、对账文件都可能重复或乱序，消费者必须用 event key、operation 状态版本和数据库唯一约束保护经济事实。

### Q9：为何不把 UNKNOWN 直接当失败，释放冻结并允许重试？

**答**：因为失败没有证据。若渠道已成功，直接释放冻结再发新请求会造成双扣或负余额。UNKNOWN 必须有查单重试预算、最大年龄、人工 owner、日终对账和明确终止动作；在证据不足时宁可降低可用性，不能制造第二笔钱。

### Q10：幂等键保存多久？

**答**：不能只按 API 的几分钟 TTL。至少覆盖客户端最大重试窗口、渠道最长异步/结算窗口、日终对账和监管保留要求；归档后保留可查询事实或墓碑，避免旧键未来重新表示另一笔操作。保留期是风险和法规参数，不是缓存参数。

---

## 24.15 自检题与交付清单

### 24.15.1 设计自检

1. 画出客户端、API、账本、Outbox、worker、渠道、webhook、查单和对账之间的身份传递；每一条箭头使用什么 key？
2. API 超时、渠道超时、账本 commit 超时分别是什么状态？为什么不能都返回 `FAILED`？
3. 同一幂等键并发 1000 次时，哪一行/哪一个唯一索引是最终裁判？
4. 同键不同金额如何检测？请求哈希是否包含币种、收款人、费率版本和策略版本？
5. 旧 worker 的 lease 过期后恢复，哪些 SQL 条件会阻止它写回？渠道是否支持 fence？不支持时的人工边界在哪里？
6. 账本 posting 和支付 API 的键如何派生？账本入账成功但 API 响应丢失时能否只读重放？
7. webhook 与主动查单谁先到、谁后到，如何保证 `SUCCESS` 不被迟到 `PENDING` 覆盖？
8. 部分退款的稳定身份是什么？两个退款并发时如何证明不超退？
9. 你的幂等表 GC/分区归档后，旧键重试会发生什么？
10. 渠道没有幂等键、没有查单、只有异步文件时，系统是否诚实地进入 UNKNOWN/人工，而不是假装自动安全？

### 24.15.2 代码和 SQL 自检

- [ ] 金额全部是币种最小单位整数，未使用 `float`/`double`。
- [ ] `namespace + idempotency_key` 有数据库唯一约束。
- [ ] `request_hash` 长度、版本和 canonical JSON 有跨 Go/Java 测试向量。
- [ ] 同键不同 hash 返回 409，不覆盖原记录。
- [ ] operation、幂等占位和 Outbox 在同一事务。
- [ ] reserve/完成事件的 `event_key` 均为已绑定的稳定 `operation_id`，跨 namespace 同 key 不丢事件。
- [ ] 账本 `operation_id + posting_type` 唯一，分录不可变，错误用冲正。
- [ ] channel key 稳定派生，重复 attempt 不生成新的经济身份。
- [ ] lease 有 owner、过期时间和单调 fence；所有写回带 fence 条件。
- [ ] 外部发送后任何网络异常都能进入 UNKNOWN，不自动按“未发送”处理。
- [ ] 查单、webhook、对账文件都经过同一回执去重和状态单调逻辑。
- [ ] partial refund 有父引用、派生键、请求哈希和累计金额约束。
- [ ] 数据库死锁/序列化重试只重试本地事务，不重做已发生的外部副作用。
- [ ] 监控 UNKNOWN 年龄、fence 丢失、key 冲突、重复发布和对账金额。
- [ ] 日志和预签令牌符合敏感数据、密钥轮换和最小权限要求。

### 24.15.3 场景题

**场景 A：** 渠道返回成功，平台在账本 commit 后宕机，客户端持续收到 503。
**应答要点：** 重试同键读取已提交 operation/posting；Outbox 可重复发布；不能再次扣款或再次入账。

**场景 B：** worker A 发送后卡住，lease 过期；worker B 查不到渠道记录，于是发送同一支付；随后 A、B 的成功回执都到达。
**应答要点：** 渠道必须按同一 channel key 去重；若渠道无法证明，B 不应自动发送，进入 UNKNOWN/人工；两份回执按 provider transaction 和 channel key 归并，只允许一个平台经济事实，异常进入对账。

**场景 C：** 同一退款号第一次金额 100，第一次响应超时，第二次金额改成 80。
**应答要点：** 第二次请求 hash 不同，返回 409；先查原退款状态，不能把 80 当成修正。若要改金额，按业务规则取消未执行退款或新建明确的退款修正事实。

---

## 24.16 与前后章节的衔接

### 24.16.1 与 20、21、22、23 的输入输出

- **第 20 章金额精度与分配**提供最小单位整数、规则版本和尾差可重放输入；金额规范化结果必须进入请求指纹。
- **第 21 章时间、交易日历与全局流水号**提供 UTC/业务日、跨日语义和技术流水号；流水号不是幂等键，不能混用。
- **第 22 章复式账本上篇**提供不可变 voucher、借贷平衡、余额投影和 `ledger_posting_request` 的事实边界；本章把支付 operation 绑定到这些事实。
- **第 23 章复式账本下篇**提供热点分桶、冲正、跨币种和对账要求；本章的重复 posting、退款、UNKNOWN 补记都必须遵守这些账本不变量。

### 24.16.2 与后续状态机、支付网关、对账和 OMS

本章的 `RESERVED/PROCESSING/SUBMITTED/UNKNOWN/SUCCEEDED` 是跨服务身份与重试的骨架；后续状态机章节可以扩展 TCC、Saga、补偿和卡单恢复，但不能删除本章的同键、同 hash、fence 和 UNKNOWN 语义。

支付网关章节应把 `channel_attempt`、查单能力矩阵、路由切换和渠道差异落地；多层对账章节应消费 `channel_receipt`、日终文件和账本 posting，形成自动归并与人工差错闭环；OMS/FIX 章节应把 `ClOrdID/ExecID` 映射为同样的稳定业务身份，不能仅依赖连接会话。

### 24.16.3 第 24.8 节衔接约定

本章 **24.8 Java 21 核心代码**与 24.7 的 Go 1.24 实现共享以下跨语言契约：

```text
canonical_request_v1 → SHA-256 → request_hash
namespace + idempotency_key → 唯一 operation_id
event_type + operation_id 的规范 UUID 文本 → 唯一 Outbox 事件
operation_id + action + version → ledger/channel derived key
lease generation → fence
UNKNOWN → query(original channel key) → reconcile/manual boundary
```

后续任何 Java/Go/其他语言实现都必须通过同一组 golden vectors：相同租户、订单、币种、最小单位金额、策略版本和退款项，产生完全相同的规范化 JSON 与 SHA-256；状态转移和数据库唯一约束也必须保持相同语义。若实现为了“方便”重新生成 key、忽略 hash 冲突、把超时改成失败或绕过 posting 唯一索引，即使单元测试通过，也不再属于本章定义的金融级幂等体系。

最终交付不是一段 middleware，而是一组可审计不变量：

```text
同一业务意图不分叉
同一键不换参数
同一账务动作不重复入账
旧执行者不能覆盖新执行者
未知副作用不被假定为失败
渠道、账本、回执和对账最终可归并
任何修复都追加事实，不删除历史
```
