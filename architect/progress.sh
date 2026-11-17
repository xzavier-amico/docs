#!/usr/bin/env bash
# architect/ 进度统计
cd "$(dirname "$0")" || exit 1

total=26
done=$(ls [0-9][0-9]-*.md 2>/dev/null | wc -l | tr -d ' ')
chars=$(cat [0-9][0-9]-*.md 2>/dev/null | wc -m | tr -d ' ')

printf "architect/ 进度\n"
printf "  章节   %s / %s\n" "$done" "$total"
printf "  字符   %s\n" "$chars"
printf "\n"
printf "%-42s %8s\n" "文件" "字符"
for f in [0-9][0-9]-*.md; do
  [ -e "$f" ] || continue
  printf "%-42s %8s\n" "$f" "$(wc -m < "$f" | tr -d ' ')"
done
