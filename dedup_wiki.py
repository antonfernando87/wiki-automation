#!/usr/bin/env python3
"""Remove an existing wiki entry before prepending to avoid duplicates on re-runs.

Usage: python3 dedup_wiki.py <patch_file> <wiki_file>
  patch_file : newly generated *_summary_patch.md
  wiki_file  : target wiki page (e.g. Daily-Updates.md, stripped.md)

The first line of patch_file is used as the section key to find and remove:
  - "## ..." headers  -> removes from that header to the next ## or end of file
  - "- **Month**:..." -> removes the matching monthly bullet block
"""
import re, sys, os

if len(sys.argv) < 3:
    sys.exit(0)

patch_file, wiki_file = sys.argv[1], sys.argv[2]

if not os.path.exists(wiki_file) or not os.path.exists(patch_file):
    sys.exit(0)

first_line = open(patch_file, encoding="utf-8").readline().strip()
if not first_line:
    sys.exit(0)

txt = open(wiki_file, encoding="utf-8").read()

# Monthly bullet format: "- **March 2026**: ..."
m = re.match(r"(- \*\*[^*]+\*\*:)", first_line)
if m:
    key = re.escape(m.group(1))
    txt = re.sub(key + r".*?\n\n", "", txt, count=1, flags=re.DOTALL)
else:
    # Daily/weekly section header: "## March 26, 2026" or "## Week of ..."
    key = re.escape(first_line)
    txt = re.sub(key + r".*?(?=\n## |\Z)", "", txt, count=1, flags=re.DOTALL)

txt = re.sub(r"\n{3,}", "\n\n", txt)
open(wiki_file, "w", encoding="utf-8").write(txt)
