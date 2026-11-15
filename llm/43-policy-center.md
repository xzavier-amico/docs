# 43. 策略中心与 Feature Flag

> **状态**：`已完成（初稿）` ｜ **所属**：第八部分 · 可观测、评估与数据闭环 ｜ **编号**：43 / 52
> **本章定位**：第 42 章讲「发布怎么设计」，本章讲「发布靠什么机制落地」。策略中心是 AgentOS 的**神经末梢**：灰度开关、限流阈值、路由规则、提示词选择、角色卡绑定、降级阶梯，全部必须在不重启、不发版的前提下改变，并且可审计、可回滚。
> **读法**：先读 §43.2 那张「必须热更清单」，逐条问你自己的平台能不能做；再读 §43.4 的 schema 与校验——**这一节能挡掉你未来 60% 的 P1**。

---

## 43.1 引子：周五 17:40，一个下拉框和 1000 倍

某租户要压测，值班同学收到工单：「把 t_004411 的限流从 600 RPM 调到 6000。」

他在管理后台找到「限流」面板，两个输入框：一个是数字，一个是单位下拉（RPM / RPS）。他填了 `6000`，然后手滑——下拉框还停在 `RPS`。保存。界面弹了个绿条「保存成功」。

**6000 RPS = 360,000 RPM = 原配额的 600 倍。** 而系统里这个租户的 key 背后挂了 40 个压测客户端，每个瞬间起了 200 个并发。下游模型网关在 40 秒内收到 **12 万 QPS** 的请求，供应商 429 全线飘红，网关的重试策略把 429 放大成 60 万 QPS，熔断器被拖成「全开」，**平台级降级触发**。

| 项 | 数值 |
| --- | --- |
| 从保存到全站降级 | 4 分 12 秒 |
| 降级持续 | 11 分钟 |
| 受影响的 Run | 11 × 60 × 5,000 ≈ **330 万次** |
| 走了兜底话术的用户 | 约 41 万 |
| 客诉 | 842 单 |
| 定位耗时 | 26 分钟（因为配置变更审计里只记了「updated limit policy」，没记改前改后） |

**这次事故最刺痛人的不是手滑，是三个「本可以」：**

1. 本可以在保存时拦住——单位是枚举，`6000 RPS` 相对该租户历史峰值是 **600 倍**，一条「超过 5 倍需要二次确认 + 破坏性变更」的规则就能挡下；
2. 本可以在 30 秒内发现——限流阈值变了 600 倍，这是一个**配置变更事件**，本该触发「配置漂移」告警；
3. 本可以在 1 分钟内回滚——但它回滚花了 26 分钟，因为没人知道改前是多少。

> 🩸 **这一章的核心命题**：
> 在 Agent 平台里，**「配置」已经变成了「代码」的一种形态**——它能改变模型行为、改变花钱速度、改变谁能用。但它享受的治理待遇，往往还不如一段 20 行的 Java 代码（没有 review、没有 CI、没有回滚、没有 blame）。
> **配置事故比代码事故更常见，不是因为人更蠢，是因为配置没有工程护栏。**

---

## 43.2 为什么 Agent 平台必须有「策略即配置」

先做一个思想实验：如果下面这些东西**必须发版才能改**，你的故障恢复时间是多少？

| 配置项 | 变更频率（Aurora） | 为什么必须热更 | 不能热更的代价 | 谁有权改 |
| --- | --- | --- | --- | --- |
| **灰度开关**（release 分配到租户/流量/场景） | 60 次/天 | 扩量与回滚要在秒级完成 | 回滚 = 25 分钟部署，故障半径 ×50 | 值班 + 发布负责人 |
| **限流阈值**（平台/租户/用户/模型/工具） | 15 次/天 | 大促、压测、突发流量都要临时调 | 只能眼睁睁看它限死或被打穿 | 值班（带审批） |
| **降级阶梯**（大模型→小模型→模板→兜底） | 8 次/周 | 故障发生时必须在 30 秒内关掉某条路径 | 降级慢 = 故障时长翻倍 | 值班（一键） |
| **路由规则**（场景→模型、权重、failover 顺序） | 20 次/周 | 成本/延迟/质量三目标的日常调节旋钮 | 每次调成本都要发版，没人会去调 | 平台组 |
| **提示词选择**（哪套模板进生产） | 30 次/周 | 提示词是运营资产，迭代以小时计 | 迭代速度从小时退化成天 | 产品 + 算法 |
| **角色卡绑定**（租户/场景用哪张人格卡） | 5 次/周 | 客户要定制语气，退款/合规要立刻收紧 | 无法做企业定制 | 业务 + 合规 |
| **模型配额与价格卡** | 3 次/周 | 商务谈判、新套餐上线 | 计费与限流对不上 | 商务 + 财务 |
| **审核与注入阈值** | 4 次/周 | 攻击态势变化，需当天收紧 | 被打了只能挨着 | 安全 |
| **采样与日志级别** | 10 次/周 | 排障时临时提高采样 | 排障没有数据 | 值班 |
| **缓存 TTL 与语义阈值** | 3 次/周 | 误命中率变化（第 24 章） | 泄露风险或命中率崩 | 平台组 |

十条里有九条，答案是「我不可能等发版」。所以结论很硬：

> **Agent 平台的治理能力上限 = 你能热更的东西的上限。**
> 凡是「改不了」的参数，最终都会变成「不敢调」的参数，然后变成「出了事没法救」的参数。

### 三条判据：什么该进配置，什么绝对不能进

不是所有东西都该配置化。做成「图灵完备的配置语言」是另一种灾难（第 49 章会讲低代码的边界）。三条判据：

| 判据 | 该进配置 | 不该进配置 |
| --- | --- | --- |
| **会随环境/时间变**吗 | 阈值、权重、比例、开关、绑定关系 | 并发模型、事务边界、幂等语义 |
| **会随人变**吗（运营/商务/合规要改） | 提示词、角色卡、话术、套餐、配额 | 安全不变量（「必须校验签名」） |
| **故障时要立刻关**吗 | 降级阶梯、kill switch、熔断阈值 | 计费公式（改了要能对账，必须走代码+评审） |

反例清单（Aurora 明确禁止进配置）：数据库连接串与密钥（走 secret 管理）、计费单价的计算逻辑（只允许配「套餐参数」，不允许配「公式」）、权限判定规则（走代码 + 策略引擎的固定 DSL）、任何带 `if/loop` 的表达式。

---

## 43.3 结构图：配置从「人」到「实例内存」的七道关

```
 ┌──────────── 写入侧（三种入口，同一套校验）────────────┐
 │  Git (config-as-code, PR)   管理后台表单   开放 API    │
 └───────────────┬──────────────────┬──────────────┬─────┘
                 └──────────────────┼──────────────┘
                                    ▼
                    ① 草稿 Draft（按租户/环境隔离，不影响线上）
                                    ▼
                    ② 静态校验：JSON Schema + 类型 + 枚举 + 范围 + 单位
                                    ▼
                    ③ 语义校验：引用完整性、依赖冲突、配额守恒、
                               与当前线上值的「漂移倍数」检查（引子事故的解药）
                                    ▼
                    ④ 影响面预演：diff 半径（多少租户/多少 QPS 会变）、
                               成本影响、回滚预案是否就绪
                                    ▼
                    ⑤ 审批：普通项单人，高风险项双人 + 变更单号
                                    ▼
                    ⑥ 快照发布 Snapshot（不可变、单调版本号、
                               prev_hash 哈希链、签名）
                                    ▼
        ┌───────────────────────────┴────────────────────────────┐
        ▼                           ▼                            ▼
  SSE / 长轮询推送             SDK 定期拉取（兜底）       审计库（append-only）
  （P99 < 5s）                 （30s 一次，防推送丢失）      （第 31 章）
        │                           │
        └─────────────┬─────────────┘
                      ▼
        实例本地快照（atomic.Pointer 切换，读耗时 < 100ns）
                      │
                      ▼
        业务代码：cfg.Get("limit.tenant.rps")  ← 零网络调用
                      │
                      └─▶ 配置中心全挂？回落到磁盘 last-known-good
```

**这张图最关键的一行是最后一行。** 整份配置（Aurora 2,300 个配置项，平均 300 字节）序列化后约 **690 KB**，压缩后 80 KB。也就是说：**每个实例完全可以把整份配置装进内存，读配置不需要任何网络调用。**

算一笔账就知道为什么必须这样：

```
峰值 5,000 QPS
× 每 Run 读 40 个配置项（限流/路由/提示词/角色卡/降级/缓存 TTL…）
= 200,000 次配置读/秒
```

如果每次读都是一次 RPC，你需要一座能扛 20 万 QPS 的配置中心集群——**为了读一堆一年变不了 20 次的数字**。这不是架构，这是自杀。

---

## 43.4 关键设计

### （1）分层与合并规则：七层，优先级从低到高

```
platform  →  env  →  tenant_tier  →  tenant  →  project  →  scene  →  request
（平台默认） （prod/staging）（free/pro/platinum）（org）（环境/部门）（场景）（单次覆盖）
```

合并规则必须写死，且只有三条，否则你会得到「配置到底生效了啥」这个永远回答不了的问题：

| 类型 | 规则 | 例子 |
| --- | --- | --- |
| 标量（数字/字符串/布尔） | **高优先级整值覆盖** | `limit.rps: 600` 被租户级 `900` 覆盖 |
| map | **深合并**，同键覆盖 | `degrade.ladder: {big: off}` 只关大模型那级 |
| list | **默认整体替换**，需要追加必须显式声明 | `scenes: [a,b]` 替换；`scenes+ : [c]` 追加 |

> ⚠️ 「list 默认追加」是个经典坑：平台默认开了 5 个审核规则，某租户配了 1 个，结果变成 6 个，谁也不知道第 6 个从哪来。**默认替换 + 显式追加**能避免 90% 的「配置不生效/生效过头」工单。

### （2）配置 schema：用类型系统消灭单位错误

引子那次事故的根因是「数字和单位分离」。解法不是加个确认弹窗，是**让错误的配置根本表达不出来**。

```json
{
  "$id": "https://aurora/schema/limit-policy/v3.json",
  "type": "object",
  "required": ["tenant_id", "limits"],
  "additionalProperties": false,
  "properties": {
    "tenant_id": { "type": "string", "pattern": "^t_[0-9]{6}$" },
    "limits": {
      "type": "object",
      "additionalProperties": {
        "type": "object",
        "required": ["value", "unit", "burst"],
        "properties": {
          "value": { "type": "number", "exclusiveMinimum": 0, "maximum": 1000000 },
          "unit":  { "enum": ["rpm", "rps"] },
          "burst": { "type": "integer", "minimum": 1, "maximum": 10000 }
        },
        "if":   { "properties": { "unit": { "const": "rps" } } },
        "then": { "properties": { "value": { "maximum": 5000 } } }
      }
    }
  }
}
```

`value` 和 `unit` 打包成一个原子对象，任何代码读它都必须显式 `.PerSecond()` 或 `.PerMinute()`，编译期就不可能把 RPM 当 RPS 用：

```go
// 单位是一等公民：不存在「裸数字」
type Rate struct {
	Value float64 `json:"value"`
	Unit  Unit    `json:"unit"` // UnitRPM | UnitRPS
}

func (r Rate) PerSecond() float64 {
	switch r.Unit {
	case UnitRPS:
		return r.Value
	case UnitRPM:
		return r.Value / 60
	default:
		panic("unreachable: schema 保证 unit ∈ {rpm, rps}")
	}
}

// 破坏性变更守卫：比历史峰值放大 5 倍以上要人工确认
func GuardRate(ctx context.Context, in Rate, hist PeakHistory) error {
	peak := hist.PeakPerSecond(ctx, in.TenantID, 30*24*time.Hour)
	if peak > 0 && in.PerSecond() > peak*5 {
		return &NeedsConfirmError{
			Got: in.PerSecond(), Peak: peak, Multiple: in.PerSecond() / peak,
			Reason: "rate increased >5x vs 30d peak; require dual approval",
		}
	}
	return nil
}
```

### （3）发布流程状态机

```
        ┌────────┐  提交   ┌──────────┐  校验通过  ┌──────────┐
        │ draft  │────────▶│ validated│───────────▶│ approved │
        └───┬────┘         └────┬─────┘            └────┬─────┘
            │ 放弃              │ 校验失败               │ 发布（灰度）
            ▼                   ▼                       ▼
        ┌────────┐         ┌──────────┐           ┌───────────┐
        │discarded│        │ rejected │           │  rolling  │
        └────────┘         └──────────┘           └─────┬─────┘
                                                         │
                    ┌────────────────────┬───────────────┼──────────────┐
                    ▼                    ▼               ▼              ▼
              ┌──────────┐        ┌───────────┐   ┌──────────┐   ┌──────────┐
              │  live    │        │  paused   │   │  failed  │   │ reverted │
              │（全量生效）│        │（人工介入）│   │（自动回滚）│   │（人工回滚）│
              └────┬─────┘        └─────┬─────┘   └────┬─────┘   └────┬─────┘
                   │ 新版本发布           │ 恢复          │              │
                   └──────────────▶ superseded ◀─────────┴──────────────┘
```

五个状态必须有明确语义，尤其 `superseded`——**旧快照永不删除**，否则你无法回答「上周三下午的限流是多少」（引子事故的 26 分钟就是这么来的）。

### （4）版本、审计与回滚：快照表

```sql
CREATE TABLE config_snapshot (
    version      bigint      PRIMARY KEY,              -- 全局单调递增
    scope        text        NOT NULL,                 -- platform/env/tenant/project/scene
    scope_value  text        NOT NULL,                 -- '*' 或 t_004411
    payload      jsonb       NOT NULL,                 -- 合并后的完整生效配置
    payload_hash text        NOT NULL,                 -- sha256(canonical json)
    prev_hash    text        NOT NULL,                 -- 哈希链，防篡改（第 31 章）
    change_ticket text       NOT NULL,                 -- 变更单号，无单不发布
    diff         jsonb       NOT NULL,                 -- 结构化 diff，前端直接渲染
    impact       jsonb       NOT NULL,                 -- 影响面预演结果
    author       text        NOT NULL,
    approver     text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    state        text        NOT NULL DEFAULT 'live'   -- live/superseded/reverted
);

CREATE INDEX ON config_snapshot (scope, scope_value, version DESC);
CREATE INDEX ON config_snapshot USING gin (payload jsonb_path_ops);
```

配套三条硬规则：

1. **快照不可变**。改配置 = 生成新 version，永远不 UPDATE payload。这样「回滚」= 把指针指回旧 version，1 秒完成。
2. **diff 结构化存储**。不是字符串 diff，是 `[{path: "limit.tenant.rps", from: 600, to: 6000}]`。前端能算「变了多少倍」，告警能算「漂移半径」（引子事故的第二个本可以）。
3. **`change_ticket` 非空约束**。没有变更单就写不进去。这是唯一能挡住「我就随手改一下」的机制。

### （5）客户端 SDK：原子快照 + 版本单调 + 磁盘兜底

```go
type Store struct {
	cur    atomic.Pointer[Snapshot]   // 读路径：一次原子读，约 20ns
	seq    atomic.Uint64              // 已应用的版本号，必须单调
	disk   string                     // last-known-good 路径
	apply  sync.Mutex
}

// Get：业务代码唯一入口，零网络调用、无锁
func (s *Store) Get(key string) (any, bool) {
	snap := s.cur.Load()
	if snap == nil {
		return nil, false            // 调用方必须能处理「没有配置」→ 用安全默认值
	}
	return snap.Lookup(key)          // 预构建的 trie，O(k) k=key 深度
}

// Apply：推送与拉取共用同一条路径，靠版本号幂等 + 防回退
func (s *Store) Apply(ctx context.Context, v *Snapshot) error {
	s.apply.Lock()
	defer s.apply.Unlock()

	if v.Version <= s.seq.Load() {
		return nil                   // 乱序到达的旧版本，直接丢（重要！）
	}
	if want := s.cur.Load().Hash; v.PrevHash != want && s.cur.Load() != nil {
		// 哈希链断裂：可能丢了一个版本，触发全量拉取而不是增量应用
		s.forceFullPull(ctx)
		return ErrChainBroken
	}
	s.cur.Store(v)                   // 原子切换，读方永远看到完整快照
	s.seq.Store(v.Version)           // 只能前进，不能后退
	return writeFileAtomic(s.disk, v.Payload) // 落盘，供重启时兜底
}

// Startup：进程启动时先用磁盘快照，别让配置中心成为启动依赖
func (s *Store) Startup(ctx context.Context) error {
	if b, err := os.ReadFile(s.disk); err == nil {
		if snap, err := ParseSnapshot(b); err == nil {
			s.cur.Store(snap)
			s.seq.Store(snap.Version)
		}
	}
	return s.subscribe(ctx)          // 后台订阅，慢一点没关系
}
```

四个细节，每一个都对应一次真实故障：

| 细节 | 不做的后果 |
| --- | --- |
| **版本单调（`v.Version <= seq` 丢弃）** | 推送乱序时旧配置覆盖新的，出现「配置自己变回去了」 |
| **原子指针切换** | 边改边读，业务读到一半新一半旧，出现「限流 600 但路由是新的」 |
| **磁盘 last-known-good** | 配置中心重启时全站实例起不来，或起在默认值上（默认值往往是「不限流」） |
| **`Get` 返回 `(nil,false)`** | 代码里 `.(float64)` 空指针 panic，配置项改名 = 全站 500 |

### （6）Feature Flag 的三条铁律

开关（flag）比配置更危险，因为它**直接控制代码分支**。三条铁律，缺一条就会翻车：

| 铁律 | 具体要求 | 违反的后果 |
| --- | --- | --- |
| **安全默认值** | 配置中心不可用时，行为必须等价于「全部关闭」，而不是「全部打开」 | 拿不到 flag 就默认启用新链路 → 配置中心抖动 = 全站跑未验证代码 |
| **kill switch 走独立通道** | 紧急关闭能力不依赖主配置推送（独立 endpoint / 独立集群 / 甚至环境变量） | 配置系统本身故障时，你连关都关不掉（第 25 章） |
| **每个 flag 有主人和过期日** | `owner` + `expires_at` 必填，过期自动告警，到期强制清理 | 三年后没人敢删的 400 个 flag，每个都是定时炸弹 |

```yaml
# flags/payment_action.yaml —— 一个「必须带主人和过期日」的开关
flag: enable_payment_action_v2
owner: team-commerce          # 必填，找不到人就不许建
expires_at: 2026-06-30        # 必填，到期前 14 天自动开单
default: false                # 安全默认值：读不到 = 关闭
kill_switch: true             # 允许通过独立通道强制关闭（优先级最高）
targeting:
  - { rule: tenant_tier == "platinum", value: true }
  - { rule: scene in ["refund_query"], value: false }   # 危险场景永远关
rollout: { percent: 10, hash_key: tenant_id }
audit: { log_evaluation: true }  # 每次求值都记 trace，否则出问题无法归因
```

### （7）灰度发布配置本身（配置的灰度）

配置也要灰度，尤其是「影响面大」的配置。Aurora 的做法是给配置发布本身加一层：

```yaml
config_rollout:
  snapshot: 20260312_0042
  strategy: instance_percent      # 按实例而不是按租户——因为它是全局阈值类配置
  stages:
    - { percent: 10, hold: 10m, guards: [error_rate, p99_latency] }
    - { percent: 50, hold: 20m, guards: [error_rate, p99_latency] }
    - { percent: 100, hold: 0 }
  abort_on: { error_rate_delta: 0.005, p99_latency_delta: 0.2 }
  # 关键：不一致窗口内，两级配置必须语义兼容
  compat_window: 30m
```

> 💡 注意最后那行 `compat_window`：配置灰度的本质是「**一段时间内系统里同时存在两套阈值**」。所以任何配置的演进都必须满足「旧值和新值同时存在时不出错」——这跟数据库的 expand-contract 是同一个道理。做不到兼容的配置项（比如改了字段语义），不能灰度，只能停机切换或双字段并行。

---

## 43.5 Java / 分布式经验对照：你踩过的坑，这里会有升级版

| 你熟悉的 | AgentOS 对应 | 同构点 | 会咬人的地方 |
| --- | --- | --- | --- |
| Nacos `Listener` / Apollo `@Value` | 配置订阅 | 推送 + 本地缓存 | Nacos 的推送是「尽力而为」，必须有拉取兜底；Apollo 有本地缓存文件，Nacos 需要自己做 |
| `@RefreshScope` | 配置热生效 | 无需重启 | 它只重建 Bean，**不重建已经持有旧值的对象**；长生命周期的连接池/线程池里的旧值不会变（半生效，最难查） |
| Spring Cloud Config `/actuator/refresh` | 手动刷新 | 显式生效 | 谁调用了 refresh？没调用的实例就是旧的。**没有版本号你无法证明全站生效** |
| Apollo 的灰度发布（`grayReleaseRule`） | 配置灰度 | 按 IP/部门灰度 | 只按客户端 IP，不按租户；Agent 平台要按**租户**灰度 |
| Sentinel 规则推送（push mode） | 规则中心 | 规则与代码分离 | Sentinel 的规则是「集群内存态」，重启丢；必须持久化 + 版本化 |
| Zookeeper/etcd `watch` | 变更通知 | 强一致 | watch 会丢事件（需重新 list）、有羊群效应；不适合 100k 实例直连 |
| 数据库配置表 + 定时 reload | 土办法 | 简单 | 轮询间隔内不一致；100k 实例 × 每分钟一次 = 打爆库 |
| 灰度发布 = 改 Nginx upstream | 流量切分 | 比例可控 | 无法按租户切；也无法切「提示词」这种非流量维度 |

**一个 Java 架构师最容易犯的错**：把 Agent 的配置当成 Spring 的 `application.yml` 来管——分层继承、`${}` 占位符、profile 切换。但 Agent 配置的核心不是「结构」，是**「这次变更会影响谁、影响多少、怎么退」**。所以它的第一等公民是 `diff`、`impact`、`change_ticket`，而不是 `YAML 缩进`。

---

## 43.6 开源坐标：这块的成熟度其实很高，但都不是给 Agent 用的

| 工具 | 它有什么 | 缺什么 |
| --- | --- | --- |
| **LaunchDarkly** | Flag targeting（用户/属性/百分比）、审计日志、实验、approval workflow、SDK 端本地求值 | 商业闭源、按 MAU 计费（10 万租户会很贵）；不解决「配置是行为」的语义校验；不管提示词与路由 |
| **Unleash** | 开源 flag、activation strategies、gradual rollout、variant | 强在 flag，不强在「阈值/路由/降级阶梯」这类结构化配置；无影响面预演 |
| **OpenFeature**（CNCF） | 厂商无关的 flag 标准 API + provider 生态 | 只定义求值接口，不定义治理流程（审批/审计/diff）；可以用它做客户端抽象层，省得锁死厂商 |
| **Apollo**（携程） | 配置中心、namespace、灰度、发布历史、回滚、权限 | 面向「应用配置」，不理解租户维度；校验能力弱（只有格式）；没有影响面预演 |
| **Nacos** | 配置 + 注册中心、长轮询推送、beta 灰度 | 同上；且推送可靠性依赖客户端兜底拉取 |
| **etcd / Consul** | 强一致 KV + watch | 无版本语义、无审批、无 diff；100k 实例直连会打爆 |
| **GitOps（Argo CD / Flux）** | 配置即代码、PR review、可回滚、审计天然 | 只解决「写入侧」；不解决「运行时求值与 5 秒生效」 |

**这块的自研边界很清楚**：客户端 SDK 用 OpenFeature 抽象，存储与推送用 Apollo/Nacos/自建都行，**真正必须自研的是「校验 + 影响面预演 + 按租户灰度 + 与发布流程（第 42 章）打通」这四件事**——因为只有你知道「这个阈值改了会打到谁」。

---

## 43.7 事故清单

- [ ] **事故 A：单位错 1000 倍（本章引子）。**
  现象：600 RPM 改成 6000 RPS，4 分钟后全站降级。根因：数值与单位分离、无破坏性变更守卫、审计无改前值。兜底：单位进类型 + 漂移倍数守卫（>5 倍需双人）+ diff 结构化存储。
- [ ] **事故 B：配置推送乱序，配置自己变回去了。**
  现象：10:00 把采样率调到 100%，10:02 又变回 10%，没人操作。根因：两个实例的推送乱序到达，旧版本覆盖新版本。兜底：版本号单调 + `v.Version <= seq` 丢弃 + 哈希链断裂触发全量拉取。
- [ ] **事故 C：配置中心重启，全站实例起不来。**
  现象：滚动更新时新 Pod 全部 CrashLoop，因为启动时要拉配置，而配置中心在重启。根因：配置中心成了硬依赖。兜底：磁盘 last-known-good + 启动先用本地快照 + 安全默认值。
- [ ] **事故 D：`@RefreshScope` 半生效。**
  现象：改了模型名，80% 实例生效，20% 还在用旧模型，持续 6 小时。根因：长生命周期对象持有旧值，且没有「版本号上报」。兜底：实例定期上报 `applied_version`，控制台给出「生效覆盖率」，不到 100% 不认为发布完成。
- [ ] **事故 E：删了一个「看起来没人用」的配置项。**
  现象：全站 NPE。根因：`Get` 返回 nil，调用方直接类型断言。兜底：`Get` 返回 `(value, ok)` + schema 里标 `required` + 配置项只能标记 deprecated，不能直接删（两个版本后才删）。
- [ ] **事故 F：一个 flag 挂了三年，没人敢关。**
  现象：代码里有 400 个 flag，评估逻辑占了 30% 的分支复杂度。根因：没有 owner 和过期日。兜底：`owner` + `expires_at` 必填，到期自动开单 + 季度清理。
- [ ] **事故 G：配置灰度期间两套阈值不兼容。**
  现象：灰度 50% 时，A 实例按新语义把 `limit=0` 理解为「不限」，B 实例理解为「全禁」。根因：改了字段语义却做了灰度。兜底：`compat_window` + 语义变更只能双字段并行（`limit_v2` 上线，`limit` 保留两个版本）。

---

## 43.8 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 核心论断 | 配置已是「代码的一种形态」，但治理待遇还不如代码；**配置事故 ≈ 60% 的 P1** |
| 十条必热更 | 灰度开关/限流阈值/降级阶梯/路由/提示词/角色卡/配额/审核阈值/采样/缓存 |
| 读放大 | 5k QPS × 40 项 = 20 万读/秒 → 整份配置（690 KB）装进内存，读零网络 |
| 分层 | 7 层，标量覆盖 / map 深合并 / **list 默认替换** |
| 单位 | 数值和单位必须是同一个原子类型，错误配置才「表达不出来」 |
| 守卫 | 相对 30 天峰值放大 >5 倍 → 双人审批（引子事故的解药） |
| 快照 | 不可变 + 单调版本 + `prev_hash` 哈希链 + 结构化 diff + 变更单号 |
| 客户端 | 原子指针切换 + 版本单调丢弃 + 磁盘 last-known-good + `(v,ok)` |
| flag 三律 | 安全默认值（读不到=关闭）、kill switch 独立通道、owner+过期日 |
| 配置灰度 | 本质是「一段时间两套值共存」，必须有 `compat_window` 与语义兼容 |
| 回滚 | 回滚 = 指针指回旧 version，1 秒；旧快照永不删除 |
| 一句话 | **能热更的才敢调，敢调的才救得了火；配置的治理强度要等于代码。** |

## 43.9 自检题

1. **你说「我们的配置有版本管理」。请给出三个能证明它真的可回滚的证据。**
   > 要点：① 快照不可变（改配置生成新 version，不 UPDATE）；② 结构化 diff 能回答「改前是多少、变了多少倍」；③ 每个实例上报 `applied_version`，控制台能看到生效覆盖率 100%；④ 回滚是 1 秒级的指针切换，不是重新发布；⑤ 旧快照可查（上周三下午的限流值能回答）。

2. **为什么「把配置全塞进每个实例内存」不是浪费，而是唯一正确解？**
   > 要点：① 算账：5k QPS × 40 项 = 20 万读/秒，RPC 化需要 20 万 QPS 的配置中心；② 整份配置 690 KB（压缩 80 KB），相对一个 3 GB 的 Agent Worker 是 0.02%；③ 好处是读零延迟、配置中心故障不影响数据面、可以原子切换；④ 代价是要解决「版本单调 + 一致性」——用哈希链与全量拉取兜底。

3. **一个 flag 从创建到删除，应该经过哪些环节？漏掉哪个最容易出事？**
   > 要点：创建（owner + expires_at + 安全默认值）→ 灰度（按租户/百分比，带护栏）→ 全量 → 代码里清理分支（**最容易漏**）→ 删 flag → 删配置。最容易漏的是「代码分支清理」：flag 全量后代码里 `if flag {} else {}` 的 else 分支还在，一年后没人敢删，形成 flag 债务；其次是 `expires_at`，没有它，400 个 flag 会永久留在求值路径上。

## 43.10 延伸阅读

- 上一章：[42. 模型与提示词的迭代上线](42-model-release.md)（`release_id` 怎么设计、灰度怎么判定）
- 下一章：[44. 多租户与资源治理](44-multi-tenant.md)（配置里的 `tenant_id` 维度怎么贯穿）
- 总纲：[0-summary.md](0-summary.md)（§5「Spring 配置中心 → 策略中心」的差异行）
- 相关：[23. 多层限流与配额体系](23-rate-limit-quota.md)（限流阈值的语义）、[25. 降级与容错](25-degradation-failover.md)（降级阶梯与 kill switch）、[26. 模型路由与多策略](26-model-routing.md)（路由规则配置化）、[49. 低代码配置](49-lowcode-agent-builder.md)（配置与代码的边界，别做成图灵完备）、[31. 合规与审计](31-compliance-audit.md)（配置变更的审计取证）
- 外部：LaunchDarkly / Unleash / OpenFeature 规范、Apollo 与 Nacos 官方文档、Argo CD（GitOps 写入侧）
- 语言细节：[../go/22-sync-primitives.md](../go/22-sync-primitives.md)（`atomic.Pointer` 与无锁读）、[../go/24-context.md](../go/24-context.md)（订阅的取消传播）
