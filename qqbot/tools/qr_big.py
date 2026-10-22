"""把 NapCat 生成的二维码放大，方便手机扫。

NapCat 启动后二维码在 napcat/cache/qrcode.png（147x147，手机上太小）。
运行本脚本生成 cache/qrcode_big.png。
"""

from pathlib import Path

from PIL import Image

CACHE = Path(__file__).resolve().parent.parent.parent / "napcat" / "cache"


def main() -> int:
    src = CACHE / "qrcode.png"
    if not src.exists():
        print(f"[!] 没找到 {src}，先启动 NapCat")
        return 1
    im = Image.open(src).convert("L")
    scale = max(1, 600 // im.width)
    big = im.resize((im.width * scale, im.height * scale), Image.NEAREST)
    out = CACHE / "qrcode_big.png"
    big.save(out)
    print(f"[+] {out}  {big.width}x{big.height}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
