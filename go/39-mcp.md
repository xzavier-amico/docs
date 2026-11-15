# 39. MCP：协议与 Go SDK 实战

> **状态**：`待撰写` ｜ **所属**：第七部分 · AI-Agent 应用架构 ｜ **编号**：39 / 50
> **一句话导读**：JSON-RPC 2.0 与 transport（stdio / streamable-http）；capabilities 协商；tools / resources / prompts / sampling / elicitation；写一个 MCP Server 暴露内部能力；写 Client 聚合多个 MCP；命名空间冲突；**工具描述注入攻击与权限边界**
> **本章写作骨架**：场景引子 → 直觉模型 → 底层实现（结构体/源码/图） → Java 对照 → 开源框架落地 → 踩坑清单 → 一页速记 + 自检题
> **风格要求**：轻松、口语化、类比多；只讲 Java 程序员会猜错的地方；不重复其它章节已讲内容（重复内容用「详见第 N 章」）。

## 本章计划小节

1. JSON-RPC 2.0 与 transport（stdio / streamable-http）
2. capabilities 协商
3. tools / resources / prompts / sampling / elicitation
4. 写一个 MCP Server 暴露内部能力
5. 写 Client 聚合多个 MCP
6. 命名空间冲突
7. **工具描述注入攻击与权限边界**

## 源码与框架坐标

- `modelcontextprotocol/go-sdk`
- `mark3labs/mcp-go`

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
- 前置：第 38 章
- 下一章：第 40 章
