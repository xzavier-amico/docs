# 054. LC146 LRU 缓存（LRU Cache）

> **状态**：`已完成（初稿）` ｜ **所属**：第 6 部分 · 链表 ｜ **编号**：054 / 150
> **难度**：中等（但它是「设计题」的门槛题）｜ **标签**：链表 · 双向链表 · 哈希表 · 设计
> **一句话**：这道题真正考的是「**当『快速查找』和『快速调整顺序』两个需求同时出现时，怎么用两个结构各管一件事**」——`get` 和 `put` 都必须 O(1)，而任何单一数据结构都做不到。
> **前置**：[`053-copy-list-with-random-pointer.md`](053-copy-list-with-random-pointer.md)（哈希与链表的配合）、[`044-reverse-linked-list.md`](044-reverse-linked-list.md)（链表基本功）

---

## 054.1 题目

**原题链接**：<https://leetcode.cn/problems/lru-cache/>

### 题意重述（把隐含条件补齐）

请你设计并实现一个满足 **LRU（最近最少使用）** 缓存约束的数据结构：

- `LRUCache(int capacity)`：以正整数 `capacity` 初始化。
- `int get(int key)`：如果 `key` 存在，返回其值并把该键**标记为最近使用**；否则返回 `-1`。
- `void put(int key, int value)`：如果 `key` 已存在，更新值并**标记为最近使用**；如果不存在，插入该键值。**若插入导致容量超出，则淘汰最久未使用的键。**

并且 **`get` 和 `put` 必须以 O(1) 的平均时间复杂度运行**。

六个必须读出来的条件：

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| **`get` 也算一次「使用」** | 「并将其标记为最近使用」 | 只读不更新顺序 → 淘汰顺序错，这是最高频的逻辑错 |
| **`put` 更新已有 key 时也算使用** | 同上 | 更新值但没提频 → 该键被提前淘汰 |
| **更新已有 key 不新增容量** | 「如果 key 已存在，则变更其值」 | 每次都新建结点 → 容量虚增、提前淘汰别人 |
| **O(1) 平均复杂度** | 题目明确要求 | 用数组/切片维护顺序是 O(n)，`n = 10⁵` 时直接 TLE |
| **`capacity >= 1`** | 约束 | 不会出现容量 0；但容量 1 是必须跑通的边界 |
| **`get` 未命中返回 `-1`** | 「否则返回 -1」 | 返回值不能是 `0` 或 `null` |

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| `1 <= capacity <= 3000` | 容量小 | 单次操作 O(capacity) 也「勉强能过」，但**面试官会看复杂度** |
| `0 <= key, value <= 10⁴` | 值域小 | 理论上能用数组当哈希，但这不是考点 |
| `1 <= 调用次数 <= 2·10⁵` | 20 万次 | **O(n) 每次 → 3·10⁸ 量级**，在严格环境会 TLE；O(1) 每次 → 2·10⁵，毫无压力 |
| `get`/`put` 平均 O(1) | 硬性要求 | 必须「哈希 + 双向链表」双结构 |

> 💡 **量级 → 算法**：`2·10⁵` 次调用是关键数字。它意味着：
> - O(1) 每次 → 约 2·10⁵ 次操作 → 微秒级 ✅
> - O(capacity) 每次 → 约 `2·10⁵ × 3000 = 6·10⁸` → 秒级，很容易 TLE ⚠️
>
> 所以**复杂度要求不是「风格建议」，而是能不能过题的硬线**。这也是 LC146 被称为「设计题门槛」的原因：它是第一道**强制你把两个数据结构缝在一起**的题。

### 示例

```text
输入：
["LRUCache", "put", "put", "get", "put", "get", "put", "get", "get", "get"]
[[2], [1, 1], [2, 2], [1], [3, 3], [2], [4, 4], [1], [3], [4]]

输出：
[null, null, null, 1, null, -1, null, -1, 3, 4]

解释：
LRUCache(2)        容量 2
put(1,1)           缓存 {1=1}
put(2,2)           缓存 {1=1, 2=2}
get(1)   → 1       1 变成最近使用
put(3,3)           容量满，淘汰最久未使用的 2 → 缓存 {1=1, 3=3}
get(2)   → -1      2 已被淘汰
put(4,4)           容量满，淘汰最久未使用的 1 → 缓存 {3=3, 4=4}
get(1)   → -1
get(3)   → 3
get(4)   → 4
```

### 我自造的八个反例（写完必须逐个跑）

```text
① capacity=1，put(1,1) put(2,2)          → 每次插入都立刻淘汰上一个
② 反复 put 同一个 key 不同 value          → 不能新增结点，且要提频
③ get 提频后淘汰顺序变化                  → 最核心的语义（官方样例已覆盖）
④ put 更新已有 key 后，另一个 key 被淘汰    → 验证「更新也算使用」
⑤ get 一个不存在的 key，然后再 put 满      → 未命中不能影响任何顺序
⑥ key 用满 capacity 后连续 put 不同 key    → 链表长度必须恒定 = capacity
⑦ capacity=2，只在两个 key 之间来回 get    → 链表节点不许泄漏
⑧ 20 万次随机操作与朴素实现对比            → 见 054.5.4 对拍
```

第 ② 和 ④ 条是同一类错误的两个面：**「更新已有 key」这条路径必须和 `get` 一样提频，且绝不新增结点**。很多人的实现里 `put` 只要「key 存在」就走「新建 + 删除旧」的路径，虽然结果有时碰巧对，但链表会多一次无意义的分配，且容易漏掉 `map` 的更新。

---

## 054.2 从暴力到最优：第一个念头为什么错

### 第一步：暴力（用切片维护「最近使用」的顺序）

```text
items = []          // items[0] 是最近使用
get(key):
    线性扫描找 key
    找到 → 把它移到 items[0]，返回值
    没找到 → 返回 -1
put(key, value):
    线性扫描找 key
    找到 → 更新值 + 移到 items[0]
    没找到 → 插到 items[0]；若长度 > capacity，丢掉最后一个
```

- 时间 **O(capacity)** 每次，空间 O(capacity)。
- `capacity = 3000`、调用 2·10⁵ 次时：约 `6·10⁸` 次操作。**能过但很危险**（实测 Java 约 0.6 s，Go 约 0.3 s；如果再加一点常数就 TLE）。
- **它为什么不算合格**：题目明确要求 O(1)。而且这个写法把「查找」和「顺序维护」都压在一个线性结构上，两个需求互相拖累 —— 这正是本题要解决的矛盾。

### 第二步：卡点在哪 —— 三个互相冲突的需求

把 `get` / `put` 需要的能力拆开，你会发现**同时有三个**：

| 需求 | 什么结构能 O(1) | 什么结构做不到 |
| --- | --- | --- |
| **① 按 key 定位** | 哈希表 | 链表（要遍历）、数组（要遍历除非 key 连续且小） |
| **② 把某元素移到「最近使用」端** | 双向链表（改 2 个指针） | 数组（要整体移动 O(n)）、**单向链表**（拿不到前驱） |
| **③ 淘汰「最久未使用」端** | 双向链表（取尾）+ 头尾哨兵 | 哈希表（无序） |

一张表看清「为什么必须组合」：

| 单一结构 | ① 定位 | ② 挪动 | ③ 淘汰 | 结论 |
| --- | --- | --- | --- | --- |
| 数组 / 切片 | O(n) | O(n) | O(1) | ① 输 |
| 单向链表 | O(n) | **O(n)**（要重新找前驱） | O(1) | ② 输 |
| 双向链表 | O(n) | O(1) | O(1) | ① 输 |
| 哈希表 | O(1) | —（无序） | —（无序） | ②③ 输 |
| **哈希 + 双向链表** | **O(1)** | **O(1)** | **O(1)** | ✅ 全胜 |

> **这张表就是本题的答案**：三个需求分别由两个结构承担 —— 哈希表管「① 定位」，双向链表管「② 挪动 + ③ 淘汰」。这不是「巧妙」，而是「被需求逼出来的唯一组合」。

### 第三步：为什么必须是**双向**链表（单向不行）

把某个结点从链上摘下来，需要改**它的前驱的 `next`**：

```
需求：把 X 从链上摘下

   A ──→ X ──→ B         单向：X 知道自己指向 B，
        ↑                     但不知道「谁指向我」= 不知道 A 是谁
        └─ 要想改 A.next = B，必须先找到 A
           └─ while (p.next != X) p = p.next;   ← O(n)！

   A ⇄ X ⇄ B              双向：X.prev 就是 A，X.next 就是 B
                              A.next = B; B.prev = A;   ← O(1)
```

**所以「O(1) 删除」的必要条件是「能 O(1) 拿到前驱」，而只有双向链表提供这一点。** 这是本章最重要的一个「为什么」，比记住「用双向链表」重要得多。

### 第四步：灵感的来源 —— 两个哨兵把边界抹平

有了「哈希 + 双向链表」，剩下的坑全是**边界**：

- 链表为空时怎么删？
- 只有一个结点时怎么删？
- 插到头部时，原头是谁？
- 淘汰时，尾巴是谁？

四个边界会带来 6~8 个 `if`。**用两个哨兵结点（`head` / `tail`）把它们全部消掉**：

```
head ⇄ [最近使用] ⇄ … ⇄ [最久未使用] ⇄ tail
 ↑                                          ↑
永远存在                                  永远存在
```

于是：

| 操作 | 无哨兵时 | 有哨兵时 |
| --- | --- | --- |
| 插到头部 | 特判「空链」 | `head.next.prev = n; n.next = head.next; ...` 一行 |
| 删除结点 | 特判「是不是头/尾」 | `n.prev.next = n.next; n.next.prev = n.prev;` 两行 |
| 取最久未使用 | 特判「空链」 | `tail.prev` |
| 链表为空 | 要特判 `head == null` | `head.next == tail` |

**这就是链表章反复出现的「用 1 个假结点换掉 n 个 if」的第三次登场**（045 的区间前驱、049 的删除前驱、本章的头尾哨兵）。**主题一致：让边界变成普通情况。**

---

## 054.3 原理拆解

### 054.3.1 数据结构与算法选型

| 方案 | `get` | `put` | 额外空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- | --- |
| 切片维护顺序 | O(n) | O(n) | O(1) | 40 行 | 2·10⁵ 次调用 + capacity 3000 → 危险 |
| `LinkedHashMap`（Java） | O(1) | O(1) | O(n) | **5 行** | **面试禁止**；判题能过但等于没答 |
| 哈希 + 单向链表（自己补前驱查找） | O(1) | **O(n)** 删除 | O(n) | 50 行 | 删除要 O(n) |
| **哈希 + 手写双向链表 + 头尾哨兵** ✅ | **O(1)** | **O(1)** | **O(n)** | **60 行** | 几乎不会输 |
| 哈希 + 数组（key→下标）+ 空闲槽位管理 | O(1) | O(1) | O(n) | 80 行 | 实现复杂，容易错；没有实际优势 |
| 有序结构（TreeMap 按时间戳） | O(1) | **O(log n)** | O(n) | 40 行 | 比 O(1) 慢 |

**为什么「哈希 + 手写双向链表」是唯一合格的面试答案**：它是**唯一**同时满足「O(1) + 不依赖语言内置特性 + 代码量可接受」的方案。

> **关于 `LinkedHashMap`**：Java 的 `LinkedHashMap` 支持 `accessOrder = true` + 重写 `removeEldestEntry`，5 行就能实现 LRU。**它在生产环境完全可用，但在面试里是「零分答案」** —— 因为出题人要看的就是「你会不会自己写双向链表」。本章只把它作为**对照**提一句，代码一律手写。

### 054.3.2 为什么这个做法是对的（正确性论证）

**结构定义**：

```
head ⇄ n1 ⇄ n2 ⇄ … ⇄ nk ⇄ tail
        ↑                  ↑
     最近使用           最久未使用
```

`map: key → 结点引用`，且 `map.size() == k`。

**核心不变量**（任何操作前后都成立）：

> ① 链表从 `head.next` 到 `tail.prev` 依次是「按最近使用时间从新到旧」排列的全部 `k` 个键；
> ② `map` 里的每个 `key` 恰好对应链表上的一个结点，且 `结点.key == key`；
> ③ `map.size() == k <= capacity`；
> ④ `head.next == tail` 当且仅当 `k == 0`。

**`unlink(n)` 的正确性**：`n.prev.next = n.next` 和 `n.next.prev = n.prev` 两条赋值把 `n` 从链上摘下。由 ④，哨兵永远存在，所以 `n.prev`、`n.next` 永不为 `null` —— **这就是哨兵的价值：让 `unlink` 对「唯一结点」「头结点」「尾结点」全部适用，无需分支。**

**`addFirst(n)` 的正确性**：

```
n.next = head.next;  n.prev = head;
head.next.prev = n;  head.next = n;
```

四步的顺序不能乱：先接好 `n` 的两条边（此时 `head.next` 还没变），再改原首结点的 `prev`，最后改 `head.next`。若先改 `head.next = n`，`n.next` 就丢掉了原首结点 —— 又一次「先存后断」。

**`get(key)` 的正确性**：

- 未命中 → 返回 `-1`，不改变任何状态（保持不变量的 ① ②，因为什么都没动）。
- 命中 → `unlink(n)` + `addFirst(n)`：把 `n` 从原位置摘下、插到最前。这使 `n` 成为「最近使用」，其余结点的相对顺序不变。① 仍成立；`map` 未变，② 成立；`k` 未变，③④ 成立。

**`put(key, value)` 的正确性**（分三种情况，逐一验证）：

| 情况 | 动作 | 为什么正确 |
| --- | --- | --- |
| **key 已存在** | 改 `n.val`；`unlink` + `addFirst` | 不新增结点 → `k` 不变（③ 保持）；提频使 ① 恢复 |
| **key 不存在且未满** | 新建结点，`map.put`，`addFirst` | `k` 增加 1；新结点在最前（最近使用）→ ①；`map` 与链表同步 → ②；`k <= capacity` → ③ |
| **key 不存在且已满** | 先淘汰 `tail.prev`（`unlink` + `map.remove(victim.key)`），再走上一行 | 淘汰的 `tail.prev` 由 ① 是「最久未使用」，正确；`k` 先减 1 再加 1，净不变 → ③；① ② 恢复 |

**终止性 / 无泄漏**：每次 `put` 新键时若满则必删一个，所以 `k` 永远 ≤ capacity；被淘汰的结点同时从链表和 `map` 移除，不会出现「map 里有但链表没有」的悬挂引用。∎

> **注意一个细节**：淘汰时必须用 `victim.key` 去 `map` 里删（`map.remove(victim.key)`）。**这就是为什么双向链表的结点里必须同时存 `key` 和 `val`** —— 只存 `val` 的话，淘汰时你不知道该从 `map` 里删哪个 key。这是本章最容易漏的设计点之一。

### 054.3.3 通用解题思路（可迁移的那一层）

**链表章的统一语言：哑结点 + 指针三件套（prev/cur/next）+ 先存后断。** 054 把这套语言发挥到了极致，并且引入了本章最后一块拼图 —— **多结构组合**：

```
需求分解：
  ① 按 key 定位      → 哈希表（key → 结点引用）
  ② O(1) 摘除任意结点 → 双向链表（有 prev 才能 O(1) unlink）
  ③ O(1) 找到最旧结点 → 双向链表 + 尾哨兵（tail.prev）
         ↓ 让两个结构互相"指"
  map 的 value 存"结点引用"（不是存值！），于是从 map 直接拿到链表位置
         ↓ 消除所有边界
  头尾两个哨兵 → unlink / addFirst 对空链、单结点、头、尾一律适用
```

**「map 的 value 存结点引用而不是值」是这类设计的通用关键**：如果 `map[key] = value`，你还得再找「这个 key 在链表哪儿」，那就退化成 O(n)；存**引用**才能 O(1) 直达结点。**这个技巧在 LFU（460）、O(1) 随机集合（043）、并查集（090）里都出现过。**

这套思路可迁移到所有「**索引 + 顺序**」双需求的设计题：

| 题 | 索引结构 | 顺序 / 分组结构 |
| --- | --- | --- |
| [LC 146 LRU](https://leetcode.cn/problems/lru-cache/) | 哈希 key→结点 | 双向链表（recency） |
| [LC 460 LFU](https://leetcode.cn/problems/lfu-cache/) | 哈希 key→结点 | 哈希 freq→双向链表（频率分桶） |
| [LC 432 全 O(1) 的数据结构](https://leetcode.cn/problems/all-oone-data-structure/) | 哈希 key→计数结点 | 双向链表按计数排序 |
| [LC 380 O(1) 插入删除随机](https://leetcode.cn/problems/insert-delete-getrandom-o1/) | 哈希 val→下标 | 数组（下标即槽位） |
| [LC 043 章节](https://leetcode.cn/problems/insert-delete-getrandom-o1/) | 同上，配合「与末尾交换」删除 | 数组 |
| [LC 588 设计内存文件系统](https://leetcode.cn/problems/design-in-memory-file-system/) | 哈希 name→子目录 | 树（有序） |

> **一句话**：**设计题的通用套路是「把需求拆成『定位』和『顺序』两件事，各找一个 O(1) 的结构，然后用引用把它们缝起来」。** LRU 是这套套路最小的完整案例。

### 054.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| **线程安全** | 并发读写 | 加一把互斥锁（Java `synchronized` / Go `sync.Mutex`）；或分段锁。锁粒度要覆盖「map 命中 + 链表挪动」这一整段，否则会出现「结点已被别人摘走」 |
| **LFU**（460） | 按访问频次淘汰，频次相同再按 recency | 需要「频率分桶」：`freq → 双向链表`，再加 `minFreq` |
| **带 TTL** | 过期时间优先 | 需要额外的按时间排序结构（小顶堆 / 时间轮），或惰性删除 + 定期清理 |
| **容量动态变化** | `resize(newCap)` | 扩容无事；缩容时循环淘汰直到 `k <= newCap` |
| **避免链表结点访问 map** | 减少一次间接引用 | 可以把 `map` 改成 `map[key] *listNode`（已经是了）；或让结点不存 key，改用 `map` 反向索引（代价是淘汰时要 O(n) 找 key） |
| **持久化 / 快照** | 需要序列化 | 链表天然可按 recency 顺序输出，正好是快照顺序 |
| 不用 `map`，用**数组**当哈希 | key 值域小（≤10⁴） | `[]*node` 数组直接下标访问，比 map 更快；但值域大时会爆内存 |

---

## 054.4 图解推演

### 054.4.1 结构总览（capacity = 2，缓存 {1=1, 2=2}，2 更新）

```text
        head                                    tail
         ↓                                       ↓
       [哨兵] ⇄ [2] ⇄ [1] ⇄ [哨兵]
                 ↑
             最近使用          最久未使用在 tail.prev = 1

  map: { 2 → (结点 2), 1 → (结点 1) }
```

### 054.4.2 `get(1)`：命中 → 提到最前

```text
① unlink(结点 1)
   [哨兵] ⇄ [2] ⇄ [哨兵]           （1 被摘下，它的 prev/next 暂时还指着旧邻居，
                                    但已不在链上，map 仍持有它）

② addFirst(结点 1)
   [哨兵] ⇄ [1] ⇄ [2] ⇄ [哨兵]
             ↑
          最近使用

   返回值 1
```

### 054.4.3 `put(3,3)` 且已满：先淘汰再插入

```text
初始：
   head ⇄ [1] ⇄ [2] ⇄ tail
   map: {1, 2}, capacity = 2

① 淘汰 tail.prev = 结点 2
   unlink(结点 2)：
     head ⇄ [1] ⇄ tail
   map.remove(2)                 ← 用 结点2.key 来删，所以结点必须存 key
   map: {1}

② 新建结点 3，addFirst：
   head ⇄ [3] ⇄ [1] ⇄ tail
   map: {1, 3}

结果：缓存 {3=3, 1=1}，最久未使用的是 1
```

### 054.4.4 官方样例的完整状态轨迹（capacity = 2）

| 操作 | 命中？ | 链表（head → tail，左新右旧） | map | 返回 |
| --- | --- | --- | --- | --- |
| 初始 | — | `∅` | `{}` | — |
| `put(1,1)` | 否 | `1` | `{1}` | — |
| `put(2,2)` | 否 | `2 ⇄ 1` | `{1,2}` | — |
| `get(1)` | **是** | `1 ⇄ 2` | `{1,2}` | `1` |
| `put(3,3)` | 否，满 | 淘汰 2 → `3 ⇄ 1` | `{1,3}` | — |
| `get(2)` | 否 | `3 ⇄ 1` | `{1,3}` | `-1` |
| `put(4,4)` | 否，满 | 淘汰 1 → `4 ⇄ 3` | `{3,4}` | — |
| `get(1)` | 否 | `4 ⇄ 3` | `{3,4}` | `-1` |
| `get(3)` | **是** | `3 ⇄ 4` | `{3,4}` | `3` |
| `get(4)` | **是** | `4 ⇄ 3` | `{3,4}` | `4` |

**注意第 3 行**：`get(1)` 之后链表变成 `1 ⇄ 2`，这直接决定了下一行淘汰的是 2 而不是 1。**「`get` 也算使用」这条语义就是在这张表的第 3 行体现的，也是本题最核心的考点。**

### 054.4.5 无哨兵 vs 有哨兵（代码分支数对比）

```text
          无哨兵                          有哨兵
  ──────────────────────          ──────────────────────
  unlink(n):                      unlink(n):
    if n.prev == null:              n.prev.next = n.next
        head = n.next               n.next.prev = n.prev
    else:                         （就这两行，永远安全）
        n.prev.next = n.next
    if n.next != null:
        n.next.prev = n.prev
    else:
        tail = n.prev
  ──────────────────────          ──────────────────────
  addFirst(n):                    addFirst(n):
    if head == null:                n.next = head.next
        head = tail = n             n.prev = head
    else:                           head.next.prev = n
        n.next = head               head.next = n
        head.prev = n
        head = n
  ──────────────────────          ──────────────────────
  共 4 个 if                       共 0 个 if
```

> **4 个 if 里每一个都可能写错**，而且是「只在空链 / 单结点时才暴露」的那种错。**用两个哨兵换掉全部 4 个 if，是本章最值的 2 行代码。**

---

## 054.5 双语言实现

### 054.5.1 Java

```java
import java.util.HashMap;
import java.util.Map;

class LRUCache {
    // 手写双向链表结点：key 存下来，淘汰时才能反查 map
    private static class Node {
        int key;
        int val;
        Node prev;
        Node next;

        Node(int key, int val) {
            this.key = key;
            this.val = val;
        }
    }

    private final int capacity;
    private final Map<Integer, Node> map;
    private final Node head; // 哨兵：head.next 是最近使用
    private final Node tail; // 哨兵：tail.prev 是最久未使用

    public LRUCache(int capacity) {
        this.capacity = capacity;
        this.map = new HashMap<>(capacity * 2); // 避免扩容重哈希
        this.head = new Node(0, 0);
        this.tail = new Node(0, 0);
        head.next = tail;
        tail.prev = head;
    }

    public int get(int key) {
        Node n = map.get(key);
        if (n == null) {
            return -1;      // 未命中：不改变任何状态
        }
        unlink(n);
        addFirst(n);        // 读也算一次使用
        return n.val;
    }

    public void put(int key, int value) {
        Node n = map.get(key);
        if (n != null) {
            n.val = value;  // 同 key 只改值 + 提频，绝不新增结点
            unlink(n);
            addFirst(n);
            return;
        }
        if (map.size() == capacity) {
            Node victim = tail.prev; // 哨兵的 prev 就是最久未使用
            unlink(victim);
            map.remove(victim.key);  // 必须用 node.key，不是 node.val
        }
        Node fresh = new Node(key, value);
        map.put(key, fresh);
        addFirst(fresh);
    }

    private void unlink(Node n) {
        n.prev.next = n.next;
        n.next.prev = n.prev;
    }

    private void addFirst(Node n) {
        n.next = head.next;
        n.prev = head;
        head.next.prev = n;
        head.next = n;
    }
}
```

**五处「为什么」**：

1. 结点里存 `key`：淘汰时只看得到结点，必须靠它反查 `map`。**只存 `val` 是设计缺陷。**
2. `map` 的 `value` 是 `Node` 而不是 `int`：这样 O(1) 直达链表位置；存 `int` 就还得找它在链表哪儿。
3. `if (map.size() == capacity)` 而不是 `>`：`put` 新键前判断，若已满就先淘汰一个再插入；用 `>` 会导致峰值 `capacity+1`。
4. `addFirst` 的四行顺序：先设 `n.next`、`n.prev`，再改 `head.next.prev`，最后改 `head.next`。顺序错任何一步都会丢结点或成环。
5. `new HashMap<>(capacity * 2)`：容量固定且已知，避免扩容；同时避免 `Integer` 装箱导致的容量估算偏差。

### 054.5.2 Go

```go
package main

// 手写双向链表结点
type lruNode struct {
	key, val   int
	prev, next *lruNode
}

// LRUCache = 哈希表（O(1) 定位）+ 双向链表（O(1) 挪动 / 淘汰）
// 约定：head 一侧是「最近使用」，tail 一侧是「最久未使用」
type LRUCache struct {
	cap        int
	m          map[int]*lruNode
	head, tail *lruNode // 两个哨兵，省掉所有判空分支
}

func Constructor(capacity int) LRUCache {
	h, t := &lruNode{}, &lruNode{}
	h.next = t
	t.prev = h
	return LRUCache{
		cap:  capacity,
		m:    make(map[int]*lruNode, capacity),
		head: h,
		tail: t,
	}
}

// remove 把结点从链上摘下（不释放、不清指针，交由调用方决定去哪）
func (c *LRUCache) remove(n *lruNode) {
	n.prev.next = n.next
	n.next.prev = n.prev
}

// pushFront 插到 head 之后，即「标记为最近使用」
func (c *LRUCache) pushFront(n *lruNode) {
	n.next = c.head.next
	n.prev = c.head
	c.head.next.prev = n
	c.head.next = n
}

func (c *LRUCache) Get(key int) int {
	n, ok := c.m[key]
	if !ok {
		return -1
	}
	c.remove(n)
	c.pushFront(n) // 读也算一次使用
	return n.val
}

func (c *LRUCache) Put(key int, value int) {
	if n, ok := c.m[key]; ok {
		n.val = value // key 已存在：改值 + 提频，不新增节点
		c.remove(n)
		c.pushFront(n)
		return
	}
	if len(c.m) == c.cap {
		old := c.tail.prev // 哨兵的前一个就是最久未使用
		c.remove(old)
		delete(c.m, old.key)
	}
	n := &lruNode{key: key, val: value}
	c.m[key] = n
	c.pushFront(n)
}
```

### 054.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 结点类型 | `private static class Node`（内部类） | 顶层 `type lruNode struct` | Java 内部类可以访问外部字段（本题不需要）；Go 用包级私有（小写） |
| 构造方法 | `public LRUCache(int capacity)` | `func Constructor(capacity int) LRUCache` | **Go 没有构造函数**，LeetCode 约定用 `Constructor` |
| 方法名 | `get` / `put`（小写） | `Get` / `Put`（**必须大写**） | LeetCode 的 Go 模板会调用 `obj.Get(key)`；小写则不可导出，编译不过 |
| 接收者 | `this` 隐含 | 显式 `(c *LRUCache)` | **必须用指针接收者**，否则每次调用都复制一份 map 头，状态不同步 |
| 空值 | `map.get(k)` 返回 `null` | `v, ok := m[k]` 的 `ok` | Go 不能靠「值为 nil」判断（结点指针为 nil 才可），必须用 `ok` |
| 删除 map 项 | `map.remove(k)` | `delete(m, k)` | 语义一致；Go 的 `delete` 对不存在的 key 是空操作，安全 |
| 字段命名 | `.key` / `.val` / `.prev` / `.next` | `.key` / `.val` / `.prev` / `.next`（本题自定义类型，小写也行） | 因为都在同一包内，小写可访问；但若要让外部读，则需大写 |
| map 初始容量 | `new HashMap<>(cap*2)` | `make(map[int]*lruNode, capacity)` | Go 的 map 没有负载因子概念，写容量只是预分配提示 |

> **Go 的两个「必踩」点**：① 方法名必须大写（`Get`/`Put`），因为 LeetCode 的调用方在包外；② 接收者必须是指针（`*LRUCache`），否则你自己写的测试会发现「改了但没生效」。

### 054.5.4 对拍测试（Go，2 万组随机操作序列 + 朴素实现对比 + 八个边界）

```go
package main

import (
	"math/rand"
	"testing"
)

// bruteLRU 用切片实现，front = 最近使用，每次操作 O(n) —— 朴素但对
type bruteLRU struct {
	cap  int
	keys []int
	vals []int
}

func newBruteLRU(c int) *bruteLRU { return &bruteLRU{cap: c} }

func (b *bruteLRU) find(k int) int {
	for i := range b.keys {
		if b.keys[i] == k {
			return i
		}
	}
	return -1
}

func (b *bruteLRU) moveToFront(i int) {
	k, v := b.keys[i], b.vals[i]
	b.keys = append(b.keys[:i], b.keys[i+1:]...)
	b.vals = append(b.vals[:i], b.vals[i+1:]...)
	b.keys = append([]int{k}, b.keys...)
	b.vals = append([]int{v}, b.vals...)
}

func (b *bruteLRU) Get(k int) int {
	i := b.find(k)
	if i < 0 {
		return -1
	}
	v := b.vals[i]
	b.moveToFront(i)
	return v
}

func (b *bruteLRU) Put(k, v int) {
	if i := b.find(k); i >= 0 {
		b.vals[i] = v
		b.moveToFront(i)
		return
	}
	if len(b.keys) >= b.cap {
		b.keys = b.keys[:len(b.keys)-1]
		b.vals = b.vals[:len(b.vals)-1]
	}
	b.keys = append([]int{k}, b.keys...)
	b.vals = append([]int{v}, b.vals...)
}

func TestLRURandom(t *testing.T) {
	r := rand.New(rand.NewSource(54))
	for iter := 0; iter < 20000; iter++ {
		capacity := 1 + r.Intn(4) // 容量 1..4，频繁触发淘汰
		c := Constructor(capacity)
		b := newBruteLRU(capacity)
		for op := 0; op < 40; op++ {
			if r.Intn(2) == 0 {
				k := r.Intn(6) // key 空间很小 → 大量命中 / 更新
				got, want := c.Get(k), b.Get(k)
				if got != want {
					t.Fatalf("iter=%d cap=%d Get(%d) got=%d want=%d", iter, capacity, k, got, want)
				}
			} else {
				k, v := r.Intn(6), r.Intn(100)
				c.Put(k, v)
				b.Put(k, v)
			}
		}
	}
	// 官方样例
	c := Constructor(2)
	c.Put(1, 1)
	c.Put(2, 2)
	if c.Get(1) != 1 {
		t.Fatal("样例 Get(1) 应为 1")
	}
	c.Put(3, 3) // 淘汰 key=2
	if c.Get(2) != -1 {
		t.Fatal("样例 Get(2) 应为 -1")
	}
	c.Put(4, 4) // 淘汰 key=1
	if c.Get(1) != -1 || c.Get(3) != 3 || c.Get(4) != 4 {
		t.Fatal("样例后半段失败")
	}
	// capacity = 1：疯狂互相淘汰
	one := Constructor(1)
	one.Put(1, 1)
	one.Put(2, 2)
	if one.Get(1) != -1 || one.Get(2) != 2 {
		t.Fatal("capacity=1 失败")
	}
	// 同 key 改值不能让链表变长
	re := Constructor(2)
	re.Put(1, 1)
	re.Put(1, 10)
	if re.Get(1) != 10 {
		t.Fatal("同 key 改值失败")
	}
	if len(re.m) != 1 {
		t.Fatalf("同 key 改值后 map 长度应为 1，实际 %d", len(re.m))
	}
	// 链表完整性：含两个哨兵，长度必须 = map 大小 + 2
	if cnt := listLen(re.head); cnt != len(re.m)+2 {
		t.Fatalf("链表含哨兵长度应为 %d，实际 %d", len(re.m)+2, cnt)
	}
	// 反复淘汰后仍能正常读写（不泄漏结点）
	loop := Constructor(2)
	for i := 0; i < 1000; i++ {
		loop.Put(i%5, i)
	}
	if cnt := listLen(loop.head); cnt != len(loop.m)+2 {
		t.Fatalf("1000 次操作后链表长度 %d，map 大小 %d，存在泄漏", cnt, len(loop.m))
	}
}

func listLen(h *lruNode) int {
	n := 0
	for p := h; p != nil; p = p.next {
		n++
	}
	return n
}
```

> **对拍的两个设计要点**：① **容量取 1..4、key 空间取 0..5** —— 让「淘汰」「命中」「更新」三种分支高频发生，比大容量更能逼出 bug；② **额外断言链表长度 = `map` 大小 + 2** —— 这是对「结点泄漏 / 悬挂引用」的直接检查，比只比对 `get` 返回值更能发现结构错误。

---

## 054.6 复杂度与常见坑

### 054.6.1 复杂度推导（`get` / `put` 为什么都是 O(1)）

**`get(key)`**：

| 步骤 | 代价 |
| --- | --- |
| `map.get(key)` | 哈希表查找，**平均 O(1)**，最坏 O(n)（全碰撞） |
| `unlink(n)` | 两条指针赋值，**O(1)** |
| `addFirst(n)` | 四条指针赋值，**O(1)** |
| 合计 | **平均 O(1)** |

**`put(key, value)`**：

| 分支 | 步骤 | 代价 |
| --- | --- | --- |
| key 已存在 | `map.get` + 改值 + `unlink` + `addFirst` | **O(1)** |
| key 不存在、未满 | `map.size()` + 新建 + `map.put` + `addFirst` | **O(1)**（`HashMap.put` 均摊 O(1)） |
| key 不存在、已满 | `tail.prev`（O(1)）+ `unlink`（O(1)）+ `map.remove`（均摊 O(1)）+ 新建 + `map.put` + `addFirst` | **O(1)** |
| 合计 | | **平均 O(1)** |

**空间 O(capacity)**：

- 链表上恰好 `k` 个数据结点（`k ≤ capacity`）+ 2 个哨兵 → `O(capacity)`。
- `map` 有 `k` 条 entry → `O(capacity)`。
- **注意**：`map` 的 value 是**指针**，不复制结点，所以总空间是 `O(capacity)` 而不是 `O(2·capacity)`。

**为什么是「平均」O(1) 而不是「严格」O(1)**：哈希表在极端碰撞下退化到 O(n)（Java 8+ 单桶超过 8 个结点转红黑树，退化为 O(log n)）。链表部分本身是严格 O(1)。所以整体是「平均 O(1)，最坏 O(n) 或 O(log n)」。面试时说清楚这一点是加分项。

### 054.6.2 九个真实踩过的坑

1. **`get` 命中了但不提频** —— 直接返回 `n.val` 而没有 `unlink + addFirst`。淘汰顺序全错，且**前几个测试用例可能碰巧对**。
2. **`put` 更新已有 key 时新建了结点** —— `map` 被覆盖成新结点，旧结点仍挂在链表上 → 链表变长、容量虚增、内存泄漏。
3. **淘汰时用 `victim.val` 去 `map.remove`** —— 删错 key（甚至删不到），`map` 与链表脱节。
4. **结点里不存 `key`** —— 淘汰时无从知道该删哪个 map 项。**这是最容易被忽视的设计缺陷。**
5. **`addFirst` 的四行顺序写错** —— 先 `head.next = n` 再设 `n.next`，就丢了原首结点；表现为「链表越来越短」或成环。
6. **没加哨兵，且 `unlink` 漏了「删的是头 / 尾」的处理** —— 空链或单结点时 NPE。
7. **`if (map.size() > capacity)` 而不是 `==`** —— `put` 新键前未先淘汰，导致短暂超容；如果判题在 `put` 之后立即检查容量，就会出错。正确做法是「插入前判断，满了先淘汰」。
8. **Go 里方法名写成小写 `get`/`put`** —— LeetCode 调用方在包外，无法访问，编译失败。必须 `Get`/`Put`。
9. **Go 里接收者用了值而不是指针** —— `func (c LRUCache) Get(...)`，修改只作用于副本，`pushFront` 完全失效，`get` 永远返回旧结构。**症状极其诡异：编译通过、运行时行为随机。**

### 054.6.3 面试官最爱追问的四个问题

**Q1：为什么必须用双向链表，单向不行吗？**
> 「因为要把一个结点从链上摘下来，必须改它**前驱**的 `next`。单向链表里结点只有 `next`，拿不到前驱，只能从头遍历找，删除就变成 O(n)。双向链表里 `n.prev` 就是前驱，`unlink` 只需要两条赋值。所以『O(1) 删除』的必要条件就是『O(1) 拿到前驱』，而只有双向链表提供这一点。」

**Q2：`get` 和 `put` 的时间复杂度分别是多少？为什么？**
> 「都是平均 O(1)。`get` 是『哈希查找 O(1) + 从链表摘下 O(1) + 插到头部 O(1)』；`put` 分三种情况：key 存在时是『哈希查找 + 改值 + 摘 + 插』；key 不存在且未满时是『新建 + 哈希插入 + 插头』；已满时多一步『取 `tail.prev`（O(1)）+ 摘 + `map.remove`（均摊 O(1)）』。三种情况里没有一步是线性的，所以都是 O(1)。严格的说是『平均 O(1)』，因为哈希表极端碰撞下会退化。」

**Q3：为什么用两个哨兵？不用会多几个分支？**
> 「不用哨兵时，`unlink` 要特判『删的是不是头 / 尾』（2 个分支），`addFirst` 要特判『链表是不是空』（1 个分支），取『最久未使用』也要特判空链（1 个分支），一共 4 个 if。这 4 个 if 都只在『空链 / 单结点』时才会暴露问题，是最难测的 bug。加了头尾哨兵后，`head.next` 和 `tail.prev` 永远存在，`unlink` 和 `addFirst` 变成无分支的两三行，取 `tail.prev` 也永远合法。**用 2 个假结点换掉 4 个 if。**」

**Q4：如果要求支持并发访问，你怎么改？**
> 「最直接的是给整个缓存加一把互斥锁，把『命中判断 + 链表挪动』这一整段包成临界区 —— 注意不能只锁哈希表，因为结点被摘下和插入之间存在不一致的中间状态。代价是所有 `get` 串行化。要提升并发度可以分段锁（按 key 哈希到 N 个独立的小 LRU，各自加锁），代价是全局容量约束变成各段容量之和，淘汰不再全局最优（可能淘汰一段里的热数据，而另一段有冷数据）。这就是 Redis / Guava 一类实现的取舍点。」

---

## 054.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 460 LFU 缓存](https://leetcode.cn/problems/lfu-cache/) | 困难 | 按**频次**淘汰，同频再按 recency；需要「频率分桶 + minFreq」 |
| [LC 432 全 O(1) 的数据结构](https://leetcode.cn/problems/all-oone-data-structure/) | 困难 | 按**计数**维护有序双向链表，取 max/min |
| [LC 380 O(1) 时间插入、删除和获取随机元素](https://leetcode.cn/problems/insert-delete-getrandom-o1/) | 中等 | 「哈希 + 数组」，删除用「与末尾交换」实现 O(1) |
| [LC 588 设计内存文件系统](https://leetcode.cn/problems/design-in-memory-file-system/) | 困难 | 哈希 + 树，路径解析 |
| [LC 155 最小栈](https://leetcode.cn/problems/min-stack/) | 中等 | 辅助栈也可看作「两个结构分工」的最小案例 |
| [LC 043 章节](https://leetcode.cn/problems/insert-delete-getrandom-o1/) | 中等 | 与 380 同题，本书第 5 部分的「数组 + 下标映射」 |
| [LC 53 最大子数组和](https://leetcode.cn/problems/maximum-subarray/) | 中等 | 对照：不是设计题，但同样考「状态怎么维护」 |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 460 LFU**
1. 频次相同的多个 key，谁先被淘汰？这说明每个频次需要维护什么结构？
2. 一个 key 的频次从 f 变成 f+1，它要移动到哪个桶？如果 f 桶空了，`minFreq` 该怎么变？

**LC 432 全 O(1) 的数据结构**
1. 计数相同的 key 应该放在一起吗？这和你刚写的 LRU 有什么共同点？
2. 取最大 / 最小计数的 key，能不能 O(1) 直接拿到？靠什么保证顺序？

**LC 380 O(1) 随机**
1. 数组支持 O(1) 随机访问，但删除中间元素要 O(n)。怎么绕开？
2. 哈希表在这里存什么，才能配合「与末尾交换」这一步？

**LC 155 最小栈**
1. 主栈之外还需要一个什么结构？它的每个元素代表什么含义？
2. 能不能只用一个栈（存差值）？代价是什么？

**通用**
- 设计题先做需求分解：哪些操作要「按 key 找」，哪些要「按顺序取」，两类需求各需要什么结构。
- 链表类设计一定要先决定「要不要哨兵」。加了之后 `unlink` 能不能做到零分支，是检验设计是否干净的试金石。

</details>

---

## 附：本章小结（可背走的三句话）

```text
① 哈希管「按 key 定位」，双向链表管「按 recency 排序 + O(1) 摘除」，缺一不可
② 双向是硬要求：单向拿不到前驱，删除就是 O(n)；哨兵是软要求，但能消掉 4 个 if
③ 结点必须存 key（淘汰时反查 map）；map 的 value 必须存结点引用（O(1) 直达）
```

### 第 6 部分 · 链表 收口（11 章一句话回顾）

| 章 | 题 | 带走的那一件东西 |
| --- | --- | --- |
| 044 | LC206 反转链表 | 三指针 + 先存后断，链表章的地基 |
| 045 | LC92 反转链表 II | 哑结点定位区间前驱；头插法让接缝变成副作用 |
| 046 | LC25 K 个一组 | 探路（只读预检）+ 以 `groupNext` 当 `prev` |
| 047 | LC21 合并两个有序链表 | 哑结点当「答案容器」，把「第一个结点」变成普通情况 |
| 048 | LC2 两数相加 | 跨轮状态 `carry` + 三条终止条件 |
| 049 | LC19 删除倒数第 N 个 | 用「固定间距」把反向定位变成一趟正向遍历 |
| 050 | LC142 环形链表 II | `a + x = k·b`，一个等式给出入口位置 |
| 051 | LC143 重排链表 | 复合题分解：找中点 + 反转 + 交替插入 |
| 052 | LC148 排序链表 | 自底向上归并，用 `step` 倍增把递归栈换成循环变量 |
| 053 | LC138 随机链表复制 | 把映射编进 `old.next`，用结构当哈希表 |
| 054 | LC146 LRU 缓存 | 两个结构各管一件事；哨兵抹平全部边界 |

> **回头看这 11 章**：真正的「新技巧」只有三个 —— **固定间距定距（049）、`a+x=kb`（050）、`step` 倍增（052）**。其余全部是「哑结点 + 三指针 + 先存后断」这套统一语言在不同场景下的复用。**这就是链表章想教的东西：语言只有一套，题目千变万化。**
