# 38. 图数据库 vs KV：图真的有用吗

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 存储选型 ｜ **编号**：38 / 52
> **本章定位**：图不是「更高级的数据库」，而是**把「关系」当成一等公民**的一种存储。它在多跳查询上有一个数量级的优势，也有一个非常明确的失效边界。读完你能算出「你这场景到底该不该上图」。
> **读法**：§38.4 的跳数账本是判断依据；§38.6 的 Agent 记忆判定表是选型结论；§38.7 的六个坑是上线前必看。

---

## 38.1 事故现场：一段 12 层 UNION 的权限 SQL，把主库拖垮

2025 年 3 月，某 SaaS 的「数据权限」需求：一个用户能看到哪些报表，取决于他所属的部门、部门的上级部门、他被授予的角色、角色继承的角色、以及「共享给部门」的报表。

最初的实现是关系型 + 递归 CTE：

```sql
WITH RECURSIVE dept_tree AS (
    SELECT dept_id FROM user_dept WHERE user_id = ?
    UNION ALL
    SELECT d.parent_id FROM dept d JOIN dept_tree t ON d.dept_id = t.dept_id
),
role_tree AS (
    SELECT role_id FROM user_role WHERE user_id = ?
    UNION ALL
    SELECT r.parent_role_id FROM role r JOIN role_tree t ON r.role_id = t.role_id
)
SELECT DISTINCT report_id FROM report_acl
WHERE dept_id IN (SELECT dept_id FROM dept_tree)
   OR role_id IN (SELECT role_id FROM role_tree)
   OR owner_id = ?;
```

平时 30ms。到了月中，某个大客户的组织架构有 **4.2 万个部门节点、平均深度 7 层、最深 12 层**，其中一个「全员可见」的根部门被 3,000 人引用。

那天 10:15：

| 时间 | 现象 |
| --- | --- |
| 10:15 | 报表首页 P99 从 180ms 涨到 **8.4s** |
| 10:17 | 连接池打满（120 个连接全部在跑这条 SQL） |
| 10:19 | 主库 CPU 92%，其他接口开始超时 |
| 10:24 | 触发限流，报表功能整体不可用 |
| 10:41 | DBA kill 了 380 个慢查询，恢复 |
| 10:55 | 定位到 `dept_tree`：**单次递归产生 18 万行中间结果** |

**26 分钟，影响 1,700 个租户的报表访问**，其中 3 个大客户提了工单。

复盘给了两个方案：

- 方案 A：加缓存（Redis 缓存用户的可见报表 ID 列表）——能扛住，但权限变更后失效逻辑复杂，且「首次访问」仍然慢。
- 方案 B：把权限关系放进图数据库，用 2~3 跳遍历替代递归 SQL。

他们最后选了 **B + 缓存**：图库负责「算关系」，Redis 负责「缓存结果」，权限变更时按用户/部门精准失效。

> 💡 **这个案例的关键不是「图比 SQL 快」，而是「关系深度和扇出超过某个阈值后，SQL 的中间结果会爆炸」。** 图库的优势是**遍历时不做 join，只走指针**。但如果你的关系只有 1~2 跳，SQL 完全够用——上图反而增加运维负担。

---

## 38.2 到底是什么：图把「关系」变成一等公民

### 三种建模方式

| 模型 | 结构 | 代表 | 适用 |
| --- | --- | --- | --- |
| **邻接表（关系型模拟）** | 两张表：`node(id)`、`edge(from,to,type)` | MySQL / PostgreSQL | 1~3 跳、数据量中等 |
| **属性图（Property Graph）** | 节点 + 边，都可带属性；有标签 | Neo4j、NebulaGraph、JanusGraph | 多跳、路径查询、推荐 |
| **RDF 三元组** | (subject, predicate, object) | Jena、Virtuoso、Amazon Neptune | 知识图谱、本体推理、标准化交换 |

### 图库真正快在哪：索引无关邻接（index-free adjacency）

```text
关系型（每次跳转 = 一次索引查找 + join）：
  user → [idx_user_dept] → dept → [idx_dept_parent] → dept → ...
  每跳约 0.05~0.5ms，且中间结果需要物化（产生行）

原生图（每跳 = 沿指针走到邻居）：
  node --(物理指针)--> edge --(物理指针)--> node
  每跳约 0.5~5μs，无中间结果物化
```

**差 1~2 个数量级**，这就是为什么「3 跳以内 SQL 够用，5 跳以上图库碾压」。

但注意：**这个优势只在「原生图存储」上成立**。很多「图数据库」底层仍是 KV/关系型（如 JanusGraph 用 HBase/Cassandra 做后端，NebulaGraph 用 KV 做存储），它们的优势是**分布式扩展与图语义**，单机多跳性能不一定比 Neo4j 强。

---

## 38.3 结构图：同一份数据，两种存法

```text
【关系型：邻接表】
┌──────────────┐        ┌────────────────────────┐
│ dept         │        │ dept_parent            │
│ dept_id (PK) │◀───────│ child_id (FK)          │
│ name         │        │ parent_id (FK)         │
└──────────────┘        └────────────────────────┘
      ▲                          ▲
      │ 每次遍历：                │
      │  SELECT parent_id FROM dept_parent WHERE child_id = ?
      │  → B+树查找 → 回表 → 产生一行 → 下一轮
      └── 深度 d、扇出 f ⇒ 中间结果 ≈ f^d

【原生图】
   ┌─────────┐  next  ┌─────────┐  next  ┌─────────┐
   │ dept:1  │───────▶│ dept:2  │───────▶│ dept:3  │
   │  props  │        │  props  │        │  props  │
   └────┬────┘        └─────────┘        └─────────┘
        │ rel: PARENT_OF (物理双向指针)
        ▼
   遍历 = 顺着指针走，不查索引、不物化中间表
```

**代价对比（同一份 4.2 万节点、平均扇出 8 的组织树）**：

| 操作 | 邻接表（PG 递归 CTE） | Neo4j 原生图 | 差异根因 |
| --- | --- | --- | --- |
| 1 跳 | 2ms | 1ms | 差不多 |
| 3 跳 | 45ms | 3ms | SQL 中间结果膨胀 |
| 5 跳 | 1.8s | 8ms | 指数级膨胀 |
| 最深 12 跳 | **> 30s（超时）** | 40ms | SQL 不可用 |
| 全图连通分量 | 分钟级 | 秒级 | 算法内置 |

> ⚠️ 这组数字是「同一台机器、同一份数据」的量级参考，不是基准测试结论。**你要用自己的数据跑一遍**——见 §38.8 的 bench 脚本。

---

## 38.4 怎么实现：四种方案与跳数账本

### 方案 A：关系型 + 递归 CTE（1~3 跳首选）

```sql
-- 完整可跑：组织树 + 权限判定（PostgreSQL 语法，MySQL 8 需把 UNION ALL 改成 UNION 兼容写法）
CREATE TABLE dept (
    dept_id    BIGINT PRIMARY KEY,
    parent_id  BIGINT REFERENCES dept(dept_id),
    name       TEXT NOT NULL,
    tenant_id  BIGINT NOT NULL
);
CREATE INDEX idx_dept_parent ON dept(parent_id);
CREATE INDEX idx_dept_tenant ON dept(tenant_id);

CREATE TABLE report_acl (
    report_id  BIGINT NOT NULL,
    subject_type TEXT NOT NULL,   -- 'DEPT' | 'ROLE' | 'USER'
    subject_id BIGINT NOT NULL,
    PRIMARY KEY (report_id, subject_type, subject_id)
);
CREATE INDEX idx_acl_subject ON report_acl(subject_type, subject_id);

-- 查某用户可见的报表：向上遍历部门树（限制深度，防爆炸）
WITH RECURSIVE up AS (
    SELECT d.dept_id, d.parent_id, 0 AS depth
    FROM dept d JOIN user_dept ud ON ud.dept_id = d.dept_id
    WHERE ud.user_id = $1
    UNION ALL
    SELECT p.dept_id, p.parent_id, u.depth + 1
    FROM dept p JOIN up u ON p.dept_id = u.parent_id
    WHERE u.depth < 8                      -- ★ 必须设深度上限
      AND p.tenant_id = $2                 -- ★ 多租户隔离
)
SELECT DISTINCT a.report_id
FROM report_acl a
LEFT JOIN up ON a.subject_type = 'DEPT' AND a.subject_id = up.dept_id
WHERE up.dept_id IS NOT NULL
   OR (a.subject_type = 'USER' AND a.subject_id = $1);
```

**必须加的三道闸**：

1. **深度上限**（`depth < 8`）：防环、防爆炸。
2. **租户/分区条件**：防跨租户遍历。
3. **`statement_timeout`**：单条 SQL 不超过 3s，超时降级到缓存或拒绝。

### 方案 B：原生图（4 跳以上首选）

```cypher
// Neo4j：查用户可见报表（关系方向：user -[:IN]-> dept -[:PARENT_OF*0..8]-> ancestor）
MATCH (u:User {id: $userId})-[:IN]->(d:Dept)
MATCH (d)-[:PARENT_OF*0..8]->(ancestor:Dept)
MATCH (ancestor)<-[:GRANTED_TO]-(r:Report)
WHERE ancestor.tenantId = $tenantId
RETURN DISTINCT r.id AS reportId, length(shortestPath((d)-[:PARENT_OF*]-(ancestor))) AS distance
ORDER BY distance
LIMIT 500;
```

```cypher
// 推荐场景：二度/三度人脉（不含自己）
MATCH (me:User {id: $uid})-[:FOLLOWS]->(friend)-[:FOLLOWS]->(fof)
WHERE fof <> me AND NOT (me)-[:FOLLOWS]->(fof)
RETURN fof.id, count(*) AS mutual
ORDER BY mutual DESC LIMIT 20;
```

**建图时要避开的三个反模式**：

| 反模式 | 后果 | 正解 |
| --- | --- | --- |
| 把整行数据塞进节点属性 | 节点膨胀、更新成本高 | 属性只放「遍历要用的字段」，详情回关系库 |
| 用边表示「同类关系」但方向混乱 | 遍历要双向写两遍 | 统一方向 + 查询用无向匹配 |
| 一个超级节点（粉丝 1000 万） | 该节点成为热点，遍历卡死 | 超级节点拆分 / 预计算 |

### 方案 C：KV 模拟图（Redis 邻接集）

```python
# 邻接表存 Redis：适合「1 跳、超高频」的场景（如关注列表、好友列表）
# 写入
r.sadd(f"adj:{node_id}", *neighbors)
# 1 跳
neighbors = r.smembers(f"adj:{node_id}")
# 2 跳（客户端做，注意扇出）
two_hop = set()
for n in neighbors:
    two_hop |= r.smembers(f"adj:{n}")     # N 次 RTT，扇出大时不可接受
```

**拐点很清楚**：

| 场景 | Redis 模拟图 | 原生图 |
| --- | --- | --- |
| 1 跳、QPS 10 万 | ✅ 最优（内存 + O(1)） | 需集群 |
| 2 跳、扇出 < 100 | ⚠️ 可接受（100 次 RTT ≈ 10ms） | 更好 |
| 3 跳以上 | ❌ RTT 数爆炸（10^4 次） | ✅ |
| 需要路径/最短路径 | ❌ 自己做 BFS | ✅ 内置 |
| 需要事务一致性 | ⚠️ Redis 弱 | ✅ |

> 💡 **一句话**：**Redis 模拟图适合「一跳热点」，原生图适合「多跳分析」。** 用 Redis 做 3 跳是在用网络 RTT 换 CPU，必输。

### 方案 D：PostgreSQL 递归 CTE + 物化（折中）

如果数据在 PG、跳数 ≤ 4、且查询模式固定，最优解往往不是上图，而是**把遍历结果物化**：

```sql
-- 物化「部门闭包表」：每行表示 ancestor 能看到 descendant（含自身）
CREATE TABLE dept_closure (
    ancestor_id  BIGINT NOT NULL,
    descendant_id BIGINT NOT NULL,
    depth        INT NOT NULL,
    PRIMARY KEY (ancestor_id, descendant_id)
);
CREATE INDEX idx_closure_desc ON dept_closure(descendant_id);

-- 一个 index range scan 就能拿到全部祖先，替代递归
SELECT report_id
FROM report_acl a
JOIN dept_closure c ON c.ancestor_id = a.subject_id
WHERE a.subject_type = 'DEPT'
  AND c.descendant_id = $myDeptId;
```

**闭包表的代价**：写入时要维护（新增节点要插入 N 行，N = 祖先数），换来的是**读变成一次索引扫描**。组织架构变更不频繁、读多写少的场景，这是性价比最高的方案——**不需要引入新数据库**。

---

## 38.5 什么时候图是刚需，什么时候是炫技

| 判据 | 图是刚需 | 图是炫技 |
| --- | --- | --- |
| **跳数** | 经常 ≥ 4 跳 | 1~2 跳 |
| **路径** | 需要「最短路径/所有路径/环路检测」 | 只要「有没有关系」 |
| **关系类型数** | 多（关注/购买/属于/授权/依赖…） | 单一 |
| **查询模式** | 不确定、探索式（用户随便点） | 固定几条 SQL |
| **数据规模** | 节点千万+、边亿级 | 几万节点 |
| **变更频率** | 关系频繁增删 | 静态 |
| **团队能力** | 有人懂图建模与调优 | 没人会，出问题只能重启 |

**典型「真刚需」场景**：

- 权限递归（组织树 + 角色继承 + 资源授权）
- 知识图谱（实体关系、多跳推理）
- 血缘分析（数据表/任务依赖，影响面分析）
- 反欺诈（资金环路、团伙识别）
- 推荐（二度人脉、共同好友）

**典型「炫技」场景**：

- 「用户-订单」这种 1 跳关系（SQL 加索引就够）
- 「文章-标签」多对多（中间表就够）
- 只有几万条数据的配置关系
- 团队没人会 Cypher，出问题只能重启

> 🩸 **反模式**：为了「技术先进」上图，结果 90% 的查询还是 `MATCH (n {id:$id}) RETURN n`——这种点查在 MySQL 里是 0.2ms，在图库里是 2ms，还多了一套运维。

---

## 38.6 双修对照：Agent 的记忆与知识图谱

| 维度 | 传统系统 | Agent 系统 | 相同 / 差异根因 |
| --- | --- | --- | --- |
| **图用来存什么** | 组织/权限/血缘/商品关系 | **实体-关系记忆**（人、项目、偏好、事件） | 同构，语义更模糊 |
| **节点规模** | 千万级 | 通常小（万~百万），但**增长快** | Agent 记忆高频写入 |
| **关系来源** | 人工建模/业务写入 | **模型抽取**（可能抽错） | 需要置信度与人工校正 |
| **查询方式** | 固定 pattern | **模型生成 Cypher/查询**（可能写错） | 需要查询校验与白名单 |
| **一致性** | 强/最终一致 | 允许不一致，但要**可追溯到原文** | 记忆必须可解释 |
| **删除** | 常规 | **必须支持「忘记」**（合规/纠错） | 级联删除 + 原文引用 |
| **主要风险** | 性能、热点 | **错误记忆被反复放大**（模型把错的当事实） | 置信度 + 过期 + 复核 |

### Agent 记忆该不该上图：判定表

| 问题 | 是 → 加分 | 否 → 减分 |
| --- | --- | --- |
| 需要多跳推理吗？（A 的同事 B 负责的项目 C） | +3 | -2 |
| 关系类型多且会演进吗？ | +2 | -1 |
| 需要「影响面分析」（改 A 会波及谁）吗？ | +3 | -1 |
| 主要是「按 key 取一段记忆」吗？ | -3 | +3 |
| 数据量 < 10 万且查询固定吗？ | -2 | +1 |
| 团队没人会图查询吗？ | -3 | +2 |

**结论参考**：

- **累计 ≥ +4**：值得上图（典型：企业知识助手、多 Agent 协作的项目记忆）。
- **-2 ~ +3**：先用「关系库 + 向量库」组合，别上图。
- **≤ -3**：**不要上图**，用 KV/文档 + 元数据过滤。

### 混合架构（生产上最常见）

```text
          ┌──────────────┐
  写入 →  │ 原始事件/文档  │ → 对象存储（真相，可追溯）
          └──────┬───────┘
                 │ 抽取（模型 + 规则）
        ┌────────┼─────────┐
        ▼        ▼         ▼
   ┌────────┐ ┌──────┐ ┌────────┐
   │ 向量库  │ │ 图库  │ │ 关系库  │
   │语义召回 │ │多跳   │ │结构化事实│
   └────────┘ └──────┘ └────────┘
        └────────┼─────────┘
                 ▼
           检索编排（RRF 融合）
```

> 💡 **别指望一个存储解决所有问题**。Agent 记忆的正确形态通常是**「原文（对象存储）+ 语义索引（向量）+ 关系索引（图/KV）+ 结构化事实（关系库）」**四件套，用统一的 ID 串起来。

---

## 38.7 会在哪炸：六个坑

| # | 坑 | 现象 | 根因 | 怎么查 | 怎么修 |
| --- | --- | --- | --- | --- | --- |
| 1 | **超级节点** | 某个查询卡死，CPU 打满 | 一个节点有几百万条边 | `MATCH (n) RETURN size((n)-->) ORDER BY 1 DESC LIMIT 10` | 拆分超级节点 / 预计算 |
| 2 | **无深度上限的遍历** | 查询永不返回 | `*` 或 `*..` 无界 | 看查询计划/慢日志 | 加 `*1..N` 上限 |
| 3 | **笛卡尔积** | 结果数爆炸、内存 OOM | 多个 `MATCH` 未连接 | `PROFILE` 看 row 数 | 用同一个变量连接 / 加 WHERE |
| 4 | **图库当主库** | 图库挂了业务全停 | 没有权威源 | 架构评审 | 关系库为准，图库是可重建索引 |
| 5 | **抽取错误累积** | Agent 记错事实并反复引用 | 模型抽取无校验 | 抽样核对原文 | 置信度阈值 + 人工复核 + 可删除 |
| 6 | **闭包表维护漏了** | 权限变更后不生效 | 新增节点未更新闭包 | 对账：闭包 vs 实时递归 | 触发器/异步任务 + 对账 |

> 🩸 **第 4 条最致命**：图库往往被当成「主库」用，但它通常没有成熟的备份/恢复/事务体系。**图库应该是「索引」，不是「真相」。**

---

## 38.8 怎么验证：跳数-延迟曲线必须自己测

### 压测脚本（Go，直接跑）

```go
package main

// 目的：测出「本场景下 SQL 递归 vs 图查询」的拐点跳数
// 用法：go run bench.go -dsn ... -neo4j ... -maxDepth 8

import (
    "context"
    "database/sql"
    "fmt"
    "time"

    "github.com/neo4j/neo4j-go-driver/v5/neo4j"
    _ "github.com/lib/pq"
)

func benchSQL(ctx context.Context, db *sql.DB, userID int64, depth int) (time.Duration, int, error) {
    q := `WITH RECURSIVE up AS (
            SELECT dept_id, parent_id, 0 AS depth FROM dept d
            JOIN user_dept ud ON ud.dept_id=d.dept_id WHERE ud.user_id=$1
            UNION ALL
            SELECT p.dept_id,p.parent_id,u.depth+1 FROM dept p
            JOIN up u ON p.dept_id=u.parent_id WHERE u.depth < $2)
          SELECT count(*) FROM up`
    start := time.Now()
    var n int
    err := db.QueryRowContext(ctx, q, userID, depth).Scan(&n)
    return time.Since(start), n, err
}

func benchNeo4j(ctx context.Context, drv neo4j.DriverWithContext, userID int64, depth int) (time.Duration, int, error) {
    start := time.Now()
    res, err := neo4j.ExecuteQuery(ctx, drv,
        `MATCH (u:User {id:$id})-[:IN]->(d:Dept)
         MATCH (d)-[:PARENT_OF*0..`+fmt.Sprint(depth)+`]->(a:Dept)
         RETURN count(DISTINCT a) AS c`,
        map[string]any{"id": userID},
        neo4j.EagerResultTransformer)
    if err != nil { return 0, 0, err }
    var n int
    if len(res.Records) > 0 { n = int(res.Records[0].Values[0].(int64)) }
    return time.Since(start), n, nil
}

func main() {
    ctx := context.Background()
    db, _ := sql.Open("postgres", "postgres://...")
    drv, _ := neo4j.NewDriverWithContext("neo4j://...", neo4j.NoAuth())
    for d := 1; d <= 8; d++ {
        sd, sn, _ := benchSQL(ctx, db, 1001, d)
        nd, nn, _ := benchNeo4j(ctx, drv, 1001, d)
        fmt.Printf("depth=%d  sql=%v(rows=%d)  neo4j=%v(rows=%d)\n", d, sd, sn, nd, nn)
    }
}
```

**判据**：

```text
在你真实的图（节点数、扇出、深度分布）上跑：
- 若 3 跳 SQL < 100ms 且 5 跳不常用 → 不上图
- 若 4 跳 SQL > 500ms，或 5 跳以上是常见查询 → 上图
- 若 1 跳 QPS 极高 → Redis/本地缓存，图库也不合适
```

### 一致性验证（闭包表 vs 实时递归）

```sql
-- 每天跑：闭包表与实时递归的差异（应为 0）
WITH RECURSIVE live AS (
    SELECT dept_id AS ancestor_id, dept_id AS descendant_id, 0 AS depth FROM dept
    UNION ALL
    SELECT l.ancestor_id, p.dept_id, l.depth+1
    FROM live l JOIN dept p ON p.parent_id = l.descendant_id
    WHERE l.depth < 12
)
SELECT (SELECT count(*) FROM live) AS live_cnt,
       (SELECT count(*) FROM dept_closure) AS closure_cnt,
       (SELECT count(*) FROM live l
        LEFT JOIN dept_closure c
          ON c.ancestor_id=l.ancestor_id AND c.descendant_id=l.descendant_id
        WHERE c.ancestor_id IS NULL) AS missing_in_closure;
```

---

## 38.9 用什么工具

| 需求 | 首选 | 备选 | 说明 |
| --- | --- | --- | --- |
| 1~3 跳、已有 PG | PG 邻接表 + 递归 CTE / 闭包表 | — | 最省事 |
| 多跳 + 灵活查询 | Neo4j | NebulaGraph | Neo4j 生态最好；Nebula 分布式强 |
| 超大规模（百亿边） | NebulaGraph / JanusGraph + HBase | — | 运维成本高 |
| 云托管 | Amazon Neptune | Neo4j Aura | 省运维、贵 |
| 内存图 / 算法 | NetworkX、igraph、Spark GraphX | — | 离线分析 |
| Redis 图 | **不要用 RedisGraph（已 EOL）** | FalkorDB（社区分支） | 生态与支持有限，谨慎 |
| 知识图谱 + 推理 | RDF 三元组库（Jena/Virtuoso） | 属性图 | 需要本体推理才选 RDF |

---

## 38.10 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 本质 | 图把「关系」当一等公民；优势来自**索引无关邻接**，不是「更先进」 |
| 拐点 | 1~3 跳 SQL 够用；**4 跳以上**或需要路径/环路 → 图库 |
| 中间结果 | SQL 递归的中间结果 ≈ 扇出^深度，这是爆炸的根源 |
| 三道闸 | 深度上限 + 租户条件 + 超时；少一个就会连环事故 |
| 闭包表 | 读多写少、跳数固定的最优解，**不必上图** |
| KV 模拟图 | 只适合 1 跳热点；2 跳勉强，3 跳必输（RTT 爆炸） |
| 超级节点 | 图库第一号性能杀手，必须拆分/预计算 |
| 定位 | **图库是索引，不是真相**；真相在关系库/原文 |
| Agent | 记忆上图要过判定表；多跳推理/影响面才是刚需，否则用向量+元数据 |
| 双修 | 关系建模与遍历原理通用；Agent 多了「抽取错误」「必须可遗忘」 |

## 38.11 自检题

1. **「我们想做二度人脉推荐，用 MySQL 加索引不就行了？」你怎么判断？**
   > 要点：先看扇出与跳数。若平均好友 200 人，二度 = 4 万次索引查找 + 去重，单次 50~200ms 可接受；若扇出 2,000，二度 = 400 万次，必炸。判据：跑一下 §38.8 的 bench，看 2 跳 P99 是否 <100ms；同时看是否需要「共同好友排序」这类图算法（内置 vs 自己写）。不必为了「先进」上图，但也不要硬扛。

2. **为什么「无深度上限的遍历」是最危险的写法？**
   > 要点：① 有环时会死循环（组织树、依赖关系常有环）；② 无环但扇出大时中间结果指数膨胀；③ 会拖垮整个库（连接/CPU/内存），影响其他租户。正解：`*1..N` 上限 + 超时 + 租户过滤，并在应用层限制最大跳数。

3. **Agent 的记忆系统，什么情况下你会选择「图 + 向量 + 关系库」三件套而不是只用一个？**
   > 要点：当记忆同时需要 ① 语义召回（「上次讨论过什么」）② 多跳关系（「这个项目的人还负责哪些」）③ 精确事实（「合同金额 23.8 万」）时。单用向量会丢关系与精确性；单用图做不了模糊召回；单用关系库做不了语义。三件套用统一 ID 串联，原文存对象存储保证可追溯与可删除。

## 38.12 延伸阅读

- 上一章：[37. NoSQL 选型](37-nosql-selection.md)
- 下一章：[39. 多存储下的事务边界](39-multi-storage-transactions.md)
- 相关：[35. 数据模型决定存储](35-storage-model-selection.md)、[36. MySQL 还能不能用](36-mysql-limits-sharding.md)
- 平台侧：[../llm/19-memory-layer.md](../llm/19-memory-layer.md)（记忆分层）、[../llm/20-rag-engineering.md](../llm/20-rag-engineering.md)（GraphRAG）
- 官方文档（以版本为准）：Neo4j Cypher 的变长路径与 `PROFILE`；PostgreSQL `WITH RECURSIVE` 的 `UNION ALL` 语义；NebulaGraph 的 `GO`/`MATCH` 语法差异；RedisGraph 已停止维护（迁移到 FalkorDB 需评估）
- 总纲：[0-summary.md](0-summary.md)
