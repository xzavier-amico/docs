# 092. LC787 K 站中转内最便宜的航班（Cheapest Flights Within K Stops）

> **状态**：`已完成（初稿）` ｜ **所属**：第 9 部分 · 图形结构 ｜ **编号**：092 / 150
> **难度**：中等 ｜ **标签**：图 · Bellman-Ford · 分层图 · Dijkstra · 动态规划
> **一句话**：这道题真正考的是「**当最短路多了一条『最多 `k` 条边』的约束时，为什么必须给状态加一维、并且必须用上一轮快照**」，不是「背 Bellman-Ford 的循环次数」。
> **前置**：[`091-network-delay-time.md`](091-network-delay-time.md)（先建立 Dijkstra 的贪心不变量，本章要说明它为什么在「限边数」下失效）

---

## 092.1 题目

**原题链接**：<https://leetcode.cn/problems/cheapest-flights-within-k-stops/>

### 题意重述（把隐含条件补齐）

有 `n` 个城市，编号 `0..n-1`。给你航班列表 `flights`，`flights[i] = (u_i, v_i, price_i)` 表示「从 `u_i` 飞往 `v_i`，票价 `price_i`」的**单向**航班。现在你要从 `src` 飞到 `dst`，**最多允许中转 `k` 次**，求最小的总票价；如果不存在这样的行程，返回 `-1`。

它藏了五个必须读出来的约束：

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| **「最多 `k` 次中转」=「最多 `k+1` 条边」** | 「最多经过 `k` 站中转」 | 少算一轮就少考虑一整层的解；`k = 0` 时只能直飞 |
| **单向航班** | 「`u_i` 飞往 `v_i`」 | 回程要另建边，不能假设对称 |
| **票价可以等于 0**（`0 <= price_i`） | 约束 | 不能拿 `0` 当「不可达」的哨兵，也不能拿 `0` 当「还没算过」 |
| **可以有重边**（同一对城市多趟航班） | 约束里没有「`(u,v)` 互不相同」 | 同一对点的多条边都要保留，取最小是松弛的自然结果 |
| **求最小总价，不是最少中转** | 「最便宜的航班」 | 不能按边数 BFS 贪心，必须比较总价 |

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| `1 <= n <= 100` | 最多 100 个城市 | 分层图的点数最多 `100 × (101+1) ≈ 10⁴`，边最多 `10⁴ × 10⁴` 不行——要按「每层复制所有边」算：`O(k·E)` |
| `0 <= flights.length <= n*(n-1)` | 最多约 `10⁴` 条边 | `O(k·E) = 100 × 10⁴ = 10⁶`，Bellman-Ford 完全够 |
| `0 <= price_i <= 10^4` | 非负（含 0） | 边权非负 ⇒ Dijkstra 在**分层图**上仍然合法 |
| `1 <= src, dst, k+1 <= n` | `k ≤ n-1` | `k+1 ≤ n` 意味着「最多 `n` 条边」，已经覆盖所有简单路 |
| 答案范围 | `≤ 99 × 10⁴ ≈ 10⁶` | `int` 足够；但 `INF` 仍要取半量程防加法溢出 |

> 💡 **量级 → 算法**：`n ≤ 100` 意味着 `O(k·E) = 10⁶` 与 `O(k·E log(kV)) ≈ 2·10⁷` 都能过，`O(V³) = 10⁶` 的 Floyd 也能过。所以在本题「能不能过」不构成区分，**区分点在于你是否意识到「限边数」必须进状态**。

### 示例

```
输入：n = 4, flights = [[0,1,100],[1,2,100],[2,0,100],[1,3,600],[2,3,200]], src = 0, dst = 3, k = 1
输出：700
解释：0→1→3 = 100+600 = 700 是唯一可行的；0→1→2→3 要 3 条边（2 次中转），超过 k=1。

输入：n = 3, flights = [[0,1,100],[1,2,100],[0,2,500]], src = 0, dst = 2, k = 1
输出：200
解释：200 < 500，且只需 1 次中转。

输入：n = 3, flights = [[0,1,100],[1,2,100],[0,2,500]], src = 0, dst = 2, k = 0
输出：500
解释：只允许直飞。← 这一条是本章的照妖镜
```

### 我自造的五个边界（写完代码必须拿它们打一遍）

```text
① [[0,1,100],[1,2,100],[2,0,100]], src=0, dst=2, k=0  → -1
   若「同轮串联」会错得 200（本章 §092.3.2 的主角）
② 同一张图, k=1                                        → 200（正好一次中转）
③ flights = [], src=0, dst=1, k=3                      → -1（无航班）
④ [[0,1,0],[1,2,0]], src=0, dst=2, k=1                 → 0（票价全为 0，0 是合法答案）
⑤ [[0,1,5],[0,1,3],[1,2,1]], src=0, dst=2, k=1         → 4（重边取小的那条）
```

第 ④ 条专杀「用 `0` 表示还没算过」的写法：正确答案就是 `0`，用 `0` 当哨兵会把它误判成「没到达」并返回 `-1`。

---

## 092.2 从暴力到最优：第一个念头为什么错

### 第一个念头 A：「直接跑 Dijkstra」

`[091]` 刚讲完堆优化 Dijkstra，手感正热，于是很多人直接套过来。**它是错的**，而且错得很隐蔽：

```
n = 3, flights = [[0,1,100],[1,2,100],[0,2,500]], src = 0, dst = 2, k = 0
标准 Dijkstra：0 → 1 → 2，总价 200
题目要求：k = 0 只能直飞，正确答案是 500
```

Dijkstra 会给出 `200`，而它对应的行程用了 **1 次中转**，超出限制。**错因不是「松弛写错了」，而是「状态里缺了一维信息」：它不知道自己是「用几条边走过来的」。**

> ⚠️ 这是本章最重要的一句话：**约束不进状态，算法就会偷偷违反约束。** 当题目给「最多 `k` 条边 / 最多 `k` 次操作 / 最多 `k` 个颜色」时，第一反应必须是「状态要多一维」。

### 第一个念头 B：「BFS 按层搜，每层一条边，第一层到 `dst` 的就是答案」

层数确实是边数，`k+1` 层就能覆盖限制——但 **BFS 找到的是「边数最少」的到达方式，不是「总价最低」的**。要拿到最便宜的，必须在第 `k+1` 层内的所有到达里取最小，而那等于枚举同层的所有路径，BFS 的「第一次到达即最优」在这里不成立。

### 第一个念头 C：「DFS 枚举所有长度 `≤ k+1` 的路径」

正确，但组数爆炸：最坏 `O(n^k)`。`n = 100, k = 5` 时是 `10¹⁰`。**它只能当极小规模的对拍 oracle**（本章 §092.5.4 就拿它当参考实现）。

### 卡点在哪

一句话：

> **「最便宜的路径」和「不超过 `k+1` 条边的路径」是两个不同维度的约束，而最短路算法只能优化一个维度。**

- 只优化「总价」→ 得到 Dijkstra，可能违反边数限制；
- 只优化「边数」→ 得到 BFS，可能不是最便宜；
- 想同时表达两者 → **把边数变成状态的一部分**，于是「图」变成了「分层图」。

### 灵感的来源 —— 两个互通的建模

**建模一：分层图（把「用了几条边」做成一层）**

把原图的每个点复制成 `k+2` 份，记作 `(v, r)`，表示「已经用了 `r` 条边到达 `v`」。原来的边 `(u,v,w)` 变成 `(u,r) → (v,r+1)`，权 `w`。于是：

- **原问题 ⇔ 分层图上从 `(src, 0)` 到任意 `(dst, r)`（`r ≤ k+1`）的最短路**；
- 分层图里所有边权非负 ⇒ **Dijkstra 又是合法的**了。

**建模二：Bellman-Ford 按边数迭代**

Bellman-Ford 的每一轮天然就是「多走一条边」，所以只要迭代 `k+1` 轮，就得到「至多 `k+1` 条边」的最小花费。但这里有一个**必须做对**的细节：**每轮必须用上一轮的快照**，否则同一轮之内会「串联」。

```
prev = [0, INF, INF]        # 0 条边能到哪儿
第 1 轮用 prev 松弛出 cur   # 至多 1 条边
第 2 轮用 prev(=上一轮的 cur) 松弛出新的 cur   # 至多 2 条边
...
共 k+1 轮
```

两条路殊途同归：**Bellman-Ford 的「第 `r` 轮」和分层图的「第 `r` 层」是同一件事**，只是一个用两个一维数组滚动、一个用二维数组摊开。

---

## 092.3 原理拆解

### 092.3.1 数据结构与算法选型

| 方案 | 时间 | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- |
| 标准 Dijkstra（不带边数状态） | `O((V+E)log V)` | `O(V+E)` | 40 行 | **无视 `k` 限制，答案错**（不是慢，是错） |
| BFS 按层 | `O(V+E)` | `O(V)` | 30 行 | 求的是最少边数，不是最小总价 |
| DFS 枚举全部路径 | `O(V^k)` | `O(V)` | 25 行 | 只能当小规模 oracle |
| **Bellman-Ford 迭代 `k+1` 轮 + 快照** ✅ | **`O(k·E)`** | `O(V)` | 25 行 | `k` 很大（如 `10⁴`）时退化成 `O(kE)`；但对「边数有上限」的场景正好合适 |
| **分层图 Dijkstra** ✅ | `O(k·E·log(kV))` | `O(k·V)` | 60 行 | 常数更大；`k` 与 `V` 都大时内存吃紧 |
| 分层图 + Floyd 式全源 DP | `O(k·V·E)` | `O(V²)` | 30 行 | 只适合 `V` 很小（对拍用） |
| 记忆化 DFS（`dfs(v, 剩余边数)`） | `O(V·k)` 状态 | `O(V·k)` | 40 行 | 有环且状态可复用，但常数大；递归深度 `≤ k` |

**两个正解怎么选**（这是面试里真正会被问的取舍）：

| 维度 | Bellman-Ford + 快照 | 分层图 Dijkstra |
| --- | --- | --- |
| 时间复杂度 | `O(k·E)` | `O(k·E·log(kV))`，多一个 `log` |
| 空间复杂度 | `O(V)`（两个一维数组滚动） | `O(k·V)`（二维 `dist`） |
| 代码长度 | 短，两重循环 | 长，要写堆（Go 还要 5 个方法） |
| 能否提前退出 | 不能（必须跑满 `k+1` 轮） | 能（弹出 `dst` 即返回） |
| 适合的追问 | 「`k` 很小、`E` 很大」 | 「只要最小费用且希望尽早返回」 |

### 092.3.2 为什么这个做法是对的（正确性论证）

#### 命题一：Bellman-Ford 第 `r` 轮后，`prev[v]` 等于「至多用 `r` 条边从 `src` 到 `v` 的最小花费」

**归纳**：

- `r = 0`：不走路，只有 `prev[src] = 0`，其余 `INF`。成立。
- 设第 `r-1` 轮成立，看第 `r` 轮。任何「至多 `r` 条边」的路径 `P`，要么只有 0 条边（`r = 0` 的情形），要么可以写成「至多 `r-1` 条边到 `u`」+「最后一条边 `(u,v,w)`」。于是

  ```
  cur[v] = min( prev[v],  min over 边 (u,v,w) of ( prev[u] + w ) )
  ```

  这正是「至多 `r` 条边」的最优子结构：**少走一条边的最优解 + 最后一条边**。由于我们遍历**所有**边，一定能命中 `P` 的最后一条边，所以 `cur[v] ≤ |P|`；又 `cur[v]` 的每个候选值都对应一条合法的「至多 `r` 条边」路径，所以 `cur[v] ≥` 真正的最优值。两边夹住即相等。∎

#### 命题二：为什么**必须**用上一轮的快照

如果不用快照（直接在一个数组上原地松弛），那么**同一轮之内**刚刚被更新的值会被立刻用于更新别人，等价于「这一轮里走了好几条边」：

```
flights = [[0,1,100],[1,2,100],[2,0,100]], src = 0, dst = 2, k = 0

✅ 快照版（k+1 = 1 轮）：
   prev = [0, INF, INF]
   cur  = copy(prev) = [0, INF, INF]
   扫边 (0,1,100)：prev[0]=0 → cur[1] = 100
   扫边 (1,2,100)：prev[1]=INF → 跳过        ← 关键：只看 prev，不看刚刚写好的 cur[1]
   扫边 (2,0,100)：prev[2]=INF → 跳过
   prev = [0, 100, INF]  → dst=2 仍是 INF → 返回 -1 ✅

❌ 原地版（同一轮）：
   dist = [0, INF, INF]
   扫边 (0,1,100)：dist[1] = 100
   扫边 (1,2,100)：dist[1]=100 → dist[2] = 200   ← 用了「本轮刚写进去的」值，等于走了 2 条边
   结果 dist[2] = 200 → 返回 200 ❌（违反 k=0）
```

**一句话总结**：原地松弛让「一轮」不再是「一条边」，而是「任意多条边」，于是 `k` 的限制被悄悄丢掉，题目退化成「无限制最短路」——而在有负权的图上，这还会带来更坏的后果（`V-1` 轮后若仍能松弛，说明有负环）。

> 这个坑之所以高频，是因为「原地松弛更省内存」的直觉太强。**记住：Bellman-Ford 的轮次语义 = 边的条数，所以必须按层隔离。**

#### 命题三：分层图 Dijkstra 与原问题等价

分层图的每个节点是 `(v, r)`，每条边只在「层 `r` → 层 `r+1`」之间连接。因此：

1. 任何从 `(src,0)` 到 `(dst,r)` 的路径，其边数恰好是 `r`（每走一步层数 +1），即对应一条「恰好 `r` 条边」的原图行程，总价相同；
2. 反过来任何「恰好 `r` 条边」的行程，都对应分层图里一条从 `(src,0)` 到 `(dst,r)` 的路径。

于是「至多 `k+1` 条边的最小总价」= `min_{0 ≤ r ≤ k+1} dist[(dst, r)]`。分层图里所有边权 `≥ 0`，Dijkstra 的贪心不变量（见 §091.3.2）依旧成立，所以算法正确。∎

**顺带解释「为什么弹出 `dst` 就能立刻返回」**：小顶堆按 `cost` 升序弹出，第一次弹出的 `dst` 状态的 `cost` 一定是最小的；而后面任意 `(dst, r')` 的 `cost` 都不会更小。所以提前返回不损失最优性。Bellman-Ford 则没有这个性质——它必须把 `k+1` 轮全部跑完。

### 092.3.3 通用解题思路（可迁移的那一层）

**母模型：当最短路多了一条「资源约束」时，把资源做成状态维度。**

```
原问题：在图上找最优路径，代价 = Σ w
        ↓ 加一条约束：最多用 k 单位资源（边数 / 时间 / 中转次数 / 颜色切换次数）
标准做法：
        ① 状态 = (节点, 已用资源)，图 → 分层图（V × (k+2) 个节点）
        ② 若代价非负 → 分层图上跑 Dijkstra
        ③ 若「资源 = 边数」且只关心最少花费 → Bellman-Ford 迭代 k+1 轮（等价，且更省空间）
        ④ 若资源维度很小、点数也小 → 直接 DP：dp[r][v] = min(dp[r-1][u] + w)
```

这张表请对照 [`091-network-delay-time.md`](091-network-delay-time.md) 的 **§091.3.3 全书图算法选型决策表**里那一行：

| 条件 | 算法 | 复杂度 | 本章对应位置 |
| --- | --- | --- | --- |
| 限制「最多 `k` 条边走完」 | 分层图 Dijkstra / Bellman-Ford 迭代 `k+1` 轮（**必须快照**） | `O(k·E·log(kV))` / `O(k·E)` | 本章 §092.3.1、§092.5 |

同属「状态加维度」这个模型的题：

| 题 | 加的那一维是什么 |
| --- | --- |
| [LC 787 K 站中转内最便宜的航班](https://leetcode.cn/problems/cheapest-flights-within-k-stops/) | 已用边数（中转次数） |
| [LC 1129 颜色交替的最短路径](https://leetcode.cn/problems/shortest-path-with-alternating-colors/) | 上一条边的颜色（2 种状态） |
| [LC 847 访问所有节点的最短路径](https://leetcode.cn/problems/shortest-path-visiting-all-nodes/) | 已访问点集合（状压） |
| [LC 1293 网格中的最短路径](https://leetcode.cn/problems/shortest-path-in-a-grid-with-obstacles-elimination/) | 已消除的障碍数（`≤ k`） |
| [LC 1928 规定时间内到达终点的最小花费](https://leetcode.cn/problems/minimum-cost-to-reach-destination-in-time/) | 已花时间（`≤ maxTime`） |

**共同点**：都是在 `(节点)` 之外多一个「预算」维度，然后在这个乘积空间里做 BFS / Dijkstra / DP。

### 092.3.4 可扩展性（面试追问的五个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| 要求「**恰好** `k` 站中转」 | 「至多」变「恰好」 | 把「`cur = copy(prev)`」这步去掉（不许跳过本层），只在 `r == k+1` 层取答案 |
| 票价允许**负数** | 分层图里出现负边 | Dijkstra 失效；分层图是 DAG（层数只增），可以直接按层 DP，`O(k·E)`，天然能处理负权 |
| 还要输出**路径本身** | 需要回溯 | 记录 `parent[(v,r)] = (u,r-1)`，从 `(dst, r*)` 倒推 |
| `k` 大到 `10⁵`，`n ≤ 100` | `O(k·E)` 太大 | 关键观察：**简单路最多 `n-1` 条边**，所以 `k ≥ n-1` 时问题退化成普通最短路 → 直接跑一次 Dijkstra |
| 要「最便宜的 **`m` 条**不同行程」 | 需要排序结果 | 把每个状态扩展成「前 `m` 小」，堆里弹出第 `m` 次到达 `dst` 就是第 `m` 便宜 |

---

## 092.4 图解推演

### 092.4.1 分层图长什么样

`n = 3`，`flights = [[0,1,100],[1,2,100],[2,0,100]]`，`src = 0`，`k = 0`（最多 1 条边）。分层图只保留 `r = 0` 与 `r = 1` 两层：

```
   r = 0 层（还没飞）        r = 1 层（已飞 1 条边）
   ┌───────────────┐        ┌───────────────┐
   │  (0,0) ◀src   │──100──▶│  (1,1)        │
   │  (1,0)        │──100──▶│  (2,1) ◀dst ✔ │
   │  (2,0)        │──100──▶│  (0,1)        │
   └───────────────┘        └───────────────┘
        ↑ 起点只有这一层有 0 代价             ↑ 只在这层里找 dst

   结论：(3,1) 不存在 → 只有 (2,1)，但从 (0,0) 到 (2,1) 需要一条 0→2 的边，
        原图没有这条边（0→2 要经过 1），所以无解 → -1 ✅
```

要点：**分层图把「边数」变成了「空间上的层」**，于是「至多 `k+1` 条边」变成了「只在前 `k+2` 层里找」，约束不再需要在松弛时判断。

### 092.4.2 Bellman-Ford 的滚动数组推演

用 `flights = [[0,1,100],[1,2,100],[2,0,100]]`、`src=0`、`dst=2`，比较 `k=0` 与 `k=1`：

| 轮次 `r` | `prev`（至多 `r` 条边） | 本轮扫边发生的更新 | `prev` 变成 |
| --- | --- | --- | --- |
| 0（初始） | `[0, INF, INF]` | — | `[0, INF, INF]` |
| 1（`k=0` 时只有这轮） | `[0, INF, INF]` | `0→1`：`cur[1]=100`；`1→2` 的 `prev[1]=INF` 跳过 | `[0, 100, INF]` → 返回 `-1` |
| 2（`k=1` 时多这轮） | `[0, 100, INF]` | `1→2`：`prev[1]+100 = 200 → cur[2]=200` | `[0, 100, 200]` → 返回 `200` |

**同轮串联的反例对照**（同一张图，`k=0`，原地松弛）：

| 扫到的边 | 原地 `dist` | 说明 |
| --- | --- | --- |
| 初始 | `[0, INF, INF]` | — |
| `(0,1,100)` | `[0, 100, INF]` | 正常 |
| `(1,2,100)` | `[0, 100, **200**]` | ❌ 用了本轮刚写进 `dist[1]` 的值 = 走了 2 条边 |
| `(2,0,100)` | `[0, 100, 200]` | `dist[2]=200` 再影响 `dist[0]`（本轮内还会继续串联） |

### 092.4.3 分层图 Dijkstra 的弹出顺序

`n = 4`，`flights = [[0,1,1],[0,2,5],[1,2,1],[1,3,10],[2,3,1]]`，`src = 0`，`dst = 3`。

```
       ① 1       ②
  0 ──1──▶ 1 ──1──▶ 2        ③ = 3 号点(dst)
  │        │        │
  5        10       1
  │        └────┬───┘
  ▼             ▼
  2 ───────────▶③
```

**`k = 1`（最多 2 条边）**：

| 步 | 弹出 `(cost, node, edges)` | 松弛 | 说明 |
| --- | --- | --- | --- |
| 1 | `(0, 0, 0)` | `dist[1][1]=1`、`dist[2][1]=5` | 起点出发 |
| 2 | `(1, 1, 1)` | `dist[2][2]=2`、`dist[3][2]=11` | 用第 2 条边到 2 与 3 |
| 3 | `(2, 2, 2)` | 边数已用尽，不能再飞 | 这一层不再扩展 |
| 4 | `(5, 2, 1)` | `dist[3][2]=6`（`5 < 11` ✅） | 直飞 2 号再转 3 号，共 6 |
| 5 | `(6, 3, 2)` | 弹出 `dst` → **返回 6** | 最优 |

**`k = 2`（最多 3 条边）**：把上面第 3 步的「不能再飞」变成可飞，于是 `(2,2,2) → dist[3][3] = 3`，最终弹出 `(3, 3, 3)` 并返回 **`3`**。

> **对比出真相**：多给一次中转，答案从 `6` 掉到 `3`。这就是「约束不进状态」会错得离谱的原因——Dijkstra 眼里 `3` 一直存在，只是它不知道自己「没资格走」。

---

## 092.5 双语言实现

### 092.5.1 Java

```java
import java.util.*;

class Solution {
    private static final int INF = Integer.MAX_VALUE / 2; // 半量程：加法不溢出

    /** 解法一：Bellman-Ford，迭代 k+1 轮，每轮必须用「上一轮快照」 */
    public int findCheapestPrice(int n, int[][] flights, int src, int dst, int k) {
        int[] prev = new int[n];        // prev[v] = 至多「上一轮轮数」条边时到 v 的最小花费
        Arrays.fill(prev, INF);
        prev[src] = 0;

        for (int round = 0; round < k + 1; round++) {  // 上限 k+1 条边 → k+1 轮
            int[] cur = prev.clone();                  // ★ 快照：cur 先完整复制 prev
            for (int[] f : flights) {
                int u = f[0], v = f[1], w = f[2];
                if (prev[u] == INF) {
                    continue;                          // 上一轮到不了 u，这条边用不上
                }
                if (prev[u] + w < cur[v]) {
                    cur[v] = prev[u] + w;              // 只写 cur：写 prev 就会同轮串联
                }
            }
            prev = cur;
        }
        return prev[dst] >= INF ? -1 : prev[dst];
    }

    /** 解法二：分层图 Dijkstra，状态 = (节点, 已用边数) */
    public int findCheapestPriceDijkstra(int n, int[][] flights, int src, int dst, int k) {
        List<List<int[]>> adj = new ArrayList<>(n);
        for (int i = 0; i < n; i++) {
            adj.add(new ArrayList<>());
        }
        for (int[] f : flights) {
            adj.get(f[0]).add(new int[]{f[1], f[2]});
        }

        int maxEdges = k + 1;
        int[][] dist = new int[n][maxEdges + 1];        // dist[v][r] = 恰好 r 条边的最小花费
        for (int[] row : dist) {
            Arrays.fill(row, INF);
        }
        dist[src][0] = 0;

        // 堆元素 = {cost, node, edges}，按 cost 升序
        PriorityQueue<int[]> pq = new PriorityQueue<>((a, b) -> a[0] - b[0]);
        pq.offer(new int[]{0, src, 0});

        while (!pq.isEmpty()) {
            int[] s = pq.poll();
            int cost = s[0], u = s[1], used = s[2];
            if (cost > dist[u][used]) {
                continue;                               // 过期条目
            }
            if (u == dst) {
                return cost;                            // 第一次弹出 dst 即最小代价
            }
            if (used == maxEdges) {
                continue;                               // 边数用尽，这一层不再扩展
            }
            for (int[] e : adj.get(u)) {
                int v = e[0], w = e[1];
                if (cost + w < dist[v][used + 1]) {     // 只能走到「下一层」
                    dist[v][used + 1] = cost + w;
                    pq.offer(new int[]{dist[v][used + 1], v, used + 1});
                }
            }
        }
        return -1;
    }
}
```

### 092.5.2 Go

```go
package main

import (
	"container/heap"
	"math"
)

const INF092 = math.MaxInt32 / 2 // 用半量程，加法不溢出

// ---------- 解法一：Bellman-Ford，迭代 k+1 轮，每轮用「上一轮快照」 ----------
func findCheapestPriceBF(n int, flights [][]int, src int, dst int, k int) int {
	prev := make([]int, n)
	for i := range prev {
		prev[i] = INF092
	}
	prev[src] = 0

	for round := 0; round < k+1; round++ { // 上限 k+1 条边 → 迭代 k+1 轮
		cur := make([]int, n)
		copy(cur, prev) // ★ 关键：必须新建切片再 copy。写成 cur := prev 会让两者共用底层数组，快照失效
		for _, f := range flights {
			u, v, w := f[0], f[1], f[2]
			if prev[u] == INF092 {
				continue // 上一轮都到不了 u，这条边不可能用上
			}
			if prev[u]+w < cur[v] {
				cur[v] = prev[u] + w // 只写 cur：写 prev 就会同轮串联
			}
		}
		prev = cur
	}

	if prev[dst] >= INF092 {
		return -1
	}
	return prev[dst]
}

// ---------- 解法二：分层图（节点 + 已用边数）上的 Dijkstra ----------
type State struct {
	node  int
	edges int
	cost  int
}

type stateHeap []State

func (h stateHeap) Len() int           { return len(h) }
func (h stateHeap) Less(i, j int) bool { return h[i].cost < h[j].cost } // 小顶堆：按总价排序
func (h stateHeap) Swap(i, j int)      { h[i], h[j] = h[j], h[i] }
func (h *stateHeap) Push(x interface{}) {
	*h = append(*h, x.(State))
}
func (h *stateHeap) Pop() interface{} {
	old := *h
	n := len(old)
	it := old[n-1]
	*h = old[:n-1]
	return it
}

func findCheapestPriceDijkstra(n int, flights [][]int, src int, dst int, k int) int {
	adj := make([][][2]int, n) // adj[u] = {{v, price}, ...}
	for _, f := range flights {
		adj[f[0]] = append(adj[f[0]], [2]int{f[1], f[2]})
	}

	maxEdges := k + 1 // 最多允许的边数
	dist := make([][]int, n)
	for i := range dist {
		dist[i] = make([]int, maxEdges+1)
		for j := range dist[i] {
			dist[i][j] = INF092
		}
	}
	dist[src][0] = 0

	pq := &stateHeap{{node: src, edges: 0, cost: 0}}
	heap.Init(pq)

	for pq.Len() > 0 {
		s := heap.Pop(pq).(State)
		if s.cost > dist[s.node][s.edges] {
			continue // 过期条目
		}
		if s.node == dst {
			return s.cost // 小顶堆第一次弹出 dst 的代价即最小代价
		}
		if s.edges == maxEdges {
			continue // 边数用尽，不能再飞
		}
		for _, e := range adj[s.node] {
			if nc := s.cost + e[1]; nc < dist[e[0]][s.edges+1] {
				dist[e[0]][s.edges+1] = nc
				heap.Push(pq, State{node: e[0], edges: s.edges + 1, cost: nc})
			}
		}
	}
	return -1
}
```

### 092.5.3 双语言差异批注（重点：快照的写法 + 堆）

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| **快照怎么写** | `int[] cur = prev.clone();` 或 `Arrays.copyOf(prev, n)` | **必须** `cur := make([]int, n); copy(cur, prev)` | Go 里写 `cur := prev` 得到的是**同一个底层数组的两个切片头**，`cur[v] = x` 会改到 `prev[v]`，快照完全失效 → 直接错成「不限边数」 |
| `clone` vs `copy` 的语义 | `clone()` 复制元素 | `copy(dst, src)` 复制 `min(len)` 个元素，**不会扩容** | Go 的 `copy` 不会 panic，长度写错时静默少拷，最隐蔽的 bug 之一 |
| 堆 | `PriorityQueue<int[]>` + `Comparator` | `container/heap` + 手写 `Len/Less/Swap/Push/Pop` | Go 要写 5 个方法；`Less` 比的是 `h[i].cost` 而不是 `h[i]` |
| 元组 | 用 `int[]{cost, node, edges}` 打包 | 用结构体 `State{node, edges, cost}` | Go 的结构体更清晰；Java 数组下标语义容易记混（`s[0]` 到底是啥） |
| 二维数组初始化 | `new int[n][m]` 后 `Arrays.fill(row, INF)` | `make([][]int, n)` 再逐行 `make` + 循环填 | Go 不做这一步就是 `nil` 切片，一写就 panic |
| `INF` 的选择 | `Integer.MAX_VALUE / 2` | `math.MaxInt32 / 2` 或 `1 << 60` | 两边都要防「`INF + w` 溢出」；Go 的 `int` 是 64 位，用 `1<<60` 更安全 |
| 提前返回 | 可以（弹出 `dst`） | 可以（同） | 但 Bellman-Ford 版本两种语言都**不能**提前收工 |

### 092.5.4 对拍测试：DFS 枚举 + Floyd 式分层 DP 双参考

```go
package main

import (
	"math/rand"
	"testing"
)

// 参考一：DFS 枚举所有长度 ≤ k+1 的走法（允许重复城市），取最小花费
func dfsRef(n int, flights [][]int, src, dst, k int) int {
	adj := make([][][2]int, n)
	for _, f := range flights {
		adj[f[0]] = append(adj[f[0]], [2]int{f[1], f[2]})
	}
	const INF = 1 << 30
	best := INF
	var dfs func(u, cost, used int)
	dfs = func(u, cost, used int) {
		if u == dst {
			if cost < best {
				best = cost
			}
			return // 边权非负：再往前走只会更贵
		}
		if used == k+1 {
			return
		}
		for _, e := range adj[u] {
			dfs(e[0], cost+e[1], used+1)
		}
	}
	dfs(src, 0, 0)
	if best >= INF {
		return -1
	}
	return best
}

// 参考二：Floyd 思路的「按边数分层」全源 DP：f[e][i][j] = 至多 e 条边 i→j 的最小花费
func fwRef(n int, flights [][]int, src, dst, k int) int {
	const INF = 1 << 30
	prev := make([][]int, n)
	for i := range prev {
		prev[i] = make([]int, n)
		for j := range prev[i] {
			prev[i][j] = INF
		}
		prev[i][i] = 0
	}
	for e := 0; e < k+1; e++ {
		cur := make([][]int, n)
		for i := range cur {
			cur[i] = make([]int, n)
			copy(cur[i], prev[i])
		}
		for _, f := range flights { // 用「恰好再加一条边」更新所有点对
			u, v, w := f[0], f[1], f[2]
			for i := 0; i < n; i++ {
				if prev[i][u] < INF && prev[i][u]+w < cur[i][v] {
					cur[i][v] = prev[i][u] + w
				}
			}
		}
		prev = cur
	}
	if prev[src][dst] >= INF {
		return -1
	}
	return prev[src][dst]
}

func TestCheapestFlights(t *testing.T) {
	r := rand.New(rand.NewSource(20240787))
	for iter := 0; iter < 12000; iter++ {
		n := 2 + r.Intn(4) // 2..5
		m := r.Intn(10)
		flights := make([][]int, 0, m)
		for i := 0; i < m; i++ {
			u, v := r.Intn(n), r.Intn(n)
			w := 1 + r.Intn(9) // 值域小 → 大量等票价与重边
			flights = append(flights, []int{u, v, w})
		}
		src := r.Intn(n)
		dst := r.Intn(n)
		for dst == src {
			dst = r.Intn(n)
		}
		k := r.Intn(4) // 含 k = 0

		want := dfsRef(n, flights, src, dst, k)
		if fw := fwRef(n, flights, src, dst, k); fw != want { // 先让两个参考互相验证
			t.Fatalf("参考不一致：iter=%d n=%d flights=%v src=%d dst=%d k=%d dfs=%d fw=%d",
				iter, n, flights, src, dst, k, want, fw)
		}
		if got := findCheapestPriceBF(n, flights, src, dst, k); got != want {
			t.Fatalf("BF 错：iter=%d flights=%v src=%d dst=%d k=%d got=%d want=%d",
				iter, flights, src, dst, k, got, want)
		}
		if got := findCheapestPriceDijkstra(n, flights, src, dst, k); got != want {
			t.Fatalf("分层 Dijkstra 错：iter=%d flights=%v src=%d dst=%d k=%d got=%d want=%d",
				iter, flights, src, dst, k, got, want)
		}
	}
}
```

> ⚠️ **为什么要两个参考实现**：DFS 是「枚举」思路，Floyd-DP 是「递推」思路，两者与两个正解的实现路径完全不同。**四个实现对同一组随机数据给出同一个答案，才说明是算法对了，而不是四个 bug 一起错。**
> 另外注意 DFS 参考里的提前 `return`（到 `dst` 就不再走）：它依赖「票价非负」。若面试官把 `price` 放宽成可以有负数，这个剪枝就错了——这也是 §092.3.4 里「负权怎么办」的伏笔。

---

## 092.6 复杂度与常见坑

### 092.6.1 复杂度推导

- **Bellman-Ford 版**：
  - 时间：`k+1` 轮，每轮扫全部 `E` 条边 → `O(k·E)`。本题 `k ≤ 99`、`E ≤ 10⁴` → 约 `10⁶` 次，毫秒级。
  - 空间：只有 `prev`、`cur` 两个长度 `n` 的数组 → `O(V)`。**这是它比分层图更优雅的地方：把「层」压进时间维度而不是空间维度。**
- **分层图 Dijkstra 版**：
  - 时间：分层图有 `V·(k+2)` 个节点、`E·(k+1)` 条边；Dijkstra 是 `O((V'+E') log V') = O(k·E·log(kV))`。比 Bellman-Ford 多一个 `log`，但能提前返回。
  - 空间：`dist` 是 `n × (k+2)` → `O(k·V)`，堆最坏 `O(k·E)`。
- **两个版本的对比**：当 `k` 很小、`E` 很大时，Bellman-Ford 的常数优势明显；当答案很早就能确定时，分层 Dijkstra 的提前返回能省掉大半工作。

### 092.6.2 七个真实踩过的坑

1. **不用快照（同轮串联）**：最经典、最高频。表现为 `k=0` 时照样给出「中转后更便宜」的答案，而样例恰好测不出来（样例 3 的 `k=0` 才是唯一能暴露它的）。
2. **Go 里 `cur := prev`**：这不是快照，而是别名。上面那个坑的 Go 版本，而且更隐蔽——因为你「以为自己复制了」。
3. **轮数算成 `k` 轮**：`k` 站中转 = `k+1` 条边。少一轮就少一层解，`k=0` 时会直接返回 `-1`（因为一轮都不跑，`prev[dst]` 恒为 `INF`）。
4. **用 `0` 当「不可达」哨兵**：票价可以是 `0`，正确答案也可能是 `0`。必须用 `INF` 判不可达。
5. **`INF` 取 `Integer.MAX_VALUE`**：`INF + w` 溢出成负数，于是「不可达」被当成「负花费」，答案变成一个负数。用半量程。
6. **忘记 `dst` 也可能等于 `src`**（约束外）：此时答案应为 `0`（不飞），`prev[src] = 0` 天然给出 `0`，但 Dijkstra 版若在起点就判 `dst` 也能正确返回 `0`——两种写法都要验证一下。
7. **分层图里跨层乱走**：松弛写成 `dist[v][used]` 而不是 `dist[v][used+1]`，等于允许「不花边数就移动」，`k` 的限制再次失效。必须是**下一层**。

### 092.6.3 面试官最爱追问的三个问题

**Q1：为什么 Bellman-Ford 必须用上一轮的快照？**
> 「因为它的轮次语义就是『边数』。原地松弛会让同一轮里的更新立刻被继续利用，等于一轮里走了好几条边，`k` 的约束被丢掉，问题退化成无限制最短路。用快照（新建数组 copy）才能保证第 `r` 轮只用 `≤ r-1` 条边的最优值去扩展，得到 `≤ r` 条边的最优值。这也是为什么 Bellman-Ford 判定负环时那句『第 `V` 轮还能松弛』是有意义的——它严格依赖轮次与边数的对应。」

**Q2：`k` 很大怎么办？**
> 「关键观察是『简单路最多 `n-1` 条边』，而所有边权非负，所以最优解一定可以取简单路。因此当 `k ≥ n-1` 时问题退化成普通最短路，直接跑一次 Dijkstra 即可，`O((V+E)log V)`，与 `k` 无关。如果 `k` 只是中等（比如 `10³`）而 `V` 也大，就老老实实 `O(k·E)`，或者用矩阵快速幂把 `min-plus` 乘积做 `O(V³ log k)`——那是另一条路。」

**Q3：这两个解法本质上是一回事吗？**
> 「是。Bellman-Ford 的第 `r` 轮 = 分层图的第 `r` 层。区别只在于：Bellman-Ford 用**时间**（轮次）表示层，空间 `O(V)`；分层图用**空间**（二维 `dist`）表示层，空间 `O(kV)`。前者不能用堆加速（因为层的顺序是强制的），后者可以（因为在同一层里可以贪心）。所以前者是 `O(kE)`，后者是 `O(kE log(kV))`：**用空间换一个 `log`，再用堆换提前返回的能力。**」

---

## 092.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 1928 规定时间内到达终点的最小花费](https://leetcode.cn/problems/minimum-cost-to-reach-destination-in-time/) | 困难 | 状态是「已花时间」，且边可以重复走：`dist[v][t]` 二维 DP |
| [LC 1129 颜色交替的最短路径](https://leetcode.cn/problems/shortest-path-with-alternating-colors/) | 中等 | 状态 = (节点, 上一条边颜色)，BFS 两状态即可 |
| [LC 1293 网格中的最短路径](https://leetcode.cn/problems/shortest-path-in-a-grid-with-obstacles-elimination/) | 困难 | 状态 = (格子, 已消除障碍数)，BFS 分层 |
| [LC 847 访问所有节点的最短路径](https://leetcode.cn/problems/shortest-path-visiting-all-nodes/) | 困难 | 状态 = (节点, 已访问集合)，状压 + BFS |
| [LC 1976 到达目的地的方案数](https://leetcode.cn/problems/number-of-ways-to-arrive-at-destination/) | 中等 | Dijkstra 顺带统计最短路条数（无 `k` 约束） |
| [LC 1631 最小体力消耗路径](https://leetcode.cn/problems/path-with-minimum-effort/) | 中等 | 另一类变形：转移从「相加」变成「取 max」 → 093 |
| [LC 1334 阈值距离内邻居最少的城市](https://leetcode.cn/problems/find-the-city-with-the-smallest-number-of-neighbors-at-a-threshold-distance/) | 中等 | 全源最短路，`V ≤ 100` 用 Floyd |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 1928 规定时间内到达终点的最小花费**
1. 这里被约束的资源不是「边数」而是「时间」，而且同一条边可以反复走——状态该怎么定义才不会死循环？
2. 若把 `dp[t][v]` 按 `t` 递增推进，转移方向和 092 的快照版有什么相似之处？

**LC 1129 颜色交替的最短路径**
1. 「交替」这个约束会影响可达性，能不能只用「上一条边的颜色」两bit就把它编码进状态？
2. 因为每步代价都是 1，[`091-network-delay-time.md`](091-network-delay-time.md) §091.3.3 决策表里「无权 → BFS」那一行在这里还成立吗？

**LC 847 访问所有节点的最短路径**
1. 「已经访问过哪些点」最多 12 个点，用什么编码？
2. 每个点都可能是起点，能不能一次多源 BFS 搞定？

**通用**
- 见到「最多 `k` 次 / 最多 `k` 条 / 剩余 `k` 个」，先问：这个 `k` 进状态后，状态空间有多大？能不能承受？
- 只要做了「按层/按轮推进」，就要立刻检查一件事：**同一层里有没有互相污染**。

</details>

> **下一章**：[`093-path-with-minimum-effort.md`](093-path-with-minimum-effort.md) —— 把转移从「相加」换成「取 max」，看 Dijkstra 的框架为什么还能用。
