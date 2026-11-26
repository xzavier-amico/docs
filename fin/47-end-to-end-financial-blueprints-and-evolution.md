# 47. 端到端金融蓝图与演进：跨境支付、互联网券商、加密交易所 + AI

> **状态**：深入工程篇｜**所属**：第六部分·全栈收束｜**编号**：47 / 48
>
> **唯一真问题**：把前面各章拼起来以后，谁能改变金融事实，谁负责证明没有多付、多买、多提？
>
> **交付边界**：三套装配蓝图、共享协议与 PostgreSQL 15+ 教学 DDL、Go/Java 可运行状态归约小核心、演进与验收方案。
> 小核心不接网络、不签名、不写数据库；装配骨架不是完整产品，更不代表复制即可投产。
> 全部机构、事故、流量、成本、延迟和恢复目标均为教学假设；没有把设计目标写成实测成绩。

## 47.1 教学事故：预算缩水后，为什么“兜底重试”变成第二次付款

### 47.1.1 合成事故时间线与损失口径

以下事故完全虚构：一家教学支付商在削减预算时，保留了推荐模型，却停掉主动查单和日内对账。
客户只批准向境外收款人支付 10,000.00 USD；两条渠道都能独立完成付款。

| 时间 | 行为 | 失守的边界 |
|---|---|---|
| 10:00:00 | 冻结客户 10,000.00 USD，渠道 A 接收请求 K1 | 此时只有一个经济意图 |
| 10:00:02 | A 响应丢失，应用超时 | 数据库被错误写为 FAILED |
| 10:00:03 | 模型建议“切换更便宜渠道”，服务生成 K2 交给 B | 未确认 A 失败便创造第二份义务 |
| 10:00:05 | B 成功，释放冻结并扣账一次 | 账本只看见 B 的回执 |
| 10:01:00 | A 实际也成功，迟到回调被当重复消息丢弃 | 用业务单号去重抹掉不同外部支付 |
| 次日 | 银行流水显示两笔出账 | 缺少总分、渠道、银行多层核对 |

设 A 收费 8.00 USD、B 收费 5.00 USD，且费用从企业自有资金支付。
实际现金流出为 `10,000.00 × 2 + 8.00 + 5.00 = 20,013.00 USD`。
按原计划只走 A，基线支出为 `10,008.00 USD`；额外现金占用为 `10,005.00 USD`。
若追回第二笔本金，只能确认额外费用 5.00 USD，不能把暂挂应收当成已实现本金损失。
若第二笔本金确认无法追回，才按适用会计政策确认对应损失；追回费用另计。
客户负债不能因此多扣 10,000.00 USD；多付款先进入企业差错应收与损失处理流程。

### 47.1.2 收束工程篇的六个不变量

1. 金额、数量、汇率、单位与舍入策略来自版本化主数据，不从模型文本猜测。
2. 已入账分录是内部资金权益的唯一事实源；余额、报表与 AI 记忆均为可重建投影。
3. 外部成交、银行到账、链上最终性仍由各自可信证据决定，内部账本不能替外部世界宣布成功。
4. 超时进入 UNKNOWN，保留义务与预占；换语言、进程、渠道不能产生第二次经济执行。
5. 对账是发现边界失配的独立控制，不能与记账逻辑共享全部输入和错误假设。
6. 认知区只有提案权；硬风控、审批、执行、签名与管理权限必须分开。

## 47.2 三套端到端蓝图：共用底座，不混用法律与业务边界

### 47.2.1 共用边界与主数据所有权

```text
客户/运营 → 身份与授权 → 版本化主数据 → 不可变意图 → 确定性风控/审批
                                                         ↓
研究资料 → 隔离的 AI 认知区 → Proposal ────────────────→ 意图编译器
                                                         ↓
                   资金预占 + 事务 Outbox → 专用执行适配器 → 外部机构/链
                         ↓                     ↑                  ↓
                    Ledger 分录 ← 事件归约/验源/去重 ← 回执/查单/最终性证据
                         ↓                                  ↓
                   可重建读模型 ← 独立对账/差错工单 ← 独立账单/节点/托管记录
```

| 主数据 | 唯一管理者 | 必须带入意图的版本/维度 |
|---|---|---|
| 客户与账户 | 身份/账户域 | 法人、租户、账户归属、授权版本 |
| 法币与数字资产 | 资产目录 | asset_id、scale、链 ID、合约地址、启停状态 |
| 证券与市场 | 标的目录 | 内部标的 ID、市场、交易单位、公司行动有效期 |
| 时间与结算规则 | 日历域 | 时区、假日表、cutoff、业务日、规则版本 |
| 路由与费用 | 支付/执行策略域 | 渠道账户、报价期限、费率版本、限额 |
| 会计科目 | 财务授权的账务域 | 法人、科目性质、资产、正常借贷方向 |

USD、USDC 与不同链上的同名 USDC 不是同一资产；统一展示价不等于可混记。
主数据采用不可变版本与生效区间，历史分录保留当时版本，不能随目录更新被重新解释。
交易状态由业务域归约；Ledger 拥有入账事实，但不拥有证券登记或区块链共识。
不同法人分账，客户资金与运营资金分账；跨法人往来须有双方明确凭证与对账。
服务只拥有自己的写库权限；共享契约不等于两个语言服务同时写同一余额表。

### 47.2.2 蓝图 A：跨境支付 + AI 客服/异常归因

```text
付款授权 → KYC/制裁筛查 → 锁汇/费用报价 → 资金冻结 → 支付编排
                                                      ↓
本地入金/银行凭证 → Ledger ← 结果归约 ← 渠道 A / B → 收款行
                           ↑               ↓
                       主动查单 ← UNKNOWN 队列
银行/Nostro 账单 + 渠道结算单 → 四方对账 → 差错工单/受控冲正
脱敏工单 → AI 解释与补证建议 → 人工复核；不得让 AI 修改路由中订单
```

边界一：支付成功、渠道结算、银行清算不是一个状态；先定义向客户承诺的是哪种完成。
边界二：锁汇报价必须绑定币对、方向、金额、费用、期限与流动性提供方；过期重新审批。
边界三：同一个意图下每次外部尝试有 attempt_id，外部效果标识不能只用客户端请求号。
切路由必须先取得“原尝试无效且以后不会生效”的可靠证据；不具备条件就挂起人工处理。
迟到成功不可丢弃：它可能是第二次实际付款，要入差错流程而不是再扣客户余额。
AML 命中可能导致冻结、拒绝或法定处置，不能把所有命中统一自动退款。

手算锁汇：客户交付 1,000.00 USD，收取 5.00 USD 服务费，剩余 995.00 USD 兑换。
教学报价为 `7.2000 CNY/USD`，收款金额为 `995.00 × 7.2000 = 7,164.00 CNY`。
USD 分录独立平衡，CNY 分录独立平衡，通过汇兑过渡科目连接，不把两币种原子相加。
若报价计算产生不足一分的尾差，按报价契约落入指定尾差科目，不静默修改收款金额。
对账至少核对客户意图、渠道订单、内部凭证、银行实际出入账，并解释费用与时点差。

### 47.2.3 蓝图 B：互联网券商 + AI 投研/受限下单

```text
开户/适当性 → 入金/换汇 → 账户现金与证券子账
用户或 AI Proposal → 意图封存 → 购买力/价格带/HITL → OMS → FIX → 执行券商
                                                       ↑             ↓
                       现金预占 ← 成交归约/去重/补包 ← ExecReport/查单
成交 → 清算义务 → 交收 → 现金/证券结算分录 → 总分核对/托管对账
公司行动 + 交易日历 → 应收/应付与持仓变更；PIT 投研只读取时点一致快照
```

券商内部持仓子账、上游 omnibus 总账户与中央登记记录的职责不同，不能以一张表替代全部。
现金可用、已交收现金、购买力、可卖证券与已交收持仓分别定义，避免误把未交收视为可提。
订单接受不等于成交，成交不等于交收；FIX 会话序号也不是跨渠道的成交唯一标识。
撤单请求待确认时仍可能成交，预占按剩余潜在义务释放，而非按“用户点了撤单”释放。
公司行动与历史成交更正产生新事件、冲正与补记，不覆盖旧持仓来源。
研究区不接触券商密钥，模型不能通过“修正建议”直接改 OMS 已封存的数量和价格。

手算委托：买 100 股，限价 25.00 USD，费用上限 2.00 USD，预占 2,502.00 USD。
先成交 40 股、价格 24.90 USD，若此时已确认费用 0.80 USD，已消耗现金为 996.80 USD。
余单仍可能需要 `60 × 25.00 + 1.20 = 1,501.20 USD`，合计义务 2,498.00 USD。
在费用上界可证明分配的前提下，可释放 4.00 USD；若费用有最低收费则重新按真实规则计算。
最终撤单确认只能释放剩余义务，不能撤回已成交的 40 股或抹掉后到成交回报。
对账以 ExecID 的渠道/账户作用域去重，并核对成交总量、费用、现金与托管证券数量。

### 47.2.4 蓝图 C：加密交易所 + AI 研究/运营辅助

```text
充值地址 → 多节点扫块 → 合约/资产验源 → 确认与最终性 → 充值账务
订单 → 硬风控/资产预占 → 确定性撮合 → WAL/复制 → 成交 → 清算分录
                                             ↓               ↓
                                        行情投影          Ledger/权益投影
提币请求 → 风险/白名单/审批 → 预占 → 独立签名区 → 广播 → UNKNOWN/最终性
热温冷钱包/链上证据 + 客户负债 + 总账 → 储备与负债核对/差错闭环
AI 研究与 AML 摘要 → 提案/证据包；不接签名器、不改 nonce、不直连撮合
```

撮合日志是订单与成交顺序的事实源；客户资金权益以账务分录为准，两者通过有序清算核对。
若清算落后，必须以序号水位与风险预占限制新增风险，不能把迟到的可用余额再次花掉。
链上余额是托管资产证据，不等于全部客户权益，也不等于完整偿付能力证明。
充值按链、合约、交易、日志位置与区块哈希定位；重组事件触发冻结与受控冲正。
同 nonce 提价替换要保留同一提币经济意图和交易候选关系，不能额外签第二份独立提币。
衍生品保险基金、运营资金和客户资产隔离；损失分摊必须符合明确规则，不能临时裸改余额。

手算现货：卖方交付 0.10000000 BTC，买方支付 6,000.00 USDT，教学价格为 60,000.00。
若买方手续费为 0.10% 且以 USDT 收取，则买方现金义务为 6,006.00 USDT。
BTC 与 USDT 分别记平；手续费 6.00 USDT 是客户负债减少与费用收入增加的对应记录。
链上归集在同一法人钱包间移动资产，不应被重复识别为客户新充值或平台收入。
任何链外授信和借贷义务都要纳入负债范围，不能用“链上储备够多”跳过核查。

### 47.2.5 风险隔离矩阵

| 区域 | 可读 | 可写/可执行 | 必须拒绝 |
|---|---|---|---|
| AI 认知区 | 脱敏、授权、带时点证据 | Proposal、草稿 | 账库写权限、渠道密钥、任意 URL 工具 |
| 策略与审批区 | 主数据、限额、证据 | 版本化规则、绑定摘要的许可 | 自批自执、模型自行提额 |
| 事务执行区 | 封存意图、有效许可 | 幂等命令、预占、Outbox | 接受未审批的新经济参数 |
| 签名/托管区 | 精确执行字节与授权上下文 | 限域签名、独立撤销 | 模型输入原始私钥、任意 calldata |
| 对账/审计区 | 独立外部证据、不可变分录 | 差错单、受审批修复请求 | 自动抹平差额、直接 UPDATE 余额 |

## 47.3 双栈共用契约与最小 DDL：同语义，比“同框架”更重要

### 47.3.1 Go 与 Java 的边界及版本策略

一种可选分工是 Go 承担网关、链节点适配与行情扇出，Java 承担 OMS、清结算与运营工作流。
这不是语言性能定律：已有团队与运行时经验优先；同一模块只设一个权威写入者。
Go 可选 pgx/sqlc，Java 可选 JDBC/jOOQ 与显式事务；数据库唯一约束而非进程锁决定幂等。
Protobuf 负责传输形状，业务验证器负责金额语法、版本兼容、权限与状态约束。

以下 `intent.proto` 是装配契约；生成器、依赖锁定和 RPC 服务尚需集成，不属于后面的独立小核心。

```protobuf
syntax = "proto3";
package fin.blueprint.v1;
option go_package = "example.invalid/fin/gen/blueprint/v1;blueprintv1";
option java_package = "example.fin.blueprint.v1";
option java_multiple_files = true;
message Intent {
  string tenant_id = 1;
  string intent_id = 2;
  string idempotency_key = 3;
  string asset_id = 4;
  string amount_atoms = 5; // 正整数十进制字符串，禁止指数与前导零
  uint32 contract_version = 6;
  string master_snapshot = 7;
  bytes canonical_digest = 8; // 经约定规范编码后的摘要，不直接哈希 protobuf 字节
  string account_id = 9;
  string action = 10; // 只接受版本目录中的枚举值，未知动作拒绝
  string expires_at_epoch_ms = 11;
}
message Receipt {
  string provider = 1;
  string provider_account = 2;
  string event_id = 3;
  string intent_id = 4;
  string effect_id = 5;
  string outcome = 6; // ACCEPTED、SETTLED、REJECTED；需验源与最终性校验
  bytes evidence_digest = 7;
}
```

结构包版本 v1、业务 contract_version、主数据 snapshot 与数据库迁移号分别管理，不能共用一个数字。
增加可选字段可兼容旧读取者，但旧执行者若不理解新经济语义必须拒绝，不能忽略后执行。
删除字段要 `reserved` 原字段号和名称；不复用字段号，不就地改变单位、类型或语义。
摘要覆盖主体、账户、动作、资产、金额、期限、策略和报价等全部经济字段；上例并非完整交易 Schema。
规范编码约定字段顺序、UTF-8、长度前缀、空值与整数语法，提供跨语言 golden vectors。
Protobuf deterministic serialization 不等于跨语言、跨版本的密码学规范编码。
数据库先扩展再迁移再收缩；事件消费者双读兼容窗口明确到版本与截止时间。

### 47.3.2 PostgreSQL 15+ 最小装配 DDL

此 DDL 是独立教学 schema，不用于覆盖前章表；省略账户余额缓存与生产权限角色的创建。
每张 journal 行只表达同资产、同法人两个科目的等额转移，因结构固定而天然借贷平衡。
它不能表达完整多腿凭证、购买力或防透支；后者必须集成账户锁、预占与第 22 章事务协议。

```sql
CREATE SCHEMA bp47;
CREATE TABLE bp47.asset (
  asset_id text PRIMARY KEY,
  scale smallint NOT NULL CHECK (scale BETWEEN 0 AND 30),
  master_version bigint NOT NULL CHECK (master_version > 0)
);
CREATE TABLE bp47.account (
  tenant_id text NOT NULL,
  account_id text NOT NULL,
  asset_id text NOT NULL REFERENCES bp47.asset,
  PRIMARY KEY (tenant_id, account_id, asset_id)
);
CREATE TABLE bp47.intent (
  tenant_id text NOT NULL,
  intent_id text NOT NULL,
  idem_key text NOT NULL,
  digest bytea NOT NULL CHECK (octet_length(digest) = 32),
  contract_version integer NOT NULL CHECK (contract_version > 0),
  master_snapshot text NOT NULL,
  state text NOT NULL CHECK (state IN
    ('PREPARED','SENT','UNKNOWN','SETTLED','REJECTED')),
  version bigint NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, intent_id),
  UNIQUE (tenant_id, idem_key)
);
CREATE TABLE bp47.journal (
  tenant_id text NOT NULL,
  journal_id text NOT NULL,
  intent_id text NOT NULL,
  leg_key text NOT NULL,
  asset_id text NOT NULL,
  debit_account text NOT NULL,
  credit_account text NOT NULL,
  atoms numeric(78,0) NOT NULL CHECK (atoms > 0),
  business_day date NOT NULL,
  reversal_of text,
  PRIMARY KEY (tenant_id, journal_id),
  UNIQUE (tenant_id, intent_id, leg_key),
  CHECK (debit_account <> credit_account),
  FOREIGN KEY (tenant_id, intent_id) REFERENCES bp47.intent,
  FOREIGN KEY (tenant_id, debit_account, asset_id) REFERENCES bp47.account,
  FOREIGN KEY (tenant_id, credit_account, asset_id) REFERENCES bp47.account,
  FOREIGN KEY (tenant_id, reversal_of) REFERENCES bp47.journal
);
CREATE FUNCTION bp47.no_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'posted journal is append-only';
END $$;
CREATE TRIGGER journal_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
ON bp47.journal FOR EACH STATEMENT EXECUTE FUNCTION bp47.no_mutation();
CREATE TABLE bp47.outbox (
  event_id text PRIMARY KEY,
  tenant_id text NOT NULL,
  intent_id text NOT NULL,
  stream_seq bigint NOT NULL CHECK (stream_seq > 0),
  payload bytea NOT NULL,
  delivered_at timestamptz,
  UNIQUE (tenant_id, intent_id, stream_seq),
  FOREIGN KEY (tenant_id, intent_id) REFERENCES bp47.intent
);
CREATE TABLE bp47.receipt (
  provider text NOT NULL,
  provider_account text NOT NULL,
  event_id text NOT NULL,
  tenant_id text NOT NULL,
  intent_id text NOT NULL,
  effect_id text NOT NULL,
  evidence_digest bytea NOT NULL CHECK (octet_length(evidence_digest) = 32),
  payload bytea NOT NULL,
  PRIMARY KEY (provider, provider_account, event_id),
  FOREIGN KEY (tenant_id, intent_id) REFERENCES bp47.intent
);
```

`numeric(78,0)` 入库可能发生标度舍入，因此必须在入口校验“原子整数字符串”，不能让 SQL 替你校验小数。
DDL 以 tenant_id 表示一个隔离法人账簿；多法人租户须新增 book_id 并进入全部主键与外键。
冲正引用原凭证，新凭证交换借贷并校验同资产、同金额；部分冲正必须累计检查不能超额。
此处 reversal_of 外键仅保证原凭证存在，不自动保证冲正金额、方向与次数正确。
迁移角色与业务写角色分离，撤销业务角色对 journal 的 UPDATE/DELETE/TRUNCATE 及触发器管理权。
触发器无法阻止数据库超级用户破坏数据；审计还需异地锚定、备份与独立权限控制。
receipt 的 event_id 去重和 effect_id 经济效果去重是两件事：不同事件可描述同一笔付款。
生产需增加 provider 作用域的效果唯一约束/映射，防止不同 intent 认领同一外部效果。
写入 receipt、归约状态、记分录、更新预占、插入 Outbox 必须在同一数据库事务中提交。
状态写入使用 `WHERE version = :old_version` 或行锁；冲突后重读重算，不盲目覆盖。
Outbox 至少一次投递，消费方 Inbox 去重；投递标志不是经济执行成功标志。

## 47.4 Go / Java 小核心：同键、同摘要、UNKNOWN 不释放

### 47.4.1 可运行范围与 Go 示例

下面两个程序各自保存成 `main.go` 与 `Main.java` 即可独立运行，不依赖 Protobuf 或数据库。
目标为 Go 1.24+、Java 21+，只使用标准库；小核心只证明归约规则，内存 map 不是持久幂等库。
输入单元为“同一已封存意图的证据”；金额验证、验签、权限、并发控制、外部效果去重均未实现。

```go
package main

import "fmt"

type Record struct { Hash, State string }
type Core struct { Rows map[string]Record }
func (c *Core) Apply(key, hash, evidence string) (string, error) {
    if key == "" || hash == "" { return "", fmt.Errorf("missing identity") }
    switch evidence {
    case "SEND", "TIMEOUT", "SETTLED", "REJECTED":
    default: return "", fmt.Errorf("untrusted evidence type")
    }
    r, ok := c.Rows[key]
    if ok && r.Hash != hash { return "", fmt.Errorf("same key, changed intent") }
    if !ok { r = Record{hash, "PREPARED"} }
    terminal := r.State == "SETTLED" || r.State == "REJECTED"
    incomingTerminal := evidence == "SETTLED" || evidence == "REJECTED"
    if terminal {
        if incomingTerminal && evidence != r.State {
            return "", fmt.Errorf("contradiction: quarantine")
        }
        return r.State, nil
    }
    switch evidence {
    case "SEND":
        if r.State == "PREPARED" { r.State = "SENT" }
    case "TIMEOUT": r.State = "UNKNOWN"
    case "SETTLED", "REJECTED": r.State = evidence
    }
    c.Rows[key] = r
    return r.State, nil
}
func main() {
    c := Core{Rows: make(map[string]Record)}
    for _, event := range []string{"SEND", "TIMEOUT", "SEND", "SETTLED", "TIMEOUT"} {
        state, err := c.Apply("T1:K1", "sealed-hash", event)
        if err != nil { panic(err) }
        fmt.Println(state)
    }
    if _, err := c.Apply("T1:K1", "changed-hash", "SEND"); err == nil {
        panic("changed intent accepted")
    }
    if _, err := c.Apply("T1:K1", "sealed-hash", "REJECTED"); err == nil {
        panic("contradictory evidence accepted")
    }
}
```

### 47.4.2 Java 等价实现与装配时序

```java
import java.util.HashMap;
import java.util.Map;

public class Main {
    record Row(String hash, String state) {}
    static final class Core {
        final Map<String, Row> rows = new HashMap<>();
        String apply(String key, String hash, String evidence) {
            if (key == null || key.isEmpty() || hash == null || hash.isEmpty())
                throw new IllegalArgumentException("missing identity");
            if (evidence == null || !java.util.Set.of(
                    "SEND", "TIMEOUT", "SETTLED", "REJECTED").contains(evidence))
                throw new IllegalArgumentException("untrusted evidence type");
            Row old = rows.get(key);
            if (old != null && !old.hash().equals(hash))
                throw new IllegalStateException("same key, changed intent");
            Row r = old == null ? new Row(hash, "PREPARED") : old;
            boolean terminal = java.util.Set.of("SETTLED", "REJECTED").contains(r.state());
            boolean incoming = java.util.Set.of("SETTLED", "REJECTED").contains(evidence);
            if (terminal) {
                if (incoming && !r.state().equals(evidence))
                    throw new IllegalStateException("contradiction: quarantine");
                return r.state();
            }
            String next = switch (evidence) {
                case "SEND" -> r.state().equals("PREPARED") ? "SENT" : r.state();
                case "TIMEOUT" -> "UNKNOWN";
                default -> evidence;
            };
            rows.put(key, new Row(hash, next));
            return next;
        }
    }
    public static void main(String[] args) {
        Core c = new Core();
        for (String e : new String[]{"SEND", "TIMEOUT", "SEND", "SETTLED", "TIMEOUT"})
            System.out.println(c.apply("T1:K1", "sealed-hash", e));
        boolean changed = false, conflict = false;
        try { c.apply("T1:K1", "changed-hash", "SEND"); }
        catch (IllegalStateException expected) { changed = true; }
        try { c.apply("T1:K1", "sealed-hash", "REJECTED"); }
        catch (IllegalStateException expected) { conflict = true; }
        if (!changed || !conflict) throw new AssertionError("missing rejection");
    }
}
```

两程序预期依次输出 `SENT、UNKNOWN、UNKNOWN、SETTLED、SETTLED`；异常路径必须被断言捕获。
本次仅在 Go 1.19.8、OpenJDK 17.0.21 实际编译/运行上述小核心，输出与断言符合预期。
这不是目标 Go 1.24+ / Java 21+ 验证，也不是并发、性能或真实金融链路验证。
本环境未安装 protoc 与 PostgreSQL 服务工具，协议生成和 DDL 执行尚未验证。
`SEND` 只是持久状态事件，不是在函数里发网络请求；UNKNOWN 收到 SEND 不会重新获得可执行资格。
生产适配器只有在持久化命令、租约 fencing 与外部幂等策略校验通过后才发送原请求。
`REJECTED` 仅指验源后确认原请求不可能再生效；“暂时查无记录”不能编译成该证据。
同一意图多次部分成交、拒付、重组与成交更正不能塞进这个五状态小核心，需要专用业务状态机。
晚到相反终态不是自动改账指令：隔离证据、人工复核，再通过更正/冲正工作流处理。

装配事务时序为：身份授权 → 封存摘要 → 抢占唯一键 → 校验同摘要 → 锁预占账户 → 写义务与 Outbox → 提交。
提交后投递可以重复；回执事务先验源/效果去重，再锁业务单、归约、记账、写事件，最后提交。
任何网络调用都不包在长期持有资金行锁的事务内，避免渠道慢请求拖垮账务锁队列。
Go 的 context 取消与 Java 的线程中断只代表本地等待结束，均不能证明远端没有执行。

## 47.5 L0—L4 演进：用信号换挡，而不是用技术名词分级

### 47.5.1 教学阈值表：全部需要本地压测重新标定

这里的 L0—L4 是系统成熟度，不是第 44 章工具权限等级，也不是法定监管分类。
表中为建议触发线，观察窗口默认连续 7 天；零容忍安全事件不等待窗口结束。

| 等级 | 最小形态 | 量化换挡信号（假设） | 下一步与回退门槛 |
|---|---|---|---|
| L0 教学验证 | 单体、模拟渠道、单账库、离线对账 | 真资金接入需求出现，或测试数据超过 10 万单 | 先补身份、审计、备份恢复与资金验收，不先拆服务 |
| L1 受控试点 | 模块化单体、独立执行凭据、事务 Outbox | 峰值达已测安全容量 60%，或资金锁等待 p99 超过 20 ms | 先定位热点与慢 SQL；新路径重复经济效果必须为 0 |
| L2 单域扩展 | 执行/行情独立部署、读模型、独立对账 | 对账积压超过 15 分钟，或单热点占写锁等待 30% | 分桶/批处理/隔离读写；抽样与全量核对同时通过 |
| L3 多域治理 | 账务单写、域间事件、可演练同城接管 | 单域故障造成两次以上跨域风险扩散/季度，或跨域发布失败率超过 5% | 缩小爆炸半径，契约回放无差异才切流 |
| L4 区域韧性 | 明确失效模型、异地恢复、持续演练 | 业务恢复目标低于现有演练 RTO，或单区域风险超出董事会容忍度 | 分区演练证明 fencing 与证据补齐；失败则停止区域切换 |

各等级都必须有精确记账、幂等、对账和最小权限；L1 不意味着可以容忍“一点重复扣款”。
衡量安全容量应使用包含故障、最热账户和回执风暴的测试，而不是只看均匀流量平均 TPS。
新服务化方案若使 UNKNOWN 老化、重复效果或恢复时间恶化，即使平均延迟下降也不能晋级。
按法人/账户/标的拆分有利于隔离；跨分片转账则引入显式在途科目与独立核对，不能假装本地原子性。
撮合单写者、账务热点串行化与链 nonce 单域顺序都存在硬边界，加机器不必然线性扩容。

### 47.5.2 预算减少 80%：降低业务面，不降低资金正确性

设月度可控工程/基础设施预算从 100 单位降到 20 单位；此表不包含无法压缩的法定资本等费用。

| 保留项 | 教学分配 | 不得删减的结果 |
|---|---|---|
| 精度与账本 | 6 | 整数/定点、不可变分录、预占、防透支与试算平衡 |
| 幂等与未知态 | 4 | 持久唯一键、同键同摘要、查单与回执收敛 |
| 对账与恢复 | 5 | 独立证据、差错工单、备份、可实际执行的恢复演练 |
| 安全与最小运营 | 5 | 权限隔离、密钥托管、审批、审计、告警与值守 |

先关 AI 自动编排、复杂推荐、多渠道智能路由、非必要实时 BI、长尾资产和多地区主动主动。
以模块化单体替代无必要微服务，降低吞吐上限、客户额度、支持币种和营业范围来匹配人力。
对账可以降低非关键报表刷新频率，但必须在风险期限内完成；高风险差额仍立即冻结相关路径。
人工接管只能降低处理速度，不能允许运营直接改余额或复制付款；人工也走同一幂等与审批入口。
若 20 单位不足以维持法定控制、密钥安全与恢复能力，就停止新业务或退出试点，而不是伪装降级成功。

## 47.6 手算容量与 RPO/RTO：先写失效模型，再谈“零丢失”

### 47.6.1 一组可复算容量假设

假设每天 100 万个业务单，峰均比 20，每单平均 6 条 journal 记录与 2 条 Outbox 事件。
平均单量 `1,000,000 / 86,400 ≈ 11.57 单/秒`，峰值约 `231.5 单/秒`。
峰值 journal 写入约 `231.5 × 6 = 1,389 条/秒`，事件约 `463 条/秒`。
若端到端平均在途时间为 0.2 秒，稳定期间平均在途约 `231.5 × 0.2 = 46.3 单`。
这是 Little 定律的平均量，不是连接池大小，也不能用 p99 延迟直接代入冒充平均并发。
若 1% 单据进入 UNKNOWN、平均停留 30 分钟，则稳定积压约 `231.5 × 1% × 1,800 = 4,167 单`。
每单最多每分钟查一次，约需 `4,167 / 60 ≈ 69.5 次/秒` 的渠道查询预算，退避与抖动另算。
若单条 journal 含索引后按 1 KB 预算，日增约 6 GB；三副本、30 天在线约 540 GB。
该估计未含 Outbox、回执、WAL、备份、膨胀和安全余量，采购容量不得只拿 540 GB 乘单价。
若 1% 的订单都争用一个账户、锁内耗时 5 ms，热点到达约 2.315 次/秒，理论服务率约 200 次/秒。
但若改为 100% 都争用该账户，231.5 已超过理论服务率；加连接会增加排队，不能治好串行瓶颈。

### 47.6.2 恢复目标的适用范围与代价

| 方案 | 教学恢复目标 | 代价/限制 |
|---|---|---|
| 单区同步副本 | 已确认账务 RPO=0，RTO≤10 分钟 | 只覆盖规定的单故障；区域整体损坏另议 |
| 跨区异步灾备 | 区域灾害 RPO≤5 秒，RTO≤60 分钟 | 5 秒是目标，不是保证；须监控确认点与复制水位 |
| 跨区同步法定人数提交 | 已确认账务 RPO=0，RTO≤15 分钟 | 写入承担跨区延迟；失去法定人数时停止确认新写入 |

RPO=0 必须限定“已被系统确认提交的数据”和可承受故障集合，不能指任意灾难下绝不损失。
若两地往返约 40 ms，同步确认延迟至少承受相关网络与刷盘路径，不能同时承诺任意地理零延迟。
异步复制下，按 231.5 单/秒与 5 秒暴露窗口估算，约 1,158 单可能需要查证，不等于已损失 1,158 单。
若每单平均 100.00 USD，对应待核验本金量约 115,800.00 USD；不是把它直接记作财务损失。
外部已执行、本地确认记录丢失时，先封锁重复发送，按外部证据恢复意图、幂等映射和分录。
缓存可以接受较大 RPO 并重建；账本、幂等键、渠道映射、审批与审计证据不能只备份其中一张表。
教学 RTO 拆解：发现 2 分钟 + 隔离旧主 3 分钟 + 恢复 8 分钟 + 证据核对/放行 12 分钟 = 25 分钟。
因此上述过程无法满足 15 分钟目标，必须缩短环节或修改承诺；“数据库已启动”不等于业务已恢复。
切换必须 fencing 旧主、凭据与任务执行器；DNS 改指向不能阻止旧进程继续付款或签名。

## 47.7 验收、压测与教学复盘：测业务不变量，不只测接口返回

### 47.7.1 最小验收矩阵

以下为待执行验收计划，不是已经通过的报告；每项记录版本、输入、预期、实际、证据和负责人。

| 场景 | 注入方式 | 必须观察到的结果 |
|---|---|---|
| 同键并发 | 100 个客户端同键同摘要 | 一个意图、一份义务，响应可重放 |
| 同键改参 | 保持 key，改金额/账户/资产/期限 | 拒绝，原记录与预占不变 |
| 提交结果丢失 | DB 提交后断开连接 | 重读唯一键，不能重建第二笔意图 |
| 网络未知态 | 渠道执行后丢响应 | UNKNOWN 保留预占，只查原单 |
| 回执乱序重放 | 终态后送旧回执、重复事件 | 不倒退、不重复分录；相反终态隔离 |
| 跨渠道双成功 | 两个 effect_id 均有效 | 识别重付，差错挂账，不多扣客户 |
| 券商撤单竞争 | 撤单确认前后乱序送成交 | 成交不被抹掉，义务不提前释放 |
| 链重组与替换 | 回滚区块、同 nonce 提价 | 追踪原意图，冻结/冲正不重复提币 |
| 日切与夏令时 | cutoff 两侧、重复/缺失小时 | 业务日固定、外部时点差有解释 |
| 模型越权 | 文档注入、任意工具、过期审批 | 只能形成拒绝/提案，零裸执行 |
| 灾备脑裂 | 隔离主网但保留外部渠道连通 | 旧主失去执行权，新主先核验再放行 |

逐资产计算借方合计与贷方合计；逐账户以期初加分录重建期末，不能只检查全局总和为零。
全局平衡仍可能把钱记给错误客户，必须额外验证账户归属、外部效果与业务义务的一致性。
对账差额分为在途、费用、币种/标度、重复效果、遗漏入账与真实损失，设金额与老化升级阈值。
可自动补投已知 Outbox；不能让“自动修复”凭差额倒推分录，未知原因先隔离再审批。

### 47.7.2 压测分层与上线门禁

先用 golden vectors 验 Go/Java 金额、摘要和状态序列一致，再做数据库事务与唯一约束测试。
对小核心分别运行 `go run main.go` 与 `javac Main.java && java Main`，核对预期输出和拒绝路径。
集成测试使用一次性数据库执行 DDL，验证非法资产、跨租户外键、重复腿和分录变更均失败。
负载从 0.5 倍、1 倍到 2 倍教学峰值逐级提升，每级假设持续 30 分钟，另做 24 小时稳定性测试。
分别测试均匀账户、单热点、90% 同键重放、回执风暴、渠道限流与慢消费者，不能混成一个 TPS。
报告 p50/p95/p99、错误率、锁等待、WAL/复制水位、UNKNOWN 老化、对账积压与重复经济效果数。
示例放行目标可设资金路径 p99≤200 ms、重复经济效果为 0、无解释差额为 0；这是待验证门槛。
在低负载也达不到目标时先诊断，不通过调大超时把 UNKNOWN 隐藏掉；外部最终性延迟单独报告。
灰度以账户/法人分组限制额度，先 paper、再人工低额、再受控放量；每级都有停止新风险的开关。
新旧逻辑影子比对只允许一条路径产生外部副作用；禁止“双写试试看”到两个真实渠道。
回退恢复旧读取逻辑，不删除已产生的新事实；停止新单后仍保留回执、查单、对账和必要撤权通道。

### 47.7.3 回看事故：真正的节流方案

删除 AI 自动切路由，恢复 UNKNOWN 查单与冻结，收益比“模型再聪明一点”更直接。
把迟到 A 回执保留为第二外部效果，补建差错应收，核对两笔银行出账，走受控追回流程。
复盘追问的是“哪一条不变量没有强制执行”，不是“谁没及时看群消息”。
事故关闭条件包括本金与费用归属清楚、客户权益正确、旧请求不可重付、回归测试与审批证据齐全。

## 47.8 术语、十分钟面试答题卡与自检

### 47.8.1 用一句话纠正常见术语误用

| 术语 | 正确含义 | 常见误用 |
|---|---|---|
| 唯一事实源 | 特定领域的权威记录与归属 | 一张 Ledger 表统治外部银行、市场与区块链 |
| 幂等 | 同经济命令重复到达不重复产生效果 | HTTP 返回相同就算幂等 |
| UNKNOWN | 已有可能生效但证据未收敛的义务 | 可以释放资金并换键重试的失败 |
| Exactly-once | 在特定边界用协议与约束实现一次业务效果 | 消息中间件替任意外部机构担保 |
| 对账 | 用独立证据核验权益与实际效果 | 两张由同一错误程序生成的表相等 |
| RPO / RTO | 可丢数据窗口 / 可接受恢复时长，均需限定失效模型 | 有副本就必然零丢失、秒恢复 |
| 双栈共享 | 共享语义、版本、测试向量与运维责任 | Go/Java 同时拥有余额表写权限 |

### 47.8.2 十分钟答题：每一步都落到可检验约束

第 0—2 分钟：明确法人、客户资产、交易规模、成功定义与外部机构职责，画资金和证据两条流。
第 2—4 分钟：讲整数精度、预占、分录与冲正，使用 [20 精度](20-monetary-precision-rounding-and-allocation.md) 和 [22 账本并发](22-double-entry-ledger-schema-and-concurrency.md) 说明一分钱如何守住。
第 4—6 分钟：用 [24 幂等](24-end-to-end-financial-idempotency.md)、[25 Outbox](25-state-machines-tcc-saga-and-outbox.md) 与 [28 UNKNOWN](28-payment-gateway-routing-and-unknown-state.md) 解释断网后为什么不换键。
第 6—7 分钟：按场景选择 [34 券商总分账](34-brokerage-omnibus-fx-margin-and-actions.md) 或 [36 重组与 nonce](36-chain-indexer-reorg-defense-and-nonce.md)，解释外部证据怎样收敛。
第 7—8 分钟：用 [27 多层对账](27-multi-tier-reconciliation-and-auto-healing.md) 定义差额闭环，用 [38 安全容灾](38-security-aml-hash-chain-and-multi-region-dr.md) 限定恢复承诺。
第 8—9 分钟：按 [39 三平面](39-fin-agent-architecture-and-three-plane-isolation.md) 与 [44 审批绑定](44-financial-mcp-hitl-approval-and-idempotency.md) 放入 AI，明确它没有记账或签名权。
第 9—10 分钟：手算容量、热点与灾备暴露量，说明预算砍 80% 后删什么、保什么，并给出失败门禁。

### 47.8.3 自检推演与参考检查点

1. 支付 UNKNOWN 已两小时，对方查无此单，能否改走 B？不能只凭一次查无记录；证明 A 不可能生效或继续挂起。
2. 券商内部总现金与上游一致，能否证明客户账正确？不能；错记客户之间的转移不改变总额，仍须逐户逐事件核验。
3. 交易所撮合重放一致，但 Ledger 少一段成交，先开放交易还是先补齐？先锁定清算水位和潜在义务，修复核对后分级恢复。
4. 峰值从 231.5 增至 463 单/秒、UNKNOWN 比率不变，30 分钟积压多少？约 8,334 单，查询预算也约翻倍。
5. 只有 20 单位预算，能否保留高频 AI、删掉对账？不能；先降业务范围与自动化，资金控制仍达不到要求就暂停新业务。

**本章收口**：架构的成熟不是框图越来越多，而是任何一次超时、重放、故障和模型越权，都能证明客户权益没有被偷偷改写。
