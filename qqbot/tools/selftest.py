"""链路自检：模拟 NapCat 反连 bot.py，发一条事件，看是否收到下发指令。

用法：
  python tools/selftest.py                              # 私聊，默认 8080
  python tools/selftest.py --group 123456789            # 群聊并 @ 机器人
  python tools/selftest.py --url ws://127.0.0.1:8090 --group 111 --text hi
"""

import argparse
import asyncio
import base64
import functools
import http.server
import json
import random
import re
import threading
import time
from pathlib import Path

import websockets

# --image 时由 start_static() 填
IMG_STATIC: dict = {"path": None}


def start_static(directory: Path) -> int:
    """临时起一个静态服务，用来模拟 NapCat 给的图片 URL。"""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(directory))
    handler.log_message = lambda *a, **k: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd.server_address[1]


def image_segment(args, url_port: int = 0) -> dict:
    p = Path(args.image).resolve()
    IMG_STATIC["path"] = p
    mode = args.image_mode
    if mode == "b64":
        return {"type": "image",
                "data": {"file": p.name,
                         "base64": base64.b64encode(p.read_bytes()).decode()}}
    if mode == "file":
        # 只给 file，逼 bot 走 get_image
        return {"type": "image", "data": {"file": p.name}}
    return {"type": "image",
            "data": {"file": p.name,
                     "url": f"http://127.0.0.1:{url_port}/{p.name}"}}


def build_event(args) -> dict:
    self_id, uid = args.self_id, args.user_id
    mid = random.randint(10**6, 10**7)

    if args.poke:
        # notice/poke：target 是"被拍的人"，默认拍机器人自己
        evt = {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "time": int(time.time()),
            "self_id": self_id,
            "user_id": uid,
            "target_id": args.target or self_id,
        }
        if args.group:
            evt["group_id"] = args.group
        return evt

    if args.group:
        if args.no_at:
            msg = [{"type": "text", "data": {"text": args.text}}]
            raw = args.text
        else:
            msg = [
                {"type": "at", "data": {"qq": str(self_id)}},
                {"type": "text", "data": {"text": " " + args.text}},
            ]
            raw = f"[CQ:at,qq={self_id}] {args.text}"
        if args.reply:
            # 引用一条（假的）原文，验证 bot 会不会回去取 get_msg
            msg.insert(0, {"type": "reply", "data": {"id": str(QUOTE_ID)}})
            raw = f"[CQ:reply,id={QUOTE_ID}]" + raw
        if args.image:
            msg.append(image_segment(args, getattr(args, "url_port", 0)))
            raw += "[CQ:image,file=test]"
        return {
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "message_id": mid,
            "group_id": args.group,
            "user_id": uid,
            "self_id": self_id,
            "sender": {"user_id": uid, "nickname": args.nick or f"U{uid}",
                       "card": args.nick or ""},
            "raw_message": raw,
            "message": msg,
        }

    pmsg = [{"type": "text", "data": {"text": args.text}}]
    praw = args.text
    if args.image:
        pmsg.append(image_segment(args, getattr(args, "url_port", 0)))
        praw += "[CQ:image,file=test]"
    return {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": mid,
        "user_id": uid,
        "self_id": self_id,
        "raw_message": praw,
        "message": pmsg,
    }


def ws_connect(url: str):
    """连本地回环时必须绕开系统代理。

    websockets>=15 的 connect 默认 proxy=True，会读 HTTP_PROXY 环境变量，
    把 ws://127.0.0.1 也塞进代理导致 502。
    """
    try:
        return websockets.connect(url, proxy=None)
    except TypeError:  # 老版本没有 proxy 参数
        return websockets.connect(url)


MOCK_API = {
    "get_login_info": {"user_id": 20002, "nickname": "selftest"},
    "get_group_info": {"group_name": "测试群", "member_count": 42, "owner_id": 10001},
    "get_group_member_list": [],
}

# 假的「被引用的那条消息」。--reply 时 bot 会拿这个 id 来调 get_msg，
# 真机（NapCat / SnowLuma）返回的结构就是这样。
QUOTE_ID = 880001
QUOTE_MSG = {
    "message_id": QUOTE_ID,
    "message_type": "group",
    "user_id": 20002,
    "raw_message": "驱动这东西，新版是给新卡修的，老卡跟着升，纯属给厂家当测试员",
    "message": [{"type": "text", "data": {
        "text": "驱动这东西，新版是给新卡修的，老卡跟着升，纯属给厂家当测试员"}}],
    "sender": {"user_id": 20002, "nickname": "达达妮娅", "card": "达达妮娅"},
}


def mock_data(action: str) -> dict:
    if action == "get_image":
        p = IMG_STATIC.get("path")
        return {"file": str(p) if p else "", "url": ""}
    if action == "get_msg":
        return QUOTE_MSG
    return MOCK_API.get(action, {})


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8080")
    ap.add_argument("--group", type=int, default=0, help="群号；不填则模拟私聊")
    ap.add_argument("--no-at", action="store_true",
                    help="群消息里不带 @，用来模拟群友闲聊（不会触发机器人）")
    ap.add_argument("--reply", action="store_true",
                    help="带一个 reply 段（引用一条假消息），验证 bot 会不会去取原文")
    ap.add_argument("--poke", action="store_true",
                    help="发一条拍一拍事件；--target 默认是机器人自己")
    ap.add_argument("--target", type=int, default=0,
                    help="拍一拍的目标 QQ（默认机器人自己）")
    ap.add_argument("--user-id", type=int, default=10001)
    ap.add_argument("--nick", default="", help="发送者昵称，测记忆功能时用")
    ap.add_argument("--self-id", type=int, default=20002)
    ap.add_argument("--text", default="自检 ping")
    ap.add_argument("--image", default="",
                    help="附一张图片的路径，验证看图链路")
    ap.add_argument("--image-mode", choices=["url", "b64", "file"], default="url",
                    help="图片段带什么字段：url=临时 HTTP 服务（最接近真机）、"
                         "b64=直接 base64、file=逼 bot 调 get_image")
    ap.add_argument("--timeout", type=float, default=40)
    ap.add_argument("--idle", type=float, default=3.0,
                    help="收到第一条后等多久没新消息就收工（秒）；"
                         "测「先回一句再异步发」的功能时调大")
    args = ap.parse_args()
    if args.image and not Path(args.image).is_file():
        print(f"[!] 找不到图片：{args.image}")
        return 1
    args.url_port = start_static(Path(args.image).resolve().parent) if args.image else 0

    if args.poke:
        where = "拍一拍"
    else:
        where = f"群 {args.group}" if args.group else "私聊"
    if args.image:
        where += f"（带图/{args.image_mode}）"
    async with ws_connect(args.url) as ws:
        print(f"[*] 已连上 {args.url}，发送{where}事件…")
        await ws.send(json.dumps(build_event(args), ensure_ascii=False))

        loop = asyncio.get_running_loop()
        deadline = loop.time() + args.timeout
        got: list[dict] = []

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            # 第一条等满 timeout；之后只要 idle 秒没新消息就收工
            wait = min(remaining, args.idle) if got else remaining
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=wait)
            except asyncio.TimeoutError:
                break

            data = json.loads(raw)
            echo = data.get("echo")
            action = data.get("action")

            # bot 连上时会发 get_login_info 探测，真实 NapCat 会回应；这里也回一下
            if echo == "probe":
                await ws.send(json.dumps({
                    "status": "ok", "retcode": 0, "echo": "probe",
                    "data": {"user_id": args.self_id, "nickname": "selftest"},
                }, ensure_ascii=False))
                continue

            # bot 通过 OneBot 调 NapCat 的接口时，假装成 NapCat 回应。
            # 只有 send_*_msg 是"下发消息"，其余带 echo 的都是 API 调用。
            if action and echo and not str(action).startswith("send_"):
                print(f"[mock] 响应 {action}")
                await ws.send(json.dumps({
                    "status": "ok", "retcode": 0, "echo": echo,
                    "data": mock_data(action),
                }, ensure_ascii=False))
                continue

            got.append(data)

        if not got:
            print(f"[!] {args.timeout:.0f} 秒内无回复 —— 被忽略或链路不通")
            return 2

        for d in got:
            body = json.dumps(d.get("params"), ensure_ascii=False)
            body = re.sub(r"base64://[A-Za-z0-9+/=]{20,}", "base64://<图片数据>", body)
            print(f"[+] {d.get('action')}")
            print(f"    {body[:800]}")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
