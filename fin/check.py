#!/usr/bin/env python3
"""Structural checks, not financial certification. python3 fin/check.py [--sync]."""
import argparse
from pathlib import Path
import re
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent


def visible_markdown(text):
    """Exclude fenced examples before inspecting links/headings."""
    result = []
    fence = None
    for line in text.splitlines():
        match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if match:
            token = match[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            result.append("")
        else:
            result.append(line if fence is None else "")
    return "\n".join(result), fence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sync", action="store_true", help="sync draft-presence checkboxes only")
    args = parser.parse_args()
    files = sorted(ROOT.glob("[0-9][0-9]-*.md"))
    by_number = {}
    errors = []
    for path in files:
        number = int(path.name[:2])
        by_number.setdefault(number, []).append(path)
        if not 1 <= number <= 48:
            errors.append(f"非计划编号: {path.name}")
    missing = [n for n in range(1, 49) if n not in by_number]
    for n, matches in by_number.items():
        if len(matches) != 1:
            errors.append(f"重复编号 {n:02}: {[p.name for p in matches]}")

    summary_path = ROOT / "0-summary.md"
    summary = summary_path.read_text(encoding="utf-8")
    if args.sync:
        def sync(match):
            n = int(match[2])
            return f"- [{'x' if n in by_number else ' '}] {match[2]} "
        summary = re.sub(r"^- \[([ x])\] (\d{2}) ", sync, summary, flags=re.M)
        summary_path.write_text(summary, encoding="utf-8")

    for mark, number in re.findall(r"^- \[([ x])\] (\d{2}) ", summary, flags=re.M):
        if (mark == "x") != (int(number) in by_number):
            errors.append(f"进度不符: 总纲 {number} [{mark}] 与文件存在性不同")

    links = 0
    for path in [summary_path] + files:
        text = path.read_text(encoding="utf-8")
        visible, fence = visible_markdown(text)
        if fence:
            errors.append(f"未闭合代码围栏: {path.name}")
        h1 = re.findall(r"^# (.+)$", visible, flags=re.M)
        if len(h1) != 1:
            errors.append(f"H1 数量应为1: {path.name}: {len(h1)}")
        if path != summary_path:
            n = int(path.name[:2])
            if h1 and not re.match(rf"0?{n}[.．、 ]", h1[0]):
                errors.append(f"H1 编号不符: {path.name}")
            if "自检" not in visible:
                errors.append(f"缺少自检题: {path.name}")
            if n >= 20:
                for lang in ("go", "java", "sql"):
                    if not re.search(rf"^\s*```{lang}\s*$", text, re.M | re.I):
                        errors.append(f"缺少 {lang} 示例: {path.name}")
            if n >= 20 and len(text) < 5000:
                errors.append(f"工程章过短，需人工复核: {path.name}")
        for target in re.findall(r"\]\(([^\s)]+)\)", visible):
            if re.match(r"[a-zA-Z]+://|mailto:|#", target):
                continue
            rel = unquote(target.split("#")[0].split("?")[0])
            if rel:
                links += 1
                if not (path.parent / rel).exists():
                    errors.append(f"断链: {path.name} -> {rel}")

    print(f"章节文件: {len([n for n in by_number if 1 <= n <= 48])} / 48")
    print("缺失章节:", " ".join(f"{n:02}" for n in missing) or "无")
    print(f"本地链接: {links}（含总纲；重复引用逐项检查）")
    for error in errors:
        print(error)
    print(f"结构/进度/链接问题: {len(errors)}")
    return 1 if errors or missing else 0


if __name__ == "__main__":
    sys.exit(main())
