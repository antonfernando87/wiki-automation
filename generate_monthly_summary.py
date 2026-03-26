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

def collect_branch_work():
    """Collect commits on branches that have no PR, using push events."""
    import re as _re
    SKIP_RE = _re.compile(
        r"^(Merge (pull request|branch|remote-tracking branch|origin|remote)|"
        r"Sync (from|with|branch)|Update(d)? (from|branch|changelog|version)|"
        r"Bump version|Revert \"?Merge )",
        _re.I,
    )
    branch_work: dict = {}   # {"repo/branch": [msg, ...]}
    active_pr_branches: set = set()
    default_branch_cache: dict = {}

    def _default_branch(rf):
        if rf not in default_branch_cache:
            try:
                r = requests.get(f"https://api.github.com/repos/{rf}", headers=GH_HEADERS)
                default_branch_cache[rf] = r.json().get("default_branch", "main")
            except Exception:
                default_branch_cache[rf] = "main"
        return default_branch_cache[rf]

    try:
        for event in gh_get(
            f"https://api.github.com/users/{GITHUB_ACTOR}/events", {"per_page": 100}
        ):
            if event["type"] != "PushEvent":
                continue
            created = parse_iso(event["created_at"])
            if not (created and MONTH_START <= created <= MONTH_END):
                continue
            repo_full = event["repo"]["name"]
            branch    = event["payload"]["ref"].replace("refs/heads/", "")
            msgs = [
                c["message"].splitlines()[0]
                for c in event["payload"].get("commits", [])
                if not SKIP_RE.match(c["message"])
            ]
            if not msgs or branch == _default_branch(repo_full):
                continue
            owner = repo_full.split("/")[0]
            try:
                pr_list = gh_get(
                    f"https://api.github.com/repos/{repo_full}/pulls",
                    {"head": f"{owner}:{branch}", "state": "all"},
                )
            except Exception:
                pr_list = []
            if not pr_list:
                key = f"{repo_full.split('/')[-1]}/{branch}"
                branch_work.setdefault(key, []).extend(msgs)
    except Exception as e:
        print(f"Warning — push events (branch work): {e}", file=sys.stderr)
    return branch_work

# ── Narrative generation ──────────────────────────────────────────────────────
def _template_narrative(prs, commits, branch_work):
    if not prs and not commits and not branch_work:
        return f"No activity was recorded for {MONTH_LABEL}."
    parts = []
    if prs:
        titles = "; ".join(p["title"] for p in prs[:4])
        parts.append(f"Work this month focused on {titles}.")
    if commits:
        msgs = "; ".join(commits[:4])
        parts.append(f"Commit activity included: {msgs}.")
    if branch_work:
        branch_msgs = [m for msgs in branch_work.values() for m in msgs][:3]
        parts.append(f"Branch work (no PR): {'; '.join(branch_msgs)}.")
    return " ".join(parts)

def generate_narrative(prs, commits, branch_work):
    if not prs and not commits and not branch_work:
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

    branch_block = "\n".join(
        f"- [{b}]: {'; '.join(msgs[:3])}"
        for b, msgs in branch_work.items()
    ) or "None"

    prompt = (
        f"Below is the GitHub activity for {MONTH_LABEL}.\n\n"
        f"Merged Pull Requests:\n{pr_block}\n\n"
        f"Commits on PR branches:\n{commit_block}\n\n"
        f"Branch work (commits on branches without a PR):\n{branch_block}\n\n"
        "Write a concise 3–5 sentence narrative summary of the month's work. "
        "Focus on the overall themes and goals, not individual items. "
        "Include work done directly in branches even if no PR was opened. "
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
        return _template_narrative(prs, commits, branch_work)

# ── Write output ──────────────────────────────────────────────────────────────
def write_summary(narrative):
    with open("monthly_summary_patch.md", "w") as f:
        f.write(f"- **{MONTH_LABEL}**: {narrative}\n\n")
    print("✓ Monthly summary written to monthly_summary_patch.md")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"Generating monthly report for {MONTH_LABEL} ({GITHUB_ACTOR})...")

    repos       = discover_repos()
    prs         = collect_merged_prs()
    branch_work = collect_branch_work()

    print(f"  Merged PRs        : {len(prs)}")
    print(f"  Branch-work groups: {len(branch_work)}")

    narrative = generate_narrative(prs, [], branch_work)
    write_summary(narrative)

if __name__ == "__main__":
    main()
