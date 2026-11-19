"""卡片宇宙：把快照库折叠成「一仓一卡」，产出 docs/codex.html + docs/cards-data.js。

为什么是构建产物而不是手工维护
------------------------------
卡片的每一个字段都能从 `repo` + `snapshot` 两张表复算出来。手工维护一张卡，
就等于把事实抄了第二遍，迟早和快照对不上。这里只做一次折叠：

  stars      最后一次观测到的星数
  depth      该仓库拥有的快照条数
  span       首末快照跨度（天）
  oa         距最新快照多少天（抓取池轮转，末次观测可能不是今天）
  rate       追踪期日均星数 = (末 - 首) / span
             只有 1 条快照时退回「生涯日均」= stars / 建库天数，并单独标注；
             卡面在档案口径下会把这类卡标成「未测速」，不用生涯均速冒充动量
  faction    语言家族 6+1（系统/前端/脚本/工程/运维/学术/无名）
  cls        analyze.classify()，其他 → 散修
  cohort     档案深度组：成型 ≥30 / 成长 14–29 / 观察 7–13 / 初现 2–6 / 一瞥 1
  rarity     组内分位（旧口径，保留给页面上的「组内分位」开关）
  traits     新星 / 爆发 / 常青 / 长跑者 / 沉默巨人 / 回归者，全部是数据判定

页面本身是零依赖的静态 HTML，数据通过 `<script src="cards-data.js">` 注入，
所以 file:// 和 GitHub Pages 都能直接打开。

边界：**只读快照库，不写数据库**。失败不应该影响看板和快照，调用方负责兜住异常。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from . import analyze, i18n, theme
from .config import Settings

log = logging.getLogger("star_pulse.cards")

TEMPLATE = Path(__file__).resolve().parent / "templates" / "codex.html"

# ── 语言 → 阵营 ────────────────────────────────────────────────
FACTIONS = {
    "系统": {"Rust", "C", "C++", "Zig", "Assembly", "Objective-C", "Makefile",
             "CMake", "Cuda", "GLSL", "LLVM", "Verilog", "ShaderLab", "Smali"},
    "前端": {"TypeScript", "JavaScript", "HTML", "CSS", "Vue", "Svelte", "SCSS",
             "Sass", "Astro", "MDX", "QML", "Less"},
    "脚本": {"Python", "Ruby", "Perl", "Lua", "Luau", "PHP", "R", "Julia",
             "GDScript", "Nushell", "PureBasic"},
    "工程": {"Go", "Java", "C#", "Kotlin", "Swift", "Dart", "Scala", "Elixir",
             "Clojure", "Solidity", "Visual Basic .NET", "PLSQL", "Groovy"},
    "运维": {"Shell", "PowerShell", "Dockerfile", "Nix", "HCL", "Batchfile"},
    "学术": {"Lean", "MATLAB", "TeX", "Jupyter Notebook", "Haskell", "Fortran",
             "Standard ML", "Rocq Prover", "Markdown", "Rich Text Format",
             "Coq", "Isabelle"},
}
LANG2FACTION = {lang: fac for fac, langs in FACTIONS.items() for lang in langs}
FACTION_ORDER = ["系统", "前端", "脚本", "工程", "运维", "学术", "无名"]

# ── 稀有度梯度（旧口径，页面可切换查看）────────────────────────
RARITY_LADDER = [("传说", 0.005), ("史诗", 0.02), ("稀有", 0.08),
                 ("优秀", 0.25), ("普通", 1.0)]

COHORT_DESC = {
    "成型": "档案 ≥30 条，最长可比",
    "成长": "档案 14–29 条",
    "观察": "档案 7–13 条",
    "初现": "档案 2–6 条",
    "一瞥": "仅 1 条档案，无法测速",
}


def _days_between(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _cohort_of(depth: int) -> str:
    if depth >= 30:
        return "成型"
    if depth >= 14:
        return "成长"
    if depth >= 7:
        return "观察"
    if depth >= 2:
        return "初现"
    return "一瞥"


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _load_series(conn: sqlite3.Connection) -> dict[int, dict]:
    """一次读完全部快照，按 repo_id 折叠。"""
    rows = conn.execute(
        """
        SELECT r.repo_id, r.full_name, r.language, r.description, r.topics,
               r.repo_created, s.snap_date, s.stars
        FROM snapshot s JOIN repo r ON r.repo_id = s.repo_id
        WHERE r.is_fork = 0 AND r.is_archived = 0
        ORDER BY r.repo_id, s.snap_date
        """
    ).fetchall()

    by_repo: dict[int, dict] = {}
    for r in rows:
        rid = r["repo_id"]
        card = by_repo.get(rid)
        if card is None:
            card = by_repo[rid] = {
                "repo_id": rid,
                "name": r["full_name"],
                "lang": (r["language"] or "").strip(),
                "desc": (r["description"] or "").strip(),
                "topics": r["topics"],
                "created": r["repo_created"] or "",
                "series": [],
            }
        card["series"].append((r["snap_date"], r["stars"] or 0))
    return by_repo


def _fold(by_repo: dict[int, dict], zh: dict[str, dict], latest: str) -> list[dict]:
    cards: list[dict] = []
    for c in by_repo.values():
        series = c["series"]
        s0, sn = series[0], series[-1]
        depth = len(series)
        span = max(0, _days_between(s0[0], sn[0]))
        stars = sn[1]
        peak = max(s for _, s in series)

        if depth >= 2 and span >= 1:
            rate = (sn[1] - s0[1]) / span
            rate_kind = "tracked"
        else:
            age = max(1, _days_between(c["created"], latest)) if c["created"] else 1
            rate = stars / age
            rate_kind = "life"

        gap = 0
        for i in range(1, len(series)):
            gap = max(gap, _days_between(series[i - 1][0], series[i][0]))

        # 中文简介：只有当缓存里的 src 与当前简介一致时才用，避免仓库改了简介、
        # 页面还显示旧译文。缓存键是 repo_id，改名不会失效，但内容会变。
        desc = c["desc"]
        entry = zh.get(str(c["repo_id"])) or {}
        use_zh = bool(entry.get("zh")) and (entry.get("src") or "").strip() == desc
        display = (entry["zh"] if use_zh else desc).strip()
        if len(display) > 160:
            display = display[:160] + "…"

        cls = analyze.classify({
            "description": desc,
            "full_name": c["name"],
            "language": c["lang"],
            "topics": c["topics"],
        })
        if cls == "其他":
            cls = "散修"

        cards.append({
            "id": c["repo_id"],
            "name": c["name"],
            "lang": c["lang"] or "—",
            "faction": LANG2FACTION.get(c["lang"], "无名"),
            "cls": cls,
            "stars": stars,
            "peak": peak,
            "depth": depth,
            "span": span,
            "first": s0[0],
            "last": sn[0],
            "created": c["created"],
            "rate": rate,
            "rate_kind": rate_kind,
            "gap": gap,
            "obs_age": _days_between(sn[0], latest),
            "desc": display,
            "raw_desc": desc,
            "translated": 1 if (use_zh or _has_cjk(desc)) else 0,
            "series": series,
        })

    for c in cards:
        c["cohort"] = _cohort_of(c["depth"])

    # ── 稀有度：组内分位（旧口径）─────────────────────────────
    # 只有 1 条档案的卡没有速度可言，改按体量定档；其余组按追踪期日均速度。
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in cards:
        groups[c["cohort"]].append(c)

    for cohort, members in groups.items():
        if cohort == "一瞥":
            members.sort(key=lambda x: (-x["stars"], x["name"]))
        else:
            members.sort(key=lambda x: (-x["rate"], -x["stars"], x["name"]))
        total = len(members)
        for i, c in enumerate(members):
            c["pct"] = round(max(0.01, (i + 1) / total * 100), 2)
            strong = c["stars"] > 0 if cohort == "一瞥" else c["rate"] > 0
            rar = "普通"
            if i == 0:
                rar = "传说"  # 每组第一名必为传说，小样本组也有自己的冠军
            elif strong:
                for label, cut in RARITY_LADDER:
                    if (i + 1) / total <= cut:
                        rar = label
                        break
            c["rarity"] = rar
            c["cohort_size"] = total
            c["cohort_rank"] = i + 1

    # ── 特性 ──────────────────────────────────────────────────
    for c in cards:
        traits = []
        age = _days_between(c["created"], latest) if c["created"] else 9999
        if 0 <= age <= 7:
            traits.append("新星")
        if c["cohort"] != "一瞥" and c["rate"] > 0 and c["pct"] <= 2:
            traits.append("爆发")
        if c["depth"] >= 30 and c["rate"] >= 100:
            traits.append("常青")
        if c["depth"] >= 45:
            traits.append("长跑者")
        if c["stars"] >= 100_000 and c["rate"] < 100:
            traits.append("沉默巨人")
        if c["gap"] >= 7:
            traits.append("回归者")
        c["traits"] = traits[:3]

    for i, c in enumerate(sorted(cards, key=lambda x: (-x["stars"], x["name"])), 1):
        c["star_rank"] = i
    return cards


def _payload(cards: list[dict]) -> list[dict]:
    out = []
    for c in sorted(cards, key=lambda x: -x["stars"]):
        out.append({
            "id": c["id"],
            "n": c["name"],
            "l": c["lang"],
            "f": c["faction"],
            "c": c["cls"],
            "s": c["stars"],
            "k": c["peak"],
            "d": c["depth"],
            "sp": c["span"],
            "fs": c["first"],
            "ls": c["last"],
            "cr": c["created"],
            "v": round(c["rate"], 2),
            "vk": c["rate_kind"],
            "g": c["gap"],
            "oa": c["obs_age"],
            "ch": c["cohort"],
            "q": c["rarity"],
            "p": c["pct"],
            "cn": c["cohort_size"],
            "cq": c["cohort_rank"],
            "rk": c["star_rank"],
            "t": c["traits"],
            "ds": c["desc"],
            # 只有「中文译文」才需要附英文原文；未翻译时 ds 本身就是原文，不重复存。
            "de": (c["raw_desc"][:240]
                   if c["translated"] and c["raw_desc"].strip() != c["desc"].strip()
                   else ""),
            "tr": c["translated"],
            # 曲线只对 ≥2 条快照有意义；一瞥组不画假线。
            "sx": ";".join(
                f"{_days_between(c['first'], d)}:{s}" for d, s in c["series"]
            ) if c["depth"] >= 2 else "",
        })
    return out


def build_cards(settings: Settings, conn: sqlite3.Connection) -> dict:
    """生成卡片数据与图鉴页。返回摘要；不写数据库。"""
    docs = settings.root / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    dates = analyze.all_snapshot_dates(conn)
    if not dates:
        raise RuntimeError("还没有任何快照，先跑 run-daily")
    latest = dates[-1]

    zh = i18n.load_cache(settings)
    by_repo = _load_series(conn)
    cards = _fold(by_repo, zh, latest)
    payload = _payload(cards)

    meta = {
        "latest": latest,
        "first_day": dates[0],
        "days": len(dates),
        "total": len(payload),
        "translated": sum(1 for c in payload if c["tr"]),
        "cohorts": {k: sum(1 for c in cards if c["cohort"] == k)
                    for k in ("成型", "成长", "观察", "初现", "一瞥")},
        "factions": {k: sum(1 for c in payload if c["f"] == k) for k in FACTION_ORDER},
        "classes": dict(Counter(c["c"] for c in payload).most_common()),
        "rarities": dict(Counter(c["q"] for c in payload).most_common()),
        "traits": dict(Counter(t for c in payload for t in c["t"]).most_common()),
    }

    js = (
        "/* 由 star_pulse.cards 自动生成，请勿手工编辑。\n"
        f"   最新快照 {latest}；口径见 star_pulse/cards.py */\n"
        "window.CARDS_META = " + json.dumps(meta, ensure_ascii=False) + ";\n"
        "window.CARDS = " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n"
    )
    data_path = docs / "cards-data.js"
    data_path.write_text(js, encoding="utf-8")

    if not TEMPLATE.is_file():
        raise FileNotFoundError(f"卡片模板缺失：{TEMPLATE}")
    # 站点导航与看板/项目/周报共用同一份（theme.sitenav / SITENAV_CSS）——
    # 三个视图的入口必须在同一位置、同一套文案，否则「从图鉴进首页就回不去」。
    page = TEMPLATE.read_text(encoding="utf-8")
    page = page.replace("<!--__SITENAV__-->", theme.sitenav("codex"))
    page = page.replace("/*__SITENAV_CSS__*/", theme.SITENAV_CSS)
    (docs / "codex.html").write_text(page, encoding="utf-8")

    log.info("卡片：%d 张，最新快照 %s，中文简介 %d 张（%.0f%%）",
             len(payload), latest, meta["translated"],
             meta["translated"] / len(payload) * 100 if payload else 0)
    return {
        "cards": len(payload),
        "latest": latest,
        "translated": meta["translated"],
        "bytes": data_path.stat().st_size,
        "data": str(data_path),
        "page": str(docs / "codex.html"),
    }
