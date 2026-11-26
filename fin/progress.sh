#!/usr/bin/env bash
# fin/ 进度统计
cd "$(dirname "$0")" || exit 1

total=48
done=$(find . -maxdepth 1 -type f -name '[0-9][0-9]-*.md' | wc -l | tr -d ' ')
chars=$(cat [0-9][0-9]-*.md 2>/dev/null | wc -m | tr -d ' ')

printf "fin/ 进度（《金融系统与智能交易架构》）\n"
printf "  章节   %s / %s\n" "$done" "$total"
printf "  字符   %s\n" "$chars"
missing=""
for n in $(seq -w 1 "$total"); do
  if ! compgen -G "${n}-*.md" >/dev/null; then missing+=" $n"; fi
done
printf "  缺失编号:%s\n" "${missing:- 无}"
printf "\n"
printf "%-56s %8s\n" "文件" "字符"
for f in [0-9][0-9]-*.md; do
  [ -e "$f" ] || continue
  printf "%-56s %8s\n" "$f" "$(wc -m < "$f" | tr -d ' ')"
done
