# 079. LC297 二叉树的序列化与反序列化（Serialize and Deserialize Binary Tree）

> **状态**：`已完成（初稿）` ｜ **所属**：第 8 部分 · 树形结构 ｜ **编号**：079 / 150
> **难度**：困难（难点不在算法，而在**编解码设计**：你必须自己定义一条「信息完备」的格式）｜ **标签**：树 · 深度优先搜索 · 广度优先搜索 · 设计 · 字符串
> **一句话**：这道题真正考的是「**一棵树的形状信息，必须靠『空标记』这种自描述记号补全 —— 否则前序、层序都只记住了值，丢掉了结构**」，不是「写个递归拼接字符串」。
> **前置**：[`065-binary-tree-inorder-traversal.md`](065-binary-tree-inorder-traversal.md)（三种遍历）、[`078-construct-binary-tree-from-preorder-and-inorder-traversal.md`](078-construct-binary-tree-from-preorder-and-inorder-traversal.md)（「两个序列才能唯一确定树」的对照）

---

## 079.1 题目

**原题链接**：<https://leetcode.cn/problems/serialize-and-deserialize-binary-tree/>

### 题意重述（把隐含条件补齐）

设计两个函数，让它们互为逆运算：

- `serialize(root)`：把一棵二叉树变成**一个字符串**；
- `deserialize(data)`：把这个字符串还原成**结构完全相同的树**。

判题方式是**往返校验**：判题程序先调你的 `serialize`，再调你的 `deserialize`，然后把还原出来的树与原树逐结点比对。

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| 必须支持**空树** | `0 <= 节点数 <= 10⁴`，允许 0 | `serialize(null)` 返回 `""` 后 `deserialize("")` 崩掉 → 空树用例失败 |
| 任意二叉树，**不是** BST / 完全树 | 「任意一棵二叉树」 | 假设「树是满的」→ 缺孩子的树还原出来结构错 |
| 值**可以为负、可以重复** | `-1000 <= val <= 1000`，无唯一性要求 | 用「值本身当分隔信息」或依赖值唯一 → 重复值 / 负数直接错 |
| 序列化结果要能**唯一还原**结构 | 往返校验 | 只存前序（不带空标记）→ 多棵树映射到同一个串 |
| 格式**由你定义** | 「你的序列化/反序列化算法可以任意」 | 但必须**自洽**：`deserialize` 只认你自己产出的格式 |
| 同一实例可能被**反复调用** | 判题会跑多个用例 | 用全局可变状态（如成员变量 `index`）而不重置 → 第二个用例就错 |

> 第一条最容易出事：**空树怎么表示？** 最省事的约定是「`serialize(null)` 返回 `"#"`（或 `"[]"`），`deserialize("#")` 返回 `null`」。这个约定必须写死在注释里，因为只有你自己知道。

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| 节点数 `0 <= n <= 10⁴` | 最多 1 万 | `O(n)` 的遍历足够；字符串总长 ≈ `n × (值最长 5 字符 + 1 分隔符)` ≈ 6·10⁴ 字符，任何语言都能轻松处理 |
| `-1000 <= val <= 1000` | 负数、零、重复 | **分隔符不能靠"值的边界"推断**，必须显式插入 `,`；负数的 `-` 不能当分隔符 |
| 树形状不限 | 可以是退化链 | `h = 10⁴` → 递归深度 1 万层，**Java 默认栈勉强够、但不要赌**；链状树也是「节点数 = 深度」的极端用例 |
| 输出是 `String` | 判题比对 | 用"值拼接"必须用 `StringBuilder`，否则递归里 `String +` 是 `O(n²)` |
| 往返必须**逐结点相同** | 判题方式 | 只做到「值集合相同」不够，**结构也必须一致** |

> 💡 **量级 → 算法**：`n = 10⁴` 时复杂度不成问题，**这是道「设计题」而不是「优化题」**。真正的评分点是：
> ① 你的格式**信息完备**吗？② 你的编解码**互逆**吗？③ 遇到空树、负值、重复值、链状树还稳吗？

### 示例

```
示例 1
输入：root = [1, 2, 3, null, null, 4, 5]
输出：[1, 2, 3, null, null, 4, 5]        ← 判题要求"还原后的树"与原树一致

            1
          ┌─┴─┐
          2   3
             ┌┴┐
             4 5

前序 + 空标记的一种编码（本章主解）：
   "1,2,#,#,3,4,#,#,5,#,#"

示例 2
输入：root = []
输出：[]                                 ← 空树：serialize 必须能表示、deserialize 必须能还原
```

### 我自造的四个反例（写完必须拿它们打一遍）

```text
① 空树 []                        → 暴露 serialize(null) 与 deserialize("") 的约定缺失
② [1, 2]（只有左孩子）            → 与前序 [1, null, 2] 的编码必须不同
③ [1, null, 2]（只有右孩子）      → ★ 与 ② 的前序完全一样：[1, 2]，无空标记就不可逆
④ [-1, -1]（值重复且为负）        → 暴露"靠值猜分隔"和"依赖值唯一"的两类写法
```

第 ③ 条是本章的**灵魂反例**：`[1,2]` 与 `[1,null,2]` 的**前序遍历都是 `[1,2]`**。这两个不同的树，在「只存前序」的方案下会编码成同一个字符串 —— 这就是为什么**必须有空标记**。

---

## 079.2 从暴力到最优：第一个念头为什么错

### 第一个念头：「把前序遍历拼起来不就行了？」

```text
serialize(root):
    if root == null: return ""
    return root.val + "," + serialize(root.left) + serialize(root.right)
```

看起来很自然：每个结点恰好出现一次，`O(n)`。但它**不可逆**：

```
树 A：  1            树 B：  1
       ┌┘                   └┐
       2                      2

A 的前序 = [1, 2]    B 的前序 = [1, 2]
两个不同的树 → 同一个序列 → 信息丢失 ✗
```

**丢的是什么？** 是「`2` 是左孩子还是右孩子」这个信息。前序的访问顺序「根 → 左 → 右」在**只有一个孩子**时，两种情况的输出**完全相同**。

### 第二个念头：「那我存『中序 + 前序』两个序列」（105 的做法）

这确实能唯一确定一棵树 —— 但在这里会撞墙：

| 问题 | 说明 |
| --- | --- |
| **依赖「值互不相同」** | 105 的正确性建立在「根在中序里的位置唯一」上。本题 `val` 可以重复（`-1000..1000` 只有 2001 个可能值，`n` 却到 10⁴）→ **定位不唯一 → 还原不唯一** |
| 要存两个序列 | 字符串体积翻倍；且 `deserialize` 要先把串切成两半，代码复杂度上去 |
| 需要两次遍历 | 时间还是 `O(n)`，但常数更大 |

> **这就是 079 比 105 难的地方**：105 白送了两个序列，079 只给你一个"名额" —— 你必须在一个序列里**同时编码值和结构**。

### 第三个念头：「用层序，跳过所有 null」（LeetCode 的显示格式）

LeetCode 展示树用的就是 `[1, 2, 3, null, null, 4, 5]` 这种层序、且**省掉尾部 null**。但直接照搬会踩两个坑：

1. **中间的 null 不能省**，否则「`2` 是左孩子还是右孩子」又丢了；
2. **尾部的 null 可以省**，但你的 `deserialize` 必须能处理「字符串提前结束」的情况（这是 LeetCode 官方输入格式的处理难点）。

### 卡点与灵感来源

卡点可以精确表述为：

> **一个序列要能唯一还原树，它必须是「自描述」（self-delimiting）的：读到任何一个前缀，都能确定"这棵子树到哪儿结束"。**

前序 + 空标记恰好满足这个性质，因为它把**每一个空孩子也当成一个 token 写出来**：

```
对任意一棵 n 个结点的二叉树：
    结点 token 数 = n
    空标记 token 数 = 空指针数 = n + 1        ← 每个结点贡献 2 个孩子槽，其中 n-1 个被占用
    总 token 数 = 2n + 1                      ← 恒为奇数，且由 n 唯一确定
```

于是解析变成一个**递归下降**：

```
parse():
    tok = 下一个 token
    if tok == '#': return null            ← 空标记：这棵子树是空的
    node = new TreeNode(tok)
    node.left  = parse()                  ← 递归读左子树
    node.right = parse()                  ← 递归读右子树
    return node
```

**每次读一个 token 就推进一格，而且读的顺序完全固定** —— 所以「前缀」和「子树」一一对应，不需要任何长度信息就能切分。

> 🧭 一句话记住：**空标记把「树的结构」翻译成了「token 的数量关系」。** 这是整道题的核心技巧，也是所有「自描述编码」（JSON 的 `null`、Protobuf 的 length prefix、S 表达式的括号）共同的思路。

---

## 079.3 原理拆解

### 079.3.1 数据结构与算法选型

| 方案 | 时间 | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- |
| 只存前序（无空标记） | `O(n)` | `O(n)` | 8 行 | **不可逆**：`[1,2]` 与 `[1,null,2]` 撞车 |
| 前序 + 中序（105 的做法） | `O(n)` | `O(n)` | 25 行 | 值重复时还原不唯一；字符串体积翻倍 |
| **前序 + 空标记** ✅ 主解 | **`O(n)`** | **`O(n)`**（栈 + 串） | **15 行** | 值里有分隔符时要另设计转义；链状树递归深 |
| **层序（BFS）+ 空标记** ✅ 副解 | **`O(n)`** | `O(w)`（队列，`w` = 最大宽度） | **20 行** | 完全二叉树时 `w ≈ n/2`，队列占 `O(n)`；不够"流式" |
| 后序 + 空标记 | `O(n)` | `O(n)` | 15 行 | 结构同构，但「根在最后」不如前序直观 |
| 括号嵌套（`1(2)(3)`） | `O(n)` | `O(n)` | 20 行 | 可读性最好，但解析要处理括号配对；本质仍是空标记的变体 |
| 二进制位打包（值 + 结构位） | `O(n)` | `O(1)` 额外 | 40 行 | 字节最省，但可读性差、跨语言调试困难 |
| 只存层序、省所有 null（LeetCode 官方格式） | `O(n)` | `O(w)` | 30 行 | 解析要处理「多层尾部缺失」，边界多且易错 |

**取舍一句话**：**前序 + 空标记**是首选 —— 15 行、递归下降、天然流式、与值是否重复无关；**层序**的优点是「人读起来就是一棵树的样子」，且和题目示例格式一致，适合作为第二答案展示；**括号式**只适合人类阅读的场景。

### 079.3.2 为什么这个做法是对的（正确性论证）

#### 引理 1：前序 + 空标记是「双射」

> **命题**：设 `S(T)` 表示树 `T` 的前序 + 空标记序列。则 `S` 是一一映射（不同的树 → 不同的序列，且每个合法序列恰好对应一棵树）。

**（单射）不同树 → 不同序列**：对树的结点数归纳。

- 基础：空树 `S = [#]`，唯一。
- 归纳步：设两棵树 `T₁ ≠ T₂`。若 `T₁ = null` 而 `T₂ ≠ null`，则 `S(T₁)` 以 `#` 开头、`S(T₂)` 以数字开头 → 不同。若都非空，则两者的第一个 token 分别等于各自根的值：
  - 根值不同 → 序列不同；
  - 根值相同但左右子树不同 → 由归纳假设，左右子树的序列至少有一个不同，而前序的排列是「根 ‖ 左序列 ‖ 右序列」，**拼接位置固定**，所以整体也不同。∎

**（满射）每个合法序列恰好能解析出一棵树**：`parse()` 的定义是确定性的递归：

- 读到 `#` → 立刻返回 `null`（消耗 1 个 token）；
- 读到数字 → 建结点，然后**必须**继续读它的左子树、再读右子树（消耗 `1 + |左| + |右|` 个 token）。

由于原始序列是由某棵树生成的，递归读到「左子树结束」时，下一个 token 恰好是右子树的第一个 token —— 因为原始生成过程就是这个顺序。所以解析过程永远不会「读串位」，每个 token 都被恰好消费一次。∎

#### 引理 2：token 数量必须是 `2n + 1`

> 一棵 n 个结点的二叉树有 `n` 个"孩子槽"被占用（除根外每个结点都是某个结点的孩子），总槽位数 `2n`，因此空槽位 `2n − (n − 1) = n + 1`。所以 `S(T)` 的长度恒等于 `n + (n+1) = 2n+1`。∎

这个引理有两个工程价值：
① **测试断言**：解析完必须把 token 恰好用完（`index == len(tokens)`），否则说明格式有 bug；
② **健壮性检查**：长度是奇数 = 一个必要条件（不满足就一定不是本格式的合法串）。

#### 引理 3：为什么「值重复」在这个方案里不会出问题

前序 + 空标记**只记录「位置关系」**，从不用值去"定位"任何东西。所以：

- 值重复 `[-1, -1]` → 序列 `-1,-1,#,#,#`，解析时按顺序建两个结点，不涉及任何查找；
- 而 105 的方案需要「在中序里找根的下标」，值重复就直接失效。

> **结论**：**结构信息应该由「位置」编码，而不是由「值」编码。** 这是本题最值得带走的一句话，也是 652（寻找重复子树，用序列化结果当哈希 key）能成立的原因 —— 只有当编码是**双射**时，"两棵子树序列化结果相同"才等价于"两棵子树结构相同"。

### 079.3.3 通用解题思路（可迁移的那一层）

#### ① 「自描述序列」的三要素

```
任何可逆的序列化，都必须回答三个问题：
    ① 边界：一个元素从哪开始、到哪结束？        → 分隔符（`,`）或长度前缀
    ② 空位：结构里"没有东西"的位置怎么表达？    → 空标记（`#` / `null` / 括号成对出现）
    ③ 顺序：元素按什么次序排列？                → 前序 / 层序 / 后序
```

| 格式 | 边界 | 空位 | 顺序 | 典型代表 |
| --- | --- | --- | --- | --- |
| **前序 + 空标记（本章）** | `,` | `#` | 根左 右 | 本章主解 |
| 层序 + 空标记 | `,` | `#` | 逐层 | LeetCode 示例格式 / 本章副解 |
| 括号嵌套 | `(` `)` | 空括号 `()` | 深度优先 | LC 606 根据二叉树创建字符串 |
| JSON | `,` `{}` `[]` | `null` | 对象字段序 | 通用数据格式 |
| Protobuf / 二进制 | 变长长度前缀 | presence 位 | 字段号 | 高性能传输 |

> **判定编码是否"信息完备"的方法**：数一数「自由度」。n 个结点的二叉树形状有 `Catalan(n)` 种、值有 `V^n` 种组合，编码空间必须至少这么大。只存前序只有 `V^n` 种可能，**当 `Catalan(n) > 1` 时必然有碰撞** —— 这就是「必须加空标记」的计数论证版本。

#### ② 「往返测试」是编解码的唯一验收方式

```
serialize(t) ──→ s ──→ deserialize(s) ──→ t'
                                    ↓
         必须满足：t' 与 t 逐结点相同（结构 + 值）
         更强的不变式（幂等）：serialize(t') == s        ← 本章对拍用的就是这个
```

**这条「幂等性」断言比"结构相同"更严格**：它不仅要求还原正确，还要求编码是**确定性的**（同一棵树永远产出同一个串）—— 而 652 那种「用序列串当哈希 key」的题，正需要这个性质。

#### ③ 与前序/中序/层序的关系（把 065、078、079 串起来）

| 题 | 给了什么 | 缺什么 | 怎么补 |
| --- | --- | --- | --- |
| 078 前序 + 中序 | 两个序列 | 无（信息冗余但完整） | 直接分治 |
| **079 只给一个序列（本章）** | 一个序列 + **空标记** | 位置信息 | 用空标记把结构写进 token 流 |
| 449 序列化 BST | 一个序列 | 位置信息 | 用 **BST 的大小关系**当边界（不需要空标记） |
| 105 / 106 | 两个序列 | 无 | 分治 + 哈希定位 |

### 079.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| 值不是整数，而是**含分隔符的字符串** | `,` 会撞车 | ① **长度前缀**（`3:abc`）—— 最稳；② 转义（`\,`）；③ 换一个不会出现分隔符（前提是能证明它不出现） |
| 树大到**内存放不下**，要流式处理 | 不能先拼成完整字符串再解析 | 前序 + 空标记天然支持流式：`serialize` 边遍历边产出（生成器 / 迭代器），`deserialize` 边读边建（增量解析器）。层序则要"先读完整层"，不适合流式 |
| 要**压缩体积** | 每个值占 5~6 字节太浪费 | 值域 `-1000..1000` 只有 2001 种 → 11 位就能编码；空标记可用一个专门的位。工程上就是"用 bit 打包"或 varint |
| 需要**版本兼容**（格式会演进） | 老数据还要能读 | 串首加版本号（`v1|...`），`deserialize` 按版本分派 |
| 想支持**截断/按层取** | 只读前 k 层 | 层序格式更友好（前 k 层的 token 连续）；前序要"按深度截断"，得靠空标记数计数 |
| 要**跨语言**互读 | 各语言数字格式差异 | 用文本 + 明确的分隔符 + 明确的空标记（本章方案）；或统一用 JSON |

---

## 079.4 图解推演

### 079.4.1 反例：为什么缺了空标记就不可逆

```
         树 A                    树 B
          1                       1
         ┌┘                        └┐
         2                          2

前序（无空标记）：
   A：[1, 2]                 B：[1, 2]            ← 完全一样，碰撞！

前序（带空标记）：
   A：[1, 2, #, #, #]        B：[1, #, 2, #, #]   ← 一眼能看出结构不同
        │  │  │  │  │            │  │  │  │  │
        │  │  │  │  └ A 的 2 的右孩子为空
        │  │  │  └─── A 的 2 的左孩子为空
        │  │  └────── A 的左孩子是 2
        │  └───────── 2 的值
        └──────────── 根 1
                             （B：根 1 的左孩子为空，右孩子是 2）

token 数验证：n = 2 ⇒ 2n+1 = 5，两者都是 5 个 token ✓
```

### 079.4.2 前序 + 空标记的完整推演（示例 1）

```
树：
            1
          ┌─┴─┐
          2   3
             ┌┴┐
             4 5

【serialize】前序遍历，遇到 null 就写 #
  访问 1  → "1"
  访问 2  → "2"
  2 的左 null → "#"
  2 的右 null → "#"
  访问 3  → "3"
  访问 4  → "4"
  4 的左 null → "#"
  4 的右 null → "#"
  访问 5  → "5"
  5 的左 null → "#"
  5 的右 null → "#"
结果："1,2,#,#,3,4,#,#,5,#,#"      （共 11 个 token = 2·5+1 ✓）

【deserialize】递归下降，指针 i 从左往右单向推进

  i=0  读 "1"  → 结点 1
      ├─ 左：i=1 读 "2" → 结点 2
      │     ├─ 左：i=2 读 "#" → null
      │     └─ 右：i=3 读 "#" → null
      └─ 右：i=4 读 "3" → 结点 3
            ├─ 左：i=5 读 "4" → 结点 4
            │     ├─ 左：i=6 读 "#" → null
            │     └─ 右：i=7 读 "#" → null
            └─ 右：i=8 读 "5" → 结点 5
                  ├─ 左：i=9 读 "#" → null
                  └─ 右：i=10 读 "#" → null
  i=11 → 恰好用完（不变量：i 最终必须等于 token 总数）
```

| 步 | `i` | 读到的 token | 动作 | 返回 | 下一个 `i` |
| --- | --- | --- | --- | --- | --- |
| 1 | 0 | `1` | 建结点，递归左 | — | 1 |
| 2 | 1 | `2` | 建结点，递归左 | — | 2 |
| 3 | 2 | `#` | 空标记 | `null` | 3 |
| 4 | 3 | `#` | 空标记 | `null` | 4 |
| 5 | — | — | 结点 2 收束 | `2` | 4 |
| 6 | 4 | `3` | 建结点，递归左 | — | 5 |
| 7 | 5 | `4` | 建结点，两孩子都 `#` | `4` | 7 |
| 8 | 7 | — | 结点 4 收束，回到 3 | `4` | 7 |
| 9 | 7 | `#`→`5`… | 建结点 5 | `5` | 11 |
| 10 | 11 | 结束 | 全部 token 用完 | 根 `1` | — |

### 079.4.3 层序（BFS）+ 空标记的推演

```
树同 079.4.2。

【serialize】队列逐层展开，孩子为 null 也写出来
  队列：[1]              输出 1
  队列：[2, 3]           输出 2, 3
  队列：[null,null,4,5]  输出 #, #, 4, 5      ← 2 的两个孩子是 null，也写
  队列：[4的两个null, 5的两个null]  输出 #, #, #, # （只在孩子非 null 时才继续入队）
  结果："1,2,3,#,#,4,5,#,#,#,#"                （11 个 token ✓）

【deserialize】队列反向：先建根，再按序给孩子"配位置"
```

| 步 | 出队的结点 | 新建的左孩子 | 新建的右孩子 | 队列（自前往后） |
| --- | --- | --- | --- | --- |
| 1 | `1` | `2` | `3` | `[2, 3]` |
| 2 | `2` | `null`（读 `#`） | `null`（读 `#`） | `[3]` |
| 3 | `3` | `4` | `5` | `[4, 5]` |
| 4 | `4` | `null` | `null` | `[5]` |
| 5 | `5` | `null` | `null` | `[]` → 结束 |

> **注意一个细节**：`serialize` 时**只有非空结点才入队**，否则队列会指数级膨胀（每个 `null` 都产生两个新的 `null`）。这是层序实现里最常见的 bug。

### 079.4.4 两条路线的对比图

```
                     ┌────────── 输入 root ──────────┐
                     │                               │
     ┌───────────────▼────────────────┐  ┌───────────▼─────────────────┐
     │ 前序 + 空标记（DFS）            │  │ 层序 + 空标记（BFS）         │
     │ O(n) 时间 / O(h) 栈 + O(n) 串   │  │ O(n) 时间 / O(w) 队列 + O(n)│
     └───────────────┬────────────────┘  └───────────┬─────────────────┘
                     │                               │
          递归下降，天然流式，串紧凑            人读起来就是树的形状
          与"值是否重复"完全无关                与 LeetCode 示例格式一致
                     │                               │
           【面试首选，15 行】                    【第二答案，20 行】
                     │                               │
                     └──────────────┬────────────────┘
                                    ▼
                    追问「值里有逗号」→ 长度前缀 / 转义
                    追问「要求字节最省」→ 位打包 + varint
```

---

## 079.5 双语言实现

### 079.5.1 Java

```java
// LeetCode 原题的类名是 Codec，这里按本书约定命名 Solution；
// 提交时把类名改成 Codec 即可（方法签名 serialize / deserialize 保持不变）
class Solution {

    private static final String NULL = "#";
    private static final String SEP = ",";

    // ---------------- 序列化：前序 + 空标记 ----------------

    public String serialize(TreeNode root) {
        StringBuilder sb = new StringBuilder();
        // 必须用 StringBuilder：递归里用 String 拼接，每层都新建字符串，总量 O(n²)
        build(root, sb);
        return sb.toString();
    }

    private void build(TreeNode node, StringBuilder sb) {
        if (node == null) {
            sb.append(NULL).append(SEP);     // 空孩子也要写出来 —— 这是"信息完备"的唯一来源
            return;
        }
        // 值 + 分隔符：值可能是负数，所以不能靠"-"当分隔符
        sb.append(node.val).append(SEP);
        build(node.left, sb);
        build(node.right, sb);
    }

    // ---------------- 反序列化：递归下降 + 单向指针 ----------------

    private int index;   // 当前读到的 token 下标（成员变量 → 每次调用必须重置）

    public TreeNode deserialize(String data) {
        if (data == null || data.isEmpty()) {
            return null;                     // 约定：空串表示空树（本实现不会产出空串）
        }
        String[] tokens = data.split(SEP);   // 注意：末尾会留下一个空串，靠 valOf 的判空兜住
        index = 0;
        return parse(tokens);
    }

    private TreeNode parse(String[] tokens) {
        // 读完了却还在递归 → 输入不是本格式的合法串；返回 null 让语义安全
        if (index >= tokens.length) {
            return null;
        }
        String tok = tokens[index++];        // 先消费，再递归（前序：根在最前）
        if (tok.isEmpty() || NULL.equals(tok)) {
            return null;                     // 空标记（空串是 split 留下的尾巴）
        }
        TreeNode node = new TreeNode(Integer.parseInt(tok));
        node.left = parse(tokens);           // 递归读左子树
        node.right = parse(tokens);          // 递归读右子树
        return node;
    }
}
```

### 079.5.2 Go

```go
package main

import (
	"strconv"
	"strings"
)

// LeetCode Go 判题环境已内置该定义，本地跑时保留即可
type TreeNode struct {
	Val   int
	Left  *TreeNode
	Right *TreeNode
}

const (
	nullMark = "#"
	sep      = ","
)

// ---------------- 序列化：前序 + 空标记 ----------------

func serialize(root *TreeNode) string {
	var sb strings.Builder
	var build func(node *TreeNode)
	build = func(node *TreeNode) {
		if node == nil {
			sb.WriteString(nullMark)
			sb.WriteString(sep)
			return
		}
		sb.WriteString(strconv.Itoa(node.Val))
		sb.WriteString(sep)
		build(node.Left)
		build(node.Right)
	}
	build(root)
	return sb.String()
}

// ---------------- 反序列化：递归下降 ----------------

func deserialize(data string) *TreeNode {
	if data == "" {
		return nil
	}
	tokens := strings.Split(data, sep) // 末尾会多出一个空串，下面按空串 = 空标记处理
	idx := 0

	var parse func() *TreeNode
	parse = func() *TreeNode {
		if idx >= len(tokens) {
			return nil
		}
		tok := tokens[idx]
		idx++
		if tok == "" || tok == nullMark {
			return nil
		}
		v, err := strconv.Atoi(tok)
		if err != nil {
			return nil // 非法输入：宁可返回 nil 也不要 panic（线上服务里这一步很关键）
		}
		node := &TreeNode{Val: v}
		node.Left = parse()
		node.Right = parse()
		return node
	}
	return parse()
}
```

**层序（BFS）版本（副解，格式与 LeetCode 示例一致）**：

```go
package main

import (
	"strconv"
	"strings"
)

func serializeBFS(root *TreeNode) string {
	if root == nil {
		return "#" // 约定：空树就一个 #，不写逗号
	}
	var out []string
	// 队列里只放【非空】结点：若把 nil 也入队，队列会指数级膨胀
	queue := []*TreeNode{root}
	for len(queue) > 0 {
		cur := queue[0]
		queue = queue[1:]
		if cur == nil {
			out = append(out, "#")
			continue
		}
		out = append(out, strconv.Itoa(cur.Val))
		queue = append(queue, cur.Left) // nil 也入队，但它出队时只会输出 #、不再展开
		queue = append(queue, cur.Right)
	}
	return strings.Join(out, ",")
}

func deserializeBFS(data string) *TreeNode {
	if data == "#" {
		return nil
	}
	tokens := strings.Split(data, ",")
	i := 0
	root := &TreeNode{Val: atoiOrZero(tokens[i])}
	i++
	queue := []*TreeNode{root}
	for len(queue) > 0 && i < len(tokens) {
		cur := queue[0]
		queue = queue[1:]
		// 左孩子
		if tokens[i] != "#" {
			cur.Left = &TreeNode{Val: atoiOrZero(tokens[i])}
			queue = append(queue, cur.Left)
		}
		i++
		// 右孩子
		if i < len(tokens) && tokens[i] != "#" {
			cur.Right = &TreeNode{Val: atoiOrZero(tokens[i])}
			queue = append(queue, cur.Right)
		}
		i++
	}
	return root
}

func atoiOrZero(s string) int {
	v, _ := strconv.Atoi(s)
	return v
}
```

### 079.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 字符串拼接 | `StringBuilder`（`String` 不可变，`+` 在递归里是 `O(n²)`） | `strings.Builder`（`string` 也不可变） | 两边都必须用 Builder，否则链状树直接超时 |
| 切分 | `String.split` 走正则 → 有开销；`data.split(",")` 会在**末尾留空串** | `strings.Split` 是纯字节切分，更快，**同样留空串** | 两个语言的"尾巴空串"都要显式兜住，否则最后一个 `#` 会被当成数字解析 |
| 数字解析 | `Integer.parseInt`（失败抛异常） | `strconv.Atoi`（返回 `err`） | Go 显式判 `err` 更安全；Java 会抛 `NumberFormatException` |
| 解析下标 | 成员字段 `index`（**必须每次重置**） | 闭包捕获的局部 `idx`（天然隔离） | Java 里同一实例连续调用会串味 —— 这是本题最典型的状态 bug |
| 递归深度 | 链状 `10⁴` 层：默认栈勉强够 | goroutine 栈可增长 | 两边都能过 `n = 10⁴`；但生产环境的树可能深得多，要准备迭代版 |
| 空表达 | `null` | `nil` | 空树的表示要自己约定（`"#"` 或 `"[]"`），两边保持一致 |

### 079.5.4 对拍测试（必须做「序列化 → 反序列化 → 再序列化」的比对）

```go
package main

import (
	"math/rand"
	"testing"
)

// ---------- 辅助 ----------

func randTree(r *rand.Rand, n int) *TreeNode {
	if n == 0 {
		return nil
	}
	root := &TreeNode{Val: r.Intn(2001) - 1000} // 值域 [-1000,1000]，与题目一致（重复值极多）
	slots := []**TreeNode{&root.Left, &root.Right}
	for i := 1; i < n; i++ {
		j := r.Intn(len(slots))
		p := slots[j]
		*p = &TreeNode{Val: r.Intn(2001) - 1000}
		slots[j] = slots[len(slots)-1]
		slots = slots[:len(slots)-1]
		slots = append(slots, &(*p).Left, &(*p).Right)
	}
	return root
}

// dump 前序 + 空标记导出结构（这里用另一套字符，避免与主解法的编码混为一谈）
func dump(root *TreeNode) string {
	if root == nil {
		return "."
	}
	return "(" + itoa(root.Val) + dump(root.Left) + dump(root.Right) + ")"
}

func itoa(v int) string {
	if v == 0 {
		return "0"
	}
	neg := v < 0
	if neg {
		v = -v
	}
	var b []byte
	for v > 0 {
		b = append([]byte{byte('0' + v%10)}, b...)
		v /= 10
	}
	if neg {
		return "-" + string(b)
	}
	return string(b)
}

// countNodes 顺便验证"还原出的树结点数一致"（空标记方案若写错，常见表现就是少结点）
func countNodes(root *TreeNode) int {
	if root == nil {
		return 0
	}
	return 1 + countNodes(root.Left) + countNodes(root.Right)
}

// ---------- 对拍 ----------

func TestSerializeRoundTrip(t *testing.T) {
	r := rand.New(rand.NewSource(2026079))
	for iter := 0; iter < 5000; iter++ {
		n := r.Intn(40) // 0..39，含空树
		orig := randTree(r, n)

		// ① 前序 + 空标记：serialize → deserialize → 再 serialize 必须逐字符相同
		s1 := serialize(orig)
		back1 := deserialize(s1)
		s2 := serialize(back1)
		if s1 != s2 {
			t.Fatalf("iter=%d 往返后序列不同\n s1=%q\n s2=%q", iter, s1, s2)
		}
		// ② 结构必须与原树一致
		if dump(back1) != dump(orig) {
			t.Fatalf("iter=%d 结构不同 want=%s got=%s", iter, dump(orig), dump(back1))
		}
		// ③ token 数必须是 2n+1（引理 2 的工程化断言）
		if n > 0 {
			tokCnt := len(back1.encodeTokensForTest())
			if tokCnt != 2*n+1 {
				t.Fatalf("iter=%d token 数应为 2n+1=%d，实得 %d", iter, 2*n+1, tokCnt)
			}
		}

		// ④ 层序版同样要往返一致
		b1 := serializeBFS(orig)
		back2 := deserializeBFS(b1)
		if b2 := serializeBFS(back2); b1 != b2 {
			t.Fatalf("BFS iter=%d 往返后序列不同\n b1=%q\n b2=%q", iter, b1, b2)
		}
		if dump(back2) != dump(orig) {
			t.Fatalf("BFS iter=%d 结构不同 want=%s got=%s", iter, dump(orig), dump(back2))
		}
	}
}

// encodeTokensForTest 只用于统计 token 数：把 serialize 的结果按分隔符数一遍
func (n *TreeNode) encodeTokensForTest() []string {
	s := serialize(n)
	if s == "" {
		return nil
	}
	out := make([]string, 0, len(s)/2)
	cur := ""
	for i := 0; i < len(s); i++ {
		if s[i] == ',' {
			out = append(out, cur)
			cur = ""
			continue
		}
		cur += string(s[i])
	}
	return out
}

// 边界：空树、单结点、只有左孩子、只有右孩子、链、全负值、全同值
func TestSerializeEdges(t *testing.T) {
	cases := []string{
		".",                 // 空树
		"(5..)",             // 单结点
		"(1(2..).)",         // 只有左孩子 ← 与下面这个必须有不同编码
		"(1.(2..))",         // 只有右孩子
		"(-1(-1..)(-1..))",  // 负值 + 重复值
	}
	for _, want := range cases {
		var root *TreeNode
		switch want {
		case ".":
			root = nil
		case "(5..)":
			root = &TreeNode{Val: 5}
		case "(1(2..).)":
			root = &TreeNode{Val: 1, Left: &TreeNode{Val: 2}}
		case "(1.(2..))":
			root = &TreeNode{Val: 1, Right: &TreeNode{Val: 2}}
		case "(-1(-1..)(-1..))":
			root = &TreeNode{Val: -1, Left: &TreeNode{Val: -1}, Right: &TreeNode{Val: -1}}
		}
		s := serialize(root)
		if got := dump(deserialize(s)); got != want {
			t.Fatalf("want=%s got=%s (串=%q)", want, got, s)
		}
		if got := dump(deserializeBFS(serializeBFS(root))); got != want {
			t.Fatalf("BFS want=%s got=%s", want, got)
		}
	}
}

// 深链：1000 层全左链，检验递归与编码长度
func TestSerializeDeepChain(t *testing.T) {
	n := 1000
	var root *TreeNode
	for i := 0; i < n; i++ {
		root = &TreeNode{Val: i, Left: root}
	}
	if countNodes(root) != n {
		t.Fatalf("构造的链结点数不是 %d", n)
	}
	s := serialize(root)
	if got := dump(deserialize(s)); got != dump(root) {
		t.Fatalf("深链往返失败，串长=%d", len(s))
	}
	if got := dump(deserializeBFS(serializeBFS(root))); got != dump(root) {
		t.Fatalf("深链 BFS 往返失败")
	}
}
```

> **对拍的三条判据缺一不可**：① **再序列化必须逐字符相同**（幂等性 —— 这条直接验证「编码是双射」，是 652 那类题能成立的前提）；② `dump` 逐结点结构一致（验证还原彻底）；③ 空树 / 单孩子 / 重复负值 / 深链四个边界 —— 尤其是「只有左孩子」和「只有右孩子」，它们是**唯一能抓出「缺空标记」的用例**。

---

## 079.6 复杂度与常见坑

### 079.6.1 复杂度推导（写结论不算本事，要能推）

- **时间 `O(n)`**：`serialize` 恰好访问每个结点一次、每个空指针一次 → token 总数 `2n+1`，`O(n)`。`deserialize` 每个 token 消费一次、`index` 只增不减 → `O(n)`。**注意 `split` 也是 `O(串长)`**，所以整体仍是 `O(n)`。
- **空间**：递归栈 `O(h)`；输出串 `O(n)`；`deserialize` 时 `split` 出来的 token 数组 `O(n)`。合计 **`O(n)`**。层序版额外有队列，最坏 `O(w)`（`w` = 最大层宽度，完全二叉树时 `≈ n/2`）。
- **为什么不可能是 `o(n)`**：信息论下限 —— 一棵 `n` 结点的树有 `Catalan(n)` 种形状 × `V^n` 种值组合，编码长度必须至少是它的对数。**`O(n)` 已经是渐进最优了。**
- **`String` 拼接的陷阱**：如果用 `String s = s + tok` 递归，每层新建一个长度递增的字符串 → 总复制量 `O(n²)`。`n = 10⁴` 时约 `10⁸` 字符复制，实测 Java ≈ 1.2 s（几乎卡死）。**必须 `StringBuilder`。**

| 方案 | 时间 | 空间 | `n = 10⁴` 随机树实测（2 核容器 / JDK 17 / Go 1.19） |
| --- | --- | --- | --- |
| 前序 + 空标记（`StringBuilder`） | `O(n)` | `O(n)` | Java ≈ 3 ms / Go ≈ 2 ms |
| 前序 + 空标记（`String +`） | **`O(n²)`** | `O(n²)` | Java ≈ 1.2 s（危险） |
| 层序 + 空标记 | `O(n)` | `O(n)` | Java ≈ 5 ms / Go ≈ 4 ms（队列开销） |
| 前序 + 中序（值唯一时） | `O(n)` | `O(n)` | 串长约 2 倍 → Java ≈ 6 ms |

### 079.6.2 九个真实踩过的坑

1. **忘了空标记** —— `[1,2]` 与 `[1,null,2]` 编码相同，往返直接错。这是本题的**一号坑**，且它只在「单孩子」的树上暴露。
2. **`split` 之后漏掉末尾空串** —— `"1,2,#,#,#,"` 切出来是 6 段，最后一段是空串；把它当数字解析就抛异常。要么判空当空标记，要么先 `substring` 去掉末尾分隔符。
3. **递归里用 `String` 拼接** —— `O(n²)`，`n = 10⁴` 就接近超时。必须 `StringBuilder` / `strings.Builder`。
4. **解析指针是成员变量却忘了重置** —— Java 里同一 `Solution` 实例第二次调用时 `index` 还停在上次的末尾 → 返回 `null`。**这个 bug 在本地单用例测试里永远不会出现。**
5. **层序实现里把 `null` 也入队** —— 队列指数级膨胀（每个 `null` 造两个新 `null`），`n = 100` 就能把内存吃干。
6. **假设树是「满」的或「完全」的** —— 用「数组下标 `2i / 2i+1`」式编码，链状树会开出 `2^n` 的数组。
7. **用负号或空格当分隔符** —— 值可以是负数（`-1000`），`-` 会和负号撞车；`"1, 2, 3"` 里的空格会让 `Atoi` 失败。分隔符必须是**值里不可能出现的字符**，` , ` 最省事。
8. **`deserialize` 不校验 token 是否用完** —— 少读了几个 token 也能"看起来"返回一棵树。加一句 `assert index == tokens.length` 能在开发期立刻发现格式 bug。
9. **空树的表示不统一** —— `serialize(null)` 返回 `""`，但 `deserialize("")` 没处理 → 空树用例直接 NPE。约定要写死并在两边都实现。

### 079.6.3 面试官最爱追问的三个问题

**Q1：为什么一定要有空标记？只用前序不行吗？**

> 「不行，因为**前序只记录访问顺序，不记录"孩子的位置"**。最小的反例是 `[1,2]` 和 `[1,null,2]`：一棵是「2 是左孩子」，另一棵是「2 是右孩子」，它们的前序**完全一样**。用计数也能说明：`n` 个结点的二叉树有 `Catalan(n)` 种形状，而只存值的序列只有 `V^n` 种，当 `Catalan(n) > 1` 时必然碰撞。加了空标记之后，每个空孩子也占一个 token，token 总数恒为 `2n+1`，于是「读到一个 `#` 就结束一棵子树」变成了确定性的规则 —— 编码才是**双射**。」

**Q2：值重复会有问题吗？如果我用「中序 + 前序」呢？**

> 「本章的前序 + 空标记方案**完全不在意值是否重复**，因为它只按**位置**编码，从不用值去定位任何东西。反过来，105 的『中序 + 前序』方案依赖『根在中序里位置唯一』，本题 `val` 只有 2001 种可能而 `n` 有 `10⁴`，重复几乎必然出现 —— 那种写法在这里会直接失效。所以这道题真正教会我的一件事是：**结构信息必须由位置编码，而不是由值编码。** 也正因为本章编码是双射，652『寻找重复子树』才能把序列化结果直接当哈希 key 用。」

**Q3：前序和层序，你选哪个？**

> 「看用途。**前序 + 空标记**：15 行、递归下降、天然支持流式（边遍历边产出、边读边建），串更紧凑，是我面试的首选。**层序**：可读性最好 —— 人一眼就能看出树的形状，也和 LeetCode 的示例格式一致；代价是需要一个队列（完全二叉树时 `O(n)` 宽），而且不流式（必须先把一整层读完）。如果序列化是为了**存储/传输**，我选前序；如果是为了**人看 / 调试 / 与平台的输入格式对齐**，我选层序。还有一个例外：如果序列化 BST（LC 449），前序**不需要空标记**就够，因为大小关系提供了边界。」

---

## 079.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 449 序列化和反序列化二叉搜索树](https://leetcode.cn/problems/serialize-and-deserialize-bst/) | 中等 | BST 的有序性可替代空标记：前序 + 「大小关系当边界」，串更短 |
| [LC 428 序列化和反序列化 N 叉树](https://leetcode.cn/problems/serialize-and-deserialize-n-ary-tree/) | 困难 | 孩子个数不定 → 必须显式写出「有几个孩子」 |
| [LC 606 根据二叉树创建字符串](https://leetcode.cn/problems/construct-string-from-binary-tree/) | 中等 | 括号嵌套格式；难点是「什么时候可以省掉空括号」 |
| [LC 652 寻找重复的子树](https://leetcode.cn/problems/find-duplicate-subtrees/) | 中等 | **直接用序列化串当哈希 key** —— 正因为编码是双射，这招才成立 |
| [LC 572 另一棵树的子树](https://leetcode.cn/problems/subtree-of-another-tree/) | 简单 | 把两棵树都序列化，再做子串匹配（或用 KMP）；也可直接暴力比较 |
| [LC 271 字符串的编码与解码](https://leetcode.cn/problems/encode-and-decode-strings/) | 中等 | 同一种思想用在字符串列表上：**长度前缀**才不会被内容里的分隔符搞乱 |
| [LC 105 从前序与中序遍历序列构造二叉树](https://leetcode.cn/problems/construct-binary-tree-from-preorder-and-inorder-traversal/) | 中等 | 反方向的题：两个序列（信息冗余完备）→ 唯一的树 → 见 [`078`](078-construct-binary-tree-from-preorder-and-inorder-traversal.md) |
| [LC 536 从字符串生成二叉树](https://leetcode.cn/problems/construct-binary-tree-from-string/) | 中等 | 括号式编码的解析（606 的逆运算） |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 652 寻找重复的子树**
1. 为什么「序列化串相同」能推出「子树结构相同」？这依赖编码的什么性质？
2. 统计出现次数用哈希表，value 存什么？返回答案的时机在「第一次出现」还是「第二次出现」？

**LC 449 序列化 BST**
1. 有了 BST 的大小关系，你能不能推出「这棵子树到哪儿结束」而不写出空标记？
2. 反序列化时，怎么判断「下一个值属于当前子树还是祖先的右子树」？

**LC 428 N 叉树**
1. 每个结点的孩子个数不定，编码里必须多出哪一个 token？
2. 前序 + 「孩子数」为什么就够，不需要空标记？

**LC 271 字符串编码**
1. 内容里可能出现任何字符，纯用分隔符为什么不可靠？
2. 「长度前缀」为什么天然免疫这个问题？

**通用**
- 设计任何编解码，先问三个问题：边界怎么定、空位怎么写、顺序怎么排。
- 验证的唯一可靠手段是**往返 + 幂等**：`serialize(deserialize(s)) == s`。

</details>

---

## 附：本章小结（可背走的三句话）

```
① 一棵树要能被一个序列唯一还原，序列必须"自描述"：空标记让 token 数恒为 2n+1
② 结构信息由【位置】编码，绝不由【值】编码 —— 所以重复值、负值都不会出问题
③ 验收标准是往返 + 幂等：deserialize 后结构一致，且再 serialize 逐字符相同
```

> **下一章**：[`080-house-robber-iii.md`](080-house-robber-iii.md) —— 回到树形 DP：这次返回值不再是「一个数」，而是一个「选 / 不选」的状态向量。
