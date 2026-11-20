---
hide:
  - navigation
---

# AI Docs

> 技术文档仓库：**Go 语言深水区 + AI-Agent 架构 + 计算机系统基座**，面向有 Java 经验的程序员。
> 讲语言内核 → 运行时 → 并发调度器源码 → 工程化 → Agent 架构 → 开源框架源码；再从 bit 一路拆到 CPU、内核与协议栈，把「背过的定义」还原成物理约束的推论。

本站按系列展示正文，每套都从第 1 章开始。

## 七套文档

| 系列 | 内容 | 规模 | 开始阅读 |
| --- | --- | --- | --- |
| **Go 深水区** | 从 Java 到 Go：语言、运行时、GMP 调度器、内存与 GC、并发、工程化、Agent 基础 | 54 章 / 103 万字符 | [第 1 章 · 心智模型重置](go/1-mindset-shift.md) |
| **AgentOS 工程架构** | 从编排框架到生产级 Agent 平台：协议、事件、调度、上下文、RAG、安全、计费、可观测 | 52 章 / 104 万字符 | [第 1 章 · 从 Demo 到生产](llm/1-agent-vs-demo-gap.md) |
| **分布式工程实战** | 幂等、事务、限流熔断、异步消息、编排、存储、运维与运营；传统侧与 Agent 侧双修 | 52 章 / 88 万字符 | [第 1 章 · 幂等的精确定义](eng/1-idempotency-definition.md) |
| **算法深水区** | LeetCode 高频 150 题，Java + Go 双解；暴力→最优演进、正确性论证、ASCII 图解 | 150 章 / 247.6 万字符 | [第 1 题 · Two Sum](algo/001-two-sum.md) |
| **系统设计实战** | 从约束卡到真实系统：短链、IM、长连接、检索、推荐、迁移、秒杀、多租户 | 26 篇 / 71.2 万字符 | [第 1 篇 · 约束卡与容量](architect/01-constraint-card-and-capacity.md) |
| **存储与中间件剖析** | InnoDB、PostgreSQL、Redis、MongoDB、HBase、RocksDB/TiKV、ClickHouse、ES、Kafka、vLLM | 53 篇初稿 | [第 1 篇 · 组件解剖方法](comp/01-component-anatomy-method.md) |
| **计算机系统基座** | 从位与字节到 CPU/GPU、Linux 内核和网络协议；含 RingBuffer、GEMM、DNS 安全、TLS 与 HTTP/2 等机制与实例 | 56 篇初稿 / 11 篇实例 | [导读](base/01-one-ruler.md) · [收口与自检](base/56-closing-ledger.md) |

## 阅读方式

- **按系列顺序读**：从上面任意一套的第 1 章开始，侧栏会按章节顺序展开。
- **按主题跳读**：每章开头都有「本章定位」和「读法」，先看这两段再决定是否继续。
- **按岗位选路线**：`go/` 打语言与运行时地基，`llm/` 建平台架构，`eng/` 练功能实现，`algo/` 补算法直觉，`architect/` 学系统装配，`comp/` 拆组件内核，`base/` 补机器本身的物理底座（位、CPU、内核、协议栈）。

> 站点由 GitHub Actions 自动构建，每次 push 到 `main` 后自动更新。
