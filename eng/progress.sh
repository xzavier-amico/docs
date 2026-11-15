#!/usr/bin/env bash
# 《分布式工程实战》写作进度看板 —— 用法: bash eng/progress.sh [--all]
cd "$(dirname "$0")" || exit 1

batch_of() {
  n=$1
  if   [ "$n" -le 6 ];  then echo "A:幂等1-6"
  elif [ "$n" -le 14 ]; then echo "B:事务7-14"
  elif [ "$n" -le 22 ]; then echo "C:韧性15-22"
  elif [ "$n" -le 28 ]; then echo "D:消息23-28"
  elif [ "$n" -le 34 ]; then echo "E:编排29-34"
  elif [ "$n" -le 40 ]; then echo "F:存储35-40"
  elif [ "$n" -le 45 ]; then echo "G:运维运营41-45"
  elif [ "$n" -le 48 ]; then echo "H:效能46-48"
  else echo "I:收口49-52"; fi
}

done_n=0; total=52
printf "%-4s %-14s %-10s %7s %6s %6s\n" 章 批次 状态 字符 小节 双修表
printf '%.0s─' {1..56}; echo
for f in $(ls [0-9]*.md | sort -V); do
  n=${f%%-*}
  [ "$n" = "0" ] && continue
  st=$(grep -m1 -Eo '已完成（初稿）|已完成|待撰写' "$f" | head -1)
  case "$st" in 已完成*) st=✅写; done_n=$((done_n+1));; *) st=·待写;; esac
  if [ "$1" = "--all" ] || [ "$st" = "✅写" ]; then
    printf "%-4s %-14s %-10s %7s %6s %6s\n" "$n" "$(batch_of "$n")" "$st" \
      "$(wc -m <"$f" | tr -d ' ')" "$(grep -c '^## ' "$f")" \
      "$(grep -c '双修\|传统系统\|Agent 系统' "$f")"
  fi
done
printf '%.0s─' {1..56}; echo
echo "已完成 $done_n / $total 章"
echo "最近改动: $(ls -t [0-9]*.md | head -3 | tr '\n' ' ')"
