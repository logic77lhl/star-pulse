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

import json
import os
import re
import sqlite3
import sys
import tempfile
from dataclasses import replace
from datetime import date, timedelta
from html import escape
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
    # 图表是服务端渲染的 CSS 条形榜：没有 canvas，也不得引入任何外部脚本
    assert 'class="hbars"' in html, "HTML 未包含增长分布图"
    assert "<canvas" not in html and "cdnjs" not in html, "周报不该再用 canvas / CDN"

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
    md, html, meta = render.build_report(settings, conn, "2026-08-30", END)
    assert meta["warm"] is False, "跨度全部超限时不应产出增量榜"
    assert meta["dropped_count"] == len(PUBLISHED_W37), (
        f"应剔除 {len(PUBLISHED_W37)} 个，实际 {meta['dropped_count']}"
    )
    assert all(d["span_days"] == 19 for d in meta["dropped"]), "跨度天数计算有误"
    assert "因跨度超限被剔除" in md, "报告未说明剔除原因"
    # 两条渲染路径的数据质量小节必须同源：被剔除仓库的名字与跨度在两边都要有。
    # 此前 Markdown 列了名单、HTML 只给一个数量，同一份报告换个格式能查到的信息不一样。
    dropped_name = meta["dropped"][0]["full_name"]
    assert dropped_name in md and dropped_name in html, (
        "被剔除仓库的名单应同时出现在 Markdown 与 HTML"
    )
    assert "跨度 19 天" in md and "跨度 19 天" in html, "跨度明细应两边都有"
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
    res = db.import_snapshot_jsons(fresh, settings.snapshots_dir)
    snaps = res.snaps
    assert snaps == len(PUBLISHED_W37) * 2, f"回灌条数异常：{snaps}"
    assert res.skipped == 0 and res.bad_files == 0, f"正常文件不该被跳过：{res}"
    md, _h, meta = render.build_report(settings, fresh, START, END)
    assert [i["full_name"] for i in meta["top"]] == [
        f for f, *_ in sorted(PUBLISHED_W37, key=lambda x: -x[2])
    ], "从 JSON 重建后排名不一致"
    print(f"  [PASS] JSON 快照可完整重建 SQLite（{snaps} 条记录，排名一致）")


def test_site_build(tmp: Path) -> None:
    """站点看板：板块齐全、首屏直接给到价值、图表不依赖任何外部资源。"""
    from star_pulse import site as site_builder

    settings, conn = _seed(tmp / "site")
    stats = site_builder.build_site(settings, conn)
    docs = Path(stats["docs"])
    index = (docs / "index.html").read_text(encoding="utf-8")
    projects = (docs / "projects.html").read_text(encoding="utf-8")

    for section in ("当日涨幅榜", "累计增长榜", "每日新增走势", "领涨仓库走势", "每天的情况", "历史周报"):
        assert section in index, f"看板缺少板块：{section}"

    # 首屏必须直接给到价值：指标条与两个榜单都在，不能全藏在折叠块后面
    for kpi_label in ("今日新增星数", "追踪项目", "累计追踪"):
        assert kpi_label in index, f"缺少指标卡：{kpi_label}"
    assert 'class="kpis"' in index, "缺少指标条"
    assert "<details" not in index, "看板不该再靠折叠块藏内容（首屏必须直接可见）"

    # 图表改成服务端渲染的 SVG / CSS：没有 canvas，也没有任何外部脚本
    assert "<canvas" not in index, "图表不该再用 canvas"
    assert "<script" not in index, "看板必须是零 JS 的静态页"
    assert "<svg" in index, "折线图应为服务端渲染的 SVG"

    # 硬性不变量：整站不得引用任何外部资源（含 CDN）。
    # 这正是本项目「零第三方依赖」的立身之本，此前 Chart.js 走 cdnjs 是唯一破例。
    for name, html in (("index.html", index), ("projects.html", projects)):
        externals = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
        bad = [u for u in externals if not u.startswith("https://github.com/")]
        assert not bad, f"{name} 引用了外部资源：{bad[:3]}"

    assert "__" not in index, "存在未替换的占位符"
    assert (docs / "data.json").is_file(), "缺少 data.json"
    assert (docs / ".nojekyll").is_file(), "缺少 .nojekyll（分支部署 Pages 需要）"
    assert stats["projects"].endswith("projects.html"), "返回值应包含项目名单页"

    # 项目名单已拆到独立页：看板只留链接，不内联 800 行
    assert 'href="projects.html"' in index, "看板应链到项目名单页"
    assert 'id="projTable"' not in index, "项目名单不该再内联在首屏"

    # 累计增长榜必须用「两端都有快照」的对齐窗口，而不是被跨度过滤误杀的榜单
    summary = analyze.daily_summary(conn)
    assert len(summary) == 2, f"应有 2 天，实际 {len(summary)}"
    assert summary[0]["is_first"] is True and summary[0]["total_gain"] == 0
    assert summary[1]["total_gain"] == sum(d for _f, _s, d, *_ in PUBLISHED_W37), (
        "第二天的全网新增合计应等于本周新增之和"
    )
    assert summary[1]["top_name"] == "ayghri/i-have-adhd", "当日冠军应为 i-have-adhd"

    ranked, comparable = analyze.range_gain(conn, START, END)
    assert comparable == len(PUBLISHED_W37), f"两端都在场的仓库应为全部，实际 {comparable}"
    best = max(PUBLISHED_W37, key=lambda x: x[2])[0]
    assert ranked[0]["full_name"] == best, f"累计榜榜首应为 {best}，实际 {ranked[0]['full_name']}"
    assert ranked[0]["full_name"] in index, "累计榜榜首应出现在页面上"
    print(f"  [PASS] 看板生成正确（{len(summary)} 天，当日冠军 {summary[1]['top_name']}，"
          f"累计榜首 {ranked[0]['full_name']}，零外部资源）")


def test_site_chinese(tmp: Path) -> None:
    """中文层：类目列、中文简介、英文回退、链接的可点标识（都在项目名单页）。"""
    from star_pulse import i18n
    from star_pulse import site as site_builder

    settings, conn = _seed(tmp / "zh")
    rows = conn.execute(
        "SELECT repo_id, full_name, description FROM repo ORDER BY repo_id LIMIT 2"
    ).fetchall()
    rid, english = rows[0]["repo_id"], rows[0]["description"]
    # 第二行故意不放进缓存 —— 用来验证「没译到的行回退英文」这条路径
    other_english = rows[1]["description"]
    assert english and other_english, "测试数据里应当有英文简介"

    # 缓存读写往返
    i18n.save_cache(settings, {str(rid): {"zh": "这是一条测试译文", "by": "llm"}})
    assert i18n.load_cache(settings)[str(rid)]["zh"] == "这是一条测试译文"

    docs = Path(site_builder.build_site(settings, conn)["docs"])
    html = (docs / "projects.html").read_text(encoding="utf-8")

    assert "这是一条测试译文" in html, "缓存里的中文译文没被渲染出来"
    # 没译到的行必须回退英文，而不是留白（简介会被截断到 80 字并压平空白）
    fallback = escape(" ".join(other_english.split())[:40])
    assert f">{fallback}" in html, "未翻译的行应回退显示英文原文"
    assert "<th>类目</th>" in html, "缺少类目列"
    assert 'id="projCat"' in html, "缺少类目筛选"
    assert 'class="repo"' in html and "↗" in html, "项目名缺少可点的视觉标识"
    assert 'data-cat="' in html and 'class="tag plain"' in html, "类目单元格或筛选属性缺失"
    assert "机翻" in html, "页面上应说明简介是机翻（不能冒充人工质量）"
    # 默认只展示前 100 条：行仍全在 DOM 里（搜索要覆盖全量），只由 JS 控制显示
    assert 'id="projLimit"' in html, "缺少「每屏条数」控件"
    assert '<option value="100" selected>' in html, "默认显示条数应为 100 条"
    assert html.count("<tr data-name=") >= len(PUBLISHED_W37), "行不能被服务端裁掉"

    # 每条记录独占一行 —— 整张表挤成一行会让 git 完全没法做增量压缩
    assert html.count("<tr data-name=") == sum(
        1 for ln in html.splitlines() if ln.startswith("<tr data-name=")
    ), "项目名单必须一行一条记录"
    # 简介只放一份：重复存 data 属性会把页面从 ~400 KB 撑到 500 KB
    assert "data-desc=" not in html, "简介不应重复存一份 data 属性"
    # data-rid 曾经占 16.4 KB 而 JS 从未引用过，已删除；不要再被加回来
    assert "data-rid=" not in html, "data-rid 是死重（JS 不引用），不该输出"
    print("  [PASS] 中文层：类目列 + 中文简介 + 英文回退 + 链接标识 + 默认条数")


class _FakeUrlopenResp:
    """假的 urlopen 返回值，用来在没有网络的情况下测 MyMemory 后端。

    与下面给 Http 用的 _FakeResp 形状不同：那个暴露 .json()，这个要支持 with。
    """

    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_translate_without_llm(tmp: Path) -> None:
    """后端不可用时，翻译必须优雅跳过：不抛异常，也不留下空缓存文件。"""
    from star_pulse import i18n

    # 环境变量优先级高于一切，先摘掉再构造 settings，避免 CI 上配了 key 就真的发请求
    saved = {k: os.environ.pop(k, None)
             for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "I18N_BACKEND")}
    try:
        settings, _conn = _seed(tmp / "nollm")
        assert not settings.llm_enabled
        # 显式关掉后端。注意：默认的 auto 在没配 LLM 时会自动退回免费机翻，
        # 所以「没配 LLM」已经不再等于「不翻译」了 —— 这里要测的是「没有可用后端」。
        settings = replace(settings, i18n_backend="none")
        assert settings.translator == "none"
        stats = i18n.translate_missing(settings, [
            {"repo_id": 1, "full_name": "a/b", "description": "hello"},
        ])
        assert stats["translated"] == 0, "没有可用后端却报告翻译成功"
        assert "error" in stats, "应明确报告原因，而不是静默返回零"
        assert not i18n.cache_path(settings).exists(), "失败时不应写出空缓存文件"
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value
    print("  [PASS] 无可用翻译后端时优雅跳过（不抛异常、不写空缓存）")


def test_mymemory_backend(tmp: Path) -> None:
    """免费机翻后端：零密钥可用，且**绝不把回显的英文原文当成译文缓存**。

    后半条是真实风险：MyMemory 在没有命中译文时会把原文原样回显，
    如果照单全收，页面上「中文简介」列里就会出现英文 —— 看起来正常、实际错位。
    """
    from star_pulse import i18n

    saved = {k: os.environ.pop(k, None)
             for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "I18N_BACKEND")}
    orig_open = i18n.urllib.request.urlopen
    try:
        settings, _conn = _seed(tmp / "mm")
        # auto + 没配 LLM 应自动落到免费后端
        assert settings.translator == "mymemory", f"实际 {settings.translator}"
        settings = replace(settings, i18n_request_gap=0.0)

        # 1) 回显原文 -> 必须判为「没拿到译文」
        i18n.urllib.request.urlopen = lambda req, timeout=30: _FakeUrlopenResp(
            {"responseStatus": 200, "responseData": {"translatedText": "Hello world"}}
        )
        assert i18n._mymemory_one(settings, "Hello world") is None, "回显原文不得当成译文"

        # 2) 正常译文 -> 写入缓存，并标注来源是机翻
        i18n.urllib.request.urlopen = lambda req, timeout=30: _FakeUrlopenResp(
            {"responseStatus": 200, "responseData": {"translatedText": "你好，世界"}}
        )
        stats = i18n.translate_missing(settings, [
            {"repo_id": 42, "full_name": "a/b", "description": "Hello world"},
        ])
        assert stats["backend"] == "mymemory" and stats["translated"] == 1, f"统计异常：{stats}"
        entry = i18n.load_cache(settings)["42"]
        assert entry["zh"] == "你好，世界" and entry["by"] == "mymemory", f"缓存异常：{entry}"

        # 3) 服务端报错 -> 计入失败，不写缓存、不抛异常
        i18n.urllib.request.urlopen = lambda req, timeout=30: _FakeUrlopenResp(
            {"responseStatus": 429, "responseDetails": "quota exceeded"}
        )
        stats2 = i18n.translate_missing(settings, [
            {"repo_id": 43, "full_name": "c/d", "description": "Another repo"},
        ])
        assert stats2["translated"] == 0 and stats2["failed_batches"] >= 1, f"应记录失败：{stats2}"
        assert "43" not in i18n.load_cache(settings), "失败不应写入缓存"
    finally:
        i18n.urllib.request.urlopen = orig_open
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value
    print("  [PASS] 免费机翻后端：零密钥可用，回显原文不被误当译文，失败不写缓存")


def _repo_json(rid: int, full_name: str, stars: int, created: str = "2026-01-01") -> dict:
    """构造一个 /repos/{name} 与 search API 都通用的仓库 JSON。"""
    owner, _, name = full_name.partition("/")
    return {
        "id": rid, "full_name": full_name, "name": name,
        "owner": {"login": owner}, "description": f"repo {name}",
        "language": "Python", "topics": [], "homepage": None,
        "license": {"spdx_id": "MIT"}, "created_at": f"{created}T00:00:00Z",
        "archived": False, "fork": False,
        "stargazers_count": stars, "forks_count": stars // 10,
        "open_issues_count": 0, "pushed_at": None,
    }


class _FakeResp:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.status = status
        self.body = ""
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHttp:
    """只实现采集用到的读接口，不碰网络。

    搜索故意**无视 max_items**、把全部结果一次性返回 —— 这是为了验证 pipeline
    自带的上限保护：即便 search_recent_repos 的截断哪天被改坏，候选池也不会越过
    max_candidates。没有那层保护，这个假实现会立刻把池子撑爆。
    Trending 复用同一个 get_json，这里一律 404，fetch_trending 有兜底会返回空列表。
    """

    def __init__(self, repos: dict[str, dict], search_items: list[dict]) -> None:
        self.repos = repos
        self.search_items = search_items

    def get_json(self, url, params=None, headers=None, allow_404=False, retries=None):
        if "/search/repositories" in url:
            return _FakeResp({
                "items": list(self.search_items),
                "total_count": len(self.search_items),
            })
        name = url.split("/repos/", 1)[-1].split("?")[0]
        hit = self.repos.get(name)
        return _FakeResp({}, 404) if hit is None else _FakeResp(hit)

    def stats(self) -> str:
        return ""


def _seed_tracked(conn, specs: list[tuple[int, str, int]]) -> None:
    """把「已经追踪了若干天」的仓库写进库。specs: (repo_id, full_name, 快照天数)。"""
    from star_pulse import github_api

    for rid, name, age in specs:
        norm = github_api.normalize_repo(_repo_json(rid, name, 100))
        for d in range(age):
            day = (date(2026, 8, 1) + timedelta(days=d)).isoformat()
            db.upsert_repos(conn, [norm], day)
            db.upsert_snapshots(conn, [{
                "repo_id": rid, "snap_date": day, "stars": 100 + d,
                "forks": 9, "open_issues": 0, "pushed_at": None,
                "source": "test", "collected_at": f"{day}T00:00:00Z",
            }])


def test_candidate_budget(tmp: Path) -> None:
    """候选池预算：保底名额必须留给「已追踪最久」的仓库，总量绝不越过上限。"""
    from dataclasses import replace

    from star_pulse import pipeline

    # 「老」仓库：5 个已追踪 5 天 + 15 个只追了 1 天，星数刻意相同。
    # 唯一区别是快照数 —— 续期若按「已追踪最久」优先，选中的必然是前 5 个。
    specs = [(7000 + i, f"vet/repo{i}", 5) for i in range(5)]
    specs += [(7100 + i, f"rook/repo{i}", 1) for i in range(15)]
    registry = {name: _repo_json(rid, name, 100) for rid, name, _age in specs}
    registry["seed/one"] = _repo_json(6000, "seed/one", 900)
    registry["seed/two"] = _repo_json(6001, "seed/two", 800)
    fresh = [_repo_json(8000 + i, f"new/repo{i}", 500 - i) for i in range(50)]

    def run(sub: str, reserve: int):
        settings = replace(
            load_settings(tmp / sub),
            max_candidates=12, pool_refresh_reserve=reserve,
            search_lookback_days=14, languages=["All"],
            seed_repos=["seed/one", "seed/two"],
        )
        conn = db.connect(settings.db_path)
        db.init_schema(conn)
        _seed_tracked(conn, specs)
        return conn, pipeline.run_daily(settings, conn, _FakeHttp(registry, fresh))

    conn, stats = run("budget", 5)
    # 12 = 2 种子 + 0 Trending + 5 续期 + 5 搜索
    assert stats["candidates_total"] == 12, f"候选池越界或没填满：{stats['candidates_total']}"
    by = stats["by_channel"]
    assert by["watchlist"] == 2, by
    assert by["pool_refresh"] == 5, f"保底名额没被用满：{by}"
    assert by["search"] == 5, f"搜索应正好用掉剩余名额：{by}"

    got = {r["repo_id"] for r in conn.execute(
        "SELECT repo_id FROM discovery WHERE channel = 'pool'")}
    assert got == {7000, 7001, 7002, 7003, 7004}, f"续期挑错了仓库（应挑追踪最久的）：{got}"

    # reserve = 0 应退回旧行为：续期一个都不进，搜索吃满全部剩余名额
    _conn0, stats0 = run("budget-zero", 0)
    assert stats0["by_channel"]["pool_refresh"] == 0, stats0["by_channel"]
    assert stats0["by_channel"]["search"] == 10, stats0["by_channel"]
    assert stats0["candidates_total"] == 12, stats0["candidates_total"]
    print("  [PASS] 候选池预算：续期保底生效且挑最久的，总量不越界（reserve=0 退回旧行为）")


def _repo_row(rid: int, full_name: str) -> dict:
    owner, _, name = full_name.partition("/")
    return {
        "repo_id": rid, "full_name": full_name, "owner": owner, "name": name,
        "description": None, "language": None, "topics": None, "homepage": None,
        "license": None, "repo_created": None, "is_archived": 0, "is_fork": 0,
    }


def _snap_row(rid: int, day: str, stars: int) -> dict:
    return {
        "repo_id": rid, "snap_date": day, "stars": stars, "forks": 0,
        "open_issues": 0, "pushed_at": None, "source": "test",
        "collected_at": f"{day}T00:00:00Z",
    }


def test_rename_recreate_no_crash(tmp: Path) -> None:
    """仓库删除重建：同一个 full_name 挂到新的 repo_id，写入绝不能崩。

    这是真实事故的根因 —— repo_id 不在库里、但 full_name 已被库中另一行占用时，
    INSERT 违反 UNIQUE(full_name)，而 ON CONFLICT(repo_id) 无法处理它。
    """
    settings = load_settings(tmp / "rename")
    conn = db.connect(settings.db_path)
    db.init_schema(conn)

    db.upsert_repos(conn, [_repo_row(100, "a/b")], "2026-01-01")
    db.upsert_snapshots(conn, [_snap_row(100, "2026-01-01", 10)])

    # 删除重建：新 repo_id 复用同名 —— 旧版这里会抛 IntegrityError
    db.upsert_repos(conn, [_repo_row(200, "a/b")], "2026-01-02")

    rows = conn.execute(
        "SELECT repo_id, full_name FROM repo ORDER BY repo_id"
    ).fetchall()
    assert [(r["repo_id"], r["full_name"]) for r in rows] == [(100, "a/b"), (200, "a/b")], (
        f"两个 repo_id 应作为两行共存，实际 {[dict(r) for r in rows]}"
    )
    kept = conn.execute(
        "SELECT stars FROM snapshot WHERE repo_id = 100 AND snap_date = '2026-01-01'"
    ).fetchone()
    assert kept and kept["stars"] == 10, "旧仓库的快照历史不能丢"
    print("  [PASS] 删除重建（同名不同 repo_id）不再崩溃，两行共存且历史保留")


def test_no_orphan_snapshots(tmp: Path) -> None:
    """删除重建后，旧 repo_id 的快照不能变成孤儿（否则 JOIN 类查询静默丢历史）。"""
    snaps_dir = tmp / "orphan" / "data" / "snapshots"
    snaps_dir.mkdir(parents=True)
    (snaps_dir / "2026-01-01.json").write_text(json.dumps({
        "date": "2026-01-01",
        "repos": [{"full_name": "a/b", "repo_id": 100, "stars": 10, "forks": 0,
                   "open_issues": 0, "is_archived": 0, "is_fork": 0}],
    }, ensure_ascii=False), encoding="utf-8")
    (snaps_dir / "2026-01-02.json").write_text(json.dumps({
        "date": "2026-01-02",
        "repos": [{"full_name": "a/b", "repo_id": 200, "stars": 20, "forks": 0,
                   "open_issues": 0, "is_archived": 0, "is_fork": 0}],
    }, ensure_ascii=False), encoding="utf-8")

    conn = db.connect(tmp / "orphan" / "o.db")
    db.init_schema(conn)
    db.import_snapshot_jsons(conn, snaps_dir)

    assert db.orphan_snapshot_count(conn) == 0, "不应产生孤儿快照"
    ids = {r["repo_id"] for r in conn.execute("SELECT repo_id FROM repo")}
    assert ids == {100, 200}, f"两个 repo_id 都应保留，实际 {ids}"
    days = {r["snap_date"] for r in conn.execute("SELECT snap_date FROM snapshot")}
    assert days == {"2026-01-01", "2026-01-02"}, f"两天的快照都应在，实际 {days}"
    print("  [PASS] 删除重建后无孤儿快照，两段历史都还在")


def test_summary_counts_match_join_views(tmp: Path) -> None:
    """「每天入库数」必须与「项目名单」同口径，否则页面上的数字自相矛盾。"""
    settings, conn = _seed(tmp / "counts")
    summary = analyze.daily_summary(conn)
    assert summary, "应当有汇总数据"
    for item in summary:
        projects = analyze.project_rows(conn, item["date"])
        assert item["repos"] == len(projects), (
            f"{item['date']} 汇总口径 {item['repos']} != 名单口径 {len(projects)}"
        )
    print(f"  [PASS] 逐日入库数与项目名单口径一致（{len(summary)} 天）")


def test_import_tolerates_malformed_rows(tmp: Path) -> None:
    """畸形快照文件/行必须被跳过并计数，不能让整个回灌（乃至所有命令）失败。"""
    snaps_dir = tmp / "malformed" / "data" / "snapshots"
    snaps_dir.mkdir(parents=True)

    (snaps_dir / "2026-01-01.json").write_text(json.dumps({
        "date": "2026-01-01",
        "repos": [{"full_name": "bad/stars", "repo_id": 1, "stars": "not-an-int"}],
    }, ensure_ascii=False), encoding="utf-8")
    (snaps_dir / "2026-01-02.json").write_text(json.dumps({
        "date": "2026-01-02",
        "repos": [{"full_name": "huge/id", "repo_id": 2 ** 63, "stars": 5}],
    }, ensure_ascii=False), encoding="utf-8")
    (snaps_dir / "2026-01-03.json").write_text(json.dumps({
        "date": "2026-01-03",
        "repos": [{"repo_id": 3, "stars": 5}],
    }, ensure_ascii=False), encoding="utf-8")
    (snaps_dir / "2026-01-04.json").write_text(json.dumps({
        "date": "2026-01-04", "repos": "not-a-list",
    }, ensure_ascii=False), encoding="utf-8")
    (snaps_dir / "2026-01-05.json").write_text('{"date": "2026-01-05", "repos": [',
                                               encoding="utf-8")
    (snaps_dir / "2026-01-06.json").write_text(json.dumps({
        "date": "2026-01-06",
        "repos": [{"full_name": "good/repo", "repo_id": 9, "stars": 42}],
    }, ensure_ascii=False), encoding="utf-8")

    conn = db.connect(tmp / "malformed" / "m.db")
    db.init_schema(conn)
    res = db.import_snapshot_jsons(conn, snaps_dir)

    assert res.bad_files == 2, f"应有 2 个坏文件（非数组 / 截断 JSON），实际 {res.bad_files}"
    assert res.skipped == 3, f"应跳过 3 行（坏 stars / 越界 id / 缺 full_name），实际 {res.skipped}"
    assert res.snaps == 1, f"只有完好行的快照应被写入，实际 {res.snaps}"
    assert res.repos == 2, (
        f"坏 stars 的 repo 行应保留（只丢快照），加上完好行共 2 个，实际 {res.repos}"
    )
    good = conn.execute(
        "SELECT stars FROM snapshot WHERE repo_id = 9 AND snap_date = '2026-01-06'"
    ).fetchone()
    assert good and good["stars"] == 42, "完好行必须被正确写入"
    print("  [PASS] 畸形快照逐行容错：坏行跳过并计数，完好行照常写入")


_LEGACY_REPO_DDL = """
CREATE TABLE repo (
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
CREATE INDEX idx_repo_created ON repo(repo_created);
"""


def test_schema_migration_from_legacy(tmp: Path) -> None:
    """v1 库（full_name 带 UNIQUE）必须被就地升级到 v2，且不丢数据。"""
    conn = db.connect(tmp / "legacy" / "old.db")
    conn.executescript(_LEGACY_REPO_DDL)
    conn.execute(
        "INSERT INTO repo (repo_id, full_name, owner, name, first_seen) "
        "VALUES (1, 'a/b', 'a', 'b', '2026-01-01')"
    )
    conn.commit()
    assert db._repo_is_legacy(conn), "构造的库应当被识别为旧结构"

    db.init_schema(conn)

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == db.SCHEMA_VERSION, f"user_version 应为 {db.SCHEMA_VERSION}，实际 {version}"
    uniques = [r for r in conn.execute("PRAGMA index_list(repo)").fetchall() if r[2]]
    assert not uniques, f"迁移后不应再有唯一索引：{uniques}"
    kept = conn.execute("SELECT repo_id, full_name FROM repo").fetchall()
    assert [(r["repo_id"], r["full_name"]) for r in kept] == [(1, "a/b")], "迁移不能丢行"
    idx = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_repo_created'"
    ).fetchone()
    assert idx, "idx_repo_created 应当被重建到新表上"

    # 迁移后同名新 repo_id 必须能写入
    db.upsert_repos(conn, [_repo_row(2, "a/b")], "2026-01-02")
    assert conn.execute("SELECT COUNT(*) c FROM repo").fetchone()["c"] == 2
    print("  [PASS] v1→v2 就地迁移：约束移除、数据保留、随后可写入同名新仓库")


def test_daily_batch_is_atomic(tmp: Path) -> None:
    """一天的采集必须整批成功或整批失败，不能留下半批状态。"""
    conn = db.connect(tmp / "atomic" / "a.db")
    db.init_schema(conn)

    bad_snap = {"repo_id": 1, "snap_date": "2026-01-01", "stars": 1}  # 缺多个必填字段
    raised = False
    try:
        db.save_daily_batch(
            conn, [_repo_row(1, "a/b")], [bad_snap], [(1, "2026-01-01", "seed", None)],
            "2026-01-01",
        )
    except Exception:  # noqa: BLE001
        raised = True
    assert raised, "缺字段的快照应当让整批失败"
    for table in ("repo", "snapshot", "discovery"):
        n = conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        assert n == 0, f"事务回滚后 {table} 不应有残留行，实际 {n}"
    print("  [PASS] 单日采集事务原子：中途失败后三张表都无残留")


def test_snapshot_json_merges(tmp: Path) -> None:
    """快照 JSON 必须**合并写**：补跑一部分仓库不能抹掉当天其余仓库。

    真实风险：JSON 是唯一事实来源且会 commit，而 `run-daily --limit 30`、
    `snapshot --repos`、失败补跑都会再次写同一天的文件。整文件覆盖会让当天
    800 个仓库缩成几十条，下一轮 CI 回灌即永久丢数据。
    """
    snaps = tmp / "merge" / "data" / "snapshots"
    snaps.mkdir(parents=True)

    def row(rid: int, name: str, stars: int) -> dict:
        return {"repo_id": rid, "full_name": name, "stars": stars, "forks": 1,
                "open_issues": 0, "language": "Python", "description": "x",
                "repo_created": "2026-01-01", "is_archived": 0, "is_fork": 0}

    db.export_snapshot_json(snaps, "2026-01-01", [row(1, "a/one", 10), row(2, "a/two", 20)])
    db.export_snapshot_json(snaps, "2026-01-01", [row(1, "a/one", 99)])  # 只补跑 1 个

    data = json.loads((snaps / "2026-01-01.json").read_text(encoding="utf-8"))
    by_id = {r["repo_id"]: r for r in data["repos"]}
    assert data["count"] == 2 and set(by_id) == {1, 2}, f"补跑抹掉了仓库：{data}"
    assert by_id[1]["stars"] == 99, "本次数据应覆盖旧值"
    assert by_id[2]["stars"] == 20, "未参与的仓库应原样保留"

    # 空结果：不落盘、不碰已有文件，也绝不凭空造出空快照
    assert db.export_snapshot_json(snaps, "2026-01-01", []) is None
    assert json.loads((snaps / "2026-01-01.json").read_text(encoding="utf-8"))["count"] == 2
    assert db.export_snapshot_json(snaps, "2026-01-02", []) is None
    assert not (snaps / "2026-01-02.json").exists(), (
        "空结果不得生成文件：发布门只看文件是否存在，空文件会被误判成「今日已产出」"
    )
    print("  [PASS] 快照 JSON 合并写：补跑不抹数据、空结果不落盘")


def test_range_gain_aligned_window(tmp: Path) -> None:
    """累计榜必须用「两端都有快照」的对齐窗口，不能用 weekly_gain 的跨度过滤。

    真实事故：站点曾用 weekly_gain(全历史, max_span_days=10) 算累计榜。跨度过滤
    恰好命中了所有「从第一天追踪到现在」的仓库（它们的跨度就等于整个窗口），
    于是真正涨得最多的全被剔除，榜首反而成了只被追踪几天就掉出池子的仓库 ——
    而页面标注的却是整个窗口。
    """
    settings = load_settings(tmp / "range")
    conn = db.connect(settings.db_path)
    db.init_schema(conn)

    days = ["2026-01-01", "2026-01-02", "2026-01-03"]
    # 全程在场，涨得最多
    for day, stars in zip(days, (100, 150, 500)):
        db.upsert_repos(conn, [_repo_row(1, "old/faithful")], day)
        db.upsert_snapshots(conn, [_snap_row(1, day, stars)])
    # 中途掉出候选池，只被追踪 2 天
    for day, stars in zip(days[:2], (100, 160)):
        db.upsert_repos(conn, [_repo_row(2, "stale/dropped")], day)
        db.upsert_snapshots(conn, [_snap_row(2, day, stars)])
    # 最后一天才进池，没有基线
    db.upsert_repos(conn, [_repo_row(3, "late/joiner")], days[-1])
    db.upsert_snapshots(conn, [_snap_row(3, days[-1], 900)])

    ranked, comparable = analyze.range_gain(conn, days[0], days[-1])
    names = [r["full_name"] for r in ranked]
    assert names == ["old/faithful"], f"累计榜应只含两端都在场的仓库，实际 {names}"
    assert ranked[0]["delta"] == 400, f"增量应为 500-100 = 400，实际 {ranked[0]['delta']}"
    assert comparable == 1, f"可比仓库应只有 1 个，实际 {comparable}"

    # 反证：旧写法（weekly_gain + 小跨度上限）会把冠军剔除、把掉队者捧上榜首
    old, dropped = analyze.weekly_gain(conn, days[0], days[-1], max_span_days=1)
    assert "old/faithful" in {r["full_name"] for r in dropped}, "旧写法应把冠军剔除"
    assert "stale/dropped" in {r["full_name"] for r in old}, "旧写法应把掉队者留在榜上"
    print("  [PASS] 累计榜用对齐窗口：冠军不再被跨度过滤误杀，中途进池的不参与")


_WF_TOP_LEVEL = ("name:", "on:", "permissions:", "concurrency:", "jobs:",
                 "env:", "run-name:", "defaults:")


def _check_workflow_yaml(name: str, text: str) -> None:
    """对一份 workflow 文本做结构性检查，不合规就抛 AssertionError。

    启发式，不是完整 YAML 解析（项目零依赖、CI 不装 PyYAML）：
      1. 不允许制表符（YAML 禁止 tab 缩进）；
      2. 第 0 列只允许出现已知的顶层键；
      3. `run: |` 块标量结束后紧跟的那一行必须像样的 YAML（列表项或键）。
    """
    lines = text.splitlines()
    for i, raw in enumerate(lines, 1):
        assert "\t" not in raw, f"{name}:{i} 出现制表符，YAML 不允许 tab 缩进"
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent == 0:
            assert raw.startswith(_WF_TOP_LEVEL), (
                f"{name}:{i} 第 0 列出现了非顶层键的内容：{raw[:70]!r}\n"
                f"    这通常意味着上面某个 `run: |` 块被顶格的行截断了"
            )
        if not raw.strip().startswith("run:"):
            continue
        value = raw.strip()[len("run:"):].strip()
        if value not in ("|", ">", "|-", ">-", "|+", ">+"):
            continue
        for j in range(i, len(lines)):  # lines[i] 即 run: 的下一行
            body = lines[j]
            if not body.strip() or body.lstrip().startswith("#"):
                continue
            if len(body) - len(body.lstrip()) > indent:
                continue  # 仍在块标量内
            # 缩进回退到 ≤ 键那一行，说明块到此结束 —— 这本身合法（后面接兄弟键、
            # 下一个列表项或注释）。但这一行必须是像样的 YAML，否则就是被截断后的残行。
            stripped = body.strip()
            assert stripped.startswith("-") or ":" in stripped, (
                f"{name}:{j + 1} 像是 `run: {value}` 块被截断后的残行：{stripped[:70]!r}"
            )
            break


def test_workflow_yaml_is_structurally_sane() -> None:
    """workflow 的 YAML 结构必须合法 —— 坏掉的 workflow 会**静默停掉整条流水线**。

    真实事故：在 `run: |` 块标量里内联了一段多行 Python，其中几行顶格书写，
    块标量被提前截断，剩下的行按顶层 YAML 解析直接报错。GitHub 判定
    "workflow file issue"，整个 daily.yml 以 0 秒失败 —— 当天的采集就此消失，
    日志里连一行都没有，非常难发现。
    """
    wf_dir = Path(__file__).resolve().parent.parent / ".github" / "workflows"
    assert wf_dir.is_dir(), f"找不到 workflow 目录：{wf_dir}"
    files = sorted(wf_dir.glob("*.yml"))
    assert files, "没有任何 workflow 文件"
    for path in files:
        _check_workflow_yaml(path.name, path.read_text(encoding="utf-8"))

    # 守卫自身必须真的能抓到那次事故的写法，否则它只是个安慰剂
    bad = (
        "name: x\njobs:\n  a:\n    steps:\n      - name: g\n"
        "        run: |\n          COUNT=$(python -c 'import json,sys\n"
        "p = json.load(open(sys.argv[1]))\ntry:\n    pass'\n"
    )
    try:
        _check_workflow_yaml("bad.yml", bad)
    except AssertionError:
        pass
    else:
        raise AssertionError("守卫没能识别出被顶格内容截断的 run 块标量")
    print(f"  [PASS] workflow YAML 结构合法（{len(files)} 个文件，且守卫可复现该事故）")


def test_report_uses_translation_cache(tmp: Path) -> None:
    """周报的「一句话」必须能用上翻译缓存。

    此前周报只认 LLM 本期解读或**英文原文**，从不读 data/i18n/zh.json ——
    于是看板上明明已经是中文的项目，到了周报里又变回英文（实测 20 条里只有 3 条
    中文，而那 3 条恰好是 LLM 解读覆盖到的）。回退链应当是：
    LLM 本期解读 > 翻译缓存 > 英文原文。
    """
    from star_pulse import i18n

    settings, conn = _seed(tmp / "repzh")
    ranked, _dropped = analyze.weekly_gain(conn, START, END, settings.max_span_days)
    top_rid = ranked[0]["repo_id"]
    i18n.save_cache(settings, {str(top_rid): {"zh": "周报专用测试译文", "by": "mymemory"}})

    md, html, meta = render.build_report(settings, conn, START, END)
    assert meta["top"][0]["repo_id"] == top_rid, "榜单第一名与预期不符"
    assert meta["top"][0]["summary"] == "周报专用测试译文", (
        f"周报未回退到翻译缓存，实际拿到：{str(meta['top'][0]['summary'])[:50]!r}"
    )
    assert "周报专用测试译文" in md, "Markdown 周报未使用译文"
    assert "周报专用测试译文" in html, "HTML 周报未使用译文"

    # LLM 本期解读的优先级仍然最高
    _md2, _html2, meta2 = render.build_report(
        settings, conn, START, END,
        narration={meta["top"][0]["full_name"]: "本期 LLM 解读"},
    )
    assert meta2["top"][0]["summary"] == "本期 LLM 解读", "LLM 解读应优先于翻译缓存"
    print("  [PASS] 周报「一句话」回退链正确（LLM 解读 > 翻译缓存 > 英文原文）")


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
        test_site_chinese(tmp)
        test_translate_without_llm(tmp)
        test_mymemory_backend(tmp)
        test_candidate_budget(tmp)
        test_rename_recreate_no_crash(tmp)
        test_no_orphan_snapshots(tmp)
        test_summary_counts_match_join_views(tmp)
        test_import_tolerates_malformed_rows(tmp)
        test_schema_migration_from_legacy(tmp)
        test_daily_batch_is_atomic(tmp)
        test_snapshot_json_merges(tmp)
        test_range_gain_aligned_window(tmp)
        test_workflow_yaml_is_structurally_sane()
        test_report_uses_translation_cache(tmp)
    print("=" * 58)
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
