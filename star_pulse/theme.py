"""共享视觉层：设计令牌、样式表、HTML 原语。

看板（site.py）、项目名单（site.py）与周报（render.py）共用这一份 —— 此前两边
各写了一套 CSS，同一个产品的三类页面字体、间距、表格样式、暗色模式全不一致，
改一处漏一处。

三个刻意的实现选择：

1. **样式表用普通字符串，不用 f-string。** CSS 里大括号密集，f-string 要求逐个
   写成 `{{`/`}}`，是最容易在改样式时静默写错的地方（render.py 此前就是这样）。
   需要插值的只有极少数地方，用占位符替换即可。

2. **暗色模式是令牌替换，不是规则复制。** 所有颜色都走 CSS 自定义属性，
   暗色下只覆盖 `:root` 那一小段变量。这样每条组件规则只写一次，
   不存在「亮色改了、暗色忘了改」的漂移。暗色令牌只有一份 `_TOKENS_DARK`，
   被 `:root[data-theme="dark"]` 与「停用 JS 时跟随系统」两个选择器共用。

3. **看板 / 周报保持零 JS。** 暗色只用 `prefers-color-scheme` 的纯 CSS 规则，
   不注入主题脚本、不加折叠块 —— 这是回归测试里的硬不变量，也是「离线、
   断网、停用 JS 看到的内容完全一样」的立身之本。项目名单页是唯一例外，
   它的搜索脚本由调用方显式传入 `page(script=...)`。

配色对齐 logic77lhl.github.io 索引站：纸感底、琥珀金强调、宋体标题；
信息骨架借鉴风扇看板与 Artificial Analysis：结论前置、一屏一问、
数字是主角、连通指标条、等宽数字。
"""

from __future__ import annotations

import html as html_mod

esc = html_mod.escape


# ── 设计令牌 ────────────────────────────────────────────────────
# 命名按语义而非外观（--up 而不是 --red），这样换配色不必改组件规则。
# 默认（浅色）配色对齐 logic77lhl.github.io 索引站的「纸感 + 琥珀金」。
_TOKENS_LIGHT = """
  --bg:          #fbf9f5;
  --surface:     #ffffff;
  --surface-2:   #f5f2ea;
  --border:      #e7e2d7;
  --border-2:    #d2cbba;
  --text:        #16181c;
  --text-2:      #39404b;
  --text-3:      #8b9199;
  --accent:      #8a6200;
  --accent-2:    #e0a900;
  --accent-fill: #f6c800;
  --accent-soft: rgba(246, 200, 0, .18);
  --up:          #b45309;
  --up-soft:     rgba(180, 83, 9, .10);
  --down:        #0f9d6b;
  --warn-bg:     #fdf4e4;
  --warn-border: #eecb96;
  --warn-text:   #7a4a0b;
  --grid:        rgba(120, 116, 106, .22);
  --shadow:      0 1px 2px rgba(28, 26, 22, .04), 0 8px 24px rgba(28, 26, 22, .05);
  --head-bg:     rgba(251, 249, 245, .85);
  --glow:        radial-gradient(900px 380px at 8% -12%, rgba(246, 200, 0, .14), transparent 60%),
                 radial-gradient(700px 340px at 92% -4%, rgba(3, 105, 161, .05), transparent 55%);
"""

_TOKENS_DARK = """
  --bg:          #0b0d10;
  --surface:     #12161b;
  --surface-2:   #171c23;
  --border:      #232a33;
  --border-2:    #313b47;
  --text:        #e9edf2;
  --text-2:      #cbd3dd;
  --text-3:      #98a2b3;
  --accent:      #ffd21e;
  --accent-2:    #ffd21e;
  --accent-fill: #ffd21e;
  --accent-soft: rgba(255, 210, 30, .12);
  --up:          #fbbf6b;
  --up-soft:     rgba(251, 191, 107, .14);
  --down:        #34d399;
  --warn-bg:     #3a2a0c;
  --warn-border: #7d5a1c;
  --warn-text:   #f3cd8c;
  --grid:        rgba(170, 166, 156, .16);
  --shadow:      0 1px 2px rgba(0, 0, 0, .35), 0 8px 24px rgba(0, 0, 0, .28);
  --head-bg:     rgba(11, 13, 16, .82);
  --glow:        radial-gradient(900px 380px at 8% -12%, rgba(255, 210, 30, .06), transparent 60%),
                 radial-gradient(700px 340px at 92% -4%, rgba(125, 211, 252, .05), transparent 55%);
"""


# 普通字符串，不插值 —— 见模块 docstring 第 1 条
CSS = """
:root {
  color-scheme: light;
__TOKENS__
  --r:      12px;
  --r-sm:   8px;
  --r-pill: 999px;
  --serif: "Iowan Old Style", "Palatino Linotype", Georgia, "Songti SC", "Source Han Serif SC", "Noto Serif CJK SC", "SimSun", "宋体", serif;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  --mono: ui-monospace, SFMono-Regular, "Cascadia Mono", Consolas, monospace;
  --max: 1160px;
}
*, *::before, *::after { box-sizing: border-box; }

html { background: var(--bg); -webkit-font-smoothing: antialiased; scroll-behavior: smooth; }
body {
  margin: 0;
  padding: 0 20px 72px;
  font-family: var(--sans);
  font-size: 14px;
  line-height: 1.62;
  background: var(--glow), var(--bg);
  color: var(--text);
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: var(--max); margin: 0 auto; }

/* ── 页头 ─────────────────────────────────────────── */
.topbar { padding: 44px 0 10px; }
h1 { font-family: var(--serif); font-size: 46px; line-height: 1.18; font-weight: 400; letter-spacing: -.012em; margin: 0 0 14px; }
h1 .brand { color: var(--text-3); font-weight: 400; }
h2 { font-family: var(--serif); font-size: 20px; font-weight: 400; letter-spacing: -.008em; margin: 0 0 4px; }
h3 { font-size: 14px; font-weight: 650; margin: 0 0 4px; }
.eyebrow { font-family: var(--mono); font-size: 11.5px; letter-spacing: .12em; color: var(--text-3); margin-bottom: 18px; }
.lede { color: var(--text-2); font-size: 16px; margin: 0 0 18px; max-width: 60ch; }
.meta { display: flex; flex-wrap: wrap; gap: 6px 16px; color: var(--text-3); font-size: 13px; font-family: var(--mono); }
.meta b { color: var(--text-2); font-weight: 600; font-variant-numeric: tabular-nums; }
.note { color: var(--text-3); font-size: 13px; margin: 6px 0 14px; max-width: 78ch; }
.note b { color: var(--text-2); font-weight: 600; }
/* 首页 Hero 两栏：左陈述、右竖线旁白（对齐索引站首屏） */
.hero.two-col { display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 44px; align-items: start; }
.hero-main { min-width: 0; }
.hero-aside { border-left: 1px solid var(--border-2); padding-left: 26px; color: var(--text-2); font-size: 13.5px; }
.hero-aside p { margin: 0 0 12px; }
.hero-aside .q { color: var(--text-3); font-family: var(--serif); font-size: 15px; }
@media (max-width: 820px) {
  .hero.two-col { grid-template-columns: minmax(0, 1fr); gap: 20px; }
  .hero-aside { border-left: 0; border-top: 1px solid var(--border); padding: 16px 0 0; }
}
/* 元信息药丸：把「数据截至 / 天数 / 时区」从一串灰字变成可读的坐标 */
.meta .pill { display: inline-flex; align-items: center; gap: 6px; background: var(--surface); border: 1px solid var(--border); border-radius: 999px; padding: 3px 11px; }
.meta.plain { font-family: var(--sans); }

/* ── 指标条：连通的一条，主角卡用琥珀金竖条标出 ───────── */
.kpis {
  display: grid; gap: 1px; margin: 22px 0 8px;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  background: var(--border); border: 1px solid var(--border);
  border-radius: var(--r); overflow: hidden; box-shadow: var(--shadow);
}
.kpi {
  background: var(--surface);
  padding: 17px 18px 15px;
  display: flex; flex-direction: column; gap: 2px; min-width: 0; position: relative;
}
.kpi.primary::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; background: var(--accent-fill); }
.kpi .k { font-size: 12px; color: var(--text-3); font-weight: 500; }
.kpi .v {
  font-family: var(--mono); font-size: 28px; font-weight: 700; letter-spacing: -.03em; line-height: 1.2;
  font-variant-numeric: tabular-nums; white-space: nowrap;
}
.kpi.primary .v { color: var(--text); }
.kpi .v .u { font-size: 13px; font-weight: 500; color: var(--text-3); margin-left: 3px; letter-spacing: 0; font-family: var(--sans); }
.kpi .d { font-size: 12px; color: var(--text-3); font-variant-numeric: tabular-nums; }
.kpi .d .up { color: var(--up); font-weight: 600; }
.kpi .d .down { color: var(--down); font-weight: 600; }
.kpi .spark { margin-top: 8px; height: 30px; }
.kpi .spark svg { display: block; width: 100%; height: 30px; }

/* ── 面板 ─────────────────────────────────────────── */
.panel {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--r);
  padding: 20px 22px 22px; margin-top: 16px; box-shadow: var(--shadow); min-width: 0;
}
.panel > h2 { margin-bottom: 3px; }
.panel > .note:last-child { margin-bottom: 0; }
.panel-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.panel-head h2 { font-family: var(--serif); font-size: 18px; font-weight: 400; margin: 0; }
.panel-head .more { font-size: 12.5px; white-space: nowrap; color: var(--text-2); }

/* 两栏分组：打破「一张接一张白卡」的瀑布节奏 */
.grid-2 { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, .85fr); gap: 16px; margin-top: 16px; }
.grid-2 > .panel { margin-top: 0; }
@media (max-width: 900px) { .grid-2 { grid-template-columns: minmax(0, 1fr); } }

/* 编号分区标题：给长页一个骨架和方向 */
.section-cap { display: flex; align-items: center; gap: 10px; margin: 40px 0 0; }
.section-cap .no { font-family: var(--mono); font-size: 11px; color: var(--accent); border: 1px solid var(--border-2); border-radius: 6px; padding: 1px 7px; }
.section-cap h3 { font-family: var(--serif); font-size: 17px; font-weight: 400; margin: 0; }
.section-cap .line { flex: 1; height: 1px; background: var(--border); }
.section-cap .cap-note { font-size: 12px; color: var(--text-3); }

/* 方法卡（口径说明） */
.method { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 18px; margin-top: 8px; }
@media (max-width: 760px) { .method { grid-template-columns: minmax(0, 1fr); } }
.method .m .t { font-weight: 650; font-size: 13.5px; margin-bottom: 5px; display: flex; align-items: center; gap: 8px; }
.method .m .t i { width: 22px; height: 22px; border-radius: 6px; background: var(--accent-soft); color: var(--accent); font-family: var(--mono); font-size: 11px; display: grid; place-items: center; font-style: normal; flex: none; }
.method .m p { margin: 0; font-size: 12.5px; color: var(--text-2); }

/* ── 结论句：标题下面先给答案，图只是佐证 ───────────── */
.takeaway {
  margin: 2px 0 18px; padding: 12px 16px;
  background: var(--accent-soft); border-left: 3px solid var(--accent-fill);
  border-radius: 0 var(--r-sm) var(--r-sm) 0;
  font-size: 14px; line-height: 1.7; color: var(--text);
}
.takeaway b { color: var(--accent); font-weight: 650; font-family: var(--mono); }
.takeaway .tk-label { display: block; font-family: var(--mono); font-size: 10.5px; letter-spacing: .12em; color: var(--text-3); margin-bottom: 5px; }

/* ── 一目了然榜单（AA 式：名字 + 条 + 数字） ─────────── */
.leaderboard { display: grid; gap: 3px; margin-top: 4px; }
.lb-row {
  display: grid; grid-template-columns: 24px minmax(0, 1fr) minmax(60px, 1.1fr) auto;
  align-items: center; gap: 12px; padding: 8px 8px; border-radius: 8px; min-width: 0;
}
.lb-row:hover { background: var(--surface-2); }
.lb-rank { font-family: var(--mono); font-size: 12.5px; color: var(--text-3); text-align: center; }
.lb-row.top .lb-rank { color: var(--accent); font-weight: 700; }
.lb-name { min-width: 0; font-family: var(--mono); font-size: 13px; }
.lb-name a.repo { color: var(--text); text-decoration: none; display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lb-name a.repo:hover { color: var(--accent); text-decoration: underline; text-underline-offset: 3px; }
.lb-name .sub { display: flex; gap: 10px; font-size: 11px; color: var(--text-3); margin-top: 2px; font-family: var(--sans); }
.lb-name .sub .lang { display: inline-flex; align-items: center; gap: 5px; }
.lb-name .sub .lang::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--accent-2); }
.lb-track { background: var(--surface-2); border-radius: 3px; height: 12px; overflow: hidden; min-width: 0; }
.lb-track > i { display: block; height: 100%; border-radius: 3px; background: color-mix(in srgb, var(--accent) 45%, var(--surface-2)); }
.lb-row.top .lb-track > i { background: var(--accent-fill); }
.lb-val { font-family: var(--mono); font-size: 14px; font-weight: 700; color: var(--up); font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; }
@media (max-width: 620px) {
  .lb-row { grid-template-columns: 20px minmax(0, 1fr) auto; gap: 8px; }
  .lb-track { grid-column: 2 / 4; grid-row: 2; height: 6px; }
  .lb-name { grid-column: 2; grid-row: 1; }
  .lb-val { grid-column: 3; grid-row: 1; font-size: 13px; }
}

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

/* ── 顶部锚点导航（B 骨：长页有方向） ─────────────── */
.sitenav {
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px;
  padding: 14px 0; border-bottom: 1px solid var(--border); margin-bottom: 4px;
  font-size: 12.5px;
}
.sitenav .brand { display: inline-flex; align-items: center; gap: 9px; font-family: var(--mono); font-size: 13px; }
.sitenav .brand .mark { width: 24px; height: 24px; border-radius: 7px; background: var(--accent-fill); color: #1b1a17; display: grid; place-items: center; font-weight: 800; }
.sitenav .brand small { color: var(--text-3); font-size: 9.5px; letter-spacing: .12em; }
.sitenav .links { display: flex; flex-wrap: wrap; gap: 4px 16px; }
.sitenav .links a { color: var(--text-2); }
.sitenav .links a:hover { color: var(--accent); text-decoration: underline; text-underline-offset: 3px; }
.sitenav .src { font-family: var(--mono); font-size: 11.5px; color: var(--text-3); }

/* ── 语言构成 ───────────────────────────────────────── */
.langbars { display: grid; gap: 9px; margin-top: 8px; }
.langrow { display: grid; grid-template-columns: 96px minmax(0, 1fr) 44px; gap: 10px; align-items: center; font-size: 12.5px; }
.langrow .n { color: var(--text-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.langrow .t { background: var(--surface-2); border-radius: 4px; height: 10px; overflow: hidden; }
.langrow .t i { display: block; height: 100%; border-radius: 4px; background: color-mix(in srgb, var(--accent) 55%, var(--surface-2)); }
.langrow .c { text-align: right; font-family: var(--mono); color: var(--text-3); font-variant-numeric: tabular-nums; }
.chart-legend { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 12px; color: var(--text-3); margin-top: 12px; }
.chart-legend b { color: var(--text-2); font-family: var(--mono); font-weight: 600; }

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
.fold-body { padding: 4px 0 2px; }
.fold-body .scroll-x { border: 1px solid var(--border); border-radius: var(--r-sm); margin-top: 12px; max-height: 70vh; overflow-y: auto; }
.fold-body table.data th { top: 0; }

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
  .topbar { padding: 28px 0 8px; }
  h1 { font-size: 32px; }
  .lede { font-size: 15px; }
  .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .kpi .v { font-size: 22px; }
  .panel { padding: 15px 15px 17px; }
  .bars { height: 150px; }
  .note { max-width: none; }
}
@media (min-width: 641px) and (max-width: 1000px) {
  .kpis { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}

/* ── 打印 ─────────────────────────────────────────── */
@media print { body { background: var(--bg); } }
"""

CSS = CSS.replace("__TOKENS__", _TOKENS_LIGHT)
DARK_CSS = (
    "@media (prefers-color-scheme: dark) {\n:root {" + _TOKENS_DARK + "}\n}\n"
)
CSS = CSS + DARK_CSS


def page(title: str, body: str, *, script: str = "", lang: str = "zh-CN") -> str:
    """包一个完整 HTML 文档。script 为空时不会插入空的 <script> 标签。

    看板 / 周报保持零 JS：暗色只由 prefers-color-scheme 的纯 CSS 规则承担，
    不注入任何主题脚本。唯一的例外是项目名单页自带的搜索脚本（由调用方传入）。
    """
    head_extra = f"\n{script}" if script else ""
    return (
        f'<!DOCTYPE html>\n<html lang="{lang}">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{esc(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n"
        f'<div class="wrap">\n{body}\n</div>{head_extra}\n</body>\n</html>\n'
    )


# ── HTML 原语 ───────────────────────────────────────────────────
def takeaway(text: str, key: str = "", label: str = "结论") -> str:
    """标题下面的结论句。text 由 narrate.py 算好，这里只负责呈现。

    刻意不放进 .note（灰小字）—— 那会让它退化成又一行说明。
    它是这个视图的答案，视觉权重必须高于注释。
    """
    kid = f' data-key="{esc(key)}"' if key else ""
    return (
        f'<div class="takeaway"{kid}><span class="tk-label">{esc(label)}</span>{text}</div>'
    )


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


def kpi(label: str, value: str, unit: str = "", detail: str = "", spark: str = "",
        *, primary: bool = False) -> str:
    """指标卡。primary=True 的卡带琥珀金竖条，用来标出这一屏的主角。"""
    u = f'<span class="u">{esc(unit)}</span>' if unit else ""
    d = f'<span class="d">{detail}</span>' if detail else ""
    sp = f'<div class="spark">{spark}</div>' if spark else ""
    cls = "kpi primary" if primary else "kpi"
    return (
        f'<div class="{cls}"><span class="k">{esc(label)}</span>'
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
