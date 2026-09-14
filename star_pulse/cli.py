"""命令行入口。

    python -m star_pulse doctor                 体检：配置与数据源连通性
    python -m star_pulse run-daily              每日采集（发现 + 快照）
    python -m star_pulse snapshot a/b c/d       给指定仓库拍快照（验证用）
    python -m star_pulse report --period last-week   生成周报
    python -m star_pulse translate              给仓库简介生成中文译文（需配置 LLM）
    python -m star_pulse rebuild                从 JSON 快照重建 SQLite
    python -m star_pulse stats                  查看快照覆盖情况
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import analyze, db, github_api, i18n, llm, pipeline, render
from . import site as site_builder
from .config import load_settings
from .net import BudgetExceeded, Http

log = logging.getLogger("star_pulse")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def prepare(settings):
    """建库 + 把 JSON 快照回灌（CI 每次都是空环境，靠这一步拿到历史）。"""
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    if settings.snapshots_dir.is_dir():
        repos, snaps = db.import_snapshot_jsons(conn, settings.snapshots_dir)
        if snaps:
            log.info("从 JSON 快照回灌 %d 条记录（%d 个仓库）", snaps, repos)
    return conn


def make_http(settings) -> Http:
    return Http(
        user_agent=settings.user_agent,
        token=settings.token,
        request_gap=settings.request_gap_seconds,
        max_wait=settings.max_wait_seconds,
        timeout=settings.timeout_seconds,
        retries=settings.retries,
    )


# ── 子命令 ──────────────────────────────────────────────────────
def cmd_doctor(settings, args) -> int:
    print("star-pulse 体检")
    print("=" * 56)
    print(f"项目根目录   : {settings.root}")
    print(f"快照目录     : {settings.snapshots_dir}")
    print(f"报告目录     : {settings.reports_dir}")
    print(f"GITHUB_TOKEN : {'已配置（5000 次/小时）' if settings.token else '未配置（仅 60 次/小时）'}")
    print(f"LLM 解读     : {'已启用 ' + settings.llm_model if settings.llm_enabled else '未启用（不影响主体功能）'}")
    print(f"候选池上限   : {settings.max_candidates}")
    print(f"种子仓库     : {len(settings.seed_repos)} 个")
    print(f"时区         : UTC+{settings.tz_offset_hours}，今天 = {settings.today()}")
    print()

    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    cov = analyze.data_coverage(conn)
    print(f"本地数据     : {db.repo_count(conn)} 个仓库，{cov['distinct_days']} 天快照"
          f"（{cov['first_day'] or '—'} ~ {cov['last_day'] or '—'}）")
    print(f"增量榜可用   : {'是' if cov['warm'] else '否 —— 预热中，还需约 %d 天' % max(0, 8 - cov['distinct_days'])}")
    print()

    http = make_http(settings)
    print("数据源连通性：")
    repo = github_api.get_repo(http, "microsoft/markitdown")
    print(f"  REST /repos        : {'OK' if repo else '失败'}"
          + (f"（markitdown {repo['_stars']:,} 星）" if repo else ""))
    search = github_api.search_recent_repos(http, lookback_days=3, min_stars=100, max_items=1)
    print(f"  Search API         : {'OK' if search else '失败或无结果'}")
    from . import trending
    hits = trending.fetch_trending(http, "daily")
    print(f"  Trending 页面      : {'OK，%d 个仓库' % len(hits) if hits else '不可用（不致命，Search API 会兜底）'}")
    print()
    print(f"配额状态：{http.stats()}")
    return 0


def cmd_run_daily(settings, args) -> int:
    conn = prepare(settings)
    http = make_http(settings)

    if args.limit:
        settings.max_candidates = args.limit

    pool = len(db.candidate_repos(conn, settings.max_candidates))
    needed = min(settings.max_candidates, max(pool, 1) + 40) + 10
    warning = settings.budget_check(needed)
    if warning:
        print(warning, file=sys.stderr)
        if not settings.token:
            print("\n提示：可以先用 --limit 20 小规模试跑，验证链路是否通畅。", file=sys.stderr)
            return 2

    try:
        stats = pipeline.run_daily(settings, conn, http)
    except BudgetExceeded as exc:
        log.error("本轮中止：%s", exc)
        return 3

    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if stats.get("errors"):
        # 数据已经落库了，所以这里返回 0（让后续的站点重建与提交照常进行），
        # 但要把问题喊出来 —— 静默的部分失败比彻底失败更危险。
        print()
        print("⚠️ 本轮采集有中断，已保存获取到的部分：", file=sys.stderr)
        for e in stats["errors"]:
            print(f"   {e}", file=sys.stderr)
        print("   下一天的采集会自动补齐时间序列（快照表是同一天幂等覆盖）。", file=sys.stderr)
    return 0


def cmd_snapshot(settings, args) -> int:
    conn = prepare(settings)
    http = make_http(settings)
    names = [n for raw in args.repos for n in raw.split(",") if n.strip()]
    if not names:
        print("请用 --repos 指定仓库，例如：--repos ayghri/i-have-adhd", file=sys.stderr)
        return 1
    warning = settings.budget_check(len(names))
    if warning:
        print(warning, file=sys.stderr)
    stats = pipeline.snapshot_only(settings, conn, http, names)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def cmd_report(settings, args) -> int:
    conn = prepare(settings)
    spec = args.period
    if spec in ("last-week", "this-week"):
        start, end = analyze.period_for(settings, spec)
    else:
        start, end = analyze.parse_period(spec)

    coverage = analyze.data_coverage(conn)
    log.info("生成报告：%s ~ %s（本地数据覆盖 %d 天）", start, end, coverage["distinct_days"])

    narration: dict[str, str] = {}
    if settings.llm_enabled and not args.no_llm:
        ranked, _ = analyze.weekly_gain(conn, start, end, settings.max_span_days)
        preview = ranked[: settings.top_n]
        meta_map = {r["full_name"]: dict(r) for r in conn.execute(
            "SELECT full_name, description, language, topics FROM repo").fetchall()}
        for it in preview:
            info = meta_map.get(it["full_name"], {})
            it["category"] = analyze.classify({**info, "full_name": it["full_name"]})
            it["description"] = info.get("description")
            it["language"] = it.get("language") or info.get("language")
        narration = llm.narrate(settings, preview)

    md, html, meta = render.build_report(settings, conn, start, end, narration)

    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{meta['week']}_{start}_{end}"
    md_path = settings.reports_dir / f"{stem}.md"
    html_path = settings.reports_dir / f"{stem}.html"
    json_path = settings.reports_dir / f"{stem}.json"
    md_path.write_text(md, encoding="utf-8")
    html_path.write_text(html, encoding="utf-8")

    compact = {k: v for k, v in meta.items() if k not in ("top", "new_repos")}
    compact["top"] = [
        {k: v for k, v in it.items() if k != "description"} for it in meta["top"]
    ]
    json_path.write_text(json.dumps(compact, ensure_ascii=False, indent=1), encoding="utf-8")

    db.save_report(conn, start, end, "weekly", md, compact, settings.now_local().isoformat(timespec="seconds"))

    print(f"已生成：")
    print(f"  {md_path}")
    print(f"  {html_path}")
    print(f"  {json_path}")
    if not meta["warm"]:
        print()
        print(f"注意：本期未产出增量榜。{meta['warmup_note']}")
    return 0


def cmd_site(settings, args) -> int:
    """生成 docs/ 静态站点（GitHub Pages 用）。"""
    conn = prepare(settings)
    stats = site_builder.build_site(settings, conn)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    dates = analyze.all_snapshot_dates(conn)
    if len(dates) < 8:
        print()
        print(f"提示：已积累 {len(dates)} 天快照，还需约 {8 - len(dates)} 天才能形成完整的 7 天窗口。")
        print("站点现在就可用，只是「累计增长榜」会基于当前已有的区间。")
    return 0


def cmd_translate(settings, args) -> int:
    """给仓库简介生成中文译文，写入 data/i18n/zh.json。

    只翻译缓存里还没有的条目，所以反复跑不会重复花钱，也能分批续跑。
    产物**必须提交进 git** —— CI 每次都是全新环境，缓存不在仓库里，
    每天重建看板时就等于白翻一次。
    """
    conn = prepare(settings)
    rows = conn.execute(
        """
        SELECT repo_id, full_name, description, language, topics
        FROM repo
        WHERE description IS NOT NULL AND description <> ''
        ORDER BY full_name
        """
    ).fetchall()
    items = [dict(r) for r in rows]
    if args.limit:
        items = items[: args.limit]
    for it in items:
        it["category"] = analyze.classify(it)

    before = len(i18n.load_cache(settings))
    stats = i18n.translate_missing(settings, items, batch_size=args.batch)
    after = len(i18n.load_cache(settings))

    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print(f"\n缓存文件：{i18n.cache_path(settings)}")
    print(f"缓存条目：{before} → {after}（库内共 {db.repo_count(conn)} 个仓库）")

    if "error" in stats:
        print(f"\n{stats['error']}", file=sys.stderr)
        return 2
    if stats["failed_batches"]:
        print("\n⚠️ 有批次失败。已成功的部分都写进缓存了，再跑一次只会补缺失的部分。",
              file=sys.stderr)
    if stats["translated"]:
        print("\n提交 data/i18n/zh.json：CI 靠它复用译文，缓存不进仓库就每次都白翻。")
    return 0


def cmd_rebuild(settings, args) -> int:
    if settings.db_path.exists():
        settings.db_path.unlink()
        print(f"已删除旧的 {settings.db_path.name}")
    conn = prepare(settings)
    cov = analyze.data_coverage(conn)
    print(f"重建完成：{db.repo_count(conn)} 个仓库，{cov['distinct_days']} 天快照")
    return 0


def cmd_stats(settings, args) -> int:
    conn = prepare(settings)
    cov = analyze.data_coverage(conn)
    print(json.dumps(cov, ensure_ascii=False, indent=2))
    dates = db.snapshot_dates(conn, 14)
    print("最近快照日期：", ", ".join(reversed(dates)) or "无")
    return 0


# ── 参数解析 ────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="star_pulse",
        description="GitHub 星耀榜生成器：每日快照 + 周增排名",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="输出调试日志")
    parser.add_argument("--root", type=Path, default=None, help="项目根目录（默认自动识别）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="体检：配置与数据源连通性")
    p_daily = sub.add_parser("run-daily", help="每日采集（发现 + 快照）")
    p_daily.add_argument("--limit", type=int, default=0, help="临时覆盖候选池上限，便于小规模试跑")
    sub.add_parser("rebuild", help="从 JSON 快照重建 SQLite")
    sub.add_parser("stats", help="查看快照覆盖情况")
    sub.add_parser("site", help="生成 docs/ 静态站点（GitHub Pages）")

    p_snap = sub.add_parser("snapshot", help="给指定仓库拍快照")
    p_snap.add_argument("--repos", nargs="+", required=True, help="owner/repo，可空格或逗号分隔")

    p_rep = sub.add_parser("report", help="生成周报")
    p_rep.add_argument(
        "--period", default="last-week",
        help="last-week | this-week | YYYY-MM-DD:YYYY-MM-DD（默认 last-week）",
    )
    p_rep.add_argument("--no-llm", action="store_true", help="强制跳过 LLM 解读")

    p_tr = sub.add_parser("translate", help="给仓库简介生成中文译文（需配置 LLM）")
    p_tr.add_argument("--limit", type=int, default=0, help="只处理前 N 个仓库（试跑用）")
    p_tr.add_argument("--batch", type=int, default=60, help="每次请求翻译多少条（默认 60）")

    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    settings = load_settings(args.root)
    handlers = {
        "doctor": cmd_doctor,
        "run-daily": cmd_run_daily,
        "snapshot": cmd_snapshot,
        "report": cmd_report,
        "translate": cmd_translate,
        "rebuild": cmd_rebuild,
        "stats": cmd_stats,
        "site": cmd_site,
    }
    return handlers[args.command](settings, args)


if __name__ == "__main__":
    raise SystemExit(main())
