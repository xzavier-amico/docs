# 033. LC295 数据流的中位数（Find Median from Data Stream）

> **状态**：`已完成（初稿）` ｜ **所属**：第 4 部分 · 排序 · 区间 · TopK ｜ **编号**：033 / 150
> **难度**：困难 ｜ **标签**：堆 · 设计 · 数据流 · 对顶堆
> **一句话**：这道题真正考的是「**用两个堆把数据流切成两半，并靠一条大小约定让中位数永远浮在两个堆顶**」，不是「每次来了数就排序」。
> **前置**：[`032-top-k-frequent-elements.md`](032-top-k-frequent-elements.md)（堆的两条腿与 `container/heap` 的五方法）

---

## 033.1 题目

**原题链接**：<https://leetcode.cn/problems/find-median-from-data-stream/>

### 题意重述（把隐含条件补齐）

设计一个类 `MedianFinder`：

- `void addNum(int num)`：把整数 `num` 加入数据结构；
- `double findMedian()`：返回目前所有元素的中位数。

中位数的定义（题干给出）：如果元素个数是**奇数**，返回中间那个；如果是**偶数**，返回中间两个数的**平均值**。

六个必须读出来的约束：

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| **返回 `double`** | 函数签名 | 返回 `int` 会丢掉 `.5`；Java 里 `(a + b) / 2` 是整数除法 |
| **`addNum` 与 `findMedian` 可以交替任意多次** | 「实现 MedianFinder 类」 | 只在最后算一次是错的——每次 `findMedian` 都必须立即正确 |
| **`num` 可能为负** | `-10⁵ <= num <= 10⁵` | 用「值域计数 + 前缀和求中位」时要把负值偏移到正索引 |
| **最多 `5·10⁴` 次调用** | 约束 | 每次 `O(n)` 的插入排序实测 58.8 ms，能过；每次 `O(n log n)` 的重排实测已经 1.24 s（3.2 万次），会超时 |
| **`findMedian` 时至少有一个元素** | 题目的调用保证 | 不必处理空结构，但写库函数时应该返回 `NaN` 或抛异常 |
| **两个堆的「大小」与「值域」都要约定** | 这是算法核心 | 只维护大小不管值域 → 中位数错；只维护值域不管大小 → 中位数错 |

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| `-10⁵ <= num <= 10⁵` | 值域 2·10⁵，可以用计数数组 | 如果**只有 `addNum`、最后查一次**，计数 + 前缀和是 `O(V)`；但本题是**交替查询**，每次都要重算前缀和 → `O(V)` 每次，`V = 2·10⁵`，5 万次就是 10¹⁰ → 不可行 |
| 最多 `5·10⁴` 次调用 | 合计 | 每次操作必须做到 `O(log n)` 以内；`O(1)` 更好 |
| 奇数 / 偶数两种中位数定义 | 题干 | 需要一个「知道当前元素个数奇偶」的结构 —— 这正是「两个堆的大小差」 |
| 只问中位数，不问别的分位数 | 题干 | 中位数是**一个固定位置**（第 `n/2` 个），所以特别适合「用固定大小约定去卡位」 |

> 💡 **量级 → 算法**：数据流题的条件反射是「**每次操作 `O(log n)`**」。
> `O(1)` 的查询 + `O(log n)` 的插入 = 堆 / 平衡树；`O(log n)` 的插入 + `O(log n)` 的查询 = 有序集合（`TreeMap`）。
> **而「只要中位数」这个特殊需求，允许我们把「维护完整有序性」降级成「维护两半的分界线」，这就是对顶堆存在的理由。**

### 示例

```text
输入：
  ["MedianFinder", "addNum", "addNum", "findMedian", "addNum", "findMedian"]
  [[], [1], [2], [], [3], []]

输出：[null, null, null, 1.5, null, 2.0]

解释：
  addNum(1)  → 数据 {1}
  addNum(2)  → 数据 {1,2}   findMedian() = (1+2)/2 = 1.5
  addNum(3)  → 数据 {1,2,3} findMedian() = 2
```

### 我自造的六个反例

```text
① addNum(5), findMedian()                 → 单元素，必须返回 5.0（不是 5 也不是 2.5）
② addNum(1), addNum(1), findMedian()      → 重复值，返回 1.0
③ 依次加入 1,2,3,4,5, findMedian()        → 返回 3.0；再加 6 → 返回 3.5
④ 依次加入 5,4,3,2,1（递减）              → 每次 findMedian 都要对；打「只处理递增」的写法
⑤ addNum(MAX_VALUE), addNum(MAX_VALUE)    → 返回 2.147483647E9；
                                             写 (a+b)/2.0 会得到 -1.0（int 先溢出再转 double）
⑥ 1 万个相同值                            → 返回该值；打「用 == 判重」或「堆顶比较写反」
```

⑤ 是我自己踩过的坑：代码看起来是 `(low.peek() + high.peek()) / 2.0`，很多人以为「除以 2.0 就安全了」，**其实加法在 `int` 域里就已经溢出了**，`double` 只负责事后把错误结果转成浮点。

---

## 033.2 从暴力到最优：第一个念头为什么错

### 第一步：暴力 A（每次 `findMedian` 时排序）

```java
void addNum(int v) { list.add(v); }
double findMedian() {
    Collections.sort(list);          // O(n log n)
    int n = list.size();
    return n % 2 == 1 ? list.get(n / 2)
                      : (list.get(n / 2 - 1) + list.get(n / 2)) / 2.0;
}
```

- 每次查询 `O(n log n)`，`m` 次查询总代价 `O(m·n log n)`。
- 本机实测（Java 17 / 2 核容器，`addNum` 与 `findMedian` 交替调用）：

| 调用次数 | 每次重排 | 有序插入（二分 + `arraycopy`） | 对顶堆 |
| --- | --- | --- | --- |
| 4,000 | 77.751 ms | 3.887 ms | **1.002 ms** |
| 8,000 | 164.212 ms | 4.252 ms | **1.655 ms** |
| 16,000 | 354.013 ms | 6.914 ms | **3.376 ms** |
| 32,000 | **1,240.850 ms** | 24.930 ms | **17.777 ms** |
| 50,000（本题上限） | 按增长趋势已到 3 s 量级 | 58.800 ms | **26.131 ms** |

「每次重排」这一列的形状很清楚：`4000 → 8000 → 16000 → 32000` 分别是 `77.8 → 164.2 → 354.0 → 1240.9`，最后一步翻了 **3.5 倍**——因为它每次都要重排整个数组，总代价是 `Σ i log i ≈ (n²/2)·log n`。**在本题的 5 万次上限上，它必然超时。**

### 第二步：暴力 B（维护一个有序数组）

如果 `addNum` 时就用二分找到位置、把后面的元素整体右移（`System.arraycopy`），`findMedian` 就变成 `O(1)`：

- `addNum` 是 `O(n)`（搬移），但 `arraycopy` 是 `memmove` 级别的操作，实测 5 万次只要 **58.8 ms**。
- **它其实能过这题。** 而且思路干净、没有堆的大小约定。

那为什么还要对顶堆？两个理由：

1. **`O(n)` 的搬移在数据流场景下是真问题**。本题上限 5 万，若改成 `10⁷` 次调用，有序数组就是 `10¹⁴` 字节的搬移量（实测的线性增长撑不住），而对顶堆仍是 `O(log n)`。
2. **面试考的就是「用一个更小的结构换掉『维护完整有序性』」**。有序数组维护了「任意两个元素的相对顺序」——远超中位数的需要。

### 第三步：卡点在哪 —— 中位数只需要一条分界线

把中位数重新表述一次：

> 把数据分成两半：**较小的一半 `L`** 和 **较大的一半 `R`**，且 `|L|` 与 `|R|` 最多差 1。
> 中位数只由 **`L` 的最大值**和 **`R` 的最小值**决定。

```text
L = {较小的一半}                     R = {较大的一半}
        ┌──────────────────┐   │   ┌──────────────────┐
        │ 1  2  3  4        │   │   │ 5  6  7  8       │
        └──────────────────┘   │   └──────────────────┘
                            max(L)=4   min(R)=5
                            中位数 = (4 + 5) / 2 = 4.5
```

于是问题变成：**只需要能 `O(1)` 拿到 `max(L)` 和 `min(R)`，并在新元素到来时 `O(log n)` 地把它塞进正确的一半。**

- 要 `O(1)` 拿最大值 → **大顶堆**；
- 要 `O(1)` 拿最小值 → **小顶堆**；
- 要 `O(log n)` 插入 → 堆的 `push` 就是这个复杂度。

**两个堆顶一夹，中位数就在夹缝里。** 这就是「对顶堆」（也有人叫双堆、两个堆夹一个点）。

### 第四步：灵感的关键 —— 一条「大小约定」

只用两个堆还不够。如果不约定大小，`L` 有 7 个元素、`R` 有 2 个元素，「中间位置」是第 5 个，它落在 `L` 内部，两个堆顶就给不出来。

所以必须钉死一条**大小约定**：

```text
约定：|L| == |R|   或   |L| == |R| + 1
      （L 是较小的一半，多出来的那个元素永远放在 L）
```

有了它，中位数的公式就是确定的：

| 情况 | 元素总数 | 中位数 |
| --- | --- | --- |
| `|L| = |R| + 1` | 奇数 `2k+1` | `max(L)`，即 `L` 的堆顶 |
| `|L| = |R|` | 偶数 `2k` | `(max(L) + min(R)) / 2.0` |

> ⚠️ 为什么「多出来的放 `L`」而不是放 `R`？两种约定都行，但必须**全程一致**。选 `L` 的好处是：奇数时中位数直接是 `L` 的堆顶，不需要额外判断；选 `R` 则奇数时是 `R` 的堆顶。**写代码前先把这个约定写进注释，否则一半的 bug 都出在这里。**

---

## 033.3 原理拆解

### 033.3.1 数据结构与算法选型

| 方案 | addNum | findMedian | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- | --- |
| 每次查询时重排 | O(1) | **O(n log n)** | O(n) | 6 行 | 交替查询时总代价 `O(m·n log n)`，5 万次实测 3 s 量级 |
| 有序数组 + 二分插入 | **O(n)** 搬移 | O(1) | O(n) | 15 行 | 调用次数上到 `10⁶` 时搬移成为瓶颈 |
| **对顶堆（两个堆）** ✅ | **O(log n)** | **O(1)** | O(n) | 25 行 | 需要「任意分位数」时只能推广（§033.3.4）；需要**删除**元素时堆不支持 |
| 平衡二叉搜索树（`TreeMap` + 双指针） | O(log n) | O(log n) | O(n) | 50 行 | 标准库不提供「按排名取元素」的接口，要自己数子树大小 |
| 值域计数 + 前缀和 | O(1) | **O(V)** | O(V) | 20 行 | 每次查询都要重扫 `V` 个桶；`V = 2·10⁵`、5 万次 → 10¹⁰ |
| 跳表 / 有序多重集（C++ `multiset`） | O(log n) | O(log n) | O(n) | 30 行 | Go / Java 标准库都没有现成的有序多重集 |

**决策表**

| 需求 | 选它 | 理由 |
| --- | --- | --- |
| 只要**中位数**，插入与查询交替 | **对顶堆** | `O(log n)` 插入 + `O(1)` 查询，代码最短 |
| 要**任意分位数**（p50 / p90 / p99） | 对顶堆按比例扩展，或有序结构 | 把「大小约定」从 1:1 换成 `p : (1-p)` |
| 要**删除**元素（滑动窗口） | 有序多重集 / 懒删除的双堆 | 堆不支持删除任意元素（LC 480 的难点） |
| 内存放不下（亿级流） | 分桶近似 / t-digest | 精确中位数需要存全部数据，或至少存值域计数 |
| 值域很小且只查一次 | 计数 + 前缀和 | `O(n + V)`，最简单 |

### 033.3.2 为什么这个做法是对的（正确性论证）

**两条不变量**（对顶堆的全部正确性都挂在这两条上）：

> **I1（值域分离）**：`max(L) <= min(R)`。即 `L` 里每个元素都 `<=` `R` 里每个元素。
> **I2（大小约定）**：`|L| = |R|` 或 `|L| = |R| + 1`。

**初始**：两个堆都空，`I1` 空洞成立，`I2` 成立（`0 = 0`）。

**插入（变体一：按值归堆 + 两处平衡）**：

```text
1) 若 L 空 或 num <= max(L)：num 入 L
   否则：                      num 入 R
2) 若 |L| > |R| + 1：把 max(L) 移到 R      ← 修 I2 的上界
   若 |R| > |L|  ：把 min(R) 移到 L        ← 修 I2 的下界
```

- 第 1 步后 `I1` 仍成立：`num <= max(L)` 时它进 `L`，不影响「`L` 的最大值 ≤ `R` 的最小值」（`L` 内新增的元素更小，最大值不变或变小）；`num > max(L)` 时它进 `R`，而 `num > max(L)` 且 `R` 原来的元素都 `>= max(L)`，所以 `min(R)` 仍是 `min(num, 旧 min(R)) >= max(L)`。
- 第 2 步后 `I1` 仍成立：从 `L` 移出的是 `max(L)`，它 `<= min(R)`，进 `R` 后仍是 `R` 里最小的候选，而 `L` 的新最大值更小。从 `R` 移出的是 `min(R)`，它 `>= max(L)`，进 `L` 后成为 `L` 的最大值。
- 第 2 步后 `I2` 成立：插入后 `|L| - |R|` 只可能是 `-1、0、1、2` 四种值（原来差在 `{0,1}` 内，插入使差最多 +1 或 -1）。`2` 时搬一个到 `R` → 差 1；`-1` 时搬一个到 `L` → 差 0；其余不动。∎

**查询**：由 `I1`，`L` 里恰好是「最小的 `|L|` 个元素」，`R` 里是「最大的 `|R|` 个元素」；由 `I2`，`|L|` 与 `|R|` 恰好把总个数 `n` 分成两半（奇数时 `L` 多一个）。所以：

- `n` 为奇数 → 中位数是第 `(n+1)/2` 小的元素 = `L` 的最大值 = `max(L)`；
- `n` 为偶数 → 中位数是第 `n/2` 与第 `n/2+1` 小的平均 = `(max(L) + min(R)) / 2`。∎

**变体二（无分支写法，三步搞定）**

上面的「按值归堆」需要一次比较，还需要两处平衡。有一个更短、且**完全不需要比较值**的写法：

```text
1) 把 num 推入 L（大顶堆）
2) 把 max(L) 推入 R（小顶堆）      ← 保证「L 中最大的」交给了 R
3) 若 |L| < |R|，把 min(R) 推回 L  ← 只修这一边
```

它的正确性仍然靠 I1 + I2，但论证更漂亮：

- 设插入前 `I1`、`I2` 成立。第 1 步后 `|L|` 增 1。
- 第 2 步把 `max(L)` 交给 `R`。这个元素是 `L ∪ {num}` 里最大的，所以它 `>=` 剩下所有 `L` 的元素；同时 `R` 的新最小值是 `min(它, 旧 min(R))`，而旧 `min(R) >= 旧 max(L)`，且它 `>= 旧 max(L)`——两者都 `>=` 旧 `max(L)`，故 `I1` 成立。
- 第 3 步在 `|L| < |R|` 时把 `min(R)` 还给 `L`。此时 `|L| = |R| - 1`（因为第 2 步后 `|R|` 恰比 `|L|` 多 1），还一个之后 `|L| = |R|`，满足 `I2`；而 `min(R) >= max(L)`，还回去后成为新的 `max(L)`，`I1` 保持。∎

**变体二的好处**：没有 `if num <= max(L)` 这种「依赖取值」的分支，因此**不需要在 `L` 为空时特判**（`max(L)` 不会在被读之前用），也就消掉了「空堆 `peek()` 崩」这一类 bug。

### 033.3.3 通用解题思路（可迁移的那一层）

本题是 **「在线分位数 = 两半分界」** 这个模型的母题：

```
原始问题：数据流里随时求第 n/2 小的元素（顺序统计量）
          ↓ 只关心一个固定位置 → 不需要完整有序性
降级成：  把数据切成「较小的一半 L」和「较大的一半 R」
          ↓ 大小约定钉死分界线，两个堆顶夹住答案
实现：    L 用大顶堆（拿 max），R 用小顶堆（拿 min）
          ↓ 插入后修复被破坏的那条不变量
代价：    O(log n) 插入 / O(1) 查询 / O(n) 空间
```

同一模型下的题：

| 题 | 「分界」是第几个 | 额外难点 |
| --- | --- | --- |
| [LC 295 数据流的中位数](https://leetcode.cn/problems/find-median-from-data-stream/) | 第 `n/2` 个 | 大小约定 + 溢出 |
| [LC 480 滑动窗口中位数](https://leetcode.cn/problems/sliding-window-median/) | 第 `k/2` 个 | **要支持删除**，堆做不到 → 懒删除或有序多重集 |
| [LC 4 寻找两个正序数组的中位数](https://leetcode.cn/problems/median-of-two-sorted-arrays/) | 第 `(m+n)/2` 个 | 两个数组都已有序 → 二分划分（离线，`O(log min(m,n))`） |
| [LC 703 数据流中的第 K 大元素](https://leetcode.cn/problems/kth-largest-element-in-a-stream/) | 第 `n-k+1` 个 | 只要一边 → 单个小顶堆（见 `031`） |
| [LC 1825 求出 MK 平均值](https://leetcode.cn/problems/finding-mk-average/) | 三段分界 | 会员题；三个有序多重集 + 维护和 |
| [LC 2102 序列顺序查询](https://leetcode.cn/problems/sequentially-ordinal-rank-tracker/) | 按分数排名 | 双 `TreeMap`（大顶存高分段、小顶存低分段） |

**共同点**：**「只要某个固定排名」的题，都可以用「两半 + 大小约定」替代「完整有序集合」。**

### 033.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| 求**第 90 百分位** | 分界线不在中间 | 把大小约定改成 `|L| : |R| = 9 : 1`，其余逻辑一模一样 |
| **滑动窗口**内求中位数（LC 480） | 需要删除元素 | 堆不支持任意删除 → ①「懒删除」：延迟堆 + 哈希计数，弹出时校验；②改用有序多重集（Java `TreeMap<值, 计数>`） |
| 数据量 10⁹，内存放不下 | 存不下全部元素 | 分桶近似：按值域分 `B` 个桶存计数，维护总计数，定位到中位数所在桶后再细分；误差可控，`O(B)` 空间 |
| 多线程并发写入 | 两个堆不是线程安全的 | 加锁（粗粒度最简单）；或每线程一个 `MedianFinder` 再合并（合并的代价是 `O(k log k)`，会失真，需谨慎） |
| `num` 是 `long` / 大整数 | 加法更容易溢出 | Java 用 `BigDecimal.add().divide(2)`，Go 用 `big.Int`；或先转 `double` 再加（精度足够时） |
| 要求返回**精确**中位数且元素是浮点 | 浮点加法有误差 | 用 `Kahan` 求和补偿，或返回分数形式（分子分母分开算） |

---

## 033.4 图解推演

### 033.4.1 对顶堆的形态：一条竖线把数据切成两半

依次 `addNum(1), 2, 3, 4, 5`，看两个堆怎么「夹」住中位数：

```text
              大顶堆 L（较小的一半）        小顶堆 R（较大的一半）
              max(L) 在堆顶 ↓               ↓ min(R) 在堆顶
Step 1  加 1        [1]                    （空）                  中位数 = 1
Step 2  加 2        [1]                    [2]                    中位数 = (1+2)/2 = 1.5
Step 3  加 3        [2, 1]                 [3]                    中位数 = 2
Step 4  加 4        [2, 1]                 [3, 4]                 中位数 = (2+3)/2 = 2.5
Step 5  加 5        [3, 2, 1]              [4, 5]                 中位数 = 3

数轴视角：
  1  2  3  4  5
  └──┬──┘  └─┬─┘
    L       R
     ↑      ↑
  max(L)=3  min(R)=4        |L| = 3 = |R| + 1 → 奇数，取 max(L) = 3 ✓
```

### 033.4.2 插入后如何恢复平衡（四种情形，一一对应）

以「变体一（按值归堆）」为准，插入 `num` 之后 `|L| - |R|` 只可能落在 `{-1, 0, 1, 2}`：

```text
情形 ① num <= max(L)，|L| = |R|     →  入 L 后 |L| = |R| + 1   ✅ 已合法，不动
情形 ② num >  max(L)，|L| = |R|     →  入 R 后 |L| = |R| - 1   ❌ 搬 min(R) 回 L
情形 ③ num <= max(L)，|L| = |R| + 1 →  入 L 后 |L| = |R| + 2   ❌ 搬 max(L) 去 R
情形 ④ num >  max(L)，|L| = |R| + 1 →  入 R 后 |L| = |R|       ✅ 已合法，不动
```

用一张状态表把四种情形串起来（`L`/`R` 里写的是**堆顶**）：

| 步 | 插入前 `|L| / |R|` | num | 归到哪 | 插入后差值 | 修复动作 | 修复后 `|L| / |R|` | 中位数 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 0 / 0 | 1 | L | +1 | 无（情形 ①） | 1 / 0 | `max(L)=1` → **1.0** |
| 2 | 1 / 0 | 2 | R | 0 | 无（情形 ④） | 1 / 1 | `(1+2)/2` → **1.5** |
| 3 | 1 / 1 | 3 | R | -1 | `min(R)=2` 搬到 L（情形 ②） | 2 / 1 | `max(L)=2` → **2.0** |
| 4 | 2 / 1 | 4 | R | 0 | 无（情形 ④） | 2 / 2 | `(2+3)/2` → **2.5** |
| 5 | 2 / 2 | 5 | R | -1 | `min(R)=3` 搬到 L（情形 ②） | 3 / 2 | `max(L)=3` → **3.0** |

### 033.4.3 堆的内部调整（下沉 / 上浮）

以情形 ③（`|L| = |R| + 1` 且新元素进了 `L`）为例，`L` 从 2 个变 3 个再搬一个去 `R`：

```text
L（大顶堆）:  [7, 3]        加入 num=5（5 <= 7，进 L）
              7
             /
            3               → 追加到末尾再上浮
              [7, 3, 5]     5 比父 7 小，不上浮 → 堆顶仍 7

现在 |L| = 3, |R| = 1 → 差 2，超了 → 把 max(L) = 7 搬到 R
   L: pop 顶 → 把末尾 5 换到根 → 下沉
        [5, 3]          5 比子 3 大，不用换 → 堆顶 5
   R: push 7
        [7] 或与已有元素比较后上浮

结果 L = {3,5}(顶 5)，R = {7,…}(顶 7)，差回到 1，且 max(L)=5 <= min(R)=7 ✓
```

### 033.4.4 三条路线的对比

```text
                 ┌────────── addNum / findMedian 交替 ──────────┐
                 │                                              │
     ┌───────────▼────────────┐   ┌──────────────▼────────────┐   ┌────────▼─────────┐
     │ 每次重排                │   │ 有序数组 + 二分插入        │   │ 对顶堆            │
     │ add O(1), find O(nlogn) │   │ add O(n) 搬移, find O(1)   │   │ add O(logn),     │
     │ 5万次实测 ≈ 3 s         │   │ 5万次实测 58.8 ms          │   │ find O(1)        │
     └───────────┬────────────┘   └──────────────┬────────────┘   │ 5万次实测 26.1ms │
                 │                               │                └────────┬─────────┘
                 │                               │                         │
            【必超时】                    【能过，但 O(n) 搬移】      【面试要的答案】
```

---

## 033.5 双语言实现

### 033.5.1 Java

```java
import java.util.Comparator;
import java.util.PriorityQueue;

class MedianFinder {

    // 约定（写进注释，否则一半的 bug 出在这里）：
    //   low  = 较小的一半，大顶堆，堆顶是这一半的最大值
    //   high = 较大的一半，小顶堆，堆顶是这一半的最小值
    //   不变量 I1：low.peek() <= high.peek()
    //   不变量 I2：low.size() == high.size() 或 low.size() == high.size() + 1
    private final PriorityQueue<Integer> low = new PriorityQueue<>(Comparator.reverseOrder());
    private final PriorityQueue<Integer> high = new PriorityQueue<>();

    public MedianFinder() {
    }

    public void addNum(int num) {
        // 第 1 步：按值决定归属。low 为空时必须先进 low，否则 peek() 抛 NPE
        if (low.isEmpty() || num <= low.peek()) {
            low.offer(num);
        } else {
            high.offer(num);
        }

        // 第 2 步：恢复大小约定。两个分支最多命中一个，所以不用 while
        if (low.size() > high.size() + 1) {
            high.offer(low.poll());          // 情形 ③：low 多出 2 个，搬走最大值
        } else if (high.size() > low.size()) {
            low.offer(high.poll());          // 情形 ②：high 反超，搬回最小值
        }
    }

    public double findMedian() {
        if (low.size() > high.size()) {
            return low.peek();               // 奇数个：中位数是 low 的堆顶
        }
        // 两个 int 相加会先在 int 域溢出（MAX_VALUE 两次 → -1），
        // 所以必须先转 long 再相加，最后除 2.0
        return ((long) low.peek() + high.peek()) / 2.0;
    }
}
```

### 033.5.2 Go

```go
package main

import (
	"container/heap"
	"fmt"
	"math/rand"
	"sort"
)

// ===== 大顶堆（存较小的一半）=====
// Go 的 container/heap 需要 5 个方法：Len / Less / Swap / Push / Pop。
// 「大顶堆 / 小顶堆」的区别只在 Less 里：Less 返回 h[i] > h[j] 就是大顶堆。
type maxHeap []int

func (h maxHeap) Len() int            { return len(h) }
func (h maxHeap) Less(i, j int) bool  { return h[i] > h[j] } // 大顶堆：父 ≥ 子
func (h maxHeap) Swap(i, j int)       { h[i], h[j] = h[j], h[i] }
func (h *maxHeap) Push(x interface{}) { *h = append(*h, x.(int)) }
func (h *maxHeap) Pop() interface{} {
	old := *h
	n := len(old)
	v := old[n-1] // 取末尾：container/heap 会先把堆顶换到末尾再调 Pop
	*h = old[:n-1]
	return v
}

// ===== 小顶堆（存较大的一半）=====
type minHeap []int

func (h minHeap) Len() int            { return len(h) }
func (h minHeap) Less(i, j int) bool  { return h[i] < h[j] } // 小顶堆：父 ≤ 子
func (h minHeap) Swap(i, j int)       { h[i], h[j] = h[j], h[i] }
func (h *minHeap) Push(x interface{}) { *h = append(*h, x.(int)) }
func (h *minHeap) Pop() interface{} {
	old := *h
	n := len(old)
	v := old[n-1]
	*h = old[:n-1]
	return v
}

// ===== MedianFinder =====
type MedianFinder struct {
	low  maxHeap // 较小的一半，堆顶 = 这一半的最大值
	high minHeap // 较大的一半，堆顶 = 这一半的最小值
}

func Constructor() MedianFinder {
	return MedianFinder{low: make(maxHeap, 0), high: make(minHeap, 0)}
}

// AddNum 用的是「变体二（无分支写法）」：
// 先推 low → 把 low 的最大值交给 high → 若 high 反超再还一个回去。
// 三步都不需要读堆顶做比较，所以不必特判「low 为空」。
func (m *MedianFinder) AddNum(num int) {
	heap.Push(&m.low, num)
	heap.Push(&m.high, heap.Pop(&m.low))
	if m.low.Len() < m.high.Len() {
		heap.Push(&m.low, heap.Pop(&m.high))
	}
}

func (m *MedianFinder) FindMedian() float64 {
	if m.low.Len() > m.high.Len() {
		return float64(m.low[0]) // 奇数个：中位数是 low 的堆顶
	}
	// 先各自转 float64 再相加：Go 的显式转换让溢出无处藏身
	return (float64(m.low[0]) + float64(m.high[0])) / 2
}

func main() {
	// 与「每次排序取中位」对拍，同时校验两条不变量
	r := rand.New(rand.NewSource(295))
	for iter := 0; iter < 3000; iter++ {
		mf := Constructor()
		all := make([]int, 0, 60)
		for step := 0; step < 60; step++ {
			v := r.Intn(201) - 100 // 值域小 → 重复值密集
			mf.AddNum(v)
			all = append(all, v)

			s := append([]int(nil), all...)
			sort.Ints(s)
			var want float64
			if len(s)%2 == 1 {
				want = float64(s[len(s)/2])
			} else {
				want = (float64(s[len(s)/2-1]) + float64(s[len(s)/2])) / 2
			}
			if got := mf.FindMedian(); got != want {
				panic(fmt.Sprintf("got=%v want=%v all=%v", got, want, all))
			}
			// I2：大小约定
			if d := mf.low.Len() - mf.high.Len(); d < 0 || d > 1 {
				panic(fmt.Sprintf("大小约定被破坏 low=%d high=%d", mf.low.Len(), mf.high.Len()))
			}
			// I1：值域分离
			if mf.high.Len() > 0 && mf.low[0] > mf.high[0] {
				panic(fmt.Sprintf("值域顺序被破坏 lowTop=%d highTop=%d", mf.low[0], mf.high[0]))
			}
		}
	}
	fmt.Println("MedianFinder: 3000 轮 × 60 次插入，每次插入后都与排序结果比对，两条不变量全部成立")

	// 边界 1：单元素
	m1 := Constructor()
	m1.AddNum(5)
	fmt.Println("单元素 5 →", m1.FindMedian())
	// 边界 2：全相同
	m2 := Constructor()
	for i := 0; i < 10000; i++ {
		m2.AddNum(7)
	}
	fmt.Println("1 万个 7 →", m2.FindMedian())
	// 边界 3：大值相加（Go 显式转 float64，不会出现 Java 那种 int 溢出）
	m3 := Constructor()
	m3.AddNum(1 << 62)
	m3.AddNum(1 << 62)
	fmt.Println("2^62 + 2^62 的中位数 →", m3.FindMedian())
}
```

### 033.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 大顶堆的写法 | `new PriorityQueue<>(Comparator.reverseOrder())`，一行 | 要单独定义一个 `maxHeap` 类型，把 `Less` 写成 `>` | Go 代码多 12 行；**`Less` 写反就是「两个堆全搞错」**，症状是中位数完全不对 |
| 堆方法数量 | `PriorityQueue` 内置 5 个方法 | 必须手写 `Len/Less/Swap/Push/Pop`，每种堆都要写一遍 | 两个堆 = 两套 5 个方法 |
| 取堆顶 | `peek()`（空时返回 `null`） | `h[0]`（空时**直接 panic**） | Go 的「变体二」写法根本不需要在空堆上取顶；Java 的「变体一」必须判 `low.isEmpty()` |
| 取两数平均 | `((long) a + b) / 2.0`，**必须转 long** | `(float64(a) + float64(b)) / 2`，转换是显式的 | Java 不转 long 会溢出（实测 `MAX_VALUE` 两次得 `-1.0`）；Go 的显式转换让这个坑不存在 |
| 返回类型 | `double` | `float64` | 整数结果要返回 `2.0` 而不是 `2`（Go 里 `float64(x)` 已保证） |
| 切片的「删除」 | `poll()` 返回并移除 | `Pop()` 里 `*h = old[:n-1]` | Go 的 `Pop` 只缩长度、不清元素：对 `int` 无所谓，若存的是指针要手动置 `nil` 防内存泄漏 |

---

## 033.6 复杂度与常见坑

### 033.6.1 复杂度推导

- **`addNum` 是 `O(log n)`**：`heap.Push` 是「追加到末尾 + 上浮」，上浮层数 = 堆高度 = `⌊log₂n⌋`；`heap.Pop` 是「堆顶与末尾交换 + 下沉」，同样 `O(log n)`。每次 `addNum` 最多做 2 次 push + 1 次 pop（变体二）或 1 次 push + 1 次交换（变体一），仍是 `O(log n)`。
- **`findMedian` 是 `O(1)`**：只读两个堆顶。
- **空间 `O(n)`**：两个堆合起来存了全部 `n` 个元素。
- **为什么不可能更好**：查询已经是最优的 `O(1)`；插入的下界是 `Ω(log n)`——因为「在 `n` 个元素里定位一个排名」等价于比较模型下的查找下界（`log₂n` 次比较）。除非像 §033.3.4 那样放弃精确性（近似分位数）。

### 033.6.2 八个真实踩过的坑

1. **`(low.peek() + high.peek()) / 2.0`**。加法在 `int` 域先溢出。实测 `addNum(MAX_VALUE)` 两次后返回 `-1.0`。必须 `((long) a + b) / 2.0`。
2. **`/ 2` 而不是 `/ 2.0`**。偶数个元素时 `(3 + 4) / 2 = 3`，返回 `3.0` 而不是 `3.5`。
3. **大小约定不统一**。写完注释「`low` 可以多一个」之后，平衡代码却写成了「`high` 可以多一个」。症状：奇数个元素时中位数取了 `high` 的堆顶，答案偏大。
4. **只修一边**。只写 `if (low.size() > high.size() + 1) high.offer(low.poll());`，漏了 `else if (high.size() > low.size()) low.offer(high.poll());`。当新元素大于 `max(low)` 时会连续把 `high` 撑大，最终 `|high| > |low|`，中位数公式直接错。
5. **`low.peek()` 在空堆上调用**。`if (num <= low.peek())` 在第一次 `addNum` 时抛 `NullPointerException`（Java）或 panic（Go 的 `h[0]`）。修法：先判 `low.isEmpty()`，或直接用「变体二」。
6. **用 `==` 比较 `Integer`**。`if (low.peek() == num)` 在值超过 `[-128,127]` 时会因缓存失效而恒为 `false`。必须 `.equals()` 或先拆箱成 `int`。
7. **`PriorityQueue` 默认是小顶堆**。想要大顶堆必须传 `Comparator.reverseOrder()`。忘了传 → `low` 和 `high` 都是小顶堆 → `max(L)` 变成 `min(L)`，答案全错。
8. **以为「`addNum` 里用 `while` 更保险」**。`if` 足够（插入只会让差值变化 1），但写成 `while` 不会错，只是多一次判断。**真正会错的是「一个 `if` 里同时改两个堆」**——那样可能越修越乱。

### 033.6.3 面试官最爱追问的三个问题

**Q1：为什么用两个堆，而不是一个平衡二叉搜索树？**
> 「两个堆的优点是**代码短、常数小、只用标准库**。`TreeMap` 也能维护有序，但『按排名取第 k 个』需要每个节点维护子树大小（order-statistic tree），Java/Go 的标准库都没有这个接口，得自己实现，代码量翻 3 倍。反过来，堆的缺点是**不支持删除任意元素**——所以一旦题目变成『滑动窗口中位数』（LC 480），堆就得加『懒删除』机制，或者换回有序多重集。**判据是：只要中位数 + 只插入 → 堆；要删除 → 有序结构。**」

**Q2：如果要求第 90 百分位呢？改动大吗？**
> 「改动很小，只需要把大小约定从 `1 : 1` 换成 `9 : 1`：让 `|low| : |high| = 9 : 1`（差不超过 1），`findMedian` 换成从 `low` 里取第 `|low|` 大、即 `low` 的堆顶。**但要注意一个陷阱**：这样 `low` 里要取的是「第 90 百分位」而不是「`low` 的最大值」，所以 `low` 必须是**能取任意排名的结构**，堆不行了。所以准确的答案是：**中位数之所以能用堆，是因为它恰好是 `low` 的最大值**——这是 50% 分位数的特权。真要任意分位数，得用 order-statistic tree 或分桶。」

**Q3：10 亿个数、内存只有 1 GB，怎么求中位数？**
> 「精确解存不下，只能**近似**：① **分桶计数**——按值域把数据分成 `B` 个桶（比如先取一个样本估计范围，再等宽切分），流式扫一遍只存每个桶的计数，累加找到中位数所在的桶，再在该桶内细化（递归分层）；空间 `O(B)`，误差可控。② **t-digest / KLL sketch** 这类专门的流式分位数算法，能在 `O(log n)` 空间内把分位数误差控制在 1% 以内。③ 如果想要**精确**值：值域只有 `2·10⁵` 宽时（本题就是这样），直接用计数数组，`O(V)` 空间就够；值域宽时才只能近似。」

---

## 033.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 480 滑动窗口中位数](https://leetcode.cn/problems/sliding-window-median/) | 困难 | **要删除**元素 → 双堆 + 懒删除，或 `TreeMap<值,计数>` |
| [LC 4 寻找两个正序数组的中位数](https://leetcode.cn/problems/median-of-two-sorted-arrays/) | 困难 | 离线 + 两个数组有序 → 二分划分，`O(log min(m,n))` |
| [LC 703 数据流中的第 K 大元素](https://leetcode.cn/problems/kth-largest-element-in-a-stream/) | 简单 | 只要一边 → 单个小顶堆（见 `031`） |
| [LC 1825 求出 MK 平均值](https://leetcode.cn/problems/finding-mk-average/) | 困难 | 会员题；三段分界 + 三个有序多重集 |
| [LC 2102 序列顺序查询](https://leetcode.cn/problems/sequentially-ordinal-rank-tracker/) | 困难 | 双 `TreeMap` 夹排名，是「对顶堆」的有序版 |
| [LC 502 IPO](https://leetcode.cn/problems/ipo/) | 困难 | 双堆贪心：小顶堆按资本、大顶堆按利润 |
| [LC 1167 连接棒材的最低费用](https://leetcode.cn/problems/minimum-cost-to-connect-sticks/) | 中等 | 会员题；小顶堆每次取最小的两个合并（Huffman） |
| [LC 502 的姊妹题 LC 1383](https://leetcode.cn/problems/maximum-performance-of-a-team/) | 困难 | 排序 + 小顶堆维护前 k 大的效率 |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 480 滑动窗口中位数**
1. 双堆方案里，一个元素被移出窗口时，你怎么知道它在哪个堆？如果它不在堆顶，能不立刻删吗？
2. 「延迟删除」需要一份额外的计数表，什么时候才真正把元素从堆里弹掉？怎么判断堆顶是不是过期的？

**LC 4 寻找两个正序数组的中位数**
1. 两个数组都有序，中位数把合并后的数组切成等长的两半——这个「切」的位置能不能用二分找？
2. 判定一个切分是否合法的条件是什么？四个边界元素之间需要满足哪两个不等式？

**LC 1167 连接棒材的最低费用**
1. 每次合并都产生「新的长度」，这个新长度还要参与后续合并——这和 Huffman 建树是同一件事吗？
2. 为什么每次取「当前最短的两根」是安全的？（交换论证的方向：把最短的两根放到最深的位置）

**通用**
- 数据流题：先写清「每次操作允许的复杂度」，再选结构。
- 任何用两个堆夹一个位置的方案，都要把「大小约定」和「值域分离」写成两条不变量，并各写一行断言。

</details>

---

## 附：本章小结（可背走的三句话）

```text
① 中位数 = 「较小一半的最大值」与「较大一半的最小值」的夹缝 → 大顶堆 + 小顶堆，O(log n) 插入 / O(1) 查询
② 两条不变量缺一不可：I1 值域分离 max(low) <= min(high)，I2 大小约定 |low| = |high| 或 |low| = |high|+1
③ 平均值必须 ((long)a + b) / 2.0：int 先溢出再转 double，实测 MAX_VALUE 两次得 -1.0
```

> **下一章**：[`034-merge-k-sorted-lists.md`](034-merge-k-sorted-lists.md) —— 把「两个有序段合并」扩到 `k` 路：堆归并与分治归并的取舍，以及为什么 Java 里分治能快 2 倍。
