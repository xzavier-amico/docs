# 32. 高并发高性能实战：从 1k 到 100k 连接

> **状态**：`已完成（初稿）` ｜ **所属**：第五部分 · 运行时与性能 ｜ **编号**：32 / 54
> **本章定位**：第五部分的收口章。前面五章讲的是「单个程序在机器上怎么跑」，这一章讲「一万个连接同时打进来时，先死的是哪一层」。内容全部是账本和公式：fd 账、内存账、池子账、端口账、CPU 账、副本账。
> **读法**：32.2 的账本表先建立数量级直觉；32.3 按你正在踩的坑挑节读；32.9 的容量规划表可以直接贴进方案文档。

---

## 32.1 引子：从 1k 到 10k 只用了 3 分钟，从 10k 到 100k 用了三天

一个 Agent 网关（对外 SSE 长连接，对内调 LLM），压测到 1 万并发时一切正常。加到 5 万时开始出鬼：

- 客户端先炸：`dial tcp: too many open files`——压测机 `ulimit -n` 是 1024。
- 调大后服务端炸：`accept4: too many open files`。
- 再调大后不炸了，但 P99 从 30ms 涨到 2.4s，CPU 只有 40%，`gctrace` 里 GC CPU 占比 35%。
- 最后发现四个完全独立的原因叠在一起：**fd 上限**（每连接 1 个）、**每连接 8KB 缓冲 × 10 万 = 800MB**（把 GC 扫描量顶爆）、**上游 `http.Transport` 用了默认 `MaxIdleConnsPerHost=2`**（99% 的请求在重新建连）、**客户端短连接不 keep-alive 导致 TIME_WAIT 堆到 6 万**。

四个问题，没有一个是「Go 性能不行」。**全部是账没算。**

> 🎯 本章的立场：**高并发不是「写得更快」，是「把每一份资源都算清楚」**。Java 程序员习惯的思考单位是「线程数 = 并发上限」，而在 Go 里，**并发上限由 fd、内存、端口、上游池子、GC 吞吐这五本账里最小的那本决定**，跟 goroutine 数量几乎无关——你可以轻松起 100 万 goroutine，然后发现你连 1000 个连接都开不了，因为 `ulimit -n`。

---

## 32.2 直觉模型：五本账 + 一条定律

先记一条定律：**Little's Law** —— `并发数 L = 到达率 QPS × 平均逗留时间 W`。所有容量规划都是它的变形。

```
资源随连接数增长（100k 长连接，每连接 1 goroutine + 4KB 读缓冲 + 4KB 写缓冲）：

 fd          ──────────────────────────────  线性 1:1
 goroutine   ──────────────────────────────  线性 1:1（栈 2KB 起，按需涨到 8KB+）
 堆内存      ──────────────────────────────  线性（缓冲 + 请求上下文）
 GC 扫描量   ──────────────────────────────  线性，且直接抬高 mark assist
 CPU         ──╱‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾   只与「活跃比例」相关，长连接空闲时不耗 CPU
 端口(TIME_WAIT) ─────────────────────────     只在你主动 close 的那一侧才爆
```

| 规模 | fd | goroutine | 缓冲内存 | 栈内存 | 典型「先爆」的东西 |
| --- | --- | --- | --- | --- | --- |
| 1k | 1k | ~2k | 8MB | 4MB | 什么都没爆 |
| 10k | 10k | ~20k | 80MB | 40MB | 上游连接池（`MaxIdleConnsPerHost`） |
| 50k | 50k | ~100k | 400MB | 200MB | `ulimit -n`、GC assist、客户端 TIME_WAIT |
| 100k | 100k | ~200k | 800MB | 400MB | 内存 limit、`GOMEMLIMIT`、epoll 扫描、日志/打点 |

> 💡 一个反直觉的结论：**10 万长连接的瓶颈通常不是 CPU，而是「每连接的固定内存 × 10 万」**。这就是为什么 epoll 事件循环（Netty）在 10 万连接场景的内存账比 goroutine-per-conn 好看——它把「每连接一份状态机」压到几百字节。Go 的解法不是自研事件循环，而是：**把每连接的缓冲池化、把长连接业务的 goroutine 栈压小、把 SSE 的中间分配干掉**。

---

## 32.3 底层实现与实战手法

### 32.3.1 并发模型选型：三种模型与它们各自的崩溃点

| 模型 | 写法 | 何时用 | 崩溃点 |
| --- | --- | --- | --- |
| **goroutine-per-request** | `go handle(conn)`，`net/http` 默认 | 绝大多数业务（含 Agent、SSE） | 每连接内存线性增长；无背压时会雪崩式堆积（第 25 章） |
| **worker pool（有界并发）** | 固定 N 个 worker 消费任务 channel | 有明确吞吐上限的后台任务（embedding、索引） | 池大小设错：太小排队、太大把下游打死；队列无界 = 内存炸弹 |
| **事件循环（自研 epoll）** | 单线程 `epoll_wait` + 状态机 | 极高连接数、极低单连接成本的代理层 | 手写状态机 → 业务代码失去同步语义，`ctx`/panic/超时都要自己重造 |

**Go 的默认答案永远是第一种**，理由就是第 19 章讲过的：`netpoller` 让「阻塞写法」不占线程，goroutine 栈只有 2KB 起。**只有在压测证明「每连接内存」是瓶颈时**，才值得考虑把「连接层」换成事件循环（如做 TCP 代理），业务层仍然回到 goroutine。

Worker pool 的正确形态是有界 + 可拒绝：

```go
// 有界工作池 + 背压：队列满就快速失败，而不是无限堆积
type Pool struct {
	jobs chan func(context.Context)
	sem  *semaphore.Weighted
}

func NewPool(n, queue int) *Pool {
	return &Pool{
		jobs: make(chan func(context.Context), queue), // 有界！这是背压的物理载体
		sem:  semaphore.NewWeighted(int64(n)),
	}
}

func (p *Pool) Submit(ctx context.Context, job func(context.Context)) error {
	select {
	case p.jobs <- job:
		return nil
	default:
		return ErrOverloaded // 立刻告诉上游"别发了"，比排队 30 秒后超时好
	}
}
```

### 32.3.2 先爆什么：一张排查表 + 五条命令

| 症状 | 现场命令 | 结论与处置 |
| --- | --- | --- |
| `too many open files` | `cat /proc/$(pgrep -f gateway)/limits`、`cat /proc/sys/fs/file-nr` | 提 `ulimit -n`（K8s 用 `securityContext` 或 node 级 `fs.file-max`），按 **连接数 × 2** 留余量 |
| `ss -s` 里 `timewait` 几万 | `ss -tan state time-wait \| wc -l` | 谁先 close 谁承担 TIME_WAIT：让服务端 keep-alive 超时**大于**客户端；开启连接复用；必要时 `net.ipv4.tcp_tw_reuse=1`（只影响出站） |
| 出站连不上：`cannot assign requested address` | `cat /proc/sys/net/ipv4/ip_local_port_range` | 单机出站连接上限 ≈ 端口数（约 2.8 万）；需要多 IP / 多实例分摊 |
| CPU 不高但延迟高 | `go tool pprof .../goroutine?debug=1` + `trace` | goroutine 数量暴涨（每连接 2 个）+ 排队 → 上背压与限流 |
| P99 抖动 + `gctrace` assist 高 | `GODEBUG=gctrace=1`、`/debug/pprof/heap?sample_index=alloc_space` | 每连接缓冲是最大分配源 → 池化 + 降 `GOGC` 压力（第 28、30 章） |

### 32.3.3 连接池三件套：参数怎么推出来

这三个池子的参数不能拍脑袋，每一本都是不等式：

```
http.Transport（出站到 LLM / 内部服务）
  需要同时开的连接数 ≈ 峰值 QPS × 上游 P99
  ⇒ MaxIdleConnsPerHost ≥ 这个数（否则每次请求都重新 TCP+TLS 握手）
  ⇒ MaxIdleConns ≥ MaxIdleConnsPerHost × 目标主机数

database/sql
  单实例最大连接数 = 并发请求数 × 每请求 DB 操作数（串行的话就是 1）
  ⇒ max(sum(MaxOpenConns)) ≤ DB 的 max_connections × 0.8
  ⇒ SetMaxIdleConns ≈ SetMaxOpenConns（避免频繁建连）
  ⇒ SetConnMaxLifetime < 中间件/LB 的空闲超时（否则用到被掐死的连接）

go-redis
  PoolSize 默认 = 10 × GOMAXPROCS，高并发下通常是瓶颈
  ⇒ PoolSize ≈ 峰值并发命令数，且全部实例总和不超 redis maxclients
```

```go
// 出站 HTTP：默认值几乎一定是错的
tr := &http.Transport{
	MaxIdleConns:        2000,
	MaxIdleConnsPerHost: 500,             // 默认是 2！这是 Go 最经典的坑之一
	MaxConnsPerHost:     800,             // 有上限才有背压，0 = 无限制
	IdleConnTimeout:     90 * time.Second,
	TLSHandshakeTimeout: 5 * time.Second,
	ForceAttemptHTTP2:   true,
}
client := &http.Client{Transport: tr, Timeout: 0} // 超时交给 ctx（第 38 章）

// 数据库：三个 Set 都要写，不写等于没管
db.SetMaxOpenConns(50)
db.SetMaxIdleConns(50)
db.SetConnMaxLifetime(5 * time.Minute)
db.SetConnMaxIdleTime(2 * time.Minute) // 1.15+
```

> 🩸 **`MaxIdleConnsPerHost=2` 的杀伤力**：500 个并发请求打同一台上游，只有 2 个连接被复用，其余请求每次都要 TCP + TLS 握手（两次 RTT 起）。在跨机房场景下，这一项能把 P99 抬高 60% 而 CPU 毫无变化。

### 32.3.4 限流：单机令牌桶 + 分布式滑动窗口

**单机**用 `golang.org/x/time/rate`（令牌桶，带突发）：

```go
// 每个租户一个 limiter，兼顾租户隔离与全局保护
type LimiterSet struct {
	mu   sync.Mutex
	m    map[string]*rate.Limiter
	rps  rate.Limit
	brst int
}

func (s *LimiterSet) allow(tenant string) bool {
	s.mu.Lock()
	l, ok := s.m[tenant]
	if !ok {
		l = rate.NewLimiter(s.rps, s.brst)
		s.m[tenant] = l
	}
	s.mu.Unlock()
	return l.Allow() // 非阻塞；要排队就用 l.Wait(ctx)
}

func RateLimitMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !limiters.allow(r.Header.Get("X-Tenant")) {
			w.Header().Set("Retry-After", "1")
			http.Error(w, "rate limited", http.StatusTooManyRequests) // 429
			return
		}
		next.ServeHTTP(w, r)
	})
}
```

**多实例**必须用中心化计数，最常用的是 Redis + Lua（把「判断 + 计数 + 过期」做成一次原子操作，避免 read-modify-write 竞态）：

```go
// 滑动窗口限流：一次 Lua 调用完成，天然原子
var slidingWindow = redis.NewScript(`
local key    = KEYS[1]
local window = tonumber(ARGV[1])   -- 窗口毫秒数
local limit  = tonumber(ARGV[2])
local now    = tonumber(ARGV[3])
local member = ARGV[4]             -- 唯一成员（request id），避免 ZADD 覆盖
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local n = redis.call('ZCARD', key)
if n < limit then
  redis.call('ZADD', key, now, member)
  redis.call('PEXPIRE', key, window)
  return 1
end
return 0`)

func allowRedis(ctx context.Context, rdb *redis.Client, tenant string, limit int) (bool, error) {
	key := "rl:" + tenant + ":" + time.Now().Format("200601021504") // 分钟粒度分桶，控制 key 数
	res, err := slidingWindow.Run(ctx, rdb, []string{key},
		60_000, limit, time.Now().UnixMilli(), uuid.NewString()).Int()
	return res == 1, err
}
```

三层限流要分开设：**入口（按 IP/租户，粗粒度）→ 服务（按接口，中粒度）→ 依赖（按上游配额，细粒度）**。Agent 场景还要加一层「并发信号量」（因为 LLM 调用贵且慢，看的是**在途数**而不是 QPS）——那是第 47 章的事，这里只记住结论。

### 32.3.5 缓存三兄弟：穿透、击穿、雪崩

| 问题 | 场景 | 解法 |
| --- | --- | --- |
| **穿透** | 查一个根本不存在的 key，每次都打到 DB | 缓存空值（短 TTL）+ 布隆过滤器；参数合法性前置校验 |
| **击穿** | 热点 key 过期瞬间，上千请求同时回源 | **`singleflight`**（同 key 只放一个请求进去）+ 逻辑过期（不设 TTL，后台异步刷新） |
| **雪崩** | 大批 key 同时过期 / 缓存层整体不可用 | TTL 加随机抖动（`ttl ± rand(0, ttl/5)`）+ 多级缓存（本地 LRU + Redis）+ 熔断降级 |

```go
var sf singleflight.Group

func GetDoc(ctx context.Context, id string) (Doc, error) {
	if d, ok := localLRU.Get(id); ok { // L1：进程内
		return d, nil
	}
	v, err, _ := sf.Do(id, func() (any, error) { // 同 key 并发只回源一次
		if d, ok := redisGet(ctx, id); ok { // L2：Redis
			return d, nil
		}
		return dbGet(ctx, id) // L3：DB
	})
	if err != nil {
		return Doc{}, err
	}
	return v.(Doc), nil
}
```

### 32.3.6 零拷贝与 IO 批处理

Go 的 `io.Copy(dst, src)` 会自动选路：`src` 有 `WriteTo` 或 `dst` 有 `ReadFrom` 时走那条捷径。最值钱的一条是 **`*net.TCPConn.ReadFrom(*os.File)` → `sendfile(2)`**：文件内容全程在内核里搬运，不经过用户态。所以：

- **静态文件/日志下载/模型权重转发**：用 `io.Copy(c, f)`，**不要**自己写 4KB 循环——那会让 `sendfile` 失效，还多两次内存拷贝。
- **多块响应**：`net.Buffers` 走 `writev(2)`，一次 syscall 写多段，避免先 `concat` 再写。
- **`bufio.Writer` 默认 4KB**（`bufio.DefaultBufferSize`），小响应要显式 `Flush`；大流量场景把 buffer 调到 32~64KB 并按阈值 flush（第 30 章）。
- **反向代理**：`httputil.ReverseProxy` 内部用 `io.Copy` + `FlushInterval`，但**默认不刷 SSE**——必须设 `FlushInterval: -1` 或包一层 `http.Flusher`（第 39 章）。

### 32.3.7 压测方法论：别用一个客户端证明服务器能扛

```bash
# 恒定速率 + 直方图（适合验证 SLO 曲线，vegeta 是最趁手的）
echo "GET http://gw:8080/v1/chat" | vegeta attack \
  -rate=2000 -duration=60s -max-workers=500 -timeout=10s | \
  vegeta report -type='hist[0,10ms,50ms,100ms,250ms,1s]'

# 固定并发（适合找拐点，hey 更直观）
hey -z 60s -c 500 -q 20 -m POST -T 'application/json' \
  -body '{"q":"hi"}' http://gw:8080/v1/chat

# gRPC：ghz 直接读 proto，能测到 streaming
ghz --insecure -n 20000 -c 200 --proto api/chat.proto \
  --call chat.v1.ChatService/Chat -d '{"q":"hi"}' gw:9090

# k6：脚本化场景（混合读写、思考时间、长连接保持）
k6 run --vus 500 --duration 60s script.js
```

四条纪律：① **客户端与服务器分开机器**，否则你在测自己的 CPU；② **客户端也要调 `ulimit -n` 和端口范围**（10 万连接压测机自己先爆）；③ **长连接压测必须保持连接**（`hey`/`vegeta` 是短连接，会把 TIME_WAIT 打满，测出来的是端口耗尽而不是服务能力）；④ **同时看两侧指标**：客户端 P99 + 服务端 `/debug/pprof` + `ss -s` + 容器 CPU/内存，缺一个就会归因错误（第 29 章）。

### 32.3.8 容量规划：从 P99 反推副本数与 limit

```
① 单请求 CPU 时间 c（从 pprof/benchmark 拿，单位秒）
   所需核心数 = 峰值 QPS × c
② 副本数 N = ceil(所需核心数 / 单副本限额核数 / 目标利用率 0.6)
③ CPU limit = ceil(所需核心数 / N × 1.5)        // 1.5 倍余量吸收抖动与 GC
④ 并发数 L = 峰值 QPS × P99                     // Little's Law
   内存下限 = 基线 + L × 每请求内存（含缓冲）
⑤ memory limit = 内存下限 × 1.3；GOMEMLIMIT = limit × 0.85
```

| 指标 | 取值 | 推导 |
| --- | --- | --- |
| 峰值 QPS | 2000 | 业务侧给 |
| 单请求 CPU | 8ms | `pprof`：2000 × 0.008 = **16 核** |
| 单副本核数 | 8 核 | 16 / 8 / 0.6 → **N = 4 个副本** |
| CPU limit | 12 核 | 8 × 1.5；1.25 起 `GOMAXPROCS` 自动跟随 cgroup |
| 并发 L | 2000 × 0.15s = 300 | P99 = 150ms |
| 每请求内存 | 400KB | 缓冲 + 请求体 + 上下文 |
| memory limit | 700MiB | (300 × 400KB + 300MB 基线) × 1.3 |
| GOMEMLIMIT | 600MiB | limit × 0.85（第 28 章） |
| fd | ≥ 8192 | 连接数 × 2，含上游连接 |

---

## 32.4 Java 对照

| 主题 | Java 侧 | Go 侧 | 会咬人的差异 |
| --- | --- | --- | --- |
| 并发上限 | Tomcat `maxThreads`（默认 200）+ `acceptCount` | **没有这个概念**，唯一上限是 fd/内存/池 | 你会误以为「Go 天然无限并发」，其实只是把上限藏到了别处 |
| 10 万连接 | 平台线程 × 1MB 栈 = 不可能；要么 Netty，要么虚拟线程（Loom） | goroutine-per-conn，2KB 栈起 | Go 天生就是 Loom 的形态，但要自己管每连接内存 |
| 连接池 | HikariCP `maximumPoolSize`/`minimumIdle`/`connectionTimeout` | `database/sql` 的 `SetMaxOpenConns`/`SetMaxIdleConns`/`SetConnMaxLifetime` | 语义几乎一一对应，但 Go 默认值更"放任"（`MaxOpenConns` 默认 0 = 无限） |
| HTTP 客户端池 | Apache HttpClient `PoolingHttpClientConnectionManager` | `http.Transport` 的 `MaxIdleConnsPerHost` | **两边默认都是 2（每路由）**，两边都必须改，坑一模一样 |
| 限流 | Sentinel / Resilience4j（注解式、有控制台） | `x/time/rate` + Redis Lua（手写） | Go 没有全家桶，限流是「代码里的一段」，好处是透明 |
| 背压 | `ThreadPoolExecutor` 的有界队列 + 拒绝策略 | 有界 channel + `select/default` + `semaphore` | 概念一致；Go 更容易写出无界堆积（第 25 章） |
| 长连接内存 | Netty `ByteBuf` 池（PooledByteBufAllocator） | `sync.Pool` + `bufio` | Netty 的池更成熟；Go 要自己建池，别每连接 `make([]byte, 4096)` |

> 💡 一句话：**Go 把「并发上限」从「线程池大小」这个显式旋钮，变成了「五本账」这个隐式约束。** 从 Java 过来最危险的心态是「Go 不需要配并发数」——恰恰相反，你需要配的是 fd、池子、信号量、channel 容量这些更底层的东西。

---

## 32.5 框架与工程落地

- **监控每连接状态**：`http.Server.ConnState` 能精确统计 `StateNew/StateActive/StateIdle/StateClosed` 的计数，做一条「在线连接数」曲线比什么都直观：

```go
srv := &http.Server{Addr: ":8080", Handler: mux}
srv.ConnState = func(c net.Conn, s http.ConnState) {
	connGauge.WithLabelValues(s.String()).Inc() // 简化示意：真实实现要配 Dec 与标签清理
}
```

- **Gin / kratos 的限流中间件**：社区版基本都是「`x/time/rate` 或 Redis 脚本 + 中间件」的形态；kratos 有 `ratelimit` 中间件接口，Gin 则普遍用 `middleware` 生态（如 `uber-go/ratelimit` 的漏桶）。**别指望框架帮你算参数**。
- **fasthttp** 是「同样的连接数下内存更低」的极端样本：它把 `RequestCtx` 全池化，所以 100k 短连接压测下内存曲线明显更平——代价是所有 `[]byte` 只在 handler 期间有效（第 30 章）。**如果你的协议能接受这个约束，它值；否则别为了一点内存换来 use-after-free 风险。**
- **Agent 场景的特别注意**：SSE 长连接的「每连接成本」不只是 8KB 缓冲，还有**一个在途的 LLM 请求**（上游连接、重试状态、token 累积 buffer）。所以真正的容量公式是 `在线用户数 × 每用户内存`，而 `QPS × P99` 只是它的近似——这一点在第 39、47 章会再算一次。

---

## 32.6 踩坑清单（CR 可直接抄）

| 现象 | 原因 | 正确写法 | 详见 |
| --- | --- | --- | --- |
| 并发一高就大量建连，P99 飙升 | `MaxIdleConnsPerHost` 默认 2 | 按「峰值 QPS × 上游 P99」设，并同时设 `MaxConnsPerHost` | 32.3.3 |
| DB 连接数打满，应用侧超时 | `MaxOpenConns` 未设（默认无限）+ 副本数相乘超 DB 上限 | 显式设三个参数，并做「副本数 × MaxOpenConns ≤ DB 上限 × 0.8」的容量校验 | 32.3.3 |
| 跑一夜后 DB 报「连接被重置」 | `ConnMaxLifetime` 未设，用到被中间件掐死的连接 | 生命周期 **小于** LB/DB 的空闲超时 | 32.3.3 |
| `too many open files` | `ulimit -n` 太小 | 连接数 × 2 起，容器/节点都要设 | 32.3.2 |
| 高峰时内存爆、GC 狂跑 | 每连接缓冲独立分配，10 万 × 8KB | `sync.Pool` 复用 + 降缓冲大小 + `GOMEMLIMIT` | 第 28、30 章 |
| 队列越积越长最后 OOM | 无界 channel 当「异步化」 | 有界 + `select/default` 拒绝 + 明确 429 | 32.3.1 |
| 限流形同虚设 | 每个副本各自限流，总流量 × 副本数 | 全局配额走 Redis 原子脚本，本地只做兜底 | 32.3.4 |
| 压测显示能扛 10 万，上线只能扛 1 万 | 压测用短连接、同机、无 TLS、无上游延迟 | 客户端分开机器 + 真实 TLS + 注入上游延迟 | 32.3.7 |
| `io.Copy` 传文件时 CPU 很高 | 自己写了 4KB 循环，破坏了 `sendfile` | 直接 `io.Copy`，或让 `dst` 实现 `ReaderFrom` | 32.3.6 |
| 容量规划只按 QPS 算 | 忽略了 Little's Law 的并发与每连接内存 | 用 32.3.8 的五步公式，从 P99 反推 | 32.3.8 |

---

## 32.7 一页速记

| 关键词 | 一句话结论 |
| --- | --- |
| 并发上限 | 由 fd / 内存 / 端口 / 上游池 / GC 吞吐 五本账里最小的一本决定 |
| Little's Law | `并发 = QPS × 延迟`，所有容量规划的根 |
| 每连接成本 | 1 fd + 1 goroutine（2KB 栈起）+ 4~8KB 缓冲 |
| 默认并发模型 | goroutine-per-request（netpoller 兜住阻塞），只在连接层考虑事件循环 |
| 池子三件套 | `Transport`（`MaxIdleConnsPerHost` 别用 2）、`database/sql`（三个 Set）、redis `PoolSize` |
| 背压 | 有界 channel + `select/default` + `semaphore`；无界队列 = 未来的 OOM |
| 限流 | 单机 `x/time/rate` 令牌桶；分布式 Redis + Lua 原子脚本 |
| 缓存三兄弟 | 穿透→空值/布隆；击穿→`singleflight`；雪崩→TTL 抖动 + 多级 |
| 零拷贝 | `io.Copy` 自动走 `sendfile`/`writev`，别手写循环 |
| 压测 | 客户端分机 + 客户端也调 `ulimit` + 长连接要复用 + 两侧指标同看 |
| 容量公式 | `核数 = QPS × 单请求CPU`；`内存 = 基线 + QPS×P99×每请求内存` |

## 32.8 自检题

1. **服务能起 100 万 goroutine，但压测到 5 万连接就报 `accept4: too many open files`。矛盾的根源是什么？**
   > 要点：goroutine 是运行时对象（2KB 栈，用户态），fd 是 OS 资源（`ulimit -n` / `fs.file-max`），两者完全解耦。Go 让「并发任务」免费，但「每个连接」仍要一个 fd。处置：按「连接数 × 2」提 `ulimit -n`，同时算清每连接的内存账，否则下一步爆的就是内存而不是 fd。

2. **`http.Transport` 用默认配置调一个内部服务，QPS 500、上游 P99 50ms，为什么延迟很高而 CPU 不高？怎么算？**
   > 要点：`MaxIdleConnsPerHost=2` 只允许 2 条连接复用，其余请求每次走 TCP+TLS 握手（至少 2~3 个 RTT）。所需复用连接数 ≈ 500 × 0.05 = 25 条（Little's Law），默认的 2 条意味着绝大多数请求在排队/握手。修：`MaxIdleConnsPerHost ≥ 25`（留余量取 100+）、`MaxConnsPerHost` 给上限做背压。

3. **压测报告写「单副本 8000 QPS，上线 4 个副本能扛 32000」。这个结论至少漏了什么？**
   > 要点：① 下游承载力没算——DB/Redis/LLM 的配额是**共享的**，副本数乘上去会打爆下游；② 压测的流量结构不同（短连接 vs 长连接、无 TLS、无真实上游延迟）；③ GC 与内存随连接数非线性变化，4 副本不代表 4 倍线性；④ 要按 **P99 目标**而不是平均值定容量（`核数 = QPS × 单请求CPU` 要乘上 0.6 利用率与 1.5 倍抖动余量）。

## 32.9 延伸阅读

- `$GOROOT/src/net/http/transport.go`（`MaxIdleConnsPerHost`、`DefaultMaxIdleConnsPerHost`、`MaxConnsPerHost`）、`$GOROOT/src/database/sql/sql.go`（`DB.SetMaxOpenConns` 等）
- `$GOROOT/src/net/sendfile_unix.go`、`$GOROOT/src/net/net.go`（`Buffers`/`WriteBuffers` 与 `writev`）、`$GOROOT/src/io/io.go`（`Copy` 的分派）
- `pkg.go.dev/golang.org/x/time/rate`、`golang.org/x/sync/singleflight`、`golang.org/x/sync/semaphore`
- vegeta（`tsenart/vegeta`）、hey（`rakyll/hey`）、ghz（`bojand/ghz`）、k6（`grafana/k6`）的 README
- 官方博客 *Go 1.25: container-aware GOMAXPROCS*；`fasthttp` 的 benchmark 与「ctx 只在 handler 期间有效」说明

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：[第 31 章 JSON 编解码](31-json-codec.md)
- 相关：netpoller 与非阻塞 IO 的运行时视角见[第 19 章](19-scheduler-runtime.md)；goroutine 泄漏与背压陷阱见[第 25 章](25-concurrency-pitfalls.md)；有界并发与 `errgroup`/`semaphore` 见[第 26 章](26-structured-concurrency.md)；`net/http` 超时四件套见[第 38 章](38-net-http.md)；SSE 长连接见[第 39 章](39-realtime-sse-websocket.md)；配额与成本见[第 47 章](47-agent-productionization.md)；K8s 资源与 SRE 见[第 52 章](52-deployment-sre.md)
