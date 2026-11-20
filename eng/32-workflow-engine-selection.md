# 32. 编排引擎选型

> **状态**：`已完成（初稿）` ｜ **所属**：第五部分 · 编排与状态机 ｜ **编号**：32 / 52
> **本章定位**：第 29 章定了「要不要编排」，第 31 章给了「不买引擎怎么写」，本章回答「买不买、买哪个、买了怎么不被它绑死」。核心不是那张打分表，而是**Temporal 的确定性重放到底限制了什么**——这是唯一一类"上线三个月后才炸"的选型风险。
> **读法**：§32.4.3 的 history/replay 机制必须读透，它是本章的技术底座；§32.4.5 的六条否定判据建议直接贴进设计评审模板。

---

## 32.1 事故现场：一行 `time.Now()` 卡住 3,100 张理赔单

### 事故一：确定性重放被打破

2025 年 4 月 22 日，某保险公司的理赔审核平台跑在 Temporal 上，在途理赔单 **3,100 张**，平均每张要等 2~5 天（等医院材料、等人工复核）。

**09:40**，一位工程师提了个很小的改动：审核流在 18:00 之后不再自动通过，顺延到次日。他写的是：

```go
if time.Now().Hour() >= 18 {
    workflow.Sleep(ctx, 14*time.Hour)
}
```

本地跑通了，单测过了，CI 绿了——因为**单元测试只验证"新 workflow 的一次执行"，不验证"老 workflow 的重放"**。

| 时刻 | 事件 |
| --- | --- |
| 10:02 | 灰度发布 2 个 worker（workflow 代码随 worker 部署，这是 Temporal 的模型） |
| 10:04:07 | 一张 4 月 19 日创建、刚收到"医院材料已上传"信号的理赔单被唤醒，worker 开始 replay 它的 history |
| 10:04:07 | `NonDeterminismError: Nondeterminism error: ActivityId: 12, expected command type ScheduleActivityTask, actual command type StartTimer` |
| 10:05 | 所有被唤醒的老实例开始成批失败；workflow task 重试 5 次后进入 DLQ |
| 10:12 | 告警 `temporal_workflow_task_failed_total` 突增 **3,100**，值班才醒 |
| 10:30 | 定位到 `time.Now()`——重放时是 10:04（上午），当时走的是另一条分支 |
| 11:05 | 决定回滚 worker 镜像 |
| 11:40 | 回滚完成。**但已经被判定"重试耗尽"的实例不会自动复活** |
| 13:20 | 开始用 `temporal workflow reset` 逐个重置到上一个 workflow task |
| 16:20 | 2,847 个实例重置完成，剩余 253 个 history 已损坏，只能人工补单 |
| 17:30 | 全部恢复。**累计延迟 7 小时 26 分，214 单超过监管 24 小时时限，赔付与罚金 18 万元** |

复盘时最扎人的一句是：

> **我们以为在写业务代码，其实在写一个必须永远能重放的纯函数。** 引擎把"进程可以死"这件事解决了，代价是"你的代码不能有任何不确定性"。

### 事故二：三步流程上了 BPMN，业务最后用 Excel 绕过去了

同年 3 月，另一家 SaaS 公司的"发票申请"流程：**提交 → 审批 → 开票**，三步，一个团队，一年改 0~1 次。

他们上了 Camunda 7 + BPMN，画了 **47 个节点**（各种排他网关、定时边界事件、消息事件、子流程）。上线后：

| 时刻 | 事件 |
| --- | --- |
| 3 月 10 日 | 上线，开发 2 人月 |
| 5 月 8 日 | 业务要加"金额 > 10 万需财务总监审批"，评估结论：**4 天**（改 XML + 部署 + 回归 + 发版） |
| 5 月 12 日 | 业务等不了，先在群里口头审批 |
| 6 月 2 日 | 业务自己拉了个 Excel 台账走流程，系统只做最后开票 |
| 8 月 20 日 | 内审抽查：**1,860 张发票**有系统记录但无审批轨迹，或反之。无法追溯审批人 |

成本：开发 2 人月 + 每月约 1.5 人运维（引擎升级、BPMN 模型版本、表单适配）；收益 ≈ 0，因为**流程太短，引擎的所有能力（长事务、重放、可视化）都用不上，只留下了"改一次要 4 天"的耦合**。

两个事故是同一枚硬币的两面：

```
事故一：该用引擎，但不懂它的约束  → 引擎反噬
事故二：不该用引擎，却用了最重的那种 → 引擎变成负债
```

---

## 32.2 到底是什么：引擎只卖三样东西

### 定义

> **编排引擎 = 把「流程状态 + 重试 + 定时器」从你的进程内存里搬到持久层，并保证「进程随便死，流程能继续」的一层基础设施。**

它只提供三个能力，**一个都不多**：

| 能力 | 具体是什么 | 没有它你会怎样 |
| --- | --- | --- |
| **持久化执行** | 每一步的输入输出、定时器、待处理信号都写进 history；进程重启后按 history 重放恢复 | 进程一重启，跑了一半的流程只能从头来或人工接手 |
| **可靠重试** | activity 级重试策略（退避、上限、不可重试错误类型）+ 任务队列 + 幂等投递 | 你得自己写重试队列、退避、死信、补偿，写到第 5 个流程时开始复制粘贴 |
| **可视化** | 每个实例的 history 就是一条完整时间线，能回答"卡在哪一步、为什么" | 排障靠 grep 五个服务的日志（第 29 章的事故） |

### 它不卖的（这是选型时最常犯的错）

```text
反例 1：以为引擎解决"业务幂等"
  Temporal 保证 activity 至少执行一次，不保证执行一次。
  你的 ChargeActivity 没做幂等键，重试 3 次就是扣 3 次钱。
  → 幂等永远是业务的责任（第 2 章）。

反例 2：以为引擎解决"补偿正确性"
  Saga 模式给你"补偿会被调用"，不给"补偿一定能成功"。
  补偿失败、空回滚、悬挂，一个都不会少（第 13 章）。

反例 3：以为引擎解决"代码烂"
  把 3,000 行 if-else 搬进 workflow，只是把烂代码放到了一个更难调试的地方
  （workflow 不能打断点、不能随便加日志时间戳）。

反例 4：以为引擎是"更高级的 MQ"
  MQ 的消费是"消息驱动、无状态"；引擎的 workflow 是"有状态、可重放"。
  用引擎做纯扇出（发 100 万条通知），你会被 history 写放大打死。
```

### 一句话判据

> **你需要引擎，当且仅当：流程里存在「必须等」或「必须记住」的东西，且等待时间超过一次请求的生命周期。**

「必须等」= 等人审批、等外部回调、等定时器到点。
「必须记住」= 已经执行到第几步、某一步的返回值是什么、预算花了多少。

如果两个都没有，你不需要引擎，你需要一个函数。

---

## 32.3 结构图：Temporal 的重放机制与选型决策树

### 图一：history 是账本，workflow 代码是解释器

```text
┌──────────────────────────────┐        ┌─────────────────────────────────────┐
│  Worker 进程（无状态，可随时死）│        │  Temporal Server 持久层              │
│                              │        │  （Cassandra / PostgreSQL / MySQL）  │
│  workflow 代码                │        │                                     │
│  func Review(ctx, id) {      │        │  history（append-only，唯一真相）     │
│    a := Act(ctx, Fetch, id)  │◀──────▶│  ┌──────────────────────────────┐   │
│    Sleep(ctx, 2h)            │ replay │  │1 WorkflowExecutionStarted    │   │
│    b := Act(ctx, Settle, id) │        │  │2 WorkflowTaskScheduled       │   │
│  }                           │        │  │3 WorkflowTaskStarted         │   │
│                              │        │  │4 WorkflowTaskCompleted       │   │
│  每执行一次 workflow task：   │        │  │5 ActivityTaskScheduled(Fetch)│   │
│  ① 从 history[0] 开始重放     │        │  │6 ActivityTaskStarted         │   │
│  ② 代码产生 command 序列      │        │  │7 ActivityTaskCompleted       │   │
│  ③ 与 history 里的 command    │        │  │8 TimerStarted(2h)            │   │
│     逐条比对                  │        │  │9 TimerFired                  │   │
│  ④ 不一致 → NonDeterminism    │        │  │10 ActivityTaskScheduled      │   │
│  ⑤ 一致 → 追加新事件，向前走   │        │  └──────────────────────────────┘   │
└──────────────────────────────┘        └─────────────────────────────────────┘
        │                                              ▲
        │ 只有 command 落成 event 之后才算"发生"          │
        └──────────────────────────────────────────────┘
```

**这张图要读出的三件事**：

1. **workflow 代码不是"被执行一次"，而是"被反复重放"**。第 100 次重放时，前 99 步的 activity **不会真的再调一次**——代码执行到 `Act(ctx, Fetch, id)` 时，SDK 直接从 history 里取出 `ActivityTaskCompleted` 的结果返回。这就是"重放"的本质：**用历史结果短路掉副作用，只让代码重新走一遍逻辑**。
2. **副作用只发生在 activity 里**。workflow 里的代码只产生 command（调度 activity、启定时器、发信号），command 落库成 event 才生效。所以 workflow 不能直接发 HTTP——它没有"事务边界"保护。
3. **determinism 是逐条比对出来的**。不是"看起来一样"，是 command 序列与 history 中记录的 command 一对一匹配。多一个、少一个、顺序变了，都是 `NonDeterminismError`。

### 图二：引擎选型决策树

```text
流程里有"必须等"或"必须记住"的东西吗？
├── 没有 → 不要引擎。一个事务方法 + 补偿代码（第 29 章四步法第 4 步）
└── 有 ──▶ 流程是"批处理/数据管道"（有明确的输入输出、按批次跑）？
          ├── 是 → Argo Workflows / Airflow / DolphinScheduler（第 31 章）
          └── 否 ──▶ 流程是"业务单据的审批/长事务"（分钟到天，要人参与）？
                    ├── 是 ──▶ 业务方要自己改流程（BPMN 可编辑）？
                    │         ├── 是 → Flowable / Camunda（接受 BPMN 的耦合）
                    │         └── 否 → 自研状态机（<10 步）或 Temporal（要重放/定时器/版本化）
                    └── 否 ──▶ 高并发短流程（>500 TPS、每流程 <100ms）
                              → 不要引擎。引擎的每次状态转移都要写库，吞吐天花板在这里
```

---

## 32.4 怎么实现

### 32.4.1 五个候选的真实能力边界

| 引擎 | 执行模型 | 状态存储 | 确定性要求 | 流程时长上限 | 可视化 | 运维负担 |
| --- | --- | --- | --- | --- | --- | --- |
| **Temporal / Cadence** | 代码即流程（workflow + activity） | history（Cassandra/PG/MySQL）+ 可见性存储（ES/PG） | **强**：workflow 代码必须确定性 | 理论无限（靠 continue-as-new） | 中（Web UI + CLI） | 高（有状态集群 + 多存储） |
| **Argo Workflows** | K8s CRD，每个节点一个 Pod | etcd（Workflow CRD）+ 对象存储（产物） | 无（每个 Pod 天然独立） | 小时~天 | 强（DAG 图 + 日志） | 中（要一套 K8s 与控制面） |
| **Airflow** | Python DAG，调度器 + executor | 元数据库（PG/MySQL） | 无（但 task 要幂等） | 分钟~小时（不适合长等待） | 强（Graph/Tree/Gantt） | 中（scheduler 单点、executor 队列） |
| **Flowable / Camunda** | BPMN 2.0 流程定义 + 引擎表（`ACT_RU_*`） | 关系库（几十张 `ACT_*` 表） | 无 | 天~月（人任务天然长） | 强（BPMN 图 + 高亮当前节点） | 中高（模型版本、表单、引擎升级） |
| **自研状态机** | 你自己写 driver（第 30 章） | 你的业务库（`status + version + log`） | 无（但要自己保证重放一致） | 任意（靠巡检推进） | 弱（要自己画） | 低（就是你的库） |
| **LangGraph（原型）** | 图执行器 + checkpointer | checkpointer（内存/SQLite/PG） | 无 | 分钟级（无跨天定时器） | 弱（可接 LangSmith） | 低（但生产能力弱） |

> ⚠️ **这张表最容易被误读的一列是"确定性要求"**。Argo/Airflow/Flowable 之所以"无"，不是因为它们更先进，而是因为**它们的节点是独立进程/独立事务，本来就不重放**——代价是它们没法在一个节点内部"记住 100 行代码执行到第 37 行"。**Temporal 的确定性要求，是它换取"代码级持久化"的对价。**

### 32.4.2 五维打分矩阵：权重比分数重要

先定权重，再打分。**权重定错，打分就是给偏见找数学外衣。**

| 维度 | 权重（默认） | 为什么是这个权重 | 打分依据（1~5，5 最好） |
| --- | --- | --- | --- |
| **学习成本** | 0.20 | 决定"多久能独立改流程"，不是"多久能跑 demo" | 5=一天能改；3=一周；1=要专职平台工程师 |
| **运维成本** | 0.25 | 引擎是**新增的有状态系统**，它的故障半径 = 全部流程 | 5=复用现有库；3=加一套中间件；1=加集群+多存储 |
| **可观测性** | 0.20 | 出事时能不能 5 分钟回答"卡在哪一步" | 5=开箱时间线；3=要自己接 trace；1=只有日志 |
| **与业务代码耦合** | 0.15 | 流程逻辑写在哪：代码里 / XML 里 / 图里 | 5=普通代码可单测；1=改流程要改 XML 再部署 |
| **长事务能力** | 0.20 | 跨天等待、定时器、版本演进、信号 | 5=原生；3=要自己补；1=不支持 |

六个候选的打分（**分数必须写理由，否则三个月后没人知道为什么是 3 分**）：

| 候选 | 学习 | 运维 | 可观测 | 耦合 | 长事务 | 加权总分 |
| --- | --- | --- | --- | --- | --- | --- |
| Temporal | 2 | 2 | 4 | 3 | **5** | **3.15** |
| Argo Workflows | 3 | 3 | 4 | 4 | 3 | **3.35** |
| Airflow | 4 | 3 | 3 | 5 | 2 | **3.30** |
| Flowable / Camunda | 2 | 3 | 4 | 1 | 3 | **2.70** |
| 自研状态机 | **5** | **5** | 3 | **5** | 2 | **4.00** |
| LangGraph（原型） | 5 | 4 | 2 | 4 | 1 | **3.20** |

算一行给你看（Temporal）：`2×0.20 + 2×0.25 + 4×0.20 + 3×0.15 + 5×0.20 = 0.40+0.50+0.80+0.45+1.00 = 3.15`。

**但这张表会骗人，因为它对权重极度敏感**。同一批分数，换成三种权重，冠军完全不同：

| 场景（权重） | Temporal | Argo | Airflow | Flowable | 自研 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| **默认**（20/25/20/15/20） | 3.15 | 3.35 | 3.30 | 2.70 | **4.00** | 自研赢 → 先自研，别急着买 |
| **长事务优先**（10/15/15/10/**50**） | **3.90** | 3.25 | 2.80 | 2.85 | 3.20 | Temporal 反超 → 有跨天等待就买 |
| **运维优先**（10/**40**/20/15/15） | 3.00 | 3.35 | 3.25 | 2.80 | **4.15** | 自研更大优势 → 没有平台团队就别买 |

> 💡 **正确的用法**：不要问"哪个引擎最好"，要问**"我们的权重是多少，以及这个权重在未来 18 个月会不会变"**。一个 6 人团队、流程 8 步、要等审批 3 天——权重接近"长事务优先"，Temporal 是对的；一个 200 人平台、流程 5 步、纯同步——权重接近"运维优先"，自研 + 状态机表才是对的，上 Temporal 是给自己找第二个要值班的系统。

**把打分落库，别写在 Confluence 里**（三个月后一定会有人问"当初为什么不用 X"）：

```sql
CREATE TABLE engine_selection_score (
  id                BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  decision_key      VARCHAR(64)  NOT NULL COMMENT '场景标识，如 refund-orchestration',
  scenario          VARCHAR(256) NOT NULL COMMENT '一句话描述流程特征',
  candidate         VARCHAR(64)  NOT NULL,
  dim_learning      TINYINT      NOT NULL CHECK (dim_learning      BETWEEN 1 AND 5),
  dim_ops           TINYINT      NOT NULL CHECK (dim_ops           BETWEEN 1 AND 5),
  dim_observability TINYINT      NOT NULL CHECK (dim_observability BETWEEN 1 AND 5),
  dim_coupling      TINYINT      NOT NULL CHECK (dim_coupling      BETWEEN 1 AND 5),
  dim_longtx        TINYINT      NOT NULL CHECK (dim_longtx        BETWEEN 1 AND 5),
  w_learning        DECIMAL(3,2) NOT NULL DEFAULT 0.20,
  w_ops             DECIMAL(3,2) NOT NULL DEFAULT 0.25,
  w_observability   DECIMAL(3,2) NOT NULL DEFAULT 0.20,
  w_coupling        DECIMAL(3,2) NOT NULL DEFAULT 0.15,
  w_longtx          DECIMAL(3,2) NOT NULL DEFAULT 0.20,
  rationale         VARCHAR(1024) NOT NULL COMMENT '每条打分的理由，禁止留空',
  decided_by        VARCHAR(64)  NOT NULL,
  review_at         DATE         NOT NULL COMMENT '18 个月后复评：权重会变',
  created_at        DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_decision_candidate (decision_key, candidate),
  CONSTRAINT chk_weight_sum CHECK (
    ABS(w_learning + w_ops + w_observability + w_coupling + w_longtx - 1.00) < 0.001)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 加权计算不靠手算：一个窗口函数排名，改权重立刻能重算
SELECT decision_key, candidate,
       ROUND(dim_learning*w_learning + dim_ops*w_ops
           + dim_observability*w_observability + dim_coupling*w_coupling
           + dim_longtx*w_longtx, 2) AS score,
       RANK() OVER (
         PARTITION BY decision_key
         ORDER BY dim_learning*w_learning + dim_ops*w_ops
                + dim_observability*w_observability + dim_coupling*w_coupling
                + dim_longtx*w_longtx DESC) AS rk
  FROM engine_selection_score
 WHERE decision_key = 'refund-orchestration'
 ORDER BY rk;
```

选型结论本身也写成机器可读的 YAML，进仓库、走 review、18 个月后自动提醒复评（**这一步不做，选型结论半年后就变成"当时有人说 Temporal 好"**）：

```yaml
# docs/adr/0007-refund-orchestration.yaml
decision_key: refund-orchestration
decided_at: 2026-02-14
decided_by: [arch-review-board]
review_at: 2027-08-14          # 18 个月后复评：权重与场景都会变
scenario:
  description: 退款编排：等风控、等账务、等人工复核
  steps: 7
  max_wait: 72h                # 最长等待，决定"长事务"维度的权重
  in_flight_p99: 12000         # 在途实例峰值，决定"吞吐/运维"维度
  tps_peak: 40
  change_frequency: 6/y        # 每年变更次数，决定"耦合"维度
weights: {learning: 0.20, ops: 0.25, observability: 0.20, coupling: 0.15, longtx: 0.20}
scores:
  temporal:  {learning: 2, ops: 2, observability: 4, coupling: 3, longtx: 5}   # 3.15
  argo:      {learning: 3, ops: 3, observability: 4, coupling: 4, longtx: 3}   # 3.35
  airflow:   {learning: 4, ops: 3, observability: 3, coupling: 5, longtx: 2}   # 3.30
  flowable:  {learning: 2, ops: 3, observability: 4, coupling: 1, longtx: 3}   # 2.70
  in_house:  {learning: 5, ops: 5, observability: 3, coupling: 5, longtx: 2}   # 4.00
decision: temporal
decision_reason: >-
  在途 1.2 万实例、最长等待 72h、每年 6 次流程变更、已有 4 人平台组。
  max_wait > 1h 且 in_flight > 1000 同时成立时，自研要补定时器/租约/重放/版本四件事，
  成本高于运维一个 Temporal 集群。若 12 个月内 in_flight 降到 1000 以下且变更降到 1 次/年，
  重新评估 in_house（见 sensitivity.longtx_050）。
sensitivity:
  longtx_050: {temporal: 3.90, argo: 3.25, airflow: 2.80, flowable: 2.85, in_house: 3.20}
  ops_040:    {temporal: 3.00, argo: 3.35, airflow: 3.25, flowable: 2.80, in_house: 4.15}
exit_plan:
  invariant: 业务状态双写到 refund_order.biz_status，run_id 关联
  fallback_driver: 自研状态机 + 巡检（第 30/33 章）
  last_verified_at: 2026-02-10      # 每季度真的导 100 个实例重建一次
```

### 32.4.3 Temporal 的 history 与确定性重放：限制清单

**先看限制，再决定用不用。** 下面这些不是"建议"，是**会让在途实例全部失败**的硬约束。

| # | 不能进 workflow 的东西 | 为什么 | 正确写法 |
| --- | --- | --- | --- |
| 1 | `time.Now()` / `System.currentTimeMillis()` | 重放时值不同 → 分支走错 | `workflow.Now(ctx)` / `Workflow.currentTimeMillis()` |
| 2 | `rand.*` / `math/rand` | 同上，且不可复现 | `workflow.SideEffect` / `Workflow.randomUUID()` |
| 3 | HTTP / DB / 任何网络 IO | 重放会重复调用；且阻塞 workflow task | 放进 **activity** |
| 4 | 全局变量、包级变量、单例可变状态 | 重放时值与首次执行不同 | 全部状态走参数/返回值/`SideEffect` |
| 5 | 文件系统、环境变量、本地缓存 | 换一台 worker 就不一样 | 作为 activity 入参或 workflow 入参传进来 |
| 6 | 日志里打时间戳/随机 ID | 本身不影响逻辑，但会污染"重放一致性"排查 | 用 `workflow.GetLogger(ctx)` |
| 7 | `go func()` 里的裸并发 | 调度顺序不确定 → command 顺序不确定 | `workflow.Go` + `workflow.Await`（确定性调度器） |
| 8 | 非确定性遍历（map 迭代序） | Go map 迭代序随机 → command 顺序变 | 先排序再遍历 |

**Go：错误写法长什么样**

```go
// ❌ 这段代码能跑通 demo，但一发布就会让所有在途实例 NonDeterminismError
func ReviewWorkflowBad(ctx workflow.Context, claimID string) error {
    // 1. 墙钟：重放时是另一个时间，分支直接走错
    if time.Now().Hour() >= 18 {
        _ = workflow.Sleep(ctx, 14*time.Hour)   // command 序列与 history 不一致
    }
    // 2. 随机数：重放时抽到另一个值，下游 activity 参数变了
    if rand.Intn(10) < 3 {
        _ = workflow.ExecuteActivity(ctx, acts.Escalate, claimID).Get(ctx, nil)
    }
    // 3. 直接网络调用：重放会重复打第三方；且这行会阻塞整个 workflow task
    resp, err := http.Get("https://crm.internal/claims/" + claimID)
    if err != nil {
        return err
    }
    defer resp.Body.Close()
    // 4. 包级变量累计：第一次执行 +1，重放再 +1
    globalProcessed++
    return nil
}
```

**Go：正确写法**

```go
func ReviewWorkflow(ctx workflow.Context, claimID string) (string, error) {
    log := workflow.GetLogger(ctx)

    // ① 时间：用确定性时间；确需"真实随机"时用 SideEffect，结果会写进 history
    now := workflow.Now(ctx)
    var jitter int
    if err := workflow.SideEffect(ctx, func(ctx workflow.Context) interface{} {
        return rand.Intn(1000)
    }).Get(&jitter); err != nil {
        return "", err
    }

    ao := workflow.ActivityOptions{
        StartToCloseTimeout: 30 * time.Second,
        ScheduleToCloseTimeout: 10 * time.Minute,     // 含重试的总预算，必须设
        HeartbeatTimeout:    10 * time.Second,        // 长任务必须设，否则"卡死"发现不了
        RetryPolicy: &temporal.RetryPolicy{
            InitialInterval:        time.Second,
            BackoffCoefficient:     2.0,
            MaximumInterval:        time.Minute,
            MaximumAttempts:        5,
            NonRetryableErrorTypes: []string{"INVALID_ARGUMENT", "CLAIM_NOT_FOUND"},
        },
    }
    ctx = workflow.WithActivityOptions(ctx, ao)

    // ② 一切外部 IO 都在 activity 里
    var claim Claim
    if err := workflow.ExecuteActivity(ctx, acts.FetchClaim, claimID).Get(ctx, &claim); err != nil {
        return "", err
    }

    // ③ 版本分叉：老实例走老逻辑，新实例走新逻辑，这是重放安全的前提
    v := workflow.GetVersion(ctx, "add-senior-approval", workflow.DefaultVersion, 1)
    if v >= 1 && claim.Amount > 500_000 {
        // ④ 等人：用信号 + 定时器，不用 sleep 轮询
        if err := workflow.ExecuteActivity(ctx, acts.RequestApproval, claimID, "senior").Get(ctx, nil); err != nil {
            return "", err
        }
        var approved bool
        workflow.Go(ctx, func(gctx workflow.Context) {
            workflow.GetSignalChannel(gctx, "approval").Receive(gctx, &approved)
        })
        if err := workflow.AwaitWithTimeout(ctx, 72*time.Hour, func() bool { return approved }); err != nil {
            return "", fmt.Errorf("approval timeout: %w", err)   // 超时是业务失败，不是重试
        }
    }

    // ⑤ 顺延：用持久化定时器，进程死 100 次也不影响
    if now.Hour() >= 18 {
        if err := workflow.Sleep(ctx, 14*time.Hour); err != nil {
            return "", err
        }
    }
    _ = jitter // 真实业务里会作为幂等键的一部分传下去

    var result string
    err := workflow.ExecuteActivity(ctx, acts.Settle, claimID).Get(ctx, &result)
    log.Info("review done", "claim", claimID, "version", v)
    return result, err
}
```

**Java：同样的约束，`getVersion` 的用法**

```java
public class ReviewWorkflowImpl implements ReviewWorkflow {

    private final ReviewActivities acts = Workflow.newActivityStub(
        ReviewActivities.class,
        ActivityOptions.newBuilder()
            .setStartToCloseTimeout(Duration.ofSeconds(30))
            .setScheduleToCloseTimeout(Duration.ofMinutes(10))
            .setHeartbeatTimeout(Duration.ofSeconds(10))
            .setRetryOptions(RetryOptions.newBuilder()
                .setInitialInterval(Duration.ofSeconds(1))
                .setBackoffCoefficient(2.0)
                .setMaximumInterval(Duration.ofMinutes(1))
                .setMaximumAttempts(5)
                .setDoNotRetry("INVALID_ARGUMENT", "CLAIM_NOT_FOUND")   // 错误类型名，不是异常类
                .build())
            .build());

    private boolean approved = false;

    @Override
    public void approve() { this.approved = true; }        // @SignalMethod

    @Override
    public String review(String claimId) {
        // 版本化：默认版本 → 1。老实例重放时 v = -1，走老分支
        int v = Workflow.getVersion("add-senior-approval", Workflow.DEFAULT_VERSION, 1);

        Claim claim = acts.fetchClaim(claimId);             // activity，可重试
        if (v >= 1 && claim.amount() > 500_000L) {
            acts.requestApproval(claimId, "senior");
            // 阻塞在 workflow 层：不占线程，进程可重启，72h 定时器持久化
            boolean ok = Workflow.await(Duration.ofHours(72), () -> approved);
            if (!ok) {
                throw ApplicationFailure.newNonRetryableFailure(
                    "approval timeout", "APPROVAL_TIMEOUT");
            }
        }
        Workflow.sleep(Duration.ofHours(2));                // 确定性定时器，不是 Thread.sleep
        return acts.settle(claimId);
    }
}
```

**Java：activity 侧才是"脏活区"——它可以随便 IO，但必须自己幂等**

```java
public class ReviewActivitiesImpl implements ReviewActivities {

    private final JdbcTemplate jdbc;

    @Override
    public Claim fetchClaim(String claimId) {
        // activity 不在重放路径上：这里可以读库、调 HTTP、读配置，重放时不会重复执行
        return jdbc.queryForObject("""
            SELECT id, amount, status, applicant FROM claim WHERE id = ?
            """, (rs, i) -> new Claim(rs.getString(1), rs.getLong(2),
                                      rs.getString(3), rs.getString(4)), claimId);
    }

    @Override
    public String settle(String claimId) {
        Activity.getExecutionContext().heartbeat("settle-start:" + claimId);  // 长任务心跳
        try {
            // 幂等闸门：activity 至少执行一次，重复执行必须被唯一键拦住
            jdbc.update("""
                INSERT INTO ledger_entry (biz_no, entry_type, amount, created_at)
                VALUES (?, 'CLAIM_SETTLE', (SELECT amount FROM claim WHERE id = ?), NOW(3))
                """, claimId, claimId);
        } catch (DuplicateKeyException dup) {
            // 已经结算过：不报错，读出已有结果返回 —— 这就是 activity 的幂等语义
            log.info("settle already done, reuse result claim={}", claimId);
        }
        Activity.getExecutionContext().heartbeat("settle-done:" + claimId);
        return "SETTLED:" + claimId;
    }
}
```

> 💡 **一句话分工**：**workflow 是"只读的编排脚本"，activity 是"可重试的脏活"**。判断标准：这段代码重放 100 次会不会有副作用？会 → 放 activity；不会且必须参与分支 → 放 workflow。**放错地方的代价不是性能，而是"发布即事故"。**

> 🩸 **`Workflow.getVersion` 的三条铁律**：
> ① 一旦上线，这段 `getVersion` **永远不能删**（老 history 重放还要靠它）。
> ② `maxSupportedVersion` 只能增不能减。
> ③ 变更如果"改变了已有 command 的顺序"，光加 version 不够——必须把老逻辑整段保留在 `if (v < N)` 分支里，**不能就地改**。

### 32.4.4 版本变更的三种类型（决定你要不要 patch）

| 变更类型 | 例子 | 需要 `getVersion` 吗 | 为什么 |
| --- | --- | --- | --- |
| **纯新增分支** | 金额 > 50 万时多调一个 activity | 需要 | 老实例重放时不能多出 command |
| **改参数、不改 command 结构** | activity 超时从 30s 改成 60s | 不需要 | 超时不进 command 比对（但会影响在途实例的重试预算，谨慎） |
| **删除/重排已有 command** | 把 `A(); B()` 改成 `B(); A()` | 需要，且必须保留老分支 | command 顺序必须逐条匹配 |
| **改 activity 的入参语义** | 金额单位从元改成分 | **必须新建 workflow type** | 老 history 里的返回值语义变了，patch 救不了 |
| **改 workflow 的返回值结构** | 从 string 改成 struct | 需要（用 converter 兼容） | 反序列化老结果会失败 |
| **换掉整个流程** | 从"三级审批"改成"两级" | **新建 workflow type + 双跑** | 用 patch 做业务重构，代码会变成一团 `if (v==1) ... else if (v==2)` |

### 32.4.5 什么时候不要用引擎：六条否定判据

| # | 判据 | 数字阈值 | 为什么 | 替代方案 |
| --- | --- | --- | --- | --- |
| 1 | **流程步数太少** | ≤ 3 步，且在一个事务里 | 引擎的收益来自"跨进程/跨天"，3 步同步流程全是成本 | 一个 `@Transactional` 方法 + 补偿代码 |
| 2 | **吞吐太高** | 单实例 > 200 TPS 的短流程 | 每次状态转移都要写 history（至少 2~3 次写），写放大直接顶到存储上限 | 状态机表 + 条件更新（第 30 章） |
| 3 | **没有平台运维** | 团队 < 5 人且无 SRE | 引擎是新增有状态系统：DB、可见性存储、worker 池、升级窗口 | 自研状态机（就一张表） |
| 4 | **流程一年不变** | 12 个月 0~1 次变更 | 买了"可编排"却不用，"可编排"只剩耦合成本（事故二） | 硬编码 + 状态机 |
| 5 | **无法承担爆炸半径** | 引擎故障 = 全公司流程停 | 所有流程共用一个控制面，它挂了没有降级路径 | 按域拆小，或自研隔离 |
| 6 | **没人能改流程** | 上线 90 天内，业务改动仍要原开发者 | 说明你只是把黑盒从代码挪到了引擎，可视化的价值没兑现 | 先补文档与演练，再谈引擎 |

> 💡 **一条反向判据（什么时候必须要引擎）**：**流程里同时出现「等超过 1 小时」+「失败要自动重试」+「在途实例超过 1,000」**。这三条同时成立时，自研的巡检、租约、重放、版本四件事会吃掉你 2 个人月以上，买引擎更便宜。

### 32.4.6 迁移与退出成本：选型时就要算

| 成本项 | 具体是什么 | 量级（经验值） | 降低手段 |
| --- | --- | --- | --- |
| **在途实例迁移** | 老流程跑到一半，新引擎不认识 | 双跑 1~2 个发布周期 | 老实例跑完再切；新流量走新引擎 |
| **history 迁移** | Temporal 的 history 是私有格式，跨集群/跨版本迁移要官方工具 | 万级实例 = 数小时 | 尽量不迁；用 continue-as-new 收口 |
| **业务代码解耦** | 把 workflow 里的业务逻辑抽到 activity，才能换引擎 | 中大型流程 20~60 人日 | 从第一天就把业务放 activity，workflow 只写编排 |
| **可观测重建** | 引擎的 UI 不等于你的 trace/告警 | 2~4 周 | 从第一天就把 `run_id` 打进所有日志 |
| **人员能力** | 会写"确定性代码"的人不多 | 2~3 个月才能独立改 | 建 replay 回归门禁（§32.7），让机器兜住 |
| **退出（换引擎/回自研）** | 最贵的一项：history 不可导出为通用格式 | 6~12 人月 | 业务状态**双写**到自己的表，引擎只做推进器 |

> ⚠️ **"业务状态双写"是唯一可靠的退出保险**：每次 workflow 推进，都同步更新你自己的 `biz_status` 表（用 `run_id` 关联）。这样即使明天把引擎扔掉，你的业务状态还在，可以拿自研 driver 接管。代价是多一次写、要保证一致（用 activity 里写，别在 workflow 里写）。

---

## 32.5 双修对照：业务长事务 vs Agent 长任务

| 维度 | 传统系统（理赔/退款/审批） | Agent 系统（Run 执行） | 相同 / 差异根因 |
| --- | --- | --- | --- |
| **机制本体** | workflow + activity + 定时器 + 信号 | 同构：Run 状态机 + Step + 等待态 | **完全相同**，Temporal 的 workflow 模型就是 Agent Run 的天然载体 |
| **唯一键/身份** | `workflow_id`（业务键）+ `run_id`（执行 ID） | `run_id` + `step_id` + `tool_call_id` | 结构相同，粒度更细 |
| **确定性要求** | 强：workflow 代码不能有随机/时间/网络 | **更强**：模型调用本身就不确定，必须放 activity | 差异根因：非确定性 |
| **一次"步骤"的成本** | 毫秒~秒（一次 RPC） | 秒~分钟（一次模型调用），且**按 token 计费** | 差异根因：成本显性且按量 |
| **失败模式** | 重放不确定、activity 不幂等、history 膨胀 | 上者 + 模型死循环、工具重复副作用、预算超支 | Agent 多三类 |
| **恢复方式** | replay history / reset | replay + **checkpoint 短路已完成的模型调用** | 机制相同，Agent 必须缓存模型输出否则重放=重花钱 |
| **验证方法** | replay 回归 + 200 并发 | replay 回归 + 成本对账 + 产物断言 | 框架相同，多一维成本 |
| **可视化** | BPMN 图高亮当前节点 | 只能画"允许的路径"，实际路径看 trace | Agent 的图是约束，不是轨迹（第 29 章） |

**哪些部分完全相同，可以直接复用**：

1. **history/event sourcing 的落库结构**——都是 `(aggregate_id, seq, type, payload)` + 唯一键防并发（第 30 章姿势 ②）。
2. **activity 的四个超时**（`ScheduleToClose` / `ScheduleToStart` / `StartToClose` / `Heartbeat`）——Agent 的工具调用一字不改地照抄，`Heartbeat` 对"模型流式输出 90 秒"尤其重要。
3. **重试策略的五个字段**（初始间隔、退避系数、最大间隔、最大次数、不可重试错误类型）——完全通用。
4. **版本化 patch 的思路**——Agent 的 prompt 版本、工具 schema 版本、状态机版本，本质上都是"同一个 `run_id` 在不同版本下要能重放"。

**Agent 特有的三条**：

| 特有 | 现象 | 对策 |
| --- | --- | --- |
| **模型调用必须在 activity 里** | 有人图省事把 `openai.Chat()` 写进 workflow → 重放时重复计费且结果不同 | 强制 code review 规则：workflow 文件禁止 import 任何 HTTP/DB/模型 SDK |
| **history 会被大 payload 撑爆** | 把 20 页文档全文塞进 activity 返回值 | 只传引用（S3 key + hash），body 不进 history（第 12 章） |
| **等待是常态，不是异常** | 70% 的时间在 `WAITING_HUMAN` | 状态机必须有两个等待态，且等待态不占 worker（第 34 章） |

---

## 32.6 会在哪炸：失效模式清单

| # | 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- | --- |
| 1 | 发布后老实例批量 `NonDeterminismError` | workflow 里混入时间/随机/网络/全局变量 | `temporal workflow show` 看失败点在哪个 event；对比新老代码 | 回滚 worker；`temporal workflow reset` 到出错前的 workflow task | **CI 里跑 replay 回归**（§32.7），这是唯一有效的手段 |
| 2 | history 膨胀到几十 MB，任务越来越慢 | 每次循环都往 history 里塞大对象；长循环不 continue-as-new | `temporal workflow describe` 看 history 长度；UI 里看 event 数 | 大对象传引用；加 `continue-as-new` 阈值 | 单次 Run 事件数上限（如 5,000）触发 continue-as-new |
| 3 | activity 重试导致重复扣款 | activity 不幂等，且 `MaximumAttempts > 1` | 按业务键 group by 数分录 | 幂等键 + 唯一索引（第 2 章） | 所有写类 activity 强制带 `idempotency_key` 参数 |
| 4 | 流程"卡住"但没有任何错误 | 等信号，信号永远没来；或 `AwaitWithTimeout` 超时被当成可重试错误 | 查 `WAITING_*` 状态 + 最老实例年龄 | 补超时策略 + 巡检（第 33 章） | 每个等待态必须定义 SLA 与超时后的动作 |
| 5 | worker 全部空闲但队列在堆积 | task queue 名字拼错 / worker 没注册 workflow / 并发上限太低 | `temporal task-queue describe` 看 poller 数 | 修配置；调 `MaxConcurrentWorkflowTaskExecutionSize` | 部署后自动断言 poller > 0 |
| 6 | 升级引擎版本后全站不可用 | 跨大版本不兼容（DB schema、可见性存储） | 升级窗口的错误日志 | 回滚 + 按官方升级路径逐版本升 | 升级前在影子集群跑一遍全量 replay |
| 7 | 可视化界面显示"运行中"，实际早失败 | 业务状态与引擎状态双真相源 | 对比 `biz_status` 与 `workflow describe` | 以引擎为准修正业务表；或反过来双写对齐 | 只允许一个真相源 + 每日对账 |
| 8 | 单条大流程拖垮整个 worker | 一个 workflow task 里有 CPU 密集计算 | 看 worker CPU 与 workflow task 超时 | 计算挪到 activity | workflow 里禁止循环超过 1,000 次/禁止重计算 |

> 🩸 **第 1 条和第 3 条是"引擎类"事故的两大主流**。前者**只有 replay 回归能防**（代码 review 挡不住，因为代码"看起来完全正常"）；后者**只有幂等能防**（引擎明确告诉你"至少一次"）。

---

## 32.7 怎么验证：replay 回归是硬门禁

### ① Replay 回归：拿生产 history 打新代码

这是本章最重要的一段运维代码。**没有它，你迟早会经历 §32.1 的事故一。**

```bash
#!/usr/bin/env bash
# replay-check.sh —— 发布前必跑：用最近 7 天的生产 history 重放新 worker 代码
set -euo pipefail
NS=${NS:-insurance}
FROM=${FROM:-$(date -v-7d +%F 2>/dev/null || date -d '7 days ago' +%F)}
OUT=$(mktemp -d /tmp/replay.XXXXXX)

# 1) 抽样：既要覆盖"已完成的"，也要覆盖"还在跑的"（在途实例才是重放的高危人群）
temporal workflow list --namespace "$NS" \
  --query "StartTime > '${FROM}T00:00:00Z'" --limit 300 \
  --fields "WorkflowId" --output json | jq -r '.[].execution.workflowId' > "$OUT/ids.txt"

total=$(wc -l < "$OUT/ids.txt" | tr -d ' ')
echo "sampled=$total"
[ "$total" -gt 0 ] || { echo "no history sampled, refusing to pass"; exit 1; }

# 2) 逐个导出 history 并重放
fail=0
while read -r wid; do
  temporal workflow show --namespace "$NS" --workflow-id "$wid" \
    --output json > "$OUT/$wid.json" 2>/dev/null || continue
  if ! ./bin/replay -history "$OUT/$wid.json" >>"$OUT/ok.log" 2>>"$OUT/err.log"; then
    echo "REPLAY FAILED: $wid"
    fail=$((fail + 1))
  fi
done < "$OUT/ids.txt"

echo "checked=$total failed=$fail log=$OUT"
[ "$fail" -eq 0 ] || { echo "BLOCK RELEASE: 重放不通过，禁止发布"; exit 1; }
```

重放器本体（Go SDK 自带，不用自己写）：

```go
package main

import (
	"flag"
	"log"

	"go.temporal.io/sdk/worker"
	"go.uber.org/zap"
)

func main() {
	history := flag.String("history", "", "exported workflow history json")
	flag.Parse()
	if *history == "" {
		log.Fatal("-history is required")
	}
	r := worker.NewWorkflowReplayer()
	// 注册的是"新代码"。老 history 喂进来，command 序列不一致就会返回 error。
	r.RegisterWorkflow(ReviewWorkflow)
	if err := r.ReplayWorkflowHistoryFromJSONFile(zap.NewNop(), *history); err != nil {
		log.Fatalf("non-deterministic: %v", err)
	}
}
```

> 💡 **Java SDK 同理**：`io.temporal.testing.WorkflowReplayer.replayWorkflowExecution(...)`。**关键不是 API，而是"把 replay 放进 CI 且失败即阻断发布"这条纪律。**

### ② PoC 验证清单（一周内必须跑完，别听销售讲 PPT）

| # | 验证项 | 怎么验 | 通过标准 |
| --- | --- | --- | --- |
| 1 | 进程崩溃恢复 | `kubectl delete pod` 在 activity 执行中 | 重启后从断点继续，activity 不重复副作用 |
| 2 | 确定性 | 故意写 `time.Now()`，再重放老 history | replay 必须报错（报错是特性，不是 bug） |
| 3 | 定时器精度 | 设 5 分钟定时器，杀掉所有 worker 再拉起 | 到点触发，误差 < 10 秒 |
| 4 | 信号丢失 | 实例未就绪时发信号 | 信号被缓冲，就绪后消费；不丢 |
| 5 | 版本升级 | 用 `getVersion` 加一个分支，重放老实例 | 老实例走老分支，不报错 |
| 6 | 吞吐 | 目标峰值的 3 倍压测（含 history 写放大） | P99 状态转移 < 50ms，无超时 |
| 7 | 运维 | 模拟可见性存储挂掉 | 主流程仍能跑（或明确降级），UI 可恢复 |
| 8 | 退出 | 导出 100 个 history，用裸 SQL 重建业务状态 | 能重建 → 你没有被锁死 |

### ③ 观测指标

| 指标 | 含义 | 告警阈值 |
| --- | --- | --- |
| `workflow_task_failed_total{reason="nondeterminism"}` | 重放不一致 | **> 0 立即 P0**（这是发布事故） |
| `workflow_schedule_to_start_latency_p99` | 任务排队时间 | > 5s（worker 不够或队列打错） |
| `activity_execution_failed_total{type}` | 单类 activity 失败率 | 5 分钟 > 5% |
| `workflow_stuck_total{state}` | 超 SLA 未推进 | > 0（按流程分级） |
| `history_size_bytes_p99` | history 膨胀 | > 10MB 预警，> 40MB 必须 continue-as-new |

---

## 32.8 用什么工具

| 场景 | 首选 | 备选 | 明确不要用 |
| --- | --- | --- | --- |
| 理赔/退款/审批，等 1 小时~30 天，要版本化 | **Temporal** | Cadence（同源，生态略旧） | Airflow（无长等待语义） |
| K8s 批处理、数据管道、容器隔离 | **Argo Workflows** | Kubeflow Pipelines | Temporal（不是批处理） |
| Python 数据编排、算子生态 | **Airflow** | DolphinScheduler | 自研（算子补不完） |
| 业务方要自己画流程、BPMN 是刚需 | **Flowable**（开源） / **Camunda 8**（Zeebe，云原生） | Camunda 7（稳定但已进维护期） | 自研表单引擎 |
| 3~10 步、单团队、要卡单巡检 | **自研状态机表**（第 30 章） | Temporal（如果已有集群） | BPMN（太重） |
| Agent 长任务 | Temporal（activity 包模型调用） | 自研 Run 状态机 + 检查点（第 34 章） | LangGraph 直接上生产（缺定时器/版本） |

---

## 32.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 引擎只卖三样 | 持久化执行、可靠重试、可视化。**幂等和补偿它不给** |
| 判据 | 需要引擎 ⟺ 流程里有「必须等」或「必须记住」，且超过一次请求的生命周期 |
| 确定性 | workflow 里不能有：时间、随机、网络、全局变量、裸并发、map 迭代序 |
| 重放 | 代码重跑，副作用短路；command 与 history 逐条比对，不一致即 `NonDeterminismError` |
| 版本 | `getVersion` 一旦上线永不删除；改语义要新建 workflow type |
| 打分 | 五维：学习/运维/可观测/耦合/长事务；**权重比分数重要**，要做敏感性分析 |
| 不要用 | 步数 ≤3、吞吐 >200TPS、无平台运维、流程一年不变、扛不住爆炸半径、90 天没人能改 |
| 迁移 | 最贵是"退出"；唯一保险是**业务状态双写**到自己表里 |
| 验证 | replay 回归必须进 CI，失败阻断发布；这是唯一能挡住确定性事故的手段 |
| 双修 | Agent 的 Run 与业务长事务同构；模型调用必须在 activity，否则重放=重花钱 |

## 32.10 自检题

1. **Temporal 的 workflow 里为什么不能调 HTTP？说清"会发生什么"，而不是"不推荐"。**
   > 要点：两层原因。① 重放：每次 replay 都会重新执行这行代码，等于对第三方重复发起请求（可能重复下单）；HTTP 的响应也不在 history 里，重放时网络状态不同 → 分支不同 → `NonDeterminismError`。② 阻塞：workflow task 有超时（默认 10 秒级），HTTP 慢一点就会让整个 workflow task 超时并重试，把 worker 拖死。正解是放 activity，并给 activity 设 `StartToCloseTimeout` 与 `HeartbeatTimeout`。

2. **五维打分表算出"自研 4.00 > Temporal 3.15"，是不是说明永远该自研？**
   > 要点：不是。分数只对一组权重成立。把长事务权重从 0.20 提到 0.50，Temporal 变成 3.90、自研 3.20——因为有跨天等待时，自研要自己造定时器、租约、重放、版本四件事，这些正是 Temporal 的核心价值。正确做法是：先写清场景（等多久、几步、多少在途、谁运维），再定权重，再做敏感性分析，最后看排名是否稳定。**排名会翻转，说明你的决策对权重敏感，必须先把权重谈拢。**

3. **为什么"业务状态双写"是换引擎时唯一的退出保险？**
   > 要点：Temporal 的 history 是私有格式，跨引擎/跨版本迁移基本不可行（万级实例成本极高）；如果你所有状态都只活在 history 里，换引擎等于"所有在途流程作废"。双写一份 `biz_status`（用 `run_id` 关联）后，最坏情况你可以扔掉引擎，用自己的 driver 接着推进——引擎退化成"推进器"，而不是"真相源"。注意双写必须在 activity 里做（在 workflow 里写库会破坏确定性）。

## 32.11 延伸阅读

- 上一章：[31. DAG 编排与依赖管理](31-dag-orchestration.md)
- 下一章：[33. 卡单、重跑、跳过与人工干预](33-stuck-order-and-rerun.md)
- 相关：[29. 编排是什么](29-orchestration-vs-choreography.md)（四步法第四步）、[30. 状态机建模与落库](30-state-machine-modeling.md)（自研路线的底座）、[34. Agent 场景的状态流转](34-agent-state-flow.md)（Run 状态机）
- 幂等与补偿：[2. 幂等的实现光谱](2-idempotency-implementations.md)、[13. Saga 三连坑](13-saga-pitfalls.md)
- 平台侧：[../llm/12-long-task-durable.md](../llm/12-long-task-durable.md)（长任务与检查点）、[../llm/34-hitl-approval.md](../llm/34-hitl-approval.md)（审批即两阶段提交）
- 官方文档（以版本为准）：Temporal 的 `workflow.GetVersion` / `SideEffect` / `ContinueAsNew` / `WorkflowReplayer`；`RetryPolicy` 五个字段；history 大小限制（默认 50MB / 50,000 events 触发错误）；`temporal workflow reset` CLI；Argo 的 `spec.parallelism`；Flowable 的 `ACT_RU_EXECUTION` / `ACT_RU_TASK` 表结构
- 总纲：[0-summary.md](0-summary.md)
