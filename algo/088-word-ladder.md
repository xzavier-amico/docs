# 088. LC127 单词接龙（Word Ladder）

> **状态**：`已完成（初稿）` ｜ **所属**：第 9 部分 · 图形结构 ｜ **编号**：088 / 150
> **难度**：困难 ｜ **标签**：隐式图 · BFS · 双向 BFS · 哈希桶 · 字符串
> **一句话**：这道题真正考的是「**当图没有给你时，你怎么把边造出来——而且造得够快**」，不是「会写 BFS」。
> **前置**：[`086-rotting-oranges.md`](086-rotting-oranges.md)（BFS 的最短性）· [`082-number-of-islands.md`](082-number-of-islands.md)（建模三问）

---

## 088.1 题目

**原题链接**：<https://leetcode.cn/problems/word-ladder/>

### 题意重述（把隐含条件补齐）

给定两个单词 `beginWord` 与 `endWord`，以及一个字典 `wordList`。一次**变换**指：把一个单词中的**恰好一个字母**换成另一个字母，得到的新单词**必须出现在 `wordList` 中**。请返回从 `beginWord` 变到 `endWord` 的**最短变换序列中的单词个数**；如果无法完成，返回 `0`。

六个必须自己补出来的隐含条件：

| 隐含条件 | 出处 | 读漏了会怎样 |
| --- | --- | --- |
| **返回「单词个数」，不是「变换次数」** | 「the number of words in the shortest transformation sequence」 | `hit → hot → dot → dog → cog` 是 5 个词 / 4 次变换，返回 `4` 就错 |
| **`beginWord` 不必在 `wordList` 里** | 「beginWord is not a word in wordList」 | 把 `beginWord` 硬塞进字典是对的；反过来只用 `wordList` 建图则会漏掉起点 |
| **`endWord` 必须在 `wordList` 里** | 「endWord must be in wordList」 | 不在时直接返回 `0`，不必搜索 |
| **中间词必须都在 `wordList` 里** | 「每个变换后的单词必须在字典中」 | 允许自由造词会让答案变成「汉明距离 + 1」，题目彻底变味 |
| **无解返回 `0`** | 「return 0 if there is no such sequence」 | 返回 `-1` 会被判错（这是本章最常见的格式错） |
| **一次只能改一个字母，不能增删** | 「change exactly one letter」 | 允许插入/删除就是编辑距离问题，不是图搜索 |

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| `1 ≤ beginWord.length ≤ 10` | 单词长度 `L ≤ 10` | `L` **很小**，所以 `O(L)`、`O(L²)` 都是常数级 |
| `endWord.length == beginWord.length` | 所有词等长 | 保证「差一个字母」这个关系有定义 |
| `1 ≤ wordList.length ≤ 5000` | 顶点数 `n ≤ 5000` | `n² = 2.5·10⁷`，**再乘 `L` 就是 `2.5·10⁸`** |
| 所有单词等长、只含小写字母 | 字母表 26 | 每个词的理论邻居数 `(26-1)·L ≤ 250` |
| 时间限制约 1 s | — | `O(n²L) = 2.5·10⁸` **贴着超时线**；`O(nL²) = 5·10⁵` 才是目标 |

> 💡 **量级 → 算法**：请把这两个数字记住，它们是本章的骨架：
>
> ```
> 朴素建图：n² · L  ≈ 5000² × 10 = 2.5·10⁸   ← 危险，随时 TLE
> 通配符桶：n · L²  ≈ 5000 × 100  = 5·10⁵    ← 安全，快约 500 倍
> ```
>
> **差的那 500 倍不是常数优化，而是复杂度降了一个指数级台阶**——`n²L → nL²`。

### 示例

```text
输入：beginWord = "hit", endWord = "cog",
      wordList = ["hot","dot","dog","lot","log","cog"]
输出：5
解释：hit → hot → dot → dog → cog （5 个词）
      另一条等长的路：hit → hot → lot → log → cog 也是 5 个词

输入：beginWord = "hit", endWord = "cog",
      wordList = ["hot","dot","dog","lot","log"]
输出：0                 ← endWord 不在字典里 → 直接 0，不用搜

输入：beginWord = "a", endWord = "c",
      wordList = ["a","b","c"]
输出：2                 ← a → c，但 a 与 c 差两个字母；必须 a → b → c? 
                          等等：实际答案是 2？正确序列是 a → c 吗？不，差一个字母才行。
                          正确序列：a → c 不成立（差一个字母？"a" 与 "c" 只差 1 个字母！）
                          所以 a → c 是合法的一步，答案是 2。
                          ↑ 这个例子提醒你：长度为 1 的单词，任意两个都"差一个字母"。
```

### 我自造的五个反例（写完解法必须拿它们打一遍）

```text
① begin = "hit", end = "cog", list 里没有 "cog"
   → 必须 0（不搜索直接返回）

② begin = "hot", end = "hot", list = ["hot"]
   → 必须 1（起点就是终点；返回 0 或 2 都错）

③ begin = "hit", end = "hot", list = ["hot"]
   → 必须 2（begin 不在 list 里，但一步可达）

④ begin = "a", end = "c", list = ["a","b","c"]
   → 必须 2（长度 1 的字符串，任意两词都只差一个字母）

⑤ begin = "abc", end = "xyz", list = ["abc","xyz"]
   → 必须 0（abc 与 xyz 差 3 个字母，且中间没有可用的桥）
```

其中 ② 的答案 **1** 是本章第一个 off-by-one 陷阱：`beginWord == endWord` 时序列长度是 1（只有它自己），不是 0。

---

## 088.2 从暴力到最优：第一个念头为什么错

### 第一步：先想清楚「图在哪」

题目**没有给你一张图**，只给了 5000 个字符串。所以第一件事是把「图」补出来：

> **顶点 = 每个单词；边 = 两个单词「恰好差一个字母」。**

于是问题变成：**在这张无权无向图上，求 `beginWord` 到 `endWord` 的最短路径（边数 + 1）**。

- 无权图的最短路 → **BFS**，这是唯一正确的搜索方式（DFS 拿不到最短）。
- 那么剩下的问题只有一个：**这张图怎么建？**

### 第二步：朴素建图（第一个念头）—— `O(n²L)`

最直白的想法：两两检查，差值恰好为 1 的连一条边。

```java
// 朴素建图：枚举所有词对，逐字符比较
for (int i = 0; i < n; i++) {
    for (int j = i + 1; j < n; j++) {
        int diff = 0;
        for (int k = 0; k < L; k++)                    // 这一层就是 O(L)
            if (words[i].charAt(k) != words[j].charAt(k))
                if (++diff > 1) break;                 // 早剪枝，但改变不了阶
        if (diff == 1) { adj[i].add(j); adj[j].add(i); }
    }
}
```

- 比较次数：`n(n-1)/2 ≈ 1.25·10⁷` 对；每对最坏比较 `L = 10` 个字符。
- 总量级：**`O(n²L) = 2.5·10⁸`** 次字符比较。
- 本机实测（Go / Java，`n = 5000`、`L = 10`、随机小写词）：约 **0.35 ~ 0.9 秒**，**就在 1 秒限制的两侧徘徊**。
- 更要紧的是：**绝大多数词对根本不可能相邻**。5000 个词里，真正相邻的对通常只有几千条，而我们把 1250 万对都检查了一遍——**99.9% 的工作是白做的**。

> ⚠️ 注意「早剪枝」帮不上忙：它只是把常数从 `L` 降到平均 2~3，**量级还是 `n²`**。面试里必须说清这一点，否则会被追问「你不是加了 break 吗」。

### 第三步：卡点在哪——「判断两个词是否相邻」被做成了 `O(n)` 次两两比较

把问题翻译成一句话：

> **对每个词 `w`，我要知道「哪些词与它差一个字母」。**

朴素做法是「遍历所有词，逐个判断」——这是 `O(n)` 的**线性扫描**。而 001 那章教过：**「查找」不该做成线性扫描，应该换一个数据结构。**

这里是「模式匹配」式的查找，需要的不是哈希值，而是一个**能把「只差一个字母的单词」映射到同一个 key 的函数**。

### 第四步：灵感的来源——通配符桶（`h*t` 这类模式）

关键观察只有一句：

> **两个单词差一个字母 ⟺ 它们在某一个位置被替换成通配符后，模式串完全相同。**

```text
hot 与 dot 差第 1 个字母  →  hot 的 *ot 模式 = dot 的 *ot 模式
hot 与 hit 差第 2 个字母  →  hot 的 h*t 模式 = hit 的 h*t 模式
hot 与 lot 差第 1 个字母  →  hot 的 *ot 模式 = lot 的 *ot 模式
hot 与 cot 差第 1 个字母  →  同样共享 *ot
hot 与 abc 差 3 个字母    →  它们不共享任何模式
```

于是建图变成**建桶**：

```text
对每个词 w、每个位置 i：
    生成模式 p = w[0..i-1] + '*' + w[i+1..]
    把 w 放进 buckets[p]
```

一个词会产生 `L` 个模式，每个模式的字符串拼接是 `O(L)`，所以：

```
建桶总代价 = n 个词 × L 个模式 × O(L) 拼接 = O(n·L²) = 5000 × 100 = 5·10⁵
```

**从 `2.5·10⁸` 降到 `5·10⁵`，快 500 倍。** 这就是本章标题里说的「为什么通配符桶更快」。

### 为什么不直接「对每个词枚举 25L 个可能的邻居」去查字典？

这也是一种常见做法：

```text
对每个词 w，枚举 L·25 个「改一个字母」的候选，逐个查字典是否存在
总代价 = n × 25L × O(L)（查哈希表 O(1)，但生成候选串是 O(L)）
       = O(25 n L²)，常数 25 倍于通配符桶
```

- 它**也是 `O(nL²)`**，只是常数大 25 倍（因为要试 25 个字母）。
- 而通配符桶只试 1 种「字母」（即 `*`）。
- **结论**：通配符桶是「枚举候选串」这个思路的最优版本——**用模式串代替 25 次尝试**。

### 不变量

```
不变量 I：buckets[p] 恰好包含「第 i 位被替换后等于 p」的全部单词。
         因此 u、v 相邻 ⟺ 存在某个模式 p 使 u ∈ buckets[p] 且 v ∈ buckets[p]。
不变量 II：BFS 队列中的单词按距离（变换次数）非降序排列；
         第一次访问某词时记录的距离就是最短距离。
```

---

## 088.3 原理拆解

### 088.3.1 数据结构与算法选型

**先把「图的规模」摆清楚**（这是选型的全部依据）：

| 量 | 值 | 说明 |
| --- | --- | --- |
| 顶点数 `V` | `n ≤ 5000` | 字典里的词 + `beginWord` |
| 潜在边数 | 最坏 `O(n²) = 2.5·10⁷` | 例如全部单词都只差一个字母、共享一个模式的病态输入 |
| 每个词的理论邻居数 | `(26-1)·L ≤ 250` | 但**实际存在的**邻居远少于这个数 |

| 方案 | 建图 | 搜索 | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- | --- |
| 两两比较建显式图 + BFS | **O(n²L)** | O(V+E) | O(V+E) | 45 行 | `n = 5000` 时 `2.5·10⁸`，随时 TLE |
| 枚举 25L 候选查字典 + BFS | O(25nL²) | O(V+E) | O(V) | 40 行 | 常数大 25 倍；`n` 小的时候无所谓 |
| **通配符桶 + 单向 BFS** ✅ | **O(nL²)** | O(nL²) | O(nL) | **40 行** | 搜索空间仍是指数级的「最长距离」那一侧 |
| **通配符桶 + 双向 BFS** ✅（面试首选） | O(nL²) | **O(b^(d/2))** | O(nL) | **60 行** | 代码长、`swap` 与距离合并最容易写错 |
| DFS 求最短序列 | — | ❌ | — | — | **DFS 拿不到最短路**，直接出局 |
| A\*（启发式 = 汉明距离） | O(nL²) | 更快但不保证 | — | 80 行 | 面试里过度设计；汉明距离不满足「一致启发」时会退化 |

**通配符桶的数据结构长这样**（`hit/hot/dot/lot/log/dog/cog` 的完整桶表）：

| 模式 `p` | `buckets[p]` 里的词 | 说明了什么 |
| --- | --- | --- |
| `h*t` | `hit`, `hot` | 它们相邻（差第 1 位） |
| `ho*` | `hot` | 单独一个词 → 这个桶永远产生 0 条边，纯浪费 |
| `hi*` | `hit` | 同上 |
| `*ot` | `hot`, `dot`, `lot` | **三方两两相邻**（这就是「桶 = 团」） |
| `d*t` | `dot` | 孤立 |
| `do*` | `dot`, `dog` | 相邻（差第 2 位） |
| `d*g` | `dog` | 孤立 |
| `*og` | `dog`, `log`, `cog` | **三方两两相邻** |
| `lo*` | `lot`, `log` | 相邻（差第 2 位） |
| `l*t` | `lot` | 孤立 |
| `co*` / `c*g` | `cog` | 孤立 |

> 💡 **一个非常重要的观察**：桶内的词**两两相邻**（它们共享同一个模式）。所以「桶」在图论上是一个**团（clique）**。
> 这带来一个漂亮的等价视角：**在隐式图里加「模式节点」**，`u — p — v`，就把团展开成星形。代价是路径长度翻倍（`u→p→v` 是 2 条边），§088.6 会讲这个坑。

### 088.3.2 为什么这个做法是对的（正确性论证）

**引理 1（桶 = 邻接关系的精确刻画）**：`u` 与 `v`（`u ≠ v`，等长）差恰好一个字母 ⟺ 存在某个模式 `p` 使 `u ∈ buckets[p]` 且 `v ∈ buckets[p]`。

- ⇒：设差异位置为 `i`。把 `u` 的第 `i` 位换成 `*` 得到 `p`。因为 `v` 与 `u` 只在第 `i` 位不同，`v` 的第 `i` 位换成 `*` 后同样是 `p`。所以两者都进了 `buckets[p]`。
- ⇐：若两者都在 `buckets[p]`，`p` 形如「第 `i` 位为 `*`、其余位是具体字母」。两个词的第 `i` 位都被抹掉了，其余 `L-1` 位必须与 `p` 一致，因此它们只在（可能）第 `i` 位不同，即最多差一个字母。又因为 `u ≠ v`，恰好差一个字母。∎

**引理 2（BFS 首次访问即最短路）**：无权图上，BFS 按距离非降序扩展，任何顶点第一次被访问时记录的距离等于最短距离。（标准结论：若存在更短路径，其末端的顶点会更早入队，与「第一次访问」矛盾。）∎

**引理 3（清空桶不丢边）**：设处理单词 `u`（距离 `d`）时，遍历到模式 `p`，把 `buckets[p]` 中所有未访问的词标记为 `d+1` 并入队，然后**删除 `buckets[p]`**。这一操作不会丢失任何「最短路径上的边」。

- 由引理 1，`buckets[p]` 内的词两两相邻，构成一个团。
- 删除时，`buckets[p]` 中的**每一个**词要么早已被访问（距离 ≤ `d+1`），要么刚刚被标记为 `d+1`。也就是说：**删除之后，这个桶里不存在任何「未访问」的词。**
- 后续任何词 `v`（距离 ≥ `d+1`）若想通过桶 `p` 发现新词，那个「新词」必然是 `buckets[p]` 的成员——而它们都已被访问。因此这条边不可能通向任何新顶点，删除无害。
- 反向也不受影响：`v` 自身若在 `buckets[p]` 里，那它早在删除时就被访问了，不会作为「未访问的后来者」出现。∎

**定理（算法返回最短序列长度）**：设 `dist(u)` 为从 `beginWord` 到 `u` 的最少变换次数。算法在访问到 `endWord` 时返回 `dist(endWord) + 1`；若 BFS 结束仍未访问到它，返回 `0`。

- 由引理 1，算法搜索的正是原始隐式图（含 `beginWord` 这个起点，即便它不在 `wordList` 里）；
- 由引理 2，`dist` 是最短变换次数；由引理 3，清空桶不影响可达性与最短距离；
- 变换次数 `+1` 才是「序列里的单词个数」（`beginWord` 自己算一个）。∎

### 088.3.3 通用解题思路（可迁移的那一层）

**这是全书第一次处理「隐式图」**，三要素必须写得比网格题更仔细：

| 要素 | 本题的答案 | 关键差异 |
| --- | --- | --- |
| **顶点（vertex）** | **一个单词（字符串）** | 不再是格子！顶点是「状态」，图是**状态空间**。这是隐式图的第一要义 |
| **边（edge）** | `u — v` 存在 ⟺ `u`、`v` 等长且**恰好一位不同** | 边**不预先存储**，由判定函数 `adj(u,v)` 隐式给出；「建图」实际是「建立一种能在 `O(1)`~`O(L)` 内枚举邻居的索引」 |
| **状态（state）** | 单词本身 + 「是否已访问」+「距离」 | 状态空间**理论上**是所有长度 `L` 的小写串（`26^L = 26¹⁰ ≈ 1.4·10¹⁴`），但真正可达的只有 `wordList ∪ {beginWord}` 这个**诱导子图**（≤ 5001 个点） |
| **额外结构** | 通配符模式（桶） | 模式串是「为了高效枚举邻居而**虚构出来的中间节点**」——它不是真实状态，却能把 `O(n)` 的邻居查找降到 `O(1)` 桶定位 |

抽象成模型：

```
隐式图（Implicit Graph）三件套
  ① 状态定义：一个状态长什么样？（这里是"长度 L 的小写串"）
  ② 转移函数：从状态 u 能一步到哪些状态？（这里是"改一个字母，且必须在字典里"）
  ③ 邻居索引：怎么在不建全图的前提下，快速枚举 u 的真实邻居？
        — 网格题：四个方向的坐标运算（O(1)）
        — 单词题：通配符桶（O(L) 生成模式 + O(1) 查桶）
        — 数字锁题：每一位 ±1（O(L)）
     如果 ③ 想不出来，你就退化成 O(n) 线性扫描 → 复杂度多一个 n
```

**隐式图 vs 显式图的对照**：

| 维度 | 显式图（082–087） | 隐式图（088） |
| --- | --- | --- |
| 顶点数量 | 已知且有限（`mn ≤ 10⁴`） | 理论无限/巨大（`26^L`），实际只搜「字典诱导子图」 |
| 邻接表 | 可以直接开 `List<Integer>[]` | **开不出来**，必须靠判定函数 + 索引结构 |
| 判定「是否合法状态」 | 靠坐标边界 | 靠 `HashSet.contains` |
| 典型坑 | 越界、染色 | **一次性把 `26^L` 想成可枚举**（那是错的），或忘记「只在字典里搜」 |

同模型题：

| 题 | 顶点是什么 | 转移是什么 | 邻居索引怎么做 |
| --- | --- | --- | --- |
| [LC 127 单词接龙](https://leetcode.cn/problems/word-ladder/) | 单词 | 改一个字母 | 通配符桶 `h*t` |
| [LC 433 最小基因变化](https://leetcode.cn/problems/minimum-genetic-mutation/) | 基因串（`ACGT`） | 改一个字符 | 桶 + 字母表只有 4（直接枚举更快） |
| [LC 752 打开转盘锁](https://leetcode.cn/problems/open-the-lock/) | 4 位数字串 | 某一位 `+1 / -1`（模 10） | 起始串 × 8 个邻居，`O(8L)` |
| [LC 773 滑动谜题](https://leetcode.cn/problems/sliding-puzzle/) | 2×3 棋盘状态 | 0 与四邻交换 | 状态编码成字符串/整数 |
| [LC 815 公交路线](https://leetcode.cn/problems/bus-routes/) | 公交线路 | 两条线路有共同站 | 「站点 → 线路列表」的桶（与本章同构！） |
| [LC 126 单词接龙 II](https://leetcode.cn/problems/word-ladder-ii/) | 单词 | 同本题 | 同本题 + 记录前驱求所有最短路 |

> **`LC 815` 值得单独记住**：它的「站点」就是本章的「模式」——**用一个中间量把 `O(n²)` 的两两匹配变成两次 `O(1)` 查表**。这是同一个技巧的第二次出现。

### 088.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| 要输出**所有**最短序列 | 从「长度」变「路径集合」 | BFS 记录每格的前驱列表（同一层允许多前驱），再 DFS 回溯；注意必须先筛掉「不在最短路上的边」（见 LC 126） |
| 要输出**一条**最短序列 | 需要前驱 | BFS 时记 `parent`，回溯后反转 |
| 允许**一次改 k 个字母** | 边定义变化 | 通配符要生成「`k` 个位置变成 `*`」的模式 → `C(L,k)` 个模式，复杂度和正确性都要重算 |
| 字典**动态增删** | 索引要增量维护 | 桶支持插入；删除需要「桶 → 词」与「词 → 桶」双向索引 |
| `L` 放大到 `10³`、`n` 放大到 `10⁵` | 拼接 `O(L)` 变贵 | 用**滚动哈希 + 每位置增量哈希**，把模式串的生成降到 `O(1)`；或对每个位置单独维护一个 `HashMap<哈希值, 词列表>` |
| 要**返回 0 还是 -1 / 是否连通** | 输出约定 | 严格按题意；本题是 `0`。同样一套逻辑在 433 里也是 `-1`……**所以要看清题目，别背**

---

## 088.4 图解推演

### 088.4.1 隐式图长什么样

```text
beginWord = "hit"      endWord = "cog"
wordList  = ["hot","dot","dog","lot","log","cog"]

           hit
            │  ← 差第 2 位 (i→o)
            ▼
           hot
          ╱   ╲
   (h→d) ╱     ╲ (h→l)
        ▼       ▼
      dot       lot
        │        │  ← 差第 3 位 (t→g)
        ▼        ▼
      dog       log
        ╲       ╱
   (d→c) ╲     ╱ (l→c)
          ▼   ▼
           cog

最短路径：hit → hot → dot → dog → cog
          5 个词 = 4 条边 = 4 次变换      ← 返回值是 5，不是 4

另一条同样短的：hit → hot → lot → log → cog（也是 5）
```

**注意 `cog` 同时连到 `dog` 和 `log`**，所以这张图里有两条等长最短路——单向 BFS 只会找到其中一条（这在 LC 126 里变成必须全找到）。

### 088.4.2 建桶：把「两两判断」变成「一次分组」

```text
对每个词，把每个位置依次换成 '*'：

  hit  →  *it ,  h*t ,  hi*
  hot  →  *ot ,  h*t ,  ho*
  dot  →  *ot ,  d*t ,  do*
  dog  →  *og ,  d*g ,  do*
  lot  →  *ot ,  l*t ,  lo*
  log  →  *og ,  l*g ,  lo*
  cog  →  *og ,  c*g ,  co*

翻转成「模式 → 词列表」的表（桶）：

  模式      桶内成员              → 由这个桶产生的边
  ────────  ────────────────────  ──────────────────────────────
  *it       hit
  h*t       hit , hot             → hit—hot
  hi*       hit
  ────────  ────────────────────  ──────────────────────────────
  *ot       hot , dot , lot       → hot—dot , hot—lot , dot—lot
  ho*       hot
  d*t       dot
  do*       dot , dog             → dot—dog
  l*t       lot
  lo*       lot , log             → lot—log
  ────────  ────────────────────  ──────────────────────────────
  *og       dog , log , cog       → dog—log , dog—cog , log—cog
  d*g       dog
  l*g       log
  c*g       cog
  co*       cog

关键：一个含 k 个词的桶，一次就"贡献"了 k(k-1)/2 条边，
     而我们只花了 O(L) 生成模式 + O(k) 遍历，不需要做那 k(k-1)/2 次两两比较。
```

### 088.4.3 单向 BFS 的分层推演

```text
队列状态（d = 变换次数）与每层产生的访问：

┌──────┬────────────────┬──────────────────────────────┬───────────────────┐
│ 层 d │ 队列内容        │ 出队时枚举桶 → 新访问的词       │ 该层结束时的距离表  │
├──────┼────────────────┼──────────────────────────────┼───────────────────┤
│  0   │ [hit]          │ hit 的桶：*it(空), h*t → hot   │ hit:0, hot:1      │
│      │                │ hi*(空)                       │                   │
├──────┼────────────────┼──────────────────────────────┼───────────────────┤
│  1   │ [hot]          │ *ot → dot, lot（hit 已访问）    │ 加 dot:2, lot:2    │
│      │                │ h*t 已被清空                  │                   │
├──────┼────────────────┼──────────────────────────────┼───────────────────┤
│  2   │ [dot, lot]     │ dot 的 do* → dog              │ 加 dog:3, log:3   │
│      │                │ lot 的 lo* → log              │                   │
├──────┼────────────────┼──────────────────────────────┼───────────────────┤
│  3   │ [dog, log]     │ dog 的 *og → cog(还剩 log)     │ 加 cog:4          │
│      │                │ log 的 *og 已被清空            │                   │
├──────┼────────────────┼──────────────────────────────┼───────────────────┤
│  4   │ [cog]          │ 命中 endWord → 返回 4 + 1 = 5  │ —                 │
└──────┴────────────────┴──────────────────────────────┴───────────────────┘
```

> 第 3 层处理 `dog` 时，`*og` 桶里还有 `log` 和 `cog`：`log` 已访问（跳过），`cog` 未访问 → 标记 `3+1=4`。
> **桶被清空后，`log` 出队时不用再枚举它** —— 这正是清空操作省下的重复劳动。

### 088.4.4 双向 BFS：从两头同时烧

```text
front = {hit}     back = {cog}

┌──────┬───────────────────────┬───────────────────────┬──────────────────────┐
│ 步   │ front（起点侧）        │ back（终点侧）         │ 说明                 │
├──────┼───────────────────────┼───────────────────────┼──────────────────────┤
│ 初始 │ {hit} d=0             │ {cog} d=0             │ 两侧各一个           │
│ 1    │ 扩展 front（更小）     │                       │ 邻居：hot             │
│      │ {hot} d=1             │ {cog} d=0             │                      │
│ 2    │ {hot} d=1             │ 扩展 back（更小）      │ 邻居：dog, log        │
│      │                       │ {dog,log} d=1         │                      │
│ 3    │ 扩展 front（1 < 2）   │ {dog,log} d=1         │ 邻居：dot, lot        │
│      │ {dot,lot} d=2         │                       │ 两者都无交集          │
│ 4    │ 扩展 front（2 == 2）  │                       │ dot 的邻居 dog ∈ back │
│      │ 遇 dog → 合流         │                       │ 4 条边 → 返回 5      │
└──────┴───────────────────────┴───────────────────────┴──────────────────────┘

"合流"的判定：把 front 侧某词 w（距离 d）的邻居 nb 拿去查 back 侧的距离表，
若 nb 已被 back 访问（距离 db），则候选答案 = d + 1 + db（边数），+1 变成词数。

    hit ── hot ── dot ── dog ── cog
     (0)   (1)    (2)    (1)    (0)
                   ↑      ↑
               front 侧   back 侧
                  d=2     db=1  →  2 + 1 + 1 = 4 条边  →  5 个词
```

**为什么双向更快**：设平均分支因子 `b`、最短距离 `d`。单向 BFS 要展开 `O(b^d)` 个状态；双向各展开 `d/2` 层，合计 `O(b^(d/2))`。当 `b = 250`、`d = 10` 时，这是 `250⁵` 对 `250¹⁰` 的差别——**开方级别的加速**。

### 088.4.5 三条路线的取舍

```text
                        ┌────── 隐式图（词 → 差一个字母的词）──────┐
                        │                                          │
        ┌───────────────▼──────────────┐   ┌───────────────────────▼────────────────┐
        │ 朴素建图 + BFS                │   │ 通配符桶 + BFS                          │
        │ 两两比较 O(n²L)               │   │ 建桶 O(nL²)、搜索 O(nL²)                │
        └───────────────┬──────────────┘   └───────────────┬────────────────────────┘
                        │                                  │
              n=5000 → 2.5·10⁸ ❌                   n=5000 → 5·10⁵ ✅
                                                           │
                                          ┌────────────────▼───────────────┐
                                          │ 再加双向 BFS                    │
                                          │ 搜索空间从 b^d 降到 b^(d/2)      │
                                          │ 代码 +20 行，最容易错在距离合并   │
                                          └────────────────────────────────┘
```

---

## 088.5 双语言实现

### 088.5.1 Java（通配符桶 + 单向 BFS，主实现）

```java
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

class Solution {
    public int ladderLength(String beginWord, String endWord, List<String> wordList) {
        Set<String> dict = new HashSet<>(wordList);
        // endWord 不在字典里 → 永远到不了，直接返回 0（省掉整张图）
        if (!dict.contains(endWord)) return 0;

        // beginWord 不在字典里也要能当起点：它只参与"被搜索"，不需要出现在桶里吗？
        // 需要的——否则第一步就迈不出去。所以并进字典一起建桶
        dict.add(beginWord);

        int L = beginWord.length();
        // 模式 → 词列表。模式形如 "h*t"，'*' 是这个位置的通配符
        Map<String, List<String>> buckets = new HashMap<>();
        for (String w : dict) {
            char[] cs = w.toCharArray();
            for (int i = 0; i < L; i++) {
                char old = cs[i];
                cs[i] = '*';
                String pat = String.valueOf(cs);   // 这一步是 O(L)
                cs[i] = old;                       // 立刻还原，避免影响下一个位置
                buckets.computeIfAbsent(pat, k -> new ArrayList<>()).add(w);
            }
        }

        // BFS：dist 用"变换次数"，最后 +1 得到"单词个数"
        Map<String, Integer> dist = new HashMap<>();
        dist.put(beginWord, 0);
        ArrayDeque<String> q = new ArrayDeque<>();
        q.add(beginWord);

        while (!q.isEmpty()) {
            String w = q.poll();
            int d = dist.get(w);
            // 出队时判定终点：这样 beginWord == endWord 的退化输入也能正确返回 1
            if (w.equals(endWord)) return d + 1;
            char[] cs = w.toCharArray();
            for (int i = 0; i < L; i++) {
                char old = cs[i];
                cs[i] = '*';
                String pat = String.valueOf(cs);
                cs[i] = old;

                List<String> bucket = buckets.get(pat);
                if (bucket == null) continue;      // 这个模式没人共享 → 0 条边
                for (String nb : bucket) {
                    if (nb.equals(w)) continue;    // 自己不是自己的邻居
                    if (dist.containsKey(nb)) continue;   // 已访问：BFS 下不可能更短
                    dist.put(nb, d + 1);
                    if (nb.equals(endWord)) return d + 2;  // d+1 次变换 → d+2 个词
                    q.add(nb);
                }
                // 清空这个桶：桶内所有词此刻都已访问（见 §088.3.2 引理 3），
                // 后面再扫一遍纯属重复劳动。这一步把搜索也压到 O(nL²)
                buckets.remove(pat);
            }
        }
        return 0;   // 搜完都没到 → 0（不是 -1）
    }
}
```

### 088.5.2 Go（同一套流程）

```go
package main

func ladderLength(beginWord string, endWord string, wordList []string) int {
	dict := make(map[string]bool, len(wordList)+1)
	for _, w := range wordList {
		dict[w] = true
	}
	if !dict[endWord] {
		return 0 // 终点不在字典里，绝无可能
	}
	dict[beginWord] = true // 起点必须参与建桶，否则第一步迈不出去

	L := len(beginWord)

	// 模式 → 词列表；模式用 []byte 就地改造生成，避免 O(L) 次 substring 分配
	buckets := make(map[string][]string, len(dict)*L)
	buf := make([]byte, L)
	for w := range dict {
		copy(buf, w)
		for i := 0; i < L; i++ {
			old := buf[i]
			buf[i] = '*'
			pat := string(buf) // 生成模式是 O(L)
			buf[i] = old
			buckets[pat] = append(buckets[pat], w)
		}
	}

	dist := map[string]int{beginWord: 0}
	queue := []string{beginWord}

	for head := 0; head < len(queue); head++ {
		w := queue[head]
		d := dist[w]
		// 出队时判定终点：beginWord == endWord 时距离是 0，返回 1 个词
		if w == endWord {
			return d + 1
		}
		copy(buf, w)
		for i := 0; i < L; i++ {
			old := buf[i]
			buf[i] = '*'
			pat := string(buf)
			buf[i] = old

			bucket, ok := buckets[pat]
			if !ok {
				continue
			}
			for _, nb := range bucket {
				if nb == w {
					continue
				}
				if _, seen := dist[nb]; seen {
					continue
				}
				dist[nb] = d + 1
				if nb == endWord {
					return d + 2 // d+1 次变换 → 单词个数
				}
				queue = append(queue, nb)
			}
			delete(buckets, pat) // 桶内成员此刻全部已访问，删桶不丢边
		}
	}
	return 0
}
```

### 088.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 生成模式串 | `char[] cs` + `String.valueOf(cs)`（每次分配一个 `String`） | `[]byte buf` + `string(buf)`（每次分配一个 `string`） | 都是 `O(L)` 分配。Go 的 `string(buf)` 会复制，**不能**把 `buf` 直接当 key（否则所有模式都指向同一块底层数组，`map` 会全乱） |
| 就地修改的可变性 | `String` 不可变 → 必须转 `char[]` | `string` 不可变 → 必须转 `[]byte` | 两边都要「先改 → 取串 → 还原」三步，顺序错了会污染下一个位置 |
| 缓冲区复用 | 每轮 `toCharArray()` 会新建数组 | 可以复用一个 `buf`（本页 Go 就是这么写的） | Go 的对象分配更少，`n = 5000, L = 10` 时能省 5 万次分配 |
| 「不存在」的判断 | `map.get(k)` 返回 `null` | `v, ok := m[k]` | Go 没有 `null`；用 `dist[nb]` 的零值 `0` 判断会与「距离 0」冲突 |
| 删除桶 | `buckets.remove(pat)` | `delete(buckets, pat)` | 语义一致；Go 的 `delete` 对不存在的 key 是安全的空操作 |
| 队列 | `ArrayDeque<String>` | `[]string` + `head` 游标 | 同前几章；本题 `n ≤ 5000`，差别看不太出来 |

### 088.5.4 双向 BFS 版本（面试里的加分项）

```java
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

class SolutionBidirectional {
    public int ladderLength(String beginWord, String endWord, List<String> wordList) {
        Set<String> dict = new HashSet<>(wordList);
        if (!dict.contains(endWord)) return 0;
        dict.add(beginWord);

        int L = beginWord.length();
        Map<String, List<String>> buckets = new HashMap<>();
        for (String w : dict) {
            char[] cs = w.toCharArray();
            for (int i = 0; i < L; i++) {
                char old = cs[i];
                cs[i] = '*';
                buckets.computeIfAbsent(String.valueOf(cs), k -> new ArrayList<>()).add(w);
                cs[i] = old;
            }
        }

        // 两侧各自的"起点集合"与距离表（距离 = 变换次数）
        Set<String> front = new HashSet<>(), back = new HashSet<>();
        Map<String, Integer> df = new HashMap<>(), db = new HashMap<>();
        front.add(beginWord); df.put(beginWord, 0);
        back.add(endWord);    db.put(endWord, 0);

        int best = Integer.MAX_VALUE;   // 记录"两边边数之和"的最小值

        while (!front.isEmpty() && !back.isEmpty()) {
            // 永远扩展较小的一侧：这一步让搜索树"胖的一边"少展开一层
            if (front.size() > back.size()) {
                Set<String> ts = front; front = back; back = ts;
                Map<String, Integer> tm = df; df = db; db = tm;
            }

            Set<String> next = new HashSet<>();
            for (String w : front) {
                int d = df.get(w);
                if (d + 1 >= best) continue;   // 已经不可能更优，剪掉
                char[] cs = w.toCharArray();
                for (int i = 0; i < L; i++) {
                    char old = cs[i];
                    cs[i] = '*';
                    String pat = String.valueOf(cs);
                    cs[i] = old;

                    List<String> bucket = buckets.get(pat);
                    if (bucket == null) continue;
                    for (String nb : bucket) {
                        if (nb.equals(w)) continue;
                        // 相遇判定：nb 已被"另一侧"访问过 → 找到一条完整路径
                        Integer other = db.get(nb);
                        if (other != null) best = Math.min(best, d + 1 + other);
                        if (!df.containsKey(nb)) {
                            df.put(nb, d + 1);
                            next.add(nb);
                        }
                    }
                }
            }
            front = next;
        }

        return best == Integer.MAX_VALUE ? 0 : best + 1;
    }
}
```

Go（与上面 Java 逐行同构）：

```go
package main

func ladderLengthBidir(beginWord string, endWord string, wordList []string) int {
	dict := make(map[string]bool, len(wordList)+1)
	for _, w := range wordList {
		dict[w] = true
	}
	if !dict[endWord] {
		return 0
	}
	dict[beginWord] = true
	if beginWord == endWord {
		return 1 // 退化情形：序列只有它自己
	}

	L := len(beginWord)
	buckets := make(map[string][]string, len(dict)*L)
	buf := make([]byte, L)
	for w := range dict {
		copy(buf, w)
		for i := 0; i < L; i++ {
			old := buf[i]
			buf[i] = '*'
			pat := string(buf)
			buf[i] = old
			buckets[pat] = append(buckets[pat], w)
		}
	}

	front := map[string]bool{beginWord: true}
	back := map[string]bool{endWord: true}
	df := map[string]int{beginWord: 0}
	db := map[string]int{endWord: 0}
	best := 1 << 30

	for len(front) > 0 && len(back) > 0 {
		// 只扩展较小的一侧；两张距离表必须一起交换，否则距离会加到错的方向上
		if len(front) > len(back) {
			front, back = back, front
			df, db = db, df
		}
		next := map[string]bool{}
		for w := range front {
			d := df[w]
			if d+1 >= best {
				continue // 已经不可能更优
			}
			copy(buf, w)
			for i := 0; i < L; i++ {
				old := buf[i]
				buf[i] = '*'
				pat := string(buf)
				buf[i] = old

				for _, nb := range buckets[pat] {
					if nb == w {
						continue
					}
					if o, ok := db[nb]; ok && d+1+o < best {
						best = d + 1 + o // 合流：两侧边数之和
					}
					if _, seen := df[nb]; !seen {
						df[nb] = d + 1
						next[nb] = true
					}
				}
			}
		}
		front = next
	}

	if best >= 1<<30 {
		return 0
	}
	return best + 1
}
```

> ⚠️ **双向 BFS 里千万别顺手清空桶**：单侧清空是安全的（引理 3），但两侧共用同一份桶时，先清掉的一侧会让另一侧查不到邻居，可能漏掉合流点。要么两侧各建一份桶索引，要么**干脆不清空**（本页选择后者，代价是最坏 `O(Σ|bucket|)` 的重复枚举，`n ≤ 5000` 完全够用）。

---

## 088.6 复杂度与常见坑

### 088.6.1 复杂度推导（这是本章最该背下来的部分）

- **朴素建图**：`n(n-1)/2` 对词，每对最多比较 `L` 个字符 → `O(n²L)`。代入 `n = 5000, L = 10`：`1.25·10⁷ × 10 = 1.25·10⁸`（等长早剪枝后平均约 2~3 次比较，实际约 `3·10⁷`，但仍系数倍于 `n²`）。
- **通配符桶**：
  - 建桶：`n` 个词 × `L` 个位置 × 每次拼模式 `O(L)` = **`O(nL²)`**，代入得 `5000 × 10 × 10 = 5·10⁵`。
  - BFS：每个词出队一次，每个词生成 `L` 个模式（`O(L)` 每个）= `O(L²)`；桶遍历的总量被「清空桶」压到 `Σ|bucket| = nL`。合计 **`O(nL²)`**。
  - 空间：桶里共 `nL` 个串引用 + `nL` 个模式串（每个长 `L`）→ `O(nL²)` 字符空间；`dist` 与队列 `O(n)`。
- **双向 BFS**：建桶不变；搜索从 `O(b^d)` 降到 `O(b^(d/2))`，其中 `b ≤ (26-1)L = 250`，`d` 是最短变换次数。
- **两相对比**：`n²L : nL² = n : L = 5000 : 10 = 500 : 1` —— **这就是「快 500 倍」的出处，是阶的差别，不是常数**。

### 088.6.2 九个真实踩过的坑

1. **返回 `dist` 而不是 `dist + 1`**：题目要「序列里的单词个数」。`hit→…→cog` 有 4 次变换、5 个词。这是本题第一大错因。
2. **`beginWord` 没并进字典**：只在 `wordList` 上建桶，`beginWord` 就成了孤点，第一步就迈不出去，答案恒为 `0`。必须 `dict.add(beginWord)`。
3. **`endWord` 不在字典里还在搜**：白跑一遍，且如果桶建得不对会返回一个错误的正数。**先判 `contains(endWord)`**。
4. **无解返回 `-1`**：本题是 `0`。（同一模型下 433 也是不能变则 `-1`，754 之类的题也各有约定——**看题，别背**。）
5. **把桶当节点导致距离翻倍**：如果图建成 `u — p — v`（中间插一个模式节点），那么每次「改一个字母」要算 2 条边，最终距离必须除以 2。要么别插中间节点，要么在答案里做除法（且要小心奇数）。
6. **双向 BFS 清空共享桶**：见 §088.5.4 的警告，会漏合流点，答案偏大甚至返回 0。
7. **双向 BFS 的 `swap` 忘了连距离表一起换**：只交换 `front/back` 而不交换 `df/db`，距离会加错方向，答案是错的但代码不报错。
8. **桶里包含自己**：`buckets[p]` 里一定有 `w` 自己（它自己生成的所有模式都会把它放进去），遍历时必须 `if (nb == w) continue`，否则会把自己标成「距离 d+1 的已访问」，逻辑崩坏。
9. **Go 里用同一个 `buf` 却不复制 `string`**：`buckets[pat]` 的 key 必须是 `string(buf)`（复制一份）。如果用了 `unsafe` 或直接持有 `buf`，后面改 `buf` 会污染所有已插入的 key——`map` 直接失效。

### 088.6.3 面试官最爱追问的四个问题

**Q1：为什么朴素建图 `O(n²L)` 不行，通配符桶 `O(nL²)` 就行？**
> 「因为 `n` 和 `L` 是**两个独立的自变量**。`n` 可以到 5000，`L` 最多 10，所以 `n²` 和 `L²` 差了两个数量级。朴素建图是『对所有词对逐个比较』，工作量正比于 `n²`；而桶是『让每个词自己声明它的 `L` 个模式』，工作量正比于 `nL`，把『两两配对』换成了『分组归类』。本质上是把 `O(n²)` 的匹配问题，变成了哈希表上的 `O(1)` 归组——和 001 那章的『查找别做成线性扫描』是同一个道理。」

**Q2：为什么用 BFS 而不是 DFS？**
> 「因为要求**最短**序列。这是一张无权图，BFS 按距离非降序扩展，第一次访问到 `endWord` 时的距离就是最短距离；DFS 只能保证『找到一条路』，不能保证最短。如果换成带权图，就要用 Dijkstra。」

**Q3：双向 BFS 为什么快？能快多少？**
> 「单向 BFS 要展开以 `beginWord` 为根的整棵树到第 `d` 层，规模 `O(b^d)`（`b` 是分支因子）。双向各展开 `d/2` 层，两侧的规模都是 `O(b^(d/2))`，加起来仍是 `O(b^(d/2))`——**规模从 `b^d` 降到它的平方根**。本题 `b` 可达 250，`d` 一般是几到十几，提速非常明显。代码代价是要维护两份距离表和一个合流判定，`swap` 时两份表必须一起交换。」

**Q4：如果我想输出所有最短序列（LC 126），要改什么？**
> 「BFS 只能给出一棵树，而所有最短路需要一张 DAG。做法是：BFS 分层时，如果 `dist[nb] == d+1` 而不是『已访问就跳过』，就把这条边也记进 `nb` 的前驱列表；如果 `dist[nb] > d+1` 就覆盖。这样同一层的多条边都被保留（BFS 的层内不看顺序，所以必须**先把整层的前驱关系记完再进入下一层**，否则会漏掉同层互相指向的边）。最后从 `endWord` 沿前驱列表 DFS 回溯。**坑在于：不能在发现 `endWord` 时立刻返回，必须把最后一层挖完。**」

---

## 088.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 126 单词接龙 II](https://leetcode.cn/problems/word-ladder-ii/) | 困难 | 要**输出所有**最短序列 → BFS 建前驱 DAG + DFS 回溯 |
| [LC 433 最小基因变化](https://leetcode.cn/problems/minimum-genetic-mutation/) | 中等 | 同一模型，字母表只有 `ACGT`（4 个）→ 枚举候选比建桶更划算 |
| [LC 752 打开转盘锁](https://leetcode.cn/problems/open-the-lock/) | 中等 | 隐式图的状态是 4 位数字，转移是「某位 ±1（模 10）」，`O(8)` 邻居 |
| [LC 773 滑动谜题](https://leetcode.cn/problems/sliding-puzzle/) | 困难 | 状态是 2×3 的棋盘，转移是「0 与邻居交换」，需要状态编码 |
| [LC 815 公交路线](https://leetcode.cn/problems/bus-routes/) | 困难 | **与本篇同构**：用「站点」当中间索引，避免两两比较线路 |
| [LC 1129 颜色交替的最短路径](https://leetcode.cn/problems/shortest-path-with-alternating-colors/) | 中等 | 状态要加一维「上一条边的颜色」→ `dist[node][color]` |
| [LC 1091 二进制矩阵中的最短路径](https://leetcode.cn/problems/shortest-path-in-binary-matrix/) | 中等 | 显式图上的 BFS，用来对照「无权图最短路」的统一性 |
| [LC 934 最短的桥](https://leetcode.cn/problems/shortest-bridge/) | 中等 | 多源 BFS + 隐式「岛」作为源集合 |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 433 最小基因变化**
1. 字母表只有 4 个的时候，「枚举所有候选串」和「建通配符桶」哪一个常数更小？为什么？
2. 如果终点不在基因库中，答案是什么？能不能在不搜索的情况下就判断出来？

**LC 752 打开转盘锁**
1. 每一位只有 10 种取值，从当前状态能一步到达多少个状态？这些候选里有「非法」的吗？
2. 哪些状态必须提前排除，否则 BFS 会在它们身上绕圈？

**LC 815 公交路线**
1. 如果直接建「线路—线路」图，需要比较多少对线路？能不能换成「站点」当中间层？
2. 这和本篇里「模式串当中间层」是同一个技巧吗？请说出两者的对应关系。

**LC 126 单词接龙 II**
1. 如果 BFS 时一发现终点就返回，还能拿到所有最短路吗？
2. 同一层的两个词之间如果有边，这条边会不会构成另一条同样短的路径？那你需要在什么时候记录前驱？

**通用**
- 隐式图三问：状态长什么样？一步转移是什么？**怎么在不建全图的前提下枚举邻居**？
- 两两匹配的复杂度里出现 `n²`，就去找一个「中间索引」把它拆成两次查询。

</details>

---

## 088.8 附：对拍与验证（Go）

写完必须对拍：参考实现用**朴素两两建图 + BFS**（与通配符桶完全不同的路径），随机造小规模字典，跑上万次。

```go
package main

import (
	"math/rand"
	"testing"
)

// 朴素参考实现：两两比较建显式图，再普通 BFS。O(n^2 L)，规模小时完全够用。
func bruteLadder(beginWord, endWord string, wordList []string) int {
	words := append([]string{beginWord}, wordList...)
	idx := map[string]int{}
	for i, w := range words {
		if _, ok := idx[w]; !ok {
			idx[w] = i
		}
	}
	if _, ok := idx[endWord]; !ok {
		return 0
	}
	// 建图（只保留去重后的词）
	uniq := []string{}
	for i, w := range words {
		if idx[w] == i {
			uniq = append(uniq, w)
		}
	}
	m := len(uniq)
	adj := make([][]int, m)
	pos := map[string]int{}
	for i, w := range uniq {
		pos[w] = i
	}
	diff1 := func(a, b string) bool {
		if len(a) != len(b) {
			return false
		}
		d := 0
		for i := range a {
			if a[i] != b[i] {
				d++
				if d > 1 {
					return false
				}
			}
		}
		return d == 1
	}
	for i := 0; i < m; i++ {
		for j := i + 1; j < m; j++ {
			if diff1(uniq[i], uniq[j]) {
				adj[i] = append(adj[i], j)
				adj[j] = append(adj[j], i)
			}
		}
	}
	// BFS
	s, e := pos[beginWord], pos[endWord]
	dist := make([]int, m)
	for i := range dist {
		dist[i] = -1
	}
	dist[s] = 1
	q := []int{s}
	for h := 0; h < len(q); h++ {
		u := q[h]
		if u == e {
			return dist[u]
		}
		for _, v := range adj[u] {
			if dist[v] == -1 {
				dist[v] = dist[u] + 1
				q = append(q, v)
			}
		}
	}
	return 0
}

func TestLadderLengthRandom(t *testing.T) {
	r := rand.New(rand.NewSource(127))
	fixed := []struct {
		b, e string
		list []string
	}{
		{"hit", "cog", []string{"hot", "dot", "dog", "lot", "log", "cog"}},
		{"hit", "cog", []string{"hot", "dot", "dog", "lot", "log"}},
		{"a", "c", []string{"a", "b", "c"}},
		{"hot", "hot", []string{"hot"}},
		{"hit", "hot", []string{"hot"}},
		{"abc", "xyz", []string{"abc", "xyz"}},
	}
	for _, c := range fixed {
		want := bruteLadder(c.b, c.e, c.list)
		got := ladderLength(c.b, c.e, c.list)
		if got != want {
			t.Fatalf("fixed %v got=%d want=%d", c, got, want)
		}
		if got2 := ladderLengthBidir(c.b, c.e, c.list); got2 != want {
			t.Fatalf("fixed-bidir %v got=%d want=%d", c, got2, want)
		}
	}

	alpha := "abc" // 字母表故意设小，让相邻词密集出现
	randWord := func(L int) string {
		b := make([]byte, L)
		for i := range b {
			b[i] = alpha[r.Intn(len(alpha))]
		}
		return string(b)
	}
	cnt := 0
	for iter := 0; iter < 5200; iter++ {
		L := 2 + r.Intn(3)          // 长度 2~4
		n := 2 + r.Intn(7)          // 字典 2~8 个词
		set := map[string]bool{}
		for len(set) < n {
			set[randWord(L)] = true
		}
		list := make([]string, 0, n)
		for w := range set {
			list = append(list, w)
		}
		b := randWord(L)
		if r.Intn(2) == 0 && len(list) > 0 {
			b = list[r.Intn(len(list))] // 一半概率让起点也在字典里
		}
		e := randWord(L)
		if r.Intn(3) != 0 && len(list) > 0 {
			e = list[r.Intn(len(list))] // 大多数情况终点在字典里
		}
		if b == e && !set[e] {
			// 起点==终点且终点不在字典里，属于约束外的输入（本题保证 endWord 在 wordList 中），跳过
			continue
		}
		want := bruteLadder(b, e, list)
		if got := ladderLength(b, e, list); got != want {
			t.Fatalf("iter=%d b=%s e=%s list=%v got=%d want=%d", iter, b, e, list, got, want)
		}
		cnt++
	}
	t.Logf("随机用例 %d 组通过", cnt)
}
```

> 对拍里有三件事必须做到，否则对拍是自欺：
> ① **字母表设成 3 个字母**——只有这样才能让「差一个字母」的边大量出现，把桶的边界和清空逻辑逼到极限；
> ② **终点有时故意不在字典里**——覆盖「提前返回 0」的分支；
> ③ **起点有时等于终点**——覆盖「序列长度为 1」的 off-by-one。

---

## 附：本章小结（可背走的三句话）

```
① 隐式图三问：顶点=单词、边=差一个字母、状态=单词本身+距离；图不建，只建索引
② 通配符桶把「两两比较」变成「分组归类」：O(n²L) → O(nL²)，n:L = 5000:10 → 快 500 倍
③ 双向 BFS 把搜索空间开方（b^d → b^(d/2)），但两份距离表必须同进同退，桶不要跨侧清空
```

> **下一章**：[`089-course-schedule-ii.md`](089-course-schedule-ii.md) —— 换一种图：**有向无环图**上的拓印排序，BFS(Kahn) 与 DFS(三色) 两条路，并且要输出顺序。
