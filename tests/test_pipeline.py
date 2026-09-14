"""端到端回归测试（零依赖，直接 python tests/test_pipeline.py 运行）。

用**已公开发布的 2026-W37 周榜数据**做基准：那期榜单已被三源互证
（知乎《本周 GitHub 星耀榜》、SegmentFault 9/13 周榜、GitStarClub W37、
whatstrending.ai 日快照），所以可以直接拿来当断言依据。

覆盖三件事：
  1. 增量计算能否逐一还原已发布的排名与数字
  2. 快照断档时，跨度超限的仓库是否被正确剔除（而不是混进榜里）
  3. 分类规则是否把项目归到了合理的类别

所有产物都写在临时目录，不污染项目。
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from star_pulse import analyze, db, render  # noqa: E402
from star_pulse.config import load_settings  # noqa: E402

# (repo, 期末总星, 本周新增, 语言, 创建日期, 描述, 期望分类)
PUBLISHED_W37 = [
    ("ayghri/i-have-adhd", 43461, 15924, "Python", "2026-08-20",
     "Output style rules that make a coding agent lead with the next action "
     "instead of burying it in filler.", "输出规范"),
    ("bilawalsidhu/gods-eye-view", 29949, 10510, "JavaScript", "2026-07-02",
     "A spy satellite simulator in your browser, except the data is real.", "可视化"),
    ("tt-a1i/archify", 59677, 10442, "JavaScript", "2026-05-11",
     "Agent skill that compiles a codebase into verifiable architecture diagrams.", "Agent Skill"),
    ("DietrichGebert/ponytail", 136630, 9272, "JavaScript", "2026-04-03",
     "Makes your AI agent think like the laziest senior dev in the room.", "Agent Skill"),
    ("mattpocock/skills", 260499, 8960, "Shell", "2026-02-18",
     "A public collection of agent skills for coding workflows.", "Agent Skill"),
    ("affaan-m/ECC", 257128, 8086, "JavaScript", "2026-03-27",
     "Agent harness performance optimization: skills, instincts, memory.", "Agent Skill"),
    ("cathrynlavery/diagram-design", 38872, 7409, "HTML", "2026-06-09",
     "39 editorial diagram types for coding agents. Self-contained HTML and SVG.", "可视化"),
    ("heygen-com/hyperframes", 49224, 5124, "TypeScript", "2026-06-21",
     "Video generation framework built for agents.", "Agent Skill"),
    ("microsoft/markitdown", 183244, 4823, "Python", "2024-11-13",
     "Python tool for converting files and office documents to Markdown.", "开发工具"),
    ("THU-MAIC/OpenMAIC", 36200, 4417, "TypeScript", "2026-05-30",
     "Open multi-agent interactive classroom for immersive learning.", "Agent Skill"),
]

START, END = "2026-09-06", "2026-09-13"


def _seed(root: Path, extra_old_snapshot: bool = False):
    settings = load_settings(root)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)

    for day, value_of in ((START, lambda s, d: s - d), (END, lambda s, d: s)):
        repos, snaps = [], []
        for i, (full, stars, delta, lang, created, desc, _cat) in enumerate(PUBLISHED_W37):
            rid = 900000 + i
            owner, _, name = full.partition("/")
            value = value_of(stars, delta)
            repos.append({
                "repo_id": rid, "full_name": full, "owner": owner, "name": name,
                "description": desc, "language": lang, "topics": [],
                "homepage": None, "license": "MIT", "repo_created": created,
                "is_archived": 0, "is_fork": 0,
            })
            snaps.append({
                "repo_id": rid, "snap_date": day, "stars": value,
                "forks": round(value / 45), "open_issues": 3,
                "pushed_at": None, "source": "test",
                "collected_at": f"{day}T00:00:00Z",
            })
        db.upsert_repos(conn, repos, day)
        db.upsert_snapshots(conn, snaps)
        db.export_snapshot_json(settings.snapshots_dir, day, [
            {**r, "_stars": s["stars"], "_forks": s["forks"],
             "_open_issues": s["open_issues"], "_pushed_at": None}
            for r, s in zip(repos, snaps)
        ])

    if extra_old_snapshot:
        # 模拟断档：只有 2026-08-25 这一个「早于周期起点」的基线，跨度 19 天
        old = (date.fromisoformat(END) - timedelta(days=19)).isoformat()
        snaps, repos = [], []
        for i, (full, stars, delta, lang, created, desc, _cat) in enumerate(PUBLISHED_W37):
            rid = 900000 + i
            owner, _, name = full.partition("/")
            value = int((stars - delta) * 0.97)
            repos.append({
                "repo_id": rid, "full_name": full, "owner": owner, "name": name,
                "description": desc, "language": lang, "topics": [],
                "homepage": None, "license": "MIT", "repo_created": created,
                "is_archived": 0, "is_fork": 0,
            })
            snaps.append({
                "repo_id": rid, "snap_date": old, "stars": value,
                "forks": round(value / 45), "open_issues": 3,
                "pushed_at": None, "source": "test",
                "collected_at": f"{old}T00:00:00Z",
            })
        db.upsert_repos(conn, repos, old)
        db.upsert_snapshots(conn, snaps)
    return settings, conn


def test_ranking_matches_published(tmp: Path) -> None:
    settings, conn = _seed(tmp / "rank")
    md, html, meta = render.build_report(settings, conn, START, END)

    expected = [f for f, _s, _d, _l, _c, _de, _cat
                in sorted(PUBLISHED_W37, key=lambda x: -x[2])]
    got = [i["full_name"] for i in meta["top"]]
    assert got == expected, f"排名不符\n期望 {expected}\n实际 {got}"

    for item, (full, stars, delta, *_rest) in zip(
        meta["top"], sorted(PUBLISHED_W37, key=lambda x: -x[2])
    ):
        assert item["full_name"] == full
        assert item["delta"] == delta, f"{full} 增量 {item['delta']} != {delta}"
        assert item["stars_after"] == stars, f"{full} 总星 {item['stars_after']} != {stars}"

    assert meta["warm"] is True, "两个快照夹住周期，应当产出增量榜"
    assert "2026-W37" == meta["week"], f"周期标签错误：{meta['week']}"
    assert "+15,924" in md, "Markdown 未输出预期增量"
    assert "gainChart" in html, "HTML 未包含图表"

    # 分类规则
    for item, (*_head, expected_cat) in zip(
        meta["top"], sorted(PUBLISHED_W37, key=lambda x: -x[2])
    ):
        assert item["category"] == expected_cat, (
            f"{item['full_name']} 分类为 {item['category']}，期望 {expected_cat}"
        )
    print(f"  [PASS] 排名与增量逐一还原已发布榜单（{len(got)} 项），分类正确")


def test_span_drop(tmp: Path) -> None:
    settings, conn = _seed(tmp / "span", extra_old_snapshot=True)
    # 周期起点 08-30 之前只有 08-25 这一个基线，跨度 19 天 > 上限 10 天
    md, _html, meta = render.build_report(settings, conn, "2026-08-30", END)
    assert meta["warm"] is False, "跨度全部超限时不应产出增量榜"
    assert meta["dropped_count"] == len(PUBLISHED_W37), (
        f"应剔除 {len(PUBLISHED_W37)} 个，实际 {meta['dropped_count']}"
    )
    assert all(d["span_days"] == 19 for d in meta["dropped"]), "跨度天数计算有误"
    assert "因跨度超限被剔除" in md, "报告未说明剔除原因"
    print(f"  [PASS] 跨度 19 天的 {meta['dropped_count']} 个仓库被正确剔除并说明原因")


def test_idempotent_snapshot(tmp: Path) -> None:
    settings, conn = _seed(tmp / "idem")
    before = conn.execute("SELECT COUNT(*) c FROM snapshot").fetchone()["c"]
    # 同一天重复写入应当覆盖而不是新增
    db.upsert_snapshots(conn, [{
        "repo_id": 900000, "snap_date": START, "stars": 1, "forks": 0,
        "open_issues": 0, "pushed_at": None, "source": "dup",
        "collected_at": f"{START}T09:00:00Z",
    }])
    after = conn.execute("SELECT COUNT(*) c FROM snapshot").fetchone()["c"]
    assert before == after, f"重复写入产生了新行：{before} -> {after}"
    stars = conn.execute(
        "SELECT stars FROM snapshot WHERE repo_id = 900000 AND snap_date = ?", (START,)
    ).fetchone()["stars"]
    assert stars == 1, "重复写入未覆盖旧值"
    print("  [PASS] 同日重复写入幂等（覆盖而非新增）")


def test_json_roundtrip(tmp: Path) -> None:
    settings, conn = _seed(tmp / "round")
    fresh = db.connect(tmp / "round" / "data" / "rebuilt.db")
    db.init_schema(fresh)
    repos, snaps = db.import_snapshot_jsons(fresh, settings.snapshots_dir)
    assert snaps == len(PUBLISHED_W37) * 2, f"回灌条数异常：{snaps}"
    md, _h, meta = render.build_report(settings, fresh, START, END)
    assert [i["full_name"] for i in meta["top"]] == [
        f for f, *_ in sorted(PUBLISHED_W37, key=lambda x: -x[2])
    ], "从 JSON 重建后排名不一致"
    print(f"  [PASS] JSON 快照可完整重建 SQLite（{snaps} 条记录，排名一致）")


def test_site_build(tmp: Path) -> None:
    """站点看板：核心板块必须齐全，且不能残留未替换的占位符。"""
    from star_pulse import site as site_builder

    settings, conn = _seed(tmp / "site")
    stats = site_builder.build_site(settings, conn)
    docs = Path(stats["docs"])
    html = (docs / "index.html").read_text(encoding="utf-8")

    for section in ("每天的情况", "当日涨幅榜", "累计增长榜", "每日新增走势", "领涨仓库走势"):
        assert section in html, f"看板缺少板块：{section}"
    assert html.count("<canvas") == 2, "应有 2 个图表"
    assert "const D = {" in html, "图表数据未内联（那样本地打开就看不到图）"
    assert "__" not in html, "存在未替换的占位符"
    assert (docs / "data.json").is_file(), "缺少 data.json"
    assert (docs / ".nojekyll").is_file(), "缺少 .nojekyll（分支部署 Pages 需要）"

    # 版面：项目名单排在最前，趋势与榜单默认收起
    assert html.index("项目名单") < html.index('<details class="trends">'), (
        "项目名单必须排在趋势与榜单之前"
    )
    assert '<details class="trends"' in html, "趋势区应当是折叠块"
    assert "<details class=\"trends\" open" not in html, "趋势区必须默认收起"
    for tid in ('id="projTable"', 'id="projQ"', 'id="projLang"', 'id="projSort"'):
        assert tid in html, f"项目名单缺少工具栏元素：{tid}"

    # 每条记录独占一行 —— 整张表挤成一行会让 git 完全没法做增量压缩
    assert html.count("<tr data-name=") == sum(
        1 for ln in html.splitlines() if ln.startswith("<tr data-name=")
    ), "项目名单必须一行一条记录"
    assert "<tr data-name=" in html
    # 简介只放一份：重复存 data 属性会把页面从 ~400 KB 撑到 500 KB
    assert "data-desc=" not in html, "简介不应重复存一份 data 属性"

    # 每日汇总必须能逐日算出来，且首日标记为基线
    summary = analyze.daily_summary(conn)
    assert len(summary) == 2, f"应有 2 天，实际 {len(summary)}"
    assert summary[0]["is_first"] is True and summary[0]["total_gain"] == 0
    assert summary[1]["total_gain"] == sum(d for _f, _s, d, *_ in PUBLISHED_W37), (
        "第二天的全网新增合计应等于本周新增之和"
    )
    assert summary[1]["top_name"] == "ayghri/i-have-adhd", "当日冠军应为 i-have-adhd"
    print(f"  [PASS] 看板生成正确（{len(summary)} 天，当日冠军 {summary[1]['top_name']}）")


def main() -> int:
    print("star-pulse 回归测试")
    print("=" * 58)
    # Windows 上 SQLite 连接不关闭会导致临时目录删不掉，故显式忽略清理错误
    with tempfile.TemporaryDirectory(prefix="star-pulse-test-", ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        test_ranking_matches_published(tmp)
        test_span_drop(tmp)
        test_idempotent_snapshot(tmp)
        test_json_roundtrip(tmp)
        test_site_build(tmp)
    print("=" * 58)
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
