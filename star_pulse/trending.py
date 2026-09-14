"""GitHub Trending 页面抓取（尽力而为，非关键路径）。

为什么它是「尽力而为」：
  Trending 页没有官方 API，只能解析 HTML，GitHub 随时可能改版；
  而且它显示的是 "stars today"，即使加 ?since=weekly 也不会变成周增量，
  所以它**只能用于候选发现，绝不能当作增量数据源**。
  一旦解析失败，本模块返回空列表并打警告，流水线继续用 Search API 兜底。

我们也刻意不把它当作主数据源：真正决定榜单的是每天的快照。
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser

from .net import Http

log = logging.getLogger("star_pulse.trending")

TRENDING_URL = "https://github.com/trending"

# 这些路径一定不是仓库
_SKIP_OWNERS = {
    "sponsors", "login", "signup", "features", "topics", "collections",
    "trending", "about", "pricing", "marketplace", "orgs", "settings",
    "explore", "notifications", "new", "search",
}
_REPO_HREF = re.compile(r"^/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/?$")
_FALLBACK_LINK = re.compile(r'href="/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)"')


class _TrendingParser(HTMLParser):
    """在 <h2 class="h3 lh-condensed"> 里找第一个仓库链接，并顺手取语言。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hits: list[tuple[str, str]] = []
        self.languages: dict[str, str] = {}
        self._in_h2 = False
        self._took_link = False
        self._current: tuple[str, str] | None = None
        self._capture_lang = False
        self._lang_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        attr = {k: (v or "") for k, v in attrs}

        if tag == "h2" and "lh-condensed" in attr.get("class", ""):
            self._in_h2 = True
            self._took_link = False
            return

        if tag == "a" and self._in_h2 and not self._took_link:
            href = attr.get("href", "")
            m = _REPO_HREF.match(href)
            if m and m.group(1).lower() not in _SKIP_OWNERS:
                self._current = (m.group(1), m.group(2))
                self.hits.append(self._current)
                self._took_link = True
            return

        if tag == "span" and attr.get("itemprop") == "programmingLanguage":
            self._capture_lang = True
            self._lang_buf = []

    def handle_data(self, data):
        if self._capture_lang:
            self._lang_buf.append(data)

    def handle_endtag(self, tag):
        if tag == "h2":
            self._in_h2 = False
            self._took_link = False
        elif tag == "span" and self._capture_lang:
            self._capture_lang = False
            lang = "".join(self._lang_buf).strip()
            if lang and self._current:
                self.languages[f"{self._current[0]}/{self._current[1]}"] = lang
            self._current = None


def parse_trending_html(html: str) -> list[dict]:
    """纯函数：HTML -> 仓库列表。便于离线用固定样本做回归测试。"""
    parser = _TrendingParser()
    try:
        parser.feed(html)
    except Exception as exc:  # HTMLParser 极少抛错，但改版时别让整个流程挂掉
        log.warning("解析 Trending HTML 出错：%s", exc)

    hits = parser.hits
    if not hits:
        # 主解析器没命中 —— GitHub 可能改了 class 名。退化成宽泛正则，
        # 结果噪声更大但比拿到空列表强，同时打警告提示需要更新解析逻辑。
        log.warning("Trending 主解析器未命中任何仓库，启用正则兜底（页面结构可能已变更）")
        for owner, name in _FALLBACK_LINK.findall(html):
            if owner.lower() in _SKIP_OWNERS:
                continue
            hits.append((owner, name))

    out: list[dict] = []
    seen: set[str] = set()
    for owner, name in hits:
        full_name = f"{owner}/{name}"
        key = full_name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "owner": owner,
                "name": name,
                "full_name": full_name,
                "language": parser.languages.get(full_name),
                "rank": len(out) + 1,
            }
        )
    return out


def fetch_trending(http: Http, since: str = "daily", languages: list[str] | None = None) -> list[dict]:
    """抓 Trending 页。任何网络/解析问题都不抛出，只返回空列表。"""
    paths = [""]
    if languages:
        for lang in languages:
            if lang and lang.lower() != "all":
                paths.append(f"/{lang.lower()}")

    results: dict[str, dict] = {}
    for path in paths:
        url = f"{TRENDING_URL}{path}?since={since}"
        # retries=0：这是尽力而为的源，被代理/防火墙拦截时重试只是浪费时间
        resp = http.get_json(url, headers={"Accept": "text/html"}, retries=0)
        if resp is None or resp.status != 200 or not resp.body:
            log.warning(
                "Trending 抓取失败（%s）——如果是代理/防火墙拦截，属预期内，"
                "候选发现会自动改用 Search API 兜底",
                url,
            )
            continue
        html = resp.body.decode("utf-8", errors="replace")
        for item in parse_trending_html(html):
            results.setdefault(item["full_name"].lower(), item)

    repos = list(results.values())
    log.info("Trending 发现 %d 个仓库（since=%s）", len(repos), since)
    return repos
