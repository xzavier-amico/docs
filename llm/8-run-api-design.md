# 8. Run API 设计：幂等、取消、审批、回放

> **状态**：`已完成（初稿）` ｜ **所属**：第二部分 · Agent-Client / Agent-Server 分离架构 ｜ **编号**：8 / 52
> **本章定位**：第 7 章解决「事件怎么流下去」，这一章解决「Run 这个资源怎么被创建、被中止、被人工干预、被重放」。**Agent API 和普通 REST API 最大的区别是：它不是「请求-响应」，而是「提交一个可能跑 30 秒、可能花 5 块钱、可能改外部世界的作业」。** 你就得按「作业」来设计它。
> **读法**：§8.2 先看五个现场和状态机，§8.4 的六个设计点按顺序读。如果你的 API 已经上线了，直接从 §8.4.3（取消传播）和 §8.4.5（回放）读起——这两块是补不回来的。

---

## 8.1 引子：五个让 API 设计露馅的现场

**现场 A：用户双击了「发送」。**

前端防抖没做好（或者网络慢用户以为没点上），同一个问题发了两次。两个 Run，两份 25,000 token 输入，两份 800 字输出，账单上 ¥0.24 而不是 ¥0.12。用户还看到两个答案略有不同的气泡，投诉「你们系统是不是有病」。

如果按 Aurora 峰值算：500 Run/s 里有 0.8% 是重复提交，那就是 **4 个重复 Run/秒 × ¥0.12 × 86,400s ≈ ¥41,000/天**。一个月 124 万，纯粹是「多发了一次请求」。

**现场 B：用户点了「停止」。**

按钮变灰，前端不再渲染。但 Run 还在跑，模型还在吐 token，工具还在执行。用户半小时后看到账单：**「我取消了为什么还收钱？」** 技术上的真问题是：取消这个动作**传播到了哪一层**？到网关？到 Worker？到模型连接？到正在执行的那个 shell 命令？

**现场 C：审批。**

Agent 要「给客户退款 ¥2,000」。按策略必须人工审批。Run 挂起，等主管在小程序上点「同意」。主管点了——但那个审批请求已经因为超时（30 分钟）被判定为拒绝，Run 已经 failed 了。**主管点了个寂寞，而且系统没告诉他。**

更糟的版本：主管点了两次「同意」（网络卡），触发了两次退款。

**现场 D：断线重连。**

用户在地铁里，连接断了。重连之后前端要「把刚才的输出补回来」。补回来的方式是——再发一次 `POST /runs`。于是有了两个 Run，一个在跑一个已废，用户看到答案突然「跳」了一下。

**现场 E：你升级了 API。**

你在响应里把一个字段从 `"tokens": 1200` 改成 `{"input": 900, "output": 300}`，觉得「更清晰了」。结果三个月前客户集成的那批 SDK 全部 `NullPointerException`，两个大客户的生产环境挂了。

> 🎯 这五个现场对应本章六件事：**幂等键、取消语义、审批回传、断点回放、版本演进、状态机**。
> 而它们有一个共同的根：**你还在把 Run 当成「一个 HTTP 请求」，其实它是一个「有生命周期、有副作用、需要人签字、可以被吊销的作业」。** 作业就得有作业的 API：提交、查询、取消、审批、续作、留档。

---

## 8.2 断崖：把 Run 建成资源，而不是一次调用

### 8.2.1 Run 资源模型

| 字段 | 类型 | 为什么必须有 | 缺了会怎样 |
| --- | --- | --- | --- |
| `id` | `run_...`（前缀 + ULID） | 全局唯一、可按时间排序 | 分库后 ID 冲突；按时间扫表要额外索引 |
| `object` | `"run"` | 可判别联合体 | 客户端反序列化要猜 |
| `session_id` | string | 归属会话，多轮上下文 | 无法把多轮串起来 |
| `status` | enum（见状态机） | 客户端据此决定要不要继续等 | 只能靠轮询 + 猜 |
| `created_at` / `started_at` / `ended_at` | RFC3339 | 排队时长 = started − created | 算不出排队时间，第 11 章的调度优化无从下手 |
| `cancel_requested_at` | nullable | 区分「取消中」和「已取消」 | 长工具调用期间状态不明 |
| `input` | 引用 + 摘要 | 原始输入可能很大（附件） | 存全文 → 单行 8MB |
| `output` | `{text, finish_reason}` | 最终结果（非流式兜底） | 只靠流，断线就没了 |
| `usage` | `{input_tokens, cached_tokens, output_tokens, tool_calls}` | 计费与对账 | 算不出钱（第 27 章） |
| `error` | `{code, message, retryable}` | 机器可判定的失败 | 客户端只能展示「出错了」 |
| `approvals[]` | 审批对象数组 | 人机协同（第 34 章） | 审批散落在别的系统 |
| `parent_run_id` | nullable | 回放 / fork / 子 Agent | 无法追溯「这次重跑基于哪次」 |
| `trace_id` | string | 可观测（第 39 章） | 查日志靠时间戳撞运气 |
| `versions` | `{model, prompt, role_card, tools, route}` | 可复现（第 42 章） | 线上效果不可复现 |
| `expires_at` | RFC3339 | 挂起 Run 的兜底 | 挂起 Run 永远占资源 |
| `idempotency_key` | 只存 hash | 防重放、可审计 | 明文存 key 等于泄露客户端内部 ID |

> 💡 注意 `versions` 这个字段。它不参与任何业务逻辑，**但没有它，你所有的「效果变差」都无法归因**。一个 Run 的完整身份是「输入 + 五个版本号」，不是「输入」。（详见第 42 章）

### 8.2.2 状态机：8 个状态，5 个终态

```
                        ┌──────────── cancel ───────────────┐
                        │                                   ▼
   POST /runs      ┌─────────┐  admit   ┌─────────┐      ┌────────────┐
  ────────────────▶│ queued  │─────────▶│ running │─────▶│ canceling  │
                   └────┬────┘          └────┬────┘      └─────┬──────┘
                        │ reject             │                 │ drained
                        ▼                    │                 ▼
                   ┌──────────┐              │            ┌──────────┐
                   │ rejected │              │            │ canceled │
                   └──────────┘              │            └──────────┘
                                             │
              ┌──────────────────────────────┼──────────────────────────┐
              │ tool 需执行                   │ 需人工签字                │ 正常结束
              ▼                              ▼                          ▼
      ┌──────────────┐              ┌──────────────────┐      ┌────────────┐
      │ waiting_tool │              │ waiting_approval │      │ succeeded  │
      └──────┬───────┘              └────────┬─────────┘      └────────────┘
             │ resume                      │ approve/deny/timeout
             └──────────▶ running          ├──▶ running（approve）
                                            ├──▶ failed（deny）
                                            └──▶ failed（approval_expired）

  终态（不可再迁移）：succeeded / failed / canceled / expired / rejected
  挂起态（占用资源但不烧 token）：waiting_tool / waiting_approval
  可取消态：queued / running / waiting_tool / waiting_approval
```

**三条状态机纪律**（违反了就是无穷 bug）：

1. **终态不可迁移**。对已 succeeded 的 Run 发 cancel，返回 `409 run_state_conflict`，不能「改成 canceled」——那会让计费和审计对不上。
2. **所有迁移必须落事件**（第 6 章）。状态是事件的投影，不是直接 `UPDATE` 出来的。否则并发下会丢迁移。
3. **挂起态必须有 `expires_at`**。`waiting_approval` 不设超时 = 永久占资源 + 用户永远等。

### 8.2.3 错误码表：机器可判定，人才看得懂

HTTP 状态码不够用。Agent 的失败有 20 种语义，而 HTTP 只有 5 类。**必须定义业务 code，并且 `retryable` 要显式给**——客户端不该去猜。

| HTTP | `code` | retryable | 何时出现 | 客户端该做什么 |
| --- | --- | --- | --- | --- |
| 400 | `invalid_request` | ❌ | schema 校验失败 | 修请求，别重试 |
| 401 | `unauthenticated` | ❌ | 无/过期凭证 | 刷新 token |
| 403 | `forbidden` | ❌ | 无权访问该 Run（跨租户） | 停止 |
| 403 | `tool_denied` | ❌ | 策略拒绝工具（第 22 章） | 提示用户 |
| 404 | `run_not_found` | ❌ | ID 不存在或已过保留期 | 停止 |
| 409 | `idempotency_in_flight` | ✅ `Retry-After: 1` | 同 key 的请求还在处理 | 等 1s 用**同 key** 重试 |
| 409 | `run_state_conflict` | ❌ | 对终态 Run 取消/审批 | 拉最新状态 |
| 409 | `approval_already_decided` | ❌ | 同一审批被不同结论覆盖 | 拉审批详情 |
| 422 | `idempotency_key_reuse` | ❌ | 同 key 但 body 不同 | **这是 bug**，换 key 或修 body |
| 402 | `quota_exhausted` | ❌ | 余额/套餐用尽 | 引导充值 |
| 429 | `rate_limited` | ✅ `Retry-After` | 请求级限流（第 23 章） | 退避重试 |
| 429 | `concurrency_limited` | ✅ `Retry-After` | 并发槽位满 | 退避，或降级到小模型 |
| 503 | `overloaded` | ✅ | 准入拒绝（第 11 章） | 退避 + 抖动 |
| 504 | `upstream_timeout` | ✅ | 模型/工具超时 | 重试（只在非写操作时） |
| 200 | `error.code = content_blocked` | ❌ | 输出被审核拦截 | 展示兜底话术 |
| 200 | `error.code = budget_exceeded` | ❌ | 超预算刹车（第 27 章） | 提示用户缩短问题 |
| 200 | `error.code = context_overflow` | ❌ | 上下文爆窗 | 提示开新会话（第 15 章） |

> ⚠️ 注意最后三行：它们是 **HTTP 200 但 `status = failed`**。为什么？因为 Run 已经创建成功了（有 id、有账单、有 trace），失败发生在执行期。**用 4xx 表达「作业失败」是错的**——4xx 意味着「请求有问题，你该改请求」，而这里请求没问题，是执行没成功。

---

## 8.3 结构图：一次带审批和取消的 Run 全生命周期

```
 Client                 Gateway                  Kernel                外部世界
   │                       │                       │                      │
   │ ① POST /runs          │                       │                      │
   │  Idempotency-Key: K   │                       │                      │
   ├──────────────────────▶│                       │                      │
   │                       │ ② SETNX idem:{org}:K  │                      │
   │                       │    (fingerprint)      │                      │
   │                       │──────┐                │                      │
   │                       │◀─────┘ 失败→读旧响应   │                      │
   │ ③ 201 {run, status:queued}                    │                      │
   │◀──────────────────────┤                       │                      │
   │   Location: /v1/runs/run_x                    │                      │
   │   Idempotent-Replayed: true (命中时)          │                      │
   │                       │ ④ admit + 落库        │                      │
   │                       ├──────────────────────▶│                      │
   │ ⑤ GET /events (SSE)   │                       │                      │
   ├──────────────────────▶│ ⑥ XREAD ev:run_x      │                      │
   │◀── token/tool/status ─┤◀──────────────────────┤                      │
   │                       │                       │ ⑦ 高危动作！          │
   │                       │                       ├─────────────────────▶│
   │                       │                       │◀── 需要审批 ──────────┤
   │◀── approval_required ─┤◀──────────────────────┤                      │
   │                       │                       │ status=waiting_approval
   │ ⑧ POST /approvals/a1  │                       │                      │
   │   {decision, nonce}   │                       │                      │
   ├──────────────────────▶│ ⑨ 校验 nonce+四眼     │                      │
   │                       ├──────────────────────▶│ ⑩ resume             │
   │                       │                       ├─────────────────────▶│ 执行
   │ ⑪ POST /runs/{id}/cancel                      │                      │
   ├──────────────────────▶│                       │ ctx.cancel()         │
   │                       ├──────────────────────▶│ ├─ 断模型流            │
   │ ⑫ 202 {cancel:{...}}  │                       │ ├─ 标记 orphan 工具    │
   │◀──────────────────────┤                       │ └─ 落 canceled 事件    │
   │ ⑬ GET /events?since=1042 (重连)               │                      │
   │◀── 从 1042 续，不重放 0~1041 ──────────────────┤                      │
```

三条关键路径的语义差别，一定要分清楚：

| 动作 | 幂等？ | 是否改变 Run 状态 | 失败后怎么办 |
| --- | --- | --- | --- |
| `POST /runs` | **靠 `Idempotency-Key`** | 创建新 Run | 同 key 重试，安全 |
| `GET /runs/{id}` | 天然幂等 | 否 | 无限重试 |
| `GET /events` | 幂等（按 seq） | 否 | 带 `Last-Event-ID` 重连 |
| `POST /cancel` | **幂等**（重复取消返回 202 + 原 cancel 对象） | queued/running → canceling → canceled | 重试安全，但**已花的钱不退** |
| `POST /approvals` | **不幂等**（需一次性 nonce） | waiting_approval → running/failed | 重复提交必须 409，不能重复执行 |
| `POST /replay` | **不幂等**（每次都建新 Run） | 创建新 Run + `parent_run_id` | 重放会重复计费 |

> 🎯 一眼看出设计对不对：**所有「会创建新 Run」的接口都不幂等（靠 key 兜），所有「改状态」的接口都幂等，所有「花钱/改世界」的动作必须绑 nonce 或审批。** 这三条记住了，API 就不会出大错。

---

## 8.4 关键设计

### 8.4.1 OpenAPI 片段（3.1）

```yaml
openapi: 3.1.0
info: { title: AgentOS Run API, version: "1.4.0" }
paths:
  /v1/runs:
    post:
      operationId: createRun
      parameters:
        - name: Idempotency-Key
          in: header
          required: true            # ★ 强制，不提供「可选」的余地
          schema: { type: string, minLength: 16, maxLength: 128 }
        - name: AgentOS-Beta
          in: header                  # 可选能力开关，同 OpenAI-Beta 的做法
          schema: { type: string, enum: [runs-async-2026-03] }
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [session_id, input]
              properties:
                session_id: { type: string }
                input:
                  type: object
                  required: [messages]
                  properties:
                    messages:
                      type: array
                      maxItems: 200
                      items:
                        type: object
                        required: [role, content]
                        properties:
                          role: { type: string, enum: [user, assistant, tool] }
                          content: { type: string, maxLength: 200000 }
                    attachments:            # 大对象走引用，不进 body
                      type: array
                      items: { type: string, format: uri }
                # ★ 显式预算：不传就用租户默认值，永远有上限
                limits:
                  type: object
                  properties:
                    max_output_tokens: { type: integer, minimum: 1, maximum: 32000, default: 4096 }
                    max_steps:         { type: integer, minimum: 1, maximum: 64,    default: 16 }
                    max_cost_cents:    { type: integer, minimum: 1, maximum: 10000, default: 500 }
                    deadline_ms:       { type: integer, minimum: 1000, maximum: 1800000, default: 120000 }
                mode: { type: string, enum: [stream, async], default: stream }
      responses:
        "201":
          description: Run 已创建
          headers:
            Location:            { schema: { type: string } }
            Idempotent-Replayed: { schema: { type: boolean } }   # true = 命中幂等，返回旧结果
          content:
            application/json:
              schema: { $ref: "#/components/schemas/Run" }
        "409":
          description: 幂等冲突（in_flight）或状态冲突
          content:
            application/json:
              schema: { $ref: "#/components/schemas/Error" }
  /v1/runs/{run_id}/cancel:
    post:
      operationId: cancelRun
      parameters:
        - { name: run_id, in: path, required: true, schema: { type: string } }
      requestBody:
        content:
          application/json:
            schema:
              type: object
              properties:
                mode:
                  type: string
                  enum: [graceful, immediate, checkpoint]   # 见 §8.4.3
                  default: graceful
                reason: { type: string, enum: [user_cancel, timeout, superseded, policy] }
      responses:
        "202":
          description: 取消已受理（不代表已停止）
          content:
            application/json:
              schema:
                type: object
                properties:
                  cancel:
                    type: object
                    properties:
                      requested_at: { type: string, format: date-time }
                      deadline:     { type: string, format: date-time }
                      mode:         { type: string }
        "409": { description: run_state_conflict（终态） }
```

### 8.4.2 幂等键：三件事必须做对

```go
// 幂等中间件：key 存在 → 返回旧响应；key 在飞 → 409；body 变了 → 422
type idemRecord struct {
	Fingerprint string `json:"fp"`     // sha256(method+path+body)
	State       string `json:"state"`  // in_flight | done
	Status      int    `json:"status"`
	Body        []byte `json:"body,omitempty"`
}

func Idempotency(store IdemStore, ttl time.Duration) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			key := r.Header.Get("Idempotency-Key")
			if key == "" {
				writeErr(w, 400, "invalid_request", "Idempotency-Key required")
				return
			}
			org := auth.OrgFromCtx(r.Context())
			body, _ := io.ReadAll(io.LimitReader(r.Body, 1<<20))
			r.Body = io.NopCloser(bytes.NewReader(body))
			fp := sha256Hex(append([]byte(r.Method+" "+r.URL.Path), body...))
			k := "idem:" + org + ":" + key

			// ① 原子占位：Lua 脚本保证 SETNX + GET 是原子的
			rec, won, err := store.Claim(r.Context(), k, fp, ttl)
			if err != nil { writeErr(w, 503, "overloaded", "idem store unavailable"); return }

			if !won {
				if rec.Fingerprint != fp {
					// 同 key 不同 body：这是客户端 bug，必须显式报错，绝不能"宽容处理"
					writeErr(w, 422, "idempotency_key_reuse", "key reused with different body")
					return
				}
				if rec.State == "in_flight" {
					w.Header().Set("Retry-After", "1")
					writeErr(w, 409, "idempotency_in_flight", "original request still processing")
					return
				}
				// ② 命中已完成：原样回放旧响应 + 标记头
				w.Header().Set("Idempotent-Replayed", "true")
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(rec.Status)
				w.Write(rec.Body)
				return
			}

			// ③ 抢到了：录响应
			rw := &recorder{ResponseWriter: w, buf: &bytes.Buffer{}}
			next.ServeHTTP(rw, r)
			if rw.status < 500 { // 5xx 不落库，允许客户端用同 key 重试
				store.Commit(r.Context(), k, idemRecord{
					Fingerprint: fp, State: "done", Status: rw.status, Body: rw.body(),
				}, ttl)
			} else {
				store.Release(r.Context(), k) // ★ 关键：5xx 要放行，否则客户端永远拿不到成功
			}
		})
	}
}
```

| 决策点 | 选择 | 理由 |
| --- | --- | --- |
| key 谁生成 | **客户端**（UUIDv7 / ULID） | 服务端生成等于没有幂等（客户端重试会拿新 key） |
| 存什么 | `sha256(method+path+body)` 指纹 | 不存 body，防 PII 落 Redis |
| TTL | **6 小时** | 客户端重试窗口 ≤ 5min；离线队列最长 6h。500/s × 6h ≈ 1080 万 key × 300B ≈ **3.2GB**（3 节点 Redis 集群） |
| 5xx 怎么处理 | 释放 key | 否则一次 503 让用户 6 小时内都用不了这个 key |
| 并发同 key | 409 + `Retry-After` | 不能「等它完成再返回」，那会把网关线程占住 |
| 幂等范围 | 只覆盖 `POST /runs` 等创建类 | 给 GET 加幂等中间件纯属浪费 Redis |

> 🩸 **一个必须踩过才知道的坑**：`409 in_flight` 时如果客户端拿**新 key** 重试，幂等就完全失效了。所以 SDK 里必须把「同一次用户意图」和「同一个 key」绑定死——key 要在**用户点发送的那一刻**生成，而不是在发 HTTP 请求的那一刻。

### 8.4.3 取消：三种语义，一个传播链

取消不是一个 `DELETE`，它是一个**协作式协议**。因为你的下游（模型、工具、沙箱里的 shell）都不可被你强行杀死。

| 模式 | 行为 | 用户感知 | 适用 | 计费 |
| --- | --- | --- | --- | --- |
| `graceful`（默认） | 停止生成新 token；把**已生成**的内容 commit 成 output；工具调用等其自然返回（或标记 orphan） | 「停了，保留已输出的」 | 99% 场景 | 已产生 token 照收 |
| `immediate` | 立刻断流、丢弃未提交事件、尽力 kill 工具 | 「啪一下没了」 | 用户明确要求、内容违规 | 已产生 token 照收 |
| `checkpoint` | 跑到下一个安全点再停（写完当前文件/提交完当前事务） | 「慢一点才停」 | 写操作、多步事务（第 12 章） | 到安全点为止 |

传播链（每一跳都要有独立预算，否则「取消」本身会超时）：

```
客户端 POST /cancel
  └─▶ Gateway：写 cancel_intent 事件（先落库，再传播 —— 顺序不能反）
        └─▶ Run Worker：ctx.cancel()                     预算 50ms
              ├─▶ 模型流：关闭 HTTP/2 stream / RST       预算 100ms
              ├─▶ 工具调用：不可中断的标记 orphan         预算 200ms
              │     （外部 API 已经发出去了，收不回来）
              └─▶ 沙箱：SIGTERM → 5s → SIGKILL            预算 5s
最终：落 canceled 事件 + 结算已用量 + 释放槽位            总预算 ≤ 500ms（用户感知阈值）
```

```go
func (k *Kernel) Cancel(ctx context.Context, runID string, mode CancelMode) (*Cancel, error) {
	// ① 幂等：已有 cancel 记录就原样返回
	if c, ok := k.cancels.Get(runID); ok { return c, nil }

	// ② 先落事件，再传播。顺序反了会出现"取消了但没记录"的黑洞
	ev := Event{Type: "run.cancel_requested", RunID: runID, Payload: map[string]any{
		"mode": mode, "at": time.Now().UTC(),
	}}
	if err := k.events.Append(ctx, ev); err != nil { return nil, err }

	// ③ 状态检查：终态直接 409
	st := k.state.Of(runID)
	if st.Terminal() { return nil, ErrStateConflict }

	// ④ 传播：拿 run 自己的 ctx，取消它
	runCtx, cancel := k.ctxRegistry.Get(runID) // 注册在 Run 启动时
	// 宽限窗口：graceful 给 500ms 收尾，immediate 立即
	if mode == CancelGraceful { time.AfterFunc(500*time.Millisecond, cancel) } else { cancel() }

	// ⑤ 等 drain（有上限），再确认终态。不能立刻返回 canceled —— 那是在撒谎
	select {
	case <-k.ctxRegistry.Done(runID):
	case <-time.After(5 * time.Second):
		k.metrics.Inc("cancel.drain_timeout") // 超时也要落 canceled（标记 forced）
	}
	c := &Cancel{RequestedAt: ev.At(), Deadline: ev.At().Add(5 * time.Second), Mode: mode}
	k.cancels.Put(runID, c)
	return c, nil
}
```

**取消必须解决的三个细节**：

1. **取消是幂等的，但计费不是**。重复取消返回同一个 `cancel` 对象（202），但已花的 token 不退。这条要在产品文案里写清楚，否则一定被投诉。
2. **取消要能赢过「即将开始」**。Run 在 `queued` 时被取消，调度器拿到它之后**必须检查一次** `cancel_requested_at`，不能傻乎乎地开始跑。这个竞态每天都会发生（8% 取消率 × 500/s = 40 次/秒）。
3. **orphan 工具调用要留痕**。外部 API 已经调出去了、结果不能用了，这个事实必须写进审计（第 31 章）——因为钱可能已经花了，世界的状态可能已经变了。

### 8.4.4 审批回传：一次性、四眼、可超时

```jsonc
// GET /v1/runs/{id} 里的 approvals 片段
{
  "approval": {
    "id": "apr_7c1f",
    "run_id": "run_x9",
    "status": "pending",              // pending | approved | denied | expired
    "action": "refund.create",
    "risk": "high",                   // low | medium | high
    "args_preview": { "order_id": "SO-8812", "amount_cents": 200000, "currency": "CNY" },
    "args_hash": "sha256:9a3e...",    // 审批的是"这份参数"，不是"这个动作"
    "requested_at": "2026-05-01T10:23:41Z",
    "expires_at":   "2026-05-01T10:53:41Z",
    "nonce": "n_5f2b8c",              // 一次性，回传必须带
    "required_approvals": 1,
    "decided_by": null
  }
}
```

```http
POST /v1/runs/run_x9/approvals/apr_7c1f
Content-Type: application/json
Idempotency-Key: apr_5f2b8c

{
  "decision": "approve",          // approve | deny
  "nonce": "n_5f2b8c",
  "reason": "客户投诉升级，同意全额退",
  "edited_args": { "amount_cents": 150000 }   // 可选：改额度再批
}
```

| 规则 | 为什么 | 违反的后果 |
| --- | --- | --- |
| `nonce` 一次性，用后即焚 | 防重放（前端重复点击/中间人重放） | 两次退款 |
| `args_hash` 绑定参数 | 审批的是「退 ¥2000 给 SO-8812」，不是「允许退款」 | 审批被复用去退别的单 |
| 审批人 ≠ 发起人（四眼） | 合规底线 | 审计不通过 |
| 重复提交同结论 → 200，不同结论 → 409 | 幂等但不可覆盖 | 后到的 deny 覆盖了前面的 approve |
| 超时 → `expired` → Run `failed` | 挂起不能永久 | 半夜挂起 4 万个 Run，早上一起来全超时 |
| 审批决定也进事件流 | 可回放、可审计 | 出事查不到谁批的 |

审批的量级账：12% 的 Run 需要审批 → 500 × 12% = **60 次/秒**；平均等待 4 分钟（主管不在电脑前）→ 稳态挂起 **14,400 个 Run**。这 14,400 个 Run 不烧 token，但**每个占一条 SSE 长连接 + 一份状态**——这正是第 7 章那 10 万连接里「挂起」那部分的主要来源。

### 8.4.5 断点回放：三个不同的东西，别混为一谈

“回放” 这个词在 Agent 平台里指三件完全不同的事，混在一起设计就会全做不好：

| 类型 | 目的 | 接口 | 是否花钱 | 是否产生新 Run |
| --- | --- | --- | --- | --- |
| **事件回放**（replay events） | 前端补全断线期间的内容 | `GET /events?since=seq` | ❌ | ❌ |
| **确定性重放**（deterministic replay） | 调试：一模一样再跑一次 | `POST /runs/{id}/replay {mode:"recorded"}` | ❌（命中录制缓存） | ✅（`parent_run_id`） |
| **分叉重跑**（fork） | 改参数/改提示词再跑 | `POST /runs/{id}/replay {mode:"fork", overrides:{...}}` | ✅ | ✅ |

```go
// POST /v1/runs/{id}/replay
type ReplayReq struct {
	Mode      string            `json:"mode"`                // recorded | fork
	FromStep  int               `json:"from_step,omitempty"` // fork 从第几步开始
	Overrides map[string]string `json:"overrides,omitempty"` // prompt_version / model / tools
}

func (h *Runs) Replay(w http.ResponseWriter, r *http.Request) {
	src, err := h.store.Get(r.Context(), chi.URLParam(r, "id"))
	if err != nil { writeErr(w, 404, "run_not_found", ""); return }

	var req ReplayReq
	_ = json.NewDecoder(r.Body).Decode(&req)

	// recorded：必须锁死五个版本号，否则"重放"出来的东西根本不是同一个
	newRun := &Run{
		ID:          newRunID(),
		SessionID:   src.SessionID,
		ParentRunID: &src.ID,
		Versions:    src.Versions,           // ★ 默认继承
		Input:       src.Input,
		ReplayMode:  req.Mode,
	}
	if req.Mode == "fork" {
		for k, v := range req.Overrides { newRun.Versions.Set(k, v) } // 显式覆盖
		newRun.ReplayFromStep = req.FromStep
	}
	// recorded 模式下，模型调用直接读 src 的录制结果（llm_record 表），不真调
	h.kernel.Submit(r.Context(), newRun)
	w.WriteHeader(202)
	json.NewEncoder(w).Encode(newRun)
}
```

**「确定性重放」在 Agent 场景里是做不到 100% 的**，这点必须诚实：模型的温度采样、批处理顺序、检索索引版本都会变。所以工程上的做法是 **录制 + 重放模型响应**（类似 VCR 模式，第 48 章）：把每次 LLM 调用的 request/response 存下来，重放时按 `(step_index, call_index)` 取录制结果。这样「重放」重的是**你的编排逻辑**，而不是模型。

### 8.4.6 版本演进：只加不删，永不复用字段名

| 变更类型 | 允许？ | 做法 |
| --- | --- | --- |
| 新增可选字段 | ✅ | 直接加，客户端忽略未知字段 |
| 新增枚举值 | ⚠️ | 客户端必须能处理 `unknown`（写进 SDK 契约） |
| 字段改类型（`int` → `object`） | ❌ | 新增字段如 `usage_v2`，旧字段保留 |
| 字段改名 | ❌ | 新增 + 双写 + 旧字段标 deprecated，至少留 12 个月 |
| 字段删除 | ❌（实际是） | 先标 deprecated，观察 6 个月调用量归零再删 |
| 语义变更（`status` 多个值） | ❌ | 新开字段 |
| 错误码新增 | ✅ | 但 `retryable` 必须给，否则客户端崩 |
| 必填参数新增 | ❌ | 只能新增**可选**参数 |
| 移除整个接口 | ⚠️ | `Deprecation` + `Sunset`（RFC 8594）头，提前 6 个月公告 |

```http
HTTP/1.1 200 OK
Deprecation: Sat, 01 Nov 2026 00:00:00 GMT
Sunset: Tue, 01 Jun 2027 00:00:00 GMT
Link: <https://docs.example.com/migrate/v1-to-v2>; rel="deprecation"
Warning: 299 - "POST /v1/runs will be removed 2027-06-01, use POST /v2/runs"
```

三条硬规矩：

1. **大版本走 URI（`/v1`），小变更走「只加不删」**。别搞 header 版本协商，你的客户端里有一半是手写的 `curl`。
2. **能力探测优于版本号**。新功能用 `AgentOS-Beta: feature-name` 头（跟 OpenAI 的做法一致），而不是 `v1.1`。因为客户端会同时用几个新特性，版本号组合会爆炸。
3. **老版本必须跑双份**，不能「新版本代码里加 if 判断」。一旦 `if version == "v1"` 出现在业务代码里，你的技术债就再也还不清了。做法是**网关做 schema 转换**（v1 ↔ 内部模型），业务代码只认一个模型。

---

## 8.5 Java / 分布式经验对照

| 你熟悉的 | AgentOS 对应 | 会咬人的差异 |
| --- | --- | --- |
| `@Idempotent` 注解 / 支付幂等表 | 幂等键 | 支付是「同 key 同结果」，Agent 还要处理 **`in_flight` 返回 409**（支付系统通常是阻塞等待，Agent 不能阻塞——一次 Run 跑 30 秒） |
| 订单状态机（待支付/已支付/已取消） | Run 状态机 | 多了**挂起态**（`waiting_approval`），且挂起态有超时；订单不会「等人签字 30 分钟」 |
| `Future.cancel(true)` | `ctx.cancel()` | Java 的 `Thread.interrupt` 只能打断阻塞点；**Go 的 `ctx` 是显式传参，能穿透到每一层**。Java 里 `CompletableFuture` 链上的 cancel **不会**传播到下游 HTTP 客户端，得手动配 `WebClient` 的 timeout + `doOnCancel` |
| 分布式事务 / Saga | 取消 + 审批 + 补偿 | 取消**不是回滚**：已花 token 不退、已发邮件收不回。要 Saga 补偿（第 12 章） |
| HATEOAS / REST 成熟度模型 | Run 资源 | 别做 HATEOAS，客户端全是手写 HTTP，没人解析 `_links` |
| API 网关版本路由 | `/v1` `/v2` | 别用 Content Negotiation 做版本，`api-version` query 参数也别用（缓存会串） |
| `RequestContextHolder` | `context.Context` | 显式 vs 隐式：Go 里没有「当前请求」的魔法，所有 IO 函数第一个参数都是 ctx。这是**优点**，别想着用全局变量绕开 |
| 审批流（Activiti/Flowable） | 审批回传 | 企业审批流的模型是「串行多级会签」，Agent 审批更像**一次性的风险确认**（有没有人会签 5 级？不会有，用户会跑掉） |

**最容易被 Java 团队做错的一件事**：把 `POST /runs/{id}/cancel` 做成「同步等待 Run 停下再返回 200」。这在 Java 里很自然（`future.get(5, SECONDS)`），但在 5,000 并发取消时会**把网关线程池打穿**。正确姿势是 `202 Accepted` + 客户端轮询/订阅状态。异步是这个 API 的默认语义，不是优化。

---

## 8.6 开源坐标

| 项目 | 对应的设计 | 能抄什么 | 缺什么 |
| --- | --- | --- | --- |
| **OpenAI Responses API / Assistants** | Run 作为资源（`threads/{id}/runs`）、`status` 枚举（`queued/in_progress/requires_action/completed/failed/cancelled/incomplete`）、`cancel` 接口 | 状态机枚举值得直接抄；`requires_action` 就是审批挂起 | 没有幂等键约定；`requires_action` 的回传协议（`submit_tool_outputs`）是给工具结果的，不是审批签字 |
| **OpenAI 的 `Idempotency-Key`**（在部分创建类接口上） | 头 + 服务端去重 | 「客户端生成 key + 服务端 24h 去重」的标准 | 没有规定 `in_flight` 怎么返回、body 变了怎么报错——这两条是最容易出事的 |
| **Anthropic Messages API** | `stop_reason` 语义比 HTTP 码丰富（`end_turn` / `max_tokens` / `tool_use`） | **用枚举表达「为什么结束」**，比 `error` 字符串强 | 无 Run 资源概念，一次调用一次响应 |
| **Temporal** | Workflow 有 `WorkflowId`（业务幂等）、`WorkflowIdReusePolicy`、`Cancel`（协作式）、`Signal`（外部输入） | **语义最完整**：`REJECT_DUPLICATE` / `ALLOW_DUPLICATE` 就是幂等键的成熟形态；`Signal` 就是审批回传 | 它是**执行引擎**不是 API 层，HTTP 契约、错误码、版本演进要你自己定 |
| **LangGraph** | `interrupt` / `Command(resume=...)` 做人机中断；`checkpointer` 支持 thread 级恢复 | 「中断 → 存 checkpoint → 从 checkpoint 恢复」的模型 | 没有 API 契约（Python 对象直接暴露）；中断状态在进程内存/checkpointer 里，**没有对外可审批的 `approval` 资源** |
| **Stripe 的 API 版本策略** | 账户级版本锁定、`Stripe-Version` 头、每月发布、字段只加不删 | **版本演进的最佳实践**（这家公司把这件事做到了极致） | 它是 to-B 小流量 API；你的 5k QPS 还要兼顾 SDK 与手写客户端 |

> 🎯 判断：**Run API 的设计在开源里没有完整答案。** OpenAI 给了状态枚举，Temporal 给了幂等与取消的语义，Stripe 给了版本策略，LangGraph 给了中断恢复——但「生成式 + 长任务 + 要审批 + 要计费」这四件事的交集，只能你自己拼。这也是本章值得单独成章的原因。

---

## 8.7 事故清单

- [ ] **双击产生双 Run**：¥41,000/天白烧，且两个答案不一致引发投诉。
      → 根因：`Idempotency-Key` 在「发 HTTP 请求时」生成而非「用户点击时」。
      → 兜底：key 绑定用户意图 + 服务端强制要求该头（缺失直接 400）。
- [ ] **取消后仍在计费**：用户投诉「点了停止还扣钱」。
      → 根因：取消只断了前端渲染，`ctx` 没传到模型调用。
      → 兜底：`ctx` 全程传递 + 模型侧 abort + 结算用「已生成 token」而非「预估 token」。
- [ ] **取消无效竞态**：Run 在 queued 被取消，调度器还是启动了它。
      → 根因：调度器取出任务后没复查 `cancel_requested_at`。
      → 兜底：**取出后、启动前必须再查一次**（这是个 TOCTOU，每天 40 次/秒）。
- [ ] **审批重放导致两次退款**：主管点了两次，两次都执行了。
      → 根因：审批接口无 `nonce`，且没做「同结论幂等 / 异结论 409」。
      → 兜底：一次性 nonce + `args_hash` 绑定 + 幂等键。
- [ ] **审批超时把 Run 挂死**：一夜之间 4 万个 Run 永久 `waiting_approval`。
      → 根因：`expires_at` 没设或没被扫。
      → 兜底：定时扫描 + 超时转 `failed(approval_expired)` + 告警。
- [ ] **`in_flight` 时客户端换 key 重试**：幂等形同虚设，一次请求变两次。
      → 根因：SDK 把 key 生成放在重试逻辑里。
      → 兜底：SDK 层固定 key + 文档明写「409 必须用同一个 key 重试」。
- [ ] **字段改类型导致大客户全挂**：`usage` 从 int 变 object，两个客户生产环境 500。
      → 根因：无版本策略，无兼容测试。
      → 兜底：只加不删 + 契约测试（第 48 章）+ 灰度发布新字段（第 42 章）。
- [ ] **回放不是「重放」**：用户点「重跑」得到完全不同的答案，被质疑系统不靠谱。
      → 根因：`recorded` 模式没有锁版本号，模型已升级。
      → 兜底：回放必须继承 `versions`，且用录制结果重放模型响应。

---

## 8.8 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 建模范式 | Run 不是「一次 HTTP 请求」，是「一个有生命周期、有副作用、要签字的作业」 |
| 幂等键 | 客户端生成、绑定用户意图、指纹校验 body、6h TTL、5xx 要释放 key |
| 幂等三态 | 新 key 创建 / 同 key 同 body 回放 / 同 key 异 body 报 422 |
| 状态机 | 8 状态 5 终态；终态不可迁移；每次迁移必落事件；挂起态必须有 `expires_at` |
| 取消 | 三种模式（graceful/immediate/checkpoint）；先落事件再传播；总预算 500ms；已花的钱不退 |
| 取消竞态 | queued 取出后启动前必须复查；8% 取消率 × 500/s = 40 次/秒 |
| 审批 | 一次性 nonce + `args_hash` + 四眼 + 超时；重复同结论 200，异结论 409 |
| 审批量级 | 12% × 500/s = 60/s，平均挂 4 分钟 → 稳态 14,400 个挂起 Run |
| 回放 | 事件回放 / 确定性重放 / fork 是三件事；`recorded` 必须锁五版本 |
| 版本 | 大版本走 URI，小变更只加不删；能力探测用 Beta 头；老版本靠网关转换 |
| 错误码 | HTTP 码 + 业务 code + `retryable`；执行期失败是 200 + `status=failed`，不是 4xx |
| 一句话 | **API 设计的所有功夫，都在回答「这个动作重复一次会怎样」。** |

## 8.9 自检题

1. **用户说「我点了取消，为什么还收了我 0.15 元？」请给出你能站得住脚的回答，以及你在系统设计上应该提前做的三件事。**
   > 要点：取消是**停止后续生成**，不是**回滚已发生消耗**——已生成的 1,200 token 是真实算力。三件事：① 计费口径按「已生成 token」而非「预估」；② 取消时立刻落 `usage_final` 事件，让用户能马上看到最终数字；③ 在 UI 上把「停止生成」和「不计费」解耦（免费额度内取消不扣，或给 200 token 的宽限）。**最忌讳的是产品文案承诺「取消不计费」而技术上做不到。**

2. **为什么 `POST /runs/{id}/cancel` 要返回 `202` 而不是 `200`？如果返回 `200 + status: canceled` 会出什么问题？**
   > 要点：取消是**协作式的、异步的**——下游工具/沙箱可能还要 5 秒才真的停。返回 200 就是在撒谎。撒谎的后果：① 客户端立刻认为可以安全重发（其实旧 Run 还在跑，副作用叠加）；② 计费系统按「已取消」结算，但实际还有 token 在产生，账对不上；③ 审计上「取消时间」失真。`202 + cancel 对象` 表达的才是真相：受理了，没完成。

3. **你的 `waiting_approval` Run 平均挂 4 分钟，峰值 14,400 个。这 14,400 个 Run 会消耗什么资源？如果什么都不做会怎样？**
   > 要点：① 每个占一条 SSE 长连接（占连接账的 14%，见第 7 章）；② 每个占一份 Run 状态 + `ctx` 注册表条目；③ 最贵的是**它们占着准入槽位**——如果你的槽位是按「已创建 Run」算的，审批挂起会把容量吃光（第 11 章）。所以正确做法是：挂起态**归还槽位**，恢复时重新排队（可能降优先级），并设 `expires_at` 兜底。

## 8.10 延伸阅读

- 上一章：[7. 流式传输：SSE / WebSocket / gRPC / 长轮询](7-streaming-transport.md)
- 下一章：[9. 端云协同：端侧 Agent 与云侧卸载](9-edge-cloud-split.md)
- 前置：[6. Session / Run / Step / Event 状态模型](6-session-run-event-model.md)（事件溯源与 seq 语义）
- 取消之后的补偿与 Saga：[12. 长任务与持久化执行](12-long-task-durable.md)
- 审批的产品与合规面：[34. 人机协同（HITL）](34-hitl-approval.md)
- 计费与「已用 vs 预估」：[27. 计费的可靠性](27-billing-reliability.md)
- 版本矩阵与可复现：[42. 模型与提示词的迭代上线](42-model-release.md)
- 契约测试与 mock 模型：[48. 测试策略](48-testing-strategy.md)
- 官方参考：OpenAI Responses API / Assistants 的 run 状态枚举、Temporal 的 `WorkflowIdReusePolicy`、Stripe 的 API 版本策略（以官方文档最新版为准）

---

**交叉引用**：总纲 [0-summary.md](0-summary.md) ｜ 前置 [7-streaming-transport.md](7-streaming-transport.md) ｜ 下一章 [9-edge-cloud-split.md](9-edge-cloud-split.md)
