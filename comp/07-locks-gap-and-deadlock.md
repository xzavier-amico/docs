# 07. 锁：行锁、间隙锁、意向锁与死锁

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · 关系库与事务内核 ｜ **编号**：07 / 53
> **本篇定位**：全仓唯一一处把「锁」拆到索引记录级的地方。[05](05-innodb-page-and-row.md) 讲「表其实是一棵以主键为键的 B+ 树」，[06](06-mvcc-and-isolation-levels.md) 讲「快照读一行锁都不加」——本篇接着讲**当前读要加什么锁、加在哪个对象上、范围有多大**。它不重复 MVCC 的版本链与 Read View（那是 comp/06 的全部内容），也不讲分布式锁（→ comp/46）；它只回答一件事：一句 `UPDATE` 到底锁住了索引上的哪一段区间，以及这段区间为什么会让别人的语句排队、把自己的事务卷进死锁、把一个只要 40 毫秒的 DDL 拖成全站 44 分钟不可用。
> **读法**：§07.2 的唯一真问题是全篇支点，一句话记不住下面所有矩阵都是白背；§07.3 的「锁挂在索引记录上」与 §07.4 的六形态表要能默画，这是评审场上唯一能当场算清「这条语句会锁多少行」的东西；正在救火的直接跳到 §07.8 的三本账与 §07.10 的坑清单；被 DDL 卡死过的人读 §07.6，那里有队列效应的因果链；§07.12 的判据表可以单独贴进研发规范。

---

## 07.1 引子：三起「只改一行，锁了全表 / DDL 卡死全站」的故障

锁的故障有一个共同点：**报错信息永远不指向锁的范围**。报的是 1205 锁等待超时、是 1213 死锁、是 502 网关超时，而真正的原因是「一条语句在索引上划出的区间比你想象的大三个数量级」。三起事故分别对应本篇的三条主线：**加锁范围（§07.3–07.5）**、**元数据锁（§07.6）**、**死锁与重试放大（§07.7–07.8）**。

### 事故一：下单锁库存，锁的不是一个 SKU，而是整个前置仓的 42 万行（2025-09-23）

某社区团购的库存表 `stock`：486 万行（SKU × 前置仓），主键 `id`，二级索引只有 `KEY idx_wh (warehouse_id)`——**`sku_id` 上没有索引**，因为「查询都是按仓库取货，SKU 是次要条件」。

下单链路为了防超卖，写的是最标准的悲观锁：`SELECT id, qty FROM stock WHERE sku_id = 8812 AND warehouse_id = 7 FOR UPDATE;`

优化器的选择没有任何争议：`sku_id` 无索引，只能用 `idx_wh`。于是一条「只想锁 1 行」的语句，把 `warehouse_id = 7` 这个二级索引值区间内的 **42 万条索引记录、42 万条聚簇索引记录、以及该键值到下一个键值之间的间隙**全部加了 next-key 锁。同一个仓里的其他 4.9 万个 SKU 的下单请求，全部排在这一句后面。

| 时间 | 事件 | 被锁行数 | 下单 P99 | 网关 5xx |
| --- | --- | --- | --- | --- |
| 09-23 19:40 | 大促预热，下单 3,200 QPS，稳定 | ~1 行/事务 | 120 ms | 0 |
| 19:58:03 | 首条该 SQL 进入，全仓区间被 next-key 锁住 | 42.1 万 | 210 ms | 0 |
| 19:59 | 同仓下单请求全部排在锁后面；不同仓互不影响 | 42.1 万 | 1,400 ms | 0.3% |
| 20:03 | `innodb_lock_wait_timeout=50` 未到，但网关 3 s 超时先触发，客户端开始重试 | 42.1 万 | 3,100 ms（超时） | 11% |
| 20:08 | 重试把同一句 SQL 的并发请求数放大 2.7 倍，锁队列成链，开始出现 1213 死锁 | 42.1 万 | 3,100 ms | 34% |
| 20:26 | 手工 `KILL` 掉该仓所有下单事务，加索引 `idx_sku_wh (sku_id, warehouse_id)` 后恢复 | — | 140 ms | 0.2% |
| 复盘 | 28 分钟同仓不可下单；失败请求 47.6 万次；GMV 损失约 830 万元；死锁 3,106 次 | — | — | — |

**根因不是「用了悲观锁」，是「锁的对象由索引决定，而索引上根本没有这个 SKU」。** 补上 `(sku_id, warehouse_id)` 这唯一索引之后，同一句 SQL 从「42 万条 next-key」变成「1 条记录锁」（§07.4 形态 ①），加锁范围缩小 42 万倍，代码一行没改。

### 事故二：一条 40 毫秒能跑完的 DDL，把工单系统堵死 44 分钟（2026-01-08）

某 SaaS 工单系统，`ticket` 表 2.1 亿行。需求很简单：加一个可空的 JSON 扩展字段，语句是 `ALTER TABLE ticket ADD COLUMN ext_json JSON NULL, ALGORITHM=INSTANT`。

MySQL 8.0 里这是一次**只改数据字典、不碰任何数据页**的操作，实测耗时 0.04 s。10:02:07 提交，10:45:32 完成，**总共 2,605 s，其中 2,605 s 全在排队**。挡路的是 09:41 开的一个报表事务：`START TRANSACTION` 之后跑了一条跨表 JOIN 导出，之后忘了提交，一直挂着——它持有 `ticket` 上的 `MDL_SHARED_READ`。

| 时间 | 事件 | `metadata_locks` 里 PENDING 数 | 应用连接池占用 | 工单接口 |
| --- | --- | --- | --- | --- |
| 10:02:07 | `ALTER` 提交，请求 `MDL_EXCLUSIVE`，被 09:41 的报表事务挡住 → 进入 PENDING 队头 | 1 | 12/200 | 正常 80 ms |
| 10:02:08 | 后续每条查询都要 `MDL_SHARED_READ`，与队头的 `MDL_EXCLUSIVE` 冲突 → **全部排队** | 480 增长中 | 60/200 | 400 ms |
| 10:02:31 | 连接池 200/200 打满，新请求拿不到连接 | 1.1 万 | 200/200 | 3 s 超时 |
| 10:03:00 | 网关/前端全面 502；只读接口也挂，因为只读也要 MDL | 2.5 万 | 200/200 | 不可用 |
| 10:44:10 | 有人从 `performance_schema.metadata_locks` 反查到 OWNER_THREAD_ID → 09:41 的报表事务 | 85 万 | 200/200 | 不可用 |
| 10:45:31 | `KILL` 报表事务（回滚耗时 41 s） | 85 万 | 200/200 | 不可用 |
| 10:45:32 | `ALTER` 拿到锁，**0.04 s 完成**，队列瞬间放行 | 0 | 逐步释放 | 恢复 |
| 复盘 | 44 分钟全站不可用；失败请求 1,840 万次；SLA 违约赔付 + 当日客服工单 3,100 张 | — | — | — |

**两个数字放在一起才是这起事故的全部：DDL 本身 0.04 s，等锁 2,605 s。** 而且「等锁」这件事会**把后面所有的查询一起拖住**——因为 MySQL 的 MDL 队列是**公平排队**的：一个 PENDING 的 `MDL_EXCLUSIVE` 会挡住它之后所有新来的共享锁请求，哪怕这些请求与当前持锁者并不冲突。这就是 §07.6 的队列效应，也是「DDL 前先看长事务」这条纪律的全部物理依据。

### 事故三：批量更新加锁顺序不一致，每分钟 40 次死锁，重试再放大 3 倍（2025-07-15）

支付对账系统有两个批处理任务，都要更新同一张 `account_balance` 表（380 万账户）：

- 任务 A（日终对账）：`SELECT ... WHERE id BETWEEN ? AND ? ORDER BY id ASC`，每 500 行一批，按主键升序；
- 任务 B（大额复核）：按 `balance DESC` 取前 1 万个账户，逐条 `UPDATE`。

单看都对。并发跑起来时，A 从 `id=1` 往上走、B 从「余额最大的账户」往下走，两条路径在两个区域交叉，于是**每个交叉点上形成标准的锁环**：A 持有 `id=2011` 等 `id=2012`，B 持有 `id=2012` 等 `id=2011`。

| 时间 | 事件 | 死锁（次/分钟） | 写入 QPS | `Innodb_deadlocks` 累计 |
| --- | --- | --- | --- | --- |
| 07-15 02:00 | A、B 同时启动，交叉区约 6,200 个账户 | 38 | 1,200 | 38 |
| 02:15 | `innodb_deadlock_detect=ON`，每次自动回滚「代价小」的一方，业务侧捕获到 1213 后**立即重试 3 次、无退避** | 41 | 3,900 | 620 |
| 02:40 | 重试放大：超过一半的重试又撞进同一环，写入 QPS 是原始的 3.2 倍，redo 生成速率翻倍 | 44 | 3,900 | 1,780 |
| 02:44 | A 的对账窗口（原本 20 分钟）超时；B 部分账户被重复写入（**重试没做幂等**） | 40 | 3,900 | 1,890 |
| 03:30 | 人工停 B、A 跑完；核对出 2,300 笔重复记账 | 0 | 1,200 | 1,890 |
| 复盘 | 对账延迟 6 小时；冲正 2,300 笔耗时 3 天；**重试风暴让一次 40 分钟的窗口变成 90 分钟** | — | — | — |

三件事叠在一起：**加锁顺序不一致（造环）**、**检测到死锁后自动回滚（掩盖了环的存在）**、**无退避无幂等的重试（把死锁变成写入放大）**。修法在 §07.7 的四种写法里，每一种都要连着代价一起说。

---

## 07.2 一句话定位与唯一真问题

**定位**：InnoDB 的锁不是「行上的一个标记」，而是「事务在**索引记录**上声明的一段区间所有权」。三个「不是」：不是按行（`row`）分配的，是按**索引记录 + 空隙**分配的；不是只有一种（表级意向锁、行级四种、MDL、AUTO-INC 是四套不同的东西）；不是越多越好也不是越少越好（锁少了换并发，锁多了换正确性，中间那条线由索引形态决定）。

### 唯一真问题

> **InnoDB 的锁加在索引上而不是行上——「我以为锁了一行，其实锁了一段区间」是所有超时、死锁与 DDL 卡死事故的总根源。**

这一句要拆成四截来用：

1. **「加在索引上」**：锁的物理对象是 B+ 树叶子层的一条索引记录（加上它前面的空隙）。所以**同一句 SQL 走不同的索引，锁的范围完全不同**：`WHERE a=? AND b=?` 有 5 种建索引的方式，就有 5 种加锁范围，最大的那种等于锁全表。§07.1 事故一就是这个字面意思的实例：一句「只锁 1 个 SKU」的 SQL，因为 `sku_id` 上没索引，锁了 42 万行。
2. **「一段区间」**：RR 下当前读加的是 **next-key 锁 = 记录锁 + 前面的间隙锁**，而间隙锁锁的是「不存在的空间」。所以**你锁住的不仅是已存在的行，还包括「将来可能被插进来的行」**——这是幻读的解药（→ comp/06 §06.6），也是并发插入被挡、死锁成环的病因（§07.5）。
3. **「超时」**：锁等待的时长不取决于语句快不快，取决于**持锁事务什么时候提交**。所以「热点行更新的 QPS 上限 = 1 / 事务持锁时间」，这个上限跟 CPU、跟连接数、跟索引都没关系（§07.8 账一），它是一个串行化常数。
4. **「DDL 卡死」**：DDL 走的是另一套锁（MDL），但失效方式是同一个形状——**它请求的是一张表上的排他元数据锁，而队列公平性会把这一个请求放大成「全站所有查询排队」**（§07.6）。

从这一句推出全部纪律：

| 从「锁在索引上」推出的约束 | 推出的设计 |
| --- | --- |
| 加锁范围 = 扫描范围（无索引时扫描全表 ⇒ 锁全表） | 当前读语句必须能在 `EXPLAIN` 里看到 `type=range/ref/eq_ref`；`type=ALL` 的 `UPDATE`/`FOR UPDATE` 一律视为事故 |
| 唯一索引等值命中才是「一行」 | 库存、余额、计数器这类热点表的主键/唯一键就是并发度的上限，别用「业务组合条件」当唯一键 |
| RR 的间隙锁锁的是「不存在的行」 | 高并发抢购优先 RC（无间隙锁），代价是幻读要靠「带条件的原子写」补（→ comp/06 §06.5） |
| 二级索引加锁要连带锁聚簇索引记录 | 走二级索引的当前读，锁对象数是「索引记录数 + 回表行数」，按索引条数放大 |
| 锁的内存结构与「页」有关，不随行数线性爆炸 | 真正的成本不是内存，是**持锁时间 × 冲突面**（§07.8 账二） |
| MDL 与行锁是两套超时参数 | `lock_wait_timeout`（默认 31536000 s，管 MDL）与 `innodb_lock_wait_timeout`（默认 50 s，管行锁）不能互相替代 |
| 死锁是「锁顺序」的性质，不是「锁多少」的性质 | 减少死锁靠**统一顺序 + 缩短事务 + 点锁化 + 小批退避**，不靠调大超时（§07.7） |

### 解剖卡八问：本篇答哪几问

| 八问 | 落在哪节 | 一句话答案 |
| --- | --- | --- |
| ① 承诺什么 | §07.2、§07.9 | 承诺「当前读期间区间内的行不会被别人改、不会被别人插」；不承诺「你读到的东西别人改不动」（那是事务语义，不是锁） |
| ② 数据长什么样 | §07.3 | 锁是内存对象：`lock struct`（按事务 × 索引 × 页聚合）+ 页内每记录 1 bit 的位图 + 一个等待队列 |
| ③ 一次读写走哪条路 | §07.4、§07.8 | 定位索引记录 → 加锁 → 检查冲突 → 冲突则入等待队列 → 等待或超时；加锁本身 µs 级，**排队才是 ms~s 级** |
| ④ 并发与一致 | §07.3、§07.5 | 记录锁互斥、间隙锁只挡插入、插入意向锁只在有间隙锁时排队；失效窗口 = 解锁在提交时，不在语句结束时 |
| ⑤ 扩展维度 | §07.9 | **单实例内的锁不可横向扩**：只能「分片把锁拆到不同实例」（→ eng/36）或「把热点行拆成多个桶」（§07.8 账一），副本不解决写锁 |
| ⑥ 会在哪崩（系统级） | §07.8、§07.9 | 锁队列把整库写吞吐打成串行（慢）、MDL 队列让全站查询不可用（假活）、重试风暴把死锁放大成写入雪崩 |
| ⑦ 看哪几个指标 | §07.11 | 行锁等待次数/平均时长、死锁计数、`data_lock_waits` 的等待链长度、`metadata_locks` 的 PENDING 数、最老事务年龄 |
| ⑧ 换掉它的信号 | §07.9 | 单行 QPS 逼近 1/持锁时间、死锁率进 SLO、锁等待占请求延迟 > 30% ⇒ 分桶 / 改 RC / 换乐观并发 / 上分片 |

---

## 07.3 物理形态：锁挂在索引记录上

### 四层锁，四个不同的数据结构

初学者把所有「锁」当成一个东西，于是「表被锁了」「行被锁了」「DDL 卡住了」被混成一句话。物理上它们是**四个互不相干的对象**，超时参数、观测视图、失效模式全都不同。

**图 1 · 四个层次与它们各自挂在哪**

```text
一条 SQL 进来，可能碰到四套锁
   │
   ├─ ① MDL（元数据锁，metadata lock）
   │     挂在哪：server 层的表定义对象（数据字典里的 TABLE_SHARE）
   │     粒度：全局 / 表 / 列；每条 SQL 自动加，事务结束才放（事务级）
   │     冲突方：DDL、ALTER、RENAME、DROP、LOCK TABLES
   │     超时参数：lock_wait_timeout     ← 默认 31536000 s（一年），等于「永不放弃」
   │
   ├─ ② 表级意向锁（IS / IX，还有 S / X 表锁）
   │     挂在哪：表对象；作用只有一个：声明「我下面在行上有锁」
   │     粒度：表；自动附带；冲突方：LOCK TABLES ... WRITE、部分 DDL 的收尾阶段
   │     它存在的理由：让「想锁整表的人」不必逐行检查几百万行，一眼看 IS/IX 就够了
   │
   ├─ ③ 行级锁（本篇主角）
   │     挂在哪：B+ 树叶子层的索引记录 + 记录之间的空隙（含 supremum 伪记录）
   │     粒度：区间（左开右闭）；类型：记录锁 / 间隙锁 / next-key / 插入意向
   │     超时参数：innodb_lock_wait_timeout ← 默认 50 s
   │
   └─ ④ AUTO-INC 锁（自增锁）
         挂在哪：表对象的自增计数器；粒度：语句（mode 1/2）或事务（mode 0）
         冲突方：其他 INSERT；它是「表级」的，但只挡住 INSERT
```

### 行锁的三种形态：锁的是「记录」「空隙」还是「两者」

这是全篇最需要画对的一张图。InnoDB 的索引是**有序**的：`...| id=8 | id=12 | id=20 | id=25 | supremum`。锁可以落在记录上，也可以落在两条记录之间的空隙里——而空隙里没有任何行，所以**间隙锁锁的是一个「还没有数据的位置」**。

**图 2 · 一条索引上的四种行锁**

```text
聚簇索引叶子层的键序列（主键 id）：

  ... ──┤ id=8 ├──────┤ id=12 ├──────┤ id=20 ├──────┤ id=25 ├──────┤ supremum ├──
         ▲     ▲      ▲      ▲       ▲      ▲       ▲      ▲        ▲
         │     └─间隙─┘      └──间隙──┘      └──间隙──┘      └─间隙──┘
         │     G(8,12)       G(12,20)        G(20,25)        G(25,+∞)

  记录锁 REC_NOT_GAP  ─ 只锁 id=12 这条记录本身，不动任何空隙
  间隙锁 GAP          ─ 只锁 G(12,20) 这段空隙：里面没有行，锁的是「不许在这里插」
  next-key ORDINARY   ─ 记录锁 + 它左边的间隙 = (8,12] 这样一段左开右闭区间
  插入意向 INTENTION  ─ 「我要往 G(12,20) 里插一条」，它只和间隙锁冲突

  一张表就是一棵树，锁的坐标永远是「索引 + 记录/空隙」，从不出现「表里第 N 行」
```

三个必须记住的推论：

1. **next-key 锁是「两半拼起来的」**，两半的冲突规则完全不同：**记录部分按记录锁互斥（X 挡 S/X），间隙部分只挡插入（间隙锁之间互不冲突）**。所以「两个事务都持有 X 间隙锁在同一个空隙上」是允许的，而「一个持有间隙锁、另一个要往里插」才冲突。这条拆解能解释 90% 「为什么这个锁没挡住对方」的困惑。
2. **间隙锁只在 RR（及以上）存在**。RC 下写语句不锁间隙（例外：外键检查与唯一键冲突检查仍会加间隙/记录锁）。这是 RC 死锁率天然低一个量级的原因，也是 RC 下幻读管不住的原因（→ comp/06 §06.5）。**注意别把这条读成「RC 下无索引 `UPDATE` 就不锁全表」**——RC 下它照样把扫过的**每一行**加上记录锁，只是不加间隙，锁全表的事实一点没变（§07.10 坑 1）。
3. **走二级索引的当前读，要锁两处**：先锁二级索引记录，再回表锁对应的聚簇索引记录。所以「42 万条索引记录」实际是 42 万 × 2 个锁对象。8.0 的 `data_locks` 里会看到两个不同 `INDEX_NAME` 的行，这是很多人第一次读锁视图时最容易忽略的细节。

### 锁的内存结构：为什么「10 万个行锁」不占内存

InnoDB 的锁不是「每行一个对象」。物理结构是：

| 结构 | 粒度 | 内存代价 | 说明 |
| --- | --- | --- | --- |
| `lock struct` | 每个 (事务 × 索引 × 页 × 模式) 一个 | 固定头 + 页号 + 位图指针，量级 300~400 B（可从死锁日志的 `heap size` 反推） | 一个事务锁住一整页里的所有记录，通常只产生 1~2 个结构 |
| 锁位图 | 页内**每条记录 1 bit** | 一页 100 条记录 = 13 B | 这就是「锁的行数」在内存里的真实成本 |
| 等待队列 | 每个被争用的锁后面挂一条链 | 与等待者数成正比 | 长等待链本身会拖慢冲突检测 |

所以「无索引 `UPDATE` 锁了 10 万行」，内存侧可能是几百 KB 到几 MB 的事——**真正的代价不是内存，是持锁时间和冲突面**（§07.8 账二会把这个账算完）。顺便说明一个只在死锁日志里出现的证据：`3 lock struct(s), heap size 1136` 这句话的意思是「这个事务有 3 个锁结构，共占 1,136 B 的锁堆内存」，用它反推单个结构量级最可靠，比任何博客里的数字都可信。

### 相容矩阵一：表级（IS / IX / S / X）

| 请求 ↓ / 持有 → | X | S | IX | IS |
| --- | --- | --- | --- | --- |
| **X**（表写锁） | 冲突 | 冲突 | 冲突 | 冲突 |
| **S**（表读锁） | 冲突 | 兼容 | 冲突 | 兼容 |
| **IX**（意向写） | 冲突 | 冲突 | 兼容 | 兼容 |
| **IS**（意向读） | 冲突 | 兼容 | 兼容 | 兼容 |

读法只有一句：**意向锁之间互不冲突（IX 和 IX、IS 和 IX 都能同时持有），意向锁只和「表级真锁」冲突。** 这就是为什么 300 个并发事务在改同一张表的不同行时，不会因为在表这一层排队——它们只是在表上各挂一个 IX 声明，然后在行上真打。`LOCK TABLES ... WRITE` 之所以是运维禁忌，就是因为它要 X 表锁，会把所有 IX 一起挡掉。

### 相容矩阵二：行级（这是答辩时最容易被追问的一张）

| 请求 ↓ / 持有 → | S 记录锁 | X 记录锁 | 间隙锁（X 间隙） | 插入意向锁 |
| --- | --- | --- | --- | --- |
| **S 记录锁** | 兼容 | **冲突** | 兼容 | 兼容 |
| **X 记录锁** | **冲突** | **冲突** | 兼容 | 兼容 |
| **间隙锁** | 兼容 | 兼容 | **兼容**（间隙锁之间从不冲突） | **冲突** |
| **插入意向锁** | 兼容 | 兼容 | **冲突** | **兼容**（同一空隙的不同位置可并行） |

这张表的三行结论，分别解释三件事：

- **「X 记录锁 vs 间隙锁 = 兼容」**：这说明间隙锁**根本不阻止别人锁已在的行**，它唯一的作用是阻止**插入**。所以「我加了 `FOR UPDATE`，为什么别人还能改这一行？」的答案通常是：你以为的 X 锁落到了空隙上（这条记录不存在），根本没锁到那一行。
- **「间隙锁 vs 间隙锁 = 兼容」**：两个事务可以同时对同一个空隙加 X 间隙锁，谁也不会被挡。**只有当第三方要往里插时，才会撞上这两个人的间隙锁。**
- **「插入意向锁 vs 插入意向锁 = 兼容」**：这是 §07.5 的主角，也是「高并发 `INSERT` 为什么不排队」的全部原因。

---

## 07.4 六种 WHERE 形态与实际加锁范围

### 判据只有两条

不要背「RR 会加间隙锁」这种话，那是结论不是判据。加锁范围由两个变量决定：

1. **访问路径**：`EXPLAIN` 里的 `type` 是 `const/eq_ref/ref/range/index/ALL` 中的哪一个；
2. **索引唯一性**：命中的是不是「唯一索引的全部列等值」（只有这一种情况能免掉间隙）。

两个变量组合出六种形态，其中只有一种真的只锁一行。

### 实战表：6 种形态 × 实际加的锁 × 阻塞范围

| # | WHERE 形态 | `EXPLAIN` 典型 `type` | RR 下实际加的锁 | 阻塞谁的什么操作 | 量级（以 486 万行、单值 42 万行为例） |
| --- | --- | --- | --- | --- | --- |
| ① | 唯一索引**全列等值命中** | `const` / `eq_ref` | 单条**记录锁**（`REC_NOT_GAP`），无间隙 | 只阻塞别人改这 1 行 | 1 个记录（锁范围 = 1） |
| ② | 唯一索引**等值未命中** | `const`（返回空） | 纯**间隙锁**：锁住「它本该在的那个空隙」 | 阻塞别人往这个空隙插（可能是一条连续 ID 区间） | 1 段空隙（插入型业务会被挡） |
| ③ | 非唯一索引**等值** | `ref` | 命中记录各加 next-key + 向右锁到下一个键值之前 | 阻塞：改这些行 + **插入相同键值、以及下一个键值之前的任意值** | 42 万索引记录 + 42 万聚簇记录 |
| ④ | 索引**范围**扫描 | `range` | 区间内每条记录 next-key + 两端边界间隙（含越过边界的第一个记录前的间隙） | 阻塞区间内改 + 区间内外两侧边界插入 | 与区间宽度成正比；`BETWEEN 7 AND 9` 若含 42 万行就是 42 万 |
| ⑤ | **无索引**（或索引被废） | `ALL` | 全表扫描，**扫过的每一行**都加 next-key + supremum 伪记录 | 阻塞全表所有写、所有 `FOR UPDATE` | 486 万（等价锁全表） |
| ⑥ | 走**二级索引**再回表 | `ref` / `range` | 二级索引记录锁 + **回表后的聚簇索引记录锁**（两处都锁） | 同 ③④，但锁对象数 ×2，且在两个索引上各占结构 | 84 万锁对象（42 万 × 2） |

### 可复现的六条 SQL

```sql
CREATE TABLE stock (
  id BIGINT PRIMARY KEY,
  sku_id BIGINT NOT NULL,
  warehouse_id INT NOT NULL,
  qty INT NOT NULL,
  KEY idx_wh (warehouse_id)              -- 非唯一二级索引；sku_id 上没有索引
) ENGINE=InnoDB;

-- ① 唯一索引等值命中 → 单条记录锁（不加间隙，别人插不进这个空隙也不归它管）
UPDATE stock SET qty = qty - 1 WHERE id = 1001;

-- ② 唯一索引等值未命中 → 纯间隙锁：锁住 (999998, supremum) 这段空隙
UPDATE stock SET qty = qty - 1 WHERE id = 999999;

-- ③ 非唯一索引等值 → 命中的每条 next-key + 向右锁到下一个 warehouse_id 之前
UPDATE stock SET qty = qty - 1 WHERE warehouse_id = 7;

-- ④ 索引范围 → 区间内每条 next-key + 两端边界间隙
UPDATE stock SET qty = qty - 1 WHERE warehouse_id BETWEEN 7 AND 9;

-- ⑤ 无索引 → 优化器只能全表扫，扫过的每一行都加 next-key（锁全表）
UPDATE stock SET qty = qty - 1 WHERE sku_id = 88;

-- ⑥ 二级索引当前读 → 二级索引记录 + 回表后的聚簇索引记录，两处都锁
SELECT qty FROM stock WHERE warehouse_id = 7 FOR UPDATE;
```

**图 3 · 六种形态在索引上划出的区间**

```text
索引 idx_wh 的键值序列：   6        7      7      7        9        11       supremum
                          │        │      │      │        │        │          │
① WHERE id=1001           └─ 走聚簇索引直接定位，只有 id=1001 这一个点被锁
                            （REC_NOT_GAP，一个记录锁，零间隙）

② WHERE id=999999（不存在）  ─────────────────────────► (11, +∞) 这段空隙被锁
                            别人 INSERT id=12 会被挡（哪怕它跟 999999 毫无关系）

③ WHERE warehouse_id=7       ├──────────────────────► 三条 7 全部 next-key
                             └─ 并向右锁到 11 之前 ⇒ 插入 7、8、9 之外的 10 也要等

④ BETWEEN 7 AND 9            ├────────────────────────────────► 7/7/7/9 全锁 + 右界到 11 前

⑤ WHERE sku_id=88（无索引）  ├──────────────────────────────────────────► 全表
                             从最左叶子记录扫到 supremum，一个不落

⑥ FOR UPDATE on idx_wh       ├─ 二级索引 42 万条  ──► 回表 ──► 聚簇索引 42 万条
                             同一个「区间」在两个索引上各锁一遍 = 锁对象 ×2
```

### 怎么在现场确认「它到底锁了什么」

不要靠推理，直接看锁视图（8.0）。`performance_schema.data_locks` 的 `LOCK_MODE` 字段就是答案的枚举：

| `LOCK_TYPE` | `LOCK_MODE` | 含义 | 对应上表 |
| --- | --- | --- | --- |
| `TABLE` | `IX` | 表级意向写（行锁的附带声明） | 任何 DML |
| `TABLE` | `S` / `X` | 真表锁（`LOCK TABLES` 或 DDL 收尾） | 运维事故 |
| `RECORD` | `X,REC_NOT_GAP` | 记录锁，无间隙 | ① |
| `RECORD` | `X,GAP` | 纯间隙锁（记录不存在时那条 `SUPREMUM`/边界记录上会显示） | ② |
| `RECORD` | `X` | next-key（记录 + 前面的间隙），RR 下的默认形态 | ③④⑤ |
| `RECORD` | `X,GAP,INSERT_INTENTION` | 插入意向锁正在等间隙锁 | §07.5 |
| `RECORD` | `S` / `S,REC_NOT_GAP` | 共享锁（`FOR SHARE`、外键检查、唯一键冲突等待） | 并发插入 |

**一个反直觉但极其重要的现象**：`①` 形态的语句在 `data_locks` 里只有 **2 行**（一个 `TABLE IX` + 一个 `RECORD X,REC_NOT_GAP`），而 `⑤` 形态会产生**几十万到几百万行**——把 `data_locks` 全查出来能把 `performance_schema` 自己的内存打满。所以线上排查锁问题时**永远先按 `INDEX_NAME` + `LOCK_MODE` 聚合，再抽样看 `OBJECT_NAME/PAGE_NO`**，不要 `SELECT *`。

---

## 07.5 间隙锁、插入意向锁与并发插入

### 为什么同一个空隙里的两个 INSERT 不互斥

如果「锁是不存在的空间」这句话成立，那最自然的推算是：「两个人往同一段空隙插数据，第二个必然被挡」。**事实相反**：默认情况下两个事务往同一段空隙插数据完全不排队。原因在于 `INSERT` 使用的不是普通间隙锁，而是**插入意向锁（insert intention lock）**——它是间隙锁的一个变种，语义是「我要在这里插，不是我要占住这里」。

| 场景 | 谁挡谁 | 挡住它的到底是什么 |
| --- | --- | --- |
| T1、T2 都往空隙 `(12,20)` 插，插入值不同（13 与 17） | **互不阻塞** | 插入意向锁之间兼容；两条记录落点不同，索引结构调整不冲突 |
| T1 已持有 `(12,20)` 的**间隙锁**（`FOR UPDATE` 一个不存在的值、或范围当前读），T2 要插 | T2 **等** T1 | 插入意向锁 vs 间隙锁 = 冲突。**间隙锁存在的唯一目的就是挡这个插入** |
| T1 插了唯一键 `k=15` 未提交，T2 也插 `k=15` | T2 **等** T1（S 锁） | 唯一性检查：T2 必须对 T1 刚插的那条记录加 **S 锁**（`S`/`S,REC_NOT_GAP`）才能确认冲突，与 T1 的 X 记录锁冲突 |
| T1、T2 都插 `k=15`，T1 已提交，T2 再插 | 直接报 `Duplicate entry` | 唯一约束，与锁无关 |

一句话：**「并发插入不互斥」的前提是「不撞同一个唯一键、且没人用间隙锁占座」**。忘记后半句的人会写出一条 `SELECT ... WHERE x BETWEEN ? AND ? FOR UPDATE` 的「取号」逻辑，然后在插入型业务上把整条流水线堵死。

三个从这张表直接推出来的工程结论：

1. **间隙锁是「插入型业务」的并发杀手**。任何「先范围当前读、再插入」的写法，在 RR 下都会把范围内将来的插入全部串行化。典型受害者：订单号取号、按日期分区的流水表、按时间窗聚合后再写汇总表。
2. **唯一键冲突的 S 锁是「最隐蔽的死锁源」**。两个事务分别插 `k=15` 和 `k=16`，同时又都插另一边已存在的键，就会构成经典的四锁环：S 锁等 X 锁、X 锁等 S 锁，日志里看起来是「两条完全无害的 INSERT 互相锁死」。
3. **`INSERT ... SELECT` 是个陷阱**：RR 下它对**源表**的扫描行加共享 next-key 锁（8.0 在 `binlog_format=ROW` + RC 下退化为记录锁），所以「把 100 万行从一个表搬进另一个表」这句话的真实含义是**把源表的这段区间也锁住几分钟**（§07.10 坑 3）。

### AUTO-INC 锁三档：一条被 8.0 改掉默认值的历史

自增列是表级计数器，它需要一把锁——但这把锁的粒度与「语句级 ID 连续性」直接绑定，而「语句级 ID 连续性」又是 STATEMENT 格式复制的正确性前提。这三个东西被 `innodb_autoinc_lock_mode` 一次性绑在一起：

| 模式 | 名字 | 锁的粒度 | 同一条语句内 ID 连续？ | STATEMENT binlog 安全？ | 说明 |
| --- | --- | --- | --- | --- | --- |
| `0` | traditional | 所有 `INSERT` 都持**表级 AUTO-INC 锁到语句结束** | 是 | 是 | 并发插入最差，现代系统不该再用 |
| `1` | consecutive | **simple inserts**（行数可预知：单行/多行 `VALUES`）用轻量互斥量**预取一段值**，不持表锁；**bulk inserts**（`INSERT ... SELECT`、`LOAD DATA`）仍持 AUTO-INC 锁到语句结束 | 是 | 是 | MySQL 5.7 的默认值 |
| `2` | interleaved | 完全不持表锁，只用互斥量取下一个值 | **否**：同一条语句的多行可能被别的语句插入的 ID 打断 | **否** | **MySQL 8.0 的默认值**，前提是 `binlog_format=ROW` |

**历史包袱要讲清楚**：模式 `2` 从 5.6 就有，但当时 `binlog_format` 默认还是 `STATEMENT`，所以默认值只能是 `1`；5.7.7 起 `binlog_format` 默认改成 `ROW`，模式 `2` 才真正安全；8.0 于是把默认值改成 `2`。也就是说「8.0 默认 `2` 是安全的」这句话的完整版是——**「`innodb_autoinc_lock_mode=2` 安全的前提是 `binlog_format=ROW`，而这两件事是分两次改的默认值」**。

麻烦在于：**`mode=2` + `binlog_format=STATEMENT` 这个组合不会被 8.0 物理禁止**（不像 RC + STATEMENT 那样直接报错），它只是「运行时给不同的 ID，从库重放后 ID 不一致」。今天任何一次「为了排查方便把 binlog 改成 STATEMENT」的操作，都会在这个组合上埋一颗静默的数据不一致炸弹。这就是本篇把它放进坑清单的原因：**能配上但不报错的错误配置，比报错的更贵。**

另外两个只影响观感的点，别当成 bug：模式 `2` 下自增 ID **允许有大段跳号**（一次预取一段、回滚不归还、重启后按 max(id) 校准），跳号与 `auto_increment_increment`、与主从切换都有关系，与「插入失败」无关。

---

## 07.6 MDL：DDL 是怎么把全站查询堵死的

### MDL 的粒度与寿命，和行锁惊人地一致

MDL（metadata lock）保护的是**表定义**这个对象，防止「一边改结构、一边按旧结构读数据」。它的三种强度足够覆盖全部语句：

| MDL 类型 | 谁加 | 兼容性 | 关键事实 |
| --- | --- | --- | --- |
| `MDL_SHARED_READ`（SR） | 普通 `SELECT`、`SHOW` | 与 SR、SW、SU 兼容，与 X 冲突 | 只读也要加，**所以只读接口一样会被 DDL 堵死** |
| `MDL_SHARED_WRITE`（SW） | `INSERT/UPDATE/DELETE` | 与 SR 兼容，与 SU、X 冲突 | 行锁之外的第二重声明 |
| `MDL_SHARED_UPGRADABLE`（SU） | online DDL 的第一阶段（扫描/拷贝数据时） | 与 SR 兼容，与 SW 冲突 | 允许读、禁止写；结束时**升级为 X** 做切换 |
| `MDL_EXCLUSIVE`（X） | `ALTER`、`DROP`、`RENAME`、online DDL 的切换瞬间 | 与所有冲突 | INSTANT DDL 也只持 X 几十毫秒，**但它必须等前面所有人** |

**寿命是全部问题的源头**：MDL 在**语句开始时申请、在事务结束时释放**（事务级，语句级 MDL 只在自动提交的单条语句里存在）。所以一个 `START TRANSACTION` 之后只读不写、然后忘记提交的会话，**会一直持有 `MDL_SHARED_READ`**——它既不占行锁、也没有 undo 增长（只读事务在 8.0 都不分配 `trx_id`），在监控上几乎看不出来，却能把一次 40 ms 的 DDL 钉死 2,600 s（§07.1 事故二）。

### 队列效应：一个 PENDING 请求如何冻结全表

MDL 的排队是**公平的（先来先服务）**，这是它跟「乐观抢占」最大的区别，也是雪崩的机制：

**图 4 · MDL 队列效应：一个请求冻结一张表**

```text
t=0  报表事务 T1（只读，未提交）──── 持有 ticket 表的 MDL_SHARED_READ ────────► 一直不放
                                          │
t=1  ALTER TABLE ticket ... ────────────► [ 申请 MDL_EXCLUSIVE ] 与 SR 冲突 ⇒ PENDING（队头）
                                          │
t=2  后面的每条 SELECT ─────────────────► [ 申请 MDL_SHARED_READ ] 与 SR 不冲突，
                                          但与「队头那个 PENDING 的 X」冲突 ⇒ 也必须排队
                                          │   ⚠ 这一条是全部灾难的来源：
                                          │     新请求挡它的不是持锁者，是「排在前面的等待者」
t=3  应用连接池 200 个连接全部排在这里 ────► 每个连接 3 s 超时 / 无超时（lock_wait_timeout 默认一年）
                                          │
t=4  新请求拿不到连接 ⇒ 立刻失败或排队 ⇒ 从「慢」变成「全站不可用」

一句话：MDL 的公平队列把「一个 DDL 与一个长事务的私人冲突」放大成「全表所有查询的公共等待」。
```

这里有个必须在评审材料里写明的事实：**出问题的不是 DDL 慢，是「DDL 申请排他锁」这个动作本身**。哪怕它最终只需要 0.04 s，它在 PENDING 期间就已经把整张表的并发度变成 0。

### online DDL 能力矩阵与持锁时间

| 变更 | `ALGORITHM` | 允许并发 DML | 重建表 | X MDL 持有时间 | 版本前提 |
| --- | --- | --- | --- | --- | --- |
| 末尾加列 | `INSTANT` | 是 | 否（只改数据字典） | 毫秒级 | 8.0.12+ |
| 任意位置加列 / 删列 | `INSTANT` | 是 | 否（改字典 + 行版本标记） | 毫秒级 | 8.0.29+；每表可累积的 instant 变更次数有上限（公开资料给的是 64 次量级，达到后需一次 rebuild，上线前实测） |
| 改列名 / 改默认值 / 改 `COMMENT` | `INSTANT` | 是 | 否 | 毫秒级 | 8.0 |
| 加普通或唯一二级索引 | `INPLACE` | 是（`LOCK=NONE`） | 否（但索引要全建一遍） | 首尾各一次短暂 X | 5.6+ |
| 删二级索引 | `INPLACE` | 是 | 否（只改字典 + 异步清理） | 短暂 X | 5.6+ |
| `VARCHAR` 加长，字节数长度前缀不变 | `INPLACE` | 是 | 否 | 首尾短暂 X | 8.0 |
| `VARCHAR` 加长，前缀从 1 B 跨到 2 B（<256 → ≥256） | `COPY` | **否** | **是** | 全程（写被挡，读可进） | — |
| 改列类型（`INT` → `BIGINT`）、改字符集 | `COPY` | **否** | **是** | 全程 | — |
| 加外键 | `INPLACE` | 是 | 否 | 首尾短暂 X | 8.0 |
| 加全文索引 | `INPLACE` | **否**（`LOCK=SHARED`） | 否 | 全程挡写 | — |
| 改主键 / `OPTIMIZE TABLE` / 加分区 | `COPY`（或 `INPLACE` 但锁表） | **否** | **是** | 全程 | — |

「持锁时间」这一列才是架构含义：`INSTANT` 只把持锁时间压到毫秒，**压不掉「排队等待」**；`COPY` 才是真的把表锁了几十分钟。所以两件事要分开做决策：**用 INSTANT/INPLACE 缩短「我持有锁的时间」，用「先杀长事务 + 设小 `lock_wait_timeout`」避免「我排队的时间」**。

### 一条可直接抄的 DDL 前置纪律

1. **先查最长的事务与最老的 MDL 持有者**（§07.11 的巡检 SQL），有 > 60 s 的立刻处置；
2. DDL 会话里先 `SET SESSION lock_wait_timeout = 5;`——把「排在队头堵死全站 2,600 s」换成「5 s 后自己失败、稍后重试」。**这一条是本节最便宜、收益最高的操作**；
3. `EXPLAIN` 性质地确认 `ALGORITHM`：`ALTER ... , ALGORITHM=INSTANT` 让引擎**拒绝**降级到 INPLACE/COPY；如果它报错，说明你以为的「毫秒级」其实是「几十分钟」；
4. 表 > 100 GB 或必须 `COPY` 时，走 gh-ost / pt-osc，并接受下面这张表的代价。

### pt-osc 与 gh-ost：代价与触发条件

| 项 | pt-online-schema-change | gh-ost |
| --- | --- | --- |
| 同步机制 | **触发器**（`INSERT/UPDATE/DELETE` 各一个）把变更写进影子表 | **消费 binlog**（伪装成副本拉取），不建触发器 |
| 前提条件 | 表上没有同名触发器冲突；需要足够磁盘（影子表 + 原表） | `binlog_format=ROW` 且 `binlog_row_image=FULL`；需要一个可连的副本或 `--allow-on-master` |
| 写放大 | 每次 DML 额外一次触发器写入 + 影子表写入 ≈ **2~3×** | 每次 DML 由 gh-ost 重新应用一次 ≈ **2×**，但多一份 binlog 解析开销 |
| 磁盘 | ≈ 2× 表大小（切完才释放） | ≈ 2× 表大小，另有 binlog 保留需求 |
| 外键 | 有外键的表风险高（触发器与级联叠加），多数团队直接禁止 | 不支持，必须先拆外键 |
| 最终切换（cut-over） | 需要拿到一次短暂 **X MDL**，被长事务挡住会重试；重试期间旧表仍在写 | 同样需要 X MDL；用 `RENAME TABLE` 原子切换，失败可回退 |
| 不可控代价 | 触发器让**所有**写入路径变慢（业务感知得到），复制延迟上升 | 只有 gh-ost 自己的线程吃 IO/CPU；但 binlog 丢失/位点异常时需要人工介入 |

触发条件一句话：**「表上不能有 > 1 分钟不可写的窗口」且「表大到 `COPY` 会跑几十分钟」⇒ 必须用工具；其余情况优先等窗口 + 原生 `INSTANT/INPLACE`。** 工具不是更安全，它只是把「一次长锁」换成「持续两倍写 + 一次风险切换」。

---

## 07.7 死锁：图、日志与四种写法

### wait-for graph：死锁的定义与检测成本

死锁在 InnoDB 里是一张**等待图**上的环：事务是顶点，`A 等 B 持有的锁` 是一条有向边。当新边加入时形成一个环，就是死锁。最经典的形状是两条 `UPDATE` 加锁顺序相反：T1 持有 `id=1` 等 `id=2`，T2 持有 `id=2` 等 `id=1`——两条边方向相反，无论等多久都不可能解开。
`innodb_deadlock_detect`（默认 `ON`）的行为：**每当一个事务进入等待，就沿等待图做一次环检测**。检测开销与「当前活跃事务数」成正比（在最坏情况下是一次图遍历），这就是它在高并发下被关掉的理由——不是它不准，而是**每次等待都要付一次全图扫描**的成本。

| 配置 | 死锁如何解开 | 代价 | 适用 |
| --- | --- | --- | --- |
| `innodb_deadlock_detect=ON`（默认） | 入等待时立即检测，发现环就**回滚「代价小」的那一方**（`undo log entries` 少 = 改的行少），报 1213 | 每次等待一次检测开销，活跃事务数上千时 CPU 可见 | 绝大多数 OLTP |
| `innodb_deadlock_detect=OFF` | 不检测；靠 `innodb_lock_wait_timeout`（默认 50 s）超时，报 1205 | 死锁要白等 50 s，期间连接与 undo 全占着；但省 CPU | 分片后单实例并发低、或写入天然无交叉的日志型表 |

关键细节：**被牺牲的总是「改得少」的那个事务**。所以「重试的永远是同一个小事务」是有偏的——批处理大事务在死锁里通常是幸存者，重试压力全部压到短小的事务上，这会让「加个小事务就好了」成为一种自我强化（§07.8 账三）。

### 逐行解读 `LATEST DETECTED DEADLOCK`

**代码块 · 一段真实的死锁日志（转账双会话，8.0 输出格式）**

```text
------------------------
LATEST DETECTED DEADLOCK
------------------------
2026-01-08 21:04:11 0x7f2c3d0a9700                 ① 发生时刻 + 报告的线程
*** (1) TRANSACTION:                                ② 环的一端
TRANSACTION 4210, ACTIVE 3 sec starting index read  ← 事务号 / 已存活 3 s / 当前动作
mysql tables in use 1, locked 1                     ← 只用 1 张表、锁住 1 张
LOCK WAIT 3 lock struct(s), heap size 1136, 2 row lock(s), undo log entries 1
                                                   ③ 正在等锁；3 个锁结构、1,136 B 锁堆、
                                                      2 个行锁、1 条 undo（= 只改过 1 行）
MySQL thread id 12, OS thread handle 140234, query id 9088 localhost app updating
UPDATE accounts SET balance = balance - 100 WHERE id = 2      ④ 它在执行哪条语句
*** (1) HOLDS THE LOCK(S):                          ⑤ 它已经拿着什么
RECORD LOCKS space id 5 page no 4 n bits 72 index PRIMARY of table `shop`.`accounts`
  trx id 4210 lock_mode X
Record lock, heap no 2 PHYSICAL RECORD: n_fields 4; compact format; info bits 0
 0: len 8; hex 8000000000000001; asc ;;             ← 持有的是 id = 1 这一行
*** (1) WAITING FOR THIS LOCK TO BE GRANTED:        ⑥ 它在等什么
RECORD LOCKS space id 5 page no 4 n bits 72 index PRIMARY of table `shop`.`accounts`
  trx id 4210 lock_mode X locks rec but not gap waiting
Record lock, heap no 3 PHYSICAL RECORD: n_fields 4; compact format; info bits 0
 0: len 8; hex 8000000000000002; asc ;;             ← 等的是 id = 2 这一行
*** (2) TRANSACTION:                                ⑦ 环的另一端（结构完全相同）
TRANSACTION 4209, ACTIVE 5 sec starting index read
LOCK WAIT 3 lock struct(s), heap size 1136, 2 row lock(s), undo log entries 1
UPDATE accounts SET balance = balance - 200 WHERE id = 1
*** (2) HOLDS THE LOCK(S):
RECORD LOCKS ... index PRIMARY of table `shop`.`accounts` trx id 4209 lock_mode X
 0: len 8; hex 8000000000000002;                   ← 持有 id = 2
*** (2) WAITING FOR THIS LOCK TO BE GRANTED:
RECORD LOCKS ... lock_mode X locks rec but not gap waiting
 0: len 8; hex 8000000000000001;                   ← 等 id = 1 ⇒ 环闭合
*** WE ROLL BACK TRANSACTION (2)                    ⑧ 被牺牲的是 T2（改的行一样多时按内部顺序）
```

怎么在 30 秒内读完这段日志：

| # | 看什么 | 怎么用 |
| --- | --- | --- |
| ① | 时间戳 | 和业务告警对时间；`innodb_print_all_deadlocks=ON` 时能在 error log 里捞历史，而不只是「最近一次」 |
| ②④ | 事务号 + 语句 | **语句是根因**：两条语句的顺序就是环的方向。看到「两条都是 `UPDATE ... WHERE id=?`」就该怀疑应用层的加锁顺序 |
| ③ | `lock struct(s)` / `heap size` / `row lock(s)` / `undo log entries` | 前三个是锁的量级；`undo log entries` 是「回滚代价」，决定谁被牺牲。**这一行也是「锁结构 ~300~400 B」的实测依据** |
| ⑤⑥ | `HOLDS` / `WAITING FOR` | 只看 hex 的最后一列就够：`8000000000000001` = id 1。判断「谁拿着谁的」。注意 `locks rec but not gap`——这是纯记录锁，说明命中了唯一索引（§07.4 形态 ①）；如果这里是 `lock_mode X`（无 `rec but not gap`），说明是 next-key，**锁的是区间，环的原因大概率是间隙** |
| ⑦ | 第二个 `TRANSACTION` | 结构与第一个相同；两段的 `HOLDS` 交叉即为环 |
| ⑧ | `WE ROLL BACK` | 回滚的一方是「代价小」的；如果是 `*** (1) TRANSACTION (2) ... both` 说明两边代价相同按内部规则 |

### 可复现的双会话实验：两种加锁顺序

```sql
-- 准备
CREATE TABLE accounts (id BIGINT PRIMARY KEY, balance BIGINT NOT NULL) ENGINE=InnoDB;
INSERT INTO accounts VALUES (1, 1000), (2, 1000);

-- ── 会话 A（顺序 1 → 2）        ── 会话 B（顺序 2 → 1）：必然死锁
-- A1                             -- B1
BEGIN;                            BEGIN;
UPDATE accounts SET balance=balance-100 WHERE id=1;
                                  UPDATE accounts SET balance=balance-200 WHERE id=2;
-- A2（阻塞，等 B 的 id=2）        -- B2（阻塞，等 A 的 id=1）⇒ 环形成
UPDATE accounts SET balance=balance+50  WHERE id=2;
                                  UPDATE accounts SET balance=balance+50  WHERE id=1;
-- 结果：3 秒内 B 或 A 之一收到 ERROR 1213 (40001)，另一方 COMMIT 成功
-- 会话 A：ERROR 1213 (40001): Deadlock found when trying to get lock; try restarting transaction
COMMIT;

-- ── 验证锁状态（另开会话，8.0）
SELECT ENGINE_TRANSACTION_ID, OBJECT_NAME, INDEX_NAME, LOCK_TYPE, LOCK_MODE,
       LOCK_STATUS, LOCK_DATA
  FROM performance_schema.data_locks ORDER BY ENGINE_TRANSACTION_ID, LOCK_DATA DESC;
```

**把 B 的顺序改成 1 → 2，「必然死锁」就变成「必然排队」**：这是全篇最便宜的一条修复，代码只改两行的顺序（§07.7 四种写法之一）。

### 四种减少死锁的写法，每种都带代价

| 写法 | 怎么做 | 为什么有效（指到机制） | 代价 |
| --- | --- | --- | --- |
| **① 加锁顺序一致** | 所有更新同一批行的路径，一律按**主键升序**（`ORDER BY id`）访问；批处理先 `SELECT id ... ORDER BY id` 再逐条更新 | 有序访问 ⇒ wait-for graph 只能形成**一条链**，不能成环。环的必要条件被去掉 | 业务逻辑有时不肯排序（如「按金额降序处理」）；排序本身要一次全量读（可能命中大范围扫描，反而锁更多） |
| **② 事务更短** | 把「读—算—写」里的 RPC/HTTP/文件 IO 挪到事务外；批处理每 500~1,000 行提交一次 | 冲突窗口 = 持锁时间；把窗口从秒降到毫秒，交叉概率近似线性下降 | 失去「整批原子」；中途失败要幂等续跑；提交次数变多 ⇒ fsync 次数上升（§07.8 账一） |
| **③ 用唯一索引把范围锁变点锁** | 给「当前读的 WHERE」建唯一索引（或补成覆盖唯一键的等值条件），把形态 ③④⑤ 变成形态 ① | 唯一索引等值命中 ⇒ 记录锁 + 零间隙 ⇒ 不锁任何空隙，环的两个必要（区间重叠）消失 | 多一份索引的写放大与空间（→ [05](05-innodb-page-and-row.md) §05.6）；唯一约束本身可能与脏数据冲突，建索引前要做数据校验 |
| **④ 批量拆小批 + 带退避重试** | 大批量按主键区间切片（`id BETWEEN a AND b`，每片 500~2,000 行）；1213/1205 捕获后**指数退避 + jitter + 幂等**重试，单事务最多重试 3 次 | 小批 ⇒ 持锁时间短、单次代价小；退避 + jitter 打散重试潮（否则退化成 §07.1 事故三） | 加工时要幂等（版本号/去重表）；退避拉长总时长；切片边界要固定，否则「边跑边改」会漏行 |

四种写法的共同点：**它们改的都是「环的形成条件」或「冲突窗口的长度」，没有一种在改「锁的参数」。** 把 `innodb_lock_wait_timeout` 从 50 秒调到 5 秒，只是让失败来得更快，死锁本身一个都不会少。

### 骨架一 · Java：只对瞬时冲突重试，退避 + jitter + 幂等

```java
// 1213（死锁）/ 1205（锁等待超时）都是「可以重来」，但绝不能在原地立即重来
static final int MAX_ATTEMPTS = 3;
static final long BASE_MS = 20;

<T> T withLockRetry(Supplier<T> tx, String bizKey) {
  for (int attempt = 1; ; attempt++) {
    try {
      return tx.get();                       // 事务必须短：只放 UPDATE + 必要的一次读
    } catch (DataAccessException e) {
      if (!isTransientLockError(e) || attempt >= MAX_ATTEMPTS) throw e;
      long backoff = (long) (BASE_MS * Math.pow(2, attempt - 1));   // 20 → 40 → 80 ms
      long jitter  = ThreadLocalRandom.current().nextLong(backoff / 2 + 1);
      sleepQuietly(backoff + jitter);        // jitter 是打散重试潮的唯一手段
      log.warn("lock retry attempt={} key={} err={}", attempt, bizKey, e.getClass().getSimpleName());
    }
  }
}

static boolean isTransientLockError(DataAccessException e) {
  // MySQL: 1213 = ER_LOCK_DEADLOCK(SQLState 40001), 1205 = ER_LOCK_WAIT_TIMEOUT(41000)
  Throwable root = e.getMostSpecificCause();
  String state = (root instanceof SQLException se) ? se.getSQLState() : null;
  return "40001".equals(state) || "41000".equals(state);
}
```

三个要点都在注释里：**只重试瞬时错误**（把 `DuplicateKeyException` 一起重试就是把重复数据变成重复写入）；**必须退避 + jitter**（否则退化成 §07.1 事故三的同步重试潮）；**事务体必须幂等**（重试落在同一业务键上时，版本号或去重表要在事务内校验）。

### 骨架二 · Go：悲观锁 + 上下文超时，把锁等待装进 deadline

```go
// 关键：锁等待时长必须由调用方 deadline 决定，而不是由 innodb_lock_wait_timeout 决定
func DebitStock(ctx context.Context, db *sql.DB, sku, wh int64, n int) error {
	ctx, cancel := context.WithTimeout(ctx, 800*time.Millisecond) // 比网关超时更短
	defer cancel()

	tx, err := db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead})
	if err != nil {
		return err
	}
	defer tx.Rollback() // 提交后 Rollback 是 no-op

	// ① 唯一索引等值（sku_id, warehouse_id）⇒ §07.4 形态①，只锁 1 行
	var qty int
	err = tx.QueryRowContext(ctx,
		`SELECT qty FROM stock WHERE sku_id = ? AND warehouse_id = ? FOR UPDATE`,
		sku, wh).Scan(&qty)
	if err != nil {
		return err // ctx 到期时驱动会 KILL QUERY，锁等待不会拖到 50 秒
	}

	// ② 带条件的原子写：快照读判断过的余额，执行那一刻可能已不成立
	res, err := tx.ExecContext(ctx,
		`UPDATE stock SET qty = qty - ? WHERE sku_id = ? AND warehouse_id = ? AND qty >= ?`,
		n, sku, wh, n)
	if err != nil {
		return err
	}
	if aff, _ := res.RowsAffected(); aff == 0 {
		return ErrSoldOut // 影响 0 行 = 被别人抢光了，不是错误
	}
	return tx.Commit()
}
```

注意 `FOR UPDATE` 那一条是**唯一索引全列等值**——这是全篇唯一能承诺「只锁一行」的形态。把 `sku_id` 从唯一索引里去掉（§07.1 事故一），这个函数的所有超时保护都白写：`ctx` 到期只是把「等 50 秒」变成「等 0.8 秒」，锁的范围一行没少。

---

## 07.8 三本账：持锁时间、锁对象数与重试放大

### 账一：热点行更新的 QPS 上限 = `1 / 事务持锁时间`

这不是经验值，是**串行化**的直接结果：同一行的 X 锁互斥 ⇒ 任何时刻只有 1 个事务在改它 ⇒ 吞吐 = 1 / 每次占用的时长。

**前提（假设清单，换参数可复算）**：单行计数器 `UPDATE counter SET c = c + 1 WHERE id = 1`，事务里只有这一句；`innodb_flush_log_at_trx_commit = 1` 且 `sync_binlog = 1`（双 1）；NVMe 单次 fsync 0.2~0.6 ms；同机房 RTT 0.2 ms。

| 持锁时间 t 的构成 | 耗时 |
| --- | --- |
| 解析 + 优化 + 点查 + 加锁 | ≈ 0.05 ms |
| 改行 + 写 redo（内存）+ 写 binlog buffer | ≈ 0.05 ms |
| **提交的两次 fsync（redo + binlog）** | ≈ 0.4~1.2 ms ← 主项 |
| 网络往返（请求 + 提交确认） | ≈ 0.2~0.4 ms |
| **合计 t** | **≈ 2 ms**（乐观 1 ms，悲观 4 ms） |

于是单行理论吞吐 = `1 / t` = `1 / 0.002 s` = **500 TPS**。

| 量 | 推导 | 结果 |
| --- | --- | --- |
| 单行串行化上限 | `1 / 2 ms` | **500 TPS**（这是承诺值：锁语义决定的硬顶，不是压测值） |
| 实用上限（ρ = 0.7） | M/M/1：平均延迟 = `t / (1 − ρ)` = 2 ms / 0.3 | ≈ 6.7 ms 延迟，**350 TPS** |
| ρ = 0.9 时 | `t / (1 − ρ)` = 20 ms | 450 TPS 但延迟 10 倍，拐点附近抖动 |
| 分 16 个计数桶（`UPDATE ... WHERE bucket = rand(16)`） | `16 × 500`（桶之间无锁竞争） | 理论 **8,000 TPS**，实用 **5,600 TPS** |
| 提交侧压力校验 | 8,000 事务/s × 2 次 fsync = 16,000 次/s；NVMe 4K 随机写 5 万~10 万 IOPS | 占 16%~32%，**还有余量**（组提交会进一步合并） |
| 汇总读的成本 | `SELECT SUM(c) FROM counter` 扫 16 行 ≈ 1 次范围扫 + 2~3 次页访问 | ≈ 10 µs，可忽略 |
| 继续加桶到 1,024 | 理论 51.2 万 TPS；实际瓶颈换成连接数、组提交队列、CPU | **收益递减**：桶数超过「每桶仍能命中同一 cache line」的规模后，唯一收益是减少锁冲突，而成本是聚合读变宽 |

三条必须记住的推论：

1. **建索引、加机器、换 SSD 都提高不了这 500 TPS**。它们能缩短 t 里的每一项，但 t 里的主项是两次 fsync（换 `innodb_flush_log_at_trx_commit=0` 可以把 t 压到 0.3 ms ⇒ 3,300 TPS，代价是崩溃丢最近 1 秒的已提交事务——这是**用 RPO 换吞吐**，必须写进预案）。
2. **分桶是唯一能线性扩展这个数字的办法**，因为它把「一行的串行」变成「N 行的并行」。代价落在读侧：任何要「精确当前值」的读必须 `SUM` 聚合，且**不能再用单行 `SELECT ... FOR UPDATE` 做原子扣减**（业务要变成「分桶写 + 异步汇总」，读到的是近似值）。
3. **拐点不是 500，是 350**。M/M/1 的延迟曲线在 ρ→1 时是发散的：从 70% 利用率到 90%，吞吐只涨 28%，平均延迟涨 3 倍。所有「明明没到 QPS 上限，P99 却崩了」的热点行事故，都发生在这条曲线上。

> **量级结论**（非承诺值）：单行计数器 ≈ 500 TPS 硬顶、分 16 桶 ≈ 8,000 TPS 理论 / 5,600 实用。假设清单：双 1 提交、NVMe、同机房、事务体只有一条 UPDATE、无其他锁竞争。若持锁时间变成 5 ms（事务里多了一次 RPC），单行上限直接掉到 200 TPS，桶数需求翻 2.5 倍。

### 账二：批量 `UPDATE` 的锁对象数与超时概率

**前提**：表 10 万行，行宽 200 B，聚簇索引叶子页约装 78 行（16,256 B 可用 / 208 B 行足迹）；语句是 `UPDATE t SET flag = 1 WHERE biz_no = 'X'`，而 `biz_no` 上**没有索引**；隔离级别 RR（换成 RC 只去掉间隙，锁的行数完全相同）；该表写入 QPS ≈ 300。

| # | 量 | 推导 | 结果 |
| --- | --- | --- | --- |
| ① | 逻辑锁对象 | 被扫过的记录 10 万行（无索引 ⇒ 全表扫，一行不落）+ 1 条 supremum 伪记录（挡住所有插入） | **100,001** 个 |
| ② | 叶子页数 | `100,000 / 78` | ≈ 1,282 页 |
| ③ | 物理锁结构 | 按 (事务 × 索引 × 页 × 模式) 聚合 ⇒ `1,282 × 1~2` | 1.3k ~ 2.6k 个 |
| ④ | 锁内存 | 2k × 300~400 B（死锁日志 `heap size 1136 / 3 struct` 反推） | **0.4 ~ 1.0 MB** |
| ⑤ | 朴素模型对照 | 每行一个 40 B 对象：`100,000 × 40 B` | 4 MB（差 4~10 倍，但同样 < 10 MB） |
| ⑥ | 持锁时间 T | 每行「扫到 + 加锁 + 冲突检查 + 写 undo/redo」≈ 20 µs，区间 15~30 µs ⇒ `100,000 × 20 µs` | **2 s** |
| ⑦ | 排队事务数 | `300/s × 2 s` | 600 个写事务在排队 |
| ⑧ | 等待分布 | 平均 ≈ `T/2`；P99 ≈ `T` | 1 s / 2 s，都远低于 50 s |
| ⑨ | 超时门槛 | `50 s / 2 s` | **25 条串联**；n ≥ 26 时队尾必然 1205 |

**这张账的结论与直觉相反**：无索引 `UPDATE` 的危险不在「锁太多把内存撑爆」，而在「**锁太久把并发变成 1**」。所以修复方向永远是**减少锁的对象数**（补索引：10 万 → 1 行，T 从 2 s → 20 µs，即 10 万倍），而不是「调大 `innodb_lock_wait_timeout` 让它等一下」——后者只是把队列排得更长。

> **量级结论**（非承诺值）：10 万行无索引 `UPDATE` ≈ 10 万个锁对象、1.3k~2.6k 个锁结构、0.4~1.0 MB 锁内存、2 s 持锁时间；并发同类语句 ≥ 26 条时队尾必然 1205。假设清单：行宽 200 B、每行加锁开销 20 µs、表写 QPS 300。若行宽 2 KB（页装 8 行），锁结构数翻 10 倍但仍只有几 MB——**内存项怎么调假设都不会成为瓶颈，时间项变一行就崩**。

### 账三：MDL 队列效应——一个 30 分钟的长事务 + 每秒 480 次查询

**前提**：`ticket` 表读 430 QPS、写 50 QPS（合计 480 QPS）；应用连接池 200，获取连接超时 3 s，网关超时 3 s；09:41 一个报表会话 `START TRANSACTION` 之后一直挂着不提交；10:02:07 提交一条 `ALGORITHM=INSTANT` 的 `ALTER`（真实耗时 0.04 s）；`lock_wait_timeout` 默认 31,536,000 s ⇒ DDL 永不放弃。

| 阶段 | 发生什么 | 推导 / 量级 |
| --- | --- | --- |
| t = 0 | `ALTER` 申请 `MDL_EXCLUSIVE`，与报表会话的 `MDL_SHARED_READ` 冲突 ⇒ 进入 **PENDING 队头** | — |
| 0 ~ 0.42 s | 后续每条查询申请 `MDL_SHARED_READ` 都与「队头 PENDING 的 X」冲突 ⇒ 全部排队；连接池打满 | `200 / 480 ≈ 0.42 s` |
| 0.42 s 之后 | MySQL 侧 200 个会话全在等 MDL；应用侧每秒 480 个请求拿不到连接 | 有效吞吐 = `200 / 3 s ≈ 67 次/s`，且全是失败 |
| 持续 30 min | **灾害形态不是「队列很长」，是「并发瞬间归零」** | 理论积压 `480 × 1,800 = 86.4 万`，但连接池把并发封顶在 200，剩下的在应用侧立刻失败 |
| 内存账 | MySQL 侧 200 个等待线程 × 单连接内存（`thread_stack` 1 MB 量级虚拟 + `net_buffer_length` 16 KB 起 + 会话状态） | ≈ 200~400 MB **虚拟**，RSS 增量通常 < 100 MB；应用侧队列 1,000 × 几 KB ≈ 数 MB ⇒ 内存不是主角 |
| 为什么不自愈 | `lock_wait_timeout` 默认一年 ⇒ DDL 不放弃 ⇒ 队列永不清空，直到有人 `KILL` 长事务 | 这是「必须人工干预」的物理原因 |
| 恢复 | `KILL` 长事务（只读事务回滚亚秒级；若是写事务则按 comp/06 §06.7 的账，回滚时长可能接近正向时长）→ DDL 0.04 s 完成 → 队列瞬间放行 | 应用侧熔断/连接重建还需 1~3 min |

> **量级结论**（非承诺值）：30 分钟长事务 + 480 QPS ⇒ 全表查询在 0.42 s 内从「慢」变成「不可用」，期间失败请求数 ≈ 480 × 1,800 = 86.4 万次（按连接池封顶后实际失败率取决于重试策略），MySQL 侧内存增量 100 MB 量级。假设清单：连接池 200、超时 3 s、无熔断。**若设了熔断与 `lock_wait_timeout=5`，同样场景的不可用时间从 44 分钟降到 5 秒加一次重试。**

---

## 07.9 架构边界：锁能保证什么、不能保证什么

| 能力 | 能保证 | 不能保证（反例） | 靠什么绕过 |
| --- | --- | --- | --- |
| 当前读的互斥 | 一个事务持有区间锁期间，别人改不了区间内的行、插不进区间内的空隙（RR） | 不保证「你的检查在执行那一刻还成立」——RC 下没有间隙锁，幻读照旧（→ comp/06 §06.6） | 带条件的原子写（`WHERE qty >= ?`）+ 唯一约束 + 幂等 |
| 一致性 | 单实例内多行多表的锁可以构成一个事务的原子性 | 跨实例无锁语义：`XA`/`Seata` 是协调层，不是锁（→ comp/43） | 单写点 + 分片键路由，让「一个业务实体只落一个实例」 |
| 并发扩展 | 行锁让不同行可以并行写 | **同一行的写永远是串行的**（1 / 持锁时间，§07.8 账一）；同一区间的插入永远排队 | 分桶（热点行）、分片（跨实例）、把同步扣减改成异步汇总 |
| 结构变更 | MDL 保证「改结构」与「读写数据」互斥 | 保证不了「不停服」：一个 PENDING 的 X 会把后面所有查询一起挡（§07.6） | INSTANT/INPLACE + 先杀长事务 + 小 `lock_wait_timeout` + gh-ost |
| 死锁 | 检测到环就牺牲一个事务，系统不会永久停住 | 不保证「不牺牲你的业务」：被回滚的可能是刚写完一半的批任务 | 统一加锁顺序 + 短事务 + 幂等重试 |
| 可观测 | `data_locks`/`data_lock_waits`/`metadata_locks` 能看到实时锁与等待链 | 看不到历史（除 `innodb_print_all_deadlocks` 的死锁日志）；`Innodb_row_lock_*` 不含 MDL | 把锁等待做成时序指标（§07.11） |

**⑤ 扩展维度的答案（本篇不许含糊）**：锁**不能横向扩**。副本不解决写锁（所有写在主库上抢同一把锁），加 CPU 也不解决（串行就是串行）。三条路只有三条：**分片**（把锁拆到不同实例，→ [eng/36](../eng/36-mysql-limits-sharding.md)）、**分桶**（把一行拆成 N 行，§07.8 账一）、**垂直切**（把高频写的小表挪到 KV/内存结构，代价是失去事务）。第四条「批处理摊薄」对锁不成立——批量越大锁越久，方向正好相反。

---

## 07.10 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| # | 现象 | 根因（指到物理形态） | 怎么定位 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- | --- |
| 1 | 一句 `UPDATE`/`FOR UPDATE` 只该改 1 行，却让整张表的写全部排队 | `WHERE` 条件上没索引（或被函数/隐式转换废掉）⇒ 全表扫描 ⇒ 扫过的每一行都加 next-key，等价锁全表（§07.4 形态 ⑤） | `EXPLAIN` 看 `type=ALL`；`data_locks` 按 `INDEX_NAME` 聚合后行数 = 表行数；`innodb_trx.trx_rows_locked` 巨大 | 补索引把形态变成 ①；立刻止血可 `KILL` 该事务 | 上线前对 `UPDATE/DELETE/FOR UPDATE` 强制 `EXPLAIN` 门禁；`type=ALL` 直接拒绝合并 |
| 2 | RC 环境下「先查后插」产生**重复数据**，而 RR 环境报的是 1213 死锁 | RC 下没有间隙锁 ⇒ 两个并发事务都插成功；RR 下间隙锁/S 锁曾「偶然」制造等待或死锁，把重复挡住了（§07.5） | 对比两个隔离级别的复现；查 `data_locks` 是否有 `X,GAP`/`S` | 把正确性交给**唯一索引** + `INSERT ... ON DUPLICATE KEY UPDATE` / `INSERT IGNORE` | 「防重复靠约束，不靠锁」写进编码规范；RC 成为默认后专项审计「先查后插」 |
| 3 | 一条 `INSERT INTO t SELECT ... FROM s` 让源表写入卡住几分钟 | RR 下对源表**扫过的行**加共享 next-key 锁；语句结束才提交，锁窗口 = 整条语句时长 | `data_locks` 里源表出现大量 `S`/`S,REC_NOT_GAP`；`SHOW PROCESSLIST` 见到该语句挂着 | 拆成「先按主键分批查 → 应用层批量 INSERT」；或 RC + `binlog_format=ROW`（退化为记录锁） | 跨表搬数据一律走分批任务，禁止单条 `INSERT ... SELECT` 搬大表 |
| 4 | 乐观锁突然失效：并发更新互相覆盖，且**没有任何报错** | `version INT UNSIGNED` 溢出回绕。4,294,967,295 / 1,000 次每秒 = 4.29e6 s ≈ **49.7 天**，回绕后 `version` 与旧值相等 ⇒ `WHERE version = ?` 命中 | 查 `MAX(version)` 是否接近 42 亿；查该行 24h 内的更新次数 | 改 `BIGINT`；或每次更新后校验 `affected_rows = 1`；或改成状态机 CAS | 所有乐观锁列一律 `BIGINT UNSIGNED`；把「版本号是该行写频率 × 保留天数的函数」写进模板 |
| 5 | 锁超时/死锁之后系统整体雪崩，写入 QPS 反而涨到 3 倍 | 重试无退避、多层同时重试（网关 × SDK × 业务 = 3³ = 27 倍）、失败者同一时刻重试（§07.1 事故三） | 看 `Innodb_deadlocks` 速率与业务重试日志；对比「请求量」与「事务数」的比值 | 只在一层重试 + 指数退避 + jitter + 熔断 + 幂等（§07.7 骨架一） | 重试策略做成统一组件，禁止业务代码里裸 `for` 重试 |
| 6 | 大表加字段的 DDL 挂 40 分钟，全站查询超时 | 一个长事务持有 `MDL_SHARED_READ`；DDL 申请 `MDL_EXCLUSIVE` 进入 PENDING；队列公平性让后续**所有**查询（含只读）排队（§07.6） | `performance_schema.metadata_locks` 里 `LOCK_STATUS='PENDING'`；`sys.schema_table_lock_waits` 直接给阻塞者 | 先 `KILL` 长事务；DDL 会话 `SET SESSION lock_wait_timeout=5` 快速失败重试 | 变更平台流程：DDL 前自动跑长事务巡检（> 60 s 阻断）+ 限制事务年龄 |
| 7 | DDL 卡住时把 `innodb_lock_wait_timeout` 调小，毫无效果 | 两套超时：`lock_wait_timeout`（默认 31,536,000 s）管 **MDL**，`innodb_lock_wait_timeout`（默认 50 s）管 **InnoDB 行锁**。DDL 在等前者 | 确认等待出现在 `metadata_locks` 而不是 `data_locks` | 改 `lock_wait_timeout`（会话级） | 运维手册把两个参数并列写死；告警里按 `metadata_locks` / `data_locks` 分两个面板 |
| 8 | `INSERT` 被卡住，监控显示「行锁等待」在涨，误判为热点行竞争 | 别人的**非唯一索引等值 next-key 锁**（或范围锁）挡住了插入空隙；请求方持有的是 `X,GAP,INSERT_INTENTION`（§07.5） | `data_locks` 里同时看到 `X` / `X,GAP`（持锁方）与 `X,GAP,INSERT_INTENTION`（等待方） | 缩短或点锁化持锁方的语句（唯一索引）；插入型业务考虑 RC | 对「范围当前读 + 插入」的组合做代码审查项 |

---

## 07.11 观测：指标、拐点与告警

**纪律：每条都带版本与开关前提**——写错一个视图名，读者就会认为整篇没验证过。

| 观测项 | 版本 + 开关前提 | 它回答什么 | 拐点 | 告警骨架 |
| --- | --- | --- | --- | --- |
| `performance_schema.data_locks` | **8.0+**，`performance_schema=ON`（默认 ON）。5.7 及以前叫 `information_schema.innodb_locks`，**8.0 已删除** | 「现在到底锁了哪些对象、什么模式」 | 单表 `RECORD` 锁行数 > 表行数的 1% 即可疑 | 巡检：按 `INDEX_NAME` 聚合的行数 > 1,000 告警 |
| `performance_schema.data_lock_waits`（`sys.innodb_lock_waits` 是它的视图） | **8.0+**，同上；5.7 的 `information_schema.innodb_lock_waits` 已删 | 「谁在等谁、等的是哪个锁」 | 等待链长度 ≥ 3 | 链长 ≥ 5 或单链持续 > 1 s |
| `Innodb_row_lock_waits` / `Innodb_row_lock_time_avg` / `Innodb_row_lock_time_max` / `Innodb_row_lock_current_waits` | 全版本（`SHOW GLOBAL STATUS`）；只在 InnoDB 行锁层面统计，**不含 MDL、不含 AUTO-INC** | 行锁争用的总量与平均时长 | `time_avg` > 100 ms；`current_waits` > 20 | `avg > 200 ms` 持续 5 分钟；`current_waits > 50` |
| `Innodb_deadlocks` | **5.7+**（`SHOW GLOBAL STATUS` 计数器） | 死锁（检测到的环）速率；**不含 1205 超时** | > 1 次/分钟即可疑 | `rate(Innodb_deadlocks[5m]) > 1` 并附最新死锁日志 |
| `performance_schema.metadata_locks` | **8.0+**；表本身默认有数据；若要看等待**耗时**，需 `wait/lock/metadata/sql/mdl` instrument（在 `setup_instruments` 里确认 `ENABLED`，不同版本默认值不一致，上线前实测） | 「谁持有 MDL、谁在排队」 | `LOCK_STATUS='PENDING'` 且持续 > 1 s | PENDING 数 ≥ 1 持续 10 s ⇒ 一级告警（带 `OBJECT_NAME`） |
| `sys.schema_table_lock_waits` | **8.0+**（基于 `metadata_locks` 与 `threads` 拼装） | 一条 SQL 给出「等待者 ↔ 阻塞者 ↔ 两条 SQL」 | 任意一行 | 与 DDL 变更单联动：变更期间轮询 |
| `information_schema.innodb_trx` 的 `trx_started` / `trx_rows_locked` / `trx_lock_structs` / `trx_query` | 全版本 | 最老事务、锁大头、它正在跑什么 | 事务年龄 > 60 s；`trx_rows_locked` > 10 万 | 年龄 > 60 s 报警（→ comp/06 §06.10 同一指标） |
| `SHOW ENGINE INNODB STATUS` 的 `LATEST DETECTED DEADLOCK` + `innodb_print_all_deadlocks` | 死锁段全版本；`innodb_print_all_deadlocks` **5.6+，默认 OFF** | 死锁的完整现场（§07.7 的逐行解读） | 同上 | 生产必须 `ON`（量小），否则只能看到最近一次 |

**代码块 · 锁与 MDL 的四个巡检查询（8.0）**

```sql
-- ① 谁在等谁（8.0：data_lock_waits 代替 information_schema.innodb_lock_waits）
SELECT w.REQUESTING_ENGINE_TRANSACTION_ID AS waiting_trx,
       w.BLOCKING_ENGINE_TRANSACTION_ID  AS blocking_trx,
       l.OBJECT_SCHEMA, l.OBJECT_NAME, l.INDEX_NAME,
       l.LOCK_TYPE, l.LOCK_MODE, l.LOCK_DATA,
       t.trx_started, t.trx_rows_locked, LEFT(t.trx_query, 60) AS blocking_sql
  FROM performance_schema.data_lock_waits w
  JOIN performance_schema.data_locks l
    ON l.ENGINE_TRANSACTION_ID = w.BLOCKING_ENGINE_TRANSACTION_ID
   AND l.ENGINE_LOCK_ID        = w.BLOCKING_ENGINE_LOCK_ID
  LEFT JOIN information_schema.innodb_trx t
    ON t.trx_id = w.BLOCKING_ENGINE_TRANSACTION_ID;

-- ② MDL：谁把 DDL 堵住了（8.0）
SELECT OBJECT_SCHEMA, OBJECT_NAME, LOCK_TYPE, LOCK_DURATION, LOCK_STATUS,
       OWNER_THREAD_ID, OWNER_EVENT_ID
  FROM performance_schema.metadata_locks
 WHERE LOCK_STATUS = 'PENDING';

-- ③ MDL 等待链（8.0 的 sys 视图，直接给两边 SQL）
SELECT waiting_pid, waiting_query, blocking_pid, blocking_query
  FROM sys.schema_table_lock_waits;

-- ④ 长事务 / 锁大头巡检（全版本，DDL 前必跑）
SELECT trx_id, trx_state, trx_started,
       TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS age_s,
       trx_rows_locked, trx_lock_structs, trx_isolation_level,
       LEFT(trx_query, 80) AS query_head
  FROM information_schema.innodb_trx
 ORDER BY trx_started ASC
 LIMIT 20;
```

排查顺序（顺序错了就永远只能看到「慢了」）：**① 看 `data_lock_waits` 有没有等待链 → ② 有链就看持锁方在跑什么、事务多老 → ③ 没有行锁等待却全站超时，立刻看 `metadata_locks`**。第三跳是最容易被漏掉的一跳：MDL 事故在行锁面板上完全无痕。

---

## 07.12 一页速记

### 判据表：加锁形态 × 隔离级别

| 场景 | RR 下加什么锁 | RC 下加什么锁 | 该用什么 |
| --- | --- | --- | --- |
| 唯一索引**等值命中** | 单条记录锁（无间隙） | 单条记录锁 | **唯一正确形态**，所有热点写都该长这样 |
| 唯一索引**等值未命中** | 纯间隙锁 | **无锁**（RC 不加间隙） | 别把「查一个不存在的值」当互斥手段 |
| **非唯一索引等值** | next-key + 向右锁到下一键值前 | 记录锁（不挡插入） | 高并发插入业务选 RC，或把索引做成唯一 |
| **范围扫描** | 区间内 next-key + 两端边界间隙 | 记录锁（幻读会出现） | 防幻读必须 RR 且 `FOR UPDATE` 早于竞争者插入 |
| **无索引** | 全表 next-key + supremum | 全表记录锁（一样锁全表） | **一律禁止**；RC 也救不了 |
| **走二级索引回表** | 二级索引记录 + 聚簇索引记录（×2） | 同（×2） | 覆盖索引省的是回表，**不省锁**；锁的对象数不减 |

### 十条一口价

1. 锁加在**索引记录**上，不在行上；同一句 SQL 换索引就换加锁范围。
2. next-key = 记录锁 + 前面的间隙；**间隙部分只挡插入，不挡改**。
3. 只有「唯一索引全列等值命中」才是 1 行记录锁，其余形态都可能锁一段区间。
4. 无索引的当前读 = 锁全表；**RC 下也锁全表**（只是没有间隙）。
5. 间隙锁之间互不冲突，插入意向锁之间互不冲突；冲突只发生在「插入 vs 间隙锁」和「唯一键冲突的 S 锁」上。
6. `innodb_autoinc_lock_mode` 8.0 默认 **2**（interleaved），安全前提是 `binlog_format=ROW`。
7. 热点行 QPS 上限 = `1 / 事务持锁时间`：2 ms ⇒ 500 TPS；分 16 桶 ⇒ 8,000 理论 / 5,600 实用。
8. 死锁检测开销与活跃事务数成正比；关掉它只是把死锁换成 50 s 超时，死锁本身不减。
9. MDL 的寿命是**事务级**，DDL 会排在队头并把后续所有查询一起堵住 ⇒ DDL 前杀长事务、设 `lock_wait_timeout=5`。
10. `lock_wait_timeout` 管 MDL（默认一年），`innodb_lock_wait_timeout` 管行锁（默认 50 s）——两个参数不能互相替代。

---

## 07.13 自检题

**1. `UPDATE big SET v = 1 WHERE biz_no = 'X'`，`biz_no` 上没有索引，表 800 万行，实际命中 3 行。RR 下锁多少行？RC 下呢？为什么？**
<details><summary>答案</summary>

RR 下锁住**全表 800 万行 + supremum**：`biz_no` 没索引 ⇒ 只能全表扫聚簇索引 ⇒ 扫过的每一行都加 next-key（§07.4 形态 ⑤）。RC 下同样锁住这 800 万行，只是锁的模式退化成**记录锁、没有间隙**——锁的行数一模一样。任何「RC 没有间隙锁所以无索引更新不锁全表」的说法都是错的：间隙锁只管「插入」，管不了「改」。修复不是换隔离级别，是**给 `biz_no` 建索引**，让形态从 ⑤ 变成 ③（甚至补唯一索引变成 ①）。
</details>

**2. 两个事务往同一段空隙插不同的值不冲突，插同一个唯一键就冲突。分别解释这两个「不冲突/冲突」的机制。**
<details><summary>答案</summary>

不冲突：`INSERT` 加的是**插入意向锁**（间隙锁的变种），插入意向锁之间互相兼容——它们声明的只是「我要在这个位置插」，位置不同就不冲突（除非要插的位置与别人的间隙锁重叠）。冲突：唯一性检查要求后来的事务对**已存在那条记录**加 **S 锁**去确认「它是否已提交/是否可见」，而前任插入者持有该记录的 X 锁 ⇒ S vs X 冲突 ⇒ 排队。所以并发插入同 key 的排队点不是间隙锁，是**唯一约束检查的 S 锁**——这也是「两条看起来无害的 `INSERT` 互相锁死」的死锁来源。
</details>

**3. 一条语句明明写了 `SELECT ... WHERE id = ? FOR UPDATE`，为什么另一个事务还能改这一行？**
<details><summary>答案</summary>

四种可能，都指向「锁落在别处」：(a) 传参类型与列类型不一致（`VARCHAR` 列传数字）或列被函数包裹 ⇒ 索引失效 ⇒ 走全表扫，锁的是扫过的每一行，而你以为锁的那一行可能压根没被定位到、或者被别人先加了记录锁；(b) `id` 不唯一（或条件是唯一索引的**部分列**）⇒ 非唯一索引等值形态，锁的是 next-key 区间而非单行；(c) 你要改的那条语句走的是**另一个索引**，锁的是另一棵索引上的记录，行级互斥需要两边的聚簇索引记录锁重叠才成立；(d) 隔离级别是 RC 且对方改的是同一行——这种情况下它会被 X 锁挡住，说明你看到的「改了」其实是它在**你提交之后**按快照/当前读改了同一行。定位方法只有一个：`data_locks` 里看 `INDEX_NAME` + `LOCK_MODE` + `LOCK_DATA`，别猜。
</details>

**4. 一条 `ALGORITHM=INSTANT` 的 DDL 只耗时 40 ms，为什么能让全站不可用 44 分钟？给出因果链与最小修复。**
<details><summary>答案</summary>

因果链：长事务持有 `MDL_SHARED_READ`（寿命是**事务级**）→ DDL 申请 `MDL_EXCLUSIVE` 冲突 → 进入 PENDING 队头 → MDL 排队是**公平**的，后续每条查询申请 `MDL_SHARED_READ` 时与「队头的 PENDING X」冲突 → **只读查询也排队** → 连接池 200 个连接在 `200/480 ≈ 0.42 s` 内打满 → 从「慢」变成「不可用」；而 `lock_wait_timeout` 默认 31,536,000 s，队列永不自愈。最小修复两行：① DDL 前巡检 `information_schema.innodb_trx` 杀掉 > 60 s 的事务；② DDL 会话 `SET SESSION lock_wait_timeout = 5`，把「排队堵死全站」变成「自己 5 秒失败后重试」。
</details>

**5. 单行计数器为什么是 500 TPS 硬顶？分 16 桶后到多少，代价是什么？**
<details><summary>答案</summary>

同一行的 X 锁互斥 ⇒ 同一时刻只有一个事务在改它 ⇒ 吞吐 = `1 / 事务持锁时间`。双 1 提交下，持锁时间主项是两次 fsync（redo + binlog）约 0.4~1.2 ms，加上解析/点查/网络 ≈ 2 ms ⇒ 500 TPS（实用拐点在 ρ=0.7，约 350 TPS）。分 16 桶：桶之间没有锁竞争，理论 `16 × 500 = 8,000 TPS`，实用 5,600；提交侧 8,000 事务/s × 2 次 fsync = 16,000 次/s，对 NVMe 只占 16%~32%，还有余量。代价：① 读侧必须 `SUM` 聚合，读到的是近似/延迟值；② 不能再靠单行 `SELECT ... FOR UPDATE` 做原子扣减；③ 桶数超过一定规模后收益递减，瓶颈换成连接数、组提交与 CPU。建索引、加机器、换 SSD 都不改变这个上限——它们只能缩短持锁时间。
</details>

---

## 07.14 延伸阅读

- 快照读为什么一行锁都不加、RC 与 RR 的差别、幻读的另一半答案：→ comp/06
- 页、行格式、聚簇索引与「二级索引 = 主键表」——理解「锁挂在哪棵树的哪条记录上」的前提：→ comp/05
- 当前读为什么走不到索引、优化器成本模型与索引失效：→ comp/09
- 三份日志与内部 2PC：提交时的 fsync 就是 §07.8 账一里 `t` 的主项：→ comp/08
- 主从复制与并行回放：大事务既锁得久又拖复制延迟：→ comp/10
- 单表极限与分库分表拐点（锁无法横向扩时的唯一出路）：→ [eng/36](../eng/36-mysql-limits-sharding.md)
- 本地事务边界、`@Transactional` 失效与「先查后写」的正确写法：→ [eng/9](../eng/9-transaction-decision-tree.md)
- 分布式锁与 fencing token（跨实例没有行锁可用时要买什么）：→ comp/46

