# 43. 多 Agent 协作与持久化执行

> **状态**：`待撰写` ｜ **所属**：第七部分 · AI-Agent 应用架构 ｜ **编号**：43 / 50
> **一句话导读**：拓扑：supervisor / hierarchical / debate / handoff；通信与共享状态（消息总线 / channel）；资源预算与失败隔离；**长久运行任务：Temporal Go SDK 的 workflow/activity、确定性重放、signal/query**；工作流引擎 vs 自研状态机；事件驱动（Kafka / NATS）
> **本章写作骨架**：场景引子 → 直觉模型 → 底层实现（结构体/源码/图） → Java 对照 → 开源框架落地 → 踩坑清单 → 一页速记 + 自检题
> **风格要求**：轻松、口语化、类比多；只讲 Java 程序员会猜错的地方；不重复其它章节已讲内容（重复内容用「详见第 N 章」）。

## 本章计划小节

1. 拓扑：supervisor / hierarchical / debate / handoff
2. 通信与共享状态（消息总线 / channel）
3. 资源预算与失败隔离
4. **长久运行任务：Temporal Go SDK 的 workflow/activity、确定性重放、signal/query**
5. 工作流引擎 vs 自研状态机
6. 事件驱动（Kafka / NATS）

## 源码与框架坐标

- temporal Go SDK
- franz-go
- nats.go

## Java 对照锚点（写作时填充：同构点 / 差异点 / 为什么这样设计）

- 

## 踩坑清单（写作时填充：可直接抄进 CR checklist）

- [ ] 

## 一页速记（写作时填充）

| 关键词 | 一句话结论 |
| --- | --- |
|  |  |

## 自检题（写作时填充，3 题 + 答案要点）

1. 

## 交叉引用

- 总纲：[0-summary.md](0-summary.md)
- 前置：第 42 章
- 下一章：第 44 章
