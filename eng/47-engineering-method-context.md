# 47. 工程方法：契约先行、小步验证

> **状态**：`已完成（初稿）` ｜ **所属**：第八部分 · 工程效能 ｜ **编号**：47 / 52
> **本章定位**：把「让模型写代码」从「许愿」变成「施工」。核心只有两条：**先把契约钉死，再让模型填实现；每填一步，用机器验证一次。**
> **读法**：§47.3 的四步流程可以直接当 SOP；§47.4 的护栏图建议贴到 CI 首页。

---

## 47.1 事故现场：联调三天，最后发现「字段名不一样」

2025 年 7 月，两个小组并行开发：A 组做「退款单查询 API」，B 组做「客服工作台」调用它。为了快，双方口头约定了字段：

```text
A 组理解：{ "refundId": 123, "status": "SUCCESS", "amount": 2380 }
B 组理解：{ "id": 123, "state": "success", "amount": 2380.00, "currency": "CNY" }
```

两边都用模型生成代码——**各自都「正确」**，因为各自都按自己的理解写了测试。

联调时：

| 问题 | 表现 | 排查耗时 |
| --- | --- | --- |
| 字段名不一致 | `refundId` vs `id`，B 组拿到 null | 2 小时 |
| 状态枚举大小写 | `SUCCESS` vs `success`，判断永远为 false | 3 小时 |
| 金额类型 | 一个用分（int），一个用元（decimal），差 100 倍 | 5 小时 |
| 缺少 currency | B 组默认人民币，跨境单显示错误 | 1 小时 |
| 分页参数 | `page/size` vs `offset/limit` | 1.5 小时 |

**三天联调，没有一行是「逻辑错」，全是「契约没定」。** 更讽刺的是：如果一开始花 30 分钟写一份 OpenAPI schema，这三天可以完全避免。

> 💡 **模型的加入放大了这个问题**：人会「打电话问一下」，模型不会——它只会按你给的信息**自信地**实现一个错误版本。

---

## 47.2 什么是「契约」：不止是接口签名

| 层 | 内容 | 不写会怎样 |
| --- | --- | --- |
| **类型契约** | 字段名、类型、可空性、精度（分/元） | 差 100 倍、NPE |
| **结构契约** | 嵌套结构、数组、分页封装 | 解析失败 |
| **语义契约** | 字段含义、单位、枚举取值、默认值 | 「成功」被理解成两种状态 |
| **错误契约** | 错误码、错误结构、哪些是重试可恢复 | 客户端乱重试 |
| **时序契约** | 是否幂等、是否保序、超时语义 | 重复扣款 |
| **版本契约** | 如何演进、只加不改、废弃策略 | 升级即破坏 |

**一份合格的契约长这样**（OpenAPI 片段）：

```yaml
components:
  schemas:
    Refund:
      type: object
      required: [refundId, status, amountCents, currency]
      properties:
        refundId:
          type: integer
          format: int64
          description: 退款单号（全局唯一，雪花 ID）
        status:
          type: string
          enum: [INIT, AUDITING, REFUNDING, SUCCESS, FAILED]
          description: 大写枚举；终态为 SUCCESS/FAILED
        amountCents:
          type: integer
          format: int64
          description: 退款金额，单位「分」，避免浮点误差
        currency:
          type: string
          pattern: '^[A-Z]{3}$'
          description: ISO-4217 币种，如 CNY
      example:
        refundId: 8821300001
        status: SUCCESS
        amountCents: 238000
        currency: CNY
```

> ⚠️ **金额字段必须带单位**。`amount: 2380` 是本章事故的直接原因——**只要单位不进字段名，就一定有人理解错**（`amountCents` / `amountYuan` / `amount` + `unit` 三选一，别用裸 `amount`）。

---

## 47.3 契约先行的四步流程（SOP）

```text
① 写契约          ② 写测试          ③ 写实现          ④ 接线
接口 + schema     表驱动测试        填最小实现        改调用方 1~2 处
+ 错误码          （此时全红）       （逐条转绿）      （集成测试）
   │                 │                 │                 │
   ▼                 ▼                 ▼                 ▼
 评审通过           编译通过          测试全绿          端到端通过
```

### 第一步：写契约（人做，30 分钟）

```go
// 契约：只定义「是什么」，不定义「怎么做」
type RefundService interface {
    // Get 查询退款单。不存在时返回 ErrNotFound（不要返回 nil, nil）
    Get(ctx context.Context, refundID int64) (*Refund, error)
    // Create 创建退款。幂等：同一 requestID 重复调用返回同一结果
    Create(ctx context.Context, req CreateRefundReq) (*Refund, error)
}

type Refund struct {
    RefundID    int64     `json:"refundId"`
    Status      Status    `json:"status"`       // 大写枚举
    AmountCents int64     `json:"amountCents"`  // 单位：分
    Currency    string    `json:"currency"`     // ISO-4217
    CreatedAt   time.Time `json:"createdAt"`
}

var (
    ErrNotFound   = errors.New("refund not found")
    ErrInvalidArg = errors.New("invalid argument")
)
```

**评审清单**（契约评审只看看这六条）：

- [ ] 字段名是否自解释？单位是否进名字？
- [ ] 可空性是否明确？`nil` 的语义是什么？
- [ ] 枚举是否封闭？新增值调用方会不会崩？
- [ ] 错误是否分类？哪些可重试？
- [ ] 幂等语义是否写明？幂等键是什么？
- [ ] 是否有 example？调用方能不能照着写？

### 第二步：写测试（人做，先红）

```go
func TestRefundService_Get(t *testing.T) {
    svc := newTestService(t)

    t.Run("exists", func(t *testing.T) {
        want := seedRefund(t, svc, 238000)
        got, err := svc.Get(context.Background(), want.RefundID)
        require.NoError(t, err)
        require.Equal(t, want.RefundID, got.RefundID)
        require.Equal(t, int64(238000), got.AmountCents)
        require.Equal(t, "CNY", got.Currency)
    })

    t.Run("not found returns ErrNotFound", func(t *testing.T) {
        _, err := svc.Get(context.Background(), 999999)
        require.ErrorIs(t, err, ErrNotFound) // 契约：不是 nil,nil
    })

    t.Run("create is idempotent", func(t *testing.T) {
        req := CreateRefundReq{RequestID: "req-1", OrderID: 1, AmountCents: 100}
        a, err1 := svc.Create(context.Background(), req)
        b, err2 := svc.Create(context.Background(), req) // 同 requestID
        require.NoError(t, err1)
        require.NoError(t, err2)
        require.Equal(t, a.RefundID, b.RefundID) // 契约：返回同一单
    })
}
```

> 💡 **测试就是可执行的契约**。文档会腐烂，测试不会——它要么绿，要么红。

### 第三步：写实现（模型做，逐条转绿）

给模型的 prompt 应该长这样：

```text
实现 RefundService 接口，满足以下约束：
- 只改 internal/refund/service_impl.go，不要动其他文件
- 依赖：RefundRepo（接口见 repo.go）、IDGen（见 internal/id/id.go），不要新造
- 金额单位是「分」，不要做元/分转换
- 错误必须用 ErrNotFound/ErrInvalidArg，不要 fmt.Errorf 包一层
- 幂等键是 RequestID，用 refund_request 表唯一索引实现（DDL 见 migrations/）
- 目标：让 service_test.go 全部通过（当前 3 个失败）
- 完成后运行 make verify
```

**这段 prompt 的每一行都在减少一种失败模式**：限定文件（防扩散）、指定依赖（防重复造）、写明单位（防 100 倍）、指定错误（防调用方判断失效）、给幂等方案（防重复退款）、给验收命令（防「看起来对」）。

### 第四步：接线（模型做，改 1~2 处）

只改调用方的最小范围，跑集成测试。**不要在同一步里既改实现又改多个调用方**——出了问题分不清是谁的锅。

---

## 47.4 验证闭环：五道护栏，一道都不能少

```text
   模型输出
      │
      ▼
  ┌────────┐   ┌────────┐   ┌────────┐   ┌────────┐   ┌────────┐
  │ 编译    │──▶│ 类型    │──▶│  lint  │──▶│ 单测    │──▶│ 集成    │──▶ 提交
  │ build  │   │ vet/tsc│   │ static │   │ test   │   │ e2e    │
  └────────┘   └────────┘   └────────┘   └────────┘   └────────┘
   语法错      类型不匹配    风格/坏味道   行为错       契约/链路错
   秒级         秒级         秒级          秒~分        分级
```

| 护栏 | 抓什么 | 漏什么 | 成本 |
| --- | --- | --- | --- |
| 编译 | 语法、未定义符号、签名不匹配 | 逻辑错 | 秒级 |
| 类型/静态检查 | 空指针风险、类型断言、未使用变量 | 运行时数据问题 | 秒级 |
| lint | 风格、复杂度、常见 bug 模式 | 业务错 | 秒级 |
| 单测 | 函数行为、边界 | 集成问题 | 秒~分 |
| 集成/e2e | 契约、链路、事务 | 生产环境差异 | 分级 |

**Makefile 一条命令**（模型每次改完自己跑）：

```makefile
.PHONY: verify
verify:
	go build ./...
	go vet ./...
	golangci-lint run --timeout 5m ./...
	go test ./... -race -count=1 -coverprofile=cover.out
	@go tool cover -func=cover.out | tail -1
```

> 🩸 **不要跳过 `-race`**：模型写的并发代码「看起来对」的概率极低，而数据竞争是最难事后排查的 bug。

---

## 47.5 小步提交与 review 粒度

### 提交粒度：一个「可编译的语义单元」

| ✅ 好的提交 | ❌ 坏的提交 |
| --- | --- |
| `feat(refund): 添加 Get 接口与实现` | `feat: 退款功能`（30 个文件） |
| `test(refund): 补幂等测试` | `fix: 修 bug`（不知道修了什么） |
| `refactor(id): 提取 IDGen 接口` | `chore: 一堆改动` |

**判据**：**这个提交单独 revert，系统还能编译、测试还能过吗？** 能 → 粒度合适。

### review 粒度：模型生成的代码，人只看四处

| 必看 | 为什么 | 怎么看 |
| --- | --- | --- |
| **事务边界** | 模型常把外部调用放进事务 | 找 `@Transactional` / `BeginTx`，看里面有没有 HTTP/MQ |
| **并发安全** | 模型爱用共享 map/无锁计数 | 找 `map`、`sync.Once`、`atomic`，问「并发 200 会怎样」 |
| **权限与租户** | 模型不知道你的鉴权模型 | 每个查询是否带 `tenant_id` |
| **错误处理** | 模型爱 `catch(Exception e) {}` 或忽略 err | 搜空的 catch / `_ =` |

其余（命名、格式、注释）交给 lint，不要浪费人的注意力。

---

## 47.6 双修对照：契约的两种消费者

| 维度 | 传统系统 | Agent 系统 | 相同 / 差异根因 |
| --- | --- | --- | --- |
| **契约形式** | OpenAPI / Protobuf / Java interface | JSON Schema（工具参数）+ 输出 schema | 结构同构 |
| **契约消费者** | 其他服务、前端 | **模型**（它要「看懂」才能调用） | 模型对描述文字更敏感 |
| **契约不明确的后果** | 联调失败、报错 | 模型**猜**参数、幻觉字段、调用失败后重试 | Agent 的失败更隐蔽 |
| **错误契约** | 错误码 + 是否可重试 | 错误信息要**给模型看**（它会据此调整） | Agent 的错误信息是 prompt 的一部分 |
| **版本演进** | 只加不改 | 同上，但旧 prompt 可能仍引用旧字段 | Agent 多一层「提示词版本」 |
| **验证** | 契约测试 | 契约测试 + **工具调用成功率** + schema 校验 | Agent 多一个指标 |

**完全相同的部分**：类型/schema 的设计原则、只加不改的演进规则、金额单位要进字段名、错误分类、幂等声明。**这些一条都不用改。**

**Agent 特有的三条**：

1. **描述即契约**：工具的 `description` 不是注释，是模型唯一的说明书。写「查询订单」不够，要写「按订单号查；订单号是 18 位数字；不存在返回 NOT_FOUND，不要重试」。
2. **参数要可校验**：模型会传 `"null"`、`""`、`0`、`"2026-13-01"`。schema 必须严格（`required`、`enum`、`pattern`），服务端二次校验。
3. **错误要可行动**：返回 `{"error":"INVALID_DATE","message":"date 必须是 YYYY-MM-DD，你传的是 2026-13-01"}`，模型下一轮能自己改；返回 `500 Internal Error` 它只会重试。

```json
{
  "name": "get_refund",
  "description": "按退款单号查询退款状态。退款单号是 18 位数字字符串。不存在时返回 code=NOT_FOUND（不要重试）。",
  "parameters": {
    "type": "object",
    "required": ["refund_id"],
    "properties": {
      "refund_id": { "type": "string", "pattern": "^[0-9]{18}$" }
    },
    "additionalProperties": false
  }
}
```

---

## 47.7 会在哪炸：七个失效模式

| # | 失效模式 | 现象 | 根因 | 正解 |
| --- | --- | --- | --- | --- |
| 1 | **契约只写在文档里** | 代码与文档不一致 | 文档不是可执行的 | 用 schema/interface + 测试 |
| 2 | **测试是模型写的，且照着实现写** | 测试全绿但行为错 | 测试与实现同源，互相印证错误 | 人先写测试（红），模型再实现 |
| 3 | **一次改太多文件** | review 不了、回滚不了 | 粒度太大 | 一次 1~3 文件 |
| 4 | **跳过 lint/类型检查** | 小问题累积成大债务 | 图快 | CI 强制，不通过不合并 |
| 5 | **契约演进破坏兼容** | 上线后老调用方崩 | 删字段/改类型 | 只加不改；废弃走两阶段 |
| 6 | **错误信息不可行动** | 模型/调用方只会重试 | 错误太笼统 | 错误码 + 具体原因 + 建议动作 |
| 7 | **「能编译」当成「可上线」** | 事务/并发/权限漏洞 | 缺人工 review | 四处必看（§47.5） |

---

## 47.8 怎么验证：契约本身也要被测

| 验证项 | 方法 | 通过标准 |
| --- | --- | --- |
| schema 合法性 | `openapi-spec-validator` / `protoc` | 0 error |
| 契约兼容性 | 与上一版本 diff（`oasdiff`） | 无 breaking change |
| 契约测试 | 消费者驱动契约（Pact）或生成客户端 | 双方测试通过 |
| 幂等声明 | 重复调用测试 | 副作用只 1 次 |
| 错误契约 | 注入每种错误 | 返回约定错误码 |
| 文档一致性 | 从代码生成文档 | 文档与代码同源 |

```bash
# 契约兼容性检查（CI 里跑）
oasdiff breaking --base api-v1.yaml --revision api-v2.yaml
# 有 breaking change 时退出码非 0，阻止合并
```

---

## 47.9 用什么工具

| 场景 | 工具 |
| --- | --- |
| HTTP 契约 | OpenAPI + `oasdiff` + `openapi-generator` |
| RPC 契约 | Protobuf + `buf breaking` |
| Java 契约 | interface + ArchUnit（架构约束测试） |
| Go 契约 | interface + `go vet` + `staticcheck` |
| 消费者驱动契约 | Pact |
| 数据库契约 | migration 文件 + schema diff（`atlas`/`flyway`） |
| Agent 工具契约 | JSON Schema + `additionalProperties: false` + 服务端校验 |

---

## 47.10 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 契约六层 | 类型/结构/语义/错误/时序/版本，少一层就会在联调时爆炸 |
| 金额 | **单位必须进字段名**（`amountCents`），裸 `amount` 必出事 |
| 四步 | 写契约（人）→ 写测试（人，先红）→ 写实现（模型）→ 接线（模型，小范围） |
| 测试归属 | **测试必须人先写**；模型照着实现写的测试等于没测 |
| 五护栏 | 编译 → 类型 → lint → 单测(-race) → 集成，一道都不能少 |
| 提交粒度 | 单独 revert 仍能编译、测试仍过 |
| review 四处 | 事务边界 / 并发安全 / 权限租户 / 错误处理 |
| 双修 | 契约设计原则全通用；Agent 多一条「**描述即契约**」 |
| 错误 | 错误信息要可行动：错误码 + 原因 + 建议动作 |
| 一句话 | **先钉死「什么算对」，再让模型去写；每写一步，让机器判一次。** |

## 47.11 自检题

1. **为什么「让模型先写实现，再照着实现写测试」是反模式？**
   > 要点：测试与实现同源，会互相印证同一个错误理解（模型把「元」当「分」，测试也按「元」写，全绿但线上差 100 倍）。正确顺序是人根据契约写测试（先红），模型实现让测试转绿——测试是**独立的判据**，不能由被验证方生成。

2. **一个接口契约里 `status` 是 `string`，没给枚举。会有什么后果？**
   > 要点：调用方不知道有哪些值，只能靠猜或 `switch default` 兜底；模型可能生成 `"Success"`、`"success"`、`"SUCCEED"`；新增状态时调用方静默走 default（可能当成失败或成功）。正解：封闭枚举 + 明确未知值的处理策略 + 契约测试。

3. **模型改完代码说「已完成」，你怎么在 2 分钟内确认？**
   > 要点：让它跑 `make verify` 并贴输出（编译/vet/lint/单测 -race）；看 `git diff --stat` 确认改动范围（是否只改了约定文件）；看 `git diff` 里是否有空 catch、共享 map、缺 tenant_id；最后看测试是否**新增**（而不是只改了实现）。

## 47.12 延伸阅读

- 上一章：[46. 小上下文写大项目](46-small-context-large-project.md)
- 下一章：[48. 工具链与反模式](48-context-toolchain-antipatterns.md)
- 相关：[5. 幂等的失效模式](5-idempotency-failure-modes.md)（幂等声明也是契约）、[39. 多存储下的事务边界](39-multi-storage-transactions.md)
- 平台侧：[../llm/22-tool-system-mcp.md](../llm/22-tool-system-mcp.md)（工具契约与 schema）、[../llm/16-prompt-as-asset.md](../llm/16-prompt-as-asset.md)（提示词也是契约资产）
- 总纲：[0-summary.md](0-summary.md)
