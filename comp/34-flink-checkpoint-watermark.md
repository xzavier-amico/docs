# 34. Flink：checkpoint、watermark、状态后端与背压

> **状态**：`已完成（初稿）` ｜ **所属**：第六部分 · 消息、流与定时 ｜ **编号**：34 / 53
> **本篇定位**：把流处理拆到**快照与时间线**这一层：一致性 checkpoint 怎样恢复状态，对齐与非对齐各把代价放在哪里，watermark 怎样决定事件时间窗口何时出结果。Kafka 复制见 [comp/29](29-kafka-replication-isr.md)，端到端投递边界见 [eng/24](../eng/24-delivery-semantics.md)。
> **读法**：救火先读 §34.1 的三起事故（checkpoint 回滚循环、窗口永不触发、反压卡死对齐），再直接翻 §34.4 的 checkpoint 失败语义表、§34.5 的停滞三因表、§34.6 的「加内存为什么无效」表与 §34.10 的坑清单；做架构决策读 §34.4 的状态后端决策表与 §34.7 的 sink 语义决策表；要出容量与 SLO 方案直接抄 §34.8 的三本账（全部带乘法链、假设清单与量级结论标记）。只想知道结论的读 §34.12。

---

## 34.1 引子：三起「作业是 RUNNING 的，数字是错的 / 不动的 / 迟到的」

Flink 的故障有一个统一形状：**Web UI 上作业是绿色的，`numRecordsInPerSecond` 在跳，没有任何异常栈**。它表现成「checkpoint 老是失败重启」「看板数字不动」「Kafka 积压突然涨」，而根因往往在三个月前定下的一个数：状态有多大、watermark 策略怎么写的、`tolerable-failed-checkpoints` 用的默认值。

### 事故一：checkpoint 超时 → failover → 从两小时前的点回滚 → 再超时（2026-03-11）

某风控特征作业：Kafka `risk.feature.raw` 96 分区 → 去重 + 30 天滚动特征聚合 → Redis。RocksDB 后端，键控状态 **380 GB**，120 个 subtask（24 TaskManager × 5 slot），checkpoint 落 S3 同 region 桶。配置是团队「照着博客抄的」：`execution.checkpointing.interval = 180s`、`timeout = 10min`、`max-concurrent-checkpoints = 1`、`tolerable-failed-checkpoints` 没设（**默认 0**）。

上线时状态 40 GB，一次 checkpoint 2 分 50 秒，一切正常；三个月后特征窗口从 7 天拉到 30 天，状态涨到 380 GB，但没人重算过这个 10 分钟还够不够。

| 时刻 | 事件 | 单次 checkpoint 时长 | 状态 | 作业状态 |
| --- | --- | --- | --- | --- |
| 03-10 之前 | 稳态：状态 380 GB，S3 独占带宽 | 2 min 50 s | 380 GB | RUNNING |
| 03-11 02:00 | 同 region 另一个团队开始每日全量归档，共享同一出口带宽 | 4 min 10 s | 380 GB | RUNNING |
| 03-11 02:40 | 归档任务进入峰值，聚合上传带宽 2.4 GB/s → 400 MB/s | 6 min 40 s | 380 GB | RUNNING |
| 03-11 03:05 | **11 min 20 s > timeout 10 min** → `Checkpoint expired before completing` | 超时 | 380 GB | 失败计数 +1 |
| 03-11 03:05 | `tolerable-failed-checkpoints=0` ⇒ **触发 failover**，从最近一次**成功**的 checkpoint（02:48）恢复 | — | — | RESTARTING |
| 03-11 03:06–03:15 | 下载 380 GB 快照 + RocksDB 打开 + 索引重建 | — | — | RESTORING |
| 03-11 03:15–03:33 | 重放 02:48 之后的 17 分钟数据（约 6,100 万条），source 全速读又把带宽打满 | — | — | RUNNING |
| 03-11 03:46 | 恢复后第一次 checkpoint 又超时 → 第二轮 failover，如此循环 | 超时 | 380 GB | 循环 |
| 03-11 05:40 | 6 轮之后人工把 `interval` 改 600 s、`timeout` 改 30 min、`tolerable-failed-checkpoints` 改 3 | 9 min 40 s | 380 GB | RUNNING |

代价：**2 小时 35 分钟无输出**（下游风控特征表停更，该时段所有请求走降级规则），6 轮共重放约 3.7 亿条、额外读 Kafka 约 400 GB，赔付与人工对账合计 **38 万元**。

这一起的教训不是「timeout 设小了」，而是：**checkpoint 失败在 `tolerable-failed-checkpoints=0` 下不是「等下一次」，它是一个 failover 触发器；而 failover 的代价是「回到上一个成功点」，不是「从头」——状态越大，恢复越慢，越赶不上下一个 checkpoint 窗口，于是自激成环。** 这个环里唯一的正反馈项是「状态字节 ÷ 可用上传带宽」，而它在事故当时正在被另一个团队吃掉。

### 事故二：watermark 停在一个空闲分区上，5 分钟窗口 7 小时一次没触发（2026-02-05）

某 IoT 平台：Kafka `device.telemetry` 48 分区（按省市哈希），Flink 48 并行度，`WatermarkStrategy.forBoundedOutOfOrderness(Duration.ofMinutes(2))`，`TumblingEventTimeWindows.of(Time.minutes(5))` 算「在线设备数」，结果写 Redis 给看板。

22:03 之后，partition 31（某西部省份，夜间设备全部休眠）再无新数据。它的 watermark 停在 22:03。而全局 watermark 是 **48 个分区 watermark 的最小值**，于是整条链路的时间线被这一个空分区钉死在 22:03。

| 时刻 | 事件 | Kafka lag | checkpoint | 看板 |
| --- | --- | --- | --- | --- |
| 02-04 22:03 | partition 31 最后一条记录（该省设备全部休眠） | 0 | 全绿 | 正常 |
| 02-04 22:05 | 窗口 `[22:00, 22:05)` 的 watermark 条件永不满足 | 0 | 全绿 | **停在 22:05 的数字** |
| 02-05 02:20 | 值班排查：lag=0、作业 RUNNING、checkpoint 全绿、无异常日志 | 0 | 全绿 | 不动 |
| 02-05 03:00 | 从 REST API 逐 subtask 拉 watermark，发现 source 的 48 个 channel 里 47 个已到 05:00、1 个停在 22:03 | 0 | 全绿 | 不动 |
| 02-05 04:30 | 定位到 `withIdleTimeout` 未配置（改代码 + 走发布流程） | 0 | 全绿 | 不动 |
| 02-05 05:10 | 发布：`.withIdleTimeout(Duration.ofMinutes(5))` | 0 | 全绿 | 一次性补触发 84 个窗口 |
| 02-05 05:12 | 84 个窗口 × 48 并行度的结果同时写 Redis，写入 QPS 打满 → 看板接口超时 | 0 | 全绿 | 二次故障 20 分钟 |

代价：看板 **7 小时**不更新（业务方按旧数字做了两次投放决策），补触发时把 Redis 打满 20 分钟。**这起事故最贵的一点是「所有监控都是绿的」**：watermark 停滞不影响 checkpoint，不影响 lag，不影响吞吐——它只让「时间」这条线停下，而时间停了不会报错，只会让窗口永远不满足条件。

### 事故三：下游 ClickHouse 变慢，背压吃掉 credit，checkpoint 对齐从 0.4 s 涨到 9 min（2026-04-02）

某 CDC 入湖作业：MySQL binlog → Flink（去重 + 打宽）→ ClickHouse。峰值 12 万条/s，TaskManager 8 GB × 16 台（`taskmanager.memory.network.fraction` 用默认 0.1 ⇒ 每台 network 约 800 MB），`execution.buffer-timeout` 用默认 100 ms。

| 时刻 | 事件 | 反压 `backPressuredTimeMsPerSecond` | checkpoint 对齐 | Kafka lag |
| --- | --- | --- | --- | --- |
| 14:00 | 稳态 | 120 ms/s（12%） | 0.4 s | 2 min |
| 14:20 | ClickHouse 一个 merge 高峰 + 一条慢查询，写入 P99 90 ms → 1.8 s | 480 ms/s | 1.2 s | 4 min |
| 14:24 | sink 的异步批量请求积压 → 输出 buffer 满 → credit 归零 → 反压逐级上传到 source | 940 ms/s | 3 min | 40 min |
| 14:26 | source 每批 `poll` 从 500 条掉到 30 条（被 mailbox 阻塞） | 990 ms/s | 9 min | 1 h 50 min |
| 14:35 | checkpoint 超时（10 min）→ `tolerable=0` → failover → 从 14:15 恢复 + 重放 20 min | — | — | 2 h 10 min |
| 14:50 | 值班判断「内存不够」，TM 8 GB → 16 GB（network 800 MB → 1.6 GB，只把「能吸多少」翻倍） | 990 ms/s | 8 min | 3 h 30 min |
| 15:20 | 杀掉 ClickHouse 上的大查询 + 调小 sink 批量 + 把 `tolerable-failed-checkpoints` 改 3 | 300 ms/s | 40 s | 1 h |
| 15:35 | 追平 | 80 ms/s | 1 s | 0 |

代价：81 分钟入湖延迟（下游看板与对账全部延后），Kafka 堆积峰值 4.7 亿条 ≈ 376 GB（保留 3 天，没炸但离炸不远），两次无效扩容。**「加内存」在这里一秒都没用**：反压的本质是「下游吞吐 < 上游吞吐」，加缓冲只改变「堆在哪里」，不改变「堆不堆」；而它同时把故障从「反压」变成了「更大的反压 + 更长的 GC」。

> 🩸 **三起事故的共同点**：没有一处业务代码写错，没有一条异常日志。① 是「状态长得比 timeout 快」；② 是「时间线被一个空分区钉死，而停顿不报错」；③ 是「吞吐守恒，缓冲不是产能」。三件事全都发生在**配置与语义的接缝**上，所以都不在 code review 里，也都不在单测里。

---

## 34.2 一句话定位与唯一真问题

**定位**：Flink 是一个**带本地状态的流式执行引擎**。它的正确性由三件事共同兑现：**算子本地维护键控状态**（不是每条记录都去查外部存储）、**周期性把「所有算子的状态 + 输入的读取位点」做成一个一致的分布式快照**（checkpoint）、**用 watermark 在事件时间轴上划一条「这条线之前的记录都已到齐」的下界**。它不是「更快的 Spark」，也不是「Kafka 的消费端」——它是一个把「一致性」换成「快照 + 重放」的运行时。

### 唯一真问题

> **一致性恢复靠状态与输入进度处在同一个一致性切面；对齐和记录在途数据是两种构造方式。** watermark 则表达事件时间推进的估计，影响窗口延迟和迟到处理；这与是否能一致地恢复是两件事。

四条推论，全篇都在给它们找证据：

1. **状态是本地内存/本地盘里的东西，不是数据库里的东西。** 所以必须被快照，而快照的代价由「key 数 × 每 key 状态」直接决定，与「业务多复杂」无关。
2. **checkpoint 是一个一致性协议，不是定时任务。** 对齐快照可能在 barrier 等待阶段超时；非对齐快照会记录在途数据，仍可能因上传或其他阶段超过限时而失败。失败是否导致重启要看失败种类和容忍设置。
3. **watermark 是 min，不是 max。** 全局事件时间由最慢的输入决定，**一个空闲分区就能让整条链路的时间停住**，而且不报错。
4. **反压是吞吐守恒的表现，不是内存不足。** buffer 只决定「能在哪里堆多久」，而反压会直接卡住 checkpoint 的对齐阶段（§34.6 的核心因果）。

**它明确不承诺什么**：不承诺「作业不重启就不丢」（重启即回退到上一个成功点）、不承诺「at-least-once 的 sink 也能端到端恰好一次」（那是 sink 的事，§34.7）、不承诺「watermark 越晚越准」（晚到超过 `allowedLateness` 的数据只能进侧输出）、不承诺「加并行度就能解决反压」（下游不变时并行度只让反压更均匀）。

### 流处理与批处理的形态差别：无限输入、事件时间、状态

| 维度 | 批处理（有界输入） | 流处理（无限输入） | 这个差别推出了什么 |
| --- | --- | --- | --- |
| **输入** | 有界、可重放、可等待 | 无界、**只有一次机会**（除非 source 可重放） | 不能「先读完再算」，只能增量算 ⇒ 必须有状态 |
| **时间语义** | 处理时间 ≈ 数据时间（数据早就躺在那了） | 事件时间与处理时间**必然错位**（网络、设备时钟、乱序） | 需要 watermark 来界定「到齐了没有」 |
| **正确性的来源** | 重跑（失败就整体重算） | **快照 + 重放**（failover 回到上一个一致点） | 需要 checkpoint 与可重放的 source |
| **状态放哪** | 可以放磁盘/临时表，算完就扔 | 必须**常驻**（内存或本地盘），且必须被周期快照 | 状态大小 = 容量与恢复时间的分母 |
| **延迟下限** | 由调度与 IO 决定（分钟–小时） | 由**最慢的那条链路 + watermark 延迟**决定（秒–分钟） | 端到端延迟不是一个数，是一条链的加法 |
| **失败的代价** | 重跑一个 stage | 回退窗口内的**全部输入重放 + 全量状态恢复** | 恢复时间 ≈ 状态恢复 + 回退窗口 × 速率比 |
| **结果语义** | 幂等（覆盖写） | 非幂等（append 的流），除非 sink 支持事务/幂等 | 端到端恰好一次最终落在 sink 上 |

```text
【形态 1：批处理 —— 数据先到齐，再算】
  输入（有界）──► 全量读入 ──► 计算 ──► 覆盖写结果     ← 失败就整体重跑，无中间状态要救

【形态 2：流处理 —— 边到边算，状态常驻，靠快照救】
  Kafka p0 ─┐
  Kafka p1 ─┤  无界输入（永远读不完）
  Kafka p2 ─┘        │
            ┌────────▼─────────────────────────────────────┐
            │ operator subtask：去重 set / 窗口累加器（每 key）│
            └────────┬─────────────────────────────────────┘
                     │ ① 状态只在本进程里 ⇒ 必须快照
                     │ ② 输入读不完 ⇒ 位点必须和状态一起被快照
                     ▼
             每 T 秒：barrier 注入 → 对齐 → 异步上传
             failover：回到上一个快照 + 从位点重放

【时间轴：两种时间必然错位，watermark 是它们之间的那条线】
  事件时间  ──●──●────●──●────────●──●──►   记录自带的 eventTime
  处理时间  ──────●───●─────●──●─────────►   算子看到它的时刻
                  ▲ 乱序 + 延迟是常态；watermark = maxEventTime − 允许乱序
                    往前推 = 更早出 + 更可能错（漏迟到）；往后压 = 更对 + 更慢
```

---

## 34.3 物理形态：JobManager、TaskManager、slot、chain 与网络缓冲

### 进程与线程：谁在管谁

| 角色 | 是什么 | 管什么 | 它挂了会怎样 |
| --- | --- | --- | --- |
| **JobManager 进程** | 一个 JVM | 包含 Dispatcher（接作业提交）、ResourceManager（申请/释放 slot）、每作业一个 JobMaster（调度、触发 checkpoint、处理 failover） | 高可用配置下切换；否则**作业全停**，但状态还在最近的 checkpoint 里 |
| **TaskManager 进程** | 一个 JVM，有固定数量的 **slot** | 跑 task 线程、托管内存（`taskmanager.memory.managed`）、**网络缓冲池**（network buffer pool）、RocksDB 本地目录 | 它上面的 subtask 全部 failover，从最近 checkpoint 恢复 |
| **slot** | 一个 TaskManager 内的资源切分单位（**不是线程**） | 一个 slot 跑一条 **operator chain**（多个算子串行在一个线程里） | slot 数决定单机并行能力；`slot` 不等于 CPU 核 |
| **subtask** | 一个算子在某个并行度上的实例 | 有自己的本地状态分片（key group 区间） | 单个 subtask 反压/失败只影响自己的 chain，但 checkpoint 是**全局**的 |
| **operator chain** | 相邻算子若并行度相同且是 forward 连接，被合并进**同一个线程** | 省掉一次序列化 + 一次网络/线程切换 | 一个 chain 里最慢的算子决定整条 chain 的吞吐（没有隔离） |
| **keyBy / shuffle** | 一次 hash 分区 | `hash(key) % maxParallelism` → keyGroup → subtask | **这里有一次完整序列化**；倾斜也发生在这里 |

### 一条记录从 source 到 sink 经过什么

```text
┌──────────── TaskManager A（source + 去重 chain，同一线程）────────────────────────┐
│ Kafka bytes ─► Deserializer ─► StreamRecord<Event> ─► [去重] ─► [打宽] ─► RecordWriter│
│ ① 反序列化 1 次                                                          │           │
│                                          ┌──────────────────────────────▼─────────┐ │
│                                          │ ResultPartition → subpartition 0..N    │ │
│                                          │ ② 序列化进 MemorySegment（每 channel 2 个）│ │
│                                          └──────────────────┬─────────────────────┘ │
│               network buffer pool（固定字节数，满了就阻塞）───┘                       │
└──────────────────────────────────────────────┬──────────────────────────────────────┘
                                               │ Netty over TCP（同 AZ 0.3–1 ms）
┌──────────── TaskManager B（窗口 + sink chain）▼──────────────────────────────────────┐
│ InputChannel（credit 流控）─► mailbox ─► ③ 反序列化 ─► [窗口算子 + 状态] ─► [sink]    │
│                                                                                      │
│ ★ checkpoint barrier 走**同一条** channel，排在已发数据之后 ⇒ 反压时它也排队（§34.6）│
└──────────────────────────────────────────────────────────────────────────────────────┘

一条记录的编解码次数（决定 CPU 有多少花在「搬字节」）：
  source 反序列化 1 次；keyBy/shuffle +1 次（对象 → MemorySegment，下游再反序列化）；
  同一 chain 内 0 次（函数调用，不落地）；sink 序列化 +1 次
  ⇒ 「source → keyBy → sink」最短路径也至少 3 次编解码
```

### 一次 keyBy 的耗时分解（量级）

| 跳 | 动作 | 量级（单条） | 备注 |
| --- | --- | --- | --- |
| ① | 算 `hash(key)` + 定位 keyGroup | 20–80 ns | 字符串 key 要先算 hash（有缓存则摊薄） |
| ② | 序列化进 `MemorySegment` | 0.2–1 μs | 取决于字段数；Avro/POJO 比 Kryo 快 2–5 倍 |
| ③ | 写入 `ResultSubpartition` 队列 | 50–200 ns | 无锁队列；**缓冲池无空闲 buffer 时在这里阻塞** |
| ④ | Netty 写出（同机/同 AZ） | 20–200 μs（批） | 攒批后摊薄到每条 1–5 μs |
| ⑤ | 下游反序列化 | 0.2–1 μs | 与 ② 对称 |
| **合计** | 单条跨网络的端到端 | **P50 1–3 ms / P99 10–50 ms** | 含 `buffer-timeout` 的期望 50 ms 摊薄（见 §34.6） |

```java
// 一个「去重 + 5 分钟窗口」作业的拓扑骨架（Flink 1.20 DataStream API）
StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();

KafkaSource<Event> src = KafkaSource.<Event>builder()
    .setBootstrapServers("kafka-1:9092")
    .setTopics("device.events")
    .setGroupId("flink-device-dedup")
    .setStartingOffsets(OffsetsInitializer.committedOffsets(OffsetResetStrategy.LATEST))
    .setProperty("isolation.level", "read_committed")   // 只读已提交事务（§34.7）
    .setDeserializer(new EventDeserializer())            // bytes → StreamRecord
    .build();

DataStream<Event> events = env.fromSource(
        src,
        WatermarkStrategy.<Event>forBoundedOutOfOrderness(Duration.ofMinutes(2))
            .withTimestampAssigner((e, ts) -> e.eventTimeMs())
            .withIdleTimeout(Duration.ofMinutes(5)),      // 空闲分区不钉住 watermark
        "kafka-source");

events
    .keyBy(Event::deviceId)                              // hash shuffle：key → keyGroup → subtask
    .window(TumblingEventTimeWindows.of(Time.minutes(5)))
    .allowedLateness(Time.seconds(30))                   // 触发后仍保留 30 s 状态
    .sideOutputLateData(new OutputTag<>("late", TypeInformation.of(Event.class)))
    .aggregate(new OnlineDeviceCount(), new WindowResult())
    .name("online-count")
    .sinkTo(clickHouseSink());                           // 端到端语义由 sink 决定（§34.7）
```

**三个物理事实决定了后面全部结论**：① 状态在 subtask 本地 ⇒ 「状态有多大」= 所有 subtask 之和，倾斜让最热那个成为恢复瓶颈；② barrier 与数据走同一条 channel ⇒ 「反压」与「checkpoint 变慢」是同一件事的两个面；③ 序列化发生在 chain 边界 ⇒ 少一次 keyBy 就是少一次编解码（纯 CPU 账，与并发无关）。

---

## 34.4 checkpoint：barrier、对齐、异步快照与状态后端

### 它不是「拍一张照」，是一个分布式快照协议

Flink 的 checkpoint 是 Chandy–Lamport 快照在数据流上的实现：JobMaster 周期性让 **source 在数据流里注入一个 barrier**（一个特殊的标记），barrier 顺着 channel 往下流；每个算子收到**所有输入**的 barrier 后，把自己当前的状态拍成快照、上传、再把 barrier 转发下去；当所有 sink 都确认，这个 checkpoint 才算**完成**。

```text
【对齐式 checkpoint（aligned，默认 EXACTLY_ONCE）】
  source ──barrier(42)──►  [算子 A]  ──barrier(42)──►  [算子 B]  ──►  sink
                              │                          │
  输入 1: ──d1─d2─d3─┃─d4─d5─►  ← barrier 到达输入 1 后，输入 1 被**暂停读取**
  输入 2: ──e1─e2────────e3─┃─►  ← 等输入 2 的 barrier 追上来
                              │
                    对齐窗口 = max(各输入 barrier 到达时间) − min(...)
                    ★ 反压时这个窗口可以到分钟级（§34.6）

  对齐完成 ⇒ 捕获本地快照并转发 barrier；持久化可异步进行，不等上传完成才转发

【非对齐式 checkpoint（unaligned，1.11+）】
  输入 1: ──d1─d2─d3─d4─d5─┃────►   barrier 直接「超车」：不暂停输入，不等待对齐
  输入 2: ──e1─e2─e3─┃─────────►
              └─ barrier 之前那些「在途数据」被一起写进状态（channel state）
  ⇒ 对齐时间 ≈ 0；代价 = checkpoint 体积变大（要存 channel 里的 buffer）+ 恢复时要先把
     这些在途数据重放出去（恢复时间变长）；且不支持「有界 source 的 savepoint」等场景
```

### 一条 checkpoint 的生命周期（每一跳都可能失败）

| 阶段 | 动作 | 失败表现 | 谁在等 |
| --- | --- | --- | --- |
| ① 触发 | JobMaster 按 `interval` 发 `TriggerCheckpoint` | 上一次还没完成 + `max-concurrent-checkpoints=1` ⇒ 本次**跳过**（不算失败） | — |
| ② 注入 | source 把 barrier 塞进每个输出 channel | source 反压时塞不进去 | — |
| ③ 对齐 | 各算子等所有输入的 barrier | **`Alignment took too long`** / 超时（§34.6 的主战场） | 整条链 |
| ④ 快照 | 每个 subtask 序列化/硬链接自己的状态 | RocksDB 快照期间磁盘打满 / 堆后端 GC 停顿 | — |
| ⑤ 上传 | 异步上传到 checkpoint storage | S3 限流 / 5xx / 带宽被抢 | — |
| ⑥ 完成 | JobMaster 收到全部 ack，标记 completed，**回调 `notifyCheckpointComplete`** | — | **两阶段提交 sink 在这一刻才 commit（§34.7）** |
| ⑦ 超时 | 从 ① 起算超过 `timeout` 仍未完成 | `Checkpoint expired before completing` ⇒ 一次失败 | `tolerable-failed-checkpoints` 决定后果 |

### checkpoint 参数决策表

| 参数（Flink 1.20 键名） | 它改的是哪条物理路径 | 调大的收益 | 调大的代价 | 我们的口径 |
| --- | --- | --- | --- | --- |
| `execution.checkpointing.interval` | 两次快照的间隔 | 重放窗口变小（恢复更快） | 快照更频繁：上传带宽 × 次数、对线上吞吐的扰动 | **状态恢复时间 SLO 反推**：`interval ≤ 可接受重放时长`；不要按「默认 10 min」抄 |
| `execution.checkpointing.timeout` | 单次快照从触发到失败的容忍 | 减少「因为一次抖动就失败」 | 卡住的快照会长期占用 `max-concurrent` 名额，掩盖真问题 | ≥ 3 × 稳态时长，且必须 < `restart` 相关超时 |
| `execution.checkpointing.min-pause` | 两次之间的最小间隔 | 给上传留出喘息 | 实际间隔 = `interval + 快照时长` 的下界被抬高 | 稳态时长的 0.2–0.5 倍 |
| `execution.checkpointing.max-concurrent-checkpoints` | 同时能跑几个快照 | 大状态下减少「跳过」 | 带宽翻倍、恢复点选择变复杂 | 大状态（>100 GB）用 1；小状态可用 2 |
| `execution.checkpointing.tolerable-failed-checkpoints` | 连续失败几次才 failover | 抖动不再触发回滚 | **真故障显得像「只是慢」**，恢复点越拖越旧 | **默认 0 太脆**；生产设 2–3，且必须配「失败数」告警 |
| `execution.checkpointing.mode` | 算子状态恢复语义：`EXACTLY_ONCE` 或 `AT_LEAST_ONCE` | `AT_LEAST_ONCE` 可省对齐，但恢复时可能重复处理 | 下游幂等不自动修复算子状态的重复累加 | 不用它替代 exactly-once 的非对齐快照 |
| `execution.checkpointing.unaligned.enabled` | 在 `EXACTLY_ONCE` 下允许 barrier 超车并记录在途数据 | 反压时减少对齐等待 | 快照额外占用存储，恢复需处理 channel state | Flink 1.20 要求最大并发 checkpoint 数为 1；实测收益和恢复时长 |
| `execution.checkpointing.externalized-checkpoint-retention` | 取消作业时是否保留快照 | 可回到任意历史点 | 存储增长没人回收 | `RETAIN_ON_CANCELLATION`（配合 savepoint 目录生命周期） |

### 状态后端决策表：HashMap 还是 RocksDB

| 维度 | `HashMapStateBackend`（堆内） | `EmbeddedRocksDBStateBackend`（本地盘 + LSM） |
| --- | --- | --- |
| 状态放哪 | JVM 堆里的对象（`HashMap` / 自定义 map） | 本地磁盘上的 LSM（内存里只有 block cache + memtable） |
| 状态上限 | **受堆大小硬限**（几个 GB 就到头，再大 GC 就崩） | 受**本地盘**限制（几百 GB 到 TB 级） |
| 读延迟 | 纳秒–微秒（纯内存） | 微秒–毫秒（可能落盘；block cache 命中时接近内存） |
| 快照代价 | **全量序列化**（CPU 打满，单核 60–120 MB/s 量级） | 全量时硬链接 SST 文件；开 `incremental` 只传新增 SST |
| 恢复代价 | 反序列化 + 重建对象图（受 CPU 限制） | 下载 SST + 打开（建 index/bloom，受磁盘与文件数限制） |
| 增量 checkpoint | ❌ 不支持 | ✅ `state.backend.incremental = true` |
| 适合 | 状态 < 数 GB、延迟敏感、key 少 | 状态几十 GB 到 TB、key 上亿、能接受毫秒级读 |
| 定位 | 也是**唯一支持「状态大于内存」以外全部场景**之前的默认选择 | 大状态生产的默认（代价是磁盘 IO 与「本地盘丢了要全量重下」） |

### checkpoint 失败的语义：三种后果，别混

| 情况 | 触发条件 | 后果 | 观察到的现象 |
| --- | --- | --- | --- |
| **跳过（skipped）** | 上次未完成 + `max-concurrent=1` | 无后果，只是这次没做 | `numberOfInProgressCheckpoints` 长期 = 1 |
| **失败（failed）但可容忍** | 超时/上传错误，且 `failed < tolerable` | 作业继续跑，恢复点仍是**旧的** | `numberOfFailedCheckpoints` 涨，`lastCheckpointRestoreTimestamp` 不变 |
| **失败触发 failover** | `failed > tolerable`（**默认 0，即第一次就触发**） | 整体重启 + 回退到最近成功点 + 重放 | 作业 `RESTARTING`，Kafka lag 跳涨，`numRestarts` 涨 |

> **一句话**：`tolerable-failed-checkpoints=0` 意味着「任何一次 checkpoint 抖动 = 一次全量回滚」。事故一的自激环就是这条默认值加上「状态长得比带宽快」共同构成的。

```java
// 状态后端 + checkpoint 全套开关（Flink 1.20；键名以目标版本文档为准）
Configuration conf = new Configuration();
// ① 状态后端：hashmap（堆内）还是 rocksdb（本地盘 + LSM）
conf.setString("state.backend.type", "rocksdb");            // 1.19 起用 .type；更早版本键名是 state.backend
conf.setString("state.backend.incremental", "true");         // 仅 rocksdb：增量 checkpoint
conf.setString("state.backend.rocksdb.localdir", "/mnt/nvme/flink-state");
conf.setString("state.backend.rocksdb.memory.managed", "true"); // 内存交给 Flink 内存管理器
// ② checkpoint storage：快照落到哪里（JobManager 堆 / 文件系统 / S3）
conf.setString("execution.checkpointing.dir", "s3://flink-ckpt/prod/device-dedup");
conf.setString("execution.checkpointing.interval", "180s");
conf.setString("execution.checkpointing.timeout", "10min");
conf.setString("execution.checkpointing.min-pause", "30s");
conf.setString("execution.checkpointing.max-concurrent-checkpoints", "1");
conf.setString("execution.checkpointing.tolerable-failed-checkpoints", "3"); // 默认 0，务必显式
// ③ 对齐方式
conf.setString("execution.checkpointing.mode", "EXACTLY_ONCE");
conf.setString("execution.checkpointing.unaligned.enabled", "false");
// ④ 重启策略：没有它，checkpoint 失败会直接让作业退出
conf.setString("restart-strategy.type", "fixed-delay");
conf.setString("restart-strategy.fixed-delay.attempts", "5");
conf.setString("restart-strategy.fixed-delay.delay", "30s");

StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment(conf);
env.enableCheckpointing(180_000);   // 与 execution.checkpointing.interval 等价，二者只留一处
```

### savepoint 与 checkpoint 的区别（一句话版）

checkpoint 是**运行时自愈用的**（格式可跨版本不兼容、可增量、可能被自动清理）；savepoint 是**运维用的**（格式稳定、全量、可指定路径）。**升级代码前打 savepoint，别指望 checkpoint**：后者不保证跨版本可恢复，也不保证还没被清理。

---

## 34.5 watermark：事件时间、乱序界与「时间怎么停住的」

### 三种时间，只有一种能用来算业务

| 时间 | 定义 | 由谁给 | 问题 |
| --- | --- | --- | --- |
| **事件时间**（event time） | 事件真实发生时刻 | 业务字段/设备时钟 | 乱序 + 迟到；设备时钟可能漂移 |
| **摄入时间**（ingestion time） | 进 Flink source 的时刻 | source 打 | 与事件时间有固定偏移，但不受乱序影响 |
| **处理时间**（processing time） | 算子处理它的时刻 | 系统时钟 | **不可重放**：同一条数据重放时值不同 ⇒ 结果不可复现 |

**唯一能用来做「可重放的业务计算」的是事件时间。** processing time 的窗口结果在 failover 重放后会变（同一个窗口可能被算两次、每次结果不同），这是「作业重启后报表数字对不上」的最常见原因。

### watermark 是什么：一条单调的、由 min 决定的承诺线

```text
事件时间轴（每个分区各自生成 watermark）
  partition 0:  ●──●───●─────●──●         maxEventTime = 10:07:20
                watermark₀ = 10:07:20 − 2min = 10:05:20
  partition 1:  ●─●──●──●                 maxEventTime = 10:06:40
                watermark₁ = 10:04:40
  partition 2:  ●──●  （22:03 之后无数据）  maxEventTime 冻结 = 22:03  ← ★ 事故二
                watermark₂ = 22:01（永不前进）
                              │
   全局 watermark = min(watermark₀, watermark₁, watermark₂) = 22:01
                              │
   ⇒ 「22:01 之前的记录都已到齐」这句承诺，被一个空分区无限期推迟
   ⇒ 窗口 [22:00, 22:05) 的触发条件 watermark ≥ 22:05 永不满足

窗口触发与清理（EventTimeTrigger 口径，以目标版本行为为准）：
  触发：watermark ≥ window.maxTimestamp()
  清理：watermark ≥ window.maxTimestamp() + allowedLateness
  迟到：在「触发后、清理前」到达的记录会**再次触发**该窗口
        ⇒ 下游会看到同一个窗口被输出多次（最后一次才对）
        ⇒ 超过清理点的记录只能进 sideOutputLateData（默认直接丢）
```

### watermark 停滞的三种原因（按发生频率排序）

| # | 原因 | 物理机制 | 现场长相 | 怎么确认 | 怎么修 |
| --- | --- | --- | --- | --- | --- |
| ① | **某个输入分区空闲**（没数据） | 全局 watermark = min over inputs；空分区不产生新 watermark | lag = 0、吞吐正常、checkpoint 全绿、**窗口不触发** | 逐 subtask 拉 watermark，看是否有个 channel 冻住 | `withIdleTimeout(...)`；或让上游对空分区发心跳记录 |
| ② | **watermark 单调性 + 时间戳异常** | watermark 只能前进；一条「未来时间」的记录会把线推到未来，随后真实数据被当迟到丢掉 | 窗口**提前**触发且数字异常（少数据）、大量 late 记录进侧输出 | 侧输出计数、`eventTime − processingTime` 分布的 P99 | 在 source 侧钳制时间戳（`min(eventTime, now + 5min)`）；设备时钟对齐 |
| ③ | **source 不推进** | 没有新数据 / 消费者被 pause / `read_committed` 下 LSO 被长事务钉住 / rebalance 中分区未分配 | 整个 source 的 watermark 不动，lag 可能同时在涨 | 看 Kafka 侧 LSO 与事务状态（`→ comp/29 §29.3`）、看 source 的 `assignedPartitions` | 处理上游事务/暂停；给 rebalance 留出时间；多 source 用 `withWatermarkAlignment` 限制漂移 |

**两个常见误判**：① `pipeline.auto-watermark-interval`（默认 200 ms 量级）只影响 watermark 的**发送**频率，不会让它停住（除非 source 完全无数据）；② 「并行度 > 分区数」会让有些 subtask 一个分区都没分到 ⇒ 它没有输入 ⇒ 在 `min` 里永远是「还没开始」，`withIdleTimeout` 只是兜住症状，**根因在并行度与分区数的配比**。

### 决策表：乱序界（`forBoundedOutOfOrderness`）该给多大

| 你给的值 | 语义 | 结果正确性 | 端到端延迟（量级） | 适合 |
| --- | --- | --- | --- | --- |
| `0`（`forMonotonousTimestamps`） | 假设严格有序 | 只要有乱序就丢数据 | 最低（≈ 处理延迟） | 单分区、单生产者、无网络乱序 |
| 1–5 s | 吸收 RTT 级乱序 | 丢的是「> 5 s 迟到」的部分 | 秒级 | 同机房实时告警 |
| 30 s–2 min | 吸收应用级乱序（重试、批提交） | 大部分数据正确 | 十秒–分钟级 | 大多数实时看板 |
| 5–15 min | 吸收上游积压与重放 | 更准 | 分钟级起 | 准实时报表 |
| `Long.MAX_VALUE`（等价于「无 watermark」） | 永不算完 | **一个窗口都不触发** | ∞ | 只在有界 source + 显式 `setBounded` 时可用 |

**口径**：乱序界不是「技术参数」，是**业务对「迟到多久还算数」的定价**。把它写进数据契约里（和埋点 schema 一起评审），否则它就会变成一个「某天有人为了少几条迟到数据调到 30 分钟」的静默事故。

---

## 34.6 背压：credit 流控、buffer time 与「为什么加内存无效」

### 机制：credit-based 流控（Flink 1.5+ 默认）

```text
【为什么需要 credit：两级缓冲，谁也别把谁灌爆】
  上游 TaskManager                                    下游 TaskManager
  ┌──────────────────┐                               ┌──────────────────┐
  │ ResultSubpartition│ ──① 下游上报 credit = 4 ─────► │ InputChannel     │
  │  发送队列         │                               │  接收队列        │
  │                  │ ◄── ② 上游只在 credit > 0 时发 ─┤                  │
  └──────────────────┘                               └──────────────────┘
        │                                                      │
        │ ③ 缓冲池（network buffer pool）里没有空闲 buffer 时，  │
        │    发送端**阻塞**在 requestBuffer 上                  │
        ▼                                                      ▼
   ┌────────────────────────────────────────────────────────────────┐
   │ 反压沿 chain 逐级上传：sink 慢 → 中间算子输出满 → 输入满 →       │
   │ source 的 poll 变慢（每批记录数下降）→ Kafka lag 涨              │
   │ ★ 吞吐守恒：整条链的吞吐 = 最慢算子的吞吐，buffer 只改变堆积位置  │
   └────────────────────────────────────────────────────────────────┘

【buffer-timeout 的作用：攒批换吞吐，代价是固定延迟】
  每条记录进 buffer 后等 execution.buffer-timeout（默认 100 ms 量级）：
    攒满 → 立刻发（吞吐好）
    没攒满 → 到点也发（延迟有上界）
  ⇒ 单跳的期望额外延迟 ≈ buffer-timeout / 2（均匀到达时）
  ⇒ 一条 6 跳的链路，光 buffer-timeout 就贡献 ~300 ms 的期望延迟
```

### 反压为什么「加内存」无效

| 你的假设 | 物理事实 | 加内存后发生什么 |
| --- | --- | --- |
| 「缓冲不够，所以堵住了」 | 缓冲池（`taskmanager.memory.network.fraction`，默认 0.1）只决定**能堆多少**，不决定**能消化多少** | 堆得更久一点，然后同样堵住；同时 GC 压力变大、full GC 停顿变长 |
| 「加并行度就能扩」 | 反压点在**下游**（sink 的写入能力）时，加并行度只会让更多写请求同时压到同一个下游 | 下游 P99 更差，反压更强 |
| 「加 CPU 就好」 | 反压时 CPU 往往不高（线程在等 buffer 或等下游 ack） | 资源空转，账单上涨 |
| 「上游少读点就行」 | Kafka source 的 `poll` 已被 mailbox 阻塞，「少读」是**结果**不是**手段** | — |

**唯一有效的顺序**：① 找反压源头（哪个 subtask 的 `backPressuredTimeMsPerSecond` 最高、它的下游是谁）→ ② 修下游的产能（批量大小、连接数、慢查询、分片键）→ ③ 才谈扩容与并行度 → ④ 最后才是缓冲与 `buffer-timeout`。

### 反压如何卡住 checkpoint 对齐（本篇最硬的一条因果链）

**因果链（一句话读法）**：反压 → channel 里积压数据 → barrier 排在数据后面 → 对齐时间 ≈ 积压量 ÷ 下游吞吐 → 超过 `timeout` ⇒ 快照失败 ⇒（`tolerable=0`）failover ⇒ 回退重放 ⇒ 重放又打到同一个慢下游 ⇒ **反压 ⇒ 循环**。

| 环节 | 量级（事故三实测） | 与什么成正比 |
| --- | --- | --- |
| channel 内积压 | 640 个 buffer × 32 KB ≈ 20 MB / channel | 下游慢多久 × 上游速率 |
| 下游消化速率 | 3 万条/s（正常 12 万条/s） | sink 的批量与下游 P99 |
| 对齐时间 | 20 MB ÷ (3 万条/s × 800 B) ≈ 0.83 s（单 channel）→ 多级串联后 **9 min** | **串联跳数 × 单跳积压 ÷ 下游吞吐** |
| 结论 | 对齐时间是**反压的线性函数**，不是「状态大小」的函数 | — |

**所以「unaligned checkpoint」不是银弹**：它把对齐时间换成「快照体积 + 恢复时间」，在反压严重但状态小的时候有效；反压严重且状态大时，它只会让快照上传变成新瓶颈。

---

## 34.7 端到端 exactly-once：三个条件、四个回调、五类做不到的 sink

### 三个条件（缺一个就退化成 at-least-once 或 at-most-once）

| # | 条件 | 谁提供 | 缺了会怎样 |
| --- | --- | --- | --- |
| ① | **source 可重放**，且位点存在 checkpoint 里 | Kafka source（offset 进快照） | 位点没进快照 ⇒ 恢复后从错的点开始（重复或丢失） |
| ② | **sink 支持事务或幂等** | 两阶段提交 / 幂等 upsert | 重放时重复写入 |
| ③ | **状态与位点在同一个 checkpoint 里原子完成** | Flink 运行时 | 「状态回退了但输出没回退」⇒ 不一致 |

### 两阶段提交的四个回调时序

| 时刻 | 回调 | 你该做什么 | 消费者能看到吗 |
| --- | --- | --- | --- |
| 快照开始 | `beginTransaction()` | 开一个新事务（写临时表 / 打开 CH 事务窗口 / 拿新的 transactional.id） | 不能 |
| 快照期间 | `invoke(tx, record)` | 正常写，但写到「不可见」的地方 | 不能 |
| barrier 到达 sink | `preCommit(tx)` | flush + 把数据落到「已写但未提交」状态 | 不能（`read_committed` 隔离） |
| **checkpoint 完成** | `commit(tx)`（由 `notifyCheckpointComplete` 驱动） | 真正提交 | **从这一刻起能** |
| 失败/恢复 | `abort(tx)` | 丢弃未提交的临时数据 | 不能 |

**Kafka 侧的三个必须**（`→ comp/29 §29.3` 的 LSO/HW 概念在这里直接生效）：

- `transactional.id` 必须**每个 subtask、每个 checkpoint 唯一**（惯例是 `prefix + subtaskId + checkpointId`），否则上一个事务的残留会与新事务冲突。
- `transaction.timeout.ms` 必须 **> checkpoint 间隔 + 快照时长**，否则事务会在 checkpoint 完成前超时被 broker abort —— 这是「端到端恰好一次莫名退化成至少一次」的头号原因。
- 下游消费者必须 `isolation.level = read_committed`，否则会读到未提交数据（**而这意味着 LSO 会钉住可见性**，长事务的代价转移到了下游）。

```java
// Kafka source + 两阶段提交 sink 的最小骨架（时序即语义）
// 说明：TwoPhaseCommitSinkFunction 在较新版本已标记废弃，官方推荐 Sink V2 的 Sink + Committer；
// 这里用它讲清「预提交 / 提交 / 回滚」三个回调的时序，换 API 时语义不变。
// 方法签名随版本变化（较新版本 preCommit 多了 Context 参数），以目标版本文档为准。
public class ClickHouseTwoPhaseSink extends TwoPhaseCommitSinkFunction<Row, TxHandle, Void> {

    public ClickHouseTwoPhaseSink() { super(new TxHandleSerializer(), VoidSerializer.INSTANCE); }

    @Override
    protected TxHandle beginTransaction() {                 // ① 每个 checkpoint 区间一个事务
        return TxHandle.open(batchId.incrementAndGet());     // 写临时表 / 打开 CH 事务窗口
    }

    @Override
    protected void invoke(TxHandle tx, Row row, Context ctx) { tx.add(row); }

    @Override
    protected void preCommit(TxHandle tx) { tx.flush(); }     // ② 已落但不可见

    @Override
    protected void commit(TxHandle tx) { tx.commit(); }       // ③ checkpoint 完成回调：才可见

    @Override
    protected void abort(TxHandle tx) { tx.rollback(); }      // ④ 失败/恢复：丢弃临时数据
}
```

### 决策表：五类 sink 能做到哪一档

| sink | 原生能力 | 能到的语义 | 要补什么 | 代价 |
| --- | --- | --- | --- | --- |
| **Kafka**（`DeliveryGuarantee.EXACTLY_ONCE`） | 事务 + `transactional.id` | ✅ 端到端恰好一次（含下游 `read_committed`） | 无 | 事务开销 + LSO 钉住可见性 |
| **文件/对象存储（S3）** | 无跨对象事务 | ⚠️ 只能「按 checkpoint 命名 + 提交时原子改清单」 | 两阶段命名 + 下游读清单而非目录 | 小文件（每次 checkpoint 一批，→ `→ architect/21 §21.6`） |
| **关系库（JDBC）** | 单库事务，无跨 checkpoint 事务 | ⚠️ 至少一次 + 幂等 | `INSERT ... ON DUPLICATE KEY` / 去重表 | 写放大 + 唯一键冲突要处理 |
| **ClickHouse** | 无跨语句事务 | ⚠️ 临时表 + `EXCHANGE`/`RENAME`，或幂等键 + `ReplacingMergeTree` | 幂等键 + 去重视图 | 查询侧要 `FINAL` 或聚合去重 |
| **HTTP / RPC 副作用** | **不可回滚** | ❌ 最多一次，或至少一次 + 业务去重 | 幂等键 + 对账（→ [eng/24](../eng/24-delivery-semantics.md)） | **没有技术解，只有业务解** |
| **Redis** | 单命令原子，无跨命令事务 | ⚠️ 至少一次 + 幂等 | 幂等 set / 版本号 | 会重复计数 |

> **一句话**：Flink 的「端到端恰好一次」不是 Flink 给自己的承诺，是**「可重放的 source + 事务型 sink + 快照里同时含位点」三者联署**的结果。凡是 sink 侧有不可回滚的外部副作用（发短信、扣款、调三方），语义上限就是「至少一次 + 业务幂等」，这一点在 [eng/24](../eng/24-delivery-semantics.md) 已经讲透，本篇不重写。

---

## 34.8 三本账：状态大小、checkpoint 时长、恢复时间

三本账都按事故一的作业代入（380 GB 状态、96 分区、120 subtask、60k 事件/s、单条 1.1 KB），每一项都给乘法链、假设清单与结论标记。

### 账一 · 状态大小 = key 数 × 每 key 状态 × LSM 开销 × 版本数

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | key 数 | 12 亿用户/设备，30 天内活跃 | 12 亿 |
| 2 | 每 key 状态 | 3 个 `long`（计数/时间戳）+ 1 个 `int`（版本）+ 1 个 `float`（分数）≈ 28 B 原始 + 序列化头 | 96 B |
| 3 | 原始状态 | 12e8 × 96 B | **115 GB** |
| 4 | RocksDB 开销 | LSM 的 SST 索引 + bloom + 未压缩 index block + 多版本残留 ≈ 1.8–2.4× | **207–276 GB** |
| 5 | 堆内后端对照 | `HashMapStateBackend` 的对象头 + 引用开销 ≈ 2.5–3.5× | **288–403 GB（堆里放不下）** |
| 6 | 倾斜放大 | 最热 subtask 占 3 倍均值（大客户） | 单 subtask 5–7 GB |
| 7 | 带 TTL 的版本 | 若状态有 TTL 且每天全量刷新 ⇒ 峰值版本数 ≈ 2 | **×2 = 414–552 GB** |
| 8 | 恢复时的额外 | RocksDB 打开 + 重建 index/bloom ≈ 状态量的 1.1–1.3 倍临时占用（本地盘） | 本地盘要 ≥ 1.3 × 状态 |

> **量级结论（非承诺值）**。假设：① 每 key 5 个定长字段、96 B 序列化后；② RocksDB 开销 1.8–2.4×（SST 块大小 4–64 KB、未开压缩、bloom 10 bit/key）；③ 无倾斜 3 倍以内的假设来自「大客户占总量 30%」；④ TTL 与刷新周期决定版本数。**换假设请按步进表重算**：把每 key 状态从 96 B 降到 48 B（只留 2 个字段），账一整体减半（207 → 115 GB）；把 TTL 从 30 天降到 7 天，key 数从 12 亿降到 4 亿，状态降到 **70–92 GB** —— **状态大小的杠杆只有两个：key 数与每 key 状态，并行度不是杠杆**。

### 账二 · checkpoint 时长 = 状态字节 ÷ 上传带宽 + 对齐等待 + 屏障传播

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 快照字节 | 全量：380 GB；增量（RocksDB `incremental=true`）：稳态新增 SST ≈ 状态的 5–15% | 380 GB / 19–57 GB |
| 2 | 快照 CPU | RocksDB 硬链接 SST ≈ 0 序列化；HashMap 后端要全量序列化，单核 60–120 MB/s | 0（不过 CPU）/ 53–105 min |
| 3 | 聚合上传带宽（独占） | 24 TM × 4 并发 × 25 MB/s | **2.4 GB/s** |
| 4 | 聚合上传带宽（被抢） | 同 region 出口被另一个归档任务吃掉 83% | **400 MB/s** |
| 5 | 全量上传时长（独占） | 380 GB ÷ 2.4 GB/s | **158 s ≈ 2.6 min** |
| 6 | 全量上传时长（被抢） | 380 GB ÷ 400 MB/s | **950 s ≈ 15.8 min** |
| 7 | 增量上传时长（被抢） | 38 GB ÷ 400 MB/s | 95 s |
| 8 | 对齐等待（稳态） | 反压 12% 时实测 | **0.4–1.2 s** |
| 9 | 对齐等待（反压 99%） | 事故三实测 | **540 s** |
| 10 | 屏障传播 | 跳数 × 单跳 buffer 排空（ms 级） | < 1 s |
| 11 | **总时长（稳态 · 全量）** | 158 s + 1 s + 1 s | **≈ 2.7 min** |
| 12 | **总时长（被抢 · 全量）** | 950 s + 1 s | **≈ 15.9 min > timeout 10 min ⇒ 必失败** |
| 13 | **总时长（反压 · 增量）** | 95 s + 540 s | **≈ 10.6 min > timeout ⇒ 也失败** |

> **量级结论（非承诺值）**。假设：① 每 TM 的上传并发 4、单流 25 MB/s（对象存储单连接限速与服务端限流决定，**不是网卡上限**）；② 状态 380 GB 且无压缩（RocksDB 全量快照可硬链接）；③ 对齐等待取实测值；④ 屏障传播与跳数线性。**这张表最重要的两格是第 12 与 13 行**：它们说明「checkpoint 超时」有两条完全不同的修法——第 12 行要**治带宽**（独立桶、限流隔离、开增量），第 13 行要**治反压**（加带宽只会让 540 s 的对齐等待依然存在）。**把两条混成一条，就会像事故三那样去加内存。**

### 账三 · 恢复时间 = 状态恢复（下载 + 打开 + 重建索引）+ 位点回退重放

| 步 | 项 | 推导 | 结果 |
| --- | --- | --- | --- |
| 1 | 快照下载 | 380 GB ÷ 1.5 GB/s（24 TM 并行拉） | **253 s ≈ 4.2 min** |
| 2 | 解压/校验 | 若开 zstd（3×），先解压再落 SST；CPU 与 ① 重叠 | +30–60 s |
| 3 | RocksDB 打开 | 每 TM 16 GB 状态、数千个 SST 文件，建 index/bloom | +40–90 s |
| 4 | **状态恢复小计** | ① + ② + ③（部分重叠） | **5–7 min** |
| 5 | 回退窗口 | `interval 180 s` + 对齐（稳态 1 s）+ 检测（30 s 量级） | **≈ 3.5 min** |
| 6 | 回退条数 | 3.5 min × 60k/s × 60 | **1,260 万条** |
| 7 | 重放吞吐（下游不限速） | = 生产吞吐 | 60k/s ⇒ 重放 3.5 min |
| 8 | 重放吞吐（下游限 1/3） | 20k/s | ⇒ 重放 **10.5 min** |
| 9 | **总恢复（稳态）** | 5 min + 3.5 min | **≈ 8.5 min** |
| 10 | **总恢复（下游堵死）** | 7 min + 10.5 min | **≈ 17.5 min** |
| 11 | **总恢复（事故一形态）** | 成功点停在 02:48：状态恢复 9 min（带宽被抢，比稳态 5–7 min 慢）+ 重放 17 min（1:1） | **≈ 26 min / 轮** |
| 12 | 6 轮累计 | 26 min × 6 | **≈ 2 h 35 min（与事故一现场一致）** |

> **量级结论（非承诺值）**。假设：① 快照 380 GB 未压缩、并行下载 1.5 GB/s；② 每 TM 状态 16 GB、SST 文件数千级；③ 生产速率 60k/s 恒定；④ 重放速率 = `min(生产速率, 下游可承受速率)`；⑤ 未计 RocksDB 的 compaction 追赶窗口（恢复后 10–30 min 内会有一波写放大）。**核心公式一行**：`恢复时间 ≈ 状态恢复 + 回退窗口 × (生产速率 ÷ 重放速率)`。**它不是承诺值**：把 `interval` 从 3 min 降到 30 s，第 5 行的回退窗口缩到 40 s，总恢复从 8.5 min 降到约 6 min——**代价是 checkpoint 频次 ×6，账二的带宽乘 6**。这就是「RTO 与带宽」之间唯一的那笔交易。

```python
# 三本账压成一个可复算的脚本（改任一假设直接重跑）
GB, MB = 1e9, 1e6

def state_gb(keys, bytes_per_key, lsm_overhead=2.0, versions=1):
    """账一：keys × 每 key 状态 × LSM 开销 × 版本数"""
    return keys * bytes_per_key * lsm_overhead * versions / GB

def ckpt_sec(state, bw_mb_s, align_s=0.0):
    """账二：状态字节 ÷ 聚合上传带宽 + 对齐等待（对齐与字节无关，与反压有关）"""
    return state * GB / (bw_mb_s * MB) + align_s

def recovery_min(state, restore_mb_s, rollback_min, prod_tps, replay_tps=None):
    """账三：状态下载+打开 + 回退窗口 × (生产速率 ÷ 重放速率)"""
    restore = state * GB / (restore_mb_s * MB) / 60
    ratio = 1.0 if replay_tps is None else prod_tps / min(prod_tps, replay_tps)
    return restore, rollback_min * ratio

print(f"账一 12e8 key × 96 B × 2.0 = {state_gb(1.2e9, 96):.0f} GB")
print(f"账二 380 GB / 2400 MB/s = {ckpt_sec(380, 2400):.0f} s（独占带宽）")
print(f"账二 380 GB /  400 MB/s = {ckpt_sec(380, 400):.0f} s（被抢带宽）")
print(f"账二 38 GB / 400 MB/s + 540 s 对齐 = {ckpt_sec(38, 400, 540):.0f} s（反压）")
r1, p1 = recovery_min(380, 1500, 3.5, 60_000)
r2, p2 = recovery_min(380, 1500, 3.5, 60_000, replay_tps=20_000)
print(f"账三 稳态恢复 {r1:.1f} + 重放 {p1:.1f} = {r1+p1:.1f} min")
print(f"账三 下游限速 {r2:.1f} + 重放 {p2:.1f} = {r2+p2:.1f} min")
```

---

## 34.9 架构边界：Flink 能保证什么、不能保证什么

| 你想要的保证 | Flink 给不给 | 物理原因 | 正确做法 |
| --- | --- | --- | --- |
| **failover 后不丢、不重** | ✅（在 §34.7 三条件下） | 状态 + 位点在同一个快照里 | 显式配 `tolerable-failed-checkpoints`、`read_committed`、事务超时 |
| **作业不重启就不丢** | ❌ | 快照是周期性的；最后一个 `interval` 内的数据只在内存/盘上 | 承认「重放窗口」存在，用 source 的可重放性兜底 |
| **外部副作用恰好一次** | ❌ | 发短信/扣款的 API 不可回滚 | 幂等键 + 去重表 + 对账（→ [eng/24](../eng/24-delivery-semantics.md)） |
| **跨作业一致** | ❌ | 每个作业有自己的 checkpoint 与位点 | 上游统一（同一份 Kafka 位点），或下游做对账 |
| **窗口结果只输出一次** | ❌（开了 `allowedLateness` 就不止一次） | 迟到数据会**再次触发**同一窗口 | 下游按窗口键 upsert，或接受「多版本」 |
| **低延迟 + 高正确性同时** | ⚠️ 只能买到一条 | watermark 就是那个兑换点 | 按业务定价：告警链路给 `1–5 s`，报表链路给 `5–15 min` |
| **状态无限增长** | ❌ | 状态是本地资源，TTL 是唯一的回收机制 | 每个 keyed state 显式 `StateTtlConfig`；无 TTL 的状态等于内存泄漏 |
| **加并行度提升吞吐** | ⚠️ 只在 source/sink 可扩时成立 | 并行度受分区数、下游连接数、key 倾斜限制 | 先看倾斜（最热 subtask 占比），再看下游 |
| **反压时 checkpoint 照常** | ⚠️ 只有 unaligned 能 | barrier 排在数据后面 | 反压常态化 + 状态小 ⇒ 开 unaligned；否则治反压 |
| **作业升级不改语义** | ❌ | 算子 UID 变了、状态描述符变了都会重置状态 | 显式 `uid()`、`savepoint` 升级、状态迁移演练 |

**一句话边界**：Flink 保证的是「**在一个可重放的 source、一个事务型/幂等的 sink、以及一个周期性快照的运行时**这三者重叠的范围内，结果与故障次数无关」。三者之外的一切（外部副作用、跨作业一致性、迟到数据、状态增长）都必须由你在业务层签字。

---

## 34.10 踩坑清单（现象 → 根因 → 定位 → 修复 → 防复发）

| 现象 | 根因（指到物理形态） | 怎么定位 | 怎么修 | 怎么防复发 |
| --- | --- | --- | --- | --- |
| checkpoint 反复失败 → 作业反复重启 → lag 涨 | `tolerable-failed-checkpoints=0`（默认）+ 快照时长 > `timeout` | `/jobs/:id/checkpoints` 看 `history[-1].duration` 与 `timeout` 的比值；看 `numRestarts` | 先抬 `timeout` 与 `tolerable` 止血；再治带宽（独立桶/开增量） | 把「快照时长 / timeout」做成斜率告警；容量评审纳入状态增长曲线 |
| 状态从 40 GB 涨到 380 GB 没人知道 | 状态无 TTL、或 key 基数随业务线性增长 | 每 subtask 的 `checkpointSize` 趋势；RocksDB 本地目录 `du` | 给每个 keyed state 加 `StateTtlConfig`；换更省的序列化（Avro/POJO 替 Kryo） | 状态大小进容量评审（与 QPS 同级）；每月对账「状态/活跃 key」 |
| 看板数字不动，但 lag=0、checkpoint 全绿 | watermark = min，被一个空闲分区钉死（事故二） | 逐 subtask 拉 watermark，找冻住的那个 channel | `withIdleTimeout`；或让上游对空分区发心跳 | 并行度与分区数配比进模板；watermark 停滞单独告警 |
| 窗口结果少量缺失 + 侧输出暴涨 | 乱序界 < 真实乱序；或一条未来时间戳把线推飞 | `eventTime − processingTime` 分布；侧输出计数 | 钳制时间戳；按 P99 乱序定界 | 乱序界写进数据契约，与埋点 schema 一起评审 |
| 反压 99%，扩容内存无效 | 吞吐守恒，buffer 只改「堆在哪」（事故三） | `backPressuredTimeMsPerSecond` 找源头 subtask；看它的下游 | 修下游产能（批量/连接/慢查询） | 反压指标按 subtask 出图；扩容前必须能指到下游哪一跳 |
| 快照时长正常但 job 卡住 | 对齐等待（反压）而非上传慢 | 看 REST 的 alignment 时长；对照 `backPressured` | 治反压；或开 unaligned | 把「对齐时长」与「上传时长」分开出图 |
| 端到端看起来重复写 | 事务超时 < checkpoint 间隔 ⇒ 事务被 broker abort | Kafka 事务日志 + `transaction.timeout.ms` 与 interval 比较 | 抬事务超时，且 > interval + 快照时长 | 两者关系写进配置校验（启动时断言） |
| 恢复后 10–30 min 吞吐只有一半 | 恢复触发的 RocksDB compaction 追赶 + 页缓存冷 | 恢复后 `iowait`、RocksDB `compaction pending` | 恢复期限速 + 预热；状态放本地 NVMe | 恢复演练纳入发布流程（每月一次） |
| 升级后状态全丢 | 算子 `uid()` 变了 / 状态描述符变了 | 从 savepoint 恢复时看「无法映射状态」告警 | 显式 `uid()`；用 savepoint 而非 checkpoint 升级 | UID 规范进代码评审 checklist |

---

## 34.11 观测：checkpoint、反压、watermark、GC 四组指标

**前置开关**：① `metrics.reporter.prom.factory.class` 配上 Prometheus reporter 才有一致的指标名；② `metrics.latency.interval` 不设则没有端到端 `latency` 指标（默认关闭）；③ 下面所有指标名以**目标版本（本篇按 1.20）的 Metrics 列表与 REST 输出为准**——Flink 的指标名在大版本间改过（例如反压在 1.15 前后拆成了 soft/hard 两档）。

| 看什么 | 指标 / 接口 | 拐点（越线就动手） | 版本与开关前提 |
| --- | --- | --- | --- |
| **① checkpoint 时长 / 失败数（第一指标）** | `lastCheckpointDuration`、`numberOfFailedCheckpoints`、`numberOfCompletedCheckpoints` | 时长 > `timeout` 的 0.6 倍 ⇒ 提前 10 分钟报警；失败数 30 min 内 > 0 | Job 级指标，需 `execution.checkpointing.interval` 开启 |
| ② 快照体积趋势 | `lastCheckpointSize`、`checkpointSize`（per subtask） | 单周涨幅 > 30% ⇒ 状态有泄漏；最热 subtask 占比 > 均值 2 倍 ⇒ 倾斜 | 同上 |
| ③ 对齐 vs 上传 | REST `checkpoints` 详情里的对齐时长 / 上传时长 | 对齐 > 5 s 且反压高 ⇒ 治反压；上传 > 稳态 3 倍 ⇒ 治带宽 | 字段名以目标版本 REST 输出为准 |
| **⑤ 反压（第二指标）** | `backPressuredTimeMsPerSecond`（1.15+ 另拆 `softBackPressuredTimeMsPerSecond` / `hardBackPressuredTimeMsPerSecond`） | 持续 > 800 ms/s ⇒ 该 subtask 是瓶颈；**必须与下游一起看** | Task 级指标，需开 `metrics.latency.interval` 之类无关，但需 reporter |
| ⑥ 忙碌/空闲 | `busyTimeMsPerSecond`、`idleTimeMsPerSecond` | `idle` 高而 `busy` 低 + lag 涨 ⇒ 上游没数据（可能是 watermark 停滞的同源） | Task 级 |
| **⑦ watermark 位置** | REST `/jobs/:jid/vertices/:vid/metrics?get=...watermark...` 或自采 | 各 channel 的 watermark 极差 > 乱序界 × 5 ⇒ 有分区被钉住 | 字段名随版本变化，以目标版本为准；必要时自采 |
| ⑧ 吞吐与延迟 | `numRecordsInPerSecond`、`numRecordsOutPerSecond`、`latency`（需 `metrics.latency.interval`） | 三个并行度之间吞吐差 > 3 倍 ⇒ 倾斜；延迟 P99 > 乱序界 ⇒ 反压在积累 | `latency` 默认关闭 |
| ⑨ GC 与内存 | `heapUsed`、GC 停顿（JVM 指标）；`taskmanager.memory.network` 使用率 | 堆后端 full GC > 1 s 持续 ⇒ 换 rocksdb；网络 buffer 使用率长期 100% ⇒ 反压（**不是内存不足**） | 需 JVM 指标 reporter |

```bash
# ① 作业级 checkpoint 健康（REST，端口以实际配置为准）
JOB=$(curl -s localhost:8081/jobs/overview | jq -r '.jobs[0].jid')
curl -s "localhost:8081/jobs/$JOB/checkpoints" \
  | jq '{counts: .counts, last: (.history[-1] | {duration, size, status})}'

# ② 反压：逐 subtask 看 backPressured / idle / busy（1.15+ 另有 soft/hard 两档）
curl -s "localhost:8081/jobs/$JOB/vertices" | jq -r '.vertices[].id' | while read -r v; do
  curl -s "localhost:8081/jobs/$JOB/vertices/$v/metrics?get=backPressuredTimeMsPerSecond,idleTimeMsPerSecond,busyTimeMsPerSecond" \
    | jq -r --arg v "$v" '.[] | "\($v)\t\(.id)\t\(.value)"'
done | sort -k3 -nr | head -10

# ③ watermark 是否在推进（字段名以目标版本 Metrics 列表为准，取不到就自采）
curl -s "localhost:8081/jobs/$JOB/vertices" \
  | jq -r '.vertices[] | "\(.name)\t\(.metrics["currentInputWatermark"] // "n/a")"'

# ④ 告警表达式骨架（Prometheus 风格，阈值取自本节拐点列）
#   10 分钟内一次完成的 checkpoint 都没有（作业还在跑）
#   rate(flink_jobmanager_job_numberofcompletedcheckpoints[10m]) == 0 and on(job) up == 1
#   快照时长已到超时的 60%
#   flink_jobmanager_job_lastcheckpointduration > 0.6 * <timeout 秒>
#   反压持续（hard 档）
#   max by (task) (flink_taskmanager_job_task_backpressuredtimemspersecond) > 800
#   状态周涨幅异常（用 lastCheckpointSize 的 7d 变化率）
#   deriv(flink_jobmanager_job_lastcheckpointsize[7d]) > 0.3 * flink_jobmanager_job_lastcheckpointsize
```

---

## 34.12 一页速记

| 记什么 | 具体结论 |
| --- | --- |
| 物理形态一句话 | 分布式流运行时 = **JobManager（调度 + 触发快照）+ TaskManager（slot 里跑 operator chain）+ 网络缓冲池（固定字节）+ 本地状态（RocksDB/堆）** |
| checkpoint 是什么 | barrier 划定一致性切面：可对齐等待，也可在非对齐模式下把在途数据计入快照；事务型 sink 按连接器约定在 checkpoint 完成后提交 |
| 三本账（一行版） | ① 状态 = key 数 × 每 key × LSM 开销 × 版本；② 快照时长 = 状态 ÷ 上传带宽 **+ 对齐等待**；③ 恢复 = 状态恢复 **+ 回退窗口 × 速率比** |
| 状态后端 | 堆内（HashMap）受堆限、快照要全量序列化；RocksDB 受盘限、可增量硬链接。**状态 > 数 GB 一律 RocksDB** |
| checkpoint 失败语义 | 默认 `tolerable-failed-checkpoints=0` ⇒ **一次抖动 = 一次 failover**；参数必须显式设 |
| watermark 是什么 | `maxEventTime − 乱序界`，全局取 **min**；**一个空闲分区就能钉死整条链**，且不报错 |
| 窗口触发 | `watermark ≥ window.maxTimestamp()` 触发；`+ allowedLateness` 才清理；迟到会**再次触发**（下游要能 upsert） |
| 停滞三因 | ① 分区空闲（`withIdleTimeout`）② 时间戳异常/单调性 ③ source 不推进（无数据/pause/LSO 钉住） |
| 背压本质 | **吞吐守恒**：buffer 只改「堆在哪」。加内存/加 CPU 都无效，只有修下游产能有效 |
| 反压卡 checkpoint 的链 | 反压 → channel 积压 → barrier 排队 → 对齐时间涨 → 超时 → failover → 重放 → 又反压（自激） |
| 端到端恰好一次 | 三条件联署：**可重放 source + 事务/幂等 sink + 位点与状态同快照**；sink 有不可回滚副作用时上限就是 at-least-once |
| Kafka 事务三必须 | `transactional.id` 每 subtask 每 checkpoint 唯一；`transaction.timeout.ms` > interval + 快照时长；下游 `read_committed` |
| 恢复公式 | `恢复时间 ≈ 状态恢复 + 回退窗口 × (生产速率 ÷ 重放速率)`；**调小 interval 是唯一能压回退窗口的旋钮，代价是带宽** |
| 第一指标 | `lastCheckpointDuration / timeout` 的比值（提前 10 分钟）；第二指标是**逐 subtask 的反压** |
| 一句话 | **一致性快照可对齐或非对齐；端到端保证还取决于 source 与 sink，watermark 单独决定事件时间输出节奏。** |

---

## 34.13 自检题（5 题）

**Q1.** 一个作业状态 300 GB（RocksDB 全量）、96 subtask、checkpoint 落 S3；`interval=5min`、`timeout=10min`、`tolerable-failed-checkpoints=0`。某天开始每 20 分钟失败一次并从 40 分钟前的点恢复。请给出两条最可能的根因、各自的确认方法，以及「只改一个参数先止血」应该改哪个。

<details><summary>答案</summary>

**两条根因，都指向「快照时长跨过了 10 min」**：

① **上传带宽被抢**：S3 出口被同 region 其它任务共享，聚合带宽 2.4 GB/s → 400 MB/s ⇒ 300 GB ÷ 400 MB/s = **750 s = 12.5 min > 10 min**。确认：`/jobs/:id/checkpoints` 看失败那次 `duration`，用历史成功快照的 `size ÷ duration` 反推有效带宽，再查同 region 的归档任务时间窗。

② **对齐等待被反压拉长**：上传只有 2 min，但对齐占 9 min（下游 sink 慢，barrier 在 channel 里排队）。确认：把对齐时长与上传时长分开取（REST checkpoint 详情有分阶段时间，字段名以目标版本为准），同时看该 subtask 的 `backPressuredTimePerSecond` 与它的下游。

**只改一个参数止血**：改 `execution.checkpointing.tolerable-failed-checkpoints` 从 0 改成 2 或 3 —— 它把「每次抖动都回滚」变成「连续失败才回滚」，恢复点不再越拖越旧，且**不改变快照本身的代价**。改 `timeout` 也能止血但会掩盖「快照确实越来越慢」这个信号；改 `interval` 只会让恢复窗口变得更大。

量级结论，非承诺值：上式的秒数随聚合带宽线性变化，换机型/换桶/换并发都要重算。
</details>

**Q2.** 某看板作业的 5 分钟窗口 8 小时没输出，但 Kafka lag = 0、checkpoint 全绿、作业 RUNNING、CPU 20%。请给出完整的排查顺序，并说明「为什么 lag=0 反而是一个提示」。

<details><summary>答案</summary>

**lag = 0 是提示**：它说明 source 把能读的都读完了——也就是说**没有数据在等**，问题不在吞吐，而在「时间线」。lag 是「位点差」，与「watermark 走到哪」完全无关。所以排查顺序应该从「时间」而不是「吞吐」开始：

1. **确认窗口真的没触发**：看 sink 的 `numRecordsOutPerSecond` 与下游写入（别只看看板）。
2. **逐 subtask 拉 watermark**：若 47 个已到 05:00、1 个停在 22:03，基本就是「空闲分区钉住 min」（事故二）。
3. **确认该分区真的没数据**：`kafka-get-offsets.sh --time -1` 看各分区 LEO 是否 8 小时没动。
4. **排除另两因**：①「未来时间戳」把线推飞（看侧输出 late 计数）；② source 被 pause 或 `read_committed` 下 LSO 被长事务钉住（`→ comp/29 §29.3`）。
5. **修**：加 `withIdleTimeout(Duration.ofMinutes(5))`（改代码 + 发布），并盯住「一次性补触发」对下游的冲击——事故二里它把 Redis 打满了 20 分钟。

**为什么 checkpoint 全绿不能排除问题**：watermark 停滞不产生任何快照异常，它只让「窗口的触发条件」永远不满足。**这是流处理里唯一一类「正确性静默丢失」的故障**，所以必须给 watermark 单独配指标和告警。
</details>

**Q3.** 下游 ClickHouse 变慢，Flink 全链路反压到 source，checkpoint 对齐从 0.4 s 涨到 9 min 并开始超时。有人说「给 TaskManager 加内存，buffer 多了就不堵了」。请用吞吐守恒解释为什么无效，并给出正确的处置顺序。

<details><summary>答案</summary>

**为什么无效**：`taskmanager.memory.network.fraction`（默认 0.1）决定的是**缓冲池的字节数**，也就是「能在哪里堆多久」。设下游消化速率 `D`、上游产生速率 `U`、`U > D`，那么无论缓冲池是 800 MB 还是 1.6 GB，队列长度的增长率都是 `U − D > 0`，只是**到达「满」的时间从 `800MB/(U−D)` 变成 `1.6GB/(U−D)`**。缓冲不是产能，它是**延迟**。副作用还有两个：① 堆/直接内存更紧，GC 停顿变长；② 反压变成「更晚发生、但发生时堆积更多」，对齐时间反而更长。

**正确顺序（事故三的教训）**：

1. **定位源头**：按 subtask 拉 `backPressuredTimeMsPerSecond`，最高的那个就是瓶颈；再看它的下游是谁（sink？shuffle？）。
2. **修下游产能**：ClickHouse 侧杀慢查询/等 merge 过去；调小 sink 批量与 flush 间隔（减小单次请求体、提高并发）；必要时按分区键改写入分布。
3. **改失败语义止血**：`tolerable-failed-checkpoints` 从 0 抬到 2–3，让抖动不再引发回滚（**注意这不是修复，是防止自激**）。
4. **才谈扩容**：加 TaskManager 只在「下游可扩」时才有意义；加 CPU 在反压时通常无效（线程在等 buffer）。
5. **最后才是缓冲与 `buffer-timeout`**：调大 `buffer-timeout` 提升吞吐、增大延迟；调小反之。它是「吞吐/延迟」的旋钮，不是「反压」的旋钮。

量级结论：对齐时间 ≈ `串联跳数 × 单跳积压 ÷ 下游吞吐`，与状态大小无关——所以「状态没涨但 checkpoint 变慢」几乎一定是反压，不是磁盘。
</details>

**Q4.** 一个作业声称「端到端恰好一次」，但下游发现同一个窗口的数据被写了两次。请列出至少四个可能原因，并说明如何用「三个条件」逐一排除。

<details><summary>答案</summary>

**四个原因**：

① **`allowedLateness > 0` 且迟到数据再次触发窗口**：这是**设计行为**，不是 bug。同一个窗口会被输出多次（最后一次才对）。确认：侧输出 late 计数、同一窗口键的多次写入时间戳。修：下游按窗口键 upsert，或把 `allowedLateness` 设为 0 并接受丢迟到数据。

② **sink 不是事务型/幂等**：JDBC/HTTP/Redis 这类 sink 在重放时必然重复。确认：看 sink 的语义（§34.7 决策表）。修：加幂等键 + 去重表。

③ **Kafka 事务超时**：`transaction.timeout.ms` < `checkpoint interval + 快照时长` ⇒ broker 主动 abort 事务，Flink 恢复后重放那一段。确认：Kafka 事务日志里的 `AbortTransaction`、`transaction.timeout.ms` 与 interval 的比值。修：抬事务超时并加启动断言。

④ **`execution.checkpointing.mode = AT_LEAST_ONCE`**：快照不提供 exactly-once 的算子状态语义，恢复可能重复处理。确认实际模式，必要时改回 `EXACTLY_ONCE`。不能把它与非对齐 exactly-once 混为一谈：后者把在途数据纳入快照，并未降级。见 [Flink 1.20 非对齐检查点](https://nightlies.apache.org/flink/flink-docs-release-1.20/docs/ops/state/checkpointing_under_backpressure/#unaligned-checkpoints)。

**用三条件排查**：① source 可重放，恢复时使用快照内的读取进度；Kafka Source 的 `setStartingOffsets` 决定没有已恢复位点时的起点，不覆盖已恢复的分区状态。② sink 的事务或幂等契约覆盖实际副作用。③ 算子状态、输入进度，以及非对齐快照中的在途数据，共同定义恢复点。不要只看配置里有没有写 `EXACTLY_ONCE`。
</details>

**Q5.** 老板问：「我们的实时看板端到端延迟 90 秒，能不能压到 5 秒？」请把 90 秒拆成一条可复算的加法链，并说明压到 5 秒必须放弃什么。

<details><summary>答案</summary>

**加法链**（量级，逐项来自本篇各节）：

| 项 | 量级 | 由谁决定 |
| --- | --- | --- |
| 上游写入间隔（埋点批量/`linger`） | 5–20 s | 客户端攒批 |
| Kafka 可见延迟（`acks=all` + 拉取周期） | 0.5–1.2 s | `→ comp/29 §29.8 账二` |
| **watermark 乱序界 + 窗口长度** | **90–420 s（两至三项中最大）** | **业务对迟到的定价（§34.5）** |
| 每跳 `buffer-timeout` 摊薄 + 下游批量 | 跳数 × 50 ms + 1–10 s | `execution.buffer-timeout`、sink 批量 |

90 秒里**最大的一项是 watermark 乱序界 + 窗口长度**，这两项都不是「性能」，是**口径**。

**压到 5 秒必须放弃的三件事**：

1. **乱序界要压到 1–2 s** ⇒ 迟到超过 2 s 的数据被丢（进侧输出），业务要签「这部分不算数」。
2. **窗口要换成 1 秒滚动窗或处理时间窗** ⇒ 处理时间窗不可重放（failover 后结果会变），「可重放的正确性」被换掉了。
3. **下游批量要压到 100 ms 级** ⇒ 写入 QPS 上升 10–100 倍，下游要能扛（这是成本项，不是配置项）。

**结论**：能压，但那是**换一条链路**（流式告警链路用 processing time + 1 s 窗；报表链路继续用 event time + 5 min 窗），不是「调一个参数」。**「同一套逻辑同时喂告警和报表」这个诉求，本质是要求 watermark 同时取两个值**——这才是这类需求真正的架构答案。
</details>

---

## 34.14 延伸阅读（锚点式）

| 方向 | 去哪 |
| --- | --- |
| Kafka 的日志形态（segment、稀疏索引、零拷贝）：`read_committed` 与 LSO 的物理来源 | [comp/28 Kafka 的日志](28-kafka-log-segments.md) §28.3、§28.7 |
| 复制与一致性（ISR、HW、LEO、acks）：本篇 §34.7 的 `transaction.timeout.ms` 与 LSO 都建立在它上面 | [comp/29 复制与一致性](29-kafka-replication-isr.md) §29.3、§29.8 |
| 投递语义与重复消费：端到端恰好一次的真实边界、幂等三个落点 | [eng/24 投递语义](../eng/24-delivery-semantics.md) §24.2、§24.4 |
| 异步消息的本质与「该不该上 MQ」的七个判据（决定 Flink 的上游是不是 Kafka） | `→ eng/23` |
| 顺序、堆积与 rebalance：写并行度 = 分区数，直接决定 source 并行度上限 | `→ eng/25` |
| 消息可靠性八环节、消息丢了怎么查（与 §34.7 的 sink 语义互补） | `→ eng/26`、`→ eng/27` |
| CDC 的「快照 + 增量」位点衔接：本篇 §34.1 事故三的上游形态 | `→ comp/33` |
| 定时与延迟：Flink 的 timer 服务与时间轮的对照（事件时间定时器 vs 处理时间定时器） | `→ comp/36` |
| 数据湖表格式：checkpoint 提交造成的小文件与 compaction 账单 | `→ comp/35`；`→ architect/21 §21.6` |
| 管道形态选型（Lambda / Kappa / 流批一体）与埋点口径：本篇的 watermark 乱序界应写进数据契约 | `→ architect/11 §11.6` |
| 本套总纲：解剖卡八问、三本放大账与「量级结论 / 承诺值」纪律 | [0-summary](0-summary.md) §0.4、§0.7 |
