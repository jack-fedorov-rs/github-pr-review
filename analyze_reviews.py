#!/usr/bin/env python3
"""
GitHub PR Review Distribution Analyzer

Collects PR + review data from GitHub GraphQL API and writes:
  raw_data/{repo}_prs.json     — raw pages, idempotent (resume on crash)
  output/reviews_raw.csv
  output/review_requests_raw.csv
  output/summary.md            — 6 analysis tables, no conclusions

Usage:
    export GITHUB_TOKEN=ghp_xxx
    pip install requests
    python analyze_reviews.py [--no-pause]
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
    sys.exit("Error: 'requests' not found. Run: pip install requests")

# ============================================================
# CONFIG
# ============================================================
ORG = "roofstock"
REPOS = [
    "service-requests-service",
    "services-contracts",
    "otto-vendor-website-gateway",
    "otto-ecs-services",
]
FOCUS_AUTHOR = "aleksandr-beliakov-rs"
FOCUS_REVIEWERS = ["artem-saidanov-rs", "dmitry-indikeev-rs"]
PERIOD_DAYS = 365
RECENT_WINDOW_DAYS = 60
BOT_PATTERNS = ["[bot]", "dependabot", "renovate", "github-actions"]

GRAPHQL_URL = "https://api.github.com/graphql"
RAW_DIR = Path("raw_data")
OUTPUT_DIR = Path("output")

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
        number
        url
        author { login }
        createdAt
        mergedAt
        closedAt
        state
        isDraft
        additions
        deletions
        changedFiles
        reviews(first: 100) {
          nodes {
            author { login }
            state
            submittedAt
          }
        }
        reviewRequests(first: 50) {
          nodes {
            requestedReviewer {
              ... on User { login }
            }
          }
        }
        timelineItems(first: 100, itemTypes: [REVIEW_REQUESTED_EVENT]) {
          nodes {
            ... on ReviewRequestedEvent {
              createdAt
              requestedReviewer {
                ... on User { login }
              }
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


def is_bot(login: str) -> bool:
    if not login:
        return True
    ll = login.lower()
    return any(p in ll for p in BOT_PATTERNS)


def parse_dt(s: Optional[str]) -> Optional[datetime.datetime]:
    if not s:
        return None
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def hours_diff(a: datetime.datetime, b: datetime.datetime) -> float:
    return (b - a).total_seconds() / 3600


def safe_median(values: List[float]) -> Optional[float]:
    return statistics.median(values) if values else None


def safe_p75(values: List[float]) -> Optional[float]:
    if not values:
        return None
    sv = sorted(values)
    idx = min(int(len(sv) * 0.75), len(sv) - 1)
    return sv[idx]


def fmt_h(val: Optional[float]) -> str:
    return f"{val:.1f}" if val is not None else "—"


def md_table(headers: List[str], rows: List[list]) -> str:
    """Render a GitHub-flavoured Markdown table."""
    col_widths = [len(str(h)) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(cell)))

    def fmt_row(r: list) -> str:
        return "| " + " | ".join(str(c).ljust(col_widths[i]) for i, c in enumerate(r)) + " |"

    sep = "| " + " | ".join("-" * w for w in col_widths) + " |"
    return "\n".join([fmt_row(headers), sep] + [fmt_row(r) for r in rows])


def write_csv(path: Path, rows: List[dict], fieldnames: List[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Written: {path}  ({len(rows)} rows)")


# ============================================================
# COLLECTION
# ============================================================


def gql(token: str, variables: dict) -> dict:
    headers = {
        "Authorization": f"bearer {token}",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        GRAPHQL_URL,
        headers=headers,
        json={"query": QUERY, "variables": variables},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def check_rate_limit(rate: dict) -> None:
    remaining = rate["remaining"]
    if remaining < 200:
        reset_time = parse_dt(rate["resetAt"])
        now = datetime.datetime.now(datetime.timezone.utc)
        if reset_time is not None:
            sleep_secs = (reset_time - now).total_seconds() + 10
            if sleep_secs > 0:
                print(
                    f"  [rate-limit] {remaining} remaining — "
                    f"sleeping {sleep_secs:.0f}s until {rate['resetAt']}"
                )
                time.sleep(sleep_secs)


def collect_repo(repo: str, token: str) -> List[dict]:
    """
    Fetches all PRs for `repo` created within PERIOD_DAYS.
    Saves progress to raw_data/{repo}_prs.json after every page so the
    script can resume without re-fetching already-downloaded pages.
    """
    state_file = RAW_DIR / f"{repo}_prs.json"

    if state_file.exists():
        state = json.loads(state_file.read_text(encoding="utf-8"))
        if state.get("complete"):
            print(f"{repo}: already complete ({len(state['prs'])} PRs). Skipping.")
            return state["prs"]
        cursor: Optional[str] = state.get("cursor")
        prs: List[dict] = state.get("prs", [])
        print(f"{repo}: resuming from saved cursor, {len(prs)} PRs already collected.")
    else:
        cursor, prs = None, []

    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=PERIOD_DAYS)
    page = 0

    while True:
        page += 1
        variables: dict = {"owner": ORG, "repo": repo}
        if cursor:
            variables["after"] = cursor

        data = gql(token, variables)

        if "errors" in data:
            sys.exit(f"GraphQL error for {repo}: {data['errors']}")

        rate = data["data"]["rateLimit"]
        pr_conn = data["data"]["repository"]["pullRequests"]
        page_info = pr_conn["pageInfo"]
        nodes: List[dict] = pr_conn["nodes"]

        new_prs: List[dict] = []
        reached_cutoff = False
        null_author_count = 0

        for pr in nodes:
            created = parse_dt(pr["createdAt"])
            if created is None or created < since:
                reached_cutoff = True
                break
            if pr.get("author") is None:
                null_author_count += 1
            new_prs.append(pr)

        if null_author_count:
            print(
                f"  [warn] {null_author_count} PR(s) with null author on this page "
                f"(deleted GitHub accounts) — will be skipped during analysis"
            )

        prs.extend(new_prs)
        cursor = page_info["endCursor"]
        done = (not page_info["hasNextPage"]) or reached_cutoff

        print(
            f"  {repo}: page {page}  +{len(new_prs)} PRs  "
            f"total {len(prs)}  rate-limit remaining {rate['remaining']}"
        )

        state_file.write_text(
            json.dumps({"complete": done, "cursor": cursor, "prs": prs}),
            encoding="utf-8",
        )

        check_rate_limit(rate)

        if done:
            break

    print(f"{repo}: collection done — {len(prs)} PRs total.")
    return prs


# ============================================================
# BUILD ANALYSIS ROWS
# ============================================================


def build_rows(
    all_prs: Dict[str, List[dict]],
) -> Tuple[List[dict], List[dict]]:
    """
    Returns (review_rows, request_rows).

    review_rows   — one row per review event (APPROVED / CHANGES_REQUESTED /
                    COMMENTED / DISMISSED), excluding bots and self-reviews.
    request_rows  — one row per REVIEW_REQUESTED_EVENT from the timeline,
                    i.e. only requests that have a timestamp.
    """
    review_rows: List[dict] = []
    request_rows: List[dict] = []

    for repo, prs in all_prs.items():
        for pr in prs:
            author_obj = pr.get("author")
            author: Optional[str] = author_obj["login"] if author_obj else None
            if not author or is_bot(author):
                continue

            pr_created = parse_dt(pr["createdAt"])
            size_loc = (pr.get("additions") or 0) + (pr.get("deletions") or 0)

            # --- timeline review-request events (have timestamps) ---
            timeline_requests: Dict[str, Optional[datetime.datetime]] = {}
            for item in pr.get("timelineItems", {}).get("nodes", []):
                rv_obj = item.get("requestedReviewer")
                if rv_obj and "login" in rv_obj:
                    login: str = rv_obj["login"]
                    if not is_bot(login):
                        ts = parse_dt(item.get("createdAt"))
                        if login not in timeline_requests or (
                            ts is not None
                            and timeline_requests[login] is not None
                            and ts < timeline_requests[login]
                        ):
                            timeline_requests[login] = ts

            # --- current reviewRequests node (no timestamp, but shows pending) ---
            current_requested: Set[str] = set()
            for req in pr.get("reviewRequests", {}).get("nodes", []):
                rv_obj = req.get("requestedReviewer")
                if rv_obj and "login" in rv_obj and not is_bot(rv_obj["login"]):
                    current_requested.add(rv_obj["login"])

            all_requested = set(timeline_requests.keys()) | current_requested

            # --- reviews ---
            reviewers_who_reviewed: Set[str] = set()
            for review in pr.get("reviews", {}).get("nodes", []):
                rv_obj = review.get("author")
                reviewer: Optional[str] = rv_obj["login"] if rv_obj else None
                if not reviewer or is_bot(reviewer) or reviewer == author:
                    continue

                submitted = parse_dt(review.get("submittedAt"))
                hours: object = (
                    round(hours_diff(pr_created, submitted), 2)
                    if pr_created and submitted
                    else ""
                )

                reviewers_who_reviewed.add(reviewer)
                review_rows.append(
                    {
                        "pr_number": pr["number"],
                        "repo": repo,
                        "pr_url": pr["url"],
                        "pr_author": author,
                        "pr_created_at": pr["createdAt"],
                        "pr_merged_at": pr.get("mergedAt") or "",
                        "pr_state": pr["state"],
                        "pr_is_draft": pr.get("isDraft", False),
                        "pr_size_loc": size_loc,
                        "pr_changed_files": pr.get("changedFiles") or 0,
                        "reviewer": reviewer,
                        "review_state": review["state"],
                        "review_submitted_at": review.get("submittedAt") or "",
                        "was_explicitly_requested": reviewer in all_requested,
                        "hours_to_review": hours,
                    }
                )

            # --- review request rows (timeline only — those have timestamps) ---
            for login, ts in timeline_requests.items():
                request_rows.append(
                    {
                        "pr_number": pr["number"],
                        "repo": repo,
                        "pr_author": author,
                        "requested_reviewer": login,
                        "requested_at": ts.isoformat() if ts else "",
                        "was_fulfilled": login in reviewers_who_reviewed,
                    }
                )

    return review_rows, request_rows


# ============================================================
# SUMMARY
# ============================================================


def generate_summary(review_rows: List[dict], request_rows: List[dict]) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    period_start = now - datetime.timedelta(days=PERIOD_DAYS)
    recent_start = now - datetime.timedelta(days=RECENT_WINDOW_DAYS)

    # Deduplicated approvals: one per (repo, pr_number, reviewer).
    # Multiple approvals on the same PR (e.g. after force-push) → keep latest.
    approval_keys: Dict[Tuple[str, int, str], dict] = {}
    dismissed_count = 0
    for r in review_rows:
        if r["review_state"] == "DISMISSED":
            dismissed_count += 1
        elif r["review_state"] == "APPROVED":
            key = (r["repo"], r["pr_number"], r["reviewer"])
            if key not in approval_keys or (
                r["review_submitted_at"] > approval_keys[key]["review_submitted_at"]
            ):
                approval_keys[key] = r

    approvals: List[dict] = list(approval_keys.values())

    # All non-dismissed reviews (used for time-to-first-review)
    active_reviews = [r for r in review_rows if r["review_state"] != "DISMISSED"]

    focus_approvals = [r for r in approvals if r["pr_author"] == FOCUS_AUTHOR]
    focus_all_reviews = [r for r in review_rows if r["pr_author"] == FOCUS_AUTHOR]

    # ----------------------------------------------------------------

    parts: List[str] = []

    parts.append("# PR Review Distribution Analysis\n")
    parts.append(
        f"| | |\n|---|---|\n"
        f"| **Period** | {period_start.date()} – {now.date()} ({PERIOD_DAYS} days) |\n"
        f"| **Generated** | {now.strftime('%Y-%m-%d %H:%M UTC')} |\n"
        f"| **Repos** | {', '.join(REPOS)} |\n"
        f"| **Focus author** | `{FOCUS_AUTHOR}` |\n"
        f"| **Focus reviewers** | {', '.join(f'`{r}`' for r in FOCUS_REVIEWERS)} |\n"
        f"| **Total review events (non-bot, non-self)** | {len(review_rows)} |\n"
        f"| **Unique (PR × reviewer) approvals** | {len(approvals)} |\n"
        f"| **Dismissed reviews (excluded from approval counts)** | {dismissed_count} |\n"
    )
    parts.append(
        "\n> **Approval counting:** Tables 1–2 use one approval per (PR, reviewer) pair. "
        "Re-approvals after force-pushes are collapsed to the latest event.\n"
        "> **Draft PRs** are included and marked in `reviews_raw.csv` (`pr_is_draft=True`).\n"
    )

    # ----------------------------------------------------------------
    # Table 1 — Per-author approval distribution
    # ----------------------------------------------------------------
    parts.append("\n---\n## 1. Per-Author Approval Distribution\n")
    parts.append(
        "One row per PR author. Top-5 approvers by count, with share of that author's total approvals.\n\n"
    )

    author_totals: Dict[str, int] = defaultdict(int)
    author_reviewer_cnt: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    author_prs: Dict[str, Set[Tuple]] = defaultdict(set)

    for r in approvals:
        author_totals[r["pr_author"]] += 1
        author_reviewer_cnt[r["pr_author"]][r["reviewer"]] += 1
        author_prs[r["pr_author"]].add((r["repo"], r["pr_number"]))

    t1: List[list] = []
    for author in sorted(author_totals, key=lambda a: -author_totals[a]):
        total = author_totals[author]
        top5 = sorted(author_reviewer_cnt[author].items(), key=lambda x: -x[1])[:5]
        top5_str = ", ".join(f"{rv} ({cnt} / {cnt / total * 100:.0f}%)" for rv, cnt in top5)
        t1.append([author, len(author_prs[author]), total, top5_str])

    parts.append(md_table(["Author", "PRs", "Total approvals", "Top approvers (count / %)"], t1))

    # ----------------------------------------------------------------
    # Table 2 — Reviewer load overall
    # ----------------------------------------------------------------
    parts.append("\n\n---\n## 2. Reviewer Load Overall\n")
    parts.append(
        "Time-to-first-review = hours from PR creation to reviewer's first review event (any state, excl. dismissed).  \n"
        "Time-to-approval = hours from PR creation to approval event.  \n"
        "Both metrics in **hours**.\n\n"
    )

    reviewer_appr_cnt: Dict[str, int] = defaultdict(int)
    reviewer_authors: Dict[str, Set[str]] = defaultdict(set)
    reviewer_appr_h: Dict[str, List[float]] = defaultdict(list)

    for r in approvals:
        reviewer_appr_cnt[r["reviewer"]] += 1
        reviewer_authors[r["reviewer"]].add(r["pr_author"])
        if r["hours_to_review"] != "":
            reviewer_appr_h[r["reviewer"]].append(float(r["hours_to_review"]))

    # Time-to-first-review: min hours per (reviewer, PR) across non-dismissed reviews
    rv_pr_first: Dict[str, Dict[Tuple, float]] = defaultdict(dict)
    for r in active_reviews:
        if r["hours_to_review"] == "":
            continue
        h = float(r["hours_to_review"])
        pr_key = (r["repo"], r["pr_number"])
        rv = r["reviewer"]
        if pr_key not in rv_pr_first[rv] or h < rv_pr_first[rv][pr_key]:
            rv_pr_first[rv][pr_key] = h

    t2: List[list] = []
    for rv in sorted(reviewer_appr_cnt, key=lambda x: -reviewer_appr_cnt[x]):
        first_times = list(rv_pr_first.get(rv, {}).values())
        appr_times = reviewer_appr_h.get(rv, [])
        t2.append(
            [
                rv,
                reviewer_appr_cnt[rv],
                len(reviewer_authors[rv]),
                fmt_h(safe_median(first_times)),
                fmt_h(safe_median(appr_times)),
            ]
        )

    parts.append(
        md_table(
            [
                "Reviewer",
                "Approvals",
                "Unique authors",
                "Median time-to-first-review (h)",
                "Median time-to-approval (h)",
            ],
            t2,
        )
    )

    # ----------------------------------------------------------------
    # Table 3 — Monthly trend for FOCUS_AUTHOR
    # ----------------------------------------------------------------
    parts.append(f"\n\n---\n## 3. Monthly Approval Trend — `{FOCUS_AUTHOR}`\n")
    parts.append(
        f"Grouped by **PR creation month**. Rows marked `*` fall within the most recent "
        f"{RECENT_WINDOW_DAYS}-day window (`>= {recent_start.date()}`).\n\n"
    )

    month_appr: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    month_prs: Dict[str, Set[Tuple]] = defaultdict(set)

    for r in focus_approvals:
        # Group by PR creation month (not review submission month) for consistency
        dt = parse_dt(r["pr_created_at"])
        if dt:
            m = dt.strftime("%Y-%m")
            month_appr[m][r["reviewer"]] += 1

    for r in focus_all_reviews:
        dt = parse_dt(r["pr_created_at"])
        if dt:
            m = dt.strftime("%Y-%m")
            month_prs[m].add((r["repo"], r["pr_number"]))

    all_months = sorted(set(month_appr.keys()) | set(month_prs.keys()))

    t3: List[list] = []
    for m in all_months:
        counts = month_appr.get(m, {})
        focus_cnts = [counts.get(rv, 0) for rv in FOCUS_REVIEWERS]
        total = sum(counts.values())
        others = total - sum(focus_cnts)
        label = m + " *" if m >= recent_start.strftime("%Y-%m") else m
        t3.append([label, len(month_prs.get(m, set()))] + focus_cnts + [others, total])

    parts.append(
        md_table(
            ["Month", "PRs created"] + FOCUS_REVIEWERS + ["Others", "Total approvals"],
            t3,
        )
    )

    # ----------------------------------------------------------------
    # Table 4 — Per-repo breakdown for FOCUS_AUTHOR
    # ----------------------------------------------------------------
    parts.append(f"\n\n---\n## 4. Per-Repo Approval Breakdown — `{FOCUS_AUTHOR}`\n\n")

    repo_appr: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    repo_prs: Dict[str, Set] = defaultdict(set)

    for r in focus_approvals:
        repo_appr[r["repo"]][r["reviewer"]] += 1

    for r in focus_all_reviews:
        repo_prs[r["repo"]].add(r["pr_number"])

    t4: List[list] = []
    for repo in REPOS:
        counts = repo_appr.get(repo, {})
        total = sum(counts.values())
        prs_cnt = len(repo_prs.get(repo, set()))
        top5 = sorted(counts.items(), key=lambda x: -x[1])[:5]
        top5_str = (
            ", ".join(f"{rv} ({cnt} / {cnt / total * 100:.0f}%)" for rv, cnt in top5)
            if top5
            else "—"
        )
        t4.append([repo, prs_cnt, total, top5_str])

    parts.append(md_table(["Repo", "PRs", "Total approvals", "Approvers (count / %)"], t4))

    # ----------------------------------------------------------------
    # Table 5 — Requested vs actual approvers for FOCUS_AUTHOR
    # ----------------------------------------------------------------
    parts.append(f"\n\n---\n## 5. Requested vs Actual Approvers — `{FOCUS_AUTHOR}`\n")
    parts.append(
        "Times each reviewer was (a) explicitly requested via `@mention` / review-request and "
        "(b) actually approved (deduplicated per PR).\n\n"
    )

    focus_requests = [r for r in request_rows if r["pr_author"] == FOCUS_AUTHOR]
    req_counts: Dict[str, int] = defaultdict(int)
    for r in focus_requests:
        req_counts[r["requested_reviewer"]] += 1

    act_counts: Dict[str, int] = defaultdict(int)
    for r in focus_approvals:
        act_counts[r["reviewer"]] += 1

    all_people = set(req_counts.keys()) | set(act_counts.keys())
    t5 = sorted(
        [[p, req_counts.get(p, 0), act_counts.get(p, 0)] for p in all_people],
        key=lambda x: -x[2],
    )
    parts.append(md_table(["Reviewer", "Times requested", "Times approved"], t5))

    # ----------------------------------------------------------------
    # Table 6 — Response time comparison for FOCUS_REVIEWERS
    # ----------------------------------------------------------------
    parts.append(f"\n\n---\n## 6. Response Time Comparison — Focus Reviewers\n")
    parts.append(
        f"Time-to-first-review (hours) on `{FOCUS_AUTHOR}`'s PRs vs. all other authors' PRs.  \n"
        f"Metric = hours from PR creation to reviewer's first review event (excl. dismissed).  \n"
        f"One data point per PR (minimum across multiple reviews from same person on same PR).\n\n"
    )

    def first_review_times_for(reviewer: str, focus: bool) -> List[float]:
        pr_min: Dict[Tuple, float] = {}
        for r in active_reviews:
            if r["reviewer"] != reviewer:
                continue
            is_focus_pr = r["pr_author"] == FOCUS_AUTHOR
            if focus != is_focus_pr:
                continue
            if r["hours_to_review"] == "":
                continue
            k = (r["repo"], r["pr_number"])
            h = float(r["hours_to_review"])
            if k not in pr_min or h < pr_min[k]:
                pr_min[k] = h
        return list(pr_min.values())

    t6: List[list] = []
    for rv in FOCUS_REVIEWERS:
        ft = first_review_times_for(rv, focus=True)
        ot = first_review_times_for(rv, focus=False)
        t6.append(
            [
                rv,
                f"{fmt_h(safe_median(ft))} / {fmt_h(safe_p75(ft))}",
                len(ft),
                f"{fmt_h(safe_median(ot))} / {fmt_h(safe_p75(ot))}",
                len(ot),
            ]
        )

    parts.append(
        md_table(
            [
                "Reviewer",
                f"On {FOCUS_AUTHOR}'s PRs (median / p75 h)",
                "N (PRs)",
                "On other authors' PRs (median / p75 h)",
                "N (PRs)",
            ],
            t6,
        )
    )

    parts.append("\n")
    return "\n".join(parts)


# ============================================================
# MAIN
# ============================================================


def main() -> None:
    parser = argparse.ArgumentParser(description="GitHub PR review distribution analyzer")
    parser.add_argument(
        "--no-pause",
        action="store_true",
        help="Skip interactive sanity-check pause after first repo",
    )
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit(
            "Error: GITHUB_TOKEN environment variable is not set.\n"
            "Run: export GITHUB_TOKEN=ghp_your_token"
        )

    RAW_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)

    # ---- Phase 1: collection ----------------------------------------
    print("=" * 60)
    print("PHASE 1: Data Collection")
    print(f"Org: {ORG}  |  Period: last {PERIOD_DAYS} days  |  Repos: {len(REPOS)}")
    print("=" * 60)

    all_prs: Dict[str, List[dict]] = {}

    for i, repo in enumerate(REPOS):
        print(f"\n[{i + 1}/{len(REPOS)}] {repo}")
        prs = collect_repo(repo, token)
        all_prs[repo] = prs

        # Pause after first repo so the EM can eyeball the numbers
        if i == 0 and not args.no_pause:
            non_null_prs = [p for p in prs if p.get("author")]
            unique_authors = len({p["author"]["login"] for p in non_null_prs})
            all_review_nodes = [
                rv
                for p in prs
                for rv in p.get("reviews", {}).get("nodes", [])
            ]
            unique_reviewers = {rv["author"]["login"] for rv in all_review_nodes if rv.get("author")}

            print(f"\n{'─' * 50}")
            print(f"Sanity check — {repo}")
            print(f"  PRs in period:          {len(prs)}")
            print(f"  PRs with valid author:  {len(non_null_prs)}")
            print(f"  Unique PR authors:      {unique_authors}")
            print(f"  Review events total:    {len(all_review_nodes)}")
            print(f"  Unique reviewers:       {len(unique_reviewers)}")
            print(f"{'─' * 50}\n")

            ans = input("Do these numbers look correct? Continue with remaining repos? [y/N]: ").strip().lower()
            if ans != "y":
                print("Aborted. Raw data is saved — re-run to resume from this point.")
                sys.exit(0)

    # ---- Phase 2: analysis ------------------------------------------
    print("\n" + "=" * 60)
    print("PHASE 2: Analysis")
    print("=" * 60)

    review_rows, request_rows = build_rows(all_prs)
    print(f"\nReview rows (non-bot, non-self): {len(review_rows)}")
    print(f"Review request rows:             {len(request_rows)}")

    write_csv(
        OUTPUT_DIR / "reviews_raw.csv",
        review_rows,
        [
            "pr_number", "repo", "pr_url", "pr_author",
            "pr_created_at", "pr_merged_at", "pr_state", "pr_is_draft",
            "pr_size_loc", "pr_changed_files",
            "reviewer", "review_state", "review_submitted_at",
            "was_explicitly_requested", "hours_to_review",
        ],
    )
    write_csv(
        OUTPUT_DIR / "review_requests_raw.csv",
        request_rows,
        ["pr_number", "repo", "pr_author", "requested_reviewer", "requested_at", "was_fulfilled"],
    )

    summary = generate_summary(review_rows, request_rows)
    summary_path = OUTPUT_DIR / "summary.md"
    summary_path.write_text(summary, encoding="utf-8")
    print(f"Written: {summary_path}")

    print("\nAll done. Check the output/ directory for results.")


if __name__ == "__main__":
    main()
