#!/usr/bin/env bash
# 以总纲清单为准，未成稿不计作完成。
cd "$(dirname "$0")" || exit 1

total=$(grep -Ec '^- \[[ x]\] [0-9]{2} ' 0-summary.md)
done=$(grep -Ec '^- \[x\] [0-9]{2} ' 0-summary.md)

printf 'llm_base/ 进度（《LLM 基础》）\n'
printf '  章节   %s / %s\n\n' "$done" "$total"
grep -E '^- \[[ x]\] [0-9]{2} ' 0-summary.md
