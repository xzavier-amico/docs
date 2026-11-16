#!/usr/bin/env bash
# 《算法深水区》写作进度看板 —— 用法: bash algo/progress.sh [--all]
cd "$(dirname "$0")" || exit 1

part_of() {
  n=$1
  if   [ "$n" -le 10 ]; then echo "P1 双指针数组"
  elif [ "$n" -le 19 ]; then echo "P2 前缀和滑窗"
  elif [ "$n" -le 27 ]; then echo "P3 二分"
  elif [ "$n" -le 35 ]; then echo "P4 排序区间堆"
  elif [ "$n" -le 43 ]; then echo "P5 哈希原地"
  elif [ "$n" -le 54 ]; then echo "P6 链表"
  elif [ "$n" -le 64 ]; then echo "P7 栈队列"
  elif [ "$n" -le 81 ]; then echo "P8 树"
  elif [ "$n" -le 95 ]; then echo "P9 图"
  elif [ "$n" -le 107 ]; then echo "P10 回溯"
  elif [ "$n" -le 131 ]; then echo "P11 动规"
  elif [ "$n" -le 139 ]; then echo "P12 贪心"
  elif [ "$n" -le 145 ]; then echo "P13 位运算数学"
  else echo "P14 矩阵字符串"; fi
}

batch_of() {
  n=$1
  if   [ "$n" -le 10 ]; then echo "A"
  elif [ "$n" -le 19 ]; then echo "B"
  elif [ "$n" -le 27 ]; then echo "C"
  elif [ "$n" -le 35 ]; then echo "D"
  elif [ "$n" -le 43 ]; then echo "E"
  elif [ "$n" -le 54 ]; then echo "F"
  elif [ "$n" -le 64 ]; then echo "G"
  elif [ "$n" -le 81 ]; then echo "H"
  elif [ "$n" -le 95 ]; then echo "I"
  elif [ "$n" -le 107 ]; then echo "J"
  elif [ "$n" -le 119 ]; then echo "K"
  elif [ "$n" -le 131 ]; then echo "L"
  elif [ "$n" -le 145 ]; then echo "M"
  else echo "N"; fi
}

done_n=0; total=150; thin=0
printf "%-5s %-4s %-20s %-10s %7s %5s %5s %5s\n" 编号 批 部分 状态 字符 小节 双码 hint
printf '%.0s─' {1..72}; echo
for f in $(ls [0-9]*.md | sort -V); do
  n=$(echo "$f" | sed -E 's/^0*([0-9]+)-.*/\1/')
  [ "$n" = "0" ] && continue
  st=$(grep -m1 -Eo '已完成（初稿）|已完成|待撰写' "$f" | head -1)
  case "$st" in 已完成*) st=✅写; done_n=$((done_n+1));; *) st=·待写;; esac
  c=$(wc -m <"$f" | tr -d ' ')
  [ "$st" = "✅写" ] && [ "$c" -lt 6000 ] && { flag="⚠️"; thin=$((thin+1)); } || flag=""
  if [ "$1" = "--all" ] || [ "$st" = "✅写" ]; then
    j=$(grep -c '^```java' "$f"); g=$(grep -c '^```go' "$f")
    printf "%-5s %-4s %-20s %-10s %7s %5s %5s %5s %s\n" \
      "$(printf '%03d' "$n")" "$(batch_of "$n")" "$(part_of "$n")" "$st" \
      "$c" "$(grep -c '^## ' "$f")" "${j}+${g}" \
      "$(grep -c '<details>' "$f")" "$flag"
  fi
done
printf '%.0s─' {1..72}; echo
echo "已完成 $done_n / $total 章（偏薄 $thin 章）"
echo "最近改动: $(ls -t [0-9]*.md 2>/dev/null | head -3 | tr '\n' ' ')"
