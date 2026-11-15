# 43. Agent 平台设计模式目录（25 个）

> **状态**：`待撰写` ｜ **所属**：第九部分 · 模式、选型与落地 ｜ **编号**：43 / 45
> **一句话导读**：编排类（Router / Orchestrator-Worker / Reflection / Plan-Execute）、数据类（Event Sourcing / Outbox / CQRS）、稳定性类（Bulkhead / Circuit Breaker / Backpressure / Load Shedding / Saga）、成本类（预扣结算 / 语义缓存 / 预算令牌）、协作类（Handoff / Supervisor / Blackboard）；每个模式给「何时用 / 何时别用 / 失败模式」
> **本章写作骨架**：场景账本（不做会怎样，带数字）→ 结构图 → 关键设计（schema/伪代码）→ Java 对照 → 开源坐标 → 事故清单 → 一页速记 + 自检题
> **贯穿案例**：Aurora 平台（10 万租户 / 峰值 5k QPS / 单 Run 30 秒 / 流式输出）

## 本章计划小节

1. 编排类（Router / Orchestrator-Worker / Reflection / Plan-Execute）、数据类（Event Sourcing / Outbox / CQRS）、稳定性类（Bulkhead / Circuit Breaker / Backpressure / Load Shedding / Saga）、成本类（预扣结算 / 语义缓存 / 预算令牌）、协作类（Handoff / Supervisor / Blackboard）
2. 每个模式给「何时用 / 何时别用 / 失败模式」

## 开源与对照坐标

- 经典分布式模式 + Agent 变体

## Java / 分布式经验对照锚点（写作时填充：同构点 / 差异点 / 会咬人的地方）

- 

## 事故清单（写作时填充：这个模块上线后最可能怎么炸 + 兜底预案）

- [ ] 

## 一页速记（写作时填充）

| 关键词 | 必须记住的结论 |
| --- | --- |
|  |  |

## 自检题（写作时填充，3 题 + 答案要点）

1. 

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 语言/运行时细节：见 [../go/0-summary.md](../go/0-summary.md)
- 前置：第 42 章
- 下一章：第 44 章
