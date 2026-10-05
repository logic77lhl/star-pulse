"""快照保留策略。

data/snapshots/*.json 是 git 里的事实来源，可读、可 diff —— 但每个文件约 290 KB，
一天一个，一年约 105 MB，仓库会无限膨胀。

裁剪之前，先把每天的汇总（日期、仓库数、星数合计，约 80 字节）追加到
data/history/daily_totals.jsonl。这是一个 append-only 的「账本」：

  - 裁剪后，「快照覆盖了多少天 / 首末日」这类问题仍然能正确回答（见
    analyze.data_coverage 的合并逻辑）；
  - 账本用 JSON Lines，追加写入。中途崩溃最多留下一行截断内容，
    读取时按行解析、坏行直接跳过，不会污染已有数据。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import Settings

log = logging.getLogger("star_pulse.retention")


def totals_path(settings: Settings) -> Path:
    return settings.daily_totals_path


def read_totals(settings: Settings) -> dict[str, dict]:
    """读取汇总账本，返回 {date: 记录}。坏行跳过。"""
    path = totals_path(settings)
    out: dict[str, dict] = {}
    if not path.is_file():
        return out
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        log.warning("无法读取汇总账本：%s", path)
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # 可能是崩溃留下的半行，跳过
        date = rec.get("date") if isinstance(rec, dict) else None
        if isinstance(date, str) and date:
            out[date] = rec
    return out


def append_totals(settings: Settings, records: list[dict]) -> Path:
    """追加汇总记录。已记账的日期不重复写（幂等）。"""
    path = totals_path(settings)
    known = read_totals(settings)
    fresh = [r for r in records if r.get("date") and r["date"] not in known]
    if not fresh:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for rec in sorted(fresh, key=lambda r: r["date"]):
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    log.info("汇总账本追加 %d 天 -> %s", len(fresh), path)
    return path


def snapshot_totals(conn) -> list[dict]:
    """从库里算出每天的汇总（只统计有对应 repo 的行，口径与站点一致）。"""
    rows = conn.execute(
        """
        SELECT s.snap_date AS date,
               COUNT(*) AS count,
               COALESCE(SUM(s.stars), 0) AS sum_stars
        FROM snapshot s
        JOIN repo r ON r.repo_id = s.repo_id
        GROUP BY s.snap_date
        ORDER BY s.snap_date
        """
    ).fetchall()
    return [
        {"date": r["date"], "count": int(r["count"]), "sum_stars": int(r["sum_stars"])}
        for r in rows
    ]


def prune(settings: Settings, conn, keep_days: int, dry_run: bool = False) -> dict:
    """只保留最近 keep_days 个快照文件，更早的先记账再删除。

    keep_days 按**文件个数**算，而文件只在交易日产生 —— 所以它等价于
    「最近 N 个交易日」，不会因为长假而误删（按自然日裁剪就会）。
    """
    snapshots = sorted(settings.snapshots_dir.glob("*.json")) if settings.snapshots_dir.is_dir() else []
    keep = max(1, int(keep_days))
    doomed = snapshots[:-keep] if len(snapshots) > keep else []

    totals = {t["date"]: t for t in snapshot_totals(conn)}
    to_archive = [totals[p.stem] for p in doomed if p.stem in totals]
    missing = [p.stem for p in doomed if p.stem not in totals]

    if not dry_run:
        append_totals(settings, to_archive)
        for path in doomed:
            try:
                path.unlink()
            except OSError as exc:
                log.warning("删除快照失败 %s：%s", path.name, exc)

    stats = {
        "snapshots_total": len(snapshots),
        "kept": min(len(snapshots), keep),
        "removed": 0 if dry_run else len(doomed),
        "would_remove": len(doomed),
        "archived_days": len(to_archive),
        "unaccounted": missing[:5],
        "keep_days": keep,
        "dry_run": dry_run,
    }
    if not dry_run and doomed:
        log.info(
            "快照裁剪：删除 %d 个旧文件，保留最近 %d 个（账本已记 %d 天）",
            len(doomed), stats["kept"], len(to_archive),
        )
    return stats