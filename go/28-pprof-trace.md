# 28. pprof 与 trace 实战

> **状态**：`待撰写` ｜ **所属**：第五部分 · 运行时与性能 ｜ **编号**：28 / 50
> **一句话导读**：CPU/heap/goroutine/mutex/block profile 的采样原理；火焰图解读；execution trace 里 G 的状态机；`-benchmem` + `benchstat`；微基准的五个陷阱；线上 profile 的安全做法；**用它定位 Agent 的 token 流延迟与工具热点**
> **本章写作骨架**：场景引子 → 直觉模型 → 底层实现（结构体/源码/图） → Java 对照 → 开源框架落地 → 踩坑清单 → 一页速记 + 自检题
> **风格要求**：轻松、口语化、类比多；只讲 Java 程序员会猜错的地方；不重复其它章节已讲内容（重复内容用「详见第 N 章」）。

## 本章计划小节

1. CPU/heap/goroutine/mutex/block profile 的采样原理
2. 火焰图解读
3. execution trace 里 G 的状态机
4. `-benchmem` + `benchstat`
5. 微基准的五个陷阱
6. 线上 profile 的安全做法
7. **用它定位 Agent 的 token 流延迟与工具热点**

## 源码与框架坐标

- `net/http/pprof`
- `runtime/trace`

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
- 前置：第 27 章
- 下一章：第 29 章
