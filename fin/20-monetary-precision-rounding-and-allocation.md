# 20. 金额表示、定点算术、舍入与尾差分摊：一分钱也不能凭概率

> **状态**：生产级教程｜**所属**：第三部分·金融工程代码基座｜**编号**：20 / 48
>
> **本章定位**：把“金额是多少、如何计算、如何舍入、尾差归谁、能否复算”写成跨 Go/Java/PostgreSQL 的确定性契约。下游 → [21 金融时间、交易日历与全局唯一流水号](21-financial-time-calendar-and-sequence-id.md) 使用本章的金额与分摊结果；→ 22 章使用本章的定点金额进入复式账本；→ 26 章在清结算批次中复用本章的舍入与尾差规则。
>
> **唯一真问题**：同一组业务输入在不同语言、不同机器、不同重试和不同展示精度下，能否得到**同一个可解释、可审计、守恒的最小货币单位结果**？

---

## 20.1 教学事故：每笔相差 1 分，累计对账差异 76,842.34 元

以下为合成教学场景，不是真实机构事故。某平台费率为万分之四；旧服务把金额转成二进制浮点，另一个服务使用不同舍入模式。下例单独隔离“舍入契约不一致”的影响，不把所有差异都归因于浮点误差。

一天有 `7,684,234` 笔、每笔 `CNY 12.50 = 1,250 分` 的订单，费率 `0.04% = 4/10,000`。精确费用为 `1,250 × 4/10,000 = 0.5 分`。渠道 `HALF_UP` 收 1 分，内部 `HALF_EVEN` 取 0 分；单笔相差 1 分：

```text
消费者订单本金总额      = 7,684,234 × 1,250 分 = 9,605,292,500 分 = CNY 96,052,925.00
支付渠道服务费          = 7,684,234 × 1 分  = 7,684,234 分 = CNY 76,842.34
内部 HALF_EVEN 服务费   = 7,684,234 × 0 分  = 0 分
支付渠道与内部账差      = 7,684,234 分      = CNY 76,842.34
```

真正的问题不是“哪种语言的四舍五入更好”，而是系统没有定义以下事实：

1. **金额的正本是什么**：元字符串、分整数，还是任意精度小数？
2. **费率的精度是什么**：万分比、百万分比，还是可变 scale 的小数？
3. **中间结果何时舍入**：每一行、每一个税种、每一级手续费，还是最后一次？
4. **总额与明细谁服从谁**：先算总额再分摊，还是先算明细再相加？
5. **尾差归属是否稳定**：同一批次重排输入后，是否会把 1 分分给另一个商户？
6. **负数如何舍入**：退款、冲正、折扣和负手续费是否与正数对称？

如果这些规则没有进入 API、数据库、代码和测试契约，所谓“金额正确”只是某台机器上的偶然结果。**金额是业务事实，不是展示字符串；舍入是业务规则，不是格式化动作；分摊是清算协议，不是 `for` 循环里的补丁。**

---

## 20.2 唯一真问题：守恒、确定性、可复算

金融金额计算必须同时满足三个性质：

### 20.2.1 守恒（conservation）

分摊前后的总额必须相等，或差异必须等于契约中明确允许的舍入误差，并在一个明确的科目中落地。

```text
sum(allocated_i) = rounded(total)
```

不能因为每一行单独舍入就让明细之和与支付总额差 1 分，也不能让系统“忽略小数”后把差异留在无主的浮动误差中。

### 20.2.2 确定性（determinism）

相同输入、相同规则版本、相同排序键，必须得到相同输出。输入顺序、CPU 架构、JVM 默认 locale、Go 的 map 遍历顺序都不能改变尾差归属。

### 20.2.3 可复算（reproducibility）

审计者仅凭原始金额、费率、scale、舍入模式、算法版本和稳定排序键，就能重新得到每一个中间值与最终值。不能只存“结果 100 分”，却不存“为什么是 100 分”。

本章所有实现遵守以下红线：

| 红线 | 生产规则 |
|---|---|
| 禁止二进制浮点 | 金额、费率、税率、价格、数量、折扣禁止 `float`/`double`；JSON 数字金额也不直接进二进制浮点类型 |
| 单位显式 | 每个金额字段带 `currency` 与 `minor_unit`；费率带 `rate_scale` 或明确分母 |
| scale 显式 | 计算上下文、输出 scale、舍入模式与规则版本必须可追溯 |
| 不提前格式化 | `format` 只用于展示；不能把展示字符串作为下一步计算输入 |
| 不裸改余额 | 计算结果进入分录/结算明细；修正使用冲正，不覆盖原值 |
| 稳定排序 | 分摊必须使用业务优先级 + 不可变唯一键，不得依赖 map/数据库无 `ORDER BY` 返回顺序 |

---

## 20.3 金额的三层模型：货币、数值、展示

### 20.3.1 最小货币单位（minor unit）

对有固定最小单位的货币，金额正本优先保存为整数：

```text
amount_minor = 金额 × 10^minor_unit
```

人民币、美元通常 `minor_unit = 2`，1 元 = 100 分；日元通常 `minor_unit = 0`，1 日元 = 1 个最小单位；某些数字资产使用 6、8、18 位小数，但它们不一定是法定货币，必须按资产元数据处理。

示例：

```text
CNY 12.34 元  ->  1234 分
USD 0.05 元   ->  5 cents
JPY 12 元      ->  12 yen
USDC 1.234567  ->  1,234,567 base units（若 token scale = 6）
```

整数最适合“已经落到货币最小单位”的账务金额：加减不会产生精度损失，比较是精确的，数据库可用 `BIGINT`，索引和聚合成本低。

但整数不是万能的。`12.5%`、`1/3`、外汇中间价、税前计税基数等中间值可能不能直接表示为目标货币的最小单位。因此系统需要区分：

```text
原始货币金额：CNY 10.00 -> 1000 分
中间精确值：  1000 × 3 / 10000 = 0.3 分
入账金额：    按规则舍入为 0 分或 1 分
```

不要把 `0.3 分` 偷偷当作 `0 分`；它必须保留到明确的舍入边界，或进入余数桶/尾差分摊算法。

### 20.3.2 定点数（fixed-point）与 scale

定点数可表示为：

```text
value = coefficient × 10^(-scale)
```

例如 `123.4500` 的 coefficient 是 `1234500`、scale 是 4；`123.45` 的 coefficient 是 `12345`、scale 是 2。数值相等不代表表示相同：

```text
123.4500 == 123.45       数值相等
scale(123.4500) = 4
scale(123.45)   = 2
```

**scale 不是精度本身。** scale 表示小数位数；precision 通常表示有效数字位数。`0.0012` 的 scale 是 4，precision 是 2。生产字段至少要明确：

| 字段 | 含义 | 示例 |
|---|---|---|
| `currency` | 货币/资产代码 | `CNY`、`USD`、`USDC` |
| `minor_unit` | 入账最小单位位数 | `2` |
| `unscaled_value` | 去掉小数点后的整数系数 | `12345` |
| `scale` | 当前数值的小数位数 | `4` |
| `rounding_mode` | 舍入规则 | `HALF_EVEN` |
| `calculation_scale` | 中间计算保留的小数位 | `12` |
| `rule_version` | 业务规则版本 | `fee-v3` |

### 20.3.3 为什么不把所有东西都换成“分”

如果费率是 `0.35%`，以万分比表示是 35；如果税率是 `6%`，以万分比表示是 600。只要分母固定，整数计算非常好：

```text
fee_minor = round(amount_minor × rate_bps / 10_000)
```

但是分母必须是契约的一部分。把 `35` 传给一个不知道它是 `35 / 10_000` 还是 `35 / 1_000_000` 的服务，整数反而会制造更隐蔽的灾难。对可变 scale、复杂分数或需要保留中间余数的计算，使用十进制定点类型；最终落账再转换到货币最小单位。

### 20.3.4 数值边界与溢出

以 `BIGINT` 存最小单位时，范围约为 `±9.22×10^18` 个最小单位。CNY 两位小数对应约 `±9.22×10^16` 元，但聚合、乘法和中间放大可能先溢出：

```text
amount_minor × rate_numerator
```

不能先在 `int64` 里相乘再期待除法“把它变回来”。Go 用 `math/big.Int` 或先做最大公约数约分；Java 用 `BigInteger`/`BigDecimal`；数据库使用 `NUMERIC` 做中间运算并设置业务上限。任何溢出都应返回错误，不能饱和到最大值，也不能静默变负数。

---

## 20.4 Java BigDecimal 与 Go decimal 的生产陷阱

### 20.4.1 Java：`BigDecimal` 不是自动正确

#### 陷阱一：`new BigDecimal(double)` 污染精度

```java
// 禁止：二进制浮点已经先丢失了十进制语义
new BigDecimal(0.1);

// 可用：从契约字符串解析
new BigDecimal("0.1");

// 可用：整数最小单位转十进制
BigDecimal.valueOf(10).movePointLeft(2); // 0.10
```

本章代码不使用 `double`；即使是 `BigDecimal.valueOf(double)`，也不能成为金额输入的入口。API 接受金额时优先接受字符串，或接受 `long amountMinor` + `currency`。

#### 陷阱二：`equals` 会比较 scale，`compareTo` 才比较数值

```java
new BigDecimal("1.0").equals(new BigDecimal("1.00"))    // false
new BigDecimal("1.0").compareTo(new BigDecimal("1.00")) // 0
```

金额业务的相等通常是数值相等，但签名、哈希、数据库唯一键可能需要规范化。建议边界处统一 `setScale`，然后在领域对象中封装比较，不要把 `BigDecimal` 直接散落在实体的 `equals/hashCode` 里。

#### 陷阱三：`divide` 可能抛 `ArithmeticException`

```java
new BigDecimal("1").divide(new BigDecimal("3")); // 非有限小数，抛异常
```

必须明确商的 scale 和舍入：

```java
new BigDecimal("1").divide(new BigDecimal("3"), 12, RoundingMode.HALF_EVEN);
```

更好的做法是为每个业务步骤定义 `MathContext` 或显式 scale，而不是随处加一个“够大的 12”。中间 scale 的选择必须能覆盖最小单位、费率分母和后续乘法的需求。

#### 陷阱四：`MathContext` 与 `setScale` 解决不同问题

- `MathContext(precision, roundingMode)` 控制有效数字位数，适合整体计算上下文。
- `setScale(scale, roundingMode)` 控制小数位数，适合进入货币单位或 API 输出前的量化。

金额落账通常需要 `setScale(currency.minor_unit, mode)`；只设置 precision 不能保证得到两位小数。

### 20.4.2 Go：decimal 库也有语义边界

Go 标准库没有内置十进制定点类型。常见选择：

| 类型 | 适用 | 风险 |
|---|---|---|
| `int64`/自研定点 | 固定 scale、极高吞吐、已知边界 | 乘法溢出、scale 混用、泛化能力弱 |
| `github.com/shopspring/decimal` | 账务常规、API 易用 | 运算产生对象；rounding API 和 scale 需显式审查；大量临时对象会增加 GC |
| `github.com/cockroachdb/apd/v3` | 可控 precision、condition、舍入上下文 | API 更复杂；必须检查 context condition/error |
| `math/big.Int` | 比率、超大整数、链上 base unit | 没有直接小数语义，需要自己实现 scale 和舍入 |

本章示例使用 `shopspring/decimal` 做可读的业务代码，但所有边界均在构造函数与 `RoundBank`/`Round` 处显式表达。生产高频撮合可使用整数定点，但必须先证明数值范围、scale 与溢出边界。

#### 陷阱一：JSON 数字进入 `float64`

```go
// 禁止：encoding/json 默认把 any 中的数字解码为 float64
var raw map[string]any
_ = json.Unmarshal(payload, &raw)

// 推荐：金额字段使用字符串，或用 struct 的 decimal 类型接收
type Request struct {
    Amount string `json:"amount"`
}
```

如果协议历史上必须传 JSON number，应使用 `json.Decoder.UseNumber()`，再从 `json.Number.String()` 解析；更推荐把金额协议改成字符串，因为 JSON schema 的 number 不能表达币种 scale 和舍入契约。

#### 陷阱二：decimal 的 zero value 与缺失值

`decimal.Decimal{}` 通常表示数值 0，但“字段缺失”“未计算”“业务上为 0”是三个不同状态。请求对象使用指针或显式 `Present` 标记；数据库 NULL 不要无条件映射成 0。

#### 陷阱三：输入 scale 不代表输出 scale

`decimal.NewFromString("1.0")` 与 `decimal.NewFromString("1.00")` 数值相同，但输出、哈希和审计文本可能不同。进入领域层后要规范化：先校验允许的输入 scale，再按货币 minor unit 量化，保存原始输入用于审计而不是依赖库内部 scale。

#### 陷阱四：忽略错误与边界

解析、量化、乘法范围检查、负数策略都不能被 `_` 丢弃。decimal 的 API 可能返回新值而不是原地修改，必须接住返回值；不要以为调用 `d.RoundBank(2)` 会改变 `d`。

---

## 20.5 舍入模式：规则、正负数与银行家舍入

设目标 scale 为 0，待舍入值为 `q + r`，其中 `q` 是整数部分、`r` 是小数部分。不同模式的差异必须写进产品契约。

| 模式 | 规则 | `1.5` | `2.5` | `-1.5` | 适用提示 |
|---|---|---:|---:|---:|---|
| `DOWN` | 向零截断 | 1 | 2 | -1 | 不是数学“向下”；退款可能被低估 |
| `UP` | 只要有余数就远离零 | 2 | 3 | -2 | 通常会放大收费/退款，谨慎使用 |
| `FLOOR` | 向负无穷 | 1 | 2 | -2 | 税务/区间下界有时适用，负数与 `DOWN` 不同 |
| `CEILING` | 向正无穷 | 2 | 3 | -1 | 上界/最低收费有时适用 |
| `HALF_UP` | 正好一半远离零 | 2 | 3 | -2 | 常被称为“四舍五入”，但负数也远离零 |
| `HALF_DOWN` | 正好一半向零 | 1 | 2 | -1 | 少用，必须明确业务理由 |
| `HALF_EVEN` | 正好一半取最近偶数 | 2 | 2 | -2 | 银行家舍入，长期聚合偏差较小 |

### 20.5.1 银行家舍入不是“全部取偶数”

`HALF_EVEN` 只在**恰好一半**时选择偶数，非半数仍取最近值：

```text
1.4 -> 1       最近整数是 1
1.5 -> 2       两个候选 1/2，取偶数 2
1.6 -> 2       最近整数是 2
2.5 -> 2       两个候选 2/3，取偶数 2
3.5 -> 4       两个候选 3/4，取偶数 4
```

对负数，`-1.5` 的候选是 `-1` 与 `-2`，偶数是 `-2`，所以结果是 `-2`；`-2.5` 的偶数候选是 `-2`，结果是 `-2`。**必须用库提供的 decimal rounding mode，不要自己用正数逻辑拼负数。**

### 20.5.2 分步可核算示例：服务费

订单金额为 CNY 12.35，即 1,235 分；费率为 `0.35% = 35 / 10,000`。精确服务费：

```text
1,235 × 35 / 10,000
= 43,225 / 10,000 分
= 4.3225 分
```

按目标 scale 0 的 `HALF_EVEN` 舍入：4.3225 分距离 4 分更近，因此：

```text
fee = 4 分 = CNY 0.04
```

若订单金额为 1,250 分：

```text
1,250 × 35 / 10,000 = 43,750 / 10,000 = 4.375 分
```

4.375 不是半分，仍为 4 分。若构造一个恰好半分的例子：金额 1,000 分、费率 5 / 10,000：

```text
1,000 × 5 / 10,000 = 0.5 分
```

`HALF_EVEN` 结果为 0 分，因为 0 是偶数；而 `HALF_UP` 为 1 分。若业务规定“最低收费 1 分”，这不是把舍入模式改成 `HALF_UP` 就结束，而应显式写成：

```text
fee = max(round_half_even(exact_fee), 1 分)
```

并说明仅在订单满足最低收费适用条件时执行。

### 20.5.3 先舍入再相加，通常不等于最后舍入

三个项目的精确费用分别为 `0.4` 分、`0.4` 分、`0.7` 分：

```text
逐项 HALF_EVEN：0 + 0 + 1 = 1 分
先汇总再舍入：0.4 + 0.4 + 0.7 = 1.5 分 -> 2 分（1.5 的偶数候选为 2）
```

两者差 1 分。正确方案不是争论哪一个“更数学”，而是定义结算规则：

- **行级入账**：每行必须是可入账最小单位，接受行级舍入并规定尾差科目；
- **批次级汇总**：保留精确中间值，批次总额只在最后一次量化，再按最大余数法分给行；
- **税务口径**：依赖当地法规，可能要求税额逐项取整或按发票总额取整。

规则版本必须被记录，不得让调用方自行选择。

---

## 20.6 最大余数法（Largest Remainder）：把尾差分给可解释的对象

### 20.6.1 问题定义

总额为 100 分，需要按权重 `1:2:3` 分给 A、B、C。精确份额：

```text
A = 100 × 1 / 6 = 16.666666... 分
B = 100 × 2 / 6 = 33.333333... 分
C = 100 × 3 / 6 = 50 分
```

先取整数下界：`16 + 33 + 50 = 99`，还剩 `1` 分。余数为：

```text
A remainder = 2 / 6
B remainder = 2 / 6
C remainder = 0 / 6
```

A 与 B 并列，使用稳定 tie-break：业务优先级相同则按 `allocation_key` 字典序。若 A 的 key 小于 B：

```text
A = 17 分
B = 33 分
C = 50 分
总和 = 100 分
```

最大余数法的核心是：先保证每个对象不超过其精确份额的向下整数部分，再把有限的剩余单位给最大余数。对正数总额和非负权重，它能把每个分配值控制在合理的相邻整数内，并保证总和守恒。

### 20.6.2 通用整数算法

设总额 `T` 为最小单位整数，权重为非负整数 `w_i`，总权重 `W = Σw_i`：

```text
raw_i       = T × w_i
base_i      = floor(raw_i / W)
remainder_i = raw_i mod W
left        = T - Σbase_i
```

按 `(remainder_i DESC, priority ASC, allocation_key ASC)` 排序，将 1 分依次加给前 `left` 个对象。注意：

1. `T × w_i` 可能溢出，生产实现要做宽整数或约分；
2. `left` 必须满足 `0 <= left < n`（权重和正且所有对象是非负分配时）；
3. 权重为 0 的对象可以参与排序但通常不应获得单位，业务需明确；
4. 空集合、总权重为 0、负总额都应在 API 层拒绝或走专门的负数算法；
5. 分配结果要保存 `base`、`remainder`、`rank`、`algorithm_version`，方便审计。

### 20.6.3 分步示例：平台费分给三个子商户

平台要把 10,001 分按成交额权重 `A=1, B=1, C=1` 分摊：

```text
T = 10,001
W = 3
每个 raw = 10,001
base = floor(10,001 / 3) = 3,333
remainder = 10,001 mod 3 = 2
```

三个对象各得 3,333 分，先分出 9,999 分，剩余 2 分。稳定 key 为 `A < B < C`，所以：

```text
A = 3,334 分
B = 3,334 分
C = 3,333 分
总和 = 3,334 + 3,334 + 3,333 = 10,001 分
```

下一批如果输入只有顺序变成 `C,B,A`，结果仍然必须是 A、B 得 3,334，C 得 3,333；因此排序不能使用输入位置，必须使用不可变 key。

### 20.6.4 负数与退款：不能盲目复用正数实现

最大余数法最清晰的生产约束是：**分配器只处理非负总额；退款以相同的正数分配逻辑计算退款金额，再对每一项取相反数。**

例如原支付分配 `[17, 33, 50]` 分，退款总额 100 分，退款结果是 `[-17, -33, -50]` 分。这样不会出现负数整除向零/向负无穷不一致的问题，也保证原支付与全额退款逐项抵消。

部分退款 1 分要定义业务策略：按原分配比例重新计算并最大余数分配，或按退款请求指定对象。不能因为“退款只有 1 分”就随机挑一行；应使用相同的稳定 key 和规则版本。

### 20.6.5 为什么不使用浮点 remainder 排序

`0.333333` 与 `0.333334` 的二进制近似可能改变排序，且不同语言的计算顺序可能造成边界不一致。使用整数对 `(raw_i mod W, W)` 比较：

```text
r1 / W > r2 / W  等价于  r1 > r2
```

完全不需要构造小数；并列时使用 key。对于有负权重或复杂比例的场景，先转化为有理数并统一分母，仍然用整数比较。

---

## 20.7 多级手续费：先定义层级和舍入边界

“手续费 1% + 渠道费 0.6% + 税 6%”至少存在三种不同业务语义：

1. 每项都基于订单本金；
2. 渠道费基于本金，税基于手续费合计；
3. 税基包含本金、手续费或某些费用，依当地法规决定。

代码不能凭变量名猜测税基。下面给出一个可复算的、仅用于说明算法的契约：

```text
订单本金：10,001 分
平台费：本金 × 1.00%，结果在该层按 HALF_EVEN 舍入到分
渠道费：本金 × 0.60%，结果在该层按 HALF_EVEN 舍入到分
税：平台费 + 渠道费的合计 × 6.00%，结果在税层按 HALF_EVEN 舍入到分
```

逐步计算：

```text
平台费精确值 = 10,001 × 100 / 10,000 = 100.01 分 -> 100 分
渠道费精确值 = 10,001 × 60  / 10,000 =  60.006 分 -> 60 分
税基         = 100 + 60 = 160 分
税精确值     = 160 × 600 / 10,000 = 9.6 分 -> 10 分
费用合计     = 100 + 60 + 10 = 170 分
消费者总扣款 = 10,001 + 170 = 10,171 分
```

如果错误地先用未舍入值构造税基：

```text
税基 = 100.01 + 60.006 = 160.016 分
税   = 160.016 × 6% = 9.60096 分 -> 10 分
```

本例最终碰巧相同，但不能把“碰巧相同”当作证明。另一个金额或费率很容易出现差 1 分。生产实现应把每个层级记录为不可变 calculation step：

```text
step_no | base_minor | rate_numerator | rate_denominator | exact_scale | rounded_minor | mode
```

### 20.7.1 组合费率的陷阱

若所有费用都基于同一本金、且法规允许最后一次舍入，可以合并费率：`1.00% + 0.60% = 1.60%`。但如果每项要单独出账、税基不同、最低收费不同或收款方不同，就不能合并。

### 20.7.2 多级折扣与最低收费

一个明确的顺序示例：

```text
原价：10,000 分
会员折扣：5%，折扣额 = 10,000 × 500 / 10,000 = 500 分
折后价：10,000 - 500 = 9,500 分
平台费：0.35%，精确值 = 9,500 × 35 / 10,000 = 33.25 分 -> 33 分
最低平台费：20 分；max(33, 20) = 33 分
```

若折扣额本身要按订单行分摊，先对行做最大余数分摊，使行折扣合计正好 500 分，再以每行折后金额计算费用，还是先按订单总额算费，都必须是产品契约。不能在“订单级”和“行级”之间随意切换。

---

## 20.8 状态/数据流图：从输入到入账的金额生命周期

### 20.8.1 状态机

```text
RECEIVED
   │ 解析字符串/整数，校验币种、scale、符号、范围
   ▼
VALIDATED
   │ 绑定 rate_version、rounding_mode、algorithm_version
   ▼
CALCULATED
   │ 产生中间 exact value、base、remainder、step trace
   ▼
ALLOCATED
   │ 最大余数法分配，校验 sum(children) == rounded(parent)
   ▼
APPROVED ──────┐
   │            │ 规则冲突、溢出、总额不守恒
   ▼            ▼
POSTED       REJECTED
   │
   ├─► SETTLED（已进入清结算批次）
   └─► REVERSED（冲正，原记录不可修改）
```

`CALCULATED` 不等于 `POSTED`。计算可以成功，但风控、税务规则、币种精度或账本幂等检查仍可能拒绝。`REJECTED` 不得被重试成另一套规则；重试必须带相同的幂等键和规则版本，或者生成明确的新业务请求。

### 20.8.2 数据流

```text
客户端金额字符串/最小单位整数
          │
          ▼
┌────────────────┐   currency metadata   ┌──────────────────────┐
│ Parse & Validate│ ───────────────────► │ scale/range/sign check│
└───────┬────────┘                       └──────────┬───────────┘
        ▼                                           ▼
┌──────────────────────────────────────────────────────────────┐
│ Calculation Context: rate version + scale + rounding + timezone? │
└──────────────────────┬───────────────────────────────────────┘
                       ▼
              ┌────────────────┐
              │ Exact calculation│  禁止提前 format
              └───────┬────────┘
                      ▼
              ┌────────────────┐
              │ Quantize/round │  只在契约边界舍入
              └───────┬────────┘
                      ▼
              ┌────────────────┐
              │ Largest remainder│  稳定 key 排序
              └───────┬────────┘
                      ▼
┌───────────────┐  sum check  ┌────────────────┐  transaction  ┌──────────┐
│ Audit trace    │ ◄────────── │ Ledger/outbox  │ ────────────► │ Posted   │
└───────────────┘             └────────────────┘               └──────────┘
```

### 20.8.3 幂等与重算

金额计算请求的幂等键至少包含业务事实 ID，例如 `order_id + fee_rule_version`；不要把随机 request ID 作为唯一依据。重算服务应：

1. 读取原始输入快照，不读取已经格式化的展示金额；
2. 固定规则版本、币种 metadata、舍入模式和排序键；
3. 生成新的 calculation result，并与原结果逐字段对比；
4. 差异进入对账/调查，不直接覆盖已入账值；
5. 若要修正，生成冲正和新分录，保留原始版本。

---

## 20.9 PostgreSQL DDL：把精度契约落到数据库

金额最小单位适合 `BIGINT`，需要保留高精度中间值和原始十进制文本时使用 `NUMERIC`。以下 DDL 只定义本章金额计算相关表；账本分录可在 22 章扩展。

```sql
CREATE TYPE money_rounding_mode AS ENUM (
    'DOWN', 'UP', 'FLOOR', 'CEILING',
    'HALF_UP', 'HALF_DOWN', 'HALF_EVEN'
);

CREATE TYPE money_calculation_status AS ENUM (
    'RECEIVED', 'VALIDATED', 'CALCULATED', 'ALLOCATED',
    'APPROVED', 'POSTED', 'SETTLED', 'REJECTED', 'REVERSED'
);

CREATE TABLE currency_metadata (
    currency_code       text PRIMARY KEY,
    minor_unit          smallint NOT NULL CHECK (minor_unit BETWEEN 0 AND 18),
    calculation_scale   smallint NOT NULL CHECK (calculation_scale >= minor_unit AND calculation_scale <= 38),
    is_active           boolean NOT NULL DEFAULT true,
    metadata_version    text NOT NULL,
    effective_from      timestamptz NOT NULL,
    effective_to        timestamptz,
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);

CREATE TABLE money_calculation (
    calculation_id          uuid PRIMARY KEY,
    idempotency_key         text NOT NULL UNIQUE,
    business_ref            text NOT NULL,
    currency_code           text NOT NULL REFERENCES currency_metadata(currency_code),
    input_amount_minor      bigint NOT NULL,
    input_scale             smallint NOT NULL CHECK (input_scale >= 0 AND input_scale <= 38),
    rate_numerator           numeric(38, 0),
    rate_denominator         numeric(38, 0),
    rounding_mode            money_rounding_mode NOT NULL,
    rule_version             text NOT NULL,
    algorithm_version        text NOT NULL,
    exact_amount              numeric(50, 30),
    rounded_amount_minor      bigint,
    status                    money_calculation_status NOT NULL,
    rejection_code            text,
    created_at                timestamptz NOT NULL,
    calculated_at             timestamptz,
    posted_at                 timestamptz,
    CHECK (rate_denominator IS NULL OR rate_denominator > 0),
    CHECK (rounded_amount_minor IS NULL OR status IN ('ALLOCATED','APPROVED','POSTED','SETTLED','REVERSED')),
    CHECK (status <> 'REJECTED' OR rejection_code IS NOT NULL)
);

CREATE INDEX money_calculation_business_ref_idx
    ON money_calculation (business_ref, created_at);

CREATE TABLE money_allocation (
    calculation_id          uuid NOT NULL REFERENCES money_calculation(calculation_id),
    allocation_key          text NOT NULL,
    beneficiary_ref         text NOT NULL,
    priority                integer NOT NULL DEFAULT 0,
    weight                  numeric(38, 0) NOT NULL CHECK (weight >= 0),
    base_amount_minor       bigint NOT NULL CHECK (base_amount_minor >= 0),
    remainder_numerator     numeric(38, 0) NOT NULL CHECK (remainder_numerator >= 0),
    remainder_denominator   numeric(38, 0) NOT NULL CHECK (remainder_denominator > 0),
    remainder_rank          integer,
    allocated_amount_minor  bigint NOT NULL CHECK (allocated_amount_minor >= 0),
    algorithm_version       text NOT NULL,
    PRIMARY KEY (calculation_id, allocation_key),
    UNIQUE (calculation_id, remainder_rank)
);

CREATE TABLE money_calculation_step (
    calculation_id          uuid NOT NULL REFERENCES money_calculation(calculation_id),
    step_no                 integer NOT NULL CHECK (step_no > 0),
    step_code               text NOT NULL,
    base_amount_minor       bigint NOT NULL,
    rate_numerator           numeric(38, 0),
    rate_denominator         numeric(38, 0),
    exact_value              numeric(50, 30),
    output_amount_minor      bigint,
    rounding_mode            money_rounding_mode,
    rule_version             text NOT NULL,
    PRIMARY KEY (calculation_id, step_no)
);

-- 生产环境还应由事务/约束触发器或应用层在同一事务中验证：
-- SUM(money_allocation.allocated_amount_minor)
--   = money_calculation.rounded_amount_minor。
```

### 20.9.1 DDL 设计说明

1. `input_amount_minor` 是金额正本，不用 `numeric` 存“看起来像金额”的任意字符串。
2. `exact_amount` 只是计算轨迹，不是最终入账金额；它不能替代 `rounded_amount_minor`。
3. 费率用分子/分母避免把费率存成没有单位的 `0.0035`；例如 0.35% 存 `35 / 10,000`。
4. `allocation_key` 必须不可变且有业务语义；不能只依赖自增 ID，因为迁移/重放可能改变插入顺序。
5. PostgreSQL `numeric` 不会自动解决业务舍入；`numeric` 的默认表达式结果和应用层 BigDecimal/decimal 仍要由规则测试锁定。
6. 金额表禁止 `UPDATE` 已 POSTED 的数值列；如果必须更正，写 `REVERSED` 和新的计算记录。

---

## 20.10 Go 1.24 核心代码：解析、舍入与最大余数分配

以下代码使用 `github.com/shopspring/decimal v1.4.0`，项目 `go.mod` 应锁定该版本。金额入口是字符串，落账结果是 `int64` 最小单位；代码没有二进制浮点类型。Go 示例的输入语法限定为 `[0-9]+(\.[0-9]+)?`，最多 64 字节；拒绝科学计数法（包括 `1e3`）、正负号、空白及省略整数/小数部分的写法。长度和格式在调用 decimal 前检查，避免短指数文本触发极端幂运算；请求体大小仍需在 HTTP/RPC 入口单独限制，不能靠此函数阻止上游分配超大字符串。

```go
package money

import (
	"errors"
	"fmt"
	"math"
	"math/big"
	"sort"

	"github.com/shopspring/decimal"
)

var (
	ErrInvalidAmount       = errors.New("invalid monetary amount")
	ErrOverflow             = errors.New("monetary amount overflow")
	ErrInvalidAllocation    = errors.New("invalid allocation input")
)

const maxAmountTextBytes = 64

// ParseMinor 只接收不带符号的普通十进制文本，最多 minorUnit 位小数。
// 例如 "12"/"12.3"/"12.34" 在 minorUnit=2 时返回 1200/1230/1234。
// 不接受科学计数法，不舍入；即使多余小数位全为 0 也拒绝。
func ParseMinor(text string, minorUnit int32) (int64, error) {
	if minorUnit < 0 || minorUnit > 18 || len(text) == 0 || len(text) > maxAmountTextBytes {
		return 0, ErrInvalidAmount
	}
	seenDot := false
	for i := 0; i < len(text); i++ {
		c := text[i]
		if c >= '0' && c <= '9' {
			continue
		}
		if c == '.' && !seenDot && i > 0 && i < len(text)-1 {
			seenDot = true
			continue
		}
		return 0, ErrInvalidAmount
	}
	d, err := decimal.NewFromString(text)
	if err != nil || d.IsNegative() {
		return 0, ErrInvalidAmount
	}
	if d.Exponent() < -minorUnit {
		return 0, fmt.Errorf("%w: too many fractional digits", ErrInvalidAmount)
	}
	minor := d.Shift(minorUnit)
	if !minor.Equal(minor.Truncate(0)) {
		return 0, fmt.Errorf("%w: not an integral minor unit", ErrInvalidAmount)
	}
	// BigInt 会截断小数，因此必须先验证整数性；Coefficient 仅是系数，
	// 不含指数（例如 12 × 10^2 的系数仍为 12），不能直接作为最终金额。
	integer := minor.BigInt()
	if !integer.IsInt64() {
		return 0, ErrOverflow
	}
	return integer.Int64(), nil
}

// RateMinor 计算 amountMinor * numerator / denominator，并按目标最小单位舍入。
// HALF_EVEN 是银行家舍入；这里用整数商/余数实现十进制语义。
func RateMinor(amountMinor, numerator, denominator int64) (int64, error) {
	if amountMinor < 0 || numerator < 0 || denominator <= 0 {
		return 0, ErrInvalidAmount
	}
	// 先做精确整数商/余数，再执行 HALF_EVEN；不依赖某个中间 decimal scale。
	n := new(big.Int).Mul(big.NewInt(amountMinor), big.NewInt(numerator))
	d := big.NewInt(denominator)
	q, r := new(big.Int), new(big.Int)
	q.QuoRem(n, d, r)
	twoR := new(big.Int).Lsh(new(big.Int).Set(r), 1)
	cmp := twoR.Cmp(d)
	if cmp > 0 || (cmp == 0 && q.Bit(0) == 1) {
		q.Add(q, big.NewInt(1))
	}
	if !q.IsInt64() {
		return 0, ErrOverflow
	}
	return q.Int64(), nil
}

type AllocationInput struct {
	Key      string
	Priority int
	Weight   int64
}

type AllocationOutput struct {
	Key                  string
	Priority              int
	BaseMinor            int64
	RemainderNumerator   int64
	RemainderDenominator int64
	Rank                 int
	AllocatedMinor       int64
}

// AllocateNonNegative 用最大余数法分配非负整数 total。
// tie-break: remainder DESC, priority ASC, Key ASC；不依赖输入切片顺序。
func AllocateNonNegative(total int64, inputs []AllocationInput) ([]AllocationOutput, error) {
	if total < 0 || len(inputs) == 0 {
		return nil, ErrInvalidAllocation
	}
	var weightSum int64
	seen := make(map[string]struct{}, len(inputs))
	for _, in := range inputs {
		if in.Key == "" || in.Weight < 0 {
			return nil, ErrInvalidAllocation
		}
		if _, ok := seen[in.Key]; ok {
			return nil, fmt.Errorf("%w: duplicate key %q", ErrInvalidAllocation, in.Key)
		}
		seen[in.Key] = struct{}{}
		if in.Weight > math.MaxInt64-weightSum {
			return nil, ErrOverflow
		}
		weightSum += in.Weight
	}
	if weightSum <= 0 {
		return nil, ErrInvalidAllocation
	}

	out := make([]AllocationOutput, 0, len(inputs))
	var baseSum int64
	for _, in := range inputs {
		// 这里用 math/big 做整数商和余数，避免十进制定点除法的精度
		// 上限在极端大数时影响截断结果；raw = total * weight 不在 int64 中相乘。
		raw := new(big.Int).Mul(big.NewInt(total), big.NewInt(in.Weight))
		q, r := new(big.Int), new(big.Int)
		q.QuoRem(raw, big.NewInt(weightSum), r)
		if !q.IsInt64() || !r.IsInt64() {
			return nil, ErrOverflow
		}
		base := q.Int64()
		rem := r.Int64()
		if base > math.MaxInt64-baseSum {
			return nil, ErrOverflow
		}
		baseSum += base
		out = append(out, AllocationOutput{
			Key: in.Key, Priority: in.Priority,
			BaseMinor: base, RemainderNumerator: rem,
			RemainderDenominator: weightSum,
		})
	}
	left := total - baseSum
	if left < 0 || left > int64(len(out)) {
		return nil, ErrInvalidAllocation
	}

	sort.Slice(out, func(i, j int) bool {
		if out[i].RemainderNumerator != out[j].RemainderNumerator {
			return out[i].RemainderNumerator > out[j].RemainderNumerator
		}
		if out[i].Priority != out[j].Priority {
			return out[i].Priority < out[j].Priority
		}
		return out[i].Key < out[j].Key
	})
	for i := range out {
		out[i].AllocatedMinor = out[i].BaseMinor
		if int64(i) < left {
			out[i].AllocatedMinor++
			out[i].Rank = i + 1
		}
	}
	// 输出按 key 排序，调用方不能把“余数排序后的切片位置”当业务顺序。
	sort.Slice(out, func(i, j int) bool { return out[i].Key < out[j].Key })
	var sum int64
	for _, item := range out {
		if item.AllocatedMinor > math.MaxInt64-sum {
			return nil, ErrOverflow
		}
		sum += item.AllocatedMinor
	}
	if sum != total {
		return nil, fmt.Errorf("%w: sum=%d total=%d", ErrInvalidAllocation, sum, total)
	}
	return out, nil
}
```

代码要点：

- `ParseMinor` 拒绝超过货币 scale 的输入（如两位货币的 `12.340`）；若产品允许自动舍入，必须新增显式 API，不能让解析函数偷偷舍入。`Shift` 后先验证整数性，再用 v1.4.0 的 `BigInt()` 得到包含指数的完整整数，最后检查 `IsInt64()`，不将 `Coefficient()` 误当最终值。
- `RateMinor` 的输入全部是整数，费率分母明确为 `10,000` 等业务单位。
- 分配乘法使用 `math/big.Int` 避免 `total * weight` 在 `int64` 中先溢出；更高吞吐版本可用已证明安全的宽整数。
- `sort.Slice` 的比较键完整且稳定；同一 key 已拒绝重复。
- 生产代码还应限制 `len(inputs)`、费率和总额最大值，避免恶意请求消耗内存或 CPU。

### 20.10.1 Go 测试示例

将上一节完整代码保存为 `money.go`，以下完整测试保存为 `money_test.go`；在独立模块中执行 `go mod init example.com/moneycheck`、`go get github.com/shopspring/decimal@v1.4.0`、`go test -v ./...`。测试不需要数据库；依赖下载需要网络或已有模块缓存。示例拒绝负数（包括 `-0.00`），`minorUnit` 仅支持 0–18，这些是该入口的业务限制，而非 decimal 库的通用限制。

```go
package money

import (
	"errors"
	"math"
	"strings"
	"testing"
)

func TestParseMinor(t *testing.T) {
	tests := []struct {
		name      string
		text      string
		minorUnit int32
		want      int64
		wantErr   error
	}{
		{"integer", "12", 2, 1200, nil},
		{"one_fractional_digit", "12.3", 2, 1230, nil},
		{"two_fractional_digits", "12.34", 2, 1234, nil},
		{"zero", "0.00", 2, 0, nil},
		{"no_fractional_digits", "12", 0, 12, nil},
		{"scale_18", "0.000000000000000001", 18, 1, nil},
		{"scientific_notation", "1e3", 2, 0, ErrInvalidAmount},
		{"extreme_positive_exponent", "1e2147483647", 2, 0, ErrInvalidAmount},
		{"extreme_negative_exponent", "1E-2147483648", 2, 0, ErrInvalidAmount},
		{"max_int64", "9223372036854775807", 0, math.MaxInt64, nil},
		{"max_int64_scaled", "92233720368547758.07", 2, math.MaxInt64, nil},
		{"overflow", "9223372036854775808", 0, 0, ErrOverflow},
		{"overflow_scaled", "92233720368547758.08", 2, 0, ErrOverflow},
		{"too_many_fractional_digits", "12.345", 2, 0, ErrInvalidAmount},
		{"extra_zero_digit", "12.340", 2, 0, ErrInvalidAmount},
		{"fraction_for_scale_zero", "12.0", 0, 0, ErrInvalidAmount},
		{"negative_scale", "12", -1, 0, ErrInvalidAmount},
		{"scale_too_large", "12", 19, 0, ErrInvalidAmount},
		{"empty", "", 2, 0, ErrInvalidAmount},
		{"negative", "-12", 2, 0, ErrInvalidAmount},
		{"negative_zero", "-0.00", 2, 0, ErrInvalidAmount},
		{"positive_sign", "+12", 2, 0, ErrInvalidAmount},
		{"whitespace", " 12", 2, 0, ErrInvalidAmount},
		{"missing_integer", ".12", 2, 0, ErrInvalidAmount},
		{"missing_fraction", "12.", 2, 0, ErrInvalidAmount},
		{"multiple_dots", "1.2.3", 2, 0, ErrInvalidAmount},
		{"length_limit", strings.Repeat("0", 64), 2, 0, nil},
		{"too_long", strings.Repeat("0", 65), 2, 0, ErrInvalidAmount},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := ParseMinor(tt.text, tt.minorUnit)
			if !errors.Is(err, tt.wantErr) || got != tt.want {
				t.Fatalf("ParseMinor(%q, %d) = (%d, %v), want (%d, %v)",
					tt.text, tt.minorUnit, got, err, tt.want, tt.wantErr)
			}
		})
	}
}

func TestAllocateNonNegative(t *testing.T) {
	got, err := AllocateNonNegative(10001, []AllocationInput{
		{Key: "C", Priority: 0, Weight: 1},
		{Key: "A", Priority: 0, Weight: 1},
		{Key: "B", Priority: 0, Weight: 1},
	})
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]int64{"A": 3334, "B": 3334, "C": 3333}
	for _, item := range got {
		if item.AllocatedMinor != want[item.Key] {
			t.Fatalf("key=%s got=%d want=%d", item.Key, item.AllocatedMinor, want[item.Key])
		}
	}
}

func TestBankersRounding(t *testing.T) {
	// 1000 * 5 / 10000 = 0.5 分；HALF_EVEN 取偶数 0。
	got, err := RateMinor(1000, 5, 10000)
	if err != nil || got != 0 {
		t.Fatalf("got=%d err=%v", got, err)
	}
}
```

---

## 20.11 Java 21 核心代码：BigDecimal、金额值对象与分配

Java 代码把金额输入限制为字符串或最小单位 `long`。`BigDecimal` 的 `RoundingMode.HALF_EVEN` 只在边界显式使用；所有中间除法给出 scale/context，避免无限小数异常。

```java
package money;

import java.math.BigDecimal;
import java.math.BigInteger;
import java.math.RoundingMode;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

public final class MoneyMath {
    private MoneyMath() {}

    public static long parseMinor(String text, int minorUnit) {
        if (text == null || text.isBlank() || minorUnit < 0 || minorUnit > 18) {
            throw new IllegalArgumentException("invalid amount");
        }
        BigDecimal value;
        try {
            value = new BigDecimal(text);
        } catch (NumberFormatException ex) {
            throw new IllegalArgumentException("invalid decimal amount", ex);
        }
        if (value.signum() < 0 || value.scale() > minorUnit) {
            throw new IllegalArgumentException("amount has invalid sign or scale");
        }
        BigDecimal minor = value.movePointRight(minorUnit);
        try {
            return minor.toBigIntegerExact().longValueExact();
        } catch (ArithmeticException ex) {
            throw new IllegalArgumentException("amount is not an integral minor unit or overflows", ex);
        }
    }

    public static long rateMinor(long amountMinor, long numerator, long denominator) {
        if (amountMinor < 0 || numerator < 0 || denominator <= 0) {
            throw new IllegalArgumentException("invalid rate input");
        }
        BigInteger product = BigInteger.valueOf(amountMinor)
                .multiply(BigInteger.valueOf(numerator));
        BigInteger divisor = BigInteger.valueOf(denominator);
        BigInteger[] qr = product.divideAndRemainder(divisor);
        BigInteger twiceRemainder = qr[1].shiftLeft(1);
        int comparison = twiceRemainder.compareTo(divisor);
        // 非负数 HALF_EVEN：大于一半进位；恰好一半时商为奇数才进位。
        if (comparison > 0 || (comparison == 0 && qr[0].testBit(0))) {
            qr[0] = qr[0].add(BigInteger.ONE);
        }
        return exactLong(qr[0]);
    }

    public record Input(String key, int priority, long weight) {}
    public record Output(String key, int priority, long baseMinor,
                         long remainderNumerator, long remainderDenominator,
                         int rank, long allocatedMinor) {}

    public static List<Output> allocateNonNegative(long total, List<Input> inputs) {
        if (total < 0 || inputs == null || inputs.isEmpty()) {
            throw new IllegalArgumentException("invalid allocation input");
        }
        Set<String> keys = new HashSet<>();
        BigInteger weightSum = BigInteger.ZERO;
        for (Input input : inputs) {
            if (input.key() == null || input.key().isBlank()
                    || input.weight() < 0 || !keys.add(input.key())) {
                throw new IllegalArgumentException("invalid or duplicate allocation key");
            }
            weightSum = weightSum.add(BigInteger.valueOf(input.weight()));
        }
        if (weightSum.signum() <= 0) {
            throw new IllegalArgumentException("weight sum must be positive");
        }
        // Output 记录把分母作为 long 保存；若产品允许更大的权重和，应把该字段改为 BigInteger/字符串。
        if (weightSum.compareTo(BigInteger.valueOf(Long.MAX_VALUE)) > 0) {
            throw new IllegalArgumentException("weight sum overflows output denominator");
        }

        List<Temp> temps = new ArrayList<>(inputs.size());
        BigInteger totalBI = BigInteger.valueOf(total);
        BigInteger baseSum = BigInteger.ZERO;
        for (Input input : inputs) {
            BigInteger raw = totalBI.multiply(BigInteger.valueOf(input.weight()));
            BigInteger[] qr = raw.divideAndRemainder(weightSum);
            temps.add(new Temp(input.key(), input.priority(), qr[0], qr[1]));
            baseSum = baseSum.add(qr[0]);
        }
        BigInteger left = totalBI.subtract(baseSum);
        if (left.signum() < 0 || left.compareTo(BigInteger.valueOf(temps.size())) > 0) {
            throw new IllegalStateException("invalid largest remainder result");
        }

        temps.sort(Comparator
                .comparing(Temp::remainder, Comparator.reverseOrder())
                .thenComparingInt(Temp::priority)
                .thenComparing(Temp::key));
        List<Output> ranked = new ArrayList<>(temps.size());
        for (int i = 0; i < temps.size(); i++) {
            Temp temp = temps.get(i);
            BigInteger allocation = temp.base();
            int rank = 0;
            if (BigInteger.valueOf(i).compareTo(left) < 0) {
                allocation = allocation.add(BigInteger.ONE);
                rank = i + 1;
            }
            ranked.add(new Output(temp.key(), temp.priority(),
                    exactLong(temp.base()), exactLong(temp.remainder()),
                    exactLong(weightSum), rank, exactLong(allocation)));
        }
        ranked.sort(Comparator.comparing(Output::key));
        BigInteger sum = ranked.stream()
                .map(item -> BigInteger.valueOf(item.allocatedMinor()))
                .reduce(BigInteger.ZERO, BigInteger::add);
        if (!sum.equals(totalBI)) {
            throw new IllegalStateException("allocation does not conserve total");
        }
        return List.copyOf(ranked);
    }

    private static long exactLong(BigInteger value) {
        try {
            return value.longValueExact();
        } catch (ArithmeticException ex) {
            throw new IllegalArgumentException("allocation overflows long", ex);
        }
    }

    private record Temp(String key, int priority, BigInteger base, BigInteger remainder) {}
}
```

Java 实现使用 `BigInteger` 做最大余数法的乘法与除法，即使最终结果是 `long`，也不会在 `total × weight` 处静默溢出。`rateMinor` 的示例为便于展示设置中间 scale 18；生产需要根据业务最大金额、费率分母和法规精度证明该 scale 足够，或直接使用有理数 `BigInteger numerator/denominator` 保存精确商直到最后一步。

### 20.11.1 Java 值对象边界

生产项目不应到处传裸 `long`，因为 `1234` 可能是 CNY 分、USD cents、JPY 日元或 token base unit。建议封装：

```java
public record MinorMoney(String currency, int minorUnit, long amountMinor) {
    public MinorMoney {
        if (currency == null || currency.isBlank()) throw new IllegalArgumentException("currency");
        if (minorUnit < 0 || minorUnit > 18) throw new IllegalArgumentException("minorUnit");
    }
}
```

更完整的生产版本还应校验 currency metadata version、资产是否允许负数、是否允许超过数据库/账本上限，并为加减法检查币种一致性。

---

## 20.12 测试：不只测几个“1.23 + 2.34”

### 20.12.1 单元测试矩阵

| 类别 | 必测项 |
|---|---|
| 解析 | `0`、`0.00`、最大允许 scale、超过 scale、空串、科学记数法策略、前导正号、负号、超大值 |
| 舍入 | `1.4/1.5/1.6`、`2.5/3.5`、`-1.4/-1.5/-1.6`、各模式、scale 从 4 到 2 |
| 比较 | `1.0` 与 `1.00` 数值相等；格式/哈希规范化稳定 |
| 除法 | 1/3、1/8、0/7、分母 0、有限小数与无限小数 |
| 费率 | 0%、100%、超过 100% 是否允许、最大金额、最大分母 |
| 分配 | 余数 0、余数并列、权重 0、一个对象、总额小于对象数、总额 0、乱序输入 |
| 守恒 | `sum(children) == parent`；正数退款逐项相反后为 0 |
| 版本 | 同输入同规则版本字节级结果相同；规则版本变化能被识别 |

### 20.12.2 性质测试（property-based testing）

对随机生成的非负 `T`、权重和稳定 key，验证：

```text
1. sum(result_i) = T
2. 每个 result_i >= 0
3. result_i 只可能是 floor(T*w_i/W) 或 floor(...) + 1
4. left 个 +1 的对象按 remainder/priority/key 排名前 left
5. 输入切片随机打乱，结果按 key 不变
6. 同一输入重复运行 N 次，序列化结果完全一致
```

对舍入，生成所有目标 scale 范围内的十进制定点值，验证：

```text
abs(rounded - exact) <= 0.5 × 10^(-targetScale)
```

但要注意 `FLOOR`/`CEILING`/`DOWN`/`UP` 有各自单调性和方向性质，不能把所有模式都套到 HALF 模式的误差断言。

### 20.12.3 Golden test 与跨语言一致性

建立 JSONL 或表格形式的 golden vectors，但金额和费率都使用字符串/整数：

```json
{"case":"fee-001","amountMinor":"1235","numerator":"35","denominator":"10000","mode":"HALF_EVEN","expectedMinor":"4"}
{"case":"alloc-001","totalMinor":"10001","weights":{"A":"1","B":"1","C":"1"},"expected":{"A":"3334","B":"3334","C":"3333"}}
```

Go 与 Java 都读取同一批向量，任何差异都阻断发布。不要用语言默认 JSON 数字类型读取这些字段；测试向量中的大整数必须是字符串。

### 20.12.4 数据库一致性测试

在事务中插入 calculation、steps、allocation，提交前验证：

```sql
SELECT c.calculation_id,
       c.rounded_amount_minor,
       COALESCE(SUM(a.allocated_amount_minor), 0) AS allocated_sum
FROM money_calculation c
LEFT JOIN money_allocation a USING (calculation_id)
WHERE c.calculation_id = $1
GROUP BY c.calculation_id, c.rounded_amount_minor
HAVING COALESCE(SUM(a.allocated_amount_minor), 0) <> c.rounded_amount_minor;
```

查询必须返回 0 行。并发测试还要覆盖同一 `idempotency_key` 的多个事务：只能有一个计算事实成功，其他请求读取同一结果，不能产生两个不同尾差版本。

---

## 20.13 压测、极端边界与观测

### 20.13.1 压测模型

压测不要只测“加法吞吐”，应按生产比例构造：

1. 90% 两位货币金额、10% 高 scale 资产；
2. 费率来自多个版本，包含 0%、最低收费和税层；
3. 分配对象数从 1、2、10、100、10,000 分布；
4. 乱序请求、重复幂等键和规则缓存 miss；
5. 结果写入 PostgreSQL，测试唯一键冲突、事务回滚和重试；
6. 记录 p50/p95/p99 延迟、吞吐、GC pause、分配对象数和拒绝率。

Go `shopspring/decimal` 在大量短生命周期计算中会产生对象，观察 heap allocation 与 GC；Java `BigDecimal` 也会生成临时对象，观察分配率、GC 频率和尾延迟。只有在 profile 证明算术是瓶颈后，才考虑整数定点/对象复用；优化不能改变 golden vectors。

### 20.13.2 极端边界清单

- 金额为 0，费率为 0，权重全 0；
- `total = MaxInt64`，权重和接近 `MaxInt64`；
- `total × weight` 超过 64 位但最终商在 64 位范围内；
- 38 位数据库 numeric 与应用 long 边界不一致；
- scale 为 18 的 token 与 CNY 两位金额误混；
- `-0.00` 的输入规范化；
- `1E+3`、`1e-2` 是否允许；允许时如何记录原始文本；
- 恰好半分的正数与负数；
- 分配对象 key 相同、空 key、Unicode 规范化不同；
- 排序 key 比较受 locale 影响；应使用字节/代码点稳定比较并固定规范；
- PostgreSQL `NUMERIC` NULL、约束绕过、部分写入和事务重试；
- 规则版本在批处理中途切换；批次必须固定快照；
- 退款金额大于原支付、部分退款重复、冲正再次冲正；
- 分配结果写入成功但 outbox 发布超时；状态必须能靠幂等重放收敛。

### 20.13.3 观测与告警

至少暴露以下指标和日志字段：

```text
calculation_id, idempotency_key, business_ref
currency, minor_unit, rule_version, algorithm_version
rounding_mode, input_minor, exact_scale, rounded_minor
allocation_count, remainder_left, conservation_check
status, rejection_code, latency, retry_count
```

告警条件包括：

- 守恒检查失败（P0）；
- 同一幂等键出现不同结果（P0）；
- 非法 scale/币种/溢出增长（P1）；
- 规则版本未知或 metadata 过期（P1）；
- 尾差集中在单一受益人超出统计基线（P1，可能是 key/排序或套利问题）；
- GC/CPU/数据库锁等待使 p99 超过结算 SLA（P1）。

不要把每个小数中间值全部打到普通日志；完整 calculation step 写审计表，日志只带 trace ID 与摘要哈希，避免敏感金额泄露和日志成本失控。

---

## 20.14 生产审核清单

### API 与协议

- [ ] 金额是字符串或明确的最小单位整数，绝不由客户端传二进制浮点语义。
- [ ] 币种、minor unit、费率分母、scale、舍入模式和规则版本都是显式字段或服务端不可变配置。
- [ ] API 明确正负号、科学记数法、超过 scale 的输入和最低收费行为。
- [ ] 返回结果包含最小单位金额；展示字符串由前端/边界层生成，不反向参与计算。

### 计算与分摊

- [ ] 中间步骤不提前格式化、不静默截断。
- [ ] 每个除法都有明确 scale/context 和 rounding mode。
- [ ] 最大余数法使用整数余数比较和稳定 tie-break。
- [ ] 分配前后总额守恒，负数退款有单独契约。
- [ ] 多级手续费税基、层级、最低收费和舍入边界有产品/法规确认。

### 存储与账本

- [ ] `BIGINT`/`NUMERIC` 的选择经过范围证明；数据库约束拒绝负数/错误状态组合。
- [ ] 保存规则版本、算法版本、舍入模式、输入快照和 calculation steps。
- [ ] 已入账金额不可 UPDATE/DELETE；更正通过冲正和新事实。
- [ ] 幂等键唯一，计算、明细、outbox 在一致性边界内提交。

### 测试与运行

- [ ] Go/Java 共用 golden vectors。
- [ ] 覆盖 HALF_EVEN 的 `1.5/2.5/-1.5/-2.5`、溢出、乱序和并发幂等。
- [ ] 压测观察 GC、对象分配、p99 和数据库锁。
- [ ] 守恒失败、规则漂移、重复结果和尾差异常有告警与人工处置流程。

---

## 20.15 术语表

| 术语 | 定义 |
|---|---|
| 最小货币单位 minor unit | 某货币/资产允许入账的最小单位，如 CNY 分、JPY 日元、token base unit |
| scale | 十进制定点数的小数位数 |
| precision | 有效数字位数；不等于 scale |
| coefficient / unscaled value | 去掉小数点后的整数系数 |
| 定点数 fixed-point | 以整数系数和固定 scale 表示十进制数 |
| 二进制浮点 | `float`/`double` 一类以二进制近似表示的数；本章禁止用于金额 |
| 量化 quantization | 把高精度数转换到目标 scale/最小单位的过程 |
| 舍入模式 rounding mode | 定义量化时如何处理被舍弃部分的规则 |
| 银行家舍入 HALF_EVEN | 恰好一半时选择最近的偶数，非一半仍取最近值 |
| 尾差 rounding residual | 精确分配与整数最小单位分配之间留下的余数 |
| 最大余数法 | 先取各份额下界，再按余数降序分配剩余最小单位的算法 |
| 稳定 tie-break | 余数相同的确定性决胜规则，如 priority、allocation key |
| 守恒 conservation | 分配明细之和等于规定的总额 |
| 费率分子/分母 | 用整数表达比例，如 0.35% = 35/10,000 |
| 中间精确值 | 尚未量化到货币最小单位的十进制定点结果 |
| 规则版本 rule version | 费率、税基、舍入顺序等业务规则的不可变版本 |
| 算法版本 algorithm version | 计算实现/排序/分配算法的版本，便于复算 |
| 计算轨迹 calculation trace | 记录输入、步骤、中间值、舍入与输出的审计资料 |
| 负零 negative zero | 某些十进制表示中的 `-0.00`；业务边界应规范化为唯一零表示 |
| 最低收费 minimum fee | 舍入后仍需满足的业务下限，不等同于舍入模式 |
| 幂等键 idempotency key | 同一业务事实重试时复用的唯一键，防止重复计算/入账 |

---

## 20.16 10 分钟面试答题卡

### Q1：为什么金融系统不能使用 `float`/`double`？

**答**：二进制浮点不能精确表示多数十进制小数，误差会在乘法、累加、比较和跨语言序列化中暴露。生产上金额正本用货币最小单位整数；需要中间小数时用十进制定点 `BigDecimal`/decimal，并显式 scale、舍入模式和规则版本。展示格式不是计算输入。

### Q2：整数分和 BigDecimal/decimal 如何选择？

**答**：已经落到固定货币最小单位的金额优先整数，比较和加减精确、性能好；费率、税基、外汇价格和 `1/3` 等中间值需要定点或有理数。最终入账前量化到 minor unit。整数实现必须证明乘法不溢出，定点实现必须检查 scale/context。

### Q3：`1.0` 和 `1.00` 在 Java 里为什么会有坑？

**答**：`BigDecimal.equals` 同时比较数值和 scale，所以可能返回 false；`compareTo` 比较数值，返回 0。领域对象应统一 scale 或封装比较/哈希规则，不能把 BigDecimal 直接当作无约束的实体值。

### Q4：银行家舍入是什么？负数 `-1.5` 结果是多少？

**答**：HALF_EVEN 只在恰好一半时取最近偶数。`1.5 -> 2`，`2.5 -> 2`，`-1.5 -> -2`，`-2.5 -> -2`。它不是“所有结果变偶数”，也不是简单的 `HALF_UP`；正负数必须用库的明确 rounding mode 测试。

### Q5：为什么逐行四舍五入再汇总会差 1 分？

**答**：舍入不是线性运算，`round(a)+round(b)` 不一定等于 `round(a+b)`。业务必须规定是行级量化还是总额级量化；若总额确定而明细要守恒，用最大余数法把剩余最小单位按最大余数和稳定 key 分配。

### Q6：最大余数法怎么保证确定性？

**答**：对 `raw_i=T*w_i` 做整数商和余数，先分 `floor(raw_i/W)`，剩余单位按 `remainder DESC` 排序；余数相同再按固定 priority、不可变 allocation key 决胜。绝不使用浮点 remainder、输入位置或无 `ORDER BY` 的数据库返回顺序。

### Q7：退款能直接把正数分摊结果乘以 -1 吗？

**答**：全额退款可以按同一正数分配结果逐项取反，最稳定；部分退款要定义是按原比例重新分配还是按指定对象。不要把负数直接塞进只为非负数证明过的整除/最大余数实现，因为不同语言的负数除法和余数规则会造成差异。

### Q8：多级手续费最容易错在哪里？

**答**：税基和舍入边界不明确。例如税是基于本金、单项费用还是已舍入费用合计；最低收费是在舍入前还是后。每一级应保存 base、rate、exact value、rounded output 和规则版本，不能靠组合费率或变量名猜语义。

### Q9：如何保证 Go 和 Java 结果一致？

**答**：定义跨语言 golden vectors，金额和费率以字符串/整数表达；固定 scale、rounding mode、排序 key、规则/算法版本。Go 使用 decimal 或宽整数，Java 使用 BigDecimal/BigInteger；CI 同时运行两套测试，任何差异阻断发布。

### Q10：为什么“结果正确”还不够？

**答**：金融系统还要可追溯和可修正。必须保留原始输入、规则版本、每个中间步骤、舍入模式、尾差排名和幂等键；已入账结果不能覆盖，错误通过冲正产生新事实。这样审计者才能复算，重试才能收敛。

---

## 20.17 三道自检题

### 题 1：费率与负数舍入

订单为 CNY 10.00，即 1,000 分；费率为 `0.05% = 5/10,000`。分别使用 `HALF_EVEN` 和 `HALF_UP` 计算平台费。随后做全额退款，要求退款逐项抵消原收费。结果各是多少？

**答案**：

```text
精确费用 = 1,000 × 5 / 10,000 = 0.5 分
HALF_EVEN：0.5 在 0 与 1 间，取偶数 0 -> 收费 0 分
HALF_UP：0.5 远离零 -> 收费 1 分
```

全额退款分别为 `0 分` 和 `-1 分`。不能因为退款是负数就再次用另一种模式；退款应引用原收费规则/结果，逐项取反。

### 题 2：最大余数法

将 101 分按权重 A=2、B=3、C=5 分配，三者 priority 都是 0，key 顺序 A < B < C。求每一步和最终结果。

**答案**：

```text
W = 2 + 3 + 5 = 10
A raw = 101×2 = 202，base=20，remainder=2
B raw = 101×3 = 303，base=30，remainder=3
C raw = 101×5 = 505，base=50，remainder=5
base sum = 100，left = 101 - 100 = 1
最大余数是 C，因此 C + 1
最终 A=20，B=30，C=51；合计 101
```

### 题 3：多级费用顺序

本金 10,001 分，平台费 1%，渠道费 0.6%，税率 6%；平台费和渠道费先各自按 `HALF_EVEN` 舍入到分，税基是两个已舍入费用之和。求总费用和消费者总扣款。

**答案**：

```text
平台费 = 10,001×100/10,000 = 100.01 -> 100 分
渠道费 = 10,001×60 /10,000 =  60.006 -> 60 分
税基 = 100 + 60 = 160 分
税 = 160×600/10,000 = 9.6 -> 10 分
费用合计 = 170 分
总扣款 = 10,001 + 170 = 10,171 分
```

如果税基改成未舍入的精确费用，必须作为另一条规则版本单独计算和测试，不能与本答案混用。

---

## 20.18 与后续章节衔接

本章解决的是“金额如何成为确定性的工程事实”，但它还不是完整账本。后续章节按以下边界衔接：

1. **→ 21 时间、交易日历与流水号**：计算结果需要 `occurred_at_utc`、会计日、结算批次和不可变流水号；金额规则版本与时间规则版本一起进入审计快照。日切不能改变已量化金额，跨日冲正必须产生新事实。
2. **→ 22 复式记账核心账本**：`rounded_amount_minor` 进入借贷分录；资产、负债、收入和费用科目必须满足借贷平衡。余额是分录的物化视图，不能用本章结果直接 `UPDATE balance`。
3. **→ 24 全链路幂等**：`idempotency_key` 防止支付回执、手续费计算和账本入账重复执行；超时是 `UNKNOWN`，不能把“计算已写库但响应丢失”当成失败再次生成不同尾差。
4. **→ 26 清结算与对账**：清算批次按本章的规则版本和算法版本固定快照；渠道总额、商户明细、平台费用、税额必须分别对账，差异进入差错科目而不是被下一批尾差吞掉。
5. **→ 28 风控与限额**：限额计算同样使用最小单位和确定性舍入；风险阈值比较前应先统一币种、报价时间和 scale，禁止用展示字符串或二进制浮点比较。
6. **→ 35 AI-Agent 安全隔离**：Agent 只能提出金额意图，不能决定舍入模式、尾差受益人或直接调用入账工具；金额计算、规则选择、守恒校验和幂等提交必须由确定性代码执行。

**本章收束句**：金额系统的质量不是“多数时候显示两位小数”，而是每一个最小单位都有来源、规则、归属和可复算路径；当总额无法整除时，尾差也必须按公开、稳定、守恒的算法落到一个明确对象上。**一分钱不是噪声，而是系统对业务规则是否诚实的证据。**
