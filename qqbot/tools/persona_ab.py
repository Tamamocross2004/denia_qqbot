"""人设 A/B 对比：当前 config.json 的人设 vs 一份候选人设文件。

改人设前先这么跑一遍，比"改完扔群里看反应"靠谱。

    python tools/persona_ab.py                        # 默认比 .workbuddy/persona_new.txt
    python tools/persona_ab.py my_persona.txt         # 比指定文件
"""

import asyncio
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent      # qqbot/
sys.path.insert(0, str(BASE))

from openai import AsyncOpenAI  # noqa: E402


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip().strip('"').strip("'")


load_env(BASE / ".env")
client = AsyncOpenAI(api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
                     base_url="https://api.deepseek.com")

CASES = [
    ("日常", "娅娅你上课又睡了？老师不是刚点你名吗"),
    ("戳痛处", "听说你以前在残星会待过？那边都干些什么"),
    ("甜点驱动", "帮我看看这个课题怎么做，我带了提拉米苏"),
    ("身份", "你到底是谁啊，感觉你不太像普通学生"),
    ("闲聊", "今天中午吃啥"),
    ("比较", "原神和鸣潮哪个好，别骑墙"),
]

BG = ("【群聊背景】\n"
      "朴俞喆：这个课题真的做不下去\n"
      "飞行翩翩：问娅娅啊，带点吃的\n")


async def ask(system: str, user: str) -> str:
    r = await client.chat.completions.create(
        model="deepseek-flash",
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": f"{user}\n\n{BG}"}],
        max_tokens=400, temperature=0.7,
        extra_body={"thinking": {"type": "disabled"}},
    )
    return (r.choices[0].message.content or "").strip()


async def main() -> None:
    live = json.loads((BASE / "config.json").read_text(encoding="utf-8"))["system_prompt"]
    cand_path = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        BASE.parent / ".workbuddy" / "persona_new.txt")
    cand = cand_path.read_text(encoding="utf-8").strip()
    print(f"[*] 线上 {len(live)} 字  vs  候选 {cand_path}（{len(cand)} 字）")
    for tag, q in CASES:
        a, b = await asyncio.gather(ask(live, q), ask(cand, q))
        print("=" * 70)
        print(f"【{tag}】{q}")
        print(f"--- 线上 ---\n{a}")
        print(f"--- 候选 ---\n{b}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
