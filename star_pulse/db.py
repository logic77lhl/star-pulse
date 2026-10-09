"""SQLite 存储层。

关于数据源的取舍（重要）：
  SQLite 只在本地存在，**不提交到 git**（二进制、diff 不友好）。
  真正提交进仓库的是 data/snapshots/YYYY-MM-DD.json —— 纯文本、可读、可 diff。
  跑在 CI 里的每一次都是全新环境，所以启动时会自动把 JSON 回灌进 SQLite。
  换句话说：JSON 是事实来源，SQLite 只是一个可以随时重建的查询缓存。

关于 `repo.full_name` 为什么**不**加 UNIQUE（schema v2）：
  repo_id 才是仓库的唯一身份。full_name 只是「当前显示名」，它会因为改名而变化，
  也会因为「删除后重建」被另一个 repo_id 复用 —— 此时库里会合法地同时存在两行同名。
  早期版本给 full_name 加了 UNIQUE，导致删除重建后写入必然违反约束：
  `ON CONFLICT(repo_id)` 在语义上无法处理 full_name 冲突，插入直接抛 IntegrityError。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
INT64_MAX = 2 ** 63 - 1

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS repo (
  repo_id      INTEGER PRIMARY KEY,
  full_name    TEXT    NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_repo_name    ON repo(full_name);

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

# v1 -> v2：把 repo.full_name 上的列级 UNIQUE 去掉。
# SQLite 无法 ALTER 掉列级 UNIQUE（它由不可 drop 的隐式索引 sqlite_autoindex_repo_1 实现），
# 所以只能原地重建表。INSERT ... SELECT 全量保留历史行，不丢数据。
_MIGRATE_REPO_V2 = (
    "ALTER TABLE repo RENAME TO repo_legacy_v1",
    # 索引跟着表改名走，先把名字腾出来，否则新表的 CREATE INDEX IF NOT EXISTS 会被跳过
    "DROP INDEX IF EXISTS idx_repo_created",
    """
    CREATE TABLE repo (
      repo_id      INTEGER PRIMARY KEY,
      full_name    TEXT    NOT NULL,
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
    )
    """,
    """
    INSERT INTO repo (repo_id, full_name, owner, name, description, language,
                      topics, homepage, license, repo_created, first_seen,
                      is_archived, is_fork)
    SELECT repo_id, full_name, owner, name, description, language,
           topics, homepage, license, repo_created, first_seen,
           COALESCE(is_archived, 0), COALESCE(is_fork, 0)
    FROM repo_legacy_v1
    """,
    "DROP TABLE repo_legacy_v1",
)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _repo_is_legacy(conn: sqlite3.Connection) -> bool:
    """判断 repo 表是否是 v1（full_name 带列级 UNIQUE）。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'repo'"
    ).fetchone()
    if row is None:
        return False  # 全新库，没有旧表
    for idx in conn.execute("PRAGMA index_list(repo)").fetchall():
        # 列级 UNIQUE 的隐式索引：unique=1 且 origin='u'
        if idx[2] and idx[3] == "u":
            return True
    ddl_row = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'repo'").fetchone()
    ddl = (ddl_row[0] if ddl_row else "") or ""
    return "unique" in ddl.lower()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode = WAL")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION and _repo_is_legacy(conn):
        log.warning("检测到 v1 版 repo 表（full_name 带 UNIQUE），正在原地重建以移除该约束…")
        try:
            # 注意：不能用 executescript —— 它会先隐式 COMMIT，DDL 将无法回滚
            with conn:
                for stmt in _MIGRATE_REPO_V2:
                    conn.execute(stmt)
        except sqlite3.Error:
            log.exception(
                "repo 表原地重建失败，数据库仍是旧结构。可运行 "
                "`python -m star_pulse rebuild` 从 data/snapshots/*.json 重建（安全，SQLite 只是派生缓存）"
            )
            raise
        log.info("repo 表已升级到 v%d，历史行已全部保留", SCHEMA_VERSION)
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def _atomic_write_text(target: Path, text: str) -> None:
    """同目录临时文件 + os.replace，避免中断时留下截断文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)


# ── 写入 ────────────────────────────────────────────────────────
_REPO_UPSERT_SQL = """
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

_SNAP_UPSERT_SQL = """
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

_DISCOVERY_UPSERT_SQL = """
INSERT INTO discovery (repo_id, found_date, channel, raw_rank)
VALUES (?, ?, ?, ?)
ON CONFLICT(repo_id, found_date, channel) DO UPDATE SET raw_rank = excluded.raw_rank
"""


def _prepare_repo_payload(repos: list[dict], first_seen: str) -> list[dict]:
    """按 repo_id 做批次内 last-wins 去重（同名不同 id 一律保留，它们是不同仓库）。"""
    by_id: dict = {}
    for r in repos:
        by_id[r["repo_id"]] = r

    payload = []
    for r in by_id.values():
        row = dict(r)
        row["first_seen"] = first_seen
        row.setdefault("topics", None)
        if isinstance(row.get("topics"), (list, tuple)):
            row["topics"] = json.dumps(list(row["topics"]), ensure_ascii=False)
        payload.append(row)
    return payload


def upsert_repos(conn: sqlite3.Connection, repos: list[dict], first_seen: str) -> int:
    payload = _prepare_repo_payload(repos, first_seen)
    if not payload:
        return 0
    with conn:
        conn.executemany(_REPO_UPSERT_SQL, payload)
    return len(payload)


def upsert_snapshots(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """按 (repo_id, snap_date) 幂等写入。同一天重复跑只会覆盖，不会写重。"""
    if not rows:
        return 0
    with conn:
        conn.executemany(_SNAP_UPSERT_SQL, rows)
    return len(rows)


def add_discoveries(conn: sqlite3.Connection, rows: list[tuple[int, str, str, int | None]]) -> int:
    """批量记录发现来源。rows: (repo_id, found_date, channel, raw_rank)。"""
    if not rows:
        return 0
    with conn:
        conn.executemany(_DISCOVERY_UPSERT_SQL, rows)
    return len(rows)


def add_discovery(
    conn: sqlite3.Connection, found_date: str, channel: str, entries: list[tuple[int, int | None]]
) -> int:
    """记录「这个仓库是从哪个渠道、哪一天被发现的」，用于溯源。"""
    return add_discoveries(conn, [(rid, found_date, channel, rank) for rid, rank in entries])


def save_daily_batch(
    conn: sqlite3.Connection,
    repo_rows: list[dict],
    snap_rows: list[dict],
    discovery_rows: list[tuple[int, str, str, int | None]],
    first_seen: str,
) -> tuple[int, int, int]:
    """一天的采集结果在**单个事务**里落库：要么全有，要么全无。

    之前三次 upsert 各自提交，后两步失败会留下「repo 已写入、快照缺失」的半批状态。
    """
    repo_payload = _prepare_repo_payload(repo_rows, first_seen)
    with conn:
        if repo_payload:
            conn.executemany(_REPO_UPSERT_SQL, repo_payload)
        if snap_rows:
            conn.executemany(_SNAP_UPSERT_SQL, snap_rows)
        if discovery_rows:
            conn.executemany(_DISCOVERY_UPSERT_SQL, discovery_rows)
    return (len(repo_payload), len(snap_rows or []), len(discovery_rows or []))


def save_report(
    conn: sqlite3.Connection, start: str, end: str, kind: str, markdown: str, meta: dict, created_at: str
) -> None:
    with conn:
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


def orphan_snapshot_count(conn: sqlite3.Connection) -> int:
    """没有对应 repo 行的快照数。

    正常应为 0。非 0 说明写入路径出了问题（历史遗留的孤儿）。只做告警，不自动删除
    —— 孤儿快照也是历史，删掉就永久丢数据。
    """
    return conn.execute(
        "SELECT COUNT(*) AS c FROM snapshot s "
        "WHERE NOT EXISTS (SELECT 1 FROM repo r WHERE r.repo_id = s.repo_id)"
    ).fetchone()["c"]


# ── JSON 快照（git 里的事实来源）────────────────────────────────
def _pick(row: dict, *keys, default=None):
    """仓库对象在「采集路径」上用 _stars/_forks 这类带下划线的键，
    在「JSON 回灌路径」上用 stars/forks。这里统一取。"""
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return default


def _snapshot_record(r: dict) -> dict | None:
    """把仓库行归一成快照 JSON 里的一条记录。

    「采集路径」用 _stars/_forks 这类带下划线的键，「回灌路径」用 stars/forks，
    这里统一取。缺 repo_id/full_name 的行无法定位，返回 None 由调用方跳过。
    """
    rid = _safe_int(r.get("repo_id"), 1, INT64_MAX)
    full_name = r.get("full_name")
    if rid is None or not isinstance(full_name, str) or not full_name.strip():
        return None
    return {
        "full_name": full_name,
        "repo_id": rid,
        "stars": int(_pick(r, "stars", "_stars", default=0) or 0),
        "forks": _pick(r, "forks", "_forks"),
        "open_issues": _pick(r, "open_issues", "_open_issues"),
        "language": r.get("language"),
        "description": (r.get("description") or "")[:200],
        "repo_created": r.get("repo_created"),
        "is_archived": int(r.get("is_archived") or 0),
        "is_fork": int(r.get("is_fork") or 0),
    }


def read_snapshot_repos(path: Path) -> dict[int, dict]:
    """读取已有快照，返回 {repo_id: 记录}。坏文件/坏行一律跳过，绝不抛异常。

    用于「合并写」时取回当天已落盘的仓库。读不出来就当作空文件 —— 宁可少合并
    一轮，也不能因为一个坏文件让整次采集失败。
    """
    if not path.is_file():
        return {}  # 当天首次写入，属正常路径，不该报错
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("已有快照无法解析，将按新建处理：%s", path.name)
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("repos"), list):
        log.warning("已有快照结构异常，将按新建处理：%s", path.name)
        return {}
    out: dict[int, dict] = {}
    for r in data["repos"]:
        if not isinstance(r, dict):
            continue
        rid = _safe_int(r.get("repo_id"), 1, INT64_MAX)
        if rid is None or not isinstance(r.get("full_name"), str):
            continue
        out[rid] = r
    return out


def export_snapshot_json(
    snapshots_dir: Path, snap_date: str, rows: list[dict], *, merge: bool = True
) -> Path | None:
    """把当天快照写成文本文件，方便 git 追踪与人工查阅。无内容可写时返回 None。

    **合并写，不是覆盖写。** 同一天可能被写入多次：正常采集、失败后补跑、
    `snapshot --repos` 验证、`run-daily --limit 30` 试跑。整文件覆盖会让后一次
    把先前的仓库整批抹掉 —— 而 JSON 是唯一事实来源且会 commit，下一轮 CI 回灌
    时那批仓库当天就永久缺一格。合并按 repo_id（仓库身份），本次数据优先。

    **本轮没有新数据就完全不碰文件**，并返回 None。这样有两重好处：
    一是不会因为一次空跑把当天早先的成功快照重写一遍（徒增 git 变更）；
    二是绝不凭空造出语法合法的空快照 —— 发布门只看文件是否存在，空文件会被
    误判成「今日已产出」，进而用空数据覆盖掉当天已有的好数据。
    """
    fresh = [rec for rec in (_snapshot_record(r) for r in rows) if rec is not None]
    if not fresh:
        return None

    target = snapshots_dir / f"{snap_date}.json"
    merged: dict[int, dict] = read_snapshot_repos(target) if merge else {}
    for rec in fresh:
        merged[rec["repo_id"]] = rec
    payload = {
        "date": snap_date,
        "count": len(merged),
        "repos": sorted(merged.values(), key=lambda x: -int(x.get("stars") or 0)),
    }
    _atomic_write_text(target, json.dumps(payload, ensure_ascii=False, indent=1))
    return target


class ImportResult(NamedTuple):
    repos: int
    snaps: int
    skipped: int
    bad_files: int
    reasons: dict


def _safe_int(value, lo: int, hi: int) -> int | None:
    """把任意 JSON 值转成落在 [lo, hi] 的 int；不可用返回 None。绝不抛异常。

    回灌的是「别人写下来的文件」，一个坏值不应该让所有命令都挂掉。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if lo <= n <= hi else None


def import_snapshot_jsons(conn: sqlite3.Connection, snapshots_dir: Path) -> ImportResult:
    """把 data/snapshots/*.json 回灌进 SQLite。CI 每次都是空环境，靠这个补历史。

    逐文件、逐行容错：坏文件/坏行跳过并计数，绝不因为一个畸形字段让所有命令失败。
    """
    if not snapshots_dir.is_dir():
        return ImportResult(0, 0, 0, 0, {})

    repo_rows: list[dict] = []
    snap_rows: list[dict] = []
    skipped = 0
    bad_files = 0
    reasons: dict = {}
    first_date: str | None = None

    def bump(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    for path in sorted(snapshots_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            bad_files += 1
            log.warning("跳过无法解析的快照文件：%s", path.name)
            continue
        if not isinstance(data, dict):
            bad_files += 1
            log.warning("跳过顶层非对象的快照文件：%s", path.name)
            continue
        entries = data.get("repos")
        if not isinstance(entries, list):
            bad_files += 1
            log.warning("跳过 repos 非数组的快照文件：%s", path.name)
            continue
        snap_date = data.get("date") or path.stem
        if first_date is None and isinstance(snap_date, str):
            first_date = snap_date

        for r in entries:
            if not isinstance(r, dict):
                skipped += 1
                bump("行非对象")
                continue
            full_name = r.get("full_name")
            if not isinstance(full_name, str) or not full_name.strip():
                skipped += 1
                bump("full_name 缺失")
                continue
            repo_id = _safe_int(r.get("repo_id"), 1, INT64_MAX)
            if repo_id is None:
                skipped += 1
                bump("repo_id 非法")
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
                    "is_archived": _safe_int(r.get("is_archived"), 0, 1) or 0,
                    "is_fork": _safe_int(r.get("is_fork"), 0, 1) or 0,
                }
            )

            # 非法 stars 只丢这一条快照、保留 repo 行：把坏值兜成 0 会伪造一次暴跌增量
            # 去污染榜单；丢一天快照只是少一个点，增量查询会自动退到相邻可用日期。
            stars = _safe_int(r.get("stars"), 0, INT64_MAX)
            if stars is None:
                skipped += 1
                bump("stars 非法")
                continue
            snap_rows.append(
                {
                    "repo_id": repo_id,
                    "snap_date": snap_date,
                    "stars": stars,
                    "forks": _safe_int(r.get("forks"), 0, INT64_MAX),
                    "open_issues": _safe_int(r.get("open_issues"), 0, INT64_MAX),
                    "pushed_at": None,
                    "source": "json_import",
                    "collected_at": f"{snap_date}T00:00:00Z",
                }
            )

    n_repos = 0
    if repo_rows:
        n_repos = upsert_repos(conn, repo_rows, first_date or "")
    if snap_rows:
        upsert_snapshots(conn, snap_rows)
    if skipped or bad_files:
        log.warning(
            "回灌跳过 %d 行 / %d 个文件：%s",
            skipped, bad_files,
            "、".join(f"{k}×{v}" for k, v in sorted(reasons.items())) or "—",
        )
    return ImportResult(n_repos, len(snap_rows), skipped, bad_files, reasons)