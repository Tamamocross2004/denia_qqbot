"""掉线通知邮件。

看门狗判定「需要人工登录」时，把二维码图片直接发到手机上：
手机 QQ → 扫一扫 → 右上角相册 → 选这张图 → 确认，就登回来了。

配置在 qqbot/notify.json（不入库，用 tools/set_mail.py 生成）：

    {
      "enable": true,
      "smtp_host": "smtp.qq.com",
      "smtp_port": 465,
      "user": "xxx@qq.com",
      "auth_code": "SMTP 授权码，不是邮箱密码",
      "to": ["a@qq.com", "b@sjtu.edu.cn"]
    }

QQ 邮箱的授权码在「设置 → 账号 → POP3/IMAP/SMTP → 生成授权码」里拿。
"""

import json
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

BASE = Path(__file__).resolve().parent
CFG_PATH = BASE / "notify.json"

PRESETS = {
    "qq.com": ("smtp.qq.com", 465),
    "sjtu.edu.cn": ("smtp.sjtu.edu.cn", 465),
}


def load() -> dict:
    try:
        return json.loads(CFG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def guess_host(user: str) -> tuple[str, int]:
    domain = user.split("@")[-1].lower()
    return PRESETS.get(domain, (f"smtp.{domain}", 465))


def send(subject: str, body: str, qr_path: str | None = None,
         to: list[str] | None = None) -> tuple[bool, str]:
    """发一封邮件；带二维码时同时内嵌显示 + 作为附件（附件才能存进相册）。"""
    cfg = load()
    if not cfg.get("enable", True):
        return False, "notify.json 里 enable=false"
    user = (cfg.get("user") or "").strip()
    code = (cfg.get("auth_code") or "").strip()
    if not user or not code:
        return False, "notify.json 缺 user 或 auth_code，先跑 tools/set_mail.py"

    host, port = cfg.get("smtp_host") or guess_host(user)
    target = to or cfg.get("to") or [user]
    if isinstance(target, str):
        target = [target]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(target)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)

    qr = Path(qr_path) if qr_path else None
    if qr and qr.exists():
        png = qr.read_bytes()
        cid = make_msgid(domain="qqbot").strip("<>")
        html = (
            "<p>用手机 QQ →「扫一扫」→ 右上角「相册」→ 选下面这张二维码图片 → 确认登录。</p>"
            "<p>二维码会过期；过期了就在电脑上双击 start-all.bat 重新生成。</p>"
            f'<p><img src="cid:{cid}" alt="login qrcode" style="width:320px"></p>'
        )
        msg.add_alternative(html, subtype="html")
        # 内嵌一份（邮件里直接看得见），再加附件（能存进相册用扫一扫选）
        msg.get_payload()[1].add_related(
            png, maintype="image", subtype="png", cid=f"<{cid}>",
            filename="login-qrcode.png", disposition="inline",
        )
        msg.add_attachment(png, maintype="image", subtype="png",
                           filename="login-qrcode.png")

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=25) as s:
            s.login(user, code)
            s.send_message(msg)
        return True, f"已发送到 {', '.join(target)}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
