"""限流感知的 HTTP 客户端（仅用标准库，无第三方依赖）。

为什么不直接用 urllib 裸调：这个项目每天要发几百次请求，能否长期稳定跑
几乎完全取决于对限流的处理是否得体。这里做了四件事：

1. 每次响应都读 x-ratelimit-remaining / x-ratelimit-reset，配额将尽时主动
   等待到重置时间——而不是等 GitHub 回一个 403 把整轮采集打断。
2. 429 / 403 / 5xx 指数退避重试；403 且 remaining=0 时精确等到 reset。
3. 请求间隔 + 全串行，规避 GitHub 的二级限流（并发 <=100、REST <=900 点/分钟）。
4. 累计请求数与等待时长，运行结束时可打印，方便判断配额是否够用。
"""

from __future__ import annotations

import http.client
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger("star_pulse.net")

# 需要重试的瞬时网络故障。
# 特别注意 http.client.HTTPException：IncompleteRead 就在这里，
# 它是代理/网关截断响应体时抛出的，**不是 OSError 的子类**。
# 早先版本漏了它，导致一次响应截断就打挂整轮采集（实测踩到）。
TRANSIENT_ERRORS = (
    urllib.error.URLError,
    http.client.HTTPException,
    TimeoutError,
    ConnectionError,
    OSError,
)


class BudgetExceeded(RuntimeError):
    """等待时间超过 max_wait_seconds，主动放弃本轮，避免 CI 卡死。"""


@dataclass
class RateLimit:
    limit: int | None = None
    remaining: int | None = None
    reset: int | None = None
    resource: str | None = None

    @staticmethod
    def _as_int(value):
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def update(self, headers: dict) -> None:
        self.limit = self._as_int(headers.get("x-ratelimit-limit")) or self.limit
        self.remaining = self._as_int(headers.get("x-ratelimit-remaining"))
        self.reset = self._as_int(headers.get("x-ratelimit-reset")) or self.reset
        self.resource = headers.get("x-ratelimit-resource") or self.resource

    def seconds_until_reset(self) -> float:
        if not self.reset:
            return 0.0
        return max(0.0, self.reset - time.time())

    def describe(self) -> str:
        if self.remaining is None:
            return "配额未知"
        return f"剩余 {self.remaining}/{self.limit}，{self.seconds_until_reset():.0f}s 后重置"


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes
    url: str

    def json(self):
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None


@dataclass
class Http:
    user_agent: str
    token: str | None = None
    request_gap: float = 0.15
    max_wait: int = 1800
    timeout: int = 30
    retries: int = 3

    rate: RateLimit = field(default_factory=RateLimit)
    requests_made: int = 0
    seconds_waited: float = 0.0
    _last_request_at: float = 0.0

    # ── 内部工具 ────────────────────────────────────────────────
    def _sleep(self, seconds: float, reason: str) -> None:
        if seconds <= 0:
            return
        if self.seconds_waited + seconds > self.max_wait:
            raise BudgetExceeded(
                f"需要等待 {seconds:.0f}s（{reason}），累计等待将超过上限 "
                f"{self.max_wait}s。已发 {self.requests_made} 次请求。"
                f"请配置 GITHUB_TOKEN 或减少候选池规模。"
            )
        log.warning("等待 %.0fs：%s", seconds, reason)
        time.sleep(seconds)
        self.seconds_waited += seconds

    def _respect_gap(self) -> None:
        elapsed = time.time() - self._last_request_at
        gap = self.request_gap - elapsed
        if gap > 0:
            time.sleep(gap)

    def _preflight_quota(self) -> None:
        """配额见底时提前等待，避免撞 403。"""
        if self.rate.remaining is not None and self.rate.remaining <= 1:
            wait = self.rate.seconds_until_reset() + 2
            self._sleep(wait, f"配额仅剩 {self.rate.remaining}（{self.rate.describe()}）")

    # ── 主入口 ──────────────────────────────────────────────────
    def get_json(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        allow_404: bool = False,
        retries: int | None = None,
    ) -> Response | None:
        """retries 可覆盖实例默认值。对「尽力而为」的请求（如 Trending 页）
        传 retries=0 更合适——被防火墙拦截时重试只会白白浪费时间。"""
        max_attempts = self.retries if retries is None else retries
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        hdrs = {
            "User-Agent": self.user_agent,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            hdrs["Authorization"] = f"Bearer {self.token}"
        if headers:
            hdrs.update(headers)

        attempt = 0
        while True:
            attempt += 1
            self._preflight_quota()
            self._respect_gap()

            req = urllib.request.Request(url, headers=hdrs, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read()
                    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                    self._last_request_at = time.time()
                    self.requests_made += 1
                    self.rate.update(resp_headers)
                    return Response(resp.status, resp_headers, body, url)

            except urllib.error.HTTPError as exc:
                self._last_request_at = time.time()
                self.requests_made += 1
                resp_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
                self.rate.update(resp_headers)
                body = exc.read()

                if exc.code == 404 and allow_404:
                    return None
                if exc.code in (403, 429) and self.rate.remaining == 0:
                    wait = self.rate.seconds_until_reset() + 3
                    self._sleep(wait, "配额耗尽（403/429）")
                    if attempt <= max_attempts:
                        continue
                if exc.code in (403, 429, 500, 502, 503, 504) and attempt <= max_attempts:
                    backoff = min(60, 2 ** attempt)
                    retry_after = resp_headers.get("retry-after")
                    if retry_after and retry_after.isdigit():
                        backoff = max(backoff, int(retry_after))
                    self._sleep(backoff, f"HTTP {exc.code}，第 {attempt} 次重试")
                    continue
                log.error("HTTP %s %s -> %s", exc.code, url, body[:200])
                return Response(exc.code, resp_headers, body, url)

            except TRANSIENT_ERRORS as exc:
                self._last_request_at = time.time()
                if attempt <= max_attempts:
                    backoff = min(60, 2 ** attempt)
                    self._sleep(backoff, f"网络错误 {type(exc).__name__}，第 {attempt} 次重试")
                    continue
                log.error("网络请求最终失败 %s -> %s: %s", url, type(exc).__name__, exc)
                return None

    def stats(self) -> str:
        return (
            f"共 {self.requests_made} 次请求，限流等待 {self.seconds_waited:.0f}s，"
            f"{self.rate.describe()}"
        )
