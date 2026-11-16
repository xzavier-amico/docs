# 034. LC23 合并 K 个升序链表（Merge k Sorted Lists）

> **状态**：`已完成（初稿）` ｜ **所属**：第 4 部分 · 排序 · 区间 · TopK ｜ **编号**：034 / 150
> **难度**：困难 ｜ **标签**：链表 · 堆 · 分治 · k 路归并
> **一句话**：这道题真正考的是「**把 `k` 路「找最小」从线性扫描换成堆，或者把 `k` 路摊成 `log k` 层两两归并**——两条路的复杂度都是 `O(N log k)`，但常数差一倍以上」。
> **前置**：[`033-find-median-from-data-stream.md`](033-find-median-from-data-stream.md)（堆的全部细节）、[`028-sort-an-array.md`](028-sort-an-array.md)（归并排序）

---

## 034.1 题目

**原题链接**：<https://leetcode.cn/problems/merge-k-sorted-lists/>

### 题意重述（把隐含条件补齐）

给你一个链表数组 `lists`，其中每个链表都已经按**升序**排列。请把所有链表合并成一个升序链表，返回合并后的头结点。

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| **`lists` 可能为空数组** | 约束 `0 <= lists.length` | 不判空 → 访问 `lists[0]` 越界 |
| **单个链表可能为空** | 约束 `0 <= lists[i].length` | 建堆时把 `null` 放进去 → 比较时 `NullPointerException` / Go 的 nil 解引用 |
| **所有链表可能都是空的** | 上面两条的叠加 | 必须返回 `null`，不能返回 `new ListNode(0)` |
| **按升序，且要稳定** | 「升序」+ 链表语义 | 比较写 `<` 而不是 `<=` 时，值相同的节点顺序会被打乱 |
| **规模瓶颈是「总节点数」不是 `k`** | `0 <= 总节点数 <= 10⁴`、`k <= 10⁴` | `k` 可以远大于总节点数（大量空链表）→ 建堆时要把空链表过滤掉，否则白初始化 `k` 个槽 |
| **结点值是 `-10⁴ ~ 10⁴`** | 约束 | 可以用计数排序（值域 2·10⁴），但链表题考的是归并 |

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| `k <= 10⁴` | 最多 1 万个链表 | 「顺序两两合并」是 `O(k·N)` 最坏：`k = N = 10⁴` 时本机实测 **124.8 ms**（对比分治 0.84 ms，差 **148 倍**） |
| `0 <= lists[i].length <= 500` | 单链表最多 500 | 单个链表很短，所以「k 很大而 N 不大」是常态 → 算法要能承受大 `k` |
| 总节点数 `<= 10⁴` | N 最多 1 万 | 这是个**很小的规模**：几乎所有解法都能过。所以这题在 LeetCode 上「能过」毫无意义，考的是复杂度分析 |
| 值域 `-10⁴ ~ 10⁴` | 2·10⁴ 宽 | 可以收集值 + 计数排序，但会丢掉链表结构（面试官通常不接受） |

> 💡 **量级 → 算法**：看到 `k` 个有序序列合并，先算清 **`N`（总元素数）和 `k` 谁大**。
> `k` 小而 `N` 大 → 堆归并与分治归并都行；`k` 大到接近 `N` → **必须**用分治或堆（顺序合并会退化到 `O(N²)`）。
> **`O(N log k)` 里的 `log k` 是这题唯一的难点。**

### 示例

```text
输入：lists = [[1,4,5],[1,3,4],[2,6]]
输出：[1,1,2,3,4,4,5,6]
解释：三个链表分别是 1→4→5、1→3→4、2→6，合并后 1→1→2→3→4→4→5→6

输入：lists = []
输出：[]

输入：lists = [[]]
输出：[]          ← 一个空链表，结果也是空
```

### 我自造的五个反例

```text
① lists = []                          → 打「不判空数组」
② lists = [[]]                        → 打「不判空链表」：建堆时会把 nil 塞进去
③ lists = [[], [1], []]               → 打「堆初始化的过滤」
④ lists = [[1,1],[1,1]]               → 打「比较用 < 而不是 <=」：值相同时顺序应保持左边优先
⑤ k = 1000、每个链表 1 个节点          → 打「顺序两两合并」：退化成 O(k²)，实测 124.8 ms
```

⑤ 是最有价值的反例：它把「合并顺序」这件事从「实现细节」变成了「复杂度问题」。

---

## 034.2 从暴力到最优：第一个念头为什么错

### 第一步：暴力 A（全收集 + 排序 + 重建链表）

最省脑子的写法：把所有结点的值收集到一个数组，排序，再串成一个新链表。

```text
vals = []
for list in lists: for node in list: vals.append(node.Val)
sort(vals)                        // O(N log N)
重建链表
```

- 时间 `O(N log N)`，空间 `O(N)`。
- 实测（Go 1.19.8 / 总节点 10⁵）：`k=10` 时 **18.4 ms**、`k=1000` 时 **15.5 ms**。
- **它甚至不比堆归并慢多少**（堆是 8.3 / 13.5 ms），因为 `N` 很小时排序的常数极低。

**它错在哪？** 两个地方：

1. **扔掉了「每段已有序」这个信息**。`k` 个有序序列里存在 `k-1` 个天然的分界点，全排序把已有的顺序又重建了一遍。
2. **需要重建链表**。`O(N)` 个新结点，如果原链表是「别人家的数据」或结点很大（比如带 100 个字段），内存直接翻倍。**堆归并与分治归并都能原地串接已有结点，一个新结点都不用开。**

### 第二步：暴力 B（顺序两两合并）

```text
cur = lists[0]
for i in 1..k-1: cur = mergeTwo(cur, lists[i])     // 复用 021 / 047 的两路归并
```

- 时间 `O(k·N)`：第 `i` 次合并要遍历「已合并的 `i·N/k` 个结点 + 第 `i+1` 段的 `N/k` 个结点」，累计 `Σ i·N/k ≈ N·k/2`。
- 实测（总节点 10⁵）：`k=10` → 5.2 ms、`k=100` → **22.9 ms**、`k=1000` → **191.5 ms**。
- 最坏情形 `k = N = 10⁴`（每个链表 1 个结点）：**124.8 ms**。

看这一列的增长：`k` 从 10 增到 1000，时间从 5.2 ms 涨到 191.5 ms（**37 倍**），而 `N` 一直没变。**这就是 `O(k·N)` 的指纹。** 它错在：每次都把「已经合并好的长链表」从头再遍历一遍，做了大量重复劳动。

### 第三步：卡点在哪 —— 「找下一个最小」被做成了线性

每输出一个结点，都要回答一次：

> **当前 `k` 个链表的头结点里，值最小的是哪一个？**

- 顺序扫描：`O(k)` 每次 → 总共 `N·k`。
- 两两合并：没有显式扫描，但「长链表被重复遍历」等价于同样的浪费。

所以卡点非常明确：**「在 `k` 个候选里反复取最小」这个操作，被实现成了 `O(k)`。**

### 第四步：灵感 A —— 用堆把「取最小」变成 `O(log k)`

这正是堆的看家本领：维护一个**大小为 `k` 的小顶堆**，堆里放「每个链表当前未消费的头结点」。

```text
初始化：把 k 个链表的头结点（非空的）全部入堆
循环：  弹出堆顶（全局最小的结点）→ 接到结果链表尾部
        如果它还有 next，把 next 入堆（补上这个链表的新头）
直到堆空
```

每次操作 `O(log k)`，共 `N` 次 → `O(N log k)`。

**为什么只需要 `k` 个元素在堆里？** 因为每个链表已经有序，所以「某个链表的第 2 个结点」在「它的第 1 个结点」被弹出之前，**不可能**是全局最小。这是一个非常漂亮的不变量，§034.3.2 会严格证明。

### 第五步：灵感 B —— 分治：把 `k` 路摊成 `log k` 层两两归并

顺序合并之所以慢，是因为「一条长链表被反复遍历」。换个合并顺序就能修好：

```text
第 1 轮：[0]+[1], [2]+[3], [4]+[5], …          N/2 → 每层总遍历量都是 N
第 2 轮：[01]+[23], [45]+[67], …
…
共 log2(k) 轮
```

每一层的总遍历量都是 `N`（每个结点在每一层恰好被访问一次），共 `log₂k` 层 → `O(N log k)`。

**关键洞察**：**复杂度不取决于「合并了几次」，而取决于「每个结点被遍历了几次」。**
顺序合并时，靠前的结点被遍历 `k` 次；分治时，每个结点被遍历 `log₂k` 次。这就是两者复杂度的全部差别。

```text
顺序合并（k=8）:  ((((((1+2)+3)+4)+5)+6)+7)+8      第 1 段的结点被遍历 7 次
分治合并（k=8）:        (1+2)(3+4)(5+6)(7+8)        每个结点被遍历 3 次 = log2(8)
                        (12)(34)(56)(78)
                        (1234)(5678)
                        (1..8)
```

---

## 034.3 原理拆解

### 034.3.1 数据结构与算法选型

| 方案 | 时间 | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- |
| 全收集 + 排序 + 重建 | O(N log N) | O(N) 新结点 | 15 行 | 需要原地串接（不能开新结点）时；`N` 极大时排序常数与内存都吃亏 |
| 顺序两两合并 | **O(k·N)** | O(1) | 12 行 | `k` 大时（`k=1000` 实测慢 24 倍） |
| **堆归并** ✅ | **O(N log k)** | **O(k)** | 20 行 + 堆实现 | Java 的 `PriorityQueue` 有装箱与接口开销（实测比分治慢 1.5~2.5 倍）；`k` 未知的流式场景反而是它赢 |
| **分治归并** ✅ | **O(N log k)** | O(log k) 栈 | 25 行 | 不能增量处理（必须一次拿到全部 `k` 个头）；`k` 极大时递归层数 `log k` 仍安全 |
| 锦标赛树 / 败者树 | O(N log k) | O(k) | 60 行 | 常数比堆更小，但代码复杂度高，只在外排实现里值得 |
| 计数排序（值域 2·10⁴） | O(N + V) | O(V) | 12 行 | 值域一大就崩；且要重建链表 |

**实测对比**

**Go 1.19.8 / 总节点 10⁵ / 单位 ms / 5 次取最快**

| k | 堆归并 | 分治归并 | 顺序两两 | 收集排序 |
| --- | --- | --- | --- | --- |
| 10 | 8.274 | **4.772** | 5.224 | 18.432 |
| 100 | 12.088 | **5.794** | 22.892 | 16.915 |
| 1000 | 13.508 | **8.044** | 191.506 | 15.487 |
| 10⁴（每链表 1 个结点） | 1.492 | **0.840** | 124.803 | 0.907 |

**Java 17 / 总节点 10⁵ / 单位 ms / 5 次取最快**

| k | 堆归并 | 分治归并 | 堆 / 分治 |
| --- | --- | --- | --- |
| 10 | 8.066 | **3.953** | 2.04× |
| 100 | 9.730 | **6.402** | 1.52× |
| 1000 | 17.438 | **6.934** | 2.51× |

**从这三张表要读出四件事**：

1. **分治在所有 `k` 上都最快**（Go 快 1.7~1.9 倍，Java 快 1.5~2.5 倍）。原因在 §034.3.3 详细展开：分治是**顺序访问 + 零额外结构**，堆是**指针跳跃 + 每次 `sift` 都要随机访问数组**，缓存命中率差一个档次。
2. **顺序合并的退化肉眼可见**：`k=1000` 时 191.5 ms，是分治的 **24 倍**；而 `k` 只是从 10 涨到 1000，`N` 完全没变。
3. **收集排序在 `k` 大时反而有竞争力**（15.5 ms 对堆的 13.5 ms）。因为 `N = 10⁵` 的 `sort.Ints` 只要 13.6 ms，而链表遍历 + 建堆 + `sift` 的开销也不小。**但它需要 `O(N)` 个新结点，这在工程上是硬伤。**
4. **`k` 接近 `N` 时，`log k` 与 `log N` 已经差不多**，所以分治和堆的复杂度优势都减弱，此时「常数」决定胜负——分治 0.84 ms 完胜。

### 034.3.2 为什么这个做法是对的（正确性论证）

**（1）堆归并的正确性**

不变量：**堆中恰好保存「每个非空链表当前未被输出的第一个结点」。**

- 初始化：每个链表的第一个结点入堆，成立。
- 归纳：弹出堆顶 `cur`（由归纳假设，它是所有候选里最小的），接到结果尾部；然后把 `cur.Next` 入堆，替换掉「该链表已消费的头」。其它链表的头没变，所以不变量保持。∎

**为什么只需要 `k` 个元素在堆里？** 引理：设链表 `L` 的第 `j` 个结点尚未入堆（`j > 1`），则 `L` 的第 1 个结点仍在堆中或已被输出。若未被输出，则它 `≤` 第 `j` 个结点（链表升序），而堆里已经有它，所以第 `j` 个结点不可能是全局最小；若已被输出，则第 `j` 个结点尚未成为「该链表的头」，按算法它此时还不该被考虑。所以每一步只需比较 `k` 个候选。∎

**全局最优性**：每次输出的都是「当前所有未输出结点里的最小值」，且已输出的序列单调不减（因为堆顶单调递增），所以输出序列就是全局升序。∎

**（2）分治归并的正确性**

命题：`mergeRange(lists, lo, hi)` 返回 `lists[lo..hi]` 全部结点按升序串成的一条链表。

- 基础：`lo == hi` 时返回 `lists[lo]` 本身，它已升序。
- 归纳：`mergeTwo(A, B)` 在 `A`、`B` 都已升序时返回升序链表（经典双指针归并的正确性：每次取两者头部较小者，剩下的部分仍然有序，对总长度归纳即得）。由归纳假设两半都有序，合并后整体有序。∎

**（3）稳定性**

`mergeTwo` 里用 `a.Val <= b.Val` 取 `a`（左边优先）。相等时先输出左边的结点，所以**同值结点的相对顺序被保留** → 稳定。堆归并这边：`Less(i,j)` 返回 `h[i].Val < h[j].Val`，值相等时 `container/heap` 的 `sift` **不保证**谁先出堆，所以**堆归并是不稳定的**——这是两者一个容易忽视的差异。

### 034.3.3 通用解题思路（可迁移的那一层）

本题是 **「k 路归并」** 这个模型的母题，它是外部排序（external merge sort）的心脏：

```
原始问题：把 k 个有序序列合成一个有序序列
          ↓ 关键操作：反复「在 k 个候选里取最小」
两条实现：
   A. 堆：把 k 个候选放进小顶堆 → 每次 O(log k)，总 O(N log k)
   B. 分治：两两合并，共 log2(k) 层 → 每层总遍历 N，总 O(N log k)
          ↓ 选哪个
   需要「流式 / k 未知 / 只读一次」 → A（堆）
   能一次拿到全部 k 个头、追求最快    → B（分治）
```

同一模型下的题：

| 题 | k 路是什么 | 备注 |
| --- | --- | --- |
| [LC 23 合并 K 个升序链表](https://leetcode.cn/problems/merge-k-sorted-lists/) | `k` 条链表 | 本章 |
| [LC 21 合并两个有序链表](https://leetcode.cn/problems/merge-two-sorted-lists/) | `k = 2` | 分治的「叶子」操作（见 047 章） |
| [LC 88 合并两个有序数组](https://leetcode.cn/problems/merge-sorted-array/) | `k = 2`，数组版 | 必须逆向填充 |
| [LC 148 排序链表](https://leetcode.cn/problems/sort-list/) | `k = 2`，递归到底 | 归并排序的链表版（见 052 章） |
| [LC 373 查找和最小的 K 对数字](https://leetcode.cn/problems/find-k-pairs-with-smallest-sums/) | 堆里放「候选对」 | 与本章的堆归并同构：`k` 个候选 + 每次补一个新候选 |
| [LC 632 最小区间](https://leetcode.cn/problems/smallest-range-covering-elements-from-k-lists/) | `k` 个指针的滑动窗口 | 堆维护 `k` 个当前值的最小/最大 |
| [LC 378 有序矩阵中第 K 小的元素](https://leetcode.cn/problems/kth-smallest-element-in-a-sorted-matrix/) | 二维的 `k` 路 | 堆 / 二分 |
| [LC 1439 有序矩阵中的第 k 个最小数组和](https://leetcode.cn/problems/find-the-kth-smallest-sum-of-a-matrix-with-sorted-rows/) | 逐行做 `k` 路归并 | 每行只保留前 `k` 个 |

**共同点**：**「`k` 个有序源 + 每次取一个最小」= `k` 路归并。要么用堆把取最小变 `log k`，要么用分治把「重复遍历」摊平。**

### 034.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| 数据在**磁盘**上、`k` 个有序文件 | 外部排序 | 堆归并（堆里放每个文件的读缓冲头）；`k` 太大时分多轮（每轮 `M` 路，`M` 受文件句柄限制） |
| 链表改成**数组** | 需要 `O(1)` 随机访问 | 分治仍是最快；堆要存 `(数组下标, 位置)` 二元组 |
| `k` 是**流式**的（链表一条条到达） | 无法预先分治 | **只能堆**：不断把新链表头入堆 |
| 要求**稳定** | 同值结点保持原顺序 | 分治（`<=` 取左）稳定；堆不稳定 → 比较时加「链表下标」当第二关键字 |
| 要求 `O(1)` 额外空间 | 不允许 `O(k)` 的堆 | **自底向上**的两两合并（迭代版分治）：只用几个指针，`O(1)` 空间，仍是 `O(N log k)` |
| `k` 极大（如 10⁶ 个链表） | 建堆的开销 `O(k)` 不可忽略 | 先过滤空链表；或先「小规模批量两两合并」把 `k` 压到 `√N` 量级再用堆 |

---

## 034.4 图解推演

### 034.4.1 堆归并的执行轨迹

`lists = [[1,4,5],[1,3,4],[2,6]]`，`k = 3`：

```text
初始堆（3 个链表的头）：
        ┌───┐
        │ 1 │  ← 链表 0 的头
        │ 1 │  ← 链表 1 的头
        │ 2 │  ← 链表 2 的头
        └───┘
        堆顶 = 1（链表 0）

Step1 弹出 1(链表0) → 结果 1 → 把 4 入堆
        堆：{1(链表1), 2(链表2), 4(链表0)}      结果：1
Step2 弹出 1(链表1) → 结果 1→1 → 把 3 入堆
        堆：{2(链表2), 3(链表1), 4(链表0)}      结果：1→1
Step3 弹出 2(链表2) → 结果 1→1→2 → 把 6 入堆
        堆：{3(链表1), 4(链表0), 6(链表2)}      结果：1→1→2
Step4 弹出 3(链表1) → → 把 4 入堆（链表1 的第二个 4）
        堆：{4(链表0), 4(链表1), 6(链表2)}      结果：1→1→2→3
Step5 弹出 4(链表0) → 链表0 已到尾，不再入堆
        堆：{4(链表1), 6(链表2)}                结果：1→1→2→3→4
Step6 弹出 4(链表1) → 不再入堆
        堆：{6(链表2)}                          结果：1→1→2→3→4→4
Step7 弹出 6 → 不再入堆，堆空 → 结束
        结果：1→1→2→3→4→4→5→6   ✓
```

状态表（`堆` 列按出堆顺序的候选列出）：

| 步 | 弹出的结点 | 来自 | 有 next？ | 入堆的元素 | 堆内元素（小顶堆） | 结果链表 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 链表 0 | 有（4） | 4 | `{1, 2, 4}` | `1` |
| 2 | 1 | 链表 1 | 有（3） | 3 | `{2, 3, 4}` | `1→1` |
| 3 | 2 | 链表 2 | 有（6） | 6 | `{3, 4, 6}` | `1→1→2` |
| 4 | 3 | 链表 1 | 有（4） | 4 | `{4, 4, 6}` | `1→1→2→3` |
| 5 | 4 | 链表 0 | 有（5） | 5 | `{4, 5, 6}` | `1→1→2→3→4` |
| 6 | 4 | 链表 1 | 无 | — | `{5, 6}` | `1→1→2→3→4→4` |
| 7 | 5 | 链表 0 | 无 | — | `{6}` | `1→1→2→3→4→4→5` |
| 8 | 6 | 链表 2 | 无 | — | `{}` | `1→1→2→3→4→4→5→6` |

> 注意第 5 步：链表 0 的 `4` 弹出后还有 `5`，所以入堆；**漏掉这行「入堆」是本题第二大 bug**（第一是没过滤空链表）。

### 034.4.2 分治归并的合并树

`k = 8`，每个结点在每一层被访问一次：

```text
第 0 层  [0]  [1]  [2]  [3]  [4]  [5]  [6]  [7]      8 个有序链表
           \  /      \  /      \  /      \  /
第 1 层   [0,1]    [2,3]    [4,5]    [6,7]           4 次合并，总访问 N
             \      /           \      /
第 2 层      [0..3]             [4..7]               2 次合并，总访问 N
                 \             /
第 3 层            [0..7]                            1 次合并，总访问 N

每层遍历 N 个结点，共 log2(8) = 3 层 → 总访问 3N = O(N log k)
```

对比顺序合并（`k = 8`）：

```text
[0]+[1] → [0,1]                    访问 2N/8
[0,1]+[2] → [0,2]                  访问 3N/8
[0,2]+[3] → [0,3]                  访问 4N/8
…                                
[0..6]+[7] → [0..7]                访问 8N/8

总访问 = (2+3+4+…+8)·N/8 = 35·N/8 ≈ 4.4N     ← 结点 0 被访问了 7 次
```

`k` 越大，两者的差距越大：分治是 `log₂k` 次，顺序是 `k/2` 次。

### 034.4.3 为什么分治比堆快（常数的来源）

```text
堆归并的一次操作：
  pop → 取 h[0]，把 h[n-1] 换到根 → 下沉 log k 层
        每层要读 h[2i+1]、h[2i+2] 两个随机位置  ← 缓存不友好（数组跳跃访问）
  push → 追加末尾 → 上浮 log k 层                ← 同样跳跃
  总计：约 2·log k 次「指针跳转 + 比较」

分治归并的一次操作：
  mergeTwo → while 循环里 a = a.Next / b = b.Next
        每次只读 a.Val、b.Val，然后顺着 next 指针往前走 ← 顺序访问
  总计：每次输出 1 个结点，只做 1 次比较，没有额外数组访问
```

**这就是 1.5~2.5 倍差距的来源：堆的 `sift` 是随机访问，分治归并是纯顺序访问。** 这也是 028 章那条结论的再现——**同样 `O(n log n)`，缓存友好度决定 30%~50% 的差距**。

### 034.4.4 四条路线对比

```text
                  ┌────────── k 个有序链表（总长 N）──────────┐
                  │                                          │
     ┌────────────▼───────────┐   ┌──────────────▼─────────┐   ┌────────▼────────────┐
     │ 堆归并                  │   │ 分治归并                │   │ 顺序两两合并         │
     │ O(N log k) / O(k) 空间  │   │ O(N log k) / O(logk)栈  │   │ O(k·N)               │
     │ 稳定？否                │   │ 稳定？是                │   │ 稳定？是             │
     │ 支持流式 / k 未知 ✅    │   │ 必须拿到全部 k 个头      │   │ 支持流式             │
     └────────────┬───────────┘   └──────────────┬─────────┘   └────────┬────────────┘
                  │                              │                      │
        Go 实测 k=1000: 13.5ms        Go 实测 k=1000: 8.0ms      k=1000: 191.5ms
                  │                              │                      │
              【流式场景选它】              【离线最快，本章默认】      【k 大时禁止】
```

---

## 034.5 双语言实现

### 034.5.1 Java

```java
import java.util.PriorityQueue;

class Solution {

    // ListNode 由 LeetCode 提供；本地编译时才需要自己定义
    static class ListNode {
        int val;
        ListNode next;
        ListNode() {}
        ListNode(int val) { this.val = val; }
        ListNode(int val, ListNode next) { this.val = val; this.next = next; }
    }

    // ===== 解法一：小顶堆归并，O(N log k) 时间 / O(k) 空间 =====
    public ListNode mergeKLists(ListNode[] lists) {
        // 堆里只放「每个链表当前未消费的头结点」，容量上界是 k（不是 N！）
        PriorityQueue<ListNode> pq = new PriorityQueue<>(
                Math.max(1, lists.length),                       // 容量必须 ≥ 1，否则抛 IllegalArgumentException
                (a, b) -> Integer.compare(a.val, b.val));        // 用 compare 而不是 a.val - b.val，避免相减溢出

        for (ListNode p : lists) {
            if (p != null) pq.offer(p);                          // 必须过滤 null，否则比较时 NPE
        }

        ListNode dummy = new ListNode(-1);                       // 哑结点：省掉「结果为空」的特判
        ListNode tail = dummy;
        while (!pq.isEmpty()) {
            ListNode cur = pq.poll();                            // 当前全局最小的结点
            tail.next = cur;
            tail = cur;
            if (cur.next != null) pq.offer(cur.next);            // 补上它所在链表的下一个候选
        }
        return dummy.next;                                       // 全空时自然是 null
    }

    // ===== 解法二：分治归并，O(N log k) 时间 / O(log k) 栈 =====
    public ListNode mergeKListsDivide(ListNode[] lists) {
        if (lists.length == 0) return null;
        return mergeRange(lists, 0, lists.length - 1);
    }

    private ListNode mergeRange(ListNode[] lists, int lo, int hi) {
        if (lo == hi) return lists[lo];                          // 叶子：一条链表本身
        int mid = lo + (hi - lo) / 2;                            // 这样写避免 lo + hi 溢出
        return mergeTwo(mergeRange(lists, lo, mid), mergeRange(lists, mid + 1, hi));
    }

    private ListNode mergeTwo(ListNode a, ListNode b) {
        ListNode dummy = new ListNode(-1);
        ListNode t = dummy;
        while (a != null && b != null) {
            if (a.val <= b.val) {                                // <= 而不是 <：保证稳定
                t.next = a;
                a = a.next;
            } else {
                t.next = b;
                b = b.next;
            }
            t = t.next;
        }
        t.next = (a != null) ? a : b;                            // 剩下的整段直接挂上，不必逐结点搬
        return dummy.next;
    }
}
```

### 034.5.2 Go

```go
package main

import (
	"container/heap"
	"fmt"
	"math/rand"
	"sort"
)

type ListNode struct {
	Val  int
	Next *ListNode
}

// ===== 解法一：小顶堆归并，O(N log k) 时间 / O(k) 空间 =====
// Go 的 container/heap 要求实现 5 个方法：Len / Less / Swap / Push / Pop。
// 与 Java 的 PriorityQueue 相比，Java 只要一个 Comparator；Go 必须落地一个具体类型。
type nodeHeap []*ListNode

func (h nodeHeap) Len() int            { return len(h) }
func (h nodeHeap) Less(i, j int) bool  { return h[i].Val < h[j].Val } // 按结点值的小顶堆
func (h nodeHeap) Swap(i, j int)       { h[i], h[j] = h[j], h[i] }
func (h *nodeHeap) Push(x interface{}) { *h = append(*h, x.(*ListNode)) }

func (h *nodeHeap) Pop() interface{} {
	old := *h
	n := len(old)
	v := old[n-1]
	old[n-1] = nil // 置 nil：*ListNode 是引用，不清掉会让这个结点无法被 GC（切片还握着它）
	*h = old[:n-1]
	return v
}

func mergeKLists(lists []*ListNode) *ListNode {
	h := &nodeHeap{}
	for _, p := range lists {
		if p != nil { // 必须过滤 nil：否则 Pop 出来的 nil 会让 h[i].Val 直接 panic
			*h = append(*h, p)
		}
	}
	heap.Init(h) // 一次性建堆 O(k)，比 k 次 Push 的 O(k log k) 更快

	dummy := &ListNode{}
	tail := dummy
	for h.Len() > 0 {
		cur := heap.Pop(h).(*ListNode) // 当前全局最小的结点
		tail.Next = cur
		tail = cur
		if cur.Next != nil {
			heap.Push(h, cur.Next) // 补上它所在链表的下一个候选
		}
	}
	return dummy.Next // 全空时自然是 nil
}

// ===== 解法二：分治归并，O(N log k) 时间 / O(log k) 栈 =====
func mergeKListsDivide(lists []*ListNode) *ListNode {
	if len(lists) == 0 {
		return nil
	}
	return mergeRange(lists, 0, len(lists)-1)
}

func mergeRange(lists []*ListNode, lo, hi int) *ListNode {
	if lo == hi {
		return lists[lo]
	}
	mid := lo + (hi-lo)/2
	return mergeTwo(mergeRange(lists, lo, mid), mergeRange(lists, mid+1, hi))
}

func mergeTwo(a, b *ListNode) *ListNode {
	dummy := &ListNode{}
	t := dummy
	for a != nil && b != nil {
		if a.Val <= b.Val { // <= 而不是 <：保证稳定
			t.Next = a
			a = a.Next
		} else {
			t.Next = b
			b = b.Next
		}
		t = t.Next
	}
	if a != nil {
		t.Next = a // 剩下整段直接挂上
	} else {
		t.Next = b
	}
	return dummy.Next
}

func main() {
	r := rand.New(rand.NewSource(23))

	// 构造 k 条升序链表，总长约 total
	mk := func(k, total int) []*ListNode {
		per := total / k
		list := make([]*ListNode, k)
		for i := 0; i < k; i++ {
			var head, tail *ListNode
			cur := 0
			for j := 0; j < per; j++ {
				cur += r.Intn(3) // 步长随机但在 [0,2]，保证升序且重复值多
				n := &ListNode{Val: cur}
				if head == nil {
					head = n
				} else {
					tail.Next = n
				}
				tail = n
			}
			list[i] = head
		}
		return list
	}
	clone := func(lists []*ListNode) []*ListNode {
		out := make([]*ListNode, len(lists))
		for i, h := range lists {
			dummy := &ListNode{}
			t := dummy
			for p := h; p != nil; p = p.Next {
				t.Next = &ListNode{Val: p.Val}
				t = t.Next
			}
			out[i] = dummy.Next
		}
		return out
	}
	toSlice := func(h *ListNode) []int {
		var out []int
		for p := h; p != nil; p = p.Next {
			out = append(out, p.Val)
		}
		return out
	}

	// 对拍：两种解法都必须等于「收集所有值后排序」
	for iter := 0; iter < 3000; iter++ {
		k := r.Intn(6) + 1
		list := mk(k, 1+r.Intn(30))
		var want []int
		for _, h := range list {
			want = append(want, toSlice(h)...)
		}
		sort.Ints(want)
		for _, got := range [][]int{toSlice(mergeKLists(clone(list))), toSlice(mergeKListsDivide(clone(list)))} {
			if fmt.Sprint(got) != fmt.Sprint(want) {
				panic(fmt.Sprintf("got=%v want=%v", got, want))
			}
		}
	}
	fmt.Println("mergeKLists / mergeKListsDivide: 3000 组随机对拍通过")

	// 三个边界
	fmt.Println("空数组 →", toSlice(mergeKLists(nil)))
	fmt.Println("一个空链表 →", toSlice(mergeKLists([]*ListNode{nil})))
	fmt.Println("全空链表 →", toSlice(mergeKLists([]*ListNode{nil, nil})))
}
```

### 034.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 堆的类型 | `PriorityQueue<ListNode>` + 一个 `Comparator` | 必须定义 `nodeHeap` 类型并实现 5 个方法 | Go 代码多 12 行；**5 个方法里 `Less` 写反就是大顶堆**，结果变成降序 |
| 建堆 | 逐个 `offer`，`O(k log k)` | `heap.Init` 一次性建堆 `O(k)` | Go 有明显优势：`k = 10⁴` 时省掉 `10⁴·log` 次上浮 |
| `null` / `nil` | `pq.offer(null)` 会在比较时抛 `NPE` | `h[i].Val` 对 nil 直接 panic | 两边都必须先过滤；**Go 的 panic 更难定位**（栈里只有 `Less` 一行） |
| 哑结点的构造 | `new ListNode(-1)` | `&ListNode{}`（零值 `Val = 0`） | 哑结点的值**不参与任何比较**，因为 `tail` 从 `dummy` 起步时先赋值 `tail.Next` 再前移；写 `new ListNode(lists[0].val)` 这类「小聪明」反而容易错 |
| 容量参数 | `new PriorityQueue<>(cap)` 要求 `cap >= 1`，`k = 0` 时抛异常 | 切片天然支持 0 长度 | Java 必须写 `Math.max(1, lists.length)` |
| `Pop` 的清理 | `poll()` 内部把数组槽位置 `null` | 手写 `old[n-1] = nil` | **Go 里不清 nil 会导致整条链表无法被 GC**（切片仍持有引用）——这是 Go 特有的内存泄漏点 |
| 递归深度 | `log₂(10⁴) ≈ 14` 层，安全 | 同样安全，且 goroutine 栈可增长 | 两边都不必担心；但如果改成「顺序合并的递归写法」就是 `k` 层，`k = 10⁴` 时 Java 会栈溢出 |

---

## 034.6 复杂度与常见坑

### 034.6.1 复杂度推导

- **堆归并 `O(N log k)`**：`N` 次「弹出 + 可能入堆」，每次 `O(log k)`（堆大小恒为 `k`）。建堆 `O(k)`。
  - 注意是 `N log k` 而不是 `N log N`：堆里只有 `k` 个元素，不是 `N` 个。**这是整个算法省下时间的根源。**
- **分治归并 `O(N log k)`**：递归树有 `⌈log₂k⌉` 层，每层的合并总量恰好 `N`（每个结点在每层被访问一次）。`T(k) = 2T(k/2) + O(N)` → `O(N log k)`。
- **顺序两两合并 `O(k·N)`**：第 `i` 次合并遍历 `i·N/k + N/k` 个结点，求和得 `≈ N·k/2`。
- **收集排序 `O(N log N)`**：排序主导，另外要 `O(N)` 个新结点。
- **空间**：堆 `O(k)`；分治 `O(log k)` 栈；顺序合并 `O(1)`；收集排序 `O(N)`。

### 034.6.2 八个真实踩过的坑

1. **建堆时没过滤 `null`**。`lists = [[]]` 时把 `null` 放进堆，`poll` 后访问 `cur.next` 直接 `NPE` / panic。
2. **忘了把 `cur.next` 入堆**。结果只输出了每条链表的第一个结点，后面的全丢了。这是「链表版」特有的 bug——数组版没有「剩余部分」。
3. **比较用 `<` 而不是 `<=`**（分治里）。值相同时右边优先，破坏了稳定性。LeetCode 判题看不出来（只比值），但业务代码里会出错。
4. **`k = 0` 时 `new PriorityQueue<>(0)`**。Java 抛 `IllegalArgumentException: initial capacity must be >= 1`。
5. **`Arrays.sort` 思路的隐性成本**。收集重建会开 `N` 个新结点；如果原结点是「带锁的、带引用计数的」对象，`O(N)` 额外内存可能直接 OOM。
6. **Go 的 `Pop` 没把 `old[n-1]` 置 `nil`**。切片底层数组仍持有那个 `*ListNode` 的引用，整条链表链都无法回收。`k = 10⁴`、`N = 10⁵` 时这是实实在在的内存泄漏。
7. **`int mid = (lo + hi) / 2`**。`lo + hi` 在 `k` 接近 `2³¹` 时溢出；虽然本题 `k ≤ 10⁴` 不会触发，但这是分治的通用规范。
8. **以为「堆归并也是稳定的」**。`container/heap` 与 `PriorityQueue` 都不保证同优先级元素的相对顺序，所以堆归并**不稳定**。要稳定就用分治，或在堆的比较里加「链表下标」当第二关键字。

### 034.6.3 面试官最爱追问的三个问题

**Q1：堆归并和分治归并都是 `O(N log k)`，你选哪个？**
> 「离线（能一次拿到全部 `k` 个头）我选分治，因为常数小：Go 实测 `k=1000` 时 8.0 ms vs 13.5 ms，Java 是 6.9 ms vs 17.4 ms，差 1.5~2.5 倍。原因很具体——堆的 `sift` 每层要读 `h[2i+1]`、`h[2i+2]` 这种跳跃位置，缓存不友好；而分治的 `mergeTwo` 是顺着 `next` 指针顺序走。分治还额外白送两个好处：**稳定**和 `O(log k)` 栈（比堆的 `O(k)` 空间更省）。只有在『`k` 未知 / 链表流式到达 / 数据在磁盘上只能顺序读』时，堆才是唯一解。」

**Q2：为什么顺序两两合并会退化到 `O(k·N)`？**
> 「因为『已经合并好的长链表』会被反复从头遍历。第 `i` 次合并要遍历 `i` 条链表的总长，累计 `Σ i·L ≈ k²L/2`，而 `N = kL`，所以是 `O(k·N)`。**换个合并顺序就修好了**：分治让每个结点在每一层只被遍历一次，共 `log₂k` 层。关键在于——**复杂度不取决于『合并了几次』，而取决于『每个结点被遍历了几次』**。实测 `k=1000` 时顺序合并 191.5 ms，分治 8.0 ms，差 24 倍。」

**Q3：如果 `k = 10⁶` 个链表，每个只放 1 个结点，内存只有 64 MB，怎么合？**
> 「这时 `N = 10⁶`，`log k = 20`，`O(N log k) = 2·10⁷` 次操作，时间不是问题；问题是**链表结点本身的指针开销**（每个结点 16~24 字节 + 指针）和「一次要持有 `10⁶` 个头」。做法：① **先过滤空链表**，把 `k` 压小；② **分批**——每 `M`（比如 10³）个链表先合并成一条，得到 `10³` 条中间链表，再对它们做一次 k 路归并，总代价仍是 `O(N log k)` 但峰值内存降到 `O(M)`；③ 若数据在磁盘上，就直接用外部排序的多轮 k 路归并，每轮的 `M` 由文件句柄限制决定。」

---

## 034.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 21 合并两个有序链表](https://leetcode.cn/problems/merge-two-sorted-lists/) | 简单 | `k = 2`，是本章分治的叶子操作 |
| [LC 88 合并两个有序数组](https://leetcode.cn/problems/merge-sorted-array/) | 简单 | 数组版，必须**逆向**填充避免覆盖 |
| [LC 148 排序链表](https://leetcode.cn/problems/sort-list/) | 中等 | 归并排序的链表版，自底向上可做到 `O(1)` 空间 |
| [LC 373 查找和最小的 K 对数字](https://leetcode.cn/problems/find-k-pairs-with-smallest-sums/) | 中等 | 堆里放「候选对」，每次弹出一个再补两个，与堆归并同构 |
| [LC 378 有序矩阵中第 K 小的元素](https://leetcode.cn/problems/kth-smallest-element-in-a-sorted-matrix/) | 中等 | 二维的 `k` 路归并 / 值域二分 |
| [LC 632 最小区间](https://leetcode.cn/problems/smallest-range-covering-elements-from-k-lists/) | 困难 | 堆维护 `k` 个当前值的最小值，同时维护最大值 → 滑动窗口 |
| [LC 1439 有序矩阵中的第 k 个最小数组和](https://leetcode.cn/problems/find-the-kth-smallest-sum-of-a-matrix-with-sorted-rows/) | 困难 | 逐行做「只保留前 k 个」的 `k` 路归并 |
| [LC 4 寻找两个正序数组的中位数](https://leetcode.cn/problems/median-of-two-sorted-arrays/) | 困难 | `k = 2` 且只要中位数 → 二分划分，不需要全合并 |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 373 查找和最小的 K 对数字**
1. 把 `nums1` 的每个下标 `i` 与 `nums2[0]` 组成一对，放进小顶堆——为什么这样就够覆盖所有候选？
2. 弹出一对 `(i, j)` 之后，应该补进哪一对才能保证不漏？边界（`j` 已经是最后一个）怎么处理？

**LC 632 最小区间**
1. 每个列表都升序，那么「覆盖所有列表各一个元素」的区间，其左右端点一定分别来自「当前 `k` 个指针里的最小值」和「最大值」——为什么？
2. 想让区间变窄，应该移动哪个指针？

**LC 148 排序链表**
1. 链表不能随机访问，快排的分区为什么行不通？归并的哪一步在链表上反而更容易？
2. 如果要求 `O(1)` 空间，递归的「找中点」能用什么替代？（想想自底向上：先合并长度 1 的相邻两段，再合并长度 2 的……）

**通用**
- `k` 路归并的两条腿：堆（取最小 `log k`）与分治（每层摊平 `N`）。先问「能不能一次拿到全部 `k` 个头」，答案决定用哪条。
- 分析链表合并的复杂度时，盯住「每个结点被遍历了几次」，而不是「合并了几轮」。

</details>

---

## 附：本章小结（可背走的三句话）

```text
① k 路归并 = 反复「在 k 个候选里取最小」：堆把这一步变 O(log k)，总 O(N log k)
② 顺序两两合并是 O(k·N)：靠前的结点被遍历 k 次；分治让每个结点只被遍历 log k 次
③ 离线选分治（Go 实测快 1.7~1.9 倍、稳定、O(log k) 栈），流式/k 未知才用堆
```

> **下一章**：[`035-meeting-rooms-ii.md`](035-meeting-rooms-ii.md) —— 区间题的第三种问法：不求合并结果，只求「最大重叠层数」。堆、扫描线、差分三条路通向同一个答案。
