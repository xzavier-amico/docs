# 27. 计费的可靠性：一个不能算错账的系统

> **状态**：`已完成（初稿）` ｜ **所属**：第五部分 · 稳定性与成本：大流量为什么不崩 ｜ **编号**：27 / 52
> **本章定位**：全书唯一一章「算错账的后果直接是钱」的内容。它把 token 计量、预扣、复式账本、对账、修复、结算、税率串成一条**可审计的资金流水线**，而不是一张统计报表。
> **读法**：想抄东西的，直接看 §27.5 的三张表（account / transaction / entry）和 §27.7.3 的对账任务。如果你的代码里金额还是 `float64` 或 `BigDecimal` 直接塞 MySQL `FLOAT` 列，先跳到 §27.5.4，看完再回来。

---

## 27.1 引子：一次 180 万的差额，和一句答不上来的问题

2026 年 3 月 1 日，Aurora 财务关 2 月的账。

| 口径 | 金额 |
| --- | --- |
| 账本「模型成本」科目累计 | ¥7,180 万 |
| 三家供应商实际发票合计 | ¥7,360 万 |
| 差额 | **¥180 万（2.5%）** |
| 按原计划毛利率 32% 算 → 实际 | **29.5%** |

2.5 个点的成本差，从 32% 的毛利里扣掉，等于**毛利少了 7.8%**。CEO 只问了一句：「钱去哪了？」

查了 11 天，三个根因，全是纯工程问题：

1. **缓存折扣算错，持续 5 天。** 价格表回滚时把 `cache_read` 的倍率从 0.1× 改回了 1.0×，影响 20% 流量。缓存读按全价记 → 成本虚高 **¥107 万**。
2. **汇率口径错。** 成本按月初中间价 7.10 折算，实际银行结算 7.24；美元成本占 60% → 少记 **¥68 万**。
3. **漏单 + 重单。** 3.2 万条 Run 因为流式中断没结算（¥9,600），4.1 万条因为幂等键带了 `region` 前缀被重复结算（¥1.2 万）。

前两条是 ¥175 万。但真正让 CTO 后背发凉的是第三条——**7.3 万条对不上的流水**。因为审计师问的下一个问题是：

> 「你怎么证明**剩下那 7,900 万条是对的**？」

答不上来。因为 Aurora 的「账本」其实是两张表：`usage_record`（要统计的）和 `tenant_balance`（一个 `UPDATE ... SET balance = balance - ?` 的余额列）。**没有借贷分录，没有平衡校验，没有不可变流水，余额被 `UPDATE` 过多少次没人知道。**

> 🎯 **本章的核心命题**：
> **计费系统不是监控系统，是资金系统。** 统计错了可以重算，资金错了只能冲正；而冲正需要证据链。所以它必须按支付系统的标准建——**复式记账 + 不可变流水 + 幂等键 + 三方对账**，一样不能少。
> 你不需要做到 Stripe 那个程度，但你**必须做到「任何一笔钱都能追到一条不会变的流水，且所有流水的借贷必然平衡」**。这是审计的最低线，也是你自己排障的最低线。

---

## 27.2 四类账务错误：每一类的代价完全不同

先把「算错账」拆细。它们的发现路径、修复代价、责任归属完全不同，不能用一套兜底策略。

| 错误类型 | 现象 | 单笔金额量级 | 谁能发现 | 发现时滞 | 修复方式 | 真实代价 |
| --- | --- | --- | --- | --- | --- | --- |
| **漏单**（少收/少记） | usage 有记录，账本无分录 | ¥0.30/次 | **只有对账** | T+1 ~ T+30 | 补记分录（可追溯） | 收入黑洞，且客户已消费，追不回来 |
| **重单**（多收） | 一次 Run 结算 N 次 | ¥0.30×N | 客户 / 对账 | 当天~次月 | 冲正 + 退款 + 道歉 | 客诉、信任损失、可能的监管投诉 |
| **超卖**（预扣不足） | 余额被扣成负数 | ¥90/账号 | 月末结算 | 最多 30 天 | 无法追回，只能记坏账 | 薅羊毛，规模化后是六位数 |
| **错价**（折扣/汇率/税率） | 单价或倍率用错 | 系统性 | 审计 / 财务 | 月末~季度 | 全量重算 + 客户对账单调整 | 影响面最大，一次能打到 ±3% 毛利 |

**关键的数量级判断**（这张表决定了你该把工程预算花在哪）：

```
单笔错误金额 ≈ ¥0.30（一次 Run）
日均 Run      = 800 万
────────────────────────────────────────────
如果错误率 0.01%：800 笔/天 = ¥240/天 → 一年 ¥8.7 万   ← 可以忍，但要能查
如果错误率 1%   ：8 万笔/天 = ¥2.4 万/天 → 一年 ¥876 万 ← 不能忍
如果系统性错价  ：2.5% → ¥180 万/月                      ← 直接吃掉毛利
```

> 💡 **所以计费系统的工程重点不是「把每一笔算准」**（单笔 ¥0.30，四舍五入都不影响），**而是三件事**：
> ① **系统性错误要在 1 小时内被发现**（错价、汇率、折扣）；
> ② **每一笔都要可追溯、可重放**（漏单能补，重单能冲）；
> ③ **账本自身要能证明自己是平的**（借贷平衡 + 哈希链）。
> 前两条靠对账，第三条靠复式记账。这就是本章剩下 90% 的内容。

---

## 27.3 token 计量：把「用了多少」变成「欠多少钱」

计量是计费的输入。输入错了，后面账本再完美也是错的账。

### 27.3.1 六个必须分开记的计量字段

一次 LLM 调用，最少要记六个数字。**混在一起就再也拆不开了**，而它们的单价能差 10 倍。

| 字段 | 来源 | 计价倍率（示例） | 坑 |
| --- | --- | --- | --- |
| `input_tokens` | 请求侧自算 or `usage.prompt_tokens` | 1.0× | 工具定义、system prompt 也算进去，容易漏 |
| `cache_write_tokens` | `usage.cache_creation_input_tokens`（Anthropic） | 1.25×（5 分钟 TTL） | 写缓存比不写还贵，短会话反而亏 |
| `cache_read_tokens` | `usage.cache_read_input_tokens` / `prompt_tokens_details.cached_tokens` | 0.1× ~ 0.5× | **折扣率因厂商而异，必须进价格表版本** |
| `output_tokens` | 流式累积 or `usage.completion_tokens` | 1.0×（基准） | 断流时厂商可能不计费，但你已计成本 |
| `reasoning_tokens` | `usage.completion_tokens_details.reasoning_tokens` | 通常并入 output 计价 | 不可见，但**能占 output 的 60%~80%** |
| `tool_calls` / `tool_units` | 网关侧计数 | 按次 / 按时长 | 与 token 成本科目不同，必须分账 |

> ⚠️ **最容易出事的一条**：`cache_read` 的折扣率。Anthropic 的 prompt caching 大致是「写缓存 1.25×、读缓存 0.1×」（5 分钟 TTL，更长 TTL 倍率更高）；OpenAI 的 cached input 是「按输入价的 50% 计费」且自动生效。**这两家的倍率差 5 倍**，如果你的价格表只有一列 `input_price`，你迟早会在混用两家模型时算错。价格表必须是 `(provider, model, price_version) → 各字段倍率` 的四维结构。**具体数值以各厂商官方价格页最新版为准，并且必须能热更新 + 带生效时间。**

### 27.3.2 流式怎么计量：三种策略，选错就是漏单

流式下「用了多少 token」在**流结束时才知道**，但用户已经看到了内容。这是计费系统里唯一一个真正棘手的地方。

```
时间轴 ──────────────────────────────────────────────────────▶

请求发出        首 token      持续吐字（每 20~40ms 一个 chunk）      流结束
  │               │           │  │  │  │  │  │  │  │  │            │
  ├── 预扣 ──────▶│           │  │  │  │  │  │  │  │  │            │
  │  (估算上限)   │           │  │  │  │  │  │  │  │  │            │
  │               └───────────┴──┴──┴──┴──┴──┴──┴──┴──┴────────────┤
  │                                                                  │
  │  ① 自算 tokenizer：每收一个 chunk 就 +=1       ← 随时可得，可能与厂商差 1~2%
  │  ② 厂商 usage 事件：Anthropic 在 message_start/message_delta 给；
  │                    OpenAI 需显式开 stream_options.include_usage
  │  ③ 客户端断开：到这里为止的 token 已经产生了真实成本，必须计
  │                                                                  ▼
  └──────────────────────────────────────────────▶ 结算（以哪个为准？）
```

**三种策略与取舍**：

| 策略 | 做法 | 准确度 | 成本 | 适用 |
| --- | --- | --- | --- | --- |
| **A. 以厂商 usage 为准** | 等流结束/`usage` 事件到达后结算 | 高（就是账单口径） | 零额外开销 | 默认，覆盖 95% |
| **B. 自算 tokenizer** | 每 chunk 累加，实时可得 | 中（差 1~2%，中英文差异更大） | 每 chunk 一次分词，CPU +5~15% | 需要实时余额显示 / 硬预算刹车 |
| **C. 估算预扣 + 事后校正** | 预扣按 `max_tokens`，结算按 usage 差额 | 高（最终一致） | 零 | **必须有，这是兜底** |

**断流怎么办**——这是 Aurora 那 3.2 万条漏单的直接原因：

```
客户端断开 → HTTP context 被 cancel
           → 结算协程挂在同一个 ctx 上，一起被 cancel
           → usage 已扣成本，账本无分录 = 漏单
```

修法只有一句：**结算必须脱离请求上下文**。

```go
// ❌ 典型错误：结算跟着请求一起死
func handleRun(w http.ResponseWriter, r *http.Request) {
    defer settle(r.Context(), runID)   // ctx 被 cancel，settle 里所有 DB 调用直接返回 error

// ✅ 正确：结算用独立上下文 + 后台队列兜底
func handleRun(w http.ResponseWriter, r *http.Request) {
    reqCtx := r.Context()
    // 1. 生成阶段用请求 ctx（用户断开会停止生成，省钱）
    usage, err := streamGenerate(reqCtx, w, runID)
    // 2. 结算用 Background + 超时，且失败进重试队列
    settleCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
    defer cancel()
    if err := billing.Settle(settleCtx, runID, usage); err != nil {
        billingQueue.Enqueue(runID, usage)   // Outbox，至少要能重投
    }
}
```

### 27.3.3 推理 token：最容易被漏算的成本

带思维链的模型（OpenAI 的 reasoning 系列、各家「思考模式」）会产出**用户看不见但照价收费**的 reasoning token。实测中它经常占 output 的 60%~80%。

```
一次「思考型」调用：可见输出 800 token，reasoning 2,400 token
按 output 单价 ¥0.000071/token（≈$10/M）算：
  只算可见输出：800 × 71 = 56,800 微元 = ¥0.057
  实际计费    ：3,200 × 71 = 227,200 微元 = ¥0.227   ← 4 倍
```

不做这件事的后果：你的成本模型低估 3~4 倍，而定价是按低估的成本做的 → **每卖一单亏一单，且财务报表上看不出来**（因为账本记的成本本身就是错的）。

> 处理原则：`reasoning_tokens` **单独落字段**，计价时并入 `output`，但报表里要能单独切出来。否则你永远回答不了「客户为什么觉得贵」。

### 27.3.4 工具计费：和 token 分开记

内置工具（联网检索、代码执行、文件解析）通常**按次或按时长**计价，MCP 工具还有第三方自己的计费。这些成本：
- 科目不同（不是「模型成本」而是「工具成本」）；
- 计价单位不同（次 / 秒 / GB）；
- 有些是**预付费额度**（你买了 10 万次检索），成本确认时点不同。

所以账本里必须有独立科目，且 `entry` 里带 `unit_type / unit_qty`，不能硬塞进 token 那一列。

### 27.3.5 计价函数：整数微元，先乘后除

```go
// money/micro.go
type Micro int64                     // 1 Micro = 1e-6 元。范围 ±9.2e12 元，足够。

func (m Micro) String() string { return fmt.Sprintf("%d.%06d", m/1e6, m%1e6) }

// 单价统一用「微元 / 百万 token」表示，避免小数
type Price struct {
    Version        string           // "2026-03-01"，必须能追溯到生效时间
    InPerMtok      Micro            // 输入，微元/百万token
    OutPerMtok     Micro
    CacheReadMul   int64            // 千分比：100 = 0.1×，1000 = 1.0×
    CacheWriteMul  int64            // 1250 = 1.25×
}

type Usage struct {
    Input      int64
    CacheRead  int64
    CacheWrite int64
    Output     int64  // 已含 reasoning
    Reasoning  int64  // 单独留档，不重复计价
}

// 先乘后除，最后统一舍入一次；禁止在中间步骤产生小数
func (p Price) Cost(u Usage) Micro {
    var sum int64
    sum += u.Input * int64(p.InPerMtok) / 1_000_000
    sum += u.CacheRead * int64(p.InPerMtok) * p.CacheReadMul / 1_000_000_000
    sum += u.Output * int64(p.OutPerMtok) / 1_000_000
    return Micro(sum)   // 每个字段单独截断 = 丢弃的零头 ≤ 1 微元
}
```

> 🩸 这段代码里唯一值得吵的是「在哪一步舍入」。**契约写死：按字段分别计算、向下取整到微元**。理由是「可解释」——客户来问「这 0.000001 元哪来的」，你能指着某个字段说「这里截断了」。如果你在总和上舍入，你永远解释不清。

---

## 27.4 预扣 — 结算 — 对账：三段式的理由

### 27.4.1 不预扣会怎样：30 秒烧掉 3.5 万

预扣不是为了「体验好」，是为了**给最坏情况设上界**。算一笔账：

```
一个「设计良好」的 Run：12k 输入 + 1.2k 输出 ≈ ¥0.30
一个「失控」的 Run    ：最大步数 20 × 每次 100k 输入
                      = 2,000,000 输入 token
                      × 17.75 微元/token ≈ ¥35.5
────────────────────────────────────────────────────
1000 个并发恶意 Run，30 秒内产生的成本 = ¥35,500
而如果账户余额只有 ¥10 且没有预扣：你全部亏掉，且拦不住第二批
```

更糟的是**免费额度薅羊毛**：注册送 ¥10，脚本并发 200 个 Run，每个都跑到最大步数：

```
真实消耗 200 × ¥35.5 = ¥7,100
账面余额只剩 ¥10，扣成 -¥7,090
× 1 万个羊毛账号 = ¥7,090 万坏账    ← 这不是假设，这是 2019 年以来每一家 AI 平台都遇到过的事
```

### 27.4.2 状态机

```
                        POST /runs
                            │
                            ▼
                   ┌────────────────┐
                   │   RESERVED     │  预扣：available → reserved
                   │  （额度已锁）  │  金额 = 预估输入 + max_output×1.2 + 工具预留
                   └───────┬────────┘
                           │
          ┌────────────────┼────────────────┐
          │ 正常结束        │ 取消/超时       │ 崩溃（进程没了）
          ▼                ▼                │
   ┌────────────┐   ┌────────────┐         │
   │  SETTLED   │   │  RELEASED  │◀────────┘  ← 定时任务扫「RESERVED 超过 2h」
   │ 按实际结算 │   │ 全额释放   │            强制定稿为「按已用量结算 + 释放余量」
   └──────┬─────┘   └────────────┘
          │ 退款 / 争议 / 错价冲正
          ▼
   ┌────────────┐
   │  REVERSED  │   反向分录，原分录不动
   └────────────┘
```

### 27.4.3 预扣：原子条件扣减（一条 SQL 就是一把锁）

```sql
-- 预扣。$1=金额(微元) $2=可用账户id $3=冻结账户id $4=txn_id
-- 关键：条件写在 UPDATE 里，"检查 + 扣减"是同一个原子操作，不存在 TOCTOU
WITH ok AS (
    UPDATE balance
       SET micro = micro - $1,
           version = version + 1,
           updated_at = now()
     WHERE account_id = $2
       AND micro >= $1              -- 余额不足 → 影响 0 行
    RETURNING account_id
)
INSERT INTO entry (txn_id, account_id, amount_micro)
SELECT $4, $2, -$1 FROM ok;

-- 如果上一条影响 0 行，整个事务回滚并返回 INSUFFICIENT_BALANCE
-- 第二条分录（冻结账户 +$1）在同一个事务里做，借贷必须抵消
```

**三个必须遵守的细节**：

1. **多账户加减必须按 `account_id` 升序**，否则 A→B 与 B→A 并发时死锁。这条在支付系统里是常识，在 Agent 平台里经常被忘。
2. **`balance` 表是账本里唯一允许 `UPDATE` 的地方**，它是 `entry` 的物化视图（snapshot + 增量），不是真相来源。
3. **预扣金额要有上界**：`min(估算值, 租户剩余额度, 单 Run 硬上限)`。Aurora 的单 Run 硬上限是 ¥5，超过直接拒。

### 27.4.4 结算：幂等 upsert

```go
func (s *Ledger) Settle(ctx context.Context, runID string, u Usage, p Price) error {
    actual := p.Cost(u)
    key := "settle:" + runID                 // ← 只有 run_id，不带 region/pod/时间戳

    return s.inTx(ctx, func(tx *sql.Tx) error {
        // 1. 抢幂等键。重复结算在这里被 DB 唯一索引挡掉，而不是靠应用判断
        txnID, dup, err := insertTxn(tx, key, "SETTLE", runID)
        if err != nil { return err }
        if dup { return nil }                // 幂等返回成功，不报错（重要）

        // 2. 冲销预扣：reserved → 已消耗
        if err := s.post(tx, txnID, "RESERVED", +actual); err != nil { return err }
        if err := s.post(tx, txnID, "COST",     -actual); err != nil { return err }

        // 3. 释放未用额度
        released := s.held(runID) - actual
        if released > 0 {
            if err := s.post(tx, txnID, "AVAILABLE", +released); err != nil { return err }
            if err := s.post(tx, txnID, "RESERVED",  -released); err != nil { return err }
        }
        return s.assertBalanced(tx, txnID)   // sum(entry.amount) 必须 == 0
    })
}
```

`dup == true` 时**返回成功而不是报错**，这一点很多人做错。报错会让上游重试，重试又触发告警，最后演变成「为了消除告警把幂等键改了」——这是重单事故的经典开场。

### 27.4.5 对账：三方，不是两方

```
┌──────────────┐   ① 内部计量      ┌──────────────┐
│ usage_record │──────────────────▶│  对账任务     │
│ (自算 token) │                   │  Reconcile   │
└──────────────┘                   │              │
┌──────────────┐   ② 账本分录      │  三方 join   │
│   entry      │──────────────────▶│  按           │
│ (结算金额)   │                   │  tenant×model │
└──────────────┘                   │  ×currency    │
┌──────────────┐   ③ 供应商账单    │  ×day         │
│ invoice.csv  │──────────────────▶│              │
│ (实际成本)   │                   └──────┬───────┘
└──────────────┘                          │
                                   ┌──────▼───────┐
                                   │ diff 表 + 告警│
                                   └──────────────┘
```

**为什么必须三方**：

| 两方对比 | 能发现什么 | 发现不了什么 |
| --- | --- | --- |
| ①vs② | 漏单、重单 | 计量本身算错（比如 tokenizer 版本不对） |
| ①vs③ | 成本口径错、缓存折扣错、汇率错 | 这类错不影响「该收多少」，只影响「成本记多少」 |
| ②vs③ | 收入/成本的整体合理性 | 具体到哪一笔 |

> ⚠️ **重要现实**：**不要试图让 ①和③逐 token 对齐。** 你自算的 tokenizer 和厂商计费用的 tokenizer 可能差 0.5%~2%，中英混排、特殊符号、工具定义序列化格式都会引入差异。逐笔对齐会让你陷入几千条「无意义差异」的泥潭，最后团队学会忽略告警。
> **正确做法**：①vs③ 只做**聚合对账**（按 `model × day`），容忍阈值设 **±1.5%**，超出才告警；①vs② 做**逐笔对账**，容忍 0（因为都是你自己算的）。

---

## 27.5 复式记账账本：三张表，一条不能破的规则

### 27.5.1 为什么不能用「一张 usage 表 + 一个余额列」

因为你需要回答的问题，那张表一个都答不了：

| 审计/排障问题 | usage 表 + 余额列 | 复式账本 |
| --- | --- | --- |
| 这笔钱为什么扣了两次？ | 说不清 | 查 `transaction.txn_key` 重复 |
| 余额是怎么变成 -300 的？ | 只有结果 | 逐条 `entry` 重放 |
| 账本本身有没有被改过？ | 不知道 | 哈希链 + 借贷平衡校验 |
| 退款了，原收入怎么处理？ | 改原值（不可追溯） | 反向分录，原分录不动 |
| 成本 vs 收入 vs 税额 怎么拆？ | 混在一起 | 分科目 |

### 27.5.2 三张表

```sql
-- ① 账户：一条「某个租户的某个科目在某个币种下的账」
CREATE TABLE account (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT      NOT NULL,
    code          TEXT        NOT NULL,
    -- AVAILABLE(可用) / RESERVED(冻结) / COST(成本) / REVENUE(收入)
    -- / REFUND(退款) / TAX_PAYABLE(应交税费) / FX_GAIN_LOSS(汇兑损益)
    currency      CHAR(3)     NOT NULL,       -- ISO 4217
    normal_side   SMALLINT    NOT NULL,       -- 1=借 2=贷（决定余额正负怎么展示）
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, code, currency)
);

-- ② 交易：一次业务动作（预扣、结算、退款…）。不可变，只 INSERT。
CREATE TABLE transaction (
    id           BIGSERIAL   PRIMARY KEY,
    txn_key      TEXT        NOT NULL,        -- 幂等键，全局唯一
    kind         TEXT        NOT NULL,        -- RESERVE/SETTLE/RELEASE/REFUND/ADJUST/FX_REVAL
    run_id       TEXT,
    currency     CHAR(3)     NOT NULL,
    biz_time     TIMESTAMPTZ NOT NULL,        -- 业务发生时间（权责发生时点）
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    prev_hash    BYTEA       NOT NULL,        -- 哈希链：全章不展开，详见第 31 章
    hash         BYTEA       NOT NULL,
    UNIQUE (txn_key)
);

-- ③ 分录：一笔交易至少两条，借贷必相等。不可变，只 INSERT。
CREATE TABLE entry (
    id           BIGSERIAL   PRIMARY KEY,
    txn_id       BIGINT      NOT NULL REFERENCES transaction(id),
    account_id   BIGINT      NOT NULL REFERENCES account(id),
    amount_micro BIGINT      NOT NULL,        -- 有符号。同一 txn 内 SUM 必须 = 0
    unit_type    TEXT        NOT NULL DEFAULT 'MONEY',  -- MONEY/TOKEN/CALL/SECOND
    unit_qty     BIGINT,
    memo         TEXT
);
CREATE INDEX ON entry (account_id, id);
CREATE INDEX ON entry (txn_id);

-- ④ 余额物化（唯一允许 UPDATE 的表；可随时由 entry 全量重算）
CREATE TABLE balance (
    account_id   BIGINT PRIMARY KEY REFERENCES account(id),
    micro        BIGINT NOT NULL,
    version      BIGINT NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 27.5.3 一条不能破的规则 + 校验

```sql
-- 规则：任意 transaction 的所有 entry 之和必须为 0（按 unit_type 分别成立）
-- 校验 1：写时校验（应用层，在事务里）
SELECT COALESCE(SUM(amount_micro), 0) FROM entry WHERE txn_id = $1;  -- 必须 = 0

-- 校验 2：日终全量（对账任务的一部分，抓应用层漏校验的历史数据）
SELECT txn_id, SUM(amount_micro) AS s
  FROM entry
 WHERE id > $since_id
 GROUP BY txn_id
HAVING SUM(amount_micro) <> 0;

-- 校验 3：余额与分录重算是否一致（防「有人 UPDATE 过 balance」）
SELECT b.account_id, b.micro, COALESCE(SUM(e.amount_micro), 0) AS recomputed
  FROM balance b
  LEFT JOIN entry e ON e.account_id = b.account_id
 GROUP BY b.account_id, b.micro
HAVING b.micro <> COALESCE(SUM(e.amount_micro), 0);
-- 这条跑出来任何一行，都意味着账本被绕过写过。这是 P0 事件。
```

一次结算产生的分录（借贷必须抵消）：

| txn_id | account | amount_micro | 含义 |
| --- | --- | --- | --- |
| 88231 | AVAILABLE | +497,000 | 预扣时从可用扣走 |
| 88231 | RESERVED | −497,000 | 进冻结 |
| 88231 | RESERVED | +298,000 | 结算冲销冻结 |
| 88231 | COST | −298,000 | 记成本 |
| 88231 | AVAILABLE | +199,000 | 释放未用额度 |
| 88231 | RESERVED | −199,000 | 冻结清零 |
| | **SUM** | **0** | ✅ |

> 💡 复式记账对工程师的真正价值不是「会计规范」，而是：**它强制你写出「钱从哪来、到哪去」**。上面 6 行如果你只能写 2 行（`COST -298000`），说明你的模型里根本没有「冻结」这个概念——而冻结正是防超卖的唯一手段。

### 27.5.4 金额精度：禁止 float，甚至慎用 DECIMAL

```go
// ❌ 绝对不能上生产
type Bill struct{ Amount float64 }

// ❌ 看起来严谨但会咬人
// amount DECIMAL(20,6) —— 除法舍入规则依赖数据库实现，且 Java 的 BigDecimal
// 与 Go 的 shopspring/decimal 在 divide 的默认精度上不一致，跨语言必出偏差

// ✅ 整数微元 + 显式舍入契约
type Micro int64
```

**为什么不能用 float64**——不是「精度不够」，是「**不可解释**」：

```
float64 尾数 53 位，能精确表示的整数上限是 2^53 ≈ 9.0e15。
但一旦做乘除就落到二进制小数：
  0.1 的真实存储值 = 0.1000000000000000055511151231257827...
  1e8 次累加后偏差可达 ¥0.01 ~ ¥1 量级，且每次运行结果不同

整数微元：
  每笔四舍五入误差 ≤ 0.5 微元
  1 亿条/天 → 最大累计 ¥50/天，并且每一笔的误差都能被复算出来
```

**关键不是那 ¥50，而是**：账对不上时，浮点误差是**随机且不可复现**的，你永远无法证明「哪一条错了、错多少」。而整数运算，你可以把同一条流水重算一万次，结果完全一样。**审计要的是可复现，不是绝对精确。**

**舍入契约（写进接口文档）**：

```
1. 所有金额入参为 Micro（int64），出参为 Micro
2. 乘法先做，除法最后做，中间不得转 float
3. 舍入方式：向零截断（truncate），并记录被丢弃的余数到 memo
4. 汇率：保存到 1e-8 精度（整数），换算时 = amount × rate / 1e8，截断
5. 任何跨币种比较必须先折算到记账币种，折算时点固定为 biz_time 的日终汇率
```

---

## 27.6 幂等键与防重放

### 27.6.1 幂等键的构造：只有一件事不能做

| 场景 | 幂等键 | 评价 |
| --- | --- | --- |
| 预扣 | `reserve:{tenant_id}:{run_id}` | ✅ |
| 结算 | `settle:{run_id}` | ✅ 最简单，最不容易错 |
| 分步结算 | `settle:{run_id}:{step_seq}` | ✅ |
| 退款 | `refund:{refund_id}` | ✅ 退款单独立发号 |
| 冲正 | `reversal:{orig_txn_id}` | ✅ 一笔原交易只能冲正一次 |
| ❌ 事故版 | `settle:{run_id}:{region}:{pod_ip}` | 🩸 region 迁移后同一 Run 生成两个 key |
| ❌ 事故版 | `settle:{run_id}:{time.Now().Unix()}` | 🩸 每次调用都不一样，等于没有幂等 |
| ❌ 事故版 | `{client_idempotency_key}`（不带租户） | 🩸 两个租户撞 key，A 的钱记到 B 头上 |

**唯一的硬规则：幂等键里只能有「业务上唯一标识这次动作」的字段，不能有任何环境信息（region/pod/时间/重试次数）。**

### 27.6.2 客户端幂等键必须加租户前缀

`Idempotency-Key` 由客户端生成（详见第 8 章），你不能假设它全局唯一。所以：

```sql
-- ❌ 全局唯一 → 跨租户冲突
UNIQUE (idempotency_key)

-- ✅ 租户内唯一
UNIQUE (tenant_id, idempotency_key)
```

不加租户前缀的后果不是「报错」，是**静默串账**：B 租户的请求命中了 A 租户的幂等记录，直接返回 A 的结果，B 不扣钱。这种 bug 在测试环境永远复现不了（因为测试环境只有一个租户在跑）。

### 27.6.3 防重放：内部调用也要签名

Agent 平台的内部调用链很长（网关 → 计费 → 账本 → 数仓），很容易出现「内部接口裸奔」：

```go
// 内部计费接口的请求签名
// X-Sign = HMAC-SHA256(secret, method + path + bodySHA256 + timestamp + nonce)
func verifyInternal(req *http.Request, body []byte) error {
    ts, _ := strconv.ParseInt(req.Header.Get("X-Timestamp"), 10, 64)
    if abs(time.Now().Unix()-ts) > 300 {          // 5 分钟窗口
        return ErrExpired
    }
    nonce := req.Header.Get("X-Nonce")
    if !nonceStore.SetNX(nonce, 1, 10*time.Minute) {  // Redis SETNX，防重放
        return ErrReplay
    }
    want := sign(req.Method, req.URL.Path, sha256Hex(body), ts, nonce)
    if !hmac.Equal([]byte(want), []byte(req.Header.Get("X-Sign"))) {
        return ErrBadSign
    }
    return nil
}
```

不做的后果：内网横向移动（一个被拿下的 Pod）可以直接调「给任意账户加钱」的接口。而内网接口通常**没有任何鉴权**——因为「反正在 VPC 里」。

---

## 27.7 漏单 / 重单 / 超卖：发现与修复

### 27.7.1 三种错误的发现手段

| 错误 | 首选发现手段 | 兜底手段 | 目标指标 |
| --- | --- | --- | --- |
| 漏单 | 逐笔对账：`usage` LEFT JOIN `transaction` 找 NULL | 小时级「RESERVED 超 2h 未结算」扫描 | `orphan_rate < 1e-4` |
| 重单 | DB 唯一索引（写时拦截，不应发生） | 「同一 run_id 的 SETTLE 交易数 > 1」日扫 | `dup_rate == 0` |
| 超卖 | 余额为负的账户数 | 「单账号 24h 内消耗 > 其历史 P99 × 10」风控规则 | `negative_balance_accounts == 0` |
| 错价 | 三方聚合对账（①vs③ 偏差 > 1.5%） | 价格表变更的 diff 告警（改倍率必须双人审批） | `cost_variance < 0.5%` |

### 27.7.2 对账任务：三段式调度

```
每小时 :  增量对账（近 48h）  → 抓漏单/重单，SLA 15 分钟内出结果
每日 02:00: 全量对账（T-1）    → 三方聚合 + 借贷平衡 + 余额重算
每月 1 日  : 关账（T-1 月）     → 冻结账本（拒绝任何 biz_time < 关账时点的写入）
                               → 生成客户对账单 + 供应商成本分析
```

```go
// reconcile/reconcile.go
type Diff struct {
    TenantID string
    Model    string
    Currency string
    Day      time.Time
    Kind     string  // ORPHAN / DUP / PRICE / FX / BALANCE
    Count    int64
    AmountMicro Micro
}

func (r *Reconciler) Run(ctx context.Context, from, to time.Time) ([]Diff, error) {
    // A. 逐笔：漏单（有 usage 无 settle）
    orphans, err := r.q.FindOrphanUsage(ctx, from, to)   // 见下方 SQL
    if err != nil { return nil, err }

    // B. 逐笔：重单（同一 run 多次结算）
    dups, err := r.q.FindDuplicateSettle(ctx, from, to)

    // C. 聚合：自算成本 vs 供应商账单，按 model×day
    variances, err := r.q.AggregateVariance(ctx, from, to)

    // D. 账本自检：借贷不平衡 + 余额与分录不一致
    unbalanced, err := r.q.FindUnbalancedTxn(ctx, from, to)
    if len(unbalanced) > 0 {
        r.alert.P0(ctx, "ledger unbalance", len(unbalanced))  // 直接 P0，不用等阈值
    }

    return append(append(orphans, dups...), variances...), nil
}
```

```sql
-- 漏单检测：这条 SQL 是对账系统的核心，值得单独优化索引
SELECT u.run_id, u.tenant_id, u.cost_micro, u.created_at
  FROM usage_record u
  LEFT JOIN transaction t
         ON t.txn_key = 'settle:' || u.run_id
 WHERE u.created_at >= $1 AND u.created_at < $2
   AND t.id IS NULL
   -- 排除「正在跑、还没到结算时点」的正常在途
   AND u.created_at < now() - interval '10 minutes'
 LIMIT 10000;

-- 超卖检测
SELECT account_id, micro FROM balance WHERE micro < 0;
```

### 27.7.3 漏单修复：五步，缺一步就是二次事故

```
① 圈定影响面
   └─ 按 (时间窗, 租户, 模型) 聚合，产出：X 笔 / Y 个租户 / 共 ¥Z
      必须同时算出「钱少收了多少」和「客户会不会已经发现」

② 判断是否可自动补记
   ├─ 在途（<2h，可能只是慢）   → 不做任何事，等下一轮
   ├─ 可确定（usage 完整，有 usage 事件） → 自动补记（见 ③）
   └─ 不可确定（usage 也丢了）   → 按「同租户同模型同小时的 P50 成本」估算，
                                   标记 estimate=true，进人工复核队列

③ 补记：写一条新的 ADJUST 交易，绝不去改原记录
   txn_key = "adjust:orphan:{run_id}"
   biz_time = 原 usage 的发生时间   ← 重要：影响期间归属，不能写成 now()

④ 通知与对账单调整
   ├─ 内部：更新对账 diff 表状态 → 已修复
   └─ 外部：金额 > ¥1,000 的，必须进当月对账单的「补记」栏并附说明

⑤ 复盘：回答「为什么对账没在 1 小时内抓到」
   └─ 90% 的答案是「结算协程跟着请求 ctx 一起被 cancel」（§27.3.2）
```

> 🩸 **最容易犯的错**：直接 `UPDATE usage_record SET settled = true` 或者补一条 `INSERT INTO transaction` 但没写分录。前者破坏了「不可变」，后者破坏了「借贷平衡」——两个都会让下一次审计直接失败。**修复动作本身必须走同一套账本 API，不允许 DBA 手工 SQL。**

### 27.7.4 超卖的三种补法

| 场景 | 补法 | 代价 |
| --- | --- | --- |
| 预扣估值偏低（正常波动） | 提高预扣系数（P50 → P90 × 1.2） | 在途资金变多，大客户会抱怨「可用额度不够」 |
| 并发绕过（同一账户并发 N 个 Run，每个都刚好够） | 预扣改为「按账户加行锁顺序扣」，而非各自判断 | 吞吐下降，但只有同账户内部串行 |
| 恶意薅羊毛 | 风控层：新账号 24h 内并发上限 + 设备指纹 + 支付方式校验（详见第 32 章） | — |

第二种是真正的技术难点。典型 bug：

```
余额 ¥10，单 Run 预扣 ¥3
并发 10 个请求同时到达 → 10 个都读到 balance=10 → 10 个都通过 → 扣成 -20
```

根因是「先 SELECT 判断再 UPDATE」。修法就是 §27.4.3 那条 `UPDATE ... WHERE micro >= $1`——**把判断塞进 UPDATE 的 WHERE 里**，利用行锁的原子性。这一条能解决 80% 的超卖。

---

## 27.8 退款与对公结算

### 27.8.1 退款是反向分录，不是删除

```sql
-- 全额退款：一笔新交易，两条反向分录
INSERT INTO transaction (txn_key, kind, run_id, currency, biz_time)
VALUES ('refund:RF20260301001', 'REFUND', 'run-abc', 'CNY', now());

INSERT INTO entry (txn_id, account_id, amount_micro) VALUES
  ($txn, $revenue_account, -298000),   -- 冲减收入
  ($txn, $refund_account,  +298000);   -- 记退款负债
```

**部分退款**、**按比例退**、**退 token 不退钱**（额度退回）是三种不同业务，账本科目不同，不要合并成一个 `refund` 开关。

### 27.8.2 对公结算的工程清单

企业客户不走预充值，走**月结账期**。这条链路比 C 端复杂一个量级：

| 环节 | 工程要求 | 不做会怎样 |
| --- | --- | --- |
| 账期 | `payment_terms=30/60` 天，到期日 = 账单日 + N | 财务手工算，算错一次就是催收纠纷 |
| 信用额度 | 独立于「余额」的一个科目 `CREDIT_LIMIT` | 用余额字段硬扛，导致报表里现金流虚高 |
| 对账单 | 每月 1 日生成 PDF/CSV，含明细 + 汇总 + 调整项 | 客户财务对不上，付一半拖着 |
| 发票 | 增值税专用发票（信息技术服务 6%），一票一账 | 票账不符，税务风险 |
| 争议处理 | 争议单要能挂起对应流水，不能直接冲正 | 冲正了又发现客户是对的，二次冲正 |
| 坏账准备 | 按账龄计提（30/60/90 天） | 财务报表失真 |

**AR 规模算式**（用于说服老板「对公要做自动化」）：

```
月收入 ¥7,200 万，其中对公占 60% = ¥4,320 万
账期 30 天 → 平均应收余额 ≈ ¥4,320 万
坏账率 1%（行业常见）→ 每月 ¥43 万
如果对账自动化把「逾期 30 天以上」的比例从 15% 降到 5%：
  可回收金额改善 ≈ ¥4,320 万 × 10% × (1-1%) ≈ ¥428 万/月
```

---

## 27.9 多币种与税率：两个「看起来简单」的坑

### 27.9.1 多币种：记账币种 ≠ 结算币种

```
       业务发生              记账                  结算
   ┌──────────┐      ┌──────────────┐      ┌──────────────┐
   │ USD $0.042│─────▶│ CNY ¥0.30    │─────▶│ 按账单日汇率  │
   │ (供应商)  │ 汇率  │ (本位币记账)  │      │ 实际付 USD   │
   └──────────┘ 7.10  └──────────────┘      └──────────────┘
                            │
                            │ 月末重估（汇率变动）
                            ▼
                     ┌──────────────┐
                     │ FX_GAIN_LOSS │  ← 汇兑损益，单独科目
                     └──────────────┘
```

三条规则：

1. **记账币种只有一种**（Aurora 是 CNY）。所有报表、毛利、税率都基于它。
2. **汇率必须落库快照**（`rate_date`, `rate_value_e8`），不能运行时查汇率表——否则同一条历史流水重算出来的金额会变。
3. **月末做汇兑重估**：外币货币性项目的余额按月末汇率重算，差额进 `FX_GAIN_LOSS`。Aurora 那次 ¥68 万就是漏了这一步。

```sql
CREATE TABLE fx_rate (
    base      CHAR(3) NOT NULL,
    quote     CHAR(3) NOT NULL,
    rate_date DATE    NOT NULL,
    rate_e8   BIGINT  NOT NULL,   -- 1e-8 精度，7.10 → 710000000
    source    TEXT    NOT NULL,   -- 'PBOC' / 'ECB' / 'BANK_SETTLEMENT'
    PRIMARY KEY (base, quote, rate_date, source)
);
```

> ⚠️ **「交易日汇率」还是「月末汇率」不是技术选择，是会计政策选择**，必须和财务确认后写进代码注释。技术团队自己拍板 = 下一次审计的例外项。

### 27.9.2 税率：三条线，别混

| 场景 | 税种 | 处理 |
| --- | --- | --- |
| 境内企业客户 | 增值税 6%（信息技术服务） | 价税分离，`TAX_PAYABLE` 科目 |
| 境内个人客户 | 同上 | 通常含税标价，需反算不含税额 |
| 跨境（EU 个人） | 买家所在国 VAT（OSS 机制） | 按 B2C 目的地税率，需判定买家国别 |
| 跨境（US 各州） | Sales Tax | 各州对数字服务是否应税不同，需按地址判定 nexus |
| 跨境（B2B，有税号） | 反向征收（reverse charge） | 发票上标注，税率 0，需校验 VAT 号有效性 |

**工程上唯一重要的事：税率是一张带生效时间的表，不是代码里的常量。**

```yaml
# pricing/tax/2026.yaml
version: "2026-03-01"
rules:
  - match: { seller_region: CN, buyer_region: CN, buyer_type: business }
    tax: { code: VAT, rate_bp: 600, inclusive: false }     # bp = 万分之一
  - match: { seller_region: CN, buyer_region: EU, buyer_type: consumer }
    tax: { code: VAT_OSS, rate_source: buyer_country, inclusive: true }
  - match: { seller_region: CN, buyer_region: EU, buyer_type: business, vat_id_valid: true }
    tax: { code: REVERSE_CHARGE, rate_bp: 0 }
```

税率错了，钱不是你的——**这是「多收的钱必须退、少收的不能追」的领域**，错一次就是税务风险。

---

## 27.10 Java / 分布式经验对照

| 你熟悉的 | Agent 计费里的对应 | 会咬人的差异 |
| --- | --- | --- |
| 支付系统的 `account/entry/transaction` | 几乎一模一样 | ✅ 直接迁移，这套设计 20 年没变过 |
| `BigDecimal` + `DECIMAL(19,4)` | 建议换成 `int64` 微元 | 🟡 `BigDecimal.divide` 不指定 scale 会抛异常，指定了各语言默认值不同 → 跨语言必不一致 |
| 数据库唯一索引做幂等 | ✅ 一样 | 🟢 这是最可靠的幂等手段，别用 Redis `SETNX` 当唯一防线 |
| RocketMQ 事务消息 / Outbox | 结算事件投递 | 🟢 直接迁移，用来解耦「结算」与「下游统计」 |
| 对账（银行对账经验） | 三方对账 | 🟡 多了「供应商账单」这一方，且它**不可信**（口径不同） |
| Sentinel 的 QPS 限流 | 成本熔断 | 🔴 限流是「次数」，成本是「金额」，需要独立的「预算令牌」机制（详见第 33 章） |
| 分库分表（按 tenant_id） | 账本分区 | 🟡 **账本不能随便分片**：跨分片的借贷平衡校验会失效，必须按 `tenant_id` 分且每租户自平衡 |
| `@Transactional` | 记账事务 | 🟢 一样，但要注意「事务里不能调外部 HTTP」（结算通知供应商） |
| 复盘文化 | 账务事故复盘 | 🔴 账务复盘必须产出「金额影响 + 客户影响 + 审计影响」三项，不只是技术根因 |

---

## 27.11 开源坐标：这块能抄什么，缺什么

| 项目 | 有什么 | 缺什么（对你来说） |
| --- | --- | --- |
| **Stripe 的账本设计**（工程博客公开过 double-entry ledger 的做法） | 复式记账 + 不可变流水 + 多币种的完整范式 | 不开源；但**设计文档值得逐字读**，尤其是「为什么不用余额列」 |
| **TigerBeetle** | 专为金融账本设计的数据库，原生 double-entry、u128 金额、单机百万 TPS 级 | 要独立部署与运维；生态年轻；对「按租户查询」的支持不如 Postgres 灵活 |
| **Formance Ledger**（原 Numary） | 可编程账本，声明式定义账户与交易，支持多币种 | 团队规模小，长期支持要评估；性能模型与你的量级要实测 |
| **LiteLLM Proxy** | 有 `spend` 记录、`max_budget`、按 key/team 的预算与限流 | **单表记账，没有复式分录、没有预扣、没有对账**；预算超了是「拒绝请求」，不是「记账」。适合做「成本护栏」，不能当账本 |
| **Langfuse** | 每次 generation 记 `cost`（可自定义 model price） | 是**可观测**，不是账本：可改可删、无幂等、无平衡校验。适合做「成本归因」，不能作为收费依据 |
| **OpenMeter** | 用量计量 + 计费（usage-based billing） | 偏「计量聚合」，预扣与对账要自己补 |
| **Temporal** | 长流程编排（预扣→结算→重试） | 不管账；但它能把「结算失败重试」写得非常干净 |
| **LangGraph / Dify** | 无 | 计费完全不在编排框架的视野里（见第 3 章的 12% 论断） |

> 🎯 **一句话结论**：**能抄设计（Stripe / TigerBeetle），别抄实现。** 你的量级（800 万 Run/天、峰值 3,000 TPS 记账）用 Postgres + 分区 + `int64` 微元完全够，比引入一个新数据库省 6 个月。真正难的不是存储，是 **§27.7 的对账与修复**——这部分没有开源项目能替你写。

---

## 27.12 事故清单

- [ ] **事故 A：结算跟着请求一起被 cancel** → 现象：流式断连的 Run 全部漏单，日漏 3 万条。→ 根因：`settle(r.Context())`。→ 兜底：`context.Background()` + Outbox 重投 + 小时级漏单扫描（§27.3.2）
- [ ] **事故 B：幂等键带环境信息** → 现象：region 切换后重单 4 万条，客户投诉「扣两次」。→ 根因：`key = run_id + region`。→ 兜底：幂等键构造代码集中在一个函数里，且单元测试断言「不含 region/pod/time」
- [ ] **事故 C：缓存折扣倍率被价格表回滚覆盖** → 现象：成本虚高 2.5%，一个月后审计发现。→ 根因：价格表没有版本与生效时间，回滚是原地覆盖。→ 兜底：价格表 append-only + 生效时间 + 变更双人审批 + 变更后 24h 内成本方差监控
- [ ] **事故 D：余额判断先 SELECT 再 UPDATE** → 现象：并发 10 个请求把余额扣成 -20，被薅。→ 根因：TOCTOU。→ 兜底：`UPDATE ... WHERE micro >= ?`，并加「负余额账户数」告警
- [ ] **事故 E：财务直接改库修账** → 现象：审计时余额与分录不一致，无法解释。→ 根因：DBA 有 `UPDATE balance` 权限。→ 兜底：账本库只给应用账号 `INSERT`（除 balance 表），DBA 只读；所有调整走 `ADJUST` 交易
- [ ] **事故 F：reasoning token 没记** → 现象：定价按可见输出算，实际成本 3.5 倍，卖一单亏一单。→ 兜底：`reasoning_tokens` 强制字段 + 成本模型回归测试
- [ ] **事故 G：关账后还有 biz_time 落在上月的写入** → 现象：上月报表变了，已发出的对账单作废。→ 兜底：关账后账本对 `biz_time < close_time` 的写入直接拒绝，迟到数据走「上期调整」科目

---

## 27.13 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 计费 vs 统计 | **计费是资金系统**：错了只能冲正，冲正需要证据链 |
| 六字段 | input / cache_write / cache_read / output / reasoning / tool，少一个就拆不开 |
| 缓存折扣 | 厂商差异能到 5 倍，必须是 `(provider, model, price_version)` 四维价格表 |
| 流式计量 | 以厂商 usage 为准；断流必须结算；**结算不能挂在请求 ctx 上** |
| 预扣 | 目的不是体验，是给最坏情况设上界；不预扣 → 1000 并发 30 秒烧 ¥3.5 万 |
| 三段式 | 预扣（条件扣减）→ 结算（幂等）→ 对账（三方，但供应商只做聚合） |
| 复式记账 | 三张表 + 一条铁律：`SUM(entry) = 0`；`balance` 是唯一可 UPDATE 的表 |
| 金额精度 | `int64` 微元，禁止 float；**不是因为精度，是因为可复现** |
| 幂等键 | 只含业务标识，**绝不含 region / pod / 时间**；客户端 key 必须带租户前缀 |
| 超卖 | `UPDATE ... WHERE balance >= ?`，把判断塞进 WHERE |
| 修复 | 补记新交易，绝不改旧记录；修复动作必须走同一套账本 API |
| 对账频率 | 小时（增量 48h）+ 日（全量）+ 月（关账），三个 SLA 不同 |
| 一句话 | **你不需要 Stripe，但你必须在 5 分钟内证明「账是平的、这笔钱追得到」。** |

## 27.14 自检题

1. **一次 Run 因为用户关闭页面被中断，你的账本会发生什么？请描述三个环节。**
   > 要点：① 生成链路收到 ctx cancel，停止生成（省钱，正确）；② 已产生的 token 已经付了供应商钱，必须结算——但如果结算协程共享请求 ctx，它会被一起 cancel → 漏单；③ 正确做法是独立 ctx + Outbox 重投 + 小时级 `usage LEFT JOIN transaction` 扫描兜底，补记用 `ADJUST` 交易且 `biz_time` 取原发生时间。

2. **为什么对账不应该要求「自算 token 数」与「供应商账单」逐笔相等？**
   > 要点：tokenizer 实现不同（中英文、特殊符号、工具定义序列化格式），差异 0.5%~2% 是正常的；逐笔对齐会产生海量无意义差异，最终导致团队忽略告警。正确做法是「内部计量 vs 账本」逐笔对账（容忍 0），「内部计量 vs 供应商账单」按 `model×day` 聚合对账（容忍 ±1.5%），并把差额归入「成本差异」科目做趋势监控。

3. **你的余额字段是 `BIGINT`，账本有复式分录，那还需要 `balance` 这张表吗？**
   > 要点：需要，但定位是「物化视图/快照」而不是真相来源。纯靠 `SUM(entry)` 算余额在 800 万 Run/天（约 1 亿条分录/天）下会随数据量线性变慢，必须靠快照 + 增量。代价是快照可能与分录不一致，所以必须有日终「余额重算比对」任务，跑出任何一行不一致都是 P0。反过来，如果只有 balance 没有 entry，你连「为什么会不一致」都查不出来。

## 27.15 延伸阅读

- 上一章：[26. 模型路由与多策略](26-model-routing.md)
- 下一章：[28. 大流量不崩：容量、压测与隔离](28-peak-traffic.md)
- 相关：[第 8 章](8-run-api-design.md)（`Idempotency-Key` 的 API 层设计）、[第 23 章](23-rate-limit-quota.md)（限流与配额，计费的上游）、[第 31 章](31-compliance-audit.md)（账本流水的哈希链与 WORM 归档）、[第 33 章](33-multi-agent-collaboration.md)（多 Agent 的预算令牌分配）、[第 47 章](47-cost-engineering.md)（单位经济模型与毛利）
- 总纲：[0-summary.md](0-summary.md)
- 语言/运行时细节：见 [../go/0-summary.md](../go/0-summary.md)
- 外部：Stripe 工程博客中关于 double-entry ledger 的文章；TigerBeetle 官方文档；各模型厂商的价格页与 prompt caching 文档（**以官方最新版为准，价格与折扣倍率变动频繁**）
