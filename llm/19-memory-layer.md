# 19. 记忆层：从「存对话」到「记得对」

> **状态**：`已完成（初稿）` ｜ **所属**：第四部分 · 上下文工程（Context Engineering） ｜ **编号**：19 / 52
> **本章定位**：RAG 解决"公司知道什么"，记忆解决"这个用户是谁、我们之间发生过什么"。这一章的核心不是"存"，而是**"写什么、信谁、什么时候忘"**——记忆是唯一一个"写错一次，后面全都错"的上下文来源。
> **读法**：§19.4.1 的四类记忆表和 §19.4.4 的冲突消解规则是本章骨架；§19.4.3 的 SQL 可以直接拿去建表。

---

## 19.1 引子：一句「按上次那个地址」，把货寄到了三个月前

用户：「帮我把上次那个订单的收货人改成我妈，地址还是上次那个。」

Agent 很自信地答：「好的，已按您上次的地址（**杭州市西湖区文三路 XXX 号**）修改。」

用户没细看就确认了。三天后货到了杭州，人已经在深圳——**客户三个月前搬过一次家**，而 Agent 记忆里的"上次那个地址"是三个月前那单的地址，中间还有一单用了新地址，但那条记忆被后来的一次"模型推断"覆盖掉了。

拆开这条记忆的写入历史，问题一目了然：

| 时间 | 记忆值 | 来源 | 置信度 | 发生了什么 |
| --- | --- | --- | --- | --- |
| 03-02 | 杭州文三路 | `tool:order.query` | 0.95 | 正常写入 |
| 05-18 | 深圳南山 | `user:msg_8842`（"我搬家了"） | 0.80 | 正常写入，但**旧值没被标记失效** |
| 05-19 | 杭州文三路 | `infer:run_91f3`（模型从"寄回老家"推断） | 0.55 | **低置信推断覆盖了高置信用户陈述** |

三个错误叠在一起：

1. **没有"当前有效值"的概念**——两条地址同时"有效"，检索按相似度返回，返回了错的那条。
2. **置信度没有参与裁决**——0.55 的推断干掉了 0.80 的显式陈述。
3. **模型推断被当成事实存了**——这是记忆污染的头号来源。

账算一下：Aurora 日均 60 万 Run，20% 需要读记忆（12 万/天），记忆相关错误率 0.6%：

```
720 次/天 × (人工纠正 12 元 + 赔付均值 40 元) ≈ 8,640 元/天
≈ 26 万/月
```

而一个做得还行的记忆层，成本大约是这套数字的 5%。**这就是为什么"记忆"必须当成一个有 schema、有仲裁、有 TTL 的子系统，而不是"把对话存进向量库"。**

---

## 19.2 断崖：四类记忆，四类事故

大多数团队对"记忆"的理解停留在"把历史对话 embedding 一下存起来"。这么做的直接后果是：**存了很多、记得很乱、错得很自信**。

| # | 断崖 | 本质 | 典型症状 | 量化判据 |
| --- | --- | --- | --- | --- |
| 1 | **没有类型划分** | 偏好、事实、情景混在一个向量库里 | 语义召回把"我说过想换个方案"当成"我要退款" | 你的记忆表没有 `kind` 字段 |
| 2 | **写入无节制** | 每轮都存全量对话 | 存储 3 个月涨 40 倍；召回全是噪声 | 记忆条数 / Run 数 > 3 |
| 3 | **冲突无仲裁** | 新旧值并存，靠相似度碰运气 | 用旧地址、引用旧政策 | 同一 `(subject, predicate)` 有多条"有效"记录 |
| 4 | **推断当事实** | 模型猜的东西被持久化 | "客户偏好电话沟通"其实是模型自己编的 | 记忆里 `source` 有 `infer:` 且 `confidence < 0.7` |
| 5 | **忘了要忘** | 无 TTL、无删除权 | 合规审计过不了；存储成本只增不减 | 你说不出"这条记忆什么时候该消失" |
| 6 | **多实例读旧** | 刚写就读不到 / 不同副本不一致 | 用户刚说完，下一句 Agent 又忘了 | 写入后 500ms 内读到的概率 < 95% |

### 19.2.1 账本：记忆的成本与收益

记忆不是免费的。三条成本线必须同时看：

| 成本项 | 量级（Aurora） | 说明 |
| --- | --- | --- |
| **存储** | 情景记忆 ≈ 1.2KB/条（摘要 + 向量），12 万条/天 → 43GB/年 | 向量索引通常是原数据 1.5~2 倍 → 实际 ~100GB/年 |
| **读放大** | 每次注入 600~1,200 token 记忆 | 12 万次/天 × 900 = 108M token/天 ≈ $324/天（走缓存后 ~$80） |
| **写放大** | 抽取用小模型，一次 Run 一次 | 60 万 × 1.5k token = 900M token/天，$0.14/M → $126/天 |

收益侧：记忆让"重复问同一件事"的比例从 22% 降到 7%，每次省一轮 4k token ≈ $0.012 → **60 万 × 15% × 0.012 = $1,080/天**。

所以记忆的 ROI 是正的，但**前提是"写得对"**。写错一条事实的代价不是 100 token，而是后面所有基于它的回答（第 19.1 节那笔 26 万/月）。

> 🎯 一句话原则：**记忆的写入要像数据库写入一样严肃（schema + 约束 + 事务），读取要像缓存一样宽容（多路召回 + 打分 + 可丢弃）。**
> 九成团队做反了：写入随手一存，读取当成真理。

---

## 19.3 结构图：四类记忆 + 双通道写入 + 三路读取

```
                        ┌──────────────── Run 结束事件 ────────────────┐
                        │                                              │
              ┌─────────▼──────────┐                    ┌──────────────▼─────────────┐
              │ 同步通道 (<20ms)    │                    │ 异步通道 (1~3s)             │
              │ 关键事实：          │                    │ 抽取器（小模型）            │
              │  订单/地址/金额/合同│                    │  候选事实 / 偏好 / 情景摘要 │
              │ 走业务工具返回，     │                    │  带 source + confidence     │
              │ 强一致，直接写主库   │                    └──────────────┬─────────────┘
              └─────────┬──────────┘                                   │
                        │                                     ┌────────▼─────────┐
                        │                                     │ 冲突消解（§19.4.4）│
                        │                                     │ Accept/Reject/    │
                        │                                     │ Conflict          │
                        │                                     └────────┬─────────┘
                        │              ┌───────────────────────────────┘
                        ▼              ▼
        ┌───────────────────────────────────────────────────────────────┐
        │  记忆存储（按 subject_id 分区，单写者）                          │
        │  ① fact       关系表 + 部分唯一索引（当前有效值唯一）            │
        │  ② preference KV                                              │
        │  ③ episode    向量（HNSW）+ 时间索引                            │
        │  ④ edge       图（可选，>2 跳才上）                             │
        └───────────────────────────────┬───────────────────────────────┘
                                        │ 主库写 → 从库读（延迟 <200ms）
                                        ▼
   Query ──▶ ① 事实精确查（主库，强一致，带 min_version）
         ──▶ ② 情景向量召回 top-k（从库，ANN）
         ──▶ ③ 偏好 KV（进程内缓存 TTL 30s）
                                        │
                                        ▼
                          融合打分 → 预算裁剪 → 注入 P2 段
                          score = 0.5·sim + 0.3·recency + 0.2·importance
                                        │
                                        ▼
                                   LLM 上下文
```

两条设计判断，直接决定成败：

1. **写分两路**。"订单地址"这种错了要赔钱的事实，必须在工具调用成功时**同步落主库**，不能等 Run 结束异步抽取——中间那 3 秒可能就是你回答下一句的时间窗。而"用户偏好简短回答"这种错了无所谓的，异步慢慢来。
2. **读要三路且可丢弃**。任何一路超时或报错，都必须能降级为"这次不带记忆"（fallback 到无记忆），而不是整条链失败。记忆是增强，不是依赖。

---

## 19.4 关键设计

### 19.4.1 四类记忆：存什么、存哪、活多久

这是本章的骨架表。**四类的差别不在"格式"，在"谁能写、谁信、怎么失效"**。

| 类型 | 存什么 | 存储 | 生命周期 | 写入时机 | 读取方式 | 典型坑 |
| --- | --- | --- | --- | --- | --- | --- |
| **短期（工作记忆）** | 当前 Run 的中间结论、工具结果摘要、计划 | 进程内 + Run 事件流 | 秒~分钟（Run 结束即弃） | 每步 | 直接拼装（P2） | 被误当长期记忆写入库 |
| **长期（偏好/画像）** | 沟通风格、语言、关注点、"不喜欢被推销" | KV + 向量 | 月~年（TTL 12 个月无交互） | Run 结束异步 | 语义召回 top-3 | 无来源的偏好（模型瞎猜） |
| **事实（Fact）** | 订单号、地址、合同条款、账户等级 | **关系表 + 唯一约束** | 跟随业务实体 | **确认后/工具返回时同步** | 精确查（key-value） | 冲突不消解、旧值不失效 |
| **情景（Episodic）** | "5 月处理过退款工单 X，结论是 Y" | 向量 + 时间索引 | 周~月（TTL 90 天） | Run 结束异步 | 时间窗 + 语义混合 | 相似但过时，被当成现状 |

三条实操结论：

- **事实永远不要靠向量召回**。地址、金额、订单号是**精确查询**问题，用向量召回"语义上像的地址"是设计错误。这条能消掉一半的记忆事故。
- **偏好要带来源**。`preferred_lang=ja` 来自"用户三次用日语提问"是可信的；来自"模型觉得他像日本人"是不可信的。**没有 `source` 的偏好不许入库。**
- **情景要带时间**。注入时必须带 `happened_at`，并且 prompt 里写明"这是历史，不是现状"。否则模型会把"上个月的政策"当成本月政策。

### 19.4.2 记忆表结构（PostgreSQL + pgvector，可直接建）

```sql
-- ① 事实记忆：部分唯一索引保证「同一主语+谓词只有一个当前有效值」
CREATE TABLE memory_fact (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     TEXT        NOT NULL,
  subject_id    TEXT        NOT NULL,        -- user:u_123 / org:o_9 / ticket:t_77
  predicate     TEXT        NOT NULL,        -- shipping_address / plan / vip_level
  object_json   JSONB       NOT NULL,
  confidence    REAL        NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  source        TEXT        NOT NULL,        -- tool:order.query / user:msg_8842 / infer:run_91f3
  source_run    TEXT        NOT NULL,        -- 可回溯到 Run（第 39 章）
  valid_from    TIMESTAMPTZ NOT NULL DEFAULT now(),
  valid_to      TIMESTAMPTZ,                 -- NULL = 当前有效
  recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 双时态：当时「系统知道什么」
  superseded_by BIGINT REFERENCES memory_fact(id),
  ttl_at        TIMESTAMPTZ,
  CHECK (valid_to IS NULL OR valid_to > valid_from)
);

-- 核心约束：同一 (租户, 主语, 谓词) 只允许一条当前有效值
CREATE UNIQUE INDEX uq_fact_current
  ON memory_fact (tenant_id, subject_id, predicate)
  WHERE valid_to IS NULL;

CREATE INDEX idx_fact_lookup ON memory_fact (tenant_id, subject_id, predicate, valid_to);

-- ② 情景记忆：向量 + 时间双索引
CREATE TABLE memory_episode (
  id          BIGSERIAL PRIMARY KEY,
  tenant_id   TEXT        NOT NULL,
  subject_id  TEXT        NOT NULL,
  run_id      TEXT        NOT NULL,
  summary     TEXT        NOT NULL,            -- 100~200 token，不是原文
  entities    TEXT[]      NOT NULL,            -- 用于图边 / 过滤
  happened_at TIMESTAMPTZ NOT NULL,
  importance  REAL        NOT NULL DEFAULT 0.5,
  embedding   vector(1024) NOT NULL,
  ttl_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_ep_vec  ON memory_episode USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_ep_time ON memory_episode (tenant_id, subject_id, happened_at DESC);

-- ③ 图记忆（可选）：只在需要 >2 跳关系时才上，别一上来就 Neo4j
CREATE TABLE memory_edge (
  tenant_id  TEXT NOT NULL,
  src        TEXT NOT NULL,          -- user:u_123
  rel        TEXT NOT NULL,          -- member_of / reports_to / owns
  dst        TEXT NOT NULL,          -- org:o_9
  weight     REAL NOT NULL DEFAULT 1.0,
  last_seen  TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (tenant_id, src, rel, dst)
);
```

三个关键设计点，每个都有明确的"不做会怎样"：

1. **`WHERE valid_to IS NULL` 的部分唯一索引**。这是把"冲突消解"下沉到数据库层。哪怕你的应用代码写错了，数据库也不允许并存两条当前有效地址。**没有这条约束，冲突消解就只是"建议"。**
2. **双时态（`valid_from` + `recorded_at`）**。`valid_from/to` 是"世界上什么时候是真的"，`recorded_at` 是"系统什么时候知道的"。审计和取证问的是后者——"5 月 20 日那天你的系统认为地址是什么"（第 31 章）。只存一个时间戳，你答不了这个问题。
3. **`source_run` 外键**。任何记忆错误都能反向定位到那次 Run 的完整上下文。这是记忆系统的"binlog"。

### 19.4.3 写入 pipeline：抽取 → 分级 → 仲裁 → 落库

```go
// memory/write.go
type Candidate struct {
    Subject    string
    Predicate  string
    Object     json.RawMessage
    Kind       Kind    // Fact | Preference | Episode
    Confidence float64 // 0.5 推断 / 0.8 用户陈述 / 0.95 工具返回
    Source     string
    TTL        time.Duration
}

func OnRunEnd(ctx context.Context, run *Run) error {
    cands, err := Extract(ctx, run) // 小模型抽取，输出严格 JSON Schema
    if err != nil {
        // 抽取失败不影响 Run 结果，但要进指标：抽取失败率是记忆质量的先行指标
        return metrics.Wrap("memory.extract", err)
    }

    for _, c := range cands {
        // 闸门 1：低置信推断直接丢（不存 = 最好的冲突消解）
        if c.Kind == Fact && c.Confidence < 0.75 {
            metrics.Inc("memory.drop.low_conf")
            continue
        }
        // 闸门 2：PII 同意管理（第 31 章）
        if IsSensitive(c) && !Consented(run.Subject, c.Predicate) {
            metrics.Inc("memory.drop.no_consent")
            continue
        }
        // 闸门 3：predicate 必须在白名单里（否则语义漂移会造出几百个近义谓词）
        if !PredicateRegistry.Has(c.Predicate) {
            metrics.Inc("memory.drop.unknown_predicate")
            continue
        }

        switch decision := Resolve(ctx, c); decision {
        case Accept:
            if err := UpsertFact(ctx, c); err != nil { // 单事务：旧值 valid_to=now
                return fmt.Errorf("upsert %s.%s: %w", c.Subject, c.Predicate, err)
            }
        case Reject:
            metrics.Inc("memory.reject")
        case Conflict:
            EnqueueClarify(c) // 不覆盖，攒到下一轮对话里问用户一句
        }
    }
    return nil
}

// UpsertFact 在单事务内完成「关旧 + 插新」，靠部分唯一索引兜底并发
func UpsertFact(ctx context.Context, c Candidate) error {
    return WithTx(ctx, func(tx Tx) error {
        var oldID int64
        _ = tx.QueryRow(`SELECT id FROM memory_fact
                         WHERE tenant_id=$1 AND subject_id=$2 AND predicate=$3
                           AND valid_to IS NULL FOR UPDATE`,
            c.TenantID, c.Subject, c.Predicate).Scan(&oldID)
        if oldID != 0 {
            if _, err := tx.Exec(`UPDATE memory_fact SET valid_to=now() WHERE id=$1`, oldID); err != nil {
                return err
            }
        }
        _, err := tx.Exec(`INSERT INTO memory_fact
            (tenant_id, subject_id, predicate, object_json, confidence, source, source_run,
             superseded_by, ttl_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)`,
            c.TenantID, c.Subject, c.Predicate, c.Object, c.Confidence,
            c.Source, c.SourceRun, nullable(oldID), c.ExpiresAt)
        return err
    })
}
```

**"Conflict 时不覆盖，攒到下一轮问一句"** 是这段代码里最重要的产品决策。记忆系统最大的错误是"猜"——猜错了用户根本不知道，直到出事。改成问一句：

> 「您是要用**深圳南山**那个地址吧？我看到您 5 月提过搬家。」

一句话的成本是 30 token，收益是把一次可能的赔付换成一次交互。**这笔账在任何场景下都划算。**

### 19.4.4 冲突消解：三级仲裁 + 覆盖规则

| 新值来源 | 置信度 | 遇到旧值时的行为 | 理由 |
| --- | --- | --- | --- |
| **工具返回**（`tool:*`） | 0.95 | **新胜**，直接覆盖 | 业务系统的真相，优先级最高 |
| **用户显式陈述**（`user:*`，含"我搬家了"这类明确表达） | 0.80~0.90 | 旧值是工具 → **Conflict（问一句）**；旧值也是用户陈述 → 新胜 | 用户可能说错/说笑，与业务系统冲突时不能盲信 |
| **用户隐式表达**（"寄到公司吧"） | 0.65~0.75 | 不覆盖，新增情景记忆 | 隐式表达是"这次的选择"，不一定是"长期的偏好" |
| **模型推断**（`infer:*`） | 0.50~0.60 | **一律不写事实表**，最多写偏好且需二次确认 | 推断污染是记忆事故第一来源 |
| **租户配置**（企业客户导入） | 0.90 | 新胜，但记审计 | 客户成功团队维护，需要留痕 |

再加两条时间规则：

- **同源同置信度**：`valid_from` 新的胜（后写覆盖先写）。
- **过期不自动复活**：被 `superseded_by` 标记的旧值永不回到"当前有效"，即使新值后来被删（删 ≠ 回滚）。否则你会遇到"用户删了地址，系统回退到三年前的地址"这种荒诞事故。

### 19.4.5 遗忘与 TTL：忘不掉就是负债

| 记忆类型 | TTL | 依据 | 到期动作 |
| --- | --- | --- | --- |
| 短期工作记忆 | Run 结束 | 无复用价值 | 丢弃（事件流里已有，第 6 章） |
| 会话摘要 | 30 天 | 用户体验（可回看） | 归档到冷存储，不删 |
| 情景记忆 | 90 天 | 合规常见要求 + 相关性衰减 | **硬删**，含向量索引条目 |
| 事实记忆 | 跟随业务实体 | 业务正确性 | 随用户/订单删除级联删除 |
| 偏好 | 12 个月无交互 | 兴趣衰减 | **降权**（importance × 0.5）而不是删除 |
| 敏感类（健康/宗教/生物特征） | 30 天 | 合规最严档 | 硬删 + 审计留痕 |

两个必须写进设计文档的点：

1. **删除要穿透所有副本**：主库 → 从库 → 向量索引 → 语义缓存（第 24 章）→ 备份策略。向量库的删除经常是"标记删除"，物理空间不回收，**这在合规审计里会被抓**。
2. **TTL 要有清理作业的 SLO**：到期 ≠ 删除。给一个"过期后 24 小时内必删"的承诺，并且**能出报告**——审计问"3 月 1 日到期的记录现在还有吗"，你要能 5 分钟内答上来。

### 19.4.6 多实例一致性与读写分离

记忆的写入有个天然的并发风险：**同一个用户的两次 Run 可能落在两个 Pod 上**，同时写同一个 `(subject, predicate)`。

三档方案，按规模选：

| 方案 | 做法 | 一致性 | 适用 |
| --- | --- | --- | --- |
| **A. 数据库兜底**（起步推荐） | 部分唯一索引 + `FOR UPDATE` 单事务 | 强（单库） | 单库能扛到 5k QPS 写 |
| **B. 单写者分区** | 按 `subject_id` 哈希路由到固定分片/固定 worker（类似 Kafka 分区、Actor 模型） | 强（分片内串行） | 多实例、写冲突频繁 |
| **C. 事件溯源 + 物化** | 写入只追加事件，物化视图异步合并 | 最终一致 | 需要完整历史与回放 |

**读写分离的坑：读己之写（read-your-writes）。**

用户刚说完"我搬家了"，下一句就是"用新地址"。中间隔 3 秒，从库延迟 200ms，看起来没事——但你的异步抽取管道也是 3 秒，实际延迟可能 3.2 秒，用户第二句已经到模型了。

解法是**把写版本号带在会话上下文里**：

```go
// memory/read.go
// 会话内维护一个 min_version：本会话已经写入（或已确认可见）的最大 commit 版本。
// 读时要求从库 >= min_version，否则回主库（只回这一次，不是永久降级）。
func Load(ctx context.Context, s *Session, q Query) ([]Memory, error) {
    if lag, _ := replica.Lag(ctx); lag > 200*time.Millisecond ||
        replica.Version() < s.MinVersion {
        return loadFromPrimary(ctx, s, q) // 少见路径，<5% 请求
    }
    return loadFromReplica(ctx, s, q)
}
```

Aurora 实测：从库延迟 P99 180ms，`read-your-writes` 触发率 4.2%，对整体 P99 延迟影响 < 8ms。**这个成本必须付**，否则用户会觉得"Agent 有失忆症"。

### 19.4.7 向量记忆 + 知识图谱：什么时候真的需要图

| 查询形态 | 需要什么 | 例子 |
| --- | --- | --- |
| "上次我们聊到哪了" | 向量 + 时间 | 情景召回 top-3 |
| "我的收货地址" | 精确查 | `predicate=shipping_address` |
| "我上次说的那个方案" | 向量（模糊指代） | 语义相似 top-5 |
| "我同事上次提的那个需求" | **图（2 跳）** | `user → member_of → org → owns → ticket` |
| "这个客户的所有关联合同风险" | **图（多跳 + 聚合）** | 3 跳以上 |

工程判断很明确：

> **90% 的记忆需求用"事实表 + 向量"就够了，别一上来就上图。**
> 图的引入成本不是 Neo4j 的部署费，是**你要维护实体对齐**（"张总"、"张三"、"zhangsan@x.com" 是不是一个人）。这件事的长期维护成本远高于它带来的收益，除非你的业务天然是关系密集的（B2B 客户成功、供应链、反欺诈）。

图记忆的正确用法（如果上）：**图只存关系，不存事实正文**。事实放 `memory_fact`，图边放 `memory_edge`，查询时先图定位 `subject_id` 集合，再回表取事实。这样图坏了不影响事实正确性。

---

## 19.5 Java / 分布式经验对照

| 你熟悉的东西 | 记忆层的对应物 | 会咬人的差异 |
| --- | --- | --- |
| 用户画像 / CRM 标签 | 事实记忆 + 偏好记忆 | 标签是人工/规则打的（确定性）；记忆有一半是**模型推断**的 |
| `UPSERT` / 幂等写 | `UpsertFact` | 多了"关旧值"这一步，且要保证同一谓词只有一个当前值 |
| binlog / 归档表 | 双时态（`valid_from` + `recorded_at`） | 审计问的是"当时系统知道什么"，不是"事实何时成立" |
| 主从读写分离 | 记忆读路径 | 必须实现 read-your-writes，否则用户感到"失忆" |
| 分库分表路由键 | `subject_id` 单写者分区 | 路由错了不是数据错，是**并发写冲突** |
| Redis 缓存 + MySQL | 偏好 KV + 事实表 | 多了一层"向量召回"，且召回结果不可解释 |
| 逻辑删除 | `valid_to` / `superseded_by` | **删除 ≠ 回滚**：旧值永不自动复活 |
| GDPR 删除请求 | 级联删除 | 要穿透向量索引与语义缓存，这是最容易漏的 |

三条最会咬人的：

1. **"模型推断就是业务数据"**。你在 Java 里从不会让一段"AI 猜的"数据进核心表，但在记忆层里，`Extract()` 的输出天生就是猜的。**必须有 `confidence` 和 `source` 两列，且低置信只写情景、不写事实。**
2. **删除的级联**。传统系统删一行就完了。这里要删：事实表 + 情景向量 + 图边 + 语义缓存键 + 备份标记。漏一个，合规就过不去。
3. **写放大**。60 万 Run/天 × 每 Run 一次抽取 = 每天 60 万次小模型调用。这在 Java 团队看来是"一个批处理"，但它是**在线链路的一部分**，要有限流和降级（第 23、25 章）。

---

## 19.6 开源坐标

| 项目 | 它做了什么 | 能抄什么 | 你要自己补什么 |
| --- | --- | --- | --- |
| **Letta（MemGPT）** | 分层记忆（core / archival / recall）+ 让模型通过工具"自己编辑记忆"（memory blocks） | 分层思想、记忆作为可调用工具的接口设计 | 自编辑记忆的**写正确性**没有约束；生产上必须加闸门与审计 |
| **Zep**（含 Graphiti） | 时序知识图谱做记忆，强调 bi-temporal（事实有效期 + 摄入时间） | 双时态建模、按时间点查询"当时的事实" | 运维复杂度（图库 + 时序）；小规模场景过重 |
| **Mem0** | 记忆的 add/update/delete 生命周期管理，支持向量 + 图两种后端 | 抽取→去重→更新的 pipeline 形态、API 抽象 | 多租户隔离、权限下推、TTL 合规策略要自己加 |
| **LangGraph checkpointer** | 会话级状态快照（短期记忆） | "状态即记忆"的落地方式 | 只有短期，没有长期/事实/情景的区分 |
| **各类向量库**（pgvector/Qdrant/Milvus） | 语义检索 | HNSW 参数调优 | 它们不理解"记忆"，只理解"向量" |

> 一致的判断：**开源把"记忆的机制"做完了，把"记忆的正确性"留给你了**——置信度、冲突消解、TTL、删除权，四件事都是你自己的合规与业务问题，没人能替你决定。

---

## 19.7 事故清单

- [ ] **事故 A：低置信推断覆盖高置信事实 → 用旧地址发货。**（§19.1）
      兜底：置信度闸门（事实 < 0.75 不写）+ 三级仲裁 + 部分唯一索引。
- [ ] **事故 B：同一用户两条"当前有效"地址并存，召回靠相似度碰运气。**
      兜底：`WHERE valid_to IS NULL` 唯一索引，数据层兜底。
- [ ] **事故 C：每轮都存全量对话，3 个月存储涨 40 倍，召回全是噪声。**
      兜底：只存"摘要 + 事实 + 偏好"，`记忆条数/Run 数 < 1`；定期重建索引。
- [ ] **事故 D：用户行使删除权，主库删了，向量库还在，语义缓存还在命中。**
      兜底：删除编排（fact → episode → vector → cache → 备份标记）+ 删除报告。
- [ ] **事故 E：刚写完读不到，用户连说三遍"我搬家了"。**
      兜底：read-your-writes（会话 `min_version` + 从库延迟检测回主）。
- [ ] **事故 F：情景记忆不带时间注入，模型把 3 个月前的政策当现行政策回答。**
      兜底：注入时强制带 `happened_at` 并在 prompt 里标注"历史记录，非当前状态"。

---

## 19.8 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| **四类记忆** | 工作（Run 级）/ 偏好（月年）/ 事实（跟随实体）/ 情景（90 天），四类四套策略 |
| **写像数据库，读像缓存** | 写入要有 schema、约束、事务；读取要多路、打分、可丢弃 |
| **事实不走向量** | 地址/金额/订单号是精确查询；用向量召回"像的地址"是设计错误 |
| **置信度分级** | 工具 0.95 / 用户显式 0.8 / 用户隐式 0.7 / 推断 0.55；事实表 < 0.75 拒写 |
| **三级仲裁** | 工具 > 用户显式 > 隐式 > 推断；同级按时间；冲突就问一句（30 token） |
| **数据层兜底** | `WHERE valid_to IS NULL` 部分唯一索引，让"两条当前值"在物理上不可能 |
| **双时态** | `valid_from`（事实何时成立）+ `recorded_at`（系统何时知道），审计问后者 |
| **删除 ≠ 回滚** | 旧值永不自动复活；删除要穿透向量索引与语义缓存 |
| **TTL 要能出报告** | 到期 ≠ 删除；给"过期 24h 内必删"的承诺并留痕 |
| **read-your-writes** | 会话带 `min_version`，从库延迟超标就回主；影响 <8ms，必须付 |
| **图别急** | 90% 场景"事实表 + 向量"够用；图只存关系，不存正文 |
| **ROI** | 记忆读 $324/天、写 $126/天，省 $1,080/天；但写错一条的成本是 26 万/月 |

---

## 19.9 自检题

1. **用户说「我不喜欢被推销」，这句话该怎么进记忆？**
   > 要点：① 归类为**偏好**而不是事实，`predicate=dislikes_sales_pitch`，必须在谓词白名单里；② `source=user:msg_xxx`、`confidence=0.85`（显式陈述）；③ 不要立刻写成"长期偏好"——先写情景（本次会话生效），若 30 天内重复出现 ≥2 次再升级为长期偏好并降低不确定性；④ 不要让它进事实表，因为事实表有唯一索引，会把"这次不喜欢"变成"永远不喜欢"。

2. **你的记忆系统在灰度发布新版本抽取器之后，误报率上升。你怎么在不回滚的情况下止损？**
   > 要点：① 抽取器版本必须进记忆的元数据（`extractor_version`），可按版本切片统计 reject/drop 率；② 加"影子写入"——新版本只写情景不写事实，跑 24h 对比一致率；③ 把置信度闸门从 0.75 临时提到 0.85（降低召回保精度）；④ 观察 `memory.reject` 与 `memory.drop.low_conf` 两个指标的比值变化，它是抽取质量最灵敏的先行指标。

3. **审计问：「3 月 1 日那天，你们的系统认为这位客户的收货地址是什么？」你怎么答？**
   > 要点：靠双时态——`recorded_at <= 3月1日` 且（`valid_to > 3月1日` 或 `valid_to IS NULL`）的记录就是当时系统的认知；同时要能给出 `source_run`，追到那次 Run 的完整上下文、模型版本、工具返回原文（第 31 章取证链）。如果只有 `valid_from/to` 一个时间维度，你答不了这个问题——因为你不知道"当时是否已经知道"。

---

## 19.10 延伸阅读

- **Letta (MemGPT)**：核心/归档/召回三层记忆与"模型自编辑记忆"的接口设计（论文与官方文档）。
- **Zep / Graphiti**：时序知识图谱做记忆，重点看它的 bi-temporal 模型。
- **Mem0**：记忆的增删改生命周期与 pipeline 抽象。
- **PostgreSQL 部分唯一索引 + `pgvector` HNSW**：本文 SQL 的落地基础，注意 `vector_cosine_ops` 与维度选择。
- 上一章：[18. 上下文压缩与前缀复用](18-context-compression.md)
- 下一章：[20. RAG 工程化：召回、混合、重排与权限](20-rag-engineering.md)
- 总纲：[0-summary.md](0-summary.md)
- 交叉引用：短期记忆与事件溯源见第 6 章；上下文预算分配见第 15 章；PII 与删除权见第 31 章；存储选型见第 45 章。
