"""报告组装与渲染（Markdown / HTML）。

报告里始终包含一个「数据质量」小节，如实交代：
  - 快照覆盖了几天（决定本期结论可不可信）
  - 哪些仓库因为跨度超限被剔除（避免 14 天增量混进 7 天榜）
  - 哪些仓库被标记为疑似刷星
这是把「自动化榜单」和「可信榜单」区分开的地方。
"""

from __future__ import annotations

import html as html_mod
import json
import sqlite3
from datetime import date

from . import analyze, db
from .config import Settings


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


def _repo_meta(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("SELECT full_name, language, description, topics FROM repo").fetchall()
    return {r["full_name"]: dict(r) for r in rows}


def build_report(
    settings: Settings,
    conn: sqlite3.Connection,
    start: str,
    end: str,
    narration: dict | None = None,
) -> tuple[str, str, dict]:
    """组装报告，返回 (markdown, html, meta)。"""
    coverage = analyze.data_coverage(conn)
    ranked_all, dropped = analyze.weekly_gain(conn, start, end, settings.max_span_days)
    meta_map = _repo_meta(conn)

    top = ranked_all[: settings.top_n]
    for item in top:
        history = analyze.daily_history(conn, item["repo_id"], start, end)
        item["flags"] = analyze.star_farm_flags(item, history)
        info = meta_map.get(item["full_name"], {})
        item["category"] = analyze.classify({**info, "full_name": item["full_name"]})
        item["summary"] = (narration or {}).get(item["full_name"]) or (item.get("description") or "")

    new_repos = analyze.new_repos_in_period(conn, start, end, limit=settings.top_n)
    for item in new_repos:
        info = meta_map.get(item["full_name"], {})
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
            desc = (item.get("summary") or "").replace("\n", " ").replace("|", "／")
            if len(desc) > 70:
                desc = desc[:70] + "…"
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
    L.append(f"- 快照覆盖：**{cov['distinct_days']} 天**"
             f"（{cov['first_day'] or '—'} ~ {cov['last_day'] or '—'}），累计 {cov['rows_total']:,} 条记录")
    L.append(f"- 增量时间跨度上限：{meta['max_span_days']} 天，超出即剔除")
    if meta["dropped_count"]:
        L.append(f"- **因跨度超限被剔除 {meta['dropped_count']} 个仓库**（快照断档导致，非同口径比较）：")
        for item in meta["dropped"][:5]:
            L.append(f"  - {item['full_name']}：跨度 {item['span_days']} 天，"
                     f"+{item['delta']:,}（{item['base_date']} → {item['head_date']}）")
    else:
        L.append("- 无仓库因跨度超限被剔除")
    flagged = [i for i in top if i.get("flags")]
    if flagged:
        L.append(f"- 疑似刷星标记 {len(flagged)} 个（仅标注，未剔除）：")
        for item in flagged:
            L.append(f"  - {item['full_name']}：{'、'.join(item['flags'])}")
    else:
        L.append("- 未发现疑似刷星迹象")
    L.append("")
    L.append("---")
    L.append("")
    L.append("*本报告由 star-pulse 自动生成。所有数值来自每日快照相减，"
             "未经任何模型改写；分类与文字描述仅用于可读性，不影响排名。*")
    L.append("")
    return "\n".join(L)


# ── HTML ────────────────────────────────────────────────────────
def _html(settings: Settings, label: str, start: str, end: str, meta: dict) -> str:
    cov = meta["coverage"]
    top = meta["top"]
    esc = html_mod.escape

    def table(headers: list[str], rows: list[list[str]], numeric: set[int]) -> str:
        out = ['<table><thead><tr>']
        for i, h in enumerate(headers):
            out.append(f'<th style="text-align:right">{esc(h)}</th>' if i in numeric else f'<th>{esc(h)}</th>')
        out.append("</tr></thead><tbody>")
        for row in rows:
            out.append("<tr>")
            for i, cell in enumerate(row):
                cls = ' class="num"' if i in numeric else ""
                out.append(f"<td{cls}>{cell}</td>")
            out.append("</tr>")
        out.append("</tbody></table>")
        return "".join(out)

    if top:
        top_rows = []
        for i, item in enumerate(top, 1):
            flags = item.get("flags") or []
            badge = f' <span class="warn">⚠ {"、".join(esc(f) for f in flags)}</span>' if flags else ""
            desc = esc((item.get("summary") or "").replace("\n", " "))
            if len(desc) > 70:
                desc = desc[:70] + "…"
            url = f"https://github.com/{item['full_name']}"
            top_rows.append([
                str(i),
                f'<a href="{esc(url)}" target="_blank" rel="noopener">{esc(item["full_name"])}</a>{badge}',
                f'+{item["delta"]:,}',
                f'{item["stars_after"]:,}',
                esc(item.get("language") or "—"),
                esc(item.get("category") or "其他"),
                desc,
            ])
        top_table = table(
            ["排名", "项目", "本周新增", "总星", "语言", "分类", "一句话"],
            top_rows,
            {0, 2, 3},
        )
    else:
        top_table = f'<p class="muted">{esc(meta["warmup_note"])}</p>'

    chart_labels = [i["full_name"] for i in top[:12]]
    chart_values = [i["delta"] for i in top[:12]]

    new_rows = [
        [
            str(i),
            f'<a href="https://github.com/{esc(r["full_name"])}" target="_blank" rel="noopener">{esc(r["full_name"])}</a>',
            f'{r["stars"]:,}',
            esc(r.get("repo_created") or "—"),
            esc(r.get("language") or "—"),
            esc(r.get("category") or "其他"),
        ]
        for i, r in enumerate(meta["new_repos"], 1)
    ]
    new_table = table(["排名", "项目", "当前星数", "创建日期", "语言", "分类"], new_rows, {0, 2}) if new_rows else '<p class="muted">本期无新项目记录。</p>'

    cat_rows = [
        [esc(name), str(count), f"{count / (sum(c for _, c in meta['breakdown']) or 1):.0%}"]
        for name, count in meta["breakdown"]
    ]
    cat_table = table(["分类", "数量", "占比"], cat_rows, {1, 2}) if cat_rows else '<p class="muted">—</p>'

    flagged = [i for i in top if i.get("flags")]
    quality_items = [
        f"快照覆盖 <b>{cov['distinct_days']} 天</b>（{esc(str(cov['first_day'] or '—'))} ~ {esc(str(cov['last_day'] or '—'))}），累计 {cov['rows_total']:,} 条记录",
        f"增量时间跨度上限 <b>{meta['max_span_days']} 天</b>，超出即剔除",
        f"因跨度超限被剔除 <b>{meta['dropped_count']}</b> 个仓库" if meta["dropped_count"] else "无仓库因跨度超限被剔除",
        f"疑似刷星标记 <b>{len(flagged)}</b> 个（仅标注，未剔除）" if flagged else "未发现疑似刷星迹象",
    ]
    quality_html = "".join(f"<li>{x}</li>" for x in quality_items)

    banner = ""
    if not meta["warm"]:
        banner = f'<div class="banner"><b>本期无增量榜</b>　{esc(meta["warmup_note"])}</div>'

    payload = json.dumps({"labels": chart_labels, "values": chart_values}, ensure_ascii=False)
    chart_block = ""
    if top:
        chart_block = f"""
<h2>增长分布</h2>
<div class="chartwrap"><canvas id="gainChart" role="img" aria-label="Top 12 项目本周新增 Star 横向条形图"></canvas></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
const D = {payload};
new Chart(document.getElementById('gainChart'), {{
  type: 'bar',
  data: {{ labels: D.labels, datasets: [{{ label: '本周新增 Star', data: D.values,
    backgroundColor: '#7F77DD', borderRadius: 3, barThickness: 16 }}] }},
  options: {{ indexAxis: 'y', responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }},
      tooltip: {{ callbacks: {{ label: c => '+' + c.parsed.x.toLocaleString('en-US') }} }} }},
    scales: {{
      x: {{ beginAtZero: true, grid: {{ color: 'rgba(128,128,128,.18)' }},
            ticks: {{ callback: v => (v/1000) + 'k' }} }},
      y: {{ grid: {{ display: false }}, ticks: {{ autoSkip: false }} }}
    }} }}
}});
</script>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GitHub 星耀榜 · {esc(label)}</title>
<style>
:root {{ color-scheme: light dark; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; padding: 40px 20px; font-family: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  background: #f7f7f5; color: #22211f; line-height: 1.65; }}
.wrap {{ max-width: 1040px; margin: 0 auto; }}
h1 {{ font-size: 26px; font-weight: 600; margin: 0 0 8px; letter-spacing: -.01em; }}
h2 {{ font-size: 17px; font-weight: 600; margin: 40px 0 14px; }}
.sub {{ color: #6b6a66; font-size: 14px; margin: 0 0 4px; }}
.banner {{ background: #FAEEDA; border: 1px solid #EF9F27; color: #633806;
  padding: 14px 18px; border-radius: 10px; margin: 24px 0; font-size: 14px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; background: #fff;
  border-radius: 10px; overflow: hidden; border: 1px solid #e3e1da; }}
th {{ text-align: left; padding: 11px 14px; background: #f1efe8; font-weight: 600;
  font-size: 13px; color: #444441; white-space: nowrap; }}
td {{ padding: 11px 14px; border-top: 1px solid #eeece6; vertical-align: top; }}
td.num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; font-weight: 600; }}
a {{ color: #534AB7; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.warn {{ color: #A32D2D; font-size: 12px; }}
.muted {{ color: #888780; font-size: 14px; }}
.chartwrap {{ position: relative; height: 420px; background: #fff; border: 1px solid #e3e1da;
  border-radius: 10px; padding: 16px; }}
ul.quality {{ background: #fff; border: 1px solid #e3e1da; border-radius: 10px;
  padding: 16px 16px 16px 36px; font-size: 14px; }}
ul.quality li {{ margin: 4px 0; }}
footer {{ margin-top: 40px; padding-top: 18px; border-top: 1px solid #e3e1da;
  color: #888780; font-size: 13px; }}
@media (prefers-color-scheme: dark) {{
  body {{ background: #1b1b19; color: #e8e6e1; }}
  table, ul.quality, .chartwrap {{ background: #262624; border-color: #3a3a37; }}
  th {{ background: #302f2c; color: #d3d1c7; }}
  td {{ border-top-color: #3a3a37; }}
  a {{ color: #AFA9EC; }}
  h1, h2 {{ color: #eeecea; }}
  .sub, .muted, footer {{ color: #9b9993; }}
  .banner {{ background: #412402; border-color: #854F0B; color: #FAC775; }}
}}
</style>
</head>
<body>
<div class="wrap">
  <h1>GitHub 星耀榜 · {esc(label)}</h1>
  <p class="sub">统计周期 {esc(start)} ~ {esc(end)}（UTC+{settings.tz_offset_hours}）　·　数据截至 {esc(str(cov['last_day'] or '—'))}　·　快照覆盖 {cov['distinct_days']} 天</p>
  {banner}
  <h2>本周新增 Star Top {len(top)}</h2>
  {top_table}
  {chart_block}
  <h2>新项目榜（周期内创建）</h2>
  <p class="sub">口径不同：衡量新项目起跑速度，不是存量增长。</p>
  {new_table}
  <h2>分类透视</h2>
  {cat_table}
  <h2>数据质量</h2>
  <ul class="quality">{quality_html}</ul>
  <footer>本报告由 star-pulse 自动生成。所有数值来自每日快照相减，未经任何模型改写；
  分类与文字描述仅用于可读性，不影响排名。</footer>
</div>
</body>
</html>
"""
