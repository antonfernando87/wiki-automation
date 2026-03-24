#!/usr/bin/env python3
"""
Generate a weekly summary with a PR table and a narrative work summary.

The narrative is generated via the GitHub Models API (gpt-4o-mini) and falls
back to a template-based paragraph when the API is unavailable.

Environment variables:
    GH_TOKEN        PAT with repo read scope (also used for GitHub Models).
    GITHUB_ACTOR    GitHub username to track (default: repository owner).
    WEEK_START      ISO date (YYYY-MM-DD) of the Monday. Defaults to last Monday.
"""

import os, sys, re, requests
from datetime import date, timedelta, timezone, datetime
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("GH_TOKEN", "")
if not TOKEN:
    sys.exit("Error: GH_TOKEN is not set.")

GITHUB_ACTOR = os.environ.get("GITHUB_ACTOR") or os.environ.get("GITHUB_REPOSITORY_OWNER", "")

WEEK_START_STR = os.environ.get("WEEK_START", "").strip()
if WEEK_START_STR:
    MONDAY = date.fromisoformat(WEEK_START_STR)
else:
    today = date.today()
    MONDAY = today - timedelta(days=today.weekday())  # last Monday

FRIDAY = MONDAY + timedelta(days=4)

WEEK_START = datetime(MONDAY.year, MONDAY.month, MONDAY.day,  0,  0,  0, tzinfo=timezone.utc)
WEEK_END   = datetime(FRIDAY.year, FRIDAY.month,  FRIDAY.day, 23, 59, 59, tzinfo=timezone.utc)

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
    return WEEK_START <= dt <= WEEK_END

# ── Collect PRs via search API ────────────────────────────────────────────────
all_prs = []

# Merged this week
try:
    start_str = MONDAY.strftime("%Y-%m-%d")
    end_str   = FRIDAY.strftime("%Y-%m-%d")
    for item in gh_get(
        "https://api.github.com/search/issues",
        {"q": f"author:{GITHUB_ACTOR} is:pr is:merged merged:{start_str}..{end_str}"},
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
    print(f"Warning — merged PRs search: {e}", file=sys.stderr)

# Open PRs updated this week
try:
    for item in gh_get(
        "https://api.github.com/search/issues",
        {"q": f"author:{GITHUB_ACTOR} is:pr is:open updated:{start_str}..{end_str}"},
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
    print(f"Warning — open PRs search: {e}", file=sys.stderr)

# ── Collect commits for narrative context ─────────────────────────────────────
commit_messages = []
try:
    repos = gh_get(
        f"https://api.github.com/users/{GITHUB_ACTOR}/repos",
        {"type": "all", "sort": "updated"},
    )
    for repo_data in repos[:20]:  # cap at 20 most-recently-updated repos
        if repo_data.get("archived"):
            continue
        repo_full = f"{repo_data['owner']['login']}/{repo_data['name']}"
        try:
            for c in gh_get(
                f"https://api.github.com/repos/{repo_full}/commits",
                {"since": WEEK_START.isoformat(), "until": WEEK_END.isoformat(), "author": GITHUB_ACTOR},
            ):
                msg = c["commit"]["message"].splitlines()[0]
                if not re.match(r"^merge\b", msg, re.I):
                    commit_messages.append(msg)
        except Exception:
            pass
except Exception as e:
    print(f"Warning — commits: {e}", file=sys.stderr)

# ── Narrative via GitHub Models ───────────────────────────────────────────────
def _template_narrative(prs):
    if not prs:
        return (
            f"No pull request activity was recorded for the week of "
            f"{MONDAY.strftime('%B %d')}–{FRIDAY.strftime('%B %d, %Y')}."
        )
    repos_mentioned = sorted({p["repo"] for p in prs})
    open_prs   = [p for p in prs if p["state"] == "open"]
    merged_prs = [p for p in prs if p["state"] == "merged"]

    parts = []
    titles_short = "; ".join(p["title"][:70] for p in prs[:3])
    parts.append(f"This week's work covered: {titles_short}.")
    pr_summary = []
    if merged_prs:
        pr_summary.append(f"{len(merged_prs)} PR{'s' if len(merged_prs) > 1 else ''} merged")
    if open_prs:
        pr_summary.append(f"{len(open_prs)} PR{'s' if len(open_prs) > 1 else ''} open")
    if pr_summary:
        parts.append(f"Overall, {' and '.join(pr_summary)} across {', '.join(repos_mentioned)}.")
    return " ".join(parts)


def generate_narrative(prs, commits):
    if not prs and not commits:
        return (
            f"No activity was recorded for the week of "
            f"{MONDAY.strftime('%B %d')}–{FRIDAY.strftime('%B %d, %Y')}."
        )

    pr_block = "\n".join(
        f"- PR #{p['number']} ({p['state']}) [{p['repo']}]: {p['title']}"
        + (f"\n  {p['body'][:200]}" if p["body"].strip() else "")
        for p in prs
    ) or "None"

    commit_block = "\n".join(f"- {m}" for m in commits[:25]) or "None"

    prompt = (
        f"Below is the GitHub activity for the week of "
        f"{MONDAY.strftime('%B %d')}–{FRIDAY.strftime('%B %d, %Y')}.\n\n"
        f"Pull Requests:\n{pr_block}\n\n"
        f"Recent commits:\n{commit_block}\n\n"
        "Write a concise 3–5 sentence narrative work summary. "
        "Focus on the themes and goals of the work, not individual commits. "
        "Mention specific variable names, file types, or components only when they "
        "are central to the PR descriptions. "
        "Do NOT use bullet points. Write in plain prose as a single cohesive paragraph. "
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
                            "developer's weekly GitHub activity. Be specific about what "
                            "was worked on; avoid generic filler sentences."
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
        print(f"Warning — GitHub Models API unavailable ({e}); using template narrative.", file=sys.stderr)
        return _template_narrative(prs)


# ── PR table ──────────────────────────────────────────────────────────────────
def status_label(state):
    return {"open": "🟢 Open", "merged": "🟣 Merged", "closed": "🔴 Closed"}.get(state, state.title())


def build_pr_table(prs):
    if not prs:
        return "_No pull requests this week._\n"
    rows = [
        "| # | Repository | Title | Status |",
        "|---|------------|-------|--------|",
    ]
    for p in prs:
        rows.append(
            f"| [#{p['number']}]({p['url']}) | {p['repo']} | {p['title']} | {status_label(p['state'])} |"
        )
    return "\n".join(rows) + "\n"


# ── Build output ──────────────────────────────────────────────────────────────
narrative = generate_narrative(all_prs, commit_messages)
pr_table  = build_pr_table(all_prs)

output = "\n".join([
    f"## Week of {MONDAY.strftime('%B %d')}–{FRIDAY.strftime('%d, %Y')}\n"
    f"_Automatically maintained log of weekly GitHub activity._",
    "",
    "### 🔀 Pull Requests",
    pr_table,
    "### 💾 Work Summary",
    narrative,
    "",
    "---",
    "",
])

with open("weekly_summary_patch.md", "w") as f:
    f.write(output)

print(
    f"Weekly summary written: {MONDAY} to {FRIDAY}  |  "
    f"{len(all_prs)} PRs  |  {len(commit_messages)} commits"
)
