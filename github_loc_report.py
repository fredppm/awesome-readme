#!/usr/bin/env python3
"""
GitHub LOC Report
Gera relatório de linhas de código por usuário/repo para uma org do GitHub.

Usa o endpoint stats/contributors: 1 request por repo (muito mais rápido que
buscar commit a commit). Cobre até 52 semanas (~12 meses).

Usage:
    python github_loc_report.py <org> [options]

Options:
    --token TOKEN        GitHub token (ou env GITHUB_TOKEN)
    --last-months N      Últimos N meses a partir de hoje (default: 12, máx: 12)
    --since YYYY-MM-DD   Data de início (substitui --last-months)
    --until YYYY-MM-DD   Data de fim (default: hoje)
    --repos REPO,...     Repos específicos (default: todos da org)
    --workers N          Workers paralelos (default: 8)
    --output FILE        Arquivo CSV de saída (default: loc_report.csv)
    --no-csv             Não gera CSV, só exibe o resumo no terminal
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


# ─── GitHub client ────────────────────────────────────────────────────────────

class GitHubClient:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def get(self, url: str, params: dict = None, retries: int = 6) -> list | dict | None:
        for attempt in range(retries):
            resp = self.session.get(url, params=params, timeout=30)

            if resp.status_code == 202:
                # GitHub está computando as stats — aguarda e tenta de novo
                wait = 2 ** attempt
                time.sleep(wait)
                continue

            if resp.status_code == 204 or resp.status_code == 409:
                return []  # repo vazio ou sem conteúdo

            if resp.status_code in (403, 429):
                reset_at = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
                wait = max(reset_at - time.time(), 1)
                print(f"  [rate limit] aguardando {wait:.0f}s...", flush=True)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp.json()

        return None  # esgotou retries (normalmente 202 repetido = repo sem stats)

    def paginate(self, url: str, params: dict = None) -> list:
        params = dict(params or {})
        params.setdefault("per_page", 100)
        results, page = [], 1
        while True:
            params["page"] = page
            data = self.get(url, params)
            if not data:
                break
            results.extend(data)
            if len(data) < params["per_page"]:
                break
            page += 1
        return results

    def get_org_repos(self, org: str) -> list[dict]:
        return self.paginate(f"{BASE_URL}/orgs/{org}/repos", {"type": "all"})

    def get_contributor_stats(self, org: str, repo: str) -> list[dict]:
        """
        Retorna lista de contribuidores com breakdown semanal de additions/deletions/commits.
        Endpoint: GET /repos/{owner}/{repo}/stats/contributors
        Resposta pode ser 202 enquanto o GitHub computa — o client já trata isso.
        """
        return self.get(f"{BASE_URL}/repos/{org}/{repo}/stats/contributors") or []


# ─── Processamento ────────────────────────────────────────────────────────────

def process_repo(
    client: GitHubClient,
    org: str,
    repo_name: str,
    since_ts: int,
    until_ts: int,
) -> list[dict]:
    stats = client.get_contributor_stats(org, repo_name)
    if not stats:
        return []

    rows = []
    for contributor in stats:
        user = (contributor.get("author") or {}).get("login", "unknown")
        for week in contributor.get("weeks", []):
            week_ts = week["w"]  # Unix timestamp (início da semana, domingo)
            if week_ts < since_ts or week_ts > until_ts:
                continue
            added = week.get("a", 0)
            deleted = week.get("d", 0)
            commits = week.get("c", 0)
            if added == 0 and deleted == 0 and commits == 0:
                continue
            week_date = datetime.fromtimestamp(week_ts, tz=timezone.utc).strftime("%Y-%m-%d")
            rows.append({
                "repo": repo_name,
                "week": week_date,
                "user": user,
                "commits": commits,
                "loc_added": added,
                "loc_deleted": deleted,
                "loc_net": added - deleted,
            })

    return rows


# ─── Apresentação ─────────────────────────────────────────────────────────────

def print_summary(all_rows: list[dict], since_dt: datetime, until_dt: datetime):
    """Exibe tabelas de resumo no terminal."""

    total_commits = sum(r["commits"] for r in all_rows)
    total_added   = sum(r["loc_added"] for r in all_rows)
    total_deleted = sum(r["loc_deleted"] for r in all_rows)

    # Agrega por usuário
    by_user: dict[str, dict] = {}
    for r in all_rows:
        u = r["user"]
        if u not in by_user:
            by_user[u] = {"commits": 0, "loc_added": 0, "loc_deleted": 0, "loc_net": 0, "repos": set()}
        by_user[u]["commits"]     += r["commits"]
        by_user[u]["loc_added"]   += r["loc_added"]
        by_user[u]["loc_deleted"] += r["loc_deleted"]
        by_user[u]["loc_net"]     += r["loc_net"]
        by_user[u]["repos"].add(r["repo"])

    # Agrega por repo
    by_repo: dict[str, dict] = {}
    for r in all_rows:
        rp = r["repo"]
        if rp not in by_repo:
            by_repo[rp] = {"commits": 0, "loc_added": 0, "loc_deleted": 0, "loc_net": 0}
        by_repo[rp]["commits"]     += r["commits"]
        by_repo[rp]["loc_added"]   += r["loc_added"]
        by_repo[rp]["loc_deleted"] += r["loc_deleted"]
        by_repo[rp]["loc_net"]     += r["loc_net"]

    W = 80
    print("\n" + "═" * W)
    print(f"  GitHub LOC Report")
    print(f"  Período : {since_dt.strftime('%Y-%m-%d')} → {until_dt.strftime('%Y-%m-%d')}")
    print(f"  Repos   : {len(by_repo)}   |   Usuários: {len(by_user)}   |   Commits: {total_commits}")
    print(f"  LOC +{total_added:,}  -{total_deleted:,}  net {total_added - total_deleted:+,}")
    print("═" * W)

    # Tabela por usuário
    print(f"\n{'USUÁRIO':<28} {'REPOS':>5} {'COMMITS':>8} {'ADDED':>10} {'DELETED':>10} {'NET':>10}")
    print("─" * W)
    for user, s in sorted(by_user.items(), key=lambda x: x[1]["loc_added"], reverse=True):
        print(
            f"{user:<28} {len(s['repos']):>5} {s['commits']:>8,} "
            f"{s['loc_added']:>10,} {s['loc_deleted']:>10,} {s['loc_net']:>+10,}"
        )

    # Tabela por repo
    print(f"\n{'REPO':<35} {'COMMITS':>8} {'ADDED':>10} {'DELETED':>10} {'NET':>10}")
    print("─" * W)
    for repo, s in sorted(by_repo.items(), key=lambda x: x[1]["loc_added"], reverse=True):
        name = repo if len(repo) <= 35 else repo[:32] + "..."
        print(
            f"{name:<35} {s['commits']:>8,} "
            f"{s['loc_added']:>10,} {s['loc_deleted']:>10,} {s['loc_net']:>+10,}"
        )

    # Tabela por semana (mais recente → mais antiga)
    by_week: dict[str, dict] = {}
    for r in all_rows:
        w = r["week"]
        if w not in by_week:
            by_week[w] = {"commits": 0, "loc_added": 0, "loc_deleted": 0, "loc_net": 0}
        by_week[w]["commits"]     += r["commits"]
        by_week[w]["loc_added"]   += r["loc_added"]
        by_week[w]["loc_deleted"] += r["loc_deleted"]
        by_week[w]["loc_net"]     += r["loc_net"]

    print(f"\n{'SEMANA':<12} {'COMMITS':>8} {'ADDED':>10} {'DELETED':>10} {'NET':>10}")
    print("─" * W)
    for week, s in sorted(by_week.items(), reverse=True):
        print(
            f"{week:<12} {s['commits']:>8,} "
            f"{s['loc_added']:>10,} {s['loc_deleted']:>10,} {s['loc_net']:>+10,}"
        )

    print("═" * W + "\n")


# ─── Main ─────────────────────────────────────────────────────────────────────

def resolve_dates(args) -> tuple[datetime, datetime]:
    today = datetime.now(timezone.utc).replace(hour=23, minute=59, second=59, microsecond=0)

    if args.since:
        since_dt = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    else:
        n = min(args.last_months, 12)  # stats/contributors cobre no máx 52 semanas
        month = today.month - n
        year  = today.year + month // 12
        month = month % 12 or 12
        since_dt = today.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)

    until_dt = (
        datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc)
        if args.until
        else today
    )
    return since_dt, until_dt


def main():
    parser = argparse.ArgumentParser(
        description="Relatório de LOC por usuário/repo para uma org do GitHub.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("org",           help="Nome da organização no GitHub")
    parser.add_argument("--token",       default=os.environ.get("GITHUB_TOKEN"), help="GitHub token")
    parser.add_argument("--last-months", type=int, default=12, metavar="N",
                        help="Últimos N meses (default: 12, máx: 12)")
    parser.add_argument("--since",       help="Data início YYYY-MM-DD")
    parser.add_argument("--until",       help="Data fim YYYY-MM-DD")
    parser.add_argument("--repos",       help="Repos específicos separados por vírgula")
    parser.add_argument("--workers",     type=int, default=8, help="Workers paralelos (default: 8)")
    parser.add_argument("--output",      default="loc_report.csv", help="Arquivo CSV de saída")
    parser.add_argument("--no-csv",      action="store_true", help="Não gera arquivo CSV")
    args = parser.parse_args()

    if not args.token:
        print("Erro: token GitHub necessário. Use --token ou defina GITHUB_TOKEN.")
        sys.exit(1)

    since_dt, until_dt = resolve_dates(args)
    since_ts = int(since_dt.timestamp())
    until_ts = int(until_dt.timestamp())

    client = GitHubClient(args.token)

    if args.repos:
        repos = [{"name": r.strip()} for r in args.repos.split(",")]
    else:
        print(f"Buscando repos de: {args.org}", flush=True)
        repos = client.get_org_repos(args.org)

    print(f"{len(repos)} repos encontrados. Extraindo stats ({args.workers} workers)...", flush=True)

    all_rows: list[dict] = []

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(process_repo, client, args.org, r["name"], since_ts, until_ts): r["name"]
            for r in repos
        }
        done = 0
        for future in as_completed(futures):
            done += 1
            repo_name = futures[future]
            try:
                rows = future.result()
                all_rows.extend(rows)
                if rows:
                    print(f"  [{done}/{len(repos)}] {repo_name}: {len(rows)} semanas com atividade", flush=True)
                else:
                    print(f"  [{done}/{len(repos)}] {repo_name}: sem atividade no período", flush=True)
            except Exception as e:
                print(f"  [{done}/{len(repos)}] {repo_name}: erro — {e}", flush=True)

    if not all_rows:
        print("Nenhum dado encontrado para o período.")
        sys.exit(0)

    print_summary(all_rows, since_dt, until_dt)

    if not args.no_csv:
        all_rows.sort(key=lambda r: (r["week"], r["repo"], r["user"]), reverse=True)
        fieldnames = ["repo", "week", "user", "commits", "loc_added", "loc_deleted", "loc_net"]
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_rows)
        print(f"CSV salvo em: {args.output}")


if __name__ == "__main__":
    main()
