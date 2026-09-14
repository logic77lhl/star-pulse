"""分析层：增量计算、防刷星过滤、分类、周期工具。

一条铁律：所有数字都来自 SQL，LLM 不参与任何计算。
周榜最常见的质量缺陷是把「跨度 14 天的增量」和「跨度 7 天的增量」放在一起排名，
所以这里把跨度（span）算出来并强制检查，超限直接剔除而不是静默留在榜上。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta

from .config import Settings

# ── 周期工具 ────────────────────────────────────────────────────
def week_bounds(day: date) -> tuple[date, date]:
    """返回 day 所在自然周的 周一、周日。"""
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


def period_for(settings: Settings, kind: str) -> tuple[str, str]:
    """计算报告周期。
    last-week：上一完整自然周（周一~周日）
    this-week：本周至今
    """
    today = settings.now_local().date()
    if kind == "this-week":
        start, _ = week_bounds(today)
        return start.isoformat(), today.isoformat()
    start, end = week_bounds(today)
    prev_start = start - timedelta(days=7)
    return prev_start.isoformat(), (prev_start + timedelta(days=6)).isoformat()


def parse_period(spec: str) -> tuple[str, str]:
    """支持 'last-week' / 'this-week' / 'YYYY-MM-DD:YYYY-MM-DD'。"""
    if ":" in spec:
        a, b = spec.split(":", 1)
        return a.strip(), b.strip()
    return spec, spec


# ── 增量计算 ────────────────────────────────────────────────────
_GAIN_SQL = """
WITH bounds AS (
  SELECT repo_id,
         MAX(CASE WHEN snap_date <= :start THEN snap_date END) AS d0,
         MAX(CASE WHEN snap_date <= :end   THEN snap_date END) AS d1
  FROM snapshot
  WHERE snap_date <= :end
  GROUP BY repo_id
)
SELECT r.repo_id, r.full_name, r.language, r.description, r.repo_created,
       b.d0 AS base_date, b.d1 AS head_date,
       s0.stars AS stars_before, s1.stars AS stars_after,
       s1.stars - s0.stars AS delta,
       COALESCE(s1.forks, 0) - COALESCE(s0.forks, 0) AS delta_forks,
       COALESCE(s1.forks, 0) AS forks
FROM bounds b
JOIN snapshot s0 ON s0.repo_id = b.repo_id AND s0.snap_date = b.d0
JOIN snapshot s1 ON s1.repo_id = b.repo_id AND s1.snap_date = b.d1
JOIN repo r      ON r.repo_id  = b.repo_id
WHERE b.d0 <> b.d1
  AND r.is_fork = 0
  AND r.is_archived = 0
  AND s1.stars > s0.stars
ORDER BY delta DESC
"""


def weekly_gain(conn: sqlite3.Connection, start: str, end: str, max_span_days: int) -> tuple[list[dict], list[dict]]:
    """返回 (合格榜单, 因跨度超限被剔除的仓库)。"""
    rows = conn.execute(_GAIN_SQL, {"start": start, "end": end}).fetchall()
    ranked: list[dict] = []
    dropped: list[dict] = []
    for row in rows:
        item = dict(row)
        try:
            span = (date.fromisoformat(item["head_date"]) - date.fromisoformat(item["base_date"])).days
        except (TypeError, ValueError):
            span = 999
        item["span_days"] = span
        if span > max_span_days:
            dropped.append(item)
        else:
            ranked.append(item)
    return ranked, dropped


def daily_history(conn: sqlite3.Connection, repo_id: int, start: str, end: str) -> list[tuple[str, int]]:
    rows = conn.execute(
        """SELECT snap_date, stars FROM snapshot
           WHERE repo_id = ? AND snap_date BETWEEN ? AND ?
           ORDER BY snap_date""",
        (repo_id, start, end),
    ).fetchall()
    return [(r["snap_date"], r["stars"]) for r in rows]


def data_coverage(conn: sqlite3.Connection) -> dict:
    """快照覆盖情况，用于在报告里如实说明「本期数据有多可靠」。"""
    row = conn.execute(
        """SELECT COUNT(DISTINCT snap_date) AS days,
                  MIN(snap_date) AS first_day,
                  MAX(snap_date) AS last_day,
                  COUNT(*) AS rows_total
           FROM snapshot"""
    ).fetchone()
    days = row["days"] or 0
    return {
        "distinct_days": days,
        "first_day": row["first_day"],
        "last_day": row["last_day"],
        "rows_total": row["rows_total"] or 0,
        "warm": days >= 8,  # 满 8 天后，才有可靠的 7 天窗口
    }


# ── 新项目榜（不依赖历史，第一天就能出）────────────────────────
def new_repos_in_period(conn: sqlite3.Connection, start: str, end: str, limit: int = 50) -> list[dict]:
    """周期内**创建**的仓库，按当前星数排序。

    注意它的含义与「本周涨星榜」完全不同：这是「新项目榜」，
    衡量的是项目起跑速度，而不是存量项目的增长。两者不能混为一谈。
    """
    rows = conn.execute(
        """
        SELECT r.repo_id, r.full_name, r.language, r.description, r.repo_created,
               COALESCE((SELECT s.stars FROM snapshot s
                         WHERE s.repo_id = r.repo_id
                         ORDER BY s.snap_date DESC LIMIT 1), 0) AS stars,
               COALESCE((SELECT s.forks FROM snapshot s
                         WHERE s.repo_id = r.repo_id
                         ORDER BY s.snap_date DESC LIMIT 1), 0) AS forks
        FROM repo r
        WHERE r.repo_created BETWEEN :start AND :end
          AND r.is_fork = 0 AND r.is_archived = 0
        ORDER BY stars DESC
        LIMIT :limit
        """,
        {"start": start, "end": end, "limit": limit},
    ).fetchall()
    return [dict(r) for r in rows]


# ── 防刷星启发式 ────────────────────────────────────────────────
def star_farm_flags(item: dict, history: list[tuple[str, int]]) -> list[str]:
    """给可疑仓库打标记。只标注、不剔除——把判断权交回给读者。"""
    flags: list[str] = []
    delta = item.get("delta") or 0
    delta_forks = item.get("delta_forks") or 0

    if delta > 0 and delta / max(delta_forks, 1) > 300:
        flags.append("星叉比异常")

    daily: list[int] = []
    for i in range(1, len(history)):
        daily.append(history[i][1] - history[i - 1][1])
    daily = [d for d in daily if d >= 0]
    if len(daily) >= 3:
        peak = max(daily)
        rest = sorted(daily)[:-1]
        baseline = sum(rest) / len(rest) if rest else 0
        if baseline > 0 and peak > 10 * baseline:
            flags.append("单日跳变")

    created = item.get("repo_created")
    if created:
        try:
            age = (date.today() - date.fromisoformat(created)).days
            if age < 30 and (item.get("stars_after") or 0) > 50000:
                flags.append("新库暴星")
        except ValueError:
            pass

    if (item.get("stars_after") or 0) > 20000 and (item.get("forks") or 0) < 500:
        flags.append("叉数过低")

    return flags


# ── 分类 ────────────────────────────────────────────────────────
# 顺序即优先级：命中数相同时靠前的胜出
#
# 关键词一律按「整词」匹配，不做子串匹配 —— 否则 "ide" 会命中 "Video"、
# "map" 会命中 "Mapper"，分类结果会莫名其妙。
# 短语（含空格）才用子串匹配，因为短语本身已经足够特异。
_CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("输出规范", ("adhd", "concise", "humanize", "de-ai", "no filler", "output style",
                  "prompt style", "action first", "writing style", "verbosity", "tone")),
    ("Agent Skill", ("skill", "claude", "cursor", "codex", "agent", "harness", "plugin",
                     "prompt", "rules", "subagent", "workflow")),
    ("MCP", ("mcp", "model context protocol")),
    ("本地优先", ("local first", "self hosted", "selfhosted", "offline", "on device",
                  "privacy", "docker")),
    ("模型与训练", ("llm", "model", "training", "fine tune", "finetune", "inference",
                    "transformer", "diffusion", "weights", "dataset", "embedding")),
    ("可视化", ("diagram", "chart", "visualization", "visualize", "graph", "map", "globe",
                "3d", "render", "canvas", "svg", "satellite", "spatial", "geospatial",
                "earth", "orbital", "astronomy", "weather", "flight")),
    ("媒体生成", ("video", "audio", "voice", "speech", "music", "image", "tts", "animation")),
    ("开发工具", ("cli", "tool", "devtool", "editor", "ide", "formatter", "linter",
                  "compiler", "runtime", "sdk", "library", "framework", "api")),
    ("科研教育", ("research", "paper", "science", "academic", "classroom", "education",
                  "course", "university", "math", "student", "teacher")),
    ("数据与基础设施", ("database", "storage", "server", "infra", "kubernetes", "proxy",
                        "network", "queue", "cache", "deploy")),
]

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _normalize(text: str) -> str:
    """把连字符/下划线/斜杠统一成空格，让 "local-first" 与 "local first" 等价。"""
    return re.sub(r"[-_/]", " ", str(text).lower())


def _score(haystack: str, tokens: set[str], keywords: tuple[str, ...]) -> int:
    hits = 0
    for kw in keywords:
        key = _normalize(kw)
        if " " in key:
            if key in haystack:
                hits += 1
        elif key in tokens or f"{key}s" in tokens:
            hits += 1
    return hits


def classify(repo: dict) -> str:
    """基于 topics + description + 仓库名的关键词分类。

    确定性、可复现、无幻觉 —— 刻意不用 LLM，因为分类结果会出现在每期报告里，
    需要可回归测试。它只影响可读性，不影响任何排名或数值。
    """
    topics = repo.get("topics") or ""
    if isinstance(topics, (list, tuple)):
        topics = " ".join(str(t) for t in topics)

    haystack = _normalize(" ".join([
        repo.get("description") or "",
        repo.get("full_name") or "",
        repo.get("language") or "",
        str(topics),
    ]))
    tokens = set(_TOKEN_RE.findall(haystack))

    best_label = "其他"
    best_score = 0
    for label, keywords in _CATEGORY_RULES:
        score = _score(haystack, tokens, keywords)
        if score > best_score:
            best_score = score
            best_label = label
    return best_label


def category_breakdown(rows: list[dict], repo_meta: dict[str, dict]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for row in rows:
        meta = repo_meta.get(row["full_name"], {})
        label = classify({**meta, "full_name": row["full_name"]})
        counts[label] = counts.get(label, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])


# ── 日维度查询（供站点看板使用）─────────────────────────────────
def all_snapshot_dates(conn: sqlite3.Connection) -> list[str]:
    """全部快照日期，升序。"""
    return [
        r["snap_date"]
        for r in conn.execute(
            "SELECT DISTINCT snap_date FROM snapshot ORDER BY snap_date"
        ).fetchall()
    ]


def daily_rows(conn: sqlite3.Connection, day: str) -> list[dict]:
    """某一天采集到的全部仓库及其星数。"""
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT r.full_name, r.language, r.description, r.repo_created,
                   s.stars, s.forks
            FROM snapshot s JOIN repo r ON r.repo_id = s.repo_id
            WHERE s.snap_date = :day AND r.is_fork = 0 AND r.is_archived = 0
            ORDER BY s.stars DESC
            """,
            {"day": day},
        ).fetchall()
    ]


def daily_gain(
    conn: sqlite3.Connection, day: str, prev_day: str, limit: int | None = None
) -> list[dict]:
    """单日涨幅榜：day 相对于 prev_day 的新增 Star。

    注意与周增榜的区别：这里只比较**相邻两个快照日**，
    要求两个日期都真实存在（不取「最近一次」），所以口径最干净。
    """
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    rows = conn.execute(
        f"""
        SELECT r.full_name, r.language,
               s1.stars - s0.stars AS delta,
               s1.stars AS stars
        FROM snapshot s1
        JOIN snapshot s0 ON s0.repo_id = s1.repo_id AND s0.snap_date = :prev
        JOIN repo r      ON r.repo_id  = s1.repo_id
        WHERE s1.snap_date = :day
          AND r.is_fork = 0 AND r.is_archived = 0
          AND s1.stars > s0.stars
        ORDER BY delta DESC
        {limit_sql}
        """,
        {"day": day, "prev": prev_day},
    ).fetchall()
    return [dict(r) for r in rows]


def daily_summary(conn: sqlite3.Connection) -> list[dict]:
    """逐日汇总，正好对应「每天的情况」这个需求。

    每一天给出：当天入库仓库数、全网池内新增星数合计、当天涨幅冠军、
    以及当天相对前一天新出现的仓库数（候选池扩张情况）。
    """
    dates = all_snapshot_dates(conn)
    out: list[dict] = []
    for i, day in enumerate(dates):
        prev = dates[i - 1] if i > 0 else None
        count = conn.execute(
            "SELECT COUNT(*) c FROM snapshot WHERE snap_date = ?", (day,)
        ).fetchone()["c"]

        item = {
            "date": day,
            "repos": count,
            "is_first": prev is None,
            "total_gain": 0,
            "gainers": 0,
            "top_name": None,
            "top_delta": 0,
            "new_repos": 0,
            "prev": prev,
        }
        if prev:
            agg = conn.execute(
                """
                SELECT COALESCE(SUM(s1.stars - s0.stars), 0) AS total,
                       COUNT(*) AS gainers
                FROM snapshot s1
                JOIN snapshot s0 ON s0.repo_id = s1.repo_id AND s0.snap_date = :prev
                JOIN repo r ON r.repo_id = s1.repo_id
                WHERE s1.snap_date = :day AND s1.stars > s0.stars
                  AND r.is_fork = 0 AND r.is_archived = 0
                """,
                {"day": day, "prev": prev},
            ).fetchone()
            item["total_gain"] = int(agg["total"] or 0)
            item["gainers"] = int(agg["gainers"] or 0)
            top = daily_gain(conn, day, prev, limit=1)
            if top:
                item["top_name"] = top[0]["full_name"]
                item["top_delta"] = top[0]["delta"]
            item["new_repos"] = conn.execute(
                """
                SELECT COUNT(*) c FROM snapshot s1
                WHERE s1.snap_date = :day
                  AND NOT EXISTS (SELECT 1 FROM snapshot s0
                                  WHERE s0.repo_id = s1.repo_id AND s0.snap_date = :prev)
                """,
                {"day": day, "prev": prev},
            ).fetchone()["c"]
        out.append(item)
    return out


def growth_series(
    conn: sqlite3.Connection, dates: list[str], limit: int = 8
) -> tuple[list[str], list[dict]]:
    """挑出「累计增量最大」的若干个仓库，返回它们相对首日的累计增量走势。

    为什么用「相对首日的累计增量」而不是星数绝对值：后者跨度可达 20 倍
    （3 万星 vs 26 万星），画在同一张折线图上什么都看不出来。
    """
    if len(dates) < 2:
        return dates, []
    first, last = dates[0], dates[-1]
    movers = conn.execute(
        """
        SELECT r.full_name, r.language,
               s1.stars - s0.stars AS gain
        FROM snapshot s1
        JOIN snapshot s0 ON s0.repo_id = s1.repo_id AND s0.snap_date = :first
        JOIN repo r      ON r.repo_id  = s1.repo_id
        WHERE s1.snap_date = :last AND s1.stars > s0.stars
          AND r.is_fork = 0 AND r.is_archived = 0
        ORDER BY gain DESC
        LIMIT :limit
        """,
        {"first": first, "last": last, "limit": limit},
    ).fetchall()

    series = []
    for row in movers:
        points = conn.execute(
            """
            SELECT s.snap_date, s.stars FROM snapshot s
            JOIN repo r ON r.repo_id = s.repo_id
            WHERE r.full_name = :name AND s.snap_date BETWEEN :first AND :last
            ORDER BY s.snap_date
            """,
            {"name": row["full_name"], "first": first, "last": last},
        ).fetchall()
        if len(points) < 2:
            continue
        base = points[0]["stars"]
        series.append(
            {
                "full_name": row["full_name"],
                "language": row["language"],
                "total_gain": row["gain"],
                "points": [
                    {"date": p["snap_date"], "gain": p["stars"] - base} for p in points
                ],
            }
        )
    return dates, series
