# 22. 压测与容量规划

> **状态**：`已完成（初稿）` ｜ **所属**：第三部分 · 流量与韧性：不崩是怎么做到的 ｜ **编号**：22 / 52
> **本章定位**：压测不是「打一波高流量看会不会挂」，而是一次**带隔离的实验**：用染色流量在生产验证「容量假设」，且不让一个字节污染真实数据。容量规划则是一道算术题——从目标 QPS 与 P99 反推副本数、限流值和连接池，算不出来就说明你还没规划。
> **读法**：§22.4 第一步（隔离）与第四步（容量模型）是本章的两根骨头，必须都读；§22.2 的六条反例建议直接贴到压测评审的门口；§22.5 只记一句：Agent 侧压测测的是**长连接与长 Run**，而且**压测本身在烧钱**。

---

## 22.1 引子：一次「只加了 header」的全链路压测

**事故一（2025-04-18，某零售平台全链路压测）**。压测方案评审时写了三条隔离：染色 header、影子表、影子 Topic。上线时只做了第一条。

- **13:58** 压测开始，200 个压测节点，目标 20,000 QPS，持续时间 30 分钟。流量带 `x-shadow: 1`。
- **14:00** 网关按 header 把流量标成「影子」，**但下游没有任何组件读这个标记**：订单服务照常写 `order_info`，积分服务照常写 `point_ledger`，MQ 照常写 `order.created`。
- **14:06** 短信供应商的客户经理打来电话：6 分钟内发出 **32,000 条**「您的订单已发货」短信。
- **14:11** 值班同学在群里看到「为什么有 8 万笔订单地址是『压测地址勿发货』」，才意识到问题。
- **14:29** 压测停止。此时已写入 **87 万条**压测订单、**11,000 张**优惠券被真实核销、**4,800 单**进入真实仓配（其中 1,700 单已实际出库）。
- **14:29—15:40** 压测流量把 Redis 连接池打满（影子流量与真实流量共用同一个集群），真实用户下单接口不可用 **18 分钟**，失败 **9,600 次**。
- **15:40—20:00** 清理：按 `remark like '%压测%'` 删订单 87 万条（分 42 批），冲正优惠券、拦截未出库的 3,100 单、对已出库的 1,700 单做退货处理。
- 合计赔付与折价 **24 万元**，客诉 **460 单**，压测报告作废重做。

**事故二（2025-06-18，同一个平台，大促前扩容）**。这次没有压测，只有一张 Excel。

- 去年大促峰值 6,000 QPS，今年业务说「按 3 倍备」，于是副本从 **8 扩到 24**，每个副本的 HikariCP `maximumPoolSize=50`。
- **19:40** 扩容完成。总连接数 = 24 × 50 = **1,200**，而 MySQL `max_connections=800`。
- **20:00** 大促开始，QPS 冲到 5,200。前 4 分钟看起来很美：CPU 32%、P99 130ms。
- **20:04** 连接申请开始排队，`Too many connections` 每秒 **1,400 次**；应用侧 `getConnection()` 超时 30s，Tomcat 线程被拖死。
- **20:06** P99 从 130ms → **8.2s**，错误率 41%。加副本的动作反而让情况更糟：每加一个副本，就多 50 个抢连接的进程。
- **20:47** 回滚到 8 副本 + 网关限流 4,500 QPS，21:02 恢复。可售时段损失 **47 分钟**，GMV 约 **260 万元**。

> 🩸 两个事故的共同点：**他们都在「验证容量」，但一个没做隔离，一个没做计算。** 压测不隔离 = 用真实用户做实验；扩容不算连接数 = 用连接池做自毁。压测和容量规划是一件事的两半：**压测回答「一个副本能扛多少」，容量模型回答「需要几个副本」**。缺任何一半，另一半都是猜的。

---

## 22.2 到底是什么：一次实验 + 一道算术

**定义**：

- **压测** = 在**与生产等价**的环境里，用**可控且可识别**的流量，验证一个明确的容量假设（「单副本 800 QPS 时 P99 ≤ 200ms」）。
- **容量规划** = 从业务目标（峰值 QPS、P99、可用性）**反推**资源（副本数、限流值、连接池、分区数），并留出可量化的余量。

两个词都有一个隐藏前提：**可测量**。如果你的压测结论是「挺稳的」，那不是结论，是感觉。

**六条反例**（每一条都配「不做会怎样」）：

| 你以为的压测/容量 | 为什么不是 | 不做会怎样 |
| --- | --- | --- |
| 「打到系统挂，挂之前的 QPS 就是容量」 | 挂之前的那一段 P99 早已超标 10 倍，用户已经跑了 | 按这个数配容量，大促时「能扛但不可用」 |
| 「压测环境 1:1 复制了生产，结论可直接用」 | 数据量、索引命中率、缓存命中率、网络拓扑没有一个是 1:1 | 压测 P99 120ms，生产 900ms，差 7 倍 |
| 「只压订单接口，其它接口不影响」 | 瓶颈常在共享资源：DB 连接、Redis、MQ、带宽 | 单接口压得很漂亮，全链路一上就雪崩 |
| 「平均延迟 80ms，很健康」 | 平均值掩盖长尾（第 20 章）；容量由 P99 决定 | 1% 的用户等 8 秒，正好是最有钱的那 1% |
| 「压测通过 = 容量够」 | 没测出**拐点**就没法留余量，也不知道先死在哪 | 峰值只超 10% 就整体崩，因为你在拐点上运行 |
| 「容量 = QPS」 | 容量是「QPS × 延迟」决定的**并发数**（Little's law） | 线程池/连接池按 QPS 配，实际需要的是 in-flight 数 |

**Little's law 是容量规划的底层公式，必须先记住**：

```
在飞请求数 L = 到达率 λ × 平均停留时间 W
例：λ = 5,000 QPS，W = 60ms  →  L = 300 个请求同时在处理
    P99 场景：λ = 5,000，W = 200ms → L = 1,000 个请求同时存在
```

这一行解释了事故二：他们扩了副本（λ 的承载能力），但 `L` 需要的连接数没算，于是连接池成了唯一的瓶颈。**QPS 是流量，L 才是资源。**

---

## 22.3 结构图：染色流量怎么走完全链路而不留痕

```
┌──────────────┐   header: x-shadow: 1 / x-shadow-id: pt-20250418-01
│ 压测引擎      │   uid 号段: 900000000-999999999（不与真实用户重叠）
│ (k6/vegeta)  │   trace 标签: shadow=true
└──────┬───────┘
       ▼
┌──────────────┐  ① 网关：识别 header → 打 ctx 标记；限流桶独立（影子流量不吃真实配额）
│   Gateway    │     不通过 → 直接 403（防漏标）
└──────┬───────┘
       ▼
┌──────────────┐  ② 服务：ShadowContext（ThreadLocal / context.Value）贯穿全链路
│ Order Service│     任何出站调用必须显式传播，否则视为 bug（见 §22.4 第一步）
└──┬────┬───┬──┘
   │    │   │
   │    │   └──────────────► ③ MQ：topic 后缀 .shadow（或 header + 消费端过滤）
   │    │                       消费者只订阅真实 topic，影子消息永不进入业务
   │    │
   │    └──────────────────► ④ Redis：key 前缀 shadow:{id}:（不是另一个集群，是另一个命名空间）
   │
   └───────────────────────► ⑤ DB：影子表 order_info_shadow / 影子库 shadow_trade
                               由 DAO 层按 ShadowContext 切表名，业务代码零改动

三条铁律：
  A. 染色标记必须在「入口」产生，在「出口」校验 —— 中间任何一跳丢失 = 污染
  B. 影子数据必须有「可识别 + 可批量清理」的物理标记（表名/前缀/号段），不靠 remark 字段
  C. 影子流量必须与真实流量「资源隔离或配额隔离」——否则压测就是把用户当压测数据
```

**为什么不能用「备注字段」当隔离**：事故一里他们就是靠 `remark like '%压测%'` 清理，结果 87 万条数据里有 12 万条备注被业务覆盖，清了 6 小时还漏了 3,000 条。**物理隔离（表名/库名）是唯一能一条 SQL 清干净的方案。**

---

## 22.4 怎么实现：六步，从染色到成本

### 第一步：染色规范与三层隔离

先定规范，再写代码。染色不是一个 header 的事，是**一套必须全链路传播的上下文**。

| 字段 | 取值 | 产生方 | 校验方 | 丢失后果 |
| --- | --- | --- | --- | --- |
| `x-shadow` | `1` / 空 | 压测引擎 | 网关（缺失即拒） | 流量进真实链路 |
| `x-shadow-id` | `pt-YYYYMMDD-NN` | 压测平台 | 全链路日志/DB 表名 | 无法按批次清理，只能全表扫 |
| `uid` 号段 | `900000000+` | 压测数据工厂 | 用户服务（越界即拒） | 与真实用户 ID 冲突，污染账户 |
| `trace.shadow` | `true` | 网关 | 所有出站客户端 | 影子调用打到真实下游 |
| `topic` 后缀 | `.shadow` | 生产者 | 消费者（订阅白名单） | 影子消息被真实消费者处理 |
| `redis key` 前缀 | `shadow:{id}:` | 缓存客户端 | —— | 覆盖真实缓存，用户看到压测数据 |

```yaml
# shadow/contract.yaml —— 染色契约，进 CI 校验：新增出站客户端必须声明传播方式
apiVersion: shadow/v1
kind: ShadowContract
metadata:
  name: order-service
spec:
  inbound:
    requiredHeader: x-shadow
    rejectIfMissingInProd: true      # 生产环境：带影子标记但无 id 的请求一律 403
    uidRange: { min: 900000000, max: 999999999 }
  propagate:
    http:  { header: x-shadow-id }   # 出站 HTTP 必须带
    grpc:  { metadataKey: x-shadow-id }
    mysql: { strategy: tableSuffix, suffix: "_shadow" }
    redis: { strategy: keyPrefix, template: "shadow:{id}:" }
    kafka: { strategy: topicSuffix, suffix: ".shadow" }
  cleanup:
    owner: trade-team
    maxTtlHours: 48                  # 影子数据存活上限，超时由清理任务强删
```

Java 侧：一个拦截器负责「入口识别」，一个 MyBatis 拦截器负责「出口切表」，两者靠 `ThreadLocal` 连接。

```java
// ① 入口：只在网关透传了合法标记时才进入 shadow 上下文
@Component
public class ShadowInterceptor implements HandlerInterceptor {
    public static final String HDR = "x-shadow";
    public static final String HDR_ID = "x-shadow-id";

    @Override
    public boolean preHandle(HttpServletRequest req, HttpServletResponse resp, Object handler) {
        String flag = req.getHeader(HDR);
        if (!"1".equals(flag)) {
            ShadowContext.clear();          // 关键：必须清理，否则线程复用会串味
            return true;
        }
        String shadowId = req.getHeader(HDR_ID);
        if (shadowId == null || !shadowId.matches("pt-\\d{8}-\\d{2}")) {
            resp.setStatus(403);            // 带标记但无批次号 = 漏标，直接拒绝
            return false;
        }
        ShadowContext.set(new ShadowContext.Shadow(shadowId));
        return true;
    }

    @Override
    public void afterCompletion(HttpServletRequest req, HttpServletResponse resp,
                                Object handler, Exception ex) {
        ShadowContext.clear();              // Tomcat 线程池复用，不清理就会污染下一个真实请求
    }
}

public final class ShadowContext {
    public record Shadow(String id) {}
    private static final ThreadLocal<Shadow> TL = new ThreadLocal<>();
    public static void set(Shadow s) { TL.set(s); }
    public static Shadow get() { return TL.get(); }
    public static boolean isShadow() { return TL.get() != null; }
    public static void clear() { TL.remove(); }
}
```

```java
// ② 出口：MyBatis 拦截器改写表名。所有影子表都用统一后缀，避免逐个 mapper 改
@Intercepts(@Signature(type = StatementHandler.class, method = "prepare",
                       args = {Connection.class, Integer.class}))
public class ShadowTableInterceptor implements Interceptor {
    @Override
    public Object intercept(Invocation inv) throws Throwable {
        if (!ShadowContext.isShadow()) {
            return inv.proceed();           // 真实流量：一行不改，零侵入
        }
        StatementHandler h = (StatementHandler) inv.getTarget();
        MetaObject meta = SystemMetaObject.forObject(h);
        String sql = (String) meta.getValue("delegate.boundSql.sql");
        // 只改白名单表，避免把 order_info_shadow 改成 order_info_shadow_shadow
        String rewritten = ShadowTables.rewrite(sql);
        meta.setValue("delegate.boundSql.sql", rewritten);
        return inv.proceed();
    }
}

final class ShadowTables {
    // 显式白名单：宁可漏配被测试发现，也不要正则误伤
    private static final List<String> TABLES = List.of(
            "order_info", "order_item", "payment_record", "point_ledger", "sku_stock");
    static String rewrite(String sql) {
        String out = sql;
        for (String t : TABLES) {
            out = out.replaceAll("\\b" + t + "\\b(?!_shadow)", t + "_shadow");
        }
        return out;
    }
}
```

Go 侧：靠 `context.Context` 传播，**任何出站调用必须显式取 ctx**，这是编译期能查的纪律。

```go
package shadow

import (
	"context"
	"database/sql"
	"fmt"
	"net/http"
	"strings"
)

type key struct{}

type Info struct {
	ID       string // pt-20250418-01
	TableSfx string // "_shadow"
}

func With(ctx context.Context, i *Info) context.Context { return context.WithValue(ctx, key{}, i) }

func From(ctx context.Context) (*Info, bool) {
	i, ok := ctx.Value(key{}).(*Info)
	return i, ok
}

// 入口中间件：标记非法直接拒绝，避免「半影子」流量
func Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("x-shadow") != "1" {
			next.ServeHTTP(w, r)
			return
		}
		id := r.Header.Get("x-shadow-id")
		if !strings.HasPrefix(id, "pt-") {
			http.Error(w, "shadow id required", http.StatusForbidden)
			return
		}
		next.ServeHTTP(w, r.WithContext(With(r.Context(), &Info{ID: id, TableSfx: "_shadow"})))
	})
}

// 出口：所有写库必须走它，禁止业务里直接拼表名
func Exec(ctx context.Context, db *sql.DB, table, query string, args ...any) (sql.Result, error) {
	if i, ok := From(ctx); ok {
		table += i.TableSfx
	}
	return db.ExecContext(ctx, fmt.Sprintf(query, table), args...)
}
```

```sql
-- 影子表：不要用 CREATE TABLE ... LIKE（会连索引一起复制，写放大 = 生产的两倍）
-- 正确做法：手写一份「只保留必要索引」的副本，主键必须一致
CREATE TABLE order_info_shadow (
  order_id      BIGINT UNSIGNED NOT NULL,
  user_id       BIGINT UNSIGNED NOT NULL,
  sku_id        BIGINT UNSIGNED NOT NULL,
  amount        DECIMAL(12,2)   NOT NULL,
  status        VARCHAR(24)     NOT NULL,
  shadow_batch  VARCHAR(32)     NOT NULL COMMENT '压测批次：pt-20250418-01',
  created_at    DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (order_id),
  KEY idx_batch (shadow_batch)          -- 只为「按批次一条 SQL 清干净」而建
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='压测影子订单表，禁止业务读取';

-- 批次登记表：每批压测一条，清理时按它算「应删 / 实删 / 残留」
CREATE TABLE shadow_batch_registry (
  batch_id       VARCHAR(32)  NOT NULL,
  scene          VARCHAR(64)  NOT NULL COMMENT '下单/支付/退款',
  target_qps     INT          NOT NULL,
  planned_rows   BIGINT       NOT NULL COMMENT '预期写入行数，用于对账',
  actual_rows    BIGINT       NOT NULL DEFAULT 0,
  started_at     DATETIME(3)  NOT NULL,
  stopped_at     DATETIME(3)  NULL,
  cleaned_at     DATETIME(3)  NULL,
  owner          VARCHAR(64)  NOT NULL,
  PRIMARY KEY (batch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='影子批次账本：没有它就无法证明清理干净';

-- 清理：一条 SQL 一个批次，分批删避免长事务
DELETE FROM order_info_shadow
 WHERE shadow_batch = 'pt-20250418-01'
 LIMIT 5000;   -- 循环执行直到 affected=0；生产上由脚本按 200ms 间隔跑
```

```sql
-- 压测后必须跑的三条核对：残留、越界、脏读
-- ① 批次残留（必须为 0）
SELECT b.batch_id, b.planned_rows, COUNT(o.order_id) AS left_rows
  FROM shadow_batch_registry b
  LEFT JOIN order_info_shadow o ON o.shadow_batch = b.batch_id
 WHERE b.cleaned_at IS NOT NULL
 GROUP BY b.batch_id, b.planned_rows
HAVING left_rows > 0;

-- ② 真实表里的压测用户（号段越界检查，必须为 0）
SELECT COUNT(*) AS polluted, MIN(created_at), MAX(created_at)
  FROM order_info
 WHERE user_id BETWEEN 900000000 AND 999999999;

-- ③ 真实表里的影子批次标记（表名切错时会留下这一列的证据）
SELECT shadow_batch, COUNT(*) FROM order_info
 WHERE shadow_batch <> '' GROUP BY shadow_batch;
```

### 第二步：三种隔离方案，按「污染代价」选

| 方案 | 做法 | 隔离强度 | 成本 | 能压出真实瓶颈吗 | 适用 |
| --- | --- | --- | --- | --- | --- |
| **影子表**（同库） | 表名加后缀，共用实例 | 中：数据隔离，资源不隔离 | 低（不需要额外实例） | 部分（DB 资源仍共享） | 日常压测、单链路 |
| **影子库**（同实例不同 schema） | 独立 schema + 独立连接池 | 中高：连接数仍共享 | 中 | 部分 | 中等规模、需要独立连接池验证 |
| **独立压测环境** | 全量复制一套 | 高 | 高（1:1 机器成本） | 弱（数据量与缓存命中率不同） | 上线前验收、演练 |
| **生产影子集群**（推荐大促） | 同 VPC、独立 DB/Redis/MQ，配置同生产 | 高 | 中高 | **强**（配置一致、数据可比） | 大促前全链路 |
| 只染色不隔离（事故一） | 加 header，下游不读 | **无** | 零 | 不能（数据被污染） | 任何场景都禁止 |

判断口径：**「压测数据能不能一条 SQL 清干净」**。答不出「清哪张表、按什么条件」，这个方案就不合格。

### 第三步：瓶颈识别顺序（连接数 → 池 → 锁 → GC → 下游）

压测曲线一定是这样的：QPS 线性上升 → 某处开始 P99 上翘 → 到某个点突然垂直（拐点）→ 错误率飙升。**拐点之前那个资源，就是瓶颈。** 找它的顺序不能乱，因为越靠前的越便宜、越靠后的越贵：

```
QPS ↑
 │                                    ╱ 垂直段：队列/连接耗尽，重试放大
 │                                  ╱
 │                            ╱──── 拐点（P99 开始非线性）
 │                     ╱──────
 │            ╱────────
 └────────────────────────────────────────► 时间/负载
        ↑ 先看：连接数是否打满（最先耗尽，最便宜）
        ↑ 再看：池子（连接池/线程池）是否排队
        ↑ 再看：锁等待（DB 行锁/Redis 分布式锁）
        ↑ 再看：GC（停顿导致抖动，曲线呈锯齿）
        ↑ 最后：下游（P99 抬升，但本地资源都健康）
```

| 顺序 | 检查对象 | 判定信号（阈值） | 命令/指标 | 典型修法 |
| --- | --- | --- | --- | --- |
| ① | **连接数**（TCP/DB） | `Too many connections`；`TIME_WAIT` > 2 万；`max_connections` 使用率 > 80% | `ss -s`、`SHOW STATUS LIKE 'Threads_connected'` | 扩连接上限、加代理、修连接泄漏 |
| ② | **池子**（Hikari/线程池） | `pending` > 0 持续 10s；`active == maximumPoolSize`；池等待 P99 > 50ms | `hikaricp_connections_pending`、`jstack` 看 `getConnection` | 算清 in-flight 再定池大小；缩短持有时间 |
| ③ | **锁** | `innodb_row_lock_waits` 上涨；`lock_wait_timeout`；Redis 锁 P99 > 100ms | `SHOW ENGINE INNODB STATUS`、`performance_schema.data_locks` | 缩短事务、按主键顺序更新、降隔离级别 |
| ④ | **GC** | Full GC > 1 次/分钟；STW > 200ms；老年代占比 > 85% | `jstat -gcutil 1s`、`-Xlog:gc*` | 降对象分配率、调堆、换 G1/ZGC |
| ⑤ | **下游** | 本地 CPU/池/锁全健康，但 P99 抬升；下游 P99 同步抬升 | 依赖拓扑图 + 下游 SLO 看板 | 限流/熔断/降级（第 18、19 章） |

判定技巧：**同时打开这五层的实时看板，压测时看「谁先到 80%」**。第一个到 80% 的就是瓶颈；到 100% 的那个是「已经死了」，不一定是根因。

```bash
# 压测中每 5 秒采样一次，事后对齐时间轴（这就是压测报告里的「证据」）
while true; do
  echo "=== $(date +%H:%M:%S) ==="
  mysql -N -e "SHOW STATUS WHERE Variable_name IN
    ('Threads_connected','Threads_running','Innodb_row_lock_waits','Innodb_row_lock_time_avg')"
  curl -s localhost:9090/actuator/prometheus | grep -E \
    'hikaricp_connections_(active|pending|max)|http_server_requests_seconds_count'
  jstat -gcutil $(pgrep -f order-service | head -1) | tail -1
  sleep 5
done | tee /tmp/pressure-watch.log
```

### 第四步：容量模型（完整算式 + 算例）

**输入**：峰值目标 `QPS_target = 5,000`，P99 目标 `≤ 200ms`，单实例在 P99≤200ms 下的实测安全吞吐 `qps_instance = 800`（来自第三步的拐点测试）。

```
【① 副本数】
  理论副本数   N_min  = ceil(QPS_target / qps_instance) = ceil(5000/800) = ceil(6.25) = 7
  容灾副本     N      = N_min + 2（容忍 2 个实例/AZ 同时不可用）= 9
  实载率       U      = QPS_target / (N × qps_instance) = 5000/7200 = 69.4%   ← 目标 60%~75%
  容量余量     H      = (N × qps_instance) / QPS_target - 1 = 44%
  → 结论：9 副本。低于 7 个，单实例就超过 800 的拐点；只配 7 个，挂 1 个就进入非线性区。

【② 限流值（每个实例）】
  单实例限流 = qps_instance × 安全系数 = 800 × 0.75 = 600
  集群限流   = 9 × 600 = 5,400  （> 5,000 目标，允许 8% 的瞬时超发）
  → 网关/服务两层都要配：服务层 600/实例（保护自己），网关层 5,000（保护下游）
  ⚠️ 扩容时集群限流不会自动变大，必须按副本数重算（第 15 章的事故就是这个）

【③ 并发数（Little's law）】
  平均延迟 W_avg = 60ms  → 单实例 in-flight = (5000/9) × 0.060 = 33.4
  P99 延迟 200ms         → 单实例 in-flight = (5000/9) × 0.200 = 111.1
  线程池下限 = P99 in-flight × 1.5 = 167  → Tomcat 200 勉强，建议 300
  → 如果线程池只有 100：即便 CPU 空着，QPS 也上不去（这是最常见的「压不上去」）

【④ 连接池】
  若每个请求平均持有 DB 连接 8ms：
    单实例需求 = 33.4 × (8/60) ≈ 4.5  → 5 个就够（但 P99 尖刺会到 20+）
  实践取：pool = max(in-flight × 1.5, 10) = 50
  集群总连接 = 9 × 50 = 450，占 max_connections=800 的 56%  ✓
  ⚠️ 若某条 SQL 从 8ms 变 200ms：450 连接能支撑的 QPS = 450/0.2 = 2,250
     → 一条慢 SQL 就能把「能扛 5,000」变成「只能扛 2,250」，所以 DB 侧要有慢查询熔断。
```

**拐点实测表（本次压测的真实数据，报告里必须贴）**：

| 单实例 QPS | P99 | 错误率 | CPU | 池 pending | 结论 |
| --- | --- | --- | --- | --- | --- |
| 400 | 90ms | 0% | 28% | 0 | 舒适区 |
| 600 | 120ms | 0% | 42% | 0 | 舒适区上限 |
| **800** | **200ms** | **0.01%** | **61%** | **0** | **拐点前，取此值为安全吞吐** |
| 900 | 480ms | 0.3% | 68% | 2 | 已越界（P99 超标 2.4 倍） |
| 1,000 | 1.6s | 4.1% | 71% | 18 | 队列形成 |
| 1,200 | 8.2s | 23% | 74% | 96 | 雪崩段（CPU 反而没满） |

注意 1,200 QPS 时 **CPU 只有 74%**——这就是「CPU 没满但已经死了」的典型，瓶颈在等待而不是计算。只看 CPU 扩容的团队，会在这里加机器，然后发现 P99 更高。

```go
package capacity

import "math"

type Input struct {
	PeakQPS      float64 // 业务峰值目标
	InstanceQPS  float64 // 拐点实测：P99 达标时的单实例吞吐
	P99Millis    float64 // P99 目标
	AvgMillis    float64 // 平均延迟
	LossBudget   int     // 允许同时不可用的实例数（容灾）
	PoolPerInst  int     // 当前连接池配置
	MaxDBConns   int     // 数据库 max_connections
	SafetyFactor float64 // 限流安全系数，0.7~0.8
}

type Result struct {
	Instances      int     `json:"instances"`
	Utilization    float64 `json:"utilization"`
	Headroom       float64 `json:"headroom"`
	RateLimitPer   int     `json:"rate_limit_per_instance"`
	RateLimitTotal int     `json:"rate_limit_total"`
	InflightAvg    float64 `json:"inflight_avg"`
	InflightP99    float64 `json:"inflight_p99"`
	MinThreads     int     `json:"min_threads"`
	TotalDBConns   int     `json:"total_db_conns"`
	DBUsage        float64 `json:"db_usage"`
	Warnings       []string
}

func Plan(in Input) Result {
	if in.SafetyFactor == 0 {
		in.SafetyFactor = 0.75
	}
	// ① 副本：先满足吞吐，再加容灾
	n := int(math.Ceil(in.PeakQPS / in.InstanceQPS))
	n += in.LossBudget
	perInst := in.PeakQPS / float64(n)

	r := Result{
		Instances:   n,
		Utilization: perInst / in.InstanceQPS,
		Headroom:    float64(n)*in.InstanceQPS/in.PeakQPS - 1,
		// ② 限流：单实例按拐点打折，集群值 = 单实例 × 副本
		RateLimitPer:   int(in.InstanceQPS * in.SafetyFactor),
		InflightAvg:    perInst * in.AvgMillis / 1000,
		InflightP99:    perInst * in.P99Millis / 1000,
	}
	r.RateLimitTotal = r.RateLimitPer * n
	// ③ 线程：按 P99 的在飞数留 1.5 倍，最少 50
	r.MinThreads = int(math.Ceil(r.InflightP99 * 1.5))
	if r.MinThreads < 50 {
		r.MinThreads = 50
	}
	// ④ 连接：池子必须覆盖平均在飞 × 1.5，且全集群不能超过 DB 上限
	r.TotalDBConns = n * in.PoolPerInst
	r.DBUsage = float64(r.TotalDBConns) / float64(in.MaxDBConns)

	if r.Utilization > 0.75 {
		r.Warnings = append(r.Warnings,
			"实载率 >75%：拐点附近运行，P99 会非线性劣化，建议加 1~2 个副本")
	}
	if r.MinThreads > 200 {
		r.Warnings = append(r.Warnings,
			"P99 在飞数超过 Tomcat 默认 200 线程，必须调大或改异步 Servlet")
	}
	if r.DBUsage > 0.7 {
		r.Warnings = append(r.Warnings,
			"集群连接数超过 DB 上限 70%，扩容前必须先算连接（事故二）")
	}
	if r.RateLimitTotal < int(in.PeakQPS) {
		r.Warnings = append(r.Warnings,
			"集群限流值低于业务峰值：限流会误伤真实流量")
	}
	return r
}
```

跑一遍 `Plan{PeakQPS: 5000, InstanceQPS: 800, P99Millis: 200, AvgMillis: 60, LossBudget: 2, PoolPerInst: 50, MaxDBConns: 800}`，输出：**9 实例 / 实载 69.4% / 限流 600×9=5,400 / P99 在飞 111 / 线程 167 / DB 连接 450(56%)**，与手算完全一致。这个函数应该进 CI：**配置变了就跑一遍，警告不为空就拦住发布。**

### 第五步：压测报告必须写全八项

| # | 项目 | 必须包含 | 常见缺失（=报告不可信） |
| --- | --- | --- | --- |
| 1 | 目标与判据 | SLO 口径（P99/错误率/成功率的定义） | 只写「验证系统稳定性」 |
| 2 | 环境差异清单 | 与生产逐项对比：节点数、数据量、索引、缓存命中率、网络 RTT | 一句「环境同生产」 |
| 3 | 流量模型 | 场景配比、参数分布（热点 key 占比）、思考时间、是否含重试 | 只写「2 万 QPS」 |
| 4 | 结果 | 吞吐/延迟分位（P50/P95/P99/P999）+ 错误码分布 + 成功率 | 只给平均值 |
| 5 | 瓶颈证据 | 拐点位置、五层信号的时间对齐截图（§22.4 第三步的日志） | 「感觉是数据库」 |
| 6 | 容量结论 | 单实例安全 QPS、副本数、限流值、余量、失效域假设 | 只写「能扛 5 万」 |
| 7 | 成本 | 每万 QPS 的机器成本 + 压测本身的成本（含 Agent 的 token 费） | 完全不提 |
| 8 | 未验证项 | 明确列出没压的场景与原因（如「退款链路未压，因影子表未建」） | 假装全测过 |

**第 8 项是区分「能信」与「不能信」的分水岭**：一份敢写「这四种情况我们没测」的报告，比一份「全部通过」的报告可信十倍。

### 第六步：Agent 侧——压长 Run 与流式连接，还要算钱

Agent 的压测和传统系统有三处根本不同，**用 QPS 思维会直接算错容量**：

1. **单次请求持续 30~180 秒**，不是 60ms。容量由「同时挂着的连接数」决定，不是 QPS。
2. **流式响应（SSE/WebSocket）要一直占着连接**，连接数 = QPS × 平均时长，很容易上万。
3. **每次压测都在真实花钱**，压 1 万次 Run 就是几千元，必须单独做预算与降本方案。

```
【并发连接数】
  SSE 长连接数 = Run QPS × 平均 Run 时长
  例：20 QPS × 45s = 900 条并发长连接；P99 180s 时 = 3,600 条
  → 网关的 keepalive/最大连接数、LB 的空闲超时（必须 > 心跳间隔 × 3）都要按 3,600 配

【模型并发】
  模型并发 = Run QPS × 每 Run 步数 × 单步耗时（秒）
  例：20 × 3 × 2.5s = 150 路并发模型调用
  → 供应商 RPM 配额要 ≥ 150 × 60/2.5 = 3,600 RPM，否则排队

【成本】
  单 Run 成本 = in_tok × p_in + out_tok × p_out
  例：in 5,625 tok @ 8 元/M、out 1,875 tok @ 24 元/M
    = 0.045 + 0.045 = 0.09 元/Run
  压 1 万次 Run = 900 元；若含重试 1.2 倍 = 1,080 元
  → 压测预算必须提前批，否则「压测把当月预算压没了」
```

```go
package agentload

// 压测成本估算：先算钱，再决定压多少。压测计划里必须有这一行。
type RunProfile struct {
	Steps       int     // 每 Run 步数
	InTokens    int     // 每步输入 token
	OutTokens   int     // 每步输出 token
	PriceInPerM float64 // 元 / 百万 token
	PriceOutPerM float64
	RetryRatio  float64 // 重试放大系数，1.0 表示无重试
}

func (p RunProfile) CostPerRun() float64 {
	in := float64(p.Steps*p.InTokens) * p.PriceInPerM / 1e6
	out := float64(p.Steps*p.OutTokens) * p.PriceOutPerM / 1e6
	return (in + out) * p.RetryRatio
}

// Concurrency 给出压测必须验证的三个「并发」指标
func (p RunProfile) Concurrency(runQPS, avgRunSec, p99RunSec, stepSec float64) (connsAvg, connsP99, modelConc float64) {
	connsAvg = runQPS * avgRunSec
	connsP99 = runQPS * p99RunSec
	modelConc = runQPS * float64(p.Steps) * stepSec
	return
}

// 降本方案：压框架时把模型换成 mock，只保留 5% 的真实调用做端到端校验
func (p RunProfile) BudgetAwareRuns(budgetCNY float64, mockRatio float64) (realRuns, mockRuns int) {
	unit := p.CostPerRun()
	if unit <= 0 {
		return 0, 0
	}
	realRuns = int(budgetCNY * mockRatio / unit)
	mockRuns = int(float64(realRuns) * (1 - mockRatio) / mockRatio)
	return
}
```

**压测脚本要分两段**：`mock 模式`压框架（网关/队列/DB/SSE 连接数，模型返回固定 200 token 的假流），`real 模式`压端到端（只跑 200~500 次，验证真实时延与 token 计数）。**只用 real 模式压 1 万次，既贵又测不出瓶颈**——因为瓶颈在模型供应商，不在你的系统。

```bash
# k6：SSE 长连接 + 阶梯加压。重点看 http_reqs 之外的 vus（并发连接）与 iterations
k6 run \
  --stage 1m:50 --stage 5m:500 --stage 10m:900 --stage 2m:0 \
  -e BASE_URL=https://agent.internal -e SHADOW=1 \
  ./sse-run.js

# vegeta：适合压「创建 Run」这种短请求；-max-workers 决定并发，不是 -rate
echo "POST http://agent.internal/v1/runs" | vegeta attack \
  -rate=20 -duration=5m -max-workers=200 -timeout=200s \
  -header "x-shadow: 1" -body ./run.json | tee /tmp/run.bin | vegeta report -type='hist[0,1s,5s,30s,180s]'
```

---

## 22.5 双修对照：同一道算术，两个「W」

| 维度 | 传统系统 | Agent 系统 | 同 or 异 |
| --- | --- | --- | --- |
| **机制本体** | 染色流量 + 影子表 + 拐点测试 + Little's law | **完全相同**：染色（`x-shadow`）、影子表（`run_shadow`）、拐点测试、Little's law | **同（可直接复用）** |
| **身份 / 唯一键** | `uid` 号段 + `shadow_batch` | `run_id` 前缀（`pt-`）+ `shadow_batch`；**Step 事件带批次号** | 同构，键多一层 |
| **W（停留时间）** | 60ms~2s | **45~180 秒**，长 100 倍 | **异**：并发数被放大 100 倍 |
| **容量单位** | QPS、并发连接、DB 连接 | QPS + **并发长连接** + **模型并发** + TPM | 单位扩展 |
| **瓶颈顺序** | 连接→池→锁→GC→下游 | 同上 + **供应商 RPM/TPM** + **单 Run 预算** | 多两层 |
| **失败模式** | 压测污染生产、扩容打满连接池 | 加：**压测烧掉当月预算**；重复 Step 让压测成本翻倍 | 多一个成本维度 |
| **恢复方式** | 压测中断就重跑脚本，无状态、无成本 | **从 `run_checkpoint` 续跑**；重跑整条 Run = 重花一次 token | **异**：长事务 + 成本 |
| **验证方法** | 阶梯加压找拐点、五层信号对齐 | 同 + **token 计量对账**（压测消耗 vs 账单）+ 流式连接保持率 | 同 + 一笔账 |
| **成本结构** | 机器折旧（压测边际成本 ≈ 0） | **按次计费**（压 1 万 Run ≈ 900 元） | **异（根因：成本显性且按量）** |
| **稀缺资源** | 线程、连接、DB | **TPM、模型并发、预算** | 限的对象不同 |

**可以直接复用的（一行不改）**：染色契约 YAML、MyBatis 表名改写拦截器、`shadow_batch_registry` 表与清理 SQL、五层瓶颈判定表、容量计算器、报告八项模板。

**只有三样必须重算**：

1. **W 从毫秒变分钟**。传统系统 5,000 QPS × 60ms = 300 在飞；Agent 20 QPS × 45s = 900 并发连接，**QPS 只有 1/250，连接数却是 3 倍**。用 QPS 定容量会把网关连接数配小 100 倍。
2. **拐点不在你手里**。传统系统的拐点由你的代码和 DB 决定；Agent 的拐点常在**供应商**——你的系统 CPU 20%，但模型 RPM 打满，P99 从 3s 变 60s。所以 Agent 压测必须**同时压供应商配额**（用 mock 模拟 429），验证排队与降级逻辑（第 19 章）。
3. **压测成本要进预算**。传统压测的成本是「占了一会儿机器」，可以忽略；Agent 压测的成本是**真金白银**，必须`先算钱再压`，并用 mock 分流。这也是为什么 Agent 的容量报告第 7 项（成本）不能省。

> 三定律检验：**状态机**——压测的真相源是 `shadow_batch_registry` 与拐点数据，不是「感觉挺稳」；**稀缺资源**——传统稀缺的是连接，Agent 稀缺的是 TPM 与预算；**不可回滚**——压测发出去的短信、扣掉的优惠券、烧掉的 token 都收不回来，所以隔离必须在**发出前**做。

---

## 22.6 会在哪炸：失效模式清单

| # | 现象 | 根因 | 怎么查 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- | --- |
| 1 | 压测后生产出现大量假订单/假短信 | 只染色未隔离，下游不读标记 | `user_id` 号段越界查询（§22.4 第一步 SQL） | 按批次冲正 + 清理 | 染色契约进 CI；无 `shadow_batch` 的写入直接拒绝 |
| 2 | 压测把真实用户打死 18 分钟 | 影子流量与真实流量共享 Redis/DB 配额 | 对比压测时间窗与真实错误率 | 影子流量独立限流桶 + 独立连接池 | 压测流量必须在网关层走独立配额 |
| 3 | 扩容后错误率反而升高 | 连接总数 > `max_connections`（事故二） | `24×50=1,200 vs 800` | 回滚 + 按 §22.4 第四步重算 | 容量模型进 CI：`副本×池 ≤ DB 上限×0.7` |
| 4 | 压测 QPS 上不去，CPU 只有 30% | 线程池/连接池太小，请求在排队 | `hikaricp_connections_pending`、`jstack` | 按 P99 在飞数 ×1.5 配池 | 池大小由容量模型算出，禁止拍脑袋 |
| 5 | 单实例压到 1,200 QPS 很稳，生产 800 就崩 | 压测数据量小、索引全命中；生产数据 20 亿行 | 对比 `EXPLAIN` 与缓存命中率 | 用生产数据副本压（脱敏） | 报告第 2 项必须写数据量差异 |
| 6 | 拐点找错，按「挂之前的 QPS」配容量 | 只看「有没有挂」，没看 P99 与错误率 | 画 QPS-P99 曲线，找斜率突变点 | 以 P99 达标的最大 QPS 为安全值 | 报告必须贴拐点表（六行数据） |
| 7 | 压测报告没人信 | 缺环境差异、缺瓶颈证据、缺未验证项 | 按八项逐条对 | 重写 | 八项模板进评审 checklist |
| 8 | Agent 压测一次烧掉 3 万预算 | 全量真实模型调用，无 mock、无上限 | 查账单峰值与 `run_id` 批次 | 改 mock 分流 + 预算硬上限 | 压测计划必须含 `BudgetAwareRuns` 估算与审批 |

---

## 22.7 怎么验证：压测本身也要被验证

**① 阶梯加压找拐点**（不是一上来打峰值，那样只能得到「挂了」）：

```bash
# 每档 5 分钟，档间不降压，观察 P99 是否可恢复（不可恢复说明有泄漏/队列积压）
for qps in 400 600 800 900 1000 1200; do
  echo "### target ${qps} QPS $(date +%H:%M:%S)"
  echo "GET http://order.internal/api/order?sku=1001" | vegeta attack \
    -rate=${qps} -duration=5m -timeout=10s -max-workers=400 \
    -header "x-shadow: 1" -header "x-shadow-id: pt-20250418-01" \
    | tee /tmp/step-${qps}.bin | vegeta report -type='hdr[0,50ms,100ms,200ms,500ms,1s,5s]'
  sleep 60   # 让队列排空，否则下一档测的是上一档的残留
done
```

**② 故障注入**（验证「少 2 个副本还达标」这个容灾假设）：

```yaml
# chaos/stress-cpu.yaml —— Chaos Mesh：给 2 个 Pod 打满 CPU，验证 N+2 假设
apiVersion: chaos-mesh.org/v1alpha1
kind: StressChaos
metadata:
  name: order-api-cpu-2pods
  namespace: trade
spec:
  mode: fixed-percent
  value: "22"                 # 9 副本里的 2 个
  selector:
    namespaces: [trade]
    labelSelectors: { app: order-api }
  stressors:
    cpu: { workers: 4, load: 100 }
  duration: "10m"
  scheduler: { cron: "@every 24h" }
```

期望：压测维持 5,000 QPS，P99 从 200ms 涨到不高于 260ms（余量 44% 应该够），错误率 < 0.1%。若不达标，说明 `N_min + 2` 的假设错了，要么加副本，要么把 `qps_instance` 下调。

**③ 隔离有效性验证**（每季度一次，比压测更重要）：

```sql
-- 造 1 万条影子订单，然后检查真实表是否被污染。期望：polluted = 0
SELECT
  (SELECT COUNT(*) FROM order_info_shadow WHERE shadow_batch='pt-selftest') AS shadow_rows,
  (SELECT COUNT(*) FROM order_info WHERE user_id BETWEEN 900000000 AND 999999999) AS polluted,
  (SELECT COUNT(*) FROM point_ledger WHERE user_id BETWEEN 900000000 AND 999999999) AS polluted_points;
```

**④ 四个必看指标**：`单实例 QPS / P99`、`池 pending`、`DB 连接使用率`、`影子数据残留数`。前三个决定容量结论，第四个决定你会不会在半夜删数据。

---

## 22.8 用什么工具：压测引擎好选，隔离必须自研

| 能力 | 开源够用 | 必须自己写 |
| --- | --- | --- |
| 压测引擎 | k6（脚本化、SSE/WS 支持好）、vegeta（恒定速率、报告干净）、JMeter（复杂协议） | 流量模型（场景配比、热点分布）——抄不到 |
| 影子隔离 | ShardingSphere（影子库 hint 路由）、MyBatis 拦截器 | 染色契约、表名白名单、批次清理脚本 |
| 消息隔离 | Kafka topic 后缀、RocketMQ namespace | 消费端订阅白名单（防误订阅 `.shadow`） |
| 故障注入 | Chaos Mesh（NetworkChaos/StressChaos/PodChaos） | 注入与 SLO 联动的「自动中止」逻辑 |
| 容量计算 | —— | 容量模型代码化（进 CI）+ 拐点数据存档 |
| Agent 压测 | k6（SSE）、mock 模型服务 | **成本估算 + mock/real 分流 + 预算硬闸门** |

**判断口径**：压测引擎随便换，**隔离方案与容量模型不能外包**——它们依赖你的表结构、你的 SLO、你的账单。

---

## 22.9 一页速记

| 关键词 | 必须记住的结论 |
| --- | --- |
| 压测的定义 | 带隔离的实验：验证「单实例 800 QPS 时 P99 ≤ 200ms」这种可判定的假设 |
| 隔离铁律 | 染色必须在入口产生、出口校验；影子数据必须有**物理标记**（表名/前缀），不靠 remark |
| 三种隔离 | 影子表（便宜）< 影子库 < 独立环境；大促用「生产影子集群」 |
| 瓶颈顺序 | 连接数 → 池 → 锁 → GC → 下游；同时开五层看板，**谁先到 80% 谁是瓶颈** |
| 拐点 | P99 斜率突变的那一点；「挂之前的 QPS」不是容量，是遗言 |
| 容量公式 | `N = ceil(QPS/单实例) + 容灾数`；`实载率 = QPS/(N×单实例)`，目标 60%~75% |
| 限流值 | 单实例 `单实例安全 QPS × 0.75`；集群 = 单实例 × 副本数，**扩容必须重算** |
| 并发数 | `L = λ × W`；P99 的 W 决定线程池，平均值决定不了 |
| 连接池 | 按 in-flight 算，不按 QPS 算；全集群连接 ≤ `max_connections × 0.7`（事故二的教训） |
| 报告八项 | 目标、环境差异、流量模型、结果分位、瓶颈证据、容量结论、成本、**未验证项** |
| Agent 侧 | W 从 60ms 变 45s，容量单位是**并发长连接**；压测**烧钱**，先算预算再压 |
| 一句话 | **压测算出「一个副本能扛多少」，容量模型算出「要几个副本」，中间那道除法谁都不能省。** |

## 22.10 自检题

1. **目标峰值 6,000 QPS，单实例在 P99 ≤ 150ms 下实测 750 QPS，要求容忍 1 个实例不可用。副本数、单实例限流值、P99 在飞数各是多少？**
   > 要点：`N_min = ceil(6000/750) = 8`，加 1 个容灾 = **9 副本**；实载率 `6000/(9×750) = 88.9%` > 75%，**不合格**，要加到 10 副本（实载 80%）或 11（72.7%）。单实例限流 = `750×0.75 ≈ 560`，集群 = `560×10 = 5,600` < 6,000 → **限流会误伤**，说明必须加到 11 副本（`560×11=6,160`）。P99 在飞 = `(6000/11)×0.15 ≈ 82`，线程池 ≥ 123。**这道题的关键是发现「容量、限流、余量」三者会互相打架，必须一起解。**

2. **压测报告写「系统可支撑 5 万 QPS」，评审时你会追问哪五个问题？**
   > 要点：① 这个数是「P99 达标的最大值」还是「挂之前的峰值」；② 单实例还是集群，几个副本，实载率多少；③ 环境与生产差在哪（数据量、索引、缓存命中率、网络）；④ 瓶颈是什么，证据在哪（五层信号的时间对齐）；⑤ 哪些链路没压。五个里任何一个答不出，这个数字就不能进容量规划。

3. **Agent 侧要压 5,000 次 Run 验证系统上限，每次 Run 3 步、每步 2,500 token（in:out = 3:1），单价 in 8 元/M、out 24 元/M。直接压要花多少钱？你会怎么改？**
   > 要点：单 Run `= 5,625×8/1e6 + 1,875×24/1e6 = 0.045+0.045 = 0.09 元`；5,000 次 = **450 元**（看起来不多，但如果压 5 万次、且加 1.2 倍重试就是 5,400 元）。改法：① 95% 流量走 mock 模型（返回固定 200 token 假流），只压框架的 SSE 连接数/队列/DB；② 5%（250 次）走真实模型，验证端到端时延、token 计数与成本对账；③ 压测前用 `BudgetAwareRuns` 估算并设预算硬闸门。**注意：真正的风险不是这 450 元，而是「重复 Step 让实际调用翻倍」——压测里必须校验 `step_id` 唯一。**

## 22.11 延伸阅读

- 压测工具：k6 官方文档 `stages` 与 `http.batch`；vegeta 的 `-rate/-max-workers` 语义（恒定速率 vs 恒定并发）；JMeter 的 `Concurrency Thread Group`
- 隔离实现：ShardingSphere 的 `HintManager` 强制路由（影子库的标准做法）；MyBatis `Interceptor` 改写 `BoundSql`
- 故障注入：Chaos Mesh `StressChaos` / `NetworkChaos` / `PodChaos` 的 `mode` 与 `scheduler` 字段
- 容量模型：Google SRE Workbook「Managing Load」；Little's law 的原始表述（`L = λW`）
- 指标与分位数（P99 怎么算才对）：[第 20 章](20-metrics-system.md)；拐点与限流值的关系：[第 15 章](15-rate-limit-basics.md)、[第 16 章](16-distributed-rate-limit.md)
- SLO 与预算（容量不达标时该触发什么）：[第 21 章](21-slo-alerting.md)；熔断降级（下游成为瓶颈时）：[第 18 章](18-circuit-breaker.md)、[第 19 章](19-degradation-design.md)
- 分库分表与连接数上限：[第 36 章](36-mysql-limits-sharding.md)；存储容量估算：[第 40 章](40-capacity-tiering-cost.md)
- 总纲：[0-summary.md](0-summary.md) ｜ 语言与运行时：[../go/0-summary.md](../go/0-summary.md) ｜ 平台治理：[../llm/0-summary.md](../llm/0-summary.md)
- 前置：[第 21 章](21-slo-alerting.md) ｜ 下一章：[第 23 章 异步解耦的本质与选型](23-async-messaging-basics.md)

**交叉引用**：Agent 侧长任务与流式的平台视角见 [../llm/12-long-task-durable.md](../llm/12-long-task-durable.md)；token 成本模型见 [../llm/47-cost-engineering.md](../llm/47-cost-engineering.md)。
