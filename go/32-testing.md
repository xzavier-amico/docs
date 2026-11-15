# 32. 测试：表驱动、mock、fuzz 与可重放

> **状态**：`待撰写` ｜ **所属**：第六部分 · 工程化 ｜ **编号**：32 / 50
> **一句话导读**：表驱动 + 子测试；`t.Cleanup/Helper/TempDir`；testify；接口 mock 与 gomock/mockery；`httptest` 与 Testcontainers-go；golden 文件；fuzz（1.18）；**LLM 依赖的录制回放测试（httptest + golden SSE）**；覆盖率与 CI 矩阵
> **本章写作骨架**：场景引子 → 直觉模型 → 底层实现（结构体/源码/图） → Java 对照 → 开源框架落地 → 踩坑清单 → 一页速记 + 自检题
> **风格要求**：轻松、口语化、类比多；只讲 Java 程序员会猜错的地方；不重复其它章节已讲内容（重复内容用「详见第 N 章」）。

## 本章计划小节

1. 表驱动 + 子测试
2. `t.Cleanup/Helper/TempDir`
3. testify
4. 接口 mock 与 gomock/mockery
5. `httptest` 与 Testcontainers-go
6. golden 文件
7. fuzz（1.18）
8. **LLM 依赖的录制回放测试（httptest + golden SSE）**
9. 覆盖率与 CI 矩阵

## 源码与框架坐标

- `testing`
- `httptest`
- testify

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
- 前置：第 31 章
- 下一章：第 33 章
