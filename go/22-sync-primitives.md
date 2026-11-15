# 22. sync 原语实现

> **状态**：`待撰写` ｜ **所属**：第四部分 · 并发 ｜ **编号**：22 / 50
> **一句话导读**：Mutex 内部：自旋、**饥饿模式（1ms 阈值）**、信号量唤醒；RWMutex 的读写计数与写者优先；WaitGroup、Once 快路径；**sync.Pool 的 private/shared/victim 三层与 GC 清理**；sync.Map 的 read/dirty/amended；`atomic` 泛型化（1.19）与 Go 的弱内存序
> **本章写作骨架**：场景引子 → 直觉模型 → 底层实现（结构体/源码/图） → Java 对照 → 开源框架落地 → 踩坑清单 → 一页速记 + 自检题
> **风格要求**：轻松、口语化、类比多；只讲 Java 程序员会猜错的地方；不重复其它章节已讲内容（重复内容用「详见第 N 章」）。

## 本章计划小节

1. Mutex 内部：自旋、**饥饿模式（1ms 阈值）**、信号量唤醒
2. RWMutex 的读写计数与写者优先
3. WaitGroup、Once 快路径
4. **sync.Pool 的 private/shared/victim 三层与 GC 清理**
5. sync.Map 的 read/dirty/amended
6. `atomic` 泛型化（1.19）与 Go 的弱内存序

## 源码与框架坐标

- `runtime/sync.go`
- `runtime/mutex.go`
- `runtime/sema.go`
- Ants
- fasthttp 池

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
- 前置：第 21 章
- 下一章：第 23 章
