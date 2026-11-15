# 31. DAG 编排与依赖管理

> **状态**：`已完成（初稿）` ｜ **所属**：第五部分 · 编排与状态机 ｜ **编号**：31 / 52
> **本章定位**：第 29 章解决了「谁拥有下一步」，第 30 章解决了「这一步怎么落库」，本章解决**「哪些步可以一起做」**——DAG 只管并行结构，不管许可规则。读完你能自己写一个带环检测、join 语义、SKIP LOCKED 领取的 DAG 执行器。
> **读法**：§31.2 的边界表是本章的锚，先看它；§31.4.2 的三种落库形态决定你后面三年的运维成本；§31.4.4 的 join 语义是线上事故最高发的一格。

---

## 31.1 事故现场：一个 `all_done` 放水，1,240 万元错账

2025 年 6 月 11 日，某消费金融的日终对账 DAG 跑完了，绿色。23 个任务全绿，看板上一个红点都没有。

**02:07**，`extract-bill` 调第三方账单接口，连吃 3 个 502，任务标 `FAILED`。
**02:09**，`join-normalize` 照常启动——因为它的 `trigger_rule` 被人从 `all_success` 改成了 `all_done`（三个月前为了"别让一个抽取失败卡住整条链"）。
**02:11 ~ 02:38**，8 个分片并行跑完，用的是**上一日的账单分区**（下游 `WHERE biz_date = '{{ds}}'` 但账单表当天分区是空的，被 `LEFT JOIN` 静默兜成 NULL）。
**08:50**，财务发现日结差额 1,240 万元，涉及 **86,400 笔**应结未结。

事后查下来是四个独立缺陷叠在一起：

| # | 缺陷 | 单独看严重吗 | 叠加后 |
| --- | --- | --- | --- |
| 1 | `trigger_rule=all_done`，上游失败仍放行 | 中 | 让错误数据进入下游 |
| 2 | `retryStrategy` 的 `backoff` 固定 30 秒，对 502 无意义 | 低 | 3 次重试在 90 秒内烧完 |
| 3 | 抽取任务"半成功"：写了 3 个分区中的 2 个，任务失败但数据留下 | 高 | 下游读到残缺但"看起来非空"的分区 |
| 4 | 合并任务只校验"分片都返回了"，不校验行数 | 高 | 差异无人发现，直到财务 |

还有一个隐形成本：为了补这一天，他们把 `depends_on_past=True` 的链路全部串行重跑，**补一天数据花了 6 小时 40 分**，因为补数不能并行。

### 事故二：并行度是"局部字段"，不是"全局配额"

两个月后的 2025 年 8 月 3 日，同一套 DAG 又炸了一次，这次不是数据错，是**把生产库打死了**。

| 时刻 | 事件 |
| --- | --- |
| 09:12 | 业务方要求补 7 月 1 日 ~ 7 月 31 日共 31 天的对账数据（渠道费率调整，历史要重算） |
| 09:20 | 运维在界面上勾选 31 个 `biz_date`，点"批量补数" |
| 09:21 | 31 个 `dag_run` 同时进入 `RUNNING`；每个 run 有 8 个分片，共 **248 个任务**同时就绪 |
| 09:23 | 业务库连接数从 180 冲到 **512**（`max_connections=500`），开始出现 `Too many connections` |
| 09:26 | 生产下单接口错误率从 0.02% 升到 **34%**，客服电话开始响 |
| 09:34 | DBA 看到连接数曲线，先扩容到 800，无效（瓶颈是 DB CPU，不是连接上限） |
| 09:41 | DBA 强杀 120 个连接，补数任务批量 `FAILED`，生产恢复 |
| 09:58 | 错误率回到基线，**累计 32 分钟 5xx、4.2 万次下单失败** |
| 10:20 | 剩余 28 天补数改成分批（每批 2 天、串行），跑到 15:40 才完 |

根因三条，一条比一条普遍：

1. **`parallelism: 8` 是"每个 run 内"的上限**，跨 run 没有任何约束。31 个 run × 8 = 248，配置看起来"很保守"，实际是 248 并发。
2. **补数和生产共用连接池**。补数是"可以慢"的负载，生产是"不能慢"的负载，它们必须在资源层被隔离（独立连接池/独立只读副本）。
3. **"批量补数"按钮没有排队**。它把 31 个 run 一次性创建，等价于给了系统一个 31 倍的瞬时脉冲。

> 🩸 **记住两句话**：
> ① DAG 的每个"绿色"，都只是一句**"我声明这些依赖满足了"**。声明写错，绿色就是假的。
> ② **并行度有三个层次：任务内、run 内、集群内。** 只配中间那层，等于没配——因为出事的时候，压力永远来自"多个 run 同时开跑"。

---

## 31.2 到底是什么：DAG 管「能不能并行」，状态机管「允不允许」

### 定义

> **DAG（有向无环图）是对「任务之间偏序关系」的声明式描述**：节点是可执行的**单元**，边是**"必须等它完成"**。无环保证「存在至少一个可执行顺序」，也就是**可终止**。

它只回答三个问题：

1. **哪些任务之间没有边？** → 可以同时做。
2. **一个任务的前置集合是什么？** → 什么时候可以开始。
3. **图上有没有环？** → 这份定义能不能被执行。

它**不回答**：这一步现在允不允许做（那是守卫）、做失败了谁负责（那是终态负责人）、等一个人三天算不算正常（那是 SLA）。

### 边界：两个模型在同一件事上的分工

我们拿退款流程里"回补库存"和"记退款分录"这两步来对比：

| 问题 | DAG 的回答 | 状态机的回答 |
| --- | --- | --- |
| 回补库存和记分录能同时做吗？ | ✅ **能**（两条边都从 `refund.approved` 出发，无相互依赖） | ❌ 状态机不表达并行，它只会说"现在处于 `REFUNDING`" |
| 现在能做记分录吗？ | ❌ DAG 不知道，它只看前置任务完成没有 | ✅ 冲到 `REFUNDING` 且金额校验通过才允许 |
| 账务系统返回"该笔已冲正"怎么办？ | DAG 只说 `FAILED`，重试逻辑要看配置 | ✅ `REFUND_SUCCEEDED` 事件被幂等消费，状态不变 |
| 等了 72 小时人工审批，谁负责？ | 弱：只能靠 task 内部 sleep/轮询，占着 worker | ✅ 落到 `WAITING_HUMAN`，worker 释放，靠 SLA 巡检兜 |
| 分片 8 个里有 1 个失败，整批算成功吗？ | ✅ 这是 DAG 的**核心语义**（join 规则） | ❌ 状态机没有"部分成功"这个概念 |

一句话判据：

```
DAG     回答「哪些可以一起做」   —— 结构（偏序、并行度、扇出扇入）
状态机  回答「此刻允许做什么」   —— 规则（守卫、许可、终态）
两者不是二选一：DAG 的每个节点内部，都是一个小的任务状态机
```

### 两层模型（这是本章最该带走的结构）

```text
┌──────────────────────────────────────────────────────────┐
│ 第一层：DAG（结构）                                       │
│   dag_run:  RUNNING                                       │
│   ┌────────┐   ┌────────┐   ┌────────┐                   │
│   │extractA│   │extractB│   │extractC│   ← 同一层可并行   │
│   └───┬────┘   └───┬────┘   └───┬────┘                   │
│       └────────────┼────────────┘                        │
│                    ▼  join（语义在这里！）                 │
│              ┌──────────┐                                │
│              │ normalize│                                │
│              └────┬─────┘                                │
│        ┌──────────┼──────────┐   ← 扇出 8 个分片          │
│        ▼          ▼          ▼                           │
│     [shard0]  [shard1] ... [shard7]                      │
│        └──────────┼──────────┘                           │
│                   ▼  merge（扇入）                        │
│              ┌──────────┐                                │
│              │ publish  │                                │
│              └──────────┘                                │
├──────────────────────────────────────────────────────────┤
│ 第二层：每个节点的任务状态机（规则）                       │
│   PENDING → RUNNING → SUCCESS                             │
│      ↑         ├──→ RETRY_WAIT ──(退避到期)──┘            │
│      │         ├──→ FAILED（重试用尽/不可重试）            │
│      │         └──→ SKIPPED（条件不满足，主动跳过）        │
│      └── UPSTREAM_FAILED（上游死了，永不执行）             │
└──────────────────────────────────────────────────────────┘
```

> 💡 **为什么必须两层**：只做第一层（很多自研调度器的通病），你会失去"为什么这个节点被跳过"的可解释性；只做第二层（状态机原教旨），你会把 8 个分片建模成 8 个状态，状态数随分片数爆炸。

### 三个反例：这些都不是 DAG 该干的事

```text
反例 1：用 DAG 表达"等人审批 3 天"
  节点 approve 里写 sleep(86400) 轮询 —— 占着一个 worker slot
  正解：节点只发审批请求，Run 进入 WAITING_HUMAN 落库释放资源（第 34 章）

反例 2：用状态机表达并行扇出
  SHARD_0_DONE / SHARD_1_DONE / ... / SHARD_7_DONE —— 状态爆炸
  正解：一个 DAG 的 8 个同构节点 + 一个 join

反例 3：DAG 节点有副作用但不幂等，然后配 retries=5
  "发短信"任务重试 5 次 = 5 条短信
  正解：DAG 的重试是"任务级重试"，它假定任务幂等；不幂等的任务必须包一层幂等键（第 2 章）
```

---

## 31.3 结构图：一个真实数据管道 DAG 的拓扑

某支付公司日终对账管道（23 个节点压成 9 个逻辑节点）：

```text
                    biz_date = D
                         │
      ┌──────────────────┼──────────────────┐
      ▼                  ▼                  ▼
 ┌─────────┐       ┌──────────┐      ┌──────────┐
 │ orders  │       │ payments │      │  ledger  │   ① 抽取（并行 3）
 │ 2.1M 行 │       │ 1.8M 行  │      │ 3.4M 行  │
 └────┬────┘       └────┬─────┘      └────┬─────┘
      └─────────────────┼─────────────────┘
                        ▼
                 ┌─────────────┐
                 │  normalize  │                      ② 清洗归一（join: all_success）
                 └──────┬──────┘
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
   ┌─────────┐    ┌─────────┐     ┌─────────┐
   │ shard 0 │    │ shard 1 │ ... │ shard 7 │        ③ 分片核对（扇出 8，并行度上限 8）
   └────┬────┘    └────┬────┘     └────┬────┘
        └───────────────┼───────────────┘
                        ▼
                 ┌─────────────┐
                 │   merge     │                      ④ 汇总（join: 8/8 全成功 + 行数校验）
                 └──────┬──────┘
            ┌───────────┴───────────┐
            ▼                       ▼
     ┌─────────────┐         ┌─────────────┐
     │   report    │         │   settle    │          ⑤ 出报表 / 触发结算（可并行）
     └─────────────┘         └─────────────┘
                                    │
                                    ▼
                             ┌─────────────┐
                             │ notify-fin  │          ⑥ 财务通知（旁路，失败不影响终态）
                             └─────────────┘
```

**读图要点**：

- ① 三个抽取之间**没有边** → 天然并行；但它们共享一个 `pool`（下游 DB 连接），并行度必须被池子限制，不是"能并行就全上"。
- ④ 是**扇入**，也是整个图上唯一"数据完整性"的判定点。**事故 90% 出在这一格**（见 §31.4.4）。
- ⑥ 是**旁路**：它失败只影响通知，不能把 `dag_run` 判成 `FAILED`。这就是第 29 章的"只能有一个终态负责人"在图上的样子。

---

## 31.4 怎么实现

### 31.4.1 依赖、并行度、重试、跳节点：六个旋钮

| 旋钮 | 要解决的问题 | 关键实现点 | 配错的后果 |
| --- | --- | --- | --- |
| **依赖** | 谁必须先完成 | 显式声明边；**禁止隐式依赖**（靠时间先后、靠文件存在、靠"反正它先跑"） | 漏边 = 读到旧数据；多边 = 白白串行 |
| **并行度** | 同时开几个 | 三层限制：run 内 `parallelism` / 集群级配额 / 资源池（DB 连接、API 配额） | 只限一层：要么打爆下游（事故二），要么 CPU 闲一半 |
| **重试** | 失败了再试几次 | 指数退避 + 抖动；区分**可重试**（超时、5xx、连接断）与**不可重试**（4xx、校验失败）；非幂等任务禁自动重试 | 固定 30 秒重试 3 次 = 对故障无感，纯浪费 |
| **跳节点** | 某些情况不必执行 | `SKIPPED` 必须是**独立状态**，且区分 `SKIPPED_CONDITION`（条件不满足）与 `UPSTREAM_FAILED`（被连坐） | 混成一个 `SKIPPED`，事后无法判断"少的数据是本来就没有，还是丢了" |
| **幂等** | 重跑不出双份 | 任务参数带幂等键；输出**覆盖写**（写临时分区 + 原子 promote）；扇出用 `(run_id, task_key, shard)` 唯一定位 | 汇总翻倍、重复通知、重复扣款 |
| **超时** | 多久算死 | 三层：任务 `timeout`（执行侧）/ run `deadline_at`（引擎侧）/ `lease_until`（收尸侧） | "永远 RUNNING"的任务占着 slot，整条链静默停摆 |

> ⚠️ **重试的第一原则**：**重试只对"可重入"的任务有意义**。抽取任务重跑一次覆盖同分区是安全的；"把结果 append 到汇总表"重跑一次就是双倍。判据很硬：`重跑 N 次的结果 == 重跑 1 次的结果`？否则先把任务改成幂等（第 2 章），再谈重试。

### 31.4.2 DAG 的三种落库形态

| 形态 | 存什么 | 优点 | 缺点 | 适合 |
| --- | --- | --- | --- | --- |
| **① 任务表**（每 run 每任务一行） | 运行态：state、attempt、时间戳 | 查询最快，索引天然，状态机原生 | 结构（边）无处安放，只能塞 JSON 列 | **所有方案的运行态都必须用这个** |
| **② 邻接表**（`dag_edge`） | 结构：from → to | 可 SQL 递归查上下游、可做血缘分析 | 每次调度要 join；大图（万级边）查询慢 | 需要血缘/影响面分析的图 |
| **③ JSON 定义**（一列大 JSON） | 结构 + 参数 + 重试策略 | 版本化天然、整体校验便宜、改定义不动表 | 无法索引内部字段，SQL 查"谁依赖 X"要全表扫 | **定义侧首选**，配合 ① 使用 |

**工程结论（不要纠结）**：**定义存 JSON（带版本号），运行态存任务表，血缘需求强再加邻接表做派生索引**。下面给完整 DDL。

```sql
-- ---------- 定义侧：版本化，只增不改 ----------
CREATE TABLE dag_definition (
  id          BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  dag_key     VARCHAR(64)  NOT NULL COMMENT '逻辑名，如 daily-recon',
  version     INT          NOT NULL,
  spec        JSON         NOT NULL COMMENT '节点/边/重试/join/超时 全在里面',
  spec_hash   CHAR(64)     NOT NULL COMMENT 'spec 的 sha256，用于快速比对',
  status      VARCHAR(16)  NOT NULL DEFAULT 'DRAFT'
              CHECK (status IN ('DRAFT','ACTIVE','ARCHIVED')),
  created_by  VARCHAR(64)  NOT NULL,
  created_at  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_dag_version (dag_key, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------- 运行态：一次 DAG 运行 ----------
CREATE TABLE dag_run (
  id               BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  dag_key          VARCHAR(64)  NOT NULL,
  definition_id    BIGINT       NOT NULL COMMENT '实例绑定版本，改定义不影响在途',
  run_key          VARCHAR(96)  NOT NULL COMMENT '业务幂等键：daily-recon:2026-02-14',
  state            VARCHAR(24)  NOT NULL DEFAULT 'PENDING'
                   CHECK (state IN ('PENDING','RUNNING','SUCCEEDED',
                                    'FAILED','CANCELLED','TIMED_OUT')),
  trigger_type     VARCHAR(16)  NOT NULL COMMENT 'CRON|MANUAL|BACKFILL|UPSTREAM',
  triggered_by     VARCHAR(64)  NOT NULL,
  biz_date         DATE         NOT NULL,
  deadline_at      DATETIME(3)  NOT NULL COMMENT '整个 run 的硬超时',
  started_at       DATETIME(3)  NULL,
  finished_at      DATETIME(3)  NULL,
  created_at       DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_run_key (run_key),          -- 重复触发直接被拦，这是 DAG 级幂等
  KEY idx_state_created (state, created_at),
  KEY idx_dag_bizdate (dag_key, biz_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------- 运行态：一个任务节点 ----------
CREATE TABLE dag_task (
  id           BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  dag_run_id   BIGINT       NOT NULL,
  task_key     VARCHAR(64)  NOT NULL COMMENT '定义里的节点名',
  shard_index  INT          NOT NULL DEFAULT 0 COMMENT '扇出分片序号，非分片为 0',
  state        VARCHAR(24)  NOT NULL DEFAULT 'PENDING'
               CHECK (state IN ('PENDING','RUNNING','SUCCESS','FAILED',
                                'RETRY_WAIT','SKIPPED','UPSTREAM_FAILED',
                                'CANCELLED')),
  attempt      INT          NOT NULL DEFAULT 0,
  max_attempts INT          NOT NULL DEFAULT 3,
  ready_at     DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
               COMMENT '最早可被领取时间：重试退避就是把它往后推',
  lease_until  DATETIME(3)  NULL COMMENT 'worker 租约，过期即视为丢失，由巡检回收',
  worker_id    VARCHAR(64)  NULL,
  version      INT          NOT NULL DEFAULT 0 COMMENT 'CAS 抢占用，见 §31.4.3',
  output_meta  JSON         NULL COMMENT '行数、文件路径、校验和：扇入校验要靠它',
  error_code   VARCHAR(64)  NULL,
  error_msg    VARCHAR(1024) NULL,
  started_at   DATETIME(3)  NULL,
  finished_at  DATETIME(3)  NULL,
  UNIQUE KEY uk_run_task_shard (dag_run_id, task_key, shard_index),
  KEY idx_runnable (state, ready_at),        -- 调度器热路径
  KEY idx_lease (state, lease_until)         -- 租约回收
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

三处设计细节，每处都对应一类事故：

1. **`uk_run_key`**：`daily-recon:2026-02-14` 唯一。cron 抖动、手动补数、上游重推同时发生时，第二个插入直接失败——这是**DAG 级幂等**，比在调度器里判重可靠一万倍。
2. **`definition_id` 绑定版本**：改定义只影响新建实例。第 30 章 §30.4 的"定义版本漂移"在 DAG 里同样致命——一次 join 语义修改能让 3,000 个在途 run 集体走错分支。
3. **`ready_at` 而不是 `next_retry_at`**：把"可运行"统一成一个时间条件，调度 SQL 就永远只有一条 `WHERE state='PENDING' AND ready_at<=NOW()`。退避、延迟、定时全部退化成"改这个时间"。

**邻接表 + 递归 CTE**（只在需要血缘/影响面分析时才建，作为 JSON 定义的派生索引）：

```sql
CREATE TABLE dag_edge (
  id            BIGINT      NOT NULL AUTO_INCREMENT PRIMARY KEY,
  definition_id BIGINT      NOT NULL,
  from_task     VARCHAR(64) NOT NULL,
  to_task       VARCHAR(64) NOT NULL,
  join_mode     VARCHAR(24) NOT NULL DEFAULT 'all_success'
                COMMENT '从 from 汇入 to 时的聚合语义，见 §31.4.4',
  UNIQUE KEY uk_edge (definition_id, from_task, to_task),
  KEY idx_to (definition_id, to_task)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

```sql
-- 问：改了 payments 表，会影响哪些下游任务？（影响面分析，发布前必查）
WITH RECURSIVE down AS (
  SELECT to_task AS task, 1 AS depth
    FROM dag_edge WHERE definition_id = :defId AND from_task = :task
  UNION ALL
  SELECT e.to_task, d.depth + 1
    FROM dag_edge e JOIN down d ON e.from_task = d.task
   WHERE e.definition_id = :defId AND d.depth < 20   -- 深度上限，防御脏数据成环
)
SELECT task, MIN(depth) AS depth FROM down GROUP BY task ORDER BY depth, task;
```

> ⚠️ **递归 CTE 一定要带上 `depth < N`**。虽然定义侧有环检测，但定义表是"人改的"，历史数据里一定存在过脏边。没有深度上限的递归在 MySQL 里会撞 `cte_max_recursion_depth`（默认 1000）报错，在 Postgres 里会跑到内存爆掉。**用 `UNION ALL` 而不是 `UNION` 也是刻意的**：`UNION` 去重会让递归提前终止并隐藏重复路径，你就看不到"这个节点被两条路径影响"的事实。

### 31.4.3 执行引擎的五个核心问题

先看调度模型。**"就绪"这件事是谁算出来的**，决定了你的吞吐上限和故障恢复方式：

| 调度模型 | 就绪怎么算 | 典型吞吐 | 进程重启后 | 复杂度 | 适用 |
| --- | --- | --- | --- | --- | --- |
| **DB 轮询（pull）** | 每分钟查一次 `PENDING AND ready_at<=NOW()` | 百~千任务/秒 | 无损，状态全在库里 | 低 | 默认选择，90% 场景 |
| **事件驱动（完成触发）** | 任务完成时同事务算出下游就绪集 | 万任务/秒 | 无损 | 中（要防漏算、要防重复触发） | 大图、低延迟要求 |
| **推送队列（Redis/Kafka）** | 入队即就绪，worker 消费 | 万级/秒 | **有损**：队列里的消息可能丢，需 DB 兜底对账 | 中 | 短任务、高吞吐 |
| **K8s Job 编排（Argo）** | controller 监听 CRD 变化 | 受 etcd/apiserver 限制（千级 pod） | 无损（CRD 即状态） | 低（运维 K8s 除外） | 容器化批处理 |
| **单机内存定时器** | 进程内时间轮 | 单机万级 | **全丢** | 最低 | 只适合"丢了也无所谓"的定时任务 |
| **外部调度中心（XXL-JOB）** | 中心按 cron 触发 HTTP | 千级触发/秒 | 无损（触发记录落库） | 低 | 定时任务为主，无复杂依赖 |

> ⚠️ **最常见的架构错误**：用"推送队列"做唯一真相源。队列消息一旦丢失（或消费失败进 DLQ 却没人看），任务就永久消失，而 `dag_task` 里那一行还停在 `PENDING`——**你有两个真相源，它们不一致时你不知道该信谁**。正确做法：**DB 是真相，队列只是加速器**；队列丢了，轮询兜底会把它捡回来。

**执行引擎要解决的五个问题**：

| 问题 | 本质 | 实现手段 | 不做会怎样 |
| --- | --- | --- | --- |
| **调度** | 谁来决定"现在能跑哪些" | 轮询 `state='PENDING' AND ready_at<=NOW()` + `FOR UPDATE SKIP LOCKED`；或事件驱动（任务完成时算出新就绪集） | 全表扫 + 锁竞争，10 万任务时调度器自己成瓶颈 |
| **重试** | 失败后怎么再来一次 | 状态回到 `PENDING`，`attempt+1`，`ready_at += backoff(attempt)`，**并检查 attempt < max_attempts** | 无限重试打爆下游；或一次失败就整条链红 |
| **幂等** | 重试/重复投递不出双份 | 任务级幂等键写进任务参数；输出**覆盖写**而不是追加；扇出用 `(run_id, task_key, shard)` 唯一定位 | 双重计数、重复发送、汇总翻倍 |
| **超时** | 单任务和整个 run 的上限 | 两层：`task.timeout`（worker 侧）与 `run.deadline_at`（引擎侧）；租约 `lease_until` 是第三层兜底（worker 崩了谁来收尸） | "永远 RUNNING"的任务占着 slot，整条链静默停摆 |
| **补偿** | 中途放弃时已做的事怎么办 | DAG **没有天然回滚**。要么每个节点自带 `undo`，要么在 run 级别挂一个补偿子 DAG（第 13 章） | 前 5 步成功了，第 6 步失败，前 5 步的副作用留在世界上 |

**用 `SKIP LOCKED` 领取任务**（Postgres 语法，多 worker 并发安全）：

```sql
-- 一次领取 N 个就绪任务；多个 worker 并发执行不会抢到同一行
WITH picked AS (
  SELECT id FROM dag_task
   WHERE state = 'PENDING'
     AND ready_at <= NOW(3)
   ORDER BY ready_at, id
   LIMIT :n
   FOR UPDATE SKIP LOCKED
)
UPDATE dag_task t
   SET state = 'RUNNING',
       attempt = attempt + 1,
       worker_id = :worker,
       started_at = NOW(3),
       lease_until = NOW(3) + INTERVAL '90 second'
  FROM picked p
 WHERE t.id = p.id
RETURNING t.id, t.task_key, t.shard_index, t.attempt;
```

> ⚠️ MySQL 8.0 没有 `UPDATE ... RETURNING`。等价写法是两条语句：先 `SELECT ... FOR UPDATE SKIP LOCKED` 拿到 id 列表（同一事务内），再 `UPDATE ... WHERE id IN (...)`。**必须在同一事务**，否则 `SKIP LOCKED` 的语义就丢了（锁随事务结束释放）。

**Go：就绪判定 + 领取 + 完成回写**（核心 40 行，无框架）：

```go
// Claim 领取就绪任务。返回空切片不是错误，只是"现在没活"。
func (s *Scheduler) Claim(ctx context.Context, workerID string, n int) ([]Task, error) {
	rows, err := s.db.QueryContext(ctx, `
		WITH picked AS (
		  SELECT id FROM dag_task
		   WHERE state='PENDING' AND ready_at <= NOW(3)
		   ORDER BY ready_at, id LIMIT $1
		   FOR UPDATE SKIP LOCKED)
		UPDATE dag_task t SET state='RUNNING', attempt=attempt+1,
		       worker_id=$2, started_at=NOW(3),
		       lease_until=NOW(3) + INTERVAL '90 second'
		  FROM picked p WHERE t.id=p.id
		RETURNING t.id, t.dag_run_id, t.task_key, t.shard_index, t.attempt`, n, workerID)
	if err != nil {
		return nil, fmt.Errorf("claim: %w", err)
	}
	defer rows.Close()
	var out []Task
	for rows.Next() {
		var t Task
		if err := rows.Scan(&t.ID, &t.RunID, &t.Key, &t.Shard, &t.Attempt); err != nil {
			return nil, err
		}
		out = append(out, t)
	}
	return out, rows.Err()
}

// Finish 回写结果并推进下游。整个函数在一个事务里：任务状态 + 就绪推进 必须原子。
// result.Err 为 nil 表示成功；否则按可重试性决定 RETRY_WAIT 还是 FAILED。
func (s *Scheduler) Finish(ctx context.Context, t Task, result Result) error {
	return s.inTx(ctx, func(tx *sql.Tx) error {
		switch {
		case result.Err == nil:
			_, err := tx.ExecContext(ctx, `
				UPDATE dag_task SET state='SUCCESS', finished_at=NOW(3),
				       lease_until=NULL, output_meta=$2
				 WHERE id=$1 AND state='RUNNING'`, t.ID, result.Meta)
			if err != nil { return err }

		case result.Retryable && t.Attempt < t.MaxAttempts:
			backoff := time.Duration(1<<uint(t.Attempt)) * 30 * time.Second // 30s,60s,120s...
			if backoff > 10*time.Minute { backoff = 10 * time.Minute }
			_, err := tx.ExecContext(ctx, `
				UPDATE dag_task SET state='PENDING', ready_at=NOW(3)+$2::interval,
				       lease_until=NULL, error_code=$3, error_msg=$4
				 WHERE id=$1 AND state='RUNNING'`,
				t.ID, backoff.String(), result.Code, truncate(result.Msg, 1000))
			if err != nil { return err }

		default: // 不可重试，或次数用尽
			_, err := tx.ExecContext(ctx, `
				UPDATE dag_task SET state='FAILED', finished_at=NOW(3),
				       lease_until=NULL, error_code=$2, error_msg=$3
				 WHERE id=$1 AND state='RUNNING'`, t.ID, result.Code, truncate(result.Msg, 1000))
			if err != nil { return err }
		}
		return s.advanceDownstream(ctx, tx, t.RunID) // 见 §31.4.5
	})
}
```

> ⚠️ **上面三个 `WHERE` 是简化版，只校验了 `state='RUNNING'`。生产上必须再加两个条件**：`AND worker_id = :me AND attempt = :attempt`。原因很硬：租约 90 秒过期，worker A 可能只是 GC 停顿时长了一点，它醒来后**并不知道自己已经失去资格**，仍会 `Finish`。此时任务已被 worker B 接管并执行完毕，A 的写入会把 B 的结果覆盖成 A 的（或者把 `SUCCESS` 改回 `FAILED`）。这两个条件就是 **fence（栅栏）**——老 worker 的 `attempt` 已经过期，写不进去。

**租约回收（reaper）**，独立进程，每 30 秒一次：

```sql
-- 回收"worker 已死但没交卷"的任务。留 30 秒宽限，避免误杀 GC 停顿的 worker。
UPDATE dag_task
   SET state = 'PENDING', worker_id = NULL, lease_until = NULL,
       ready_at = NOW(3), version = version + 1,
       error_code = 'LEASE_EXPIRED'
 WHERE id IN (
   SELECT id FROM dag_task
    WHERE state = 'RUNNING'
      AND lease_until < NOW(3) - INTERVAL 30 SECOND
    ORDER BY lease_until
    LIMIT 200
    FOR UPDATE SKIP LOCKED
 )
RETURNING id, dag_run_id, task_key, attempt;
```

**Java 版：抢占 + 完成（带 fence）**：

```java
@Component
public class DagTaskClaimer {

    private final JdbcTemplate jdbc;
    private final TransactionTemplate tx;

    public DagTaskClaimer(JdbcTemplate jdbc, PlatformTransactionManager tm) {
        this.jdbc = jdbc;
        this.tx = new TransactionTemplate(tm);
    }

    /** 抢占一个就绪任务。empty 只表示"现在没活"，不是错误。 */
    public Optional<ClaimedTask> claim(String workerId) {
        return tx.execute(status -> {
            List<Long> ids = jdbc.queryForList("""
                SELECT id FROM dag_task
                 WHERE state = 'PENDING' AND ready_at <= NOW(3)
                 ORDER BY ready_at, id
                 LIMIT 1
                 FOR UPDATE SKIP LOCKED
                """, Long.class);
            if (ids.isEmpty()) {
                return Optional.<ClaimedTask>empty();
            }
            long id = ids.get(0);
            int n = jdbc.update("""
                UPDATE dag_task
                   SET state = 'RUNNING', attempt = attempt + 1,
                       worker_id = ?, started_at = NOW(3),
                       lease_until = NOW(3) + INTERVAL 90 SECOND,
                       version = version + 1
                 WHERE id = ? AND state = 'PENDING'
                """, workerId, id);
            if (n != 1) {                       // 极小概率：被并发 UPDATE 抢先
                return Optional.<ClaimedTask>empty();
            }
            return Optional.ofNullable(jdbc.queryForObject("""
                SELECT id, dag_run_id, task_key, shard_index, attempt
                  FROM dag_task WHERE id = ?
                """, (rs, i) -> new ClaimedTask(
                        rs.getLong("id"), rs.getLong("dag_run_id"),
                        rs.getString("task_key"), rs.getInt("shard_index"),
                        rs.getInt("attempt")), id));
        });
    }

    /**
     * 交卷。fence 条件 (worker_id, attempt) 保证"租约过期后复活的旧 worker"写不进来。
     * 返回 0 行 = 我已经失去资格，结果必须丢弃（并打 WARN，这是重要观测信号）。
     */
    public boolean complete(long taskId, String workerId, int attempt,
                            String state, JsonNode outputMeta, String errCode) {
        int n = jdbc.update("""
            UPDATE dag_task
               SET state = ?, finished_at = NOW(3), lease_until = NULL,
                   output_meta = ?, error_code = ?, version = version + 1
             WHERE id = ? AND state = 'RUNNING'
               AND worker_id = ? AND attempt = ?
            """, state, outputMeta, errCode, taskId, workerId, attempt);
        if (n == 0) {
            log.warn("stale worker write rejected, task={} worker={} attempt={}",
                     taskId, workerId, attempt);
        }
        return n == 1;
    }
}
```

### 31.4.4 扇出与扇入：三种 join 语义，三种事故

**扇出（fan-out）** 是一对多：`normalize` 一个节点产出 8 个分片任务。**扇入（fan-in / join）** 是多对一：8 个分片怎么合成一个结论。

问题永远出在扇入。**"8 个分片全成功了"和"8 个分片都结束了"是两件完全不同的事。**

| join 语义 | 放行条件 | 什么时候是对的 | 误用后果 |
| --- | --- | --- | --- |
| **all_success**（默认） | 上游**全部 SUCCESS** | 数据完整性要求高（对账、结算、报表） | 一个分片抖动 → 整批不跑；但这是"正确的失败" |
| **all_done** | 上游全部结束（含 FAILED / SKIPPED） | 收尾类任务：清理临时目录、发"本次跑完了"通知 | **§31.1 的事故**：拿脏数据往下跑，静默错账 |
| **one_success** | 至少 1 个 SUCCESS | 多副本抢主（A/B 两个源任一可用） | 8 个分片里 1 个成功就出报表 → 87.5% 数据缺失 |
| **none_failed_min_one_success** | 没有 FAILED 且有 SKIPPED 也算过 | 有条件分支的图（部分分片被 `when` 条件跳过） | 分支条件写错时，"跳过整个分支"被当成正常 |
| **部分成功（阈值）** | 成功比例 ≥ 阈值（如 95%） | 大规模扇出（1,000 个租户分片），个别租户数据源不可用属正常 | **必须把失败的清单落库**，否则"阈值内"等于"永久无人管" |

**部分成功的正确写法**（不要只看比例）：

```go
// JoinDecision 扇入判定：返回是否放行 + 必须落库的失败清单
func JoinDecision(policy Policy, shards []ShardResult) (bool, error) {
	if len(shards) == 0 {
		return false, errors.New("join: no shards, definition bug")
	}
	var ok, failed, skipped int
	var failedKeys []string
	for _, s := range shards {
		switch s.State {
		case "SUCCESS":
			ok++
		case "FAILED", "UPSTREAM_FAILED":
			failed++
			failedKeys = append(failedKeys, fmt.Sprintf("%s#%d", s.TaskKey, s.Shard))
		case "SKIPPED":
			skipped++
		}
	}
	switch policy.Mode {
	case "all_success":
		return failed == 0 && skipped == 0, nil
	case "all_done":
		return true, nil // 危险，仅限清理类
	case "threshold":
		// 阈值不是"大概齐"：失败清单必须落库并开告警，否则等于掩盖
		if float64(ok)/float64(len(shards)) < policy.MinRatio {
			return false, nil
		}
		// 关键补充：行数守卫。分片数达标 ≠ 数据完整
		if err := checkRowCount(policy.ExpectedRows, shards, policy.RowTolerance); err != nil {
			return false, err
		}
		log.Warn("join passed with failures", "failed", failedKeys, "ratio", policy.MinRatio)
		alert.Report("dag_join_partial", failedKeys)
		return true, nil
	}
	return false, fmt.Errorf("unknown join mode %q", policy.Mode)
}
```

> 🩸 **扇入的唯一可靠判据不是"分片状态"，是"数据量"**。分片全 SUCCESS 但总行数是平时的 30%，照样是事故（上游静默少给数据）。所以 §31.1 的第 4 条缺陷才是真正致命的那条——**join 必须校验行数/金额，而不是只看任务状态**。

### 31.4.5 拓扑排序与环检测（完整可跑）

Kahn 算法有两个好处：**天然分层**（同层可并行，直接告诉你并行度上限）+ **顺带检测环**（出队数 ≠ 节点数即有环）。生产上还要再加一个 DFS：Kahn 只告诉你"有环"，DFS 能告诉你**"是哪几个节点成环"**——否则用户拿着一份 200 节点的 YAML 猜。

```go
package dag

import (
	"errors"
	"fmt"
	"sort"
)

var (
	ErrCycle      = errors.New("dag: cycle detected")
	ErrUnknownDep = errors.New("dag: unknown dependency")
)

type Graph struct {
	nodes map[string]struct{}
	succ  map[string][]string // from -> [to...]
	indeg map[string]int
}

func New() *Graph {
	return &Graph{nodes: map[string]struct{}{}, succ: map[string][]string{}, indeg: map[string]int{}}
}

func (g *Graph) AddNode(id string) {
	if _, ok := g.nodes[id]; !ok {
		g.nodes[id] = struct{}{}
		g.indeg[id] = 0
	}
}

func (g *Graph) AddEdge(from, to string) error {
	g.AddNode(from)
	g.AddNode(to)
	if from == to {
		return fmt.Errorf("%w: self loop %q", ErrCycle, from)
	}
	g.succ[from] = append(g.succ[from], to)
	g.indeg[to]++
	return nil
}

// Layers 返回分层拓扑序：同一层的任务彼此无依赖，可以并行。
// 复杂度 O(V+E)；环检测是副产品（出队数 < 节点数 即有环）。
func (g *Graph) Layers() ([][]string, error) {
	indeg := make(map[string]int, len(g.indeg))
	for k, v := range g.indeg {
		indeg[k] = v
	}
	var cur []string
	for id, d := range indeg {
		if d == 0 {
			cur = append(cur, id)
		}
	}
	sort.Strings(cur) // 保证输出稳定，测试可比对

	var out [][]string
	seen := 0
	for len(cur) > 0 {
		out = append(out, cur)
		seen += len(cur)
		var next []string
		for _, from := range cur {
			for _, to := range g.succ[from] {
				indeg[to]--
				if indeg[to] == 0 {
					next = append(next, to)
				}
			}
		}
		sort.Strings(next)
		cur = next
	}
	if seen != len(g.nodes) {
		return nil, fmt.Errorf("%w: %d/%d nodes reachable", ErrCycle, seen, len(g.nodes))
	}
	return out, nil
}

// FindCycle 三色 DFS，返回一条真实的环路径，如 [a b c a]，直接报给用户。
func (g *Graph) FindCycle() []string {
	const white, gray, black = 0, 1, 2
	color := make(map[string]int, len(g.nodes))
	var stack, cycle []string

	ids := make([]string, 0, len(g.nodes))
	for id := range g.nodes {
		ids = append(ids, id)
	}
	sort.Strings(ids)

	var dfs func(string) bool
	dfs = func(n string) bool {
		color[n] = gray
		stack = append(stack, n)
		for _, m := range g.succ[n] {
			switch color[m] {
			case gray: // 回边：m 还在栈里，说明成环
				for i := len(stack) - 1; i >= 0; i-- {
					if stack[i] == m {
						cycle = append(append([]string{}, stack[i:]...), m)
						return true
					}
				}
			case white:
				if dfs(m) {
					return true
				}
			}
		}
		stack = stack[:len(stack)-1]
		color[n] = black
		return false
	}

	for _, id := range ids {
		if color[id] == white && dfs(id) {
			return cycle
		}
	}
	return nil
}
```

**Java 版：发布前的 DAG 校验门禁**（放进 CI，`mvn test` 就跑）。它比 Go 版多了两件事：**重复边去重**（重复边会让入度虚高，任务永远不就绪）与**分层结果直接给并行度上限**。

```java
public final class DagValidator {

    public record Edge(String from, String to) {}
    public record Result(boolean ok, List<List<String>> layers, List<String> cycle) {}

    public static Result validate(Collection<String> nodes, Collection<Edge> edges) {
        Set<String> nodeSet = new LinkedHashSet<>(nodes);
        Map<String, Set<String>> succ = new LinkedHashMap<>();
        Map<String, Integer> indeg = new LinkedHashMap<>();
        for (String n : nodeSet) {
            succ.put(n, new LinkedHashSet<>());
            indeg.put(n, 0);
        }
        for (Edge e : edges) {
            if (!nodeSet.contains(e.from()) || !nodeSet.contains(e.to())) {
                throw new IllegalArgumentException("edge references unknown node: " + e);
            }
            if (succ.get(e.from()).add(e.to())) {     // Set.add=false → 重复边，直接忽略
                indeg.merge(e.to(), 1, Integer::sum);
            }
        }

        // Kahn 分层：同层可并行，层宽 = 该层并行度下限需求
        List<List<String>> layers = new ArrayList<>();
        List<String> cur = indeg.entrySet().stream()
                .filter(en -> en.getValue() == 0)
                .map(Map.Entry::getKey).sorted().toList();
        int seen = 0;
        while (!cur.isEmpty()) {
            layers.add(cur);
            seen += cur.size();
            List<String> next = new ArrayList<>();
            for (String from : cur) {
                for (String to : succ.get(from)) {
                    if (indeg.merge(to, -1, Integer::sum) == 0) next.add(to);
                }
            }
            Collections.sort(next);
            cur = next;
        }
        if (seen != nodeSet.size()) {
            return new Result(false, layers, findCycle(succ));
        }
        return new Result(true, layers, List.of());
    }

    // 三色 DFS 找一条真实环路径，报错时直接指出是哪几个节点
    private static List<String> findCycle(Map<String, Set<String>> succ) {
        Map<String, Integer> color = new HashMap<>();   // 0 白 / 1 灰 / 2 黑
        Deque<String> stack = new ArrayDeque<>();
        for (String start : succ.keySet()) {
            List<String> c = dfs(start, succ, color, stack);
            if (c != null) return c;
        }
        return List.of();
    }

    private static List<String> dfs(String n, Map<String, Set<String>> succ,
                                    Map<String, Integer> color, Deque<String> stack) {
        color.put(n, 1);
        stack.addLast(n);
        for (String m : succ.get(n)) {
            int c = color.getOrDefault(m, 0);
            if (c == 1) {                                // 回边：m 还在栈里 = 成环
                List<String> cycle = new ArrayList<>();
                boolean hit = false;
                for (String s : stack) {
                    if (s.equals(m)) hit = true;
                    if (hit) cycle.add(s);
                }
                cycle.add(m);                            // 闭合：a → b → c → a
                return cycle;
            }
            if (c == 0) {
                List<String> inner = dfs(m, succ, color, stack);
                if (inner != null) return inner;
            }
        }
        stack.removeLast();
        color.put(n, 2);
        return null;
    }
}
```

CI 里的用法（校验失败即拒绝合并，**不要等到运行时才发现环**）：

```java
@Test
void everyDagDefinitionIsAcyclic() throws Exception {
    for (Path p : Files.newDirectoryStream(Path.of("dags"), "*.yaml")) {
        DagSpec spec = DagSpecLoader.load(p);                 // 解析 nodes/edges
        DagValidator.Result r = DagValidator.validate(spec.nodes(), spec.edges());
        assertTrue(r.ok(), () -> p + " has cycle: " + String.join(" -> ", r.cycle()));
        assertTrue(r.layers().size() <= 32, () -> p + " too deep: " + r.layers().size());
        // 顺带检查：任何一层的宽度不应超过集群配额，否则一上线就打满
        int widest = r.layers().stream().mapToInt(List::size).max().orElse(0);
        assertTrue(widest <= spec.maxParallelism(),
                () -> p + " layer width " + widest + " > parallelism " + spec.maxParallelism());
    }
}
```

配套的 YAML（Argo Workflows 语法，字段与官方 v1alpha1 一致，以对应版本为准）：
```yaml
apiVersion: argoproj.io/v1alpha1
kind: Workflow
metadata:
  generateName: daily-recon-
  labels: { pipeline: daily-recon, owner: fin-data }
spec:
  entrypoint: daily-recon
  serviceAccountName: argo-data
  parallelism: 8                      # 全局并行度：保护下游 DB，不是"能并行就全上"
  ttlStrategy: { secondsAfterCompletion: 604800 }
  podGC: { strategy: OnWorkflowSuccess }   # 失败时保留 pod，否则日志没了没法查
  arguments:
    parameters:
      - { name: biz_date, value: "2026-02-14" }
  templates:
    - name: daily-recon
      dag:
        tasks:
          - { name: extract-orders,   template: extract, arguments: { parameters: [{ name: src, value: orders }] } }
          - { name: extract-payments, template: extract, arguments: { parameters: [{ name: src, value: payments }] } }
          - { name: extract-ledger,   template: extract, arguments: { parameters: [{ name: src, value: ledger }] } }
          - name: normalize
            template: normalize
            dependencies: [extract-orders, extract-payments, extract-ledger]  # all_success
          - name: recon-shard
            template: shard
            dependencies: [normalize]
            withParam: "[0,1,2,3,4,5,6,7]"    # 扇出 8 个分片
          - name: merge-recon
            template: merge
            dependencies: [recon-shard]        # 扇入：8/8 全成功才放行
          - name: notify-fin
            template: notify
            dependencies: [merge-recon]
            continueOn:
              failed: true                     # 旁路：通知失败不能把整个 run 判红
    - name: extract
      inputs:
        parameters: [{ name: src }]
      retryStrategy:
        limit: 3
        retryPolicy: OnError
        backoff: { duration: "30s", factor: 2, maxDuration: "10m" }
      container:
        image: registry.local/data/extract:2.7.3
        command: ["/extract"]
        args: ["--src={{inputs.parameters.src}}", "--date={{workflow.parameters.biz_date}}"]
```

---

## 31.5 双修对照：数据管道 DAG vs Agent 多步 DAG

| 维度 | 传统系统（数据/批处理 DAG） | Agent 系统（多 Agent / 工具 DAG） | 相同 / 差异根因 |
| --- | --- | --- | --- |
| **机制本体** | 偏序关系 + 就绪判定 + join | 同构：Agent 节点之间也是偏序 | **完全相同**，拓扑排序可直接复用 |
| **唯一键/身份** | `run_key = dag_key:biz_date`、`(run_id, task_key, shard)` | `run_id + step_id + tool_call_id` | 结构相同，粒度更细 |
| **图是谁画的** | 人写 YAML，**运行前就固定** | 人画骨架，**模型可能运行时动态扇出**（"拆成 N 个子任务"） | 差异根因：非确定性 |
| **失败模式** | 读到旧分区、join 放水、半成功留脏数据 | 上者 + 死循环、工具重复副作用、模型自称完成 | Agent 多两类 |
| **恢复方式** | 重跑任务（覆盖写） | 重跑 = **重新花钱**，必须 checkpoint + 结果缓存 | 差异根因：成本显性且按量 |
| **验证方法** | 行数对账、分区基线比对 | 产物断言 + token/调用次数对账 | 框架相同，被核对对象变了 |
| **观测指标** | 任务时长 P99、失败率、数据量偏差 | 加 token、成本、工具成功率 | 指标集扩展 |
| **成本结构** | 机器折旧，近似固定 | 每个节点都可能产生模型账单 | 差异根因：成本显性 |

**哪些部分完全相同，可以直接复用**：

1. **拓扑排序与环检测**——§31.4.5 的代码在两侧一字不改。Agent 的"子任务图"照样可能成环（模型把 A 派给 B，B 又派回 A），必须在提交执行前跑一次 `FindCycle`。
2. **`dag_task` 表结构**——把 `task_key` 换成 `step_key`、`shard_index` 换成 `sub_run_index` 就完事。`ready_at` / `lease_until` / `attempt` 三个字段的语义完全相同。
3. **join 语义的五个模式**——"多 Agent 并行检索，全部成功才算检索完成"就是 `all_success`；"多模型投票，至少一个成功就给答案"就是 `one_success`。
4. **`uk_run_key` 的幂等**——Agent 的 `run_key` 换成 `(tenant, session, idem_key)`，防的是用户重试点击。

**Agent 特有的三条**：

| 特有 | 现象 | 对策 |
| --- | --- | --- |
| **动态扇出无上限** | 模型说"拆成 200 个子任务"，账单直接翻 20 倍 | 扇出宽度必须由**代码**给上限（`max_fanout`）+ 成本守卫（第 29 章） |
| **节点输出不可信** | 子 Agent 报 "done"，实际产物是空的 | 扇入前做**产物断言**（文件存在、schema 校验、非空），不通过就 `FAILED` |
| **图会自己长** | 运行中新增节点，事后无法复现 | 每次新增节点都追加事件 + 落 `definition_version`，保证可从 checkpoint 重放 |

---

## 31.6 会在哪炸：失效模式清单

| # | 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- | --- |
| 1 | 任务全绿但数据不对 | join 用了 `all_done` 或 `one_success` | `SELECT * FROM dag_task WHERE dag_run_id=? AND state='SUCCESS'` 再对比上游是否有 FAILED | 改回 `all_success` + 加行数守卫 | 定义评审：join 语义必须显式填写，禁止默认 |
| 2 | 补数跑了一整天 | `depends_on_past` 让补数全串行 | 看 `dag_run.started_at` 与 `finished_at` 的间隔 | 补数时关掉串行依赖，或按 biz_date 并行 | 定义里区分"生产依赖"与"补数依赖" |
| 3 | 半成功任务留下脏数据 | 任务先写后校验，失败不回滚 | 对比失败任务的分区行数是否非 0 | 写临时分区 + 成功后原子 rename/promote | 所有写任务用"影子分区 + 切分"模式 |
| 4 | 任务永远 RUNNING | worker 被 kill，租约过期无人回收 | `SELECT * FROM dag_task WHERE state='RUNNING' AND lease_until < NOW() - INTERVAL 10 MINUTE` | 巡检把过期租约改回 `PENDING` 并 `attempt+1` | 租约回收任务必须常态开启（第 33 章） |
| 5 | 扇出把下游打爆 | 只设了 DAG 并行度，没设资源池 | 看下游 DB 连接数 / API 429 曲线与分片启动时间重合 | 加资源池 + 令牌桶 | 并行度三层限制写进模板 |
| 6 | 重复跑出双份数据 | 任务不是覆盖写，且 cron 抖动手动补数叠加 | `SELECT run_key, COUNT(*) FROM dag_run GROUP BY 1 HAVING COUNT(*)>1`（应为 0） | 清理 + 加 `uk_run_key` | DAG 级幂等键 |
| 7 | 改了定义，在途 run 全乱 | 定义与实例没绑定版本 | 看 `dag_run.definition_id` 是否都在同一版本 | 回滚定义 + 手工修正受影响 run | `definition_id` 落库 + 发布走灰度 |
| 8 | 一路重试到天亮 | 错误不可重试（4xx/校验失败）却配了 retries=10 | 看 `error_code` 分布 | 按错误码分级：白名单可重试 | `error_code` 必须结构化，不是一段自由文本 |

---

## 31.7 怎么验证：四类检查，都能直接跑

**① DAG 级幂等（定义即测试）**

```sql
-- 同一个 run_key 只能有一条 dag_run；任何 >0 的结果都是 bug
SELECT run_key, COUNT(*) AS n
  FROM dag_run
 WHERE created_at >= NOW() - INTERVAL 7 DAY
 GROUP BY run_key
HAVING n > 1;
```

**② join 语义体检：找出"自己成功但上游有失败"的任务（放水证据）**

```sql
SELECT t.dag_run_id, t.task_key, t.state AS task_state,
       up.task_key AS upstream_key, up.state AS upstream_state
  FROM dag_task t
  JOIN dag_task up
    ON up.dag_run_id = t.dag_run_id
   AND up.finished_at < t.started_at
 WHERE t.state = 'SUCCESS'
   AND up.state IN ('FAILED', 'UPSTREAM_FAILED')
   AND t.started_at >= NOW() - INTERVAL 1 DAY
 ORDER BY t.dag_run_id DESC
 LIMIT 200;
```

**③ 数据量守卫：拿最近 14 天的行数基线判今天是否异常**

```sql
WITH hist AS (
  SELECT biz_date,
         CAST(JSON_EXTRACT(output_meta, '$.rows') AS UNSIGNED) AS rows_cnt
    FROM dag_task
   WHERE task_key = 'merge-recon' AND state = 'SUCCESS'
     AND biz_date >= CURDATE() - INTERVAL 14 DAY
)
SELECT biz_date, rows_cnt,
       AVG(rows_cnt) OVER (ORDER BY biz_date ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING) AS avg7,
       rows_cnt / AVG(rows_cnt) OVER (ORDER BY biz_date ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING) AS ratio
  FROM hist
 ORDER BY biz_date DESC;
-- ratio < 0.7 或 > 1.3 → 告警。任务"成功"不代表数据对。
```

**④ 故障注入（每周跑一次，5 分钟）**

| 注入 | 命令/手段 | 期望 |
| --- | --- | --- |
| worker 突然死亡 | 任务跑到一半 `kubectl delete pod` | 租约 90 秒后过期 → 巡检改回 `PENDING` → attempt+1 → 最终成功，且**输出不重复** |
| 下游 DB 拒绝连接 | `iptables -A OUTPUT -p tcp --dport 3306 -j REJECT` | 任务 `RETRY_WAIT`，退避递增，不超过 `max_attempts` |
| 定义成环 | YAML 里把 `merge` 加一条边指回 `normalize` | 提交时 `FindCycle` 返回 `[normalize shard merge normalize]`，拒绝发布 |
| 扇出 8 片杀 2 片 | 手工把 2 个分片标 FAILED | `all_success` 下 merge 不执行，run 判 `FAILED`，且告警带分片清单 |

把上面四条写成脚本，**每次改 DAG 定义都跑一遍**（这才是"可执行验证"，不是"注意验证一下"）：

```bash
#!/usr/bin/env bash
# verify-dag.sh —— 租约回收 + 幂等重跑的端到端断言，约 5 分钟
set -euo pipefail
DAG=${DAG:-daily-recon}
DATE=${1:-$(date -v-1d +%F 2>/dev/null || date -d yesterday +%F)}
PSQL=${PSQL:-"psql -h 127.0.0.1 -U dag -d dag -At"}

fail() { echo "ASSERT FAILED: $*" >&2; exit 1; }

# 0) 清掉同 key 的残留 run。uk_run_key 保证这里最多一条
$PSQL -c "DELETE FROM dag_run WHERE run_key='${DAG}:${DATE}'" >/dev/null

# 1) 触发一次运行
RUN=$($PSQL -c "INSERT INTO dag_run(dag_key,definition_id,run_key,state,trigger_type,
                    triggered_by,biz_date,deadline_at)
     VALUES ('$DAG',(SELECT MAX(id) FROM dag_definition
                      WHERE dag_key='$DAG' AND status='ACTIVE'),
             '${DAG}:${DATE}','RUNNING','MANUAL','verify','$DATE',
             NOW(3)+INTERVAL 2 HOUR) RETURNING id")
echo "run_id=$RUN"

# 2) 等 normalize 进入 RUNNING，然后强杀它（模拟 worker 崩溃）
ST=""
for _ in $(seq 1 60); do
  ST=$($PSQL -c "SELECT state FROM dag_task
                  WHERE dag_run_id=$RUN AND task_key='normalize' LIMIT 1")
  [ "$ST" = "RUNNING" ] && break
  sleep 2
done
[ "$ST" = "RUNNING" ] || fail "normalize never started (state=$ST)"
kubectl delete pod -l dag-task=normalize --grace-period=0 --force

# 3) 断言：租约 90s + 宽限 30s + reaper 周期 30s ≈ 150s 内被回收并重试
DEADLINE=$(( $(date +%s) + 220 ))
ROW=""
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  ROW=$($PSQL -c "SELECT state||'|'||attempt FROM dag_task
                   WHERE dag_run_id=$RUN AND task_key='normalize' LIMIT 1")
  case "$ROW" in SUCCESS\|2|RUNNING\|2) break;; esac
  sleep 5
done
echo "normalize => ${ROW:-<none>}"
[ "$ROW" = "SUCCESS|2" ] || fail "reaper did not recover task: ${ROW:-<none>}"

# 4) 断言：重试后输出没有重复行（覆盖写 + 幂等键生效）
DUP=$($PSQL -c "SELECT COUNT(*) FROM (
                   SELECT order_no FROM recon_detail
                    WHERE biz_date='$DATE' GROUP BY order_no HAVING COUNT(*) > 1) x")
[ "$DUP" = "0" ] || fail "duplicated rows after retry: $DUP"

# 5) 断言：没有悬挂任务（PENDING/RUNNING 必须为 0）
INFLIGHT=$($PSQL -c "SELECT COUNT(*) FROM dag_task
                      WHERE dag_run_id=$RUN AND state IN ('PENDING','RUNNING')")
[ "$INFLIGHT" = "0" ] || fail "run finished with $INFLIGHT in-flight tasks"

echo "OK: $DAG $DATE (lease reaper + idempotent retry verified)"
```

**观测指标（至少四个）**：

| 指标 | 类型 | 用途 | 告警 |
| --- | --- | --- | --- |
| `dag_task_state_total{state}` | Counter | 状态分布，`FAILED` 突增 | 5 分钟内失败率 > 5% |
| `dag_task_duration_seconds{task_key}` | Histogram | 单任务 P99，找长尾 | 超该任务 SLO 2 倍 |
| `dag_run_ready_lag_seconds` | Gauge | 从 ready 到被领取的等待 | > 60s 说明 worker 不够或调度卡住 |
| `dag_lease_expired_total` | Counter | 租约过期次数 | > 0 持续 10 分钟 = worker 在崩 |

---

## 31.8 用什么工具

| 需求 | 首选 | 备选 | 不要用 |
| --- | --- | --- | --- |
| K8s 上的批处理/数据管道，要容器隔离 | **Argo Workflows** | Kubeflow Pipelines | Temporal（不是批处理场景） |
| Python 生态重、任务多、要丰富的 operator | **Airflow** | DolphinScheduler（国产、可视化强） | 自研（operator 生态补不完） |
| 大量定时任务 + 可视化依赖 + 补数 | **DolphinScheduler** | Airflow + 自研补数 | cron + shell 脚本 |
| 任务数 < 200、依赖简单、要极简运维 | **自研（本章 §31.4）** | XXL-JOB | Argo（一套 K8s 组件换 200 个任务不划算） |
| 任务之间要传递大对象、要人审批中断 | **Temporal**（第 32 章） | 自研 + 对象存储 | Airflow（XCom 不适合大对象） |
| Agent 的多步/多 Agent 图 | 自研 Run 状态机 + DAG 校验 | LangGraph（原型期） | 把图写进 prompt |

> ⚠️ **一条踩过的坑**：Argo 的 `parallelism` 是**整个 Workflow 的并发上限**，不是每个 template 的。想做"抽取最多 3 并发、分片最多 8 并发"，要么拆成两个 Workflow（用 `templateRef` + `WorkflowTemplate`），要么在节点里自己做信号量。**别指望一个字段管两层。**

---

## 31.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 边界 | **DAG 管「能不能并行」，状态机管「允不允许」**；两者是两层，不是二选一 |
| 回答的问题 | DAG 只答三件事：谁和谁无依赖、前置是谁、有没有环 |
| 落库 | 定义存 JSON（带版本）+ 运行态存任务表；血缘强再加邻接表 |
| DAG 幂等 | `uk_run_key = dag_key:biz_date`，重复触发直接被数据库拦 |
| 版本绑定 | `dag_run.definition_id` 必须绑定，改定义不影响在途 |
| 调度 | `state='PENDING' AND ready_at<=NOW()` + `FOR UPDATE SKIP LOCKED` |
| 退避 | 重试 = 把 `ready_at` 往后推 + `attempt+1`，不是原地 sleep |
| 租约 | `lease_until` 是"worker 死了谁收尸"的唯一答案，必须有回收巡检 |
| join | 五种语义；`all_done` 只能用于清理类；**扇入必须校验行数，不能只看状态** |
| 拓扑 | Kahn 分层（告诉你并行度）+ DFS 三色（告诉你哪几个节点成环） |
| 并行度 | 三层限制：DAG 全局 / 资源池 / 分片数；少一层就炸一边 |
| 双修 | 传统与 Agent 的 DAG 结构完全相同；Agent 多动态扇出、产物不可信、重跑要花钱 |

## 31.10 自检题

1. **一个 8 分片的对账 DAG，`merge` 配了 `one_success`。这个配置在什么情况下是对的？如果不对，会以什么形式暴露？**
   > 要点：只有在"多源冗余、任一源可用即可出结果"时才对（如主备两个数据源）。分片核对场景下错得离谱：7 片失败、1 片成功，`merge` 照常出报表，数据缺 87.5%，而看板全绿。暴露方式是**行数/金额对账**，不是任务状态——所以扇入必须有 `checkRowCount` 这类数据量守卫，否则这个 bug 会活到财务发现。

2. **为什么 DAG 的任务重试要把 `ready_at` 往后推，而不是在 worker 里 `sleep` 后重试？**
   > 要点：`sleep` 占着 worker slot（并行度被无效占用），进程一重启重试意图就丢了，且无法被外部观测"它还要等多久"。把 `ready_at` 往后推，重试意图就变成了数据库里的一行，任何 worker 都能接手，重启无损，还能被"就绪延迟"指标观测到。这和 Temporal 里"定时器要持久化"是同一个道理。

3. **拓扑排序已经能检测环了，为什么还要写 `FindCycle`？**
   > 要点：Kahn 只能告诉你"出队数少于节点数"，即"有环"；用户拿到的是一份 200 节点的 YAML，还是要自己找。三色 DFS 能返回一条真实路径（`[normalize shard merge normalize]`），直接把问题指到具体三个节点。**报错质量决定排障时间**——这是"引擎好不好用"和"引擎能不能上生产"的分界线。

## 31.11 延伸阅读

- 上一章：[30. 状态机建模与落库](30-state-machine-modeling.md)
- 下一章：[32. 编排引擎选型](32-workflow-engine-selection.md)
- 相关：[29. 编排是什么](29-orchestration-vs-choreography.md)（四步法与终态负责人）、[33. 卡单、重跑与人工干预](33-stuck-order-and-rerun.md)（租约回收与卡单巡检）、[34. Agent 场景的状态流转](34-agent-state-flow.md)（Run/Step 状态机）
- 幂等与补偿：[2. 幂等的实现光谱](2-idempotency-implementations.md)、[13. Saga 三连坑](13-saga-pitfalls.md)、[14. 对账体系](14-reconciliation-system.md)
- 平台侧：[../llm/12-long-task-durable.md](../llm/12-long-task-durable.md)（检查点与持久化执行）
- 官方文档（以版本为准）：Argo Workflows 的 `dag.tasks.dependencies` / `parallelism` / `retryStrategy` / `continueOn`；Airflow 的 `trigger_rule`（`all_success` / `all_done` / `one_success` / `none_failed_min_one_success`）；MySQL 8.0 与 PostgreSQL 的 `FOR UPDATE SKIP LOCKED` 语义差异
- 总纲：[0-summary.md](0-summary.md)
