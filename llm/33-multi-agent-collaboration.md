# 33. 多 Agent 协作：拓扑、总线与预算

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 运行时、安全、合规与防御 ｜ **编号**：33 / 52
> **本章定位**：多 Agent 是过去两年被吹得最狠、被用得最滥的能力。本章不讲「怎么让两个 Agent 聊天」（那个 30 行代码就够），只讲**在多 Agent 结构下怎么不烧穿预算、不无限循环、不集体失败、还能算清谁花的钱**。
> **读法**：先看 §33.2 的成本放大账本，如果你的场景算不过来这笔账，就**不要上多 Agent**。要上，直接跳到 §33.5 的预算令牌——那是本章的地基。

---

## 33.1 引子：一个子 Agent 烧掉了 $1,900，用时 47 分钟

Aurora 的数据分析 Agent 有一次「小小的架构升级」：把「写 SQL → 执行 → 解读 → 出结论」拆成三个 Agent，用一个 supervisor 调度，理由是「专业分工，效果更好」。

上线第三天，一个企业客户上传了一份 4.2 万行的 CSV，问题很朴素：「找出异常订单并给我结论」。

47 分钟后，这个 Run 结束了。账单：

```
Run r-8f2c…（数据分析 Agent，单会话）
├── supervisor        :  38 步 × 平均 3.1k input  →  118k in / 9k out
├── sql_writer  ×23   :  每次重写 SQL（因为执行报错）  →  412k in / 31k out
├── executor    ×23   :  23 次全表扫描超时，每次 42s
├── interpreter ×14   :  每次把完整结果集塞进上下文   →  890k in / 58k out
└── supervisor 汇总   :  1 次，把上面所有历史再带一遍 →  180k in / 4k out
──────────────────────────────────────────────────────────
合计：1.6M input token / 102k output token ≈ $1.9
单次 Run 平均成本：$0.031。这一单是它的 61 倍。
```

三个问题同时暴露，而且**没有任何一个是「模型不行」**：

1. **预算没有传下去。** supervisor 有 $0.5 的预算上限，但子 Agent 各自新建了「自己的」预算（默认无上限），父级的上限形同虚设。
2. **循环没有终止判据。** `sql_writer` 和 `executor` 之间来回 23 次，每次「换一种写法试试」，而失败原因（表太大、超时）根本不是 SQL 写法问题。
3. **谁花的钱看不见。** 账单上只有「一个 Run $1.9」，你没法在第 3 分钟就发现异常并掐断。

> 🎯 **本章要建立的第一个共识**：
> **多 Agent 不是「能力放大」，是「成本、延迟、失败概率三者同时乘性放大」。** 只有当你换来的质量提升能覆盖这个乘数时，它才划算。而这个乘数，通常比你以为的大得多。

---

## 33.2 五种拓扑：先算乘法，再选结构

### 33.2.1 账本：Aurora 上的实测放大倍数

同样一个「退款政策咨询 + 订单查询 + 生成回复」的任务，做成不同拓扑的成本与延迟（Aurora 内部 A/B，N=2,000）：

| 拓扑 | 结构 | token 放大 | 成本/次 | P50 延迟 | 质量（人工评分 1-5） | 适合什么 |
| --- | --- | --- | --- | --- | --- | --- |
| **单 Agent + 工具**（基线） | 一个循环，工具当函数 | ×1.0 | $0.031 | 11.9s | 4.1 | 90% 的场景。**默认选它** |
| **Supervisor** | 1 调度 + N 执行者，单层 | ×2.6 | $0.082 | 19.4s | 4.4 | 子任务**确实可分**且需要不同工具集 |
| **Handoff（移交）** | 控制权转移，前者退出 | ×1.3 | $0.041 | 13.2s | 4.2 | 场景切换（售前→售后）、专家转接 |
| **Hierarchical（层级）** | 多级 supervisor | ×7.7 | $0.24 | 38.7s | 4.5 | 报告生成、跨领域长任务（**慎用**） |
| **Debate（辩论）** | N 个独立作答 + 裁判 | ×(N+2) | $0.15 (N=3) | 26.1s | 4.3（但方差小） | 高价值单点判断、需要抗幻觉 |
| **Swarm（群体）** | 去中心、黑板共享 | ×5~20（不可预测） | $0.3~1.2 | 不可预测 | 4.0~4.6 | 研究/探索性任务。**生产禁用** |

看这张表要抓两件事：

- **质量从 4.1 到 4.5，成本从 1× 到 7.7×。** 这中间的性价比拐点在 supervisor（×2.6 / +0.3 分）。层级如果没换来 +0.4 分以上，就是纯浪费。
- **延迟也是乘性的**，而且多 Agent 让「并行」变成必需——supervisor 的 19.4s 里，如果 N 个 worker 串行，就是 30s+；必须并行扇出再聚合。

### 33.2.2 判据：什么时候该开子 Agent

一条硬判据，能砍掉 80% 的多 Agent 冲动：

> **如果一个「子任务」能被一次工具调用或一次 LLM 调用完成，就不要给它开子 Agent。**

子 Agent 只在三种情况下划算：

| 该开子 Agent 的理由 | 为什么单 Agent 做不到 | 例子 |
| --- | --- | --- |
| **上下文隔离** | 子任务的中间过程会污染主上下文（几十 K 的工具返回），需要「只带回结论」 | 代码库搜索：读 200 个文件，只回 5 行摘要 |
| **权限隔离** | 子 Agent 需要不同（更窄或更宽）的工具集与凭证 | 财务子 Agent 有退款工具，客服主 Agent 没有 |
| **预算/审计边界** | 需要独立的成本上限、独立的责任归属、独立的审计记录 | 外包/第三方 Agent：只能花 $0.1，必须单独出账 |

不在这三条里的「分工」，基本都是**给同一个模型加了一层 RPC 开销**。

### 33.2.3 五种拓扑的结构与失败模式

| 拓扑 | 主要失败模式 | 终止性 | 观测难度 |
| --- | --- | --- | --- |
| 单 Agent | 步数爆炸、上下文爆窗 | ✅ 容易（max_steps） | 低 |
| Supervisor | supervisor 上下文膨胀（每次汇总都带全部子结果）；调度抖动（反复重派） | ✅ 可控 | 中 |
| Handoff | 踢皮球（A→B→A）；移交时丢上下文；责任不清 | ⚠️ 需要交接计数 | 中 |
| Hierarchical | **预算乘性放大**；延迟叠加；调试地狱（错误在三层之间丢） | ⚠️ 需要每层硬闸 | 高 |
| Debate | 趋同（N 个 Agent 都错）；裁判偏见；成本线性爆炸 | ✅ 容易 | 低 |
| Swarm | **无限对话**；终止性无法证明；状态竞争 | ❌ 不可证 | 极高 |

> ⚠️ **生产环境里唯一「禁止上线」的是 Swarm**。不是因为它没用，而是因为**你无法向 SRE 证明它会停**。任何需要「证明一定终止」的系统，都不能靠概率收敛。

---

## 33.3 结构图：拓扑 + 预算令牌的流转

```
                    ┌───────────────────────────────────────────────┐
                    │ Run 根预算  B0 = $0.50 / 120k token / 90s      │
                    │ （来自租户配额，第 23 章）                      │
                    └───────────────────┬───────────────────────────┘
                                        │ Reserve(0.4)  ← 预留，不是花掉
                        ┌───────────────▼───────────────┐
                        │ supervisor（B1 = $0.40）       │
                        │  自己的 LLM 调用从 B1 扣        │
                        └───┬───────────┬───────────┬───┘
              Reserve(0.1)  │           │ 0.1       │ 0.1
                    ┌───────▼──┐  ┌─────▼────┐  ┌───▼──────┐
                    │sql_writer│  │executor  │  │interpreter│
                    │ B2=$0.10 │  │ B3=$0.10 │  │ B4=$0.10  │
                    └────┬─────┘  └────┬─────┘  └────┬──────┘
                         │ Commit(0.06)│ Commit(0.09)│ Commit(0.02)
                         │ Release(0.04)│Release(0.01)│Release(0.08)
                         ▼             ▼             ▼
                    ┌──────────────────────────────────────────┐
                    │ 回收池：0.13 回到 B1（父级可再用，兄弟可复用）│
                    │ 剩余 0.27 在 Run 结束时 Release 回 B0        │
                    └──────────────────────────────────────────┘

   铁律（三条，缺一不可）：
   ① 预留制：调模型前按 max_tokens 上限 Reserve，返回后 Commit 实际值
   ② 不可透支：子级总额 ≤ 父级可用额，父级永远不能「借」给子级
   ③ 级联回收：子级结束/超时/崩溃 → 未 Commit 的部分自动回父级
```

**为什么必须「预留（Reserve）」而不是「事后记账」？** 因为并发。supervisor 并行扇出 5 个 worker，每个 worker 在花之前都查「我还有钱吗」，5 个都说有，然后一起花超——这是**经典的检查后使用（TOCTOU）**。预留把「检查」和「占用」合并成一次原子操作，等价于数据库的行锁。**这就是 Java 里 `tryAcquire` 与 `get then set` 的区别。**

---

## 33.4 消息总线与共享状态：选错了会很难受

多 Agent 之间怎么通信，只有三个真实选项：

| 机制 | 语义 | 一致性 | 适合 | 不适合 | Aurora 里对应 |
| --- | --- | --- | --- | --- | --- |
| **共享状态（Blackboard）** | 所有 Agent 读写同一份结构化状态（Run 的 state 对象） | 强（单写者 + 乐观锁）或最终一致 | 任务有明确阶段、需要「看到全局」 | 高并发写、Agent 数量多 | LangGraph 的 state channel |
| **消息总线（Message Bus）** | Agent 之间发消息，异步、解耦 | 最终一致（至少一次） | 事件驱动、长流程、跨服务 | 需要「读到最新」的强一致场景 | Kafka/NATS + 内部事件 |
| **直接调用（RPC/函数）** | A 同步调 B，等结果 | 同步 | 简单、可预测、延迟敏感 | 深层嵌套（延迟叠加）、失败传播 | 子 Agent 当工具调 |

三条工程判断：

1. **默认用共享状态，不要用消息总线。** 一个 Run 内的多个 Agent 是「同一个逻辑事务的多个执行者」，它们需要看到一致的状态。用消息总线做进程内协作，你会自己造出一个分布式一致性问题，而这个问题你本来不需要面对。
2. **跨 Run / 跨服务才上总线。** 比如「长任务完成后通知另一个系统」「多个 Run 共享一个资源池」。这时候至少一次语义 + 幂等消费才成立（第 13 章）。
3. **共享状态必须「单写者 + 版本号」**。多个 Agent 并发写同一个 state，必然覆盖。做法：每个 state 字段有 owner，非 owner 只能提议（propose）不能写；所有写入带 `expected_version`，冲突就重读重试。

```yaml
# agent/topology/cs-supervisor.yaml —— 拓扑即配置（不要硬编码成 Go 的 if-else）
kind: supervisor
version: 14
entry: triage
budget:
  total_tokens: 120000
  total_cost_usd: 0.50
  wall_clock: 90s
  # 关键：子级默认额度 = min(父可用 × share, cap)
  child_default: {share: 0.25, cap_usd: 0.10, cap_tokens: 30000}
state:
  store: run_state        # 共享状态，单写者 + 版本号
  fields:
    - {name: findings, owner: worker, mode: append, max_items: 20}
    - {name: plan,     owner: supervisor, mode: replace}
    - {name: final,    owner: supervisor, mode: replace}
agents:
  - id: triage
    role: 分类与拆解
    tools: []
    budget: {cap_usd: 0.02}
    max_steps: 3
  - id: sql_writer
    role: 生成 SQL
    tools: [schema.read]
    budget: {cap_usd: 0.08}
    max_steps: 4
    on_budget_exhausted: degrade_to_template   # 不是重试
  - id: executor
    role: 执行查询
    tools: [sql.run]
    budget: {cap_usd: 0.06}
    max_steps: 4
    timeout: 20s
  - id: interpreter
    role: 解读结果
    tools: []
    budget: {cap_usd: 0.10, cap_tokens: 20000}
    max_steps: 2
fanout:
  mode: parallel
  max_concurrency: 3
  join: first_success_then_cancel_rest   # 别等全部，省一半钱
terminate:
  max_total_steps: 24
  max_depth: 2
  loop_detection: {repeat_threshold: 3, similarity: 0.95}
```

---

## 33.5 预算在 Agent 间分配与回收（本章地基）

### 33.5.1 预算令牌的数据结构

预算不是一个数字，是**一棵带租约的树**。每个节点有三个量：

- `cap`：这个节点的硬上限（由父级授予，不可改）
- `reserved`：已预留但还没结算的部分（并发安全的关键）
- `committed`：已经实际花掉的

`available = cap - reserved - committed`。

```go
// budget/token.go —— 树形预算令牌：Reserve / Commit / Release 三段式
package budget

import (
	"context"
	"errors"
	"sync"
	"time"
)

var (
	ErrBudgetExhausted = errors.New("budget exhausted")
	ErrOverCap         = errors.New("exceeds node cap")
	ErrLeaseExpired    = errors.New("lease expired")
)

type Amount struct {
	Tokens int64
	Micro  int64 // 百万分之一美元，整数运算避免浮点误差（和第 27 章账本一致）
}

func (a Amount) Add(b Amount) Amount { return Amount{a.Tokens + b.Tokens, a.Micro + b.Micro} }
func (a Amount) Sub(b Amount) Amount { return Amount{a.Tokens - b.Tokens, a.Micro - b.Micro} }
func (a Amount) LTE(b Amount) bool   { return a.Tokens <= b.Tokens && a.Micro <= b.Micro }

type Node struct {
	ID       string
	Cap      Amount
	Reserved Amount
	Committed Amount
	Lease    time.Duration // 预留租约，超时自动回收（防子 Agent 崩溃泄漏）

	mu       sync.Mutex
	leases   map[string]time.Time // leaseID -> 到期时间
	parent   *Node
	children map[string]*Node
	closed   bool
}

// Spawn 创建子节点：cap = min(父可用 × share, 请求的 cap)
// ★ 必须是「从父级预留」而不是「凭空发钱」
func (n *Node) Spawn(ctx context.Context, id string, share float64, cap Amount) (*Node, error) {
	n.mu.Lock()
	defer n.mu.Unlock()
	if n.closed {
		return nil, ErrBudgetExhausted
	}
	avail := n.Cap.Sub(n.Reserved).Sub(n.Committed)
	want := Amount{
		Tokens: int64(float64(avail.Tokens) * share),
		Micro:  int64(float64(avail.Micro) * share),
	}
	if cap.Tokens > 0 && cap.Tokens < want.Tokens {
		want.Tokens = cap.Tokens
	}
	if cap.Micro > 0 && cap.Micro < want.Micro {
		want.Micro = cap.Micro
	}
	if want.Tokens <= 0 || want.Micro <= 0 {
		return nil, ErrBudgetExhausted
	}
	n.Reserved = n.Reserved.Add(want) // 父级立刻占用，子级才有钱
	c := &Node{
		ID: id, Cap: want, Lease: 30 * time.Second,
		leases: map[string]time.Time{}, parent: n, children: map[string]*Node{},
	}
	n.children[id] = c
	return c, nil
}

// Reserve 在「发起模型调用之前」预扣：额度按 max_tokens 上限算，宁多不少
func (n *Node) Reserve(ask Amount) (leaseID string, err error) {
	n.mu.Lock()
	defer n.mu.Unlock()
	if n.closed {
		return "", ErrBudgetExhausted
	}
	if !ask.LTE(n.Cap.Sub(n.Reserved).Sub(n.Committed)) {
		return "", ErrBudgetExhausted
	}
	leaseID = newID()
	n.Reserved = n.Reserved.Add(ask)
	n.leases[leaseID] = time.Now().Add(n.Lease)
	return leaseID, nil
}

// Commit 结算：把预留转成实际消耗，多留的部分自动还回去
func (n *Node) Commit(leaseID string, actual Amount) error {
	n.mu.Lock()
	defer n.mu.Unlock()
	exp, ok := n.leases[leaseID]
	if !ok || time.Now().After(exp) {
		return ErrLeaseExpired // 超时的预留已被回收，实际花费走「事后补记 + 审计」
	}
	delete(n.leases, leaseID)
	// 这里只做「额度」记账；真正的金额流水在计费账本（第 27 章）
	n.Committed = n.Committed.Add(actual)
	return nil
}

// Release 放弃（未使用 / 失败 / 取消）：把预留退回去，兄弟节点可复用
func (n *Node) Release(leaseID string) {
	n.mu.Lock()
	defer n.mu.Unlock()
	if _, ok := n.leases[leaseID]; !ok {
		return
	}
	delete(n.leases, leaseID)
	// 预留额从 Reserved 里减去需要知道原始 ask，实际实现用一个 lease 结构体存它
}
```

> 🩸 **三个必踩的坑（上面的代码就是被这三个坑磨出来的）**：
> ① **按 `max_tokens` 预留而不是按「预计用量」**。并按预计用量预留，5 个并发 worker 各估 2k，实际各花 8k，直接超支 4 倍。
> ② **必须有租约（Lease TTL）**。子 Agent 崩溃 / Pod 被杀 / 上下文取消时如果没人 Release，额度就永久泄漏——跑到第 20 步时整个 Run 显示「没钱了」，但实际只花了 30%。
> ③ **金额用整数微美元（Micro），不用 float64**。浮点累加 1 万次会有可见误差，而这是要进账单的数（和第 27 章同一条纪律）。
>
> 分布式部署下这套结构要落到 Redis：`Spawn`/`Reserve` 用 Lua 脚本保证原子（`HGETALL` + 判断 + `HINCRBY` 在一个脚本内完成），租约用 ZSET 存到期时间，后台扫描回收。

### 33.5.2 预算耗尽不是错误，是「降级信号」

这是多 Agent 设计里最容易被写错的一行：`if budgetExhausted { return err }`。

正确姿势是**每一级都有明确的「没钱了怎么办」**：

| 耗尽位置 | 处置 | 用户看到 | 不能做什么 |
| --- | --- | --- | --- |
| 子 Agent（worker） | 返回**已有部分结论 + `partial: true`**，父级决定能否拼出答案 | 正常答案（可能略简） | ❌ 不能自己「再试一次」 |
| supervisor | 停止扇出，用已收集的 findings 直接汇总；若不足则走降级阶梯（第 25 章） | 「我查到了 A、B，C 需要更多时间」 | ❌ 不能申请提额（提额等于没有上限） |
| 根 Run | 中止 + 返回部分结果 + 账单明示 + 建议（「这个问题建议开启深度分析模式」） | 明确的部分答案 + 建议 | ❌ 不能静默截断 |

**关键原则：预算耗尽必须在 trace 里是一条事件，不是一个 error。** 因为它不是故障，是设计内的路径。把它当 error 打日志，你的错误率看板会被它污染，真正的问题就淹没了。

### 33.5.3 一个具体的分配策略：按「信息增量」而不是按「平均分」

朴素做法是平均分（每个子 Agent 1/N）。问题是：有些子任务 3 步就完了，有些要 20 步；平均分会导致「快的浪费、慢的不够」。

Aurora 用的策略（简单有效）：

```
初始分配：按 supervisor 的预估复杂度分（LLM 输出的 plan 里带 priority）
第 1 轮结束后：按「实际消耗 / 产出信息量」重分配
  - 未用完额度的子 Agent：释放 50%，留给后续
  - 已耗尽且 `partial` 的子 Agent：从回收池追加一次（只追加一次）
  - 连续 2 次耗尽的子 Agent：不再追加，标记 degraded
上限：任何一个子 Agent 不得超过父级总预算的 40%（防单点烧穿）
```

这一条「40% 上限」是硬规则——**它直接防住了 §33.1 的事故**：interpreter 本来可以花掉 60% 的预算。

---

## 33.6 失败隔离、循环检测、终止保证

### 33.6.1 失败隔离：一个子 Agent 挂了不能拖垮整个 Run

| 失败类型 | 传播方式（如果不管） | 隔离手段 |
| --- | --- | --- |
| 子 Agent 超时 | 父级一直等，最终整 Run 超时 | 每子 Agent 独立 `context.WithTimeout`；超时即 `partial` |
| 子 Agent 报错 | 父级重试 → 重试风暴（第 25 章） | 错误分类：可重试（网络/限流）vs 不可重试（schema/权限）；最多 1 次 |
| 子 Agent 输出污染 | 一条垃圾结论带偏整个汇总 | 结构化输出契约 + 父级校验（schema + 置信度）；不合格直接丢弃 |
| 子 Agent 占满并发 | 其他租户的 Run 排队 | 子 Agent 并发计入**同一租户**的并发额度（不能「开小号」绕过限流） |
| 子 Agent 写出共享状态 | 覆盖其他 Agent 的结果 | 单写者 + 字段 owner + 版本号 |

> 💡 **一条容易漏的**：子 Agent 的**工具调用**也要单独设预算与超时。Aurora 的事故里，`executor` 的 23 次全表扫描各跑 42 秒——工具层的超时是 60s（因为「大查询要宽容」）。**一个任务的超时预算应该自上而下分配，而不是每个工具各自定一个「宽容值」。**

### 33.6.2 循环检测：三种信号，缺一种就漏

```go
// guard/loop.go —— 多 Agent 循环检测：结构 + 语义 + 信息量
type LoopGuard struct {
	seen      map[string]int    // key = agentID + tool + argsHash
	repeat    int               // 阈值 3
	embs      []float32         // 最近 N 条消息的嵌入（N=8）
	simThresh float64           // 0.95
	novelty   *NoveltyTracker   // 新增实体/决策计数
}

func (g *LoopGuard) Check(step Step) (stop bool, reason string) {
	// 信号 1：结构重复 —— 同一个 Agent 用同样的参数调同样的工具
	k := step.AgentID + "|" + step.Tool + "|" + step.ArgsHash
	g.seen[k]++
	if g.seen[k] >= g.repeat {
		return true, "structural_repeat"
	}

	// 信号 2：语义复读 —— A 说的话和 B 三分钟前说的话几乎一样（踢皮球）
	if len(g.embs) >= 2 {
		if cos(g.embs[len(g.embs)-1], step.Embedding) > g.simThresh {
			return true, "semantic_pingpong"
		}
	}

	// 信号 3：零信息增量 —— 说了很多话，但没有新实体、没有新决策、没有新工具结果
	if step.Kind == StepLLM && !g.novelty.HasDelta(step.Output) {
		if g.novelty.ZeroStreak() >= 2 {
			return true, "no_information_gain"
		}
	}
	return false, ""
}
```

| 信号 | 抓什么 | 漏了会怎样 |
| --- | --- | --- |
| **结构重复**（工具+参数哈希） | SQL 写法换汤不换药、反复查同一张表 | 最常见，占 Aurora 循环的 71% |
| **语义复读**（嵌入相似度） | supervisor 与 worker 互相推诿、debate 里两个 Agent 复述 | 表现为「说得很多、没有进展」，账单线性涨 |
| **零信息增量** | 每步都在「总结一下」，但没有新事实 | 最隐蔽，能跑 30 步你都看不出异常 |

### 33.6.3 终止保证：三道闸 + 一个单调量

任何多 Agent 图都必须能证明「一定会停」。用四件事保证：

```
闸 1（硬）：max_total_steps        —— 全局步数上限（Aurora: 24）
闸 2（硬）：预算树耗尽              —— 单调递减，不可能自愈
闸 3（硬）：wall_clock 上限         —— 整个 Run 的墙钟上限（Aurora: 90s）
闸 4（软）：深度上限 max_depth=2    —— 禁止递归开子 Agent（防层级爆炸）
单调量：每步必须使「剩余待办数」严格减少，或显式声明「本轮无进展」
```

第四条的工程意义：**给每个 Run 维护一个 `remaining_subtasks` 计数器，只减不增。** 如果某一步既没减少待办、又没产出新信息，就要计入 `no_progress_steps`；连续 3 次 → 终止。这条规则把「看起来在干活」的循环也拦住了。

> ⚠️ **不要用「LLM 判断是否该停」作为唯一的终止条件。** 模型在循环里会倾向于「再试一次」。终止必须由**外部计数器**决定，模型只能提议停。

---

## 33.7 协作的观测：谁花的钱

多 Agent 的 trace 不是平的，是**树的**。而且成本必须能落到每个 Agent、每个子任务上。

```
Trace (run_id=r-8f2c)
└── agent span  supervisor      cost=$0.31  tokens=118k/9k   steps=38
    ├── agent span  sql_writer  cost=$0.21  tokens=412k/31k  steps=23  ⚠ loop_suspect
    │   └── tool span sql.run   ×23  p95=42s  errors=23
    ├── agent span  executor    cost=$0.00  （纯工具）
    │   └── tool span sql.run   ×23  ...
    └── agent span  interpreter cost=$1.38  tokens=890k/58k  steps=14  🔴 budget_hit
        └── llm span  model=m-large  ctx=64k  （每次带入完整结果集）
```

必采字段（缺一个就查不出问题）：

| 字段 | 为什么必须有 |
| --- | --- |
| `agent_id` / `parent_agent_id` / `depth` | 没有父子关系，你看到的是 5 个孤立的调用，不知道谁派的 |
| `budget.granted` / `budget.committed` / `budget.released` | 才能回答「额度去哪了」 |
| `steps` / `subtask_id` | 才能按子任务聚合成本，而不是按 Agent |
| `share_of_run_cost` | 看板一行：**哪个 Agent 吃掉了 70% 的钱** |
| `termination_reason` | `completed` / `budget_exhausted` / `loop_detected` / `timeout` / `cancelled`——这五个值的分布，就是多 Agent 的健康度 |

**成本归因表（ClickHouse 侧）**：

```sql
-- 每个 Run 内按 Agent 聚合，用于「谁烧的钱」看板与异常告警
SELECT
  run_id,
  agent_id,
  parent_agent_id,
  depth,
  count()                                   AS llm_calls,
  sum(tokens_in)                            AS tokens_in,
  sum(tokens_out)                           AS tokens_out,
  round(sum(cost_usd), 4)                   AS cost_usd,
  round(sum(cost_usd) / any(total_cost_usd), 3) AS share,
  any(termination_reason)                   AS term
FROM agent_spans
WHERE ts >= now() - INTERVAL 1 HOUR
GROUP BY run_id, agent_id, parent_agent_id, depth
ORDER BY cost_usd DESC
LIMIT 20;
```

告警规则（Aurora 在生产上真正会响的只有三条）：

| 规则 | 阈值 | 为什么这么设 |
| --- | --- | --- |
| 单 Run 成本 > P99.9 | > $0.35 | 单点异常，值得看一眼 |
| 单 Agent 占 Run 成本 > 40% | > 0.40 | 违反分配硬规则，说明预算没生效 |
| `loop_detected` 占比 > 2% | > 2% | 拓扑或提示词退化的早期信号 |

---

## 33.8 Java / 分布式经验对照

| 你的 Java 经验 | 多 Agent 里对应什么 | 会咬人的地方 |
| --- | --- | --- |
| 线程池 + `Semaphore` | 并发限额 | 但 Agent 会「自己决定再开几个」，并发不再是静态的；必须**把子 Agent 的并发计入父租户的额度** |
| `CompletableFuture` 扇出/聚合 | supervisor 并行扇出 | 超时与取消的传播容易写错；一个 future 超时不能只 `cancel`，还要 `Release` 预算 |
| Saga / 补偿事务 | 多 Agent 的失败回滚 | Agent 的「补偿」不是技术操作，往往是「再跑一个 Agent 解释一下」，成本要预留在预算里 |
| Temporal 子工作流 | 子 Agent | Temporal 的子工作流有独立历史和重试策略，**但预算不会自动传递**——必须显式把它作为输入传下去（Java 里用 `Workflow.getInfo()` + 自定义 budget 对象） |
| Akka Actor 监督树 | 层级拓扑 | Actor 的「父监督子」语义很好用，但 Agent 的失败模式是「输出是垃圾」而不是「抛异常」，监督策略要按结果质量判定 |
| 分布式锁 / 乐观锁 | 共享状态写入 | 用版本号，不要用悲观锁——LLM 调用是秒级的，持锁会堵死 |
| 熔断器 | 子 Agent 熔断 | 熔断后不能「换一个 Agent 重试」，那等于绕过熔断；要直接降级 |

> 🩸 **最容易翻车的架构决定**：让子 Agent **自己创建子 Agent**（递归）。你做的不再是一棵树，而是一个「可能无限的进程树」——而且每一层都觉得自己只花了「一点点」。**深度硬上限是唯一可靠的解法。**

---

## 33.9 开源坐标

| 能力 | LangGraph | AutoGen | CrewAI | 缺什么 |
| --- | --- | --- | --- | --- |
| 拓扑表达 | 图 + 条件边，`Send` API 做扇出 | 对话式 group chat | Role/Task/Crew 声明式 | 都能表达，**但预算不是一等公民** |
| 共享状态 | `StateGraph` + channel + reducer + checkpoint | 消息列表 | Task 输出传递 | LangGraph 最完善（有 reducer 处理并发写） |
| 循环检测 | 靠 `recursion_limit`（步数） | `max_consecutive_auto_reply` | `max_iter` | **都只有步数闸**，没有语义/信息量信号 |
| 预算分配 | ❌ 无 | ❌ 无 | ❌ 无 | **必须自研**（§33.5） |
| 终止保证 | ✅（步数） | ⚠️（可配但容易绕） | ⚠️ | Swarm 类拓扑无法证明 |
| 观测（谁花的钱） | 需要接 LangSmith/Langfuse，按 span 聚合 | 自带少量 | 少量 | **「子 Agent 成本占比」这张表要自己做** |

> **一句话**：**开源框架给你拓扑的表达能力，把「资源治理」全部留给你。** 而多 Agent 的生产事故 90% 出在资源治理上——这跟全书对编排框架 12% 的判断完全一致。

---

## 33.10 事故清单

- [ ] **事故 A：子 Agent 烧穿会话** —— 现象：单 Run $1.9（均值 61 倍）。→ 根因：子 Agent 新建独立预算，父级 cap 不生效。→ 兜底：预算树 Reserve/Commit/Release + 单 Agent ≤40% 硬规则。
- [ ] **事故 B：两 Agent 踢皮球 23 轮** —— 现象：延迟 47 分钟，用户早就走了。→ 根因：只有 `max_steps`，没有语义复读检测。→ 兜底：三信号循环检测 + `no_progress_steps` 连续 3 次终止。
- [ ] **事故 C：并发扇出超支 4 倍** —— 现象：并行 5 个 worker 都说「还有额度」，一起花超。→ 根因：先查后花（TOCTOU）。→ 兜底：按 `max_tokens` 上限 Reserve，原子化。
- [ ] **事故 D：worker 崩溃导致额度泄漏** —— 现象：Run 跑到一半报「预算耗尽」，但实际只花了 30%。→ 根因：异常路径没有 Release。→ 兜底：租约 TTL + 后台回收扫描 + `defer Release()`。
- [ ] **事故 E：共享状态互相覆盖** —— 现象：三个 worker 的 findings 只剩最后一个。→ 根因：并发写同一个字段。→ 兜底：字段 owner + `append` reducer + 版本号重试。
- [ ] **事故 F：递归开子 Agent** —— 现象：一次 Run 开了 200 多个 Agent，账单 $14。→ 根因：没有 `max_depth`。→ 兜底：深度硬上限 + 子 Agent 创建需要父级显式 Spawn（子 Agent 无权限自建）。
- [ ] **事故 G：子 Agent 绕过限流** —— 现象：某租户通过多 Agent 把并发放大 10 倍，挤爆其他租户。→ 根因：子 Agent 并发没计入租户配额。→ 兜底：并发计量挂在租户维度，子 Agent 共享同一桶（第 23 章）。

---

## 33.11 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 成本乘数 | 单 Agent 1× / handoff 1.3× / supervisor 2.6× / hierarchical 7.7× / swarm 不可预测 |
| 该不该拆 | 子任务若能被一次调用完成，就别开子 Agent；只在上下文隔离/权限隔离/预算边界三种情况下拆 |
| Swarm | 生产禁用——不是因为没用，是因为**无法证明它会停** |
| 预算令牌 | 树形 + Reserve(按 max_tokens)/Commit/Release 三段 + 租约 TTL 回收 |
| 硬规则 | 单个子 Agent ≤ 父级预算 40%；子级总额 ≤ 父级可用；父级不可借 |
| 耗尽语义 | 预算耗尽**不是 error，是降级信号**；每级都要定义「没钱了怎么办」 |
| 金额 | 用整数微美元，不用 float64（要进账单） |
| 循环检测 | 结构重复 + 语义复读 + 零信息增量，三种信号缺一漏一 |
| 终止保证 | 步数 + 预算 + 墙钟三道硬闸 + `max_depth` + `remaining_subtasks` 单调递减 |
| 通信 | 默认共享状态（单写者+版本号）；跨 Run 才上总线 |
| 观测 | trace 是树：`agent_id/parent/depth` + `share_of_run_cost` + `termination_reason` |
| 一句话 | **多 Agent 的难点不是「怎么让它们协作」，而是「怎么让它们停下来、并且知道谁花的钱」。** |

## 33.12 自检题

1. **你给 supervisor 设了 50 万 token 上限，为什么子 Agent 还是能烧穿？**
   > 要点：① 子 Agent 如果新申请预算（而不是从父级 `Spawn`），父级上限对它无效；② 即使从父级分，若按「预计用量」而非 `max_tokens` 预留，并发扇出会 TOCTOU 超支；③ 若没有租约回收，崩溃的子 Agent 会泄漏额度导致「假性耗尽」。正确做法：树形 Reserve/Commit/Release + 租约 + 单子 ≤40%。

2. **「循环检测」为什么不能只靠「同一步重复 N 次」？**
   > 要点：Agent 的循环往往**形式不同内容相同**——SQL 每次换写法、prompt 每次换措辞，结构哈希永远不重复，但语义在原地打转。所以要补「语义复读（嵌入相似度）」和「零信息增量（新实体/新决策计数）」。反之只有语义也不行：正常的总结步骤本来就和上文相似，会误伤。

3. **预算耗尽时返回 error，会在生产上造成什么问题？**
   > 要点：① 错误率指标被污染，真实故障被淹没；② 上游会触发重试（重试必然也失败），造成无意义的排队与延迟；③ 用户看到的是「服务异常」而不是「部分答案 + 建议」，体验与信任双输。正确做法：把它当一条**事件**，返回 `partial` 结果 + 明确说明，并在 trace 里标记 `termination_reason=budget_exhausted`。

## 33.13 延伸阅读

- 上一章：[32. 防御工程：防破解、防薅羊毛、防蒸馏](32-defense-hardening.md)
- 下一章：[34. 人机协同（HITL）：审批、中断与恢复](34-hitl-approval.md)
- 总纲：[0-summary.md](0-summary.md)
- 相关：[12. 长任务与持久化执行](12-long-task-durable.md)（子 Agent 的 checkpoint 与重放）
- 相关：[23. 多层限流与配额体系](23-rate-limit-quota.md)（子 Agent 并发必须计入租户桶）
- 相关：[27. 计费的可靠性](27-billing-reliability.md)（微美元整数记账）
- 相关：[39. 可观测层](39-observability.md)（agent span 与成本归因）
- 外部参考：LangGraph 多 Agent 文档（`Send`、`StateGraph` reducer、`recursion_limit`）、AutoGen（`GroupChat`、`max_consecutive_auto_reply`）、CrewAI（`Crew`/`Task`），以各自官方文档最新版为准
