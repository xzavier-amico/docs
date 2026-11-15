# 37. NoSQL 选型：Redis / Mongo / Cassandra / ES / ClickHouse

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 存储选型：状态该放哪 ｜ **编号**：37 / 52
> **本章定位**：这五个产品不是「MySQL 的替代品」，而是五种**不同的数据模型 + 不同的失败模式**。这一章逐个给数据模型、适用场景、以及「用了会后悔」的真实事故，最后用一张匹配表和一张反匹配表收口。
> **读法**：§37.3 的缓存边界先看，它决定了你能不能把 Redis 当存储；§37.5 的成本表用于说服老板；§37.7 的双修对照是 Agent 团队最关心的向量库与语义缓存。

---

## 37.1 引子：三次「把派生存储当权威源」

**事故一：Redis 当订单库，切换丢 40 分钟（2024-08，某电商）**

待支付订单只写 Redis，靠 AOF `everysec` 持久化。主库磁盘故障，切换到一台**复制落后 40 分钟**的从库——AOF 理论最多丢 1 秒，但异步复制 + 故障切换把「1 秒」放大成「40 分钟」。丢单 **2.3 万笔**，已支付未发货的赔付 **31 万元**。

**事故二：ES 当订单查询库，重复支付 380 笔（2024-12，某 SaaS）**

支付回调写 MySQL 后同步写 ES，前端「支付成功页」从 ES 读订单状态。ES 默认 1 秒 refresh，用户在这 1 秒内刷新看到「待支付」，又点了一次支付。**380 笔重复支付**，退款 + 手续费损失 **12 万元**。

**事故三：Mongo 跨文档转账没开事务（2025-01，某 fintech）**

余额拆成两个文档，转账用两次 `updateOne`，没包 session。A 扣了 100 万，进程被 kill，B 没加。日终对账才发现，缺口 **18.6 万元**，监管报送迟了 3 天。

> 🩸 三次都不是「NoSQL 不行」，是**把派生存储、近实时索引、非事务模型当成了权威源**。选型的第一句话应该是：**谁是真相，谁是派生。**

---

## 37.2 到底是什么：缓存、KV 存储、数据库，是三件事

| 能力 | Redis 当缓存 | Redis 当 KV 存储 | 关系数据库 |
| --- | --- | --- | --- |
| 数据丢了能否重建 | 能（回源） | **不能**（除非另有权威源） | 不能 |
| 持久化保证 | 不需要 | 单机尽量少丢，切换可能丢 | WAL + 副本，RPO 可做到 0 |
| 查询能力 | 只需 key | 只需 key | SQL、join、聚合 |
| 约束/事务 | 不需要 | 无外键、无回滚 | 完整 |
| 成本 | 内存价，但只存热数据 | 全量内存价，最贵 | SSD 价 |
| 扩容 | 加节点/分片 | 16384 slot 集群，扩缩容要迁移 | 分片/分布式库 |

**四个必须钉死的反例**：

| 你以为的 | 真相 | 不做会怎样 |
| --- | --- | --- |
| 「Redis 有 RDB/AOF，就是数据库」 | 它保证的是**单机重启少丢**，不是**故障切换不丢**；没有约束、没有查询、内存贵 | 一次主从切换丢几十分钟数据 |
| 「Mongo 无 schema，什么都能存」 | 无 schema ≠ 无约束；跨文档事务有 60 秒上限、16MB oplog 限制 | 账务写一半，对账才发现 |
| 「ES 搜得快，当库用」 | 近实时（默认 1s）、无跨文档事务、更新是「标记删除 + 重建索引」 | 写完读不到，用户重复操作 |
| 「ClickHouse 快，点查也行」 | 列存 + 稀疏索引 + 后台 merge，适合扫百万行，不适合取一行 | 单条点查 P99 2.8 秒，CPU 打满 |

---

## 37.3 结构图：权威源只有一个，其余都是派生

```
                    ┌────────────────────┐
   业务写 ────────► │ MySQL / PG 权威源   │
                    └─────────┬──────────┘
                              │ binlog / Outbox / CDC（第 10、28 章）
          ┌───────────┬───────┼─────────┬──────────────┐
          ▼           ▼       ▼         ▼              ▼
      Redis        ES      ClickHouse  向量库      对象存储
    （缓存/会话）（检索）  （分析）   （语义召回） （原文/大对象）
          │           │       │         │              │
          └─ 读路径：先缓存 → miss 回源 → 异步回填；ES/CH/向量只回答查询，不判定写成功
```

**三条铁律**：

1. **写成功只由权威源判定**。ES 写成功、Redis 写成功都不算业务成功。
2. **派生存储可重建**。重建脚本必须存在，且每季度跑一次（第 40 章）。
3. **读路径允许降级**。ES 挂了可以回源模糊查；Redis 挂了可以直查库；但权威源不能挂。

---

## 37.4 五个产品：模型、适用、后悔场景

### 37.4.1 Redis：内存数据结构服务器

**模型**：key → string/hash/list/set/zset/stream，单线程执行命令（6.0 起网络 IO 多线程），Cluster 16384 slot，主从异步复制。

**适用**：缓存、会话、计数器、排行榜、分布式锁、简单去重、限流（第 16 章）。

**用了会后悔**：

- **当唯一存储**：见事故一。RDB 默认 `save 900 1 / 300 10 / 60 10000`，AOF `everysec` 正常最多丢 1 秒，但**主从异步复制 + 切换**可能丢几十分钟。
- **大 key**：一个 200MB 的 hash，`HGETALL` 阻塞主线程 1.2 秒，全站 P99 抖动；`DEL` 大 key 同样阻塞，要用 `UNLINK`。
- **当可靠队列**：List 做队列没有 ack/重投/DLQ，消费端崩了消息就丢；要可靠用 Kafka/RocketMQ（第 23 章）。
- **`KEYS *`**：O(N) 扫全库，生产禁用，用 `SCAN`。

### 37.4.2 MongoDB：文档数据库

**模型**：BSON 文档（单文档上限 16MB），集合，WiredTiger 行级并发，B-tree 索引；4.0 起副本集支持多文档事务，4.2 起分片集群支持。

**适用**：内容/商品详情、用户画像、目录、工单、中等规模日志——**以聚合为单位读写**。

**用了会后悔**：

- **强事务账务**：事务默认 60 秒超时（`transactionLifetimeLimitSeconds`），oplog 单条 16MB；跨分片事务更慢。见事故三。
- **`$lookup` 做 join/递归**：没有索引的 `$lookup` 是集合级扫描，四层递归能把 CPU 打满（第 35 章事故二）。
- **`skip` 深分页**：`skip(100000)` 是逐条丢弃，O(skip)；用 `_id`/排序键游标。
- **无索引的大集合**：一个 2,000 万文档的集合，每次查询 `totalDocsExamined` 等于全表。

### 37.4.3 Cassandra：宽列存储

**模型**：`PRIMARY KEY ((partition_key), clustering_key)`，LSM-Tree（memtable → SSTable），commitlog，最终一致可调（ONE/QUORUM/ALL），无 join。

**适用**：时序、消息、审计、IoT、海量写入、多地域——**查询先设计，再建表**。

**用了会后悔**：

- **分区过大**：单分区建议 < 100MB / < 10 万行；一个设备一天 1 秒一条 = 86,400 行，接近上限，必须按小时分桶。
- **需要聚合/二级索引**：`ALLOW FILTERING` 是反模式，会扫全节点。
- **频繁更新同一行**：每次更新产生 tombstone，`gc_grace_seconds` 默认 10 天，删除量大时读 P99 从 20ms 涨到 5s。
- **要事务**：LWT（`IF NOT EXISTS`）是 Paxos，性能差一个数量级，别拿它做账务。

```cql
-- query-first：一个设备一小时一个分区，按时间倒序，TTL 90 天
CREATE TABLE device_metric (
  device_id  uuid,
  bucket     timestamp,          -- 按小时截断
  ts         timestamp,
  metric     text,
  value      double,
  PRIMARY KEY ((device_id, bucket), ts, metric)
) WITH CLUSTERING ORDER BY (ts DESC, metric ASC)
  AND compaction = {
    'class': 'TimeWindowCompactionStrategy',
    'compaction_window_size': '1',
    'compaction_window_unit': 'DAYS'
  }
  AND gc_grace_seconds = 864000
  AND default_time_to_live = 7776000;   -- 90 天
```

### 37.4.4 Elasticsearch：搜索与分析引擎

**模型**：倒排索引（text）+ doc_values（keyword/numeric/date），近实时（默认 `refresh_interval: 1s`），无跨文档事务，`from+size` 默认上限 10,000。

**适用**：全文检索、日志、多维筛选、中等规模聚合——**永远是派生索引**。

**用了会后悔**：

- **当主库**：见事故二。写 ES 成功不等于业务成功，refresh 前读不到。
- **频繁更新**：每次 update 是「标记删除 + 写新 doc」，段合并压力大；10 万 QPS 的计数类更新会把集群打挂。
- **深分页**：`from=100000` 每片都要构造 100020 个 doc 的优先队列，堆内存爆炸；用 `search_after` + PIT。
- **mapping 爆炸**：动态 mapping 遇到 10 万个不同字段名的 JSON，集群元数据撑爆；生产必须 `dynamic: strict`。

### 37.4.5 ClickHouse：列式分析库

**模型**：MergeTree 家族，列存 + 稀疏主键索引（`index_granularity` 默认 8192 行一个 granule），写入生成 part，后台 merge；`UPDATE/DELETE` 是异步 mutation。

**适用**：OLAP、日志、指标、大屏、宽表聚合、亿级 `GROUP BY`。

**用了会后悔**：

- **点查**：按主键取一行要扫一个 granule（8192 行），还要跨 part；客服查单 P99 2.8 秒，不如 MySQL 0.3ms。
- **高频单条 insert**：每次 insert 生成一个 part，part 多了报 `Too many parts`；必须批量（建议 ≥ 1,000 行，最好 1 万~10 万）。
- **高频更新/删除**：mutation 重写整个 part，别拿它当 OLTP。
- **高并发小查询**：单机建议几十到一两百 QPS 的分析查询，不要拿它扛 5,000 QPS 的接口。

```sql
-- 列存宽表：按月分区、按用户+时间排序、24 个月 TTL
CREATE TABLE order_wide (
  order_id   UInt64,
  user_id    UInt64,
  status     UInt8,
  amount     Decimal(18,2),
  created_at DateTime,
  dt         Date MATERIALIZED toDate(created_at)
) ENGINE = MergeTree
PARTITION BY toYYYYMM(dt)
ORDER BY (user_id, created_at, order_id)
TTL dt + INTERVAL 24 MONTH
SETTINGS index_granularity = 8192;

-- 写入必须批量；单条 INSERT 是事故
INSERT INTO order_wide (order_id, user_id, status, amount, created_at)
SELECT order_id, user_id, status, amount, created_at
FROM mysql('10.0.0.12:3306', 'order_db', 't_order', 'reader', '***')
WHERE created_at >= now() - INTERVAL 1 HOUR;
```

---

## 37.5 缓存与 KV 存储的边界：Redis 持久化到底保证什么

| 配置 | 触发时机 | 单机重启最多丢 | 恢复速度 | 坑 |
| --- | --- | --- | --- | --- |
| 无持久化 | — | 全丢 | — | 重启即空，缓存场景可接受 |
| RDB | `save 900 1 / 300 10 / 60 10000` | 上次快照以来的全部写（可能几十分钟） | 快 | fork + COW，内存可能翻倍 |
| AOF `everysec` | 每秒 fsync | 正常 ≤ 1 秒 | 慢（重放） | 磁盘/OS 抖动时可能到 2 秒 |
| AOF `always` | 每条命令 fsync | 理论 0（单机） | 最慢 | 吞吐掉一个数量级 |
| RDB + AOF 混合 | 4.0+ 默认 | 同 AOF | 中 | 重写期间 IO 高 |
| **主从异步复制** | 持续 | **未同步到从库的部分** | — | 故障切换丢数据，可能远超 1 秒 |

**结论**：Redis 的持久化只承诺「单机重启尽量少丢」，**不承诺「故障切换不丢」**。要把它当 KV 存储，必须先回答：这份数据丢了能不能从别处重建？不能，就别放 Redis。

**缓存旁路的正确写法**（版本号防旧值回填）：

```lua
-- KEYS[1] = order:10086  ARGV = version, payload, ttl_seconds
-- 只有新版本 >= 旧版本才写入，防止并发回填把旧值盖回去
local old = tonumber(redis.call('HGET', KEYS[1], 'version') or '0')
local new = tonumber(ARGV[1])
if new < old then
  return 0
end
redis.call('HSET', KEYS[1], 'version', ARGV[1], 'payload', ARGV[2])
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
```

```go
// 缓存击穿：同一 key 只放一个请求回源
var group singleflight.Group

func GetOrder(ctx context.Context, id int64) (*Order, error) {
	key := fmt.Sprintf("order:%d", id)
	if v, ok := cache.Get(key); ok {
		return v.(*Order), nil
	}
	v, err, _ := group.Do(key, func() (any, error) {
		o, err := repo.Find(ctx, id)
		if err != nil {
			return nil, err
		}
		cache.SetWithVersion(key, o.Version, o, 30*time.Second)
		return o, nil
	})
	if err != nil {
		return nil, err
	}
	return v.(*Order), nil
}
```

---

## 37.6 成本对比：内存是 SSD 的 40~80 倍

> 以下为量级参考（2026 年国内某云公开价折算），实际以账单为准。

| 产品 | 存储单价（元/GB/月） | 请求成本 | 运维人力 | 经济的数据量 | 成本杀手 |
| --- | --- | --- | --- | --- | --- |
| MySQL（ESSD，三副本） | 0.7~1.2 | 包含在规格内 | 0.3 人 | TB 级 | 大字段、无索引扫描 |
| Redis（内存型主从） | **40~80** | 按规格，不按请求 | 0.2 人 | 10~100GB 热数据 | 全量数据、大 key、无 TTL |
| MongoDB | 2~5 | 按规格 | 0.5 人 | 百 GB~TB | 无索引、大文档、副本 |
| Cassandra（自建） | 0.5~1.5（3 副本） | 按节点 | 1 人 | 十 TB~PB | 分区过大、compaction |
| Elasticsearch | 1.5~4（热节点） | 按节点 | 0.5 人 | 百 GB~十 TB | 副本数、频繁更新、深分页 |
| ClickHouse（自建） | 0.5~1.5（压缩后） | 按节点 | 0.5 人 | 十 TB~PB | 小批量写、高并发点查 |
| 对象存储 OSS | 0.12 标准 / 0.08 低频 / 0.033 归档 | 按请求+流量 | 0.05 人 | PB 级 | 频繁读取归档、外网流量 |

**算一笔**：100GB 热数据放 Redis ≈ **6,000 元/月**；同样数据放 MySQL ≈ **100 元/月**。所以 Redis 里只配放「访问频率 × 价值」最高的那 5%~10%。

---

## 37.7 场景-产品匹配表与反匹配表

### 匹配表

| 场景 | 首选 | 理由 | 备注 |
| --- | --- | --- | --- |
| 交易/账务/库存 | MySQL / PG | 事务 + 约束 + 可对账 | 分片见第 36 章 |
| 会话/验证码/计数器 | Redis | TTL + 原子自增 | 丢了可重建 |
| 商品详情/内容 | MySQL + JSON / MongoDB | 聚合读写 | 强事务部分留 MySQL |
| 站内搜索/日志检索 | Elasticsearch | 倒排 + 相关性 | 派生索引 |
| 多维运营筛选 | ES / ClickHouse | 任意维度组合 | 秒级延迟可接受 |
| 亿级聚合/大屏 | ClickHouse | 列存 + 向量化 | 批量写 |
| 海量写入/多地域 | Cassandra | LSM + 可调一致 | query-first |
| 权限/血缘/多跳 | 图库 | 免索引邻接 | 见第 38 章 |
| 语义检索 | 向量库 / pgvector | ANN | 见第 39 章一致性 |
| 图片/PDF/模型权重 | 对象存储 | 便宜 + 直传 | 数据库只存 URI |

### 反匹配表

| 场景 | 常见错选 | 会发生什么 | 正确做法 |
| --- | --- | --- | --- |
| 订单唯一存储 | Redis | 切换丢单，无法对账 | MySQL 权威 + Redis 缓存 |
| 资金流水 | MongoDB 无事务 | 扣了没加，日终才发现 | MySQL 事务 / Mongo 多文档事务 + 对账 |
| 全文搜索主库 | ES | refresh 前读不到，更新贵 | MySQL 为准，ES 派生 |
| 客服查单 | ClickHouse | 点查 2~3 秒，CPU 打满 | MySQL 点查 + CH 只做报表 |
| 任意维度查询 | MySQL 加 10 个索引 | 写放大、优化器选错 | ES / CH |
| 高基数标签 | 时序库 tag | 内存 OOM | 高基数字段改 field 或拆表 |
| 可靠消息 | Redis List | 无 ack，消费端崩了丢消息 | Kafka / RocketMQ |

---

## 37.8 双修对照：多了一个「近似」维度

| 维度 | 传统系统 | Agent 系统 | 同 or 异 |
| --- | --- | --- | --- |
| **机制本体** | 缓存/搜索/列存/宽列 | 加：向量库、语义缓存、prompt 存储 | 产品集扩展，原理相同 |
| **唯一键** | `order_id` / `sku_id` | `chunk_id` / `content_hash` / `embedding_version` | 结构相同，键要带模型版本 |
| **失败模式** | 缓存脏、ES 延迟、CH 点查慢 | 加：**假命中**（相似度阈值误判）、向量与原文不一致 | 多一类「近似错误」 |
| **恢复方式** | 回源重建 | 原文重放 → 重新切分 → 重新 embedding | 相同，但重建成本高 |
| **验证方法** | key 数 vs 行数、doc count | 加：召回率、引用命中率、向量条数 | 指标扩展 |
| **成本结构** | 内存/SSD/对象 | 向量吃内存，prompt 吃带宽 | 结构不同 |

**完全相同的部分**：缓存旁路、TTL、空值缓存、singleflight、版本号、权威源原则、成本算法——一套代码两边用。

**Agent 特有的三个点**：

1. **向量库不是「更快的 ES」**。它是 ANN 近似索引，召回率 95%~99%，且**删除/更新是软删除 + 后台 compaction**。选型时必须问：删除后多久不可召回？过滤条件多不多（pre-filter vs post-filter）？
2. **语义缓存的命中判定从「key 相等」变成「相似度 > 阈值」**。某客服 Agent 阈值设 0.92，把「退款政策」和「退货政策」判成同一问题，错误回答 **2,300 次**，客诉 47 起。解法：阈值 + 意图分类前置 + 只缓存事实型问答 + 缓存 key 带 prompt 版本。
3. **prompt/会话存储的成本结构不同**：会话是热数据（Redis，TTL 30 分钟），prompt 是温数据（MySQL 存元数据 + 对象存储存正文），事件是冷数据（分区表/列存）。

---

## 37.9 会在哪炸：失效模式清单

| 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| Redis P99 抖动到 1s | 大 key / `KEYS` / `DEL` 阻塞主线程 | `SLOWLOG GET 10`、`--bigkeys` | `UNLINK`、拆 key、禁用 `KEYS` | 大 key 巡检 + 命令黑名单 |
| 缓存雪崩 | 同一时刻大量 key 过期 | 过期时间分布、DB QPS 曲线 | TTL 加随机抖动、多级缓存 | 缓存预热 + 熔断降级 |
| ES 写入成功但查不到 | refresh 延迟 / 副本未分配 | `_refresh`、`_cluster/health` | 关键读加 `refresh=wait_for` 或读主库 | 写成功只认权威源 |
| ES 集群变红 | 磁盘水位 / mapping 爆炸 | `_cluster/allocation/explain` | 加节点、删索引、`dynamic: strict` | 索引模板 + 字段数上限 |
| CH `Too many parts` | 高频单条 insert | `system.parts` 按表统计 | 批量写、合并小 part | 客户端攒批 ≥ 1,000 行 |
| Cassandra 读超时 | 分区过大 / tombstone 堆积 | `nodetool tablestats`、`tpstats` | 重新分桶、`compaction` | 分区 < 100MB 规范 |
| Mongo 事务超时 | 事务超过 60 秒 / 锁冲突 | `currentOp`、慢事务日志 | 拆小事务、缩短持有时间 | 事务内不做外部调用 |

---

## 37.10 怎么验证：压测 + 故障注入 + 对账

```bash
# Redis：看真实命令 mix 下的 P99，不是只压 SET/GET
redis-benchmark -h 10.0.0.21 -p 6379 -n 1000000 -c 200 \
  -r 100000 -d 512 -t set,get -q

# Elasticsearch：官方 rally 跑真实查询集
esrally race --track=geonames --target-hosts=10.0.0.31:9200 \
  --pipeline=benchmark-only --challenge=append-no-conflicts

# ClickHouse：并发点查 vs 聚合，两种负载分开压
clickhouse-benchmark -h 10.0.0.41 -c 50 -q 'SELECT count() FROM order_wide WHERE dt = today()'
```

**故障注入**：kill Redis 主节点看切换丢多少 key；断 ES 一个节点看查询降级；把 CH 磁盘打满看写入行为。**对账**：Redis key 数 vs 权威源行数（允许缓存未命中差异）、ES doc count vs MySQL 行数（允许秒级延迟）、CH 分区行数 vs 源表。

---

## 37.11 用什么工具

| 需求 | 推荐 | 说明 |
| --- | --- | --- |
| 缓存 | Redis 7.x / 8.x | 8.0 起可选 AGPLv3；集群模式 16384 slot |
| 文档 | MongoDB 7.x / 8.x | 事务必须显式 session；生产开 majority write concern |
| 宽列 | Cassandra 4.x / 5.x | 5.0 起支持向量类型与 SAI 向量索引 |
| 搜索 | Elasticsearch 8.x / OpenSearch | 生产必须 `dynamic: strict` + 索引模板 |
| 分析 | ClickHouse 24.x+ | MergeTree 家族；`ReplacingMergeTree` 做最终去重 |
| 向量 | Milvus / Qdrant / pgvector | 千万级以内优先 pgvector，省一个组件 |
| 不推荐 | 自研缓存/搜索 | 备份、扩缩容、故障恢复的成本远超预期 |

---

## 37.12 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 权威源 | 只有一个；Redis/ES/CH/向量都是派生，写成功只认权威源 |
| Redis 持久化 | 保证单机重启少丢，不保证故障切换不丢；当存储前先问「能否重建」 |
| Redis 成本 | 内存是 SSD 的 40~80 倍，只放 5%~10% 热数据 |
| Mongo | 以聚合为单位读写；跨文档事务有 60 秒/16MB 限制 |
| Cassandra | query-first；单分区 < 100MB / 10 万行；`ALLOW FILTERING` 是反模式 |
| ES | 近实时 1s、无事务、深分页用 `search_after`；永远不是主库 |
| ClickHouse | 批量写 ≥ 1,000 行；点查与高频更新别用 |
| 缓存三件套 | TTL + 空值缓存 + singleflight |
| 语义缓存 | 命中判定是「相似度 > 阈值」，会假命中；阈值 + 意图前置 + 版本号 |
| 双修 | 产品集扩展，方法论不变；多一类「近似错误」 |

## 37.13 自检题

1. **产品经理说「Redis 有 AOF，为什么不能当订单库？」你怎么用一句话和一个数字回答？**
   > 答案要点：AOF `everysec` 只保证单机重启最多丢约 1 秒；但主从是异步复制，故障切换到落后从库时丢的是「未同步部分」，可能是几十分钟。事故里丢了 2.3 万单、赔 31 万。订单库要的是 RPO≈0 + 可对账，Redis 两样都不给。

2. **一个「按 10 个维度任意组合筛选」的运营后台，你选 ES 还是 ClickHouse？为什么？**
   > 答案要点：看查询形态。ES 擅长「过滤 + 相关性 + top-k」，任意维度组合靠倒排/doc_values，延迟 10~100ms；ClickHouse 擅长「扫大量行做聚合」，任意维度 `GROUP BY` 更快但点查/高并发差。运营后台若是「筛出 20 条看详情」→ ES；若是「按维度出报表/求和」→ ClickHouse；两者都常用，靠 CDC 各喂一份。

3. **语义缓存阈值调到 0.98 就安全了吗？还会怎么错？**
   > 答案要点：阈值高会降低命中率、失去缓存意义；仍会错在：① 同义但答案因用户/租户/版本不同（必须把 tenant、prompt 版本、知识库版本放进 key）；② 否定句/数字/日期敏感问题；③ 时效性问题（政策刚改）。解法：只缓存事实型、加意图分类、TTL 短、命中后仍做引用校验。

## 37.14 延伸阅读

- Redis 文档：*Persistence*（RDB/AOF/everysec 语义）、*Cluster specification*（16384 slots）
- MongoDB 文档：*Transactions*（60 秒 `transactionLifetimeLimitSeconds`）、*Data Modeling*
- Apache Cassandra 文档：*Data Modeling*、*Compaction*、`gc_grace_seconds`
- Elasticsearch 文档：*Near real-time search*、*Paginate search results*（`search_after` / PIT）
- ClickHouse 文档：*MergeTree*、*TTL*、*Selecting an Insert Strategy*
- 上一章：[36. MySQL 还能不能用：极限与分库分表时机](36-mysql-limits-sharding.md)
- 下一章：[38. 图数据库 vs KV：图真的有用吗](38-graph-vs-kv.md)
- 缓存一致性见第 39 章 ｜ 冷热分层与成本见第 40 章
