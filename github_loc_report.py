#!/usr/bin/env python3
"""
GitHub LOC Report
Generates a CSV report of lines of code committed per user/repo/date for a GitHub org.

Usage:
    python github_loc_report.py <org> [options]

Options:
    --token TOKEN       GitHub personal access token (or set GITHUB_TOKEN env var)
    --since YYYY-MM-DD  Only include commits after this date
    --until YYYY-MM-DD  Only include commits before this date
    --output FILE       Output CSV file (default: loc_report.csv)
    --repos REPO,...    Comma-separated list of repos to include (default: all)
    --workers N         Number of parallel workers (default: 5)
"""

import argparse
import csv
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

BASE_URL = "https://api.github.com"


class GitHubClient:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def _get(self, url: str, params: dict = None) -> dict | list:
        for attempt in range(5):
            resp = self.session.get(url, params=params, timeout=30)

            if resp.status_code == 403 and "rate limit" in resp.text.lower():
                reset_at = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
                wait = max(reset_at - time.time(), 1)
                print(f"  [rate limit] waiting {wait:.0f}s...", flush=True)
                time.sleep(wait)
                continue

            if resp.status_code == 409:  # empty repo
                return []

            resp.raise_for_status()
            return resp.json()

        raise RuntimeError(f"Failed after retries: {url}")

    def paginate(self, url: str, params: dict = None) -> list:
        params = dict(params or {})
        params.setdefault("per_page", 100)
        results = []
        page = 1
        while True:
            params["page"] = page
            data = self._get(url, params)
            if not data:
                break
            results.extend(data)
            if len(data) < params["per_page"]:
                break
            page += 1
        return results

    def get_org_repos(self, org: str) -> list[dict]:
        print(f"Fetching repos for org: {org}", flush=True)
        return self.paginate(f"{BASE_URL}/orgs/{org}/repos", {"type": "all"})

    def get_commits(self, org: str, repo: str, since: str = None, until: str = None) -> list[dict]:
        params = {}
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        return self.paginate(f"{BASE_URL}/repos/{org}/{repo}/commits", params)

    def get_commit_detail(self, org: str, repo: str, sha: str) -> dict:
        return self._get(f"{BASE_URL}/repos/{org}/{repo}/commits/{sha}")


def process_repo(client: GitHubClient, org: str, repo_name: str, since: str, until: str) -> list[dict]:
    print(f"  [{repo_name}] fetching commits...", flush=True)
    try:
        commits = client.get_commits(org, repo_name, since, until)
    except requests.HTTPError as e:
        print(f"  [{repo_name}] skipped ({e})", flush=True)
        return []

    if not commits:
        return []

    rows = []
    for i, commit in enumerate(commits):
        sha = commit["sha"]
        author = (
            commit.get("author") or {}
        ).get("login") or (
            commit.get("commit", {}).get("author") or {}
        ).get("name", "unknown")
        date_raw = commit.get("commit", {}).get("author", {}).get("date", "")
        date = date_raw[:10] if date_raw else ""

        try:
            detail = client.get_commit_detail(org, repo_name, sha)
            stats = detail.get("stats", {})
            additions = stats.get("additions", 0)
            deletions = stats.get("deletions", 0)
        except Exception as e:
            print(f"  [{repo_name}] commit {sha[:7]} error: {e}", flush=True)
            additions, deletions = 0, 0

        rows.append({
            "repo": repo_name,
            "date": date,
            "user": author,
            "sha": sha[:7],
            "loc_added": additions,
            "loc_deleted": deletions,
            "loc_net": additions - deletions,
        })

        if (i + 1) % 20 == 0:
            print(f"  [{repo_name}] {i + 1}/{len(commits)} commits processed", flush=True)

    print(f"  [{repo_name}] done — {len(rows)} commits", flush=True)
    return rows


def main():
    parser = argparse.ArgumentParser(description="Generate a GitHub LOC report for an org.")
    parser.add_argument("org", help="GitHub organization name")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"), help="GitHub token")
    parser.add_argument("--since", help="Start date YYYY-MM-DD (inclusive)")
    parser.add_argument("--until", help="End date YYYY-MM-DD (inclusive)")
    parser.add_argument("--output", default="loc_report.csv", help="Output CSV file")
    parser.add_argument("--repos", help="Comma-separated list of specific repos")
    parser.add_argument("--workers", type=int, default=5, help="Parallel workers")
    args = parser.parse_args()

    if not args.token:
        print("Error: GitHub token required. Use --token or set GITHUB_TOKEN env var.")
        sys.exit(1)

    # Convert dates to ISO 8601 with time component expected by GitHub API
    since = f"{args.since}T00:00:00Z" if args.since else None
    until = f"{args.until}T23:59:59Z" if args.until else None

    client = GitHubClient(args.token)

    if args.repos:
        repos = [{"name": r.strip()} for r in args.repos.split(",")]
    else:
        repos = client.get_org_repos(args.org)

    print(f"Found {len(repos)} repos. Starting commit extraction with {args.workers} workers...\n", flush=True)

    all_rows = []

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(process_repo, client, args.org, repo["name"], since, until): repo["name"]
            for repo in repos
        }
        for future in as_completed(futures):
            repo_name = futures[future]
            try:
                rows = future.result()
                all_rows.extend(rows)
            except Exception as e:
                print(f"  [{repo_name}] unexpected error: {e}", flush=True)

    # Sort by date desc, then repo, then user
    all_rows.sort(key=lambda r: (r["date"], r["repo"], r["user"]), reverse=True)

    fieldnames = ["repo", "date", "user", "sha", "loc_added", "loc_deleted", "loc_net"]
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\nReport written to: {args.output}")
    print(f"Total commits: {len(all_rows)}")

    # Summary by user
    summary: dict[str, dict] = {}
    for row in all_rows:
        u = row["user"]
        if u not in summary:
            summary[u] = {"commits": 0, "loc_added": 0, "loc_deleted": 0, "loc_net": 0}
        summary[u]["commits"] += 1
        summary[u]["loc_added"] += row["loc_added"]
        summary[u]["loc_deleted"] += row["loc_deleted"]
        summary[u]["loc_net"] += row["loc_net"]

    print("\n--- Summary by user ---")
    print(f"{'user':<30} {'commits':>8} {'added':>10} {'deleted':>10} {'net':>10}")
    print("-" * 70)
    for user, s in sorted(summary.items(), key=lambda x: x[1]["loc_net"], reverse=True):
        print(f"{user:<30} {s['commits']:>8} {s['loc_added']:>10} {s['loc_deleted']:>10} {s['loc_net']:>10}")


if __name__ == "__main__":
    main()
