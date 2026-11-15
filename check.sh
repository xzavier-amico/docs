#!/usr/bin/env bash
# 全仓自检 —— 用法: bash check.sh [系列...]   例: bash check.sh eng   或   bash check.sh
# 检查项：① 相对链接是否可达 ② 是否残留占位符/待撰写 ③ 章节字数与小节数 ④ 状态标记是否一致
set -u
cd "$(dirname "$0")" || exit 1

SERIES=("$@")
[ ${#SERIES[@]} -eq 0 ] && SERIES=(go llm eng)

fail=0

echo "═══ 1. 断链检查 ═══"
python3 - "${SERIES[@]}" <<'PY'
import os, re, sys, urllib.parse
series = sys.argv[1:]
bad = 0
total = 0
for s in series:
    if not os.path.isdir(s):
        print(f"  ⚠️  目录不存在: {s}"); continue
    for fn in sorted(os.listdir(s)):
        if not fn.endswith(".md"):
            continue
        path = os.path.join(s, fn)
        text = open(path, encoding="utf-8").read()
        # 去掉围栏代码块，避免把 Go 泛型 Foo[T any](x T) 误判成链接
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        text = re.sub(r"`[^`]*`", "", text)
        # 只检查 markdown 链接里的相对路径，跳过 http/#/mailto
        for m in re.finditer(r"\[[^\]]*\]\(([^)\s]+)\)", text):
            link = m.group(1)
            if link.startswith(("http://", "https://", "#", "mailto:")):
                continue
            link = urllib.parse.unquote(link.split("#")[0])
            if not link:
                continue
            total += 1
            target = os.path.normpath(os.path.join(s, link))
            if not os.path.exists(target):
                bad += 1
                print(f"  ❌ {path}  →  {link}")
print(f"  检查 {total} 个相对链接，断链 {bad} 个")
sys.exit(1 if bad else 0)
PY
[ $? -ne 0 ] && fail=1

echo
echo "═══ 2. 占位符检查 ═══"
python3 - "${SERIES[@]}" <<'PY'
import os, re, sys
series = sys.argv[1:]
pats = [
    (r"待撰写", "未开写"),
    (r"写作时填充", "骨架残留"),
    (r"本章计划小节", "骨架残留"),
    (r"<!--\s*CHUNK\d", "分块占位符"),
    (r"<!--\s*CH\d+-CONT\s*-->", "章节未写完"),
    (r"<!--\s*CONT\s*-->", "章节未写完"),
    (r"<!--\s*PART\d+\s*-->", "章节未写完"),
    (r"TODO:", "TODO:"),
    (r"\bTBD\b", "TBD"),
    (r"（待补）|待补充|此处省略", "待补"),
]
bad = 0
for s in series:
    if not os.path.isdir(s):
        continue
    for fn in sorted(os.listdir(s)):
        if not fn.endswith(".md") or fn == "0-summary.md":
            continue
        p = os.path.join(s, fn)
        t = open(p, encoding="utf-8").read()
        hits = [(lbl, t[:m.start()].count("\n") + 1) for pat, lbl in pats for m in re.finditer(pat, t)]
        if hits:
            bad += 1
            locs = ", ".join(f"{lbl}@L{n}" for lbl, n in hits[:6])
            print(f"  ❌ {p}: {locs}{' …' if len(hits) > 6 else ''}")
print(f"  含占位符文件 {bad} 个")
sys.exit(1 if bad else 0)
PY
[ $? -ne 0 ] && fail=1

echo
echo "═══ 3. 章节体量与结构 ═══"
python3 - "${SERIES[@]}" <<'PY'
import os, re, sys
series = sys.argv[1:]
THIN = 11000
for s in series:
    if not os.path.isdir(s):
        continue
    files = [f for f in sorted(os.listdir(s)) if re.match(r"^\d+-.+\.md$", f)]
    done = thin = 0
    chars = 0
    rows = []
    for fn in files:
        t = open(os.path.join(s, fn), encoding="utf-8").read()
        n = int(fn.split("-")[0])
        if n == 0:
            continue
        c = len(re.sub(r"\s", "", t))
        chars += c
        is_done = "已完成" in t
        done += is_done
        secs = len(re.findall(r"^## ", t, re.M))
        tables = len(re.findall(r"^\|[-: |]+\|$", t, re.M))
        codes = t.count("```") // 2
        flag = ""
        if is_done and c < THIN:
            thin += 1; flag = "  ⚠️ 偏薄"
        if is_done and (secs < 6 or tables < 2 or codes < 2):
            flag += "  ⚠️ 结构不足"
        rows.append((n, c, secs, tables, codes, flag))
    print(f"  [{s}] 章节 {len(files)-1}，已完成 {done}，正文 {chars/10000:.1f} 万字，偏薄 {thin}")
    for n, c, secs, tables, codes, flag in rows:
        if flag:
            print(f"    {n:>2}  {c:>6}字  {secs:>2}节  {tables:>2}表  {codes:>2}码{flag}")
PY

echo
echo "═══ 4. 状态标记一致性 ═══"
for s in "${SERIES[@]}"; do
  [ -d "$s" ] || continue
  files=$(ls "$s"/[0-9]*.md 2>/dev/null | grep -v "/0-summary.md")
  n=$(echo "$files" | xargs grep -l "已完成" 2>/dev/null | wc -l | tr -d ' ')
  m=$(echo "$files" | xargs grep -l "待撰写" 2>/dev/null | wc -l | tr -d ' ')
  echo "  [$s] 已完成 $n / 待撰写 $m"
done


echo
echo "═══ 5. 大纲清单 ↔ 章节状态 ═══"
SYNC_FLAG=$(printf "%s\n" "$@" | grep -x -- "--sync" || true) python3 - "${SERIES[@]}" <<'PY2'
import os, re, sys
args = sys.argv[1:]
sync = os.environ.get("SYNC_FLAG") == "--sync"
series = [a for a in args if not a.startswith("--")]
mismatch = 0
for s in series:
    summ = os.path.join(s, "0-summary.md")
    if not os.path.exists(summ):
        continue
    lines = open(summ, encoding="utf-8").read().split("\n")
    done_n = pend_n = 0
    for i, line in enumerate(lines):
        m = re.match(r"^- \[( |x)\] (\d{1,2}) (.+)$", line)
        if not m:
            continue
        checked, n, title = m.group(1) == "x", int(m.group(2)), m.group(3)
        if n == 0:
            continue
        files = [f for f in os.listdir(s) if re.match(rf"^{n}-.+\.md$", f)]
        if not files:
            print(f"  ❌ [{s}] 第 {n} 章无文件：{title}")
            mismatch += 1
            continue
        t = open(os.path.join(s, files[0]), encoding="utf-8").read()
        is_done = "已完成" in t
        if is_done and not checked:
            if sync:
                lines[i] = line.replace("- [ ]", "- [x]", 1)
            else:
                print(f"  ⚠️  [{s}] 第 {n:>2} 章已完成但清单未勾：{title}")
                mismatch += 1
        elif checked and not is_done:
            if sync:
                lines[i] = line.replace("- [x]", "- [ ]", 1)
            else:
                print(f"  ⚠️  [{s}] 第 {n:>2} 章未完成但清单已勾：{title}")
                mismatch += 1
        done_n += is_done
        pend_n += not is_done
    if sync:
        open(summ, "w", encoding="utf-8").write("\n".join(lines))
    print(f"  [{s}] 章节完成 {done_n} / 待写 {pend_n}" + ("（已同步清单）" if sync else ""))
sys.exit(1 if mismatch and not sync else 0)
PY2
[ $? -ne 0 ] && fail=1

echo
if [ $fail -ne 0 ]; then
  echo "❌ 自检未通过"
  exit 1
fi
echo "✅ 自检通过"
