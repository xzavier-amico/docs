#!/usr/bin/env python3
"""Check fin math with the same Markdown extensions as the site.

Run after installing requirements.txt: python3 scripts/test-fin-math.py.
This checks preprocessing/HTML, not browser/CDN availability or TeX semantics.
"""
import importlib.util
from pathlib import Path
import re
import unittest
import markdown

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("prepare", ROOT / "scripts/prepare-docs.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


def render(text):
    return markdown.markdown(
        prepare.normalize_fin_math(text),
        extensions=["pymdownx.arithmatex", "pymdownx.superfences", "tables"],
        extension_configs={"pymdownx.arithmatex": {"generic": True}},
    )


class MathRendering(unittest.TestCase):
    def test_delimiters(self):
        html = render("行内 $x^2$，另一种 \\(y^2\\)。\n\n$$x+y$$\n\n\\[\nz^2\n\\]")
        self.assertEqual(html.count('class="arithmatex"'), 4)

    def test_money_and_code(self):
        html = render("`$100` 与 `$200`。\n\n```text\n$$not math$$\n```\n")
        self.assertNotIn('class="arithmatex"', html)
        self.assertIn("$$not math$$", html)

    def test_sql_dollar_quoting_is_preserved(self):
        source = "```sql\nCREATE FUNCTION f() RETURNS void AS $$\nBEGIN\n  RETURN;\nEND;\n$$ LANGUAGE plpgsql;\n```\n"
        self.assertEqual(prepare.normalize_fin_math(source), source)

    def test_adjacent_and_list_display(self):
        html = render("1. 演示：\n   $$x^2$$\n\n\\[\na\n\\]\n\\[\nb\n\\]")
        self.assertEqual(html.count('class="arithmatex"'), 3)

    def test_existing_chapters_no_raw_display(self):
        for path in sorted((ROOT / "fin").glob("[0-9][0-9]-*.md")):
            with self.subTest(chapter=path.name):
                html = render(path.read_text(encoding="utf-8"))
                html = re.sub(r'<(span|div) class="arithmatex">.*?</\1>', "", html, flags=re.S)
                html = re.sub(r"<pre\b.*?</pre>|<code\b.*?</code>", "", html, flags=re.S)
                self.assertNotIn("$$", html)
                self.assertNotIn(r"\frac", html)


if __name__ == "__main__":
    unittest.main()
