# 44. 多租户与资源治理

> **状态**：`已完成（初稿）` ｜ **所属**：第九部分 · 平台工程底座 ｜ **编号**：44 / 52
> **本章定位**：前 43 章都在讲「一个 Agent 怎么跑好」，这一章开始讲「**十万个客户的 Agent 同时跑，怎么不互相弄死**」。多租户不是「加个 tenant_id 字段」，它是**隔离、配额、账本、删除**四件事的总和，且这四件事互相牵制。
> **读法**：先看 §44.2 的三档隔离与成本账，这是全章最重要的取舍；再读 §44.4 的 key 规范与 RLS——**这一节的两个错误会直接变成数据泄露**。

---

## 44.1 引子：一个客户的批量任务，把十万个客户按在地上摩擦

3 月 18 日，上午 10:12。一家电商客户（占平台用量 1.8%）为了大促前的工单归因，把积压的 20 万条工单丢进了批量接口。

你的平台**只有一张全局 FIFO 队列**。

| 时间 | 发生了什么 | 数字 |
| --- | --- | --- |
| 10:12 | 批量任务开始灌入 | 20 万 Run / 40 分钟 ≈ **83 Run/s** |
| 10:15 | 执行池 600 个槽位被吃掉 | **480 / 600 = 80%** |
| 10:16 | 交互式请求开始排队 | 队列深度从 200 涨到 42,000 |
| 10:19 | 排队时长 P99 突破 SLO | **3.4s → 28s**（8.2 倍） |
| 10:59 | 大促客户打电话投诉 | 47 分钟 |
| 11:03 | 有人手工把这个租户的 key 禁了 | 恢复用了 4 分钟 |

算账：47 分钟 × 5,000 QPS ≈ **1,410 万次请求**受排队影响；其中约 **9.3 万个 Run** 因超时被客户端放弃（钱已经花了，答案没人看到）；客诉 2,300 单；两家铂金客户启动了 SLA 违约条款（承诺 P99 ≤ 6s）。

**根因不是「那个客户太坏」**，是三个结构性缺失：

1. **没有租户维度的排队**：全局 FIFO，一个大客户的 20 万 Run 和别人的 1 个 Run 抢同一个队头；
2. **没有舱壁（Bulkhead）**：600 个槽位是一个大池子，谁先到谁占；
3. **没有「用量异常」检测**：一个租户的 QPS 相对其 30 天基线涨了 **40 倍**，没有任何告警。

> 🩸 **这一章的核心命题**：
> 多租户系统的第一性原理不是「共享以提高效率」，而是「**共享之后，必须能证明每个租户都拿到了他付钱买的那一份**」。
> 这句话拆成四个可执行的问题：**隔离到哪一档？配额怎么算？账单怎么落？数据怎么删？** 答不出这四个，你的平台在签下第 100 个客户时就会开始赔钱。

---

## 44.2 隔离三档：从「便宜但吵」到「贵但安静」

三档不是三个技术方案，是**三个价格点**。选错档位，要么成本失控，要么客户流失。

| 维度 | ① 共享（Shared / Pool） | ② 分组池（Pooled / Cell） | ③ 独占（Silo） |
| --- | --- | --- | --- |
| 计算 | 所有租户共用 Worker/GPU 池 | 按 tier 或合规域分池，池间物理隔离 | 单租户独立部署单元（自己的 Pod/节点/GPU） |
| 模型配额 | 平台总配额 + 租户限流 | 池级配额 + 租户限流 | 租户独享配额（可预留） |
| 数据库 | 同库同表 + `tenant_id` + RLS | 同库分 schema（或分库） | 独立实例 |
| 向量库 | 同 collection + payload 过滤 | 分组 collection | 独立 collection / 实例 |
| 缓存 | 同 Redis，键含 `tenant_id` | 分组实例 | 独立实例 |
| 对象存储 | 同 bucket + 前缀 | 分组 bucket | 独立 bucket + 独立 KMS key |
| 密钥 | 平台统一 KMS，按租户派生 | 分组 KMS key | 租户自带密钥（BYOK） |
| 故障域 | 全体共担 | 组内共担 | 独立 |
| 升级节奏 | 统一 | 分组（可延迟 3 天） | 租户自选窗口（可延迟 14 天） |
| 典型客户 | 长尾 99%（免费/自助/中小） | 中腰部 + 有区域合规要求 | 铂金/金融/政企/私有化 |

### 成本：为什么不能「所有人都独占」

以 100 张 GPU 等效算力为基准，三档的有效利用率完全不同——**因为隔离的本质是「为峰值预留，而不是为均值付费」**：

| 档位 | 有效利用率 | 承载同样负载需要的卡数 | 相对成本 | 年成本（$30k/卡·年） |
| --- | --- | --- | --- | --- |
| 共享 | 65% | 100 | **1.00×** | $3.0M |
| 分组池（3 组） | 45% | 100 × 0.65/0.45 ≈ **144** | **1.44×** | $4.3M |
| 独占（每客户一池） | 25% | 100 × 0.65/0.25 = **260** | **2.60×** | $7.8M |

算式：`所需卡数 = 基准卡数 × (共享利用率 / 本档利用率)`；`相对成本 = 共享利用率 / 本档利用率`。

再看客户结构（Aurora 真实分布）：

| 分档 | 租户数 | 占比 | 用量占比 | 收入占比 | 该给哪档 |
| --- | --- | --- | --- | --- | --- |
| 免费/自助 | 82,000 | 82% | 4% | 0% | 共享 |
| 标准 | 15,400 | 15.4% | 26% | 31% | 共享 |
| 企业 | 2,550 | 2.55% | 30% | 34% | 分组池 |
| 铂金/受监管 | 50 | 0.05% | **40%** | 35% | 分组池 + 少数独占 |

> 💡 这张表说明一个反直觉的结论：**独占只对 0.05% 的租户有意义，但决策权在这 0.05% 手里。** 所以架构上必须做到「同一套代码、同一个镜像，能按租户参数降级成三档部署」，而不是维护三套系统。这就是第 46 章要讲的「部署形态参数化」。

### 选型判据：什么时候必须升档

| 触发条件 | 共享 → 分组池 | 分组池 → 独占 |
| --- | --- | --- |
| 合规 | 数据驻留要求（境内/欧盟） | 等保三级、金融监管、政企审计 |
| 用量 | 月用量 > 平台 1% | 月用量 > 平台 5% 且愿意付 2.6× |
| SLA | 承诺 99.9% | 承诺 99.95% + 违约赔付 |
| 定制 | 需要自定义提示词/角色卡 | 需要自定义模型、私有化部署、BYOK |
| 风险 | 曾发生过噪声邻居事件 | 客户明确拒绝共享基础设施 |
| 商业 | 年合同 > $120k | 年合同 > $1M |

---

## 44.3 租户模型：四层，不能少

「一个 `tenant_id` 走天下」在第三个客户就会崩。真实客户一定会要「测试环境和生产分开」「市场部和技术部预算分开」「这个 key 只给爬虫用」。所以必须是四层：

```
Organization (org_id)                    ← 计费主体、合同、数据主权区域、KMS key
  │  职责：账单、法务、数据删除的主体、跨境合规的边界
  │
  ├── Project (prj_id)                   ← 配额分配单元、知识库边界、环境隔离
  │     │  职责：配额下沉、预算、RAG 索引隔离、"prod / staging" 的容器
  │     │
  │     ├── User (usr_id)                ← 人、权限、成本归属、审计对象
  │     │     │  职责：RBAC、审批人、审计日志的"谁"
  │     │     │
  │     │     └── APIKey (key_id)        ← 机器身份、scope、速率、可轮换
  │     │          职责：鉴权、限流的最小粒度、审计的"哪个程序"
  │     │
  │     └── Run (run_id)                 ← 计量与计费的原子
  │
  └── Project (prj2)
```

五条必须写进设计文档的规则：

| 规则 | 内容 | 违反的后果 |
| --- | --- | --- |
| **计费在 org** | 账单、发票、合同、税率都挂 org | 按 project 出账 → 财务对不上合同 |
| **配额在 project** | 配额分配与预算控制的最小单元是 project | 只有 org 级配额 → 客户没法给部门分预算 |
| **限流在 key** | 令牌桶挂在 key 上，project/org 级做汇总 | 只有 org 级限流 → 一个失控的 key 拖垮客户自己所有业务 |
| **审计在人** | 每个动作记录 `usr_id`（或 `service_account`） | 只记 org → 出事不知道谁干的 |
| **数据分区在 org** | 分区键/RLS 以 org 为界，跨 org 永不同区 | 以 project 分区 → 删除 org 时无法一次定位 |

---

## 44.4 关键设计

### （1）表结构与租户贯穿

```sql
-- 租户主数据（全局表，量小，可全内存缓存）
CREATE TABLE orgs (
    org_id       text PRIMARY KEY,              -- t_004411
    name         text NOT NULL,
    tier         text NOT NULL,                 -- free/standard/enterprise/platinum
    isolation    text NOT NULL DEFAULT 'shared',-- shared/pooled/silo（决定部署单元）
    cell         text,                          -- 池化/独占时的部署单元标识
    region       text NOT NULL,                 -- cn-sh-1 / eu-fra-1（数据驻留）
    kms_key_id   text NOT NULL,
    status       text NOT NULL DEFAULT 'active',-- active/suspended/deleting/deleted
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE projects (
    prj_id     text PRIMARY KEY,
    org_id     text NOT NULL REFERENCES orgs(org_id),
    env        text NOT NULL,                   -- prod/staging/dev
    quota_policy_id text NOT NULL
);

CREATE TABLE api_keys (
    key_id      text PRIMARY KEY,               -- k_7f2a91
    org_id      text NOT NULL,
    prj_id      text NOT NULL,
    key_hash    bytea NOT NULL,                 -- sha256(secret)，明文永不落库
    prefix      text NOT NULL,                  -- ak_live_t004411_k7f2a91（可展示）
    scopes      text[] NOT NULL,                -- {run:write, kb:read, admin:read}
    rpm_limit   int NOT NULL,
    concurrency int NOT NULL,
    expires_at  timestamptz,
    last_used_at timestamptz,
    revoked_at  timestamptz
);
CREATE INDEX ON api_keys (org_id, prj_id);
```

### （2）全链路 tenant 贯穿：key 规范

key 的格式决定了两件事：**能不能在热路径上少查一次库**，以及**泄露时能不能快速定位与封禁**。

```go
// 格式：ak_<env>_<org8>_<keyid>_<secret32>
// 示例：ak_live_t004411_k7f2a91_9c1d8e2f4a6b0c3d5e7f9a1b2c4d6e8f
//
//   ak_      固定前缀，给密钥泄露扫描器（GitHub secret scanning 等）用
//   live     环境标识，防止测试 key 打生产
//   t004411  租户前缀：可路由到正确的 cell / 分片 / 区域，无需全局查表
//   k7f2a91  key 短 ID：日志、审计、封禁都只用它（不进日志 secret）
//   secret   32 字节随机，只存哈希
const keyPrefix = "ak"

type ParsedKey struct {
	Env    string // live | test
	OrgID  string // t004411
	KeyID  string // k7f2a91
	Secret string
}

func ParseKey(raw string) (ParsedKey, error) {
	parts := strings.Split(raw, "_")
	if len(parts) != 5 || parts[0] != keyPrefix {
		return ParsedKey{}, ErrMalformedKey       // 不要返回"格式错误，期望 ak_xxx"
	}
	if len(parts[4]) != 32 {
		return ParsedKey{}, ErrMalformedKey
	}
	return ParsedKey{Env: parts[1], OrgID: parts[2], KeyID: parts[3], Secret: parts[4]}, nil
}

// 校验：先按前缀路由到 cell（快），再用哈希比对（安全）。两步都不能省。
func (a *Authenticator) Authenticate(ctx context.Context, raw string) (*Principal, error) {
	p, err := ParseKey(raw)
	if err != nil {
		return nil, status.Error(codes.Unauthenticated, "invalid key") // 永不回显细节
	}
	// ① 前缀路由：这决定了你能不能做多 cell，而不做全局一致性读
	cell, err := a.router.CellFor(ctx, p.OrgID)   // 本地缓存，命中率 > 99.9%
	if err != nil || cell != a.cellID {
		return nil, status.Error(codes.Unauthenticated, "invalid key")
	}
	// ② 哈希比对：secret 只以 sha256 存储；比较用常数时间
	rec, err := a.store.GetKey(ctx, p.KeyID)
	if err != nil || rec.RevokedAt != nil || rec.OrgID != p.OrgID {
		return nil, status.Error(codes.Unauthenticated, "invalid key")
	}
	if subtle.ConstantTimeCompare(rec.KeyHash, sha256Sum(raw, p.Secret)) != 1 {
		return nil, status.Error(codes.Unauthenticated, "invalid key")
	}
	return rec.ToPrincipal(), nil
}
```

> ⚠️ 三个不能省的细节：① **常数时间比较**，否则时间侧信道能逐字节爆破；② **失败信息统一**，区分「key 不存在」和「signature 错」等于送情报（第 32 章）；③ **日志只打 `key_id`**，永远不打 secret，包括 trace / 异常堆栈 / 请求体快照。

### （3）ctx 贯穿：让「忘记带租户」编译不过

Java 里靠 `ThreadLocal` + 拦截器；Go 里靠 `context.Context`。但两者有个共同问题：**新写的一段代码很容易「忘了取」**。解法是**类型系统 + lint**，不是纪律。

```go
// 不用裸 string 当租户 ID —— 裸 string 谁都能造一个假的
type OrgID string

type tenantCtxKey struct{} // 私有类型，跨包无法伪造

func WithTenant(ctx context.Context, p *Principal) context.Context {
	return context.WithValue(ctx, tenantCtxKey{}, p)
}

// MustTenant：所有数据访问层的第一行。取不到就是编程错误，直接 panic 500，
// 而不是"返回全部数据"——后者就是数据泄露。
func MustTenant(ctx context.Context) *Principal {
	p, ok := ctx.Value(tenantCtxKey{}).(*Principal)
	if !ok || p.OrgID == "" {
		panic("tenant missing in context: this is a data-leak-class bug")
	}
	return p
}

// 数据访问层：org_id 不由调用方传，只从 ctx 取
func (r *RunRepo) Get(ctx context.Context, runID string) (*Run, error) {
	p := MustTenant(ctx)
	// 注意：org_id 在 WHERE 里，不是查完再判断（见下一节）
	row := r.db.QueryRowContext(ctx,
		`SELECT id, org_id, state, created_at FROM runs
		  WHERE org_id = $1 AND id = $2`, p.OrgID, runID)
	...
}
```

配套一条 CI 规则：`grep -rn "context.Background()" --include=*.go internal/` 在 HTTP handler 层必须为空；`OrgID` 出现在函数参数位置（而非 ctx）的，必须白名单说明理由（一般是跨租户的后台任务）。

### （4）数据分区与 RLS：「查完再过滤」是越权 + 性能双杀

```sql
-- 事件表：按时间分区，子分区按 org 哈希（10 万租户 → 64 个哈希桶）
CREATE TABLE run_events (
    org_id     text        NOT NULL,
    run_id     uuid        NOT NULL,
    seq        bigint      NOT NULL,
    ts         timestamptz NOT NULL,
    kind       text        NOT NULL,
    payload    jsonb       NOT NULL,
    PRIMARY KEY (org_id, run_id, seq, ts)
) PARTITION BY RANGE (ts);

CREATE TABLE run_events_2026_03 PARTITION OF run_events
    FOR VALUES FROM ('2026-03-01') TO ('2026-04-01')
    PARTITION BY HASH (org_id);
CREATE TABLE run_events_2026_03_p07 PARTITION OF run_events_2026_03
    FOR VALUES WITH (MODULUS 64, REMAINDER 7);

-- 行级安全：即使代码写错漏了 WHERE，数据库也会兜底
ALTER TABLE run_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON run_events
    USING (org_id = current_setting('app.org_id', true));
```

```go
// 每个请求开始时把租户塞进连接会话变量（连接池复用时必须 SET LOCAL）
func (db *DB) WithTenantTx(ctx context.Context, fn func(pgx.Tx) error) error {
	p := MustTenant(ctx)
	return pgx.BeginFunc(ctx, db.pool, func(tx pgx.Tx) error {
		// SET LOCAL 只在本事务生效，连接归还池后自动失效——这是关键
		if _, err := tx.Exec(ctx, `SET LOCAL app.org_id = $1`, string(p.OrgID)); err != nil {
			return err
		}
		return fn(tx)
	})
}
```

性能账（为什么必须分区而不是加索引）：

| 方案 | 数据量 20 亿行时的查询耗时 | 原因 |
| --- | --- | --- |
| 全表 + `tenant_id` 索引 + 应用层过滤 | **3.2s** | 索引回表 + 排序，且过滤在内存里做 |
| 分区裁剪（时间 + org 哈希） | **12ms** | 只扫 1/64 的月分区，约 300 万行 |
| 分区裁剪 + RLS | 14ms | RLS 谓词被下推进分区扫描 |

> 🩸 「查完再过滤」不只是慢 260 倍，它是**越权漏洞的温床**：只要有一个接口忘了过滤（分页的第二页、导出的第 1000 行、缓存的命中路径），就泄露了。**下推 + RLS 是双保险，两个都必须有。**

### （5）配额与噪声邻居：三层防御

```go
// 三层：本地令牌桶（快） → Redis 汇聚（准） → 平台级配额（保命）
type Limiter struct {
	local *rate.Limiter      // 每 key 一个，进程内，无锁热路径
	redis *redis.Client      // 每 100ms 汇聚一次，容忍 2% 超卖
	ceil  int64              // 平台级硬上限，宁可全拒也不打穿下游
}

func (l *Limiter) Allow(ctx context.Context, p *Principal, cost int) (bool, error) {
	// ① 本地：挡住 95% 的突发，延迟 ~50ns
	if !l.local.AllowN(time.Now(), cost) {
		return false, nil
	}
	// ② 分布式汇聚：每 key 每 100ms 一次，用 Lua 原子扣减
	if !l.redisBucket(p.KeyID).Take(ctx, cost) {
		return false, nil
	}
	// ③ 平台级：这一层是"宁可错杀"，防止下游被打穿
	if !l.platformCeil.Take(ctx, cost) {
		return false, ErrPlatformSaturated
	}
	return true, nil
}
```

三层之外，真正治噪声邻居的是**舱壁 + 公平调度**（第 11、28 章）。四条硬措施：

| 措施 | 做法 | 效果（Aurora 实测） |
| --- | --- | --- |
| **按租户加权排队（WFQ/DRR）** | 每租户一个虚拟队列，按 tier 给权重（free 1 / standard 4 / enterprise 10 / platinum 30） | 大客户灌流量时，小客户 P99 从 28s 回到 3.8s |
| **批量任务强制隔离** | `batch` 类 Run 走独立队列 + 独立 Worker 池，最多占 20% 总槽位 | 40 分钟批量期间，交互式 P99 只涨 0.4s |
| **用量异常检测** | 租户 QPS / Run 数相对 30 天基线 > 10 倍 → 告警并自动降速到基线 3 倍 | 引子事故的检测时间从 47 分钟 → 90 秒 |
| **并发信号量 + 队列上限** | 每租户 `max_inflight`，超了直接 429（带 `Retry-After`），不进队列 | 防止「排队排到内存爆」 |

### （6）大客户专属通道：不是「多给资源」，是「多给确定性」

铂金客户要的从来不是更快，是**可预期**。四件事，缺一件就会被投诉：

```
        ┌──────────────── Platinum Channel ────────────────┐
入口    │ 独立域名 api-<org>.aurora.example  + 独立 IP 段   │
        │ 独立 TLS 证书、可选双向 TLS / IP 白名单            │
        ├──────────────────────────────────────────────────┤
路由    │ 独立 cell（部署单元），跨 AZ 3 副本                │
        │ 独立模型配额（预留而非抢占），独立 GPU 分池         │
        ├──────────────────────────────────────────────────┤
数据    │ 独立 KMS key（可 BYOK）、独立 S3 bucket            │
        │ 数据驻留区域锁定，跨境默认拒绝                      │
        ├──────────────────────────────────────────────────┤
变更    │ 升级窗口自选，灰度延迟 ≥ 7 天（别人先当小白鼠）     │
        │ 变更前 72 小时邮件通知 + 提供"本次变更影响评估"      │
        └──────────────────────────────────────────────────┘
```

最后一条最容易被忽视，但它是最便宜的客户满意度杠杆：**「我们不给你当试验田」这件事，比多给 20% 配额值钱得多。**

### （7）删除与数据主权：能证明「删干净了」

GDPR 的「被遗忘权」和国内《个人信息保护法》的删除权，要求的不是「点击删除按钮」，而是**「能在 30 天内证明数据在所有存储介质上都不存在了」**。Agent 平台有个额外难点：**一次 Run 的数据会散落在 7 个地方**。

| # | 存储面 | 数据形态 | 删除手段 | 时延 |
| --- | --- | --- | --- | --- |
| 1 | Postgres（会话/Run/账本） | 行 | `DELETE` + 分区 `DROP` | 分钟 |
| 2 | ClickHouse（事件/trace） | 列存分区 | `ALTER TABLE … DROP PARTITION` 或 `DELETE`（mutation） | 分钟~小时 |
| 3 | 向量库（记忆/知识库） | 向量 + payload | 按 `org_id` 过滤删除 + 重建索引 | 小时 |
| 4 | 对象存储（附件/大结果/归档） | 对象 | 删 key + 生命周期规则 + 版本删除 | 即时~天 |
| 5 | 缓存（Redis / 语义缓存） | KV | 按前缀扫描删除 + TTL 兜底 | 分钟 |
| 6 | 备份（快照/归档） | 不可变 | **不能删**，只能等保留期到期 | 35 天 |
| 7 | 日志与审计（WORM） | append-only | 按合规要求保留（法律义务优先） | 保留期 |

删除状态机：

```
requested ──▶ cooling(30d) ──▶ purging ──▶ purged ──▶ certified
    │              │               │           │
    │ 撤销         │ 客户撤回       │ 失败       │ 出具删除证明
    ▼              ▼               ▼           ▼
 cancelled     cancelled        retry(3)    （PDF + 哈希清单，审计留存）
```

```go
// 删除任务：每个存储面一个 handler，失败可重试，全程有审计
type PurgeJob struct {
	OrgID    string    `json:"org_id"`
	Deadline time.Time `json:"deadline"` // requested + 45d（含备份保留期 35d + 缓冲）
	Handlers []string  `json:"handlers"`
}

func (w *PurgeWorker) Run(ctx context.Context, j PurgeJob) error {
	// ① 先冻结：禁止该 org 任何写入（否则永远删不完）
	if err := w.freeze(ctx, j.OrgID); err != nil {
		return err
	}
	// ② 逐面删除，记录每个面的「已删条数 + 校验哈希」
	for _, h := range w.handlers {
		res, err := h.Purge(ctx, j.OrgID)
		w.audit.Record(ctx, j.OrgID, h.Name(), res, err) // 第 31 章：不可篡改
		if err != nil {
			return fmt.Errorf("purge %s: %w", h.Name(), err)
		}
	}
	// ③ 出具证明：列出每个面的「剩余条数 = 0」，含备份到期日
	return w.certify(ctx, j)
}
```

> ⚠️ **三个必须写进合同的诚实条款**：① 备份里的数据要等保留期（35 天）自然过期，无法单独删除——所以要承诺「45 天内全量清除」而不是「立即」；② 审计日志因法律义务可以保留（并明确告知）；③ 已经被第三方模型供应商处理的请求，取决于供应商的零留存政策，**这一条必须写进 DPA**（第 31 章）。

---

## 44.5 Java / 分布式经验对照

| 你熟悉的 | AgentOS 对应 | 同构点 | 会咬人的地方 |
| --- | --- | --- | --- |
| ShardingSphere 分库分表 | 按 `org_id` 分区 | 路由键 + 分片算法 | 分片键选错（选成 `prj_id`）→ 删租户要扫全部分片 |
| `TenantLineHandler` 自动拼 `tenant_id` | RLS + MustTenant | 无侵入 | 它只拼 WHERE，不防「跨租户 JOIN」；且 `UNION`/子查询容易漏 |
| MyBatis 拦截器 | 数据访问层统一注入 | 集中治理 | 拦截器对 `@SelectProvider` 与手写 XML 覆盖不全 |
| `ThreadLocal` 存 `TenantContext` | `context.Context` | 请求级传递 | Go 的 goroutine 会「继承但可被遗忘」；异步任务要显式 `WithTenant` |
| Spring Security 的 `Authentication` | `Principal` | 身份载体 | Token 里带 `org_id` 不等于可信，必须查表校验（引子：伪造 org 越权） |
| Hibernate Filter / `@FilterDef` | RLS | 自动过滤 | 需要手动 enable，且对原生 SQL 无效 |
| K8s Namespace + ResourceQuota | Cell + 配额 | 隔离 + 限额 | K8s 的 Quota 是硬限（超了 Pending），Agent 要的是软限（超了降速） |
| 多租户 SaaS 的「共享库共享表」 | ① 共享档 | 成本最低 | 必须配 RLS，否则一个 ORM 懒加载就是泄露 |
| 计费系统的 `account_id` | `org_id` | 计费主体 | 多一层 project 是必须的，别图省事 |

---

## 44.6 开源坐标

| 工具/产品 | 它有什么 | 缺什么 |
| --- | --- | --- |
| **Dify** | Workspace 概念（成员、应用、知识库隔离），按 workspace 分数据 | 只有一层（workspace ≈ org），没有 project/预算层；无 RLS；无隔离档位差异 |
| **Langfuse** | Organization → Project 两级、按 project 的 API key 与用量统计 | 强在可观测，不解决配额与调度隔离；删除是「软删 + 后台任务」，需自己补证明 |
| **Keycloak**（Organizations 特性） | 组织/成员/身份联邦、OIDC | 只解决「身份」这一层，租户的配额/数据分区/计费全不管；新版 Organizations 能力以官方文档为准 |
| **ShardingSphere** | 分片 + `TenantLineHandler` | 只做数据面；且对 NoSQL/向量库无效 |
| **Postgres RLS + pg_partman** | 行级安全 + 分区自动化 | 需要自己保证 `SET LOCAL` 不漏、连接池不串租户 |
| **K8s Namespace / vCluster** | 逻辑隔离、配额、网络策略 | 隔离的是「部署」，不是「同一 API 里的租户」；vCluster 成本不低 |
| **Qdrant / Milvus 的 payload 过滤 / partition** | 向量层的租户隔离 | 「查完再过滤」性能塌方；必须用 partition key（Qdrant 的 `is_tenant` payload 索引）做下推 |

> 这块没有现成的「多租户 Agent 平台」可抄。业界所有多租户 SaaS 的经验都能搬（Stripe 的 account 模型、Salesforce 的 org + RLS），**唯独要多加一条：模型配额与 token 账单必须成为一等公民**，这是传统 SaaS 没有的维度。

---

## 44.7 事故清单

- [ ] **事故 A：噪声邻居（本章引子）。**
  现象：一个租户 40 分钟灌 20 万 Run，全平台 P99 从 3.4s 到 28s。根因：全局 FIFO + 单一执行池 + 无用量异常检测。兜底：按租户 WFQ + 批量走独立池（上限 20%）+ 用量相对基线 10 倍即告警降速。
- [ ] **事故 B：缓存 key 漏 `tenant_id`。**
  现象：A 租户的检索结果被 B 租户命中，B 看到了 A 的合同摘要。根因：语义缓存键只用了 query 文本。兜底：缓存键 = `(org_id, model, prompt_set, index_snap, query_norm)`，缺一项就是泄露（第 24 章）。
- [ ] **事故 C：向量检索「先查后过滤」。**
  现象：`top_k=50` 后按 `org_id` 过滤，B 租户经常拿不到结果（因为前 50 名全是 A 的）。根因：过滤没下推。兜底：Qdrant/Milvus 用 partition/tenant 索引下推，pgvector 用 `WHERE org_id = $1` 建复合索引。
- [ ] **事故 D：伪造 `org_id` 越权。**
  现象：攻击者用自己租户的 token 改 payload 里的 `org_id`，拿到别人数据。根因：`org_id` 从请求体读，而不是从鉴权结果推导。兜底：`org_id` **只能**来自 `Principal`（`MustTenant`），请求体里的同名字段一律忽略并告警。
- [ ] **事故 E：删除请求 90 天没执行完。**
  现象：客户投诉「你们说删了，我发现备份里还有」。根因：备份保留 35 天但合同写「7 天内删除」，且没有删除证明。兜底：删除状态机 + 分存储面清单 + 合同写「45 天内全量清除，审计日志除外」+ 出具证明。
- [ ] **事故 F：独占客户的池子空转，共享池被压垮。**
  现象：5 个独占客户各占 24 卡，平均利用率 25%；共享池 65% 已过载。根因：一档一刀切，缺「可回收预留」。兜底：独占客户签「弹性借用」条款——闲置算力可被共享池借用，但 5 分钟内可抢占回来。
- [ ] **事故 G：`SET LOCAL` 没生效，连接池串租户。**
  现象：偶发（约 1/5000）返回别的租户数据。根因：用了 `SET` 而非 `SET LOCAL`，连接归还池后变量残留。兜底：只允许 `SET LOCAL` + 事务内执行 + 集成测试里专门造「跨租户串号」用例。

---

## 44.8 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 第一性原理 | 共享之后必须能证明「每个租户拿到了他付钱的那一份」 |
| 三档隔离 | 共享 1.00× / 分组池 1.44× / 独占 2.60×（利用率 65%/45%/25%） |
| 客户结构 | 0.05% 的租户贡献 40% 用量；独占只对这 0.05% 有意义 |
| 四层模型 | org（计费/主权）→ project（配额）→ user（审计）→ key（限流）→ run（计量） |
| key 规范 | `ak_<env>_<org>_<keyid>_<secret>`：前缀可路由、ID 可封禁、secret 只存哈希 |
| 鉴权三律 | 常数时间比较、失败信息统一、日志只打 key_id |
| 贯穿 | `OrgID` 是独立类型；`MustTenant` 取不到就 panic，绝不「返回全部」 |
| 分区 | 时间 RANGE + org HASH 子分区；3.2s → 12ms（260 倍） |
| 双保险 | 应用层 WHERE 下推 + 数据库 RLS；只做一个都会漏 |
| 噪声治理 | WFQ 加权排队 + 批量独立池（≤20%）+ 基线 10 倍告警 |
| 删除 | 7 个存储面 + 30 天冷静期 + 35 天备份 + 出具证明；合同写 45 天 |
| 一句话 | **多租户不是加字段，是隔离档位、配额、账本、删除四件事的总和。** |

## 44.9 自检题

1. **一个客户要求「物理隔离」，报价只能涨 30%，你怎么谈？**
   > 要点：① 先算账：独占利用率 25% vs 共享 65%，成本 2.6×，涨 30% 是亏的；② 拆需求：客户真实诉求通常是「合规」或「确定性」，不一定是物理机——分组池 + 独立 KMS key + BYOK + 7 天延迟升级 + 独立域名 IP，能满足 80% 的合规诉求，成本只有 1.44×；③ 若真需要物理独占，报价必须 ≥2.6× 或设最低消费；④ 折中方案：独占但签「闲置算力可被借用，5 分钟内可抢占」。

2. **为什么 key 里要带 `org_id` 前缀？它不是可以被伪造吗？**
   > 要点：前缀不是信任来源，是**路由提示**：① 让网关能在读库之前把请求路由到正确的 cell/分片，避免全局一致性读；② 让泄露扫描器能识别（`ak_live_` 前缀）；③ 让运维能一眼看出「这把 key 属于谁」。真正的信任来自「按 key_id 查库 + 常数时间哈希比对 + `rec.OrgID == p.OrgID` 交叉校验」。所以前缀可伪造，但伪造后哈希比对必失败。

3. **「我们做了多租户」——请给出四个可以验证的证据。**
   > 要点：① 数据面：RLS 已启用 + 集成测试有「跨租户串号」用例（含分页第 2 页、导出、缓存命中三条路径）；② 资源面：按租户的 WFQ 排队 + 每租户 `max_inflight`，能复现「一个大客户灌流量时小客户 P99 只涨 0.4s」；③ 财务面：账单能拆到 project，且能回答「哪个租户贡献 80% 成本」；④ 合规面：删除请求有状态机、分存储面清单、45 天承诺与删除证明样本。

## 44.10 延伸阅读

- 上一章：[43. 策略中心与 Feature Flag](43-policy-center.md)（租户维度的配置怎么下发）
- 下一章：[45. 存储架构](45-storage-architecture.md)（`org_id` 怎么变成分区键）
- 总纲：[0-summary.md](0-summary.md)（§1「cgroup 限额 → 配额/限流/预算刹车」）
- 相关：[11. 准入控制、排队与公平性](11-admission-and-queue.md)（WFQ/DRR）、[23. 多层限流与配额体系](23-rate-limit-quota.md)（五维限流）、[27. 计费的可靠性](27-billing-reliability.md)（按 project 出账）、[28. 大流量不崩](28-peak-traffic.md)（舱壁模式）、[31. 合规与审计](31-compliance-audit.md)（数据主权与删除取证）、[20. RAG 工程化](20-rag-engineering.md)（检索权限下推）
- 外部：Postgres RLS 与声明式分区文档、Qdrant 多租户（payload 索引/partition）文档、Stripe 的 account 模型、Salesforce 多租户架构公开资料
- 语言细节：[../go/24-context.md](../go/24-context.md)（ctx 传值与取消）、[../go/22-sync-primitives.md](../go/22-sync-primitives.md)（本地令牌桶的无锁实现）
