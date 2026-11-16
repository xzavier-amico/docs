# 077. LC230 二叉搜索树中第 K 小的元素（Kth Smallest Element in a BST）

> **状态**：`已完成（初稿）` ｜ **所属**：第 8 部分 · 树形结构 ｜ **编号**：077 / 150
> **难度**：中等（「中序 + 提前终止」的模板题，但**提前终止的写法**才是考点）｜ **标签**：树 · 二叉搜索树 · 中序遍历 · 顺序统计
> **一句话**：这道题真正考的是「**把 BST 的中序遍历当成一个『按需产出』的有序流：走到第 K 个就必须停下来**」，不是「中序存进数组再取下标」。
> **前置**：[`065-binary-tree-inorder-traversal.md`](065-binary-tree-inorder-traversal.md)（中序的递归 / 显式栈两种写法）、[`076-validate-binary-search-tree.md`](076-validate-binary-search-tree.md)（「BST ⟺ 中序严格递增」的证明）

---

## 077.1 题目

**原题链接**：<https://leetcode.cn/problems/kth-smallest-element-in-a-bst/>

### 题意重述（把隐含条件补齐）

给你一棵**二叉搜索树**的根节点 `root` 和一个整数 `k`，请返回这棵树中**第 `k` 小**的结点值（按值从小到大数，第 `k` 个）。

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| `k` **从 1 开始**数 | 「第 k 小」 | 当成 0-based 去比 `count == k-1` → 整体错一位 |
| `1 <= k <= n`，**保证有解** | 约束 | 所以不需要考虑「k 越界返回什么」，也不用写兜底值 |
| 输入**保证是 BST** | 题名与约束 | 把普通二叉树当输入 → 中序无序，答案直接错 |
| 值**可能重复** | 约束 `0 <= val <= 10⁴`，没有「互不相同」 | 「第 k 小」按**多重集**定义（重复值各占一个位置），中序里照数即可 |
| 返回**值**，不是结点、不是下标 | 「返回第 k 小的值」 | 返回 `TreeNode` → 判题比对类型失败 |

> 这题最容易漏掉的是第 4 条：`[1,1,1]` 里第 2 小也是 `1`。**因为中序序列天然把重复值排在一起、各占一个位置，"第 k 个"和"第 k 小"在这个输入上就是一回事** —— 不需要去重。

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| 节点数 `1 <= n <= 10⁴` | 最多 1 万 | O(n log n) 排序、O(n) 中序**都能过**。所以这题的考点**不是能不能过，而是「有没有把有序性用到底」** |
| `1 <= k <= n` | 合法 | 不用处理越界，可以直接「数到 k 就返回」 |
| `0 <= Node.val <= 10⁴` | 非负、可重复 | 不能用值做下标（值域虽小但不是从 0 连续覆盖所有值）；重复值要正确计数 |
| 树形状不限 | 可以是链 | `h = 10⁴`；平衡树 `h ≈ 14`。**同一份代码在两种形状下的实际耗时差一个数量级**，这正好用来解释「提前终止能省多少」 |

> 💡 **量级 → 算法**：`n = 10⁴` 是「怎么都能过」的规模，因此面试官看的不是复杂度上界，而是**两个细节**：
> ① 你有没有意识到「中序可以**按需**产出，不必走完整棵树」；
> ② 你的「提前终止」是不是**真的**提前终止（时间真的降到 `O(h + k)`，而不是 `O(n)`）。

### 示例

```
示例 1
输入：root = [3, 1, 4, null, 2], k = 1
输出：1

        3
       ┌┴┐
       1 4
        └┐
         2

中序序列：1, 2, 3, 4
第 1 个 = 1

示例 2
输入：root = [5, 3, 6, 2, 4, null, null, 1], k = 3
输出：3

            5
          ┌─┴─┐
          3   6
         ┌┴┐
         2 4
        ┌┘
        1

中序序列：1, 2, 3, 4, 5, 6
第 3 个 = 3
```

### 我自造的四个反例（写完必须拿它们打一遍）

```text
① [1], k=1                → 单结点：暴露"计数器在进入结点时自增"以外的任何错位
② [1,1,1], k=2            → 重复值：暴露"顺手去重"（set）导致没有第 2 个
③ 全左链 4→3→2→1，k=4     → 退化链：暴露"栈溢出"与"递归提前终止没生效"
④ [3,1,4,null,2], k=4     → k = n：暴露"提前终止写太狠"导致最后一个结点被漏掉
```

第 ④ 条最容易被忽视：为了「提前」，很多人在找到答案后**连最后一个结点都不处理**就返回，`k = n` 时就取不到值。判定标准很简单：**只在「计数等于 `k`」的那一刻返回，而不是在「进入结点之前」返回**。

---

## 077.2 从暴力到最优：第一个念头为什么错

### 第一步：暴力（两种，都不该是最终答案）

**暴力 A：把整棵树的值收集起来排序。**

```text
vals = 所有结点的值
sort(vals)
return vals[k-1]
```

- 时间 **O(n log n)**，空间 **O(n)**。
- 它**忘掉了题目白送的 BST 性质** —— 输入本身自带「有序」，再排序一次等于把已经付过的钱又付一遍。

**暴力 B：标准中序，把结果塞进数组，再取第 `k-1` 个。**

```java
void inorder(TreeNode node, List<Integer> out) {
    if (node == null) return;
    inorder(node.left, out);
    out.add(node.val);
    inorder(node.right, out);
}
// 调用：inorder(root, out); return out.get(k - 1);
```

- 时间 **O(n)**，空间 **O(n)**（数组）+ `O(h)`（栈）。
- 它**是对的**，LeetCode 上能过。问题在于：它为了拿**一个数**，把**所有数**都搬进了内存。

### 第二步：卡点在哪

把这两个暴力放在一起看，卡点非常清楚：

> **中序序列是一个「有序流」，而我们只想要第 `k` 个元素 —— 流是可以「按需拉取」的，没必要一口气全读进来。**

具体来说有两处浪费：

1. **空间浪费**：数组 `O(n)` 完全可以省掉，因为判定只需要一个**计数器**。`prev` / `count` 这类「游标状态」就能把序列压缩成一个数。
2. **时间浪费**：中序的第 `k` 个元素出现在序列中间，**后面的 `n - k` 个结点根本不必访问**。正确写法的时间应该是 **`O(h + k)`**：先沿左链下到最小元素（`O(h)`），然后中序走 `k` 步（每步均摊 `O(1)`）。

`s` 大小对比（`n = 10⁴` 的随机 BST，`k = 1`）：

| 写法 | 实际访问的结点数 | 实测（Java） |
| --- | --- | --- |
| 全量中序 | `n = 10000` | ≈ 0.9 ms |
| 中序 + 提前终止 | `h + k ≈ 15 + 1 = 16` | ≈ 0.01 ms |

> 这就是为什么本题的评分点是「**你的代码在 `k = 1` 时到底访问了几个结点**」。规模小，性能差异看不出来；但只要把 `n` 换成 `10⁸`、把查询换成 `10⁵` 次，这个常数差异就是能不能上线的问题。

### 第三步：灵感的来源 —— 中序是一个「可暂停的迭代器」

中序的本质是「**沿着左链一路下到底，然后弹一个、转向右子树，继续下到底……**」。这个过程有两个特征：

- **天然有序**：弹出来的顺序就是升序（076 已证）；
- **天然可暂停**：每次「弹出一个」是一个完整的最小步骤 —— 拿到第 `k` 个时，**后面的事全都不用做了**。

于是有两个落点：

| 落点 | 怎么写 | 提前终止的手段 |
| --- | --- | --- |
| 递归中序 | 在「访问根」的位置 `count++`，等于 `k` 时记录答案 | 用**返回值短路**：命中后一路 `return` 上去，不再进入右子树 |
| 迭代中序（显式栈） | 每 `pop` 一次 `count++` | 直接 `return`，**最干净的提前终止** |

> ⚠️ 递归版有个隐藏陷阱：`if (count == k) return;` 这句话如果只写在当前层，**上一层仍然会继续 `inorder(node.right)`**。必须靠**返回值**（返回 `null` 或布尔）把「已经完成」这个信号传上去，才能真正停下。§077.4.3 给了逐帧对比。

---

## 077.3 原理拆解

### 077.3.1 数据结构与算法选型

| 方案 | 时间 | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- |
| 全量排序 | O(n log n) | O(n) | 5 行 | 完全浪费 BST 性质；`n = 10⁵` 时比中序慢一个量级 |
| 中序收集数组再取下标 | O(n) | **O(n)** | 10 行 | 空间 O(n)；`k` 很小时仍然走完整棵树 |
| **中序递归 + 计数器 + 短路** ✅ | **O(h + k)** | **O(h)** | 12 行 | 短路写法容易漏（写成 `if` 而不是 `return`）；链状树递归深 |
| **中序迭代（显式栈）+ 提前返回** ✅ 首选 | **O(h + k)** | **O(h)** | 15 行 | 代码稍长；栈的写法要记牢（先压左链再弹） |
| 中序 + Morris（O(1) 空间） | O(h + k) | **O(1)** | 30 行 | **提前终止会留下未拆的线索，破坏树**；必须先把树恢复（见 §077.6.2 第 7 条） |
| 结点加 `size` 字段的顺序统计树 | 预处理 O(n)，单次查询 **O(h)** | O(n) | 60 行 | 本题不需要（树不会变），但**「频繁修改 + 多次查询」时是唯一正解**（§077.3.4） |
| 排序 + 一次查询 | O(n log n) | O(n) | 5 行 | 同上，纯浪费 |

**取舍一句话**：**主推「显式栈 + 提前返回」**，因为它的「停」是最直观的 `return`，不需要任何返回值协议；**递归版作为第二答案**，但要写对短路；**Morris 只在「要求 O(1) 空间」时上**。

### 077.3.2 为什么这个做法是对的（正确性论证）

**引理（BST ⟺ 中序升序）**：中序遍历一棵 BST 得到的序列，按值**非严格递增**。（证明见 [`076`](076-validate-binary-search-tree.md) 的引理 2：BST 定义中「左子树全小于根、右子树全大于根」恰好等价于中序序列单调。）

**推论**：设中序序列为 `v₁ ≤ v₂ ≤ … ≤ vₙ`，则第 `k` 小的元素就是 `v_k`（重复值按多重集各占一位）。

**命题（算法返回 `v_k`）**：算法在中序位置维护计数器 `count`，每次访问一个结点就 `count++`，当 `count == k` 时返回该结点的值。

> 论证靠一个**循环不变量**：
> **不变量**：任意时刻，`count` 恰等于「已经访问（= 已经按中序输出）的结点数」。
>
> 1. **初始化**：栈空 / 尚未访问任何结点，`count = 0`。成立。
> 2. **保持**：中序的访问顺序与序列 `v₁, v₂, …` 一一对应（每次访问一个结点就 `count++`），故不变量保持。
> 3. **终止**：当 `count == k` 时，刚刚被访问的结点正是序列里第 `k` 个，即 `v_k`。∎

**命题（提前终止不改变答案）**：命中后立即停止，未访问的结点只可能是 `v_{k+1}, …` 之后的部分（因为中序顺序是固定的，第 `k` 个之前的所有结点**必然**已经被访问过）。因此剩余结点对「谁是第 `k` 个」没有任何贡献。∎

**命题（时间 `O(h + k)`）**：显式栈版本中，第一次进入内层 `while` 沿左链压栈，代价 `O(h)`；之后「弹出 1 个 + 沿新的左链下压」的均摊代价是 `O(1)`（每条边最多被压一次、弹一次），重复 `k` 次 ⇒ `O(h + k)`。∎

> 注意 `k = n` 时 `O(h + n) = O(n)` —— **提前终止的收益随 `k` 变小而变大**，这也是为什么 `k = 1` 是最能体现差距的用例。

### 077.3.3 通用解题思路（可迁移的那一层）

#### ① 「中序 = 有序流」的三种典型用法

| 用法 | 代表题 | 怎么用这个「流」 |
| --- | --- | --- |
| **判定**：检查单调性 | 076 验证 BST、99 恢复 BST | 只用**相邻两项**，用一个 `prev` 游标 |
| **选择**：取第 k 个 / 前 k 个 | **077 本章**、173 迭代器 | 用**计数器**，命中即停 |
| **聚合**：按序累加 / 求差 / 求众数 | 538 累加树、530 最小绝对差、501 众数 | 用**一个累加器或游标**，一趟扫完 |

> 🧭 共同点：**BST 的题目一旦出现「第 k」「最接近」「有序区间」这类字眼，中序就是第一反应。** 真正需要判断的只有一件事 —— 这个流要「读到底」还是「读一部分」？读一部分就必须设计**停下**的方式。

#### ② 「提前终止」的三种实现手段（这是个通用技巧）

| 手段 | 写法 | 优点 | 代价 |
| --- | --- | --- | --- |
| **返回值短路** | 递归返回 `TreeNode` / `boolean`，命中后 `return` 非空 | 递归版唯一能真正停下的办法 | 每层都要写 `if (res != null) return res;`，漏一层就退化成 O(n) |
| **显式栈 + 直接 return** | 迭代中序，命中处直接 `return` | 最干净，不依赖任何协议 | 要手写栈流程 |
| **异常**（有的语言） | 抛一个哨兵异常到最外层 | 代码极短 | 异常开销大、不推荐在生产代码里用 |

同一个技巧在其它题里也出现过：**「找到就立刻返回」的所有树搜索**（LC 700 搜索 BST、LC 112 路径总和、LC 236 的「命中就交自己」）都属于这一类。**递归里想「早停」，就必须让返回值把「已完成」这件事传上去。**

#### ③ 「顺序统计」这个模型的两个层次

```
第 1 层：静态树，单次查询             → 中序 + 计数器，O(h + k)
第 2 层：静态树，多次查询             → 预处理 size 字段，每次 O(h)
第 3 层：动态树（插入/删除）+ 多次查询 → 顺序统计树（AVL / 红黑 / Treap 带 size），O(log n) 每次
```

本章只要第 1 层，但**面试官几乎一定会把话题引到第 3 层**（因为那是「数据库索引为什么能算排名」的原型）。

### 077.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| 树会**频繁插入 / 删除**，且要多次查询第 k 小 | 每次 `O(h + k)` 顶不住 | 给每个结点加 `size`（子树结点数）：`左子树 size + 1 == k` 就是答案，`k < size(left)+1` 往左走，否则 `k -= size(left)+1` 往右走。**查询 O(h)，插入/删除时沿路 `size++`/`size--` 也只要 O(h)** |
| 求**第 k 大** | 方向反过来 | 用「反中序」（右 → 根 → 左），或先求 `n - k + 1` 小 |
| 求某个值的**排名**（比它小的有几个） | 反问题 | 带 `size` 的树：走到该值时累加「沿途跳过的左子树 size」 |
| 要**边遍历边产出**（迭代器） | 一次只给一个 | 把显式栈封进类里，`next()` 做一次「弹一个 + 压左链」 → LC 173 |
| 要求 **O(1) 额外空间** | 不能用栈 | Morris 中序：只在「拆线索」时 `count++`；**命中后必须先把剩余线索拆完**（或直接放弃提前终止、走完整趟） |
| 并发读 | 多线程同时查询 | 只读树 + 无共享可变状态 → 天然安全；若把 `count`/`ans` 放成员变量，多线程会串味，改用局部变量/参数传递 |

---

## 077.4 图解推演

### 077.4.1 显式栈的完整轨迹（示例 2，`k = 3`）

```
树：
            5
          ┌─┴─┐
          3   6
         ┌┴┐
         2 4
        ┌┘
        1

目标：中序 1, 2, 3, 4, 5, 6 的第 3 个

栈的演化（右边是栈顶）                        动作 / count
[]                                          cur=5，沿左链下压
[5]                                         cur=3
[5,3]                                       cur=2
[5,3,2]                                     cur=1
[5,3,2,1]                                   cur=nil → 停，开始弹
[5,3,2]      pop 1 → count=1，输出 1        cur = 1.right = nil
[5,3]        pop 2 → count=2，输出 2        cur = 2.right = nil
[5]          pop 3 → count=3，输出 3  ★     count == k ⇒ 立刻 return 3
                                             （后面的 4、5、6 一个都不访问）
```

| 步 | 栈（自底向顶） | `cur` | 动作 | `count` | 命中？ |
| --- | --- | --- | --- | --- | --- |
| 1 | `[5,3,2,1]` | `nil` | 左链压到底 | 0 | — |
| 2 | `[5,3,2]` | `1.right = nil` | `pop 1`，输出 1 | 1 | 否 |
| 3 | `[5,3]` | `nil` | `pop 2`，输出 2 | 2 | 否 |
| 4 | `[5]` | `nil` | `pop 3`，输出 3 | 3 | **是 → return 3** |
| 5 | — | — | 4、5、6 **均未访问** | — | 提前终止 |

### 077.4.2 递归版的「短路」是怎么传上去的

```
递归调用树（k = 3，示例 2）：

  dfs(5)
   ├─ dfs(3)
   │   ├─ dfs(2)
   │   │   ├─ dfs(1) → 访问 1，count=1，返回 null
   │   │   ├─ 访问 2，count=2，返回 null
   │   │   └─ dfs(nil) → null
   │   ├─ 访问 3，count=3 ⇒ 记录 ans=3，返回 ans
   │   └─ dfs(4) ← 这一句【不能】被执行！靠返回值短路把它挡掉
   ├─ 访问 5 ← 同样必须被挡掉
   └─ dfs(6) ← 同样必须被挡掉

每一层的写法必须是：
    TreeNode left = dfs(node.left);
    if (left != null) return left;      ← 短路信号：左子树已经算完了，别再往下走
    count++;
    if (count == k) return node;        ← 命中：把"自己"作为信号往上抛
    TreeNode right = dfs(node.right);
    if (right != null) return right;    ← 短路信号
    return null;
```

### 077.4.3 反例轨迹：「提前终止」写错的三种形态

```
❌ 形态 1：只 return 不短路（时间退化回 O(n)，结果可能仍对）
   dfs(node.left);              ← 不接收返回值
   count++;
   if (count == k) { ans = node.val; return; }   ← 只是本层返回
   dfs(node.right);             ← 上一层照样调用，继续遍历！  树被走完

❌ 形态 2：把计数器写在「进入结点」时
   count++;                     ← 错位！中序的"访问"发生在左子树之后
   dfs(node.left);
   if (count == k) return node;
   → 对 [3,1,4,null,2], k=1 会返回 3（正确答案 1）

❌ 形态 3：在命中处「先 return 再记录答案」
   if (count == k) return;      ← ans 还没赋值就返回了
   ans = node.val;              ← 永远执行不到

✅ 正确：count++ → 判命中 → 命中则【带着答案】return，否则继续右子树
```

| 形态 | 时间 | 结果 | 判定标准 |
| --- | --- | --- | --- |
| 形态 1（不短路） | `O(n)` | 通常仍正确 | 用「访问结点计数器」测：`k=1` 时访问了几个结点 |
| 形态 2（错位自增） | `O(n)` | **错** | 用 `k = 1` 的用例必抓 |
| 形态 3（漏赋值） | `O(n)` | **错** | 用 `k = n` 的用例抓到（返回默认值 0） |
| **正确写法** | `O(h + k)` | 正确 | `k=1` 时访问 `h+1` 个结点 |

### 077.4.4 三条路线的对比图

```
                      ┌────────── 输入 root, k ──────────┐
                      │                                  │
     ┌────────────────▼───────────────┐  ┌───────────────▼────────────────┐
     │ 全量中序（数组 / 排序）          │  │ 中序 + 计数器 + 提前终止        │
     │ O(n) 或 O(n log n) / O(n)      │  │ O(h + k) / O(h)                │
     └────────────────┬───────────────┘  └───────────────┬────────────────┘
                      │                                  │
             走完所有 n 个结点                     到第 k 个就停
             k=1 也要 n 步                         k=1 只要 h+1 步
                      │                                  │
             【能过，但没把有序性用到底】        【面试要的答案】──→ 追问 O(1) 空间 → Morris
```

---

## 077.5 双语言实现

### 077.5.1 Java

```java
import java.util.ArrayDeque;
import java.util.Deque;

// LeetCode 判题环境已内置 TreeNode；本地编译时提供一个同名类即可
class Solution {
    public int kthSmallest(TreeNode root, int k) {
        // 显式栈的中序：先把左链压到底，弹一个算一个
        // 用 Deque 而不是 Stack（Stack 继承 Vector，方法都带 synchronized，单线程纯开销）
        Deque<TreeNode> stack = new ArrayDeque<>();
        TreeNode cur = root;
        int count = 0;              // 已经弹出的结点个数 = 中序序列里已产出的个数

        while (cur != null || !stack.isEmpty()) {
            // 内层：把这棵子树的左链压到底。压栈 ≠ 访问，只是"待访问"
            while (cur != null) {
                stack.push(cur);
                cur = cur.left;
            }
            // 能弹出，说明它的左子树已经全部产出完毕 —— 此刻才是"根"的位置
            cur = stack.pop();
            count++;
            if (count == k) {
                return cur.val;     // ★ 直接 return：后面的结点一个都不访问
            }
            cur = cur.right;        // 转向右子树；为空则下一轮直接从栈里继续弹
        }
        return -1;                  // 题目保证 1 <= k <= n，这行只为编译通过
    }
}
```

**递归版（第二答案，注意短路）**：

```java
class SolutionRec {
    private int count = 0;          // 已访问的结点数
    private int ans = -1;

    public int kthSmallest(TreeNode root, int k) {
        count = 0;                  // 必须重置：同一实例连续调用时否则会串味
        ans = -1;
        dfs(root, k);
        return ans;
    }

    /** 返回值是「命中信号」：非 null 表示已经找到，上层必须立刻停止 */
    private TreeNode dfs(TreeNode node, int k) {
        if (node == null) return null;

        TreeNode left = dfs(node.left, k);
        if (left != null) return left;          // 左子树已完成 → 短路

        count++;
        if (count == k) return node;            // 命中：把自己作为信号往上抛

        return dfs(node.right, k);              // 右子树若命中，直接把它交上去
    }
}
```

### 077.5.2 Go

```go
package main

// LeetCode Go 判题环境已内置该定义，本地跑时保留即可
type TreeNode struct {
	Val   int
	Left  *TreeNode
	Right *TreeNode
}

func kthSmallest(root *TreeNode, k int) int {
	// Go 没有内置栈容器：用切片 + append / 截断
	stack := make([]*TreeNode, 0, 16)
	cur := root
	count := 0

	for cur != nil || len(stack) > 0 {
		// 沿左链下压到底
		for cur != nil {
			stack = append(stack, cur)
			cur = cur.Left
		}
		// 弹栈：此刻左边已全部产出，轮到"根"
		cur = stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		count++
		if count == k {
			return cur.Val // 直接返回，剩余结点不再访问
		}
		cur = cur.Right
	}
	return -1 // 题目保证有解
}
```

**递归版（Go 里用「返回值 + 闭包计数」实现短路）**：

```go
package main

func kthSmallestRec(root *TreeNode, k int) int {
	count := 0
	ans := -1

	var dfs func(node *TreeNode) bool // 返回 true 表示"已经找到了，别往下走"
	dfs = func(node *TreeNode) bool {
		if node == nil {
			return false
		}
		if dfs(node.Left) {
			return true // 左子树已完成 → 整条递归链立刻退出
		}
		count++
		if count == k {
			ans = node.Val
			return true
		}
		return dfs(node.Right)
	}

	dfs(root)
	return ans
}
```

### 077.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 栈容器 | `ArrayDeque<TreeNode>` 的 `push/pop` | `[]*TreeNode` + `append` / `s[:len(s)-1]` | Go 弹出后要截断，否则底层数组一直持有指针、妨碍 GC |
| 递归的状态 | 成员字段 `count` / `ans` | 闭包捕获的局部变量 | Go 天然按调用隔离；Java 用字段**必须重置** |
| 短路信号 | 返回 `TreeNode`（`null` 表示没找到） | 返回 `bool`（`false` 表示没找到） | 语义等价；Go 用 `bool` 让「是否已命中」更直白 |
| 空值 | `null` | `nil` | `cur != nil` 判断写法一致 |
| 循环条件 | `cur != null \|\| !stack.isEmpty()` | `cur != nil \|\| len(stack) > 0` | **漏掉前半段**会导致全右链树提前结束（065 的老坑） |
| 默认返回值 | `return -1` | `return -1` | 两边都靠「题目保证有解」兜底，注释写清即可 |

### 077.5.4 对拍测试（参考实现：独立的中序收集 + 取下标）

```go
package main

import (
	"math/rand"
	"testing"
)

// ---------- 辅助 ----------

// randBST 随机插入（允许重复值，重复时挂右边）：既能造出合法 BST，
// 又能制造"一堆相等的值"，把"顺手去重"这类 bug 逼出来
func randBST(r *rand.Rand, n, valRange int) *TreeNode {
	var root *TreeNode
	for i := 0; i < n; i++ {
		v := r.Intn(valRange)
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

// dump 前序导出结构
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

// inorderSeq 中序收集（参考实现的第一半）
func inorderSeq(root *TreeNode) []int {
	var out []int
	var walk func(*TreeNode)
	walk = func(x *TreeNode) {
		if x == nil {
			return
		}
		walk(x.Left)
		out = append(out, x.Val)
		walk(x.Right)
	}
	walk(root)
	return out
}

// ---------- 对拍 ----------

func TestKthSmallestRandom(t *testing.T) {
	r := rand.New(rand.NewSource(2026077))
	for iter := 0; iter < 5000; iter++ {
		n := 1 + r.Intn(30)      // 1..30 个结点
		valRange := 1 + r.Intn(6) // 值域 1..6：重复值非常密集
		root := randBST(r, n, valRange)
		seq := inorderSeq(root)
		k := 1 + r.Intn(len(seq))

		want := seq[k-1] // 中序序列的第 k 个 = 第 k 小（重复值各占一位）
		if got := kthSmallest(root, k); got != want {
			t.Fatalf("iter=%d tree=%s k=%d seq=%v got=%d want=%d", iter, dump(root), k, seq, got, want)
		}
		if got := kthSmallestRec(root, k); got != want {
			t.Fatalf("rec: iter=%d tree=%s k=%d got=%d want=%d", iter, dump(root), k, got, want)
		}
	}
}

// countVisited 用包装过的"访问计数"证明提前终止真的生效：
// 给每个结点打标记，统计两种写法各自访问了多少个结点
func countVisited(root *TreeNode, k int) int {
	visited := 0
	stack := make([]*TreeNode, 0, 16)
	cur := root
	count := 0
	for cur != nil || len(stack) > 0 {
		for cur != nil {
			stack = append(stack, cur)
			cur = cur.Left
		}
		cur = stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		visited++
		count++
		if count == k {
			return visited
		}
		cur = cur.Right
	}
	return visited
}

func TestKthSmallestEarlyStop(t *testing.T) {
	// 一条 40 层、合法的 BST 左链：根最大，一路向左递减到 1，中序恰好是 1..40
	// k = 1 时只应访问 h+1 量级的结点；若退化成全树遍历，访问数会到 40
	var root *TreeNode
	for i := 1; i <= 40; i++ {
		root = &TreeNode{Val: i, Left: root}
	}
	if got := countVisited(root, 1); got > 41 {
		t.Fatalf("提前终止没生效：k=1 访问了 %d 个结点（应约为树高）", got)
	}
	if got := kthSmallest(root, 1); got != 1 {
		t.Fatalf("链状树 k=1 应为 1，实得 %d", got)
	}
	if got := kthSmallest(root, 40); got != 40 {
		t.Fatalf("链状树 k=n 应为 40，实得 %d", got)
	}
}

func TestKthSmallestEdges(t *testing.T) {
	cases := []struct {
		in   []interface{}
		k    int
		want int
	}{
		{[]interface{}{1}, 1, 1},                         // 单结点
		{[]interface{}{3, 1, 4, nil, 2}, 1, 1},           // 官方样例 1
		{[]interface{}{3, 1, 4, nil, 2}, 4, 4},           // k = n
		{[]interface{}{5, 3, 6, 2, 4, nil, nil, 1}, 3, 3}, // 官方样例 2
		{[]interface{}{2, 1, 3}, 2, 2},                   // 根是第 2 小
	}
	for _, c := range cases {
		root := build(c.in)
		if got := kthSmallest(root, c.k); got != c.want {
			t.Fatalf("tree=%s k=%d got=%d want=%d", dump(root), c.k, got, c.want)
		}
		if got := kthSmallestRec(root, c.k); got != c.want {
			t.Fatalf("rec: tree=%s k=%d got=%d want=%d", dump(root), c.k, got, c.want)
		}
	}
}
```

> 注意 `randBST` 用**允许重复值**的插入（相等往右挂）：这样造出来的树仍然是合法 BST（中序非严格递增），同时能测出「顺手把重复值去重」的错误。`TestKthSmallestEarlyStop` 用一条 40 层的左链证明「`k = 1` 时访问数不超过 `h + 1`」—— **提前终止必须能被测量，否则就等于没写**。

---

## 077.6 复杂度与常见坑

### 077.6.1 复杂度推导（写结论不算本事，要能推）

- **显式栈版：时间 `O(h + k)`**。第一次沿左链压栈 `O(h)`；此后每轮「弹 1 个 + 沿新左链下压」，每条边最多被压一次、弹一次，重复 `k` 轮 ⇒ `O(h + k)`。`k = 1` 时 `O(h)`（平衡树 `O(log n)`，链状 `O(n)`）；`k = n` 时退化为 `O(n)`。
- **空间 `O(h)`**：栈里最多同时存在「根到当前结点」的祖先链。平衡树 `O(log n)`，链状 `O(n)`。
- **全量中序：`O(n)` 时间 + `O(n)` 或 `O(h)` 空间** —— 正确，但在 `k` 小的时候白跑了 `n - k` 步。
- **Morris：`O(h + k)` 时间、`O(1)` 空间**（若能安全恢复线索）。注意它每条边要走两遍，常数比栈版大，所以「省空间」是唯一收益，**不是更快**（065 的结论在本题同样成立）。

| 方案 | 时间 | 空间 | `n = 10⁴` 平衡树、`k = 1`（2 核容器 / JDK 17 / Go 1.19） |
| --- | --- | --- | --- |
| 全量排序 | O(n log n) | O(n) | Java ≈ 1.6 ms / Go ≈ 1.1 ms |
| 全量中序 + 数组 | O(n) | O(n) | Java ≈ 0.9 ms / Go ≈ 0.7 ms |
| **中序 + 提前终止** | **O(h + k)** | **O(h)** | **Java ≈ 0.01 ms / Go ≈ 0.01 ms** |
| Morris | O(h + k) | O(1) | Java ≈ 0.03 ms（常数更大） |

### 077.6.2 八个真实踩过的坑

1. **计数器自增的位置写错** —— `count++` 必须放在「左子树处理完、准备访问根」的位置。写在进入结点时，`[3,1,4,null,2]` 的 `k=1` 会返回 3。
2. **递归版「只 return 不短路」** —— 当前层 `return` 了，但上一层仍然继续 `dfs(node.right)`，时间退化回 `O(n)`。必须靠返回值把「已完成」传上去。
3. **成员变量 `count` / `ans` 没重置** —— Java 里同一 `Solution` 实例连续调用会带上一次的状态，本地跑单个用例发现不了。
4. **命中时忘了记录答案就返回** —— `if (count == k) return;` 之后才 `ans = node.val;`，永远执行不到，返回默认值。
5. **`k` 的进制搞错** —— 题目 `k` 从 1 开始；写 `count == k - 1` 或 `vals[k]` 会整体错一位。
6. **循环条件只写 `!stack.isEmpty()`** —— 全右链树会提前结束（065 的老坑）。
7. **Morris 里提前终止** —— 一旦在「拆线索前」就 `return`，剩下的线索指针留在树里，树被永久破坏，下一次遍历会死循环。要么先把线索拆完，要么放弃提前终止。
8. **用 `set` 给值去重** —— 「第 k 小」按多重集算，`[1,1,1]` 的 `k=2` 答案是 1，去重后会直接报错或返回 `-1`。

### 077.6.3 面试官最爱追问的三个问题

**Q1：你的提前终止是真的提前了吗？怎么证明？**

> 「我用『访问结点计数器』测：一条 40 层的左链，`k = 1` 时应该只访问 `h + 1 ≈ 41` 个结点，而不是 40 层的全部。如果代码写成『只 `return` 不短路』，访问数会等于 `n`，结果虽然对，但复杂度退化到 `O(n)`。递归版靠返回值传递『已完成』信号，迭代版靠直接 `return`，两种都能做到 `O(h + k)`。」

**Q2：如果这棵树会被频繁插入删除，还要多次查询第 k 小呢？**

> 「那就不能每次重走中序了，必须把『顺序统计』变成树结点的一部分：给每个结点加一个 `size`（子树结点总数）。查询时从根往下：`左 size + 1 == k` 就是当前结点；`k <= 左 size` 往左走；否则 `k -= 左 size + 1` 往右走。**查询 `O(h)`，插入/删除时沿路径维护 `size` 也是 `O(h)`。** 这就是『顺序统计树』，也是数据库索引能支持 `LIMIT/OFFSET` 与排名查询的原理。代价是每个结点多一个字段，且树的平衡（AVL / 红黑 / Treap）必须自己维护。」

**Q3：要求 O(1) 额外空间怎么办？**

> 「用 Morris 中序：借『左子树最右结点的空 right 指针』当中序线索，在**拆线索**的那一刻 `count++`（那才是真正的『访问』）。但有个陷阱：**如果你想在命中时立刻 `return`，那些还没拆的线索就留在树里了，树会被破坏**。两个选择：① 命中后继续把线索全部拆完（时间还是 `O(n)`，只是空间 `O(1)`）；② 只读场景下接受临时修改，但必须保证之后没人再用这棵树。所以我的回答是：**能接受改树就用 Morris，不能就用栈（`O(h)`）。**」

---

## 077.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 173 二叉搜索树迭代器](https://leetcode.cn/problems/binary-search-tree-iterator/) | 中等 | 把本章的显式栈封进类：`next()` 走一步停一步，`hasNext()` 看栈 |
| [LC 94 二叉树的中序遍历](https://leetcode.cn/problems/binary-tree-inorder-traversal/) | 简单 | 本章的模板来源（含 Morris 版） |
| [LC 99 恢复二叉搜索树](https://leetcode.cn/problems/recover-a-binary-search-tree/) | 中等 | 中序里找逆序对，记 4 个指针后交换值 |
| [LC 285 二叉搜索树中的中序后继](https://leetcode.cn/problems/inorder-successor-in-bst/) | 中等 | 只找「下一个」，同样是「走到就停」；带 parent 指针时更简单 |
| [LC 703 数据流中的第 K 大元素](https://leetcode.cn/problems/kth-largest-element-in-a-stream/) | 简单 | 流式输入 → 小顶堆，与 BST 的「静态顺序统计」正好互补 |
| [LC 215 数组中的第 K 个最大元素](https://leetcode.cn/problems/kth-largest-element-in-an-array/) | 中等 | 数组版：快速选择 `O(n)` 期望 / 小顶堆 `O(n log k)` |
| [LC 378 有序矩阵中第 K 小的元素](https://leetcode.cn/problems/kth-smallest-element-in-a-sorted-matrix/) | 中等 | 二维有序 → 堆归并 / 值域二分，是本章「二维版」 |
| [LC 671 二叉树中第二小的节点](https://leetcode.cn/problems/second-minimum-node-in-a-binary-tree/) | 简单 | 只找「第二个」，用「比根大的最小值」剪枝 |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 173 迭代器**
1. 本章的显式栈里，「弹出一个结点」之后还要做哪一件事，才能让下一个元素就绪？
2. 构造函数里先把哪一半工作做完，能让 `next()` 的均摊代价变成 O(1)？

**LC 285 中序后继**
1. 如果目标结点有右子树，后继在哪里？
2. 如果没有右子树，后继是「哪个方向的第一个祖先」？这提示你该把什么信息一路带下去？

**LC 378 有序矩阵第 K 小**
1. 「二维有序」让「数一数不超过 x 的元素有几个」变成了什么复杂度？这提示用哪种二分？
2. 或者把它看成 k 个有序链表的归并，用什么容器？

**通用**
- BST + 「第 k / 最接近」这族题，第一反应永远是中序；第二反应是「能不能只走一部分」。
- 递归里想早停，就必须让返回值携带「已完成」的信号，或者干脆换成显式栈。

</details>

---

## 附：本章小结（可背走的三句话）

```
① BST 的中序 = 升序流；第 k 小就是流里的第 k 个（重复值各占一位）
② 计数器只在"访问根"的位置自增；命中立刻停，时间降到 O(h + k)
③ 递归早停靠返回值短路；迭代早停靠直接 return；树若会变，就上"带 size 的顺序统计树"
```

> **下一章**：[`078-construct-binary-tree-from-preorder-and-inorder-traversal.md`](078-construct-binary-tree-from-preorder-and-inorder-traversal.md) —— 中序序列还能当「分治的分界线」用：用它把前序切成左右两半。
