"""可选的 LLM 中文解读。

严格边界：LLM **只**接收结构化字段并回写一句中文描述，不接触任何计算。
如果没配 API Key，本模块整体静默跳过，榜单与数据功能完全不受影响。
任何异常都被吞掉并返回空字典——解读是锦上添花，绝不能因为它让整份报告失败。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from .config import Settings

log = logging.getLogger("star_pulse.llm")

_SYSTEM = (
    "你是开源项目观察者。给你一批 GitHub 项目的结构化数据，"
    "为每个项目写一句不超过 40 字的中文说明，讲清楚它解决什么问题。"
    "只依据给定的 description 和你对项目的了解，禁止编造数字、星数或排名。"
    "输出必须是严格的 JSON 对象，键为仓库全名，值为中文说明字符串，不要任何其他文字。"
)


def narrate(settings: Settings, items: list[dict]) -> dict[str, str]:
    if not settings.llm_enabled or not items:
        return {}

    payload = [
        {
            "full_name": it.get("full_name"),
            "description": (it.get("description") or "")[:400],
            "language": it.get("language"),
            "category": it.get("category"),
        }
        for it in items
    ]
    body = {
        "model": settings.llm_model,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    }

    req = urllib.request.Request(
        f"{settings.llm_base_url}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.llm_api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        content = data["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.strip("`")
            content = content.split("\n", 1)[-1] if "\n" in content else content
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
        log.warning("LLM 返回的不是字典，忽略解读")
    except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError, TimeoutError) as exc:
        log.warning("LLM 解读失败（不影响报告生成）：%s", exc)
    return {}
