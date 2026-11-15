# 10. 一次 Agent 请求的 12 站旅程

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 请求生命周期与队列调度 ｜ **编号**：10 / 52
> **本章定位**：前九章讲了各个零件（协议、API、端云），这一章把它们串成一条**时间轴**：一次请求从 TCP 握手到计费落库，中间经过 12 个站，每站花多少毫秒、超时了怎么办、谁负责兜底。**这是全书唯一一章「按时间顺序」讲架构的地方，建议打印出来贴在工位上。**
> **读法**：§10.3 的图和 §10.4 的预算表是核心，其余是解释。做容量规划时看 §10.6，做故障演练时看 §10.7。

---

## 10.1 引子：一次 12.4 秒的请求，坏在第 7 站

线上一条告警：客服 Agent 的 P99 从 18s 涨到 41s，成功率没掉，但用户投诉「太慢了」。

你打开 trace，看到这样一条：

```
run_7f2a  total=12420ms  status=ok
├─ gateway.ingress           18ms
├─ auth.verify                6ms
├─ idem.check                 2ms
├─ preprocess.normalize      41ms
├─ preprocess.injection      33ms
├─ preprocess.moderation     88ms   ← 串行调了三次
├─ quota.check                9ms
├─ admission.wait           120ms
├─ context.session_load      44ms
├─ context.retrieve         1840ms  ← ★ 召回 3 路 + rerank，P99 长尾
├─ context.compress          61ms
├─ router.select              8ms
├─ llm.call#1               2110ms
├─ tool.call#1               420ms
├─ llm.call#2               3520ms
├─ tool.call#2               610ms
├─ llm.call#3               4180ms
├─ audit.stream_first         52ms
├─ postprocess.validate       71ms
├─ billing.settle             33ms
└─ persist.events             (async)
```

**总耗时 12.4 秒。前 8 站加起来 400ms，占 3%；后面模型+工具占 11.9 秒，占 96%。** 而那条 P99=41s 的告警，追下去是 `context.retrieve` 的 P99 到了 9.2 秒——因为某个租户上传了 4 万条文档，检索走了全表扫描。

这里是本章最重要的一个认知：

> 🎯 **12 站旅程的价值不在于「每站都快」，而在于「知道哪一站值得优化、哪一站超时了该放弃谁」。**
> 400ms 的那 8 站，你优化到 200ms，用户感知不到；`retrieve` 省 1.5s，用户立刻感知。
> 反过来，如果那 8 站里有一站挂了，整个请求就 500——**它们不值钱，但它们是门票。**

所以本章给两样东西：一张**预算表**（每站能花多少）和一份**兜底矩阵**（超了怎么办）。这两样东西定下来，你的链路就从「一堆代码」变成了「一个可以被讨论、被压测、被演练的系统」。

---

## 10.2 断崖：不设预算会怎么炸

没有每站预算的系统，失败形态非常统一：**一个慢站把整条链拖死，而且没人知道是谁。**

| 现象 | 根因 | 量化后果（Aurora 5k QPS） |
| --- | --- | --- |
| 全链路超时，但每个下游都「正常」 | 各站超时独立设置（都是 30s），叠加后 200s | 用户等 30s 后放弃，但服务端还在跑，资源全占死 |
| 重试风暴 | 站与站之间无条件重试，指数放大 | 一次模型抖动触发 3×3×3 = 27 倍请求 |
| 降级不生效 | 降级判断在「超时之后」，而超时已经是终态 | 降级永远来不及，等于没写 |
| P99 不可控 | 只有总超时，没有分配 | 一个租户的慢检索拖垮整个分片的 P99 |
| 成本失控 | 没有「预算刹车」，跑到死为止 | 单个 Run 最高 ¥4.7（正常 ¥0.12），39 倍 |
| 无法定位 | trace 只有总耗时，没有分站 | 平均故障定位时间 4 小时 → 40 分钟（有了分站耗时后） |

**核心机制只有一句话**：**预算必须从「总 deadline」往下分配，而不是从「每站超时」往上叠加。** 前者保证总时长可控，后者保证总时长不可控（这是分布式系统的老规矩，gRPC 的 deadline propagation 就是这么设计的）。

```
❌ 每站独立 30s：
   实际最坏 = 30 × 12 = 360s，而你的网关 60s 就掐了。中间 300s 全是白烧的钱。

✅ 总 deadline 90s，按站分配：
   每站拿 min(本站预算, 剩余预算 − 后续必需预留)
   剩余不够 → 提前降级，而不是等它超时。
```

---

## 10.3 结构图：12 站与时间轴

```
 t=0                                                                      t=30s
  │                                                                          │
  ├─①接入─┬─②鉴权─┬─③幂等─┬─④前处理──┬─⑤配额─┬─⑥排队─┬─⑦上下文装配─┬─⑧路由─┐   │
  │ 18ms  │  6ms  │ 2ms   │  162ms   │  9ms  │ 120ms │   1945ms    │ 8ms  │   │
  │       │       │       │          │       │       │             │      │   │
  │  并行：规范化+注入预检+审核（三者同时跑，取最慢）              │      │   │
  │       │       │       │          │       │       │  检索3路并行+rerank  │   │
  └───────┴───────┴───────┴──────────┴───────┴───────┴─────────────┴──────┘   │
                                                                   │          │
                                        ┌──────────────────────────▼───────┐  │
                                        │ ⑨ 模型调用与步循环（Run 内核）      │  │
                                        │  ┌────────┐  ┌────────┐  ┌──────┐│  │
                                        │  │llm#1   │→ │tool#1  │→ │llm#2 ││  │
                                        │  │2.1s    │  │0.4s    │  │3.5s  ││  │
                                        │  └────────┘  └────────┘  └──┬───┘│  │
                                        │        ↺ 循环直到 done 或超步数   │  │
                                        └──────────────────────────────┼───┘  │
                                                                       │      │
        ┌──────────────────────────────────────────────────────────────▼───┐  │
        │ ⑩ 流式输出 + 边发边审（与 ⑨ 重叠，不是之后！）                      │  │
        │    每个 delta → 窗口审 → 放行 / 缓冲 / 截断（第 37 章）             │  │
        └──────────────────────────────┬───────────────────────────────────┘  │
                                       │                                      │
                       ┌───────────────▼──────────────┐   ┌──────────────────┐│
                       │ ⑪ 后处理 + 结算（同步，计入）  │   │ ⑫ 落库与回流      ││
                       │  校验/脱敏/用量/账本  104ms   │   │  (异步，不计入)   ││
                       └───────────────────────────────┘   └──────────────────┘│
                                                                                │
  ▲ TTFT（首字可见）≈ 1.0s                                    ▲ 总时长 30s ────┘
```

三个必须记住的结构特征：

1. **④ 前处理内部是并行的**，不是三件事串行。串行 41+33+88 = 162ms，并行 = max(41,33,88) + 合并 ≈ 95ms。
2. **⑩ 与 ⑨ 重叠**。审计不是「等模型说完再审」，那样 TTFT 会翻倍；是「边发边审」，用 300~800ms 的滑动窗口换安全（第 37 章）。
3. **⑫ 异步**。落库、trace、样本入湖绝不能在用户链路上。你多花 30ms，用户就多等 30ms，而这件事完全可以 200ms 后做。

---

## 10.4 关键设计

### 10.4.1 预算表：TTFT 与总时长是两个不同的目标

先说一个反直觉的结论：**TTFT 和总时长的优化方向经常是冲突的。**

| 优化动作 | TTFT | 总时长 | 结论 |
| --- | --- | --- | --- |
| 先发一句「让我查一下…」 | ↓ 好 | ↑ 差（多一次模型调用） | 值得，感知延迟优先 |
| 检索从 3 路加到 5 路 | ↑ 差 | ↑ 差（但质量↑） | 看任务，客服可接受 |
| 并行工具调用 | → 无影响 | ↓ 好 | 必做 |
| 流式输出 | ↓↓ 极好 | → 无影响 | 必做 |
| 上下文压缩 | ↓ 略好 | ↓↓ 好 | 必做（第 18 章） |
| 高精度 rerank | ↑ 差 | ↑ 差 | 只在质量敏感场景开 |

**目标设定（Aurora 客服 Agent）**：

| 指标 | P50 | P95 | P99 | 硬上限 | 超了怎么办 |
| --- | --- | --- | --- | --- | --- |
| TTFT（首字可见） | 1.0s | 2.0s | 3.0s | 3.0s | 发 `status: thinking` 占位（用户看到动的东西就不焦虑） |
| 首站响应（有内容） | 1.8s | 3.5s | 6.0s | 6.0s | 降级：跳过检索，用缓存答案 |
| 总时长（客服） | 12s | 25s | 40s | 60s | 截断输出 + `finish_reason=timeout` |
| 总时长（编码） | 90s | 180s | 240s | 300s | 转异步 + 通知（第 12 章） |
| 加权平均 | 30s | — | — | — | 决定容量（第 11 章） |

### 10.4.2 完整预算分配表

```
硬 deadline：客服 60s / 编码 300s
下面所有预算都在 deadline 内分配，且「后续预留」优先于「本站使用」
```

| # | 站点 | 职责 | 预算 | P50 实际 | 累计 TTFT | 超时兜底 | 可重试 | 归属 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ① | 接入 | TLS/协议/连接复用/请求体校验 | 30ms | 18ms | 18ms | 直接 400/408 | ✅ | 网关 |
| ② | 鉴权 | JWT 验签 + 租户配置 LRU | 20ms | 6ms | 24ms | 401；配置读失败用缓存 | ✅ | 治理面 |
| ③ | 幂等 | `Idempotency-Key` 占位/回放 | 10ms | 2ms | 26ms | store 挂 → 放行（宁重不漏） | ❌ | 接入面 |
| ④ | 前处理 | 规范化 / 长度 / 语言 / 注入预检 / 入站审核 | 150ms | 95ms（并行） | 121ms | 审核超时 → **失败关闭**（拒绝） | ⚠️ 仅审核可重试 | 管道 |
| ⑤ | 配额与准入 | 限流 / 预算预扣 / 槽位申请 | 15ms | 9ms | 130ms | 预扣失败 → 402；限流 → 429 | ✅ | 治理面 |
| ⑥ | 排队 | 等可用槽位 | 150ms（可配） | 20ms | 150ms | 超 150ms → 返回 `queued` + 排队事件（转异步） | — | 调度 |
| ⑦ | 上下文装配 | 会话加载 / 记忆 / 检索 / 裁剪 / 前缀组装 | **400ms** | 240ms | 390ms | 检索超时 → 跳过检索用缓存；记忆超时 → 只用短期 | ✅ | 上下文层 |
| ⑧ | 路由 | 选模型（能力/成本/健康度） | 20ms | 8ms | 398ms | 路由服务挂 → 用默认模型 + 告警 | ✅ | 治理面 |
| ⑨ | 模型步循环 | LLM 调用 + 工具调用 + 循环 | 见下 | 11.5s | 首 token 878ms | 单步超时 → 换模型/截断；步数超 → 强制收尾 | ✅（幂等步） | 运行时 |
| ⑩ | 流式+边审 | delta 下发 / 窗口审 / 撤回 | 每窗口 800ms | 52ms（首检） | **1.0s** | 审核降级 → 加延迟窗口 | ❌ | 管道 |
| ⑪ | 后处理+结算 | 校验 / 脱敏 / 用量 / 账本 | 100ms | 104ms | — | 结算失败 → 落 `billing_pending` 队列（第 27 章） | ✅ | 治理面 |
| ⑫ | 落库与回流 | 事件持久化 / trace / 样本入湖 | （异步 5s 内） | — | — | 重试 + 死信，不影响用户 | ✅ | 数据闭环 |

**⑨ 的内部预算**（这是唯一需要动态分配的一段）：

| 步 | 预算 | 说明 |
| --- | --- | --- |
| 单次 LLM 调用 | 20s | 超过则 abort，换备用模型（第 25 章） |
| 单次工具调用 | 10s | 超过标记 orphan，结果丢弃但保留审计 |
| 单步总时长 | 25s | llm + tool 之和 |
| 最大步数 | 16 | 硬上限，防失控循环（第 1 章断崖 3） |
| 剩余预算守门 | 每次循环前检查 | 剩余 < 8s → 强制进入「收尾模式」（不再调工具，直接生成答案） |

### 10.4.3 预算配置（YAML）

```yaml
# journey-budget.yaml  —— 按 agent 类型覆盖，可热更（第 43 章）
version: "22"
defaults:
  deadline_ms: 60000
  stages:
    ingress:      { budget_ms: 30,   on_timeout: fail_408 }
    auth:         { budget_ms: 20,   on_timeout: fail_401, cache_fallback: true }
    idempotency:  { budget_ms: 10,   on_timeout: pass_through }
    preprocess:
      budget_ms: 150
      parallel: [normalize, injection, moderation]
      on_timeout: fail_closed          # ★ 安全闸门失败必须关闭，不能放行
    quota:        { budget_ms: 15,   on_timeout: fail_429 }
    admission:    { budget_ms: 150,  on_timeout: return_queued_ticket }
    context:
      budget_ms: 400
      parallel: [session_load, memory_fetch, retrieve]
      on_timeout: degrade_retrieval    # ★ 降级而非失败
      retrieve:
        budget_ms: 300
        top_k: 8
        paths: 3
        on_timeout: { paths: 1, rerank: false }   # 提前降级的具体动作
    router:       { budget_ms: 20,   on_timeout: use_default_model }
    step_loop:
      max_steps: 16
      per_llm_ms: 20000
      per_tool_ms: 10000
      wind_down_below_ms: 8000      # 剩余 < 8s 进入收尾模式
    stream_audit: { window_ms: 800, on_timeout: add_delay_window }
    postprocess:  { budget_ms: 100,  on_timeout: skip_optional_checks }
    billing:      { budget_ms: 80,   on_timeout: enqueue_pending }
  async:
    persist_events: { budget_ms: 5000, retry: 5 }
    trace_export:   { budget_ms: 10000, retry: 3 }

overrides:
  agent:code:
    deadline_ms: 300000
    stages:
      context:    { budget_ms: 800 }
      step_loop:  { max_steps: 64, per_llm_ms: 60000 }
  agent:analytics:                 # 数据分析：重检索、重沙箱
    deadline_ms: 300000
    stages:
      context:    { budget_ms: 1200, retrieve: { top_k: 20 } }
      step_loop:  { per_tool_ms: 120000 }
```

### 10.4.4 伪代码：预算如何逐站传递

核心是那个 `stage()` 包装器——它做三件事：**扣预算、传 deadline、超时按站降级**。

```go
type Budget struct {
	deadline time.Time
	reserve  map[string]time.Duration // 后续站点的最低预留
	order    []string                 // 站的顺序，用于算"后面还需要多少"
	idx      int
	spent    map[string]time.Duration
}

// remaining 返回剩余可用时间；reserved 返回后续站点的最低预留之和
func (b *Budget) remaining() time.Duration { return time.Until(b.deadline) }
func (b *Budget) reserved() time.Duration {
	var s time.Duration
	for _, k := range b.order[b.idx+1:] { s += b.reserve[k] }
	return s
}

// allowed 本站最多能用多久 = min(本站预算, 剩余 - 后续预留)
func (b *Budget) allowed(stage string) time.Duration {
	bud := b.stages[stage]
	if slack := b.remaining() - b.reserved(); slack < bud { bud = slack }
	if bud < 0 { return 0 }
	return bud
}

// stage 执行一站：任何站都必须走这里，不允许裸调
func (j *Journey) stage(ctx context.Context, name string, fn func(context.Context) error) error {
	allow := j.budget.allowed(name)
	if allow <= 0 {
		// ★ 不是"超时"，是"还没开始就已经没预算了" → 直接走降级，不浪费一次调用
		return j.degrade(name, ErrBudgetExhausted)
	}
	sctx, cancel := context.WithTimeout(ctx, allow)
	defer cancel()

	start := time.Now()
	err := fn(sctx)
	j.budget.spent[name] = time.Since(start)
	j.trace.Stage(name, time.Since(start), err)

	if errors.Is(err, context.DeadlineExceeded) {
		j.metrics.Inc("stage.timeout." + name)
		return j.degrade(name, ErrStageTimeout) // 按站配置兜底（表 10.4.2）
	}
	return err
}

func (j *Journey) Run(ctx context.Context, req *RunReq) (*RunResult, error) {
	b := NewBudget(req.Limits.Deadline, req.AgentType)

	// ①~⑧：前置链，任何一站 fatal 就直接返回（但都要落事件）
	if err := j.stage(ctx, "ingress", j.ingress); err != nil { return nil, err }
	if err := j.stage(ctx, "auth", j.auth); err != nil { return nil, err }
	if err := j.stage(ctx, "idempotency", j.idem); err != nil { return nil, err }
	if err := j.stage(ctx, "preprocess", j.preprocess); err != nil {
		return j.rejectWithPolicy(ctx, req, err) // 失败关闭，但给用户明确话术
	}
	if err := j.stage(ctx, "quota", j.quota); err != nil { return nil, err }

	// ⑥ 排队：超预算不是错误，是"转异步"信号
	if err := j.stage(ctx, "admission", j.admission); err != nil {
		if errors.Is(err, ErrStageTimeout) {
			return j.enqueueAsync(ctx, req) // 返回 queued ticket + 事件流地址
		}
		return nil, err
	}

	// ⑦ 上下文装配（内部并行 fan-out，见 10.4.5）
	assembled, err := j.assembleContext(ctx, req)
	if err != nil && !isDegradable(err) { return nil, err }

	// ⑧ 路由
	route, _ := j.stageRouter(ctx, req, assembled)

	// ⑨ 步循环：唯一会反复消费预算的一段
	final, err := j.stepLoop(ctx, req, assembled, route)
	if err != nil {
		return j.settleFailed(ctx, req, err) // 也要结算已用量
	}

	// ⑪ 后处理 + 结算（必须做，但可以降级到异步）
	if err := j.stage(ctx, "postprocess", func(c context.Context) error {
		return j.postprocess(c, final)
	}); err != nil {
		j.queuePendingBilling(ctx, final) // 不让结算失败影响用户
	}

	// ⑫ 异步：不阻塞响应
	j.async.PersistEvents(final)
	j.async.ExportTrace(final)
	return final, nil
}
```

### 10.4.5 上下文装配的并行 fan-out（⑦ 站展开）

⑦ 站是前置链里唯一值得展开讲的，因为它**既是最大的一块延迟（240ms，占前置链 60%），又是最容易写成串行的**。

```go
func (j *Journey) assembleContext(ctx context.Context, req *RunReq) (*Context, error) {
	type result struct {
		kind string
		data any
		err  error
	}
	// 三路并行，各自有独立预算；一路慢不拖累其他两路
	ch := make(chan result, 3)

	go func() { d, e := j.session.Load(ctx, req.SessionID, 20); ch <- result{"session", d, e} }()
	go func() { d, e := j.memory.Fetch(ctx, req);              ch <- result{"memory", d, e} }()
	go func() { d, e := j.retriever.Search(ctx, req.Query, j.retrieveOpts()); ch <- result{"retrieve", d, e} }()

	ctxOut := &Context{Session: emptySession()}
	for i := 0; i < 3; i++ {
		r := <-ch
		switch r.kind {
		case "session":
			if r.err != nil { return nil, r.err } // 会话拿不到 = 致命
			ctxOut.Session = r.data.(*Session)
		case "memory", "retrieve":
			if r.err != nil {
				ctxOut.Degraded = append(ctxOut.Degraded, r.kind) // ★ 降级但继续
				continue
			}
			ctxOut.put(r.kind, r.data)
		}
	}
	// 预算裁剪（第 15、18 章）：超了就压，压不动就砍
	return j.budgeter.Fit(ctx, ctxOut, req)
}
```

**为什么 `session` 是 fatal 而 `retrieve` 是可降级**？因为会话拿不到就等于「失忆」，答案必然错；检索拿不到只是「少了一些参考资料」，模型还能靠参数知识回答，质量降但不至于崩。**每站都要回答这个问题：这一站挂了，是「不能答」还是「答得差一点」？** 前者 fatal，后者降级。

### 10.4.6 分站耗时落库（trace schema）

没有分站耗时，本章所有讨论都是空谈。最少要落这些：

```sql
-- ClickHouse 或 Postgres 分区表（第 45 章）
CREATE TABLE run_stage_timing (
    ts            DateTime,
    run_id        String,
    org_id        UInt64,
    agent_type    LowCardinality(String),
    stage         LowCardinality(String),   -- ingress/auth/preprocess/.../billing
    attempt       UInt8,                    -- 重试次数，重试要单独统计！
    duration_ms   UInt32,
    budget_ms     UInt32,                   -- ★ 必须记预算，否则无法判断"是否该优化"
    outcome       LowCardinality(String),   -- ok | timeout | degraded | error
    queue_ms      UInt32 DEFAULT 0,         -- 只在 admission 站有值
    degrade_reason LowCardinality(String)
) ENGINE = MergeTree
PARTITION BY toDate(ts)
ORDER BY (org_id, stage, ts)
TTL ts + INTERVAL 30 DAY;

-- 一线查询：哪些站超预算最多？（决定优化优先级）
SELECT stage,
       countIf(outcome = 'timeout') / count() AS timeout_rate,
       quantile(0.99)(duration_ms)            AS p99,
       any(budget_ms)                          AS budget,
       p99 / budget                            AS pressure
FROM run_stage_timing
WHERE ts > now() - INTERVAL 1 HOUR
GROUP BY stage
ORDER BY pressure DESC;
```

---

## 10.5 Java / 分布式经验对照

| 你熟悉的 | AgentOS 12 站 | 会咬人的差异 |
| --- | --- | --- |
| Spring Cloud Gateway 的 Pre/Post Filter | ①~⑤、⑪ | 过滤器是**同步无状态**的；这里 ④ 要花 100ms 且可能拒绝、⑦ 要并行 IO。别用同步 Filter 链硬套 |
| Hystrix/Resilience4j 的 `TimeLimiter` | 每站预算 | 这些库的超时是**每调用独立**的，不会「从总预算里扣」。你必须自己写 `Budget.allowed()` |
| gRPC 的 deadline propagation | 预算传递 | **这是唯一原生支持预算传播的机制**，Java 侧如果全链路 gRPC，可以直接用；HTTP 时代只能自己传 `X-Deadline-Ms` 头 |
| `ThreadPoolExecutor` 线程池隔离 | 站间隔离 | 每站一个池子会创建太多线程；Go 用 goroutine + 有界并发（`semaphore`）更轻 |
| `RequestContextHolder` / MDC | trace 上下文 | 异步/并行 fan-out 后 MDC 丢失，trace id 要**显式传参**。Go 的 `ctx` 强制你这么做，反而少踩坑 |
| `CompletableFuture.allOf` | ⑦ 并行装配 | 注意**异常传播**：`allOf` 里一个 future 抛异常，其他还在跑；要 `exceptionally` 逐路兜底 |
| 链路超时 60s（Nginx） | 硬 deadline | Nginx 会先掐，你的 graceful degrade 逻辑根本来不及跑。**硬 deadline 必须小于所有反代超时** |
| 定时任务补偿 | ⑫ 异步落库 | 别用 cron 扫表补；用消息队列 + 死信（第 13 章） |

**一句话**：Java 团队的肌肉记忆是「每个下游设超时」，而这里要的是「一条时间轴上的预算会计」。**换个心智模型：你不是在配置超时，你是在做时间维度的内存分配。**

---

## 10.6 容量与延迟的换算（把预算表用起来）

预算表不只是运维文档，它直接决定容量：

```
并发 Run 数（Little 定律，详见第 11 章）
  L = λ × W
  Aurora: 500 Run/s × 30s = 15,000 并发 Run

单 Run 在链路上占用的"槽位时长"
  = TTFT 期间独占 1 个槽位 + 生成期间独占 1 个槽位 = 30s（全程）
  → 所以"槽位"就是并发 Run 数本身

如果 ⑦ 站从 240ms 优化到 120ms：
  单 Run 省 120ms → 15,000 并发 × 0.12s = 1,800 槽位·秒/秒
  换算成容量：1,800 / 30 = 60 个槽位 ≈ 0.4 个副本  ← 几乎不值
  ⚠️ 结论：优化前置链对容量几乎无意义

如果 ⑨ 站模型从 40 tok/s 提到 60 tok/s（输出 1,200 token）：
  生成时间 30s → 20s，单 Run 减 10s
  容量提升 = 30/20 = 1.5 倍（同样槽位能跑 1.5 倍流量）
  ⚠️ 结论：模型吞吐是唯一真正影响容量的变量
```

> 🎯 **这就是预算表最重要的用途：它告诉你「优化哪里能换来容量，优化哪里只是自我安慰」。** 前置链（①~⑧）合计 400ms，占总时长 1.3%；模型生成占 96%。你在前置链上省 200ms 换来的是 P50 好看一点，容量一点没变。而模型侧的 20% 吞吐提升，直接等于 20% 的机器。

---

## 10.7 事故清单

- [ ] **各站超时叠加**：每站 30s，12 站最坏 360s，反代 60s 先掐，中间白烧 300s 的 token。
      → 根因：无总预算、无传播。
      → 兜底：`Budget.allowed()` 逐站扣减 + 硬 deadline < 反代超时。
- [ ] **降级永远来不及**：降级逻辑写在「超时 catch 里」，但超时已经是终态。
      → 根因：判断滞后。
      → 兜底：**进入站点前**判断剩余预算（`allowed() <= 0` 直接降级，不发起调用）。
- [ ] **安全闸门被降级放行**：前处理审核超时，代码 `if err != nil { continue }`，脏输入进了模型。
      → 根因：把 ④ 站当成普通站处理。
      → 兜底：④ 必须 `fail_closed`；审核超时 = 拒绝，不是放行。
- [ ] **检索长尾拖垮 P99**：某租户 4 万文档，检索 P99 从 300ms 到 9.2s。
      → 根因：检索无超时、无租户级配额、无降级。
      → 兜底：检索独立 300ms 预算 + 超时降级到 1 路 + 大租户独立索引（第 28 章舱壁）。
- [ ] **重试放大**：模型超时 → 站点重试 → 上层再重试 → 3×3×3=27 倍。
      → 根因：多层重试无预算共享。
      → 兜底：**只有最外层重试**，内层用「降级」不用「重试」。
- [ ] **结算失败导致整体 500**：用户答案已生成，但账本写失败，返回 500，用户重试又跑一遍。
      → 根因：⑪ 站没做「失败可异步补」。
      → 兜底：落 `billing_pending` 表 + 异步对账（第 27 章），用户侧返回 200。
- [ ] **异步站阻塞主链**：⑫ 落库同步写 ClickHouse，慢查询时整个请求卡 2s。
      → 根因：异步站写成了同步。
      → 兜底：内存队列 + 批量 flush，攒批 100 条或 200ms。
- [ ] **预算表没人维护**：新加一站忘了配预算，默认 0，该站永远降级。
      → 根因：配置无校验。
      → 兜底：启动时校验「所有站预算之和 × 1.2 < deadline」，否则启动失败。

---

## 10.8 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 12 站分配 | 前 8 站 400ms（3%）、⑨ 模型 96%、⑫ 异步不计入 |
| 预算方向 | 从总 deadline **往下分**，不是每站独立超时**往上加** |
| 剩余守门 | 每站启动前算 `min(本站预算, 剩余 − 后续预留)`；≤0 直接降级 |
| 失败分级 | 每站先回答：挂了是「不能答」（fatal）还是「答得差」（degrade） |
| 安全闸门 | ④ 前处理 `fail_closed`，超时即拒绝，绝不放行 |
| TTFT 目标 | P50 1.0s / P95 2.0s / 硬上限 3.0s，超了发 `thinking` 占位 |
| TTFT vs 总时长 | 两者目标冲突；「先说一句废话」牺牲总时长换感知 |
| 并行 | ④ 三检并行、⑦ 三路并行；串行 162ms → 并行 95ms |
| 重试 | 只有最外层重试，内层降级；否则 3×3×3 放大 |
| 容量真相 | 前置链优化 200ms ≈ 0.4 副本；模型吞吐 +20% = 容量 +20% |
| 必须落库 | `stage` + `duration_ms` + **`budget_ms`** + `outcome`，缺一不可定位 |
| 一句话 | **这条链上没有"每站都快"，只有"该放弃的时候知道放弃谁"。** |

## 10.9 自检题

1. **你的 trace 显示 `context.retrieve` P99 = 9s，但 P50 = 180ms。你有三种动作：加超时、加缓存、加舱壁隔离。排序并说明理由。**
   > 要点：顺序是 **加超时 → 加舱壁 → 加缓存**。① 加超时（300ms）立刻把 P99 拉回预算内，成本最低，且本来就应该有；② 加舱壁（大租户独立索引 + 独立并发池）解决根因「一个租户拖垮所有」，但工程量大；③ 加缓存是优化均值，对 P99 长尾基本无效（长尾往往是冷查询）。**关键判断：P50 好 P99 差 = 长尾问题，长尾问题用「超时 + 隔离」治，不用「缓存」治。**

2. **为什么「前处理审核超时」必须拒绝，而「检索超时」可以降级？请从「失败的方向」角度解释。**
   > 要点：这是 fail-closed vs fail-open 的选择。审核是**安全闸门**，fail-open 意味着脏输入（注入、违规内容）直接进最贵的 CPU，后果是安全事故 + 不可撤回；检索是**质量增强**，fail-open 的后果只是「答案少点参考资料」。判断标准：**这一站失败后，最坏情况是「多花钱」还是「造成不可逆损害」**。前者可降级，后者必须拒绝。

3. **有人说「我们把前置链从 400ms 优化到 150ms，性能提升 60%」。这句话哪里有问题？**
   > 要点：60% 是**前置链内部**的比例，不是端到端的。端到端 12.4s，省 250ms = 2%。容量上更惨：15,000 并发 × 0.25s / 30s ≈ 125 槽位，不到 1 个副本（每副本 200 槽位）。**正确的说法是「P50 TTFT 降了 250ms」，而不是「性能提升 60%」。** 这种表述误导决策，是架构评审里最常见的自欺。

## 10.10 延伸阅读

- 上一章：[9. 端云协同：端侧 Agent 与云侧卸载](9-edge-cloud-split.md)
- 下一章：[11. 准入控制、排队与公平性](11-admission-and-queue.md)
- 前置：[6. Session / Run / Step / Event 状态模型](6-session-run-event-model.md)（为什么每站都要落事件）
- 前处理站的展开：[35. 前处理层](35-pre-processing.md)
- 流式与边发边审：[36. 流式输出工程](36-streaming-output.md)、[37. 输出审计与撤回](37-output-audit-retraction.md)
- 上下文装配与预算裁剪：[15. 上下文预算](15-context-budget.md)、[18. 上下文压缩](18-context-compression.md)
- 每站的可观测字段：[39. 可观测层](39-observability.md)
- 超时、熔断、降级阶梯：[25. 降级与容错](25-degradation-failover.md)
- 超过 30s 转异步：[12. 长任务与持久化执行](12-long-task-durable.md)
- 结算失败的兜底：[27. 计费的可靠性](27-billing-reliability.md)

---

**交叉引用**：总纲 [0-summary.md](0-summary.md) ｜ 前置 [9-edge-cloud-split.md](9-edge-cloud-split.md) ｜ 下一章 [11-admission-and-queue.md](11-admission-and-queue.md)
