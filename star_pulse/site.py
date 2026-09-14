"""静态站点生成（GitHub Pages）。

产出 docs/ 目录，作为「每天的情况」看板：
  docs/index.html          日维度看板：每日汇总表 + 每日涨幅榜 + 走势图 + 报告索引
  docs/reports/*.html      历史周报归档
  docs/data.json           全量结构化数据，供二次消费
  docs/.nojekyll           阻止 Jekyll 处理（Pages 从分支部署时需要）

站点是**完全自包含**的（图表数据直接内联在 HTML 里），不依赖 fetch，
所以本地双击打开也能看，不只是在 Pages 上能看。
"""

from __future__ import annotations

import html as html_mod
import json
import shutil
import sqlite3
from datetime import date
from pathlib import Path

from . import analyze
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
table { width:100%; border-collapse:collapse; font-size:14px; background:#fff;
  border:1px solid #e3e1da; border-radius:10px; overflow:hidden; }
th { text-align:left; padding:10px 13px; background:#f1efe8; font-weight:600; font-size:13px;
  color:#444441; white-space:nowrap; }
td { padding:10px 13px; border-top:1px solid #eeece6; vertical-align:top; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
td.num { font-weight:600; }
tr.first td { color:#888780; }
a { color:#534AB7; text-decoration:none; }
a:hover { text-decoration:underline; }
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
footer { margin-top:46px; padding-top:16px; border-top:1px solid #e3e1da; color:#888780; font-size:13px; }
@media (prefers-color-scheme: dark) {
  body { background:#1b1b19; color:#e8e6e1; }
  .card, table, .chart, ul.reports { background:#262624; border-color:#3a3a37; }
  th { background:#302f2c; color:#d3d1c7; }
  td { border-top-color:#3a3a37; }
  a { color:#AFA9EC; }
  h1, h2 { color:#eeecea; }
  .sub, .note, .muted, footer, .card .k, .card .u, .rank { color:#9b9993; }
  .banner { background:#412402; border-color:#854F0B; color:#FAC775; }
  .up { color:#F09595; }
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
    return (
        f'<a href="https://github.com/{esc(full_name)}" target="_blank" '
        f'rel="noopener">{esc(full_name)}</a>'
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
    latest_rows = analyze.daily_rows(conn, latest) if latest else []

    # 日均新增：只看有前一日对比的那些天
    comparable = [s for s in summary if not s["is_first"]]
    avg_gain = round(sum(s["total_gain"] for s in comparable) / len(comparable)) if comparable else 0

    cards = (
        _card("快照天数", str(len(dates)), "天")
        + _card("追踪仓库", f"{tracked:,}", "个")
        + _card("最新入库", f"{summary[-1]['repos']:,}" if summary else "0", "个")
        + _card("日均新增", f"{avg_gain:,}", "star")
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

    # 最新快照明细
    detail_rows = []
    for r in latest_rows[:100]:
        detail_rows.append(
            f'<tr><td>{_repo_link(r["full_name"])}</td>'
            f'<td class="num">{r["stars"]:,}</td>'
            f'<td class="num">{r["forks"]:,}</td>'
            f'<td>{esc(r.get("language") or "—")}</td></tr>'
        )
    detail_block = (
        "<table><thead><tr><th>项目</th><th class=\"num\">星数</th>"
        "<th class=\"num\">Fork</th><th>语言</th></tr></thead><tbody>"
        + "".join(detail_rows) + "</tbody></table>"
        if detail_rows
        else '<p class="muted">暂无数据。</p>'
    )

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
<p class="sub">每日快照 · 每天的情况一目了然</p>
<p class="note">数据截至 <b>{esc(str(latest or "—"))}</b>　·　时区 UTC+{settings.tz_offset_hours}　·　
每日 04:00 自动采集</p>
<div class="cards">{cards}</div>
{banner}

<h2>每天的情况</h2>
<p class="note">「新增星数合计」是当天全部追踪仓库的星数净增之和；首日没有对比基线。</p>
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

<h2>最新快照明细（前 100）</h2>
<p class="note">数据日期 {esc(str(latest or "—"))}</p>
{detail_block}

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

    script = f"""<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
const D = {chart_payload};
const PALETTE = ['#7F77DD','#1D9E75','#D85A30','#378ADD','#BA7517','#D4537E','#639922','#888780'];

new Chart(document.getElementById('dailyChart'), {{
  type: 'bar',
  data: {{ labels: D.daily.labels, datasets: [{{ label: '新增星数合计', data: D.daily.values,
    backgroundColor: '#7F77DD', borderRadius: 3 }}] }},
  options: {{ responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }},
      tooltip: {{ callbacks: {{ label: c => '+' + c.parsed.y.toLocaleString('en-US') + ' stars' }} }} }},
    scales: {{ x: {{ grid: {{ display: false }}, ticks: {{ autoSkip: false, maxRotation: 45 }} }},
      y: {{ beginAtZero: true, grid: {{ color: 'rgba(128,128,128,.18)' }},
        ticks: {{ callback: v => v.toLocaleString('en-US') }} }} }} }}
}});

new Chart(document.getElementById('trendChart'), {{
  type: 'line',
  data: {{ labels: D.trend.labels, datasets: D.trend.series.map((s, i) => ({{
    label: s.name, data: s.values, borderColor: PALETTE[i % PALETTE.length],
    backgroundColor: 'transparent', borderWidth: 2, tension: .25,
    pointRadius: 2, pointHoverRadius: 4 }})) }},
  options: {{ responsive: true, maintainAspectRatio: false, interaction: {{ mode: 'index', intersect: false }},
    plugins: {{ legend: {{ position: 'bottom', labels: {{ boxWidth: 10, usePointStyle: true, font: {{ size: 11 }} }} }},
      tooltip: {{ callbacks: {{ label: c => c.dataset.label + '  +' + c.parsed.y.toLocaleString('en-US') }} }} }},
    scales: {{ x: {{ grid: {{ display: false }} }},
      y: {{ beginAtZero: true, grid: {{ color: 'rgba(128,128,128,.18)' }},
        ticks: {{ callback: v => '+' + v.toLocaleString('en-US') }} }} }} }}
}});
</script>"""

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
