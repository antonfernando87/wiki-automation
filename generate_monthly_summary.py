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
_IGNORE_REPOS        = {r.split("/")[-1] for r in (_cfg.get("ignore_repos") or [])}
_SUMMARY_STYLE        = str(_cfg.get("summary_style",        "narrative")).lower()
_SUMMARY_WORD_LIMIT   = int(_cfg.get("summary_word_limit",   130))
_SUMMARY_BULLET_COUNT = int(_cfg.get("summary_bullet_count", 5))
# ── Summary style overrides from workflow_dispatch inputs ────────────────────
# Env vars set by the workflow take precedence over config.yml values.
_env_style = os.environ.get("SUMMARY_STYLE", "").strip().lower()
if _env_style:
    _SUMMARY_STYLE = _env_style
_env_wl = os.environ.get("SUMMARY_WORD_LIMIT", "").strip()
if _env_wl:
    try:
        _SUMMARY_WORD_LIMIT = int(_env_wl)
    except ValueError:
        pass
_env_bc = os.environ.get("SUMMARY_BULLET_COUNT", "").strip()
if _env_bc:
    try:
        _SUMMARY_BULLET_COUNT = int(_env_bc)
    except ValueError:
        pass


# ── Schedule-disable check ────────────────────────────────────────────────────
if _cfg.get("enable_monthly", True) is False:
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        print("Monthly summary disabled in config.yml — skipping scheduled run.")
        sys.exit(0)


def _should_scan(repo_data):
    name = repo_data["name"]
    if _IGNORE_REPOS and name in _IGNORE_REPOS:
        return False
    if _TRACK_REPOS:
        return name in _TRACK_REPOS
    return True

def _should_include_repo(name):
    """Same logic as _should_scan but accepts a bare repo name string."""
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
            {
                "title":  pr["title"],
                "number": pr["number"],
                "repo":   pr.get("repository_url", "").split("/")[-1],
                "url":    pr["html_url"],
                "body":   (pr.get("body") or "")[:300],
            }
            for pr in items
            if _should_include_repo(pr.get("repository_url", "").split("/")[-1])
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
        default_br = _default_branch(rf)
        try:
            if br == default_br:
                items = gh_get(
                    f"https://api.github.com/repos/{rf}/commits",
                    {"sha": br, "since": MONTH_START.isoformat(),
                     "until": MONTH_END.isoformat(), "author": GITHUB_ACTOR},
                )
            else:
                # Use compare to get only commits unique to this branch (not on default)
                resp = requests.get(
                    f"https://api.github.com/repos/{rf}/compare/{default_br}...{br}",
                    headers=GH_HEADERS,
                    params={"per_page": 250},
                )
                resp.raise_for_status()
                all_unique = resp.json().get("commits", [])
                items = [
                    c for c in all_unique
                    if (c.get("author") or {}).get("login", "").lower() == GITHUB_ACTOR.lower()
                    and MONTH_START <= datetime.fromisoformat(
                        c["commit"]["committer"]["date"].replace("Z", "+00:00")
                    ) <= MONTH_END
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

def collect_created_issues():
    """Issues created this month."""
    last_day = calendar.monthrange(year, month)[1]
    try:
        items = gh_get(
            "https://api.github.com/search/issues",
            {"q": (
                f"author:{GITHUB_ACTOR} is:issue "
                f"created:{year}-{month:02d}-01..{year}-{month:02d}-{last_day:02d}"
            )},
        )
        return [
            {
                "title":  i["title"],
                "number": i["number"],
                "repo":   i.get("repository_url", "").split("/")[-1],
                "url":    i["html_url"],
            }
            for i in items
            if _should_include_repo(i.get("repository_url", "").split("/")[-1])
        ]
    except Exception as e:
        print(f"Warning — created issues: {e}", file=sys.stderr)
        return []

def collect_pr_reviews():
    """PRs reviewed this month (deduped by PR URL).
    Captures formal reviews, inline diff comments, and PR conversation comments.
    """
    reviews = []
    seen: set = set()
    try:
        for event in gh_get(f"https://api.github.com/users/{GITHUB_ACTOR}/events"):
            evt_type = event.get("type", "")
            if evt_type not in ("PullRequestReviewEvent", "PullRequestReviewCommentEvent", "IssueCommentEvent"):
                continue
            ts = event.get("created_at", "")
            if not ts:
                continue
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if not (MONTH_START <= dt <= MONTH_END):
                continue
            payload = event.get("payload", {})
            if evt_type == "IssueCommentEvent":
                # Only capture comments on PRs, not plain issues
                issue = payload.get("issue", {})
                if not issue.get("pull_request"):
                    continue
                pr_url = issue.get("html_url", "")
                if not pr_url or pr_url in seen:
                    continue
                seen.add(pr_url)
                # Skip reviews/comments on your own PRs
                if issue.get("user", {}).get("login", "") == GITHUB_ACTOR:
                    continue
                repo_url = issue.get("repository_url", "")
                repo_name = repo_url.split("/")[-1] if repo_url else ""
                reviews.append({
                    "title":  issue.get("title", ""),
                    "number": issue.get("number"),
                    "repo":   repo_name,
                    "url":    pr_url,
                })
                continue
            pr = payload.get("pull_request", {})
            if not pr or pr.get("html_url") in seen:
                continue
            # Skip dismissed reviews — they are invalidated contributions
            if payload.get("action") == "dismissed":
                continue
            seen.add(pr["html_url"])
            # Skip reviews/comments on your own PRs
            if pr.get("user", {}).get("login", "") == GITHUB_ACTOR:
                continue
            reviews.append({
                "title":  pr.get("title", ""),
                "number": pr.get("number"),
                "repo":   (pr.get("base") or {}).get("repo", {}).get("name", ""),
                "url":    pr.get("html_url", ""),
            })
    except Exception as e:
        print(f"Warning — PR reviews: {e}", file=sys.stderr)
    return [r for r in reviews if _should_include_repo(r["repo"])]

# ── Narrative generation ──────────────────────────────────────────────────────
def _template_narrative(prs, commits, branch_work, created_issues=None, pr_reviews=None):
    created_issues = created_issues or []
    pr_reviews     = pr_reviews or []
    if not prs and not commits and not branch_work and not created_issues and not pr_reviews:
        return f"No activity was recorded for {MONTH_LABEL}."
    parts = []
    if prs:
        titles = "; ".join(p['title'] for p in prs[:4])
        parts.append(f"Work this month focused on {titles}.")
    if commits:
        msgs = "; ".join(commits[:4])
        parts.append(f"Commit activity included: {msgs}.")
    if branch_work:
        branch_msgs = [m for msgs in branch_work.values() for m in msgs][:3]
        parts.append(f"In-progress work: {'; '.join(branch_msgs)}.")
    if created_issues:
        titles = "; ".join(i['title'] for i in created_issues[:3])
        parts.append(f"Issues opened: {titles}.")
    if pr_reviews:
        titles = "; ".join(r['title'] for r in pr_reviews[:3])
        parts.append(f"PRs reviewed: {titles}.")
    return " ".join(parts)

def generate_narrative(prs, commits, branch_work, created_issues=None, pr_reviews=None):
    created_issues = created_issues or []
    pr_reviews     = pr_reviews or []
    if not prs and not commits and not branch_work and not created_issues and not pr_reviews:
        return f"No activity was recorded for {MONTH_LABEL}."

    pr_block = (
        "\n".join(
            f"- {p['title']}"
            + (f"\n  {p['body'][:200]}" if p["body"].strip() else "")
            for p in prs
        )
        or ""
    )
    commit_block = "\n".join(f"- {m}" for m in commits[:30]) or ""

    branch_block = "\n".join(
        f"- Branch {b}: {'; '.join(msgs[:3])}"
        for b, msgs in branch_work.items()
    ) or ""

    issue_block = "\n".join(
        f"- [#{i['number']}]({i['url']}) [{i['repo']}]: {i['title']}"
        for i in created_issues
    ) or ""

    review_block = "\n".join(
        f"- [#{r['number']}]({r['url']}) [{r['repo']}]: {r['title']}"
        for r in pr_reviews
    ) or ""

    # Only include sections that have content so the LLM can't mention empty categories
    activity_sections = []
    if pr_block:
        activity_sections.append(f"Merged Pull Requests:\n{pr_block}")
    if commit_block:
        activity_sections.append(f"Commits on PR branches:\n{commit_block}")
    if branch_block:
        activity_sections.append(f"In-progress work (no PR yet):\n{branch_block}")
    if issue_block:
        activity_sections.append(f"Issues opened this month:\n{issue_block}")
    if review_block:
        activity_sections.append(f"PRs reviewed this month:\n{review_block}")
    activity_text = "\n\n".join(activity_sections) or "No activity recorded."

    if _SUMMARY_STYLE == "bullets":
        prompt = (
            f"Below is the GitHub activity for {MONTH_LABEL}.\n\n"
            f"{activity_text}\n\n"
            f"Write exactly {_SUMMARY_BULLET_COUNT} concise bullet points summarising the month's work — omit the subject pronoun and start each bullet directly with a past-tense verb (e.g. 'Worked on...', not 'I worked on...'). "
            "Only describe the categories listed above — do NOT mention or imply the absence of any category not listed. "
            "Each bullet should cover one high-level theme or area of work. "
            "Do NOT mention PR numbers, issue numbers, commit hashes, or URLs. "
            "When referencing branch work, use the repo name the branch belonged to. "
            "Naturally integrate the repository name into each bullet where relevant "
            "(e.g. 'in global-workflow', 'in GDASApp') so it is clear where each activity occurred. "
            "Output only the bullet list — no headings, no preamble."
        )
    else:
        prompt = (
            f"Below is the GitHub activity for {MONTH_LABEL}.\n\n"
            f"{activity_text}\n\n"
            f"Write a concise narrative summary of the month's work in no more than {_SUMMARY_WORD_LIMIT} words — omit the subject pronoun and start sentences directly with a past-tense verb (e.g. 'Worked on...', not 'I worked on...'). "
            "Only describe the categories listed above — do NOT mention or imply the absence of any category not listed. "
            "Focus on the overall themes and goals, not individual items. "
            "Do NOT mention PR numbers, issue numbers, commit hashes, URLs, or weeks. "
            "Do NOT use bullet points. "
            "Write in plain prose as a single cohesive paragraph. "
            "When referencing branch work, use the repo name the branch belonged to and consider these are ongoing work. "
            "Naturally integrate the repository name into the narrative where relevant "
            "(e.g. 'in global-workflow', 'in GDASApp') so it is clear where each activity occurred. "
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
                            "You are writing a monthly work log entry for a software developer. "
                            "Do NOT use 'I', 'the developer', or 'they' — omit the subject pronoun entirely and begin sentences with a past-tense verb (e.g. 'Worked on...', 'Fixed...', 'Added...'). "
                            "Be specific about what was worked on; avoid generic filler. "
                            "Never mention PR numbers, issue numbers, commit hashes, URLs, or specific week dates."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 300,
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
        return _template_narrative(prs, commits, branch_work, created_issues, pr_reviews)

# ── Write output ──────────────────────────────────────────────────────────────
def write_summary(narrative):
    with open("monthly_summary_patch.md", "w") as f:
        if _SUMMARY_STYLE == "bullets":
            # Indent each bullet so it nests under the month key
            indented = "\n".join("  " + ln if ln.strip() else ln
                                  for ln in narrative.splitlines())
            f.write(f"- **{MONTH_LABEL}**:\n{indented}\n\n")
        else:
            f.write(f"- **{MONTH_LABEL}**: {narrative}\n\n")
    print("✓ Monthly summary written to monthly_summary_patch.md")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"Generating monthly report for {MONTH_LABEL} ({GITHUB_ACTOR})...")

    repos       = discover_repos()
    prs         = collect_merged_prs()
    branch_work = collect_branch_work()
    issues      = collect_created_issues()
    reviews     = collect_pr_reviews()

    print(f"  Merged PRs        : {len(prs)}")
    print(f"  Branch-work groups: {len(branch_work)}")
    print(f"  Issues opened     : {len(issues)}")
    print(f"  PRs reviewed      : {len(reviews)}")

    narrative = generate_narrative(prs, [], branch_work, issues, reviews)
    write_summary(narrative)

if __name__ == "__main__":
    main()
