# 127. LC121 买卖股票的最佳时机（Best Time to Buy and Sell Stock）

> **状态**：`已完成（初稿）` ｜ **所属**：第 11 部分 · 动态规划 ｜ **编号**：127 / 150
> **难度**：简单 ｜ **标签**：数组 · 动态规划 · 贪心 · 前缀最小值 · 状态机
> **一句话**：这道题真正考的是「**把『枚举买点卖点』的 `O(n²)` 降成『边扫边记历史最低价』的 `O(n)`**」，它是整个「股票系列」的总入口 —— 后面 128 / 129 / 188 / 309 / 714 全部是它的状态机扩展。
> **前置**：[`108-climbing-stairs.md`](108-climbing-stairs.md)（DP 四步法）· [`110-maximum-subarray.md`](110-maximum-subarray.md)（「以 `i` 结尾」的线性 DP 思路）

---

## 127.1 题目

**原题链接**：<https://leetcode.cn/problems/best-time-to-buy-and-sell-stock/>

### 题意重述（把隐含条件补齐）

给你一个数组 `prices`，第 `i` 天股票价格是 `prices[i]`。你**只能完成一笔交易**（选一天买入、之后的某一天卖出），求能获得的最大利润。若无法获利，返回 **0**。

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| **只能买卖各一次** | 「You want to maximize your profit by choosing a **single day** to buy… a **different day in the future** to sell」 | 若允许多次买卖，`[7,1,5,3,6,4]` 答 7（两个波段都吃），正确 5 |
| **买必须在卖之前** | 「a different day in the future」 | 若允许先卖后买（做空），`[7,6,4,3,1]` 会答 6，正确 0 |
| 不能同一天买卖 | 同上「different day」 | 同一天买卖利润恒 0，无害，但语义上不合法 |
| 无利润时返回 **0** | 「If no profit, return 0」 | 初值写成 `-inf` 或「第一天的价差」会答负数 |
| `prices[i] >= 0` | 约束 `0 <= prices[i]` | 不影响主解；但「最小价格」的初值不能取 0（价格 0 是合法值） |

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| `1 <= prices.length <= 10^5` | n 最大十万 | **`O(n²) = 5·10⁹` → 必 TLE**；`O(n)` 是唯一舒适解 |
| `0 <= prices[i] <= 10^4` | 非负、值域小 | 利润上界 `10^4`，`int` 绝对安全；也不会出现「负价格」（负价格会让最优做法变成「一直等到最负」） |
| 单笔交易 | — | 答案一定形如 `prices[j] − prices[i]`（`i < j`） |
| 无取模 | — | 不需要 `long`，但换成 `10^9` 值域 + 多次交易时就要 `long` |

> 💡 **量级 → 算法**：`n = 10^5` + 「最大差值」= 必须 `O(n)`。而 `O(n)` 的钥匙只有一个：**对每个卖点，买点取「它之前的最低价格」**。

### 示例

```text
输入：prices = [7,1,5,3,6,4]
输出：5
解释：第 2 天买入（价格 1）、第 5 天卖出（价格 6），利润 5
     注意不能「第 1 天买第 2 天卖 + 第 2 天买第 5 天卖」，因为只允许一笔交易

输入：prices = [7,6,4,3,1]
输出：0
解释：价格一直跌，任何买卖都亏，所以选择不交易

输入：prices = [1]
输出：0
解释：只剩一天，无法完成「买 + 卖」
```

### 我自造的四个反例（写完解法必须拿它们打一遍）

```text
① [1]             → 只有一天：答案 0。循环里的比较一次都不该发生「卖出」动作
② [5,5,5]         → 全相同：答案 0，检验「利润为 0 时不更新」的写法
③ [7,6,4,3,1]     → 单调递减：答案 0，检验 best 的初值是否为 0 而不是 -1
④ [2,4,1]         → 陷阱顺序：最低点在最后，卖点必须【在买点之后】
                     正确答案 2（2 买 4 卖）；若「先找全局最小再找其后最大」会答 0
```

第 ④ 条是最经典的坑：**最小值出现在最后一天，它不能当买点**。这正是「边扫边记最小值」正确的关键 —— 我们只在「已经扫过的前缀」里取最小值，天然保证买点早于卖点。

---

## 127.2 从暴力到最优：第一个念头为什么错

### 第一步：暴力 —— 枚举所有 `(买, 卖)` 组合

```text
best = 0
for i in 0..n-1:                # 买点
    for j in i+1..n-1:          # 卖点（必须在买点之后）
        best = max(best, prices[j] - prices[i])
return best
```

- 时间 **`O(n²)`**，空间 `O(1)`。
- `n = 10^5` 时 `n(n-1)/2 ≈ 5·10⁹` 次减法与比较。按每秒 `10⁸` 次运算估算 **≈ 50 秒** → **TLE**。
- 它是对的，但它**在做重复劳动**：外层每次固定一个买点，内层都要把后面的所有价格看一遍。

### 第二步：卡点在哪 —— 「内层循环求的是什么」

把内层改写一下：

```text
best = max over i of ( max{prices[j] : j > i} − prices[i] )
```

也就是说，内层其实在求「**从 `i` 之后的最大值**」。这引出一个 O(1) 可以维护的量：**后缀最大值**。或者反过来，换一种枚举方式：

```text
best = max over j of ( prices[j] − min{prices[i] : i < j} )
```

内层变成「**`j` 之前的最小值**」—— 而这是一个**前缀量**，边扫边维护只需 O(1)！

> 🔑 **卡点不是「想不出公式」，而是「枚举顺序选错了」**。按「买点」枚举要前缀最大值之后的信息（后缀）；按「卖点」枚举只需要前缀信息 —— **同一个问题，换个枚举方向就从 `O(n²)` 变成 `O(n)`。**

### 第三步：灵感的来源 —— 前缀最小值

```text
minPrice = +∞                       # 历史最低买点
best     = 0                        # 目前最好的利润
for p in prices:
    minPrice = min(minPrice, p)     # 更新历史最低（买点候选）
    best     = max(best, p - minPrice)   # 假设今天卖，买入点取历史最低
```

**顺序不能反**：先更新 `minPrice` 再算 `p - minPrice`，等价于允许「今天买今天卖」（利润 0，无害）；如果先算 `best` 再更新 `minPrice`，则严格禁止同日买卖。**两种写法在这道题上答案一样**，因为同日交易的利润是 0，不会让答案变大。但**先更新更简洁**，且与 122 题的「同宽降序」一样，属于「用一个顺序把非法情形变成无害情形」的技巧。

### 第四步：为什么这一定是全局最优（直观版）

设最优交易是「第 `i` 天买、第 `j` 天卖」（`i < j`），利润 `prices[j] − prices[i]`。

当循环走到第 `j` 天时，`minPrice` 已经看过 `prices[0..j]`，所以 `minPrice ≤ prices[i]`。于是

```text
算出来的候选值 = prices[j] − minPrice ≥ prices[j] − prices[i] = 最优利润
```

也就是说，**算法在 `j` 这一天算出的候选值不会比最优解差**；而它又不可能比最优解更好（因为 `prices[j] − minPrice` 本身就是一个合法的买卖方案）。两边夹住，答案就精确等于最优利润。§127.3.2 会用同样的思路写成严格论证。

### 第五步：另一条路 —— 状态机 DP（为 128 / 129 铺路）

把「持有 / 不持有」当成两个状态：

```text
hold  = 当前「持有股票」时手上的最大现金（买入要花钱，所以是负数）
free  = 当前「不持有股票」时手上的最大现金（也就是已实现利润）
每读一天价格 p：
    hold = max(hold, -p)          # 继续持有，或今天买入（只能买一次 ⇒ 直接是 -p）
    free = max(free, hold + p)    # 继续不持有，或今天卖出
答案 = free
```

这两个变量与「前缀最小值」完全等价（`hold = −minPrice`），但它有一个巨大优势：**加一个维度就能表达「多次交易」「k 次交易」「冷冻期」「手续费」**。§127.3.3 会给出扩展表。

---

## 127.3 原理拆解

### 127.3.1 数据结构与算法选型

| 方案 | 时间 | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- |
| 枚举买卖点 | `O(n²)` | `O(1)` | 8 行 | `n ≥ 10^4` 就开始卡，`10^5` 必 TLE |
| **前缀最小值贪心（一趟扫描）** ✅ | **`O(n)`** | **`O(1)`** | **6 行** | 只能解决「一笔交易」；多笔交易时「贪心」失效（要 DP） |
| 状态机 DP（`hold` / `free`） | `O(n)` | `O(1)` | 8 行 | 本题同样优；但**语义更重**，简单题里稍显绕 |
| 后缀最大值 + 正向扫描 | `O(n)` | `O(n)` | 10 行 | 需要额外数组；与「前缀最小」对偶但更费空间 |
| 分治（求出左半最优、右半最优、跨中点最优） | `O(n log n)` | `O(log n)` | 25 行 | 常数大；但**它是 110「最大子数组和」分治解的亲戚**，可作思维练习 |
| 转成「差分数组的最大子段和」（Kadane） | `O(n)` | `O(1)` | 8 行 | 需要构造差分数组；思路漂亮但不必要 |

**为什么最终选「前缀最小值」**：`O(n)`、`O(1)`、6 行，且证明极其干净（§127.3.2）。状态机版虽然同样优，但它是**为后续章节准备的通用语言** —— 面试时先写前缀最小，再补一句「这也可以写成 hold/free 的状态机，扩展到 k 次交易」，是最稳的答法。

### 127.3.2 为什么这个做法是对的（正确性论证）

**命题**：算法返回 `max(0, max_{i<j}(prices[j] − prices[i]))`。

**（1）算法不会返回非法值。** 循环第 `j` 轮里，`minPrice` 是 `prices[0..j]` 的最小值，设它在下标 `k` 处取到（`k ≤ j`）。于是候选值 `prices[j] − minPrice = prices[j] − prices[k]` 是一个**合法的单笔交易利润**（若 `k == j` 则利润为 0，仍合法）。取 `best = max(best, 候选)` 且初值为 0，所以最终 `best` 一定 ≤ 最优利润。

**（2）算法不会漏掉最优值。** 设最优交易是 `(i, j)`，`i < j`。走到第 `j` 轮时，`minPrice` 已包含 `prices[i]`，因此 `minPrice ≤ prices[i]`，候选值
`prices[j] − minPrice ≥ prices[j] − prices[i] = 最优利润`。
结合 (1)，该轮候选值**恰好等于**最优利润（否则会与 (1) 矛盾）。所以 `best ≥ 最优利润`。

**（3）两边夹逼** ⇒ `best = 最优利润`。∎

**（4）为什么不能「先找全局最小值再找它后面的最大值」？** 因为全局最小值可能出现在最后一天（反例 ④ `[2,4,1]`）。算法必须保证「买点卖点的先后」这一约束，而**前缀结构天然满足它** —— 这正是「枚举卖点 + 前缀最小」胜过「枚举全局极值」的原因。

> ⚠️ **注意 (2) 里用到的一个细节**：`minPrice` 是在**更新之后**才参与计算的（先 `min` 再算差值）。如果写成「先算差值再更新」，第 `j` 轮用的是 `prices[0..j-1]` 的最小值，此时 `minPrice ≤ prices[i]` 依然成立（因为 `i < j`），结论不变。**两种写法都正确，但理由不同**：前者允许同日交易（利润 0），后者禁止。这一点在 122 题（排序方向）和 309（冷冻期）里会变成「必须选对顺序」的硬约束。

### 127.3.3 通用解题思路（可迁移的那一层）

这道题是 **「前缀极值 + 一次扫描」** 模型的母题，它有一个极其清晰的抽象：

```text
原始：max over (i<j) of ( a[j] − a[i] )       ← O(n²)
      ↓ 固定右端点 j，把「与 j 有关的部分」提出来
改写：max over j of ( a[j] − min{a[0..j-1]} )  ← 括号里是前缀量，可 O(1) 维护
      ↓ 边扫边维护前缀量
实现：O(n) 时间、O(1) 空间
```

**同模型的其他题**：

| 题 | 被求的式子 | 前缀量是什么 |
| --- | --- | --- |
| [LC 121 买卖股票](https://leetcode.cn/problems/best-time-to-buy-and-sell-stock/)（本题） | `max(a[j] − a[i])` | 前缀**最小**值 |
| [LC 53 最大子数组和](https://leetcode.cn/problems/maximum-subarray/) | `max(前缀和差)` | 前缀和的**最小**值 |
| [LC 152 乘积最大子数组](https://leetcode.cn/problems/maximum-product-subarray/) | `max(前缀积比)` | 前缀积的**最大 / 最小**（负数要两个） |
| [LC 42 接雨水](https://leetcode.cn/problems/trapping-rain-water/) | `Σ min(左max, 右max) − h[i]` | 前缀**最大**值 + 后缀最大值 |
| [LC 1014 最佳观光组合](https://leetcode.cn/problems/best-sightseeing-pair/) | `max(a[i] + a[j] + i − j)` | 前缀的 `max(a[i] + i)` |

> 🔑 **一句话**：**「两两配对求最值」先问「固定一端之后，另一端要的是前缀/后缀的什么量」**。只要那个量能 O(1) 更新，`O(n²)` 就变 `O(n)`。

**股票系列的状态机总表**（记住这张表，六道题一起解决）：

| 状态 | 含义 | 转移 |
| --- | --- | --- |
| `hold` | 手上有股票 | `hold = max(hold, free_prev − p)`（买入） |
| `free` | 手上没股票、可买 | `free = max(free, hold_prev + p)`（卖出） |
| `sold`（309 需要） | 今天刚卖出 → 明天冷冻 | `sold = hold_prev + p`，且 `free` 从 `sold` 隔一天回来 |

| 题 | 交易次数 | 额外约束 | 状态数 |
| --- | --- | --- | --- |
| [LC 121](https://leetcode.cn/problems/best-time-to-buy-and-sell-stock/)（本题） | 1 | — | 2 |
| [LC 122](https://leetcode.cn/problems/best-time-to-buy-and-sell-stock-ii/) | 无限 | — | 2 |
| [LC 123](https://leetcode.cn/problems/best-time-to-buy-and-sell-stock-iii/) | 2 | — | `2×(k+1)` |
| [LC 188](https://leetcode.cn/problems/best-time-to-buy-and-sell-stock-iv/) | k | — | `2×(k+1)` |
| [LC 309](https://leetcode.cn/problems/best-time-to-buy-and-sell-stock-with-cooldown/) | 无限 | 卖出后冷冻 1 天 | 3 |
| [LC 714](https://leetcode.cn/problems/best-time-to-buy-and-sell-stock-with-transaction-fee/) | 无限 | 每笔手续费 | 2 |

### 127.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| 允许**多次**交易（LC 122） | 「只能买一次」的约束解除 | `hold = max(hold, free − p)`（用 `free` 而不是 `-p`）；答案仍是 `free` |
| **最多 k 次**交易（LC 123 / 188） | 加一维「已用几次」 | `dp[c][0/1]` 状态机，`O(nk)`；见 [`128-best-time-to-buy-and-sell-stock-iii.md`](128-best-time-to-buy-and-sell-stock-iii.md) |
| 卖出后**冷冻 1 天**（LC 309） | 多一个状态 | 三状态 `hold / sold / rest`；见 [`129-best-time-to-buy-and-sell-stock-with-cooldown.md`](129-best-time-to-buy-and-sell-stock-with-cooldown.md) |
| 每笔**手续费** `fee`（LC 714） | 卖出时扣 | `free = max(free, hold + p − fee)`，其余不变 |
| 允许**做空**（先卖后买） | 状态翻倍 | 增加 `short` 状态：`short = max(short, free + p)`、`free = max(free, short − p)` |
| 要**输出交易方案**（哪天买哪天卖） | 从值变方案 | 记录 `minPrice` 更新时的下标与卖出时的下标；状态机版则记录转移来源 |

---

## 127.4 图解推演

### 127.4.1 一趟扫描的执行轨迹（`prices = [7,1,5,3,6,4]`）

```text
初值：minPrice = +∞, best = 0

第 0 天 p=7:  minPrice = min(∞, 7) = 7      best = max(0, 7-7)  = 0
第 1 天 p=1:  minPrice = min(7, 1) = 1      best = max(0, 1-1)  = 0
第 2 天 p=5:  minPrice = min(1, 5) = 1      best = max(0, 5-1)  = 4   ← 1 买 5 卖
第 3 天 p=3:  minPrice = min(1, 3) = 1      best = max(4, 3-1)  = 4
第 4 天 p=6:  minPrice = min(1, 6) = 1      best = max(4, 6-1)  = 5   ← 1 买 6 卖（最优）
第 5 天 p=4:  minPrice = min(1, 4) = 1      best = max(5, 4-1)  = 5

答案 best = 5
```

表格化（每一轮的两个量）：

| 天 `i` | `prices[i]` | 更新后 `minPrice` | `prices[i] − minPrice` | `best` | 备注 |
| --- | --- | --- | --- | --- | --- |
| 0 | 7 | 7 | 0 | 0 | 当天买当天卖，利润 0 |
| 1 | 1 | **1** | 0 | 0 | 最低价刷新 |
| 2 | 5 | 1 | 4 | 4 | 1 买 5 卖 |
| 3 | 3 | 1 | 2 | 4 | 不如 4，`best` 不变 |
| 4 | 6 | 1 | **5** | **5** | 1 买 6 卖，最优 |
| 5 | 4 | 1 | 3 | 5 | 后面没有更高的了 |

### 127.4.2 反例 `[2,4,1]` 的两种错误画法

```text
✅ 正确（前缀最小值）：
   p=2: minPrice=2, best=max(0,0)=0
   p=4: minPrice=2, best=max(0,2)=2      ← 2 买 4 卖
   p=1: minPrice=1, best=max(2,0)=2      ← 1 出现在最后，只能「往后」卖，无效
   答案 2 ✅

❌ 错误（先找全局最小、再找它后面的最大）：
   全局最小 = 1（下标 2，最后一天）
   它后面没有元素 → 答 0 ❌
```

```text
       2     4     1
       ▲     ▲     ▲
       │     │     └── 全局最小在这里，但它后面没有「卖点」
       └─────┘
       唯一合法的买点必须在卖点左侧 ⇒ 前缀结构天然保证这一点
```

### 127.4.3 两条路线对比

```text
                  ┌──────── prices[0..n-1] ────────┐
                  │                                │
      ┌───────────▼───────────┐        ┌───────────▼────────────┐
      │ 路线 A：前缀最小值贪心  │        │ 路线 B：状态机 DP        │
      │ O(n) / O(1) / 6 行    │        │ O(n) / O(1) / 8 行      │
      └───────────┬───────────┘        └───────────┬────────────┘
                  │                                │
        minPrice = min(minPrice, p)        hold = max(hold, -p)
        best     = max(best, p-minPrice)   free = max(free, hold+p)
                  │                                │
        ✅ 最短、证明最干净                ✅ 加一维就能处理 k 次/冷冻/手续费
                  │                                │
          【本题首选】                    【128/129/188 必须用这个】
```

> 🔑 **两版的等价关系**：`hold = −minPrice`。因为本题只允许买一次，`hold` 的候选永远是 `-p`（不依赖之前的 `free`）；而 `minPrice` 就是「所有已见价格的最小值」。一旦允许多次交易，`hold` 就变成 `free − p`，**与 `minPrice` 的对应关系断开** —— 这就是 122（多次交易）必须换 DP 的原因。

---

## 127.5 双语言实现

### 127.5.1 Java

```java
class Solution {
    public int maxProfit(int[] prices) {
        // minPrice 的初值是「无穷大」而不是 0：
        // 价格可以是 0（约束 0 <= prices[i]），若初值取 0 会错失「0 是最低买点」的情形
        int minPrice = Integer.MAX_VALUE;
        int best = 0;   // 无利润时返回 0，所以初值取 0（而不是 -1 或 -inf）

        for (int p : prices) {
            if (p < minPrice) {
                minPrice = p;           // 刷新历史最低买点（此时今天不可能是卖点）
            } else if (p - minPrice > best) {
                best = p - minPrice;    // 今天卖、买点取历史最低
            }
        }
        return best;
    }
}
```

### 127.5.2 Go

```go
package main

import "math"

func maxProfit(prices []int) int {
	// 与 Java 版逐行同构。Go 里没有 Integer.MAX_VALUE，用 math.MaxInt32（或 1<<62）
	minPrice := math.MaxInt32
	best := 0

	for _, p := range prices {
		if p < minPrice {
			minPrice = p
		} else if p-minPrice > best {
			best = p - minPrice
		}
	}
	return best
}
```

### 127.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 「无穷大」怎么写 | `Integer.MAX_VALUE` | `math.MaxInt32` / `math.MaxInt64` / `1<<62` | Go 的 `math.MaxInt32` 是**无类型常量**，赋给 `int` 变量在 32 位平台上会溢出 —— 生产代码用 `math.MaxInt64` 或直接 `prices[0]` |
| 初值替代方案 | `int minPrice = prices.length > 0 ? prices[0] : 0;` | 同 | 更稳：完全不依赖哨兵，但需要特判空数组（本题 `n ≥ 1`） |
| 循环取值 | `for (int p : prices)` | `for _, p := range prices` | 一致；Go 的 `_` 丢弃下标，避免「声明未使用」 |
| 空数组 | `for` 不执行，返回 `best = 0` | 同 | 两边都天然正确（本题保证非空） |
| 溢出 | 价格 `≤ 10^4`，差值 `≤ 10^4`，`int` 安全 | 同 | 若值域到 `10^9`，Java 需 `long`（`p - minPrice` 最坏 `2·10^9` 仍在 int 内，但保险起见用 `long`） |

### 127.5.4 对拍测试（写完必须跑，这是本书的验收习惯）

```go
package main

import (
	"math/rand"
	"testing"
)

// 暴力参考：枚举所有 (买, 卖) 组合，O(n^2)
func maxProfitBrute(prices []int) int {
	best := 0
	for i := 0; i < len(prices); i++ {
		for j := i + 1; j < len(prices); j++ {
			if d := prices[j] - prices[i]; d > best {
				best = d
			}
		}
	}
	return best
}

// 状态机参考：与贪心版完全不同的建模（hold / free 两状态）
func maxProfitDP(prices []int) int {
	const neg = -1 << 60
	hold, free := neg, 0
	for _, p := range prices {
		// 只用旧值计算，避免「同一天既买又卖」污染；本题同日交易无害，但写成两行更清晰
		newHold := hold
		if -p > newHold {
			newHold = -p // 只允许买一次 ⇒ 买点候选永远是 -p
		}
		newFree := free
		if hold+p > newFree {
			newFree = hold + p // 用【上一轮】的 hold 卖出
		}
		hold, free = newHold, newFree
	}
	return free
}

func TestMaxProfit(t *testing.T) {
	edges := [][]int{
		{1},                // 只有一天 → 0
		{5, 5, 5},          // 全相同 → 0
		{7, 6, 4, 3, 1},    // 单调递减 → 0
		{1, 2},             // 最小可交易 → 1
		{2, 4, 1},          // 最小点在最右 → 2
		{7, 1, 5, 3, 6, 4}, // 题面样例 → 5
		{0, 10000},         // 值域边界 → 10000
		{10000, 0},         // 反向 → 0
		{3, 3, 5, 0, 0, 3, 1, 4}, // 多个波段，但只能吃一个 → 4
	}
	for _, a := range edges {
		want := maxProfitBrute(a)
		if got := maxProfit(a); got != want {
			t.Fatalf("edge %v got=%d want=%d", a, got, want)
		}
		if got := maxProfitDP(a); got != want {
			t.Fatalf("dp edge %v got=%d want=%d", a, got, want)
		}
	}

	r := rand.New(rand.NewSource(121121))
	for iter := 0; iter < 20000; iter++ {
		n := 1 + r.Intn(12)
		a := make([]int, n)
		for i := range a {
			// 值域压到 0..8：等值多、锯齿多，容易暴露「同日买卖」与「初值」问题
			a[i] = r.Intn(9)
		}
		want := maxProfitBrute(a)
		if got := maxProfit(a); got != want {
			t.Fatalf("iter=%d a=%v got=%d want=%d", iter, a, got, want)
		}
		if got := maxProfitDP(a); got != want {
			t.Fatalf("dp iter=%d a=%v got=%d want=%d", iter, a, got, want)
		}
	}
}
```

> 📌 **为什么同时测两个实现**：`maxProfit`（前缀最小）与 `maxProfitDP`（状态机）是两套不同的建模，互相验证能查出「模型理解错误」而不只是「实现手误」。而且状态机版正是 128 / 129 的代码骨架，**现在就让它通过测试，后面两章可以直接复用**。

---

## 127.6 复杂度与常见坑

### 127.6.1 复杂度推导（写结论不算本事，要能推）

- **时间 `O(n)`**：一趟循环，每次一次比较 + 至多一次减法 + 一次比较。`n = 10^5` 时约 `3·10^5` 次操作，实测 Java 1 ms、Go 1 ms。
- **空间 `O(1)`**：两个 `int`。**为什么能 O(1)**：转移只依赖「前缀最小值」这一个标量，不依赖任何历史结构。
- **下界**：任何正确算法都必须读一遍 `prices`（否则漏掉某天的信息就可能错过最优），所以 `Ω(n)`。本题达到下界。
- **暴力的定量代价**：`n = 10^5` ⇒ `5·10⁹` 次减法，按每秒 `10⁸` 次约 **50 秒**。这就是「简单题也要讲复杂度」的理由。

### 127.6.2 六个真实踩过的坑

1. **`best` 初值写成 `-1` 或 `-inf`** —— `[7,6,4,3,1]` 会返回负数。题目要求「无利润返回 0」。
2. **`minPrice` 初值取 `prices[0]` 却从 `prices[0]` 开始循环** —— 逻辑上没错（同日交易利润 0），但如果写成「先算差值再更新 `minPrice`」并配合 `minPrice = prices[1]` 之类的特殊初始化，`n = 1` 时会越界。
3. **先找全局最小值再找其后最大值** —— `[2,4,1]` 答 0。**必须用前缀结构保证「买在卖之前」。**
4. **把「多次交易」的写法混进来**（`minPrice` 在卖出后重置）—— `[7,1,5,3,6,4]` 会答 7（两个波段都吃）。本题只允许一笔交易。
5. **`n = 1` 时返回 1 或返回 `prices[0]`** —— 只有一天无法交易，必须返回 0。
6. **用 `Integer.MAX_VALUE` 做 `p - minPrice`** —— 若循环体写成「先算差值再更新 `minPrice`」，第一轮 `p - MAX_VALUE` 会**下溢**（Java 得到 `p + 1` 之类），必须用 `long` 或调整语句顺序。

### 127.6.3 面试官最爱追问的三个问题

**Q1：为什么一次扫描就够？贪心为什么不会错过最优？**

> 「固定卖点 `j`，最优买点一定是 `prices[0..j]` 里的最小值 —— 因为差价 = 卖价 − 买价，卖价固定时买价越小越好。所以维护一个前缀最小值就够了。对最优解 `(i, j)`，扫到 `j` 时 `minPrice ≤ prices[i]`，于是当轮候选值 ≥ 最优利润；又因为候选值本身是一个合法交易，不能超过最优利润，所以恰好相等。」

**Q2：如果允许多次交易（LC 122）呢？**

> 「贪心的『前缀最小』就不够了，要换成状态机：`hold = max(hold, free − p)`、`free = max(free, hold + p)`。关键差别是『买入』的候选从 `-p` 变成 `free − p` —— 也就是说买之前可以已经赚过钱。答案是 `free`。这也解释了为什么 121 的 `hold` 恰好等于 `−minPrice`：只允许买一次时 `free` 恒为 0。」

**Q3：k 次交易怎么做？**

> 「加一维：`dp[c][0]` 表示『已用 c 次额度、手上不持有』的最大现金，`dp[c][1]` 表示『已用 c 次额度、持有』。转移：`dp[c][1] = max(dp[c][1], dp[c-1][0] - p)`、`dp[c][0] = max(dp[c][0], dp[c][1] + p)`，按 c 递增处理即可自动满足『至多 k 次』的单调性。时间 `O(nk)`，空间 `O(k)`。当 `k ≥ n/2` 时退化成『无限次交易』，直接套 122 的贪心/状态机版本反而更快。」

---

## 127.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 122 买卖股票的最佳时机 II](https://leetcode.cn/problems/best-time-to-buy-and-sell-stock-ii/) | 中等 | 无限次交易 ⇒ `hold` 的候选变成 `free − p`；也可用「所有上升段求和」的贪心 |
| [LC 123 买卖股票的最佳时机 III](https://leetcode.cn/problems/best-time-to-buy-and-sell-stock-iii/) | 困难 | 恰好/至多 2 笔 ⇒ 状态机加一维 `c`（见 128） |
| [LC 188 买卖股票的最佳时机 IV](https://leetcode.cn/problems/best-time-to-buy-and-sell-stock-iv/) | 困难 | 至多 k 笔 ⇒ 123 的参数化版本，且要处理 `k ≥ n/2` 的退化 |
| [LC 309 最佳买卖股票时机含冷冻期](https://leetcode.cn/problems/best-time-to-buy-and-sell-stock-with-cooldown/) | 中等 | 卖出后冷冻 1 天 ⇒ 三状态（见 129） |
| [LC 714 买卖股票的最佳时机含手续费](https://leetcode.cn/problems/best-time-to-buy-and-sell-stock-with-transaction-fee/) | 中等 | 卖出扣 `fee` ⇒ 只改一个转移项 |
| [LC 53 最大子数组和](https://leetcode.cn/problems/maximum-subarray/) | 中等 | 「前缀最小 + 当前前缀和」的同一手法，是本题的连续子数组版 |
| [LC 1014 最佳观光组合](https://leetcode.cn/problems/best-sightseeing-pair/) | 中等 | 把 `a[i] + i` 当成前缀量维护，形式与本题几乎一致 |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 122 买卖股票的最佳时机 II**
1. 只允许买一次与允许多次，差别体现在「买入的钱从哪里来」—— 想想 `hold` 的候选应该用哪个量。
2. 有没有一种更简单的等价说法：如果明天比今天贵，就「今天买明天卖」，这会不会漏掉最优？

**LC 714 含手续费**
1. 手续费放在买入还是卖出，会影响答案吗？为什么？
2. 能不能用「每两天之间若有上涨就吃」的贪心？为什么这题不行而 122 行？

**LC 1014 最佳观光组合**
1. 目标式子里，哪一部分只和 `i` 有关、哪一部分只和 `j` 有关？能不能把「只和 `i` 有关」的部分提出成前缀量？
2. 维护的前缀量是 `max(a[i] + i)` 还是 `max(a[i] - i)`？取决于你如何展开式子。

**通用**
- 两两配对求最值：先固定一端，看另一端需要的是**前缀**还是**后缀**的什么量。
- 「持有 / 不持有」这两个状态，是全部股票题的公共骨架；加维度只会变宽，不会变形。

</details>

---

## 附：本章小结（可背走的三句话）

```text
① 固定卖点，买点取前缀最小值 ⇒ O(n) 一趟扫描，best 初值 0（无利润返回 0）
② 等价的状态机：hold = max(hold, -p)、free = max(free, hold + p)，且 hold = -minPrice
③ 一旦允许多次买卖，hold 的候选要换成 free - p —— 这是 122/123/188 的分岔口
```

> **下一章**：[`128-best-time-to-buy-and-sell-stock-iii.md`](128-best-time-to-buy-and-sell-stock-iii.md) —— 把「持有 / 不持有」扩成 `dp[i][k][0/1]` 的完整状态机，并一次推广到 188。
