"""人设拼装：把 system_prompt 里的 {spicy} 换成当前档位的串子文案。

单独一个模块，是因为 tools/persona_ab.py 也要用——
它去 import bot 会撞上运行中进程独占的 bot.log（PermissionError）。

档位文案在 spicy.json，数值在 config.json 的 persona.spicy。
"""

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent      # qqbot/
SPICY_PATH = BASE / "spicy.json"
PLACEHOLDER = "{spicy}"

# spicy.json 读不到时的兜底：就是原来写死在 prompt 里的那句
FALLBACK = ("默认带刺，不用等人惹你：三四条里至少一条要有牙。"
            "连着几句都温温吞吞的，那就不是你了")

DEFAULT_VALUE = 45


def load_tiers() -> list[dict]:
    try:
        data = json.loads(SPICY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    tiers = data.get("tiers") if isinstance(data, dict) else None
    if not isinstance(tiers, list):
        return []
    out = []
    for t in tiers:
        if not isinstance(t, dict):
            continue
        try:
            mx = int(t.get("max"))
        except (TypeError, ValueError):
            continue
        text = str(t.get("text") or "").strip()
        if text:
            out.append({"max": mx, "name": str(t.get("name") or ""), "text": text})
    return sorted(out, key=lambda x: x["max"])


def clamp(value) -> int:
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return DEFAULT_VALUE
    return max(0, min(100, n))


def spicy_text(value, tiers: list[dict] | None = None) -> str:
    """落在哪档就用哪档的文案；比最高档还高就用最高档。"""
    v = clamp(value)
    for t in (tiers if tiers is not None else load_tiers()):
        if v <= t["max"]:
            return t["text"]
    return FALLBACK


def tier_name(value, tiers: list[dict] | None = None) -> str:
    v = clamp(value)
    for t in (tiers if tiers is not None else load_tiers()):
        if v <= t["max"]:
            return t["name"]
    return "未知"


def render(system: str, value) -> str:
    """把 {spicy} 换掉。没占位符就原样返回（解题那套 system 就不带）。"""
    if not system or PLACEHOLDER not in system:
        return system
    return system.replace(PLACEHOLDER, spicy_text(value))
