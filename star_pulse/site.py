"""静态站点生成（GitHub Pages）。

产出 docs/ 目录，作为「每天的情况」看板：
  docs/index.html          看板：项目名单（主角）+ 折叠的趋势与榜单 + 报告索引
  docs/reports/*.html      历史周报归档
  docs/data.json           汇总层结构化数据，供二次消费
  docs/.nojekyll           阻止 Jekyll 处理（Pages 从分支部署时需要）

版面取向：**项目名单排在最前**，趋势图和榜单收进 `<details>` 默认折叠。
理由是这个看板的主用途是「看有哪些项目、各自什么情况」，趋势只是佐证。

项目名单是**服务端全量渲染**的（不截断），再叠一层纯 DOM 的搜索/筛选/排序；
所以停用 JS 也仍是一份完整可读的名单。图表则相反 —— 折叠区里的 canvas
在展开前量不到尺寸，必须等 `toggle` 事件里再创建，否则会按 0×0 画出空白。
"""

from __future__ import annotations

import html as html_mod
import json
import shutil
import sqlite3
from collections import Counter
from pathlib import Path

from . import analyze, i18n
from .config import Settings

esc = html_mod.escape

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin:0; padding:36px 18px 60px; font-family: system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
  background:#f7f7f5; color:#22211f; line-height:1.6; }
.wrap { max-width:1120px; margin:0 auto; }
h1 { font-size:27px; font-weight:600; margin:0 0 6px; letter-spacing:-.015em; }
h2 { font-size:17px; font-weight:600; margin:44px 0 6px; }
h2:first-of-type { margin-top:34px; }
.sub { color:#6b6a66; font-size:14px; margin:0 0 6px; }
.note { color:#888780; font-size:13px; margin:0 0 14px; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin:24px 0 0; }
.card { background:#fff; border:1px solid #e3e1da; border-radius:10px; padding:14px 16px; }
.card .k { font-size:13px; color:#888780; margin:0 0 4px; }
.card .v { font-size:24px; font-weight:600; margin:0; font-variant-numeric:tabular-nums; letter-spacing:-.02em; }
.card .u { font-size:13px; font-weight:400; color:#888780; }
table { width:100%; border-collapse:separate; border-spacing:0; font-size:14px; background:#fff;
  border:1px solid #e3e1da; border-radius:10px; }
th { text-align:left; padding:10px 13px; background:#f1efe8; font-weight:600; font-size:13px;
  color:#444441; white-space:nowrap; position:sticky; top:0; z-index:2;
  box-shadow:inset 0 -1px 0 #e3e1da; }
th:first-child { border-top-left-radius:10px; }
th:last-child { border-top-right-radius:10px; }
tr:last-child td:first-child { border-bottom-left-radius:10px; }
tr:last-child td:last-child { border-bottom-right-radius:10px; }
td { padding:10px 13px; border-top:1px solid #eeece6; vertical-align:top; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
td.num { font-weight:600; }
tr.first td { color:#888780; }
a { color:#534AB7; text-decoration:none; }
a:hover { text-decoration:underline; }
/* 项目名要一眼看出可点：常态就带下划线，再挂一个 ↗ 角标 */
a.repo { text-decoration:underline; text-decoration-thickness:1px;
  text-decoration-color:rgba(83,74,183,.38); text-underline-offset:2.5px; }
a.repo:hover { text-decoration-color:currentColor; }
a.repo .ext { font-size:.82em; margin-left:3px; opacity:.5; }
td.cat { white-space:nowrap; color:#6b6a66; }
.up { color:#A32D2D; font-weight:600; }
.rank { color:#888780; font-variant-numeric:tabular-nums; }
.banner { background:#FAEEDA; border:1px solid #EF9F27; color:#633806;
  padding:13px 17px; border-radius:10px; margin:20px 0; font-size:14px; }
.chart { position:relative; height:340px; background:#fff; border:1px solid #e3e1da;
  border-radius:10px; padding:16px; margin-top:14px; }
.muted { color:#888780; font-size:14px; }
ul.reports { background:#fff; border:1px solid #e3e1da; border-radius:10px;
  padding:8px 18px 8px 34px; font-size:14px; margin:0; }
ul.reports li { margin:9px 0; }
.down { color:#1D9E75; font-weight:600; }
td.desc { color:#6b6a66; font-size:13px; max-width:430px; }
.toolbar { display:flex; flex-wrap:wrap; gap:9px; align-items:center; margin:14px 0 12px; }
.toolbar input, .toolbar select { font:inherit; font-size:14px; padding:8px 11px;
  border:1px solid #e3e1da; border-radius:8px; background:#fff; color:inherit; }
.toolbar input { flex:1 1 230px; min-width:170px; }
.toolbar select { flex:0 0 auto; }
.toolbar .count { color:#888780; font-size:13px; margin-left:auto; white-space:nowrap; }
details.trends { margin:34px 0 0; }
details.trends > summary { cursor:pointer; list-style:none; user-select:none;
  background:#fff; border:1px solid #e3e1da; border-radius:10px; padding:14px 18px;
  font-size:16px; font-weight:600; display:flex; align-items:center; gap:9px; }
details.trends > summary::-webkit-details-marker { display:none; }
details.trends > summary::before { content:"▸"; color:#888780; font-weight:400;
  display:inline-block; transition:transform .15s ease; }
details.trends[open] > summary::before { transform:rotate(90deg); }
details.trends > summary:hover { border-color:#c9c6bc; }
details.trends > summary .hint { font-weight:400; font-size:13px; color:#888780; }
details.trends .tbody h2 { margin-top:34px; }
footer { margin-top:46px; padding-top:16px; border-top:1px solid #e3e1da; color:#888780; font-size:13px; }
@media (prefers-color-scheme: dark) {
  body { background:#1b1b19; color:#e8e6e1; }
  .card, table, .chart, ul.reports, details.trends > summary { background:#262624; border-color:#3a3a37; }
  th { background:#302f2c; color:#d3d1c7; box-shadow:inset 0 -1px 0 #3a3a37; }
  td { border-top-color:#3a3a37; }
  a { color:#AFA9EC; }
  a.repo { text-decoration-color:rgba(175,169,236,.42); }
  td.cat { color:#9b9993; }
  h1, h2 { color:#eeecea; }
  .sub, .note, .muted, footer, .card .k, .card .u, .rank, .toolbar .count,
  details.trends > summary .hint { color:#9b9993; }
  .banner { background:#412402; border-color:#854F0B; color:#FAC775; }
  .up { color:#F09595; }
  .down { color:#5DCAA5; }
  td.desc { color:#a8a69f; }
  .toolbar input, .toolbar select { background:#262624; border-color:#3a3a37; color:#e8e6e1; }
  details.trends > summary:hover { border-color:#4d4d49; }
  details.trends > summary::before { color:#9b9993; }
}
@media (max-width:640px){ body{padding:22px 12px 40px} h1{font-size:22px} }
"""


def _page(title: str, body: str, script: str = "") -> str:
    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{esc(title)}</title>\n<style>{_CSS}</style>\n</head>\n<body>\n"
        f'<div class="wrap">\n{body}\n</div>\n{script}\n</body>\n</html>\n'
    )


def _card(key: str, value: str, unit: str = "") -> str:
    u = f'<span class="u">{esc(unit)}</span>' if unit else ""
    return f'<div class="card"><p class="k">{esc(key)}</p><p class="v">{value}{u}</p></div>'


def _repo_link(full_name: str) -> str:
    """仓库链接。

    显式加下划线 + ↗ 角标：项目名本来就是 `<a>`，但不做视觉区分时读者根本
    认不出可以点（这是被真实反馈过的问题）。
    """
    return (
        f'<a class="repo" href="https://github.com/{esc(full_name)}" target="_blank" '
        f'rel="noopener">{esc(full_name)}<span class="ext" aria-hidden="true">↗</span></a>'
    )


def _daily_table(summary: list[dict]) -> str:
    if not summary:
        return '<p class="muted">还没有任何快照。先运行 <code>python -m star_pulse run-daily</code>。</p>'
    rows = []
    for item in reversed(summary):  # 最新在上
        if item["is_first"]:
            cells = (
                f'<td class="num">{item["repos"]}</td>'
                '<td class="num">—</td><td class="num">—</td>'
                '<td class="muted">首日基线，无对比</td><td class="num">—</td>'
            )
            rows.append(f'<tr class="first"><td><b>{esc(item["date"])}</b></td>{cells}</tr>')
            continue
        top = (
            f'{_repo_link(item["top_name"])} <span class="up">+{item["top_delta"]:,}</span>'
            if item["top_name"]
            else '<span class="muted">无上涨</span>'
        )
        rows.append(
            f"<tr><td><b>{esc(item['date'])}</b></td>"
            f'<td class="num">{item["repos"]:,}</td>'
            f'<td class="num">{item["total_gain"]:,}</td>'
            f'<td class="num">{item["gainers"]:,}</td>'
            f"<td>{top}</td>"
            f'<td class="num">{item["new_repos"]:,}</td></tr>'
        )
    return (
        "<table><thead><tr>"
        "<th>日期</th><th class=\"num\">入库仓库</th><th class=\"num\">新增星数合计</th>"
        "<th class=\"num\">上涨仓库数</th><th>当日涨幅冠军</th><th class=\"num\">新进候选</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _gain_table(rows: list[dict], window: str, label: str) -> str:
    if not rows:
        return f'<p class="muted">{esc(label)}：窗口内没有上涨的仓库。</p>'
    body = []
    for i, r in enumerate(rows, 1):
        body.append(
            f'<tr><td class="rank">{i}</td><td>{_repo_link(r["full_name"])}</td>'
            f'<td class="num up">+{r["delta"]:,}</td>'
            f'<td class="num">{r["stars"]:,}</td>'
            f'<td>{esc(r.get("language") or "—")}</td></tr>'
        )
    return (
        f'<p class="note">{esc(label)}　窗口：{esc(window)}</p>'
        "<table><thead><tr><th>#</th><th>项目</th><th class=\"num\">新增</th>"
        "<th class=\"num\">当前星数</th><th>语言</th></tr></thead><tbody>"
        + "".join(body) + "</tbody></table>"
    )


def _fmt_delta(delta: int, has_prev: bool) -> str:
    """增量单元格。首日没有对比基线时给「—」而不是 0，避免被误读成「真的没涨」。"""
    if not has_prev:
        return '<span class="muted">—</span>'
    if delta > 0:
        return f'<span class="up">+{delta:,}</span>'
    if delta < 0:
        return f'<span class="down">{delta:,}</span>'
    return '<span class="muted">0</span>'


def _project_table(rows: list[dict], zh: dict[str, dict]) -> str:
    """「项目本身」名单：全量仓库，可搜索 / 筛选 / 排序。

    全部行都服务端渲染（不截断），再叠加一层纯 DOM 的搜索与排序 —— 这样
    即使停用 JS 也仍是一张完整可读的名单，而不是一片空白。

    三个刻意的取舍：
      * 简介以**中文译文**为正文，英文原文不内联，只留跳去 GitHub 的链接。
        把原文再塞进 title 属性会给每行多加约 85 字符，800 行就是 68 KB ——
        正是这类「顺手多存一份」把页面从 400 KB 撑到 500 KB 的。
      * 每行一个换行。整张表挤成一行（约 400 KB 单行）会让 git 完全没法做
        增量压缩，每天都要重存一份完整 blob。
      * 简介里的换行符会打散「一行一条记录」的结构，先压平。
    """
    if not rows:
        return '<p class="muted">暂无数据。</p>'
    lines = []
    for i, r in enumerate(rows, 1):
        entry = zh.get(str(r["repo_id"])) or {}
        zh_text = (entry.get("zh") or "").strip()
        # 没翻译过就退回英文原文，至少不会留白。
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
            f'<td>{_repo_link(r["full_name"])}</td>'
            f'<td class="cat">{esc(cat)}</td>'
            f'<td>{esc(r.get("language") or "—")}</td>'
            f'<td class="num">{r["stars"]:,}</td>'
            f'<td class="num">{r["forks"]:,}</td>'
            f'<td class="num">{_fmt_delta(r["delta"], bool(r["has_prev"]))}</td>'
            f'<td class="desc">{desc_cell}</td></tr>'
        )
    return (
        '<table id="projTable"><thead><tr><th>#</th><th>项目</th><th>类目</th>'
        '<th>语言</th><th class="num">星数</th><th class="num">Fork</th>'
        '<th class="num">当日新增</th><th>简介</th></tr></thead><tbody>\n'
        + "\n".join(lines)
        + "\n</tbody></table>"
    )


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

    tracked = conn.execute(
        "SELECT COUNT(DISTINCT repo_id) c FROM snapshot"
    ).fetchone()["c"]

    # 「项目本身」名单 —— 看板的主角。相对前一日给增量，首日没有基线则显示「—」。
    prev = dates[-2] if len(dates) >= 2 else None
    projects = analyze.project_rows(conn, latest, prev) if latest else []

    # 中文简介：来自 data/i18n/zh.json（提交进 git），没有就退回英文原文。
    zh = i18n.load_cache(settings)
    translated = sum(
        1 for r in projects if (zh.get(str(r["repo_id"])) or {}).get("zh")
    )

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

    # 日均新增：只看有前一日对比的那些天
    comparable = [s for s in summary if not s["is_first"]]
    avg_gain = round(sum(s["total_gain"] for s in comparable) / len(comparable)) if comparable else 0

    cards = (
        _card("项目名单", f"{len(projects):,}", "个")
        + _card("中文简介", f"{translated:,}", f"/ {len(projects):,} 条")
        + _card("累计追踪", f"{tracked:,}", "个")
        + _card("覆盖语言", str(len(langs)), "种")
        + _card("快照天数", str(len(dates)), "天")
    )

    banner = ""
    if len(dates) < 8:
        need = 8 - len(dates)
        banner = (
            '<div class="banner"><b>预热中</b>　已积累 '
            f"{len(dates)} 天快照，还需约 {need} 天才能形成完整的 7 天窗口。"
            "「每日情况」和「累计增长」现在就可看。<br>"
            "GitHub 没有星数增量的官方接口，只能靠每日快照累积——这是设计使然，不是故障。</div>"
        )

    # 最新一日涨幅榜
    daily_top = (
        analyze.daily_gain(conn, latest, dates[-2], limit=15)
        if len(dates) >= 2
        else []
    )
    daily_block = (
        _gain_table(daily_top, f"{dates[-2]} → {latest}", "单日涨幅榜 Top 15")
        if len(dates) >= 2
        else '<p class="muted">只有一天数据，还无法计算单日涨幅。</p>'
    )

    # 累计增长榜
    cumulative = []
    if len(dates) >= 2:
        ranked, _dropped = analyze.weekly_gain(conn, first, latest, settings.max_span_days)
        cumulative = [
            {"full_name": r["full_name"], "delta": r["delta"],
             "stars": r["stars_after"], "language": r.get("language")}
            for r in ranked[:15]
        ]
    cumulative_block = (
        _gain_table(cumulative, f"{first} → {latest}", "累计增长榜 Top 15")
        if cumulative
        else '<p class="muted">只有一天数据，还无法计算累计增长。</p>'
    )

    # 「项目本身」名单 + 工具栏
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
        'placeholder="搜索项目名或中文简介…" aria-label="搜索项目">'
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
        f'<span class="count" id="projCount">共 {len(projects):,} 个</span>'
        "</div>"
    )
    projects_block = _project_table(projects, zh)

    # 历史报告索引
    report_files = sorted(reports_out.glob("*.html"), reverse=True)
    reports_block = (
        "<ul class=\"reports\">"
        + "".join(f'<li><a href="reports/{esc(p.name)}">{esc(p.stem)}</a></li>' for p in report_files)
        + "</ul>"
        if report_files
        else '<p class="muted">还没有生成过周报。每周一由定时任务自动生成。</p>'
    )

    body = f"""<h1>star-pulse · GitHub 星耀榜</h1>
<p class="sub">每日快照 · 项目名单在前，趋势在后</p>
<p class="note">数据截至 <b>{esc(str(latest or "—"))}</b>　·　时区 UTC+{settings.tz_offset_hours}　·　
每日 04:00 自动采集</p>
<div class="cards">{cards}</div>
{banner}

<h2>项目名单</h2>
<p class="note">共 <b>{len(projects):,}</b> 个追踪仓库，数据日期 {esc(str(latest or "—"))}。
可搜索、按类目或语言筛选、按星数·新增·Fork·创建时间排序；点项目名直接打开 GitHub。<br>
「类目」由关键词规则判定，确定性可复现；「简介」是模型翻译的<b>机翻</b>中文
（已译 {translated:,}/{len(projects):,} 条），想看英文原文请点项目名去 GitHub。
未译到的行仍显示英文原文。<br>
「当日新增」是相对上一次快照的净增；首日没有对比基线时显示「—」。
类目与简介都只影响可读性，不参与任何数值计算。</p>
{toolbar}
{projects_block}

<details class="trends">
<summary>趋势与榜单<span class="hint">默认收起 · 点击展开每日走势与涨幅榜</span></summary>
<div class="tbody">

<h2>每天的情况</h2>
<p class="note">「新增星数合计」是当天全部追踪仓库的星数净增之和；首日没有对比基线。
可比日均 {avg_gain:,} star。</p>
{_daily_table(summary)}

<h2>当日涨幅榜</h2>
{daily_block}

<h2>累计增长榜</h2>
{cumulative_block}

<h2>每日新增走势</h2>
<p class="note">柱状图是当天全部追踪仓库的新增星数合计，用来判断今天是不是「热闹的一天」。</p>
<div class="chart"><canvas id="dailyChart" role="img" aria-label="每日新增星数合计"></canvas></div>

<h2>领涨仓库走势</h2>
<p class="note">显示区间内累计增量最大的 8 个仓库，纵轴是相对区间首日的累计增量（不是绝对星数，
否则 3 万星和 26 万星的曲线没法画在同一张图上）。</p>
<div class="chart"><canvas id="trendChart" role="img" aria-label="领涨仓库累计增量走势"></canvas></div>

</div>
</details>

<h2>历史周报</h2>
{reports_block}

<footer>由 <a href="https://github.com/logic77lhl/star-pulse">star-pulse</a> 自动生成。
所有数值来自每日快照相减，未经任何模型改写。</footer>"""

    trend_dates, trend_series = analyze.growth_series(conn, dates, limit=8)
    light_dates = [d for d in trend_dates if d] or []
    core = [s for s in summary if not s["is_first"]]
    chart_payload = json.dumps(
        {
            "daily": {
                "labels": [s["date"] for s in core],
                "values": [s["total_gain"] for s in core],
            },
            "trend": {
                "labels": light_dates,
                "series": [
                    {"name": s["full_name"], "values": [p["gain"] for p in s["points"]]}
                    for s in trend_series
                ],
            },
        },
        ensure_ascii=False,
    )

    # 这段 JS 用普通字符串而非 f-string：JS 里大括号太多，逐个转义极易出错，
    # 改成占位符替换更稳。
    js = """<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
const D = __PAYLOAD__;
const PALETTE = ['#7F77DD','#1D9E75','#D85A30','#378ADD','#BA7517','#D4537E','#639922','#888780'];

/* ── 项目名单：搜索 / 语言筛选 / 排序。纯 DOM 操作，不依赖任何库。 ── */
(function () {
  const tbl = document.getElementById('projTable');
  if (!tbl || !tbl.tBodies.length) return;
  const tb = tbl.tBodies[0];
  const rows = Array.prototype.slice.call(tb.rows);
  const q = document.getElementById('projQ');
  const catSel = document.getElementById('projCat');
  const langSel = document.getElementById('projLang');
  const sortSel = document.getElementById('projSort');
  const out = document.getElementById('projCount');
  const low = s => (s || '').toLowerCase();
  const num = (tr, k) => Number(tr.dataset[k]) || 0;
  // 预先拼好可搜索文本。用 td.desc 而不是写死的列号 —— 列顺序改过好几次了，
  // 写死索引是最容易在下次加列时静默失配的写法。
  const hay = new Map();
  for (const tr of rows) {
    const cell = tr.querySelector('td.desc');
    hay.set(tr, low(tr.dataset.name + ' ' + tr.dataset.cat + ' '
                    + (cell ? cell.textContent : '')));
  }

  function rank() {
    let n = 0;
    for (const tr of rows) if (!tr.hidden) tr.cells[0].textContent = String(++n);
  }
  function view() {
    const needle = q.value.trim().toLowerCase();
    const cat = catSel.value;
    const lg = langSel.value;
    let shown = 0;
    for (const tr of rows) {
      const hit = (!needle || hay.get(tr).indexOf(needle) !== -1)
               && (!cat || tr.dataset.cat === cat)
               && (!lg || tr.dataset.lang === lg);
      tr.hidden = !hit;
      if (hit) shown++;
    }
    out.textContent = '显示 ' + shown.toLocaleString('en-US')
                    + ' / ' + rows.length.toLocaleString('en-US') + ' 个';
    rank();
  }
  function reorder() {
    const parts = sortSel.value.split(':');
    const key = parts[0], s = parts[1] === 'asc' ? 1 : -1;
    const sorted = rows.slice().sort(function (a, b) {
      if (key === 'name') return s * a.dataset.name.localeCompare(b.dataset.name);
      if (key === 'created') return s * (a.dataset.created || '').localeCompare(b.dataset.created || '');
      return s * (num(a, key) - num(b, key));
    });
    const frag = document.createDocumentFragment();
    for (const tr of sorted) frag.appendChild(tr);
    tb.appendChild(frag);
    rank();
  }
  q.addEventListener('input', view);
  catSel.addEventListener('change', view);
  langSel.addEventListener('change', view);
  sortSel.addEventListener('change', function () { reorder(); view(); });
  view();
})();

/* ── 图表。折叠区里的 canvas 在展开前量不到尺寸，必须等展开后再建，
      否则 Chart.js 会按 0×0 初始化，展开后是一片空白。 ── */
(function () {
  const box = document.querySelector('details.trends');
  let built = false;
  function build() {
    if (built) return;
    built = true;
    if (typeof Chart === 'undefined') {
      document.querySelectorAll('.chart').forEach(function (el) {
        el.innerHTML = '<p class="muted">图表库没能从 CDN 加载（需要联网）。'
                     + '上方表格里的数据是完整的。</p>';
      });
      return;
    }
    new Chart(document.getElementById('dailyChart'), {
      type: 'bar',
      data: { labels: D.daily.labels, datasets: [{ label: '新增星数合计', data: D.daily.values,
        backgroundColor: '#7F77DD', borderRadius: 3 }] },
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false },
          tooltip: { callbacks: { label: c => '+' + c.parsed.y.toLocaleString('en-US') + ' stars' } } },
        scales: { x: { grid: { display: false }, ticks: { autoSkip: false, maxRotation: 45 } },
          y: { beginAtZero: true, grid: { color: 'rgba(128,128,128,.18)' },
            ticks: { callback: v => v.toLocaleString('en-US') } } } }
    });

    new Chart(document.getElementById('trendChart'), {
      type: 'line',
      data: { labels: D.trend.labels, datasets: D.trend.series.map((s, i) => ({
        label: s.name, data: s.values, borderColor: PALETTE[i % PALETTE.length],
        backgroundColor: 'transparent', borderWidth: 2, tension: .25,
        pointRadius: 2, pointHoverRadius: 4 })) },
      options: { responsive: true, maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: { legend: { position: 'bottom', labels: { boxWidth: 10, usePointStyle: true, font: { size: 11 } } },
          tooltip: { callbacks: { label: c => c.dataset.label + '  +' + c.parsed.y.toLocaleString('en-US') } } },
        scales: { x: { grid: { display: false } },
          y: { beginAtZero: true, grid: { color: 'rgba(128,128,128,.18)' },
            ticks: { callback: v => '+' + v.toLocaleString('en-US') } } } }
    });
  }
  if (!box) { build(); return; }
  if (box.open) build();
  box.addEventListener('toggle', function () { if (box.open) build(); });
})();
</script>"""

    script = js.replace("__PAYLOAD__", chart_payload)

    (docs / "index.html").write_text(
        _page("star-pulse · GitHub 星耀榜", body, script), encoding="utf-8"
    )

    # 归档周报
    copied = 0
    if settings.reports_dir.is_dir():
        for src in settings.reports_dir.glob("*.html"):
            shutil.copy2(src, reports_out / src.name)
            copied += 1

    # 只放汇总层数据。
    # 刻意**不**内联 800 个仓库的逐条明细 —— 那是 data/snapshots/*.json 的职责，
    # 重复一份会让这个文件膨胀到 200 KB 并且每天全量变化，白白撑大仓库。
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
                "daily_top": daily_top,
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
    }
