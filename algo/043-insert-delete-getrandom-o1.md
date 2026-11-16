# 043. LC380 O(1) 时间插入、删除和获取随机元素（Insert Delete GetRandom O(1)）

> **状态**：`已完成（初稿）` ｜ **所属**：第 5 部分 · 哈希与数组原地变换 ｜ **编号**：043 / 150
> **难度**：中等 ｜ **标签**：设计 · 数组 · 哈希表 · 随机化
> **一句话**：这道题真正考的是「**怎么用一个哈希表把『值 → 下标』补上，让数组的『删中间』变成『删末尾』**」，不是「会用 `Random`」。
> **前置**：[`042-majority-element-ii.md`](042-majority-element-ii.md) —— 前七章都在「读」数组，这一章开始「设计」一个数组。

---

## 043.1 题目

**原题链接**：<https://leetcode.cn/problems/insert-delete-getrandom-o1/>

### 题意重述（把隐含条件补齐）

实现 `RandomizedSet` 类，支持三个操作，且**每个操作的平均时间复杂度为 `O(1)`**：

- `insert(val)`：若 `val` 不在集合中，插入并返回 `true`；否则返回 `false`（**不允许重复**）。
- `remove(val)`：若 `val` 在集合中，删除并返回 `true`；否则返回 `false`。
- `getRandom()`：**等概率**返回集合中的一个元素。调用时保证集合**非空**。

这句话背后有六个必须读出来的约束：

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| **元素互不重复** | 「`insert` 若已存在返回 false」 | 集合语义 ⇒ 可以用「值 → 唯一位置」的映射；若允许重复（381）就要改成「值 → 位置集合」 |
| 三个操作都要求 **平均 `O(1)`** | 题面「平均时间复杂度为 O(1)」 | 删除后「整体前移」是 `O(n)`，直接不合格 |
| `getRandom` 必须**等概率** | 「每个元素被返回的概率应该相同」 | 用「随机挑下标但数组有空洞」会让某些值更容易被选中 |
| `getRandom` 调用时**集合非空** | 题面保证 | 不必处理空集合（但防御性判断仍是好习惯） |
| `-2^31 <= val <= 2^31 - 1` | 约束 | 不能拿值当下标（值域 42 亿）；必须用哈希 |
| 操作次数最多 `2·10^5` | 约束 | `O(n)` 的单次操作在 `2·10^5` 次调用下是 `4·10^10`，必超时 |

> 💡 **本题是整个第 5 部分的收口**：036–042 都在「读」一个给定的数组，043 让你**自己设计**一个数组。
> 而设计的关键，恰好就是把前几章的技巧反过来用：**既然下标能编码值（038/040），那值也能编码下标（本章）。**

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| 调用次数 `<= 2·10^5` | 二十万次 | 单次操作必须是 `O(1)`；`O(log n)` 也能过，但题面要求 `O(1)` |
| 集合内元素最多 `2·10^5` 个 | 二十万个 | 数组和哈希都放得下（Java 约 10 MB，Go 约 6 MB） |
| 值域 `±2^31` | 42 亿 | 值 → 下标必须靠 `HashMap`；但**下标 → 值**可以用数组（下标范围只有 `[0, size)`） |
| 元素互不重复 | 集合语义 | 「值 → 下标」是**函数**（一对一），可以安全地存单个 `int` |

> 💡 **量级 → 算法**：注意这里的关键不对称 ——
> **值域很大（42 亿）但元素个数很少（20 万）**。
> 所以「值 → 下标」必须用哈希（`O(1)` 平均），而「下标 → 值」可以用数组（`O(1)` 最坏）。
> **一个哈希 + 一个数组，各管一个方向**，这就是本题的全部结构。

### 示例

```
输入：
["RandomizedSet", "insert", "remove", "insert", "getRandom", "remove", "insert", "getRandom"]
[[], [1], [2], [2], [], [1], [2], []]

输出：
[null, true, false, true, 2, true, false, 2]

解释：
RandomizedSet set = new RandomizedSet();
set.insert(1);    // true，集合 {1}
set.remove(2);    // false，2 不在集合里
set.insert(2);    // true，集合 {1,2}
set.getRandom();  // 随机返回 1 或 2
set.remove(1);    // true，集合 {2}
set.insert(2);    // false，2 已存在
set.getRandom();  // 只能返回 2
```

### 我自造的六个反例（写完必须拿它们打一遍）

```text
① insert(1), insert(1)          → true, false。重复插入必须返回 false，且不能改变结构
② insert(1), remove(1), insert(1) → 最后一次必须是 true（删干净了，映射里不能有"幽灵条目"）
③ insert(1), insert(2), remove(2)  → 删的正好是末尾元素。此时"把末尾搬到被删位置"是自赋值，
                                     最容易把映射写坏的场景
④ insert(1), insert(2), remove(1)  → 删中间元素：要把 2 搬到下标 0，并更新 pos[2] = 0
⑤ remove(不存在的值)             → false，且数组与映射都不能被改动
⑥ getRandom 在只有 1 个元素时      → 只能返回那一个；写成 rand.nextInt(size+1) 会越界
```

第 ③ 条是最隐蔽的：**删的正好是末尾元素时，`last` 和 `val` 是同一个值**。如果代码顺序写错（先把 `last` 的映射写回去、再删 `val` 的映射），会留下一个指向已删除位置的「幽灵条目」，于是后续 `insert(val)` 返回 `false` —— 数据永远插不进去。

---

## 043.2 从暴力到最优：第一个念头为什么错

### 第一步：三个「第一念头」

```java
// 念头 A：只用一个 ArrayList
List<Integer> list = new ArrayList<>();
// insert: list.contains(val) → O(n) 判重，add → O(1)
// remove: list.remove(Integer.valueOf(val)) → O(n) 查找 + O(n) 前移
// getRandom: list.get(rand.nextInt(list.size())) → O(1)
// 结论：insert/remove 都是 O(n)，不合格

// 念头 B：只用一个 HashSet
Set<Integer> set = new HashSet<>();
// insert: set.add(val) → 均摊 O(1)
// remove: set.remove(val) → 均摊 O(1)
// getRandom: set 没有下标！只能 for 循环走到第 k 个 → O(n)
// 结论：getRandom 是 O(n)，不合格

// 念头 C：HashSet + 每次 getRandom 转数组
set.toArray()[rand.nextInt(size)]  // 每次 O(n) 复制，不合格
```

| 念头 | insert | remove | getRandom | 死在哪 |
| --- | --- | --- | --- | --- |
| A 数组 | `O(n)`（判重） | `O(n)`（前移） | `O(1)` | 前两个 |
| B 哈希集 | `O(1)` | `O(1)` | `O(n)`（无法按下标取） | 第三个 |
| C 哈希 + 临时转数组 | `O(1)` | `O(1)` | `O(n)` | 第三个 |

### 第二步：卡点在哪 —— 三个需求互相打架

```
     insert 要判重      →  需要"值 → 存在性"  →  哈希表最合适
     remove 要定位      →  需要"值 → 位置"    →  哈希表最合适
     getRandom 要等概率 →  需要"位置 → 值"    →  数组最合适（下标就是随机源）
     删除要 O(1)        →  不能整体前移       →  ？？？
```

四个需求里，三个指向哈希、一个指向数组，**偏偏「删除」这一条谁都不满足** —— 数组删中间要前移，哈希不支持下标的等概率采样。

### 第三步：灵感的来源 —— 删「中间」不如删「末尾」

关键观察：**删末尾是 `O(1)` 的**（只需把长度减一）。而「删中间」之所以要 `O(n)`，是因为要保持其余元素的**相对顺序**。

但本题**根本不需要顺序**！集合是无序的。所以：

```
  要删下标 2 的元素：
  ┌────┬────┬────┬────┬────┐
  │ 1  │ 5  │ 3  │ 8  │ 9  │      下标 0..4
  └────┴────┴────┴────┴────┘
              ↑         ↑
            要删       末尾

  做法：把末尾的 9 直接搬过来盖住 3，然后把长度减一
  ┌────┬────┬────┬────┐
  │ 1  │ 5  │ 9  │ 8  │          下标 0..3，3 消失
  └────┴────┴────┴────┘
              ↑
        pos[9] 从 4 改成 2   ← 唯一的簿记工作
```

```
  ┌───────────────────────────────────────────────────────────┐
  │ "删除一个元素" 可以重构成 "把末尾元素搬到它的位置，再砍掉末尾" │
  │ 代价：需要知道"被删元素在哪个下标"  →  这正是哈希表的活      │
  └───────────────────────────────────────────────────────────┘
```

于是结构定了：

```
  nums[]  ：存值，下标连续无空洞 → getRandom 均匀采样的物理基础
  pos{}   ：值 → 下标            → remove/insert 的 O(1) 定位
```

### 第四步：三个操作各自的实现要点

| 操作 | 步骤 | 关键细节 |
| --- | --- | --- |
| `insert(v)` | `pos` 里有 `v` → `false`；否则 `nums.add(v)`、`pos[v] = nums.size()-1` | 新元素永远追加到末尾，下标 = `size-1` |
| `remove(v)` | `i = pos[v]`；把 `nums` 末尾元素 `last` 写到 `nums[i]`；`pos[last] = i`；砍掉末尾；`pos.erase(v)` | **顺序不能错**：`pos.remove(v)` 必须放最后，否则删末尾元素时会把 `v` 的映射写回去 |
| `getRandom()` | `nums.get(rand.nextInt(nums.size()))` | 均匀性来自「`nums` 无空洞」 |

> **这就是本章的核心技巧：「数组 + 下标映射」实现 `O(1)` 删除。**
> 它在工程里有个名字叫 **swap-and-pop**（交换后弹出），是 `std::vector` 的 `remove_if` + `erase` 惯用法、游戏引擎里的「对象池回收」、以及各种「需 O(1) 删除的无序容器」的标准实现。

---

## 043.3 原理拆解

### 043.3.1 数据结构与算法选型

| 方案 | insert | remove | getRandom | 空间 | 什么时候会输 |
| --- | --- | --- | --- | --- | --- |
| 单数组（`ArrayList`） | `O(n)` | `O(n)` | `O(1)` | `O(n)` | 元素多时 insert/remove 直接崩 |
| 单哈希（`HashSet`） | `O(1)` | `O(1)` | `O(n)` | `O(n)` | `getRandom` 是高频操作时 |
| **数组 + 哈希（swap-and-pop）** ✅ | **`O(1)`** | **`O(1)`** | **`O(1)`** | `O(n)` | 元素很大时「搬动」有常数代价；允许重复时要改结构（381） |
| 双向链表 + 哈希 | `O(1)` | `O(1)` | **`O(n)`** | `O(n)` | 链表不支持按下标随机访问 → `getRandom` 退化 |
| 平衡树 / 有序结构 | `O(log n)` | `O(log n)` | `O(log n)`（按 rank 取） | `O(n)` | 题面要求 `O(1)`；但**支持范围随机**与有序遍历 |
| 跳表 + 下标索引 | `O(log n)` | `O(log n)` | `O(log n)` | `O(n)` | 同上 |

**为什么「数组 + 哈希」是唯一能三个 `O(1)` 的组合？**

- 「随机访问第 k 个」这件事**只有数组能做 `O(1)`**（内存连续 + 下标寻址）。链表做不到。
- 「按值定位」这件事**哈希最快**。
- 「删中间」这件事**数组最怕**，但 swap-and-pop 把它转化成「删末尾」。
- 三者拼起来，正好凑齐。

### 043.3.2 为什么这个做法是对的（正确性论证）

维护一条核心不变量：

> **不变量 I**：`nums` 中无重复值；且对每个 `0 ≤ i < nums.size()`，都有 `pos[nums[i]] = i`；且 `pos` 的键集合恰好等于 `nums` 的值集合。

也就是说，`pos` 与 `nums` 互为**逆映射**，两者严格同步。

**三个操作各自保持不变量 I**：

1. **`insert(v)`**：
   - 若 `v ∈ pos`，由 I 知 `v` 已在 `nums` 中，返回 `false`，结构未变，I 保持。
   - 否则把 `v` 追加到 `nums` 末尾（下标 `s = size`），令 `pos[v] = s`。`v` 原本不在 `nums` 中（否则 `v ∈ pos`），所以未引入重复；新下标的映射已写入；已有的 `pos[nums[i]] = i` 未被触碰。I 保持。
2. **`remove(v)`**：设 `v = nums[i]`（由 I，`pos[v] = i`），设末尾元素 `last = nums[s-1]`。
   - `nums[i] ← last`，同时 `pos[last] ← i`。这两步一起，把 `last` 的映射从 `s-1` 平移到 `i`，仍然满足「`pos[nums[i]] = i`」。
   - 砍掉末尾（`size ← s-1`）：`last` 在下标 `s-1` 的旧位置不再存在。
   - `pos.erase(v)`：`v` 已不在 `nums` 中，映射必须一并删除，否则 I 的第三句被破坏（出现「幽灵键」）。
   - 特殊情况 `i == s-1`（删的就是末尾）时：`last == v`，第一步是自赋值、第二步把 `pos[v]` 重新写成 `i`，最后 `pos.erase(v)` 把它删掉 —— 结果正确，且**依赖「`erase(v)` 放最后」**这个顺序。
3. **`getRandom()`**：`nums` 的下标范围是 `[0, s)`，由 I 无空洞、无重复，`rand.nextInt(s)` 等概率取到每个下标，因此每个元素被返回的概率都是 `1/s`。∎

**推论**：三个操作都只做常数次哈希操作与数组操作，平均（哈希）或最坏（数组）`O(1)`。∎

> **不变量 I 是这类「双结构同步」题的通用武器**。凡是「一个数组 + 一张表，两边都要维护同一个事实」的设计，都可以用「互为逆映射」这一句把正确性钉死。
> 它同时告诉你**哪些代码顺序是错的**：任何会让「`nums` 里有的值在 `pos` 里没有」或「`pos` 里有的键在 `nums` 里没有」的中间状态，都是 bug。

### 043.3.3 通用解题思路（可迁移的那一层）

本题是 **「用一张索引表把『随机访问容器』的删除代价从 O(n) 摊成 O(1)」** 这个模型的母题：

```
原始需求：一个容器，要同时支持
         ① 按值增删（哈希擅长）
         ② 按下标随机取（数组擅长）
         ③ 删除 O(1)（数组不擅长）
         ↓ 观察：容器无序 ⇒ 顺序不重要 ⇒ 可以让"末尾元素"顶替被删元素
         ↓ 补一个哈希表：值 → 下标（这是数组唯一缺的信息）
         ↓ 删除 = 末尾覆盖 + 砍尾 + 改一处映射（swap-and-pop）
代价：    O(n) 空间（哈希表），换来三个 O(1)
```

**「swap-and-pop」的通用形态**：

| 要素 | 说明 | 变体 |
| --- | --- | --- |
| 无序容器 | 顺序无所谓，才能用末尾顶替 | 若要有序（如 LRU 的访问顺序），就不能这么删 |
| 位置索引 | 有办法 `O(1)` 找到「要删的在哪儿」 | 哈希 / 数组下标 / 双向链表结点指针 |
| 末尾可删 | 末尾删除是 `O(1)` | 若末尾也删不掉（如固定容量池），改用空闲链表 |

归属同一模型的题：

| 题 | 需要 `O(1)` 的操作 | 关键差异 |
| --- | --- | --- |
| [LC 380 O(1) 插入删除获取随机](https://leetcode.cn/problems/insert-delete-getrandom-o1/) | 增 / 删 / 随机取 | 数组 + `值→下标` |
| [LC 381 O(1) 插入删除获取随机 - 允许重复](https://leetcode.cn/problems/insert-delete-getrandom-o1-duplicates-allowed/) | 同上，但元素可重复 | `值 → 下标集合`，删的时候从集合里挑一个位置 swap-and-pop |
| [LC 146 LRU 缓存](https://leetcode.cn/problems/lru-cache/) | 查 / 插 / 按访问序淘汰 | 哈希 + **双向链表**（因为需要「有序」，不能 swap-and-pop） |
| [LC 460 LFU 缓存](https://leetcode.cn/problems/lfu-cache/) | 查 / 插 / 按频次淘汰 | 哈希 + 频次桶 + 链表 |
| [LC 710 黑名单中的随机数](https://leetcode.cn/problems/random-pick-with-blacklist/) | 等概率取「不在黑名单」的数 | 白名单映射就是用 swap 思想把黑名单值"挤"到末尾 |
| [LC 519 随机翻转矩阵](https://leetcode.cn/problems/random-flip-matrix/) | 等概率取未翻转的格子 | 一维化 + swap 到末尾，与本章同构 |
| [LC 355 设计推特](https://leetcode.cn/problems/design-twitter/) | 发推 / 取最近 10 条 | 哈希 + 链表 + 堆 |
| [LC 895 最大频率栈](https://leetcode.cn/problems/maximum-frequency-stack/) | 按频次弹栈 | 哈希 + 每频次一个栈 |

### 043.3.4 可扩展性（面试追问的方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| **允许重复元素**（381） | `insert` 永远成功 | `pos: 值 → 下标集合`（Java `Map<Integer, Set<Integer>>`）。删除时从集合里任取一个下标做 swap-and-pop，注意**被搬过来的 `last` 也是同一个值时**要把集合里的 `i` 换成 `s-1` |
| 要求 `getRandom` **按权重** | 非均匀随机 | 前缀和 + 二分（`O(log n)`）；或用「别名法」（`O(1)` 预处理后 `O(1)` 采样） |
| 要求 `getRandom` 取 **k 个不重复** | 一次取多个 | 部分 Fisher-Yates：只洗前 k 个，`O(k)`；洗的时候交换的是下标，需要同步 `pos` |
| 元素是**大对象** | 搬动成本高 | 数组里存「句柄 / 指针」而不是对象本身；swap-and-pop 只搬 8 字节 |
| 需要**遍历顺序稳定** | 顺序有了语义 | 不能 swap-and-pop；改用「墓碑标记 + 定期压缩」或双向链表 + 哈希（LRU 的做法） |
| **多线程**并发调用 | 有数据竞争 | 粗粒度锁最快（临界区短）；细粒度要保证「数组 + 映射」两次写在同一把锁里，否则中间态会被别的线程看见 |
| 内存要**可回收** | 长期运行的服务 | 数组缩容（`size < cap/4` 时减半）；Go 里截断前把被删槽置零，避免底层数组持有引用 |

---

## 043.4 图解推演

### 043.4.1 两种删除方式的对比

```
         要删下标 2 的元素（值为 3）

 方式 A：前移（保序）— O(n)
 ┌────┬────┬────┬────┬────┐
 │ 1  │ 5  │ 3  │ 8  │ 9  │
 └────┴────┴────┴────┴────┘
              ✗ 删除
 ┌────┬────┬────┬────┐
 │ 1  │ 5  │ 8  │ 9  │     8 前移到 2，9 前移到 3 → 4 次写
 └────┴────┴────┴────┘
              ↑
        所有后继都要动 → O(n)

 方式 B：末尾顶替（无序）— O(1)
 ┌────┬────┬────┬────┬────┐
 │ 1  │ 5  │ 3  │ 8  │ 9  │
 └────┴────┴────┴────┴────┘
              ↑         ↑
             i=2      s-1=4
              │         │
              └────┬────┘
                   ▼
          nums[2] ← nums[4] = 9
 ┌────┬────┬────┬────┬────┐
 │ 1  │ 5  │ 9  │ 8  │ 9  │   ← 末尾还是 9，稍后砍掉
 └────┴────┴────┴────┴────┘
              ↑
        pos[9]：4 → 2   （只改一处映射）
                   │
                   ▼
 ┌────┬────┬────┬────┐
 │ 1  │ 5  │ 9  │ 8  │   size ← 4，末尾被"砍掉"
 └────┴────┴────┴────┘
        pos.erase(3)    ← 最后再删被删值的映射
```

### 043.4.2 完整操作轨迹

初始：`nums = []`，`pos = {}`

| # | 操作 | `pos[v]` 查询 | 数组操作 | 映射操作 | 结果 | `nums` / `pos` |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `insert(1)` | 无 | 追加 → 下标 0 | `pos[1]=0` | `true` | `[1]` / `{1:0}` |
| 2 | `remove(2)` | 无 | — | — | `false` | 不变 |
| 3 | `insert(2)` | 无 | 追加 → 下标 1 | `pos[2]=1` | `true` | `[1,2]` / `{1:0,2:1}` |
| 4 | `getRandom()` | — | `rand(2)` | — | 1 或 2 | 不变 |
| 5 | `remove(1)` | `pos[1]=0` | 末尾 2 搬到 0 | `pos[2]=0`；`erase(1)` | `true` | `[2]` / `{2:0}` |
| 6 | `insert(2)` | 存在（0） | — | — | `false` | 不变 |
| 7 | `getRandom()` | — | `rand(1)=0` | — | `2` | 不变 |

### 043.4.3 删末尾元素时，顺序错一步就出「幽灵条目」

```
状态：nums = [1, 2]，pos = {1:0, 2:1}      现在 remove(2)，即删末尾

❌ 错误顺序：先 erase(v)，再写 pos[last]
   nums[1] ← last = 2          → nums = [1, 2]（自赋值）
   pos.erase(2)                → pos = {1:0}
   pos[2] = 1                  → pos = {1:0, 2:1}   ← 幽灵！
   size ← 1                    → nums = [1]
   
   结果：nums 里没有 2，但 pos 里有 2 → 之后 insert(2) 返回 false，永远插不进去

✅ 正确顺序：先搬值 + 写 pos[last]，最后 erase(v)
   nums[1] ← last = 2          → nums = [1, 2]
   pos[2] = 1                  → 仍然是 1（自赋值，无害）
   size ← 1                    → nums = [1]
   pos.erase(2)                → pos = {1:0}   ✅ 干净
```

> **这就是「不变量 I」的实战价值**：它规定了「任何时刻 `pos` 的键集合 = `nums` 的值集合」。
> 一旦你先写 `pos[last]` 再 `erase(v)`，在 `last == v` 的那一刻就违反了它 —— 而违反的方式是**静默的**（不报错、不崩溃，只是数据悄悄不一致）。

### 043.4.4 三条设计路线的对比

```
                    ┌──────────── 需求：增 / 删 / 随机取 都 O(1) ────────────┐
                    │                                                        │
   ┌────────────────▼─────────────┐  ┌──────────────────────▼──────────────┐
   │ 路线 A：数组 + 哈希（swap）  │  │ 路线 B：双向链表 + 哈希            │
   │ O(1) / O(1) / O(1)          │  │ O(1) / O(1) / O(n)                 │
   └────────────────┬─────────────┘  └──────────────────┬──────────────────┘
                    │                                   │
   下标 → 值 O(1)，随机采样天然均匀        结点 → 前驱后继 O(1)，但没有"第 k 个"
                    │                                   │
   ✅ 满足全部三条                      ✅ 保序（LRU 场景）
   ❌ 顺序被打乱                        ❌ getRandom 退化成遍历
                    │                                   │
            【本题选择 A】                      【LC 146 LRU 选择 B】
```

> **这张图解释了「数据结构选择」的本质**：不是「哪个更高级」，而是「**你需要的操作和它擅长的事情是否对齐**」。
> 一旦需求里出现「随机访问」，链表就出局；一旦出现「有序遍历 / 按序淘汰」，swap-and-pop 就出局。

---

## 043.5 双语言实现

### 043.5.1 Java

```java
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ThreadLocalRandom;

class RandomizedSet {
    // nums：存值，下标连续、无空洞 —— getRandom 均匀采样的物理基础
    private final List<Integer> nums;
    // pos：值 → 下标。这是数组唯一缺的那条信息，也是 O(1) 删除的关键
    private final Map<Integer, Integer> pos;

    public RandomizedSet() {
        nums = new ArrayList<>();
        pos = new HashMap<>();
    }

    public boolean insert(int val) {
        Integer i = pos.get(val);
        if (i != null) {
            return false;                 // 已存在，集合语义：不重复插入
        }
        nums.add(val);                    // 新元素永远追加到末尾
        pos.put(val, nums.size() - 1);    // 映射与数组同步写入
        return true;
    }

    public boolean remove(int val) {
        Integer i = pos.get(val);
        if (i == null) {
            return false;                 // 不存在，不能改动任何结构
        }
        int last = nums.get(nums.size() - 1);
        nums.set(i, last);                // ① 末尾元素顶替被删位置
        pos.put(last, i);                 // ② 同步它的下标（自赋值也安全）
        nums.remove(nums.size() - 1);     // ③ 砍掉末尾 —— 注意这里是 remove(int index)，
                                          //    传 int 走下标版；若要按值删必须 remove(Integer.valueOf(v))
        pos.remove(val);                  // ④ 必须最后删：删末尾元素时 last == val，
                                          //    若先删会把 val 的映射重新写回去（幽灵条目）
        return true;
    }

    public int getRandom() {
        // nums 无空洞 ⇒ 均匀取下标 = 均匀取元素
        // ThreadLocalRandom 比 new Random() 快，且避免多线程争用同一个种子序列
        return nums.get(ThreadLocalRandom.current().nextInt(nums.size()));
    }
}
```

### 043.5.2 Go

```go
package main

import "math/rand"

type RandomizedSet struct {
	nums []int
	pos  map[int]int // 值 → 下标
}

func Constructor() RandomizedSet {
	return RandomizedSet{
		nums: make([]int, 0, 16),
		pos:  make(map[int]int, 16),
	}
}

func (s *RandomizedSet) Insert(val int) bool {
	// 注意接收者是 *RandomizedSet（指针）：值接收者会复制整个结构体，
	// 对 nums/pos 的修改只作用在副本上 —— 这是 Go 设计题最常见的"静默无效"bug
	if _, ok := s.pos[val]; ok {
		return false
	}
	s.nums = append(s.nums, val)
	s.pos[val] = len(s.nums) - 1
	return true
}

func (s *RandomizedSet) Remove(val int) bool {
	i, ok := s.pos[val]
	if !ok {
		return false
	}
	last := s.nums[len(s.nums)-1]
	s.nums[i] = last // ① 末尾顶替
	s.pos[last] = i  // ② 同步下标（val == last 时是自赋值）
	// ③ 砍掉末尾。先置零再截断：让底层数组不再持有该槽位的值，
	//    对那些"值是会阻止 GC 的引用类型"的泛型版本很重要，int 场景只是好习惯
	s.nums[len(s.nums)-1] = 0
	s.nums = s.nums[:len(s.nums)-1]
	// ④ 最后删映射：删末尾元素时 last == val，顺序错会留下幽灵键
	delete(s.pos, val)
	return true
}

func (s *RandomizedSet) GetRandom() int {
	return s.nums[rand.Intn(len(s.nums))]
}
```

### 043.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 「方法改状态」 | 方法天然改 `this` | **必须用指针接收者** `(s *RandomizedSet)` | Go 用值接收者时 `insert` 静默无效（编译通过、结果全错），是本语言最危险的坑 |
| 「值不存在」的表示 | `pos.get(v)` 返回 `null` | `i, ok := pos[v]` 的 `ok` | **下标 0 是合法值**，绝不能用「返回 0 表示不存在」 |
| 按下标删除 | `list.remove(int index)` 走下标版；`remove(Object)` 走值版 | 切片截断 `s.nums = s.nums[:n-1]` | Java 的重载陷阱：`remove(nums.size()-1)` 里 `size()-1` 是 `int`，走下标签删 —— 正好是我们要的；写成 `remove(last)` 就变成按值删，`O(n)` 且删错元素 |
| 底层数组残留 | `ArrayList` 内部会把被删槽位置 `null`（引用类型） | 截断不会清零，需手动置零 | Go 的 `nums` 截断后底层数组尾部仍持旧值，长期运行的服务要注意内存 |
| 随机数 | `ThreadLocalRandom.current()` | `math/rand`（Go 1.20 前需手动 `rand.Seed`） | Go 旧版本不设种子会得到**确定的**序列，测试里可复现，生产里要用 `rand.New(rand.NewSource(...))` |
| 容量预分配 | `new ArrayList<>()` 会扩容 | `make([]int, 0, 16)` 显式给容量 | 二者都不影响渐复杂度；20 万次插入时能省若干次扩容拷贝 |

### 043.5.4 对拍测试（随机操作 vs 参考模型 + 均匀性检验）

```go
package main

import (
	"math/rand"
	"testing"
)

// 参考模型：只用一个 map[int]bool 记录集合（不做随机取），
// 用来校验 insert / remove 的返回值与集合内容是否始终一致。
type refSet struct {
	m map[int]bool
}

func newRefSet() *refSet { return &refSet{m: map[int]bool{}} }

func (r *refSet) insert(v int) bool {
	if r.m[v] {
		return false
	}
	r.m[v] = true
	return true
}

func (r *refSet) remove(v int) bool {
	if !r.m[v] {
		return false
	}
	delete(r.m, v)
	return true
}

func TestRandomizedSetAgainstRef(t *testing.T) {
	r := rand.New(rand.NewSource(380))
	for iter := 0; iter < 2000; iter++ {
		set := Constructor()
		ref := newRefSet()

		for step := 0; step < 60; step++ {
			// 值域只取 -3..3：插入/删除频繁撞车，最容易暴露"幽灵条目"
			v := r.Intn(7) - 3
			switch r.Intn(3) {
			case 0:
				if got, want := set.Insert(v), ref.insert(v); got != want {
					t.Fatalf("insert(%d) got=%v want=%v", v, got, want)
				}
			case 1:
				if got, want := set.Remove(v), ref.remove(v); got != want {
					t.Fatalf("remove(%d) got=%v want=%v", v, got, want)
				}
			default:
				if len(ref.m) == 0 {
					continue
				}
				got := set.GetRandom()
				if !ref.m[got] {
					t.Fatalf("getRandom() 返回了集合里不存在的值 %d", got)
				}
			}
			// 每一轮都核对「数组 + 映射」与参考集合完全一致：
			// 数组内容 = 参考集合，且映射与数组互为逆映射（这是本题的核心不变量）
			if len(set.nums) != len(ref.m) {
				t.Fatalf("长度不一致：nums=%v ref=%v", set.nums, ref.m)
			}
			for i, val := range set.nums {
				if !ref.m[val] {
					t.Fatalf("nums 里有集合外的值 %d", val)
				}
				if set.pos[val] != i {
					t.Fatalf("逆映射被破坏：pos[%d]=%d 但它在下标 %d", val, set.pos[val], i)
				}
			}
			if len(set.pos) != len(set.nums) {
				t.Fatalf("pos 与 nums 不同步：pos=%v nums=%v", set.pos, set.nums)
			}
		}
	}
}

// 均匀性检验：插入 10 个值，取 10 万次随机，每个值的频次都应接近 1/10
func TestGetRandomUniform(t *testing.T) {
	set := Constructor()
	for v := 0; v < 10; v++ {
		set.Insert(v)
	}
	const trials = 100000
	cnt := make([]int, 10)
	for i := 0; i < trials; i++ {
		cnt[set.GetRandom()]++
	}
	for v := 0; v < 10; v++ {
		// 期望 10000，允许 ±5% 的统计波动（10 万次采样下标准差约 95，5% 非常宽松）
		if cnt[v] < 9500 || cnt[v] > 10500 {
			t.Fatalf("值 %d 出现 %d 次，偏离均值 10000 太多：%v", v, cnt[v], cnt)
		}
	}
}

// 「删干净再插回来」：专测幽灵条目
func TestInsertAfterRemove(t *testing.T) {
	set := Constructor()
	if !set.Insert(1) {
		t.Fatal("首次插入应为 true")
	}
	if !set.Remove(1) {
		t.Fatal("删除已存在的值应为 true")
	}
	if !set.Insert(1) {
		t.Fatal("删干净后应能重新插入（说明映射里残留了幽灵条目）")
	}
	// 删末尾元素的情形
	set = Constructor()
	set.Insert(1)
	set.Insert(2)
	if !set.Remove(2) {
		t.Fatal("删末尾应成功")
	}
	if set.Insert(2) != true {
		t.Fatal("删末尾后重新插入 2 应成功")
	}
}
```

实测：2000 组 × 60 步随机操作（值域只有 `-3..3`，插入删除高度冲突）与参考模型逐轮核对通过；10 万次采样的均匀性检验通过；幽灵条目专项测试通过。

> **为什么测试里每一轮都要核对「逆映射」**：只看返回值对不够 ——「幽灵条目」这类 bug 会让 `insert` 在某次之后**永远返回 false**，但如果不检查 `pos` 与 `nums` 的长度关系，测试会一直通过到你某次真的去 `insert` 那个值为止。
> **测「不变量」，不要只测「输出」。**

---

## 043.6 复杂度与常见坑

### 043.6.1 复杂度推导

- **`insert`**：一次 `HashMap.get`（平均 `O(1)`）+ 一次 `ArrayList` 追加（均摊 `O(1)`，扩容时 `O(n)` 但均摊后是 `O(1)`）+ 一次 `put`（平均 `O(1)`）→ **平均 `O(1)`**。
- **`remove`**：一次 `get` + 常数次数组写 + 一次 `put` + 一次 `remove` → **平均 `O(1)`**。注意**没有**任何「前移」，这正是 swap-and-pop 的价值。
- **`getRandom`**：一次 `nextInt` + 一次 `get(index)`（`ArrayList` 是 `O(1)` 的数组寻址）→ **最坏 `O(1)`**（不含哈希）。
- **空间 `O(n)`**：`nums` 存 `n` 个值，`pos` 存 `n` 个键值对。Java 的 `HashMap` 有 0.75 装载因子（实际容量约 `1.33n`），Go 的 map 每桶 8 键；20 万元素时 Java 约 8~10 MB，Go 约 5~6 MB。
- **「平均」二字考究**：哈希操作的 `O(1)` 是**平均**（依赖哈希函数分散），数组操作是**最坏**。面试里被问「真的 O(1) 吗」，要答「哈希部分是平均，最坏 `O(n)`（全部冲突到同一个桶）」。

### 043.6.2 九个真实踩过的坑

1. **`pos.remove(val)` 写在 `pos.put(last, i)` 之前** —— 删末尾元素时 `last == val`，会把已删值的映射写回去，形成幽灵条目。**必须最后删。**
2. **忘记 `pos.put(last, i)`** —— 值搬走了但映射还指着旧下标 `s-1`，之后 `remove(last)` 会去删错位置，静默破坏数据。
3. **Java 的 `list.remove` 重载陷阱** —— `remove(int)` 删下标、`remove(Object)` 删值。写成 `nums.remove(last)` 会按值删（`O(n)` 且如果值重复会删错）。本题应显式写 `nums.remove(nums.size() - 1)`。
4. **Go 用值接收者** —— `func (s RandomizedSet) Insert(...)` 编译通过、逻辑正确，但所有修改都作用在副本上，`insert` 永远返回 true 而集合永远为空。**必须用 `*RandomizedSet`。**
5. **`getRandom` 的随机上界写成 `size+1` 或 `size-1`** —— 前者越界，后者漏掉最后一个元素（均匀性被破坏）。
6. **`getRandom` 每次调用都 `new Random()`** —— 若在同一毫秒内连续 new，可能拿到相同种子，随机性变差且慢。应把 `Random` 作为字段（Java 可用 `ThreadLocalRandom`，无需字段）。
7. **用 `HashSet` + `toArray()` 实现 `getRandom`** —— 每次 `O(n)` 复制，`2·10^5` 次调用直接超时。
8. **删除后忘记收缩数组/只看 `nums` 不看 `pos`** —— 「数组长度」与「映射大小」必须始终相等，这是不变量 I 的直接推论，建议写成断言。
9. **在 `insert` 时用了「先加数组再看映射」的顺序** —— 若中途抛异常（如 OOM），会留下「数组有值、映射没有」的中间态。单线程且不抛异常时无害，但作为设计题，先检查再写入更稳。

### 043.6.3 面试官最爱追问的三个问题

**Q1：为什么删除不能直接 `remove(index)`？**

> 「因为数组删除中间元素要把后面所有元素前移，`O(n)`。而本题的集合是**无序**的，顺序没有任何语义，所以可以用「末尾元素顶替被删位置 + 砍掉末尾」把删除变成 `O(1)`。代价是顺序被打乱 —— 这一点在允许乱序的场景（集合、对象池、随机采样）里完全免费，但在需要保序的场景（LRU）里就不能这么做，得改用双向链表。」

**Q2：`getRandom` 的均匀性是怎么保证的？**

> 「`nums` 里没有空洞、没有重复，长度就是集合大小 `s`，所以 `rand.nextInt(s)` 等概率取到每个下标，每个元素概率恰好 `1/s`。真正会破坏均匀性的是「用哈希表迭代取第 k 个」——`HashSet` 的迭代顺序不是均匀随机，而且第 k 个要遍历 `O(k)`。我在测试里做了 10 万次采样的频次检验，每个值都应落在 10000±500。」

**Q3：如果允许重复元素（381），结构要怎么改？**

> 「`pos` 从『值 → 下标』改成『值 → 下标集合』，比如 `Map<Integer, Set<Integer>>`。`insert` 总是成功并往集合里加一个下标；`remove(val)` 从集合里任取一个下标 `i` 做 swap-and-pop，把末尾元素搬过来。这里有一个必须处理的细节：如果**被搬过来的末尾元素恰好也等于 `val`**（重复值），那么集合里要把 `i` 换成 `s-1` 而不是删掉 `i` —— 否则会丢掉一个合法下标。`getRandom` 完全不变，仍然是在 `nums` 上均匀取下标。」

---

## 043.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 381 O(1) 插入删除获取随机 - 允许重复](https://leetcode.cn/problems/insert-delete-getrandom-o1-duplicates-allowed/) | 困难 | `值 → 下标集合`；swap-and-pop 时要处理「搬来的值也等于被删值」 |
| [LC 146 LRU 缓存](https://leetcode.cn/problems/lru-cache/) | 中等 | 需要保序 ⇒ 哈希 + **双向链表**，不能用 swap-and-pop |
| [LC 460 LFU 缓存](https://leetcode.cn/problems/lfu-cache/) | 困难 | 哈希 + 频次桶（每桶一个双向链表）+ 最小频次指针 |
| [LC 710 黑名单中的随机数](https://leetcode.cn/problems/random-pick-with-blacklist/) | 困难 | 把黑名单里的值「映射」到末尾的白名单值上，与 swap 思想同源 |
| [LC 519 随机翻转矩阵](https://leetcode.cn/problems/random-flip-matrix/) | 中等 | 二维压一维 + 已翻转格子 swap 到末尾，`O(1)` 均匀取未翻转格 |
| [LC 895 最大频率栈](https://leetcode.cn/problems/maximum-frequency-stack/) | 困难 | 哈希（值→频次）+ 每个频次一个栈，按最高频次弹 |
| [LC 705 设计哈希集合](https://leetcode.cn/problems/design-hashset/) | 简单 | 手写哈希：链地址法 / 开放寻址，理解 `O(1)` 平均的来源 |
| [LC 706 设计哈希映射](https://leetcode.cn/problems/design-hashmap/) | 简单 | 同上，加 value；顺便体会「扩容 + rehash」为什么是均摊 `O(1)` |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 381 允许重复的版本**
1. `pos` 的 value 该换成什么类型？删的时候从那堆下标里随便挑一个，行不行？
2. 如果被搬过来的末尾元素**恰好也等于**要删的值，集合里那个下标应该怎么变？（删除？还是替换？）

**LC 710 黑名单中的随机数**
1. 能否把「命中黑名单」的情况映射到「某个一定不在黑名单里的值」上，从而让采样永远一次命中？
2. 这个映射需要多大的表？能不能只在「黑名单值落在白名单区间内」时才建立？

**LC 519 随机翻转矩阵**
1. 二维坐标怎么压成一维？翻转过的格子怎么「挪」到末尾，才能保证前面始终是未翻转的？
2. 为什么不需要真的维护一个 `m×n` 的布尔矩阵？

**LC 146 LRU 缓存**
1. 为什么这里不能用 swap-and-pop？（提示：什么东西必须"有序"？）
2. 双向链表里，为什么要在哈希表里存**结点引用**而不是下标？

**通用**
- 设计题先列「每个操作需要什么能力」，再去找「哪种结构提供这种能力」，最后处理冲突的那一项。
- 「乱序」常常是免费的优化空间：能乱序就能 swap-and-pop，能 swap-and-pop 就能 `O(1)` 删除。

</details>

---

## 附：本章小结（可背走的三句话）

```
① 三个 O(1) 的唯一拼法：数组（下标随机访问）+ 哈希（值→下标）+ swap-and-pop（删中间变删末尾）
② 不变量：pos 与 nums 互为逆映射，键集合 = 值集合；pos.remove(val) 必须放在最后
③ Go 用指针接收者、Java 分清 remove(int) 与 remove(Object)；均匀性靠"数组无空洞"
```

> **下一部分**：[`044-reverse-linked-list.md`](044-reverse-linked-list.md) —— 第 6 部分 · 链表。本章的「末尾顶替」和「双向链表 + 哈希」会在 LRU（054）里再见面一次。
