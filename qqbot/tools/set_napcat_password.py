"""把 bot 号的 QQ 密码写成 MD5，填进 napcat\\run.bat 的回退登录那一栏。

为什么要用它：
    NapCat 被踢下线后，本地保存的 token 也会一起失效，快速登录救不回来；
    不配密码就只剩扫码一条路，凌晨没人扫，bot 就一直躺着。
    配了 NAPCAT_QUICK_PASSWORD_MD5 之后，Worker 重启会自己走密码回退登录。

密码不会以明文落在任何文件里，只在内存里算一次 MD5。

用法：
    python tools/set_napcat_password.py          输入两次密码，写进 run.bat
    python tools/set_napcat_password.py --clear  把那一栏清空
"""

import argparse
import hashlib
import re
import sys
from getpass import getpass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUN_BAT = ROOT / "napcat" / "run.bat"
PATTERN = re.compile(rb'set "NAPCAT_QUICK_PASSWORD_MD5=[^\r\n]*')


def write_md5(md5: str) -> None:
    raw = RUN_BAT.read_bytes()
    if not PATTERN.search(raw):
        print(f"[!] {RUN_BAT} 里没有 NAPCAT_QUICK_PASSWORD_MD5 这一行")
        print("    先确认 run.bat 是最新版")
        sys.exit(1)
    new = PATTERN.sub(f'set "NAPCAT_QUICK_PASSWORD_MD5={md5}"'.encode("ascii"), raw)
    RUN_BAT.write_bytes(new)
    print(f"[+] 已写入 {RUN_BAT}")


def main() -> int:
    ap = argparse.ArgumentParser(description="set NapCat password fallback")
    ap.add_argument("--clear", action="store_true", help="清空回退密码")
    args = ap.parse_args()

    if not RUN_BAT.exists():
        print(f"[!] 找不到 {RUN_BAT}")
        return 1

    if args.clear:
        write_md5("")
        print("[+] 已清空，之后只能靠快速登录/扫码")
        return 0

    pw = getpass("QQ 密码（输入不回显）：").strip()
    if not pw:
        print("[!] 密码为空，什么都不改")
        return 1
    if getpass("再输一次：").strip() != pw:
        print("[!] 两次不一致，什么都不改")
        return 1

    md5 = hashlib.md5(pw.encode("utf-8")).hexdigest()
    del pw
    write_md5(md5)
    print(f"[+] MD5 = {md5}")
    print("[*] 重启 NapCat 生效（start-all.bat，或等看门狗）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
