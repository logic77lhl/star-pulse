"""配置加载。

优先级：环境变量 > .env 文件 > config/settings.toml 默认值。
CI 里由 GitHub Actions 注入的 secret 会直接命中环境变量，因此 .env 不会覆盖它。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT = PACKAGE_DIR.parent


def load_dotenv(path: Path) -> None:
    """极简 .env 解析器。用 setdefault 保证真实环境变量优先。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


@dataclass
class Settings:
    root: Path
    db_path: Path
    snapshots_dir: Path
    reports_dir: Path
    logs_dir: Path

    # 认证：None 表示匿名模式，配额只有 60 次/小时
    token: str | None = None

    # 候选池
    max_candidates: int = 800
    search_lookback_days: int = 14
    search_min_stars: int = 50
    languages: list[str] = field(default_factory=lambda: ["All"])
    trending_since: str = "daily"
    trending_per_language: bool = False
    seed_repos: list[str] = field(default_factory=list)

    # 报告
    top_n: int = 20
    tz_offset_hours: int = 8
    max_span_days: int = 10

    # 网络
    request_gap_seconds: float = 0.15
    max_wait_seconds: int = 1800
    timeout_seconds: int = 30
    retries: int = 3

    # LLM（可选）
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    user_agent: str = "star-pulse/1.0 (+https://github.com/)"

    # ── 时区工具 ────────────────────────────────────────────────
    @property
    def tz(self) -> timezone:
        return timezone(timedelta(hours=self.tz_offset_hours))

    def now_local(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> str:
        """当前日期（按配置时区归属），YYYY-MM-DD。"""
        return self.now_local().date().isoformat()

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key and self.llm_base_url and self.llm_model)

    # ── 匿名模式保护 ────────────────────────────────────────────
    @property
    def rate_limit_per_hour(self) -> int:
        return 5000 if self.token else 60

    def budget_check(self, needed_requests: int) -> str | None:
        """开跑前预估请求量，超预算就返回警告文本（而不是跑到一半被 403 打断）。"""
        if needed_requests <= self.rate_limit_per_hour:
            return None
        return (
            f"本次需要约 {needed_requests} 次 API 请求，但当前配额只有 "
            f"{self.rate_limit_per_hour} 次/小时"
            f"（{'已认证' if self.token else '匿名模式'}）。\n"
            f"请配置 GITHUB_TOKEN（见 .env.example），或把 config/settings.toml 里的 "
            f"max_candidates 调到 {max(1, self.rate_limit_per_hour - 20)} 以内。"
        )


def _read_watchlist(path: Path) -> list[str]:
    if not path.is_file():
        return []
    repos: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "/" not in line:
            continue
        name = line.strip().strip("/")
        if name.count("/") != 1:
            continue
        key = name.lower()
        if key not in seen:
            seen.add(key)
            repos.append(name)
    return repos


def load_settings(root: Path | None = None) -> Settings:
    root = (root or ROOT).resolve()
    load_dotenv(root / ".env")

    cfg_path = root / "config" / "settings.toml"
    cfg: dict = {}
    if cfg_path.is_file():
        with cfg_path.open("rb") as fh:
            cfg = tomllib.load(fh)

    cand = cfg.get("candidate", {})
    rep = cfg.get("report", {})
    net = cfg.get("network", {})

    s = Settings(
        root=root,
        db_path=root / "data" / "pulse.db",
        snapshots_dir=root / "data" / "snapshots",
        reports_dir=root / "reports",
        logs_dir=root / "logs",
        token=os.environ.get("GITHUB_TOKEN") or None,
        max_candidates=int(cand.get("max_candidates", 800)),
        search_lookback_days=int(cand.get("search_lookback_days", 14)),
        search_min_stars=int(cand.get("search_min_stars", 50)),
        languages=list(cand.get("languages", ["All"])),
        trending_since=str(cand.get("trending_since", "daily")),
        trending_per_language=bool(cand.get("trending_per_language", False)),
        seed_repos=_read_watchlist(root / "config" / "watchlist.txt"),
        top_n=int(rep.get("top_n", 20)),
        tz_offset_hours=int(rep.get("tz_offset_hours", 8)),
        max_span_days=int(rep.get("max_span_days", 10)),
        request_gap_seconds=float(net.get("request_gap_seconds", 0.15)),
        max_wait_seconds=int(net.get("max_wait_seconds", 1800)),
        timeout_seconds=int(net.get("timeout_seconds", 30)),
        retries=int(net.get("retries", 3)),
        llm_base_url=os.environ.get("LLM_BASE_URL", "").rstrip("/"),
        llm_api_key=os.environ.get("LLM_API_KEY", ""),
        llm_model=os.environ.get("LLM_MODEL", ""),
    )
    return s
