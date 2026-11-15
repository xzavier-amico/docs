# 40. 容量、冷热分层与成本

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 存储选型 ｜ **编号**：40 / 52
> **本章定位**：容量是唯一一个「不算就一定出事」的工程项——它不会今天报错，会在某个凌晨以「磁盘 100%、写入失败、主从全挂」的形式一次性还给你。本章给可复算的容量公式、四种分层手段、备份演练清单与成本优化顺序。
> **读法**：§40.2 的公式直接套你的表；§40.4 的分层手段按需选；§40.6 的「没演练过的备份等于没有备份」是本章最该被记住的一句。

---

## 40.1 事故现场：磁盘 100%，订单库写入全部失败

2025 年 11 月，某电商的订单库（MySQL 8.0，主从各 1 台，数据盘 2TB）在大促前一周挂了。

| 时间 | 事件 |
| --- | --- |
| 22:41 | 监控告警：磁盘使用率 91% |
| 22:43 | 值班判断「明天再清」，未处理（历史告警每周都有） |
| 次日 09:02 | 大促预热，写入量 ×4 |
| 09:38 | 磁盘 97%，binlog 开始积压 |
| 09:52 | 磁盘 100%，MySQL 报 `ER_RECORD_FILE_FULL`，**所有写入失败** |
| 09:53 | 主库不可写，订单创建全部失败，错误率 100% |
| 10:05 | DBA 紧急 `PURGE BINARY LOGS`，释放 180GB |
| 10:12 | 写入恢复，但**期间 14 分钟的订单全部失败**（约 1.2 万单） |
| 10:30 | 从库因为同步延迟 + relay log 堆积，也满了，级联故障 |

**总影响 14 分钟、1.2 万单失败、赔付 8.6 万**，而根因极其「不值钱」：

- 订单表 **1 年没有归档**，1.8 亿行，数据+索引 59GB，加从库副本共 118GB。
- binlog 保留 **30 天**（默认 7 天，被人改成 30），大促前后日均 30GB，占用 900GB。
- 全备每天 1 次，保留 30 份，压缩后 600GB（还在同一块盘上）。
- 同实例其他库、undo 与临时表空间约 250GB。
- 磁盘 2TB，实际可用只有 ~1.85TB。

**没有一项是「技术难题」，全是「没算过」。**

> 🩸 **容量的残酷之处**：它不是线性恶化，而是**断崖**。90% 时一切正常，100% 时所有写入瞬间失败，且恢复需要腾空间——而你连删数据都删不动（delete 也要写 binlog）。

---

## 40.2 容量算法：把「感觉够」变成「算得出来」

### 第一步：算行宽（不是 `SHOW TABLE STATUS` 的 Avg_row_length，那个含碎片）

```text
行大小 ≈ 行头(5B) + 事务ID(6B) + 回滚指针(7B) + 各列存储 + NULL 位图(每 8 列 1B)

常见列大小：
  BIGINT        8B
  INT           4B
  TINYINT       1B
  DATETIME(3)   7B（MySQL 5.6+ 紧凑格式）
  DATE          3B
  DECIMAL(18,2) 9B
  VARCHAR(n)    实际长度 + 1~2B 长度前缀（utf8mb4 中文 3B/字，emoji 4B）
  TEXT/BLOB     行内只存 20B 指针，数据在溢出页
```

**算例：订单表**

```text
列：order_id BIGINT(8) + user_id BIGINT(8) + amount BIGINT(8) + status TINYINT(1)
   + created_at DATETIME(3)(7) + updated_at DATETIME(3)(7) + pay_time DATETIME(3)(7)
   + remark VARCHAR(200) 平均 60B + 其余 12 个字段约 60B
   ≈ 166B + 行头开销 18B ≈ 184B

InnoDB 页 16KB，实际填充率约 70%（页分裂/删除留下的空洞）
⇒ 实际每行占用 ≈ 184 / 0.7 ≈ 263B
```

### 第二步：算数据 + 索引

```text
数据（聚簇索引叶子） = 行数 × 每行实际大小
1.8 亿 × 263B ≈ 47GB          ← 注意：这是数据本体

二级索引（每个包含主键 order_id）：
  idx_user_created (user_id 8 + created_at 7 + 主键 8 = 23B) × 1.8亿 ≈ 4.1GB（含页开销约 6GB）
  idx_status_created (status 1 + created_at 7 + 主键 8 = 16B) ≈ 3GB
  idx_pay_time (7 + 8 = 15B) ≈ 3GB
  ⇒ 索引合计 ≈ 12GB

InnoDB 其他：
  undo log（长事务/大查询期间）≈ 10~50GB（波动）
  redo log（固定，如 2GB × 2）
  临时表空间（大查询/排序）≈ 20~100GB（波动，最容易被忽略）
```

### 第三步：算副本 + 日志 + 备份

```text
副本：主 1 + 从 1 = 2 份  ⇒ 数据+索引 ≈ (47+12) × 2 = 118GB
binlog：每天写入量 ≈ 日订单 50 万 × 0.5KB(含索引变更) ≈ 250MB/天？不对——
        实际大促前后日均 30GB（含批量更新、索引变更、DDL）
        保留 30 天 ⇒ 30 × 30GB = 900GB（这就是那次事故的主因）
全备：47+12 = 59GB/份，压缩后约 20GB × 30 份 ≈ 600GB（不压缩则 1.77TB，直接顶到磁盘上限）
```

> 💡 **这张账算出来，你会立刻发现两个问题**：① 全备不压缩根本放不下（1.77TB），压缩后 600GB 也吃掉近三分之一磁盘；② binlog 保留 30 天、日均 30GB，就是 900GB——在写入高峰是灾难。**容量事故往往不是「数据太大」，而是「日志和备份没算」。**

### 第四步：算增长与水位

```text
月增订单 = 日均 50 万 × 30 = 1,500 万行 ≈ 11GB/月（数据+索引+副本）
binlog 与全备到保留期后不再净增；规划时按 50GB/月留缓冲，年增约 600GB

磁盘水位线：
  预警 70%（开始规划扩容）
  告警 85%（立即处理）
  红线 90%（禁止任何批量写入/DDL）
  
按 70% 水位反推：可用容量 = 单机磁盘 × 0.7
  若单机 2TB ⇒ 可用 1.4TB
  假设清理后占用 1.2TB ⇒ 只剩 200GB ≈ 4 个月
```

### 一张可复算的表（模板）

| 项 | 算式 | 你的值 |
| --- | --- | --- |
| 每行实际大小 | (列宽和 + 18) / 0.7 | ____ B |
| 数据量 | 行数 × 每行大小 | ____ GB |
| 索引量 | Σ(索引列宽 + 主键) × 行数 × 1.4 | ____ GB |
| 副本倍数 | 主 + 从数 | × ____ |
| binlog | 日增量 × 保留天数 | ____ GB |
| 全备 | (数据+索引) × 保留份数 × 压缩比(0.3) | ____ GB |
| 临时/undo | 峰值并发 × 单查询临时空间 | ____ GB |
| **总需求** | 上列求和 | ____ GB |
| **年增长** | 月增 × 12 | ____ GB |

### 一个必须会背的算例：1 亿订单 × 500B × 3 副本 × 2 索引

| 步骤 | 算式 | 结果 |
| --- | --- | --- |
| 裸数据 | 1×10⁸ × 500B | 50 GB |
| 二级索引（2 个，合计 +70%） | 50 × 1.7 | 85 GB |
| 三副本 | 85 × 3 | 255 GB |
| binlog/undo/临时（+30%） | 255 × 1.3 | 331.5 GB |
| 2 年增长（年增 41%，1.41²≈2） | 331.5 × 2 | 663 GB |
| 30% 余量 | 663 × 1.3 | **862 GB ≈ 0.86 TB** |

直觉答案 50GB，实际 0.86TB，差 **17 倍**。如果只按 500GB 买盘，第 14 个月就会写满。**报预算时按这张表报，不要按「行数 × 行宽」报。**

### 第五步：B+ 树高度（决定点查是 3 次还是 4 次 IO）

```text
非叶页容量 ≈ 16KB / (主键 8B + 指针 6B) ≈ 1,170 条
叶页容量   ≈ 16KB / 263B ≈ 62 行

3 层 B+ 树可容纳 ≈ 1,170 × 1,170 × 62 ≈ 8,500 万行
⇒ 1.8 亿行 ≈ 4 层 ⇒ 每次点查多一次 IO（约 +0.1ms，但在高并发下放大）
```

**这也是为什么「单表 2,000 万行」被反复提起**：它是「3 层树 + 内存装得下热数据」的经验拐点，不是硬限制。**真正该看的是「热数据能否装进 buffer pool」**——如果 20% 的热数据能装下，2 亿行也能跑得很好。

---

## 40.3 结构图：容量水位与分层

```text
 使用率
 100% ┤                          ╔══ 断崖：写入失败
  90% ┤                      ╔═══╝
  85% ┤                  ╔═══╝   告警线（立即处理）
  70% ┤          ╔═══════╝       预警线（规划扩容/归档）
  50% ┤    ╔═════╝
   0% └────┴──────────────────────────────▶ 时间

分层（按访问频率）
 ┌──────────────────────────────────────────────┐
 │ 热（7 天，<5% 数据）  → 内存/SSD，buffer pool   │
 │ 温（7~90 天，15%）    → SSD 主表 + 分区          │
 │ 冷（90 天~2 年，30%） → 归档表 / 列存            │
 │ 冰（>2 年，50%）      → 对象存储（Parquet）      │
 └──────────────────────────────────────────────┘
```

---

## 40.4 冷热分层的四种手段

### 手段一：分区表（最适合「按时间」的数据）

```sql
-- 按月分区：删一个月 = DROP PARTITION（秒级），而不是 DELETE（几小时 + 大量 binlog）
CREATE TABLE orders (
    order_id   BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    amount     BIGINT NOT NULL,
    status     TINYINT NOT NULL,
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (order_id, created_at),      -- ★ 分区键必须在主键里
    KEY idx_user (user_id, created_at)
) ENGINE=InnoDB
PARTITION BY RANGE (TO_DAYS(created_at)) (
    PARTITION p202609 VALUES LESS THAN (TO_DAYS('2026-10-01')),
    PARTITION p202610 VALUES LESS THAN (TO_DAYS('2026-11-01')),
    PARTITION p202611 VALUES LESS THAN (TO_DAYS('2026-12-01')),
    PARTITION pmax    VALUES LESS THAN MAXVALUE
);

-- 归档：把老分区搬走（先导出，再 drop）
ALTER TABLE orders EXCHANGE PARTITION p202609 WITH TABLE orders_archive_202609;
DROP TABLE orders_archive_202609;   -- 或转存到冷库
```

**分区的三个陷阱**：

| 陷阱 | 后果 | 正解 |
| --- | --- | --- |
| 分区键不在唯一索引里 | 建表失败 | 主键/唯一键包含分区键 |
| 分区太多（>1000） | 打开表慢、DDL 慢 | 按月/周，控制在几百 |
| 查询不带分区键 | 扫全部分区 | 应用层强制时间范围 |

### 手段二：归档表 + 异步搬迁

```sql
-- 归档任务：每次 5,000 行，按主键推进，避免大事务
INSERT INTO orders_archive
SELECT * FROM orders
WHERE created_at < NOW() - INTERVAL 90 DAY
  AND order_id > ?            -- 断点续跑
ORDER BY order_id
LIMIT 5000;

DELETE FROM orders
WHERE created_at < NOW() - INTERVAL 90 DAY
  AND order_id > ?
ORDER BY order_id
LIMIT 5000;
```

```go
// Go 归档任务：先插后删 + 校验 + 断点 + 限速（import "fmt"）
func (j *ArchiveJob) Run(ctx context.Context) error {
    for {
        var lastID int64
        if err := j.db.QueryRowContext(ctx,
            `SELECT COALESCE(MAX(last_id),0) FROM archive_checkpoint WHERE job='orders'`).Scan(&lastID); err != nil {
            return err
        }

        // 1) 先确定本批边界：按主键推进，避免大事务与 OFFSET
        var batchMax int64
        err := j.db.QueryRowContext(ctx, `
            SELECT COALESCE(MAX(order_id),0) FROM (
                SELECT order_id FROM orders
                WHERE created_at < NOW() - INTERVAL 90 DAY AND order_id > ?
                ORDER BY order_id LIMIT 5000
            ) t`, lastID).Scan(&batchMax)
        if err != nil { return err }
        if batchMax == 0 { return nil }

        tx, err := j.db.BeginTx(ctx, nil)
        if err != nil { return err }

        // 2) 插入归档表
        if _, err := tx.ExecContext(ctx, `
            INSERT INTO orders_archive
            SELECT * FROM orders
            WHERE created_at < NOW() - INTERVAL 90 DAY
              AND order_id > ? AND order_id <= ?`, lastID, batchMax); err != nil {
            tx.Rollback(); return err
        }

        // 3) 校验本批行数：src == dst 才允许删
        var srcCnt, dstCnt int
        if err := tx.QueryRowContext(ctx, `
            SELECT COUNT(*) FROM orders
            WHERE created_at < NOW() - INTERVAL 90 DAY
              AND order_id > ? AND order_id <= ?`, lastID, batchMax).Scan(&srcCnt); err != nil {
            tx.Rollback(); return err
        }
        if err := tx.QueryRowContext(ctx, `
            SELECT COUNT(*) FROM orders_archive
            WHERE created_at < NOW() - INTERVAL 90 DAY
              AND order_id > ? AND order_id <= ?`, lastID, batchMax).Scan(&dstCnt); err != nil {
            tx.Rollback(); return err
        }
        if srcCnt != dstCnt {
            tx.Rollback()
            return fmt.Errorf("archive count mismatch: src=%d dst=%d", srcCnt, dstCnt)
        }

        // 4) 再删源表
        if _, err := tx.ExecContext(ctx, `
            DELETE FROM orders
            WHERE created_at < NOW() - INTERVAL 90 DAY
              AND order_id > ? AND order_id <= ?`, lastID, batchMax); err != nil {
            tx.Rollback(); return err
        }

        // 5) 断点必须是本批最大主键，不能写 lastID + 行数
        if _, err := tx.ExecContext(ctx, `
            INSERT INTO archive_checkpoint(job,last_id) VALUES('orders',?)
            ON DUPLICATE KEY UPDATE last_id=VALUES(last_id)`, batchMax); err != nil {
            tx.Rollback(); return err
        }
        if err := tx.Commit(); err != nil { return err }

        time.Sleep(200 * time.Millisecond)   // 限速，保护主库
    }
}
```

> ⚠️ **归档必须「先插后删 + 校验」**：直接 `DELETE` 会让你在发现搬错时无法回滚。正确顺序：插入归档表 → 校验行数 → 删除原表 → 记录断点。

### 手段三：列存（分析型查询）

```sql
-- ClickHouse：把订单归档表同步到列存，报表查询从分钟级降到秒级
CREATE TABLE orders_ck (
    order_id   UInt64,
    user_id    UInt64,
    amount     Int64,
    status     UInt8,
    created_at DateTime
) ENGINE = MergeTree
PARTITION BY toYYYYMM(created_at)
ORDER BY (created_at, user_id)
TTL created_at + INTERVAL 3 YEAR DELETE;   -- 自动过期

-- 报表：按月聚合（从 MySQL 查要扫全表，这里只扫相关 granule）
SELECT toStartOfMonth(created_at) AS m,
       count() AS orders,
       sum(amount) AS gmv
FROM orders_ck
WHERE created_at >= '2026-01-01'
GROUP BY m ORDER BY m;
```

### 手段四：对象存储（最冷的数据）

```text
热/温/冷 → 数据库/列存
冰（>2 年，只用于审计/合规） → 对象存储（Parquet/ORC，压缩后约为原表 1/5~1/10）

生命周期策略（以 S3/OSS 为例）：
  0~30 天   标准存储
  30~180 天 低频访问
  180 天以后 归档/冷归档（取回需分钟~小时）
```

---

## 40.5 保留期与合规删除

| 数据类型 | 常见保留要求 | 说明 |
| --- | --- | --- |
| 交易/账务 | 5~10 年（金融） | 不能真删，只能归档 + 脱敏 |
| 操作日志 | 6 个月（等保） | 到期可删 |
| 个人信息 | 业务目的达成后删除（GDPR Art.17） | **必须能定位并删除** |
| 风控/反欺诈 | 3~5 年 | 合规豁免 |
| 模型训练数据 | 按授权期限 | 授权到期需下线并重训 |

**「删除」在分布式系统里是三件事**：

```text
① 逻辑删除：业务表打标 deleted_at（立刻对用户不可见）
② 物理删除：异步任务删数据 + 删备份中的副本（备份也要能定向删除！）
③ 派生删除：缓存、ES、向量、数仓、日志——**每一处都要删**
```

> 🩸 **最常见的合规事故**：用户行使删除权，主库删了，但 ES、向量库、日志平台、备份里都还在。监管只看「能否检索到」，不看「主库有没有」。

---

## 40.6 备份与恢复：没演练过的备份等于没有备份

### 先定义两个口径

| 口径 | 定义 | 例子 |
| --- | --- | --- |
| **RPO**（恢复点目标） | 最多能丢多少数据 | 5 分钟 ⇒ 最多丢 5 分钟写入 |
| **RTO**（恢复时间目标） | 多久能恢复服务 | 30 分钟 ⇒ 半小时内可对外服务 |

### 备份策略对照

| 策略 | RPO | RTO | 成本 | 适用 |
| --- | --- | --- | --- | --- |
| 每日全备 | 24h | 1~4h | 低 | 非核心 |
| 全备 + binlog 增量 | 分钟级 | 1~2h | 中 | 核心业务 |
| 主从 + 延迟从库 | 秒级 | 分钟级 | 高 | 高可用 |
| 跨地域备份 | 分钟级 | 30min~2h | 很高 | 容灾要求 |

### 恢复演练清单（每季度一次，必须计时）

```text
□ 1. 取一份最新的全备 + 对应 binlog，恢复到一台**空机器**
□ 2. 记录：开始时间、全备恢复耗时、binlog 回放耗时、总耗时（= 实测 RTO）
□ 3. 校验：表数量、关键表行数、checksum、抽样 100 行业务核对
□ 4. 校验：自增 ID / 序列 / 触发器 / 视图 / 存储过程是否完整
□ 5. 校验：账号权限、字符集、时区、SQL_MODE 是否一致
□ 6. 让业务方跑一遍关键 SQL，确认可用
□ 7. 记录：实际 RPO（最后一个 binlog 位点与故障点的差）
□ 8. 把耗时与问题写进 Runbook，更新告警阈值
```

**常见「备份看起来有、其实没用」的六种情况**：

| 情况 | 表现 | 怎么发现 |
| --- | --- | --- |
| 备份文件损坏 | 恢复时报错 | 演练 |
| 只备份了数据没备份 binlog | 无法恢复到故障点 | 检查 binlog 归档 |
| 备份在同一块盘/同一机房 | 机房故障全丢 | 看存储位置 |
| 备份没加密 | 合规问题、泄露风险 | 检查加密配置 |
| 恢复脚本过期 | 恢复出来版本不对 | 演练 |
| 没人知道怎么恢复 | 出事现场现学 | 演练 + Runbook |

---

## 40.7 成本优化：按「性价比」排序，别先换存储

| 优先级 | 手段 | 典型节省 | 风险 | 工作量 |
| --- | --- | --- | --- | --- |
| 1 | **删除无用数据**（日志、临时表、重复副本、僵尸索引） | 10~30% | 低 | 低 |
| 2 | **压缩**（表压缩、备份压缩、日志压缩） | 20~50% | 低 | 低 |
| 3 | **归档/分层**（冷数据下沉） | 30~60% | 中 | 中 |
| 4 | **索引治理**（删冗余索引，索引也占空间且拖慢写） | 5~20% | 中 | 中 |
| 5 | **换存储介质/规格**（SSD→HDD 冷数据、按量→预留） | 20~40% | 中 | 中 |
| 6 | **重构数据模型**（宽表拆冷热列、大字段外置） | 20~50% | 高 | 高 |
| 7 | **换数据库** | 不确定 | 很高 | 很高 |

> 💡 **经验**：**先做 1~3，通常能省 50%，且不改变架构。** 一上来就讨论「换 TiDB 还是换 PG」的团队，往往连「哪些表 90 天没人查」都没统计过。

### 一条 SQL 找出「可以删的索引」

```sql
-- MySQL 8：从未被使用过的索引（performance_schema）
SELECT object_schema, object_name, index_name
FROM performance_schema.table_io_waits_summary_by_index_usage
WHERE index_name IS NOT NULL
  AND count_star = 0
  AND object_schema NOT IN ('mysql','sys')
ORDER BY object_schema, object_name;

-- 冗余索引：A 是 B 的前缀（(a) 与 (a,b) 同时存在，前者通常可删）
SELECT t1.table_name, t1.index_name AS redundant, t2.index_name AS covering
FROM information_schema.statistics t1
JOIN information_schema.statistics t2
  ON t1.table_schema=t2.table_schema AND t1.table_name=t2.table_name
 AND t1.seq_in_index=1 AND t2.seq_in_index=1
 AND t1.column_name=t2.column_name
 AND t1.index_name<>t2.index_name
WHERE t1.table_schema NOT IN ('mysql','sys');
```

---

## 40.8 双修对照：三种成本结构的差异

| 维度 | 传统系统 | Agent 系统 | 相同 / 差异根因 |
| --- | --- | --- | --- |
| **主要成本** | 机器 + 存储 + 人力（近似固定） | 以上 + **token（随量线性）** | 成本结构不同 |
| **容量增长源** | 业务数据 | 业务数据 + **事件表（每步一条）+ 会话历史 + 向量 + 缓存副本** | 写放大 5~20 倍 |
| **冷数据** | 订单/日志 | 以上 + **历史会话与 trace**（合规要求保留） | 量大且不可删 |
| **备份** | 数据 + binlog | 以上 + **向量库快照（大且难增量）** | 重建成本高 |
| **优化手段** | 归档/压缩/索引 | 以上 + **上下文裁剪、模型路由、语义缓存** | 优化点更多 |
| **成本可见性** | 月末账单 | **按租户/按 Run 实时可见** | 需要成本归因 |

### Agent 侧三个容易算错的量

```text
① 事件表：一次 Run 20 步，每步 3 条事件（start/end/tool）⇒ 60 行/Run
   10 万 Run/天 ⇒ 600 万行/天 ⇒ 年 22 亿行
   每行 1KB（含 payload）⇒ 22TB/年（未压缩）
   ⇒ 必须：payload 只存增量/引用、按月分区、90 天后转列存

② 向量：100 万 chunk × 1024 维 × 4B = 4GB 裸向量
   HNSW 索引额外 1~2 倍 ⇒ 8~12GB 常驻内存
   托管向量库按内存计费 ⇒ 约 $100~200/月（1 个实例）
   重建：100 万 chunk × 500 token = 5 亿 token，embedding 约 $10~20（一次性）

③ 上下文：单次 8k 输入 + 800 输出，日 10 万次
   输入 8 亿 token/天；若 $0.15/1M ⇒ $120/天 ⇒ $3.6k/月
   ⇒ 优化顺序：语义缓存（省 30~50%）→ 上下文裁剪（省 20~30%）→ 小模型路由（省 40~60%）
```

---

## 40.9 会在哪炸：八个坑

| # | 坑 | 现象 | 根因 | 怎么查 | 怎么修 |
| --- | --- | --- | --- | --- | --- |
| 1 | binlog 保留过长 | 磁盘被日志吃掉 | 默认 7 天被改成 30 | `SHOW VARIABLES LIKE 'binlog_expire_logs_seconds'` | 改回 7 天 + 归档到对象存储 |
| 2 | 全备与数据同盘 | 磁盘双倍占用、一起丢 | 图省事 | `df -h` 与备份路径 | 异地/异盘 |
| 3 | 大事务 | undo/临时空间暴涨 | 批量 update 100 万行 | 看 `INNODB_TRX` | 拆批（≤5000/批） |
| 4 | 分区键不在主键 | 建表失败或无法分区 | 设计疏忽 | DDL 报错 | 主键含分区键 |
| 5 | DELETE 归档 | 删了 6 小时还删不完 | 逐行删 + 大量 binlog | 看 binlog 增长 | `DROP PARTITION` |
| 6 | 备份未演练 | 恢复时才发现坏 | 只备份不验证 | 季度演练 | 建演练制度 |
| 7 | 临时表空间不回收 | 磁盘只涨不降 | 大查询排序/`ALTER TABLE` | `du -sh #innodb_temp` | 重启/调 `innodb_temp_data_file_path` |
| 8 | 冷数据没删派生副本 | 合规风险 | 只删主库 | 全链路排查 | 统一删除服务 |

---

## 40.10 怎么验证

| 验证项 | 命令/方法 | 通过标准 |
| --- | --- | --- |
| 容量水位 | `df -h`、`SHOW TABLE STATUS`、`information_schema.tables` | < 70% |
| 表与索引大小 | `SELECT table_name, data_length, index_length FROM information_schema.tables` | 与预算表一致 |
| binlog 占用 | `du -sh /var/lib/mysql/*binlog*` | < 预算 |
| 临时空间 | `du -sh /var/lib/mysql/#innodb_temp` | 峰值可控 |
| 慢查询与大事务 | `SLOWLOG`、`INNODB_TRX` | 无 > 5s |
| 备份可恢复 | 季度演练 | 实测 RTO ≤ 目标 |
| 归档正确性 | 抽样比对归档表与原表 | 100% 一致 |

```bash
# 一键体检脚本（放到跳板机）
mysql -N -e "
SELECT table_schema, table_name,
       ROUND((data_length+index_length)/1024/1024/1024,2) AS gb,
       table_rows
FROM information_schema.tables
WHERE table_schema NOT IN ('mysql','sys','information_schema','performance_schema')
ORDER BY (data_length+index_length) DESC LIMIT 20;"
df -h | awk '$5+0 > 70 {print "⚠️ 水位偏高:", $0}'
```

---

## 40.11 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 断崖 | 容量不是线性恶化：90% 正常，100% 全部写入失败，且删都删不动 |
| 行宽 | (列宽和 + 18) / 0.7；别信 `Avg_row_length` |
| 总需求 | 数据 + 索引 + 副本 + binlog + 全备 + 临时空间，**日志与备份常是主因** |
| 水位 | 70% 规划、85% 告警、90% 禁写 |
| B+ 树 | 3 层 ≈ 8,500 万行；真正该看的是「热数据能否装进 buffer pool」 |
| 分区 | 必删用 `DROP PARTITION`；分区键必须在主键/唯一键里 |
| 归档 | 先插后删 + 校验 + 断点 + 限速 |
| 分层 | 热内存 → 温 SSD → 冷归档 → 冰对象存储 |
| 删除 | 逻辑删 + 物理删 + **派生删（缓存/ES/向量/备份）** |
| 备份 | **没演练过的备份等于没有备份**；RTO/RPO 要实测 |
| 成本顺序 | 删无用 → 压缩 → 分层 → 清索引 → 换介质 → 重构 → 换库 |
| 双修 | Agent 写放大 5~20 倍（事件/会话/向量）；成本要按租户/Run 归因 |

## 40.12 自检题

1. **一张 1.8 亿行的订单表，磁盘 2TB，你怎么在 10 分钟内判断「还能撑多久」？**
   > 要点：① 算每行实际大小（列宽+18）/0.7；② 数据+索引 ≈ 行数×大小×1.4；③ 乘副本数；④ 加上 binlog（日增量×保留天数）、全备（大小×份数×压缩比）、临时空间；⑤ 看当前水位与月增长。若「日志+备份」占比 > 40%，先治这两项——往往立刻释放几百 GB，比归档快得多。

2. **为什么「归档」必须「先插入归档表、校验、再删原表」，不能直接 DELETE 后插入？**
   > 要点：DELETE 先执行时，一旦归档插入失败（磁盘满、约束冲突、任务中断），数据就永久丢了；而且 DELETE 产生大量 binlog、锁、undo，可能拖垮主库。先插后删保证「至少有一份」，且可断点续跑、可回滚。

3. **用户行使删除权，你只删了主库，会有什么风险？怎么系统性地避免？**
   > 要点：ES/向量库/缓存/日志平台/数仓/备份里仍可检索到 → 违反 GDPR 等法规，属合规事故。系统性做法：① 所有派生存储用统一 `doc_id/entity_id` 关联；② 删除只走一个「删除服务」，由它级联通知各存储；③ 建「孤儿数据对账」（如 §39.6 的 SQL）定期扫描；④ 备份侧支持按 ID 定向清除或声明保留期与豁免依据。

## 40.13 延伸阅读

- 上一章：[39. 多存储下的事务边界](39-multi-storage-transactions.md)
- 下一章：[41. 边界定义：一条故障在哪一层被解决](41-ops-vs-operation.md)
- 相关：[36. MySQL 还能不能用](36-mysql-limits-sharding.md)、[37. NoSQL 选型](37-nosql-selection.md)、[14. 对账体系](14-reconciliation-system.md)
- 平台侧：[../llm/47-cost-engineering.md](../llm/47-cost-engineering.md)（Token 成本工程）、[../llm/45-storage-architecture.md](../llm/45-storage-architecture.md)（平台存储）
- 官方文档（以版本为准）：MySQL `binlog_expire_logs_seconds`、`innodb_temp_data_file_path`、分区表限制；ClickHouse `TTL` 与 `MergeTree` 分区；GDPR Art.17 删除权与备份豁免口径
- 总纲：[0-summary.md](0-summary.md)
