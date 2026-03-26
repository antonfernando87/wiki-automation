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

import os, sys, re, requests, calendar
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

# ── Personal config (config.yml) ──────────────────────────────────────────────
try:
    import yaml as _yaml
    _cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yml")
    try:
        with open(_cfg_path) as _f:
            _cfg = _yaml.safe_load(_f) or {}
    except FileNotFoundError:
        _cfg = {}
    except Exception as _e:
        print(f"Warning — config.yml: {_e}", file=sys.stderr)
        _cfg = {}
except ImportError:
    _cfg = {}

_TRACK_REPOS  = {r.split("/")[-1] for r in (_cfg.get("track_repos") or [])}
_IGNORE_REPOS = {r.split("/")[-1] for r in (_cfg.get("ignore_repos") or [])}


def _should_scan(repo_data):
    name = repo_data["name"]
    if _IGNORE_REPOS and name in _IGNORE_REPOS:
        return False
    if _TRACK_REPOS:
        return name in _TRACK_REPOS
    return True


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
    """Collect commits on all branches across all repos that have no open/merged PR."""
    SKIP_RE = re.compile(
        r"^Merged?\b|"                                    # all merge commits
        r"^Sync (from|with|to|branch)|"                   # sync commits
        r"^Update(d)? (from|branch|changelog|version|submodule)|"  # update noise
        r"^Bump (version|deps?|dependencies)|"            # version bumps
        r"^Revert .{0,30}[Mm]erge|"                       # merge reverts
        r"^Auto.?generated|^chore(\(.*\))?:\s*(release|bump|version)",
        re.I,
    )
    branch_work: dict = {}
    default_branch_cache: dict = {}

    def _default_branch(rf):
        if rf not in default_branch_cache:
            try:
                r = requests.get(f"https://api.github.com/repos/{rf}", headers=GH_HEADERS)
                default_branch_cache[rf] = r.json().get("default_branch", "main")
            except Exception:
                default_branch_cache[rf] = "main"
        return default_branch_cache[rf]

    def _branch_msgs(rf, br):
        try:
            items = gh_get(
                f"https://api.github.com/repos/{rf}/commits",
                {"sha": br, "since": MONTH_START.isoformat(),
                 "until": MONTH_END.isoformat(), "author": GITHUB_ACTOR},
            )
            return [
                c["commit"]["message"].splitlines()[0]
                for c in items
                if not SKIP_RE.match(c["commit"]["message"])
            ]
        except Exception:
            return []

    try:
        all_repos = gh_get(
            f"https://api.github.com/users/{GITHUB_ACTOR}/repos",
            {"type": "all", "sort": "updated"},
        )
        _repo_pool = all_repos if _TRACK_REPOS else all_repos[:40]
        for repo_data in _repo_pool:
            if repo_data.get("archived") or not _should_scan(repo_data):
                continue
            repo_full  = f"{repo_data['owner']['login']}/{repo_data['name']}"
            default_br = _default_branch(repo_full)
            owner      = repo_full.split("/")[0]
            try:
                branches = gh_get(f"https://api.github.com/repos/{repo_full}/branches")
            except Exception:
                branches = []
            for br_info in branches:
                branch = br_info["name"]
                if branch == default_br:
                    continue
                msgs = _branch_msgs(repo_full, branch)
                if not msgs:
                    continue
                try:
                    pr_list = gh_get(
                        f"https://api.github.com/repos/{repo_full}/pulls",
                        {"head": f"{owner}:{branch}", "state": "all"},
                    )
                except Exception:
                    pr_list = []
                if not pr_list:
                    key = f"{repo_data['name']}/{branch}"
                    branch_work.setdefault(key, []).extend(msgs)
    except Exception as e:
        print(f"Warning — repo/branch scan: {e}", file=sys.stderr)
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
        "Write a concise 3–5 sentence first-person narrative summary of the month's work (use 'I', not 'the developer'). "
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
                            "You are writing a first-person monthly work log entry for a software developer. "
                            "Write as 'I' — never say 'the developer' or 'they'. "
                            "Be specific about what was worked on; avoid generic filler. "
                            "Never mention PR numbers, issue numbers, commit hashes, URLs, or specific week dates."
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
