# 093. LC1631 最小体力消耗路径（Path With Minimum Effort）

> **状态**：`已完成（初稿）` ｜ **所属**：第 9 部分 · 图形结构 ｜ **编号**：093 / 150
> **难度**：中等 ｜ **标签**：图 · 二分答案 · 网格图 · Dijkstra 变形 · 并查集
> **一句话**：这道题真正考的是「**把『路径上最大值最小』这类问题识别成『存在性判定 + 单调性二分』**」，不是「会写网格 BFS」。
> **前置**：[`091-network-delay-time.md`](091-network-delay-time.md)（Dijkstra 的贪心不变量在这里要被换成「取 max」的版本）

---

## 093.1 题目

**原题链接**：<https://leetcode.cn/problems/path-with-minimum-effort/>

### 题意重述（把隐含条件补齐）

给你一个 `rows × cols` 的整数矩阵 `heights`，`heights[r][c]` 表示格子 `(r, c)` 的高度。你从左上角 `(0,0)` 出发，只能**上下左右**移动，要走到右下角 `(rows-1, cols-1)`。

一条路径的**体力消耗**定义为：**这条路径上所有相邻两格高度差的绝对值的最大值**。求所有路径里**体力消耗的最小值**。

它藏了五个必须读出来的约束：

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| **四方向移动**（不含斜角） | 「上下左右移动」 | 加了斜向 `dirs` 就会得到更小的错误答案 |
| **体力 = 最大值，不是求和** | 「所有相邻格子高度差的绝对值的最大值」 | 写成累加就变成「最短路径和」，完全是另一道题 |
| **取绝对值** | 「高度差的绝对值」 | 不取绝对值会让「下坡」抵消「上坡」 |
| **不要求路径最短** | 只问体力 | 绕远路可能更省体力（本章的关键反例） |
| **单格图是合法的** | `1 <= rows, cols` | `rows = cols = 1` 时答案必须是 `0`（不需要移动） |

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| `1 <= rows, cols <= 100` | 最多 `10⁴` 个格子 | `O(RC) = 10⁴`；`O(RC log(RC)) ≈ 1.4·10⁵`；二分 `log(10⁶) ≈ 20` 轮 × `10⁴ = 2·10⁵`。**三种做法全都能过** |
| `1 <= heights[r][c] <= 10⁶` | 答案 ∈ `[0, 10⁶-1]` | 二分只需要 ~20 轮；答案上界可以收紧到 `maxH - minH` |
| 边数 | 约 `2·RC = 2·10⁴` | 稠密程度低，堆版 Dijkstra 占优 |

> 💡 **量级 → 算法**：这题真正的「量级线索」不是 `RC`，而是**答案的值域** `10⁶`。答案是一个可以二分枚举的**数**（不是路径、不是集合），值域只有 `10⁶` 量级，于是「二分答案 + 可行性判定」这一整套（第 3 部分的 025/026 用过）可以直接搬过来。**看到「求最大/最小化某个极端量」，先问：这个量能不能二分？**

### 示例

```
输入：heights = [[1,2,2],[3,8,2],[5,3,5]]
输出：2
解释：路线 (0,0)→(0,1)→(0,2)→(1,2)→(2,2) 的高差是 1,0,0,3 → 体力 3；
      更优的是绕左下：(0,0)→(1,0)→(2,0)→(2,1)→(2,2)，高差 2,2,2,2 → 体力 2。
      （直穿中间的 8 会把体力顶到 5 以上，所以那不是最优。）

输入：heights = [[1,2,3],[3,8,4],[5,3,5]]
输出：1
解释：存在一条只含高差 ≤ 1 的路。

输入：heights = [[1,2,1,1,1],[1,2,1,2,1],[1,2,1,2,1],[1,2,1,2,1],[1,1,1,2,1]]
输出：0
解释：可以全程只走高差为 0 的边。
```

### 我自造的四个反例（写完代码必须拿它们打一遍）

```text
① [[7]]                        单格图 → 0（最容易漏的边界）
② [[5,5],[5,5]]                全等高 → 0（答案可以是 0，不能用 0 当「没算」的哨兵）
③ [[1,2,3],[1,100,1],[7,1,1]]   绕路赢：直穿 1→100 要 99，绕右上只要 2 → 2
④ [[1,2,2]]                    单行：只能一路向右，答案是最大相邻差 = 0？不，1 → 答案 1
```

第 ③ 条是本章的照妖镜：任何「按步数最少走」的写法（BFS 最短路）在这张图上都会给出 `99`。

---

## 093.2 从暴力到最优：第一个念头为什么错

### 第一个念头 A：「BFS 找最短路径」

网格最短路是 082–089 章的肌肉记忆，于是很多人直接 BFS 求出「步数最少的路径」，然后算它的体力。

**错因**：BFS 优化的是**步数**，题目优化的是**最大高差**。这两者之间没有任何单调关系。反例 ③ 里 BFS 会选 `1 → 100 → 1` 这条两步的路（体力 `99`），而最优路是绕一圈的四步路（体力 `2`）。**多走两步，省下 97。**

### 第一个念头 B：「枚举所有路径，取最小值」

路径数是指数级的（`10⁴` 个格子的简单路数量远超宇宙原子数）。**这条思路连「小数据能过」都做不到**，只能把规模缩到 `3×4` 以内当对拍 oracle（本章 §093.5.4 就这么干的）。

### 第一个念头 C：「网格 DP：`dp[r][c] = min(...)`」

网格题里 `dp[r][c]` 递推确实好用（LC 64 最小路径和），但**它成立的前提是「只能向右/向下」**。本题允许上下左右，路径会「绕回去」，`dp` 的拓扑序就不存在了——你无法确定「`(r,c)` 的最优值只依赖左、上」。反例 ③ 恰好需要从上面绕到右下，DP 会漏掉。

### 卡点在哪

一句话：

> **要优化的目标是「一条路径上所有边权的最大值」，它既不满足「可以拆成前后两半相加」（所以不能简单 DP），也不满足「按步数递增」（所以不能 BFS）。**

### 灵感的来源 —— 两条，都来自同一个观察：**「最大值」是可以被「阈值」驯服的**

**灵感一：把优化问题换成判定问题**

不要问「最小体力是多少」，改问一个更容易回答的问题：

> 给定一个上限 `X`，**存不存在**一条从 `(0,0)` 到终点、且所有相邻高差都 `≤ X` 的路径？

这个判定用一次 BFS 就能做完，`O(RC)`。而它有一个关键性质：

```
若 X 可行，则 X+1、X+2、… 全都可行（可用边更多，能走的范围只增不减）
```

**这就是单调性**，于是「最小的可行 `X`」可以用**二分**在 `[0, maxH - minH]` 上找出来。这是第 3 部分（025/026）那套「二分答案」的直接复用。

**灵感二：把「相加」换成「取 max」，Dijkstra 依然成立**

回想 091 里 Dijkstra 的转移是 `dist[v] = min(dist[v], dist[u] + w)`。这里把 `+` 换成 `max`：

```
eff[v] = min over 路径 P of ( max over 边 e in P of w(e) )
       ← 转移：eff[v] = min( eff[v], max(eff[u], w(u,v)) )
```

为什么还能用堆做贪心？因为 `max` 和 `+` 有同一个关键性质：**沿路径往前走，代价不会变小**。`max(eff[u], w) ≥ eff[u]` 恒成立（对任何符号的 `w` 都成立），于是 §091.3.2 的交换论证一个字都不用改。这叫「**瓶颈最短路**（bottleneck shortest path）」，是「半环上的最短路」的一个标准特例。

---

## 093.3 原理拆解

### 093.3.1 数据结构与算法选型

| 方案 | 时间 | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- |
| BFS 最少步数 | `O(RC)` | `O(RC)` | 30 行 | **优化目标错了，答案错**（反例 ③） |
| 网格 DP（只允许右下） | `O(RC)` | `O(RC)` | 20 行 | 允许四方向后拓扑序消失，答案错 |
| DFS 枚举所有简单路 | 指数 | `O(RC)` | 25 行 | 只能当小规模 oracle |
| **二分答案 + BFS 判定** ✅ | **`O(RC · log(maxH))`** | `O(RC)` | 45 行 | 答案值域极大时（如 `10¹⁸`）轮数变多；只能求「值」，不好求路径 |
| **Dijkstra 变形（转移取 max）** ✅ | **`O(RC · log(RC))`** | `O(RC)` | 60 行 | `k` 维约束下要加状态；常数比二分大一点（要维护堆） |
| 并查集按边权从小到大加边，直到两端连通 | `O(RC log(RC))` | `O(RC)` | 55 行 | 要先枚举所有边并排序，代码最长；但思路最直观（= 瓶颈生成树） |
| 最小生成树上取两端路径的最大边 | `O(RC log(RC))` | `O(RC)` | 70 行 | 杀鸡用牛刀，实现成本最高 |

**两个正解的取舍**（这是面试会追问的重点）：

| 维度 | 二分答案 + BFS | Dijkstra 变形 |
| --- | --- | --- |
| 时间复杂度 | `O(RC log C)`，`C = maxH - minH` | `O(RC log(RC))` |
| `RC = 10⁴, C = 10⁶` 时的估算 | `10⁴ × 20 = 2·10⁵` | `10⁴ × 14 ≈ 1.4·10⁵` |
| 空间 | `O(RC)`（visited + 队列） | `O(RC)`（`best` + 堆，堆可能 `O(RC)`） |
| 代码复杂度 | 低：一个 `check` + 一个标准二分 | 中：要维护堆和「过期条目」 |
| 能否处理「答案不在输入里」 | 能（值域可任意大） | 不能（只能用现成边权，靠 Lazy 也要先枚举边） |
| 能否顺便输出路径 | 不方便（还要再跑一次） | 方便（记 `parent`） |
| 什么时候被难住 | 判定函数本身很贵（例如要跑流网络） | 代价不满足「可结合 + 单调」时 |

> **一句话取舍**：**答案值域小、判定便宜 → 二分；边权就是答案、还想顺便要路径 → Dijkstra。** 本题两者都过，就把两个都写出来，这是面试里最能加分的「我知道两条路并且知道怎么选」。

### 093.3.2 为什么这个做法是对的（正确性论证）

#### 命题一：判定函数有单调性，所以二分能找到「最小可行上限」

定义 `feasible(X)` = 「存在一条从起点到终点、每条边高差都 `≤ X` 的路径」。

**引理（单调）**：若 `feasible(X)` 成立，则对任意 `Y ≥ X`，`feasible(Y)` 也成立。
**证明**：`X` 可行意味着存在路径 `P`，它的每条边高差 `≤ X ≤ Y`，于是同一条 `P` 在 `Y` 下也是合法的。∎

**命题**：设 `E*` = 最小体力。则 `feasible(X) ⇔ X ≥ E*`。
**证明**：`X ≥ E*` 时，取实现 `E*` 的那条最优路径 `P*`，它的所有边高差 `≤ E* ≤ X`，故 `feasible(X)` 成立。反之若 `feasible(X)` 成立，则存在一条路径的所有边高差 `≤ X`，它的体力 `≤ X`，于是 `E* ≤ X`（`E*` 是最小值）。∎

于是 `E*` 恰好是「最小的可行 `X`」，二分在单调布尔序列上找分界点，必然收敛到它。

#### 命题二：`max` 型松弛下，Dijkstra 的贪心不变量仍然成立

**不变量**：从堆中弹出 `(e, u)` 且 `e == eff[u]` 时，`eff[u]` 就是起点到 `u` 的最小瓶颈值（最终值）。

**证明**（与 §091.3.2 同一骨架）：反设第一次出错，存在更优路径 `P` 满足 `瓶颈(P) < eff[u]`。沿 `P` 从起点走到第一个「尚未确定」的点 `x`（存在，因为 `u` 就未确定），设它的前驱 `p` 已确定。则

```
eff[x] ≤ max( eff[p], w(p,x) ) ≤ 瓶颈( P 的起点到 x 这一段 ) ≤ 瓶颈(P) < eff[u]
```

第二步用到 `eff[p]` 已是起点到 `p` 的最优瓶颈值（归纳假设），且 `max` 单调。于是堆里存在一个小于 `eff[u]` 的未确定值，与「`u` 最小」矛盾。∎

> **关键的一步**是 `max(eff[p], w) ≤ 瓶颈(前缀)`：这依赖「路径的瓶颈值 = 各边权与前面瓶颈值的 max」，也就是 `max` 的**结合律与幂等性**。若把运算换成别的（比如「平均」），这条就不成立，贪心也随之失效。

#### 命题三：一条很实用的等价关系

**最小瓶颈路径 ⇔ 最小生成树上两端之间那条路径的最大边 ⇔ 并查集按边权升序加边、两端第一次连通时那条边的权**。

**证明要点**：Kruskal 加入某条边 `(u,v,w)` 时，`u` 与 `v` 第一次连通；此时在最小生成树里 `u → v` 的路径所有边权都 `≤ w`（因为它们是更早加入的），所以瓶颈 `≤ w`；而任何瓶颈 `< w` 的路径会说明 `u`、`v` 在只用权 `< w` 的边时就已连通，与「第一次连通发生在 `w`」矛盾。∎

这条关系解释了为什么「并查集加边法」（§093.3.1 表里的第三行）也能解本题——它是三种解法共同的数学后台。

### 093.3.3 通用解题思路（可迁移的那一层）——兼全书图算法收口

本章是图形结构收口六章（090–095）的第三章。**全局的选型决策表在 [`091-network-delay-time.md`](091-network-delay-time.md) 的 §091.3.3**，本章对应其中两行：

| 你观察到的条件 | 该用的算法 | 复杂度 | 本章位置 |
| --- | --- | --- | --- |
| 答案是「路径上**最大值最小** / 瓶颈」，且值域可枚举 | **二分答案 + BFS 判定** | `O(RC · log C)` | §093.5.1 |
| 同上，但代价就是边权本身、还想顺便求路径 | **Dijkstra 变形（转移取 max）** | `O(RC log(RC))` | §093.5.2 |

把这个模型抽象出来，它是「**minimax 型优化**」这个母题的实例：

```
目标：min over 路径 P of ( max over 边 e ∈ P of w(e) )
      ↓ 换成判定问题
存在一个 X，使「只用 w ≤ X 的边，起点能到终点」           ← 一次 BFS
      ↓ 观察到 X 越大越容易成立（单调性）
二分 X，找最小可行值
```

**「二分答案」的三件套**（这三条缺一条就不能二分）：

| 要素 | 本题的答案 | 缺失时会怎样 |
| --- | --- | --- |
| **值域可枚举**（有明确上下界） | `[0, maxH - minH]` | 无界就没法定位中点 |
| **可行性单调** | 上限越大越容易连通 | 非单调时二分收敛到错误的点 |
| **判定函数便宜** | 一次 `O(RC)` 的 BFS | 判定本身是指数级时，二分毫无意义 |

同属这个模型的题：

| 题 | 「最大值最小」的具体形态 |
| --- | --- |
| [LC 778 水位上升的泳池中游泳](https://leetcode.cn/problems/swim-in-rising-water/) | 路径上最大高度最小（与本章几乎同构） |
| [LC 1102 得分最高的路径](https://leetcode.cn/problems/path-with-maximum-minimum-value/) | 路径上最小值最大（把 `max` 换成 `min`，对称） |
| [LC 2492 两个城市间路径的最小分数](https://leetcode.cn/problems/minimum-score-of-a-path-between-two-cities/) | 不要求简单路，答案就是两端所在连通块里的最小边权（并查集味道） |
| [LC 2577 在网格图中访问一个格子的最少时间](https://leetcode.cn/problems/minimum-time-to-visit-a-cell-in-a-grid/) | Dijkstra 的「等时间 + 步行一格」变形 |
| [LC 1976 到达目的地的方案数](https://leetcode.cn/problems/number-of-ways-to-arrive-at-destination/) | 变体：最短路 + 计数（见 092） |

### 093.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| 允许**八个方向**移动 | `dirs` 变成 8 个 | 算法完全不变，只改 `dirs`；注意答案可能变小 |
| 代价改成「路径上**高的个数**最小」 | 边权变成 0/1 | 变成 0-1 BFS（用双端队列），仍是 Dijkstra 的特例 |
| 要输出**路径本身** | 需要回溯 | 走 Dijkstra 版并记 `parent`；二分版要再跑一次带 `limit` 的 BFS 复原一条路径 |
| 网格变成**三维 / 任意图** | 输入形态变了 | 算法不变，`O(V log V)`；只要「代价 = 路径最大边」就仍是瓶颈最短路 |
| 边权允许**负数** | 直觉上觉得 Dijkstra 会崩 | **本章不会崩**：`max` 型转移对任何符号都单调（`max(a,w) ≥ a`），所以负权照样正确。这与 091 的「相加」型 Dijkstra 形成鲜明对比——**能否用贪心，取决于运算的单调性，而不是权重的符号** |
| 再加一条「最多 `k` 次穿越高墙」 | 多一个预算维度 | 状态变 `(r, c, 已穿越次数)`，Dijkstra 分层（同 092 的手法） |

---

## 093.4 图解推演

### 093.4.1 绕路为什么赢（反例 ③ 的展开）

```
heights = [[  1,   2,   3],
           [  1, 100,   1],
           [  7,   1,   1]]        起点 (0,0)=1，终点 (2,2)=1

路线 A（步数最少，2 步）：1 → 100 → 1
   高差：|100-1| = 99, |1-100| = 99      体力 = 99   ❌

路线 B（绕右上，4 步）：(0,0)1 →(0,1)2 →(0,2)3 →(1,2)1 →(2,2)1
   高差：1, 1, 2, 0                        体力 = 2    ✅

     ┌───┬───┬───┐
     │ 1 → 2 → 3 │       ← 路线 B：沿上边走到右列，再往下
     │ ↑       ↓ │
     │ 1  [100] 1│       ← 中间的 100 是「墙」，只有路线 A 才穿它
     │         ↓ │
     │ 7   1   1 │
     └───┴───┴───┘
```

**这张图要说清的事**：BFS 的「第一层/第二层」在这种题里毫无意义，因为**代价是边的属性，不是步数**。

### 093.4.2 二分答案的收敛过程

`heights = [[1,2,2],[3,8,2],[5,3,5]]`，`maxH = 8`、`minH = 1`，所以 `lo = 0`、`hi = 7`：

| 轮 | `lo` | `hi` | `mid` | `feasible(mid)`？ | 关键路径 | 动作 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 0 | 7 | 3 | 是（存在高差全 `≤ 3` 的路） | `1→2→2→2→5`，高差 `1,0,0,3` | `hi = 3` |
| 2 | 0 | 3 | 1 | 否（`≤1` 的边走不到终点） | 只能到 `(1,2)`，再往 `(2,2)` 要跨 3 | `lo = 2` |
| 3 | 2 | 3 | 2 | 是 | `1→3→5→3→5`，高差 `2,2,2,2` | `hi = 2` |
| 4 | 2 | 2 | — | 收敛 | — | 返回 `2` |

注意第 2 轮：`mid = 1` 时 BFS 会沿 `1 → 2(0,1) → 2(0,2) → 2(1,2)` 一路走到右列，然后被 `|5-2|=3 > 1` 挡住，无法到达终点。**这就是「判定成功/失败」的全部工作**：不需要知道最好路径长什么样，只需要知道「能不能到」。

### 093.4.3 Dijkstra 变形的弹出顺序（含「更优解后来居上」）

同一张图，`eff` 初始 `[0, INF, ...]`：

```
┌─────┬─────┬─────┐
│  1  │  2  │  2  │
├─────┼─────┼─────┤
│  3  │  8  │  2  │
├─────┼─────┼─────┤
│  5  │  3  │  5  │
└─────┴─────┴─────┘
```

| 步 | 弹出 `(eff, 格)` | 松弛结果 |
| --- | --- | --- |
| 1 | `(0, (0,0))` | `(1,0)` 高差 2 → `eff=2`；`(0,1)` 高差 1 → `eff=1` |
| 2 | `(1, (0,1))` | `(1,1)` 高差 6 → `eff=6`；`(0,2)` 高差 0 → `eff=max(1,0)=1` |
| 3 | `(1, (0,2))` | `(1,2)` 高差 0 → `eff=1` |
| 4 | `(1, (1,2))` | `(2,2)` 高差 3 → `eff=max(1,3)=3` ← 先塞进一个「3」 |
| 5 | `(2, (1,0))` | `(2,0)` 高差 2 → `eff=2`；`(1,1)` 高差 5 → `5 > 6` 不更新 |
| 6 | `(2, (2,0))` | `(2,1)` 高差 2 → `eff=2` |
| 7 | `(2, (2,1))` | `(2,2)` 高差 2 → `2 < 3` → **改写成 2** ✅ |
| 8 | `(2, (2,2))` | 是终点 → 返回 **`2`** |

**要读出两件事**：

1. 第 4 步塞进去的 `(2,2)` 副本值是 `3`，第 7 步被更优的 `2` 覆盖——**堆里会留下旧副本，靠 `eff > best[r][c]` 判过期**。本例里旧副本（`3`）排在新副本（`2`）后面，所以它弹出时会被直接丢掉，不影响结果；
2. **瓶颈值是单调不降的**（`0 → 1 → 2`），所以第一次弹出终点时，它的值就是最优值。这一点与 091 完全一致。

---

## 093.5 双语言实现

### 093.5.1 Java

```java
import java.util.*;

class Solution {
    private static final int[][] DIRS = {{1, 0}, {-1, 0}, {0, 1}, {0, -1}};

    /** 解法一：二分答案 + BFS 可达性判定 */
    public int minimumEffortPath(int[][] heights) {
        int minH = heights[0][0], maxH = heights[0][0];
        for (int[] row : heights) {
            for (int v : row) {
                minH = Math.min(minH, v);
                maxH = Math.max(maxH, v);
            }
        }
        int lo = 0, hi = maxH - minH;   // 答案闭区间；单格图 / 全等高时 lo == hi == 0
        while (lo < hi) {
            int mid = lo + (hi - lo) / 2;  // 不用 (lo+hi)/2，防溢出（本题不溢出，但习惯要养成）
            if (reachable(heights, mid)) {
                hi = mid;                  // mid 可行 → 答案不会比它大
            } else {
                lo = mid + 1;              // mid 不可行 → 答案必须更大
            }
        }
        return lo;
    }

    private boolean reachable(int[][] heights, int limit) {
        int rows = heights.length, cols = heights[0].length;
        if (rows == 1 && cols == 1) {
            return true;                   // 单格图：原地即到达
        }
        boolean[][] seen = new boolean[rows][cols];
        seen[0][0] = true;
        Deque<int[]> q = new ArrayDeque<>();
        q.offer(new int[]{0, 0});
        while (!q.isEmpty()) {
            int[] cur = q.poll();
            for (int[] d : DIRS) {
                int nr = cur[0] + d[0], nc = cur[1] + d[1];
                if (nr < 0 || nr >= rows || nc < 0 || nc >= cols || seen[nr][nc]) {
                    continue;
                }
                if (Math.abs(heights[nr][nc] - heights[cur[0]][cur[1]]) > limit) {
                    continue;              // 这一步超过体力上限 → 这条边在判定中不存在
                }
                if (nr == rows - 1 && nc == cols - 1) {
                    return true;           // 提前收工，省掉一整个 BFS
                }
                seen[nr][nc] = true;
                q.offer(new int[]{nr, nc});
            }
        }
        return false;
    }

    /** 解法二：Dijkstra 变形——转移用 max 而不是加法 */
    public int minimumEffortPathDijkstra(int[][] heights) {
        int rows = heights.length, cols = heights[0].length;
        int[][] best = new int[rows][cols];
        for (int[] row : best) {
            Arrays.fill(row, Integer.MAX_VALUE);
        }
        best[0][0] = 0;

        PriorityQueue<int[]> pq = new PriorityQueue<>((a, b) -> a[0] - b[0]); // {effort, r, c}
        pq.offer(new int[]{0, 0, 0});

        while (!pq.isEmpty()) {
            int[] cur = pq.poll();
            int eff = cur[0], r = cur[1], c = cur[2];
            if (eff > best[r][c]) {
                continue;                  // 过期条目（堆里可能留着更差的副本）
            }
            if (r == rows - 1 && c == cols - 1) {
                return eff;                // 第一次弹出终点即最优
            }
            for (int[] d : DIRS) {
                int nr = r + d[0], nc = c + d[1];
                if (nr < 0 || nr >= rows || nc < 0 || nc >= cols) {
                    continue;
                }
                int step = Math.abs(heights[nr][nc] - heights[r][c]);
                int next = Math.max(eff, step);   // ★ 整条路径的代价 = 最陡那一步
                if (next < best[nr][nc]) {
                    best[nr][nc] = next;
                    pq.offer(new int[]{next, nr, nc});
                }
            }
        }
        return best[rows - 1][cols - 1];
    }
}
```

### 093.5.2 Go

```go
package main

import "container/heap"

// ---------- 解法一：二分答案 + BFS 可达性 ----------
func minimumEffortPathBinarySearch(heights [][]int) int {
	// 答案的取值范围：0 ~ (全局最大高差)。单点图时上下界都是 0
	minH, maxH := heights[0][0], heights[0][0]
	for _, row := range heights {
		for _, v := range row {
			if v < minH {
				minH = v
			}
			if v > maxH {
				maxH = v
			}
		}
	}

	lo, hi := 0, maxH-minH // 单调性：上限越大越容易连通
	for lo < hi {
		mid := lo + (hi-lo)/2
		if canReachWithLimit(heights, mid) {
			hi = mid // mid 可行 → 答案不会比它大
		} else {
			lo = mid + 1
		}
	}
	return lo
}

// 只用「相邻格高差 ≤ limit」的边，判断左上角能否走到右下角（BFS）
func canReachWithLimit(heights [][]int, limit int) bool {
	rows, cols := len(heights), len(heights[0])
	if rows == 1 && cols == 1 {
		return true
	}
	seen := make([][]bool, rows)
	for i := range seen {
		seen[i] = make([]bool, cols)
	}
	seen[0][0] = true
	queue := []int{0} // 把 (r,c) 压成 r*cols+c，省掉一次结构体分配
	dirs := [4][2]int{{1, 0}, {-1, 0}, {0, 1}, {0, -1}}

	for len(queue) > 0 {
		id := queue[0]
		queue = queue[1:] // 队列头出队；切片不共享元素，安全
		r, c := id/cols, id%cols
		for _, d := range dirs {
			nr, nc := r+d[0], c+d[1]
			if nr < 0 || nr >= rows || nc < 0 || nc >= cols || seen[nr][nc] {
				continue
			}
			if absInt(heights[nr][nc]-heights[r][c]) > limit {
				continue // 这条边超过体力上限，判定中不存在
			}
			if nr == rows-1 && nc == cols-1 {
				return true
			}
			seen[nr][nc] = true
			queue = append(queue, nr*cols+nc)
		}
	}
	return false
}

func absInt(x int) int {
	if x < 0 {
		return -x
	}
	return x
}

// ---------- 解法二：Dijkstra 变形（边权取「路径最大值」） ----------
type cell struct {
	r, c, eff int // eff = 走到这一格为止，整条路径上的最大高差
}

type cellHeap []cell

func (h cellHeap) Len() int           { return len(h) }
func (h cellHeap) Less(i, j int) bool { return h[i].eff < h[j].eff } // 小顶堆：瓶颈小的先确定
func (h cellHeap) Swap(i, j int)      { h[i], h[j] = h[j], h[i] }
func (h *cellHeap) Push(x interface{}) {
	*h = append(*h, x.(cell))
}
func (h *cellHeap) Pop() interface{} {
	old := *h
	n := len(old)
	it := old[n-1]
	*h = old[:n-1]
	return it
}

func minimumEffortPath(heights [][]int) int {
	rows, cols := len(heights), len(heights[0])
	const INF = 1 << 60
	best := make([][]int, rows)
	for i := range best {
		best[i] = make([]int, cols)
		for j := range best[i] {
			best[i][j] = INF
		}
	}
	best[0][0] = 0

	pq := &cellHeap{{r: 0, c: 0, eff: 0}}
	heap.Init(pq)
	dirs := [4][2]int{{1, 0}, {-1, 0}, {0, 1}, {0, -1}}

	for pq.Len() > 0 {
		cur := heap.Pop(pq).(cell)
		if cur.eff > best[cur.r][cur.c] {
			continue // 过期条目
		}
		if cur.r == rows-1 && cur.c == cols-1 {
			return cur.eff // 小顶堆首次弹出终点 = 最小体力消耗
		}
		for _, d := range dirs {
			nr, nc := cur.r+d[0], cur.c+d[1]
			if nr < 0 || nr >= rows || nc < 0 || nc >= cols {
				continue
			}
			step := absInt(heights[nr][nc] - heights[cur.r][cur.c])
			// 转移不是「相加」而是「取 max」：整条路径的代价由最陡的一步决定
			next := cur.eff
			if step > next {
				next = step
			}
			if next < best[nr][nc] {
				best[nr][nc] = next
				heap.Push(pq, cell{r: nr, c: nc, eff: next})
			}
		}
	}
	return best[rows-1][cols-1]
}
```

### 093.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| BFS 队列 | `ArrayDeque<int[]>`，`poll()` 取头 | `[]int` + `queue[1:]` 取头 | Go 的 `queue = queue[1:]` 只是移动切片头，底层数组不会立刻释放；在「反复入队」的场景下内存峰值是 `O(入队总数)` 而不是 `O(队内元素)`，本题可忽略 |
| 坐标打包 | 用 `int[]` 两元素数组 | 用 `id = r*cols + c` 压成一个 `int` | 后者省内存、省 GC；但 `id/cols` 与 `id%cols` 写反（`id/rows`）是经典 bug |
| 堆 | `PriorityQueue<int[]>` + `Comparator` | `container/heap` + 手写 `Len/Less/Swap/Push/Pop` | Go 要写 5 个方法；`Less` 比较的是 `h[i].eff` |
| 二维数组 | `new int[rows][cols]` + `Arrays.fill(row, MAX)` | `make([][]int, rows)` 后逐行 `make` + 内层循环填 | Go 不逐行 `make` 就是 `nil`，一写就 panic |
| 二维布尔 | `new boolean[rows][cols]`（默认 `false`） | `make([][]bool, rows)` 逐行 `make`（默认 `false`） | 两边语义一致，但 Go 忘 `make` 内层是 `nil`，`seen[r][c]` 直接 panic |
| 二分中点 | `lo + (hi - lo) / 2` | 同 | 两边都这么写，避免「闭区间 + `(lo+hi)/2`」在 `hi` 很大时溢出 |
| `INF` 与 `max` 的交互 | `MAX_VALUE` 参与 `Math.max` 会污染结果 | `1<<60` 参与比较也有同样的风险 | 都不能让 `INF` 进入 `max` 运算；本题只在「已知可达的状态」上做转移，所以安全 |

### 093.5.4 对拍测试：DFS 枚举全部简单路 + 两种正解互拍

```go
package main

import (
	"math/rand"
	"testing"
)

// 参考实现：DFS 枚举所有简单路径，取「路径上最大高差」的最小值
func bruteDFS(heights [][]int) int {
	rows, cols := len(heights), len(heights[0])
	const INF = 1 << 30
	best := INF
	seen := make([][]bool, rows)
	for i := range seen {
		seen[i] = make([]bool, cols)
	}
	dirs := [4][2]int{{1, 0}, {-1, 0}, {0, 1}, {0, -1}}
	var dfs func(r, c, cur int)
	dfs = func(r, c, cur int) {
		if r == rows-1 && c == cols-1 {
			if cur < best {
				best = cur
			}
			return
		}
		for _, d := range dirs {
			nr, nc := r+d[0], c+d[1]
			if nr < 0 || nr >= rows || nc < 0 || nc >= cols || seen[nr][nc] {
				continue
			}
			step := absInt(heights[nr][nc] - heights[r][c])
			next := cur
			if step > next {
				next = step
			}
			if next >= best { // 剪枝：已经不比当前最好更优
				continue
			}
			seen[nr][nc] = true
			dfs(nr, nc, next)
			seen[nr][nc] = false
		}
	}
	seen[0][0] = true
	dfs(0, 0, 0)
	return best
}

func TestPathWithMinimumEffort(t *testing.T) {
	r := rand.New(rand.NewSource(20241631))
	for iter := 0; iter < 6000; iter++ {
		rows := 1 + r.Intn(3) // 1..3
		cols := 1 + r.Intn(4) // 1..4，最多 12 格，DFS 全枚举可控
		heights := make([][]int, rows)
		for i := range heights {
			heights[i] = make([]int, cols)
			for j := range heights[i] {
				heights[i][j] = 1 + r.Intn(12) // 值域小 → 大量等高与重复
			}
		}
		want := bruteDFS(heights)
		if got := minimumEffortPathBinarySearch(heights); got != want {
			t.Fatalf("二分+BFS 错：iter=%d h=%v got=%d want=%d", iter, heights, got, want)
		}
		if got := minimumEffortPath(heights); got != want {
			t.Fatalf("Dijkstra 变形错：iter=%d h=%v got=%d want=%d", iter, heights, got, want)
		}
	}
}
```

> ⚠️ **参考实现里的剪枝要小心**：`if next >= best { continue }` 用到了「路径变长不会让瓶颈变小」这条单调性——它对 `max` 型代价恒成立（所以安全），但如果哪天把代价换成「求和」，这个剪枝反而会错误地剪掉更优解。**剪枝的正确性也依赖运算的单调性，这件事必须自己想清楚，不能照抄。**
> 另外网格规模只能开到 `3×4`：简单路数量呈指数增长，`4×4` 就已经接近对拍能忍受的上限。

---

## 093.6 复杂度与常见坑

### 093.6.1 复杂度推导

- **二分答案 + BFS**：
  - 轮数 `O(log(maxH - minH))`：值域 `10⁶` → 约 20 轮。
  - 每轮 BFS 是 `O(RC)`（每个格子最多入队一次，4 条边）。
  - 合计 `O(RC · log(maxH))`。本题 `10⁴ × 20 = 2·10⁵`，微秒到毫秒级。
  - 空间 `O(RC)`：`seen` 数组 + 队列。
- **Dijkstra 变形**：
  - 每个「格子 + 当前瓶颈值」最多入堆常数次（实际是「每次成功改进一次」），总堆操作 `O(RC)` 次，每次 `O(log(RC))` → `O(RC log(RC)) ≈ 1.4·10⁵`。
  - 空间 `O(RC)`：`best` 二维数组 + 堆（最坏 `O(RC)` 个条目）。
- **并查集加边法**：排序 `O(RC log(RC))` + 并查集 `O(RC·α)`，量级与 Dijkstra 相同。

### 093.6.2 七个真实踩过的坑

1. **二分的下界写成 `1`**：单格图与「全等高」的答案是 `0`。`lo` 必须从 `0` 开始，否则这两类输入直接错。
2. **忘了 `rows == 1 && cols == 1` 的短路**：BFS 从起点出发，永远走不到「终点 ≠ 起点」的分支，于是返回 `false`，二分最后给出 `maxH - minH` 而不是 `0`。
3. **判定里写成 `>= limit` 而不是 `> limit`**：高差**等于**上限是允许的（体力就是最大值），写成 `>=` 会把合法边判成非法，答案偏大。
4. **Dijkstra 版用加法**：`next = eff + step` 就变成了「最短路径和」，与题意完全不同。**转移方式的错误比边界错误更难发现，因为它照样能通过一部分样例。**
5. **`INF` 参与 `max`**：如果代码在没有「可达」判断的情况下对 `INF` 做 `Math.max`，结果是 `INF`，看起来没事；但若先做 `INF - step` 之类的运算就会得到负数，污染答案。**统一只在已确定可达的状态上转移。**
6. **Go 里 `queue = queue[1:]` 后用 `cap` 恢复**：本题没这么写，但要记住 `append` 到「已 slice 过头」的切片会复用底层数组，可能覆盖未处理元素。
7. **把坐标打包成 `id` 时用错除数**：`id/cols` 与 `id%cols` 必须配 `cols`；写成 `id/rows` 在方阵上恰好不出错，一换成矩形网格立刻崩——**用非方阵 `1×4`、`3×4` 做随机测试就能抓住它**（本章的对拍正是这么设定的）。

### 093.6.3 面试官最爱追问的三个问题

**Q1：这道题为什么可以二分？**
> 「因为判定问题有单调性：一旦存在一条所有边高差都 `≤ X` 的路径，那么把上限放大到 `Y > X` 时，同一条路径依然合法，所以可行性只会从『不可行』变成『可行』一次。单调布尔序列上二分，收敛点就是最小体力。三步都要说清：值域有界（`0 ~ maxH-minH`）、单调（上限越大越容易连通）、判定便宜（一次 `O(RC)` 的 BFS）。」

**Q2：Dijkstra 的转移从『相加』换成『取 max』，为什么还能用？**
> 「Dijkstra 的正确性只依赖一件事：**沿路径往前延伸时，代价不会变小**。`max(a, w) ≥ a` 恒成立，所以『当前最小的未确定点的值已是最终值』这条论证原样成立。这类问题叫瓶颈最短路，本质是半环上的最短路——`+` 换成 `max` 后，结合律、单调性、单位元都还在。**反过来，如果运算不单调（比如取平均），贪心立刻失效。**」

**Q3：还能怎么做？**
> 「第三条路是并查集：把所有相邻格子的边按高差排序，从小到大一条条加，当起点与终点第一次连通时，那条边的权就是答案——因为最小瓶颈路径等于最小生成树上两端路径的最大边。时间复杂度 `O(RC log(RC))`，代码比 Dijkstra 长但思路最直观，而且它顺便解释了为什么三种解法答案一致。若还要输出路径，就在最小生成树上从起点 DFS 到终点。」

---

## 093.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 778 水位上升的泳池中游泳](https://leetcode.cn/problems/swim-in-rising-water/) | 困难 | 与本章同构：路径最大高度最小；另有「二分 + BFS」「Dijkstra」「并查集」三解 |
| [LC 1102 得分最高的路径](https://leetcode.cn/problems/path-with-maximum-minimum-value/) | 中等 | `max` 换 `min`：最大化路径上的最小值（会员题） |
| [LC 2492 两个城市间路径的最小分数](https://leetcode.cn/problems/minimum-score-of-a-path-between-two-cities/) | 中等 | 允许重复走 → 答案就是两端连通块里的最小边权，并查集视角 |
| [LC 2577 在网格图中访问一个格子的最少时间](https://leetcode.cn/problems/minimum-time-to-visit-a-cell-in-a-grid/) | 困难 | Dijkstra 但带「等待到奇数时刻」的规则 |
| [LC 1514 概率最大的路径](https://leetcode.cn/problems/path-with-maximum-probability/) | 中等 | 转移是乘法 + 取 max：又一个「半环上的最短路」 |
| [LC 1976 到达目的地的方案数](https://leetcode.cn/problems/number-of-ways-to-arrive-at-destination/) | 中等 | 最短路 + 计数（见 091/092） |
| [LC 743 网络延迟时间](https://leetcode.cn/problems/network-delay-time/) | 中等 | 标准 `+` 型 Dijkstra，用来对照本章的 `max` 型 |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 778 水位上升的泳池中游泳**
1. 把「时间 t 时哪些格子被淹没」想成一个不断变大的子图，问题就变成了哪种「最值」被最小化？
2. 它与本章的差别只在「边权取两端的较大者」还是「取差值」？

**LC 1102 得分最高的路径**
1. 目标是「路径上最小值最大」，它的可行性单调方向与本章相反，二分该怎么改？
2. 若把边权取相反数，会不会退化成一道「最小值最小」的题？

**LC 1514 概率最大的路径**
1. 概率相乘、取最大，与「长度相加、取最小」在代数结构上有什么共同点？
2. 取对数之后会变成什么样，为什么要用 `log` 而不是直接比较？

**通用**
- 见到「最大化最小值 / 最小化最大值」，第一反应应该是「二分 + 判定」，第二反应是「把 `min`/`max` 直接塞进 Dijkstra 的转移」。
- 判断能不能用贪心确定一个点的最终值，只看一件事：**路径变长时，代价是否可能变小**。

</details>

> **下一章**：[`094-min-cost-to-connect-all-points.md`](094-min-cost-to-connect-all-points.md) —— 从「两点之间」扩展到「把所有点连起来」，进入最小生成树。
