# 094. LC1584 连接所有点的最小费用（Min Cost to Connect All Points）

> **状态**：`已完成（初稿）` ｜ **所属**：第 9 部分 · 图形结构 ｜ **编号**：094 / 150
> **难度**：中等 ｜ **标签**：图 · 最小生成树 · Kruskal · Prim · 并查集
> **一句话**：这道题真正考的是「**为什么『每次加最便宜且不成环的边』一定得到全局最优**」，不是「背 Kruskal 的两段代码」。
> **前置**：[`090-redundant-connection.md`](090-redundant-connection.md)（Kruskal 的另一半是并查集，那一章把并查集讲到了能背）

---

## 094.1 题目

**原题链接**：<https://leetcode.cn/problems/min-cost-to-connect-all-points/>

### 题意重述（把隐含条件补齐）

给你一个二维平面上的 `n` 个点 `points[i] = (x_i, y_i)`。连接点 `i` 与点 `j` 的**费用**是两个点的**曼哈顿距离** `|x_i - x_j| + |y_i - y_j|`。请把所有点连成一个整体（任意两点之间都能沿着已连的边互相到达），求**最小总费用**。

它藏了四个必须读出来的约束：

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| **任意两点之间都可以直接连边** | 「费用为曼哈顿距离」 | 这是一张**完全图**（`n(n-1)/2` 条边），不是稀疏图——这决定了后面选 Prim 还是 Kruskal |
| **费用是曼哈顿距离，不是欧氏距离** | 「曼哈顿距离」 | 写成 `sqrt` 会引入浮点误差，且答案类型是整数 |
| **只要连通，不需要额外性质** | 「把所有点连起来」 | 任何连通子图都行，但最小代价的连通子图一定是**树**（多一条边就是浪费） |
| **点可以重合** | 坐标没有「互不相同」的约束 | 重合点之间距离为 `0`，`0` 是合法边权，不能拿 `0` 当「没连」的哨兵 |

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| `1 <= n <= 1000` | 最多 1 千个点 | 完全图边数 `E = n(n-1)/2 ≈ 5·10⁵`；`O(n²) = 10⁶`、`O(E log E) ≈ 10⁷` 都能过 |
| `-10⁶ <= x_i, y_i <= 10⁶` | 单边最长 `4·10⁶` | 单条边能用 `int`；但**总费用最坏 `(n-1) × 4·10⁶ ≈ 4·10⁹` 会溢出 `int`**，内部累加要用 `long` / `int64` |
| 图是完全图 | 不是输入给的边 | 不需要「建图」步骤，边权现算；也意味着「按边排序」要自己生成 `5·10⁵` 条边 |

> 💡 **量级 → 算法**：`E ≈ V²` 是**稠密图**的典型形态。这里有一个非常实用的判据：

| 图形态 | 判据 | 选谁 | 理由 |
| --- | --- | --- | --- |
| **稠密** | `E` 接近 `V²` | **Prim（`O(V²)` 数组版）** | 每轮线性找最近点，共 `V` 轮 → `V²`，与 `E` 无关；不需要存 `E` 条边 |
| **稀疏** | `E ≈ c·V`（`c` 小） | **Kruskal（`O(E log E)`）** | 排序 `E` 条边很便宜，还省掉 `V²` 的扫描 |
| 稀疏且动态加点 | 边是流式来的 | **Prim（堆版）** | 只需维护「候选边堆」，不必预先枚举全部边 |

本题 `E = V²/2`，**Prim 数组版是理论最优**；但 Kruskal 也过（`10⁷` 次操作），所以面试里两个都写才是加分项。

### 示例

```
输入：points = [[0,0],[2,2],[3,10],[5,2],[7,0]]
输出：20
解释：MST 的四条边：
      (1,3) 距离 |2-5|+|2-2| = 3
      (0,1) 距离 2+2 = 4
      (3,4) 距离 2+2 = 4
      (1,2) 距离 1+8 = 9
      合计 3+4+4+9 = 20

输入：points = [[3,12],[-2,5],[-4,1]]
输出：18
解释：(0,1) = 5+7 = 12；(1,2) = 2+4 = 6；合计 18。
```

### 我自造的五个边界（写完代码必须拿它们打一遍）

```text
① [[0,0]]                         单点图 → 0（一条边都不需要）
② [[3,3],[3,3],[3,3]]             全重合 → 0（三条 0 权边即可连通）
③ [[0,0],[1,0],[3,0],[6,0]]       共线 → 1+2+3 = 6
④ [[-1000000,-1000000],[1000000,1000000]]  → 4·10⁶（单条边就顶到坐标上限）
⑤ 正方形四点 [[0,0],[1,0],[1,1],[0,1]]     → 3（提示：4 条边长都是 1，但只能选 3 条）
```

第 ⑤ 条同时是「最近邻贪心」的反例，见 §094.2。

---

## 094.2 从暴力到最优：第一个念头为什么错

### 第一个念头 A：「每个点连到离它最近的点」

这个想法很自然：既然要总费用最小，那每个点都应该用最便宜的边接出去。**它是错的**，用反例 ⑤ 打一遍就清楚了：

```
points = (0,0) A, (1,0) B, (1,1) C, (0,1) D     单位正方形

每个点的最近距离都是 1（有两个候选），若按「下标小的优先」选：
   A ── B, B ── A, C ── D, D ── C
   得到的边集 = {A-B, C-D}              费用 2，但图是**两个连通块**，没连通！

若允许重复计数、强行每个点一条边：
   {A-B, B-C, C-D, D-A}                 费用 4，且**含环**（A-B-C-D-A）

正确答案：任意去掉一条边的生成树，费用 3。
```

两个失败模式都出现了：**① 有的点被漏连（连通性没保证）；② 有的边被重复选（成环浪费）**。这说明「局部最优」和「全局最优」在这里不重合——不是「贪心不行」，而是**贪心的对象选错了**（应该对「边」贪心，而不是对「点」贪心）。

### 第一个念头 B：「枚举所有生成树，取最小」

`n` 个点的生成树数量由 Cayley 公式给出：`n^(n-2)`。`n = 10` 时是 `10⁸`，`n = 1000` 时是天文数字。**这条路的复杂度是输入规模的超指数函数，连小数据都要小心。**

### 第一个念头 C：「按边权从小到大加，不成环就加」

这就是 Kruskal，**它是正确的**——但「为什么正确」并不是直觉自明的。它犯了两个看起来危险的错误：

1. 只看**局部**（当前最便宜的边），却敢宣称全局最优；
2. 选的边甚至可能「暂时」让某些点看起来被孤立。

所以本章的重点不在于「会写这个循环」，而在于 §094.3.2 的那个**割性质（cut property）**：它才是「局部最优 ⇒ 全局最优」的许可证书。没有它，Kruskal 只是个启发式。

### 卡点在哪

一句话：

> **这是一张完全图 `V = 10³`，`E ≈ 5·10⁵`。要在这 `5·10⁵` 条边里挑出 `V-1` 条，使它们连通且总权最小——这是一个组合优化问题，而我们要在近线性时间里解掉它。**

### 灵感的来源 —— 两个方向相反的贪心

**方向一（对边贪心）：Kruskal**

把全部边按权升序排好，从最便宜的边开始，**只要它不会和我已经选好的边构成环，就收下它**。凑够 `n-1` 条就停。

```
为什么「不成环」这个条件恰好够？因为：
  连通 + 无环 = 树；而 n 个点的树恰好 n-1 条边。
  所以「收下 n-1 条不成环的边」自动等于「连通了所有点」。
```

**方向二（对点贪心）：Prim**

从任意一点出发，维护「已选集合 `S`」与「每个未选点到 `S` 的最短边」。每轮把「离 `S` 最近的那个点」拉进 `S`，同时收下那条边。重复 `n-1` 轮。

```
Prim 的眼睛始终盯着「割」：每一轮都在回答
   「把 S 与 V\S 连起来，最便宜的那条边是哪条？」
```

两个方向最终都会得到同一棵（或同费用的）最小生成树——因为它们其实是**同一个割性质**的两种用法：Kruskal 在「当前并查集划分出的割」上取最小边，Prim 在「`S` 与 `V\S` 的割」上取最小边。

---

## 094.3 原理拆解

### 094.3.1 数据结构与算法选型

| 方案 | 时间 | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- |
| 每点连最近邻（启发式） | `O(n²)` | `O(n)` | 20 行 | **不保证连通也不保证最优**（反例 ⑤） |
| 枚举所有生成树 | `O(n^(n-2))` | `O(n)` | 30 行 | 完全不可用 |
| Kruskal + 用 DFS 判连通 | `O(E·V)` | `O(V+E)` | 50 行 | `E = 5·10⁵, V = 10³` → `5·10⁸`，慢 1000 倍 |
| **Kruskal + 并查集** ✅ | `O(E log E)` | `O(V+E)` | 55 行 | `E` 极大（`V ≥ 10⁴` 的完全图）时排序吃内存 |
| **Prim 数组版（线性找最近）** ✅ | **`O(V²)`** | `O(V)` | 35 行 | 稀疏图（`E ≈ 3V`）时白扫 `V²` 次 |
| Prim 堆版 | `O(E log V)` | `O(V+E)` | 60 行（Go 要写堆） | 稠密图上比数组版慢一个量级（`5·10⁵ × 10` vs `10⁶`） |
| Boruvka | `O(E log V)` | `O(V+E)` | 70 行 | 实现最复杂；优势在「并行求 MST」时体现 |

**稠密 / 稀疏的取舍，用本题的数字算一遍**（这张表要能现场推）：

| 方案 | `n = 1000`，`E = 5·10⁵` | 操作量级 | 备注 |
| --- | --- | --- | --- |
| Prim 数组版 | `V² = 10⁶` | 最小 | 与 `E` 无关，完全图下最优 |
| Kruskal | `E log E ≈ 5·10⁵ × 19 ≈ 10⁷` | 大 10 倍 | 但要先生成并排序 `5·10⁵` 条边 |
| Prim 堆版 | `E log V ≈ 5·10⁵ × 10 = 5·10⁶` | 大 5 倍 | 堆操作常数大 |
| Kruskal + DFS 判连通 | `E·V = 5·10⁸` | 大 500 倍 | 本章的反例级写法 |

> **结论**：完全图 / 稠密图 → **Prim 数组版**；稀疏图 → **Kruskal**；边是流式给出、无法预先枚举 → **Prim 堆版**。三者算法思想同源，差别只在「用数组扫描还是用堆取最小」。

### 094.3.2 为什么这个做法是对的（正确性论证）

#### 引理一：割性质（cut property）

**定义**：把点集分成两个非空部分 `(S, V\S)` 就叫一个**割**；跨越割的边里权最小的那条叫**轻边**（light edge）。

**命题**：对任意割 `(S, V\S)`，任何一条轻边都包含在**某棵**最小生成树里。

**证明（交换论证）**：设 `e = (u,v)` 是该割的一条轻边，`T` 是任意一棵最小生成树。

- 若 `e ∈ T`，结论已成立。
- 若 `e ∉ T`：把 `e` 加进 `T` 会形成唯一的一个环 `C`。由于 `u ∈ S`、`v ∈ V\S`，`C` 上必然还有另一条跨割的边 `e'`（环从 `S` 走出去必须再走回来）。由「轻边」的定义 `w(e) ≤ w(e')`。现在用 `e` 替换 `e'`，得到 `T' = T - e' + e`，它仍是生成树，且 `w(T') ≤ w(T)`。因为 `T` 已经是最小，所以 `w(T') = w(T)`，`T'` 也是最小生成树，且包含 `e`。∎

#### 引理二：Kruskal 收下的每条边都是「当时的某个割」的轻边

Kruskal 处理到边 `e` 时，如果 `e` 的两端分属**不同**的连通块，就让 `S` = 「`e` 的 `u` 端所在的那个连通块」，割就是 `(S, V\S)`。

- `e` 是这个割上**最轻**的边吗？假设存在更轻的跨割边 `f`，那么 `f` 在排序里排在 `e` 之前、已经被处理过；而 `f` 的两端也分属不同的连通块（一端在 `S`、一端在 `V\S`），所以 `f` 当时会被收下——这与「`f` 跨割」并不矛盾……需要更细：`f` 被处理后 `S` 与 `V\S` 就被并起来了，于是 `e` 的两端就会**同块**，与「不同块」矛盾。所以不存在更轻的跨割边，`e` 是轻边。∎

由引理一，`e` 属于某棵 MST。Kruskal 收下的边因此可以逐步扩展成一棵完整的 MST：**每一步都保持「已选边集是某棵 MST 的子集」这个不变量**，最后收到 `n-1` 条边时，它就是一棵 MST。∎

#### 引理三：Prim 收下的每条边都是「`S` 与 `V\S`」这个割的轻边

Prim 每一轮从「未选点」里挑 `minEdge` 最小的那个 `u`，它对应边 `(p, u)`（`p ∈ S`）。这条边就是割 `(S, V\S)` 上权最小的边——这正是 `minEdge` 数组的定义（`minEdge[v]` = `v` 到 `S` 的最短边）。由引理一，它属于某棵 MST。∎

#### 引理四：为什么停在 `n-1` 条边

连通 + 无环 = 树；`n` 个点的树恰好 `n-1` 条边。所以「收下 `n-1` 条不成环的边」蕴含「全部点连通」，同时也蕴含「再加任何一条边都会成环」。这也是为什么两个算法都可以在 `n-1` 条边时收工。

> **这套论证的价值**：它把「贪心为什么不是撞运气」讲清楚了——**贪心的合法性来自「割」，而割的性质是可证的**。同一套论证还会在 132–139 的贪心部分反复出现（交换论证）。

### 094.3.3 通用解题思路（可迁移的那一层）——兼全书图算法收口

本章是图形结构收口六章（090–095）的第四章。**全局的选型决策表在 [`091-network-delay-time.md`](091-network-delay-time.md) 的 §091.3.3**，本章对应这一行：

| 你观察到的条件 | 该用的算法 | 复杂度 | 本章位置 |
| --- | --- | --- | --- |
| 「把所有点连起来，总代价最小」 | **Kruskal（并查集）/ Prim** | `O(E log E)` / `O(V²)` | §094.5 |

把这个模型抽象出来：

```
最小生成树 = 「用最小的代价把一堆等价类合并成一个」

  并查集的视角：          Kruskal 就是「按代价从小到大做 union，
                         一共做 n-1 次成功的 union」
  生长的视角：            Prim 就是「从一个种子开始，每次吸收离种子最近的据点」

于是 MST 问题 ⇔ 「如何用最便宜的边把连通块合并成一个」
     ⇒ 这也解释了为什么 Kruskal 必须配并查集（合并 + 查是否同块）
```

同属这个模型的题：

| 题 | 它把什么当成点 / 边 |
| --- | --- |
| [LC 1135 最低成本连通所有城市](https://leetcode.cn/problems/connecting-cities-with-minimum-cost/) | 直接给边，标准 MST（会员题） |
| [LC 1168 水资源分配优化](https://leetcode.cn/problems/optimize-water-distribution-in-a-village/) | 虚拟源点 = 井，井的成本变成从虚拟源点出发的边 |
| [LC 1489 找到最小生成树里的关键边和伪关键边](https://leetcode.cn/problems/find-critical-and-pseudo-critical-edges-in-minimum-spanning-tree/) | 对每条边强制选/禁选，再跑 Kruskal 比较权值 |
| [LC 1631 最小体力消耗路径](https://leetcode.cn/problems/path-with-minimum-effort/) | 相邻格子的高差当边权，答案 = 并查集合并到连通时那条边的权（093） |
| [LC 959 由斜杠划分区域](https://leetcode.cn/problems/regions-cut-by-slashes/) | 把每个格子拆成 4 个三角形，判区域数（也可用并查集） |

### 094.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| 变成**有向图**，求「从根出发能到达所有点」的最小代价 | 最小树形图 | Chu-Liu / Edmonds 算法，`O(E·V)` 或 `O(E log V)` |
| 要求**返回边集**而不是总费用 | 需要记录选择 | Kruskal 天然能记录收下的边；Prim 记 `parent[u]` |
| 点带权（每个城市自己建站要花钱） | 加入虚拟源点 | 见 1168：把「建站成本」变成「虚拟源点 → 该点」的边，再求 MST |
| 求**最大**生成树 | 排序方向反过来 | Kruskal 按降序排；Prim 取 `max` |
| 边权允许**负数** | 算法照样正确 | MST 算法对权重的正负没有假设；但「最大生成树 + 负数」要小心取反后溢出 |
| 动态**加边**，反复问当前 MST 的权 | 需要增量维护 | 若新边比当前 MST 上的最大边小，则替换（用 LCT 维护树上最大边），`O(log V)` 每次 |

---

## 094.4 图解推演

### 094.4.1 示例的两种推演

`points = [[0,0],[2,2],[3,10],[5,2],[7,0]]`，先把完全图的 `10` 条边权算出来：

```
        p2 (3,10)
          │  ╲  9        距离表（对称）：
          │   ╲             d(0,1)=4   d(0,2)=13  d(0,3)=7   d(0,4)=7
          │    ╲            d(1,2)=9   d(1,3)=3   d(1,4)=7
     13   │     ╲  10       d(2,3)=10  d(2,4)=14
          │      ╲          d(3,4)=4
  p0(0,0)─┼─4────p1(2,2)───3────p3(5,2)────4────p4(7,0)
       ╰──────────7───────────────╯
```

**Kruskal 的收边过程**（按权升序）：

| 步 | 边 | 权 | `u,v` 同块？ | 动作 | 累计费用 | 已选边数 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `(1,3)` | 3 | 否 | 收下，合并 `{1,3}` | 3 | 1 |
| 2 | `(0,1)` | 4 | 否 | 收下，合并 `{0,1,3}` | 7 | 2 |
| 3 | `(3,4)` | 4 | 否 | 收下，合并 `{0,1,3,4}` | 11 | 3 |
| 4 | `(0,3)` | 7 | **是** | 跳过（会成环 0-1-3-0） | 11 | 3 |
| 5 | `(1,4)` | 7 | **是** | 跳过 | 11 | 3 |
| 6 | `(1,2)` | 9 | 否 | 收下，合并全部 | **20** | 4 = `n-1` ✅ 收工 |

**Prim 的收点过程**（从点 0 出发，`minEdge` 是「到已选集合的最短边」）：

| 轮 | 选中点 | `minEdge[u]` | 收下的边 | `minEdge` 更新后（未选点） | 累计 |
| --- | --- | --- | --- | --- | --- |
| 1 | `0` | 0 | — | `1:4, 2:13, 3:7, 4:7` | 0 |
| 2 | `1` | 4 | `(0,1)` | `2:9, 3:3, 4:7` | 4 |
| 3 | `3` | 3 | `(1,3)` | `2:9, 4:4` | 7 |
| 4 | `4` | 4 | `(3,4)` | `2:9` | 11 |
| 5 | `2` | 9 | `(1,2)` | 全部已选 | **20** |

**两条路线选的边集合完全相同**（`{1-3, 0-1, 3-4, 1-2}`）——这不是巧合：MST 可能不唯一，但**费用一定相同**，而这两个算法都是从割性质出发的，收敛到同一类解。

### 094.4.2 正方形反例：为什么「最近邻」不行

```
反例 ⑤：A(0,0) B(1,0) C(1,1) D(0,1)

   ┌───B───┐            每个点的最近距离都是 1
   │   │   │            「每点连最近」可能选出：
   D───┼───C              A-B, B-A, C-D, D-C
   │   │   │              ⇒ 边集 {A-B, C-D}，两个连通块 ❌
   └───────┘
                        若强制每点各出一条边（不去重）：
                           {A-B, B-C, C-D, D-A}  含环，费用 4 ❌

   正确 MST：任选 3 条边，费用 3 ✅
```

> **这张图的意义**：它证明「对点贪心」不满足割性质（因为「每个点的最近边」不一定是**某个割**的轻边），而「对边猜心」满足。**贪心能不能用，取决于贪心对象与割结构是否对齐。**

### 094.4.3 两条路线对比图

```
              ┌──────── 输入：完全图（n 个点，n(n-1)/2 条边）────────┐
              │                                                   │
    ┌─────────▼──────────┐                          ┌─────────────▼──────────┐
    │ Kruskal：按边生长   │                          │ Prim：按点生长         │
    │ O(E log E) + 并查集 │                          │ O(V²) 数组 / O(E log V) 堆 │
    └─────────┬──────────┘                          └─────────────┬──────────┘
              │                                                   │
      排序 5·10⁵ 条边 → 逐条 union                       每轮扫 10³ 个点找最近
              │                                                   │
      ✅ 稀疏图省事（不用 V² 扫描）                    ✅ 稠密图最优（与 E 无关）
      ✅ 天然能记录「选了哪些边」                      ✅ 不需要存 E 条边
      ❌ 稠密图要存 5·10⁵ 条边 + 排序 10⁷ 次             ❌ 稀疏图白扫 V² 次
              │                                                   │
      【E ≈ c·V：稀疏】                                【E ≈ V²：稠密，本题】
              └──────────────── 同一个割性质 ─────────────────────┘
```

---

## 094.5 双语言实现

### 094.5.1 Java

```java
import java.util.*;

class Solution {
    /** 解法一：Kruskal —— 全边排序 + 并查集 */
    public int minCostConnectPoints(int[][] points) {
        int n = points.length;
        if (n <= 1) {
            return 0;                       // 单点图：不需要任何边
        }
        int m = n * (n - 1) / 2;            // 完全图的边数
        int[][] edges = new int[m][];        // {u, v, w}
        int p = 0;
        for (int i = 0; i < n; i++) {
            for (int j = i + 1; j < n; j++) {
                int w = Math.abs(points[i][0] - points[j][0]) + Math.abs(points[i][1] - points[j][1]);
                edges[p++] = new int[]{i, j, w};
            }
        }
        Arrays.sort(edges, (a, b) -> a[2] - b[2]);   // 边权升序：贪心的前提

        int[] parent = new int[n];
        int[] rank = new int[n];
        for (int i = 0; i < n; i++) {
            parent[i] = i;
        }

        long total = 0;                     // 最坏 (n-1)·4·10⁶ ≈ 4·10⁹，必须用 long
        int used = 0;
        for (int[] e : edges) {
            int ra = find(parent, e[0]), rb = find(parent, e[1]);
            if (ra == rb) {
                continue;                   // 已成环，这条边不能进 MST
            }
            if (rank[ra] < rank[rb]) { int t = ra; ra = rb; rb = t; }
            parent[rb] = ra;
            if (rank[ra] == rank[rb]) { rank[ra]++; }
            total += e[2];
            if (++used == n - 1) {
                break;                      // n-1 条边即成树，后面的边一定更重
            }
        }
        return (int) total;
    }

    private int find(int[] parent, int x) {
        while (parent[x] != x) {
            parent[x] = parent[parent[x]];  // 隔代压缩
            x = parent[x];
        }
        return x;
    }

    /** 解法二：Prim 数组版 —— O(n²)，稠密图最优 */
    public int minCostConnectPointsPrim(int[][] points) {
        int n = points.length;
        int[] minEdge = new int[n];         // minEdge[v] = v 到「已选集合」的最短边
        boolean[] inTree = new boolean[n];
        Arrays.fill(minEdge, Integer.MAX_VALUE);
        minEdge[0] = 0;                     // 起点入树代价为 0

        long total = 0;
        for (int k = 0; k < n; k++) {
            int u = -1;
            for (int v = 0; v < n; v++) {   // O(n)：找离树最近的未选点
                if (!inTree[v] && (u == -1 || minEdge[v] < minEdge[u])) {
                    u = v;
                }
            }
            inTree[u] = true;
            total += minEdge[u];
            for (int v = 0; v < n; v++) {   // O(n)：用 u 更新其余点
                if (!inTree[v]) {
                    int d = Math.abs(points[u][0] - points[v][0])
                          + Math.abs(points[u][1] - points[v][1]);
                    if (d < minEdge[v]) {
                        minEdge[v] = d;
                    }
                }
            }
        }
        return (int) total;
    }

    /** 解法三：Prim 堆版 —— O(E log V)，边是流式给出时最好用 */
    public int minCostConnectPointsHeapPrim(int[][] points) {
        int n = points.length;
        boolean[] inTree = new boolean[n];
        PriorityQueue<int[]> pq = new PriorityQueue<>((a, b) -> a[0] - b[0]); // {w, v}
        pq.offer(new int[]{0, 0});

        long total = 0;
        int cnt = 0;
        while (!pq.isEmpty() && cnt < n) {
            int[] e = pq.poll();
            int w = e[0], v = e[1];
            if (inTree[v]) {
                continue;                   // 懒惰删除：这个点已经在树里
            }
            inTree[v] = true;
            total += w;
            cnt++;
            for (int u = 0; u < n; u++) {
                if (!inTree[u]) {
                    int d = Math.abs(points[v][0] - points[u][0])
                          + Math.abs(points[v][1] - points[u][1]);
                    pq.offer(new int[]{d, u});
                }
            }
        }
        return (int) total;
    }
}
```

### 094.5.2 Go

```go
package main

import (
	"container/heap"
	"sort"
)

func manhattan(a, b []int) int {
	dx := a[0] - b[0]
	if dx < 0 {
		dx = -dx // 不用 math.Abs：整数版自己判负更快，也不必引 math 包
	}
	dy := a[1] - b[1]
	if dy < 0 {
		dy = -dy
	}
	return dx + dy
}

// ---------- 解法一：Kruskal（按边权排序 + 并查集） ----------
type mstDSU struct {
	parent, rank []int
}

func newMstDSU(n int) *mstDSU {
	d := &mstDSU{parent: make([]int, n), rank: make([]int, n)}
	for i := range d.parent {
		d.parent[i] = i
	}
	return d
}

func (d *mstDSU) find(x int) int {
	for d.parent[x] != x {
		d.parent[x] = d.parent[d.parent[x]] // 路径隔代压缩
		x = d.parent[x]
	}
	return x
}

func (d *mstDSU) union(a, b int) bool {
	ra, rb := d.find(a), d.find(b)
	if ra == rb {
		return false
	}
	if d.rank[ra] < d.rank[rb] {
		ra, rb = rb, ra
	}
	d.parent[rb] = ra
	if d.rank[ra] == d.rank[rb] {
		d.rank[ra]++
	}
	return true
}

type kEdge struct{ u, v, w int }

func minCostConnectPointsKruskal(points [][]int) int {
	n := len(points)
	if n <= 1 {
		return 0 // 单点图
	}
	edges := make([]kEdge, 0, n*(n-1)/2) // 完全图：每条点对都是一条边
	for i := 0; i < n; i++ {
		for j := i + 1; j < n; j++ {
			edges = append(edges, kEdge{i, j, manhattan(points[i], points[j])})
		}
	}
	sort.Slice(edges, func(a, b int) bool { return edges[a].w < edges[b].w })

	d := newMstDSU(n)
	total, used := 0, 0
	for _, e := range edges {
		if d.union(e.u, e.v) { // 只在两端不同簇时才收下这条边
			total += e.w
			used++
			if used == n-1 { // 已是最小生成树，后面的边一定更重，提前收工
				break
			}
		}
	}
	return total
}

// ---------- 解法二：Prim（O(n²) 数组版，稠密图最快） ----------
func minCostConnectPointsPrim(points [][]int) int {
	n := len(points)
	const INF = 1 << 60
	minEdge := make([]int, n) // minEdge[v] = v 到「已选集合」的最短边
	inTree := make([]bool, n)
	for i := range minEdge {
		minEdge[i] = INF
	}
	minEdge[0] = 0

	total := 0
	for k := 0; k < n; k++ {
		u := -1 // 每轮线性扫出「离树最近」的未选点：O(n)
		for v := 0; v < n; v++ {
			if !inTree[v] && (u == -1 || minEdge[v] < minEdge[u]) {
				u = v
			}
		}
		inTree[u] = true
		if minEdge[u] < INF {
			total += minEdge[u]
		}
		for v := 0; v < n; v++ { // 用 u 更新其余未选点的最短边：O(n)
			if !inTree[v] {
				if d := manhattan(points[u], points[v]); d < minEdge[v] {
					minEdge[v] = d
				}
			}
		}
	}
	return total
}

// ---------- 解法三：Prim 的堆优化版（稀疏图 / 流式边友好） ----------
type pEdge struct {
	w int
	v int
}

type pEdgeHeap []pEdge

func (h pEdgeHeap) Len() int           { return len(h) }
func (h pEdgeHeap) Less(i, j int) bool { return h[i].w < h[j].w } // 小顶堆：最便宜的候选边先出
func (h pEdgeHeap) Swap(i, j int)      { h[i], h[j] = h[j], h[i] }
func (h *pEdgeHeap) Push(x interface{}) {
	*h = append(*h, x.(pEdge))
}
func (h *pEdgeHeap) Pop() interface{} {
	old := *h
	n := len(old)
	it := old[n-1]
	*h = old[:n-1]
	return it
}

func minCostConnectPointsHeapPrim(points [][]int) int {
	n := len(points)
	inTree := make([]bool, n)
	pq := &pEdgeHeap{{w: 0, v: 0}} // 从 0 号点出发，入树代价 0
	heap.Init(pq)

	total, cnt := 0, 0
	for pq.Len() > 0 && cnt < n {
		e := heap.Pop(pq).(pEdge)
		if inTree[e.v] {
			continue // 懒惰删除：这个点已经在树里了
		}
		inTree[e.v] = true
		total += e.w
		cnt++
		for v := 0; v < n; v++ {
			if !inTree[v] {
				heap.Push(pq, pEdge{w: manhattan(points[e.v], points[v]), v: v})
			}
		}
	}
	return total
}
```

### 094.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 边的表示 | `int[][] edges`，每行 `{u,v,w}` | `[]kEdge{u,v,w int}` 结构体 | Go 的结构体省内存、可读性好；Java 用数组省对象头 |
| 排序 | `Arrays.sort(edges, (a,b) -> a[2]-b[2])` | `sort.Slice(edges, func(i,j int) bool { return edges[i].w < edges[j].w })` | Java 的 `a[2]-b[2]` 在极端值下会溢出（`w ≤ 4·10⁶` 时安全）；Go 的 `Less` 返回布尔值，天然没有溢出问题 |
| 稳定性 | 对象数组排序是稳定归并（`TimSort`） | `sort.Slice` **不稳定**（pdqsort） | MST 费用不受影响；但要「输出某一条特定 MST」时不稳定会给出不同结果 |
| 累加类型 | `long total`，最后 `(int)` | `int` 在 64 位平台就是 64 位，天然够 | Java 侧若用 `int total` 会在 `n=1000` 的最坏数据上溢出成负数 |
| 堆 | `PriorityQueue<int[]>` | `container/heap` + 手写 5 个方法 | 与 091/093 完全相同的样板代码；`Less` 比 `h[i].w` |
| 并查集 | 传 `parent` 数组进 `find(...)`（静态写法） | 方法挂在 `*mstDSU` 上 | Go 没有「类内私有方法共享字段」的概念，状态必须显式挂在接收者上 |
| `INF` | `Integer.MAX_VALUE`（不会参与加法） | `1 << 60` | 本题 `minEdge` 不与权值相加，`MAX_VALUE` 也安全；但统一用半量程更省心 |

### 094.5.4 对拍测试：Prim 与 Kruskal 互拍（三实现交叉验证）

```go
package main

import (
	"math/rand"
	"testing"
)

func TestMinCostConnectPoints(t *testing.T) {
	r := rand.New(rand.NewSource(20241584))
	for iter := 0; iter < 12000; iter++ {
		n := 1 + r.Intn(9) // 1..9，含单点图
		points := make([][]int, n)
		for i := range points {
			points[i] = []int{r.Intn(21) - 10, r.Intn(21) - 10} // 值域小 → 大量重合点
		}
		want := minCostConnectPointsKruskal(points) // Kruskal 作为基准
		if got := minCostConnectPointsPrim(points); got != want {
			t.Fatalf("Prim 与 Kruskal 不一致：iter=%d p=%v prim=%d kruskal=%d",
				iter, points, got, want)
		}
		if got := minCostConnectPointsHeapPrim(points); got != want {
			t.Fatalf("堆版 Prim 与 Kruskal 不一致：iter=%d p=%v heapPrim=%d kruskal=%d",
				iter, points, got, want)
		}
	}
}
```

> ⚠️ **为什么随机点要用小值域 `[-10,10]`**：这样会大量出现**重合点**与**等距边**，逼出「0 权边」「多重最优解」等情形。若点都随机生成在 `10⁶` 范围里，几乎不会有两条边等权，MST 唯一，反倒测不出「等权时如何取舍」的分支。
> 另外注意三个实现互拍的价值：**Kruskal 与 Prim 的失败模式完全不同**（前者错在排序/并查集，后者错在 `minEdge` 更新），所以它们同时出错的概率极低。

---

## 094.6 复杂度与常见坑

### 094.6.1 复杂度推导

- **Kruskal**：
  - 生成边：`O(n²)` 次距离计算（`n(n-1)/2`）。
  - 排序：`O(E log E)`，其中 `E = n(n-1)/2`，所以是 `O(n² log n)`。
  - 并查集：`O(E·α(n))`，被排序吃掉。
  - 合计 **`O(n² log n)`**，空间 `O(n²)`（要存全部边）。
- **Prim 数组版**：
  - 外层 `n` 轮，每轮两次 `O(n)` 扫描 → **`O(n²)`**。空间只有 `minEdge` 与 `inTree`，`O(n)`。
  - **注意它完全不依赖 `E`**：这正是完全图下它的优势所在。
- **Prim 堆版**：每次入树把 `n` 个候选边压堆 → 总 `n²` 次 `push`，每次 `O(log n²)`，合计 `O(n² log n)`。**在完全图上它比数组版慢一个 `log`**，说明「堆优化」不是无条件的优势。

| 规模 | Prim 数组 | Kruskal | Prim 堆 |
| --- | --- | --- | --- |
| `n = 1000`（本题上限） | `10⁶` ✅ 最快 | `5·10⁵ × 19 ≈ 10⁷` | `10⁶ × 20 ≈ 2·10⁷` |
| `n = 10⁴`（完全图） | `10⁸`（勉强） | `5·10⁷ × 27 ≈ 1.4·10⁹` ❌ 且要存 5·10⁷ 条边 | `5·10⁷ × 27` ❌ |

### 094.6.2 七个真实踩过的坑

1. **`total` 用 `int`**：`n = 1000` 时最坏总费用 `(n-1) × 4·10⁶ ≈ 4·10⁹ > 2³¹-1`，Java 的 `int` 会溢出成负数。必须 `long`（Go 的 `int` 是 64 位，躲过一劫——**这就是「同一份代码两种语言表现不同」的典型**）。
2. **忘记 `n <= 1` 的短路**：`minCostConnectPoints` 里若直接生成边，`n = 1` 时 `edges` 为空，循环不执行，返回 `0` 恰好正确；但 Prim 里若忘了 `minEdge[0] = 0`，`u` 会选到 `-1` 然后数组越界。**两条都要显式处理。**
3. **Kruskal 忘了「跳过同块」**：`if (ra == rb) continue;` 少写，会把成环的边也加进总费用，答案偏大且可能少合并。
4. **Kruskal 忘记 `used == n-1` 提前停止**：不影响正确性，但在完全图上会白扫后面几十万条边，时间常数翻倍。
5. **Prim 里 `minEdge` 初始化成 0**：必须初始化成 `INF`、只把起点设成 0。若全设 0，第一轮随便挑点，答案直接错。
6. **堆版 Prim 忘记 `inTree` 判断**：同一节点会被多次收下，**它的边权被重复累加**，答案偏大。这个 bug 在小数据上也很容易漏（因为重合点/等距点少时，重复入堆的代价边往往不是最优边）。
7. **把「最近邻启发式」当成 MST**：`points` 里每个点连最近点，看起来合理，实际既不保证连通也不保证最优（反例 ⑤）。**面试里写出这个会被直接扣分。**

### 094.6.3 面试官最爱追问的三个问题

**Q1：为什么 Kruskal 是对的？**
> 「靠割性质：对任意的点集划分 `(S, V\S)`，跨割最轻的边一定属于某棵 MST——证明用交换论证，把这条边加进任意 MST 会形成一个环，环上必有一条跨割的旧边，替换后总权不增。Kruskal 每次收的边，恰好是『以它两端所在连通块为割』的轻边，所以每一步都在扩展某棵 MST 的边集，收到 `n-1` 条时就是完整的 MST。」

**Q2：这题用 Prim 还是 Kruskal？**
> 「取决于图是稠密还是稀疏。本题是完全图，`E = n(n-1)/2 ≈ 5·10⁵`，Prim 的数组版是 `O(n²) = 10⁶`，与 `E` 无关，最优；Kruskal 要先生成并排序 `5·10⁵` 条边，`O(E log E) ≈ 10⁷`，也能过但慢一个量级。判据是拿 `E` 和 `V log V` 比：`E` 明显大于 `V log V` 就用 Prim 数组版，否则用 Kruskal。另外如果边是流式给出、无法预先枚举，就只能用 Prim 堆版。」

**Q3：如果要求最大生成树 / 边权有负数 / 图不连通怎么办？**
> 「① 最大生成树：把排序方向反过来（Kruskal 降序，或 Prim 里取 `max`），算法结构完全不变——割性质对『最重边』同样成立。② 边权负数：MST 算法对符号没有假设，照样正确；但『取反后求最大生成树』要小心 `-MIN_VALUE` 溢出。③ 图不连通：MST 不存在，应求**最小生成森林**——Kruskal 跑完后合并次数会小于 `n-1`，可以用并查集统计连通块个数，或返回 `-1`。」

---

## 094.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 1135 最低成本连通所有城市](https://leetcode.cn/problems/connecting-cities-with-minimum-cost/) | 中等 | 直接给边列表，纯 Kruskal；要先判是否连通（返回 `-1`） |
| [LC 1168 水资源分配优化](https://leetcode.cn/problems/optimize-water-distribution-in-a-village/) | 困难 | 加虚拟源点：把「打井成本」变成「虚拟源点 → 该村」的边 |
| [LC 1489 找到最小生成树里的关键边和伪关键边](https://leetcode.cn/problems/find-critical-and-pseudo-critical-edges-in-minimum-spanning-tree/) | 困难 | 对每条边「强制选 / 禁选」各跑一次 Kruskal 比较权值 |
| [LC 1631 最小体力消耗路径](https://leetcode.cn/problems/path-with-minimum-effort/) | 中等 | 网格上的瓶颈路，等价于「并查集加边到连通」（093） |
| [LC 959 由斜杠划分区域](https://leetcode.cn/problems/regions-cut-by-slashes/) | 中等 | 把格子拆成 4 个三角，用并查集数连通块 |
| [LC 778 水位上升的泳池中游泳](https://leetcode.cn/problems/swim-in-rising-water/) | 困难 | 「按时间加边直到连通」，MST 思路的网格版 |
| [LC 684 冗余连接](https://leetcode.cn/problems/redundant-connection/) | 中等 | 并查集判环：Kruskal 的「另一半」（090） |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 1135 最低成本连通所有城市**
1. 什么情况下答案不存在？这个判断能不能在跑 Kruskal 的过程中顺手拿到？
2. Kruskal 结束时的「成功合并次数」与连通块个数是什么关系？

**LC 1168 水资源分配优化**
1. 有的村子自己打井更便宜，这个「打井」动作在图里能不能也看成一条边？
2. 如果引入一个「虚拟的城市」，它到每个村的边权该填多少？

**LC 1489 找到最小生成树里的关键边和伪关键边**
1. 「关键边」的定义是「去掉它，MST 的权值一定变大」，怎么用一次 Kruskal 验证？
2. 「伪关键边」是能被某棵 MST 选中的边，最少需要跑几次 Kruskal 才能判完所有边？

**通用**
- 看到「把所有东西连起来，总代价最小」，先想 MST，再问自己是稠密还是稀疏。
- 想验证「贪心为什么正确」，去构造「割」，然后问：我这一步选的边是不是某个割的轻边？

</details>

> **下一章**：[`095-reconstruct-itinerary.md`](095-reconstruct-itinerary.md) —— 从「选一部分边」换成「所有边恰好用一次」，进入欧拉路径。
