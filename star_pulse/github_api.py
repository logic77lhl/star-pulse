"""GitHub REST API 封装。

覆盖两个端点：
  /repos/{owner}/{repo}         —— 每日快照的主链路，返回当前总星数
  /search/repositories          —— 候选发现（找近期创建的新项目）

两个端点的配额是**相互独立**的，这点很容易被忽略：
  主 REST  ：匿名 60/小时，认证 5000/小时
  Search   ：匿名 10/分钟，认证 30/分钟（且搜索结果最多返回 1000 条）
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .net import Http

log = logging.getLogger("star_pulse.github")

API = "https://api.github.com"
SEARCH_RESULT_CAP = 1000

# 每页条数刻意不用上限 100。
# 实测：单页 100 条时响应体约 550 KB，会被中间代理在 ~512 KB 处截断，
# 抛出 IncompleteRead 并导致该页永久失败。降到 50 条（约 275 KB）可稳定通过。
# 代价只是多几个请求，而 Search 配额（认证 30 次/分钟）完全够用。
SEARCH_PER_PAGE = 50


def normalize_repo(raw: dict) -> dict | None:
    """把 API 返回的仓库对象映射成 repo 表需要的字段。"""
    if not raw or not raw.get("id") or not raw.get("full_name"):
        return None
    lic = raw.get("license") or {}
    return {
        "repo_id": int(raw["id"]),
        "full_name": raw["full_name"],
        "owner": (raw.get("owner") or {}).get("login") or raw["full_name"].split("/")[0],
        "name": raw.get("name") or raw["full_name"].split("/")[-1],
        "description": raw.get("description"),
        "language": raw.get("language"),
        "topics": raw.get("topics") or [],
        "homepage": raw.get("homepage"),
        "license": lic.get("spdx_id") if isinstance(lic, dict) else None,
        "repo_created": (raw.get("created_at") or "")[:10] or None,
        "is_archived": 1 if raw.get("archived") else 0,
        "is_fork": 1 if raw.get("fork") else 0,
    }


def with_stats(repo: dict, raw: dict) -> dict:
    """把当前星数/叉数等挂到规范化后的仓库对象上（键名带下划线，不落库）。

    这一步很关键：Search API 返回的 items 里同样带 stargazers_count，
    所以搜索发现的新项目可以顺手完成快照，不必再单独发一次 /repos 请求。
    """
    repo["_stars"] = int(raw.get("stargazers_count") or 0)
    repo["_forks"] = int(raw.get("forks_count") or 0)
    repo["_open_issues"] = int(raw.get("open_issues_count") or 0)
    repo["_pushed_at"] = raw.get("pushed_at")
    return repo


def get_repo(http: Http, full_name: str) -> dict | None:
    """取单个仓库的当前状态。私有/已删除返回 None。"""
    resp = http.get_json(f"{API}/repos/{full_name}", allow_404=True)
    if resp is None or resp.status != 200:
        if resp is not None and resp.status != 404:
            log.warning("取仓库失败 %s: HTTP %s", full_name, resp.status)
        return None
    raw = resp.json() or {}
    repo = normalize_repo(raw)
    if repo is None:
        return None
    return with_stats(repo, raw)


def search_recent_repos(
    http: Http,
    lookback_days: int,
    min_stars: int,
    language: str | None = None,
    max_items: int = 300,
) -> list[dict]:
    """搜索「最近 N 天创建且已有一定星数」的仓库，按星数降序。

    这是冷启动当天就能出榜的关键：它不依赖任何历史快照。
    注意 Search API 最多只返回 1000 条结果（第 11 页会返回 422），
    因此这里显式限制页数，并用 stars 门槛控制结果集大小。
    """
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    query = f"created:>{since} stars:>{min_stars}"
    if language and language.lower() != "all":
        query += f" language:{language}"

    collected: list[dict] = []
    max_pages = min(
        SEARCH_RESULT_CAP // SEARCH_PER_PAGE,
        max(1, -(-max_items // SEARCH_PER_PAGE)),  # 向上取整
    )
    for page in range(1, max_pages + 1):
        resp = http.get_json(
            f"{API}/search/repositories",
            params={
                "q": query,
                "sort": "stars",
                "order": "desc",
                "per_page": SEARCH_PER_PAGE,
                "page": page,
            },
        )
        if resp is None:
            # 不要静默退出：这会让候选池悄悄变小，而日志里什么都看不出来
            log.warning("搜索第 %d 页请求失败，已获取 %d 条后中止", page, len(collected))
            break
        if resp.status == 422:
            log.info("搜索已达 %d 条上限，停止翻页（q=%s）", SEARCH_RESULT_CAP, query)
            break
        if resp.status != 200:
            log.warning("搜索失败 HTTP %s（q=%s，第 %d 页）", resp.status, query, page)
            break
        data = resp.json() or {}
        items = data.get("items") or []
        if not items:
            break
        collected.extend(items)
        if len(items) < SEARCH_PER_PAGE or len(collected) >= data.get("total_count", 0):
            break

    repos: list[dict] = []
    for it in collected:
        normalized = normalize_repo(it)
        if normalized:
            repos.append(with_stats(normalized, it))
    log.info("搜索发现 %d 个新项目（q=%s）", len(repos), query)
    return repos[:max_items]
