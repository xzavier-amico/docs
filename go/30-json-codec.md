# 30. JSON 编解码：性能与协议设计

> **状态**：`待撰写` ｜ **所属**：第五部分 · 运行时与性能 ｜ **编号**：30 / 50
> **一句话导读**：`encoding/json` 的反射 + 字段缓存；tag 与 `omitempty` 的谎言；`RawMessage` 延迟解析；**`Decoder` 流式解码（JSONL / SSE）**；数字精度（`float64` vs `json.Number`）；**`encoding/json/v2`（1.25 实验）**；sonic/easyjson 的取舍；**LLM 结构化输出的严格解码（DisallowUnknownFields + schema 校验）**
> **本章写作骨架**：场景引子 → 直觉模型 → 底层实现（结构体/源码/图） → Java 对照 → 开源框架落地 → 踩坑清单 → 一页速记 + 自检题
> **风格要求**：轻松、口语化、类比多；只讲 Java 程序员会猜错的地方；不重复其它章节已讲内容（重复内容用「详见第 N 章」）。

## 本章计划小节

1. `encoding/json` 的反射 + 字段缓存
2. tag 与 `omitempty` 的谎言
3. `RawMessage` 延迟解析
4. **`Decoder` 流式解码（JSONL / SSE）**
5. 数字精度（`float64` vs `json.Number`）
6. **`encoding/json/v2`（1.25 实验）**
7. sonic/easyjson 的取舍
8. **LLM 结构化输出的严格解码（DisallowUnknownFields + schema 校验）**

## 源码与框架坐标

- `encoding/json`
- sonic
- jsonschema

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
- 前置：第 29 章
- 下一章：第 31 章
