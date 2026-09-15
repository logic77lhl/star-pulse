"""SQLite 存储层。

关于数据源的取舍（重要）：
  SQLite 只在本地存在，**不提交到 git**（二进制、diff 不友好）。
  真正提交进仓库的是 data/snapshots/YYYY-MM-DD.json —— 纯文本、可读、可 diff。
  跑在 CI 里的每一次都是全新环境，所以启动时会自动把 JSON 回灌进 SQLite。
  换句话说：JSON 是事实来源，SQLite 只是一个可以随时重建的查询缓存。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS repo (
  repo_id      INTEGER PRIMARY KEY,
  full_name    TEXT    NOT NULL UNIQUE,
  owner        TEXT    NOT NULL,
  name         TEXT    NOT NULL,
  description  TEXT,
  language     TEXT,
  topics       TEXT,
  homepage     TEXT,
  license      TEXT,
  repo_created TEXT,
  first_seen   TEXT    NOT NULL,
  is_archived  INTEGER DEFAULT 0,
  is_fork      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_repo_created ON repo(repo_created);

CREATE TABLE IF NOT EXISTS snapshot (
  repo_id      INTEGER NOT NULL,
  snap_date    TEXT    NOT NULL,
  stars        INTEGER NOT NULL,
  forks        INTEGER,
  open_issues  INTEGER,
  pushed_at    TEXT,
  source       TEXT    NOT NULL,
  collected_at TEXT    NOT NULL,
  PRIMARY KEY (repo_id, snap_date)
);
CREATE INDEX IF NOT EXISTS idx_snap_date ON snapshot(snap_date);

CREATE TABLE IF NOT EXISTS discovery (
  repo_id    INTEGER NOT NULL,
  found_date TEXT    NOT NULL,
  channel    TEXT    NOT NULL,
  raw_rank   INTEGER,
  PRIMARY KEY (repo_id, found_date, channel)
);

CREATE TABLE IF NOT EXISTS report (
  period_start TEXT NOT NULL,
  period_end   TEXT NOT NULL,
  kind         TEXT NOT NULL,
  markdown     TEXT NOT NULL,
  meta         TEXT,
  created_at   TEXT NOT NULL,
  PRIMARY KEY (period_start, period_end, kind)
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ── 写入 ────────────────────────────────────────────────────────
def upsert_repos(conn: sqlite3.Connection, repos: list[dict], first_seen: str) -> int:
    sql = """
    INSERT INTO repo (repo_id, full_name, owner, name, description, language,
                      topics, homepage, license, repo_created, first_seen,
                      is_archived, is_fork)
    VALUES (:repo_id, :full_name, :owner, :name, :description, :language,
            :topics, :homepage, :license, :repo_created, :first_seen,
            :is_archived, :is_fork)
    ON CONFLICT(repo_id) DO UPDATE SET
      full_name    = excluded.full_name,
      description  = COALESCE(excluded.description, repo.description),
      language     = COALESCE(excluded.language, repo.language),
      topics       = COALESCE(excluded.topics, repo.topics),
      homepage     = COALESCE(excluded.homepage, repo.homepage),
      license      = COALESCE(excluded.license, repo.license),
      repo_created = COALESCE(excluded.repo_created, repo.repo_created),
      is_archived  = excluded.is_archived,
      is_fork      = excluded.is_fork
    """
    payload = []
    for r in repos:
        row = dict(r)
        row["first_seen"] = first_seen
        row.setdefault("topics", None)
        if isinstance(row.get("topics"), (list, tuple)):
            row["topics"] = json.dumps(list(row["topics"]), ensure_ascii=False)
        payload.append(row)
    if not payload:
        return 0
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def upsert_snapshots(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """按 (repo_id, snap_date) 幂等写入。同一天重复跑只会覆盖，不会写重。"""
    sql = """
    INSERT INTO snapshot (repo_id, snap_date, stars, forks, open_issues,
                          pushed_at, source, collected_at)
    VALUES (:repo_id, :snap_date, :stars, :forks, :open_issues,
            :pushed_at, :source, :collected_at)
    ON CONFLICT(repo_id, snap_date) DO UPDATE SET
      stars       = excluded.stars,
      forks       = excluded.forks,
      open_issues = excluded.open_issues,
      pushed_at   = excluded.pushed_at,
      source      = excluded.source,
      collected_at= excluded.collected_at
    """
    if not rows:
        return 0
    conn.executemany(sql, rows)
    conn.commit()
    return len(rows)


def add_discovery(
    conn: sqlite3.Connection, found_date: str, channel: str, entries: list[tuple[int, int | None]]
) -> int:
    """记录「这个仓库是从哪个渠道、哪一天被发现的」，用于溯源。"""
    sql = """
    INSERT INTO discovery (repo_id, found_date, channel, raw_rank)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(repo_id, found_date, channel) DO UPDATE SET raw_rank = excluded.raw_rank
    """
    if not entries:
        return 0
    conn.executemany(sql, [(rid, found_date, channel, rank) for rid, rank in entries])
    conn.commit()
    return len(entries)


def save_report(
    conn: sqlite3.Connection, start: str, end: str, kind: str, markdown: str, meta: dict, created_at: str
) -> None:
    conn.execute(
        """
        INSERT INTO report (period_start, period_end, kind, markdown, meta, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(period_start, period_end, kind) DO UPDATE SET
          markdown = excluded.markdown,
          meta     = excluded.meta,
          created_at = excluded.created_at
        """,
        (start, end, kind, markdown, json.dumps(meta, ensure_ascii=False), created_at),
    )
    conn.commit()


# ── 读取 ────────────────────────────────────────────────────────
def candidate_repos(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """候选池：最近被发现过的仓库优先，其次是更近创建的、星数更高的。

    目标是把宝贵的请求预算花在最可能上榜的仓库上。
    """
    return conn.execute(
        """
        SELECT r.repo_id, r.full_name,
               COALESCE((SELECT d.found_date FROM discovery d
                         WHERE d.repo_id = r.repo_id
                         ORDER BY d.found_date DESC LIMIT 1), r.first_seen) AS last_found,
               COALESCE((SELECT s.stars FROM snapshot s
                         WHERE s.repo_id = r.repo_id
                         ORDER BY s.snap_date DESC LIMIT 1), 0) AS last_stars
        FROM repo r
        WHERE r.is_fork = 0 AND r.is_archived = 0
        ORDER BY last_found DESC, last_stars DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def pool_refresh_repos(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """池内续期候选：**已追踪最久**的仓库优先（按快照数降序）。

    与 `candidate_repos` 的唯一区别就是排序。为什么要另开一个函数：

    搜索渠道只覆盖「最近 N 天创建」的仓库。一个项目滑出这个窗口后，
    就再没有任何渠道能发现它 —— 当天拿不到快照，星数增量随之永久中断。
    所以续期必须按「历史最长」优先，而不是按「最近发现」优先：
    后者排在前面的恰恰是刚发现的仓库，它们在搜索窗口内，本来就会被找到，
    占着保底名额却不解决任何问题（这一版是踩过才改的）。
    """
    return conn.execute(
        """
        SELECT r.repo_id, r.full_name,
               (SELECT COUNT(*) FROM snapshot s WHERE s.repo_id = r.repo_id) AS snaps,
               COALESCE((SELECT s.stars FROM snapshot s
                         WHERE s.repo_id = r.repo_id
                         ORDER BY s.snap_date DESC LIMIT 1), 0) AS last_stars
        FROM repo r
        WHERE r.is_fork = 0 AND r.is_archived = 0
        ORDER BY snaps DESC, last_stars DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def snapshot_dates(conn: sqlite3.Connection, limit: int = 30) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT snap_date FROM snapshot ORDER BY snap_date DESC LIMIT ?", (limit,)
    ).fetchall()
    return [r["snap_date"] for r in rows]


def latest_snapshot_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(snap_date) AS d FROM snapshot").fetchone()
    return row["d"] if row and row["d"] else None


def repo_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM repo").fetchone()["c"]


# ── JSON 快照（git 里的事实来源）────────────────────────────────
def _pick(row: dict, *keys, default=None):
    """仓库对象在「采集路径」上用 _stars/_forks 这类带下划线的键，
    在「JSON 回灌路径」上用 stars/forks。这里统一取。"""
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return default


def export_snapshot_json(snapshots_dir: Path, snap_date: str, rows: list[dict]) -> Path:
    """把当天快照写成文本文件，方便 git 追踪与人工查阅。"""
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    target = snapshots_dir / f"{snap_date}.json"
    payload = {
        "date": snap_date,
        "count": len(rows),
        "repos": [
            {
                "full_name": r["full_name"],
                "repo_id": r["repo_id"],
                "stars": int(_pick(r, "stars", "_stars", default=0) or 0),
                "forks": _pick(r, "forks", "_forks"),
                "open_issues": _pick(r, "open_issues", "_open_issues"),
                "language": r.get("language"),
                "description": (r.get("description") or "")[:200],
                "repo_created": r.get("repo_created"),
                "is_archived": int(r.get("is_archived") or 0),
                "is_fork": int(r.get("is_fork") or 0),
            }
            for r in sorted(
                rows, key=lambda x: -int(_pick(x, "stars", "_stars", default=0) or 0)
            )
        ],
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return target


def import_snapshot_jsons(conn: sqlite3.Connection, snapshots_dir: Path) -> tuple[int, int]:
    """把 data/snapshots/*.json 回灌进 SQLite。CI 每次都是空环境，靠这个补历史。"""
    if not snapshots_dir.is_dir():
        return (0, 0)
    repo_rows: list[dict] = []
    snap_rows: list[dict] = []
    for path in sorted(snapshots_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        snap_date = data.get("date") or path.stem
        for r in data.get("repos", []):
            full_name = r.get("full_name")
            repo_id = r.get("repo_id")
            if not full_name or not repo_id:
                continue
            owner, _, name = full_name.partition("/")
            repo_rows.append(
                {
                    "repo_id": repo_id,
                    "full_name": full_name,
                    "owner": owner,
                    "name": name,
                    "description": r.get("description"),
                    "language": r.get("language"),
                    "topics": None,
                    "homepage": None,
                    "license": None,
                    "repo_created": r.get("repo_created"),
                    "is_archived": int(r.get("is_archived") or 0),
                    "is_fork": int(r.get("is_fork") or 0),
                }
            )
            snap_rows.append(
                {
                    "repo_id": repo_id,
                    "snap_date": snap_date,
                    "stars": int(r.get("stars") or 0),
                    "forks": r.get("forks"),
                    "open_issues": r.get("open_issues"),
                    "pushed_at": None,
                    "source": "json_import",
                    "collected_at": f"{snap_date}T00:00:00Z",
                }
            )
    if repo_rows:
        upsert_repos(conn, repo_rows, snap_rows[0]["snap_date"])
    if snap_rows:
        upsert_snapshots(conn, snap_rows)
    return (len(repo_rows), len(snap_rows))
