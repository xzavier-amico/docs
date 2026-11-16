# 046. LC25 K 个一组翻转链表（Reverse Nodes in k-Group）

> **状态**：`已完成（初稿）` ｜ **所属**：第 6 部分 · 链表 ｜ **编号**：046 / 150
> **难度**：困难 ｜ **标签**：链表 · 指针 · 分组 · 哑结点
> **一句话**：这道题真正考的是「**在一层循环里反复推进『上一组的尾巴』，并且在不破坏链表的前提下先探路确认够不够 k 个**」，不是「会反转」。
> **前置**：[`045-reverse-linked-list-ii.md`](045-reverse-linked-list-ii.md)（区间反转与哑结点定位）、[`044-reverse-linked-list.md`](044-reverse-linked-list.md)（三指针模板）

---

## 046.1 题目

**原题链接**：<https://leetcode.cn/problems/reverse-nodes-in-k-group/>

### 题意重述（把隐含条件补齐）

给你单链表的头结点 `head` 和一个正整数 `k`，请你**每 `k` 个结点一组进行翻转**，返回翻转后的链表。如果最后剩余的结点数**不足 `k` 个**，就**保持原有顺序**。

题干两句话，藏了六个必须读出来的条件：

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| **每 k 个一组，组内反转** | 「每 k 个结点一组进行翻转」 | 不是「反转前 k 个」，也不是「反转 k 次」 |
| **不足 k 个保持原序** | 「如果节点总数不是 k 的整数倍……保持原有顺序」 | 老老实实把尾巴也反转了，是最高频错误 |
| **不能改 `val`** | 指针题默认约定 | 抄值重排能过题，但被追问就废；本题官方要求「只能改动指针」 |
| **`k` 是正整数，可大于 `n`** | `1 <= k <= n` 之外的实际输入 | `k > n` 时**一个都不反**，直接返回原链 |
| **`k == 1` 时什么都不做** | 退化的输入 | 每轮都「反转」一个结点，把 `next` 改乱 |
| **`n` 可达 5000** | 数据范围 | 递归写法深度 `n/k`，`k = 1` 时是 5000 层，Java 能过但已接近上限 |

> ⚠️ 特别注意题干那半句「**不足 k 个保持原序**」。它不是一个可选优化，而是**算法结构的分水岭**：因为「不足 k 个不反转」，你必须在**动手反转之前**就知道这一段够不够 k 个 —— 这就是「探路」这一步的来源。如果题目改成「不足 k 个也反转」，这题会瞬间掉到中等。

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| `1 <= k <= n <= 5000` | n 很小 | O(n) 一定能过；O(n²) 也能过，所以**复杂度不是评分点** |
| `0 <= Node.val <= 1000` | 有重复值 | 不能靠值判断组边界，只能数个数 |
| 要求空间 O(1) | 进阶追问 | 迭代写法满足；递归是 O(n/k) 栈 |
| 「只能改动指针」 | 题目 note | 数组重排是「作弊解」，面试会被打回 |

> 💡 **量级 → 算法**：`n ≤ 5000` 再次说明链表题的规模是「够用就行」。本题的难度全在**结构**：探路、反转、接缝、推进，四步各有各的边界。**困难题的「困难」往往不是算法想不出来，而是四件事同时要对。**

### 示例

```text
输入：head = [1,2,3,4,5], k = 2
输出：      [2,1,4,3,5]
解释：1-2 反转成 2-1，3-4 反转成 4-3，剩下的 5 不足 2 个，保持原样

输入：head = [1,2,3,4,5], k = 3
输出：      [3,2,1,4,5]
解释：1-2-3 反转成 3-2-1，剩下 4-5 不足 3 个，保持原样（注意：不是 4-5 反转成 5-4）

输入：head = [1,2,3,4,5], k = 1
输出：      [1,2,3,4,5]

输入：head = [1], k = 2
输出：      [1]            ← k 大于链长，一个都不反
```

### 我自造的七个反例（写完必须逐个跑）

```text
① [] , k = 2                → 空链，必须返回 nil/null
② [1], k = 2                → k 大于链长：探路第一步就落空
③ [1,2], k = 2              → n 恰好等于 k：只有一组，组尾必须接 null
④ [1,2,3], k = 2            → 有一组 + 一个余数结点：最常见的形态
⑤ [1,2,3,4], k = 2          → 恰好两组：验证「上一组尾巴」推进对不对
⑥ [1,2,3,4,5,6], k = 3      → 恰好两组 + 无余数：组间连边最容易错
⑦ [7,7,7,7,7,7], k = 2      → 全相等值：验证没有偷偷用值比较
```

第 ⑤ 和第 ⑥ 条是「接缝重灾区」：只要 `groupPrev` 忘了推进，第二组就会被拼到第一组的**头**上，结果变成 `[2,1]` 加一段乱序，而第 1 组看起来完全正确 —— **这类 bug 只在小数据的两组交界处才暴露**。

---

## 046.2 从暴力到最优：第一个念头为什么错

### 第一步：暴力（转数组 → 分组反转 → 重建链表）

```text
vals = 链表所有值
for (i = 0; i + k <= n; i += k):
    反转 vals[i .. i+k-1]
用 vals 重建一条新链
```

- 时间 O(n)，空间 **O(n)**（数组 + 新结点）。
- 它「能 AC」，因为 LC 判题只看最终结果，不看你是不是改了指针。
- 但题目 note 明确写了「只能改动指针」，而且面试官会追问「空间能不能 O(1)」。这条路**在面试里是零分**。

### 第二步：卡点在哪 —— 三个卡点，不是一个

很多人以为「会反转 044 就够了」，然后写出这样的代码：

```text
// 想当然版：边数边反转
prev = null; cur = head; cnt = 0
while cur != null:
    nxt = cur.next
    cur.next = prev
    prev = cur
    cur = nxt
    cnt++
    if cnt == k: ...  // 该"收尾"了，但收什么？
```

这段代码在第 1 组就崩了，因为它**边反转边数**：等你数到 k 个，指针已经全部掉头，你手上只剩「组尾」，而**组的入口（原来的第一个结点）已经被埋在里面了**，而且**下一组的起点**要看你有没有提前存。三个卡点依次浮出：

| 卡点 | 具体表现 | 需要的动作 |
| --- | --- | --- |
| **卡点 1：不知道够不够 k 个** | 反转到一半发现只剩 2 个，没法退回原状（箭头已经掰了） | **先探路**：从组前驱走 k 步，走不到就是不够 |
| **卡点 2：组尾该指向谁** | 组尾的 `next` 本来是 `null`，需要指向下一组（或 `null`） | 反转时把 **`prev` 的初值设成 `groupNext`** |
| **卡点 3：上一组和下一组怎么粘** | 上一组的尾巴要指向本组新头；本组新尾巴要成为下一组的前驱 | 维护 `groupPrev`，每轮更新成**本组原来的第一个结点** |

### 第三步：灵感的来源 —— 「探路 + 以 groupNext 为 prev 起点」

**灵感 1：探路必须独立于反转。**

因为反转是**破坏性**的（改了就回不去），所以「够不够 k 个」必须在动手之前回答。做法是：

```
kth = groupPrev
走 k 步
if kth == null: break      // 不足 k 个，剩余部分原样保留
```

`kth` 走完 k 步后正好落在**本组第 k 个结点**上 —— 它也将成为本组反转后的**新头**。所以这一步既做了「探路」，又顺手拿到了「新头」，零浪费。

**灵感 2：把 `groupNext` 当成 `prev` 的初值。**

这是本题最漂亮的一手。044 里 `prev` 初值是 `null`，因为整条链的尾巴要指向 `null`。而这里，**本组反转后，组尾应该指向 `groupNext`**（下一组的第一个结点）。所以只要：

```
prev = groupNext        // 而不是 null
cur  = groupPrev.next   // 本组第一个结点
while cur != groupNext:
    nxt = cur.next
    cur.next = prev
    prev = cur
    cur = nxt
```

反转结束时，`prev` 就是本组新头（原第 k 个结点），而**组尾的 `next` 已经被自动设成了 `groupNext`** —— 后向接缝不用写任何额外代码！

> 这一个技巧把「组尾接下一组」这条最容易漏的边，变成了反转循环的**免费副作用**。**这是 046 的核心技巧，也是它被称为「困难」的原因：技巧本身不难，但要想到「用哨兵当 prev 初值」。**

**灵感 3：`groupPrev` 每轮要推进到「本组原来的第一个结点」。**

本组原来的第一个结点（记作 `oldStart`），反转后成了**本组的尾巴**，也就天然是**下一组的前驱**。所以在接缝时：

```
oldStart = groupPrev.next   // 注意：必须在改 groupPrev.next 之前存
groupPrev.next = kth        // 前向接缝：上一组尾巴 → 本组新头
groupPrev = oldStart        // 推进：下次从本组尾巴出发
```

顺序不能反：先存 `oldStart`，再改 `groupPrev.next`，最后推进。又是一个「**先存后断**」。

### 另一条路：递归

```text
reverseKGroup(head, k):
    探路：从 head 走 k 步
    if 走不到: return head                 // 不足 k 个，原样返回
    反转前 k 个，得到 newHead（= 第 k 个结点）
    原来的 head（现在是第 k 个的前一个，即组尾）的 next = reverseKGroup(第 k+1 个, k)
    return newHead
```

思路极清晰，和 044 的递归版同构。代价：递归深度 `n/k`，`k = 1` 时是 5000 层。Java 默认栈大约能扛 1e4 层，所以本题能过；但这是**侥幸**，不是设计。

---

## 046.3 原理拆解

### 046.3.1 数据结构与算法选型

| 方案 | 时间 | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- |
| 数组重排 + 重建链 | O(n) | O(n) | 15 行 | **题目明令「只能改指针」**；面试判零分 |
| 递归（探路 + 递归剩余） | O(n) | **O(n/k) 栈** | 20 行 | `k = 1` 且 `n = 10⁵` 时栈溢出 |
| 迭代：边反转边数（不探路） | — | — | — | **不成立**，反转到一半无法回退 |
| **迭代：探路 + 以 groupNext 为 prev** ✅ | **O(n)** | **O(1)** | **25 行** | 几乎不会输；`groupPrev` 推进写错就全错 |
| 迭代：先切段、再调 044、再接回 | O(n) | O(1) | 30 行 | 每条接缝都要显式写，比上一行多 5 行、多 2 个变量 |

**为什么迭代版是唯一推荐**：题目要求「只能改指针」，空间要求 O(1)，且 `k` 可能为 1（递归深度 = n）。三个条件同时满足的只有迭代版。

### 046.3.2 为什么这个做法是对的（正确性论证）

**外层循环不变量**（每轮开始前）：

> `groupPrev` 指向「**已经处理完的所有组**」的最后一个结点（第一轮时它是 `dummy`）；`groupPrev.next` 指向「**尚未处理的第一个结点**」；并且已经处理的部分是一条**正确反转**的链，其尾部通过 `groupPrev.next` 与未处理部分相连。

**内层「探路」的正确性**：从 `groupPrev` 出发走 k 步得到 `kth`。由于 `groupPrev` 的下一跳是未处理部分的第 1 个结点，`kth` 就是未处理部分的第 k 个结点。`kth == null` 当且仅当未处理部分不足 k 个结点。所以「`kth == null` → break」恰好实现了「不足 k 个保持原序」。∎

**内层「反转」的正确性**：设本组为 `p1 → p2 → … → pk → groupNext`，`prev` 初值 `groupNext`。

用归纳证明：执行 i 次后，`prev = pi`（i ≥ 1）或 `groupNext`（i = 0），且 `pi → p(i-1) → … → p1 → groupNext` 已成链，`cur = p(i+1)`。

- i = 0：`prev = groupNext`，链为 `groupNext`（空段），`cur = p1`。成立。
- 从 i 到 i+1：`nxt = p(i+2)`；`p(i+1).next = pi`，于是 `p(i+1) → pi → … → p1 → groupNext`；`prev = p(i+1)`；`cur = p(i+2)`。成立。

终止于 `cur == groupNext`（即 i = k），此时 `prev = pk`，链为 `pk → p(k-1) → … → p1 → groupNext`。也就是说：

- 新头 = `pk`（原第 k 个结点），这正好是探路得到的 `kth`；
- 组尾 = `p1`（原第 1 个结点），它的 `next` 在 i = 1 那一步被设成 `groupNext`，此后不再改变 —— **后向接缝自动完成**。∎

**接缝的正确性**：`groupPrev.next = kth` 让「已处理部分的尾巴」指向「本组新头」；`groupPrev = oldStart = p1` 让「已处理部分的尾巴」推进到「本组新尾」。两个赋值合起来，外层不变量在新一轮成立。∎

**终止性**：每轮处理的组长度恰为 k ≥ 1，`groupPrev` 严格向后推进 k 个结点，所以最多 `⌊n/k⌋` 轮后 `kth == null`，循环结束。∎

> **这个论证的价值**：它把「后向接缝为什么不用写」这件事讲清楚了 —— 因为 `p1.next` 在内层循环的**第一轮**就被设成了 `groupNext`，而 `p1` 之后再也不被访问（它已经是尾巴）。**「一次设定，永不再改」就是「免费接缝」的全部原理。**

### 046.3.3 通用解题思路（可迁移的那一层）

**链表章的统一语言：哑结点 + 指针三件套（prev/cur/next）+ 先存后断。** 046 是这套语言的「组合应用」：它把 044 的四句模板包在一个外层循环里，并额外引入一个 `groupPrev` 来记住「已处理部分的尾巴」。

```
原始问题：每 k 个结点一组反转，不足 k 个不动
         ↓ 分解成两个子问题
子问题 A：这一段够不够 k 个？        → 探路（只走不改，安全）
子问题 B：把这一段反转并接回两端    → 044 模板 + groupNext 当 prev
         ↓ 外层再加一件事
粘合：    每轮结束把 groupPrev 推到本组原来的第一个结点
         ↓ 边界统一
外壳：    dummy 消灭「第一组没有前驱」
```

**「探路（只读）」与「操作（读写）」分离**，是这道题最值得带走的思想。它的适用面远超链表：

| 场景 | 「探路」是什么 | 「操作」是什么 |
| --- | --- | --- |
| 046 K 个一组 | 数够不够 k 个 | 反转这 k 个 |
| 反转字符串里的单词 | 先找到单词边界 | 再翻转单词 |
| 文件批量处理 | 先统计/校验整批 | 再写入 |
| 数据库事务 | 先校验约束 | 再提交 |

> **一句话**：**破坏性操作之前，先用只读操作确认「该不该做、做到哪」。** 这条原则在链表题里叫「探路」，在系统设计里叫「预检」（precheck），本质相同。

属于同一模型的题：

| 题 | 探路做什么 | 操作做什么 |
| --- | --- | --- |
| [LC 92 反转链表 II](https://leetcode.cn/problems/reverse-linked-list-ii/) | 定位 `left-1` | 头插反转（045） |
| [LC 24 两两交换链表中的节点](https://leetcode.cn/problems/swap-nodes-in-pairs/) | k = 2 的退化探路 | 交换一对 |
| [LC 143 重排链表](https://leetcode.cn/problems/reorder-list/) | 找中点（快慢指针） | 反转 + 合并（051） |
| [LC 19 删除倒数第 N 个结点](https://leetcode.cn/problems/remove-nth-node-from-end-of-list/) | 快指针先走 n+1 步定距 | 删除（049） |
| [LC 61 旋转链表](https://leetcode.cn/problems/rotate-list/) | 求长度、算断点位置 | 成环 + 断链 |

### 046.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| 不足 k 个**也**反转 | 探路可以省掉 | 循环条件改成「非空就反」；但**组尾的 `next` 该指向谁**要看清楚，最后一段没有 `groupNext` |
| 从**右往左**每 k 个分组 | 分组起点变了 | 先求 n，算出第一组的偏移 `n % k`，前 `n % k` 个不动，后面照旧 |
| `k` 是**变量**、每次不一样 | 每组长度不同 | 探路步数变成参数；`groupPrev` 推进逻辑不变 |
| 分组后**组内不反转、组间反转** | 语义翻转 | 先按组切片存进数组，再倒序拼接 |
| 要求**同时**保证「结点值不变，只有链接变」 | 已经满足 | 本题就是纯指针操作，无需改动 |
| 用**双向链表** | 有 `prev` | 反转时两个指针都要改；但探路仍需要，因为 `prev` 不提供「后面够不够」的信息 |

---

## 046.4 图解推演

### 046.4.1 一轮完整的三步（`[1,2,3,4,5]`，`k = 3`）

```text
初始：
  dummy   head
    ↓      ↓
   [∅] → [1] → [2] → [3] → [4] → [5] → ∅
    ↑
  groupPrev

① 探路：kth 从 groupPrev 走 3 步
  dummy → 1 → 2 → 3
                  ↑
                 kth          groupNext = kth.next = 4

② 反转（prev 从 groupNext 起步，走 044 的模板）
   prev=4, cur=1:  nxt=2  1.next=4   prev=1  cur=2
   prev=1, cur=2:  nxt=3  2.next=1   prev=2  cur=3
   prev=2, cur=3:  nxt=4  3.next=2   prev=3  cur=4
   cur == groupNext → 停

   此时链形态：
   dummy → 1?  ✗ 1 现在在区间内部
   实际：  3 → 2 → 1 → 4 → 5 → ∅
           ↑           ↑
          prev        （1.next 已自动 = 4，后向接缝 ✅）
   但 dummy.next 还指着 1，前向接缝 ✗

③ 接缝 + 推进
   oldStart = groupPrev.next = 1
   groupPrev.next = kth = 3      ← 前向接缝
   groupPrev = oldStart = 1      ← 下一组的前驱

   dummy → [3] → [2] → [1] → [4] → [5] → ∅
                           ↑
                        groupPrev（= oldStart）

②' 第二轮探路：从 groupPrev 走 3 步 → 1 → 4 → 5 → 再走一步是 ∅
   kth == ∅  →  break，剩余 [4,5] 原样保留 ✅

最终：3 → 2 → 1 → 4 → 5
```

### 046.4.2 每轮四个变量的取值（状态表）

| 轮 | `groupPrev` | `kth` | `groupNext` | `oldStart` | 操作后 `groupPrev` | 链（从 dummy 起） |
| --- | --- | --- | --- | --- | --- | --- |
| 初始 | `dummy` | — | — | — | — | `1→2→3→4→5` |
| 1 | `dummy` | 3 | 4 | 1 | 1 | `3→2→1→4→5` |
| 2 | 1 | ∅ | — | — | — | `3→2→1→4→5`（break） |

### 046.4.3 六个结点、两组：接缝的连续推进（`k = 3`）

```text
初始：
  [∅] → 1 → 2 → 3 → 4 → 5 → 6 → ∅
   ↑
  groupPrev

第 1 组后：
  [∅] → 3 → 2 → 1 → 4 → 5 → 6 → ∅
                   ↑
                groupPrev（= 原 1）

第 2 组（4,5,6）探路：kth = 6，groupNext = ∅
反转后（prev 初值 = ∅，因为 groupNext 是 ∅）：
  6 → 5 → 4 → ∅

接缝：oldStart = 4；groupPrev.next = 6；groupPrev = 4
  [∅] → 3 → 2 → 1 → 6 → 5 → 4 → ∅   ✅

下一轮：从 4 走 3 步 → ∅，break。结束。
```

### 046.4.4 迭代 vs 递归

```text
                ┌──────── 输入 head, k ────────┐
                │                              │
    ┌───────────▼───────────┐      ┌───────────▼────────────┐
    │ 迭代（探路 + 反转）    │      │ 递归（探路 + 子问题）   │
    │ O(n) / O(1)            │      │ O(n) / O(n/k) 栈       │
    └───────────┬───────────┘      └───────────┬────────────┘
                │                              │
   groupPrev 显式推进              组尾.next = reverseKGroup(剩余)
   接缝靠 groupNext 当 prev        接缝靠递归返回值
                │                              │
   ✅ 空间 O(1)，k=1 也安全         ✅ 代码短，与 044 递归版同构
   ✅ 面试首选                      ❌ k=1 且 n 大时栈溢出
```

---

## 046.5 双语言实现

### 046.5.1 Java

```java
class ListNode {
    int val;
    ListNode next;

    ListNode(int val) {
        this.val = val;
    }
}

class Solution {
    public ListNode reverseKGroup(ListNode head, int k) {
        ListNode dummy = new ListNode(0);
        dummy.next = head;
        ListNode groupPrev = dummy; // 上一组反转后的尾巴

        while (true) {
            // ① 探路：走 k 步确认本组够长
            ListNode kth = groupPrev;
            for (int i = 0; i < k && kth != null; i++) {
                kth = kth.next;
            }
            if (kth == null) {
                break; // 不足 k 个，剩余保持原样
            }

            ListNode groupNext = kth.next; // 组内反转的终止哨兵

            // ② 组内反转：就是 044 的写法，只是 prev 从 groupNext 起步
            ListNode prev = groupNext;
            ListNode cur = groupPrev.next;
            while (cur != groupNext) {
                ListNode nxt = cur.next;
                cur.next = prev;
                prev = cur;
                cur = nxt;
            }

            // ③ 接缝：groupPrev.next 是反转前的组头 → 现在是组尾
            ListNode oldStart = groupPrev.next;
            groupPrev.next = kth; // kth 现在是组头
            groupPrev = oldStart; // 下一组的连接点
        }
        return dummy.next;
    }
}
```

**五处「为什么」**：

1. `ListNode kth = groupPrev` 而不是 `groupPrev.next`：从**前驱**开始走 k 步，落点才是本组第 k 个。从组头走 k 步会落到下一组的第 1 个。
2. `prev = groupNext` 而不是 `null`：这是「后向接缝免费」的全部秘密。组尾的 `next` 在内层第一轮就被设成 `groupNext`，此后不再改。
3. `cur != groupNext` 而不是 `cur != null`：本组后面还有别的组，用 `null` 会一路反到底。
4. `oldStart` 必须在 `groupPrev.next = kth` **之前**取：改完之后 `groupPrev.next` 已经是 `kth`，`oldStart` 就成了本组新头（原第 k 个），推进方向就错了。
5. `return dummy.next`：第一组的头会被换掉，`head` 不再是结果链的头。

### 046.5.2 Go

```go
package main

type ListNode struct {
	Val  int
	Next *ListNode
}

// 分组反转：先探路确认够 k 个，再整组掉头，最后接缝
func reverseKGroup(head *ListNode, k int) *ListNode {
	dummy := &ListNode{Next: head}
	groupPrev := dummy // 上一组的尾巴（也是本组的前驱）

	for {
		// ① 探路：从 groupPrev 走 k 步，落到本组第 k 个结点
		kth := groupPrev
		for i := 0; i < k && kth != nil; i++ {
			kth = kth.Next
		}
		if kth == nil {
			break // 不足 k 个，剩余部分原样保留
		}

		// ② 记下本组之后的位置，它是组内反转的「终止哨兵」
		groupNext := kth.Next

		// ③ 组内反转：以 groupNext 为 prev 起点，走一遍 044
		prev, cur := groupNext, groupPrev.Next
		for cur != groupNext {
			nxt := cur.Next
			cur.Next = prev
			prev = cur
			cur = nxt
		}

		// ④ 接缝：groupPrev.Next 原本是本组第 1 个（反转后成尾巴）
		oldStart := groupPrev.Next
		groupPrev.Next = kth // kth 反转后成了本组新头
		groupPrev = oldStart // 下一组的前驱 = 本组尾巴
	}
	return dummy.Next
}
```

### 046.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| `while (true)` 的写法 | `while (true)` | `for { }`（Go 没有 while 关键字） | 语义相同；Go 的 `for {}` 必须靠 `break` 退出，注意别漏 |
| 多重赋值 | 只能分开写 `prev = cur; cur = nxt` | 可写 `prev, cur := groupNext, groupPrev.Next` | Go 在多指针初始化时更紧凑，但**内层循环里千万别写 `prev, cur = cur, nxt` 之类**，容易看错顺序 |
| 循环内变量 | 每次循环都是新的 | `nxt := cur.Next` 每轮新建（Go 1.22 前后循环变量语义有变，但这里在循环体内，无影响） | 无差异 |
| 探路的空判断 | `kth != null` | `kth != nil` | 必须写在 `for` 条件里，否则 `kth = kth.next` 会 NPE/panic |
| 指针字段 | `.val` / `.next` | `.Val` / `.Next` | Go 首字母大写才能跨包访问 |
| 返回 | `dummy.next` | `dummy.Next` | 一致 |

### 046.5.4 对拍测试（Go，2 万次随机 + 逐组暴力 + 七个边界）

```go
package main

import (
	"math/rand"
	"testing"
)

// 逐组暴力：切片上每 k 个反转一次，末尾不足 k 个保持原序
func bruteReverseKGroup(a []int, k int) []int {
	b := append([]int{}, a...)
	for i := 0; i+k <= len(b); i += k {
		for x, y := i, i+k-1; x < y; x, y = x+1, y-1 {
			b[x], b[y] = b[y], b[x]
		}
	}
	return b
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

func TestReverseKGroupRandom(t *testing.T) {
	r := rand.New(rand.NewSource(46))
	for iter := 0; iter < 20000; iter++ {
		n := r.Intn(14) // 含 0 / 1 / 2
		a := make([]int, n)
		for i := range a {
			a[i] = r.Intn(3) // 值域 {0,1,2}，大量重复
		}
		k := 1 + r.Intn(5) // k 可能大于 n（这时一个都不该反）
		want := bruteReverseKGroup(a, k)
		got := dump(reverseKGroup(build(a), k))
		if len(got) != len(want) {
			t.Fatalf("iter=%d a=%v k=%d got=%v want=%v", iter, a, k, got, want)
		}
		for i := range got {
			if got[i] != want[i] {
				t.Fatalf("iter=%d a=%v k=%d got=%v want=%v", iter, a, k, got, want)
			}
		}
	}
	// 七个边界逐个人工核对
	cases := []struct {
		a    []int
		k    int
		want []int
	}{
		{nil, 2, nil},                        // ① 空链
		{[]int{1}, 2, []int{1}},              // ② k 大于链长
		{[]int{1, 2}, 2, []int{2, 1}},        // ③ n == k
		{[]int{1, 2, 3}, 2, []int{2, 1, 3}},  // ④ 一组 + 余数
		{[]int{1, 2, 3, 4}, 2, []int{2, 1, 4, 3}},          // ⑤ 恰好两组
		{[]int{1, 2, 3, 4, 5, 6}, 3, []int{3, 2, 1, 6, 5, 4}}, // ⑥ 两组无余数
		{[]int{7, 7, 7, 7, 7, 7}, 2, []int{7, 7, 7, 7, 7, 7}}, // ⑦ 全相等
	}
	for i, c := range cases {
		got := dump(reverseKGroup(build(c.a), c.k))
		if len(got) != len(c.want) {
			t.Fatalf("边界 %d: a=%v k=%d got=%v want=%v", i+1, c.a, c.k, got, c.want)
		}
		for j := range got {
			if got[j] != c.want[j] {
				t.Fatalf("边界 %d: a=%v k=%d got=%v want=%v", i+1, c.a, c.k, got, c.want)
			}
		}
	}
}
```

> **两个细节值得抄走**：① `dump` 带 10 万步上限——046 写错最容易产生**环**（`groupPrev` 没推进、`groupNext` 记错），没有上限会长跑不返回；② `k` 生成范围 `1..5` 而 `n` 是 `0..13`，**天然覆盖 `k > n`、`k == n`、`n % k == 0`、`n % k != 0` 四种关系**，不需要单独构造。

---

## 046.6 复杂度与常见坑

### 046.6.1 复杂度推导

- **时间 O(n)**：探路每轮走 k 步；反转每轮走 k 步；共有 `⌊n/k⌋` 个完整组，加最后一轮失败的探路最多走 k 步。总步数 ≤ `2k·⌊n/k⌋ + k ≤ 2n + k = O(n)`。**每一组恰好被遍历两遍（探路一遍、反转一遍）**，常数因子 2。
- **空间 O(1)**：`dummy` + `groupPrev` + `kth` + `groupNext` + `prev` + `cur` + `nxt` + `oldStart`，8 个指针，其中 `nxt` 是循环内局部变量。与 n、k 都无关。
- **递归版空间 O(n/k)**：递归深度等于完整组的个数，`k = 1` 时等于 n。
- **为什么不能只遍历一遍**：因为「不足 k 个不动」要求**先知道够不够**，而链表不支持随机访问，无法「回头」。所以探路与反转必然分两趟。**这是问题本身的下界，不是实现缺陷。**

### 046.6.2 八个真实踩过的坑

1. **探路起点写成 `groupPrev.next`** —— 走 k 步落到「下一组第 1 个」，`kth` 用错位置，整组错位。
2. **`prev` 初值写 `null`** —— 组尾指向 `null`，后向接缝丢失，从第二组开始整段断链。
3. **内层 `while (cur != null)`** —— 一路反到底，把后面的组也翻了（这是「不足 k 个也反转」的错误版本）。
4. **`oldStart` 取晚了** —— 在 `groupPrev.next = kth` 之后再取，拿到的是 `kth`，下一轮 `groupPrev` 就指到组头，第二组会被拼到第一组头前面。
5. **`groupPrev` 忘了推进** —— 死循环（`groupPrev` 不变，探路结果永远相同）。判题机上是超时，不是答案错。
6. **`k == 1` 时多走一步** —— 探路走 1 步得到 `kth = 组头`，反转循环 `cur != groupNext` 一次都不执行（因为 `cur` 就是 `groupNext` 的前一个……实际上 `cur = groupPrev.next = kth`，而 `groupNext = kth.next`，循环会执行一次，把组头 `next` 改成自己 → **自环**）。必须验证 `k == 1`。
7. **`k > n` 时先反转再判断** —— 直接崩或把整条链反了。探路必须在反转前。
8. **`return head`** —— 第一组的头已换，返回的是某组的尾巴。

### 046.6.3 面试官最爱追问的四个问题

**Q1：为什么 `prev` 的初值不是 `null` 而是 `groupNext`？**
> 「因为本组反转后，组尾必须指向下一组的第一个结点，也就是 `groupNext`。把 `prev` 设成 `groupNext`，内层循环第一轮就把 `p1.next` 设成了 `groupNext`，而 `p1` 之后成为组尾、再也不会被访问。于是『组尾接后段』这条边变成反转的免费副作用，一行额外代码都不用写。如果写成 `null`，就得在反转后手动 `oldStart.next = groupNext`，多一条边、多一个出错点。」

**Q2：为什么必须先探路，能不能边反转边数？**
> 「不能。反转是破坏性的：`cur.next = prev` 一执行，下一站就没了，你无法在中途『退回』一条已经掉头的链。所以『够不够 k 个』必须在任何写操作之前回答。这就是探路必须独立的根本原因 —— 它是一次**纯只读的预检**。反过来说，如果题目允许「不足 k 个也反转」，探路确实可以省掉。」

**Q3：时间和空间分别是多少？能不能再优化？**
> 「时间 O(n)，准确说是 2n 步左右：每组被走两遍（探路一遍、反转一遍）。空间 O(1)，8 个指针。**时间上无法再优化到一遍**，因为『不足 k 个不动』要求先知道长度信息，而单链表不支持回退；除非题目放宽条件。空间已经是 O(1)，没法更少。」

**Q4：如果 `k` 不是常数，而是每次调用都可能不同，复杂度变吗？**
> 「不变。复杂度表达式里 k 只影响常数因子（每组走 2k 步，共 ⌊n/k⌋ 组，乘起来还是 2n）。只要 k ≥ 1，总步数就是 O(n)。唯一要注意的是 `k = 1` 时内层循环退化成单次赋值，容易写出自环 bug，必须专门测。」

---

## 046.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 206 反转链表](https://leetcode.cn/problems/reverse-linked-list/) | 简单 | `k = n` 的特例（044） |
| [LC 92 反转链表 II](https://leetcode.cn/problems/reverse-linked-list-ii/) | 中等 | 只反一段，位置给定（045） |
| [LC 24 两两交换链表中的节点](https://leetcode.cn/problems/swap-nodes-in-pairs/) | 中等 | `k = 2` 的特例，可写更短的迭代 |
| [LC 61 旋转链表](https://leetcode.cn/problems/rotate-list/) | 中等 | 「分组」的另一种形态：整体平移 |
| [LC 143 重排链表](https://leetcode.cn/problems/reorder-list/) | 中等 | 找中点 + 反转 + 合并（051） |
| [LC 234 回文链表](https://leetcode.cn/problems/palindrome-linked-list/) | 简单 | 反转后半段后比较，组间不涉及推进 |
| [LC 25 的变体：不足 k 个也反转](https://leetcode.cn/problems/reverse-nodes-in-k-group/) | — | 去掉探路，但组尾指向要重新推 |
| [LC 23 合并 K 个升序链表](https://leetcode.cn/problems/merge-k-sorted-lists/) | 困难 | 另一种「K 个为一组」的处理，思路是堆/分治 |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 24 两两交换**
1. 把 `k` 写死成 2，探路循环还需要吗？如果不需要，`groupNext` 怎么一眼得到？

**LC 61 旋转链表**
1. 向右移动 `k` 位，和「把链尾移到前面」之间差了多少次？`k` 大于链长时该对什么取模？
2. 成环之后，你要在哪里断开？断点与「新尾巴」是什么关系？

**LC 23 合并 K 个升序链表**
1. 每次从 K 条链里取最小，最直接的做法是什么结构？它的复杂度是多少？
2. 如果不用堆，「两两合并」要合并多少轮？每轮总工作量是多少？

**LC 25 的变体（不足 k 个也反转）**
1. 去掉探路后，最后一组的组尾该指向 `null` 还是 `groupNext`？这两者在什么情况下相同？

**通用**
- 只要出现「按固定长度分组」，先问：不够一组时该怎么办？这个答案决定了要不要探路。
- 接缝类 bug 的通用排查法：在纸上画出「上一组尾巴 → 本组新头」和「本组新尾 → 下一组头」两条箭头，然后逐行检查代码里哪一行负责哪条箭头。**凡是找不到负责语句的箭头，就是 bug。**

</details>

---

## 附：本章小结（可背走的三句话）

```text
① 三步走：探路（只读，确认够 k 个）→ 反转（prev 从 groupNext 起步）→ 接缝（groupPrev 推进到 oldStart）
② prev 初值 = groupNext，让「组尾接后段」变成反转的免费副作用
③ 破坏性操作之前先预检——这是 046 教给所有链表题的一条通用原则
```

> **下一章**：[`047-merge-two-sorted-lists.md`](047-merge-two-sorted-lists.md) —— 从「改箭头」换到「挑结点」，哑结点从「消边界」升级为「答案的容器」。
