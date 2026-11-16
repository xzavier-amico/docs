# 067. LC199 二叉树的右视图（Binary Tree Right Side View）

> **状态**：`已完成（初稿）` ｜ **所属**：第 8 部分 · 树形结构 ｜ **编号**：067 / 150
> **难度**：中等（「右视图」一词极容易被误读成「最右链」，这是本章唯一的真考点）｜ **标签**：树 · 深度优先搜索 · 广度优先搜索 · 队列 · 二叉树
> **一句话**：这道题真正考的是「**站在右边看到的是『每层最右那个』，不是『一路往右走』**」，不是「把 066 的模板抄一遍」。
> **前置**：[`066-binary-tree-level-order-traversal.md`](066-binary-tree-level-order-traversal.md)（BFS 按层模板与 `size` 快照的出处）

---

## 067.1 题目

**原题链接**：<https://leetcode.cn/problems/binary-tree-right-side-view/>

### 题意重述（把隐含条件补齐）

给你一棵二叉树的根节点 `root`，想象你站在树的**右侧**，从根所在的高度往下看。你能看到的，是**每一层里最靠右的那个节点**。请按「从上到下」的顺序返回这些节点的值。

一句话就说完的题，藏了五个必须读出来的约束：

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| 每层只取**一个** | 「想象自己站在右侧」「只能看到右侧能看到的节点」 | 返回全部节点 → 判错 |
| 是「每层**最右**」而不是「**最右链**」 | 样例 `[1,2,3,null,5,null,4] → [1,3,4]` | 只沿 `right` 指针走 → 得到 `[1,3]`，漏掉 `4` |
| 顺序是**从上到下** | 「从顶部到底部」 | 从叶子往根返回 → 判错 |
| 空树返回**空列表** | 样例 `root = []` | 返回 `[0]` 或 `[null]` → 判错 |
| 节点值**可以为负、可以重复** | 约束 `-100 <= Node.val <= 100` | 用 `0` 或值本身当「本层没取到」的标记 → 逻辑崩 |

> 📌 **本题与 066 的关系**：066 的模板在每层里收集**全部**节点；本题只要「每层**最后一个**」。
> 也就是说，067 是 066 模板的**第一次空位替换**（见 §066.3.3 ② 那张「三个空位」表）。本章要验证的是：**那套模板真的能一行改动复用**。

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| 节点数 `[0, 100]` | 最多 100 个 | 任何 O(n) / O(n·h) 都能过；**本题不考时间，考读题** |
| `-100 <= Node.val <= 100` | 有负数 | 不能用 `val > 0` 判断「有没有取到」 |
| 树不保证平衡 | 可能是全左链 | 「最右链」在全左链上只有根一个节点，但右视图有 `h` 个 —— 反例的种子 |
| 返回 `List<Integer>`（一维） | 「返回从顶部到底部能看到的节点值」 | 比 066 少一层嵌套，所以**不需要收集整层** |

> 💡 **量级 → 算法**：`n ≤ 100`，复杂度完全不是考点。
> 本题真正的难度全在**语义**：把「右视图」翻译成「每层最右」这一句，题就做完了；翻错成「最右链」，怎么优化都是错的。
> **读题比写题难**，这是本章唯一要带走的东西。

### 示例

```
示例 1
输入：root = [1, 2, 3, null, 5, null, 4]
输出：[1, 3, 4]

                1              ← 第 0 层最右：1
             ┌──┴──┐
             2     3           ← 第 1 层最右：3
              \     \
               5     4         ← 第 2 层最右：4

注意：4 是「3 的右孩子」，而不在「2 的右孩子」那条线上 ——
      只沿 right 走只能拿到 1 → 3，拿不到 4。

示例 2
输入：root = [1, 2, 3, 4, null, null, null, 5]
输出：[1, 3, 4, 5]             ← 每层最右分别是 1 / 3 / 4 / 5

示例 3
输入：root = [1, null, 3]
输出：[1, 3]

示例 4
输入：root = []
输出：[]
```

### 我自造的三个反例（写完必须拿它们打一遍）

```text
① 空树 []                       → 暴露「没判空就 offer(root)」的 NPE，以及返回 [0] 的错
② [1, 2, 3, null, 5, null, 4]   → 暴露「一路沿 right 走」的错（会漏掉 4）
③ 全左链 1←2←3←4（只有 left）    → 暴露「只处理右孩子」的错（右视图应为 [1,2,3,4]）
```

第 ③ 条最能说明问题：全左链上**没有一个右孩子**，但右视图**每层都有一个节点**。这说明「右视图」与「右指针」根本是两件事，前者是**层的属性**，后者是**树的连边**。

---

## 067.2 从暴力到最优：第一个念头为什么错

### 第一个念头：沿 `right` 一路走（最典型的语义误读）

```java
List<Integer> ans = new ArrayList<>();
TreeNode cur = root;
while (cur != null) {          // ❌ 这不是右视图
    ans.add(cur.val);
    cur = cur.right;
}
```

- 时间 **O(h)**，空间 **O(1)** —— 复杂度最漂亮，但**答案是错的**。
- 反例就是示例 1：`1 → 3 → 4` 这条线上确实是 `[1,3,4]`，看起来对了；但把树换成
  `root = [1,2,3,4]`（`4` 是 `2` 的左孩子），右视图应该是 `[1,3,4]`，而这条代码只给 `[1,3]` —— **第 2 层明明有节点，却被跳过了**。

```
❌ 错误直觉：右视图 = 最右链
             1
           ┌─┴─┐
           2   3
          ┌┘
          4
   沿 right：1 → 3 → (null)，得到 [1,3]
   真正的右视图：第 2 层只有 {4}，最右是 4 → [1,3,4]   ← 差一个
```

**为什么这个念头特别危险**：它在「每层的唯一节点恰好都在右链上」的树（比如全右链、完全左偏的树）上**完全正确**；只有遇到「右子树比左子树浅」的树才暴露。所以自测一定要造这种树。

### 第二个念头：不看层，只取「最后入队的」

```java
while (!q.isEmpty()) {
    TreeNode cur = q.poll();
    if (cur.left != null)  q.offer(cur.left);
    if (cur.right != null) q.offer(cur.right);
    if (q.isEmpty()) ans.add(cur.val);   // ❌ 这是「整棵树最后一个」
}
```

- 它只在队列**整体空了**的时候记录一次，得到的是**树最右下的那个叶子**，只有 1 个值。
- 变体错误：把判断改成「`i == q.size() - 1`」却又忘了先拍快照 —— 于是 `q.size()` 一边长一边变，记录到的是「某一层的第二个孩子」，结果随机地多一个少一个。

### 第三个念头：老老实实求完整层序，再取每层最后一个

```java
List<List<Integer>> levels = levelOrder(root);   // 复用 066 的完整解法
List<Integer> ans = new ArrayList<>();
for (List<Integer> lv : levels) ans.add(lv.get(lv.size() - 1));
```

- 时间 **O(n)**，空间 **O(n)**（为了拿 `h` 个值，存了 `n` 个值）。
- **它是对的**，而且是把 067 归约到 066 的干净思路。唯一的浪费是「每层收集了 `w` 个，却只用 1 个」。

### 卡点在哪

> 卡点只有一个：**你怎么知道「当前节点是本层最后一个」？**

层序遍历的队列里，「本层」是一条**不动的分界线**左侧的片段（见 §066.2）。三个候选判据，一对一错一勉强：

| 判据 | 是否可靠 | 原因 |
| --- | --- | --- |
| `q.isEmpty()` | ❌ | 只在整棵树结束时成立，不是「本层结束」 |
| `q.size()` 动态判断 | ❌ | 队列一边弹一边长，`size` 永远在变 |
| **`i == size - 1`（`size` 是快照）** | ✅ | 快照固定了本层长度，第 `size-1` 个就是本层最后一个 |

### 灵感的来源 —— 「先右后左」这个交换

把入队顺序从「先左后右」改成「先右后左」，会发生什么？

```
原顺序（先左后右）：每层的节点从左到右入队 → 本层最后一个 = 最右节点
新顺序（先右后左）：每层的节点从右到左入队 → 本层第一个   = 最右节点
```

两种写法都能拿到答案，但**第二种有个额外好处**：它和第 4 个思路（下方 DFS）**共用同一句直觉** —— 「右优先遍历时，每层第一个被访问的节点就是最右节点」。

> 这条直觉还能进一步压成一句话：
> **右视图 = 「右优先的遍历」里，每一层第一次到达的节点。**
> 有了这句，连队列都可以不要 —— 一个 `depth` 参数 + 一个「这一层还没记过」的判断就够，见 §067.3.3 ③。

---

## 067.3 原理拆解

### 067.3.1 数据结构与算法选型

| 方案 | 时间 | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- |
| **BFS + `size` 快照，取 `i == size-1`** ✅ 首选 | O(n) | O(w) | 11 行 | 超宽树内存吃紧；只读一层时做了整层工作（但同层的其他节点必须处理才能到达下一层，无法避免） |
| BFS + 先右后左，取 `i == 0` | O(n) | O(w) | 11 行 | 与上一行同量级；层内顺序变了，若同时要层序结果就不能这么写 |
| **DFS 右优先 + `depth == ans.size()` 记录** ✅ 进阶 | O(n) | O(h) | 8 行 | 树极深（链状 `h = n`）时爆栈；递归深度不受控 |
| DFS 左优先 + 每层覆盖写 `ans[depth] = val` | O(n) | O(h) | 8 行 | 正确但反直觉：最后写入的是每层**最右**节点，需要先想清楚为什么 |
| 先算完整层序再取末位 | O(n) | **O(n)** | 复用 066 | 多存 `n - h` 个用不上的值 |
| 沿 `right` 指针走 | O(h) | O(1) | 5 行 | **语义就错了**，只能骗过右链树 |

**首选 BFS 还是 DFS？三个判据**：

| 判据 | 偏向 BFS | 偏向 DFS |
| --- | --- | --- |
| 题目是否**按层**给语义 | ✅「每层最右」天然是层的属性 | — |
| 树是否**极深**（`h` 接近 `n`） | ✅ 无栈深风险 | ❌ 递归爆栈 |
| 是否**只要本层一个值**、不想多存 | 每层要处理完整层（不可避免，否则走不到下一层） | ✅ 只需 `depth` 首次判断，栈空间 O(h) |

一句话取舍：**本题两种都对，BFS 更贴题面语义，DFS 更贴「右优先」直觉**。面试里先给 BFS（承接 066 的模板，展示复用能力），再补 DFS（展示不依赖队列的视角）。

### 067.3.2 为什么这个做法是对的（正确性论证）

#### 命题 A（BFS 版）

**命题**：算法返回的第 `d` 个值是第 `d` 层最右节点的值。

**证明**：
1. 由 [`066`](066-binary-tree-level-order-traversal.md) §066.3.2 的不变量 —— 每轮 `while` 开始前队列里恰好是同一层的全部节点 —— 我们对其中前 `size` 个节点按「先左后右」的顺序处理。
2. 归纳可得本轮弹出的序列恰为第 `d` 层**从左到右**的顺序（基点：`root` 唯一；归纳步：左孩子先于右孩子入队，且先进先出）。
3. 因此第 `size-1` 个（0 基）弹出的节点是本层最右节点，`i == size - 1` 时记录的正是它。
4. 层号 `d` 随外层 `while` 递增，所以输出顺序是从上到下。∎

#### 命题 B（DFS 右优先版）

**命题**：在「根 → 右子树 → 左子树」（右优先前序）的遍历中，每一层**第一个**被访问的节点就是该层最右节点。

**证明**（核心是层的全序性质）：

取同层任意两个不同节点 `u`、`v`，设 `v` 在 `u` 的右边，令 `a = LCA(u, v)`（最近公共祖先）。则 `u` 落在 `a` 的左子树、`v` 落在 `a` 的右子树。

右优先前序在 `a` 处的执行顺序是「记录 `a` → 递归右子树 → 递归左子树」，所以**右子树里的全部节点都先于左子树里的任何节点被访问**，于是 `v` 先于 `u`。

对同层的任意两点都成立，因此「同层节点的访问顺序」恰是「从左到右」的**逆序**，即从右到左；其中第一个被访问的必然是最右的那个。再用 `depth == ans.size()` 判断「这一层还没记录过」（因为 `ans` 每层只增长一次），记录的正是首次到达的节点。∎

> **这一步为什么值得严格写**：命题 B 的证明用到的是「层内任意两点，右边那个一定先被访问」—— 它解释了为什么**必须右优先**。如果改成左优先前序，同层的访问顺序就变成从左到右，第一个到达的是**最左**节点，得到的是「左视图」（这也是 §067.3.4 第 1 条追问的答案）。

#### 命题 C（DFS 左优先 + 覆盖写版）

**命题**：若按「根 → 左 → 右」前序遍历，并对每个节点执行 `ans[depth] = val`（每层覆盖写），则最终 `ans[depth]` 是第 `depth` 层最右节点的值。

**证明**：由命题 B 的对偶论证，左优先前序下同层访问顺序是**从左到右**，所以每层最后一个被访问的是最右节点；覆盖写保留的正是最后一次写入的值。∎

> 三种写法都对，但**命题 C 是「看起来最不像对的」那种**，面试里写它要主动解释一句，否则容易被判成碰巧。

### 067.3.3 通用解题思路（可迁移的那一层）

#### ① 把 066 的模板「填空」复用（本章的复用演示）

§066.3.3 ② 的模板只有三个空位，本章只动第一个：

| 空位 | 066 的填法 | 067 的填法 |
| --- | --- | --- |
| 「本层收集什么」 | 整层 `level.add(cur.val)` | **只在本层最后一个时记一个值**：`if (i == size - 1) ans.add(cur.val)` |
| 「本层插到答案哪端」 | 追加 | 追加（不变） |
| 「每轮方向」 | 先左后右 | 先左后右（不变） |

**结论：骨架一行不改，只把「收集整层」换成「收集末位」，`ans` 从二维降成一维，辅助空间从「每层一个 List」降到 0。**

#### ② 树递归语言：本章贡献的是「**视角互换**」

066 教会我们「按层切」，067 教会我们「同一棵树，换个视角看」。把这两条并到本书的树递归语言里：

| 视角 | 一句话 | 代表题 |
| --- | --- | --- |
| **按层**（横向切片） | 用队列，`size` 快照切出「第 d 层」 | 066 / 067 / 103 / 107 / 515 / 637 |
| **按路径**（纵向走到底） | 用递归/栈，答案沿路径累积或回溯 | 068 / 072 / 073 |
| **按子树聚合**（自底向上） | 递归返回值 = 子树的某个统计量，父节点合并 | 068 / 071 / 074 / 080 |
| **按「首次到达」**（右优先/左优先） | 深度优先 + 每层第一次到达即记录 | 067 的 DFS 版 / 199 / 513（左下角） |

> 🧭 **第 4 行是本章新增的视角**，它和 065 的「前序/中序/后序」不冲突：065 管的是「根在哪个位置被记录」，本章管的是「先钻哪棵子树」。两者正交，可以组合出 4 种前序（先左/先右 × 记录时机）。

#### ③ DFS 版骨架（可背）

```text
ans = []
dfs(node, depth):
    if node == null: return
    if depth == ans.size():        # 这一层第一次到达 → 只有「右优先」时它才是最右
        ans.add(node.val)
    dfs(node.right, depth + 1)     # ★ 先右
    dfs(node.left,  depth + 1)     # ★ 再左
```

**这句 `depth == ans.size()` 是「首次到达」的通用判据**，后面 513（找树左下角的值）把它和「左优先」组合，就得到左下角 —— 完全是同一段代码换一行。

### 067.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| 要**左视图** | 站到左边 | BFS：取 `i == 0`；DFS：改「先左后右」，`depth == ans.size()` 判断不变 |
| 要**树左下角的值**（513） | 最左 + 最深 | DFS 左优先 + `depth == ans.size()` 覆盖式记录（每层都覆盖成更深层的最左）→ 见 §067.6.3 Q4 |
| 要**每层最右那个节点本身**（不是值） | 返回节点引用 | BFS 里存 `cur` 而不是 `cur.val`，其余不变 |
| 树是 **N 叉** | 孩子不定 | BFS 里对本层最后一个孩子入队顺序无要求；DFS 里「先右」变成「从最后一个孩子往前遍历」 |
| 数据流 / **树在变**（边插边看） | 无法一次遍历 | 维护「每层最右」的表，插入节点时按 `depth` 覆盖，O(h) 一次 |
| 要**整条右侧轮廓**（含右边界叶子到根的折返） | 不是每层一个 | 分成「左边界 + 叶子 + 右边界」三段拼，见 LC 545 |
| **O(1) 空间**求右视图 | 不许开队列 | 用 116/117 的 `next` 指针链（改树）或 Morris 变体；只读场景下无解 |

---

## 067.4 图解推演

### 067.4.1 用来推演的树与正确答案

```
                 1                      层 0：{1}          最右 1
              ┌──┴──┐
              2     3                  层 1：{2,3}        最右 3
               \     \
                5     4                层 2：{5,4}        最右 4

右视图 = [1, 3, 4]
```

### 067.4.2 三种「判据」的差别（同一个队列，三行不同的判断）

```
队列演化（先左后右入队）：

轮 0  队列 [1]        size=1   弹 1（i=0=size-1 ✅ 记录）   入队 2,3      → [2,3]
轮 1  队列 [2,3]      size=2   弹 2（i=0）→ 入队 5
                               弹 3（i=1=size-1 ✅ 记录）   入队 4       → [5,4]
轮 2  队列 [5,4]      size=2   弹 5（i=0）
                               弹 4（i=1=size-1 ✅ 记录）                → []
轮 3  队列 []                  结束

判据对比：
  i == size-1  → 每层各记一次 → [1, 3, 4]        ✅ 正确
  q.isEmpty()  → 只在最后成立 → [4]              ❌ 只有树最右下的叶子
  q.size()     → 边界随入队增长 → 乱记            ❌ 不可用
```

### 067.4.3 DFS 右优先的轨迹（同一棵树）

```
visit(1, d=0)   → ans.size()=0 == d → 记录 1         ans=[1]
 ├─ visit(3, d=1) → ans.size()=1 == d → 记录 3        ans=[1,3]
 │   ├─ visit(null, d=2) 返回
 │   ├─ visit(4, d=2) → ans.size()=2 == d → 记录 4    ans=[1,3,4]
 │   │   ├─ visit(null,2) 返回
 │   │   └─ visit(null,2) 返回
 │   └─ visit(null, d=2) 返回
 └─ visit(2, d=1) → ans.size()=3 ≠ d=1 → 不记录（第 1 层已经记过 3 了）
     ├─ visit(null, d=2) 返回
     └─ visit(5, d=2) → ans.size()=3 ≠ d=2 → 不记录（第 2 层已经记过 4 了）
```

> 看最后两行：`2` 和 `5` 都被**访问了但不记录**。这就是「右优先 + 首次到达」的实质 —— **先到的人占位，后来的人让位**。

### 067.4.4 两条路线的对比

```
                  ┌────────── 输入 root ──────────┐
                  │                               │
      ┌───────────▼────────────┐   ┌──────────────▼──────────────┐
      │ BFS：队列 + size 快照  │   │ DFS：右优先前序 + depth 判据 │
      │ O(n) / O(w)            │   │ O(n) / O(h)                 │
      └───────────┬────────────┘   └──────────────┬──────────────┘
                  │                               │
         i == size-1 时记录              depth == ans.size() 时记录
         语义直接对齐「每层最右」          语义来自「右边先到先占位」
                  │                               │
        【宽树 / 贴题面首选】            【深树怕爆栈，但代码更短】
```

### 067.4.5 步骤表（行 = 步骤，列 = 变量）

| 步骤 | 动作 | `ans`（BFS） | `ans`（DFS 右优先） | 说明 |
| --- | --- | --- | --- | --- |
| 1 | 记录第 0 层最右 | `[1]` | `[1]` | 两种写法都一样 |
| 2 | 记录第 1 层最右 | `[1,3]` | `[1,3]` | BFS 靠 `i==size-1`，DFS 靠首次到达 |
| 3 | 处理第 2 层的 `5` | `[1,3,4]`（先弹 5，不记录） | `[1,3,4]`（访问 5，不记录） | 两条路都不记 `5`，因为 `4` 更靠右 |
| 4 | 处理第 2 层的 `4` | 记录 → `[1,3,4]`（BFS 里 4 在本层最后） | 已在步骤 3 前记过 | 顺序不同，结果相同 |

---

## 067.5 双语言实现

### 067.5.1 Java

**主解（BFS，承接 066 模板）**：

```java
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;

// LeetCode 判题环境已内置 TreeNode，粘贴到网页时不要重复定义：
// class TreeNode {
//     int val;
//     TreeNode left, right;
//     TreeNode(int val) { this.val = val; }
// }

class Solution {
    public List<Integer> rightSideView(TreeNode root) {
        List<Integer> ans = new ArrayList<>();
        if (root == null) {
            return ans;                      // 空树 → 空列表（不是 [0]）
        }

        Deque<TreeNode> q = new ArrayDeque<>();
        q.offer(root);

        while (!q.isEmpty()) {
            int size = q.size();             // ★ 066 的快照：本层节点数，之后队列怎么长都不变
            for (int i = 0; i < size; i++) {
                TreeNode cur = q.poll();
                // 只在本层最后一个（i == size-1）时记录：这就是「站在右边看到它」的定义
                if (i == size - 1) {
                    ans.add(cur.val);
                }
                // 入队顺序仍是先左后右 —— 因为判据用的是「最后一个」，
                // 保持和 066 完全一致才不会在两层模板间来回切换时写错
                if (cur.left != null) {
                    q.offer(cur.left);
                }
                if (cur.right != null) {
                    q.offer(cur.right);
                }
            }
        }
        return ans;
    }
}
```

**备选解（DFS，右优先 + 首次到达）**：

```java
import java.util.ArrayList;
import java.util.List;

class Solution {
    public List<Integer> rightSideView(TreeNode root) {
        List<Integer> ans = new ArrayList<>();
        dfs(root, 0, ans);
        return ans;
    }

    // depth 从 0 开始；ans.size() 恰好是「已经记录过的层数」
    private void dfs(TreeNode node, int depth, List<Integer> ans) {
        if (node == null) {
            return;
        }
        // depth == ans.size() 表示「这一层还是空的」→ 这是本层第一个被访问的节点。
        // 因为先走右子树，第一个被访问的就是本层最右节点（严格论证见 §067.3.2 命题 B）
        if (depth == ans.size()) {
            ans.add(node.val);
        }
        dfs(node.right, depth + 1, ans);   // ★ 先右：决定了「第一个到达者是最右」
        dfs(node.left, depth + 1, ans);    // ★ 再左：这一支只可能被「不记录」地访问
    }
}
```

### 067.5.2 Go

**主解（BFS）**：

```go
package main

// LeetCode 判题环境已内置该定义，本地编译时保留即可
type TreeNode struct {
	Val   int
	Left  *TreeNode
	Right *TreeNode
}

func rightSideView(root *TreeNode) []int {
	ans := make([]int, 0, 16)
	if root == nil {
		return ans
	}

	// 切片 + head 下标当队列（理由见 066 §5.3：q = q[1:] 会拖住底层数组）
	q := make([]*TreeNode, 1, 64)
	q[0] = root
	head := 0

	for head < len(q) {
		size := len(q) - head // ★ 快照
		for i := 0; i < size; i++ {
			cur := q[head]
			head++
			if i == size-1 { // 本层最后一个 = 最右
				ans = append(ans, cur.Val)
			}
			if cur.Left != nil {
				q = append(q, cur.Left)
			}
			if cur.Right != nil {
				q = append(q, cur.Right)
			}
		}
	}
	return ans
}
```

**备选解（DFS 右优先）**：

```go
package main

func rightSideViewDFS(root *TreeNode) []int {
	ans := make([]int, 0, 16)
	var dfs func(node *TreeNode, depth int)
	dfs = func(node *TreeNode, depth int) {
		if node == nil {
			return
		}
		// ans 的长度就是「已经记录过的层数」，二者相等说明这一层还没被记录
		if depth == len(ans) {
			ans = append(ans, node.Val)
		}
		dfs(node.Right, depth+1) // 先右
		dfs(node.Left, depth+1)  // 再左
	}
	dfs(root, 0)
	return ans
}
```

**本地跑一遍（含四类边界）**：

```go
package main

import "fmt"

// 按 LeetCode 层序数组建树：nil 表示空位
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

func main() {
	cases := [][]interface{}{
		{},                                        // 空树
		{1},                                       // 单结点
		{1, 2, 3, nil, 5, nil, 4},                 // 官方样例
		{1, 2, 3, 4},                              // 「4 是 2 的左孩子」→ 抓「沿 right 走」的错
		{4, 3, nil, 2, nil, 1},                    // 全左链
		{1, nil, 2, nil, 3},                       // 全右链
	}
	for _, c := range cases {
		root := build(c)
		fmt.Printf("%v → BFS %v | DFS %v\n", c, rightSideView(root), rightSideViewDFS(root))
	}
}
```

输出（两条路必须一致，这也是最省的互验）：

```text
[] → BFS [] | DFS []
[1] → BFS [1] | DFS [1]
[1 2 3 <nil> 5 <nil> 4] → BFS [1 3 4] | DFS [1 3 4]
[1 2 3 4] → BFS [1 3 4] | DFS [1 3 4]
[4 3 <nil> 2 <nil> 1] → BFS [4 3 2 1] | DFS [4 3 2 1]
[1 <nil> 2 <nil> 3] → BFS [1 2 3] | DFS [1 2 3]
```

### 067.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 队列容器 | `ArrayDeque`：`offer/poll/isEmpty` | `[]*TreeNode` + `head` 下标 | Go 用 `q = q[1:]` 会让底层数组无法回收；`head` 法一次分配搞定 |
| 递归内部函数 | 直接写私有方法 `dfs(...)` | 必须 `var dfs func(...)` 再赋值（闭包自引用） | Go 里漏写 `var dfs func(...)` 这行声明 → 编译错误 `undefined: dfs` |
| 捕获 `ans` | 参数传 `List` 或字段都行（引用类型） | 闭包直接捕获切片变量，`ans = append(ans, ...)` 会更新**闭包外**的变量 | Go 的切片是按值传递的头，但闭包捕获的是变量本身，所以能"看见" append 的结果 |
| `depth == ans.size()` 的写法 | `ans.size()` | `len(ans)` | 语义完全一致：都是「已记录层数」 |
| 返回空 | `new ArrayList<>()` | `make([]int, 0, 16)` | 都不要返回 `nil`/`null`，判题虽多能接受，但显式空切片更稳 |
| 越界保护 | `size - 1` 需保证 `size ≥ 1`（循环条件已保证） | `size-1` 在 Go 里也是由 `head < len(q)` 保证 | 空树已在函数开头 return，所以 `size ≥ 1` 恒成立 |

### 067.5.4 对拍测试（BFS 与 DFS 两套独立实现 + 暴力层序参考）

```go
package main

import (
	"math/rand"
	"testing"
)

// 暴力参考：先求完整层序（用最朴素的 map[depth] 收集），再取每层最后一个。
// 它与 BFS 快照、DFS 右优先都没有共享代码，适合当裁判
func refRightSide(root *TreeNode) []int {
	byDepth := map[int][]int{}
	maxD := -1
	var walk func(n *TreeNode, d int)
	walk = func(n *TreeNode, d int) {
		if n == nil {
			return
		}
		byDepth[d] = append(byDepth[d], n.Val)
		if d > maxD {
			maxD = d
		}
		walk(n.Left, d+1)
		walk(n.Right, d+1)
	}
	walk(root, 0)
	ans := []int{}
	for d := 0; d <= maxD; d++ {
		lv := byDepth[d]
		ans = append(ans, lv[len(lv)-1]) // 左优先前序 ⇒ 同层从左到右 ⇒ 末位是最右
	}
	return ans
}

func sameInts(a, b []int) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func randTree(r *rand.Rand, n int) *TreeNode {
	if n == 0 {
		return nil
	}
	root := &TreeNode{Val: r.Intn(201) - 100}
	slots := []**TreeNode{&root.Left, &root.Right}
	for i := 1; i < n; i++ {
		j := r.Intn(len(slots))
		p := slots[j]
		*p = &TreeNode{Val: r.Intn(201) - 100}
		slots[j] = slots[len(slots)-1]
		slots = slots[:len(slots)-1]
		slots = append(slots, &(*p).Left, &(*p).Right)
	}
	return root
}

func TestRightSideViewRandom(t *testing.T) {
	r := rand.New(rand.NewSource(199))
	for iter := 0; iter < 20000; iter++ {
		n := r.Intn(40)
		root := randTree(r, n)
		want := refRightSide(root)
		gotBFS := rightSideView(root)
		gotDFS := rightSideViewDFS(root)
		if !sameInts(gotBFS, want) {
			t.Fatalf("iter=%d BFS got=%v want=%v", iter, gotBFS, want)
		}
		if !sameInts(gotDFS, want) {
			t.Fatalf("iter=%d DFS got=%v want=%v", iter, gotDFS, want)
		}
	}
}

// 专测「右子树比左子树浅」的形状：这正是「沿 right 走」会漏节点的形态
func TestRightSideViewLeftDeeper(t *testing.T) {
	// 造一条长左链，每个左孩子再挂一个右叶子：最右节点全在「左链的右孩子」上
	var root *TreeNode
	var prev *TreeNode
	for i := 0; i < 20; i++ {
		node := &TreeNode{Val: i}
		if root == nil {
			root = node
		} else {
			prev.Left = node
		}
		prev = node
	}
	got := rightSideView(root)
	if len(got) != 20 {
		t.Fatalf("全左链右视图应有 20 个值，got=%v", got)
	}
	for i, v := range got {
		if v != i {
			t.Fatalf("第 %d 层应为 %d，got=%v", i, i, got)
		}
	}
}
```

> **为什么要专门写 `TestRightSideViewLeftDeeper`**：随机树里「全左链」这种极端形态出现概率极低，而它恰好是本章唯一的高危 bug 形态。**测试要贴 bug 的形状，不是贴样例。**

---

## 067.6 复杂度与常见坑

### 067.6.1 复杂度推导

- **BFS：时间 O(n)** —— 每个节点入队一次、出队一次，`i == size-1` 判断是 O(1)；内层 `for` 的总迭代次数是 `n`。
- **BFS：空间 O(w)** —— 队列最多同时持有最宽一层的节点（外加下一层正在入队的那部分）；`ans` 本身是 O(h) 的输出空间。
  - 完全二叉树 `w = O(n)`；链状树 `w = O(1)`。
- **DFS：时间 O(n)** —— 每个节点进入一次，`depth == ans.size()` 是 O(1)。
- **DFS：空间 O(h)** —— 递归栈深等于树高；`n` 个节点的链状树是 O(n)。**这是本题唯一需要提醒的风险**：题目 `n ≤ 100` 不会爆，但同样代码搬到 `n = 10⁵` 的链上会 `StackOverflowError`。

| 方案 | 时间 | 辅助空间 | `n = 100` 全左链 | `n = 10⁵` 全左链 |
| --- | --- | --- | --- | --- |
| BFS + 快照 | O(n) | O(w) = O(1) | 安全 | 安全 |
| DFS 右优先 | O(n) | O(h) = O(n) | 安全（100 层） | **爆栈风险** |
| 先求完整层序 | O(n) | O(n) | 安全但浪费 | 安全但浪费 |

### 067.6.2 七个真实踩过的坑

1. **把「右视图」当成「最右链」** —— 本章第一杀手。反例 `[1,2,3,4]`：正解 `[1,3,4]`，错解 `[1,3]`。
2. **用 `q.isEmpty()` 判本层结束** —— 只在整棵树结束时成立，结果只剩一个值（树最右下的叶子）。
3. **没拍 `size` 快照，直接写 `i == q.size()-1`** —— 队列在变长，判据跟着漂移，记录数量随机出错。
4. **DFS 写成先左后右还保留 `depth == ans.size()`** —— 得到的是**左视图**（每层最左），不是右视图。两行 `dfs` 的顺序就是语义本身。
5. **DFS 里忘了 `depth == ans.size()` 用「等于」而不是「小于等于」** —— 写成 `depth >= ans.size()` 时，若某层为空（不会发生，但链状树 + 深度跳跃时会）会越界；更常见的错是写成 `depth > ans.size()`，导致第一层开始就漏记。
6. **空树没判，直接 `q.offer(root)`** —— Java `ArrayDeque` 禁止 `null`，抛 `NullPointerException`；Go 里塞 `nil` 不报错，但随后 `cur.Val` 直接 panic。
7. **拿 `ans.get(ans.size()-1)` 当「本层最右」之前先 add 了整层** —— 顺序一乱就取错层；正确姿势是「判据在弹出时判断」，而不是「事后从层结果里取」。

### 067.6.3 面试官最爱追问的三个问题

**Q1：右视图为什么不能靠沿 `right` 指针走？**

> 「因为右视图是**层的属性**，不是**树的连边属性**。反例 `root = [1,2,3,4]`：`4` 是 `2` 的左孩子，只在 `right` 指针上走会完全看不到它，可它确实是第 2 层唯一的节点。右视图正确定义是『每层最右节点』，所以必须知道层的信息 —— 队列或 `depth` 参数都行，但**必须按层**。」

**Q2：BFS 和 DFS 两个版本，你选哪个？为什么？**

> 「两者都是 O(n)。BFS 的空间是 O(w)，最宽层；DFS 是 O(h)，树高。**树宽选 BFS，树深选 DFS**。语义上 BFS 的 `i == size-1` 直接对应『每层最右』，更不容易写错，所以我默认给 BFS；如果面试官接着问『不用队列呢』，我就给右优先前序 + `depth == ans.size()` 的版本，并解释为什么『右优先时每层第一个到达者就是最右』。」

**Q3：如果要求返回每层最右那个**节点本身**（而不是值）？**

> 「BFS 里把 `ans.add(cur.val)` 换成 `ans.add(cur)` 就行。但要注意：返回节点引用意味着**调用方可能改它**；如果树是共享只读的，应该深拷贝或只返回值。DFS 版把 `cur.val` 换成 `cur` 即可。」

**Q4（高频变体）：怎么顺手改成「树左下角的值」（LC 513）？**

> 「把两行递归调换成**先左后右**，并把 `depth == ans.size()` 的记录方式改成『每层覆盖写』（`ans[depth] = val`）—— 左优先时同层访问是从左到右，最后一个覆盖进去的就是最左；而层号更大时 `ans` 会变长，最终剩下的最深一层的左端就是答案。**同一套代码，换的只有两行顺序和一个赋值方式。**」

---

## 067.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 102 二叉树的层序遍历](https://leetcode.cn/problems/binary-tree-level-order-traversal/) | 中等 | 066：收集整层；本章是它的「只留末位」版 |
| [LC 513 找树左下角的值](https://leetcode.cn/problems/find-bottom-left-tree-value/) | 中等 | 左优先 + 覆盖写；比本章少一个「首次到达」的判据约束 |
| [LC 515 在每个树行中找最大值](https://leetcode.cn/problems/find-largest-value-in-each-tree-row/) | 中等 | 每层维护 `max`，同样不需要整层结果 |
| [LC 637 二叉树的层平均值](https://leetcode.cn/problems/average-of-levels-in-binary-tree/) | 简单 | 每层求和 / `size`，注意 `double` |
| [LC 103 二叉树的锯齿形层序遍历](https://leetcode.cn/problems/binary-tree-zigzag-level-order-traversal/) | 中等 | 层内顺序交替，但「每层最后一个是哪个」的判据不变 |
| [LC 545 二叉树的边界](https://leetcode.cn/problems/boundary-of-binary-tree/) | 中等 | 右视图只是「右边界」的一半；还要配左边界 + 叶子 |
| [LC 1302 层数最深叶子节点的和](https://leetcode.cn/problems/deepest-leaves-sum/) | 中等 | 层序遍历到最后一层再求和；也可 DFS 双参数（最大深度 + 和） |
| [LC 116 填充每个节点的下一个右侧节点指针](https://leetcode.cn/problems/populating-next-right-pointers-in-each-node/) | 中等 | 把「层内相邻」写成指针，是「右视图」的连续版 |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 513 找树左下角的值**
1. 本章的 DFS 版靠「先走哪边」决定「第一个到达的是谁」；要最左，该先走哪边？
2. 「最深一层的第一个」和「每一层都覆盖一次」这两种记录方式，哪种代码更短？

**LC 515 每行最大值**
1. 你还需要把整层收集下来吗？只需维护一个随弹出更新的变量，它初值该取什么才不会和负数冲突？
2. 如果换成 DFS + `depth` 下标，桶的容量怎么定？

**LC 1302 最深叶子层和**
1. 先用一次遍历确定最大深度，还是一边遍历一边记录「当前最深」？
2. 当遇到更深的层时，之前累加的和该怎么处理？

**通用**
- 题目说「从右侧/左侧看」时，第一件事是把这句话翻译成「每层的哪一个位置」。
- 一个判据够不够用，看它是否能**独立于队列长度变化** —— 这就是快照的价值。

</details>

---

## 附：本章小结（可背走的三句话）

```
① 右视图 = 每层最右节点，不是「沿 right 指针的那条链」
② BFS：快照 size 后取 i == size-1；DFS：右优先前序 + depth == ans.size() 首次记录
③ 一行判据的差别就是「左视图 / 右视图 / 左下角」，本章真正的产出是「视角互换」这个语言
```

> **下一章**：[`068-maximum-depth-of-binary-tree.md`](068-maximum-depth-of-binary-tree.md) —— 从「按层看」回到「自底向上算」，并给出全书最重要的一张表：**自顶向下传参 vs 自底向上返回值**。
