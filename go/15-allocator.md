# 15. 内存分配器：mcache / mcentral / mheap

> **状态**：`待撰写` ｜ **所属**：第三部分 · 数据结构与内存 ｜ **编号**：15 / 50
> **一句话导读**：TCMalloc 思路三级缓存；size class；span 与 arena；tiny allocator；scavenger 与内存归还；`runtime.MemStats` 读法；与 JVM 分代/区域化的对照
> **本章写作骨架**：场景引子 → 直觉模型 → 底层实现（结构体/源码/图） → Java 对照 → 开源框架落地 → 踩坑清单 → 一页速记 + 自检题
> **风格要求**：轻松、口语化、类比多；只讲 Java 程序员会猜错的地方；不重复其它章节已讲内容（重复内容用「详见第 N 章」）。

## 本章计划小节

1. TCMalloc 思路三级缓存
2. size class
3. span 与 arena
4. tiny allocator
5. scavenger 与内存归还
6. `runtime.MemStats` 读法
7. 与 JVM 分代/区域化的对照

## 源码与框架坐标

- `runtime/malloc.go`
- `runtime/mheap.go`

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
- 前置：第 14 章
- 下一章：第 16 章
