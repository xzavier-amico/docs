# 45. 记忆与会话：状态一致性

> **状态**：`已完成（初稿）` ｜ **所属**：第七部分 · AI-Agent 应用架构 ｜ **编号**：45 / 54
> **本章定位**：LLM 是无状态的，所以"记忆"完全是**你自己搭的一套状态系统**：怎么存、怎么裁、怎么防止多副本互相踩。这一章把聊天记录变成分层记忆，并给出 Redis 会话读写的最小可跑实现。
> **读法**：45.2 的图和表先记住（分层 + 选型），45.3.5 的并发一致性是全章重点。向量检索细节见第 44 章，不做重复。

---

## 45.1 引子：用户的第二句话，Agent 就失忆了

线上客服 Agent 的三连击：

1. 用户：「我上周买的那个蓝色杯子」→ 用户：「**它**能退吗？」→ Agent：「请问您说的是哪件商品？」。**代词跨轮指代失败**——因为服务端只把最后一条消息发给了模型。
2. 你把整个对话历史塞进去后，P99 延迟从 1.2s 涨到 6s，单次成本涨了 7 倍。第 3 天有人贴了张报警截图：某会话上下文 **38 万 token**，直接 400。
3. 服务从 2 副本扩到 6 副本后，出现了「用户刚说的话消失」「回答里混进别人的订单号」。查了两小时发现：会话存在**进程内 `map`** 里，靠 L7 的会话保持勉强活着；扩容时新 Pod 没有旧数据，而 `map` 又没有锁。

这就是这一章要解决的三件事：**分层（该记什么）、预算（能带多少）、一致性（谁能写）**。

---

## 45.2 直觉模型：四层记忆，各有各的寿命

```
┌──────────────────────────────────────────────────────────────────────┐
│ ① 工作记忆（working set）—— 生命周期：一次请求，纯栈上/堆上 struct     │
│    system + 最近 N 轮 + 检索片段 + 工具结果 + 摘要                    │
│    预算：见 45.3.2 的裁剪算法，超了就是 400 / 变傻                    │
└──────────────▲──────────────────────────────────┬────────────────────┘
   装载(读+裁剪)│                                  │落盘(幂等 append)
┌──────────────┴───────────┐        ┌─────────────▼──────────────────┐
│ ② 短期记忆：消息流         │        │ ③ 滚动摘要                      │
│  Redis List / PG rows      │◄───────┤ 超阈值时把旧消息压成一段文本    │
│  TTL 7~30 天               │  压缩  │ (异步，绝不阻塞主链路)          │
└──────────────▲───────────┘        └────────────────────────────────┘
                │ 事实抽取（异步，每 N 轮）
┌──────────────┴───────────────────────────────────────────────────────┐
│ ④ 长期记忆：事实表（PG: subject/predicate/object/confidence/有效期）  │
│    + 向量索引（pgvector/qdrant，按 user/tenant 过滤）                  │
│    生命周期：跟随用户，不跟随会话；支持 superseded_by 冲突消解        │
└──────────────────────────────────────────────────────────────────────┘
```

四层放哪、活多久，先看这张表：

| 层 | 载体 | 容量/寿命 | 读延迟 | 一致性要求 | 谁写 |
| --- | --- | --- | --- | --- | --- |
| 工作记忆 | 请求内 struct | 单次请求 | 0 | 无 | handler |
| 短期消息流 | Redis List / PG 表 | 千条 / TTL 7~30 天 | 0.2~2ms | 需幂等、需 CAS | 主链路同步写 |
| 滚动摘要 | Redis Hash / PG 列 | 一条几百字 | 同左 | 需 CAS（防并发覆盖） | 后台异步 |
| 长期事实 | PG + 向量索引 | 万条 / 跟随用户 | 5~50ms | 需去重、可回滚 | 异步抽取 job |

> 💡 **一句话心智**：短期记忆是「原始日志」，摘要和长期事实是「物化视图」。视图可以重建，日志不能丢——**先把消息流写稳，再谈 Memory 的各种花活**。

---

## 45.3 底层实现

### 45.3.1 消息模型：先定结构，再谈记忆

```go
type Role string

const (
	RoleSystem    Role = "system"
	RoleUser      Role = "user"
	RoleAssistant Role = "assistant"
	RoleTool      Role = "tool"
)

type Message struct {
	ID         string    // 幂等键：由客户端 request_id 或 UUID 生成，全局唯一
	Role       Role
	Content    string
	ToolCallID string    // assistant 发起工具调用时的 id；对应的 role=tool 结果必须成对出现
	Tokens     int       // 写入时就算好，别在拼 prompt 时现算
	At         time.Time
}

type Session struct {
	TenantID  string
	ID        string
	UserID    string
	Version   int64     // 乐观锁版本，见 45.3.5
	Summary   string    // 滚动摘要
	UpdatedAt time.Time
}
```

两个容易忽略的硬约束：

1. **`tool` 消息必须和它的 `assistant.tool_call` 成对存在**。裁剪时把 assistant 的调用删掉、留下孤立的 tool 结果，多数供应商会直接返回 400（`messages with role 'tool' must be a response to a preceding message with 'tool_calls'`）。这是裁剪算法里最容易漏的一条。
2. **`ID` 就是幂等键**。客户端重试、网关重放、MQ 重投，同一个 `ID` 只能写一次（45.3.7）。

### 45.3.2 token 预算与消息裁剪：从尾部往前装

裁剪的目标不是"平等对待每条消息"，而是**保 system、保最近、保工具配对、丢中间**：

```go
// Trim 在 budget 内保留尽量多的后缀消息，保证 system 前缀完整、工具调用成对。
func Trim(msgs []Message, budget int) []Message {
	var system, rest []Message
	for _, m := range msgs {
		if m.Role == RoleSystem {
			system = append(system, m)
			continue
		}
		rest = append(rest, m)
	}
	used := 0
	for _, m := range system {
		used += m.Tokens
	}
	// 从最新往旧装
	i := len(rest)
	for i > 0 {
		j := i - 1
		// 如果这条是 tool 结果，把前面连续的 tool + 发起调用的 assistant 一起装上
		blockStart := j
		if rest[j].Role == RoleTool {
			for blockStart > 0 && rest[blockStart-1].Role == RoleTool {
				blockStart--
			}
			if blockStart > 0 && rest[blockStart-1].Role == RoleAssistant {
				blockStart--
			}
		}
		cost := 0
		for k := blockStart; k < i; k++ {
			cost += rest[k].Tokens
		}
		if used+cost > budget && len(rest[i:]) > 0 {
			break // 保留已有的一段，绝不切半块
		}
		used += cost
		i = blockStart
	}
	out := make([]Message, 0, len(system)+len(rest)-i)
	out = append(out, system...)
	return append(out, rest[i:]...)
}
```

更进一步的三档策略，按成本从低到高：

| 策略 | 触发条件 | 代价 | 何时用 |
| --- | --- | --- | --- |
| 滑窗（drop-oldest） | 超出预算 | 零成本，丢失指代 | 默认档，配合"最后 1 轮强制保留" |
| 滚动摘要 | 消息条数 > 30 或 token > 预算 60% | 一次额外 LLM 调用（异步） | 长会话（客服、陪伴、编码助手） |
| 长期事实抽取 | 每 N 轮 / 会话结束 | 一次抽取调用 + 写库 | 需要跨会话记住用户偏好 |

> ⚠️ 摘要**必须异步**且**用 CAS 写回**：两个副本同时给对方压摘要，后写的会覆盖前写的，用户会感觉"记忆被重置"。

### 45.3.3 长期记忆：事实表 + 向量，而不是"再存一份聊天记录"

长期记忆的可用形态是**可更新的结构化事实**，不是更多文本：

```sql
CREATE TABLE memories (
  id           bigserial PRIMARY KEY,
  tenant_id    text NOT NULL,
  user_id      text NOT NULL,
  subject      text NOT NULL,        -- "user" / "订单 A123"
  predicate    text NOT NULL,        -- "prefers_color" / "budget"
  object       text NOT NULL,        -- "蓝色"
  confidence   real NOT NULL DEFAULT 0.8,
  source_msg   text,                 -- 溯源：来自哪条消息，评估可查
  superseded_by bigint,              -- 新事实覆盖旧事实（软失效，不物理删）
  created_at   timestamptz NOT NULL DEFAULT now(),
  embedding    vector(1024),
  UNIQUE (tenant_id, user_id, subject, predicate, object)
);
```

写入规则（这就是"记忆"和"日志"的区别）：**同 `subject+predicate` 出现新 `object` 时，把旧的 `superseded_by` 指向新的**，检索只取 `superseded_by IS NULL`。用户改主意时你不用删数据，历史可审计、可回滚。

### 45.3.4 Redis 会话存储：最小可跑实现

设计先定 key（**注意 `{sid}` 花括号：Redis Cluster 下只有同一 hash slot 的 key 才能进一个 Lua 脚本**）：

```
agent:sess:{sid}:msgs   List   ← 消息 JSON，最新在尾部
agent:sess:{sid}:seen   Set    ← 已写入的消息 ID（幂等去重）
agent:sess:{sid}:meta   Hash   ← version / summary / user_id / updated_at
TTL: 7 天，每次写刷新
```

```go
package session

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
)

// 原子：幂等判重 + 追加 + 裁剪到 maxLen + 刷新 TTL
var luaAppend = redis.NewScript(`
if redis.call('SISMEMBER', KEYS[2], ARGV[1]) == 1 then
  return redis.call('LLEN', KEYS[1])   -- 已写过，直接返回，幂等
end
redis.call('SADD', KEYS[2], ARGV[1])
redis.call('RPUSH', KEYS[1], ARGV[2])
redis.call('LTRIM', KEYS[1], -tonumber(ARGV[3]), -1)
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[4]))
redis.call('EXPIRE', KEYS[2], tonumber(ARGV[4]))
redis.call('EXPIRE', KEYS[3], tonumber(ARGV[4]))
return redis.call('LLEN', KEYS[1])
`)

// 原子 CAS：只有版本一致才更新摘要（后台 job 用，防多副本互相覆盖）
var luaCAS = redis.NewScript(`
if redis.call('HGET', KEYS[1], 'version') ~= ARGV[1] then return 0 end
redis.call('HSET', KEYS[1], 'version', ARGV[2], 'summary', ARGV[3])
return 1
`)

type RedisStore struct {
	rdb *redis.Client
	ttl time.Duration
}

func New(rdb *redis.Client) *RedisStore { return &RedisStore{rdb: rdb, ttl: 7 * 24 * time.Hour} }

func key(sid, part string) string { return fmt.Sprintf("agent:sess:{%s}:%s", sid, part) }

func (s *RedisStore) Append(ctx context.Context, sid string, msgs ...Message) error {
	keys := []string{key(sid, "msgs"), key(sid, "seen"), key(sid, "meta")}
	for _, m := range msgs {
		b, err := json.Marshal(m)
		if err != nil {
			return err
		}
		if err := luaAppend.Run(ctx, s.rdb, keys,
			m.ID, b, 500, int(s.ttl.Seconds())).Err(); err != nil {
			return fmt.Errorf("append %s: %w", m.ID, err)
		}
	}
	return nil
}

// Load 只取尾部 limit 条：千万不要 LRANGE 0 -1 全量拉回来再在 Go 里裁
func (s *RedisStore) Load(ctx context.Context, sid string, limit int) ([]Message, error) {
	raw, err := s.rdb.LRange(ctx, key(sid, "msgs"), int64(-limit), -1).Result()
	if err != nil {
		return nil, err
	}
	out := make([]Message, 0, len(raw))
	for _, r := range raw {
		var m Message
		if err := json.Unmarshal([]byte(r), &m); err != nil {
			return nil, fmt.Errorf("bad message json: %w", err)
		}
		out = append(out, m)
	}
	return out, nil
}

func (s *RedisStore) UpdateSummary(ctx context.Context, sid string, from, to int64, summary string) (bool, error) {
	n, err := luaCAS.Run(ctx, s.rdb, []string{key(sid, "meta")}, from, to, summary).Int()
	return n == 1, err
}
```

> ⚠️ `redis.Nil`：`Get` 一个不存在的 key 返回 `redis.Nil` 错误，**它不是故障**。用 `errors.Is(err, redis.Nil)` 判空会话，不要当 500 上报。

### 45.3.5 并发安全与多实例一致性：这才是真正难的部分

三条独立的竞争，必须分别解决：

| 竞争 | 场景 | 症状 | 解法 |
| --- | --- | --- | --- |
| 同会话并发追加 | 用户双击发送 / 前端重试 / 双标签页 | 顺序错乱、重复消息 | Lua 原子 RPUSH + ID 去重（45.3.4） |
| 读-改-写覆盖 | 两个副本同时"读历史→生成→写摘要" | 摘要被覆盖、记忆"回退" | 版本 CAS（`luaCAS`）或 PG 行锁 `SELECT ... FOR UPDATE` |
| 跨副本写冲突 | 同一会话被负载均衡到两个 Pod | 一方写的东西另一方看不见 | **共享存储 + 单写者**，别用进程内 `map` |

工业级的四条纪律：

1. **会话写路径只允许 append，不允许 update 单条消息**。追加天然无冲突；修改历史消息需要一个"变更事件"，用序号（`seq`）保证幂等。
2. **同一会话的生成串行化**：用 Redis 分布式锁（`SET key val NX PX 15000`，删锁用 Lua 比对 val）或按 `session_id` 做一致性哈希路由到固定副本。锁的价值不是性能，是防止两条流式回答交错写进同一个历史。
3. **不要把 `map` 当会话存储**，除非你真的只有单副本 + 无状态丢失容忍。`sync.Map` 只解决并发安全，不解决"进程挂了数据没了"。
4. **PG 做权威、Redis 做加速**：Postgres 表是 source of truth（可审计、可回溯），Redis 是热缓存。两者用同一条消息 `ID` 对齐，Redis miss 就回填。

> 🩸 **事故复盘**：某团队用 `WATCH/MULTI` 做会话 CAS，在 QPS 3000 时 CPU 飙到 90%。原因：`WATCH` 冲突重试风暴。换成一个 Lua 脚本（判断 + 写一次原子完成）后降了 60%。**Redis 上"能用一个脚本解决的事，不要用事务重试"。**

### 45.3.6 TTL 与清理

- Redis：**每次写都刷新 TTL**（`EXPIRE`），别只在创建时设一次——否则活跃会话会在第 7 天突然消失。
- PG：软删 + 分区表（按月/按租户）+ 定时任务物理清理；清理要按 `updated_at < now() - interval '90 days'`，并**先删向量再删行**（避免孤儿索引）。
- 长期记忆**不设会话级 TTL**：它跟随用户，跟随的是合规保留期（数据合规见第 47 章）。
- 注意 Redis 的过期是惰性 + 定期抽样删除：**不能拿 TTL 当"数据一定已删除"的合规承诺**。

### 45.3.7 幂等写入

```go
// Handler 层：request_id 来自前端；tool 内部产生的消息用 稳定 id（如 tool_call_id + 序号）
func (h *Handler) Chat(ctx context.Context, req ChatReq) (ChatResp, error) {
	// 1) 用 SETNX 抢占幂等键，防同一请求被网关重放两次
	ok, err := h.rdb.SetNX(ctx, "agent:idem:"+req.RequestID, "1", 10*time.Minute).Result()
	if err != nil {
		return ChatResp{}, err
	}
	if !ok {
		return h.waitResult(ctx, req.RequestID) // 直接取第一次的结果，不要重跑 LLM
	}
	// 2) 写消息（内部 Lua 再用 msg.ID 二次去重，双保险）
	// 3) 生成 → 落库 → 存结果
	return h.doChat(ctx, req)
}
```

幂等的三条"必须"：**消息 ID 由客户端/服务端在重试前就确定**（不是每次生成新 UUID）；**副作用（下单、发邮件）要有独立幂等键**（第 41 章「工具幂等」）；**流式响应重放时从头播放已存的分片，而不是重新生成**。

### 45.3.8 与 Java 侧 session / 缓存体系对接

现实里你旁边总有一个 Spring Boot 服务。三种接法，按风险从低到高：

| 接法 | 做法 | 优点 | 坑 |
| --- | --- | --- | --- |
| 共用身份，独立会话 | Java 侧签 JWT（或共享 `SESSION` cookie 值），Go 侧校验后建自己的会话 key | Go 完全自由演进 | 退出登录要能感知（JWT 黑名单 / 短 TTL） |
| 读 Spring Session 的 Redis | key 是 `spring:session:sessions:<id>`，字段在 hash 的 `sessionAttr:*` | 真正的会话共享 | **值默认是 JDK 序列化二进制**，Go 解码极痛；可让 Java 侧改成 `GenericJackson2JsonRedisSerializer` |
| 会话桥接服务 | Go 通过 Java 提供的内部 API 读写会话 | 零耦合 | 多一跳延迟，多一个故障点 |

> 💡 强烈建议：**新系统的会话是"业务会话"，不是 Servlet 的 `HttpSession`**。前者是你在数据库里定义的、可审计、可回放、可后台运营的实体；后者是容器给你的、带锁的、序列化黑洞。不要复刻 `HttpSession` 的语义（尤其是它的 `setAttribute` 脏检查与整对象序列化）。

---

## 45.4 Java 对照

| 你的 Java 直觉 | Go 的现实 | 会猜错的地方 |
| --- | --- | --- |
| `HttpSession` 容器托管、`session.setAttribute` 自动持久化 | 自己定义 `Store` 接口，显式 `Append/Load` | Go 没有容器，"自动持久化"这件事必须写出来；好处是脏检查、序列化这些隐性成本消失了 |
| `ConcurrentHashMap` 当本地缓存 | `map` + `sync.RWMutex`，或 `sync.Map`（只适合读多写少） | Go 的 map **并发写直接 fatal**（不是抛异常）；`sync.Map` 不给 TTL，需要自己搭 |
| `ThreadLocal` 存用户上下文 | `ctx context.Context` 显式传参 | ctx 是**参数**不是全局环境；存值只放请求作用域的标量（第 24 章） |
| `@Transactional` 保证"写会话 + 写业务"一致 | 显式事务；跨 Redis/PG 需要 outbox 或最终一致 | **Redis 和 PG 之间没有分布式事务**，这是设计选择而非缺陷 |
| Redisson 的 `RLock` | go-redis + `SET NX PX` + Lua 解锁 | 手写锁要处理"锁过期了别人拿到、自己还在写"（fencing token） |
| JDK 序列化 / Kryo | JSON（可读、可跨语言）或 protobuf（省空间） | Go 无法解 Java 的 JDK 序列化；跨语言一律 JSON/protobuf |

---

## 45.5 框架与工程落地

- **langchaingo `memory`**：`schema.Memory` 接口只有五个方法——`GetMemoryKey` / `MemoryVariables` / `LoadMemoryVariables` / `SaveContext` / `Clear`（接口签名以你本地版本为准）。内置 `memory.NewConversationBuffer()`（全量）、`memory.NewConversationWindowBuffer(k)`（滑窗），它们只解决"存取消息"，**不解决 TTL、多实例一致性、token 预算**——生产上你几乎一定要自己实现 `schema.Memory`，把 Redis/PG 塞进去。
- **eino**：把会话状态做成了显式的 state / checkpoint 概念，配合图编排（第 49 章）可以在节点间传递状态并做中断恢复，比 langchaingo 的 `Memory` 更适合长流程。
- **go-redis 的用法纪律**：`redis.Options` 里设 `PoolSize`（并发上限）、`ReadTimeout`/`WriteTimeout`；批量用 `Pipeline()`、跨 key 原子用 `TxPipelined`；**不要在 `MULTI` 事务里放超过 20 个命令**（会阻塞主线程）。
- **可观测**：每条消息写入打一个 span（第 35 章），把 `session_id`、`tenant_id` 放进属性；摘要 job 单独指标（触发率、失败率、压缩比）。

---

## 45.6 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| 用户第二条消息就失忆 | 只发最后一条消息 | 装载窗口（滑窗/摘要）+ 强制保留最近 1 轮 | 45.3.2 |
| 400：tool 消息没有对应的 tool_calls | 裁剪把 assistant 调用删了 | 裁剪以"块"为单位，tool 连同发起方一起保留 | 45.3.2 |
| 偶发 400 token 超限 | 用字符数估 token；没预留输出 | 写入时算 token，预算里留 10~20% 输出 | 45.3.2 |
| 重复消息 / 回答两次 | 客户端重试 + 无幂等键 | `msg.ID` + Lua `SISMEMBER` + `SETNX` request_id | 45.3.7 |
| 摘要莫名回退 | 多副本并发写 summary | 版本 CAS 或分布式锁 | 45.3.5 |
| 扩容后会话丢失 | 会话存在进程内 map | 共享存储（Redis/PG），进程内只做 L1 | 45.3.5 |
| Redis Cluster 报 CROSSSLOT | Lua 里多个 key 落在不同 slot | key 加 hash tag `{sid}` | 45.3.4 |
| 活跃会话第 7 天集体消失 | TTL 只在创建时设置 | 每次写都 `EXPIRE` 刷新 | 45.3.6 |
| `LRANGE 0 -1` 拉 5000 条 | 全量读再裁 | `LRANGE -N -1`，服务端只取尾部 | 45.3.4 |
| Go 读不到 Spring Session | JDK 序列化二进制 | 让 Java 侧改 JSON 序列化，或改用 JWT | 45.3.8 |

---

## 45.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 记忆分层 | 工作（请求内）/ 短期（Redis）/ 摘要（物化视图）/ 长期（事实 + 向量） |
| 裁剪顺序 | 保 system、保最近、保工具配对、丢中间 |
| token 预算 | 写入时就算；输出预留 10~20% |
| 摘要 | 异步 + CAS 写回，别阻塞主链路 |
| 长期记忆 | 存事实（SPO）不存文本；用 `superseded_by` 做软失效 |
| Redis 结构 | `{sid}` hash tag + List(msgs) + Set(seen) + Hash(meta) |
| 原子性 | 判重+追加+裁剪+TTL 一个 Lua 脚本搞定 |
| 一致性 | 共享存储 + 单写者 + 版本 CAS；禁用进程内 map |
| TTL | 每次写刷新；不能当合规删除承诺 |
| 幂等 | 消息 ID 早在重试之前确定；request_id SETNX |
| Java 对接 | 别复刻 `HttpSession`；JWT 或 JSON 序列化的共享会话 |

---

## 45.8 自检题

1. **为什么"裁剪历史"时必须以"块"（assistant 工具调用 + tool 结果）为单位？**
   > 要点：多数供应商要求 `role=tool` 必须紧跟对应的 `tool_calls`，孤立 tool 消息会 400；块级裁剪还保证语义完整（模型不会看到"结果"却看不到"调用"），避免模型误判工具又跑一遍。

2. **两个副本同时为同一会话做"读历史 → 生成摘要 → 写回"，会出现什么问题？你怎么办？**
   > 要点：后写覆盖前写，摘要里丢掉一段记忆（用户感觉"失忆"）。方案：① 按 session 一致性哈希固定副本；② `meta.version` 读时带出，写回用 CAS（Lua 或 `UPDATE ... WHERE version = $old`）；③ 加短租期分布式锁，锁只保护"生成+写回"这段。

3. **为什么 Redis 会话 key 要写成 `agent:sess:{sid}:msgs` 这种带花括号的形式？**
   > 要点：Redis Cluster 按 key 做 CRC16 分 slot；Lua 脚本要求所有 key 在同一 slot。花括号里的内容就是 hash tag，`{sid}:msgs` / `{sid}:seen` / `{sid}:meta` 会被算到同一 slot，脚本才能原子执行。

---

## 45.9 延伸阅读

- `github.com/redis/go-redis/v9`：`redis.NewScript`、`Client.Watch`、`Pipeliner` 的文档与 `example_test.go`
- langchaingo `memory` 包与 `schema.Memory` 接口（`memory/conversation_buffer.go`、`memory/conversation_window_buffer.go`）
- Redis 官方文档：transactions vs Lua scripting、keyspace notifications、`EXPIRE` 的惰性删除语义
- Spring Session 文档中 Redis 序列化配置一节（理解为什么 Go 读不出来）
- 上一章：[44. RAG 管线：从文档到上下文](44-rag-pipeline.md) ｜ 下一章：[46. 多 Agent 协作与持久化执行](46-multi-agent-durable.md)

---

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[44. RAG 管线：从文档到上下文](44-rag-pipeline.md)；第 24 章（context）、第 22 章（sync 原语）
- 下一章：[46. 多 Agent 协作与持久化执行](46-multi-agent-durable.md)
