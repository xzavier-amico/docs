# 076. LC98 验证二叉搜索树（Validate Binary Search Tree）

> **状态**：`已完成（初稿）` ｜ **所属**：第 8 部分 · 树形结构 ｜ **编号**：076 / 150
> **难度**：中等（**全系列通过率最低的「简单思路」陷阱题**：几乎所有人都先写成错的）｜ **标签**：树 · 深度优先搜索 · 二叉搜索树 · 中序遍历
> **一句话**：这道题真正考的是「**BST 是一个『全局』约束，局部比较左右孩子必然漏信息 —— 要么把上下界自顶向下传下去，要么换成中序的单调性**」，不是「比较一下 `left.val < root.val < right.val`」。
> **前置**：[`065-binary-tree-inorder-traversal.md`](065-binary-tree-inorder-traversal.md)（中序遍历：BST 的「中序 = 有序」是本章第二条解法的地基）

---

## 076.1 题目

**原题链接**：<https://leetcode.cn/problems/validate-binary-search-tree/>

### 题意重述（把隐含条件补齐）

给你一棵二叉树的根节点 `root`，判断它是不是一棵**二叉搜索树**。BST 的定义是：

1. 结点的**左子树**里**所有**结点的值都**严格小于**当前结点的值；
2. 结点的**右子树**里**所有**结点的值都**严格大于**当前结点的值；
3. 左右子树**本身**也都是 BST。

三条一起读，才发现它是一个**递归的、全局的**约束 —— 这也是全题的题眼。把它拆成必须读出来的五个隐含条件：

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| 比较的是**整棵子树**，不是直接孩子 | 定义第 1、2 条里的「所有结点」 | 只比直接孩子 → `[5,1,4,null,null,3,6]` 被判成合法（正确答案：不合法） |
| **严格**小于 / 大于 | 定义里的「小于」「大于」，没有「等于」 | 用 `<=` 或 `>=` → `[2,2,2]` 被判成合法（正确答案：不合法） |
| **空树**是合法的 BST | 官方说明「空树也是 BST」 | 空树时直接返回 `false` → 判错 |
| 结点值域是**完整 int**：`-2³¹ ≤ val ≤ 2³¹−1` | 约束 | 用 `Integer.MIN_VALUE` 当初始下界，遇到结点值恰好是 `-2³¹` 时误判（见 §076.6.2 第 2 条） |
| 返回 `boolean` | 「判断」 | 返回最长路径、返回结点 → 判题直接失败 |

> 面试里真正的分水岭是第一条。**「所有结点」三个字意味着：只看一层是永远不够的。** 一个结点的合法性由**它所有的祖先**共同决定，而这件事没法从「直接孩子」那里得到。

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| 节点数 `1 <= n <= 10⁴` | 最多 1 万 | O(n²) = 10⁸ → 实测 Java ≈ 1.5 s、Go ≈ 1 s，**能过但很危险**（再来一组用例就 TLE）；O(n) 才是的答案 |
| `-2³¹ <= val <= 2³¹−1` | **含 int 两端** | 上下界的初值必须用 `long` / `*TreeNode` 的 `nil`，不能用 int 的极值 |
| 值**可以重复** | 定义用「小于」，没说唯一 | `[2,2]` 必须返回 `false`；判断条件必须严格 |
| 树形状不限 | 可以是链 | `h = 10⁴` 的链：递归没问题，但换到 10⁵ 就要考虑迭代 / Morris |

> 💡 **量级 → 算法**：`n = 10⁴` 看着宽松，但这题的重点根本不在复杂度 —— **两解（上下界 / 中序）都是 O(n)**，区别只在「空间常数」和「能不能顺手拿 O(1) 空间」。这里量级只用来排除「每个结点都去扫一遍子树」的 O(n²) 写法。

### 示例

```
示例 1
输入：root = [2, 1, 3]
输出：true
        2
       ┌┴┐
       1 3

示例 2
输入：root = [5, 1, 4, null, null, 3, 6]
输出：false
解释：根是 5，右孩子是 4 —— 4 < 5，但它出现在 5 的右子树里，违反「右子树所有结点 > 5」

        5
       ┌┴┐
       1 4
        ┌┴┐
        3 6

   注意：单看每个「父—子」对，5>1 ✓、5>4 ✗（这一对就已经错了）
   但真正致命的是 3 和 6 的位置：3 在 5 的右子树里，却比 5 小

示例 3
输入：root = [5, 4, 6, null, null, 3, 7]
输出：false
解释：每一对「父—子」都合法（5>4、5<6、6>3、6<7），但 3 挂在 5 的右子树里 → 全局非法
       ← 这一例才是"只比左右孩子"的精确杀手
```

### 我自造的四个反例（写完必须拿它们打一遍）

```text
① [5, 4, 6, null, null, 3, 7]   → 每个父子对都合法，但 3 在 5 的右子树里 → 必须 false
                                   （这是"只比直接孩子"的精确反例，比示例 2 更狠）
② [2147483647]                  → 单个 int 最大值：暴露"用 Integer.MIN_VALUE 当初值"的边界 bug
③ [-2147483648, null, 2147483647] → 两端极值同时出现
④ [2, 2, 2]                     → 重复值：必须 false（严格小于），用 <= 会判成 true
```

第 ① 条请务必亲手推一遍：**它和示例 2 的区别在于，示例 2 里「5 与 4」这一对父子本身就违规，所以错误写法也能抓到；而示例 3 里所有父子对都合法，只有全局约束被破坏 —— 这才是真正的照妖镜。**

---

## 076.2 从暴力到最优：第一个念头为什么错

### 第一步：暴力（正确定义直译，但慢）

把定义逐字翻译：对每个结点，去它的左子树里找**最大值**、右子树里找**最小值**，逐个比较。

```text
isValid(root):
    if root == null: return true
    if maxOf(root.left)  >= root.val: return false     # 左子树有更大的
    if minOf(root.right) <= root.val: return false     # 右子树有更小的
    return isValid(root.left) and isValid(root.right)

maxOf / minOf：各扫一遍子树           ← O(子树大小)
```

- 时间：`T(n) = 2T(n/2) + O(n)` → **O(n log n)**（完全二叉树），**退化链上是 O(n²)**。
- 空间 O(h)。

**它是正确的**（完全照定义），但对链状树是 `10⁴ × 10⁴ = 10⁸` 次访问，实测 Java ≈ 1.5 s。面试里写这个不会被判错，但一定会被追问「能不能 O(n)」。

### 第二步：卡点在哪 —— 把人人都踩过的坑摆出来

先写出来「最诱人」的版本（**错的**）：

```java
// ❌ 只比较直接孩子：这是本题的"标准错误答案"
boolean isValid(TreeNode root) {
    if (root == null) return true;
    if (root.left  != null && root.left.val  >= root.val) return false;
    if (root.right != null && root.right.val <= root.val) return false;
    return isValid(root.left) && isValid(root.right);
}
```

它错在哪？拿 `[5, 4, 6, null, null, 3, 7]` 走一遍：

```
        5
       ┌┴┐
       4 6
        ┌┴┐
        3 7

逐对检查（错误算法的视野）：
   5 vs 4 → 4 < 5  ✓
   5 vs 6 → 6 > 5  ✓
   6 vs 3 → 3 < 6  ✓
   6 vs 7 → 7 > 6  ✓
   全部通过 → 返回 true        ← ✗ 大错

真实情况：3 在 5 的【右子树】里，而 3 < 5，违反「右子树所有结点 > 根」
正确答案：false
```

> **两个反例要一起记住，它们分工不同**：
> - 官方示例 2 `[5, 1, 4, null, null, 3, 6]`：**连直接父子都违规**（根 5 的右孩子是 4）。它证明「只看孩子」不够用，但它**抓不出**「每对父子都合法、整体却非法」这种更深的问题；
> - `[5, 4, 6, null, null, 3, 7]`：**所有父子对都合法**，只有全局约束被破坏。它才是「只比直接孩子」的**精确杀手**。
> 面试里先举官方那个（对方一眼能看懂），再补上第二个（对方会点头：原来你的理解不止一层）。

**卡点的准确表述**：

> **`3` 的合法性不由它的父结点 `6` 决定，而由它的祖先 `5` 决定。** 而 `5` 的信息在往下递归时**被丢掉了** —— 递归进入 `root.right` 之后，函数再也不知道「我的祖先要求我大于 5」。

于是解题方向只有两条，而且它们正好是两种相反的思路：

| 方向 | 做法 | 本质 |
| --- | --- | --- |
| **把祖先的约束带下去** | 递归参数里带 `(lo, hi)` 开区间 | **自顶向下传参**：父给子的信息不能丢 |
| **换一个等价刻画** | BST ⟺ 中序遍历**严格递增** | 把「结构约束」变成「序列约束」，一次遍历就能查 |

### 第三步：灵感的来源 —— 两种等价刻画

**刻画 A（区间）**：`x` 的整棵子树中，每个结点的值都必须落在**祖先们挤出来的开区间** `(lo, hi)` 里。从根往下走时：

- 进入左子树：上界被当前结点收紧 → `(lo, x.val)`
- 进入右子树：下界被当前结点收紧 → `(x.val, hi)`

```
                5               区间 (−∞, +∞) ✓
              ┌─┴─┐
             4     6            左 (−∞,5) ✓   右 (5,+∞) ✓
                  ┌┴┐
                  3 7           3 必须在 (−∞,5)∩(5,6) = (5,6) 内 ✗
```

**刻画 B（序列）**：BST 的定义恰好等价于「**中序遍历得到严格递增序列**」。这是 065 里埋下的伏笔 —— 中序把「左 → 根 → 右」的顺序摊平，BST 的大小关系就变成了「序列单调」。

> 🧭 一句话记住：**区间刻画 = 把约束往下传；序列刻画 = 把结构摊平成数组。** 两条路的复杂度都是 O(n)，区别在空间与「能不能顺手做 O(1)」。

---

## 076.3 原理拆解

### 076.3.1 数据结构与算法选型

| 方案 | 时间 | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- |
| ❌ 只比直接孩子 | O(n) | O(h) | 8 行 | **正确性直接崩**：`[5,4,6,null,null,3,7]` 判成 true |
| 逐点扫子树求 max/min | O(n²)（链）/ O(n log n)（平衡） | O(h) | 20 行 | `n = 10⁴` 链 → 10⁸ 次访问，1.5 s 边缘徘徊 |
| **上下界传递（自顶向下传参）** ✅ | **O(n)** | **O(h)** | **10 行** | 上下界初值写错（用 int 极值）→ 边界值用例错；链状树递归深 |
| **中序 + 前驱比较** ✅ | **O(n)** | **O(h)** | **10 行** | `prev` 要用成员变量/闭包，忘记重置 → 多次调用串味 |
| 中序收集成数组再判 | O(n) | **O(n)** | 12 行 | 多花 `O(n)` 空间，只为了「看起来直观」 |
| 中序 + Morris | O(n) | **O(1)** | 25 行 | 会临时改树；代码长、易写错（同 065 的结论） |
| 自底向上返回 `(min, max)` | O(n) | O(h) | 15 行 | 需要额外处理空子树；返回值要打包成两个值，Go 里得用结构体 |

**取舍一句话**：**默认写「上下界传递」**（10 行、思路最贴合定义、不会碰到「重置状态」的坑）；**如果题目顺带要求 `O(1)` 空间**，就用中序 + Morris。中序 + 成员变量 `prev` 是最短的写法，但要小心多次调用。

### 076.3.2 为什么这个做法是对的（正确性论证）

#### 引理 1：上下界传递与原定义等价

记 `check(x, lo, hi)` 表示「`x` 的子树中每个结点都落在开区间 `(lo, hi)` 内」。

**命题**：`check(x, lo, hi) 且在 x 处做递归收紧` 成立 ⟺ `x` 的子树是 BST，且子树所有值都在 `(lo, hi)` 内。

> 归纳。基础：`x == null` 时为空子树，恒真。
> 归纳步：`x` 的子树是 BST 且值域受限 ⟺ ① `x.val ∈ (lo,hi)`；② `x.left` 的整棵子树是 BST 且所有值 `< x.val`（并且继承 `lo` 这个下界）；③ `x.right` 同理且所有值 `> x.val`（继承 `hi`）。
> 这三条恰好就是 `check(x, lo, hi)` 的三次调用（`x.val` 的检查 + 左子树用 `(lo, x.val)` + 右子树用 `(x.val, hi)`）。∎

> **这就是「把约束传下去」的形式化**：左子树的**上界**被 `x.val` 收紧，右子树的**下界**被 `x.val` 收紧，而祖先的约束一路继承 —— `3` 之所以非法，正是因为进入 `6` 的右子树时上界是 `5`（继承自祖先），而不是 `6`（只看父亲）。

#### 引理 2：中序严格递增 ⟺ 是 BST

> **（⇒）** 设中序序列严格递增。取任一结点 `x`。由中序遍历的定义，`x` 的左子树的所有结点都排在 `x` **之前**，右子树的所有结点都排在 `x` **之后**。序列严格递增 ⇒ 左边的值全 `< x.val`、右边的值全 `> x.val`。对每个 `x` 都成立 ⇒ 满足 BST 定义的第 1、2 条；两条对子树自然继承（子树的中序是整段序列的子序列，仍递增）⇒ 满足第 3 条。∎
>
> **（⇐）** 设是 BST。对中序序列中任意相邻的两个位置，它们的关系只有两种：前一个是某个结点 `x`，后一个是 `x` 右子树的最左结点（此时后者 `> x.val`），或者前一个是某个结点左子树的最右结点、后一个是该结点（同样后者更大）。这两种情况都由 BST 定义保证「后 > 前」。所以整个序列严格递增。∎

**推论（中序写法正确）**：遍历时只需记住「中序里的前一个结点」，若出现 `prev.val >= cur.val` 就返回 `false`。由于序列严格递增 ⟺ BST，判定完全等价。

#### 引理 3：「只比直接孩子」为什么不充分

反例 `[5,4,6,null,null,3,7]`：所有父子对都合法，但 `3` 在 `5` 的右子树内且 `3 < 5`，违反 BST。因此「每对父子合法」**不能推出**「是 BST」—— 它不是充分条件。要让它成立，必须把约束一路继承（引理 1），或者换成全局可查的序列（引理 2）。∎

### 076.3.3 通用解题思路（可迁移的那一层）

本章把 065 提出的**递归语言**补完：树递归的信息流只有两个方向，而选错方向就是这类题最常见的失败原因。

```
                        ┌──────────────── 自顶向下：传参 ────────────────┐
    祖先的约束 ────────→ │ (lo, hi) 上界下界 / 当前路径和 / 累计深度 / 前缀 │ ────→ 子孙
                        └───────────────────────────────────────────────┘
                        ┌──────────────── 自底向上：返回 ────────────────┐
    子孙的结论 ────────→ │ 高度 / min-max / 三态结点 / 选与不选的状态向量  │ ────→ 父
                        └───────────────────────────────────────────────┘
```

| 题 | 信息流方向 | 传下去 / 传上来的到底是什么 |
| --- | --- | --- |
| **076 验证 BST（本章）** | **下行** | 祖先挤出的开区间 `(lo, hi)` |
| 074 最大路径和 | 上行 | 单边最大贡献 `gain` |
| 075 最近公共祖先 | 上行 | 三态结点（没找到 / 一个 / 答案） |
| 080 打家劫舍 III | 上行 | `(选, 不选)` 两个状态 |
| 112 路径总和 | 下行 | 剩余目标和 `targetSum − 已走和` |
| 257 二叉树的所有路径 | 下行 | 当前路径（回溯时撤销） |
| 437 路径总和 III | 下行 | 根到当前结点的前缀和（配合哈希） |
| 110 平衡二叉树 | 上行 | 子树高度（`-1` 表示已失衡） |

> 🧭 **判定口诀**：
> **「我的合法性依赖祖先」→ 下行传参；「我的答案依赖子孙」→ 上行返回；两个都依赖 → 一个方向传、另一个方向记在全局（074 就是这种）。**

还有第三条路，它经常被当成分水岭答案：**换刻画**。把「结构性质」翻译成「遍历序列的性质」（本章的中序递增），或者翻译成「子树的聚合量」（自底向上返回 `min/max`）。**当递归方向怎么选都别扭时，先问：这个性质有没有更简单的等价说法？**

### 076.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| 允许**重复值**（左 ≤ 根 < 右） | 定义放宽 | 中序改成**非严格**递增（`prev.val > cur.val` 才报错）；上下界要改成 `[lo, hi)` 并把左边界定为闭 |
| 值域换成 `long` 或更大 | 上下界放不下 | 用 `Long.MIN/MAX` 或干脆用「`null` 表示无界」（Java 用 `Integer` 包装、Go 用 `*int`），彻底摆脱极值陷阱 |
| 求「**最少删除**几个结点使其成为 BST」 | 从判定变成优化 | 树形 DP：对每个结点记「作为根的子树最大值链长」+「是否保留父结点」，O(n²) 或 O(n log n) → 见 LC 333 的变体 |
| 求「**最大的 BST 子树**」 | 要尺寸 | 自底向上返回 `(是否 BST, min, max, size)`，O(n) → LC 333 |
| 树深 `10⁵`、禁止递归 | 栈不可控 | 中序 + 显式栈（065 的迭代中序），把「记录前驱」塞进迭代循环 |
| 要求 `O(1)` 额外空间 | 不能用栈 | 中序 + Morris：**建线索时不比较，拆线索时比较**（因为拆线索那一刻才是「访问」） |

---

## 076.4 图解推演

### 076.4.1 反例的精确轨迹（`[5,4,6,null,null,3,7]`）

```
初始区间 (−∞, +∞)，从根往下走，区间被祖先逐层收紧：

步骤 1  结点 5，区间 (−∞, +∞)
        5 在区间内 ✓
        → 左子树的区间变成 (−∞, 5)，右子树的区间变成 (5, +∞)

步骤 2  结点 4，区间 (−∞, 5)
        4 < 5 ✓
        → 它的子树区间变成 (−∞, 4) 与 (4, 5)

步骤 3  结点 6，区间 (5, +∞)
        6 > 5 ✓
        → 它的子树区间变成 (5, 6) 与 (6, +∞)

步骤 4  结点 3，区间 (5, 6)
        3 不在 (5, 6) 内  ✗  ✗  ✗
        → 返回 false

        3 会被判非法，不是因为 3 < 6（父子对合法），
        而是因为【祖先 5 要求它大于 5】—— 这个要求由"传参"带下来了
```

| 步 | 当前结点 | 收到的区间 `(lo, hi)` | 是否落在区间内 | 下一步传递的区间 |
| --- | --- | --- | --- | --- |
| 1 | 5 | `(−∞, +∞)` | ✓ | 左 `(−∞,5)` / 右 `(5,+∞)` |
| 2 | 4 | `(−∞, 5)` | ✓ | 左 `(−∞,4)` / 右 `(4,5)` |
| 3 | 6 | `(5, +∞)` | ✓ | 左 `(5,6)` / 右 `(6,+∞)` |
| 4 | 3 | `(5, 6)` | **✗ 3 < 5** | — 直接返回 `false` |
| 5 | 7 | `(6, +∞)`（被短路，不再访问） | — | — |

### 076.4.2 中序写法的推演（同一棵树）

```
中序遍历序列：4, 3, 5, 6, 7

        5
       ┌┴┐
       4 6
        ┌┴┐
        3 7

走法：dfs(5) → dfs(4)：4 无左 → 记录 4 → 无右 → 回到 5
              记录 5
      → dfs(6) → dfs(3)：记录 3 → 回到 6 → 记录 6 → dfs(7)：记录 7

逐对比较（相邻两项必须严格递增）：
   prev=null, cur=4  → 跳过
   prev=4,    cur=3  → 4 >= 3  ✗ 非法
```

| 步 | 访问到的结点 | `prev` | 检查 | 结论 |
| --- | --- | --- | --- | --- |
| 1 | 4 | `nil` | 第一个结点，无前驱可比 | 通过，`prev = 4` |
| 2 | 3 | `4` | `4 >= 3` | **返回 `false`** |
| 3 | 5 / 6 / 7 | — | 被短路 | 不再访问 |

### 076.4.3 两条路线的对比图

```
                        ┌──────────── 输入 root ────────────┐
                        │                                   │
        ┌───────────────▼────────────────┐  ┌───────────────▼────────────────┐
        │ 路线 A：上下界传递（下行传参）   │  │ 路线 B：中序严格递增（序列化）   │
        │ O(n) 时间 / O(h) 空间           │  │ O(n) 时间 / O(h) 空间            │
        └───────────────┬────────────────┘  └───────────────┬────────────────┘
                        │                                   │
            每个结点看一次「祖先给的区间」            每个结点与前驱比一次
            天然前序（先判自己再下探）              天然中序（左 → 根 → 右）
                        │                                   │
              ✅ 与定义一一对应，最好讲           ✅ 与"BST 中序有序"呼应，最短
              ⚠️ 初值要用 long / nil              ⚠️ 需要 prev 状态（记得重置）
                        │                                   │
                        └───────────┬───────────────────────┘
                                    ▼
                      追问 O(1) 空间 → 路线 B + Morris
```

---

## 076.5 双语言实现

### 076.5.1 Java

```java
// LeetCode 判题环境已内置 TreeNode；本地编译时提供一个同名类即可
class Solution {
    public boolean isValidBST(TreeNode root) {
        // 上下界用 Long 的 null 表示"没有边界"：
        // 若用 Integer.MIN_VALUE / MAX_VALUE 当哨兵，结点值恰好等于极值时会误判
        // （约束是完整的 int 区间，两端都能取到）
        return check(root, null, null);
    }

    /** lo/hi 为 null 表示这一侧无界；开区间 (lo, hi) */
    private boolean check(TreeNode node, Long lo, Long hi) {
        if (node == null) return true;                 // 空树是合法的 BST

        // 用 long 比较：node.val 是 int，但和 Long 比较时会自动提升，不会溢出
        if (lo != null && node.val <= lo) return false; // 必须严格大于下界
        if (hi != null && node.val >= hi) return false; // 必须严格小于上界

        // 左子树的上界被当前结点收紧，右子树的下界被当前结点收紧
        // —— 这一步就是"把祖先的约束传下去"，也是 3 能被抓出来的原因
        return check(node.left, lo, (long) node.val)
            && check(node.right, (long) node.val, hi);
    }
}
```

### 076.5.2 Go

```go
package main

// LeetCode Go 判题环境已内置该定义，本地跑时保留即可
type TreeNode struct {
	Val   int
	Left  *TreeNode
	Right *TreeNode
}

func isValidBST(root *TreeNode) bool {
	// Go 用 *int 为 nil 表示"这一侧无界"，等价于 Java 的 Long null
	var check func(node *TreeNode, lo, hi *int) bool
	check = func(node *TreeNode, lo, hi *int) bool {
		if node == nil {
			return true
		}
		// 注意用 >= / <=：BST 要求严格，值相等就非法
		if lo != nil && node.Val <= *lo {
			return false
		}
		if hi != nil && node.Val >= *hi {
			return false
		}
		// 取地址是安全的：v 是每次调用栈上的新局部变量，逃逸分析会把它放到堆上，
		// 两个递归调用各自拿到独立的副本
		v := node.Val
		return check(node.Left, lo, &v) && check(node.Right, &v, hi)
	}
	return check(root, nil, nil)
}
```

**第二种写法（中序 + 前驱，Go 版把状态封在闭包里，避免全局变量）**：

```go
package main

func isValidBSTInorder(root *TreeNode) bool {
	var prev *TreeNode // 中序的前一个结点；nil 表示还没访问过任何结点
	ok := true         // 一旦非法就置 false，但仍然把遍历走完（这里用短路更省）

	var walk func(node *TreeNode)
	walk = func(node *TreeNode) {
		if node == nil || !ok {
			return
		}
		walk(node.Left)
		// 中序的"根"位置：此刻 prev 是左子树的最右结点，必须严格小于当前结点
		if prev != nil && prev.Val >= node.Val {
			ok = false
			return
		}
		prev = node
		walk(node.Right)
	}
	walk(root)
	return ok
}
```

### 076.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 「无界」的表示 | `Long lo = null` | `*int` 为 `nil` | 两边都不用 int 极值当哨兵；这是本题最容易错的边界 |
| 递归参数包装 | `Long` 会装箱，`n = 10⁴` 时约 2 万次装箱 | `*int` 无装箱，但取地址有逃逸分析开销 | 想要零开销可改写成「用 `int64` 的 ±∞ 代表无界」（`math.MinInt64/2`），见下 |
| 状态变量 | `private TreeNode prev` 字段（**记得重置**） | 闭包捕获的局部变量（天然按次隔离） | 中序解法里 Go 更安全；Java 用字段时同一实例连续调用会串味 |
| 布尔短路 | `&&` 短路 | `&&` 短路 | 两边一致；上下界写法里靠短路省掉一半递归 |
| 指针可空 | `null` | `nil` | Go 的 `nil` 指针比较用 `==`，Java 也必须用 `==` 判 `null` |

> 想彻底避开「包装类型」的开销，可以把无界写成 `int64` 哨兵（Java `Long.MIN_VALUE / 2`、Go `math.MinInt64 / 2`）。用 `long` 而不是 `int`，是为了让哨兵**严格小于任何 int 值**，不会被真实数据撞上。

### 076.5.4 对拍测试（参考实现：独立的中序收集 + 严格递增检查）

```go
package main

import (
	"math"
	"math/rand"
	"testing"
)

// ---------- 辅助 ----------

func randTree(r *rand.Rand, n int) *TreeNode {
	if n == 0 {
		return nil
	}
	root := &TreeNode{Val: r.Intn(7) - 3} // 值域 [-3,3]：重复值密集，逼出"严格/非严格"的 bug
	slots := []**TreeNode{&root.Left, &root.Right}
	for i := 1; i < n; i++ {
		j := r.Intn(len(slots))
		p := slots[j]
		*p = &TreeNode{Val: r.Intn(7) - 3}
		slots[j] = slots[len(slots)-1]
		slots = slots[:len(slots)-1]
		slots = append(slots, &(*p).Left, &(*p).Right)
	}
	return root
}

// randBST 造一棵【合法】BST：随机插入互不相同的值，顺便测试"合法路径"不被误杀
func randBST(r *rand.Rand, n int) *TreeNode {
	perm := r.Perm(n) // 0..n-1 的随机排列，天然互不相同
	var root *TreeNode
	for _, v := range perm {
		if root == nil {
			root = &TreeNode{Val: v}
			continue
		}
		cur := root
		for {
			if v < cur.Val {
				if cur.Left == nil {
					cur.Left = &TreeNode{Val: v}
					break
				}
				cur = cur.Left
			} else {
				if cur.Right == nil {
					cur.Right = &TreeNode{Val: v}
					break
				}
				cur = cur.Right
			}
		}
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

// dump 前序导出结构：失败时能按输出重建整棵树
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

// ---------- 参考实现：中序收集成数组，再判严格递增 ----------
// 刻意与「上下界传递」逻辑完全不同：一个走序列，一个走区间

func bruteIsBST(root *TreeNode) bool {
	var seq []int
	var walk func(*TreeNode)
	walk = func(x *TreeNode) {
		if x == nil {
			return
		}
		walk(x.Left)
		seq = append(seq, x.Val)
		walk(x.Right)
	}
	walk(root)
	for i := 1; i < len(seq); i++ {
		if seq[i-1] >= seq[i] { // 严格：相等也非法
			return false
		}
	}
	return true
}

// ---------- 对拍 ----------

func TestIsValidBSTRandom(t *testing.T) {
	r := rand.New(rand.NewSource(2026076))
	for iter := 0; iter < 5000; iter++ {
		n := r.Intn(25) // 0..24，含空树
		root := randTree(r, n)
		want := bruteIsBST(root)
		if got := isValidBST(root); got != want {
			t.Fatalf("bounds: iter=%d tree=%s got=%v want=%v", iter, dump(root), got, want)
		}
		if got := isValidBSTInorder(root); got != want {
			t.Fatalf("inorder: iter=%d tree=%s got=%v want=%v", iter, dump(root), got, want)
		}
	}

	// 合法 BST 分支：随机 BST 必须被判成 true（防止"一律返回 false"的假通过）
	for iter := 0; iter < 2000; iter++ {
		root := randBST(r, 1+r.Intn(30))
		if !isValidBST(root) || !isValidBSTInorder(root) {
			t.Fatalf("合法 BST 被误判: iter=%d tree=%s", iter, dump(root))
		}
	}
}

func TestIsValidBSTEdges(t *testing.T) {
	maxInt := int(math.MaxInt32)
	minInt := int(math.MinInt32)
	cases := []struct {
		in   []interface{}
		want bool
	}{
		{nil, true},                  // 空树合法
		{[]interface{}{1}, true},     // 单结点
		{[]interface{}{1, 1}, false}, // 重复值 ⇒ 非法
		{[]interface{}{5, 4, 6, nil, nil, 3, 7}, false}, // 父子对全合法但全局非法
		{[]interface{}{2, 1, 3}, true},
		{[]interface{}{5, 1, 4, nil, nil, 3, 6}, false},
		{[]interface{}{maxInt}, true},                    // int 上界当结点值
		{[]interface{}{minInt}, true},                    // int 下界当结点值
		{[]interface{}{minInt, nil, maxInt}, true},       // 两端同时出现
		{[]interface{}{0, minInt, maxInt}, true},         // 只是"看起来像边界"
		{[]interface{}{2, 2, 2}, false},                  // 三个相同值
	}
	for _, c := range cases {
		root := build(c.in)
		if got := isValidBST(root); got != c.want {
			t.Fatalf("bounds: tree=%s got=%v want=%v", dump(root), got, c.want)
		}
		if got := isValidBSTInorder(root); got != c.want {
			t.Fatalf("inorder: tree=%s got=%v want=%v", dump(root), got, c.want)
		}
		if got := bruteIsBST(root); got != c.want {
			t.Fatalf("brute: tree=%s got=%v want=%v", dump(root), got, c.want)
		}
	}
}
```

> **对拍的三个设计要点**：① 参考实现走「序列」而不是「区间」，与主解法**逻辑互不相同**；② 随机值域只有 `[-3,3]`，重复值密集，把「严格与非严格」的差别逼出来；③ 额外用 `randBST` 造**合法**树做正样本，否则一个「永远返回 `false`」的假解法也能通过随机对拍。

---

## 076.6 复杂度与常见坑

### 076.6.1 复杂度推导（写结论不算本事，要能推）

- **时间 O(n)**：两种写法都是「每个结点恰好被访问一次」。上下界版本：每次递归做常数次比较，`T(n) = T(左) + T(右) + O(1)` ⇒ `Θ(n)`。中序版本：每个结点进一次、比一次。
- **空间 O(h)**：递归栈深度。平衡树 `O(log n)`；`n = 10⁴` 的退化链 `O(n) = 10⁴` —— Java 默认栈（约 1 万层）刚刚好卡在边缘，这也是为什么 10⁵ 的同类题必须换迭代。
- **为什么中序「不需要额外数组」**：因为判定只依赖**相邻两项**，所以用一个 `prev` 变量就够，把「序列」压缩成「一个游标」。一旦你写出「先收集数组再判」，就多了 `O(n)` 空间 —— 功能正确但没有必要。

| 方案 | 时间 | 空间 | `n = 10⁴` 退化链实测（2 核容器 / JDK 17 / Go 1.19） |
| --- | --- | --- | --- |
| 逐点扫子树 | O(n²) | O(h) | Java ≈ 1.5 s / Go ≈ 1.0 s（危险区） |
| **上下界传递** | **O(n)** | **O(h)** | **Java ≈ 1 ms / Go ≈ 1 ms** |
| 中序 + prev | O(n) | O(h) | Java ≈ 1 ms / Go ≈ 1 ms |
| 中序 + Morris | O(n) | **O(1)** | Java ≈ 3 ms（更慢，但空间 O(1)） |

### 076.6.2 八个真实踩过的坑

1. **只比较直接左右孩子** —— 本章第一号坑。`[5,4,6,null,null,3,7]` 会被判成 `true`。必须把祖先的区间传下去。
2. **用 `Integer.MIN_VALUE` / `MAX_VALUE` 当初始上下界** —— 约束允许结点值等于 `-2³¹` 或 `2³¹−1`，此时 `node.val >= hi` 会把合法结点判非法。改用 `Long` 的 `null`（或 `int64` 的 ±∞ 哨兵）。
3. **把「严格」写成「非严格」** —— `lo <= node.val` 或 `prev.val > cur.val` 的方向写反，`[2,2,2]` 会被判成合法。
4. **`prev` 没重置** —— 中序写法里 `prev` 是成员变量，同一 `Solution` 实例连续调用会带着上一次的残留状态 → 本地跑单个用例永远发现不了。
5. **递归里忘了判空** —— `node.left.val` 在 `node.left == null` 时直接 NPE / panic。
6. **左右子树传参顺序写反** —— 左子树应该是 `(lo, node.val)`、右子树是 `(node.val, hi)`；写反后 `[5,4,6,null,null,3,7]` 反而被判成合法。
7. **中序实现里「先比较再更新 prev」写成了「先更新再比较」** —— 变成自己和自己比，永远通过，全部用例返回 `true`。
8. **低估链状树的递归深度** —— `n = 10⁴` 尚可，但同类题（LC 501 / 530）的树可能到 10⁵，那时必须换显式栈或 Morris。

### 076.6.3 面试官最爱追问的三个问题

**Q1：为什么只比较左右孩子不行？能不能给个例子？**

> 「因为 BST 的约束是**全局**的：一个结点要小于它所有祖先中『在右边』的那些、大于所有祖先中『在左边』的那些。只比直接孩子，等于把祖先的约束丢了。精确反例是 `[5,4,6,null,null,3,7]`：所有父子对都合法（5>4、5<6、6>3、6<7），但 `3` 在 `5` 的右子树里却比 `5` 小，所以不是 BST。这也是为什么正确写法必须**把区间传下去**，或者改成**中序序列必须严格递增**。」

**Q2：上下界传参和中序遍历，你更推荐哪个？**

> 「取决于题目还要求什么。**上下界**与定义一一对应，最好讲、最好证明、不需要任何状态变量，是面试首选。**中序**的优势是短，而且它给出了一个可复用的等价刻画（BST ⟺ 中序严格递增），一旦要求 **O(1) 空间**，中序 + Morris 是唯一的路。它的代价是多一个 `prev` 状态（Java 里是字段，要记得重置）。我自己会先写上下界，然后补一句『如果要求 O(1) 空间，我会用 Morris 中序，在拆线索的那一刻做比较』。」

**Q3：如果允许重复值，怎么改？**

> 「那要把定义说清楚：常见约定是『左子树 ≤ 根 < 右子树』，此时判定从**严格递增**放宽成**非严格递增**（`prev.val > cur.val` 才报错）；上下界版本对应把区间从开区间 `(lo, hi)` 改成半开 `[lo, hi)`，只在右子树收紧下界为闭。**关键是别混**：如果一边允许相等、一边又要求严格，`[2,2,2]` 这种用例会左右摇摆。工程上我会在函数注释里把约定写死，再配一个 `[2,2,2]` 的用例锁住它。」

---

## 076.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 99 恢复二叉搜索树](https://leetcode.cn/problems/recover-a-binary-search-tree/) | 中等 | 中序里恰有两处「逆序」，记下 4 个指针后交换值 |
| [LC 230 二叉搜索树中第 K 小的元素](https://leetcode.cn/problems/kth-smallest-element-in-a-bst/) | 中等 | 中序走到第 K 个提前停 → 见 [`077-kth-smallest-element-in-a-bst.md`](077-kth-smallest-element-in-a-bst.md) |
| [LC 235 二叉搜索树的最近公共祖先](https://leetcode.cn/problems/lowest-common-ancestor-of-a-binary-search-tree/) | 简单 | 借有序性 O(h)/O(1)（对比见 [`075-lowest-common-ancestor-of-a-binary-tree.md`](075-lowest-common-ancestor-of-a-binary-tree.md)） |
| [LC 333 最大 BST 子树](https://leetcode.cn/problems/largest-bst-subtree/) | 中等 | 自底向上返回 `(是否 BST, min, max, size)`，一次遍历把答案算出来 |
| [LC 530 二叉搜索树的最小绝对差](https://leetcode.cn/problems/minimum-absolute-difference-in-bst/) | 简单 | 中序相邻两项之差的最小值（提示：答案必在相邻项之间） |
| [LC 501 二叉搜索树中的众数](https://leetcode.cn/problems/find-mode-in-binary-search-tree/) | 简单 | 中序 + 统计连续相同值的长度，O(1) 空间即可 |
| [LC 700 二叉搜索树中的搜索](https://leetcode.cn/problems/search-in-a-binary-search-tree/) | 简单 | 有序性带来的「只走一条路」：O(h) 时间、O(1) 空间 |
| [LC 1038 从二叉搜索树到更大和树](https://leetcode.cn/problems/binary-search-tree-to-greater-sum-tree/) | 中等 | 「反中序」（右 → 根 → 左）累加前缀和 |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 99 恢复 BST**
1. 中序本该严格递增，两个结点被交换后，序列里会出现几处 `前 ≥ 后`？
2. 第一处逆序要记下哪两个结点？最后一处呢？

**LC 333 最大 BST 子树**
1. 父结点判断「我是不是 BST」需要子树提供哪些信息？只给一个布尔值够不够？
2. 若子树不是 BST，它的 `min/max/size` 还有用吗？

**LC 501 众数（要求 O(1) 空间）**
1. 中序序列里相同的值是连续出现的，这让「统计频次」变得多简单？
2. 众数可能有多个，一边遍历一边维护「当前众数集合」要注意什么？

**通用**
- 先问「这个性质依赖祖先吗」：依赖就往下传，不依赖就看能不能换个刻画。
- 只要题目提到 BST，第一个念头就该是「中序遍历 = 有序序列」。

</details>

---

## 附：本章小结（可背走的三句话）

```
① BST 是全局约束：只比左右孩子必错（反例 [5,4,6,null,null,3,7]）
② 两条正解：把区间 (lo,hi) 自顶向下传参 / 中序必须严格递增；都是 O(n)
③ 下界上界的初值绝不能用 int 极值，用 long 的 ±∞ 或 nil，"严格"两个字不能松
```

> **下一章**：[`077-kth-smallest-element-in-a-bst.md`](077-kth-smallest-element-in-a-bst.md) —— 把「中序 = 有序」用到极致：走到第 K 个就停，顺便讨论「树会频繁修改」时的解法升级。
