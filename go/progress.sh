#!/usr/bin/env bash
# 《Go 深水区》写作进度看板  —— 用法: bash go/progress.sh [--all]
# 状态判定: "已完成（初稿）"/"已完成" = 已写；其余 = 未写（骨架）
cd "$(dirname "$0")" || exit 1

# 批次划分（对应并行子任务）
batch_of() {
  n=$1
  case $n in
    1) echo "手写";; 
    [2-6]) echo "A:2-6";; [7-9]|10|11) echo "B:7-11";;
    1[2-6]) echo "C:12-16";; 1[7-9]|20) echo "D:17-20";;
    2[1-6]) echo "E:21-26";; 2[7-9]|3[0-2]) echo "F:27-32";;
    3[3-8]) echo "G:33-38";; 3[9]|4[0-3]) echo "H:39-43";;
    4[4-8]) echo "I:44-48";; 4[9]|5[0-1]) echo "J:49-51";;
    5[2-4]) echo "K:52-54";; *) echo "-";;
  esac
}

done_n=0
printf "%-4s %-8s %-10s %7s %7s\n" 章 批次 状态 字节 字数
printf '%.0s─' {1..48}; echo
for f in $(ls [0-9]*.md | sort -V); do
  n=${f%%-*}
  st=$(grep -m1 -Eo '已完成（初稿）|已完成|待撰写|草稿' "$f" | head -1)
  case "$st" in 已完成*) st=✅写; done_n=$((done_n+1));; *) st=·待写;; esac
  if [ "$1" = "--all" ] || [ "$st" = "✅写" ]; then
    printf "%-4s %-8s %-10s %7s %7s\n" "$n" "$(batch_of "$n")" "$st" \
      "$(wc -c <"$f" | tr -d ' ')" "$(wc -m <"$f" | tr -d ' ')"
  fi
done
printf '%.0s─' {1..48}; echo
echo "已完成 $done_n / 54 章"
echo
echo "最近改动的 5 个文件（正在写的那批就在里面）:"
ls -lt [0-9]*.md | head -5 | awk '{printf "  %-32s %s %s %s\n", $NF, $6, $7, $8}'
