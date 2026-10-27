"""端到端跑一遍「看图解题」，不经过 QQ。

用法（在仓库根目录）：
    qqbot/.venv/Scripts/python.exe qqbot/tools/solve_probe.py 图片路径 ["题目要求"]

走的是 bot.py 里一模一样的路径：OCR → SOLVE_RULES → 文本模型带 calc 分步解。
用来复盘「她这道题答错了」，也可以当回归测试（同一张图跑几次看答案稳不稳）。
"""

import asyncio
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent      # qqbot/
sys.path.insert(0, str(BASE))

import bot  # noqa: E402


async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    path = Path(sys.argv[1])
    text = sys.argv[2] if len(sys.argv) > 2 else "这题怎么做"
    images = [path.read_bytes()]

    ocr = await bot.ocr_image(images)
    print(f"--- OCR（{len(ocr)} 字）---")
    print(ocr)
    print("--- 解题 ---")

    extra = bot.SOLVE_RULES.format(ocr=ocr)
    vc = bot.cfg().get("vision") or {}
    answer = await bot.ask(
        "probe", text, {"ws": None, "evt": {}, "group_id": None},
        extra=extra, images=[], rounds=10,
        model=vc.get("solve_model") or "deepseek-v4-pro",
        system=vc.get("solve_system") or bot.VISION_SOLVE_SYSTEM,
        temperature=float(vc.get("solve_temperature", 0)),
        max_tokens=int(vc.get("solve_max_tokens", 2600)))
    print(answer)
    print("--- 存档 ---")
    bot._note_solve(text, ocr, answer, images)
    print(f"已写入 {bot.SOLVE_LOG}")


if __name__ == "__main__":
    asyncio.run(main())
