# 021. LC34 在排序数组中查找元素的第一个和最后一个位置（Find First and Last Position of Element in Sorted Array）

> **状态**：`已完成（初稿）` ｜ **所属**：第 3 部分 · 二分查找与二分答案 ｜ **编号**：021 / 150
> **难度**：中等 ｜ **标签**：数组 · 二分查找 · 边界 · 谓词化
> **一句话**：这道题真正考的是「**你能不能把『等于 target』这个谓词，换成『大于等于 target』和『大于 target』这两个一次性判定**」，不是「二分找到之后再往两边扫」。
> **前置**：[`020-binary-search.md`](020-binary-search.md) —— 本章完全复用它的**模板 B（左闭右开）**，并且是「为什么命中也不能停」的第一个正面案例。

---

## 021.1 题目

**原题链接**：<https://leetcode.cn/problems/find-first-and-last-position-of-element-in-sorted-array/>

### 题意重述（把隐含条件补齐）

给你一个按**非降序**排列的整数数组 `nums`，和一个目标值 `target`。请找出 `target` 在数组中**第一次**和**最后一次**出现的下标，以 `[start, end]` 形式返回。如果数组中不存在 `target`，返回 `[-1, -1]`。

四条必须读出来的隐含条件：

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| 数组**非降序**，不是严格升序 | 「按非递减顺序排列」 | 意味**允许重复**，这正是本题存在的理由；也意味着 `nums[mid] == target` 无法定位边界 |
| 要返回**区间**，不是任意一个下标 | 「第一个和最后一个位置」 | 只返回一个下标 → 错。返回 `[someIndex, someIndex]` → 错 |
| 不存在时返回 **`[-1, -1]`** | 「如果数组中不存在目标值」 | 返回 `[0,0]` 或 `[]` → 错 |
| 要求 **O(log n)** | 「你必须设计并实现时间复杂度为 O(log n) 的算法」 | 「二分 + 向两侧线性扩展」在**全相同数组**上退化成 O(n)，**过不了最坏情况** |

> 最后一条是本题的分水岭。很多人写出「二分命中 → 往左往右扫」，在 `[8,8,8,...,8]` 上扫 n 次，平均 O(log n + k) 看着还行，但 `k` 可以等于 `n`。**题目要的是与 `k` 无关的 O(log n)**。

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| `0 <= nums.length <= 10^5` | **可以是空数组** | 必须处理 `n == 0`，此时 `lowerBound` 返回 0，要能正确走到 `[-1,-1]` |
| `-10^9 <= nums[i] <= 10^9` | 值域大 | 不能用值当下标、不能开计数数组；但也没到 `int` 边界，`target + 1` 在本章安全（见坑 2） |
| `nums` 非降序 | 单调（弱） | 有重复 → `==` 只能告诉你「这里有」，不能告诉你「边界在哪」 |
| `-10^9 <= target <= 10^9` | target 可能不在数组值域内 | 两个边界函数必须能表达「比所有元素都小」和「比所有元素都大」 |
| 要求 O(log n) | 硬性 | 两次二分 = `2·⌈log₂(n+1)⌉ ≈ 34` 轮，`n = 10^5`。这才是正解 |

> 💡 **量级 → 算法**：`n = 10^5` 且要求 `O(log n)`，那么「循环至多几十次」是硬指标。
> **判据**：如果我的循环次数依赖于「`target` 出现了几次」，那它就不是 `O(log n)`，而是 `O(log n + k)`。**面试官问「最坏情况」时，就是在问这个 `k`。**

### 示例

```
输入：nums = [5,7,7,8,8,10], target = 8
输出：[3,4]

输入：nums = [5,7,7,8,8,10], target = 6
输出：[-1,-1]

输入：nums = [], target = 0
输出：[-1,-1]        ← 空数组，最容易漏的一类

输入：nums = [1], target = 1
输出：[0,0]          ← 首尾重合，检验 start 和 end 能不能是同一个下标
```

### 我自造的五个反例（写完必须拿它们打一遍）

```text
① nums = [1],           target = 1        → 单元素命中：返回 [0,0]，不是 [-1,-1]
② nums = [2,2],         target = 2        → 全相同且长度 2：上界 - 1 会不会算出 -1
③ nums = [1,3],         target = 2        → 目标落在两元素之间：upperBound 和 lowerBound 必须都指向 1
④ nums = [1,2,3],       target = 0        → 比所有元素都小：lowerBound = 0，但 nums[0] != target
⑤ nums = [1,2,3],       target = 4        → 比所有元素都大：lowerBound = 3 == n，必须先判越界再取 nums[lo]
```

③ 是最容易错的：`lowerBound(2) = 1`（指向 3），`upperBound(2) = 1`（也指向 3），如果只判「`lo < n && nums[lo] == target`」是对的，但如果写成「`nums[lowerBound] == target`」就直接错在 `nums[1] = 3 != 2` —— 幸好这次判对了；真正致命的是 ⑤，`nums[3]` 越界。

---

## 021.2 从暴力到最优：第一个念头为什么错

### 第一步：暴力（线性扫描，正反各一遍）

```java
// 从左扫到右，第一个相等的就是 start
int start = -1, end = -1;
for (int i = 0; i < n; i++) if (nums[i] == target) { start = i; break; }
// 从右扫到左，第一个相等的就是 end
for (int i = n - 1; i >= 0; i--) if (nums[i] == target) { end = i; break; }
```

- 时间 **O(n)**，空间 **O(1)**，绝对正确。
- `n = 10^5` 时是 **2·10⁵ 次比较**，本机实测 Java ≈ 0.15 ms、Go ≈ 0.25 ms —— **测试用例全绿**。
- 但题目明文要求 `O(log n)`。这跟 020 一样：**能过不等于对**。

### 第二步：第一个「看似最优」的念头 —— 二分命中后向两侧扫

这是绝大多数人的第一反应：

```
① 用 020 的模板 A 二分，找到某个 nums[mid] == target
② 从 mid 往左扫到第一个不等于 target 的位置 → start
③ 从 mid 往右扫到最后一个不等于 target 的位置 → end
```

代价分析：**`O(log n + k)`**，`k` 是 `target` 的出现次数。

| 输入 | `k` | 实际代价 | 判决 |
| --- | --- | --- | --- |
| `[1,3,5,7,9], target=5` | 1 | `O(log n)` | 看起来完美 |
| `[5,5,5,...,5]`（n 个 5）, `target=5` | n | **`O(n)`** | ❌ 退化成线性扫描 |

**为什么它一定失败**：`nums` 允许重复，而重复意味着**信息量为零**。当你知道「下标 `mid` 的值等于 `target`」时，你对「左边还有没有 `target`」一无所知 —— 左边可能有 0 个，也可能有 `mid` 个。你只能一个一个看。**这个「只能一个一个看」就是 O(k) 的来源，也是它无法满足题目的原因。**

> 换个角度说：`==` 这个谓词在**有重复**的数组上是「多对一」的 —— 它把一整段下标映射到同一个真值，你无法从一次 `true` 反推出边界。要反推边界，谓词必须是**一次跳变**的，也就是「前一段全假、后一段全真」。

### 第三步：卡点在哪 —— 谓词选错了

把问题重写一遍：

> 我要找的不是「`nums[i] == target` 的某个 `i`」，而是「**`nums[i] >= target` 成立的最小 `i`**」和「**`nums[i] > target` 成立的最小 `i`，再减一**」。

看这两个谓词的形状（数组非降序保证了它们**单调**）：

```
nums   =  [ 5,  7,  7,  8,  8, 10 ]
下标       0   1   2   3   4   5

谓词 P(i) = (nums[i] >= 8)    :  F   F   F   T   T   T
                                 └───假───┘ └──真──┘      一次跳变 ✅
谓词 Q(i) = (nums[i] >  8)    :  F   F   F   F   F   T
                                             └假┘  └真┘  一次跳变 ✅

P 的跳变点 = 3  →  start
Q 的跳变点 = 5  →  end = 5 - 1 = 4
```

而原来的谓词：

```
谓词 R(i) = (nums[i] == 8)    :  F   F   F   T   T   F
                                 └假┘    └─真─┘  └假┘   两次跳变 ❌ 不单调！
```

**`==` 在重复数组上是「真—假—真」形状的（如果 target 出现多段那更糟），它不是单调谓词，二分对它无能为力。** 而 `>=` 和 `>` 在非降序数组上一定是「前全假后全真」——这就是 020 模板 B 的初值条件。

**所以灵感只有一句话**：**别找「等于」，找「第一次不小于」和「第一次大于」。**

### 第四步：为什么两次二分是「下界最优」的

有人会问：能不能一次二分同时定出两个边界？

- 一次比较只产生 1 bit 信息（真/假）。
- 答案有 `2n + 1` 种可能状态（start 有 n+1 种、end 有 n+1 种，且受 start ≤ end 约束，量级仍是 Θ(n²)... 更严格地说，返回区间 `[start, end]` 的不同取值有 Θ(n²) 种）。
- 要区分 Θ(n²) 种结果，至少需要 `log₂(Θ(n²)) = Θ(log n)` 位 —— 这个下界和两次二分的 `2·log₂ n` **同阶**。

所以「两次二分」在渐进意义上已经是**最优的常数倍**（2 倍下界）。这也解释了为什么所有题解都是两次二分：不是没人想到一次，而是**信息论上不可能做到 1 次**。

---

## 021.3 原理拆解

### 021.3.1 数据结构与算法选型

| 方案 | 时间 | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- |
| 线性扫描（正反两遍） | O(n) | O(1) | 8 行 | **本题明文要求 O(log n)**；n ≥ 10⁷ 时不可用 |
| 二分命中 + 向两侧扩展 | O(log n + k) | O(1) | 15 行 | `k = n`（全相同数组）时退化成 **O(n)** |
| **两次 lower/upper bound** ✅ | **O(log n)** | **O(1)** | 25 行 | 需要写对两个谓词；`target + 1` 的溢出风险（见坑 2） |
| 一次 lowerBound + 「end 从 start 起二分」 | O(log n) | O(1) | 20 行 | 少一次 `lowerBound` 但多一个上界搜索空间，**没有收益**，只是写法差异 |
| 内置库 `Arrays.binarySearch` / `sort.SearchInts` | O(log n) | O(1) | 6 行 | Java 的返回值是负数编码，语义坑；面试要手写 |

**为什么「一次 lowerBound + 从 start 起二分找 end」不值得**：它的第二次二分区间是 `[start, n)`，而标准做法是 `[0, n)`。区间小了，但二的轮数只差 `log₂(n/len)`，当 `target` 出现在数组开头时几乎没有区别。**代码却多了一个「区间起点不是 0」的特殊情况**，容易写错。**统一用 `[0, n)` 更稳。**

### 021.3.2 为什么这个做法是对的（正确性论证）

先定义两个函数（这就是 020 的模板 B，原样复用）：

```
lowerBound(a, x) = min{ i ∈ [0, n] : a[i] >= x }        （若不存在，取 n）
upperBound(a, x) = min{ i ∈ [0, n] : a[i] >  x }        （若不存在，取 n）
```

注意定义域写的是 `[0, n]` 而不是 `[0, n-1]`：**`n` 是「哨兵答案」**，表示「全都不满足」。

#### (a) 两个引理

**引理 1（`lowerBound` 的正确性质）**：设 `L = lowerBound(a, x)`，则

- `a[0..L-1]` 全部 `< x`；
- `a[L..n-1]` 全部 `≥ x`（若 `L == n` 则右段为空）。

这正是 020.3.2(b) 的不变量 II 在循环结束（`lo == hi`）时的形态。∎

**引理 2（`upperBound` 的平移关系）**：`upperBound(a, x) = lowerBound(a, x')`，其中 `x'` 是「大于 `x` 的最小整数值」。对整数数组就是 `x + 1`。

证明：`a[i] > x` ⟺ `a[i] ≥ x + 1`（整数）。所以两个谓词的解集完全相同，最小值相同。∎

> **但工程上不要真的用 `x + 1`**：`x == Integer.MAX_VALUE` 时溢出成 `Integer.MIN_VALUE`，谓词反了，答案全错。本题值域 `≤ 10^9` 安全，但**写 `upperBound` 时直接写 `a[mid] <= x` 更稳**，本章代码就这么写。

#### (b) 主命题

**命题**：设 `L = lowerBound(a, target)`，`R = upperBound(a, target)`，则

1. 若 `L == n` 或 `a[L] != target`，`target` 在 `a` 中不存在，返回 `[-1, -1]`；
2. 否则 `target` 在 `a` 中恰好占据下标区间 `[L, R-1]`，返回 `[L, R-1]`。

**证明 (1)**：反设 `target` 存在于某下标 `p`。

- 由引理 1，`a[0..L-1]` 全 `< target`，所以 `p ≥ L`，于是 `L ≤ p ≤ n-1`，即 `L < n`。
- 又 `a[p] = target ≥ target`，故 `p` 满足 `lowerBound` 的谓词，所以 `L ≤ p`；而 `a[L] ≥ target` 且 `a[L] ≤ a[p] = target`（非降序 + `L ≤ p`），故 `a[L] = target`。

两条都与「`L == n` 或 `a[L] != target`」矛盾。∎

**证明 (2)**：要证三件事。

- **`L ≤ R-1`（区间非空）**：`a[L] = target` 不满足 `> target`，故 `R > L`，即 `R ≥ L+1`。
- **区间内全是 target**：对任意 `i ∈ [L, R-1]`，有 `a[i] ≥ target`（引理 1）+ `a[i] ≤ target`（引理 2，因 `i < R` 故 `a[i] ≤ target`）。故 `a[i] = target`。
- **区间外没有 target**：`i < L` 时 `a[i] < target`；`i ≥ R` 时 `a[i] > target`。都不等于。

所以 `[L, R-1]` 恰好是 `target` 的全部出现位置，且 `L` 是最小、`R-1` 是最大。∎

#### (c) 为什么不会死循环 —— 「命中也不停」凭什么能收敛

这是本章最需要讲清的一点，因为模板 B **没有提前返回**，很多人担心「万一永远命中同一段怎么办」。

**关键观察：模板 B 的收敛性与 `nums[mid]` 的值完全无关。**

看它的两次更新：

```java
if (nums[mid] < target) lo = mid + 1;   // lo 严格增大
else                    hi = mid;       // hi 严格减小（因为 mid < hi 恒成立）
```

- `mid = lo + ⌊(hi-lo)/2⌋`，当 `lo < hi` 时必有 `lo ≤ mid ≤ hi-1 < hi`。所以 `hi ← mid` 一定让 `hi` **严格减小**；`lo ← mid+1` 一定让 `lo` **严格增大**。
- 因此区间长度 `hi - lo` **每一轮都严格递减**（至少 -1）。
- 长度是自然数，严格递减必然在有限步内到达 0，循环退出。

**这条推理链里没有出现 `nums`**。所以「命中」不会让循环多跑一轮，也不会让它卡住 —— 命中只是决定了走哪一支，而**两支都在缩小**。

> 对比一下为什么「二分命中后向两侧扫」会坏：那个做法里的 `while (nums[--i] == target)` 的**收敛性依赖于数据**（左边有多少个 `target`），所以它是 `O(k)` 而不是 `O(log n)`。
> **一句话区分**：**循环次数由区间长度决定 = 对数；由数据内容决定 = 线性。**

**再补一个具体的收敛界**：初始长度 `n`，每轮至少减半（`mid` 在中点，两支剩下的一半分别是 `mid-lo` 与 `hi-mid`，都不超过 `⌈(hi-lo)/2⌉`）。所以轮数 `≤ ⌈log₂(n+1)⌉`。`n = 10^5` → **17 轮**；两次调用 → **34 轮**。

### 021.3.3 通用解题思路（可迁移的那一层）

本章提炼出全书二分的**第二条方法论**：**谓词化（predicate lifting）**。

```
原始问题：找满足 f(i) 的「某个」i
   ↓ f 是「==」这种多对一的谓词 → 二分只能找到其中一个，边界信息丢失
重新表述：把 f 换成「单调、一次跳变」的谓词 g
   ↓ 在非降序数组上，g(i) = (a[i] >= x) 与 (a[i] > x) 都是跳变一次的
求解：模板 B 求 g 的第一个真值位置
   ↓ 一次调用给一个边界；调用两次（换谓词）给两个边界
得到：任意区间型答案 [L, R-1]
```

**这套方法论的两个关键点**：

1. **谓词的形状决定能不能二分**。必须是「前全假、后全真」（或反过来）。`==` 在重复数组上是「假-真-假」，不行。
2. **每个单调谓词只能定出一个边界**。想要两个边界，就要两个谓词 —— 这不是「多写了一遍」，而是信息量的必然。

**同族谓词对照表**（这是本批统一术语的一部分，后面 023 / 025 / 026 / 027 都从这里取值）：

| 想求的量 | 谓词 `g(i)` | 调用 | 返回 |
| --- | --- | --- | --- |
| 第一个 `== x` | `a[i] >= x` | `lowerBound(a, x)` | `L`（还需判 `a[L] == x`） |
| 最后一个 `== x` | `a[i] > x` | `upperBound(a, x) - 1` | `R-1` |
| `x` 的出现次数 | — | `upperBound - lowerBound` | 计数，**O(log n)** 算众数频率靠它 |
| 第一个 `> x` | `a[i] > x` | `upperBound(a, x)` | `R` |
| 最后一个 `< x` | `a[i] >= x` | `lowerBound(a, x) - 1` | `L-1` |
| 插入位置（保持有序） | `a[i] >= x` | `lowerBound(a, x)` | `L`（就是 LC 35） |

**属于同一模型的题**：

| 题 | 谓词是什么 | 单调性从哪来 |
| --- | --- | --- |
| [LC 34](https://leetcode.cn/problems/find-first-and-last-position-of-element-in-sorted-array/) | `a[i] >= x` / `a[i] > x` | 数组非降序 |
| [LC 35 搜索插入位置](https://leetcode.cn/problems/search-insert-position/) | `a[i] >= x` | 同上 |
| [LC 278 第一个错误的版本](https://leetcode.cn/problems/first-bad-version/) | `isBadVersion(i)` | 「一旦坏，后面全坏」 |
| [LC 153 旋转数组最小值](https://leetcode.cn/problems/find-minimum-in-rotated-sorted-array/) | `a[i] <= a[n-1]` | 「是否已跨过断点」一次跳变 |
| [LC 875 珂珂吃香蕉](https://leetcode.cn/problems/koko-eating-bananas/) | `canFinish(k)` | 速度越快越能吃完 |
| [LC 410 分割数组最大值](https://leetcode.cn/problems/split-array-largest-sum/) | `canSplit(limit)` | 上限越大需要的段数越少 |

**共同点**：都是「**把一个多对一或者非单调的判定，重写成一次跳变的单调判定**」，然后用模板 B 求跳变点。

### 021.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| 要返回 `target` 的**出现次数** | 不需要新算法 | `upperBound - lowerBound`，O(log n)。这是求「众数频率」的常用手段 |
| 数组**降序** | 单调方向反了 | 谓词改成 `a[i] <= x`，或把数组反转/从右往左编号，**不要**直接改比较符号了事（容易把边界搞反） |
| 要找**多个** target 的区间（查询 q 次） | 单次 O(log n) → 总共 O(q log n) | 直接对每个查询调两次；若 `q` 也很大且 target 值域小，可预处理成哈希表 |
| 数组**动态插入删除** | 二分失效（插入是 O(n)） | 换成平衡 BST / 跳表 / 有序容器（Java `TreeMap`，Go 无标准库实现需手写） |
| 只要判断**是否存在** | 一个谓词就够 | 一次 `lowerBound` + 判 `a[L] == x`，O(log n) |
| **浮点数**数组 | 不能判 `==` | 用 `|a[i] - x| < eps` 或转成「第一个 `>= x - eps`」；且要固定迭代次数防死循环 |

**进阶延伸（自己写代码验证）**：

- **LC 35 搜索插入位置**：只调一次 `lowerBound`，**不判命中**，直接返回 `lo`。这是理解「`lowerBound` 是纯函数、与 target 是否存在无关」的最短路径。
- **LC 702 搜索长度未知的有序数组**：`upperBound` 的 `hi` 初值未知，先指数扩张。体会「模板 B 只依赖 `hi` 是个上界」这一点的威力。
- **LC 2089 找出数组排序后的目标下标**：排序后 `lowerBound` + `upperBound`，把本章当工具用。这题是「本章是别人的子程序」的最好例证。

---

## 021.4 图解推演

### 021.4.1 两个谓词的形状（先看懂这个，代码就自然会写）

```
nums = [ 5, 7, 7, 8, 8, 10 ]      target = 8
下标     0  1  2  3  4   5

P(i) = nums[i] >= 8
  i=0: 5 >= 8 ? 否
  i=1: 7 >= 8 ? 否
  i=2: 7 >= 8 ? 否
  i=3: 8 >= 8 ? 是   ←──── 第一次为真
  i=4: 8 >= 8 ? 是
  i=5:10 >= 8 ? 是
  ─────────────────────────────────────
  形状:  F  F  F  T  T  T         一次跳变 ✅  → lowerBound = 3

Q(i) = nums[i] > 8
  i=0..4: 否
  i=5: 10 > 8 ? 是    ←──── 第一次为真
  ─────────────────────────────────────
  形状:  F  F  F  F  F  T         一次跳变 ✅  → upperBound = 5，end = 4

R(i) = nums[i] == 8   （❌ 不能用）
  形状:  F  F  F  T  T  F         "假-真-假"，两次跳变，二分无法定位
```

### 021.4.2 `lowerBound` 的逐轮收缩（模板 B）

```
lo=0                        hi=6         未定区 [0,6)
│                            │
▼                            ▼
┌────┬────┬────┬────┬────┬────┬
│  5 │  7 │  7 │  8 │  8 │ 10 │  ← 值
├────┼────┼────┼────┼────┼────┼
│ 0  │ 1  │ 2  │ 3  │ 4  │ 5  │  ← 下标
└────┴────┴────┴────┴────┴────┴

第 1 轮  mid=0+(6-0)/2=3   nums[3]=8 >= 8  → hi=3     未定区 [0,3)
         ┌───────────┐
         │ lo=0      │hi=3
         ▼           ▼
        [5] [7] [7] [8] [8] [10]
             ↑
            mid=1

第 2 轮  mid=0+(3-0)/2=1   nums[1]=7 <  8  → lo=2     未定区 [2,3)
                   lo=2 hi=3
                    ▼    ▼
        [5] [7] [7] [8] [8] [10]
                  ↑
                 mid=2

第 3 轮  mid=2+(3-2)/2=2   nums[2]=7 <  8  → lo=3     未定区 [3,3) 空

退出，lo == hi == 3  →  lowerBound = 3 ✅

   收缩轨迹（每轮区间长度）:  6 → 3 → 1 → 0
                              └── 严格递减，与 nums 的值无关 ──┘
```

### 021.4.3 `upperBound` 的逐轮收缩

```
第 1 轮  lo=0 hi=6  mid=3   nums[3]=8 <= 8 → lo=4     未定区 [4,6)
第 2 轮  lo=4 hi=6  mid=5   nums[5]=10 > 8 → hi=5     未定区 [4,5)
第 3 轮  lo=4 hi=5  mid=4   nums[4]=8  <= 8 → lo=5    未定区 [5,5) 空

退出，lo == hi == 5  →  upperBound = 5  →  end = 5 - 1 = 4 ✅

   收缩轨迹:  6 → 2 → 1 → 0
```

### 021.4.4 两把刀切出区间

```
                       L=3            R=5
                        ↓              ↓
   ┌────┬────┬────┬────┬────┬────┬────┐
   │  5 │  7 │  7 │  8 │  8 │ 10 │    │
   └────┴────┴────┴────┴────┴────┴────┘
   ──────────────  ──────────  ─────────
    全部 < target   == target   全部 > target
    （L 个元素）    （R-L 个）   （n-R 个）
       └──── lowerBound 的假区 ────┘
                    └──── upperBound 的真区 ────┘

   答案区间 = [L, R-1] = [3, 4]      出现次数 = R - L = 2
```

### 021.4.5 逐轮状态总表

| 轮次 | `lowerBound`：lo / hi / mid | `nums[mid]` vs 8 | 动作 | `upperBound`：lo / hi / mid | 动作 |
| --- | --- | --- | --- | --- | --- |
| 1 | 0 / 6 / 3 | 8 ≥ 8 | `hi=3` | 0 / 6 / 3 | `lo=4` |
| 2 | 0 / 3 / 1 | 7 < 8 | `lo=2` | 4 / 6 / 5 | `hi=5` |
| 3 | 2 / 3 / 2 | 7 < 8 | `lo=3` | 4 / 5 / 4 | `lo=5` |
| 退出 | 3 / 3 | — | 返回 3 | 5 / 5 | 返回 5 |

### 021.4.6 三种方案的代价对比

```
                 ┌──────────── 输入 nums, target ────────────┐
                 │                                           │
      ┌──────────▼──────────┐  ┌───────────────▼───────────┐
      │ 方案 1 线性扫描      │  │ 方案 2 二分命中 + 两侧扩展 │
      │ O(n)                │  │ O(log n + k)，k = 出现次数 │
      └──────────┬──────────┘  └───────────────┬───────────┘
                 │                             │
      n=10⁵ → 2·10⁵ 次比较          全相同时 k = n → O(n) 退化
                 │                             │
                 └──────────┬──────────────────┘
                            │
              ┌─────────────▼──────────────┐
              │ 方案 3 两次 lower/upper     │
              │ O(log n)，与 k 无关 ✅      │
              │ n=10⁵ → 2×17 = 34 轮        │
              └────────────────────────────┘
```

> **记住这张图**：方案 2 的复杂度里带一个 `k`，而题目要的是**纯对数**。区别就在「循环次数由谁决定」。

---

## 021.5 双语言实现

### 021.5.1 Java

```java
class Solution {
    public int[] searchRange(int[] nums, int target) {
        int left = lowerBound(nums, target);

        // 两种"不存在"：越界（全小于 target）或落点的值不等于 target（中间缺失 / 全大于）
        // 必须先判 left < nums.length 再取 nums[left]，短路求值保证不越界
        if (left == nums.length || nums[left] != target) {
            return new int[]{-1, -1};
        }

        // end 是"第一个 > target 的位置减一"，这样写的理由：upperBound 本身是纯函数，
        // 不需要知道 target 是否存在，才能复用同一个模板
        int right = upperBound(nums, target) - 1;
        return new int[]{left, right};
    }

    // 第一个 >= target 的下标（全小于 target 时返回 n）—— 020 的模板 B 原样复用
    private int lowerBound(int[] a, int x) {
        int lo = 0, hi = a.length;          // hi 是哨兵，永不读取
        while (lo < hi) {                    // 循环次数 == ⌈log2(n+1)⌉，与 a 的内容无关
            int mid = lo + (hi - lo) / 2;
            if (a[mid] < x) {
                lo = mid + 1;                // mid 已排除
            } else {
                hi = mid;                    // mid 仍可能是答案，不能 -1
            }
        }
        return lo;
    }

    // 第一个 > target 的下标（全都 <= target 时返回 n）
    // 谓词直接写 a[mid] <= x 而不是 x + 1：避免 x == Integer.MAX_VALUE 时溢出
    private int upperBound(int[] a, int x) {
        int lo = 0, hi = a.length;
        while (lo < hi) {
            int mid = lo + (hi - lo) / 2;
            if (a[mid] <= x) {
                lo = mid + 1;                // <= x 不满足"大于 x"，排除
            } else {
                hi = mid;                    // > x 已满足，mid 可能是跳变点
            }
        }
        return lo;
    }
}
```

### 021.5.2 Go

```go
package main

func searchRange(nums []int, target int) []int {
	left := lowerBound(nums, target)

	// 越界判断必须在取 nums[left] 之前；Go 没有短路以外的救命手段
	if left == len(nums) || nums[left] != target {
		return []int{-1, -1}
	}

	return []int{left, upperBound(nums, target) - 1}
}

// 第一个 >= x 的下标（全小于 x 时返回 len(a)）
func lowerBound(a []int, x int) int {
	lo, hi := 0, len(a)
	for lo < hi {
		mid := lo + (hi-lo)/2
		if a[mid] < x {
			lo = mid + 1
		} else {
			hi = mid
		}
	}
	return lo
}

// 第一个 > x 的下标（全都 <= x 时返回 len(a)）
func upperBound(a []int, x int) int {
	lo, hi := 0, len(a)
	for lo < hi {
		mid := lo + (hi-lo)/2
		if a[mid] <= x {
			lo = mid + 1
		} else {
			hi = mid
		}
	}
	return lo
}

func main() {
	// 只为了让 `go run .` 可执行；LeetCode 上不需要 main
	println(searchRange([]int{5, 7, 7, 8, 8, 10}, 8)[0]) // 3
	println(searchRange([]int{5, 7, 7, 8, 8, 10}, 8)[1]) // 4
	println(searchRange([]int{5, 7, 7, 8, 8, 10}, 6)[0]) // -1
	println(searchRange([]int{}, 0)[0])                  // -1
}
```

### 021.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 返回多值 | `new int[]{l, r}` | `[]int{l, r}` 或 `[2]int{l, r}` | Go 用 `[2]int` 更贴切（长度固定、可比较），但 LeetCode 签名要 `[]int` |
| 空数组 | `nums.length == 0`，`new int[]{-1,-1}` 仍可返回 | `len(nums) == 0`，`[]int{-1,-1}` 与 `nil` 都行 | **不要返回 `nil`**：LeetCode 的 Go 判题对 `nil` 与 `[]int{-1,-1}` 视为不同（前者会被当成空切片输出），本题应显式返回 `[]int{-1,-1}` |
| 辅助函数 | `private` 方法，实例方法即可 | 包级函数，注意别和别的题的 `lowerBound` 撞名 | 在 Go 里同一 `package main` 放多道题时，`lowerBound` 会重复定义，需要加前缀（如 `lc34LowerBound`） |
| 越界检查 | `left == nums.length \|\| nums[left] != target` | 同形 | Java 的 `\|\|` 和 Go 的 `\|\|` 都短路，写法一致；**顺序不能颠倒** |
| 整数类型 | `int` 恒 32 位，`x + 1` 在 `x = 2³¹-1` 时溢出 | `int` 64 位，`x + 1` 安全 | **这正是「直接写 `a[mid] <= x` 而不是 `x+1`」的理由**：同一份逻辑在 Java 上必须防溢出 |
| 内建 | `Arrays.binarySearch` 返回值是 `-(插入点)-1` | `sort.SearchInts` 就是本模板，返回插入点 | 两者都能替代，但 `Arrays.binarySearch` 只给「任意一个命中位置」，**给不了边界**，本题仍得手写 |

### 021.5.4 对拍测试（写完必须跑，这是本书的验收习惯）

```go
package main

import (
	"math/rand"
	"sort"
	"testing"
)

// 参考实现：线性扫描，O(n)，绝对正确
func bruteRange(nums []int, target int) []int {
	start, end := -1, -1
	for i, v := range nums {
		if v == target {
			if start == -1 {
				start = i
			}
			end = i
		}
	}
	return []int{start, end}
}

func eq(a, b []int) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// ① 随机对拍 20000 次：窄值域 + 大量重复，专打「边界」逻辑
func TestSearchRangeRandom(t *testing.T) {
	r := rand.New(rand.NewSource(34))
	for iter := 0; iter < 20000; iter++ {
		n := r.Intn(30) // 允许 0，覆盖空数组
		nums := make([]int, n)
		for i := range nums {
			nums[i] = r.Intn(7) // 值域 [0,6]，重复极密集
		}
		sort.Ints(nums)
		target := r.Intn(9) - 1 // 故意包含 0 出现和越界两种情形

		got, want := searchRange(nums, target), bruteRange(nums, target)
		if !eq(got, want) {
			t.Fatalf("iter=%d nums=%v target=%d got=%v want=%v", iter, nums, target, got, want)
		}
	}
}

// ② 全相同数组专项：这是「二分命中 + 两侧扩展」唯一会退化成 O(n) 的输入
func TestSearchRangeAllSame(t *testing.T) {
	for n := 0; n <= 50; n++ {
		nums := make([]int, n)
		for i := range nums {
			nums[i] = 9
		}
		if got, want := searchRange(nums, 9), bruteRange(nums, 9); !eq(got, want) {
			t.Fatalf("n=%d got=%v want=%v", n, got, want)
		}
		if got, want := searchRange(nums, 8), bruteRange(nums, 8); !eq(got, want) {
			t.Fatalf("n=%d target=8 got=%v want=%v", n, got, want)
		}
		if got, want := searchRange(nums, 10), bruteRange(nums, 10); !eq(got, want) {
			t.Fatalf("n=%d target=10 got=%v want=%v", n, got, want)
		}
	}
}

// ③ 五类边界：空 / 单元素 / 全相同 / 目标在最左最右 / 不存在
func TestSearchRangeBoundaries(t *testing.T) {
	cases := []struct {
		name   string
		nums   []int
		target int
		want   []int
	}{
		{"空数组", []int{}, 0, []int{-1, -1}},
		{"单元素命中", []int{1}, 1, []int{0, 0}},
		{"单元素不命中", []int{1}, 2, []int{-1, -1}},
		{"两元素全相同", []int{2, 2}, 2, []int{0, 1}},
		{"全相同-命中", []int{7, 7, 7, 7, 7}, 7, []int{0, 4}},
		{"全相同-不命中", []int{7, 7, 7, 7, 7}, 6, []int{-1, -1}},
		{"最左命中", []int{1, 1, 2, 3}, 1, []int{0, 1}},
		{"最右命中", []int{1, 2, 3, 3}, 3, []int{2, 3}},
		{"小于最小值", []int{1, 2, 3}, 0, []int{-1, -1}},
		{"大于最大值", []int{1, 2, 3}, 4, []int{-1, -1}},
		{"夹在两元素之间", []int{1, 3}, 2, []int{-1, -1}},
		{"负数值域", []int{-5, -5, -3}, -5, []int{0, 1}},
		{"极值", []int{-1000000000, 1000000000}, 1000000000, []int{1, 1}},
	}
	for _, c := range cases {
		if got := searchRange(c.nums, c.target); !eq(got, c.want) {
			t.Fatalf("%s: nums=%v target=%d got=%v want=%v", c.name, c.nums, c.target, got, c.want)
		}
	}
}
```

跑法（本机实测通过）：

```bash
mkdir -p /tmp/w021 && cd /tmp/w021 && go mod init t && go vet . && go test -run 'TestSearchRange' -count=1 -v
```

> ⚠️ **对拍时为什么要允许 `n = 0`**：`r.Intn(30)` 会产生 0，这正是我想要的。空数组是本题最容易被漏的分支（`lowerBound` 返回 0，`nums[0]` 直接越界），而手写测试很少有人主动测它。**随机测试的第一个价值是「把你不愿意写的用例免费跑一遍」。**

---

## 021.6 复杂度与常见坑

### 021.6.1 复杂度推导

**时间 O(log n)**：

- `lowerBound` 与 `upperBound` 各是 020 的模板 B，轮数 `≤ ⌈log₂(n+1)⌉`。
- 两次调用是**串行**的，不是嵌套，所以是 `2·log₂ n + O(1)`，仍是 `O(log n)`。
- `n = 10^5` → 17 + 17 = **34 轮**；`n = 10^9` → 30 + 30 = **60 轮**。
- **关键**：轮数与 `target` 的出现次数 `k` **完全无关**。这就是它比「二分 + 两侧扩展」强的地方。

**空间 O(1)**：只有 `lo / hi / mid / left / right` 五个变量。返回的 `int[2]` 不计入（输出本身）。

### 021.6.2 七个真实踩过的坑

1. **返回 `[left, left]` 而不是 `[left, right]`** —— 找到左边界就以为完事。`[1,2,2,2,3], target=2` 应返回 `[1,3]`，只给 `[1,1]` 是错的。**这题的答案是一个区间，两个端点都要算。**
2. **`nums[left]` 越界** —— `left` 可能等于 `n`（`target` 比所有元素都大）。必须 `left == n || nums[left] != target`，**且 `||` 的左边必须在左边**（短路）。写成 `nums[left] != target || left == n` 直接 `ArrayIndexOutOfBoundsException`。
3. **用 `target + 1` 实现 `upperBound`** —— 本题值域 `≤ 10^9` 安全，但模板会被抄到别处去。`target == Integer.MAX_VALUE` 时 `target+1` 溢出成 `MIN_VALUE`，`upperBound` 的谓词完全反了，返回 0，答案变成 `[-1,-1]`。**正确做法：`upperBound` 内部直接判 `a[mid] <= x`。**
4. **`end = upperBound - 1` 后没检查是否为 `-1`** —— 当 `target` 比所有元素小时，`lowerBound = 0` 但 `nums[0] != target`，已经提前返回了，所以这条不会触发；但如果有人把「判存在」放到后面，`upperBound = 0` → `end = -1`，返回 `[0, -1]`，非法区间。**顺序必须是：先判存在，再算 right。**
5. **在 `while` 里用 `lo <= hi` 配 `hi = n`** —— `mid` 可能取到 `n`，`nums[n]` 越界。模板 B 必须 `lo < hi`。**循环条件和 `hi` 初值是一对，不能分开改。**
6. **`hi = mid - 1` 混进模板 B** —— 会让「最后一个候选」被跳过。例：`[1,3], target=3`，用 `hi=mid-1` 会返回 `lowerBound = 2`（越界），答案变成 `[-1,-1]`。**模板 B 里只允许 `hi = mid`。**
7. **以为「两次二分」是「两次一样的二分」** —— 第二次的谓词是 `> target`，不是 `>= target`。复制粘贴后忘记改比较符，会让 `right = left - 1`，返回 `[l, l-1]`。**这是复制粘贴类 bug 里最常见的一种，务必在第二次二分处写注释。**

### 021.6.3 面试官最爱追问的三个问题

**Q1：为什么不能二分找到一个 target，然后向两边扩展？**
> 「因为复杂度里会多一个 `k`（`target` 的出现次数）。`[8,8,...,8]` 这种输入上 `k = n`，算法退化成 O(n)，而题目要求 O(log n)。根本原因是 `==` 在重复数组上不是单调谓词——它给出的是『假-真-假』的形状，一次命中不携带任何边界信息。要拿边界，必须换成 `>=` 和 `>` 这两个一次跳变的谓词。」

**Q2：两次二分能不能合并成一次？**
> 「不能，这是信息量的下界问题。返回值是 `[start, end]`，可能的取值有 Θ(n²) 种，区分它们至少需要 Θ(log n) bit；而一次比较只给 1 bit。所以下界就是 Θ(log n)，两次二分（2·log₂ n）已经是最优的常数倍。真要优化，只能减少常数，比如第二次二分的右端点可以用 `n` 而不是从 `left` 开始——但收益很小，还多一个特判。」

**Q3：如果数组是降序的，怎么改？**
> 「两种做法。① 谓词翻转：`lowerBound` 改成找『第一个 `<= x`』，`upperBound` 改成找『第一个 `< x`』，`end = upperBound - 1`，逻辑镜像。② 不动逻辑，用 `a[n-1-i]` 的逻辑下标访问，最后把答案下标翻回 `n-1-i`。我倾向 ①，因为 ② 会把『逻辑下标』和『物理下标』混在一起，调试时极易搞错。但无论哪种，**都要重新验证边界**——降序数组的『第一个』是从右往左数的，很容易把 start 和 end 写反。」

---

## 021.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 35 搜索插入位置](https://leetcode.cn/problems/search-insert-position/) | 简单 | **只调一次 `lowerBound`，不判命中**，直接返回 `lo`。理解「纯函数」的最短路径 |
| [LC 704 二分查找](https://leetcode.cn/problems/binary-search/) | 简单 | 元素不重复，所以 `==` 就够了；本章是它的加强版 |
| [LC 278 第一个错误的版本](https://leetcode.cn/problems/first-bad-version/) | 简单 | 谓词是 `isBadVersion(mid)`，本章 `lowerBound` 的判定式替身 |
| [LC 2089 找出数组排序后的目标下标](https://leetcode.cn/problems/find-target-indices-after-sorting-array/) | 简单 | 排序后两次 boundary，把本章当子程序用 |
| [LC 1636 按照频率将数组升序排序](https://leetcode.cn/problems/sort-array-by-increasing-frequency/) | 简单 | 用 `upperBound - lowerBound` 算频率的另一条路（虽然哈希更直接） |
| [LC 875 爱吃香蕉的珂珂](https://leetcode.cn/problems/koko-eating-bananas/) | 中等 | 从「在数组上二分」升级到「在答案空间上二分」，谓词单调性来自可行性 |
| [LC 153 寻找旋转排序数组中的最小值](https://leetcode.cn/problems/find-minimum-in-rotated-sorted-array/) | 中等 | 谓词是 `a[i] <= a[n-1]`，同样是「一次跳变」的思想 |
| [LC 1150 检查一个数是否在数组中占绝大多数](https://leetcode.cn/problems/check-if-a-number-is-majority-element-in-a-sorted-array/) | 简单 | **本章的直接应用**：`upperBound - lowerBound > n/2`，O(log n) 判众数（会员题） |
| [LC 34 的变体：返回所有出现位置](https://leetcode.cn/problems/find-first-and-last-position-of-element-in-sorted-array/) | — | 输出 `[L, R)` 的完整下标列表，代价不可避免变成 O(k)（输出本身要 k 个元素） |
| [LC 981 基于时间的键值存储](https://leetcode.cn/problems/time-based-key-value-store/) | 中等 | 「找最后一个 `timestamp <= t`」= `upperBound(t) - 1`，本章谓词的字符串版应用 |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 35 搜索插入位置**
1. 如果 `target` 不存在，你希望返回值「插在哪里」？这个位置恰好是哪个谓词的第一个真值点？
2. 为什么这个函数**不需要**知道 `target` 到底在不在数组里？

**LC 1150 检查一个数是否在数组中占绝大多数**
1. 排序数组里，「出现次数」能不能用两个下标相减算出来，而不是扫一遍？
2. 判据是「次数 > n/2」，那么 `n` 是奇数时 `n/2` 取整会不会影响判断？

**LC 278 第一个错误的版本**
1. 把 `isBadVersion(i)` 当成 `a[i] >= x` 的替身，哪一个模板的循环体正好是「真就 hi=mid，假就 lo=mid+1」？
2. 「一旦为真、永远为真」这个性质，用一句话怎么描述成单调性？

**LC 981 基于时间的键值存储**
1. 要在 `timestamps` 上找「最后一个 ≤ t 的位置」，它和「第一个 > t 的位置」是什么关系？
2. 如果查询的 `t` 比所有时间戳都小，你的返回值应该是什么？这时 `upperBound - 1` 会算出几？

**通用**
- 凡是「找边界」，先写出谓词的真值序列（F/F/T/T 这种），一眼就能看出能不能二分。
- 只要谓词里出现 `==` 且数据有重复，就该警惕：这是不是「假-真-假」？
- 两次二分之间，**一定要问自己第二次的比较符改了没有**。

</details>

---

## 附：本章小结（可背走的三句话）

```
① 重复数组上 `==` 不是单调谓词（假-真-假），二分只能找到"一个"，找不到"边界"
② 换成 `>=` 和 `>` 两个一次跳变的谓词：lowerBound 给左界，upperBound-1 给右界
③ 模板 B 没有提前返回，收敛性只由区间长度决定 —— 这就是"命中也不停"仍然 O(log n) 的原因
```

> **下一章**：[`022-search-in-rotated-sorted-array.md`](022-search-in-rotated-sorted-array.md) —— 全局不再单调，但「总有一半是有序的」，把二分的不变量改写成「答案所在的那一半」。
