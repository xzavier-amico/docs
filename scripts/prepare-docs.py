#!/usr/bin/env python3
"""把仓库根目录的正文复制到 MkDocs 的 docs_dir。

仓库本身的布局是 go/、llm/、eng/... 直接在根目录，而 MkDocs 要求
docs_dir 是配置文件所在目录的子目录。这里只复制 Markdown、.nav.yml
和 LICENSE，构建产物不会回写到源文件。
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGE = ROOT / "build" / "docs"

SKIP_DIRS = {".git", ".github", "build", "node_modules", "scripts", "site", "docs"}
COPY_NAMES = {".nav.yml", "LICENSE"}
SEARCH_EXCLUDE_NAMES = {"0-summary.md", "0-compiled.md", "INDEX.md"}
SEARCH_EXCLUDE_FRONT_MATTER = "---\nsearch:\n  exclude: true\n---\n\n"


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
        if src.suffix.lower() != ".md" and src.name not in COPY_NAMES:
            continue

        dest = STAGE / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.name in SEARCH_EXCLUDE_NAMES:
            text = src.read_text(encoding="utf-8")
            dest.write_text(SEARCH_EXCLUDE_FRONT_MATTER + text, encoding="utf-8")
        else:
            shutil.copy2(src, dest)
        copied += 1

    print(f"Prepared {copied} files in {STAGE}")


if __name__ == "__main__":
    main()
