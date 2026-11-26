#!/usr/bin/env python3
"""把仓库根目录的正文复制到 MkDocs 的 docs_dir。

仓库本身的布局是 go/、llm/、eng/... 直接在根目录，而 MkDocs 要求
docs_dir 是配置文件所在目录的子目录。只复制 Markdown、.nav.yml、
LICENSE 和明确列出的示例附件，构建产物不会回写到源文件。
"""

from __future__ import annotations

import shutil
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGE = ROOT / "build" / "docs"

SKIP_DIRS = {".git", ".github", "build", "node_modules", "scripts", "site", "docs"}
COPY_NAMES = {".nav.yml", "LICENSE"}
COPY_EXAMPLES = {Path("base/examples/h3-loopback.py")}
COPY_ASSETS = {Path("assets/javascripts/mathjax.js")}
SEARCH_EXCLUDE_NAMES = {"0-summary.md", "0-compiled.md", "INDEX.md"}
SEARCH_EXCLUDE_FRONT_MATTER = "---\nsearch:\n  exclude: true\n---\n\n"


def normalize_fin_math(text: str) -> str:
    """Isolate display blocks for arithmatex without rewriting code examples."""
    result = []
    fence = None
    display = False
    for line in text.splitlines():
        match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if match:
            token = match[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            result.append(line)
            continue
        if fence:
            result.append(line)
            continue
        match = re.fullmatch(r"(\s*)\$\$(.+)\$\$\s*", line)
        if match:
            _, formula = match.groups()
            result.extend(["", "$$", formula, "$$", ""])
        elif line.strip() == "$$":
            result.extend(["", "$$", ""])
            display = not display
        elif display:
            result.append(line.lstrip())
        elif line.strip() in (r"\[", r"\]"):
            # Adjacent display equations need a paragraph boundary too.
            if line.strip() == r"\[":
                result.append("")
            result.append(line)
            if line.strip() == r"\]":
                result.append("")
        else:
            result.append(line)
    return "\n".join(result) + "\n"


def main() -> None:
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)

    copied = 0
    for src in ROOT.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(ROOT)
        if rel.parts and rel.parts[0] in SKIP_DIRS:
            continue
        if src.suffix.lower() != ".md" and src.name not in COPY_NAMES and rel not in COPY_EXAMPLES and rel not in COPY_ASSETS:
            continue

        dest = STAGE / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if rel.parts[0] == "fin" and src.suffix == ".md":
            text = normalize_fin_math(src.read_text(encoding="utf-8"))
            if src.name in SEARCH_EXCLUDE_NAMES:
                text = SEARCH_EXCLUDE_FRONT_MATTER + text
            dest.write_text(text, encoding="utf-8")
        elif src.name in SEARCH_EXCLUDE_NAMES:
            text = src.read_text(encoding="utf-8")
            dest.write_text(SEARCH_EXCLUDE_FRONT_MATTER + text, encoding="utf-8")
        else:
            shutil.copy2(src, dest)
        copied += 1

    print(f"Prepared {copied} files in {STAGE}")


if __name__ == "__main__":
    main()
