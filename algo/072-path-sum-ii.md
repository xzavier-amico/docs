# 072. LC113 路径总和 II（Path Sum II）

> **状态**：`已完成（初稿）` ｜ **所属**：第 8 部分 · 树形结构 ｜ **编号**：072 / 150
> **难度**：中等（第一次出现「共享可变状态 + 撤销」，写错就是路径互相污染）｜ **标签**：树 · 深度优先搜索 · 回溯 · 二叉树
> **一句话**：这道题真正考的是「**递归是在共享一个 `path`，所以进门前加、出门后必须删**」，不是「累加求和」。
> **前置**：[`071-diameter-of-binary-tree.md`](071-diameter-of-binary-tree.md)（返回值的边界）、[`068-maximum-depth-of-binary-tree.md`](068-maximum-depth-of-binary-tree.md)（传参范式）

---

## 072.1 题目

**原题链接**：<https://leetcode.cn/problems/path-sum-ii/>

### 题意重述（把隐含条件补齐）

给你一棵二叉树的根节点 `root` 和一个整数 `targetSum`，找出**所有**「从根节点到叶子节点」的路径，使路径上所有节点值之和等于 `targetSum`，返回这些路径（每条路径是节点值的列表）。

五个必须读出来的约束：

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| 路径必须**从根开始** | 「从根节点到叶子节点」 | 只找「某段向下的路径」→ 那是 437 的题（073） |
| 终点必须是**叶子**（左右孩子都空） | 「到叶子节点」 | 中途某个节点和已经等于 `targetSum` 就收 → `[1,2], targetSum=1` 会多出一条 `[1]` |
| 值**可以是负数** | 约束 `-1000 <= Node.val`、`-1000 <= targetSum` | 用「当前和 > targetSum 就剪枝」→ 漏解（见 §072.2） |
| 要**所有**解，不是一条 | 「找出所有路径」 | 找到一条就 `return` → 漏解 |
| 返回的是**路径本身** | 「返回这些路径」 | 只返回条数 → 判错（那是 437 的返回类型） |

> 📌 **本章在全书里的位置**：068 立了「自顶向下传参」，本章第一次让「传参」变成 **共享可变状态**：
> `path` 不是在调用之间按值传递的上下文，而是**全递归共用的一条列表**。共享就必须收拾现场 —— 这就是「**撤销选择**」。
> 本章的「进门前 `add`、出门后 `remove`」会一路服务到 096–107 整个回溯部分；而 073 会给出它的**免撤销变体**（用哈希计数代替列表），两章要对着看。

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| 节点数 `[0, 5000]` | 最多 5000 | 递归深度最坏 5000，Java 默认栈勉强够；O(n²) 的输出复制在 5000 规模下仍可接受 |
| `-1000 <= Node.val <= 1000` | **有负数** | 累加和不是单调的 → **任何基于「和太大就剪枝」的优化都是错的** |
| `-1000 <= targetSum <= 1000` | 目标也可能为负 | 不能用「和为正才有解」之类的假设 |
| 返回 `List<List<Integer>>` | 两层结构 | 每个答案都要一份**快照**，不能是共享引用 |

> 💡 **量级 → 算法**：`n ≤ 5000`，O(n) 的递归必然够；真正的成本不在搜索，而在**输出**：最坏情况（比如全 0 的树 + `targetSum = 0`）要输出 `O(n)` 条长度为 `O(h)` 的路径，总输出量 `O(n·h)`。
> 所以本题的正确复杂度表述是「**输出敏感**」：搜索 `O(n)`，加上拷贝答案 `O(答案总长度)`。

### 示例

```
示例 1
输入：root = [5,4,8,11,null,13,4,7,2,null,null,5,1], targetSum = 22
输出：[[5,4,11,2], [5,8,4,5]]

                    5
                ┌───┴───┐
                4       8
              ┌─┘     ┌─┴─┐
             11      13   4
            ┌─┴─┐       ┌┴┐
            7   2       5 1

  路径 5→4→11→2：5+4+11+2 = 22 ✅
  路径 5→8→4→5 ：5+8+4+5  = 22 ✅
  路径 5→8→4→1 ：5+8+4+1  = 18 ❌
  路径 5→4→11→7：5+4+11+7 = 27 ❌

示例 2
输入：root = [1,2,3], targetSum = 5
输出：[]              ← 没有任何根到叶路径和为 5（1+2=3、1+3=4）

示例 3
输入：root = [], targetSum = 0
输出：[]
```

### 我自造的三个反例（写完必须拿它们打一遍）

```text
① [1,2], targetSum = 1
   → 正解 []（1 不是叶子，它还有孩子 2）；错解会给出 [[1]]
   → 专治「不判叶子，和够了就收」

② [5,-3], targetSum = 2
   → 答案 [[5,-3]]：前缀和先到 5（已经超过目标 2），加上 -3 才落回 2；
     任何「前缀和超过 targetSum 就停」的剪枝都会误杀
   → 专治「负数下的非法剪枝」

③ 全左链 [1,2,3]（每个节点只有一个左孩子），targetSum = 6
   → 正解 [[1,2,3]]；若 path 忘了撤销，
     处理完左支再"回头"时会带上残留节点，输出变成 [[1,2,3],[2,3],[3]] 之类
   → 专治「忘记 remove」
```

第 ① 条最容易漏掉：**「和等于目标」和「到达叶子」是两个独立条件，必须同时满足。**

---

## 072.2 从暴力到最优：第一个念头为什么错

### 第一个念头：和等于 `targetSum` 就收集（不判叶子）

```java
void dfs(TreeNode node, int sum, List<Integer> path) {
    if (node == null) return;
    path.add(node.val);
    sum += node.val;
    if (sum == targetSum) ans.add(new ArrayList<>(path));   // ❌ 少了"是叶子"这个条件
    dfs(node.left, sum, path);
    dfs(node.right, sum, path);
    path.remove(path.size() - 1);
}
```

- 反例 `[1,2], targetSum = 1`：走到 `1` 时和已经是 `1`，于是收下 `[1]`；但 `1` 有左孩子 `2`，**不是叶子**，这条路径非法。正解是 `[]`。
- **修法**：条件改成 `sum == targetSum && node.left == null && node.right == null`。

### 第二个念头：用「当前和 > targetSum 就剪枝」

```java
if (sum > targetSum) return;   // ❌ 负数下必错
```

- 这个「优化」只在**所有节点值都非负**时成立（LC 112 的题面如果限定正数，很多人会加这一句）。
- 本章的值域含负数，累加和**不是单调的**：现在超过，后面加几个负数就可能正好落回目标。

```
反例轨迹（targetSum = 2）：
   5        ← 当前和 5 > 2 → 被剪枝 ❌
   │
  -3        ← 本可让和变成 2 ✅（路径 5→-3 的和 = 2）

结论：本题【不能】用和的大小做剪枝，只能老老实实走到叶子再判。
```

### 第三个念头：不用回溯，靠「每条路径自己带一份副本」

```java
void dfs(TreeNode node, int rest, List<Integer> pathSoFar) {
    if (node == null) return;
    List<Integer> path = new ArrayList<>(pathSoFar);   // 每次调用复制一份
    path.add(node.val);
    if (node.left == null && node.right == null && rest == node.val) ans.add(path);
    dfs(node.left,  rest - node.val, path);
    dfs(node.right, rest - node.val, path);
}
```

- **它完全正确**，而且不需要「撤销」—— 因为每个递归分支拿到的都是自己的副本，互不影响。
- 代价是**每一层都复制一次列表**：总代价 `O(n·h)`，链状树上 `O(n²) = 2.5·10⁷` 次整数拷贝（`n = 5000`）。能过，但比回溯版慢一个量级。
- 它的价值：**这是「回溯」更容易想明白的等价形式**。先把这版写出来，再把它优化成「共享 + 撤销」，是理解本章最顺的路径。

### 卡点在哪

> **卡点不在「怎么求和」，而在「怎么在一条共享的列表上，安全地反复进出」。**

DFS 一旦决定「所有递归共用一个 `path` 列表」，就必须处理三件互相关联的事：

| 问题 | 答错的后果 |
| --- | --- |
| 什么时候往 `path` 里加当前节点？ | 加晚了 → 判叶子时路径缺节点；加早了（在判空之前）→ 空节点也被加进去 |
| 什么时候从 `path` 里删掉当前节点？ | 忘了删 → 兄弟分支看到的是别人的路径（**污染**） |
| 收集答案时存引用还是副本？ | 存引用 → 所有答案都是同一个会被后续修改的列表（**全变成空列表或最后一条路径**） |

### 灵感的来源 —— 「递归是在借用一条走廊」

把 `path` 想成**一条共同的走廊**：每进入一个节点，就往走廊尽头放一块牌子；走完这个节点的整棵子树，必须**把自己那块牌子摘走**，否则下一个进来的兄弟会看到不属于它的牌子。

```
进入 node：  path.add(node.val)     ← 挂上自己的牌子
   ... 处理左子树、右子树 ...
离开 node：  path.remove(最后一块)   ← 摘掉自己的牌子（★ 必须与 add 配对）
```

由此得到一条**极好用的正确性判据（不变量）**：

> **对任意一次 `dfs(node, ...)` 调用：返回之后，`path` 的内容必须和调用前完全一样。**

这条不变量一旦成立，`path` 就永远是「根到当前节点」的准确快照，答案自然正确；它对**每一个**递归分支都成立，所以整棵树的搜索过程可以被「折叠」成一次次互不干扰的局部进出。

### 第四个念头：把 `sum` 改成「剩余量 `rest`」

这不是错误，而是**让边界更干净**的改写：

```text
传 sum  ：到达叶子时判  sum == targetSum
传 rest ：进入时 rest -= node.val，到达叶子时判  rest == 0
```

两者等价（`rest = targetSum - sum`），但 `rest` 版有三个好处：① 判据变成「等于 0」，少一个外部变量；② 不必在每层都拿 `targetSum` 做减法，减少一次减法口径的错误；③ 到叶子时 `rest == node.val` 也可以（进入前判）。本书统一用 `rest`。

---

## 072.3 原理拆解

### 072.3.1 数据结构与算法选型

| 方案 | 时间 | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- |
| **DFS + 共享 `path` + 撤销** ✅ 首选 | O(n) + 输出 | O(h) | 15 行 | 共享状态必须严格配对 `add/remove`，写漏即错 |
| DFS + 每层路径副本（无撤销） | O(n·h) + 输出 | O(h²) 峰值 | 14 行 | 链状树 `n = 5000` 时多 `2.5·10⁷` 次拷贝 |
| BFS：队列里存 `(节点, 剩余和, 路径副本)` | O(n·h) + 输出 | **O(w·h)** | 25 行 | 宽树时队列里塞着大量路径副本，内存最差 |
| 递归**返回**所有子树路径再拼接 | O(n·h) + 输出 | O(n·h) | 20 行 | 每层都在拼接新列表，常数最大 |
| 迭代 + 显式栈（手写回溯） | O(n) + 输出 | O(h) | 35 行 | 要在栈里同时保存「进入/退出」两个阶段，容易写错 |
| 只判存在（不返回路径） | O(n) | O(h) | 8 行 | 那是 112 的题；本题要所有路径 |

**为什么「共享 + 撤销」是首选**：

- 空间是 O(h) 而不是 O(h²)：任何时刻只有**一条**根到当前节点的路径存在；
- 时间上省掉了每层的列表复制，只在**命中答案**时才复制一次快照 —— 而复制答案是无法避免的（判题要求每个答案独立）；
- 它是「回溯」这一通用技术最干净的入门例子：**做选择 → 递归 → 撤销选择**，三行一一对应。

### 072.3.2 为什么这个做法是对的（正确性论证）

记 `dfs(v, rest, path)` 的语义为：

> 调用前 `path` 恰为「根 → `v` 的父节点」这条路径上的值序列，`rest = targetSum − sum(path)`。
> 调用结束时：把所有「`v` 的子树中、从 `v` 到某个叶子的路径」接在 `path` 后面、且总和为 `targetSum` 的那些路径加入 `ans`；并保证 `path` 恢复为调用前的样子。

**命题（正确性与不变量同时成立，对子树高归纳）**：

1. **基例**：`v == null`。直接返回，未改动 `path`，也没有产生任何路径 —— 与语义一致（空子树里没有根到叶的路径）。∎
2. **归纳假设**：对 `v` 的左右孩子 `l`、`r`，上述语义成立。
3. **归纳步**：
   - 先把 `v` 加入 `path`（此时 `path` = 根到 `v`，`rest' = rest − v.val`）。
   - **若 `v` 是叶子**：从 `v` 出发的「根到叶路径」只有 `path` 这一条，其和为 `targetSum − rest'`。故它命中当且仅当 `rest' == 0`，此时把 `path` 的**副本**加入 `ans`（副本保证后续修改不影响答案）。判据与 §072.1 的约束「终点必须是叶子」完全对应。∎
   - **若 `v` 非叶子**：对两支分别递归。由归纳假设，两支分别把所有合法路径收集完毕，且各自返回后 `path` 都恢复为「根到 `v`」。所以两支之间**互不污染**，两次递归可以任意顺序执行。
   - 最后 `path.remove(最后一项)`，把 `path` 恢复到「根到 `v` 的父节点」，满足「调用前后一致」的不变量。∎
4. 对所有节点成立，故从 `dfs(root, targetSum, 空列表)` 出发得到全部合法路径。∎

**为什么「撤销」是必需的（反证）**：假设某次调用返回后没有删除 `v`。则其兄弟分支（或祖先的兄弟分支）拿到的 `path` 里会多出 `v` 这个值，于是它收集到的是「根→…→`v`→兄弟子树」这种**不存在的路径**，答案非法；同时 `path` 长度会持续膨胀，最终可能越界或爆内存。所以「`add` 与 `remove` 必须成对」不是风格问题，而是正确性要求。∎

**为什么不能按和剪枝（反证）**：设 `v` 处当前和已超过 `targetSum`。若在 `v` 处直接返回，则所有经过 `v` 的路径都不会被检查；但只要 `v` 子树里存在足够多的负数节点，就可以让和重新等于 `targetSum`（§072.2 给了具体反例 `5 → -3`）。值域含负数时「和」不单调，任何基于单调性的剪枝都是错的。∎

### 072.3.3 通用解题思路（可迁移的那一层）

#### ① 回溯三段式（本章是它在树上的第一次亮相）

```text
dfs(node, rest):
    if node == null: return
    path.add(node.val)                 # ① 做选择（挂上牌子）
    rest -= node.val
    if node 是叶子 and rest == 0:
        ans.add(path 的副本)            # ★ 必须复制
    dfs(node.left,  rest)
    dfs(node.right, rest)
    path.remove(path.size() - 1)       # ② 撤销选择（摘掉牌子）→ 与 ① 严格配对
```

**三个「检查点」**，每一处都有对应的问题：

| 检查点 | 位置 | 要回答什么 | 本章答案 |
| --- | --- | --- | --- |
| 进入 | `add` 之后 | 「当前节点算进路径了吗？」 | 已算进，于是判叶子时路径完整 |
| 中止 | 判叶子时 | 「什么时候可以收答案？」 | 叶子 且 `rest == 0`（两个条件缺一不可） |
| 退出 | `remove` 之前 | 「现场恢复了吗？」 | 必须恢复，否则兄弟分支被污染 |

#### ② 在本书的树递归语言里，本章占据「范式一 + 共享状态」这一格

068 的对照表只说「传参 / 返回值」，本章要补上第三个维度：**传的东西是不是共享的**。

| 传的东西 | 是共享可变状态吗 | 需要撤销吗 | 代表题 |
| --- | --- | --- | --- |
| `int depth` / `int rest`（值类型参数） | 否（每次调用一份拷贝） | **不需要** | 068 传参版、077 中序计数 |
| `List<Integer> path`（引用类型、全递归共用） | **是** | **必须撤销** | **072 本章**、096 子集、079 序列化（收集路径） |
| `Map<Long,Integer> prefixCount`（全递归共用） | 是 | **必须撤销**（计数减一） | 073（下一章） |
| 返回值（`int` / `对象`） | 否（每层一份） | 不需要 | 068、071、074 |

> 🧭 **判据一句话**：**只要「往里加东西」的那个容器在两个兄弟分支之间是同一个对象，就必须在离开时把它恢复。**
> 值类型参数天然安全；引用类型参数必须自己管。

#### ③ 与 073 的分工预告（两章必须成对读）

| 维度 | 072 本章 | 073 下一章 |
| --- | --- | --- |
| 路径定义 | 必须从根到叶子 | 任意「向下」的一段（不需要从根、不需要到叶） |
| 维护的状态 | 整条 `path`（列表） | 只用「当前前缀和 + 哈希计数」 |
| 撤销什么 | 删掉最后一个元素 | 把当前前缀和的计数减一 |
| 判据 | 叶子 且 `rest == 0` | `count[prefix − target]` 累加 |

**两章的「撤销」是同一个思想，只是载体从列表换成了计数。**

### 072.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| 只问**存在**这样一条路径（112） | 不返回路径 | 丢掉 `path`，返回 `boolean`，并可在命中后**提前终止** |
| 只问**有多少条**（437） | 不返回路径，只要计数 | 换成「树上前缀和 + 哈希计数」→ 073，复杂度从 O(n·h) 降到 O(n) |
| 路径**不必从根开始**（437） | 路径定义放宽 | 同上（073）；本章的「从根」前提被移除后，必须用前缀和才不退化 |
| 允许**任意两节点**做端点（124） | 路径可拐弯 | 换成 071/124 的「返回单边 + 全局结算」范式 |
| 所有节点值**非负** | 和单调 | 此时可以加剪枝 `rest < 0 → return`，速度提升明显（面试里可作为加分点主动提出） |
| 结果**太大放不下内存** | 输出量爆炸 | 改成回调/生成器逐条产出（Java 用 `Consumer<List<Integer>>`，Go 用回调 `func([]int) bool`） |
| 树是 **N 叉** | 孩子不定 | `for c in children: dfs(c, rest)`，其余不变 |
| 值域放大到 `10^9` | 溢出 | Java 用 `long` 累加（`targetSum` 与 `sum` 都可能超 `int`） |

---

## 072.4 图解推演

### 072.4.1 用示例 1 走完整棵树

```
                    5
                ┌───┴───┐
                4       8
              ┌─┘     ┌─┴─┐
             11      13   4
            ┌─┴─┐       ┌┴┐
            7   2       5 1

targetSum = 22
```

用 `rest` 记号（每进一个节点就减去它的值）：

```
dfs(5, rest=22)  path=[5]     rest→17
├─ dfs(4, rest=17)  path=[5,4]  rest→13
│   ├─ dfs(11, rest=13)  path=[5,4,11]  rest→2
│   │   ├─ dfs(7, rest=2)  path=[5,4,11,7]  叶子，rest=2≠0 → 不收      ┐
│   │   │                                                   path 撤销 7 │
│   │   └─ dfs(2, rest=2)  path=[5,4,11,2]  叶子，rest=0 ✅ → 收下     │ ★ 第一条答案
│   │                                                       path 撤销 2 │
│   │                                                path 撤销 11      │
│   └─ dfs(null) 返回                                              │
│                                                path 撤销 4         │
└─ dfs(8, rest=17)  path=[5,8]  rest→9
    ├─ dfs(13, rest=9)  path=[5,8,13]  叶子，rest=9≠0 → 不收；撤销 13
    └─ dfs(4, rest=9)  path=[5,8,4]  rest→5
        ├─ dfs(5, rest=5)  path=[5,8,4,5]  叶子，rest=0 ✅ → 收下   ★ 第二条答案
        └─ dfs(1, rest=5)  path=[5,8,4,1]  叶子，rest=4≠0 → 不收
                                                path 撤销 4 / 8 / 5
ans = [[5,4,11,2], [5,8,4,5]]
```

### 072.4.2 `path` 的「走廊挂/摘牌子」轨迹（关键部分放大）

```
时刻                          动作              path
─────────────────────────────────────────────────────────────
进入 5                        add 5            [5]
  进入 4                      add 4            [5,4]
    进入 11                   add 11           [5,4,11]
      进入 7                  add 7            [5,4,11,7]
      离开 7                  remove → 7       [5,4,11]
      进入 2                  add 2            [5,4,11,2]   ★ 命中
      离开 2                  remove → 2       [5,4,11]
    离开 11                   remove → 11      [5,4]
  离开 4                      remove → 4       [5]
  进入 8                      add 8            [5,8]
    ...
```

注意最后：整棵树走完后 `path` 回到 `[]` —— **这就是「调用前后一致」不变量在根上的体现**。

### 072.4.3 步骤状态表（行 = 事件，列 = 变量）

| 事件 | `node` | `rest`（进入后） | `path` | 是叶子？ | `ans` |
| --- | --- | --- | --- | --- | --- |
| 进入 | `5` | 17 | `[5]` | 否 | `[]` |
| 进入 | `4` | 13 | `[5,4]` | 否 | `[]` |
| 进入 | `11` | 2 | `[5,4,11]` | 否 | `[]` |
| 进入 | `7` | −5 | `[5,4,11,7]` | 是 | `[]`（rest ≠ 0） |
| 退出 | `7` | — | `[5,4,11]` | — | `[]` |
| 进入 | `2` | 0 | `[5,4,11,2]` | 是 | `[[5,4,11,2]]` ★ |
| 退出 | `2` | — | `[5,4,11]` | — | `[[5,4,11,2]]` |
| 退出 | `11` | — | `[5,4]` | — | 同上 |
| 退出 | `4` | — | `[5]` | — | 同上 |
| 进入 | `8` | 9 | `[5,8]` | 否 | 同上 |
| 进入 | `13` | −4 | `[5,8,13]` | 是 | 同上（rest ≠ 0） |
| 退出 | `13` | — | `[5,8]` | — | 同上 |
| 进入 | `4` | 5 | `[5,8,4]` | 否 | 同上 |
| 进入 | `5` | 0 | `[5,8,4,5]` | 是 | `[[5,4,11,2],[5,8,4,5]]` ★ |
| … | | | | | |

### 072.4.4 忘撤销 vs 记得撤销（同一棵 [1,2,3] 链，targetSum=3）

```
       1
      /
     2
    /
   3

正确（撤销）：
  add 1 → add 2 → add 3 → （叶子，rest=0 ✅ ans=[[1,2,3]]）→ remove 3 → remove 2 → remove 1
  最终 ans = [[1,2,3]]

错误（忘 remove）：
  add 1 → add 2 → add 3 → ans=[[1,2,3]]
  返回上层时 path 仍是 [1,2,3]，兄弟分支拿到的是脏数据
  若再加一个右孩子，就很可能产出 [[1,2,3], [1,2,3,x]] 这类不存在的路径
```

### 072.4.5 三条路线对比

```
                  ┌────────── 输入 root, targetSum ──────────┐
                  │                                          │
      ┌───────────▼────────────┐          ┌──────────────────▼─────────────┐
      │ 共享 path + 撤销        │          │ 每层复制 path 副本              │
      │ O(n) + 输出 / O(h)      │          │ O(n·h) + 输出 / O(h²) 峰值      │
      └───────────┬────────────┘          └──────────────┬─────────────────┘
                  │                                      │
        add / remove 必须严格配对                无需撤销，天然隔离
        只在命中时复制一次                        每层都复制一次
                  │                                      │
          【工程首选，也是回溯的标准形态】      【更好理解，可作"为什么需要撤销"的对照组】
```

---

## 072.5 双语言实现

### 072.5.1 Java

**主解（DFS + 共享 `path` + 撤销）**：

```java
import java.util.ArrayList;
import java.util.List;

// LeetCode 判题环境已内置 TreeNode，粘贴到网页时不要重复定义：
// class TreeNode {
//     int val;
//     TreeNode left, right;
//     TreeNode(int val) { this.val = val; }
// }

class Solution {
    private final List<List<Integer>> ans = new ArrayList<>();
    private final List<Integer> path = new ArrayList<>();

    public List<List<Integer>> pathSum(TreeNode root, int targetSum) {
        dfs(root, targetSum);
        return ans;
    }

    // rest = targetSum - (path 中已有节点之和)
    private void dfs(TreeNode node, int rest) {
        if (node == null) {
            return;
        }

        // ① 做选择：把当前节点挂到走廊上
        path.add(node.val);
        rest -= node.val;

        // 中止判据：必须是叶子【并且】剩余量刚好为 0
        //   只判 rest == 0 → 会把"半路就凑够"的非法路径收进来
        //   只判叶子      → 会把"叶子但和不对"的路径收进来
        if (node.left == null && node.right == null && rest == 0) {
            // ★ 必须复制：ans 里若存的是 path 的引用，后面 remove 会把答案一起改掉
            ans.add(new ArrayList<>(path));
        }

        dfs(node.left, rest);
        dfs(node.right, rest);

        // ② 撤销选择：与 add 严格配对。少了这一行，兄弟分支会看到脏路径
        path.remove(path.size() - 1);
    }
}
```

**备解（无共享状态：每层传路径副本，不撤销）**：

```java
import java.util.ArrayList;
import java.util.List;

class Solution {
    public List<List<Integer>> pathSum(TreeNode root, int targetSum) {
        List<List<Integer>> ans = new ArrayList<>();
        dfs(root, targetSum, new ArrayList<>(), ans);
        return ans;
    }

    private void dfs(TreeNode node, int rest, List<Integer> path, List<List<Integer>> ans) {
        if (node == null) {
            return;
        }
        // 每个分支持有自己的副本：兄弟分支之间天然隔离，因此不需要撤销
        List<Integer> next = new ArrayList<>(path);
        next.add(node.val);
        if (node.left == null && node.right == null && rest == node.val) {
            ans.add(next);           // next 已经是独立对象，直接放进去即可
            return;
        }
        dfs(node.left,  rest - node.val, next, ans);
        dfs(node.right, rest - node.val, next, ans);
    }
}
```

### 072.5.2 Go

**主解（DFS + 共享 `path` + 撤销）**：

```go
package main

// LeetCode 判题环境已内置该定义，本地编译时保留即可
type TreeNode struct {
	Val   int
	Left  *TreeNode
	Right *TreeNode
}

func pathSum(root *TreeNode, targetSum int) [][]int {
	ans := [][]int{}
	path := []int{}

	var dfs func(node *TreeNode, rest int)
	dfs = func(node *TreeNode, rest int) {
		if node == nil {
			return
		}

		path = append(path, node.Val) // ① 做选择
		rest -= node.Val

		if node.Left == nil && node.Right == nil && rest == 0 {
			// ★ 快照必须复制：否则后面 path 的 append 会复用同一块底层数组，
			//   已经存进 ans 的"答案"会被后续节点覆盖（Go 里最阴的一个坑）
			snapshot := make([]int, len(path))
			copy(snapshot, path)
			ans = append(ans, snapshot)
		}

		dfs(node.Left, rest)
		dfs(node.Right, rest)

		path = path[:len(path)-1] // ② 撤销选择：长度减一即可，无需清零
	}

	dfs(root, targetSum)
	return ans
}
```

**备解（每层副本，不撤销）**：

```go
package main

func pathSumCopy(root *TreeNode, targetSum int) [][]int {
	var dfs func(node *TreeNode, rest int, path []int, ans *[][]int)
	dfs = func(node *TreeNode, rest int, path []int, ans *[][]int) {
		if node == nil {
			return
		}
		// 关键：这里复制出一份新的切片头 + 新数组，兄弟分支互不影响
		next := make([]int, len(path)+1)
		copy(next, path)
		next[len(path)] = node.Val

		if node.Left == nil && node.Right == nil {
			if rest == node.Val {
				*ans = append(*ans, next)
			}
			return
		}
		dfs(node.Left, rest-node.Val, next, ans)
		dfs(node.Right, rest-node.Val, next, ans)
	}
	ans := [][]int{}
	dfs(root, targetSum, nil, &ans)
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
	cases := []struct {
		tree   []interface{}
		target int
	}{
		{[]interface{}{}, 0},                                                                // 空树
		{[]interface{}{1}, 1},                                                               // 单结点（叶子）
		{[]interface{}{1, 2}, 1},                                                            // 根不是叶子 → 无答案
		{[]interface{}{5, 4, 8, 11, nil, 13, 4, 7, 2, nil, nil, 5, 1}, 22},                   // 官方样例
		{[]interface{}{5, -3}, 2},                                                           // 前缀和先超后回
	}
	for _, c := range cases {
		root := build(c.tree)
		fmt.Printf("tree=%v target=%d\n   回溯版=%v\n   副本版=%v\n",
			c.tree, c.target, pathSum(root, c.target), pathSumCopy(root, c.target))
	}
}
```

输出（对照用）：

```text
tree=[] target=0
   回溯版=[]
   副本版=[]
tree=[1] target=1
   回溯版=[[1]]
   副本版=[[1]]
tree=[1 2] target=1
   回溯版=[]
   副本版=[]
tree=[5 4 8 11 <nil> 13 4 7 2 <nil> <nil> 5 1] target=22
   回溯版=[[5 4 11 2] [5 8 4 5]]
   副本版=[[5 4 11 2] [5 8 4 5]]
tree=[5 -3] target=2
   回溯版=[[5 -3]]
   副本版=[[5 -3]]
```

### 072.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| `path` 的载体 | `List<Integer>`，`add` / `remove(size-1)` | `[]int`，`append` / `path = path[:len-1]` | Go 的 `append` 可能复用底层数组，**必须复制快照**；Java 的 `new ArrayList<>(path)` 天然复制 |
| 撤销的写法 | `path.remove(path.size() - 1)` | `path = path[:len(path)-1]` | Go 只改长度，被"删掉"的元素仍留在底层数组里 —— 所以千万别把 `path` 直接存进 `ans` |
| 快照复制 | `new ArrayList<>(path)` | `make` + `copy`（或 `append([]int(nil), path...)`） | 两边都必须复制；Go 忘复制是本章头号事故 |
| 全局状态的载体 | 字段 `ans` / `path` | 闭包外部变量 | Java 字段在同实例多调用时要清空，Go 闭包每次新建 |
| 判叶子的写法 | `node.left == null && node.right == null` | `node.Left == nil && node.Right == nil` | 语义一致，忌用「`val == 0` 之类」的假判据 |
| 返回二维切片 | `List<List<Integer>>` | `[][]int` | Go 的 `[][]int{{}}` 与 `[][]int{}` 不同：空答案要返回长度 0 的切片 |

### 072.5.4 对拍测试（暴力：先枚举所有根到叶路径，再筛和；真跑 20000 次）

```go
package main

import (
	"math/rand"
	"testing"
)

// 暴力参考：完全不看"和"，先把所有根到叶路径**全部枚举**出来，
// 然后逐条求和筛选。它与"边搜边算 rest"是两种不同的思路，适合当裁判。
func brutePathSum(root *TreeNode, targetSum int) [][]int {
	all := [][]int{}
	var walk func(n *TreeNode, cur []int)
	walk = func(n *TreeNode, cur []int) {
		if n == nil {
			return
		}
		next := make([]int, len(cur)+1)
		copy(next, cur)
		next[len(cur)] = n.Val
		if n.Left == nil && n.Right == nil {
			all = append(all, next) // 枚举：先不管和
			return
		}
		walk(n.Left, next)
		walk(n.Right, next)
	}
	walk(root, nil)

	ans := [][]int{}
	for _, p := range all {
		s := 0
		for _, v := range p {
			s += v
		}
		if s == targetSum {
			ans = append(ans, p)
		}
	}
	return ans
}

func samePaths(a, b [][]int) bool {
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

func randTreeVals(r *rand.Rand, n int) *TreeNode {
	if n == 0 {
		return nil
	}
	root := &TreeNode{Val: r.Intn(9) - 4} // 值域 [-4,4]，负值频繁出现
	slots := []**TreeNode{&root.Left, &root.Right}
	for i := 1; i < n; i++ {
		j := r.Intn(len(slots))
		p := slots[j]
		*p = &TreeNode{Val: r.Intn(9) - 4}
		slots[j] = slots[len(slots)-1]
		slots = slots[:len(slots)-1]
		slots = append(slots, &(*p).Left, &(*p).Right)
	}
	return root
}

func TestPathSumRandom(t *testing.T) {
	r := rand.New(rand.NewSource(113))
	for iter := 0; iter < 20000; iter++ {
		n := r.Intn(14)
		root := randTreeVals(r, n)
		target := r.Intn(13) - 6 // 目标也含负数
		want := brutePathSum(root, target)
		got := pathSum(root, target)
		if !samePaths(got, want) {
			t.Fatalf("iter=%d target=%d got=%v want=%v", iter, target, got, want)
		}
		if got2 := pathSumCopy(root, target); !samePaths(got2, want) {
			t.Fatalf("iter=%d 副本版 target=%d got=%v want=%v", iter, target, got2, want)
		}
	}
}

// 边界专测：非叶子的根不能收、全左链、全 0 树（答案最多）、负数摆回
func TestPathSumEdge(t *testing.T) {
	// ① 根不是叶子：targetSum = 1 时不能返回 [[1]]
	root := &TreeNode{Val: 1, Left: &TreeNode{Val: 2}}
	if got := pathSum(root, 1); len(got) != 0 {
		t.Fatalf("根不是叶子时不应有答案，got=%v", got)
	}

	// ② 全左链：只有一条路径
	chain := &TreeNode{Val: 1, Left: &TreeNode{Val: 2, Left: &TreeNode{Val: 3}}}
	got := pathSum(chain, 6)
	if len(got) != 1 || len(got[0]) != 3 || got[0][0] != 1 || got[0][2] != 3 {
		t.Fatalf("全左链应只有 [1 2 3]，got=%v", got)
	}

	// ③ 全 0 完全二叉树：每一条根到叶路径的和都是 0 → 答案条数 = 叶子数
	//    （必须用 fresh 节点递归构造；若写成 Left: zero, Right: zero 共享同一棵子树，
	//      那就不是一棵树了，而是一张有环的图 —— 这本身也是造测试数据的常见事故）
	var build0 func(depth int) *TreeNode
	build0 = func(depth int) *TreeNode {
		if depth == 0 {
			return nil
		}
		return &TreeNode{Val: 0, Left: build0(depth - 1), Right: build0(depth - 1)}
	}
	zero := build0(5)
	res := pathSum(zero, 0)
	if len(res) != 16 { // 深度 5 的满二叉树有 16 个叶子
		t.Fatalf("全 0 满二叉树应有 16 条路径，got=%d", len(res))
	}

	// ④ 负数摆回：不能用"和超了就剪枝"
	neg := &TreeNode{Val: 5, Left: &TreeNode{Val: -3}}
	if got := pathSum(neg, 2); len(got) != 1 || got[0][0] != 5 || got[0][1] != -3 {
		t.Fatalf("含负数时应有 [[5 -3]]，got=%v", got)
	}
}
```

> **为什么要专门测「全 0 满二叉树」**：它是**答案条数最多**的形态（每条路径都命中），最容易暴露「`path` 快照没复制」的 bug —— 如果 `ans` 里存的是同一块底层数组，16 条答案会全部变成最后那一条（或全部变成空）。

---

## 072.6 复杂度与常见坑

### 072.6.1 复杂度推导

- **时间 O(n + 输出量)**：每个节点被进入一次、退出一次，函数体是 O(1)（`add` 均摊 O(1)）。命中答案时要复制一条长度为 `h` 的路径，总代价是 `O(答案条数 × 平均路径长度)`。所以严格写是 **O(n + Σ|答案|)**，最坏 `O(n·h)`（例如全 0 树 + `targetSum = 0`）。
- **空间 O(h)**：`path` 最多 `h` 个元素，递归栈 `h` 层 —— 两者同阶，所以是 O(h)。答案本身的 `O(Σ|答案|)` 属于输出空间，通常单列。
- **副本版对照**：时间 `O(n·h)`（每层复制一次），峰值空间 `O(h²)`（每层都挂着一份副本）。

| 方案 | 时间 | 辅助空间 | `n = 5000` 全左链 |
| --- | --- | --- | --- |
| 共享 `path` + 撤销 | O(n + 输出) | O(h) | 1 次路径复制，栈深 5000（Java 边缘） |
| 每层副本 | O(n·h + 输出) | O(h²) | 约 `1.2·10⁷` 次拷贝 |
| BFS 队列存路径副本 | O(n·h + 输出) | O(w·h) | 队列里挂 1 条路径，尚可；宽树最差 |

### 072.6.2 八个真实踩过的坑

1. **不判叶子，和够了就收** —— `[1,2], targetSum=1` 会多出 `[1]`。判据必须是「叶子 **且** `rest == 0`」。
2. **忘记撤销（漏 `remove`）** —— 兄弟分支看到脏路径，答案出现不存在的路径；`path` 还会一路变长。**`add` 与 `remove` 必须成对出现，且中间不能有 `return` 绕过。**
3. **`ans.add(path)` 存了引用** —— Java 里所有答案都指向同一个 `List`，最后全变成空列表（因为最终 `path` 被清空）。必须 `new ArrayList<>(path)`。
4. **Go 里 `append` 复用底层数组导致快照被覆盖** —— 只 `ans = append(ans, path)` 而没复制，后续 `append` 会把已存答案改写。必须 `make + copy`。
5. **用「和 > targetSum 就剪枝」** —— 值域含负数时必错（反例 `5 → -3`）。**只有在题目保证节点值非负时才可以使用。**
6. **在 `node == null` 分支里 `return` 前忘了撤销** —— 如果 `add` 写在判空之前（比如先 `path.add(node.val)` 再判空），空节点也会入栈，且 `return` 时不撤销。**正确顺序：先判空，再 `add`。**
7. **命中后直接 `return` 而不撤销** —— 有些实现想在叶子处「提前结束」，但仍然必须先 `remove` 再返回，否则上层兄弟分支被污染。
8. **用 `int` 累加但值域放大** —— 本题 `n ≤ 5000`、值最大 1000，和最大 `5·10⁶`，`int` 安全；但若约束改成 `10^5` 个节点、值 `10^9`，必须换 `long`。

### 072.6.3 面试官最爱追问的三个问题

**Q1：为什么一定要「撤销」？不撤销会怎样？**

> 「因为所有递归调用共用同一个 `path` 对象。进入节点时它被加进去，如果不删除，这个节点就会一直留在列表里 —— 后面处理它的兄弟分支时，`path` 里就多了一份不属于该路径的数据，会产出**不存在的根到叶路径**。用一个不变量描述最准确：**『任何一次 `dfs` 调用返回后，`path` 必须和调用前完全一致』**。`add` 与 `remove` 一一配对，这条不变量就成立。」

**Q2：能不能用「和超过 targetSum 就剪枝」？**

> 「本题不能，因为节点值可以是负数，累加和**不单调**。反例：`targetSum = 2`，路径 `5 → -3` 的前缀和 `5` 已经超过目标，但加上 `-3` 就正好等于 2，剪掉就漏解。**只有当题目保证所有值非负时，`rest < 0 → return` 才成立** —— 面试里如果被问到「怎么优化」，正确答法是先确认值域，再决定能不能剪。」

**Q3：时间复杂度怎么说才准确？**

> 「搜索本身是 O(n)（每个节点进出一次），但答案要复制，所以是**输出敏感**的 `O(n + Σ|答案|)`，最坏 `O(n·h)`（例如全 0 的树、`targetSum = 0` 时每条路径都命中）。这个区分很重要：只报 O(n) 会低估复制成本，只报 O(n·h) 又漏掉了大多数输入根本拷不了几次。」

**Q4（常见追问）：如果只想知道「存不存在」这样一条路径（112）？**

> 「把 `path` 整个去掉，函数返回 `boolean`：到叶子时判 `rest == 0`，任一孩子返回 `true` 就立刻短路返回 `true`。空间降到 O(h)（只剩递归栈），而且能**提前终止** —— 这是本章这种『收集所有解』的问题做不到的（010 与 072 的核心差别就在这：收集所有解时，剪枝空间极小）。」

---

## 072.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 112 路径总和](https://leetcode.cn/problems/path-sum/) | 简单 | 只判存在：返回 `boolean`，可提前终止，不需要 `path` |
| [LC 437 路径总和 III](https://leetcode.cn/problems/path-sum-iii/) | 中等 | 073：路径不必从根开始、只求**条数**；用树上前缀和 + 哈希，O(n) |
| [LC 257 二叉树的所有路径](https://leetcode.cn/problems/binary-tree-paths/) | 简单 | 同样的回溯骨架，但**不收条件**（所有根到叶路径都要），输出字符串 |
| [LC 129 求根节点到叶节点数字之和](https://leetcode.cn/problems/sum-root-to-leaf-numbers/) | 中等 | 回溯时维护「已构成的数字」，命中叶子后累加，不需要保存路径 |
| [LC 988 从叶结点开始的最小字符串](https://leetcode.cn/problems/smallest-string-starting-from-leaf/) | 中等 | 回溯 + 字符串构造（Java 用 `StringBuilder` 撤销末位，Go 用 `[]byte`） |
| [LC 124 二叉树中的最大路径和](https://leetcode.cn/problems/binary-tree-maximum-path-sum/) | 困难 | 074：路径可拐弯、要最大 → 换 071 的「返回值 + 全局答案」范式 |
| [LC 110 平衡二叉树](https://leetcode.cn/problems/balanced-binary-tree/) | 简单 | 返回值带状态的另一例：用 `-1` 编码「已失衡」 |
| [LC 面试题 04.12 求和路径](https://leetcode.cn/problems/paths-with-sum-lcci/) | 中等 | 与 437 同题，可当作 073 的第二遍练习 |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 112 路径总和**
1. 只需回答「有没有」，那么某个分支一旦成功，还需要继续看别的分支吗？
2. 到叶子时的判据与本章相比，少了哪一部分（`path` 还留着吗）？

**LC 437 路径总和 III**
1. 路径不必从根开始 —— 那么「当前路径和减去某个祖先处的路径和」能得到什么？
2. 如果要把「从根到当前节点的和」反复查询，用什么结构能让查询变成 O(1)？离开节点时又要做什么？

**LC 129 数字之和**
1. 「已构成的数字」该怎么随深度变化？是 `x*10 + val` 还是先存列表再拼接？
2. 命中叶子后，还需要把 `x` 恢复成进入前的值吗？为什么？

**通用**
- 只要递归里有一个「所有分支共用」的容器，就问自己：离开这一层时，它恢复原样了吗？
- 答案里存的是**快照**还是**引用**？这一步决定了你的输出是正确结果还是一堆空列表。

</details>

---

## 附：本章小结（可背走的三句话）

```
① 判据是「叶子 且 rest == 0」两个条件缺一不可；和相同不代表路径合法
② 共享 path 就必须撤销：add / remove 严格配对，靠「调用前后 path 一致」这条不变量保证正确
③ 答案要存副本：Java new ArrayList<>(path)，Go make+copy —— 存引用/复用底层数组必翻车
```

> **下一章**：[`073-path-sum-iii.md`](073-path-sum-iii.md) —— 把「路径必须从根开始」这个前提拿掉，用一个哈希表把 O(n·h) 压到 O(n)。
