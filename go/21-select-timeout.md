# 21. select 与超时控制

> **状态**：`待撰写` ｜ **所属**：第四部分 · 并发 ｜ **编号**：21 / 50
> **一句话导读**：select 实现（`pollorder`/`lockorder`、sudog 链）；随机公平性；`default` 非阻塞；**nil channel 永久阻塞（动态关闭分支的利器）**；`time.After` 泄漏陷阱 → `NewTimer/Stop`；循环 select 的退出范式
> **本章写作骨架**：场景引子 → 直觉模型 → 底层实现（结构体/源码/图） → Java 对照 → 开源框架落地 → 踩坑清单 → 一页速记 + 自检题
> **风格要求**：轻松、口语化、类比多；只讲 Java 程序员会猜错的地方；不重复其它章节已讲内容（重复内容用「详见第 N 章」）。

## 本章计划小节

1. select 实现（`pollorder`/`lockorder`、sudog 链）
2. 随机公平性
3. `default` 非阻塞
4. **nil channel 永久阻塞（动态关闭分支的利器）**
5. `time.After` 泄漏陷阱 → `NewTimer/Stop`
6. 循环 select 的退出范式

## 源码与框架坐标

- `runtime/select.go`
- net/http 超时链路

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
- 前置：第 20 章
- 下一章：第 22 章
