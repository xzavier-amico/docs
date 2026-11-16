# 053. LC138 随机链表的复制（Copy List with Random Pointer）

> **状态**：`已完成（初稿）` ｜ **所属**：第 6 部分 · 链表 ｜ **编号**：053 / 150
> **难度**：中等 ｜ **标签**：链表 · 哈希表 · 深拷贝 · 原地编织
> **一句话**：这道题真正考的是「**当新结点还没创建时，怎么给『指向一个旧结点的指针』找到对应的新结点**」——哈希解法用表建档，最优解法把表**编进链表本身**。
> **前置**：[`047-merge-two-sorted-lists.md`](047-merge-two-sorted-lists.md)（哑结点与「边遍历边构造」）、[`044-reverse-linked-list.md`](044-reverse-linked-list.md)（指针别名与「先存后断」）

---

## 053.1 题目

**原题链接**：<https://leetcode.cn/problems/copy-list-with-random-pointer/>

### 题意重述（把隐含条件补齐）

给你一个长度为 `n` 的链表，每个结点包含一个额外指针 `random`，它可以指向链表中的**任意结点**，也可以为 `null`。请构造这条链表的**深拷贝**：返回一条**全新**的链表，其中每个结点都是**新建**的，`next` 与 `random` 的相对结构与原链表完全一致。

六个必须读出来的条件：

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| **深拷贝** | 「深拷贝」 | 返回原链表的引用（浅拷贝）直接错；共享任何一个结点都算错 |
| **`random` 可以为 `null`** | 「或者指向 null」 | 不判空就 NPE / nil 解引用 |
| **`random` 可以指向自己** | 「指向链表中的任意节点」 | 单结点自指是必须跑通的边界 |
| **`random` 可以指向前面的结点** | 「任意」 | 所以不能假设「random 只指向已创建的结点」 |
| **`0 <= n <= 1000`** | 数据范围 | 规模小，哈希解法能过；考点是「能不能 O(1) 空间」 |
| **不能返回原结点** | 深拷贝语义 | 判题会比对「新链的结点指针是否与原链不同」 |

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| `0 <= n <= 1000` | 很小 | O(n²) 也能过（10⁶ 次），所以**考点不在时间** |
| `-10⁴ <= Node.val <= 10⁴` | 值可重复 | **绝对不能靠 `val` 建立映射** —— 相同值的结点是不同结点 |
| `random` 可指向任意结点 | 关键结构 | 这意味着「建结点」和「建 random 边」必须分两阶段，或者用某种方式让新结点可被找到 |
| 空间 O(1)（进阶） | 经典追问 | 交错复制（编织）是唯一方案 |

> 💡 **量级 → 算法**：`n ≤ 1000` 说明「哈希表 O(n) 空间」完全能过。**所以这题的分水岭仍然是能不能做到 O(1) 额外空间**，而不是能不能过。**链表章里凡是给「1000」这种小规模的题，几乎都在等你掏出那个 O(1) 的巧解。**

### 示例

```text
输入：head = [[7,null],[13,0],[11,4],[10,2],[1,0]]
输出：      [[7,null],[13,0],[11,4],[10,2],[1,0]]
解释：每个 [值, random 指向的下标]
      结点 0（值 7）random = null
      结点 1（值 13）random = 结点 0
      结点 2（值 11）random = 结点 4
      结点 3（值 10）random = 结点 2
      结点 4（值 1）random = 结点 0

输入：head = [[1,1],[2,1]]
输出：      [[1,1],[2,1]]
解释：结点 0 的 random 指向结点 1（自己指向后面的结点）

输入：head = [[3,null],[3,0],[3,null]]
输出：      [[3,null],[3,0],[3,null]]
解释：三个结点的值全是 3 —— 这就是「不能用 val 建映射」的证据
```

### 我自造的八个反例（写完必须逐个跑）

```text
① []                                  → 空链，返回 null
② [[7,null]]                          → 单结点，random 为 null
③ [[7,0]]                             → 单结点，random 指向自己（自环）
④ [[3,null],[3,0],[3,null]]           → 全相等值：靠 val 建映射必错
⑤ [[1,1],[1,0]]                       → 两结点互相指
⑥ 所有 random 全为 null                → 第二阶段一行都不该执行
⑦ n = 1000，random 随机                → 规模上限
⑧ 复制完成后原链表必须完好              → 交错法会临时改原链，必须还原
```

第 ⑧ 条是最容易被忽视的：**交错复制（编织）会临时修改原链表的结构**，如果拆分阶段写错，原链表就损坏了。判题机虽然只看返回值，但「不能破坏输入」是工程上的硬要求，也是面试官会专门检查的点。

---

## 053.2 从暴力到最优：第一个念头为什么错

### 第一步：暴力（两遍扫描 + 线性查找，O(n²)）

```text
// 第一遍：只建 next 链，值照抄
old = head; dummy = new Node(0); tail = dummy
while old != null:
    tail.next = new Node(old.val)
    tail = tail.next
    old = old.next
newHead = dummy.next

// 第二遍：修 random —— 每个结点都要回到原链里"找位置"
old = head; new = newHead
while old != null:
    if old.random != null:
        // 从两个链头同时出发，走到 random 指的那个结点
        p = head; q = newHead
        while p != old.random:
            p = p.next; q = q.next
        new.random = q
    old = old.next; new = new.next
```

- 时间 **O(n²)**（每个结点的 random 都要 O(n) 定位），空间 O(1)（不算结果链）。
- `n = 1000` 时约 10⁶ 次，能过。但面试会问「能不能 O(n)」。

### 第二步：卡点在哪

把问题翻译成一句话：

> **`random` 存的是「结点的地址」。新链里每个旧结点都有一个对应的新结点，但我需要能「按旧结点查新结点」。**

这就是一个**映射问题**：`old → new`。而映射的两种实现是「查表」和「算出来」。

卡点的另一种表现形式，是「**建边的顺序**」：

```
如果我按遍历顺序创建新结点：
  第 1 个结点创建时，它的 random 可能指向第 5 个 —— 尚不存在；
  于是必须先建完所有结点，再统一修 random —— 但那又需要"旧→新"的映射。
```

因此这题的解法分成两条路，区别只在「映射存在哪儿」：

| 路线 | 映射存在哪儿 | 空间 |
| --- | --- | --- |
| 哈希表 | `Map<旧结点, 新结点>` | O(n) |
| **交错复制** | **编进原链表本身**：旧结点的 `next` 指向它的副本 | **O(1)** |

### 第三步：灵感的来源 —— 把「表」编进链表

哈希表在存什么？存的是「旧结点 → 新结点」。那么有没有办法**不用任何额外容器**，就能从旧结点 O(1) 找到新结点？

有 —— **让旧结点的 `next` 直接指向它的副本**：

```
原链：  A ──→ B ──→ C ──→ ∅
编织后：A ──→ A' ──→ B ──→ B' ──→ C ──→ C' ──→ ∅
                ↑
        从 A 走一步就是 A' —— 这就是「用结构当哈希表」
```

于是：

- **从旧结点找新结点** = `old.next`，O(1)；
- **新结点的 `next`** 暂时指向「旧结点的下一个旧结点」，等拆分时再修好。

一旦建立了这个映射，修 `random` 就变成一行：

```
old.next.random = old.random.next
    ↑                ↑
  副本              old.random 的副本（因为 old.random 的 next 就是它的副本）
```

**这就是本题最漂亮的一步**：`old.random.next` 之所以是「`old.random` 的副本」，正是因为编织不变量保证了「每个旧结点的 `next` 都是它的副本」。**一个不变量同时解决了「怎么查」和「怎么连」。**

### 另一条路：递归 + 哈希（DFS 克隆）

```text
clone(node, map):
    if node == null: return null
    if map has node: return map[node]
    copy = new Node(node.val)
    map[node] = copy
    copy.next = clone(node.next, map)
    copy.random = clone(node.random, map)
    return copy
```

- 时间 O(n)，空间 O(n)（哈希 + 递归栈）。
- 它的价值是**通用性**：同样的模板可以克隆**任意图**（LC 133）。链表只是「每个结点最多两条出边」的特例。
- 缺点：空间 O(n)，且 `random` 可能指回前面，所以必须用「先登记再递归」的顺序（否则会无限递归）。

---

## 053.3 原理拆解

### 053.3.1 数据结构与算法选型

| 方案 | 时间 | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- |
| 两遍 + 线性查找定位 random | **O(n²)** | O(1) | 25 行 | n 大时超时；代码最啰嗦 |
| 哈希映射（两遍） | O(n) | **O(n)** | 15 行 | 空间要求 O(1)；且需要额外 O(n) 的 map |
| 递归 DFS + 哈希 | O(n) | O(n)（map + 栈） | 12 行 | 同上；但通用性最好（可克隆图） |
| **交错复制（编织 → 修 random → 拆分）** ✅ | **O(n)** | **O(1)** | **30 行** | 会临时修改原链表（必须还原）；非线程安全 |
| 先建值数组 + 下标映射 | O(n) | O(n) | 20 行 | 等价于哈希，但更绕 |

**为什么交错复制最优**：唯一同时满足 O(n) 时间 + O(1) 额外空间的方案。代价是「必须还原原链表」和「不能并发执行」，面试要主动说出这两点。

### 053.3.2 为什么这个做法是对的（正确性论证）

分三个阶段，每个阶段一个不变量。

#### 阶段 ①：编织（interleave）

```text
for p = head; p != null; p = p.next.next:
    cp = new Node(p.val)
    cp.next = p.next
    p.next = cp
```

**不变量**：处理完前 k 个原结点后，链表形如

```
p1 → p1' → p2 → p2' → … → pk → pk' → p(k+1) → …
```

即「**每个原结点的 `next` 都是它的副本**」，且每个副本的 `next` 是「原结点的原后继」。

**正确性**：循环体先把 `cp.next` 设为 `p.next`（保留原后继），再把 `p.next` 设为 `cp`（插入）。`p = p.next.next` 跳过副本，落到下一个原结点。∎

**这正是「用结构当哈希表」的实现**：不变量保证了 `old.next == 副本(old)`，这个性质等价于一个 O(1) 的映射表。

#### 阶段 ②：修 `random`

```text
for p = head; p != null; p = p.next.next:
    if p.random != null:
        p.next.random = p.random.next
```

**要证**：`p.next` 是 `p` 的副本，且修改后它的 `random` 指向「`p.random` 的副本」。

1. 由阶段 ① 的不变量，`p.next` 就是 `p` 的副本。✅
2. 由同一不变量，`p.random.next` 就是 `p.random` 的副本（因为 `p.random` 是原结点，它的 `next` 是它的副本）。✅
3. 所以 `p.next.random = p.random.next` 恰好把「副本的 random」设为「原 random 的副本」，与深拷贝语义一致。✅

**`p.random == null` 时必须跳过**：否则 `p.random.next` 会 NPE / nil 解引用。∎

#### 阶段 ③：拆分

```text
dummy = new Node(0); tail = dummy
for p = head; p != null; :
    cp = p.next          // 副本
    p.next = cp.next     // 原链跳过副本 → 恢复原链
    tail.next = cp       // 副本链尾插
    tail = cp
    p = p.next           // 直接跳到下一个原结点
```

**要证**：拆分后 ① 原链表完全恢复原状；② 副本链的 `next` 关系与原链一致。

1. **原链恢复**：`p.next = cp.next`，而由阶段 ① 的不变量 `cp.next` 正是「`p` 的原后继」，所以原链的每条 `next` 都被还原。✅
2. **副本链正确**：阶段 ① 中 `cp.next` 曾被设为「原后继」；但在拆分时，我们**先把 `cp` 挂到副本链尾**（`tail.next = cp`），而下一个副本会在下一轮被挂到 `cp` 后面 —— 覆盖了 `cp.next` 的旧值。最后一轮的 `cp.next` 需要特别检查：它是最后一个副本，其 `cp.next` 是 `null`（原链表的尾巴的 `next` 是 `null`），正好是副本链的尾巴。✅
3. **顺序问题**：必须先 `p.next = cp.next` 再 `p = p.next`。若反过来，`p` 会走到副本上，下一轮 `cp = p.next` 就取到了「副本的 next」，整条链错位。∎

**合并结论**：三个阶段合起来，产出的是「结构与原链完全一致、且不含任何原结点」的新链，即深拷贝。∎

> **这套证明的方法论**：**每个阶段都定义一个可检验的形状（不变量），然后证明「这个形状让下一阶段变成一行代码」。** 阶段 ① 的形状让阶段 ② 的 random 修正变成一行；阶段 ② 不改形状，让阶段 ③ 的拆分逻辑保持简单。**这就是「原地算法」最典型的结构：一个不变量串起三个阶段。**

### 053.3.3 通用解题思路（可迁移的那一层）

**链表章的统一语言：哑结点 + 指针三件套（prev/cur/next）+ 先存后断。** 053 展示的是这套语言里一个更高阶的技巧 —— **用结构本身当容器**：

```
需求：需要一个 old → new 的映射
      ↓ 两条路
路线 A：显式容器（哈希表）         → 空间 O(n)
路线 B：把映射"编进"已有结构        → 空间 O(1)
        └─ 具体做法：让 old.next = new，于是映射 = 走一步
```

这条「**把额外空间藏进已有结构**」的思路，在数组题里有一个著名的孪生兄弟：

| 场景 | 「容器」被什么替代 | 章 |
| --- | --- | --- |
| 038 缺失的第一个正数 | 数组下标当哈希桶（把值 x 放到位置 x−1） | 第 5 部分 |
| 039 寻找重复数 | 数组本身当链表（`i → nums[i]`），Floyd 判圈 | 第 5 部分 |
| 040 找到所有消失的数字 | 用正负号当「出现过」的标记 | 第 5 部分 |
| **053 随机链表的复制** | **旧结点的 `next` 指向副本** | 本章 |
| 043 O(1) 随机集合 | 数组 + 下标映射表 | 第 5 部分 |

> **一句话**：**当「需要 O(n) 的映射」但「不允许 O(n) 空间」时，去找一个「本来就存在的、可以临时借用的结构」，把映射编进去。** 这类技巧的代价通常是「必须能还原」或「不能并发」，面试时要主动说出来。

而本题还有另一层可迁移的东西 —— **「深拷贝」的通用套路**：

```
第 1 遍：只复制"结点本身"，建立 旧→新 的映射
第 2 遍：按映射复制"边"
```

任何「带指针的图结构拷贝」都是这个套路（链表、图、二叉树带 random 指针）。区别只在「映射存在哪儿」。

属于同一模型的题：

| 题 | 复制什么 | 映射怎么存 |
| --- | --- | --- |
| [LC 138](https://leetcode.cn/problems/copy-random-pointer/) | 链表 + random | 哈希 / 编织 |
| [LC 133 克隆图](https://leetcode.cn/problems/clone-graph/) | 无向图 | 必须哈希（图没有"空位"可借） |
| [LC 1485 克隆含随机指针的二叉树](https://leetcode.cn/problems/clone-binary-tree-with-random-pointer/) | 二叉树 + random | 哈希 |
| [LC 146 LRU 缓存](https://leetcode.cn/problems/lru-cache/) | 不是拷贝，但同样「哈希 + 双向链表」 | 054 |
| [LC 38 外观数列](https://leetcode.cn/problems/count-and-say/) | 字符串生成 | 不需要映射 |

### 053.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| **不允许修改原链表** | 编织法失效 | 只能用哈希（O(n) 空间）；或在编织前先整条复制一份再操作 |
| **多线程并发读原链表** | 编织期间原链结构被改，读者会看到脏数据 | 必须加锁，或改用哈希法 |
| `random` 指向**链外结点** | 不是本节的问题 | 需要额外的「外部对象表」，本质是把映射域扩大 |
| 克隆**任意图**（133） | 结点可能有多个出边、有环 | 必须哈希 + DFS/BFS；编织法不适用（没有"空位"） |
| n 很大（10⁷） | O(1) 空间优势明显 | 编织法只需一次分配，哈希法要 10⁷ 条 entry |
| 要求**保持原链不变且 O(1) 空间** | 矛盾 | 不可能：O(1) 空间下要建立 n 条映射，必须有地方存放；编织法是"借用原链"，一旦禁止借用就无解 |

---

## 053.4 图解推演

### 053.4.1 三个阶段全景（`A→B→C`，A.random=C，B.random=A，C.random=null）

```text
阶段 ①：编织（把副本插到原结点后面）

  原链：  A ──→ B ──→ C ──→ ∅
          │     │     │
        random 指向：A→C, B→A, C→∅

  编织后：
          A ──→ A' ──→ B ──→ B' ──→ C ──→ C' ──→ ∅
          ↑      ↑     ↑      ↑     ↑      ↑
          原    副本    原    副本    原    副本

  不变量：每个原结点的 next 就是它的副本
          A.next = A'，B.next = B'，C.next = C'
```

```text
阶段 ②：修 random（一行搞定）

  处理 A：A.random = C，所以 A.next.random = A.random.next = C.next = C'
          A'.random = C'  ✅

  处理 B：B.random = A，所以 B.next.random = A.next = A'
          B'.random = A'  ✅

  处理 C：C.random = ∅  →  跳过（不判空就 NPE）

  结果（只画副本的 random）：
          A' ┄┄→ C'
          B' ┄┄→ A'
          C' ┄┄→ ∅
```

```text
阶段 ③：拆分（恢复原链 + 抽出副本链）

  拆分前：A → A' → B → B' → C → C' → ∅
  p=A:
     cp = A'
     A.next = A'.next = B      ← 原链跳过副本
     tail.next = A'            → 副本链：A'
     p = B

  p=B:
     cp = B'
     B.next = B'.next = C
     tail.next = B'            → 副本链：A' → B'
     p = C

  p=C:
     cp = C'
     C.next = C'.next = ∅      ← 原链表尾恢复
     tail.next = C'            → 副本链：A' → B' → C'
     p = ∅

  结果：
    原链：  A → B → C → ∅                    （完全恢复）
    副本链：A' → B' → C' → ∅                 （新链，无共享结点）
```

### 053.4.2 三个阶段的指针状态表

| 阶段 | 原结点 `p` | `p.next`（处理后） | 副本 | 副本的 `next` | 说明 |
| --- | --- | --- | --- | --- | --- |
| ① 后 | A | A' | A' | B | 副本插在 A 与原后继之间 |
| ① 后 | B | B' | B' | C | |
| ① 后 | C | C' | C' | ∅ | |
| ② 后 | A | A' | A' | B | `A'.random = C'` |
| ② 后 | B | B' | B' | C | `B'.random = A'` |
| ② 后 | C | C' | C' | ∅ | `C'.random = ∅`（跳过） |
| ③ 后 | A | B | A' | B' | 原链恢复，副本链成形 |
| ③ 后 | B | C | B' | C' | |
| ③ 后 | C | ∅ | C' | ∅ | |

### 053.4.3 两条路线对比

```text
        ┌──────── 映射 old → new 存在哪儿？────────┐
        │                                          │
   ┌────▼──────────┐                     ┌─────────▼─────────┐
   │ 哈希表（显式） │                     │ 编织（隐式）       │
   │ O(n) 空间      │                     │ O(1) 空间          │
   └────┬──────────┘                     └─────────┬─────────┘
        │                                          │
   两遍扫描：                       三阶段：
   ① 建结点 + 填 map                ① 把副本插进原链
   ② 按 map 连 next/random          ② 用 old.random.next 修 random
        │                            ③ 拆开并恢复原链
        │                                          │
   ✅ 不改原链                         ✅ O(1) 空间
   ✅ 线程安全                         ❌ 临时改原链（必须还原）
   ✅ 通用（图也适用）                  ❌ 非线程安全
```

---

## 053.5 双语言实现

### 053.5.1 Java

```java
import java.util.HashMap;
import java.util.Map;

// LeetCode 判题环境已提供 Node（val / next / random），本地自建
class Node {
    int val;
    Node next;
    Node random;

    Node(int val) {
        this.val = val;
        this.next = null;
        this.random = null;
    }
}

class Solution {
    // 解法一：哈希映射。两遍扫描，第一遍造结点，第二遍接边
    public Node copyRandomList(Node head) {
        if (head == null) {
            return null;
        }
        Map<Node, Node> map = new HashMap<>();
        for (Node p = head; p != null; p = p.next) {
            map.put(p, new Node(p.val));
        }
        for (Node p = head; p != null; p = p.next) {
            // p.next 可能为 null，map.get(null) 返回 null，正好对应新链的尾巴
            map.get(p).next = map.get(p.next);
            map.get(p).random = map.get(p.random);
        }
        return map.get(head);
    }

    // 解法二：交错复制。A → A' → B → B' → C → C'，再拆开，零额外结构
    public Node copyRandomListWeave(Node head) {
        if (head == null) {
            return null;
        }
        // ① 编织
        for (Node p = head; p != null; p = p.next.next) {
            Node cp = new Node(p.val);
            cp.next = p.next;
            p.next = cp;
        }
        // ② 修 random：原 random 的下一个就是它的副本
        for (Node p = head; p != null; p = p.next.next) {
            if (p.random != null) {
                p.next.random = p.random.next;
            }
        }
        // ③ 拆开：原链复原，副本链抽出
        Node dummy = new Node(0);
        Node tail = dummy;
        for (Node p = head; p != null; ) {
            Node cp = p.next;   // 副本
            p.next = cp.next;   // 原链跳过副本
            tail.next = cp;     // 副本链尾插
            tail = cp;
            p = p.next;
        }
        return dummy.next;
    }
}
```

**五处「为什么」**：

1. 哈希版里 `map.get(p.next)` 在 `p.next == null` 时返回 `null` —— 正好对应「新链的尾巴」。**不用特判 `p.next` 是否为空**，这是 `HashMap` 允许 `null` key 的红利（Go 的 `map` 也允许 nil key，但用指针类型时更安全）。
2. 编织版的循环步长是 `p = p.next.next`：跳过刚插入的副本。
3. `p.next.random = p.random.next` 前必须判 `p.random != null`，否则 NPE。
4. 拆分时**先** `p.next = cp.next`（恢复原链），**再** `p = p.next`。顺序反了会走到副本上。
5. `dummy` 是副本链的容器；返回 `dummy.next` 而不是 `head`（`head` 是原链的头）。

### 053.5.2 Go

```go
package main

// Node 是带随机指针的链表结点（LeetCode 判题环境已提供）
type Node struct {
	Val    int
	Next   *Node
	Random *Node
}

// 交错复制（原地编织）：A→A'→B→B'→C→C'，再拆成两条链
func copyRandomList(head *Node) *Node {
	if head == nil {
		return nil
	}

	// ① 编织：把副本插到原结点后面
	for p := head; p != nil; p = p.Next.Next {
		cp := &Node{Val: p.Val, Next: p.Next}
		p.Next = cp
	}

	// ② 修 Random：副本的 random = 原 random 的副本（即原 random 的下一个）
	for p := head; p != nil; p = p.Next.Next {
		if p.Random != nil {
			p.Next.Random = p.Random.Next
		}
	}

	// ③ 拆分：恢复原链，抽出副本链
	dummy := &Node{}
	t := dummy
	for p := head; p != nil; {
		cp := p.Next     // 副本
		p.Next = cp.Next // 原链跳过副本
		t.Next = cp      // 副本链尾插
		t = cp
		p = p.Next // 跳到下一个原结点
	}
	return dummy.Next
}

// 哈希映射版：先复制结点，再补两条指针
func copyRandomListHash(head *Node) *Node {
	if head == nil {
		return nil
	}
	m := make(map[*Node]*Node)
	for p := head; p != nil; p = p.Next {
		m[p] = &Node{Val: p.Val}
	}
	for p := head; p != nil; p = p.Next {
		m[p].Next = m[p.Next]     // nil 的 key 取出来也是 nil，无需特判
		m[p].Random = m[p.Random]
	}
	return m[head]
}
```

### 053.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| `map` 存 `null` key | `HashMap` 允许 `null` key，`get(null)` 返回 `null` | `map[*Node]*Node` 允许 `nil` key，`m[nil]` 返回零值 `nil` | **两边都能省掉判空**；但 Go 若用 `m[p.Next]` 且 `p.Next == nil`，取到的是零值 `nil`，刚好正确 |
| map 查找不存在的 key | 返回 `null`（需靠 `containsKey` 区分） | 返回零值（**不 panic**） | 本题里「不存在的 key」只会是 `nil` 本身，所以都安全 |
| 结点构造 | `new Node(p.val)` 用构造器 | `&Node{Val: p.Val, Next: p.Next}` 字面量 | Go 可以一次构造带 `Next`，编织阶段一行搞定 |
| 空判断 | `head == null` | `head == nil` | 一致 |
| 字段名 | `.val` / `.next` / `.random` | `.Val` / `.Next` / `.Random` | 大写首字母 |
| 多返回值 | 无 | 有（本题用不上） | — |

> **一个 Go 特有的注意点**：如果 `map` 的 key 类型是结构体（而不是指针），Go 会用**值相等**（逐字段比较）来查找，那就会把所有「值相同的结点」当成同一个 key —— 这与 Java 的 `HashMap` 用 `equals`/`hashCode` 是同一类陷阱。**本题必须用 `map[*Node]*Node`（指针当 key），不能用 `map[Node]*Node`。**

### 053.5.4 对拍测试（Go，2 万次随机 + 深拷贝校验 + 八个边界）

```go
package main

import (
	"math/rand"
	"testing"
)

// buildRandom 用「值数组 + random 下标数组」建一条带随机指针的链
func buildRandom(a []int, randIdx []int) *Node {
	if len(a) == 0 {
		return nil
	}
	nodes := make([]*Node, len(a))
	for i := range a {
		nodes[i] = &Node{Val: a[i]}
	}
	for i := 0; i < len(a)-1; i++ {
		nodes[i].Next = nodes[i+1]
	}
	for i, r := range randIdx {
		if r >= 0 {
			nodes[i].Random = nodes[r]
		}
	}
	return nodes[0]
}

// dumpRandom 输出 (Val, Random 的下标)，无 random 记 -1
func dumpRandom(h *Node) [][2]int {
	idx := make(map[*Node]int)
	i := 0
	for p := h; p != nil; p = p.Next {
		idx[p] = i
		i++
	}
	out := [][2]int{}
	for p := h; p != nil; p = p.Next {
		r := -1
		if p.Random != nil {
			r = idx[p.Random]
		}
		out = append(out, [2]int{p.Val, r})
	}
	return out
}

func sameRandom(a, b [][2]int) bool {
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

func TestCopyRandomListRandom(t *testing.T) {
	r := rand.New(rand.NewSource(53))
	for iter := 0; iter < 20000; iter++ {
		n := r.Intn(9) // 含 0（空链）
		a := make([]int, n)
		randIdx := make([]int, n)
		for i := range a {
			a[i] = r.Intn(10) // 值域很小 → 大量重复值
			if r.Intn(4) == 0 {
				randIdx[i] = -1 // 1/4 概率为 nil
			} else if n > 0 {
				randIdx[i] = r.Intn(n) // 可能指向自己（i == randIdx[i]）
			}
		}
		orig := buildRandom(a, randIdx)
		want := dumpRandom(orig)

		for name, cp := range map[string]*Node{
			"weave": copyRandomList(orig),
			"hash":  copyRandomListHash(orig),
		} {
			// ① 结构必须与原链一致
			if got := dumpRandom(cp); !sameRandom(got, want) {
				t.Fatalf("%s iter=%d a=%v ri=%v got=%v want=%v", name, iter, a, randIdx, got, want)
			}
			// ② 不能共享任何原结点（否则是浅拷贝）
			origSet := make(map[*Node]bool)
			for p := orig; p != nil; p = p.Next {
				origSet[p] = true
			}
			for p := cp; p != nil; p = p.Next {
				if origSet[p] {
					t.Fatalf("%s iter=%d 共享了原结点，不是深拷贝", name, iter)
				}
			}
		}
		// ③ 原链必须被复原（编织法会临时改它）
		if got := dumpRandom(orig); !sameRandom(got, want) {
			t.Fatalf("iter=%d 原链表被破坏: got=%v want=%v", iter, got, want)
		}
	}
	// 八个边界
	if copyRandomList(nil) != nil || copyRandomListHash(nil) != nil {
		t.Fatal("空链表必须返回 nil")
	}
	one := buildRandom([]int{7}, []int{0})
	cp := copyRandomList(one)
	if cp == one || cp.Val != 7 || cp.Random != cp || cp.Next != nil {
		t.Fatal("单结点自指（random 指向自己）失败")
	}
	allNil := buildRandom([]int{1, 1, 1}, []int{-1, -1, -1})
	if got := dumpRandom(copyRandomList(allNil)); !sameRandom(got, [][2]int{{1, -1}, {1, -1}, {1, -1}}) {
		t.Fatal("全 nil random 失败")
	}
	// 全相等值 + 交叉 random
	same := buildRandom([]int{3, 3, 3}, []int{1, 0, 1})
	if got := dumpRandom(copyRandomList(same)); !sameRandom(got, [][2]int{{3, 1}, {3, 0}, {3, 1}}) {
		t.Fatal("全相等值 + 交叉 random 失败")
	}
}
```

> **三个断言层次值得记住**：① 比对结构（`dumpRandom`）；② 比对**指针身份**（不在 `origSet` 里）—— 这才真正验证了「深」拷贝；③ 比对原链是否被复原 —— 交错法的独有风险。**「深拷贝」的测试必须同时验证这三件事，缺一件就可能放过浅拷贝或破坏输入的实现。**

---

## 053.6 复杂度与常见坑

### 053.6.1 复杂度推导

- **时间 O(n)**（三种解法都是）：
  - 哈希版：两遍扫描，每遍 n 次；哈希操作均摊 O(1)。
  - 编织版：三个阶段各一遍扫描，共 3n 次；每次常数操作。
- **空间**：
  - 哈希版 **O(n)**：n 条 `old → new` 的 entry（Java 的 `HashMap` 还有负载因子开销，约 1.3n 个桶）。
  - 编织版 **O(1)**：只用 `p`、`cp`、`dummy`、`t` 四个指针 + n 个新结点（新结点是**输出**，不算额外空间）。
  - **注意口径**：题目说「空间 O(1)」指的是**除输出之外**的额外空间。新链的 n 个结点是必须分配的，任何解法都有。
- **为什么编织版能省掉哈希表**：因为「旧结点 → 新结点」这个映射恰好可以存进「旧结点的 `next`」这个位置，而这个位置在拷贝过程中本来就空闲（副本的 `next` 暂时指向原后继，等拆分时才修正）。

### 053.6.2 八个真实踩过的坑

1. **`p.random.next` 没判空** —— `random == null` 时直接崩。这是最高频错误。
2. **用 `val` 建映射** —— `[[3,null],[3,0],[3,null]]` 三个结点值相同，映射会被覆盖，结果错。
3. **Go 里用 `map[Node]*Node`（值当 key 而不是指针）** —— 相同值的结点被当成同一个 key，结果错。必须 `map[*Node]*Node`。
4. **拆分时先 `p = p.next` 再 `p.next = cp.next`** —— `p` 跑到副本上，下一轮 `cp = p.next` 取错，整条链错位。
5. **编织后忘记拆分 / 拆分不彻底** —— 返回的链里混着原结点，不是深拷贝；或者原链被污染（原结点的 `next` 指向副本）。
6. **哈希版只做一遍（建结点时顺便连 random）** —— random 可能指向尚未创建的结点，`map.get` 得到 `null`，random 全变成 `null`。
7. **递归版没先登记 `map[node] = copy` 就递归** —— `random` 指回自己或前面时无限递归，栈溢出。
8. **返回 `head` 而不是新链头** —— 深拷贝必须返回新链的头；返回 `head` 就是浅拷贝。

### 053.6.3 面试官最爱追问的三个问题

**Q1：为什么可以用原链表的 `next` 存映射？代价是什么？**
> 「因为编织阶段副本就插在原结点后面，于是『旧结点的 `next`』这个位置天然可以表示『旧 → 新』这个映射，等于把一个 O(n) 的哈希表藏进了链表结构里。代价有两个：① 过程中原链表被临时修改，所以最后必须完整还原；② 这段时间内其他线程读到的是脏数据，所以不是线程安全的。如果题目要求『不能修改输入』或『并发安全』，就只能退回哈希解法，空间 O(n)。」

**Q2：哈希解法和编织解法分别适合什么场景？**
> 「哈希解法不改输入、线程安全、代码短，并且能直接推广到克隆任意图（133 题）；空间 O(n)。编织解法空间 O(1)，适合内存敏感、n 很大（比如 10⁷）的场景，但要求『允许临时修改输入且能还原』。面试里我一般写编织法，然后主动把哈希法作为对比说出来——因为面试官通常就是在等 O(1) 空间的那个答案。」

**Q3：如果 `random` 可以指向链表外的结点，怎么办？**
> 「那『旧 → 新』的映射域就不止这条链了，编织法失效（链外结点没有 `next` 可以借用）。这时只能退回哈希表，而且要把映射域扩展到所有可能被引用的对象；如果外部对象也需要深拷贝，那就是通用的对象图克隆问题，用 DFS/BFS + 一个全局的 `visited` 映射，像 133 克隆图那样。」

---

## 053.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 133 克隆图](https://leetcode.cn/problems/clone-graph/) | 中等 | 同一套「先建结点再连边」，但图没有"空位"可借 → 必须哈希 |
| [LC 1485 克隆含随机指针的二叉树](https://leetcode.cn/problems/clone-binary-tree-with-random-pointer/) | 中等 | 二叉树版，`random` 指向任意祖先/兄弟，只能哈希 |
| [LC 146 LRU 缓存](https://leetcode.cn/problems/lru-cache/) | 中等 | 不是拷贝，但同样「哈希 + 双向链表」的组合（054） |
| [LC 41 缺失的第一个正数](https://leetcode.cn/problems/first-missing-positive/) | 困难 | 同族思想：用数组下标当哈希桶，O(1) 额外空间 |
| [LC 287 寻找重复数](https://leetcode.cn/problems/find-the-duplicate-number/) | 中等 | 同族思想：把数组本身当链表结构 |
| [LC 206 反转链表](https://leetcode.cn/problems/reverse-linked-list/) | 简单 | 同样是「原地改指针」，但没有映射需求（044） |
| [LC 92 反转链表 II](https://leetcode.cn/problems/reverse-linked-list-ii/) | 中等 | 区间内的原地改造（045） |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 133 克隆图**
1. 图的结点可以直接借用「下一个」位置来存映射吗？为什么不行？
2. 既然不能借用，就必须显式建表。那张表应该在什么时候填、什么时候读，才能避免无限递归？

**LC 1485 克隆含随机指针的二叉树**
1. 二叉树有两个孩子指针，随机指针可能指向任意结点。映射还是必须的吗？
2. 用什么遍历顺序可以保证「访问一个结点时，它的 random 可能还没被创建」这件事被正确处理？

**LC 41 缺失的第一个正数**
1. 值 x 应该被放到哪个下标上？放好之后，怎么从下标反推「谁缺失」？
2. 这个「用下标当桶」和本章「用 next 当映射」的共同点是什么？

**LC 146 LRU（提前预习）**
1. 哈希表和双向链表各自承担什么职责？为什么单用一个不够？

**通用**
- 深拷贝题永远问三句：结点是否新建？指针是否按原结构连？原结构是否被破坏？
- 想省掉哈希表时，先问：现有结构里有没有一个「本来就空闲、可以临时挪用」的指针位？

</details>

---

## 附：本章小结（可背走的三句话）

```text
① 深拷贝 = 先建结点、再连边；中间需要一张 old → new 的映射
② 编织法把映射编进 old.next，于是 random 修正只需一行 old.next.random = old.random.next
③ 拆分必须先恢复原链再前进；否则会走到副本上，整条链错位
```

> **下一章**：[`054-lru-cache.md`](054-lru-cache.md) —— 链表章收口：把「哈希表 O(1) 定位」和「双向链表 O(1) 挪动」缝在一起，手写一个能在 O(1) 里完成 get / put 的缓存。
