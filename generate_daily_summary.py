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
            "repo":        repo.split("/")[-1],
            "repo_full":   repo,
            "number":      item["number"],
            "title":       item["title"],
            "state":       "merged",
            "had_commits": True,
            "branch":      "",
            "body":        (item.get("body") or "")[:300],
            "url":         item["html_url"],
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
            "repo":        repo.split("/")[-1],
            "repo_full":   repo,
            "number":      item["number"],
            "title":       item["title"],
            "state":       "open",
            "had_commits": None,  # determined after push-event scan
            "branch":      "",
            "body":        (item.get("body") or "")[:300],
            "url":         item["html_url"],
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

# ── Commit & branch-work collection via push events ─────────────────────────
# Filters out merge/sync/update-branch noise commits
SKIP_RE = re.compile(
    r"^(Merge (pull request|branch|remote-tracking branch|origin|remote)|"
    r"Sync (from|with|branch)|Update(d)? (from|branch|changelog|version)|"
    r"Bump version|Revert \"?Merge )",
    re.I,
)

commit_messages    = []
branch_work_commits: dict = {}   # {"repo/branch": [msg, ...]} – branches with no PR
active_pr_branches: set  = set() # (repo_full, branch) pushed in window + has a PR
_default_branch_cache: dict = {}  # repo_full -> default branch name


def _default_branch(repo_full):
    """Return the default branch name for a repo (cached)."""
    if repo_full not in _default_branch_cache:
        try:
            r = requests.get(
                f"https://api.github.com/repos/{repo_full}", headers=GH_HEADERS
            )
            _default_branch_cache[repo_full] = r.json().get("default_branch", "main")
        except Exception:
            _default_branch_cache[repo_full] = "main"
    return _default_branch_cache[repo_full]


try:
    for event in gh_get(
        f"https://api.github.com/users/{GITHUB_ACTOR}/events", {"per_page": 100}
    ):
        if event["type"] != "PushEvent":
            continue
        created = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))
        if not (DAY_START <= created <= DAY_END):
            continue
        repo_full = event["repo"]["name"]
        branch    = event["payload"]["ref"].replace("refs/heads/", "")
        msgs = [
            c["message"].splitlines()[0]
            for c in event["payload"].get("commits", [])
            if not SKIP_RE.match(c["message"])
        ]
        if not msgs:
            continue
        # Commits directly to the default branch go straight to commit_messages
        if branch == _default_branch(repo_full):
            commit_messages.extend(msgs)
            continue
        # For other branches: check if a PR exists (open or merged)
        owner = repo_full.split("/")[0]
        try:
            pr_list = gh_get(
                f"https://api.github.com/repos/{repo_full}/pulls",
                {"head": f"{owner}:{branch}", "state": "all"},
            )
        except Exception:
            pr_list = []
        if pr_list:
            active_pr_branches.add((repo_full, branch))
            commit_messages.extend(msgs)
        else:
            key = f"{repo_full.split('/')[-1]}/{branch}"
            branch_work_commits.setdefault(key, []).extend(msgs)
except Exception as e:
    print(f"Warning — push events: {e}", file=sys.stderr)

# Mark which open PRs had commits pushed in this window (for narrative filtering)
for p in all_prs:
    if p["state"] != "open":
        continue
    try:
        detail = requests.get(
            f"https://api.github.com/repos/{p['repo_full']}/pulls/{p['number']}",
            headers=GH_HEADERS,
        ).json()
        branch = detail.get("head", {}).get("ref", "")
        p["branch"]      = branch
        p["had_commits"] = bool(branch) and (p["repo_full"], branch) in active_pr_branches
    except Exception:
        p["had_commits"] = True  # default: include if check fails

# ── Narrative generation ──────────────────────────────────────────────────────
def _template_narrative(prs, commits, branch_work):
    narrative_prs = [p for p in prs if p.get("had_commits", True)]
    if not narrative_prs and not commits and not branch_work:
        return f"_No activity recorded for {SUMMARY_DATE.strftime('%B %d, %Y')}._"
    parts = []
    if narrative_prs:
        titles = "; ".join(p["title"][:70] for p in narrative_prs[:3])
        parts.append(f"Pull request activity centred on: {titles}.")
    if commits:
        unique = list(dict.fromkeys(commits[:6]))
        parts.append(f"Commit work included: {'; '.join(unique[:4])}.")
    if branch_work:
        branch_msgs = [m for msgs in branch_work.values() for m in msgs][:4]
        parts.append(f"Branch work (no PR): {'; '.join(branch_msgs)}.")
    return " ".join(parts)


def generate_narrative(prs, commits, branch_work):
    # Only include open PRs that had commits pushed in the window
    narrative_prs = [p for p in prs if p.get("had_commits", True)]
    if not narrative_prs and not commits and not branch_work:
        return f"_No activity recorded for {SUMMARY_DATE.strftime('%B %d, %Y')}._"

    pr_block = "\n".join(
        f"- PR #{p['number']} ({p['state']}) [{p['repo']}]: {p['title']}"
        + (f"\n  {p['body'][:200]}" if p["body"].strip() else "")
        for p in narrative_prs
    ) or "None"

    commit_block = "\n".join(f"- {m}" for m in commits[:20]) or "None"

    branch_block = "\n".join(
        f"- [{b}]: {'; '.join(msgs[:3])}"
        for b, msgs in branch_work.items()
    ) or "None"

    prompt = (
        f"Below is the GitHub activity for {SUMMARY_DATE.strftime('%A, %B %d, %Y')}.\n\n"
        f"Pull Requests (with commits today):\n{pr_block}\n\n"
        f"Commits on PR branches:\n{commit_block}\n\n"
        f"Branch work (commits on branches without a PR):\n{branch_block}\n\n"
        "Write a concise 2–4 sentence narrative work summary. "
        "Describe the theme and purpose of the work, not individual commits. "
        "Include work done directly in branches even if no PR exists yet. "
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
        return _template_narrative(prs, commits, branch_work)


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


def build_branch_work_table(branch_work):
    """Table of commits on branches that have no associated PR."""
    if not branch_work:
        return ""
    rows = [
        "| Branch | Work Commits |",
        "|--------|--------------|"  ,
    ]
    for branch_key, msgs in branch_work.items():
        unique_msgs = list(dict.fromkeys(msgs))[:4]
        rows.append(f"| `{branch_key}` | {' · '.join(unique_msgs)} |")
    return "\n".join(rows) + "\n"


# ── Build output ──────────────────────────────────────────────────────────────
narrative    = generate_narrative(all_prs, commit_messages, branch_work_commits)
pr_table     = build_pr_table(all_prs)
iss_table    = build_issue_table(all_issues)
branch_table = build_branch_work_table(branch_work_commits)

sections = [
    f"## {SUMMARY_DATE.strftime('%B %d, %Y')}\n"
    f"_{SUMMARY_DATE.strftime('%A')}_",
    "",
    "### 🔀 Pull Requests",
    pr_table,
    "### 🐛 Issues",
    iss_table,
]
if branch_table:
    sections += ["### 🌿 Branch Work", branch_table, ""]
sections += ["### 💾 Work Summary", narrative, "", "---", ""]

output = "\n".join(sections)

with open("daily_summary_patch.md", "w") as f:
    f.write(output)

print(
    f"Daily summary written: {SUMMARY_DATE}  |  "
    f"{len(commit_messages)} PR-branch commits  |  {len(branch_work_commits)} branch-work groups  |  "
    f"{len(all_prs)} PRs  |  {len(all_issues)} issues"
)
