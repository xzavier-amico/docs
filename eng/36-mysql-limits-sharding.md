# 36. MySQL 还能不能用：极限与分库分表时机

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 存储选型：状态该放哪 ｜ **编号**：36 / 52
> **本章定位**：分库分表不是「容量方案」，是一笔「用运维复杂度换单点写能力」的交易。这一章给你单表/QPS 的真实拐点判据、五种「不该拆」的情况、四种分片键与热点解法，以及什么时候该把钱花在 TiDB 而不是中间件上。
> **读法**：§36.4 的拐点判据先对照自己的库；§36.5 的五条「不该拆」建议直接进架构评审；§36.8 的对比表用于回答老板那句「为什么不直接上 TiDB」。

---

## 36.1 引子：两次「拆了更糟」

**事故一：拆完 32 个库，运营查不了订单（2025-03，某 SaaS）**

订单表 6.8 亿行，按 `user_id` 哈希拆成 32 库 64 表。上线三个月后，客服要「按手机号查订单」——手机号不在分片键里。中间件把查询广播到 64 个分片，每次 64 路并发，单次 P99 **9.4 秒**；客服高峰期把连接池打满，连带下单接口不可用 **27 分钟**，当天工单积压 3,100 张。

**事故二：DDL 跑了 19 小时（2024-12，某电商）**

订单表 4.2 亿行，要加一个 `channel` 字段。用 gh-ost 在线改表，跑了 **19 小时**；期间 binlog 堆积 1.2 TB，从库延迟 4 小时，风控读从库读到的是 4 小时前的数据，拦截规则失效，盗刷损失 **86 万元**。

> 🩸 两次的共同点：**拆与不拆都不是免费的**。第一次是「拆了但没解决查询维度」，第二次是「没拆但没管住 DDL 与复制延迟」。选型要算的是总账，不是单表行数。

---

## 36.2 到底是什么：分库分表是一笔交易

**定义**：分库分表 = 把一张逻辑表按某个**分片键**切到多个物理库/表，使单点的数据量、写 TPS、连接数落到单机可承受范围内。它换来的能力是「水平扩展写」，付出的代价是：

1. **跨片查询**变成 N 路 fan-out + 应用层归并；
2. **跨片事务**消失，只能用 Saga/Outbox；
3. **全局唯一 ID** 要自己造；
4. **运维对象**从 1 个库变成 N 个库（备份、DDL、监控、扩容）；
5. **扩容**要迁移数据，且迁移期间要双写。

先钉死四个误解：

| 你以为的 | 为什么错 | 不做会怎样 |
| --- | --- | --- |
| 「单表 2000 万就必须拆」 | 2000 万只是「1KB 行宽、3 层 B+ 树」的经验值；500B 行宽能撑 4000 万+ | 提前拆，运维成本翻 5 倍，收益为零 |
| 「QPS 高就必须拆」 | 先看是不是慢查询、缺索引、大事务、连接池太小 | 拆完发现瓶颈在一条没索引的 SQL |
| 「拆了就能线性扩」 | 热点 key 还在同一个分片；跨片聚合反而更慢 | 32 个库，一个库扛 40% 流量 |
| 「分片能解决所有问题」 | 它只解决「单机装不下/写不动」，不解决查询维度 | 拆完再补冗余表、ES、数仓，工期翻倍 |

---

## 36.3 结构图：拆之前先走完这张图

```
单表变慢 / 容量告急
  │
  ├─ Q1 慢在哪？
  │    ├─ 某几条 SQL → 加索引 / 改写 / 限流（第 51 章），结束
  │    ├─ 锁等待 / 大事务 → 拆事务 / 降隔离 / 异步化（第 7 章），结束
  │    ├─ 连接池打满 → 加从库 / 连接池调优 / 读写分离，结束
  │    └─ 单表数据量 / 写 TPS 到顶 → Q2
  │
  ├─ Q2 三年内能不能靠「升配 + 归档 + 分区 + 列存」撑住？
  │    ├─ 能 → 不拆（见 §36.5 五条判据）
  │    └─ 不能 → Q3
  │
  ├─ Q3 业务能否接受跨片查询/跨片事务的改造？
  │    ├─ 不能 → 上分布式数据库（TiDB/OceanBase，§36.8）
  │    └─ 能 → Q4
  │
  └─ Q4 选分片键（用户/订单/时间/一致性哈希）→ 预留热点方案 → 双写迁移
```

拆分后的典型拓扑：

```
                    ┌─────────────────────────────┐
   写请求 ─────────►│ 路由层 shard = hash(key) % N │
                    └──────┬──────────────────────┘
        ┌──────────┬───────┼────────┬──────────┐
        ▼          ▼       ▼        ▼          ▼
     db_00      db_01   db_02     ...       db_63
   t_order_0  t_order_1 ...                t_order_63
        │
        ├─ 每库 1 主 2 从；同库内 order / order_item 同分片 → 本地事务
        ├─ 跨片查询：冗余表（user_id → order_no）或 ES 宽表
        └─ 禁止 64 路 fan-out 深分页；翻页走游标 + 归并
```

---

## 36.4 拐点：单表多大、QPS 多少

### 36.4.1 单表行数：先算 B+ 树，再谈经验值

InnoDB 页 16KB。非叶子节点一条索引项约 14B（8B 主键 + 6B 页指针），一页约 **1,170** 条；叶子页装行。

```
3 层 B+ 树容量 ≈ 1170 × 1170 × (16KB / 行宽)

行宽 500B → 每叶 32 行 → 1170² × 32 ≈ 4,380 万行
行宽 1KB  → 每叶 16 行 → 1170² × 16 ≈ 2,190 万行
行宽 4KB  → 每叶  4 行 → 1170² ×  4 ≈  547 万行
```

所以「2000 万」不是定律，是**行宽 1KB** 时的 3 层树容量。真正该看的不是行数，而是下面五个信号：

| 信号 | 经验阈值 | 怎么测 | 超了怎么办 |
| --- | --- | --- | --- |
| buffer pool 命中率 | 持续 < 99% | `1 - Innodb_buffer_pool_reads/read_requests` | 加内存 / 归档冷数据 / 分区 |
| 单表物理大小 | > 300~500 GB | `information_schema.tables` 的 `data_length+index_length` | 归档 / 分区 / 拆表 |
| DDL 时长 | > 业务低峰窗口 | gh-ost/pt-osc 试跑 | 拆表 / 用 INSTANT 算法 |
| 备份+恢复时长 | > RTO | 每季度恢复演练（第 40 章） | 拆库 / 物理备份 / 增量 |
| 单表写 TPS | > 单机安全值（见下） | `Com_*` + `SHOW ENGINE INNODB STATUS` | 分片 / 换分布式库 |

### 36.4.2 QPS：算分片数，不要拍脑袋

单机 32C64G + NVMe，`sync_binlog=1`、`innodb_flush_log_at_trx_commit=1` 的保守值：

- 主键点查：**3 万~6 万 QPS**（buffer pool 全命中）；
- 单行写事务：**3,000~8,000 TPS**（每次 commit 至少一次 fsync，组提交能抬高上限）；
- 带二级索引的范围查：**5,000~15,000 QPS**，取决于返回行数。

分片数算式：

```
分片数 = ceil( 峰值写 TPS × 安全系数 1.5 / 单库安全写 TPS )
例：峰值 24,000 TPS × 1.5 / 5,000 = 7.2 → 取 8 或 16（2 的幂，便于路由与扩容）
```

> ⚠️ 读写分离不是分片。读 QPS 高先加从库 + 缓存；写 TPS 高才需要分片。把读流量算进分片数，是最常见的过度设计。

### 36.4.3 什么时候不该拆：五条判据

| # | 判据 | 说明 |
| --- | --- | --- |
| 1 | 三年预测 < 1 亿行，且热数据能装进 buffer pool | 加内存比加中间件便宜一个数量级 |
| 2 | 慢在 SQL / 锁 / 大事务，不在容量 | 拆了这些 SQL 照样慢，还多一层路由 |
| 3 | 团队能值班的 DBA/中间件人 < 3 | 分片后备份、DDL、扩容、路由排障都要人 |
| 4 | 核心链路需要跨维度强事务 | 账务、库存对账跨片后只能 Saga + 对账 |
| 5 | 有更便宜的招能撑 18 个月 | 归档、分区、读写分离、列存、加内存、换 NVMe |

**算一笔总账**（某 6 亿行订单库，真实量级）：

| 方案 | 一次性投入 | 年运维成本 | 18 个月后的天花板 |
| --- | --- | --- | --- |
| 升配 64C256G + 归档 2 年冷数据 | 8 万 | 12 万（机器+备份） | 约 12 亿行，够用 3 年 |
| 分 16 库 64 表 | 6 人月 ≈ 60 万 | 2 名运维 80 万 + 机器 30 万 | 理论上无限，但热点仍在 |
| TiDB 三副本 | 迁移 4 人月 ≈ 40 万 | 机器 45 万 + 平台 0.5 人 20 万 | 线性扩，但点查延迟 +1~2ms |

> 💡 **结论**：**只有当「不拆就真的写不动/装不下」且「跨片改造业务能接受」同时成立，才拆。** 否则先升配 + 归档，把 18 个月买回来。

---

## 36.5 四种分片键与热点

| 分片键 | 分布均匀性 | 跨片查询 | 热点 | 扩容 | 典型场景 | 最大的坑 |
| --- | --- | --- | --- | --- | --- | --- |
| **用户 ID** | 好（大 V 除外） | 按用户查最顺；按商家/商品要冗余表 | 头部用户 | rehash，需迁移 | C 端订单、账户、购物车 | 大 V 一个用户打满一片 |
| **订单 ID** | 好（雪花含时间） | 按订单查 O(1)；按用户查要冗余 | 新订单集中（范围分片时） | 需重算 | 订单主表、支付流水 | 业务查询几乎都不带订单号 |
| **时间** | 按天/月均匀 | 范围查最快；点查要带时间 | **当前分片是唯一热点** | 加新表即可 | 日志、流水、事件、账单 | 写入永远打在最新一片 |
| **一致性哈希** | 好 | 范围查差 | 热点仍在 | 只迁移 1/N | 缓存节点、对象、租户 | 节点少时分布不均 |

**选键三原则**：① 查询**必带**；② 基数高；③ 分布均匀。一张表只能有一个分片键，其他查询维度靠**冗余表**（异构索引）解决。

### 热点案例：头部商家占 18% 订单

按 `merchant_id` 哈希分 64 片，某头部商家占全站 **18%** 订单，其所在分片 CPU 长期 **92%**，其余分片 30%。解法：

| 手段 | 做法 | 代价 | 适用 |
| --- | --- | --- | --- |
| **加盐** | 订单表按 `order_id % 10` 再散成 10 张子表，写入分散 | 查询要聚合 10 张表 | 写热点，读少 |
| **二次分片** | 热点商家单独一组库，路由表标记 | 多一套路由与运维 | 热点稳定且可识别 |
| **冷热分离** | 热 30 天留 MySQL，冷数据进 ES/ClickHouse | 跨存储查询 | 读多写少 |
| **请求合并** | 同商家写请求批量提交 | 延迟上升 | 计数/库存类 |

```go
package shard

import (
	"fmt"
	"hash/fnv"
)

// 普通路由：同一商家落同一片，保证本地事务
func ShardOf(merchantID int64, shards int) int {
	h := fnv.New32a()
	fmt.Fprintf(h, "%d", merchantID)
	return int(h.Sum32() % uint32(shards))
}

// 热点商家：订单再按 orderID 加盐散到 salts 张子表；查询时 fan-out 后归并
func TableOf(merchantID, orderID int64, shards, salts int) string {
	base := ShardOf(merchantID, shards)
	if isHotMerchant(merchantID) {
		return fmt.Sprintf("db_%02d.t_order_%02d_s%02d", base, base, orderID%int64(salts))
	}
	return fmt.Sprintf("db_%02d.t_order_%02d", base, base)
}

func isHotMerchant(id int64) bool { return id == 90001 } // 实际查配置中心/路由表
```

---

## 36.6 跨分片查询、分页与聚合的代价

| 方案 | 原理 | 延迟 | 一致性 | 适用 |
| --- | --- | --- | --- | --- |
| **冗余表（异构索引）** | 按第二维度再建一张表，同事务双写 | 单分片，低 | 强（同库）或最终（跨库） | 手机号→订单、商家→订单 |
| **ES 宽表** | binlog/Outbox 同步到 ES | 10~100ms | 最终（秒级） | 多维筛选、运营后台 |
| **内存聚合** | N 路查询后在应用层归并 | N × 单查询 | 最终（读时） | 结果集 < 1 万行 |
| **预聚合表** | 定时/实时汇总到汇总表 | 极低 | 最终（分钟级） | 报表、GMV 大屏 |
| **列存/数仓** | CDC 同步到 ClickHouse | 秒~分钟 | 最终 | 亿级聚合、BI |

**深分页是分片后的第一杀手**。`LIMIT 100000, 20` 在 64 个分片上等于每个分片扫 100020 行再丢弃，内存和 IO 都是 64 倍。正确做法是**游标分页**：

```sql
-- 每个分片执行同一条，应用层做 64 路归并（按 created_at, id 排序取前 20）
SELECT id, order_no, user_id, created_at
FROM t_order_07
WHERE user_id = 10086
  AND (created_at, id) < ('2026-03-01 00:00:00.000', 881234567890)
ORDER BY created_at DESC, id DESC
LIMIT 20;
-- 需要 (user_id, created_at, id) 联合索引，否则每片仍是 filesort
```

**冗余表**的写法（同库同事务，最可靠）：

```sql
CREATE TABLE t_order_by_phone (
  phone      VARCHAR(20)  NOT NULL,
  order_no   CHAR(26)     NOT NULL,
  user_id    BIGINT UNSIGNED NOT NULL,
  created_at DATETIME(3)  NOT NULL,
  PRIMARY KEY (phone, created_at, order_no),
  KEY idx_order_no (order_no)
) ENGINE=InnoDB;

-- 下单事务里同时写主表和冗余表；两张表必须同一个分片键路由（phone 与 user_id 同片）
START TRANSACTION;
INSERT INTO t_order (id, order_no, user_id, phone, status, total_amount, created_at)
VALUES (?, ?, ?, ?, 10, ?, NOW(3));
INSERT INTO t_order_by_phone (phone, order_no, user_id, created_at)
VALUES (?, ?, ?, NOW(3));
COMMIT;
```

> ⚠️ 冗余表只解决「已知的第二维度」。运营要「任意 4 个条件组合筛选」时，别再建第 5 张冗余表，直接上 ES——那是搜索问题，不是分片问题。

---

## 36.7 分片后的事务与全局 ID

**事务边界**：把「必须原子」的实体放进同一个分片键。订单 + 订单明细 + 支付流水都用 `order_id` 路由，就能保住本地事务；跨片一律 Outbox + Saga（第 10、13 章），不要再幻想 XA。

**全局 ID** 两条路：

1. **号段模式**（Leaf-segment）：数据库批量发号，应用内存里消耗，扛得住高并发，ID 连续、对分页友好。
2. **雪花**：本地生成、无网络开销，但依赖时钟，必须处理回拨。

```sql
-- 号段表：一次取 1000 个，DB 只承担 1/1000 的 QPS
CREATE TABLE id_segment (
  biz_tag     VARCHAR(64) NOT NULL,
  max_id      BIGINT UNSIGNED NOT NULL DEFAULT 0,
  step        INT NOT NULL DEFAULT 1000,
  updated_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (biz_tag)
) ENGINE=InnoDB;

INSERT INTO id_segment (biz_tag, max_id, step) VALUES ('order', 0, 1000)
ON DUPLICATE KEY UPDATE max_id = max_id;

-- 取号：必须在同一事务里 UPDATE 再 SELECT，靠行锁串行化
START TRANSACTION;
UPDATE id_segment SET max_id = max_id + step WHERE biz_tag = 'order';
SELECT max_id, step FROM id_segment WHERE biz_tag = 'order';
COMMIT;
-- 应用拿到 [max_id - step + 1, max_id] 这一段，内存里自增发放
```

```go
package idgen

import (
	"sync"
	"time"
)

// Snowflake：41b 时间戳 + 10b 机器 + 12b 序列；epoch 自定义，别用 1970
type Snowflake struct {
	mu       sync.Mutex
	lastMs   int64
	machine  int64
	sequence int64
	epoch    int64
}

func NewSnowflake(machine int64, epoch time.Time) *Snowflake {
	return &Snowflake{machine: machine & 0x3FF, epoch: epoch.UnixMilli()}
}

func (s *Snowflake) Next() int64 {
	s.mu.Lock()
	defer s.mu.Unlock()

	now := time.Now().UnixMilli()
	if now < s.lastMs { // 时钟回拨：小幅等待，大幅直接报错，绝不生成重复 ID
		drift := s.lastMs - now
		if drift > 5 {
			panic("clock moved backwards > 5ms, refuse to generate id")
		}
		time.Sleep(time.Duration(drift) * time.Millisecond)
		now = time.Now().UnixMilli()
	}
	if now == s.lastMs {
		s.sequence = (s.sequence + 1) & 0xFFF
		if s.sequence == 0 { // 同一毫秒用尽 4096 个，自旋到下一毫秒
			for now <= s.lastMs {
				now = time.Now().UnixMilli()
			}
		}
	} else {
		s.sequence = 0
	}
	s.lastMs = now
	return ((now - s.epoch) << 22) | (s.machine << 12) | s.sequence
}
```

> 💡 新项目也可以直接用 **UUIDv7**（时间有序），MySQL 里用 `BINARY(16)` + `UUID_TO_BIN(?, 1)` 存储，省掉一套发号服务；代价是 16B 主键比 8B 雪花更占索引空间。

---

## 36.8 什么时候上 TiDB / OceanBase

| 维度 | MySQL 分片 | TiDB | OceanBase |
| --- | --- | --- | --- |
| 兼容性 | 100% | MySQL 协议，大部分语法兼容 | MySQL / Oracle 双模式 |
| 架构 | 中间件 + N 个 MySQL | TiDB + TiKV + PD（+ TiFlash） | OBServer + Paxos 多副本 |
| 点查延迟 | 0.2~1ms | 1~3ms（跨 Region 更高） | 1~3ms |
| 跨片事务 | 自己写 Saga | 原生分布式事务 | 原生分布式事务 |
| 分析能力 | 另建数仓 | TiFlash 列存 HTAP | 列存副本 HTAP |
| 扩缩容 | 手工迁移/双写 | 在线加 TiKV，自动调度 | 在线加 Zone/副本 |
| 运维复杂度 | 中（组件多但熟悉） | 中高（3 组件 + 调优） | 高（金融级参数多） |
| 成本 | 低 | 中（三副本 + TiFlash） | 中高 |
| 迁移成本 | 改 SQL/路由/事务 | 改自增、大事务、字符集、隔离级别 | 兼容性最好，但运维要重新学 |

**决策口径**：

- 数据量 **> 5TB 且 3 年翻倍**、写入持续增长、又不想自己维护分片路由 → TiDB。
- 金融核心、要求 Oracle 兼容、强一致多副本、能养平台团队 → OceanBase。
- 只是「单表大 + 报表慢」→ MySQL + 归档 + ClickHouse，别上分布式库。
- 只是「写 TPS 高」→ 先看能不能把写削峰/批量/异步，再谈换库。

**迁移成本清单**（漏一项就会在上线前一周爆炸）：自增主键行为、`AUTO_INCREMENT` 连续性、大事务上限、`SELECT ... FOR UPDATE` 语义、隔离级别默认值、字符集与排序规则、存储过程/触发器、`ON DUPLICATE KEY` 行为、连接池与超时、备份恢复工具、监控指标名。

---

## 36.9 双修对照：事件表是 Agent 的「订单表」

| 维度 | 传统系统 | Agent 系统 | 同 or 异 |
| --- | --- | --- | --- |
| **机制本体** | 订单/用户表分片 | 事件表、会话表、prompt 表分片 | 方法相同，表结构不同 |
| **唯一键** | `order_id` / `user_id` | `run_id` / `step_id` / `event_id` | 雪花/号段完全复用 |
| **失败模式** | 跨片查询、热点、ID 回拨 | 加：事件表爆量、delta 落库写放大 | 多两类 |
| **恢复方式** | 备份 + binlog | 事件可重放，prompt 可从对象存储回捞 | 原则相同 |
| **验证方法** | 分片行数求和、倾斜度 | 加：run 事件条数完整性 | 指标扩展 |
| **成本结构** | 存储 + IOPS | 事件写入占大头，向量其次 | 结构不同 |

**完全相同的部分**：分片键选择方法、全局 ID、跨片聚合方案、冷热分层、扩容双写流程。这些一套代码/一套规范两边通用。

**Agent 特有的三个差异**：

1. **事件表爆量**：一次 Run 平均 42 条 step event，100 万 Run/月 = **4,200 万行/月**，一年 5 亿行。它 append-only、按时间查、按 run 查，**分片键应该选时间（RANGE 分区）+ run_id 二级索引**，而不是照抄订单表的 `user_id` 哈希——因为你的查询是「某 Run 的全部事件」和「某时间段的统计」，不是「某用户的所有事件」。
2. **delta 不落库**：流式输出的每个 token 增量都不要写库。只存最终 message + 首 token 时间 + token 数；否则一次 2,000 token 的回答会产生 2,000 次 UPDATE，写放大 **20~50 倍**。
3. **prompt/大字段外置**：prompt 平均 8KB、工具结果 30KB，别放 MySQL 行内。DB 只存 `object_uri + sha256 + size`，正文进对象存储（第 40 章算过这笔账）。

```sql
-- Agent 事件表：按月 RANGE 分区，主键含时间，天然支持分区裁剪与归档
CREATE TABLE agent_event (
  id         BIGINT UNSIGNED NOT NULL,
  run_id     CHAR(26)     NOT NULL,
  step_id    INT          NOT NULL,
  event_type VARCHAR(32)  NOT NULL,
  payload    JSON         NOT NULL,
  created_at DATETIME(3)  NOT NULL,
  PRIMARY KEY (id, created_at),
  KEY idx_run (run_id, step_id),
  KEY idx_type_time (event_type, created_at)
) ENGINE=InnoDB
PARTITION BY RANGE COLUMNS(created_at) (
  PARTITION p202601 VALUES LESS THAN ('2026-02-01'),
  PARTITION p202602 VALUES LESS THAN ('2026-03-01'),
  PARTITION p202603 VALUES LESS THAN ('2026-04-01'),
  PARTITION pmax    VALUES LESS THAN (MAXVALUE)
);
```

---

## 36.10 会在哪炸：失效模式清单

| 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 某个分片 CPU 90%，其他 30% | 热点 key / 分片键分布不均 | 各分片 QPS、`max/avg` 倾斜度 | 加盐、二次分片、冷热分离 | 上线前用真实 key 分布算倾斜度 |
| 跨片查询 P99 暴涨 | fan-out 路数 × 单查询延迟 | 路由日志里的分片数 | 冗余表 / ES / 限制查询维度 | 禁止无分片键的查询进核心链路 |
| 扩容后数据对不上 | 双写期间新旧分片都写，回滚未清理 | 对比新旧路由的 count/checksum | 停双写、按主键重放、对账 | 双写开关 + 对账任务 + 回滚脚本 |
| 全局 ID 重复 | 时钟回拨 / 号段并发取重 | 唯一索引冲突日志 | 换号段 / 修时钟 / 加机器位 | 雪花回拨检测 + 号段双 buffer |
| 深分页 OOM | 每片 `offset+limit` 全量归并 | 堆内存、GC、慢查询 | 改游标分页 | 代码层禁用超过 1000 的 offset |
| DDL 拖垮从库 | 大表在线改表 + binlog 暴涨 | 从库延迟、binlog 大小 | gh-ost 限速 / 低峰 / 拆表 | DDL 预约 + 延迟熔断 |

---

## 36.11 怎么验证：三个能跑的检查

```sql
-- 1) 分片倾斜度：max/avg > 1.5 就该处理（在每个分片执行后汇总）
SELECT 'db_07' AS shard, COUNT(*) AS rows_cnt, SUM(total_amount) AS amt
FROM db_07.t_order WHERE created_at >= '2026-01-01';

-- 2) 跨片总数校验：各分片求和 vs 全局流水表
SELECT SUM(c) FROM (
  SELECT COUNT(*) c FROM db_00.t_order WHERE created_at >= '2026-03-01'
  UNION ALL SELECT COUNT(*) FROM db_01.t_order WHERE created_at >= '2026-03-01'
  UNION ALL SELECT COUNT(*) FROM db_02.t_order WHERE created_at >= '2026-03-01'
  -- ... 其余分片
) t;
```

3. **扩容演练**：在预发用 1/10 数据跑一遍「双写 → 迁移 → 校验 → 切读 → 停双写 → 回滚」，记录每一步耗时。没演练过的扩容方案，等于没有方案。

**观测指标**：各分片 QPS/CPU/连接数、倾斜度、跨片查询占比、路由层 P99、复制延迟、全局 ID 冲突数。

---

## 36.12 用什么工具

| 需求 | 推荐 | 说明 |
| --- | --- | --- |
| Java 生态分片 | Apache ShardingSphere-JDBC 5.x | 客户端模式，无代理跳数；支持分片/读写分离/加密/影子库 |
| 多语言/代理模式 | ShardingSphere-Proxy / Vitess | Vitess 在 YouTube 规模验证过，运维重 |
| 分布式数据库 | TiDB / OceanBase | 见 §36.8；先 PoC 再迁移 |
| 在线 DDL | gh-ost / pt-online-schema-change | 必须限速 + 监控从库延迟 |
| 数据同步 | Canal / Debezium | 喂 ES、数仓、冗余表 |
| 不推荐 | MyCat 1.x | 基本停更，新项目别碰 |

---

## 36.13 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 分片本质 | 用运维复杂度换单点写能力，不是免费扩容 |
| 单表拐点 | 先算 B+ 树：500B 行宽约 4,380 万行；别背「2000 万」 |
| 真信号 | buffer pool 命中率、单表 >300GB、DDL/恢复超窗口、写 TPS 到顶 |
| QPS | 单机写 3k~8k TPS；分片数 = 峰值×1.5/单库安全值 |
| 不该拆 | 三年 <1 亿行、慢在 SQL、团队没人、要跨维事务、有更便宜的招 |
| 分片键 | 查询必带、基数高、分布均匀；一张表只能一个 |
| 热点 | 加盐、二次分片、冷热分离、请求合并 |
| 跨片查询 | 冗余表 / ES 宽表 / 预聚合；禁止深分页 fan-out |
| 全局 ID | 号段（连续、DB 压力小）/ 雪花（本地、怕回拨）/ UUIDv7 |
| 上 TiDB | >5TB 且持续增长、要跨片事务与 HTAP，且能接受 +1~2ms |
| 双修 | 事件表按时间分区，不按 user 哈希；delta 绝不落库 |

## 36.14 自检题

1. **老板说「单表过 2000 万就要拆，这是最佳实践」，你怎么用数字反驳？**
   > 答案要点：① 2000 万对应 1KB 行宽的 3 层 B+ 树，500B 行宽可到 4,380 万；② 真正的拐点是 buffer pool 命中率 <99%、单表 >300GB、DDL/恢复超 RTO、写 TPS 到顶；③ 拆分的年成本（2 名运维 + 改造 + 机器）通常 100 万级，升配 + 归档只要 20 万级；④ 先用真实数据跑 3 层树容量算式再决定。

2. **按 `user_id` 分片后，客服要按手机号查订单，你有哪几种解法？各自的代价？**
   > 答案要点：① 冗余表（phone → order_no），同事务双写，强一致但只解决一个维度；② ES 宽表，CDC 同步，支持多维但最终一致（秒级）；③ 广播查询，P99 随分片数线性涨，只能用于后台低频；④ 让客服先按用户 ID 查——流程改造也是解法，且最便宜。

3. **什么时候该从 MySQL 分片换成 TiDB？给出三个硬条件和两个不该换的理由。**
   > 答案要点：硬条件：数据 >5TB 且 3 年翻倍、需要跨片事务、需要 HTAP 或在线扩缩容。不该换：只是报表慢（加 ClickHouse 更便宜）、只是写峰值高（削峰/批量/异步先做）；另外要算迁移成本（自增、大事务、隔离级别、运维重学）与点查延迟 +1~2ms 是否可接受。

## 36.15 延伸阅读

- MySQL 8.0 Reference Manual：*InnoDB Page Structure*、*Online DDL Operations*、`innodb_buffer_pool` 相关状态变量
- Apache ShardingSphere 5.x 文档：*Sharding*、*Shadow Database*、*DistSQL*
- TiDB 文档：*TiKV / PD 架构*、*事务隔离级别*、*TiFlash*
- OceanBase 文档：*Paxos 多副本*、*LSM-Tree 存储引擎*
- gh-ost 与 Percona Toolkit `pt-online-schema-change` 官方文档
- 上一章：[35. 数据模型决定存储：八类存储的本质](35-storage-model-selection.md)
- 下一章：[37. NoSQL 选型：Redis / Mongo / Cassandra / ES / ClickHouse](37-nosql-selection.md)
- 容量与归档见第 40 章 ｜ 跨服务事务见第 12 章 ｜ 全局 ID 的幂等用法见第 3 章
