"""批量导入表情包：自动识别情绪，裁好尺寸，分到 stickers/<标签>/。

用法：
    # 1. 把图片扔进 qqbot/inbox/
    # 2. 先空跑，看分类结果对不对
    python tools/import_stickers.py --dry-run
    # 3. 确认没问题再真跑
    python tools/import_stickers.py

选项：
    --src  <目录>   源目录，默认 qqbot/inbox
    --dry-run       只识别不写入
    --tags  a,b,c   覆盖默认标签（困,无语,开心）
    --keep-origin   静态图不裁剪，只缩放（默认裁成方形）
"""

import argparse
import asyncio
import base64
import io
import os
import sys
from pathlib import Path

from PIL import Image, ImageSequence

try:
    from openai import AsyncOpenAI
except ImportError:
    print("[!] 缺 openai，先跑 qqbot/start.bat 装依赖")
    sys.exit(1)

BASE = Path(__file__).resolve().parent.parent
INBOX = BASE / "inbox"
STICKERS = BASE / "stickers"
DEFAULT_TAGS = ["困", "无语", "开心"]
MODEL = "deepseek-v4-flash-vision-exp"
EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

RAW_LIMIT = 800 * 1024      # 动图超过这个大小才压缩


def load_key() -> str:
    env = BASE / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("DEEPSEEK_API_KEY="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if v:
                    return v
    return os.environ.get("DEEPSEEK_API_KEY", "").strip()


def first_frame_bytes(path: Path, size: int = 512) -> bytes:
    """取（动图的）第一帧，压成给模型看的 JPEG。"""
    im = Image.open(path)
    im.seek(0)
    im = im.convert("RGBA")
    bg = Image.new("RGB", im.size, (255, 255, 255))
    bg.paste(im, mask=im.split()[-1])
    bg.thumbnail((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    bg.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def build_prompt(tags: list[str]) -> str:
    # 标签边界必须写死，否则「困 / 无语」这两个极易混
    # 嫌弃已经并进无语：冷脸、撇嘴、斜眼这些本身就是「无语」的一种
    hints = {
        "困": "闭眼、打哈欠、趴着、睡着、眼神涣散、有气无力",
        "开心": "在笑、比心、雀跃、眼睛发亮、满足",
        "无语": "面无表情、眼神空洞、摊手、被雷到、不想理人、"
                "皱眉、撇嘴、冷淡、不屑、斜眼看人",
    }
    lines = [f"- {t}：{hints[t]}" for t in tags if t in hints]
    if len(lines) < len(tags):
        lines = [f"- {t}：{hints.get(t, t)}" for t in tags]
    return (
        "这是一张 QQ 表情包图片。\n"
        "第一步：判断图里主要是不是一位浅粉到紫色渐变长发、紫粉色眼睛的动漫少女"
        "（游戏《鸣潮》的达妮娅）。不是的话，只输出 skip。\n"
        "第二步：如果是，从下面的标签里选最贴切的一个，只输出标签本身：\n"
        + "\n".join(lines)
        + "\n不要解释，不要标点，不要引号。"
    )


async def classify(client: AsyncOpenAI, img: bytes, prompt: str,
                   tags: list[str]) -> str | None:
    b64 = base64.b64encode(img).decode()
    try:
        resp = await client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]}],
            max_tokens=24,
            extra_body={"thinking": {"type": "disabled"}},
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        print(f"    识别失败：{type(exc).__name__} {exc}")
        return None

    low = raw.lower()
    if "skip" in low:
        return "skip"
    for t in tags:
        if t in raw:
            return t
    return None


def save_static(src: Path, dst: Path, square: bool = True) -> None:
    im = Image.open(src)
    im.seek(0)
    alpha = im.mode in ("RGBA", "LA", "P") and "transparency" in im.info
    im = im.convert("RGBA")

    if square:
        w, h = im.size
        side = min(w, h)
        left, top = (w - side) // 2, (h - side) // 2
        im = im.crop((left, top, left + side, top + side))

    im.thumbnail((320, 320), Image.LANCZOS)
    canvas = Image.new("RGBA", (320, 320), (0, 0, 0, 0))
    canvas.paste(im, ((320 - im.width) // 2, (320 - im.height) // 2), im)

    if alpha:
        canvas.save(dst.with_suffix(".png"), "PNG", optimize=True)
    else:
        canvas.convert("RGB").save(dst.with_suffix(".jpg"), "JPEG",
                                   quality=82, optimize=True)


def save_gif(src: Path, dst: Path) -> None:
    if src.stat().st_size <= RAW_LIMIT:
        dst.write_bytes(src.read_bytes())
        return
    im = Image.open(src)
    dur = im.info.get("duration", 80) or 80
    frames = []
    for i, f in enumerate(ImageSequence.Iterator(im)):
        if i and i % 2:
            continue
        fr = f.convert("RGBA")
        bg = Image.new("RGB", fr.size, (255, 255, 255))
        bg.paste(fr, mask=fr.split()[-1])
        frames.append(bg.resize((240, 240), Image.LANCZOS)
                      .convert("P", palette=Image.ADAPTIVE, colors=64))
    if not frames:
        return
    frames[0].save(dst, save_all=True, append_images=frames[1:],
                   duration=int(dur * 2), loop=0, optimize=True, disposal=2)


def next_name(folder: Path, prefix: str) -> Path:
    """找一个没被占用的文件名。"""
    for i in range(1, 1000):
        for ext in (".jpg", ".png", ".gif", ".webp"):
            p = folder / f"{prefix}{i:02d}{ext}"
            if p.exists():
                continue
            # 同编号的其他扩展名也算占用
            if any((folder / f"{prefix}{i:02d}{e}").exists()
                   for e in (".jpg", ".png", ".gif", ".webp")):
                continue
            return folder / f"{prefix}{i:02d}"
    raise RuntimeError("文件太多了，换个前缀")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(INBOX))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tags", default=",".join(DEFAULT_TAGS))
    ap.add_argument("--keep-origin", action="store_true",
                    help="静态图不裁方形，只缩放")
    args = ap.parse_args()

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    src = Path(args.src)
    if not src.is_dir():
        print(f"[!] 找不到目录：{src}")
        return 1

    files = sorted(p for p in src.iterdir()
                   if p.is_file() and p.suffix.lower() in EXTS)
    if not files:
        print(f"[!] {src} 里没有图片。把表情包扔进去再跑。")
        return 1

    key = load_key()
    if not key:
        print("[!] 没找到 DEEPSEEK_API_KEY")
        return 1

    client = AsyncOpenAI(api_key=key, base_url="https://api.deepseek.com")
    prompt = build_prompt(tags)

    print(f"[*] {len(files)} 张待处理，标签：{'/'.join(tags)}"
          f"{'（空跑，不写入）' if args.dry_run else ''}\n")

    stats = {t: 0 for t in tags}
    skipped = failed = 0

    for idx, f in enumerate(files, 1):
        print(f"  [{idx}/{len(files)}] {f.name}", end="  ")
        try:
            img = first_frame_bytes(f)
        except Exception as exc:
            print(f"读不了：{exc}")
            failed += 1
            continue

        label = await classify(client, img, prompt, tags)
        if label is None:
            print("→ 认不出来，跳过")
            failed += 1
            continue
        if label == "skip":
            print("→ 不是达妮娅，跳过")
            skipped += 1
            continue

        folder = STICKERS / label
        if args.dry_run:
            print(f"→ {label}/（空跑）")
            stats[label] += 1
            continue

        folder.mkdir(parents=True, exist_ok=True)
        target = next_name(folder, "imp")
        try:
            if f.suffix.lower() == ".gif":
                save_gif(f, target.with_suffix(".gif"))
            else:
                save_static(f, target, square=not args.keep_origin)
        except Exception as exc:
            print(f"→ {label} 但写文件失败：{exc}")
            failed += 1
            continue
        print(f"→ {label}/{target.name}")
        stats[label] += 1
        await asyncio.sleep(0.3)

    print("\n=== 汇总 ===")
    for t, n in stats.items():
        print(f"  {t}: {n}")
    print(f"  跳过（不是达妮娅）: {skipped}")
    print(f"  失败: {failed}")
    if args.dry_run:
        print("\n[*] 这是空跑。确认分类没问题后去掉 --dry-run 再跑一次。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
