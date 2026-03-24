#!/usr/bin/env python3
"""
Generate a daily activity summary and write it to daily_summary_patch.md.
The work summary is a narrative paragraph (via GitHub Models API), not a
commit list.

Environment variables:
    GH_TOKEN        PAT with repo read scope (also used for GitHub Models).
    GITHUB_ACTOR    GitHub username to track (default: repository owner).
    SUMMARY_DATE    ISO date (YYYY-MM-DD). Defaults to yesterday.
"""

import os, sys, re, requests
from datetime import date, timedelta, timezone, datetime
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("GH_TOKEN", "")
if not TOKEN:
    sys.exit("Error: GH_TOKEN is not set.")

GITHUB_ACTOR = os.environ.get("GITHUB_ACTOR") or os.environ.get("GITHUB_REPOSITORY_OWNER", "")

SUMMARY_DATE_STR = os.environ.get("SUMMARY_DATE", "").strip()
SUMMARY_DATE = date.fromisoformat(SUMMARY_DATE_STR) if SUMMARY_DATE_STR else date.today() - timedelta(days=1)

DAY_START = datetime(SUMMARY_DATE.year, SUMMARY_DATE.month, SUMMARY_DATE.day,  0,  0,  0, tzinfo=timezone.utc)
DAY_END   = datetime(SUMMARY_DATE.year, SUMMARY_DATE.month, SUMMARY_DATE.day, 23, 59, 59, tzinfo=timezone.utc)

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

def in_window(dt_str):
    if not dt_str:
        return False
    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    return DAY_START <= dt <= DAY_END

# ── Data collection ───────────────────────────────────────────────────────────
commit_messages = []
all_prs, all_issues = [], []

date_str = SUMMARY_DATE.strftime("%Y-%m-%d")

# PRs merged today
try:
    for item in gh_get(
        "https://api.github.com/search/issues",
        {"q": f"author:{GITHUB_ACTOR} is:pr is:merged merged:{date_str}..{date_str}"},
    ):
        repo = item.get("repository_url", "").replace("https://api.github.com/repos/", "")
        all_prs.append({
            "repo":   repo.split("/")[-1],
            "number": item["number"],
            "title":  item["title"],
            "state":  "merged",
            "branch": "",
            "body":   (item.get("body") or "")[:300],
            "url":    item["html_url"],
        })
except Exception as e:
    print(f"Warning — merged PRs: {e}", file=sys.stderr)

# Open PRs updated today
try:
    for item in gh_get(
        "https://api.github.com/search/issues",
        {"q": f"author:{GITHUB_ACTOR} is:pr is:open updated:{date_str}..{date_str}"},
    ):
        repo = item.get("repository_url", "").replace("https://api.github.com/repos/", "")
        all_prs.append({
            "repo":   repo.split("/")[-1],
            "number": item["number"],
            "title":  item["title"],
            "state":  "open",
            "branch": "",
            "body":   (item.get("body") or "")[:300],
            "url":    item["html_url"],
        })
except Exception as e:
    print(f"Warning — open PRs: {e}", file=sys.stderr)

# Issues updated today
try:
    for item in gh_get(
        "https://api.github.com/search/issues",
        {"q": f"author:{GITHUB_ACTOR} is:issue updated:{date_str}..{date_str}"},
    ):
        repo = item.get("repository_url", "").replace("https://api.github.com/repos/", "")
        all_issues.append({
            "repo":   repo.split("/")[-1],
            "number": item["number"],
            "title":  item["title"],
            "state":  item["state"],
            "labels": [lb["name"] for lb in item.get("labels", [])],
            "url":    item["html_url"],
        })
except Exception as e:
    print(f"Warning — issues: {e}", file=sys.stderr)

# Commits today (across recently-active repos)
try:
    repos = gh_get(
        f"https://api.github.com/users/{GITHUB_ACTOR}/repos",
        {"type": "all", "sort": "updated"},
    )
    for repo_data in repos[:20]:
        if repo_data.get("archived"):
            continue
        repo_full = f"{repo_data['owner']['login']}/{repo_data['name']}"
        try:
            for c in gh_get(
                f"https://api.github.com/repos/{repo_full}/commits",
                {"since": DAY_START.isoformat(), "until": DAY_END.isoformat(), "author": GITHUB_ACTOR},
            ):
                msg = c["commit"]["message"].splitlines()[0]
                if not re.match(r"^merge\b", msg, re.I):
                    commit_messages.append(msg)
        except Exception:
            pass
except Exception as e:
    print(f"Warning — commits: {e}", file=sys.stderr)

# ── Narrative generation ──────────────────────────────────────────────────────
def _template_narrative(prs, commits):
    if not prs and not commits:
        return f"_No activity recorded for {SUMMARY_DATE.strftime('%B %d, %Y')}._"
    parts = []
    if prs:
        titles = "; ".join(p["title"][:70] for p in prs[:3])
        parts.append(f"Pull request activity centred on: {titles}.")
    if commits:
        unique = list(dict.fromkeys(commits[:6]))
        parts.append(f"Commit work included: {'; '.join(unique[:4])}.")
    return " ".join(parts)


def generate_narrative(prs, commits):
    if not prs and not commits:
        return f"_No activity recorded for {SUMMARY_DATE.strftime('%B %d, %Y')}._"

    pr_block = "\n".join(
        f"- PR #{p['number']} ({p['state']}) [{p['repo']}]: {p['title']}"
        + (f"\n  {p['body'][:200]}" if p["body"].strip() else "")
        for p in prs
    ) or "None"

    commit_block = "\n".join(f"- {m}" for m in commits[:20]) or "None"

    prompt = (
        f"Below is the GitHub activity for {SUMMARY_DATE.strftime('%A, %B %d, %Y')}.\n\n"
        f"Pull Requests:\n{pr_block}\n\n"
        f"Commits:\n{commit_block}\n\n"
        "Write a concise 2–4 sentence narrative work summary. "
        "Describe the theme and purpose of the work, not individual commits. "
        "Mention specific variable names, components, or files only when central to the changes. "
        "Do NOT use bullet points. Write in plain prose as a single paragraph. "
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
                            "You are a concise technical writer summarising a software "
                            "developer's daily GitHub activity. Be specific about what "
                            "was worked on; avoid generic filler sentences."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 250,
                "temperature": 0.3,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"Warning — GitHub Models API unavailable ({e}); using template narrative.", file=sys.stderr)
        return _template_narrative(prs, commits)


# ── Format tables ─────────────────────────────────────────────────────────────
def status_badge(state, is_issue=False):
    return {
        "merged": "🟣 Merged",
        "open":   "🟢 Open",
        "closed": "✅ Closed" if is_issue else "🔴 Closed",
    }.get(state, state.title())


def build_pr_table(prs):
    if not prs:
        return "_No pull request activity for this date._\n"
    rows = [
        "| # | Repository | Title | Status |",
        "|---|------------|-------|--------|",
    ]
    for p in prs:
        rows.append(
            f"| [#{p['number']}]({p['url']}) | {p['repo']} | {p['title']} | {status_badge(p['state'])} |"
        )
    return "\n".join(rows) + "\n"


def build_issue_table(issues):
    if not issues:
        return "_No issue activity for this date._\n"
    rows = [
        "| # | Repository | Title | Status |",
        "|---|------------|-------|--------|",
    ]
    for i in issues:
        lbl = f" `{'` `'.join(i['labels'])}`" if i["labels"] else ""
        rows.append(
            f"| [#{i['number']}]({i['url']}) | {i['repo']} | {i['title']}{lbl} | {status_badge(i['state'], True)} |"
        )
    return "\n".join(rows) + "\n"


# ── Build output ──────────────────────────────────────────────────────────────
narrative = generate_narrative(all_prs, commit_messages)
pr_table  = build_pr_table(all_prs)
iss_table = build_issue_table(all_issues)

output = "\n".join([
    f"## {SUMMARY_DATE.strftime('%B %d, %Y')}\n"
    f"_{SUMMARY_DATE.strftime('%A')}_",
    "",
    "### 🔀 Pull Requests",
    pr_table,
    "### 🐛 Issues",
    iss_table,
    "### 💾 Work Summary",
    narrative,
    "",
    "---",
    "",
])

with open("daily_summary_patch.md", "w") as f:
    f.write(output)

print(
    f"Daily summary written: {SUMMARY_DATE}  |  "
    f"{len(commit_messages)} commits  |  {len(all_prs)} PRs  |  {len(all_issues)} issues"
)
