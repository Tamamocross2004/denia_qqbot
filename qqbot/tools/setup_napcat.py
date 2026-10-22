"""把反向 WebSocket 配置注入 NapCat 的 OneBot11 配置文件。

必须在 NapCat 至少登录成功一次之后运行（那时才会生成 onebot11_<QQ号>.json）。
运行后重启 NapCat 生效。

用法：python tools/setup_napcat.py [--port 8080]
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAPCAT = HERE.parent.parent / "napcat"
CONFIG_DIR = NAPCAT / "config"

ENTRY_NAME = "qqbot"


def build_entry(port: int) -> dict:
    return {
        "name": ENTRY_NAME,
        "enable": True,
        "url": f"ws://127.0.0.1:{port}",
        "messagePostFormat": "array",
        "reportSelfMessage": False,
        "reconnectInterval": 5000,
        "token": "",
        "debug": False,
        "heartInterval": 30000,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    if not CONFIG_DIR.exists():
        print(f"[!] 找不到 NapCat 配置目录：{CONFIG_DIR}")
        return 1

    targets = [
        p for p in sorted(CONFIG_DIR.glob("onebot11_*.json"))
        if p.stem.split("_", 1)[1].isdigit()
    ]
    if not targets:
        print("[!] 还没找到 onebot11_<QQ号>.json")
        print("    说明 NapCat 还没成功登录过。先启动 NapCat 扫码登录，再回来跑本脚本。")
        return 2

    for path in targets:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        network = cfg.setdefault("network", {})
        clients = [c for c in network.get("websocketClients", [])
                   if c.get("name") != ENTRY_NAME]
        clients.append(build_entry(args.port))
        network["websocketClients"] = clients

        backup = path.with_suffix(".json.bak")
        shutil.copy2(path, backup)
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[+] {path.name}  已写入反向 WS -> ws://127.0.0.1:{args.port}")
        print(f"    备份：{backup.name}")

    print("\n[*] 重启 NapCat（关掉窗口重新运行 run.bat）后生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
