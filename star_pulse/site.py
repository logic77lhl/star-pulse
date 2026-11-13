"""静态站点生成（GitHub Pages）。

产出 docs/：

  index.html      看板 —— 指标条 + 今日亮点 + 走势图 + 每天的情况 + 周报索引。
                  **零 JavaScript**：图表是服务端渲染的 SVG / CSS，所以停用 JS、
                  断网、CDN 挂掉，看到的内容都完全一样。
  projects.html   项目名单 —— 全量服务端渲染 + 搜索/筛选/排序（唯一带 JS 的页面，
                  但停用 JS 仍是一份完整可读的名单）。
  reports/*.html  历史周报归档
  data.json       汇总层结构化数据，供二次消费
  .nojekyll       阻止 Jekyll 处理（Pages 从分支部署时需要）

版面取向与旧版相反：**首屏必须直接给到价值**。旧版把项目名单和趋势全部收进两个
默认收起的 `<details>`，落地页只剩几张卡片和两根灰条，产品的内容一点都看不到。
现在的取舍是「先给摘要、再给下钻」：概览与今日亮点直接可见，完整 800 行名单
放到独立页 —— 既解决首屏空洞，也把首页从 505 KB 降到几十 KB
（此前 docs/index.html 一天一个新 blob，在 git 里累计占了 14.4 MB）。
"""

from __future__ import annotations

import html as html_mod
import json
import re
import shutil
import sqlite3
from collections import Counter
from pathlib import Path

from . import analyze, charts, i18n, narrate, theme
from .config import Settings
from .theme import esc, fmt_int, delta_html, delta_bar, kpi, repo_link, table, takeaway

# 周报文件名形如 2026-W40_2026-09-28_2026-10-04.html
_REPORT_RE = re.compile(r"^(\d{4})-W(\d{2})_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})$")


# ── 页面片段 ────────────────────────────────────────────────────
def _section_cap(no: str, title: str, note: str = "") -> str:
    """编号分区标题：给长页一个骨架，也充当前端锚点导航的落点。"""
    tail = f'<span class="cap-note">{esc(note)}</span>' if note else ""
    return (
        f'<div class="section-cap"><span class="no">{esc(no)}</span>'
        f"<h3>{esc(title)}</h3><span class=\"line\"></span>{tail}</div>"
    )


def _topbar(title: str, subtitle: str, meta: list[str], *, eyebrow: str = "",
            aside: str = "") -> str:
    """首页 Hero：eyebrow + 大标题 + 导语 + 元信息，右侧可带一段竖线旁白。

    旧版把「副标题」塞进 .note、元信息堆在一行，首屏没有钩子也没有层级。
    这里改成对齐索引站的陈述/旁白两栏：左边说这是什么，右边说为什么这么做。
    """
    eyebrow_html = f'<div class="eyebrow">{esc(eyebrow)}</div>' if eyebrow else ""
    metas = "".join(f"<span>{m}</span>" for m in meta)
    left = (
        f"{eyebrow_html}<h1>{title}</h1>"
        f'<p class="lede">{subtitle}</p>'
        f'<div class="meta">{metas}</div>'
    )
    right = f'<div class="hero-aside">{aside}</div>' if aside else ""
    hero_cls = "topbar hero two-col" if aside else "topbar hero"
    return f'<header class="{hero_cls}"><div class="hero-main">{left}</div>{right}</header>'


def _panel(title: str, body: str, *, note: str = "", more: str = "",
           conclusion: str = "") -> str:
    """面板。conclusion 是标题下面的结论句（由 narrate.py 算好）。"""
    head = f'<div class="panel-head"><h2>{esc(title)}</h2>{more}</div>'
    concl_html = conclusion  # 已经是渲染好的 HTML
    note_html = f'<p class="note">{note}</p>' if note else ""
    return f'<section class="panel">{head}{concl_html}{note_html}{body}</section>'


def _report_cards(reports_out: Path) -> str:
    """历史周报列表。旧版直接列出原始文件名（2026-W40_2026-09-28_2026-10-04），
    这里解析成「第 N 周 + 起止日期」。"""
    files = sorted(reports_out.glob("*.html"), reverse=True)
    if not files:
        return '<p class="muted">还没有生成过周报。每周一由定时任务自动生成。</p>'
    items = []
    for path in files:
        m = _REPORT_RE.match(path.stem)
        if m:
            year, week, start, end = m.groups()
            wk = f"{year} 年第 {int(week)} 周"
            rg = f"{start[5:]} ~ {end[5:]}" if start != end else f"{start}（单日）"
        else:
            wk, rg = path.stem, ""
        items.append(
            f'<li><a href="reports/{esc(path.name)}">'
            f'<span class="wk">{esc(wk)}</span>'
            f'<span class="rg">{esc(rg)}</span></a></li>'
        )
    return '<ul class="reports">' + "".join(items) + "</ul>"


def _lang_bars(langs: list, limit: int = 8) -> str:
    """首页「语言构成」小图。数据是当日快照里各语言的仓库计数，纯 CSS。"""
    if not langs:
        return '<p class="muted">暂无语言数据。</p>'
    rows = langs[:limit]
    hi = max((r["n"] for r in rows), default=1) or 1
    items = "".join(
        f'<div class="langrow"><span class="n">{esc(str(r["lang"]))}</span>'
        f'<span class="t"><i style="width:{r["n"] / hi * 100:.0f}%"></i></span>'
        f'<span class="c">{r["n"]:,}</span></div>'
        for r in rows
    )
    return f'<div class="langbars">{items}</div>'


def _daily_table(summary: list[dict]) -> str:
    """「每天的情况」。最新在上，并给「新增星数合计」加一条强度条。"""
    if not summary:
        return '<p class="muted">还没有任何快照。</p>'
    peak = max((s["total_gain"] or 0) for s in summary) or 1
    rows = []
    for item in reversed(summary):
        date = f'<b>{esc(item["date"])}</b>'
        if item["is_first"]:
            rows.append([date, (fmt_int(item["repos"]), "num"), ("—", "num"),
                         ("—", "num"), '<span class="muted">首日基线</span>', ("—", "num")])
            continue
        gain = item["total_gain"]
        gain_cell = (
            f'<span class="up">+{gain:,}</span>{delta_bar(gain, peak)}'
        )
        top = (
            f'{repo_link(item["top_name"])} <span class="up">+{item["top_delta"]:,}</span>'
            if item["top_name"] else '<span class="muted">无上涨</span>'
        )
        rows.append([
            date,
            (fmt_int(item["repos"]), "num"),
            (gain_cell, "num"),
            (fmt_int(item["gainers"]), "num"),
            top,
            (fmt_int(item["new_repos"]), "num"),
        ])
    return table(
        ["日期", ("入库仓库", "num"), ("新增星数合计", "num"),
         ("上涨仓库", "num"), "当日涨幅冠军", ("新进候选", "num")],
        rows, cls="data compact",
    )


def _gain_table(rows: list[dict], peak: int) -> str:
    """涨幅榜。增量列带强度条，让 +11,059 和 +29 一眼可分。"""
    if not rows:
        return '<p class="muted">窗口内没有上涨的仓库。</p>'
    body = []
    for i, r in enumerate(rows, 1):
        delta = r["delta"]
        body.append([
            (str(i), "rank"),
            repo_link(r["full_name"]),
            (f'<span class="up">+{delta:,}</span>{delta_bar(delta, peak)}', "num"),
            (fmt_int(r.get("stars") or r.get("stars_after")), "num"),
            esc(r.get("language") or "—"),
        ])
    return table(
        ["#", "项目", ("新增", "num"), ("当前星数", "num"), "语言"],
        body, cls="data compact",
    )


def _project_table(rows: list[dict], zh: dict[str, dict]) -> str:
    """项目名单全量表格（服务端渲染，不截断）。

    三个刻意的取舍（都是被真实的仓库膨胀问题逼出来的）：

      * 简介以**中文译文**为正文，英文原文不内联，只留跳去 GitHub 的链接。
        把原文再塞进 title 属性会给每行多加约 85 字符，800 行就是 68 KB。
      * 每行一个换行。整张表挤成一行会让 git 完全没法做增量压缩。
      * 不再输出 data-rid —— 它曾经占 16.4 KB，而 JS 从未引用过。
    """
    if not rows:
        return '<p class="muted">暂无数据。</p>'
    lines = []
    for i, r in enumerate(rows, 1):
        entry = zh.get(str(r["repo_id"])) or {}
        zh_text = (entry.get("zh") or "").strip()
        desc = " ".join((r.get("description") or "").split())
        short = desc[:80] + ("…" if len(desc) > 80 else "")
        text = zh_text or short
        desc_cell = esc(text) if text else '<span class="muted">—</span>'
        cat = analyze.classify(r)
        lines.append(
            f'<tr data-name="{esc(r["full_name"].lower())}" '
            f'data-cat="{esc(cat)}" '
            f'data-lang="{esc(r.get("language") or "")}" '
            f'data-stars="{r["stars"]}" data-forks="{r["forks"]}" '
            f'data-delta="{r["delta"]}" data-created="{esc(r.get("repo_created") or "")}">'
            f'<td class="rank">{i}</td>'
            f'<td>{repo_link(r["full_name"])}</td>'
            f'<td><span class="tag plain">{esc(cat)}</span></td>'
            f'<td>{esc(r.get("language") or "—")}</td>'
            f'<td class="num">{r["stars"]:,}</td>'
            f'<td class="num">{r["forks"]:,}</td>'
            f'<td class="num">{delta_html(r["delta"], has_prev=bool(r["has_prev"]))}</td>'
            f'<td class="desc">{desc_cell}</td></tr>'
        )
    return table(
        ["#", "项目", "类目", "语言", ("星数", "num"), ("Fork", "num"),
         ("当日新增", "num"), "简介"],
        cls="data", table_id="projTable", raw_rows="\n".join(lines),
    )


_PROJECTS_JS = """
<script>
/* 项目名单：搜索 / 类目与语言筛选 / 排序。纯 DOM 操作，不依赖任何库。
   行是全量服务端渲染的，「每屏条数」只控制显示（hidden），所以搜索、筛选、
   排序覆盖的始终是全量，停用 JS 也仍是一份完整可读的名单。 */
(function () {
  var tbl = document.getElementById('projTable');
  if (!tbl || !tbl.tBodies.length) return;
  var tb = tbl.tBodies[0];
  var rows = Array.prototype.slice.call(tb.rows);
  var q = document.getElementById('projQ');
  var catSel = document.getElementById('projCat');
  var langSel = document.getElementById('projLang');
  var sortSel = document.getElementById('projSort');
  var limitSel = document.getElementById('projLimit');
  var out = document.getElementById('projCount');
  var low = function (s) { return (s || '').toLowerCase(); };
  var num = function (tr, k) { return Number(tr.dataset[k]) || 0; };

  // 预先拼好可搜索文本。用 td.desc 而不是写死的列号 —— 列顺序改过好几次，
  // 写死索引是最容易在下次加列时静默失配的写法。
  var hay = new Map();
  rows.forEach(function (tr) {
    var cell = tr.querySelector('td.desc');
    hay.set(tr, low(tr.dataset.name + ' ' + tr.dataset.cat + ' '
                    + (cell ? cell.textContent : '')));
  });

  function rank() {
    var n = 0;
    rows.forEach(function (tr) { if (!tr.hidden) tr.cells[0].textContent = String(++n); });
  }
  function view() {
    var needle = q.value.trim().toLowerCase();
    var cat = catSel.value, lg = langSel.value;
    var lim = (!limitSel || limitSel.value === 'all') ? Infinity : Number(limitSel.value);
    var matched = 0, shown = 0;
    rows.forEach(function (tr) {
      var hit = (!needle || hay.get(tr).indexOf(needle) !== -1)
             && (!cat || tr.dataset.cat === cat)
             && (!lg || tr.dataset.lang === lg);
      if (hit) matched++;
      var vis = hit && shown < lim;
      if (vis) shown++;
      tr.hidden = !vis;
    });
    out.textContent = '显示 ' + shown.toLocaleString('en-US')
                    + ' / ' + rows.length.toLocaleString('en-US') + ' 个'
                    + (matched > shown
                       ? '（另有 ' + (matched - shown).toLocaleString('en-US') + ' 个符合条件）'
                       : '');
    rank();
  }
  function reorder() {
    var parts = sortSel.value.split(':');
    var key = parts[0], s = parts[1] === 'asc' ? 1 : -1;
    var sorted = rows.slice().sort(function (a, b) {
      if (key === 'name') return s * a.dataset.name.localeCompare(b.dataset.name);
      if (key === 'created') return s * (a.dataset.created || '').localeCompare(b.dataset.created || '');
      return s * (num(a, key) - num(b, key));
    });
    var frag = document.createDocumentFragment();
    sorted.forEach(function (tr) { frag.appendChild(tr); });
    tb.appendChild(frag);
    rank();
  }
  q.addEventListener('input', view);
  catSel.addEventListener('change', view);
  langSel.addEventListener('change', view);
  sortSel.addEventListener('change', function () { reorder(); view(); });
  if (limitSel) limitSel.addEventListener('change', view);
  view();
})();
</script>
"""


def _projects_page(
    latest: str, projects: list[dict], zh: dict, langs: list, cats: Counter,
    translated: int,
) -> str:
    lang_options = "".join(
        f'<option value="{esc(r["lang"])}">{esc(r["lang"])}（{r["n"]:,}）</option>'
        for r in langs
    )
    cat_options = "".join(
        f'<option value="{esc(name)}">{esc(name)}（{n:,}）</option>'
        for name, n in cats.most_common()
    )
    toolbar = (
        '<div class="toolbar">'
        '<input id="projQ" type="search" autocomplete="off" '
        'placeholder="搜索项目名、类目或简介…" aria-label="搜索项目">'
        f'<select id="projCat" aria-label="按类目筛选"><option value="">全部类目</option>'
        f"{cat_options}</select>"
        f'<select id="projLang" aria-label="按语言筛选"><option value="">全部语言</option>'
        f"{lang_options}</select>"
        '<select id="projSort" aria-label="排序方式">'
        '<option value="stars:desc">星数 从高到低</option>'
        '<option value="stars:asc">星数 从低到高</option>'
        '<option value="delta:desc">当日新增 从高到低</option>'
        '<option value="forks:desc">Fork 从高到低</option>'
        '<option value="created:desc">创建时间 最新</option>'
        '<option value="name:asc">名称 A→Z</option>'
        "</select>"
        '<select id="projLimit" aria-label="显示条数">'
        '<option value="50">每屏 50 条</option>'
        '<option value="100" selected>每屏 100 条</option>'
        '<option value="300">每屏 300 条</option>'
        '<option value="all">全部</option>'
        "</select>"
        f'<span class="count" id="projCount">共 {len(projects):,} 个</span>'
        "</div>"
    )
    body = (
        _topbar(
            "项目名单",
            "按星数排序的全部追踪仓库。可搜索、按类目或语言筛选、按多种维度排序。",
            [f'<a href="index.html">← 返回看板</a>',
             f"数据日期 <b>{esc(latest)}</b>",
             f"共 <b>{len(projects):,}</b> 个"],
        )
        + _panel(
            f"共 {len(projects):,} 个追踪仓库",
            toolbar + _project_table(projects, zh),
            note=(
                f"「类目」由关键词规则判定，确定性可复现；「简介」是<b>机翻</b>中文"
                f"（机器翻译，每周自动增量补齐，已译 {translated:,}/{len(projects):,} 条）——"
                f"想看英文原文请点项目名去 GitHub，未译到的行显示英文原文。<br>"
                f"「当日新增」是相对上一次快照的净增，没有对比基线时显示「—」。"
                f"类目与简介都只影响可读性，不参与任何数值计算。"
            ),
            more=f'<span class="more muted">共 {len(projects):,} 个</span>',
        )
        + '<footer>由 <a href="https://github.com/logic77lhl/star-pulse">star-pulse</a> 自动生成。'
          '所有数值来自每日快照相减，未经任何模型改写。</footer>'
    )
    return theme.page(f"项目名单 · star-pulse（{latest}）", body, script=_PROJECTS_JS)


def build_site(settings: Settings, conn: sqlite3.Connection) -> dict:
    """生成 docs/ 静态站点。返回生成摘要。"""
    docs = settings.root / "docs"
    reports_out = docs / "reports"
    docs.mkdir(parents=True, exist_ok=True)
    reports_out.mkdir(parents=True, exist_ok=True)

    dates = analyze.all_snapshot_dates(conn)
    summary = analyze.daily_summary(conn)
    latest = dates[-1] if dates else None
    first = dates[0] if dates else None

    # 口径与「项目名单」一致（JOIN repo + 过滤 fork/archived），否则卡片与名单对不上。
    tracked = conn.execute(
        """SELECT COUNT(DISTINCT s.repo_id) c FROM snapshot s
           JOIN repo r ON r.repo_id = s.repo_id
           WHERE r.is_fork = 0 AND r.is_archived = 0"""
    ).fetchone()["c"]

    prev = dates[-2] if len(dates) >= 2 else None
    projects = analyze.project_rows(conn, latest, prev) if latest else []
    zh = i18n.load_cache(settings)
    translated = sum(1 for r in projects if (zh.get(str(r["repo_id"])) or {}).get("zh"))
    cats = Counter(analyze.classify(r) for r in projects)

    langs = conn.execute(
        """
        SELECT r.language AS lang, COUNT(*) AS n
        FROM snapshot s JOIN repo r ON r.repo_id = s.repo_id
        WHERE s.snap_date = :day AND r.is_fork = 0 AND r.is_archived = 0
          AND r.language IS NOT NULL AND r.language <> ''
        GROUP BY r.language ORDER BY n DESC, r.language
        """,
        {"day": latest or ""},
    ).fetchall()

    # ── 指标条 ──
    core = [s for s in summary if not s["is_first"]]
    today = core[-1] if core else None
    yesterday = core[-2] if len(core) >= 2 else None
    today_gain = today["total_gain"] if today else 0
    if yesterday and yesterday["total_gain"]:
        pct = (today_gain - yesterday["total_gain"]) / yesterday["total_gain"] * 100
        wow = f'环比 <span class="{"up" if pct >= 0 else "down"}">{pct:+.1f}%</span>'
    else:
        wow = "首日基线，暂无环比"
    gainers = today["gainers"] if today else 0
    share = f"占追踪项目 {gainers / len(projects) * 100:.0f}%" if projects else ""
    spark = charts.sparkline([s["total_gain"] for s in core]) if len(core) >= 2 else ""

    kpis = (
        kpi("今日新增星数", f"+{today_gain:,}", detail=wow, spark=spark, primary=True)
        + kpi("上涨项目", fmt_int(gainers), "个", detail=share)
        + kpi("追踪项目", fmt_int(len(projects)), "个", detail=f"数据日期 {latest or '—'}")
        + kpi("累计追踪", fmt_int(tracked), "个", detail=f"覆盖 {len(dates)} 天")
        + kpi("覆盖语言", fmt_int(len(langs)), "种",
              detail=f"最多 {langs[0]['lang']}（{langs[0]['n']:,}）" if langs else "")
    )

    banner = ""
    if len(dates) < 8:
        banner = (
            '<div class="banner"><b>预热中</b>　已积累 '
            f"{len(dates)} 天快照，还需约 {8 - len(dates)} 天才能形成完整的 7 天窗口。"
            "GitHub 没有星数增量的官方接口，只能靠每日快照累积 —— 这是设计使然，不是故障。</div>"
        )

    # ── 今日亮点：当日涨幅榜 ──
    daily_top = (
        analyze.daily_gain(conn, latest, dates[-2], limit=10) if len(dates) >= 2 else []
    )
    daily_peak = max((r["delta"] for r in daily_top), default=1)
    daily_block = (
        _gain_table(daily_top, daily_peak)
        if daily_top
        else '<p class="muted">只有一天数据，还无法计算单日涨幅。</p>'
    )

    # ── 累计增长榜：全历史窗口，只比「两端都有快照」的仓库 ──
    # 不能用 weekly_gain —— 它的 max_span_days 跨度过滤恰好会剔除所有从第一天
    # 追踪到现在的仓库（跨度 == 全窗口），把真正涨得最多的全部滤掉。
    cumulative: list[dict] = []
    comparable = 0
    if len(dates) >= 2:
        ranked, comparable = analyze.range_gain(conn, first, latest, limit=10)
        cumulative = [
            {"full_name": r["full_name"], "delta": r["delta"],
             "stars": r["stars_after"], "language": r.get("language")}
            for r in ranked
        ]
    cum_peak = max((r["delta"] for r in cumulative), default=1)
    cumulative_block = (
        _gain_table(cumulative, cum_peak)
        if cumulative
        else '<p class="muted">只有一天数据，还无法计算累计增长。</p>'
    )

    # ── 结论层：一次算出全部「一句话答案」，同时作为未来周报口播的事实来源 ──
    narration = narrate.build_narration(
        conn, dates, len(projects), daily_top, cumulative, comparable, langs
    )
    tk = narration.get("takeaways", {})

    def _concl(key: str, label: str = "结论") -> str:
        item = tk.get(key)
        if not item:
            return ""
        return takeaway(esc(item["text"]), key=key, label=label)

    # ── 视图：横向条榜（替代表格与那张 8 条折线） ──
    daily_block = (
        charts.leaderboard(daily_top, limit=10)
        if daily_top else '<p class="muted">只有一天数据，还无法计算单日涨幅。</p>'
    )
    cumulative_block = (
        charts.leaderboard(cumulative, limit=10)
        if cumulative else '<p class="muted">只有一天数据，还无法计算累计增长。</p>'
    )

    # 每日新增走势保留（它回答「今天热不热闹」），但顶部先给结论
    bars = charts.css_bars([s["date"] for s in core], [s["total_gain"] for s in core])
    gains = [s["total_gain"] for s in core]
    peak_val = max(gains, default=0)
    avg_val = round(sum(gains) / len(gains)) if gains else 0
    bars_legend = (
        '<div class="chart-legend">'
        f"<span>峰值 <b>+{peak_val:,}</b></span>"
        f"<span>均值 <b>+{avg_val:,}</b></span>"
        f"<span>最新 <b>+{today_gain:,}</b></span></div>"
    )

    # ── 页脚 / 周报 ──
    reports_block = _report_cards(reports_out)

    sitenav = (
        '<nav class="sitenav">'
        '<a class="brand" href="index.html"><span class="mark">L</span>'
        '<span>logic77lhl <small>BUILD · SHIP · NOTE</small></span></a>'
        '<div class="links">'
        '<a href="#daily">当日涨幅</a><a href="#cumulative">累计增长</a>'
        '<a href="#trend">走势</a><a href="#method">口径</a>'
        '<a href="#reports">周报</a><a href="projects.html">项目名单</a>'
        "</div>"
        '<a class="src" href="https://github.com/logic77lhl/star-pulse">GitHub ↗</a>'
        "</nav>"
    )

    aside = (
        "<p>增量是个容易骗人的东西：口径不透明，也说不清两个数字之间隔了多久。</p>"
        "<p>这里换了一条更笨的路 —— 只认自己拍下来的快照。宁可自己拍两张照片，"
        "也不相信别人算好的差值。</p>"
        '<p class="q">凡是「增量」，先问基数是什么。</p>'
    )

    body = (
        sitenav
        + _topbar(
            '每天给 800 个项目<br><span class="brand">拍一张星数快照。</span>',
            "不用任何人的「增量」字段。每天 04:00 自己拍一次，用两个真实存在的日期相减 —— "
            "涨了多少，只取决于两张照片。",
            [f'<span class="pill">◷ 数据截至 <b>{esc(str(latest or "—"))}</b></span>',
             f'<span class="pill">◈ 已积累 <b>{len(dates)}</b> 天</span>',
             f'<span class="pill">◉ 追踪 <b>{len(projects):,}</b> 个</span>',
             f'<span class="pill">◍ 覆盖 <b>{len(langs)}</b> 种语言</span>',
             f'<span class="pill">↻ UTC+{settings.tz_offset_hours} · 04:00</span>'],
            eyebrow=f"STAR-PULSE · {first or '—'} — {latest or '—'} · {len(dates)} DAYS",
            aside=aside,
        )
        # 首屏第一句就是今天的总判断，不是一堆等权卡片
        + _concl("today", "今天怎么样")
        + f'<div class="kpis">{kpis}</div>'
        + banner
        # 01 · 两个榜并排，各自先给一句答案
        + _section_cap("01", "今天谁在涨", f"窗口 {dates[-2]} → {latest}" if len(dates) >= 2 else "")
        + '<div class="grid-2" id="daily">'
        + _panel(
            "当日涨幅榜",
            daily_block,
            conclusion=_concl("daily_top"),
            note=f"窗口 {dates[-2]} → {latest}。只比较相邻两个快照日，两个日期都真实存在，口径最干净。"
            if len(dates) >= 2 else "",
            more=f'<a class="more" href="projects.html">查看全部 {len(projects):,} 个项目 →</a>',
        )
        + _panel(
            "累计增长榜",
            cumulative_block,
            conclusion=_concl("cumulative_top"),
            note=(
                f"窗口 {first} → {latest}。只统计<b>两端都有快照</b>的 {comparable:,} 个仓库 ——"
                f"窗口对所有人一致才可比；中途进池的仓库不参与，避免把 3 天的增量和 26 天的增量放在一起排名。"
            ) if len(dates) >= 2 else "",
        )
        + "</div>"
        # 02 · 走势 + 语言构成，各自一句答案
        + _section_cap("02", "走势与构成", f"{len(dates)} 天 · 服务端渲染")
        + '<div class="grid-2" id="trend">'
        + _panel(
            "每日新增走势",
            bars + bars_legend,
            conclusion=_concl("breadth"),
            note="当天全部追踪仓库的新增星数合计。首日没有基线，不参与。",
        )
        + _panel(
            "语言构成 · Top 8",
            _lang_bars(langs),
            conclusion=_concl("languages"),
            note=f"按当日快照中有语言的仓库计数，共 {len(langs)} 种。",
        )
        + "</div>"
        # 03 · 口径说明（用真实机制撑住，不写功能清单）
        + '<div id="method">' + _section_cap("03", "这份数据怎么来的") + "</div>"
        + _panel(
            "四路候选池 → 每天一张 → 两张照片相减",
            '<div class="method">'
            '<div class="m"><div class="t"><i>1</i> 四路候选池</div>'
            "<p>种子清单、Trending 解析、池内续期、Search 补位，凑出当天要看的名单。</p></div>"
            '<div class="m"><div class="t"><i>2</i> 每天拍一张</div>'
            "<p>04:00 UTC 调 GitHub API 拿当前 star，和库里上一条快照相减，写入数据文件。</p></div>"
            '<div class="m"><div class="t"><i>3</i> 看两张照片的差</div>'
            "<p>看板读的是两张快照的差，不是任何人的「增量」字段。整条链路只用 Python 标准库。</p></div>"
            "</div>",
        )
        # 04 · 逐日明细（不折叠：回归测试规定看板首屏必须直接可见）
        + '<div id="history">' + _section_cap("04", "逐日明细与周报") + "</div>"
        + _panel("每天的情况", _daily_table(summary),
                 note="「新增星数合计」是当天全部追踪仓库的星数净增之和；首日没有对比基线。")
        + '<div id="reports">' + _panel("历史周报", reports_block) + "</div>"
        + '<footer>由 <a href="https://github.com/logic77lhl/star-pulse">star-pulse</a> 自动生成。'
          '所有数值来自每日快照相减，未经任何模型改写。'
          '本页图表为服务端渲染的 SVG / CSS，不含任何外部脚本。</footer>'
    )

    (docs / "index.html").write_text(
        theme.page(f"star-pulse · GitHub 星耀榜（{latest or '—'}）", body), encoding="utf-8"
    )
    (docs / "projects.html").write_text(
        _projects_page(str(latest or "—"), projects, zh, langs, cats, translated),
        encoding="utf-8",
    )

    # 归档周报
    copied = 0
    if settings.reports_dir.is_dir():
        for src in settings.reports_dir.glob("*.html"):
            shutil.copy2(src, reports_out / src.name)
            copied += 1

    # 只放汇总层数据：不内联逐条明细，那是 data/snapshots/*.json 的职责。
    (docs / "data.json").write_text(
        json.dumps(
            {
                "generated_at": settings.now_local().isoformat(timespec="seconds"),
                "tz_offset_hours": settings.tz_offset_hours,
                "snapshot_dates": dates,
                "tracked_repos": tracked,
                "latest_date": latest,
                "daily_summary": summary,
                "cumulative_top": cumulative,
                "cumulative_comparable": comparable,
                "daily_top": daily_top,
                # 结论层：页面文案、周报口播、视频字幕共用的唯一事实来源。
                # spoken_order 给出串讲顺序，text 可直接朗读，facts 是可机读的数字。
                "narration": narration,
                "raw_snapshots": "data/snapshots/YYYY-MM-DD.json",
                "reports": [p.name for p in sorted(reports_out.glob("*.html"), reverse=True)],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    (docs / ".nojekyll").write_text("", encoding="utf-8")

    return {
        "docs": str(docs),
        "snapshot_days": len(dates),
        "tracked_repos": tracked,
        "reports_archived": copied,
        "index": str(docs / "index.html"),
        "projects": str(docs / "projects.html"),
    }
