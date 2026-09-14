"""采集流水线：候选发现 + 每日快照。

一个关键优化：Search API 返回的仓库对象里**自带 stargazers_count**，
所以搜索发现的新项目可以顺手完成快照，不需要再单独发一次请求。
这让「发现」这个环节几乎是免费的。

请求预算估算：
  搜索        2–10 次（取决于候选池与门槛）
  候选池快照  <= max_candidates 次
  合计        约等于候选池大小
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import sqlite3

from . import db, github_api, trending
from .config import Settings
from .net import Http

log = logging.getLogger("star_pulse.pipeline")


def _all_names(conn: sqlite3.Connection, limit: int) -> list[str]:
    """已有候选池里的仓库名，按「最近发现 + 星数」排序。"""
    return [row["full_name"] for row in db.candidate_repos(conn, limit)]


def run_daily(settings: Settings, conn: sqlite3.Connection, http: Http) -> dict:
    """一轮完整采集：发现 + 快照。每日跑一次。"""
    today = settings.today()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    repo_rows: list[dict] = []
    snap_rows: list[dict] = []
    channels: dict[str, list[tuple[str, int | None]]] = {}
    seen_ids: set[int] = set()

    def add_channel(repo_id: int, channel: str, rank: int | None) -> None:
        channels.setdefault(str(repo_id), []).append((channel, rank))

    def accept(repo: dict, source: str, channel: str, rank: int | None = None) -> None:
        """把一个仓库纳入本轮 repo + snapshot 结果。"""
        rid = repo["repo_id"]
        add_channel(rid, channel, rank)
        if rid in seen_ids:
            return
        seen_ids.add(rid)
        repo_rows.append(repo)
        snap_rows.append(
            {
                "repo_id": rid,
                "snap_date": today,
                "stars": repo.get("_stars", 0),
                "forks": repo.get("_forks"),
                "open_issues": repo.get("_open_issues"),
                "pushed_at": repo.get("_pushed_at"),
                "source": source,
                "collected_at": now_iso,
            }
        )

    # ── 渠道 A：Search API 找近期创建的新项目（自带星数，零额外请求）──
    search_total = 0
    langs = settings.languages or ["All"]
    for lang in langs:
        remaining = settings.max_candidates - len(seen_ids)
        if remaining <= 0:
            log.info("候选池已达上限 %d，跳过之后的搜索渠道", settings.max_candidates)
            break
        per_lang = max(1, remaining // max(1, len(langs)))
        for rank, repo in enumerate(
            github_api.search_recent_repos(
                http,
                lookback_days=settings.search_lookback_days,
                min_stars=settings.search_min_stars,
                language=lang,
                max_items=per_lang,
            ),
            start=1,
        ):
            accept(repo, "search_api", "gh_search", rank)
            search_total += 1

    # ── 渠道 B：Trending 页（只有名字，需补一次请求）──
    trending_hits = trending.fetch_trending(http, settings.trending_since, settings.languages)
    trending_resolved = 0
    for hit in trending_hits:
        if len(seen_ids) >= settings.max_candidates:
            break
        repo = github_api.get_repo(http, hit["full_name"])
        if repo:
            accept(repo, "github_api", "gh_trending", hit.get("rank"))
            trending_resolved += 1

    # ── 渠道 C：种子池（watchlist + 上一期候选池）──
    pool: list[str] = list(settings.seed_repos)
    pool.extend(_all_names(conn, settings.max_candidates))
    dedup_pool: list[str] = []
    seen_names: set[str] = set()
    for name in pool:
        key = name.lower()
        if key not in seen_names:
            seen_names.add(key)
            dedup_pool.append(name)

    known_ids = {r["repo_id"] for r in db.candidate_repos(conn, settings.max_candidates)}
    pool_budget = max(0, settings.max_candidates - len(seen_ids))
    refreshed = 0
    for name in dedup_pool[:pool_budget]:
        existing = conn.execute(
            "SELECT repo_id FROM repo WHERE lower(full_name) = ?", (name.lower(),)
        ).fetchone()
        if existing and existing["repo_id"] in seen_ids:
            continue  # 本轮已经通过搜索或 Trending 采过了
        repo = github_api.get_repo(http, name)
        if repo:
            accept(repo, "github_api", "seed", None)
            refreshed += 1
        if len(seen_ids) >= settings.max_candidates:
            log.info("已达候选池上限 %d，停止扩充", settings.max_candidates)
            break

    # ── 落库 ──
    db.upsert_repos(conn, repo_rows, today)
    n_snap = db.upsert_snapshots(conn, snap_rows)
    for rid, items in channels.items():
        for channel, rank in items:
            db.add_discovery(conn, today, channel, [(int(rid), rank)])

    json_path = db.export_snapshot_json(settings.snapshots_dir, today, repo_rows)

    stats = {
        "date": today,
        "repos_upserted": len(repo_rows),
        "snapshots_written": n_snap,
        "from_search": search_total,
        "from_trending": trending_resolved,
        "refreshed_from_pool": refreshed,
        "candidates_total": len(seen_ids),
        "json": str(json_path),
        "http": http.stats(),
        "known_pool_size": len(known_ids),
    }
    log.info(
        "采集完成 %s：快照 %d 个（搜索 %d / Trending %d / 池内刷新 %d）",
        today, n_snap, search_total, trending_resolved, refreshed,
    )
    return stats


def snapshot_only(settings: Settings, conn: sqlite3.Connection, http: Http, names: list[str]) -> dict:
    """给指定仓库拍快照（用于手动验证、补跑）。"""
    today = settings.today()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    repo_rows: list[dict] = []
    snap_rows: list[dict] = []

    for name in names:
        repo = github_api.get_repo(http, name.strip())
        if not repo:
            log.warning("跳过 %s（取不到数据）", name)
            continue
        repo_rows.append(repo)
        snap_rows.append(
            {
                "repo_id": repo["repo_id"],
                "snap_date": today,
                "stars": repo.get("_stars", 0),
                "forks": repo.get("_forks"),
                "open_issues": repo.get("_open_issues"),
                "pushed_at": repo.get("_pushed_at"),
                "source": "manual",
                "collected_at": now_iso,
            }
        )

    db.upsert_repos(conn, repo_rows, today)
    n = db.upsert_snapshots(conn, snap_rows)
    if repo_rows:
        db.export_snapshot_json(settings.snapshots_dir, today, repo_rows)
    return {"date": today, "snapshots_written": n, "http": http.stats()}
