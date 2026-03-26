#!/usr/bin/env python3
"""
Generate a weekly summary with a PR table and a narrative work summary.

The narrative is generated via the GitHub Models API (gpt-4o-mini) and falls
back to a template-based paragraph when the API is unavailable.

Environment variables:
    GH_TOKEN        PAT with repo read scope (also used for GitHub Models).
    GITHUB_ACTOR    GitHub username to track (default: repository owner).
    WEEK_START      Any date within the target week (YYYY-MM-DD). Defaults to the current week.
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
    _provided = date.fromisoformat(WEEK_START_STR)
    MONDAY = _provided - timedelta(days=_provided.weekday())  # normalise to Monday
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
    print(f"Warning — merged PRs search: {e}", file=sys.stderr)

# Open PRs updated this week
try:
    for item in gh_get(
        "https://api.github.com/search/issues",
        {"q": f"author:{GITHUB_ACTOR} is:pr is:open updated:{start_str}..{end_str}"},
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
    print(f"Warning — open PRs search: {e}", file=sys.stderr)

# ── Commit & branch-work collection (full repo+branch scan) ──────────────────
# Skip any merge/sync/automated commit — filter broadly so stale branch noise
# never leaks into the work summary
SKIP_RE = re.compile(
    r"^Merged?\b|"                                    # all merge commits
    r"^Sync (from|with|to|branch)|"                   # sync commits
    r"^Update(d)? (from|branch|changelog|version|submodule)|"  # update noise
    r"^Bump (version|deps?|dependencies)|"            # version bumps
    r"^Revert .{0,30}[Mm]erge|"                       # merge reverts
    r"^Auto.?generated|^chore(\(.*\))?:\s*(release|bump|version)",
    re.I,
)

commit_messages    = []
branch_work_commits: dict = {}   # {"repo/branch": [msg, ...]} – no-PR branches
active_pr_branches: set  = set() # (repo_full, branch) that have commits in window + a PR
_default_branch_cache: dict = {}


def _default_branch(repo_full):
    if repo_full not in _default_branch_cache:
        try:
            r = requests.get(
                f"https://api.github.com/repos/{repo_full}", headers=GH_HEADERS
            )
            _default_branch_cache[repo_full] = r.json().get("default_branch", "main")
        except Exception:
            _default_branch_cache[repo_full] = "main"
    return _default_branch_cache[repo_full]


def _branch_msgs(repo_full, branch):
    """Return commits by GITHUB_ACTOR that are unique to this branch
    (not reachable from the default branch) within the week window, noise-filtered.
    For the default branch itself, falls back to the standard commits endpoint.
    """
    default_br = _default_branch(repo_full)
    try:
        if branch == default_br:
            items = gh_get(
                f"https://api.github.com/repos/{repo_full}/commits",
                {"sha": branch, "since": WEEK_START.isoformat(),
                 "until": WEEK_END.isoformat(), "author": GITHUB_ACTOR},
            )
        else:
            # Use compare to get only commits unique to this branch (not on default)
            resp = requests.get(
                f"https://api.github.com/repos/{repo_full}/compare/{default_br}...{branch}",
                headers=GH_HEADERS,
                params={"per_page": 250},
            )
            resp.raise_for_status()
            all_unique = resp.json().get("commits", [])
            # Filter by author login and time window
            items = [
                c for c in all_unique
                if (c.get("author") or {}).get("login", "").lower() == GITHUB_ACTOR.lower()
                and WEEK_START <= datetime.fromisoformat(
                    c["commit"]["committer"]["date"].replace("Z", "+00:00")
                ) <= WEEK_END
            ]
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

        # Default branch commits
        commit_messages.extend(_branch_msgs(repo_full, default_br))

        # All other branches
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
            if pr_list:
                active_pr_branches.add((repo_full, branch))
                commit_messages.extend(msgs)
            else:
                key = f"{repo_data['name']}/{branch}"
                branch_work_commits.setdefault(key, []).extend(msgs)
except Exception as e:
    print(f"Warning — repo/branch scan: {e}", file=sys.stderr)

# Mark which open PRs had commits in this window (for narrative filtering)
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
        if (p["repo_full"], branch) in active_pr_branches:
            p["had_commits"] = True
        elif branch:
            # PR may be in a repo the user contributes to but doesn't own — scan it directly
            msgs = _branch_msgs(p["repo_full"], branch)
            p["had_commits"] = bool(msgs)
            if msgs:
                active_pr_branches.add((p["repo_full"], branch))
                commit_messages.extend(msgs)
        else:
            p["had_commits"] = False
    except Exception:
        p["had_commits"] = True  # safe default: include in narrative

# ── Narrative via GitHub Models ───────────────────────────────────────────────
def _template_narrative(prs, commits, branch_work):
    narrative_prs = [p for p in prs if p.get("had_commits", True)]
    if not narrative_prs and not commits and not branch_work:
        return (
            f"No activity was recorded for the week of "
            f"{MONDAY.strftime('%B %d')}–{FRIDAY.strftime('%B %d, %Y')}."
        )
    repos_mentioned = sorted({p["repo"] for p in narrative_prs})
    open_prs   = [p for p in narrative_prs if p["state"] == "open"]
    merged_prs = [p for p in narrative_prs if p["state"] == "merged"]
    parts = []
    titles_short = "; ".join(f"[#{p['number']}]({p['url']}) {p['title'][:60]}" for p in narrative_prs[:3])
    if titles_short:
        parts.append(f"This week's work covered: {titles_short}.")
    if branch_work:
        branch_msgs = [m for msgs in branch_work.values() for m in msgs][:3]
        parts.append(f"Branch work (no PR): {'; '.join(branch_msgs)}.")
    pr_summary = []
    if merged_prs:
        pr_summary.append(f"{len(merged_prs)} PR{'s' if len(merged_prs) > 1 else ''} merged")
    if open_prs:
        pr_summary.append(f"{len(open_prs)} PR{'s' if len(open_prs) > 1 else ''} open with commits")
    if pr_summary and repos_mentioned:
        parts.append(f"Overall, {' and '.join(pr_summary)} across {', '.join(repos_mentioned)}.")
    return " ".join(parts)


def generate_narrative(prs, commits, branch_work):
    narrative_prs = [p for p in prs if p.get("had_commits", True)]
    if not narrative_prs and not commits and not branch_work:
        return (
            f"No activity was recorded for the week of "
            f"{MONDAY.strftime('%B %d')}–{FRIDAY.strftime('%B %d, %Y')}."
        )

    pr_block = "\n".join(
        f"- [#{p['number']}]({p['url']}) ({p['state']}) [{p['repo']}]: {p['title']}"
        + (f"\n  {p['body'][:200]}" if p["body"].strip() else "")
        for p in narrative_prs
    ) or "None"

    commit_block = "\n".join(f"- {m}" for m in commits[:25]) or "None"

    branch_block = "\n".join(
        f"- [{b}]: {'; '.join(msgs[:3])}"
        for b, msgs in branch_work.items()
    ) or "None"

    prompt = (
        f"Below is the GitHub activity for the week of "
        f"{MONDAY.strftime('%B %d')}–{FRIDAY.strftime('%B %d, %Y')}.\n\n"
        f"Pull Requests (with commits this week):\n{pr_block}\n\n"
        f"Commits on PR branches:\n{commit_block}\n\n"
        f"Branch work (commits on branches without a PR):\n{branch_block}\n\n"
        "Write a concise 3–5 sentence first-person narrative work summary (use 'I', not 'the developer'). "
        "Focus on the themes and goals of the work, not individual commits. "
        "Include work done directly in branches even if no PR exists yet. "
        "Mention specific variable names, file types, or components only when they "
        "are central to the descriptions. "
        "When referencing a PR, use its markdown link exactly as given in the input (e.g. [#123](url)). "
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
                            "You are writing a first-person weekly work log entry for a software developer. "
                            "Write as 'I' — never say 'the developer' or 'they'. "
                            "Be specific about what was worked on; avoid generic filler sentences."
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
        return _template_narrative(prs, commits, branch_work)


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


def build_branch_work_table(branch_work):
    if not branch_work:
        return ""
    rows = [
        "| Branch | Work Description |",
        "|--------|------------------|",
    ]
    for branch_key, msgs in branch_work.items():
        unique_msgs = list(dict.fromkeys(msgs))
        # Truncate each message and join up to 3
        snippets = [m[:72] + ("…" if len(m) > 72 else "") for m in unique_msgs[:3]]
        description = " · ".join(snippets)
        rows.append(f"| `{branch_key}` | {description} |")
    return "\n".join(rows) + "\n"


# ── Build output ──────────────────────────────────────────────────────────────
narrative    = generate_narrative(all_prs, commit_messages, branch_work_commits)
pr_table     = build_pr_table(all_prs)
branch_table = build_branch_work_table(branch_work_commits)

sections = [
    f"## Week of {MONDAY.strftime('%B %d')}–{FRIDAY.strftime('%d, %Y')}\n"
    f"_Automatically maintained log of weekly GitHub activity._",
    "",
    "### 🔀 Pull Requests",
    pr_table,
]
if branch_table:
    sections += ["### 🌿 Branch Work", branch_table, ""]
sections += ["### 💾 Work Summary", narrative, "", "---", ""]

output = "\n".join(sections)

with open("weekly_summary_patch.md", "w") as f:
    f.write(output)

print(
    f"Weekly summary written: {MONDAY} to {FRIDAY}  |  "
    f"{len(all_prs)} PRs  |  {len(commit_messages)} PR-branch commits  |  {len(branch_work_commits)} branch-work groups"
)
