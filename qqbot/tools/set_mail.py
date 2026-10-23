"""配置掉线通知邮件：写 qqbot/notify.json（不入库）。

只问两样东西：用哪个邮箱发、授权码是多少。
授权码用 getpass 输入（不回显），写进 notify.json 后不会再出现在任何日志里。

QQ 邮箱：设置 → 账号 → POP3/IMAP/SMTP → 开启服务并生成授权码
交大邮箱：多数情况直接支持客户端登录，授权码/密码按学校邮箱设置来

用法：
    python tools/set_mail.py               按提示配置并测试发送
    python tools/set_mail.py --test        只重发一次测试邮件
    python tools/set_mail.py --show        看看现在配置成什么样（不显示授权码）
"""

import argparse
import getpass
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent      # qqbot/
sys.path.insert(0, str(BASE))

import mailer  # noqa: E402


def show() -> None:
    cfg = mailer.load()
    if not cfg:
        print(f"[!] {mailer.CFG_PATH} 还不存在")
        return
    safe = dict(cfg)
    if safe.get("auth_code"):
        safe["auth_code"] = "***已保存***"
    print(json.dumps(safe, ensure_ascii=False, indent=2))


def save(cfg: dict) -> None:
    mailer.CFG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def wizard() -> int:
    cfg = mailer.load()
    user = input(f"用哪个邮箱发送？[{cfg.get('user') or 'someone@qq.com'}]：").strip()
    user = user or cfg.get("user") or ""
    if "@" not in user:
        print("[!] 邮箱格式不对")
        return 1

    host, port = mailer.guess_host(user)
    host = input(f"SMTP 服务器 [{host}]：").strip() or host
    port_s = input(f"端口 [{port}]：").strip() or str(port)

    to_raw = input(f"发到哪些地址（逗号分隔）[{','.join(cfg.get('to') or [user])}]：").strip()
    to = [x.strip() for x in (to_raw or ",".join(cfg.get("to") or [user])).split(",") if x.strip()]

    print("[*] 授权码不会回显，也不会写进日志")
    code = getpass.getpass("SMTP 授权码（不是邮箱密码）：").strip()
    if not code:
        print("[!] 空授权码，什么都没改")
        return 1

    cfg.update(enable=True, smtp_host=host, smtp_port=int(port_s),
               user=user, auth_code=code, to=to)
    save(cfg)
    print(f"[+] 已写入 {mailer.CFG_PATH}")

    ok, info = mailer.send(
        "[达妮娅] 通知邮件测试",
        "这封是测试邮件。收到说明掉线通知链路通了。\n\n"
        "以后她被踢下线、需要你扫一次码时，会自动给你发一封带二维码的邮件。\n",
    )
    print(("[+] 测试邮件" if ok else "[!] 测试邮件失败：") + info)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="configure offline notification mail")
    ap.add_argument("--test", action="store_true", help="重发测试邮件")
    ap.add_argument("--show", action="store_true", help="查看当前配置")
    args = ap.parse_args()

    if args.show:
        show()
        return 0
    if args.test:
        ok, info = mailer.send(
            "[达妮娅] 通知邮件测试",
            "这封是测试邮件。收到说明掉线通知链路通了。\n")
        print(("[+] " if ok else "[!] ") + info)
        return 0 if ok else 1
    return wizard()


if __name__ == "__main__":
    sys.exit(main())
