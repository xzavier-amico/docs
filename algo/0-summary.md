# 《算法深水区：LeetCode 高频 150 题 · Java / Go 双解》· 总纲

> **一句话**：不是「题解合集」，而是**把 150 道高频题拆成 40 个可迁移的思维模型**——每道题都要回答「为什么第一个念头是错的」「这个技巧为什么成立」「换个条件还成不成立」。
>
> **它与前三套的关系**：
> - [`go/`](../go/0-summary.md) 讲「**语言怎么运转**」（运行时、内存、并发）
> - [`llm/`](../llm/0-summary.md) 讲「**平台怎么活下来**」（架构与治理）
> - [`eng/`](../eng/0-summary.md) 讲「**功能怎么写不出错**」（实现与踩坑）
> - `algo/`（本套）讲「**问题怎么被想出来**」——它是前三套的**前置肌肉**：不会拆问题的人，读再多运行时源码也写不出调度器。

| 项 | 说明 |
| --- | --- |
| 读者 | ① 准备跳槽、被「中等题卡 40 分钟」折磨的后端；② 刷了 300 题但仍然「见过就会、换皮就废」的人；③ 想把算法直觉补回来的架构师 |
| 定位 | **思维模型手册**，不是题库搬运。每章必须讲清「无思路 → 有思路」的那一步是怎么跨过去的 |
| 规模 | 14 个部分 · **150 章（全部完稿）** · 247.6 万字符 / 2,509 代码块 / 1,548 行表格 · 另含 **≈400 道**同类扩展题（带链接 + 隐藏 hint） |
| 语言 | **Java 与 Go 双实现**，同一套变量名与流程，逐段对照；不使用任何第三方库（Go 仅用标准库 `container/heap`、`sort`） |
| 风格 | 先说人话，再说原理，最后给「能背走的模板 + 能证伪的边界」 |
| 不写什么 | ❌ 不写「显然」「易得」「略」；❌ 不写只贴代码的题解；❌ 不写「这题用 DP」然后就没了 |

---

## 0. 这本书的方法论：五步解题法

**刷题无效的根本原因**：大多数人是「看答案 → 记住 → 下次遇到同款会」。这条路的天花板是「见过的题」，而面试考的是「没见过的题」。

本书每章都强制走完下面五步，**缺一步就算没写**：

```
① 读题  把自然语言翻译成「输入域 + 输出域 + 约束量级」
         └─ 量级决定算法：n ≤ 20 → 状压/回溯；n ≤ 2·10³ → O(n²)；n ≤ 10⁵ → O(n log n)；n ≤ 10⁹ → O(log n) 或数学
② 建模  这道题本质是哪个「已解决问题」的变体
         └─ 找子结构 / 找单调性 / 找等价类 / 找图的顶点与边
③ 选型  在 3~5 个候选方案里做取舍（时间、空间、代码复杂度、可扩展性）
         └─ 每个方案都要给出「什么条件下它会输」
④ 写码  先写不变量（invariant），再写循环；双语言对照写
         └─ 边界：空输入 / 单元素 / 全相同 / 溢出 / 重复值
⑤ 验证  小样例手推 + 暴力对拍 + 复杂度复核
         └─ 「这个解法在什么输入上会错」——答不出来就是没懂
```

**贯穿全书的四把尺子**（遇到没见过的题，先问这四个问题）：

| 尺子 | 问法 | 对应章节 |
| --- | --- | --- |
| **单调性** | 答案随某个变量是单调的吗？能二分吗？ | 020–027、072–073 |
| **子结构** | 大问题的解能否由小问题的解拼出来？重叠子问题吗？ | 108–131 |
| **等价类** | 哪些输入是「同一种情况」？能不能只处理代表元？ | 036–043、132–139 |
| **状态机** | 能不能把过程压成「有限个状态 + 转移」？ | 055–064、108–131 |

> 💡 **一句话**：**算法题不是「想出一个妙招」，而是「识别出一个已经会了的模型」。** 本书 150 章，就是 150 次「识别训练」。

---

## 1. 每章统一模板（七件套，缺一不可）

每章文件 `NNN-slug.md` 必须包含以下结构，顺序固定：

````markdown
# NNN. LC<题号> <中文题名>（English Title）

> **状态**：`已完成（初稿）` ｜ **所属**：第 X 部分 · <主题> ｜ **编号**：NNN / 150
> **难度**：中等 ｜ **标签**：数组 · 双指针 · 排序
> **一句话**：这道题真正考的是 ______，不是 ______。
> **前置**：[`0NN-xxx.md`](0NN-xxx.md)（如果没有就写「无」，不许瞎指）

## NNN.1 题目

- 原题链接（**必须是** `https://leetcode.cn/problems/<slug>/`）
- 题干复述（不能照抄英文题干，要**用中文重写并补齐隐含条件**）
- 约束表（量级 → 反推算法）
- 示例 + **一个自己构造的反例**

## NNN.2 从暴力到最优：第一个念头为什么错

- 暴力解法（写出来，标复杂度）
- 它为什么不够 —— 用**具体数字**说话（"n=10⁵ 时 TLE 需要 10¹⁰ 次操作，约 10 秒"）
- 卡点在哪 —— 重复计算？无序？局部最优不是全局最优？
- **灵感的来源** —— 是观察到了什么不变量？

## NNN.3 原理拆解

### NNN.3.1 数据结构与算法选型
候选方案对比表：方案 / 时间 / 空间 / 代码量 / 什么时候会输

### NNN.3.2 为什么这个做法是对的（正确性论证）
不变量 + 归纳 / 交换论证 / 反证 / 界（bound）

### NNN.3.3 通用解题思路（可迁移的那一层）
抽象成「XX 模型」，并说明还有哪些题属于这个模型

### NNN.3.4 可扩展性（面试追问）
如果改成「有重复元素」「要求输出所有方案」「数据流输入」「多线程」……分别怎么办

## NNN.4 图解推演

- ASCII 状态演变图（必须能看出**每一步指针/栈/DP 表怎么动**）
- 关键步骤的表格推演（行=步骤，列=变量）
- 如果有两种解法，给一张「两条路线对比图」

## NNN.5 双语言实现

### NNN.5.1 Java
```java
// 带注释：边界怎么处理、为什么这里不用 >= / 为什么用 long
```

### NNN.5.2 Go
```go
// 同一套变量名与流程，标注与 Java 的写法差异（切片扩容/map/堆）
```

### NNN.5.3 双语言差异批注
| 点 | Java | Go | 影响 |
| --- | --- | --- | --- |

## NNN.6 复杂度与常见坑

- 时间/空间复杂度（要写**推导**，不是结论）
- 常见坑清单（≥5 条：边界、整数溢出、重复、死循环、Java 装箱、Go 切片共享……）
- 面试官最爱追问的 3 个问题 + 标准答法

## NNN.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC xxx 题名](链接) | 中等 | 一句话差异 |

<details>
<summary>💡 Hint（点击展开）</summary>

1~3 条提示，**只给方向和关键转化**，不给完整代码、不直接说答案。

</details>
````

### 隐藏 hint 的硬性格式

GitHub 渲染 `<details>` 为可折叠块，**必须**用下面这个精确写法（多一个空行都会导致渲染异常）：

```html
<details>
<summary>💡 Hint（点击展开）</summary>

第一行提示

第二行提示

</details>
```

**hint 的写作分级**（这是本书最容易被写废的地方）：

| 级别 | 允许写 | 禁止写 |
| --- | --- | --- |
| ✅ 好 hint | 「想想什么量在移动过程中是单调的」「注意答案与 `mid` 的单调关系」「先把二维降成一维」 | — |
| ❌ 坏 hint | 「用二分答案，check 函数是……」「维护一个 `int[] dp`，转移方程是……」 | 直接给解法 |

> **判定标准**：读者看完 hint 应该「哦！我知道该往哪想了」，而不是「哦，抄完了」。

---

## 2. 全书骨架：14 部分 · 150 章

```
┌────────────────────────────────────────────────────────────────────┐
│  第 1 部分  双指针与数组            001–010   (10)                  │
│  第 2 部分  前缀和 · 差分 · 滑窗     011–019   ( 9)                  │
│  第 3 部分  二分查找与二分答案       020–027   ( 8)                  │
│  第 4 部分  排序 · 区间 · TopK       028–035   ( 8)                  │
│  第 5 部分  哈希与数组原地变换       036–043   ( 8)                  │
│  第 6 部分  链表                     044–054   (11)                  │
│  第 7 部分  栈 · 队列 · 单调结构     055–064   (10)                  │
│  第 8 部分  树形结构                 065–081   (17)                  │
│  第 9 部分  图形结构                 082–095   (14)                  │
│  第 10 部分 回溯                     096–107   (12)                  │
│  第 11 部分 动态规划                 108–131   (24)                  │
│  第 12 部分 贪心                     132–139   ( 8)                  │
│  第 13 部分 位运算与数学             140–145   ( 6)                  │
│  第 14 部分 矩阵 · 字符串            146–150   ( 5)                  │
└────────────────────────────────────────────────────────────────────┘
```

**难度分布**：简单 30 / 中等 97 / 困难 23 —— 简单题只保留「双指针原地」「摩尔投票」「位运算」这类**有可迁移思想**的；纯 API 题（如 `LC 344` 反转字符串）一律不放正文，只出现在扩展里。

---

## 3. 完整题目总表（150 章）

> 文件命名规则：`NNN-slug.md`，`NNN` 与下表**严格一致**，`slug` 用 LeetCode 官方 slug。
> 「核心算法」列是**这道题真正训练的能力**，不是它挂的标签。

### 第 1 部分 · 双指针与数组（001–010）

| 编号 | LC | 题目 | 难度 | 主要结构 | 核心算法 |
| --- | --- | --- | --- | --- | --- |
| 001 | 1 | [两数之和](https://leetcode.cn/problems/two-sum/) | 简单 | 数组 · 哈希 | 哈希 / 排序双指针 · **兼全书方法论开篇** |
| 002 | 167 | [两数之和 II - 输入有序数组](https://leetcode.cn/problems/two-sum-ii-input-array-is-sorted/) | 中等 | 数组 | 对撞双指针 + 单调性 |
| 003 | 15 | [三数之和](https://leetcode.cn/problems/3sum/) | 中等 | 数组 | 排序 + 双指针 + 去重 |
| 004 | 18 | [四数之和](https://leetcode.cn/problems/4sum/) | 中等 | 数组 | 降维：n 数和 → 两数和 |
| 005 | 11 | [盛最多水的容器](https://leetcode.cn/problems/container-with-most-water/) | 中等 | 数组 | 对撞双指针 + 交换论证 |
| 006 | 42 | [接雨水](https://leetcode.cn/problems/trapping-rain-water/) | 困难 | 数组 · 栈 | 双指针 / 单调栈 / 前缀最值 三解 |
| 007 | 283 | [移动零](https://leetcode.cn/problems/move-zeroes/) | 简单 | 数组 | 快慢指针（原地压缩） |
| 008 | 88 | [合并两个有序数组](https://leetcode.cn/problems/merge-sorted-array/) | 简单 | 数组 | 逆向双指针（避免覆盖） |
| 009 | 75 | [颜色分类](https://leetcode.cn/problems/sort-colors/) | 中等 | 数组 | 三指针 · 荷兰国旗 |
| 010 | 26 | [删除有序数组中的重复项](https://leetcode.cn/problems/remove-duplicates-from-sorted-array/) | 简单 | 数组 | 快慢指针 + 不变量 |

### 第 2 部分 · 前缀和 · 差分 · 滑动窗口（011–019）

| 编号 | LC | 题目 | 难度 | 主要结构 | 核心算法 |
| --- | --- | --- | --- | --- | --- |
| 011 | 560 | [和为 K 的子数组](https://leetcode.cn/problems/subarray-sum-equals-k/) | 中等 | 数组 · 哈希 | 前缀和 + 哈希（负数也成立） |
| 012 | 238 | [除自身以外数组的乘积](https://leetcode.cn/problems/product-of-array-except-self/) | 中等 | 数组 | 左右前缀积（不用除法） |
| 013 | 303 | [区域和检索 - 数组不可变](https://leetcode.cn/problems/range-sum-query-immutable/) | 简单 | 数组 | 前缀和模板（附 2D 版 304） |
| 014 | 1109 | [航班预订统计](https://leetcode.cn/problems/corporate-flight-bookings/) | 中等 | 数组 | 差分数组 |
| 015 | 3 | [无重复字符的最长子串](https://leetcode.cn/problems/longest-substring-without-repeating-characters/) | 中等 | 字符串 · 哈希 | 变长滑窗 + 窗口内去重 |
| 016 | 76 | [最小覆盖子串](https://leetcode.cn/problems/minimum-window-substring/) | 困难 | 字符串 · 哈希 | 变长滑窗 + 计数覆盖 |
| 017 | 438 | [找到字符串中所有字母异位词](https://leetcode.cn/problems/find-all-anagrams-in-a-string/) | 中等 | 字符串 | 定长滑窗 + 词频 |
| 018 | 567 | [字符串的排列](https://leetcode.cn/problems/permutation-in-string/) | 中等 | 字符串 | 定长滑窗（与 438 同构） |
| 019 | 209 | [长度最小的子数组](https://leetcode.cn/problems/minimum-size-subarray-sum/) | 中等 | 数组 | 正数滑窗 + 单调性前提 |

### 第 3 部分 · 二分查找与二分答案（020–027）

| 编号 | LC | 题目 | 难度 | 主要结构 | 核心算法 |
| --- | --- | --- | --- | --- | --- |
| 020 | 704 | [二分查找](https://leetcode.cn/problems/binary-search/) | 简单 | 数组 | 二分模板（区间闭合性） |
| 021 | 34 | [在排序数组中查找元素的第一个和最后一个位置](https://leetcode.cn/problems/find-first-and-last-position-of-element-in-sorted-array/) | 中等 | 数组 | 左右边界二分 |
| 022 | 33 | [搜索旋转排序数组](https://leetcode.cn/problems/search-in-rotated-sorted-array/) | 中等 | 数组 | 二段有序性判定 |
| 023 | 153 | [寻找旋转排序数组中的最小值](https://leetcode.cn/problems/find-minimum-in-rotated-sorted-array/) | 中等 | 数组 | 与右端点比较 |
| 024 | 240 | [搜索二维矩阵 II](https://leetcode.cn/problems/search-a-2d-matrix-ii/) | 中等 | 矩阵 | 阶梯式搜索（右上角起点） |
| 025 | 875 | [爱吃香蕉的珂珂](https://leetcode.cn/problems/koko-eating-bananas/) | 中等 | 数组 | **二分答案**：可行性单调 |
| 026 | 410 | [分割数组的最大值](https://leetcode.cn/problems/split-array-largest-sum/) | 困难 | 数组 | 二分答案 + 贪心 check |
| 027 | 4 | [寻找两个正序数组的中位数](https://leetcode.cn/problems/median-of-two-sorted-arrays/) | 困难 | 数组 | 二分划分 + 边界条件 |

### 第 4 部分 · 排序 · 区间 · TopK（028–035）

| 编号 | LC | 题目 | 难度 | 主要结构 | 核心算法 |
| --- | --- | --- | --- | --- | --- |
| 028 | 912 | [排序数组](https://leetcode.cn/problems/sort-an-array/) | 中等 | 数组 | 快排三路 / 归并 / 堆排 三实现 |
| 029 | 56 | [合并区间](https://leetcode.cn/problems/merge-intervals/) | 中等 | 数组 | 按左端点排序 + 扫描 |
| 030 | 57 | [插入区间](https://leetcode.cn/problems/insert-interval/) | 中等 | 数组 | 三阶段线性扫描 |
| 031 | 215 | [数组中的第 K 个最大元素](https://leetcode.cn/problems/kth-largest-element-in-an-array/) | 中等 | 数组 · 堆 | 快速选择 / 小顶堆 |
| 032 | 347 | [前 K 个高频元素](https://leetcode.cn/problems/top-k-frequent-elements/) | 中等 | 哈希 · 堆 | 词频 + 堆 / 桶排序 |
| 033 | 295 | [数据流的中位数](https://leetcode.cn/problems/find-median-from-data-stream/) | 困难 | 堆 | 对顶堆（大顶+小顶） |
| 034 | 23 | [合并 K 个升序链表](https://leetcode.cn/problems/merge-k-sorted-lists/) | 困难 | 链表 · 堆 | 堆归并 / 分治归并 |
| 035 | 253 | [会议室 II](https://leetcode.cn/problems/meeting-rooms-ii/) | 中等 | 数组 · 堆 | 扫描线 / 小顶堆（Premium） |

### 第 5 部分 · 哈希与数组原地变换（036–043）

| 编号 | LC | 题目 | 难度 | 主要结构 | 核心算法 |
| --- | --- | --- | --- | --- | --- |
| 036 | 49 | [字母异位词分组](https://leetcode.cn/problems/group-anagrams/) | 中等 | 字符串 · 哈希 | 设计等价类 key |
| 037 | 128 | [最长连续序列](https://leetcode.cn/problems/longest-consecutive-sequence/) | 中等 | 哈希 | 只从序列起点扩展 |
| 038 | 41 | [缺失的第一个正数](https://leetcode.cn/problems/first-missing-positive/) | 困难 | 数组 | 原地哈希（下标当 key） |
| 039 | 287 | [寻找重复数](https://leetcode.cn/problems/find-the-duplicate-number/) | 中等 | 数组 | 环形链表 / 二分 / 二进制 |
| 040 | 448 | [找到所有数组中消失的数字](https://leetcode.cn/problems/find-all-numbers-disappeared-in-an-array/) | 简单 | 数组 | 负号标记 |
| 041 | 169 | [多数元素](https://leetcode.cn/problems/majority-element/) | 简单 | 数组 | 摩尔投票 |
| 042 | 229 | [多数元素 II](https://leetcode.cn/problems/majority-element-ii/) | 中等 | 数组 | 摩尔投票推广（k-1 候选） |
| 043 | 380 | [O(1) 时间插入、删除和获取随机元素](https://leetcode.cn/problems/insert-delete-getrandom-o1/) | 中等 | 数组 · 哈希 | 数组 + 下标映射 |

### 第 6 部分 · 链表（044–054）

| 编号 | LC | 题目 | 难度 | 主要结构 | 核心算法 |
| --- | --- | --- | --- | --- | --- |
| 044 | 206 | [反转链表](https://leetcode.cn/problems/reverse-linked-list/) | 简单 | 链表 | 三指针迭代 / 递归 |
| 045 | 92 | [反转链表 II](https://leetcode.cn/problems/reverse-linked-list-ii/) | 中等 | 链表 | 局部反转 + 虚拟头结点 |
| 046 | 25 | [K 个一组翻转链表](https://leetcode.cn/problems/reverse-nodes-in-k-group/) | 困难 | 链表 | 分组 + 组内反转 |
| 047 | 21 | [合并两个有序链表](https://leetcode.cn/problems/merge-two-sorted-lists/) | 简单 | 链表 | 归并 + 哑结点 |
| 048 | 2 | [两数相加](https://leetcode.cn/problems/add-two-numbers/) | 中等 | 链表 | 高精度加法（进位状态） |
| 049 | 19 | [删除链表的倒数第 N 个结点](https://leetcode.cn/problems/remove-nth-node-from-end-of-list/) | 中等 | 链表 | 双指针定距 |
| 050 | 142 | [环形链表 II](https://leetcode.cn/problems/linked-list-cycle-ii/) | 中等 | 链表 | Floyd 判圈 + 数学推导 |
| 051 | 143 | [重排链表](https://leetcode.cn/problems/reorder-list/) | 中等 | 链表 | 找中点 + 反转 + 合并 |
| 052 | 148 | [排序链表](https://leetcode.cn/problems/sort-list/) | 中等 | 链表 | 自底向上归并（O(1) 空间） |
| 053 | 138 | [随机链表的复制](https://leetcode.cn/problems/copy-list-with-random-pointer/) | 中等 | 链表 · 哈希 | 哈希映射 / 交错复制 |
| 054 | 146 | [LRU 缓存](https://leetcode.cn/problems/lru-cache/) | 中等 | 哈希 · 链表 | 哈希 + 双向链表 |

### 第 7 部分 · 栈 · 队列 · 单调结构（055–064）

| 编号 | LC | 题目 | 难度 | 主要结构 | 核心算法 |
| --- | --- | --- | --- | --- | --- |
| 055 | 20 | [有效的括号](https://leetcode.cn/problems/valid-parentheses/) | 简单 | 栈 | 栈匹配 + 不变量 |
| 056 | 32 | [最长有效括号](https://leetcode.cn/problems/longest-valid-parentheses/) | 困难 | 栈 · DP | 栈存下标 / 计数法 |
| 057 | 224 | [基本计算器](https://leetcode.cn/problems/basic-calculator/) | 困难 | 栈 | 符号入栈 + 括号展开 |
| 058 | 394 | [字符串解码](https://leetcode.cn/problems/decode-string/) | 中等 | 栈 | 双栈 / 递归下降 |
| 059 | 232 | [用栈实现队列](https://leetcode.cn/problems/implement-queue-using-stacks/) | 简单 | 栈 · 队列 | 均摊 O(1) 双栈 |
| 060 | 155 | [最小栈](https://leetcode.cn/problems/min-stack/) | 中等 | 栈 | 辅助栈 / 差值编码 |
| 061 | 739 | [每日温度](https://leetcode.cn/problems/daily-temperatures/) | 中等 | 栈 | 单调递减栈（下一个更大） |
| 062 | 84 | [柱状图中最大的矩形](https://leetcode.cn/problems/largest-rectangle-in-histogram/) | 困难 | 栈 | 单调递增栈 + 哨兵 |
| 063 | 85 | [最大矩形](https://leetcode.cn/problems/maximal-rectangle/) | 困难 | 矩阵 · 栈 | 逐行压缩 → 84 题 |
| 064 | 239 | [滑动窗口最大值](https://leetcode.cn/problems/sliding-window-maximum/) | 困难 | 队列 | 单调队列 |

### 第 8 部分 · 树形结构（065–081）

| 编号 | LC | 题目 | 难度 | 主要结构 | 核心算法 |
| --- | --- | --- | --- | --- | --- |
| 065 | 94 | [二叉树的中序遍历](https://leetcode.cn/problems/binary-tree-inorder-traversal/) | 简单 | 树 | 递归 / 显式栈 / **Morris O(1)** |
| 066 | 102 | [二叉树的层序遍历](https://leetcode.cn/problems/binary-tree-level-order-traversal/) | 中等 | 树 · 队列 | BFS 分层 |
| 067 | 199 | [二叉树的右视图](https://leetcode.cn/problems/binary-tree-right-side-view/) | 中等 | 树 · 队列 | BFS 层末元素 |
| 068 | 104 | [二叉树的最大深度](https://leetcode.cn/problems/maximum-depth-of-binary-tree/) | 简单 | 树 | 递归（后序）/ BFS |
| 069 | 226 | [翻转二叉树](https://leetcode.cn/problems/invert-binary-tree/) | 简单 | 树 | 前序交换 |
| 070 | 101 | [对称二叉树](https://leetcode.cn/problems/symmetric-tree/) | 简单 | 树 | 双指针镜像递归 |
| 071 | 543 | [二叉树的直径](https://leetcode.cn/problems/diameter-of-binary-tree/) | 简单 | 树 | 后序 + 全局最值 |
| 072 | 113 | [路径总和 II](https://leetcode.cn/problems/path-sum-ii/) | 中等 | 树 | 回溯 + 撤销选择 |
| 073 | 437 | [路径总和 III](https://leetcode.cn/problems/path-sum-iii/) | 中等 | 树 · 哈希 | **树上前缀和** |
| 074 | 124 | [二叉树中的最大路径和](https://leetcode.cn/problems/binary-tree-maximum-path-sum/) | 困难 | 树 | 树形 DP（贡献值） |
| 075 | 236 | [二叉树的最近公共祖先](https://leetcode.cn/problems/lowest-common-ancestor-of-a-binary-tree/) | 中等 | 树 | 后序回溯（附 BST 版 235） |
| 076 | 98 | [验证二叉搜索树](https://leetcode.cn/problems/validate-binary-search-tree/) | 中等 | BST | 上下界 / 中序单调 |
| 077 | 230 | [二叉搜索树中第 K 小的元素](https://leetcode.cn/problems/kth-smallest-element-in-a-bst/) | 中等 | BST | 中序 + 提前终止 |
| 078 | 105 | [从前序与中序遍历序列构造二叉树](https://leetcode.cn/problems/construct-binary-tree-from-preorder-and-inorder-traversal/) | 中等 | 树 · 哈希 | 分治 + 下标映射 |
| 079 | 297 | [二叉树的序列化与反序列化](https://leetcode.cn/problems/serialize-and-deserialize-binary-tree/) | 困难 | 树 | 前序遍历 + 空标记 |
| 080 | 337 | [打家劫舍 III](https://leetcode.cn/problems/house-robber-iii/) | 中等 | 树 | 树形 DP（选/不选） |
| 081 | 208 | [实现 Trie (前缀树)](https://leetcode.cn/problems/implement-trie-prefix-tree/) | 中等 | Trie | 字典树（附 212 词搜索 II） |

### 第 9 部分 · 图形结构（082–095）

| 编号 | LC | 题目 | 难度 | 主要结构 | 核心算法 |
| --- | --- | --- | --- | --- | --- |
| 082 | 200 | [岛屿数量](https://leetcode.cn/problems/number-of-islands/) | 中等 | 网格图 | DFS / BFS / 并查集 三解 |
| 083 | 695 | [岛屿的最大面积](https://leetcode.cn/problems/max-area-of-island/) | 中等 | 网格图 | 洪水填充 + 返回值 |
| 084 | 130 | [被围绕的区域](https://leetcode.cn/problems/surrounded-regions/) | 中等 | 网格图 | 反向思维（从边界灌） |
| 085 | 417 | [太平洋大西洋水流问题](https://leetcode.cn/problems/pacific-atlantic-water-flow/) | 中等 | 网格图 | 多源反向 DFS |
| 086 | 994 | [腐烂的橘子](https://leetcode.cn/problems/rotting-oranges/) | 中等 | 网格图 | 多源 BFS + 层计时 |
| 087 | 542 | [01 矩阵](https://leetcode.cn/problems/01-matrix/) | 中等 | 网格图 | 多源 BFS / 两遍 DP |
| 088 | 127 | [单词接龙](https://leetcode.cn/problems/word-ladder/) | 困难 | 隐式图 | BFS + 通配符建图 |
| 089 | 210 | [课程表 II](https://leetcode.cn/problems/course-schedule-ii/) | 中等 | DAG | 拓扑排序（BFS/DFS） |
| 090 | 684 | [冗余连接](https://leetcode.cn/problems/redundant-connection/) | 中等 | 图 · 并查集 | 并查集判环 |
| 091 | 743 | [网络延迟时间](https://leetcode.cn/problems/network-delay-time/) | 中等 | 图 | Dijkstra（堆优化） |
| 092 | 787 | [K 站中转内最便宜的航班](https://leetcode.cn/problems/cheapest-flights-within-k-stops/) | 中等 | 图 · DP | Bellman-Ford / 分层 DP |
| 093 | 1631 | [最小体力消耗路径](https://leetcode.cn/problems/path-with-minimum-effort/) | 中等 | 图 | 二分答案 + BFS / Dijkstra 变形 |
| 094 | 1584 | [连接所有点的最小费用](https://leetcode.cn/problems/min-cost-to-connect-all-points/) | 中等 | 图 | **最小生成树**（Prim/Kruskal） |
| 095 | 332 | [重新安排行程](https://leetcode.cn/problems/reconstruct-itinerary/) | 困难 | 图 | **欧拉路径**（Hierholzer） |

### 第 10 部分 · 回溯（096–107）

| 编号 | LC | 题目 | 难度 | 主要结构 | 核心算法 |
| --- | --- | --- | --- | --- | --- |
| 096 | 78 | [子集](https://leetcode.cn/problems/subsets/) | 中等 | 回溯 | 选/不选 二叉决策树 |
| 097 | 90 | [子集 II](https://leetcode.cn/problems/subsets-ii/) | 中等 | 回溯 | 排序 + 同层去重 |
| 098 | 77 | [组合](https://leetcode.cn/problems/combinations/) | 中等 | 回溯 | 起点递增 + 剪枝 |
| 099 | 39 | [组合总和](https://leetcode.cn/problems/combination-sum/) | 中等 | 回溯 | 可重复选（起点不 +1） |
| 100 | 40 | [组合总和 II](https://leetcode.cn/problems/combination-sum-ii/) | 中等 | 回溯 | 不可重复 + 同层去重 |
| 101 | 46 | [全排列](https://leetcode.cn/problems/permutations/) | 中等 | 回溯 | used 数组 |
| 102 | 47 | [全排列 II](https://leetcode.cn/problems/permutations-ii/) | 中等 | 回溯 | 去重排列 |
| 103 | 31 | [下一个排列](https://leetcode.cn/problems/next-permutation/) | 中等 | 数组 | 字典序构造（回溯的迭代版） |
| 104 | 51 | [N 皇后](https://leetcode.cn/problems/n-queens/) | 困难 | 回溯 | 三方向剪枝 |
| 105 | 131 | [分割回文串](https://leetcode.cn/problems/palindrome-partitioning/) | 中等 | 回溯 · DP | 切分树 + 回文预处理 |
| 106 | 22 | [括号生成](https://leetcode.cn/problems/generate-parentheses/) | 中等 | 回溯 | 合法性剪枝（左右计数） |
| 107 | 79 | [单词搜索](https://leetcode.cn/problems/word-search/) | 中等 | 回溯 · 网格 | 网格 DFS + 现场恢复 |

### 第 11 部分 · 动态规划（108–131）

| 编号 | LC | 题目 | 难度 | 主要结构 | 核心算法 |
| --- | --- | --- | --- | --- | --- |
| 108 | 70 | [爬楼梯](https://leetcode.cn/problems/climbing-stairs/) | 简单 | DP | 递推 → 滚动 → 矩阵快速幂 |
| 109 | 198 | [打家劫舍](https://leetcode.cn/problems/house-robber/) | 中等 | DP | 线性 DP（附 213 环形） |
| 110 | 53 | [最大子数组和](https://leetcode.cn/problems/maximum-subarray/) | 中等 | DP | Kadane + 分治解 |
| 111 | 152 | [乘积最大子数组](https://leetcode.cn/problems/maximum-product-subarray/) | 中等 | DP | 双状态（max/min） |
| 112 | 62 | [不同路径](https://leetcode.cn/problems/unique-paths/) | 中等 | DP | 网格 DP（附 63 障碍） |
| 113 | 64 | [最小路径和](https://leetcode.cn/problems/minimum-path-sum/) | 中等 | DP | 网格 DP + 空间压缩 |
| 114 | 416 | [分割等和子集](https://leetcode.cn/problems/partition-equal-subset-sum/) | 中等 | DP | **01 背包**（可行性） |
| 115 | 494 | [目标和](https://leetcode.cn/problems/target-sum/) | 中等 | DP | 01 背包（计数）+ 转化 |
| 116 | 322 | [零钱兑换](https://leetcode.cn/problems/coin-change/) | 中等 | DP | **完全背包**（最少个数） |
| 117 | 518 | [零钱兑换 II](https://leetcode.cn/problems/coin-change-ii/) | 中等 | DP | 完全背包（方案数）+ 循环顺序 |
| 118 | 279 | [完全平方数](https://leetcode.cn/problems/perfect-squares/) | 中等 | DP | 完全背包 / 四平方和定理 |
| 119 | 139 | [单词拆分](https://leetcode.cn/problems/word-break/) | 中等 | DP · 哈希 | 序列 DP + 字典 |
| 120 | 91 | [解码方法](https://leetcode.cn/problems/decode-ways/) | 中等 | DP | 双条件转移 + 前导零 |
| 121 | 300 | [最长递增子序列](https://leetcode.cn/problems/longest-increasing-subsequence/) | 中等 | DP | O(n²) DP / **贪心+二分 O(n log n)** |
| 122 | 354 | [俄罗斯套娃信封问题](https://leetcode.cn/problems/russian-doll-envelopes/) | 困难 | DP · 排序 | 排序 + LIS（降维陷阱） |
| 123 | 1143 | [最长公共子序列](https://leetcode.cn/problems/longest-common-subsequence/) | 中等 | DP | 双串 DP 模板 |
| 124 | 72 | [编辑距离](https://leetcode.cn/problems/edit-distance/) | 困难 | DP | 双串 DP + 三操作 |
| 125 | 115 | [不同的子序列](https://leetcode.cn/problems/distinct-subsequences/) | 困难 | DP | 匹配计数（不匹配则继承） |
| 126 | 10 | [正则表达式匹配](https://leetcode.cn/problems/regular-expression-matching/) | 困难 | DP | `*` 的零次/多次分支 |
| 127 | 121 | [买卖股票的最佳时机](https://leetcode.cn/problems/best-time-to-buy-and-sell-stock/) | 简单 | DP · 贪心 | 前缀最小值（附 122 无限制） |
| 128 | 123 | [买卖股票的最佳时机 III](https://leetcode.cn/problems/best-time-to-buy-and-sell-stock-iii/) | 困难 | DP | 状态机 DP（k 次交易） |
| 129 | 309 | [最佳买卖股票时机含冷冻期](https://leetcode.cn/problems/best-time-to-buy-and-sell-stock-with-cooldown/) | 中等 | DP | 三状态机 |
| 130 | 5 | [最长回文子串](https://leetcode.cn/problems/longest-palindromic-substring/) | 中等 | 字符串 · DP | 中心扩展 / 区间 DP / Manacher |
| 131 | 516 | [最长回文子序列](https://leetcode.cn/problems/longest-palindromic-subsequence/) | 中等 | DP | 区间 DP（附 312 戳气球） |

### 第 12 部分 · 贪心（132–139）

| 编号 | LC | 题目 | 难度 | 主要结构 | 核心算法 |
| --- | --- | --- | --- | --- | --- |
| 132 | 55 | [跳跃游戏](https://leetcode.cn/problems/jump-game/) | 中等 | 数组 | 最远可达边界 |
| 133 | 45 | [跳跃游戏 II](https://leetcode.cn/problems/jump-game-ii/) | 中等 | 数组 | 隐式 BFS 分层 |
| 134 | 134 | [加油站](https://leetcode.cn/problems/gas-station/) | 中等 | 数组 | 前缀和 + 唯一性证明 |
| 135 | 435 | [无重叠区间](https://leetcode.cn/problems/non-overlapping-intervals/) | 中等 | 区间 | 按右端点贪心（交换论证） |
| 136 | 452 | [用最少数量的箭引爆气球](https://leetcode.cn/problems/minimum-number-of-arrows-to-burst-balloons/) | 中等 | 区间 | 区间选点（与 435 同构） |
| 137 | 763 | [划分字母区间](https://leetcode.cn/problems/partition-labels/) | 中等 | 字符串 | 最远出现位置扫描 |
| 138 | 406 | [根据身高重建队列](https://leetcode.cn/problems/queue-reconstruction-by-height/) | 中等 | 数组 · 排序 | 高者先行 + 插入 |
| 139 | 621 | [任务调度器](https://leetcode.cn/problems/task-scheduler/) | 中等 | 哈希 · 数学 | 桶思想（构造法） |

### 第 13 部分 · 位运算与数学（140–145）

| 编号 | LC | 题目 | 难度 | 主要结构 | 核心算法 |
| --- | --- | --- | --- | --- | --- |
| 140 | 136 | [只出现一次的数字](https://leetcode.cn/problems/single-number/) | 简单 | 位运算 | 异或（附 137/260 系列） |
| 141 | 191 | [位 1 的个数](https://leetcode.cn/problems/number-of-1-bits/) | 简单 | 位运算 | `n & (n-1)` 消位 |
| 142 | 338 | [比特位计数](https://leetcode.cn/problems/counting-bits/) | 简单 | 位运算 · DP | `dp[i] = dp[i>>1] + (i&1)` |
| 143 | 268 | [丢失的数字](https://leetcode.cn/problems/missing-number/) | 简单 | 位运算 · 数学 | 异或 / 求和差 |
| 144 | 50 | [Pow(x, n)](https://leetcode.cn/problems/powx-n/) | 中等 | 数学 | 快速幂（含负指数/溢出） |
| 145 | 7 | [整数反转](https://leetcode.cn/problems/reverse-integer/) | 中等 | 数学 | 溢出预判（不能先算后判） |

### 第 14 部分 · 矩阵与字符串（146–150）

| 编号 | LC | 题目 | 难度 | 主要结构 | 核心算法 |
| --- | --- | --- | --- | --- | --- |
| 146 | 48 | [旋转图像](https://leetcode.cn/problems/rotate-image/) | 中等 | 矩阵 | 转置 + 反转（原地） |
| 147 | 54 | [螺旋矩阵](https://leetcode.cn/problems/spiral-matrix/) | 中等 | 矩阵 | 四边界收缩 |
| 148 | 73 | [矩阵置零](https://leetcode.cn/problems/set-matrix-zeroes/) | 中等 | 矩阵 | 首行首列当标记位 |
| 149 | 14 | [最长公共前缀](https://leetcode.cn/problems/longest-common-prefix/) | 简单 | 字符串 | 纵向扫描 / 分治 / 二分 |
| 150 | 28 | [找出字符串中第一个匹配项的下标](https://leetcode.cn/problems/find-the-index-of-the-first-occurrence-in-a-string/) | 简单 | 字符串 | **KMP**（next 数组 / Z 函数） |

---

## 4. 覆盖矩阵（自查用：每个格子都要有题）

### 4.1 数据结构 × 章号

| 数据结构 | 覆盖章节 | 题量 |
| --- | --- | --- |
| 数组 / 一维 | 001–014, 019, 026, 028–031, 038–042, 103, 132–138, 143 | 30+ |
| 矩阵 / 二维 | 024, 063, 082–087, 093, 112–113, 146–148 | 14 |
| 字符串 | 015–018, 036, 088, 105, 119–120, 123–126, 130, 137, 149–150 | 18 |
| 链表 | 034, 044–054 | 12 |
| 栈（含单调栈） | 006, 055–058, 060–063 | 9 |
| 队列（含单调队列、优先队列） | 031–035, 059, 064, 066–067, 091, 093 | 11 |
| 哈希表 | 001, 011, 036–037, 039, 043, 049, 053–054, 073, 078, 088, 116–119, 139 | 15 |
| 堆 | 031–035, 091, 093 | 8 |
| 二叉树 / BST | 065–080 | 16 |
| Trie | 081（含 212 扩展） | 1 |
| 并查集 | 082, 090, 094 | 3 |
| 图（邻接 / 网格 / DAG） | 082–095 | 14 |

### 4.2 算法思想 × 章号

| 算法思想 | 覆盖章节 |
| --- | --- |
| 双指针（对撞 / 快慢 / 三指针） | 002–010, 044–046, 049–051, 070, 103, 130 |
| 前缀和 / 差分 | 011–014, 073, 134 |
| 滑动窗口 | 015–019, 064 |
| 二分查找 | 020–024, 031（快速选择）, 121 |
| 二分答案 | 025–026, 093 |
| 排序（快 / 归 / 堆 / 计数） | 003, 028–030, 032, 052, 097, 100, 102, 122, 135–138 |
| 递归 / 分治 | 034, 052, 068–071, 078, 110, 130, 149 |
| 迭代（模拟 / 显式栈） | 044, 065, 079, 103, 147 |
| 回溯 | 072, 096–107 |
| 贪心 | 005, 121, 132–139 |
| 动态规划 | 108–131 |
| 单调栈 / 单调队列 | 006, 061–064 |
| BFS / DFS | 066–067, 082–088, 093 |
| 拓扑排序 | 089 |
| 最短路（Dijkstra / Bellman-Ford） | 091–093 |
| 最小生成树 | 094 |
| 欧拉路径 | 095 |
| 位运算 | 140–143 |
| 数学 / 快速幂 | 108, 118, 139, 144–145 |
| 摩尔投票 | 041–042 |
| 设计题（多结构组合） | 043, 054, 060, 081（LC 460 LFU 未收录，见 054 章扩展） |

> **自查规则**：上表任何一行如果「只有 1 个章节」且是困难题，说明这个思想讲得不够，需要在其它章的「可扩展性」里补。

---

## 5. 双语言实现规范

### 5.1 硬性要求

| 项 | Java | Go |
| --- | --- | --- |
| 类 / 函数签名 | `public class Solution { public int foo(int[] a) }` | `func foo(a []int) int` |
| 输出 | 直接 `return` | 直接 `return` |
| 禁止 | 第三方库、`System.exit`、读 stdin | 第三方库、`fmt.Scan` |
| 容器 | `ArrayDeque` > `Stack`；`HashMap` 指定泛型 | `[]T` + `map[K]V`；堆用 `container/heap` |
| 越界 | 显式判 `i < n` | 显式判 `i < len(a)` |
| 溢出 | `long` 累加、`Math.abs` 慎用（`Integer.MIN_VALUE`） | `int64` 累加 |

### 5.2 必须写进 `NNN.5.3` 的差异对照

每章至少对比下面 2~3 项中**与本题目相关的**：

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 切片 / 数组 | 定长，`length` | `[]T` 有 cap，`append` 可能重新分配 | 回溯里 `append` 后必须 `copy` 或 `[:len:len]` 截断 |
| 字符串 | `charAt` 返回 char | `s[i]` 是 byte，中文要 `[]rune` | 涉及字符操作时选对类型 |
| 哈希 | `HashMap` 装箱开销 | `map[int]int` 无装箱 | 大规模计数 Go 更快 |
| 堆 | `PriorityQueue` + `Comparator` | `heap.Interface` 需实现 5 个方法 | 堆题 Go 代码更长 |
| 排序 | `Arrays.sort` 对基本类型是双轴快排 | `sort.Ints` 是 pdqsort，不稳定 | 需要稳定时用 `sort.SliceStable` |
| 空值 | `null` | `nil`（map/slice 零值可用） | 边界判断写法不同 |

---

## 6. 写作红线（验收时逐条打勾）

1. ❌ **禁止**出现：`显然`、`易得`、`略`、`同理`（不带展开）、`TODO`、`待补`、`此处省略`
2. ❌ **禁止**只给一种解法就结束 —— 中等及以上难度**至少两种**（暴力 + 最优，或两种最优对比），并说明**取舍**
3. ❌ **禁止**贴代码不解释 —— 每段代码的注释必须解释「**为什么**」，不是「**做什么**」
4. ❌ **禁止**错误的 LeetCode 链接（必须是 `leetcode.cn/problems/<slug>/`，写完统一 curl 校验）
5. ✅ **必须**有 ASCII 图解（能看出状态怎么变），且图与正文的变量名一致
6. ✅ **必须**有「常见坑 ≥ 5 条」和「面试追问 ≥ 3 条」
7. ✅ **必须**有 ≥ 1 个 `<details>` 隐藏 hint，且符合 §1 的格式与分级
8. ✅ **必须**在文首标注前置章节，且前置章节必须存在
9. ✅ **必须**双语言代码都能在本地编译/运行（Go 用 `go vet`，Java 用 `javac`）

### 单章体量基线

| 难度 | 正文字符（去空白） | `##` 小节 | 表格 | 代码块 |
| --- | --- | --- | --- | --- |
| 简单 | ≥ 6,000 | ≥ 7 | ≥ 3 | ≥ 4（java/go/图示/对拍） |
| 中等 | ≥ 8,000 | ≥ 7 | ≥ 3 | ≥ 4 |
| 困难 | ≥ 10,000 | ≥ 8 | ≥ 4 | ≥ 5 |

> 低于基线 = 不算完成，打回重写。

---

## 7. 主题编排与章节验收

150 章按主题分组。同一组使用统一的术语、图示和正确性论证方法；跨组引用指向对应章节。

| 批次 | 部分 | 章号 | 章数 | 交付重点 |
| --- | --- | --- | --- | --- |
| A | 双指针与数组 | 001–010 | 10 | 建立「不变量」写法：每章必须写清循环不变量 |
| B | 前缀和 / 差分 / 滑窗 | 011–019 | 9 | 「窗口内维护什么」的统一语言 |
| C | 二分 | 020–027 | 8 | 二分三模板 + 二分答案判定函数设计 |
| D | 排序 / 区间 / TopK | 028–035 | 8 | 排序选择决策树 + 堆的两种用法 |
| E | 哈希与原地 | 036–043 | 8 | 「用下标当哈希」的通用技巧 |
| F | 链表 | 044–054 | 11 | 虚拟头结点 + 指针体操的安全规程 |
| G | 栈 / 队列 / 单调 | 055–064 | 10 | 单调栈「什么时候弹」的统一判定 |
| H | 树 | 065–081 | 17 | 递归三问（做什么/何时停/返回什么） |
| I | 图 | 082–095 | 14 | 建模：什么是顶点、什么是边、什么是状态 |
| J | 回溯 | 096–107 | 12 | 「决策树 + 剪枝 + 去重」三件套 |
| K | 动态规划（上） | 108–119 | 12 | 状态定义 + 转移 + 初始化 + 遍历序 |
| L | 动态规划（下） | 120–131 | 12 | 多维 DP 与空间压缩 |
| M | 贪心 + 位运算 | 132–145 | 14 | 贪心的「交换论证」与位运算恒等式 |
| N | 矩阵 / 字符串 | 146–150 | 5 | KMP 与原地矩阵的收口 |

每章独立成文件，格式见 [001. Two Sum](001-two-sum.md)。验收时按照 §1、§5、§6 检查结构、双语言实现和论证，再核对术语、交叉引用与清单状态。

---

## 8. 验收命令

```bash
bash check.sh algo          # 断链 / 占位符 / 体量与结构 / 状态一致性
bash algo/progress.sh       # 进度看板（按批次）
bash algo/progress.sh --all # 全量明细
```

**`check.sh algo` 的专属检查项**（已写进 `check.sh` 第 6 段）：

- 每个 `.md` 必须同时含 ```` ```java ```` 与 ```` ```go ```` 代码块
- 必须含 `leetcode.cn/problems/` 链接
- 必须含 `<details>` 与 `<summary>`
- 必须含 ASCII 图示（代码块内至少一处 `─`、`│`、`→`、`↓` 之一）

---

## 9. 阅读路线

| 路线 | 适合谁 | 章节 |
| --- | --- | --- |
| 🏃 面试急救（3 天） | 一周后面试 | 001, 002, 003, 005, 015, 020, 021, 044, 055, 065, 066, 096, 101, 108, 121, 127, 132, 140 |
| 🚶 体系通关（8 周） | 系统补基础 | 按 001 → 150 顺序，每周 2 个部分 |
| 🔬 思维模型（4 周） | 已刷 200+ 但不成体系 | 按 §4.2 逐行读：先读「双指针」所有章，再读「二分」所有章…… |
| 🎯 只看困难题（2 周） | 冲大厂 L5+ | 006, 016, 027, 033, 034, 038, 046, 056, 057, 062, 063, 064, 074, 079, 088, 095, 104, 124, 125, 126, 128, 122, 131 |

---

## 10. 里程碑

| 阶段 | 目标 | 验收 |
| --- | --- | --- |
| M0 | 大纲（本文件）+ 金标准样章 `001` | ✅ 已交付 |
| M1 | 第 1–5 部分（001–043，43 章） | ✅ 完成 |
| M2 | 第 6–9 部分（044–095，52 章） | ✅ 完成 |
| M3 | 第 10–11 部分（096–131，36 章） | ✅ 完成 |
| M4 | 第 12–14 部分（132–150，19 章） | ✅ 完成 |
| M5 | 收口：`algo/INDEX.md`、README/INDEX 接入、全量终检 | ✅ 清单已 sync，全仓 `check.sh` 通过 |

### 完稿实况（终检数据）

| 指标 | 数值 |
| --- | --- |
| 章数 | 150 / 150 |
| 正文字符（去空白） | 2,476,418 |
| 代码块 | 2,509（Java 330 / Go 332 / 图示与测试若干） |
| 表格行 | 1,548 |
| 隐藏 hint 块 | 150（每章 1 个） |
| 难度分布 | 简单 25 / 中等 102 / 困难 23 |
| 单章体量 | 最短 11,712 / 最长 26,287 / 平均 16,509 |

**终检口径**（可复现）：

```bash
bash check.sh algo          # 断链 0 / 占位符 0 / 偏薄 0 / algo 专项 0 问题
# Go   ：150/150 go vet 通过
# Java ：150/150 javac 通过（补 TreeNode/ListNode/Node 平台桩）
# 逻辑 ：137 章自带 go test 随机对拍全 PASS；
#        无自带测试的 13 章（028–034/082–087）用独立暴力 oracle 对拍全 PASS
```

---

## 附：章节清单（勾选即表示「已完成」）

> 这个清单是 `check.sh --sync` 的数据源，**格式必须严格是** `- [x] NNN slug`。

### 第 1 部分 · 双指针与数组
- [x] 001 two-sum
- [x] 002 two-sum-ii-input-array-is-sorted
- [x] 003 3sum
- [x] 004 4sum
- [x] 005 container-with-most-water
- [x] 006 trapping-rain-water
- [x] 007 move-zeroes
- [x] 008 merge-sorted-array
- [x] 009 sort-colors
- [x] 010 remove-duplicates-from-sorted-array

### 第 2 部分 · 前缀和 · 差分 · 滑动窗口
- [x] 011 subarray-sum-equals-k
- [x] 012 product-of-array-except-self
- [x] 013 range-sum-query-immutable
- [x] 014 corporate-flight-bookings
- [x] 015 longest-substring-without-repeating-characters
- [x] 016 minimum-window-substring
- [x] 017 find-all-anagrams-in-a-string
- [x] 018 permutation-in-string
- [x] 019 minimum-size-subarray-sum

### 第 3 部分 · 二分
- [x] 020 binary-search
- [x] 021 find-first-and-last-position-of-element-in-sorted-array
- [x] 022 search-in-rotated-sorted-array
- [x] 023 find-minimum-in-rotated-sorted-array
- [x] 024 search-a-2d-matrix-ii
- [x] 025 koko-eating-bananas
- [x] 026 split-array-largest-sum
- [x] 027 median-of-two-sorted-arrays

### 第 4 部分 · 排序 · 区间 · TopK
- [x] 028 sort-an-array
- [x] 029 merge-intervals
- [x] 030 insert-interval
- [x] 031 kth-largest-element-in-an-array
- [x] 032 top-k-frequent-elements
- [x] 033 find-median-from-data-stream
- [x] 034 merge-k-sorted-lists
- [x] 035 meeting-rooms-ii

### 第 5 部分 · 哈希与数组原地变换
- [x] 036 group-anagrams
- [x] 037 longest-consecutive-sequence
- [x] 038 first-missing-positive
- [x] 039 find-the-duplicate-number
- [x] 040 find-all-numbers-disappeared-in-an-array
- [x] 041 majority-element
- [x] 042 majority-element-ii
- [x] 043 insert-delete-getrandom-o1

### 第 6 部分 · 链表
- [x] 044 reverse-linked-list
- [x] 045 reverse-linked-list-ii
- [x] 046 reverse-nodes-in-k-group
- [x] 047 merge-two-sorted-lists
- [x] 048 add-two-numbers
- [x] 049 remove-nth-node-from-end-of-list
- [x] 050 linked-list-cycle-ii
- [x] 051 reorder-list
- [x] 052 sort-list
- [x] 053 copy-list-with-random-pointer
- [x] 054 lru-cache

### 第 7 部分 · 栈 · 队列 · 单调结构
- [x] 055 valid-parentheses
- [x] 056 longest-valid-parentheses
- [x] 057 basic-calculator
- [x] 058 decode-string
- [x] 059 implement-queue-using-stacks
- [x] 060 min-stack
- [x] 061 daily-temperatures
- [x] 062 largest-rectangle-in-histogram
- [x] 063 maximal-rectangle
- [x] 064 sliding-window-maximum

### 第 8 部分 · 树形结构
- [x] 065 binary-tree-inorder-traversal
- [x] 066 binary-tree-level-order-traversal
- [x] 067 binary-tree-right-side-view
- [x] 068 maximum-depth-of-binary-tree
- [x] 069 invert-binary-tree
- [x] 070 symmetric-tree
- [x] 071 diameter-of-binary-tree
- [x] 072 path-sum-ii
- [x] 073 path-sum-iii
- [x] 074 binary-tree-maximum-path-sum
- [x] 075 lowest-common-ancestor-of-a-binary-tree
- [x] 076 validate-binary-search-tree
- [x] 077 kth-smallest-element-in-a-bst
- [x] 078 construct-binary-tree-from-preorder-and-inorder-traversal
- [x] 079 serialize-and-deserialize-binary-tree
- [x] 080 house-robber-iii
- [x] 081 implement-trie-prefix-tree

### 第 9 部分 · 图形结构
- [x] 082 number-of-islands
- [x] 083 max-area-of-island
- [x] 084 surrounded-regions
- [x] 085 pacific-atlantic-water-flow
- [x] 086 rotting-oranges
- [x] 087 01-matrix
- [x] 088 word-ladder
- [x] 089 course-schedule-ii
- [x] 090 redundant-connection
- [x] 091 network-delay-time
- [x] 092 cheapest-flights-within-k-stops
- [x] 093 path-with-minimum-effort
- [x] 094 min-cost-to-connect-all-points
- [x] 095 reconstruct-itinerary

### 第 10 部分 · 回溯
- [x] 096 subsets
- [x] 097 subsets-ii
- [x] 098 combinations
- [x] 099 combination-sum
- [x] 100 combination-sum-ii
- [x] 101 permutations
- [x] 102 permutations-ii
- [x] 103 next-permutation
- [x] 104 n-queens
- [x] 105 palindrome-partitioning
- [x] 106 generate-parentheses
- [x] 107 word-search

### 第 11 部分 · 动态规划
- [x] 108 climbing-stairs
- [x] 109 house-robber
- [x] 110 maximum-subarray
- [x] 111 maximum-product-subarray
- [x] 112 unique-paths
- [x] 113 minimum-path-sum
- [x] 114 partition-equal-subset-sum
- [x] 115 target-sum
- [x] 116 coin-change
- [x] 117 coin-change-ii
- [x] 118 perfect-squares
- [x] 119 word-break
- [x] 120 decode-ways
- [x] 121 longest-increasing-subsequence
- [x] 122 russian-doll-envelopes
- [x] 123 longest-common-subsequence
- [x] 124 edit-distance
- [x] 125 distinct-subsequences
- [x] 126 regular-expression-matching
- [x] 127 best-time-to-buy-and-sell-stock
- [x] 128 best-time-to-buy-and-sell-stock-iii
- [x] 129 best-time-to-buy-and-sell-stock-with-cooldown
- [x] 130 longest-palindromic-substring
- [x] 131 longest-palindromic-subsequence

### 第 12 部分 · 贪心
- [x] 132 jump-game
- [x] 133 jump-game-ii
- [x] 134 gas-station
- [x] 135 non-overlapping-intervals
- [x] 136 minimum-number-of-arrows-to-burst-balloons
- [x] 137 partition-labels
- [x] 138 queue-reconstruction-by-height
- [x] 139 task-scheduler

### 第 13 部分 · 位运算与数学
- [x] 140 single-number
- [x] 141 number-of-1-bits
- [x] 142 counting-bits
- [x] 143 missing-number
- [x] 144 powx-n
- [x] 145 reverse-integer

### 第 14 部分 · 矩阵与字符串
- [x] 146 rotate-image
- [x] 147 spiral-matrix
- [x] 148 set-matrix-zeroes
- [x] 149 longest-common-prefix
- [x] 150 find-the-index-of-the-first-occurrence-in-a-string
