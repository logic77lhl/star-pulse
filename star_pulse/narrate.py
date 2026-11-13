"""结论层：把「读者要自己从图里归纳的东西」先用一句话说出来。

这一层是**为讲述而生的**。每个视图都由一个 `fact_*` 函数产出一条
`Takeaway`（结论句 + 支撑数字），它同时是：

  * 页面上标题下面那句话（读者扫一眼要的答案）；
  * 未来周报口播 / 图文系列的台词与字幕（`data.json` 里的 `narration` 字段）；
  * 唯一的事实来源 —— 页面与视频共用，不会出现「图里一个数、稿里另一个数」。

铁律（与 analyze.py 一致）：**所有数字都来自 SQL，模型不参与任何计算**。
这里只做「把算好的数排成一句人话」，措辞是确定性的，同一份数据永远同一句。

为什么单独成模块而不是塞进 site.py：site.py 负责排版，这里负责判断。
周报（render.py）和未来的视频脚本可以直接复用同一组函数，不必各写一套归纳。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, asdict

from . import analyze

# 结论里最多点名几个仓库。多了就变成榜单复读，失去「一句话」的意义。
TOP_N_NAMES = 2


@dataclass
class Takeaway:
    """一条结论。text 可直接朗读，facts 是支撑它的结构化数字。"""

    key: str                 # 稳定标识，供视频脚本 / data.json 引用
    text: str                # 一句中文，不含 Markdown
    facts: dict              # 可机读的数字，全部来自 SQL


def _name(row: dict) -> str:
    return str(row.get("full_name") or "—")


def fmt(n: int | float | None) -> str:
    return f"{int(n):,}" if isinstance(n, (int, float)) else "—"


def pct(part: int | float, whole: int | float) -> str:
    if not whole:
        return "—"
    return f"{part / whole * 100:.0f}%"


# ── 榜单类结论 ──────────────────────────────────────────────────
def fact_daily_top(rows: list[dict], total_gain: int) -> Takeaway:
    """当日涨幅榜：谁最猛、前几名占了当天多少。"""
    if not rows:
        return Takeaway("daily_top", "今天还没有可比的两天快照。", {"rows": 0})
    first = rows[0]
    second = rows[1] if len(rows) > 1 else None
    share_num = first["delta"] / total_gain if total_gain else 0
    if share_num >= 0.25:
        tail = f"光是第一名就占了当日全部增量的 {pct(first['delta'], total_gain)}。"
    elif share_num > 0:
        tail = f"头名只占当日增量的 {pct(first['delta'], total_gain)}，涨得相当分散。"
    else:
        tail = ""
    if second:
        text = (
            f"今天涨得最快的是 {_name(first)}，+{fmt(first['delta'])}；"
            f"其次是 {_name(second)}，+{fmt(second['delta'])}。"
            f"{tail}"
        )
    else:
        text = f"今天涨得最快的是 {_name(first)}，+{fmt(first['delta'])}。{tail}"
    return Takeaway(
        "daily_top", text,
        {"leader": _name(first), "leader_delta": int(first["delta"]),
         "runner_up": _name(second) if second else None,
         "runner_up_delta": int(second["delta"]) if second else None,
         "leader_share_pct": round(first["delta"] / total_gain * 100) if total_gain else None,
         "total_gain": total_gain},
    )


def fact_cumulative_top(rows: list[dict], comparable: int, first_day: str,
                        last_day: str) -> Takeaway:
    """累计增长榜：全窗口谁涨得最多，以及可比口径说明。"""
    if not rows:
        return Takeaway("cumulative_top", "全窗口还没有可比的增量。", {"rows": 0})
    first = rows[0]
    second = rows[1] if len(rows) > 1 else None
    text = (
        f"{first_day} 到 {last_day} 这段时间，{_name(first)} 累计涨了 +{fmt(first['delta'])}，"
        f"是榜首；"
    )
    if second:
        text += f"{_name(second)} 以 +{fmt(second['delta'])} 紧随其后。"
    text += f"这个榜只算两端都有快照的 {fmt(comparable)} 个仓库，窗口对所有人一致。"
    return Takeaway(
        "cumulative_top", text,
        {"leader": _name(first), "leader_delta": int(first["delta"]),
         "runner_up": _name(second) if second else None,
         "runner_up_delta": int(second["delta"]) if second else None,
         "comparable": comparable, "start": first_day, "end": last_day},
    )


def fact_today(summary: list[dict], today: dict, yesterday: dict | None) -> Takeaway:
    """今日总览：今天热不热闹，放在 61 天里是什么位置。"""
    core = [s for s in summary if not s["is_first"]]
    gains = [s["total_gain"] for s in core]
    today_gain = today["total_gain"]
    avg = round(sum(gains) / len(gains)) if gains else 0
    peak = max(core, key=lambda s: s["total_gain"]) if core else None
    rank = 1 + sum(1 for g in gains if g > today_gain)  # 从高到低，今天是第几
    wow = None
    if yesterday and yesterday["total_gain"]:
        wow = round((today_gain - yesterday["total_gain"]) / yesterday["total_gain"] * 100)

    if wow is not None and wow <= -30:
        tone = f"比前一天少了 {abs(wow)}%，是明显偏冷的一天"
    elif wow is not None and wow >= 30:
        tone = f"比前一天多了 {wow}%，是明显偏热的一天"
    elif today_gain < avg:
        tone = f"低于 {len(core)} 天均值（+{fmt(avg)}）"
    else:
        tone = f"高于 {len(core)} 天均值（+{fmt(avg)}）"

    text = f"今天全网新增 +{fmt(today_gain)} 颗星，{tone}；在已记录的 {len(core)} 天里排第 {rank}。"
    if peak and peak["date"] != today["date"]:
        text += f"最热闹的一天是 {peak['date']}（+{fmt(peak['total_gain'])}）。"
    return Takeaway(
        "today", text,
        {"total_gain": today_gain, "avg_gain": avg, "days": len(core),
         "rank_by_gain": rank, "wow_pct": wow, "gainers": today["gainers"],
         "peak_date": peak["date"] if peak else None,
         "peak_gain": peak["total_gain"] if peak else None},
    )


def fact_languages(langs: list[dict], total_repos: int) -> Takeaway:
    """语言构成：哪种语言最多、占比多少。"""
    if not langs:
        return Takeaway("languages", "还没有语言分布数据。", {"rows": 0})
    top = langs[0]
    text = (
        f"追踪的 {fmt(total_repos)} 个项目里，{top['lang']} 最多，{fmt(top['n'])} 个，"
        f"占 {pct(top['n'], total_repos)}；"
    )
    if len(langs) > 1:
        text += f"其次是 {langs[1]['lang']}（{fmt(langs[1]['n'])}）。"
    return Takeaway(
        "languages", text,
        {"top": top["lang"], "top_n": top["n"], "top_pct": round(top["n"] / total_repos * 100)
         if total_repos else None, "distinct": len(langs), "total_repos": total_repos},
    )


def fact_breadth(today: dict, projects_n: int) -> Takeaway:
    """上涨面：多少个项目在涨，涨得是不是集中。"""
    gainers = today["gainers"]
    share = pct(gainers, projects_n) if projects_n else "—"
    text = f"今天有 {fmt(gainers)} 个项目在涨，占追踪池的 {share}。"
    return Takeaway(
        "breadth", text,
        {"gainers": gainers, "tracked": projects_n,
         "gainers_pct": round(gainers / projects_n * 100) if projects_n else None},
    )


# ── 汇总：一次算出全部结论，供 site.py / 周报 / 视频共用 ──────────
def build_narration(
    conn: sqlite3.Connection, dates: list[str], projects_n: int,
    daily_top: list[dict], cumulative: list[dict], comparable: int,
    langs: list[dict],
) -> dict:
    """返回 {key: {...}} 的纯 dict（可直接 json.dumps 进 data.json）。

    这是「看板 + 周报 + 未来口播」共享的唯一事实来源。加新结论只需加一个
    fact_* 并在这里挂上，三端自动同步。
    """
    summary = analyze.daily_summary(conn)
    core = [s for s in summary if not s["is_first"]]
    if not core:
        return {"_warm": False, "takeaways": {}}

    today = core[-1]
    yesterday = core[-2] if len(core) >= 2 else None
    first_day, last_day = dates[0], dates[-1]

    items = [
        fact_today(summary, today, yesterday),
        fact_daily_top(daily_top, today["total_gain"]),
        fact_cumulative_top(cumulative, comparable, first_day, last_day),
        fact_breadth(today, projects_n),
        fact_languages(langs, projects_n),
    ]
    return {
        "_warm": True,
        "date": last_day,
        "window": [first_day, last_day],
        "takeaways": {t.key: asdict(t) for t in items},
        # 口播串联顺序：先总览、再当日冠军、再累计、再广度、再语言。
        # 视频脚本可直接按这个顺序念 text，不必自己组织逻辑。
        "spoken_order": [t.key for t in items],
    }
