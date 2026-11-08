"""拉一份梗库到 qqbot/memes.db（17MB，不入库）。

数据来自 cnmeme.wiki 的公开 API，由 thawline/dsh-memewiki 整理成 SQLite：
    https://github.com/thawline/dsh-memewiki

用法：
    qqbot/.venv/Scripts/python.exe qqbot/tools/fetch_memes.py
    qqbot/.venv/Scripts/python.exe qqbot/tools/fetch_memes.py --mirror raw

GitHub 直连在本机会被 schannel 掐断（curl 56），所以默认走 jsdelivr，
一次就能拉完；raw 那条带 Range 断点续传兜底。
"""

import argparse
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent      # qqbot/
DEST = BASE / "memes.db"
SIZE = 17084416
URLS = {
    "jsdelivr": "https://cdn.jsdelivr.net/gh/thawline/dsh-memewiki@main/data/memes.db",
    "raw": "https://raw.githubusercontent.com/thawline/dsh-memewiki/main/data/memes.db",
}
UA = {"User-Agent": "Mozilla/5.0"}


def fetch(url: str, dest: Path, tries: int = 6) -> bool:
    for i in range(tries):
        have = dest.stat().st_size if dest.exists() else 0
        if have >= SIZE:
            return True
        req = urllib.request.Request(url, headers=UA)
        if have:
            req.add_header("Range", f"bytes={have}-")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp, \
                    open(dest, "ab" if have else "wb") as f:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
        except Exception as exc:
            print(f"  第 {i + 1} 次中断：{exc}")
            time.sleep(1.5)
        got = dest.stat().st_size if dest.exists() else 0
        print(f"  已拿 {got / 1048576:.1f} / {SIZE / 1048576:.1f} MB")
    return dest.exists() and dest.stat().st_size >= SIZE


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mirror", choices=list(URLS), default="jsdelivr")
    args = ap.parse_args()

    if DEST.exists():
        print(f"已有 {DEST.name}（{DEST.stat().st_size / 1048576:.1f} MB），重新拉")
        DEST.unlink()
    print(f"从 {args.mirror} 拉词库…")
    if not fetch(URLS[args.mirror], DEST):
        print("没拉全，换个镜像再试：--mirror raw")
        return 1

    import sqlite3
    db = sqlite3.connect(f"file:{DEST}?mode=ro", uri=True)
    n, built = db.execute("SELECT COUNT(*) FROM memes").fetchone()[0], None
    try:
        built = db.execute("SELECT value FROM meta WHERE key='built_at'").fetchone()[0]
    except sqlite3.Error:
        pass
    print(f"完成：{n} 条，构建于 {built or '未知'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
