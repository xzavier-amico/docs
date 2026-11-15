# 31. 合规、审计与取证

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 运行时、安全、合规与防御 ｜ **编号**：31 / 52
> **本章定位**：前 30 章都在讲「怎么让它跑对」，这一章讲**「怎么证明它当时是那样跑的」**。这是一条从「日志」到「证据」的升级路径：日志给你排障，审计给你合规，取证给你法律效力。
> **读法**：§31.5 的哈希链 + WORM 是本章的技术核心，请逐行看代码。§31.8 的取证流程是可以直接做成 runbook 的。如果你只想知道「要留什么、留多久、花多少钱」，跳到 §31.4 和 §31.9。

---

## 31.1 引子：一封 72 小时的协查函

2026 年 9 月 14 日（周一）上午 10 点，Aurora 法务收到一封监管协查函，要求 **72 小时内**提供：

> 「2026 年 8 月 12 日至 8 月 18 日期间，租户 `T-88231` 名下全部涉及「投资建议」类内容的会话记录，包括：用户输入原文、系统输出原文、所用模型及版本、内容审核判定结果、以及该租户的账号与计费信息。」

法务把函转给 CTO，CTO 转给你。你打开系统，发现三件事：

```
① 会话内容存在 ClickHouse，TTL = 30 天
   → 8 月 12 日的数据在 9 月 11 日已经被 TTL 清掉了。回收站也没有。

② 审核判定存在 MySQL 的 audit_log 表
   → 表结构里有 updated_at，说明这表被 UPDATE 过
   → 三个 DBA 有写权限，谁改过不知道
   → 更糟：这张表没有「判定结果」的快照，只存了最终的 label

③ 模型版本记在 trace 里，trace 采样率 5%
   → 95% 的调用根本不知道用的哪个模型版本
```

你能交出去的东西：一堆「查不到」「没有记录」「版本未知」。

> 🎯 **本章的核心命题**：
> **合规不是「有一份隐私政策」，是「在监管问你的时候，你能在 72 小时内拿出一份自己都改不了、别人也改不了的证据链」。**
> 这里面有三个独立的能力，缺一个就交不出东西：
> ① **留得够久**（留存策略，§31.4）；
> ② **改不了**（append-only + 哈希链 + WORM，§31.5）；
> ③ **查得出来**（可解释性记录 + 取证流程，§31.7、§31.8）。
> 三者是乘法关系。留了 3 年但能被改 = 0；改不了但只留 30 天 = 0；都做到了但查不出「凭什么这么答」= 0。

---

## 31.2 监管地图：你被哪几部法律同时管着

先把地图铺开。**Agent 平台的麻烦在于：同一个动作会被三到五部法规同时约束**，而它们的关注点完全不同。

| 法规 | 管辖区 | 核心要求（对 Agent 平台而言） | 触发条件 | 罚则量级 |
| --- | --- | --- | --- | --- |
| **EU AI Act**（Regulation (EU) 2024/1689） | 欧盟 | 风险分级（禁止/高风险/有限风险/最小风险）；高风险系统需风险管理、数据治理、技术文档、**自动日志记录（record-keeping）**、人类监督、准确性/鲁棒性/网络安全；通用目的 AI（GPAI）另有透明度与版权义务 | 在欧盟投放或使用 | 最高可达全球年营业额 7%（禁止性条款）或 3%（其他义务），**以官方文本与各成员国实施为准** |
| **《生成式人工智能服务管理暂行办法》** | 中国 | 训练数据合法性、**内容标识**、用户实名、投诉渠道、**配合监督检查**、未成年人保护、不得生成违法内容 | 向境内公众提供生成式 AI 服务 | 责令整改、警告、通报、罚款、停止服务 |
| **《互联网信息服务深度合成管理规定》** | 中国 | 深度合成内容**显著标识**、不得删除/篡改标识、训练数据与技术措施记录 | 提供深度合成服务 | 同上 |
| **《人工智能生成合成内容标识办法》** | 中国 | 显式标识（可见）+ 隐式标识（元数据），**2025 年 9 月 1 日施行，以官方发布为准** | 生成合成文本/图片/音视频/虚拟场景 | 同上 |
| **《网络安全法》** | 中国 | **网络日志留存不少于 6 个月**（第二十一条）；个人信息保护；关键信息基础设施 | 网络运营者 | 罚款、停业整顿 |
| **GDPR** | 欧盟 | 合法性基础、数据最小化、**处理活动记录（Art.30）**、**访问权（Art.15）**、**删除权（Art.17）**、**自动化决策（Art.22）**、**数据泄露 72 小时通知（Art.33）**、跨境传输机制 | 处理欧盟数据主体数据 | 最高 2,000 万欧元或全球营业额 4% |
| **SOC 2 Type II** | 美国（客户要求） | 安全性/可用性/处理完整性/保密性/隐私 五类信任服务准则，**需要一段观察期（通常 3~12 个月）的证据** | B2B 客户尽调 | 不是法律罚则，是「拿不到单」 |
| **等保 2.0（GB/T 22239-2019）** | 中国 | 三级要求：安全审计（审计记录保护、留存 ≥6 个月）、入侵防范、数据完整性与保密性、个人信息保护 | 定级备案后 | 警告、罚款、停业 |
| **《数据出境安全评估办法》/《个人信息出境标准合同办法》/《促进和规范数据跨境流动规定》** | 中国 | 数据出境的三条合法路径：安全评估、标准合同、认证 | 向境外提供数据 | 同上 |

### 31.2.1 「我到底算哪个风险等级」

EU AI Act 的分级是最容易误判的，给你一个工程判断表：

| 你的场景 | 大概率分级 | 需要做什么 |
| --- | --- | --- |
| 客服问答、内容摘要、代码补全 | 有限风险（transparency obligations） | 告知用户「你在和 AI 交互」；AI 生成内容可标识 |
| 招聘筛选、信贷评估、教育评分、关键基础设施 | **高风险** | 全套：风险管理系统、技术文档、自动日志、人类监督、准确性验证、**登记备案** |
| 社会评分、实时远程生物识别（特定场景）、操纵性技术 | **禁止** | 不能做 |
| 通用模型提供方（你训了基座） | GPAI | 技术文档、版权政策、训练数据摘要（部分开源豁免） |

> ⚠️ **别自己拍板分级**。这是法务 + 合规 + 业务的联合判断，工程团队提供「场景事实」（是否影响人的重大权益、是否有实质决策权、是否有替代人工复核），别提供结论。**技术团队唯一应该坚持的一件事是：不管什么分级，审计日志都要按最高标准建**——因为技术文档、人类监督、准确性验证全都依赖它。

### 31.2.2 一部法规一张表的「合规到工程」映射

| 法规要求 | 工程实现 | 本章节 |
| --- | --- | --- |
| 自动日志记录（AI Act record-keeping） | 不可篡改审计事件流 | §31.5 |
| 配合监督检查（生成式 AI 办法） | 协查响应流程 + 导出能力 | §31.8 |
| 网络日志留存 ≥ 6 个月（网安法/等保） | 分级留存策略 + WORM 归档 | §31.4 |
| 处理活动记录（GDPR Art.30） | 数据地图 + 处理目的登记 | §31.4 |
| 删除权（GDPR Art.17） | **加密擦除**（crypto-shredding） | §31.4.3 |
| 自动化决策（GDPR Art.22） | 可解释性记录 + 人工复核通道 | §31.7 |
| 内容标识（标识办法/深度合成规定） | C2PA + 元数据 + 可见标识 | §31.6 |
| 泄露 72 小时通知（Art.33） | 取证流程 + 影响面统计能力 | §31.8 |

---

## 31.3 数据驻留与跨境：先画数据地图，再谈合规

**没有数据地图，一切合规都是空谈。** 数据地图要回答四个问题：什么数据、在哪、谁能访问、流向哪。

| 数据类别 | 典型位置 | 是否可出境 | 驻留要求 | 备注 |
| --- | --- | --- | --- | --- |
| 用户输入原文 / 模型输出 | 区域内的 OLTP + 对象存储 | ❌ 默认不可 | 强（金融/医疗/政企客户常要求境内） | 最敏感 |
| 会话/事件/记忆 | 区域内 DB | ❌ | 强 | 派生于输入 |
| 审计事件（不含内容） | 区域 + 全局只读汇总 | ⚠️ 脱敏后可 | 弱 | 只含 id/hash/判定，不含原文 |
| Trace / 指标 | 全局 | ✅ 脱敏后 | 弱 | 必须先去标识 |
| 模型调用（第三方 API） | **境外** | ⚠️ **最容易踩雷** | 视客户合同 | 见 §31.3.2 |
| 备份 / 冷归档 | 区域内 | ❌ | 强 | 备份也要驻留 |
| 账单/发票 | 区域 | ⚠️ | 中 | 财务口径单独处理 |

### 31.3.1 多区域架构：把「region」变成第一等公民

```
                       ┌──────────────────────────────┐
                       │  全局控制面（不含用户数据）     │
                       │  租户注册 / 套餐 / 计费汇总     │
                       │  策略分发 / 版本注册           │
                       └───────┬──────────┬───────────┘
                               │          │
          ┌────────────────────▼──┐   ┌───▼────────────────────┐
          │  Region: cn-shanghai  │   │  Region: eu-frankfurt  │
          │  ├─ 会话/事件/记忆     │   │  ├─ 会话/事件/记忆      │
          │  ├─ 对象存储（内容快照）│   │  ├─ 对象存储            │
          │  ├─ 审计链 + WORM     │   │  ├─ 审计链 + WORM       │
          │  └─ 模型网关（境内模型）│   │  └─ 模型网关（EU 模型）  │
          └───────────────────────┘   └────────────────────────┘
                    ▲                            ▲
                    └──── 租户创建时绑定 region ───┘
                          ★ 数据永不跨 region ★
```

**四条硬规则**（违反任何一条都是合规事故）：

1. **租户绑定 region，且不可改**（要改 = 重新开户 + 数据迁移流程）。
2. **主键里带 region**：`run_id = {region}:{uuid}`。这样任何一条数据从 id 就能看出它属于哪个区域，跨区误写会在写入时就报错。
3. **全局控制面不存用户数据**，只存「租户 → region」的映射和聚合后的计费数字。
4. **备份、日志、缓存、消息队列全部要跟 region**。最常被漏掉的是**跨区灾备**和**全局日志聚合**——「为了排障方便把日志集中到一个地方」是跨境事故的头号原因。

### 31.3.2 第三方模型 API：最大的跨境风险点

```
用户（中国境内） → 你的服务（境内） → OpenAI/Anthropic API（境外）
                                        ↑
                            用户的原文在这里出境了
```

这件事在合同里必须写清楚，在技术上必须可切换：

| 措施 | 做法 | 效果 |
| --- | --- | --- |
| **区域级模型白名单** | `region=cn` 只能路由到境内可用模型 | 从架构上不可能误发 |
| **敏感数据本地预处理** | 入站 PII 脱敏后再出境（§30.10） | 降低出境数据敏感度 |
| **供应商 DPA + SCC** | 签数据处理协议 + 标准合同条款 | 法律层面合规 |
| **可切换** | 模型适配层（第 26 章）保证 1 小时内可切到境内模型 | 监管临时要求时可应急 |
| **审计** | 每次出境调用记 `egress_to` + 模型 + 数据分类 | 出事时能统计影响面 |

---

## 31.4 留存期与删除权：一对天生的矛盾

### 31.4.1 矛盾在哪

```
审计/合规要求：日志留存 ≥ 6 个月（网安法），审计链建议 3 年（诉讼时效），
              高风险 AI 的技术文档可能要留到系统退役后 10 年（AI Act）
                        ▲
                        │  直接冲突
                        ▼
GDPR Art.17：数据主体要求删除，你必须「无不当延迟地删除」
```

**这个矛盾不是靠「选一个」解决的，是靠「分层 + 分离」解决的**：

- **内容**（可识别个人的原文）→ 按删除权处理，可删；
- **元数据/审计事件**（不含原文，只有 id、hash、判定、时间）→ 按合规要求长期保留；
- **两者之间用 hash 关联**，删除内容后审计链依然完整（因为链里存的是 hash，不是原文）。

### 31.4.2 留存策略表

| 数据类别 | 热存 | 温存 | 冷存/WORM | 删除方式 | 依据 |
| --- | --- | --- | --- | --- | --- |
| 会话内容（原文） | 7 天 | 90 天 | 3 年（可选，客户合同） | 加密擦除 | 合同 + 删除权 |
| 事件流（Run/Step/Event） | 30 天 | 180 天 | 3 年 | 加密擦除（内容字段） | 排障 + 诉讼 |
| **审计事件（不含内容）** | 90 天 | 1 年 | **3~7 年（WORM）** | **不可删** | 网安法/等保/AI Act |
| 内容审核判定 | 90 天 | 1 年 | 3 年 | 不可删（元数据） | 合规举证 |
| 计费账本流水 | 1 年 | 3 年 | 7 年（WORM） | 不可删 | 税法/审计（第 27 章） |
| Trace（采样） | 7 天 | 30 天 | — | TTL 自动 | 排障 |
| 指标聚合 | 1 年 | 3 年 | — | TTL | 容量规划 |
| 模型/提示词版本 | 永久 | — | — | 不可删 | 可复现性 |
| 安全事件/取证包 | — | — | 10 年（WORM） | 不可删 | 诉讼时效 |

> 💡 **注意这张表里没有「全部删掉」的选项**。审计事件的「不可删」是设计出来的，不是做不到——因为链里只有 hash，删掉内容也不影响审计完整性。这正是 §31.5 哈希链设计的深层原因：**它让「删内容」和「留证据」不再矛盾**。

### 31.4.3 加密擦除（crypto-shredding）：删除权的工程解法

```
写入时：
  DEK_subject = KMS.GenerateDataKey()        # 每个数据主体（或每个会话）一个数据密钥
  内容 = AES-GCM(DEK_subject, 明文)
  存储：ciphertext + key_id（DEK 本身加密存在 KMS，不落业务库）

删除时：
  KMS.ScheduleKeyDeletion(DEK_subject)        # 删的是密钥，不是密文
  → 密文变成永远解不开的随机字节（密码学意义上等于删除）
  → 但审计链里那条 "CONTENT_WRITTEN" 事件的 hash 依然有效
  → 再写一条 "DELETION_EXECUTED" 事件进链（这条不可删）
```

**为什么不用「物理删除」**：因为备份、WORM 归档、只读副本里的数据你删不掉（WORM 的本意就是删不掉）。对 WORM 里的数据，唯一可行的「删除」就是让它不可解密。

**必须留的删除证据**（否则监管会问「你怎么证明你删了」）：

```json
{
  "event": "DELETION_EXECUTED",
  "subject_id_hash": "sha256:9f2c...",   // 不存原始 subject id
  "request_id": "DSR-2026-09-14-0031",
  "scope": ["session", "content_snapshot"],
  "records_affected": 1284,
  "key_destroyed": "kms://.../dek-subject-8f31",
  "executed_at": "2026-09-15T02:11:33Z",
  "operator": "system:dsr-worker",
  "prev_hash": "…",
  "hash": "…"
}
```

---

## 31.5 不可篡改审计日志：append-only + 哈希链 + WORM

这是本章的技术核心。**三层防护，各挡一种攻击者**：

| 层 | 防谁 | 手段 | 单独使用的问题 |
| --- | --- | --- | --- |
| **应用层 append-only** | 应用 bug、误操作 | 只 INSERT；DB 账号无 UPDATE/DELETE 权限；触发器拒绝修改 | 有 DB 超级权限的人能改 |
| **哈希链** | 内部人改单条记录 | 每条记录的 hash 包含前一条的 hash，改一条要重算后面全部 | 能改「最后 N 条」（截断攻击） |
| **WORM 存储** | 连管理员也改不了 | S3 Object Lock（compliance mode）/ 磁带 / 一次性写入介质 | 贵、慢、不能改错（写错了只能另存） |

### 31.5.1 结构图：链 + 锚定

```
分片链（by hash(tenant_id) % 32，避免单链串行瓶颈）
┌──────────────────────────────────────────────────────────────────┐
│ Chain #07                                                        │
│  ┌────────┐   ┌────────┐   ┌────────┐        ┌────────┐         │
│  │ seq 1  │──▶│ seq 2  │──▶│ seq 3  │──▶ ... │ seq N  │         │
│  │ h=3f.. │   │ h=8a.. │   │ h=c1.. │        │ h=7e.. │         │
│  │ prev=0 │   │prev=3f │   │prev=8a │        │prev=.. │         │
│  └────────┘   └────────┘   └────────┘        └───┬────┘         │
└──────────────────────────────────────────────────┼──────────────┘
                                                   │
        每小时：32 条链的 head hash 组成 Merkle root │
                                                   ▼
                        ┌──────────────────────────────────────┐
                        │  Anchor（外部，不可回写）              │
                        │  ① 独立 AWS 账号的 WORM bucket        │
                        │     （与生产账号不同凭据，compliance） │
                        │  ② RFC 3161 可信时间戳（TSA）          │
                        │  ③ 可选：第三方公证 / 公开账本          │
                        │                                      │
                        │  记录：{hour, merkle_root, sig, ts}   │
                        └──────────────────────────────────────┘

验证逻辑：任何一条历史记录 → 重算到它所在链的 head → 与 Anchor 里的 root 比对
         → 一致 = 该时刻之前的数据未被篡改（含未被截断）
```

**为什么必须「锚定」**：哈希链本身防不住**截断攻击**——攻击者把最后 1000 条删掉，剩下的链依然是自洽的。只有把 head hash 定期送到「攻击者也改不了的地方」，才能证明「链至少有这么长」。

### 31.5.2 审计事件 schema

```sql
-- audit/audit_event.sql
CREATE TABLE audit_event (
    -- ① 定位
    id            BIGSERIAL    PRIMARY KEY,
    chain_id      SMALLINT     NOT NULL,      -- 0..31，按 hash(tenant_id)%32
    seq           BIGINT       NOT NULL,      -- 链内单调递增，无空洞
    event_id      UUID         NOT NULL,      -- 全局唯一（对外暴露这个，不暴露 id）
    ts            TIMESTAMPTZ  NOT NULL,      -- 事件时间（UTC，微秒精度）

    -- ② 谁 / 对谁 / 做了什么
    tenant_id     BIGINT       NOT NULL,
    actor_type    TEXT         NOT NULL,      -- user / service / model / admin / system
    actor_id      TEXT         NOT NULL,      -- user_id / service_name / model_id
    action        TEXT         NOT NULL,      -- RUN_CREATE / LLM_CALL / TOOL_CALL / TOOL_RESULT
                                              -- / AUDIT_DECISION / RETRACTION / DELETION_EXECUTED
                                              -- / CONFIG_CHANGE / KEY_ROTATE / EXPORT / LOGIN
    resource_type TEXT         NOT NULL,      -- run / session / tool / policy / tenant
    resource_id   TEXT         NOT NULL,
    outcome       TEXT         NOT NULL,      -- success / denied / error

    -- ③ 决策与依据（可解释性，见 §31.7）
    policy_id     TEXT,                       -- 命中的策略/规则版本
    decision      TEXT,                       -- allow / degrade / block / require_approval
    risk_score    NUMERIC(5,4),
    reason        TEXT,                       -- 人类可读的一句话（不是自由文本，受控词表）

    -- ④ 内容引用（不存原文！）
    payload_hash  TEXT         NOT NULL,      -- sha256(规范化后的 payload)
    payload_ref   TEXT,                       -- 对象存储 URI（内容快照，可加密擦除）
    payload_bytes BIGINT,
    contains_pii  BOOLEAN      NOT NULL DEFAULT false,

    -- ⑤ 上下文版本（可复现性，见 §31.7）
    model         TEXT,
    model_version TEXT,
    prompt_ver    TEXT,
    rolecard_ver  TEXT,
    toolset_ver   TEXT,
    region        TEXT         NOT NULL,

    -- ⑥ 防篡改
    prev_hash     TEXT         NOT NULL,      -- 前一条的 hash（hex），链首为 64 个 0
    hash          TEXT         NOT NULL,      -- 本条 hash（hex）
    sig           TEXT,                       -- 可选：服务端私钥对 hash 的签名

    -- ⑦ 约束
    UNIQUE (chain_id, seq),
    UNIQUE (event_id)
) PARTITION BY RANGE (ts);

-- 按月分区，便于按时间归档到 WORM
CREATE TABLE audit_event_2026_09 PARTITION OF audit_event
    FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');

CREATE INDEX ON audit_event (tenant_id, ts);
CREATE INDEX ON audit_event (resource_type, resource_id, ts);

-- ★ append-only 的应用层保障 ★
REVOKE UPDATE, DELETE, TRUNCATE ON audit_event FROM app_role;
GRANT INSERT, SELECT ON audit_event TO app_role;

-- 再加一道保险：触发器拒绝任何修改（防 DBA 误操作以外的应用 bug）
CREATE OR REPLACE FUNCTION reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_event is append-only';
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER audit_event_no_update BEFORE UPDATE OR DELETE ON audit_event
    FOR EACH ROW EXECUTE FUNCTION reject_mutation();
```

**事件表的一行长这样**（对外导出的 JSON）：

```json
{
  "event_id": "e-01J8Z9K3M4N5P6Q7R8S9T0",
  "chain_id": 7, "seq": 1834201,
  "ts": "2026-09-14T08:12:33.421Z",
  "tenant_id": 88231,
  "actor_type": "model", "actor_id": "claude-x-2026-01",
  "action": "TOOL_CALL", "resource_type": "tool", "resource_id": "refund.create",
  "outcome": "denied",
  "policy_id": "cap-policy-v14", "decision": "block", "risk_score": 0.9231,
  "reason": "capability_scope_exceeded",
  "payload_hash": "sha256:4b2e...",
  "payload_ref": "s3://aurora-audit-cn/2026/09/14/4b2e.json.gz",
  "contains_pii": false,
  "model": "claude-x", "model_version": "2026-01-15",
  "prompt_ver": "p-37", "rolecard_ver": "rc-12", "toolset_ver": "ts-5",
  "region": "cn-shanghai",
  "prev_hash": "9d31...", "hash": "7e0a..."
}
```

### 31.5.3 哈希链计算（Go，逐行看）

```go
// audit/chain.go
package audit

import (
    "crypto/sha256"
    "encoding/hex"
    "encoding/json"
    "time"
)

// 用于计算 hash 的结构：字段顺序固定，禁止 map，禁止 omitempty（否则字段缺失会改变 hash）
type canonical struct {
    ChainID   int16  `json:"chain_id"`
    Seq       int64  `json:"seq"`
    TsUnixUs  int64  `json:"ts_us"`      // ★ 固定用微秒整数，不用 RFC3339 字符串
    TenantID  int64  `json:"tenant_id"`
    ActorType string `json:"actor_type"`
    ActorID   string `json:"actor_id"`
    Action    string `json:"action"`
    ResType   string `json:"resource_type"`
    ResID     string `json:"resource_id"`
    Outcome   string `json:"outcome"`
    PolicyID  string `json:"policy_id"`
    Decision  string `json:"decision"`
    Risk      int64  `json:"risk_e4"`    // 定点，0.9231 → 9231
    Reason    string `json:"reason"`
    Payload   string `json:"payload_hash"`
    PayloadRef string `json:"payload_ref"`
    Model     string `json:"model"`
    ModelVer  string `json:"model_version"`
    PromptVer string `json:"prompt_ver"`
    PrevHash  string `json:"prev_hash"`
}

func (e *Event) Hash() (string, error) {
    c := canonical{
        ChainID: e.ChainID, Seq: e.Seq, TsUnixUs: e.Ts.UnixMicro(),
        TenantID: e.TenantID, ActorType: e.ActorType, ActorID: e.ActorID,
        Action: e.Action, ResType: e.ResourceType, ResID: e.ResourceID,
        Outcome: e.Outcome, PolicyID: e.PolicyID, Decision: e.Decision,
        Risk: e.RiskE4, Reason: e.Reason,
        Payload: e.PayloadHash, PayloadRef: e.PayloadRef,
        Model: e.Model, ModelVer: e.ModelVersion, PromptVer: e.PromptVer,
        PrevHash: e.PrevHash,
    }
    b, err := json.Marshal(c)     // struct 序列化 → 字段顺序稳定（Go 保证）
    if err != nil { return "", err }
    sum := sha256.Sum256(b)
    return hex.EncodeToString(sum[:]), nil
}

// Append：串行追加到某条链。用「head 表 + 行锁」保证 seq 无空洞、prev_hash 正确。
func (w *Writer) Append(ctx context.Context, ev *Event) error {
    return w.inTx(ctx, func(tx *sql.Tx) error {
        // 1. 锁住这条链的 head（一行），这是唯一的串行点
        var head struct{ Seq int64; Hash string }
        err := tx.QueryRowContext(ctx,
            `SELECT seq, hash FROM audit_chain_head WHERE chain_id=$1 FOR UPDATE`,
            ev.ChainID).Scan(&head.Seq, &head.Hash)
        if err != nil { return err }

        // 2. 接上链
        ev.Seq = head.Seq + 1
        ev.PrevHash = head.Hash
        h, err := ev.Hash()
        if err != nil { return err }
        ev.Hash = h

        // 3. 写入 + 推进 head（同一个事务）
        if _, err := tx.ExecContext(ctx, insertSQL, ev.args()...); err != nil { return err }
        _, err = tx.ExecContext(ctx,
            `UPDATE audit_chain_head SET seq=$1, hash=$2, updated_at=now() WHERE chain_id=$3`,
            ev.Seq, ev.Hash, ev.ChainID)
        return err
    })
}
```

**三个必须注意的实现细节**：

| 细节 | 原因 | 做错的后果 |
| --- | --- | --- |
| **时间戳用整数（UnixMicro）** | RFC3339 字符串有多种等价写法（`Z` vs `+00:00`、小数位数），序列化实现一变 hash 全变 | 半年后你验证不了自己的链 |
| **禁止 `omitempty`** | 字段为空时序列化会省略，导致「空字符串」和「字段不存在」产生同一个 hash | 可以构造 hash 碰撞（弱碰撞） |
| **`seq` 无空洞 + `prev_hash` 来自 DB 而非内存** | 从内存里的「上一条」取会因并发/重试导致分叉 | 出现两条 seq 相同、prev 不同的记录 → 链分叉，无法验证 |

### 31.5.4 验证器：审计链的「体检」

```go
// audit/verify.go
// 全链验证：从 anchor 里最近的一个 root 往回验，直到上一个 anchor
func VerifyRange(ctx context.Context, chain int16, fromSeq, toSeq int64) (*Report, error) {
    rep := &Report{Chain: chain, Checked: 0}
    var prev string
    rows, err := db.QueryContext(ctx,
        `SELECT seq, prev_hash, hash, canonical_bytes(event_id) FROM audit_event
          WHERE chain_id=$1 AND seq BETWEEN $2 AND $3 ORDER BY seq`, chain, fromSeq, toSeq)
    if err != nil { return nil, err }

    for rows.Next() {
        var seq int64; var ph, h string; var raw []byte
        _ = rows.Scan(&seq, &ph, &h, &raw)

        // ① seq 必须连续（防删除）
        if rep.Checked > 0 && seq != rep.lastSeq+1 {
            rep.Break("SEQ_GAP", seq); return rep, nil
        }
        // ② prev_hash 必须等于上一条的 hash（防篡改/防插入）
        if rep.Checked > 0 && ph != prev {
            rep.Break("PREV_MISMATCH", seq); return rep, nil
        }
        // ③ 重算 hash 必须一致（防内容被改）
        sum := sha256.Sum256(raw)
        if hex.EncodeToString(sum[:]) != h {
            rep.Break("HASH_MISMATCH", seq); return rep, nil
        }
        prev = h; rep.lastSeq = seq; rep.Checked++
    }
    rep.OK = true
    return rep, nil
}
```

**什么时候跑验证**：

| 频率 | 范围 | 目的 |
| --- | --- | --- |
| 每小时 | 刚锚定的那个区间 | 尽早发现写入侧 bug（比如序列化变了） |
| 每日 | 昨日全量 | 常规体检，结果进合规看板 |
| 每季度 | 抽样 10 个随机区间（含历史） | 防「长期缓慢篡改」 |
| 协查/诉讼时 | 指定区间 + 对应 anchor | 出具可验证的证据 |

> 🩸 **验证必须能失败**。如果你跑了一年验证器从来没报过错，先怀疑验证器是不是真的在比对——最常见的问题是「重算 hash 时用了当前代码的序列化逻辑，而历史数据是旧逻辑写的」，于是验证永远失败或者永远通过（取决于你怎么处理）。**正确做法是：把 `canonical_bytes` 一起存进去**，验证时直接对存储的字节做 sha256，不再依赖代码版本。这一条能让你的链在 5 年后依然可验证。

### 31.5.5 WORM 存储选型

| 方案 | 不可删强度 | 成本（量级） | 运维复杂度 | 适用 |
| --- | --- | --- | --- | --- |
| **S3 Object Lock（compliance mode）** | ★★★★★ 保留期内**连 root 都删不掉**，也不能改保留期 | 低（标准 ¥0.15/GB/月，可配生命周期转 Glacier） | 低 | **默认首选** |
| S3 Object Lock（governance mode） | ★★★☆☆ 有特殊权限的人可以绕过 | 同上 | 低 | 内部流程够用，但审计时说服力弱 |
| **MinIO + object lock** | ★★★★★（自建） | 中（要自己运维集群） | 中高 | 私有云 / 等保要求数据不出机房 |
| **Azure Blob immutable（time-based retention，locked）** | ★★★★★ | 低 | 低 | Azure 技术栈 |
| **GCS Bucket Lock** | ★★★★★（bucket 级，锁定后不可撤销） | 低 | 低 | GCP 技术栈 |
| **LTO 磁带 / 光盘** | ★★★★★（物理只写） | 极低存储成本 + 高人工成本 | 高 | 超长期（7~10 年）归档 |
| 区块链 / 分布式账本 | ★★★★☆ | 高（写入费 + 复杂度） | 高 | **一般不建议**；除非客户明确要求，用「哈希锚定到公开账本」即可，不必把数据上链 |

**S3 Object Lock 配置要点**（很容易配错）：

```hcl
# terraform: 审计归档桶（独立账号，与生产账号隔离）
resource "aws_s3_bucket" "audit_worm" {
  bucket              = "aurora-audit-worm-cn"
  object_lock_enabled = true          # ★ 只能在创建时开启，事后无法开启
}

resource "aws_s3_bucket_object_lock_configuration" "audit" {
  bucket = aws_s3_bucket.audit_worm.id
  rule {
    default_retention {
      mode = "COMPLIANCE"             # ★ 不是 GOVERNANCE
      days = 2557                     # 7 年
    }
  }
}

resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit_worm.id
  versioning_configuration { status = "Enabled" }   # Object Lock 依赖版本控制
}

# 生命周期：热 90 天 → 冷归档，但保留期不能缩短
resource "aws_s3_bucket_lifecycle_configuration" "audit" {
  bucket = aws_s3_bucket.audit_worm.id
  rule {
    id     = "tier-down"
    status = "Enabled"
    filter {}
    transition { days = 90,  storage_class = "GLACIER_IR" }
    transition { days = 365, storage_class = "DEEP_ARCHIVE" }
    # 不要写 expiration —— WORM 桶不应该有过期删除
  }
}
```

> ⚠️ **三个致命配置错误**：
> ① `object_lock_enabled` 忘了在创建时开启 → 这个桶永远不能开 WORM，只能重建；
> ② 用了 `GOVERNANCE` 模式 → 有 `s3:BypassGovernanceRetention` 权限的人能删，审计时被认为「不具备不可否认性」；
> ③ 归档桶和生产账号在同一个账号里 → 攻击者拿到生产凭证就能删（虽然 compliance mode 下删不掉，但能停掉写入 / 改 IAM）。**归档桶必须在独立账号，用独立的、只写不删的凭据。**

---

## 31.6 AI 生成内容标识与溯源

### 31.6.1 三层标识，缺一层就不完整

| 层 | 载体 | 谁看得到 | 抗篡改 | 成本 | 法律/合规作用 |
| --- | --- | --- | --- | --- | --- |
| **显式标识**（可见） | 文本水印/角标（「AI 生成」）、图片角标、音频提示音 | 所有人 | 低（截图就丢） | 极低 | 满足「显著标识」要求 |
| **隐式标识**（元数据） | C2PA manifest / EXIF / XMP / 文件头 | 软件可读 | 中（可被剥离，但剥离后可检测「缺失」） | 低 | **溯源主力**：谁生成、什么时候、用什么模型、有没有被编辑 |
| **不可见水印** | 图像频域水印、文本 token 采样偏置 | 只有你能读 | 中（图像较强，文本很弱） | 中（图像）/ 高（文本） | 辅助举证，不能作为唯一证据 |

### 31.6.2 C2PA：目前最接近「行业标准」的方案

C2PA（Coalition for Content Provenance and Authenticity）定义了一套**可验证的内容来源凭证**（Content Credentials）：

```
生成时：
  ┌─────────────────────────────────────────────┐
  │ C2PA Manifest                               │
  │  ├─ claim: "此内容由 aurora/gen 生成"        │
  │  ├─ assertions:                             │
  │  │   ├─ c2pa.actions: c2pa.created（含模型）  │
  │  │   ├─ 生成参数（模型版本、提示词 hash）      │
  │  │   └─ 自定义 assertion（租户、run_id）      │
  │  ├─ 内容 hash（绑定到具体像素/字节）           │
  │  └─ 签名：X.509 证书（你的组织）+ 时间戳       │
  └─────────────────────────────────────────────┘
              │ 嵌入
              ▼
        JPEG / PNG / WebP / MP4 / PDF ...
```

**工程上要落的三件事**（很多团队只做了第一件）：

1. **生成时签名**：图片/音视频写入 manifest；文本单独处理（见下）。
2. **编辑后仍然可验证**：用户裁剪/调色后，manifest 应该记录「这是一次编辑」，而不是消失。这依赖 C2PA 的 `ingredient` 机制——**如果你的 pipeline 在转码时会剥掉元数据，这一层就废了**。所以 CDN/图片处理链路必须保留 manifest。
3. **提供验证入口**：给用户/监管一个「上传文件 → 看凭证」的页面。**没有验证入口的标识等于没做**——因为你自己也无法在纠纷中证明。

**文本的困境（必须诚实面对）**：

| 手段 | 效果 | 现实 |
| --- | --- | --- |
| 元数据（文件属性/HTTP header） | 只对「文件」有效 | 用户复制粘贴一段文字，元数据全丢 |
| 可见声明（「本内容由 AI 生成」） | 有效，但可被删除 | 合规上通常够用；也可加在导出文件里 |
| 文本水印（token 采样偏置） | 统计上可检测 | **改一个词、翻译一次、换个模型润色就没了**；且需要密钥才能检测 |
| 平台侧登记 | 你在自己库里记了「这段文本是我们生成的」 | 只能证明「是我们的」，不能证明「这段是」 |

> 🎯 **结论**：**文本标识不要指望「水印」**。正确做法是「可见声明 + 平台侧内容指纹登记 + 输出元数据（API 响应里带 `generated_by` 字段）」三件套。其中**平台侧指纹登记**最有用：把所有生成内容的 hash 存进一张表（配合审计链），当有人拿一段文字说是你生成的，你能查「是不是、什么时候、哪个 Run」。

### 31.6.3 元数据 schema（API 响应里带什么）

```json
{
  "id": "msg_01J8Z...",
  "content": "...",
  "generated_by": {
    "system": "Aurora",
    "provider": "aurora-platform",
    "model": "claude-x",
    "model_version": "2026-01-15",
    "run_id": "cn-shanghai:01J8Z9...",
    "generated_at": "2026-09-14T08:12:33Z",
    "content_hash": "sha256:4b2e...",
    "c2pa": null,
    "disclosure": "ai-generated",
    "human_reviewed": false
  }
}
```

---

## 31.7 可解释性记录：凭什么这么答

「可解释」在 Agent 场景里不是指「模型的注意力权重」（那玩意儿对监管和法务没有意义），而是指：**给定一次 Run，你能不能用人类可读的方式重建「它当时看到了什么、依据什么规则、做了什么判断」**。

### 31.7.1 一个 Run 的「决策包」

```
Run: cn-shanghai:01J8Z9K3M4N5
├── 输入快照
│   ├── 用户输入原文（加密，payload_ref）
│   ├── 规范化后的版本（hash）
│   └── 附件清单 + 每个附件的 hash + 提取出的文本 hash
├── 上下文快照（★ 最关键，也最贵）
│   ├── 实际送进模型的完整 prompt（含 system / 历史 / 检索片段）
│   ├── 检索命中：doc_id + doc_version + 分数（前 10 条）
│   └── 记忆读取：memory_id + 写入时间
├── 版本指纹
│   ├── model + model_version
│   ├── prompt_version / rolecard_version / toolset_version
│   ├── 路由策略版本 / 索引版本
│   └── 配置版本（策略中心快照 id）
├── 决策轨迹
│   ├── 每步：意图判定、工具选择、参数（含被服务端重写的值）
│   ├── 审核判定：risk_score + labels + policy_id + 命中规则
│   └── 降级/拦截的原因码（受控词表，非自由文本）
└── 人工干预
    ├── 审批人 / 时间 / 意见
    └── 撤回记录（原因、范围、通知情况）
```

**成本控制是这里的关键**：上下文快照是 12k token ≈ 48KB/次，800 万 Run/天 = 384GB/天。所以：

| 策略 | 做法 | 覆盖率 | 成本 |
| --- | --- | --- | --- |
| A 全量快照 | 每次都存 | 100% | 384GB/天（压缩后 ~96GB/天） |
| B 条件快照 | 只在「有决策/有工具调用/风险分 > 0.3/金额 > 阈值」时存 | ~25% | 96GB/天 |
| **C 分层（推荐）** | 全量存「摘要指纹」（hash + 长度 + 结构），按条件存全量快照，**高风险场景强制全量** | 100% 指纹 + 25% 全文 | ~100GB/天 |

> ⚠️ **高风险场景（金融、医疗、招聘、法务）必须选 A**。监管问「当时模型看到了什么」时，「我们只存了 hash」是不能接受的答案——除非你能证明 hash 对应的原文还在别处。

### 31.7.2 决策记录的可读性：给受控词表，不给自由文本

```
❌ reason: "模型认为这个请求有点风险，所以拒绝了"
   → 无法统计、无法审计、无法训练

✅ reason: "capability_scope_exceeded"  + policy_id: "cap-policy-v14"
   → 可聚合、可比对、可回归测试
```

受控词表大约 30~50 个码就够，例如：`pii_inbound_detected` / `capability_scope_exceeded` / `content_policy_hit:violence` / `rate_limit_exceeded` / `upstream_timeout` / `human_approval_required` / `retracted_by_policy_change`。

---

## 31.8 监管协查与内部取证：一份能直接用的流程

### 31.8.1 七个步骤 + SLA

```
 ① 接收与登记（0~2h）
    ├─ 收到函件/工单 → 法务登记（案号、范围、时限、联系人）
    ├─ 判断性质：监管协查 / 用户投诉 / 诉讼 / 内部调查 / 数据泄露
    └─ ★ 立刻做「证据保全」：冻结相关数据的 TTL ��删除任务，禁止任何清理作业
       （这一步做晚了，后面全是空的）

 ② 影响面界定（2~8h）
    ├─ 按 tenant_id + 时间窗 + 场景/标签，统计：会话数、Run 数、用户数
    ├─ 判断是否涉及个人信息 / 敏感个人信息 / 未成年人
    └─ 输出《影响面初判表》给法务

 ③ 证据固定（8~24h）
    ├─ 导出：审计事件（含 hash 链）+ 内容快照 + 模型/版本指纹 + 审核判定
    ├─ 对导出物做整体 hash（sha256），并写入审计链（EXPORT 事件）
    ├─ 生成《完整性证明》：区间 anchor + 验证报告
    └─ 存证：写入独立 WORM 桶（取证专用前缀，保留 10 年）

 ④ 数据最小化裁剪（24~48h）
    ├─ 只保留协查范围内的数据，其他租户/用户的数据必须剔除
    ├─ 涉及第三方的个人信息 → 脱敏或单独说明
    └─ ★ 这一步不能省：超范围提供数据本身就是一次违规

 ⑤ 交付（48~72h）
    ├─ 格式：CSV/JSON + PDF 说明 + 校验清单（每份文件的 sha256）
    ├─ 渠道：监管指定渠道（加密传输 + 密码另送）
    └─ 留档：交付物本身 + 交付回执 也要进 WORM

 ⑥ 归档与复盘（T+7）
    ├─ 完整卷宗归档：函件、影响面、证据、裁剪记录、交付物、回执
    └─ 复盘：这次的哪些数据「查不到 / 查得慢 / 不完整」→ 改进项

 ⑦ 后续跟踪
    ├─ 监管可能有二次问询（往往更细）
    └─ 保留取证包，直到明确结案
```

### 31.8.2 取证的四个原则（写进 runbook）

| 原则 | 含义 | 具体动作 |
| --- | --- | --- |
| **合法性** | 取证过程本身要合法 | 走审批流；涉及员工个人数据要告知；跨境取证要走合法路径 |
| **完整性** | 证明「交出去的东西没被动过」 | 每个文件 sha256 + 整体 Merkle root + 审计链 anchor + RFC 3161 时间戳 |
| **可重复** | 同一个查询，任何人再跑一次得同样结果 | **查询语句也要归档**（SQL + 参数 + 数据快照版本），而不是「我筛了一下」 |
| **链式保管**（chain of custody） | 谁在什么时候碰过这份证据 | 每一次交接记录：人、时间、动作、hash 前后一致性 |

### 31.8.3 取证查询模板

```sql
-- 场景：某租户某时间窗内含「投资建议」类内容的 Run 清单
-- 注意：查询本身要归档（作为证据的一部分）
WITH hits AS (
    SELECT e.resource_id AS run_id, e.ts, e.event_id,
           e.model, e.model_version, e.prompt_ver, e.decision, e.risk_score
      FROM audit_event e
     WHERE e.tenant_id = $1
       AND e.ts >= $2 AND e.ts < $3
       AND e.action = 'AUDIT_DECISION'
       AND 'investment_advice' = ANY(e.labels)     -- 假设 labels 是 text[]
)
SELECT h.*, s.payload_ref, s.payload_hash
  FROM hits h
  LEFT JOIN content_snapshot s ON s.run_id = h.run_id
 ORDER BY h.ts;
```

### 31.8.4 一个容易漏掉的取证对象：**配置的历史版本**

监管问「为什么它会这么答」时，最常见的答案是「因为当时的路由策略把这类问题路由到了那个模型」。而策略是**热更新**的（第 43 章）。如果你不存策略中心的历史版本，你永远无法解释一个月前的行为。

```
必须版本化并长期保留（不可删）：
  · 提示词模板（每次变更的全文 + hash + 生效时间）
  · 角色卡
  · 工具集定义 + MCP server 版本
  · 路由策略 / 限流阈值 / 降级阶梯
  · 审核阈值与规则
  · 模型版本映射表
```

每一条审计事件里的 `prompt_ver` / `policy_id` 都指向这些版本。**这条链断了，可解释性就是零。**

---

## 31.9 审计采样与成本：算清这笔账才不会砍错预算

### 31.9.1 什么必须全量，什么可以采样

| 数据类型 | 采样策略 | 理由 |
| --- | --- | --- |
| **审计事件**（谁做了什么决策） | **全量，100%** | 合规硬要求；缺一条就无法证明「完整」 |
| **内容审核判定** | **全量** | 举证核心；也是模型迭代的训练数据 |
| **计费流水** | **全量** | 资金（第 27 章） |
| 内容快照（上下文全文） | 分层：高风险全量、常规 25%、闲聊 5% | 成本大头；但高风险不能省 |
| Trace（span） | 采样 5%~10% + **错误/慢请求 100%** | 排障够用；尾采样保证异常不丢 |
| 指标 | 聚合，不需要全量 | — |
| 日志（应用日志） | 1%~10% | 排障用，不是证据 |

### 31.9.2 成本算式（Aurora 真实量级）

```
审计事件：
  800 万 Run/天 × 8 条事件 = 6,400 万条/天
  单条 600B（含索引约 1.5KB）→ 96GB/天
  3 年 = 105TB

内容快照（按策略 C，25% 全量）：
  800 万 × 25% × 48KB = 96GB/天（压缩 4:1 → 24GB/天）
  3 年 = 26TB

合计 3 年 ≈ 131TB

分级存储成本（示例单价，按你自己的云账单）：
  热（90 天，标准存储 ¥0.15/GB/月）
     审计 (96×90/1024)TB=8.4TB=8,600GB × 0.15 = ¥1,290/月
     快照 (24×90/1024)TB=2.1TB=2,100GB × 0.15 = ¥  315/月
  温（1 年，低频 ¥0.08/GB/月）
     约 (96+24)×275/1024 = 32TB = 32,000GB × 0.08 = ¥2,560/月
  冷（3 年，归档 ¥0.033/GB/月）
     约 131TB - 40TB = 91TB = 91,000GB × 0.033 = ¥3,003/月
  ─────────────────────────────────────────────────
  合计 ≈ ¥7,200/月 ≈ ¥8.6 万/年
```

**这个数字要和什么比**：

| 对比项 | 金额 |
| --- | --- |
| 3 年审计留存 | **¥26 万**（3 年合计） |
| 一次监管罚款（下限） | ¥50 万起 |
| 一次数据泄露事故的直接损失 | ¥500 万起 |
| 一个大客户因为「你们过不了 SOC 2」流失 | 年合同额 × 3 = ¥300 万起 |

> 🎯 **结论：审计留存是这个平台上性价比最高的基础设施。** 它占平台总成本（¥7,200 万/月）不到 0.01%。**任何试图在这里省钱的提案，都应该被打回去**——包括「日志太多了我们降采样到 10%」这种看起来很专业的建议。

### 31.9.3 成本优化的正确姿势

不是「少存」，而是「 smarter 地存」：

| 手段 | 效果 | 风险 |
| --- | --- | --- |
| 压缩（zstd，审计事件 JSON 4~6:1） | 存储 -75% | 无 |
| 列式存储（ClickHouse/Parquet 存冷数据） | 查询快、压缩率更高 | 需要 ETL |
| 分级生命周期（热→温→冷） | -60% | 冷数据取回有延迟（分钟级），协查时要等 |
| 内容快照按策略采样（C 方案） | -75% | **高风险场景必须全量**，别一刀切 |
| 审计事件只存 hash 不存 payload | 已经这么做了（§31.5.2） | 无 |
| ❌ 降审计事件的采样率 | — | **不合规**，别做 |

---

## 31.10 Java / 分布式经验对照

| 你熟悉的 | Agent 合规审计里 | 会咬人的差异 |
| --- | --- | --- |
| `@Transactional` + binlog | 审计事件流 | 🔴 binlog 记的是「数据变了」，不是「谁基于什么决策让它变」。合规要后者 |
| 业务操作日志表（`op_log`） | 审计事件表 | 🟡 差别在 append-only + 哈希链 + 不可删。普通业务表做不到「证明没被改」 |
| Spring AOP 切面记日志 | 审计埋点 | 🔴 切面只拿得到方法参数，拿不到「模型版本 / 提示词版本 / 上下文快照 / 审核判定」 |
| ShardingSphere 分库分表 | 审计链分片 | 🔴 **哈希链不能跨分片**：每条链必须在单库单表内串行，否则 seq/prev_hash 会分叉。按 `hash(tenant_id)%32` 分，每条链内部完整 |
| Kafka 不可变日志 | 事件流 | 🟢 思路对，但 Kafka 的 retention 会删数据，且没有 hash 链。只适合做「传输层」，不适合做「证据层」 |
| Hibernate Envers（审计版本表） | 变更审计 | 🟡 只覆盖 DB 变更，覆盖不了 LLM 调用/工具调用/审核判定 |
| 归档到 S3（普通桶） | WORM 归档 | 🔴 普通桶 = 能删。必须 Object Lock + compliance mode + 独立账号 |
| MDC / TraceID 贯穿 | `run_id` 贯穿 | 🟢 完全一致，这是最值钱的习惯迁移 |
| GDPR 的 DSR（数据主体请求）流程 | 删除权/访问权 | 🟡 传统系统的删除 = DELETE 行；这里要「加密擦除 + 保留审计元数据」 |

---

## 31.11 开源坐标

| 项目 | 是什么 | 能抄什么 | 缺什么 / 坑 |
| --- | --- | --- | --- |
| **C2PA**（`c2pa-rs`、`c2pa-python`） | 内容来源凭证的开放规范 + 参考实现 | 生成时签名、编辑后保留 manifest 的完整机制 | 文本支持有限；需要证书体系（X.509）；转码链路要专门处理 |
| **RFC 3161 时间戳**（OpenSSL `ts`、各类 TSA 服务） | 可信时间戳 | 给 anchor 加「第三方证明的时间」 | 需要信任一个 TSA；有成本 |
| **Trillian**（Google 开源） | 可验证日志（Merkle tree），CT 的底层 | 大规模 append-only + Merkle 证明的工程范式 | 偏「证书透明度」场景，接入成本高；小规模用「链 + anchor」就够 |
| **immudb**（Codenotary） | 带密码学验证的不可变数据库 | 内建 Merkle 树与验证 API，省掉自己写链 | 生态较小；要评估长期维护与性能 |
| **Sigstore / cosign** | 签名与透明日志（Rekor） | 配置/镜像/审计 anchor 的签名与公开记录 | 主要面向制品签名，不是审计事件 |
| **OpenTelemetry** | 统一 trace/metrics/log | `gen_ai.*` 语义约定、trace 贯穿 | **不是审计系统**：可采样、可丢、可改，不能当证据 |
| **Langfuse / LangSmith / Phoenix** | LLM 可观测 | 提示词版本、成本、判定的可视化 | 定位是「调试与评估」，不是「合规取证」；数据可删可改，没有哈希链与 WORM |
| **OpenPolicyAgent (OPA)** | 策略引擎 | 把「谁能做什么」变成可版本化、可审计的 policy | 只管决策，不管记录；决策日志要自己接审计链 |
| **各类 DLP / 审计 SaaS** | 商业方案 | 省事 | 数据出境与驻留要评估（§31.3），很多 SaaS 的存储区不在你要的区域 |

> 🎯 **一句话**：**开源能给你「链」，给不了你「锚」和「流程」。** 哈希链用 300 行 Go 就能写完（§31.5.3），Trillian/immudb 是「更工程化」的版本；但真正的难点是「独立账号的 WORM」「72 小时协查的 runbook」「配置版本的长期保留」——这些没有开源项目能替你做完。

---

## 31.12 事故清单

- [ ] **事故 A：TTL 把证据删了** → 现象：协查要 6 天前的数据，ClickHouse TTL 30 天但对某个表配成了 7 天。→ 兜底：**留存策略集中配置 + 变更需审批 + 定期校验实际 TTL 与策略一致**（`SHOW CREATE TABLE` 巡检）
- [ ] **事故 B：审计表有 UPDATE 权限** → 现象：审计时发现 `updated_at` 变了，无法自证未被篡改。→ 兜底：`REVOKE UPDATE/DELETE` + 触发器拒绝 + 应用账号最小权限（§31.5.2）
- [ ] **事故 C：哈希链被截断** → 现象：攻击者删掉最后 200 条，链依然自洽。→ 兜底：**每小时 anchor 到独立账号 WORM + RFC 3161 时间戳**（§31.5.1）
- [ ] **事故 D：序列化变化导致链不可验证** → 现象：升级 Go 版本后 `json.Marshal` 行为变化，历史 hash 全部对不上。→ 兜底：**把 `canonical_bytes` 一起存进去**，验证时对存储字节做 hash，不依赖代码版本
- [ ] **事故 E：WORM 桶配成 GOVERNANCE 模式** → 现象：审计认为不具备不可否认性，SOC 2 出例外项。→ 兜底：Terraform 里硬编码 `COMPLIANCE`，并加 conftest 检查
- [ ] **事故 F：归档桶与生产同账号** → 现象：攻击者拿到生产凭证后改了归档写入路径，后续证据缺失。→ 兜底：独立账号 + 独立凭据 + 只写不删的 IAM policy
- [ ] **事故 G：配置版本没留** → 现象：被问「为什么当时路由到那个模型」，答不出（策略已热更 3 次）。→ 兜底：策略中心全量版本化 + 不可删 + 审计事件带 `policy_id`（§31.8.4）
- [ ] **事故 H：跨境把日志集中了** → 现象：为排障方便，全球日志汇总到一个 ELK，被认定构成数据出境。→ 兜底：日志按 region 隔离，只在全局汇总脱敏后的指标（§31.3.1）
- [ ] **事故 I：删除请求执行了但没证据** → 现象：用户投诉「你们没删」，你无法证明删了。→ 兜底：加密擦除 + `DELETION_EXECUTED` 事件进链（§31.4.3）
- [ ] **事故 J：AI 标识被转码链路剥掉** → 现象：用户二次上传后 C2PA manifest 消失，无法溯源；或反过来被指「这是你们生成的」而你无法否认。→ 兜底：转码保留 manifest + 平台侧内容指纹登记表（§31.6.2）
- [ ] **事故 K：协查超时** → 现象：72 小时到了，只交了 60%。→ 兜底：把 §31.8.1 做成 runbook，**每季度演练一次**（演练是唯一能让流程真正可用的方法）

---

## 31.13 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 三个能力 | 留得够久 × 改不了 × 查得出，乘法关系，任一为 0 则整体为 0 |
| 监管地图 | AI Act（分级+记录义务）/ 生成式 AI 办法（内容标识+协查）/ 网安法（日志≥6 月）/ GDPR（删除权+72h 通知）/ 等保 / SOC 2 |
| 数据驻留 | 租户绑 region 且不可改；`run_id` 带 region 前缀；全局只放聚合数字 |
| 最大出境风险 | 第三方模型 API——用「区域模型白名单」从架构上禁止误发 |
| 删除 vs 审计 | 靠**分层**解决：内容可删（加密擦除），审计元数据不可删（只有 hash） |
| append-only 三层 | 应用权限（REVOKE+触发器）/ 哈希链（防改单条）/ WORM（防管理员） |
| 哈希链实现 | 时间戳用整数、禁 `omitempty`、`prev_hash` 从 DB 取、**存 canonical_bytes** |
| 截断攻击 | 链防不住「删最后 N 条」→ 必须**每小时 anchor 到独立账号 WORM + RFC 3161** |
| WORM 选型 | S3 Object Lock **compliance** mode + 独立账号 + 创建时开启 |
| AI 标识 | 显式 + 隐式（C2PA）+ 水印；**文本别指望水印**，靠可见声明 + 平台侧指纹登记 |
| 可解释性 | 决策包 = 上下文快照 + 版本指纹 + 决策轨迹；**配置历史版本也必须长期保留** |
| 取证四原则 | 合法性 / 完整性（hash）/ 可重复（查询也归档）/ 链式保管 |
| 采样 | 审计事件与计费**全量**；trace 5~10%；内容快照分层（高风险全量） |
| 成本 | 3 年留存约 ¥26 万；一次罚款/泄露/丢单是它的 10~100 倍 |
| 一句话 | **合规不是文档工作，是「你能证明自己没改过」。** |

## 31.14 自检题

1. **你的审计日志表有主键、有时间戳、有索引，为什么还不能叫「审计」？**
   > 要点：缺三样东西。① **append-only 的强制**：有 UPDATE 权限的表，任何一条记录都可能被改，你无法自证；② **完整性证明**：没有哈希链，改一条记录不会留下痕迹；没有外部 anchor，整段删除也不会被发现；③ **可复现的上下文**：只记「发生了什么」不够，还要记「凭什么这么决定」——模型版本、提示词版本、策略版本、审核判定、上下文快照。三者缺一，这份数据在监管面前就只是「日志」，不是「证据」。

2. **GDPR 的删除权和「审计日志留存 3 年」怎么同时满足？**
   > 要点：靠数据分层 + 加密擦除。① 审计事件里**不存内容原文**，只存 `payload_hash` 和对象存储引用；② 内容用「每个数据主体一个 DEK」加密，删除请求执行时**销毁 DEK**（crypto-shredding），密文变成不可解密的随机字节，等效于删除，且备份/WORM 里的副本也一并失效；③ 同时写一条 `DELETION_EXECUTED` 事件进哈希链，作为「我们确实删了」的证据。这样「内容被删」和「审计链完整」同时成立。

3. **哈希链已经能防篡改了，为什么还需要 WORM 和外部锚定？分别防什么？**
   > 要点：三者防的攻击者不同。① **哈希链**防「改单条记录」——改一条要重算后面全部，成本高且 head 会变；② **WORM** 防「有管理员权限的人整体重写/删除」——compliance mode 下连 root 都删不掉，且保留期不可缩短；③ **外部锚定**防「**截断**」——攻击者删掉最后 N 条，剩下的链完全自洽，哈希链和 WORM 都发现不了（因为你没写过那些记录）。只有把 head hash 定期送到独立账号/第三方时间戳，才能证明「链至少有这么长」。所以顺序是：链保证内部一致，WORM 保证存储不可改，锚定保证长度不可缩。

## 31.15 延伸阅读

- 上一章：[30. 内容安全与注入防御](30-injection-content-safety.md)
- 下一章：[32. 防御工程：防破解、防薅羊毛、防蒸馏](32-defense-hardening.md)
- 相关：[第 6 章](6-session-run-event-model.md)（事件溯源，审计事件的天然载体）、[第 27 章](27-billing-reliability.md)（计费流水的不可变账本）、[第 39 章](39-observability.md)（trace 与审计的边界）、[第 43 章](43-policy-center.md)（配置版本化，可解释性的前提）、[第 45 章](45-storage-architecture.md)（存储选型与冷热分层）、[第 37 章](37-output-audit-retraction.md)（撤回记录也是审计事件）
- 总纲：[0-summary.md](0-summary.md)
- 外部：EU AI Act 官方文本（Regulation (EU) 2024/1689）与欧盟委员会实施指南；《生成式人工智能服务管理暂行办法》《互联网信息服务深度合成管理规定》《人工智能生成合成内容标识办法》；《网络安全法》第二十一条；GDPR 官方文本（Art.15/17/22/30/33）；AICPA Trust Services Criteria；GB/T 22239-2019；C2PA 规范与 `c2pa-rs`；AWS S3 Object Lock 文档；RFC 3161。**所有法规条文与生效时间以官方最新发布为准。**
