# 17. MongoDB：BSON、WiredTiger 与 oplog

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · LSM 族与分析 ｜ **编号**：17 / 53
> **本篇定位**：Part IV 的开篇。18 讲 HBase 的 RowKey、19 讲 RocksDB+TiKV、20 讲列存，本篇只讲一件事：**MongoDB 的一次写到底改了哪几份东西（BSON 文档本身、WiredTiger 的 update chain、journal、oplog），以及这四份东西为什么正好决定了「它能给你什么、不能给你什么」**。三个可带走的结论：① 字段名在每个文档里重复付费，schema 命名是真实的存储成本项；② 文档级原子只保一个文档，跨聚合根的一致性得由应用补；③ oplog 能随便重放，是因为 `$inc` 这类更新在落盘时已被改写成幂等的「结果值」形态。注意一个反常识的事实：**WiredTiger 的主存储是 B 树，MongoDB 不在 LSM 这一族里**——它被放进 Part IV，是因为它和 LSM 家族在回答同一个问题：怎样用一份追加日志把随机写摊掉。
> **读法**：§17.1 三起事故先建立「加内存没用 / 切主回滚 / 索引建了不用」的直觉；§17.3 的 BSON 字节图与 §17.4 的 update chain 图是两个物理底座，§17.5 的三家 MVCC 对照表是全篇支点，建议照着声抄一遍；赶时间直接读 §17.8 四笔账 + §17.13 一页速记。被追问「为什么 Mongo 起不了大事务」读 §17.5 与 §17.10，被追问「CDC 位点为什么丢」读 §17.7 与 §17.8 账三。

---

## 17.1 引子：三起「加了内存没用 / 切主后数据回滚 / 索引建了不用」的故障

MongoDB 的事故有个共同点：**没有报错，没有慢查询日志，`rs.status()` 全是 PRIMARY/SECONDARY，面板上一个红点都没有**。变的是数据的**物理形状**（字段名变长、数组变深）、**写关注的语义**（`w:1` 从来没保证过落盘）、**索引的字段顺序**（字典序和你以为的不一样）。这三样东西都不在监控里，只在 `Object.bsonsize()`、`writeConcern`、`explain().winningPlan` 里。

### 事故一：集合 3 个月从 40 GB 涨到 210 GB，加了 64 GB 内存没用（2025-09-08 → 2025-12-11，某 SaaS 工单中心）

`tickets` 集合，3,000 万文档，40 GB，平均文档 1.33 KB。业务侧为了「省事、方便前端直接展示」，把工单的全部信息塞进一个文档：`current_assignment_information: { assigned_engineer_identifier, assignment_timestamp, assignment_reason_code }`，历史流转存成 `workflow_transition_records: [ {transition_from_status_code, transition_to_status_code, transition_operator_identifier, transition_timestamp_iso8601}, … ]` —— 平均 12 个元素，涨到 40 个。

| 时间 | 文档数 | 集合大小 | 平均文档 | WiredTiger cache S 用量 | `tracked dirty bytes` | 工单列表 P99 |
| --- | --- | --- | --- | --- | --- | --- |
| 09-08（上线前） | 3,000 万 | 40 GB | 1.33 KB | 11 GB | 1.4 GB | 48 ms |
| 10-15 | 4,600 万 | 96 GB | 2.09 KB | 26 GB | 4.8 GB | 130 ms |
| 11-20 | 7,200 万 | 168 GB | 2.33 KB | 31 GB（打满） | 9.6 GB | 410 ms |
| 12-05 | 8,400 万 | 197 GB | 2.35 KB | 31 GB | 11 GB | 1,050 ms |
| 12-08 | 加内存 128 GB → cache 上限 63.5 GB，**P99 只降到 720 ms** | 8,700 万 | 203 GB | 58 GB | 17 GB | 720 ms |
| 12-11 | 改短字段名 + 把数组拆成独立集合 + 重建，size 降到 61 GB | 8,700 万 | 61 GB | 19 GB | 2.1 GB | 52 ms |
| 12-12 复盘 | 3 个月扩容费用约 21 万元，改 schema 与重建花 6 人日 | — | — | — | — | — |

**拆开数一遍**：抽样 1 万个文档，平均每个文档里有 **41 个字段名、平均名长 19 B**，这些字段名合计 780 B/文档，占平均文档 2.33 KB 的 **33%**；`workflow_transition_records` 数组里每个元素都重复存一遍它自己的 4 个字段名，仅这一项就是 12×40 ≈ 480 B/文档。加内存为什么只降到 720 ms？因为 grew 的不是「冷数据」，是**每一条都要被读到的热文档**——working set 从 40 GB 变成 197 GB 的解压后足迹，cache 从 31.5 GB 变到 63.5 GB 仍然装不下（推导见 §17.8 账二）。**真正有效的是让文档重新变小**：同一份业务数据，字段名从 19 B 缩到 3 B，文档从 2.33 KB 掉到 0.7 KB，working set 回到 19 GB。

### 事故二：`w:1` + 主库故障，换届后回滚了 6 分钟前的 3.2 万条订单状态（2025-06-27，某跨境电商）

订单状态机跑在 3 节点副本集上（NVMe，`writeConcern: {w:1}`，因为「`w:majority` 太慢，扛不住 1,800 TPS」）。主库在 13:41:07 磁盘只读挂掉，选举在 13:41:19 完成，新主库恢复服务。

| 时间 | 事件 | 主库 oplog 末尾 | 从库 A 已确认 | 从库 B 已确认 |
| --- | --- | --- | --- | --- |
| 13:35–13:40 | 大促，订单状态写入 1,800 TPS，`w:1` | t=…640 | t=…612 | t=…598 |
| 13:41:07 | 主库磁盘故障，进程还能应答应用（但写不进盘） | — | — | — |
| 13:41:08–13:41:19 | 应用继续拿到 `ok:1` 的确认 | t=…712（只在内存/页缓存） | t=…612 | t=…598 |
| 13:41:19 | 从库 A 当选；它末尾是 t=…612 | — | — | — |
| 13:41:19–13:44 | A 回放自己的 oplog，`712−612 ≈ 100` 条 op 之外全部丢弃 | — | — | — |
| 13:44 | A 把 B 上「自己没有的历史」写进 `rollback` 目录 | — | — | — |
| 06-28 对账 | 3.2 万条订单状态回退到旧值，其中 4,100 条已通知仓库发货 ⇒ 对账缺口 **40.3 万元** | — | — | — |

**根因不是副本集不可靠，是「acknowledged」和「durable」被混淆了。** `w:1` 只保证「主库在内存里改完了」，`j:false`（默认）不保证 journal 落盘；而换届时新主库的准绳是**它自己的 oplog 末尾**，凡是没复制到多数派的写，都进 `rollback` 目录。三件事同时缺：写关注没按业务分级、应用侧没有幂等补偿（它把「没收到异常」当成「写成功了」）、对账没有按状态机维度做（只对了金额，没对状态）。

### 事故三：复合索引按「业务习惯」排成 R-E-S，`explain` 里天天 `COLLSCAN` + `SORT`，P99 从 30 ms 到 2.4 s（2026-01-14，某 OTA 订单查询）

运维类查询：`{"order_status": "PAID", "departure_date": {"$gte": …, "$lt": …}}`，按 `created_at` 倒序取 20 条。索引建的是 `{created_at: -1, order_status: 1, departure_date: 1}` —— 顺序来自「页面默认按创建时间倒序，所以把 created_at 放第一个」。

| 时间 | 事件 | `totalKeysExamined` | `totalDocsExamined` | `nReturned` | stage | P99 |
| --- | --- | --- | --- | --- | --- | --- |
| 01-06 | 上线，数据量 800 万，P99 还能忍 | 800 万 | 62 万 | 20 | `IXSCAN` + `FETCH` + `SORT` | 30 ms（假象） |
| 01-10 | 数据量 3,400 万，查询带上状态与日期区间 | 2,100 万 | 480 万 | 20 | 同上 | 1,700 ms |
| 01-14 09:20 | 早高峰，查询并发 40 | 2,100 万 | 480 万 | 20 | 同上 | **2,400 ms**，超时率 7% |
| 01-14 11:05 | 新建 `{order_status: 1, departure_date: 1, created_at: -1}` | 3,180 | 3,180 | 20 | `IXSCAN` + `FETCH`，无 `SORT` | 31 ms |
| 01-14 11:40 | 旧索引还能被别的查询用上（排查中） | — | — | — | — | — |
| 01-15 复盘 | 4.5 小时超时查询 3.1 万次，客服工单 900 件 | — | — | — | — | — |

**24 小时的数据量增长背不动 80 倍的延迟：真正的 80 倍在索引顺序上。** `created_at` 打头意味着索引是「按时间切的字典序」，`order_status = "PAID"` 这个等值条件在 2,100 万条里只能当过滤用（索引里连续但散落），日期区间更是跨全部时间片；于是引擎必须把 2,100 万条全部捞出、回表 480 万条、在内存里 `SORT`。改成 **E→S→R**（等值 → 排序 → 范围）后，索引变成「(PAID, 日期升序) 的连续片段，片段内按 created_at 倒序」，`LIMIT 20` 走 20 条就停。这条规则的物理来源与 MySQL 的最左前缀完全同源，见 §17.6 与 `→ comp/04`。

---

## 17.2 一句话定位与唯一真问题

**定位**：MongoDB = **「BSON 文档模型 + WiredTiger B 树存储 + 副本集 oplog 复制」**的三层组合。它的"三个不是"：不是一个关系库（没有 join、没有跨文档外键约束）、不是一个 LSM（写不追加、靠 B 树原位改 + journal）、不是一个强一致系统（默认 `readConcern: local` / `writeConcern: w:1`）。

### 唯一真问题

> **MongoDB 的「文档级原子」只保一个文档——它给你的是「一个聚合根一次写完」的便利，代价是跨聚合根的一致性必须自己在应用层补；而它敢让你随便重放 oplog，是因为 `$inc` 这类更新在落 oplog 时已经被改写成幂等形态。**

这句话有两个推论，正好是全篇两半：

1. **文档是原子性的边界，也是性能的边界。** 一个文档内的所有修改（含数组、子文档）无条件原子（WiredTiger 层面是「一个 update chain 上的一次提交」）；两个文档之间，即使在同一集合里，4.0 之前什么保证都没有，4.0/4.2 之后有事务但要付「锁 + 快照 + oplog 窗口」的账单（§17.10）。所以「内嵌还是拆表」不是一个风格问题，是**你要不要原子性**的问题。
2. **复制安全来自 oplog 的幂等形态，而不是来自「操作可重放」。** 只有「记录结果值」的操作才能重放任意多次，所以 MongoDB 服务端把非幂等的修饰符（`$inc`/`$mul`/`$pop`）在落 oplog 时改写成结果形态（§17.7）。这也解释了为什么 `changeStream` 的 resume token 有效期等于 oplog 窗口——**位点是环上的位置，环外的东西不存在**。

### 解剖卡八问：本篇答哪几问

| 问 | 落在哪节 | 一句话答案 |
| --- | --- | --- |
| ① 它承诺什么 | §17.7、§17.10 | 单文档原子 + `w:majority` 后可回滚性收敛；**不**承诺跨文档原子（4.0 前）、不承诺 `w:1` 落盘 |
| ② 数据长什么样 | §17.3、§17.4 | 磁盘：BSON 文档字节流 + WiredTiger B 树页；内存：cache 页 + update chain + journal |
| ③ 一次读写走哪条路 | §17.6、§17.9 | 单分片点查 = 1 次 B 树查找；二级索引 = 2 次；无 shard key = 扇出到所有分片 |
| ④ 并发与一致 | §17.5、§17.7 | 快照隔离（`readConcern`）+ 多数派确认（`w:majority`）+ 回滚窗口 |
| ⑤ 扩展维度 | §17.9 | **分片**（唯一的横向扩法）；副本只买可用性不买写吞吐；单机靠垂直切（拆文档、拆数组）撑 |
| ⑥ 会在哪崩（只写系统级） | §17.10 | 多数派不足 → 无主可用性丧失；rollback 丢写 → 数据不一致；字段名/索引/广播查询 → 成本失控 |
| ⑦ 看哪几个指标 | §17.12 | cache 脏页比、`totalDocsExamined/totalKeysExamined` 比值、oplog 窗口、复制延迟 |
| ⑧ 换掉它的信号 | §17.10 | 需要跨聚合根 ACID 的比例 > 30%、working set 装不进分片后的 cache、查询必须 join |

---

## 17.3 物理形态：BSON 的字段名是重复付费的

BSON 是「长度前缀 + 类型字节 + 字段名 + 值」的二进制序列，设计目标是**可跳过**：解析器不需要知道 schema，读到一个字段名后能靠类型字节和长度直接跳过未知字段。这个「可跳过」是它的全部价值的来源，也是它全部成本的来源——**为了能跳，每个字段都必须自带名字**。

**图 1 · 一个 BSON 文档的字节布局**

```text
一个 BSON 文档：总长在前，字段名在每一个文档里各存一遍

偏移  字节                                          说明
──────────────────────────────────────────────────────────────────────
0     int32 total = 617                            含自身 4 B 与结尾 0x00
4     0x02 │ "order_id\0" │ int32 14 │ "SO-2026-1"   1 B 类型 + cstring + 值
      ▲      ▲
      │      └── 字段名 + \0：每个文档、每个数组元素各付一次
      └── 类型字节：0x01 double  0x02 string  0x03 子文档  0x04 数组
                    0x07 ObjectId  0x08 bool  0x10 int32  0x12 int64
                    0x09 Date(UTC ms)  0x11 Timestamp  0x05 binary
…     同一形状重复 12 次（12 个类型字节 + 12 个名字 + 12 个值）
末尾  0x00                                         文档结束符
──────────────────────────────────────────────────────────────────────
每个字段的固定开销 = 1 B(类型) + len(name) + 1 B(字段名结束符)
库级别每页还有 WiredTiger 自己的页头/key/内部结构开销（见 §17.4）
```

| 结构 | 编码方式 | 关键后果 |
| --- | --- | --- |
| 标量字段 | 1 B 类型 + cstring 名 + 定长/变长值 | 字段名与值都是变长的，但**名在前**，所以「跳过」是 O(1) |
| 子文档 | `0x03` + 名 + 整个子文档（**自带总长与结尾符**） | 嵌套一层 = 多一份自己的 4 B 总长 + N 个字段名；嵌套不省字段名，反而多套壳 |
| 数组 | `0x04` + 名 + 一个「键为 `"0"` `"1"` `"2"`… 的子文档」 | **数组元素就是子文档**，所以每个元素里每个字段名都要重复一遍；1,000 个元素的数组 = 1,000 份字段名 |
| `_id` | 默认 `ObjectId`，12 B | 见下方「近似有序」推导；可换任意类型，但**索引结构与排序语义跟着变** |
| 排序/比较 | 字段类型决定比较规则（`0x10` int32 与 `0x12` int64 可互比，string 与 int 不可） | 索引里同一字段的混合类型会破坏「连续前缀」，最左前缀规则照旧失效 |

### `_id` 的 12 字节：为什么它近似有序，单机内就能当聚簇序

`ObjectId` 是 12 B：**4 B 时间戳（秒，大端）+ 5 B 进程级随机值 + 3 B 自增计数器（大端，随机起点）**。早期规范里那 5 B 是「3 B 机器标识 + 2 B 进程号」，现在统一改为进程启动时生成一次的 5 B 随机值——字节数不变，**语义从「可推导」变成了「不可推导」**，这是为了不泄露主机信息。

- 时间戳在最前面 ⇒ 跨秒的 `_id` **严格按时间递增**；
- 同一秒内计数器自增 ⇒ **同一进程**内严格单调；两个 mongos 各自生成时，同秒内互不有序（各自的随机起点与计数器独立）；
- 于是单机（单进程写）内 `_id` 近似单调递增 ⇒ 插入永远落在 B 树右端 ⇒ 页填充度高、页分裂少。这正是 `comp/05` 里「自增主键优于 UUID」的同一条推导，**MongoDB 用「时间戳打头」把这条纪律做成了默认值**。

反过来：把 `_id` 换成业务侧生成的 UUID 字符串，就同时踩中「更长」（36 B 字符串 vs 12 B）与「随机」（插入散落全树）两条，后果与 `→ comp/05` 事故一完全同构。

```javascript
// 字段名到底占多少：同一份业务数据，两套命名各测一次（mongosh）
const longName = {
  order_identifier: "SO-2026-0001", customer_identifier: "C-88213",
  payment_status_code: "PAID", created_at_timestamp: new Date(),
  shipping_address: { province_name: "浙江省", city_name: "杭州市" }
};
const shortName = {
  oid: "SO-2026-0001", cid: "C-88213", st: "PAID", ts: new Date(),
  addr: { p: "浙江省", c: "杭州市" }
};
print("long  =", Object.bsonsize(longName), "B");   // 约 210 B
print("short =", Object.bsonsize(shortName), "B");  // 约 120 B
print("差值   =", Object.bsonsize(longName) - Object.bsonsize(shortName), "B/文档");
// 1 亿文档 ⇒ 差值 × 1e8 ÷ 1024^3 ≈ 8.4 GiB。压缩能吃掉一部分，但 cache 里是解压后的形态。
```

**压缩能不能救回这笔钱？** 部分能：snappy/zstd 是**块级**压缩，同一块里重复的字段名会被压得很好，磁盘上通常能省 50%–80%。但三件事逃不掉：① cache 里被访问的页是**解压后**的足迹，working set 按未压缩字节算（§17.8 账二就是按这个口径算的）；② 网络上一份文档就是一个消息体，压缩只发生在协议层（`OP_COMPRESSED`，需客户端显式开启），默认往往没开；③ 索引里**也存字段名**——索引键就是字段路径，多重索引键会让字段名成本翻倍。所以「用短字段名」是一个真实的成本优化，只是它的收益落在内存和 IOPS 上，不落在磁盘容量上。

---

## 17.4 WiredTiger：缓存内原位更新 + update chain + checkpoint

WiredTiger 是 MongoDB 3.0 起的默认引擎。它和 InnoDB 像（都是 B 树、都有页、都有 WAL），但**写路径差的正是最关键的那一步**：InnoDB 把旧版本挪到 undo，然后允许脏页慢慢刷回聚簇索引叶子（原地改盘上页）；WiredTiger **永远不就地改盘上的块**，它在 cache 里改，攒到 checkpoint 时把整棵树的当前状态写成一个新的、一致的盘上形态。

**图 2 · update chain 与 checkpoint：内存里一条链，磁盘上一张快照**

```text
同一份数据的两个世界：cache 里是一条 update chain，磁盘上是一次 checkpoint

cache（页内原位置更新：写只改内存，不碰盘）
┌──────────────────────────────────────────────────────────────┐
│ key="SO-1" ├─► update #3 {status:"SHIPPED"}  最新，普通读者从这走
│            ├─► update #2 {status:"PAID"}     ◄─ 老快照的读者沿链下溯
│            └─► update #1 {status:"CREATED"}
│ key="SO-2" ├─► update #1 {status:"CREATED"}
└──────────────────────┬───────────────────────────────────────┘
                       │ checkpoint（wiredTiger 周期落盘，默认量级 60 s）
                       │ 写新块 + 更新元数据指针，旧块不就地覆盖
                       ▼
┌──────────────────────────────────────────────────────────────┐
│ 磁盘 .wt：一棵完整的、某一刻一致的 B 树（含全部索引）            │
└──────────────────────┬───────────────────────────────────────┘
                       │ 崩溃恢复 = 最近一次 checkpoint + 重放其后的 journal
                       ▼
┌──────────────────────────────────────────────────────────────┐
│ journal（WAL，追加写；writeConcern:{j:true} 才等 fsync 返回）    │
│ t0 ── t1 ── t2 ── … ── tN   ← 这段长度 = 写入速率 × checkpoint 间隔│
└──────────────────────────────────────────────────────────────┘
```

| 机制 | 它改变了哪条物理路径 | 直接推出的后果 |
| --- | --- | --- |
| **B 树 + 页**（不是 LSM） | 读是「从 root 走到叶子」，写入 cache 后在页内加一条 update；没有 compaction | 读放大低（点查 3 层次量级）、写放大低；但**没有「顺序追加」这个便宜**，随机更新最终还是要靠 checkpoint 把脏页写出去 |
| **update chain** | 每个 key 挂一条「最新→最旧」的链表，旧值不落盘、留在内存 | 快照读沿链回溯（链越长越慢）；链的长度 = 一个 key 在 checkpoint 间隔内被改的次数 ⇒ **热点 key 高频更新会撑大内存** |
| **checkpoint** | 周期把整棵树的一致形态写到新块，形成恢复起点 | 恢复时间 ≈ 重放「上次 checkpoint 之后」的 journal；间隔越大恢复越慢，间隔越小写放大越大（每次都要把脏页全写下） |
| **journal（WAL）** | 每次写先追加一条日志，`j:true` 时 fsync | `w:1`+`j:false` = 只在内存里生效，掉电必丢；`j:true` 才是「已经落盘」的唯一含义 |
| **块级压缩**（snappy 默认，zstd 可选） | 只压盘上的块，读时解压进 cache | 省盘省 IO 带宽，**不省 cache**；working set 要按解压后足迹估 |
| **cache 上限** | `wiredTiger.engineConfig.cacheSizeGB` 默认 ≈ (RAM − 1 GB) × 50%，下限 256 MB | working set 装不进 cache 时，**应用线程自己去做 eviction**（延迟尖刺、P99 直接翻倍），加内存就成为唯一短期解——直到它也装不下 |

**checkpoint 间隔与「崩溃要重放多少 journal」的关系**，一句话：`待重放字节 ≈ 写入速率 × checkpoint 间隔`。按 40 MB/s 与 60 s 量级算 = **2.4 GB**，这是崩溃恢复时必须读一遍并重放的数据量，也是「恢复要多久」的分母。把间隔从 60 s 调到 15 s，恢复量降到 600 MB，但每次 checkpoint 都要把所有脏页写一遍 ⇒ 写放大与后台 IO 上升；调到 300 s，恢复量涨到 12 GB，一次异常重启可能十几分钟起不来。**这不是调参题，是「恢复时间目标 RTO」与「后台写 IO」的对赌。**

一个容易被忽略的连带效应：checkpoint 间隔同时决定了 **update chain 能有多长**（链上的旧值在 checkpoint 之前不能丢，因为可能有老快照要读），所以「长事务」在 MongoDB 上的表现是 `tracked dirty bytes` 涨 + 更新密集的 key 链变长，而不像 InnoDB 那样表现为 undo 表空间涨。三家对照见下一节。

---

## 17.5 三家 MVCC 对照：PG 的堆、InnoDB 的 undo、WiredTiger 的链

同一个目标（读不阻塞写、写不阻塞读、每个事务看见一致的快照），三家把「旧版本」放在了三个不同的地方。**旧版本放哪里，决定了你运维时清理什么、长事务撑爆什么、崩溃恢复重放什么。**

**图 3 · 三条版本链的物理位置**（PG 与 InnoDB 的细节见 `→ comp/11` §11.5、`→ comp/06`）

```text
PG：旧版本就在堆页里，各版本平权
  heap blk 8842 ┌ lp7 [xmin=100, xmax=150] ┐
                └ lp9 [xmin=150, xmax=0  ] ┘   ← 两个独立的元组，靠 xmin/xmax 判可见

InnoDB：盘上只有当前版本，旧版本在 undo 表空间
  聚簇叶 row(id=42) [DB_TRX_ID=150][DB_ROLL_PTR]──► undo: 前像(trx150) ──► 前像(trx100)

WiredTiger：盘上是 checkpoint 快照，内存里是一条链
  cache page ─► key="SO-1" ─► #3 最新 ─► #2 ─► #1 最旧
  读：拿事务的快照（MVCC id）沿链下溯，找到自己可见的那一版
  写：直接往链头 push 一条，不动盘
  checkpoint：把「此刻链头的样子」写成一棵新的一致树
```

| 维度 | PostgreSQL（堆内版本） | InnoDB（undo 版本） | WiredTiger（cache 内原位 + update chain） |
| --- | --- | --- | --- |
| **旧版本存哪** | 堆页里的独立元组（`xmin/xmax` 标记） | undo 表空间里的前像（`DB_ROLL_PTR` 链） | **内存里的 update chain**（链在 cache 页上，不在盘上） |
| **谁负责清理** | `VACUUM` / autovacuum（扫堆 + 清索引 + freeze） | purge 线程（沿 history list 清 undo） | **checkpoint 之后由 eviction 丢弃**：链上无人再需要的旧值随脏页写出而消失，没有独立「清理器」 |
| **被什么钉住** | 最老快照（`backend_xmin`、复制槽、prepared xact） | 最老 Read View（含只读事务） | 最老事务的快照 id（长事务/长 `$setWindowFields` 类操作）+ **checkpoint 间隔**：链上旧值在 checkpoint 前不能回收 |
| **膨胀长什么样** | 表与索引文件膨胀，VM 不置位，index-only 退化 | undo 表空间涨、`history list length` 涨、purge 落后 | **cache 里的脏页与链长度涨**（`tracked dirty bytes` 高、eviction 压力大、应用线程被拖去做 evict）；盘不一定涨，但**延迟一定涨** |
| **崩溃恢复要重放什么** | WAL 重放 + hint bit 重建 + 之后仍留大量死元组等 VACUUM | redo 重放 + 用 undo 回滚未提交事务 | **最近 checkpoint + 之后的 journal**；链上的未 checkpoint 修改全在 journal 里 |
| 更新方式 | 插入新版本，旧版本留原页/新页 | 叶子原地改，旧值写 undo | **只在内存里 push 一条链节点**，盘上等 checkpoint |
| 回滚成本 | 便宜（标 aborted，等清理） | 贵（反向应用 undo，大事务可跑几小时） | 中等（沿链丢弃前缀，取决于链长与是否已 checkpoint） |
| 读旧版本的成本 | 不沿链（索引给 ctid，不可见就跳过） | 沿 undo 链回溯，链越深越慢 | 沿内存链下溯，链越长越慢——**但链在内存里，没有盘 IO** |

三条推论，可以直接搬进评审：

1. **PG 的空间账最显性**（表会变大，`du` 看得见），**InnoDB 的账在 undo 表空间上**（盘会涨），**WiredTiger 的账在 cache 上**（盘看不出，延迟看得出）。所以「MongoDB 为什么加内存有用」和「MySQL 为什么加内存只对 buffer pool 命中率有用」是两个不同的因果链。
2. **长事务在 WiredTiger 上不会撑爆盘，会撑爆 cache 与延迟**：链上的旧值必须留在内存里等 checkpoint，长快照又让 checkpoint 无法回收，于是脏页比例上升 → 应用线程被拉去做 eviction → P99 尖刺。这与 `→ comp/06` 的「长事务撑爆 undo」是同一件事在另一种物理形态下的表现。
3. **WiredTiger 的隔离级别是快照隔离**，粒度由 `readConcern` 决定：`local`（默认，读最新已提交且不保证不被回滚）、`majority`（读多数派已确认的数据，**抗回滚**）、`linearizable`、`snapshot`（事务内，4.0+）。**注意 `local` 的语义：它不保证你读到的写不会在换届后消失**——这是事故二里「读到了不存在的历史」的那一半。

---

## 17.6 索引与 explain：RecordId 回表与 ESR

MongoDB 的索引形态与 InnoDB 有一个决定性差异：**二级索引的叶子里存的是 `RecordId`（集合内部记录的地址标识），不是主键值。** 这意味着「回表」不再是「走另一棵聚簇树」，而是一次**独立的 B 树点查**——代价量级一样（3 层次量级），但语义不同：`_id` 改了不会重写二级索引，而 `RecordId` 在文档被移动（如 `compact`、某些更新导致重写）时会变。

| 事实 | 推出什么 |
| --- | --- |
| `_id` 索引永远存在，不可删 | 每个集合天然有一棵 B 树；`_id` 的选择直接决定插入是否落在树右端（§17.3） |
| 二级索引叶子 = 索引键 + `RecordId` | 一次 `IXSCAN` 命中 N 条 = N 次独立回表查找；**覆盖查询**（所有返回字段都在索引里）才免掉这笔 |
| 索引键里带上 `_id` 的成本很低（12 B） | 想覆盖查询时，把 `_id` 之外的字段都塞进索引键，比 MySQL 里「把主键抄进二级索引」更便宜 |
| 覆盖查询要求返回字段**全部**来自索引，且显式排除 `_id` | 少了 `{_id: 0}` 就会多一次回表——最常见的「我明明建了覆盖索引」的翻车点 |
| 多键索引（数组字段）每个数组元素一条索引项 | 数组越大，索引越大；1,000 元素数组 = 1,000 个索引项 + 1,000 份字段名 |
| 索引也有自己的 B 树与字段名 | `totalIndexSize` 是独立的容量项，§17.8 账二里要一起算 |

### ESR 规则：等值 → 排序 → 范围

复合索引的键顺序只能有一个正确答案，规则是 **E（Equality）→ S（Sort）→ R（Range）**：

| 顺序 | 放在第几位 | 为什么 | 反例后果 |
| --- | --- | --- | --- |
| **E 等值字段**（`$eq`/`$in`） | 最左，可以有多个 | 等值条件把索引切成一个「连续片段」，片段内后续字段才可能有序 | E 放后面 ⇒ 索引扫描要跨全部分片，`totalKeysExamined` 爆炸 |
| **S 排序字段** | 紧跟等值之后，方向与查询一致 | `SORT` stage 需要把结果全捞出来再排；排序字段在等值之后才在索引里天然有序 | S 放最后 ⇒ 一定出现 `SORT` stage，`LIMIT 20` 也救不了（要排完全部） |
| **R 范围字段**（`$gt/$gte/$lt/$lte`） | 最后 | 范围条件一旦出现在某字段上，**它之后的字段在索引里不再有序** | R 放中间 ⇒ 后面字段只能当过滤用，且区间越宽越接近全扫 |

**与 MySQL 最左前缀的关系**：物理原因完全相同——B 树的叶子是「按 `(k1,k2,k3)` 字典序」排的，所以「等值打头」等价于「前缀固定」，同一个前缀内的记录才按 `k2` 有序（`→ comp/04` §04.5 有形式化证明与 9 种条件组合的判定表）。**MongoDB 只多一条纪律：把「排序」显式当成索引设计的一等公民**（因为 MySQL 的 `ORDER BY` 在索引有序时可以免费拿到，而 MongoDB 的 `SORT` stage 是一个会写进 `explain` 的显式内存排序，还有 32 MB 的默认内存上限，超了会报错——`allowDiskUse` 与内存上限的具体口径以该版本 `explain` 输出与文档为准）。

### `explain()` 看到哪个 stage，该做什么

| stage | 它意味着 | 该做什么 |
| --- | --- | --- |
| `COLLSCAN` | 没有可用索引，逐文档扫描 | 检查条件字段是否被索引、类型是否匹配（string vs int 不互比）、是否有 `$where`/`$regex`(无前缀) |
| `IXSCAN` | 走了索引 | 看 `totalKeysExamined` 与 `nReturned` 的比值：接近 1 是好索引，差 10 倍以上说明「索引前缀没吃到」 |
| `FETCH` | 用 `RecordId` 回表取文档 | 看 `totalDocsExamined`：等于 `nReturned` 最好；远大于则是「索引筛选度不够」 |
| `SORT` | 内存排序，说明索引没提供顺序 | 按 ESR 调整键顺序；或接受小结果集排序但要确认没超内存上限 |
| `SORT_KEY_GENERATOR` | 排序前先算键 | 通常和 `SORT` 一起出现，是「表达式排序」的信号 |
| `PROJECTION_COVERED` / 无 `FETCH` | 覆盖查询 | 好事，确认 `_id` 是否被显式排除 |
| `SHARD_MERGE` + `SHARDING_FILTER` | mongos 汇总多个分片结果 | 说明查询**没带 shard key** 或带了范围（§17.9 广播成本） |
| `IDHACK` | 按 `_id` 精确点查 | 最快路径；如果 `IDHACK` 没出现而你查的就是 `_id`，检查类型（`ObjectId` vs 字符串） |

```javascript
// 一次 explain 的最小实验：先看 winningPlan 的 stage 链，再看比值（mongosh）
db.orders.createIndex({ order_status: 1, departure_date: 1, created_at: -1 });
const q  = { order_status: "PAID", departure_date: { $gte: ISODate("2026-02-01"), $lt: ISODate("2026-02-08") } };
const ex = db.orders.find(q).sort({ created_at: -1 }).limit(20)
             .explain("executionStats");
// 1) stage 链：期望 IXSCAN → FETCH（没有 SORT）
const walk = (p) => p.stage + (p.inputStage ? " → " + walk(p.inputStage) : "");
print("winning :", walk(ex.queryPlanner.winningPlan));
print("rejected:", ex.queryPlanner.rejectedPlans.map(walk));   // ← 别只看 winning
// 2) 三个数：键数、文档数、返回数
const s = ex.executionStats;
print("keys/docs/returned:", s.totalKeysExamined, s.totalDocsExamined, s.nReturned);
print("selectivity =", (s.totalDocsExamined / Math.max(s.nReturned, 1)).toFixed(1), "x"); // 1~2 好
```

**`rejectedPlans` 为什么必看**：优化器是「按代价选计划」，它算错的典型姿势是「统计信息过期」或者「索引基数字段全是同一个值」。看到被拒的计划里有一个 `IXSCAN` 而你实际跑的是 `COLLSCAN`，说明代价估算错了（往往要 `hint()` 或更新统计），而不是索引不存在。

---

## 17.7 oplog：为什么重放是安全的

`oplog.rs` 是一个 **capped collection**（固定大小的环形集合，写在 `local` 库），按时间顺序记录**所有数据变更**。副本集的每个成员都有一份自己的 oplog；从库从主库拉取 oplog 条目并**原样应用**（不重新执行你的查询条件，只用 oplog 里记录的东西定位并修改）。

这就带来一个硬要求：**条目必须能被应用任意多次而结果不变**，也就是幂等。`$inc` 显然不满足——重放两次就多加了两次。所以服务端在落 oplog 时会把这类「动作」改写成「结果」：

| 你发的操作 | 落在 oplog 里的形态 | 为什么重放安全 |
| --- | --- | --- |
| `$set` / `$unset` | 直接就是结果值（`$set` 语义天然幂等） | 重放 N 次结果相同 |
| `$inc` / `$mul` / `$pop` / `$push` 等非幂等修饰符 | **由服务端改写成幂等 op**：早期以修饰符形态记录（`$v:1` 一类），较新版本用「增量/结果值」形态（带 `$v` 标记的 diff 结构，记录字段的**新值**） | 记录的是「最终值」而不是「加 1 这个动作」，重放多少次都收敛到同一个文档状态 |
| `insert` | 完整文档 | 重放会撞 `_id` 重复键，但副本应用路径按「已存在则跳过/视为成功」处理 |
| `delete` | `{q: {_id: …}, limit: 1}` | 按 `_id` 删，删两次第二次匹配 0 条，结果相同 |
| `updateOne` 匹配多条 | 主库执行后为**每条被改的文档**各生成一条 oplog | 从库不需要重跑 predicate，避免了「从库扫描时刻不同 ⇒ 命中行不同」这类不一致（对比 `→ comp/06` 事故二里 STATEMENT 格式的翻车） |

> **纪律**：不同版本的 oplog 内部字段形态确实变过（这正是 `$v` 字段存在的意义：让新旧版本知道怎么解释条目）。凡是具体到「哪个字段装什么」，一律以该版本 `db.getSiblingDB("local").oplog.rs.findOne()` 的实际输出为准，任何文章里的字段名都可能过时。

**图 4 · oplog 是环，回滚窗口由「多数派确认点」决定**

```text
oplog 是固定长度的环；环外的东西不存在

主库 ──oplog──► 从库 A（已确认 t=612）      从库 B（落后，只有 t=598）
                   │
  写 t=613..t=712 时  w:1        → 主库自己 ack，A/B 都还没确认
  写 t=613..t=712 时  w:majority → 必须等 A（多数派）确认后才 ack 给应用

  13:41:07 主库故障 → 13:41:19 A 当选
                     │
                     ▼
    A 的 oplog 末尾是 612 ⇒ 613..712 全部进 rollback 目录
    （rollback/<db>.<coll>.bson；回滚历史可用 db.adminCommand({rollbackStatus:1}) 查）

  ┌──────────── 环（大小 = oplog 容量）────────────┐
  旧 ─────────────────────────────────────────► 新   ← 写入端
        ▲
        └─ CDC 的 resume token 落在环外 ⇒ ChangeStreamHistoryLost
           只能重新做一次全量快照（位点语义 → comp/33）
```

| 写关注组合 | 主库掉电（进程崩/断电） | 主库磁盘故障 + 换届 | 网络分区少数派 |
| --- | --- | --- | --- |
| `w:1`, `j:false`（默认） | **丢**（journal 未 fsync，报错都没有） | **回滚**（未到多数派的写进 rollback） | 少数派侧写入被回滚 |
| `w:1`, `j:true` | 不丢（已 fsync 到主库本地 journal） | **回滚**（本地持久但未复制 → 换届后仍是「没发生过」） | 同上 |
| `w:"majority"`, `j:false` | **丢**（多数派在内存里确认，全部掉电则丢） | 不回滚（多数派已有，新主必然包含） | 少数派写不进去（拿不到多数派） |
| `w:"majority"`, `j:true` | 不丢 | 不回滚 | 少数派写不进去 |
| `w:1` + `readConcern: local` 读 | — | **读到「不存在的历史」**：读到了一个会被回滚的值 | 同左 |

**这张表的用法**：`durable` 只有一个定义——`j:true`（落盘）且 `w:majority`（落多数派）。只写 `j:true` 买的是「单机不丢」，不是「集群不丢」；只写 `w:majority` 买的是「不被回滚」，不是「不掉电不丢」。事故二买的是最便宜的那一档，然后按最贵的那一档做业务假设。

**`changeStream` 的位点语义**：resume token 指向 oplog 里的一个位置，`resumeAfter` 从该位置之后继续。它**只在 oplog 窗口内有效**——窗口是「环的长度 ÷ 写入速率」，一旦目标 op 被环覆盖，服务端只能抛 `ChangeStreamHistoryLost`，你唯一的选择是重做全量。这决定了 CDC 的运维纪律：**先算 oplog 窗口，再定「消费者允许停机多久」**（§17.8 账三）。

---

## 17.8 三本账：字段名、working set 与 oplog 窗口

写放大、读放大、空间放大三本账在 MongoDB 上的具体形状是：**字段名 → 空间放大**、**回表 → 读放大**、**checkpoint + oplog + 索引 → 写放大**。

### 账一：字段名的钱（空间放大，也顺带是 cache 账）

假设：1 亿文档、平均 12 个字段、平均字段名 9 B、平均字段值 40 B、无嵌套、无数组。

| 项 | 每字段 | 每文档（12 字段） | 1 亿文档合计 |
| --- | --- | --- | --- |
| 类型字节 | 1 B | 12 B | 1.2 GB |
| **字段名** | **9 B** | **108 B** | **10.8 GB** |
| 字段名结束符 `\0` | 1 B | 12 B | 1.2 GB |
| 字段值 | 40 B | 480 B | 48.0 GB |
| 文档总长/结束符 | — | 5 B | 0.5 GB |
| **合计** | 51 B | **617 B** | **61.7 GB** |

- **纯字段名 = 1e8 × 12 × 9 B = 1.08e10 B = 10.8 GB（≈10.1 GiB），占文档总量 10.8 / 61.7 = 17.5%**；把类型字节与结束符也算进「结构开销」，则是 13.2 GB / 61.7 = **21.4%**。
- 换成 3 字母字段名：每字段结构开销 1+3+1 = 5 B ⇒ 文档 4 + 60 + 480 + 1 = **545 B**，总量 54.5 GB，**省 7.2 GB = 11.7%**；纯字段名从 10.8 GB 降到 3.6 GB。
- 若这份数据是 §17.1 事故一那种形态（41 个字段名、平均 19 B、数组里再重复一遍）：字段名可达 780 B/文档 × 8,700 万 = **63 GB 纯字段名**，这就是 210 GB 里最肥的一块。

> **量级结论**（不是承诺值）。假设清单：12 字段/文档、名长 9 B、值 40 B、无嵌套、doc padding 与 WiredTiger 页头开销忽略（实际会再高 5%–15%）。换任何一个假设请重跑上表；嵌套与数组会让「字段名占比」单调上升，不会下降。

### 账二：文档膨胀与 cache——为什么「必须分片而不是加内存」

假设：单文档 2 KB（事故一后期的量级）、热点集合 5,000 万文档全在 working set 里、写入速率 40 MB/s。

- 逻辑 working set = 5,000 万 × 2 KB = **100 GB**（按**解压后**足迹算；块压缩只减盘不减 cache）。
- cache 上限 = (RAM − 1 GB) × 50%：64 GB 机器 ⇒ (64−1)×0.5 = **31.5 GB ≈ 32 GB**；128 GB ⇒ 63.5 GB；256 GB ⇒ 127.5 GB。
- 缺口：100 / 31.5 = **3.2 倍**。后果不是「慢一点」，是**应用线程自己去做 eviction**（`pages evicted by application threads` 上升）+ 延迟尖刺 + 脏页写不完。
- 加内存的路径：64 GB → 128 GB 仍差 1.6 倍（P99 只从 1,050 ms 降到 720 ms，与事故一的实测吻合）；要到 256 GB 才勉强装下，但那时单机的故障域、恢复时间（重放 journal 更久）、单机 IOPS 都成了新瓶颈。
- 分片路径：6 个分片、按 shard key 均匀切 ⇒ 每片 833 万文档 ⇒ 每片 working set ≈ 16.7 GB < 32 GB cache，**留出 2 倍余量**；代价是跨分片查询变广播（§17.9）。
- 或者把文档改小：字段名缩到 3 B + 数组外置 ⇒ 文档 2 KB → 0.7 KB ⇒ working set 35 GB，单机也能装下（事故一的最终解法）。

> **量级结论**。假设清单：2 KB/文档、无冷热之分（全热）、cache 口径按 (RAM−1 GB)×50%、6 分片均匀分布、每片 cache 与主库同配。若只有 20% 是热数据，working set 按 20% 算，结论会从「必须分片」变成「加内存 + 归档」，请按真实热比复算。

### 账三：oplog 窗口——CDC 停机 5 小时会不会丢位点

- oplog 容量 50 GB，写入速率 40 MB/s（含索引更新与 oplog 自身的写放大）。
- 窗口 = 50 × 1,024 MB / 40 MB/s ≈ 1,280 s ≈ **21 分钟**（按 1 GB = 10^9 B 算 ≈ 20.8 分钟，差别可忽略）。
- CDC 消费端停机 5 小时 = 18,000 s ⇒ 期间产生 18,000 × 40 MB = **720 GB** 的 oplog；环只有 50 GB ⇒ **位点必然被覆盖，`resumeAfter` 一定失败**，只能重做全量快照。
- 要撑 5 小时窗口需要 oplog ≈ 720 GB。把 oplog 从 50 GB 调到 720 GB 的代价：oplog 本身也是 WiredTiger 表，更大的 oplog 会让更多脏页压在 cache 里，等于用内存换窗口。
- MongoDB 4.4 起可用 `db.adminCommand({replSetResizeOplog: 1, size: <MB>})` **在线**调整容量，不必重启；4.4 之前要先下线改配置再重启（降容量的约束以该版本命令文档为准）。
- 正确姿势不是「加 oplog 到 720 GB」，是「oplog 按「允许的最长消费者停机时间 × 写入速率」定，并把 CDC 消费端做成 7×24 不可停的基础设施」。

> **量级结论**。假设清单：写入速率恒定 40 MB/s（峰值会显著压缩窗口）、capped collection 无保留策略、oplog 里只有数据变更（不含心跳类噪声）。真实集群的写入速率有明显日内波动，**按峰值而不是均值定 oplog**。

### 账四：二级索引回表——10 万次 RecordId 查找是什么量级

- `IXSCAN` 命中 10 万条 ⇒ 10 万次回表（每次是一次独立 B 树点查，3 层次量级）。
- 假设 95% 命中 cache：10 万 × 3 页 × 1 µs ≈ **0.3 s**；剩余 5% 落盘（每次 ~100 µs 的 NVMe 随机读）⇒ 10 万 × 3 × 5% × 100 µs ≈ **1.5 s**。
- 合计 ≈ **1.8 s 量级**，且是单查询；并发 20 时直接把 IO 打满。
- 对照：同样的 10 万条如果走覆盖索引（无 `FETCH`）≈ 0.3 s 量级；如果选择率差到 40%（落到 2,000 万条候选），不如直接 `COLLSCAN` 顺序扫（阈值推导见 `→ comp/04` §04.6）。
- 判据：`totalDocsExamined / nReturned` > 10 就该重新设计索引；`totalKeysExamined / nReturned` > 100 说明前缀没吃到。

> **量级结论**。假设清单：3 层次量级树高、cache 命中 95%、NVMe 随机读 100 µs、无并发争用。机械盘或跨可用区存储会把落盘那 5% 放大 5–10 倍。

---

## 17.9 分片：只讲到能算账的程度

分片只回答一个问题：**一次查询要碰几个分片。** 碰 1 个是点查，碰 N 个是广播（`scatter-gather`），代价按 N 倍算再加尾延迟。

**图 5 · shard key 决定一次查询碰几个分片**

```text
带 shard key 的点查                        不带 shard key 的查询（scatter-gather）
app ─► mongos ─► shard A                    app ─► mongos ─┬─► shard A  (全扫/索引扫)
                    │                                       ├─► shard B  (全扫/索引扫)
                    └─ 1 次 B 树查找 (3 层次量级)            ├─► shard C  (全扫/索引扫)
                                                            ├─► shard D  (全扫/索引扫)
       延迟 = 1 个分片的延迟                                └─► shard E  (全扫/索引扫)
                                                            │
       成本 = 1/N 的 IO                          mongos 归并 ←┘ 延迟 = 最慢分片 + 归并
                                                           成本 = 全集群的 IO，且 1/N 的机器
                                                           救不了「最慢那个分片」的尾延迟
```

| 事实 | 版本 | 后果 |
| --- | --- | --- |
| shard key 一旦选定，**不能改键** | 全线 | 选错只能新建集合 + 双写迁移（成本 = 全量重写 + 双跑窗口） |
| `refineCollectionShardKey` 只能**追加后缀字段** | **4.4+** | 修得动「基数不够」，修不动「单调递增」；改完仍要重建索引 |
| chunk 默认大小 64 MB（可调，`sh settings` 里改） | 全线 | chunk 数 ≈ 数据量 / chunk 大小；太小 ⇒ 元数据与迁移开销大，太大 ⇒ 迁移粒度粗、分布不均 |
| balancer 搬 chunk 靠迁移 + 追增量 | 全线 | 迁移期间源/目标分片 IO 双写；高峰期迁移会撞上业务 IO |
| **jumbo chunk**：chunk 超过大小上限但无法分裂 | 全线（4.4+ 有显式 `jumbo` 标记与 `attemptToBalanceJumboChunks` 选项，具体命令与默认行为以该版本为准） | 成因只有两种：① chunk 内**全部文档共享同一个 shard key 值**（键值不可再切，低基数/单调热的直接后果）；② chunk 内文档数超过该版本允许的分裂上限。后果：这个 chunk 永远搬不动 ⇒ 分布永久倾斜 |
| `changeStream` 可在分片集群用 | **4.2+** | 位点语义与副本集相同（oplog 窗口内有效），跨分片顺序按分片内有序、分片间不保证（→ comp/33） |

**广播查询的账**：集合 50 亿文档、6 个分片、每片 8.3 亿；一次不带 shard key 的范围查询每片都要扫 8.3 亿候选 ⇒ 总 IO 与单机 50 亿文档时**完全一样**，只是分摊到 6 台机器上；而 P99 由最慢的那片决定，**尾延迟一点没改善**。分片买到的是「容量与总吞吐线性扩」，不是「单查询变快」——这条把很多人的预期打碎了。

选 shard key 的两条硬纪律（与 `→ comp/18` HBase 的 RowKey 纪律同构）：**不要单调递增**（`created_at`、`ObjectId`、自增 ID 打头 ⇒ 所有新写落在一个 chunk、一个分片 ⇒ 单点热），**不要低基数**（`status` 只有 5 个值 ⇒ 最多 5 个可分裂区间 ⇒ jumbo chunk + 无法均衡）。常用的折中是「高基数 + 非单调」的复合键（如 `{tenant_id: 1, _id: 1}` 或哈希后的业务键），代价是范围查询要按 shard key 前缀来设计。

---

## 17.10 架构边界：单文档原子能挡到哪里

| 能做什么 | 不能做什么 | 靠什么绕过 |
| --- | --- | --- |
| 单文档内任意修改原子（含数组元素、子文档） | 跨文档原子：4.0 前完全没有，4.0（副本集）/4.2（分片）后有事务但要付代价 | 把「必须一起改」的东西放进同一个文档（内嵌）；或上事务并承担 §17.8 之外的锁/快照成本 |
| 单文档写入是「一个 update chain 上的一次提交」，无需锁竞争 | 单文档大小上限 16 MB（与索引键总长共享预算） | 大数组外置成独立集合，用 `$lookup` 或应用侧拼装 |
| 读一致性可选 `readConcern`（`local`/`majority`/`snapshot`） | `local` 读到的写**可能被回滚** | 结算类读一律 `majority`，并可配 `writeConcern: {w:"majority", j:true}` |
| 副本集自动故障转移（多数派还在就能选主） | 多数派不可达 ⇒ 无主，写全停（这是**设计**不是 bug） | 3 副本跨 3 AZ；接受「分区少数派只能读旧数据」 |
| 事务跨集合、跨文档（4.0/4.2+） | 事务寿命默认 60 s（`transactionLifetimeLimitSeconds`），且事务的 oplog 记录必须完整落在 oplog 窗口内 | 事务里只做写、不做长读；把计算挪到事务外 |
| 分片横向扩容量与总吞吐 | 跨分片查询是广播，尾延迟由最慢分片决定 | 查询必须带 shard key；或者建「读模型集合」专门服务这类查询 |

### 单文档内嵌 vs 跨文档事务：判定表

| 场景 | 选择 | 理由（物理层） |
| --- | --- | --- |
| 一个聚合根 + 它的从属明细（订单 + 明细行，且明细总数可控在几十条） | **单文档内嵌** | 一次提交、一个 update chain，天然原子、无锁、无快照成本 |
| 明细无上限增长（工单流转、日志、操作记录） | **拆集合**，单文档只留最新 N 条 | 文档会撞 16 MB 上限、数组越大索引越大、链越改越长（§17.4） |
| 余额扣减 + 流水落账（必须一起生效） | **跨文档事务**（4.0+）或「单文档账户模型」 | 若两者分属不同集合又必须原子，只有事务；更好的做法是把「余额 + 最近流水」放进一个账户文档，流水批量异步落表 |
| 跨租户批量迁移、批量对账 | **不用事务**，改「状态机 + 幂等 + 对账」 | 事务期间持锁 + 长快照会让 cache 与 oplog 双涨；分批 + 幂等更便宜（→ 见 `eng/37` 的选型口径） |
| 需要 join 出报表 | **不用事务**，走 `$lookup` 或把结果物化成读模型集合 | 事务里做大量读 = 长快照 = 脏页涨 + oplog 涨 |

**事务的三条硬代价**（写进设计文档，别写进 PPT）：① **锁**：事务内的写持有文档级意向锁到提交，热点文档会排队；② **快照**：事务级快照钉住 update chain 的旧值，长事务 ⇒ 脏页回收受阻 ⇒ eviction 压力上升；③ **oplog 窗口**：事务的 oplog 条目在提交时作为一个整体写入，它必须完整落在 oplog 里，所以「oplog 窗口 ≥ 最长事务时长 × 若干余量」是硬约束。工程口径上把 oplog 窗口做到 **≥ 72 小时**是给回滚、CDC、事务留余量的常见做法——**这是纪律，不是数据库的承诺值**；真正的上限由 `oplog` 容量与写入速率两个数算出来（§17.8 账三）。

```go
// 会话 + 事务 + 超时：把「事务寿命」变成一个 deadline（mongo-go-driver）
ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
defer cancel()

sess, err := client.StartSession()          // 事务必须挂在 session 上
if err != nil { return err }
defer sess.EndSession(ctx)

boolPtr := func(b bool) *bool { return &b }
wc := writeconcern.Majority()               // 多数派 + journal：durable 的唯一组合
wc.Journal = boolPtr(true)
txnOpts := options.Transaction().
    SetWriteConcern(wc).
    SetReadConcern(readconcern.Snapshot())  // 事务内的读一致性（4.0+）

res, err := sess.WithTransaction(ctx, func(sc mongo.SessionContext) (interface{}, error) {
    // 事务里只做写：把校验、聚合、外部调用全部挪到事务外
    if _, e := accounts.UpdateOne(sc, bson.M{"_id": from, "bal": bson.M{"$gte": amt}},
        bson.M{"$inc": bson.M{"bal": -amt}}); e != nil { return nil, e }
    if _, e := accounts.UpdateOne(sc, bson.M{"_id": to},
        bson.M{"$inc": bson.M{"bal": amt}}); e != nil { return nil, e }
    return nil, nil                        // 返回 nil error ⇒ 提交
}, txnOpts)
if err != nil {
    // TransientTransactionError / UnknownTransactionCommitResult 可重试；
    // 其余错误重试无意义，必须走对账（写关注已保证「不丢」但「可能重复」）
    return err
}
_ = res
```

### ⑤ 扩展维度：四选一，选「分片」

- **分片**：唯一的横向扩法，扩容量与总吞吐；不改善单查询延迟（§17.9）。
- **副本**：只买可用性与读扩展（`readPreference: secondary`），**不买写吞吐**——所有写都进主库。
- **垂直切**（本篇独有的一档）：**把文档改小**（短字段名、数组外置、去掉冗余冗余）在 MongoDB 上是一种真实的扩容手段，因为它直接缩小 working set；这与「加 CPU 加内存」是两种不同的垂直扩展。
- **批处理摊薄**：批量写（`bulkWrite`）与批量读（避免 N+1）摊薄的是网络 RTT 与事务开销，摊薄不了 IO。

### ⑥ 会在哪崩（只写系统级失效）

1. **集群不可用**：多数派不可达 ⇒ 无主、写全停。触发条件是「3 副本里挂 2 个」或「跨 AZ 网络分区切掉了 2 个 AZ」，不是负载高。
2. **数据不一致**：`w:1` 或 `w:majority` 但不配 `j:true` 的组合，在换届/掉电时把「已 ack」的写回滚掉，而应用侧以为成功（事故二）。这是**语义漂移**类失效，不是「丢了几条」。
3. **成本失控**：字段名/嵌套/数组把空间与 working set 放大 2–3 倍；索引数量与多键索引把写入放大；无 shard key 的广播查询把集群 IO 乘以分片数。三条都会让「加机器」变成无底洞。

### ⑧ 换掉它的信号

- 业务里「必须跨 2 个以上文档原子」的写法超过 30%，且都是高频写路径 ⇒ 关系库或 NewSQL 更便宜（→ comp/19）。
- 查询形态必须是 join / 多表聚合 ⇒ 文档模型在硬撑，`$lookup` 会一路拖到内存排序。
- working set 已经超过「分片数 × 单片 cache」且增长曲线仍陡 ⇒ 分片在追着数据跑，考虑列存/分析库分离（→ comp/20）。
- 需要跨文档强一致 + 全局二级索引 + 无热点写入 ⇒ 关系型分库分表或 TiKV 类（→ comp/19）。

---

## 17.11 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| # | 现象 | 根因（指到物理形态） | 怎么定位 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- | --- |
| 1 | 集合 3 个月涨 5 倍，加内存无效 | 字段名每文档重复 + 数组元素重复同一批字段名（§17.3） | `Object.bsonsize()` 抽样、`$bsonSize` 聚合、比对「字段名总字节 / 文档字节」 | 短字段名 + 数组外置 + 重建集合 | schema 评审卡「字段名长度」与「数组上限」，纳入容量模型 |
| 2 | 主库换主后部分写「消失」，应用毫无感知 | `w:1` + `j:false`：ack 只代表内存里改完（§17.7） | `rs.status()` 看 `optimeDate` 落差点、`rollback` 目录、`{rollbackStatus:1}` | 关键写改 `{w:"majority", j:true}`；补幂等与对账 | 写关注按业务分级写进代码模板；对账按状态机维度做 |
| 3 | CDC 断点后无法 `resumeAfter`，只能全量重做 | oplog 环被覆盖（窗口 20 分钟量级） | `db.getReplicationInfo()` 看窗口；消费者 lag 与窗口比 | 4.4+ `replSetResizeOplog` 扩容；或重建快照 | 按「最长允许停机 × 峰值写入」定 oplog；CDC 端点不可停 |
| 4 | 写入全砸在一个分片，其他分片闲着 | shard key 单调递增（`created_at`/`ObjectId`）⇒ 新写永远落在最后一个 chunk | `sh.status()` 看 chunk 分布；`collStats` 看各片文档数 | 4.4+ `refineCollectionShardKey` 只能折中；根治要换键重建 | 选键纪律：高基数 + 非单调（哈希或复合前缀） |
| 5 | 某个 chunk 永远搬不动，balancer 反复重试 | jumbo chunk：全 chunk 共享一个 shard key 值（低基数）或文档数超分裂上限（§17.9） | `sh.status({verbose:true})` 找 `jumbo` 标记；看该 chunk 的键值分布 | 提高键基数后 `splitVector` + 手动 `moveChunk` | 选键前先算「可分裂区间数 = 键基数」 |
| 6 | 查询 P99 高，`explain` 里 `COLLSCAN` | `$where`/无锚定 `$regex` 不能被索引；或类型不匹配（string vs int） | `explain().winningPlan` 看 stage；`totalKeysExamined` 是否 0 | 改等值查询 / 锚定正则 `^prefix` / 统一字段类型 | 禁止 `$where`；正则只允许前缀式；schema 校验器锁类型 |
| 7 | 事务一开，P99 与 `tracked dirty bytes` 一起涨 | 事务长时间持有快照 ⇒ update chain 旧值不能回收 + 锁排队（§17.5） | `db.currentOp()` 找 `secs_running` 大的事务；看 `transaction` 字段 | 事务里只写不读；把读与计算挪出去；`transactionLifetimeLimitSeconds` 收紧 | 事务代码复查三条：只写、短、无外部调用 |
| 8 | 只优化了 `winningPlan`，上线后反而变慢 | 忽略了 `rejectedPlans`：优化器代价估算错（统计过期/基数极低）（§17.6） | `explain("allPlansExecution")` 对比各计划实测 | `hint()` 固定计划；重建统计；必要时改索引前缀 | 变更评审必须贴 `explain("executionStats")` 全量输出 |
| 9 | 分片集群单查询比单机还慢 | 查询不带 shard key ⇒ `scatter-gather`，延迟 = 最慢分片 + 归并（§17.9） | `explain()` 里出现 `SHARD_MERGE` / `SHARDING_FILTER` | 查询带上 shard key，或建读模型集合 | 上分片前先统计「带 shard key 的查询占比」，低于 90% 不上 |

---

## 17.12 观测：指标、拐点与告警

| 指标 | 位置（版本 / 开关） | 拐点 | 告警骨架 |
| --- | --- | --- | --- |
| **`totalDocsExamined / nReturned`**（最有用的单指标） | `explain("executionStats")`，3.0+；需**主动对可疑查询采样**，不是自动全局指标（Atlas 侧看 Query Profiler / 慢查询采样） | > 10 说明索引筛选度不够 | 慢查询日志里比值 > 10 的查询数 > N/分钟 |
| `totalKeysExamined / nReturned` | 同上 | > 100 说明复合索引前缀没吃到 | 同上 |
| `bytes currently in the cache` | `db.serverStatus().wiredTiger.cache`（3.0–7.x 均为该字段名；若该版本键名不同，以 `db.serverStatus().wiredTiger.cache` 实际输出为准） | > 90% × `maximum bytes configured` = 在打满 | 持续 10 分钟 > 90% |
| `tracked dirty bytes` | 同上 | > 5%–10% 的 cache 上限即危险（对应 `eviction_dirty_trigger` 的默认量级，各版本略有差异，以该版本参数输出为准） | > 10% 立即告警：应用线程会开始做 eviction |
| `pages evicted by application threads` | 同上（3.0+） | 从 0 变成持续增长 = cache 已装不下 working set | 5 分钟增量 > 0 即告警（这是「加内存或分片」的硬信号） |
| 长事务 | `db.currentOp({active: true})` 的 `secs_running` + `transaction` 字段（副本集事务 4.0+、分片事务 4.2+ 才有该字段） | > `transactionLifetimeLimitSeconds` 的一半 | 存在 `secs_running > 30` 且带 `transaction` 的 op |
| 复制延迟 | `rs.status().members[n].optimeDate` 与 `lastAppliedWallTime` / `lastDurableWallTime`（**4.4+ 才有后两个**，单位是墙钟毫秒，自己做差）。秒级延迟字段在不同版本命名与存在性不同，以该版本 `rs.status()` 实际输出为准 | 与 oplog 窗口比较：延迟接近窗口 = 即将断流 | 延迟 > 10% 的 oplog 窗口 |
| oplog 窗口 | `db.getReplicationInfo()` 的 `logSizeMB` / `timeDiff` / `timeDiffHours`（估算自 oplog 首尾时间戳，3.0+） | < 2 × 最长消费者停机时长 | 窗口 < 2 小时告警；< 30 分钟告警升级 |
| 索引空间 | `db.coll.stats().totalIndexSize`（3.0+） | 索引 > 数据 1.5 倍 | 环比增长 > 30%/周 |
| 分片均衡 | `sh.status()` 的 chunk 分布与 `jumbo` 标记（4.4+ 有显式 jumbo 字段） | 单分片 chunk 数偏离均值 > 2 倍 | 偏离持续 1 小时 |

```bash
#!/usr/bin/env bash
# MongoDB 巡检五连：cache 水位 / 脏页 / oplog 窗口 / 复制延迟 / 索引占比
mongosh --quiet "$MONGO_URI" --eval '
const s = db.serverStatus();
const c = s.wiredTiger.cache;
const maxB = c["maximum bytes configured"], curB = c["bytes currently in the cache"];
print("== cache ==");
print("  used %      :", (100*curB/maxB).toFixed(1));
print("  dirty %     :", (100*c["tracked dirty bytes"]/maxB).toFixed(1));
print("  evict by app:", c["pages evicted by application threads"]);

print("== oplog ==");
const r = db.getReplicationInfo();
print("  size MB     :", r.logSizeMB);
print("  window hours:", r.timeDiffHours === undefined ? r.timeDiff/3600 : r.timeDiffHours);

print("== repl lag ==");
rs.status().members.forEach(m => print("  ", m.name, m.stateStr,
  m.optimeDate, m.lastAppliedWallTime ? m.lastAppliedWallTime.toISOString() : "(<4.4)"));

print("== 集合 / 索引 ==");
db.getSiblingDB("app").getCollectionNames().forEach(n => {
  const st = db.getSiblingDB("app").getCollection(n).stats();
  print("  ", n, "data MB:", (st.size/1048576).toFixed(0),
        "index MB:", (st.totalIndexSize/1048576).toFixed(0),
        "ratio:", (st.totalIndexSize/Math.max(st.size,1)).toFixed(2));
});
'
```

**先看哪条线**：`pages evicted by application threads` 从 0 变正 —— 这一条同时说明三件事（working set 超了、延迟尖刺要来了、加内存或分片该立项了）；其次是 `tracked dirty bytes`（checkpoint 跟不上写入）；再次是 oplog 窗口（决定「出事时还能不能救」）。

---

## 17.13 一页速记

| 记什么 | 具体数字 / 结论 |
| --- | --- |
| BSON 结构 | `int32 总长` → 每字段 `1 B 类型 + name + \0` + 值 → 末尾 `0x00` |
| 字段名成本 | 每字段固定 1 + len(name) + 1 B；1 亿 × 12 × 9 B = **10.8 GB 纯字段名**（占 ~17.5%） |
| 数组 | 数组 = 键为 `"0","1",…` 的子文档 ⇒ **每个元素重复一遍字段名** |
| `_id` | 12 B = 4 B 秒级时间戳 + 5 B 进程随机 + 3 B 自增 ⇒ 单机近似单调 = 天然的插入右端 |
| WiredTiger | B 树 + cache 原位改 + **update chain** + 周期 checkpoint + journal；**不是 LSM** |
| cache 上限 | `cacheSizeGB` ≈ (RAM − 1 GB) × 50%，下限 256 MB；装不下 ⇒ 应用线程做 eviction |
| 待重放 journal | ≈ 写入速率 × checkpoint 间隔；40 MB/s × 60 s ≈ 2.4 GB |
| durable | 只有 `{w:"majority", j:true}`；`w:1` = 内存里 ack；`w:1`+`j:true` = 单机不丢，集群还会回滚 |
| 二级索引 | 叶子存 `RecordId` 而非主键 ⇒ 回表是**另一次 B 树查找**（3 层次量级） |
| 索引顺序 | **E → S → R**：等值 → 排序 → 范围；范围之后的字段不再有序 |
| 覆盖查询 | 返回字段全部来自索引 + `{_id: 0}` |
| oplog | capped collection、按时间序、**幂等 op**（`$inc` 被改写成结果值形态，明细以该版本 `oplog.rs` 输出为准） |
| oplog 窗口 | 容量 ÷ 写入速率；50 GB / 40 MB/s ≈ **21 分钟**；4.4+ 可 `replSetResizeOplog` 在线调 |
| 回滚 | 未到多数派的写进 `rollback` 目录；`w:majority` 才免回滚 |
| 分片 | shard key 不可改（4.4+ 只能 `refineCollectionShardKey` 追加后缀）；chunk 默认 64 MB；低基数/单调 ⇒ 热点与 jumbo chunk |
| 事务 | 4.0 副本集 / 4.2 分片；代价 = 锁 + 快照 + oplog 窗口；默认寿命 60 s（`transactionLifetimeLimitSeconds`） |
| 单指标 | `totalDocsExamined / nReturned` > 10 ⇒ 索引不合格 |

**如果只记五句：**

1. **文档是原子性的边界**：一个文档内的修改永远原子，跨文档就要事务（要付锁、快照、oplog 窗口三笔钱）。
2. **字段名在每个文档里重复付费**：短字段名、数组外置、控制嵌套，是真实的成本优化，收益落在 cache 与 IOPS 上而不是磁盘上。
3. **WiredTiger 的旧版本活在内存里**（update chain），checkpoint 之前不能回收 —— 所以长事务在 MongoDB 上表现为「脏页涨 + 延迟尖刺」，不是「盘涨」。
4. **重放安全来自 oplog 记的是结果不是动作**，所以 `$inc` 必须被改写；也因此 `changeStream` 的位点只在 oplog 窗口内有效。
5. **`w:1` 不是 durable**：acknowledged ≠ 落盘，更 ≠ 不被回滚。

---

## 17.14 自检题（5 题，答案折叠）

**1. 一个集合 1 亿文档、平均 12 个字段、字段名平均 9 B、平均值 40 B。纯字段名占多少 GB、占比多少？改用 3 字母字段名能省多少？**
<details><summary>答案</summary>

纯字段名 = 1e8 × 12 × 9 B = 1.08e10 B = **10.8 GB**（≈10.1 GiB）。文档总长 = 4 B 总长 + 12×(1 类型 + 9 名 + 1 结束符) + 12×40 值 + 1 B 结尾 = 4+132+480+1 = **617 B** ⇒ 61.7 GB，字段名占 **17.5%**（若把类型与结束符也算作结构开销则 13.2/61.7 = 21.4%）。

3 字母名：每字段结构 1+3+1 = 5 B ⇒ 文档 545 B ⇒ 54.5 GB，**省 7.2 GB（11.7%）**，纯字段名从 10.8 GB 降到 3.6 GB。

**注意这是量级结论**，假设无嵌套无数组。一旦有数组，每个元素里都要重复一遍字段名，占比会更高——这才是事故一那种「加内存无效」的真正来源。
</details>

**2. `$inc: {stock: -1}` 这类非幂等更新凭什么能安全重放？oplog 里存的是什么？**
<details><summary>答案</summary>

从库不重新执行你发的那条语句（不重跑 predicate、不重跑修饰符语义做判断），而是**原样应用 oplog 条目**。如果条目里存的是「减 1 这个动作」，重放两次就错了。所以服务端在落 oplog 时把它改写成**结果值形态**（较新版本用带 `$v` 标记的增量/diff 结构记录字段的新值；早期版本用修饰符形态记录，并用 `$v` 区分版本），记录的是「最终状态」而不是「动作」，重放任意多次都收敛到同一个结果。**具体字段形态以该版本 `local.oplog.rs` 实际输出为准**——这类内部格式确实在版本间变过。

推论：`changeStream` 的 resume token 指向 oplog 位置，所以它的有效期 = oplog 窗口；窗口不够长就只能 `ChangeStreamHistoryLost` 后全量重做。
</details>

**3. 单文档 2 KB、热点集合 5,000 万文档、机器 64 GB 内存。为什么「加内存」不是答案？**
<details><summary>答案</summary>

working set = 5,000 万 × 2 KB = **100 GB**；cache 上限 = (64−1)×50% = **31.5 GB**，差 3.2 倍。后果不是「慢一点」，而是应用线程被拉去做 eviction（`pages evicted by application threads` 转正）、`tracked dirty bytes` 高、P99 尖刺。

加内存到 128 GB ⇒ cache 63.5 GB，仍差 1.6 倍（实测 P99 只降三成左右）；要到 256 GB 才勉强装下，此时单机故障域、恢复时间、IOPS 都成了新瓶颈。两条正解：① **分片**（6 片 ⇒ 每片 16.7 GB，留 2 倍余量）；② **把文档改小**（字段名缩短 + 数组外置 ⇒ 2 KB 降到 0.7 KB ⇒ working set 35 GB，单机也装得下）。压缩帮不上忙，因为 cache 记的是解压后足迹。
</details>

**4. oplog 50 GB、写入 40 MB/s。窗口多长？CDC 消费端停机 5 小时会不会丢位点？要撑 5 小时需要多大？**
<details><summary>答案</summary>

窗口 = 50×1,024 MB ÷ 40 MB/s ≈ 1,280 s ≈ **21 分钟**（量级结论）。停机 5 小时 = 18,000 s，期间产生 18,000 × 40 MB = **720 GB** 的 oplog，环只有 50 GB ⇒ 位点被覆盖，`resumeAfter` 必然失败，只能重做全量快照。

要撑 5 小时需 oplog ≈ **720 GB**；MongoDB **4.4 起**可用 `db.adminCommand({replSetResizeOplog: 1, size: <MB>})` 在线扩容，但 oplog 也是 WiredTiger 表，更大的 oplog 会把更多脏页压进 cache（用内存换窗口）。

真正的纪律：oplog 按「最长允许消费者停机时长 × **峰值**写入速率」定，且 CDC 消费端必须是 7×24 不可停的基础设施。
</details>

**5. 查询 `{status: "PAID", date: {$gte, $lt}}` 按 `created_at` 倒序取 20 条。索引 `{created_at:-1, status:1, date:1}` 为什么慢？给出正确顺序并说清原因。**
<details><summary>答案</summary>

B 树叶子按 `(created_at, status, date)` 字典序排，`created_at` 打头 ⇒ 索引是「按时间切的连续区段」，`status` 等值条件在每个时间区段里只是散落的标签，`date` 区间更跨全部区段。于是引擎必须把几乎所有条目都过一遍（`totalKeysExamined` 达千万级）、回表（`totalDocsExamined` 百万级）、再 `SORT` 一遍取前 20——`LIMIT 20` 完全救不了，因为排序要看到全部结果。

正确顺序按 **E→S→R**：`{status: 1, date: 1, created_at: -1}`。等值把索引切成一个连续片段，片段内 `date` 有序（范围条件只用一段），片段内 `created_at` 天然有序 ⇒ `LIMIT 20` 走到 20 条就停，无 `SORT`。这跟 MySQL 的最左前缀是同一个物理事实（`→ comp/04` §04.5），MongoDB 只是把「排序」显式化为一个会出现在 `explain` 里的 `SORT` stage。
</details>

---

## 17.15 延伸阅读

| 方向 | 去哪 |
| --- | --- |
| 表就是一棵以主键为键的 B+ 树、页/行格式、主键选的物理后果（本篇 `_id` 的推导同源） | `→ comp/05` |
| undo 版本链与 Read View、长事务为什么撑爆 undo（与 update chain 逐条对照） | `→ comp/06` |
| PG 的堆内版本与 VACUUM：MVCC 的第二种实现（本篇 §17.5 的第一列） | `→ comp/11` |
| B+ 树页、回表与最左前缀的形式化（ESR 规则的同一物理基础） | `→ comp/04` |
| 索引本质、回表与四种索引形态 | `→ comp/04` §04.6 |
| HBase 的 RowKey 设计：同为「单调递增键 = 单点热」的另一副面孔 | `→ comp/18` |
| RocksDB + TiKV + Raft：B 树之外的另一种分布式 KV 组合 | `→ comp/19` |
| 列存与点更新：为什么分析库不能当主库 | `→ comp/20` |
| CDC 位点语义、快照与增量的衔接（changeStream 的上位话题） | `→ comp/33` |
| 五类失效（慢/满/脑裂/假活/语义漂移）与兜底套路 | `→ comp/52` |
| NoSQL 选型矩阵：Mongo / Cassandra / ES / ClickHouse 的匹配与反匹配 | [eng/37 NoSQL 选型](../eng/37-nosql-selection.md) |
| 索引形态决策与写路径放大（选引擎就是选在哪一栏付钱） | `→ architect/03` |
| 全套的尺子与写作公约（八问卡、三本放大账、数字纪律） | [0-summary 总纲](0-summary.md) |
