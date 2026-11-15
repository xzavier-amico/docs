# 39. 多存储下的事务边界

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 存储选型 ｜ **编号**：39 / 52
> **本章定位**：真实系统几乎不可能只用一个存储。一旦你同时写 MySQL、Redis、ES、对象存储、向量库，就必然面对一个问题：**「谁说了算，以及不一致能持续多久」**。本章给四种不一致形态、五套解法、一套可执行的验证。
> **读法**：§39.2 的一致性等级表决定了你能承诺什么；§39.4 的四种写法对比是核心；§39.7 的十个坑建议逐条对照现有代码。

---

## 39.1 事故现场：一次商品改价，3,400 单按旧价卖出

2025 年 8 月，某电商做「限时秒杀」，商品详情页的数据来自三个存储：

```text
MySQL（商品主数据）  →  Redis（详情缓存，TTL 10 分钟）  →  ES（搜索列表）
```

改价流程是「先改 MySQL，再删 Redis，再更新 ES」。运营在 09:58 把秒杀价从 199 改成 179。

| 时间 | 事件 |
| --- | --- |
| 09:58:00 | 运营点了「保存」，MySQL 更新成功 |
| 09:58:00.120 | 删 Redis 缓存失败（超时），代码 `catch` 后**只打了日志，继续往下走** |
| 09:58:00.300 | ES 更新成功（列表页显示 179） |
| 09:58:01 | 用户从搜索页点进详情页，**命中旧缓存 199** |
| 09:58~10:10 | 先后 **3,400 单** 以 199 成交（详情页 199，列表页 179） |
| 10:12 | 客服发现「价格不一致」投诉 200+ |
| 10:20 | 手动清缓存 + 全量重建 ES |
| 10:35 | 数据一致，但已成交订单需逐个处理 |

**直接损失 6.8 万元**（3,400 单 × 20 元差价补偿）+ 客服与运营 3 人天。

根因不是「缓存没删掉」这么简单，而是**四个设计缺陷叠加**：

1. **双写没有兜底**：删缓存失败被 catch 吞掉，没有重试，没有对账。
2. **没有「以谁为准」的定义**：详情页信 Redis，列表页信 ES，两者都不是权威。
3. **TTL 太长**：10 分钟的旧价窗口，足够卖 3,400 单。
4. **没有版本号**：即使晚到的旧值回写，也不会被拒绝。

> 🩸 **一句话**：**多存储不是一个「同步问题」，是一个「权威源 + 不一致窗口 + 兜底」的问题。** 只想「怎么同步」，一定出事。

---

## 39.2 精确定义：一致性等级与你必须承诺的东西

### 四种不一致形态（按发生频率排序）

| 形态 | 时序 | 现象 | 典型原因 |
| --- | --- | --- | --- |
| **写成功一半** | DB 成功、Redis 失败 | 缓存脏，读到旧值 | 删缓存失败被吞；网络抖动 |
| **顺序错** | 先删缓存、后写库 | 并发读把旧值回填 | 经典 cache-aside 竞态 |
| **重试重复** | 同一次写被重试两次 | 计数翻倍、消息重复 | 无幂等 |
| **缓存脏（延迟回写）** | 旧值回填 / 旧值晚到 | 读到旧值，新值被覆盖 | 删缓存失败后回填；无版本号；异步复制乱序 |

### 一致性等级表（写进设计文档，别口头说「最终一致」）

| 等级 | 定义 | 能承诺什么 | 代价 | 典型实现 |
| --- | --- | --- | --- | --- |
| **L0 强一致** | 任何时刻读到的都是最新已提交值 | 读到的一定最新 | 高（跨存储分布式事务） | 单库事务 / 2PC（不推荐跨存储） |
| **L1 读己之写** | 同一用户写后立刻能读到自己的写 | 用户不会「刚改完看不见」 | 中 | 写后读主库；缓存主动失效 + 等待 |
| **L2 有界不一致** | 不一致窗口 ≤ T（如 1s / 5s） | 「最多旧 T 秒」 | 中 | CDC 同步 + 版本号 + 监控延迟 |
| **L3 最终一致** | 一段时间后一致，无上界承诺 | 「最终会一致」 | 低 | Outbox + 重试 + 对账 |
| **L4 可能不一致** | 不保证 | 无 | 最低 | 双写无兜底（**事故温床**） |

> 💡 **选型铁律**：**缓存/搜索/向量这类「派生存储」最多承诺 L2，永远不能承诺 L0。** 想让 Redis 强一致 = 想让它变成数据库 = 选错了工具（第 37 章）。
> **对外承诺的等级，要与业务损失挂钩**：价格/库存 → L1~L2；商品描述 → L3；推荐位 → L4 可接受。

---

## 39.3 结构图：三条写入路径

```text
【路径 A：应用双写（最常见，也最容易错）】

  写请求 ──▶ ① 写 MySQL ──▶ ② 删 Redis ──▶ ③ 更新 ES
                 │                │              │
                 └─ 任一失败 ─────┴──────────────┘
                     ↓
              只能打日志/重试（无事务保证）

【路径 B：Outbox + CDC（推荐）】

  写请求 ──▶ ┌───────────────────────────┐
             │ 本地事务：                  │
             │   UPDATE product ...       │
             │   INSERT INTO outbox ...   │
             └───────────┬───────────────┘
                         │ binlog（原子提交）
                         ▼
                 ┌───────────────┐
                 │ Debezium/Canal │
                 └───┬───────┬───┘
                     ▼       ▼
                Redis 失效   ES 更新（带版本号）
                     │       │
                     └───┬───┘
                         ▼
                  失败进重试队列 + 对账兜底

【路径 C：读时重建（cache-aside + 版本）】

  读请求 ──▶ Redis 命中？ ──是──▶ 返回
                │否
                ▼
          查 MySQL（带 version）──▶ 写回 Redis（仅当 version 更新）
```

**三条路径的选择**：

| 场景 | 推荐路径 | 理由 |
| --- | --- | --- |
| 写少读多、允许秒级旧 | A（双写）+ 版本号 + 对账 | 简单，够用 |
| 数据重要、要求有界不一致 | **B（Outbox + CDC）** | 与业务同事务，天然可靠 |
| 缓存只是加速、可随时重建 | C（读时重建） | 不需要同步逻辑 |
| 多存储且写入频繁 | B + C 组合 | CDC 负责失效，读时兜底重建 |

---

## 39.4 怎么实现：五种写法与它们的真实保证

### 写法一：先写库、再删缓存（cache-aside 标准姿势）

```java
@Transactional
public void updatePrice(long productId, long newPriceCents, long expectedVersion) {
    // 1) 库为准：带版本号的条件更新，防止旧值覆盖
    int n = jdbc.update(
        "UPDATE product SET price_cents=?, version=version+1, updated_at=NOW() " +
        "WHERE id=? AND version=?", newPriceCents, productId, expectedVersion);
    if (n == 0) throw new OptimisticLockException("version conflict");

    // 2) 事务提交后删缓存（不要放事务里，否则回滚了缓存已删）
    TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
        @Override public void afterCommit() {
            cacheDeleteWithRetry(productId);   // 失败进重试队列，不吞
        }
    });
}

private void cacheDeleteWithRetry(long id) {
    try {
        redis.delete("product:" + id);
    } catch (Exception e) {
        // ★ 关键：不吞异常。落本地重试表 / 发 MQ，由后台补偿
        retryQueue.add(new CacheInvalidateTask("product:" + id, System.currentTimeMillis()));
        metrics.counter("cache.delete.fail").increment();
    }
}
```

**为什么是「删」不是「更新」**：

| 做法 | 问题 |
| --- | --- |
| 更新缓存 | 两个并发写可能以错误顺序写缓存（旧值覆盖新值） |
| 删缓存 | 下次读时从库重建，天然以库为准 |

### 写法二：延迟双删（应对「读回填旧值」的竞态）

```text
时序（危险）：
  T1 写库（新值）→ T2 删缓存 → T3 读请求未命中 → T4 查库（此时可能读到旧值？）
      → T5 把旧值写回缓存 → 缓存脏

为什么 T4 会读到旧值？
  主从延迟：写的是主库，读的是从库（延迟 200ms）
```

```java
// 延迟双删：删一次，延迟 500ms 再删一次（覆盖主从延迟窗口）
public void invalidateWithDoubleDelete(long id) {
    redis.delete(key(id));
    scheduler.schedule(() -> redis.delete(key(id)), 500, TimeUnit.MILLISECONDS);
}
```

**延迟双删的局限**（必须知道）：

- 500ms 是拍脑袋值，**主从延迟可能更长**（大事务、批量写时几秒）。
- 第二次删也可能失败 → 仍需重试队列。
- 高并发下仍可能有窗口 → **终极兜底是版本号 + TTL**。

> 💡 **更稳的做法**：**缓存里存版本号**，读时比较库的版本，旧版本不写回。

### 写法三：版本号防旧值覆盖（最被低估的一招）

```go
type Product struct {
    ID       int64  `json:"id"`
    PriceCents int64 `json:"priceCents"`
    Version  int64  `json:"version"`   // 单调递增
}

// 读：缓存命中也要校验版本（可选，成本高）
func (s *Service) Get(ctx context.Context, id int64) (*Product, error) {
    if p, ok := s.cache.Get(ctx, id); ok && p.Version >= s.dbVersion(ctx, id) {
        return p, nil
    }
    p, err := s.repo.Get(ctx, id)          // 库为准
    if err != nil { return nil, err }
    s.cache.SetIfNewer(ctx, p)             // 只有版本更新才写回
    return p, nil
}

// SetIfNewer：Redis Lua 原子比较版本
const setIfNewerLua = `
local cur = redis.call('HGET', KEYS[1], 'version')
if (not cur) or (tonumber(ARGV[2]) > tonumber(cur)) then
  redis.call('HSET', KEYS[1], 'version', ARGV[2], 'payload', ARGV[1])
  redis.call('EXPIRE', KEYS[1], ARGV[3])
  return 1
end
return 0
`
```

**这一招同时解决**：① 旧值回填；② 乱序的异步更新；③ 延迟双删的窗口。

### 写法四：Outbox + CDC（推荐给重要数据）

```sql
-- Outbox 表（与业务表同库，同事务）
CREATE TABLE outbox (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    aggregate_type VARCHAR(64) NOT NULL,          -- 路由用：product/order/...
    aggregate_id  BIGINT NOT NULL,
    event_type    VARCHAR(64) NOT NULL,
    payload       JSON NOT NULL,
    version       BIGINT NOT NULL,
    status        TINYINT NOT NULL DEFAULT 0,   -- 0 待投递 1 已投递 2 失败
    retry_count   INT NOT NULL DEFAULT 0,
    next_retry_at DATETIME(3) NULL,
    created_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    KEY idx_status_next (status, next_retry_at),
    KEY idx_agg (aggregate_id, id)
) ENGINE=InnoDB;
```

```java
// 业务写入与 outbox 同事务：这是「原子」的唯一来源
@Transactional
public void updateProduct(Product p) {
    productRepo.update(p);                                   // 业务表
    outboxRepo.insert(Outbox.of("product", p.getId(),        // 第一参 = aggregate_type
                                "PRODUCT_UPDATED", toJson(p), p.getVersion())); // outbox
}
```

```json
// Debezium 配置：把 outbox 事件路由到 Kafka（EventRouter SMT）
{
  "name": "outbox-connector",
  "config": {
    "connector.class": "io.debezium.connector.mysql.MySqlConnector",
    "database.hostname": "mysql",
    "database.port": "3306",
    "database.user": "debezium",
    "database.password": "${env:DB_PASSWORD}",
    "topic.prefix": "cdc",
    "schema.history.internal.kafka.bootstrap.servers": "kafka:9092",
    "schema.history.internal.kafka.topic": "schema-changes.shop",
    "database.include.list": "shop",
    "table.include.list": "shop.outbox",
    "transforms": "outbox",
    "transforms.outbox.type": "io.debezium.transforms.outbox.EventRouter",
    "transforms.outbox.route.by.field": "aggregate_type",
    "transforms.outbox.route.topic.replacement": "cdc.${routedByValue}",
    "transforms.outbox.table.field.event.key": "aggregate_id",
    "transforms.outbox.table.field.event.payload": "payload",
    "transforms.outbox.table.field.event.type": "event_type",
    "transforms.outbox.table.field.event.timestamp": "created_at"
  }
}
```

**消费端（更新 Redis + ES，带版本号）**：

```go
func (h *Handler) HandleProductUpdated(ctx context.Context, e Event) error {
    var p Product
    if err := json.Unmarshal(e.Payload, &p); err != nil {
        return err // 进重试/DLQ
    }
    // ① Redis：版本号比较后写（防乱序）
    if _, err := h.redis.Eval(ctx, setIfNewerLua,
        []string{"product:" + strconv.FormatInt(p.ID, 10)},
        p.PayloadJSON, p.Version, 600).Result(); err != nil {
        return err
    }
    // ② ES：external version，旧事件自动被拒（409）
    _, err := h.es.Update(ctx, "products", strconv.FormatInt(p.ID, 10), p,
        h.es.WithVersion(p.Version), h.es.WithVersionType("external"))
    if err != nil && !isConflict(err) {   // 409 = 旧版本，正常
        return err
    }
    return nil
}
```

> 💡 **两个「防乱序」的硬件级开关**：Redis 的 Lua 版本比较、ES 的 `version_type=external`。**没有它们，CDC 的乱序一定会让缓存/索引回到旧值。**

### 写法五：对账兜底（最后一道防线）

无论用哪种写法，都必须有**定期比对**：

```sql
-- MySQL 为准，抽查 Redis / ES 是否一致（按 updated_at 增量）
-- 1) 找出最近 1 小时变更过的商品
SELECT id, price_cents, version, updated_at
FROM product
WHERE updated_at >= NOW() - INTERVAL 1 HOUR
ORDER BY updated_at
LIMIT 1000;
```

```go
// 2) 抽样比对（应用侧，10% 采样，避免全量压垮缓存）
func (c *Checker) Verify(ctx context.Context) (mismatch int, err error) {
    rows, err := c.db.QueryContext(ctx, `
        SELECT id, price_cents, version FROM product
        WHERE updated_at >= NOW() - INTERVAL 1 HOUR
        ORDER BY RAND() LIMIT 200`)
    if err != nil { return 0, err }
    defer rows.Close()
    for rows.Next() {
        var id, price, ver int64
        if err := rows.Scan(&id, &price, &ver); err != nil { return 0, err }
        cached, err := c.redis.HGetAll(ctx, "product:"+strconv.FormatInt(id, 10)).Result()
        if err != nil { continue }
        // 版本落后 = 正常（可能还没消费到），版本相同但值不同 = 真不一致
        if cv, _ := strconv.ParseInt(cached["version"], 10, 64); cv == ver {
            if cached["priceCents"] != strconv.FormatInt(price, 10) {
                mismatch++
                c.repair(id)    // 主动删缓存/重建 ES
            }
        }
    }
    return mismatch, nil
}
```

**对账的判据**：

| 指标 | 含义 | 告警阈值 |
| --- | --- | --- |
| 抽样不一致率 | 同版本但值不同 | > 0.1% 告警 |
| 同步延迟 P99 | CDC 事件到消费完成 | > 5s 告警 |
| 缓存失效失败数 | 删缓存失败累计 | > 10/min |
| Outbox 积压 | status=0 且 created_at 超 1 分钟 | > 100 条 |

---

## 39.5 权威源：一句话写进设计文档

```text
┌──────────────────────────────────────────────────────────┐
│ 权威源（Source of Truth）：MySQL.shop.product             │
│                                                          │
│ 派生存储（Derived，可随时重建）：                          │
│   - Redis  product:{id}      TTL 10min   L2 有界不一致     │
│   - ES     products          索引          L2 有界不一致   │
│   - 向量库  product_vec       索引          L3 最终一致    │
│                                                          │
│ 重建方式：全量任务 + 增量 CDC；重建期间读服务降级到 MySQL    │
│ 不一致处理：抽样对账 → 主动失效/重建 → 记录工单            │
└──────────────────────────────────────────────────────────┘
```

**必须写清的四件事**：

1. **谁是权威**（表名 + 字段级，必要时指明「金额以 `product.price_cents` 为准」）。
2. **每个派生存储能承诺的等级**（L2/L3）。
3. **重建方式与耗时**（出事时能不能一键重建）。
4. **不一致的发现与处理**（对账频率、阈值、修复动作、负责人）。

> 🩸 **没有第 1 条的团队，会在事故复盘会上吵 40 分钟「到底哪边是对的」。**

---

## 39.6 双修对照：向量索引与原文的一致性

| 维度 | 传统多存储 | Agent 多存储 | 相同 / 差异根因 |
| --- | --- | --- | --- |
| **权威源** | MySQL | 原文（对象存储/DB）+ 会话事件表 | 相同原则 |
| **派生存储** | Redis、ES | 以上 + **向量库 + 语义缓存 + 摘要** | 多两类 |
| **更新单位** | 一行/一个文档 | **一个 chunk**（文档切片） | 粒度更细 |
| **删除复杂度** | 删 key / 删 doc | **删原文要级联删所有 chunk 向量** | 容易漏删（合规风险） |
| **版本** | `version` 字段 | 文档版本 + **embedding 模型版本 + 切分版本** | 换模型要全量重建 |
| **不一致后果** | 显示旧价格 | **模型基于已删除内容作答**（合规/隐私事故） | 更严重 |
| **对账** | 抽样比对价格 | 抽样「按 chunk 反查原文是否存在」 | 同构 |
| **重建成本** | 重建缓存（秒~分） | **重新 embedding（小时 + 花钱）** | 高一个量级 |

**Agent 特有的三条纪律**：

1. **删除必须级联**：删文档 → 删所有 chunk → 删向量 → 删语义缓存。用 `doc_id` 做统一关联，删除走同一个服务，不允许多处直删。
2. **换 embedding 模型 = 全量重建**：不同模型的向量空间不可比，混用会导致召回质量断崖。版本进索引名（`product_vec_v2`），切换时双写 + 灰度。
3. **语义缓存要带出处**：缓存命中时仍要返回原文引用；原文已删除时缓存必须失效（否则「答了不该答的」）。

**Agent 侧的「多存储」不止向量库，先把五笔账算清**：

| 形态 | 传统对应 | Agent 侧的具体形态 | 权威/派生 | 典型失败 |
| --- | --- | --- | --- | --- |
| 事件表膨胀 | 订单流水/操作日志 | 每次 run/step/tool_call 各写事件；一次请求 10~30 行 | 权威（会话真相） | 不分区、不归档 → 单表 10 亿行，写入和回放都慢 |
| 向量库 | ES 倒排索引 | 一篇文档切成 N 个 chunk，每个 chunk 一条向量 | 派生，可重建 | 删原文漏删向量；换 embedding 模型不重建 → 召回崩 |
| 语义缓存 | Redis 结果缓存 | 相似问题直接复用旧答案 | 派生 + TTL + 出处 | 原文更新/删除后仍命中旧答案，答了不该答的 |
| Prompt/会话存储 | 审计日志/工单 | 每轮完整 prompt + 工具结果，单条 10KB~1MB | 会话原文权威，摘要派生 | 塞进 MySQL 大字段 → 行溢出、备份膨胀；全放对象存储 → 读放大 |
| 写放大 | 双写 | 一次工具调用写事件表、会话、向量、缓存、审计 5 处 | 只有事件表与业务表同事务 | 同步写 5 处 → P99 翻倍；异步写 → 不一致窗口 |

算一笔账：日 10 万次 Agent 请求，每次 8 个 step、每 step 2 条事件 = 160 万行/天；每行 1KB（含工具参数与结果摘要）≈ 1.6GB/天，一年约 580GB。若每个 step 还把完整 prompt（平均 8KB）写进 MySQL，日增 6.4GB，一年约 2.3TB。**写放大不是「多写几次」，是把 1 次用户动作放大成 10~20 次存储写入；P99 和成本都按这个倍数走。**

```sql
-- 向量与原文的一致性对账（pgvector）
-- 找出「向量存在但原文已删」的孤儿 chunk
SELECT c.id, c.doc_id
FROM chunks c
LEFT JOIN documents d ON d.id = c.doc_id
WHERE d.id IS NULL
LIMIT 100;
-- 反向：原文存在但无向量（漏切/漏 embedding）
SELECT d.id
FROM documents d
LEFT JOIN chunks c ON c.doc_id = d.id
WHERE c.id IS NULL AND d.status = 'PUBLISHED'
LIMIT 100;
```

---

## 39.7 会在哪炸：十个坑

| # | 坑 | 现象 | 根因 | 怎么查 | 怎么修 |
| --- | --- | --- | --- | --- | --- |
| 1 | 删缓存失败被吞 | 缓存一直脏 | `catch { log }` | 搜空 catch / 指标无失败计数 | 落重试队列 + 告警 |
| 2 | 先删缓存后写库 | 并发读回填旧值 | 时序错 | 复现：并发读写 | 先写库后删 + 版本号 |
| 3 | 无版本号 | 旧值覆盖新值 | 乱序 | 看缓存 version 与库不一致 | Lua 比较版本 |
| 4 | ES 深分页/全量重建慢 | 重建要 2 小时 | 单线程 bulk | 看 bulk 速率 | 并行 + 别名切换 |
| 5 | CDC 位点丢失 | 一段时间的数据没同步 | connector 重启丢 offset | 对比 binlog 位点 | offset 存 Kafka + 监控延迟 |
| 6 | 大事务导致 CDC 延迟 | 同步延迟几分钟 | 单事务改 10 万行 | 看 binlog 事件大小 | 拆批 + 限速 |
| 7 | 缓存 TTL 太长 | 旧值窗口大 | 10 分钟 TTL | 算「最多旧多久」 | 缩短 + 主动失效 |
| 8 | 双写重试不幂等 | 计数翻倍 | 无幂等 | 看计数与事件数 | 幂等键（第 2 章） |
| 9 | 向量与原文脱节 | 答已删除内容 | 删除未级联 | 跑孤儿 chunk 对账 | 统一删除服务 |
| 10 | 重建期间无降级 | 重建时全站慢 | 重建抢资源 | 压测重建 | 限速 + 读降级到 DB |

---

## 39.8 怎么验证：三类测试 + 四个指标

### 测试一：并发读写一致性（可跑）

```go
// 100 个写 + 100 个读并发，最后校验：缓存值 == 库值
func TestCacheConsistency(t *testing.T) {
    var wg sync.WaitGroup
    for i := 0; i < 100; i++ {
        wg.Add(2)
        go func(v int64) {           // 写
            defer wg.Done()
            _, err := svc.UpdatePrice(ctx, 1, v, currentVersion())
            require.NoError(t, err)
        }(int64(i))
        go func() {                  // 读
            defer wg.Done()
            _, _ = svc.Get(ctx, 1)
        }()
    }
    wg.Wait()
    // 最终一致性：给 CDC 2 秒
    time.Sleep(2 * time.Second)
    dbVal := readDB(t, 1)
    cacheVal := readCache(t, 1)
    require.Equal(t, dbVal.PriceCents, cacheVal.PriceCents)
    require.Equal(t, dbVal.Version, cacheVal.Version)
}
```

### 测试二：故障注入（关键）

| 注入 | 期望行为 |
| --- | --- |
| Redis 不可用 | 读降级到 DB，写不失败（缓存删除进重试） |
| ES 不可用 | 搜索降级/返回提示，详情不受影响 |
| CDC 停止 5 分钟 | 有延迟告警；恢复后自动追平 |
| 消费端重复消费 | 版本号/幂等挡住，不产生脏数据 |

### 测试三：重建演练（季度）

```bash
# 重建 ES 索引：别名切换，零停机
curl -X PUT "es:9200/products_v2" -H 'Content-Type: application/json' -d @mapping.json
# 全量灌数据（限速，避免打垮 DB）
python reindex.py --src mysql --dst es:9200/products_v2 --rate 2000/s
# 校验数量与抽样
python verify.py --index products_v2 --sample 1000
# 切别名
curl -X POST "es:9200/_aliases" -H 'Content-Type: application/json' -d '{
  "actions":[
    {"remove":{"index":"products_v1","alias":"products"}},
    {"add":{"index":"products_v2","alias":"products"}}
  ]}'
```

### 四个必看指标

| 指标 | 采集 | 告警 |
| --- | --- | --- |
| `cdc_lag_seconds` | Debezium JMX / Kafka lag | > 5s |
| `cache_invalidate_fail_total` | 应用计数 | > 10/min |
| `reconcile_mismatch_ratio` | 对账任务 | > 0.1% |
| `derived_rebuild_duration` | 重建任务 | > 30min（说明重建不可用） |

---

## 39.9 用什么工具

| 需求 | 工具 | 说明 |
| --- | --- | --- |
| CDC | Debezium（Kafka Connect） | 最主流；支持 MySQL/PG/Mongo |
| CDC（阿里系） | Canal | 轻量，MySQL 专用 |
| Outbox 投递 | 自研轮询 / Debezium EventRouter | 轮询简单，CDC 更实时 |
| 缓存客户端 | Jedis/Lettuce（Java）、go-redis | 注意连接池与超时 |
| ES 版本控制 | `version_type=external` | 防乱序的关键 |
| 延迟双删 | 本地延迟队列 / MQ 延迟消息 | 别用 `Thread.sleep` |
| 对账 | 自研任务 + 抽样 SQL | 必须有，别省 |
| 分布式锁 | Redis Redisson / etcd | 重建时防并发 |

---

## 39.10 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 本质 | 多存储不是「同步问题」，是「**权威源 + 不一致窗口 + 兜底**」 |
| 四种形态 | 写一半 / 顺序错 / 缓存脏 / 重试重复 |
| 等级 | L0 强一致 · L1 读己之写 · L2 有界不一致 · L3 最终 · L4 可能不一致；**派生存储最多 L2** |
| 标准姿势 | 先写库 → 事务提交后删缓存（不是更新）→ 失败进重试 |
| 延迟双删 | 治标，窗口靠版本号兜底；500ms 是拍的，主从延迟可能更长 |
| 版本号 | **最被低估的一招**：Lua 比较版本 + ES external version，一次解决乱序与旧值回填 |
| Outbox+CDC | 重要数据的首选：与业务同事务，天然原子 |
| 对账 | 抽样比对「同版本不同值」；孤儿 chunk 对账 |
| 权威源 | 必须写进文档：谁为准、各派生存储等级、重建方式、不一致处理 |
| Agent | 删除要级联到 chunk/向量/语义缓存；换 embedding 模型要全量重建 + 双写灰度 |
| 一句话 | **缓存可以旧，但不能「不知道它旧」；一致性可以最终，但必须有界且可发现。** |

## 39.11 自检题

1. **为什么是「删缓存」而不是「更新缓存」？给出一个更新缓存会出错的时序。**
   > 要点：两个并发写 A（新值 2）、B（旧值 1，晚到）：若都更新缓存，可能 A 先写 2、B 后写 1 → 缓存永久为旧值。删缓存则下次读从库重建，天然以库为准。若必须更新，必须带版本号比较（只有更新的版本才写）。

2. **主从延迟 800ms，你用「删缓存 + 500ms 延迟双删」，会怎样？**
   > 要点：第二次删除发生在 500ms，此时从库可能仍是旧值；读请求在 500~800ms 之间回填旧值，缓存又脏。正解：延迟时间要大于主从延迟 P99（并用监控验证）；更根本的是「读时带版本比较写回」或「写后读走主库」。延迟双删只能降低概率，不能消除。

3. **Agent 知识库删了一篇文档，为什么必须做「级联删除」，漏了会怎样？**
   > 要点：文档被切成 N 个 chunk，每个 chunk 一条向量 + 可能被语义缓存引用。只删原文档 → 向量仍在 → 检索仍召回 → 模型仍据已删除内容作答，构成隐私/合规事故（用户行使删除权后仍可被检索到）。正解：统一删除服务，按 `doc_id` 级联删 chunk/向量/缓存，并用孤儿对账 SQL 定期检查。

## 39.12 延伸阅读

- 上一章：[38. 图数据库 vs KV](38-graph-vs-kv.md)
- 下一章：[40. 容量、冷热分层与成本](40-capacity-tiering-cost.md)
- 相关：[10. Outbox 与事务消息](10-outbox-and-transactional-message.md)、[14. 对账体系](14-reconciliation-system.md)、[37. NoSQL 选型](37-nosql-selection.md)
- 平台侧：[../llm/24-cache-hierarchy.md](../llm/24-cache-hierarchy.md)（四层缓存与语义缓存）、[../llm/45-storage-architecture.md](../llm/45-storage-architecture.md)（平台存储架构）
- 官方文档（以版本为准）：Debezium MySQL Connector 的 offset/GTID 语义与 EventRouter SMT；Elasticsearch `version_type=external`；Redis `EVAL` 与 Lua 原子性边界
- 总纲：[0-summary.md](0-summary.md)
