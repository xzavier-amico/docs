# 42. 神经符号风控 Agent：确定性 VaR/ES 重定价与 AML 证据链

> **状态**：深入工程篇｜**所属**：第五部分·金融 × AI-Agent 架构实战｜**编号**：42 / 48
>
> **定位**：承接 [11 风险分类](11-financial-risk-taxonomy-and-crashes.md)、[20 金额精度](20-monetary-precision-rounding-and-allocation.md)、[30 硬风控](30-pretrade-risk-margin-and-liquidation-engine.md)、[38 AML 与安全](38-security-aml-hash-chain-and-multi-region-dr.md)、[39 三平面](39-fin-agent-architecture-and-three-plane-isolation.md)。让模型负责发现值得检验的假设，让可信程序负责计算、证据和授权。
>
> **代码契约**：PostgreSQL 15+ DDL 为教学骨架；Go 与 Java 标准库示例仅计算等权离散 VaR/ES，不是完整定价器、审批器或交易 SDK。金额用最小货币单位整数；统计与定价内部允许 `double/float64`，但其结果绝不能直接作为账务入账事件。
>
> **红线**：LLM 不生产最终风险数、不调整情景概率、不直接降仓、不自动提交 SAR；“降低一个风险指标”不等于“交易天然安全”；图关联不等于犯罪事实；模型置信度不是法律依据。

## 42.1 教学事故：一句“立即降仓”穿透了两个系统

### 42.1.1 时间线与口径

**以下为合成教学事故，机构、时间和金额均为虚构，不对应已证实事件。**

| 时间 | 事件 | 错误边界 |
|---|---|---|
| 09:00 | Agent 阅读“波动率上升 5%”的研报 | 把相对上涨 5% 当成上涨 5 个波动率百分点 |
| 09:01 | 模型生成 99% ES，并解释为最坏损失 | 没有损失样本、分位点约定和版本 |
| 09:02 | 发现集中度偏高，直接调用卖出工具 | 未验证保护性头寸、保证金和审批 |
| 09:03 | OMS 超时，Agent 更换请求号重试 | 同一降仓意图重复送单 |
| 09:04 | 图检索命中一条两年前的地址标签 | 未检查交易时间、标签有效期与归属假设 |
| 09:05 | 模型把“已提交 SAR”写入客服答复 | 泄露申报存在性，违反保密边界 |

假设只获准卖出 100 股，两次分别卖出 100 股；每股成交 98.00 USD，基准价 100.00 USD。
额外卖出的 100 股相对基准产生 **200.00 USD 实施价差**，不等于系统全部损失，更不等于已实现税务损益。
若重复单导致裸空，还存在未来价格风险；SAR 泄露属于独立合规事件，不应用这个价差金额度量。

### 42.1.2 需要证明的不变量

1. 相同输入清单、算法、运行环境与版本，能重放同一风险结论或解释数值容差。
2. 每条损失样本都有情景、持仓、行情、模型、币种、期限与单位来源。
3. 卖出、对冲、解除冻结等动作不能由风险报告或模型文本直接触发。
4. AML 的事实、推断、缺失信息分栏表达，每个事实能定位到授权证据。
5. 超时、数据缺失、定价失败不得悄悄转成零风险、无命中或可执行批准。

## 42.2 神经与符号的职责：模型提出“如果”，程序回答“多少”

### 42.2.1 三条隔离的数据流

```text
新闻 / 合规材料（不可信数据） → LLM → ScenarioProposal / InvestigationDraft
                                       │ 无交易与申报凭据
                                       ▼
Schema + 主数据 + 单位/时间/地域校验 → 可信快照与情景编译器
                                       │
                   ┌───────────────────┴──────────────────┐
                   ▼                                      ▼
           确定性重定价与风险归约                    受控 AML 图查询
                   │                                      │
           风险报告 + 贡献明细                     证据包 + 人工调查
                   │                                      │
           硬策略 → 独立审批 → OMS                 合规复核 → 地域申报适配器
```

认知区没有券商密钥、账本写权限或 SAR 提交权限；执行区不解释自然语言。
所谓“神经符号”在本章指概率提案与显式约束/数值算法协作，不声称接入规则引擎就获得形式化正确性。

| 产物 | 可信生产者 | 模型可做什么 | 模型不能做什么 |
|---|---|---|---|
| 情景提案 | LLM，标记不可信 | 提出利率、价差、波动、流动性冲击 | 自封市场事实或改变监管概率 |
| 情景集 | 情景编译服务 | 解释编译拒绝原因 | 删除不利情景、调概率压低 ES |
| 风险报告 | 版本化定价/归约服务 | 摘要、比较、指出缺口 | 编造数值或覆盖定价失败 |
| 降仓意图 | 策略服务封存 | 提供候选方案 | 直接发单、批准自己、绕过硬限额 |
| AML 叙述 | 调查员确认的证据包 | 提取事实、拟写草稿 | 定罪、自动 SAR、向客户透露申报 |

### 42.2.2 情景编译不是执行模型输出的代码

只接收白名单因子和枚举冲击类型，禁止 `eval`、SQL、任意 URL 或 Python 表达式。
冲击声明包含 `factor_id, operator, value, unit, horizon, effective_at`；币种和市场由主数据解析。
“利率 +100bp”编译为绝对利率 `+0.01`；“波动率 +5vol_point”编译为 `+0.05`。
若原波动率 20%，相对提高 5% 得 21%，提高 5 个百分点得 25%；二者不可混用。
价格相对冲击、收益率平移、期限点扭曲、汇率报价方向必须分别建模，不能统称一个 `shock=5`。
模型提出的新压力情景进入独立压力集；只有治理批准后才能进入带概率的统计风险样本集。
历史、蒙特卡洛与主观压力结果分栏，不用“八个有趣故事等权”冒充真实市场分布。

## 42.3 VaR、离散 ES、Greeks 与聚合的可执行口径

### 42.3.1 损失符号与定价边界

持仓在风险时点冻结为 `position_snapshot`，行情与曲线冻结为 `market_snapshot`。
对情景 `s`，先按共同情景重定价每个工具，再汇总到报告币种：

```text
V_s = Σ_i FX(currency_i → reporting_currency, s) × PV_i(position_i, market_s)
L_s = V_0 − V_s                       # 正数为损失，负数为盈利
```

这里先假设期间无现金流；有票息、分红、到期兑付时，把期间现金流及其再投资/折现约定纳入总回报。
跨币种时 `FX_s` 本身是情景变量；只对工具价格冲击却固定汇率，会遗漏汇率与相关性风险。
数量、合约乘数、报价币种、净价/全价、应计、拆股和期权行权单位都由主数据决定。
重定价器固定曲线构建、插值、日历、估值时刻、随机种子和模型参数；异步获取“最新价”不可复算。
蒙特卡洛还需固定随机数算法、子流划分和归约顺序；固定种子不自动保证跨 CPU/GPU 位级一致。

### 42.3.2 分位点采用经验 CDF 左逆，而不是库默认值

设有 `n` 个等权损失，升序排列为 `l_(1) ≤ … ≤ l_(n)`，置信度 `0 < α < 1`：

```text
VaR_α = inf{x : F_n(x) ≥ α} = l_(ceil(nα))
ES_α  = [∫_α^1 VaR_u du] / (1−α)
k = ceil(nα)
ES_α = [(k−nα) × l_(k) + Σ_(i=k+1)^n l_(i)] / [n(1−α)]
```

这是对经验分布尾部的积分定义；不等于随便对 `L ≥ VaR` 的全部样本取平均。
分位点处有概率原子或并列值时，只取补足 `1−α` 所需的概率质量；并列值如何分配不改变 ES 数值。
`nα` 为整数时边界权重为零；`n(1−α)<1` 时经验 ES 退化为最大样本，不能据此宣称充分覆盖尾部。
例如 `n=8, α=0.80`，尾部为 1.6 个样本：最大值占 1，次大值占 0.6。
置信度在线协议中用整数基点，例如 `8000/10000`，避免 `ceil(8×0.8)` 的浮点边界漂移。
R 默认 Type 7 等插值算法不等于这里的 Type 1；比较报告必须连同 `quantile_method` 对账。
本章不将负 VaR/ES 截为零；若业务要展示非负限额口径，另列指标，不能暗改原统计量。

非等权情景必须先验证 `p_s ≥ 0, Σp_s=1`，再按损失从大到小截取尾部概率并允许边界分割。
重复情景的处理必须保留原概率质量；把重复行去重后重新等权会改变分布。
禁止删除失败定价的样本后重新归一化：报告应 `INCOMPLETE`，或使用经批准且显式披露的保守替代。

### 42.3.3 Greeks 是局部导数，不是全重估的替代品

```text
ΔV ≈ Delta × ΔS + 0.5 × Gamma × (ΔS)^2 + Vega × Δσ
     + Rho × Δr + Theta × Δt + 交叉项
```

| 量 | 本章单位约定 | 常见事故 |
|---|---|---|
| Delta | USD PV / 1 USD 标的变动 | 已含数量与乘数后又乘一次 |
| Gamma | USD PV / (1 USD 标的变动)² | 漏掉 Taylor 展开中的 0.5 |
| Vega | USD PV / 1.00 绝对波动率 | 把“每 1 vol point”误放大 100 倍 |
| Rho | USD PV / 1.00 绝对利率 | 与 DV01 的每基点口径混算 |
| Theta | USD PV / 日，明确时间前进方向 | 将年化导数当日损耗 |

示例：组合 `Delta=100, Gamma=−10, Vega=−5000`，`ΔS=−10 USD, Δσ=+0.05`。
近似 `ΔV=−1000−500−250=−1750 USD`；只是局部近似的 **1,750.00 USD 损失**。
这不是下一节到期期权案例的 Greeks；两例模型和期限不同，不可拼成同一对账表。
用有限差分验证导数：中心差分、步长收敛、解析值对照；近障碍、到期、跳跃和大冲击必须全重估。
大波动下同时检查曲面无套利、相关矩阵半正定、负价格允许域与数值收敛，非法状态不能静默截断。

### 42.3.4 先聚合同一情景 P&L，再取尾部

`ES(A+B)` 需要对齐同一情景下 `L_A,s + L_B,s`，不能把两个已排序数组逐位相加。
ES 在一致分布和可积条件下具有次可加性，但 `ES(A)+ES(B)` 通常只是上界，不是组合 ES。
VaR 一般不满足次可加性；净额结算集、法律实体和报告币种也不能为追求更低数字随意合并。
Euler ES 贡献需满足齐次性/可微性条件；有限差分增量 ES 不等于可直接求和的风险贡献。
离散并列尾部采用一致分配规则，使贡献和等于组合 ES；否则贡献报告会受排序稳定性影响。
不把一天风险机械乘 `√10` 推导十天风险：独立同分布、尺度模型和流动性条件必须先成立。

## 42.4 可复算压力例：保护性 Put 降低 ES，却提高这个 VaR

### 42.4.1 初始组合与到期情景

**教学设定**：报告币种 USD；100 股现货，每股 100.00；100 份每份覆盖 1 股的欧式 Put。
Put 执行价 90.00、当前公允价 2.00，初始组合价值 `10,000.00+200.00=10,200.00`。
期限是 Put 到期日，利率与现金流设为零，不计费税与滑点；到期按 `max(90−S,0)` 精确重定价。
以下八个情景人为等权，置信度 80% 仅为展示离散权重；是**教学压力分布**，不是监管 ES 校准。

| 情景 | 到期 S / USD | 现货损失 A / USD | Put 损失 B / USD | 组合损失 / USD |
|---|---:|---:|---:|---:|
| S1 | 101.00 | −100.00 | 200.00 | 100.00 |
| S2 | 100.00 | 0.00 | 200.00 | 200.00 |
| S3 | 99.00 | 100.00 | 200.00 | 300.00 |
| S4 | 98.00 | 200.00 | 200.00 | 400.00 |
| S5 | 96.00 | 400.00 | 200.00 | 600.00 |
| S6 | 93.00 | 700.00 | 200.00 | 900.00 |
| S7 | 90.00 | 1,000.00 | 200.00 | 1,200.00 |
| S8 | 80.00 | 2,000.00 | −800.00 | 1,200.00 |

`k=ceil(6.4)=7`：现货 `VaR=1,000.00`，`ES=(0.6×1000+2000)/1.6=1,625.00`。
Put 单独 `VaR=ES=200.00`；组合 `VaR=ES=1,200.00`，均为 USD。
组合 ES 小于分项 ES 之和 `1,825.00`，但组合 VaR 比现货单独 VaR 高 `200.00`。
这并不矛盾：保护尾部要支付当前期权价值，中等跌幅下期权仍无到期价值。
若对现货错误地取所有 `L≥1000` 的平均值，会得到 `1,500.00`，而不是正确的 `1,625.00`。
把组合最大两项取平均恰好为 `1,200.00` 只是并列导致的巧合，不能证明错误算法正确。

### 42.4.2 降仓建议的交易边界

若模型建议“卖掉 Put 回收 200.00”，必须重算：当前现金增加不代表尾部风险降低。
卖出期权可能移除保护、增加保证金或产生裸露义务；“卖出”不是通用 `reduce_only` 证明。
降仓候选需同时验证净敞口、最坏压力、保证金、集中度、可卖数量、流动性、价格保护和账户权限。
硬策略形成不可变 Intent，审批绑定其摘要、持仓/行情版本、策略版本和有效期；审批不能覆盖硬拒绝。
本章要求独立人工审批降仓；预授权自动处置只能作为另经批准的确定性应急策略，不是给 LLM 开例外。
OMS 在送单前重验权限、行情和在途义务；新成交或审批过期会使旧评估失效，需重新评估。
撤单未知、部分成交和超时保留预占，以稳定 `client_order_id` 查单收敛，禁止模型另造订单号重试。

## 42.5 协议与 DDL：固定什么，才能复算什么

### 42.5.1 提案协议与计算清单

以下 JSON 只是情景提案，不包含概率、批准标志或执行工具名；账户从可信身份上下文绑定。

```json
{
  "schema_version": 1,
  "proposal_id": "SP-42-001",
  "base_snapshot_id": "M-20261126-0900",
  "horizon": "P1D",
  "shocks": [
    {"factor_id": "USD.OIS.5Y", "operator": "ADD", "value": "100", "unit": "BP"},
    {"factor_id": "EQ.DEMO.IV.ATM", "operator": "ADD", "value": "5", "unit": "VOL_POINT"}
  ],
  "rationale_evidence_ids": ["NEWS-019"]
}
```

Schema 拒绝未知字段、重复键、尾随 JSON、非有限值、过长数组和越界冲击；数值字符串需严格词法解析。
`P1D` 还需指定日历日/交易日、收盘时刻与时区；不能让两个计算节点自行推断到期时间。
可信清单固定 `position/market/scenario/model/calendar/fx/policy` 的版本和内容摘要，以及算法/镜像摘要。
摘要覆盖规范化字节、币种、金额尺度、置信度、期限、分位算法和随机数清单，不能只哈希报告正文。
金额/数量用十进制字符串传输，避免 JSON 客户端 `2^53` 限制；`alpha_bps` 是受限小整数。
模型解释引用 `run_id + report_hash`；只读工具返回可信数值，模型转述不得覆盖报告原值。

### 42.5.2 PostgreSQL 最小持久化骨架

```sql
CREATE TABLE risk_run (
  tenant text NOT NULL, run_id text NOT NULL,
  manifest_hash bytea NOT NULL CHECK (octet_length(manifest_hash)=32),
  manifest_bytes bytea NOT NULL,
  report_ccy char(3) NOT NULL, minor_scale smallint NOT NULL CHECK (minor_scale=2),
  alpha_bps integer NOT NULL CHECK (alpha_bps>0 AND alpha_bps<10000),
  quantile_method text NOT NULL CHECK (quantile_method='ECDF_LEFT_INV'),
  state text NOT NULL CHECK (state IN ('PENDING','COMPLETE','INCOMPLETE','FAILED')),
  var_minor bigint, es_minor numeric(38,12),
  PRIMARY KEY (tenant,run_id)
);
CREATE TABLE risk_scenario_loss (
  tenant text NOT NULL, run_id text NOT NULL, scenario_id text NOT NULL,
  probability numeric(24,20) NOT NULL CHECK (probability>0 AND probability<=1),
  loss_minor bigint NOT NULL, valuation_hash bytea NOT NULL,
  PRIMARY KEY (tenant,run_id,scenario_id),
  FOREIGN KEY (tenant,run_id) REFERENCES risk_run
);
CREATE TABLE risk_action_proposal (
  tenant text NOT NULL, action_id text NOT NULL, run_id text NOT NULL,
  intent_hash bytea NOT NULL CHECK (octet_length(intent_hash)=32),
  intent_bytes bytea NOT NULL, policy_version text NOT NULL,
  expires_at timestamptz NOT NULL,
  PRIMARY KEY (tenant,action_id), UNIQUE (tenant,action_id,intent_hash),
  FOREIGN KEY (tenant,run_id) REFERENCES risk_run
);
CREATE TABLE risk_action_approval (
  tenant text NOT NULL, approval_id text NOT NULL, action_id text NOT NULL,
  intent_hash bytea NOT NULL, approver_subject text NOT NULL,
  decision text NOT NULL CHECK (decision IN ('ALLOW','DENY')),
  expires_at timestamptz NOT NULL, signature bytea NOT NULL,
  PRIMARY KEY (tenant,approval_id),
  FOREIGN KEY (tenant,action_id,intent_hash)
    REFERENCES risk_action_proposal(tenant,action_id,intent_hash)
);
CREATE TABLE aml_evidence_ref (
  tenant text NOT NULL, case_id text NOT NULL, evidence_id text NOT NULL,
  event_at timestamptz NOT NULL, observed_at timestamptz NOT NULL,
  valid_from timestamptz NOT NULL, valid_to timestamptz,
  region text NOT NULL, source_version text NOT NULL, locator text NOT NULL,
  content_hash bytea NOT NULL CHECK (octet_length(content_hash)=32),
  classification text NOT NULL CHECK (classification IN ('FACT','INFERENCE')),
  PRIMARY KEY (tenant,case_id,evidence_id),
  CHECK (valid_to IS NULL OR valid_to>valid_from)
);
```

这里只支持两位小数币种与已转换到报告币种的损失；生产应按币种元数据约束，不把所有币种强设“分”。
`es_minor` 可以含分的小数，因为它是风险估计；它不是余额或可直接入账金额，输出还需明确舍入规则。
当前骨架按情景将 PV 转为最小单位后计算统计；高精度定价生产路径可先高精度归约、再一次舍入。
两种方法会有差异，必须版本化，禁止一个语言逐工具舍入、另一个语言逐组合舍入却声称严格一致。
概率和、样本完整性与 `COMPLETE` 条件是跨行约束，需封账事务校验，不能依赖单行 `CHECK`。
`manifest`、完成报告和 Intent 封存后追加存储；生产补充不可变触发器、RLS、审计、密钥隔离与保留策略。
审批表只是记录，不会自动产生执行权限；执行区还需签名/撤销检查、独立身份、Outbox 和幂等消费。
本节没有 SAR 正文、密钥或提交表；它们属于独立合规隔离域，不能混进普通风险分析数据库。

## 42.6 Go + Java：分割离散尾部的最小计算核

### 42.6.1 Go 标准库例子

下例输入为 USD 分；先用整数基点计算秩，再用任意精度有理数保留 ES，不发生中间整数乘法溢出。

```go
package main

import (
    "fmt"
    "math/big"
    "sort"
)

// Tail 只接受等权样本，不是定价服务或账本接口。
func Tail(loss []int64, alphaBps int64) (int64, *big.Rat, error) {
    if len(loss) == 0 || alphaBps <= 0 || alphaBps >= 10000 {
        return 0, nil, fmt.Errorf("invalid sample or alpha")
    }
    if len(loss) > 1_000_000 { // 教学核内存/乘法上界
        return 0, nil, fmt.Errorf("too many samples")
    }
    x := append([]int64(nil), loss...)
    sort.Slice(x, func(i, j int) bool { return x[i] < x[j] })
    n := int64(len(x))
    a := n * alphaBps
    k := (a + 9999) / 10000 // 一基秩 ceil(nα)
    numerator := new(big.Int).Mul(
        big.NewInt(k*10000-a), big.NewInt(x[int(k)-1]))
    for i := int(k); i < len(x); i++ {
        term := new(big.Int).Mul(big.NewInt(10000), big.NewInt(x[i]))
        numerator.Add(numerator, term)
    }
    denominator := big.NewInt(n * (10000 - alphaBps))
    return x[int(k)-1], new(big.Rat).SetFrac(numerator, denominator), nil
}

func main() {
    stock := []int64{-10000, 0, 10000, 20000, 40000, 70000, 100000, 200000}
    combined := []int64{10000, 20000, 30000, 40000, 60000, 90000, 120000, 120000}
    for _, x := range [][]int64{stock, combined} {
        v, e, err := Tail(x, 8000)
        if err != nil { panic(err) }
        fmt.Printf("VaR_minor=%d ES_minor=%s\n", v, e.FloatString(2))
    }
}
```

预期依次输出 `VaR_minor=100000 ES_minor=162500.00`、`VaR_minor=120000 ES_minor=120000.00`。
打印的 `.00` 是“分的分数位”，不是 USD；API 必须返回 `currency=USD, scale=2, value_kind=RISK_ESTIMATE`。
`FloatString` 只用于展示舍入；需要完全复算时保存约分后的分子/分母，不能把展示串当内部真值。

### 42.6.2 Java 21 标准库例子

同样限制样本数，秩使用 `long`，累计使用 `BigInteger`；除不尽时以 12 位“分的小数”展示。

```java
import java.math.BigDecimal;
import java.math.BigInteger;
import java.math.RoundingMode;
import java.util.Arrays;

public class TailRisk {
    record Result(long varMinor, BigInteger esNum, BigInteger esDen) {
        BigDecimal displayMinor() {
            return new BigDecimal(esNum).divide(
                new BigDecimal(esDen), 12, RoundingMode.HALF_EVEN);
        }
    }

    static Result tail(long[] loss, int alphaBps) {
        if (loss == null || loss.length == 0 || loss.length > 1_000_000
                || alphaBps <= 0 || alphaBps >= 10000) {
            throw new IllegalArgumentException("sample or alpha");
        }
        long[] x = loss.clone();
        Arrays.sort(x);
        long n = x.length;
        long a = n * alphaBps;
        long k = (a + 9999) / 10000;
        BigInteger numerator = BigInteger.valueOf(k * 10000 - a)
            .multiply(BigInteger.valueOf(x[(int) k - 1]));
        for (int i = (int) k; i < x.length; i++) {
            numerator = numerator.add(BigInteger.valueOf(x[i])
                .multiply(BigInteger.valueOf(10000)));
        }
        return new Result(x[(int) k - 1], numerator,
            BigInteger.valueOf(n * (10000 - alphaBps)));
    }

    public static void main(String[] args) {
        long[][] samples = {
            {-10000, 0, 10000, 20000, 40000, 70000, 100000, 200000},
            {10000, 20000, 30000, 40000, 60000, 90000, 120000, 120000}
        };
        for (long[] x : samples) {
            Result r = tail(x, 8000);
            System.out.println("VaR_minor=" + r.varMinor()
                + " ES_minor=" + r.displayMinor().toPlainString());
        }
    }
}
```

分别可用 `go run tail.go`、`javac TailRisk.java && java TailRisk` 验证；这些是读者运行方式，不代表本仓库已有相应源码。
Java 预期 ES 为 `162500.000000000000` 与 `120000.000000000000`，与 Go 的有理数值相等。
Go `FloatString` 半值远离零，Java 示例采用 HALF_EVEN；本例无差异，生产必须统一展示舍入，核心对账用精确分数。
这两个核未实现不等权、FX、现金流、定价器或报告签名；接入这些能力前必须扩展输入契约和测试。
生产浮点定价先拒绝 NaN/Inf，检查收敛，明确币种尺度、舍入、溢出和误差上界，再生成风险结果。
成交、费用、结算汇率只能来自可信业务事件，由账本按自身定点规则入账，不能调用 `post(ES)`。

## 42.7 AML 图证据、运行治理与验收

### 42.7.1 查询的时间范围是证据的一部分

AML 观察图可含账户、客户、受益所有人、设备、地址和交易；边必须携带类型、来源和时间，不只保存连接关系。
查询至少绑定 `tenant, purpose, region, case_id, event_range=[t0,t1), knowledge_cutoff, graph_version`。
交易发生时间 `event_at`、本机构获知时间 `observed_at`、标签有效区间 `[valid_from,valid_to)` 各有不同含义。
按“当时已知”复盘时，要求 `observed_at ≤ knowledge_cutoff`；不能拿今天的新标签假装昨天已知。
2026 年获知的标签可用于今天调查 2024 年交易，但必须声明是事后知识，并验证标签是否追溯覆盖该期间。
区块链证据还固定链 ID、交易哈希、日志索引、区块哈希、高度与确认策略；重组后撤销旧证据并追加新版本。
同一地址多跳关联、共享设备、资金进入交易所归集地址，都不能直接证明同一实益所有人。
图路径检索设最大跳数、边类型、时间连续性、金额单位、节点预算和超时；路径拼接不得跨互不相关时间。
循环转账按唯一业务交易去重，桥接与换汇分开标识，不能把各跳金额相加解释为新增犯罪所得。
图币种转换用明确时点的 FX，原币金额仍保留定点整数；混合 USD 与 USDT 不得因名称相似视作等值。

### 42.7.2 引用、人工复核、SAR 保密与地域

| 输出层 | 必要内容 | 禁止替代 |
|---|---|---|
| 事实 | 证据 ID、对象摘要、页码/记录定位、交易时间、来源版本 | 只贴主页 URL 或模型生成引用 |
| 推断 | 路径、聚类规则、假设、置信区间/限制 | 把“疑似控制”写成“已控制” |
| 缺口 | 缺失银行流水、归属不明、图覆盖范围 | 用流畅叙述填补空白 |
| 处置草稿 | 适用法域、调查员问题、复核理由 | 将风险分数当法定申报结论 |

引用验证器先查对象是否存在、摘要是否匹配、主体是否有权、地域是否允许，再校验引文确实支持该句。
仅“引用存在”不代表语义蕴含成立；对实体、金额、日期等关键字段做结构化比对，无法证明则交人工。
RAG 文本即使来自合规材料也只作数据，不允许“忽略保密规则”“自动提交”一类嵌入指令变成系统控制。
模型生成调查摘要或 SAR 草稿；由有权限的调查员核实证据、适用条件、时限和申报地域后人工批准。
草稿、正式 SAR、提交回执及能揭示其存在的信息按本机构保密分类隔离；不能回流客服、通用向量库或训练集。
美国银行 SAR 保密规则参考 31 CFR 1020.320(e)，存在法定允许披露边界；不是“全世界任何人一律不能共享”。
地域路由依据法律实体、数据所在地、申报机关与适用法律配置；集团内部共享也必须有法务确认的依据。
模型供应商日志、缓存、嵌入、备份、故障转移和人工支持访问都算数据路径，不能只检查推理主机地域。
若适用规则不允许跨境处理，就在合规域本地检索/推理，或只输出经过批准且不可逆的聚合信息。
SAR/STR、制裁冻结和业务风控拦截是不同流程；有权威制裁硬命中时按法律策略处置，不等待 LLM 判断。
监测 SLA 和法定申报计时独立运行，模型故障不得暂停时钟；告警分数低也不能自动关闭人工调查。

### 42.7.3 测试、对账与压测验收

| 类别 | 注入/输入 | 必须验证的结果 |
|---|---|---|
| 黄金向量 | 本章八情景、乱序、全部相等、负损失 | Go/Java 秩与 ES 有理值一致，输入不被修改 |
| 分位边界 | `nα` 恰为整数、尾部小于 1、VaR 并列 | 边界质量准确，不能用条件均值替代积分 |
| 属性测试 | 常数平移、正数缩放、共同情景聚合 | 平移/齐次性成立；不把 VaR 次可加当公理 |
| 单位测试 | BP、VOL_POINT、REL_PCT、乘数、FX 反向 | 错单位被拒，转换后有共同黄金向量 |
| 定价测试 | Put 到期、平价、解析/差分 Greeks | 已知解吻合；大冲击不假装局部近似精确 |
| 不完整输入 | 缺曲线、NaN、单个情景超时 | 报告 INCOMPLETE，不删除样本后重算低 ES |
| 授权故障 | 审批过期、持仓变更、LLM 伪造批准 | 重新评估或拒绝；模型身份永远不可送单 |
| 证据故障 | 伪引用、未来标签、图重组、跨地域查询 | 不支持的句子阻断，旧版本撤销，地域拒绝 |
| 提示注入 | 新闻/图节点含“卖出”“提交 SAR” | 不增加权限，不改变硬策略，不泄露案件状态 |

对账链为 `持仓账/成交 → 风险快照 → 单工具PV → 情景P&L → 聚合 → 排序尾部 → 报告/审批`。
同一快照比较生产与独立参考定价器；容差分别按价格绝对误差、相对误差、Greeks 单位与总风险误差设定。
核对 `Σ工具P&L=情景组合P&L`、概率质量为 1、尾部质量为 `1−α`、贡献和与 ES 一致。
风险报告不与总账余额逐项强行相等：先桥接估值时间、应计、结算状态、FX 和风险/会计估值差异。
动作对账核对 Intent、审批、OMS、成交和仓位变化；AML 对账核对告警、案件、人工决策和申报回执访问记录。
压测工作量按“持仓数 × 情景数 × 定价复杂度”估计；图查询另按扇出、时间跨度和高连接节点限流。
例如 `10万工具×1万情景=10亿次估值`，不能因为 LLM 只调用一次工具就记作一次轻量请求。
测量 p50/p95/p99、排队时间、吞吐、内存、超时率、覆盖率和数据陈旧度；基准必须记录硬件与模型组合。
流式批处理与近似算法都要有误差预算；分位 sketch 不能未经尾部验证替代精确 ES 排序。
执行区、监测计时与重定价批任务隔离资源；限流/取消传播、重试预算、死信和人工接管要实际演练。
模型不可用时保留确定性风险计算与监测规则；行情或模型输入失效则停止新增风险或走已批准的保守策略。

## 42.8 极端术语、面试自检与来源

### 42.8.1 极端术语别混用

- **VaR breach**：实际损失超过预测分位点，不自动证明模型故障；检查预测口径、样本外数据与例外频率。
- **Expected Shortfall / CVaR**：本章采用分位积分 ES；连续分布下的条件均值直觉不能直接搬到离散原子。
- **Black swan / stress / reverse stress**：未知尾部、指定冲击、反推失败条件不同，模型写出的故事不自带概率。
- **Wrong-way risk**：风险暴露增大同时对手信用变差；不是普通相关系数大，也不能只靠市场 ES 覆盖。
- **Procyclicality / liquidation spiral**：限额收紧与被迫卖出可能反馈放大波动，降仓算法必须考虑市场冲击。
- **Look-ahead / survivorship bias**：偷用未来知识与只保留幸存资产是两类偏差，固定种子不能修复它们。
- **Graph contamination / tipping-off**：图证据被污染与泄露调查/申报是不同威胁，各需来源治理与保密控制。
- **FRTB ES**：监管框架涉及特定置信度、流动性期限、压力校准等要求，不等同本章 80% 教学指标。

### 42.8.2 面试自检：能否写出验证方法

1. 为什么本例现货 ES 是 1,625.00 而非 1,500.00？写出 1.6 个尾部样本的权重。
2. 为什么买 Put 后 ES 下降而 VaR 上升？区分当前期权价值与到期收益，不说“模型不一致”。
3. 为什么不能把各资产已排序损失相加？构造两个负相关资产，展示情景对应关系被破坏。
4. Vega 的“每 1%”有几种解释？写出从绝对波动率到 vol point 的 100 倍换算。
5. 风险计算用了 double，如何防止污染账本？指出可信成交事件、舍入契约与独立入账 API。
6. “降仓”为什么仍需硬策略和审批？用卖出保护性期权、重复卖单、保证金增加举反例。
7. 2026 年新增标签可否用于 2024 年案件？分别回答今天调查与当时已知复盘的证据条件。
8. 能否把 SAR 草稿交给通用客服模型润色？先检查保密分类、供应商数据路径、地域和用途授权。
9. 哪些故障应产生 INCOMPLETE 而不是零？列出缺行情、未收敛定价、失效引用与情景缺失。
10. 如何证明“模型不能直调交易”？展示服务身份、网络 ACL、凭据边界和恶意提案的拒绝测试。

### 42.8.3 来源与适用边界

- [BIS Basel Framework：MAR33 内部模型法](https://www.bis.org/basel_framework/chapter/MAR/33.htm)：监管 ES、流动性期限与压力校准要求；适用版本由机构合规确定，不由本章教学分布替代。
- [R stats：Sample Quantiles](https://stat.ethz.ch/R-manual/R-devel/library/stats/html/quantile.html)：Type 1–9 的精确定义，用于解释库默认分位数为何可能不同。
- [Acerbi & Tasche，On the coherence of expected shortfall](https://doi.org/10.1016/S0378-4266(02)00283-2)：ES 在离散分布下的定义与一致性，不把简单条件均值当普适公式。
- [FATF Recommendations](https://www.fatf-gafi.org/en/publications/Fatfrecommendations/Fatf-recommendations.html)：风险为本、可疑交易报告、保密与禁止泄露等国际标准；以当地转化法律及行业规则为准。
- [31 CFR 1020.320](https://www.ecfr.gov/current/title-31/subtitle-B/chapter-X/part-1020/section-1020.320)：美国银行 SAR 的申报、留存与保密；重点参见 (b)、(d)、(e)，不能外推为全球统一门槛或时限。
- [NIST AI RMF 1.0](https://www.nist.gov/itl/ai-risk-management-framework)：AI 风险治理、测量与管理方法；不能替代金融授权或 AML 法律意见。

**交付检查**：情景有单位与概率来源，风险有可复算清单，尾部有边界质量，动作有硬策略与独立审批，AML 有时间与逐句引用，SAR 有人工与地域保密边界；少任一项，都不能把“模型说得合理”当作上线验收。
