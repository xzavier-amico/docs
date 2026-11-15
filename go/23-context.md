# 23. context：取消传播（Agent 的命脉）

> **状态**：`待撰写` ｜ **所属**：第四部分 · 并发 ｜ **编号**：23 / 50
> **一句话导读**：接口与树形结构；`cancelCtx`（children map / `propagateCancel`）、`timerCtx`、`valueCtx` 链与查找成本；`WithCancelCause`；`Canceled` vs `DeadlineExceeded`；**取消如何穿透 net/http、database/sql、gRPC**；「一次用户请求 → 多次 LLM 调用 + 并行工具 → 全链路超时」实战；与 ThreadLocal + `CompletableFuture.cancel` 对照
> **本章写作骨架**：场景引子 → 直觉模型 → 底层实现（结构体/源码/图） → Java 对照 → 开源框架落地 → 踩坑清单 → 一页速记 + 自检题
> **风格要求**：轻松、口语化、类比多；只讲 Java 程序员会猜错的地方；不重复其它章节已讲内容（重复内容用「详见第 N 章」）。

## 本章计划小节

1. 接口与树形结构
2. `cancelCtx`（children map / `propagateCancel`）、`timerCtx`、`valueCtx` 链与查找成本
3. `WithCancelCause`
4. `Canceled` vs `DeadlineExceeded`
5. **取消如何穿透 net/http、database/sql、gRPC**
6. 「一次用户请求 → 多次 LLM 调用 + 并行工具 → 全链路超时」实战
7. 与 ThreadLocal + `CompletableFuture.cancel` 对照

## 源码与框架坐标

- `context/context.go`
- 任何 LLM SDK 的 ctx 语义

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
- 前置：第 22 章
- 下一章：第 24 章
