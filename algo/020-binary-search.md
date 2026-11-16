# 020. LC704 二分查找（Binary Search）

> **状态**：`已完成（初稿）` ｜ **所属**：第 3 部分 · 二分查找与二分答案 ｜ **编号**：020 / 150
> **难度**：简单（但它是全书的二分基准章，021–027 全部引用本章的两套模板）｜ **标签**：数组 · 二分查找 · 循环不变量
> **一句话**：这道题真正考的是「**你能不能把『区间里还剩哪些候选』写成一条不变量**」，不是「背 `lo <= hi` 还是 `lo < hi`」。
> **前置**：[`019-minimum-size-subarray-sum.md`](019-minimum-size-subarray-sum.md) —— 滑窗能退（右指针不回退）的前提也是单调性；单调性是第 2、3 部分共用的那条尺子。

---

## 020.1 题目

**原题链接**：<https://leetcode.cn/problems/binary-search/>

### 题意重述（把隐含条件补齐）

给定一个**升序**排列的整型数组 `nums` 和一个整数 `target`，如果 `target` 存在于数组中，返回它的**下标**；否则返回 `-1`。

一句话读完，但这里有四条隐含条件，每一条都直接决定代码怎么写：

| 隐含条件 | 出处 | 违反了会怎样 |
| --- | --- | --- |
| 数组**升序**排列 | 「nums 按升序排列」 | 只要不是有序，「每次排除一半」的推理链就断了，二分退化成瞎猜 |
| 元素**互不相同** | 「nums 中的所有元素是不重复的」 | 若有重复，二分只能保证「找到一个」，不能保证「找到第一个」——那是 021 要额外解决的事 |
| 不存在时返回 **`-1`** | 「否则返回 -1」 | 返回 `lo`（插入位置）是 021 的语义，本题会判错 |
| **必须在 O(log n) 内** | 「你可以假设 nums 中的所有元素是不重复的…请必须使用时间复杂度为 O(log n) 的算法」 | 线性扫描虽然结果对，但不满足题目对复杂度的硬要求 |

> **注意最后一条**：这题和大多数 LeetCode 题不一样，它**明文规定了复杂度**。所以「能过」不等于「对」，O(n) 扫描在这里是**错误答案**，哪怕测试用例全绿。这是本章第一个要建立的纪律：**二分题的正确性包含复杂度**。

### 约束与「量级反推」

| 约束 | 值 | 它对算法的含义 |
| --- | --- | --- |
| `1 <= nums.length <= 10^4` | n 最多 1 万 | 线性扫描 1 万次，本机实测 Java ≈ 0.03 ms、Go ≈ 0.05 ms —— **能过但违规**；二分只要 **⌈log₂(10⁴+1)⌉ = 14 轮** |
| `-10^4 < nums[i], target < 10^4` | 值域很小 | 不会溢出，但**不要依赖这一点**：生产代码里 `lo + hi` 是会溢出的（见 020.6 的坑 1） |
| `nums` 按升序排列 | 单调 | 二分的**唯一前提**。没有它，本章所有论证作废 |
| 元素不重复 | 严格单调 | 所以「找到即答案」，不用处理左右边界 |
| 要求 O(log n) | 硬性 | `n = 10^9` 时也只要 30 轮 —— 这是「量级 → 算法」表里 `10^9 → O(log n)` 那一行的来源 |

> 💡 **量级 → 算法** 的第二条反射：看到「**有序** + 要求在 `10^5` 以上的规模里查找」，先算 `log₂ n`：
> `n = 10^5 → 17`，`n = 10^9 → 30`，`n = 10^18 → 60`。
> **二分的轮数增长得比你想的慢得多**，所以「用 O(log n) 换掉 O(n)」在面试里几乎永远是划算的。

### 示例

```
输入：nums = [-1, 0, 3, 5, 9, 12], target = 9
输出：4
解释：9 出现在下标 4 处

输入：nums = [-1, 0, 3, 5, 9, 12], target = 2
输出：-1
解释：2 不在数组中，返回 -1
```

### 我自造的四个反例（写完必须拿它们打一遍）

```text
① nums = [5],        target = 5      → 单元素命中：lo == hi == 0 时循环体必须还能进一次
                                      （写 while (lo < hi) 的人在这里直接返回 -1）
② nums = [5],        target = -5     → 单元素不命中：循环体进一次后必须干净退出，不能死循环
③ nums = [1,2,3,4],  target = 4      → 目标在最右端：检验 hi = n-1 还是 hi = n
④ nums = [1,2,3,4],  target = 0      → 目标在最左端之外：检验 lo = mid+1 会不会把 lo 推成 -1
                                      （不会，因为 lo 只会变大；但返回值语义要看清）
```

其中 ① 是最狠的：`while (lo < hi)` 配上 `hi = n-1`，在 `n = 1` 时循环体一次都不执行，`target` 明明命中却返回 `-1`。**这种 bug 不会在小样例上暴露，只会在「数组长度 1」时暴露**，而面试官恰恰爱试这个。

---

## 020.2 从暴力到最优：第一个念头为什么错

### 第一步：暴力（先写出来，别急着优化）

```java
// 暴力：从左到右扫一遍，第一个相等就返回
for (int i = 0; i < nums.length; i++)
    if (nums[i] == target) return i;
return -1;
```

- 时间 **O(n)**，空间 **O(1)**。
- 在 `n = 10^4` 时是 **10⁴ 次比较**，本机实测 Java ≈ 0.03 ms、Go ≈ 0.05 ms —— **测试用例全绿**。
- 但题目明文要求 `O(log n)`。把 `n` 换成 `10^9`（一个真实的大表），线性扫描是 **10⁹ 次比较 ≈ 3 秒**，而二分是 **30 次**。**30 次 vs 10 亿次，这就是二分存在的理由。**

### 第二步：卡点在哪 —— 「有序」这个信息被浪费了

暴力只用了两种信息：**下标可以逐个推进**、**`==` 能判等**。

它完全没有用「**升序**」这件事。升序意味着什么？

> 对于任意下标 `m`，只要有 `nums[m] < target`，那么 `nums[0..m]` 里的**每一个**元素都 `< target`（因为升序）。
> 反过来，只要有 `nums[m] > target`，那么 `nums[m..n-1]` 里的**每一个**元素都 `> target`。

**一次比较，能一次性排除掉一整段前缀或一整段后缀。** 暴力是「一次比较排除 1 个」，二分是「一次比较排除一半」。这个差距不是常数级的优化，是**信息获取方式的改变**。

### 第三步：灵感的来源 —— 把「查找」看成决策树

把每一次比较看成一个二叉分支：

```
                  nums[m] ? target
                 /                \
        nums[m] < target      nums[m] > target
        （答案只可能在右半）    （答案只可能在左半）
```

这是一棵**二叉决策树**。树高 `h` 意味着最坏情况下要比较 `h` 次；叶子数最多 `2^h`，而要区分的结果有 `n + 1` 种（n 个命中位置 + 1 个「不存在」）。于是：

```
2^h >= n + 1   =>   h >= log2(n+1)
```

**这同时给出了下界和构造**：任何基于比较的查找算法至少要 `⌈log₂(n+1)⌉` 次比较（信息论下界），而二分恰好达到它。所以二分不只是「一个还不错的做法」，它是**比较模型的渐进最优解**。

### 第四步：真正难的地方不是思路，是「边界怎么写」

思路三行就讲完了，但所有人在写代码时都会卡在同样四个问题上：

| 卡点 | 典型疑问 | 本章的答案 |
| --- | --- | --- |
| `hi` 初始化 | `n-1` 还是 `n`？ | 取决于你用闭区间还是半开区间，**两者都对，但必须配套** |
| 循环条件 | `lo <= hi` 还是 `lo < hi`？ | 由 `hi` 的语义决定：`hi` 是「候选」就 `<=`，`hi` 是「哨兵」就 `<` |
| 区间怎么缩 | `hi = mid` 还是 `hi = mid-1`？ | 看 `mid` 有没有被排除：**排除了就 ±1，没排除就不 ±1** |
| 返回什么 | `lo`、`hi`、`-1`？ | 由不变量决定，退出时 `lo` 和 `hi` 的物理含义各不相同 |

这四问的答案**不能靠记**，只能靠一条不变量推出来。下一节就干这件事。

---

## 020.3 原理拆解

### 020.3.1 数据结构与算法选型

| 方案 | 时间 | 空间 | 代码量 | 什么时候会输 |
| --- | --- | --- | --- | --- |
| 线性扫描 | O(n) | O(1) | 3 行 | **本题明文禁止**；n ≥ 10⁷ 时不可用 |
| 建哈希表再 O(1) 查 | O(n) 建 + O(1) 查 | O(n) | 8 行 | 只查一次时**建表比查表还贵**；本题只查一次 |
| 二分 · **闭区间 `[lo,hi]`** ✅ | O(log n) | O(1) | 8 行 | 需要「第一个 ≥ x」这类边界语义时，返回值的含义要靠额外推导 |
| 二分 · **左闭右开 `[lo,hi)`** ✅ | O(log n) | O(1) | 8 行 | 初学时容易把 `hi = mid` 写成 `hi = mid-1`，导致答案被跳过 |
| 库函数（`Arrays.binarySearch` / `sort.SearchInts`） | O(log n) | O(1) | 1 行 | **面试官要看你自己写**；且 Java 的返回值语义是 `-(插入点)-1`，极易用错 |

**两套模板都保留，是因为它们服务于不同的题型**：

| 场景 | 推荐模板 | 原因 |
| --- | --- | --- |
| 「存在返回下标，不存在返回 -1」 | 闭区间 `[lo, hi]` | 命中就 `return mid`，中途退出，最直观 |
| 「第一个 ≥ target 的位置」（lower_bound） | 左闭右开 `[lo, hi)` | 循环结束 `lo == hi` 就是答案，不需要判断是否命中 |
| 「最后一个 ≤ target 的位置」（upper_bound 变体） | 左闭右开 `[lo, hi)` | 转成 `lower_bound(target+1) - 1`，或镜像写法 |
| 「最小化 x 使 check(x) 成立」（二分答案） | 左闭右开 / 闭区间均可 | 见 025、026，判定的单调性才是主角 |

> **一句话选型**：**要找「某个具体值」用闭区间；要找「某个分界点」用左闭右开。** 这条规则在 021（找左右边界）、023（找最小值）、025（二分答案）里会反复用到。

### 020.3.2 为什么这个做法是对的（正确性论证）

#### (a) 闭区间模板的循环不变量

```java
int lo = 0, hi = n - 1;
while (lo <= hi) {
    int mid = lo + (hi - lo) / 2;
    if (nums[mid] == target) return mid;
    else if (nums[mid] < target) lo = mid + 1;
    else hi = mid - 1;
}
return -1;
```

**不变量 I（候选不丢失）**：

> 在每一轮循环开始时，若 `target` 存在于 `nums` 中，则它的下标必在 `[lo, hi]` 内。

**初始成立**：`[lo, hi] = [0, n-1]` 覆盖全部下标。∎

**保持**：设本轮 `mid = lo + (hi-lo)/2`，则 `lo ≤ mid ≤ hi`（因为 `lo ≤ hi` 且下取整不会越界）。

- 若 `nums[mid] < target`：由升序，`nums[0..mid]` 全部 `< target`，故答案（若存在）只能在 `mid+1 .. hi`，而代码令 `lo = mid+1`，新区间正是它。✔
- 若 `nums[mid] > target`：由升序，`nums[mid..n-1]` 全部 `> target`，故答案只能在 `lo .. mid-1`，而代码令 `hi = mid-1`，新区间正是它。✔
- 若 `nums[mid] == target`：直接返回，正确。✔

三种情况都不丢候选，不变量保持。∎

**终止**：循环条件 `lo ≤ hi` 失效时 `lo = hi + 1`，区间为空。由不变量，若 `target` 存在则其下标在空区间内 —— 矛盾。故 `target` 不存在，返回 `-1` 正确。∎

> 注意这里的巧妙之处：**「返回 -1 正确」不是靠额外判断，而是靠不变量在区间为空时自然导出**。这就是为什么不变量比代码注释值钱。

#### (b) 左闭右开模板的循环不变量

```java
int lo = 0, hi = n;
while (lo < hi) {
    int mid = lo + (hi - lo) / 2;
    if (nums[mid] < target) lo = mid + 1;
    else hi = mid;
}
return lo;   // 第一个 >= target 的下标（可能 == n，表示全都 < target）
```

**不变量 II（分界性质）**：

> 在每一轮循环开始时，`nums[0..lo-1]` 全部 `< target`，且 `nums[hi..n-1]` 全部 `≥ target`。

**初始成立**：`lo = 0` 使左段为空，`hi = n` 使右段为空，两条全称命题在空集上真。∎

**保持**：`mid = lo + (hi-lo)/2`，由 `lo < hi` 得 `lo ≤ mid < hi`，所以 `mid` 一定落在「未定区」`[lo, hi)` 内。

- `nums[mid] < target` → `nums[0..mid]` 全部 `< target`，令 `lo = mid+1`，左段扩到 `[0, mid]`，仍全部 `< target`。✔
- `nums[mid] ≥ target` → `nums[mid..n-1]` 全部 `≥ target`，令 `hi = mid`，右段扩到 `[mid, n-1]`。✔

**终止**：`lo == hi` 时未定区为空，由不变量：下标 `< lo` 的都 `< target`，下标 `≥ hi = lo` 的都 `≥ target`。所以 `lo` 恰好是**第一个 `≥ target` 的位置**。∎

#### (c) 为什么不会死循环 —— 本章最重要的一段论证

死循环只会来自「某一轮区间没有真的变小」。把「区间长度」当成势函数（potential），逐模板检查：

**闭区间模板**：长度 `L = hi - lo`。

- 当 `lo < hi` 时，`mid = lo + ⌊(hi-lo)/2⌋` 满足 `lo < mid < hi`（因为 `hi - lo ≥ 2` 时 `(hi-lo)/2 ≥ 1`）。两个分支：`lo ← mid+1 ≥ lo+1`，或 `hi ← mid-1 ≤ hi-1`。无论哪支，`L` 至少减 1。**严格递减。**
- 当 `lo == hi` 时，`mid == lo == hi`，`nums[mid] == target` 就返回；否则 `lo ← mid+1` 或 `hi ← mid-1`，两种都会让 `lo > hi`，循环退出。**不会卡在 `lo == hi`。**

所以最多 `n` 轮退出。**严格递减**是「不会死循环」的全部理由。∎

**左闭右开模板**：长度 `L = hi - lo`。

- `lo < hi` 时 `mid = lo + ⌊(hi-lo)/2⌋ < hi`（下取整保证 `mid ≤ hi-1`）。两个分支：`lo ← mid+1 > lo`（至少 +1），或 `hi ← mid < hi`（至少 -1）。**严格递减。**
- 长度为 1（`hi = lo+1`）时，`mid = lo`，若走 `hi ← mid` 则 `hi = lo`，长度归零退出；若走 `lo ← mid+1 = lo+1 = hi`，也退出。**不会卡住。**

∎

> ⚠️ **反过来，把这两套模板的更新混用，立刻死循环**。最常见的三种混法：
>
> | 错误写法 | 后果 | 原因 |
> | --- | --- | --- |
> | `while (lo < hi)` + `hi = mid - 1` | **漏答案** | 长度为 1 时循环体不进，最后一个候选没被检查 |
> | `while (lo < hi)` + `lo = mid` | **死循环** | `hi = lo+1` 时 `mid = lo`，`lo = mid` 不变，长度恒为 1 |
> | `while (lo <= hi)` + `hi = mid` | **死循环** | `hi = lo+1` 时 `mid = lo`，若走 `hi = mid` 则 `hi` 只减到 `lo`，`lo ≤ hi` 仍成立 |
>
> **记忆法则（本批统一）**：**`mid` 下取整 ⇒ `lo` 必须 `mid+1`；`hi` 可以 `mid`。** 一旦你写 `lo = mid`，就必须把 `mid` 改成上取整 `lo + (hi-lo+1)/2`，否则必死循环。025 的 `check` 二分里会用到上取整的变体，`020.6` 的坑 4 会再提醒一次。

### 020.3.3 通用解题思路（可迁移的那一层）

本章建立**三套模板**，此后 021–027 一律按名字引用：

```
┌── 模板 A：闭区间 [lo, hi] ──「找具体值」
│   lo, hi = 0, n-1
│   while lo <= hi:
│       mid = lo + (hi-lo)/2
│       if a[mid] == key: return mid
│       elif a[mid] < key: lo = mid+1      # mid 已排除
│       else:              hi = mid-1      # mid 已排除
│   return -1
│
├── 模板 B：左闭右开 [lo, hi) ──「找分界点 / lower_bound」
│   lo, hi = 0, n
│   while lo < hi:
│       mid = lo + (hi-lo)/2
│       if a[mid] < key: lo = mid+1        # mid 已排除
│       else:            hi = mid          # mid 仍是候选
│   return lo                              # 第一个 >= key 的位置
│
└── 模板 C：二分答案 ──「把最优化转成判定」（详见 025 / 026）
    lo, hi = 答案下界, 答案上界            # 闭区间，答案必在 [lo,hi] 内
    while lo < hi:
        mid = lo + (hi-lo)/2
        if check(mid): hi = mid            # mid 可行 → 答案不超过 mid
        else:          lo = mid+1          # mid 不可行 → 答案至少 mid+1
    return lo                              # 最小的可行值
```

**三套模板的关系统一表**（这是全批的术语基准）：

| 项 | A 闭区间 | B 左闭右开 | C 二分答案 |
| --- | --- | --- | --- |
| `lo` 的含义 | 候选区间的左端（含） | 未定区间左端（含） | 答案下界（含） |
| `hi` 的含义 | 候选区间右端（**含**） | 未定区间右端（**不含**，哨兵） | 答案上界（含） |
| 循环条件 | `lo <= hi` | `lo < hi` | `lo < hi` |
| 每次必缩 | `hi-lo` 至少 -1 | `hi-lo` 至少 -1 | `hi-lo` 至少 -1 |
| 提前返回 | 有（命中即 return） | **无** | **无** |
| 退出时 | `lo = hi+1`，区间空 | `lo == hi`，即答案 | `lo == hi`，即答案 |
| 典型题 | 704 / 33 / 240 | 34 / 153 / 27 | 875 / 410 / 1011 |

**为什么模板 C 不带提前返回**：因为它每次只回答「`mid` 可行吗」，而不是「`mid` 是答案吗」。`check(mid)` 为真时我们只知道**存在一个不超过 mid 的答案**，还得继续往左逼近。这个「只能逼近、不能命中」的性质，是二分答案和二分查找最本质的区别，026 会用一张图讲透。

**这套模板能迁移到哪些题**：

| 题 | 用什么模板 | 「单调」的对象是什么 |
| --- | --- | --- |
| [LC 704 二分查找](https://leetcode.cn/problems/binary-search/) | A / B | 数组下标 → 元素值（升序） |
| [LC 34 查找首末位置](https://leetcode.cn/problems/find-first-and-last-position-of-element-in-sorted-array/) | B ×2 | 数组下标 → 元素值（非降序） |
| [LC 33 旋转数组查找](https://leetcode.cn/problems/search-in-rotated-sorted-array/) | A + 局部有序判定 | 两段各自单调 |
| [LC 153 旋转数组最小值](https://leetcode.cn/problems/find-minimum-in-rotated-sorted-array/) | B（与右端比） | 「是否已跨过断点」是 0/1 单调 |
| [LC 875 珂珂吃香蕉](https://leetcode.cn/problems/koko-eating-bananas/) | C | 速度 → 能否吃完（单调） |
| [LC 410 分割数组最大值](https://leetcode.cn/problems/split-array-largest-sum/) | C | 子数组和上限 → 需要的段数（单调递减） |
| [LC 4 两个正序数组的中位数](https://leetcode.cn/problems/median-of-two-sorted-arrays/) | 划分二分 | 划分位置 → 左半最大 ≤ 右半最小 |

**共同点**：都有一个「**随参数单调变化的 0/1 判定**」，二分的任务就是把那条 0/1 边界钉出来。

### 020.3.4 可扩展性（面试追问的六个方向）

| 追问 | 变化 | 应对 |
| --- | --- | --- |
| 数组**有重复元素** | 二分仍能「找到一个」 | 但要找「第一个 / 最后一个」必须用模板 B 两次 → 见 021 |
| 数组**被旋转**过 | 全局不再单调 | 每轮先判 `[lo,mid]` 和 `[mid,hi]` 哪半有序 → 见 022 / 023 |
| **二维**矩阵，行列都升序 | 全局不单调，但沿对角线 / 阶梯单调 | 从右上角走「阶梯搜索」O(m+n)，或对每行二分 O(m log n) → 见 024 |
| 要查的是**答案**不是元素 | 没有数组可索引，但有单调判定 | 二分答案，模板 C → 见 025 / 026 |
| 数组**长度未知**（`ArrayReader` 接口） | 拿不到 `n` | 先「指数搜索」把 `hi` 翻倍到 `2^k ≥ n`，再二分，总代价仍是 O(log n)（LC 702） |
| 数组是**浮点 / 要精度** | 不能用 `==` | 固定迭代 100 次，或 `while (hi - lo > 1e-7)`；不要用 `==` 判等（LC 69 求平方根） |

**进阶延伸（自己写代码验证）**：

- **LC 702 搜索长度未知的有序数组**（会员题）：`hi` 从 1 开始每次翻倍，命中 `reader.get(hi) >= target` 再二分。体会「未知边界 → 指数扩张」这个套路，它和 027 的「短数组上二分」是同一思路的两个方向。
- **LC 69 x 的平方根**：把 `check(m) = m*m <= x` 套模板 C，注意 `m*m` 会溢出，Java 用 `long`。这是「二分答案」最小的练习场。
- **LC 374 猜数字大小**：模板 A 的裸奔版，用 `guess(mid)` 替代 `nums[mid]` 比较，用来练手感。

---

## 020.4 图解推演

### 020.4.1 模板 A（闭区间）的收缩轨迹

目标 `target = 9`，`nums = [-1, 0, 3, 5, 9, 12]`（`n = 6`）：

```
下标   0    1    2    3    4    5
值    -1    0    3    5    9   12
      ─────┬────┬────┬────┬────┬─────
           │    │    │    │    │
第 1 轮  lo=0                    hi=5        mid=0+(5-0)/2=2   nums[2]=3  < 9   → lo=3
              │                   │
第 2 轮       lo=3               hi=5        mid=3+(5-3)/2=4   nums[4]=9  == 9  → 返回 4 ✅
              │    │    │    │    │
              ↑    ↑    ↑    ↑    ↑
              lo   │   mid   │    hi
                   └─────────┘
              排除掉的候选：下标 0,1,2（一次比较排除 3 个）
```

再走一个**不存在**的例子，`target = 8`，看清「区间怎么收缩到空」：

```
下标   0    1    2    3    4    5
值    -1    0    3    5    9   12

第 1 轮  lo=0  hi=5  mid=2  nums[2]=3 < 8  → lo=3       区间 [3,5]
第 2 轮  lo=3  hi=5  mid=4  nums[4]=9 > 8  → hi=3       区间 [3,3]
第 3 轮  lo=3  hi=3  mid=3  nums[3]=5 < 8  → lo=4       区间 [4,3] 空
         └─ lo(4) > hi(3) → 第 4 轮条件不成立 → 返回 -1 ✅

         lo ↓
   ──┬───┬───┬───┬───┬───┬──┐
     │   │   │   │   │   │  │
    -1   0   3   5   9  12
                 ↑
                 hi 与 lo 在此交叉，交叉即"没有候选"
```

### 020.4.2 模板 B（左闭右开）的收缩轨迹

同样的数组，`target = 9`，求「第一个 ≥ 9 的位置」（答案应为 4）：

```
         lo=0                        hi=6        未定区 [0,6)
第 1 轮   mid = 0+(6-0)/2 = 3      nums[3]=5  < 9   → lo=4     未定区 [4,6)
         ↓ 左段 [0,3] 全部 < 9，作废
第 2 轮   lo=4  hi=6   mid = 4+(6-4)/2 = 5        nums[5]=12 ≥ 9   → hi=5   未定区 [4,5)
第 3 轮   lo=4  hi=5   mid = 4+(5-4)/2 = 4        nums[4]=9  ≥ 9   → hi=4   未定区 [4,4) 空
         退出，lo == hi == 4，返回 4 ✅

  ──┬───┬───┬───┬───┬───┬──
    │   │   │   │   │   │
   -1   0   3   5   9  12
    └───────┴───┘   └──┴──┘
      < target       ≥ target
       （lo 推进到这里↑）    ↑（hi 收缩到这里）
```

**注意 `hi` 一开始是 6，不是 5**。它是**哨兵**：`nums[6]` 根本不存在，永远不会被读，但它让「右段为空」这个初始状态有地方可写。这就是「左闭右开」里那个「开」的全部意义。

### 020.4.3 两套模板逐轮状态对照表

| 轮次 | 模板 A：`lo` | `hi` | `mid` | `nums[mid]` | 动作 | 模板 B：`lo` | `hi` | `mid` | 动作 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 0 | 5 | 2 | 3 < 9 | `lo=3` | 0 | 6 | 3 | `lo=4` |
| 2 | 3 | 5 | 4 | 9 == 9 | **return 4** | 4 | 6 | 5 | `hi=5` |
| 3 | — | — | — | — | — | 4 | 5 | 4 | `hi=4` → 退出，`lo=4` |

### 020.4.4 两套模板的路线对比图

```
                     ┌──────────── 输入：有序数组 + target ────────────┐
                     │                                                │
          ┌──────────▼───────────┐                       ┌────────────▼────────────┐
          │ 模板 A 闭区间 [lo,hi] │                       │ 模板 B 左闭右开 [lo,hi)  │
          │ hi = n-1             │                       │ hi = n（哨兵）           │
          │ while lo <= hi       │                       │ while lo < hi            │
          └──────────┬───────────┘                       └────────────┬────────────┘
                     │                                                │
        命中即 return mid（可提前结束）                  不提前返回，靠 lo == hi 收敛
                     │                                                │
        返回 mid 或 -1                                   返回 lo（= 分界点，可能 == n）
                     │                                                │
        ┌────────────▼────────────┐                      ┌────────────▼────────────┐
        │ 适合：704 / 33 / 240    │                      │ 适合：34 / 153 / 4      │
        │ 「值在不在」             │                      │ 「边界在哪」             │
        └─────────────────────────┘                      └─────────────────────────┘
                     │                                                │
                     └──────────────┬─────────────────────────────────┘
                                    │
                     两者都是「区间长度严格递减」
                     ── 这是「不死循环」的唯一保证
```

---

## 020.5 双语言实现

### 020.5.1 Java

```java
class Solution {
    // 模板 A：闭区间 [lo, hi]，命中返回下标，否则 -1
    public int search(int[] nums, int target) {
        int lo = 0, hi = nums.length - 1;      // hi 是"最后一个可能的下标"，参与候选
        while (lo <= hi) {                      // 区间非空才继续（lo == hi 时还有 1 个候选，必须进）
            int mid = lo + (hi - lo) / 2;       // 不写 (lo+hi)/2：lo+hi 可能超出 int 上界
            if (nums[mid] == target) {
                return mid;                     // 只有这一支能提前结束
            } else if (nums[mid] < target) {
                lo = mid + 1;                   // nums[mid] 已排除，新区间 [mid+1, hi]
            } else {
                hi = mid - 1;                   // nums[mid] 已排除，新区间 [lo, mid-1]
            }
        }
        return -1;                              // 区间收缩为空 => 不存在
    }

    // 模板 B：左闭右开 [lo, hi)，返回"第一个 >= target 的下标"（全部 < target 时返回 n）
    // 单独留一个方法，是为了让 021 / 027 可以直接引用它的语义
    public int lowerBound(int[] nums, int target) {
        int lo = 0, hi = nums.length;           // hi 是哨兵：nums[hi] 永不读取
        while (lo < hi) {                       // lo == hi 时未定区为空，退出
            int mid = lo + (hi - lo) / 2;       // 下取整 => mid < hi 恒成立，故 hi = mid 会真的缩小
            if (nums[mid] < target) {
                lo = mid + 1;                   // mid 已排除
            } else {
                hi = mid;                       // mid 仍可能是答案，不能 -1
            }
        }
        return lo;                              // 左段全 < target，右段全 >= target => lo 就是分界
    }
}
```

### 020.5.2 Go

```go
package main

// 模板 A：闭区间 [lo, hi]
func search(nums []int, target int) int {
	lo, hi := 0, len(nums)-1
	for lo <= hi {
		mid := lo + (hi-lo)/2 // Go 的 int 在 64 位平台是 64 位，但仍这么写：换语言时不改习惯
		switch {
		case nums[mid] == target:
			return mid
		case nums[mid] < target:
			lo = mid + 1
		default:
			hi = mid - 1
		}
	}
	return -1
}

// 模板 B：左闭右开 [lo, hi)，返回第一个 >= target 的下标
// Go 标准库的 sort.SearchInts 就是这一套模板，签名 (n int, f func(int) bool) 的 f 是"小于"谓词的反面
func lowerBound(nums []int, target int) int {
	lo, hi := 0, len(nums)
	for lo < hi {
		mid := lo + (hi-lo)/2
		if nums[mid] < target {
			lo = mid + 1
		} else {
			hi = mid
		}
	}
	return lo
}

func main() {
	// 只为让 `go run .` 可执行；LeetCode 上不需要 main
	println(search([]int{-1, 0, 3, 5, 9, 12}, 9))       // 4
	println(search([]int{-1, 0, 3, 5, 9, 12}, 2))       // -1
	println(lowerBound([]int{-1, 0, 3, 5, 9, 12}, 9))   // 4
}
```

### 020.5.3 双语言差异批注

| 点 | Java | Go | 对本题的影响 |
| --- | --- | --- | --- |
| 数组长度 | `nums.length`（字段） | `len(nums)`（内建函数） | Go 里 `len` 是常量时间的，写在循环条件里也不会慢；Java 的 `.length` 同理 |
| 赋值多变量 | 只能 `int lo = 0, hi = n - 1;` | `lo, hi := 0, len(nums)-1` | Go 的 `:=` 一次声明两个，但要小心别在循环里用 `:=` 遮蔽外层变量（那是 020.6 坑 6） |
| 分支写法 | `if / else if / else` | `switch { case ... }` 或 `if` | Go 的 `switch` 无 `break` 落入，写三路比较比 `else if` 链清楚 |
| 整数除法 | `/` 对负数向零取整 | `/` 同样向零取整 | 本题 `lo, hi ≥ 0`，不受影响；但「上取整」`(lo+hi+1)/2` 在负区间行为不同，浮点二分时要注意 |
| 标准库对应 | `Arrays.binarySearch(a, key)`，**返回值 `= -(插入点)-1`** | `sort.SearchInts(a, x)`，返回插入点（即模板 B 的 `lo`） | Java 那个负数编码是高频 bug 源；Go 的直接返回下标，语义更干净。**两者都不是本题答案，面试要手写** |

### 020.5.4 对拍测试（写完必须跑，这是本书的验收习惯）

```go
package main

import (
	"math/rand"
	"sort"
	"testing"
)

// 参考实现：线性扫描。O(n) 但绝对正确，用来当"标准答案"
func linearSearch(nums []int, target int) int {
	for i, v := range nums {
		if v == target {
			return i
		}
	}
	return -1
}

// 参考实现：lowerBound 的暴力版（第一个 >= target 的下标）
func linearLowerBound(nums []int, target int) int {
	for i, v := range nums {
		if v >= target {
			return i
		}
	}
	return len(nums)
}

// ① 随机对拍 20000 次：值与 target 都从窄值域里取，制造大量"命中/不命中"混合情形
func TestSearchRandom(t *testing.T) {
	r := rand.New(rand.NewSource(20240220))
	for iter := 0; iter < 20000; iter++ {
		n := 1 + r.Intn(40)
		nums := make([]int, n)
		for i := range nums {
			nums[i] = r.Intn(61) - 30 // 值域 [-30, 30]，n ≤ 40 必有重复，必须先排序去重
		}
		sort.Ints(nums)
		// 去重：题目保证元素不重复
		uniq := nums[:1]
		for _, v := range nums[1:] {
			if v != uniq[len(uniq)-1] {
				uniq = append(uniq, v)
			}
		}
		nums = uniq

		target := r.Intn(71) - 35 // 故意比值域宽，保证有一半概率落空
		got, want := search(nums, target), linearSearch(nums, target)
		if got != want {
			t.Fatalf("iter=%d nums=%v target=%d got=%d want=%d", iter, nums, target, got, want)
		}
		// 顺手对拍 lowerBound：它的语义是 021 的基石，一起验
		if g, w := lowerBound(nums, target), linearLowerBound(nums, target); g != w {
			t.Fatalf("lowerBound iter=%d nums=%v target=%d got=%d want=%d", iter, nums, target, g, w)
		}
	}
}

// ② 五类边界真跑：空 / 单元素 / 全相同 / 目标在最左最右 / 目标不存在
func TestSearchBoundaries(t *testing.T) {
	type tc struct {
		name   string
		nums   []int
		target int
		want   int
	}
	cases := []tc{
		{"空数组", []int{}, 1, -1},
		{"单元素命中", []int{5}, 5, 0},
		{"单元素不命中-左", []int{5}, 4, -1},
		{"单元素不命中-右", []int{5}, 6, -1},
		{"两元素命中左", []int{1, 2}, 1, 0},
		{"两元素命中右", []int{1, 2}, 2, 1},
		{"全相同-命中", []int{7, 7, 7, 7}, 7, 1}, // 命中下标不唯一，只要求 0..3 内且值相等
		{"全相同-不命中", []int{7, 7, 7, 7}, 8, -1},
		{"最左命中", []int{1, 2, 3, 4, 5}, 1, 0},
		{"最右命中", []int{1, 2, 3, 4, 5}, 5, 4},
		{"小于最小值", []int{1, 2, 3, 4, 5}, 0, -1},
		{"大于最大值", []int{1, 2, 3, 4, 5}, 6, -1},
		{"偶数长度中间偏右", []int{1, 2, 3, 4}, 3, 2},
		{"负数值域", []int{-9, -7, -5}, -7, 1},
	}
	for _, c := range cases {
		got := search(c.nums, c.target)
		if c.name == "全相同-命中" {
			if got < 0 || got >= len(c.nums) || c.nums[got] != c.target {
				t.Fatalf("%s: got=%d", c.name, got)
			}
			continue
		}
		if got != c.want {
			t.Fatalf("%s: nums=%v target=%d got=%d want=%d", c.name, c.nums, c.target, got, c.want)
		}
	}
}
```

跑法（本机实测通过）：

```bash
mkdir -p /tmp/w020 && cd /tmp/w020 && go mod init t && go vet . && go test -run 'TestSearch' -count=1 -v
```

> ⚠️ **「全相同」这一类为什么只验合法性、不验精确下标**：因为题目保证了元素不重复，`[7,7,7,7]` 是**非法输入**。但真实面试里常有人拿它试你的二分——此时任何「值等于 target 的下标」都算对，但如果你把它当成 021（找第一个），就必须返回 0。**这两者的区别，就是 020 和 021 的分界线。**

---

## 020.6 复杂度与常见坑

### 020.6.1 复杂度推导（写结论不算本事，要能推）

**时间 O(log n)**，两种推法都要会：

1. **递推**：设 `T(n)` 为长度 `n` 的区间需要的轮数。每轮比较一次后区间长度最多变成 `⌈n/2⌉`，故
   `T(n) = T(n/2) + O(1)`，解得 `T(n) = O(log n)`。
2. **势函数**：区间长度 `L = hi - lo`，初始 `n-1`，每轮至少减半（因为 `mid` 在区间正中间，两支剩下的一半分别是 `mid-lo` 与 `hi-mid`，都不超过 `⌈L/2⌉`）。所以轮数 `≤ ⌈log₂(n+1)⌉`。本题 `n = 10^4` → **14 轮**；`n = 10^9` → **30 轮**。

**空间 O(1)**：只有 `lo / hi / mid` 三个变量。**这题别写递归版**——递归不会更快，只是把 O(1) 空间变成 O(log n) 栈空间，白送一个劣势。

### 020.6.2 七个真实踩过的坑

1. **`mid = (lo + hi) / 2` 溢出** —— `lo` 和 `hi` 都是 `int` 时，`lo + hi` 可能超过 `2³¹-1`。`n = 1.5·10⁹` 时就炸（Java 会得到负数，然后用负下标访问数组 → `ArrayIndexOutOfBoundsException`）。**必须写 `lo + (hi - lo) / 2`**。Go 的 `int` 在 64 位平台是 64 位，问题被掩盖了，但换到 32 位平台（`GOARCH=386`）或写 `int32` 时就复现。**这个坑在面试里几乎必问。**
2. **`while (lo < hi)` 配 `hi = n - 1`** —— `n == 1` 时循环一次都不进，命中也返回 `-1`。这是本章最高频的错误。修法：要么 `lo <= hi`，要么 `hi = n`，**循环条件和 `hi` 初始化必须配套**。
3. **`hi = mid - 1` 与 `hi = mid` 混用** —— 前者用于闭区间（`mid` 已排除），后者用于半开区间（`mid` 仍是候选）。混用会导致**漏掉最后一个候选**或**死循环**（见 020.3.2(c) 的表）。
4. **`lo = mid` 而 `mid` 用下取整** —— 必定死循环。`hi = lo+1` 时 `mid = lo`，`lo = mid` 让 `lo` 原地踏步。**要用 `lo = mid` 就必须把 `mid` 改成 `lo + (hi-lo+1)/2`（上取整）**。025 的二分答案里会出现这个变体，务必对照。
5. **返回 `lo` 却当成「命中下标」** —— 模板 B 的 `lo` 是**插入点**，`target` 不存在时它是「第一个大于 target 的位置」，且可能是 `n`。拿它直接当下标访问 `nums[lo]` 会越界。**必须再判 `lo < n && nums[lo] == target`**。
6. **Go 里用 `:=` 在循环内遮蔽 `lo`** —— `for lo < hi { mid := ...; lo := mid + 1 }` 这样写编译不过（`lo` 已声明）；但如果在 `if` 块里写 `lo := mid + 1`，会创建一个**块内新变量**，外层 `lo` 不变，于是死循环。**Go 的 `:=` 是「至少声明一个新变量」，遮蔽是静默发生的**，`go vet` 的 `shadow` 检查器能抓到（默认未开启，需要 `-vettool`）。
7. **Java 用 `Arrays.binarySearch` 当答案** —— 它的返回值是「找到则下标，找不到则 `-(插入点) - 1`」。很多人写成 `if (Arrays.binarySearch(a, x) >= 0)`，在 `x` 小于所有元素时 `-(0)-1 = -1`，恰好也被判成「没找到」—— **但 `x` 插在 0 号位之前的正确表达是「没找到」，这个巧合掩盖了 bug；一旦有人写 `> 0` 就全错**。而且面试要的是手写。

### 020.6.3 面试官最爱追问的三个问题

**Q1：为什么 `mid` 要写成 `lo + (hi - lo) / 2`，而不是 `(lo + hi) / 2`？**
> 「防溢出。`lo` 和 `hi` 都是 `int` 时，当数组长度接近 `2³¹`（约 21 亿），`lo + hi` 会溢出成负数，`/2` 之后还是负数，拿去当下标就崩。`hi - lo` 恒 ≤ `n ≤ 2³¹-1`，不会溢出；`lo + (hi-lo)/2` 的中间结果也一定落在 `[lo, hi]` 内。这个 bug 在 JDK 的 `Arrays.binarySearch` 里真实存在了 9 年（2006 年才被 Joshua Bloch 在博客里指出并修复），所以它是「面试官必问」而不是「面试官刁难」。」

**Q2：`while (lo <= hi)` 和 `while (lo < hi)` 到底用哪个？**
> 「不看习惯，看 `hi` 的语义。如果我把 `hi` 定义为『最后一个候选下标』（初始 `n-1`），那么 `lo == hi` 时区间里还有 1 个候选，必须进循环 → `lo <= hi`。如果我把 `hi` 定义为『哨兵，永不被读取』（初始 `n`），那么 `lo == hi` 表示未定区为空，不该进 → `lo < hi`。**这两种写法都正确，但绝不能交叉**：`lo < hi` 配 `hi = n-1` 会漏掉单元素，`lo <= hi` 配 `hi = n` 会让 `mid` 取到 `n` 越界。」

**Q3：这题如果数组有重复元素，你的代码还对吗？要找第一个和最后一个怎么办？**
> 「『找到某一个』仍然对——`nums[mid] == target` 时直接返回，返回的确实是某个等于 `target` 的下标。但『找第一个 / 最后一个』就不行了，因为命中时不能停，必须继续往左（或往右）逼近。做法是用模板 B 跑两次：`lowerBound(target)` 得左边界，`lowerBound(target+1) - 1` 得右边界。注意 `target + 1` 在 `target == Integer.MAX_VALUE` 时会溢出，安全写法是改成 `upperBound`（谓词用 `nums[mid] <= target`）。这就是 021 的内容。」

---

## 020.7 同类题扩展

| 题目 | 难度 | 差异点 |
| --- | --- | --- |
| [LC 34 在排序数组中查找元素的第一个和最后一个位置](https://leetcode.cn/problems/find-first-and-last-position-of-element-in-sorted-array/) | 中等 | 命中不能停，模板 B 跑两次；「为什么要用两次二分而不是一次」是本章的直接延伸 |
| [LC 35 搜索插入位置](https://leetcode.cn/problems/search-insert-position/) | 简单 | **就是模板 B 本身**，返回 `lo`，不需要判是否命中。练模板 B 的最佳入口 |
| [LC 69 x 的平方根](https://leetcode.cn/problems/sqrtx/) | 简单 | 二分答案的最小练习场：`check(m) = m*m <= x`，注意 `long` |
| [LC 33 搜索旋转排序数组](https://leetcode.cn/problems/search-in-rotated-sorted-array/) | 中等 | 全局不单调，但每轮必有一半单调 → 先判哪半有序 |
| [LC 153 寻找旋转排序数组中的最小值](https://leetcode.cn/problems/find-minimum-in-rotated-sorted-array/) | 中等 | `nums[mid]` 与 `nums[hi]` 比较，把「是否跨过断点」变成 0/1 判定 |
| [LC 74 搜索二维矩阵](https://leetcode.cn/problems/search-a-2d-matrix/) | 中等 | 行内有序且下一行首 > 上一行尾 → 摊平成一维后就是 704 |
| [LC 240 搜索二维矩阵 II](https://leetcode.cn/problems/search-a-2d-matrix-ii/) | 中等 | 只有行、列各自有序 → 二分不够，要「阶梯搜索」或对每行二分 |
| [LC 702 搜索长度未知的有序数组](https://leetcode.cn/problems/search-in-a-sorted-array-of-unknown-size/) | 中等 | 拿不到 `n`，先指数扩张再二分（会员题） |
| [LC 374 猜数字大小](https://leetcode.cn/problems/guess-number-higher-or-lower/) | 简单 | 用 `guess(mid)` 替代数组比较，纯模板 A 手感训练 |
| [LC 278 第一个错误的版本](https://leetcode.cn/problems/first-bad-version/) | 简单 | **模板 B 的判定式版本**：谓词是 `isBadVersion(mid)`，是 025 二分答案的前身 |

<details>
<summary>💡 Hint（点击展开）</summary>

**LC 34 查找首末位置**
1. 命中时为什么不能直接 `return mid`？想想「命中」这个事实能否告诉你「还有没有更左的相同值」。
2. 如果只能写一个 `lowerBound(x)`，怎么用它同时算出左边界和右边界？注意右边界对应的是「第一个 **大于** target 的位置」再往回退一格。

**LC 35 搜索插入位置**
1. 把 `lo` 一直推到「左边全小于 target」为止，循环结束时 `lo` 的物理含义是什么？
2. 这个含义下，`target` 比所有元素都大时 `lo` 应该等于几？你的 `hi` 初值允许它取到这个值吗？

**LC 69 x 的平方根**
1. 把「求最大整数 m 使 m² ≤ x」写成「答案随 m 单调」的形式，谁是那个 0/1 谓词？
2. `m * m` 在 `m` 接近 46341 时就超过 `int` 了——你的 `mid` 是什么类型？

**LC 278 第一个错误的版本**
1. 把 `isBadVersion` 看成 `nums[mid] >= target` 的替身，它对应模板 A 还是模板 B？
2. 谓词一旦为真就不再变假，这个「一旦为真永远为真」的性质叫什么？

**通用**
- 写二分前先用一句话写清不变量：「第 k 轮开始时，答案（若存在）必在 ___ 内」。写不出来就别动键盘。
- 每次更新 `lo` / `hi` 后问自己：`mid` 有没有被排除？排除了才 ±1。
- 检查死循环只需要检查一件事：**长度为 1 的区间**上会发生什么。

</details>

---

## 附：本章小结（可背走的三句话）

```
① 二分 = 区间长度严格递减 + 不变量保住答案；两套模板靠 hi 的语义配套
② 闭区间 [lo,hi] 找具体值（hi=n-1，lo<=hi）；左闭右开 [lo,hi) 找分界点（hi=n，lo<hi，返回 lo）
③ mid 下取整 ⇒ lo 必须 mid+1；写 lo=mid 就必须上取整，否则必死循环
```

> **下一章**：[`021-find-first-and-last-position-of-element-in-sorted-array.md`](021-find-first-and-last-position-of-element-in-sorted-array.md) —— 把模板 B 用两次，回答「有重复元素时，边界在哪」。
