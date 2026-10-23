"""NapCat 看门狗：掉线太久就重启 NapCat，且不碰你自己那台官方 QQ。

判定依据只有一个：bot.py 每 20 秒写的 logs/heartbeat.json
    {"online": true/false, "ts": ..., "since": <最后一次状态变化>}

  online=false 且已经持续 offline_after 秒  ->  重启 NapCat
  heartbeat 文件超过 stale 秒没更新         ->  认为整套都关了，不动（避免瞎拉起来）
  logs/watchdog.pause 存在                ->  暂停，手动维护时用

重启范围严格限制：只杀 NapCatWinBootMain.exe 整棵树（taskkill /PID x /T /F），
官方 QQ 是从 explorer.exe 起的另一棵树，不会被牵连。

用法：
    python tools/watchdog.py          常驻巡检
    python tools/watchdog.py --once   只查一次然后退出（试配置用）
    python tools/watchdog.py --dry-run 只打印决策，不动真家伙
"""

import argparse
import json
import logging
import socket
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # qqbot/
ROOT = BASE.parent                                      # 仓库根
LOG_DIR = BASE / "logs"
CFG_PATH = BASE / "config.json"
HB_PATH = LOG_DIR / "heartbeat.json"
STATE_PATH = LOG_DIR / "watchdog.json"
PAUSE_PATH = LOG_DIR / "watchdog.pause"
NAPCAT_DIR = ROOT / "napcat"
LOCK_PORT = 45671                                       # 只做单例互斥，不跑业务

DEFAULTS = {
    "enable": True,
    "check_every": 60,      # 巡检间隔（秒）
    "offline_after": 240,   # 断线持续多久才动手（秒）
    "stale": 1800,          # 心跳超过这个时间没更新 = 整套已停，不管
    "cooldown": 600,        # 两次重启之间的最小间隔（秒）
    "max_per_hour": 3,      # 一小时最多重启几次，防止来回拉扯
}

CREATE_NO_WINDOW = 0x08000000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "watchdog.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("watchdog")


# ---------- 配置 ----------

_cfg = {"mtime": -1.0, "data": {}}


def cfg() -> dict:
    try:
        mtime = CFG_PATH.stat().st_mtime
    except OSError:
        return DEFAULTS
    if mtime != _cfg["mtime"]:
        try:
            data = json.loads(CFG_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        merged = dict(DEFAULTS)
        merged.update(data.get("watchdog") or {})
        _cfg.update(mtime=mtime, data=merged)
    return _cfg["data"] or DEFAULTS


# ---------- 进程 ----------

def napcat_boot_pid() -> int:
    """找出 NapCatWinBootMain.exe 的 PID，没有就返回 0。"""
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='NapCatWinBootMain.exe'\""
        " | Select-Object -ExpandProperty ProcessId | ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=60,
            creationflags=CREATE_NO_WINDOW, encoding="utf-8", errors="ignore",
        )
        raw = (out.stdout or "").strip()
        if not raw:
            return 0
        val = json.loads(raw)
        if isinstance(val, list):
            return int(val[0]) if val else 0
        return int(val)
    except Exception as exc:
        log.warning("查 NapCat 进程失败：%s", exc)
        return 0


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"restarts": [], "total": 0}


def _save_state(st: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------- 心跳 ----------

def read_heartbeat() -> dict | None:
    try:
        return json.loads(HB_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------- 重启 ----------

def restart_napcat(boot_pid: int, dry: bool) -> None:
    log.warning("准备重启 NapCat（boot pid=%s）", boot_pid or "无")
    if dry:
        log.warning("[dry-run] 只演示，实际没杀也没拉")
        return

    st = _load_state()
    st["restarts"] = (st.get("restarts") or []) + [time.time()]
    st["restarts"] = st["restarts"][-20:]
    st["total"] = int(st.get("total") or 0) + 1
    st["last_local"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_state(st)

    if boot_pid:
        r = subprocess.run(["taskkill", "/PID", str(boot_pid), "/T", "/F"],
                           capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW,
                           encoding="utf-8", errors="ignore")
        log.warning("taskkill 返回 %s %s", r.returncode, (r.stdout or r.stderr or "").strip())
    for _ in range(20):
        time.sleep(1)
        if not napcat_boot_pid():
            break

    subprocess.Popen(
        ["cmd", "/c", "run.bat"], cwd=str(NAPCAT_DIR),
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    log.warning("已拉起 napcat\\run.bat，等它自己登录")


# ---------- 单次巡检 ----------

def check(dry: bool = False) -> str:
    c = cfg()
    if not c.get("enable", True):
        return "watchdog 在 config.json 里关着，跳过"
    if PAUSE_PATH.exists():
        return "检测到 logs/watchdog.pause，暂停中不动作"

    hb = read_heartbeat()
    if not hb:
        return f"没有心跳文件 {HB_PATH.name}，bot 大概没在跑，跳过"

    now = time.time()
    age = now - float(hb.get("ts") or 0)
    if age > float(c.get("stale", DEFAULTS["stale"])):
        return f"心跳已 {int(age)} 秒没更新（超过 stale={c.get('stale')}），整套停了，跳过"

    st = _load_state()
    restarts = [t for t in (st.get("restarts") or []) if now - t < 3600]
    if restarts and now - max(restarts) < float(c.get("cooldown", DEFAULTS["cooldown"])):
        return f"距上次重启 {int(now - max(restarts))} 秒，冷却期内不动"

    if hb.get("online"):
        return f"在线（心跳 {int(age)} 秒前）"

    offline_for = now - float(hb.get("since") or hb.get("ts") or now)
    limit = float(c.get("offline_after", DEFAULTS["offline_after"]))
    if offline_for < limit:
        return f"离线 {int(offline_for)} 秒（阈值 {int(limit)}），再等等"

    if len(restarts) >= int(c.get("max_per_hour", DEFAULTS["max_per_hour"])):
        return f"最近一小时已重启 {len(restarts)} 次，达到上限，先不动"

    restart_napcat(napcat_boot_pid(), dry)
    return f"离线 {int(offline_for)} 秒 -> 已触发重启"


def main() -> int:
    ap = argparse.ArgumentParser(description="NapCat watchdog")
    ap.add_argument("--once", action="store_true", help="只巡检一次")
    ap.add_argument("--dry-run", action="store_true", help="只打印决策")
    args = ap.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    lock = socket.socket()
    lock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        print(f"[!] 端口 {LOCK_PORT} 已被占用，说明看门狗已经在跑了")
        return 1

    c = cfg()
    log.info("看门狗启动 enable=%s every=%ss offline_after=%ss cooldown=%ss max/h=%s",
             c.get("enable"), c.get("check_every"), c.get("offline_after"),
             c.get("cooldown"), c.get("max_per_hour"))
    print(f"[*] watchdog running. cfg: {dict(c)}  (log: {LOG_DIR / 'watchdog.log'})")

    try:
        while True:
            try:
                print(f"[check] {check(args.dry_run)}")
            except Exception as exc:
                log.warning("巡检异常：%s", exc)
            if args.once:
                break
            time.sleep(max(10, int(c.get("check_every", 60))))
    except KeyboardInterrupt:
        log.info("看门狗已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
