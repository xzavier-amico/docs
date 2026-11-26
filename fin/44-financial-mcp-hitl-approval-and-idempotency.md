# 44. 金融 MCP 工具网关：HITL 审批绑定、同键执行与可信回执

> **状态**：深入工程篇｜**所属**：第五部分·金融 × AI-Agent 架构实战｜**编号**：44 / 48
>
> **定位**：承接 [24 全链路幂等](24-end-to-end-financial-idempotency.md)、[27 对账](27-multi-tier-reconciliation-and-auto-healing.md)、[29 OMS](29-oms-fix-gateway-and-algo-execution.md)、[30 硬风控](30-pretrade-risk-margin-and-liquidation-engine.md) 与 [39 三平面](39-fin-agent-architecture-and-three-plane-isolation.md)，解释工具调用如何变成可审计、不可越权的金融命令。
>
> **边界**：本章只提供协议、PostgreSQL DDL 和 Go/Java 教学示例；不调用、安装、启动或配置任何 MCP 服务，不连接券商，不执行实盘交易。DDL、代码和压测方案不代表已经集成、编译通过或实测达标。
>
> **红线**：MCP 负责工具发现、调用与结果交换，并定义部分授权机制；它不自动提供账户级金融权限、交易幂等或 exactly-once 承诺。HITL 不是聊天里的“同意”，UNKNOWN 不是失败，实盘状态不能来自 LLM 自述。

## 44.1 教学事故：用户点过确认，为什么仍然重复下单

### 44.1.1 虚构时间线

**以下为合成教学事故，证券、账户、时间及金额均为演算设定，不对应真实机构事件。**
设用户只批准买入 100 股，限价 100.00 USD，费用上限 1.00 USD。

| 时刻 | 事件 | 错误 |
|---|---|---|
| 09:30:00 | Agent 调用报价工具，取得参考价 100.00 | 未保存报价时点与来源 |
| 09:30:05 | 弹窗写“是否买入”，用户点击确认 | 确认未绑定账户、数量、限价与摘要 |
| 09:31:00 | 市价升到 102.00，模型把限价改为 102.00 | 旧审批被用于新经济参数 |
| 09:31:01 | 券商接受订单 K1，响应丢失 | 本地只看到超时 |
| 09:31:02 | 工具循环生成 K2 再次送单 | UNKNOWN 被误判为可换键重试 |
| 09:31:10 | 两单各成交 100 股，均价 102.00 | 同一意图产生双倍风险 |
| 09:32:00 | 模型回复“订单已全部成交，审批有效” | 没有可验证回执，文本冒充事实 |

### 44.1.2 手算责任口径

假定两笔各收费 1.00 USD，之后参考价降到 99.00 USD，且没有卖出。
实际现金支出为 `2 × (100 × 102.00 + 1.00) = 20,402.00 USD`。
总持仓市值为 `200 × 99.00 = 19,800.00 USD`，含手续费的账面差额为 `−602.00 USD`。
重复的第二单单独贡献 `100 × (99.00 − 102.00) − 1.00 = −301.00 USD`。
第一单相对批准价格多付 `100 × (102.00 − 100.00) = 200.00 USD`，属于越权改价差额。
这两种归因不可直接相加冒充总损失：第一单还有相对 100.00 买入基线的价格变动。
原批准上限仅 `10,001.00 USD`；实际支出超过该上限 `10,401.00 USD`，这是现金占用差额，不是损益。
这些数字未含税费、汇率、融资利息和市场冲击；限价仍为 100.00 时，不能假设会在 102.00 成交。

> 本章唯一真问题：即使模型乱改参数、审批过期、响应丢失，系统能否仍证明“谁批准了什么、只执行哪个意图、交易事实来自哪里”？

## 44.2 MCP 边界与分级能力网关

### 44.2.1 三种不同的“能力”

| 层次 | 能说明什么 | 不能推出什么 |
|---|---|---|
| MCP 协议 capability | 服务支持 tools、resources 等协议功能 | 用户有证券账户交易权 |
| 工具 Schema / annotations | 参数形状、描述、行为提示 | 工具可信、只读声明真实、请求幂等 |
| 业务 capability / permit | 经签名的限域执行许可 | 审批永久有效、硬风控可绕过 |

MCP 不是“完全没有安全机制”：固定版本的 HTTP 授权规范定义令牌受众与授权流程。
但 OAuth scope 只解决一部分访问授权，不能替代券商账户归属、适当性、资金冻结、交易限额与审计。
`readOnlyHint`、`destructiveHint`、`idempotentHint` 是提示，不是安全证明；不可信服务的 annotations 也不可信。
JSON-RPC `id` 关联请求响应，MCP 会话标识关联会话；二者均不应充当经济意图或审批身份。
`isError: false` 表示工具执行层没有报错，不等于订单成交；`isError: true` 也不证明外部没有副作用。

### 44.2.2 按后果分级，不按工具名字分级

| 等级 | 能力示例 | 可信闸门 | 模型侧开放方式 |
|---|---|---|---|
| L0 | 公开行情、公开财报查询 | 来源、时效、出站白名单、限流 | 可自动调用 |
| L1 | 账户持仓、订单查询 | 主体与账户绑定、字段脱敏、审计 | 授权后的只读调用 |
| L2 | 计算风险、生成订单提案 | 强类型参数、预算、版本化证据 | 只产生 Proposal |
| L3 | 实盘买卖、撤单、资金划转 | 独立 HITL、硬风控、短期许可、OMS | 只提交已封存 intent_id |
| L4 | 提额、改白名单、改凭据 | 独立管理面、双人复核、变更审计 | 不向模型注册 |

撤单可能解除保护性头寸或冻结资金，不因“减少订单数”自动降为 L0。
权限交集为 `用户授权 ∩ 租户策略 ∩ 账户权限 ∩ 工具许可 ∩ 标的范围 ∩ 风险预算`。
工具列表过滤只是用户体验；服务端必须逐次鉴权，不能因工具没展示就省略后端检查。

```text
不可信文档 → LLM → Proposal
                     ↓ 主体来自可信认证通道，不能取模型提供的 tenant/account
能力网关 → 主数据/Schema → 封存 Intent → 风控与预占 → 独立审批服务
                                                        ↓ 签名凭证
执行服务 → 最终硬闸门 → 持久化执行键/Outbox → OMS → 券商
    ↑                                                   ↓
可信只读投影 ← 回执验源/事件去重/状态归约 ← 查单与成交回报
```

每跳记录 `subject, actor_service, tenant, account, intent_id, trace_id`，区分用户和代理服务。
服务账号不能把任意模型参数当作委托授权；账户映射由认证后的授权目录读取。
入站令牌校验 issuer、受众、期限与 scope；调用下游使用单独的服务身份或正式委托令牌。
禁止把客户端令牌原样透传券商，禁止把券商密钥交给认知区；会话 ID 不是认证凭据。
对新风险在身份源、行情源不可用时 fail closed；对已有 UNKNOWN 保留预占与查询通道。

## 44.3 HITL：签名必须绑定 Intent，而不是绑定一句“同意”

### 44.3.1 不可变意图与审批载荷

以下是业务协议，不是 MCP 新增标准字段；可以封装在工具参数及 `structuredContent` 中。
金额采用 USD 分、数量采用整股，整数在线协议中用十进制字符串，拒绝科学计数法和多余前导零。

```json
{
  "schema_version": 1,
  "intent_id": "I44-001",
  "tenant": "T1",
  "account": "A1",
  "instrument": "XNYS:DEMO",
  "side": "BUY",
  "order_type": "LIMIT",
  "quantity_shares": "100",
  "limit_price_cents": "10000",
  "fee_cap_cents": "100",
  "currency": "USD",
  "time_in_force": "DAY",
  "expires_at_epoch_s": "2000000030",
  "evidence_snapshot_id": "Q44-1"
}
```

网关严格拒绝未知字段、重复 JSON 键、尾随对象、缺字段、非法数值与未知版本。
生产摘要定义为 `SHA-256(domain_separator || canonical_intent_bytes)`，包括全部上述字段。
规范化可采用 RFC 8785；保留规范化原文，禁止用 `jsonb::text` 或语言默认序列化替代约定。
修改账户、标的、方向、数量、价格、有效期或费用上限，均需新 Intent 和新摘要。
新的会话、trace ID、超时重试不能使同一经济意图变成新的 intent_id。

审批服务签署的载荷至少包括：

```text
approval_version, approval_id, intent_id, intent_hash, tenant, account,
approver_subject, decision=ALLOW, policy_version, auth_epoch,
risk_snapshot_id, reservation_id, reference_quote_id,
max_drift_bps, audience=execution-gateway, issued_at, not_before, expires_at, nonce
```

可信审批页面直接读取封存 Intent，显示账户、金额上限、方向、有效期与摘要短码。
用户使用强认证完成审批；审批服务记录认证强度与原始审计证据，再用受控私钥签名。
这是“审批服务证明用户做过指定决策”，不是声称用户本人持有同一服务签名私钥。
签名算法、kid 与密钥用途固定；拒绝未知算法、错误受众、撤销密钥及不存在的 reservation。
审批人与提案人职责分离由策略决定；高风险动作可要求双人批准，模型不能成为审批人。

### 44.3.2 价格漂移、过期与最终送单闸门

以可信参考价 `P0 > 0` 和送单前同口径新价 `P1` 计算：

```text
drift_bps = abs(P1 − P0) × 10000 / P0
超阈值判断：abs(P1 − P0) × 10000 > max_drift_bps × P0
```

整数交叉相乘避免舍入误放行；产品策略可用买卖方向、价差或最优档位口径，但必须版本化。
例：P0=10000 分、P1=10060 分，漂移 60bp；阈值 50bp，旧审批失效。
行情超过新鲜度阈值、停牌、授权撤销、策略切换、风险快照变化，同样触发重新判断。
漂移阈值只规定审批容忍区间，不允许把限价 100.00 偷改为 100.60。
限价不变也必须按策略重算可用资金、保证金、集中度、流动性与共享预算。
本章保守策略：有效期届满、漂移超阈值或关键上下文变更，旧批准标记 STALE，重新风控并人工重审。
经济参数未变时可保留原 intent_id，追加新的审批记录；经济参数变更则创建后继 Intent。
后继 Intent 不能绕过仍处于 UNKNOWN、在途或部分成交的前序风险；先查单、撤单或聚合计入敞口。
人工再次点击批准也不能覆盖硬限额、禁买资产或授权撤销。

最终检查发生在执行区、尽量靠近出站送单：验签、验摘要、验身份、验期限、验撤销、重风控。
检查与预算消费/执行占位在同一受控事务内完成，提交后通过 Outbox 分发。
签名消费唯一键和预算锁避免并发双花；worker 还需短期发送租约及版本栅栏。
数据库事务无法原子覆盖外部市场与权限变更；必须定义最后授权点、最大出站延迟和重检规则。
超出延迟或闸门失效且尚未出站时阻止发送；可能已出站则进入 UNKNOWN，不能当作未提交。
审批到期不等于已挂在市场的 DAY 单自动撤销；需要独立撤单动作与撤单结果证明。

## 44.4 DDL、执行协议与 UNKNOWN 收敛

### 44.4.1 PostgreSQL 15+ 教学持久化骨架

以下只表达键与引用关系，不是完整账本、预算或安全迁移脚本；各表置于同一受控数据库。

```sql
CREATE TABLE fin44_intent (
  tenant text NOT NULL, intent_id text NOT NULL, account text NOT NULL,
  canonical_bytes bytea NOT NULL, intent_hash bytea NOT NULL,
  expires_at timestamptz NOT NULL,
  PRIMARY KEY (tenant, intent_id),
  UNIQUE (tenant, intent_id, intent_hash),
  CHECK (octet_length(intent_hash) = 32)
);
CREATE FUNCTION fin44_no_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'sealed record is append-only'; END;
$$;
CREATE TRIGGER fin44_intent_immutable BEFORE UPDATE OR DELETE ON fin44_intent
FOR EACH ROW EXECUTE FUNCTION fin44_no_mutation();
CREATE TABLE fin44_approval (
  tenant text NOT NULL, approval_id text NOT NULL, intent_id text NOT NULL,
  intent_hash bytea NOT NULL, approver_subject text NOT NULL,
  policy_version text NOT NULL, auth_epoch bigint NOT NULL,
  expires_at timestamptz NOT NULL, signed_payload bytea NOT NULL, signature bytea NOT NULL,
  PRIMARY KEY (tenant, approval_id), UNIQUE (tenant, approval_id, intent_id),
  FOREIGN KEY (tenant, intent_id, intent_hash)
    REFERENCES fin44_intent (tenant, intent_id, intent_hash)
);
CREATE TRIGGER fin44_approval_immutable BEFORE UPDATE OR DELETE ON fin44_approval
FOR EACH ROW EXECUTE FUNCTION fin44_no_mutation();
CREATE TABLE fin44_execution (
  tenant text NOT NULL, intent_id text NOT NULL, approval_id text NOT NULL,
  venue_scope text NOT NULL, cl_ord_id text NOT NULL,
  state text NOT NULL CHECK (state IN
    ('READY','DISPATCHING','UNKNOWN','WORKING','PARTIAL','FILLED','CANCELED','REJECTED')),
  version bigint NOT NULL DEFAULT 0, last_evidence_id text,
  PRIMARY KEY (tenant, intent_id), UNIQUE (venue_scope, cl_ord_id),
  UNIQUE (tenant, approval_id),
  FOREIGN KEY (tenant, intent_id) REFERENCES fin44_intent,
  FOREIGN KEY (tenant, approval_id, intent_id)
    REFERENCES fin44_approval (tenant, approval_id, intent_id)
);
CREATE TABLE fin44_outbox (
  event_id text PRIMARY KEY, tenant text NOT NULL, intent_id text NOT NULL,
  kind text NOT NULL CHECK (kind = 'INITIAL_SUBMIT'), payload bytea NOT NULL,
  published_at timestamptz, UNIQUE (tenant, intent_id, kind),
  FOREIGN KEY (tenant, intent_id) REFERENCES fin44_execution
);
CREATE TABLE fin44_evidence (
  evidence_id text PRIMARY KEY, tenant text NOT NULL, intent_id text NOT NULL,
  source text NOT NULL, source_event_id text NOT NULL,
  observed_at timestamptz NOT NULL, raw_hash bytea NOT NULL,
  raw_object_ref text NOT NULL, UNIQUE (source, source_event_id),
  CHECK (octet_length(raw_hash) = 32),
  FOREIGN KEY (tenant, intent_id) REFERENCES fin44_execution
);
CREATE INDEX fin44_unknown_scan ON fin44_execution (state) WHERE state = 'UNKNOWN';
```

`venue_scope` 应含券商、账户/发送方与协议去重域；`source` 同样包含来源事件 ID 的完整命名空间。
生产必须另加预算预占表、审批撤销/失效事件、执行尝试、租约、成交明细、账本凭证与归档策略。
触发器不是防 DBA 篡改的完整方案；应用角色禁 DDL/TRUNCATE，证据采用访问隔离及不可变留存。
表约束不自动验证签名，也不保证 canonical_bytes 与摘要一致；只允许可信验证服务写入。
接单事务依次锁定 Intent、比较摘要、检查已有执行、锁预算、验证许可、预占、插入 READY 与 Outbox。
同键同摘要返回已有执行状态；同键异摘要返回业务冲突，绝不 UPDATE 覆盖旧参数。
首次消费审批与建立执行记录原子提交；后续同意图查询不因“审批已消费”报错，更不能再次扣预算。
Outbox 只证明可重放投递，不等于可无条件再次发单；重复消息先读取执行状态与发送尝试。

### 44.4.2 intent_id → ClOrdID：先持久化，再发出去

首次接单在数据库中生成、保存稳定映射 `(tenant, intent_id) → (venue_scope, ClOrdID)`。
ClOrdID 满足券商长度、字符集与跨日唯一要求；禁止每次重试重新生成、截断后碰撞或忽略租户命名空间。
FIX ClOrdID 是客户订单标识，不是 FIX 或 MCP 对任意渠道承诺的永久幂等键。
必须向券商核实重复提交行为、重传规则、查单一致性、跨日范围、历史保留期与故障恢复契约。
会话 SeqNum、PossDupFlag 和业务 ClOrdID 分属不同层；不可让应用自行构造新单绕开 FIX 恢复流程。

```text
READY → DISPATCHING → WORKING → PARTIAL → FILLED
                  ↘ UNKNOWN → 可信查单/回执 → WORKING/PARTIAL/FILLED/CANCELED/REJECTED
WORKING/PARTIAL → 撤单在途 → CANCELED 或更多成交
UNKNOWN → 无权威结论 → 保留原键、预占和人工事件，不创建第二笔
```

超时、连接断开、进程在发送窗口崩溃，均可能意味着已经被券商接受。
UNKNOWN 时用原 ClOrdID 与原 venue_scope 查单；单次 NOT_FOUND 可能是最终一致性延迟，不是失败凭证。
禁止换键重下、换券商“补单”、自动释放冻结或把 UNKNOWN 改为 REJECTED。
只有渠道提供明确的同键安全重送契约，且恢复程序验证其条件时，才可按原键重送。
没有该契约时停止自动重送，持续查单并升级人工；人工也不得凭感觉消灭潜在成交义务。
确认原请求未受理需要渠道权威否定证据及协议规定的观察窗口，而非本地重试次数达到上限。
撤单/改单是独立命令，拥有各自稳定键，通过 OrigClOrdID 等字段引用原单，不能复用原始新单键。
状态归约以成交事实、撤单事实及累积数量约束为准；迟到 WORKING 不覆盖已知 FILLED。

## 44.5 Go 与 Java：相同的审批检查核心

### 44.5.1 Go 1.24+ 标准库示例

下例仅演示固定载荷的 Ed25519 验签、摘要绑定与整数漂移；不是 MCP SDK，也不执行下单。
载荷用限定 ASCII 字段和固定顺序换行编码，无末尾换行；这不是完整 Intent 的生产规范化实现。

```go
package main

import (
    "crypto/ed25519"
    "crypto/rand"
    "crypto/sha256"
    "encoding/hex"
    "fmt"
    "math/big"
    "strconv"
    "strings"
)

type Approval struct {
    Hash string
    Expires int64
    Drift int64
}

func payload(a Approval) []byte {
    return []byte(strings.Join([]string{
        "approval-v1", "T1", "A1", "I44-001", a.Hash,
        "execution-gateway", strconv.FormatInt(a.Expires, 10),
        strconv.FormatInt(a.Drift, 10), "nonce-1",
    }, "\n"))
}

func check(pub ed25519.PublicKey, a Approval, sig, intent []byte,
    now, reference, current int64) error {
    if len(pub) != ed25519.PublicKeySize ||
        !ed25519.Verify(pub, payload(a), sig) {
        return fmt.Errorf("bad signature")
    }
    h := sha256.Sum256(intent)
    if a.Hash != hex.EncodeToString(h[:]) {
        return fmt.Errorf("intent hash mismatch")
    }
    if now >= a.Expires || reference <= 0 || current <= 0 ||
        a.Drift < 0 || a.Drift > 10000 {
        return fmt.Errorf("expired or invalid input")
    }
    delta := new(big.Int).Sub(big.NewInt(current), big.NewInt(reference))
    delta.Abs(delta)
    left := new(big.Int).Mul(delta, big.NewInt(10000))
    right := new(big.Int).Mul(big.NewInt(reference), big.NewInt(a.Drift))
    if left.Cmp(right) > 0 {
        return fmt.Errorf("STALE: risk review and human reapproval")
    }
    return nil // 仅通过这些纯函数检查，不代表获得实盘执行权。
}

func main() {
    intent := []byte("sealed-intent-demo-v1")
    h := sha256.Sum256(intent)
    a := Approval{hex.EncodeToString(h[:]), 2000000030, 50}
    pub, priv, err := ed25519.GenerateKey(rand.Reader)
    if err != nil { panic(err) }
    sig := ed25519.Sign(priv, payload(a))
    fmt.Println(check(pub, a, sig, intent, 2000000000, 10000, 10060))
}
```

该输入的预期结果是 STALE，因为 60bp 大于 50bp；不是本章已运行得到的日志。
临时生成密钥仅为展示；生产签名必须由独立审批服务/KMS 完成，执行服务只持有受信公钥。

### 44.5.2 Java 21+ 标准库同口径示例

保存为 `ApprovalDemo.java` 可作为独立练习；仍不包含持久化消费、权限目录、行情验源或硬风控。

```java
import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.security.*;
import java.util.HexFormat;

public class ApprovalDemo {
    record Approval(String hash, long expires, long drift) {}

    static byte[] payload(Approval a) {
        return String.join("\n", "approval-v1", "T1", "A1", "I44-001",
            a.hash(), "execution-gateway", Long.toString(a.expires()),
            Long.toString(a.drift()), "nonce-1")
            .getBytes(StandardCharsets.UTF_8);
    }

    static String hash(byte[] bytes) throws GeneralSecurityException {
        return HexFormat.of().formatHex(
            MessageDigest.getInstance("SHA-256").digest(bytes));
    }

    static String check(PublicKey key, Approval a, byte[] signature,
                        byte[] intent, long now, long reference, long current)
                        throws GeneralSecurityException {
        Signature verifier = Signature.getInstance("Ed25519");
        verifier.initVerify(key);
        verifier.update(payload(a));
        if (!verifier.verify(signature)) return "bad signature";
        if (!a.hash().equals(hash(intent))) return "intent hash mismatch";
        if (now >= a.expires() || reference <= 0 || current <= 0 ||
            a.drift() < 0 || a.drift() > 10000) {
            return "expired or invalid input";
        }
        BigInteger delta = BigInteger.valueOf(current)
            .subtract(BigInteger.valueOf(reference)).abs();
        BigInteger left = delta.multiply(BigInteger.valueOf(10000));
        BigInteger right = BigInteger.valueOf(reference)
            .multiply(BigInteger.valueOf(a.drift()));
        return left.compareTo(right) > 0
            ? "STALE: risk review and human reapproval"
            : "pure checks passed; not an execution permit";
    }

    public static void main(String[] args) throws GeneralSecurityException {
        byte[] intent = "sealed-intent-demo-v1".getBytes(StandardCharsets.UTF_8);
        Approval a = new Approval(hash(intent), 2000000030L, 50);
        KeyPair pair = KeyPairGenerator.getInstance("Ed25519").generateKeyPair();
        Signature signer = Signature.getInstance("Ed25519");
        signer.initSign(pair.getPrivate());
        signer.update(payload(a));
        System.out.println(check(pair.getPublic(), a, signer.sign(), intent,
            2000000000L, 10000, 10060));
    }
}
```

两个例子的载荷编码相同，但没有宣称完成跨语言验签测试；应另共享公钥、签名和字节级黄金向量。
异常统一拒绝新风险，不允许捕获验签异常后默认 ALLOW；时间必须来自可信服务器时钟。
示例省略完整审批字段、not_before、撤销、审批人绑定、nonce 消费和 reservation 检查，不可直接上线。

## 44.6 可验证实盘回执：刷新事实，不刷新模型的自信

可信查询工具接受 intent_id；网关先验证账户读权限，再解析已保存的 ClOrdID，禁止模型另传任意账号查单。
响应的业务结构至少包含以下字段；内容为教学占位示例，不是实际实盘回执。

```json
{
  "mode": "ILLUSTRATION",
  "intent_id": "I44-001",
  "cl_ord_id": "C44-001",
  "business_status": "UNKNOWN",
  "cum_qty_shares": "0",
  "leaves_qty_shares": null,
  "as_of": "2033-05-18T03:33:20Z",
  "evidence_id": "E44-001",
  "source": "broker-adapter-demo",
  "freshness": "STALE"
}
```

只有生产适配器从已认证券商连接、查单端点或受控回报通道取得并验证证据，才能输出 `mode=LIVE`。
验证包括通道身份、账户与 ClOrdID 匹配、消息完整性、事件唯一性、数量/金额约束和时序归约。
来源支持签名时验签；不支持时保留认证通道及原始报文证据，不能编造“券商数字签名”。
哈希只能证明归档字节未变，不能单独证明来源真实；evidence_id 也必须能被有权限的审计者解析。
UNKNOWN 的 `cum_qty=0` 仅表示目前未确认成交量，不能解释为真实成交为零；未知字段使用 null。
刷新按钮调用可信只读查询并更新投影，显示 as_of、最后同步时间、来源和过期标识。
LLM 只能转述这份结果，不能写入状态、补造成交 ID，或把上轮记忆当作本轮回执。
本地缓存过期或查单失败时显示“结果未知/数据陈旧”，不能沿用绿色“成交成功”而隐藏时间。
工具执行成功、券商受理、部分成交、全成、清算与结算必须分开显示；全成不代表交割已经完成。
账本仅消费去重后的已验证成交/费用事件；最终通过订单、成交、资金、持仓及结算单多层对账证明。

## 44.7 对账、故障注入、压测与面试自检

### 44.7.1 故障测试矩阵

| 注入点 | 必须观察到的结果 | 禁止结果 |
|---|---|---|
| 同 intent 并发 100 次接单 | 一个执行占位、一份预占、同一 ClOrdID | 100 份预算冻结或多个新单键 |
| 同键换账户/价格/数量 | 摘要冲突且无新增 Outbox | 覆盖 Intent 后继续执行 |
| 审批重放到另一租户/受众 | 身份或签名载荷检查拒绝 | 仅看 ALLOW 字符串放行 |
| 审批过期、撤销、漂移 51bp | 无新风险，重新风控并人工重审 | 静默延长签名期限 |
| 漂移恰好 50bp | 通过该阈值检查，仍检查其他硬闸门 | 直接宣称可下单 |
| 发送成功后丢响应 | UNKNOWN、保留原键并查单 | 换键/换券商重下 |
| DISPATCHING 后进程崩溃 | 恢复程序按可能已发送处理 | 无条件回到 READY 重发 |
| 连续 NOT_FOUND 后迟到成交 | 同键收敛到真实成交并保持预算 | 提前释放冻结、再下一笔 |
| 重复/乱序成交与撤单回报 | 去重，累计量正确，证据可追溯 | 重复记账或终态倒退 |
| 伪造回执/跨账户 evidence_id | 来源与权限检查失败 | 模型解释后写为 FILLED |
| 撤单确认前又有成交 | 保留实际成交，只释放已确认剩余义务 | 按“请求撤单”释放全部冻结 |
| 审批页被注入工具文本 | 只展示可信封存字段，拒绝指令执行 | 模型替换审批对象 |

每轮对账比较内部执行表、券商查单/成交、资金预占、账本分录与日终结算单。
按原始业务键归集差异，记录差异金额、账龄、来源版本、责任服务及处置审批；不能只比订单总数。
超龄 UNKNOWN 升级人工并可暂停账户新增风险，不能用后台定时任务批量改为 FAILED。
补记账、冲正或释放预占都要有对应证据和独立幂等键；不得删除差异来“对平”。

### 44.7.2 压测是验证计划，不是吞吐宣传

只在隔离仿真环境使用模拟券商与合成账户，禁止拿实盘下单压测或把审批自动点击当真实 HITL。
基线设计：100 个账户、每账户 10 个并发，持续 30 分钟，20% 同键重试，5% 接受后丢响应。
另测单热账户、冷缓存、主备切换、数据库提交歧义、查单限流及回执延迟；分别控制变量。
人工审批延迟用可审计测试夹具模拟，测的是排队与过期处理，不是人的真实决策能力。
记录接单/查询 p50/p95/p99、预算锁等待、队列深度、UNKNOWN 账龄、查单流量与恢复收敛时间。
业务验收硬门槛：越权受理数为 0，同意图被渠道接受的初始订单数不超过 1，重复记账数为 0。
同时断言 `spent + reserved ≤ limit`、预占明细与总账一致、所有终态有证据且差异未被吞掉。
“出站请求次数大于一”不必然重复成交；“HTTP 成功率 100%”也不证明没有重复单，必须核对渠道事实。
性能门槛应由真实 SLO 和渠道速率协商给出；本章不提供未经实测的 TPS、延迟或恢复秒数。
**实测界限**：独立提取的 Go/Java 标准库示例已在本环境 Go 1.19、Java 17 编译运行，均输出 `STALE: risk review and human reapproval`；未验证目标 Go 1.24/Java 21。未连接 MCP/券商、未执行 DDL、未开展真实故障注入或压测；阈值均为教学设定。

### 44.7.3 术语与面试自检

| 术语 | 本章含义 | 常见混淆 |
|---|---|---|
| HITL | 人对具体不可变意图做可审计决策 | 聊天中说“继续”就是签名授权 |
| intent_hash | 规范化经济命令的摘要 | 对话摘要、模型理由的哈希 |
| ClOrdID | 客户在约定命名空间内的订单号 | MCP request id 或永久幂等保证 |
| UNKNOWN | 外部业务结果尚无充分证据 | 已失败、可换键重试 |
| capability | 限域业务许可或协议功能，须注明层次 | 出现在 tools/list 就可执行 |
| receipt / evidence | 可验来源、可追溯原文的业务证据 | LLM 生成的“成功”句子 |

1. 为什么 OAuth 验证通过后，还要检查账户绑定和交易许可？分别说明认证、授权与风险接受。
2. 用户批准后价格涨 60bp，阈值 50bp，是否可以改限价？应先失效、重风控、重审，改价须新 Intent。
3. 为什么把 intent_id 换成 UUID 就能“重试成功”反而危险？它可能创建第二个经济事实。
4. 审批过期但订单可能已发送，能否释放预算？不能，审批有效性与已产生的在途义务分开处理。
5. 如何证明一次 FILLED？说明可信回报、累计量归约、成交事件去重、账本及后续对账证据。
6. 两个 worker 同时接管 UNKNOWN 怎么办？数据库栅栏限制本地动作，渠道契约决定是否可同键重送。
7. 渠道不支持同键幂等、查单又长期未知，能否承诺 exactly-once？不能，应保留风险并升级人工。

## 44.8 来源与适用范围

以下资料用于核对协议语义与工程原则；版本按列出的链接固定，不把旧版要求称为最新规范。

1. [MCP 2025-06-18 Tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)：工具发现、调用、结果结构、人工确认建议与 annotations 信任边界。
2. [MCP 2025-06-18 Authorization](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization)：HTTP 授权、受众校验与禁止令牌透传；不是证券账户的完整授权模型。
3. [MCP 2025-06-18 Security Best Practices](https://modelcontextprotocol.io/specification/2025-06-18/basic/security_best_practices)：confused deputy、token passthrough 等威胁；落地时另核对所采用协议版本。
4. [RFC 8785：JCS](https://www.rfc-editor.org/rfc/rfc8785)：可重复规范化、重复键与大整数处理；它不定义本章的金融审批字段。
5. [RFC 8032：Ed25519](https://www.rfc-editor.org/rfc/rfc8032)：签名算法依据；签名不替代授权撤销、时间检查及持久化防重放。
6. [FIX 4.4 ClOrdID 字典（OnixS）](https://www.onixs.biz/fix-dictionary/4.4/tagnum_11.html)：客户订单标识及跨日唯一考虑；具体券商幂等与恢复契约须单独核实。
7. [OWASP Transaction Authorization Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Transaction_Authorization_Cheat_Sheet.html)：重要交易数据绑定、独立授权与最终控制闸门。
8. [PostgreSQL Constraints](https://www.postgresql.org/docs/15/ddl-constraints.html) 与 [Transaction Isolation](https://www.postgresql.org/docs/15/transaction-iso.html)：唯一约束、外键与并发事务边界；不覆盖外部券商副作用。

分级 L0–L4、50bp、30 分钟及本章表结构均为教学设计，不是 MCP 强制标准、监管统一要求或券商 SLA。
真实部署还需法律合规、适当性、数据许可、密钥托管、业务连续性与渠道认证；本章不构成投资建议或上线认证。
