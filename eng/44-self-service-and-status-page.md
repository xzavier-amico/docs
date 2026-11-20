# 44. 支撑系统：自助运维与对外状态页

> **状态**：`已完成（初稿）` ｜ **所属**：第七部分 · 运维与运营 ｜ **编号**：44 / 52
> **本章定位**：**「让客户自己查到原因」比「让客服去问运维」便宜 10 倍**，而且快 30 倍。本章给自助诊断的接口设计、状态页的写法与法律含义、限流/降级时的对外沟通模板，以及内部工具化的优先级排序。
> **读法**：§44.2 的成本账决定要不要做；§44.4 的自助诊断 API 可直接实现；§44.5 的状态页「写什么/不写什么」是法务也会关心的部分。

---

## 44.1 引子：一次「我的订单呢」，客户等了 22 分钟

2026 年 7 月，一个客户在 IM 里问：「我 10:03 下的单，怎么还没发货？」

典型链路：

| 时间 | 动作 | 耗时 |
| --- | --- | --- |
| 10:20 | 客服 1 接待，问订单号 | 2 min |
| 10:22 | 客户给了单号，客服 1 查后台 → 显示「已支付，待发货」 | 1 min |
| 10:23 | 客服 1 不确定是否有异常，转二线 | 3 min 等待 |
| 10:26 | 二线查订单库，发现库存锁定成功但发货单未生成 | 2 min |
| 10:28 | 二线找仓储系统对接人 | 4 min |
| 10:32 | 仓储说「消息积压，正在消费」 | 2 min |
| 10:34 | 二线回给客服 1 | 2 min |
| 10:36 | 客服 1 回复客户「系统繁忙，稍等」 | 1 min |
| 10:42 | 客户再次催问 | — |

**总计 22 分钟：3 名员工（客服 1、二线、仓储）接力处理，客户等待 22 分钟**，而客户想知道的只有一句：「我的单卡在哪一步、大概什么时候好」。

如果有一个自助查询页，这件事只需要 **15 秒**：

```text
订单 8821300001
├── 10:03:12  已创建          ✅
├── 10:03:14  已支付          ✅
├── 10:03:15  库存已锁定      ✅
├── 10:03:16  发货单生成      ⏳ 排队中（当前积压 1,240 单，预计 10:50 前完成）
└── 预计发货：今天 18:00 前
```

> 💡 **自助诊断的本质**：把「内部排查路径」变成「客户可读的状态时间线」。它同时服务三个目标：减少工单、降低客服压力、让客户有确定感。

---

## 44.2 成本账：为什么值得做

| 方式 | 单次成本 | 响应时间 | 可扩展性 |
| --- | --- | --- | --- |
| 客服问运维 | 3 名员工合计约 17 分钟 ≈ **0.3 人时**（约 90 元，按 300 元/人时含机会成本） | 20~60 分钟 | 差（运维被占用） |
| 客服查后台（有工具） | 5 分钟 ≈ **25 元** | 5~10 分钟 | 中 |
| **自助诊断** | **≈ 0**（服务器成本 < 0.01 元） | **15 秒** | 极好 |
| 状态页（批量） | ≈ 0 | 即时 | 极好（一次覆盖所有受影响客户） |

**算一笔账**：每天 300 个「进度类」咨询 × 90 元 = **2.7 万元/天**；自助覆盖 60% ⇒ 省 **1.62 万元/天**，一年约 **590 万**，而开发成本约 3 人月。

> 🩸 **但有个前提**：**自助诊断必须准**。给出错误的状态（「已发货」但其实没发）比不给更糟——客户会拿截图来投诉。

---

## 44.3 结构图：三层支撑系统

```text
┌─────────────────────────────────────────────────────────┐
│ 第三层：对外状态页 / 公告（批量沟通，所有客户可见）        │
│   status.example.com：当前事件、历史事件、订阅             │
├─────────────────────────────────────────────────────────┤
│ 第二层：自助诊断（单客户、单对象，需鉴权）                 │
│   订单进度 / 请求追踪 / 错误码解释 / 额度与用量            │
├─────────────────────────────────────────────────────────┤
│ 第一层：内部工具（客服/运维用，含敏感信息）                │
│   全链路查询 / 一键重试 / 补偿发起 / 开关操作              │
└─────────────────────────────────────────────────────────┘
```

**三层的权限与脱敏**：

| 层 | 可见范围 | 敏感信息 | 审计 |
| --- | --- | --- | --- |
| 状态页 | 所有人 | 无（只写影响面） | 不需要 |
| 自助诊断 | 该租户/该用户 | 脱敏（手机掩码、金额可见自己的） | 记录查询日志 |
| 内部工具 | 员工（按角色） | 可见但需审批导出 | 全量审计 |

---

## 44.4 怎么实现：自助诊断的四个接口

### 通用设计原则

```text
1. 鉴权：必须校验「这个对象属于这个用户/租户」（水平越权防护）
2. 只读：自助接口绝不提供写操作（写操作走工单）
3. 脱敏：手机/身份证/银行卡掩码；不返回内部 ID 与堆栈
4. 可解释：返回「状态 + 原因 + 预计时间 + 下一步」，不返回错误码原文
5. 可追溯：每次查询记日志（谁查了什么）
6. 限流：防爬取（每用户 60 次/小时）
```

### 接口一：订单进度（状态时间线）

```http
GET /api/self-service/orders/{orderNo}/progress
Authorization: Bearer <user token>
```

```json
{
  "orderNo": "8821300001",
  "status": "PROCESSING",
  "statusText": "正在处理",
  "timeline": [
    {"at": "2026-07-15T10:03:12+08:00", "step": "CREATED",  "text": "订单已创建", "done": true},
    {"at": "2026-07-15T10:03:14+08:00", "step": "PAID",     "text": "支付成功",   "done": true},
    {"at": "2026-07-15T10:03:15+08:00", "step": "STOCK",    "text": "库存已锁定", "done": true},
    {"at": null,                        "step": "SHIPMENT", "text": "正在生成发货单", "done": false,
     "eta": "2026-07-15T10:50:00+08:00", "note": "当前积压 1,240 单"}
  ],
  "eta": "2026-07-15T18:00:00+08:00",
  "canCancel": true,
  "helpUrl": "/help/order-delay"
}
```

```java
@RestController
public class SelfServiceController {

    @GetMapping("/api/self-service/orders/{orderNo}/progress")
    public OrderProgress progress(@PathVariable String orderNo,
                                  @AuthenticationPrincipal User user) {
        // ★ 越权防护：必须按「用户 + 订单号」查，不能只按订单号
        Order order = orderRepo.findByUserAndNo(user.getId(), orderNo)
            .orElseThrow(() -> new NotFoundException());   // 不暴露「存在但不属于你」

        List<Step> steps = stepRepo.findByOrderId(order.getId());
        return OrderProgress.of(order, steps, etaService.estimate(order));
    }
}
```

```sql
-- 状态时间线：从事件表读，不要从多个业务表拼（拼出来的时序会错）
SELECT step, status, occurred_at, payload
FROM order_step_event
WHERE order_id = ?
ORDER BY occurred_at, id;
```

**ETA 怎么给才不被打脸**：

| 数据 | ETA 算法 | 话术 |
| --- | --- | --- |
| 有明确 SLA | 创建时间 + SLA | 「预计 18:00 前送达」 |
| 有积压 | 当前位次 ÷ 消费速率 | 「积压 1,240 单，预计 10:50 前完成」 |
| 无数据 | **不给具体时间** | 「正在处理，如超过 24 小时请联系我们」 |

> ⚠️ **不要为了「好看」给乐观 ETA**：给不出就诚实说「暂无预计时间」，比给一个必然违约的时间更安全。

### 接口二：请求追踪（按 trace_id）

```http
GET /api/self-service/requests/{traceId}
```

```json
{
  "traceId": "7f3a9c...",
  "summary": "请求已受理，正在等待库存服务响应",
  "hops": [
    {"service": "gateway",   "status": "OK",   "ms": 3},
    {"service": "order-api", "status": "OK",   "ms": 28},
    {"service": "stock-api", "status": "SLOW", "ms": 1850, "note": "响应较慢"},
    {"service": "payment",   "status": "OK",   "ms": 90}
  ],
  "suggestion": "库存服务响应较慢，您的请求已进入重试队列，通常 1 分钟内完成。"
}
```

**关键**：**只暴露「客户能理解的跳」，不暴露内部服务名与堆栈**。把 `stock-api` 翻译成「库存服务」，把 `timeout` 翻译成「响应较慢」。

### 接口三：错误码解释

```http
GET /api/self-service/errors/{code}
```

```json
{
  "code": "ERR_4021",
  "title": "该商品暂不支持配送到您所在地区",
  "detail": "受物流限制，新疆部分地区的订单暂时无法下单。",
  "actions": [
    {"text": "修改收货地址", "url": "/address"},
    {"text": "查看可配送区域", "url": "/help/delivery-area"}
  ],
  "contact": {"text": "仍无法解决？提交工单", "url": "/support/ticket"}
}
```

**错误码字典是基础设施**：每个对外错误码都必须有 `title / detail / actions`，并在 CI 里检查「代码里出现的错误码是否都有文案」。没有文案的错误码，客服只能编。

### 接口四：额度与用量（Agent 平台必备）

```http
GET /api/self-service/usage?period=2026-09
```

```json
{
  "period": "2026-09",
  "plan": {"name": "Pro", "includedTokens": 50000000},
  "used": {"tokens": 38210000, "requests": 128400, "costCents": 48200},
  "breakdown": [
    {"feature": "chat",    "tokens": 21000000, "costCents": 26000},
    {"feature": "rag",     "tokens": 12000000, "costCents": 15000},
    {"feature": "agent",   "tokens":  5210000, "costCents": 7200}
  ],
  "forecast": {"tokens": 51200000, "willExceed": true, "exceedAt": "2026-09-27"},
  "limits": {"ratePerMin": 60000, "concurrent": 20}
}
```

> 💡 **成本争议的根因是「不可见」**。一旦客户能看到「哪个功能花了多少」，80% 的争议会自己消失——剩下 20% 才需要人工解释。

---

## 44.5 对外状态页：写什么，不写什么

### 状态页的四条铁律

```text
① 只写「影响」与「进展」，不写「内部原因」与「责任」
   ✅「部分用户无法创建订单，正在回滚最近的发布」
   ❌「DBA 误删索引导致主库崩溃」（暴露内部、引发追责与安全担忧）

② 不承诺未验证的时间
   ✅「预计 30 分钟内恢复，我们会在 15 分钟后更新进展」
   ❌「马上就好」（一旦超时，信任崩塌）

③ 每一次更新都要带时间戳与署名（团队名，不是个人）
④ 历史事件必须保留（可查询），不能事后删除
```

### 状态页结构

```markdown
## 当前状态
● 部分服务降级        （2026-09-15 10:42 CST 更新）

## 进行中的事件
### 部分用户无法创建订单
- 状态：监控中
- 开始时间：2026-09-15 09:52 CST
- 影响范围：约 12% 的下单请求（部分地域）
- 当前进展：已回滚 09:45 的发布，错误率从 18% 降至 0.3%，持续观察中
- 下次更新：10:42 或更早

## 历史事件（近 90 天）
| 日期 | 事件 | 影响时长 | 影响范围 |
| 2026-08-15 | 下单失败 | 14 分钟 | 12% 请求 |
| 2026-08-02 | 报表加载慢 | 42 分钟 | 报表模块 |
```

### 法律与合规含义（很多人忽略）

| 写法 | 风险 | 建议 |
| --- | --- | --- |
| 「系统正常」但实际有故障 | 可能构成虚假陈述，SLA 争议时不利 | 用「监控中」「部分影响」等准确措辞 |
| 承诺「绝不泄露数据」 | 若发生泄露需担责 | 写「已采取行业标准措施」，不写绝对承诺 |
| 公开客户名称 | 违反保密协议 | 用「部分客户」「某行业客户」 |
| 删除历史事件 | 被质疑隐瞒 | 保留至少 90 天~1 年 |
| 写「黑客攻击」但未证实 | 引发恐慌与监管关注 | 未经确认写「异常访问」 |

> 🩸 **状态页是「对外证据」**：事故后的 SLA 核算、监管问询、客户诉讼，都会拿它当事实依据。**写下的每个字都要能站住。**

### 订阅与通知

```text
- 邮件订阅（按组件/按严重级别）
- Webhook（企业客户接入自己的监控）
- RSS/Atom（简单但有效）
- 站内信（登录后可见）
```

```json
// Webhook 载荷（企业客户集成）
{
  "event": "incident.updated",
  "incident": {
    "id": "inc_20260915_01",
    "status": "monitoring",
    "impact": "partial_outage",
    "components": ["order-api"],
    "startedAt": "2026-09-15T09:52:00+08:00",
    "updatedAt": "2026-09-15T10:12:00+08:00",
    "message": "已回滚发布，错误率降至 0.3%，持续观察中。"
  }
}
```

---

## 44.6 限流/降级时的对外沟通模板

| 场景 | 对内（客服话术） | 对外（状态页/公告） |
| --- | --- | --- |
| **主动限流** | 「您当前请求量超过套餐配额（60,000 次/分钟），已被限流。可临时提额或优化调用频率，需要我帮您申请吗？」 | 「部分高并发请求可能被限流，属预期保护措施，不影响正常使用。」 |
| **依赖降级** | 「推荐功能暂时不可用，其他功能正常。预计 1 小时内恢复。」 | 「个性化推荐暂时不可用，已降级为基础列表，其余功能正常。」 |
| **只读模式** | 「系统正在维护，当前只能查看不能修改，预计 30 分钟。」 | 「系统处于只读维护模式，预计 30 分钟内恢复写入。」 |
| **区域故障** | 「您所在区域（华东）访问受影响，其他区域正常。」 | 「华东区域部分服务受影响，其他区域正常。」 |

**话术三条纪律**：

1. **说人话**：不写「5xx」「连接池打满」「OOM」。
2. **给行动**：客户能做什么（重试、换区域、联系谁）。
3. **给预期**：什么时候再更新，不要给虚假的恢复时间。

---

## 44.7 内部工具化优先级：先建什么

| 优先级 | 工具 | 解决什么 | 工作量 |
| --- | --- | --- | --- |
| **P0** | 全链路查询（按单号/trace 查所有环节） | 80% 的客服咨询需要它 | 2~4 周 |
| **P0** | 一键重试/补偿（带 dry-run 与审计） | 卡单处理 | 2~3 周 |
| **P1** | 开关面板（限流/降级/灰度） | 止血速度 | 1~2 周 |
| **P1** | 客户健康分看板 | 主动运营 | 2~4 周 |
| **P2** | 自助诊断页（对外） | 减少工单 | 4~6 周 |
| **P2** | 状态页 | 批量沟通 | 1~2 周（可用现成服务） |
| **P3** | 补偿审批流 | 资金安全 | 3~4 周 |

> 💡 **先做「内部查询 + 一键重试」**：它们立刻降低客服与运维的负担，且是自助诊断的数据基础。**不要先做漂亮的对外页面**——数据不准，页面越漂亮越糟。

---

## 44.8 双修对照：Agent 的「为什么这么答」

| 维度 | 传统自助 | Agent 自助 | 相同 / 差异根因 |
| --- | --- | --- | --- |
| **可查对象** | 订单/请求/账单 | 以上 + **Run / 回答 / 引用来源** | 多一层 |
| **状态语义** | 已下单/已发货 | 以上 + **已检索/已调用工具/已生成** | 步骤更多 |
| **可解释性** | 错误码 → 文案 | **必须给「依据」**（引用了哪段文档） | 信任的关键 |
| **越权风险** | 查别人订单 | 查别人对话/记忆（**更敏感**） | 需更强隔离 |
| **删除权** | 删订单记录 | 删对话/记忆/向量（**必须级联**） | 合规要求更高 |
| **投诉入口** | 工单 | 工单 + **「这个回答有问题」一键反馈** | 需要闭环到评测集 |

### 「为什么这么答」接口设计

```http
GET /api/self-service/runs/{runId}/explain
```

```json
{
  "runId": "run_8f2a...",
  "question": "跨境订单可以退吗？",
  "answer": "跨境订单支持 7 天无理由退货，但需自行承担运费。",
  "sources": [
    {"docId": "kb_1024", "title": "退款政策 > 跨境订单", "snippet": "跨境订单支持 7 天无理由退货...", "score": 0.87},
    {"docId": "kb_1031", "title": "运费说明", "snippet": "退货运费由买家承担...", "score": 0.71}
  ],
  "model": "gpt-4o-mini",
  "promptVersion": "v12",
  "steps": [
    {"step": 1, "action": "retrieve", "ms": 42, "result": "2 条"},
    {"step": 2, "action": "generate", "ms": 680, "tokens": 420}
  ],
  "feedback": {"helpful": null, "reportUrl": "/feedback?run=run_8f2a"}
}
```

**三个纪律**：

1. **只给该用户自己的 Run**（用 `user_id + run_id` 联合查询，不能只按 run_id）。
2. **引用片段要能点开原文**（且原文未删除，否则提示「内容已更新」）。
3. **反馈要能进评测集**（第 49 章），否则「一键反馈」只是安慰剂。

---

## 44.9 会在哪炸：八个坑

| # | 坑 | 现象 | 根因 | 怎么修 |
| --- | --- | --- | --- | --- |
| 1 | 自助页数据不准 | 客户拿截图投诉 | 读的是从库/缓存旧值 | 读主库或标注「数据延迟 X 秒」 |
| 2 | 越权 | 能查别人订单 | 只按业务号查，没校验归属 | 联合 `user_id` 查询 + 统一 404 |
| 3 | 暴露内部信息 | 页面出现堆栈/服务名 | 直接透传错误 | 错误码字典 + 翻译层 |
| 4 | ETA 过度乐观 | 承诺时间反复跳票 | 拍脑袋给时间 | 有数据才给，否则说「暂无」 |
| 5 | 状态页写内部原因 | 引发追责/安全担忧 | 照搬复盘内容 | 只写影响与进展 |
| 6 | 没有历史事件 | 被质疑隐瞒 | 只展示当前 | 保留 90 天~1 年 |
| 7 | 限流不告知 | 客户以为故障 | 只返回 429 | 响应头 + 文档 + 状态页 |
| 8 | 反馈无闭环 | 客户重复投诉 | 反馈不进评测/工单 | 反馈 → 抽样 → 评测集/改进项 |

---

## 44.10 怎么验证

| 指标 | 定义 | 目标 |
| --- | --- | --- |
| 自助解决率 | 自助完成后 24h 未提工单 / 自助访问 | ≥ 50% |
| 自助查询成功率 | 返回明确状态 / 总查询 | ≥ 95% |
| 咨询类工单占比 | 进度类咨询 / 总工单 | 持续下降 |
| 状态页订阅数 | 订阅客户 / 活跃客户 | ≥ 30%（B 端） |
| 状态页更新及时性 | 事件开始 → 首次公告 | P1 < 15 min |
| 越权测试通过 | 渗透用例全部拦截 | 100% |
| 反馈闭环率 | 进入评测/工单的反馈 / 总反馈 | ≥ 60% |

```bash
# 越权回归测试（CI 里跑）
curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $USER_A_TOKEN" \
  "$BASE/api/self-service/orders/$ORDER_OF_USER_B/progress"
# 期望 404（不是 403，避免泄露「存在」）
```

---

## 44.11 用什么工具

| 能力 | 工具 | 说明 |
| --- | --- | --- |
| 状态页 | Statuspage / Instatus / 自研 | 自研注意历史留存与订阅 |
| 全链路查询 | 基于 trace 系统（Jaeger/Tempo）+ 业务索引 | 要能按业务号查 |
| 开关面板 | 自研 / Unleash / Apollo | 必须审计 |
| 错误码字典 | 代码内维护 + CI 校验 | 防止遗漏 |
| 反馈收集 | 自研 + 进评测管道 | 闭环是关键 |

---

## 44.12 完整实现：订单时间线服务（Go）

自助诊断最容易做成「查多个表拼一个 JSON」，结果时序错乱、性能差。正确做法是**从事件表读**（第 30/34 章的事件溯源），再做视图投影。

```go
// internal/selfservice/timeline.go
package selfservice

import (
	"context"
	"database/sql"
	"time"
)

type Step struct {
	Step  string     `json:"step"`
	Text  string     `json:"text"`
	At    *time.Time `json:"at,omitempty"`
	Done  bool       `json:"done"`
	ETA   *time.Time `json:"eta,omitempty"`
	Note  string     `json:"note,omitempty"`
}

type Timeline struct {
	OrderNo  string     `json:"orderNo"`
	Status   string     `json:"status"`
	StatusText string   `json:"statusText"`
	Steps    []Step     `json:"timeline"`
	ETA      *time.Time `json:"eta,omitempty"`
	CanCancel bool      `json:"canCancel"`
	HelpURL  string     `json:"helpUrl"`
}

// 对外可见的步骤（内部步骤不暴露）
var publicSteps = []struct{ Step, Text string }{
	{"CREATED", "订单已创建"},
	{"PAID", "支付成功"},
	{"STOCK", "库存已锁定"},
	{"SHIPMENT", "生成发货单"},
	{"SHIPPED", "已发货"},
}

type Service struct {
	db   *sql.DB
	eta  ETAEstimator
}

func (s *Service) GetTimeline(ctx context.Context, userID int64, orderNo string) (*Timeline, error) {
	// ★ 越权防护：联合 user_id，查不到与无权限统一返回 not found
	var orderID int64
	var status string
	err := s.db.QueryRowContext(ctx,
		`SELECT id, status FROM orders WHERE order_no=? AND user_id=?`, orderNo, userID).
		Scan(&orderID, &status)
	if err == sql.ErrNoRows {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}

	// 一次查询取全部事件（按时间 + id 保证稳定顺序）
	rows, err := s.db.QueryContext(ctx,
		`SELECT step, occurred_at FROM order_step_event
		 WHERE order_id=? ORDER BY occurred_at, id`, orderID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	done := map[string]*time.Time{}
	for rows.Next() {
		var step string
		var at time.Time
		if err := rows.Scan(&step, &at); err != nil {
			return nil, err
		}
		if _, ok := done[step]; !ok { // 同一步多次事件，取第一次
			t := at
			done[step] = &t
		}
	}

	tl := &Timeline{OrderNo: orderNo, Status: status}
	for _, ps := range publicSteps {
		st := Step{Step: ps.Step, Text: ps.Text}
		if at, ok := done[ps.Step]; ok {
			st.At, st.Done = at, true
		} else {
			// 未完成的步骤：给 ETA（有数据才给）
			st.ETA = s.eta.Estimate(ctx, orderID, ps.Step)
		}
		tl.Steps = append(tl.Steps, st)
	}

	tl.StatusText = statusText(status)
	tl.ETA = s.eta.FinalETA(ctx, orderID)
	tl.CanCancel = status == "PAID" || status == "PROCESSING"
	tl.HelpURL = "/help/order-delay"
	return tl, nil
}
```

**三个实现细节决定成败**：

| 细节 | 原因 |
| --- | --- |
| 只查**事件表**，不拼业务表 | 业务表的状态是「当前值」，拼不出时序 |
| 同一 step 取**第一次**事件 | 重试会产生多条，否则会出现「发货两次」 |
| ETA 由 `ETAEstimator` 统一算 | 各页面各自算 → 同一订单在不同页面显示不同时间 |

### ETA 估算（可解释，不拍脑袋）

```go
type ETAEstimator struct{ db *sql.DB }

// 有积压时：位次 ÷ 消费速率；否则用历史 P50
func (e *ETAEstimator) Estimate(ctx context.Context, orderID int64, step string) *time.Time {
	if step != "SHIPMENT" {
		return nil // 只对已知瓶颈给 ETA
	}
	var position, ratePerMin int
	err := e.db.QueryRowContext(ctx, `
		SELECT (SELECT COUNT(*) FROM shipment_queue WHERE order_id <= ?),
		       CAST((SELECT COUNT(*) FROM shipment_done
		             WHERE done_at >= NOW() - INTERVAL 5 MINUTE) / 5 AS SIGNED)`,
		orderID).Scan(&position, &ratePerMin)
	if err != nil || ratePerMin == 0 {
		return nil // ★ 没数据就不给时间
	}
	eta := time.Now().Add(time.Duration(position/ratePerMin) * time.Minute)
	return &eta
}
```

> ⚠️ **`ratePerMin == 0` 时返回 nil**，不是返回一个很大的时间。前端展示「正在处理，暂无预计时间」，比给一个必然违约的时间安全。

---

## 44.13 自助诊断的度量：怎么证明它有用

上线自助诊断后，必须能回答「它到底减少了多少工单」。三个指标 + 一个实验。

| 指标 | 计算 | 目标 |
| --- | --- | --- |
| **自助覆盖率** | 使用自助的会话 / 总咨询会话 | ≥ 40% |
| **自助解决率** | 自助后 24h 未提工单 / 自助会话 | ≥ 50% |
| **工单下降率** | (上线前 4 周均值 - 上线后 4 周均值) / 上线前 | ≥ 20% |
| **平均解决时长** | 从客户提问到问题解决 | 下降 ≥ 50% |

**可执行的实验设计**（避免「上线了但说不清效果」）：

```text
① 选两类同质咨询（如「订单进度」「发票」），一类开自助，一类不开（对照组）
② 观察 4 周：工单量、CSAT、平均时长
③ 计算增量：实验组工单下降 vs 对照组
④ 若实验组下降显著且 CSAT 不降 → 全量
```

```sql
-- 自助效果看板（按天）
SELECT DATE(created_at) AS d,
       COUNT(*) AS self_serve_sessions,
       SUM(followed_by_ticket_24h = 0) / COUNT(*) AS resolved_rate,
       ROUND(AVG(duration_sec), 1) AS avg_sec
FROM self_service_session
WHERE created_at >= NOW() - INTERVAL 30 DAY
GROUP BY d ORDER BY d;
```

**埋点必须记的三件事**：

```text
1. 查了什么（order_progress / trace / errcode / usage）
2. 查到结果了吗（found / not_found / error）
3. 24h 内是否提了工单（关联 ticket.requester_id）
```

没有第 3 项，你永远无法证明「自助有用」；没有第 2 项，你不知道是「没人用」还是「用了查不到」。

---

## 44.14 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 成本对比 | 客服问运维 0.3 人时（约 90 元）vs 自助 15 秒；自助覆盖 60% 年省约 590 万 |
| 前提 | 自助必须准；错误的状态比不给更糟 |
| 三层 | 状态页（所有人）/ 自助（本租户）/ 内部工具（员工 + 审计） |
| 六原则 | 鉴权、只读、脱敏、可解释、可追溯、限流 |
| ETA | 有数据才给；没数据说「暂无」，别给乐观时间 |
| 状态页 | 只写影响与进展；不写内部原因；不承诺未验证时间；保留历史 |
| 法律 | 状态页是对外证据；措辞要能站住；不点名客户 |
| 话术 | 说人话、给行动、给下次更新时间 |
| 优先级 | 先做全链路查询 + 一键重试，再做对外页面 |
| 双修 | Agent 必须能解释「为什么这么答」：引用来源 + 模型 + prompt 版本 + 步骤 |

## 44.15 自检题

1. **自助诊断接口为什么「只按订单号查」是漏洞？正确做法是什么？**
   > 要点：只按订单号查会允许用户 A 查到用户 B 的订单（水平越权），且返回 403 会泄露「该订单存在」。正解：`WHERE order_no=? AND user_id=?`（或 `tenant_id`），不存在与无权限统一返回 404；并加 CI 越权回归测试。

2. **状态页写「DBA 误删索引导致主库崩溃」，会有什么问题？**
   > 要点：① 暴露内部架构与责任，可能引发追责、诉讼中的不利证据；② 引发安全担忧（客户会问「你们权限管理怎么做的」）；③ 对客户无价值（他们只关心影响与恢复）。正解：写影响面、当前进展、下次更新时间；根因放内部复盘。

3. **Agent 平台的「为什么这么答」页面，最小的可用信息是什么？为什么？**
   > 要点：最小集 = 引用来源（文档标题 + 片段 + 可点开原文）+ 模型 + prompt 版本 + 关键步骤（检索/工具/生成）+ 反馈入口。因为客户质疑的是「依据是否可信、是不是最新、有没有越权」，而不是模型内部权重；缺引用则无法验证，缺版本则无法复盘。

## 44.16 延伸阅读

- 上一章：[43. 运营体系](43-operation-system.md)
- 下一章：[45. 一次故障的两份报告](45-incident-two-reports.md)
- 相关：[41. 运维与运营边界](41-ops-vs-operation.md)、[42. 运维体系](42-ops-system.md)、[51. 排障手册](51-troubleshooting-handbook.md)
- 平台侧：[../llm/39-observability.md](../llm/39-observability.md)（可观测与可解释）、[../llm/37-output-audit-retraction.md](../llm/37-output-audit-retraction.md)（输出审计与撤回）
- 参考：Atlassian Statuspage 最佳实践；Google SRE 的「对外沟通」章节；GDPR Art.15 访问权（自助查询是合规能力）
- 总纲：[0-summary.md](0-summary.md)
