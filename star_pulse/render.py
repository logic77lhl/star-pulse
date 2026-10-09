"""报告组装与渲染（Markdown / HTML）。

报告里始终包含一个「数据质量」小节，如实交代：
  - 快照覆盖了几天（决定本期结论可不可信）
  - 哪些仓库因为跨度超限被剔除（避免 14 天增量混进 7 天榜）
  - 哪些仓库被标记为疑似刷星
这是把「自动化榜单」和「可信榜单」区分开的地方。

HTML 侧与看板共用 `theme`（设计令牌 + 样式表 + HTML 原语）。此前这里另写了一套
CSS，同一个产品的两类页面字体、间距、表格样式、暗色模式全不一致，改一处漏一处。
图表同样改成服务端渲染 —— 周报里曾经也从 cdnjs 加载 Chart.js。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date

from . import analyze, charts, i18n, theme
from .config import Settings

esc = theme.esc


def week_label(start: str, end: str) -> str:
    """按周期**末日**归属 ISO 周。

    为什么不用起始日：对于标准的周一~周日周期，两者结果相同；
    但对于跨周的自定义周期，用末日更符合直觉（例如 09-06 周日 ~ 09-13 周日 应记作 W37）。
    """
    try:
        iso = date.fromisoformat(end).isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    except ValueError:
        return f"{start}_{end}"


def _repo_meta(conn: sqlite3.Connection) -> dict[int, dict]:
    """按 repo_id 索引的仓库元数据。

    不能用 full_name 作键：仓库删除重建后同名会对应两个 repo_id，
    按名字查表会把两个不同仓库的元数据折叠成一条。
    """
    rows = conn.execute(
        "SELECT repo_id, full_name, language, description, topics FROM repo"
    ).fetchall()
    return {r["repo_id"]: dict(r) for r in rows}


def build_report(
    settings: Settings,
    conn: sqlite3.Connection,
    start: str,
    end: str,
    narration: dict | None = None,
) -> tuple[str, str, dict]:
    """组装报告，返回 (markdown, html, meta)。"""
    coverage = analyze.data_coverage(conn, settings)
    ranked_all, dropped = analyze.weekly_gain(conn, start, end, settings.max_span_days)
    meta_map = _repo_meta(conn)

    top = ranked_all[: settings.top_n]
    # 中文简介缓存。此前周报**完全没有**读它 —— 「一句话」列走的是 LLM 解读或
    # 英文原文，于是看板上明明已经是中文的项目，到了周报里又变回英文。
    # 缓存与看板同源（data/i18n/zh.json，以 repo_id 为键），这里补上回退链：
    #   LLM 本期解读 > 翻译缓存 > 英文原文
    zh = i18n.load_cache(settings)
    for item in top:
        history = analyze.daily_history(conn, item["repo_id"], start, end)
        item["flags"] = analyze.star_farm_flags(item, history)
        info = meta_map.get(item["repo_id"], {})
        item["category"] = analyze.classify({**info, "full_name": item["full_name"]})
        item["summary"] = (
            (narration or {}).get(item["full_name"])
            or (zh.get(str(item["repo_id"])) or {}).get("zh")
            or (item.get("description") or "")
        )

    new_repos = analyze.new_repos_in_period(conn, start, end, limit=settings.top_n)
    for item in new_repos:
        info = meta_map.get(item["repo_id"], {})
        item["category"] = analyze.classify({**info, "full_name": item["full_name"]})

    breakdown = analyze.category_breakdown(top, meta_map)
    label = week_label(start, end)

    # 增量榜能不能出，只看「有没有仓库同时具备周期前后的两个快照」，
    # 而不是看总共有多少天数据 —— 查询一个历史区间时，2 个快照就足够了。
    warm = bool(top)
    if warm:
        warmup_note = ""
    elif coverage["distinct_days"] < 2:
        warmup_note = (
            f"「本周新增 Star」需要至少两个时间点的快照相减，当前只有 "
            f"{coverage['distinct_days']} 天数据，因此本期暂不输出增量榜。"
            "这是设计使然，不是故障：GitHub 没有提供星数增量的官方接口，"
            "任何声称能直接查到周增量、又拿不出历史快照的方案，数据都不可信。"
        )
    else:
        warmup_note = (
            "本周期内没有可比较的增量：可能所有候选仓库星数均未增长，"
            "或它们的时间跨度超出了上限而被剔除。详见下方「数据质量」。"
        )

    meta = {
        "period": [start, end],
        "week": label,
        "coverage": coverage,
        "top": top,
        "new_repos": new_repos,
        "dropped": dropped[:20],
        "dropped_count": len(dropped),
        "breakdown": breakdown,
        "max_span_days": settings.max_span_days,
        "warm": warm,
        "warmup_note": warmup_note,
    }

    md = _markdown(settings, label, start, end, meta)
    html = _html(settings, label, start, end, meta)
    return md, html, meta


def _truncate(text: str, limit: int = 70) -> str:
    """压平空白并截断。**先截断、后转义**。

    顺序反了会把 HTML 实体切断：`Tom &amp; Jerry` 截到 70 字可能留下 `&am`，
    浏览器会把它渲染成乱码。所以这里只处理纯文本，转义交给渲染方。
    """
    text = " ".join((text or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


# ── Markdown ────────────────────────────────────────────────────
def _fmt_flags(flags: list[str]) -> str:
    return f" ⚠️ {'、'.join(flags)}" if flags else ""


def _markdown(settings: Settings, label: str, start: str, end: str, meta: dict) -> str:
    cov = meta["coverage"]
    top = meta["top"]
    L: list[str] = []
    L.append(f"# GitHub 星耀榜 · {label}")
    L.append("")
    L.append(f"统计周期：**{start} ~ {end}**（UTC+{settings.tz_offset_hours}）　｜　"
             f"数据截至：{cov['last_day'] or '—'}　｜　快照覆盖：{cov['distinct_days']} 天")
    L.append("")
    L.append("---")
    L.append("")

    if not meta["warm"]:
        L.append("## ⏳ 本期无增量榜")
        L.append("")
        L.append(meta["warmup_note"])
        L.append("")
        if meta["new_repos"]:
            L.append("下方「新项目榜」不依赖历史快照，仍然可用。")
            L.append("")

    if top:
        L.append(f"## 本周新增 Star Top {len(top)}")
        L.append("")
        L.append("| 排名 | 项目 | 本周新增 | 总星 | 语言 | 分类 | 一句话 |")
        L.append("|---:|---|---:|---:|---|---|---|")
        for i, item in enumerate(top, 1):
            desc = _truncate(item.get("summary") or "").replace("|", "／")
            L.append(
                f"| {i} | [{item['full_name']}](https://github.com/{item['full_name']})"
                f"{_fmt_flags(item.get('flags') or [])} "
                f"| +{item['delta']:,} | {item['stars_after']:,} "
                f"| {item.get('language') or '—'} | {item.get('category') or '其他'} | {desc} |"
            )
        L.append("")

    if meta["new_repos"]:
        L.append("## 新项目榜（周期内创建，按当前总星排序）")
        L.append("")
        L.append("> 与上一节口径不同：这里衡量的是**新项目的起跑速度**，不是存量项目的增长。")
        L.append("")
        L.append("| 排名 | 项目 | 当前星数 | 创建日期 | 语言 | 分类 |")
        L.append("|---:|---|---:|---|---|---|")
        for i, item in enumerate(meta["new_repos"], 1):
            L.append(
                f"| {i} | [{item['full_name']}](https://github.com/{item['full_name']}) "
                f"| {item['stars']:,} | {item.get('repo_created') or '—'} "
                f"| {item.get('language') or '—'} | {item.get('category') or '其他'} |"
            )
        L.append("")

    if meta["breakdown"]:
        L.append("## 分类透视")
        L.append("")
        total = sum(c for _, c in meta["breakdown"]) or 1
        L.append("| 分类 | 数量 | 占比 |")
        L.append("|---|---:|---:|")
        for name, count in meta["breakdown"]:
            L.append(f"| {name} | {count} | {count / total:.0%} |")
        L.append("")

    L.append("## 数据质量")
    L.append("")
    for fact, details in _quality_facts(meta):
        L.append(f"- {fact}")
        for d in details:
            L.append(f"  - {d}")
    L.append("")
    L.append("---")
    L.append("")
    L.append("*本报告由 star-pulse 自动生成。所有数值来自每日快照相减，"
             "未经任何模型改写；分类与文字描述仅用于可读性，不影响排名。*")
    L.append("")
    return "\n".join(L)


# ── 数据质量：两条渲染路径的唯一事实来源 ────────────────────────
def _quality_facts(meta: dict) -> list[tuple[str, list[str]]]:
    """返回 [(结论, [明细...])]，Markdown 与 HTML 都从这里渲染。

    此前两条路径各写一遍，已经漂移：Markdown 列出了被剔除仓库的名字与跨度，
    HTML 只给了一个数量 —— 同一份报告在两种格式下能查到的信息不一样。
    强调用 `**...**` 标记，HTML 侧再转成 <b>。
    """
    cov = meta["coverage"]
    top = meta["top"]
    flagged = [i for i in top if i.get("flags")]

    facts: list[tuple[str, list[str]]] = [
        (
            f"快照覆盖 **{cov['distinct_days']} 天**"
            f"（{cov['first_day'] or '—'} ~ {cov['last_day'] or '—'}），"
            f"累计 {cov['rows_total']:,} 条记录",
            [],
        ),
        (f"增量时间跨度上限 **{meta['max_span_days']} 天**，超出即剔除", []),
    ]

    if meta["dropped_count"]:
        facts.append((
            f"因跨度超限被剔除 **{meta['dropped_count']}** 个仓库"
            f"（快照断档导致，非同口径比较）",
            [
                f"{d['full_name']}：跨度 {d['span_days']} 天，+{d['delta']:,}"
                f"（{d['base_date']} → {d['head_date']}）"
                for d in meta["dropped"][:5]
            ],
        ))
    else:
        facts.append(("无仓库因跨度超限被剔除", []))

    if flagged:
        facts.append((
            f"疑似刷星标记 **{len(flagged)}** 个（仅标注，未剔除）",
            [f"{i['full_name']}：{'、'.join(i['flags'])}" for i in flagged],
        ))
    else:
        facts.append(("未发现疑似刷星迹象", []))

    return facts


# ── HTML ────────────────────────────────────────────────────────
def _emph(text: str) -> str:
    """先转义，再把 `**x**` 变成 <b>x</b>。`*` 不在转义范围内，顺序安全。"""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc(text))


def _html(settings: Settings, label: str, start: str, end: str, meta: dict) -> str:
    cov = meta["coverage"]
    top = meta["top"]

    if top:
        rows = []
        for i, item in enumerate(top, 1):
            flags = item.get("flags") or []
            badge = (
                f' <span class="tag plain">⚠ {"、".join(esc(f) for f in flags)}</span>'
                if flags else ""
            )
            rows.append([
                (str(i), "rank"),
                theme.repo_link(item["full_name"]) + badge,
                (f'<span class="up">+{item["delta"]:,}</span>', "num"),
                (f'{item["stars_after"]:,}', "num"),
                esc(item.get("language") or "—"),
                f'<span class="tag plain">{esc(item.get("category") or "其他")}</span>',
                f'<span class="muted">{esc(_truncate(item.get("summary") or ""))}</span>',
            ])
        top_table = theme.table(
            ["#", "项目", ("本周新增", "num"), ("总星", "num"), "语言", "分类", "一句话"],
            rows, cls="data compact",
        )
    else:
        top_table = f'<p class="muted">{esc(meta["warmup_note"])}</p>'

    new_rows = [
        [
            (str(i), "rank"),
            theme.repo_link(r["full_name"]),
            (f'{r["stars"]:,}', "num"),
            esc(r.get("repo_created") or "—"),
            esc(r.get("language") or "—"),
            f'<span class="tag plain">{esc(r.get("category") or "其他")}</span>',
        ]
        for i, r in enumerate(meta["new_repos"], 1)
    ]
    new_table = (
        theme.table(["#", "项目", ("当前星数", "num"), "创建日期", "语言", "分类"],
                    new_rows, cls="data compact")
        if new_rows else '<p class="muted">本期无新项目记录。</p>'
    )

    breakdown_total = sum(c for _, c in meta["breakdown"]) or 1
    cat_rows = [
        [esc(name), (str(count), "num"), (f"{count / breakdown_total:.0%}", "num")]
        for name, count in meta["breakdown"]
    ]
    cat_table = (
        theme.table(["分类", ("数量", "num"), ("占比", "num")], cat_rows, cls="data compact")
        if cat_rows else '<p class="muted">—</p>'
    )

    # 数据质量：与 Markdown 同源，不再各自维护一份
    quality_items = []
    for fact, details in _quality_facts(meta):
        sub = (
            "<ul>" + "".join(f"<li>{esc(d)}</li>" for d in details) + "</ul>"
            if details else ""
        )
        quality_items.append(f"<li>{_emph(fact)}{sub}</li>")
    quality_html = "".join(quality_items)

    banner = ""
    if not meta["warm"]:
        banner = f'<div class="banner"><b>本期无增量榜</b>　{esc(meta["warmup_note"])}</div>'

    chart_block = ""
    if top:
        chart_block = (
            "<h2>增长分布</h2>"
            '<p class="note">Top 12 的本周新增 Star。横向条形，不需要任何脚本。</p>'
            + charts.hbar_chart([i["full_name"] for i in top[:12]],
                                [i["delta"] for i in top[:12]])
        )

    body = (
        f'<header class="topbar"><h1>GitHub 星耀榜 · {esc(label)}</h1>'
        f'<p class="note" style="margin:0 0 8px">统计周期 {esc(start)} ~ {esc(end)}'
        f'（UTC+{settings.tz_offset_hours}）　·　数据截至 {esc(str(cov["last_day"] or "—"))}'
        f'　·　快照覆盖 {cov["distinct_days"]} 天</p>'
        f'<div class="meta"><span><a href="../index.html">← 返回看板</a></span></div></header>'
        f"{banner}"
        f'<section class="panel"><div class="panel-head"><h2>本周新增 Star Top {len(top)}</h2></div>'
        f"{top_table}</section>"
        + (f'<section class="panel">{chart_block}</section>' if chart_block else "")
        + '<section class="panel"><div class="panel-head"><h2>新项目榜（周期内创建）</h2></div>'
          '<p class="note">口径不同：衡量新项目起跑速度，不是存量增长。</p>'
          f"{new_table}</section>"
          '<section class="panel"><div class="panel-head"><h2>分类透视</h2></div>'
          f"{cat_table}</section>"
          '<section class="panel"><div class="panel-head"><h2>数据质量</h2></div>'
          f'<ul class="quality">{quality_html}</ul></section>'
          "<footer>本报告由 star-pulse 自动生成。所有数值来自每日快照相减，未经任何模型改写；"
          "分类与文字描述仅用于可读性，不影响排名。</footer>"
    )

    return theme.page(f"GitHub 星耀榜 · {label}", body)
