# 36. net/http 深潜：中间件、超时、SSE

> **状态**：`待撰写` ｜ **所属**：第六部分 · 工程化 ｜ **编号**：36 / 50
> **一句话导读**：Server 结构（accept → goroutine per conn → keep-alive）；Handler 即函数与中间件组合；超时四件套；优雅关停 `Shutdown`；Client/Transport 连接池（**`MaxIdleConnsPerHost=2` 经典坑**）；Body 必须关闭与连接复用；**SSE 流式（`Flush` / `ResponseController` / 反代缓冲）**；`ReverseProxy`；与 Tomcat 线程模型的对照
> **本章写作骨架**：场景引子 → 直觉模型 → 底层实现（结构体/源码/图） → Java 对照 → 开源框架落地 → 踩坑清单 → 一页速记 + 自检题
> **风格要求**：轻松、口语化、类比多；只讲 Java 程序员会猜错的地方；不重复其它章节已讲内容（重复内容用「详见第 N 章」）。

## 本章计划小节

1. Server 结构（accept → goroutine per conn → keep-alive）
2. Handler 即函数与中间件组合
3. 超时四件套
4. 优雅关停 `Shutdown`
5. Client/Transport 连接池（**`MaxIdleConnsPerHost=2` 经典坑**）
6. Body 必须关闭与连接复用
7. **SSE 流式（`Flush` / `ResponseController` / 反代缓冲）**
8. `ReverseProxy`
9. 与 Tomcat 线程模型的对照

## 源码与框架坐标

- `net/http`
- `httputil`
- Gin

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
- 前置：第 35 章
- 下一章：第 37 章
