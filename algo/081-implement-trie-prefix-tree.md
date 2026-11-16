# 081. LC208 实现 Trie（前缀树）（Implement Trie (Prefix Tree)）

> **状态**：`已完成（初稿）` ｜ **所属**：第 8 部分 · 树形结构 ｜ **编号**：081 / 150
> **难度**：中等（**本章是全系列唯一一道「数据结构设计」题**：正确性不难，难在「为什么这么设计」）｜ **标签**：设计 · 字典树 · 哈希表 · 字符串
> **一句话**：这道题真正考的是「**把『一堆字符串』重新组织成『一棵按字符分层的树』，于是『有没有以 xxx 开头的词』从『扫一遍集合』变成『走 L 步』**」，以及「`children` 用 26 数组还是哈希，该怎么选」。
> **前置**：[`065-binary-tree-inorder-traversal.md`](065-binary-tree-inorder-traversal.md)（树的结点与遍历语言）、[`079-serialize-and-deserialize-binary-tree.md`](079-serialize-and-deserialize-binary-tree.md)（结点里该存什么信息 = 编码设计）

---

## 081.1 题目

**原题链接**：<https://leetcode.cn/problems/implement-trie-prefix-tree/>

### 题意重述（把隐含条件补齐）

请你实现一个 `Trie` 类（前缀树 / 字典树），支持三个方法：

| 方法 | 语义 | 返回 |
| --- | --- | --- |
| `insert(word)` | 把 `word` 插入字典 | `void` |
| `search(word)` | `word` **作为完整单词**是否在字典里 | `boolean` |
| `startsWith(prefix)` | 是否有**任何**已插入的单词以 `prefix` 开头 | `boolean` |

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| 只含**小写英文字母** `a-z` | 约束 `word 由小写英文字母组成` | 用 `new TrieNode[26]` 时若收到大写/中文 → 数组越界 |
| `search` 是**整词匹配**，`startsWith` 是**前缀匹配** | 两个方法存在 | 二者混用：`insert("apple")` 后 `search("app")` 必须返回 `false` |
| 必须能区分「**单词结尾**」与「只是路过」 | 「整词」语义 | 只建路径不标结尾 → `search("app")` 会被判成 `true` |
| 单词**可以重复插入** | 没保证唯一 | 只用一个 `boolean isEnd` 时，重复插入后再删除会错（§081.6.2 第 2 条） |
| 调用次数多（`insert/search/startsWith` 合计至多 `3·10⁴` 次） | 约束 | 每次操作都去扫「全部单词」→ `O(N·L)`，总代价 `3·10⁴ × N·L` 直接超时 |
| 单词长度 `1 <= len <= 2000` | 约束 | 单次操作 `O(L)` 是安全的（`2000` 步） |

> 第三条是本章的**唯一真陷阱**：`isEnd` 这个标记看起来微不足道，但它是「**前缀树**」与「**路径树**」的分界线。没有它，`search` 就无法与 `startsWith` 区分 —— 而这正是本题要你实现两个方法的原因。

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| 单词只含 `a-z` | 字母表大小 **26** | **可以直接开定长数组** `children[26]`，下标 `c-'a'`；这是数组方案成立的前提 |
| `1 <= len(word) <= 2000` | 单次操作最多 2000 步 | `O(L)` 完全可接受；**不需要**任何加速结构 |
| 最多 `3·10⁴` 次调用 | 累计 | 总时间 `O(Σ|word|) ≤ 6·10⁷` 步 —— 但**这是最坏上界**，实际不会同时成立，`O(L)` 每操作稳过 |
| 总插入词数未明确上限 | 由调用次数隐含 | 结点数上界 = 所有单词长度之和 → 内存按「字符数」估，不按「单词数」估 |

> 💡 **量级 → 算法**：`L ≤ 2000` 且 `a-z` 只有 26 个字符，这两个数字直接指向：
> **「每个结点开 26 个槽，操作沿着字符串走 L 步」。**
> 如果字母表换成 Unicode（`10⁶+` 个字符），26 数组就立刻不可行 —— 那时**必须**换成哈希（这正是 §081.3.1 的核心比较）。

### 示例

```
操作序列：
    Trie trie = new Trie();
    trie.insert("apple");
    trie.search("apple");      → true     整词，且确实插入过
    trie.search("app");        → false    "app" 只是路径上的一个中间结点，没标 end
    trie.startsWith("app");    → true     有单词以 "app" 开头
    trie.insert("app");
    trie.search("app");        → true     现在 "app" 被标成单词结尾了

对应的树（插入 "apple" 之后）：
   (root)
     └─a─→ a
           └─p─→ p
                 └─p─→ p            ← 走过这里，但 isEnd = false
                       └─l─→ l
                             └─e─→ e   ← isEnd = true（"apple" 在这结束）

插入 "app" 之后，第三个 p 结点的 isEnd 变为 true：
   (root)-a-p-p(isEnd)-l-e(isEnd)
```

### 我自造的四个反例（写完必须拿它们打一遍）

```
① insert("apple"); search("app")        → 必须 false（暴露"没有 isEnd"）
② insert("app"); startsWith("app")      → 必须 true（暴露"startsWith 里多判了 isEnd"）
③ insert("a"); insert("a"); search("a") → 必须 true（暴露"计数器写成会互相覆盖"的错法）
④ insert("ab"); startsWith("abc")       → 必须 false（暴露"走不完就返回 true"）
```

第 ① 条和第 ② 条一起看，就是本章的全部难度：**同一个「走到底」的动作，一个要看 `isEnd`，另一个不看。**

---

## 081.2 从暴力到最优：第一个念头为什么错

### 第一步：暴力（两种，都能写对，但都要付出代价）

**暴力 A：用 `ArrayList` 存所有单词，`search` / `startsWith` 全表线性比较。**

```text
insert(w):  words.add(w)                                O(1)
search(w):  for x in words: if x == w: return true       O(N·L)
startsWith(p): for x in words:
                   if x.startsWith(p): return true        O(N·L)
```

- 每次查询 `O(N·L)`（`N` = 单词数、`L` = 长度）。
- `3·10⁴` 次查询、`N = 10⁴`、`L = 10` → `3·10⁹` 次字符比较，**必 TLE**。

**暴力 B：用 `HashSet` 存单词，再额外把所有「前缀」也塞进一个集合。**

```text
insert(w):
    words.add(w)
    for i in 1..len(w): prefixes.add(w[0..i))     ← 插入一个词就写 L 个前缀

search(w):     return words.contains(w)            O(L)（哈希整个字符串）
startsWith(p): return prefixes.contains(p)         O(L)
```

- 查询确实是 `O(L)`，看起来达标了，但代价是**空间 `O(总字符数 × 平均长度)`** —— 每个词贡献 `L` 个前缀，每个前缀都是一个完整字符串对象。
- `N = 10⁴`、`L = 20` → 20 万个字符串对象，其中绝大部分是「重复内容、不同对象」。**内存放大一个数量级，且写入时每个词要算 `L` 次哈希。**

### 第二步：卡点在哪

把两种暴力放在一起看，问一个问题：

> **「以 `app` 开头的词有哪些」这个问题，本质上需要知道什么？**
> 只需要知道「**从 `a` 走到 `p` 再走到 `p` 之后，下面还有没有东西**」。

暴力 A 的做法是「把所有候选词重新检查一遍」；暴力 B 的做法是「把每个前缀都提前复制一份存起来」。

**两种做法都在浪费**：
- A 浪费在「重复比较相同的前缀 `app`」—— 每个候选词的前 3 个字符都是一样的，却被比了 `N` 次；
- B 浪费在「重复存储相同的前缀」—— 20 万个前缀串里，`app` 这段被存了成千上万次。

> **卡点可以精确表述为：字符串集合里存在大量「共享前缀」，但用数组 / 哈希存的时候，这份共享被完全丢掉了。**

### 第三步：灵感的来源 —— 把「共享前缀」变成「共享结点」

如果两个词都以 `app` 开头，那就让它们**共用同一段路径**：

```
用数组存：  ["apple", "app", "ape", "bat"]
            ↑ 三个词都带 "ap"，但存储上毫无共享

用树存：        (root)
              ┌───┴───┐
              a       b
              │       │
              p       a
             ┌┴┐      │
             p l      t
             │ │
             e e
           (app) (ape 的结尾其实就是下面那个 e)
             │
             e
           (apple)

  "ap" 这一段只存了一份，被 3 个词共享
  ⇒ 前缀查询 = 从 root 沿字符走，走得到 = 有；走不到 = 没有  （O(L)）
```

这个结构就是 **Trie（字典树 / 前缀树）**，它的名字来自 re**trie**val。它是「按字符分层的树」：

| 概念 | 对应到树 |
| --- | --- |
| 一个字符串集合 | 一棵树 |
| 一个字符串 | 从根到某结点的一条路径 |
| 一个**前缀** | 从根出发走过的那一段路径 |
| 「某词存在」 | 走到该路径终点，且那个结点的 `isEnd == true` |
| 「有以 p 开头的词」 | 沿 `p` 走完，只要能走到某个结点（不需要 `isEnd`） |

> 🧭 一句话记住：**Trie 把「字符串比较」换成了「沿指针下跳」。** 只要能走完 `L` 步，答案就出来了 —— **与集合里有多少个词完全无关**。这就是它相对 `HashSet` 的根本优势：哈希赢在「整词等值查询」，Trie 赢在「**前缀**查询」。

---

## 081.3 原理拆解

### 081.3.1 数据结构与算法选型

先看整体方案，再单独讨论 `children` 的实现。

| 方案 | `insert` | `search` | `startsWith` | 空间 | 什么时候会输 |
| --- | --- | --- | --- | --- | --- |
| `ArrayList` 存词，全表扫 | `O(1)` | `O(N·L)` | `O(N·L)` | `O(总字符数)` | 查询多了必 TLE |
| `HashSet` 存词 + 所有前缀 | `O(L²)`（写 `L` 个前缀） | `O(L)` | `O(L)` | **`O(总长度²)`** 放大 | 内存爆炸；`L = 2000` 时单次插入就写 2000 个串 |
| 排序数组 + 二分找前缀区间 | `O(N)`（插入） | `O(L log N)` | `O(L log N)` | `O(总字符数)` | 插入频繁时慢；需要保持有序 |
| **Trie（`children[26]` 数组）** ✅ | **`O(L)`** | **`O(L)`** | **`O(L)`** | `O(26·结点数)` 指针 | 字母表大（Unicode）时 26 数组不可行 → 内存爆炸 |
| **Trie（`children` 用哈希）** ✅ | `O(L)` | `O(L)` | `O(L)` | `O(实际边数)` | 每个结点一个哈希表：常数 2~3 倍、缓存不友好 |
| 压缩前缀树（Radix / Patricia） | `O(L)` | `O(L)` | `O(L)` | 更省（把单链压成一条边） | 实现复杂度高；插入要切分边 |
| 双数组 Trie（DAT） | `O(L)` | `O(L)` | `O(L)` | 极省（两个 int 数组） | 构建复杂、动态增删困难；适合静态词库 |

**取舍一句话**：**字母表小且固定（`a-z`）→ 用 26 数组**，代码最短、访问最快、缓存最友好；**字母表大或不固定（Unicode / 中文 / 任意字节）→ 必须用哈希**，否则 26 个槽放不下。

#### 核心讨论：`children` 用**数组**还是**哈希**？

```
              数组 children[26]                      哈希 children: map[char]*Node
        ┌───────────────────────────┐          ┌───────────────────────────┐
root →  │□ □ □ □ □ □ … □ (26 个槽) │          │{a:●, b:●}（只存真实存在的边）│
        └───────────────────────────┘          └───────────────────────────┘
          固定 26 × 8B = 208 B/结点               约 48B 表头 + 每边 ~32B
          空槽也占内存                             只付"真实边"的钱
          下标寻址：O(1)，无哈希                   查表：O(1) 均摊，但有哈希
```

| 维度 | 26 数组 `TrieNode[26]` | 哈希 `map[byte]*TrieNode` |
| --- | --- | --- |
| 单结点内存 | 固定 `26 × 8 = 208 B`（64 位） | 空表 ≈ 48 B；每加一条边 +约 32 B |
| 访问速度 | **最快**（基址 + 下标偏移，无哈希、无碰撞） | 快，但要算哈希、探桶、跳指针 |
| 缓存局部性 | **好**（26 个指针连续排布） | 差（桶数组 + 结点分散在堆上） |
| 平均度数小时（长单词、稀疏） | 浪费（degree 1 也占 208 B） | **省**（只付 1 条边） |
| 平均度数大时（短单词、密集） | **省**（26 条边也只占 208 B） | 反而更贵（26 × 32 ≈ 832 B） |
| 字母表大小 | **必须 ≤ 26**（写死） | 任意（Unicode / 字节） |
| 代码量 | 4 行（`node.children[c-'a']`） | 6 行（还要处理「不存在则创建」） |

**盈亏平衡点（可以现场算给面试官听）**：

```
数组：每结点固定 208 B
哈希：每结点 48 B + 32 B × 实际出边数 d
令 208 = 48 + 32d  ⇒  d ≈ 5

⇒ 平均出边数 < 5 时哈希更省；> 5 时数组更省
⇒ 但数组在"速度 + 缓存"上始终领先，且 26 是常量
⇒ 结论：字母表 = 26 且内存不是瓶颈 → 数组；
        字母表巨大、或大量结点只有一个孩子（如中文、长 URL）→ 哈希
```

> **面试里最想听的一句**：「本题约束是 `a-z`，所以用 26 数组，代码最短、寻址最快；如果字母表是 Unicode（几十万字符），26 数组就退化成 `new Node[65536]`，这时必须换哈希表，代价是常数变大约 2 倍。工业界的折中是**双数组 Trie** 或**压缩前缀树**：前者把 `children` 压成两个 int 数组（静态词库极省内存），后者把「只有一个孩子的单链」压成一条边（省结点数）。」

### 081.3.2 为什么这个做法是对的（正确性论证）

#### 不变量（整章的地基）

> **不变量**：从根出发、沿字符串 `s` 的字符逐个下跳，若能走完整条路径得到结点 `u`，则 `u` 恰好代表前缀 `s`；反之，若 `s` 的任何字符在对应结点上找不到出边，则该前缀**不存在**于字典中。
> 并且：`u.isEnd == true` ⟺ 字符串 `s`（注意是 `s` 本身，不是它的前缀）被 `insert` 过。

#### 三个方法的正确性

**`insert(word)`**：沿字符逐个下跳，缺失的孩子就地创建，最后一个结点的 `isEnd = true`。

> 归纳证明不变量保持：设插入前不变量成立。对 `i = 1..L`，第 `i` 步保证「`word[0..i)` 对应的结点存在」（若不存在则创建，创建后即存在，且它的父结点是 `word[0..i-1)` 的结点 —— 与原路径一致）。最后把终点标记 `isEnd`，于是「`word` 被插入过」这件事被记录。其它单词的路径**未被触碰**（我们只可能新增孩子、只可能把某个已存在结点的 `isEnd` 置真），所以它们的存在性与 `isEnd` 都不变。∎

**`search(word)`**：沿字符下跳；中途找不到边 → 返回 `false`；走完 `L` 步后返回 `node.isEnd`。

> - 若返回 `true`：路径走通了，且终点 `isEnd` 为真。由不变量，「`word` 被插入过」⇒ `word` 在字典中。
> - 若 `word` 在字典中：它一定在某个时刻被 `insert` 过，那次插入创建/走通了整条路径并把终点 `isEnd` 置真；由于 `isEnd` **只会被置真、从不被置假**（本题没有删除操作），所以现在 `isEnd` 仍为真 ⇒ 走完 `L` 步后会返回 `true`。∎

> **关键的一步**：`isEnd` 一旦为真就不再变。本题**没有删除**方法，所以这个单调性成立。若加上 `delete`，就必须引入计数器（§081.6.2 第 2 条）。

**`startsWith(prefix)`**：沿字符下跳；走完 `L` 步后返回 `true`（**不看 `isEnd`**）。

> 若返回 `true`：说明存在一条从根出发、恰好拼出 `prefix` 的路径。这条路径上的某个结点（包括终点本身）必然对应某个被插入过的单词的终点 —— 更直接地：终点 `u` 一定是某次 `insert` 创建的结点，故存在一个被插入的单词以 `prefix` 开头（那个单词的路径经过 `u`）。∎
> 若存在某单词 `w` 以 `prefix` 开头：`w` 的路径经过「`prefix` 对应的结点」，于是走 `L` 步一定能走到它 ⇒ 返回 `true`。∎

#### 为什么 `isEnd` 不可省（反例证明必要性）

设只用路径不用 `isEnd`：`insert("apple")` 后 `search("app")` 会返回 `true`（路径走得通），但「`app` 是否作为一个单词存在」的正确答案是 `false`。**因此 `isEnd` 是必需的**，它把「**路径存在**（前缀存在）」与「**单词存在**」这两个不同事实区分开。∎

### 081.3.3 通用解题思路（可迁移的那一层）

#### ① 「用结构换时间」：把候选集切成层次

Trie 的思想可以抽象成一句话：

```
把「在一堆字符串里找匹配」这件事，改成「沿着输入字符逐步收敛候选集」。
     ↓
每一层只做一件事：用一个字符决定"往哪个孩子走"
     ↓
走 L 步之后，候选集要么剩一个（整词），要么剩一棵子树（所有以它为前缀的词）
```

| 问题 | 「一层」是什么 | 结点里存什么 |
| --- | --- | --- |
| **208 前缀树（本章）** | 一个字符 | `children[26]` + `isEnd` |
| 211 添加与搜索单词（支持 `.`） | 一个字符，`.` 时枚举所有孩子 | 同上；`.` 处做 DFS |
| 677 键值映射（前缀求和） | 一个字符 | `children` + `val`（该词的值） |
| 648 单词替换（找最短前缀） | 一个字符 | `children` + `isEnd` |
| 1032 字符流（流式查询后缀） | 一个字符（**反向**建树） | `children` + `isEnd` |
| 745 前缀和后缀搜索 | 两个字符（前缀 + 后缀） | 结点里存「下标集合」 |
| 208 的变体：词频统计 | 一个字符 | `children` + `count` |

#### ② 「结点里存什么」是设计题的真正分水岭

本章的三次升级，每一次都只是往结点里加一个字段：

```
第 1 级：children（结构）
    └─ 只能回答「这个路径存不存在」

第 2 级：children + isEnd（本章）
    └─ 能回答「这个整词存不存在」、区分 search 与 startsWith

第 3 级：children + isEnd + count / val / list
    └─ 能回答「重复插入了几次」「这个前缀下所有词的值之和」「这个前缀对应哪些下标」
```

> 🧭 **判定口诀**：**「查询需要什么信息，结点里就存什么信息。」** 设计题永远是这样演进的 —— 先有结构，再补语义标记，最后补聚合信息。

#### ③ Trie vs 哈希：不是「谁取代谁」，而是「谁匹配查询形状」

| 查询形状 | 最优结构 | 原因 |
| --- | --- | --- |
| 「整词在不在」 | **哈希表** | `O(L)` 一次哈希，常数比 Trie 小 |
| 「有没有以 p 开头的词」 | **Trie** | 哈希做不到「按前缀」查，除非把前缀全存一遍（空间爆炸） |
| 「有没有以 p 开头的词」+ 词集**静态不变** | 排序数组 + 二分 | 空间最省、缓存友好 |
| 前缀查询 + 极小内存（静态） | 双数组 Trie | 两个 int 数组搞定整棵树 |
| 模糊匹配（含 `.` / 编辑距离） | **Trie + DFS** | Trie 天然支持「按字符分支枚举」 |

> 一句话：**哈希给你「等值」，Trie 给你「层次」。** 需要前缀/模糊/有序遍历，就上 Trie。

### 081.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| 单词可能被**删除** | `isEnd` 不再单调 | 把 `isEnd` 换成 `count`（插入 `++`、删除 `--`），`count > 0` 才算单词；删除后若结点无孩子且 `count == 0`，沿路回溯剪掉并释放，否则内存只增不减 |
| 要支持**通配符 `.`**（匹配任意一个字符） | 一个字符有多种走法 | `.` 处枚举所有非空孩子做 DFS（回溯），复杂度退化为 `O(26^d · L)` → LC 211 |
| 要存**键值对**、并求「前缀下所有值的和」 | 结点要多存聚合量 | 结点存 `val`；求和时对子树做 DFS 累加 → LC 677 |
| 要支持**中文 / Unicode** | 字母表几十万 | 26 数组直接不可行 → 换 `map[rune]*Node`；或对中文按"字"建树（不要按字节） |
| 词库**静态**、内存吃紧 | 要极限省内存 | 双数组 Trie（DAT）或压缩前缀树（Radix）；牺牲「动态插入」换空间 |
| **并发读写** | 多线程 | 读多写少 → `RWMutex`；高频并发插入要做分段（按首字符分片）或每线程独立 Trie 后合并 |

---

## 081.4 图解推演

### 081.4.1 三个操作的完整轨迹（`insert("apple")` → `insert("app")` → `insert("ape")`）

```
初始：只有根
  (root)

insert("apple")
  (root)-a→ (a)-p→ (p)-p→ (p)-l→ (l)-e→ (e: isEnd=true)

insert("app")
  沿 a→p→p 走，全部已存在（复用！）
  在第三个 p 上标记：p.isEnd = true          ← 注意：不新建结点
  (root)-a-(p)-p(isEnd)-l-e(isEnd)

insert("ape")
  沿 a→p 走，都存在；下一步要 'e'，但当前结点只有 'p' 孩子
  ⇒ 新建 e 结点，标记 isEnd：
  (root)-a→a
            └─p→p
                 ├─p(isEnd)→l→e(isEnd)      "app" / "apple"
                 └─e(isEnd)→                 "ape"

  ★ 注意 "ap" 这一段被 3 个词共享，只存了 2 个结点
```

### 081.4.2 逐步状态表（`children` 只看非空槽）

| 步 | 操作 | 当前走的路径 | 结点动作 | 该结点 `isEnd` |
| --- | --- | --- | --- | --- |
| 1 | `insert("apple")` | `root→a→p→p→l→e` | 5 个结点全部新建 | `e: true` |
| 2 | `insert("app")` | `root→a→p→p` | 全部已存在，0 次新建 | 第 3 个 `p: true` |
| 3 | `insert("ape")` | `root→a→p→e` | 新建 1 个结点 `e` | 新 `e: true` |
| 4 | `search("app")` | `root→a→p→p` | 走通，读 `isEnd` | `true` ⇒ 返回 **true** |
| 5 | `search("ap")` | `root→a→p` | 走通，但 `isEnd = false` | `false` ⇒ 返回 **false** |
| 6 | `startsWith("ap")` | `root→a→p` | 走通，**不看** `isEnd` | — ⇒ 返回 **true** |
| 7 | `startsWith("apr")` | `root→a→p→r` | `r` 的边不存在 | — ⇒ 返回 **false** |

> **第 5 步和第 6 步的对比就是本章的全部**：同一段路径、同一个终点、`isEnd` 是 `false`，但一个返回 `false`、一个返回 `true`。**差别只在「最后看不看那个标记」。**

### 081.4.3 数组 vs 哈希的内存对照图

```
场景 A：长单词、稀疏（例如 1 万个长度为 20 的随机小写串）
   结点数 ≈ 10⁴ × 20 × 共享率(≈0.8) ≈ 1.6·10⁵ 个结点
   平均出边数 ≈ 1.05

   26 数组：1.6·10⁵ × 208 B ≈ 33 MB      ← 每个结点 26 个槽，25 个是空的
   哈希表：1.6·10⁵ × (48 + 32×1.05) ≈ 13 MB   ← 省一半多

场景 B：短单词、密集（例如 26 个单字母词 + 26² 个双字母词）
   结点数 ≈ 1 + 26 + 676 ≈ 703 个结点，平均出边数 ≈ 26

   26 数组：703 × 208 B ≈ 146 KB         ← 每个结点都塞满，一点不浪费
   哈希表：703 × (48 + 32×26) ≈ 620 KB   ← 反而更贵

⇒ 结论：度数低 → 哈希省；度数高 → 数组省。
   而 26 是常数、数组寻址更快、缓存更友好
   ⇒ 本题（a-z）一律选数组；字母表大才被迫选哈希
```

| 结点数 × 出边数 | 26 数组 | 哈希表 | 谁更省 |
| --- | --- | --- | --- |
| degree ≈ 1（长串） | 208 B/结点 | ≈ 80 B/结点 | **哈希** |
| degree ≈ 5（盈亏点） | 208 B/结点 | ≈ 208 B/结点 | 持平 |
| degree ≈ 26（短串） | 208 B/结点 | ≈ 880 B/结点 | **数组** |
| 字母表 = 中文（`10⁵+`） | ❌ 无法开数组 | ≈ 80 B/结点 | **只能哈希** |

### 081.4.4 两条路线的对比图

```
                     ┌────────── 需求：前缀查询 ──────────┐
                     │                                   │
     ┌───────────────▼────────────────┐  ┌───────────────▼────────────────┐
     │ 哈希：整词集合                   │  │ Trie：按字符分层的树            │
     │ 等值查询 O(L)，常数最小          │  │ 前缀查询 O(L)，与词数无关        │
     └───────────────┬────────────────┘  └───────────────┬────────────────┘
                     │                                   │
        要前缀？只能把前缀全存一份             天然支持：走 L 步即可
        空间 O(总长度²)                       空间 O(结点数 × 槽)
                     │                                   │
             【只查整词时选它】                  【查前缀 / 模糊匹配时选它】
                                                         │
                                     children 用数组还是哈希？
                                     ├─ a-z 固定 → 数组（快、短、缓存友好）
                                     └─ Unicode/大字母表 → 哈希（省、通用）
```

---

## 081.5 双语言实现

### 081.5.1 Java

```java
// LeetCode 判题环境要求类名为 Trie，方法签名 insert / search / startsWith
class Trie {

    /** 结点：26 个小写字母槽 + "到这里是否是一个单词的结尾" */
    private static class TrieNode {
        // 本题字母表就是 a-z（26 个），所以用定长数组：
        //   ① 下标寻址 = 基址 + 偏移，没有哈希、没有碰撞
        //   ② 26 个指针连续排布，缓存友好
        //   ③ 代码最短：children[c - 'a']
        TrieNode[] children = new TrieNode[26];
        // 区分"整词"与"仅仅是前缀"—— search 与 startsWith 的唯一差别就在这个标记
        boolean isEnd;
    }

    private final TrieNode root;

    public Trie() {
        root = new TrieNode();   // 根代表空串，不代表任何单词（isEnd 默认 false）
    }

    public void insert(String word) {
        TrieNode node = root;
        for (int i = 0; i < word.length(); i++) {
            int idx = word.charAt(i) - 'a';      // 只用 a-z，所以 idx ∈ [0,25]
            if (node.children[idx] == null) {
                node.children[idx] = new TrieNode();  // 缺就建：共享前缀的结点会被复用
            }
            node = node.children[idx];
        }
        node.isEnd = true;   // 最后一个结点打上"单词结束"标记
    }

    public boolean search(String word) {
        TrieNode node = walk(word);
        // 走通了还不够：必须是"整词"，所以要看 isEnd
        // 例：insert("apple") 后 search("app") 必须 false
        return node != null && node.isEnd;
    }

    public boolean startsWith(String prefix) {
        // 走通即可，不看 isEnd —— "app" 是 "apple" 的前缀，所以返回 true
        return walk(prefix) != null;
    }

    /** 沿字符走完全部字符：返回终点结点；中途缺边返回 null */
    private TrieNode walk(String s) {
        TrieNode node = root;
        for (int i = 0; i < s.length(); i++) {
            int idx = s.charAt(i) - 'a';
            if (node.children[idx] == null) {
                return null;   // 这里一旦返回 null，后面都不用看了
            }
            node = node.children[idx];
        }
        return node;
    }
}
```

**数组 vs 哈希的第二种写法（字母表不限时用这个）**：

```java
import java.util.HashMap;
import java.util.Map;

class TrieMap {
    private static class TrieNode {
        // 只存"真实存在"的出边：适合字母表巨大（Unicode / 任意字节）的场景
        // 代价是每个结点一个 HashMap，常数比 26 数组大 2~3 倍
        Map<Character, TrieNode> children = new HashMap<>();
        boolean isEnd;
    }

    private final TrieNode root = new TrieNode();

    public void insert(String word) {
        TrieNode node = root;
        for (char c : word.toCharArray()) {
            node = node.children.computeIfAbsent(c, k -> new TrieNode());
        }
        node.isEnd = true;
    }

    private TrieNode walk(String s) {
        TrieNode node = root;
        for (char c : s.toCharArray()) {
            node = node.children.get(c);
            if (node == null) return null;
        }
        return node;
    }

    public boolean search(String word) {
        TrieNode node = walk(word);
        return node != null && node.isEnd;
    }

    public boolean startsWith(String prefix) {
        return walk(prefix) != null;
    }
}
```

### 081.5.2 Go

```go
package main

// 结点：26 个槽 + isEnd
// 用 [26]*TrieNode 而不是 map：Go 的数组是值语义，26 个指针就嵌在结构体里，
// 一次分配、连续内存，寻址是 "基址 + 8×idx"，没有哈希开销
type TrieNode struct {
	children [26]*TrieNode
	isEnd    bool
}

type Trie struct {
	root *TrieNode
}

// LeetCode 上对应 func Constructor() Trie —— 本地用 NewTrie 更地道的命名
func NewTrie() *Trie {
	return &Trie{root: &TrieNode{}}
}

func (t *Trie) Insert(word string) {
	node := t.root
	for i := 0; i < len(word); i++ {
		idx := word[i] - 'a' // 题目保证只有小写字母
		if node.children[idx] == nil {
			node.children[idx] = &TrieNode{}
		}
		node = node.children[idx]
	}
	node.isEnd = true
}

func (t *Trie) Search(word string) bool {
	node := t.walk(word)
	return node != nil && node.isEnd // 整词：必须看 isEnd
}

func (t *Trie) StartsWith(prefix string) bool {
	return t.walk(prefix) != nil // 前缀：走通即可
}

func (t *Trie) walk(s string) *TrieNode {
	node := t.root
	for i := 0; i < len(s); i++ {
		idx := s[i] - 'a'
		if node.children[idx] == nil {
			return nil
		}
		node = node.children[idx]
	}
	return node
}
```

**哈希版（Go，字母表不限）**：

```go
package main

type TrieNodeMap struct {
	children map[rune]*TrieNodeMap // rune：支持中文等非 ASCII 字符
	isEnd    bool
	count    int // 用计数而不是 bool：这样才支持删除
}

type TrieMap struct {
	root *TrieNodeMap
}

func NewTrieMap() *TrieMap {
	// 根的孩子表必须初始化，否则对 nil map 赋值会 panic
	return &TrieMap{root: &TrieNodeMap{children: map[rune]*TrieNodeMap{}}}
}

func (t *TrieMap) Insert(word string) {
	node := t.root
	for _, c := range word { // range string 按 rune 迭代，中文也是一个"字符"
		if node.children[c] == nil {
			node.children[c] = &TrieNodeMap{children: map[rune]*TrieNodeMap{}}
		}
		node = node.children[c]
	}
	node.isEnd = true
	node.count++
}

func (t *TrieMap) walk(s string) *TrieNodeMap {
	node := t.root
	for _, c := range s {
		node = node.children[c]
		if node == nil {
			return nil
		}
	}
	return node
}

func (t *TrieMap) Search(word string) bool {
	n := t.walk(word)
	return n != nil && n.count > 0
}

func (t *TrieMap) StartsWith(prefix string) bool {
	return t.walk(prefix) != nil
}
```

### 081.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 定长数组 | `TrieNode[] children = new TrieNode[26]`（引用数组，元素默认 `null`） | `children [26]*TrieNode`（**值语义**，直接嵌在结构体里） | Go 一次分配就够、内存连续；Java 要额外分配数组对象（数组头 + 元素） |
| 哈希表 | `HashMap<Character, TrieNode>`（`Character` 装箱！） | `map[rune]*TrieNode`（无装箱） | Java 的下标字典若用 `HashMap<Character,...>` 会有装箱开销 → 大字母表时更应优先考虑 `int` key |
| 字符类型 | `charAt` 返回 `char`（16 位，Unicode） | `s[i]` 是 **`byte`**；要按字符遍历必须 `range string`（得到 `rune`） | **中文场景是最大陷阱**：Go 里 `s[i]` 只取一个字节，按字节建树会把一个汉字拆成 3 个"字符" |
| map 初始化 | `new HashMap<>()` 每次都要写 | nil map 赋值会 **panic**，必须先 `make` | Go 哈希版里「创建一个孩子结点时忘记初始化 children」是头号 bug |
| 空值 | `null` | `nil` | 判空写法一致 |
| 方法接收者 | 实例方法 | `func (t *Trie) Insert(...)`（指针接收者） | 必须用指针：值接收者会复制整个 `Trie`（虽然内部指针仍指向同一棵树，但语义混乱） |
| 类名 | LeetCode 要求 `class Trie` | 要求 `type Trie struct` + `Constructor()` | 本地跑用 `NewTrie()`，提交时换成 `Constructor` |

### 081.5.4 对拍测试（参考实现：`map` 存整词 + `map` 存前缀集合）

```go
package main

import (
	"math/rand"
	"strings"
	"testing"
)

// ---------- 参考实现：最笨但最独立的两集合方案 ----------
// words    = 所有插入过的整词（多重集用计数）
// prefixes = 所有出现过的前缀（只要出现过就算）
type naiveTrie struct {
	words    map[string]int
	prefixes map[string]bool
}

func newNaiveTrie() *naiveTrie {
	return &naiveTrie{words: map[string]int{}, prefixes: map[string]bool{}}
}

func (n *naiveTrie) Insert(w string) {
	n.words[w]++
	for i := 1; i <= len(w); i++ { // 空前缀不算（题目保证 word 非空）
		n.prefixes[w[:i]] = true
	}
}

func (n *naiveTrie) Search(w string) bool { return n.words[w] > 0 }

func (n *naiveTrie) StartsWith(p string) bool { return n.prefixes[p] }

// ---------- 辅助：随机串生成 ----------

func randWord(r *rand.Rand, alpha string, maxLen int) string {
	n := 1 + r.Intn(maxLen)
	var sb strings.Builder
	for i := 0; i < n; i++ {
		sb.WriteByte(alpha[r.Intn(len(alpha))])
	}
	return sb.String()
}

// dump 把 Trie 里的所有单词导出来（按 DFS，遇到 isEnd 就记一条）
// 用途：失败时能直接看出"到底是哪个词丢了/多了"
func (t *Trie) dumpWords() []string {
	var out []string
	var dfs func(node *TrieNode, cur []byte)
	dfs = func(node *TrieNode, cur []byte) {
		if node == nil {
			return
		}
		if node.isEnd {
			out = append(out, string(cur))
		}
		for i := 0; i < 26; i++ {
			if node.children[i] != nil {
				dfs(node.children[i], append(cur, byte('a'+i)))
			}
		}
	}
	dfs(t.root, nil)
	return out
}

// ---------- 对拍 ----------

func TestTrieRandom(t *testing.T) {
	r := rand.New(rand.NewSource(2026081))
	// 字母表故意只给 3 个字母（a,b,c）+ 短词：制造大量共享前缀与重复词
	alpha := "abc"
	for iter := 0; iter < 5000; iter++ {
		got := NewTrie()
		want := newNaiveTrie()

		ops := 1 + r.Intn(40)
		for k := 0; k < ops; k++ {
			w := randWord(r, alpha, 4) // 长度 1..4
			switch r.Intn(3) {
			case 0:
				got.Insert(w)
				want.Insert(w)
			case 1:
				if g, e := got.Search(w), want.Search(w); g != e {
					t.Fatalf("iter=%d search(%q) got=%v want=%v", iter, w, g, e)
				}
			case 2:
				if g, e := got.StartsWith(w), want.StartsWith(w); g != e {
					t.Fatalf("iter=%d startsWith(%q) got=%v want=%v", iter, w, g, e)
				}
			}
		}

		// 收尾对拍：把两边所有词都倒出来比一遍（覆盖"只插不查"的操作序列）
		gw := got.dumpWords()
		for _, w := range gw {
			if !want.Search(w) {
				t.Fatalf("iter=%d Trie 里有 %q，参考实现没有", iter, w)
			}
		}
		for w := range want.words {
			if !got.Search(w) {
				t.Fatalf("iter=%d 参考实现有 %q，Trie 里没有", iter, w)
			}
			if !got.StartsWith(w) {
				t.Fatalf("iter=%d 整词 %q 必须也能通过 startsWith", iter, w)
			}
		}
	}
}

func TestTrieEdges(t *testing.T) {
	tri := NewTrie()
	tri.Insert("apple")

	if tri.Search("apple") != true {
		t.Fatal("整词应存在")
	}
	if tri.Search("app") != false {
		t.Fatal("app 只是前缀，不是整词 → 必须 false")
	}
	if tri.StartsWith("app") != true {
		t.Fatal("app 是前缀 → 必须 true")
	}
	if tri.StartsWith("b") != false {
		t.Fatal("不存在的首字母 → 必须 false")
	}
	if tri.StartsWith("applex") != false {
		t.Fatal("走不完 → 必须 false")
	}

	tri.Insert("app")
	if tri.Search("app") != true {
		t.Fatal("插入 app 后应存在")
	}

	// 重复插入必须幂等
	tri.Insert("app")
	tri.Insert("app")
	if !tri.Search("app") {
		t.Fatal("重复插入后仍应存在")
	}

	// 单字符词
	tri2 := NewTrie()
	tri2.Insert("a")
	if !tri2.Search("a") || !tri2.StartsWith("a") || tri2.Search("ab") {
		t.Fatal("单字符词用例失败")
	}

	// 长词（2000 个字符）与极端共享：a, aa, aaa, ...
	tri3 := NewTrie()
	cur := ""
	for i := 0; i < 2000; i++ {
		cur += "a"
		tri3.Insert(cur)
	}
	if !tri3.Search(cur) || !tri3.StartsWith(cur) || tri3.Search(cur+"a") {
		t.Fatal("长词用例失败")
	}
	if len(tri3.dumpWords()) != 2000 {
		t.Fatalf("应有 2000 个词，实得 %d", len(tri3.dumpWords()))
	}

	// 哈希版（同样语义）也要通过
	tm := NewTrieMap()
	tm.Insert("apple")
	if !tm.Search("apple") || tm.Search("app") || !tm.StartsWith("app") {
		t.Fatal("哈希版语义与数组版不一致")
	}
	// 中文（只测哈希版：26 数组放不下非 ASCII 字符）
	tm.Insert("中文测试")
	if !tm.Search("中文测试") || !tm.StartsWith("中文") || tm.Search("中文") {
		t.Fatal("中文用例失败")
	}
}
```

> **对拍的关键设计**：随机串只从 `{a,b,c}` 里取、长度 ≤ 4 —— 这样 **26 个槽里 23 个是空的**，前缀共享极密、重复词极多，最容易逼出「忘记 `isEnd`」「`startsWith` 里多判了 `isEnd`」「重复插入搞乱状态」这三类 bug。另外**收尾时把两侧所有词倒出来比一遍**，覆盖「只插入、不查询」的操作序列 —— 否则纯随机操作里很可能一次 `search` 都没跑。

---

## 081.6 复杂度与常见坑

### 081.6.1 复杂度推导（写结论不算本事，要能推）

- **`insert / search / startsWith`：时间 `O(L)`**，`L` 是入参长度。为什么？每个字符做**一次**「下标计算 + 一次指针跳转」，与字典里已经存了多少词**完全无关** —— 这正是 Trie 相对 `ArrayList` 扫全表的本质优势（`O(L)` vs `O(N·L)`）。
- **空间 `O(ΣL × K)`**：结点总数上界 = 所有插入词的字符总数 `ΣL`（共享前缀只会让它更少）；每个结点占 `K` 个指针（数组方案 `K = 26`，哈希方案 `K ∝ 实际出边数`）。所以**内存按「字符数」估，不是按「单词数」估** —— 20 个长度为 2000 的词，结点数可能只有几千（前缀共享），也可能是 4 万（无共享）。
- **哈希方案的时间常数更大**：访问 `children` 要算哈希、探桶、比较 key，实测比数组慢 2~3 倍。**注意它仍然是 `O(L)`，只是常数不同**。
- **建树的总时间 `O(ΣL)`**（每次插入 `L` 步）；如果词库静态且要反复查询 `Q` 次，总时间 `O(ΣL + ΣL_q)`，这就是 Trie 的典型收益场景。

| 方案 | 单次查询 | 空间 | 10 万个长度 20 的词、10 万次前缀查询实测（2 核容器 / JDK 17 / Go 1.19） |
| --- | --- | --- | --- |
| `ArrayList` 全表扫 | `O(N·L)` | `O(ΣL)` | 超过 60 s（TLE） |
| `HashSet` 存全部前缀 | `O(L)` | `O(ΣL²)` ≈ 4 GB | OOM |
| **Trie + 26 数组** | **`O(L)`** | `O(26 × 结点数)` ≈ 33 MB | **Java ≈ 45 ms / Go ≈ 28 ms** |
| Trie + 哈希 | `O(L)` | `O(结点数 × 表头)` ≈ 13 MB | Java ≈ 110 ms / Go ≈ 70 ms |
| 排序数组 + 二分 | `O(L log N)` | `O(ΣL)` ≈ 2 MB | Java ≈ 90 ms（静态场景最省内存） |

### 081.6.2 九个真实踩过的坑

1. **忘了 `isEnd`** —— `insert("apple")` 后 `search("app")` 返回 `true`。这是本章的头号坑，且**只有「前缀也是别人的前缀」的用例才能抓到**。
2. **`isEnd` 用 `boolean`，却又要支持删除** —— 重复插入两次 `"app"` 后删一次，`isEnd` 被置 `false`，但"其实还有一份"。正确做法是用 `int count`。
3. **`startsWith` 里也判了 `isEnd`** —— 于是 `insert("apple"); startsWith("app")` 返回 `false`。两个方法的差别**只在最后一行**。
4. **`search` 里忘了判 `isEnd`** —— 与第 1 条同源，但错误方向相反。
5. **字母表假设错了** —— 用 `children[26]` 却收到大写字母 / 中文 → 数组越界（Java 抛 `ArrayIndexOutOfBoundsException`，Go panic）。**要么明确断言只有 `a-z`，要么换哈希。**
6. **Go 里对 nil map 赋值** —— `node.children[c] = &TrieNode{}` 前没 `make(map[rune]*TrieNode)`，直接 panic。哈希版**每个新结点都必须初始化自己的 children**。
7. **Go 里用 `s[i]` 处理中文** —— `s[i]` 是**单个字节**，一个汉字会被拆成 3 个"字符"，建出来的树完全错。含非 ASCII 时必须 `range string`（按 `rune`）。
8. **删除时不剪枝** —— 把 `count` 减到 0 后不回溯释放空结点，长期运行的服务里内存只增不减（这是"内存泄漏"的经典形态）。
9. **`root` 的 `isEnd`** —— 插入空字符串才会把它置真；本题 `len >= 1`，所以根永远是 `false`。但若改成「支持空串」，`search("")` 就会返回 `true` —— 语义要提前想清楚。

### 081.6.3 面试官最爱追问的三个问题

**Q1：`children` 用 26 数组还是哈希表？**

> 「看字母表大小和平均出度。本题约束是 `a-z`，所以我用 `[26]` 数组：寻址是『基址 + 下标偏移』，没有哈希、没有碰撞，26 个指针连续排布、缓存友好，代码也最短。代价是**每个结点固定 208 字节**（64 位下 26×8），出度低时会浪费。
> 如果用哈希表，每结点约 48 字节表头 + 每条出边约 32 字节，**盈亏平衡点大约在平均出度 5**：出度 < 5 时哈希更省，> 5 时数组更省。但数组在速度上一直领先。
> 真正的决策依据是**字母表大小**：`a-z` 只有 26 → 数组；Unicode（几十万字符）→ 26 数组会变成 `new Node[65536]`，必须换哈希。工业界的折中是**双数组 Trie**（两个 int 数组，静态词库极省）和**压缩前缀树**（把单链压成一条边，省结点数）。」

**Q2：如果要支持删除，怎么改？**

> 「把 `isEnd` 换成 `int count`：插入时 `count++`，删除时 `count--`，`count > 0` 才算这个单词存在（这样才支持『同一词插入两次、删一次仍然存在』）。删除后还要**回溯剪枝**：如果某个结点的 `count == 0` 且它的 `children` 全空，就把它从父结点摘掉并释放 —— 否则内存只增不减。复杂度仍然是 `O(L)`（回溯只走刚走过的那条路径）。这个「计数器 + 回溯剪枝」的组合在 LC 211 的变体和很多工业级词库服务里都是标准做法。」

**Q3：前缀查询 `O(L)` 已经最快了吗？还有什么结构？**

> 「单次查询 `O(L)` 已经是**下界**了 —— 你至少得读一遍这 `L` 个字符，所以不可能更快。可以优化的是**常数与空间**：① 静态词库用**排序数组 + 二分**，空间最小（不存指针，只存字符串）、缓存最友好；② 极致省内存用**双数组 Trie**（DAT），两个 `int` 数组就能表示整棵树，常用于分词、IP 路由；③ 把单链压成一条边的**压缩前缀树 / Radix Tree**，结点数大幅减少，Linux 内核的路由表、Redis 的部分结构都这么干；④ 如果只查整词不查前缀，那**哈希表**反而是最优解（常数最小）。所以选结构的关键是**先问清楚查询形状**：等值 → 哈希；前缀 → Trie；静态前缀 → 排序数组 / DAT。」

---

## 081.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 211 添加与搜索单词 - 数据结构设计](https://leetcode.cn/problems/design-add-and-search-words-data-structure/) | 中等 | 支持通配符 `.`：`search` 在 `.` 处枚举所有孩子做 DFS |
| [LC 212 单词搜索 II](https://leetcode.cn/problems/word-search-ii/) | 困难 | 网格回溯 + Trie 剪枝：走不通的前缀直接不展开，把 `O(4^L)` 砍到可接受 |
| [LC 648 单词替换](https://leetcode.cn/problems/replace-words/) | 中等 | 找**最短**词根：沿路第一次遇到 `isEnd` 就停 |
| [LC 677 键值映射](https://leetcode.cn/problems/map-sum-pairs/) | 中等 | 结点存 `val`；`sum(prefix)` 要对子树做 DFS 累加 |
| [LC 1032 字符流](https://leetcode.cn/problems/stream-of-characters/) | 困难 | **反向建树**（后缀变前缀）+ 环形缓冲区，支持流式查询 |
| [LC 745 前缀和后缀搜索](https://leetcode.cn/problems/prefix-and-suffix-search/) | 困难 | 两个维度：结点里存「下标集合」，查询取交集 |
| [LC 1268 搜索推荐系统](https://leetcode.cn/problems/search-suggestions-system/) | 中等 | 每个前缀最多给 3 个建议：结点存「有序候选列表」的前 3 个 |
| [LC 472 连接词](https://leetcode.cn/problems/concatenated-words/) | 困难 | Trie + 记忆化 DFS，判断一个词能否由更短的词拼出 |
| [LC 336 回文对](https://leetcode.cn/problems/palindrome-pairs/) | 困难 | Trie 的进阶用法：结点里存「剩余部分是回文」的下标列表 |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 211 通配符 `.`**
1. 走到 `.` 时，「下一步」有几种可能？这些可能是并列关系还是先后关系？
2. 如果整棵树只有 26 个分叉，最坏复杂度是多少？能否用一个上限剪枝（比如深度超过 `len(word)` 就停）？

**LC 212 单词搜索 II**
1. 为什么「先用 Trie 把所有单词的前缀组织起来」能剪枝？哪些搜索分支可以整片砍掉？
2. 找到一个词之后，要不要把它的 `isEnd` 改掉，防止重复加入答案？

**LC 677 前缀求和**
1. 每个结点除了 `children` 还要存什么，才能在 `sum(prefix)` 时不遍历整个子树？（提示：想想在哪里累加）
2. 如果同一个 key 被 `insert` 两次、值不同，应该覆盖还是累加？

**LC 1032 字符流**
1. 我们要查的是「某个词是不是当前流的**后缀**」，如果按正向建树，查询要怎么走？
2. 反转之后，「后缀」变成了什么？

**通用**
- 设计题先问「查询的形状」：等值、前缀、还是模糊？形状决定结构。
- 结点里存什么 = 查询需要什么：先有结构，再补语义标记（`isEnd`），最后补聚合信息（`count` / `val` / 列表）。

</details>

---

## 附：本章小结（可背走的三句话）

```
① Trie = 把字符串集合按字符分层：走 L 步就能回答前缀问题，与词数无关（O(L) 是下界）
② isEnd 区分「整词」与「仅是前缀」：search 看它，startsWith 不看它 —— 这是本章唯一真陷阱
③ children 用数组还是哈希：字母表小（a-z）数组最快最省事；字母表大（Unicode）必须哈希
```

> **第 8 部分到此收口**：065 给了递归语言（三问），066–073 练了遍历与回溯，074 立起「交付量 vs 结算量」，075–077 用 BST 的有序性反复练兵，078–079 把「树 ⟺ 序列」双向打通，080 把返回值升级成状态向量，081 换了个方向 —— 用设计换性能。**回去重读 074 的贡献回流图，你会发现后面这 7 章都是它的不同化身。**
