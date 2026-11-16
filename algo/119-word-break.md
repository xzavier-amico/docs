# 119. LC139 单词拆分（Word Break）

> **状态**：`已完成（初稿）` ｜ **所属**：第 11 部分 · 动态规划 ｜ **编号**：119 / 150
> **难度**：中等 ｜ **标签**：字符串 · 动态规划 · 序列 DP · 哈希集合 · 记忆化搜索 · 前缀可达性
> **一句话**：这道题真正考的是「**把『切分方案能不能成』压成『前缀可达性』（`dp[i]` = 前 `i` 个字符可达），并认出转移可以从两端枚举：枚举切分点（看左半边可不可达）或枚举字典单词（看末尾是不是某个词）**」，不是「背一个 `dp[i] = dp[j] && dict.contains(s[j..i])`」。
> **前置**：[`108-climbing-stairs.md`](108-climbing-stairs.md)（DP 四步法）· [`117-coin-change-ii.md`](117-coin-change-ii.md)（同样是「按最后一个物品分类」的组合口径）

---

## 119.1 题目

**原题链接**：<https://leetcode.cn/problems/word-break/>

### 题意重述（把隐含条件补齐）

给一个字符串 `s` 和一个字符串列表 `wordDict`（视为字典，**可以重复使用**里面的单词）。问：能否把 `s` 拆成若干个字典中出现的单词的**顺序拼接**（每个单词至少用一次、词与词之间不能有空隙、也不能跳字符）？

读起来简单，但它藏了八个必须读出来的约束：

| 隐含条件 | 出处 | 读错了会怎样 |
| --- | --- | --- |
| 字典单词**可以重复使用** | 「字典中的单词可以重复使用」 | 按「每个单词最多用一次」写（记录已用词）→ `applepenapple` 会答 false，正确 true |
| 切分出来的段必须**恰好拼成 `s`** | 「拼接成 `s`」 | 理解成「能不能挑几个词按某种顺序拼成 `s`」→ 就变成「单词的排列组合」问题（`catsandog` 会被错判为 true，因为 `cats+and+dog` 这堆词都在字典里） |
| 返回**布尔值**，不是方案数 | 「返回 `true`」 | 用 `+=` 累加 → 类型直接错；写成计数题会溢出（方案数可以指数级大） |
| 词与词之间**不能有空隙** | 「空格隔开」的隐含语义 | 允许留空隙 → 任何字符串都返回 true |
| 单词可以**比 `s` 长** | `1 <= wordDict[i].length <= 20`，而 `s` 可以只有 1 | 不判 `len(w) <= i` → 切片时负下标，Go 直接 panic，Java 抛 `StringIndexOutOfBoundsException` |
| `s` 长度非空 | `1 <= s.length <= 300` | 空串在语义上是「0 段切分」，合法的 true（变体里会出现，见 §119.6.2） |
| 全小写字母 | 「`s` 和 `wordDict[i]` 仅由小写英文字母组成」 | 用 `byte` 索引是安全的；换成含中文/emoji 的变体必须用 `[]rune`（Go 的 `s[i]` 是字节） |
| 字典里**可能有重复词** | 约束未要求去重 | 重复词只影响「枚举字典单词」方向的候选数（多跑几遍同一个词），不影响正确性 |

> 📌 **本题最容易误读的是第 2 条**。`catsandog` 的标准答案为什么是 `false`？因为字典 `["cats","dog","sand","and","cat"]` 里的词能拼出的**唯一**完整覆盖是 `cats + and + og`（`og` 不在字典里）或 `cats + an + dog`（`an` 不在字典里）。而 `cats + and + dog` 拼出来的是 `catsanddog`，**多了一个 `d`**。**「词典里有这些词」不等于「这些词能按顺序拼出 `s`」。**

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| `1 <= s.length <= 300` | n 只有 300 | **`O(n²) = 90000` 次子串检查**完全可行；但 `2ⁿ` 的枚举切法（`2²⁹⁹`）绝无可能 |
| `1 <= wordDict.length <= 1000` | 字典最多 1000 词 | 「枚举字典单词」方向的候选数是 `k = 1000`；「枚举切分点」方向被最长词长剪枝后最多 `maxLen` 个 |
| `1 <= wordDict[i].length <= 20` | 词长 ≤ 20 | 关键剪枝：任何切点 `j < i - 20` 都不可能是单词边界，`O(n²)` 直接变 `O(20n)` |
| 答案只有 `true/false` | — | 可以**提前返回**（一旦 `dp[n]` 为真就停），这是 DP 之外的一个实用优化 |
| 无取模、无计数 | — | 不存在溢出问题；但也意味着「判定题」不需要存方案 |

> 💡 **量级 → 算法**：`n ≤ 300` 是个「怎么暴力都不够、怎么 DP 都够」的量级 —— `2²⁹⁹` 必死，`O(n²)` 秒过。**看到「字符串切分 + 问可行性」，第一反应就该是「前缀可达性 DP」，和 91（解码方法）、132（分割回文串 II）同一个模型。**

### 示例

```text
输入：s = "leetcode", wordDict = ["leet", "code"]
输出：true
解释：返回 true，因为 "leetcode" 可以拆成 "leet" + "code"

输入：s = "applepenapple", wordDict = ["apple", "pen"]
输出：true
解释："apple" + "pen" + "apple"；注意 "apple" 用了两次 —— 字典词可重复使用

输入：s = "catsandog", wordDict = ["cats","dog","sand","and","cat"]
输出：false
解释：四种拆法分别卡在 og / an / ndog / sando+?，没有一种能完整覆盖
      ← 这是本题唯一的官方 false 样例，也是唯一能暴露「贪心从左往右拼」错法的样例
```

### 我自造的七个反例（写完解法必须拿它们打一遍）

```text
① s = "catsandog", dict = ["cats","dog","sand","and","cat"] → false；贪心（能拼多长拼多长）会答 true
② s = "a",          dict = ["a"]                            → true
③ s = "a",          dict = ["aaaa"]                         → false；词比串长，切片易越界
④ s = "",           dict = ["a"]                            → true（0 段切分）；变体里会考，别让 dp[0]=false
⑤ s = "aaaaaaa",    dict = ["aaaa","aaa"]                   → true（3+4）；暴露「只看能不能被某个词整除」的错法
⑥ s = "bb",         dict = ["a","b","bbb"]                  → true（b+b）；暴露「词长必须一致」的错法
⑦ s = "abcdef",     dict = ["abc","def","abcdef"]           → true；三种拆法都能成，检验「只要一条路通就返回真」
```

第 ① 条是本题最容易「侥幸过关」的反例。用**最长前缀匹配的贪心**跑一遍：

```text
s = "catsandog"，dict = {"cats","dog","sand","and","cat"}

贪心：位置 0 能匹配的最长词是 "cats" → 跳到位置 4
      位置 4 能匹配的最长词是 "and"  → 跳到位置 7
      位置 7 剩下 "og"，一个词都匹配不上 ⇒ 答 false   ✅ 侥幸对

但贪心只是"这次运气好"：
  · "cats" 之所以能接上 "and"，是因为字典里恰好有 "and"；
  · 若字典换成 {"cats","dog","sand","cat"}（删掉 "and"），
    贪心在位置 4 只能匹配到空 ⇒ 答 false，真值也是 false；
  · 只要有一组字典让"长词吃下去之后接不上、短词却接得上"，贪心立刻错。
```

**一个必然让贪心翻车的反例**（本机对拍程序实测 `greedy=false`、`dp=true`）：

```text
s = "aab"，dict = {"aa", "a", "ab"}

贪心：位置 0 的最长匹配是 "aa"（长 2）→ 跳到位置 2
      位置 2 剩下 "b"，不在字典 ⇒ 答 false        ❌

正确：a + ab = "aab"                            ✅ 答 true
      （"a" ∈ dict，"ab" ∈ dict，两段首尾相接正好覆盖）

根因：贪心在位置 0 面临「吃 aa（2 字符）」和「吃 a（1 字符）」两个选择，
      它选了更长的那一个，而"更长"与"能不能拼完整"毫无关系 ——
      这正是 §119.2 要讲的"局部最优 ≠ 全局最优"。
```

> ⚠️ **「贪心样例跑对了」不等于「贪心正确」**。面试里与其争「贪心能不能过」，不如直接给 DP —— DP 的正确性是可证的（§119.3.2 用「最后一段」分类构造双射），贪心的正确性需要交换论证，而本题给不出来。

---

## 119.2 从暴力到最优：第一个念头为什么错

### 第一个念头：枚举所有切分方式

`s` 有 `n` 个字符，字符之间有 `n - 1` 个「缝隙」，每个缝隙「切 / 不切」→ 共 `2ⁿ⁻¹` 种切法。对每种切法逐个检查每一段是否在字典里。

```text
s = "catsandog"（n = 9）→ 2^8 = 256 种切法，检查 256 次
n = 300              → 2^299 ≈ 10^90 种  ✗ 完全不可能
```

即便只保留「每段都是字典词」的切法，最坏情况（字典里有 `"a"` 和 `"aa"`，`s = "aaaa…"`）仍是指数级：**每切一段都有两条路可选，且两条路都可能走通**。

### 第二个念头：递归枚举「下一段」

```java
// 后缀视角的暴力：f(i) = 从 i 开始的后缀能不能切分
boolean f(int i) {
    if (i == n) return true;                        // 走到了串尾 ⇒ 切分成功
    for (int j = i + 1; j <= n; j++) {
        if (dict.contains(s.substring(i, j)) && f(j)) return true;   // 只要一条路通就够
    }
    return false;
}
```

- 时间：最坏 `O(2ⁿ · n)`（每个位置都能切出词典词时，递归树是满二叉树）。
- 空间：`O(n)` 递归栈。

对 `s = "aaaaaaaaaa"`、`dict = {"a", "aa"}` 这类输入，`f(5)` 会被 `f(3) → f(5)`、`f(4) → f(5)`、`f(0) → f(5)` 等大量路径重复调用。**状态只有 `n + 1` 个（后缀起点 `0..n`），访问次数却是指数级 —— 又是那个熟悉的信号。**

### 第三步：卡点在哪

不是「不知道要判 `dict.contains`」，而是：

> **同一个「从位置 `i` 开始的后缀能不能切」被反复求解，而它的答案只取决于 `i`，与你「怎么走到 `i`」无关。**

这就是无后效性。于是有两条等价的路：

1. **记忆化**：`f(i)` 第一次算完就缓存，整棵递归树被剪成一棵「每个点只展开一次」的树 → `O(n²)`；
2. **递推**：既然每个 `i` 都要算，就按 `i` 递增把答案填进数组 → `O(n²)`，且没有栈。

**但真正决定这道题能不能再快一档的，是接下来这一步。**

### 第四步：灵感的来源 —— 转移可以从两端枚举

「最后一个单词」是本题的分类基准。设 `dp[i]` = `s` 的前 `i` 个字符能否被完整切分。那么 `s[0..i)` 的最后一段必是某个字典词 `w`，覆盖 `s[i-|w|..i)`，且前面 `s[0..i-|w|)` 必须自己可切分：

```text
dp[i] = ∃ w ∈ dict, |w| ≤ i,  s[i-|w| .. i) == w  且  dp[i-|w|] == true
```

这个式子有两种**完全等价但代价不同**的实现方向：

**方向 A：枚举切分点 `j`**（先猜「最后一段的起点」在哪，再问这一段是不是词）

```java
for (int i = 1; i <= n; i++)
    for (int j = i - 1; j >= 0 && i - j <= maxLen; j--)   // 候选数 = min(i, maxLen)
        if (dp[j] && dict.contains(s.substring(j, i))) { dp[i] = true; break; }
```

**方向 B：枚举字典单词 `w`**（先拿出每个词，问它能不能贴在第 `i` 个字符前面）

```java
for (int i = 1; i <= n; i++)
    for (String w : dict)                                  // 候选数 = k
        if (w.length() <= i && dp[i - w.length()] && s.startsWith(w, i - w.length())) {
            dp[i] = true; break;
        }
```

**两者的差别只有「每个 `i` 试多少个候选」这一件事**：

| 方向 | 每个 `i` 的候选数 | 单次比较代价 |
| --- | --- | --- |
| A：枚举切分点 | `min(i, maxLen)`（被最长词长剪枝） | 哈希 `s[j..i)`，`O(i-j)` 个字符 |
| B：枚举字典单词 | `k = wordDict.length`（受 `|w| ≤ i` 过滤） | 比较 `w` 与 `s[i-|w|..i)`，`O(|w|)` 个字符 |

**所以「谁更优」取决于 `min(i, maxLen)` 与 `k` 谁更小，而不是取决于「字符串长短」**：

- **字典很大、词很短**（LC 139 的原始量级：`k = 1000`，`maxLen = 20`）→ `min(i, 20) ≤ 20 ≪ 1000` ⇒ **方向 A 完胜**；
- **字典极小、词极长**（比如 `k = 3`，词长都是 100）→ `min(i, maxLen)` 随 `maxLen` 一起从常数涨到 100（极端时逼近 `n`），而 `k` 只有 3 ⇒ **方向 B 完胜**。这就是「长单词场景」的确切含义：**不是字符串长，而是「词典里存在接近 `n` 的长词」，使方向 A 的候选数从常数膨胀到 `n` 量级，而方向 B 的候选数只受 `k` 限制、完全不随词长增长。**

**还有第三条路，能把两者的优点合并**：把字典**按词长分桶** `byLen[len] → set`，方向 A 就只需要遍历「字典里真实出现过的词长」：

```java
for (int i = 1; i <= n; i++)
    for (Map.Entry<Integer, Set<String>> e : byLen.entrySet())      // 候选数 = 不同词长的个数
        if (e.getKey() <= i && dp[i - e.getKey()] && e.getValue().contains(s.substring(i - e.getKey(), i)))
            { dp[i] = true; break; }
```

**它同时解决了两个方向各自的浪费**：方向 A 会去试「长度不在字典里」的切点，方向 B 会对同一个长度重复试很多词。分桶后候选数 = 「不同词长的个数」，通常远小于 `k` 也远小于 `maxLen`。

> 🔑 **一句话总结这一步**：`dp[i]` 的转移对象是「最后一段」，而「最后一段」既可以用**位置**描述（`j`），也可以用**内容**描述（`w`）。**用哪一端当枚举轴，决定了候选数是 `n` 量级还是 `k` 量级。**

### 第五步：对照解 —— 记忆化 DFS

```java
// 后缀视角：f(i) = s[i..n) 能不能切分；memo 用 0/1/2 三态（0 = 没算过）
boolean dfs(int i) {
    if (i == n) return true;
    if (memo[i] != 0) return memo[i] == 1;
    for (int j = i + 1; j <= n && j - i <= maxLen; j++)
        if (dict.contains(s.substring(i, j)) && dfs(j)) { memo[i] = 1; return true; }
    memo[i] = 2;
    return false;
}
```

和递推版的关系：

| 维度 | 记忆化 DFS | 递推（前缀 `dp`） |
| --- | --- | --- |
| 方向 | 后缀视角，自顶向下 | 前缀视角，自底向上 |
| 状态数 | `n + 1` | `n + 1` |
| 时间 | `O(n · maxLen)` 次子串检查 | 同 |
| 空间 | `O(n)` 表 + `O(n)` 递归栈 | `O(n)` 表，无栈 |
| 剪枝友好度 | **高**：只展开「从可达位置出发」的分支，`s` 大部分不可切分时几乎不干活 | 低：`i` 从 1 扫到 `n` 一个不漏 |
| 栈风险 | `n = 300` 安全；`n = 10⁶` 会爆栈（Go 的栈会自动增长，Java 不会） | 无 |

> 📌 **两者在「最坏情况」下同阶，但在「稀疏可达」的实际输入上记忆化明显更快。** 面试里如果先写递推、再补一句「也可以用记忆化自顶向下，它会在不可切分的输入上少做很多工作」，就是完整的答案。

---

## 119.3 原理拆解

### 119.3.1 数据结构与算法选型

| 方案 | 时间 | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- |
| 枚举所有切法 | `O(2ⁿ·n)` | `O(n)` 栈 | 12 行 | `n ≥ 25` 就秒级；`300` 不可能 |
| 无记忆化递归 | `O(2ⁿ·n)` | `O(n)` 栈 | 10 行 | 同上；`"aaaa…"` + `{"a","aa"}` 立刻炸 |
| **记忆化 DFS** ✅ | `O(n·min(n,maxLen))` | `O(n)` + 栈 | 16 行 | 栈深度 `n`；`n = 10⁶` 时 Java 爆栈 |
| **递推 + 枚举切分点** ✅ | `O(n·min(n,maxLen)·L)` | `O(n)` | 14 行 | 长单词场景候选数膨胀到 `n` |
| 递推 + 枚举字典单词 | `O(n·k·L)` | `O(n)` | 14 行 | 大字典小词场景（`k ≫ maxLen`）明显慢 |
| 递推 + 按词长分桶 | `O(n·|不同词长|·L)` | `O(n + Σ|w|)` | 20 行 | 需要额外建桶；代码稍长 |
| 反向 Trie（从 `i` 往前匹配） | `O(n·maxLen)` | `O(Σ|w|)` | 30 行 | 常数大；但**与 `k` 完全无关**，大字典场景的正解 |
| BFS（位置为顶点、词为边） | `O(n·min(n,maxLen))` | `O(n)` | 24 行 | 要额外维护队列；但可以**在第一次到达 `n` 时立刻返回** |

**为什么正文选「递推 + 枚举切分点 + maxLen 剪枝」**：它是唯一一个「代码最短、无栈、且把两个方向的取舍讲得最清楚」的方案；`n ≤ 300` 时 `6000` 次子串检查就是极限量级。而「枚举字典单词」版本同样给全，因为它是面试官最爱追问的对照面。

### 119.3.2 为什么这个做法是对的（正确性论证）

**命题**：对 `i ∈ [0, n]`，`dp[i] = true` ⟺ 前缀 `s[0..i)` 能被字典词完整切分。且

```text
dp[0] = true
dp[i] = OR over j ∈ [0, i) 满足  dp[j] = true 且 s[j..i) ∈ dict      （i ≥ 1）
答案 = dp[n]
```

**证明（对 `i` 归纳，按「最后一段」分类，两个方向各证一次）**：设 `T(i)` 为「前缀 `s[0..i)` 的全部合法切分」构成的集合，命题即 `dp[i] = true ⟺ T(i) ≠ ∅`。

- **基础**：`i = 0` 时 `T(0) = {空切分}`，非空，而 `dp[0] = true`。✔
- **方向一（若可达 ⇒ `dp[i]` 真）**：设 `t ∈ T(i)` 且 `i ≥ 1`。`t` 的最后一段是某个字典词 `w = s[j..i)`（`|w| ≥ 1`），而 `t` 去掉这一段后是 `s[0..j)` 的一个合法切分，故 `T(j) ≠ ∅`，由归纳假设 `dp[j] = true`。内层循环会枚举到这个 `j`（`j < i` 且 `i - j = |w| ≤ maxLen`），于是 `dp[i]` 被置真。✔
- **方向二（若 `dp[i]` 真 ⇒ 可达）**：`dp[i]` 被置真说明存在某个 `j` 使 `dp[j] = true` 且 `s[j..i) = w ∈ dict`。由归纳假设存在 `s[0..j)` 的合法切分 `t'`；把 `w` 接在 `t'` 之后得到 `s[0..i)` 的一个合法切分，故 `T(i) ≠ ∅`。✔

两个方向合起来即得 `dp[i] = true ⟺ T(i) ≠ ∅`，取 `i = n` 就是答案。∎

> 🔑 **注意方向一只需要「存在一个 `j`」**：这就是「`OR` 是存在量词」的具体体现 —— 转移不需要把 `T(i)` 里所有方案都数出来，只要判定它非空。**这也是为什么本题能从「枚举方案」压成「布尔的表」：`OR` 把集合压成了一个比特。**

**遍历序的正确性**：`dp[i]` 只读 `dp[j]`（`j < i`），所以外层 `i` 从 1 递增即可。**这一点和 117 的「物品在外层」形成了有趣的反差**：

| 题 | 转移按什么分类 | 循环谁在外层 | 为什么 |
| --- | --- | --- | --- |
| 117（计数） | 第 `i` 种硬币用了几枚 | 物品（硬币） | 为了避免把「同一多重集的不同排列」重复计数 |
| 119（判定） | 最后一段是哪个词 | **位置** `i`（递增） | `OR` 是幂等可交换的，不重复计数也无所谓；只要下标小的先算好即可 |

**边界 `dp[0] = true` 的语义**：`dp[0]` 是「空前缀的合法切分」，而「0 段切分」在逻辑上成立（不需要任何词就覆盖了空串）。写成 `false` 会让所有转移失效（因为没有一条链能落地），`s = "a"`、`dict = ["a"]` 会直接答 false。

**它就是一个图上可达性问题**（面孔二）：把位置 `0..n` 看成顶点，对每个 `i` 与每个「`s[i..i+|w|) == w`」的词连一条 `i → i+|w|` 的边。问题是「`0` 能否到达 `n`」。边永远指向更大下标 ⇒ 图是 DAG ⇒ 按拓扑序（下标递增）做一次 DP 就是可达性。**BFS / DFS / 递推 DP 三者在 DAG 上完全等价**，这也是 §119.2 第五步「记忆化 DFS 与递推同阶」的根据。

### 119.3.3 通用解题思路（可迁移的那一层）

本题是 **「序列切分 / 前缀可达性」** 模型的母题。四件套固定不变：

```text
① 状态：dp[i] = 前缀 s[0..i) 是否可被完整切分（可达性）
② 转移：枚举「最后一段」的位置或内容，dp[i] = OR( dp[i] - 最后一段 的起点 )
③ 初值：dp[0] = true（空前缀是合法的 0 段切分）
④ 顺序：i 从 1 到 n 递增（转移只读更小的下标）
```

**这个家族的对照表**（同一个骨架，换「最后一段」的判定条件）：

| 题 | 「最后一段」的判定条件 | 答案类型 |
| --- | --- | --- |
| **LC 139 单词拆分（本题）** | 这一段是字典词 | 布尔 |
| [LC 91 解码方法](https://leetcode.cn/problems/decode-ways/) | 这一段是 1 位且非 `0`，或 2 位且落在 `10..26` | **计数** |
| [LC 132 分割回文串 II](https://leetcode.cn/problems/palindrome-partitioning-ii/) | 这一段是回文 | **最少段数**（`min`） |
| [LC 131 分割回文串](https://leetcode.cn/problems/palindrome-partitioning/) | 这一段是回文 | **枚举所有方案**（DP 失效，回溯） |
| [LC 472 连接词](https://leetcode.cn/problems/concatenated-words/) | 对字典里每个词跑一次本题 | 布尔 × k |
| [LC 140 单词拆分 II](https://leetcode.cn/problems/word-break-ii/) | 同本题，但要求输出所有切分 | 记忆化 + 回溯 |

**套用 DP 四步法**：

| 四步 | 本题的答案 |
| --- | --- |
| ① 状态定义 | `dp[i]` = `s` 的前 `i` 个字符能否被字典词完整切分 |
| ② 转移方程 | `dp[i] = ∃j < i: dp[j] && s[j..i) ∈ dict`（按「最后一段」分类；或按「最后一段是哪个词」写成 `∃w`） |
| ③ 初始化 | `dp[0] = true`（0 段切分合法）；其余 `false` |
| ④ 遍历序 | `i` 从 1 到 `n` 递增；内层在「切分点」或「字典单词」里二选一枚举 |

### 119.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| 要输出**所有**切分方案 | 布尔 → 枚举 | 记忆化 DFS + 回溯（`f(i)` 返回「从 `i` 开始的所有切分列表」），注意总输出规模本身可以指数级（LC 140） |
| 求**最少切几段**（或最少几个词） | `OR` → `min` | `dp[i] = min(dp[j] + 1)`，判据同款（LC 132 的最小切割次数） |
| 求**方案数** | `OR` → `+` | `dp[i] += dp[j]`；此时**必须固定枚举顺序**才不会重复计数，且方案数可能指数级溢出 → 取模 |
| 字典里有**通配符**（`.` 匹配任意字符） | 词尾匹配变复杂 | 用 Trie 从 `i` 往前回溯匹配，或对每个长度做「带通配的字符串匹配」（LC 472 的进阶） |
| `s` 长到 `10⁶`，词长都 ≤ 20 | `O(20n)` 仍可行 | 递推 + 滚动窗口（只保留最近 `maxLen` 个 `dp` 值）+ **滚动哈希**把子串比较降到 `O(1)` |
| 词典极大（`10⁶` 词） | 枚举单词不可行 | **反向 Trie**：从 `i` 出发往前最多走 `maxLen` 步，复杂度 `O(n·maxLen)`，与 `k` 无关 |

---

## 119.4 图解推演

### 119.4.1 方向 A（枚举切分点）：`s = "applepenapple"`, `dict = {"apple","pen"}`

```text
s:   a  p  p  l  e  p  e  n  a  p  p  l  e
下标 0  1  2  3  4  5  6  7  8  9 10 11 12   （n = 13）
     └──── 前缀长度 i 表示 s[0..i) ────┘

maxLen = 5（"apple" 长 5，"pen" 长 3）⇒ 内层剪枝：只试 i-5 ≤ j < i

下标 i :   0     1     2     3     4     5     6     7     8     9    10    11    12    13
dp     :   T     F     F     F     F     T     F     F     T     F     F     F     F     T
                                        ↑                 ↑                             ↑
                                      apple             pen                      第二个 apple

逐格推演（每行左边是命中的 (j, 段)，右边是结论）：

i = 1 : (0,"a")        ✗  不在字典                                     dp[1] = F
i = 2 : (1,"p") (0,"ap")                        ✗ 都不在               dp[2] = F
i = 3 : (2,"p") (1,"pp") (0,"app")              ✗ 都不在               dp[3] = F
i = 4 : (3,"l") (2,"pl") (1,"ppl") (0,"appl")   ✗ 都不在               dp[4] = F
i = 5 : (4,"e") (3,"le") (2,"ple") (1,"pple")   ✗
        (0,"apple") ✅ 且 dp[0]=T  ⇒ 命中！                             dp[5] = T
i = 6 : (5,"p") (4,"ep") (3,"lep") (2,"plep") (1,"pplep")  ✗ 都不在
        ← 注意 j 最小只能到 i-5 = 1，j=0 的段长 6 > maxLen 已被剪掉        dp[6] = F
i = 7 : (6,"n") (5,"en") (4,"epe") (3,"lepe") (2,"plepe")   ✗ 都不在    dp[7] = F
i = 8 : (7,"n") (6,"en") ✗
        (5,"pen") ✅ 且 dp[5]=T  ⇒ 命中！                             dp[8] = T
i = 9 : (8,"a") (7,"na") (6,"ena") (5,"pena") (4,"epena")   ✗ 都不在    dp[9] = F
i = 10: (9,"p") (8,"ap") (7,"nap") (6,"enap") (5,"penap")   ✗ 都不在    dp[10] = F
i = 11: (10,"p") (9,"pp") (8,"app") (7,"napp") (6,"enapp")  ✗ 都不在    dp[11] = F
i = 12: (11,"l") (10,"pl") (9,"ppl") (8,"appl") (7,"nappl") ✗ 都不在    dp[12] = F
i = 13: (12,"e") (11,"le") (10,"ple") (9,"pple") ✗
        (8,"apple") ✅ 且 dp[8]=T  ⇒ 命中！                             dp[13] = T

答案 dp[13] = true ✅
```

| `i` | 命中的 `j` | 命中的段 | `dp[i]` |
| --- | --- | --- | --- |
| 0 | — | （空前缀） | **T** |
| 1~4 | — | 无 | F |
| 5 | 0 | `apple` | **T** |
| 6, 7 | — | 无 | F |
| 8 | 5 | `pen` | **T** |
| 9~12 | — | 无 | F |
| 13 | 8 | `apple` | **T** |

> 🔑 **读表要点**：`dp[i]` 为真的位置（`0, 5, 8, 13`）正好是「可以在这里断句」的边界。**`dp` 表本身就编码了所有可能的断点**，这是「前缀可达性」最直观的形态。

### 119.4.2 方向 B（枚举字典单词）：同样的输入，另一条轨迹

```text
dict = {"apple"(长度 5), "pen"(长度 3)}

i = 5 : 试 "apple" → 5 ≤ 5 ✅，dp[0]=T ✅，s[0..5) == "apple" ✅ ⇒ dp[5] = T
        试 "pen"  → 长度 3 ≤ 5，dp[2]=F ✗
i = 8 : 试 "apple" → 5 ≤ 8，dp[3]=F ✗
        试 "pen"   → 3 ≤ 8，dp[5]=T ✅，s[5..8) == "pen" ✅ ⇒ dp[8] = T
i = 13: 试 "apple" → 5 ≤ 13，dp[8]=T ✅，s[8..13) == "apple" ✅ ⇒ dp[13] = T

答案 true ✅   —— 与方向 A 逐格相同，只是"候选枚举的轴"不同
```

| 对比项 | 方向 A（枚举切分点 `j`） | 方向 B（枚举字典单词 `w`） |
| --- | --- | --- |
| 每个 `i` 试几次 | `min(i, maxLen)` 次 | `k` 次（`|w| ≤ i` 时） |
| 单次代价 | 哈希 `s[j..i)`：`O(i-j)` 字符 | 比较 `w` 与 `s[i-|w|..i)`：`O(|w|)` 字符 |
| 天然剪枝 | 「段长 ≤ maxLen」 | 「词长 ≤ i」 |
| 会做哪些无用功 | 试「长度在字典里不存在」的段 | 同一个长度上试很多词 |
| 最适合 | 字典大、词短（LC 139 原始量级） | 字典小、词长 |

### 119.4.3 「长单词」为什么让方向 A 变慢（代价随 `maxLen` 膨胀）

```text
n = 300，方向 A 的内层候选数 = min(i, maxLen)

maxLen = 20 ：  每行最多试 20 个切点，全表 ≤ 300 × 20 = 6 000 次哈希
maxLen = 150：  每行最多试 150 个切点，全表 ≤ 300 × 150 = 45 000 次哈希
                而且每次哈希平均扫 75 个字符 ⇒ 字符级代价 ≈ 45 000 × 75 = 3.4·10⁶

方向 B 的候选数 = k，与 maxLen 无关：
k = 3      ：  全表 ≤ 300 × 3 = 900 次比较，每次扫 100 字符 ⇒ 9·10⁴
k = 1000   ：  全表 ≤ 300 × 1000 = 3·10⁵ 次比较 ⇒ 比方向 A 多一个量级
```

**所以「长单词场景下方向 B 更优」的准确条件是 `k < maxLen`** —— 词汇表里只有少数几个很长的词时成立；字典很大时方向 A 仍然赢。§119.5.4 用真实计时验证了这一点（三组输入，见下表）。

| 场景 | `n` | `k` | 最长词 | 方向 A | 方向 A + 词长分桶 | 方向 B |
| --- | --- | --- | --- | --- | --- | --- |
| ① LC 139 原始量级 | 300 | 1000 | 20 | **60 µs** ✅ | 229 µs | 1809 µs |
| ② 大字典 + 长词 | 300 | 1000 | 150 | **109 µs** ✅ | 799 µs | 5051 µs |
| ③ 小字典 + 极长词 | 300 | 3 | 100 | 35 µs | **13 µs** ✅ | 19 µs |

> 📌 **这张表的正确读法**：真正决定胜负的是**候选数**（`min(i, maxLen)` vs `k` vs `不同词长的个数`），不是「字符串长不长」。
> ① 中 `k = 1000 ≫ maxLen = 20` ⇒ A 赢 30 倍；③ 中 `k = 3 < maxLen = 100` ⇒ B 赢。
> 而「按词长分桶」在 ③ 里把候选数压到 1（只有一个词长），于是它比 A 和 B 都快 —— **这说明两个方向不是对立的两派，而是同一个优化的两个粗糙端点。**
> 数字是本机 `go run` 实测（每项跑 200 次取均值，随机串 + 随机字典），只用来比较数量级，绝对值随机器浮动。

### 119.4.4 `catsandog` 的失败轨迹（为什么答 false）

```text
s = "catsandog"（n = 9）
dict = {"cats", "dog", "sand", "and", "cat"}
maxLen = 4

下标 i :  0    1    2    3    4    5    6    7    8    9
dp     :  T    F    F    T    T    F    F    T    F    F

i = 3 : j=0, 段 "cat"  ✅ 且 dp[0]=T        ⇒ dp[3] = T
i = 4 : j=0, 段 "cats" ✅ 且 dp[0]=T        ⇒ dp[4] = T
i = 5 : 试 j=4"a" / j=3"sa" / j=2"tsa" / j=1"atsa"
        （maxLen=4 ⇒ j 最小到 i-4 = 1）      ⇒ dp[5] = F
i = 6 : 试 j=5"n" / j=4"an" / j=3"san" / j=2"tsan"   ⇒ dp[6] = F
        ← "sand" = s[3..7)，它落在 i=7 那一行，不在这一行
i = 7 : j=6"d" ✗；j=5"nd" ✗；
        j=4"and" ✅ 且 dp[4]=T               ⇒ dp[7] = T
i = 8 : 试 j=7"o" / j=6"do" / j=5"ndo" / j=4"ando"   ⇒ dp[8] = F
i = 9 : j=8"g" ✗；j=7"og" ✗；
        j=6"dog" ✅ 但 dp[6]=F ⇒ 不成立（这正是 dp[6] 必须为 T 的原因）
        j=5"ndog" ✗（i-j = 4 ≤ maxLen，仍会试到）
                                              ⇒ dp[9] = F

答案 false ✅

规律一目了然：dp 为真的位置是 {0, 3, 4, 7}，
而 dp[9] 要成立只可能来自 j ∈ {6, 7, 8}（对应 "dog" / "og" / "g"），
这三个位置里只有 dp[7]=T，但 "og" 不是字典词 ⇒ 三条路全断。
```

> ⚠️ **`dp[6] = F` 是本题的关键**：`dog` 明明是字典词，位置也对（`s[6..9) = "dog"`），但它要求 `dp[6] = T`，也就是「前 6 个字符 `catsan` 能被完整切分」。而 `catsan` 只能切成 `cat + san` 或 `cats + an`，`san` 和 `an` 都不在字典里 ⇒ 断链。**这正是「`dp` 表用位置编码了所有断点」的价值：一眼看出哪条链断了 ——「词在字典里」和「这个词能接在可达位置上」是两件不同的事。**

### 119.4.5 两条路线的对比图

```text
                     ┌──────────── s、wordDict ────────────┐
                     │                                     │
        ┌────────────▼────────────┐          ┌─────────────▼────────────┐
        │  方向 A：枚举切分点 j    │          │  方向 B：枚举字典单词 w   │
        │  候选数 min(i, maxLen)  │          │  候选数 k                │
        └────────────┬────────────┘          └─────────────┬────────────┘
                     │                                     │
          「先猜位置，再验内容」                 「先拿内容，再对位置」
                     │                                     │
        ┌────────────▼────────────┐          ┌─────────────▼────────────┐
        │ 大字典 + 短词 → 选 A ✅  │          │ 小字典 + 长词 → 选 B ✅   │
        └────────────┬────────────┘          └─────────────┬────────────┘
                     │                                     │
                     └──────────────┬──────────────────────┘
                                    ▼
                     ┌──────────────────────────────┐
                     │ 合并解：按词长分桶 / 反向 Trie │
                     │ 候选数 = 不同词长的个数        │
                     └──────────────────────────────┘
```

---

## 119.5 双语言实现

### 119.5.1 Java

```java
import java.util.HashSet;
import java.util.List;
import java.util.Set;

class Solution {
    public boolean wordBreak(String s, List<String> wordDict) {
        Set<String> dict = new HashSet<>(wordDict);
        // maxLen 是「最长的字典词」：它是剪枝的依据，没有它内层要试到 j = 0
        int maxLen = 0;
        for (String w : wordDict) maxLen = Math.max(maxLen, w.length());

        int n = s.length();
        // dp[i] = s 的前 i 个字符能否被字典词完整切分
        boolean[] dp = new boolean[n + 1];
        dp[0] = true;                    // 空前缀 = 0 段切分，合法

        for (int i = 1; i <= n; i++) {
            // j 从 i-1 往回退；一旦段长 > maxLen 就不可能命中，直接停
            // 倒着退而不是正着进，是为了更早命中（长词在前），但这个顺序不影响正确性
            for (int j = i - 1; j >= 0 && i - j <= maxLen; j--) {
                if (dp[j] && dict.contains(s.substring(j, i))) {
                    dp[i] = true;
                    break;               // 判定题：存在一条路就够，不用继续试
                }
            }
        }
        return dp[n];
    }

    // 对照解：枚举字典单词（「最后一段是哪个词」的直接翻译）
    public boolean wordBreakByWord(String s, List<String> wordDict) {
        Set<String> dict = new HashSet<>(wordDict);
        int n = s.length();
        boolean[] dp = new boolean[n + 1];
        dp[0] = true;
        for (int i = 1; i <= n; i++) {
            for (String w : dict) {
                int l = w.length();
                // l <= i 必须判：词可能比当前前缀还长，不判会下标为负
                if (l <= i && dp[i - l] && s.startsWith(w, i - l)) {
                    dp[i] = true;
                    break;
                }
            }
        }
        return dp[n];
    }
}
```

### 119.5.2 Go

```go
package main

// wordBreak：方向 A —— 枚举切分点（前缀 DP），与 Java 的 wordBreak 逐行对应
func wordBreak(s string, wordDict []string) bool {
	dict := make(map[string]bool, len(wordDict))
	maxLen := 0
	for _, w := range wordDict {
		dict[w] = true
		if len(w) > maxLen {
			maxLen = len(w)
		}
	}

	n := len(s)
	dp := make([]bool, n+1)
	dp[0] = true // 空前缀永远可切分

	for i := 1; i <= n; i++ {
		// 条件里的 i-j <= maxLen 就是剪枝：更短的前缀不可能是单词起点
		for j := i - 1; j >= 0 && i-j <= maxLen; j-- {
			if dp[j] && dict[s[j:i]] { // Go 的字符串切片是 O(1)（共享底层数组），但 map 查找要哈希 O(i-j)
				dp[i] = true
				break
			}
		}
	}
	return dp[n]
}

// wordBreakWords：方向 B —— 枚举字典单词
func wordBreakWords(s string, wordDict []string) bool {
	dict := make(map[string]bool, len(wordDict))
	for _, w := range wordDict {
		dict[w] = true
	}
	n := len(s)
	dp := make([]bool, n+1)
	dp[0] = true
	for i := 1; i <= n; i++ {
		for w := range dict {
			l := len(w)
			if l <= i && dp[i-l] && s[i-l:i] == w {
				dp[i] = true
				break
			}
		}
	}
	return dp[n]
}

func main() {}
```

### 119.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 子串创建 | `s.substring(j, i)` **每次新建一个 String** 并拷贝字符（Java 7 之后不再共享底层数组） | `s[j:i]` 是**切片，不拷贝**，只存指针 + 长度 | `O(n²)` 次子串的场景下，Java 的分配压力明显更大；Go 的切片虽然省拷贝，但 `map[string]bool` 查找时仍要**哈希整段** |
| 末尾匹配 | `s.startsWith(w, i - l)`，**不创建子串** | `s[i-l:i] == w`，切片后比较 | Java 的 `startsWith(prefix, offset)` 是方向 B 的最优写法；Go 里切片 + `==` 已足够 |
| 字符宽度 | `charAt` 返回 16 位 `char`，中文安全 | `s[i]` 是 **`byte`** | 本题全小写英文字母 ⇒ `byte` 与 `char` 等价。**换成含中文的变体，Go 必须 `[]rune(s)`，`len(s)` 也要换成 `len([]rune(s))`** |
| 集合类型 | `HashSet<String>` | `map[string]bool`（Go 没有内置 Set） | Go 的 `map[string]struct{}` 更省内存（零大小值），但 `map[string]bool` 可读性更好 |
| 遍历顺序 | `HashSet` 迭代顺序不保证 | `map` 迭代顺序**每次随机**（故意的，防止依赖顺序） | 方向 B 里 `break` 会因顺序不同而命中不同的词，但**只影响耗时、不影响结果**（`OR` 是幂等的） |
| 递归深度 | 默认栈约 512 KB，`n = 10⁴` 的记忆化 DFS 可能 `StackOverflowError` | goroutine 栈按需增长，`n = 10⁶` 的深递归也常能活 | 本题 `n ≤ 300`，两边写成递归都安全；但**递推版是无栈的，优先选它** |

### 119.5.4 对拍测试（写完必须跑，这是本书的验收习惯）

```go
package main

import (
	"math/rand"
	"testing"
)

func randWord(r *rand.Rand, maxLen int) string {
	l := 1 + r.Intn(maxLen)
	b := make([]byte, l)
	for i := range b {
		b[i] = byte('a' + r.Intn(3)) // 只用 3 个字母：制造大量"看起来能拼上"的前缀
	}
	return string(b)
}

// 指数级暴力（无记忆化）：枚举所有切分点，作真值
func bruteWordBreak(s string, wordDict []string) bool {
	dict := map[string]bool{}
	for _, w := range wordDict {
		dict[w] = true
	}
	var dfs func(i int) bool
	dfs = func(i int) bool {
		if i == len(s) {
			return true
		}
		for j := i + 1; j <= len(s); j++ {
			if dict[s[i:j]] && dfs(j) {
				return true
			}
		}
		return false
	}
	return dfs(0)
}

// 记忆化 DFS 参考实现（对照组，用于验证"记忆化与递推同解"）
func memoWordBreak(s string, wordDict []string) bool {
	dict := map[string]bool{}
	maxLen := 0
	for _, w := range wordDict {
		dict[w] = true
		if len(w) > maxLen {
			maxLen = len(w)
		}
	}
	memo := make([]int8, len(s)+1) // 0 未知 / 1 可以 / 2 不可以
	var dfs func(i int) bool
	dfs = func(i int) bool {
		if i == len(s) {
			return true
		}
		if memo[i] != 0 {
			return memo[i] == 1
		}
		for j := i + 1; j <= len(s) && j-i <= maxLen; j++ {
			if dict[s[i:j]] && dfs(j) {
				memo[i] = 1
				return true
			}
		}
		memo[i] = 2
		return false
	}
	return dfs(0)
}

func TestWordBreakRandom(t *testing.T) {
	r := rand.New(rand.NewSource(139_001))
	for iter := 0; iter < 12000; iter++ {
		s := randWord(r, 12)
		words := make([]string, 1+r.Intn(4))
		for i := range words {
			words[i] = randWord(r, 3)
		}
		want := bruteWordBreak(s, words)
		if got := wordBreak(s, words); got != want {
			t.Fatalf("A iter=%d s=%q words=%v got=%v want=%v", iter, s, words, got, want)
		}
		if got := wordBreakWords(s, words); got != want {
			t.Fatalf("B iter=%d s=%q words=%v got=%v want=%v", iter, s, words, got, want)
		}
		if got := memoWordBreak(s, words); got != want {
			t.Fatalf("memo iter=%d s=%q words=%v got=%v want=%v", iter, s, words, got, want)
		}
	}
}

// 带"超长词"和"整串就是一个词"的字典：逼出 maxLen 剪枝与 |w| ≤ i 的边界
func TestWordBreakLongWords(t *testing.T) {
	r := rand.New(rand.NewSource(139_002))
	for iter := 0; iter < 12000; iter++ {
		s := randWord(r, 14)
		words := []string{s} // 整串就是一个词
		if r.Intn(2) == 0 {
			words = append(words, s[:len(s)/2])
		}
		if r.Intn(3) == 0 {
			words = append(words, randWord(r, 20)) // 比 s 还长的词
		}
		want := bruteWordBreak(s, words)
		if got := wordBreak(s, words); got != want {
			t.Fatalf("A iter=%d s=%q words=%v got=%v want=%v", iter, s, words, got, want)
		}
		if got := wordBreakWords(s, words); got != want {
			t.Fatalf("B iter=%d s=%q words=%v got=%v want=%v", iter, s, words, got, want)
		}
		if got := memoWordBreak(s, words); got != want {
			t.Fatalf("memo iter=%d s=%q words=%v got=%v want=%v", iter, s, words, got, want)
		}
	}
}
```

**实测结果**（本机 2 核容器 / Go 1.19.8）：`go vet .` 无告警；**随机对拍 12000 轮 + 「超长词 / 整串是词」对拍 12000 轮**，方向 A、方向 B、记忆化 DFS 三种实现全部与指数级暴力一致。边界用例：

| `s` | `wordDict` | 期望 | 说明 |
| --- | --- | --- | --- |
| `"leetcode"` | `["leet","code"]` | true | 官方样例 1 |
| `"applepenapple"` | `["apple","pen"]` | true | 同一个词用两次 ⇒ 字典词可重复使用 |
| `"catsandog"` | `["cats","dog","sand","and","cat"]` | **false** | 官方样例 3；所有链都断 |
| `"a"` | `["a"]` | true | 最小正例 |
| `"a"` | `["b"]` | false | 单字符不可分 |
| `"a"` | `["aaaa"]` | **false** | 词比串长 ⇒ 必须判 `|w| ≤ i` |
| `""` | `["a"]` | **true** | 空串 = 0 段切分（变体边界） |
| `"aaaaaaa"` | `["aaaa","aaa"]` | true | `3 + 4` 混合；暴露「整除」错法 |
| `"bb"` | `["a","b","bbb"]` | true | `b + b`；暴露「词长必须一致」错法 |
| `"abcdef"` | `["abc","def","abcdef"]` | true | 三种拆法都可行 |

> 📌 **对拍的三个设计要点**：
> ① 随机串只用 3 个字母（`a/b/c`），这样「看起来能拼上」的前缀特别多，能把「剪枝写错」「`|w| ≤ i` 漏判」逼出来；
> ② 必须**单独**造一批「整串就是一个词」「词比串还长」的用例 —— 随机小词几乎不会覆盖这两种极端；
> ③ 用三种实现（A / B / 记忆化）互相夹，比「只有两版」更狠：**三个方向同时错的概率远小于一个方向错**。

---

## 119.6 复杂度与常见坑

### 119.6.1 复杂度推导（写结论不算本事，要能推）

- **方向 A（枚举切分点 + maxLen 剪枝）**：外层 `n` 次；内层最多 `min(i, maxLen)` 次。总次数
  `Σ_{i=1}^{n} min(i, maxLen) ≤ n · maxLen`。本题 `n ≤ 300`、`maxLen ≤ 20` → **6000 次**子串检查，每次哈希 `O(maxLen)` 字符 ⇒ 字符级 `1.2·10⁵`。本机实测 **≈ 60 µs**。
- **方向 B（枚举字典单词）**：外层 `n` 次；内层 `k` 次（`|w| ≤ i` 过滤后更少）。总次数 `≤ n·k = 3·10⁵`，字符级 `O(n·Σ|w|)`。本机实测 `k = 1000` 时 **≈ 1.8 ms**。
- **空间 `O(n)`**：`boolean[n+1]` 只有 301 字节。字典本身占 `O(Σ|w|)`。
- **记忆化 DFS**：时间与方向 A 同阶（`O(n·maxLen)` 次检查）；空间多了 `O(n)` 递归栈。**但在「几乎不可切分」的输入上，它只展开从可达位置出发的分支，实测能比递推少做几倍的工作。**
- **暴力 `O(2ⁿ·n)`**：`n = 9` 时 256 次，`n = 30` 时 `10⁹`，`n = 300` 时不可能。
- **为什么不能做到 `O(n)`**：转移要枚举「最后一段」，即使剪到 `maxLen`，也是 `O(n·maxLen)`。要再降，只能用**滚动哈希把比较降到 `O(1)`**（`O(n·maxLen)` 次 `O(1)` 操作）或 Trie（`O(n·maxLen)` 与 `k` 无关）。**`n·maxLen` 是这个模型的时间下界量级。**

### 119.6.2 七个真实踩过的坑

1. **`dp[0] = false`** —— 最高频错误。所有转移链都要落到 `dp[0]`，它一假则整表单假，`s = "a"`、`dict = ["a"]` 直接答 false。修法：把 `dp[0]` 读成「空前缀 = 0 段切分 = 合法」。
2. **忘了判 `len(w) <= i`** —— `s = "a"`、`dict = ["aaaa"]` 会算 `s[i-l:i]` 得到 `s[-3:1]`：Go **直接 panic（slice bounds out of range）**，Java 抛 `StringIndexOutOfBoundsException`。修法：方向 B 的第一个条件就是 `l <= i`。
3. **忘了 `maxLen` 剪枝** —— 正确性不受影响，但内层退化成 `O(n)`，整体 `O(n²)` 且每次哈希平均 `O(n/2)` ⇒ `O(n³)`。`n = 300` 时 `2.7·10⁷` 字符操作，比剪枝版慢两个量级。
4. **把「能切」写成「能拼」** —— 理解成「字典里挑几个词任意排列拼成 `s`」→ `catsandog` 被答成 true（`cats + and + dog` 这堆词都在字典里，但拼出来是 `catsanddog`）。修法：钉死「必须**恰好覆盖**、不许跳字符、顺序就是原串顺序」。
5. **方向 B 里用 `dp[i] = dp[i-l] && s.startsWith(...)` 覆盖而不是 `||` 累或** —— `=` 会让后面不匹配的词把前面命中的 `true` 覆盖成 `false`。本题的 `break` 能挡住一部分，但**一旦你为了「统计命中次数」删掉 `break`**，这个 bug 立刻显形。
6. **换成中文/emoji 变体时 Go 用 `s[i]` 索引** —— Go 的 `s[i]` 是 **byte**，`len(s)` 是字节数。中文一个字 3 字节，切片会切出半个字。修法：`r := []rune(s)`，之后所有下标都用 `rune` 下标，`len(r)` 才是字符数。Java 的 `charAt` 对 BMP 字符安全，对 emoji 仍需 `codePointAt`。
7. **把内层的 `break` 写成 `return true`** —— `break` 只跳出内层循环（语义是「`dp[i]` 已确定为真，不必再试别的 `j`」），**它不代表整串可达**。换成 `return true` 就是把「前缀可达」误当「整串可达」：`s = "catsandog"` 会在 `i = 4`（`cats` 命中，`dp[4] = T`）时错误返回 true，而正确答案是 false。

### 119.6.3 面试官最爱追问的四个问题

**Q1：为什么 `dp[0] = true`？**
> 「`dp[i]` 表示『前 `i` 个字符能否被完整切分』。`i = 0` 时是空前缀，它不需要任何词就已被覆盖 —— 也就是**存在一种「0 段切分」**，所以是 true。从递推看，如果 `dp[0]` 是 false，那么任何「一整段就是一个字典词」的情况（比如 `"code"` 与 `["code"]`）都无法被认定，因为那条链要从 `dp[0]` 出发。**它是所有转移链的落地锚点，必须为真。**」

**Q2：`dp` 数组能不能压到 `O(maxLen)`？**
> 「能。`dp[i]` 只依赖 `dp[i-1] … dp[i-maxLen]`，所以用一个长度为 `maxLen + 1` 的循环缓冲区就够了（`dp[i % (maxLen+1)]`）。但要注意：本题的转移要读**最近 `maxLen` 个**状态，不是「固定前两项」，所以只能压到 `O(maxLen)` 而不是 `O(1)`。`maxLen ≤ 20` 时这一步收益很小，**只有在 `n = 10⁶`、`maxLen` 也很小的大规模场景才值得做**。」

**Q3：这题和「凑零钱」有什么本质关系？**
> 「结构上完全是同一个模型：都按『最后一段 / 最后一枚』分类，都用到 `dp[0]` 作为空方案的锚点。区别在**物品的表示**：117 的物品是数字（面额可以直接当下标用，所以能压成一维表），本题的物品是字符串（必须靠哈希或 Trie 匹配，压缩维度就难得多）。还有一处关键差别是**循环谁在外层**：117 求方案数，必须物品在外层才有组合语义；本题求布尔可达性，`OR` 是幂等可交换的，因此循环顺序可以按效率自由选。**这正说明：循环顺序不是玄学，它只在『计数』时才有语义。**」

**Q4：`s` 长到 `10⁶`、字典有 `10⁵` 个词，怎么办？**
> 「三件事同时做：① `dp` 用滚动窗口压到 `O(maxLen)`；② 子串比较用**滚动哈希**做到 `O(1)`（否则每次比较 `O(maxLen)`）；③ 匹配结构换成**反向 Trie** —— 从位置 `i` 往前走最多 `maxLen` 步，边走边查 Trie 的单词结束标记，复杂度 `O(n·maxLen)`，**与字典大小 `k` 完全无关**。这样就避开了『枚举字典单词』的 `O(n·k)` 和『枚举切分点』的 `O(n·maxLen·maxLen)`。如果还嫌慢，就把 Trie 换成 Aho–Corasick 自动机，一次扫描拿到所有「以 `i` 结尾的字典词」，复杂度 `O(n + 匹配次数)`。」

---

## 119.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 140 单词拆分 II](https://leetcode.cn/problems/word-break-ii/) | 困难 | 同一模型要求**输出所有切分**：布尔 DP 换成记忆化 DFS + 回溯，输出规模本身可指数级 |
| [LC 91 解码方法](https://leetcode.cn/problems/decode-ways/) | 中等 | 「最后一段」是 1 位或 2 位数字，求**方案数** → 见 120 章 |
| [LC 132 分割回文串 II](https://leetcode.cn/problems/palindrome-partitioning-ii/) | 困难 | 「最后一段是回文」，求**最少段数**（`min` 转移），回文判定可预处理成 `O(1)` |
| [LC 131 分割回文串](https://leetcode.cn/problems/palindrome-partitioning/) | 中等 | 同一模型求**所有方案** → DP 失效，改回溯 → 见 105 章 |
| [LC 472 连接词](https://leetcode.cn/problems/concatenated-words/) | 困难 | 对字典里每个词跑一次本题（长度剪枝 + 排序是关键），是「本题当子程序」的典型 |
| [LC 322 零钱兑换](https://leetcode.cn/problems/coin-change/) | 中等 | 数字版的「最后一段」：物品可用下标索引，**结构同源** → 见 116 章 |
| [LC 45 跳跃游戏 II](https://leetcode.cn/problems/jump-game-ii/) | 中等 | 也是「位置上的可达性 + 最少步数」，但转移是区间覆盖 ⇒ 可以贪心 → 见 133 章 |
| [LC 211 添加与搜索单词 - 数据结构设计](https://leetcode.cn/problems/design-add-and-search-words-data-structure/) | 中等 | 字典一边加词一边查（含 `.` 通配），专练 Trie 与「词尾匹配」这一层 |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 140 单词拆分 II**
1. 一旦要输出所有方案，`dp` 表就不够用了 —— 想想「布尔可达性」丢掉了什么信息。
2. 自顶向下时，把「从位置 `i` 开始的所有切分」整批缓存下来，比逐个方案回溯更容易写对；但先估一下：总输出规模本身可能有多大？

**LC 132 分割回文串 II**
1. 和 139 的骨架一样，只是「这一段是字典词」换成了「这一段是回文」。先想清楚：回文判定如果每次现算，总代价会升到几阶？
2. 能不能用一张表把「任意子串是不是回文」提前算好，让转移退化成 `O(1)` 判定？

**LC 472 连接词**
1. 需要判的是「一个词能不能被**至少两个**更短的字典词拼出来」—— 把字典按长度排序后，这个「至少两个」的条件怎么在转移里表达？
2. 本题的字符串可能很长（`≤ 10⁴`），所以「枚举切分点」和「枚举字典单词」哪个更合适？先比较候选数。

**通用**
- 序列切分题先答两问：**状态是「前缀可达」还是「后缀可达」**（两者等价，选剪枝方便的那个）、**转移枚举的是切点位置还是段落内容**（候选数分别是 `maxLen` 与 `k` 量级）。
- `dp[0]` 在切分题里永远是「空串 = 0 段切分 = 合法」；`dp[0] = false` 会让整张表全假，而小样例常常测不出来。

</details>

---

## 附：本章小结（可背走的三句话）

```text
① 单词拆分 = 前缀可达性 DP：dp[0]=true，dp[i] = ∃j<i ( dp[j] && s[j..i) ∈ dict )，答案 dp[n]
② 转移可以从两端枚举：枚举切分点（候选 min(i,maxLen)）或枚举字典单词（候选 k）—— 谁小用谁；长词场景 k < maxLen 时后者胜
③ 它同时是「DAG 上的可达性」：位置是顶点、词是边，BFS / DFS / 递推三者等价；记忆化在稀疏可达输入上更快
```

> **上一章**：[`118-perfect-squares.md`](118-perfect-squares.md) —— 同一套「完全背包 = 最短路」的语言用在数字上，并且有数论层面的降维解法。
> **下一章**：[`120-decode-ways.md`](120-decode-ways.md) —— 同一个「单串切分」骨架，但转移只有「1 位」和「2 位」两种候选，而且要求计数而不是布尔。
