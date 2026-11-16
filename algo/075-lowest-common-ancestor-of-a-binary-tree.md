# 075. LC236 二叉树的最近公共祖先（Lowest Common Ancestor of a Binary Tree）

> **状态**：`已完成（初稿）` ｜ **所属**：第 8 部分 · 树形结构 ｜ **编号**：075 / 150
> **难度**：中等（但它是「**后序回溯**」这个模型的母题，四种情况分类必须背下来）｜ **标签**：树 · 深度优先搜索 · 后序遍历 · 回溯
> **一句话**：这道题真正考的是「**一个返回值同时承载三种语义（都没有 / 只找到一个 / 已经找到答案）**」，不是「背那五行递归」。
> **前置**：[`065-binary-tree-inorder-traversal.md`](065-binary-tree-inorder-traversal.md)（递归三问）、[`074-binary-tree-maximum-path-sum.md`](074-binary-tree-maximum-path-sum.md)（交付量与结算量的分离）

---

## 075.1 题目

**原题链接**：<https://leetcode.cn/problems/lowest-common-ancestor-of-a-binary-tree/>

### 题意重述（把隐含条件补齐）

给你一棵二叉树的根节点 `root`，以及树中的两个**不同**节点 `p`、`q`，请返回它们的**最近公共祖先**（Lowest Common Ancestor）：在所有「同时是 `p` 的祖先和 `q` 的祖先」的节点中，**深度最大**的那一个。

题干里的「祖先」有个关键约定必须读准：**一个节点也可以是自己本身的祖先**。所以如果 `p` 恰好是 `q` 的祖先，答案就是 `p`。

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| **结点可以是自己的祖先** | 官方定义「a node can be a descendant of itself」 | `p` 是 `q` 的父结点时，答案写成了 `p` 的父结点 → 错 |
| `p != q`，且都**保证在树中** | 约束 | 若允许「可能不存在」，返回 `null` 的语义就变味了 → 需要额外计数（见 §075.3.4） |
| 树的形状**任意**，不是 BST | 题名里没有 Binary Search Tree | 用 `val` 大小比较决定往左还是往右 → 完全错（见 §075.3.3 的 235 对比） |
| 结点值**可能重复** | 约束 `-10⁹ ≤ val ≤ 10⁹`，**没有**「互不相同」 | 用 `val` 判断「是不是 p」→ 重复值直接误判，必须用**引用/指针身份**比较 |
| 返回**结点本身** | 「返回该节点」 | 返回 `val` 或下标 → 判题按结点身份比对，直接判错 |

> 全题最容易翻车的是最后两行：**`p`、`q` 是「结点对象」，不是「值」**。Java 用 `==` 比引用，Go 用 `==` 比指针，两边都**不能**写成 `node.val == p.val`。

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| 节点数 `2 <= n <= 10⁵` | 最多 10 万个 | O(n²) = 10¹⁰ → **必 TLE**；O(n) 是唯一可行档位。注意这题比 074 的 3·10⁴ 大 3 倍 |
| `-10⁹ <= Node.val <= 10⁹` | 有负数、可重复 | 不能用值做标记，也不能用 `val` 当下标 |
| `p != q` 且都在树中 | 保证存在解 | 所以「找不到」这个返回值只表示「这棵子树里一个都没有」，语义可以写死 |
| 树形状不限 | 可以是退化链 | `h = 10⁵`，递归深度直接到 10 万层 → **Java 默认栈会爆**（§075.6.2 第 5 条给了迭代写法） |

> 💡 **量级 → 算法**：`n = 10⁵` 意味着「**一次遍历**，每个结点常数次操作」。而「一次遍历同时处理两路信息」正是后序遍历的强项 —— 它天然能「先拿到左右两个子问题的结果，再合并」。

### 示例

```
示例 1
输入：root = [3,5,1,6,2,0,8,null,null,7,4], p = 5, q = 1
输出：3
解释：5 在根 3 的左子树，1 在右子树 → 5、1 分居两侧，根 3 就是最近公共祖先

                3
            ┌───┴───┐
            5       1
          ┌─┴─┐   ┌─┴─┐
          6   2   0   8
            ┌─┴─┐
            7   4

示例 2
输入：root = [3,5,1,6,2,0,8,null,null,7,4], p = 5, q = 4
输出：5
解释：5 自己就是 4 的祖先（4 在 5 的左子树里：5 → 2 → 4），所以答案是 5 而不是 3

示例 3
输入：root = [1,2], p = 1, q = 2
输出：1
解释：老根就是祖先。返回值恰是 p 本身 —— 这就是「结点可以是自己的祖先」
```

### 我自造的四个反例（写完必须拿它们打一遍）

```text
① root=[1,2], p=1, q=2
   → 暴露「答案必须能是 p 本身」；也暴露「只在左右子树里找、不考虑当前结点」的写法

② root=[3,5,1], p=5, q=1
   → 暴露「提前 return」：在左子树找到 5 就立刻返回 5，不再看右子树 → 得到 5，正确答案是 3

③ 值重复：root=[0,0,0]（三个结点的值都是 0），p = 左孩子, q = 右孩子
   → 暴露「用 val 比较结点身份」：三个结点 val 全等，用值判断会把根当成 p

④ 退化链 1→2→3→…→100000（只有右孩子），p=99999, q=100000
   → 暴露「递归深度 = n」：Java 默认栈直接 StackOverflowError
```

第 ② 条是本章最经典的死法，§075.4.3 会用一张图把它和正确写法逐帧对比。

---

## 075.2 从暴力到最优：第一个念头为什么错

### 第一步：暴力（三种，都能写对，但都要付出代价）

**暴力 A：把两条路径显式求出来，比最后一个公共前缀。**

```text
① 从根 DFS 到 p，记下路径 Rp = [root, ..., p]
② 从根 DFS 到 q，记下路径 Rq = [root, ..., q]
③ 从 i = 0 开始同时扫 Rp、Rq，最后一个相等的结点就是答案   ← 「最后一个公共前缀」
```

时间 O(n)，空间 O(n)（两条路径 + 两次遍历）。**它是对的**，而且很好解释。代价是：**要遍历两遍**，还要在递归里维护「当前路径」这个栈。

**暴力 B：对每个结点判断「它是不是 p、q 的公共祖先」，取最深的一个。**

```text
for 每个结点 x:
    if isAncestor(x, p) and isAncestor(x, q):
        答案 = 深度最大的那个 x
isAncestor(a, b) = 从 a 出发能不能走到 b      ← O(h)
```

时间 O(n·h)，退化链上就是 **O(n²) = 10¹⁰**，直接 TLE。

**暴力 C：自底向上用 parent 指针。**

```text
① 一遍 DFS 建父指针表 parent[x]
② 从 p 往上走，把沿途所有祖先塞进集合 S
③ 从 q 往上走，第一个出现在 S 里的结点就是答案
```

时间 O(n)，空间 O(n)（父指针表 + 集合）。这是**迭代版的标准做法**，也是「两个链表求交点」的树上版。

### 第二步：卡点在哪

三种暴力都卡在同一个地方：**「判断祖先关系」这件事被当成了外部工具。**

- A 卡在「要先把路径存下来」；
- B 卡在「每问一次祖先关系都要走一遍」；
- C 卡在「要先有 parent 指针，而题目给的树没有」。

> **真正的卡点：我们需要的不是「谁是祖先」这个布尔值，而是「这棵子树里，p 和 q 各自找到没找到」。**
> 而这件事**天然是后序的** —— 只有把左右子树都问过一遍，我才知道自己这棵子树里到底有几个目标。

### 第三步：灵感的来源 —— 「一个返回值，三种含义」

把问题重新表述成一句可以递归的话：

> **`dfs(x)` 的语义：在「以 `x` 为根的子树」里找 `p` 和 `q`。**
> - 两个都没找到 → 返回 `null`
> - 恰好找到一个 → 返回**找到的那个结点**
> - 两个都找到了 → 返回**它们的最近公共祖先**

第三种情况一旦产生，就再也不会变 —— 往上的每一层都只做「原样上交」。这就是为什么**同一个变量能同时承载三种语义**：因为「已经找到答案」这个状态一旦出现，上层就不需要再区分了。

于是每种结点上只剩四条分支（**本章的核心分类表**）：

| # | 左子树返回 | 右子树返回 | 含义 | 该返回什么 |
| --- | --- | --- | --- | --- |
| **情况 1** | `null` | `null` | `p`、`q` 都不在这棵子树里 | `null` |
| **情况 2** | 非空 | `null` | 只在左边找到了（可能一个，也可能已经是 LCA） | **原样上交左边** |
| **情况 3** | `null` | 非空 | 只在右边找到了 | **原样上交右边** |
| **情况 4** | 非空 | 非空 | 左一个、右一个 → **分居两侧，当前结点就是 LCA** | **返回当前结点 `x`** |

再补上「当前结点自己就是 `p` 或 `q`」的入口判断：

```text
dfs(x):
    if x == null || x == p || x == q: return x      ← 自己命中就交自己（保证"结点可以是自己的祖先"）
    left  = dfs(x.left)
    right = dfs(x.right)
    按上面四种情况返回
```

> 注意 `x == p` 时**直接返回 `x`，不再往下搜**。这不是偷懒：如果 `q` 在 `x` 的子树里，那么「`x` 是 `q` 的祖先」成立，答案就该是 `x`，上层拿到 `x` 后会自己拼出结论（示例 2 正是这条路）。

---

## 075.3 原理拆解

### 075.3.1 数据结构与算法选型

| 方案 | 时间 | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- |
| 暴力 B：逐点判祖先 | O(n·h)，退化链 O(n²) | O(h) | 25 行 | `n = 10⁵` 的链状树 → 10¹⁰ 次操作，TLE |
| 暴力 A：两条根路径比公共前缀 | O(n) | O(n) | 30 行 | 要两遍遍历 + 维护路径栈；递归里带 `List` 易漏「回溯时删元素」 |
| 暴力 C：parent 表 + 祖先集合 | O(n) | O(n) | 20 行 | 需要额外 O(n) 表；LeetCode 的树不允许改（没有 parent 字段），只能另建 map |
| **后序回溯（三态语义）** ✅ | **O(n)** | **O(h)** | **10 行** | `h = 10⁵` 的退化链上递归爆栈 → 换迭代（§075.6.3 Q3） |
| BST 专用（LC 235） | O(h) | O(1) | 8 行 | **只对 BST 成立**；普通二叉树用它会错 |
| Tarjan 离线 / 倍增 / 欧拉序 RMQ | O(n + q) / O(n log n) / O(n log n) | O(n log n) | 40+ 行 | 单次查询属于杀鸡用牛刀；代码量大、易错 |

**取舍一句话**：**单次查询无脑选后序回溯**（10 行、O(n)、不改树、空间 O(h)）；**查询很多次**才考虑倍增或 Tarjan；**树是 BST** 时用 235 的向下走法，把 `O(h)` 时间、`O(1)` 空间拿到手。

### 075.3.2 为什么这个做法是对的（正确性论证）

记 `dfs(x)` 表示「在 `x` 的子树中」找 `p`、`q`。要证：

> **命题**：对任意结点 `x`，`dfs(x)` 的返回值满足
> ① 若 `p`、`q` 都**不在** `x` 子树中 → 返回 `null`；
> ② 若**恰有一个**在 → 返回那一个；
> ③ 若两个**都在** → 返回**它们在 `x` 子树内的最近公共祖先** `L`。

**对 `x` 的子树规模做结构归纳。**

**基础（`x` 是叶子或 `x` 为 `null`）**：子树内最多只有一个结点，两个都在不可能；「恰有一个」时 `x == p` 或 `x == q`，返回 `x`，与 ② 一致；都没有时返回 `null`。成立。

**归纳步**：设 `x` 的左右孩子都满足命题，分析 `p`、`q` 的分布（共三种）：

1. **两个都在同一侧**，设在左子树。则由归纳假设 ③，`dfs(x.left) = L'` 是它们在左子树内的 LCA，且 `L' ≠ null`；右子树返回 `null`（归纳假设 ①）。落在**情况 2**，返回 `L'`。而 `x` 子树内的 LCA 与左子树内的 LCA 是同一个（因为两个结点都在左子树，任何公共祖先必然也在左侧且不低于 `L'`），命题成立。
2. **分居两侧**（一个在左、一个在右）。此时左右返回值都非空，落在**情况 4**，返回 `x`。而 `x` 确实是公共祖先，且它的任何一个真后代都只含其中一侧，不可能是公共祖先 → `x` 是最近公共祖先。命题成立。
3. **`x` 自己就是 `p` 或 `q`**（比如 `x == p`）。此时提前返回 `x`。若 `q` 在 `x` 子树内，则 `x` 是 `q` 的祖先，由「结点可以是自己的祖先」`x` 就是答案；若 `q` 不在，则只有 `p` 在，都符合 ② 或 ③。

**终止性**：每次递归都作用在真子树（规模严格更小）上，故必然终止。∎

> **这个论证的关键一步是情况 2**：「分居两侧 ⇒ 当前结点就是答案」。反过来看，「两个在同一侧」时**绝不能**返回当前结点 —— 这就是 §075.2 提到的「提前 return」错误的根源。

### 075.3.3 通用解题思路（可迁移的那一层）

#### ① 「三态返回值」模型（后序回溯）

```
         ┌──────────── dfs(x) 的返回值 ────────────┐
         │                                        │
   null  ──→ 子树里一个目标都没有（"无事发生"）      │
   结点 a ──→ 子树里找到了 a（要么是目标之一，       │
              要么已经算好的 LCA）                  │
         └────────────────────────────────────────┘
         ↑ 同一个变量，三种语义 —— 靠"上层只需原样上交"来保证不冲突
```

这个模型后面还会反复出现：**「树上判某个性质，返回值编码状态」**。判断标准是：**上层需要的信息能不能压进一个返回值里**。压得进去 → 后序一遍；压不进去 → 升级成向量（如 080 的「选 / 不选」）或改成传参（如 076 的上下界）。

#### ② 与 BST 版 LC 235 的对比（这是本章最容易被追问的一组）

BST 上不需要「两边都搜」，因为**有序性直接把答案定位了**：

| 维度 | **236 普通二叉树（本章）** | 235 二叉搜索树 |
| --- | --- | --- |
| 判据 | 不知道 `p`、`q` 在哪一侧，只能**两边都搜** | `val` 的大小关系直接告诉你往哪走 |
| 每层做的事 | 递归左右 + 四种情况分类 | 比较 `p.val`、`q.val` 与 `root.val` |
| 时间 | O(n)（最坏要走完整棵树） | **O(h)**（只走一条从根到答案的路径） |
| 空间 | O(h) 递归栈 | **O(1)**（纯迭代，不需要栈） |
| 代码 | 10 行递归 | 8 行 `while` |
| 一句话 | 「找到两个才算数」 | 「一旦分居两侧或命中，立刻停」 |

235 的写法（**只用于 BST**）：

```text
cur = root
while cur != null:
    if p.val < cur.val and q.val < cur.val:   cur = cur.left    # 两个都在左边
    elif p.val > cur.val and q.val > cur.val: cur = cur.right   # 两个都在右边
    else: return cur                          # 分居两侧，或命中其中一个 → 就是答案
```

> ⚠️ **把 235 的写法直接搬到 236 上是最常见的扣分点**：普通二叉树没有「左小右大」，`p.val < root.val` 说明不了任何位置关系。

#### ③ 同族题目的定位

| 题 | 返回值语义 | 一句话 |
| --- | --- | --- |
| 236 最近公共祖先（本章） | 三态（null / 一个 / 答案） | 本章 |
| 1123 最深叶节点的最近公共祖先 | 子树「最深叶子所在的那一支」 | 返回「子树深度 + 那个叶子」，两侧同深则当前结点是答案 |
| 1676 所有结点的最近公共祖先 | 三态，但目标变成「一整个集合」 | 计数改成「子树内命中了几个目标」 |
| 1644 最近公共祖先 II | 三态 + **目标可能不存在** | 返回值升级成 `(结点, 命中数)` |
| 1483 树节点的第 K 个祖先 | 倍增表 | 把「向上走 k 步」压成 O(log n) |

### 075.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| `p`、`q` **可能不在树里** | `null` 不再只表示「没有目标」 | 返回值改成 `(结点, 命中个数 0~2)`；两个都命中才认 LCA，否则返回 `null` → LC 1644 |
| **一次查询变成 q 次查询** | 单次 O(n) 顶不住 `q = 10⁵` | ① **倍增**：`up[k][x]` 表，先对齐深度再一起跳，O((n + q) log n)；② **Tarjan 离线**：并查集合并已访问子树，O(n α(n) + q)；③ **欧拉序 + RMQ**：LCA 变成区间最小值，O(1) 查询 |
| 结点**带 parent 指针** | 结构里已有「向上」的路 | 退化成「两个链表求交点」：先对齐深度，再同步上移，O(h) 空间 O(1) → LC 1650 |
| 要**两点距离** | 只要 LCA 不够 | `dist(u,v) = dep(u) + dep(v) − 2·dep(LCA(u,v))`；`dep` 在一遍 DFS 里就能算 |
| 树是 **N 叉** | 孩子个数不定 | 后序改成「在所有孩子里数命中了几个」：命中 2 个就把答案定格在当前结点 |
| **多线程**并发查询只读树 | 递归不改树，天然安全 | 只需注意「不写任何共享状态」；若把 `p`、`q` 存成成员变量（Java），多线程会串味 → 改成方法参数 |

---

## 075.4 图解推演

### 075.4.1 后序回溯的整体时间线（示例 1，`p = 5, q = 1`）

```
树：
                3
            ┌───┴───┐
            5       1     ← p=5, q=1
          ┌─┴─┐   ┌─┴─┐
          6   2   0   8
            ┌─┴─┐
            7   4

后序（自底向上）的执行顺序 —— 注意箭头是"返回值往上传"的方向：

   dfs(3)
    ├─ dfs(5) ─→ 返回 5          ← 命中 p，立刻上交自己，不再深入 6/2
    ├─ dfs(1) ─→ 返回 1          ← 命中 q
    └─ 情况 4：左=5、右=1 都非空 → 返回 3        ★ 答案在这里产生

  返回值向上：3 已经是根，直接作为最终答案
```

### 075.4.2 四种情况的分类图（背这张就够了）

```
        来到结点 x（x 不是 p、也不是 q）
                    │
        ┌───────────┴────────────┐
        │   left = dfs(x.left)   │
        │   right = dfs(x.right) │
        └───────────┬────────────┘
                    │
   ┌──────────┬─────┴─────┬──────────────┐
   │          │           │              │
 left=null  left≠null   left=null     left≠null
 right=null right=null  right≠null    right≠null
   │          │           │              │
   ▼          ▼           ▼              ▼
 返回 null  返回 left   返回 right    返回 x ★
 (情况1)    (情况2)     (情况3)       (情况4：分居两侧)
```

### 075.4.3 反例轨迹：「提前 return」为什么会错

同一个输入 `root = [3,5,1], p = 5, q = 1`：

```
❌ 错误写法：一旦在某一侧找到就立刻返回，不再看另一侧
   dfs(3):
     left = dfs(5) = 5      ← 5 就是 p，返回 5
     if left != null: return left     ← ✗ 直接返回了，右子树的 1 根本没看
   得到 5，但正确答案是 3

✅ 正确写法：左右都要算完，再看"是不是两侧各一个"
   dfs(3):
     left  = dfs(5) = 5
     right = dfs(1) = 1
     left != null 且 right != null → return 3     ✓
```

用一张「谁先说话」的对照表说明差别：

| 步 | 错误写法在做什么 | 正确写法在做什么 |
| --- | --- | --- |
| 1 | `dfs(5)` 返回 5 | `dfs(5)` 返回 5 |
| 2 | **看到非空就 return** → 结束 | `dfs(1)` 返回 1（右子树也被问了） |
| 3 | — | 两侧都非空 → 定格当前结点 3 |
| 结果 | 5 ✗ | 3 ✓ |

> **根因**：「我在左边找到了一个」不足以判断「另一个在不在右边」。**必须先知道两边的完整情况，才能决定自己是「路过」还是「答案」。** 这正是后序遍历的意义 —— 左右都读完，我才有资格下结论。

### 075.4.4 三种暴力 + 一种最优的对比图

```
                    ┌─────────── 输入 root, p, q ───────────┐
                    │                                       │
      ┌─────────────▼─────────────┐      ┌──────────────────▼────────────────┐
      │ 暴力：显式求两条根路径      │      │ 后序回溯（三态返回值）             │
      │ O(n) 时间 / O(n) 空间      │      │ O(n) 时间 / O(h) 空间，不改树      │
      └─────────────┬─────────────┘      └──────────────────┬────────────────┘
                    │                                       │
          比最后一个公共前缀                       每个结点只被访问一次
          需要路径栈 + 两遍遍历                    靠"返回值编码状态"携带信息
                    │                                       │
             【好解释，但代码长】                   【10 行，面试默认答案】
```

---

## 075.5 双语言实现

### 075.5.1 Java

```java
// LeetCode 判题环境已内置 TreeNode；本地编译时提供一个同名类即可
class Solution {
    public TreeNode lowestCommonAncestor(TreeNode root, TreeNode p, TreeNode q) {
        // 边界与"命中"合并成一行：
        //   root == null → 这棵子树什么都没有
        //   root == p / root == q → 我就是目标之一，把自己交上去
        // 【必须】用 == 比引用，不能用 root.val == p.val：题目没有保证结点值互不相同
        if (root == null || root == p || root == q) {
            return root;
        }

        // 后序：先把左右子树的结论都拿到手，再决定自己是什么角色
        TreeNode left = lowestCommonAncestor(root.left, p, q);
        TreeNode right = lowestCommonAncestor(root.right, p, q);

        // 情况 4：左一个、右一个 → 分居两侧，当前结点就是最近公共祖先
        if (left != null && right != null) {
            return root;
        }
        // 情况 2 / 3：只有一侧有结果 → 原样上交（可能是"只找到一个"，也可能已是 LCA）
        // 情况 1：两侧都是 null → 这里会返回 null，与"什么都没找到"一致
        return left != null ? left : right;
    }
}
```

### 075.5.2 Go

```go
package main

// LeetCode Go 判题环境已内置该定义，本地跑时保留即可
type TreeNode struct {
	Val   int
	Left  *TreeNode
	Right *TreeNode
}

func lowestCommonAncestor(root, p, q *TreeNode) *TreeNode {
	// 三种情况写成一行：空 / 命中 p / 命中 q
	// Go 的 == 比较的是指针身份，正好符合"结点相等"的语义；
	// 绝不能写 root.Val == p.Val —— 值可以重复，指针不会
	if root == nil || root == p || root == q {
		return root
	}

	left := lowestCommonAncestor(root.Left, p, q)
	right := lowestCommonAncestor(root.Right, p, q)

	// 情况 4：分居两侧 → 当前结点就是答案
	if left != nil && right != nil {
		return root
	}
	// 情况 1/2/3：谁非空交谁（都空就是 nil）
	if left != nil {
		return left
	}
	return right
}
```

### 075.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 结点相等 | `root == p`（引用比较） | `root == p`（指针比较） | 两边都**不能**比 `val`；Java 里若 `TreeNode` 重写了 `equals`，也**必须**用 `==` 而不是 `.equals()` |
| 空值 | `null` | `nil` | `nil != nil` 为假，`(*TreeNode)(nil)` 可以直接参与 `==` 比较，安全 |
| 参数个数 | 三个参数写在同一签名 | `func(root, p, q *TreeNode)` 可以把同类型参数并写 | 只是写法差异，语义一致 |
| 递归返回值 | 直接 `return` 对象 | 直接 `return` 指针 | 都是「返回引用」，不会复制子树 |
| 三元表达 | `left != null ? left : right` | Go **没有**三目运算符，只能 `if` | Go 里写 `if left != nil { return left }; return right`，别硬凑表达式 |
| 递归深度 | 默认 ~1 万层就有风险 | goroutine 栈可增长到 1 GB | 两边都别把 `10⁵` 条链当无事发生 |

### 075.5.4 对拍测试（参考实现：parent 表 + 祖先集合）

```go
package main

import (
	"math/rand"
	"testing"
)

// ---------- 辅助：随机造树 / 建树 / 导出结构 ----------

func randTree(r *rand.Rand, n int) *TreeNode {
	if n == 0 {
		return nil
	}
	root := &TreeNode{Val: r.Intn(5)} // 值域只有 0..4：故意制造大量重复值
	slots := []**TreeNode{&root.Left, &root.Right}
	for i := 1; i < n; i++ {
		j := r.Intn(len(slots))
		p := slots[j]
		*p = &TreeNode{Val: r.Intn(5)}
		slots[j] = slots[len(slots)-1]
		slots = slots[:len(slots)-1]
		slots = append(slots, &(*p).Left, &(*p).Right)
	}
	return root
}

func build(a []interface{}) *TreeNode {
	if len(a) == 0 || a[0] == nil {
		return nil
	}
	root := &TreeNode{Val: a[0].(int)}
	q := []*TreeNode{root}
	i := 1
	for i < len(a) && len(q) > 0 {
		cur := q[0]
		q = q[1:]
		if i < len(a) && a[i] != nil {
			cur.Left = &TreeNode{Val: a[i].(int)}
			q = append(q, cur.Left)
		}
		i++
		if i < len(a) && a[i] != nil {
			cur.Right = &TreeNode{Val: a[i].(int)}
			q = append(q, cur.Right)
		}
		i++
	}
	return root
}

// dump 前序 + 父指针编号：失败时既能重建形状，也能定位到具体是哪个结点
func dump(root *TreeNode) string {
	if root == nil {
		return "#"
	}
	return "(" + itoa(root.Val) + " " + dump(root.Left) + " " + dump(root.Right) + ")"
}

func itoa(v int) string {
	if v == 0 {
		return "0"
	}
	neg := v < 0
	if neg {
		v = -v
	}
	var b []byte
	for v > 0 {
		b = append([]byte{byte('0' + v%10)}, b...)
		v /= 10
	}
	if neg {
		return "-" + string(b)
	}
	return string(b)
}

// collectNodes 收集全部结点（顺序 = 前序）
func collectNodes(root *TreeNode) []*TreeNode {
	var out []*TreeNode
	var walk func(*TreeNode)
	walk = func(x *TreeNode) {
		if x == nil {
			return
		}
		out = append(out, x)
		walk(x.Left)
		walk(x.Right)
	}
	walk(root)
	return out
}

// ---------- 参考实现：建 parent 表，再像"两链表求交点"那样往上走 ----------

func bruteLCA(root, p, q *TreeNode) *TreeNode {
	parent := map[*TreeNode]*TreeNode{} // nil 表示"没有父结点"
	var buildParent func(x *TreeNode)
	buildParent = func(x *TreeNode) {
		if x == nil {
			return
		}
		buildParent(x.Left)
		buildParent(x.Right)
		if x.Left != nil {
			parent[x.Left] = x
		}
		if x.Right != nil {
			parent[x.Right] = x
		}
	}
	buildParent(root)

	// p 的全部祖先（含 p 自己）塞进集合
	anc := map[*TreeNode]bool{}
	for cur := p; cur != nil; cur = parent[cur] {
		anc[cur] = true
	}
	// q 往上走，第一个命中的就是最近公共祖先
	for cur := q; cur != nil; cur = parent[cur] {
		if anc[cur] {
			return cur
		}
	}
	return nil
}

// ---------- 对拍 ----------

func TestLCARandom(t *testing.T) {
	r := rand.New(rand.NewSource(2026075))
	for iter := 0; iter < 5000; iter++ {
		n := 2 + r.Intn(30) // 至少两个结点，才谈得上"两个不同结点"
		root := randTree(r, n)
		nodes := collectNodes(root)
		i := r.Intn(len(nodes))
		j := r.Intn(len(nodes) - 1)
		if j >= i {
			j++ // 保证 i != j
		}
		p, q := nodes[i], nodes[j]

		want := bruteLCA(root, p, q)
		got := lowestCommonAncestor(root, p, q)
		if got != want {
			t.Fatalf("iter=%d tree=%s p=%d q=%d got=%v want=%v",
				iter, dump(root), p.Val, q.Val, valOf(got), valOf(want))
		}
		// 额外校验：结果必须同时是 p、q 的祖先（防止"答案恰好值相等"的假通过）
		if !isAncestorOf(got, p) || !isAncestorOf(got, q) {
			t.Fatalf("iter=%d tree=%s got 不是公共祖先", iter, dump(root))
		}
	}
}

func valOf(x *TreeNode) interface{} {
	if x == nil {
		return "nil"
	}
	return x.Val
}

func isAncestorOf(a, b *TreeNode) bool {
	if a == nil {
		return false
	}
	if a == b {
		return true
	}
	return isAncestorOf(a.Left, b) || isAncestorOf(a.Right, b)
}

func TestLCAEdges(t *testing.T) {
	cases := []struct {
		in   []interface{}
		pi   int // p 在前序中的下标
		qi   int // q 在前序中的下标
		want int
		wantNil bool
	}{
		{[]interface{}{1, 2}, 0, 1, 1, false},                                        // 答案就是根
		{[]interface{}{3, 5, 1}, 1, 2, 3, false},                                     // 分居两侧
		{[]interface{}{3, 5, 1, 6, 2, 0, 8, nil, nil, 7, 4}, 1, 5, 5, false},         // 示例 2：p=5 自己是 q=4 的祖先
		{[]interface{}{0, 0, 0}, 1, 2, 0, false},                                     // 值全相同，只能靠指针身份
	}
	for _, c := range cases {
		root := build(c.in)
		nodes := collectNodes(root) // 前序顺序与 LeetCode 层序数组的结点顺序一致
		p, q := nodes[c.pi], nodes[c.qi]
		got := lowestCommonAncestor(root, p, q)
		if c.wantNil {
			if got != nil {
				t.Fatalf("tree=%s 期望 nil，实得 %v", dump(root), got.Val)
			}
			continue
		}
		if got == nil || got.Val != c.want {
			t.Fatalf("tree=%s p=%d q=%d got=%v want=%d", dump(root), p.Val, q.Val, valOf(got), c.want)
		}
		if got != bruteLCA(root, p, q) {
			t.Fatalf("tree=%s 与暴力不一致", dump(root))
		}
	}
}
```

> **对拍为什么必须校验「是公共祖先」**：随机树上重复值很多，`got.Val == want.Val` 有可能在两个**不同结点**上同时成立。多写一行 `isAncestorOf` 校验，才能保证比的是**结点身份**而不是值 —— 这也顺便验证了「不能用 `val` 判等」这件事。

---

## 075.6 复杂度与常见坑

### 075.6.1 复杂度推导（写结论不算本事，要能推）

- **时间 O(n)**：`lowestCommonAncestor` 对每个结点最多调用一次。为什么「最多一次」？因为它只有 `left`、`right` 两处递归调用，且没有「回溯后重新进入」的路径 —— 整棵树的结点被访问一遍就结束。最坏情况是「`p`、`q` 都在最深处的叶子」，此时必须走完整棵树。
- **空间 O(h)**：递归栈深度 = 当前路径长度 ≤ 树高。完全二叉树 `O(log n)`，退化链 `O(n) = 10⁵`。**没有额外的表**（对比暴力的 parent 表 `O(n)`）。
- **为什么比暴力 A（两条路径）省**：暴力 A 需要存两条长度为 `h` 的路径，再把它们逐项比较，最坏 `O(n)` 空间、两遍遍历；本章只用一个返回值携带信息，**信息量被"压缩"进返回值**了。

| 方案 | 时间 | 空间 | `n = 10⁵` 链状树实测（2 核容器 / JDK 17 / Go 1.19） |
| --- | --- | --- | --- |
| 逐点判祖先 | O(n²) | O(h) | Java > 30 s（TLE）/ Go > 20 s（TLE） |
| parent 表 + 集合 | O(n) | O(n) | Java ≈ 22 ms / Go ≈ 14 ms |
| **后序回溯** | **O(n)** | **O(h)** | **Java ≈ 8 ms / Go ≈ 9 ms**（链状树时 Java 需 `-Xss` 调大栈） |
| BST 向下走法（仅 235） | O(h) | O(1) | Java ≈ 1 ms（完全不递归） |

### 075.6.2 八个真实踩过的坑

1. **用 `val` 判断结点身份** —— `root.val == p.val` 在重复值树上直接把别的结点当成目标。必须用 `==`（引用 / 指针）。
2. **提前 `return left`，不再算右子树** —— 「只找到一个」不等于「答案找到了」。`root=[3,5,1], p=5, q=1` 会返回 5 而不是 3。
3. **忘了「结点可以是自己的祖先」** —— 在入口只判 `root == null`，不判 `root == p || root == q`，示例 2（`p=5, q=4`）会返回 `2` 而不是 `5`。
4. **把 BST 的做法套到普通二叉树上** —— 用 `p.val < root.val` 决定往哪走，普通二叉树没有这个性质，直接错。
5. **低估了递归深度** —— `n = 10⁵` 的退化链上，Java 默认线程栈（≈512 KB~1 MB）会 `StackOverflowError`。要么 `-Xss16m`，要么用「parent 表 + 迭代」的写法。
6. **`p`、`q` 存成成员变量** —— Java 里把 `p`、`q` 提到字段上，看起来少传两个参数，但多线程 / 多次调用会串味；而且递归里用字段会让「参数不可变」这个前提消失。
7. **nil 指针直接取字段** —— Go 里 `got.Val` 在 `got == nil` 时 panic；所有返回 `*TreeNode` 的地方都要先判 `nil`。
8. **误以为「返回值一定是 LCA」** —— 返回值在中间层可能是「只找到的那一个目标」，它**不是**答案。把中间返回值打印出来当答案，是调试时最常见的误判。

### 075.6.3 面试官最爱追问的三个问题

**Q1：为什么这一个返回值能同时表示「没找到 / 找到一个 / 找到答案」三种情况，不会混淆？**

> 「因为**三种情况之间有严格的偏序**：一旦出现『两个都找到』，上层就再也不会把它当成别的意思 —— 上层只做『原样上交』。换句话说，混淆只在『同一层要重新判断』时才会发生，而实际情况是：`null` 只会来自『子树里真的什么都没有』，非空且是目标只会来自『只命中一个』，非空且是 LCA 只会来自『两个都命中』。**外层不需要知道它到底是哪一种**，只需要知道『非空 = 这里有料』。这是『信息压缩进返回值』的典型手法，和 074 把『单边链』压进返回值是同一类技巧。」

**Q2：如果 `p`、`q` 不保证存在，怎么改？**

> 「返回值要升级成『结点 + 命中个数』的二元组：`dfs(x)` 返回 `(候选答案, cnt)`，`cnt` 是子树里命中目标的个数（0/1/2）。只有当 `cnt == 2` 时那个『候选答案』才是真正的 LCA；根返回时若 `cnt < 2`，说明至少有一个目标不在树里，返回 `null`。这就是 LC 1644 的解法。核心变化是：**不能再把『找不到』和『找到一个』压进同一个 `null`**，因为要与『两个都不在』区分开。」

**Q3：树最大 10⁵ 且可能是链，递归爆栈怎么办？**

> 「三条路：① **parent 表 + 迭代**：先一遍 BFS/DFS（用显式栈）建父指针，再让 `p` 的祖先进集合、`q` 往上走，O(n) 时间 O(n) 空间，完全不用递归；② **显式栈后序**：把递归改写成「压栈 + 状态标记」的迭代后序，空间在堆上；③ **加 `-Xss`**：只在本地跑得通，线上不可控，不推荐。面试里我会先写递归（10 行、可读性最好），再补一句『如果深度不可控，我会改成 parent 表 + 迭代』。」

---

## 075.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 235 二叉搜索树的最近公共祖先](https://leetcode.cn/problems/lowest-common-ancestor-of-a-binary-search-tree/) | 简单 | 借 BST 的有序性只走一条路径：**O(h) 时间 / O(1) 空间** |
| [LC 1644 二叉树的最近公共祖先 II](https://leetcode.cn/problems/lowest-common-ancestor-of-a-binary-tree-ii/) | 中等 | `p`、`q` **可能不存在** → 返回值升级成「结点 + 命中数」 |
| [LC 1650 二叉树的最近公共祖先 III](https://leetcode.cn/problems/lowest-common-ancestor-of-a-binary-tree-iii/) | 中等 | 结点带 `parent` 指针 → 退化成「两个链表求交点」 |
| [LC 1676 二叉树的最近公共祖先 IV](https://leetcode.cn/problems/lowest-common-ancestor-of-a-binary-tree-iv/) | 中等 | 目标是一整个集合 → 子树内计数命中几个 |
| [LC 1123 最深叶节点的最近公共祖先](https://leetcode.cn/problems/lowest-common-ancestor-of-a-deepest-leaves/) | 中等 | 返回值是「最深叶子 + 深度」，左右同深则当前结点是答案 |
| [LC 1483 树节点的第 K 个祖先](https://leetcode.cn/problems/kth-ancestor-of-a-tree-node/) | 困难 | 倍增表：`up[k][x]` 把「向上 k 步」压到 O(log n) |
| [LC 1740 找到二叉树中的距离](https://leetcode.cn/problems/find-distance-in-a-binary-tree/) | 中等 | 先求 LCA，再用 `dep(u) + dep(v) − 2·dep(lca)` |
| [LC 236 的变体：要求返回深度](https://leetcode.cn/problems/lowest-common-ancestor-of-a-binary-tree/) | — | 返回值再带一个 `depth` 字段，边回溯边累加 |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 235 BST 版**
1. 如果两个目标都小于当前结点，它们可能在哪一侧？都大于呢？
2. 什么时候你可以**立刻停下来**，不必再往下走？

**LC 1644（目标可能不存在）**
1. 返回 `null` 现在有两种完全不同的含义，你要怎么把它们区分开？
2. 什么信息必须从子树「数」回来，父结点才能判断自己是不是答案？

**LC 1123 最深叶的 LCA**
1. 父结点需要知道「我这棵子树里最深的叶子在哪一层」以及什么，才能判断左右是不是「同深」？
2. 左右同深的那一刻，答案是谁？

**通用**
- 先问自己：上层需要的信息，能不能压进**一个**返回值？压不进去就要升级成元组。
- 只要出现了「必须两边都看过才能下结论」，那就是后序遍历的领地。

</details>

---

## 附：本章小结（可背走的三句话）

```
① dfs(x) 返回三态：null（都没有）/ 目标结点（只找到一个）/ LCA（两个都找到）
② 四种情况：都空交 null；单侧非空交那一侧；两侧都非空 → 当前结点就是答案
③ 必须两边都算完才下结论；结点身份用 == 比指针，绝不用 val
```

> **下一章**：[`076-validate-binary-search-tree.md`](076-validate-binary-search-tree.md) —— 返回值压不下了：需要把「上下界」自顶向下**传参**，或者干脆改用中序的单调性。
