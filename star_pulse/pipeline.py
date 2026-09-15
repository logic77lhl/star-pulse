"""采集流水线：候选发现 + 每日快照。

一个关键优化：Search API 返回的仓库对象里**自带 stargazers_count**，
所以搜索发现的新项目可以顺手完成快照，不需要再单独发一次请求。
这让「发现」这个环节几乎是免费的。

请求预算估算：
  搜索        2–10 次（取决于候选池与门槛）
  候选池快照  <= max_candidates 次
  合计        约等于候选池大小

四个渠道，顺序即优先级：种子 > Trending > 池内续期 > 搜索新项目。
把「续期」排在「搜索」之前是有意的 —— 搜索只覆盖最近 N 天创建的仓库，
若让它先吃满名额，老项目就会静默掉出候选池、时间序列断档（详见下方注释）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import sqlite3

from . import db, github_api, trending
from .config import Settings
from .net import Http

log = logging.getLogger("star_pulse.pipeline")


def _pool_names(conn: sqlite3.Connection, limit: int) -> list[str]:
    """已有候选池里的仓库名，按「已追踪最久」排序（见 db.pool_refresh_repos）。"""
    return [row["full_name"] for row in db.pool_refresh_repos(conn, limit)]


def run_daily(settings: Settings, conn: sqlite3.Connection, http: Http) -> dict:
    """一轮完整采集：发现 + 快照。每日跑一次。

    渠道顺序刻意如此：种子(watchlist) > Trending > 池内续期 > 搜索新项目。
    因为请求预算有限，谁先来谁占名额，所以优先级就是「谁更不可替代」：

    - 种子是用户显式关注的，必须保证进池；
    - Trending 是当期热点，且只有这一个渠道能提供；
    - 池内续期排第三：搜索只覆盖最近 N 天创建的仓库，老项目一旦滑出窗口就
      再也没有渠道能发现它。若让搜索先吃满名额，时间序列会静默断档；
    - 搜索垫底，用剩下的名额发现新项目。
    """
    today = settings.today()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cap = max(1, settings.max_candidates)
    # 给池内续期预留的保底名额。0 = 不预留（搜索可吃满整个池，即旧行为）
    reserve = max(0, min(settings.pool_refresh_reserve, cap))

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

    # 计数放进 dict，这样即使中途异常也能带着已有成绩落到持久化步骤
    counts = {"watchlist": 0, "trending": 0, "search": 0, "pool_refresh": 0}
    errors: list[str] = []

    try:
        # ── 渠道 A：种子池（watchlist）──────────────────────────
        # 放在最前面且不设上限：这些是用户显式关注的项目，必须保证一定进池。
        # 早先版本里 Search 渠道会先把名额吃光，导致 watchlist 反而进不来，已修正。
        for name in settings.seed_repos:
            if len(seen_ids) >= cap:
                log.warning("候选池已满，%d 个 watchlist 仓库未能纳入", len(settings.seed_repos))
                break
            repo = github_api.get_repo(http, name)
            if repo:
                accept(repo, "github_api", "seed", None)
                counts["watchlist"] += 1

        # ── 渠道 B：Trending 页（只有名字，需补一次请求）──
        for hit in trending.fetch_trending(http, settings.trending_since, settings.languages):
            if len(seen_ids) >= cap:
                break
            repo = github_api.get_repo(http, hit["full_name"])
            if repo:
                accept(repo, "github_api", "gh_trending", hit.get("rank"))
                counts["trending"] += 1

        # ── 渠道 C：池内续期（**必须排在搜索之前**）────────────────
        # 搜索渠道只覆盖「最近 N 天创建」的仓库。一个项目滑出这个窗口后就再也
        # 没有渠道能发现它 —— 当天拿不到快照，星数增量永久中断。所以先按「已追踪
        # 最久」把保底名额续上，剩下的名额才交给搜索去填新项目。
        # 保底名额见 pool_refresh_reserve；设为 0 即退回「搜索优先」的旧行为。
        pool_ceiling = min(cap, len(seen_ids) + reserve)
        for name in _pool_names(conn, cap):
            if len(seen_ids) >= pool_ceiling:
                break
            existing = conn.execute(
                "SELECT repo_id FROM repo WHERE lower(full_name) = ?", (name.lower(),)
            ).fetchone()
            if existing and existing["repo_id"] in seen_ids:
                continue  # 本轮已经通过前面渠道采过了
            repo = github_api.get_repo(http, name)
            if repo:
                accept(repo, "github_api", "pool", None)
                counts["pool_refresh"] += 1

        # ── 渠道 D：Search API 找近期创建的新项目（自带星数，零额外请求）──
        # 填满剩余名额。内层循环必须有硬上限检查：少了它，一旦 GitHub 返回的结果集
        # 大于剩余预算，候选池会越过 max_candidates，多发的请求可能撑爆限流配额。
        langs = settings.languages or ["All"]
        for idx, lang in enumerate(langs):
            left = cap - len(seen_ids)
            if left <= 0:
                log.info("候选池已达上限 %d，跳过之后的搜索渠道", cap)
                break
            per_lang = max(1, left // (len(langs) - idx))
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
                if len(seen_ids) >= cap:
                    break
                accept(repo, "search_api", "gh_search", rank)
                counts["search"] += 1

    except Exception as exc:  # noqa: BLE001
        # 关键：任何意外都不能丢掉已经采到的数据。
        # 之前这里没有兜底，一次响应体截断就让整轮 800 个仓库全部作废。
        msg = f"{type(exc).__name__}: {exc}"
        errors.append(msg)
        log.exception("采集中途出错，将保存已获取的 %d 个仓库", len(repo_rows))

    # ── 落库（无论上方是否出错都执行）──
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
        "candidates_total": len(seen_ids),
        "by_channel": counts,
        "errors": errors,
        "json": str(json_path),
        "http": http.stats(),
    }
    log.info(
        "采集完成 %s：快照 %d 个（watchlist %d / Trending %d / 搜索 %d / 池内刷新 %d）",
        today, n_snap, counts["watchlist"], counts["trending"],
        counts["search"], counts["pool_refresh"],
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
