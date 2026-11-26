# 40. 金融时序感知 RAG：财报解析、Point-in-Time 与可核验数字证据

> **状态**：深入工程篇｜**所属**：第五部分·金融 × AI-Agent 架构实战｜**编号**：40 / 48
>
> **定位**：承接 [39 三平面隔离](39-fin-agent-architecture-and-three-plane-isolation.md)，让“截至某时公司收入是多少”成为可回放的数据查询，而不是模型凭印象回答；精度与时钟分别复用 [20 金额精度](20-monetary-precision-rounding-and-allocation.md)、[21 金融时间](21-financial-time-calendar-and-sequence-id.md)。
>
> **代码契约**：Go 1.24+、Java 21+ 标准库示例可独立运行；DDL 面向 PostgreSQL 16+ 与 pgvector 0.8+，向量仅用三维教学数据。SQL 是可绑定参数的查询模板，部署权限、解析器和模型适配明确属于工程骨架，不冒充完整生产系统。
>
> **红线**：报告期末不等于信息可得时间；重述不能覆盖历史；单位不能靠猜；引用存在不等于数字正确；权限必须在候选检索前生效；基础模型已学过的未来信息，不能仅靠 Prompt 消除。

## 40.1 虚构事故：回测提前两个月读到重述利润

**以下为虚构教学事故；公司、时间和金额均为设定，不指向任何已证实机构事件。** 示例公司“示例制造”使用 USD，系统在 2025 年研究其 2024 年财报。

| UTC 时间 | 外部事实与系统动作 | 错误处理 |
|---|---|---|
| 2024-12-31 | 年度报告期结束 | 用期末日期作为所有财报片段的可用时间 |
| 2025-03-03 21:00 | 发布业绩公告：营收 1,200 百万美元、净利 90 百万美元 | 下载后只保留正文和向量 |
| 2025-03-03 21:02 | 抓取完成；21:05 通过解析与索引校验 | 把摄取延迟从回测中抹掉 |
| 2025-03-10 20:00 | 提交正式年报，含更完整脚注 | 认为同一报告期的文档可以任意替换 |
| 2025-05-05 20:00 | 修订披露：营收改为 1,140、净利改为 57 百万美元 | 覆盖旧事实行，刷新全部向量 |
| 2025-06-01 | 重跑 3 月 4 日策略 | 检索到 5 月版本，解释为“当时已知利润下降” |

3 月 4 日可能知道公告，不可能知道 5 月修订；3 月 3 日 21:03 的真实系统甚至尚不能使用 21:05 才验证就绪的事实。事后按最新口径重算历史，与复原当时可执行策略，是两种不同产品。

若策略因这条未来信息避开 10 万美元持仓，随后价格跌 8%，回测会少记 **8,000 美元账面损失**；这里只是条件演算，不是该错误必然造成的收益，也不是已实现盈利。

> 唯一真问题：能否证明答案中的每个数字，来自请求者有权访问、在决策截止前已可得且在指定系统视角中可用的那个版本，并且算式、量纲和引用都能独立复核？

交付物不是“一段更好的答案”，而是 `query_spec + evidence_manifest + calculation_trace + rendered_answer`；没有满足约束的证据，就返回缺证据，不自动切换到今天的最新版。

## 40.2 从 PDF 表格到事实：先保住上下文，再讨论 Embedding

### 40.2.1 单位、表头与版面是事实的一部分

财报中的 `1,200` 不是完整事实。最低信息元组为：主体、指标、期间、单位与币种、合并范围、维度、会计口径、数值、来源定位、版本。

| 必须保留 | 例子 | 丢失后的错误 |
|---|---|---|
| 表标题与单位行 | 合并利润表；USD in millions, except per-share data | 营收少乘一百万，EPS 却被多乘一百万 |
| 多级列头 | 2024 / 2023；截至 12 月 31 日止年度 | 把上年列当本年列 |
| 行路径与脚注 | 持续经营 → 归母净利润；注 7 | 把归母净利与含少数股东损益混用 |
| 合并/母公司/分部 | Consolidated / Parent / Europe | 同指标同期间被错误去重 |
| 符号与缺失语义 | `(57)`、`—`、空白、`0` | 把负数变正数，把未披露变零 |
| 原币与展示换算 | USD、HKD、人民币万元 | 没有汇率来源却比较绝对金额 |
| 页号与物理位置 | PDF 物理第 18 页、印刷页 16、bbox | 审计人员无法定位同一单元格 |

解析顺序：原始文件封存并哈希 → 文档类型检测 → 原生文本/HTML 表结构 → 扫描页 OCR → 表头树与单元格关系 → 单位/符号正规化 → 事实校验 → 建索引。OCR 不是默认第一步，低置信度或合计异常进入隔离队列。

Chunk 按“表头路径 + 若干完整行 + 必要脚注”切分，而不是每 500 字机械截断。跨页表格仅在标题、列结构、单位和续表标记一致时续接；重复的表头可以复用，但不能猜测缺失的年份。

同时保存原文与展示投影：`raw_cell="1,200"`、`header_unit="USD million"`、`normalized_value="1200000000"`。前端显示“12 亿美元”只是第三种投影，不能回写原始值；单位未知应拒绝参与金额计算。

### 40.2.2 XBRL context 不是“年份标签”

标准 XBRL 数字事实由 concept、`contextRef`、`unitRef`、数值及精度属性共同确定；context 包含实体、instant/duration 期间及 segment/scenario。维度还需按 Dimensions 规范解析，不能只读 XML 中第一个 entity identifier。

```text
concept       = {taxonomy-namespace}Revenue
entity        = identifier.scheme + identifier.value
period        = duration: 2024-01-01 .. 2024-12-31
dimensions    = { GeographyAxis: EuropeMember }
unit          = iso4217:USD
contextRef    = C_Europe_2024（只在该实例内有意义）
value         = 1200000000；decimals = -6
```

`decimals=-6` 表示报告精度到百万量级，**不是再乘 `10^6` 的指令**。Inline XBRL 的 `scale=6` 才参与展示数字到实例值的转换；应先应用 transformation registry、scale、sign，再得到实例事实。已经解析成实例值或 API 的 `val`，不能再次应用网页单位。

`xsi:nil=true` 不等于零；`USD/shares`、shares、pure 不是 USD。每股收益与普通金额分开类型化，`balance=credit/debit` 也不能当作给每个数字取反的开关。括号、iXBRL sign 与已解析负号必须防止重复处理。

context ID 可任意命名，跨文档不稳定；比较时用规范化后的实体、期间、完整维度和必要 scenario，而非字符串相等。QName 应展开成 namespace URI 与 local name；固定 taxonomy/DTS 摘要与映射版本，避免前缀变化或扩展标签误映射。

标准标签不保证可比：IFRS 与 US GAAP、净额与总额、终止经营、52/53 周年度都可能不同。自定义扩展 concept 不能仅凭 label 向量相似就映射为营收；映射是有版本、有审批、有测试的语义规则。

SEC Company Facts API 便于候选发现，但其聚合范围不覆盖所有自定义标签与分部上下文；Frames 偏向最后提交且近似日历期间的值。**不能把今天下载的聚合 API 当作历史 PIT 数据仓库**；必须回到具体 accession、原始提交及当时的可得证明。

### 40.2.3 结构化事实与叙述证据双轨

财务数字优先查询结构化事实，风险因素、会计估计和解释走段落/表格检索；二者通过 `fact_id → source_locator → filing_version` 关联。表格单元格、表头和脚注应形成证据包，不是各自互不认识的向量。

重复事实需按上下文与精度校验；一致的重复保留全部来源，冲突的重复标记 `CONFLICT`。HTML、XBRL、PDF 数值不一致时隔离而非取平均；calculation linkbase 和报表合计是检查依据，不是证明所有财务语义正确的万能规则。

## 40.3 PIT 多时间、重述与最小持久化模型

### 40.3.1 四种时间之外，还要有系统就绪时间

| 字段 | 定义 | 不允许的替代 |
|---|---|---|
| `period_start/end` / `instant` | 被描述的经济期间或时点 | 用它代替公告可得时间 |
| `published_at` | 发布者声明的此次发布事件时间 | 默认等于公众已能下载 |
| `first_available_at` | 在约定来源、渠道及授权人群中首次可得的可信时间 | 将今天首次抓取倒填到期末 |
| `ingested_at` | 本系统首次持久化此原始版本的时间 | 为修复回测而回拨 |
| `ready_at` | 指定解析/校验/索引产物可查询的时间 | 只要下载完成就视为可用于策略 |
| `revision_published_at` | 此版本作为修订披露的发布事件时间 | 用重述涉及的旧期间伪装旧发布 |

统一存 UTC、原始时区、精度与来源证明。只有日期的披露不能偷偷变成当天零点；可按政策保守到该来源当地日末之后，或标记时间不确定并拒绝日内回放。来源时钟偏差、夏令时和交易所休市另行处理。

`first_available_at` 是渠道相关的观察结论，不是宇宙中人人同时知道的真理。付费终端、公告网站、监管提交可能不同；保存观察者、响应头/签名回执、对象摘要及证据等级。HTTP Last-Modified 或 filed 日期本身不足以证明分钟级首次公开。

两种查询必须显式区分：

- **公众知识回放**：`first_available_at ≤ decision_at`，摄取截止为研究数据快照；这是“当时公开了什么”的事后重建，不能声称实际系统当时能用。
- **系统执行回放**：上述条件外，还要求 `ingested_at ≤ decision_at` 且对应产物 `ready_at ≤ decision_at`；已封存快照可进一步收紧截止。

双时态可理解为“被描述的业务有效期间”与“数据库知晓/记录历史”，但金融可得性还需要发布渠道时间。不能把所有字段挤成 `valid_from/valid_to` 两列然后宣称已经满足 PIT。

### 40.3.2 重述追加，版本选择先于最新值解释

经济事实键 `fact_key` 包含主体、concept、期间、单位、维度、合并范围与会计口径；同一键的数值修订追加为更高 `revision_no`。只有经校验的来源血缘可分配版本序号，不能按摄取顺序给“最新”盖章。

修订覆盖范围要细到事实或章节：一份 10-K/A 只修改签名/附件，不代表所有财务数字都重述；后续年报中的比较列也不必然表示原值错误。重述、重分类、会计政策追溯调整和普通重复披露要分别标记。

`as_reported_at(t)` 先过滤可得性，再在同一事实键内选最高有效修订；`latest_restated` 则明确是今天版本。不能先取全库最新行再过滤时间；否则修订会使旧版本从历史答案中“消失”。发布撤回/不应再依赖等通知也作为事件保存，不能仅靠更换数字表达。

下面是**最小可执行 DDL**；需先由管理员安装 pgvector。只表达金额事实及其数字证据，不覆盖完整 DTS、公共共享授权、撤回事件和所有产物历史；生产扩展采用追加的 artifact/availability 事件，禁止 UPDATE 抹掉旧解析结果。

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE filing_version (
  tenant text NOT NULL, filing_id text NOT NULL, issuer text NOT NULL,
  accession text NOT NULL, source_uri text NOT NULL,
  raw_sha256 bytea NOT NULL CHECK (octet_length(raw_sha256) = 32),
  published_at timestamptz NOT NULL, first_available_at timestamptz NOT NULL,
  ingested_at timestamptz NOT NULL, revision_published_at timestamptz,
  availability_proof text NOT NULL,
  PRIMARY KEY (tenant, filing_id), UNIQUE (tenant, accession, raw_sha256)
);
CREATE TABLE filing_grant (
  tenant text NOT NULL, filing_id text NOT NULL, db_role name NOT NULL,
  PRIMARY KEY (tenant, filing_id, db_role),
  FOREIGN KEY (tenant, filing_id) REFERENCES filing_version
);
CREATE TABLE financial_fact (
  tenant text NOT NULL, fact_id text NOT NULL, filing_id text NOT NULL,
  fact_key text NOT NULL, revision_no integer NOT NULL CHECK (revision_no > 0),
  concept text NOT NULL, context_json jsonb NOT NULL,
  period_start date, period_end date NOT NULL,
  unit text NOT NULL, normalized_value numeric NOT NULL,
  raw_cell text NOT NULL, display_scale integer NOT NULL,
  decimals_attr text, parser_version text NOT NULL,
  ready_at timestamptz NOT NULL,
  status text NOT NULL CHECK (status IN ('VERIFIED','CONFLICT','QUARANTINED')),
  PRIMARY KEY (tenant, fact_id), UNIQUE (tenant, fact_key, revision_no),
  UNIQUE (tenant, fact_id, filing_id),
  FOREIGN KEY (tenant, filing_id) REFERENCES filing_version,
  CHECK (period_start IS NULL OR period_start <= period_end)
);
CREATE TABLE evidence_chunk (
  tenant text NOT NULL, chunk_id text NOT NULL, filing_id text NOT NULL,
  fact_id text NOT NULL, locator jsonb NOT NULL, body text NOT NULL,
  body_sha256 bytea NOT NULL CHECK (octet_length(body_sha256) = 32),
  ready_at timestamptz NOT NULL, embedding_model text NOT NULL,
  embedding vector(3) NOT NULL,
  search_tsv tsvector GENERATED ALWAYS AS
    (to_tsvector('simple'::regconfig, body)) STORED,
  PRIMARY KEY (tenant, chunk_id),
  FOREIGN KEY (tenant, fact_id, filing_id)
    REFERENCES financial_fact(tenant, fact_id, filing_id)
);
CREATE INDEX fact_lookup ON financial_fact(tenant, fact_key, revision_no DESC);
CREATE INDEX filing_pit ON filing_version(tenant, first_available_at, ingested_at);
CREATE INDEX chunk_text ON evidence_chunk USING gin(search_tsv);
ALTER TABLE filing_grant ENABLE ROW LEVEL SECURITY;
ALTER TABLE filing_grant FORCE ROW LEVEL SECURITY;
CREATE POLICY own_grant ON filing_grant FOR SELECT
  USING (db_role = current_user);
ALTER TABLE filing_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE filing_version FORCE ROW LEVEL SECURITY;
CREATE POLICY readable_filing ON filing_version FOR SELECT USING (EXISTS (
  SELECT 1 FROM filing_grant g WHERE g.tenant = filing_version.tenant
    AND g.filing_id = filing_version.filing_id AND g.db_role = current_user
));
ALTER TABLE financial_fact ENABLE ROW LEVEL SECURITY;
ALTER TABLE financial_fact FORCE ROW LEVEL SECURITY;
CREATE POLICY readable_fact ON financial_fact FOR SELECT USING (EXISTS (
  SELECT 1 FROM filing_version d WHERE d.tenant = financial_fact.tenant
    AND d.filing_id = financial_fact.filing_id
));
ALTER TABLE evidence_chunk ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_chunk FORCE ROW LEVEL SECURITY;
CREATE POLICY readable_chunk ON evidence_chunk FOR SELECT USING (EXISTS (
  SELECT 1 FROM filing_version d WHERE d.tenant = evidence_chunk.tenant
    AND d.filing_id = evidence_chunk.filing_id
));
```

服务角色需获四表 SELECT，但不能是 owner、superuser 或带 `BYPASSRLS`；无写入 grant 的能力，也不能随意 `SET ROLE`。示例用数据库身份绑定请求者；共享连接池应由可信代理映射身份，不接受模型提供的角色。部署 GRANT/角色创建属于骨架，必须由管理员按实际身份域配置。

授权查询此刻仍需有效：回放历史不允许绕过今天的撤权。若要证明历史用户当时是否有权，还要另存授权事件并取“历史许可与当前许可”的交集。DDL 的哈希长度约束不验证内容正确，也不保证不可变；写服务需限制 UPDATE/DELETE、采用独立对象锁定与审计。

## 40.4 SQL 混合检索：时间与权限先裁剪，排名后发生

### 40.4.1 数字查询不要先让向量决定哪个年份

先将请求编译为结构化约束：可信租户、主体 ID、指标候选、期间类型、维度、单位、`decision_at`、回放模式、快照与授权版本。证券简称解析、指标映射或报告期有歧义时先澄清；“2024Q4”可能指自然季度，也可能指企业财年第四季。

下列模板专用于**系统执行回放的一个事实键**；`$1=decision_at`、`$2=ingest_cutoff`，并要求 `$2≤$1`；`$3=tenant`、`$4=fact_key`、`$5=关键词`、`$6=三维查询向量`、`$7=embedding_model`。参数由可信查询编译器绑定，不让模型写任意 SQL。

```sql
WITH visible AS MATERIALIZED (
  SELECT f.*, d.first_available_at, d.accession, d.source_uri, d.raw_sha256
  FROM financial_fact f JOIN filing_version d
    ON d.tenant = f.tenant AND d.filing_id = f.filing_id
  WHERE f.tenant = $3 AND f.fact_key = $4
    AND $2::timestamptz <= $1::timestamptz
    AND d.first_available_at <= $1::timestamptz
    AND d.ingested_at <= $2::timestamptz
    AND f.ready_at <= $1::timestamptz
), picked AS MATERIALIZED (
  SELECT * FROM visible ORDER BY revision_no DESC LIMIT 1
), eligible AS MATERIALIZED (
  SELECT c.*, f.normalized_value, f.unit, f.revision_no,
    f.accession, f.source_uri, f.raw_sha256
  FROM picked f JOIN evidence_chunk c
    ON c.tenant = f.tenant AND c.fact_id = f.fact_id
  WHERE f.status = 'VERIFIED' AND c.ready_at <= $1::timestamptz
    AND c.embedding_model = $7
), lexical AS (
  SELECT chunk_id, row_number() OVER (
    ORDER BY ts_rank_cd(search_tsv, plainto_tsquery('simple', $5)) DESC,
      chunk_id) AS r
  FROM eligible WHERE search_tsv @@ plainto_tsquery('simple', $5)
  ORDER BY r LIMIT 100
), semantic AS (
  SELECT chunk_id, row_number() OVER (
    ORDER BY embedding <=> $6::vector(3), chunk_id) AS r
  FROM eligible ORDER BY r LIMIT 100
), votes AS (
  SELECT chunk_id, 1.0 / (60 + r) AS score FROM lexical
  UNION ALL
  SELECT chunk_id, 1.0 / (60 + r) AS score FROM semantic
), fused AS (
  SELECT chunk_id, sum(score) AS score FROM votes GROUP BY chunk_id
)
SELECT e.chunk_id, e.fact_id, e.normalized_value, e.unit, e.locator,
  e.body, e.body_sha256, e.accession, e.source_uri, e.raw_sha256, x.score
FROM fused x JOIN eligible e USING (chunk_id)
ORDER BY x.score DESC, e.chunk_id LIMIT 10;
```

RLS 与显式 tenant 约束共同限制候选；MATERIALIZED 表达裁剪边界，不代替权限机制。这里对 eligible 做精确向量排序，便于审计，不声称用了 HNSW；真实中文分词要接入经过验证的分析器，`simple` 仅是能运行的占位配置，不能宣称中文召回质量。

为什么不“全库向量 Top-100，再过滤”？因为未来和无权文档会占满候选，造成授权集合内的漏召回，且可能进入外部 reranker、日志或模型。ANN 的过滤可能在索引扫描后发生；需按许可域分区、支持过滤的索引设计、迭代扫描及授权集合内 Recall@K 对照测试，不能只加 WHERE 就宣布安全且高召回。

已就绪的最高修订若为 CONFLICT/QUARANTINED 或没有相应证据片段，应返回冲突/缺证据，不得偷偷回退旧版本；所以 status 在 picked 之后检查。未就绪修订不参与本模板选择，若已收到“不应再依赖旧报表”的通知，还须由独立撤回事件闸门阻断旧值。公众知识重建需使用另一条受审模板放宽摄取/就绪截止，并标记 reconstructed；不要让客户端任意组合时间字段。

### 40.4.2 三层证据闸门与确定性数字

1. **检索闸门**：时间、当前权限、版本、模型版本、单位和报告期匹配；候选正文视为不可信数据，脚注中的“请忽略指令”不能改变查询。
2. **数值闸门**：金额用 Decimal/整数有理数；所有加减要求同币种、同维度与可比期间；同比先对齐口径，除零和负基数按政策输出 N/A，不由模型编造百分比。
3. **输出闸门**：每个数字绑定事实 ID 或计算节点；引用定位、原文片段、文件摘要、数值及量纲逐项核验；未通过则仅返回缺口与证据，不返回未证实结论。

原文校验不等于搜索字符串：一个文件里可能有几十个“1,200”。校验器必须读取指定对象版本，核验摘要，再按页/bbox 或 DOM/XBRL 节点取出单元格，连同列头、行头、单位与脚注比较。OCR 文本哈希只证明提取物未变，不证明 OCR 与图片一致。

严格输出模式下，LLM 只生成受限 Claim AST 与叙述模板槽位；可信 renderer 插入数字和证据卡，拒绝未知引用 ID、未登记数字、伪造 accession。普通自由文本即使做数字正则检查，也挡不住“利润翻倍”等无数字的暗示，仍需语义评估或受限模板。

证据 manifest 固化 `decision_at/mode/snapshot_id/auth_epoch/fact_ids/chunk_ids/raw_hashes/parser_version/mapping_version/embedding_model/calculation_policy`，加上实际检索结果与校验结果。确定性计算正确只证明指定输入的算术，不证明披露真实、完整或策略有收益。

## 40.5 Go + Java：可运行的精确金额与增长率核心

### 40.5.1 手算黄金向量

公告营收 `1,200 USD million = 1,200 × 10^6 = 1,200,000,000 USD`；上年同口径营收 `1,000 USD million`，同比 `(1,200−1,000)/1,000=20%`，即 **2,000 bp**。净利率 `90/1,200=7.5%`，即 **750 bp**。

修订版营收 `1,140`、净利 `57` 百万美元，对比同一上年基数时同比 **14% = 1,400 bp**、净利率 **5% = 500 bp**。3 月 4 日答案应使用 20% 与 7.5%；5 月修订可得且系统就绪之后才可使用 14% 与 5%。若上年数也重述，必须重算，不能继续机械沿用 1,000。

下面两段代码使用同一窄域：非负整数展示金额、十进制 scale 为 0..9、基数为正、增长可负；增长率以 bp 四舍五入，恰好半个 bp 时远离零（HALF_UP）。不支持 EPS、负营收基数、科学计数法或完整 XBRL transformation，超出域就拒绝，不用 float 兜底。

### 40.5.2 Go：标准库 big.Int，避免缩放与比率溢出

保存为 `main.go`，执行 `go run main.go`；断言失败会 panic，正常输出两行。示例只校验算术，不负责解析源文件、PIT、权限、哈希或引用匹配。

```go
package main

import (
    "fmt"
    "math/big"
    "regexp"
)

var digits = regexp.MustCompile(`^(0|[1-9][0-9]{0,29})$`)

func amount(raw string, scale int) *big.Int {
    if !digits.MatchString(raw) || scale < 0 || scale > 9 {
        panic("unsupported amount")
    }
    n, ok := new(big.Int).SetString(raw, 10)
    if !ok { panic("bad integer") }
    m := new(big.Int).Exp(big.NewInt(10), big.NewInt(int64(scale)), nil)
    return new(big.Int).Mul(n, m)
}

func growthBP(current, previous *big.Int) *big.Int {
    if current == nil || previous == nil || current.Sign() < 0 || previous.Sign() <= 0 {
        panic("unsupported base")
    }
    n := new(big.Int).Sub(current, previous)
    n.Mul(n, big.NewInt(10000))
    sign := n.Sign()
    n.Abs(n)
    q, r := new(big.Int), new(big.Int)
    q.QuoRem(n, previous, r)
    if new(big.Int).Lsh(r, 1).Cmp(previous) >= 0 {
        q.Add(q, big.NewInt(1))
    }
    if sign < 0 { q.Neg(q) }
    return q
}

func main() {
    old := amount("1000", 6)
    initial := amount("1200", 6)
    revised := amount("1140", 6)
    tests := []struct{ now, base, want string }{
        {"1200", "1000", "2000"}, {"1140", "1000", "1400"},
        {"19999", "20000", "-1"}, {"20001", "20000", "1"},
        {"0", "1000", "-10000"}, {"1000", "1000", "0"},
    }
    for _, t := range tests {
        if growthBP(amount(t.now, 0), amount(t.base, 0)).String() != t.want {
            panic("golden vector failed")
        }
    }
    fmt.Printf("initial=%s USD growth=%s bp\n", initial, growthBP(initial, old))
    fmt.Printf("revised=%s USD growth=%s bp\n", revised, growthBP(revised, old))
}
```

### 40.5.3 Java：BigDecimal 的精确构造与显式舍入

保存为 `Main.java`，执行 `java Main.java`，也可 `javac Main.java && java Main`；输出与 Go 一致。不要 `new BigDecimal(double)`；这里以字符串构造，金额只做精确乘法，除法才舍入。

```java
import java.math.BigDecimal;
import java.math.BigInteger;
import java.math.RoundingMode;

public class Main {
    static BigDecimal amount(String raw, int scale) {
        if (raw == null || !raw.matches("(0|[1-9][0-9]{0,29})") || scale < 0 || scale > 9)
            throw new IllegalArgumentException("unsupported amount");
        return new BigDecimal(raw).scaleByPowerOfTen(scale);
    }
    static BigInteger growthBP(BigDecimal current, BigDecimal previous) {
        if (current == null || previous == null || current.signum() < 0 || previous.signum() <= 0)
            throw new IllegalArgumentException("unsupported base");
        return current.subtract(previous).multiply(new BigDecimal("10000"))
            .divide(previous, 0, RoundingMode.HALF_UP).toBigIntegerExact();
    }
    public static void main(String[] args) {
        BigDecimal old = amount("1000", 6);
        BigDecimal initial = amount("1200", 6);
        BigDecimal revised = amount("1140", 6);
        String[][] tests = {
            {"1200", "1000", "2000"}, {"1140", "1000", "1400"},
            {"19999", "20000", "-1"}, {"20001", "20000", "1"},
            {"0", "1000", "-10000"}, {"1000", "1000", "0"}
        };
        for (String[] t : tests)
            if (!growthBP(amount(t[0], 0), amount(t[1], 0)).toString().equals(t[2]))
                throw new AssertionError("golden vector failed");
        System.out.printf("initial=%s USD growth=%s bp%n",
            initial.toPlainString(), growthBP(initial, old));
        System.out.printf("revised=%s USD growth=%s bp%n",
            revised.toPlainString(), growthBP(revised, old));
    }
}
```

共同输出：`initial=1200000000 USD growth=2000 bp` 与 `revised=1140000000 USD growth=1400 bp`。生产类型再包上币种、量纲、fact ID 和期间，禁止把两个“恰好都是大整数”的不同币种相减；精确算法不自动提供类型安全。

框架适配**骨架**：`authenticated identity → QuerySpec → parameterized SQL → evidence validator → calculation DAG → Claim AST → renderer`。Go 可用 database/sql 接 PostgreSQL 驱动，Java 可用 JDBC；驱动、解析器与模型 SDK 需锁版本。不要把上面函数包装成任意表达式执行器或让模型直连数据库。

## 40.6 摄取与回答状态机：重试不能抹去证据

### 40.6.1 文档生命周期与发布屏障

```text
DISCOVERED → FETCHED → PARSED → VALIDATED → INDEXED → READY
                │         │           │
                └─────────┴───────────┴──→ QUARANTINED → 新解析产物重新验证
新外部披露 → 新 filing_version；新解析器 → 新 artifact；绝不覆盖历史产物
```

每条转换记录输入摘要、工具版本、开始/结束时间、错误码、操作者和 CAS version；幂等键用 `(source, accession, raw_hash, parser_version, mapping_version)`，而不是只用文件名。相同 accession 返回不同字节先记录新对象与异常，不能当“重复文件”丢掉。

READY 是发布屏障：事实库、chunk、向量与原文对象均通过校验后原子发布 manifest，再更新可见指针；跨数据库用 Outbox 与确认协议，不能把向量写成功等同于整个证据包成功。新旧索引切换必须保留旧 manifest 以支持历史回放。

最小 DDL 中每个事实只容纳一个验证产物；生产要保留多产物的 `recorded_at/ready_at/supersedes`。今天修复去年 OCR 后，可以提供“修复后重建视角”，但绝不能让去年真实系统自动拥有今天才就绪的修正值。

### 40.6.2 回答生命周期与撤权

```text
RECEIVED → AUTHORIZED → SNAPSHOT_PINNED → RETRIEVED → VERIFIED → RENDERED
                 任一步缺时间/单位/引用 → INSUFFICIENT_EVIDENCE
                 权限撤销/快照不一致   → DENIED / RETRY_WITH_NEW_SNAPSHOT
```

查询内固定数据库快照与证据 manifest，不持有事务等待 LLM；先取完整证据封存，模型只读这个包。渲染或下载前按授权 epoch 再检查，撤权取消缓存、活动流和引用签名 URL；已发送的字节无法撤回，需限定短租约及暴露边界。

缓存键至少包含主体/许可域、tenant、decision、模式、数据 manifest、映射/解析版本、计算政策、授权 epoch；只用自然语言 query 做缓存键会跨租户或跨时点泄漏。外部 reranker/Embedding API 也属于数据接收方，需要相同许可与地域约束。

## 40.7 测试、压测与未来函数：把“看起来正确”拆成可证性质

### 40.7.1 时间边界与原文对账测试

| 测试输入 | 必须断言 |
|---|---|
| 初版 3 月 3 日 21:00 可得、21:05 就绪；查询 21:03 | 公众重建可列初版，系统回放不可列 |
| 5 月修订在库；查询 3 月 4 日 | 仍返回初版 1,200 与 90，不返回 1,140 与 57 |
| 修订只有签名附件变化 | 财务事实不生成假重述；旧事实仍可检索 |
| 高版本先摄取、低版本后补抓 | 以经验证的修订血缘排序，不按 ingest 排序 |
| 文本单位 million、EPS 单独 except | 金额乘百万，EPS 不乘；未知单位进隔离 |
| 同文件同数字在两个年份各出现一次 | 引用必须定位正确列，不允许字符串存在即通过 |
| 相同 context ID、不同维度/不同实例 | 不能合并；展开维度后才生成事实键 |
| 两个租户问同一句话，缓存热命中 | 无跨域正文、标题、片段、引用 URL 泄漏 |
| 撤权与生成并发、向量/事实发布一半 | 最后闸门拒绝或重试，不输出半份证据 |
| 零基数、负基数、半 bp、大整数 | 按声明域拒绝或精确舍入；Go/Java 黄金向量一致 |

性质测试：对合法历史快照，加入 `first_available_at > decision` 的任意文档，历史证据集合和确定性输出不变；同键重试不产生新事实；调换抓取顺序不改变修订顺序；不同币种不允许直接运算。对模型自由文案不要求逐字确定，而要求不得新增事实性 Claim。

对账分四链：原始提交清单 ↔ 对象存储；对象 ↔ 解析事实；事实 ↔ 数字引用；回答 Claim ↔ 计算 DAG。统计缺失、重复、冲突、未就绪与拒答率，不能只用“有引用的答案占比”冒充事实准确率。

### 40.7.2 压测矩阵与容量手算

**以下是待验证的教学目标，不是代码实测结果**：1,000 万 chunks、100 个租户、200 QPS，含 1% 许可域内可见数据及热门单公司倾斜；分别测纯 SQL、rerank、生成与端到端 p50/p95/p99、超时、授权漏出和 Recall@K。

若平均请求驻留 2 秒，200 QPS 按 Little 定律平均约 **400 个在途请求**；若数据库阶段平均 50 ms，则平均约 **10 个在途数据库请求**。两者不是同一个连接池容量，也不是峰值上界；模型在途不应长期占数据库连接。

按冷缓存、热缓存、修订集中发布、权限快速撤销、回放大跨度和索引切换分别测试。精确授权集合 Top-K 作为 ANN 召回基线；无权结果进入模型或日志的计数必须为零，时间违规率也须为零，不能拿整体回答满意度抵消。

过载时先限并发、限扫描与候选数，允许返回“检索容量不足”；向量不可用可按明确策略降级结构化/全文查询，绝不放松时间/权限。监控 `ready_at−first_available_at` 的 p99 与最老未就绪年龄，不能用平均摄取延迟掩盖财报季堆积。

### 40.7.3 五类未来函数与模型参数污染

1. **版本未来函数**：今日 latest_restated 覆盖过去；用可得性过滤后的追加版本选择解决。
2. **流水线未来函数**：历史回放使用今天才修复的 OCR、映射或索引；冻结当时产物，重建实验另行标记。
3. **样本未来函数**：只保留今天仍上市公司，使用事后行业分类、公司行动和汇率；证券主数据与样本宇宙也要 PIT。
4. **评价未来函数**：随机切分同一财报片段，让修订或同源比较列穿透训练/测试；按时间及来源分组隔离，做 embargo 与重叠检查。
5. **参数未来函数**：模型训练中见过未来财报、退市结局或新闻，即使 RAG 证据只含历史，仍可能从参数记忆输出未来。

第五类不能通过“你只能使用以下资料”可靠消除。可采用在目标时点前训练截止明确且可核验的模型、隔离微调语料、隐藏公司身份与时间做对照、受限证据计算器，以及无 LLM 的结构化基线；商业模型截止声明不等于可证明没有污染。

固定 Prompt、temperature=0 或加第二个模型都不是无泄漏证明。严格 PIT 策略评估应让决策特征只来自受控证据/确定性计算，模型不能自由添加特征；若仍允许参数知识影响决策，必须披露这一限制，不能宣称“无未来函数回测”。

## 40.8 术语、面试、自检与后续衔接

### 40.8.1 术语正本清源

| 术语 | 本章精确定义 | 常见误用 |
|---|---|---|
| PIT / as-of | 按指定可得视角复原截止时刻的信息集合 | WHERE period_end ≤ t 就算 PIT |
| Restatement / revision | 重述是一类有财务含义的修订；修订不一定改数字 | 任意 10-K/A 都替换所有原事实 |
| Context / unit / scale | 主体期间维度 / 量纲 / 展示缩放 | decimals 当 scale，context ID 当全局键 |
| Hybrid retrieval / RRF | 词法与语义候选融合 / 基于名次的分数融合 | 把不同模型的原始分数直接相加 |
| Grounding / citation validation | 结论受证据约束 / 来源定位与数值语义核验 | 有可点击链接就是真实且当时可得 |
| System replay / reconstruction | 复原当时系统可用输入 / 用今天资料重建过去公开输入 | 两种收益曲线混在同一回测报告 |

### 40.8.2 十分钟面试答题卡

1. **0–2 分钟**：问清市场、数据许可、日内/日频、公众还是系统回放；列出报告期、发布、首次可得、摄取、就绪与修订时间。
2. **2–4 分钟**：画原文对象、表头/OCR/XBRL 解析、事实库、词法/向量索引与 manifest；先讲单位和上下文，后讲 Embedding。
3. **4–6 分钟**：追加修订，先时间/权限裁剪再版本选择和混合排名；当前撤权始终有效，缓存和 reranker 不得旁路。
4. **6–8 分钟**：精确计算 DAG、每数一证、哈希与定位核验、可信 renderer；缺证据拒答，模型不得发明公司/期间/数值。
5. **8–10 分钟**：时间性质测试、授权集合 ANN 召回基线、摄取延迟、发布屏障、模型未来污染；说明保证边界而非承诺绝对无幻觉。

### 40.8.3 自检与参考答案

**题 1**：财报 `1,200`，表头 USD million，XBRL 实例值 1,200,000,000、decimals=-6。数据库该存多少？

**答案**：正规化值为 1,200,000,000 USD；保留 raw_cell、单位与精度。对实例值不再乘百万；EPS 的单独单位需另判，不能套整张表缩放。

**题 2**：3 月 3 日 21:03 查询，公告 21:00 可得、21:02 摄取、21:05 READY；5 月有重述，6 月才补抓。能返回什么？

**答案**：系统回放不得使用公告事实，因尚未就绪；公众知识重建可在有可得证明时使用初版，并注明事后重建。任何视角都不得将 5 月重述带到 3 月；缺初版就拒答，不能用重述填补。

**题 3**：金额计算与引用校验全过，模型却建议“提前避开即将破产的公司”，是否证明 PIT 回测有效？

**答案**：没有。未来结局可能来自参数记忆、样本筛选或主数据；需限制特征来源、设置结构化无模型基线并核验模型/数据历史。引用正确不能证明自由决策没有未来信息。

本章输出的是第 39 章护栏可以消费的**有时间、有权限、有计算出处的证据快照**，不是交易许可。后续进入多源投研与策略编排时，检索结论仍只能形成 Proposal；第 43 章注入防御、第 44 章 MCP/HITL 与实盘回执不能省略本章的证据约束，也不能把证据约束当成盈利保证。

### 官方来源与核验入口

- [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)：Submissions、Company Facts、Frames 的范围、聚合语义与更新时间；尤其核对 Frames 的 last filed 行为与非自定义、全实体事实范围。
- [SEC 开发者访问规范](https://www.sec.gov/os/webmaster-faq#developers)：自动访问、身份标识与公平访问要求；摄取器必须遵守当期政策与限流，不把教学 QPS 当 SEC 抓取许可。
- [XBRL 2.1 规范及勘误](https://www.xbrl.org/Specification/xbrl-2.1/REC-2003-12-31/xbrl-2.1-REC-2003-12-31+corrected-errata-2013-02-20.html)：context、unit、decimals、重复事实与期间语义。
- [Inline XBRL 1.1 Part 1](https://www.xbrl.org/specification/inlinexbrl-part1/rec-2013-11-18/inlinexbrl-part1-rec-2013-11-18.html) 与 [Dimensions 1.0](https://specifications.xbrl.org/work-product-index-dimensions-dimensions-1.0.html)：scale/sign/transformation、维度及有效性；引用固定历史规范，不宣称为最新发布版本。
- [PostgreSQL 16 行安全](https://www.postgresql.org/docs/16/ddl-rowsecurity.html)、[全文搜索](https://www.postgresql.org/docs/16/textsearch.html)、[事务隔离](https://www.postgresql.org/docs/16/transaction-iso.html)：RLS 绕过角色、政策组合与快照语义；RLS 不是匿名 SQL 沙箱。
- [pgvector 官方仓库](https://github.com/pgvector/pgvector)：距离运算符、过滤与近似索引、迭代扫描；实际依赖锁定到经过测试的版本。
- [Go math/big](https://pkg.go.dev/math/big)、[Java 21 BigDecimal](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/math/BigDecimal.html)、[RoundingMode](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/math/RoundingMode.html)：精确数值与舍入语义；语言精度不能替代财务口径。

**一页速记**：封存原文 → 恢复单位/表头/XBRL 上下文 → 追加版本与可得证明 → 固定 PIT 视角和当前权限 → 先裁剪再混合排名 → 精确计算与逐数引用核验 → 可信渲染与 manifest；模型记忆、缺失历史和参数污染必须单独暴露，不能用 Prompt 掩盖。
