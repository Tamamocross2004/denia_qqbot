"""测联网搜索倾向：同一批问题，看它到底有没有调 web_search。

不碰真实网络：把 run_tool 换成假的，只记录有没有被调用。
跑法：
    python tools/search_probe.py            # 当前 config 的人设
    python tools/search_probe.py cand.txt   # 用候选人设（文件名）
"""

import asyncio
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent      # qqbot/
sys.path.insert(0, str(BASE))
import bot  # noqa: E402

for raw in (BASE / ".env").read_text(encoding="utf-8").splitlines():
    if "=" in raw and not raw.strip().startswith("#"):
        k, v = raw.split("=", 1)
        os.environ[k.strip()] = v.strip().strip('"').strip("'")

CALLS: list[str] = []


async def fake_tool(name, args, ctx):
    CALLS.append(name)
    if name == "web_search":
        return ("【搜索结果（测试桩）】这里是一段占位结果，用来观察她会不会先查。"
                "关键词：测到的内容与真实网页无关。")
    return "（测试桩）"


bot.run_tool = fake_tool

BG = ("【群里刚才的聊天记录，都是别人说的，不是你自己的经历】\n"
      "朴俞喆：这课题做不下去\n"
      "飞行翩翩：问娅娅啊，带点吃的\n")

# (分类, 问题)  should = 该不该查
CASES = [
    ("该查", "鸣潮3.3版本什么时候上线的"),
    ("该查", "绯雪是谁"),
    ("该查", "iPhone 17 现在多少钱"),
    ("该查", "黑神话钟馗什么时候发售"),
    ("该查", "南怀北瑾丨林川"),
    ("该查", "昨天那个发布会讲了啥"),
    ("该查", "原神现在卡池里有谁"),
    ("不该查", "困死了怎么办"),
    ("不该查", "今天中午吃啥"),
    ("不该查", "你是谁啊"),
    ("不该查", "哈哈哈哈哈"),
    ("不该查", "我好累"),
]


def hint_for(text: str) -> str:
    """复刻 handle() 里那条硬性提示的判定。"""
    if bot._ASK_RE.search(text) or bot.looks_like_name(text):
        return ("【注意】对方在问一个具体的东西（人或事），而你手头没有资料。"
                "如果不是在问你自己（你的外貌、口味、日常作息），就必须先调 web_search 查。")
    return ""


async def main() -> None:
    cand = sys.argv[1] if len(sys.argv) > 1 else ""
    if cand:
        original = bot.cfg
        prompt = Path(cand).read_text(encoding="utf-8").strip()

        def patched():
            d = dict(original())
            d["system_prompt"] = prompt
            return d
        bot.cfg = patched

    ok = bad = 0
    for i, (kind, q) in enumerate(CASES):
        CALLS.clear()
        hint = hint_for(q)
        extra = "\n\n".join(x for x in (BG, hint) if x)
        try:
            answer = await bot.ask(f"probe{i}", q, {}, extra=extra)
        except Exception as exc:
            print(f"[{kind}] {q}  -> 调用异常 {exc}")
            continue
        searched = "web_search" in CALLS
        hit = (searched if kind == "该查" else not searched)
        ok += hit
        bad += not hit
        flag = "✓" if hit else "✗"
        print(f"{flag} [{kind}] {q}")
        print(f"   提示={'有' if hint else '无'} 搜索={searched}  答={answer[:52]!r}")
    print(f"\n命中 {ok}/{len(CASES)}")


asyncio.run(main())
