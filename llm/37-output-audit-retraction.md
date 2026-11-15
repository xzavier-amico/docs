# 37. 输出审计与「撤回已说出去的话」

> **状态**：`已完成（初稿）` ｜ **所属**：第七部分 · 输入输出管道 ｜ **编号**：37 / 52
> **本章定位**：全书的招牌章节。前面 36 章都在讲「怎么把输出送出去」，这一章讲**怎么把已经送出去的东西收回来**——这是流式架构留给你的最大一笔技术债，也是唯一一个「做错了会直接赔钱、被投诉、被监管问话」的架构决策。
> **读法**：先读 §37.1 的三条定律，它们决定了后面所有机制的形态；然后 §37.4 的事件协议和前端 reducer 是可以直接抄进代码评审的；§37.4.9 的指标表建议每周例会过一遍。

---

## 37.1 引子：那一句话已经在你用户屏幕上了

凌晨 1:47，Aurora 客服 Agent 正在给一个企业客户回答退费政策：

```
用户：我们这个合同是年付的，现在要退，能退多少？
Agent：根据您签署的《企业服务协议》第 7.2 条，年付合同在服务期内
       可随时申请全额退款，我们会在 3 个工作日内原路退回。█
```

用户回了一句「好的，那我明天走流程」，然后**把这张截图发给了他的财务总监**。

四分钟后，你的知识库版本对账任务跑完，发现这条回答引用的是 2024 年版的协议（第 7.2 条在 2026 年 3 月已改为「按剩余服务月数比例退款」）。检索层召回了一篇没被下线的旧文档。

现在你面对一个问题：**这句话已经说出去了，你怎么收回来？**

这不是一个边缘场景。Aurora 的实测数据是：

- 日均 Run：约 **180 万**（30 万日活 × 人均 6 次）
- 单 Run 输出 800 字 ≈ 1200 token，切成约 **12 个可撤回片段**
- 片段总量：**2160 万/日**
- 需要撤回的片段率：**0.3%**（含幻觉、审核漏放、政策失效、工具结果过期）
- → **每天 6.5 万次撤回**

0.3% 的撤回率听起来很低，但它摊到时间轴上是：**平均每 5 秒就有一次全平台某处正在更正自己刚说的话**。其中约 25%（1.6 万次/日）发生在用户**已经读到那段文字之后**。

> 🩸 **这就是第 36 章留给你的账单**：流式输出把「一次性响应」拆成了几十个「已提交的副作用」。在传统后端，一次 HTTP 响应发出去之前你什么都能改；在流式 Agent 里，**每 flush 一个片段就等于 commit 一个不可回滚的事务**。

### 三条定律（本章的所有设计都是它们推出来的）

```
定律一 · 不可撤回定律
  信息一旦离开你的控制边界（用户屏幕 / 剪贴板 / 截图 / 转发），
  你能做的只有「更正」，不能「撤回」。
  ⇒ 撤回能力 = 你对「已读时刻」的控制能力。

定律二 · 追加定律
  系统里没有 undo，只有「后来的事实覆盖先前的事实」。
  所有撤回必须落成 append-only 的补偿事件，否则审计链断裂、取证失效。

定律三 · 单调定律
  撤回必须单调且不可复活：客户端重连重放旧事件，
  不能让已撤回的内容重新出现在屏幕上。
```

第一条告诉你：**别指望事后补救，要去买「延迟窗口」**。这就是 §37.4 双通道设计的存在理由。
第二条告诉你：**别写 `UPDATE fragments SET deleted=1`**，那是在销毁证据。详见第 31 章（合规审计）。
第三条告诉你：**事件协议里必须有 `rev` 和 tombstone**，否则断线重连会把撤回的内容抖回来——这是本章最容易被漏掉的一个 bug，也是 §37.4 前端契约的核心。

---

## 37.2 断崖：为什么「必须能退回」

先把「不撤回会怎样」算清楚。五类触发源，每一类的频率和后果完全不同，**不能用同一套机制兜**：

| # | 触发源 | 本质 | Aurora 频率 | 不撤回的后果 | 后果量化 |
| --- | --- | --- | --- | --- | --- |
| 1 | **审核滞后** | 审核器比生成器慢；异步复核在输出后才出结果 | 每 10 万片段 3 次漏放 | 不当内容留在会话里，可被截图传播 | 一起监管投诉 ≈ 5 万元成本 + 平台级风险 |
| 2 | **模型幻觉** | 模型编了不存在的条款/数字/时效 | 每 1000 Run 约 4 次 = 7200 次/日 | 用户按错误信息行动 → 赔付 | 承诺类幻觉赔付均值 ¥260，30% 会落地 |
| 3 | **政策变更** | 知识库/规则更新，历史回答全部失效 | 每季度 1~2 次，单次影响 3~12 万条回答 | 存量会话持续输出旧政策 | 一次批量召回 ≈ 12 万条 × 客服工时 |
| 4 | **用户投诉** | 用户指出错误，需要当场认账并改口 | 每 1 万 Run 约 6 次 | 不认账 → 升级为工单/舆情 | 投诉升级率从 22% 涨到 61% |
| 5 | **上游撤稿** | 引用的文档/数据被作者撤回或标记过期 | 每月约 1 次 | 你在引用一个已作废的来源 | 法务风险 + 引用可信度崩塌 |

**算一笔钱**（这是说服老板加这个模块的唯一方式）：

```
幻觉类错误片段：6.5 万 × 60% ≈ 3.9 万/日
其中「承诺型」（退款/时效/价格/资质）：2%  → 780 次/日
无撤回机制时落地为实际赔付的比例：30%     → 234 次/日
单次赔付均值 ¥260 + 法务工时 ¥150          → ¥410
                                        ─────────────
日损失                                  ≈ ¥9.6 万
年损失                                  ≈ ¥3,500 万

上了撤回机制后，落地比例 30% → 5%（大部分用户在行动前被更正）
日损失 ≈ ¥1.6 万，年节省 ≈ ¥2,900 万
模块自身成本（审核算力 + 工程人力）≈ 年 ¥400 万
                                        ─────────────
ROI ≈ 7×
```

> 💡 顺便说一句：这笔账里最贵的其实不是赔付，是**「用户不再相信它说的话」**。一个会当场认错并改口的 Agent，比一个永远错得很自信的 Agent 存活率高一个数量级。撤回不是失败，**是产品可信度的一部分**。

### 反模式：静默替换（silent rewrite）——绝对禁止

你一定会在某次需求评审里听到这个方案：「用户又不会回头看，后台把那条消息改掉就行了，刷新就没了。」

**这是最坏的一种做法**，比不撤回还糟：

| 做法 | 用户感知 | 取证后果 | 法律风险 |
| --- | --- | --- | --- |
| 不撤回 | 错的内容留着，但一致 | 有据可查，责任清晰 | 内容风险，但过程诚实 |
| **静默改写** | 刷新后「我明明看到的是另一个答案」 | **审计链断裂，你无法自证清白** | 最差：涉嫌篡改记录 |
| 追加更正 | 看到「更正」标记 | 完整留痕，可复盘 | 最优：透明的纠错过程 |

静默改写一旦被用户发现（截图对比），你就从「AI 说错了」升级成「平台篡改数据」。第 31 章会讲，append-only 审计链的整个价值就建立在「历史不可变更」上——你在这里做一次 UPDATE，整套取证体系就废了。

---

## 37.3 结构图：撤回能力沿着时间轴衰减

这是本章最重要的一张图。**你所有的架构努力，都是在把这条曲线往右推。**

```
可撤回度
100% ┤██████  ① 草稿缓冲：还没 flush（只在服务端）
 95% ┤█████   ② 已写出，在 TCP/SSE 缓冲或反代里（可以 RST）
 70% ┤████    ③ 已到客户端，未渲染（在 JS 事件队列里，可以丢）
 45% ┤███     ④ 已渲染但在折叠区/视口外（可以替换，用户大概率没看）
 20% ┤██      ⑤ 已读，用户尚未行动（可以更正，用户会看到"打脸"）
  4% ┤▏       ⑥ 已读且已行动（复制/转发/截图/已按它操作）→ 只能道歉+补偿
  0% ┘────────┬────────┬────────┬────────┬──────────▶ t
             0ms      300ms     1s       10s        ∞
              │        │         │
              │        │         └─ R2 追加更正的主战场
              │        └─ 双通道确认流的 hold 窗口
              └─ R1 截断停机的最后机会
```

**这张图读出来三件事**：

1. 曲线的陡降点在 **1 秒**，不是 100 毫秒。也就是说，你花 300~800ms 买到的「确认窗口」是极其划算的——它覆盖了从 45% 到 95% 的可撤回度区间。
2. 曲线在 10 秒后基本躺平。所以**超过 10 秒的老消息，别浪费算力去撤**，改用「批量下线 + 置顶横幅」。
3. 「已读且已行动」之后，撤回机制的技术价值归零，只剩**法务与客诉流程**。

### 数据面：双通道与片段状态机

```
                        ┌──────────────────────────────────┐
   LLM token 流 ───────▶│ Segmenter  句/段边界切分          │
                        │ (句号、换行、JSON 闭合、工具边界) │
                        └──────┬────────────────┬──────────┘
                     draft 直发│                │hold_ms 缓冲
                               ▼                ▼
                     ┌──────────────┐  ┌────────────────────┐
                     │ 草稿通道      │  │ 确认通道            │
                     │ draft SSE    │  │ audit ─▶ commit     │
                     │ 立即可见/灰显 │  │ 可被引用/可被下游消费│
                     └──────┬───────┘  └─────────┬──────────┘
                            │                    │
                            └────────┬───────────┘
                                     ▼
                        ┌──────────────────────────────────┐
                        │ 客户端 Fragment Store            │
                        │  fragments[] rev state           │
                        │  tombstones: Set<seq>   ← 关键   │
                        └──────┬───────────────────────────┘
                               │ 事后判定失败
                               ▼
                        ┌──────────────────────────────────┐
                        │ Retractor（四道闸门）             │
                        │ G1 pre-send / G2 in-flight        │
                        │ G3 post-commit / G4 session       │
                        └──────┬───────────────────────────┘
                               │ correction 事件（append-only）
                               ▼
                        ┌──────────────────────────────────┐
                        │ 审计链 audit_event（哈希链，WORM）│
                        │ 片段 × 上下文快照 × 模型版本 × 判定 │
                        └──────────────────────────────────┘
```

**为什么要双通道而不是「全部延迟 800ms」**：因为 800ms 的 TTFT 惩罚会实打实地掉转化。Aurora 做过实验：TTFT 从 900ms 拉到 1700ms，首轮会话完成率掉 11%。所以只能**给 8% 的敏感流量付这个延迟**，其余 92% 走草稿直发 + 事后更正。这就是分级延迟的全部动机。

### 控制面：撤回决策的四道闸门

```
时间轴 ─────────────────────────────────────────────────────────────▶

  [片段生成]   [flush]    [渲染]     [用户读]    [用户行动]
      │           │          │          │           │
      ▼           ▼          ▼          ▼           ▼
   ┌──────┐   ┌──────┐   ┌──────┐   ┌──────┐   ┌──────┐
   │ G1   │   │ G2   │   │ G3   │   │ G3'  │   │ G4   │
   │pre-  │   │in-   │   │post- │   │已读  │   │session│
   │send  │   │flight│   │commit│   │更正  │   │撤销  │
   └──┬───┘   └──┬───┘   └──┬───┘   └──┬───┘   └──┬───┘
      │          │          │          │          │
   丢弃+重生成  R1 截断    R2 追加    R2 + 高亮   R4 会话级
   成本：0.4×   成本：1.0×  成本：1.15× 成本：1.2×  成本：2~3×
   用户不可见   用户可见截断 用户可见更正 用户被"打脸" 品牌受损
```

四道闸门的成本逐级上升、体验逐级下降——所以判定逻辑必须**尽早触发**。一个把 80% 的拦截放在 G3 的系统，等于把 80% 的撤回都做成了用户可见的「打脸」。

---

## 37.4 关键设计

### 37.4.1 四种撤回机制与它们的体验代价

| 机制 | 做法 | 可用窗口 | 用户看到什么 | 成本倍数 | 适用场景 | 风险 |
| --- | --- | --- | --- | --- | --- | --- |
| **R1 截断停机** | 立刻终止流，发 `aborted` | 0 ~ 300ms（未 commit） | 半句话 + 「本次回答已中止」 | 1.0× | 严重违规、注入命中、明显跑偏 | 观感差，用户会重试 → 流量放大 |
| **R2 追加更正** | 不删旧片段，插一条 `correction` | 300ms ~ 10s（已 commit 未行动） | 旧内容划删除线/灰化 + 更正块 | 1.15× | **默认机制**，覆盖 80% 场景 | 会话变长，需要前端渲染规范 |
| **R3 全量重发 rebase** | 作废整个 Run 输出，用修正后的上下文重生成 | 任意（但要用户等） | 「正在重新生成…」+ 全新回答 | 2.0× | 前提整体错了（检索到旧文档、上游撤稿） | 双倍延迟；用户会对比两个版本 |
| **R4 会话级撤销** | tombstones 覆盖多轮，置顶横幅 + 主动通知 | 任意 | 「本会话中关于退费的表述有误，已更正」 | 2~3× | 跨轮次污染、政策批量失效 | 品牌损伤最大，但法律上最必要 |

> ⚠️ R3 有一个隐蔽的坑：**rebase 之后的回答可能与用户已经引用的内容不一致**，用户会困惑「到底哪个是对的」。所以 R3 必须保留旧版本可展开（`superseded_by` 链），并且明确标注「以下为更正后的版本」。

### 37.4.2 分级延迟策略：给敏感场景买时间

延迟窗口不是全局开关，是**策略表**。这张表应该放在策略中心（第 43 章）里热更，而不是写死在代码里：

```yaml
# aurora 策略中心：retraction-policy-v14
retraction_policy:
  default:
    channel: draft            # 草稿直发，不额外延迟
    hold_ms: 0
    mechanism: R2
    audit_mode: async         # 事后复核
    read_ack: false

  profiles:
    # 金融/退款/价格承诺：必须走确认流
    billing_commitment:
      match: { intent: ["refund", "pricing", "contract_term"], risk_score: ">= 0.3" }
      channel: confirmed
      hold_ms: 800            # 覆盖审核器 P99 + 聚合窗口
      mechanism: R3           # 前提错了就整段重生成
      audit_mode: sync_blocking
      read_ack: true          # 需要客户端回执才允许下游消费
      max_retract_cost_ratio: 2.0

    # 医疗/法律/资质类：延迟最狠，宁可慢
    regulated_advice:
      match: { agent_type: ["medical", "legal"] }
      channel: confirmed
      hold_ms: 1200
      mechanism: R4
      audit_mode: dual_judge  # 双裁判一致才放行
      read_ack: true

    # 代码生成：只对"可执行/可下发"的部分要确认
    code_agent:
      match: { agent_type: "coding" }
      channel: draft
      hold_ms: 0
      mechanism: R2
      audit_mode: async
      # 例外：涉及 shell 命令的片段走确认流
      overrides:
        - { match: { fragment_kind: "shell_command" }, channel: confirmed, hold_ms: 400 }

    # 闲聊/摘要类：几乎不用延迟
    chitchat:
      match: { intent: ["smalltalk", "summarize"] }
      channel: draft
      hold_ms: 0
      mechanism: R1
      audit_mode: sample_5pct
```

**`hold_ms` 怎么定？** 不是拍脑袋，是算出来的：

```
hold_ms ≈ SegAggWindow + AuditP99 + ClockSkew

SegAggWindow = 聚合窗口，把碎片拼成语义完整的句子才送审（200~500ms）
AuditP99    = 审核器 P99 延迟（小分类器 80~300ms；LLM 审核 800~2000ms）
ClockSkew   = 客户端时钟偏差容忍（给 30ms，用单调 seq 不用时间戳可省掉）

示例（billing_commitment）：
  300ms + 300ms + 0 = 600ms   ← 实测取 800ms 留 33% 余量
```

### 37.4.3 事件协议（可直接进协议文档）

```typescript
// aurora-stream-protocol v1 —— 只列与撤回相关的帧
// 传输：SSE（第 7 章），每个 data 帧是一个 JSON

type Channel = "draft" | "confirmed";
type Mechanism = "R1_truncate" | "R2_append" | "R3_rebase" | "R4_session";

interface AuditStamp {
  verdict: "pass" | "flag" | "block";
  risk: number;              // 0..1
  rules: string[];           // 命中的规则 id，如 "pii.email", "policy.stale_doc"
  auditor: string;           // "classifier:v3" | "llm-judge:gpt-x" | "human:u_882"
  latency_ms: number;
  at: string;                // RFC3339
}

interface BaseEvent {
  v: 1;
  type: string;
  rev: number;      // 单调递增，全局（每 Run 一个计数器）
  seq: number;      // 片段序号，从 1 开始，Run 内唯一
  run_id: string;
  msg_id: string;
}

type ServerEvent =
  | (BaseEvent & {
      type: "fragment.open";
      channel: Channel;
      kind: "text" | "json" | "tool_preview" | "citation";
    })
  | (BaseEvent & { type: "fragment.delta"; text: string })
  | (BaseEvent & {
      type: "fragment.commit";       // 审计通过，可被引用
      audit: AuditStamp;
      first_read_ack: boolean;       // 客户端是否已上报过"已渲染"
    })
  | (BaseEvent & {
      type: "correction";
      mechanism: Mechanism;
      targets: number[];             // 被撤回的 seq 列表（可为多个）
      reason:
        | "hallucination"
        | "stale_source"
        | "policy_change"
        | "safety_block"
        | "upstream_retracted"
        | "user_dispute";
      text?: string;                 // R2/R4：更正文案，为空则用模板
      rebase_run_id?: string;        // R3：新 Run 的 id
      ack_required: boolean;         // 需要用户确认后才继续
      supersede_policy: "hide" | "strike" | "collapse"; // 前端渲染方式
      audit: AuditStamp;
    })
  | (BaseEvent & { type: "run.done"; status: "ok" | "aborted" | "retracted" });
```

一个真实的帧序列（R2 追加更正）：

```json
{"v":1,"type":"fragment.commit","rev":47,"seq":9,"run_id":"run_8f3a","msg_id":"m2",
 "audit":{"verdict":"flag","risk":0.62,"rules":["policy.stale_doc"],
          "auditor":"classifier:v3","latency_ms":214,"at":"2026-05-11T01:47:12Z"},
 "first_read_ack":true}

{"v":1,"type":"correction","rev":48,"seq":9,"run_id":"run_8f3a","msg_id":"m2",
 "mechanism":"R2_append","targets":[9],
 "reason":"stale_source",
 "text":"更正：我引用的协议版本已过期。年付合同现按剩余服务月数比例退款，不是全额退款。",
 "ack_required":true,"supersede_policy":"strike",
 "audit":{"verdict":"block","risk":0.93,"rules":["policy.stale_doc","kb.version_mismatch"],
          "auditor":"kb-version-reconciler","latency_ms":4120,
          "at":"2026-05-11T01:51:04Z"}}
```

> 💡 注意 `rev` 和 `seq` 的分工：`seq` 标识**片段身份**（撤回的 target），`rev` 标识**事件顺序**（幂等与丢弃旧帧）。两个都省不掉。

### 37.4.4 前端处理语义：这是最容易写错的地方

前端不是一个「append 字符串」的循环，它是一个**带墓碑的小型状态机**。规则只有四条，但错一条就会出现「撤回的内容复活」：

| 事件 | 前置条件 | 状态迁移 | 渲染 |
| --- | --- | --- | --- |
| `fragment.delta` | `rev > local.rev` 且 `seq ∉ tombstones` | pending → pending | 追加文本（draft 通道加灰显/光标） |
| `fragment.delta` | `seq ∈ tombstones` | — | **丢弃**（不复活） |
| `fragment.commit` | `seq ∉ tombstones` | pending → committed | 去掉灰显，允许被引用/复制 |
| `correction` | 总是应用 | `targets` → superseded，并写入 tombstones | 按 `supersede_policy` 渲染 |

```typescript
// 客户端 reducer（核心逻辑，骨架级）
type FragState = "pending" | "committed" | "superseded" | "aborted";

interface Store {
  rev: number;
  frags: Map<number, { text: string; state: FragState; channel: Channel; correctedBy?: number }>;
  tombstones: Set<number>;
  corrections: number[];   // 渲染成更正块，按 rev 排序
}

function applyEvent(s: Store, ev: ServerEvent): Store {
  // 规则 1：旧事件直接丢（断线重连场景下尤其重要）
  if (ev.rev <= s.rev && ev.type !== "fragment.commit") return s;

  switch (ev.type) {
    case "fragment.open":
      if (s.tombstones.has(ev.seq)) return s;              // 不复活
      s.frags.set(ev.seq, { text: "", state: "pending", channel: ev.channel });
      break;

    case "fragment.delta": {
      // 规则 3：墓碑优先于增量
      if (s.tombstones.has(ev.seq)) return s;
      const f = s.frags.get(ev.seq);
      if (f) f.text += ev.text;
      break;
    }

    case "fragment.commit": {
      if (s.tombstones.has(ev.seq)) return s;
      const f = s.frags.get(ev.seq);
      if (f) f.state = "committed";
      break;
    }

    case "correction": {
      // 规则 4：correction 永远应用，即使 rev 更旧（防止漏撤回）
      for (const t of ev.targets) {
        s.tombstones.add(t);
        const f = s.frags.get(t);
        if (f) { f.state = "superseded"; f.correctedBy = ev.rev; }
      }
      s.corrections.push(ev.rev);
      if (ev.ack_required) showCorrectionDialog(ev);        // 阻塞式确认
      break;
    }

    case "run.done":
      if (ev.status === "aborted")
        s.frags.forEach(f => { if (f.state === "pending") f.state = "aborted"; });
      break;
  }
  s.rev = Math.max(s.rev, ev.rev);
  return s;
}
```

**重连时的两条铁律**（对应 §37.1 的单调定律）：

1. `Last-Event-ID` 重放只能从服务端**已确认的 rev** 开始，服务端必须返回 `corrections_snapshot`（当前所有墓碑的紧凑表示），客户端先应用快照再应用增量。否则：你重连的这 3 秒里发生的撤回会丢失。
2. 客户端**永远不能自己删消息**。所有 `supersede` 必须由服务端的 `correction` 事件驱动——包括 R4 会话级撤销，也是发一条带大范围 `targets` 的 correction，不是前端清屏。

### 37.4.5 后端：审计 + 撤回决策伪代码

```go
// Retractor 是撤回决策的唯一入口。
// 铁律：它只产出 correction 事件，绝不改动已落库的 fragment。
type Retractor struct {
    store    FragmentStore   // append-only
    bus      EventBus        // 发 correction
    policy   PolicyResolver  // 策略中心（第 43 章）
    clock    Clock
}

type Frag struct {
    RunID, MsgID  string
    Seq           int64
    Text          string
    Channel       Channel
    State         FragState
    CtxHash       string   // 上下文快照哈希 → 能追溯到"当时 context 里有什么"
    ModelVer      string
    PromptVer     string
    RoleCardVer   string
    Audit         AuditStamp
    FirstReadAt   time.Time // 客户端上报的首次渲染时间（零值=未知）
}

// OnVerdict 在四道闸门的任意一处被调用，gate 由调用方给出。
func (r *Retractor) OnVerdict(ctx context.Context, f *Frag, v AuditStamp, gate Gate) error {
    if v.Verdict == "pass" {
        return nil
    }
    p := r.policy.For(ctx, f.RunID)

    // G1：还没 flush，最便宜 —— 丢弃并让上层重生成
    if gate == GatePreSend {
        return r.discardAndRegen(ctx, f, v)   // 用户完全看不到
    }

    // G2：已 flush 但未 commit —— 截断
    if gate == GateInFlight || f.State == StateHolding {
        return r.emit(ctx, f, MechanismR1, []int64{f.Seq}, ReasonFrom(v))
    }

    // G3：已 commit —— 看用户有没有读到
    readRatio := r.readRatio(ctx, f, p)
    switch {
    case readRatio < 0.2 && v.Risk < 0.8:
        // 用户基本没看：R2 追加更正，代价最低
        return r.emit(ctx, f, MechanismR2, []int64{f.Seq}, ReasonFrom(v))
    case v.Rules.Has("policy.stale_doc"), v.Rules.Has("upstream_retracted"):
        // 前提整体错了：必须整段重生成
        return r.rebase(ctx, f, v)
    case readRatio >= 0.2, v.Risk >= 0.8:
        // 用户看过了 / 高风险：R2 + 强提示 + 通知
        return r.emitWithAck(ctx, f, MechanismR2, []int64{f.Seq}, ReasonFrom(v))
    default:
        return r.escalate(ctx, f, v) // 进人工队列
    }
}

// readRatio 估算"用户读到了多少"。三个信号，优先级从高到低：
//   1) 客户端 first_read_ack（IntersectionObserver 上报）—— 最准
//   2) 同 msg 后续用户输入（用户回了话 = 前面的都读了）
//   3) 时间衰减：flush 后多久（>10s 按已读算）
func (r *Retractor) readRatio(ctx context.Context, f *Frag, p Policy) float64 {
    if !f.FirstReadAt.IsZero() {
        return 0.95
    }
    if r.sawUserReplyAfter(ctx, f.MsgID) {
        return 0.8
    }
    if d := r.clock.Since(f.CommittedAt); d < 10*time.Second {
        return 0.2
    }
    return 0.6
}

// 会话级撤销：一次覆盖多轮，用于政策变更 / 跨轮污染
func (r *Retractor) RetractSession(ctx context.Context, sessID string, targets []int64, reason Reason) error {
    // 1. 先算影响面（dry-run 必做，别问为什么）
    impact, err := r.store.Impact(ctx, sessID, targets)
    if err != nil { return err }
    if impact.Fragments > 200 || impact.Messages > 20 {
        return fmt.Errorf("impact too large (%d frags), require human approval", impact.Fragments)
    }
    // 2. 发一条 correction，targets 覆盖全部
    // 3. 写审计链（哈希链，见第 31 章）
    // 4. 若 readRatio > 0.5，触发主动通知（邮件/IM/工单）
    return r.emitWithAck(ctx, &Frag{RunID: sessID}, MechanismR4, targets, reason)
}
```

### 37.4.6 审计链：撤回必须能回答「凭什么」

撤回本身就是一次需要被审计的操作。片段级的审计记录长这样（ClickHouse DDL，详见第 45 章）：

```sql
CREATE TABLE output_fragment_audit (
    ts             DateTime64(3),
    tenant_id      LowCardinality(String),
    run_id         String,
    msg_id         String,
    seq            UInt32,
    rev            UInt64,

    -- 内容指纹（不存明文时用于核对，见第 31 章）
    text_sha256    FixedString(64),
    text_len       UInt32,

    -- 当时的环境快照：这是"凭什么这么答"的唯一证据
    ctx_hash       FixedString(64),      -- 上下文快照哈希
    ctx_tokens     UInt32,
    retrieval_ids  Array(String),        -- 命中的文档 id + 版本
    model_ver      LowCardinality(String),
    prompt_ver     LowCardinality(String),
    role_card_ver  LowCardinality(String),
    policy_ver     LowCardinality(String),  -- 撤回策略版本！

    -- 判定
    verdict        Enum8('pass'=1,'flag'=2,'block'=3),
    risk           Float32,
    rules          Array(String),
    auditor        String,               -- classifier:v3 / llm-judge / human:u_882
    audit_latency_ms UInt32,
    reviewer       Nullable(String),     -- 人工复核者（为空=纯机器）
    reviewer_note  Nullable(String),

    -- 动作
    gate           Enum8('pre_send'=1,'in_flight'=2,'post_commit'=3,'session'=4),
    mechanism      Enum8('none'=0,'R1'=1,'R2'=2,'R3'=3,'R4'=4),
    read_before    Bool,                 -- 撤回时用户是否已读
    effective_ms   UInt32,               -- 判定 → 客户端生效的时延
    superseded_by  UInt64                -- 指向更正事件的 rev
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (tenant_id, ts, run_id, seq)
TTL ts + INTERVAL 3 YEAR;   -- 合规留存期，见第 31 章
```

> 🩸 **`policy_ver` 这一列别省**。撤回策略本身会变（阈值调了、规则加了一条）。半年后有人问「为什么这条撤了那条没撤」，没有策略版本你答不上来。

### 37.4.7 事后召回与批量下线

政策变更和上游撤稿是**批量场景**，机制完全不同：不是「撤回一条」，是「找到所有受影响的历史输出，分级处置」。

```
① 影响面查询（分钟级）
   kb 文档 d 被标记 stale
   → 从 retrieval_ids 反查所有引用过 d 的片段
   → 按 tenant / 时间窗 / 是否已读 / 是否承诺型 分组

② 分级处置（不能一刀切）
   ┌───────────────────────────────────────────────────────────┐
   │ L0 未读（read_before=false）  → 静默 tombstone，不发通知   │
   │ L1 已读、无行动               → 会话内置顶横幅 + R4        │
   │ L2 已读、已行动（有后续操作） → 主动通知（邮件/IM）+ 工单  │
   │ L3 已产生赔付/合同动作        → 人工介入 + 法务复核        │
   └───────────────────────────────────────────────────────────┘

③ 执行（异步、限速、可中断）
   每租户限速 50 QPS，全局 200 QPS —— 否则一次性召回 12 万条
   会把前端 correction 推送打爆（第 23 章）

④ 留痕
   生成 recall_job 记录：影响面、处置分级、通知量、完成率
```

关键 SQL（反查受影响片段，ClickHouse）：

```sql
SELECT
    tenant_id,
    count()                                              AS affected_frags,
    countIf(read_before)                                 AS read_frags,
    countIf(has(rules, 'commitment_language'))           AS commitment_frags,
    min(ts) AS first_seen, max(ts) AS last_seen
FROM output_fragment_audit
WHERE ts >= now() - INTERVAL 180 DAY
  AND has(retrieval_ids, 'kb_doc_77321@v2024-11')   -- 过期文档的 id@version
  AND verdict != 'block'
GROUP BY tenant_id
HAVING affected_frags > 0
ORDER BY commitment_frags DESC, read_frags DESC;
```

> ⚠️ 这条查询在 Aurora 上扫 180 天 × 30 亿行，实测 4~9 秒（靠 `retrieval_ids` 的 bloom filter 跳索引）。**必须做成异步任务 + 预估影响面**，不要在产品界面上做成同步按钮——你已经看过太多「点一下卡 10 秒」的后台了。

### 37.4.8 已读撤回：法律与体验的三条路

撤回发生在用户已经读过之后，你面临一个纯产品/法务决策：

| 策略 | 用户看到 | 法律姿态 | 体验 | 建议 |
| --- | --- | --- | --- | --- |
| **静默删除** | 无变化（刷新后没了） | 最差：无法证明你纠正过 | 表面最好，实为埋雷 | ❌ 禁止 |
| **标记更正** | 旧内容划删除线 + 更正块 | 最好：完整的纠错留痕 | 略打脸，但可接受 | ✅ 默认 |
| **主动通知** | 站内信/邮件/IM 推送 | 最强：证明已履行告知义务 | 打扰，但严重错误必须 | ✅ 仅限 L2 以上 |

监管侧的保守原则（具体条款以官方文档最新版为准）：

- EU AI Act 对生成式系统有**透明度与信息准确性**相关义务，纠错留痕是有利证据。
- 《生成式人工智能服务管理暂行办法》要求对生成内容做标识、发现违法内容及时处置——「及时处置」在技术上就是本章的 R1/R4。
- GDPR 的「被遗忘权」与「审计留存」是**冲突的**：这里要做「内容删除 + 事件留痕（只留哈希与判定）」的分层，详见第 31 章。

**一句话判据**：如果这段错误内容会被用户拿去做**有金钱后果的决定**（退款、报价、合同、医疗建议），就必须主动通知；否则标记更正即可。

### 37.4.9 撤回指标：四个数决定这套系统是否可信

| 指标 | 定义 | Aurora 目标 | 失控信号 |
| --- | --- | --- | --- |
| **撤回率** `retraction_rate` | 撤回片段 / 已 commit 片段 | < 0.4% | > 1.2% → 模型或检索出了系统性问题 |
| **误撤回率** `false_retraction_rate` | 人工复审判「不该撤」/ 撤回总数 | < 8% | > 20% → 审核器太保守，用户在陪跑 |
| **已读撤回率** `read_before_retract_rate` | 撤回时用户已读 / 撤回总数 | < 25% | > 40% → 你的闸门全在 G3，延迟窗口没用上 |
| **撤回生效时延** `retract_effective_p99` | 判定 → 客户端渲染生效 | < 800ms | > 2s → 前端/推送链路有问题 |
| **撤回预算消耗** | 当月撤回次数 / 预算 | < 70% | > 100% → **冻结发布**（类比错误预算） |

最后一条是本章的运营抓手：**把撤回次数做成错误预算（error budget）**。一旦某月撤回率突破预算，自动冻结提示词/模型发布，逼团队先修根因。这比任何 dashboard 都有效，因为它有牙齿。

```sql
-- 撤回质量看板：按场景 × 小时，找出"撤回大户"
SELECT
    toStartOfHour(ts)                                              AS h,
    tenant_id,
    JSONExtractString(payload, 'scenario')                         AS scenario,
    countIf(mechanism != 'none')                                   AS retracted,
    countIf(read_before)                                           AS retracted_after_read,
    round(retracted_after_read / nullIf(retracted, 0), 4)          AS read_before_rate,
    quantile(0.99)(effective_ms)                                   AS p99_effective_ms,
    groupUniqArray(5)(arrayJoin(rules))                            AS top_rules
FROM output_fragment_audit
WHERE ts >= now() - INTERVAL 6 HOUR
  AND mechanism != 'none'
GROUP BY h, tenant_id, scenario
HAVING retracted >= 20
ORDER BY retracted_after_read DESC
LIMIT 100;
```

> 💡 还有一个必须算的账：**撤回的 token 收不收费？** 我们的口径是「已生成的 token 照收（成本已发生），但撤回片段不计入质量 SLA，并按月给受影响租户返还 0.5% 额度」。这既诚实又不至于让财务模型崩掉——详见第 27 章（计费）。

---

## 37.5 Java / 分布式经验对照

| 你熟悉的东西 | Agent 撤回的对应物 | 会咬人的差异 |
| --- | --- | --- |
| 数据库事务 rollback | ❌ **不存在**。已 flush 的输出等于已 commit | 只能用**补偿事务（Saga）**——R2/R3/R4 本质都是 compensating event |
| Kafka 消费 offset 回退重放 | 事件重放 | 危险！重放会让**已撤回内容复活**（违反单调定律）。必须带 tombstone 快照 |
| 灰度回滚 / 配置热更还原 | R3 rebase | 回滚是「回到旧版本」，rebase 是「用新前提重新生成」，后者要再付一次推理费 |
| 幂等键去重 | `(run_id, seq, rev)` 三层 | 传统幂等只防重复执行；这里还要防**重复渲染已撤回内容** |
| Sentinel 熔断 | 撤回预算 | 熔断保护下游；撤回预算保护**品牌与法务**，触发的动作是冻结发布 |
| 消息队列的死信队列 | 人工复核队列 | 撤回误判的片段进人工队列，**误撤回率**从这里统计 |
| 分布式事务 TCC | 四道闸门 | TCC 的 Try 是预留资源；撤回的 G1/G2 是「在 commit 前拦」，思路同构 |
| 乐观锁 version 字段 | `rev` | 一样，但 `rev` 是 Run 级单调计数器，不是行级 |

最大的认知差异：**Java 后端的肌肉记忆是「出错了就回滚」，Agent 平台的现实是「出错了只能追加事实」**。所有把撤回实现成 `UPDATE/DELETE` 的方案，最后都会在审计和取证上崩掉。

---

## 37.6 开源坐标：这块基本是空白

| 项目 | 有什么 | 缺什么 |
| --- | --- | --- |
| **Vercel AI SDK** | `useChat` 的消息状态、`message.parts` 可增量更新、`onError` 处理 | 没有 correction 语义、没有版本号、没有墓碑；撤回要自己在 reducer 里造 |
| **AG-UI 协议** | 有 `RUN_ERROR`、`STATE_DELTA` 这类事件，明确把「Agent ↔ 前端」的状态同步协议化（具体事件名以官方文档最新版为准） | 没有「已渲染内容替换」的标准语义，也没有 `rev`/tombstone |
| **OpenAI Responses API** | 有 response 对象与状态查询，可以拿来做「重新生成」 | 不解决「客户端已渲染的内容怎么办」 |
| **LangGraph** | `interrupt()` + checkpointer 可以做「暂停/恢复/重跑某节点」 | 这是**运行时的撤回**，不是**已输出内容的撤回**；两者常被混淆 |
| **Temporal** | 补偿（Saga）、`WorkflowId` 幂等、signal | 补偿的是**副作用**（发邮件/下单），不补偿「用户读到的文字」 |
| **内容审核系统**（各家云厂商） | 同步/异步审核、人工复核队列 | 只给 verdict，不给「怎么把已发的改掉」 |
| **工单/客服系统**（Zendesk 类） | 消息撤回、编辑留痕 | 单条消息级，不理解「片段」和「流式」 |

> 🎯 结论：**「流式输出的撤回」在开源生态里没有现成答案。** 你要自己定义协议（§37.4.3）、自己写前端 reducer（§37.4.4）、自己建审计表（§37.4.6）。这块代码大概 3000~5000 行，但它决定了你的产品能不能被企业客户信任。这也是全书反复说的那句话：**开源把能力做完了，把治理留给你了。**

---

## 37.7 事故清单

- [ ] **事故 A：撤回内容复活**。现象：用户断线重连后，之前被更正的错误回答又出现了。
  → 根因：重放时按 `Last-Event-ID` 拉了旧 `delta`，客户端没有 tombstone 集合。
  → 兜底：`corrections_snapshot` + 墓碑优先规则（§37.4.4 规则 3），并加一条回归测试「撤回后重连不应出现原文」。
- [ ] **事故 B：撤回风暴**。现象：一次知识库批量下线触发 12 万条 correction，前端卡死、IM 通知把用户手机打爆。
  → 根因：批量召回没限速、没分级，L0/L1/L2 一视同仁。
  → 兜底：分级处置 + 每租户 50 QPS 限速 + L2 以上才发外部通知。
- [ ] **事故 C：静默改写被截图对质**。现象：客户拿着两张不同截图投诉「你们改我的记录」。
  → 根因：某次「小修改」用了 `UPDATE`。
  → 兜底：DB 层禁止对 `output_fragment` 做 UPDATE/DELETE（用权限 + append-only 表引擎强制），走后置钩子审计。
- [ ] **事故 D：审核器把正常输出全撤了**。现象：误撤回率飙到 34%，用户看到满屏删除线。
  → 根因：新审核模型阈值调太严，且没有灰度。
  → 兜底：撤回策略本身要灰度（第 42 章）+ 误撤回率纳入发布门禁（第 40 章）。
- [ ] **事故 E：撤回时延 8 秒**。现象：判定早就算完了，用户 8 秒后才看到更正。
  → 根因：correction 走了异步消息队列的低优先级通道，排在批处理后面。
  → 兜底：correction 走独立高优通道，P99 预算 800ms，纳入 SLO。
- [ ] **事故 F：账户里的 token 已计费但内容被撤**。现象：租户投诉「撤掉的内容也收钱」。
  → 根因：计费在 commit 时结算，撤回没走返还流程。
  → 兜底：撤回片段按月汇总返还额度（§37.4.9），并在账本里留 `retraction_adjustment` 分录。

---

## 37.8 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 三条定律 | 不可撤回（只能更正）/ 追加（无 undo，只有补偿事件）/ 单调（重放不能复活） |
| 本质 | 撤回 = 分布式补偿事务，不是 rollback；已 flush 等于已 commit |
| 曲线 | 可撤回度在 1 秒处陡降；300~800ms 窗口买到 45%→95% 的可撤回度 |
| 四种机制 | R1 截断（<300ms）/ R2 追加更正（默认，80% 场景）/ R3 rebase（前提错）/ R4 会话级（跨轮污染） |
| 双通道 | 草稿流（92% 流量，0 延迟）+ 确认流（8% 敏感流量，hold 600~1200ms） |
| 禁止 | 静默改写。一次 UPDATE 毁掉整套取证能力 |
| 协议 | `seq` 标识片段，`rev` 标识顺序；correction 带 `targets[]` + `supersede_policy` |
| 前端 | 墓碑优先于增量；重连先应用 `corrections_snapshot`；客户端永不自己删消息 |
| 审计 | 片段级绑定 `ctx_hash + model/prompt/role/policy 四个版本 + verdict + reviewer`；`policy_ver` 别省 |
| 指标 | 撤回率 <0.4% / 误撤回率 <8% / 已读撤回率 <25% / 生效 P99 <800ms |
| 运营 | 撤回次数做成错误预算，超预算**冻结发布** |
| 一句话 | **能撤回的系统才敢流式；不能撤回的流式，是在拿品牌赌运气。** |

---

## 37.9 自检题

1. **你的流式输出已经 commit 了 9 个片段，第 10 个片段的审核判定是 `block`，但根因是「检索到了过期文档」——导致前面 9 个片段的前提也错了。你怎么撤？**
   > 要点：`block` 只说明第 10 段有问题，但 `policy.stale_doc` 是**前提级错误**，必须走 R3 rebase（整段重生成），而不是只撤第 10 段。同时前端要保留旧版本可展开（`superseded_by` 链），否则用户无法理解为什么答案变了。成本是 2×，但这是唯一正确的选择。

2. **为什么不能只用 `rev` 一个计数器，必须再加一个 `seq`？**
   > 要点：`seq` 是片段身份，撤回事件要用 `targets: [seq...]` 指名道姓地撤某几段；`rev` 是事件顺序，用于丢弃过期帧和幂等判重。一个 `seq` 可能对应 `open` / 多个 `delta` / `commit` / 一个 `correction`，共 N 个事件但只有 1 个身份。混用会导致重连时无法定位要撤哪段。

3. **TTFT 从 900ms 涨到 1700ms 掉 11% 完成率，但撤回率只有 0.3%。你会给全部流量加 800ms 延迟窗口吗？为什么？**
   > 要点：不会。0.3% 的撤回率摊到流量上，全量延迟的期望收益远小于确定性的体验损失。正确做法是分级：只有命中「承诺/合规/高风险」画像的约 8% 流量走确认流，其余走草稿直发 + 事后 R2。用 §37.4.2 的策略表实现，别在代码里写 if-else。

---

## 37.10 延伸阅读

- 上一章：[36. 流式输出工程：从首 token 到增量渲染](36-streaming-output.md)（本章的所有约束都来自流式）
- 下一章：[38. 后处理层：把「差不多」变成「能交付」](38-post-processing.md)（commit 之前的那道闸）
- 相关：[31. 合规、审计与取证](31-compliance-audit.md)（哈希链与留存期）、[34. HITL](34-hitl-approval.md)（人审就是 G4 的延伸）、[43. 策略中心](43-policy-center.md)（撤回策略必须热更）、[6. Session/Run/Event 状态模型](6-session-run-event-model.md)（append-only 的底座）
- 外部：SSE 规范（`Last-Event-ID` 语义）、AG-UI 协议（Agent↔前端事件模型，以官方文档最新版为准）、Vercel AI SDK `useChat`、EU AI Act 透明度条款、《生成式人工智能服务管理暂行办法》
- 总纲：[0-summary.md](0-summary.md)
- 语言/运行时细节：见 [../go/0-summary.md](../go/0-summary.md)
