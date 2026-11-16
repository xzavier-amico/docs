# 070. LC101 对称二叉树（Symmetric Tree）

> **状态**：`已完成（初稿）` ｜ **所属**：第 8 部分 · 树形结构 ｜ **编号**：070 / 150
> **难度**：简单（但它是「单指针递归」升级成「**双指针递归**」的分水岭）｜ **标签**：树 · 深度优先搜索 · 广度优先搜索 · 递归 · 二叉树
> **一句话**：这道题真正考的是「**对称是两棵子树之间的关系，不是每一棵子树自己的性质**」，不是「比较根的两个孩子」。
> **前置**：[`069-invert-binary-tree.md`](069-invert-binary-tree.md)（翻转是本章的「单树自镜」，本章升级成「双树互相照镜」）

---

## 070.1 题目

**原题链接**：<https://leetcode.cn/problems/symmetric-tree/>

### 题意重述（把隐含条件补齐）

给你一棵二叉树的根节点 `root`，判断它是否**轴对称**：把树沿根节点竖直画一条中轴线，左半边与右半边能完全重合。

「重合」同时要求两件事：
1. **形状镜像** —— 左边在深度 `d`、第 `k` 个位置的节点，右边必须在深度 `d`、倒数第 `k` 个位置有对应节点；
2. **值相等** —— 每一对镜像位置上的节点值必须相同。

一句话就说完的题，藏了四个必须读出来的约束：

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| 对称是**关于根的镜像** | 「镜像对称」 | 只比较「根的左右孩子值相等」→ 深层错位全漏 |
| **形状**也要镜像 | 样例 `[1,2,2,null,3,null,3] → false` | 只比值不看结构 → 把这个样例判成 `true` |
| **空树算对称** | 样例 `root = [1] → true`、空树 → `true` | 空树返回 `false` → 判错 |
| 返回**布尔值**，不是树 | 「返回 `true` / `false`」 | 返回修改后的树 / 返回 `int` → 签名不符 |

> 📌 **本章在全书里的位置**：069 是「一棵树自己翻」，本章第一次出现**递归函数带两个指针参数**（`mirror(a, b)`）。
> 这个形态会一路复用到 100（相同的树）、572（另一棵树的子树）、617（合并二叉树）、951（翻转等价）、235/236 的变体。**双指针递归是本书树系列的第二把钥匙**（第一把是 068 的单指针 + 双范式）。

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| 节点数 `[1, 1000]` | 最多 1000 个 | 任何 O(n) 都过；`h ≤ 1000` 递归安全 |
| `-100 <= Node.val <= 100` | 有负数、可重复 | **不能**用「值 → 下标」去配对；重复值必须靠结构区分 |
| 树不保证平衡 | 可能是全左链 | 全左链一定不对称（右侧全空），是很好的快速反例 |
| 返回 `boolean` | 单个真假 | 不需要收集路径，也不需要额外容器 |

> 💡 **量级 → 算法**：`n ≤ 1000`，复杂度不是考点。
> 本题的考点只有一句话 —— **你能不能把「一棵树对称」翻译成「两棵子树互为镜像」**。
> 翻译对了，代码 8 行；翻译错了，就只能在「比较遍历序列」这类**丢失结构信息**的路上越走越远。

### 示例

```
示例 1
输入：root = [1, 2, 2, 3, 4, 4, 3]
输出：true

                1
             ┌──┴──┐
             2     2
           ┌─┴┐  ┌┴─┐
           3  4  4  3
           ↑  ↑  ↑  ↑
           └──┴──┴──┘  镜像配对：3↔3、4↔4

示例 2
输入：root = [1, 2, 2, null, 3, null, 3]
输出：false

                1
             ┌──┴──┐
             2     2
              \     \
               3     3
    形状不镜像：左边 3 挂在内侧，右边 3 挂在外侧

示例 3
输入：root = [1, 2, 2, 3, null, null, 3]
输出：true

示例 4
输入：root = [1]
输出：true          ← 单结点天然对称
```

### 我自造的三个反例（写完必须拿它们打一遍）

```text
① [1, 2, 2, 3, 3, 4, 4]        → 正解 false，但「只比值 + 各子树自对称」的错解会给 true
② [1, 2, 2, null, 3, null, 3]  → 结构不对称，专治「只比较值」
③ 全左链 1←2←3                  → 必须 false（右边全空），专治「没判 null 配对」
```

第 ① 条是最有杀伤力的：`[1,2,2,3,3,4,4]` 里，根的左右孩子都是 `2`（值配对成功），左右子树**各自**都左右对称（`2` 的孩子是 `3,3` 和 `4,4`），但它**整体并不对称** —— 因为左边是 `3,3`、右边是 `4,4`，镜像位置上值不同。**「各自对称」与「互相镜像」是两件事**，这就是本章的全部。

---

## 070.2 从暴力到最优：第一个念头为什么错

### 第一个念头：左子树「各自」对称 + 右子树「各自」对称 + 根的两个孩子值相等

```java
boolean isSymmetric(TreeNode root) {
    if (root == null) return true;
    if (root.left == null || root.right == null) return root.left == root.right;
    return root.left.val == root.right.val
        && isSymmetric(root.left)      // ❌ 检查的是"左子树自己是否对称"
        && isSymmetric(root.right);    // ❌ 检查的是"右子树自己是否对称"
}
```

- 时间 **O(n)**，空间 **O(h)**，写起来非常像对的。
- 反例 `[1,2,2,3,3,4,4]`：根的左右孩子都是 `2` ✅，左子树 `2(3,3)` 自对称 ✅，右子树 `2(4,4)` 自对称 ✅ → 返回 `true`，**正确答案是 `false`**。

```
       1                  ✗ 但"左边 3,3"与"右边 4,4"镜像位置不同
    ┌──┴──┐
    2     2
  ┌─┴┐  ┌┴─┐
  3  3  4  4
  └─┬─┘ └┬─┘
   自对称  自对称        ← 两个"自对称"拼起来，整体不一定对称
```

- 顺带它还错在**另一个方向**：`[1,2,2,3,4,4,3]`（真正的对称树）会被它判成 `false`，因为左子树 `2(3,4)` 自己并不自对称。**两向都错，说明它抓错了性质。**

### 第二个念头：把中序遍历（或层序）取出来，看是不是回文

```java
List<Integer> seq = new ArrayList<>();
inorder(root, seq);
for (int i = 0, j = seq.size() - 1; i < j; i++, j--)
    if (!seq.get(i).equals(seq.get(j))) return false;   // ❌
return true;
```

- 时间 **O(n)**，空间 **O(n)**。
- 它的想法很自然：对称树的「从左到右」序列应该和「从右到左」序列一样。但**遍历序列不是树的等价表示** —— 两棵结构完全不同的树可以有同样的中序序列。
- 具体反例（左子树是 `4→右→2→右→3` 的右链，右子树是 `2(左 3, 右 4)`）：

```
        1
     ┌──┴──┐
     4     2
      \   ┌┴┐
       2  3 4
        \
         3

整树中序：[4, 2, 3, 1, 3, 2, 4]  ← 是回文！
但它并不对称（左边是全右链，右边是"两根杈"）
```

- **根因**：中序序列丢掉了「谁是父、谁是子」的结构信息；`4,2,3` 和 `3,2,4` 这组序列可以由完全不同的两棵树产生。**任何「先序列化再比较序列」的方案，都必须保证序列能唯一确定树**（前序 + 空标记可以，见 079），中序单独不行。

### 第三个念头：翻转左子树，再判断两棵树是否相同

```java
TreeNode invertedLeft = invertTree(root.left);   // 复用 069
return sameTree(invertedLeft, root.right);        // 复用 068 的思路（相同的树）
```

- 时间 **O(n)**，空间 **O(n)**（新建了一整棵镜像树）或 O(h)（原地翻转，**但会破坏输入**）。
- **这个念头是完全正确的**，而且很好地串联了 069 与本章。它的唯一代价：
  - 原地翻：把用户给的树改坏了（`root.left` 结构被永久改变），除非题目允许；
  - 新建镜像：多 O(n) 个节点。
- 所以它是「**正确的次优解**」—— 面试里可以作为第二种解法说出来，正好用来解释「为什么双指针递归更好」（因为它一遍扫完、不改树、只多 O(h) 栈）。

### 卡点在哪

> **卡点：对称性不属于任何一棵子树，它属于「一对子树」。**

`isSymmetric(node)` 这个签名里只有一个位置，装不下「和谁比」。所以无论怎么递归，只要还在「对一棵子树判断对称」，就注定抓不到「跨左右」的配对关系。

### 灵感的来源 —— 「把一棵树的问题，改写成两棵树的问题」

把根摘掉，剩下两棵子树 `L` 和 `R`。整棵树对称，等价于：

```
L 与 R 互为镜像：
  ① L.val == R.val
  ② L.left  与 R.right 互为镜像
  ③ L.right 与 R.left  互为镜像
```

第 ② ③ 条就是「交叉配对」：**左边往左走的时候，右边要往右走**。于是自然得到一个带两个参数的函数：

```
mirror(a, b):
    if a == null && b == null: return true          # 同时到底 → 这一对匹配
    if a == null || b == null: return false          # 一边有一边没有 → 结构不镜像
    return a.val == b.val
        && mirror(a.left,  b.right)                  # ★ 交叉
        && mirror(a.right, b.left)                   # ★ 交叉
```

> **这一行 `mirror(a.left, b.right)` 是本章的全部**。它把「两个指针反向推进」这件事编码进了递归参数里 —— 这就是本书所说的**双指针递归**。

---

## 070.3 原理拆解

### 070.3.1 数据结构与算法选型

| 方案 | 时间 | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- |
| **双指针镜像递归 `mirror(a,b)`** ✅ 首选 | O(n) | O(h) 栈 | 8 行 | 深链树 `h ≈ n` 时爆栈 |
| 层序遍历 + 每层「含 null 占位」判回文 | O(n) | O(w) | 20 行 | 需要把 `null` 也入队当占位符，否则形状信息丢失；宽树内存大 |
| 双队列（左边按 左→右 入队，右边按 右→左 入队） | O(n) | O(w) | 18 行 | 与上一行同构，代码更绕 |
| 翻转左子树（原地）后比较两棵树 | O(n) | O(h) | 复用 069+068 | **破坏输入树**；若要求不许改树则必须新建 O(n) 节点 |
| 中序/层序序列判回文 | O(n) | O(n) | 15 行 | **答案错**：序列不能唯一确定树（反例见 §070.2） |
| 「各子树自对称 + 根孩子值相等」 | O(n) | O(h) | 10 行 | **答案错**：抓的是自性质而不是关系 |

**为什么双指针递归是首选**：

- 它把「对称」的数学定义（互为镜像）**逐字翻译**成代码，没有中间表示，也没有信息损失；
- 一遍 O(n) 扫描，不需要额外容器，不改原树；
- 空间只有递归栈 O(h)，与 068 同阶。

**层序方案为什么也能对（以及它唯一的坑）**：一层一层看，对称意味着「第 `d` 层的节点序列」是**回文**——但前提是**把 `null` 也当成元素写进序列**。如果不写 `null`，`[1,2,2,null,3,null,3]` 的第 2 层会被压缩成 `[3,3]`（回文），于是误判为对称。**这就是「形状信息必须显式保留」的代价。**

### 070.3.2 为什么这个做法是对的（正确性论证）

**定义（互为镜像）**：两棵树 `a`、`b` 互为镜像，记作 `mir(a, b)`，递归定义为

```
mir(∅, ∅)     = true
mir(∅, 非空)  = false          （对称地，非空 vs ∅ 也为 false）
mir(a, b)     = (a.val == b.val) ∧ mir(a.left, b.right) ∧ mir(a.right, b.left)
```

**命题 1**：`isSymmetric(T)` ⟺ `mir(T.left, T.right)`（空树特判为 `true`）。

**证明**：按「轴对称」的几何定义，沿根的中轴线折叠后重合，当且仅当「左边每一层的第 `k` 个」与「右边每一层的倒数第 `k` 个」逐点相等，而这一一对应正是 `mir` 递归展开时产生的配对关系：`mir(a, b)` 的每一次交叉调用都把「`a` 的左支」与「`b` 的右支」配成一对，恰是折叠后的镜像位置。∎

**命题 2（算法正确性）**：递归函数 `mirror(a, b)` 返回值恰为 `mir(a, b)`。

**证明（对 `a`、`b` 的节点总数做结构归纳）**：

1. **两个都空**：算法返回 `true`，定义给 `true`。
2. **恰有一个空**：算法返回 `false`，定义给 `false`。
3. **都非空**：算法返回 `(a.val == b.val) ∧ mirror(a.left, b.right) ∧ mirror(a.right, b.left)`。由归纳假设（子调用规模更小），后两项分别等于 `mir(a.left, b.right)` 与 `mir(a.right, b.left)`，于是整体等于定义中的 `mir(a, b)`。∎

**命题 3（为什么「各子树自对称」不够）**：由反例 `[1,2,2,3,3,4,4]` 直接否定。用定义验算：`mir(2(3,3), 2(4,4))` 需要 `3 == 4`，为假。而「各自自对称」只检查了 `mir(2(3,3), 2(3,3))` 与 `mir(2(4,4), 2(4,4))`，**检查的是自己和自己比**，得到的真值与此无关。∎

**命题 4（为什么序列比较会错）**：中序序列不是树的完备不变量 —— 反例见 §070.2 第二个念头（两棵结构不同的树有同一中序序列）。因此存在 `inorder(L) = reverse(inorder(R))` 但 `¬mir(L, R)` 的情况，序列法必然误判。∎

### 070.3.3 通用解题思路（可迁移的那一层）

#### ① 本书的「树递归语言」：双指针递归（本章新增，全书第二把钥匙）

068 立了「单指针 + 两个范式」，本章加上「双指针」：

| 递归形态 | 签名 | 参数含义 | 代表题 |
| --- | --- | --- | --- |
| **单指针 · 后序返回值** | `f(node)` 返回某统计量 | 就是当前子树 | 068 深度、071 直径、074 最大路径和、080 打家劫舍 III |
| **单指针 · 前序传参** | `f(node, ctx)` 无返回或返回 `boolean` | 从上往下带的上下文（`depth` / `path` / `prefix`） | 069 翻转（无需 ctx）、072 路径和 II、073 前缀和、077 中序计数 |
| **双指针 · 同步推进** | `f(a, b)` 返回 `boolean` | **两棵**树上的当前位置 | **070 本章**、100 相同的树、951 翻转等价、572 子树匹配（内层） |
| **双指针 · 合并** | `f(a, b)` 返回新树 | 两棵树融合 | 617 合并二叉树 |
| **双指针 · 交叉（镜像）** | `f(a.left, b.right)` | 反向推进 | **070 本章的核心一行** |

> 🧭 判断「要不要双指针」的标准同样是 068 那句的延伸：
> **问「答案属于一个节点，还是属于一对节点？」** 属于一对 ⟹ 双指针递归（`mirror(a,b)`）；属于一个 ⟹ 单指针（068 的两种范式择一）。

#### ② 双指针递归的两个骨架（本章 + 同类题一次写清）

```text
# 骨架 A：镜像（交叉推进）—— 070 / 951 的「翻」分支
mirror(a, b):
    if a == null && b == null: return true
    if a == null || b == null: return false
    return a.val == b.val && mirror(a.left, b.right) && mirror(a.right, b.left)

# 骨架 B：同步（同向推进）—— 100 相同的树 / 070 若改成"左右子树相同"
same(a, b):
    if a == null && b == null: return true
    if a == null || b == null: return false
    return a.val == b.val && same(a.left, b.left) && same(a.right, b.right)
```

**两者只差两行参数顺序，含义天差地别**：

| 骨架 | 配对方式 | 语义 | 反例判据 |
| --- | --- | --- | --- |
| A 镜像（`a.left` ↔ `b.right`） | 交叉 | 关于中轴线折叠重合 | `[1,2,2,3,4,4,3] → true` |
| B 同步（`a.left` ↔ `b.left`） | 同向 | 两棵树长得一模一样 | `[1,2,2,3,4,4,3] → false`（左右孩子值不同） |

#### ③ 与 069 的关系：三种等价表述（可以用来交叉验证思路）

| 表述 | 做法 | 代价 |
| --- | --- | --- |
| 「左右子树互为镜像」 | 本章的双指针递归 | O(n) 时间 / O(h) 空间，**不改树** |
| 「左子树翻转后与右子树相同」 | 069（翻转）+ 100（相同的树） | O(n) / O(h)，但**改树**或新建 O(n) |
| 「每层（含 null 占位）是回文」 | 层序 BFS | O(n) / O(w)，需要显式 `null` 占位 |

三者的等价性可以互相验证：如果三种实现给出不同答案，一定有一个写错了。这也是 §070.5.4 对拍的思路来源。

### 070.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| 树是 **N 叉**且要求对称 | 孩子列表首尾配对 | `for i in 0..k/2: mirror(children[i], children[k-1-i])` |
| 判两棵树**是否相同**（100） | 不镜像，同向 | 骨架 B，把 `b.right` 换回 `b.left` |
| 判**通过翻转**能否相等（951） | 每个节点两种可能 | 返回 `(同向 or 交叉) or ...`，两个分支都要试 |
| 只要**最大对称子树**（不是整树） | 不是布尔，是规模 | 每个节点返回「以它为根的镜像信息」，自底向上聚合（比本章难一档） |
| 树**极深**不允许递归 | 栈深爆炸 | 用队列装「节点对」：`offer(a.left); offer(b.right);` 成对入队，逐个弹出比较 |
| 要求 **O(1) 额外空间** | 不能用队列也不能用栈 | Morris 变体可以求「是否镜像」，但要同时跑两条线索，代码量极大，实战无人这么写 |

---

## 070.4 图解推演

### 070.4.1 镜像配对关系（一张图看清「交叉」）

```
                1
             ┌──┴──┐
        a→   2     2   ←b
           ┌─┴┐  ┌┴─┐
           3  4  4  3
           ↑  ↑  ↑  ↑
           │  │  │  │
           │  └──┼──┘  └── 4 与 4 配对（内侧对内）
           │     │
           └─────┴──────── 3 与 3 配对（外侧对外）

推进方向永远是相反的：
    a 往左走 ⟹ b 必须往右走
    a 往右走 ⟹ b 必须往左走
```

### 070.4.2 递归调用树（行 = 一次 `mirror` 调用）

```
mirror(2, 2)                    ← 根的左右孩子
├─ 值 2 == 2 ✅
├─ mirror(3, 3)                 ← a.left 与 b.right（交叉）
│   ├─ 值 3 == 3 ✅
│   ├─ mirror(null, null) → true
│   └─ mirror(null, null) → true
└─ mirror(4, 4)                 ← a.right 与 b.left（交叉）
    ├─ 值 4 == 4 ✅
    ├─ mirror(null, null) → true
    └─ mirror(null, null) → true
```

### 070.4.3 反例 `[1,2,2,3,3,4,4]` 在 `mirror` 下如何暴露

```
mirror(2(3,3), 2(4,4))
├─ 值 2 == 2 ✅                        ← 错解在这里就"过关"了
├─ mirror(3, 4)  ← a.left 与 b.right
│   └─ 值 3 == 4 ❌ → 返回 false
└─ 短路，不再继续
最终 false ✅（正确）
```

步骤表：

| 步 | `a` | `b` | 值相等？ | 结果 |
| --- | --- | --- | --- | --- |
| 1 | `2` | `2` | ✅ | 继续 |
| 2 | `3`（a 的左孩子） | `4`（b 的右孩子） | ❌ | **false，短路返回** |
| — | `3`（a 的右孩子） | `4`（b 的左孩子） | 未执行 | 被短路跳过 |

> **注意第 2 步的配对**：`a` 的左孩子 `3` 配的是 `b` 的**右**孩子 `4`。如果写成同向（`a.left` 配 `b.left`），就会拿 `3` 和 `4` 比 —— 但那是 069 的「翻转后比较」，是另一件事。

### 070.4.4 层序版为什么必须写 `null` 占位

```
树的第 2 层（含空位）：[3, 4, 4, 3]     → 回文 ✅ 对称
若不写 null 后：[3, 4, 4, 3]            → 回文 ✅

反例 [1,2,2,null,3,null,3] 的第 2 层（含空位）：[null, 3, null, 3]  → 回文 ❌ 不对称 ✅（正确）
若不写 null 后：[3, 3]                                           → 回文 ✅ → 误判为对称 ❌
```

### 070.4.5 三条路线对比

```
                   ┌────────── 输入 root ──────────┐
                   │                               │
     ┌─────────────▼──────────────┐  ┌─────────────▼───────────────┐
     │ 双指针镜像递归 mir(a,b)    │  │ 层序 + 每层（含 null）回文  │
     │ O(n) / O(h)  · 不改树      │  │ O(n) / O(w)  · 需要显式占位 │
     └─────────────┬──────────────┘  └─────────────┬───────────────┘
                   │                               │
        交叉推进 a.left↔b.right            每层序列必须把 null 写进去
        结构信息全程保留                    结构信息靠"占位符"保住
                   │                               │
           【本章首选，8 行】              【宽树更省内存？其实更费，只在迭代题用】
```

---

## 070.5 双语言实现

### 070.5.1 Java

**主解（双指针镜像递归）**：

```java
// LeetCode 判题环境已内置 TreeNode，粘贴到网页时不要重复定义：
// class TreeNode {
//     int val;
//     TreeNode left, right;
//     TreeNode(int val) { this.val = val; }
// }

class Solution {
    public boolean isSymmetric(TreeNode root) {
        // 空树关于中轴线折叠后仍然是空的 → 对称
        if (root == null) {
            return true;
        }
        // ★ 关键一步：把「一棵树是否对称」换写成「两棵子树是否互为镜像」
        return mirror(root.left, root.right);
    }

    // 双指针递归：a 从左侧往下走，b 从右侧往下走，方向始终相反
    private boolean mirror(TreeNode a, TreeNode b) {
        if (a == null && b == null) {
            return true;          // 两边同时到底 → 这一对匹配
        }
        if (a == null || b == null) {
            return false;         // 一边有一边没有 → 结构不镜像
        }
        // 顺序上先比值，成本最低，可以最快短路
        return a.val == b.val
                && mirror(a.left, b.right)   // ★ 交叉：a 往左，b 往右
                && mirror(a.right, b.left);  // ★ 交叉：a 往右，b 往左
    }
}
```

**备解（层序 + 含 `null` 占位判回文）**：

```java
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;

class Solution {
    public boolean isSymmetric(TreeNode root) {
        if (root == null) {
            return true;
        }
        // 队列里允许出现 null（所以不能直接用 ArrayDeque，它禁止 null 元素）：
        // 这里用 ArrayList 当"逐层容器"，避免 ArrayDeque 的 null 限制
        List<TreeNode> cur = new ArrayList<>();
        cur.add(root);
        while (!cur.isEmpty()) {
            List<TreeNode> next = new ArrayList<>();
            for (TreeNode node : cur) {
                if (node == null) {
                    continue;      // 空节点没有孩子，但它的"空"已经体现在本层序列里
                }
                next.add(node.left);   // 故意把 null 也加进去
                next.add(node.right);
            }
            // 本层序列（含 null）必须是回文
            for (int i = 0, j = cur.size() - 1; i < j; i++, j--) {
                TreeNode x = cur.get(i), y = cur.get(j);
                if (x == null && y == null) {
                    continue;
                }
                if (x == null || y == null) {
                    return false;
                }
                if (x.val != y.val) {
                    return false;
                }
            }
            cur = next;
        }
        return true;
    }
}
```

### 070.5.2 Go

**主解（双指针镜像递归）**：

```go
package main

// LeetCode 判题环境已内置该定义，本地编译时保留即可
type TreeNode struct {
	Val   int
	Left  *TreeNode
	Right *TreeNode
}

func isSymmetric(root *TreeNode) bool {
	if root == nil {
		return true
	}
	var mirror func(a, b *TreeNode) bool
	mirror = func(a, b *TreeNode) bool {
		if a == nil && b == nil {
			return true // 同时到底
		}
		if a == nil || b == nil {
			return false // 结构不镜像
		}
		return a.Val == b.Val &&
			mirror(a.Left, b.Right) && // 交叉
			mirror(a.Right, b.Left)    // 交叉
	}
	return mirror(root.Left, root.Right)
}
```

**备解（显式队列成对入队）**：

```go
package main

func isSymmetricQueue(root *TreeNode) bool {
	if root == nil {
		return true
	}
	// Go 的切片可以放 nil，天然支持"空位占位"
	q := []*TreeNode{root.Left, root.Right}
	head := 0
	for head < len(q) {
		a, b := q[head], q[head+1]
		head += 2
		if a == nil && b == nil {
			continue
		}
		if a == nil || b == nil {
			return false
		}
		if a.Val != b.Val {
			return false
		}
		// ★ 交叉入队：a 的左配 b 的右，a 的右配 b 的左
		q = append(q, a.Left, b.Right, a.Right, b.Left)
	}
	return true
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
		{},                                     // 空树 → true
		{1},                                    // 单结点 → true
		{1, 2, 2, 3, 4, 4, 3},                  // 官方样例 true
		{1, 2, 2, nil, 3, nil, 3},              // 官方样例 false（结构不镜像）
		{1, 2, 2, 3, 3, 4, 4},                  // 自造反例：各子树自对称但整体不对称 → false
		{1, 2, nil},                            // 只有左孩子 → false
		{2, 1, 1, nil, nil, nil, nil},          // 浅层对称 → true
	}
	for _, c := range cases {
		root := build(c)
		fmt.Printf("%v → 递归 %v | 队列 %v\n", c, isSymmetric(root), isSymmetricQueue(root))
	}
}
```

输出（两种实现必须一致）：

```text
[] → 递归 true | 队列 true
[1] → 递归 true | 队列 true
[1 2 2 3 4 4 3] → 递归 true | 队列 true
[1 2 2 <nil> 3 <nil> 3] → 递归 false | 队列 false
[1 2 2 3 3 4 4] → 递归 false | 队列 false
[1 2 <nil>] → 递归 false | 队列 false
[2 1 1 <nil> <nil> <nil> <nil>] → 递归 true | 队列 true
```

### 070.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 双指针递归的写法 | 私有方法 `mirror(TreeNode a, TreeNode b)` | 必须先 `var mirror func(a, b *TreeNode) bool` 再赋值 | Go 漏声明 → `undefined: mirror`；这是 Go 写二叉递归的固定套路 |
| 队列能否装空值 | `ArrayDeque` **禁止** `null` | `[]*TreeNode` 可以装 `nil` | Java 的层序备解要用 `ArrayList`/`LinkedList`；Go 可以直接 `append(q, nil)` |
| 空值 | `null` | `nil` | 语义一致；判空组合（`&&` / `||`）要写全，缺一半就判错 |
| 布尔短路 | `&&` 短路生效 | `&&` 同样短路 | 两边都靠短路省掉一半递归；注意**不要**把有副作用的调用放在 `&&` 右边 |
| 值比较 | `a.val == b.val`（`int`，安全） | `a.Val == b.Val` | 若值是 `Integer`/`interface{}`，Java 必须用 `equals`；本题是 `int`，无风险 |
| 返回类型 | `boolean` | `bool` | 空树返回 `true`，两边都要显式写 |

### 070.5.4 对拍测试（三条独立路线互验 + 构造对称/破坏对称）

```go
package main

import (
	"math/rand"
	"testing"
)

// 参考实现 A：层序 + 含 nil 占位判回文（与双指针递归完全不同的思路）
func symByLevels(root *TreeNode) bool {
	if root == nil {
		return true
	}
	cur := []*TreeNode{root}
	for len(cur) > 0 {
		for i, j := 0, len(cur)-1; i < j; i, j = i+1, j-1 {
			x, y := cur[i], cur[j]
			if x == nil && y == nil {
				continue
			}
			if x == nil || y == nil || x.Val != y.Val {
				return false
			}
		}
		next := []*TreeNode{}
		for _, n := range cur {
			if n == nil {
				continue
			}
			next = append(next, n.Left, n.Right) // 显式保留 nil
		}
		cur = next
	}
	return true
}

// 参考实现 B：把一个子树镜像复制，再判两棵树是否相同（069 + 100 的组合）
func mirrorCopy(root *TreeNode) *TreeNode {
	if root == nil {
		return nil
	}
	return &TreeNode{Val: root.Val, Left: mirrorCopy(root.Right), Right: mirrorCopy(root.Left)}
}

func sameTree(a, b *TreeNode) bool {
	if a == nil || b == nil {
		return a == b
	}
	return a.Val == b.Val && sameTree(a.Left, b.Left) && sameTree(a.Right, b.Right)
}

func symByMirrorCopy(root *TreeNode) bool {
	if root == nil {
		return true
	}
	// 整棵树对称 ⟺ 它与自己的镜像完全相同
	return sameTree(root, mirrorCopy(root))
}

// 随机构造一棵"保证对称"的树：先随机造左子树，再把它镜像复制成右子树
func randLeft(r *rand.Rand, n int) *TreeNode {
	if n == 0 {
		return nil
	}
	root := &TreeNode{Val: r.Intn(7)} // 值域故意小，制造大量重复值
	slots := []**TreeNode{&root.Left, &root.Right}
	for i := 1; i < n; i++ {
		j := r.Intn(len(slots))
		p := slots[j]
		*p = &TreeNode{Val: r.Intn(7)}
		slots[j] = slots[len(slots)-1]
		slots = slots[:len(slots)-1]
		slots = append(slots, &(*p).Left, &(*p).Right)
	}
	return root
}

func collectAll(root *TreeNode, out *[]*TreeNode) {
	if root == nil {
		return
	}
	*out = append(*out, root)
	collectAll(root.Left, out)
	collectAll(root.Right, out)
}

func TestSymmetricRandom(t *testing.T) {
	r := rand.New(rand.NewSource(101))
	for iter := 0; iter < 20000; iter++ {
		leftN := r.Intn(12)
		left := randLeft(r, leftN)
		root := &TreeNode{Val: r.Intn(7), Left: left, Right: mirrorCopy(left)} // 构造对称树

		// ① 对称树必须三种实现都判 true
		if !isSymmetric(root) || !symByLevels(root) || !symByMirrorCopy(root) {
			t.Fatalf("iter=%d 对称树被判成了不对称", iter)
		}

		// ② 随机改掉一个【非根】节点的值，对称性必然被破坏：
		//    对称树里除根之外的每个节点都有一个"另一个位置的"镜像搭档，
		//    只改其中一个，这一对的值就不等了。
		//    注意：根节点不能拿来改 —— 它自己就是中轴，改它不影响对称性。
		all := []*TreeNode{}
		collectAll(root, &all)
		nonRoot := all
		if len(nonRoot) > 0 {
			nonRoot = nonRoot[1:] // 丢掉 root
		}
		if len(nonRoot) > 0 {
			victim := nonRoot[r.Intn(len(nonRoot))]
			victim.Val += 1000 // 值域是 [0,6]，+1000 后必然与任何搭档都不同
			if isSymmetric(root) || symByLevels(root) || symByMirrorCopy(root) {
				t.Fatalf("iter=%d 破坏一个非根节点后仍被判为对称", iter)
			}
		}

		// ③ 纯随机树（大概率不对称）三路互验
		randRoot := randLeft(r, r.Intn(15))
		a, b, c := isSymmetric(randRoot), symByLevels(randRoot), symByMirrorCopy(randRoot)
		if a != b || b != c {
			t.Fatalf("iter=%d 三路不一致：递归=%v 层序=%v 镜像=%v", iter, a, b, c)
		}
	}
}

// 边界专测：空树、单结点、全左链（必不对称）
func TestSymmetricEdge(t *testing.T) {
	if !isSymmetric(nil) {
		t.Fatal("空树应判为对称")
	}
	if !isSymmetric(&TreeNode{Val: 1}) {
		t.Fatal("单结点应判为对称")
	}
	var chain *TreeNode
	for i := 1; i <= 50; i++ {
		chain = &TreeNode{Val: 1, Left: chain}
	}
	if isSymmetric(chain) {
		t.Fatal("全左链不应判为对称")
	}
}
```

> **对拍设计的两处用心**：① 随机树**极难**恰好对称，如果只造随机树，`isSymmetric` 返回 `true` 的分支几乎测不到 —— 所以先「构造对称」（左子树镜像复制）再「破坏对称」（改一个节点的值），两个方向都覆盖；② 三个实现分别基于**递归交叉**、**层序回文**、**镜像复制后比较**，思路互不共享，同时一致才可信。

---

## 070.6 复杂度与常见坑

### 070.6.1 复杂度推导

- **时间 O(n)**：`mirror(a, b)` 每次调用处理一对节点；每个节点最多参与一次配对（因为镜像映射是一一对应的），所以总调用次数 O(n)。短路 `&&` 只可能减少调用，不会增加。
- **空间 O(h)**：递归栈深等于树高（两条分支同步下降，取更浅的那条决定何时返回，但上界是 `h`）。链状树 `h = n`。
- **层序版空间 O(w)**：每层容器装最宽一层（含 `null` 占位，实际可达到 `2w`）。
- **镜像复制版空间 O(n)**：额外造了一整棵树。

| 方案 | 时间 | 空间 | 1000 节点全左链 | 1000 节点满二叉树 |
| --- | --- | --- | --- | --- |
| 双指针递归 | O(n) | O(h) = O(n) | 栈深 1000，安全 | 栈深 10 |
| 层序（含 null） | O(n) | O(w) | O(1)（每层 1 个 + 1 个 null） | O(n) |
| 镜像复制后比较 | O(n) | O(n) 新节点 | O(n) | O(n) |

### 070.6.2 七个真实踩过的坑

1. **把「对称」写成「左右子树各自对称」** —— `[1,2,2,3,3,4,4]` 上返回 `true`（正解 `false`）。**对称是关系，不是自性质。**
2. **用中序遍历/层序序列判回文** —— 序列丢失结构信息，`[4,2,3,1,3,2,4]` 是回文但树不对称。
3. **层序判回文时省掉了 `null` 占位** —— `[1,2,2,null,3,null,3]` 会被压缩成 `[3,3]` 的回文 → 误判 `true`。
4. **只比较根的两个孩子** —— `root.left.val == root.right.val` 之后就 `return true`，第二层以下全漏。
5. **交叉写成同向** —— `mirror(a.left, b.left)` 变成了「两棵树是否相同」（100 的题），对称树 `[1,2,2,3,4,4,3]` 会被判 `false`。
6. **空值判定的三种组合漏掉一种** —— 只写 `if (a == null || b == null) return false;` 而漏掉 `a == null && b == null` 的返回 `true`，于是「两边同时到底」也被判成不对称，所有树都返回 `false`。
7. **Java 用 `==` 比较 `Integer` 值** —— 若把 `val` 装箱成 `Integer`（比如 `List<Integer>` 里比较），超过 `[-128,127]` 缓存范围时 `==` 恒为 `false`。本题 `int` 字段无此问题，但把逻辑挪到「层序列表比较」时就会踩到，必须用 `equals`。

### 070.6.3 面试官最爱追问的三个问题

**Q1：为什么不能直接对每个子树判对称？**

> 「因为对称是**两棵子树之间的关系**，不是任何一棵子树的性质。反例 `[1,2,2,3,3,4,4]`：根的左边是 `2(3,3)`、右边是 `2(4,4)` —— 两棵子树各自都左右对称，但整体不对称，因为镜像位置上 `3 ≠ 4`。所以要判的是 `mirror(a, b)` 这种**双参数**关系。」

**Q2：不用递归怎么写？**

> 「把递归的「一对节点」搬进队列：初始入队 `(root.left, root.right)`，每次弹出一对，比较值，再**交叉**入队 `(a.left, b.right)` 和 `(a.right, b.left)`。空间 O(w)。这就是很多同学写的「迭代版对称」，本质是把系统栈搬到堆上。**注意 Go 的切片可以装 `nil`，Java 的 `ArrayDeque` 不行，得用 `LinkedList` 或 `ArrayList` 分轮处理。**」

**Q3：这道题和「翻转二叉树」是什么关系？**

> 「069 是**单树自镜**（把树变成它的镜像），070 是**双树互镜**（判两棵树是否互为镜像）。三句话可以连起来：① 树对称 ⟺ 它与自己的镜像完全相同（`root == mirror(root)`）；② 所以「先翻左子树再判两棵树相同」也能做，但会改树或多占 O(n) 内存；③ 双指针递归把这件事实现在**一趟扫描**里 —— 左指针往左走时右指针往右走，等价于「一边走一边照镜子」，不改树、不建新树。」

**Q4（常见追问）：如果要求 O(1) 额外空间呢？**

> 「两层意思要分清：① **栈空间**可以用 Morris 线索化压到 O(1)，但要同时跑两条线索（一条从中轴左侧按前序、一条按镜像顺序），代码量和风险都很大；② 更现实的做法是承认 O(h) 已经是最优工程解 —— 因为判定必须访问每个节点，而访问树本身至少要一条 O(h) 的路径。**面试里答『O(h) 是必需的，除非允许改树』比硬写 Morris 更显水平。**」

---

## 070.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 100 相同的树](https://leetcode.cn/problems/same-tree/) | 简单 | 双指针**同向**推进（`a.left`↔`b.left`），本章的「骨架 B」 |
| [LC 226 翻转二叉树](https://leetcode.cn/problems/invert-binary-tree/) | 简单 | 069：单树自镜；本章等价于「`root` 与 `mirror(root)` 相同」 |
| [LC 572 另一棵树的子树](https://leetcode.cn/problems/subtree-of-another-tree/) | 简单 | 外层遍历每个节点，内层用「相同的树」匹配，O(n·m) |
| [LC 951 翻转等价二叉树](https://leetcode.cn/problems/flip-equivalent-binary-trees/) | 中等 | 每个节点可翻可不翻 → 两个分支都要试，`cross || same` |
| [LC 617 合并二叉树](https://leetcode.cn/problems/merge-two-binary-trees/) | 简单 | 双指针递归但**返回新树**：值相加、空则接另一棵 |
| [LC 110 平衡二叉树](https://leetcode.cn/problems/balanced-binary-tree/) | 简单 | 单指针后序返回值；与本章对照「一个指针 vs 两个指针」 |
| [LC 101 的最大对称子树变体](https://leetcode.cn/problems/symmetric-tree/) | — | 不是判整树，而是求最大对称子树规模：需要每个节点自底向上返回镜像信息 |
| [LC 559 N 叉树的最大深度](https://leetcode.cn/problems/maximum-depth-of-n-ary-tree/) | 简单 | 把「两个孩子」换成「孩子列表」，是双指针思维在 N 叉上的落点 |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 100 相同的树**
1. 本章的 `mirror` 如果把两行递归的 `b.right` / `b.left` 换成同一个方向，语义会变成什么？
2. 两棵树都为空的返回值、只有一棵为空的返回值，必须分别是什么？

**LC 951 翻转等价二叉树**
1. 每个节点处，除了「不翻」这一种配对，还有一种配对方式值得试，它对应本章的哪一行？
2. 两个分支之间用什么逻辑连接，才既完整又不重复？

**LC 572 另一棵树的子树**
1. 外层要在每个节点上「尝试匹配」，这件事用哪种遍历都行吗？
2. 内层匹配和本章的哪个骨架完全一致？

**通用**
- 当题目问「两个东西是否……」，先判断它是「一个对象的性质」还是「一对对象的关系」。
- 一旦发现递归里需要同时推进两个位置，就把签名改成两个参数 —— 这是双指针递归的起点。

</details>

---

## 附：本章小结（可背走的三句话）

```
① 对称 = 左右子树互为镜像：mir(a,b) = a.val==b.val ∧ mir(a.left,b.right) ∧ mir(a.right,b.left)
② 对称是「一对节点」的关系，不是「一棵子树」的性质 —— 这就是必须双指针递归的原因
③ 空×空 = true，空×非空 = false；交叉配对两行是整个算法，同向就退化成「相同的树」
```

> **下一章**：[`071-diameter-of-binary-tree.md`](071-diameter-of-binary-tree.md) —— 单指针后序回归，但会带来本书最重要的一次背叛：**函数返回的东西 ≠ 题目要的答案**。
