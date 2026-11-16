# 066. LC102 二叉树的层序遍历（Binary Tree Level Order Traversal）

> **状态**：`已完成（初稿）` ｜ **所属**：第 8 部分 · 树形结构 ｜ **编号**：066 / 150
> **难度**：中等（本体不难，但「按层处理」这套模板是 067 / 103 / 107 / 199 / 515 / 637 的共同地基）｜ **标签**：树 · 广度优先搜索 · 队列 · 二叉树
> **一句话**：这道题真正考的是「**用一个 `size` 快照，给一条还在不断变长的队列划出一条不动的分界线**」，不是「会写一个 queue」。
> **前置**：[`065-binary-tree-inorder-traversal.md`](065-binary-tree-inorder-traversal.md)（递归三问、三种遍历的术语定义处，本章沿用它的术语体系）

---

## 066.1 题目

**原题链接**：<https://leetcode.cn/problems/binary-tree-level-order-traversal/>

### 题意重述（把隐含条件补齐）

给你一棵二叉树的根节点 `root`，请你返回它节点值的**逐层遍历**结果：第 0 层（根）单独一组，第 1 层（根的两个孩子）从左到右一组，第 2 层从左到右一组，直到最深一层。返回值是「组的列表」，不是扁平数组。

一句话就说完的题，藏了五个必须读出来的约束：

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| 结果是**两层结构** `List<List<Integer>>` | 「逐层返回」「每一层的节点值」 | 返回扁平的 `[3,9,20,15,7]` → 返回类型都不对，直接判错 |
| 每层内部**从左到右** | 样例 `[[3],[9,20],[15,7]]` | 先压右孩子再压左孩子 → 得到 `[[3],[20,9],...]` |
| 空树返回**空列表** `[]` | 样例 `root = []` | 返回 `[[]]`（含一个空层）→ 判错；这是本题最高频的失分点 |
| 节点值**可以重复**（如两个 `1`） | 约束没有「互不相同」 | 想用值做「已访问」标记 → 漏节点 |
| 返回的是**值**，不是节点 | 「节点值」 | 返回 `List<List<TreeNode>>` → 判题拿不到结果 |

> 📌 **本题在全书里的位置**：065 把「深度优先 + 栈」的三种遍历讲完，本章第一次把方向掰到「**广度优先 + 队列**」。从此树题分两条主线：
> **DFS 主线**（065 中序 / 144 前序 / 145 后序 → 068 深度 / 069 翻转 / 070 对称 / 071 直径 / 072 回溯 / 073 前缀和 / 074 树形 DP）
> **BFS 主线**（066 分层 → 067 右视图 → 103 之字形 → 107 自底向上 → 515 层最大值 → 637 层平均 → 116/117 层内 next 指针）。
> 两条主线的「遍历术语」都沿用 065：**递归 / 显式栈 / Morris** 属于 DFS 侧，**队列按层**属于 BFS 侧，两边不混用词。

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| 节点数 `[0, 2000]` | 最多 2000 个 | O(n) 与 O(n·h) 都能过（`2000² = 4·10⁶`），所以**本题不考时间，考模板的正确性**：能不能一次写对、能不能直接复用到 067 |
| `-1000 <= Node.val <= 1000` | 有负数 | 不能用 `val > 0` 当「本层结束」的哨兵 |
| 树不保证平衡 | 可能是全左链 / 全右链 | 递归写法在高 2000 的链上正好卡在 Java 默认栈边缘；队列写法则完全无栈深问题 |
| 每层从左到右 | 与入队顺序绑定 | 必须**先左后右**入队，顺序就是答案顺序 |

> 💡 **量级 → 算法**：`n ≤ 2000` 时任何 O(n) 写法都够。
> 但这道题的真实门槛不在复杂度，而在**「一次写对 + 可复用」**：066 写歪一个 `size`，067 / 103 / 107 / 199 会一起歪。
> 所以本章请把「**快照模板**」背到能默写，而不是每次都重新推。

### 示例

```
示例 1
输入：root = [3, 9, 20, null, null, 15, 7]
输出：[[3], [9, 20], [15, 7]]

              3                 ← 第 0 层
           ┌──┴──┐
           9     20             ← 第 1 层（从左到右）
                ┌─┴─┐
               15   7            ← 第 2 层（从左到右）

示例 2
输入：root = [1]
输出：[[1]]

示例 3
输入：root = []
输出：[]                        ← 注意：不是 [[]]
```

### 我自造的三个反例（写完必须拿它们打一遍）

```text
① 空树 []                        → 暴露「先 q.offer(root) 再判空」的 NPE，以及返回 [[]] 的错
② 全左链 4→3→2→1（只有 left）     → 暴露「只处理右孩子」「size 用了动态 len」
③ 全右链 1→2→3（只有 right）      → 暴露「先右后左入队」导致层内顺序反了
```

第 ② 条最阴：全左链上每一层都只有 1 个节点，如果 `size` 取错，结果会出现「`[[4,3],[2],[1]]`」这种一层塞了两个人的形态 —— **它看起来很像正确答案，只有数一遍每层节点数才发现不对**。

---

## 066.2 从暴力到最优：第一个念头为什么错

### 第一个念头：用一个队列把节点吐出来

```java
Deque<TreeNode> q = new ArrayDeque<>();
q.offer(root);
while (!q.isEmpty()) {
    TreeNode cur = q.poll();
    ansFlat.add(cur.val);              // ← 只得到扁平数组
    if (cur.left != null)  q.offer(cur.left);
    if (cur.right != null) q.offer(cur.right);
}
```

- 时间 **O(n)**，空间 **O(w)**（`w` 为最大层宽）。
- 它**没有错**，但它只解决了「怎么把树拍平」，完全丢了「哪一层」这个信息。题目要的是两层结构，这个写法交不上去。
- 这一步真正有价值的地方是：它证明了「队列确实能把树按层序吐出来」。剩下的问题只剩一件事 —— **在哪切刀**。

### 第二个念头：用 `len(q)` 当本层边界（最常见的真实 bug）

```java
while (!q.isEmpty()) {
    List<Integer> level = new ArrayList<>();
    for (int i = 0; i < q.size(); i++) {     // ❌ 每轮都重新求 q.size()
        TreeNode cur = q.poll();
        level.add(cur.val);
        if (cur.left != null)  q.offer(cur.left);
        if (cur.right != null) q.offer(cur.right);
    }
    ans.add(level);
}
```

这段代码在**每一层只有一个节点**的树上能通过所有样例，但在「根有 4 个孙辈」的树上就直接崩：循环条件里的 `q.size()` 会随着 `offer` 一起长大，`i` 永远追不上它，**要么把下一层节点混进本层，要么死循环**。

```
关键区别只有一处：
  ❌ for (int i = 0; i < q.size(); i++)   ← 边界是"活的"，每轮重算
  ✅ int size = q.size();                 ← 边界是"死的"，先拍快照
     for (int i = 0; i < size; i++)
```

### 第三个念头：反复求「第 k 层」

一个纯暴力视角：写一个 `nodesAtLevel(root, k)` 递归，然后 `for k in 0..height: ans.add(nodesAtLevel(root,k))`。

- 时间 **O(n·h)**：`n = 2000`、链状树 `h = 2000` 时是 **4·10⁶ 次访问**，能过；但 `n = 10⁵` 就是 **10¹⁰ 次**，约 30 秒，必然 TLE。
- 它每次只为了取一层就从头走一遍，**同一批节点被走了 `h` 遍** —— 这就是本章要去掉的重复劳动。

### 卡点在哪

把三个念头放在一起看，卡点非常清楚：

> **队列里同时装着「还没处理的当前层」和「刚开始入队的下一层」，而队列头指针一直在往前跑。**
> 你需要的是一条**不会跟着入队一起移动的分界线**。

### 灵感的来源 —— 「每一轮开始时，队列里恰好只有一层」

观察一下队列内容的变化（用示例 1 的树）：

```
轮次 0 开始：  [3]               ← 队列里只有第 0 层
轮次 1 开始：  [9, 20]           ← 队列里只有第 1 层
轮次 2 开始：  [15, 7]           ← 队列里只有第 2 层
轮次 3 开始：  []                ← 结束
```

**每一轮开始时，队列里的元素恰好是「同一层的全部节点」**。既然这样，只要在进入这一轮时把**元素个数**记下来，这一段就是本层；等这一段全部弹出并展开完孩子，队列里剩下的自然就是下一层。

```
不变量（本章正确的全部依据）：
  进入第 d 轮 while 前，队列中从左到右恰好是第 d 层的全部节点，且不含任何其他节点
```

一行 `int size = q.size();` 代价是 O(1)，换来的是「层次边界」，这就是全部的巧思。

---

## 066.3 原理拆解

### 066.3.1 数据结构与算法选型

| 方案 | 时间 | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- |
| **BFS + `size` 快照** ✅ 首选 | O(n) | O(w) | 10 行 | 需要「从最深一层往回输出」时要额外反转（见 107） |
| BFS + 双队列（`cur` / `next`） | O(n) | O(w) 但常数 ×2 | 14 行 | 多一个队列对象，每轮要交换引用；出错点更多 |
| 把 `(node, level)` 打包入队 | O(n) | O(n) 额外结构体 | 12 行 | 每个节点多带一个 int，且要自己判「level 变了没」；GC 压力更大 |
| DFS 递归 + `depth` 下标建桶 | O(n) | O(h) 栈 | 8 行 | 树极深（`h = n`）时爆栈；输出顺序**不是**遍历顺序，靠 depth 归位 |
| 反复求第 k 层 | O(n·h) | O(h) | 15 行 | `h` 大时平方级爆炸，`n = 10⁵` 链状树直接 TLE |
| Morris 变体层序 | O(n) | **O(1)** | 30+ 行 | 需要「判断一层结束」的线索技巧，只适合被追问「O(1) 空间层序」时使用 |

**为什么 `size` 快照优于双队列**：

- 双队列要维护两个变量、每轮末尾交换，读代码的人得同时在脑子里跟踪两条队列。
- 快照版只有一条队列，「本层」是队列的一个**前缀片段**，语义比「第二条队列」更直接。
- 时间空间量级完全相同，**省下的是出错概率**，这在笔试里就是分数。

**为什么 BFS 是本题的天然选择（而不是「DFS 也能做所以随便」）**：

层序是「**按距离根的边数**分组」，BFS 的性质正是「按距离分层扩展」，二者天然对齐；DFS 得到的是「按深度优先次序」的序列，要额外记录 depth 再归位，多一份状态。

| 维度 | BFS（队列） | DFS（递归/显式栈，065 的术语） |
| --- | --- | --- |
| 访问顺序 | 按层，天然就是答案顺序 | 按深度，必须靠 depth 归位 |
| 需要的额外状态 | 一个 `size` | 一个 `depth` 参数 + 容量足够的桶 |
| 空间 | O(w)，最宽层；链状树只有 O(1) | O(h)，最深路径；链状树 O(n) |
| 擅长的问题 | 最短路 / 分层统计 / 右视图 | 路径 / 子树聚合 / 自底向上返回值（068 / 071） |

### 066.3.2 为什么这个做法是对的（正确性论证）

**命题**：算法输出的第 `d` 个内层列表，恰好是从左到右的第 `d` 层节点值。

用 §066.2 的不变量做归纳：

1. **初始化**：`d = 0` 那轮之前，队列里只有 `root`，而第 0 层就是 `[root]`，成立。
2. **保持**：设进入第 `d` 轮前不变成立，即队列中恰为第 `d` 层的节点 `v₁, v₂, …, v_k`（从左到右）。取快照 `size = k`，然后循环 `k` 次：
   - 第 `i` 次弹出 `v_i`，把 `v_i` 的值追加进本层结果 —— 弹出顺序就是从左到右，所以本层结果正确；
   - 同时按「先左后右」的顺序把 `v_i` 的非空孩子入队。因为 `v_i` 从左到右遍历，而孩子的入队顺序是左再右，所以入队完成的序列恰好是第 `d+1` 层从左到右的顺序。
   - 循环跑满 `k` 次后，第 `d` 层全部弹出、第 `d+1` 层全部入队，且队列中没有别的元素 —— 不变量在 `d+1` 轮成立。
3. **终止**：队列空时，意味着某轮的第 `d+1` 层为空，也就是树的最大深度就是 `d`，所有节点恰好被输出一次。∎

**「为什么必须先左后右」的严格说法**：本题要求每层从左到右，而队列是先进先出 —— 入队顺序**就是**输出顺序。所以「先左后右」不是习惯，是**答案顺序的唯一保证**；把两行 `offer` 调换，算法就变成「层内从右到左」，即 199 右视图想要的那种顺序（067 会用到这个观察）。

### 066.3.3 通用解题思路（可迁移的那一层）

#### ① 先把「本书的树递归语言」立起来（后续 067–081 全靠它）

065 已经给了树递归三问，本章第一次让它们和「非递归」正面碰撞，这里正式固定下来：

| 三问 | 在 BFS 题里的表现 | 在 DFS 题里的表现 |
| --- | --- | --- |
| **问 1 做什么** | 「按层收集」这个动作靠**队列的轮次**表达，不靠递归位置 | 靠「记录自己」放在递归的哪一步表达（前 / 中 / 后序） |
| **问 2 何时停** | `root == null` 时直接返回空列表（**不是**空层） | `node == null` 时返回空单位元（0 / -1 / 0 取决于题，见 068 / 071） |
| **问 3 返回什么** | 不需要向上交付，答案直接在全局累积 | 必须说清「向父节点交付什么」（071 就死在这里） |

> 🧭 本章只需记住：**BFS 的「三问」退化成两问** —— 因为没有递归调用，就没有「返回值」这一层。这恰好是 BFS 有时比递归更好写的原因，也是它在 068 里可以和递归并列对照的原因。

#### ② BFS 按层模板（本批固定模板，067 直接复用）

```text
ans = []
if root == null: return ans                # 空树返回「空列表」，不是「含空层的列表」
q = 队列; q.offer(root)
while q 非空:
    size = q.size()                        # ★ 快照：本层节点数，之后 q 怎么变都不影响它
    本层 = []
    for i in 0 .. size-1:                  # ★ 只跑 size 次 → 只处理本层
        cur = q.poll()
        本层.add(cur.val)                  #   「做点什么」的位置（067 的右视图就改这一行）
        if cur.left  != null: q.offer(cur.left)     # ★ 先左
        if cur.right != null: q.offer(cur.right)    # ★ 后右
    ans.add(本层)
```

这套模板只有三个可替换的空位，**换空位不换骨架**：

| 空位 | 换成什么 | 得到哪道题 |
| --- | --- | --- |
| 「本层」怎么收集 | 只留最后一个 | 199 右视图（067）、515 层最大值 |
| 「本层」插到答案哪一端 | 头部插入 / 最后整体反转 | 107 自底向上层序 |
| 每轮方向 | 奇数轮先右后左 | 103 之字形层序 |
| 队列换成栈 | 单栈 + 反转 / 双栈 | 145 后序（与 065 的栈写法呼应） |

#### ③ 递归写法（同一道题的第二种解法，本题要求至少两种）

```text
dfs(node, depth):
    if node == null: return
    if depth == ans.size: ans.add(新列表)     # 第一次到达这一层，开桶
    ans[depth].add(node.val)                  # 归位
    dfs(node.left,  depth + 1)
    dfs(node.right, depth + 1)
```

它把「层」变成了数组下标 `depth`，用**前序**保证同一层内也是从左到右（先左后右）。时间 O(n)，空间 O(h)。两条路的取舍见 066.3.1 的表，一句话：**结构对齐选 BFS，深度受限选 BFS，只想图省事可写递归**。

### 066.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| 只要**每层最右**那个 | 答案从 `List<List>` 变成 `List` | 模板里的「收集整层」换成「取本层最后一个」→ 067，一行改动 |
| 要求**之字形**（奇数层反向） | 层内顺序交替 | 保持入队顺序不动，只在**收集时**决定 add 到队尾还是队头（`offerFirst`），O(1) 交换方向 |
| 要求**自底向上** | 层的顺序反过来 | 把 `ans.add(level)` 换成 `ans.add(0, level)`（`LinkedList`），或全部算完再 `reverse`（`ArrayList` 不建议头部插入） |
| 树是 **N 叉** | 孩子个数不定 | `for (TreeNode c : node.children) q.offer(c)`，其余不动 → LC 429 |
| 要求**层内 `next` 指针**（116/117） | 不返回值，要改树 | 用 `size` 快照的同时记录 `prev`，把 `prev.next = cur` 串起来，空间可省到 O(1)（用已建好的 next 链代替队列） |
| 树**太宽放不下**（10⁷ 节点一层） | 内存爆炸 | 换 DFS + 外部落盘（每层一个文件）；或分批 BFS（每层只处理前 `B` 个） |

---

## 066.4 图解推演

### 066.4.1 一棵用来推演的树

```
                 3
              ┌──┴──┐
              9     20
                   ┌─┴─┐
                  15   7

层 0： 3
层 1： 9  20
层 2： 15 7
```

### 066.4.2 队列的「分界线」怎么移动

```
轮次 0： 队列 [3]                  size = 1
          ┌───┐
          │ 3 │  ← 本层片段（就 1 个）
          └───┘
          弹出 3，入队 9、20           →

轮次 1： 队列 [9, 20]              size = 2
          ┌────┬────┐
          │ 9  │ 20 │  ← 本层片段
          └────┴────┘
          弹出 9（无孩子）、弹出 20（入队 15、7）  →

轮次 2： 队列 [15, 7]              size = 2
          ┌────┬────┐
          │ 15 │ 7  │  ← 本层片段
          └────┴────┘
          弹出 15、7（都无孩子）    →

轮次 3： 队列 []                   → 结束
```

关键在于：**`size` 是在「本层片段」还完整的时候拍的照**，所以后面 `offer` 进来的孩子全部落在片段右边，不会被 `for` 数进去。

### 066.4.3 逐步状态表（行 = 轮次，列 = 变量）

| 轮次 d | 轮初队列 | `size` 快照 | 本轮依次弹出 | 本轮入队（先左后右） | 本轮 `level` | 轮末队列 | `ans` |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | `[3]` | 1 | 3 | 9, 20 | `[3]` | `[9,20]` | `[[3]]` |
| 1 | `[9,20]` | 2 | 9, 20 | 15, 7 | `[9,20]` | `[15,7]` | `[[3],[9,20]]` |
| 2 | `[15,7]` | 2 | 15, 7 | （无） | `[15,7]` | `[]` | `[[3],[9,20],[15,7]]` |

### 066.4.4 两种写法的路线对比

```
                     ┌─────────── 输入 root ───────────┐
                     │                                 │
        ┌────────────▼─────────────┐   ┌───────────────▼────────────┐
        │ BFS：队列 + size 快照    │   │ DFS：递归 + depth 下标     │
        │ 时间 O(n) / 空间 O(w)    │   │ 时间 O(n) / 空间 O(h)      │
        └────────────┬─────────────┘   └───────────────┬────────────┘
                     │                                 │
        输出顺序天然是层序                    输出顺序是前序，靠 depth 归位
        无栈深风险（宽树无忧）                 宽树省内存，但深链有爆栈风险
                     │                                 │
            【本题 / 067 / 103 首选】             【树深可控时的简洁写法】
```

> **记住这张图**：它说明 BFS 与 DFS 在本章**不是「谁更优」，而是「谁更贴合题目结构」**。到 068（最大深度）两者又各得一分，届时会给出一张同量级的对照表。

---

## 066.5 双语言实现

### 066.5.1 Java

```java
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;

// LeetCode 判题环境已内置 TreeNode，粘贴到网页时不要重复定义：
// class TreeNode {
//     int val;
//     TreeNode left, right;
//     TreeNode() {}
//     TreeNode(int val) { this.val = val; }
//     TreeNode(int val, TreeNode left, TreeNode right) { this.val = val; this.left = left; this.right = right; }
// }

class Solution {
    public List<List<Integer>> levelOrder(TreeNode root) {
        List<List<Integer>> ans = new ArrayList<>();
        // 空树必须返回「空列表」；返回 [空层] 是最常见的判错原因
        if (root == null) {
            return ans;
        }

        // 用 ArrayDeque 而不是 Stack：Stack 继承 Vector，每个操作都 synchronized（详见 065 §5.3）
        // 注意：ArrayDeque 不允许入队 null，所以下面每个 offer 都必须先判孩子非空
        Deque<TreeNode> q = new ArrayDeque<>();
        q.offer(root);

        while (!q.isEmpty()) {
            // ★ 快照：本层的节点数。后面 q 会不断变长，而这个 int 不会变，
            //   它就是我们给「本层」划出的那条不动的分界线
            int size = q.size();
            List<Integer> level = new ArrayList<>(size);   // 按 size 预分配，避免多次扩容

            for (int i = 0; i < size; i++) {
                TreeNode cur = q.poll();
                level.add(cur.val);
                // 先左后右：队列是先进先出，入队顺序就是本层从左到右的答案顺序
                if (cur.left != null) {
                    q.offer(cur.left);
                }
                if (cur.right != null) {
                    q.offer(cur.right);
                }
            }
            ans.add(level);
        }
        return ans;
    }
}
```

### 066.5.2 Go

```go
package main

// LeetCode 判题环境已内置该定义，本地编译时保留即可
type TreeNode struct {
	Val   int
	Left  *TreeNode
	Right *TreeNode
}

func levelOrder(root *TreeNode) [][]int {
	ans := make([][]int, 0, 16)
	if root == nil {
		return ans // 空树 → 空列表；注意不是 [][]int{{}}（含一个空层）
	}

	// 用「切片 + head 下标」当队列：如果写成 q = q[1:]，
	// 每轮都会把底层数组的头往后挪，元素无法回收，而且 append 扩容后旧数据被复制，
	// 在 10⁵ 规模下这属于典型的隐性 O(n) 内存放大。head 下标法整个循环只分配一次。
	q := make([]*TreeNode, 1, 1024)
	q[0] = root
	head := 0

	for head < len(q) {
		size := len(q) - head // ★ 快照：本层还剩几个节点
		level := make([]int, 0, size)
		for i := 0; i < size; i++ {
			cur := q[head]
			head++
			level = append(level, cur.Val)
			// 先左后右：决定层内从左到右
			if cur.Left != nil {
				q = append(q, cur.Left)
			}
			if cur.Right != nil {
				q = append(q, cur.Right)
			}
		}
		ans = append(ans, level)
	}
	return ans
}
```

**本地跑一遍（含四类边界）**：

```go
package main

import "fmt"

// 按 LeetCode 的层序数组建树：nil 表示空位，用 FIFO 顺序消费
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
		{},                              // 空树
		{1},                             // 单结点
		{3, 9, 20, nil, nil, 15, 7},     // 官方样例
		{4, 3, nil, 2, nil, 1},          // 全左链
		{1, nil, 2, nil, 3},             // 全右链
	}
	for _, c := range cases {
		fmt.Printf("%v → %v\n", c, levelOrder(build(c)))
	}
}
```

输出（可直接对照）：

```text
[] → []
[1] → [[1]]
[3 9 20 <nil> <nil> 15 7] → [[3] [9 20] [15 7]]
[4 3 <nil> 2 <nil> 1] → [[4] [3] [2] [1]]
[1 <nil> 2 <nil> 3] → [[1] [2] [3]]
```

### 066.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 队列容器 | `ArrayDeque<TreeNode>`，`offer/poll` | `[]*TreeNode` + `head` 下标 | Go 没有内置队列；用 `q = q[1:]` 会让底层数组无法回收，`head` 下标法更稳 |
| 能否存 `null`/`nil` | `ArrayDeque` **禁止** `null` | 切片可以存 `nil` | Java 必须先判孩子非空再 `offer`；Go 里即使塞了 `nil` 也不报错，但取 `.Val` 会 panic —— 同样必须先判 |
| 返回值类型 | `List<List<Integer>>`，每层 `new ArrayList<>(size)` | `[][]int`，每个内层 `make([]int,0,size)` | 两边都要**每层新建**内层容器，否则所有层会指向同一个被反复追加的对象 |
| 空结果 | `new ArrayList<>()`（不能是 `null`） | `make([][]int, 0, 16)`（不能是 `[][]int{{}}`） | 空树是本题第一失分点，两边都要显式处理 |
| 整数宽度 | `int` 恒 32 位 | `int` 在 64 位平台是 64 位 | 本题值域 `[-1000,1000]`，无影响；但节点数上到 10⁹ 时 Java 求和要换 `long` |
| 预留容量 | `new ArrayList<>(size)` | `make([]int, 0, size)` | 两者都能减少扩容；Go 的 `append` 在超过 cap 时按约 2 倍增长，故预估 cap 很划算 |

### 066.5.4 对拍测试（BFS 快照 vs 递归建桶，两套独立实现互验）

```go
package main

import (
	"math/rand"
	"testing"
)

// 参考实现：递归 + depth 下标建桶。它与 levelOrder 的代码路径完全不同，
// 只有「两边都对」才说明快照逻辑真的正确（同源实现互验没有意义）
func refLevel(node *TreeNode, depth int, ans *[][]int) {
	if node == nil {
		return
	}
	if depth == len(*ans) {
		*ans = append(*ans, []int{})
	}
	(*ans)[depth] = append((*ans)[depth], node.Val)
	refLevel(node.Left, depth+1, ans)
	refLevel(node.Right, depth+1, ans)
}

func refLevelOrder(root *TreeNode) [][]int {
	ans := [][]int{}
	refLevel(root, 0, &ans)
	return ans
}

func same2D(a, b [][]int) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if len(a[i]) != len(b[i]) {
			return false
		}
		for j := range a[i] {
			if a[i][j] != b[i][j] {
				return false
			}
		}
	}
	return true
}

// 随机造树：每个新节点随机挂到某个空槽，能覆盖左偏/右偏/完全/稀疏各类形状
func randTree(r *rand.Rand, n int) *TreeNode {
	if n == 0 {
		return nil
	}
	root := &TreeNode{Val: r.Intn(2001) - 1000}
	slots := []**TreeNode{&root.Left, &root.Right}
	for i := 1; i < n; i++ {
		j := r.Intn(len(slots))
		p := slots[j]
		*p = &TreeNode{Val: r.Intn(2001) - 1000}
		slots[j] = slots[len(slots)-1]
		slots = slots[:len(slots)-1]
		slots = append(slots, &(*p).Left, &(*p).Right)
	}
	return root
}

func TestLevelOrderRandom(t *testing.T) {
	r := rand.New(rand.NewSource(66))
	for iter := 0; iter < 20000; iter++ {
		n := r.Intn(30) // 0 个节点 = 空树，也一起测
		root := randTree(r, n)
		got := levelOrder(root)
		want := refLevelOrder(root)
		if !same2D(got, want) {
			t.Fatalf("iter=%d n=%d got=%v want=%v", iter, n, got, want)
		}
	}
}

// 边界专测：全左链 / 全右链——每层恰好一个节点，专门用来抓 size 快照写错
func TestLevelOrderChain(t *testing.T) {
	for n := 0; n <= 50; n++ {
		var leftRoot, rightRoot *TreeNode
		for i := n; i >= 1; i-- {
			leftRoot = &TreeNode{Val: i, Left: leftRoot}
			rightRoot = &TreeNode{Val: i, Right: rightRoot}
		}
		for _, root := range []*TreeNode{leftRoot, rightRoot} {
			got := levelOrder(root)
			if len(got) != n {
				t.Fatalf("n=%d 层数错误 got=%v", n, got)
			}
			for i, lv := range got {
				if len(lv) != 1 || lv[0] != i+1 {
					t.Fatalf("n=%d 第 %d 层错误 got=%v", n, i, got)
				}
			}
		}
	}
}
```

> **为什么参考实现要选「递归 + depth 建桶」**：它是本题的另一条合法解法（§066.3.3 ③），与 BFS 没有任何共享代码。两套独立实现同时给出同一答案，才叫对拍；如果两个实现都从快照模板抄来，测的只是「抄得一样不一样」。

---

## 066.6 复杂度与常见坑

### 066.6.1 复杂度推导（结论要能推）

- **时间 O(n)**：每个节点**恰好入队一次**（只有非空孩子才入队），也**恰好出队一次**。内层 `for` 的总迭代次数等于所有层的节点数之和，即 `n`。`q.size()` 是 O(1)（Java `ArrayDeque` 维护 `size` 字段；Go 里是 `len(q)-head`，同样 O(1)）。没有嵌套重复扫描，所以不是 O(n·h)。
- **空间 O(w)**，`w` 是**最宽的一层**的节点数：
  - 完全二叉树：最后一层约 `(n+1)/2` 个节点，所以 **O(n)**；
  - 链状树：每层 1 个，**O(1)**；
  - 结论：`O(w) ≤ O(n)`，两者都合法，关键看你会不会说出「上界 O(n)，实际取决于最宽层」。
  - 另外 `ans` 本身要存 `n` 个值，这部分是 O(n) 的**输出空间**，通常不计入辅助空间。
- **递归写法的空间是 O(h)**：栈深等于树高。`n = 2000` 的链状树 `h = 2000`，Java 默认栈约 1 万层调用，仍然安全；但如果把同一段代码搬到 `n = 10⁵` 的链状树上就危险了 —— 这就是 BFS 在「深树」场景胜出的地方。

| 方案 | 时间 | 辅助空间 | 全左链 n=2000 的表现 |
| --- | --- | --- | --- |
| BFS + size 快照 | O(n) | O(w) = O(1) | 层数 2000，每层 1 个，完全无压 |
| BFS + 双队列 | O(n) | O(w)·2 | 同上，多一个队列对象 |
| DFS 递归 + depth 桶 | O(n) | O(h) = O(2000) | 栈深 2000，安全但已到边缘 |
| 反复求第 k 层 | O(n·h) | O(h) | 4·10⁶ 次访问，能过但入不了「最优」 |

### 066.6.2 七个真实踩过的坑

1. **`for (int i = 0; i < q.size(); i++)`** —— 边界是活的，队列一边长一边追，层内混进下一层甚至死循环。修法：先把 `size` 存下来。这是本章**唯一的必考坑**。
2. **空树返回 `[[]]`** —— 少数派写法是「先建一个空层再判 root」，被判错。修法：`root == null` 立刻 `return ans`（此时 `ans` 是空列表）。
3. **Java `ArrayDeque` 里塞 `null`** —— `q.offer(root)` 在 `root == null` 时抛 `NullPointerException`（`ArrayDeque` 禁止 null 元素）。修法：判空放在最前面。
4. **先右后左入队** —— 每层内部从右到左，答案整层反了。这个 bug 在「每层只有一个节点」的树上测不出来，必须用满二叉树测。
5. **Go 里 `for _, n := range q` 同时 `append` 到 `q`** —— `range` 在循环开始时固定了切片头，`append` 扩容后写入新数组，原 `range` 看不见新元素，结果「少一层」或「顺序错乱」，而且**不会 panic**，极难查。修法：老老实实写 `for i := 0; i < size; i++` 配 `head` 下标。
6. **Go 里 `q = q[1:]` 当出队** —— 底层数组头部永远不回收，`append` 又在尾部增长，内存持续膨胀；`n` 大时是隐性 O(n) 浪费。修法：`head++`，必要时在 `head` 超过某阈值时整体压缩一次。
7. **所有层共用同一个 `level` 容器** —— 若把 `level` 提到循环外 `clear()` 重用，`ans` 里存的都是同一个对象的引用，最终所有层内容相同（Java）；Go 里如果 `level` 用 `level[:0]` 复用，`append` 复写底层数组，前面已存进 `ans` 的内层切片会被悄悄改掉。修法：每层新建。

### 066.6.3 面试官最爱追问的三个问题

**Q1：为什么 `size` 快照能保证不越层？**

> 「因为有一条不变量：**每轮 `while` 开始前，队列里恰好只有同一层的全部节点**。初始化时队列只有根，成立；一轮里我们只弹 `size` 次，弹一个就把它的孩子（属于下一层）追加到队尾，所以这一轮结束时，队头到队尾恰好是下一层的全部节点。归纳下来不变量永远成立。`size` 只是把这个不变量「取出来」存在一个 int 里 —— 关键是它一旦存下来就不会跟着队列变长而变。」

**Q2：空间到底是 O(n) 还是 O(w)？**

> 「准确说是 **O(w)，`w` 为最宽一层的节点数**，且 `w ≤ n`。所以写 O(n) 不算错，但写出 O(w) 说明你懂 BFS 的真实内存形态：链状树只有 O(1)，完全二叉树最后一层约 `n/2`，才是 O(n)。此外 `ans` 本身占 O(n) 的输出空间，通常单列。」

**Q3：不用队列，能写出 O(1) 空间的层序遍历吗？**

> 「可以，代价是复杂度换清晰度。思路有两种：① **Morris 式线索**——层序没有天然的『回家的路』，所以要用「上一层已经建好的 `next` 指针代替队列」（这就是 LC 116/117 的做法：靠父层链表遍历子层，空间 O(1)）；② 有了 `next` 指针后，只要在每层末尾补一个 `null` 来标记层边界。这两种写法都要求**允许改树**，只读场景下还得回到队列。」

**Q4（常见追问）：如果只要「右边的那个」，代码要改几行？**

> 「改一行：`level.add(cur.val)` 换成 `if (i == size - 1) ans.add(cur.val)` —— 因为 `size` 快照恰好告诉我们『谁是本层最后一个』。这正是 067 的做法，也是本章模板值得背下来的原因：**换一道题只换一个空位，骨架不换。**」

---

## 066.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 199 二叉树的右视图](https://leetcode.cn/problems/binary-tree-right-side-view/) | 中等 | 模板只留每层最后一个 `i == size-1`（067 全章展开） |
| [LC 107 二叉树的层序遍历 II](https://leetcode.cn/problems/binary-tree-level-order-traversal-ii/) | 中等 | 自底向上：每层插到答案头部，或算完整体 `reverse` |
| [LC 103 二叉树的锯齿形层序遍历](https://leetcode.cn/problems/binary-tree-zigzag-level-order-traversal/) | 中等 | 保持入队顺序不动，奇数层收集时改用 `offerFirst` / 最后 `reverse` |
| [LC 515 在每个树行中找最大值](https://leetcode.cn/problems/find-largest-value-in-each-tree-row/) | 中等 | 每层维护一个 `max`，不用收集整层，空间降到 O(1) 每层 |
| [LC 637 二叉树的层平均值](https://leetcode.cn/problems/average-of-levels-in-binary-tree/) | 简单 | 每层求和除以 `size`，注意用 `double` 防整数截断 |
| [LC 429 N 叉树的层序遍历](https://leetcode.cn/problems/n-ary-tree-level-order-traversal/) | 中等 | 把「左右孩子」换成 `for c in children`，骨架完全不变 |
| [LC 116 / 117 填充每个节点的下一个右侧节点指针](https://leetcode.cn/problems/populating-next-right-pointers-in-each-node/) | 中等 | 用 `size` 快照 + `prev` 指针串 `next`；进阶要求 O(1) 空间（用上一层链表当队列） |
| [LC 104 二叉树的最大深度](https://leetcode.cn/problems/maximum-depth-of-binary-tree/) | 简单 | 层序遍历的「层数」就是深度 → 068：同一题的 DFS 与 BFS 对照 |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 199 右视图**
1. 右视图想要的节点，在「按层从左到右」的序列里处于什么位置？
2. 如果不想额外收集整层，那么在 `for` 循环里，哪一个 `i` 值对应本层最后一个元素？

**LC 103 之字形**
1. 入队顺序需要跟着变吗？还是只有「往本层结果里放」的方向需要变？
2. Java 的 `LinkedList` 有 `addFirst`，`ArrayList` 没有 —— 这暗示了哪种容器？

**LC 107 自底向上**
1. 能否在完全不动 BFS 逻辑的前提下，只在最后做一次操作？
2. 如果每层都用头部插入，`ArrayList` 的复杂度会退化成什么？

**通用**
- 只要题目出现「每一层」「逐层」「按行」，先把 `size = q.size()` 写下来，再想「本层要算什么」。
- 层内顺序由**入队顺序**决定，层间顺序由**层结果插到哪一端**决定，这两件事互相独立，别混在一起改。

</details>

---

## 附：本章小结（可背走的三句话）

```
① 层序遍历 = 一条队列 + 一个 size 快照；size 一旦拍下，队列怎么长都不影响本层边界
② 空树返回 [] 而不是 [[]]；Java 的 ArrayDeque 禁止 null，孩子必须先判空再入队
③ 本模板只有三个空位（怎么收集本层 / 插到答案哪端 / 方向），换空位就得到 067、103、107、515、637
```

> **下一章**：[`067-binary-tree-right-side-view.md`](067-binary-tree-right-side-view.md) —— 把「本层最后一个」这个空位填上，验证本章模板的可复用性。
