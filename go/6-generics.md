# 6. 泛型：约束、type set 与实现原理

> **状态**：`待撰写` ｜ **所属**：第二部分 · 语言内核 ｜ **编号**：6 / 50
> **一句话导读**：type parameter / constraint / `~T`；Go 用 **GC shape stenciling + dictionaries**，既不是 C++ 单态展开也不是 Java 擦除，代价与收益；泛型方法为何不支持；**什么时候不该用泛型**；SDK 中的 `Tool[T]`/`Result[T]`
> **本章写作骨架**：场景引子 → 直觉模型 → 底层实现（结构体/源码/图） → Java 对照 → 开源框架落地 → 踩坑清单 → 一页速记 + 自检题
> **风格要求**：轻松、口语化、类比多；只讲 Java 程序员会猜错的地方；不重复其它章节已讲内容（重复内容用「详见第 N 章」）。

## 本章计划小节

1. type parameter / constraint / `~T`
2. Go 用 **GC shape stenciling + dictionaries**，既不是 C++ 单态展开也不是 Java 擦除，代价与收益
3. 泛型方法为何不支持
4. **什么时候不该用泛型**
5. SDK 中的 `Tool[T]`/`Result[T]`

## 源码与框架坐标

- `cmd/compile/internal/types2`
- `runtime
- dictionary 实现
- go-sdk

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
- 前置：第 5 章
- 下一章：第 7 章
