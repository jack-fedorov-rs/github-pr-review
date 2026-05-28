#!/usr/bin/env python3
"""
GitHub PR Review Distribution Analyzer

Usage:
    export FETCH_TOKEN=ghp_xxx
    python analyze_reviews.py                    # all teams from config.yaml
    python analyze_reviews.py --team backend     # one team only
    python analyze_reviews.py --config my.yaml   # custom config file
    python analyze_reviews.py --refresh          # fetch only new PRs since last run
    python analyze_reviews.py --no-pause         # skip interactive sanity check
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run: pip install requests pyyaml")

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency. Run: pip install requests pyyaml")

GRAPHQL_URL = "https://api.github.com/graphql"
RAW_DIR     = Path("raw_data")

# ============================================================
# GRAPHQL QUERY
# ============================================================
QUERY = """
query($owner: String!, $repo: String!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequests(
      first: 50
      after: $after
      orderBy: { field: CREATED_AT, direction: DESC }
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number url
        author { login }
        createdAt mergedAt closedAt state isDraft
        additions deletions changedFiles
        reviews(first: 100) {
          nodes { author { login } state submittedAt }
        }
        reviewRequests(first: 50) {
          nodes { requestedReviewer { ... on User { login } } }
        }
        timelineItems(first: 100, itemTypes: [REVIEW_REQUESTED_EVENT]) {
          nodes {
            ... on ReviewRequestedEvent {
              createdAt
              requestedReviewer { ... on User { login } }
            }
          }
        }
      }
    }
  }
  rateLimit { remaining cost resetAt }
}
"""

# ============================================================
# HELPERS
# ============================================================

def make_is_bot(patterns: List[str]):
    def is_bot(login: str) -> bool:
        if not login:
            return True
        ll = login.lower()
        return any(p in ll for p in patterns)
    return is_bot


def parse_dt(s: Optional[str]) -> Optional[datetime.datetime]:
    if not s:
        return None
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def safe_median(values: List[float]) -> Optional[float]:
    return statistics.median(values) if values else None


def safe_p75(values: List[float]) -> Optional[float]:
    if not values:
        return None
    sv = sorted(values)
    return sv[min(int(len(sv) * 0.75), len(sv) - 1)]


def fmt_h(val: Optional[float]) -> str:
    return f"{val:.1f}" if val is not None else "—"


def md_table(headers: List[str], rows: List[list]) -> str:
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for i, c in enumerate(row):
            widths[i] = max(widths[i], len(str(c)))

    def fmt_row(r: list) -> str:
        return "| " + " | ".join(str(r[i]).ljust(widths[i]) for i in range(len(headers))) + " |"

    sep = "| " + " | ".join("-" * w for w in widths) + " |"
    return "\n".join([fmt_row(headers), sep] + [fmt_row(r) for r in rows])


def write_csv(path: Path, rows: List[dict], fieldnames: List[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Written: {path}  ({len(rows)} rows)")


# ============================================================
# CONFIG
# ============================================================

def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    for field in ("org", "repos", "teams"):
        if field not in cfg:
            sys.exit(f"Config error: missing required field '{field}'")
    if not cfg["teams"]:
        sys.exit("Config error: 'teams' must not be empty")

    cfg.setdefault("period_days", 365)
    cfg.setdefault("recent_window_days", 60)
    cfg.setdefault("bot_patterns", ["[bot]", "dependabot", "renovate", "github-actions", "copilot"])

    for team_name, team in cfg["teams"].items():
        if "members" not in team:
            sys.exit(f"Config error: team '{team_name}' missing 'members'")
        team.setdefault("repos", cfg["repos"])

    return cfg


# ============================================================
# PREFLIGHT
# ============================================================

def preflight_check(token: str, org: str, repos: List[str]) -> None:
    print("Preflight checks...")
    headers = {"Authorization": f"bearer {token}", "Content-Type": "application/json"}

    resp = requests.post(
        GRAPHQL_URL, headers=headers,
        json={"query": "{ viewer { login } rateLimit { remaining } }"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        sys.exit(f"Token error: {data['errors']}")

    viewer    = data["data"]["viewer"]["login"]
    remaining = data["data"]["rateLimit"]["remaining"]
    print(f"  Authenticated as: {viewer}  (rate limit: {remaining})")

    failed = []
    for repo in repos:
        result = requests.post(
            GRAPHQL_URL, headers=headers,
            json={"query": f'{{ repository(owner: "{org}", name: "{repo}") {{ name }} }}'},
            timeout=30,
        ).json()
        if "errors" in result or not (result.get("data") or {}).get("repository"):
            failed.append(repo)
            print(f"  ✗ {org}/{repo}")
        else:
            print(f"  ✓ {org}/{repo}")

    if failed:
        sys.exit(
            f"\nCannot access: {failed}\n"
            "Check SAML SSO authorization for your token at https://github.com/settings/tokens"
        )
    print("Preflight OK\n")


# ============================================================
# COLLECTION
# ============================================================

def _gql(token: str, variables: dict) -> dict:
    headers = {"Authorization": f"bearer {token}", "Content-Type": "application/json"}
    for attempt in range(4):
        try:
            resp = requests.post(
                GRAPHQL_URL, headers=headers,
                json={"query": QUERY, "variables": variables},
                timeout=90,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (502, 503, 504) and attempt < 3:
                wait = 2 ** (attempt + 1)
                print(f"  [retry {attempt+1}] HTTP {e.response.status_code} — waiting {wait}s")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Max retries exceeded")


def _check_rate(rate: dict) -> None:
    if rate["remaining"] < 200:
        reset_time = parse_dt(rate["resetAt"])
        now = datetime.datetime.now(datetime.timezone.utc)
        if reset_time:
            secs = (reset_time - now).total_seconds() + 10
            if secs > 0:
                print(f"  [rate-limit] {rate['remaining']} remaining — sleeping {secs:.0f}s until {rate['resetAt']}")
                time.sleep(secs)


def collect_repo(repo: str, org: str, token: str, period_days: int, refresh: bool = False) -> List[dict]:
    state_file = RAW_DIR / f"{repo}_prs.json"
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=period_days)

    if state_file.exists():
        state = json.loads(state_file.read_text(encoding="utf-8"))
        existing_prs    = state.get("prs", [])
        existing_numbers: Set[int] = {pr["number"] for pr in existing_prs}

        if state.get("complete") and not refresh:
            print(f"{repo}: complete ({len(existing_prs)} PRs cached). Use --refresh to update.")
            return existing_prs

        if refresh and state.get("complete"):
            print(f"{repo}: refreshing — fetching PRs newer than last run...")
            cursor: Optional[str] = None
            prs = existing_prs
        else:
            cursor = state.get("cursor")
            prs    = existing_prs
            if cursor:
                print(f"{repo}: resuming from saved cursor ({len(prs)} PRs so far).")
    else:
        cursor, prs, existing_numbers = None, [], set()

    page = 0
    while True:
        page += 1
        variables: dict = {"owner": org, "repo": repo}
        if cursor:
            variables["after"] = cursor

        data = _gql(token, variables)
        if "errors" in data:
            sys.exit(f"GraphQL error for {repo}: {data['errors']}")

        rate      = data["data"]["rateLimit"]
        pr_conn   = data["data"]["repository"]["pullRequests"]
        page_info = pr_conn["pageInfo"]
        nodes: List[dict] = pr_conn["nodes"]

        new_prs: List[dict] = []
        reached_cutoff = False
        null_count = 0

        for pr in nodes:
            created = parse_dt(pr["createdAt"])
            if created is None or created < since:
                reached_cutoff = True
                break
            if refresh and pr["number"] in existing_numbers:
                reached_cutoff = True
                break
            if pr.get("author") is None:
                null_count += 1
            new_prs.append(pr)

        if null_count:
            print(f"  [warn] {null_count} PR(s) with null author (deleted accounts) — will be skipped in analysis")

        if refresh:
            new_numbers = {pr["number"] for pr in new_prs}
            prs = new_prs + [p for p in prs if p["number"] not in new_numbers]
        else:
            prs.extend(new_prs)

        cursor = page_info["endCursor"]
        done   = (not page_info["hasNextPage"]) or reached_cutoff

        print(f"  {repo}: page {page}  +{len(new_prs)} PRs  total {len(prs)}  rate-limit {rate['remaining']}")
        state_file.write_text(
            json.dumps({"complete": done, "cursor": cursor, "prs": prs}),
            encoding="utf-8",
        )

        _check_rate(rate)
        if done:
            break

    print(f"{repo}: done — {len(prs)} PRs.")
    return prs


# ============================================================
# BUILD ROWS
# ============================================================

def build_rows(all_prs: Dict[str, List[dict]], is_bot) -> Tuple[List[dict], List[dict]]:
    review_rows:  List[dict] = []
    request_rows: List[dict] = []

    for repo, prs in all_prs.items():
        for pr in prs:
            author_obj = pr.get("author")
            author: Optional[str] = author_obj["login"] if author_obj else None
            if not author or is_bot(author):
                continue

            pr_created = parse_dt(pr["createdAt"])
            size_loc   = (pr.get("additions") or 0) + (pr.get("deletions") or 0)

            timeline_requests: Dict[str, Optional[datetime.datetime]] = {}
            for item in pr.get("timelineItems", {}).get("nodes", []):
                rv_obj = item.get("requestedReviewer")
                if rv_obj and "login" in rv_obj:
                    login: str = rv_obj["login"]
                    if not is_bot(login):
                        ts = parse_dt(item.get("createdAt"))
                        if login not in timeline_requests or (
                            ts and timeline_requests[login] and ts < timeline_requests[login]
                        ):
                            timeline_requests[login] = ts

            current_requested: Set[str] = set()
            for req in pr.get("reviewRequests", {}).get("nodes", []):
                rv_obj = req.get("requestedReviewer")
                if rv_obj and "login" in rv_obj and not is_bot(rv_obj["login"]):
                    current_requested.add(rv_obj["login"])

            all_requested = set(timeline_requests.keys()) | current_requested
            reviewers_who_reviewed: Set[str] = set()

            for review in pr.get("reviews", {}).get("nodes", []):
                rv_obj   = review.get("author")
                reviewer: Optional[str] = rv_obj["login"] if rv_obj else None
                if not reviewer or is_bot(reviewer) or reviewer == author:
                    continue

                submitted = parse_dt(review.get("submittedAt"))
                hours: object = (
                    round((submitted - pr_created).total_seconds() / 3600, 2)
                    if pr_created and submitted else ""
                )

                reviewers_who_reviewed.add(reviewer)
                review_rows.append({
                    "pr_number":               pr["number"],
                    "repo":                    repo,
                    "pr_url":                  pr["url"],
                    "pr_author":               author,
                    "pr_created_at":           pr["createdAt"],
                    "pr_merged_at":            pr.get("mergedAt") or "",
                    "pr_state":                pr["state"],
                    "pr_is_draft":             pr.get("isDraft", False),
                    "pr_size_loc":             size_loc,
                    "pr_changed_files":        pr.get("changedFiles") or 0,
                    "reviewer":                reviewer,
                    "review_state":            review["state"],
                    "review_submitted_at":     review.get("submittedAt") or "",
                    "was_explicitly_requested": reviewer in all_requested,
                    "hours_to_review":         hours,
                })

            for login, ts in timeline_requests.items():
                request_rows.append({
                    "pr_number":         pr["number"],
                    "repo":              repo,
                    "pr_author":         author,
                    "requested_reviewer": login,
                    "requested_at":      ts.isoformat() if ts else "",
                    "was_fulfilled":     login in reviewers_who_reviewed,
                })

    return review_rows, request_rows


# ============================================================
# SUMMARY
# ============================================================

def generate_summary(
    team_name: str,
    team_cfg: dict,
    review_rows: List[dict],
    request_rows: List[dict],
    cfg: dict,
) -> str:
    now          = datetime.datetime.now(datetime.timezone.utc)
    period_start = now - datetime.timedelta(days=cfg["period_days"])
    recent_start = now - datetime.timedelta(days=cfg["recent_window_days"])

    team_repos      = team_cfg.get("repos", cfg["repos"])
    team_members    = set(team_cfg["members"])
    focus_author    = team_cfg.get("focus_author")
    focus_reviewers = team_cfg.get("focus_reviewers", [])

    # Filter rows to this team's repos
    review_rows  = [r for r in review_rows  if r["repo"] in team_repos]
    request_rows = [r for r in request_rows if r["repo"] in team_repos]

    # Deduplicated approvals (one per PR × reviewer)
    approval_keys: Dict[Tuple, dict] = {}
    dismissed_count = 0
    for r in review_rows:
        if r["review_state"] == "DISMISSED":
            dismissed_count += 1
        elif r["review_state"] == "APPROVED":
            key = (r["repo"], r["pr_number"], r["reviewer"])
            if key not in approval_keys or r["review_submitted_at"] > approval_keys[key]["review_submitted_at"]:
                approval_keys[key] = r

    approvals      = list(approval_keys.values())
    active_reviews = [r for r in review_rows if r["review_state"] != "DISMISSED"]

    parts: List[str] = []
    parts.append(f"# PR Review Analysis — {team_name}\n")
    parts.append(
        f"| | |\n|---|---|\n"
        f"| **Period** | {period_start.date()} – {now.date()} ({cfg['period_days']} days) |\n"
        f"| **Generated** | {now.strftime('%Y-%m-%d %H:%M UTC')} |\n"
        f"| **Repos** | {', '.join(team_repos)} |\n"
        f"| **Team members** | {', '.join(f'`{m}`' for m in sorted(team_members))} |\n"
        f"| **Total review events (non-bot, non-self)** | {len(review_rows)} |\n"
        f"| **Unique (PR × reviewer) approvals** | {len(approvals)} |\n"
        f"| **Dismissed reviews (excluded from approval counts)** | {dismissed_count} |\n"
    )
    parts.append(
        "\n> **Approval counting:** one per (PR, reviewer). Re-approvals after force-push → latest kept.\n"
        "> **Draft PRs** included; marked in CSV (`pr_is_draft=True`).\n"
    )

    # ---- Table 1: Per-author approval distribution ----
    parts.append("\n---\n## 1. Per-Author Approval Distribution\n\n")

    author_totals:      Dict[str, int]               = defaultdict(int)
    author_reviewer_cnt: Dict[str, Dict[str, int]]   = defaultdict(lambda: defaultdict(int))
    author_prs:         Dict[str, Set]               = defaultdict(set)

    for r in approvals:
        author_totals[r["pr_author"]] += 1
        author_reviewer_cnt[r["pr_author"]][r["reviewer"]] += 1
        author_prs[r["pr_author"]].add((r["repo"], r["pr_number"]))

    t1 = []
    for author in sorted(author_totals, key=lambda a: -author_totals[a]):
        total = author_totals[author]
        top5  = sorted(author_reviewer_cnt[author].items(), key=lambda x: -x[1])[:5]
        top5_str = ", ".join(f"{rv} ({cnt} / {cnt/total*100:.0f}%)" for rv, cnt in top5)
        t1.append([author, len(author_prs[author]), total, top5_str])

    parts.append(md_table(["Author", "PRs", "Total approvals", "Top approvers (count / %)"], t1))

    # ---- Table 2: Reviewer load ----
    parts.append("\n\n---\n## 2. Reviewer Load Overall\n\n"
                 "Time metrics in hours. Time-to-first-review uses first event of any state (excl. dismissed).\n\n")

    reviewer_appr_cnt: Dict[str, int]         = defaultdict(int)
    reviewer_authors:  Dict[str, Set[str]]    = defaultdict(set)
    reviewer_appr_h:   Dict[str, List[float]] = defaultdict(list)

    for r in approvals:
        reviewer_appr_cnt[r["reviewer"]] += 1
        reviewer_authors[r["reviewer"]].add(r["pr_author"])
        if r["hours_to_review"] != "":
            reviewer_appr_h[r["reviewer"]].append(float(r["hours_to_review"]))

    rv_pr_first: Dict[str, Dict[Tuple, float]] = defaultdict(dict)
    for r in active_reviews:
        if r["hours_to_review"] == "":
            continue
        h = float(r["hours_to_review"])
        k = (r["repo"], r["pr_number"])
        if k not in rv_pr_first[r["reviewer"]] or h < rv_pr_first[r["reviewer"]][k]:
            rv_pr_first[r["reviewer"]][k] = h

    t2 = []
    for rv in sorted(reviewer_appr_cnt, key=lambda x: -reviewer_appr_cnt[x]):
        first_times = list(rv_pr_first.get(rv, {}).values())
        appr_times  = reviewer_appr_h.get(rv, [])
        t2.append([rv, reviewer_appr_cnt[rv], len(reviewer_authors[rv]),
                   fmt_h(safe_median(first_times)), fmt_h(safe_median(appr_times))])

    parts.append(md_table(
        ["Reviewer", "Approvals", "Unique authors",
         "Median time-to-first-review (h)", "Median time-to-approval (h)"],
        t2,
    ))

    # ---- Table 3: Team matrix ----
    parts.append("\n\n---\n## 3. Team Review Matrix\n\n"
                 "Cross-coverage and response time between team members only.\n")

    # Per-author stats (total PRs + median size)
    author_total_prs: Dict[str, Set] = defaultdict(set)
    author_pr_size:   Dict[str, Dict] = defaultdict(dict)
    seen_pk: Set = set()
    for r in review_rows:
        author_total_prs[r["pr_author"]].add((r["repo"], r["pr_number"]))
        pk = (r["repo"], r["pr_number"])
        if pk not in seen_pk:
            seen_pk.add(pk)
            author_pr_size[r["pr_author"]][pk] = r["pr_size_loc"]

    author_median_size = {
        au: int(statistics.median(list(d.values())))
        for au, d in author_pr_size.items() if d
    }

    rv_au_prs:   Dict[Tuple, Set]   = defaultdict(set)
    rv_au_first: Dict[Tuple, Dict]  = defaultdict(dict)

    for r in active_reviews:
        rv, au = r["reviewer"], r["pr_author"]
        if rv not in team_members or au not in team_members or rv == au:
            continue
        pk = (r["repo"], r["pr_number"])
        rv_au_prs[(rv, au)].add(pk)
        if r["hours_to_review"] != "":
            h    = float(r["hours_to_review"])
            prev = rv_au_first[(rv, au)].get(pk)
            if prev is None or h < prev:
                rv_au_first[(rv, au)][pk] = h

    tm1, tm2 = [], []
    for (rv, au), pr_set in rv_au_prs.items():
        n     = len(pr_set)
        total = len(author_total_prs.get(au, set()))
        pct   = n / total * 100 if total else 0
        times = list(rv_au_first[(rv, au)].values())
        med   = safe_median(times)
        size  = author_median_size.get(au, 0)
        tm1.append((rv, au, n, total, pct, size))
        tm2.append((rv, au, n, med, size))

    tm1.sort(key=lambda x: (x[1], -x[4]))
    tm2.sort(key=lambda x: (x[1], x[0]))

    parts.append("\n### 3a. Coverage\n\n")
    h_tm1 = ["Reviewer", "PR Author", "PRs reviewed", "Author total PRs", "% reviewed", "Median PR size (LOC)"]
    r_tm1 = [[rv, au, n, tot, f"{pct:.0f}%", size] for rv, au, n, tot, pct, size in tm1]
    parts.append(md_table(h_tm1, r_tm1) if r_tm1 else "_No intra-team reviews found._")

    parts.append("\n\n### 3b. Response Time\n\n"
                 "_(median hours from PR creation to first review event)_\n\n")
    h_tm2 = ["Reviewer", "PR Author", "PRs reviewed", "Median time-to-first-review (h)", "Median PR size (LOC)"]
    r_tm2 = [[rv, au, n, f"{med:.1f}" if med is not None else "—", size] for rv, au, n, med, size in tm2]
    parts.append(md_table(h_tm2, r_tm2) if r_tm2 else "_No data._")

    # ---- Tables 4-7: focus_author analysis (optional) ----
    if focus_author and focus_reviewers:
        focus_approvals = [r for r in approvals    if r["pr_author"] == focus_author]
        focus_all       = [r for r in review_rows  if r["pr_author"] == focus_author]

        # Table 4: Monthly trend
        parts.append(f"\n\n---\n## 4. Monthly Approval Trend — `{focus_author}`\n\n"
                     f"Grouped by PR creation month. Rows marked `*` are within the last {cfg['recent_window_days']} days.\n\n")

        month_appr: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        month_prs:  Dict[str, Set]            = defaultdict(set)

        for r in focus_approvals:
            dt = parse_dt(r["pr_created_at"])
            if dt:
                month_appr[dt.strftime("%Y-%m")][r["reviewer"]] += 1

        for r in focus_all:
            dt = parse_dt(r["pr_created_at"])
            if dt:
                month_prs[dt.strftime("%Y-%m")].add((r["repo"], r["pr_number"]))

        all_months = sorted(set(month_appr.keys()) | set(month_prs.keys()))
        t4 = []
        for m in all_months:
            counts = month_appr.get(m, {})
            fc     = [counts.get(rv, 0) for rv in focus_reviewers]
            total  = sum(counts.values())
            label  = m + " *" if m >= recent_start.strftime("%Y-%m") else m
            t4.append([label, len(month_prs.get(m, set()))] + fc + [total - sum(fc), total])

        parts.append(md_table(
            ["Month", "PRs created"] + focus_reviewers + ["Others", "Total approvals"], t4
        ))

        # Table 5: Per-repo breakdown
        parts.append(f"\n\n---\n## 5. Per-Repo Approval Breakdown — `{focus_author}`\n\n")

        repo_appr: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        repo_prs:  Dict[str, Set]            = defaultdict(set)
        for r in focus_approvals:
            repo_appr[r["repo"]][r["reviewer"]] += 1
        for r in focus_all:
            repo_prs[r["repo"]].add(r["pr_number"])

        t5 = []
        for repo in team_repos:
            counts = repo_appr.get(repo, {})
            total  = sum(counts.values())
            top5   = sorted(counts.items(), key=lambda x: -x[1])[:5]
            top5_s = ", ".join(f"{rv} ({cnt} / {cnt/total*100:.0f}%)" for rv, cnt in top5) if top5 else "—"
            t5.append([repo, len(repo_prs.get(repo, set())), total, top5_s])

        parts.append(md_table(["Repo", "PRs", "Total approvals", "Approvers (count / %)"], t5))

        # Table 6: Requested vs actual
        parts.append(f"\n\n---\n## 6. Requested vs Actual Approvers — `{focus_author}`\n\n"
                     "Times each reviewer was (a) explicitly requested and (b) actually approved.\n\n")

        req_counts: Dict[str, int] = defaultdict(int)
        for r in [x for x in request_rows if x["pr_author"] == focus_author]:
            req_counts[r["requested_reviewer"]] += 1

        act_counts: Dict[str, int] = defaultdict(int)
        for r in focus_approvals:
            act_counts[r["reviewer"]] += 1

        all_people = set(req_counts.keys()) | set(act_counts.keys())
        t6 = sorted([[p, req_counts.get(p, 0), act_counts.get(p, 0)] for p in all_people], key=lambda x: -x[2])
        parts.append(md_table(["Reviewer", "Times requested", "Times approved"], t6))

        # Table 7: Response time comparison
        parts.append(f"\n\n---\n## 7. Response Time Comparison — Focus Reviewers\n\n"
                     f"Time-to-first-review on `{focus_author}`'s PRs vs. all other authors' PRs.\n\n")

        def first_rv_times(reviewer: str, focus: bool) -> List[float]:
            pr_min: Dict[Tuple, float] = {}
            for r in active_reviews:
                if r["reviewer"] != reviewer:
                    continue
                if focus != (r["pr_author"] == focus_author):
                    continue
                if r["hours_to_review"] == "":
                    continue
                k = (r["repo"], r["pr_number"])
                h = float(r["hours_to_review"])
                if k not in pr_min or h < pr_min[k]:
                    pr_min[k] = h
            return list(pr_min.values())

        t7 = []
        for rv in focus_reviewers:
            ft = first_rv_times(rv, True)
            ot = first_rv_times(rv, False)
            t7.append([
                rv,
                f"{fmt_h(safe_median(ft))} / {fmt_h(safe_p75(ft))}", len(ft),
                f"{fmt_h(safe_median(ot))} / {fmt_h(safe_p75(ot))}", len(ot),
            ])

        parts.append(md_table(
            ["Reviewer", f"On {focus_author}'s PRs (median / p75 h)", "N",
             "On other authors' PRs (median / p75 h)", "N"],
            t7,
        ))

    parts.append("\n")
    return "\n".join(parts)


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="GitHub PR review distribution analyzer")
    parser.add_argument("--config",    default="config.yaml", help="Config file (default: config.yaml)")
    parser.add_argument("--team",      help="Run one team only (must match a key in config teams:)")
    parser.add_argument("--no-pause",  action="store_true", help="Skip interactive sanity check")
    parser.add_argument("--refresh",   action="store_true", help="Fetch only new PRs since last run")
    args = parser.parse_args()

    interactive = sys.stdin.isatty() and not args.no_pause

    token = os.environ.get("FETCH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("Error: set FETCH_TOKEN (or GITHUB_TOKEN) environment variable.")

    if not Path(args.config).exists():
        sys.exit(f"Config file not found: {args.config}")
    cfg = load_config(args.config)

    if args.team:
        if args.team not in cfg["teams"]:
            sys.exit(f"Team '{args.team}' not in config. Available: {list(cfg['teams'].keys())}")
        teams_to_run = {args.team: cfg["teams"][args.team]}
    else:
        teams_to_run = cfg["teams"]

    RAW_DIR.mkdir(exist_ok=True)

    # Preflight
    preflight_check(token, cfg["org"], cfg["repos"])

    # ---- Phase 1: collect all repos ----
    print("=" * 60)
    print("PHASE 1: Data Collection")
    print(f"Org: {cfg['org']}  |  Period: {cfg['period_days']} days  |  "
          f"Repos: {len(cfg['repos'])}" + ("  [REFRESH]" if args.refresh else ""))
    print("=" * 60)

    all_prs: Dict[str, List[dict]] = {}
    for i, repo in enumerate(cfg["repos"]):
        print(f"\n[{i+1}/{len(cfg['repos'])}] {repo}")
        prs = collect_repo(repo, cfg["org"], token, cfg["period_days"], refresh=args.refresh)
        all_prs[repo] = prs

        if i == 0 and interactive:
            non_null = [p for p in prs if p.get("author")]
            print(f"\n{'─'*52}")
            print(f"Sanity check — {repo}")
            print(f"  PRs in period:       {len(prs)}")
            print(f"  PRs with author:     {len(non_null)}")
            print(f"  Unique PR authors:   {len({p['author']['login'] for p in non_null})}")
            print(f"  Review events:       {sum(len(p.get('reviews',{}).get('nodes',[]))for p in prs)}")
            print(f"{'─'*52}\n")
            if input("Looks correct? Continue? [y/N]: ").strip().lower() != "y":
                print("Aborted. Raw data saved — re-run to resume.")
                sys.exit(0)

    # ---- Phase 2: analyze per team ----
    print("\n" + "=" * 60)
    print("PHASE 2: Analysis")
    print("=" * 60)

    is_bot = make_is_bot(cfg["bot_patterns"])

    for team_name, team_cfg in teams_to_run.items():
        team_repos = team_cfg.get("repos", cfg["repos"])
        team_prs   = {r: all_prs[r] for r in team_repos if r in all_prs}

        print(f"\n[{team_name}]  repos: {team_repos}")

        review_rows, request_rows = build_rows(team_prs, is_bot)
        print(f"  Review rows: {len(review_rows)}  |  Request rows: {len(request_rows)}")

        out_dir = Path("output") / team_name
        out_dir.mkdir(parents=True, exist_ok=True)

        write_csv(
            out_dir / "reviews_raw.csv", review_rows,
            ["pr_number", "repo", "pr_url", "pr_author", "pr_created_at", "pr_merged_at",
             "pr_state", "pr_is_draft", "pr_size_loc", "pr_changed_files",
             "reviewer", "review_state", "review_submitted_at",
             "was_explicitly_requested", "hours_to_review"],
        )
        write_csv(
            out_dir / "review_requests_raw.csv", request_rows,
            ["pr_number", "repo", "pr_author", "requested_reviewer", "requested_at", "was_fulfilled"],
        )

        summary_path = out_dir / "summary.md"
        summary_path.write_text(
            generate_summary(team_name, team_cfg, review_rows, request_rows, cfg),
            encoding="utf-8",
        )
        print(f"  Written: {summary_path}")

    print("\nDone. Results in output/<team>/")


if __name__ == "__main__":
    main()
