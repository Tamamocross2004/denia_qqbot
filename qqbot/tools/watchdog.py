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

# NapCat 自己的控制台输出（扫码、验证码、登录失败原因都在这上面）。
# 看门狗从 DEVNULL 改成写文件，就是为了下次出事能直接看到原因。
CONSOLE_LOG = LOG_DIR / "napcat-console.log"
MARKER = LOG_DIR / "NEEDS_LOGIN.txt"
QR_PNG = NAPCAT_DIR / "cache" / "qrcode.png"
WEBUI_CFG = NAPCAT_DIR / "config" / "webui.json"

# 控制台里出现这些字样 = QQ 卡在人工验证上，重启多少次都没用，必须停手
HUMAN_MARKS = ("需要验证码", "二维码已保存到", "自动密码回退登录需要验证码")
# 出现这些 = 登录成功/在线，可以清掉 needs_human
OK_MARKS = ("AdapterManager", "登录成功", "自动快速登录成功", "自动密码回退登录成功")

# 重启后最多盯多久（秒）：够 NapCat 走完 快速登录 -> 密码登录 -> 二维码
LOGIN_WAIT = 150

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

def webui_url() -> str:
    """WebUI 登录页（带 token），人工验证码就在这个页面完成。"""
    try:
        d = json.loads(WEBUI_CFG.read_text(encoding="utf-8"))
        return f"http://127.0.0.1:{d.get('port', 6099)}/webui?token={d.get('token', '')}"
    except Exception:
        return "http://127.0.0.1:6099/webui"


def console_tail(n: int = 8000) -> str:
    try:
        with open(CONSOLE_LOG, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read()
    except Exception:
        return ""


def qr_url() -> str:
    """从控制台里抠出二维码解码 URL（手机上打不开，但可以贴给二维码生成网站）。"""
    tail = console_tail(20000)
    for line in reversed(tail.splitlines()):
        if "二维码解码URL" in line:
            parts = line.split(":", 1)
            if len(parts) == 2:
                return parts[1].strip()
    return ""


def send_mail(reason: str) -> None:
    """把二维码推到手机上：手机 QQ → 扫一扫 → 相册 → 选图 → 确认。"""
    try:
        sys.path.insert(0, str(BASE))
        import mailer
    except Exception as exc:
        log.warning("没加载到 mailer：%s", exc)
        return

    url = qr_url()
    body = (
        "达妮娅掉线了，QQ 要求人工验证一次（扫码或短信验证码），自动登录过不去。\n\n"
        "最快的办法：手机上打开这封信 → 长按/下载下面的二维码图片存到相册\n"
        "→ 打开手机 QQ → 扫一扫 → 右上角相册 → 选这张图 → 确认。\n\n"
        f"掉线时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"判定原因：{reason}\n"
        f"二维码有效期很短，过期就打不开这次登录了。\n"
    )
    if url:
        body += f"\n二维码解码URL（备用）：{url}\n"
    body += f"\nWebUI 登录页（只在电脑上能开）：{webui_url()}\n"
    body += f"\n二维码原图：{QR_PNG}\n"

    ok, info = mailer.send(
        f"[达妮娅] {time.strftime('%H:%M')} 掉线了，扫一下这张码",
        body, qr_path=str(QR_PNG),
    )
    if ok:
        log.warning("掉线通知邮件：%s", info)
    else:
        log.warning("掉线通知邮件没发出去：%s", info)


def notify_human(reason: str) -> None:
    """QQ 卡在验证码/扫码上：写说明文件 + 把二维码弹出来，然后别再重启。"""
    msg = (
        "达妮娅掉线了，而且需要人工登录一次（QQ 要求验证码或扫码），看门狗已停止重启。\n"
        f"原因：{reason}\n"
        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        "怎么修（二选一）：\n"
        f"1) 打开 WebUI 登录页，按页面提示收短信验证码/扫码：\n   {webui_url()}\n"
        "2) 扫二维码图片（NapCat 刚生成的那张）：\n"
        f"   {QR_PNG}\n\n"
        "登录成功后本文件会自动删除，看门狗恢复待命。\n"
    )
    try:
        MARKER.write_text(msg, encoding="utf-8")
    except Exception:
        pass
    log.error("需要人工登录：%s", reason)
    send_mail(reason)
    # 二维码弹出来，用户扫一下就完事
    try:
        if QR_PNG.exists():
            big = QR_PNG.with_name("qrcode_big.png")
            subprocess.run([sys.executable, str(BASE / "tools" / "qr_big.py")],
                           capture_output=True, timeout=60,
                           creationflags=CREATE_NO_WINDOW)
            target = big if big.exists() else QR_PNG
            subprocess.Popen(["cmd", "/c", "start", "", str(target)],
                             creationflags=CREATE_NO_WINDOW)
            log.warning("已把二维码图片弹出来：%s", target)
    except Exception as exc:
        log.warning("弹二维码失败：%s", exc)


def _wait_login(seconds: int = 150) -> str:
    """启动后盯控制台，判断是登进去了、还是卡在人工验证。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(4)
        hb = read_heartbeat()
        if hb and hb.get("online"):
            return "ok"
        tail = console_tail()
        if any(m in tail for m in HUMAN_MARKS):
            return "need_human"
        if any(m in tail for m in OK_MARKS):
            return "ok"
    return "unknown"


def restart_napcat(boot_pid: int, dry: bool) -> str:
    log.warning("准备重启 NapCat（boot pid=%s）", boot_pid or "无")
    if dry:
        log.warning("[dry-run] 只演示，实际没杀也没拉")
        return "dry"

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
        log.warning("taskkill 返回 %s", r.returncode)
    for _ in range(20):
        time.sleep(1)
        if not napcat_boot_pid():
            break

    # 控制台输出落文件：扫码、验证码、登录失败的唯一线索都在这
    try:
        CONSOLE_LOG.parent.mkdir(exist_ok=True)
        if CONSOLE_LOG.exists() and CONSOLE_LOG.stat().st_size > 4 * 1024 * 1024:
            CONSOLE_LOG.unlink()
    except Exception:
        pass
    out = open(CONSOLE_LOG, "a", encoding="utf-8", errors="replace")
    out.write(f"\n\n===== watchdog restart {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    out.flush()
    subprocess.Popen(
        ["cmd", "/c", "run.bat"], cwd=str(NAPCAT_DIR),
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
    )
    log.warning("已拉起 napcat\\run.bat，等它登录（输出见 logs/%s）", CONSOLE_LOG.name)

    result = _wait_login(LOGIN_WAIT)
    if result == "need_human":
        notify_human("QQ 要求验证码/扫码，自动登录走不通")
        st = _load_state()
        st["needs_human"] = True
        st["fail_streak"] = int(st.get("fail_streak") or 0) + 1
        _save_state(st)
    elif result == "ok":
        st = _load_state()
        st["needs_human"] = False
        st["fail_streak"] = 0
        _save_state(st)
        log.warning("NapCat 已重新登录")
    else:
        st = _load_state()
        st["fail_streak"] = int(st.get("fail_streak") or 0) + 1
        _save_state(st)
        log.warning("等了 %s 秒没看到登录成功的迹象，下次再试（连续失败 %s 次）",
                    LOGIN_WAIT, st["fail_streak"])
    return result


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

    # 已经判定要人工登录：只等，不再重启（重启会把待扫的二维码/验证码冲掉）
    if st.get("needs_human"):
        if hb.get("online"):
            st["needs_human"] = False
            st["fail_streak"] = 0
            _save_state(st)
            try:
                MARKER.unlink()
            except Exception:
                pass
            log.warning("已恢复在线，清除 needs_human")
            return "已恢复在线"
        return f"等人工登录（{MARKER} 里有步骤，WebUI：{webui_url()}）"

    restarts = [t for t in (st.get("restarts") or []) if now - t < 3600]
    if hb.get("online"):
        return f"在线（心跳 {int(age)} 秒前）"

    offline_for = now - float(hb.get("since") or hb.get("ts") or now)
    limit = float(c.get("offline_after", DEFAULTS["offline_after"]))
    if offline_for < limit:
        return f"离线 {int(offline_for)} 秒（阈值 {int(limit)}），再等等"

    # 连续失败就翻倍退避：10min -> 20min -> 40min -> 上限 1h
    streak = int(st.get("fail_streak") or 0)
    wait = float(c.get("cooldown", DEFAULTS["cooldown"])) * (2 ** min(streak, 6))
    wait = min(wait, 3600)
    if restarts and now - max(restarts) < wait:
        return f"距上次重启 {int(now - max(restarts))} 秒（退避 {int(wait)} 秒），再等等"

    if len(restarts) >= int(c.get("max_per_hour", DEFAULTS["max_per_hour"])):
        return f"最近一小时已重启 {len(restarts)} 次，达到上限，先不动"

    result = restart_napcat(napcat_boot_pid(), dry)
    return f"离线 {int(offline_for)} 秒 -> 已触发重启（结果：{result}）"


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
