#!/usr/bin/env python3
"""Rebuild the committed static documentation: python3 docs/render.py (requires Markdown)."""
from html import escape
from pathlib import Path
import re
import markdown

root = Path(__file__).resolve().parent
guide = (root / "GUIDE.md").read_text().split("## What runs", 1)[1]
source = "## What runs" + guide
source += "\n\n## Implementation diary\n\n" + (root / "DIARY.md").read_text().split("\n\n", 1)[1]
renderer = markdown.Markdown(extensions=["fenced_code", "tables", "toc"])
body = renderer.convert(source)
sections = re.split(r'(?=<h2 id=")', body)
body = "".join(f'<section class="chapter">{section}</section>' for section in sections if section.strip())
links = "".join(f'<a href="#{item["id"]}">{escape(item["name"])}</a>' for item in renderer.toc_tokens)
template = (root / "shell.html").read_text()
page = template.replace("<!-- NAV -->", links).replace("<!-- CONTENT -->", body)
out = root / "site"
out.mkdir(exist_ok=True)
(out / "index.html").write_text(page)
print(f"Built {out / 'index.html'} ({len(renderer.toc_tokens)} chapters)")
