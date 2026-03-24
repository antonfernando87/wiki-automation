#!/usr/bin/env python3
"""
Generate a monthly progress report as a single narrative paragraph.
No PR/issue/commit metadata is included — output is plain prose only.

Format:
  ## Progress Report — <Month Year>

  <narrative paragraph>

  ---

Environment variables:
    GH_TOKEN        PAT with repo read scope (also used for GitHub Models).
    GITHUB_ACTOR    GitHub username to track (default: repository owner).
    REPORT_MONTH    ISO year-month (YYYY-MM). Defaults to last month.
"""

import os, sys, requests, calendar
from datetime import date, timedelta, timezone, datetime

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("GH_TOKEN", "")
if not TOKEN:
    sys.exit("Error: GH_TOKEN is not set.")

GITHUB_ACTOR = (
    os.environ.get("GITHUB_ACTOR")
    or os.environ.get("GITHUB_REPOSITORY_OWNER", "")
)
REPORT_MONTH_STR = os.environ.get("REPORT_MONTH", "").strip()

if REPORT_MONTH_STR:
    year, month = map(int, REPORT_MONTH_STR.split("-"))
else:
    today = date.today()
    last_month = today.replace(day=1) - timedelta(days=1)
    year, month = last_month.year, last_month.month

MONTH_START = datetime(year, month, 1, 0, 0, 0, tzinfo=timezone.utc)
MONTH_END   = datetime(
    year, month, calendar.monthrange(year, month)[1], 23, 59, 59,
    tzinfo=timezone.utc,
)
MONTH_LABEL = date(year, month, 1).strftime("%B %Y")

GH_HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# ── GitHub REST helper ────────────────────────────────────────────────────────
def gh_get(url, params=None):
    results, p = [], {"per_page": 100, **(params or {})}
    while url:
        r = requests.get(url, headers=GH_HEADERS, params=p)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "items" in data:
            results.extend(data["items"])
        elif isinstance(data, list):
            results.extend(data)
        else:
            results.append(data)
        url, p = None, {}
        for part in r.headers.get("Link", "").split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
    return results

def parse_iso(ts):
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))

def discover_repos():
    repos = gh_get(
        f"https://api.github.com/users/{GITHUB_ACTOR}/repos",
        {"type": "all", "sort": "updated"},
    )
    return [
        f"{r['owner']['login']}/{r['name']}"
        for r in repos
        if not r.get("archived", False)
    ]

# ── Data collection ───────────────────────────────────────────────────────────
def collect_merged_prs():
    try:
        items = gh_get(
            "https://api.github.com/search/issues",
            {
                "q": (
                    f"author:{GITHUB_ACTOR} is:pr is:merged "
                    f"merged:{year}-{month:02d}-01"
                    f"..{year}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"
                )
            },
        )
        return [
            {"title": pr["title"], "body": (pr.get("body") or "")[:300]}
            for pr in items
        ]
    except Exception as e:
        print(f"Warning — merged PRs: {e}", file=sys.stderr)
        return []

def collect_commits(repos):
    commits = []
    for repo in repos:
        try:
            items = gh_get(
                f"https://api.github.com/repos/{repo}/commits",
                {
                    "since": MONTH_START.isoformat(),
                    "until": MONTH_END.isoformat(),
                    "author": GITHUB_ACTOR,
                },
            )
            for c in items:
                dt = parse_iso(c["commit"]["author"]["date"])
                if dt and MONTH_START <= dt <= MONTH_END:
                    commits.append(c["commit"]["message"].split("\n")[0])
        except Exception as e:
            print(f"Warning: {repo}: {e}", file=sys.stderr)
    return commits

# ── Narrative generation ──────────────────────────────────────────────────────
def _template_narrative(prs, commits):
    if not prs and not commits:
        return f"No activity was recorded for {MONTH_LABEL}."
    parts = []
    if prs:
        titles = "; ".join(p["title"] for p in prs[:4])
        parts.append(f"Work this month focused on {titles}.")
    if commits:
        msgs = "; ".join(commits[:4])
        parts.append(f"Commit activity included: {msgs}.")
    return " ".join(parts)

def generate_narrative(prs, commits):
    if not prs and not commits:
        return f"No activity was recorded for {MONTH_LABEL}."

    pr_block = (
        "\n".join(
            f"- {p['title']}"
            + (f"\n  {p['body'][:200]}" if p["body"].strip() else "")
            for p in prs
        )
        or "None"
    )
    commit_block = "\n".join(f"- {m}" for m in commits[:30]) or "None"

    prompt = (
        f"Below is the GitHub activity for {MONTH_LABEL}.\n\n"
        f"Pull Requests:\n{pr_block}\n\n"
        f"Commits:\n{commit_block}\n\n"
        "Write a concise 3–5 sentence narrative summary of the month's work. "
        "Focus on the overall themes and goals, not individual items. "
        "Do NOT mention PR numbers, issue numbers, commit hashes, URLs, or weeks. "
        "Do NOT use bullet points. "
        "Write in plain prose as a single cohesive paragraph. "
        "Output only the paragraph — no headings, no preamble."
    )

    try:
        resp = requests.post(
            "https://models.inference.ai.azure.com/chat/completions",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a concise technical writer summarising a "
                            "software developer's monthly GitHub activity into "
                            "a single plain narrative paragraph. Be specific "
                            "about what was worked on; avoid generic filler. "
                            "Never mention PR numbers, issue numbers, commit "
                            "hashes, URLs, or specific week dates."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 350,
                "temperature": 0.3,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(
            f"Warning — GitHub Models API unavailable ({e}); using template.",
            file=sys.stderr,
        )
        return _template_narrative(prs, commits)

# ── Write output ──────────────────────────────────────────────────────────────
def write_summary(narrative):
    with open("monthly_summary_patch.md", "w") as f:
        f.write(f"## Progress Report — {MONTH_LABEL}\n\n")
        f.write(f"{narrative}\n\n")
        f.write("---\n\n")
    print("✓ Monthly summary written to monthly_summary_patch.md")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"Generating monthly report for {MONTH_LABEL} ({GITHUB_ACTOR})...")

    repos   = discover_repos()
    prs     = collect_merged_prs()
    commits = collect_commits(repos)

    print(f"  Merged PRs : {len(prs)}")
    print(f"  Commits    : {len(commits)}")

    narrative = generate_narrative(prs, commits)
    write_summary(narrative)

if __name__ == "__main__":
    main()
