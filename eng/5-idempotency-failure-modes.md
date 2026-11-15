# 5. 幂等的失效模式与测试：10 种翻车方式

> **状态**：`已完成（初稿）` ｜ **所属**：第一部分 · 幂等：你到底「确保」了什么 ｜ **编号**：5 / 52
> **本章定位**：幂等不是「上线时加了个唯一索引」就一劳永逸——它会在键、窗口、事务边界、异常处理、重试链、时钟和锁这七个地方，以 10 种固定姿势翻车。本章每种给「现象 → 根因 → 怎么查 → 怎么修 → 怎么防复发」，再给一套能进 CI 的测试方法。
> **读法**：§5.6 那张 10 行表当评审清单用；§5.7 的 k6/Go 脚本与 chaos 注入点可以直接抄；急着排障只读 §5.2 的「六类漏点」图，它决定了你该往哪儿找。

---

## 5.1 引子：压测通过的那套代码，三天后重复下单 3,700 笔

2025-08-15，某零售平台大促前压测：200 并发、同一个下单接口，结果只有 1 单，团队签字上线。

2025-08-18 20:00 大促开闸，90 分钟内：

- 同一用户重复下单 **3,700 笔**，其中 **1,140 笔已支付**；
- 退款 + 补偿券 + 客服工时合计 **120 万元**，客诉 **486 起**；
- 库存被多扣 5,200 件，两个爆款提前售罄，正常用户看到「无货」。

复盘时发现压测和生产的差异只有一个：

```
压测脚本：每次请求新生成 UUID 当 Idempotency-Key      → 测的是「200 个不同请求的并发」
真实链路：网关超时重试 3 次 × 服务内部重试 3 次
          服务每次重试都 UUID.randomUUID()            → 9 次请求、9 个不同的键
          且网关升级时把客户端传的 X-Request-Id 覆写成自己的 trace id
结果：幂等表一次都没命中，业务执行了 9 次
```

> 🩸 **本章最贵的一条**：**压测必须模拟「同一个业务意图的重复」，而不是「N 个不同业务意图的并发」。** 前者验证幂等，后者只验证吞吐。大部分团队的幂等测试都测错了对象。

---

## 5.2 到底是什么：失效 = 「同一个意图被执行了两次以上」

先把「幂等失效」定义清楚，否则你会一直在错误的地方找 bug。

**定义**：给定一次业务意图（用户点了一次「提交订单」），若系统对其产生的**持久副作用**超过一份（多一条订单、多一笔扣款、多一条消息、多一次第三方调用），即为幂等失效。注意判据落在**副作用**上，不落在「接口返回了什么」上——返回 200 + 「已处理」但券发了两张，仍然是失效。

幂等能成立，靠的是三个不变量，**任何一条破了就会出现对应的失效模式**：

| 不变量 | 含义 | 破了会怎样 | 对应模式 |
| --- | --- | --- | --- |
| **键稳定** | 同一意图，任何时候算出的键都相同 | 每次重试都是「首次」 | 1、7、8 |
| **判定与生效原子** | 「确认没做过」与「做」之间没有窗口，或窗口被唯一约束兜住 | 并发下多条生效 | 3、4、5、6 |
| **副作用可外部验证** | 有独立的账/流水/对账能证明「只发生了一次」 | 失效了也发现不了 | 全部（这是最后一道） |

### 四个「不是幂等」的反例

| 你以为的防护 | 缺口在哪 | 不做会怎样 |
| --- | --- | --- |
| 加了唯一索引 | 索引只能挡住**相同的键**；键漂移时它一次都不会冲突 | 重复 9 条，索引一条没报错 |
| Redis `SETNX` + 24h TTL | 渠道回调最长重试 72h，第 25 小时起就裸奔 | 隔天重复入账，T+1 才发现 |
| 事务包住 | 跨库、跨线程、调三方时事务根本不覆盖（见第 7 章） | 幂等表 DONE、业务表没有 |
| 分布式锁 | 锁到期 ≠ 业务结束；GC/主从切换都会让锁提前没 | 两个线程同时进临界区 |

### 六类漏点：失效只从这六个地方进来

| 类别 | 根因一句话 | 模式编号 |
| --- | --- | --- |
| **键** | 键里混进可变字段，或键根本没从客户端传下来 | 1、7 |
| **窗口** | 「先查后写」的检查与写入不原子 | 3 |
| **边界** | 幂等表与业务表不在同一个事务/同一个库 | 4、6 |
| **异常** | 唯一索引冲突被 `catch` 吞掉，错误被当成功 | 5 |
| **重试链** | 多层重试叠加，每层都可能换键 | 7、8 |
| **时间与锁** | TTL 太短、时钟回拨、锁提前释放 | 2、9、10 |

一个反直觉但要记住的结论：**10 种里有 7 种不是「幂等组件坏了」，而是「幂等组件和业务的关系错了」**。所以排查时先画事务边界和重试链路，别一上来就怀疑 Redis。

---

## 5.3 结构图：重复怎么被放大，10 种模式落在哪

```
① 重复的放大链（模式 7、8 的母题）

   客户端超时重试 ×3
        │  携带 Idempotency-Key: A
        ▼
   网关重试 ×3           ← 模式 7：覆写/丢弃 header，键变成 B、C、D
        │
        ▼
   服务内部重试 ×3       ← 模式 7：UUID.randomUUID()，键变成 E…M
        │
        ▼
   下游收到 3×3×3 = 27 次
        ├─ 键稳定 → 幂等表命中 26 次，业务执行 1 次   ✅
        └─ 键漂移 → 27 次全是「首次」，业务执行 27 次  🩸

② 幂等判定必须发生在副作用之前，且与副作用同一个原子边界

   [入口] ──► 键计算 ──► 占位(INSERT) ──┬──► 业务写 ──► 标记 DONE ──┐
      │            模式1  │  模式3       │      模式4  │             │ 同一事务
      │                   │  模式6       │              │             │（模式 6）
      │                   ▼              │              ▼             ▼
      │              键漂移/含时间戳     └─► 模式 5：唯一键冲突被 catch 吞
      │
      └─► 模式 2：TTL 到期后占位消失 ──► 重复放行
          模式 9：时钟回拨 → 键重复/乱序 → 冲突被误判为「重复请求」
          模式 10：锁 TTL 到期 → 第二个线程进临界区（占位还没写）

③ 观测点（第 6 章展开）

   hit / miss / conflict / store_error 四个计数 + span attribute
   命中率突降 = 模式 1、7、8 的信号；PROCESSING 积压 = 模式 6 的信号
```

---

## 5.4 怎么实现：把三个不变量做成结构

不要指望「小心一点」。把不变量变成表结构和代码形状，失效模式就没地方落。

### 5.4.1 幂等表：一张表兜住 6 种模式

```sql
CREATE TABLE idempotency_record (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  scope         VARCHAR(64)  NOT NULL COMMENT '业务域：order:create / pay:callback / refund:apply',
  key_hash      CHAR(64)     NOT NULL COMMENT 'sha256(scope|client_token)，不存原始键（可能含手机号）',
  status        VARCHAR(16)  NOT NULL COMMENT 'PROCESSING / DONE / FAILED',
  owner_node    VARCHAR(64)  NOT NULL COMMENT '抢占者，便于排查是谁卡住了',
  lease_until   DATETIME(3)  NOT NULL COMMENT 'PROCESSING 的租约；到期视为「上次执行者已死」',
  response_code SMALLINT     NULL,
  response_body MEDIUMBLOB   NULL COMMENT '重放用；超过 64KB 只存引用',
  expire_at     DATETIME(3)  NOT NULL COMMENT 'TTL，必须 ≥ 重试窗口 × 2（模式 2）',
  created_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_idem_scope_key (scope, key_hash)     -- ★ 唯一约束是真正的幂等执行者
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='幂等表：按 expire_at 做月度分区，过期分区直接 DROP';
```

三个设计决定，每个都对应一种失效模式：

| 决定 | 挡住的模式 | 不做会怎样 |
| --- | --- | --- |
| `UNIQUE KEY (scope, key_hash)` | 3、4 | 并发 200 时出现 3~5 条，「先查后写」窗口永远关不上 |
| `status + lease_until` 两字段 | 6、10 | 进程被 KILL 后 `PROCESSING` 永久卡死，重试永远 409 |
| `expire_at` 显式落库 + 分区 | 2 | 靠 Redis TTL 兜，TTL 一短就重复；且无法审计「键活过多久」 |

> ⚠️ **`scope` 不能省**。同一个 `request_id` 可能对应「支付」和「退款」两种业务，只用 `request_id` 做唯一键，退款会被支付记录挡住——这是第 3 章 §3.4.2 踩过的同一个坑。

### 5.4.2 判定与执行：接管、重放、抢占三分支

关键点：**先 `INSERT` 抢占，再干活**，而不是「先查再插」。查出来的「不存在」在并发下毫无意义。

```go
// 复用第 7 章的 InTx 保证「占位 + 业务写 + 标 DONE」在同一事务
func (s *IdemService) Do(ctx context.Context, scope, clientToken string,
	fn func(context.Context, *sql.Tx) (any, error)) (resp any, replayed bool, err error) {

	keyHash := sha256hex(scope + "|" + clientToken)
	rec, owner, err := s.claim(ctx, scope, keyHash) // INSERT → 冲突则读回并判断
	if err != nil {
		return nil, false, err
	}
	if !owner {
		switch rec.Status {
		case "DONE":
			return rec.Payload(), true, nil // 重放：返回与首次一致的结果（模式 5 的正解）
		case "PROCESSING":
			// 别人正在执行：既不重复执行，也不能报 500 让上游改成「失败」→ 立刻重试
			return nil, false, ErrInProgress // 调用方回 409/425 + Retry-After: 1
		}
	}

	err = s.tx.InTx(ctx, func(ctx context.Context, tx *sql.Tx) error {
		out, e := fn(ctx, tx) // 业务写：与占位同一事务
		if e != nil {
			return e // 一起回滚 → 占位消失，下次重试可重新接管（模式 6 的正解）
		}
		return s.markDone(ctx, tx, scope, keyHash, out)
	})
	if err != nil {
		_ = s.release(ctx, scope, keyHash) // 只对「可判定失败」的异常释放，未知异常留租约
		return nil, false, err
	}
	return resp, false, nil
}

func (s *IdemService) claim(ctx context.Context, scope, keyHash string) (Record, bool, error) {
	_, err := s.db.ExecContext(ctx, `INSERT INTO idempotency_record
		(scope, key_hash, status, owner_node, lease_until, expire_at)
		VALUES (?, ?, 'PROCESSING', ?, DATE_ADD(NOW(3), INTERVAL 15 SECOND),
		        DATE_ADD(NOW(3), INTERVAL 7 DAY))`, scope, keyHash, s.nodeID)
	if err == nil {
		return Record{}, true, nil // 抢到了
	}
	if !isDupKey(err, "uk_idem_scope_key") {
		return Record{}, false, err // ★ 别的唯一键冲突不是「重复请求」，必须抛
	}
	rec, e := s.get(ctx, scope, keyHash)
	if e != nil {
		return Record{}, false, e
	}
	if rec.Status == "PROCESSING" && rec.LeaseUntil.Before(time.Now()) {
		// 租约过期：上次执行者大概率已死，条件更新抢占（不是无脑覆盖）
		res, e := s.db.ExecContext(ctx, `UPDATE idempotency_record
			SET lease_until = DATE_ADD(NOW(3), INTERVAL 15 SECOND), owner_node = ?
			WHERE scope = ? AND key_hash = ? AND status = 'PROCESSING' AND lease_until < NOW(3)`,
			s.nodeID, scope, keyHash)
		if e == nil && mustAffected(res) == 1 {
			return rec, true, nil
		}
	}
	return rec, false, nil
}
```

### 5.4.3 唯一键冲突：只认自己那张约束

Java 侧最常见的事故写法，和正确写法只差五行：

```java
// ❌ 事故写法：把所有 1062 都当「重复请求」，接口返回 200，订单实际不存在（模式 5）
try {
    return orderService.create(cmd);
} catch (DuplicateKeyException e) {
    return Result.ok();          // 空 catch 的变种，最致命
}

// ✅ 正确写法：先认约束名，命中幂等键才重放；否则原样抛出
try {
    return txTemplate.execute(st -> {
        idemMapper.insertProcessing(scope, keyHash, 15);
        Order o = orderService.create(cmd);          // 同一事务
        idemMapper.markDone(scope, keyHash, 200, Json.write(o));
        return o;
    });
} catch (DuplicateKeyException e) {
    if (constraintOf(e).filter("uk_idem_scope_key"::equals).isEmpty()) {
        throw e;                                     // 库存唯一键、订单号重复 → 真错误
    }
    Record rec = idemMapper.get(scope, keyHash);      // 冲突后必须读回，不能猜
    if ("DONE".equals(rec.getStatus())) {
        return Json.read(rec.getResponseBody(), Order.class); // 重放同一响应
    }
    throw new InProgressException("duplicate request in flight");
}
```

> 💡 `constraintOf(e)` 是解析驱动异常 message 里的键名——**跨 MySQL 版本不稳**。更稳的做法是：捕获后按 `(scope, key_hash)` 读回记录，读到就按记录分支，读不到才抛出（说明是别的唯一键冲突）。这条规则应该写进团队规范。

---

## 5.5 双修对照：传统 vs Agent

| 维度 | 传统系统 | Agent 系统 | 同 / 异 |
| --- | --- | --- | --- |
| **机制本体** | 幂等表 + 唯一索引 + 状态机前置校验 | 完全相同，一张表复用（`run_id/step_id` 代替 `request_id`） | **同（可直接复用）** |
| **唯一键 / 身份** | `request_id` / 订单号 / 渠道流水号 | `run_id` + `step_id` + `tool_call_id`，且必须含模型版本与提示词版本 | 结构同构，**键的组成多两维** |
| **失效模式** | 本章 10 种 | 上述 10 种全有，另加两类：**prompt hash 漂移**（同参不同键）、**流式半失败**（已输出 800 token 后连接断，重放算不算重复） | 多两类 |
| **恢复方式** | 重试整个请求 / 人工修数 | 重试 = 重新花钱；只能靠 checkpoint + 补偿，且「已消耗的 token 不可退还」 | 差异根因：**成本显性 + 非确定性** |
| **验证方法** | 200 并发同参 + 对账 SQL | 同 + **重放同一条 trace** + token/调用次数对账（详见第 14 章） | 方法同构，多一层 |
| **观测指标** | 幂等命中率、错误率 | 同 + `idempotent_hit` 与**计费失败率**绑定（命中一次可能已经烧了 2,000 token） | 指标集扩展 |
| **成本结构** | 一次重复 = 一条脏数据 | 一次重复 = 脏数据 + 真金白银（工具调用可能还带了副作用） | 差一个量级 |

**哪些完全相同、可以直接复用**：幂等表结构与 `uk(scope, key_hash)`、`PROCESSING/DONE + lease` 状态机、唯一键冲突的分支处理、200 并发同参压测脚本、对账 SQL。**这套东西一行都不用改。**

**差异根因（对齐总纲 §1.5 四条）**：

1. **非确定性** → 键不能取 prompt 内容 hash（检索结果顺序、时间戳、采样温度都会变），只能取 `run_id+step_id` 这类稳定 ID。
2. **成本显性** → 幂等在 Agent 侧多了个新目标：**不是「防止副作用两次」，而是「防止两次收费」**。所以幂等判定必须发生在**调用模型之前**，不能等结果回来再判。
3. **流式半失败** → 「已吐了 800 token 连接断了」这种半成品状态传统系统没有，必须显式定义：**按 step 粒度作废重来，已消耗的 token 冲正（详见第 3 章 §3.5）**。
4. **长事务** → 一次 Run 可能 30 秒到 10 分钟，`lease_until` 必须按「最长单步耗时 × 3」配，不能沿用传统系统的 15 秒。

---

## 5.6 会在哪炸：10 种失效模式

### 5.6.1 全景表（评审会逐行过）

| # | 模式 | 现象（现场长什么样） | 根因 | 怎么查（日志·指标·SQL） | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | **幂等键含可变字段** | 同一业务重复 2~5 条，每次键都不同 | 键里带 `timestamp`/`trace_id`/`update_time`/随机数/prompt hash | 日志里并排打两次的键逐字段 diff；`SELECT user_id,sku_id,COUNT(DISTINCT idem_key) FROM orders GROUP BY 1,2 HAVING 2>1`；指标：命中率从 12% 掉到 0.3% | 键只用「客户端稳定 token + scope」；版本显式进键 | 键 schema 单测：同一请求重放 10 次，键必须相同；键定义进设计文档模板 |
| 2 | **TTL 太短** | 重复间隔 > TTL 时开始重复，多见于跨天 | TTL 小于渠道/网关最大重试窗口 | `SELECT MIN(created_at),MAX(created_at) FROM 重复单`；指标 `idempotency_age_at_hit` 逼近 TTL 就有风险 | 把 `expire_at` 落到表里并按「渠道 SLA 最大重试 72h × 2 = 7 天」配 | TTL 与重试策略同源配置（一份 YAML 同时生成重试参数和 TTL），改一个必改另一个 |
| 3 | **先查后写窗口** | 200 并发下出 2~5 条，压测单并发永远复现不了 | `if (!exists) insert`；查主库/从库不一致 | `SELECT idem_key,COUNT(*) c FROM t GROUP BY 1 HAVING c>1`；从库延迟指标 `Seconds_Behind_Master` | 唯一索引 + `INSERT` 抢占，删除所有 check-then-act | 代码扫描禁 `exists` + `insert` 相邻出现；集成测试强制 200 并发 |
| 4 | **跨库不同步** | 幂等表 DONE、业务表没记录（或反之），接口 200 但查不到单 | 幂等表在 Redis/独立库，与 MySQL 业务表无同事务 | `idempotency_record LEFT JOIN orders ON request_id=key` 查出单边；Redis `TTL` 与 MySQL 行数比对 | 同库同事务；跨库则改「状态机 + 补偿 + 对账」，禁止先写幂等表 | 事务边界图画进设计文档；跨库必须写「差异由哪次对账兜」 |
| 5 | **唯一键冲突被 catch 吞** | 接口 200 但订单不存在；少卖但用户以为成功 | 空 catch；把所有 1062 都当重复请求 | 业务表与接口返回日志对不上；日志里无异常但订单缺失；指标 `dup_swallowed_total` 无埋点则查不到 | 冲突后必须读回记录再决定；按约束名/读回结果分流 | 禁止空 catch（lint 规则）；新接口必须有过「制造冲突 → 期望 500」的用例 |
| 6 | **幂等表与业务表不同事务** | `PROCESSING` 永久卡死，重试永远 409；或业务成功但幂等表回滚 → 下一次重复 | 两个 `DataSource`；`@Transactional` 自调用失效（第 7 章）；`@Async` 线程 | `SELECT COUNT(*) FROM idempotency_record WHERE status='PROCESSING' AND updated_at < NOW()-INTERVAL 5 MINUTE`；`age>1min` 即异常 | 同事务；退一步用 `lease_until` 租约回收 | 幂等表与业务表**强制同库**；跨库要在评审时说明并由对账兜 |
| 7 | **重试换新 request_id** | 同一业务单对应多个 `request_id`；每层都「首次」 | 网关/客户端/服务各生成一份；中间件覆写 header | trace 里同一业务单出现多个 `request_id`；网关 access log 与上游入参比对 | 客户端生成、全链路透传、网关只读不写 | header 白名单：网关只允许 `X-Request-Id` 透传，禁止 rewrite（配置评审项） |
| 8 | **网关重试 + 服务重试叠加** | 重复 9~27 次；下游被打爆、日志刷屏 | 多层重试、每层独立、无重试预算 | 入口 QPS ÷ 唯一业务意图数 > 1.3；`X-Retry-Count` 累加值；下游收到次数 | 只留一层重试（推荐网关不重试、服务重试）；全局重试预算 ≤ 原流量 20% | 重试拓扑图画出来贴在 SRE 文档；重试指标进看板 |
| 9 | **时钟回拨** | 发号器 ID 重复 → 唯一索引冲突 → 被模式 5 吞掉变成「少卖」；或基于时间窗口的键互相覆盖 | NTP 回拨、容器时钟漂移、VM 迁移 | `ntpq -p`/`chronyc tracking` 的 offset；`SELECT entry_no,COUNT(*) FROM journal_entry GROUP BY 1 HAVING COUNT(*)>1` | 发号器检测回拨：`if now < lastTs { wait | reject }`；workerId 隔离 | 时钟 offset 监控（>50ms 告警）；**禁止用时间做唯一键的任何组成部分** |
| 10 | **分布式锁超时提前释放** | 并发重复；锁 TTL 3s、业务 GC STW 4.2s | 锁过期；Redis 主从切换丢锁；续期线程被饿死 | 锁持有时间 P99 vs TTL 的比值（>0.5 就危险）；Redis `slowlog`；GC 日志里的 STW | 看门狗续期（Redisson `lockWatchdogTimeout`）+ **锁内再查结果表**；跨资源用 fencing token | 锁只做「减少并发」，正确性必须由结果表/唯一键保证；锁 TTL ≥ 业务 P99 × 3 |

### 5.6.2 三处最值得单独讲的

**① 模式 2 的 TTL 是一条契约，不是一个数字。** 算一遍最慢重试链：客户端 9 分钟 + 网关异步 30 分钟 + 渠道隔日补发 72 小时 = **73 小时**。TTL 配 24 小时，第 25 小时的回调就是全新业务。**规则：TTL ≥ 最长重试窗口 × 2**，并在注释里写清「这个窗口是谁给的」。

**② 模式 4/6 的边界，先问「谁和谁必须一起成功」。** 幂等表在 Redis、业务在 MySQL 时，它就不是幂等表，只是**去重缓存**，正确性得靠「业务唯一索引 + 对账」。这一句能省掉半年的「偶发重复」工单。

**③ 模式 9/10 其实都不是幂等问题，是时间问题。** 正解一致：**不要用时间/锁承担正确性**——唯一键用带回拨保护的发号器，串行化用「锁 + 结果表」，锁只算性能优化。

---

## 5.7 怎么验证：一张测试矩阵 + 三个可执行件

### 5.7.1 测试矩阵（10 种模式各自怎么测）

| # | 模式 | 测试方法 | 通过判据 |
| --- | --- | --- | --- |
| 1 | 键含可变字段 | 固定 `clientToken`，其余参数（含 `ts`、`traceId`）每次随机，重放 20 次 | 业务表 1 条；幂等表 hit = 19 |
| 2 | TTL 太短 | 把 TTL 改成 1s，sleep 3s 后重放 | **应当重复**（证明测试有效）；恢复 7 天后必须不重复 |
| 3 | 先查后写 | 200 并发同参（k6/Go 脚本） | 业务表 1 条，无 5xx |
| 4 | 跨库不同步 | 在「幂等表写成功、业务写之前」注入 `kill -9`（PodChaos） | 重启后：要么都回滚可重来，要么租约到期接管；最终 1 条 |
| 5 | 冲突被吞 | 手工插入一条**非幂等键**的冲突数据（如相同 `order_no`），再请求 | 返回 5xx/4xx，**不能返回 200** |
| 6 | 不同事务 | 业务 `fn` 里 `return errors.New(...)` 抛错 | 幂等表**无残留**（一起回滚）或租约 15s 后可重试 |
| 7 | 换新 request_id | 用 toxiproxy 在网关注入 502，触发客户端重试 | 下游收到的 header 键**完全相同** |
| 8 | 重试叠加 | 同时开启网关重试(3) 与服务重试(3)，注入 500 | 下游收到次数 ≤ 3（只有一层重试生效） |
| 9 | 时钟回拨 | `libfaketime` 把进程时钟回拨 10s 后连续发号 1 万次 | 零重复；发号器打印回拨告警 |
| 10 | 锁提前释放 | 业务 `sleep 2×TTL`（模拟 STW），并发 2 个请求 | 结果表/唯一键挡住第二个；业务只执行 1 次 |

### 5.7.2 k6：200 并发「同一个请求」

```js
// k6 run -e BASE=http://order.internal ./idem-200.js
import http from 'k6/http';
import { check } from 'k6';

export const options = {
  scenarios: {
    same_request: {
      executor: 'per-vu-iterations',
      vus: 200,
      iterations: 1,        // 200 个 VU 各打 1 次，总计 200 次
      maxDuration: '60s',
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],   // 重复请求不该产生 5xx
  },
};

const KEY = 'it-20250818-0001';       // ★ 固定键：这才是「重复请求」

export default function () {
  const res = http.post(`${__ENV.BASE}/api/orders`,
    JSON.stringify({ skuId: 'SKU-9', qty: 1, userId: 'U-1001', ts: Date.now() }),
    { headers: { 'Content-Type': 'application/json', 'Idempotency-Key': KEY },
      timeout: '5s' });
  check(res, {
    'status is 2xx/409/425': (r) => [200, 201, 409, 425].includes(r.status),
  });
}
```

跑完立刻用一条 SQL 判定成败（**不要看接口返回，看业务表**）：

```sql
-- 判据：1 条（允许 0 条且幂等表全为 FAILED，那说明业务全失败，另有问题）
SELECT COUNT(*) AS orders, COUNT(DISTINCT request_id) AS distinct_keys
  FROM orders WHERE request_id = 'it-20250818-0001';
-- 期望 orders = 1，distinct_keys = 1
```

### 5.7.3 Go：带断言与竞态检测的并发用例（进 CI）

```go
func TestSameRequest200Concurrent(t *testing.T) {
	const n, key = 200, "it-20250818-0001"
	var wg sync.WaitGroup
	codes := make([]int, n)

	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			body := `{"skuId":"SKU-9","qty":1,"userId":"U-1001"}`
			req, _ := http.NewRequest(http.MethodPost, baseURL+"/api/orders", strings.NewReader(body))
			req.Header.Set("Content-Type", "application/json")
			req.Header.Set("Idempotency-Key", key) // 同一个键 = 同一次业务意图
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				codes[i] = -1
				return
			}
			defer resp.Body.Close()
			codes[i] = resp.StatusCode
		}(i)
	}
	wg.Wait()

	var cnt int
	if err := db.QueryRow(`SELECT COUNT(*) FROM orders WHERE request_id = ?`, key).Scan(&cnt); err != nil {
		t.Fatal(err)
	}
	if cnt != 1 {
		t.Fatalf("重复下单：期望 1 条，实际 %d 条（codes=%v）", cnt, codes)
	}
	for i, c := range codes {
		if c >= 500 || c == -1 {
			t.Fatalf("第 %d 个请求返回 %d：重复请求必须返回 2xx/409/425，不能 5xx", i, c)
		}
	}
}
```

> 💡 这个用例跑 `go test -race -count=5` 才有意义。跑一次通过说明不了什么——窗口期竞态就是「跑 5 次过一次」的那类问题。

### 5.7.4 chaos 注入点清单（Chaos Mesh / toxiproxy）

| 注入点 | 手段 | 期望观察 | 观察不到就说明 |
| --- | --- | --- | --- |
| 占位后、业务写前 `kill -9` | `PodChaos{action: pod-kill}` | 租约 15s 后被接管，最终 1 条 | 模式 6 存在（PROCESSING 永久卡死） |
| Redis 主从切换（锁丢失） | `NetworkChaos{partition}` + 手工 `failover` | 第二个请求被结果表挡住 | 模式 10 存在（只靠锁） |
| 网关 502 + 客户端重试 | `HTTPChaos{abort}` | header 键不变、下游收到次数 ≤ 重试预算 | 模式 7/8 存在 |
| 进程时钟回拨 10s | `libfaketime` / 容器 `CLOCK_REALTIME` 偏移 | 发号器拒绝发号并告警 | 模式 9 存在 |
| 从库延迟 5s | `NetworkChaos{delay}` | 「先查从库」的实现出现重复 | 模式 3 存在 |
| MySQL 唯一索引冲突 | 造数据（非幂等键） | 返回 4xx/5xx，不是 200 | 模式 5 存在 |

### 5.7.5 对账 SQL：失效之后唯一能翻案的东西

```sql
-- ① 键漂移：同一用户同一 SKU 在 5 分钟内多个键（模式 1/7）
SELECT user_id, sku_id, COUNT(*) AS rows_cnt,
       COUNT(DISTINCT request_id) AS key_cnt,
       MIN(created_at) AS first_at, MAX(created_at) AS last_at
  FROM orders
 WHERE created_at >= NOW() - INTERVAL 10 MINUTE
 GROUP BY user_id, sku_id
HAVING rows_cnt > 1
 ORDER BY rows_cnt DESC LIMIT 50;

-- ② 幂等表与业务表不一致（模式 4/6）
SELECT i.scope, i.status, COUNT(*) AS cnt
  FROM idempotency_record i
  LEFT JOIN orders o ON o.request_id = i.key_hash
 WHERE i.created_at >= NOW() - INTERVAL 1 DAY
   AND i.scope = 'order:create'
   AND ((i.status = 'DONE' AND o.id IS NULL)      -- 说成功但没单
     OR (i.status = 'PROCESSING' AND i.updated_at < NOW() - INTERVAL 5 MINUTE))
 GROUP BY i.scope, i.status;
```

---

## 5.8 用什么工具

| 需求 | 现成的 | 你还必须自己写的 |
| --- | --- | --- |
| 唯一约束 | MySQL `UNIQUE KEY` / PG 唯一索引 | 键 schema（含 scope、版本）与迁移 |
| 快速去重 | Redis `SET NX PX` / Lua 原子脚本 | 兜底：Redis 只是缓存，正确性靠 DB |
| 声明式幂等 | 自研 `@Idempotent` 注解 / Go middleware（第 6 章给完整实现） | 键表达式、冲突策略、响应重放 |
| 框架自带的「幂等」 | Seata AT 的 `undo_log`、RocketMQ 重试、Kafka 事务 | **它们都不做你的业务幂等**，只保证各自内部语义（详见第 11 章） |
| 并发压测 | k6、vegeta、`go test -race` | 固定键的脚本、判定 SQL、CI 门禁 |
| 故障注入 | Chaos Mesh、toxiproxy、`libfaketime` | 注入点清单与「观察不到就说明有 bug」的判据 |

> ⚠️ 再强调一次：Seata 帮你回滚，Temporal 帮你重放，**没有一个框架知道「什么算同一个业务意图」**。那一个判断，永远是你的。

---

## 5.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 定义 | 失效判据看**副作用**，不看接口返回 |
| 三不变量 | 键稳定、判定与生效原子、副作用可外部验证；破一个就有一类失效 |
| 六类漏点 | 键 / 窗口 / 边界 / 异常 / 重试链 / 时间与锁 |
| 最贵一条 | 压测要模拟「同一意图重复」，不是「N 个请求并发」；键必须固定 |
| 键设计 | `sha256(scope + client_token)`；禁止时间戳、trace_id、随机数、prompt hash |
| TTL | ≥ 最长重试窗口 × 2（渠道 72h → 7 天）；TTL 与重试策略同源配置 |
| 抢占 | 先 `INSERT` 抢唯一键再干活，永远不要 check-then-act |
| 冲突处理 | 只认 `uk(scope,key_hash)`；其它唯一键冲突必须抛，禁止空 catch |
| 事务边界 | 幂等表与业务表同库同事务；做不到就承认它只是去重缓存 |
| 重试 | 只允许一层重试；重试预算 ≤ 原流量 20%；header 只透传不改写 |
| 时间与锁 | 时钟回拨要拒绝发号；锁只是优化，正确性靠结果表 |
| Agent 差异 | 多两类失效：prompt hash 漂移、流式半失败；且重复一次 = 重复收费 |
| 不做会怎样 | 每次「偶发重复」都要 2 天查、3 天修数，T+1 对账才发现，账已经错了 |
| 一句话 | **幂等的敌人不是并发，是「同一个意图换了个身份又来了一次」。** |

## 5.10 自检题

1. **压测 200 并发、只出 1 单，为什么上线还是重复了 3,700 笔？说出至少两个必须改的测试设计。**
   <details><summary>答案要点</summary>
   ① 压测每个请求用了新生成的 `Idempotency-Key`，测的是 200 个不同意图的并发，不是「同一意图的重复」；必须固定键。② 没有模拟多层重试（网关 3 × 服务 3），也没验证「重试时键是否保持不变」。③ 没在链路中间注入故障（网关 502）去触发真实重试路径。④ 判定只看接口返回，没查业务表条数。
   </details>

2. **幂等表里出现 8% 的 `PROCESSING` 记录超过 10 分钟不结束，你怎么定位并修？**
   <details><summary>答案要点</summary>
   先分三类：① 幂等表与业务表不同事务（模式 6）→ 查是否两个 DataSource、`@Transactional` 是否自调用失效、是否在 `@Async` 线程里；② 进程被杀 → 看 `owner_node` 对应的 Pod 是否重启过；③ 业务本身卡在慢 RPC 上 → 看 span 耗时。修法：同库同事务 + `lease_until` 租约回收（15s 抢占）+ 扫描任务；防复发：评审强制画事务边界图，并把 `PROCESSING` 年龄做成告警。
   </details>

3. **为什么「先 `SELECT` 有没有记录，没有再 `INSERT`」在 200 并发下必然重复，而「直接 `INSERT` 靠唯一键」不会？**
   <details><summary>答案要点</summary>
   前者是两个独立语句、两个时刻：两个请求可以在同一时刻都读到「不存在」，然后各自插入，重复由应用逻辑承担。后者把「判定」交给数据库的唯一索引：InnoDB 在同一索引键上的插入会串行化，第二个必然拿到 1062。**幂等的判定权必须交给有原子性保证的一层（唯一索引/条件 UPDATE），不能放在应用代码的 if 里。**
   </details>

## 5.11 延伸阅读

- MySQL 官方手册：`InnoDB` 唯一索引与 `INSERT ... ON DUPLICATE KEY UPDATE` 的加锁行为（以你使用的 8.0.x 版本为准）
- Stripe API 文档 *Idempotent requests*：`Idempotency-Key` 的 24h 窗口、并发冲突返回 409、同键不同 payload 返回 422 的语义，是业界最清晰的一份契约
- Chaos Mesh 文档：`PodChaos` / `NetworkChaos` / `HTTPChaos` 的 YAML 字段
- k6 文档：`per-vu-iterations` / `shared-iterations` 两种 executor 的差别（本章脚本用的是前者）
- 上一章：[4. 订单场景全套：下单、支付回调、退款、库存](4-idempotency-order-scenarios.md)
- 下一章：[6. 幂等的可观测与工具化](6-idempotency-observability.md)
- 交叉引用：定义与 20 条判定问答见第 1 章；幂等表 DDL 的金融版见第 3 章；`@Transactional` 十坑见第 7 章；对账兜底见第 14 章；Agent 工具幂等见 [../llm/22-tool-system-mcp.md](../llm/22-tool-system-mcp.md)

**交叉引用**：总纲 [0-summary.md](0-summary.md) ｜ 语言与运行时 [../go/0-summary.md](../go/0-summary.md) ｜ 平台治理 [../llm/0-summary.md](../llm/0-summary.md)
