# 11. 准入控制、排队与公平性

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 请求生命周期与队列调度 ｜ **编号**：11 / 52
> **本章定位**：第 10 章给出了每站的预算，但有一件事预算解决不了：**当请求数超过容量时，谁先跑、谁等着、谁被拒。** 这是操作系统的调度器问题，只不过这里的「进程」是 Run，「CPU 时间」是模型 token。**这是全书唯一一章纯算法 + 纯算账的章节，也是 5k QPS 下平台能不能活下来的分水岭。**
> **读法**：§11.2 先建立一个反直觉认知（不排队 = 集体超时），§11.3 的漏斗图是全章骨架，§11.4 的 DRR 与 §11.6 的容量算式是能直接抄的两块。

---

## 11.1 引子：一次「加机器」引发的雪崩

周三上午 10:04，Aurora 的营销活动开始，流量从 3,200 QPS 涨到 6,500 QPS。

SRE 的第一反应很正常：扩容。HPA 在 90 秒内把 Run Worker 从 75 个扩到 140 个。**但 P99 从 18s 涨到了 190s，成功率从 99.2% 掉到 61%。**

复盘时看到的因果链，让所有人沉默：

```
10:04:00  λ 从 500 Run/s 涨到 650 Run/s（+30%）
          容量仍是 500 Run/s（15,000 槽位 ÷ 30s）
          → 每秒净积压 150 个 Run

10:04:20  队列 3,000 个。新请求平均等 6 秒才开始跑
          用户开始手动刷新（以为卡了）→ λ 再 +15%

10:05:00  队列 9,000 个。等待 18 秒 > 用户容忍阈值（10s）
          → 用户关页面 → 客户端重连重试 → λ 变 1,300/s

10:05:30  HPA 扩容完成（140 个 Worker，28,000 槽位）
          ★ 但新 Worker 启动时要加载模型路由表 + 预热连接，需要 45 秒
          ★ 而且它们一启动就被队列里最老的 9,000 个请求打满
          → 队列没变短，只是「等待的请求被更多机器同时处理」，
            每个请求的等待时间不变，但因为都超时了，重试还在继续

10:07:00  重试流量占比 48%。有效吞吐没涨，无效 token 消耗翻了 3 倍
          → 模型侧配额打满 → 上游 429 → 更多重试

10:12:00  SRE 手动限流到 400/s + 清空队列（丢弃排队超过 20s 的请求）
          → 系统在 90 秒内恢复
```

> 🩸 **这次事故的根因不是「容量不够」，是「没有准入控制」。**
> 如果有准入：650/s 的请求里，500 个进队列、150 个立刻拿到 `429 + Retry-After: 2`。用户看到「系统繁忙，请稍后重试」——**难听，但 500 个请求正常服务，成功率 77%，P99 稳定在 20s。**
> 而没有准入：**所有 650 个请求都进入等待，最终 39% 成功、P99 190s、重试把流量放大到 1,300/s。**
>
> **不排队会死，无限排队死得更快。** 排队是一种资源，它也有容量。

---

## 11.2 断崖：三种「不设准入」的死法

| 死法 | 机制 | 量化后果 |
| --- | --- | --- |
| **集体超时** | 无上限排队 → 每个请求都在等 → 所有请求都超过 deadline | λ=650、容量 500/s → 60s 后队列 9,000，等待 18s > 容忍阈值 |
| **重试雪崩** | 超时 → 客户端重试 → λ 翻倍 → 队列更长 → 更多超时 | 重试占比 48%，有效吞吐没涨，成本 ×3 |
| **公平性崩塌** | 大租户跑批把队列占满，小租户全饿死 | 一个租户的批处理 20,000 个 Run 能让 99,900 个其他租户全部超时 |
| **缓存穿透型雪崩** | 队列里全是「本该被拒」的请求，命中率归零 | 语义缓存命中率 34% → 2%（因为都在等，没人拿到答案） |
| **饥饿** | 无优先级 → 长任务挤占短任务 | 编码 Agent（90s）和客服（12s）混跑，客服 P99 从 18s 到 70s |

这里必须钉死一个反直觉结论：

> 🎯 **排队不会增加吞吐，只会把「拒绝」变成「更慢的拒绝」。**
> 容量是 500/s，你塞 650/s 进来，最终还是有 150/s 得不到服务——区别只是：**立刻告诉他们（429，用户 2 秒后重试成功）**，还是**让他们等 18 秒再告诉他们（504，用户已经骂完了）**。
> 所以准入控制的第一原则：**早拒绝优于晚拒绝，晚拒绝优于不拒绝（挂死）。**

---

## 11.3 结构图：准入漏斗 + 队列 + 公平调度

```
   λ = 5,000 req/s（网关总入口，含查询/审批/事件）
   其中 Run 创建 λ_run = 500/s
        │
        ▼
┌───────────────────────────────────────────────────────────────────────┐
│ ⓪ 前置闸门（第 35 章）：规范化/注入预检/审核 —— 拦住 6% 脏请求         │
│    成本：按规则算（~0.1ms），不是按 token 算                          │
└───────────────────────────┬───────────────────────────────────────────┘
                            │ 470/s
                            ▼
┌───────────────────────────────────────────────────────────────────────┐
│ ① 三维准入（全部通过才放行，任一不过走对应出口）                        │
│                                                                        │
│   维度 A｜并发槽位    used_slots < total_slots × 0.92                  │
│   维度 B｜token 预算  预扣 est_tokens × price ≤ 租户剩余额度            │
│   维度 C｜请求速率    租户令牌桶 / 全局限流（第 23 章）                  │
│                                                                        │
│   三个出口：                                                            │
│     ✅ 放行 → 拿槽位 → 直接执行（快车道，不排队）                       │
│     ⏳ 排队 → 入 FIFO/WFQ 队列，返回 ticket + 预计等待                  │
│     ⛔ 拒绝 → 429/402/503 + Retry-After（明确告诉用户多久后能好）        │
└───────────────────────────┬───────────────────────────────────────────┘
                            │
              ┌─────────────┴──────────────┐
              ▼ 92%                        ▼ 8%（超阈值）
      ┌───────────────┐            ┌──────────────────┐
      │  准入快车道    │            │  拒绝 + Retry-After│
      └───────┬───────┘            └──────────────────┘
              │
              ▼
┌───────────────────────────────────────────────────────────────────────┐
│ ② 优先级队列（三层，各自独立 FIFO，避免长任务堵短任务）                  │
│                                                                        │
│   P0 交互     ████████████  quantum 权重 70   deadline 60s  可抢占 P2  │
│   P1 批处理   ██████        quantum 权重 25   deadline 600s            │
│   P2 离线评估 ██            quantum 权重  5   deadline 6h   可被抢占    │
│                                                                        │
│   每层内部：分层 DRR（租户组 → 租户 → 用户）                             │
│     组 A(权重40) ─┬─ 租户A1(w=10) ─┬─ user1                        │
│                   │                └─ user2                        │
│                   └─ 租户A2(w=8)                                   │
│     组 B(权重20) ─── 租户B1(w=5) ...                                │
└───────────────────────────┬───────────────────────────────────────────┘
                            │ 调度器每次取「虚拟时间最小」的队首
                            ▼
┌───────────────────────────────────────────────────────────────────────┐
│ ③ 槽位池（Bulkhead 分池，第 28 章）                                     │
│   ┌──────────────┐ ┌──────────────┐ ┌──────────────┐                  │
│   │ 客服池        │ │ 编码池        │ │ 数据分析池    │                  │
│   │ 11,000 槽位   │ │ 26,400 槽位   │ │ 4,000 槽位    │                  │
│   │ 55 节点       │ │ 132 节点      │ │ 20 节点       │                  │
│   └──────────────┘ └──────────────┘ └──────────────┘                  │
│   ★ 池间不互相借槽位（除非显式借用 + 可抢占）                            │
└───────────────────────────┬───────────────────────────────────────────┘
                            ▼
                     Run Worker 执行（第 10 章 12 站）
```

三层设计各自解决一个不同的问题，缺一不可：

| 层 | 解决什么 | 不做的后果 |
| --- | --- | --- |
| **三维准入** | 不让系统进入过载区 | 集体超时（§11.1） |
| **优先级队列** | 不同 SLA 的请求不互相踩 | 编码 Agent 把客服的 P99 拖到 70s |
| **DRR 公平** | 同一优先级内，租户之间不互相饿死 | 一个大租户能饿死 10 万小租户 |
| **槽位分池** | 故障与流量不跨类传播 | 编码池被打满，客服也挂（共享连接池/模型配额） |

---

## 11.4 关键设计

### 11.4.1 三维准入：为什么一个维度不够

只有并发限制，会被「小请求洪水」和「大请求怪兽」同时突破；只有速率限制，会被「慢请求占坑」拖死。

| 维度 | 度量 | 阈值（Aurora） | 超了怎么办 | 为什么需要它 |
| --- | --- | --- | --- | --- |
| **A 并发槽位** | 在途 Run 数 | `total_slots × 0.92` | 排队（不是拒绝） | 这是唯一真实的容量指标；0.92 是给突发留的缓冲 |
| **B token 预算** | 预扣成本 | 租户余额 × 0.9 | 402 / 降配到小模型 | 防止「一个失控 Run 烧穿月预算」 |
| **C 请求速率** | 令牌桶 | 按套餐（第 23 章） | 429 | 防止单租户刷接口（成本更低的保护） |
| **D 队列深度** | 在队请求数 | `λ × W_q_target` = 1,500 | 立即拒绝（不排队） | 防止队列变成内存黑洞 |
| **E 排队时长** | 队首等待 | 15s | 拒绝新请求（队首保留） | 让「已经在等的人」不被新流量饿死 |

维度 D 和 E 是最容易被漏掉的两个，但它们才是「雪崩开关」：

```
队列上限 L_q = λ_allow × W_q_target = 500/s × 3s = 1,500 个

为什么是 1,500？
  W_q_target = 3s  ← 用户能忍的排队时间（超过就该直接说"忙"）
  λ_allow = 500/s  ← 服务的实际吞吐
  超过 1,500 就拒绝，因为再排下去 ETA 一定 > 15s，用户不会等
```

### 11.4.2 Go 准入控制器

```go
type Admission struct {
	slots   *Semaphore        // 并发槽位，总容量 total，阈值 92%
	queue   *FairQueue        // 分层 DRR 队列
	buckets *RateLimiterSet   // 租户令牌桶
	budgets *BudgetLedger     // token 预算预扣

	maxSlots   int64
	maxQueue   int
	maxWait    time.Duration
}

type AdmissionResult struct {
	Decision string        // "admit" | "queue" | "reject"
	Ticket   *QueueTicket  // queue 时返回
	Retry    time.Duration // reject 时返回
	Code     string        // overloaded | concurrency_limited | queue_full
}

func (a *Admission) Admit(ctx context.Context, r *RunReq) (*AdmissionResult, error) {
	// ① 速率（最便宜，先算）：令牌桶，按租户
	if !a.buckets.Allow(r.OrgID, r.Class) {
		return &AdmissionResult{Decision: "reject", Code: "rate_limited",
			Retry: a.buckets.RetryAfter(r.OrgID, r.Class)}, nil
	}

	// ② 预算（次便宜）：预扣。按 est_tokens 估，结算时多退少补（第 27 章）
	est := r.EstTokens() // 见 11.4.5 的估算式
	if err := a.budgets.Hold(ctx, r.OrgID, est); err != nil {
		return &AdmissionResult{Decision: "reject", Code: "quota_exhausted"}, nil
	}

	// ③ 并发槽位（真正的容量判定）
	used := a.slots.Used()
	limit := int64(float64(a.maxSlots) * 0.92)
	if used < limit {
		if ok := a.slots.TryAcquire(1); ok {
			return &AdmissionResult{Decision: "admit"}, nil // ★ 快车道
		}
	}

	// ④ 要排队了，先看队列还装不装得下（维度 D）
	if a.queue.Len() >= a.maxQueue {
		a.budgets.Refund(ctx, r.OrgID, est) // ★ 必须退预扣，否则额度泄漏
		return &AdmissionResult{Decision: "reject", Code: "queue_full", Retry: 2 * time.Second}, nil
	}

	// ⑤ 再看「排到了要等多久」，等太久不如现在就说不行（维度 E）
	eta := time.Duration(a.queue.Len()/a.throughput()) * time.Second
	if eta > a.maxWait {
		a.budgets.Refund(ctx, r.OrgID, est)
		return &AdmissionResult{Decision: "reject", Code: "overloaded",
			Retry: minDuration(eta/2, 30*time.Second)}, nil
	}

	// ⑥ 入队，返回 ticket（带位置与 ETA，用于排队体验）
	t := a.queue.Push(r)
	return &AdmissionResult{Decision: "queue", Ticket: t}, nil
}

// 出队 → 真正执行
func (a *Admission) Dispatcher(ctx context.Context) {
	for {
		run, ok := a.queue.Pop(ctx) // DRR，非阻塞时返回 false
		if !ok { time.Sleep(time.Millisecond); continue }

		// ★ TOCTOU 复查：排队期间可能已被取消 / 租户降级 / 过期（第 8 章）
		if run.Canceled || time.Now().After(run.Deadline) {
			run.Mark("expired_in_queue")
			continue
		}
		go a.execute(ctx, run) // execute 结束时 Release 槽位 + Refund 差价
	}
}
```

> ⚠️ 三个必须写对的细节，漏一个就是事故：
> 1. **预扣必须在拒绝时退**。忘了退 → 租户额度泄漏，跑一天发现「余额还在但用不了」。
> 2. **出队后必须复查取消/过期**。排队 18 秒里用户很可能已经取消了（第 8 章的 40 次/秒）。
> 3. **快车道优先级高于队列**。如果已排队的人要等新请求先跑，那排队就变成了惩罚老用户。

### 11.4.3 优先级：三类，不是五类

| 类别 | 典型场景 | 目标 SLA | 权重 | deadline | 可被抢占 | 资源池 |
| --- | --- | --- | --- | --- | --- | --- |
| **P0 交互** | 用户在屏幕前等 | TTFT ≤ 2s，P99 ≤ 40s | 70 | 60s | ❌ 抢占别人 | 独立池，不允许排队超过 3s |
| **P1 批处理** | 工单批量处理、文档批量摘要 | P99 ≤ 10min | 25 | 600s | ✅ 可被 P0 抢占 | 共享池 |
| **P2 离线评估** | 评测集回归、合成数据生成 | 无 SLA（数小时内） | 5 | 6h | ✅ 可被 P0/P1 抢占 | 潮汐池（用闲置容量） |

**为什么只有三类？** 因为**优先级越多，反转和饥饿越难排查**。五类优先级在纸上很优雅，线上会出现「P3 永远跑不到」和「P2 偶尔比 P1 快」的诡异现象（K8s 也只有三个 QoS class，这不是巧合）。

**抢占怎么做**（P0 需要槽位但没有）：选一个 P2 的 Run，**checkpoint 后重新入队**（第 12 章），释放槽位。约束：

```
抢占成本 = checkpoint 写入 + 重新调度 + 已消耗 token（白烧）
所以：只抢占「已运行 < 30%」的 P2 Run，且每个 P2 Run 最多被抢占 2 次
     （否则它会永远在 checkpoint 循环里，永远跑不完 → 饿死）
```

**必须防的优先级反转**：即便你的队列按 P0 优先出队，**模型服务内部的 continuous batching 会把 P0 和 P2 拼在一个 batch 里**——P2 的长输出会拖慢 P0 的 token 速率。解法是**模型侧也分池**：P0 走独立 endpoint / 独立 LoRA 副本，或者用 vLLM 的 priority scheduling（`--scheduling-policy priority`）配合多队列。**队列层的优先级，必须穿透到模型层，否则等于没做。**（详见第 14 章）

### 11.4.4 分层 DRR：10 万租户怎么公平

先看为什么不能「简单轮询」：10 万租户轮询一轮，活跃的只有 8,000 个，一轮扫 8,000 个空队列 = 纯浪费；而且权重无法表达（大客户该多分）。

**DRR（Deficit Round Robin）** 的做法：每个租户有一个 `deficit` 计数器，每轮加 `quantum`（正比于权重），能发多少发多少，发不动就留到下一轮。

```go
type tenantState struct {
	id      string
	weight  int           // 权重 = f(套餐, 当前用量)
	quantum int           // quantum = baseQuantum * weight
	deficit int
	queue   []*Run        // 该租户的等待队列（FIFO）
	active  bool          // 在 active 环里才被扫描
}

type DRR struct {
	active    []*tenantState // ★ 只有非空的租户在环里，环小则快
	cursor    int
	baseQ     int // 基础 quantum，如 1（1 个 Run）
}

func (d *DRR) Push(r *Run) {
	t := d.getOrCreate(r.OrgID)
	t.queue = append(t.queue, r)
	if !t.active {
		t.active = true
		t.deficit = 0
		d.active = append(d.active, t) // 进环
	}
}

// Pop 返回下一个该跑的 Run；没有就返回 nil
func (d *DRR) Pop() *Run {
	for len(d.active) > 0 {
		t := d.active[d.cursor%d.len()]
		if len(t.queue) == 0 { // 空了，移出环
			d.remove(d.cursor % d.len())
			continue
		}
		// 补 quantum：权重大的租户每轮补得多
		if t.deficit <= 0 {
			t.deficit = d.baseQ * t.weight
		}
		r := t.queue[0]
		t.queue = t.queue[1:]
		t.deficit--          // ★ 每次只服务一个 Run，让环转得快
		d.cursor++
		return r
	}
	return nil
}
```

**为什么 `quantum` 按「Run 个数」而不是「token 数」？** 因为按 token 算需要预估，而预估必然有误差，误差会让权重失真。按 Run 个数算简单、无误差；「大 Run 占更多资源」的问题由**槽位池 + 并发限制**解决，不该由队列的 quantum 解决。**一个机制只解决一个问题。**

**分层（两级）**：10 万租户直接 DRR 环太大（最坏 O(活跃租户)）。按租户组（行业/套餐/大客户专属）分 200 组，组间 DRR + 组内 DRR：

```
扫描成本对比（活跃租户 8,000）
  单级 DRR：每取一个 Run 平均扫 8,000/8,000 = 1 次，但环维护成本 O(1)
  两级 DRR：组间 200 个 + 组内平均 40 个 → 每取一个 Run 扫 ~1.05 次
  ⚠️ 其实单级的环操作也是 O(1)（用环形数组 + 跳过空），
     分层的真正价值不是性能，是「组级配额」：
     可以给「免费用户组」整体限 20% 容量，组内再公平。
```

**公平性怎么量化验证**（不然你无法证明 DRR 有效）：

```
定义：Jain 公平指数 J = (Σx_i)² / (n × Σx_i²)   ∈ (1/n, 1]
  x_i = 租户 i 在过去 60s 获得的槽位·秒
  J = 1 → 完全公平；J = 0.5 → 严重不均

Aurora 目标：同权重租户的 J ≥ 0.92
实测：简单 FIFO 下 J = 0.31（大租户吃掉 70% 容量）
      两级 DRR 下 J = 0.94
```

### 11.4.5 容量算式：把「要几个节点」算出来

这是本章最该背下来的部分。所有数都从 Aurora 的真实参数出发。

```
【已知】
  λ_peak      = 500 Run/s           （Run 创建速率）
  Ts_mean     = 30s                 （加权平均服务时长）
  Ts_p99      = 90s                 （客服 25s / 编码 240s 加权后的 P99）
  每节点槽位   = 200                 （受模型并发 + 内存 + 连接数约束，第 14 章）

【公式一：并发数（Little 定律）】
  L = λ × W
  L_mean = 500 × 30  = 15,000 并发 Run
  L_p99  = 500 × 90  = 45,000 并发 Run   ← 按 P99 规划

【公式二：节点数】
  N = L / 每节点槽位 × (1 + 故障余量)
  按均值：N = 15,000 / 200 × 1.2 = 90 个节点
  按 P99 ：N = 45,000 / 200 × 1.1 = 248 个节点
  ★ 差额 158 个节点 / 月 ≈ ¥XXX 万 —— 这就是「绝不排队」的保费

【公式三：理性选择（准入 + 排队兜底）】
  按均值 + 20% 规划：90 节点 = 18,000 槽位
  服务能力 μ = 18,000 / 30s = 600 Run/s
  允许到达 λ_allow = 500/s（留 17% 缓冲）
  → 超出 500/s 的部分：排队（≤ 3s）或拒绝（> 3s）

【公式四：排队深度上限】
  L_q = λ_allow × W_q_target = 500 × 3 = 1,500 个

【公式五：分池后的容量（Bulkhead）】
  客服：λ=400/s, Ts_p99=25s → 400 × 25 × 1.1 = 11,000 槽位 = 55 节点
  编码：λ= 95/s, Ts_p99=240s → 95 × 240 × 1.1 = 25,080 槽位 = 126 节点
  分析：λ=  5/s, Ts_p99=400s →  5 × 400 × 1.1 =  2,200 槽位 = 11 节点
  合计 192 节点（vs 混合 248），省 23%
  ★ 分池不是为了省钱，是为了「编码池抖动不影响客服」
```

| 参数 | 值 | 怎么来的 | 调它会发生什么 |
| --- | --- | --- | --- |
| `每节点槽位 = 200` | 200 | min(模型并发 400, 连接 5,000, 内存 200×40MB) | 调高 → 单节点抖动放大；调低 → 成本上升 |
| `准入阈值 = 0.92` | 0.92 | 经验值：留 8% 给突发与长尾 | 调高 → 队列变长；调低 → 机器浪费 |
| `W_q_target = 3s` | 3s | 用户容忍阈值（TTFT 2s 的两倍） | 调高 → 队列更长，超时更多 |
| `maxWait = 15s` | 15s | 超过就该直接说「忙」 | 调高 → 用户等 20s 后关页面，白烧 |
| `故障余量 = 1.1~1.2` | 1.2 | 单 AZ 故障时剩 2/3 容量还得撑住 | 调低 → 一个 AZ 挂了全线超时 |

### 11.4.6 排队体验：把「等」变成「可以接受」

排队本身不是问题，**不知道要等多久**才是问题。三个信号必须回传：

| 信号 | 频率 | 内容 | 前端表现 |
| --- | --- | --- | --- |
| `queue.position` | 1s 或位置变化时 | 前面还有 N 个（不是全局 N，是同优先级+同租户视角） | 「前面还有 12 位」 |
| `queue.eta_ms` | 3s | 预计等待（带 ±30% 抖动区间） | 「预计 8 秒」→ 不要显示「约 1 分钟」 |
| `run.admitted` | 事件触发 | 开始执行 | 从「排队中」切到「正在思考」 |

```go
// ETA 估算：不要用「队列长度 × 平均服务时间」，那是错的
// 正确做法：用「可用槽位的周转率」估算
func (q *FairQueue) ETA(t *QueueTicket) time.Duration {
	// ① 我前面有多少「同等或更高优先级」的 Run
	ahead := q.countAhead(t) // O(1)：每个 ticket 记入队时的序号

	// ② 当前吞吐（秒级的滑动平均，不是理论值）
	tput := q.throughputPerSec() // 实测 600 Run/s 完成数

	// ③ 我的租户在 DRR 里的实际配额份额（不是 1/N！）
	share := q.orgShare(t.OrgID) // 例如大租户 5%，小租户 0.1%

	eta := time.Duration(float64(ahead) / (tput * share) * float64(time.Second))

	// ④ 加抖动区间，别给单点承诺
	return eta // UI 显示 eta ± 30%
}

// 保底承诺：如果 ETA 超过阈值，主动升级而不是让用户干等
func (q *FairQueue) Guard(t *QueueTicket) {
	if time.Since(t.EnqueuedAt) > 15*time.Second {
		q.escalate(t) // ① 提升到 P0 ② 或降配到小模型先给个答案 ③ 或明确告知"预计还需 2 分钟，是否继续等"
	}
}
```

> 💡 排队体验里最有效的一招不是「更快」，是**「给一个可以放弃的选择」**：「前面还有 40 位，预计 3 分钟。你可以 **[继续等]** 或 **[用快速模式（小模型，立即回答，质量略低）]**。」——把等待变成一个用户主动做出的权衡，投诉率能降一半。**这本质上是把「降级」包装成了「选择」。**

### 11.4.7 优先级与配额配置

```yaml
# admission-policy.yaml
version: "31"
global:
  slot_total: 18000
  admit_threshold: 0.92
  queue_max: 1500
  max_wait: 15s
  base_quantum: 1

classes:
  - name: interactive
    weight: 70
    deadline: 60s
    preemptible_by: []
    can_preempt: [offline]
    pool: interactive
    max_queue_wait: 3s          # P0 排队超 3s 就直接返回 ticket 转异步
  - name: batch
    weight: 25
    deadline: 600s
    preemptible_by: [interactive]
    pool: shared
  - name: offline
    weight: 5
    deadline: 6h
    preemptible_by: [interactive, batch]
    pool: tidal               # 潮汐池：跟随整体利用率伸缩
    only_when_util_below: 0.7  # ★ 主池利用率 > 70% 时离线任务不进池

tenant_groups:
  - id: enterprise
    weight: 40
    slot_quota: 0.55          # 最多占 55% 容量（防单组垄断）
    members_quantum: tier     # 组内按套餐分权重
  - id: standard
    weight: 45
    slot_quota: 0.40
  - id: free
    weight: 15
    slot_quota: 0.20          # ★ 三者之和 > 1 是故意的：配额是上限不是预留
    max_queue_wait: 2s

preemption:
  enabled: true
  victim_classes: [offline]
  max_run_progress: 0.3       # 只抢运行不到 30% 的
  max_preempt_per_run: 2      # 防饿死
  checkpoint_timeout: 5s
```

---

## 11.5 Java / 分布式经验对照

| 你熟悉的 | AgentOS 调度 | 会咬人的差异 |
| --- | --- | --- |
| `ThreadPoolExecutor` + 有界队列 + `CallerRunsPolicy` | 准入 + 队列 + 拒绝 | 线程池的队列满会走拒绝策略，**但线程池不知道「一个任务要跑 30 秒」**。Agent 的槽位必须按「时长×并发」算，不是按线程数 |
| Sentinel 的 `FlowRule`（QPS 限流） | 维度 C | 限流只管速率，不管**并发占用**。500/s 放进来但每个跑 30s，一样会崩 |
| Kafka 消费组 rebalance | DRR 公平调度 | Kafka 的公平是「分区分配」，静态；DRR 是动态的、带权重的、每秒都在变 |
| K8s 调度器的 PriorityClass + 抢占 | 优先级 + 抢占 | K8s 抢占是「驱逐 Pod」，代价是分钟级；Agent 抢占是「checkpoint + 重新入队」，秒级，但**会白烧已消耗的 token** |
| K8s 的 QoS Class（Guaranteed/Burstable/BestEffort） | P0/P1/P2 | 同构概念。区别：K8s 按资源 request/limit 分，这里按 SLA + 权重分 |
| Hystrix 线程池隔离 | 槽位分池 | 同构（Bulkhead）。但 Agent 分池的边界是**Agent 类型**，因为它们的时长差 10 倍（12s vs 240s），混池必然长尾 |
| 消息队列的「堆积告警」 | 队列深度上限 | MQ 敢堆 100 万条，因为消费快、可回溯；Agent 队列堆 1 万条就是灾难（用户在线等） |
| `ScheduledExecutorService` 定时补偿 | ETA 与超时清理 | 排队超时必须**主动清理并通知**，不能等客户端超时（那已经晚了 15 秒） |

**最容易被 Java 团队做错的一件事**：用「线程池 + 无界队列」实现准入。`new LinkedBlockingQueue<>()` 无界 → 请求全进来 → 内存涨 → OOM 或 GC 停顿 → 全线挂。**Agent 平台的队列必须有界，而且界的算法是 `λ × W_q_target`，不是「凭感觉给个 10,000」。**

---

## 11.6 开源坐标

| 项目 | 对应能力 | 能抄什么 | 缺什么 |
| --- | --- | --- | --- |
| **K8s 调度器** | 优先级、抢占、QoS class、资源 request/limit | **三层结构（PriorityClass / QoS / Preemption）的设计哲学**；`least-allocated` 打分 | 它调度的是「长时间驻留的 Pod」，不是「30 秒的请求」；没有队列深度概念 |
| **Kafka 消费组** | 分区分配公平性 | `StickyAssignor` / `CooperativeSticky` 的「尽量少搬动」思路——DRR 的环也要避免频繁进出 | 静态分配，无权重，无优先级 |
| **YARN Capacity Scheduler / Fair Scheduler** | 多队列 + 权重 + 抢占 | **最接近的一号参照物**：队列树、容量保证、最大容量、抢占 | 面向 Hadoop 批作业（分钟~小时级），粒度太粗 |
| **vLLM 的调度**（`--scheduling-policy`、continuous batching） | 模型层排队 | FCFS / priority 两种策略；PagedAttention 的块管理 | 它是**单实例内**的调度，不解决跨实例的全局准入（第 14 章） |
| **Envoy / 限流过滤器**（local/global rate limit） | 维度 C | 全局限流用 Redis 的成熟实现 | 只有速率，没有「并发槽位」与「队列」 |
| **Temporal 的 Task Queue** | 任务分发 | 有 task queue + worker 拉取 + 限流（`worker.taskSlots`） | 无优先级（官方明确说 task queue 不保证优先级）；无跨租户公平 |
| **NSQ / RabbitMQ 的 priority queue** | 优先级 | 多级优先级的实现细节 | 只支持固定几级，且不支持动态权重 |

> 🎯 判断：**全局准入 + 多租户公平调度这块，没有现成的开源方案可以直接用。** YARN 的 Capacity Scheduler 思路能抄 70%，但它的粒度（队列、容器、ApplicationMaster）跟 Agent 平台对不上——你需要的是「毫秒级决策 + 每秒上千次入队出队 + 带 token 成本的预算判定」。**这块自研是合理的，但要抄 YARN 的队列树与配额模型，别自己发明。**

---

## 11.7 事故清单

- [ ] **无准入 → 集体超时**：λ=650、容量 500/s，60s 后队列 9,000，P99 190s，成功率 61%。
      → 根因：没有并发上限，所有请求都进队列。
      → 兜底：三维准入 + 队列上限 `λ × W_q` + 早拒绝（429 + Retry-After）。
- [ ] **预扣不退导致额度泄漏**：拒绝时忘了 `Refund`，租户「余额显示有钱但发不出请求」。
      → 根因：`Hold`/`Refund` 不成对。
      → 兜底：`defer refund` 模式 + 每日对账扫描（第 27 章）。
- [ ] **排队期间被取消的 Run 仍然执行**：用户取消后 18 秒，Run 被调度器取出并执行，白烧 token。
      → 根因：出队后没复查 `canceled` / `deadline`。
      → 兜底：出队即复查（每天的常态化竞态，40 次/秒）。
- [ ] **大租户饿死全场**：一个客户跑了 20,000 个批处理 Run，99,900 个租户全部超时。
      → 根因：FIFO + 无租户配额。
      → 兜底：分层 DRR + `slot_quota` 上限 + P2 独立池。
- [ ] **优先级反转**：队列按 P0 优先，但模型侧 batch 把 P0 和 P2 拼一起，P0 的 token 速率掉 60%。
      → 根因：优先级没穿透到模型层。
      → 兜底：模型侧独立 endpoint / priority scheduling（第 14 章）。
- [ ] **抢占导致饿死**：一个 P2 Run 被抢了 5 次，永远跑不完。
      → 根因：无 `max_preempt_per_run`。
      → 兜底：抢占次数上限 + 被抢后权重临时提升（aging）。
- [ ] **ETA 骗人**：显示「预计 8 秒」，实际 90 秒，用户投诉。
      → 根因：用「队列长度 × 平均时长」估算，忽略租户在 DRR 里的份额。
      → 兜底：ETA = ahead / (tput × orgShare) + 抖动区间 + 超 15s 主动 escalate。
- [ ] **HPA 扩容追不上**：扩出来的节点启动要 45 秒，扩的时候队列已经雪崩。
      → 根因：只靠 reactive 扩容。
      → 兜底：按「队列深度 + 准入拒绝率」提前扩容（predictive）+ 扩容期间**先限流**。

---

## 11.8 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 第一原则 | 排队不增加吞吐，只把「拒绝」变成「更慢的拒绝」；早拒绝 > 晚拒绝 > 挂死 |
| Little 定律 | `L = λW`。500/s × 30s = 15,000 并发；P99 算要 45,000，差额就是保费 |
| 队列上限 | `L_q = λ_allow × W_q_target` = 500 × 3 = 1,500；超了立刻拒，不排 |
| 三维准入 | 并发槽位（真容量）+ token 预算（防烧穿）+ 速率（防刷）；漏一个就有洞 |
| 阈值 | 槽位用到 92% 开始排队；队列 1,500 上限；队首等 15s 就拒新请求 |
| 优先级 | 只分三类（P0 交互/P1 批处理/P2 离线），多了必然反转；权重 70/25/5 |
| 抢占 | checkpoint + 重新入队；只抢 <30% 进度的；每个 Run 最多被抢 2 次 |
| 公平 | DRR：`quantum ∝ 权重`，按 Run 个数不按 token；Jain 指数目标 ≥0.92（FIFO 只有 0.31） |
| 分层 | 组间 DRR + 组内 DRR；真正价值是「组级配额」（免费组最多 20%） |
| 分池 | 客服 55 + 编码 126 + 分析 11 = 192 节点，比混合 248 省 23%，且故障不传染 |
| 排队体验 | 位置 + ETA（带抖动）+ 「继续等 / 快速模式」二选一；把降级包装成选择 |
| 扩容 | HPA 有 45s 冷启动；扩容期间必须**先限流**，否则扩出来也救不了 |
| 一句话 | **调度器的工作不是「让所有请求都跑」，是「在容量内让该跑的跑，并诚实地告诉其他人不行」。** |

## 11.9 自检题

1. **容量 500 Run/s，来流 650 Run/s。方案 A：全部排队。方案 B：拒 150/s。请算出 60 秒后两种方案的用户体验差异。**
   > 要点：A：净积压 150/s × 60s = 9,000 排队；排队等待 = 9,000 / 500 = 18s；加上执行 30s，总时长 48s > 多数用户容忍阈值 → 大量主动放弃 + 重试 → λ 进一步上升 → 雪崩。B：500/s 正常服务（P99 约 20s），150/s 拿到 `429 + Retry-After: 2`，2 秒后重试大概率成功。**结论：B 的「成功率」看起来更低（77%），但用户实际完成率更高、P99 更稳、成本更低。** 这就是「早拒绝」的价值。

2. **你的 DRR 权重是 P0:P1:P2 = 70:25:5，但监控显示 P2 的实际 CPU 占用是 40%。哪里错了？**
   > 要点：权重管的是**出队顺序**，不是**资源占用**。P2 的 Run 更长（400s vs 30s），同样的出队次数会占更多资源。修正：① quantum 要按「Run 数 × 平均时长」折算，或者 ② 更简单——**给 P2 独立的池 + `only_when_util_below: 0.7`**，让它只在闲置容量上跑。**别试图用一个权重同时表达「优先级」和「资源份额」，这是两个维度。**

3. **有人提议「队列不设上限，反正内存便宜」。请用三个数字反驳他。**
   > 要点：① 队列 100 万个 Run，每个占 ~2KB 元数据 + 一条挂起的 SSE 连接（~40KB）= 42GB 内存 + 100 万 fd —— 先 OOM 或 fd 耗尽；② 100 万 / 500/s = 2,000 秒 = 33 分钟排队，用户 100% 走光，这 100 万个请求全部白烧（预扣、槽位预留、连接维护）；③ 队列里的请求会重试，λ 翻 2~3 倍，把「过载」变成「雪崩」。**队列的容量不是内存决定的，是「用户愿意等多久」决定的。**

## 11.10 延伸阅读

- 上一章：[10. 一次 Agent 请求的 12 站旅程](10-request-lifecycle.md)
- 下一章：[12. 长任务与持久化执行](12-long-task-durable.md)
- 前置：[4. AgentOS 总架构与贯穿案例「Aurora」](4-architecture-overview.md)（容量基线）
- 多层限流与配额（维度 C 的展开）：[23. 多层限流与配额体系](23-rate-limit-quota.md)
- 降级阶梯与熔断（拒绝之后给什么）：[25. 降级与容错](25-degradation-failover.md)
- 模型侧调度与并发度（优先级穿透）：[14. 调度器设计：GPU/CPU 混合与并发度](14-scheduler-design.md)
- 舱壁与容量压测：[28. 大流量不崩](28-peak-traffic.md)
- 抢占后的 checkpoint 与恢复：[12. 长任务与持久化执行](12-long-task-durable.md)
- 参考：YARN Capacity Scheduler / Fair Scheduler 的队列树与抢占文档、K8s PriorityClass 与 Preemption、vLLM 的 scheduling policy（以官方最新版为准）

---

**交叉引用**：总纲 [0-summary.md](0-summary.md) ｜ 前置 [10-request-lifecycle.md](10-request-lifecycle.md) ｜ 下一章 [12-long-task-durable.md](12-long-task-durable.md)
