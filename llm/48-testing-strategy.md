# 48. 测试策略：Agent 系统怎么测

> **状态**：`已完成（初稿）` ｜ **所属**：第九部分 · 平台工程底座 ｜ **编号**：48 / 52
> **本章定位**：第 1 章说「Agent 没法写单元测试」，这一章说**「那就换一种测试」**。三层测试（单元/契约 → 录制回放 → 评测集回归）+ 不确定性治理 + chaos + 发布门禁，目标是把「感觉挺好的」变成「有证据的绿灯」。
> **读法**：如果你是那个在 CI 里加了 `--retry 3` 和 `continue-on-error` 的人，先读 §48.1 和 §48.4.2（录制回放），那两节能救你 80% 的时间。§48.4.6 的门禁表可以直接抄进你们的发布流程。

---

## 48.1 引子：一个逗号、4 个月、和 41% 的假绿灯

**第一天**。有人提了个 PR，标题是「微调客服语气」，diff 只有一行：

```diff
- 你是 Aurora 的客服助手。回答要准确、简洁。
+ 你是 Aurora 的客服助手。回答要准确、简洁、友好。
```

Review 通过，合并，上线。CI 全绿。

**第四天**。客服团队反馈：「它现在经常先寒暄两句再查订单，而且有时候忘记调工具就直接回答了。」工具调用成功率从 **96.2% 掉到 71.4%**。查了两天才定位到这一行——多出来的「友好」两个字，让模型倾向于「先共情再行动」，而共情那段占用了它做工具决策的注意力。

**为什么 CI 没抓到？** 因为 CI 长这样：

```yaml
# 当时的 workflow（反面教材）
- name: agent tests
  run: go test ./agent/... -count=1
  continue-on-error: true        # ← 这句是罪魁祸首
  timeout-minutes: 30
```

`./agent/...` 里有 2,400 个测试，其中 **380 个会真的调模型**。这 380 个的 flaky 率是 **41%**——平均每个测试跑 5 次有 2 次结果不同。团队的处理方式是：先加 `-count=3` 重试，再加 `continue-on-error`，最后在群里约定「agent 那部分红了先不用管」。

**第 42 天**。一个真实的回归被放过了：计费模块把缓存命中的 token 也按全价计了，客单价虚高 18%，三周后财务对账才发现。而这个 bug **本来有一个测试能抓到**——只是它也在那个「红了先不用管」的目录里。

事后复盘的结论只有一句，但很值钱：

> 🩸 **「Agent 测不了」的真正含义不是「没法断言」，而是「你不知道该断言什么，于是断言了最不该断言的东西（字符串相等），然后被 flaky 打败，然后关掉了测试。」**

这一章就干三件事：**把断言换对**、**把不确定性关进笼子**、**把「红绿灯」修好**。

---

## 48.2 断崖：为什么传统测试方法在这里集体失效

| 维度 | 传统后端服务 | Agent 系统 | 后果 |
| --- | --- | --- | --- |
| **输出** | 确定的（同输入同输出） | 概率的（同输入可能不同输出） | `assertEquals` 全军覆没 |
| **正确性** | 可判定（对/错二值） | 语义的（好/较好/不太好） | 需要判定器 + 评分，需要人工校准 |
| **依赖** | DB/缓存/下游 API，可 mock | 模型 API：贵、慢、会变版本、会限流 | mock 必须能模拟「不确定性」本身 |
| **单次成本** | ~0 | $0.001~$0.1 / 次 | 全量回归 = 真金白银，不能每次 PR 跑 |
| **单次耗时** | ms | 3~40 秒 | 2,400 个测试 × 10 秒 = 6.7 小时，CI 直接崩 |
| **失败复现** | 有堆栈、可断点 | 「昨天是好的」 | 必须录制完整上下文才能复现 |
| **外部世界的副作用** | 可以回滚事务 | 工具真的发了邮件/下了单 | 测试环境必须隔离真实工具 |

**算一下账**，你就知道为什么不能「全都跑真模型」：

```
Aurora 评测规模：
  PR 触发一次全量评测 = 200 条 × 1 Run = 200 × $0.0316 ≈ $6.3
  每天 40 次 PR + 20 次合并 = 60 次 × $6.3 = $378/天
  一个月 ≈ $11.3k/月，只为「测试」
  加上开发者本地调试（每人每天 30 次真调用 × 40 人）= $38/天
```

**$11.3k/月不是不能花，问题在于它跑得慢（200 条 × 30 秒 ÷ 并发 20 ≈ 5 分钟）、不稳定（±5% 波动）、且定位差（失败了不知道是哪一行导致的）。** 所以不是「要不要用真模型」，而是**「在哪一层用真模型」**。

---

## 48.3 结构：三层测试金字塔 + 门禁流水线

```
                        ▲ 成本高、慢、接近真实
                       ╱ ╲
                      ╱   ╲   L3 评测集回归（真实模型）
                     ╱ L3  ╲   · 200 条离线集 + 判定器 + 统计阈值
                    ╱───────╲  · 只在上线前/夜间跑，$6~25/次
                   ╱         ╲ · 抓：质量回归、路由降档、提示词 A/B
                  ╱    L2     ╲
                 ╱  录制回放    ╲  L2 录制回放（cassette，确定性）
                ╱  ───────────  ╲ · 真实模型的历史响应，0 成本重放
               ╱                 ╲· 每个 PR 跑，抓逻辑/拼装/流式解析
              ╱        L1         ╲
             ╱   单元 + 契约测试    ╲ L1 单元 + 契约（手写 mock，毫秒级）
            ╱  ───────────────────  ╲· 80% 的测试量，PR 必跑 < 3 分钟
           ╱_________________________╲· 抓：解析、状态机、幂等、计费、脱敏
                        ▼ 快、便宜、确定
```

| 层 | 数量占比 | 单次耗时 | 单次成本 | 确定性 | 能抓什么 | **抓不到什么** |
| --- | --- | --- | --- | --- | --- | --- |
| **L1 单元/契约** | ~80% | < 10ms | $0 | 100% | 事件序列化、流式增量解析、工具参数校验、幂等键、计费公式、状态机非法迁移、脱敏规则 | 模型行为、提示词质量 |
| **L2 录制回放** | ~15% | 10~200ms | $0（首次录制有成本） | 100% | 多步编排逻辑、工具调用拼装、上下文装配顺序、缓存键正确性、降级分支 | 模型在新输入上的表现 |
| **L3 评测集回归** | ~5% | 3~40s/条 | $0.001~0.1/条 | 统计意义 | 质量回归、幻觉率、工具正确率、角色一致性、成本/延迟漂移 | 「未知的未知」（要靠线上采样与对抗集） |

> 💡 **三层的分工不是「细到粗」，而是「确定性到概率」**。L1/L2 是**门禁**（必须 100% 绿，红了就是红了）；L3 是**指标**（看通过率与置信区间，允许小幅波动）。把这两类信号混在一个 CI 面板里，是 90% 团队放弃测试的起点。

**门禁流水线**：

```
 git push
    │
    ▼
 ┌──────────────┐  < 3 min   ┌───────────────┐  < 8 min   ┌────────────────┐
 │ L1 单元/契约  │──────────▶│ L2 录制回放    │──────────▶│ L3-smoke (40条) │
 │ 100% 必须绿   │   fail→❌  │ 100% 必须绿    │  fail→❌   │ 通过率 ≥ 95%    │
 └──────────────┘            └───────────────┘            └───────┬────────┘
                                                                  │ 合并
                                                                  ▼
 ┌───────────────────────────────────────────────────────────────────────┐
 │ 预发布环境：L3-full (200 条) + chaos 套件 + 金丝雀 1% 流量 30 分钟        │
 │ 门禁：质量不降（±1pt 内）、P99 延迟不升 10%、单 Run 成本不升 5%            │
 └───────────────────────────────┬───────────────────────────────────────┘
                                 │ 通过
                                 ▼
                        灰度 5% → 25% → 100%（第 42 章）
```

---

## 48.4 关键设计

### 48.4.1 mock 模型的三种姿势：别用「假模型」测「真逻辑」

| 类型 | 做法 | 适合测什么 | 陷阱 |
| --- | --- | --- | --- |
| **Scripted（脚本化）** | 按调用顺序返回预设响应 | 状态机、重试、降级、循环上限 | 脚本和真实响应格式不一致 → 测过了线上挂 |
| **Fixture（样本库）** | 从固定 JSON 样本里挑，按 `hash(messages)` 匹配 | 上下文装配、模板渲染、结构化输出解析 | 样本过时，变成「测的是三个月前的模型」 |
| **Recorded（录制回放）** | 真跑一次，把请求/响应存成 cassette，之后重放 | 端到端编排逻辑 | **改 prompt 后全部 miss**；自动重录会把 bug 一起录进去 |

```go
// testkit/fakellm.go —— L1 用的 scripted mock，实现和真实 provider 同一个接口
type ChatClient interface {
    Chat(ctx context.Context, req ChatRequest) (ChatResponse, error)
}

type ScriptedLLM struct {
    mu    sync.Mutex
    steps []Step          // 按序消费
    calls []ChatRequest   // 记录，供断言
}

type Step struct {
    Resp  ChatResponse    // 要返回的内容
    Err   error           // 或者返回错误（测重试/降级）
    Delay time.Duration   // 或者慢（测超时）
}

func (s *ScriptedLLM) Chat(ctx context.Context, req ChatRequest) (ChatResponse, error) {
    s.mu.Lock()
    defer s.mu.Unlock()
    s.calls = append(s.calls, req)
    if len(s.steps) == 0 {
        return ChatResponse{}, fmt.Errorf("scripted: unexpected call #%d", len(s.calls))
        // ↑ 关键：多余调用要报错，否则「多循环了一圈」这种 bug 永远抓不到
    }
    st := s.steps[0]
    s.steps = s.steps[1:]
    if st.Delay > 0 {
        select {
        case <-time.After(st.Delay):
        case <-ctx.Done():
            return ChatResponse{}, ctx.Err()
        }
    }
    return st.Resp, st.Err
}
```

> ⚠️ **mock 必须和真实 provider 共用同一个 interface 和同一份 DTO**（第 26 章的模型适配层）。如果 mock 自己定义了一套「简化版 response」，那你测的是 mock，不是系统。这是 L1 测试最常见的自欺。

### 48.4.2 录制回放：把「一次真实的 Run」变成可重放的资产

原理和 Java 的 WireMock / Ruby 的 VCR 一样：**拦截 HTTP 层，把请求当 key、响应当 value 存起来；下次遇到同一个 key 直接返回存档**。

Aurora 的 cassette 设计（三条硬规则）：

1. **key = hash(provider, model_id, messages, tools_schema, temperature, seed, prompt_ver)**。`prompt_ver` 必须进 key——否则改了提示词还命中老录像，你会得到「改了没变化」的假象。
2. **默认 fail-closed**：没命中就**报错**，不自动调真实模型。CI 里静默调真模型 = 账单事故 + 不确定性回流。
3. **cassette 进 Git，按 `scene/prompt_ver` 分目录**，PR 里能看到 diff——**响应的变化本身就是一种可 review 的产出**。

```go
// testkit/cassette.go
type Cassette struct {
    dir   string
    mode  Mode // ModeReplay | ModeRecord | ModePassthrough
    real  ChatClient
    mu    sync.Mutex
    seen  map[string]int
}

type entry struct {
    Key      string       `json:"key"`       // 见上面的 key 定义
    PromptVer string      `json:"prompt_ver"`
    Request  ChatRequest  `json:"request"`
    Response ChatResponse `json:"response"`
    Usage    Usage        `json:"usage"`
    Recorded time.Time    `json:"recorded_at"`
    Model    string       `json:"model"`     // 真实模型 id + 版本，方便排查漂移
}

func (c *Cassette) Chat(ctx context.Context, req ChatRequest) (ChatResponse, error) {
    key := cassetteKey(req)

    c.mu.Lock()
    n := c.seen[key]      // 同名调用第几次（循环场景必须区分）
    c.seen[key]++
    c.mu.Unlock()

    path := filepath.Join(c.dir, fmt.Sprintf("%s.%d.json", key[:16], n))

    if c.mode != ModeRecord {
        b, err := os.ReadFile(path)
        if err != nil {
            if c.mode == ModePassthrough {
                return c.real.Chat(ctx, req)
            }
            // fail-closed：CI 里绝不允许静默走真模型
            return ChatResponse{}, fmt.Errorf(
                "cassette miss: %s (prompt_ver 变了？tools schema 改了？)\n"+
                    "确认行为变化符合预期后，用 RECORD=1 重录并 review diff", path)
        }
        var e entry
        if err := json.Unmarshal(b, &e); err != nil {
            return ChatResponse{}, err
        }
        return e.Response, nil
    }

    // ModeRecord：真跑，落盘
    resp, err := c.real.Chat(ctx, req)
    if err != nil {
        return resp, err
    }
    b, _ := json.MarshalIndent(entry{
        Key: key, Request: req, Response: resp,
        Recorded: time.Now().UTC(), Model: req.Model,
    }, "", "  ")
    _ = os.MkdirAll(c.dir, 0o755)
    if err := os.WriteFile(path, b, 0o644); err != nil {
        return resp, err
    }
    return resp, nil
}
```

**记录一次真实 Run 的正确姿势**（这段很重要，抄就完了）：

```bash
# 从线上 trace 里捞一次真实 Run 的完整请求链，重放成 cassette
# ① 从 trace 存储导出（第 39 章），带 prompt_ver / tools_ver / 索引快照
aurora-cli trace export --run run_01HX8... --out ./testdata/scenes/cs_refund_01/
# ② 脱敏（PII 必须处理，第 31 章）
aurora-cli trace scrub --in ./testdata/scenes/cs_refund_01/ --profile pii-cn
# ③ 用录制模式跑一遍，生成 cassette
RECORD=1 go test ./agent/e2e -run TestRefundFlow -tags=cassette
# ④ ★ 人工 review cassette 的 diff（这是唯一能发现「模型行为漂移」的时机）
git diff ./testdata/scenes/cs_refund_01/
```

> 🩸 **最阴的一个坑**：为了让 CI「绿得快点」，有人把 miss 时改成「自动调真模型并顺便录制」。结果：一次带 bug 的代码跑出了错误响应，被录进 cassette；之后三个月，所有测试都在**证明这个 bug 是正确的**。**cassette 的写入必须只发生在人工显式 `RECORD=1` 时。**

### 48.4.3 判定式断言：五类断言，替代 `assertEquals`

| 断言类型 | 例子 | 抓什么 | 什么时候不能用 |
| --- | --- | --- | --- |
| **① Schema** | JSON Schema 校验输出必须含 `{"action","order_id","reply"}` | 结构化输出崩坏、字段缺失 | 自由文本场景 |
| **② 实体/事实** | 输出必须包含订单号 `A1B2`，且金额等于输入里的金额 | 幻觉、张冠李戴 | 生成式创作 |
| **③ 禁止项** | 不得出现「保证退款」「100%」、不得出现其他租户名 | 越权承诺、数据泄露 | — |
| **④ 工具轨迹** | 调用序列必须是 `[get_order, get_logistics]`，且 `get_order` 参数 `order_id` 正确 | 该调没调、调错工具、参数幻觉 | 允许自由探索的场景 |
| **⑤ 统计** | N=20 次里 ≥18 次满足 ①~④ | 概率性失败 | 单次跑（成本） |

```go
// agent/assert.go —— 判定式断言（这一节最值得抄的 40 行）
type Judgement struct {
    SchemaOK   bool
    MissingEntities []string
    ForbiddenHits   []string
    ToolTraceOK bool
    ToolTrace   []string
}

func Judge(out Output, want Spec) Judgement {
    j := Judgement{SchemaOK: true, ToolTraceOK: true}

    // ① schema
    if err := want.Schema.Validate(out.Raw); err != nil {
        j.SchemaOK = false
    }
    // ② 必须出现的实体（订单号、金额、时间）
    for _, e := range want.MustContain {
        if !strings.Contains(out.Text, e) {
            j.MissingEntities = append(j.MissingEntities, e)
        }
    }
    // ③ 禁止项（合规话术、越权承诺、其他租户数据）
    for _, f := range want.MustNotContain {
        if strings.Contains(out.Text, f) {
            j.ForbiddenHits = append(j.ForbiddenHits, f)
        }
    }
    // ④ 工具轨迹：允许「顺序无关的子集匹配」，不要写死全序（模型会并行调用）
    if !isSubsequence(want.ToolSeq, out.Tools) {
        j.ToolTraceOK = false
    }
    j.ToolTrace = out.Tools
    return j
}

// ⑤ 统计断言：单次通过不算通过
func AssertPassRate(t *testing.T, n, minPass int, run func(i int) error) {
    pass := 0
    for i := 0; i < n; i++ {
        if run(i) == nil {
            pass++
        }
    }
    if pass < minPass {
        t.Fatalf("pass rate %d/%d < required %d/%d (这是概率性失败，先看分布再看单例)",
            pass, n, minPass, n)
    }
}
```

> 💡 **工具轨迹断言要用「子序列」而不是「全等」**：模型经常会并行调 `get_order` 和 `get_user`，顺序不稳定。你写成全等，40% 的概率假红。这是 L3 flaky 的第一大来源。

### 48.4.4 工具与 MCP 的契约测试：不测模型，测边界

模型会变，**但你和工具之间的契约不该变**。这一层是纯 L1，投入产出比最高：

| 契约项 | 断言 | 不测会怎样 |
| --- | --- | --- |
| **JSON Schema 合法性** | `inputSchema` 能被严格校验器解析，`required` 与 `properties` 一致 | 模型生成的参数被静默丢弃 |
| **描述长度与歧义** | `description` ≤ 300 字符、不含歧义词、每个参数有类型与示例 | 模型选错工具（第 22 章的「工具描述注入」） |
| **错误码约定** | 业务错误返回 `isError:true` + 结构化 `code`，不抛裸异常 | 模型把「查无此单」当成「系统故障」开始重试 |
| **超时与取消** | 传 `context.WithTimeout`，超时后工具真的停下、返回可识别错误 | goroutine 泄漏 + 成本翻倍 |
| **幂等** | 同 `idempotency_key` 调两次，副作用只发生一次 | 重复下单（第 8 章） |
| **结果大小** | 返回 > 32KB 时自动截断 + 摘要，且带 `truncated:true` | 上下文爆窗（第 15 章） |
| **副作用分级** | 写类工具在测试环境必须指向 mock 后端（Testcontainers 起的假服务） | 测试环境给真客户发邮件 |

```go
// tools/contract_test.go —— 对每个注册的工具/MCP server 跑同一套契约
func TestToolContracts(t *testing.T) {
    for _, tl := range registry.All() {
        t.Run(tl.Name(), func(t *testing.T) {
            // ① schema 必须严格合法，且每个参数都有描述
            require.NoError(t, jsonschema.Validate(tl.InputSchema))
            require.LessOrEqual(t, len(tl.Description), 300, "描述过长会挤占上下文预算")

            // ② 副作用分级必须有标注，未标注直接失败（防呆）
            require.NotEmpty(t, tl.SideEffect, "工具必须声明 read/write/dangerous")

            // ③ 超时必须能被取消（用一个故意 sleep 的 fake server）
            ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
            defer cancel()
            _, err := tl.Call(ctx, map[string]any{"order_id": "A1B2"})
            require.ErrorIs(t, err, context.DeadlineExceeded)

            // ④ 大结果必须截断
            big, _ := tl.Call(context.Background(), map[string]any{"order_id": "BIG"})
            require.LessOrEqual(t, len(big.Content), 32*1024)
            require.True(t, big.Truncated)

            // ⑤ 写工具幂等
            if tl.SideEffect != "read" {
                k := "test-" + uuid.NewString()
                _, e1 := tl.Call(withIdem(context.Background(), k), writeArgs)
                _, e2 := tl.Call(withIdem(context.Background(), k), writeArgs)
                require.NoError(t, e1); require.NoError(t, e2)
                require.Equal(t, 1, fakeBackend.CountWrites(), "重复副作用！")
            }
        })
    }
}
```

MCP server 的契约测试用 **Testcontainers 起真容器**（不要 mock stdio），验证 `initialize` → `tools/list` → `tools/call` 三步，以及 `notifications/cancelled` 是否被正确处理。第三方 MCP server 还要额外测「恶意描述」（第 30、32 章）。

### 48.4.5 chaos：不确定性之外，还有坏天气

L3 测的是「模型答得好不好」，chaos 测的是「下游坏了你塌不塌」。Aurora 每周三凌晨跑一次，注入点全部在**网关层**，不改业务代码：

| 故障 | 注入方式 | 期望行为 | 判定 |
| --- | --- | --- | --- |
| 模型超时 | 网关延迟 +30s（Toxiproxy / 自定义 middleware） | 按超时预算降级到小模型或兜底话术，**不重试到超时叠加** | P99 < 20s，`degraded=true` 指标 +1 |
| 模型 429 限流 | 返回 429 + `Retry-After: 2` | 退避重试 ≤ 2 次，然后降档；不打出重试风暴 | 出站 QPS 不超过基线 1.2× |
| 模型 5xx | 返回 500 | 熔断 30s，切备用 provider | 成功率 > 98%（failover 生效） |
| 流式中断 | 流到第 50 个 token 后断连接 | 已输出内容保留 + 可续传（`Last-Event-ID`） | 客户端不出现半句截断 |
| 工具慢 | 工具延迟 5s | 超时 + 降级，不阻塞整条 Run | Run 成功率 > 95% |
| 工具返回垃圾 | 返回截断 JSON | 解析失败 → 重试 1 次 → 跳过该工具并说明 | 不抛未捕获 panic |
| Redis 挂 | 停掉缓存 | 直连 DB，命中率归零但服务可用 | 可用性 > 99%，延迟 < 2× |
| 队列积压 | 灌 10 倍流量 | 公平调度生效、免费层先降级 | 付费租户 P99 < 2× 基线 |
| 沙盒逃逸尝试 | 在沙盒里跑已知逃逸样本 | 被 seccomp/网络策略拦住并告警 | **零**逃逸，审计有记录 |

```yaml
# chaos/experiments.yaml（Chaos Mesh 风格，节选）
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata: { name: llm-timeout-drill, namespace: aurora-test }
spec:
  action: delay
  mode: one
  selector: { labelSelectors: { app: model-gateway } }
  delay: { latency: "30s", jitter: "2s" }
  duration: "10m"
  scheduler: { cron: "@weekly" }
---
# 模型层故障用「环境变量开关」而不是网络故障，更可控
# MODEL_FAULT=429:0.3,timeout:0.1,truncate:0.05  （30% 限流 / 10% 超时 / 5% 流截断）
```

### 48.4.6 发布门禁：什么情况必须拦住发布

| 阶段 | 跑什么 | 阈值 | 不达标怎么办 |
| --- | --- | --- | --- |
| PR | L1 + L2 | 100% 通过（**零容忍，无 retry**） | 阻断合并 |
| PR | L3-smoke（40 条精选） | 通过率 ≥ 95%，且不低于基线 -2pt | 阻断合并，除非 PR 标注 `eval-exempt` + 双人审批 |
| 合并后 | L3-full（200 条） | 通过率不低于基线 -1pt；工具正确率 ≥ 基线 -1pt；幻觉率 ≤ 基线 +0.5pt | 阻断发布 |
| 预发布 | 成本与延迟 | 单 Run 成本 ≤ 基线 +5%；TTFT P95 ≤ 基线 +10% | 阻断发布（**成本也是门禁**） |
| 预发布 | chaos 套件（9 项） | 全部符合期望行为 | 阻断发布 |
| 灰度 1% | 线上指标 30 分钟 | 成功率、撤回率、投诉率、P99 无显著恶化 | 自动回滚（第 42 章） |
| 发布后 24h | 线上采样评估 | 抽样 200 条人工/LLM-judge，分数不低于基线 -2pt | 一键回滚 + 事故复盘 |

```yaml
# .github/workflows/agent-ci.yml
name: agent-ci
on: [pull_request]

jobs:
  l1-l2:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with: { go-version: "1.24" }
      - name: L1 单元 + 契约（毫秒级，零网络）
        run: go test ./... -count=1 -race -short
      - name: L2 录制回放（fail-closed，禁止真调用）
        env:
          CASSETTE_MODE: replay
          # 万一有代码试图打真模型，直接失败而不是扣钱
          OPENAI_API_KEY: ""
          ANTHROPIC_API_KEY: ""
        run: go test ./agent/... -tags=cassette -count=1
      - name: cassette 未被静默改写（防「自动重录把 bug 录进去」）
        run: git diff --exit-code -- ./testdata/scenes || \
             (echo "::error::cassette 被改动，必须人工 review" && exit 1)

  l3-smoke:
    needs: l1-l2
    runs-on: ubuntu-latest
    timeout-minutes: 20
    env:
      EVAL_MODEL_KEY: ${{ secrets.EVAL_MODEL_KEY }}
    steps:
      - uses: actions/checkout@v4
      - name: L3 smoke（40 条，判定式断言 + 统计阈值）
        run: |
          go run ./cmd/eval \
            --set evals/smoke.yaml \
            --concurrency 10 \
            --min-pass-rate 0.95 \
            --baseline evals/baseline.json \
            --budget-usd 2.00        # 超预算自动中止，防跑飞
      - name: 上传失败样本（供回流，第 41 章）
        if: failure()
        uses: actions/upload-artifact@v4
        with: { name: eval-failures, path: ./out/failures/ }
```

---

## 48.5 Java / 分布式经验对照

| 你熟悉的 | Agent 测试的对应物 | 会咬人的差异 |
| --- | --- | --- |
| JUnit `assertEquals` | 判定式断言（schema/实体/禁止项/轨迹） | 语义正确性不能二值化；**要断言「不该出现什么」比「该出现什么」更稳** |
| Mockito mock | Scripted/Fixture/Cassette 三层 mock | mock 对象返回固定值；这里要模拟**不确定性、慢、限流、截断流**四种真实故障 |
| WireMock 录制回放 | cassette | 关键差异：**key 必须含提示词版本**，否则改 prompt 后命中老录像 |
| Testcontainers | MCP server / 工具后端的容器化测试 | 多一条：要测「MCP 协议握手 + 取消通知」，不是只测 HTTP |
| `@Retryable` / 重试测试 | 统计断言（N=20 过 18） | Java 里的重试是确定性的；这里的「重试」是采样，**要把概率当一等公民** |
| JaCoCo 覆盖率 | 「断言覆盖率」：多少条评测样本至少有一条判定式断言 | 覆盖率 85% 也可能一条断言都没有——**行覆盖 ≠ 行为覆盖** |
| Pact 契约测试 | 工具 schema + 错误码 + 幂等契约 | 消费者是**模型**，它不是确定性消费者，契约要写得比 Pact 更宽松（允许可选字段） |
| Chaos Monkey / ChaosBlade | chaos 套件 | 注入点要在网关层，**别在业务代码里塞 if**，否则测的是开关不是韧性 |

---

## 48.6 开源坐标

| 能力 | 代表项目 | 能给什么 | 缺什么 / 会咬你 |
| --- | --- | --- | --- |
| 录制回放 | **VCR**（Ruby）、**go-vcr**、WireMock（Java） | HTTP 层录制/重放，成熟的匹配器 | 都为「确定性 HTTP」设计：key 不懂 `prompt_ver`、不懂流式 SSE 分片、不懂 `tools` schema。**必须自己包一层** |
| 评测/回归 | **promptfoo** | YAML 定义用例、`contains`/`llm-rubric`/`javascript` 断言、多 provider 对比、CI 集成 | 不懂你的多租户/计费/流式链路；断言是「答案级」不是「系统级」 |
| 评测框架 | DeepEval、Ragas | 指标库（faithfulness、answer relevancy、context precision）、pytest 集成 | 指标是统计量，**不要用来做 100% 门禁**，只能做趋势 |
| 容器化依赖 | **Testcontainers** | 起 Postgres/Redis/MCP server/假三方服务 | 冷启动慢（CI 里 20~60s），要复用容器 |
| 网络故障 | Toxiproxy | 延迟/断连/限速/超时，按连接粒度 | 只管 TCP，不管「模型返回 429 但 body 是 200」这种应用层故障 |
| 混沌工程 | Chaos Mesh、ChaosBlade | K8s 层 pod kill / 网络分区 / 压力 | 注入半径大，**测试环境必须独立集群**；周期长，不适合 PR |
| 覆盖率 | `go test -cover`、JaCoCo | 行/分支覆盖 | 对 Agent 意义有限，见 §48.5 |

> 🎯 **判断**：录制回放这块**必须自研一层薄封装**（key 设计 + fail-closed + 流式支持），因为现有 VCR 库都为「确定性 HTTP」设计。评测这块可以直接用 promptfoo/DeepEval，但它们只能当「指标」，门禁逻辑（基线对比、预算、阻断）要你自己写。

---

## 48.7 事故清单

- [ ] **事故 A：`continue-on-error: true` 把真实回归放过去了。** 现象：计费 bug 上线三周才发现，客单价虚高 18%。→ 根因：flaky 太多 → 关掉门禁。→ 兜底：**先修 flaky 再加门禁**；L1/L2 零容忍，L3 用统计阈值；任何 `continue-on-error` 必须带到期日。
- [ ] **事故 B：cassette 自动重录，把 bug 录成了「正确答案」。** 现象：修了 bug 反而测试红。→ 根因：miss 时自动调真模型并落盘。→ 兜底：录制只允许 `RECORD=1` 人工触发；CI 加 `git diff --exit-code` 检查 cassette 被改。
- [ ] **事故 C：改提示词后 L2 全部 miss，CI 直接跳过。** 现象：「测试全绿」，实际一条没跑。→ 根因：`t.Skip` 掩盖 miss。→ 兜底：miss 必须 fail，且提示「哪一维 key 变了」（把这几个维度打出来）。
- [ ] **事故 D：评测集被「污染」成训练集。** 现象：离线 92 分，线上 61 分。→ 根因：拿评测集做提示词迭代，迭代了几十轮。→ 兜底：评测集分 dev/holdout（7:3），holdout 只在发布前跑一次，且禁止看明细。
- [ ] **事故 E：工具轨迹断言写成全序，40% 假红。** 现象：每次 rerun 都过，但一改并行就红。→ 根因：模型并行调工具的顺序不稳定。→ 兜底：子序列匹配 + 集合语义。
- [ ] **事故 F：chaos 在生产环境跑。** 现象：注入延迟把真客户打挂 8 分钟。→ 根因：namespace selector 写错。→ 兜底：chaos 只允许在独立测试集群；生产只做「单 Pod 只读演练」，且需 SRE 双人确认。
- [ ] **事故 G：评测把预算跑飞。** 现象：一次 CI 循环重跑 6 小时，烧掉 $400。→ 根因：评测脚本失败重试无上限。→ 兜底：`--budget-usd` 硬上限 + 并发上限 + 单次墙钟超时。

---

## 48.8 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 根本病因 | 不是「测不了」，是「断言写错了 + flaky 打败了你 + 于是关掉了门禁」 |
| 三层 | L1 单元/契约（80%，毫秒，零容忍）→ L2 录制回放（15%，确定性）→ L3 评测集（5%，统计） |
| 关键分野 | L1/L2 是**门禁**（100% 绿）；L3 是**指标**（看通过率与置信区间）。别混在一个面板 |
| mock | 三种（scripted / fixture / recorded）；**必须与真实 provider 共用 interface 与 DTO** |
| cassette | key 必须含 `prompt_ver` + `tools_schema` + `model_id`；**默认 fail-closed**；录制只允许人工触发 |
| 断言 | 五类：schema / 实体 / 禁止项 / 工具轨迹（子序列！）/ 统计（N=20 ≥18） |
| 契约 | 工具与 MCP 必须测：schema、错误码、超时取消、幂等、结果截断、副作用分级 |
| chaos | 9 项固定剧本，每周跑；注入点在网关，不在业务代码 |
| 门禁 | PR 跑 L1+L2+smoke；合并跑 full；预发布跑 chaos + 成本/延迟门禁；灰度 1% 看 30 分钟 |
| 反模式 | `continue-on-error`、`--retry 3`、自动重录、评测集当训练集、全序轨迹断言 |
| 一句话 | **测不了「它答得对不对」，就去测「它有没有守规矩」。** |

## 48.9 自检题

1. **你的 CI 里 380 个测试会调真模型，flaky 率 41%。请给出 30 天改造计划（按月/按周，给动作）。**
   > 要点：第 1 周先止血——把 380 个分成「逻辑类」和「质量类」，逻辑类全部改成 L2 录制回放（预计 300 个），剩下的 80 个并入评测集。第 2 周把断言从字符串相等改成判定式（schema/实体/禁止项/轨迹）。第 3 周建 L3 基线（40 条 smoke + 200 条 full）+ 预算上限。第 4 周才敢打开门禁：L1/L2 零容忍，L3 用「不低于基线 -1pt」。**顺序不能反——先修 flaky 再开门禁，否则团队会再次关掉它。**

2. **为什么 cassette 的 key 必须包含 `prompt_ver`？不加会怎样？**
   > 要点：不加的话，你改了提示词，测试仍然命中旧响应，得到「改了没效果」的结论，于是一次真实的质量改善被误判为无效、或者一次真实的质量回归被判为通过。更糟的是它会**掩盖 prompt 与代码的不一致**——线上跑新 prompt、测试跑老响应。加 `prompt_ver` 后，改 prompt 会导致大面积 miss，这是**特性不是 bug**：它强制你在改行为时重新审视每一条预期。

3. **L3 评测集通过率从 92% 掉到 88%，你会怎么处理？给出决策树。**
   > 要点：① 先看是「全体下降」还是「某个 scene/某类样本下降」；② 再看是不是模型版本/路由策略变了（版本矩阵，第 42 章）；③ 看失败样本的失败类型（schema 崩？工具没调？事实错？），聚类；④ 单点 4pt 在 N=200 时约等于 8 条样本，先确认不是判定器抖动（用同一份输出重跑判定器）；⑤ 如果确认是真实回归，判断是「可接受的能力变化」还是「必须修」——**这一步必须由人决定，不能自动放行**。

## 48.10 延伸阅读

- 上一章：[47. 成本工程（FinOps）](47-cost-engineering.md)（评测预算那笔账在 §47.4）
- 下一章：[49. 低代码配置：让业务自己搭 Agent](49-lowcode-agent-builder.md)（配置也需要评测门禁）
- 相关：[40. 评估体系](40-evaluation-system.md)（评测集构建、LLM-as-judge 校准）、[42. 模型与提示词的迭代上线](42-model-release.md)（灰度与回滚）、[39. 可观测层](39-observability.md)（从 trace 捞真实 Run 做 cassette）
- 工具：`promptfoo`（YAML 评测 + CI）、`go-vcr` / `WireMock`（录制回放）、`Testcontainers`（MCP server 契约测试）、`Toxiproxy` / `Chaos Mesh`（故障注入），版本与用法以官方文档最新版为准
- 总纲：[0-summary.md](0-summary.md)
