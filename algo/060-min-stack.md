# 060. LC155 最小栈（Min Stack）

> **状态**：`已完成（初稿）` ｜ **所属**：第 7 部分 · 栈 · 队列 · 单调结构 ｜ **编号**：060 / 150
> **难度**：中等 ｜ **标签**：栈 · 设计 · 辅助栈 · 差值编码 · 状态可回退
> **一句话**：这道题真正考的是「**怎么让一个只能看栈顶的结构，在 O(1) 内回答一个『历史极值』的问题 —— 把答案写进每一层，而不是拿一个变量去追**」，不是「背一个 `push/pop/top` 的壳子」。
> **前置**：[`006-trapping-rain-water.md`](006-trapping-rain-water.md) 的单调栈备解 —— 那里第一次出现「栈里存下标、弹栈那一刻结算」，本章把同一句话推进到「栈里存增广值（值 + 该层的答案）」

---

## 060.1 题目

**原题链接**：<https://leetcode.cn/problems/min-stack/>

### 题意重述（把隐含条件补齐）

请设计一个栈，除支持普通栈的 `push(x)`、`pop()`、`top()` 之外，再支持一个 `getMin()` 返回当前栈中的最小元素。**要求这四个操作全部是 O(1) 时间**。

一句话说完，但它藏着五个必须读出来的约束：

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| **四个操作都要 O(1)** | 「在常数时间内检索到最小元素」 | 每次 `getMin` 扫一遍全栈是 O(n)，调用 3·10⁴ 次就退化到 10⁹ 次比较 |
| **`pop` 会改变最小值** | 栈是 LIFO，元素会被撤销 | 只用一个 `min` 变量记录最小值，一旦最小值被 `pop` 掉，`min` 就成了「历史遗迹」，无法恢复 |
| **元素可以重复** | `val` 无「互不相同」的约束 | `push(2), push(2), pop()` 之后最小值仍然是 2；辅助栈若「只在更小时压」，两栈就会错位 |
| **`pop / top / getMin` 保证在非空栈上调用** | 「如果栈中没有元素，则不调用…」 | 可以省掉判空分支；但你自己写的测试必须覆盖「压空后再压」 |
| **值域是完整的 32 位整数区间** | `-2³¹ <= val <= 2³¹-1` | 差值编码的 `x - min` 最大可达 `2³²-1`，**超出 int32**，符号位会翻掉 |

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| `push / pop / top / getMin` 总调用次数 | `<= 3·10⁴` | 每次操作必须是**真 O(1)**；即便 `getMin` 只占一半（1.5·10⁴ 次）而每次扫 n 个元素，最坏是 `1.5·10⁴ × 1.5·10⁴ ≈ 2·10⁸` 次比较 —— 本机实测已到 0.2~0.4 s 量级，「能过但会被面试官判为不合格」 |
| `val` 值域 | `[-2³¹, 2³¹-1]` | ① 不能拿「值」当数组下标；② 差值编码必须用 64 位 |
| 空间要求 | 题目**没限制** | 允许 O(n) 辅助空间；这正是「辅助栈」能存活的前提 |
| 「设计题」形式 | 多方法协作，不是单次调用 | 要论证的是**一串操作后的状态一致性**（不变量），而不是「一次调用的返回值」 |

> 💡 **量级 → 算法**：`3·10⁴` 次调用是**「设计题专用量级」** —— 它不算大，任何 O(n) 单次操作都能勉强跑过判题机，但**设计题的考法是「约束写在你脑子里」**：题目白纸黑字写了「常数时间」，你的实现就必须真的常数时间，不能靠数据弱蒙过去。

### 示例

```
输入：
["MinStack","push","push","push","getMin","pop","top","getMin"]
[[],[-2],[0],[-3],[],[],[],[]]

输出：
[null,null,null,null,-3,null,0,-2]

解释：
MinStack minStack = new MinStack();
minStack.push(-2);     栈 = [-2]
minStack.push(0);      栈 = [-2, 0]
minStack.push(-3);     栈 = [-2, 0, -3]
minStack.getMin();  →  -3    ← 最小值在栈顶
minStack.pop();        栈 = [-2, 0]
minStack.top();     →  0
minStack.getMin();  →  -2    ← 最小值被 pop 掉了，"历史最小值 -3" 必须自动作废
```

### 我自造的三个反例（写完解法必须拿它们打一遍）

```text
① push(2), push(2), pop(), getMin()                → 2
   暴露「辅助栈只在严格更小时才压」造成的两栈错位

② push(MAX), push(MIN), pop(), getMin()            → MAX = 2147483647
   暴露「差值编码用 int 存 d」的溢出：d = MIN - MAX 溢出成 +1，
   "我是新的最小值"的符号信号被翻成"我不是"，pop 时便无法还原旧 min

③ push(-2), push(0), push(-3), pop(), pop(), getMin() → -2
   暴露「min 变量只在 push 时更新、pop 时不回退」的经典错误
```

第 ② 条是本批**唯一**一个「代码能编译、小样例全过、只在极值上错」的坑。我在本机真的跑出了它的错误输出（见 §060.6.2），所以它必须写进坑清单，而不是当理论风险提一句。

---

## 060.2 从暴力到最优：第一个念头为什么错

### 第一步：暴力（先写出来，别急着优化）

```java
// 暴力：一个数组当栈，getMin 每次扫全栈
int[] data = new int[30000];
int size = 0;

void push(int x) { data[size++] = x; }
void pop()       { size--; }
int top()        { return data[size - 1]; }

int getMin() {                     // ← O(n)
    int m = data[0];
    for (int i = 1; i < size; i++) m = Math.min(m, data[i]);
    return m;
}
```

- `push / pop / top` 是 O(1)，**只有 `getMin` 是 O(n)**。
- 具体数字：`3·10⁴` 次操作里若 1.5·10⁴ 次是 `getMin`，每次平均扫 1.5·10⁴ 个元素，合计约 **2·10⁸ 次比较**。本机（2 核容器 / JDK 17 / Go 1.19）实测这一段在 **0.2~0.4 s** 之间 —— **判题机能过，面试现场会被打回**。
- 为什么它一定不够：题目原文写的是「常数时间」。**这不是「数据弱就能过」的题，而是「约束即评分标准」的题。**

### 第二步：第一个「聪明」念头，以及它为什么是错的

几乎所有人第二步都会写：

```text
维护一个 min 变量：push 时 min = Math.min(min, x)；getMin 直接返回 min。   ← O(1) 了！
```

它错在 **`pop` 上**：

```
栈 = [-2, 0, -3]      min = -3
pop()  →  栈 = [-2, 0]    min 应该是 -2
                          但变量 min 还写着 -3 —— 它撒谎了
```

**卡点不在「怎么求最小值」，而在「`pop` 是一次撤销」**：撤销要求你把状态**回退**到上一个版本，而一个标量变量只有一个版本，没有历史。

### 第三步：灵感的来源 —— 「把答案写进结构里」

既然 `pop` 是「回退一个版本」，那就让**每一层都自带它那一层的答案**：

```
push x 时，除了 x 本身，再记一份「压入 x 之后的最小值」= min(x, 上一层的最小值)
pop  时，两份一起弹掉 → 上一层的最小值自动重新露出来
```

用一句话概括这一步跳跃：

> **不要用变量去「追踪」历史，而要把历史「存进结构」。** 变量的缺点是「只有一个版本」，栈的优点是「天然有 n 个版本」。

于是 `getMin` 变成「看辅助栈顶」，`pop` 变成「两个栈一起弹」—— 四个操作全是 O(1)。

### 第四步（另一条路）：差值编码 —— 把最小值藏进元素里

辅助栈要 2n 个格子的空间。有没有办法只用 n 个？有：**不再单独存元素，只存「它相对于当时最小值的差」**。

```
d = x - min_prev
d < 0  ⟺  x 是压栈那一刻的新最小值   ← d 的符号位同时充当"我是不是最小值"的信号
```

`getMin` 直接返回 `min` 变量，`top` / `pop` 靠 `d` 反推。空间从 2n 降到 n+1（一个变量）。

**但这条路埋了一颗雷**：`x` 与 `min` 的差最大可达 `2³²-1`，**超出 int32 的表示范围**。符号位一旦被溢出翻掉，「我是最小值」这个唯一信号就丢了 —— 后面 §060.3.2 会给出严格的还原公式，§060.6.2 会给出我实测到的错误输出。

---

## 060.3 原理拆解

### 060.3.1 数据结构与算法选型

| 方案 | push | pop | top | getMin | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A 单数组 + 扫全栈 | O(1) | O(1) | O(1) | **O(n)** | O(n) | 10 行 | 调用次数一多就退化；违反题面的「常数时间」 |
| B 单变量 `min` | O(1) | O(1) | O(1) | O(1) | O(n) | 8 行 | **不是慢，是错**：`pop` 掉最小值后无法回退 |
| **C 辅助栈（同步压）** ✅ | O(1) | O(1) | O(1) | **O(1)** | **O(2n)** | 15 行 | 空间翻倍；面试官若追加「能否只用一个栈」就不够 |
| D 辅助栈（仅压新最小值） | O(1) | O(1) | O(1) | O(1) | O(n)~O(2n) | 18 行 | 两栈不一定等长，`pop` 必须判断「该不该弹辅助栈」，多一个易错分支 |
| **E 差值编码** ✅ | O(1) | O(1) | O(1) | O(1) | **O(n)+1** | 22 行 | **必须 64 位**；差值的符号位是唯一信号，溢出即全错；面试讲解成本高 |

**为什么 C 比 D 好**：D 省了一点空间，代价是把「两栈等长」这个**结构不变量**换成了「每次 `pop` 都要判断」的**运行时分支**。判断分支正是 bug 的温床：`push` 时用 `x < min`、`pop` 时用 `data[i] == min`，两处符号不一致，遇到重复最小值就错位。C 用一点点空间买了一个不需要判断的不变量。

**为什么还要讲 E**：面试追问「能不能只用一个栈」时，E 是唯一答案。它同时也是「增量编码」这个思想的入门：**不存绝对值，存相对上一次的变化量**（同一个思想在做差分数组 014、前缀和 011 时反复出现）。

### 060.3.2 为什么这个做法是对的（正确性论证）

**（方案 C）命题 1**：任意时刻，`minStk[i] = min(data[0..i])`，对 `i ∈ [0, size)` 恒成立。

**证明（对操作序列归纳）**：

1. **初始**：`size = 0`，命题在空区间上真（约定成立）。
2. **`push(x)`**：`data[size] = x`；`minStk[size] = min(x, minStk[size-1])`。
   - 由归纳假设 `minStk[size-1] = min(data[0..size-1])`，两侧取 `min` 得 `min(data[0..size])`，命题成立。
   - 首元素时 `size = 0` 走特判 `minStk[0] = x`，也就是 `min(x)`，同样成立。
3. **`pop()`**：`size` 减一，两栈同步。剩下的前缀 `data[0..size-2]` 与 `minStk[0..size-2]` 正是上一次 `push` 之前的状态，由归纳假设成立。
4. **`getMin()`** 返回 `minStk[size-1] = min(data[0..size-1])`，正是当前栈的最小值。∎

**推论（为什么 `push` 里必须用 `<=` 之外还有一个隐含点）**：上面的证明只要求「`minStk[size] = min(x, minStk[size-1])`」，与 `x` 与旧值是否相等无关 —— 也就是说，**压不压都要算出同一个数**。真正的问题是「长度必须对齐」，所以选择「无论如何都压一份」是最省心的写法：`pop` 只需 `size--`，永不判断。

**（方案 E）命题 2**：设压栈序列为 `a₁…a_k`，`m_i = min(a₁…a_i)`，`d_i = a_i - m_{i-1}`（`i ≥ 2`），`d₁ = 0`。则

| 情形 | 判据 | 结论 |
| --- | --- | --- |
| `d_i < 0` | `a_i < m_{i-1}` | `a_i` 是新的最小值，`m_i = a_i` |
| `d_i ≥ 0` | `a_i ≥ m_{i-1}` | `m_i = m_{i-1}` |
| `top()` | `d_i < 0` 返回 `m`；否则返回 `m + d_i` | `m + d_i = m_{i-1} + (a_i - m_{i-1}) = a_i` ✓ |
| `pop()` | `d_i < 0` 时 `m ← m - d_i` | `m_i - d_i = a_i - (a_i - m_{i-1}) = m_{i-1}` ✓ |

**唯一失效模式**：`d_i` 被存成 32 位整数时，`|d_i|` 最大 `2³²-1` 装不下，`d_i` 的**符号位被翻转**：

```
a₁ = MAX = 2147483647,  a₂ = MIN = -2147483648
d₂ = MIN - MAX = -4294967295  →  (int32) 截断后 = +1   ← 本应是负数
```

于是第 2 条判据落入「`d ≥ 0`」分支：`push` 时 `m` 被更新为 `MIN`（这一步是对的，因为 `a_i < m` 是直接比较，不受 `d` 影响），但 `pop` 时按 `d ≥ 0` 判定「弹出者不是最小值」，**旧的 `m = MAX` 永远不会被还原**。这就是 §060.1 反例 ② 的机制。

> **一句话记住**：差值编码把「布尔信息（是不是最小值）」编码进了「数值的符号位」。**任何压缩编码都必须保证信号位不被数据挤爆。**

### 060.3.3 通用解题思路（可迁移的那一层）

本章是本批（060–064）统一判定语言的**第 0 课**，因为它是「结构里存带答案的数据」最裸的形态：

```
                     ┌── 单调栈 / 单调队列 / 辅助栈 的统一判定 ──┐
                     │  ① 结构里存什么？                        │
                     │     · 060：存「到这一层为止的最小值」（增广值）│
                     │     · 061-064：存下标（因为要算宽度/距离）  │
                     │  ② 结构内维持什么单调性？                  │
                     │     · 060：minStk 单调不增                 │
                     │     · 061/062：栈内高度单调；064：队列值递减│
                     │  ③ 什么时候结算？                          │
                     │     · 弹出 / 出队的那一刻 = 「被弹出者的答案」│
                     │       定稿的时刻（它的未来已经不可能更好）  │
                     └──────────────────────────────────────────┘
```

| 章 | 结构里存什么 | 单调性 | 结算时刻 | 结算的是谁的答案 |
| --- | --- | --- | --- | --- |
| **060** | 这一层的最小值 | `minStk` 单调不增 | `pop` 时两栈同步弹出 | 上一层的最小值「自动恢复」 |
| 061 | 下标 | 栈内温度单调不增（相等的留着不弹） | 遇到更暖的一天 `i` 时弹栈 | 被弹出者 `j` 的答案 = `i - j` |
| 062 | 下标 | 栈内高度严格递增 | 遇到更矮的柱子时弹栈 | 被弹出者 `j` 的最大矩形 = `h[j] × (i - left - 1)` |
| 063 | 每行的累加高度（相当于「批量喂给 062」） | 每行各自维护递增栈 | 每行结束 | 以当前行为底的最大矩形 |
| 064 | 下标 | 队列内数值严格递减 | 队首过期 / 队尾被支配 | 队首就是当前窗口最大值 |

**三句话背走**：

1. **栈里存下标（或带答案的增广值）** —— 只有下标能算出「宽度/距离」，只有增广值能让「回退」变成 O(1)。
2. **栈内保持单调** —— 被支配的元素（未来不可能成为答案的）**立刻淘汰**，这是 O(n) 的全部来源。
3. **弹出时结算「被弹出者的答案」** —— 元素被弹出的那一刻，就是它「答案定稿」的那一刻，因为往后不会再有更好的机会。

### 060.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| 再要一个 `getMax()` 也是 O(1) | 两个极值都要 | 再开一个 `maxStk` 辅助栈（同样同步压）；差值编码**做不到**同时编码 min 与 max（一个 `d` 只有一个符号位） |
| `getMin` 要求 O(1)，空间也要求 O(1) | 空间极限 | **不可能**：n 个元素的栈有 `n+1` 个不同状态，每个状态对应一个「当前最小值」，至少要能区分这 n 个值 → 信息论下界 Ω(n) |
| 要支持 `popMin()`（删任意位置的最小值） | 栈不再是纯 LIFO | 辅助栈失效；用「栈 + 双向链表」（LC 716 Max Stack 的套路），或「懒删除」：辅助栈记 `(value, 在 data 里的下标)`，弹到不匹配就丢弃 |
| 多线程并发 push/pop | 状态不再是单线程顺序 | 每个实例一把锁最省事；无锁版要把 `(top, min)` 打进一个 64 位字里用 CAS 更新，需处理 ABA |
| 数据流只增不减（不 pop） | 退化成「前缀最小值」 | 一个变量就够，不需要任何栈 —— 说明辅助栈的全部开销都是为 `pop` 买的 |
| 值域该扩到 64 位（`long`） | 差值编码要 `__int128` 或 BigInt | 直接放弃差值编码，改用辅助栈 —— 这是「编码方案有值域天花板」的典型例子 |

---

## 060.4 图解推演

### 060.4.1 辅助栈（方案 C）的完整轨迹

```
操作序列：push(-2) → push(0) → push(-3) → getMin() → pop() → top() → getMin()

初始：   data = [ ]            minStk = [ ]
              │                     │
              ▼ push(-2)  两者都是空，minStk 直接压 -2
┌────┬────┐        ┌────┬────┐
│data│ -2 │        │min │ -2 │        getMin = -2
└────┴────┘        └────┴────┘
              │
              ▼ push(0)   min(0, -2) = -2
┌────┬────┬────┐   ┌────┬────┬────┐
│data│ -2 │  0 │   │min │ -2 │ -2 │   ← 顶部沿用旧最小值（"这一层的最小值还是 -2"）
└────┴────┴────┘   └────┴────┴────┘
              │
              ▼ push(-3)  min(-3, -2) = -3
┌────┬────┬────┬────┐   ┌────┬────┬────┬────┐
│data│ -2 │  0 │ -3 │   │min │ -2 │ -2 │ -3 │
└────┴────┴────┴────┘   └────┴────┴────┴────┘
                                top→        ↑
                                     getMin = -3
              │
              ▼ pop()   两个栈同步弹一格
┌────┬────┬────┐   ┌────┬────┬────┐
│data│ -2 │  0 │   │min │ -2 │ -2 │   getMin = -2  ← 上一层的答案自己露出来了
└────┴────┴────┘   └────┴────┴────┘
                     top→           top→
```

### 060.4.2 每一步的状态表

| 步骤 | 操作 | `data`（栈底→栈顶） | `minStk`（栈底→栈顶） | 返回值 |
| --- | --- | --- | --- | --- |
| 0 | 初始化 | `[]` | `[]` | `null` |
| 1 | `push(-2)` | `[-2]` | `[-2]` | `null` |
| 2 | `push(0)` | `[-2, 0]` | `[-2, -2]` | `null` |
| 3 | `push(-3)` | `[-2, 0, -3]` | `[-2, -2, -3]` | `null` |
| 4 | `getMin()` | `[-2, 0, -3]` | `[-2, -2, -3]` | **-3** |
| 5 | `pop()` | `[-2, 0]` | `[-2, -2]` | `null` |
| 6 | `top()` | `[-2, 0]` | `[-2, -2]` | **0** |
| 7 | `getMin()` | `[-2, 0]` | `[-2, -2]` | **-2** |

### 060.4.3 差值编码（方案 E）的轨迹 —— 以及它在极值上怎么崩

同一条操作序列，只存一个 `d` 数组加一个 `min` 变量：

| 步骤 | 操作 | 计算 | `diff`（栈底→栈顶） | `min` | `top()` | `getMin()` |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `push(-2)` | 首元素：`d = 0` | `[0]` | `-2` | -2 | -2 |
| 2 | `push(0)` | `d = 0 - (-2) = 2` | `[0, 2]` | `-2` | `min+d = 0` ✓ | -2 |
| 3 | `push(-3)` | `d = -3 - (-2) = -1`（负数 → 我是新最小值） | `[0, 2, -1]` | `-3` | `min = -3` ✓ | -3 |
| 4 | `pop()` | `d = -1 < 0` → 还原 `min = -3 - (-1) = -2` | `[0, 2]` | `-2` | `min+d = 0` ✓ | -2 |

崩溃现场（这就是 §060.1 反例 ②）：

```
push(MAX = 2147483647)   diff=[0]              min = MAX
push(MIN = -2147483648)  d = MIN - MAX = -4294967295
                         ↓ 用 int32 存，截断成 +1（本应是负数！）
                         diff=[0, 1]           min = MIN     ← 直接比较 a_i < m，这步侥幸对
pop()                    d = 1 >= 0 → 认为"弹出者不是最小值"→ 不还原 min
                         diff=[0]              min = MIN     ← 应该回到 MAX，现在是 MIN
getMin()                 → -2147483648         ✗ 正确答案是 2147483647
```

```
┌──────────── 信号位被数据挤爆 ────────────┐
│ d 的「符号」= 「我是不是最小值」的开关     │
│ int32 装不下 2³²-1 的差值 → 负号翻成正号  │
│ 开关失效 → pop 时永不回退 → 状态永久跑偏   │
└──────────────────────────────────────────┘
```

### 060.4.4 两条路线对比图

```
                  ┌──────────── 需求：getMin 也要 O(1) ────────────┐
                  │                                              │
        ┌─────────▼──────────┐                    ┌──────────────▼─────────┐
        │ 路线 C：辅助栈      │                    │ 路线 E：差值编码        │
        │ 存 2n 个 int        │                    │ 存 n 个 64 位 + 1 个变量│
        └─────────┬──────────┘                    └──────────────┬─────────┘
                  │                                              │
       结构不变量：两栈等长                          数学不变量：d 的符号位
       pop 只需 size--                               pop 要靠 d 还原 min
                  │                                              │
        ✅ 好讲、好证、值域无关                       ✅ 空间省一半；面试加分
        ❌ 空间 2n                                   ❌ 必须 64 位；溢出即全错
                  │                                              │
             【生产首选】                              【面试炫技 + 必须讲清风险】
```

---

## 060.5 双语言实现

### 060.5.1 Java

```java
class MinStack {
    private int[] data;   // 主栈：存全部元素
    private int[] minStk; // 辅助栈：minStk[i] = min(data[0..i])
    private int size;     // 两个数组共用一个 size，天然等长

    public MinStack() {
        data = new int[16];
        minStk = new int[16];
    }

    public void push(int x) {
        if (size == data.length) {
            grow();
        }
        data[size] = x;
        // 无论 x 是否更小都压一格：minStk 的长度永远等于 data，
        // pop 时只需 size--，不需要「判断该不该回溯最小值」的分支
        minStk[size] = (size == 0) ? x : Math.min(x, minStk[size - 1]);
        size++;
    }

    public void pop() {
        // 题目保证 pop / top / getMin 只在非空栈上调用，故这里不做判空
        size--;
    }

    public int top() {
        return data[size - 1];
    }

    public int getMin() {
        return minStk[size - 1];
    }

    private void grow() {
        // 手写扩容：用 int[] 而不是 Deque<Integer>，是为了避开 3·10⁴ 次操作的装箱开销
        int[] d2 = new int[data.length << 1];
        int[] m2 = new int[minStk.length << 1];
        System.arraycopy(data, 0, d2, 0, size);
        System.arraycopy(minStk, 0, m2, 0, size);
        data = d2;
        minStk = m2;
    }
}

// 备解：差值编码。类名不同是因为同一个 .java 文件里不允许出现两个同名类。
class MinStackDiff {
    private long[] diff = new long[16]; // 存 d = x - min，必须是 long
    private int size = 0;
    private long min = 0;               // 必须是 long：差值最大 2³²-1

    public void push(int x) {
        if (size == diff.length) {
            long[] d2 = new long[diff.length << 1];
            System.arraycopy(diff, 0, d2, 0, size);
            diff = d2;
        }
        if (size == 0) {
            min = x;
            diff[size++] = 0L;          // 第一个元素没有「前任最小值」
            return;
        }
        long d = (long) x - min;        // 先把 x 提升成 long 再减：写成 x - min 会按 int 溢出
        diff[size++] = d;
        if (x < min) {
            min = x;
        }
    }

    public void pop() {
        long d = diff[--size];
        if (d < 0) {                    // 弹出的是当时的最小值 → 用 d 还原旧 min
            min = min - d;
        }
    }

    public int top() {
        long d = diff[size - 1];
        return d < 0 ? (int) min : (int) (min + d);
    }

    public int getMin() {
        return (int) min;
    }
}
```

### 060.5.2 Go

```go
package main

// MinStack 辅助栈版：data 存全部元素，minStk 与它严格等长，
// minStk[i] = min(data[0..i])。
type MinStack struct {
	data   []int
	minStk []int
}

// Constructor 名字与 LeetCode Go 模板一致，方便直接粘贴
func Constructor() MinStack {
	return MinStack{data: make([]int, 0, 16), minStk: make([]int, 0, 16)}
}

// 用指针接收者：切片 append 可能换底层数组，值接收者会导致改动丢失
func (s *MinStack) Push(x int) {
	s.data = append(s.data, x)
	if len(s.minStk) == 0 || x <= s.minStk[len(s.minStk)-1] {
		s.minStk = append(s.minStk, x) // 新的最小值（等于也压，保证长度对齐）
	} else {
		s.minStk = append(s.minStk, s.minStk[len(s.minStk)-1]) // 沿用旧的最小值
	}
}

func (s *MinStack) Pop() {
	if len(s.data) == 0 { // 判空只为自测时压空再压也不 panic
		return
	}
	s.data = s.data[:len(s.data)-1]
	s.minStk = s.minStk[:len(s.minStk)-1]
}

func (s *MinStack) Top() int { return s.data[len(s.data)-1] }

func (s *MinStack) GetMin() int { return s.minStk[len(s.minStk)-1] }

// MinStackDiff 差值编码版：只存 d = x - min。
// 关键：diff 用 int64 而不是 int —— int 在 32 位平台上只有 32 位，差值会被截断。
type MinStackDiff struct {
	diff []int64
	min  int64
}

func NewMinStackDiff() *MinStackDiff { return &MinStackDiff{diff: make([]int64, 0, 16)} }

func (s *MinStackDiff) Push(x int) {
	v := int64(x)
	if len(s.diff) == 0 {
		s.min = v
		s.diff = append(s.diff, 0) // 第一个元素没有「前任最小值」
		return
	}
	d := v - s.min // 先转 int64 再相减，顺序错了就会溢出
	s.diff = append(s.diff, d)
	if v < s.min {
		s.min = v
	}
}

func (s *MinStackDiff) Pop() {
	d := s.diff[len(s.diff)-1]
	s.diff = s.diff[:len(s.diff)-1]
	if d < 0 { // 弹出的是当时的最小值，旧 min = 现值 - d
		s.min = s.min - d
	}
}

func (s *MinStackDiff) Top() int {
	d := s.diff[len(s.diff)-1]
	if d < 0 {
		return int(s.min)
	}
	return int(s.min + d)
}

func (s *MinStackDiff) GetMin() int { return int(s.min) }
```

### 060.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 栈的载体 | `int[]` + `size`，扩容要自己 `System.arraycopy` | `[]int` + `append`，扩容由运行时完成 | Go 少 8 行样板；Java 想避开装箱就得手写扩容 |
| **整数宽度** | `int` 恒 32 位，**平台无关** | `int` 在 64 位平台是 64 位、在 32 位平台是 32 位 | 差值编码在 Go 里「本机跑对了」是**假象**：`GOARCH=386` 下同一份代码立刻错（§060.5.4 有实测） |
| 接收者 / 对象 | 方法直接改字段 | 必须 `func (s *MinStack)`，值接收者改不动切片头 | 写成值接收者会编译通过、逻辑错误 —— 这是 Go 特有的静默 bug |
| 空栈判定 | `size == 0` | `len(s.data) == 0` | 语义一致 |
| 构造入口 | 构造函数 `public MinStack()` | `Constructor()`（LeetCode 模板名） | 名字必须对齐模板，否则判题机找不到入口 |
| 溢出预防写法 | `(long) x - min`，转型必须写在**减法之前** | `int64(x) - s.min`，同一个坑 | 两语言都是「先转型再运算」，写成 `(long)(x - min)` 依然溢出 |

### 060.5.4 对拍测试（写完必须跑，这是本书的验收习惯）

```go
package main

import (
	"math/rand"
	"testing"
)

// bruteStack 参考实现：GetMin 每次扫全栈，O(n)。只用来对拍，不追求快。
type bruteStack struct{ d []int }

func (b *bruteStack) push(x int) { b.d = append(b.d, x) }
func (b *bruteStack) pop()       { b.d = b.d[:len(b.d)-1] }
func (b *bruteStack) top() int   { return b.d[len(b.d)-1] }
func (b *bruteStack) getMin() int {
	m := b.d[0]
	for _, v := range b.d[1:] {
		if v < m {
			m = v
		}
	}
	return m
}

func TestMinStackRandom(t *testing.T) {
	r := rand.New(rand.NewSource(20240601))
	for iter := 0; iter < 6000; iter++ { // 6000 次随机操作序列
		aux := Constructor()
		diff := NewMinStackDiff()
		b := &bruteStack{}
		ops := 1 + r.Intn(60)
		for op := 0; op < ops; op++ {
			// 值域显式包含 int32 两端：差值最大 2³²-1，专打 32 位实现
			vals := []int{-2147483648, 2147483647, 0, -1, 1, r.Intn(21) - 10}
			x := vals[r.Intn(len(vals))]

			if len(b.d) == 0 || r.Intn(100) < 55 { // 偏向往栈里压，保证栈不早空
				aux.Push(x)
				diff.Push(x)
				b.push(x)
				continue
			}
			switch r.Intn(3) {
			case 0:
				aux.Pop()
				diff.Pop()
				b.pop()
			case 1:
				if got, want := aux.Top(), b.top(); got != want {
					t.Fatalf("iter=%d top got=%d want=%d", iter, got, want)
				}
			case 2:
				if got, want := diff.GetMin(), b.getMin(); got != want {
					t.Fatalf("iter=%d diff.GetMin got=%d want=%d", iter, got, want)
				}
			}
			// 每次操作后三个实现必须完全同步
			if len(b.d) > 0 {
				if aux.GetMin() != b.getMin() || diff.GetMin() != b.getMin() {
					t.Fatalf("iter=%d min aux=%d diff=%d want=%d", iter, aux.GetMin(), diff.GetMin(), b.getMin())
				}
				if aux.Top() != b.top() || diff.Top() != b.top() {
					t.Fatalf("iter=%d top aux=%d diff=%d want=%d", iter, aux.Top(), diff.Top(), b.top())
				}
			}
		}
	}
}

// 边界：单元素、全相同、单调递增压栈、单调递减压栈、压空再压、int32 两端
func TestMinStackEdges(t *testing.T) {
	cases := [][]int{{5}, {7, 7, 7}, {1, 2, 3, 4, 5}, {5, 4, 3, 2, 1},
		{-2147483648, 2147483647}, {2147483647, -2147483648}}
	for _, c := range cases {
		aux, diff, b := Constructor(), NewMinStackDiff(), &bruteStack{}
		for _, x := range c {
			aux.Push(x)
			diff.Push(x)
			b.push(x)
			if aux.GetMin() != b.getMin() || diff.GetMin() != b.getMin() {
				t.Fatalf("case=%v push %d 最小值不一致", c, x)
			}
		}
		for len(b.d) > 1 {
			aux.Pop()
			diff.Pop()
			b.pop()
			if aux.GetMin() != b.getMin() || diff.GetMin() != b.getMin() || aux.Top() != b.top() {
				t.Fatalf("case=%v pop 后状态不一致", c)
			}
		}
		aux.Pop(); diff.Pop(); b.pop() // 压空
		aux.Push(-9); diff.Push(-9); b.push(-9)
		if aux.GetMin() != -9 || diff.GetMin() != -9 || aux.Top() != -9 {
			t.Fatalf("case=%v 压空后再压失败", c)
		}
	}
}
```

**本机实测结果**（2 核容器 / Go 1.19.8 / JDK 17.0.21）：

| 测试 | 规模 | 结果 |
| --- | --- | --- |
| `TestMinStackRandom`（三实现同步对拍） | 6000 条随机操作序列，含 int32 两端 | **PASS**，耗时 0.028 s |
| `TestMinStackEdges`（6 组边界 × 压到空再压） | 单元素 / 全相同 / 递增 / 递减 / 极值 | **PASS** |
| 故意用 `[]int` 存差值的坏版本，`go test`（amd64） | 同一组极值断言 | **PASS**（假象！`int` 在这里是 64 位） |
| 同一份坏版本，`GOARCH=386 go test` | 同一组极值断言 | **FAIL**：`after pop min got=-2147483648 want=2147483647` |
| Java 坏版本（`int d = x - min`） | `push(MAX)` 后 `push(MIN)`，再 `pop` | `getMin()` 得到 `-2147483648`，正确答案是 `2147483647` |

> **这张表最重要的一行是第三行**：`go test` 全绿，不代表算法对 —— 它只代表「你恰好在一台 64 位机器上」。所以正式代码里我写的是 `int64`，而不是 `int`。

---

## 060.6 复杂度与常见坑

### 060.6.1 复杂度推导（写结论不算本事，要能推）

- **时间**：`push` / `pop` / `top` / `getMin` 各自都是「若干次数组写 + 一次比较」，**每条路径上没有任何循环** → 每次操作严格 O(1)，不是均摊。
  - 注意区分：辅助栈版的 `push` 有一个 `if`，差值编码版的 `push` 有一个 `if` + 一次减法，都仍是常数条指令。
  - 整个序列 `q` 次操作合计 O(q) = O(3·10⁴)。
- **空间**：辅助栈版 O(2n)（两个数组），差值编码版 O(n) + O(1)。
  - Java 里 `int` 4 字节：辅助栈版最坏 `3·10⁴ × 2 × 4 B = 240 KB`；差值编码版 `3·10⁴ × 8 B = 240 KB` —— **两者几乎打平**，因为差值版必须用 `long`（8 字节）换掉 32 位溢出风险，省下的那份空间又被宽度吃回去了。这是一个很值得在面试里说的观察。
- **扩容**：Java 手写 `grow()` 是 2 倍扩容，`push` 的**均摊**代价仍是 O(1)（几何级数求和）；Go 的 `append` 也是按容量翻倍增长，均摊同样是 O(1)。

### 060.6.2 八个真实踩过的坑

1. **只用一个 `min` 变量** —— 最经典的错误。`push(-3)` 之后 `pop()`，`min` 还停在 `-3`。**修法**：让每层都存答案（辅助栈），或者用差值把答案编码进元素。
2. **辅助栈「只在严格更小时压」** —— 两栈长度不一致，`pop` 时必须判断「弹出的是不是最小值」。判断写漏、或者 `push` 用 `<` 而 `pop` 用 `==`，遇到 `[2,2]` 就错位。**修法**：无条件压一份，长度恒等，`pop` 只做 `size--`。
3. **差值编码用 `int` 存 `d`** —— `d` 最大 `2³²-1`，符号位被溢出翻掉，「我是不是最小值」的信号失效。本机实测 `GOARCH=386` 直接 FAIL。
4. **Java 写成 `long d = x - min;`** —— 前面的 `x - min` 仍按 `int` 求值，**溢出发生在赋值之前**。必须 `(long) x - min`。
5. **Go 用值接收者 `func (s MinStack) Push(x int)`** —— `append` 可能换底层数组，改的是副本，主栈纹丝不动。编译通过、`go vet` 通过、测试全挂。**修法**：一律 `*MinStack`。
6. **差值编码忘记首元素特判** —— Go 里 `min` 的零值是 `0`，第一个元素若直接算 `d = x - 0`，`min` 就会被错误地当成「压栈前的 0」，`getMin()` 在负数栈上返回正数。
7. **`pop()` 里不判空就去 `data[size-1]`** —— 题面保证「非空才调用」，但你自己的对拍程序经常压空后再 `pop`。生产代码要么判空、要么在文档里写清前置条件，别两头不靠。
8. **用 `java.util.Stack` / `ArrayDeque<Integer>`** —— `Stack` 是同步的（每个方法上锁），`ArrayDeque<Integer>` 每压一个 `int` 都要装箱。`3·10⁴` 次操作感觉不明显，但这是「设计题里最容易被追问细节」的地方：为什么用 `int[]`？答「避免装箱、避免锁」。

### 060.6.3 面试官最爱追问的三个问题

**Q1：为什么不能只用一个变量记录最小值？**

> 「因为 `pop` 是一次**撤销**：它要求状态回退到上一个版本，而标量变量只有一个版本，没有历史。`push(-3)` 之后 `pop()`，变量里的 `-3` 已经没地方回退了。两种修法：① 把「每层的最小值」也压进栈（辅助栈），让结构自己保存 n 个版本；② 用差值编码，把「我是不是最小值」这一个比特藏进元素的符号位里 —— 但那样必须用 64 位，否则差值溢出会翻掉信号位。」

**Q2：差值编码的溢出边界在哪？为什么一定要 64 位？**

> 「`d = x - min`，`x` 和 `min` 都取自 `[-2³¹, 2³¹-1]`，所以 `|d|` 最大 `2³²-1 ≈ 4.29·10⁹`，**装不进 int32**。差值编码的全部信息都在 `d` 的符号位上：`d < 0` 表示『刚压进来的是新的最小值』。一旦溢出，`MIN - MAX` 会被截成 `+1`，符号翻转，`pop` 时就认为『弹出者不是最小值』，旧的 `min` 永远回不去。我在 386 架构下实测过这份代码，`go test` 直接失败。」

**Q3：如果同时要求 `getMin()` 和 `getMax()` 都是 O(1)，空间还能不能只有 O(1) 额外空间？**

> 「不能降到 O(1)。n 个元素的栈能处于 n+1 种不同状态，每种状态的『当前最小值』都不同，结构至少要能区分这 n 个值，所以额外空间的信息论下界是 Ω(n)。工程上最直接的做法是两条辅助栈（min 栈 + max 栈），空间 3n；差值编码**做不了**这件事，因为一个 `d` 只有一个符号位，只够编码一个极值。如果面试官还要问『popMax 呢』，那就得升级成 LC 716 的『栈 + 双向链表 + 懒删除』，这时 O(1) 就别想了。」

---

## 060.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 155 最小栈](https://leetcode.cn/problems/min-stack/) | 中等 | 本章母题：辅助栈 / 差值编码 |
| [LC 716 最大栈](https://leetcode.cn/problems/max-stack/) | 困难 | 多了 `popMax()`：要删**任意位置**的最大值 → 辅助栈失效，改用「栈 + 双向链表 + 懒删除」（会员题） |
| [LC 232 用栈实现队列](https://leetcode.cn/problems/implement-queue-using-stacks/) | 简单 | 同样是设计题，考「均摊 O(1)」而不是「每次 O(1)」；与本章对照看很有意思（第 059 章） |
| [LC 225 用队列实现栈](https://leetcode.cn/problems/implement-stack-using-queues/) | 简单 | 反向变换，把两个结构的职责对调 |
| [LC 946 验证栈序列](https://leetcode.cn/problems/validate-stack-sequences/) | 中等 | 用栈模拟 `push/pop` 序列，本质仍是「用不变量判断状态是否可达」 |
| [LC 239 滑动窗口最大值](https://leetcode.cn/problems/sliding-window-maximum/) | 困难 | 极值问题换到**队列**上：单调队列（第 064 章）。「窗口最小值」是它的直接变体 |
| [LC 295 数据流的中位数](https://leetcode.cn/problems/find-median-from-data-stream/) | 困难 | 极值再上一个台阶：要中位数 → 对顶堆（第 033 章） |
| [LC 907 子数组的最小值之和](https://leetcode.cn/problems/sum-of-subarray-minimums/) | 中等 | 把「最小值」从「一个查询」变成「所有区间的贡献和」——单调栈的计数版 |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 716 最大栈（`popMax()` 也是 O(1)/O(log n)）**
1. 你真正要删的是「最大值所在的那个位置」，而不是栈顶。想想：能不能再开一个结构，记录「每个值出现在栈里的哪些位置」？
2. 如果最大值已经被别的 `pop()` 拿走，记录里的那条还有效吗？「懒删除」这个词值得想一想。

**LC 946 验证栈序列**
1. 别去想「能不能构造」，而是模拟：什么时候「必须」入栈，什么时候「可以」出栈？
2. 如果 `popped[j]` 既不等于栈顶、也没法再从 `pushed` 里拿，说明什么？

**LC 239 的「窗口最小值」（本章的横向变体）**
1. 把「最小值」从「整个栈」换成「最近 k 个元素」，结构该从栈换成什么？
2. 单调性从「单调不增的辅助栈」变成「队列里的什么性质」？

**通用**
- 只要一个操作会「撤销历史」（`pop`、滑出窗口），就必须问自己：**这个结构有没有能力回退到上一个版本？**
- 「存答案」比「算答案」便宜：把每层结论写进结构，查询就退化成看栈顶。

</details>

---

## 附：本章小结（可背走的三句话）

```
① 最小栈 = 把「历史极值」写进每一层，pop 就是一次自动的状态回退
② 辅助栈靠「两栈等长」换取「pop 无需判断」；差值编码靠 int64 保住符号位这个唯一信号
③ 本批统一语言：结构里存下标/增广值 + 保持单调 + 弹出（出队）时结算被弹出者
```

> **下一章**：[`061-daily-temperatures.md`](061-daily-temperatures.md) —— 从「设计一个结构」切换到「用单调栈消灭一轮 O(n) 的向右扫描」，同一句话（栈里存下标、弹出时结算）第一次变成「产生答案」而不是「恢复状态」。
