#!/usr/bin/env python3
"""
Generate a monthly progress report in the format:

  ## Progress Report, workflow for operational GEFS
  ### <Month Year>

  **Major Accomplishments**
  - bullet ... (3-5)

  **Blockers / Issues**
  - bullet ... (1-3)

Environment variables:
    GH_TOKEN        PAT with repo read scope.
    GITHUB_ACTOR    GitHub username to track (default: repository owner)
    REPORT_MONTH    ISO year-month (YYYY-MM). Defaults to last month.
"""

import os
import sys
import requests
from datetime import date, timedelta, timezone, datetime
from collections import defaultdict
import calendar

# ── Config ───────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("GH_TOKEN", "")
if not TOKEN:
    sys.exit("Error: GH_TOKEN is not set.")

GITHUB_ACTOR = os.environ.get("GITHUB_ACTOR") or os.environ.get("GITHUB_REPOSITORY_OWNER", "")
REPORT_MONTH_STR = os.environ.get("REPORT_MONTH", "").strip()

# Default to last month
if REPORT_MONTH_STR:
    year, month = map(int, REPORT_MONTH_STR.split("-"))
else:
    today = date.today()
    first_of_this_month = today.replace(day=1)
    last_month = first_of_this_month - timedelta(days=1)
    year, month = last_month.year, last_month.month

MONTH_START = datetime(year, month, 1, 0, 0, 0, tzinfo=timezone.utc)
MONTH_END   = datetime(year, month, calendar.monthrange(year, month)[1], 23, 59, 59, tzinfo=timezone.utc)

MONTH_LABEL = date(year, month, 1).strftime("%B %Y")
DATE_STR    = f"{year}-{month:02d}"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# ── Helpers ───────────────────────────────────────────────────────────────────
def gh_get(url, params=None):
    """Paginated GitHub API GET."""
    results, p = [], {"per_page": 100, **(params or {})}
    while url:
        r = requests.get(url, headers=HEADERS, params=p)
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
    url = f"https://api.github.com/users/{GITHUB_ACTOR}/repos"
    repos = gh_get(url, {"type": "all", "sort": "updated"})
    return [f"{r['owner']['login']}/{r['name']}" for r in repos if not r.get("archived", False)]

# ── Data Collection ───────────────────────────────────────────────────────────
def collect_merged_prs():
    """All PRs authored by GITHUB_ACTOR merged this month (any org)."""
    items = gh_get(
        "https://api.github.com/search/issues",
        {"q": f"author:{GITHUB_ACTOR} is:pr is:merged merged:{DATE_STR}-01..{DATE_STR}-{calendar.monthrange(year, month)[1]:02d}"},
    )
    prs = []
    for pr in items:
        repo = pr.get("repository_url", "").replace("https://api.github.com/repos/", "")
        prs.append({"repo": repo, "number": pr["number"], "title": pr["title"], "url": pr["html_url"]})
    return prs

def collect_open_prs():
    """PRs by GITHUB_ACTOR still open at end of month (across all orgs)."""
    items = gh_get(
        "https://api.github.com/search/issues",
        {"q": f"author:{GITHUB_ACTOR} is:pr is:open"},
    )
    prs = []
    for pr in items:
        repo = pr.get("repository_url", "").replace("https://api.github.com/repos/", "")
        created = parse_iso(pr.get("created_at"))
        # Only include if opened on or before end of this month
        if created and created <= MONTH_END:
            prs.append({"repo": repo, "number": pr["number"], "title": pr["title"], "url": pr["html_url"], "created_at": created})
    return prs

def collect_open_issues():
    """Issues created by GITHUB_ACTOR that are still open."""
    items = gh_get(
        "https://api.github.com/search/issues",
        {"q": f"author:{GITHUB_ACTOR} is:issue is:open"},
    )
    issues = []
    for i in items:
        repo = i.get("repository_url", "").replace("https://api.github.com/repos/", "")
        created = parse_iso(i.get("created_at"))
        if created and created <= MONTH_END:
            issues.append({"repo": repo, "number": i["number"], "title": i["title"], "url": i["html_url"]})
    return issues

def collect_commits(repos):
    """Commits authored by GITHUB_ACTOR across all owned repos this month."""
    commits = []
    for repo in repos:
        try:
            items = gh_get(
                f"https://api.github.com/repos/{repo}/commits",
                {"since": MONTH_START.isoformat(), "until": MONTH_END.isoformat(), "author": GITHUB_ACTOR},
            )
            for c in items:
                dt = parse_iso(c["commit"]["author"]["date"])
                if dt and MONTH_START <= dt <= MONTH_END:
                    commits.append({
                        "repo": repo,
                        "sha": c["sha"][:7],
                        "message": c["commit"]["message"].split("\n")[0],
                        "url": c["html_url"],
                    })
        except Exception as e:
            print(f"Warning: {repo}: {e}", file=sys.stderr)
    return commits

# ── Bullet generation ──────────────────────────────────────────────────────────
# Keywords used to group activity into themes
_THEME_KEYWORDS = [
    ("EE2 compliance / environment variable rename",   ["ee2", "rename", "homeglobal", "homegfs", "homeverif", "vargfs", "nco", "compliance"]),
    ("Submodule / dependency updates",                 ["submodule", "ufs_utils", "ufs-utils", "hash", "gsi", "gsm", "gdas", "jedi"]),
    ("Automation / wiki tooling",                      ["wiki", "automation", "summary", "schedule", "workflow_dispatch", "cron", "weekly", "daily", "monthly"]),
    ("CI / testing infrastructure",                    ["test", "ci", "build", "compile", "unit", "consistency", "cmake"]),
    ("Documentation / code owners",                   ["doc", "readme", "owner", "codeowner", "copilot", "mcp", "guidance"]),
    ("Bug fixes",                                      ["fix", "bug", "error", "hotfix", "patch", "revert"]),
]

def _theme(text):
    t = text.lower()
    for label, kws in _THEME_KEYWORDS:
        if any(k in t for k in kws):
            return label
    return "General workflow / feature development"

def build_accomplishment_bullets(merged_prs, commits):
    """
    Produce 3-5 accomplishment bullets by grouping merged PRs + commits
    into themes, then writing one concise sentence per theme.
    """
    theme_prs     = defaultdict(list)
    theme_commits = defaultdict(list)

    for pr in merged_prs:
        theme_prs[_theme(pr["title"])].append(pr)
    for c in commits:
        # Only include commits not already captured by a merged PR
        theme_commits[_theme(c["message"])].append(c)

    # Merge theme sets
    all_themes = sorted(set(list(theme_prs.keys()) + list(theme_commits.keys())))

    bullets = []
    for theme in all_themes:
        prs_here     = theme_prs.get(theme, [])
        commits_here = theme_commits.get(theme, [])

        # Build a concise sentence
        parts = []
        if prs_here:
            pr_links = ", ".join(
                f"[{p['repo'].split('/')[-1]}#{p['number']}]({p['url']})"
                for p in prs_here[:3]
            )
            suffix = f" (+{len(prs_here)-3} more)" if len(prs_here) > 3 else ""
            parts.append(f"{len(prs_here)} PR{'s' if len(prs_here)>1 else ''} merged ({pr_links}{suffix})")
        if commits_here:
            repo_names = sorted({c['repo'].split('/')[-1] for c in commits_here})
            parts.append(f"{len(commits_here)} commit{'s' if len(commits_here)>1 else ''} in {', '.join(repo_names)}")

        if parts:
            bullets.append(f"**{theme}**: {'; '.join(parts)}.")

    # Clamp 3-5
    if len(bullets) > 5:
        bullets = bullets[:5]
    elif len(bullets) < 3 and not bullets:
        bullets = ["No significant activity recorded this month."]

    return bullets

def build_blocker_bullets(open_prs, open_issues):
    """
    Produce 1-3 blocker bullets from open PRs (pending review / merge)
    and open issues still unresolved.
    """
    bullets = []

    # Long-running open PRs (open for > 7 days relative to month end)
    stale_prs = [
        p for p in open_prs
        if p["created_at"] and (MONTH_END - p["created_at"]).days >= 7
    ]
    if stale_prs:
        pr_links = ", ".join(
            f"[{p['repo'].split('/')[-1]}#{p['number']}]({p['url']})"
            for p in stale_prs[:3]
        )
        bullets.append(
            f"{len(stale_prs)} PR{'s' if len(stale_prs)>1 else ''} "
            f"pending review/merge ({pr_links})."
        )

    # Newly opened PRs (< 7 days old) — in progress, not yet a blocker
    new_prs = [p for p in open_prs if p not in stale_prs]
    if new_prs:
        pr_links = ", ".join(
            f"[{p['repo'].split('/')[-1]}#{p['number']}]({p['url']})"
            for p in new_prs[:3]
        )
        bullets.append(f"{len(new_prs)} PR{'s' if len(new_prs)>1 else ''} in progress ({pr_links}).")

    # Open issues
    if open_issues:
        issue_links = ", ".join(
            f"[{i['repo'].split('/')[-1]}#{i['number']}]({i['url']}): {i['title']}"
            for i in open_issues[:3]
        )
        bullets.append(f"{len(open_issues)} open issue{'s' if len(open_issues)>1 else ''}: {issue_links}.")

    if not bullets:
        bullets = ["No outstanding blockers or open issues."]

    return bullets[:3]

# ── Write output ──────────────────────────────────────────────────────────────
def write_summary(accomplishments, blockers, merged_prs, open_prs, open_issues):
    lines = []
    lines.append("## Progress Report, workflow for operational GEFS\n")
    lines.append(f"### {MONTH_LABEL}\n")
    lines.append("\n")
    lines.append("**Major Accomplishments**\n")
    for b in accomplishments:
        lines.append(f"- {b}\n")
    lines.append("\n")
    lines.append("**Blockers / Issues**\n")
    for b in blockers:
        lines.append(f"- {b}\n")

    # Details collapsible
    if merged_prs or open_prs or open_issues:
        lines.append("\n<details>\n<summary>Details</summary>\n")

        if merged_prs:
            lines.append("\n### Merged Pull Requests\n")
            for pr in merged_prs:
                lines.append(f"- [{pr['repo'].split('/')[-1]}#{pr['number']}]({pr['url']}): {pr['title']}\n")

        if open_prs:
            lines.append("\n### Open Pull Requests\n")
            for pr in open_prs:
                lines.append(f"- [{pr['repo'].split('/')[-1]}#{pr['number']}]({pr['url']}): {pr['title']}\n")

        if open_issues:
            lines.append("\n### Open Issues\n")
            for i in open_issues:
                lines.append(f"- [{i['repo'].split('/')[-1]}#{i['number']}]({i['url']}): {i['title']}\n")

        lines.append("\n</details>\n")

    lines.append("\n---\n\n")

    with open("monthly_summary_patch.md", "w") as f:
        f.writelines(lines)

    print("✓ Monthly summary written to monthly_summary_patch.md")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"Generating monthly report for {MONTH_LABEL} ({GITHUB_ACTOR})...")

    repos        = discover_repos()
    merged_prs   = collect_merged_prs()
    open_prs     = collect_open_prs()
    open_issues  = collect_open_issues()
    commits      = collect_commits(repos)

    accomplishments = build_accomplishment_bullets(merged_prs, commits)
    blockers        = build_blocker_bullets(open_prs, open_issues)

    print(f"  Merged PRs : {len(merged_prs)}")
    print(f"  Open PRs   : {len(open_prs)}")
    print(f"  Open issues: {len(open_issues)}")
    print(f"  Commits    : {len(commits)}")

    write_summary(accomplishments, blockers, merged_prs, open_prs, open_issues)

if __name__ == "__main__":
    main()
