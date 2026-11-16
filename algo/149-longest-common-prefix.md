# 149. LC14 最长公共前缀（Longest Common Prefix）

> **状态**：`已完成（初稿）` ｜ **所属**：第 14 部分 · 矩阵与字符串 ｜ **编号**：149 / 150
> **难度**：简单 ｜ **标签**：字符串 · 纵向扫描 · 分治 · 二分 · 前缀性质
> **一句话**：这道题真正考的是「**『公共前缀』这个判定天然带单调性——长度 `k` 是公共的，则任何 `k' < k` 也是公共的**」，因此同一道题能长成三副面孔：纵向扫描、分治合并、二分长度，而三者最坏复杂度都是 `O(S)`，差别只在**常数与提前退出的时机**。
> **前置**：[`020-binary-search.md`](020-binary-search.md)（二分解法的骨架：在一个单调的判定上二分）、[`147-spiral-matrix.md`](147-spiral-matrix.md)（上一章的「边界驱动遍历」，本章换成「列驱动遍历」）

---

## 149.1 题目

**原题链接**：<https://leetcode.cn/problems/longest-common-prefix/>

### 题意重述（把隐含条件补齐）

给你一个字符串数组 `strs`，找出其中所有字符串的**最长公共前缀**。如果不存在公共前缀，返回空字符串 `""`。

五条必须读出来的条件：

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| 求的是**前缀**，不是「公共子串」 | 「最长公共前缀」 | 求子串是另一道题（且难得多）；前缀必须从第 0 个字符开始 |
| 所有字符串**都要参与** | 「所有字符串」 | 只和第一个比是不够的——第一、二个相同不代表第三个相同，仍要逐个校验 |
| 可以**没有**公共前缀 | 「如果不存在公共前缀，返回 `""`」 | `["dog","racecar","car"]` 的答案是 `""`，不能返回 `strs[0]` |
| 字符串**可能为空串** | 「`0 <= strs[i].length <= 200`」 | `["", "b"]` 的答案是 `""`，且**长度 0 天然是下界** |
| 大小写**敏感** | `strs[i]` 由小写英文字母组成（部分版本含大写） | 不能统一转小写再比 |

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| `1 <= strs.length <= 200` | 最多 200 个串 | `O(n · L)` 的逐对比较完全够用；`n` 小说明「常数」比「渐进」更重要 |
| `0 <= strs[i].length <= 200` | 每个串最多 200 字符 | **`S`（所有串长之和）≤ 40 000**，所以 `O(S)` 是这些解法的共同量级 |
| 只有小写字母（部分版本含大写） | 字母表很小 | 哈希/前缀树都能做，但没必要 |
| 返回值是**字符串** | 需要截取子串 | `substring` / 切片都只引用原串（Java 复制、Go 共享底层数组），不改变复杂度 |

> 💡 **量级 → 算法** 在这里给出一个**判据**：
> **「共同」类问题（公共前缀、公共区间、交集）几乎总能写成「逐列」或「分治合并」；若再加一个「单调可二分」的性质，就能长出第三条路。**
> 而三条路的**最坏复杂度往往相同**，选型依据是「实际数据的形态」：串短、差异早出现时，纵向扫描最快。

### 示例

```text
输入：strs = ["flower","flow","flight"]   输出："fl"
输入：strs = ["dog","racecar","car"]      输出：""      ← 第一个字符就不同
输入：strs = ["a"]                        输出："a"     ← 只有一个串，整个串都是前缀
```

### 我自造的六个反例（写完解法必须拿它们打一遍）

```text
① strs = [""]                  → ""          单元素且为空串
② strs = ["", "b"]             → ""          暴露「拿 strs[0] 当基准」但没检查长度 0
③ strs = ["abc", "", "abd"]    → ""          空串在中间，不能被跳过
④ strs = ["abc"]               → "abc"       只有一个串时必须返回整串，别忘了
⑤ strs = ["ab", "abc"]         → "ab"        最短的串本身就是答案（前缀不能超过它）
⑥ strs = ["aaa", "aaa", "aab"] → "aa"        差异出现在最后一个串的最后一个字符
```

第 ⑥ 条是「后半段才出问题」的典型：它专门用来抓「提前 return」的 bug——有些写法在比较完 `strs[0]` 与 `strs[1]` 之后就返回了，完全没看 `strs[2]`。

---

## 149.2 从暴力到最优：第一个念头为什么错

### 第一步：暴力（先写出来，别急着优化）

最直白的做法是「拿第一个串当候选，逐个和后面的串比对并缩短候选」——这叫**横向扫描**：

```java
String prefix = strs[0];
for (int i = 1; i < strs.length; i++) {
    while (!strs[i].startsWith(prefix)) {         // 一直缩到它成为前缀
        prefix = prefix.substring(0, prefix.length() - 1);
        if (prefix.isEmpty()) return "";
    }
}
return prefix;
```

- 时间 **O(S)**（每个字符最多被「缩掉」一次，加上 `startsWith` 的检查开销），空间 **O(1)**（不算返回值的拷贝）。
- **完全正确**，而且很短。它是一个合格的答案。
- 它的三处「不够好」：
  1. `startsWith(prefix)` 在 Java 里要**从头比一遍**，最坏情况下候选很长（比如 200 个字符全相同），每次都比满 200 个字符。虽然均摊下来还是 `O(S)`，但常数比纵向扫描大。
  2. 每缩一次就 `substring` 一次，会不断产生新字符串（Java 里 `substring` 在 7 之后会复制内容），带来 `O(L²)` 的**隐式拷贝**（`L` 是候选长度）。
  3. 它只比较「候选」和「当前串」，**没有利用列的结构**，因此不容易处理「第 k 列是否全部相同」这类变体。

### 第二步：卡点在哪

把问题翻译成一句话：

> **第一个不同出现在第几列？答案就是前 `k` 列。**

于是问题变成一个非常干净的形式：

> **存在一个最大的 `k`，使得所有 `strs[i]` 的前 `k` 个字符都相同（并且长度都 ≥ k）。求这个 `k`。**

这句话里有两个关键信息：

1. **「所有字符串」** —— 每个位置都要全体投票，不是两两比较。
2. **「最大」** —— 有最大值，说明**判定是单调的**：`k` 可行则更小的 `k` 都可行。

### 第三步：三条路都是从这句话长出来的

```
存在最大 k 使「前 k 列全体一致」
     ├── 按列扫（纵向）：让 k 从 0 往上试，一列一列验 → 碰壁就停
     ├── 分治：把「全体」拆成两半，各自求 LCP，再合并 → 利用结合律
     └── 二分：既然 k 的可行性单调，就对 k 二分 → log 因子
```

三条路的最坏复杂度都是 `O(S)`：

- **纵向扫描**：每个字符最多被看一次，一旦某列不一致立刻停 → `O(S)`，而且**自然提前退出**。
- **分治**：把 `n` 个串两两合并，每层合并的字符总量不超过 `S`，层数 `log n`？—— 注意这里容易出现一个错误直觉，见 §149.6.1 的推导：合并的总代价是 `O(S log n)` 还是 `O(S)`，取决于「LCP 长度随合并快速下降」这一实际情况。**严格的最坏界是 `O(S log n)`**，但因为每层合并后答案不会变长，实际运行远好于界。
- **二分**：`O(n · log L)` 次字符比较，每次比较的是前 `mid` 个字符 → 最坏 `O(S log L)`（`L` 为最短串长）。

> **所以「最优」在这题上没有绝对答案**：面试时先说纵向扫描（最直白、常数最小、天然早停），再补一句「分治能利用结合律做并行，二分把检查次数压到 `log` 级但每次检查更贵」，就是完整答案了。

---

## 149.3 原理拆解

### 149.3.1 数据结构与算法选型

| 方案 | 时间 | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- |
| 横向扫描（逐串缩短候选） | O(S) 均摊（+ 隐式子串拷贝） | O(1) | 8 行 | 候选很长时常数大；Java 的 `substring` 反复拷贝 |
| **纵向扫描（按列）** ✅ | **O(S)** 且**天然早停** | **O(1)** | **10 行** | 无；这是本题首选 |
| 分治（两两合并 LCP） | O(S log n) 最坏，实际接近 O(S) | O(log n) 栈 | 20 行 | 串很多而答案很短时多花合并开销；但**可并行** |
| 二分长度 + 检查 | O(S log L) | O(1) | 18 行 | 每次检查都要比 `n` 个串的前缀，常数大；好处是**检查次数只有 log 次** |
| 排序后只比首尾两个 | O(n log n · L) | O(1) 或 O(n) | 6 行 | 排序本身比扫描贵；「首尾最不相同」这个观察很有意思但常数不划算 |
| Trie（前缀树） | O(S) 建树 + O(L) 查询 | O(S) 结点 | 40 行 | 只求一次时纯属浪费；但**多次查询**场景下最优 |

### 149.3.2 为什么这个做法是对的（正确性论证）

**命题 0（前缀性质，三条路共用的地基）**：若长度 `k` 的前缀是所有串的公共前缀，则任意 `k' < k` 也一定是公共前缀。

**证明**：长度 `k'` 的前缀是长度 `k` 的前缀的**前缀**。若 `strs[i]` 的前 `k` 个字符都等于同一个串 `p`，那么它的前 `k'` 个字符都等于 `p[0..k'-1]`，对所有 `i` 成立。∎

> 这条命题有两个用途：① 让二分合法（可行性单调）；② 说明「答案一定是最短串的前缀」——所以任何公共前缀都不比 `min(strs[i].length)` 长。

**命题 1（纵向扫描）**：算法「`k` 从 0 开始，只要第 `k` 列在所有串中都存在且相等就 `k++`」返回的是最长公共前缀的长度。

**证明**：

- **不变量**：进入第 `k` 轮时，前 `k` 列在所有串中一致。
- **初始化**：`k = 0`，空前缀对任何字符串都成立 ✓。
- **终止**：循环在「第 `k` 列有串越界」或「第 `k` 列出现不同字符」时停止，此时长度 `k` 的前缀是公共的（不变量），而长度 `k+1` 的前缀不公共（因为第 `k` 列就不是全体一致，或某个串根本没这么长）。由命题 0 的逆否形式：比 `k+1` 更长的都不可能是公共前缀。因此 `k` 就是最大值 ✓。∎

**命题 2（分治）**：对任意两个字符串 `a`、`b`，记 `LCP(a, b)` 为它们的最长公共前缀，则

```
LCP(strs[0..n-1]) = LCP( LCP(strs[0..m-1]), LCP(strs[m..n-1]) )
```

**证明**：设全体公共前缀为 `P`（最长），左右两半的最长公共前缀为 `P₁`、`P₂`。

- 因为 `P` 同时是两半所有串的公共前缀，所以 `P` 是 `P₁` 的前缀，也是 `P₂` 的前缀，即 `P` 是 `LCP(P₁, P₂)` 的前缀，故 `|P| ≤ |LCP(P₁,P₂)|`。
- 反过来，`LCP(P₁, P₂)` 是 `P₁` 的前缀、又是 `P₂` 的前缀，而 `P₁` 是左半所有串的公共前缀、`P₂` 是右半所有串的公共前缀，因此 `LCP(P₁,P₂)` 是所有串的公共前缀，故 `|LCP(P₁,P₂)| ≤ |P|`。
- 两边长度相等且互为前缀，所以两者相同 ✓。∎

**命题 3（二分）**：定义判定函数 `ok(k) = 「所有串的长度 ≥ k 且前 k 个字符相同」`。由命题 0，`ok` 关于 `k` 是**单调不增**的（真……真、假……假）。因此在 `[0, minLen]` 上二分可以找到最大的 `ok(k)` 为真的 `k`。

**证明**：单调性由命题 0 直接给出；二分在单调布尔数组上必收敛到「最后一个真」，这是二分的基本性质（见 [`020-binary-search.md`](020-binary-search.md) 的「找最后一个满足条件的值」模板）。∎

### 149.3.3 通用解题思路（可迁移的那一层）

**模型：「多路公共性」的三种问法**

```
① 按列问：第 k 列是否全体一致？          → 纵向扫描（最省常数，天然早停）
② 按结合律问：整体 = 左半 ⊕ 右半 吗？    → 分治（可并行、可缓存）
③ 按单调性问：长度 k 可行吗？            → 二分（把「试 k 次」压成「试 log 次」）
```

这三问不是三套知识，而是**同一个问题的三种视角**。能问出后两种的前提是两条性质：**结合律**（分治）与**单调性**（二分）。这两条在绝大多数「公共性」问题上都成立，所以这套拆法可以整体搬走：

| 题 | 公共性问的是什么 | 用哪条路 |
| --- | --- | --- |
| [LC 14 最长公共前缀](https://leetcode.cn/problems/longest-common-prefix/) | 所有串的共同前缀 | 三条路都行（本章） |
| [LC 128 最长连续序列](https://leetcode.cn/problems/longest-consecutive-sequence/) | 值域上的连续段 | 哈希 + 端点延伸（本质是「按值扫」） |
| [LC 4 寻找两个正序数组的中位数](https://leetcode.cn/problems/median-of-two-sorted-arrays/) | 划分点两侧的大小关系 | **二分答案**（单调性） |
| [LC 875 爱吃香蕉的珂珂](https://leetcode.cn/problems/koko-eating-bananas/) | 速度 `k` 是否够用 | **二分答案**（单调性） |
| [LC 5 最长回文子串](https://leetcode.cn/problems/longest-palindromic-substring/) | 中心扩展 / 区间 DP | 与分治同源的「由内向外」 |
| [LC 1392 最长快乐前缀](https://leetcode.cn/problems/longest-happy-prefix/) | 自身的最长「前缀=后缀」 | **KMP 的 next 数组**（见下一章） |

> 最后一行值得停一下：LC 1392 求的是「字符串自身的**最长前缀同时是后缀**」，那是 `next` 数组的定义；而本章求的是「多个字符串的公共前缀」。**两者看起来像，其实是不同的东西**——`150` 章会把这两者区分清楚。

### 149.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| 要**多次查询**（给一批新串，问与集合的 LCP） | 一次建、多次查 | 建 **Trie**：从根往下走，只要结点只有一个孩子就继续，走到分叉/结束为止，`O(L)` 一次查询 |
| 字符串**很长**（`10⁶` 级）且串很多 | `S` 很大 | 纵向扫描仍然 `O(S)` 且**在最坏情况下也更早停**；分治可以并行到多核 |
| 允许**最多 `k` 处不匹配** | 前缀不再严格要求一致 | 变成「模糊前缀」，纵向扫描改成「计数不一致的列数 ≤ k」，仍是 `O(S)` |
| 求最长公共**子串**（不是前缀） | 难度跃升 | 后缀自动机 / 后缀数组 + 二分 + 哈希；或对 `S` 做二分 + 哈希校验 |
| 求**所有**最长的公共前缀（可能多个） | 语义变了 | 公共前缀本质唯一（前缀是线性结构），所以「所有最长公共前缀」只有一个；若问「哪些串共享这个前缀」，输出下标列表即可 |
| 要求**流式**输入（串一个个来） | 不能回头 | 纵向扫描天然流式：维护当前 `prefix`，每来一个新串就 `prefix = LCP(prefix, new)`，随时可输出当前答案 |

---

## 149.4 图解推演

### 149.4.1 纵向扫描：一列一列地看

```
        col 0   col 1   col 2   col 3   col 4   col 5
        ┌───┐   ┌───┐   ┌───┐   ┌───┐   ┌───┐   ┌───┐
strs[0] │ f │   │ l │   │ o │   │ w │   │ e │   │ r │
strs[1] │ f │   │ l │   │ o │   │ w │   │   │   │   │  ← 长度只有 4
strs[2] │ f │   │ l │   │ i │   │ g │   │ h │   │ t │
        └───┘   └───┘   └───┘   └───┘   └───┘   └───┘
          ✓       ✓       ✗
         f==f    l==l   o≠i  ← 第 2 列就分叉了 → 答案 "fl"（长度 2）

扫描顺序（按列，先停先赢）：
   k=0: f f f 一致 → k=1
   k=1: l l l 一致 → k=2
   k=2: o o i 不一致 → 立刻停止，返回前 2 个字符 "fl"
```

**这张图说明了纵向扫描最大的优点：一旦发现分叉就整体停止**，后面的列一个字符都不用看。上面这个例子只比较了 9 次字符，而横向扫描至少要 `startsWith` 三遍。

### 149.4.2 分治的合并树（4 个串）

```
                  LCP(全部)
                ╱          ╲
        LCP(s0, s1)      LCP(s2, s3)
           = "fl"           = "fl"
                ╲          ╱
              LCP("fl","fl") = "fl"      ← 合并
```

```
       s0 = flower ┐
                   ├─ LCP = "flow" ┐
       s1 = flow   ┘                │
                                     ├─ LCP = "flow" → 再与 s3 合并 → "fl"
       s2 = flight ┐                │
                   ├─ LCP = "fl"   ┘
       s3 = fl     ┘

   注意：s0 与 s1 的 LCP 是 "flow"（4 个字符），
         s2 与 s3 的 LCP 是 "fl"（2 个字符），
         合并时只需要比前 2 个字符 —— 因为答案只会越并越短，绝不会变长。
```

**「答案只会越并越短」是分治的关键性质**：合并两个 LCP 时，只要取较短的那个长度作为上界，比较量很快降到最终答案的长度。这就是为什么实际运行远好于 `O(S log n)` 的最坏界。

### 149.4.3 二分的可行区间（`L = 最短串长 = 4`）

```
        k:      0     1     2     3     4
        ok(k):  ✓     ✓     ✗     ✗     ✗
                └─────┴─────┴─────┴─────┘
                  真……真   假……假        ← 单调不增（命题 0）

二分过程（找「最后一个真」）：
  lo=0, hi=4
  mid=2 → ok(2)? 第 2 列 o/o/i 不一致 → 假 → hi = 1
  mid=0 → ok(0)? 空前缀，恒真        → lo = 1
  lo > hi → 答案 = lo-1 = ... 按模板取最后一个真的 k
```

```
   ┌────────────────────────────────────────────────────┐
   │ 二分的三段式回答（面试可以直接说）：                │
   │ ① 判定函数：前 mid 列是否全体一致？ O(n·mid)         │
   │ ② 单调性：mid 可行 ⇒ 更小的都可行（命题 0）          │
   │ ③ 边界：k ∈ [0, minLen]，minLen 是最短串长度         │
   └────────────────────────────────────────────────────┘
```

### 149.4.4 三条路的对比（同一组输入）

| 输入形状 | 纵向扫描的比较次数 | 二分（log 次检查 × n 串） | 谁更快 |
| --- | --- | --- | --- |
| `["flower","flow","flight"]`（第 2 列分叉） | 9（3 列 × 3 串） | 2 次检查 × 3 串 × 约 2 字符 ≈ 12 | 纵向更快 |
| `["aaa…a"(200 个),"aaa…a","aab…"]`（末尾分叉） | 600（200 列 × 3 串） | 8 次检查 × 3 串 × 平均 100 字符 ≈ 2400 | **纵向快一个量级** |
| `[""]`（长度为 0） | 0（立刻返回） | 0 | 持平 |
| 200 个串，答案长度为 1 | 200（1 列 × 200 串） | 8 次检查 × 200 × 1 = 1600 | 纵向快 |

> **结论要敢说**：本题**纵向扫描是工程最优**。二分的价值在于「检查昂贵」的场景（例如每次检查都要访问磁盘或远程服务），此时把检查次数从 `L` 次压到 `log L` 次才是决定性的。

---

## 149.5 双语言实现

### 149.5.1 Java

**纵向扫描（推荐提交版本）**：

```java
class Solution {
    public String longestCommonPrefix(String[] strs) {
        // 空数组没有「公共前缀」可言；题目保证 n ≥ 1，工程代码仍要防
        if (strs == null || strs.length == 0) {
            return "";
        }

        // 以第 0 个串为「列宽参照」：公共前缀不可能比任何一个串长，
        // 所以遍历的列号 j 最多走到 strs[0] 的长度为止。
        for (int j = 0; j < strs[0].length(); j++) {
            char c = strs[0].charAt(j);
            for (int i = 1; i < strs.length; i++) {
                // 两个越界条件合成一个判断：该串长度不够 → 公共前缀到此为止
                if (j >= strs[i].length() || strs[i].charAt(j) != c) {
                    // j 之前（共 j 个字符）全部一致，所以答案就是前 j 个字符
                    return strs[0].substring(0, j);
                }
            }
        }

        // 循环跑完说明 strs[0] 整体是所有人的前缀（例如 ["ab","abc"]）
        return strs[0];
    }
}
```

**二分版（面试要求「再写一种」时用）**：

```java
class Solution {
    public String longestCommonPrefix(String[] strs) {
        if (strs == null || strs.length == 0) {
            return "";
        }
        // 答案长度不超过最短串长度 —— 这是二分的上界
        int minLen = Integer.MAX_VALUE;
        for (String s : strs) {
            minLen = Math.min(minLen, s.length());
        }

        // 在 [0, minLen] 上找「最后一个满足 ok(k) 的 k」
        int lo = 0, hi = minLen;
        while (lo < hi) {
            // 上取整，避免 lo + 1 == hi 时死循环
            int mid = lo + (hi - lo + 1) / 2;
            if (isCommonPrefix(strs, mid)) {
                lo = mid;      // mid 可行 → 试着更长
            } else {
                hi = mid - 1;  // mid 不可行 → 只能更短
            }
        }
        return strs[0].substring(0, lo);
    }

    // 判定：所有串的前 len 个字符是否完全相同（len = 0 时恒真）
    private boolean isCommonPrefix(String[] strs, int len) {
        String base = strs[0].substring(0, len);
        for (int i = 1; i < strs.length; i++) {
            if (!strs[i].startsWith(base)) {
                return false;
            }
        }
        return true;
    }
}
```

### 149.5.2 Go

**纵向扫描**：

```go
package main

func longestCommonPrefix(strs []string) string {
	if len(strs) == 0 {
		return ""
	}

	// 以第 0 个串为列宽参照（Go 里 s[j] 取的是 byte，英文串安全；
	// 若遇到非 ASCII 字符必须先转 []rune，否则会切碎多字节字符）
	for j := 0; j < len(strs[0]); j++ {
		c := strs[0][j]
		for i := 1; i < len(strs); i++ {
			if j >= len(strs[i]) || strs[i][j] != c {
				return strs[0][:j] // 切片共享底层数组，零拷贝
			}
		}
	}

	return strs[0]
}
```

**分治版（体现结合律）**：

```go
package main

// lcpDivide 用分治求最长公共前缀，与纵向扫描的机制不同：它靠「合并」而不是「按列」。
func lcpDivide(strs []string) string {
	if len(strs) == 0 {
		return ""
	}
	var solve func(lo, hi int) string
	solve = func(lo, hi int) string {
		if lo == hi {
			return strs[lo] // 单个串的公共前缀就是它自己
		}
		mid := lo + (hi-lo)/2
		return lcpTwo(solve(lo, mid), solve(mid+1, hi))
	}
	return solve(0, len(strs)-1)
}

// lcpTwo 求两个串的最长公共前缀：逐字符比，遇到不同或越界就停
func lcpTwo(a, b string) string {
	n := len(a)
	if len(b) < n {
		n = len(b)
	}
	i := 0
	for i < n && a[i] == b[i] {
		i++
	}
	return a[:i]
}
```

**二分版**：

```go
package main

func lcpBinary(strs []string) string {
	if len(strs) == 0 {
		return ""
	}
	minLen := len(strs[0])
	for _, s := range strs {
		if len(s) < minLen {
			minLen = len(s)
		}
	}

	lo, hi := 0, minLen
	for lo < hi {
		mid := lo + (hi-lo+1)/2 // 上取整，防止死循环
		if isCommonPrefix(strs, mid) {
			lo = mid
		} else {
			hi = mid - 1
		}
	}
	return strs[0][:lo]
}

func isCommonPrefix(strs []string, length int) bool {
	base := strs[0][:length]
	for i := 1; i < len(strs); i++ {
		if len(strs[i]) < length || strs[i][:length] != base {
			return false
		}
	}
	return true
}
```

### 149.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 字符串索引 | `charAt(j)` 返回 `char`（UTF-16 码元） | `s[j]` 返回 **`byte`** | 英文串两者等价；含中文时必须用 `[]rune` 或 `[]rune(s)`，否则 `s[j]` 会切碎多字节字符 |
| 截取前缀 | `substring(0, j)` **复制**一份新串 | `s[:j]` **共享底层数组**，零拷贝 | Go 快很多；但要注意共享会导致原串无法被 GC（大串切小片段时用 `strings.Clone`） |
| 判前缀 | `strs[i].startsWith(base)` | `strings.HasPrefix(strs[i], base)` 或切片比较 | Go 的 `strs[i][:len] != base` 需先判长度，否则越界 panic |
| 空数值 | `Integer.MAX_VALUE` 当 `minLen` 初值 | `math.MaxInt` 或直接取 `len(strs[0])` | 直接取首个元素更简洁，也避免 `int` 宽度差异 |
| 字符串不可变 | `String` 不可变 | `string` 不可变 | 两者都不可变，所以「缩短候选」一定会产生新串（横向扫描的隐患） |

### 149.5.4 对拍测试（写完必须跑）

```go
package main

import (
	"math/rand"
	"strings"
	"testing"
)

// 参考实现：最朴素的「和每个串逐字符比」，与三种解法的机制都不同
func bruteLCP(strs []string) string {
	if len(strs) == 0 {
		return ""
	}
	base := strs[0]
	for k := len(base); k >= 0; k-- { // 从长到短试，第一个可行就是答案
		cand := base[:k]
		all := true
		for _, s := range strs {
			if !strings.HasPrefix(s, cand) {
				all = false
				break
			}
		}
		if all {
			return cand
		}
	}
	return ""
}

// 随机造串：让所有串共享一个长度 0..3 的随机前缀，
// 这样「有公共前缀 / 没有公共前缀」两种形态都会被高频造出。
func genStrs(r *rand.Rand, n int) []string {
	shared := ""
	for k := 0; k < r.Intn(4); k++ {
		shared += string(rune('a' + r.Intn(2))) // 共享前缀只用 a/b，保证真共享
	}
	strs := make([]string, n)
	for i := range strs {
		s := shared
		L := r.Intn(6) // 附加随机后缀
		for k := 0; k < L; k++ {
			s += string(rune('a' + r.Intn(26)))
		}
		// 偶尔故意截短，让某些串比共享前缀还短 → 造出「最短串就是答案」的形态
		if r.Intn(5) == 0 && len(s) > 0 {
			s = s[:r.Intn(len(s)+1)]
		}
		strs[i] = s
	}
	return strs
}

func TestLongestCommonPrefixRandom(t *testing.T) {
	r := rand.New(rand.NewSource(20260222))
	for iter := 0; iter < 20000; iter++ {
		n := r.Intn(6) + 1
		strs := genStrs(r, n)
		want := bruteLCP(strs)

		if got := longestCommonPrefix(strs); got != want {
			t.Fatalf("iter=%d strs=%q 纵向 got=%q want=%q", iter, strs, got, want)
		}
		if got := lcpDivide(strs); got != want {
			t.Fatalf("iter=%d strs=%q 分治 got=%q want=%q", iter, strs, got, want)
		}
		if got := lcpBinary(strs); got != want {
			t.Fatalf("iter=%d strs=%q 二分 got=%q want=%q", iter, strs, got, want)
		}
	}
}

func TestLongestCommonPrefixEdge(t *testing.T) {
	cases := []struct {
		strs []string
		want string
	}{
		{[]string{""}, ""},                             // 单元素空串
		{[]string{"", "b"}, ""},                        // 空串在开头
		{[]string{"abc", "", "abd"}, ""},               // 空串在中间
		{[]string{"abc"}, "abc"},                       // 只有一个串
		{[]string{"ab", "abc"}, "ab"},                  // 最短串就是答案
		{[]string{"aaa", "aaa", "aab"}, "aa"},          // 差异在最后的串
		{[]string{"flower", "flow", "flight"}, "fl"},   // 题面示例
		{[]string{"dog", "racecar", "car"}, ""},        // 首字符就不同
		{[]string{"a"}, "a"},
		{[]string{"same", "same", "same"}, "same"},     // 全部相同
		{[]string{"abab", "aba", "ab"}, "ab"},
		{[]string{}, ""},                               // 空数组（题目保证 n ≥ 1）
	}
	for _, c := range cases {
		if got := longestCommonPrefix(c.strs); got != c.want {
			t.Fatalf("strs=%q 纵向 got=%q want=%q", c.strs, got, c.want)
		}
		if got := lcpDivide(c.strs); got != c.want {
			t.Fatalf("strs=%q 分治 got=%q want=%q", c.strs, got, c.want)
		}
		if got := lcpBinary(c.strs); got != c.want {
			t.Fatalf("strs=%q 二分 got=%q want=%q", c.strs, got, c.want)
		}
	}
}
```

> **三种解法一起对拍**是这段测试的设计重点：它们互相独立（按列 / 合并 / 二分），任何一条路的实现 bug 都会被另外两条揪出来。尤其 `lcpBinary` 的「上取整 `mid`」写法，一旦写成下取整就会死循环——**这类「会卡住」的 bug 只能靠真跑才发现**。

---

## 149.6 复杂度与常见坑

### 149.6.1 复杂度推导（写结论不算本事，要能推）

- **纵向扫描 O(S)**：设 `S = Σ len(strs[i])`。外层按列推进，内层按串推进，**每个 `(i, k)` 组合最多被检查一次**；一旦第 `k` 列出问题就整体退出（**早停**）。因此比较次数 ≤ `n · |答案|`，而 `n · |答案| ≤ S`。空间 `O(1)`（不算返回值的拷贝）。
- **二分 O(S · log L)？**：每次检查 `ok(mid)` 要花 `O(n · mid)`，最多 `log(minLen)` 次检查，所以上界是 `O(n · minLen · log minLen)`。**注意这不是 `O(S log L)` 的精确说法**——只有当所有串长度接近 `minLen` 时才近似相等；严格的界写成 `O(n · minLen · log minLen)` 更稳。
- **分治的最坏界是 O(S log n)**：把「每层合并的总代价 ≤ S」乘以层数 `log n`。但**答案只会越并越短**：第二层之后每次合并只需比较到当前答案长度，而答案长度 ≤ 最短串长，实际代价往往接近 `O(S)`。面试答「最坏 `O(S log n)`，实际接近 `O(S)`，且可并行」最准确。
- **横向扫描的隐式代价**：`substring` / 拼接每次都会复制 `O(L)` 字符，缩 `L` 次就是 `O(L²)` 的拷贝量。`L = 200` 时是 4 万次字符拷贝，虽然仍可接受，但这是「算法复杂度看起来一样、实际慢一截」的典型例子。

### 149.6.2 七个真实踩过的坑

1. **空数组直接取 `strs[0]`** —— 抛异常。`strs.length == 0` 时要返回 `""`（虽然题目保证 `n ≥ 1`，工程代码必须防）。
2. **忘了判「某个串长度不够」** —— `strs[i].charAt(j)` 在 `j >= len(strs[i])` 时抛 `StringIndexOutOfBoundsException`。必须把 `j >= strs[i].length()` 和「字符不等」写在同一个 `if` 里。
3. **拿 `strs[0]` 当基准且它是最长的串** —— 不影响正确性（内层会挡住越界），但会让「列循环」多跑一些无用的列；以最短串为界更省。
4. **提前 `return` 只比较了前两个串** —— `["aaa","aaa","aab"]` 会返回 `"aaa"`（错，应为 `"aa"`）。所有串都必须参与。
5. **二分的 `mid` 取下整导致死循环** —— 「找最后一个真」的模板必须用 `mid = lo + (hi - lo + 1) / 2`（上取整），否则 `lo == hi - 1` 时 `mid == lo`，`lo` 永不前进。
6. **Go 里用 `s[j]` 处理非 ASCII 串** —— `s[j]` 是字节，中文一个字符占 3 字节，按字节比较会把字符切碎，得到非法 UTF-8 片段。多语言场景必须 `[]rune`。
7. **把「最长公共前缀」和「最长公共子串」混淆** —— 前者是「从第 0 个字符起」，后者中间可以任意起，难度完全不同（需要后缀结构）。面试里说错这一句会显得没读懂题。

### 149.6.3 面试官最爱追问的三个问题

**Q1：三种解法你选哪个？为什么？**

> 「工程上选纵向扫描：`O(S)` 时间、`O(1)` 空间，而且**一旦某一列分叉就整体停止**，实际比较次数常常远小于 `S`。分治的好处是能利用结合律做并行，或者把结果缓存起来给多次查询用；二分的好处是把『检查次数』从 `L` 压到 `log L`，适合每次检查都很贵的场景（远程调用、磁盘读取），但它每次检查都要比 `n` 个串，本地场景反而更慢。三者的最坏界分别是 `O(S)`、`O(S log n)`、`O(n·log L·minLen)`。」

**Q2：为什么二分在这题是合法的？**

> 「因为『长度 `k` 的前缀是公共的』这个判定关于 `k` 单调：如果长度 `k` 都能公共，那么更短的 `k'` 的前缀是它的前缀，必然也公共。于是布尔序列是『真…真假…假』，可以二分找最后一个真。上界是 `min(len(strs[i]))`，因为公共前缀不可能比最短的串更长。」

**Q3：如果要支持「多次查询」，你会怎么改？**

> 「建 Trie。把所有串插进 Trie 后，最长公共前缀就是『从根出发，沿着唯一孩子的路径一直走，直到遇到分叉或某个串结束的位置』。建树 `O(S)`，每次查询 `O(L)`，而且增删字符串都只影响一条路径。如果查询是『求某个新串与原集合的 LCP』，同样从根走，走到失配为止，`O(L)`。」

---

## 149.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 28 找出字符串中第一个匹配项的下标](https://leetcode.cn/problems/find-the-index-of-the-first-occurrence-in-a-string/) | 简单 | 本章是「多串的公共前缀」，那是「单模式串的完整匹配」→ 见 [`150-find-the-index-of-the-first-occurrence-in-a-string.md`](150-find-the-index-of-the-first-occurrence-in-a-string.md) |
| [LC 1392 最长快乐前缀](https://leetcode.cn/problems/longest-happy-prefix/) | 困难 | 求「自身的最长前缀=后缀」→ 正是 KMP 的 `next` 数组定义 |
| [LC 459 重复的子字符串](https://leetcode.cn/problems/repeated-substring-pattern/) | 简单 | 用 `next` 数组判断「整串是否由某个子串重复构成」 |
| [LC 208 实现 Trie](https://leetcode.cn/problems/implement-trie-prefix-tree/) | 中等 | 「多次查询公共前缀」的正确数据结构 |
| [LC 648 单词替换](https://leetcode.cn/problems/replace-words/) | 中等 | 用 Trie 找每个单词的「最短公共前缀词根」 |
| [LC 4 寻找两个正序数组的中位数](https://leetcode.cn/problems/median-of-two-sorted-arrays/) | 困难 | 同样靠「单调性 + 二分」把线性压到 `log` |
| [LC 875 爱吃香蕉的珂珂](https://leetcode.cn/problems/koko-eating-bananas/) | 中等 | 二分答案的母题：判定函数 + 单调性 + 上下界 |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 1392（最长快乐前缀）**
1. 「前缀同时等于后缀」这个条件，和「把模式串和文本串拼起来求某种失配函数」是不是同一件事？
2. 如果不想写 KMP，能不能用「字符串哈希 + 二分长度」达到同样复杂度？代价是什么？

**LC 459（重复的子字符串）**
1. 如果一个串由长度为 `p` 的子串重复构成，那么 `p` 与「最长前缀=后缀长度」之间有什么算术关系？
2. 试几个例子（`"abab"`、`"abcabcabc"`、`"aba"`）验证你想的那个关系。

**LC 208（Trie）**
1. 求多个串的最长公共前缀时，Trie 上「什么样的结点是分叉点」？
2. 为什么走到「某个串结束标记」也必须停下来？

**通用**
- 「公共性」问题先问三句：能不能**按列**看？有没有**结合律**？判定有没有**单调性**？
- 三条路的最坏复杂度常常相同，真正决定快慢的是**常数与提前退出的时机**。

</details>

---

## 附：本章小结（可背走的三句话）

```
① 最长公共前缀 = 「最大的 k，使前 k 列在所有串中一致」→ 这句话能长出三种解法
② 纵向扫描 O(S) 且天然早停（工程最优）；分治靠结合律可并行；二分靠单调性把检查次数压到 log
③ 前缀性质（k 可行 ⇒ 更小的都可行）是二分的合法性来源，也解释了答案不超过最短串长
```

> **下一章**：[`150-find-the-index-of-the-first-occurrence-in-a-string.md`](150-find-the-index-of-the-first-occurrence-in-a-string.md) —— 全书收官：从「多串的公共前缀」走到「单模式串的精确匹配」，主角是 KMP 的 `next` 数组（失败函数）。
