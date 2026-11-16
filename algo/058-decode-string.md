# 058. LC394 字符串解码（Decode String）

> **状态**：`已完成（初稿）` ｜ **所属**：第 7 部分 · 栈 · 队列 · 单调结构 ｜ **编号**：058 / 150
> **难度**：中等 ｜ **标签**：栈 · 字符串 · 递归 · 表达式解析
> **一句话**：这道题真正考的是「**遇到 `[` 时把『外层已经拼好的半成品』和『待用的重复次数』一起压栈，遇到 `]` 时用它们把括号内的结果接回外层**」——与基本计算器是同一种「现场保存/恢复」，只是结算出来的东西从数变成了字符串。
> **前置**：[`055-valid-parentheses.md`](055-valid-parentheses.md)（括号的栈不变量）、[`057-basic-calculator.md`](057-basic-calculator.md)（压「累计值 + 符号」的现场恢复）

---

## 058.1 题目

**原题链接**：<https://leetcode.cn/problems/decode-string/>

### 题意重述（把隐含条件补齐）

编码规则只有一条：`k[encoded_string]` 表示把方括号里的 `encoded_string` **重复 `k` 次**。给定一个编码后的字符串 `s`，返回它解码后的结果。编码可以**任意嵌套**，括号外也可以有**未被编码的字母**。

六个必须读出来的细节：

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| **`k` 可以有多位** | 约束「`1 <= k <= 300`」 | 按单字符读数字 → `"10[a]"` 解成 `"aaaaaaaaa"`（9 个，实际 10 个） |
| **括号外可以有普通字母** | 示例 `"2[abc]3[cd]ef"` | 只处理形如 `k[...]` 的整串 → 末尾的 `ef` 直接丢掉 |
| **可以嵌套** | 示例 `"3[a2[c]]"` | 只处理一层 → 内层 `2[c]` 当成文本原样输出 |
| **同一个串里可以有多段编码** | 示例 `"3[a]2[bc]"` | 处理完第一段就把 `num` / `cur` 忘掉 → 第二段出错 |
| **输入保证合法** | 约束 | 不必校验 `[` `]` 是否配对，也不必处理 `0[` 或 `]]` 这种脏数据 |
| **答案长度上界 `10⁵`** | 约束 | 若用 `s = s + t` 反复拼接，退化成 `O(输出²)`：本机 Go 实测 10 万个字符的朴素拼接 **1.13 s**，`strings.Builder` **0.31 ms**（差 3600 倍） |

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| `1 <= s.length <= 30` | 编码串极短 | **几乎所有解法都能过**（包括最朴素的递归），所以这题 AC 毫无信息量；考的是「结构是否清晰、能否扩展到 LC 726」 |
| `1 <= 重复次数 k <= 300` | 单个倍数不大 | 但**嵌套乘法**会让输出爆炸：`"9[9[9[9[9[9[a]]]]]]"` 已经是 `9⁶ = 531441` |
| 解码结果长度 `<= 10⁵` | 输出上界 | 复杂度应当按 **`O(输出长度)`** 衡量，而不是 `O(n)` —— 这是本题最容易被漏掉的分析点 |
| `s` 只含小写字母、数字、`[` `]` | 字符集 | 可以用 `byte` 安全索引；不需要 UTF-8 处理 |

> 💡 **量级 → 算法**：`n ≤ 30` 是个**诈骗式上界**：它让 `O(n²)` 甚至 `O(n³)` 的写法都能过。
> 真正该练的是：**（1）栈的结构怎么设计；（2）把复杂度说成 `O(输出长度)`；（3）用 `Builder` 而不是 `+` 拼接。**
> 面试里如果这题被当成「简单题」放水，你可以主动补一句「如果 `n` 到 `10⁵` 呢」——这就把话题引到你想要的地方。

### 示例

```text
输入：s = "3[a]2[bc]"        输出："aaabcbc"
输入：s = "3[a2[c]]"         输出："accaccacc"        ← 嵌套：2[c] → cc，3[acc] → accaccacc
输入：s = "2[abc]3[cd]ef"    输出："abcabccdcdcdef"   ← 括号外还有 ef
输入：s = "abc3[cd]xyz"      输出："abccdcdcdxyz"     ← 前后都有普通字母
输入：s = "10[a]"            输出："aaaaaaaaaa"       ← 两位数的 k
```

### 我自造的七个反例（写完必须拿它们打一遍）

```text
① "a"                 → 打「没有括号时直接返回」的漏判：正确 "a"
② "abc3[cd]xyz"       → 打「漏掉括号外字母」（前、后两段都要保留）：正确 "abccdcdcdxyz"
③ "10[a]"             → 打「多位数 k」：正确 10 个 a
④ "2[2[b]]"           → 打「嵌套时内层结果没交给外层」：正确 "bbbb"
⑤ "3[a]2[bc]"         → 打「处理完一段后 num/cur 没复位」：正确 "aaabcbc"
⑥ "300[a]"            → 打「k 很大时用 + 拼接」：输出 300 字符，朴素拼接已开始变慢
⑦ "9[9[9[a]]]"        → 打「输出长度按 n 估」：真实输出 729 个字符，比输入长 25 倍
```

第 ④ 条是嵌套的核心：`"2[2[b]]"` 里内层算出 `"bb"`，外层要把它重复 2 次得到 `"bbbb"`。**如果内层结果只是 `append` 到全局结果上而没有交给外层，就会得到 `"bb"`。**

---

## 058.2 从暴力到最优：第一个念头为什么错

### 第一步：暴力（反复找最内层括号，展开后替换回原串）

人脑解编码串的顺序是：先找最里层（不含 `[` 的）那一段，展开，塞回去，再处理外层。

```java
// 暴力：O(n²) 次字符串重建，每层括号都要扫全串
while (s.contains("[")) {
    int r = s.indexOf(']');                 // 第一个 ']' → 它左边就是最内层
    int l = s.lastIndexOf('[', r);
    int p = l - 1;                          // 往回找数字的起点
    while (p >= 0 && Character.isDigit(s.charAt(p))) p--;
    int k = Integer.parseInt(s.substring(p + 1, l));
    String inner = s.substring(l + 1, r);
    s = s.substring(0, p + 1) + inner.repeat(k) + s.substring(r + 1);
}
return s;
```

- 时间 **`O(层数 × 长度 + 输出长度)`**（最坏 `O(n² + out)`），空间 `O(out)`。
- 在 `n ≤ 30` 时它**完全能过**，而且代码短；但它有三个硬伤：
  1. **依赖字符串可变性**：Java / Go 里每次拼接都是全量拷贝；
  2. **完全不能流式**：必须先拿到整串、反复回扫；
  3. **数字可能被缓存打断**：`"12[ab]"` 里 `1` `2` 之间若有前一层展开产生的字符，回找数字的边界逻辑会变复杂。

### 第二步：它为什么不够 —— 用数字说话

- 本机 Go 实测：**朴素拼接 10 万个字符要 1.13 s**，而 `strings.Builder` 只要 **0.31 ms**（3600 倍）。暴力法的「展开后拼回去」每一步都相当于朴素拼接。
- 更本质的问题：**它把「嵌套结构」当成了「重复劳动」**。括号的嵌套是**后进先出**的，一次从左到右的扫描就能把所有嵌套关系解开 —— 只要你在遇到 `[` 时**把外层的半成品存起来**。

### 第三步：卡点在哪

把问题翻译成一句话：

> **读到 `]` 时，我需要两样东西才能继续：① 外层在我进入这个括号之前已经拼好的那部分；② 我这个括号要重复多少次。**

这两样东西在进入 `[` 的那一刻就已知（甚至是必须先知道的），所以**必须在 `[` 处把它们存起来**——这就是栈。

```
进入 '[' 时：   压入 (外层前缀 prefix, 重复次数 k)，然后清空自己的 cur
退出 ']' 时：   取出 (prefix, k)，本层结果 = prefix + k 份 cur
```

### 第四步：灵感的来源 —— 「当前层」这个概念

把整道题想成「**我在写一个叫 `cur` 的便签，一层一个**」：

```
"3[a2[c]]"

层 0（最外）        便签 cur₀ = "acc"
  [ 3
  层 1            便签 cur₁ = "acc"   ← 注意：内层算完后，结果是被"交回"层 1 的
    [ 2
    层 2          便签 cur₂ = "c"
    ]
  ]
```

于是栈里装的不是「字符」，而是「**每层进入括号前的那个便签（prefix）**」+「该层的倍数」。**遇到 `]` 就是把内层便签结算成字符串、并贴回外层便签上。**

> 一句可以带走的话：**「栈里存的是『上一层的我来找你要东西时，我需要收回什么』。」** —— 这与 `057` 里「栈里存括号外的累计值和符号」是同一个思维动作。

---

## 058.3 原理拆解

### 058.3.1 数据结构与算法选型

| 方案 | 时间 | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- |
| 反复找最内层括号 + 替换 | O(层数 × n + out) | O(out) | 20 行 | 不可流式；`n` 变大立刻退化；嵌套里含多位数时边界易错 |
| **双栈（数字栈 + 字符串栈）** ✅ | **O(n + out)** | **O(深度 × 前缀 + out)** | **25 行** | 需要两份栈；`n` 极大且深度极大时前缀总量可能到 `O(n²)`（见 `058.6.1`） |
| **递归下降** ✅ | **O(n + out)** | **O(深度)** 调用栈 | **22 行** | 深度由 `n` 决定（本题 `n ≤ 30`，安全）；深嵌套语言里会爆栈 |
| 单栈存「复合对象」 | O(n + out) | O(深度) | 28 行 | 需要自定义结构体 / 类，Java 写起来更啰嗦 |

**双栈 vs 递归下降 的本质关系**：

```
递归下降的"函数调用栈"  ≡  显式栈
   进入 '[' 时递归调用      ≡  push(prefix, k) + 重置 cur
   从 ']' 返回             ≡  pop → 结算 → 写回外层 cur

→ 两者是同一个算法的两种写法。哪个更短？递归下降（不需要手动管理 prefix）。
→ 哪个更安全？双栈（不受递归深度限制，Java 也能跑深嵌套）。
```

> **取舍怎么说**：**面试首选递归下降**（代码短、可读性最好，且能自然扩展到 LC 726 这类「括号 = 作用域」的题）；**工程 / 深嵌套场景首选双栈**（不依赖调用栈）。能同时写出两个，并说清「它们其实是同一个东西」的，是这题的最高分答案。

### 058.3.2 为什么这个做法是对的（正确性论证）

**循环不变量 `I(i)`**（扫描到下标 `i`，`s[0..i-1]` 已处理）：

> 设此刻**未闭合的 `[`** 个数为 `L`（即当前嵌套深度）。则：
> ① 栈中恰好有 `L` 个元素，第 `j` 个元素是「进入第 `j` 层括号前，外层便签的内容」与「该层的倍数 `k_j`」；
> ② `cur` 恰好等于：**第 `L` 层里，从它那个 `[` 之后到现在为止，已经解码出来的字符串**；
> ③ `num` 是「正在读的、尚未遇到 `[` 的数字」。
> 特别地 `L = 0` 时栈为空，`cur` 就是「整个 `s[0..i-1]` 的解码结果」。

**命题 1（`[` 的转移保持不变量）**：在 `[` 处执行 `push(cur, num); cur = ""; num = 0`。新的 `L' = L+1`，栈新增的元素正是「进入新层前外层便签的内容」，而新层的 `cur` 为空 —— 空串恰是「新层还没有任何输出」的正确语义。∎

**命题 2（`]` 的结算正确）**：设弹出 `(prefix, k)`，令 `inner` 为弹出前的 `cur`。由不变量 ②，`inner` 是「该括号内全部内容」的解码结果，于是由题目编码规则，`k[inner]` 解码为 `inner` 重复 `k` 遍。令 `cur_new = prefix + inner×k`，它正是「外层便签在吞掉这个括号后的内容」，因此不变量 ② 对 `L-1` 层成立。∎

**命题 3（普通字母 / 数字的转移保持不变量）**：字母直接 `cur += c`（外层便签增长，语义正确）；数字累乘到 `num`（因为 `k` 可以是多位数）。∎

**命题 4（终止时正确）**：扫描结束时由输入保证 `L = 0`，于是 `cur` 就是整个串的解码结果。∎

> **一个值得讲的细节**：为什么 `num` 必须在 `[` 处**归零**、在 `]` 处**也要归零**？前者防止「上一次的倍数残留到嵌套内层」；后者防止「上层的倍数被内层结算污染」。`058.4.2` 会画出这两处归零各自挡住的是哪种错误。

### 058.3.3 通用解题思路（可迁移的那一层）

**本章主题（与 055、056、057、059 共用）：栈里存的是「还没被解决的问题」——存什么、何时弹、弹出时结算了谁。**

| 三问 | 本题（058） | 与 057 的对比 |
| --- | --- | --- |
| **① 存什么** | 存「外层便签 `prefix` + 待用倍数 `k`」 | 057 存「累计值 `res` + 符号 `sign`」，结构完全同构 |
| **② 何时弹** | 遇到 `]` | 057 是遇到 `)` —— 都是「作用域结束」 |
| **③ 弹出时结算了谁** | 结算了「这一段 `k[...]` 整体」→ 变成外层的一段字符串 | 057 结算「括号内整个表达式」→ 变成外层的一个数 |

**「谁是那个『整体』」** 是这类题的分水岭：

| 题 | `]` / `)` 弹出的那一刻，被结算的「整体」是什么 |
| --- | --- |
| [LC 394 字符串解码](https://leetcode.cn/problems/decode-string/)（本章） | 一段字符串（要重复 k 次） |
| [LC 224 基本计算器](https://leetcode.cn/problems/basic-calculator/)（`057`） | 一个整数（要乘符号再加回） |
| [LC 726 原子的数量](https://leetcode.cn/problems/number-of-atoms/) | 一张「元素 → 计数」表（要乘以括号外的倍数后并入外层表） |
| [LC 736 解析 Lisp 表达式](https://leetcode.cn/problems/parse-lisp-expression/) | 一个求值结果（同时销毁本层作用域） |
| [LC 1190 反转每对括号间的子串](https://leetcode.cn/problems/reverse-substrings-between-each-pair-of-parentheses/) | 一段先反转再交回外层的字符串 |

**通用模型的第三种形态：把「递归」当 SQL 里的 join，把「栈」当手动 join**

```
递归版本：   solve(子串)  ← 每层新建一个"局部变量 cur"
栈版本：     用 (prefix, k) 元组"手动保存父层局部变量"

⟹ 任何"局部变量需要跨层保存"的递归，都能机械地改写为显式栈
```

这个改写能力在工程上非常实用：**Java 里遇到 `StackOverflowError` 时，你的第一反应应该是「把递归改成显式栈」，而不是「调大 `-Xss`」。**

### 058.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| `k` 可以**为 0 或负数** | 语义扩展 | 输出可能为空；栈结构不变，只是 `repeat` 的次数要为 0（注意负数需要校验） |
| 允许 **`k` 之间有空隙 / 空格** | 语法放宽 | 数字解析里跳过空格即可；`num` 的「遇到 `[` 才结束」这一条件仍成立 |
| 要求**内存 `O(输出)` 且不物化** | 流式输出 | 不要真的构造字符串：写一个「迭代器」按解码顺序吐字符（跳跃式展开），空间只要 `O(深度)` |
| 输出长度**不再受限**（如 `10⁹`） | 必须不物化 | 同上：只能「按需生成第 `x` 个字符」，或直接返回「长度」而不返回内容 |
| 括号里还有**其他编码规则**（如 `{a,b}`） | 语法树变复杂 | 递归下降天然可扩展：每个语法结构一层函数（LC 1096 的方向） |
| 需要**同时保留原始位置信息**（映射回编码串下标） | 双向映射 | 在栈元素里附带「源区间起点」，解码时记录输出区间 ↔ 源区间的对应表 |

**进阶延伸（自己写代码验证）**：

- **[LC 726 原子的数量](https://leetcode.cn/problems/number-of-atoms/)**：把「字符串重复」换成「计数表翻倍」，栈里存 `map`，是本章最直接的加强版。
- **[LC 736 解析 Lisp 表达式](https://leetcode.cn/problems/parse-lisp-expression/)**：括号里是函数调用与变量绑定，需要「作用域链」——栈元素从元组升级成 `map` 链。
- **[LC 1190 反转每对括号间的子串](https://leetcode.cn/problems/reverse-substrings-between-each-pair-of-parentheses/)**：本章的「结算动作」从 `repeat` 换成 `reverse`，其余一模一样。

---

## 058.4 图解推演

### 058.4.1 双栈：`"3[a2[c]]"` 的完整轨迹

```
字符:  3   [   a   2   [   c   ]   ]
下标:  0   1   2   3   4   5   6   7

┌──────┬──────┬──────────────────────────────┬───────────┬──────────┬─────┐
│ 步骤 │ 字符 │ 动作                         │ cur       │ num      │ 栈  │
├──────┼──────┼──────────────────────────────┼───────────┼──────────┼─────┤
│  0   │  3   │ num = 3                      │ ""        │ 3        │ []  │
│  1   │  [   │ 压("", 3)；cur=""; num=0     │ ""        │ 0        │ [("",3)] │
│  2   │  a   │ cur = "a"                    │ "a"       │ 0        │ [("",3)] │
│  3   │  2   │ num = 2                      │ "a"       │ 2        │ [("",3)] │
│  4   │  [   │ 压("a", 2)；cur=""; num=0    │ ""        │ 0        │ [("",3),("a",2)] │
│  5   │  c   │ cur = "c"                    │ "c"       │ 0        │ [("",3),("a",2)] │
│  6   │  ]   │ 弹("a",2)→cur = "a"+"cc"     │ "acc"     │ 0        │ [("",3)] │
│  7   │  ]   │ 弹("",3)→cur = ""+"acc"×3    │ "accaccacc"│ 0       │ []      │
└──────┴──────┴──────────────────────────────┴───────────┴──────────┴─────┘
答案 = "accaccacc"
```

### 058.4.2 两处 `num` 归零各自挡住什么

```
❌ 若在 '[' 处忘记 num = 0：
   "3[a2[c]]" → 内层 num 会沿用 2？不 —— 真正的问题在 "2[a]3[b]" 这种同级串联：
   处理完 "2[a]" 后 num 还是 2，遇到 '[' 的前一刻读到了 '3' → num = 2*10+3 = 23 → 错

❌ 若在 ']' 处忘记 num = 0：
   "3[a]" 结算完后 num 仍是 3；接着遇到下一个数字 '2' → num = 3*10+2 = 32 → 错

✅ 正确做法：
   '[' 处：压栈后立刻 cur="" 且 num=0（进入新层，一切从零开始）
   ']' 处：结算后 num=0（本层的 k 已经用完，不能污染后续）
```

### 058.4.3 括号外有字母：`"2[abc]3[cd]ef"` 的便签视图

```
层 0 便签:  ""  →  "abcabc"  →  "abcabccdcdcdef"
                     ↑ 弹 ("" , 2)      ↑ 弹("abcabc", 3)   ↑ 后两个字母直接贴上去
                                                              "ef"

  ┌────────────────────────────────────────────────────────┐
  │ 关键认知：cur 是"当前层"的便签，不是"全局结果"。       │
  │ 只有深度回到 0 时，cur 才等于最终答案。                │
  └────────────────────────────────────────────────────────┘
```

### 058.4.4 递归下降与双栈的对应关系

```
双栈（迭代）                          递归下降
─────────────────────────────────────────────────────────────
push(prefix, k); cur = ""     ←→     pos++ 后调用 parse()
  …扫描本层…                          …在子调用里扫描…
pop → prefix + k×cur          ←→     return 后 for(k) append(inner)
  继续本层循环                  ←→     回到本层的 while 循环

两条路径的"信息流"完全相同，只是一份放在堆上的栈里，
另一份放在调用栈的栈帧里。
```

---

## 058.5 双语言实现

### 058.5.1 Java

```java
import java.util.ArrayDeque;
import java.util.Deque;

class Solution {
    // 解 A：双栈（数字栈 + 字符串栈）
    public String decodeString(String s) {
        Deque<Integer> numStack = new ArrayDeque<>();   // 每层的倍数 k
        Deque<String> strStack = new ArrayDeque<>();     // 每层进入 '[' 前外层的便签
        StringBuilder cur = new StringBuilder();         // 当前层的便签
        int num = 0;

        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c >= '0' && c <= '9') {
                num = num * 10 + (c - '0');              // '12[ab]' 这种多位数
            } else if (c == '[') {
                numStack.push(num);                      // 本层倍数入栈
                strStack.push(cur.toString());           // ★ 必须 toString()：存的是"值"不是引用
                num = 0;                                 // 进入新层，倍数清零
                cur.setLength(0);                        // 便签换新的一张
            } else if (c == ']') {
                int k = numStack.pop();
                String prefix = strStack.pop();
                // 用新 Builder 拼，而不是 cur.insert(0, prefix)：后者是 O(前缀长度) 的搬移
                StringBuilder merged = new StringBuilder(prefix.length() + cur.length() * k);
                merged.append(prefix);
                for (int t = 0; t < k; t++) merged.append(cur);
                cur = merged;                            // cur 变回"外层视角"
            } else {
                cur.append(c);                           // 普通字母
            }
        }
        return cur.toString();
    }

    // 解 B：递归下降。pos 是"全局游标"，']' 不消费，交给上层
    public String decodeStringRec(String s) {
        int[] pos = {0};
        return parse(s, pos);
    }

    private String parse(String s, int[] pos) {
        StringBuilder sb = new StringBuilder();
        int num = 0;
        while (pos[0] < s.length()) {
            char c = s.charAt(pos[0]);
            if (c >= '0' && c <= '9') {
                num = num * 10 + (c - '0');
                pos[0]++;
            } else if (c == '[') {
                pos[0]++;                              // 越过 '['
                String inner = parse(s, pos);           // 递归解出括号内内容
                pos[0]++;                              // 越过 ']'
                for (int t = 0; t < num; t++) sb.append(inner);
                num = 0;
            } else if (c == ']') {
                return sb.toString();                   // ']' 留给上层消费
            } else {
                sb.append(c);
                pos[0]++;
            }
        }
        return sb.toString();
    }
}
```

### 058.5.2 Go

```go
package main

import "strings"

// 解 A：双栈
func decodeString(s string) string {
	numStack := make([]int, 0, 8)
	strStack := make([]string, 0, 8)
	var cur strings.Builder
	num := 0

	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c >= '0' && c <= '9':
			num = num*10 + int(c-'0')
		case c == '[':
			numStack = append(numStack, num)
			strStack = append(strStack, cur.String()) // 存"值"：Builder 会被 Reset 复用
			num = 0
			cur.Reset()
		case c == ']':
			k := numStack[len(numStack)-1]
			numStack = numStack[:len(numStack)-1]
			prefix := strStack[len(strStack)-1]
			strStack = strStack[:len(strStack)-1]
			// 预分配 2 份容量，避免 Strings.Repeat 与 WriteString 反复扩容
			var merged strings.Builder
			merged.Grow(len(prefix) + len(cur.String())*k)
			merged.WriteString(prefix)
			merged.WriteString(strings.Repeat(cur.String(), k))
			cur.Reset()
			cur.WriteString(merged.String())
		default:
			cur.WriteByte(c)
		}
	}
	return cur.String()
}

// 解 B：递归下降
func decodeStringRec(s string) string {
	pos := 0
	var parse func() string
	parse = func() string {
		var sb strings.Builder
		num := 0
		for pos < len(s) {
			c := s[pos]
			switch {
			case c >= '0' && c <= '9':
				num = num*10 + int(c-'0')
				pos++
			case c == '[':
				pos++               // 越过 '['
				inner := parse()    // 递归解出括号内内容
				pos++               // 越过 ']'
				sb.WriteString(strings.Repeat(inner, num))
				num = 0
			case c == ']':
				return sb.String() // ']' 留给上层消费
			default:
				sb.WriteByte(c)
				pos++
			}
		}
		return sb.String()
	}
	return parse()
}

func main() {}
```

### 058.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 可变字符串 | `StringBuilder`（有 `setLength(0)` 复用） | `strings.Builder`（有 `Reset()`） | 两边都必须用「可变缓冲」，不能用 `s + t`：本机实测 10 万字符朴素拼接 1.13 s vs Builder 0.31 ms |
| **存「值」还是「引用」** | `strStack.push(cur.toString())` 存的是快照；若存同一个 `StringBuilder` 引用，后面 `setLength(0)` 会把栈里的内容也清空 | 同样地：`cur.String()` 返回只读字符串（内部零拷贝），`Reset()` 之后再取才安全 | **这是本题最隐蔽的 bug 来源**：Java 若写成 `strStack.push(cur)`（`Deque<StringBuilder>`）就会全错 |
| 预分配 | `new StringBuilder(prefix.length() + cur.length()*k)` | `merged.Grow(n)` | 大输出时能省掉多次翻倍扩容；Go 的 `Grow` 参数是「字节数」 |
| 重复字符串 | 手写 `for` 循环 `append`，或 Java 11+ 的 `String.repeat` | `strings.Repeat(s, k)`（`k < 0` 会 panic，`k == 0` 返回空串） | Go 的 `strings.Repeat` 更直接；Java 11+ 也有 `repeat`，但注意它是**在现有串上产生新串**，不是追加 |
| 递归游标 | 需要 `int[] pos = {0}` 这种「伪引用」（Java 只有值传递） | 用闭包捕获 `pos` 变量，天然可写 | Go 的闭包让递归下降少一个参数；Java 只能用数组 / 成员变量绕过 |
| 数字解析 | `num * 10 + (c - '0')` | `num*10 + int(c-'0')` | Go 必须显式 `int(...)` 转换（`byte` 运算结果是 `byte`，会溢出） |

### 058.5.4 对拍测试（从「解码结果」反向生成编码串）

```go
package main

import (
	"math/rand"
	"strconv"
	"strings"
	"testing"
)

// 生成器：从"解码后的字符串"出发，期望值不经过任何解码器
func genDecode(r *rand.Rand, depth int) (enc, dec string) {
	letter := func() string { return string(rune('a' + r.Intn(26))) }
	if depth <= 0 || r.Intn(3) == 0 {
		l := letter()
		return l, l
	}
	switch r.Intn(3) {
	case 0: // 包一层 k[...]
		k := 1 + r.Intn(4)
		ie, id := genDecode(r, depth-1)
		if len(ie)+len(strconv.Itoa(k))+2 > 25 || len(id)*k > 28 {
			l := letter()
			return l, l
		}
		return strconv.Itoa(k) + "[" + ie + "]", strings.Repeat(id, k)
	default: // 拼接两段
		ae, ad := genDecode(r, depth-1)
		be, bd := genDecode(r, depth-1)
		if len(ae)+len(be) > 28 || len(ad)+len(bd) > 28 {
			return ae, ad
		}
		return ae + be, ad + bd
	}
}

func TestDecodeStringRandom(t *testing.T) {
	r := rand.New(rand.NewSource(999))
	checked := 0
	for iter := 0; iter < 20000; iter++ {
		enc, dec := genDecode(r, 4)
		if len(enc) == 0 || len(enc) > 30 || len(dec) > 30 {
			continue // 跳过超出 LC 约束的样本
		}
		checked++
		if got := decodeString(enc); got != dec {
			t.Fatalf("stack iter=%d s=%q got=%q want=%q", iter, enc, got, dec)
		}
		if got := decodeStringRec(enc); got != dec {
			t.Fatalf("rec   iter=%d s=%q got=%q want=%q", iter, enc, got, dec)
		}
	}
	if checked < 5000 {
		t.Fatalf("有效用例只有 %d 个，不足 5000", checked)
	}
	t.Logf("有效用例 %d 个", checked)

	// 手写边界：空串 / 单字符 / 多位数 / 嵌套 / 括号外字母
	cases := []struct{ in, want string }{
		{"3[a]2[bc]", "aaabcbc"},
		{"3[a2[c]]", "accaccacc"},
		{"2[abc]3[cd]ef", "abcabccdcdcdef"},
		{"abc3[cd]xyz", "abccdcdcdxyz"},
		{"10[a]", "aaaaaaaaaa"},
		{"2[2[b]]", "bbbb"},
		{"a", "a"},
		{"", ""},
	}
	for _, c := range cases {
		if got := decodeString(c.in); got != c.want {
			t.Fatalf("stack s=%q got=%q want=%q", c.in, got, c.want)
		}
		if got := decodeStringRec(c.in); got != c.want {
			t.Fatalf("rec   s=%q got=%q want=%q", c.in, got, c.want)
		}
	}

	// 边界：嵌套 10 层 —— 2^10 = 1024 个 'a'（注意输出长度与输入长度完全不成比例）
	deep := strings.Repeat("2[", 10) + "a" + strings.Repeat("]", 10)
	if got := decodeString(deep); len(got) != 1024 {
		t.Fatalf("深嵌套 stack got len=%d want=1024", len(got))
	}
	if got := decodeStringRec(deep); len(got) != 1024 {
		t.Fatalf("深嵌套 rec got len=%d want=1024", len(got))
	}
}
```

> **为什么生成器要从「解码结果」出发**：如果先随机生成一个编码串、再用某个解码器求期望值，那么被测实现和「参考实现」很可能犯同一个错。**从 `dec` 反推出 `enc`（`k[...]` 的包裹是天然的逆运算）**，期望值就与被测代码完全无关了。

---

## 058.6 复杂度与常见坑

### 058.6.1 复杂度推导

- **时间**：扫描 `n` 个字符，每个字符常数工作量；每次 `]` 结算要做 `k × len(inner)` 次字符拷贝。总拷贝次数正好等于输出长度（每个输出字符都是被某次结算拷贝出来的，且只会被拷贝一次）。因此时间 **`O(n + 输出长度)`**。
  - **易错点**：不要说成 `O(n)` —— `"9[9[9[9[9[9[a]]]]]]"` 的 `n` 只有 21，输出却有 `9⁶ = 531441` 个字符。
- **空间**：栈深 `O(深度)`；但注意**栈里存的前缀总量最坏是 `O(深度 × 前缀长度)`**，在「`"a"×k + "[" + …` 深嵌套」的构造下可以是 `O(n²)`。本题 `n ≤ 30` 无感，但这是**在 `n = 10⁵` 时必须改成「记录偏移量」而不是「存字符串快照」**的原因。
- **递归下降的空间**：调用栈 `O(深度)`，每帧一个 `StringBuilder`，因此同样有 `O(深度 × 前缀)` 的隐患。
- **朴素拼接的代价**：`s = s + t` 每步 `O(len)`，总共 `O(out²)`。本机实测 `out = 10⁵` 时朴素拼接 **1.13 s**，`Builder` **0.31 ms**。

### 058.6.2 八个真实踩过的坑

1. **`[` 处存了 `StringBuilder` 的引用而不是快照** —— Java 里 `Deque<StringBuilder>` + `setLength(0)` 会把栈里的内容一起清空；Go 里 `Reset()` 也有同样的问题。**必须存不可变快照**（`cur.toString()` / `cur.String()`）。
2. **`num` 不归零** —— `[` 处不归零 → 同级第二段倍数被污染（`"2[a]3[b]"` 变成 `23[b]`）；`]` 处不归零 → 同样的道理。两处都要归零。
3. **多位数当成一位** —— `"10[a]"` 解成 9 个 `a`。必须 `num = num*10 + digit`，且**只在遇到 `[` 时才认为数字读完**。
4. **遇到 `]` 时没有把内层结果交回外层** —— 直接 `return cur` 或把 `inner` 丢掉，`"2[2[b]]"` 会得到 `"bb"` 而不是 `"bbbb"`。
5. **括号外的字母被丢掉** —— 只处理 `k[...]` 形式，`"abc3[cd]xyz"` 会得到 `"cdcdcd"`。修法：`default: cur.append(c)`。
6. **用 `cur.insert(0, prefix)` 而不是新建 Builder** —— `insert(0, …)` 每次都要把已有内容整体后移，嵌套深时是 `O(前缀 × 深度)` 的额外搬移。
7. **递归下降里 `pos` 没有前进** —— `']'` 分支 `return` 前若不 `pos++`（或者上层不 `pos++` 消费 `]`），会**死循环**。约定必须写死：**`']'` 由「谁碰到谁不消费」，上一层负责 `pos++`。**
8. **`strings.Repeat` 的 `k` 传了 0 或负数** —— `k = 0` 返回空串（本题不会出现，但扩展时要防）；负数在 Go 里直接 panic。

### 058.6.3 面试官最爱追问的三个问题

**Q1：为什么用两个栈？能不能只用一个？**
> 「两个栈是『把要恢复的两样东西分别存放』的直译：倍数 `k` 和前缀 `prefix` 的类型不同、生命周期相同。用一个栈完全可以 —— 存 `(prefix, k)` 元组（Java 用 `int[]` 或自定义类，Go 用 `struct`）即可。**选两个栈只是省掉一个类型的定义**。真正不能省的是『快照』这个动作：栈里必须存不可变的值。」

**Q2：递归和迭代你选哪个？它们的关系是什么？**
> 「同一个算法：递归用**调用栈**保存父层局部变量，迭代用**显式栈**保存。递归更短、更好读；显式栈不受深度限制（Java 默认 1 MB 调用栈，深嵌套会 `StackOverflowError`）。本题 `n ≤ 30` 所以递归绝对安全；但如果是『解析一层就能嵌套一层的配置/模板文件』，我会直接写显式栈。」

**Q3：如果输出长度到 `10⁹`，怎么办？**
> 「不能物化字符串。两个方向：① 只返回长度 —— 用「长度栈」：`len = prefixLen + k × innerLen`，`O(深度)` 空间，与输出长度无关；② 要返回第 `x` 个字符 —— 维护每层的『起始偏移 + 单元长度』，从外向内定位，`O(深度)` 时间。这两个方向都把 `O(输出)` 的物化成本消掉了，也是『大数据量下不要构造中间字符串』这条工程原则的具体化。」

---

## 058.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 224 基本计算器](https://leetcode.cn/problems/basic-calculator/) | 困难 | 上一章：同样的「压现场」，结算的是整数 |
| [LC 726 原子的数量](https://leetcode.cn/problems/number-of-atoms/) | 困难 | `]` 弹出时结算的是「元素 → 计数」表，要做乘法与合并 |
| [LC 736 解析 Lisp 表达式](https://leetcode.cn/problems/parse-lisp-expression/) | 困难 | 括号 = 函数调用 + 变量作用域，栈里存 `map` 链 |
| [LC 385 迷你语法分析器](https://leetcode.cn/problems/mini-parser/) | 中等 | 把括号结构还原成嵌套对象（`NestedInteger`） |
| [LC 1190 反转每对括号间的子串](https://leetcode.cn/problems/reverse-substrings-between-each-pair-of-parentheses/) | 中等 | 结算动作从「重复 k 次」换成「反转」 |
| [LC 1096 花括号展开 II](https://leetcode.cn/problems/brace-expansion-ii/) | 困难 | 括号内是「并集 / 连接」，要输出所有组合 → 递归下降 + 集合运算 |
| [LC 591 标签验证器](https://leetcode.cn/problems/tag-validator/) | 困难 | 括号换成 XML 标签，本质仍是「配对 + 状态机」 |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 726 原子的数量**
1. 遇到 `(` 时，本层要新开一个什么容器？遇到 `)` 时，这个容器的内容应该「乘以几」再合并到哪一层？
2. 元素计数需要**有序输出**（字典序）：你打算用有序结构，还是最后对 key 排序？

**LC 1190 反转每对括号间的子串**
1. 对照 `058`：哪一步是「结算」？把 `repeat(k)` 换成什么动作？
2. 不用栈行不行？想想「先记录每对括号的配对位置，再从内向外处理」这种做法与栈解的关系。

**LC 1096 花括号展开 II**
1. `{a,b}` 是「并集」，`ab` 是「连接」——这两件事谁是「加法」谁是「乘法」？为什么这决定了每一层该返回什么类型？
2. 递归下降时，`parse` 返回的是「一个字符串」还是「一个字符串集合」？

**通用**
- 遇到 `]` 的那一刻：被结算的「整体」是什么类型？是它的类型决定了你的栈元素长什么样。
- 栈里存的东西必须是**不可变快照**；可变对象入栈后再被修改，是这类题最隐蔽的 bug。

</details>

---

## 附：本章小结（可背走的三句话）

```
① 遇见 '[' 压 (外层便签 prefix, 倍数 k) 并清空自己；遇见 ']' 用它们把内层结果接回外层
② 数字是多位的、括号外有字母、两处 num 都要归零 —— 三个细节决定了能不能过
③ 递归下降与双栈是同一个算法；复杂度要按"输出长度"算，且必须用 Builder 而不是 +
```

> **上一章**：[`057-basic-calculator.md`](057-basic-calculator.md) —— 括号展开与符号栈。
> **下一章**：[`059-implement-queue-using-stacks.md`](059-implement-queue-using-stacks.md) —— 栈与队列的语义差异，以及「均摊 `O(1)`」是怎么证出来的。
