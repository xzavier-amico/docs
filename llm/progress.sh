#!/usr/bin/env bash
# 《AgentOS 工程架构》写作进度看板 —— 用法: bash llm/progress.sh [--all]
cd "$(dirname "$0")" || exit 1

batch_of() {
  n=$1
  if [ "$n" = "1" ]; then echo "手写"
  elif [ "$n" -le 6 ];  then echo "A:2-6"
  elif [ "$n" -le 11 ]; then echo "B:7-11"
  elif [ "$n" -le 16 ]; then echo "C:12-16"
  elif [ "$n" -le 21 ]; then echo "D:17-21"
  elif [ "$n" -le 26 ]; then echo "E:22-26"
  elif [ "$n" -le 31 ]; then echo "F:27-31"
  elif [ "$n" -le 36 ]; then echo "G:32-36"
  elif [ "$n" -le 41 ]; then echo "H:37-41"
  elif [ "$n" -le 46 ]; then echo "I:42-46"
  else echo "J:47-52"; fi
}

done_n=0; total=52
printf "%-4s %-8s %-10s %7s %7s\n" 章 批次 状态 字符 小节
printf '%.0s─' {1..48}; echo
for f in $(ls [0-9]*.md | sort -V); do
  n=${f%%-*}
  [ "$n" = "0" ] && continue
  st=$(grep -m1 -Eo '已完成（初稿）|已完成|待撰写' "$f" | head -1)
  case "$st" in 已完成*) st=✅写; done_n=$((done_n+1));; *) st=·待写;; esac
  if [ "$1" = "--all" ] || [ "$st" = "✅写" ]; then
    printf "%-4s %-8s %-10s %7s %7s\n" "$n" "$(batch_of "$n")" "$st" \
      "$(wc -m <"$f" | tr -d ' ')" "$(grep -c '^## ' "$f")"
  fi
done
printf '%.0s─' {1..48}; echo
echo "已完成 $done_n / $total 章"
echo "最近改动: $(ls -t [0-9]*.md | head -3 | tr '\n' ' ')"
