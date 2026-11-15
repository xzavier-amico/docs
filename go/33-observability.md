# 33. 可观测性：slog、OTel、Prometheus

> **状态**：`待撰写` ｜ **所属**：第六部分 · 工程化 ｜ **编号**：33 / 50
> **一句话导读**：`log/slog`（Handler / 分组 / 零分配技巧）；日志上下文与 trace id 传递；OpenTelemetry Go 三件套；**Agent 的语义约定：`llm.request`、`tool.call` span**；指标设计（延迟分位、token 计数、错误率）；采样与成本；与 Logback/Micrometer/Sleuth 对照
> **本章写作骨架**：场景引子 → 直觉模型 → 底层实现（结构体/源码/图） → Java 对照 → 开源框架落地 → 踩坑清单 → 一页速记 + 自检题
> **风格要求**：轻松、口语化、类比多；只讲 Java 程序员会猜错的地方；不重复其它章节已讲内容（重复内容用「详见第 N 章」）。

## 本章计划小节

1. `log/slog`（Handler / 分组 / 零分配技巧）
2. 日志上下文与 trace id 传递
3. OpenTelemetry Go 三件套
4. **Agent 的语义约定：`llm.request`、`tool.call` span**
5. 指标设计（延迟分位、token 计数、错误率）
6. 采样与成本
7. 与 Logback/Micrometer/Sleuth 对照

## 源码与框架坐标

- `log/slog`
- otel-go
- client_golang

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
- 前置：第 32 章
- 下一章：第 34 章
