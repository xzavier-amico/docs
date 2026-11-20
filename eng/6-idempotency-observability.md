# 6. 幂等的可观测与工具化

> **状态**：`已完成（初稿）` ｜ **所属**：第一部分 · 幂等：你到底「确保」了什么 ｜ **编号**：6 / 52
> **本章定位**：幂等做没做对，不能靠「我加了个唯一索引」来证明——只能靠四个指标、一条 trace 和一张水位表来证明。本章把「重复从哪来、拦住了多少、漏了多少、代价多少」变成可查的数字，再把幂等能力沉淀成 SDK，让它从「每个接口各写一遍」变成「一次接入、处处生效」。
> **读法**：§6.4.1 的指标口径表可以直接变成埋点需求；§6.4.4 的分层对比决定你的幂等该写在哪；§6.7 的告警规则可直接 `promtool check rules` 后上线。

---

## 6.1 引子：两个「监控全绿」的幂等事故

### 事故一：网关升级漏了一个 header，80 分钟后才由客诉发现（2026-02-09）

某 SaaS 的支付回调接口靠 `Idempotency-Key` 去重。网关 2.7.0 升级时，「header 透传白名单」被重写，`Idempotency-Key` 不在新白名单里，网关给每个请求重新生成了 `X-Request-Id`。

| 时间 | 事件 | 数据 |
| --- | --- | --- |
| 09:12 | 网关 2.7.0 灰度 10% | — |
| 09:20 | 幂等命中率从 **12.4%** 掉到 **0.2%**（无人看此指标） | 命中 41/s → 0.6/s |
| 09:31 | 全量发布 | — |
| 10:05 | 支付回调重复入账达到 **4,200 笔** | 重复金额 **21.6 万元** |
| 10:40 | 客服收到「同一笔订单扣了两次」投诉 17 起 | — |
| 11:05 | 回滚网关，命中率 3 分钟内回到 11.8% | 已污染数据不可回滚 |
| T+7 | 日终对账确认最终差异 3,912 笔，人工冲正 3 天 | 退款 + 补偿 24.3 万元 |

期间：**HTTP 错误率 0.02%、P99 180ms、CPU 38%，全部正常**。唯一能提前 **80 分钟**发现事故的信号，是那条没人看的命中率曲线——它在 09:20 就跌到了 0.2%，只是没有配告警。

### 事故二：一次 `DELETE` 把幂等表锁了 4 小时（2025-12-18）

幂等表积到 9.4 亿行，DBA 按惯例跑清理：`DELETE FROM idempotency_record WHERE created_at < '2025-09-01' LIMIT 5000000`。单事务 500 万行，执行 4 小时 8 分钟仍未结束（`EXPLAIN` 只能看扫描行数与执行计划，估不出实际耗时），期间二级索引与主键间隙被大量锁定。

| 时间 | 事件 | 数据 |
| --- | --- | --- |
| 11:02 | 清理任务启动 | 目标 5,000,000 行 |
| 11:10 | 行锁等待开始堆积，`data_lock_waits` 每秒 3,100 次 | 应用连接池 68% |
| 11:20 | 下单接口 409 比例从 0.3% 涨到 **12.1%** | 用户看到「请求正在处理中」 |
| 11:35 | 值班按「容量不足」扩容 6 个 Pod，无效 | 连接池 100%，QPS 反降 22% |
| 15:10 | DBA `KILL` 清理会话，锁释放 | 回滚耗时 4 分 12 秒 |
| 15:22 | 409 回落到 0.4%，积压的 `PROCESSING` 记录靠租约自然恢复 | 影响窗口 11:10–15:22，共 **4h12m** |

后果：**18.7 万次请求被 409/超时**，估算 GMV 影响 **340 万元**，另有 6,400 条 `PROCESSING` 记录卡住 15 分钟后才被接管。根因有两层：清理方式错（该 `DROP PARTITION`，见第 5 章 §5.4.6），**以及没有任何一个指标告诉你「卡住的是幂等表」**——你只有 `http_409_total`，没有 `idempotency_record_stuck`。

> 🩸 两次事故的共同点：系统「活着」，但幂等已经失效或半失效。**可观测性在这里不是「看服务死没死」，而是「看保护机制还在不在」。**

---

## 6.2 到底是什么：幂等可观测 = 每次判定留下四元组

**定义**：幂等可观测性，是让每一次幂等判定都留下可聚合、可追溯的四元组——**谁（scope + key 指纹）、判了什么（hit/miss/conflict/error）、花了多少代价（RT、存储、重放字节）、结果是否被重放**。缺任何一个，你都只能靠猜。

先钉死五个误解：

| 你以为的「观测」 | 为什么不算 | 不做会怎样 |
| --- | --- | --- |
| 日志里 grep `Duplicate entry` | 日志是抽样/采样/被限流的，且无事前信号 | 事故当天日志被 ELK 限流丢弃，事后 0 证据 |
| 看接口错误率 | 幂等失效恰恰不产生错误：返回 200，业务做了两次 | 事故期间错误率 0.02%，看起来最健康 |
| 幂等命中率越高越好 | 命中率高可能是「上游重试风暴」，是故障信号 | 把重试风暴当成绩，掩盖了真实问题 |
| 一条 trace 看全链路就够 | trace 是采样的，重复请求的 trace 可能一条都没采到 | 你要证明「只生效一次」，却拿不出证据 |
| 幂等是业务代码的事，平台不用管 | 每个团队各写一遍，口径不同、没人聚合 | 出问题时连「全站重复率」都算不出来 |

**四个指标 + 一条 trace，分别回答四个问题**：

| 问题 | 指标 | 一句话口径 |
| --- | --- | --- |
| 拦住了多少？ | 幂等命中率 | `hit / (hit + miss)`，按 scope 拆 |
| 重复从哪来？ | 重复请求率 | 每个业务意图平均到达次数 − 1，按来源层拆 |
| 保护机制还活着吗？ | 去重失败率 | `store_error` 占判定总数；`conflict` 单列（保护生效，不是失败） |
| 会不会涨爆？ | 幂等表水位与日增 | 行数、字节、`PROCESSING` 年龄、最老记录年龄 |

---

## 6.3 结构图：埋点该长在哪

```
① 一次请求的判定路径与埋点位置（★ 是必须打点的地方）

  客户端 ──► 网关 ──► 服务入口 ──► 键计算 ──► 判定 ──► 业务执行 ──► 提交
              │          │            │         │          │
              │          │            │         ├─ hit ────┼─► 重放响应
              │          │            │         │   ★ idempotent_request_total{result="hit"}
              │          │            │         │   ★ span: idempotency.result=hit
              │          │            │         ├─ conflict（他人在飞）
              │          │            │         │   ★ {result="conflict"} → 409/425
              │          │            │         └─ miss ──► 占位成功，执行业务
              │          │            │             ★ {result="miss"}
              │          │            └─ ★ key_hash 只进 trace/log，绝不进 metric label
              │          └─ ★ http_inflight / 409 比例（来源层归因）
              └─ ★ 网关侧 retry_total{route}（判断是否重试风暴）

② 指标分层：一眼看出该看哪一层

   L0 业务结果   orders_created_total{status}         ← 用户关心的
   L1 幂等效果   idempotent_request_total{result}     ← 本章主角
   L2 存储健康   idempotency_record_rows / stuck      ← 保护机制自己
   L3 资源       mysql_innodb_row_lock_waits / cpu    ← 根因层

   排查顺序：L0 异常 → L1 定位是否幂等相关 → L2 看机制 → L3 找资源
   反了就会像事故二：从 L3 开始扩容，扩了 4 小时没用

③ 数据流：指标、日志、trace 各存一份，靠 key 指纹串起来

   metric  : idempotent_request_total{scope="pay:callback",result="hit"}  低基数，永久
   log     : {"scope":"pay:callback","kh":"9f2c1a7b","result":"hit","age_ms":84000}
   trace   : span(idempotency.check) attrs{result=hit, age_ms, replayed=true}
              └─ 三者用 kh（key_hash 前 8 位）+ trace_id 关联；raw key 不落任何存储
```

> ⚠️ 注意图里的 `key_hash` 处理：幂等键常含手机号、订单号、租户 ID，**原始键既不进 label 也不进日志**。统一 `sha256(scope|key)` 取前 8~16 位，既能关联又不泄露。

---

## 6.4 怎么实现：四个指标、一条 trace、一层落点、一个 SDK

### 6.4.1 四个指标的口径（口径错，等于没有）

| 指标 | 类型 | 标签 | 分子 / 分母 | 常见口径错误 |
| --- | --- | --- | --- | --- |
| `idempotent_request_total` | Counter | `scope`, `result`(hit/miss/conflict/error), `store` | — | 把「重放」和「命中」算成两件事，导致 hit 翻倍 |
| `idempotency_hit_ratio` | 派生（PromQL） | `scope` | `rate(result="hit")` / `rate(result=~"hit\|miss")` | 分母里混入 `conflict`，把在飞请求算成「未命中」 |
| `idempotency_store_error_total` | Counter | `scope`, `store`, `reason` | — | 只记 MySQL 错误，Redis 超时被静默降级 |
| `idempotency_record_age_seconds` | Histogram | `scope`, `status` | — | 只记「最老记录」，看不出 P99 卡住的人 |
| `idempotency_table_rows` | Gauge | `table`, `partition` | — | 用 `COUNT(*)` 每 15s 采一次，把库打挂 |
| `idempotent_replay_bytes_total` | Counter | `scope` | — | 不采 → 不知道重放把带宽吃掉了多少 |

```promql
# 全局与分 scope 的幂等命中率（命中 = 同一意图的重复被吸收）
sum by (scope) (rate(idempotent_request_total{result="hit"}[5m]))
  /
sum by (scope) (rate(idempotent_request_total{result=~"hit|miss"}[5m]))

# 去重失败率：只算存储错误（>0.1% 就是保护机制在漏）
sum(rate(idempotent_request_total{result="error"}[5m]))
  / sum(rate(idempotent_request_total[5m]))

# 在飞重复率：conflict 是「他人在飞」，保护机制正常，单独看
sum(rate(idempotent_request_total{result="conflict"}[5m]))
  / sum(rate(idempotent_request_total[5m]))

# 重试放大系数：入口 QPS ÷ 唯一业务意图数（miss 数近似唯一意图数）
sum(rate(http_requests_total{route="/api/pay/callback"}[5m]))
  / sum(rate(idempotent_request_total{scope="pay:callback",result="miss"}[5m]))

# 幂等表水位（用采集器写入的 Gauge，不要用 COUNT(*)）
max by (partition) (idempotency_table_rows{table="idempotency_record"})

# PROCESSING 年龄 P99（Histogram 的经典用法：桶计数聚合后再算分位）
histogram_quantile(0.99,
  sum by (le) (rate(idempotency_record_age_seconds_bucket{status="PROCESSING"}[5m])))
```

**为什么命中率不是越高越好**：命中率 35% 有两种完全相反的解释——① 上游有稳定的重试机制（健康，例如支付渠道固定重发 3 次）；② 上游陷入重试风暴（故障，第 15 章 §15.7 的放大链）。区分方法只有一个：看**重试放大系数**和 `http_requests_total` 的绝对值。命中率涨 + 入口 QPS 涨 = 风暴；命中率涨 + 入口 QPS 平 = 正常重试。

**重复率突增意味着什么**（值班最常被问的一句话，直接查表）：

| 现象组合 | 最可能原因 | 第一个动作 |
| --- | --- | --- |
| 重复率↑ + 入口 QPS↑ + 命中率↑ | 上游重试风暴（客户端/网关无退避） | 查 `retry_total`，下发 `Retry-After`，压重试预算 |
| 重复率↑ + 入口 QPS 平 + 命中率↓ | **键漂移**（header 被改写、版本升级） | diff 两条请求的 `key_hash`，查网关白名单与发布记录 |
| 重复率↑ 只集中在单个 scope | 该接口的上游在重发（渠道补发、MQ 重投） | 查该渠道重试策略与消费位点（第 24 章） |
| 重复率↑ + P99↑ | 超时值小于真实 P99，正常请求被误判失败后重试 | 对齐超时与 P99，必要时提高超时而不是加重试 |
| 重复率↓ + 业务量平 + 错误率↑ | 上游把重试关了（失败被静默吞掉） | 别庆祝，先看错误率与对账差异 |
| 重复率突增后归零 + 429↑ | 限流把重试挡在门外（治标没治本） | 修根因，否则下一次限流阈值调整就复发 |

> 💡 记住这条因果关系：**重复率是「上游行为」的指标，命中率是「我方防护」的指标，去重失败率才是「我方正确性」的指标。** 三个一起看，才能分清「有人在重试」「我们挡住了」「我们没挡住」。

### 6.4.2 trace 里怎么表示「这次请求被幂等拦下了」

指标告诉你「有多少」，trace 告诉你「是哪一次、为什么」。**别把幂等做成一个看不见的黑盒**——被拦下的请求也要有完整 span，否则你无法向审计证明「这个订单只执行了一次」。

Span 结构（父子关系就是判定顺序）：

```
POST /api/pay/callback                      [server]  dur=12ms
├── idempotency.check                       [internal] dur=1.8ms
│     attrs: idempotency.scope      = "pay:callback"
│            idempotency.key_hash   = "9f2c1a7b"        ← 只存前缀
│            idempotency.store      = "mysql"
│            idempotency.result     = "hit"
│            idempotency.record_age = 84000ms
│            idempotency.replayed   = true
│     event: duplicate_suppressed{owner_node="order-7d9f", first_trace_id="4b1e…"}
└── (没有业务子 span：被拦住时不执行业务)
```

Go 侧（OpenTelemetry，`go.opentelemetry.io/otel`）：

```go
func Check(ctx context.Context, s Store, scope, keyHash string) (context.Context, Result, error) {
	ctx, span := tracer.Start(ctx, "idempotency.check",
		trace.WithSpanKind(trace.SpanKindInternal),
		trace.WithAttributes(
			attribute.String("idempotency.scope", scope),
			attribute.String("idempotency.key_hash", keyHash[:8]),
			attribute.String("idempotency.store", s.Name()),
		))
	defer span.End()

	res, rec, err := s.TryBegin(ctx, scope, keyHash)
	if err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, "store error")
		return ctx, res, err
	}
	span.SetAttributes(
		attribute.String("idempotency.result", res.String()), // hit / miss / conflict
		attribute.Bool("idempotency.replayed", res == Hit),
	)
	if res == Hit {
		span.SetAttributes(attribute.Int64("idempotency.record_age_ms",
			time.Since(rec.CreatedAt).Milliseconds()))
		span.AddEvent("duplicate_suppressed", trace.WithAttributes(
			attribute.String("owner_node", rec.OwnerNode)))
		span.SetStatus(codes.Ok, "replayed")
	}
	return ctx, res, nil
}
```

Java 侧（`io.opentelemetry.api`，注解版 SDK 内部同样要打点）：

```java
Span span = tracer.spanBuilder("idempotency.check")
        .setSpanKind(SpanKind.INTERNAL)
        .setAttribute("idempotency.scope", scope)
        .setAttribute("idempotency.key_hash", keyHash.substring(0, 8))
        .setAttribute("idempotency.store", store.name())
        .startSpan();
try (Scope ignored = span.makeCurrent()) {
    Outcome out = store.tryBegin(scope, keyHash);
    span.setAttribute("idempotency.result", out.result().name());
    if (out.result() == Result.HIT) {
        span.setAttribute("idempotency.replayed", true);
        span.setAttribute("idempotency.record_age_ms", out.ageMillis());
        span.addEvent("duplicate_suppressed",
                Attributes.of(AttributeKey.stringKey("owner_node"), out.ownerNode()));
    }
    return out;
} catch (RuntimeException e) {
    span.recordException(e);
    span.setStatus(StatusCode.ERROR, "idempotency store failure");
    throw e;
} finally {
    span.end();
}
```

**两个容易被忽略的工程细节**：

1. **采样策略**。默认 1% 头采样会把 99% 的「命中」丢掉，而命中恰恰是你最想看的少数派。用 tail sampling：`result=hit` 或 `result=error` 的 trace **100% 保留**，`miss` 按 1% 保留。Collector 配置里按 `idempotency.result` 属性做策略（`tail_sampling` processor，以你使用的 Collector 版本为准）。
2. **attribute 基数**。`idempotency.key_hash` 是天然高基数，**只能进 span/log，绝对不能进 metric label**。Prometheus 侧一个 `key_hash` 标签就能把 series 从 40 涨到 400 万（§6.6 会给具体数字）。

### 6.4.3 幂等该放哪一层：网关 / 服务 / SDK / DB

| 落点 | 粒度 | 能拿到什么 | 拿不到什么 | 典型失效 | 适合 |
| --- | --- | --- | --- | --- | --- |
| **网关** | IP / 租户 / 路由 + header | 全站统一、跨语言、挡重试风暴最早 | 业务语义（什么叫「同一意图」）、响应重放、事务边界 | 键被改写（事故一）；无法区分「重试」与「用户真的点了两次」 | 防刷、防重放攻击、粗粒度去重 |
| **服务（接口）** | 接口 + 业务键 | 业务语义、响应重放、与业务同事务 | 跨服务（下游服务看不到）、多语言重复实现 | 每个服务写一遍，口径不一致 | 单服务内写接口，绝大多数场景 |
| **SDK / 中间件** | 方法 + SpEL/表达式 | 声明式、事务整合、统一埋点 | 跨进程、跨语言需各自实现 | 注解不生效（自调用/private，见第 7 章） | 同栈多服务，团队 ≥3 个服务 |
| **DB 唯一索引** | 行 | 绝对权威、并发安全、不受应用 bug 影响 | 无重放、无 TTL 语义、错误码要翻译 | 键漂移时完全不冲突（第 5 章模式 1） | **永远要有**，作为最后一道 |

**判断标准（按顺序问，命中即停）**：

```
Q1 这个副作用是资金/资产/权益吗？
   └─ 是 → DB 唯一索引必须有；再按 Q2 选上层
Q2 幂等键只有业务代码知道（如订单号 + 业务类型）？
   └─ 是 → 服务层 或 SDK
Q3 需要重放「与首次完全一致的响应体」吗（客户端要拿结果）？
   └─ 是 → 服务层 / SDK（网关只能缓存原始 body，无法保证业务语义）
Q4 多语言栈（Java + Go + Node）且团队不想写四遍？
   └─ 是 → 网关做粗粒度 + 各语言 SDK 做细粒度，两层都留
Q5 只是想挡恶意重放 / 刷单？
   └─ 是 → 网关（带签名与时间窗的 nonce 去重）
```

> 💡 **结论：不是「选一层」，是「三层各司其职」**：网关挡流量级重复（便宜、早），SDK/服务保证业务级幂等（准），DB 唯一索引兜底（绝对）。**唯一不能省的是 DB 那一层**——前两层都可能因为一次配置变更而失效，事故一就是活证据。

### 6.4.4 通用幂等 SDK：接口该怎么设计

一个能用的幂等 SDK 只需要 5 个语义，多一个都是负担：

| 语义 | 说明 | 不做会怎样 |
| --- | --- | --- |
| `Begin(scope, key) → (owner, record)` | 原子抢占；非 owner 时返回已有记录 | 每个业务自己 `if exists`，窗口重现 |
| `Commit(key, response)` | 标记完成 + 存响应（供重放） | 重试拿不到首次结果，只能返回「已处理」空壳 |
| `Abort(key)` | 只释放「可判定失败」的那次占位 | 网络超时也 Abort → 把正在跑的别人的占位删了 |
| `Replay(record) → response` | 原样返回首次响应（含状态码） | 客户端看到「已处理」但没有订单号，体验断裂 |
| `InProgress → 409/425 + Retry-After` | 明确的「请稍后重试」语义 | 返 500 → 上游判定失败 → 触发更多重试 |

Java：注解 + 切面（Spring AOP，注意代理边界）：

```java
@Target(ElementType.METHOD)
@Retention(RetentionPolicy.RUNTIME)
public @interface Idempotent {
    String scope();                                  // 必填：业务域，如 "order:create"
    String key();                                    // SpEL，如 "#cmd.requestId"
    long ttlSeconds() default 604800;                // 必须 ≥ 重试窗口 × 2（默认 7 天）
    Conflict onConflict() default Conflict.REPLAY;   // REPLAY / REJECT / WAIT
    boolean replayResponse() default true;
}

@Aspect
@Component
public class IdempotentAspect {
    @Around("@annotation(idem)")
    public Object around(ProceedingJoinPoint pjp, Idempotent idem) throws Throwable {
        String key = SpelUtil.eval(idem.key(), pjp);          // 键只由「稳定字段」拼
        String keyHash = sha256(idem.scope() + "|" + key);

        try {
            // 占位 + 业务 + markDone 在同一个事务里（实现见 §6.4.5）：
            // 业务方法若自带 @Transactional(REQUIRED)，会加入这个事务，而不是另开一个
            Object ret = executor.inTx(idem.scope(), keyHash, idem.ttlSeconds(), () -> {
                try {
                    return pjp.proceed();
                } catch (RuntimeException | Error e) {
                    throw e;
                } catch (Throwable e) {
                    throw new BizCheckedException(e);        // 受检异常包成运行时，确保回滚
                }
            });
            metrics.counter("idempotent_request_total",
                    "scope", idem.scope(), "result", "miss").increment();
            return ret;
        } catch (DuplicateKeyException e) {                  // 占位撞唯一键 = 重复请求
            Record rec = executor.get(idem.scope(), keyHash);
            if (rec == null) throw e;                        // 别的唯一键冲突 → 真错误
            if ("DONE".equals(rec.getStatus())) {
                metrics.counter("idempotent_request_total",
                        "scope", idem.scope(), "result", "hit").increment();
                return idem.replayResponse() ? Json.read(rec.getResponseBody(),
                        pjp.getSignature().getDeclaringType()) : null;
            }
            metrics.counter("idempotent_request_total",
                    "scope", idem.scope(), "result", "conflict").increment();
            throw new InProgressException("duplicate in flight, retry after 1s");
        }
    }
}
```

> ⚠️ `onConflict = WAIT` 别真的阻塞线程等结果：最多自旋 200~500ms，超时就返 425。占着 Tomcat 线程等一个可能 10 秒才完成的事务，是第 15 章「无限队列」的同一个错误。

Go：HTTP middleware（捕获响应体才能重放，这点很多人漏掉）：

```go
type Config struct {
	Scope   string
	KeyFn   func(*http.Request) string // 必须是稳定字段，禁止用 time.Now()/traceID
	TTL     time.Duration
	Store   Store
	Metrics *Metrics
}

func Middleware(cfg Config) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			keyHash := sha256hex(cfg.Scope + "|" + cfg.KeyFn(r))
			rec, owner, err := cfg.Store.TryBegin(r.Context(), cfg.Scope, keyHash, cfg.TTL)
			if err != nil {
				cfg.Metrics.Inc(cfg.Scope, "error")
				http.Error(w, "idempotency store unavailable", http.StatusServiceUnavailable)
				return // ★ 存储故障要「明确失败」，不能降级放行（否则就是重复）
			}
			if !owner {
				if rec.Status == StatusDone && len(rec.Response) > 0 {
					cfg.Metrics.Inc(cfg.Scope, "hit")
					w.Header().Set("Idempotent-Replayed", "true")
					w.WriteHeader(rec.HTTPStatus)
					_, _ = w.Write(rec.Response)
					return
				}
				cfg.Metrics.Inc(cfg.Scope, "conflict")
				w.Header().Set("Retry-After", "1")
				w.WriteHeader(http.StatusTooEarly) // 425：可重试，不污染 5xx
				return
			}

			buf := &responseBuffer{header: http.Header{}, status: 200}
			next.ServeHTTP(buf, r)                 // 业务写进 buffer，先不发
			cfg.Metrics.Inc(cfg.Scope, "miss")

			if buf.status < 500 {                  // 5xx 不缓存（可能是暂时故障）
				_ = cfg.Store.Commit(r.Context(), cfg.Scope, keyHash,
					buf.status, buf.header, buf.body.Bytes())
			} else {
				_ = cfg.Store.Abort(r.Context(), cfg.Scope, keyHash)
			}
			buf.flush(w)                           // 最后一次性写给客户端
		})
	}
}
```

> ⚠️ **两个设计红线**：① 存储不可用时**必须明确失败**（503），不能「降级为放行」——否则幂等在最需要它的时候自动关闭；② `5xx` 不要 Commit，否则一次瞬时故障会被固化成「永久成功」。

### 6.4.5 与事务的接缝：SDK 最容易骗人的地方

SDK 再漂亮，也绕不开第 7 章和模式 6 的约束：**占位、业务写、标 DONE 必须在同一个事务里**。SDK 的正确做法是「接受一个事务回调」，而不是「自己开事务、再调业务」：

```java
public <T> T inTransaction(TransactionTemplate tx, String scope, String keyHash, Supplier<T> biz) {
    return tx.execute(status -> {                       // 一个连接、一个事务
        idemMapper.insertProcessing(scope, keyHash, 15, ttlSeconds);  // 占位
        T out = biz.get();                               // 业务写
        if (idemMapper.markDone(scope, keyHash, toJson(out)) != 1) {
            throw new IllegalStateException("lease lost"); // 条件更新失败 = 别人接管了
        }
        return out;
    });
}
```

跨库时 SDK 必须显式暴露「我不保证原子性」——此时它退化为**去重缓存**，正确性由「业务唯一索引 + 对账」承担（第 14 章）。**SDK 文档里不写这一句，就是在骗使用者。**

### 6.4.6 组件盘点：谁能给你什么，谁不能

| 组件 | 它提供的 | 它不提供的 | 结论 |
| --- | --- | --- | --- |
| **MySQL 唯一索引** | 并发安全的「只允许一个」；1062 错误码 | 重放、TTL、状态机 | 必须有，但不是全部 |
| **Redis `SET NX PX` / Lua** | 低延迟去重、原子占位 | 持久性（异步复制会丢）、跨重启一致 | 只做前置缓存 |
| **Seata AT** | 全局事务回滚（`undo_log`）、全局锁 | **业务幂等**：它回滚的是数据，不管你的重复请求 | 别指望它挡重复 |
| **Sentinel** | 热点参数限流（`ParamFlowRule`）、熔断 | 幂等：限流是「少放几个」，不是「只放一个」 | 互补，不替代 |
| **APISIX / Envoy / Nginx** | 限流、路由、header 透传/改写 | 幂等语义插件（网关无法理解业务键） | 可做粗粒度防重放 |
| **Temporal** | `WorkflowID` 唯一性 + 事件重放（`WorkflowIDReusePolicy`） | 外部副作用的幂等（工具调用仍需自己保证） | 长流程场景最好的底座 |
| **Stripe 语义（参考实现）** | `Idempotency-Key` 24h、并发 409、同键不同 payload → 4xx（400 系） | 你的业务键设计 | **业界最值得抄的契约** |
| **自研 SDK** | 键规范、注解/中间件、埋点、冲突策略 | — | 团队 ≥3 个写服务时值得做 |

以各项目当前版本文档为准；上表的判断依据是「它管不管你的业务意图」。

---

## 6.5 双修对照：传统 vs Agent

| 维度 | 传统系统 | Agent 系统 | 同 / 异 |
| --- | --- | --- | --- |
| **指标集** | 命中率、重复率、去重失败率、表水位 | 上述全部 + **重复计费额**、**重复 token 数**、**被拦 step 的成本** | 多一维成本口径 |
| **唯一键 / 身份** | `request_id` / 订单号 | `run_id` / `step_id` / `tool_call_id`，含模型与提示词版本 | 结构同构，键更复杂 |
| **trace 语义** | 一次请求一个 span | 一次 Run = 一棵 span 树，幂等判定发生在**每个 step、每次工具调用** | 同机制，**判定点数量 ×10** |
| **「命中」的代价** | 省下一次写库 | 命中时**模型调用可能已经发生**——钱已经花了，只是没重复花 | **异（关键差异）** |
| **落点选择** | 网关 + 服务 + DB | 网关（租户级）+ Run 编排器（step 级）+ 工具适配层（tool_call 级） | 层数更多，因为粒度更细 |
| **告警含义** | 命中率突降 = 键漂移 | 命中率突降 = 键漂移 **或** 模型版本升级导致键变化 | 多一个误报源 |
| **验证方法** | 200 并发 + 对账 | 同 + **重放同一条 trace** + token 用量对账 | 方法同构，多一层 |

**哪些完全相同、可以直接复用**：四个指标的口径与 PromQL、span attribute 结构、tail sampling 策略、告警规则阈值、SDK 的五个语义、分层落点判断树。**换的只是键的组成和「一次判定」的粒度。**

**差异根因（对齐总纲 §1.5 四条）**：

1. **成本显性** → 传统系统里「命中」纯赚；Agent 里「命中」常常是「花了钱但没重复花」。所以 Agent 的命中率必须与 `llm_tokens_total` 对齐看：**命中率上升但 token 也在涨 = 上游在重试，钱白烧**。
2. **非确定性** → 模型版本升级会让「同一意图」的键变化，命中率会阶跃式下降。所以**发布流程里必须有一条**：模型/提示词版本变更时，观测 30 分钟内命中率是否异常下跌（§6.7 的告警规则就是为这个准备的）。
3. **长事务** → 一次 Run 的 `PROCESSING` 可能持续 90 秒，租约不能沿用 15 秒；指标上要单独看 `status="PROCESSING"` 的年龄分布，否则会把正常的在飞 Run 当卡单。
4. **不可回滚** → 工具副作用（发邮件、下单）不能靠回滚，只能补偿；trace 里必须能查到「第几次工具调用被拦下」，这是事后审计的唯一依据。

---

## 6.6 会在哪炸：可观测性自身的失效模式

| 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| 命中率恒为 100% | 分母只算了 hit；或把 `conflict` 也算进 hit | 看 PromQL 分母表达式 | 分母固定为 `hit + miss` | 口径写进指标字典，PromQL 进代码库评审 |
| 命中率恒为 0 | 键每次都变（模式 1/7）；或埋点写在业务成功后 | 抽样看两次请求的 `key_hash` 是否相同 | 修键；埋点前移到判定处 | 单测断言「重放 10 次 key_hash 相同」 |
| Prometheus OOM | `key_hash`/`order_id`/`user_id` 进了 label | `topk(20, count by (__name__)({__name__=~".+"}))`，看 series 数 | 删标签，改到 trace/log | label 白名单 + CI 检查（§6.7 给脚本） |
| 告警天天响没人管 | 阈值按「绝对值」拍，不看基线 | 看告警历史与业务波峰是否重合 | 用「相对前 7 天同小时」的比率 | 告警必须有 runbook 与负责人 |
| 事后拿不出「只执行一次」的证据 | trace 采样丢了命中 span；业务表无审计列 | 抽查一条投诉单，查不到首个 trace | tail sampling 保 `result=hit`；补审计表 | 审计需求进上线清单 |
| 409/425 被当成错误 | SDK 用 500 表达「在飞」 | 看状态码分布，5xx 里混着「重复请求」 | 改 425 + `Retry-After` | SLO 规则：429/425 不算错误预算消耗 |
| 幂等表涨到 9 亿行才发现 | 没有水位指标；清理用 DELETE | `information_schema.tables` 看 data_length | 改分区 + 水位告警 | 建表时就要有分区与保留期（§6.7 的 DDL 检查） |
| SDK 接入后 TPS 掉 40% | 每次判定多一次同步 Redis + 一次 MySQL 写 | 看 span 的 `idempotency.check` 耗时占比 | 本地布隆前置 + 批量；或只对高危接口开 | 接入前压测，判定 P99 必须 < 5ms |

> 🩸 **最隐蔽的一条是「命中率恒为 100%」**：某团队把 `result="hit"` 当默认标签值，异常分支忘赋值，看板上命中率 100% 连续三个月。直到一次重复扣款事故，才发现真实命中率只有 6%。**默认值是最贵的 bug。**

---

## 6.7 怎么验证：规则测试、基数审计、故障演练

### 6.7.1 告警规则（可直接上线，`promtool check rules` 通过）

```yaml
groups:
  - name: idempotency
    interval: 30s
    rules:
      - alert: IdempotencyHitRatioDrop
        # 绝对命中率 <5% 持续 5 分钟；低流量 scope 可在此基础上再加相对基线
        expr: |
          (
            sum by (scope) (rate(idempotent_request_total{result="hit"}[10m]))
            / sum by (scope) (rate(idempotent_request_total{result=~"hit|miss"}[10m]))
          ) < 0.05
        for: 5m
        labels: { severity: P2, team: platform }
        annotations:
          summary: "幂等命中率异常下跌 scope={{ $labels.scope }}"
          runbook: "https://wiki/runbook/idempotency#hit-ratio-drop"
          hint: "先查网关 header 白名单与模型版本变更，再查 key 计算是否含可变字段"

      - alert: IdempotencyStoreError
        expr: |
          sum by (scope, store) (rate(idempotency_store_error_total[5m]))
          / sum by (scope, store) (rate(idempotent_request_total[5m])) > 0.001
        for: 2m
        labels: { severity: P1 }
        annotations:
          summary: "去重存储错误率 > 0.1%（scope={{ $labels.scope }} store={{ $labels.store }}）"
          hint: "存储故障时 SDK 会返回 503；先确认是否在降级放行（绝不允许）"

      - alert: IdempotencyRecordStuck
        expr: |
          histogram_quantile(0.99,
            sum by (le) (rate(idempotency_record_age_seconds_bucket{status="PROCESSING"}[5m]))) > 300
        for: 10m
        labels: { severity: P2 }
        annotations:
          summary: "PROCESSING P99 年龄 > 5 分钟，疑似租约/事务卡死"
          hint: "查 innodb_trx 长事务与 owner_node 是否重启"

      - alert: IdempotencyTableGrowth
        expr: |
          predict_linear(idempotency_table_rows{table="idempotency_record"}[6h], 7*86400) > 3e9
        for: 30m
        labels: { severity: P3 }
        annotations:
          summary: "幂等表 7 天内将超过 30 亿行，检查分区与 TTL"
```

### 6.7.2 规则单测（`promtool test rules`）

```yaml
rule_files: [idempotency.yml]
evaluation_interval: 1m
tests:
  - interval: 1m
    input_series:
      - series: 'idempotent_request_total{scope="pay:callback",result="hit"}'
        values: '0+60x20'
      - series: 'idempotent_request_total{scope="pay:callback",result="miss"}'
        values: '0+540x20'
    alert_rule_test:
      - eval_time: 15m
        alertname: IdempotencyHitRatioDrop
        exp_alerts: []          # 10% 命中率属于基线内（支付渠道固定重发），不该告警
  - interval: 1m
    input_series:
      - series: 'idempotent_request_total{scope="pay:callback",result="hit"}'
        values: '0+1x20'        # 命中率跌破 5%
      - series: 'idempotent_request_total{scope="pay:callback",result="miss"}'
        values: '0+99x20'
    alert_rule_test:
      - eval_time: 15m
        alertname: IdempotencyHitRatioDrop
        exp_alerts:
          - exp_labels: { severity: P2, team: platform, scope: "pay:callback" }
```

```bash
promtool check rules rules/idempotency.yml
promtool test rules rules/idempotency_test.yml
```

### 6.7.3 基数审计与一键体检脚本

```bash
#!/usr/bin/env bash
# idem-audit.sh：上线前/每周跑一次；任一非 0 退出码 = 阻断发布
set -euo pipefail
PROM=${PROM:-http://prometheus:9090}

q() { curl -sG "$PROM/api/v1/query" --data-urlencode "query=$1" \
      | python3 -c 'import sys,json;d=json.load(sys.stdin)["data"]["result"];print(int(float(d[0]["value"][1])) if d else 0)'; }

series=$(q 'sum(count by (__name__)({__name__=~"idempotent_.*"}))')
echo "idempotent_* series 总数: $series  (阈值 5000)"
[ "$series" -lt 5000 ] || { echo "FAIL: series 过多，检查 label 是否含 key/order/user"; exit 1; }

hit=$(q 'sum(rate(idempotent_request_total{result="hit"}[5m]))')
miss=$(q 'sum(rate(idempotent_request_total{result="miss"}[5m]))')
echo "hit=$hit miss=$miss"
if ! awk -v h="$hit" -v m="$miss" 'BEGIN { exit !(h + m > 1e-9) }'; then
  echo "FAIL: 5 分钟内没有任何幂等判定，埋点可能没生效"; exit 1
fi

stuck=$(q 'sum(idempotency_record_stuck{status="PROCESSING"})')
echo "PROCESSING 卡住: $stuck (阈值 100)"
[ "$stuck" -lt 100 ] || { echo "FAIL: 卡单过多"; exit 1; }
echo "OK"
```

### 6.7.4 水位采集：别用 `COUNT(*)` 打生产库

```sql
-- 采集器每 60s 跑一次（走只读从库），结果写成 Gauge：
--   idempotency_table_rows{table="idempotency_record",partition="p202602"}
--   idempotency_record_stuck{status="PROCESSING"}
SELECT table_name,
       table_rows                                   AS est_rows,   -- InnoDB 估算，误差 30%~50% 正常
       ROUND(data_length  / 1024 / 1024)            AS data_mb,
       ROUND(index_length / 1024 / 1024)            AS index_mb
  FROM information_schema.tables
 WHERE table_schema = 'order_db' AND table_name = 'idempotency_record';

-- 卡单与最近记录年龄：只扫最近 24 小时（需 idx_status_created(status, created_at)）
SELECT status,
       COUNT(*)                                        AS cnt,
       TIMESTAMPDIFF(SECOND, MIN(updated_at), NOW(3))  AS oldest_age_sec
  FROM idempotency_record
 WHERE created_at >= NOW() - INTERVAL 1 DAY
 GROUP BY status;

-- 精确行数只每小时算一次，或直接用分区元数据（推荐）
SELECT partition_name, table_rows
  FROM information_schema.partitions
 WHERE table_schema = 'order_db' AND table_name = 'idempotency_record'
 ORDER BY partition_ordinal_position;
```

> ⚠️ `information_schema.tables.table_rows` 在 InnoDB 下是**采样估算**，频繁 `ANALYZE` 反而会锁表。水位指标只需要「趋势 + 分区级数量」，估算值完全够用；要精确到个位数，说明你在用它做业务判断，那是设计错误。

### 6.7.5 演练：让保护机制「假死」一次

只测正常路径的团队，第一次遇到存储故障就会用「降级放行」把它变成事故。演练脚本：

```bash
# 1) 让幂等存储不可用 60 秒（Chaos Mesh NetworkChaos 或直接 iptables）
kubectl -n prod exec deploy/order -- \
  sh -c 'iptables -A OUTPUT -p tcp --dport 6379 -j DROP; sleep 60; iptables -D OUTPUT -p tcp --dport 6379 -j DROP'

# 2) 同时打 200 并发同参（固定 key），观察行为
k6 run -e BASE=http://order.internal ./idem-200.js

# 3) 判定标准（三条，缺一不可）
#    a. 请求返回 503，而不是 200（降级放行 = 事故）
#    b. 存储恢复后，重放同一 key → 业务表仍然只有 1 条
#    c. 指标 idempotency_store_error_total 有值，且告警在 2 分钟内触发
```

---

## 6.8 用什么工具

| 需求 | 首选 | 理由 / 坑 |
| --- | --- | --- |
| 指标采集（Java） | Micrometer + `MeterRegistry` | `Counter` 用 `increment()`；**别给 Counter 加高基数 label** |
| 指标采集（Go） | `prometheus/client_golang` + `promauto` | `promauto` 自动注册，重复注册会 panic（测试里要 `NewRegistry()`） |
| trace 埋点 | OpenTelemetry SDK + Collector | 用 tail sampling 保 `result=hit/error`；attribute 不要放原始键 |
| 存储 | MySQL（权威）+ Redis（前置） | Redis 只做缓存，见 §6.4.5 |
| 长流程幂等 | Temporal `WorkflowID` | `WorkflowIDReusePolicy` 决定「同 ID 能不能再跑」，与业务语义强相关 |
| 告警 | Prometheus Rule + Alertmanager | 阈值用相对基线，不用绝对值（第 21 章展开） |
| 看板 | Grafana | 一屏四图：命中率、去重失败率、PROCESSING 年龄、表水位 |

**自研边界**：指标采集、trace、告警都别自研；**必须自研的是「键规范 + 指标口径 + 分层落点」**——这三件事没有任何框架能替你做，做错了框架只会把你的错误放大一万倍。

---

## 6.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 四元组 | 每次判定留：scope+key 指纹、结果、代价、是否重放 |
| 四个指标 | 命中率、重复请求率、去重失败率、表水位与日增 |
| 命中率口径 | 分母只能是 `hit + miss`；`conflict` 不进分母 |
| 命中率高≠好 | 要和重试放大系数一起看：QPS 涨 = 风暴，QPS 平 = 正常重试 |
| trace | 被拦的请求也要有 span；`result=hit` 的 trace 必须 100% 保留 |
| 原始键 | 只存 hash 前 8 位，不进 label、不进日志明文 |
| 分层 | 网关挡流量、SDK/服务保业务、DB 唯一索引兜底；DB 层不能省 |
| SDK 五语义 | Begin / Commit / Abort / Replay / InProgress→425 |
| 存储故障 | 必须 503 明确失败，**绝不降级放行** |
| 5xx | 不 Commit、不缓存；否则瞬时故障被固化成永久成功 |
| 水位 | 建表就要分区；清理用 `DROP PARTITION`，不用 `DELETE` |
| Agent 差异 | 命中时钱可能已经花了 → 命中率必须与 token 一起看 |
| 不做会怎样 | 事故一 45 分钟、21.6 万元；事故二 4 小时、340 万元 |
| 一句话 | **幂等的可观测性，是让你能证明「保护机制还在」，而不是证明「服务还活着」。** |

## 6.10 自检题

1. **命中率从 12% 掉到 0.2%，但错误率、延迟、CPU 全绿。你的前三个动作是什么？**
   <details><summary>答案要点</summary>
   ① 看 `idempotent_request_total` 的 `scope` 拆分，确认是全局还是单接口；② 抽样两条请求对比 `key_hash`——不同则键漂移（header 被网关改写/服务端重新生成），相同则判定逻辑或存储异常；③ 查最近 1 小时的配置变更（网关 header 白名单、模型/提示词版本），回滚验证。事故一是典型：键变了但业务成功，所以只有命中率这一个指标能报警。
   </details>

2. **为什么幂等存储挂掉时不能「降级放行」？如果业务要求高可用怎么办？**
   <details><summary>答案要点</summary>
   降级放行等于在最需要保护的时刻关闭保护，200 会变成「业务执行了两次但返回成功」。正确做法：① 明确失败（503），让上游退避重试；② 若业务不可接受 503，退到「只读校验 + 业务唯一索引」这条更窄的路（DB 层仍能挡重复），并在指标上标记 `degraded=true`；③ 高危接口（资金/权益）永不降级。核心判断：幂等是正确性，不是可用性，不能拿正确性换可用性。
   </details>

3. **幂等 SDK 接入后 TPS 掉了 40%，你怎么定位是「必须付的成本」还是「实现有问题」？**
   <details><summary>答案要点</summary>
   看 `idempotency.check` span 的耗时占比：正常应 < 5ms（一次 Redis + 一次 MySQL）。常见实现问题：① 每次判定两次同步往返（Redis + MySQL 串行）；② 用 `COUNT(*)` 采水位；③ 高基数 label 导致指标侧 CPU 飙升；④ 事务被拉长（占位与业务不在同一事务却放在同一个事务模板里）。优化顺序：本地布隆前置 → Redis 只挡热点 → 批量写 → 只对高危接口开启。若判定 P99 > 5ms，先修实现再谈取舍。
   </details>

## 6.11 延伸阅读

- OpenTelemetry 规范：*Semantic Conventions* 与 `SpanKind`/`SpanStatus` 语义；Collector `tail_sampling` processor 的 `policy_type: string_attribute`（以你使用的版本为准）
- Prometheus 文档：`histogram_quantile`、`rate()` 对 Counter 重置的处理、`predict_linear`
- Stripe API 文档 *Idempotent requests*：24 小时窗口、并发 409、同键不同 payload 400（IETF 草案用 422）—— 最清晰的对外契约
- Micrometer 文档：`Counter`/`Gauge`/`Timer` 的命名与 `publishPercentileHistogram`
- 上一章：[5. 幂等的失效模式与测试：10 种翻车方式](5-idempotency-failure-modes.md)
- 下一章：[7. 本地事务：ACID 的真实边界与框架陷阱](7-local-transaction-pitfalls.md)
- 交叉引用：幂等定义与判定问答见第 1 章；幂等表 DDL 与租约见第 5 章；token 用量对账见第 14 章；告警降噪见第 21 章；Agent 侧 Run 与 step 模型见 [../llm/8-run-api-design.md](../llm/8-run-api-design.md)

**交叉引用**：总纲 [0-summary.md](0-summary.md) ｜ 语言与运行时 [../go/0-summary.md](../go/0-summary.md) ｜ 平台治理 [../llm/0-summary.md](../llm/0-summary.md)
