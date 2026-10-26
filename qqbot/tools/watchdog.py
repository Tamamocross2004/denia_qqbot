"""协议端看门狗：掉线太久就重启协议端，且绝不碰你自己那台官方 QQ。

支持两种协议端，靠 `watchdog.backend` 选（默认 auto，按 WebUI 端口判断）：

    napcat     —— 6099 在听。重启方式：杀 NapCatWinBootMain.exe 整棵树，再跑 napcat\run.bat
    snowluma   —— 5099 在听。重启方式：杀 bot 那棵 QQ 树 -> 重开 QQ -> 调 WebUI 接口把 hook 挂回去

判定依据只有一个：bot.py 每 20 秒写的 logs/heartbeat.json
    {"online": true/false, "ts": ..., "since": <最后一次状态变化>}

  online=false 且已经持续 offline_after 秒  ->  重启协议端
  heartbeat 文件超过 stale 秒没更新         ->  认为整套都关了，不动（避免瞎拉起来）
  logs/watchdog.pause 存在                ->  暂停，手动维护时用

重启范围严格限制（两种后端都一样）：只碰 profile 是 `qqbot-client` 的那棵 QQ。
官方 QQ 用的是默认 profile，不会被牵连；NapCat 那套则只按 NapCatWinBootMain.exe 的 PID 杀整棵树。

用法：
    python tools/watchdog.py            常驻巡检
    python tools/watchdog.py --once     只查一次然后退出（试配置用）
    python tools/watchdog.py --dry-run  只打印决策，不动真家伙
"""

import argparse
import json
import logging
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # qqbot/
ROOT = BASE.parent                                      # 仓库根
LOG_DIR = BASE / "logs"
CFG_PATH = BASE / "config.json"
HB_PATH = LOG_DIR / "heartbeat.json"
STATE_PATH = LOG_DIR / "watchdog.json"
PAUSE_PATH = LOG_DIR / "watchdog.pause"

NAPCAT_DIR = ROOT / "napcat"
SNOW_DIR = ROOT / "snowluma"
SNOW_LOG_DIR = SNOW_DIR / "logs"
SNOW_CONSOLE = SNOW_LOG_DIR / "snowluma-console.log"
SNOW_CRED = SNOW_LOG_DIR / "webui-credentials.txt"

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
    "backend": "auto",      # auto / napcat / snowluma
    "check_every": 60,      # 巡检间隔（秒）
    "offline_after": 240,   # 断线持续多久才动手（秒）
    "stale": 1800,          # 心跳超过这个时间没更新 = 整套已停，不管
    "cooldown": 600,        # 两次重启之间的最小间隔（秒）
    "max_per_hour": 3,      # 一小时最多重启几次，防止来回拉扯
    "snowluma": {
        "webui": "http://127.0.0.1:5099",
        "qq_exe": "D:\\QQ.exe",
        "qq_profile": "D:\\qqbot-client\\profile",
        "login_wait": 180,
    },
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
        snow = dict(DEFAULTS["snowluma"])
        snow.update((data.get("watchdog") or {}).get("snowluma") or {})
        merged["snowluma"] = snow
        _cfg.update(mtime=mtime, data=merged)
    return _cfg["data"] or DEFAULTS


# ---------- 通用小工具 ----------

def _ps(script: str, timeout: int = 60) -> str:
    """跑一段 PowerShell 拿 stdout；失败返回空串。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW, encoding="utf-8", errors="ignore",
        )
        return (out.stdout or "").strip()
    except Exception as exc:
        log.warning("PowerShell 查询失败：%s", exc)
        return ""


def _pid_list(raw: str) -> list[int]:
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except Exception:
        return []
    if isinstance(val, list):
        return [int(x) for x in val if str(x).isdigit()]
    return [int(val)] if str(val).isdigit() else []


def port_open(port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def file_tail(path: Path, n: int = 8000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read()
    except Exception:
        return ""


def console_tail(n: int = 8000) -> str:
    return file_tail(CONSOLE_LOG, n)


# ---------- 协议端识别 ----------

def backend_name() -> str:
    """auto 时按 WebUI 端口判断；两个都没起来就回落到上次记住的那个。"""
    want = str(cfg().get("backend") or "auto").lower()
    if want in ("napcat", "snowluma"):
        return want
    if port_open(5099):
        return "snowluma"
    if port_open(6099):
        return "napcat"
    last = str(_load_state().get("backend") or "")
    return last if last in ("napcat", "snowluma") else "unknown"


# ---------- 进程 ----------

def napcat_boot_pid() -> int:
    """找出 NapCatWinBootMain.exe 的 PID，没有就返回 0。"""
    raw = _ps(
        "Get-CimInstance Win32_Process -Filter \"Name='NapCatWinBootMain.exe'\""
        " | Select-Object -ExpandProperty ProcessId | ConvertTo-Json -Compress"
    )
    pids = _pid_list(raw)
    return pids[0] if pids else 0


def snowluma_qq_pids() -> list[int]:
    """bot 那棵 QQ 的主进程。

    只认命令行里带 `qqbot-client` 的：官方 QQ 用的是默认 profile，不可能命中。
    子进程（--type=...）不算，避免重复杀。
    """
    raw = _ps(
        "Get-CimInstance Win32_Process -Filter \"Name='QQ.exe'\""
        " | Where-Object { $_.CommandLine -match 'qqbot-client' -and $_.CommandLine -notmatch '--type=' }"
        " | Select-Object -ExpandProperty ProcessId | ConvertTo-Json -Compress"
    )
    return _pid_list(raw)


def snowluma_node_pids() -> list[int]:
    raw = _ps(
        "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\""
        " | Where-Object { $_.CommandLine -match 'snowluma' }"
        " | Select-Object -ExpandProperty ProcessId | ConvertTo-Json -Compress"
    )
    return _pid_list(raw)


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


def _mark_restart(backend: str) -> dict:
    st = _load_state()
    st["restarts"] = (st.get("restarts") or []) + [time.time()]
    st["restarts"] = st["restarts"][-20:]
    st["total"] = int(st.get("total") or 0) + 1
    st["last_local"] = time.strftime("%Y-%m-%d %H:%M:%S")
    st["backend"] = backend
    _save_state(st)
    return st


# ---------- 心跳 ----------

def read_heartbeat() -> dict | None:
    try:
        return json.loads(HB_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------- NapCat ----------

def webui_url() -> str:
    """WebUI 登录页（带 token），人工验证码就在这个页面完成。"""
    try:
        d = json.loads(WEBUI_CFG.read_text(encoding="utf-8"))
        return f"http://127.0.0.1:{d.get('port', 6099)}/webui?token={d.get('token', '')}"
    except Exception:
        return "http://127.0.0.1:6099/webui"


def qr_url() -> str:
    """从控制台里抠出二维码解码 URL（手机上打不开，但可以贴给二维码生成网站）。"""
    tail = console_tail(20000)
    for line in reversed(tail.splitlines()):
        if "二维码解码URL" in line:
            parts = line.split(":", 1)
            if len(parts) == 2:
                return parts[1].strip()
    return ""


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

    _mark_restart("napcat")

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
    _after_restart(result, "NapCat", "QQ 要求验证码/扫码，自动登录走不通")
    return result


# ---------- SnowLuma ----------

def snow_api(path: str, payload: dict | None = None, token: str = "") -> tuple[int, str]:
    """调 SnowLuma 的 WebUI 接口。必须绕开系统代理，否则会 502/10061。"""
    sc = cfg().get("snowluma") or {}
    url = str(sc.get("webui") or DEFAULTS["snowluma"]["webui"]).rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers,
                                method="POST" if data is not None else "GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def snow_uin() -> str:
    """从 snowluma/config/onebot_<uin>.json 的文件名里读账号，省得把号写进代码。"""
    try:
        for p in sorted(SNOW_DIR.glob("config/onebot_*.json")):
            name = p.stem.split("_", 1)[-1]
            if name.isdigit():
                return name
    except Exception:
        pass
    return "bot 号"


def snow_password() -> str:
    """密码存在 snowluma/logs/webui-credentials.txt（该目录不入库）。"""
    try:
        for line in SNOW_CRED.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("password="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return str((cfg().get("snowluma") or {}).get("password") or "")


def snow_login() -> str:
    pw = snow_password()
    if not pw:
        log.warning("没找到 SnowLuma WebUI 密码（snowluma/logs/webui-credentials.txt）")
        return ""
    status, body = snow_api("/api/login", {"password": pw})
    try:
        token = json.loads(body).get("token", "")
    except Exception:
        token = ""
    if not token:
        log.warning("SnowLuma WebUI 登录失败：%s %s", status, body[:120])
    return token


def _start_snowluma() -> None:
    """node 没在跑时把它拉起来，输出照旧落到 snowluma-console.log。"""
    node = SNOW_DIR / "node.exe"
    exe = str(node if node.exists() else "node")
    try:
        SNOW_LOG_DIR.mkdir(exist_ok=True)
    except Exception:
        pass
    out = open(SNOW_CONSOLE, "a", encoding="utf-8", errors="replace")
    out.write(f"\n\n===== watchdog restart {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    out.flush()
    subprocess.Popen([exe, ".\\index.mjs"], cwd=str(SNOW_DIR),
                     stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                     creationflags=CREATE_NO_WINDOW)
    log.warning("已拉起 snowluma\\index.mjs")


def _wait_snow_online(seconds: int) -> str:
    """等 bot 的心跳转在线；等不到就判断是不是卡在扫码。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(4)
        hb = read_heartbeat()
        if hb and hb.get("online"):
            return "ok"
    tail = file_tail(SNOW_LOG_DIR / f"snowluma-{time.strftime('%Y-%m-%d')}.log", 20000)
    if "login detected" in tail:
        return "unknown"          # QQ 登进去了，但 OneBot 还没连上（多半 bot.py 没跑）
    return "need_human"           # 一直没 login detected = 大概率停在扫码页


def restart_snowluma(dry: bool) -> str:
    sc = cfg().get("snowluma") or {}
    qq_exe = str(sc.get("qq_exe") or DEFAULTS["snowluma"]["qq_exe"])
    profile = str(sc.get("qq_profile") or DEFAULTS["snowluma"]["qq_profile"])
    wait_s = int(sc.get("login_wait") or DEFAULTS["snowluma"]["login_wait"])

    pids = snowluma_qq_pids()
    log.warning("准备重启 SnowLuma（bot 的 QQ 进程 %s）", pids or "无")
    if dry:
        log.warning("[dry-run] 只演示：杀 %s -> 重开 %s -> 调 /api/processes/<新pid>/load", pids, qq_exe)
        return "dry"

    _mark_restart("snowluma")

    # 1) node 没在跑就先拉起来（比如用户手滑关了那个黑窗口）
    if not snowluma_node_pids() and not port_open(5099):
        _start_snowluma()
        for _ in range(30):
            time.sleep(1)
            if port_open(5099):
                break

    # 2) 杀掉 bot 那棵 QQ 树（绝不动默认 profile 的官方 QQ）
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
                       encoding="utf-8", errors="ignore")
    for _ in range(20):
        if not snowluma_qq_pids():
            break
        time.sleep(1)

    # 3) 用同一个 profile 重开 QQ
    subprocess.Popen(["cmd", "/c", "start", "", qq_exe, f"--user-data-dir={profile}"],
                     creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log.warning("已重开 QQ（profile=%s），等它起来", profile)

    new_pid = 0
    for _ in range(40):
        time.sleep(2)
        again = snowluma_qq_pids()
        if again:
            new_pid = again[0]
            break
    if not new_pid:
        log.warning("等了 80 秒没看到新的 QQ 进程")
        _after_restart("unknown", "SnowLuma", "重开 QQ 后进程没起来")
        return "unknown"
    log.warning("新 QQ 进程 pid=%s，开始挂 hook", new_pid)

    # 4) 调接口把 hook 挂回去（hookAutoLoad 是关的，必须手动 load）
    token = snow_login()
    if not token:
        _after_restart("need_human", "SnowLuma", "WebUI 登录失败，没法挂 hook")
        return "need_human"
    status, body = snow_api(f"/api/processes/{new_pid}/load", {}, token)
    log.warning("load pid=%s -> %s %s", new_pid, status, body[:160].replace("\n", " "))
    if status != 200:
        _after_restart("need_human", "SnowLuma", f"挂载进程失败：HTTP {status}")
        return "need_human"

    # 5) 等 OneBot 连回来
    result = _wait_snow_online(wait_s)
    _after_restart(result, "SnowLuma", "QQ 停在扫码页，需要人工登录一次")
    return result


# ---------- 重启之后 ----------

def _after_restart(result: str, who: str, human_reason: str) -> None:
    st = _load_state()
    if result == "ok":
        st["needs_human"] = False
        st["fail_streak"] = 0
        _save_state(st)
        log.warning("%s 已重新上线", who)
        return
    if result == "need_human":
        notify_human(human_reason)
        st = _load_state()
        st["needs_human"] = True
        st["fail_streak"] = int(st.get("fail_streak") or 0) + 1
        _save_state(st)
        return
    st["fail_streak"] = int(st.get("fail_streak") or 0) + 1
    _save_state(st)
    log.warning("%s 重启后没确认上线，下次再试（连续失败 %s 次）", who, st["fail_streak"])


def send_mail(reason: str, backend: str) -> None:
    try:
        sys.path.insert(0, str(BASE))
        import mailer
    except Exception as exc:
        log.warning("没加载到 mailer：%s", exc)
        return

    if backend == "napcat":
        url = qr_url()
        body = (
            "达妮娅掉线了，QQ 要求人工验证一次（扫码或短信验证码），自动登录过不去。\n\n"
            "最快的办法：手机上打开这封信 → 长按/下载下面的二维码图片存到相册\n"
            "→ 打开手机 QQ → 扫一扫 → 右上角相册 → 选这张图 → 确认。\n\n"
            f"掉线时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"判定原因：{reason}\n"
            "二维码有效期很短，过期就打不开这次登录了。\n"
        )
        if url:
            body += f"\n二维码解码URL（备用）：{url}\n"
        body += f"\nWebUI 登录页（只在电脑上能开）：{webui_url()}\n"
        body += f"\n二维码原图：{QR_PNG}\n"
        ok, info = mailer.send(
            f"[达妮娅] {time.strftime('%H:%M')} 掉线了，扫一下这张码",
            body, qr_path=str(QR_PNG),
        )
    else:
        sc = cfg().get("snowluma") or {}
        body = (
            "达妮娅掉线了，SnowLuma 重启后没能自己登回去，需要人到电脑前看一眼。\n\n"
            f"去这台机器的 QQ 窗口看看：如果停在登录页，用 {snow_uin()} 扫码。\n"
            "登进去之后，WebUI 会自己识别；没识别就在 Processes 页对那个 PID 点一次 load。\n\n"
            f"掉线时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"判定原因：{reason}\n"
            f"SnowLuma WebUI：{sc.get('webui') or DEFAULTS['snowluma']['webui']}\n"
            f"密码：{SNOW_CRED}\n"
            "（SnowLuma 只支持扫码，没有密码回退，所以这步机器替不了你）\n"
        )
        ok, info = mailer.send(
            f"[达妮娅] {time.strftime('%H:%M')} 掉线了，需要扫一次码", body)

    if ok:
        log.warning("掉线通知邮件：%s", info)
    else:
        log.warning("掉线通知邮件没发出去：%s", info)


def notify_human(reason: str) -> None:
    """卡在人工验证上：写说明文件 + 发邮件，然后别再重启。"""
    backend = backend_name()
    if backend == "napcat":
        howto = (
            "怎么修（二选一）：\n"
            f"1) 打开 WebUI 登录页，按页面提示收短信验证码/扫码：\n   {webui_url()}\n"
            "2) 扫二维码图片（NapCat 刚生成的那张）：\n"
            f"   {QR_PNG}\n"
        )
    else:
        howto = (
            "怎么修：\n"
            f"1) 在这台机器的 QQ 窗口里，用 {snow_uin()} 扫码登录\n"
            "2) 登进去后，到 SnowLuma 的 Processes 页对那个 PID 点一次 load\n"
            f"   WebUI：{(cfg().get('snowluma') or {}).get('webui') or DEFAULTS['snowluma']['webui']}\n"
            f"   密码：{SNOW_CRED}\n"
        )
    msg = (
        f"达妮娅掉线了，而且需要人工登录一次，看门狗已停止重启。\n"
        f"协议端：{backend}\n"
        f"原因：{reason}\n"
        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"{howto}\n"
        "登录成功后本文件会自动删除，看门狗恢复待命。\n"
    )
    try:
        MARKER.write_text(msg, encoding="utf-8")
    except Exception:
        pass
    log.error("需要人工登录：%s", reason)
    send_mail(reason, backend)

    if backend == "napcat":
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
    backend = backend_name()
    if backend != "unknown":
        st["backend"] = backend
        _save_state(st)

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
        return f"等人工登录（{MARKER} 里有步骤）"

    restarts = [t for t in (st.get("restarts") or []) if now - t < 3600]
    if hb.get("online"):
        return f"在线（{backend}，心跳 {int(age)} 秒前）"

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

    if backend == "unknown":
        return "识别不出协议端（5099 / 6099 都没在听），先不动"

    if backend == "snowluma":
        result = restart_snowluma(dry)
    else:
        result = restart_napcat(napcat_boot_pid(), dry)
    return f"离线 {int(offline_for)} 秒 -> 已触发重启 {backend}（结果：{result}）"


def main() -> int:
    ap = argparse.ArgumentParser(description="protocol-side watchdog (napcat / snowluma)")
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
    log.info("看门狗启动 backend=%s every=%ss offline_after=%ss cooldown=%ss max/h=%s",
             c.get("backend"), c.get("check_every"), c.get("offline_after"),
             c.get("cooldown"), c.get("max_per_hour"))
    print(f"[*] watchdog running. backend={backend_name()}  cfg: {dict(c)}")

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
