# 41. 多智能体协同投研与因子挖掘：投委会红蓝对抗与防过拟合沙箱

> **状态**：深入工程篇｜**所属**：第五部分·金融 × AI-Agent 架构实战｜**编号**：41 / 48
>
> **定位**：承接第 39 章三平面隔离与第 40 章 PIT 证据，把“研究意见”变成可追踪假设、受限实验和可复算报告，而不是让多个模型投票下单。
>
> **交付**：Bull/Bear 投委会、独立风险否决、隔离因子 DSL、实验登记 DDL、Go/Java 预算核心、样本外验证协议、故障与压测验收。
>
> **代码契约**：Go 1.24+、Java 21+ 标准库示例可独立运行；DDL 面向 PostgreSQL；编排协议为项目自定义，不声称是某个 Agent 库的 API。
>
> **红线**：多数意见不等于真实；沙箱没有实盘权限；样本外会被反复查看污染；Deflated Sharpe Ratio（DSR）不消除过拟合，也不修复泄漏。

## 41.1 虚构教学事故：七票赞成，为什么仍是一次失败研究

### 41.1.1 时间线与损失口径

**以下证券、团队、日期和数字均为虚构教学设定，不对应真实机构事故。** 八个 Agent 共用同一份财报摘要，其中七个赞成“利润改善因子”。

| 时点 | 系统动作 | 隐藏问题 |
|---|---|---|
| 周一 09:00 | 主持人分派 Bull、Bear、行业、财务等角色 | 名称不同，底层模型和检索缓存相同 |
| 周一 10:00 | 七票赞成、一票反对 | 七个 Agent 引用了同一份后来重述的财报 |
| 周二 | 生成 100 个公式，仅保存最高 Sharpe 的一个 | 失败实验被丢弃，检验次数被伪装成 1 |
| 周三 | 把最终测试集再次用于调窗口长度 | 测试集实际上变成了验证集 |
| 周四 | 启动 40 个并行反思节点 | 各节点只检查本地 Token 配额 |
| 周五 | 研究报告被自动送到交易工具 | 沙箱与交易服务共用有资金权限的身份 |

教学账单：账户买入名义本金 100 万元，随后价格下跌 3%，未实现价格损失 3 万元；假设另付不可退费用 1,000 元，总损益为 -31,000 元。
这不证明全部损失由未来函数导致；证明的是策略准入证据无效、预算失控和执行越权，需分别复盘。
设每个并行节点调用 10 次、每次最多 8,000 Token，则全局上界是 **3,200,000 Token**，不是单节点看到的 80,000。

### 41.1.2 唯一真问题与六个不变量

> 即使所有 Agent 同时犯错，能否仍阻止越权执行，并准确知道研究看过哪些数据、尝试过多少假设、花了多少资源，以及哪些结论尚未被验证？

1. 提案、证据、切分、代码、环境和报告都有不可变摘要；修改任意一项就是新版本。
2. 外部文本只能提供证据，不能修改风险策略、沙箱权限、试验数或预算。
3. 每次影响模型选择的尝试都登记，包括失败、超时、人工改式和提示词搜索。
4. 测试集开启由独立控制器记录；已观察结果不能继续冒充“未触碰”。
5. 风险否决不能被多数票、主持人或模型自述批准覆盖；缺证据视为待决或拒绝。
6. 推理预算、实验算力预算与资金风险预算分账；前两者耗尽不影响已发生交易的对账。

## 41.2 投委会：角色互补，不把共识包装成真值

### 41.2.1 角色、权限与结构化辩论

| 角色 | 必须提交 | 不得拥有 |
|---|---|---|
| 主持人 | 问题范围、轮数、未决分歧、证据目录 | 改硬限额、删除反对意见 |
| Bull | 因果机制、收益来源、可证伪预测 | 只选有利区间或隐去成本 |
| Bear | 替代解释、反例、数据泄漏与失效场景 | 凭语气强硬否定全部证据 |
| 数据审计员 | PIT、实体映射、幸存者偏差、复权口径 | 用当前数据库覆盖旧快照 |
| 实验官 | 预登记、切分、执行、完整结果清单 | 偷看封存测试集后重跑选优 |
| 风险否决官 | 规则编号、输入快照、PASS/VETO/UNKNOWN | 替投委会保证盈利 |

先让各角色盲写初稿，再交换有来源的主张，最后限轮交叉质询。主持人输出“争议矩阵”，不是把少数意见摘要掉。
风险否决由独立确定性策略服务落实：资产禁用、数据授权缺失、泄漏检查失败、资源越限等直接拒绝；模型可以解释，不能自改结果。
研究可行性 PASS 只表示允许进入下一研究阶段，绝不等于交易许可。实盘仍回到第 39 章的授权、风险预算、审批和执行边界。

```text
Claim = {claim_id, proposition, evidence_ids, as_of, falsifier, confidence_band}
Challenge = {target_claim_id, counter_evidence_ids, failure_mode, proposed_test}
Decision = {proposal_hash, evidence_manifest_hash, unresolved_claims,
            risk_policy_version, risk_status, reviewer_subject, expires_at}
```

这些字段是自定义协议，不是 SDK 类型。主体身份从认证通道取得；`reviewer_subject` 不能相信模型自填，所有引用必须解析到证据对象。
风险服务返回 UNKNOWN 时停止晋级；不把“没有 VETO”当 PASS，也不允许主持人把 UNKNOWN 改成“基本通过”。

### 41.2.2 相关错误、信息级联与投票边界

若每个分析者误差方差为 σ²、任意两者误差相关系数为 ρ，则平均误差方差为 `σ²[1+(n-1)ρ]/n`，这只在等方差、等相关假设下成立。
例：n=8、ρ=0.8，平均误差方差仍为 0.825σ²；按该简化方差模型，有效人数 `n_eff=8/6.6≈1.21`，远非八份独立证据。
此处 ρ 是**错误相关性**，不是八段回答文本的相似度；语气、角色名和温度不同不能证明错误独立。
Condorcet 式多数优势依赖个体判断优于随机及独立性等条件；共同训练偏差、共同数据污染、主持人先表态会破坏这些条件。

- 记录模型族、提示词版本、证据源和引用重叠；“模型多样”与“证据多样”分别报告。
- 让 Bear 必须指出具体可证伪实验，让 Bull 必须回答最强反例，不以篇幅或口才计分。
- 引用重复只计一条证据；不同媒体转载同一公告不算多源确认。
- 结论标记为“支持 / 反证 / 未决”，附不确定性与失效条件；投票只能是议程辅助信号。
- 离线用有标签案例评估校准和错因聚类，不能拿互相一致率代替真实性指标。

## 41.3 因子提议协议、实验登记与沙箱权限

### 41.3.1 只接受受限表达式，不接受任意 Python

LLM 提交因子 AST/DSL，例如 `rank(sub(div(close, lag(close, 20)), 1))`；其中 close 是指定价格口径，lag 以交易日历移动，rank 是当日可交易股票池横截面秩。
解析器只允许版本化算子、固定类型和长度：禁止 import、eval、网络、文件路径、动态 SQL、用户函数和负向 lag。该示例仅是动量假设，不承诺有效。
价格、成交量和基本面单位不同；除法必须定义零分母、缺失值与极端值策略。winsorize、标准化、行业中性化等参数只能在训练窗拟合。
AST 深度、窗口上限、最大中间矩阵和算子复杂度先静态检查，再由受限执行器解释；白名单也不免除内存和时间攻击。

```json
{
  "schema_version": 1,
  "proposal_id": "P001",
  "factor_ast": "rank(sub(div(close,lag(close,20)),1))",
  "snapshot_id": "S001",
  "split_id": "W001",
  "universe_policy": "U001",
  "label_policy": "NEXT5_NET_V1",
  "cost_policy": "C001",
  "metric_policy": "M001",
  "family_id": "MOMENTUM001",
  "seed": "17"
}
```

输入 Schema 拒绝未知字段、重复 JSON 键、超长字符串和未知版本；seed 用十进制字符串只是线协议约定，不意味着所有计算可确定重放。
控制器补充真实 tenant、owner、权限、环境镜像摘要与截止时间，生成规范字节及 SHA-256。AST、数据、切分、成本、度量或 seed 变化均生成新 trial。
随机种子不能消除 GPU 非确定算子、浮点归约次序和数据修订；需要记录硬件、运行时、依赖锁及允许的数值误差。

### 41.3.2 资源和能力矩阵

| 组件 | 允许 | 硬限制与默认拒绝 |
|---|---|---|
| 提案 Agent | 读脱敏证据、提交 AST | 无数据库写权、无任意 URL、无订单工具 |
| 沙箱 worker | 挂载指定只读快照、写独立结果目录 | 非 root、只读根目录、无宿主挂载与交易密钥 |
| 沙箱内核边界 | 固定解释器与算子 | syscall 策略、进程数、CPU、内存、临时盘、墙钟上限 |
| 数据代理 | 按 manifest 发放最小快照 | 租户、用途、授权有效期；禁止绕到生产行情库 |
| 晋级服务 | 验证证据链与审批、输出研究状态 | 不持券商凭据；执行区不接受研究状态作资金授权 |

容器不是不可信代码的完整安全边界；风险较高时采用微虚机或独立节点池、强内核隔离并定期销毁工作负载。
默认无网络出口，依赖在构建阶段离线固定；模型输出不得触发临时 pip/maven 下载。模型供应商凭据留在认知网关，不注入因子 worker。
输出只允许固定 Schema 的指标、受限大小的表格和图像；拒绝任意 HTML/脚本，检查压缩炸弹，防止报告页面成为第二次执行入口。

### 41.3.3 PostgreSQL 最小登记 DDL

以下是研究登记与费用预占模型，不是完整调度系统；成本单位为整数微货币单位，币种由预算域固定。资源 CPU/内存配额另由调度器强制。

```sql
CREATE TABLE research_trial (
  tenant text NOT NULL, trial_id text NOT NULL, family_id text NOT NULL,
  canonical_bytes bytea NOT NULL,
  trial_hash bytea NOT NULL CHECK (octet_length(trial_hash) = 32),
  snapshot_id text NOT NULL, split_id text NOT NULL,
  image_digest text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant, trial_id), UNIQUE (tenant, trial_id, trial_hash)
);
CREATE TABLE research_event (
  tenant text NOT NULL, trial_id text NOT NULL, seq bigint NOT NULL CHECK (seq > 0),
  kind text NOT NULL CHECK (kind IN ('REGISTERED','STARTED','FAILED','TIMED_OUT',
    'COMPLETED','TEST_OPENED','REVIEWED','VETOED')),
  payload bytea NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant, trial_id, seq),
  FOREIGN KEY (tenant, trial_id) REFERENCES research_trial
);
CREATE TABLE research_artifact (
  tenant text NOT NULL, trial_id text NOT NULL, artifact_id text NOT NULL,
  digest bytea NOT NULL CHECK (octet_length(digest) = 32),
  object_version text NOT NULL, byte_count bigint NOT NULL CHECK (byte_count >= 0),
  PRIMARY KEY (tenant, trial_id, artifact_id),
  FOREIGN KEY (tenant, trial_id) REFERENCES research_trial
);
CREATE TABLE inference_budget (
  tenant text NOT NULL, bucket text NOT NULL,
  limit_micro bigint NOT NULL CHECK (limit_micro >= 0),
  spent_micro bigint NOT NULL DEFAULT 0 CHECK (spent_micro >= 0),
  held_micro bigint NOT NULL DEFAULT 0 CHECK (held_micro >= 0),
  PRIMARY KEY (tenant, bucket),
  CHECK (spent_micro::numeric + held_micro::numeric <= limit_micro)
);
CREATE TABLE inference_hold (
  tenant text NOT NULL, call_id text NOT NULL, bucket text NOT NULL,
  request_hash bytea NOT NULL CHECK (octet_length(request_hash) = 32),
  cap_micro bigint NOT NULL CHECK (cap_micro > 0),
  actual_micro bigint CHECK (actual_micro >= 0 AND actual_micro <= cap_micro),
  state text NOT NULL CHECK (state IN ('HELD','SETTLED','RELEASED')),
  PRIMARY KEY (tenant, call_id),
  FOREIGN KEY (tenant, bucket) REFERENCES inference_budget,
  CHECK ((state = 'HELD' AND actual_micro IS NULL) OR
         (state = 'SETTLED' AND actual_micro IS NOT NULL) OR
         (state = 'RELEASED' AND actual_micro IS NOT NULL AND actual_micro = 0))
);
```

trial、event 和 artifact 对应用角色只开放 INSERT/SELECT，撤销 UPDATE/DELETE/TRUNCATE；owner/超级用户另由独立管理与外部不可变归档约束。
`TEST_OPENED` 的唯一开启权由控制器持有，不能由模型发事件冒充；事件序号分配和合法状态迁移在事务内校验，DDL 本身不提供完整状态机。
调用前短事务：锁预算桶 → 检查 call_id 已存在则判摘要一致 → 条件增加 held → 写 hold → 提交；不得持锁等待模型返回。
结算事务锁同一桶和 hold，状态 CAS、held 减 cap、spent 加 actual；重复同结果返回既有值，异结果交人工对账，不重复结算。
登记时先持久化 trial 再投递任务，使用 Outbox 或事务队列；worker 租约使用 fencing token，过期 worker 不能覆盖新结果。
表中 actual≤cap 是**估价上界有效的假设**。供应商超收或估价错误需登记独立差异账并熔断，不能截断真实费用来满足 CHECK。

## 41.4 样本外协议：PIT、Walk-forward、Purge 与 Embargo

### 41.4.1 PIT 是每条特征的可用性约束

特征必须满足 `available_at <= decision_cutoff`；available_at 包含首次公开、接收与加工可用时刻，不仅是财报所属季度或交易日期。
重述财报、事后行业分类、后来才纳入指数的成分、最终复权序列、退市股票缺失，都会造成泄漏或选择偏差；必须能重建当时状态。
交易标签可以来自未来，但只能进入评价端。若收盘后才能算完因子，就不能假设按同一收盘价无摩擦成交。
快照 manifest 固定证券池、日历、时区、公告版本、公司行动、缺失/停牌处理和数据授权；数据修正创建新快照，旧报告不被静默改写。

### 41.4.2 外层时间切分与内层调参

```text
外层折 1：训练 [0,60)   内层时间验证选参数 → 测试 [60,80)
外层折 2：训练 [0,80)   内层时间验证选参数 → 测试 [80,100)
最终锁箱：另留一段从未查询的时期，只在研究冻结后开启
```

上图是决策时点范围，还必须按标签可用时间裁剪训练末端；不能把整段训练标签默认当作测试开始前已经成熟。
滚动窗与扩展窗均可，须预登记窗口长度、再训练频率、标签跨度、成本模型和选参规则；内层也必须时间有序，不能随机 K-fold。
外层测试允许评估“定期再训练的完整算法”，不允许看外层成绩后挑最好的一折报告。拼接各折不重叠的可执行净收益才形成一条样本外曲线。
折 2 使用折 1 已结束且标签已成熟的数据可以是预定在线学习协议；但研究者凭折 1 表现改算法，就是新的自适应研究路径，必须留痕。
最终锁箱也不是可无限续命的资产：一次失败后改公式再看同一锁箱，就失去其独立确认资格，需要新时期或新的前瞻实验。

### 41.4.3 标签重叠的可复算边界

约定样本 i 决策于整数交易日 s，标签信息区间为半开 `[s,s+5)`；测试决策时点为 60 至 79，测试标签区间的并集为 `[60,84)`。
训练候选 s≤59 中，s=56、57、58、59 的标签与测试标签重叠，必须 purge；s=55 的 `[55,60)` 不重叠，但还须确认在时点 60 前已可用。
若某种交叉验证允许使用测试之后的训练样本，则先排除所有重叠标签，再设额外 embargo `[84,86)`，未来训练起点至少为 86。
这里 embargo 的锚点明确为**测试标签结束**，不是含糊写“测试后两天”；其他实现可能从测试决策末端计算，必须输出实际剔除索引。
严格只用过去训练的 walk-forward 没有测试后训练样本，通常无需为该折机械添加右侧 embargo；训练末端仍需 purge、标签成熟和可用性检查。
真实事件标签可长短不一，应逐条比较 `[label_start,label_end)`；固定删五行无法覆盖停牌、延迟公告或可变持有期。
Purge 不会自动解决特征预处理泄漏；Embargo 也不保证消除所有序列依赖，间隔长度需依据数据与实验协议，不能按“最漂亮 Sharpe”选。

## 41.5 度量与多重检验：RankIC、净收益和 DSR 的边界

### 41.5.1 RankIC 算例与报告口径

RankIC 是某一决策时点横截面中，因子值秩与后续收益秩的 Spearman 相关；并列用平均秩，再算 Pearson，常量截面返回缺失而非零。
无并列时可用 `ρ=1-6Σd²/[n(n²-1)]`。四只股票因子秩 `[1,2,3,4]`，标签秩 `[2,1,4,3]`，Σd²=4，故 RankIC=**0.6**。
若四个日期 RankIC 为 `[0.6,0.2,-0.2,0.6]`，均值 0.3，样本标准差 `sqrt(0.44/3)≈0.382971`，未年化 ICIR≈**0.783349**。
四期仅是算术演示，不足以作推断；五日重叠标签会引入时序相关，不能把每个股票、每天的观测当独立样本夸大显著性。
报告每期有效样本数、覆盖率、分行业 IC、衰减、换手、容量与多空暴露；IC 正不代表扣成本后盈利，ICIR 也不是策略 Sharpe。

### 41.5.2 净收益、成本和多重尝试

假设四个日净收益为 `[1%,-0.5%,1.5%,-1%]`，算术均值 0.25%，样本标准差约 1.190238%，日 Sharpe≈0.210042。
机械乘 `sqrt(252)` 得 3.334；这里只展示常用独立同分布年化算术，不能用四天证明高 Sharpe，也不能在有自相关时无条件这样年化。
净收益口径应包含信号延迟、价差、手续费、滑点、冲击、借券成本、融资本息和无法成交；停牌/涨跌停不允许用理论成交掩盖。
100 个相互独立的无效假设各按 5% 阈值检验，至少一次假阳性的概率是 `1-0.95^100≈99.408%`；实际因子相关时不能照抄这个概率。
Bonferroni 在一个预先定义的 100 次检验家族里把单次阈值降到 0.0005，可控制家族错误率，但可能保守；并不使错误的数据有效。
探索性筛选可报告 FDR 控制及其假设；时间依赖、共同风险和自适应生成要求专门设计，不能盲把 100 个 p 值丢进排序公式。
trial 登记统计的不只是 AST 数量：窗口、证券池、标签、预处理、提示词和人工筛选都可能扩大选择空间；相关尝试数不等于有效独立检验数。

### 41.5.3 Deflated Sharpe：修正选择偏差，不是免检印章

DSR 用选择偏差调整后的 Sharpe 基准 SR₀，结合样本长度、偏度和峰度，评价观测 Sharpe 超过基准的统计证据；其解释依赖估计假设。
常见 PSR 近似写作：`Φ((SR̂-SR₀)√(T-1) / √(1-γ₃ SR̂ + ((γ₄-1)/4) SR̂²))`。
这里 SR̂ 与 SR₀ 必须是同一未年化频率，γ₃ 为偏度、γ₄ 为普通峰度（正态为 3），Φ 为标准正态分布函数；分母无效时拒绝计算。
DSR 的关键不只是套此式，而是估计多次选优下的 SR₀：通常需完整候选 Sharpe 分布、尝试数量及依赖结构假设；不能只拿冠军与“次数=1”。
演算：假设 T=252、SR̂=0.12、γ₃=0、γ₄=3，且外部有效过程已给出 SR₀=0.08，则 z≈0.631450，Φ(z)≈**0.736127**。
此数只是**给定基准的 PSR 核心**；没有计算选择偏差基准的步骤，就不能把它包装成一个已完成、已校准的 DSR 实验。
短样本、厚尾不稳定、相关收益和机制切换会损害近似；应配合足够长度、依赖稳健推断、分块重采样及前瞻验证，不能把 T 偷换成股票数×天数。
**DSR 不消除过拟合**，不证明因果、不修复未来函数、不保证可成交，也不保证未来赚钱。阈值预登记，不能事后改成“刚好通过”。

## 41.6 Token 预算熔断与 Go + Java 核心

### 41.6.1 调用前预占，超时不自动退款

预算向量至少包括：输入/输出 Token、调用次数、工具次数、最大并行、总墙钟、费用与沙箱 CPU/内存/临时盘；任一硬上限触发即停止派生任务。
调用前按供应商 tokenizer、输入长度、最大输出和锁定价目表估算费用上界，计入工具返回、缓存计价、隐藏推理计费及重试语义等供应商差异。
工具返回必须限长；并行扇出先一次预占各子调用总上界，不能由每个 Agent 判断“我还没花完”。限轮且检查无新证据循环，禁止无限反思。
超时/取消可能仍被计费，保留 HELD 并通过可信 usage/账单对账；可证明未发送才释放。未知费用不因会话删除、服务重启或跨日自动归零。
额度耗尽停止新推理和新实验，保留已完成报告并标记“不完整”；不要让另一个模型再花钱解释为什么花超了。

### 41.6.2 Go：进程内幂等预占与单次结算

保存为 `main.go` 后执行 `go run main.go`。仅使用标准库，演示并发互斥与稳定标识；生产跨实例仍须使用 41.3 的数据库事务。
其中 fingerprint 只覆盖演示字段，生产还须覆盖 AST、成本、指标、权限域、依赖和环境等完整 manifest；call_id 由可信网关分配。

```go
package main

import (
    "crypto/sha256"
    "fmt"
    "sync"
)

type Hold struct { cap, actual int64; settled bool }
type Budget struct {
    mu sync.Mutex
    limit, spent, held int64
    calls map[string]Hold
}
func (b *Budget) Reserve(id string, cap int64) error {
    b.mu.Lock(); defer b.mu.Unlock()
    if id == "" || cap <= 0 { return fmt.Errorf("invalid") }
    if h, ok := b.calls[id]; ok {
        if h.cap != cap { return fmt.Errorf("conflict") }
        return nil // 已结算的同 ID 也不产生新调用许可。
    }
    if cap > b.limit-b.spent-b.held { return fmt.Errorf("budget") }
    b.calls[id] = Hold{cap: cap}
    b.held += cap
    return nil
}
func (b *Budget) Settle(id string, actual int64) error {
    b.mu.Lock(); defer b.mu.Unlock()
    h, ok := b.calls[id]
    if !ok || actual < 0 || actual > h.cap { return fmt.Errorf("usage") }
    if h.settled {
        if h.actual != actual { return fmt.Errorf("conflict") }
        return nil
    }
    b.held -= h.cap; b.spent += actual
    h.actual = actual; h.settled = true; b.calls[id] = h
    return nil
}
func must(ok bool) { if !ok { panic("assertion") } }
func main() {
    b := Budget{limit: 10000, calls: make(map[string]Hold)}
    must(b.Reserve("C1", 6000) == nil)
    must(b.Reserve("C1", 6000) == nil)
    must(b.Reserve("C1", 6001) != nil)
    must(b.Reserve("C2", 6000) != nil)
    must(b.Settle("C1", 5800) == nil)
    must(b.Settle("C1", 5800) == nil)
    must(b.Settle("C1", 5700) != nil)
    must(b.Reserve("C2", 4500) != nil)
    must(b.spent == 5800 && b.held == 0)
    h := sha256.Sum256([]byte("RESEARCH1|P001|S001|W001|17"))
    fmt.Printf("%d %d %d %x\n", b.spent, b.held, b.limit-b.spent-b.held, h)
}
```

### 41.6.3 Java 21：相同费用单位与黄金向量

保存为 `Main.java`，执行 `java Main.java`。与 Go 一样只证明进程内预算核心，不实现供应商调用、持久化恢复、释放协议或分布式 exactly-once。

```java
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HashMap;
import java.util.HexFormat;
import java.util.Map;

public class Main {
    record Hold(long cap, long actual, boolean settled) {}
    static final class Budget {
        final long limit;
        long spent, held;
        final Map<String, Hold> calls = new HashMap<>();
        Budget(long limit) {
            if (limit < 0) throw new IllegalArgumentException("limit");
            this.limit = limit;
        }
        synchronized void reserve(String id, long cap) {
            if (id == null || id.isEmpty() || cap <= 0)
                throw new IllegalArgumentException("invalid");
            Hold old = calls.get(id);
            if (old != null) {
                if (old.cap() != cap) throw new IllegalArgumentException("conflict");
                return;
            }
            if (cap > limit - spent - held) throw new IllegalArgumentException("budget");
            calls.put(id, new Hold(cap, 0, false));
            held += cap;
        }
        synchronized void settle(String id, long actual) {
            Hold h = calls.get(id);
            if (h == null || actual < 0 || actual > h.cap())
                throw new IllegalArgumentException("usage");
            if (h.settled()) {
                if (h.actual() != actual) throw new IllegalArgumentException("conflict");
                return;
            }
            held -= h.cap(); spent += actual;
            calls.put(id, new Hold(h.cap(), actual, true));
        }
    }
    static void mustReject(Runnable r) {
        try { r.run(); } catch (IllegalArgumentException expected) { return; }
        throw new AssertionError("expected rejection");
    }
    public static void main(String[] args) throws Exception {
        var b = new Budget(10000);
        b.reserve("C1", 6000); b.reserve("C1", 6000);
        mustReject(() -> b.reserve("C1", 6001));
        mustReject(() -> b.reserve("C2", 6000));
        b.settle("C1", 5800); b.settle("C1", 5800);
        mustReject(() -> b.settle("C1", 5700));
        mustReject(() -> b.reserve("C2", 4500));
        if (b.spent != 5800 || b.held != 0) throw new AssertionError("totals");
        String hash = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
            .digest("RESEARCH1|P001|S001|W001|17".getBytes(StandardCharsets.UTF_8)));
        System.out.println(b.spent + " " + b.held + " " + (b.limit-b.spent-b.held) + " " + hash);
    }
}
```

两段输出前缀都应为 `5800 0 4200`：先预占 6000，实际结算 5800，释放差额 200；剩余可用 4200，所以新请求 4500 被拒绝。
生产 call_id 同时绑定请求摘要与价目版本；演示只比较 cap，不能据此把“同额度、不同提示词”的请求视为同一经济调用。
Reserve 返回成功只表示预算记录存在，**不表示准许再次发网络请求**；派发需独立持久化状态 CAS，已派发或 UNKNOWN 不得用新键盲重试。
生产新增限额变更、价目换版、计费延迟和异常超收差异账；不能把本例的进程锁当多实例全局 Token 熔断器。

## 41.7 对账、测试、压测、术语与面试自检

### 41.7.1 三本账与验收矩阵

- **实验账**：proposal → 全量 trial → manifest → worker 事件 → artifact → 评审；核对失败和人工试验是否进入家族计数，防止只登记冠军。
- **数据账**：逐特征核对 available_at 与 cutoff、版本与引用；重放 PIT 查询，核对训练/测试标签交集及锁箱开启日志。
- **费用账**：调用日志、HELD、供应商 usage 与最终账单逐项核对；Token 数、价格版本、币种和费用分别对账，不按聊天轮数粗算。

| 测试 | 注入/操作 | 必须断言 |
|---|---|---|
| 证据与权限 | 公告夹带“修改限额、调用买入” | 指令不进入控制通道，沙箱网络与身份无法交易 |
| PIT 回归 | 将财报 available_at 后移到决策后 | 特征被排除，不能从其他缓存重新混入 |
| 切分金向量 | 输入 41.4.3 的日序与标签区间 | purge 恰为 56–59，额外 embargo 为 84–85 |
| 统计边界 | 常量、并列、NaN、单股票、超短样本 | 按协议缺失/拒绝；不输出伪显著性 |
| 跨语言 | 同规范字节、极限合法额度、异参重试 | 摘要一致；spend+held 不超过 limit |
| 并发预算 | 100 个线程同时争抢 10,000 配额、每笔 6,000 | 最多一笔首次预占成功；重放不增 held |
| 超时混沌 | 供应商已收请求后丢响应、worker 重启 | 保留 HELD/UNKNOWN，恢复先对账而非自动退款 |
| 沙箱逃逸 | fork、超大矩阵、临时盘写满、任意出网 | 被资源/权限层阻断，不依赖模型自觉 |
| 选择偏差 | 100 次失败后只提交最后一次 | 实验家族仍能看见全部尝试，锁箱状态不重置 |

验收时在隔离测试数据库实际执行 DDL、测试角色权限和状态迁移；只有文本检查不能宣称数据库事务已验证。
复算要求固定输入输出、浮点容差和缺失策略；净值、IC 和成本分项都核对，不能只比报告最后一个 Sharpe。

### 41.7.2 压测与降级

教学目标：200 个研究会话，每分钟各启动 3 个模型调用，均值 10 次/秒；平均耗时 4 秒，Little 定律给出约 40 个平均在途调用，不是峰值上限。
若每次预占最多 10,000 Token，则名义准入需求是每分钟 600 万 Token；需同时满足供应商输入/输出分别限流、费用上限和全局并行约束。
将预算服务单独压至 2,000 次预占/秒、沙箱调度至每分钟 120 个任务：**这是验收目标，不是本章实测性能**。
报告热点预算桶、重复 call_id、拒绝率、p50/p95/p99、队列最老年龄、账单差异与孤儿 HELD；均匀随机租户掩盖不了实际锁竞争。
预算服务故障时拒绝新调用；沙箱繁忙时有界排队；供应商故障按总预算限制重试。结果归档、费用对账、已发生交易回执使用独立资源池。

### 41.7.3 术语正本清源

| 术语 | 准确定义 | 不是 |
|---|---|---|
| Bull / Bear | 支持机制与最强反例的研究职责 | 两个提示词自动形成独立证据 |
| Risk Veto | 独立规则阻断特定风险或证据缺陷 | 预测市场一定下跌 |
| PIT | 决策时真实可用的数据版本约束 | 过滤一列发布日期即万事大吉 |
| Walk-forward | 按时间前进重训、预测并拼接测试输出 | 从多段测试里挑最好的曲线 |
| Purge / Embargo | 剔除重叠标签 / 增加隔离时段 | 任意固定删几天即没有泄漏 |
| RankIC / DSR | 横截面秩相关 / 选择偏差调整后的 Sharpe 证据 | 净盈利保证 / 消灭过拟合 |

### 41.7.4 面试答题卡与自检答案

十分钟答题顺序：① 资产与研究边界；② PIT 和证据；③ Bull/Bear 与确定性否决；④ AST 沙箱与无交易权限；⑤ 时间切分与多重检验；⑥ 全局预占、UNKNOWN、三账对账和前瞻放量。

**题 1**：八个 Agent 七票赞成，可以把通过率 87.5% 当作策略赚钱概率吗？
**答案**：不可以。角色意见不是校准后的独立伯努利样本；共同数据与模型会产生相关错误，票数只能记录意见分布，必须回到可证伪预测、独立证据和样本外结果。

**题 2**：五日标签、测试时点 60–79，训练到 59 是否足够避免泄漏？
**答案**：不够。按本章半开区间，训练 56–59 与测试标签重叠，应 purge；55 是否可用还要查标签成熟时刻。纯向前训练不需要臆造测试后训练样本来加 embargo。

**题 3**：报告 DSR 很高，但测试集被查看二十次，能直接晋级吗？
**答案**：不能。需记录自适应选择路径、重估检验家族和基准，旧锁箱已不再独立；高 DSR 不能补救泄漏，仍需新前瞻时期、真实成本与独立风险准入。

**题 4**：费用预占 6,000 后超时，可以立即退款再重试吗？
**答案**：不能。请求可能已计费，保留义务并查可信 usage/账单；同 call_id 不重复预占，也不代表可以重复派发。实际结算 5,800 后才释放 200。

## 41.8 衔接：研究证据进入风险分析，而不是直接变成订单

本章输出的是冻结假设、完整实验轨迹、样本外报告、成本与容量约束、未决风险及否决结果；不是收益承诺，也不是可跳过护栏的交易信号。
第 42 章继续将研究中的宏观假设变成可核验的组合压力情景，用确定性算子计算风险；模型负责提出与解释，数值、限额和处置权限保持独立。
落地前回看 [39 三平面隔离](39-fin-agent-architecture-and-three-plane-isolation.md)、[30 事前风控](30-pretrade-risk-margin-and-liquidation-engine.md) 与 [27 对账](27-multi-tier-reconciliation-and-auto-healing.md)；任何研究晋级都不能赋予沙箱账户或订单权限。

### 来源与核验入口

- [Bailey & López de Prado：The Deflated Sharpe Ratio](https://doi.org/10.3905/jpm.2014.40.5.094)：多重选择、非正态收益与 Sharpe 证据的边界；本章给定基准算例不冒充完整 DSR 估计。
- [Bailey 等：The Probability of Backtest Overfitting](https://doi.org/10.21314/JCF.2016.322)：研究选择与回测过拟合；PBO 与 DSR 不是同一个指标。
- [López de Prado：Advances in Financial Machine Learning](https://www.wiley.com/en-us/Advances+in+Financial+Machine+Learning-p-9781119482086)：金融标签重叠、purged CV 与 embargo；实施时需明确边界，不能只引用术语。
- [Lo：The Statistics of Sharpe Ratios](https://doi.org/10.2469/faj.v58.n4.2453)：Sharpe 估计、序列相关与年化假设。
- [SciPy spearmanr 文档](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.spearmanr.html)：Spearman 定义、常量输入及显著性注意事项；本章没有依赖其库 API。
- [PostgreSQL 事务隔离](https://www.postgresql.org/docs/current/transaction-iso.html) 与 [权限](https://www.postgresql.org/docs/current/ddl-priv.html)：预算短事务、角色和表权限；不提供自动业务幂等。
- [OCI Runtime Spec：Linux 配置](https://github.com/opencontainers/runtime-spec/blob/main/config-linux.md)：资源与 Linux 隔离配置规范；配置正确不代表消除了容器逃逸风险。
- [Go sync](https://pkg.go.dev/sync)、[Java 21 标准库](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/module-summary.html)：本章互斥、摘要与编码示例的真实 API。

**一页速记**：盲写假设 → 有证据辩论 → 独立风险否决 → 冻结 AST 与 manifest → 无权沙箱 → PIT 时间切分与标签隔离 → 全量试验登记与多重检验 → 前瞻确认；全过程全局限额、费用对账，不用票数或高 DSR 代替真实世界验证。
