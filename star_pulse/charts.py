"""图表：全部服务端渲染成内联 SVG / CSS，不用任何 JS 图表库。

为什么不用 Chart.js（此前是从 cdnjs 加载的）：

1. 本项目立身之本是「零第三方依赖、只用标准库」。为了两张图引入一个外部 CDN
   脚本，是这个项目里唯一一处破例，而且它换来的东西并不值这个价。
2. 离线 / CDN 被墙 / 加载失败时，页面会静默变成一片空白（代码里那句
   「图表库没能从 CDN 加载」的兜底就是证据）。服务端渲染的 SVG 没有这个问题。
3. 折叠区里的 canvas 在展开前量不到尺寸，Chart.js 会按 0×0 初始化，必须靠
   `toggle` 事件延后创建。SVG 是普通 DOM，根本没有这个时序问题 —— 这个
   workaround 连同它的注释一起消失了。
4. 停用 JS 也仍然看得到图。

两条实现约束：

* **文字留在 HTML 里，不要塞进 SVG。** SVG 用 viewBox 缩放时里面的文字会一起
  缩放，窄屏上会变成蚂蚁字。所以柱状图用 CSS flex 实现（标签是真实文本），
  折线图只保留少量轴标签，图例放在 HTML 里。
* **折线图给 `min-width` 并允许横向滚动。** 8 条曲线挤进 390px 没有可读性，
  与其缩放到看不清，不如让它滚。
"""

from __future__ import annotations

import math

# 曲线配色。刻意不用主题令牌：这是「一组要互相区分」的色板，
# 不是语义色（语义色只有涨/跌两种），塞进令牌表反而会让它和 --up/--down 混淆。
PALETTE = [
    "#7F77DD", "#1D9E75", "#D85A30", "#378ADD",
    "#BA7517", "#D4537E", "#639922", "#9C6ADE",
]


def _abbrev(n: float) -> str:
    """轴标签用的紧凑数字：76125 -> 76k，1234567 -> 1.2M。"""
    n = float(n)
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.0f}k"
    return f"{n:.0f}"


def _nice_ticks(max_value: float, count: int = 4) -> list[float]:
    """把最大值切成好看的刻度（1/2/2.5/5 × 10^n）。"""
    if max_value <= 0:
        return [0.0]
    raw = max_value / count
    mag = 10 ** math.floor(math.log10(raw))
    step = mag * 10
    for m in (1, 2, 2.5, 5, 10):
        if mag * m >= raw:
            step = mag * m
            break
    ticks, v = [], 0.0
    while v < max_value - 1e-9:
        ticks.append(v)
        v += step
    ticks.append(v)
    return ticks


def sparkline(values: list[float], *, width: int = 120, height: int = 30) -> str:
    """KPI 卡里的迷你走势线。没有数据时返回空串（调用方直接不渲染）。"""
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    pad = 3.0
    inner_h = height - pad * 2
    step = width / (len(vals) - 1)
    pts = [
        (i * step, pad + inner_h - (v - lo) / span * inner_h)
        for i, v in enumerate(vals)
    ]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = (
        f"M0,{height - pad:.1f} L" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        + f" L{width:.1f},{height - pad:.1f} Z"
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
        f'role="img" aria-label="走势" style="width:100%;height:{height}px">'
        f'<path d="{area}" fill="var(--accent-soft)" stroke="none"/>'
        f'<polyline points="{line}" fill="none" stroke="var(--accent-2)" '
        f'stroke-width="1.6" vector-effect="non-scaling-stroke" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f"</svg>"
    )


def css_bars(labels: list[str], values: list[float], *, unit: str = "stars") -> str:
    """CSS flex 柱状图。完全响应式，标签是真实文本，窄屏不会被缩放。

    数值走原生 `title` 属性，不用绝对定位的自绘 tooltip：后者是 `nowrap` 的绝对
    定位盒子，首尾两根柱子的提示框会横向溢出、把整页撑宽（实测 +49px）。精确数值
    下方表格里本来就有，为悬浮效果承担布局风险不值得。
    """
    if not values:
        return '<p class="muted">还没有可比的两天数据。</p>'
    hi = max(values) or 1
    cols = []
    for label, value in zip(labels, values):
        pct = max(1.5, value / hi * 100)
        cols.append(
            f'<div class="col" title="{label}　+{value:,.0f} {unit}">'
            f'<i style="height:{pct:.2f}%"></i></div>'
        )
    xlabels = "".join(f"<span>{label[5:]}</span>" for label in labels)  # 只留 MM-DD
    return (
        '<div class="bars">' + "".join(cols) + "</div>"
        '<div class="bars-x">' + xlabels + "</div>"
    )


def line_chart(
    labels: list[str],
    series: list[dict],
    *,
    width: int = 900,
    height: int = 250,
) -> str:
    """多序列折线图。series: [{"name": str, "values": list[float]}, ...]

    y 轴从 0 起（都是增量，负数没有意义）；x 轴只标首/中/末几个日期，
    避免 26 个日期糊成一团。图例由调用方在 HTML 里渲染（见模块 docstring）。
    """
    if not labels or not series:
        return '<p class="muted">还没有足够的数据画走势。</p>'

    pad_l, pad_r, pad_t, pad_b = 58, 14, 14, 28
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    all_values = [v for s in series for v in s["values"]]
    hi = max(all_values) if all_values else 1
    ticks = _nice_ticks(hi)
    top = ticks[-1] or 1

    def x_at(i: int) -> float:
        if len(labels) == 1:
            return pad_l + plot_w / 2
        return pad_l + plot_w * i / (len(labels) - 1)

    def y_at(v: float) -> float:
        return pad_t + plot_h - (v / top) * plot_h

    parts: list[str] = []

    # 网格线 + y 轴刻度
    for tick in ticks:
        y = y_at(tick)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
            f'stroke="var(--grid)" stroke-width="1" vector-effect="non-scaling-stroke"/>'
        )
        parts.append(
            f'<text x="{pad_l - 9}" y="{y + 3.5:.1f}" text-anchor="end" '
            f'font-size="11" fill="var(--text-3)">+{_abbrev(tick)}</text>'
        )

    # x 轴标签：最多 6 个，均匀取样
    n = len(labels)
    show = min(6, n)
    idxs = sorted({round(i * (n - 1) / max(1, show - 1)) for i in range(show)}) if n > 1 else [0]
    for i in idxs:
        parts.append(
            f'<text x="{x_at(i):.1f}" y="{height - 9}" text-anchor="middle" '
            f'font-size="11" fill="var(--text-3)">{labels[i][5:]}</text>'
        )

    # 曲线
    for si, s in enumerate(series):
        color = PALETTE[si % len(PALETTE)]
        pts = [(x_at(i), y_at(v)) for i, v in enumerate(s["values"])]
        if len(pts) < 2:
            continue
        d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        parts.append(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" '
            f'vector-effect="non-scaling-stroke" stroke-linejoin="round" '
            f'stroke-linecap="round" opacity=".92"/>'
        )
        # 只标末点，点太多会糊
        lx, ly = pts[-1]
        parts.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="2.6" fill="{color}"/>')

    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="领涨仓库累计增量走势" preserveAspectRatio="xMidYMid meet">'
        + "".join(parts)
        + "</svg>"
    )


def hbar_chart(labels: list[str], values: list[float], *, unit: str = "stars") -> str:
    """横向条形榜。纯 CSS，标签是真实文本，窄屏自动换行不会被缩放。

    取代此前周报里的 Chart.js 横向柱状图 —— 同样是为了去掉 CDN 依赖。
    """
    if not values:
        return '<p class="muted">没有可画的数据。</p>'
    hi = max(values) or 1
    rows = []
    for label, value in zip(labels, values):
        pct = max(1.0, value / hi * 100)
        rows.append(
            f'<div class="hbar" title="{label}　+{value:,.0f} {unit}">'
            f'<span class="lbl">{label}</span>'
            f'<span class="track"><i style="width:{pct:.2f}%"></i></span>'
            f'<span class="val">+{value:,.0f}</span></div>'
        )
    return '<div class="hbars">' + "".join(rows) + "</div>"


def legend(names: list[str]) -> str:
    """折线图的 HTML 图例。放在 SVG 外面，文字才不会被 viewBox 缩放。"""
    if not names:
        return ""
    items = "".join(
        f'<span><i style="background:{PALETTE[i % len(PALETTE)]}"></i>{name}</span>'
        for i, name in enumerate(names)
    )
    return f'<div class="legend">{items}</div>'
