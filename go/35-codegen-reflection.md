# 35. 代码生成与反射：schema 自动化

> **状态**：`待撰写` ｜ **所属**：第六部分 · 工程化 ｜ **编号**：35 / 50
> **一句话导读**：`go:generate` 工作流；stringer/mockgen/wire/protobuf 生成物如何融入 CI；`reflect` 的能力边界与成本；tag 解析；**从 struct 自动生成 JSON Schema 喂给 LLM 工具**；`go/ast` 入门（自研生成器）；反射 vs 生成的取舍
> **本章写作骨架**：场景引子 → 直觉模型 → 底层实现（结构体/源码/图） → Java 对照 → 开源框架落地 → 踩坑清单 → 一页速记 + 自检题
> **风格要求**：轻松、口语化、类比多；只讲 Java 程序员会猜错的地方；不重复其它章节已讲内容（重复内容用「详见第 N 章」）。

## 本章计划小节

1. `go:generate` 工作流
2. stringer/mockgen/wire/protobuf 生成物如何融入 CI
3. `reflect` 的能力边界与成本
4. tag 解析
5. **从 struct 自动生成 JSON Schema 喂给 LLM 工具**
6. `go/ast` 入门（自研生成器）
7. 反射 vs 生成的取舍

## 源码与框架坐标

- `go/ast`
- `invopop/jsonschema`
- mockery

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
- 前置：第 34 章
- 下一章：第 36 章
