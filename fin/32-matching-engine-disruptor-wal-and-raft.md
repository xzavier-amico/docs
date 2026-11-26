# 32. 低延迟内存撮合引擎（下）：Disruptor 流水线、WAL 快照与 Raft 主备零丢单

> **状态**：生产级工程篇｜**所属**：第四部分·支付、券商、风控、撮合与链上交易核心架构｜**编号**：32 / 48
>
> **本章定位**：承接 [31 单线程订单簿与零 GC 核心](31-matching-engine-orderbook-and-zero-gc.md)，将确定性撮合器放进可持久化、可复制、可恢复的运行时；同时连接 [24 全链路幂等](24-end-to-end-financial-idempotency.md)、[25 状态机与 Outbox](25-state-machines-tcc-saga-and-outbox.md)、[29 OMS 与 FIX 网关](29-oms-fix-gateway-and-algo-execution.md)、[30 风控与强平](30-pretrade-risk-margin-and-liquidation-engine.md) 和 [33 实时行情](33-market-data-l2-depth-kline-and-ws-fanout.md)。
>
> **覆盖范围**：WAL、fsync 与组提交；Disruptor/Go RingBuffer 的序列屏障、内存序和缓存行填充；CoW 一致性快照；Raft、Aeron Cluster、Dragonboat 与主备 fencing；RPO=0 的故障边界；Go + Java 核心代码；协议与 PostgreSQL DDL；32.8 恢复重放运行手册；压测与混沌、极端边界、术语、面试答题卡、自检题和扩展衔接。
>
> **不可妥协的红线**：ring 发布不是持久化；`write()` 成功不是断电安全；Raft 收到响应不自动等于多数派稳定存储；未提交输入不能发布成交事实；旧主的停止不能只靠自觉；快照必须对应一个完整命令边界；重放不得重新调用时间、随机数或外部风控；ACK 丢失是未知态，不是失败；`RPO=0` 必须声明已确认事实、故障模型和持久化配置，不能宣传成任意灾难下绝不丢失。

---

## 32.1 事故引子：撮合用了 8 微秒，确认的订单仍然丢了

### 32.1.1 从“进程可恢复”到“机器断电丢单”

某市场按第 31 章实现了单写者 OrderBook，撮合热路径达到零分配。团队把订单送入 Disruptor，让 journal、撮合和网络线程并行消费，并宣称“有 WAL、有备机，所以零丢单”。实际时间线如下：

| 时刻 | 动作 | 隐含错误 | 故障结果 |
|---|---|---|---|
| T0 | 命令 8100 发布到 ring | 把 release-store 当成 durable commit | ring 只存在于进程内存 |
| T1 | journal 调用 `write()` | 未执行 fsync 就推进 `durable_seq` | 数据仍可能只在页缓存 |
| T2 | 撮合生成成交并发给客户 | 没有等待持久化/多数派屏障 | 客户看见了一笔未提交成交 |
| T3 | 备机接收到命令 | TCP ACK 被当成应用落盘 ACK | 备机还没持久化 |
| T4 | 主机断电，备机接管 | 按“最大收到序号”启动 | 已确认的 8100 消失 |
| T5 | 旧主网络恢复 | 只检查本机租约时间 | 两个主节点都继续发成交 |
| T6 | 运维加载最新快照 | 后台遍历到新旧状态混合 | 同一订单数量、FIFO 和成交计数互相矛盾 |

这里没有哪次价格比较出错。错误是**把不同安全级别的进度，合并成了一个“处理完成”序号**。

### 32.1.2 唯一真问题

> 在进程崩溃、机器断电、网络分区、旧主复活、ACK 丢失和快照中断时，系统能否证明：所有已确认命令仍在新的权威日志中；每条命令只产生一组确定性经济结果；不确定命令可按原身份查证；只有当前任期获得授权的发布者可以输出新事实？

### 32.1.3 推荐基线：先提交输入，再应用，再发布

```text
客户端/FIX → 网关规范化 → 风控 reservation / 接受事实
                              │ 同一 command_id 重试
                              ▼
                  分区 leader / 规范化输入日志
                              │
                    Raft 多数派稳定存储
                              │ committed commands
                              ▼
                  单写者 Matching State Machine
                              │ 固定 event_no / 稳定业务 ID
                     ┌────────┴─────────┐
                     ▼                  ▼
            可重建的事件发布流        CoW 快照根
                     │                  │
             fencing + 幂等            snapshot + manifest
                     │
             OMS / 账本 / 行情 / 审计
```

本章采用**输入日志作为恢复权威**：记录足够完整的规范化命令，由相同规则版本的确定性状态机重建输出。输出 journal 是发布和审计的加速层，不构造一个未经协调的“输入日志 + 输出日志双主事实”。如果产品要求输出事实也必须独立同步归档，ACK 条件还要增加输出归档屏障，并明确故障时如何从已提交输入补齐。

---

## 32.2 四类进度、三种 ACK 与 RPO=0 的真实含义

### 32.2.1 不要把业务序列等同于 Raft index

| 进度 | 含义 | 单独足以回成功吗 |
|---|---|---|
| `published_seq` | 命令内容已对 ring 消费者可见 | 否，进程重启就消失 |
| `local_durable_seq` | 当前副本已将该连续前缀同步到稳定存储 | 否，未必有权成为新主 |
| `committed_engine_seq` | 对应输入已被共识提交 | 可以确认“输入已持久接纳”，不表示已成交 |
| `applied_engine_seq` | 状态机已经应用的连续命令前缀 | 仍须证明它不超出 committed |
| `released_engine_seq` | 该前缀输出已满足安全条件，可以对外发布 | 用于最终撮合结果 |
| `consumer_event_cursor` | 某个下游已幂等落地的事件前缀 | 只代表该消费者，不代表其他消费者 |

基线状态机满足：

```text
released_engine_seq ≤ applied_engine_seq ≤ committed_engine_seq
snapshot_engine_seq ≤ applied_engine_seq
snapshot 对应的 raft_index ≤ 当时已提交且已应用的 raft_index
```

`raft_index` 包含成员变更、任期内空操作和其他控制记录，不应直接拿来充当连续的市场 `engine_seq`。同一个 Raft group 可承载多个市场，每个市场仍有自己的连续业务序号；本章代码和示例优先采用“一组管理一个撮合分区”，分区可以包含多个市场，但定序域必须明确。

未提交尾部可以被截断。因而“本地见过的最大 index”“收到网络包的最大序号”都不是接管边界。

### 32.2.2 ACK 要声明承诺对象

| 回执 | 明确语义 | 客户端动作 |
|---|---|---|
| `RECEIVED` | 网关收到请求，不保证 durable | 断线后按原 command_id 查证 |
| `DURABLY_ACCEPTED` | 规范化输入已共识提交，取得稳定业务身份 | 订单后续仍可因撮合规则拒绝或部分成交 |
| `MATCH_RESULT` | 已提交输入被应用，结果事件可确定性恢复并获发布授权 | 按稳定事件 ID 更新 OMS，不能把重复回报算成新成交 |

风控的 `OrderAccepted` 和撮合域的 `DURABLY_ACCEPTED` 不应共用含糊的“成功”。跨风控分区与撮合分区不能假设有隐式分布式事务：前者通过 durable outbox 重试投递，后者以 `command_id` 去重；超时期间 reservation 保守保留，只有明确的终态/补偿事实才能释放。

### 32.2.3 RPO=0 是一个带前提的命题

一个可审计的声明可以是：

> 对已返回 `DURABLY_ACCEPTED` 或 `MATCH_RESULT` 的输入，在三投票副本、跨三个独立故障域部署、投票确认依赖稳定存储、日志与任期元数据同步语义经验证、最多一个故障域失效、其余多数派存储保持可读且共识实现满足安全性时，切换不会丢失这些输入；输出由固定版本状态机恢复。失去多数派时停写，不通过强制选主继续承诺 RPO=0。

| 故障模型 | 可否给出该承诺 | 必要条件 |
|---|---|---|
| 单进程崩溃 | 可以 | 日志和共识元数据真实持久化 |
| 单机器/单盘永久损坏 | 可以 | 多数派复制，不共享同一盘/宿主机 |
| 单可用区消失 | 可以 | 幸存投票节点仍构成多数派，日志在独立 AZ |
| 机房故障但远端只有异步副本 | 不可以 | 未复制尾部可能丢失；只能按真实 RPO 披露 |
| 三副本全部断电后恢复 | 需要额外验证 | 每个已确认副本稳定存储均履约，且没有超出模型的介质损坏 |
| 多数派磁盘同时损毁 | 不可以 | 不在三副本容忍一个故障的模型内 |
| 共同软件错误、管理员误删 | 不可以靠 Raft 解决 | 不可变备份、审批、异构校验与灾备演练 |

两个节点可以做同步主备，但 2/2 法定人数会使任意一节点失效后无法自动安全写入；第三个仅投票、不保存完整业务日志的 witness，不能被当成一个完整持久副本。跨地域同步复制可以保护地域故障，却必须承担网络往返和 quorum 尾延迟，不能同时承诺任意距离的微秒确认。

---

## 32.3 WAL 与 fsync：把“写过”变成“能恢复”

### 32.3.1 存储栈的五个位置

```text
应用编码 buffer
   → 内核页缓存（write 返回）
   → 文件系统/块层提交
   → 控制器/设备缓存
   → 按设备承诺可断电保存的介质或受保护缓存
```

`fsync(fd)` 请求同步文件数据及必要元数据，`fdatasync(fd)` 可减少与数据恢复无关的元数据同步；实际语义依赖 OS、文件系统、挂载方式、虚拟化和设备。必须验证 flush/FUA、断电保护（PLP）和云盘持久性契约，不能把“NVMe 很快”当成证明。

- `mmap` 写入后没有正确同步，不等于 durable；内存映射也不自动消除恢复协议。
- `O_DIRECT` 绕过页缓存，不等于绕过设备易失缓存，也不自动提供事务原子性。
- 关闭文件不是提交协议；日志库叫 `flush()` 也不一定下发 fsync。
- 新建/rename 文件时，文件内容同步与目录项同步是两件事；在目标 Linux 文件系统上验证 `fsync(dirfd)`，不能只同步文件。
- `kill -9` 仅杀进程，页缓存仍在；它不能替代虚拟机硬重启、节点掉电或块设备故障注入。

### 32.3.2 WAL 记录与组边界

最小二进制 framing 可为：

```text
SegmentHeader:
  magic, format_version, group_id, partition_id, segment_id,
  first_engine_seq, previous_segment_hash, encryption_key_id

RecordHeader（下面 Go 示例使用 36 bytes，小端）:
  magic:u32, version:u16, kind:u16,
  payload_len:u32, partition_id:u32, engine_seq:u64, origin_term:u64,
  crc32c:u32
Payload:
  CanonicalCommand / RuleChange / MarketStateChange

生产封装还应定义：batch_id、批尾长度/计数/摘要、截断规则、最大长度、对齐与 seal。
```

CRC32C 用于发现随机损坏和 torn write，不提供对抗篡改能力；审计需额外采用哈希链、签名和独立归档。长度字段必须先做上限校验，再分配/读取，防止损坏日志让恢复程序申请数 TB 内存。

命令本身是一个应用原子边界。一笔大单可能生成十万条成交；输出可以分帧传输，但必须带 `event_count` 和命令结束标记，下游不能将缺尾命令误认为完整状态。若采用跨多个物理记录的原子输入，必须有明确批提交标记，恢复时只应用完整批。

### 32.3.3 组提交不是异步丢单

```text
收集最多 N 条 / B 字节 / 等待最多 Δt
    → 完整编码 → 顺序 write 全部字节
    → fdatasync/fsync 成功
    → 推进 local_durable_seq
    → 共识层推进 commit（若尚未完成）
    → 应用 / 发布 / ACK
```

组提交的收益是多条输入共享一次同步成本，代价是排队等待。必须同时限制**条数、字节和最长等待时间**，低流量时也要到期提交；“只在满 1,024 条时 fsync”会把最后一笔永远留在内存。

示例：100 µs 最大攒批等待、64 KiB 最大批次、最多 256 条，只是容量起点；实际值需按同机磁盘和网络测量。`endOfBatch` 表示消费者当前追到了一批可见事件的末端，不是“已经得到多数派持久化”。

发生 `ENOSPC`、`EIO`、short write 或 fsync 失败时，不得推进 durable 水位，立即停该 WAL writer 并停止成功回执。失败前可能已经写入部分数据，所以客户端仍可能需要 UNKNOWN 查证，不能一概告诉客户“绝对未发生”。

### 32.3.4 Go：有界组提交与失败封闭

下面是**单 writer、单分区、本地 WAL** 核心示例，不是 Raft 实现。文件已由上层按 segment 协议创建并同步目录，`last` 来自可信恢复边界；记录编码不做热路径扩容。错误文本与启动分配不属于撮合热路径。

```go
package durability

import (
    "encoding/binary"
    "errors"
    "hash/crc32"
    "io"
    "os"
)

const headerBytes = 36
const magic uint32 = 0x57414c31
var castagnoli = crc32.MakeTable(crc32.Castagnoli)
var ErrBatch = errors.New("invalid or oversized WAL batch")
var ErrPoisoned = errors.New("WAL writer requires recovery")

type Record struct {
    Kind uint16
    Partition uint32
    Seq, Term uint64
    Payload []byte // 调用返回前不可修改；writer 不保留该引用
}

type WAL struct {
    f *os.File
    scratch []byte
    durable uint64
    failed bool
}

func NewWAL(f *os.File, maxBatchBytes int, last uint64) *WAL {
    if maxBatchBytes < headerBytes { panic("WAL batch too small") }
    return &WAL{f: f, scratch: make([]byte, maxBatchBytes), durable: last}
}

func writeAll(f *os.File, b []byte) error {
    for len(b) > 0 {
        n, err := f.Write(b)
        if n > 0 { b = b[n:] }
        if err != nil { return err }
        if n == 0 { return io.ErrNoProgress }
    }
    return nil
}

// 不允许并发调用。全部预校验在任何 I/O 前完成。
func (w *WAL) AppendAndSync(rs []Record) (uint64, error) {
    if w.failed { return w.durable, ErrPoisoned }
    if len(rs) == 0 { return w.durable, nil }
    used, expected := 0, w.durable
    partition := rs[0].Partition
    for i := range rs {
        r := &rs[i]
        if expected == ^uint64(0) || r.Seq != expected+1 ||
            r.Partition != partition ||
            uint64(len(r.Payload)) > uint64(^uint32(0)) ||
            len(w.scratch)-used < headerBytes ||
            len(r.Payload) > len(w.scratch)-used-headerBytes {
            return w.durable, ErrBatch
        }
        used += headerBytes + len(r.Payload)
        expected = r.Seq
    }
    off := 0
    for i := range rs {
        r := &rs[i]
        h := w.scratch[off : off+headerBytes]
        binary.LittleEndian.PutUint32(h[0:4], magic)
        binary.LittleEndian.PutUint16(h[4:6], 1)
        binary.LittleEndian.PutUint16(h[6:8], r.Kind)
        binary.LittleEndian.PutUint32(h[8:12], uint32(len(r.Payload)))
        binary.LittleEndian.PutUint32(h[12:16], r.Partition)
        binary.LittleEndian.PutUint64(h[16:24], r.Seq)
        binary.LittleEndian.PutUint64(h[24:32], r.Term)
        body := w.scratch[off+headerBytes : off+headerBytes+len(r.Payload)]
        copy(body, r.Payload)
        c := crc32.Update(0, castagnoli, h[:32])
        c = crc32.Update(c, castagnoli, body)
        binary.LittleEndian.PutUint32(h[32:36], c)
        off += headerBytes + len(body)
    }
    if err := writeAll(w.f, w.scratch[:used]); err != nil {
        w.failed = true
        return w.durable, err
    }
    if err := w.f.Sync(); err != nil {
        w.failed = true
        return w.durable, err
    }
    w.durable = expected // 只有 Sync 成功才能推进
    return w.durable, nil
}
```

生产版本还必须把 `partition_id` 固定在 writer 实例上，验证 record kind/version，预留故障和撤单空间，实现 segment 轮转、封口、目录同步、加密与恢复扫描；调用方不能在出错后把 `failed` 清零继续写。为便于展示，示例批间没有哈希链，也没有批事务提交标记：每条输入单独构成命令边界，不能把整个 `rs` 误解为跨命令事务。

**接入 Raft 时通常由共识库管理日志存储，而不是让应用再写一套独立“权威 WAL”。** 若保留这类应用 journal，须明确它只是审计/输出加速层，或严格接入共识库定义的存储接口；本地 `AppendAndSync` 返回不构成集群提交证明。

---

## 32.4 Disruptor 与 Go RingBuffer：并行的是阶段，不是订单簿写者

### 32.4.1 两种管线不能混为一谈

**保守模式：提交后应用。**

```text
Ingress → 规范化/定序 → durable quorum → Matcher → Release → 网络/投影
                                         │
                                         └→ 快照/状态摘要
```

**推测模式：撮合与持久化并行。**

```text
                     ┌→ WAL / replication → committed_seq ─┐
Sequenced command ───┤                                     ├→ Release barrier
                     └→ speculative matcher → applied_seq ─┘
```

推测模式可以隐藏一部分 fsync 延迟，但必须同时满足：

1. 输出 buffer 在提交之前完全私有，不能进入行情、OMS、账本或客户网络；
2. `release ≤ min(committed, applied)`，按连续命令前缀开放，不能逐条越过洞；
3. 失败时整个推测簿和未发布输出可丢弃，再从已提交边界恢复；
4. 快照不能包含未提交状态，除非能从版本根精确选择已提交切面；
5. 未提交输出不能驱动新的风险/强平命令，否则产生级联外部副作用；
6. 内存容量可以容纳最坏提交延迟内的推测输入和输出。

默认优先保守模式。它让 `Apply` 输入天然都是 committed，明显减少恢复与切换的证明成本。不要为了省几微秒，先增加一个没有实现回滚/隔离的推测状态机。

### 32.4.2 Disruptor 的三个概念

- **Sequencer**：分配 ring sequence，解决哪个生产者可以写哪个槽位；sequence 不是市场业务序列，也不是 Raft index。
- **SequenceBarrier**：消费者必须等待哪些前置阶段；可见不等于落盘，屏障语义由阶段正确实现来保证。
- **GatingSequence**：限制生产者覆写，只有依赖该槽的所有必要消费者完成，槽位才能复用。

单输入定序器可用 `ProducerType.SINGLE`；网络层是多生产者时应先汇聚，或使用正确的 MULTI sequencer。不能把 `SINGLE` 当成“一般情况下只有一个线程”而偶尔并发调用。

事件槽属于 ring，不属于业务调用者。下游异步任务不能保留 `event` 对象或其可复用字节数组指针；必须复制到自己拥有的预分配区，或者让其完成序号参与 gating。

### 32.4.3 Java：本地 durable barrier 的最小示例

以下示例针对 LMAX Disruptor 4.x 风格 API，演示“前置 journal 失败后，不得让 matcher 越过它”。它仅提供**本地 durable** 顺序，不提供 Raft 多数派保证。接入集群时，应将“已提交输入”作为这条管线的入口，或用共识库提交回调驱动前置屏障。

```java
import com.lmax.disruptor.*;
import com.lmax.disruptor.dsl.*;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.util.concurrent.ThreadFactory;

final class CommandSlot {
    long engineSeq;
    int length;
    final byte[] payload = new byte[4096];
    final ByteBuffer view = ByteBuffer.wrap(payload);
}

final class DurableStage implements EventHandler<CommandSlot> {
    private final FileChannel channel;
    private long pending = -1;
    volatile long durableEngineSeq = -1;

    DurableStage(FileChannel channel) { this.channel = channel; }

    @Override
    public void onEvent(CommandSlot e, long ringSeq, boolean endOfBatch)
            throws Exception {
        // payload 在入环前已包含 framing、CRC、完整规范化命令。
        ByteBuffer b = e.view;
        b.clear().limit(e.length);
        while (b.hasRemaining()) {
            int n = channel.write(b);
            if (n == 0) throw new IllegalStateException("WAL made no progress");
        }
        pending = e.engineSeq;
        if (endOfBatch) {
            channel.force(false); // 文件已存在；目录项同步由 segment 管理器负责
            durableEngineSeq = pending;
        }
    }
}

final class StopOnError implements ExceptionHandler<CommandSlot> {
    public void handleEventException(Throwable ex, long seq, CommandSlot e) {
        // 配套 supervisor 必须关闭 ingress；不能 log 后返回继续推进序号。
        throw new IllegalStateException("halt partition", ex);
    }
    public void handleOnStartException(Throwable ex) {
        throw new IllegalStateException("start failed", ex);
    }
    public void handleOnShutdownException(Throwable ex) {
        throw new IllegalStateException("shutdown failed", ex);
    }
}

final class LocalPipeline {
    static Disruptor<CommandSlot> start(FileChannel wal,
                                       EventHandler<CommandSlot> matcher,
                                       EventHandler<CommandSlot> release) {
        ThreadFactory threads = r -> {
            Thread t = new Thread(r, "matching-stage");
            t.setDaemon(false);
            return t;
        };
        Disruptor<CommandSlot> d = new Disruptor<>(
            CommandSlot::new, 1 << 16, threads,
            ProducerType.SINGLE, new BusySpinWaitStrategy());
        d.setDefaultExceptionHandler(new StopOnError());
        DurableStage durable = new DurableStage(wal);
        d.handleEventsWith(durable).then(matcher).then(release);
        d.start();
        return d;
    }
}
```

Disruptor 的 batch processor 在正常完成当前可用批后才公开完成进度，因此 matcher 等待 journal 阶段时，可以以批为单位被 durable 屏障阻挡。仍需对所用版本源码与异常处理路径验证：**错误处理若吞异常继续返回，就可能错误地推进处理进度**。若自定义 processor 每条推进序号，必须显式用 `durableEngineSeq` 做门禁，不能照搬上述假设。

示例把 matcher/release 作为接口参数，生产实现要保证它们不保存槽引用，并把生成事件放在有界、具有独立生命周期的输出区。输入帧必须在 claim 前完成大小和编码校验；claim 后异常不能留下永不发布的洞，也不能发布一半内容。生产者还应使用有界等待/`tryNext` 实现准入，而不是 ring 满后无限阻塞网络线程。

`BusySpinWaitStrategy` 适用于有独占核和足够容量的低延迟场景；共享 CPU、容器配额或超售环境宜测量 yielding/blocking 方案。忙等不能降低磁盘 fsync 的物理延迟，反而可能抢占 journal/网络线程。

### 32.4.4 Go：真正带内存序的 SPSC ring

第 31 章中的简化 ring 表达槽位复用，本节补齐跨 goroutine 发布与复用屏障。以下仅支持**一个生产者、一个消费者**，通过传值避免把可复用槽指针交给外部。

```go
package transport

import "sync/atomic"

// 此处按 64B cache line 设计。前后填充降低邻居字段伪共享风险；
// 它不保证 Go 堆地址恰好 64B 对齐，也不适合直接推导所有 CPU 的布局。
type paddedCounter struct {
    _ [64]byte
    value atomic.Uint64
    _ [56]byte
}

type Command struct {
    EngineSeq uint64
    CommandID [16]byte
    Kind uint32
    Size uint32
    Body [256]byte // 固定宽度示例；大输入使用独立、受所有权约束的 slab
}

type Ring struct {
    slots []Command
    mask uint64
    published paddedCounter // 只有生产者写
    consumed paddedCounter  // 只有消费者写
}

func NewRing(n int) *Ring {
    if n < 2 || n&(n-1) != 0 { panic("capacity must be power of two") }
    return &Ring{slots: make([]Command, n), mask: uint64(n-1)}
}

// false 表示本次没有入环；上游保持原 command_id，并实施背压。
func (r *Ring) TryPush(c Command) bool {
    p := r.published.value.Load()
    h := r.consumed.value.Load()
    if p-h == uint64(len(r.slots)) { return false }
    r.slots[p&r.mask] = c
    r.published.value.Store(p+1) // 完整写槽后才发布
    return true
}

func (r *Ring) TryPop(out *Command) bool {
    h := r.consumed.value.Load()
    if h == r.published.value.Load() { return false }
    *out = r.slots[h&r.mask] // 复制完后才允许生产者复用槽
    r.consumed.value.Store(h+1)
    return true
}
```

Go 标准原子操作提供顺序一致性，其同步关系使“先写数据、再发布计数”对消费者可见；不能把其中一边改为普通读写，也不能凭“x86 很强”忽略 Go 内存模型。计数按无符号模数计算，容量远小于半个序号空间；进程内不允许外部重置计数，跨重启由持久化业务序列恢复，不能复用这个 ring 计数作为业务身份。

把多个生产者接到 `TryPush` 会发生覆盖，哪怕计数器都是 atomic 也不安全。MPSC/MPMC 需要额外 claim、每槽发布序号和缺口处理，优先采用经过验证的实现；`sync/atomic` 不会自动把普通算法变成并发算法。测试需同时验证 FIFO、无重复、无缺失、ring 反复绕回和 `go test -race`。

### 32.4.5 缓存行填充、NUMA 与伪共享

两个核分别更新相邻 `head`、`tail`，即使逻辑上互不争用，也可能不断转移同一缓存行的独占所有权。填充是隔离**高频独立写字段**，不是给每个字段浪费一整行。

| 手段 | 解决什么 | 不能解决什么 |
|---|---|---|
| head/tail 分离与 padding | 降低 false sharing | 真正的数据共享、跨 NUMA 访问 |
| 每生产者局部 claim 缓存 | 减少共享原子流量 | MPSC 的发布洞与正确性 |
| 单写者顺序批处理 | 提升局部性、减少屏障频率 | 超大批次造成的尾延迟 |
| CPU affinity / NUMA 本地分配 | 减少迁核和远端内存 | OS 抢占、中断、SMT 干扰 |
| Java `@Contended` 或库内 padding | 分离高频计数器 | 未启用相应 JVM 配置时的布局保证 |

缓存行常见为 64B，但不能硬编码成跨架构真理。Go 用 `unsafe.Sizeof/Offsetof` 检查布局，Java 用 JOL/实际字段布局检查，并以 `perf c2c`、cache-miss、cycles/op 和 p99 验证；不只比较“加 padding 后吞吐数字好看”。

---

## 32.5 CoW 快照：冻结一个序列，而不是扫描一片正在变化的内存

### 32.5.1 快照必须覆盖所有会影响未来输出的状态

除了价格树和订单，还要保存：

```text
group / partition / market 身份
snapshot_id, engine_seq, raft_index, raft_term
规则、编码器、tick/lot、市场状态版本与 hash
订单槽、generation、arrival_seq、价位 FIFO、索引恢复材料
match_seq / event_seq / last_trade_price / 当前交易日
command_id 去重状态、payload_hash、原结果或结果定位信息
确定性计时器/条件单状态（若运行时支持）
容量参数、空闲槽恢复材料、页目录/格式版本
内容摘要、文件清单、压缩/加密参数、密钥引用
```

L2 十档、当前订单表和“最新 best bid/ask”都不是完整恢复快照。去重状态若没保存，重启后原命令可能被再次执行；保留期至少覆盖客户端重试、灾备回放和下游补偿窗口，超窗身份应返回“已过查询窗口/需归档查证”，不能静默按新请求执行。

### 32.5.2 应用级页 CoW 的安全算法

采用固定大小逻辑页，每页包含一组订单槽/价位槽；活跃根 `R` 是 page-id → page-buffer 映射，撮合线程是唯一修改者。

1. 将 `SnapshotBarrier(S)` 放入已提交命令流，在**完整应用 S 之后、应用 S+1 之前**执行。
2. 固定该时刻的根 `R_S` 和序列元数据，旧页在快照生命周期内不可修改。
3. 后续命令第一次写到共享页 P 时，从有界预分配池取新页 P'，复制 P，修改活跃根映射后只写 P'。
4. 快照线程只读取 `R_S` 所引用的不可变页，不遍历活跃订单簿的变化链接。
5. 页数据写临时文件、计算 hash，文件同步后发布 manifest，再同步目录。
6. manifest 成为有效恢复点后解除 pin；所有读取者退出后才回收旧页。

```text
S 时刻： R_active ──► P0, P1, P2
         R_snapshot ─► P0, P1, P2

写 P1 后：R_active ──► P0, P1',P2
          R_snapshot ─► P0, P1, P2  （始终代表 S）
```

“冻结根”本身也需要一致性协议：可以在命令边界短暂停顿复制页表，或用持久化 radix/page table。若根表复制是 O(页数)，必须纳入暂停预算，不能宣传成无条件 O(1)。不能只冻结树根，却让树节点、订单数组和 ID map 继续原地变化。

页引用计数、epoch reclamation 和快照线程退出都需要明确同步；CoW 不是让两个线程同时写同一页。失败时丢弃未发布快照，原 WAL 仍然是恢复基础。快照耗尽 CoW 池时，应取消快照并安全释放 pin、限流，或按运行手册停写；不能回收仍被快照读取的页。

### 32.5.3 不优先采用 Java/Go 进程直接 fork 快照

OS fork 的地址空间 CoW 很诱人，但 JVM/Go 是多线程运行时，继承锁、运行时内部状态、子进程可执行动作和 fork 后内存行为都有约束，不能照搬单线程 C 程序的做法。透明大页、密集修改和内存压力还可能导致巨大复制与延迟尖峰。

优先使用应用级不可变页/双缓冲，或由共识库提供的正确快照接口。无法承担复杂 CoW 时，可在受控停市窗口复制完整快照，用可测量的暂停替代一个无法证明正确的“无停顿方案”。

### 32.5.4 manifest 发布和日志回收顺序

```text
写 snapshot.tmp
 → 校验页/文件摘要
 → fsync(snapshot.tmp)
 → 同文件系统 rename 为不可变 snapshot 文件
 → fsync(snapshot 目录)
 → 写 manifest.tmp（包含全部文件及 hash）
 → fsync(manifest.tmp)
 → rename manifest.tmp 为正式 manifest
 → fsync(manifest 目录)
 → 通知共识/保留策略这是有效恢复点
 → 满足复制、归档、消费者回放要求后才回收旧日志
```

跨文件系统 rename 不能提供上述原子替换语义；对象存储也不照搬 POSIX 目录 fsync，应使用不可变对象、校验和、版本化指针及其一致性契约。多文件快照通过最终 manifest 一次引用完整文件集，恢复时不读“目录里名字最新的那一堆文件”。

共识日志 compaction 使用其已安装快照边界；审计命令归档、输出重放保留和下游游标是不同保留需求，不能仅因本机拍完快照就删除所有历史。至少保留一个已验证旧恢复点及所需日志，防止最新快照在真正恢复时才被发现不可读。

### 32.5.5 容量与恢复时间计算

```text
额外 CoW 内存 ≈ 快照期间被首次修改的页数 × 页大小 + 根/元数据开销
快照时间 ≈ 快照字节数 / 实际序列化及持久写入吞吐
重放时间 ≈ 待重放命令数 / 含校验的实际重放速率
RTO ≈ 发现故障 + 选主 + 状态追赶/加载 + 重放 + 校验 + fencing/路由切换
```

若输入 40 万条/秒、快照每 30 秒一次，恢复最坏要重放约 1,200 万条；若含校验重放只有 100 万条/秒，单这一项就是约 12 秒，不能承诺 2 秒冷恢复。热备已持续应用和冷备加载快照是两套 RTO，必须分别压测。

---

## 32.6 Raft、Aeron Cluster 与 Dragonboat：选一种权威顺序

### 32.6.1 Raft 提供什么，应用仍需提供什么

Raft 提供复制日志的共识顺序、任期与选举规则。其安全性依赖日志/投票元数据按实现要求持久化，leader 仅在正确条件下推进 commit，并通过合法配置变更维持 quorum 交集。

应用仍要负责：

- 定点整数、稳定排序、规则版本和输入规范化，保证副本应用一致；
- `command_id` 去重与 payload 冲突检测，防止客户端重试变成两次交易；
- 状态快照完整性、兼容升级与确定性重放；
- 外部输出 fencing、幂等消费、ACK 与业务状态的区别；
- 故障域布局、容量、日志归档、审计与运行手册。

**一条 committed 命令不一定每次都只执行一次回调。** 进程可能在应用后、记录应用进度前崩溃，恢复重放再次执行；安全性来自确定性状态与稳定事件身份，不能靠回调次数来推导恰好一次经济效果。

### 32.6.2 Java 路线：Aeron Transport ≠ Aeron Cluster

| 组件 | 主要职责 | 不应误认为 |
|---|---|---|
| Aeron Transport | 低延迟消息传输、流控和重传等 | 多数派共识或断电持久化 |
| Aeron Archive | 流录制与重放 | 自动提供集群选主和业务提交 |
| Aeron Cluster | 基于复制日志/Raft 机制的有序集群服务、leader 切换 | 任意默认配置都满足本章断电 RPO=0 |
| Disruptor / Agrona | 进程内低延迟数据传递、内存布局 | 集群权威日志 |

Aeron Cluster 的 clustered service 应把匹配逻辑作为确定性状态机，计时器/会话/输入使用集群提供的有序事件；不要在服务回调中再次读取外部数据库决定是否成交。

必须核对固定版本的 Archive 文件同步选项（如 `fileSyncLevel` 的具体语义）、日志/mark file 的配置、复制确认路径和部署文档，并做断电试验。**看到“Archive 已记录”或“Cluster 已提交”，不意味着可以跳过设备持久性与配置验证。** 还要确认所选配置的提交确认是否满足投票副本稳定存储要求，而不是只确认 follower 复制进了页缓存；不满足时应降低承诺或改用适配的持久化路径。

### 32.6.3 Go 路线：Dragonboat 状态机适配

Dragonboat 提供多 Raft group、状态机和快照能力。需按项目固定 major version 的 `IStateMachine`、并发/磁盘状态机接口和快照取消协议实现，不应混用网上不同版本的接口签名。

```text
Propose(canonical command)
  → consensus 内部日志持久化/复制
  → 命令提交
  → Update/批量 Update：校验 engine_seq，确定性应用，产生稳定结果
  → proposal 结果返回

Snapshot：获取一致边界 → 保存完整状态 → 校验/安装
Lookup：提供明确一致性级别的查询，不在 follower 上随意返回旧状态
```

以下是**应用适配伪代码**，刻意不冒充某一版 SDK 可直接编译的接口：

```go
// ApplyCommitted 只由选定状态机模型允许的串行应用路径调用。
func ApplyCommitted(cmd CanonicalCommand) Result {
    if old, ok := dedup.Find(cmd.CommandID); ok {
        if old.PayloadHash != cmd.PayloadHash { return Conflict() }
        return old.Result
    }
    Require(cmd.EngineSeq == state.LastEngineSeq+1)
    Require(cmd.RuleHash == state.ActiveRuleHash)
    result := matcher.Apply(cmd) // 无时钟、网络、非确定性 map 顺序
    state.LastEngineSeq = cmd.EngineSeq
    dedup.Put(cmd.CommandID, cmd.PayloadHash, result)
    // 不在这里直接向银行/账本发起不可幂等操作。
    // 输出用 (partition, engine_seq, event_no) 定位，发布器另做 fencing。
    return result
}
```

去重检查应早于“必须下一个 engine_seq”的新请求检查，因为重试可能携带旧序号。leader 在提案前的临时缓存只能做加速，权威去重必须进入复制状态；两个并发重复请求即便都被提案，也只能有一个改变订单簿。对于已经进入日志的重复包装记录，要明确定义它是否消耗业务序号：推荐 leader 合并同身份请求，状态机的重复记录返回原结果，不给同一业务操作分配新的订单优先级；日志位置与有效业务序列映射须可恢复。

### 32.6.4 主备设计与跨分区边界

- **热备**：follower 持续应用已提交命令，接管主要耗在选举、少量追赶、fencing 和路由更新；它不是一个随意可写的第二主。
- **温/冷备**：保存日志或快照，接管需要恢复和重放，RTO 通常更长。
- **多市场分片**：每组内部有序，不自动产生跨所有市场的全局总序；跨市场组合订单需专门协调或明确“不保证原子同时成交”。
- **两套共识不要无意嵌套**：把 Aeron Cluster 再套在 Dragonboat 外并不会自然更安全，只会增加双重提交、快照和成员变更边界。
- **成员替换**：使用库支持的配置变更流程，新副本先追赶再参与投票；不可同时移走多数派，然后以“紧急模式”重建成多个可写集群。

---

## 32.7 fencing、协议与 DDL：让旧主没有副作用的能力

### 32.7.1 fencing 必须由接收方强制执行

旧主可能经历长时间 GC/STW、CPU 饥饿或网络分区，它不会可靠地在失权瞬间自杀。只在发送前检查本机 `isLeader`，检查与实际写出之间仍可能发生切主。

建议把发布授权定义为：

```text
Fence = (cluster_incarnation, partition_id, owner_epoch)
StableEventID = (partition_id, engine_seq, event_no)
Origin = (raft_term, raft_index)
```

`owner_epoch` 必须从权威共识/接管协议单调分配并在接收侧持久化；若直接使用 Raft term，必须保证它与该发布授权一一对应，且在“从备份重建新集群”时不会回退。集群 incarnation 需要权威登记/批准关系，不能把任意 UUID 按字典序比较大小就当成更新的集群。

新主接管顺序：

1. 确认新任期和必要的当前任期提交/线性化屏障，应用到已知提交位置；
2. 获得新 `owner_epoch`，向输出边界安装 fence，等待必要接收方确认；
3. 输出边界拒绝更旧 epoch 的**新副作用**；同一事务中校验 epoch、事件幂等键并落地；
4. 新主从持久消费游标续发，重发缺失结果时使用新发布 epoch，但保留原 StableEventID/Origin；
5. 更新网关路由并开放新请求；旧主恢复只允许成为 follower/隔离实例。

fencing 不能“撤销已经被接收的合法事实”。一个旧主迟到发送的、确已提交且具有稳定身份的事件，可以被边界拒绝并由新主补发，不应生成新成交 ID；若旧主把未提交事件泄露到无法校验的公共通道，fencing 也不能神奇挽回，因此必须首先保证 release barrier 正确。

对于不能直接识别 token 的 Kafka/外部推送等通道，可设置唯一受 fence 约束的 egress gateway，或结合目标系统的生产者隔离机制。不能只把 token 放进消息 header，却没有任何接收方验证。

### 32.7.2 输入与输出 envelope

```protobuf
syntax = "proto3";

message DurableCommand {
  uint32 schema_version = 1;
  string partition_id = 2;
  string market_id = 3;
  bytes command_id = 4;           // 固定 16 bytes，重试不变
  uint64 engine_seq = 5;          // 本市场有效命令的连续序列
  bytes canonical_payload = 6;    // 包含完整 MatchingCommand
  bytes payload_hash = 7;         // 对明确规范化字节计算，不含易变路由字段
  bytes rule_hash = 8;
  bytes reservation_id = 9;
  uint64 accepted_seq = 10;
  uint64 logical_time_ns = 11;    // 若影响规则，必须成为输入事实
}

message MatchOutput {
  string partition_id = 1;
  string market_id = 2;
  uint64 engine_seq = 3;
  uint32 event_no = 4;
  uint32 event_count = 5;
  uint64 raft_index = 6;
  uint64 origin_term = 7;
  uint64 owner_epoch = 8;         // 本次发布授权，可不同于 origin_term
  bytes cluster_incarnation = 9;
  uint32 kind = 10;
  bytes payload = 11;
  bytes payload_hash = 12;
  bytes state_hash_after_command = 13; // 一般在命令末尾/检查点携带
}
```

业务 ID 若要使用 uint64，须处理 PostgreSQL `bigint` 的有符号范围；以下 DDL 把所有序列限制为 `0..2^63-1`，在耗尽前受控迁移，不能让 Go/Java 数值溢出后写库失败。Protobuf 编码也不能默认是跨语言规范化格式：明确字段默认值、未知字段、map 排序、重复字段和字符串归一化规则，或直接保存唯一规范化字节串。

多市场分区的 StableEventID 要加入 `market_id`，或者定义分区级唯一 `command_seq`。为避免两个市场 engine_seq 同号冲突，以下表和事件去重统一使用 `(partition_id, market_id, engine_seq, event_no)`。

### 32.7.3 PostgreSQL DDL：权威日志的投影与发布证据

以下表不替代共识存储。命令镜像、快照 manifest、发布游标和 fence 是运维/审计材料；SQL 数据库若作为输出边界，fence 安装和消费必须使用同一数据库事务序列化。

```sql
CREATE TABLE engine_command_mirror (
    partition_id text NOT NULL,
    market_id text NOT NULL,
    engine_seq bigint NOT NULL CHECK (engine_seq > 0),
    command_id uuid NOT NULL,
    raft_index bigint NOT NULL CHECK (raft_index > 0),
    origin_term bigint NOT NULL CHECK (origin_term > 0),
    payload_hash bytea NOT NULL CHECK (octet_length(payload_hash) = 32),
    rule_hash bytea NOT NULL CHECK (octet_length(rule_hash) = 32),
    payload bytea NOT NULL,
    PRIMARY KEY (partition_id, market_id, engine_seq),
    UNIQUE (partition_id, command_id)
);

CREATE TABLE engine_snapshot_manifest (
    partition_id text NOT NULL,
    snapshot_id uuid NOT NULL,
    applied_raft_index bigint NOT NULL CHECK (applied_raft_index >= 0),
    applied_raft_term bigint NOT NULL CHECK (applied_raft_term >= 0),
    market_sequences jsonb NOT NULL, -- 每市场业务切面；hash 时使用规范化表示
    format_version integer NOT NULL CHECK (format_version > 0),
    rules_hash bytea NOT NULL CHECK (octet_length(rules_hash) = 32),
    content_hash bytea NOT NULL CHECK (octet_length(content_hash) = 32),
    manifest_uri text NOT NULL,
    byte_size bigint NOT NULL CHECK (byte_size >= 0),
    status text NOT NULL CHECK (status IN ('VERIFIED','RETIRED')),
    verified_at timestamptz NOT NULL,
    PRIMARY KEY (partition_id, snapshot_id)
);

CREATE TABLE engine_output_fence (
    partition_id text PRIMARY KEY,
    cluster_incarnation uuid NOT NULL,
    owner_epoch bigint NOT NULL CHECK (owner_epoch > 0),
    leader_id text NOT NULL,
    installed_at timestamptz NOT NULL
);

CREATE TABLE engine_event_inbox (
    consumer_id text NOT NULL,
    partition_id text NOT NULL,
    market_id text NOT NULL,
    engine_seq bigint NOT NULL CHECK (engine_seq > 0),
    event_no integer NOT NULL CHECK (event_no >= 0),
    event_count integer NOT NULL CHECK (event_count > 0),
    payload_hash bytea NOT NULL CHECK (octet_length(payload_hash) = 32),
    origin_term bigint NOT NULL CHECK (origin_term > 0),
    accepted_owner_epoch bigint NOT NULL CHECK (accepted_owner_epoch > 0),
    applied_at timestamptz NOT NULL,
    PRIMARY KEY (consumer_id, partition_id, market_id, engine_seq, event_no),
    CHECK (event_no < event_count)
);

CREATE TABLE engine_consumer_checkpoint (
    consumer_id text NOT NULL,
    partition_id text NOT NULL,
    market_id text NOT NULL,
    complete_engine_seq bigint NOT NULL CHECK (complete_engine_seq >= 0),
    checkpoint_hash bytea NOT NULL CHECK (octet_length(checkpoint_hash) = 32),
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (consumer_id, partition_id, market_id)
);
```

### 32.7.4 正确的 fence + inbox 消费事务

```sql
BEGIN;
-- fence 安装者也必须锁定同一行，并验证权威接管证明。
SELECT cluster_incarnation, owner_epoch
FROM engine_output_fence
WHERE partition_id = :partition_id
FOR UPDATE;

-- 应用代码要求 incarnation == 已登记集群，owner_epoch == 本次授权。
-- 不接受发送方自行带一个更大的 epoch 就更新 fence。

INSERT INTO engine_event_inbox (
    consumer_id, partition_id, market_id, engine_seq, event_no, event_count,
    payload_hash, origin_term, accepted_owner_epoch, applied_at
) VALUES (
    :consumer_id, :partition_id, :market_id, :engine_seq, :event_no, :event_count,
    :payload_hash, :origin_term, :owner_epoch, clock_timestamp()
)
ON CONFLICT DO NOTHING
RETURNING engine_seq;

-- 插入成功：同事务追加对应账本/OMS 变化。
-- 未插入：查原 hash 和 event_count，一致才是重复；不一致必须隔离报警。
-- checkpoint 只能推进到无缺口、事件齐全的命令前缀。
COMMIT;
```

数据库事务提交前不得回消费 ACK；不能“先查询 token、释放锁、再开另一个事务扣钱”。DDL 的唯一键只解决重复，不验证 payload 相同，也不防缺口；必须执行冲突校验和连续前缀推进。若事件要发往外部系统，继续使用事务 Outbox，并让该外部系统按稳定业务键幂等，不能以一次数据库事务假装覆盖网络调用。

---

## 32.8 恢复重放与故障切换运行手册

本节是第 31 章恢复接口的运行时实现，也是本章后续性能、混沌和扩展能力的共同验收入口。**恢复不是“能把程序启动”，而是从持久证据证明一个新写者可以接管。**

### 32.8.1 状态机与启动门禁

```text
OFFLINE → DISCOVERING → LOADING → REPLAYING → VERIFYING
                                                    │
                                   ┌────────────────┴──────────────┐
                                   ▼                               ▼
                              READY_FOLLOWER                   QUARANTINED
                                   │ election / authorization
                                   ▼
                              FENCING → SERVING
                                           │ loss of authority
                                           ▼
                                       DRAINING / STALE
```

`LOADING/REPLAYING/VERIFYING/FENCING` 都不对外返回新订单成功。网关可以暂存有界请求或返回可重试错误；不能不受限排队，也不能在两台恢复中的节点间随机分发撮合请求。

### 32.8.2 冷启动步骤

1. **隔离输出与输入**：默认禁止 egress；确定 cluster incarnation、分区归属、软件/规则版本及本机身份。
2. **发现权威恢复点**：由共识库恢复已持久化日志、任期/投票和已安装快照；应用只选择合法 manifest，不按文件修改时间猜测。
3. **检查快照**：验证所有文件 hash、容量、规则、格式、密钥和每市场序列；缺文件或 hash 冲突就退回可验证旧恢复点或隔离。
4. **加载影子状态**：重建订单槽、价位 FIFO、ID 索引、generation、计数器和去重表；加载完成前不暴露半簿。
5. **确定提交前缀**：通过共识恢复/选举协议获得 committed 位置，不把扫描到的最大 index 当成 commit；不允许应用自行越过未提交尾部。
6. **顺序重放**：从快照 index 之后逐条读取已提交命令，验证 framing、长度、CRC、业务序列和规则切换，按固定顺序应用。
7. **验证状态**：检查数量守恒、树/链表、订单数、best、match_seq、去重状态和规范化 state hash；与同一边界的副本/审计检查点比较。
8. **准备发布**：追赶输出/消费游标，重建缺失的确定性结果；重放期间不重新向客户/账本执行副作用。
9. **接管门禁**：成为合法 leader 后安装 fence、取得必要接收边界确认，完成路由切换，再进入 `SERVING`。

状态 hash 应遍历规范化领域状态，例如按市场、side、price、arrival_seq、order_id 排序；排除内存地址、页分配顺序、未规定顺序的 map 和观测时间。槽布局 hash 可用于二进制完整性，但不能替代跨语言的领域 hash。比较必须在**同一个完整命令边界**进行，不能拿 follower 落后 100 条的 hash 证明分歧。

### 32.8.3 torn tail 与已提交损坏是两种事故

| 发现 | 处理 |
|---|---|
| 未提交尾部的半条记录/CRC 错误 | 按存储与共识协议截断/修复，从 leader 重新获取 |
| sealed 历史 segment 中间 CRC 错误 | 隔离该副本，从健康副本/归档修复，不能跳过 |
| 共识认定已提交区间出现本地损坏 | 停止应用与输出；健康副本校验恢复，缺少证据时人工事故流程 |
| index 连续但 engine_seq 有洞 | 检查控制记录/重复记录映射；无法解释则停写 |
| 同 command_id 不同 payload hash | 幂等冲突或数据破坏，拒绝并审计 |
| 规则版本不可用 | 禁止套用“当前版本”重放，先恢复旧版本/可信升级工具 |

CRC 正确不等于记录已提交；记录完整也不等于有权对外确认。应用自行删文件、按 `max(seq)` 截断、跳过损坏成交继续开市，都会破坏共识的安全边界。

### 32.8.4 ACK 丢失与输出重发

```text
客户端提交 command_id=A
 → 输入提交 / 状态机应用
 → MATCH_RESULT 回程丢失
 → 客户端以 A 查证或重试
 → 读取复制去重状态，返回 A 的原结果
 → 不分配新 order_id、不再占一份 reservation、不再次成交
```

结果不存在时也不能立即断言“没发生”：若查询来自落后 follower，可能只是没追上。线性化查证走 leader 的一致性读/ReadIndex 类机制或满足条件的已证明读屏障；普通 follower 查询必须标记 stale 和应用位置。

下游断在“账本已提交但 ACK 未发出”时，新主会重发事件；账本 inbox 命中同键同 hash，不再入账。市场行情仍需自己的序列和缺口恢复协议：金融事实幂等不等于 WebSocket 帧只发送一次。

### 32.8.5 计划迁移与灾难接管

**计划迁移**：停止新准入 → 处理到约定提交边界 → 验证新节点追赶/快照 → 移交 leadership/新 epoch → 安装 fence → 切路由 → 恢复准入；原主不是靠关闭端口来“证明已卸任”。

**失去多数派**：停止成功回执和新经济副作用，保留原 request 身份与 UNKNOWN；先恢复多数派。强制恢复新集群是独立灾难决策，要求确认旧集群物理/权限隔离、明确不可证明尾部、变更 incarnation、审批与客户影响披露，不能继续沿用原来的 RPO=0 承诺。

**切换验收**：已确认输入集合是新日志已提交集合的子集；所有确定性结果 hash 一致；旧主不能产生新副作用；客户端原身份可收敛；恢复过程中没有释放未经终态证明的 reservation。

---

## 32.9 压测、尾延迟预算与混沌实验

### 32.9.1 先定义测量终点

```text
L_received  = 网关收包 → schema/身份校验完成
L_commit    = 网关收包 → durable quorum 提交
L_match     = 状态机开始应用 → 完整命令输出生成
L_result    = 网关收包 → 获授权的完整撮合结果到达客户端
L_ledger    = 成交生成 → 账本按稳定事件 ID 提交
```

`L_match=8µs` 不能替代 `L_result`，也不能用“每秒 200 万次空簿操作”证明同步提交每秒能处理 200 万真实委托。最终延迟包含排队、批等待、网络、磁盘、状态机、发布和客户接收端。

| 阶段 | 示例 p99 预算 | 测量重点 |
|---|---:|---|
| 接入/风控/准入 | 250 µs | 缓存、预占、重复请求 |
| ring 排队与攒批 | 150 µs | 低流量 batch timer、突发积压 |
| 同故障域布局下 quorum 持久提交 | 1,000 µs | fsync、复制 RTT、慢 follower |
| 撮合应用 | 100 µs | 成交扇出、FOK/STP、容量 |
| egress/回执 | 250 µs | fence、发布游标、网络背压 |

这些是**示例阶段预算**，不是硬件保证；阶段 p99 也不能简单相加就声称端到端 p99，必须直接测量端到端分布，并记录 p99.9/max。跨地域 quorum 要重新定目标，不对生产协议“关 fsync 跑一个更好看的数字”。

### 32.9.2 有界容量与停止准入

```text
输入 ring 需求 ≥ 峰值入站速率 × 可容忍阶段停顿 + 突发余量
输出 buffer 需求 ≥ 未释放命令数 × 最坏合法事件扇出
恢复可持续性要求：重放/追赶吞吐 > 同期新增日志吞吐
```

若峰值 50 万条/秒，journal 暂停 20 ms，就新增约 1 万条积压。若大单平均每条产生 100 个事件，按“一条命令一个事件槽”估算会严重低估。必须限制单笔最大扫盘/事件扇出，并在命令开始前预留输出容量，或者采用经过验证的可恢复流式输出机制。

环高水位先限新单与低优先级流量，风险撤单可预留配额但仍需走同一顺序与持久化路径。失去 durable quorum 时，撤单也不能绕过共识偷偷成功；可以记录“撤单请求已收到/待处理”，不能假装订单已被移出簿。

### 32.9.3 性能测试矩阵

| 维度 | 必测组合 | 通过条件 |
|---|---|---|
| 流量 | 空载、常态、过载、脉冲、长稳态 | 不无限排队；限流动作可解释 |
| 命令 | 新单/撤改/IOC/FOK/STP/大单扫盘 | 结果与参考模型一致 |
| 持久化 | 单条 sync、组提交、segment 轮转 | durable 不提前，低流量不饿死 |
| 复制 | 本地/跨 AZ/跨地域、慢 follower | quorum 退化符合设计 |
| 内存 | CoW 无快照/密集快照/池耗尽 | 不读写竞争、不覆盖 pin 页 |
| GC/调度 | Java JFR、Go pprof、CPU 配额 | 热路径分配与 STW 尖峰可追踪 |
| 缓存行 | 有无 padding、跨 NUMA、SMT | 性能结论有硬件与布局证据 |
| 恢复 | 热切、冷启、旧快照、大尾部 | RTO 真实达标、hash 一致 |
| 下游 | 账本慢、行情慢、回执丢失 | 不泄漏未提交输出、不重复入账 |

使用开放环流量发生器或修正 coordinated omission，避免服务变慢后压测器也变慢，最终直方图只统计“没排队的请求”。记录 seed、命令规范化字节、提交轨迹和机器配置，让失败可重现。

### 32.9.4 混沌不是“随机 kill 一下”

| 注入点 | 操作 | 必须验证 |
|---|---|---|
| write 后 sync 前 | 主进程 kill、节点硬重启分别测 | 无提前 ACK；不可把 kill 测试当断电测试 |
| follower 收到后落盘前 | 终止 follower/注入 fsync 阻塞 | 不把内存确认算稳定副本 |
| 提交后应用前 | 杀 leader | 新主按相同输入应用 |
| 应用后回复前 | 丢 ACK、客户端重试 | 只返回原结果，不重复撮合 |
| 账本提交后消费 ACK 前 | 断网 | inbox 幂等，余额不重复变化 |
| leader 与多数派隔离 | 分区、旧主继续收包 | 旧主不成功，fence 拒绝其新输出 |
| 长时间 STW 后复活 | 暂停旧主再切换 | 旧主不能复用旧 epoch |
| 快照写入/rename/manifest 各阶段 | 定点崩溃 | 只选完整已发布 manifest |
| 磁盘 | ENOSPC、EIO、部分写、延迟 | writer 失败封闭、UNKNOWN 正确 |
| 日志 | 未提交尾部撕裂、历史中段损坏 | 两者不同处置，绝不跳过已提交洞 |
| 集群成员变更期间 | 迁移/停节点 | 不产生两个合法写 quorum |

维护一个外部测试 oracle，记录完整的 `DURABLY_ACCEPTED/MATCH_RESULT` 证明及 command_id，不依赖被测节点自己的内存日志。故障后检查已确认集合包含关系、同输入输出 hash、账本唯一性与旧主输出隔离。对超时请求允许“已提交或未提交”两种合法结局，但一旦查证确定，不得反复改变事实。

### 32.9.5 指标必须能驱动动作

```text
wal_sync_duration_seconds / wal_sync_errors_total
wal_batch_bytes / wal_batch_records / oldest_uncommitted_age
raft_term_changes_total / quorum_unavailable_seconds
raft_replication_lag / raft_apply_lag
ring_used_ratio / input_rejected_total{reason}
committed_minus_applied / applied_minus_released
snapshot_duration_seconds / snapshot_cow_pages / cow_pool_free
recovery_replay_commands_total / recovery_phase_duration_seconds
fence_rejected_total / event_hash_conflict_total
consumer_lag_commands / dedup_retention_remaining
```

序号只在同一域比较；多市场 `engine_seq` 差值不能相互相减。Raft term 不应做无界 Prometheus 标签，command_id/order_id 不做常驻高基数标签。日志保留原业务身份与关键切面，指标保留低基数聚合。

示例动作：ring 70% 告警、90% 启动新单限流；sync 连续超时关闭成功回执并触发分区健康降级；state hash 冲突立即隔离；CoW 池接近下限取消快照/限流；失去 quorum 立即停写，不等尾延迟报表慢慢变红。

---

## 32.10 极端边界与反模式决策表

| 极端边界 | 正确决策 | 禁止的“修复” |
|---|---|---|
| ring sequence 已发布、WAL 未 sync | 仍未 durable，不能确认结果 | 把内存屏障当持久化屏障 |
| leader 本机盘坏、其他多数派健康 | 按共识与故障模型切换 | 用本地最大序号强制成为主 |
| 只有一个副本幸存 | 停止新成功回执，进入灾难流程 | 声称仍保留原 quorum 的 RPO=0 |
| 两个副本位于同一宿主机 | 按相关故障重新评估布局 | 按三个进程算三个故障域 |
| 当前任期未建立可读屏障 | 走线性化读协议 | 旧 leader 缓存直接回答“未下单” |
| 旧主携带更大但未经授权 token | 接收方拒绝，自身不安装 | 谁报的数字大就认谁 |
| 新主重发旧任期 committed 事件 | 新发布 epoch + 原事件身份 | 生成新 trade_id 再扣一次钱 |
| 同幂等键不同 payload | 冲突拒绝、隔离审计 | 把新参数覆盖到旧结果 |
| 去重窗口过期 | 归档查证/拒绝不安全重试 | 当作全新订单自动执行 |
| 快照规则 hash 不匹配 | 隔离，恢复正确版本 | 用当前规则“差不多回放” |
| CoW 池耗尽 | 安全取消快照或停写 | 原地改被 snapshot pin 的页 |
| 日志预分配文件末尾有全零区 | 按有效 framing/seal 解析 | 按文件长度当有效提交长度 |
| CRC 碰巧通过/恶意篡改 | 结合共识、摘要、签名/归档校验 | CRC 当密码学证明 |
| 序列/成交计数接近整数上限 | 提前停市/迁移规划 | 让有符号整数回绕 |
| 单命令输出超过预留 | 按规则限制输入或采用安全分帧 | 成交一半才发现 buffer 满 |
| 磁盘满时撤单洪峰 | 预留应急空间，无法提交就 UNKNOWN | 内存撤单成功，重启又挂回簿 |
| 时间回拨/跨日 | 使用日志化逻辑时间与日切命令 | 重放读取操作系统当前时间 |
| 集群恢复成新 incarnation | 权威注册与 fence 重新安装 | 旧集群仍能对外发布 |
| 同一输入副本 hash 不同 | 停写定位首个分歧命令 | 投票选“多数盘口”覆盖历史 |
| 交易所/账本在旁路直接改状态 | 通过规范化命令/补偿事实修复 | 手动改簿或 `UPDATE balance` |

**升级也是边界条件。** 滚动升级必须在固定提交切面验证新旧版本对旧规则输入输出相同；新规则通过有序配置命令生效。快照格式迁移、去重编码迁移和 hash 算法版本都要有回退路径，不能让不同副本在同一 index 运行不同业务规则。

---

## 32.11 术语表：把“复制”“提交”“持久化”分开说

| 术语 | 精确定义 |
|---|---|
| WAL | Write-Ahead Log，状态生效前保存可恢复事实的追加日志 |
| fsync / fdatasync | 请求文件达到指定持久化语义的系统调用，不替代设备契约 |
| Group Commit | 多条记录共享一次同步，确认仍须等待其批次同步成功 |
| Torn Write | 崩溃后只保留部分写入，需 framing、CRC 与恢复边界识别 |
| PLP | Power-Loss Protection，设备断电保护，仍需验证整条存储链 |
| Disruptor | 基于预分配 ring 和序号屏障的进程内事件处理框架 |
| Gating Sequence | 限制生产者覆写仍被消费者使用槽位的进度 |
| Release / Acquire | 用于表达数据写入与后续读取可见性的内存同步关系 |
| False Sharing | 不同核修改不同变量，却争用同一缓存行 |
| CoW | Copy-on-Write，共享旧页保持不变，写者先复制再修改 |
| Snapshot Barrier | 在完整应用边界冻结一致状态的有序屏障 |
| Manifest | 对不可变快照文件集、摘要和序列边界的权威描述 |
| Raft Term | 共识任期，不能直接替代所有外部发布授权 |
| Commit Index | 共识已提交的日志前缀，不是本地最大收到位置 |
| Applied Index | 状态机已完成应用的日志位置 |
| Fencing | 接收边界强制拒绝过期所有者的新副作用 |
| Incarnation | 灾难重建/新集群的身份代际，避免复用旧授权空间 |
| RPO | 可接受的数据恢复缺口，必须声明对象与故障模型 |
| RTO | 故障到业务恢复的时间目标，包括发现、选主、恢复和接管 |
| Deterministic Replay | 相同快照、规则和输入产生相同领域状态与输出 |
| Linearizable Read | 具有实时一致性保证的读，不是“从任意副本读最新缓存” |
| Exactly-once Effect | 通过稳定身份、原子落点与幂等实现单次经济效果，不保证网络只传一次 |

---

## 32.12 面试答题卡：十二个必须说出边界的问题

### Q1：有 WAL 为什么还会丢已确认订单？

可能只写到页缓存、未同步目录、设备未履行 flush、复制确认只到内存，或 ACK 早于持久/提交边界。先定义 ACK 与故障模型，再检查从应用到介质的完整持久化链。

### Q2：Disruptor 为什么快，它能保证不丢单吗？

预分配槽、顺序访问、批处理和序号屏障减少分配与锁竞争；它只处理进程内可见性与生命周期，不提供磁盘或共识持久化，必须另设 durable/release 屏障。

### Q3：为什么要 padding？全都 atomic 还不够吗？

atomic 解决原子性和内存可见性，不避免不同核对同一缓存行的独占权争用。padding 隔离独立写字段，仍须验证布局、NUMA 和实际性能，不能代替正确并发算法。

### Q4：journal 和 matcher 能并行吗？

可以推测执行，但输出必须完全隔离，release 只开放 committed 与 applied 的连续交集；故障丢弃推测簿并重放，快照不得包含未提交状态。默认先提交再应用更容易证明。

### Q5：组提交是否牺牲 RPO？

不必。只要每条成功回执等待所在批次完成持久化和共识提交，就只增加批等待；若先回成功再定时 fsync，才引入已确认数据丢失窗口。

### Q6：CoW 快照怎么保证一致？

完整命令边界冻结根，旧页保持不可变，首次写共享页前复制；快照只读冻结根，文件同步后通过 manifest 原子发布。根表、ID 索引、去重与计数器也在切面内。

### Q7：Raft 是否自动给 RPO=0？

不自动。需要符合故障模型的 quorum、确认前稳定存储、正确持久化配置与存储契约。多数派同时损毁、异步跨地域副本、共同逻辑错误都超出常见承诺。

### Q8：Aeron 与 Dragonboat 怎么选？

Java 可考虑 Aeron Cluster 的有序集群服务与低延迟生态，Go 可考虑 Dragonboat 的多 Raft group/状态机能力；Aeron Transport 本身不是共识。选择重点是固定版本持久语义、快照、运维和故障验证，不是微基准排名。

### Q9：为什么 leader 自检不够，仍要 fencing？

旧主可能在检查后被暂停，恢复时已失权却继续发送。单调授权必须在最终副作用接收边界校验，并与幂等入账同事务执行；只在 header 带 token 没有防护作用。

### Q10：ACK 丢失后换个订单号重试行不行？

不行。原命令可能已提交成交，换号会变成第二笔交易。用原 command_id 线性化查证/重试，复制去重表返回原结果，未证实终态前不释放 reservation。

### Q11：日志最后一条 CRC 错了，删掉即可吗？

先区分未提交 torn tail 与已提交/封口历史损坏。前者按存储共识协议修复；后者隔离副本并从可信来源恢复，不能由业务程序跳过或删掉一个已确认事实。

### Q12：如何证明切换不是“盘口看起来差不多”？

比较同边界提交输入集合、规范化领域 hash、稳定输出身份、订单数量/FIFO/去重状态；验证旧主失去副作用能力，重发不会重复入账，并按 32.8 跑完整接管门禁。

---

## 32.13 自检题：把安全证明写成可执行实验

1. 一条命令已写入 leader 页缓存，follower 仅收到网络包，客户端已收到“成功”。列出三种可能丢单的故障，并重写正确 ACK 条件。
2. 给定 `published=1200, local_durable=1190, committed=1188, applied=1185`，保守模式下最多能发布到哪里？推测模式 applied 达到 1198 时是否能把 1190 发给客户？
3. 一个 Raft group 承载 BTC、ETH，二者 `engine_seq` 都是 100。为什么 `(partition, engine_seq, event_no)` 不是合法唯一键？给出修复。
4. 设计低流量组提交测试：只输入一条命令，随后不再发送，如何证明它不会无限等满批？
5. SPSC ring 中把生产者的 Store 改成普通字段写，或消费者复制前先推进 consumed，分别会破坏什么保证？
6. 同一命令生成十万条 Trade，输出区只能放一万条。如何在不制造半命令副作用的前提下限制输入或设计恢复协议？
7. 快照根固定后，订单数组用了 CoW，order_id map 没有冻结。这份快照是否一致？写一个能复现错误的短命令序列。
8. 三投票副本分布为 AZ-A 两台、AZ-B 一台。丢失 AZ-A 后能否继续安全写？与三 AZ 各一台有何区别？
9. 旧主带 epoch=7 的消息延迟到达，新主 epoch=8 已安装 fence。消息对应的是旧任期合法 committed 事件，如何补发而不生成第二笔成交？
10. 一个客户端 UNKNOWN 查询在 follower 返回“不存在”，随后 leader 却返回已成交。为什么前一次结果不能作为释放 reservation 的证据？
11. 给定每秒 30 万条输入、快照间隔 20 秒、重放 150 万条/秒，忽略其他阶段时最坏重放多久？若要求冷恢复 2 秒，怎样调整并验证？
12. 恢复扫描发现某 sealed segment 中部 CRC 错误，但后续记录都能解码。能否跳过这一条？如何让值班手册阻止危险操作？
13. 写一个混沌测试，在“账本事务提交、消费 ACK 尚未发送”时断网，证明重发后余额只变一次，且冲突 hash 会报警。
14. 假设从异步备份强制恢复新集群，旧集群尚未确认断电。为什么换一个 UUID 不能自动解决双主？必须补充哪些隔离和授权步骤？

**参考要点**：第 2 题最多 1185，推测模式也不能发布超过 1188；第 3 题加入 market_id 或使用分区级唯一序列；第 8 题丢失多数派必须停写；第 11 题仅尾部重放约 4 秒，尚未包含加载、选主和校验。其余题必须落到 durable quorum、稳定身份、不可变切面、接收侧 fencing 与 32.8 证据链，不能只回答“加锁/重试/最终一致”。

---

## 32.14 与 32.8 的衔接：把恢复契约交给行情、风控与运行平台

这里的 **32.8 指本章的恢复重放与故障切换契约**。所有后续扩展都必须复用其状态、序列和接管门禁，而不是各自维护一份“最新状态”。

| 衔接方 | 从 32.8 获取的契约 | 不得越过的边界 |
|---|---|---|
| [33 行情运行时](33-market-data-l2-depth-kline-and-ws-fanout.md) | 已释放事件、完整命令边界、可验证重订阅起点 | 不能广播推测成交；L2 snapshot 不能替代引擎恢复快照 |
| [29 OMS/FIX](29-oms-fix-gateway-and-algo-execution.md) | 原 command_id 结果查证、稳定 Execution ID | 断线重连不把重放回报当新成交 |
| [30 风控/强平](30-pretrade-risk-margin-and-liquidation-engine.md) | committed/applied 状态、UNKNOWN 收敛与 reservation 终态 | 无 quorum 时不能假装撤单/强平已完成 |
| [26 清结算](26-clearing-settlement-netting-and-cutoff.md) | 稳定成交身份、交易日输入和连续消费检查点 | 回放不能改变原成交归属日与费用版本 |
| 集群运行平台 | incarnation、fence、快照 manifest、恢复阶段和 RTO | 路由切换不等于接管完成 |
| 在线升级/市场迁移 | 同一切面 state hash 与输出差分证明 | 不允许在一份簿上混跑不同规则 |

后续若扩展集合竞价、冰山、Stop/Peg、跨市场路由，必须把触发条件、逻辑时间和规则变化加入已提交输入，补全快照和恢复测试。**32.8 的第一条扩展验收标准：同一初始快照、规则和 committed 输入，在主节点、备节点、冷恢复及升级影子实例上得到相同的规范化结果。**

---

## 32.15 本章落地清单

- [ ] RECEIVED、DURABLY_ACCEPTED、MATCH_RESULT 的承诺对象与观测位置分别定义。
- [ ] 输入日志权威唯一；Raft index、业务 engine_seq、ring sequence、event_no 不混用。
- [ ] fsync/设备/文件系统/目录同步语义在目标环境经过故障验证。
- [ ] 组提交同时限制条数、字节、等待时间，失败 writer 不继续推进水位。
- [ ] Disruptor gating 和异常处理路径不会让 matcher 越过 durable/commit 屏障。
- [ ] Go ring 的 SPSC 前提、原子发布和槽生命周期通过 race 与绕回测试。
- [ ] padding/NUMA 优化有布局证据与端到端 p99 数据，不只报吞吐。
- [ ] CoW 冻结所有影响未来输出的状态，页池与根复制暂停都有容量预算。
- [ ] 快照文件与 manifest 按同步协议发布，日志回收考虑复制、审计和消费者需求。
- [ ] 共识库固定版本、持久化选项、故障域和 quorum 退化行为已记录。
- [ ] 接收边界强制 fence，幂等身份不随 leader/重试变化，冲突 hash 不被吞掉。
- [ ] 按 32.8 完成冷恢复、热切换、旧主复活和 UNKNOWN 查证演练。
- [ ] 混沌覆盖 write/sync/commit/apply/ACK 各边界，而不只 kill 进程。
- [ ] RPO=0 声明明确最大容忍故障、确认范围、存储契约与失去多数派时停写策略。
- [ ] 端到端性能使用真实持久化与复制配置，RTO 包含加载、校验、fencing 和路由。

**本章的生产结论不是“用了 Disruptor 和 Raft 就不会丢单”，而是：每一个成功回执都有可恢复的提交证明，每一次状态变更都有确定性输入，每一个外部副作用都有稳定身份与当前授权，每一次接管都经过 32.8 的可验证门禁。**
