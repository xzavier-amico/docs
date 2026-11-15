# 13. string / []byte / rune 与零拷贝

> **状态**：`待撰写` ｜ **所属**：第三部分 · 数据结构与内存 ｜ **编号**：13 / 50
> **一句话导读**：string 是不可变 header；UTF-8 与 rune；`len` vs 字符数；`strings.Builder`；**`unsafe.String` / `StringData` 零拷贝转换（1.20）**；四种拼接方式的基准对比；流式处理大 prompt
> **本章写作骨架**：场景引子 → 直觉模型 → 底层实现（结构体/源码/图） → Java 对照 → 开源框架落地 → 踩坑清单 → 一页速记 + 自检题
> **风格要求**：轻松、口语化、类比多；只讲 Java 程序员会猜错的地方；不重复其它章节已讲内容（重复内容用「详见第 N 章」）。

## 本章计划小节

1. string 是不可变 header
2. UTF-8 与 rune
3. `len` vs 字符数
4. `strings.Builder`
5. **`unsafe.String` / `StringData` 零拷贝转换（1.20）**
6. 四种拼接方式的基准对比
7. 流式处理大 prompt

## 源码与框架坐标

- `strings`
- `runtime/string.go`
- `bytes`

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
- 前置：第 12 章
- 下一章：第 14 章
