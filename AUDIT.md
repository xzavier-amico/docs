# 旧系列技术审校 · 第一轮

> **范围**：`base/`、`go/`、`eng/`、`algo/`、`llm/`、`comp/`、`architect/` 共 443 篇编号正文已做文件级检查；技术事实只定向抽查了相关段落。本轮 16 项已修入正文，不表示未抽查内容已经逐篇核验，也没有重跑文章中的生产基准。
>
> **定位**：下表保留初次审校的行号以便对照原稿；修订后的行号可能已变化。

## 优先修复项（7 项，已修）

| 文件与位置 | 问题 | 建议校正与依据 |
| --- | --- | --- |
| [base/29 · fsync 与崩溃](base/29-fsync-and-crash.md)，280、539、583 行 | 把 ext4 `data=ordered` 写成多块普通文件写入在崩溃后「全新或全旧」。 | 它约束相关数据与元数据的提交顺序，不给普通多块覆盖提供崩溃原子性；文件长度不变时仍可能留下一部分新块、一部分旧块。见 [ext4 日志模式](https://www.kernel.org/doc/html/latest/filesystems/ext4/journal.html)与[原子写入的专门条件](https://www.kernel.org/doc/html/latest/filesystems/ext4/atomic_writes.html)。 |
| [eng/10 · Outbox](eng/10-outbox-and-transactional-message.md)，102、194–202、575 行 | 按 `aggregate_id` 进同一 Kafka 分区，就认为按业务版本投递。 | Kafka 保证的是分区内写入顺序；并行投递者可以跳过被锁定的 v1 先发 v2，v1 进入退避时也会发生。要求严格按版本时，要约束上游投递顺序或在消费端明确缓冲和补齐缺失版本。见 [MySQL 锁定读](https://dev.mysql.com/doc/refman/8.0/en/innodb-locking-reads.html#innodb-locking-reads-nowait-skip-locked)、[Kafka 分区说明](https://kafka.apache.org/43/getting-started/introduction/#main-concepts-and-terminology)。 |
| [eng/14 · 对账](eng/14-reconciliation-system.md)，330–364、538、552、590 行 | 把打开 `join_use_nulls` 和对连接键直接检查 `IS NULL` 当成两种替代办法；误判默认设置的后果。 | 默认未匹配的非 Nullable `String` 键填空串，样例的 `IS NULL` 会漏掉 SHORT/LONG；用 `INNER JOIN` 算金额差的分支根本不会看到未匹配行。为示例明确启用 `join_use_nulls=1`，或改用反连接。见 [ClickHouse JOIN](https://clickhouse.com/docs/sql-reference/statements/select/join#processing-of-empty-or-null-cells)。 |
| [comp/29 · Kafka 副本](comp/29-kafka-replication-isr.md)，468、619 行 | 把 `replica.selector.class` 当成 ISR 或生产者跨可用区确认策略。 | 它用于选择消费者优先读取的副本，不能保证 ISR 中存在远端副本。如果两个本地副本仍在 ISR、远端已退出，`min.insync.replicas=2` 和 `acks=all` 仍可能确认未到远端的数据。见 [Kafka Broker 配置](https://kafka.apache.org/41/configuration/broker-configs/#brokerconfigs_min.insync.replicas)、[KIP-392](https://cwiki.apache.org/confluence/display/KAFKA/KIP-392%3A+Allow+consumers+to+fetch+from+closest+replica)。 |
| [architect/14 · WebSocket 集群](architect/14-websocket-fleet.md)，307–313 行 | 将 Redis `WAIT 1 120ms` 当成故障切换「丢 0 条」的保证。 | 超时参数应是整数毫秒；检查实际确认数，且命令仅覆盖同一连接此前的写入。即便返回 1，故障切换也未必选中收到写入的副本。见 [Redis WAIT](https://redis.io/docs/latest/commands/wait/)。 |
| [llm/47 · 成本工程](llm/47-cost-engineering.md)，69–76、194–205 行 | 计价骨架从总输入减去缓存读取，再按缓存的完整写入单价另收一次，数了两遍写入 token。 | 总输入口径应拆成普通输入、缓存写入、缓存读取三个互斥桶；若已使用 Anthropic 原生的拆分字段，就直接按各桶计价，不再相减。写入单价是普通输入的 1.25 倍时，全部为缓存写入的一批 token 不应算成 2.25 倍。见 [Anthropic Prompt Caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)。 |
| [comp/49 · vLLM 容量估算](comp/49-inference-engine-vllm.md)，561–571、583–593 行 | 32K 算例把 3 条并发下的单请求 Decode 墙钟时间直接计为每请求 GPU 时间，和 3.8K 算例口径不一致。 | 同样的简化假设下，整卡输出吞吐约 3 ÷ 0.0416 ≈ 72.1 Token/s，450 个输出 Token 折约 6.24 GPU 秒，不是 18.7 秒；按原流量和利用率复算约 347 卡，不是 703 卡。这只修正算术，不能用来承诺容量；算例本身的 5.9 秒 Prefill 也已超过首 Token 目标 1.5 秒。见 [vLLM benchmark 指标](https://docs.vllm.ai/en/latest/benchmarking/cli/)。 |

## 其余勘误（9 项，已修）

| 文件与位置 | 问题 | 建议校正与依据 |
| --- | --- | --- |
| [base/15 · vDSO](base/15-privilege-and-virtualization.md)，182、558–564 行 | 断言 `getrandom` 没有也不能有 vDSO 实现。 | Linux 6.11 在 x86 增加 `__vdso_getrandom`；调用者有独立的可写状态，不满足快路径条件时仍走系统调用。见 [v6.11 x86 入口](https://github.com/torvalds/linux/blob/v6.11/arch/x86/entry/vdso/vgetrandom.c)。 |
| [go/52 · 部署](go/52-deployment-sre.md)，347–356 行；[go/19 · 调度](go/19-scheduler-runtime.md)，703–709 行 | Go 1.25 默认不跟随容器 CPU 限额变化，默认值只取 `NumCPU` 与 quota 的较小值。 | 对启用相应默认行为的 Go 1.25 程序，运行时会定期重新算；还考虑 CPU affinity，分数配额向上取整，且仅凭 quota 通常不会把默认值压到 2 以下。早期语言版本的兼容默认值以及显式设置有单独规则。见 [Go 1.25 runtime](https://pkg.go.dev/runtime@go1.25.0#GOMAXPROCS)。 |
| [go/22 · sync.Map](go/22-sync-primitives.md)，373–403、499、510、522 行 | 把 `read/dirty/misses` 快照提升机制当作目标版本的默认实现。 | 这是 Go 1.23 及更早的机制；Go 1.24 默认改用 HashTrieMap。`Range` 的非快照语义仍成立，但旧实现不能直接解释新版性能。见 [Go 1.24 发布说明](https://go.dev/doc/go1.24#syncpkg)、[Go 1.24.0 源码](https://github.com/golang/go/blob/go1.24.0/src/sync/hashtriemap.go)。 |
| [algo/024 · 二维矩阵搜索](algo/024-search-a-2d-matrix-ii.md)，174、244–250、738–741 行 | 把阶梯搜索的 `O(m+n)` 上界写成任意矩阵长宽比下的下界。 | `1×n` 可直接二分，`O(log n)`；原下界证明中的 `r+c=m+n−2` 在零基索引下只有右下角一格。保留阶梯法的上界与方阵最优性，收回任意矩阵的最优性。见 [Richard Bird，Improving on saddleback search](https://dai.fmph.uniba.sk/courses/FPRO/bird_pearls.pdf)。 |
| [algo/150 · 子串搜索](algo/150-find-the-index-of-the-first-occurrence-in-a-string.md)，79–89、664 行 | 等长两串长度各为 10,000、末字符才失配，宣称暴力比较一亿次。 | 示例只检查合法起点，两串等长仅一个起点、比较 10,000 次。对 `m≤n`，示例最坏比较次数是 `(n−m+1)m`；应替换输入及对应倍数结论。见 [Princeton 参考暴力实现](https://algs4.cs.princeton.edu/53substring/Brute.java.html)。 |
| [llm/7 · SSE](llm/7-streaming-transport.md)，45–51、63–65 行 | 把浏览器回传 `Last-Event-ID` 当成自动业务续传和天然幂等。 | 它是可利用的重连游标，不是业务 ACK。服务端要实现保留、回放和缺口处理；业务如怕重复，仍要设计去重。见 [WHATWG SSE](https://html.spec.whatwg.org/multipage/server-sent-events.html#the-last-event-id-header)。 |
| [comp/34 · Flink](comp/34-flink-checkpoint-watermark.md)，4、80、259–260、689 行 | 把 barrier 对齐写成 `EXACTLY_ONCE` 的必要条件，与本篇介绍的非对齐快照矛盾。 | 非对齐的 exactly-once checkpoint 会把在途数据作为 channel state 纳入快照；端到端语义仍依赖源和落地端，不能只看是否对齐。见 [Flink 1.20 非对齐检查点](https://nightlies.apache.org/flink/flink-docs-release-1.20/docs/ops/state/checkpointing_under_backpressure/#unaligned-checkpoints)。 |
| [architect/01 · 容量约束](architect/01-constraint-card-and-capacity.md)，161、173、448、518 行 | 用 Little 定律把 QPS 乘 P99 延迟，直接得出在飞并发数。 | 定律用同一系统边界下的平均到达率与平均停留时间估计平均在飞数；把 P99 代入既得不出平均并发，也得不出 P99 并发或硬上限。见 [Little 原论文](https://pubsonline.informs.org/doi/10.1287/opre.9.3.383)。 |
| [comp/42 · 对象存储](comp/42-object-storage-and-cloud-disk.md)，317–322 行；[architect/25 · 分片上传](architect/25-chunked-upload.md)，174–186 行 | 对 Amazon S3 multipart 对象上限，混写旧的 5 TiB 与精确的 50 TiB。 | 当前 AWS 限制表列 48.8 TiB；10,000 × 5 GiB = 50,000 GiB，约 48.8 TiB。如果应用自行限制每片 512 MiB，则应用上限才约 4.88 TiB。区分历史限制、厂商限制、应用上限。见 [AWS 限制表](https://docs.aws.amazon.com/AmazonS3/latest/userguide/qfacts.html)。 |

## 自动检查与范围限制

- 旧系列的 `check.sh` 检查了 3,313 个相对链接，没有断链或占位符；章节清单与完成状态一致。另查了七套编号正文，未发现编号缺失、重复或可解析的标题编号错位。篇幅偏薄提示不当成事实错误。
- 站点首页原把 `comp/` 标成「20 篇正文」，与目录及总纲的 53 篇初稿不一致；已更正首页入口，不计入上述 16 项技术勘误。
- MkDocs 曾报出旧总索引的 6 个 `progress.sh` 链接在生成站点内不可用：脚本在 Git 仓库里存在，但不进入站点。已在 `028781f` 移除站点总索引中的这些链接；脚本仍留在仓库。
- 平台、组件与架构共定向抽查 15 篇，重点是流式恢复、缓存计价、推理容量、副本确认、checkpoint、对象存储、上传限制与连接容量。其余四套筛查了文件持久性、内核接口、Go 版本语义、Outbox、对账和算法复杂度等高风险段落；`base/47` 有用户已有修改，未作逐条技术复核。
