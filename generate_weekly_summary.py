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
if _cfg.get("enable_weekly", True) is False:
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        print("Weekly summary disabled in config.yml — skipping scheduled run.")
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
            "draft":       False,
            "created_at":  item.get("created_at", ""),
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
            "draft":       item.get("draft", False),
            "created_at":  item.get("created_at", ""),
            "branch":      "",
            "body":        (item.get("body") or "")[:300],
            "url":         item["html_url"],
        })
except Exception as e:
    print(f"Warning — open PRs search: {e}", file=sys.stderr)

# Open PRs created this week (catches PRs that have since been updated —
# important for backfill runs and for draft PRs opened during the week)
_seen_urls = {p["url"] for p in all_prs}
try:
    for item in gh_get(
        "https://api.github.com/search/issues",
        {"q": f"author:{GITHUB_ACTOR} is:pr is:open created:{start_str}..{end_str}"},
    ):
        if item["html_url"] in _seen_urls:
            continue
        repo = item.get("repository_url", "").replace("https://api.github.com/repos/", "")
        all_prs.append({
            "repo":        repo.split("/")[-1],
            "repo_full":   repo,
            "number":      item["number"],
            "title":       item["title"],
            "state":       "open",
            "draft":       item.get("draft", False),
            "created_at":  item.get("created_at", ""),
            "branch":      "",
            "body":        (item.get("body") or "")[:300],
            "url":         item["html_url"],
        })
except Exception as e:
    print(f"Warning — open PRs (created this week): {e}", file=sys.stderr)

# PRs converted from draft to ready-for-review this week (via Events API).
# The search API misses these on backfill runs because updated_at has since
# advanced past the target date. The Events API records the exact transition.
_seen_urls_rfr = {p["url"] for p in all_prs}
try:
    for event in gh_get(f"https://api.github.com/users/{GITHUB_ACTOR}/events"):
        if event.get("type") != "PullRequestEvent":
            continue
        payload = event.get("payload", {})
        if payload.get("action") != "ready_for_review":
            continue
        if not in_window(event.get("created_at", "")):
            continue
        pr = payload.get("pull_request", {})
        if not pr or pr.get("html_url") in _seen_urls_rfr:
            continue
        base_repo = (pr.get("base") or {}).get("repo") or {}
        all_prs.append({
            "repo":          base_repo.get("name", ""),
            "repo_full":     base_repo.get("full_name", ""),
            "number":        pr["number"],
            "title":         pr["title"],
            "state":         "open",
            "draft":         False,
            "had_rfr_event": True,
            "created_at":    pr.get("created_at", ""),
            "branch":        (pr.get("head") or {}).get("ref", ""),
            "body":          (pr.get("body") or "")[:300],
            "url":           pr["html_url"],
        })
        _seen_urls_rfr.add(pr["html_url"])
except Exception as e:
    print(f"Warning — ready_for_review events: {e}", file=sys.stderr)

# Deduplicate all_prs by URL keeping the highest-precedence state.
# If the same PR appears via multiple searches (e.g. merged: AND updated:,
# or created-as-draft AND rfr event), keep: merge > open > draft.
_PR_PRIORITY = {"merged": 3, "open": 2, "draft": 1}
_pr_by_url: dict = {}
for _p in all_prs:
    _sk = "draft" if _p.get("draft") else _p["state"]
    _pri = _PR_PRIORITY.get(_sk, 0)
    _existing = _pr_by_url.get(_p["url"])
    if _existing is None:
        _pr_by_url[_p["url"]] = _p
    else:
        _esk = "draft" if _existing.get("draft") else _existing["state"]
        if _pri > _PR_PRIORITY.get(_esk, 0):
            _pr_by_url[_p["url"]] = _p
all_prs = list(_pr_by_url.values())
all_prs = [p for p in all_prs if _should_include_repo(p["repo"])]

# ── Issues & PR reviews collection ─────────────────────────────────────────
# Issues created this week
all_issues: list = []
try:
    for item in gh_get(
        "https://api.github.com/search/issues",
        {"q": f"author:{GITHUB_ACTOR} is:issue created:{start_str}..{end_str}"},
    ):
        repo = item.get("repository_url", "").replace("https://api.github.com/repos/", "")
        all_issues.append({
            "repo":   repo.split("/")[-1],
            "number": item["number"],
            "title":  item["title"],
            "state":  item["state"],
            "url":    item["html_url"],
        })
except Exception as e:
    print(f"Warning — created issues: {e}", file=sys.stderr)
all_issues = [i for i in all_issues if _should_include_repo(i["repo"])]

# PR reviews submitted this week (via Events API).
# Captures formal reviews, inline diff comments, and PR conversation comments.
pr_reviews: list = []
_seen_review_keys: set = set()
try:
    for event in gh_get(f"https://api.github.com/users/{GITHUB_ACTOR}/events"):
        evt_type = event.get("type", "")
        if evt_type not in ("PullRequestReviewEvent", "PullRequestReviewCommentEvent", "IssueCommentEvent"):
            continue
        if not in_window(event.get("created_at", "")):
            continue
        payload = event.get("payload", {})
        if evt_type == "IssueCommentEvent":
            # Only capture comments on PRs, not plain issues
            issue = payload.get("issue", {})
            if not issue.get("pull_request"):
                continue
            pr_url = issue.get("html_url", "")
            if not pr_url or pr_url in _seen_review_keys:
                continue
            _seen_review_keys.add(pr_url)
            # Skip reviews/comments on your own PRs
            if issue.get("user", {}).get("login", "") == GITHUB_ACTOR:
                continue
            repo_url = issue.get("repository_url", "")
            repo_name = repo_url.split("/")[-1] if repo_url else ""
            pr_reviews.append({
                "number": issue.get("number"),
                "title":  issue.get("title", ""),
                "repo":   repo_name,
                "url":    pr_url,
                "state":  "commented",
            })
            continue
        pr = payload.get("pull_request", {})
        if not pr:
            continue
        key = pr.get("html_url")
        if key in _seen_review_keys:
            continue
        _seen_review_keys.add(key)
        # Skip reviews/comments on your own PRs
        if pr.get("user", {}).get("login", "") == GITHUB_ACTOR:
            continue
        if evt_type == "PullRequestReviewEvent":
            state = payload.get("review", {}).get("state", "commented").lower()
        else:
            state = "commented"
        pr_reviews.append({
            "number": pr.get("number"),
            "title":  pr.get("title", ""),
            "repo":   (pr.get("base") or {}).get("repo", {}).get("name", ""),
            "url":    pr.get("html_url", ""),
            "state":  state,
        })
except Exception as e:
    print(f"Warning — PR reviews: {e}", file=sys.stderr)
pr_reviews = [r for r in pr_reviews if _should_include_repo(r["repo"])]

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
        commit_messages.extend(
            f"[{repo_data['name']}]: {m}" for m in _branch_msgs(repo_full, default_br)
        )

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
                commit_messages.extend(f"[{repo_data['name']}]: {m}" for m in msgs)
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
        head        = detail.get("head", {})
        branch      = head.get("ref", "")
        # For fork-based PRs the branch lives on the fork, not the base repo
        head_repo_full = (head.get("repo") or {}).get("full_name") or p["repo_full"]
        p["branch"]      = branch
        if (head_repo_full, branch) in active_pr_branches:
            p["had_commits"] = True
        elif branch:
            # PR may be in a repo the user contributes to but doesn't own — scan it directly
            msgs = _branch_msgs(head_repo_full, branch)
            p["had_commits"] = bool(msgs)
            if msgs:
                active_pr_branches.add((head_repo_full, branch))
                commit_messages.extend(
                    f"[{head_repo_full.split('/')[-1]}]: {m}" for m in msgs
                )
        else:
            p["had_commits"] = False
    except Exception:
        p["had_commits"] = True  # safe default: include in narrative

# ── Narrative via GitHub Models ───────────────────────────────────────────────
def _template_narrative(prs, commits, branch_work, created_issues=None, pr_reviews=None):
    narrative_prs = [p for p in prs if p.get("had_commits", True) or p.get("had_rfr_event")]
    created_issues = created_issues or []
    pr_reviews     = pr_reviews or []
    if not narrative_prs and not commits and not branch_work and not created_issues and not pr_reviews:
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
    if commits:
        unique = list(dict.fromkeys(commits[:8]))
        parts.append(f"Commit work included: {'; '.join(unique[:4])}.")
    if branch_work:
        branch_msgs = [m for msgs in branch_work.values() for m in msgs][:3]
        parts.append(f"Branch work (no PR): {'; '.join(branch_msgs)}.")
    if created_issues:
        titles = "; ".join(f"[#{i['number']}]({i['url']}) {i['title'][:60]}" for i in created_issues[:3])
        parts.append(f"Issues opened: {titles}.")
    if pr_reviews:
        titles = "; ".join(f"[#{r['number']}]({r['url']}) {r['title'][:60]}" for r in pr_reviews[:3])
        parts.append(f"PRs reviewed: {titles}.")
    pr_summary = []
    if merged_prs:
        pr_summary.append(f"{len(merged_prs)} PR{'s' if len(merged_prs) > 1 else ''} merged")
    if open_prs:
        pr_summary.append(f"{len(open_prs)} PR{'s' if len(open_prs) > 1 else ''} open with commits")
    if pr_summary and repos_mentioned:
        parts.append(f"Overall, {' and '.join(pr_summary)} across {', '.join(repos_mentioned)}.")
    return " ".join(parts)


def generate_narrative(prs, commits, branch_work, created_issues=None, pr_reviews=None):
    created_issues = created_issues or []
    pr_reviews     = pr_reviews or []
    narrative_prs = [p for p in prs if p.get("had_commits", True) or p.get("had_rfr_event")]
    if not narrative_prs and not commits and not branch_work and not created_issues and not pr_reviews:
        return (
            f"No activity was recorded for the week of "
            f"{MONDAY.strftime('%B %d')}–{FRIDAY.strftime('%B %d, %Y')}."
        )

    pr_block     = "\n".join(
        f"- [#{p['number']}]({p['url']}) ({p['state']}) [{p['repo']}]: {p['title']}"
        + (f"\n  {p['body'][:200]}" if p["body"].strip() else "")
        for p in narrative_prs
    ) or ""
    commit_block = "\n".join(f"- {m}" for m in commits[:25]) or ""
    branch_block = "\n".join(
        f"- Branch {b}: {'; '.join(msgs[:3])}"
        for b, msgs in branch_work.items()
    ) or ""
    issue_block  = "\n".join(
        f"- [#{i['number']}]({i['url']}) [{i['repo']}]: {i['title']}"
        for i in created_issues
    ) or ""
    review_block = "\n".join(
        f"- [#{r['number']}]({r['url']}) [{r['repo']}]: {r['title']} ({r['state']})"
        for r in pr_reviews
    ) or ""

    # Only include sections that have content so the LLM can't mention empty categories
    activity_sections = []
    if pr_block:
        activity_sections.append(f"Pull Requests (with commits this week):\n{pr_block}")
    if commit_block:
        activity_sections.append(f"Commits on PR branches:\n{commit_block}")
    if branch_block:
        activity_sections.append(f"Branch work (no PR yet):\n{branch_block}")
    if issue_block:
        activity_sections.append(f"Issues opened this week:\n{issue_block}")
    if review_block:
        activity_sections.append(f"PRs reviewed this week:\n{review_block}")
    activity_text = "\n\n".join(activity_sections) or "No activity recorded."

    if _SUMMARY_STYLE == "bullets":
        prompt = (
            f"Below is the GitHub activity for the week of "
            f"{MONDAY.strftime('%B %d')}–{FRIDAY.strftime('%B %d, %Y')}.\n\n"
            f"{activity_text}\n\n"
            f"Write exactly {_SUMMARY_BULLET_COUNT} concise bullet points summarising the key tasks this week — omit the subject pronoun and start each bullet directly with a past-tense verb (e.g. 'Worked on...', not 'I worked on...'). "
            "Only describe the categories listed above — do NOT mention or imply the absence of any category not listed. "
            "Each bullet should cover one distinct task or theme. "
            "When referencing a PR or issue, use its markdown link exactly as given in the input (e.g. [#123](url)). "
            "When referencing branch work, use the repository name the branch belonged to and describe the tasks as in-progress. "
            "Naturally integrate the repository name into each bullet where relevant "
            "(e.g. 'in global-workflow', 'in GDASApp') so it is clear where each activity occurred. "
            "Output only the bullet list — no headings, no preamble."
        )
    else:
        prompt = (
            f"Below is the GitHub activity for the week of "
            f"{MONDAY.strftime('%B %d')}–{FRIDAY.strftime('%B %d, %Y')}.\n\n"
            f"{activity_text}\n\n"
            f"Write a concise narrative work summary in no more than {_SUMMARY_WORD_LIMIT} words — omit the subject pronoun and start sentences directly with a past-tense verb (e.g. 'Worked on...', not 'I worked on...'). "
            "Only describe the categories listed above — do NOT mention or imply the absence of any category not listed. "
            "Focus on the themes and goals of the work, not individual commits. "
            "When referencing a PR or issue, use its markdown link exactly as given in the input (e.g. [#123](url)). "
            "Do NOT use bullet points. Write in plain prose as a single cohesive paragraph. "
            "When referencing branch work, use the repository name branch belonged to and describe the tasks as in-progress. "
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
                            "You are writing a weekly work log entry for a software developer. "
                            "Do NOT use 'I', 'the developer', or 'they' — omit the subject pronoun entirely and begin sentences with a past-tense verb (e.g. 'Worked on...', 'Fixed...', 'Added...'). "
                            "Be specific about what was worked on; avoid generic filler sentences."
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
        print(f"Warning — GitHub Models API unavailable ({e}); using template narrative.", file=sys.stderr)
        return _template_narrative(prs, commits, branch_work, created_issues, pr_reviews)


# ── PR table ──────────────────────────────────────────────────────────────────
def status_label(state):
    return {
        "open":   "🟢 Open",
        "merged": "🟣 Merged",
        "draft":  "⬜ Draft",
        "closed": "🔴 Closed",
    }.get(state, state.title())


def build_pr_table(prs):
    if not prs:
        return "_No pull requests this week._\n"
    rows = [
        "| # | Repository | Title | Status |",
        "|---|------------|-------|--------|",
    ]
    for p in prs:
        state_key = "draft" if p.get("draft") else p["state"]
        rows.append(
            f"| [#{p['number']}]({p['url']}) | {p['repo']} | {p['title']} | {status_label(state_key)} |"
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
narrative    = generate_narrative(all_prs, commit_messages, branch_work_commits, all_issues, pr_reviews)
pr_table     = build_pr_table([p for p in all_prs
                               if p.get("had_commits", True)
                               or in_window(p.get("created_at", ""))
                               or p.get("had_rfr_event")])
branch_table = build_branch_work_table(branch_work_commits)

sections = [
    f"## Week of {MONDAY.strftime('%B')} {MONDAY.day}–{FRIDAY.day}, {FRIDAY.year}\n"
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
