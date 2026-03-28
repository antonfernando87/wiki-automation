# GitHub Activity Wiki Automation — Setup Guide

Automatically post **daily, weekly, and monthly summaries** of your GitHub
activity (commits, pull requests, and issues) to your repository's wiki.
Summaries are generated as first-person **narrative paragraphs or bullet-point lists** using the GitHub
Models API (gpt-4o-mini) with a plain-text fallback — no external services or
API keys required beyond a standard GitHub PAT.

Great for tracking your own work and producing progress reports with minimal
effort.

---

## What it produces

| Workflow | Schedule | Wiki page |
|----------|----------|-----------|
| Daily summary | Mon–Fri at 23:59 UTC | `Daily-Updates` |
| Weekly summary | Every Friday at 23:59 UTC | `Weekly-Updates` |
| Monthly summary | Last day of each month at 23:59 UTC | `Monthly-Updates` |

Each summary contains:

- **Pull Requests** — all PRs opened (or draft) or merged in the period. Open PRs are
  shown in the table if they were opened **or** had commits pushed in the
  period. Only PRs with real commits appear in the Work Summary narrative.
- **Issues** — issues opened or closed in the period.
- **Work in Progress** — commits pushed to branches that don't yet have a PR,
  grouped by `repo/branch`.
- **PR Review Participation** — PRs authored by others (not your own) where you left a formal
  review, an inline diff comment, or a conversation comment during the period.
- **Work Summary** — an AI-generated first-person narrative covering only the
  activity categories that actually occurred (empty categories are never
  mentioned).

All workflows can also be triggered manually for any date range.

---

## Repository structure

```
your-wiki-automation-repo/
├── .github/workflows/
│   ├── daily-wiki-update.yml
│   ├── weekly-wiki-update.yml
│   └── monthly-wiki-update.yml
├── generate_daily_summary.py
├── generate_weekly_summary.py
├── generate_monthly_summary.py
├── config.yml          <- personal repo filter (optional)
└── README.md
```

---

## Setup

### 1. Fork or copy this repository

Fork `AntonMFernando-NOAA/wiki-automation` into your own GitHub account, or
copy the files into a new repository. The repository must be **public** or you
must have a paid plan for GitHub Actions on private repos.

### 2. Initialize the wiki

GitHub wikis must have at least one page before automation can push to them.

1. Go to your repo → **Wiki** tab → **Create the first page**.
2. Set the title to `Home` and save.

### 3. Create a Personal Access Token (PAT)

1. Go to https://github.com/settings/tokens → **Generate new token (classic)**.
2. Grant scopes: **`repo`** (full control) and **`read:org`**.
3. Set expiry to at least 90 days (or no expiry). **If it expires, the workflow
   will fail silently.**
4. Copy the token.

### 4. Add the PAT as a repository secret

In your repo → **Settings → Secrets and variables → Actions → Secrets → New
repository secret**:

| Name | Value |
|------|-------|
| `WIKI_PAT` | The PAT you just created |

### 5. (Optional) Set your GitHub username as a variable

By default the scripts track the account that owns the repository. To track a
different username, add a repository variable:

In your repo → **Settings → Secrets and variables → Actions → Variables → New
repository variable**:

| Name | Value |
|------|-------|
| `GITHUB_ACTOR` | Your GitHub username (e.g. `octocat`) |

### 6. Enable Actions with write permissions

In your repo → **Settings → Actions → General → Workflow permissions**:
- Select **Read and write permissions** → **Save**.

This is required for two reasons: the workflows push updates to the wiki, and they also commit any changes to tracked repo files (e.g. `README.md`, `prompt_instructions.txt`) back to the main branch automatically after each run.

### 7. Enable the workflows

The workflow files in `.github/workflows/` will be picked up automatically by
GitHub Actions. You can verify they appear under the **Actions** tab of your
repo.

---

## Environment variables reference

| Variable | Set via | Purpose |
|----------|---------|---------|
| `GH_TOKEN` | `secrets.WIKI_PAT` | GitHub API access + GitHub Models narrative generation |
| `GITHUB_ACTOR` | `vars.GITHUB_ACTOR` (optional) | GitHub username to track; defaults to repo owner |
| `SUMMARY_DATE` | Manual workflow input (optional) | Override the target date; defaults to yesterday |
| `WEEK_START` | Manual workflow input (optional) | Override the week start date (weekly workflow) |
| `REPORT_MONTH` | Manual workflow input (optional) | Override the report month `YYYY-MM` (monthly workflow) |
| `SUMMARY_STYLE` | Manual workflow input (optional) | Override summary style (`narrative` or `bullets`) for this run |
| `SUMMARY_WORD_LIMIT` | Manual workflow input (optional) | Override max words for narrative for this run |
| `SUMMARY_BULLET_COUNT` | Manual workflow input (optional) | Override number of bullet points for this run |

No hardcoded repository list is needed. PRs and issues are found via GitHub's
search API across all repositories. Commits are scanned per-branch across your
40 most-recently-updated non-archived repos by default (or all repos when
`track_repos` is set in `config.yml`).

---

## Personal configuration — `config.yml`

`config.yml` (at the repo root) lets you control which repositories are
scanned without touching the scripts. All keys are optional.

```yaml
# If non-empty, ONLY these repos are scanned (no 40-repo cap).
# Accepts "owner/name" or bare "name".
track_repos:
  - AntonMFernando-NOAA/global-workflow
  - ufs-weather-model

# Always excluded, regardless of track_repos.
ignore_repos:
  - test-automation
  - some-fork

# Disable specific scheduled (cron) runs.
# Manual workflow_dispatch triggers are NEVER blocked.
# Default: true (all run on schedule).
enable_daily:   true
enable_weekly:  true
enable_monthly: true

# Summary style: "narrative" (default) or "bullets".
# summary_word_limit   applies when style is "narrative" (default: 130).
# summary_bullet_count applies when style is "bullets"  (default: 5).
summary_style: narrative
summary_word_limit: 130
summary_bullet_count: 5
```

| Key | Default | Behaviour |
|-----|---------|----------|
| `track_repos` | `[]` (empty) | Empty = auto-scan the 40 most-recently-updated repos. Non-empty = explicit allowlist, no cap. |
| `ignore_repos` | `[]` (empty) | Repos in this list are always skipped. |
| `enable_daily` | `true` | Set to `false` to prevent the daily cron from running. |
| `enable_weekly` | `true` | Set to `false` to prevent the weekly cron from running. |
| `enable_monthly` | `true` | Set to `false` to prevent the monthly cron from running. |
| `summary_style` | `narrative` | `narrative` = prose paragraph; `bullets` = bullet-point list. |
| `summary_word_limit` | `130` | Maximum words in the narrative paragraph. |
| `summary_bullet_count` | `5` | Number of bullet points when style is `bullets`. |

> **Note:** `pyyaml` must be available in the runner environment for
> `config.yml` to be loaded. If it is missing, the scripts fall back to
> default behaviour and print a warning. Install it with
> `pip install pyyaml` or add it to the workflow's dependency step.

---

## Manual and backfill runs

All three workflows can be triggered manually from the GitHub UI.

### Steps

1. Go to your repository on GitHub.
2. Click the **Actions** tab.
3. Select the workflow you want to run from the left sidebar:
   - **Daily Wiki Update**
   - **Weekly Wiki Update**
   - **Monthly Wiki Update**
4. Click **Run workflow** (top-right of the workflow run list).
5. Fill in the input fields (all are optional — leave blank for defaults).
6. Click the green **Run workflow** button.

### Daily workflow inputs

| Input | Example | Effect |
|-------|---------|--------|
| Date | `2026-03-20` | Summarise that specific day. Leave blank for yesterday. |
| Summary style | `bullets` | `narrative` or `bullets`. Leave blank to use `config.yml`. |
| Max words (narrative) | `100` | Word limit for narrative style. Leave blank for `config.yml` value. |
| Bullet count | `4` | Number of bullet points. Leave blank for `config.yml` value. |

### Weekly workflow inputs

| Input | Example | Effect |
|-------|---------|--------|
| Week start | `2026-03-16` | Any date within the target week. Leave blank for the current week. |
| Summary style | `narrative` | `narrative` or `bullets`. Leave blank to use `config.yml`. |
| Max words (narrative) | `130` | Word limit for narrative style. Leave blank for `config.yml` value. |
| Bullet count | `5` | Number of bullet points. Leave blank for `config.yml` value. |

### Monthly workflow inputs

| Input | Example | Effect |
|-------|---------|--------|
| Month | `2026-02` | Month to summarise in `YYYY-MM` format. Leave blank for last month. |
| Summary style | `bullets` | `narrative` or `bullets`. Leave blank to use `config.yml`. |
| Max words (narrative) | `130` | Word limit for narrative style. Leave blank for `config.yml` value. |
| Bullet count | `6` | Number of bullet points. Leave blank for `config.yml` value. |

> **Note:** Workflow dispatch inputs override `config.yml` for that single run only.
> The permanent default is always whatever is set in `config.yml`.
>
> **Running from a custom branch (forks):** Select your branch from the **Use workflow from** dropdown when triggering manually. For scheduled runs to pick up your branch automatically, set it as the **default branch** of your fork (Repository → Settings → Default branch).

---

## How it works

```
Scheduled trigger (or manual)
        |
        v
GitHub Actions runner
  1. Checks out the repository
  2. Runs the summary script
        |-- Reads config.yml (track_repos / ignore_repos)
        |-- Searches GitHub for PRs and issues authored by GITHUB_ACTOR
        |   (two searches per type: updated in window + created in window,
        |   deduplicated — ensures backfill runs capture PRs that have
        |   been updated after the target date)
        |-- Collects PR review participation via the Events API
        |   (PullRequestReviewEvent, PullRequestReviewCommentEvent,
        |   and IssueCommentEvent on PRs — covers approvals, inline diff
        |   comments, and regular conversation comments)
        |   Only PRs authored by others are included; reviews/comments on
        |   your own PRs are excluded
        |-- Scans every branch in every qualifying repo for commits
        |   authored by GITHUB_ACTOR within the time window
        |   (merge commits, sync commits, and automated bumps are filtered)
        |     |-- Commits on a PR branch  --> attributed to that PR
        |     |   (fork-based PRs: fetches commits from the fork repo,
        |     |   not the base repo, so cross-org PRs are captured)
        |     +-- Commits on a branch without a PR --> collected as Work in Progress
        |-- Open PRs with no real commits are kept in the PR table
        |   (draft PRs always appear in the table on the day they were opened)
        |   but omitted from the Work Summary narrative
        |-- Calls GitHub Models API (gpt-4o-mini) for a first-person
        |   narrative paragraph (falls back to a template if unavailable)
        +-- Writes <type>_summary_patch.md
  3. Clones <your-repo>.wiki.git
  4. Prepends the new entry to the relevant wiki page
  5. Commits and pushes the wiki
```

### Commit filtering

The following commit messages are automatically excluded from summaries to
avoid noise from automated processes and merge operations:

- `Merge ...` / `Merged ...` (all merge commits)
- `Sync from ...` / `Sync to ...` / `Sync with branch ...`
- `Update from ...` / `Updated branch ...` / `Update changelog` / `Update submodule`
- `Bump version` / `Bump deps` / `Bump dependencies`
- `Revert "Merge ..."`
- `Auto-generated ...` / `Autogenerated ...`
- `chore: release` / `chore: bump` / `chore(scope): version`

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `Error: GH_TOKEN is not set` | `WIKI_PAT` secret missing or expired | Regenerate PAT and update the secret |
| `403` on `git push` to wiki | Workflow permissions not set to read/write | Settings → Actions → Workflow permissions → Read and write |
| `fatal: could not read from remote` | Wiki not initialised | Create at least one wiki page manually first |
| No activity in summary | PAT lacks `repo` or `read:org` scope | Regenerate PAT with the correct scopes |
| Workflow not visible under Actions | Workflow YAML not in `.github/workflows/` | Confirm files are committed to the default branch |
| `config.yml` ignored or warning printed | `pyyaml` not installed | Add `pip install pyyaml` to the workflow's setup step |
| Draft PR or new PR missing on backfill | PR was updated after the target date, so `updated:` search misses it | The scripts also run a `created:` search — ensure the PAT has `repo` scope on the repo where the PR lives |
| Scheduled run fires but writes nothing | `enable_daily` / `enable_weekly` / `enable_monthly` set to `false` in `config.yml` | Expected behaviour — set back to `true` or trigger manually |

---

## Customising

| What | Where |
|------|-------|
| Restrict which repos are scanned | Edit `track_repos` / `ignore_repos` in `config.yml` |
| Change schedule | Edit the `cron` expression in the relevant workflow YAML |
| Track a different user | Set the `GITHUB_ACTOR` repository variable |
| Change wiki page names | Edit the filename references in the workflow's push step |
| Adjust narrative style | Edit the system prompt string inside `generate_*.py` |
| Add more noise-commit patterns | Extend the `SKIP_RE` regex near the top of each `generate_*.py` |
