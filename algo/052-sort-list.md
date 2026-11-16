# 052. LC148 排序链表（Sort List）

> **状态**：`已完成（初稿）` ｜ **所属**：第 6 部分 · 链表 ｜ **编号**：052 / 150
> **难度**：中等 ｜ **标签**：链表 · 归并排序 · 自底向上 · 倍增
> **一句话**：这道题真正考的是「**把递归版归并排序改写成『段的长度不断翻倍』的迭代版，从而把 O(log n) 的递归栈也压成 O(1)**」——`step` 倍增是这一整章唯一的新东西。
> **前置**：[`051-reorder-list.md`](051-reorder-list.md)（找中点）、[`047-merge-two-sorted-lists.md`](047-merge-two-sorted-lists.md)（归并合并）

---

## 052.1 题目

**原题链接**：<https://leetcode.cn/problems/sort-list/>

### 题意重述（把隐含条件补齐）

给你链表的头结点 `head`，请把它按**升序**排列并返回排序后的头结点。要求时间 **O(n log n)**、空间 **O(1)**。

五个必须读出来的条件：

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| **时间 O(n log n)** | 进阶要求 | 插入排序 O(n²) 会 TLE（`n = 5·10⁴` 时约 1.25·10⁹ 次操作） |
| **空间 O(1)** | 进阶要求 | 自顶向下归并的递归栈是 O(log n)，不算 O(1) |
| **必须动指针** | 「排序链表」 | 抄值到数组排完再写回，空间 O(n)，且面试会被追问 |
| **值可重复、可为负** | `-10⁵ <= Node.val <= 10⁵` | 比较必须稳定（`<=`），且不能拿值当「结束标记」 |
| **`0 <= 结点数`** | 数据范围 | 空链、单结点必须能返回 |

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| `0 <= 结点数 <= 5·10⁴` | 5 万 | `n log n ≈ 5·10⁴ × 16 = 8·10⁵`，非常宽松；**O(n²) = 1.25·10⁹ 会 TLE** |
| `-10⁵ <= Node.val <= 10⁵` | 有负数、有重复 | 比较用 `<=`；不能做计数排序（值域 2·10⁵ 也不大，但那是 O(n + V) 的另一种路） |
| 空间 O(1) | 进阶要求 | **自底向上归并**是唯一同时满足 O(n log n) + O(1) 的做法 |
| 单链表 | 结构 | 无法随机访问，所以快排的 `partition` 与「三数取中」都很难做 |

> 💡 **量级 → 算法**：`5·10⁴` 这个规模，`n log n` 和 `n²` 的分界非常清楚：`n log n ≈ 8·10⁵`（微秒级），`n² ≈ 2.5·10⁹`（秒级）。**看到 5·10⁴ 就该条件反射：O(n log n) 是唯一选择，且不允许额外 O(n) 空间。** 这两条约束一起把答案锁死成「自底向上归并」。

### 示例

```text
输入：head = [4,2,1,3]
输出：      [1,2,3,4]

输入：head = [-1,5,3,4,0]
输出：      [-1,0,3,4,5]

输入：head = []
输出：      []
```

### 我自造的八个反例（写完必须逐个跑）

```text
① []                     → 空链：直接返回，不能进任何循环
② [1]                    → 单结点：step 循环一次都不该进
③ [2,1]                  → 两结点：step = 1 一轮就够
④ [4,4,4,4]              → 全相等：验证 <= 不丢结点、不死循环
⑤ [1,2,3,4,5]            → 已升序：归并**仍要做**（不能提前判「已有序」而漏改指针）
⑥ [5,4,3,2,1]            → 完全逆序：最常见的形态
⑦ [3,3,1,1,2,2]          → 成对重复：验证稳定性（相同值的相对次序）
⑧ 1000 个伪随机数        → 覆盖 step = 1,2,4,…,512 共 10 轮，验证倍增不出错
```

第 ⑤ 条容易被忽视：**已有序的输入也必须跑完整个归并**，因为归并会重建指针关系。如果为了「优化」加一个「若已有序则直接返回」的检查，那个检查本身要 O(n) 且容易写错，收益几乎没有。

第 ⑧ 条是关键：`n = 1000` 时 `step` 要跑到 512，共 10 轮。**step 倍增的 bug（比如 `step *= 2` 写成 `step++`，或 `step < n` 写成 `step <= n`）只在这种多轮输入上才暴露。**

---

## 052.2 从暴力到最优：第一个念头为什么错

### 第一步：暴力（抄进数组，`sort` 完再写回）

```text
vals = []
for p in list: vals.append(p.val)
sort(vals)
p = head
for v in vals: p.val = v; p = p.next
```

- 时间 O(n log n)，空间 **O(n)**。
- 它**能 AC**，而且 5 万次排序在 Go/Java 里只要几毫秒。
- 但它有两个问题：① 空间 O(n)，不满足进阶要求；② **它排的是值，不是结点**。若结点带卫星数据（比如「按分数排序的玩家对象」），只排值就彻底错了。

### 第二步：暴力升级（对链表做插入排序）

```text
sorted = null
for each node in list:
    在 sorted 里找到第一个 >= node 的位置，插入
```

- 时间 **O(n²)**，空间 O(1)。
- `n = 5·10⁴` 时约 `1.25·10⁹` 次比较 —— **TLE**。
- 但在「链表基本有序」时它是 O(n)，所以**面试时可以提一句「若数据近乎有序，插入排序反而最优」**，显示你知道取舍。

### 第三步：卡点在哪 —— 快排为什么在链表上不好使

第一反应通常是「快排 O(n log n)，空间 O(1)」。但在链表上：

| 快排要素 | 数组上 | 链表上 |
| --- | --- | --- |
| 选 pivot | `a[mid]` 随机访问 | 只能走一遍找 |
| partition（左右指针往中间夹） | 从两端走 | **不能从右往左**，只能单向 |
| 递归左右两段 | 传下标区间 | 传两个头指针，还要**先把链切成两段** |
| 最坏情况 | 已排序输入退化成 O(n²) | 同样退化，且链表上无法用「三数取中」 |

**结论**：链表上做快排是可行的（用「三链划分：小于 / 等于 / 大于」），但：
1. 需要额外指针，代码复杂；
2. 递归栈 O(log n)（期望），不满足 O(1)；
3. 最坏 O(n²)。

所以这题的「标准答案」不是快排，而是**归并排序** —— 它天然适配链表：**合并两条有序链是 O(1) 空间的（047 已经会了），不需要随机访问。**

### 第四步：归并的第一版（自顶向下，递归）—— 它差在哪

```text
sortList(head):
    if head == null or head.next == null: return head
    用快慢指针找中点，切成两半 a、b
    return merge(sortList(a), sortList(b))
```

- 时间 O(n log n) ✅
- **空间 O(log n)**（递归栈）❌ —— 不满足题目的 O(1) 要求。

### 第五步：灵感的来源 —— 递归在做什么？把它「压平」

递归版的分治结构是：

```
第 1 层：把整条链切成 2 段（每段 n/2）
第 2 层：切成 4 段（每段 n/4）
...
第 log n 层：切成 n 段（每段 1 个）
然后自底向上两两合并回来
```

**关键洞察**：递归栈存在的唯一理由，是「我得先知道怎么切，才能切」。但**如果我们换个方向：从「长度 1 的有序段」开始，主动把相邻两段合并，就不需要「先切」了** —— 因为段的长度是**已知的**（一开始是 1，之后每轮翻倍）。

```
自顶向下：先切到最小，再往上合并（靠递归记住"切到哪了"）
自底向上：从最小开始，直接按"长度 step"两两合并，每轮 step 翻倍
          └─ 不需要记住任何东西，因为 step 是循环变量
```

这就是**自底向上归并（bottom-up merge sort）**。它把递归栈换成了一个 `int step`。

### 第六步：`step` 倍增到底在做什么（本章核心）

```
step = 1：把链看成 [1][1][1][1][1][1]... 每段长度 1，两两合并 → 每段长度 2 且有序
step = 2：把链看成 [2][2][2][2]...        每段长度 2，两两合并 → 每段长度 4 且有序
step = 4：把链看成 [4][4]...              两两合并          → 每段长度 8 且有序
...
step >= n：整条链已经是一个有序段，结束
```

**归纳一下**：

- **不变量**：每一轮开始时，「从链头按 `step` 切分，每一段内部已经有序」。
- **一轮操作**：把相邻两段（长度各 ≤ step）合并成一段（长度 ≤ 2·step）。
- **轮数**：`⌈log₂ n⌉` 轮（step 从 1 到 ≥ n）。`n = 5·10⁴` 时是 16 轮。
- **每轮工作量**：所有段加起来恰好覆盖 n 个结点，每个结点被访问常数次 → O(n)。
- **总时间**：`O(n log n)`。
- **空间**：`step`、`cur`、`tail`、`left`、`right`、`h`、`t` 若干指针 → **O(1)**。

> **一句话记住**：**自底向上归并 = 把「递归的分治树」按层展开成 for 循环，用 `step` 表示「当前层每段的长度」。**

### 另一条路：能不能更简单？直接对链表做「三链快排」？

可以，代码大概 40 行，期望 O(n log n)。但：

- 递归栈 O(log n)，不满足 O(1)；
- 最坏 O(n²)（全相等值时如果分区不当）；
- 和「归并」比，没有任何优势（链表归并天然 O(1) 额外空间）。

**所以本题没有理由用快排。** 但要能在面试里说清「为什么不用」，这也是考点。

---

## 052.3 原理拆解

### 052.3.1 数据结构与算法选型

| 方案 | 时间 | 空间 | 稳定性 | 什么时候会输 |
| --- | --- | --- | --- | --- |
| 抄值到数组 + `sort` + 写回 | O(n log n) | **O(n)** | 取决于库 | 空间不达标；排的是值不是结点 |
| 插入排序（链表原地） | **O(n²)** | O(1) | 稳定 | `n = 5·10⁴` 时 TLE（仅近乎有序时好用） |
| 自顶向下归并（递归） | O(n log n) | **O(log n) 栈** | 稳定 | 空间不达标（这是最常见的「正确答案但不达标」） |
| **自底向上归并（迭代）** ✅ | **O(n log n)** | **O(1)** | **稳定** | 几乎不会输；`step` 与 `split` 的边界要仔细 |
| 三链快排 | 期望 O(n log n) | O(log n) 栈 | **不稳定** | 最坏 O(n²)；空间不达标 |
| 堆排序（先建堆再逐个取） | O(n log n) | O(n) | 不稳定 | 链表不适合建堆（无随机访问） |

**为什么自底向上归并是唯一完全达标**：它是唯一同时满足「O(n log n) 时间 + O(1) 空间 + 稳定」的链表排序。

### 052.3.2 为什么这个做法是对的（正确性论证）

先明确两个辅助函数。

**`split(head, k)`**：从 `head` 起砍下前 `k` 个结点，返回「剩余部分的头」。

```text
p = head
前进 k-1 步（若中途到 null 就停）
if p == null: return null
rest = p.next
p.next = null      // 切断
return rest
```

- **语义**：`head` 起的前 `k` 个（不足则全部）被截下来，`p.next` 置空保证它是一条独立链。

**`merge(a, b)`**：合并两条有序链，返回 `(新头, 新尾)`。核心逻辑与 047 完全一致，唯一区别是**必须返回尾巴**，因为外层要用它继续拼接。

#### 主循环的正确性

**外层不变量**（第 `r` 轮开始前，`step = 2^r`）：

> 从链头开始，把链按每 `step` 个结点一段切开，**每一段内部都已经是升序**。

**基础**（r = 0，`step = 1`）：每段只有 1 个结点，天然有序。成立。

**保持**：在一轮里，`cur` 从头扫到尾，每次取两段 `left`（长度 ≤ step）与 `right`（长度 ≤ step），合并成一段（长度 ≤ 2·step）挂到 `tail` 后面。因为：

- `left` 与 `right` 各自有序（由不变量），
- `merge` 的输出有序（047 的结论），

所以合并结果是一段长度 ≤ 2·step 的有序段，而且**所有段按原顺序被依次处理**，于是「按 2·step 切分，每段有序」成立。∎

**终止**：`step` 每次翻倍，当 `step >= n` 时，整条链只有一段（长度 n），且由不变量它有序。循环条件写成 `step < n` 恰好在这一刻之前停下 —— 此时**最后一次执行的那一轮已经把整条链排好**（因为 step 是 `≥ n/2` 的最后一个 2 的幂，一轮合并后整条链成为单段）。∎

**为什么 `step < n` 而不是 `step <= n`**：当 `step >= n` 时链只有一段，不需要再合并。写成 `<= n` 会多跑一轮空转（结果仍正确，但浪费一轮 O(n)）。

#### `merge` 返回尾巴的必要性

看这一行：

```text
tail.Next = h
tail = t
```

如果 `merge` 只返回头，`tail` 就得自己从头走到尾 —— 每次走 `O(段长)`，一轮下来还是 O(n)（因为段总和是 n），**复杂度不变，但代码要多一个循环，且容易漏**。返回尾巴是纯粹的工程优化。

#### 关于稳定性

`merge` 里用 `a.Val <= b.Val` 取 `a`。当两值相等时先取左段，而左段在输入中本来就在右段之前，所以**相等元素的相对次序不变** → 稳定排序。若写成 `<`，相等时取右段，稳定性被破坏。

> **稳定性在链表排序里不是可选项**：如果结点带其它字段（比如「按分数排序的学生」，同分者要保持原班级顺序），不稳定就会出错。所以面试时主动说「我用 `<=` 保证稳定」是加分项。

### 052.3.3 通用解题思路（可迁移的那一层）

**链表章的统一语言：哑结点 + 指针三件套（prev/cur/next）+ 先存后断。** 052 的主要新东西不是这套语言，而是**「把递归改写成倍增迭代」**这一招：

```
递归版的结构：分成两半 → 递归 → 合并
             ↓ 展开成"层"
第 0 层：n 个长度 1 的有序段
第 1 层：n/2 个长度 2 的有序段
第 r 层：n/2^r 个长度 2^r 的有序段
             ↓ 用循环变量表示"层"
for step = 1; step < n; step *= 2:
    扫描整条链，把每两段合并
```

**这套「倍增」思想的可迁移性极强**，凡是「规模按 2 的幂增长，每轮利用上一轮的结果」都能用：

| 场景 | 倍增的量 | 每轮做什么 |
| --- | --- | --- |
| 自底向上归并排序 | 有序段长度 `step` | 把两段合成一段 |
| [LC 144 快速幂](https://leetcode.cn/problems/powx-n/) | 指数位 | `x^(2^k)` 逐位乘 |
| 后缀数组（SA）构造 | 比较长度 `2^k` | 用上一轮 rank 排序 |
| [LC 148 本题](https://leetcode.cn/problems/sort-list/) | `step` | 合并相邻两段 |
| 倍增求 LCA | 祖先距离 `2^k` | 跳表式上跳 |
| 二进制计数 / 树状数组 | lowbit | 按 2 的幂累加 |

**判断标志**：如果一个问题在「规模翻倍」时能**复用上一规模的结果**，就可以考虑倍增。归并排序正是如此：**长度 2k 的有序段由两个长度 k 的有序段合并而成。**

> **一句话**：**倍增 = 把「分治的深度」变成「循环的次数」。** 它换来的好处是「不需要栈」，代价是「必须能显式地表示当前处理到哪个规模」。

属于同一模型的题：

| 题 | 递归版 | 迭代 / 倍增版 |
| --- | --- | --- |
| [LC 148 排序链表](https://leetcode.cn/problems/sort-list/) | 自顶向下归并 | **自底向上归并**（本章） |
| [LC 912 排序数组](https://leetcode.cn/problems/sort-an-array/) | 归并 / 快排 | 迭代归并（数组上更好写） |
| [LC 23 合并 K 个升序链表](https://leetcode.cn/problems/merge-k-sorted-lists/) | 分治两两合并 | 迭代：每轮把 K 条合成 K/2 条 |
| [LC 144 Pow(x, n)](https://leetcode.cn/problems/powx-n/) | — | 位运算倍增 |
| [LC 206 反转链表](https://leetcode.cn/problems/reverse-linked-list/) | 递归 | 迭代三指针（044，同类改造） |

### 052.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| 要求**稳定**排序 | 已有 | `<=` 已经保证；说明理由即可 |
| 数据**几乎有序** | 归并仍 O(n log n) | 可先 O(n) 检查是否已有序，或改用插入排序（n 小时） |
| **内存放不下**（外部排序） | 需要落盘 | 分块排序 + K 路归并，与本章同一思路，只是「合并」变成「多路并归 + 磁盘读写」 |
| 结点带**卫星数据** | 不能只排值 | 本章方案天然满足（改指针不改值） |
| 要求**降序** | 比较反向 | `<=` 改 `>=`，注意稳定性方向也随之改变 |
| 只要求**部分有序**（前 k 个） | TopK | 用大小为 k 的堆，O(n log k)（031） |
| **双向链表** | 有 `prev` | 合并时两个方向的指针都要改；split 之后也要保证 `prev` 正确 |

---

## 052.4 图解推演

### 052.4.1 三轮全景（`[4,2,1,3]`，n = 4）

```text
初始：4 → 2 → 1 → 3 → ∅

────────── step = 1（每段 1 个）──────────
cur=4, tail=dummy
  ① left=4      split(4,1)  → right=2, 4.next=∅
     right=2    split(2,1)  → cur=1, 2.next=∅
     merge(4,2) → 2 → 4      挂到 tail
   链：2 → 4 → ∅
  ② cur=1
     left=1      split(1,1)  → right=3, 1.next=∅
     right=3     split(3,1)  → cur=∅,  3.next=∅
     merge(1,3) → 1 → 3      挂到 tail（4.next = 1）
   链：2 → 4 → 1 → 3 → ∅

一轮结束：每 2 个一段，段内有序 → [2,4] [1,3]

────────── step = 2（每段 2 个）──────────
cur=2
  ① left=2→4   split(left,2)   → right=1, 4.next=∅
     right=1→3  split(right,2)  → cur=∅,  3.next=∅
     merge(2→4, 1→3) → 1 → 2 → 3 → 4
   链：1 → 2 → 3 → 4 → ∅

step=4，4 < 4 不成立 → 循环结束
结果：1 → 2 → 3 → 4 ✅
```

### 052.4.2 `split` 的两重作用（画清楚这一步）

```text
split(left = "2 → 4 → 1 → 3", k = 2)

  p = 2
  前进 k-1 = 1 步 → p = 4      （循环 i 从 1 到 k-1）
  rest = p.next = 1
  p.next = ∅                    ← 切断！"2 → 4" 成为独立段
  return 1

  结果：
     left  = 2 → 4 → ∅          （长度恰好 k）
     right = 1 → 3 → ∅          （原来的剩余）

  调用者再对自己做一次：
     split(right, k) → 得到 "1 → 3" 和 cur = ∅

  于是这一轮处理的 4 个结点被整齐切成两段，cur 指向下一轮的起点。
```

**注意 `split` 的两个作用**：① 截出长度为 k 的段；② **切断尾巴**（`p.next = null`）。第 ② 个作用极其重要 —— 不切断的话 `merge` 会沿着 `left` 的尾巴一路走到下一段去，把后面的段也吞进来。

### 052.4.3 `merge` 返回尾巴的用途

```text
dummy → （已合并好的部分，tail 指着它的最后一个结点）
                ↓
              tail ──→ 上一次合并结果
                           │
本次 merge 返回 (h, t)：    │
   tail.Next = h  ──────────┘   直接接上，不用找尾巴
   tail = t                       O(1) 拿到新尾巴
```

如果不返回 `t`，就得写 `for tail.Next != nil { tail = tail.Next }`，虽然总复杂度不变（每轮仍是 O(n)），但多一层循环、多一处可能写错的地方。

### 052.4.4 递归版 vs 迭代版（一张图看清换了什么）

```text
递归版（自顶向下）                    迭代版（自底向上）
──────────────────                  ──────────────────
sort(整条)                           for step = 1; step < n; step *= 2
 ├─ 找中点，切两半                      for cur = 头; cur != ∅;
 │   ├─ sort(左半)                         把相邻两段（长度 step）合并
 │   │   ├─ ...                       
 │   │   └─ merge                     
 │   └─ sort(右半)                    
 │       └─ ...                       
 └─ merge(左, 右)                     
                                     
栈深度 O(log n)                       栈深度 O(1)
需要"先切"（因为不知道切点）          不需要切点（段长 = step，已知）
```

> **这张图就是本章的全部价值**：自顶向下与自底向上产生**完全相同**的合并序列，只是「先切到底」和「边扫边切」的区别。**前者需要栈记住切点，后者用 `step` 计算切点。**

---

## 052.5 双语言实现

### 052.5.1 Java

```java
class ListNode {
    int val;
    ListNode next;

    ListNode(int val) {
        this.val = val;
    }
}

class Solution {
    // 自底向上归并：空间 O(1)，无递归栈
    public ListNode sortList(ListNode head) {
        if (head == null || head.next == null) {
            return head;
        }
        int n = 0;
        for (ListNode p = head; p != null; p = p.next) {
            n++;
        }

        ListNode dummy = new ListNode(0);
        dummy.next = head;

        // step 倍增：1, 2, 4, ... < n。每轮结束时，长度 2*step 的段天然有序
        for (int step = 1; step < n; step <<= 1) {
            ListNode cur = dummy.next;
            ListNode tail = dummy;
            while (cur != null) {
                ListNode left = cur;
                ListNode right = split(left, step);  // 切出左段，返回右段头
                cur = split(right, step);            // 切出右段，返回下一轮起点
                ListNode[] merged = mergeSorted(left, right);
                tail.next = merged[0];
                tail = merged[1];
            }
        }
        return dummy.next;
    }

    // 砍下前 n 个结点，返回剩余部分的头
    private ListNode split(ListNode head, int n) {
        for (int i = 1; i < n && head != null; i++) {
            head = head.next;
        }
        if (head == null) {
            return null;
        }
        ListNode rest = head.next;
        head.next = null; // 必须切断，否则 merge 会吞掉后面的段
        return rest;
    }

    // 返回 {新头, 新尾}
    private ListNode[] mergeSorted(ListNode a, ListNode b) {
        ListNode dummy = new ListNode(0);
        ListNode t = dummy;
        while (a != null && b != null) {
            if (a.val <= b.val) { // <= 保稳定
                t.next = a;
                a = a.next;
            } else {
                t.next = b;
                b = b.next;
            }
            t = t.next;
        }
        t.next = (a != null) ? a : b;
        while (t.next != null) { // 走到真正的尾巴，下一段要从这里接
            t = t.next;
        }
        return new ListNode[]{dummy.next, t};
    }

    // 自顶向下归并（对照组）：空间 O(log n) 递归栈
    public ListNode sortListTopDown(ListNode head) {
        if (head == null || head.next == null) {
            return head;
        }
        ListNode slow = head;
        ListNode fast = head.next;
        while (fast != null && fast.next != null) {
            slow = slow.next;
            fast = fast.next.next;
        }
        ListNode mid = slow.next;
        slow.next = null; // 切成两半
        ListNode a = sortListTopDown(head);
        ListNode b = sortListTopDown(mid);
        return mergeSorted(a, b)[0];
    }
}
```

**五处「为什么」**：

1. `step <<= 1` 而不是 `step *= 2`：两者等价，位移更贴合「倍增」语义；**但要注意 `step` 不能是 0 或负数，且 `step < n` 保证不会溢出**（n ≤ 5·10⁴）。
2. `while (cur != null)` 而不是 `while (cur.next != null)`：最后可能只剩一段（长度 ≤ step），也要被 merge 处理（`merge(left, null)` 返回 `left` 本身）。
3. `split(left, step)` 后再 `split(right, step)` 得到 `cur`：**这就是「边扫边切」** —— `cur` 天然指向下一对段的起点，不需要额外记录。
4. `split` 里的 `head.next = null`：切断是必须的，见 052.4.2。
5. `mergeSorted` 返回 `{头, 尾}`：Java 没有多返回值，用数组或 `ListNode[]` 承载；这也是 Java 版比 Go 版多几行的原因。

### 052.5.2 Go

```go
package main

type ListNode struct {
	Val  int
	Next *ListNode
}

// 自底向上归并：step 从 1 倍增到 >= n，每轮把「长度 step 的有序段」两两合并
func sortList(head *ListNode) *ListNode {
	if head == nil || head.Next == nil {
		return head
	}
	n := 0
	for p := head; p != nil; p = p.Next {
		n++
	}
	dummy := &ListNode{Next: head}
	for step := 1; step < n; step <<= 1 {
		cur := dummy.Next
		tail := dummy
		for cur != nil {
			left := cur
			right := split(left, step) // 切出左半段，返回右半段头
			cur = split(right, step)   // 切出右半段，返回下一轮起点
			h, t := mergeSorted(left, right)
			tail.Next = h
			tail = t
		}
	}
	return dummy.Next
}

// split 砍下以 head 开头的前 n 个结点，返回剩余部分的头
func split(head *ListNode, n int) *ListNode {
	for i := 1; i < n && head != nil; i++ {
		head = head.Next
	}
	if head == nil {
		return nil
	}
	rest := head.Next
	head.Next = nil // 必须切断，否则合并会吞掉下一段
	return rest
}

// mergeSorted 合并两条有序链，返回 (新头, 新尾)
func mergeSorted(a, b *ListNode) (head, tail *ListNode) {
	dummy := &ListNode{}
	t := dummy
	for a != nil && b != nil {
		if a.Val <= b.Val { // <= 保稳定
			t.Next = a
			a = a.Next
		} else {
			t.Next = b
			b = b.Next
		}
		t = t.Next
	}
	if a != nil {
		t.Next = a
	} else {
		t.Next = b
	}
	for t.Next != nil { // 走到真正的尾巴，供下一段拼接
		t = t.Next
	}
	return dummy.Next, t
}
```

### 052.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 返回两个值 | 只能 `ListNode[] {h, t}` 或自定义类 | **原生多返回值** `(head, tail)` | Go 版更干净；Java 版每次 merge 多分配一个数组对象（5·10⁴ 个结点会分配 ~5 万个临时数组，GC 压力明显） |
| 空链判断 | `head == null \|\| head.next == null` | `head == nil \|\| head.Next == nil` | 一致 |
| 位移赋值 | `step <<= 1` | `step <<= 1`（Go 也支持） | 一致；也可写 `step *= 2` |
| 短变量声明 | 无 | `h, t := mergeSorted(...)` | Go 直接用多返回值解构 |
| 辅助函数可见性 | `private` 方法 | 顶层函数（小写 = 包内私有） | 命名冲突风险：`split`、`mergeSorted` 这类名字在自建文件里要注意 |
| 递归 | `sortListTopDown` 可留作对照 | 同样可写，但注意栈 | 5·10⁴ 规模下 Java 递归版**可能栈溢出**（默认栈约 1 万层，而这里是 log₂ n ≈ 16 层，其实安全）|

> **关于 Java 的 `new ListNode[]{h, t}`**：这是本题 Java 版唯一「不优雅」的地方。生产代码里可以定义一个 `static class Pair { ListNode head, tail; }` 复用，避免每个段都分配数组。**面试时说一句「这里为了少写一个类用数组凑，生产会给 Pair 加对象池」是加分项。**

### 052.5.4 对拍测试（Go，2 万次随机 + `sort.Ints` 对拍 + 八个边界）

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
	steps := 0
	for p := h; p != nil; p = p.Next {
		out = append(out, p.Val)
		steps++
		if steps > 100000 {
			panic("疑似成环")
		}
	}
	return out
}

func TestSortListRandom(t *testing.T) {
	r := rand.New(rand.NewSource(52))
	for iter := 0; iter < 20000; iter++ {
		n := r.Intn(30) // 含 0 / 1 / 2
		a := make([]int, n)
		for i := range a {
			a[i] = r.Intn(9) - 4 // 值域 [-4, 4]，有负数与大量重复
		}
		want := append([]int{}, a...)
		sort.Ints(want) // 朴素实现对拍

		got := dump(sortList(build(a)))
		if len(got) != len(want) {
			t.Fatalf("iter=%d a=%v got=%v want=%v", iter, a, got, want)
		}
		for i := range got {
			if got[i] != want[i] {
				t.Fatalf("iter=%d a=%v got=%v want=%v", iter, a, got, want)
			}
		}
	}
	// 1000 个元素：覆盖 step = 1,2,4,...,512 共 10 轮
	big := make([]int, 1000)
	for i := range big {
		big[i] = (i*37 + 11) % 1000
	}
	want := append([]int{}, big...)
	sort.Ints(want)
	got := dump(sortList(build(big)))
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("1000 元素失败 at %d: got=%d want=%d", i, got[i], want[i])
		}
	}
	// 八个边界
	cases := []struct{ a, want []int }{
		{nil, nil},
		{[]int{3}, []int{3}},
		{[]int{2, 1}, []int{1, 2}},
		{[]int{4, 4, 4, 4}, []int{4, 4, 4, 4}},
		{[]int{1, 2, 3, 4, 5}, []int{1, 2, 3, 4, 5}},
		{[]int{5, 4, 3, 2, 1}, []int{1, 2, 3, 4, 5}},
		{[]int{3, 3, 1, 1, 2, 2}, []int{1, 1, 2, 2, 3, 3}},
		{[]int{0, -1, 0, -1}, []int{-1, -1, 0, 0}},
	}
	for i, c := range cases {
		got := dump(sortList(build(c.a)))
		if len(got) != len(c.want) {
			t.Fatalf("边界 %d: a=%v got=%v want=%v", i+1, c.a, got, c.want)
		}
		for j := range got {
			if got[j] != c.want[j] {
				t.Fatalf("边界 %d: a=%v got=%v want=%v", i+1, c.a, got, c.want)
			}
		}
	}
}
```

> **两个测试要点**：① `n = 1000` 的用例专门覆盖 `step` 的 10 轮倍增 —— 只测 30 以内的随机数据只能覆盖到 `step = 16`，`step` 写错成 `step++` 也「可能碰巧过」；② `dump` 带步数上限，因为 `split` 忘了切断会导致成环（`merge` 沿着尾巴跑到下一段），此时没有上限会直接挂死。

---

## 052.6 复杂度与常见坑

### 052.6.1 复杂度推导

- **时间 O(n log n)**：
  - 外层 `step` 从 1 倍增到 ≥ n，共 `⌈log₂ n⌉` 轮。`n = 5·10⁴` 时 `⌈log₂ 50000⌉ = 16` 轮。
  - 每轮内层 `while (cur != null)` 把所有结点扫一遍：`split` 走 `2·step` 步 + `merge` 走 `2·step` 步，总计每轮 O(n)。
  - 合计 `O(n log n)`。常数因子约为 4（split 两段 + merge 两条链）。
- **空间 O(1)**：`dummy` 1 个结点 + `cur/tail/left/right` 四个指针 + `mergeSorted` 里的 `dummy`/`t` + Go 的多返回值。**注意：`mergeSorted` 每次调用都 `new` 一个 dummy，但它在返回前就被丢掉，是「每段 1 个临时结点」而非「O(n) 个」，不构成量级上的空间。** 严格实现可以用栈上变量或复用同一个 dummy 避免分配。
- **为什么 `split` 不会让复杂度变 O(n²)**：`split` 走 `min(step, 剩余长度)` 步，一轮所有 `split` 的步数总和 = n。
- **下界**：比较排序的下界是 Ω(n log n)，所以本题时间已达最优。

### 052.6.2 八个真实踩过的坑

1. **`split` 里忘了 `head.next = null`** —— `merge` 会沿着左段尾巴走到右段、再到下一段，把整条链吞进来；表现为「结果变成一段乱序长链」或成环。
2. **`step *= 2` 写成 `step++`** —— 时间复杂度退化成 O(n²)，`n = 5·10⁴` 时 TLE。这个 bug 在 `n ≤ 30` 的随机数据上**完全测不出来**（因为轮数只差几轮，结果仍对），必须用 1000 规模的用例。
3. **`step` 循环写成 `step <= n`** —— 多跑一轮空转（结果仍对），白花 O(n)。
4. **`cur` 更新写错** —— 比如 `cur = right` 而不是 `cur = split(right, step)`，会导致同一段被反复处理（死循环）。
5. **`mergeSorted` 忘了「走到尾巴」** —— 返回的 `t` 不在尾部，`tail = t` 之后下一段会覆盖掉本次结果的一部分，链被截断。
6. **用 `<` 而不是 `<=`** —— 不稳定；在本题（只比数值）结果仍对，但若结点带卫星数据就错。**面试要主动说明用 `<=`。**
7. **递归版当答案提交** —— 自顶向下是 O(log n) 栈，不满足题目进阶要求；`sortListTopDown` 只能作为对照。
8. **`dummy` 每轮复用但忘了重置 `tail`** —— `tail` 必须每轮重置为 `dummy`，否则会接到上一轮的链尾，整条链被拼成环或多段。

### 052.6.3 面试官最爱追问的三个问题

**Q1：为什么自底向上归并的空间是 O(1)，自顶向下就不是？**
> 「自顶向下需要递归栈记住『每层切到哪了』，深度是 log n，所以栈空间 O(log n)。自底向上把「层」变成了循环变量 `step`：段长是显式已知的（1, 2, 4, …），所以不需要记住任何切点，`step` 一个 `int` 就够。两者产生完全相同的合并序列，区别只在于『切点由栈记住』还是『切点由 step 算出来』。」

**Q2：`step` 倍增为什么是 log n 轮，能不能减少？**
> 「每轮段长翻倍，从 1 到 ≥ n 需要 ⌈log₂ n⌉ 轮，这是归并排序的固有结构 —— 每一轮只能保证段长翻倍，因为合并两个长度 k 的有序段最多得到长度 2k 的有序段。想减少轮数就只能一次合并多段（K 路归并），但那样需要 K 个指针或一个堆，空间或常数都会变差。」

**Q3：链表排序为什么不用快排？**
> 「三个原因：① 链表的 partition 无法从两端往中间夹（只有 `next`），只能单向做『三链划分』，代码复杂度高；② 递归栈 O(log n)，不满足 O(1) 空间；③ 快排不稳定且最坏 O(n²)，而链表归并天然 O(1) 额外空间、稳定、最坏也是 O(n log n)。所以在链表上归并严格优于快排。」

---

## 052.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 21 合并两个有序链表](https://leetcode.cn/problems/merge-two-sorted-lists/) | 简单 | 本章 `mergeSorted` 的原型（047） |
| [LC 23 合并 K 个升序链表](https://leetcode.cn/problems/merge-k-sorted-lists/) | 困难 | K 路归并：堆 O(N log K) 或分治两两合并 |
| [LC 912 排序数组](https://leetcode.cn/problems/sort-an-array/) | 中等 | 数组版的归并 / 快排 / 堆排，可与本章对照「有随机访问时归并怎么写」 |
| [LC 147 对链表进行插入排序](https://leetcode.cn/problems/insertion-sort-list/) | 中等 | O(n²) 的链表插入排序，适合小规模或近乎有序 |
| [LC 143 重排链表](https://leetcode.cn/problems/reorder-list/) | 中等 | 也用「找中点」，但不合并而是交替拼接（051） |
| [LC 876 链表的中间结点](https://leetcode.cn/problems/middle-of-the-linked-list/) | 简单 | 自顶向下归并的「切分」步骤原题 |
| [LC 148 的变体：降序排序](https://leetcode.cn/problems/sort-list/) | — | 把 `<=` 改成 `>=`，注意稳定性方向随之反转 |
| [LC 剑指 Offer II 077 链表排序](https://leetcode.cn/problems/7WHec2/) | 中等 | 与本题完全同题，可用于二次练习 |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 23 合并 K 个升序链表**
1. K 条链表，每次都要挑当前最小的头。用什么结构能让「挑最小」变成 O(log K)？
2. 如果不用堆而用「两两合并」，一共要合并多少轮？每轮处理的结点总数是多少？

**LC 912 排序数组**
1. 数组上做自底向上归并时，「切段」还需要 `split` 吗？为什么？
2. 归并时如果反复 `new` 临时数组，空间会变成多少？能不能只分配一次？

**LC 147 对链表进行插入排序**
1. 插入排序在链表上为什么天然是 O(1) 空间（不像数组要后移）？
2. 什么输入会让它退化成 O(n²) 的最坏情况？

**LC 148 的变体（降序）**
1. 把 `<=` 改成 `>=` 之后，「稳定性」的方向变了没有？相等元素现在谁在前？

**通用**
- 只要看到「O(n log n) + O(1) 空间 + 链表」，答案几乎一定是自底向上归并。
- 倍增类算法写完必须用「规模刚好跨过一个 2 的幂」的数据测（比如 1000、1024），否则轮数写错测不出来。

</details>

---

## 附：本章小结（可背走的三句话）

```text
① 自底向上归并 = 把递归的分治树按层展开，用 step 表示"当前层每段长度"
② split 的两件事：截出 k 个结点 + 切断尾巴（不切断 merge 会吞掉下一段）
③ merge 返回尾巴，tail 才能 O(1) 接上；比较用 <= 保证稳定
```

> **下一章**：[`053-copy-list-with-random-pointer.md`](053-copy-list-with-random-pointer.md) —— 从「改结构」换到「造结构」：怎么在一条带随机指针的链上做深拷贝，同时不共享任何结点。
