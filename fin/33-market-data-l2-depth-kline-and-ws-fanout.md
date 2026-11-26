# 33. 实时行情系统：L2 深度拼装、可修正 K 线与百万 WebSocket 扇出

> **状态**：生产级工程篇｜**所属**：第四部分·核心交易与金融业务架构｜**编号**：33 / 48
>
> **本章定位**：承接 [04 订单簿与流动性](04-orderbook-order-types-and-liquidity.md)、[20 定点算术](20-monetary-precision-rounding-and-allocation.md)、[21 金融时间与序列](21-financial-time-calendar-and-sequence-id.md)、[27 多层对账](27-multi-tier-reconciliation-and-auto-healing.md)、[30 事前风控与强平](30-pretrade-risk-margin-and-liquidation-engine.md) 和 [31 撮合引擎](31-matching-engine-orderbook-and-zero-gc.md)，回答一个具体问题：**如何把不可靠网络上的市场事件，变成有版本、可核验、可恢复的行情，并在慢客户端存在时仍可靠地服务百万连接？** 第 32 章负责撮合日志与主备持久化，本章负责其下游行情投影，不用公网广播反向决定撮合事实。
>
> **覆盖范围**：REST 快照 + WS 增量 `[U,u,pu]` 衔接；断包恢复状态机；L2 价位拼装与有限深度完整性；成交去重、迟到事件与 K 线修正；PostgreSQL 协议/DDL；Go 1.24+、Java 21+ 核心代码；Netty / `gobwas/ws` 预编码广播；百万连接容量、背压与慢消费者；对账压测、极端边界、术语、面试答题卡、自检题和 §33.8 系统衔接。
>
> **不可妥协的红线**：价格/数量不能用浮点；价位数量是绝对值还是差值必须写进协议；快照、增量和序列必须属于同一市场与同一来源代际；发现断包立即撤销 `LIVE`，不能继续展示“看似正常”的旧簿；K 线来自成交而非盘口，重复消息不得重复计量；`closed` 不代表永不修正；不许无界写队列；严格深度增量不能静默丢包；预编码不是跨 socket、跨 TLS 的物理零拷贝；行情缓存不是订单、成交或资金真相。

---

## 33.1 事故引子：连接还活着，不等于盘口还是对的

### 33.1.1 一次复合事故的时间线

以下为**教学合成事故**，时间与损失用于工程推演，不冒充真实机构披露。

| 时间 | 事件 | 错误实现 | 结果 |
|---|---|---|---|
| `09:30:00.000` | 网关先请求 REST 快照 | 快照回来后才订阅 WS | 两次调用之间的盘口变更永远丢失 |
| `09:30:00.150` | 快照 `S=100` 返回 | 将每个价位数量当成增量相加 | 原买一 `5`，更新绝对量 `3` 后变成 `8` |
| `09:30:00.220` | 收到 `[101,105]`，随后漏掉 `[106,108]` | 只比较 `u` 是否变大 | `[109,112]` 被接受，旧买一残留 |
| `09:30:01.000` | 做市策略读取旧深度 | 网关的 TCP 心跳健康，行情就被标成健康 | 策略按不存在的流动性放量，被动承受滑点 |
| `09:31:00.010` | 上一分钟一笔成交迟到 | 按接收时间分桶 | 前一分钟少量，下一分钟虚增；收盘价错误 |
| `09:31:00.050` | 重新订阅后重复推送成交 | 没有 `trade_id` 去重 | 成交量重复，VWAP 执行算法多买 |
| `09:31:05.000` | 2% 移动客户端进入弱网 | 每连接无界缓存，每条消息重新序列化 | 老消息继续排队，堆内存和 GC 一起上升 |
| `09:31:20.000` | 网关 OOM 重启 | 客户端立即重连、同时 REST 回源 | 鉴权、TLS 和快照服务共同雪崩 |

损失示例：按虚假深度多执行 `120 BTC`，平均额外滑点 `15 USDT/BTC`，直接滑点损失 `1,800 USDT`；因重复成交量多买 `20 BTC`，不能把其 `1,000,000 USDT` 名义额全部叫作“亏损”，应另外核算平仓价差、费用和对冲成本。

**根因不是某一条 WS 消息没收到，而是系统没有为“当前视图值得相信”建立可验证条件。** TCP 顺序只覆盖一个存活连接里的字节；进程队列溢出、订阅切换、来源主备切换、解析失败和重连，仍然能在业务事件序列上制造缺口。

### 33.1.2 唯一真问题与边界

> 对任意展示或策略消费的盘口/K 线，能否回答：它源自哪里、截至哪个序列、缺没缺数据、是否完整、何时可修正、客户端若落后该怎样恢复？同时，百万个消费者中最慢的那批，能否不拖垮最快的那批？

四种身份不能混用：

| 对象 | 事实身份 | 本章能做什么 | 本章不能做什么 |
|---|---|---|---|
| 撮合成交 | `(venue, instrument, trade_id)` + 修订 | 留存、去重、投影 | 通过修改 K 线“撤销成交” |
| 上游深度增量 | `(source_epoch, U, u)` 与来源协议 | 校验、重建 L2 | 把不同交易所增量直接拼一个簿 |
| 自有下行频道 | `(stream_epoch, channel_seq)` | 检测网关/客户端断档 | 把它当成撮合 `engine_seq` |
| K 线版本 | `(series_key, bucket_start, revision)` | 迟到修正、历史重现 | 用接收时刻冒充交易发生时刻 |

行情采用**至少一次接入 + 确定性投影 + 显式恢复**。浏览器“最终看到最新值”不等于每条成交被交付，更不是端到端 exactly-once。

---

## 33.2 L2 重建：先定义协议，再写断包恢复状态机

### 33.2.1 总体架构与单写者

```text
自有撮合 durable events                 外部交易所 REST + WS
         │                                     │
         └────────► 来源适配 / 原文归档 ◄────────┘
                              │ 分区键：venue + instrument
                    有界缓冲 / 代际 / 序列校验
                              │
                 ┌────────────┴──────────────┐
                 ▼                           ▼
          L2 单写者重建器                成交去重 / 修订事实
                 │                           │
          原子不可变快照                 事件时间 K 线聚合
                 │                           │
                 └────────────┬──────────────┘
                              ▼
                   地域 Relay / 频道分发树
                              │ 每网关每频道一份
                              ▼
                  预编码 → 网关本地订阅列表
                              │ 有界队列 / 单写者
                              ▼
                        百万 WS 客户端
```

同一来源、同一市场只有一个逻辑 L2 写者。解析可并行，但送回写者前必须恢复接收顺序；不能让多个解析线程谁先完成就先修改簿。读取方拿带版本的不可变视图或通过单写者请求快照，不得并发遍历正在变化的 map/tree。

### 33.2.2 `[U,u,pu]` 不是全球统一标准

- `U`：本消息覆盖的首个更新 ID。
- `u`：本消息覆盖的最后更新 ID。
- `pu`：前一条流消息的最终更新 ID；**只有部分来源有此字段**。
- `S`：REST 快照携带的 `lastUpdateId`。
- `L`：本地已经完整应用的最后更新 ID。

本章冻结两个**适配器配置**，用于说明规则差异，不能依靠“字段长得像”自动推断：

| 规则 | `RANGE_NEXT`，现货常见区间衔接 | `PREV_LINK`，部分合约流的 `pu` 链 |
|---|---|---|
| 初始化丢弃 | `u <= S` | `u < S` |
| 第一条可用增量 | `U <= S+1 <= u` | `U <= S <= u` |
| 第一条的 `pu` | 无该字段也正常 | 不要求等于 `S`，快照可切在消息内部 |
| 后续重复/旧消息 | `u <= L` 丢弃，记录重复指标 | `u <= L` 丢弃，记录重复指标 |
| 后续连续性 | `U <= L+1 <= u` | 必须 `pu == L`，且 `u > L` |
| 断档 | `U > L+1` | `pu != L` |

`PREV_LINK` 中不额外要求 `U == L+1`：更新 ID 是否连续、空洞如何分配属于来源协议。反过来，无 `pu` 的来源不能写成 `pu=0` 后套链式校验。

**版本注意**：交易所文档会修订，连初始化边界的文字表述都可能改变。上线时固定 `venue_protocol_version`、保存文档日期和录制样本，并针对来源验证桥接条件；上表是本文代码的显式契约，不是宣称所有 Binance 产品、所有时期都相同。现货文档可参考 [Diff. Depth 与本地簿维护](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md#how-to-manage-a-local-order-book-correctly)，合约参考 [USDⓈ-M 本地簿维护](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/How-to-manage-a-local-order-book-correctly)。规则不确定时宁可重抓快照，不能“放宽到能跑”为止。

### 33.2.3 状态机：先开 WS 缓冲，再抓快照

```text
DISCONNECTED
   │ 分配新的 connection_generation，连 WS 并确认订阅
   ▼
BUFFERING ──► FETCHING_SNAPSHOT ──► ALIGNING
   ▲                 │                 │
   │                 │ 429/超时         ├─ 快照过旧/无法桥接 → 重抓
   │                 └─ 预算内退避       ├─ 尚无覆盖事件 → 等待，仍非 LIVE
   │                                   └─ 桥接 + 缓冲按序回放完成
   │                                                    ▼
   └──── 新连接/重建 ◄── RESYNCING ◄── GAP/校验失败 ─── LIVE
                               ▲                         │
                               └─ 缓冲溢出 / 来源代际改变 ┘
```

完整恢复顺序：

1. 先让该连接的消息进入**有界**缓冲，再发起 REST。记录 `connection_generation`、首个 `U`、接收时间和字节水位。
2. REST 回来后检查市场、来源、协议版本、精度版本；快照响应必须仍属于当前恢复请求。旧请求迟到不能覆盖新恢复。
3. 在**候选簿**安装快照 `S`，按相应 profile 丢弃旧消息，寻找桥接消息。没有新事件时继续等待，不凭一次 REST 成功就宣告流已追平。
4. 按来源顺序重放；先验证完整批次、再提交价位与 `L`。任一处断档，废弃候选簿，重新恢复。
5. 缓冲排到当前接收边界，并在同一个 actor 中切换到实时应用；原子发布新 `stream_epoch` 的快照。不能先清空缓冲、再切换订阅回调，制造第二个竞态窗口。
6. 进入 `LIVE` 后持续检查序列、来源心跳、数据新鲜度与完整性。断档立即标为 `STALE/RESYNCING`，停止发布可交易深度。

原来的最后正确快照可以继续展示为 `stale=true`，但必须保留原来的 `as_of`；**不得不断更新时间戳把旧簿伪装成新簿**。给策略/风控的接口则返回不可用或明确的低可信状态，不能依赖 UI 的灰色标签来保护自动交易。

### 33.2.4 手工走一遍边界

**区间衔接**：快照 `S=100`，缓冲 `[95,100]`、`[99,103]`、`[104,106]`。丢弃第一条；第二条覆盖 `101`，全量应用其中所有绝对数量，令 `L=103`；第三条覆盖 `104`，令 `L=106`。收到 `[109,112]` 时 `109 > 107`，断档，不得仅因 `112 > 106` 就接受。

**前链衔接**：快照 `S=100`，首条 `[98,102,pu=97]` 合法，`pu=97` 不等于快照并非错误；随后 `[103,105,pu=102]` 合法；`[109,112,pu=108]` 不合法。若首条是 `[101,103]`，不能套用现货规则接受，它没有覆盖本 profile 要求的 `100`。

**快照超前**：快照 `S=200`，缓冲目前只到 `190`，先丢弃过期消息，继续等桥接，不是“快照失败”。**快照落后**：缓冲首条 `U=300` 而 `S=200`，缺少中间历史，重新请求快照；重抓次数/总时长达到预算则熔断该来源，避免 REST 无限自激。

### 33.2.5 L2 价位应用与有限深度陷阱

每条消息的 bid/ask 元组为 `(price_ticks, qty_lots)`，本章协议规定是**价位绝对剩余量**：

```text
qty > 0  → set(side, price, qty)    // 新增或覆盖，不是 +=
qty = 0  → delete(side, price)      // 原来不存在也可幂等成功
qty < 0  → 非法报文，整条失败       // 不拿它表示撤单
```

生产实现应遵守：

- 按市场元数据做整数转换和溢出校验；拒绝超 scale 的小数，不能 `ParseFloat` 后乘倍数。**价格可否为负由合约定义**，不能对所有期货写死 `price > 0`。
- bids 按价格降序、asks 升序。Go 可用 `google/btree` 泛型版，Java 可用 Agrona 原始类型存储配合专用价格树；`TreeMap<Long,Long>` 便于参考实现，但有装箱与分配成本。
- 消息内同一侧同一价位重复时，若来源未定义“最后一个生效”，则拒绝；定义了才按原顺序覆盖。不能因 map 无序遍历改变来源语义。
- 先解析/验证全部元组，再原子提交。不能改完 bids 才发现 asks 数量非法，却仍保留半条更新。可用预校验 + 无失败提交，或暂存变更/回滚日志。
- 连续竞价市场在**完整消息边界**检查 `best_bid < best_ask`；不能在处理完 bids、尚未处理 asks 的中间态误报。集合竞价、锁定市场、多来源聚合按独立规则判定，不能套用此不变量。
- L2 是同价聚合量，不包含订单身份与队列优先级；不能凭 L2 还原真实排队位置，也不能拿盘口减少量当成交量。

**最隐蔽的边界：有限 REST 快照不是全簿。** 若快照仅给出前 1,000 档，外面没变动过的价位永远不会凭空出现。深度不断被吃掉后，“内存里还有 20 档”不代表这就是市场真实前 20 档，可能有未知价位夹在其间。

应维护每侧的**已知连续覆盖区间**、初始边界及 `depth_complete`：在覆盖范围内才宣称前 N 档可靠；边界逼近时重新取得更深快照或明确降级。对未知区间收到一个价位更新，只能知道这个价位，不能把整个区间标成完整。对外发布 Top-N 增量时，新进入 N 档的价位需要发送其完整数量，离开 N 档要发删除；直接过滤“原始消息里当前排前 N 的价位”会在客户端留下幽灵档位。

---

## 33.3 协议、DDL 与 Go / Java 序列核心

### 33.3.1 规范化入口与下行协议

入口价格/数量已经过十进制定点转换；JSON 中使用字符串，避免 JavaScript 对大于 `2^53-1` 的整数丢精度。内部 SBE/Protobuf 可使用 `int64`，但必须携带 schema 与标度版本。

```json
{
  "schema_version": 1,
  "venue": "VENUE_A",
  "instrument": "BTC-USDT-PERP",
  "source_epoch": "source-session-42",
  "connection_generation": "17",
  "protocol_profile": "PREV_LINK",
  "metadata_version": "9",
  "U": "103", "u": "105", "pu": "102",
  "event_time_ns": "1805967000000000000",
  "receive_time_ns": "1805967000003000000",
  "bids": [["5000000", "300"]],
  "asks": [["5000010", "0"]]
}
```

`source_epoch` 是来源真正的序列命名空间；`connection_generation` 是本地网络恢复代际，两者不等价。没有可靠来源 epoch 的外部接口，由适配器在重建时分配本地代际并强制快照恢复，不能宣称跨重连证明了原始全局连续性。

下行使用自己的序列，不复用上游 `u`：

```json
{
  "type": "depth_delta", "schema_version": 1,
  "channel": "VENUE_A:BTC-USDT-PERP:l2:20:100ms",
  "stream_epoch": "view-88", "channel_seq": "701",
  "prev_channel_seq": "700", "source_u": "105",
  "metadata_version": "9", "as_of_ns": "1805967000000000000",
  "status": "LIVE", "depth_complete": true,
  "bids": [["5000000", "300"]], "asks": [["5000010", "0"]],
  "checksum": "implementation-defined-canonical-hash"
}
```

配套控制帧：`subscribed`、`depth_snapshot`、`resync_required`、`stale`、`draining`、`error`。快照是该频道在 `channel_seq=700` 的完整 Top-N，客户端先原子替换，再只接受同 epoch 且 `prev_channel_seq=700` 的增量。新订阅加入必须完成“固定快照位点 → 缓冲后续更新 → 入队快照 → 入队连续尾部 → 实时”的原子握手；订阅 ACK 不是快照屏障。

若按 100 ms 合并上游深度，合并器必须先验证所有输入连续，按价位保留最终绝对量并正确处理 Top-N 出入；下行产生**新的一条频道事件**。不能丢掉中间原始消息后仍伪装成原始 `pu` 链。不同档数、节流频率、权限和编码是不同频道/编码组，序列与校验口径要一致。

### 33.3.2 PostgreSQL DDL：存事实、版本与检查点，不逐帧写关系库

以下为可独立建表的最小审计/恢复模型。高频原文进入持久日志/对象存储，关系库保存成交有效修订、K 线版本、快照清单与事务发件箱；生产中按日期/市场分区、设置保留策略、评估索引成本。

```sql
CREATE TABLE md_snapshot_manifest (
    snapshot_id       uuid PRIMARY KEY,
    venue             text NOT NULL,
    instrument        text NOT NULL,
    source_epoch      text NOT NULL,
    protocol_profile  text NOT NULL CHECK (protocol_profile IN ('RANGE_NEXT','PREV_LINK')),
    source_u          bigint NOT NULL CHECK (source_u >= 0),
    metadata_version  bigint NOT NULL,
    object_uri        text NOT NULL,
    sha256            bytea NOT NULL CHECK (octet_length(sha256) = 32),
    coverage          jsonb NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (venue, instrument, source_epoch, source_u)
);

CREATE TABLE md_trade_revision (
    venue             text NOT NULL,
    instrument        text NOT NULL,
    trade_id          text NOT NULL,
    revision          bigint NOT NULL CHECK (revision >= 1),
    source_epoch      text NOT NULL,
    source_seq        bigint NOT NULL CHECK (source_seq >= 0),
    event_time_ns     bigint NOT NULL,
    available_time_ns bigint NOT NULL,
    action            text NOT NULL CHECK (action IN ('NEW','REPLACE','CANCEL')),
    price_ticks       bigint NOT NULL,
    qty_lots          bigint NOT NULL CHECK (qty_lots > 0),
    metadata_version  bigint NOT NULL,
    payload_sha256    bytea NOT NULL CHECK (octet_length(payload_sha256) = 32),
    PRIMARY KEY (venue, instrument, trade_id, revision)
);
CREATE INDEX md_trade_event_idx
    ON md_trade_revision (venue, instrument, event_time_ns);
-- CANCEL 保存被撤销版本的定位参数，不表示又成交一次。
-- 最新有效版本必须先按 trade_id 取最新 revision，再排除 CANCEL。

CREATE TABLE md_kline_version (
    venue             text NOT NULL,
    instrument        text NOT NULL,
    series_version    bigint NOT NULL,
    interval_code     text NOT NULL,
    bucket_start_ns   bigint NOT NULL,
    bucket_end_ns     bigint NOT NULL,
    revision          bigint NOT NULL CHECK (revision >= 1),
    open_ticks        bigint,
    high_ticks        bigint,
    low_ticks         bigint,
    close_ticks       bigint,
    volume_lots       numeric(38,0) NOT NULL CHECK (volume_lots >= 0),
    turnover_units    numeric(38,0) NOT NULL,
    trade_count       bigint NOT NULL CHECK (trade_count >= 0),
    status            text NOT NULL CHECK (status IN ('OPEN','CLOSED','FINAL')),
    quality           text NOT NULL CHECK (quality IN ('COMPLETE','INCOMPLETE','SYNTHETIC')),
    input_watermark_ns bigint NOT NULL,
    available_time_ns bigint NOT NULL,
    reason            text NOT NULL,
    PRIMARY KEY (venue, instrument, series_version, interval_code, bucket_start_ns, revision),
    CHECK (bucket_end_ns > bucket_start_ns),
    CHECK ((trade_count = 0 AND open_ticks IS NULL AND high_ticks IS NULL
            AND low_ticks IS NULL AND close_ticks IS NULL)
        OR (trade_count > 0 AND open_ticks IS NOT NULL AND high_ticks IS NOT NULL
            AND low_ticks IS NOT NULL AND close_ticks IS NOT NULL
            AND low_ticks <= open_ticks AND low_ticks <= close_ticks
            AND high_ticks >= open_ticks AND high_ticks >= close_ticks))
);

CREATE TABLE md_projector_checkpoint (
    projector         text NOT NULL,
    partition_id      integer NOT NULL,
    log_epoch         text NOT NULL,
    committed_offset  bigint NOT NULL,
    owner_epoch       bigint NOT NULL,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (projector, partition_id)
);

CREATE TABLE md_outbox (
    event_id          uuid PRIMARY KEY,
    aggregate_key     text NOT NULL,
    aggregate_version bigint NOT NULL,
    event_type        text NOT NULL,
    payload           jsonb NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    published_at      timestamptz,
    UNIQUE (aggregate_key, aggregate_version)
);
CREATE INDEX md_outbox_pending_idx ON md_outbox (created_at)
    WHERE published_at IS NULL;
```

额外纪律：

1. 同一成交身份/修订号出现不同 hash，不可用 `ON CONFLICT DO NOTHING` 吞掉，应隔离并报警；同 hash 的重复才是幂等成功。
2. K 线修订、checkpoint 与对应 outbox 在同一数据库事务提交；事务提交前崩溃就重放，提交后重复靠版本键吸收。`owner_epoch` 必须在更新 checkpoint 时做条件比较，阻止旧写者继续提交。
3. Outbox 可能重复投递，下游按事件身份去重；同一 aggregate 的版本有序发送，不能让多 worker 的 `SKIP LOCKED` 把修订 8 先于修订 7 投递。
4. 对象快照先完整上传并校验，再登记 manifest；写了一半的对象不能成为有效恢复点。K 线 checkpoint 之外，还要恢复待关闭窗口、去重状态、watermark 与未完成修正任务，或从更早安全位点重放。
5. `numeric(38,0)` 也有上限。产品最大价格、最大量、单桶成交笔数的乘法上界必须校验；负价格产品的 turnover 可能为负，不对它加通用非负约束。

### 33.3.3 Go：显式 profile 的序列闸门

下面是可单独编译的序列核心，不负责网络和价位树。**`Ready` 不修改游标，只有候选价位更新成功后才能 `Commit`**；调用者是单写者，不允许两个消息同时通过 `Ready` 后交叉提交。

```go
package depth

import (
    "errors"
    "math"
)

type Profile uint8
const (
    RangeNext Profile = iota
    PrevLink
)

type Delta struct {
    U, Last, Prev int64
    HasPrev bool
}

var (
    ErrGap = errors.New("depth gap: resnapshot required")
    ErrBad = errors.New("invalid depth event/profile")
)

type Gate struct {
    Profile Profile
    Last int64
    Bridged bool
}

func NewGate(p Profile, snapshot int64) (*Gate, error) {
    if (p != RangeNext && p != PrevLink) || snapshot < 0 || snapshot == math.MaxInt64 {
        return nil, ErrBad
    }
    return &Gate{Profile: p, Last: snapshot}, nil
}

// false,nil: 旧事件；true,nil: 可应用；非 nil: 退出 LIVE / 重建。
func (g *Gate) Ready(d Delta) (bool, error) {
    if d.U < 0 || d.Last < d.U || d.Last == math.MaxInt64 {
        return false, ErrBad
    }
    if g.Profile == PrevLink && (!d.HasPrev || d.Prev < 0 || d.Prev > d.Last) {
        return false, ErrBad
    }
    if !g.Bridged {
        if g.Profile == RangeNext {
            if d.Last <= g.Last { return false, nil }
            if d.U > g.Last+1 { return false, ErrGap }
        } else {
            if d.Last < g.Last { return false, nil }
            if d.U > g.Last { return false, ErrGap }
        }
        return true, nil
    }
    if d.Last <= g.Last { return false, nil }
    if g.Profile == RangeNext {
        if d.U > g.Last+1 { return false, ErrGap }
    } else if d.Prev != g.Last {
        return false, ErrGap
    }
    return true, nil
}

func (g *Gate) Commit(d Delta) {
    g.Last = d.Last
    g.Bridged = true
}
```

调用顺序必须是 `校验 generation/epoch → 解析并验证完整价位批次 → Ready → 候选簿应用 → 消息边界不变量 → Commit → 发布`。任一异常均丢弃候选状态；`ErrGap` 后不能在原 Gate 上继续处理后续消息。`Commit` 故意不提供并发安全假象，它是 actor 内部方法。

### 33.3.4 Java：相同边界，不把 `long` 溢出当“极少发生”

```java
public final class DepthGate {
    public enum Profile { RANGE_NEXT, PREV_LINK }
    public record Delta(long first, long last, Long prev) {}
    public static final class Resnapshot extends RuntimeException {
        public Resnapshot(String message) { super(message); }
    }

    private final Profile profile;
    private long last;
    private boolean bridged;

    public DepthGate(Profile profile, long snapshot) {
        if (profile == null || snapshot < 0 || snapshot == Long.MAX_VALUE)
            throw new IllegalArgumentException("bad snapshot/profile");
        this.profile = profile;
        this.last = snapshot;
    }

    public boolean ready(Delta d) {
        if (d.first() < 0 || d.last() < d.first() || d.last() == Long.MAX_VALUE)
            throw new Resnapshot("bad interval");
        if (profile == Profile.PREV_LINK &&
            (d.prev() == null || d.prev() < 0 || d.prev() > d.last()))
            throw new Resnapshot("missing/bad pu");
        if (!bridged) {
            if (profile == Profile.RANGE_NEXT) {
                if (d.last() <= last) return false;
                if (d.first() > Math.addExact(last, 1))
                    throw new Resnapshot("snapshot bridge gap");
            } else {
                if (d.last() < last) return false;
                if (d.first() > last) throw new Resnapshot("snapshot bridge gap");
            }
            return true;
        }
        if (d.last() <= last) return false;
        if (profile == Profile.RANGE_NEXT) {
            if (d.first() > Math.addExact(last, 1)) throw new Resnapshot("range gap");
        } else if (d.prev().longValue() != last) {
            throw new Resnapshot("pu gap");
        }
        return true;
    }

    // 仅在价位批次已验证且原子应用成功后调用。
    public void commit(Delta d) { last = d.last(); bridged = true; }
    public long last() { return last; }
}
```

示例采用非负、有符号 63 位更新 ID；若来源使用完整 `uint64`，应换成无符号比较/更宽类型，并全链路保持一致，不允许解析后符号翻转。`Long prev` 便于展示缺字段语义，低分配实现换成原始 `long` + `hasPrev`。

---

## 33.4 流式 K 线：迟到会改开收盘，撤销会改最高最低

### 33.4.1 先说清楚 K 线到底统计什么

K 线输入是**实际成交事实**，不是深度增减、挂单量、报价中价，也不是把每次 ticker 的 `last` 都算一笔成交。原始成交与聚合成交不能混吃，否则同一成交计两遍；使用聚合成交时，`trade_count` 的含义还要明确是聚合条数还是原始撮合笔数。

对窗口 `[start,end)` 的有效成交集合 `T`：

```text
order_key(t) = (event_time_ns, market_trade_sequence)
open   = price(argmin order_key(T))
close  = price(argmax order_key(T))
high   = max(price(T))
low    = min(price(T))
volume = Σ qty_lots
turnover_product_units = Σ (price_ticks × qty_lots)
VWAP_ticks = turnover_product_units / volume_lots
```

`market_trade_sequence` 必须来自可重放的市场总序；跨 epoch 无法直接比较时，由规范化日志分配稳定排序键。若来源只给不透明 trade ID，则明确定义稳定 tie-break 并标注其不一定等于真实撮合先后，不能把“到达我这台机器的先后”伪装成交易顺序。

`price_ticks × qty_lots` 的物理单位由 tick/lot 决定：例如 tick=`0.01 USDT/BTC`、lot=`0.001 BTC`，一个乘积单位=`0.00001 USDT`。VWAP 展示时才按规则舍入。反向合约/非线性合约的成交额可能不是 `P×Q`，应按合约版本提供独立算子，不能照抄现货公式。

UTC 固定一分钟桶可用 `floorDiv(event_time_ns, 60e9) * 60e9`；股票日线用交易所交易日历，不能用自然日替代；月线不能把一个月写成固定 30 天。日历、时区、复权规则和合约单位属于 `series_version`。

### 33.4.2 watermark、关闭与可修正窗口

一个可执行策略示例：

| 阶段 | 条件/动作 | 消费者可依赖什么 |
|---|---|---|
| `OPEN` | 当前桶持续聚合，每 250 ms 发最新完整桶状态 | 实时暂态，revision 会增长 |
| `CLOSED` | `watermark >= bucket_end` | 常规流已跨过桶末，但仍允许迟到修正 |
| 允许迟到 | 例如 `watermark <= bucket_end + 120s` | 迟到去重后重算，增加 revision |
| `FINAL` | 超过在线修正窗口且来源补洞检查通过 | 常规实时算子不再直接修改 |
| 极迟到/官方更正 | 进入修正任务，重放有效成交集 | 产生新的 FINAL 修订及审计原因，不覆写旧历史 |

watermark 的一种定义是 `max_valid_event_time - 2s`；分区合并取活跃分区最小值。**watermark 是完整性判断的策略，不是证明网络不会再送旧消息。** 一个远未来时间戳会提前关闭大量窗口，因此必须做时钟偏差校验和隔离，不能直接纳入 `max_event_time`。

无成交的冷门市场不能靠不断出现的成交推进 watermark，需要来源有序心跳/进度信号或受约束的空闲分区策略。排除 idle 分区后它若恢复并送来旧事件，仍按迟到处理；若进度信号不能证明期间无丢包，桶质量应为 `INCOMPLETE`，不能仅因墙上时间过了就变成“完整”。

`FINAL` 指常规处理阶段结束，不是“金融事实以后绝不更正”。官方 bust、错单撤销、数据补齐仍可追加修订；消费者按 `(桶身份, revision)` 替换完整桶，不对 volume 做盲目累加。修正消息可带 `supersedes_revision`、`reason`、`input_cutoff`，让落后的消费者检测缺少版本并补拉。

### 33.4.3 一分钟手工算账

窗口为 `09:30:00 ≤ t < 09:31:00`，价格为演示整数 tick，数量为整数 lot：

| 到达顺序 | 成交发生时间 | ID/序列 | 价格 × 数量 | 动作 |
|---:|---|---|---|---|
| 1 | `09:30:10` | A/10 | `100 × 2` | 正常 |
| 2 | `09:30:50` | B/12 | `110 × 1` | 正常 |
| 3 | `09:30:10` | A/10 | `100 × 2` | 重复，忽略 |
| 4（09:31 后到） | `09:30:05` | C/9 | `90 × 1` | 迟到，仍属于上一分钟 |

前两笔：`O=100,H=110,L=100,C=110,V=3,Q=310`。C 到达后：`O=90,H=110,L=90,C=110,V=4,Q=400,VWAP=100`。不能只做 `high=max,low=min` 而忘记改 open。

若后来官方撤销 B，重新求有效集合 `{A,C}`：`O=90,H=100,L=90,C=100,V=3,Q=290`。**最高价和收盘价都下降了**，单靠从旧 volume 减一笔不能修好 OHLC。若更正成交时间跨桶，需要同时重算旧桶与新桶；若一分钟修订，五分钟、小时、日线父桶也要失效重算，不能只修最底层。

### 33.4.4 Go：重放有效成交集的确定性聚合器

下列 reducer 适合参考实现、修正重算及对账 oracle。它接收**已按成交身份去重、已选择最新有效修订、已排除 CANCEL** 的集合；热点实时路径可用固定容量窗口状态和定点宽整数优化，但不能删掉相同语义。

```go
package bars

import (
    "errors"
    "math/big"
)

type Trade struct {
    TimeNS, Seq, Price, Qty int64
}
type Bar struct {
    Open, High, Low, Close int64
    FirstTime, FirstSeq, LastTime, LastSeq int64
    Count int64
    Volume, Turnover big.Int
}
func before(t1, s1, t2, s2 int64) bool {
    return t1 < t2 || (t1 == t2 && s1 < s2)
}
func Rebuild(start, end int64, trades []Trade) (*Bar, error) {
    if start >= end { return nil, errors.New("invalid bucket") }
    b := new(Bar)
    var price, qty, product big.Int
    for _, t := range trades {
        if t.TimeNS < start || t.TimeNS >= end || t.Qty <= 0 {
            return nil, errors.New("invalid trade for bucket")
        }
        if b.Count == 0 {
            b.Open, b.High, b.Low, b.Close = t.Price, t.Price, t.Price, t.Price
            b.FirstTime, b.FirstSeq = t.TimeNS, t.Seq
            b.LastTime, b.LastSeq = t.TimeNS, t.Seq
        } else {
            if t.Price > b.High { b.High = t.Price }
            if t.Price < b.Low { b.Low = t.Price }
            if before(t.TimeNS, t.Seq, b.FirstTime, b.FirstSeq) {
                b.Open, b.FirstTime, b.FirstSeq = t.Price, t.TimeNS, t.Seq
            }
            if before(b.LastTime, b.LastSeq, t.TimeNS, t.Seq) {
                b.Close, b.LastTime, b.LastSeq = t.Price, t.TimeNS, t.Seq
            }
        }
        b.Count++
        qty.SetInt64(t.Qty)
        price.SetInt64(t.Price)
        b.Volume.Add(&b.Volume, &qty)
        product.Mul(&price, &qty)
        b.Turnover.Add(&b.Turnover, &product)
    }
    return b, nil
}
```

`Count=0` 时 Go 结构的 OHLC 零值只是内部占位，写入 DDL/发 JSON 必须编码为 `null`；不能把 `0` 当成真实开盘价。`big.Int` 是可变对象，不可按值浅拷贝后跨 goroutine 修改，共享快照需深拷贝或编码成不可变字节。

### 33.4.5 Java：同一个 reducer，使用 `BigInteger` 保住乘积精度

```java
import java.math.BigInteger;
import java.util.List;

public final class BarReducer {
    public record Trade(long timeNs, long seq, long price, long qty) {}
    public record Bar(Long open, Long high, Long low, Long close,
                      long count, BigInteger volume, BigInteger turnover) {}

    private static boolean before(long t1, long s1, long t2, long s2) {
        return t1 < t2 || (t1 == t2 && s1 < s2);
    }

    public static Bar rebuild(long start, long end, List<Trade> trades) {
        if (start >= end) throw new IllegalArgumentException("invalid bucket");
        long o = 0, h = 0, l = 0, c = 0, n = 0;
        long ft = 0, fs = 0, lt = 0, ls = 0;
        BigInteger v = BigInteger.ZERO, q = BigInteger.ZERO;
        for (Trade t : trades) {
            if (t.timeNs() < start || t.timeNs() >= end || t.qty() <= 0)
                throw new IllegalArgumentException("invalid trade for bucket");
            if (n == 0) {
                o = h = l = c = t.price();
                ft = lt = t.timeNs(); fs = ls = t.seq();
            } else {
                h = Math.max(h, t.price()); l = Math.min(l, t.price());
                if (before(t.timeNs(), t.seq(), ft, fs)) {
                    o = t.price(); ft = t.timeNs(); fs = t.seq();
                }
                if (before(lt, ls, t.timeNs(), t.seq())) {
                    c = t.price(); lt = t.timeNs(); ls = t.seq();
                }
            }
            n = Math.incrementExact(n);
            BigInteger qty = BigInteger.valueOf(t.qty());
            v = v.add(qty);
            q = q.add(BigInteger.valueOf(t.price()).multiply(qty));
        }
        return new Bar(n == 0 ? null : o, n == 0 ? null : h,
                       n == 0 ? null : l, n == 0 ? null : c, n, v, q);
    }
}
```

实时聚合状态机还有三件事不能省：

1. 先验证/登记成交身份，再聚合；去重 TTL 必须覆盖重放窗口。内存 TTL 去重不是永久审计去重，长停机恢复需持久事实或安全位点重算。
2. 提交当前桶完整状态、revision、输入位点与 outbox 后才确认消费成功；失效重建父桶的任务也要有幂等身份。
3. 同一时点向客户端广播的是**整个桶的新版本**，不是“再加 2 lot”。消费者迟到收到旧版本时忽略，发现版本冲突时补拉。

空桶策略必须显式：本章事实桶用 `OHLC=null,V=0,count=0`；前端若画“前收平线”，在独立展示序列里标 `synthetic=true`，不可把它伪装成真实成交。缺包导致的空桶与确实无交易也要通过 `quality` 区分。

---

## 33.5 百万连接广播：先编码一次，再把背压限制在每个消费者

### 33.5.1 扇出树与预编码的真实收益

不要让 100 万连接分别订阅上游，也不要每个网关建立 100 万个 Kafka consumer。来源只接有限条冗余连接；投影器按市场产生频道事件，地域 Relay 按网关订阅集合转发，网关在本地对连接列表扇出。

同一频道/同一编码协商组只序列化一次：

```text
领域视图 → JSON / SBE / Protobuf payload（一次）
        → WS frame（可兼容时一次）
        → 同网关本地 N 个连接引用同一不可变内容
        → 每连接自己的写游标、TLS 状态与发送完成状态
```

`payload` 共享省的是重复序列化、用户态拷贝和内存分配，**不是把 N 次发送变成 1 次发送**：每个 socket 仍要传输字节；TLS 的会话密钥、序号和密文是每连接独立的，不能共享一份 TLS ciphertext。内核发送缓冲、TLS staging、压缩状态和库内部缓存仍然占空间。

以下条件才允许共享完整 WS 数据帧：服务端→客户端（未掩码）、相同 opcode/扩展协商、相同内容、无逐连接个性化字段。客户端→服务端必须按 RFC 6455 掩码，不能复用同一固定 mask。协商 `permessage-deflate` 且存在上下文接管时，压缩历史按连接不同，**不能共享同一份压缩结果**；可关闭压缩，或显式采用无上下文接管且验证编码兼容的分组方案。个性化 ACK、授权错误和带用户标记的报文必须另走。

### 33.5.2 Go + `gobwas/ws`：不可变预编码帧与有界单写者

`gobwas/ws` 提供握手、帧读写等底层能力，**不自动提供完整的百万连接事件循环、慢消费者治理或协议状态机**。以下是可测试的写侧核心：按服务端未压缩二进制帧预编码，一个连接一个写者，队列满立即断开恢复，绝不阻塞上游广播。

```go
package fanout

import (
    "bytes"
    "io"
    "net"
    "sync"
    "time"

    "github.com/gobwas/ws"
)

type Frame struct { wire []byte } // 发布后不可变，外部不能取得可写切片

func Encode(payload []byte) (*Frame, error) {
    var out bytes.Buffer
    err := ws.WriteFrame(&out, ws.Frame{
        Header: ws.Header{Fin: true, OpCode: ws.OpBinary,
                          Length: int64(len(payload)), Masked: false},
        Payload: payload,
    })
    if err != nil { return nil, err }
    return &Frame{wire: out.Bytes()}, nil // Buffer 已复制 payload
}

type Peer struct {
    conn net.Conn
    send chan *Frame
    done chan struct{}
    once sync.Once
}
func NewPeer(conn net.Conn) *Peer {
    return &Peer{conn: conn, send: make(chan *Frame, 8), done: make(chan struct{})}
}
func (p *Peer) Close() {
    p.once.Do(func() { close(p.done); _ = p.conn.Close() })
}
func (p *Peer) Offer(f *Frame) bool {
    if f == nil || len(f.wire) > 16*1024 { p.Close(); return false }
    select { case <-p.done: return false; default: }
    select {
    case <-p.done:
        return false
    case p.send <- f:
        return true // 只代表已入队，不代表客户端已收到
    default:
        p.Close() // 深度增量不能静默丢弃后继续发
        return false
    }
}
func writeAll(w io.Writer, b []byte) error {
    for len(b) != 0 {
        n, err := w.Write(b)
        if err != nil { return err }
        if n <= 0 { return io.ErrShortWrite }
        b = b[n:]
    }
    return nil
}
func (p *Peer) RunWriter() {
    defer p.Close()
    for {
        select {
        case <-p.done:
            return
        case f := <-p.send:
            if err := p.conn.SetWriteDeadline(time.Now().Add(2*time.Second)); err != nil {
                return
            }
            if err := writeAll(p.conn, f.wire); err != nil { return }
        }
    }
}
```

这个参考模型有意选择容易审计的边界：队列 8 条、单帧至多 16 KiB、一次阻塞写至多 2 秒。**不是所有产品都该使用这组值**；需要更大快照时使用独立预算和有界分块/快照协议，而非悄悄放开最大帧。控制面必须补齐：

- `send` 永不 `close`，只关闭 `done`，避免广播与关闭竞争产生 `send on closed channel`；停止后由订阅注册表移除 Peer 并释放队列引用。`Offer=true` 与并发断线可同时发生，不能将其当交付确认。
- 业务帧、ping/pong、close 帧都经过同一写者，不能读线程直接写 pong，与数据帧交叉。控制队列要有独立小额度和优先级；若当前写阻塞，只能靠 deadline/关闭 socket 打断。
- 读侧必须校验掩码、opcode、分片、最大消息长度，处理 ping/pong/close；需要时使用 `wsutil`。只启动 writer 而不读控制帧，会把本来健康的连接断掉。
- 断开后可以直接走客户端重连恢复；若想发 `resync_required`，必须给控制帧预留预算，不能假设一个已满、已阻塞的写队列一定能把最后通知送达。
- 共享 `Frame.wire` 不能提前放回 `sync.Pool`。只有全部队列与 in-flight 写完成且无引用，才能复用；最简单的安全版本交给 GC 管理共享帧。
- Go 的 `net.Conn` 通过 runtime netpoll 支持非阻塞等待，但一个 writer goroutine 仍有栈、定时器与调度成本。是否换成 reactor/事件循环由实测决定，不要仅凭“底层有 epoll”就声称连接免费。

### 33.5.3 Java + Netty：共享内容，独立 readerIndex 与引用计数

以下按 Netty 4.1 API 展示。应用 payload 已编码且发布后不再修改；每个 `BinaryWebSocketFrame` 拿一个 `retainedDuplicate()`，共享底层存储但有独立游标。Netty WebSocket encoder 仍负责 WS 帧头，TLS handler 仍逐连接加密，因此这里是**payload 预编码共享**，不是跨连接 TLS 零拷贝。

```java
import io.netty.buffer.ByteBuf;
import io.netty.channel.Channel;
import io.netty.channel.ChannelFuture;
import io.netty.handler.codec.http.websocketx.BinaryWebSocketFrame;

public final class LocalFanout {
    public interface Membership {
        // 必须在该 Channel 所属 EventLoop 调用：移除订阅并关闭/安排重连。
        void evict(Channel channel, String reason);
    }

    // 前置条件：channel.eventLoop().inEventLoop()；本方法消费不了 base 所有权。
    public static boolean offer(Channel channel, ByteBuf base, Membership membership) {
        if (!channel.eventLoop().inEventLoop())
            throw new IllegalStateException("fanout must run on owning EventLoop");
        if (!channel.isActive()) return false;
        if (!channel.isWritable()) {
            membership.evict(channel, "slow_consumer");
            return false;
        }
        BinaryWebSocketFrame frame = new BinaryWebSocketFrame(base.retainedDuplicate());
        ChannelFuture future;
        try {
            future = channel.write(frame); // 成功移交后由 Netty 释放，包括异步失败
        } catch (RuntimeException failure) {
            frame.release();              // 尚未成功移交的异常路径
            membership.evict(channel, "write_rejected");
            return false;
        }
        future.addListener(result -> {
            if (!result.isSuccess()) membership.evict(channel, "write_failed");
        });
        return true;
    }
}
```

所有权与调度约定比这十几行写调用更重要：

1. 编码器创建 `base`，持一个引用；向每个 EventLoop 的**有界邮箱**投递一个广播任务，任务各持有一份 retain；该 EventLoop 遍历自己拥有的订阅者，用 `offer` 创建各连接引用，批量 `flush`，最后释放任务引用。发布者在投递结束后释放初始引用。
2. EventLoop 邮箱拒绝任务、channel 关闭、任务取消时都必须释放对应引用；不能每个订阅者每条消息都 `eventLoop.execute`，否则尚未进入 ChannelOutboundBuffer 的任务已先在执行器队列无界堆积。
3. 配置 `WriteBufferWaterMark`，例如低水位 32 KiB、高水位 64 KiB，并限制单帧大小。`isWritable` 是信号，不是强制内存硬上限；当前帧、TLS/编码临时缓冲以及自建任务队列仍会增加占用，必须一起记账。
4. `write` 返回只表示交给 pipeline；future 成功通常是本地传输完成，不是客户端应用已应用该序列。客户端处理延迟需要独立的序列 ACK/采样回执观测。
5. `ByteBuf.duplicate()` **不增加引用计数**，不能当成异步持有；`retain()` 共享相同游标，也不适合多个 pipeline 独立消费。使用 `retainedDuplicate()` 并确保交付一次、释放一次。压测/灰度启用泄漏检测验证异常路径。
6. EventLoop 不做阻塞 REST、数据库、压缩大对象和巨量 JSON 解析。热点频道 fanout 可按订阅者分块、按轮次调度，但每连接消息顺序和任务总预算必须保持不变。

### 33.5.4 慢消费者策略必须按消息语义拆开

| 消息类型 | 可否合并/丢弃 | 满队列后的正确动作 |
|---|---|---|
| 最新 ticker / 完整 BBO | 可以 latest-only | 覆盖尚未发送的同类旧状态，标明采样语义 |
| 完整 Top-N 快照 | 可替换未开始发送的旧快照 | 发送最新完整快照，不能截断正在发送的帧 |
| 严格 L2 增量链 | 不能任意丢一条继续 | 标记失步，停止该链，重新快照或断线恢复 |
| 当前桶完整 K 线版本 | 同桶可留最新 revision | 保留桶身份与 revision，历史订正可补拉 |
| 逐笔成交流 | 实时通道不能声称无损又丢包 | 有限重放/持久补拉；超出窗口明确报 gap |
| 下单/成交私有回报 | 不属于公共行情降级策略 | 独立持久通道与业务查询，不随 ticker 丢弃 |

一般同时设置三种上限：队列字节数、队列最老事件年龄、单次写耗时。例如队列 64 KiB / 最老 500 ms / 单次写 2 s，任一越界便降频、切完整快照频道或剔除；具体阈值需按产品 SLO 验证。不要只限制消息条数，一条全簿快照可能比一千条 BBO 更大。

客户端重连携带 `(channel, stream_epoch, last_seq)`。服务端仅在相同 epoch 且有限 ring 中有连续尾部时重放，否则返回新快照；ring 必须限制总字节与时间。不能用“从最后 seq 往后发”掩盖中间缺了一段，也不能承诺在无限离线后继续补所有逐笔。

### 33.5.5 容量算账：100 万空连接与 100 万活跃订阅不是一件事

以下是**规划假设，不是性能承诺**：峰值 `C=1,000,000`；每连接平均两个频道、每频道 5 条/秒；平均 payload 300 B；估计总线长 400 B（帧头、TLS/TCP/IP 和批处理效果需实测）。

```text
下行投递率 = 1,000,000 × 2 × 5 = 10,000,000 次/s
payload 吞吐 = 10,000,000 × 300 B = 3.0 GB/s
估计链路吞吐 = 10,000,000 × 400 B = 4.0 GB/s ≈ 32 Gbit/s

20 台网关：每台 50,000 连接、500,000 次/s、约 1.6 Gbit/s
40 台网关：每台 25,000 连接、250,000 次/s、约 0.8 Gbit/s
```

网卡标称 10/25 Gbit/s 不是可用吞吐证明；小包 PPS、TLS CPU、系统调用、热点 EventLoop 和 `softirq` 可能先到天花板。按上述负载，若单次投递平均独占 CPU 为 1 µs，全集群就需要约 10 个满载 CPU 核，仅这一个步骤还没算 TLS、序列化与接入。预编码不会消掉 `C × rate` 的乘法。

每连接内存必须实测拆账：

| 项 | 假设均值 | 100 万连接规模 |
|---|---:|---:|
| 应用连接状态/订阅/队列引用 | 6 KiB | 约 5.7 GiB |
| goroutine 栈或 Netty 对象/TLS 状态 | 14 KiB | 约 13.4 GiB |
| 内核 socket 与实际收发缓冲 | 32 KiB | 约 30.5 GiB |
| 发送积压预算 | 平均 4 KiB | 约 3.8 GiB |
| 小计 | 56 KiB | 约 53.4 GiB，不含共享簿/页缓存/安全余量 |

如果给每连接预分配 64 KiB，仅此一项就约 **61 GiB**。共享 payload 仍可能被最慢连接长期钉住，导致很多历史帧无法回收，因此必须监控 retained bytes 与最老引用年龄，不只看“序列化次数下降”。内核自动调节缓冲的上限与实际占用不是同一个数，要同时看 RSS、direct memory、socket memory 和 cgroup 口径。

20 台方案在失去 5 台时，每存活节点至少需承载 `1,000,000/15 ≈ 66,667` 条连接和同比消息量；若单节点稳态只验过 50,000，它就没有宣称中的故障冗余。容量同时受**存量连接、持续投递、连接新建、订阅变更、快照恢复**五个维度约束，不能只用 FD 数量推算。

心跳与重连也要算：30 秒一次 ping 约 `33,333 次/s`，再加 pong；使用时间轮和随机相位，避免整点齐发。100 万连接在 60 秒里恢复需要约 `16,667 次 TLS 握手/s`；要本地验签、连接准入令牌桶、指数退避加全抖动，快照回源做合并和全局限额，不能让每个浏览器直接抓上游 REST。

---

## 33.6 对账、压测与故障演练：验证“不撒谎”，而不只是“发得快”

### 33.6.1 分层核验与建议 SLO

SLO 以下仅为评审起点，必须注明市场、网络边界、测量时钟与统计窗口。不同地域公网端到端延迟不能拿同一机房数字承诺。

| 层 | 核验内容 | 示例目标/告警 |
|---|---|---|
| 接入 | profile、原文 hash、重复/缺口、REST 限流 | 任何未检测到的注入 gap 都不允许 |
| L2 | 同 epoch/同 `u` 的规范排序 hash、Top-N/覆盖边界 | 可比位点状态差异为 0；断档后禁止继续 `LIVE` |
| 新鲜度 | event/receive/apply/publish 时间与来源进度 | 活跃市场 `receive→publish p99 < 50 ms` |
| 恢复 | `gap→stale`、重建成功率、快照年龄 | `stale` 状态在本地检测后立即生效；恢复 p99 < 3 s（来源可用时） |
| K 线 | 有效成交 count/volume/turnover/OHLC | 与离线 reducer 按版本逐桶完全相等 |
| 网关 | 每频道尾部 seq、队列年龄/字节、投递失败 | 正常客户端不受慢消费者拖累；队列始终有界 |
| 客户端 | 已应用 seq、显示行情年龄、重连次数 | 采样 ACK 的处理延迟满足产品目标 |

源端 `event_time` 与本机时钟偏差会污染延迟分位数。拆开 `source→receive` 与本机 `receive→publish`，单机阶段用单调时钟测耗时，事件时间用于业务桶，不拿 NTP 回拨后的墙钟相减当真实网络延迟。无成交市场的“最后成交时间旧”也不等于连接死了，要结合来源心跳与进度。

checksum 应明确：bids/asks 排序、覆盖档数、价格/量的整数文本、分隔符、字节序、hash 算法与元数据版本。CRC 可检错但不是防恶意篡改签名；撮合事件审计需要另一条有身份认证与持久性保证的链。

**REST 对账不能比较两个不同时刻的裸结果。** 流程应是：新建影子重建器 → 获取快照 `S` → 回放同一段已归档增量到公共位点 `u` → 比较同 epoch、同覆盖范围的 hash。仅用 `REST 现在的 bestBid` 对比 `WS 20 ms 前 bestBid`，既会制造假差异，也可能在不同错误间碰巧对上。

### 33.6.2 K 线有效成交的离线核验 SQL

对于 `NEW/REPLACE/CANCEL` 修订，先取最新版本，再按事件时间筛桶；顺序不能反过来，否则被更正到别的桶的旧版本会“复活”。下面假设 `:cutoff_ns` 是审计可见时间截点：

```sql
WITH latest AS (
    SELECT DISTINCT ON (venue, instrument, trade_id) *
    FROM md_trade_revision
    WHERE venue = :venue AND instrument = :instrument
      AND available_time_ns <= :cutoff_ns
    ORDER BY venue, instrument, trade_id, revision DESC
), live AS (
    SELECT * FROM latest
    WHERE action <> 'CANCEL'
      AND event_time_ns >= :bucket_start_ns
      AND event_time_ns <  :bucket_end_ns
)
SELECT count(*) AS trade_count,
       coalesce(sum(qty_lots::numeric), 0) AS volume_lots,
       coalesce(sum(price_ticks::numeric * qty_lots::numeric), 0) AS turnover_units,
       max(price_ticks) AS high_ticks,
       min(price_ticks) AS low_ticks
FROM live;
```

open/close 还需按 `(event_time_ns, source_seq)` 排序求首尾；若 source_seq 跨来源 epoch 不可比，查询应使用规范化后的稳定全序键。`available_time_ns` 是用于历史回放的系统可见时间，不等于交易发生时间；“截至当时知道什么”和“今天修正后的历史”必须提供两个查询入口。

### 33.6.3 正确性测试矩阵

| 注入场景 | 必须观测到的结果 |
|---|---|
| 快照获取中消息持续到来、快照响应乱序 | 同 generation 单写者切换，无空隙；旧响应不生效 |
| 初始化事件 `u==S` | RANGE_NEXT 丢弃；PREV_LINK 可桥接且不错误检查首条 `pu` |
| `pu` 链不匹配 / `U>L+1` | 立即退出 LIVE，不发布后续错误簿 |
| 重复、重叠区间、旧事件重放 | 最终价位一致，游标不倒退，重复不造成累加 |
| 新连接与旧连接回调交错 | 旧 generation 被隔离，不跨流盲混 |
| 缓冲满、解析异常、数量溢出 | 整批不提交，触发可观测重建 |
| 有限快照边界被扫穿 | `depth_complete=false` 或提前补全，无“假完整” |
| 同秒成交乱序、相同时间不同 seq | OHLC 与固定排序 oracle 完全一致 |
| 迟到、超迟到、官方撤销/改价/改时 | 修订可追踪；父桶联动；重放幂等 |
| checkpoint/outbox 提交前后分别 kill | 无重复计量、无已提交修订丢失 |
| `ByteBuf` 写失败/任务拒绝/频道取消 | 引用归零，无泄漏、无 use-after-release |
| Go 共享帧写完前尝试池复用 | 测试能检测内容污染；正式路径禁止早回收 |
| 客户端停读、半开、RTT 抖动 | 队列有界并剔除，不拖慢其他客户端 |

用属性测试生成合法事件流，再随机插入重复、丢失、断线：**无故障时重建结果等于 reference book；有缺口时结果要么正确，要么明确不可用，绝不允许错误且 LIVE。** Go 用 `-race` 覆盖 actor 边界和 Peer 关闭；Java 对跨 EventLoop 所有权做并发压力测试。序列闸门的 Go / Java 用同一份黄金 JSON 向量对拍，特别检查 `S+1`、`u==S`、最大整数和缺失 `pu`。

### 33.6.4 负载模型与验收

至少分六种压测，避免“百万空闲连接成功”遮住真正瓶颈：

1. **连接面**：TLS/鉴权全开，连接阶梯 10 万→50 万→100 万；测握手率、FD、conntrack、accept backlog、内核内存与重连恢复时间。
2. **活跃面**：采用 §33.5 的频道数/推送频率/报文大小，给出 delivered messages/s 与链路带宽，而不是只报源端“每秒发布 10 条”。
3. **热点面**：80% 连接订阅同一市场；测最慢 EventLoop、热点 Relay 和 fanout 轮转延迟。平均 CPU 50% 不代表热点单核未满。
4. **慢消费者面**：1%/5%/20% 连接完全停读或限速到业务吞吐以下；内存到达平台后必须稳定，正常消费者 p99 不出现持续恶化。
5. **恢复面**：随机摘掉 25% 网关、来源断流、REST 429、快照延迟 5 秒；检查快照服务全局预算与客户端退避是否有效。
6. **长稳态**：至少覆盖完整日切、来源强制重连周期和多轮发布；观察 retained bytes、Netty direct memory、Go 堆存活与 goroutine/任务数量是否缓慢爬升。

压测客户端也要有足够机器与源 IP；单个负载机的临时端口、CPU、NIC 或 NAT 表可能先耗尽。用独立消费者核验接收并应用的频道序列，不把 `write()` 调用次数当成功交付量。使用开放式到达率和独立排队计时，避免客户端因变慢自动降载而造成 coordinated omission。

发布时先灰度一个市场/一组网关，影子对拍旧新投影；摘流顺序是停止新接入 → `draining`（带随机重连窗口）→ 订阅迁移/客户端重连 → 超时关闭。不能一次关闭 20% 连接却不为鉴权、TLS、快照预留 20% 以上恢复容量。

---

## 33.7 极端边界、术语与面试自检

### 33.7.1 极端边界与反模式清单

| 边界/误区 | 为什么会出错 | 工程动作 |
|---|---|---|
| “WS/TCP 保序，所以不检查序列” | 保序不覆盖重连和应用内丢弃 | 对每个来源 profile 校验序列，缓冲溢出算 gap |
| “REST 200 就说明重建完成” | REST 与实时流还未桥接 | ALIGNING 结束后才原子进入 LIVE |
| 快照请求超时但旧响应后来到 | 会覆盖更晚的新簿 | 恢复 request_id + generation 双重过滤 |
| 来源主备切换后序列归零 | 较小 u 被永远当旧事件 | epoch/来源会话改变时强制重建 |
| 两条冗余 WS 任意交织 | `pu` 是各自消息链，分批边界可能不同 | 每条流独立重建/验证，按公共位点切换，不拼接裸消息 |
| 无消息即空簿 | 无交易/无深度变化都可能无消息 | 靠心跳、进度与明确快照判断，不清空猜测 |
| 单边空簿 | 可能真实缺流动性，不一定数据错误 | 允许一侧无最佳价，BBO 用 null；不制造虚假价格 |
| 重建过程中跨盘 | 半条消息或未桥接的候选态不应外显 | 整批校验，候选簿与已发布簿隔离 |
| 股票停牌、集合竞价、涨跌停 | 连续竞价规则不一定适用 | 发布 session 状态与交易日历版本 |
| 负油价/合约单位变更 | 固定 `price>0`、沿用旧倍率会错 | 产品定义价格域；精度变化用新元数据/新序列版本 |
| 大规模撤单导致巨帧 | 单帧、压缩和编码会卡住 event loop | 限大小、限批次、分片协议，快照按总预算恢复 |
| UTC 日线当本地交易日 | 跨夜盘/夏令时/节假日均可能错桶 | 按交易所日历构建 series key |
| 墙钟回拨/来源未来时间 | 延迟负数、watermark 错跳 | 单调耗时；事件时间异常隔离；不推进完整性 |
| 极迟到直接丢弃 | 官方成交事实和 K 线永久漂移 | 持久修正任务 + 新版本 + 父桶失效 |
| “FINAL 不允许变” | 真实交易也会 bust/correct | 版本不可覆盖，FINAL 可追加审计修订 |
| 无界重放/无限保留去重 key | 一次断网即可耗尽资源 | 明确 replay retention、补拉接口与快照降级 |
| 只盯 Java heap / Go heap | direct、TLS、socket 内存仍可 OOM | 统一进程/内核/cgroup 容量账 |
| 鉴权通过就能无限订阅 | 频道数、频率、解压炸弹均可攻击 | 每连接/租户/IP 订阅、字节、控制消息和解压额度 |
| 公共行情与私有回报混用降级 | 丢 ticker 语义不适用于订单回报 | 通道/预算/补查策略隔离 |
| 断线时一定能发送最后提示 | 已堵塞的 socket 发不出去 | 客户端自身检测 gap/epoch，提示仅尽力而为 |

### 33.7.2 术语正本清源

| 术语 | 本章精确定义 | 最常见混淆 |
|---|---|---|
| L1 / BBO | 最优买卖价及对应量，具体字段以协议为准 | 不等于最新成交价 |
| L2 / MBP | 按价格聚合的可见买卖深度 | 不等于逐订单 L3/MBO，更不含隐藏流动性 |
| Snapshot | 在明确序列边界上的状态 | 不是随便遍历一次正在变动的 map |
| Delta | 状态变化报文，可能是绝对量覆盖 | “增量流”不必然表示数值相加 |
| Gap | 协议定义的连续性条件不满足 | 不一定是 IP 包丢失，也可能是应用队列丢事件 |
| Epoch / Generation | 序列命名空间 / 本地恢复尝试代际 | 不是可互换的时间戳 |
| Watermark | 事件时间处理进度策略 | 不是永不迟到的数学保证 |
| Late Event | 到达时所属桶已跨过处理进度 | 不是按接收时间塞进当前桶 |
| Revision | 同一业务桶的后继完整状态版本 | 不是一笔新的成交量增量 |
| Fanout | 一份频道事件向多个消费者分发 | 不代表网络广播或“一次写发给所有 TCP” |
| Backpressure | 限制积压并使压力有确定出口 | 不是把无界队列换个名字 |
| Pre-encode / Zero-copy | 复用编码和底层内容，减少特定层复制 | 不代表所有层零分配、TLS 零加密或零系统调用 |
| Slow Consumer | 消费能力低于所订阅事件产生速率的连接 | 不只是 RTT 大；应用不读也一样 |
| PIT / As-of | 截至历史可见时点可知道的数据版本 | 不是今天修完的历史全量数据 |

### 33.7.3 面试系统设计：10 分钟答题卡

**0–1 分钟：约束卡。** 先问市场数、L1/L2/逐笔、频道频率、平均订阅数、峰值在线、延迟边界、有限重放与历史留存、是否供自动交易。明确公共行情不是私有成交回报，也不是标记价的唯一来源。

**1–3 分钟：正确性主链。** 画 REST+WS → 来源适配 → 单市场单写者 → 不可变快照 → Relay → 网关。先 WS 缓冲，再 REST，按 profile 桥接、原子回放；`U,u,pu` 不混产品规则，断档立即失去 LIVE，snapshot/generation 解决旧响应竞争。

**3–4 分钟：L2 语义。** 数量绝对覆盖、零量删除、整数 tick/lot、同批原子提交；快照有限覆盖不是全簿；Top-N 出入档必须补完整量/删除；checksum 在同位点比较。

**4–6 分钟：K 线。** 以成交事件时间分桶，trade ID 幂等，稳定总序决定开收盘；watermark 与迟到窗口，完整桶 revision 更新；撤销最高价必须重算，父桶联动；两种历史查询区分“当时可见”与“事后修订”。

**6–8 分钟：容量与广播。** 算 `连接 × 订阅 × 频率 × 字节`，不是“有 epoll 所以百万”。同编码组预编码、Netty retainedDuplicate / Go immutable frame，每连接单写者，TLS 仍逐连接处理；EventLoop 前也要有界邮箱。

**8–9 分钟：慢消费者和恢复。** ticker 可合并，严格增量不能静默丢；字节/年龄/写超时三阈值，超限断开或切快照；epoch+seq 有限重放，超窗快照；新建连接与快照回源限流，客户端带 jitter。

**9–10 分钟：验收与边界。** 同位点 hash、逐桶离线对拍、丢包/迟到/重启/停读注入、百万活跃连接与热点频道长稳态。最后主动说出：L2 不能精确还原队列、REST 无限重抓会雪崩、物理零拷贝/TLS 共享是错误承诺。

常见追问的一句话答案：

- **为什么不能拿 `u` 大于当前值就更新？** 因为单调不代表连续，缺失价位可能永远残留。
- **为什么第一条 `pu` 不要求等于快照 S？** 快照可能位于该消息覆盖区间内部，第一条按来源的桥接条件对齐，后续才按链校验。
- **为什么广播成功客户端却错了？** 入队、socket 写完和应用完成是三个边界；客户端需原子快照安装和 seq 校验。
- **为什么 K 线量对、收盘价仍错？** 用到达顺序替代事件时间总序，或迟到成交只加量没改 open/close。
- **一个慢连接能拖垮预编码吗？** 能，它会钉住共享历史帧，并累积内核/TLS 缓冲，所以共享编码不能替代背压。

### 33.7.4 自检推演题与参考要点

**题一：快照衔接。** `S=500`，缓冲 `(U=498,u=500,pu=497)`、`(501,503,500)`、`(504,506,503)`。分别按两个 profile 处理，LIVE 后收到 `(509,511,508)` 怎么办？

参考：RANGE_NEXT 丢第一条，从第二条覆盖 `501` 桥接，第三条后 `L=506`。PREV_LINK 第一条覆盖 `S`，可桥接到 `500`，第二条 `pu=500`、第三条 `pu=503` 连续。最后一条两个 profile 均断档，停止 LIVE 并恢复；不能先更新到 `511` 再报警。

**题二：迟到与撤销。** 一个桶有 `10:00:10` 的 `100×2`、`10:00:40` 的 `105×3`；先迟到 `10:00:05` 的 `98×1`，再撤销 `105×3`。分别算 OHLC/V/Q，并说明存储和父桶变化。

参考：初始 `100/105/100/105,V=5,Q=515`；迟到后 `98/105/98/105,V=6,Q=613`；撤销后 `98/100/98/100,V=3,Q=298`。每次新修订保留旧版本、输入截点与原因；父桶失效重算；不是覆盖历史记录，也不是往新一分钟扣量。

**题三：容量和剔除。** 100 万连接，平均两个频道，每频道 10 条/s，每次线长 500 B，5% 客户端停读。若允许每连接最多积压 64 KiB，带宽和积压预算各是多少？能否说共享 payload 后这些都接近零？

参考：`20,000,000 次/s × 500 B = 10 GB/s ≈ 80 Gbit/s`。5 万慢连接的逻辑积压上限约 `3.05 GiB`，全体最坏约 `61 GiB`，不含内核/TLS/任务队列；共享会减少内容物理副本，但引用、独立 socket 与最老帧存活仍存在，无法消掉线速。严格增量超限必须失步恢复，不能直接丢掉最老一条继续发送。

**题四：历史是否穿越。** `09:30` 桶在 `09:31` 发布 revision 1，`09:35` 因迟到修为 revision 2。回测 `09:32` 的策略该读取哪一版？

参考：按 `available_time <= 09:32` 只能读 revision 1；用事件时间过滤后取“今天最新”会偷看 09:35 才知道的修正。保存双时间与所有修订不是存储浪费，而是避免未来函数的必要条件。

---

## 33.8 系统衔接：行情是有质量标签的投影，不是万能价格真相

### 33.8.1 与撮合、OMS、风控的接口契约

```text
31/32 撮合与 durable journal
  ├─ BookDelta → 本章 L2 投影 → 带 epoch/seq/覆盖范围的公共行情
  ├─ Trade     → 本章 K 线/逐笔 → 带 revision/available_time 的历史数据
  └─ Execution→ 29 OMS / 22 账本 → 独立业务身份与资金分录

本章质量状态 → 30 风控/策略价格闸门
                  │ 可用来源、最大年龄、完整性、置信度
                  └─ 不能因公共 WS 仍连着就允许无限交易
```

| 上下游 | 本章交付 | 对方必须保证/禁止 |
|---|---|---|
| 31/32 撮合与主备 | 从已确认持久化的输出构造 L2/成交视图，带 source_epoch 与 engine 边界映射 | 未提交输出不得伪装成最终成交；主备切换显式 fencing/epoch |
| 29 OMS | 独立的报价视图、行情年龄与可用性；便于执行算法估算滑点 | 公共行情不是订单是否成交的回执；超时仍通过 OMS 身份查单 |
| 30 风控/强平 | 每个参考源的价格、as-of、状态、完整性及异常标签 | 标记价用独立规则组合指数/参考源；不能把单一 latest trade 直接当强平触发器 |
| 27 对账 | 成交身份、规范化日志位点、同位点快照 hash、K 线有效输入截点 | 把真差异、跨时点假差异和缺覆盖区间分开，不自动改资金账 |

风控接口不只返回一个 `price`，至少返回 `source_set_version / as_of / freshness / status / dispersion / metadata_version`。市场剧烈波动时，“拒绝没有可信价格的新风险”与“保留减仓、撤单能力”应由风控策略明确，而非由行情网关直接决定交易权限。

### 33.8.2 与第 34 章券商公司行动、交易日历的衔接

第 34 章进入综合账户、银证换汇、融资融券和公司行动。本章应提前交付三个稳定接口：

1. **标的主数据接口**：稳定 `instrument_id`、交易所代码、生效时间、tick/lot/合约倍率、币种与日历版本。股票代码更名不应把历史行情接到另一个资产；拆合股后旧数量标度不能直接继续拼接增量簿。
2. **原始成交与复权视图分离**：公司行动不能修改过去真实成交价格、成交数量和结算义务。前复权/后复权在独立 `series_version` 下生成，写明因子版本、可见时间与舍入口径；实时原始 L2 不做历史复权。
3. **停复牌与挂单清退边界**：停牌、除权日开市、重新挂牌需要 session 状态事件；若盘口被清空并重建，发新 epoch/快照，而不是让客户端用旧簿继续接新精度增量。公司行动对实际持仓/现金的变更仍走第 34 章业务事件与账本，不走 K 线修正。

例如 `1 股拆 10 股`，图表可把历史价格除 10、历史量乘 10 形成可比序列；不能据此声称当年的撮合原始成交变成了十笔，更不能让历史成交回报重新触发账本入账。

### 33.8.3 与历史研究、AI 和审计的衔接

历史消费者需要两种明确命名的 API：

```text
GET bars?series=raw&as_known_at=...   # 当时实际可见的版本：回测/决策复现
GET bars?series=raw&latest=true      # 现在修正后的历史：研究/数据核验
```

未来的金融时序 RAG、策略回测和 AI 决策复现必须用第一种语义；若给它们最新修正的历史却声称是当时行情，会产生未来函数。行情中的 `event_time`、`available_time`、`revision`、`series_version` 要一路保留到特征和引用证据，不能在导出 Parquet 时只剩一个 `timestamp`。

审计应能串起：`原始来源报文 hash → 规范化事件身份 → 投影输入位点 → 快照/桶修订 → 下行 epoch/seq → 抽样客户端应用回执`。这条链证明的是数据投影和分发过程，不代替账本的经济义务证明，也不代替交易所本身对成交真实性的保证。

### 33.8.4 交付前检查表

- [ ] 每个来源都有冻结的 profile 与真实录制回放样本，现货/合约边界不混用。
- [ ] 先 WS 缓冲再 REST；恢复请求与连接代际均有 fencing；gap 后不会继续 LIVE。
- [ ] 价位绝对量、零量删除、有限覆盖、Top-N 出入档与批次原子性已测试。
- [ ] 成交去重、迟到、更正/撤销、跨桶修正和父桶联动均有确定性 oracle。
- [ ] K 线有 revision、available_time、series_version 和完整性，不把 CLOSED 当永不修正。
- [ ] Go 共享帧不会提前复用；Netty 引用计数在写失败、任务拒绝和关闭路径均释放。
- [ ] EventLoop 前置邮箱、每连接队列、重放 ring、快照服务和握手入口都有预算。
- [ ] 压测覆盖百万活跃连接、热点频道、停读客户端、25% 网关失效与长稳态，而非只测 FD。
- [ ] 已与第 34 章约定主数据、公司行动、复权序列与 session 切换契约，行情修订不会重新触发资金入账。

**一句话收束**：好的行情系统不承诺“网络永不丢、客户端永不慢”，而是保证**丢了能识别、旧了不冒充新、错了能重建、历史能修正、慢者不拖垮全体，并且每个数字都知道自己来自哪个版本的事实。**
