# 047. LC21 合并两个有序链表（Merge Two Sorted Lists）

> **状态**：`已完成（初稿）` ｜ **所属**：第 6 部分 · 链表 ｜ **编号**：047 / 150
> **难度**：简单 ｜ **标签**：链表 · 归并 · 哑结点
> **一句话**：这道题真正考的是「**哑结点如何充当答案的容器，把『第一个结点是谁』的特判彻底消掉**」——这是所有「边构造边返回」链表题的原型，不是「两个指针比大小」。
> **前置**：[`044-reverse-linked-list.md`](044-reverse-linked-list.md)（指针三件套与哑结点的第一次登场）

---

## 047.1 题目

**原题链接**：<https://leetcode.cn/problems/merge-two-sorted-lists/>

### 题意重述（把隐含条件补齐）

给你两个**升序**链表的头结点 `list1` 和 `list2`。请把这两个链表**拼接成一个新的升序链表**，新链表由**原来的所有结点**组成，返回新链表的头结点。

四个必须读出来的条件：

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| **两条链各自已经升序** | 「升序链表」 | 若没排序，就得先各自排序再合并（复杂度变 O(n log n)） |
| **必须复用原结点** | 「新链表由拼接给定两个链表的所有节点组成」 | 新建 n 个结点虽然结果对，但空间 O(n)，且被追问「能不能原地」就废 |
| **允许重复值** | 示例里 `1,1,2,3,4,4` | 相等时不能丢任何一个，`<` 和 `<=` 的选择决定**稳定性** |
| **任一链表可以为空** | `0 <= 链表长度` | 双空、单空都要能返回正确结果 |

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| `0 <= list1.length, list2.length <= 50` | 极度宽松 | **规模完全不是考点**；考点是「指针怎么接」 |
| `-100 <= Node.val <= 100` | 值域小、可重复 | 不能假设值唯一；也不能用值当「结束标记」 |
| 两条链都升序 | 关键前提 | 只需一次线性扫描，O(m + n) |
| 空间期望 O(1) | 面试追问 | 迭代版满足；递归版是 O(m + n) 栈 |

> 💡 **量级 → 算法**：`n ≤ 50` 意味着「哪怕你 O(n²) 也能过」。所以链表题从来不是用规模筛人。**这道题真正的考点是：你能不能在不用任何特判的情况下，把『第一个结点』这件事处理掉。** 这就是哑结点的全部意义。

### 示例

```text
输入：list1 = [1,2,4], list2 = [1,3,4]
输出：          [1,1,2,3,4,4]

输入：list1 = [], list2 = [0]
输出：          [0]

输入：list1 = [], list2 = []
输出：          []
```

### 我自造的六个反例（写完必须逐个跑）

```text
① [],[1,2]          → 单边为空：直接返回另一条，不要进循环
② [],[ ]            → 双边为空：返回 null
③ [2,2],[2,2]       → 全相等：验证 <= 不会丢结点（结果 4 个 2）
④ [1],[2]           → 各 1 个：最短的「真的比较一次」的输入
⑤ [1,3,5],[2,4,6]   → 完美交错：最考验「轮换挑结点」
⑥ [1,2,3],[]        → 左非空右空：最容易在「剩余段整段接上」那步写错
```

第 ③ 条最关键：很多人写 `if (a.val < b.val) ... else if (a.val > b.val) ...`，两个分支都不进，`tail` 不前进 → **死循环**。用 `<=` 或双分支 `if/else` 才安全。

---

## 047.2 从暴力到最优：第一个念头为什么错

### 第一步：暴力（收集所有值 → 排序 → 重建链表）

```text
vals = []
for p in list1: vals.append(p.val)
for p in list2: vals.append(p.val)
sort(vals)
用 vals 重建一条链
```

- 时间 O((m+n) log(m+n))，空间 **O(m+n)**。
- 它抛开了「两条链**已经有序**」这个最贵的额外信息，把 O(m+n) 的活做成了 O(n log n)。
- 而且它新建了结点，不满足「复用原结点」。

### 第二步：为什么它不够 —— 把「已有信息」浪费掉了

`m = n = 50` 时，排序版大约 `100·log₂100 ≈ 664` 次比较；而双指针归并只需最多 `m + n - 1 = 99` 次比较。**差了近 7 倍，而且随规模放大差距会继续加大**（1e5 规模时差 17 倍以上）。

更要紧的是**结构**：合并两条有序链不需要「重新排」，只需要「轮流挑小的」。这是一个 **归并（merge）**，不是**排序（sort）**。把 merge 写成 sort，说明没读出题目的那半句「升序」。

### 第三步：卡点在哪

真正写下代码时，90% 的人会在同一个地方卡住：

> **「新链表的第一个结点，到底是 `list1` 的还是 `list2` 的？」**

如果你想「先比一次，确定头是谁」，代码会立刻长出一堆特判：

```text
// 特判版（能过，但很难看）
if list1 == null: return list2
if list2 == null: return list1
if list1.val <= list2.val:
    head = list1; list1 = list1.next
else:
    head = list2; list2 = list2.next
tail = head
while list1 != null and list2 != null:
    ...   // 挑结点接到 tail 后面
```

`head` 这一段「先挑一次」的逻辑，和后面的循环体**几乎重复**，而且多出了「`tail` 初值处理」「空链早退」三处特判。**这就是卡点：不是算法不会，而是「第一个结点」没有统一的落脚点。**

### 第四步：灵感的来源 —— 哑结点当「答案容器」

造一个**假结点** `dummy`，让它的 `next` 指向「真正的答案头」。于是：

```
dummy.next = 最终答案的头（一开始是 null，随着挑结点被填上）
tail = dummy   ← 所有结点都往 tail 后面挂
```

这样做的三个红利，全都来自「**"挂第一个结点"和"挂第 100 个结点"走同一条语句**」：

| 红利 | 没有 dummy 时 | 有 dummy 时 |
| --- | --- | --- |
| 第一个结点 | 必须特判「head 还没定」 | `tail.next = 挑中的结点`，天然成立 |
| 「答案头」的返回 | 用 `head` 变量，容易忘更新 | `return dummy.next`，永远正确 |
| 空链早退 | 必须 `if (l1 == null) return l2;` | 循环一次都不跑，`dummy.next` 仍是 `null`，返回 `null` 也对 |

> **哑结点的本质**：它把「空答案」和「非空答案」变成**同一种情况**——答案永远可以从 `dummy.next` 读出来。这就是 047 要带走的第一件事，也是 054（LRU 的哨兵）复用同一思想的地方。

### 另一条路：递归

```text
merge(a, b):
    if a == null: return b
    if b == null: return a
    if a.val <= b.val:
        a.next = merge(a.next, b)
        return a
    else:
        b.next = merge(a, b.next)
        return b
```

它把「谁当新头」直接写成了返回值，不需要 dummy，也不需要 tail。**代码只有 6 行，比迭代版还短。** 但空间是 O(m+n) 的递归栈，`m = n = 5·10⁴` 时会栈溢出。所以：

- **面试写迭代**（O(1) 空间）。
- **递归要会**，因为它是 023（合并 K 个）、052（排序链表）递归版的直接基础，而且能一句话说清「归并 = 挑一个头 + 归并剩下的」。

---

## 047.3 原理拆解

### 047.3.1 数据结构与算法选型

| 方案 | 时间 | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- |
| 收集值 → 排序 → 重建 | O(n log n) | O(n) | 15 行 | 浪费「已有序」信息；不复用结点 |
| 特判头结点 + while | O(m+n) | O(1) | 20 行 | 特判多，易漏「单边为空」 |
| **哑结点 + 尾插（迭代）** ✅ | **O(m+n)** | **O(1)** | **12 行** | 几乎不会输；要注意剩余段整段接上 |
| 递归 | O(m+n) | **O(m+n) 栈** | 6 行 | 长链栈溢出 |
| 优先队列（k 路归并退化） | O(n log 2) | O(1) | 20 行 | 只有 2 路时纯属小题大做，常数更大 |

**为什么「哑结点 + 尾插」是唯一推荐**：12 行、无特判、O(1) 空间，三个维度全优。

### 047.3.2 为什么这个做法是对的（正确性论证）

**循环不变量**：

> ① `dummy.next` 起、到 `tail` 止的那段链，是「已经从 `list1` / `list2` 中取出的所有结点」按升序排好的结果；
> ② `tail` 是这段链的最后一个结点；
> ③ `list1`、`list2` 分别指向各自链中**尚未取出**的第一个结点，且两条剩余链各自仍升序；
> ④ 已取出段的最后一个值 ≤ 两条剩余链的首值（这一条保证了「追加」不会破坏有序）。

**初始化**：已取出段为空（`tail = dummy`，`dummy.next = null`），③ 成立，④ 因为段空而空洞成立。

**保持**：设 `a = list1`、`b = list2`，都不为空。若 `a.val <= b.val`，取 `a`：

- 由 ④，段尾值 ≤ `a.val`，所以把 `a` 追加到 `tail` 后面不破坏升序 → ① 保持，`tail = a`。
- `a` 前进一格，`list1` 剩余部分仍升序 → ③ 保持。
- 新的段尾是 `a`，而 `a.val <= b.val` 且 `a.val <= a.next.val`（原链升序），所以 ④ 保持。

`b.val < a.val` 时对称。所以每次循环都保持不变量。

**终止**：循环在 `list1 == null` 或 `list2 == null` 时结束。此时一条链已空，另一条链剩余部分**天然升序**，且由 ④，它的首值 ≥ 段尾值，所以直接把整段挂到 `tail` 后面即可。∎

**为什么不丢结点**：每次循环恰好从两条链之一取走一个结点，取走的结点立即进入已取出段；循环结束后剩余整段挂上。总数 = `m + n`，与输入一致。∎

**稳定性**：`a.val == b.val` 时取 `a`（因为条件写的是 `<=`）。于是两条链中相等的结点，`list1` 的排在 `list2` 的之前，与「输入中 `list1` 在前」的相对次序一致 —— 这就是**稳定归并**。若写成 `<`，则相等时取 `b`，稳定性反转。对本题结果无影响（值相同），但 052 排序链表若不稳，可能在某些「结点带卫星数据」的场景出错。

### 047.3.3 通用解题思路（可迁移的那一层）

**链表章的统一语言：哑结点 + 指针三件套（prev/cur/next）+ 先存后断。** 047 展示的是这套语言里**哑结点的第二种用法**：

```
044/045/046：dummy 用来「消掉头结点可能被改」的特判
047：        dummy 用来「当答案的容器」，消掉「第一个结点是谁」的特判
054：        dummy（哨兵）用来「消掉链表为空 / 只有一个结点」的特判
```

同一个 dummy，三种身份，一条思想：**用一个「永远存在」的假结点，把边界情况变成普通情况。**

而本题的「指针三件套」变形为 **`tail` / `list1` / `list2`**：

| 角色 | 传统三件套 | 047 里的对应物 | 作用 |
| --- | --- | --- | --- |
| 已处理部分的入口 | `prev` | `dummy`（固定不动） | 答案从 `dummy.next` 读 |
| 已处理部分的出口 | `cur` | `tail`（每轮右移） | 新结点挂这里 |
| 未处理部分的入口 | `nxt` | `list1` / `list2` | 「先存后断」的存：先记住两条链的当前位置，再改 `tail.next` |

注意最后一行的对应关系：**`tail.next = list1` 之前，`list1` 已经是「下一站的地址」了**，所以这里不存在丢链风险 —— 这是 047 比 044 简单的地方：**它的每一步都是「读当前结点 + 前进」，没有「原地掰箭头」。**

属于同一模型的题：

| 题 | 归并什么 | 多了什么 |
| --- | --- | --- |
| [LC 88 合并两个有序数组](https://leetcode.cn/problems/merge-sorted-array/) | 数组 | 从后往前填，避免覆盖 |
| [LC 23 合并 K 个升序链表](https://leetcode.cn/problems/merge-k-sorted-lists/) | K 条链 | 堆 / 分治 |
| [LC 148 排序链表](https://leetcode.cn/problems/sort-list/) | 自己的一半与自己的一半 | 先找中点切分（052） |
| [LC 143 重排链表](https://leetcode.cn/problems/reorder-list/) | 前半与反转后的后半 | 是「交替合并」不是「有序合并」（051） |
| [LC 2 两数相加](https://leetcode.cn/problems/add-two-numbers/) | 两条链逐位相加 | 多了进位状态（048） |
| [LC 86 分隔链表](https://leetcode.cn/problems/partition-list/) | 按值分成两段再接 | 用**两个** dummy 分别收集（见 3.4） |

### 047.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| **降序**合并 | 比较方向反转 | 把 `<=` 改成 `>=`，其余不动 |
| 合并后**去重** | 相等只留一个 | 相等时跳过一个：`if (tail.val == 选中值) 跳过`；注意 `dummy` 的 `val` 是假值，要特判第一个 |
| **不能改原链表** | 必须深拷贝 | 每个结点 `new` 一份再挂；空间 O(m+n) |
| **K 条链** | 两两合并升级 | 堆（O(N log K)）或分治两两合并（O(N log K)，常数更小）→ 023 |
| 交替合并（不是按大小） | 语义变了 | 见 051 重排链表：`p.next = q; q.next = nextP` |
| **数据流**输入 | 边来边合并 | 双指针天然适配：哪边先到就取哪边 |
| 要求**返回合并后的长度 / 中位数** | 不需要构造链 | 只需双指针计数，O(1) 空间不建链 |

---

## 047.4 图解推演

### 047.4.1 执行轨迹（`[1,2,4]` + `[1,3,4]`）

```text
初始：
  dummy   tail
    ↓      ↓
   [∅] → ∅

  list1: 1 ──→ 2 ──→ 4 ──→ ∅
          ↑
  list2: 1 ──→ 3 ──→ 4 ──→ ∅
          ↑

第 1 轮：1 <= 1 → 取 list1 的 1（稳定性：相等取左边）
   [∅] → [1]      tail 前进
          ↑
        tail
  list1 前进到 2

第 2 轮：2 <= 1 ? 否 → 取 list2 的 1
   [∅] → [1] → [1]
                ↑
              tail

第 3 轮：2 <= 3 → 取 list1 的 2
   [∅] → [1] → [1] → [2]

第 4 轮：4 <= 3 ? 否 → 取 list2 的 3
   [∅] → [1] → [1] → [2] → [3]

第 5 轮：4 <= 4 → 取 list1 的 4
   [∅] → [1] → [1] → [2] → [3] → [4]

第 6 轮：list1 已空，list2 剩 [4]
   → 整段挂上：tail.next = list2
   [∅] → [1] → [1] → [2] → [3] → [4] → [4] → ∅

返回 dummy.next = 第一个 1
```

### 047.4.2 每一轮的状态（行 = 轮次，列 = 变量）

| 轮 | `list1.val` | `list2.val` | 取谁 | `tail.val`（取后） | 已合并段 |
| --- | --- | --- | --- | --- | --- |
| 初始 | 1 | 1 | — | — | 空 |
| 1 | 1 | 1 | list1（`<=`） | 1 | `1` |
| 2 | 2 | 1 | list2 | 1 | `1→1` |
| 3 | 2 | 3 | list1 | 2 | `1→1→2` |
| 4 | 4 | 3 | list2 | 3 | `1→1→2→3` |
| 5 | 4 | 4 | list1（`<=`） | 4 | `1→1→2→3→4` |
| 6 | ∅ | 4 | 整段接上 | 4 | `1→1→2→3→4→4` |

### 047.4.3 有 dummy vs 无 dummy（两条路线对比）

```text
        ┌──────────── 无 dummy ────────────┐        ┌──────── 有 dummy ────────┐
        │                                  │        │                          │
        │  特判 l1/l2 为空（2 处）           │        │  无任何前置特判            │
        │  特判"头是谁"（1 处 if/else）      │        │  循环体一行搞定第一个结点    │
        │  循环体再写一遍"挑结点"（重复）     │        │  返回 dummy.next           │
        │                                  │        │                          │
        │  约 20 行，3 个特判分支            │        │  约 12 行，0 个特判        │
        └──────────────────────────────────┘        └──────────────────────────┘

                        ↑ 但注意：dummy 多分配了 1 个结点，只影响"空间常数 +1"
```

> **记住这个对比**：它解释了 047 / 023 / 086 这些「边构造边返回」的题为什么几乎人人都用 dummy —— 不是风格偏好，而是**用 1 个结点的代价换掉 3 个特判分支**。

---

## 047.5 双语言实现

### 047.5.1 Java

```java
class ListNode {
    int val;
    ListNode next;

    ListNode(int val) {
        this.val = val;
    }
}

class Solution {
    public ListNode mergeTwoLists(ListNode l1, ListNode l2) {
        ListNode dummy = new ListNode(0);
        ListNode tail = dummy;
        while (l1 != null && l2 != null) {
            if (l1.val <= l2.val) { // <= 而不是 <：相等时先取 l1，保持稳定
                tail.next = l1;
                l1 = l1.next;
            } else {
                tail.next = l2;
                l2 = l2.next;
            }
            tail = tail.next;
        }
        // 一方走完后，另一方剩余部分天然有序，整段接上
        tail.next = (l1 != null) ? l1 : l2;
        return dummy.next;
    }

    // 递归版：谁小谁当新头，剩余部分交给子问题
    public ListNode mergeTwoListsRec(ListNode l1, ListNode l2) {
        if (l1 == null) {
            return l2;
        }
        if (l2 == null) {
            return l1;
        }
        if (l1.val <= l2.val) {
            l1.next = mergeTwoListsRec(l1.next, l2);
            return l1;
        }
        l2.next = mergeTwoListsRec(l1, l2.next);
        return l2;
    }
}
```

**四处「为什么」**：

1. `tail = dummy` 而不是 `tail = null`：所有结点都往 `tail.next` 挂，`dummy` 让「挂第一个」不需要特判。
2. `tail.next = (l1 != null) ? l1 : l2`：不用 `while` 逐个搬 —— 剩余部分是**一整条已经有序的链**，直接接上去，O(1) 完成。
3. `<=` 而不是 `<`：相等时取 `l1`，保证稳定性；同时避免「两值相等时两个分支都不进」的死循环风险（用 `if/else` 已经规避，但 `<=` 让语义更明确）。
4. `return dummy.next` 而不是 `return head`：本题根本没有 `head` 变量 —— 这就是 dummy 的红利。

### 047.5.2 Go

```go
package main

// ListNode 是链表结点的统一定义（LeetCode 判题环境已提供，本地自建）
type ListNode struct {
	Val  int
	Next *ListNode
}

// 迭代：哑结点 + 尾插
func mergeTwoLists(l1 *ListNode, l2 *ListNode) *ListNode {
	dummy := &ListNode{}
	tail := dummy
	for l1 != nil && l2 != nil {
		if l1.Val <= l2.Val { // <= 保证「稳定性」：相等时先取 l1
			tail.Next = l1
			l1 = l1.Next
		} else {
			tail.Next = l2
			l2 = l2.Next
		}
		tail = tail.Next
	}
	if l1 != nil {
		tail.Next = l1 // 剩下的整段接上，不用逐个搬
	} else {
		tail.Next = l2
	}
	return dummy.Next
}

// 递归版：谁小谁当新头，剩下的递归合并
func mergeTwoListsRec(l1 *ListNode, l2 *ListNode) *ListNode {
	if l1 == nil {
		return l2
	}
	if l2 == nil {
		return l1
	}
	if l1.Val <= l2.Val {
		l1.Next = mergeTwoListsRec(l1.Next, l2)
		return l1
	}
	l2.Next = mergeTwoListsRec(l1, l2.Next)
	return l2
}
```

### 047.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 三元表达式 | `(l1 != null) ? l1 : l2` | **没有三元运算符** | Go 必须写成 `if/else` 两个分支；**别试图用 `map[bool]*ListNode{true: l1, false: l2}[l1 != nil]` 这种把戏**，可读性极差 |
| 空指针 | `null` | `nil` | 判断写法不同，语义一致 |
| 哑结点构造 | `new ListNode(0)` 需要构造器 | `&ListNode{}` 零值即可 | Go 少一个构造器，`Val` 默认 0 且本题不读 |
| 返回值 | 必须声明类型 | 必须声明类型 | 一致 |
| 递归深度 | 默认栈约 1 MB，约 1e4 层 | goroutine 栈可动态增长 | `m + n = 10⁵` 时 Java 崩、Go 可能活，但都不推荐 |
| 计数 / 判空 | `while (l1 != null && l2 != null)` | `for l1 != nil && l2 != nil` | Go 的 `for` 兼作 while，注意别写成 `for ; ;` |

### 047.5.4 对拍测试（Go，2 万次随机 + 六个边界）

```go
package main

import (
	"math/rand"
	"sort"
	"testing"
)

func build(a []int) *ListNode {
	dummy := &ListNode{}
	t := dummy
	for _, v := range a {
		t.Next = &ListNode{Val: v}
		t = t.Next
	}
	return dummy.Next
}

func dump(h *ListNode) []int {
	out := []int{}
	for p := h; p != nil; p = p.Next {
		out = append(out, p.Val)
		if len(out) > 100000 {
			panic("疑似成环")
		}
	}
	return out
}

func TestMergeTwoListsRandom(t *testing.T) {
	r := rand.New(rand.NewSource(47))
	for iter := 0; iter < 20000; iter++ {
		n1, n2 := r.Intn(8), r.Intn(8) // 含 0：单边 / 双边为空
		a := make([]int, n1)
		b := make([]int, n2)
		for i := range a {
			a[i] = r.Intn(6) - 1 // 值域 [-1, 4]，含负数与大量重复
		}
		for i := range b {
			b[i] = r.Intn(6) - 1
		}
		sort.Ints(a) // 输入必须升序，这是题目前提
		sort.Ints(b)

		want := append(append([]int{}, a...), b...)
		sort.Ints(want) // 参考解：合并后排序（sort.Ints 就是朴素实现）

		got := dump(mergeTwoLists(build(a), build(b)))
		if len(got) != len(want) {
			t.Fatalf("iter=%d a=%v b=%v got=%v want=%v", iter, a, b, got, want)
		}
		for i := range got {
			if got[i] != want[i] {
				t.Fatalf("iter=%d a=%v b=%v got=%v want=%v", iter, a, b, got, want)
			}
		}
		// 同一批数据跑递归版
		got2 := dump(mergeTwoListsRec(build(a), build(b)))
		if len(got2) != len(want) {
			t.Fatalf("rec iter=%d a=%v b=%v got=%v want=%v", iter, a, b, got2, want)
		}
		for i := range got2 {
			if got2[i] != want[i] {
				t.Fatalf("rec iter=%d a=%v b=%v got=%v want=%v", iter, a, b, got2, want)
			}
		}
	}
	// 边界逐个核对
	cases := []struct{ a, b, want []int }{
		{nil, nil, nil},                                   // ① 双边空
		{nil, []int{1, 2}, []int{1, 2}},                   // ② 单边空
		{[]int{1, 2}, nil, []int{1, 2}},                   // ③ 另一边空
		{[]int{2, 2}, []int{2, 2}, []int{2, 2, 2, 2}},     // ④ 全相等，不能丢结点
		{[]int{1}, []int{2}, []int{1, 2}},                 // ⑤ 各一个
		{[]int{1, 3, 5}, []int{2, 4, 6}, []int{1, 2, 3, 4, 5, 6}}, // ⑥ 完全交错
	}
	for i, c := range cases {
		got := dump(mergeTwoLists(build(c.a), build(c.b)))
		if len(got) != len(c.want) {
			t.Fatalf("边界 %d: got=%v want=%v", i+1, got, c.want)
		}
		for j := range got {
			if got[j] != c.want[j] {
				t.Fatalf("边界 %d: got=%v want=%v", i+1, got, c.want)
			}
		}
	}
}
```

> **对拍要点**：参考实现用 `sort.Ints` 是有效的，因为「合并两条有序链 = 把元素排好序」——**结果唯一，不依赖稳定性**。如果题目要求稳定（结点带卫星数据），就不能用 `sort.Ints` 对拍，必须手写稳定归并的参考实现。这是「参考实现的选型也要贴合题意」的例子。

---

## 047.6 复杂度与常见坑

### 047.6.1 复杂度推导

- **时间 O(m + n)**：每轮循环从两条链之一取走一个结点，`tail` 前进一格。最坏情况（完全交错）循环执行 `m + n - 1` 次，每次常数操作。剩余段整段挂上是 O(1)。所以总操作数 ≤ `3(m+n)`。
- **空间 O(1)**（迭代）：`dummy` / `tail` / `l1` / `l2` 四个指针；`dummy` 是唯一新分配的结点。**没有新建任何承载数据的结点**，全部复用原结点。
- **空间 O(m + n)**（递归）：递归深度 = 合并的结点数。
- **下界**：任何正确算法必须至少检查 `min(m, n)` 个结点（决定交错顺序），所以 Ω(m + n) 是紧的 —— **本题达到最优**。

### 047.6.2 八个真实踩过的坑

1. **`while` 里忘了 `tail = tail.next`** —— 所有结点都被挂到同一个位置，结果只剩最后一个。这是最高频错误。
2. **`if/else if` 只覆盖 `<` 和 `>`** —— 相等时两个分支都不进，`tail` 不动，**死循环**。用 `if/else`（一正一反）或 `<=`。
3. **剩余段用 `while` 逐个搬** —— 结果正确但慢（常数 3 倍），且多一处可能写错的循环。整段接上是 O(1) 的。
4. **忘了返回 `dummy.next` 而是 `dummy`** —— 结果多一个值为 0 的头结点。
5. **`return head`** —— 本题没有 `head`，若自己加了 `head` 变量又忘了在「第一轮」赋值，返回 `null`。
6. **`l1 = l1.next` 写在 `tail.next = l1` 之前** —— 顺序反了会丢掉当前结点（挂的是下一个）。这是「先存后断」在本题的对应形态：**先挂，再前进**。
7. **递归版没写 `if (l2 == null) return l1;`** —— 只判了一边，另一边为空时 NPE。
8. **认为「值相同要去重」** —— 题目要求保留**所有**结点，`[2,2] + [2,2]` 必须是四个 2。

### 047.6.3 面试官最爱追问的三个问题

**Q1：为什么要用哑结点？不加会怎样？**
> 「不加的话，『新链表的第一个结点是谁』必须单独判断一次，而这段判断逻辑和循环体几乎重复；同时空链还要单独早退。加哑结点后，所有结点（包括第一个）都走同一条 `tail.next = 挑中的结点`，返回时统一 `return dummy.next`，空链也自动得到 `null`。代价只是多分配一个不承载数据的结点，空间仍是 O(1)。」

**Q2：递归版看起来更短，为什么不推荐？**
> 「因为空间。递归深度等于合并后的长度，`m + n = 10⁵` 时 Java 默认栈（约 1 MB，约 1 万层）会 StackOverflowError；迭代版是 O(1)。递归的价值在于它把归并的本质写得最清楚——『挑一个较小的当新头，剩下的交给子问题』——这个结构正是 23 题（K 路归并）和 148 题（排序链表）递归版的基础，所以我两个都会写，但生产代码用迭代。」

**Q3：如果两条链长度差很多（比如 1 和 10⁶），有优化空间吗？**
> 「时间上仍是 O(m + n)，因为必须逐个挑；但『剩余段整段接上』这一步已经把常数压到最低——短链走完后剩下的 10⁶ 个结点是 O(1) 接上的，不是逐个搬。空间也是 O(1)。真正值得做的优化是：如果允许改结构，可以把长链的结点做二分查找定位插入点（O(m log n)），但那要求结点能随机访问，链表不满足。」

---

## 047.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 88 合并两个有序数组](https://leetcode.cn/problems/merge-sorted-array/) | 简单 | 数组版，**必须从后往前**填，否则覆盖未处理元素 |
| [LC 23 合并 K 个升序链表](https://leetcode.cn/problems/merge-k-sorted-lists/) | 困难 | 堆（O(N log K)）或分治两两合并 |
| [LC 148 排序链表](https://leetcode.cn/problems/sort-list/) | 中等 | 自己与自己归并：先找中点切分（052） |
| [LC 21 的变体：合并后去重](https://leetcode.cn/problems/merge-two-sorted-lists/) | — | 相等时跳过，注意 dummy 的假值不能参与比较 |
| [LC 86 分隔链表](https://leetcode.cn/problems/partition-list/) | 中等 | 用**两个** dummy 分别收集小于/不小于 x 的结点 |
| [LC 2 两数相加](https://leetcode.cn/problems/add-two-numbers/) | 中等 | 两条链逐位相加 + 进位状态（048） |
| [LC 143 重排链表](https://leetcode.cn/problems/reorder-list/) | 中等 | 交替合并（不是按大小挑）（051） |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 88 合并两个有序数组**
1. 如果从前往后合并，`nums1` 里还没处理的元素会被写进去的新值覆盖。换个方向呢？
2. 从后往前时，谁先被填满就说明剩下的可以整段复制了吗？

**LC 23 合并 K 个升序链表**
1. 每次都要从 K 个头里挑最小的。K 很大时，线性挑和用堆挑差多少？
2. 如果改用「两两合并」，一共要合并多少轮？每轮总共处理多少个结点？

**LC 86 分隔链表**
1. 你要维护几条独立的链？每条链的「尾巴」在哪里？
2. 拼起来的时候，最后一条链的尾巴有没有可能还指着别的结点？需要断吗？

**通用**
- 只要题目是「边遍历边构造结果链」，先问：能不能用一个 dummy 当容器，让「第一个」和「第 n 个」走同一条语句？
- 写完「剩余段处理」那一步，一定检查：是整段接上（O(1)），还是逐个搬（O(k)）？前者更好。

</details>

---

## 附：本章小结（可背走的三句话）

```text
① 归并 = 轮流挑较小的那个挂到 tail 后面；哑结点让「第一个结点」不再是特例
② 一方走完 → 另一方剩余段整段接上，O(1)，不要 while 逐个搬
③ 相等时用 <=，既保稳定也避免「两分支都不进」的死循环
```

> **下一章**：[`048-add-two-numbers.md`](048-add-two-numbers.md) —— 同样是「同步走两条链 + 尾插」，但多了「进位」这个跨轮状态，且要处理长度不等与最后额外进位。
