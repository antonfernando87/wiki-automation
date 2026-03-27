#!/usr/bin/env python3
"""Upsert a wiki entry at the correct chronological position (newest first).

Usage: python3 dedup_wiki.py <patch_file> <wiki_file>
  patch_file : newly generated *_summary_patch.md
  wiki_file  : target wiki page (e.g. Daily-Updates.md, stripped.md)

Handles two formats:
  - Section-based : "## March 26, 2026" or "## Week of March 23, 2026"
  - Bullet-based  : "- **March 2026**: ..."  (monthly)

The existing entry for the same date is removed (if present), then the new
entry is inserted at the correct date-sorted position (newest first).
"""
import re, sys, os
from datetime import datetime


def parse_date(line):
    line = line.strip()
    # Monthly bullet: "- **March 2026**: ..."
    m = re.match(r"- \*\*([^*]+)\*\*:", line)
    if m:
        try:
            return datetime.strptime(m.group(1).strip(), "%B %Y")
        except ValueError:
            pass
    # Weekly section: "## Week of March 23–27, 2026" (date range)
    if line.startswith("## Week of "):
        rest = line[11:]  # e.g. "March 23–27, 2026"
        # Range format: "Month DD–DD, YYYY" or "Month DD–Month DD, YYYY"
        m2 = re.match(r"([A-Za-z]+ \d+)[\u2013\u2014\-](?:[A-Za-z]+ )?\d+, (\d{4})", rest)
        if m2:
            try:
                return datetime.strptime(f"{m2.group(1)}, {m2.group(2)}", "%B %d, %Y")
            except ValueError:
                pass
        # Fallback: single date "Month DD, YYYY"
        try:
            return datetime.strptime(rest, "%B %d, %Y")
        except ValueError:
            pass
    # Daily section: "## March 26, 2026"
    if line.startswith("## "):
        try:
            return datetime.strptime(line[3:], "%B %d, %Y")
        except ValueError:
            pass
    return None


if len(sys.argv) < 3:
    sys.exit(0)

patch_file, wiki_file = sys.argv[1], sys.argv[2]

if not os.path.exists(patch_file):
    sys.exit(0)

patch_text = open(patch_file, encoding="utf-8").read()
first_line = next((ln for ln in patch_text.splitlines() if ln.strip()), "")
if not first_line:
    sys.exit(0)

new_date = parse_date(first_line)
monthly = bool(re.match(r"- \*\*[^*]+\*\*:", first_line.strip()))

# Wiki file doesn't exist yet — just write the patch and exit
if not os.path.exists(wiki_file):
    open(wiki_file, "w", encoding="utf-8").write(patch_text.rstrip() + "\n")
    sys.exit(0)

txt = open(wiki_file, encoding="utf-8").read()

# ── DEDUP: remove any existing entry for the same key ──────────────────────
if monthly:
    key_m = re.match(r"(- \*\*[^*]+\*\*:)", first_line.strip())
    if key_m:
        txt = re.sub(re.escape(key_m.group(1)) + r".*?(?=\n- \*\*|\Z)",
                     "", txt, flags=re.DOTALL)
else:
    # Use date-based matching to handle format differences (e.g. "March 4" vs "March 04")
    if new_date is not None:
        # Loop to handle multiple duplicates, including ones with differing text formats
        while True:
            existing_heading = next(
                (ln.strip() for ln in txt.splitlines()
                 if parse_date(ln) is not None
                 and parse_date(ln).date() == new_date.date()),
                None
            )
            if not existing_heading:
                break
            txt = re.sub(re.escape(existing_heading) + r".*?(?=\n## |\Z)", "",
                         txt, flags=re.DOTALL)
    else:
        txt = re.sub(re.escape(first_line.strip()) + r".*?(?=\n## |\Z)", "",
                     txt, flags=re.DOTALL)

txt = re.sub(r"\n{3,}", "\n\n", txt).strip()

# ── INSERT at correct chronological position (newest first) ────────────────
new_entry = patch_text.rstrip()

if monthly:
    # Split into preamble + individual bullet blocks
    bullet_split = re.split(r"\n(?=- \*\*)", "\n" + txt)
    preamble = bullet_split[0].lstrip("\n").rstrip()
    bullets = [b.lstrip("\n").rstrip() for b in bullet_split[1:] if b.strip()]

    inserted = False
    result = []
    for b in bullets:
        b_date = parse_date(b.splitlines()[0] if b else "")
        if not inserted and (new_date is None or b_date is None or b_date <= new_date):
            result.append(new_entry)
            inserted = True
        result.append(b)
    if not inserted:
        result.append(new_entry)

    out_parts = ([preamble] if preamble else []) + result
    txt = "\n\n".join(out_parts) + "\n"

else:
    # Split into preamble + ## sections
    split = re.split(r"\n(?=## )", "\n" + txt)
    preamble = split[0].lstrip("\n").rstrip()
    sections = [s.lstrip("\n").rstrip() for s in split[1:] if s.strip()]

    inserted = False
    result = []
    for s in sections:
        s_date = parse_date(s.splitlines()[0] if s else "")
        if not inserted and (new_date is None or s_date is None or s_date <= new_date):
            result.append(new_entry)
            inserted = True
        result.append(s)
    if not inserted:
        result.append(new_entry)

    out_parts = ([preamble] if preamble else []) + result
    txt = "\n\n".join(out_parts) + "\n"

open(wiki_file, "w", encoding="utf-8").write(txt)
