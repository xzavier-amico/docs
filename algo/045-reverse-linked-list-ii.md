# 045. LC92 反转链表 II（Reverse Linked List II）

> **状态**：`已完成（初稿）` ｜ **所属**：第 6 部分 · 链表 ｜ **编号**：045 / 150
> **难度**：中等 ｜ **标签**：链表 · 指针 · 哑结点 · 局部反转
> **一句话**：这道题真正考的是「**把 044 的反转模板套到一个区间上之后，怎么保证区间外的两条边还能接回去**」，不是「再写一次三指针」。
> **前置**：[`044-reverse-linked-list.md`](044-reverse-linked-list.md)（三指针模板与「先存后断」在那里）

---

## 045.1 题目

**原题链接**：<https://leetcode.cn/problems/reverse-linked-list-ii/>

### 题意重述（把隐含条件补齐）

给你单链表的头结点 `head` 和两个整数 `left`、`right`，其中 `1 <= left <= right <= n`。请把**从第 `left` 个结点到第 `right` 个结点**这一段反转，其余部分保持原样，返回反转后的头结点。

题干只有一句，但藏着五个必须读出来的条件：

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| **下标从 1 开始** | `1 <= left` | 按 0 起算会整体偏移一位；`left = 1` 时 `prev` 会退化成 `null` |
| **`left <= right`** | 约束 | 不需要处理「空区间」，少一个分支（但仍要处理 `left == right` 的「不反转」） |
| **只反转一段** | 「区间」 | 反转完必须把「前段尾巴」和「后段头」重新接上，漏一条边就断链 |
| **头结点可能变** | `left` 可以是 1 | 一旦头变，就必须用哑结点，否则要单独写一段「新头是谁」的逻辑 |
| **`n` 可达 500** | 数据范围 | 规模很小，**评分点是边界与接缝，不是复杂度** |

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| `1 <= n <= 500` | 极小 | 任何 O(n) 都过；O(n²) 都能过（500² = 25 万），所以「一次遍历」是加分项而非必需 |
| `-500 <= Node.val <= 500` | 值域小、可重复 | 不能靠值定位区间，只能靠**计数** |
| `1 <= left <= right <= n` | 区间合法 | 不用处理越界，但 `left == 1` 与 `right == n` 是必须单独跑的两个边界 |
| 要求**一次遍历** | 进阶追问 | 头插法天然一次遍历；「先切断、再反转、再接回」需要先定位，也是两次遍历 |

> 💡 **量级 → 算法**：`n ≤ 500` 是典型的「**出题人不想让你纠结复杂度**」。链表区间题的规模永远给得宽松，因为它考的是「指针有没有接错」。看到 500 就该明白：**这题的分数全在接缝上。**

### 示例

```text
输入：head = [1,2,3,4,5], left = 2, right = 4
输出：      [1,4,3,2,5]
解释：区间 [2,4] 是 2→3→4，反转成 4→3→2，前段 1 和后段 5 保持不动

输入：head = [5], left = 1, right = 1
输出：      [5]
```

### 我自造的六个反例（写完必须逐个跑）

```text
① [1,2,3], left=1, right=3   → 整条反转：头结点变了，dummy 是唯一解
② [1,2,3], left=1, right=1   → 区间只有一个：一条边都不该动
③ [1,2,3], left=2, right=2   → 同上，但 prev 不是 dummy，验证「定位偏移」
④ [1,2],   left=1, right=2   → 最短的「头尾同时被反转」输入
⑤ [1,2,3], left=2, right=3   → 反转区间贴到表尾：接缝只剩前段一条边
⑥ [3,3,3], left=1, right=3   → 全相等值：验证实现没有偷偷用值做判断
```

第 ① 条是关键：`left = 1` 时「区间前驱」不存在。**这就是哑结点存在的全部理由**——给「不存在的前驱」一个统一的落脚点，让后面的循环不必区分「left 是不是 1」。

---

## 045.2 从暴力到最优：第一个念头为什么错

### 第一步：暴力（把值抄出来，反转后再写回）

```text
vals = 链表所有值
把 vals[left-1 .. right-1] 这一段原地反转
再把 vals 依次写回各结点的 val
```

- 时间 O(n)，空间 **O(n)**。
- 它能过题，但**没有改任何指针**。面试官一句「结点里还有别的字段」或「要求原地」就废。

### 第二步：卡点在哪

很多人以为这题是「044 加个计数器」，于是写出：

```text
// 想当然版
prev = null; cur = head;
for i = 1 .. right:
    nxt = cur.next
    if i >= left: cur.next = prev      // 只在区间内掰箭头
    prev = cur
    cur = nxt
```

这段代码在 `left > 1` 时**一定会断链**，因为它把区间第一个结点的 `next` 改成了 `null`（`prev` 在进入区间那一刻是区间外的前一个结点吗？不是——`prev` 一直在跟着 `cur` 走，进入区间时它正好是区间前驱，看起来没问题）。真正的麻烦在**区间之后**：

```
反转完 [2,4] 后，如果不做任何处理：
    1 ───→ 4 ───→ 3 ───→ 2 ───→ ∅      ✗ 5 丢了
                         ↑
                    2.next 被改成了 ∅（或指向 1）

正确结果：
    1 ───→ 4 ───→ 3 ───→ 2 ───→ 5 ───→ ∅
```

也就是说：**区间内的箭头好办，区间两端的两条边才是真正的活**。

- 前段尾巴（`1`）原本指向区间头（`2`），反转后必须指向区间尾（现在是 `4`）。
- 区间尾（原本是 `2`）反转后变成区间头，它的 `next` 必须是后段头（`5`）。
- 而区间**原来的第一个结点**（`2`）反转后正好是区间**尾巴**，它就是「接后段」的那个结点 —— 记住这一点，后面所有写法都围绕它展开。

### 第三步：灵感的来源 —— 换个动作：不掰整段的箭头，改成「一个个往前面插」

044 是「把箭头全部掉头」。区间反转还有第二种等价写法，叫**头插法**：

> 把 `prev` 固定在**区间前驱**不动，然后反复把 `cur` 的**下一个结点**摘下来，插到 `prev` 后面。

```
初始：  prev → [2] → [3] → [4] → 5
               ↑
              cur（区间第一个，永远不动）

第 1 次：把 3 摘下，插到 prev 后面
        prev → [3] → [2] → [4] → 5

第 2 次：把 4 摘下，插到 prev 后面
        prev → [4] → [3] → [2] → 5      ✅ 收工
```

这个视角的好处是：**「区间前驱」和「区间第一结点」这两个位置从头到尾都没变**，所以两条接缝天然是对的：

- `prev.next` 每轮都被更新成「新的区间头」，最后自然指向区间尾（原区间头）——这正是「前段尾巴接区间头」。
- `cur` 从没离开原位，它最后就是区间尾，而它的 `next` 在「摘结点」时被自动改成后段头 —— 这正是「区间尾接后段头」。

**这就是本题的灵感**：不要去「反转区间」，要去「**往区间前面插结点**」。同一个结果，两种视角，后者的接缝是免费的。

### 另一条路：「切断 → 反转 → 接回」

先把区间整段切下来（记下 `before` = 区间前驱，`after` = 区间后一个），对这段调用 044 的 `reverse`，再把 `before → 新头`、`新尾 → after` 接回去。

- 优点：完全复用 044，思路最直白。
- 缺点：要记的指针更多（`before`、`after`、`segHead`、`segTail`），且 `left == 1` 时 `before` 为 `null`，必须额外特判或上哑结点。
- 结论：**能写，但头插法少两个变量、少一个特判**，本题推荐头插法。

---

## 045.3 原理拆解

### 045.3.1 数据结构与算法选型

| 方案 | 时间 | 空间 | 要维护的变量 | 什么时候会输 |
| --- | --- | --- | --- | --- |
| 抄值 → 反转数组 → 写回 | O(n) | O(n) | 3 | 要求原地 / 结点带其它字段 |
| 切断 → 044 反转 → 接回 | O(n) | O(1) | 6（含 2 个临时） | `left == 1` 需特判；接缝多，易漏 |
| **头插法（哑结点）** ✅ | **O(n)** | **O(1)** | **4** | 几乎不会输；心算 `right - left` 次数容易错 |
| 递归（先递归到 left，再局部反转） | O(n) | O(n) 栈 | 少 | n 大时栈溢出；`left` 靠栈展开回退，可读性差 |
| 用一个栈弹栈重排 | O(n) | O(n) | 少 | 纯炫技 |

**为什么头插法最优**：变量最少、无特判、一次遍历、O(1) 空间。四个指标全满。

### 045.3.2 为什么这个做法是对的（正确性论证）

**循环不变量**（第 k 次循环前，k 从 0 开始）：

> ① `prev` 始终是**区间前驱**（`left == 1` 时是 `dummy`），全程不动；
> ② `prev.next` 指向「**已经搬到前面的那部分**」的头；
> ③ `cur` 始终是**原区间的第一个结点**，也就是「已搬部分」的尾巴；
> ④ 从 `cur.next` 到 `right` 这一段，相对顺序与原链表一致，且尚未被改动。

**初始化**（k = 0）：`prev` 走 `left-1` 步后停在区间前驱；`cur = prev.next` 即原区间第一个结点；此时「已搬部分」为空。①②③④ 均成立。

**保持**：设 `nxt = cur.next`（满足 ④ 的第一个未处理结点）。

1. `cur.next = nxt.next`：把 `nxt` 从原位摘掉。此时「未处理段」变成 `nxt.next` 起的部分，仍满足 ④。
2. `nxt.next = prev.next`：让 `nxt` 指向当前已搬部分的头。
3. `prev.next = nxt`：`nxt` 成为已搬部分的新头。于是已搬部分 = `nxt → (原来的已搬部分)`，长度 +1，`cur` 仍是它的尾巴（因为 `cur` 没动，且新加结点在它前面）。①②③ 保持。

**终止**：循环执行 `right - left` 次后，已搬部分长度 = `right - left + 1` = 区间长度，即整个区间都被搬到了 `prev` 后面，且顺序是原区间的**逆序**。

- `prev.next` = 区间新头 = 原第 `right` 个结点 → 前段尾巴正确接上。✅
- `cur` = 原第 `left` 个结点 = 区间新尾；它的 `next` 在第 1 步里已经被改成 `nxt.next`，随着搬运推进，最终停在原第 `right+1` 个结点（或 `null`）→ 区间尾正确接上后段。✅

∎

**再证「不会成环」**：每一步只改两条边（`cur.next`、`nxt.next`）和一条 `prev.next`；`nxt` 前插后，`prev` 段的所有结点都在 `cur` 之前或就是 `cur`，而 `cur.next` 指向的永远是「尚未处理」的结点，不可能指回 `prev` 段。∎

> **这个不变量一次性管住了三个最容易错的点**：循环次数是 `right - left`（不是 `right - left + 1`）、`cur` 全程不动、`prev` 全程不动。

### 045.3.3 通用解题思路（可迁移的那一层）

**链表章的统一语言：哑结点 + 指针三件套（prev/cur/next）+ 先存后断。** 045 是这套语言第一次完整登场：

```
原始问题：反转链表的一个区间
         ↓ 044 已经解决"整段反转"
复用：    把区间当作一条独立的链，调用 044 的四句模板
         ↓ 但区间两端会断
卡点：    接缝 —— 前段尾巴要指向区间新头，区间新尾要指向后段头
         ↓ 两个选择
路线 A：  切断 → 反转 → 接回（要 4 个额外指针）
路线 B：  头插法（prev 不动，接缝免费）← 本题最优
         ↓ 边界统一
外壳：    dummy 消灭 "left == 1 时没有前驱" 这个特判
```

**为什么「哑结点」在这题是必需品而不是锦上添花**：如果 `left == 1`，「区间前驱」不存在。用 `null` 表示它，那么 `prev.next` 这一步就会 NPE，所有代码都得写成 `if (left == 1) ... else ...`。加一个 `dummy`（`dummy.next = head`），前驱就永远存在，`left == 1` 和 `left = 5` 走的是同一条路径。**哑结点的本质是：把「不存在的对象」变成「存在的哨兵」，从而消掉分支。**

属于同一模型的题：

| 题 | 复用什么 | 边界难点 |
| --- | --- | --- |
| [LC 25 K 个一组翻转链表](https://leetcode.cn/problems/reverse-nodes-in-k-group/) | 区间反转 | 分组探路 + 组间接缝（046） |
| [LC 24 两两交换链表中的节点](https://leetcode.cn/problems/swap-nodes-in-pairs/) | 区间反转（长度 2） | 无 |
| [LC 61 旋转链表](https://leetcode.cn/problems/rotate-list/) | 找断点 + 接回 | `k` 可能大于链长，要取模 |
| [LC 19 删除倒数第 N 个结点](https://leetcode.cn/problems/remove-nth-node-from-end-of-list/) | 哑结点 + 双指针 | 删头结点（049） |
| [LC 83 删除排序链表中的重复元素](https://leetcode.cn/problems/remove-duplicates-from-sorted-list/) | 哑结点 | 删除后指针怎么续 |

### 045.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| 反转**多段**区间（给一组 `[l, r]`） | 区间可能重叠/相邻 | 按 `l` 排序后逐段处理；重叠要合并区间，**先把区间规范化再动链表** |
| 每 k 个一组反转 | 区间长度固定但位置递推 | 见 046，外层循环推进 `groupPrev` |
| `left`/`right` 越界 | 输入不合法 | 先遍历求长度 n，把 `right` 截到 `n`，`left` 小于 1 时截到 1 |
| 要求**不能改 `next`** | 只能改值 | 用数组存区间值、反转后写回，空间 O(区间长) |
| **双向链表**的区间反转 | 有 `prev` 指针 | 逐结点交换 `prev`/`next`，最后重新接前后段（两边都要改） |
| 区间反转后要求**返回新长度或新头** | 返回值语义变 | 头可能变成「原第 right 个」，所以仍要返回 `dummy.next` |

---

## 045.4 图解推演

### 045.4.1 头插法全过程（`[1,2,3,4,5]`，`left = 2`，`right = 4`）

```text
第 0 步：定位。prev 走 left-1 = 1 步 → 停在 1；cur = prev.next = 2
        dummy
          ↓
        [∅] ──→ [1] ──→ [2] ──→ [3] ──→ [4] ──→ [5] ──→ ∅
                  ↑       ↑
                 prev    cur        （循环 right-left = 2 次）

第 1 步：nxt = 3
        ① cur.next = nxt.next  →  2 指向 4
        ② nxt.next = prev.next →  3 指向 2
        ③ prev.next = nxt      →  1 指向 3
        [1] ──→ [3] ──→ [2] ──→ [4] ──→ [5] ──→ ∅
         ↑               ↑
        prev            cur          已搬部分：3→2

第 2 步：nxt = 4（注意：nxt 是 cur.next，不是 prev.next）
        ① cur.next = nxt.next  →  2 指向 5
        ② nxt.next = prev.next →  4 指向 3
        ③ prev.next = nxt      →  1 指向 4
        [1] ──→ [4] ──→ [3] ──→ [2] ──→ [5] ──→ ∅
         ↑                       ↑
        prev                    cur

循环结束：prev.next = 4（区间新头）✅   2.next = 5（区间尾接后段）✅
```

### 045.4.2 三条边在每一步的变化（状态表）

| 步 | `nxt` | ① `cur.next` 变成 | ② `nxt.next` 变成 | ③ `prev.next` 变成 | 已搬部分（`prev.next` 起） |
| --- | --- | --- | --- | --- | --- |
| 初始 | — | 3 | — | 2 | 空 |
| 1 | 3 | 4 | 2 | 3 | `3→2` |
| 2 | 4 | 5 | 3 | 4 | `4→3→2` |
| 结束 | — | 5 | — | 4 | `4→3→2`（长度 = right-left+1 = 3）✅ |

### 045.4.3 「切断 + 反转 + 接回」的路线图（对照组）

```text
① 切断：
        before            after
          ↓                 ↓
    … → [1]   [2]→[3]→[4]   [5] → …
              ↑
            segHead

② 反转（044）：
    [4] → [3] → [2] → ∅        segTail = 2

③ 接回：
    before.next = 反转后的新头（4）
    segTail.next = after（5）

    … → [1] → [4] → [3] → [2] → [5] → …
```

> 对比一下就很清楚：这条路要记住 `before` / `after` / `segHead` / `segTail` 四个位置，而头插法只需要 `prev` / `cur` / `nxt` —— 而且**接缝是副作用，不用显式写**。**「让接缝变成副作用」是链表题最高级的优化思路。**

---

## 045.5 双语言实现

### 045.5.1 Java

```java
class ListNode {
    int val;
    ListNode next;

    ListNode(int val) {
        this.val = val;
    }
}

class Solution {
    public ListNode reverseBetween(ListNode head, int left, int right) {
        ListNode dummy = new ListNode(0);
        dummy.next = head; // 哑结点：left == 1 时 prev 有落脚处

        ListNode prev = dummy;
        for (int i = 1; i < left; i++) { // 走 left-1 步，停在 left 前驱
            prev = prev.next;
        }

        ListNode cur = prev.next;             // 区间第一个结点，反转中始终是区间尾
        for (int i = 0; i < right - left; i++) {
            ListNode nxt = cur.next;          // 待搬运结点
            cur.next = nxt.next;              // ① 从原位摘下
            nxt.next = prev.next;             // ② 指向当前区间头
            prev.next = nxt;                  // ③ 挂到 prev 后面
        }
        return dummy.next;
    }
}
```

**四处「为什么」**：

1. `for (int i = 1; i < left; i++)`：循环体执行 `left - 1` 次，`prev` 落在第 `left - 1` 个结点上，即区间前驱。写成 `i <= left` 就会多走一步，把区间头也搬走。
2. `i < right - left`：区间有 `right - left + 1` 个结点，第一个已经在 `prev.next` 上了，所以只需再搬 `right - left` 个。
3. `nxt = cur.next` 而不是 `nxt = prev.next`：`prev.next` 已经是「刚搬过来的结点」，再用它会原地打转。
4. `return dummy.next` 而不是 `return head`：`left == 1` 时 `head` 已被搬到区间尾部。

### 045.5.2 Go

```go
package main

type ListNode struct {
	Val  int
	Next *ListNode
}

// 头插法：prev 固定在区间前驱不动，把后面每个结点依次插到 prev 后面
func reverseBetween(head *ListNode, left int, right int) *ListNode {
	dummy := &ListNode{Next: head}
	prev := dummy
	for i := 1; i < left; i++ { // 走 left-1 步，停在 left 的前驱
		prev = prev.Next
	}
	cur := prev.Next // 区间第一个结点，反转期间它一直是区间尾巴
	for i := 0; i < right-left; i++ {
		nxt := cur.Next      // 待搬运的结点
		cur.Next = nxt.Next  // ① 从原位摘下
		nxt.Next = prev.Next // ② 插到 prev 之后
		prev.Next = nxt      // ③ 成为已搬部分的新头
	}
	return dummy.Next
}
```

### 045.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 哑结点构造 | `new ListNode(0)` 再赋 `.next` | `&ListNode{Next: head}` 一行字面量 | Go 更简洁，且未写的字段自动为零值（`Val` = 0，本题不读它） |
| 循环变量作用域 | `for (int i = ...)` 循环体内可见 | `for i := ...` 同样只在本层作用域 | 无差异 |
| `nxt` 的声明 | 必须在循环体内 `ListNode nxt = ...` | `nxt := cur.Next` 每轮新建 | 两边行为一致，但 Go 若写 `var nxt *ListNode` 在循环外，就要小心「上一轮残留」 |
| 取值 / 改值 | `prev.next` | `prev.Next`（**大写**） | Go 的字段必须导出才能被同包外的代码访问；LeetCode 的 `ListNode` 字段是大写 `Val`/`Next` |
| 返回 | `dummy.next` | `dummy.Next` | 一致 |
| 空链表 | `head == null` 时 `dummy.next = head` 仍成立 | 同理，`&ListNode{Next: nil}` 合法 | **本题 `1 <= left`，链表必非空**，但哑结点写法对空链也安全 |

### 045.5.4 对拍测试（Go，2 万次随机 + 六个边界）

```go
package main

import (
	"math/rand"
	"testing"
)

// 暴力参考：切片上把 [left-1, right-1] 原地反转
func bruteReverseBetween(a []int, left, right int) []int {
	b := append([]int{}, a...)
	for i, j := left-1, right-1; i < j; i, j = i+1, j-1 {
		b[i], b[j] = b[j], b[i]
	}
	return b
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

func build(a []int) *ListNode {
	dummy := &ListNode{}
	t := dummy
	for _, v := range a {
		t.Next = &ListNode{Val: v}
		t = t.Next
	}
	return dummy.Next
}

func TestReverseBetweenRandom(t *testing.T) {
	r := rand.New(rand.NewSource(45))
	for iter := 0; iter < 20000; iter++ {
		n := 1 + r.Intn(10)
		a := make([]int, n)
		for i := range a {
			a[i] = r.Intn(4) // 大量重复值
		}
		left := 1 + r.Intn(n)
		right := left + r.Intn(n-left+1) // 保证 left <= right <= n

		want := bruteReverseBetween(a, left, right)
		got := dump(reverseBetween(build(a), left, right))
		if len(got) != len(want) {
			t.Fatalf("iter=%d a=%v l=%d r=%d got=%v want=%v", iter, a, left, right, got, want)
		}
		for i := range got {
			if got[i] != want[i] {
				t.Fatalf("iter=%d a=%v l=%d r=%d got=%v want=%v", iter, a, left, right, got, want)
			}
		}
	}
	// 六个边界：整条反转 / 单点区间 / 贴表尾 / 两结点 / 全相等 / left=right=1
	cases := []struct{ a []int; l, r int }{
		{[]int{1, 2, 3}, 1, 3},
		{[]int{1, 2, 3}, 2, 2},
		{[]int{1, 2, 3}, 2, 3},
		{[]int{1, 2}, 1, 2},
		{[]int{3, 3, 3}, 1, 3},
		{[]int{5}, 1, 1},
	}
	for _, c := range cases {
		got := dump(reverseBetween(build(c.a), c.l, c.r))
		want := bruteReverseBetween(c.a, c.l, c.r)
		for i := range want {
			if got[i] != want[i] {
				t.Fatalf("边界 a=%v l=%d r=%d got=%v want=%v", c.a, c.l, c.r, got, want)
			}
		}
	}
}
```

> **随机数据必须包含 `left == 1` 与 `right == n`**：`left` 用 `1 + r.Intn(n)` 生成，天然会覆盖 1；`right` 用 `left + r.Intn(n-left+1)` 生成，天然会覆盖 n。**边界不是「另外补的测试」，而应该被数据生成器自然覆盖**——这是写好链表对拍的关键技巧。

---

## 045.6 复杂度与常见坑

### 045.6.1 复杂度推导

- **时间 O(n)**：定位阶段走 `left - 1` 步；搬运阶段走 `right - left` 步；总计 `right - 1 ≤ n - 1` 步，每步常数次赋值。**只遍历了一次**（严格说是「走了 right-1 步」，没有回头）。
- **空间 O(1)**：`dummy`、`prev`、`cur`、`nxt` 四个指针，与 n 无关（`dummy` 是唯一新分配的结点）。
- **为什么不是 O(right - left)**：定位也要花时间。若 `left = n`，仍要 O(n) 才能找到区间前驱 —— 这是单链表不支持随机访问的必然代价。

### 045.6.2 七个真实踩过的坑

1. **不用哑结点，`left == 1` 时 NPE** —— `prev` 初值为 `null`，`prev.next` 直接崩。修法：永远 `dummy.next = head`，`prev = dummy`。
2. **循环次数写成 `right - left + 1`** —— 多搬一次，把区间后的第一个结点也拉进来，`[1,2,3,4,5], 2, 4` 会输出 `[1,5,4,3,2]`。
3. **`nxt = prev.next`** —— 每轮拿到的是「上一轮刚插进去的结点」，于是原地打转、死循环或结果错乱。
4. **定位循环写成 `i <= left`** —— 多走一步，`prev` 落到区间第一个结点上，整个区间被挪走。
5. **`return head`** —— `left == 1` 时 `head` 已经变成区间尾巴（`[1,2,3] → [3,2,1]`，返回 1 得 `[1]`）。
6. **`right == n` 时区间尾巴的 `next` 没置空** —— 如果实现里显式 `segTail.next = after`，而 `after` 恰好是 `null`，要确保写的是 `after`（可能是 `null`）而不是漏掉这一步，否则成环或残留旧指针。
7. **把 `left`/`right` 当成 0-based** —— 定位少走一步、循环次数差 1，两个错误同时发生，现象是「结果看起来像整体平移了一位」。

### 045.6.3 面试官最爱追问的三个问题

**Q1：为什么这题一定要用哑结点？**
> 「因为 `left` 可以是 1，此时区间前驱不存在。没有哑结点，`prev` 就得用 `null` 表示，后面每一次 `prev.next` 都要加判空分支，代码会分成两条路径。加哑结点后『前驱永远存在』这个不变量成立，`left == 1` 和 `left == 5` 走同一条代码路径，边界被结构性地消掉了，而不是靠 if 补掉。」

**Q2：头插法为什么不需要显式「接回」？**
> 「因为 `prev` 和 `cur` 从头到尾没离开原位：`prev` 是区间前驱，它的 `next` 每轮被更新成新区间头，最后自然接上；`cur` 是原区间第一个结点，反转后它必然是区间尾，而它在第 1 步摘结点时 `next` 就已经指向区间后段了。也就是说，两条接缝分别是『`prev.next` 的副作用』和『`cur.next` 的副作用』，不需要额外语句。」

**Q3：如果要求「只遍历一次」，头插法满足吗？「切断+反转+接回」满足吗？**
> 「头插法满足：定位后再也没有回头，总步数 `right - 1`。『切断+反转+接回』也满足，它同样先定位到 `left-1`，再在区间内反转，最后改两条边。真正不满足一次遍历的是那种『先求总长度再倒数定位』的写法 —— 那需要两趟。不过本题 `n ≤ 500`，两趟也无所谓；面试时说明「一次遍历」是加分项。」

---

## 045.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 206 反转链表](https://leetcode.cn/problems/reverse-linked-list/) | 简单 | 整条反转，本题的 `left=1, right=n` 特例（044） |
| [LC 25 K 个一组翻转链表](https://leetcode.cn/problems/reverse-nodes-in-k-group/) | 困难 | 区间长度固定为 k、位置递推，要探路（046） |
| [LC 24 两两交换链表中的节点](https://leetcode.cn/problems/swap-nodes-in-pairs/) | 中等 | 区间长度恒为 2 的简化版 |
| [LC 61 旋转链表](https://leetcode.cn/problems/rotate-list/) | 中等 | 成环 + 找断点，`k` 要对长度取模 |
| [LC 19 删除链表的倒数第 N 个结点](https://leetcode.cn/problems/remove-nth-node-from-end-of-list/) | 中等 | 同为「哑结点 + 定位」，用双指针定距（049） |
| [LC 143 重排链表](https://leetcode.cn/problems/reorder-list/) | 中等 | 区间反转的进阶：先找中点，再反转后半（051） |
| [LC 92 的变体：反转所有长度 ≥ 2 的连续相同值段](https://leetcode.cn/problems/reverse-linked-list-ii/) | — | 需要先扫描出区间列表，再逐段调用本题 |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 25 K 个一组**
1. 如果一上来就反转，走到一半发现不足 k 个，你有办法退回原状吗？如果不能，应该在反转**之前**做什么检查？
2. 每组反转结束时，组尾应该指向哪里？这个「哪里」在下一轮里会变成什么角色？

**LC 61 旋转链表**
1. `k` 比链长还大时，实际等价于移动多少步？这个数和链长的关系是什么运算？
2. 把链尾接到链头之后，你的「新尾巴」和「新头」分别在第几步位置？

**LC 24 两两交换**
1. 把本题的区间长度固定成 2，定位循环还需要单独写吗？

**通用**
- 只要题目出现「区间」「一段」，先在纸上写出四个名字：区间前驱 / 区间头 / 区间尾 / 区间后继。写代码时哪一个没被正确设置，就一定是它出问题。
- 判断要不要 dummy 的一句话：**头结点会不会被换掉或被删除**。会 → 必须用。

</details>

---

## 附：本章小结（可背走的三句话）

```text
① 区间反转 = 哑结点定位前驱 + 把区间结点逐个头插到前驱后面
② 循环 right-left 次（不是 +1）；prev 和 cur 全程不动，接缝是副作用
③ left 可能等于 1 → 哑结点不是可选项，是把分支消掉的结构手段
```

> **下一章**：[`046-reverse-nodes-in-k-group.md`](046-reverse-nodes-in-k-group.md) —— 同一个头插/整段反转模板，但要在一层循环里反复推进「上一组尾巴」，还要处理「不足 k 个不反转」。
