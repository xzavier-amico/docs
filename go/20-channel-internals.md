# 20. channel 原理与编排模式

> **状态**：`待撰写` ｜ **所属**：第四部分 · 并发 ｜ **编号**：20 / 50
> **一句话导读**：`hchan` 结构、环形缓冲、`sendq/recvq/sudog`、**直接移交（direct handoff）**；close 的三条规则；happens-before 与内存模型；无缓冲 channel 的同步本质；**编排模式**：pipeline / fan-out-fan-in / orchestrator-worker / 令牌桶 / 结果汇聚；何时该用 Mutex 代替 channel
> **本章写作骨架**：场景引子 → 直觉模型 → 底层实现（结构体/源码/图） → Java 对照 → 开源框架落地 → 踩坑清单 → 一页速记 + 自检题
> **风格要求**：轻松、口语化、类比多；只讲 Java 程序员会猜错的地方；不重复其它章节已讲内容（重复内容用「详见第 N 章」）。

## 本章计划小节

1. `hchan` 结构、环形缓冲、`sendq/recvq/sudog`、**直接移交（direct handoff）**
2. close 的三条规则
3. happens-before 与内存模型
4. 无缓冲 channel 的同步本质
5. **编排模式**：pipeline / fan-out-fan-in / orchestrator-worker / 令牌桶 / 结果汇聚
6. 何时该用 Mutex 代替 channel

## 源码与框架坐标

- `runtime/chan.go`
- Agent 中工具结果汇聚
- token 转发

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
- 前置：第 19 章
- 下一章：第 21 章
