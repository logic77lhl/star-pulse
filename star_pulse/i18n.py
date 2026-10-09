"""简介中文化：模型翻译 + 本地缓存。

为什么要有缓存
--------------
翻译是**一次性的昂贵动作**：同一个仓库的简介不会天天变，但看板是天天重建的。
把结果落在 `data/i18n/zh.json`（**提交进 git**），于是一个仓库一辈子只翻一次，
之后每天、每次 CI 重建都直接复用，不再产生任何调用。

几条刻意的设计
--------------
* 缓存以 `repo_id` 为键，不用 `full_name` —— 仓库改名不应该导致重新翻译。
* `by` 字段区分 `"llm"` 与 `"human"`，页面上据此标注哪些是机翻。
  机器翻译就是机器翻译，不冒充人工质量。
* 与 `llm.py` 同样的边界：**只产出说明文字，绝不参与任何数值计算**。
  星数、增量、排名永远来自 SQL。
* 所有异常都在内部吞掉：翻译只是锦上添花，缺了就是显示英文原文，
  绝不能因此让整站构建失败。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import Settings

log = logging.getLogger("star_pulse.i18n")

CACHE_VERSION = 1

_SYSTEM = (
    "你在为一个中文的 GitHub 项目看板翻译仓库简介。"
    "把每条英文简介翻译成简洁、通顺的简体中文，控制在 40 字以内，"
    "说清楚这个项目是做什么的。\n"
    "要求：\n"
    "1. 产品名、库名、协议名、命令名保留原文（如 React、MCP、CLI、Docker）；\n"
    "2. 不要添加原文没有的功能、数字、评价或营销词；\n"
    "3. 不要翻译仓库名本身，也不要输出任何解释性文字；\n"
    "4. 输出必须是严格的 JSON 对象，键为传入的 id，值为中文字符串。"
)


def cache_path(settings: Settings):
    return settings.root / "data" / "i18n" / "zh.json"


def load_cache(settings: Settings) -> dict[str, dict]:
    """读取翻译缓存。文件不存在或坏了都只返回空字典，不抛异常。"""
    path = cache_path(settings)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("翻译缓存不可读，按空缓存处理（页面会显示英文原文）：%s", exc)
        return {}
    items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, dict):
        log.warning("翻译缓存结构异常，按空缓存处理")
        return {}
    return items


def save_cache(settings: Settings, items: dict[str, dict]) -> str:
    path = cache_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": CACHE_VERSION,
                "note": "GitHub 仓库简介的中文翻译缓存。键是 repo_id（仓库改名不会失效）。"
                        "由 python -m star_pulse translate 生成，请提交进 git 以便 CI 复用。",
                "items": items,
            },
            ensure_ascii=False,
            indent=1,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return str(path)


def _call_llm(settings: Settings, payload: list[dict]) -> dict[str, str]:
    body = {
        "model": settings.llm_model,
        "temperature": 0.2,
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
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    content = data["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = content.strip("`")
        content = content.split("\n", 1)[-1] if "\n" in content else content
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("模型没有返回 JSON 对象")
    return {str(k): str(v).strip() for k, v in parsed.items()}


# ── 免费后端：MyMemory ──────────────────────────────────────────
# 不需要任何密钥。匿名 5000 字符/天；请求里带一个邮箱即提到 50000 字符/天。
# 官方限额说明：https://mymemory.translated.net/doc/usagelimits.php
_MYMEMORY_ENDPOINT = "https://api.mymemory.translated.net/get"


def _mymemory_one(settings: Settings, text: str) -> str | None:
    """翻一条。返回 None 表示「对方没有给出可用译文」。"""
    params = {"q": text, "langpair": "en|zh-CN", "mt": "1"}
    if settings.i18n_email:
        params["de"] = settings.i18n_email
    req = urllib.request.Request(
        _MYMEMORY_ENDPOINT + "?" + urllib.parse.urlencode(params),
        headers={"User-Agent": settings.user_agent},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    if int(data.get("responseStatus") or 0) != 200:
        raise ValueError(str(data.get("responseDetails") or "MyMemory 返回非 200"))
    out = ((data.get("responseData") or {}).get("translatedText") or "").strip()
    # 没命中译文时对方会把原文回显回来 —— 绝不能把英文原文当成中文译文缓存下来，
    # 否则页面上会出现「中文列里是英文」这种看起来正常、实际错位的数据。
    if not out or out.lower() == text.strip().lower():
        return None
    return out


def translate_missing(
    settings: Settings, items: list[dict], batch_size: int = 60
) -> dict[str, int]:
    """把还没翻译过的仓库简介翻成中文并写回缓存。

    items 每项需含 repo_id / full_name / description（其余可选，用于辅助理解）。
    后端由 settings.translator 决定（见 config.Settings.translator）。
    返回统计字典；**任何失败都不抛异常**。
    """
    stats = {"candidates": 0, "translated": 0, "skipped_cached": 0,
             "skipped_empty": 0, "failed_batches": 0}

    backend = settings.translator
    stats["backend"] = backend
    if backend == "none":
        stats["error"] = (
            "没有可用的翻译后端：i18n.backend=llm 但未配置 LLM_*，"
            "或 backend 被设成了 none（见 config/settings.toml 的 [i18n]）"
        )
        return stats

    cache = load_cache(settings)
    todo: list[dict] = []
    for it in items:
        rid = str(it.get("repo_id") or "")
        if not rid:
            continue
        stats["candidates"] += 1
        desc = (it.get("description") or "").strip()
        if not desc:
            stats["skipped_empty"] += 1
            continue
        if rid in cache and (cache[rid] or {}).get("zh"):
            stats["skipped_cached"] += 1
            continue
        todo.append(it)

    if not todo:
        log.info("没有需要翻译的简介（候选 %d，已缓存 %d，空简介 %d）",
                 stats["candidates"], stats["skipped_cached"], stats["skipped_empty"])
        return stats

    if backend == "mymemory":
        _translate_via_mymemory(settings, todo, cache, stats)
    else:
        _translate_via_llm(settings, todo, cache, stats, batch_size)

    save_cache(settings, cache)
    return stats


def _translate_via_llm(
    settings: Settings, todo: list[dict], cache: dict, stats: dict, batch_size: int
) -> None:
    for start in range(0, len(todo), batch_size):
        batch = todo[start : start + batch_size]
        payload = [
            {
                "id": str(it["repo_id"]),
                "name": it.get("full_name"),
                "language": it.get("language"),
                "category": it.get("category"),
                "description": (it.get("description") or "")[:400],
            }
            for it in batch
        ]
        try:
            result = _call_llm(settings, payload)
        except (urllib.error.URLError, KeyError, IndexError, ValueError,
                json.JSONDecodeError, TimeoutError) as exc:
            # 单批失败不终止整轮 —— 已成功的批次照样写回缓存。
            stats["failed_batches"] += 1
            log.warning("第 %d 批翻译失败：%s", start // batch_size + 1, exc)
            continue

        got = 0
        for it in batch:
            zh = result.get(str(it["repo_id"]), "").strip()
            if zh:
                cache[str(it["repo_id"])] = {
                    "zh": zh,
                    "by": "llm",
                    "src": (it.get("description") or "").strip()[:200],
                }
                got += 1
        stats["translated"] += got
        # 模型漏答是常见现象，必须喊出来 —— 静默少几条会让页面长期缺一块。
        if got < len(batch):
            log.warning("第 %d 批：请求 %d 条，只返回 %d 条",
                        start // batch_size + 1, len(batch), got)
        log.info("已翻译 %d/%d", min(start + batch_size, len(todo)), len(todo))


def _translate_via_mymemory(
    settings: Settings, todo: list[dict], cache: dict, stats: dict
) -> None:
    """逐条调用免费机翻。没有密钥，靠间隔 + 连续失败熔断来控制额度。"""
    for i, it in enumerate(todo, 1):
        desc = (it.get("description") or "").strip()[:400]
        try:
            zh = _mymemory_one(settings, desc)
        except (urllib.error.URLError, ValueError, TimeoutError,
                json.JSONDecodeError) as exc:
            stats["failed_batches"] += 1
            log.warning("MyMemory 第 %d 条失败：%s", i, exc)
            # 连续失败多半是被限流/当天额度用尽。继续硬撞只会把额度耗在重试上，
            # 已翻译的部分会照常写回缓存，下一轮接着来。
            if stats["failed_batches"] >= 5:
                log.warning("连续失败 %d 次，提前结束本轮（已成功的已写回缓存）",
                            stats["failed_batches"])
                return
            continue
        if zh:
            cache[str(it["repo_id"])] = {
                "zh": zh[:80],
                "by": "mymemory",
                "src": desc[:200],
            }
            stats["translated"] += 1
        if settings.i18n_request_gap:
            time.sleep(settings.i18n_request_gap)
        if i % 20 == 0:
            log.info("MyMemory 进度 %d/%d", i, len(todo))
