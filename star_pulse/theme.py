"""共享视觉层：设计令牌、样式表、HTML 原语。

看板（site.py）与周报（render.py）共用这一份 —— 此前两边各写了一套 CSS，
同一个产品的两类页面字体、间距、表格样式、暗色模式全不一致，改一处漏一处。

两个刻意的实现选择：

1. **样式表用普通字符串，不用 f-string。** CSS 里大括号密集，f-string 要求逐个
   写成 `{{`/`}}`，是最容易在改样式时静默写错的地方（render.py 此前就是这样）。
   需要插值的只有极少数地方，用占位符替换即可。

2. **暗色模式是令牌替换，不是规则复制。** 所有颜色都走 CSS 自定义属性，
   暗色下只覆盖 `:root` 那一小段变量。这样每条组件规则只写一次，
   不存在「亮色改了、暗色忘了改」的漂移。
"""

from __future__ import annotations

import html as html_mod

esc = html_mod.escape


# ── 设计令牌 ────────────────────────────────────────────────────
# 命名按语义而非外观（--up 而不是 --red），这样换配色不必改组件规则。
_TOKENS_LIGHT = """
  --bg:          #faf9f6;
  --surface:     #ffffff;
  --surface-2:   #f4f2ec;
  --border:      #e7e3da;
  --border-2:    #d6d1c4;
  --text:        #1b1a17;
  --text-2:      #55524a;
  --text-3:      #8b877c;
  --accent:      #534ab7;
  --accent-2:    #7f77dd;
  --accent-soft: rgba(83, 74, 183, .09);
  --up:          #c2410c;
  --up-soft:     rgba(194, 65, 12, .10);
  --down:        #15803d;
  --warn-bg:     #fdf4e4;
  --warn-border: #eecb96;
  --warn-text:   #7a4a0b;
  --grid:        rgba(120, 116, 106, .22);
  --shadow:      0 1px 2px rgba(28, 26, 22, .04), 0 4px 14px rgba(28, 26, 22, .05);
"""

_TOKENS_DARK = """
  --bg:          #131312;
  --surface:     #1d1d1b;
  --surface-2:   #26261f;
  --border:      #33332e;
  --border-2:    #45453e;
  --text:        #eceae4;
  --text-2:      #b3b0a6;
  --text-3:      #8a877d;
  --accent:      #aea7ee;
  --accent-2:    #8f87e0;
  --accent-soft: rgba(174, 167, 238, .12);
  --up:          #f2955f;
  --up-soft:     rgba(242, 149, 95, .14);
  --down:        #6fd39a;
  --warn-bg:     #3a2a0c;
  --warn-border: #7d5a1c;
  --warn-text:   #f3cd8c;
  --grid:        rgba(170, 166, 156, .22);
  --shadow:      0 1px 2px rgba(0, 0, 0, .3), 0 4px 14px rgba(0, 0, 0, .25);
"""


# 普通字符串，不插值 —— 见模块 docstring 第 1 条
CSS = """
:root {
  color-scheme: light dark;
__TOKENS__
  --r:      12px;
  --r-sm:   8px;
  --r-pill: 999px;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}
*, *::before, *::after { box-sizing: border-box; }

body {
  margin: 0;
  padding: 0 20px 72px;
  font-family: system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  font-size: 14px;
  line-height: 1.62;
  background: var(--bg);
  color: var(--text);
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1140px; margin: 0 auto; }

/* ── 页头 ─────────────────────────────────────────── */
.topbar { padding: 40px 0 26px; }
h1 { font-size: 29px; line-height: 1.2; font-weight: 650; letter-spacing: -.022em; margin: 0 0 10px; }
h1 .brand { color: var(--text-3); font-weight: 450; }
h2 { font-size: 16px; font-weight: 620; letter-spacing: -.008em; margin: 0 0 4px; }
h3 { font-size: 14px; font-weight: 600; margin: 0 0 4px; }
.meta { display: flex; flex-wrap: wrap; gap: 6px 16px; color: var(--text-3); font-size: 13px; }
.meta b { color: var(--text-2); font-weight: 600; font-variant-numeric: tabular-nums; }
.note { color: var(--text-3); font-size: 13px; margin: 6px 0 14px; max-width: 78ch; }
.note b { color: var(--text-2); font-weight: 600; }

/* ── 指标卡 ───────────────────────────────────────── */
.kpis {
  display: grid; gap: 12px; margin: 4px 0 8px;
  grid-template-columns: repeat(auto-fit, minmax(168px, 1fr));
}
.kpi {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--r);
  padding: 15px 17px 13px; box-shadow: var(--shadow);
  display: flex; flex-direction: column; gap: 2px; min-width: 0;
}
.kpi .k { font-size: 12.5px; color: var(--text-3); font-weight: 500; }
.kpi .v {
  font-size: 26px; font-weight: 660; letter-spacing: -.028em; line-height: 1.15;
  font-variant-numeric: tabular-nums; white-space: nowrap;
}
.kpi .v .u { font-size: 13px; font-weight: 500; color: var(--text-3); margin-left: 3px; letter-spacing: 0; }
.kpi .d { font-size: 12.5px; color: var(--text-3); font-variant-numeric: tabular-nums; }
.kpi .spark { margin-top: 6px; height: 30px; }

/* ── 面板 ─────────────────────────────────────────── */
.panel {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--r);
  padding: 18px 20px 20px; margin-top: 16px; box-shadow: var(--shadow);
}
.panel > h2 { margin-bottom: 3px; }
.panel > .note:last-child { margin-bottom: 0; }
.panel-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.panel-head .more { font-size: 13px; white-space: nowrap; }

/* ── 表格 ─────────────────────────────────────────── */
/* 宽表格必须有自己的横向滚动容器。少了它，表格会把**整页**撑宽 ——
   窄屏上标题、说明、页脚全部被裁掉且无法滚动（实测页面 scrollWidth 402→617）。 */
.scroll-x { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table.data { width: 100%; border-collapse: collapse; font-size: 13.5px; }
table.data th {
  text-align: left; padding: 9px 12px; font-size: 12px; font-weight: 600;
  color: var(--text-3); text-transform: none; letter-spacing: .01em;
  border-bottom: 1px solid var(--border-2); white-space: nowrap;
  position: sticky; top: 0; background: var(--surface); z-index: 2;
}
table.data td { padding: 9px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }
table.data tbody tr:last-child td { border-bottom: 0; }
table.data tbody tr:hover td { background: var(--surface-2); }
table.data .num, table.data th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
table.data td.num { font-weight: 600; }
table.data.compact td, table.data.compact th { padding: 7px 10px; }
table.data .rank { color: var(--text-3); font-variant-numeric: tabular-nums; width: 1%; }
table.data tbody tr:nth-child(-n+3) .rank { color: var(--text); font-weight: 650; }
/* 简介列：限宽 + 弱化，避免长文本把行高撑得参差不齐 */
table.data td.desc { color: var(--text-2); font-size: 13px; min-width: 240px; max-width: 460px; }

/* 表格里代表数值强度的内联条：让 +11,059 和 +29 一眼可分 */
.bar { display: block; height: 4px; border-radius: 2px; background: var(--up-soft); margin-top: 4px; min-width: 2px; }
.bar > i { display: block; height: 100%; border-radius: 2px; background: var(--up); }

/* ── 语义色 ───────────────────────────────────────── */
.up { color: var(--up); font-weight: 620; font-variant-numeric: tabular-nums; }
.down { color: var(--down); font-weight: 620; font-variant-numeric: tabular-nums; }
.zero { color: var(--text-3); font-variant-numeric: tabular-nums; }
.muted { color: var(--text-3); }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; text-underline-offset: 2.5px; }
a.repo { text-decoration: underline; text-decoration-color: color-mix(in srgb, var(--accent) 35%, transparent); text-underline-offset: 2.5px; }
a.repo:hover { text-decoration-color: currentColor; }
a.repo .ext { font-size: .8em; margin-left: 3px; opacity: .45; }

.tag {
  display: inline-block; padding: 1px 8px; border-radius: var(--r-pill);
  background: var(--accent-soft); color: var(--accent);
  font-size: 12px; font-weight: 550; white-space: nowrap;
}
.tag.plain { background: var(--surface-2); color: var(--text-2); }

/* ── 提示条 ───────────────────────────────────────── */
.banner {
  background: var(--warn-bg); border: 1px solid var(--warn-border); color: var(--warn-text);
  padding: 12px 16px; border-radius: var(--r); margin: 16px 0 0; font-size: 13.5px;
}
.banner b { font-weight: 650; }

/* ── 图表 ─────────────────────────────────────────── */
/* overflow-x 是必须的：折线图带 min-width，没有这层滚动容器时它会把**整个页面**
   撑到 560px 宽，窄屏上标题、说明、表格全部被裁掉且无法滚动。 */
.chart { margin-top: 14px; overflow-x: auto; -webkit-overflow-scrolling: touch; }
.chart svg { display: block; width: 100%; height: auto; min-width: 560px; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 14px; margin-top: 12px; font-size: 12.5px; color: var(--text-2); }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.legend i { width: 9px; height: 9px; border-radius: 2px; flex: none; }

/* 横向条形榜（周报的「增长分布」）：纯 CSS，标签真实文本，窄屏自动换行 */
.hbars { display: grid; gap: 6px; margin-top: 14px; }
.hbar { display: grid; grid-template-columns: minmax(90px, 22%) 1fr auto; gap: 10px; align-items: center; font-size: 13px; }
.hbar .lbl { color: var(--text-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hbar .track { background: var(--surface-2); border-radius: 3px; height: 16px; overflow: hidden; }
.hbar .track > i { display: block; height: 100%; background: var(--accent-2); border-radius: 3px; }
.hbar .val { color: var(--up); font-weight: 620; font-variant-numeric: tabular-nums; white-space: nowrap; }
@media (max-width: 640px) {
  .hbar { grid-template-columns: 1fr auto; grid-template-areas: "lbl val" "track track"; gap: 3px 8px; }
  .hbar .lbl { grid-area: lbl; }
  .hbar .val { grid-area: val; }
  .hbar .track { grid-area: track; }
}

/* CSS 柱状图：完全响应式，标签是真实文本，窄屏也不会被缩放成蚂蚁字。
   数值提示走原生 title —— 自绘 tooltip 是 nowrap 的绝对定位盒子，首尾柱子
   的提示框会横向溢出把整页撑宽（实测 +49px），不值得。 */
.bars { display: flex; align-items: flex-end; gap: 3px; height: 190px; margin-top: 16px; }
.bars .col { flex: 1 1 0; min-width: 0; display: flex; flex-direction: column; justify-content: flex-end; height: 100%; }
.bars .col > i { display: block; background: var(--accent-2); border-radius: 3px 3px 0 0; min-height: 2px; transition: background .12s; }
.bars .col:hover > i { background: var(--accent); }
.bars-x { display: flex; gap: 3px; margin-top: 6px; }
.bars-x span {
  flex: 1 1 0; min-width: 0; text-align: center; font-size: 11px; color: var(--text-3);
  overflow: hidden; white-space: nowrap;
}
/* 窄屏下隔一个显示一个日期，避免糊成一团 */
@media (max-width: 720px) { .bars-x span:nth-child(even) { visibility: hidden; } }

/* ── 周报列表 ─────────────────────────────────────── */
.reports { display: grid; gap: 8px; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); margin: 0; padding: 0; list-style: none; }
.reports a {
  display: flex; flex-direction: column; gap: 2px; padding: 11px 14px;
  border: 1px solid var(--border); border-radius: var(--r-sm); background: var(--surface-2);
  color: var(--text); text-decoration: none; transition: border-color .12s, background .12s;
}
.reports a:hover { border-color: var(--border-2); background: var(--surface); text-decoration: none; }
.reports .wk { font-weight: 620; font-size: 14px; }
.reports .rg { font-size: 12.5px; color: var(--text-3); font-variant-numeric: tabular-nums; }

/* ── 工具栏 ───────────────────────────────────────── */
.toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 0 0 14px; }
.toolbar input, .toolbar select {
  font: inherit; font-size: 13.5px; padding: 7px 11px;
  border: 1px solid var(--border-2); border-radius: var(--r-sm);
  background: var(--surface); color: var(--text);
}
.toolbar input:focus, .toolbar select:focus { outline: 2px solid var(--accent-soft); border-color: var(--accent); }
.toolbar input { flex: 1 1 240px; min-width: 160px; }
.toolbar .count { color: var(--text-3); font-size: 12.5px; margin-left: auto; white-space: nowrap; font-variant-numeric: tabular-nums; }

/* ── 折叠块 ───────────────────────────────────────── */
details.fold { margin-top: 16px; }
details.fold > summary {
  cursor: pointer; list-style: none; user-select: none;
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--r);
  padding: 14px 18px; font-size: 14.5px; font-weight: 600;
  display: flex; align-items: center; gap: 9px; box-shadow: var(--shadow);
}
details.fold > summary::-webkit-details-marker { display: none; }
details.fold > summary::before {
  content: "▸"; color: var(--text-3); font-weight: 400;
  display: inline-block; transition: transform .15s ease;
}
details.fold[open] > summary::before { transform: rotate(90deg); }
details.fold > summary:hover { border-color: var(--border-2); }
details.fold > summary .hint { font-weight: 400; font-size: 12.5px; color: var(--text-3); }

/* ── 周报的「数据质量」清单 ───────────────────────── */
ul.quality { margin: 4px 0 0; padding-left: 22px; font-size: 13.5px; color: var(--text-2); }
ul.quality > li { margin: 6px 0; }
ul.quality ul { margin: 4px 0 0; padding-left: 20px; color: var(--text-3); font-size: 13px; }
ul.quality ul li { margin: 2px 0; }

footer { margin-top: 40px; padding-top: 18px; border-top: 1px solid var(--border); color: var(--text-3); font-size: 12.5px; }
footer a { color: var(--text-2); }

/* ── 窄屏 ─────────────────────────────────────────── */
@media (max-width: 640px) {
  body { padding: 0 14px 56px; }
  .topbar { padding: 26px 0 20px; }
  h1 { font-size: 23px; }
  .kpi .v { font-size: 22px; }
  .panel { padding: 15px 15px 17px; }
  .bars { height: 150px; }
  .note { max-width: none; }
}
"""

CSS = CSS.replace("__TOKENS__", _TOKENS_LIGHT)
DARK_CSS = ":root {" + _TOKENS_DARK + "}"
CSS = CSS + "@media (prefers-color-scheme: dark) {\n" + DARK_CSS + "\n}\n"


def page(title: str, body: str, *, script: str = "", lang: str = "zh-CN") -> str:
    """包一个完整 HTML 文档。script 为空时不会插入空的 <script> 标签。"""
    head_extra = f"\n{script}" if script else ""
    return (
        f'<!DOCTYPE html>\n<html lang="{lang}">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{esc(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n"
        f'<div class="wrap">\n{body}\n</div>{head_extra}\n</body>\n</html>\n'
    )


# ── HTML 原语 ───────────────────────────────────────────────────
def repo_link(full_name: str) -> str:
    """仓库链接。常态带下划线 + ↗ 角标 —— 不做视觉区分时读者认不出可以点。"""
    return (
        f'<a class="repo" href="https://github.com/{esc(full_name)}" target="_blank" '
        f'rel="noopener">{esc(full_name)}<span class="ext" aria-hidden="true">↗</span></a>'
    )


def fmt_int(value: int | None) -> str:
    return f"{value:,}" if isinstance(value, int) else "—"


def delta_html(delta: int | None, *, has_prev: bool = True) -> str:
    """增量单元格。没有对比基线时给「—」而不是 0，避免被误读成「真的没涨」。"""
    if not has_prev or delta is None:
        return '<span class="muted">—</span>'
    if delta > 0:
        return f'<span class="up">+{delta:,}</span>'
    if delta < 0:
        return f'<span class="down">{delta:,}</span>'
    return '<span class="zero">0</span>'


def delta_bar(delta: int | None, max_delta: int) -> str:
    """增量强度条：把数值大小变成可见长度。

    表格里 +11,059 和 +29 是同一串数字加同一个颜色，扫视时完全分不出量级；
    一条按比例的长度条能在不看数字的情况下立刻分出主次。
    """
    if not delta or delta <= 0 or max_delta <= 0:
        return ""
    pct = max(2.0, min(100.0, delta / max_delta * 100))
    return f'<span class="bar" aria-hidden="true"><i style="width:{pct:.1f}%"></i></span>'


def kpi(label: str, value: str, unit: str = "", detail: str = "", spark: str = "") -> str:
    u = f'<span class="u">{esc(unit)}</span>' if unit else ""
    d = f'<span class="d">{detail}</span>' if detail else ""
    sp = f'<div class="spark">{spark}</div>' if spark else ""
    return (
        f'<div class="kpi"><span class="k">{esc(label)}</span>'
        f'<span class="v">{value}{u}</span>{d}{sp}</div>'
    )


def table(
    headers: list,
    rows: list[list] | None = None,
    *,
    cls: str = "data",
    table_id: str = "",
    raw_rows: str = "",
    scroll: bool = True,
) -> str:
    """通用表格。

    headers/单元格都可以是 `"文本"` 或 `("HTML", "额外class")`。
    `raw_rows` 用于需要自己控制 `<tr>` 属性的场合（例如项目名单要给每行挂
    data-* 供前端筛选），此时传 `rows=None`。
    `scroll` 默认开启：把表格包进横向滚动容器，避免宽表格把整页撑宽。
    """

    def cell(item) -> str:
        if isinstance(item, tuple):
            text, extra = item
            klass = f' class="{esc(extra)}"' if extra else ""
            return f"<td{klass}>{text}</td>"
        return f"<td>{item}</td>"

    def head(item) -> str:
        if isinstance(item, tuple):
            text, extra = item
            klass = f' class="{esc(extra)}"' if extra else ""
            return f"<th{klass}>{text}</th>"
        return f"<th>{item}</th>"

    id_attr = f' id="{esc(table_id)}"' if table_id else ""
    body = raw_rows or "\n".join(
        "<tr>" + "".join(cell(c) for c in row) + "</tr>" for row in (rows or [])
    )
    html = (
        f'<table class="{esc(cls)}"{id_attr}><thead><tr>'
        + "".join(head(h) for h in headers)
        + "</tr></thead><tbody>\n" + body + "\n</tbody></table>"
    )
    return f'<div class="scroll-x">{html}</div>' if scroll else html
