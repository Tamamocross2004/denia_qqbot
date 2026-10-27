"""群友分享的视频：抓链接 → 读元数据 → 必要时听声音。

三层，按代价递增：

  links(evt)        从消息段里挖链接（text / json 卡片 / xml / share / video 段）
  fetch(url)        B站 API 拿标题、UP主、时长、播放、简介、热评（1 秒级）
  transcript(...)   下音频跑 ASR（分钟级），结果落库，同一个视频只跑一次

设计取舍：
- **只要音频**，不下整段视频。B站的 dash.audio 免登录就能拿（30216/30232），
  10 分钟视频约 5MB，比视频小两个数量级。
- **ASR 用 CPU int8**。这台机器的 8GB 显存要留给语音合成，而且 ctranslate2
  在 Windows 上还缺 cublas64_12.dll；实测 small 模型 CPU 上 RTF≈0.18
  （137 秒音频 25 秒出稿），够用。
- **绕开 PyAV**。av 19 与 faster-whisper 1.2 不兼容（decode_audio 传
  metadata_errors 会 TypeError），所以自己用 wave + numpy 喂 float32 数组。
- **模型走 hf-mirror，且禁掉 Xet**（hf-xet 对镜像返回 401）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path

import store

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
CACHE_DIR = BASE / "logs" / "video"

# 模型下载走镜像；Xet 存储对镜像返回 401，必须关掉
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# B站的 WAF 对「完整的 Chrome UA」回 412，反而只认最朴素的 Mozilla/5.0（实测）。
# 普通网页再去用像浏览器的 UA。
UA = "Mozilla/5.0"
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 所有请求都显式绕开系统代理：本机的 Clash 会把 B站/CDN 也塞进代理，反而更慢
_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))

LINKS_RE = re.compile(r"https?://[^\s，。！？；：、）】」》\"'<>()\[\]]+")
BVID_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
# 手机分享卡片的 title 常常只是 app 名，不是视频标题
APP_NAMES = {"哔哩哔哩", "bilibili", "哔哩哔哩弹幕网", "QQ", "微信"}
AVID_RE = re.compile(r"(?:^|/)av(\d+)", re.I)
SHORT_HOSTS = ("b23.tv", "bili2233.cn", "bili22.cn", "v.douyin.com", "xhslink.com")

MAX_DOWNLOAD = 60 * 1024 * 1024      # 音频最多下 60MB（约 2 小时 64kbps）
ASR_SAMPLE_RATE = 16000

_model_lock = threading.Lock()
_model = None
_model_cfg: tuple | None = None
_asr_lock = threading.Lock()          # 同一时刻只跑一个转写，别把 CPU 打满


# ---------- 从消息里挖链接 ----------

def _clean_url(u: str) -> str:
    u = (u or "").strip().strip("<>\"'")
    for tail in ("。", "，", "！", "？", "；", "：", ")", "）", "】", "》"):
        if u.endswith(tail):
            u = u[:-1]
    return u


def _urls_in(raw: str) -> list[str]:
    """把字符串里的 http(s) 链接都挖出来。

    小程序卡片经常把目标链接塞进 mqqapi:// 的查询参数里、还是 URL 编码过的
    （...%3A%2F%2Fwww.bilibili.com%2Fvideo%2FBV...），所以解一次码再找一遍。
    """
    if not raw:
        return []
    text = raw.replace("&amp;", "&").replace("\\/", "/")
    found = [_clean_url(u) for u in LINKS_RE.findall(text)]
    if not found:
        try:
            decoded = urllib.parse.unquote(text)
        except Exception:
            decoded = text
        if decoded != text:
            found = [_clean_url(u) for u in LINKS_RE.findall(decoded)]
    return found


def _from_json_seg(raw: str) -> list[dict]:
    """QQ 的分享卡片：json 段里的 data.data 是一串 JSON。"""
    out: list[dict] = []
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [{"url": u, "title": ""} for u in _urls_in(raw)]

    meta = obj.get("meta") or {}
    # news/detail_1 是站外链接卡片，music/video 是站内内容卡片
    for key in ("news", "detail_1", "music", "video", "ark"):
        item = meta.get(key)
        if not isinstance(item, dict):
            continue
        url = (item.get("jumpUrl") or item.get("qqdocurl")
               or item.get("url") or "")
        title = (item.get("title") or "").strip()
        desc = (item.get("desc") or item.get("summary") or "").strip()
        # 手机分享的卡片 title 往往是「哔哩哔哩」这种 app 名，真标题在 desc 里
        if title in APP_NAMES and desc:
            title = desc
        if not url.startswith("http"):
            # 小程序卡片：mqqapi://... 里可能嵌着编码过的目标链接
            for u in _urls_in(str(item)):
                if "bilibili.com" in u or video_id(u):
                    url = u
                    break
        if url.startswith("http"):
            out.append({"url": _clean_url(url), "title": title[:120]})
    if not out:
        for u in _urls_in(raw):
            out.append({"url": u, "title": ""})
    return out


def _from_xml_seg(raw: str) -> list[dict]:
    """老式 xml 卡片：url 藏在属性里，标题在 <title> 里。"""
    text = (raw or "").replace("&amp;", "&")
    title = ""
    m = re.search(r"<title>([^<]{1,120})</title>", text)
    if m:
        title = m.group(1).strip()
    out = []
    for u in re.findall(r'url="(https?://[^"]+)"', text):
        out.append({"url": _clean_url(u), "title": title})
    if not out:
        for u in _urls_in(text):
            if "qun.qq.com" in u or "ti.qq.com" in u:      # 卡片自己的装饰链
                continue
            out.append({"url": u, "title": title})
    return out


def _dump_card(kind: str, data: dict) -> None:
    """认不出来的卡片存一份原始结构，下次照着它补解析。

    手机 QQ 和小程序的卡片结构跟电脑端不一样，光靠猜补不完。
    """
    try:
        path = BASE / "logs" / "video-unparsed.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 512 * 1024:
            path.replace(path.with_name("video-unparsed.1.jsonl"))
        trimmed = {}
        for k, v in (data or {}).items():
            trimmed[k] = v[:2000] if isinstance(v, str) else v
        line = json.dumps({"ts": int(time.time()), "type": kind, "data": trimmed},
                          ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def links(evt: dict) -> list[dict]:
    """把一条消息里能当视频/链接看的东西都挖出来，按出现顺序去重。"""
    raw: list[dict] = []
    for seg in evt.get("message") or []:
        if not isinstance(seg, dict):
            continue
        kind = seg.get("type")
        data = seg.get("data") or {}
        got: list[dict] = []
        if kind == "text":
            got = [{"url": u, "title": ""} for u in _urls_in(data.get("text") or "")]
        elif kind == "json":
            got = _from_json_seg(str(data.get("data") or ""))
        elif kind == "xml":
            got = _from_xml_seg(str(data.get("data") or ""))
        elif kind == "share":
            url = str(data.get("url") or "")
            if url.startswith("http"):
                got = [{"url": _clean_url(url),
                        "title": str(data.get("title") or "")[:120]}]
        elif kind == "video":
            url = str(data.get("url") or "")
            if url.startswith("http"):
                # 群友直接发的视频文件：不用元数据，下下来自己抽音轨
                got = [{"url": _clean_url(url), "title": "[群里的视频]",
                        "direct": True}]
        if kind in ("json", "xml", "share", "video") and not got:
            _dump_card(kind, data)
        raw += got

    seen: set[str] = set()
    out: list[dict] = []
    for item in raw:
        url = item.get("url") or ""
        if not url.startswith("http"):
            continue
        key = video_id(url) or url
        if key in seen:
            continue
        seen.add(key)
        item["url"] = url
        out.append(item)
    return out


def brief_of(seg: dict) -> str:
    """给群聊缓冲用的一行摘要，不联网。"""
    kind = seg.get("type")
    data = seg.get("data") or {}
    items: list[dict] = []
    if kind == "json":
        items = _from_json_seg(str(data.get("data") or ""))
    elif kind == "xml":
        items = _from_xml_seg(str(data.get("data") or ""))
    elif kind == "share":
        items = [{"url": str(data.get("url") or ""),
                  "title": str(data.get("title") or "")}]
    elif kind == "video":
        items = [{"url": str(data.get("url") or ""), "title": "[QQ 视频]"}]
    if not items:
        return "[链接]"
    first = items[0]
    title = (first.get("title") or "").strip()
    if title:
        return f"[视频] {title[:40]}"
    host = urllib.parse.urlparse(first.get("url") or "").netloc or "链接"
    return f"[链接] {host}"


# ---------- 平台识别 ----------

def video_id(url: str) -> str | None:
    """能一眼看出是哪个视频就返回稳定 id，否则 None（短链要先解析）。"""
    if not url:
        return None
    m = BVID_RE.search(url)
    if m:
        return m.group(1)
    if "bilibili.com" in url:
        m = AVID_RE.search(url)
        if m:
            return f"av{m.group(1)}"
    if "youtu.be/" in url or "youtube.com/watch" in url:
        m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
        if m:
            return f"yt:{m.group(1)}"
    if "douyin.com" in url:
        m = re.search(r"/video/(\d+)", url)
        if m:
            return f"dy:{m.group(1)}"
    return None


def _resolve_short(url: str, timeout: float = 8.0) -> str:
    """b23.tv / v.douyin.com 这类短链跟一次跳转。"""
    host = urllib.parse.urlparse(url).netloc.lower()
    if not any(h in host for h in SHORT_HOSTS):
        return url
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with _OPEN.open(req, timeout=timeout) as resp:
            return resp.geturl() or url
    except Exception:
        return url


# ---------- 元数据 ----------

def _get_json(url: str, referer: str = "https://www.bilibili.com/",
              timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": referer, "Accept": "application/json"})
    with _OPEN.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _og_title(url: str, timeout: float = 10.0) -> str:
    """B站以外的页面：抓 og:title，拿不到就拉倒。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with _OPEN.open(req, timeout=timeout) as resp:
            html = resp.read(400 * 1024).decode("utf-8", "replace")
    except Exception:
        return ""
    for pat in (r'property="og:title"\s+content="([^"]{1,160})"',
                r'content="([^"]{1,160})"\s+property="og:title"',
                r"<title>([^<]{1,160})</title>"):
        m = re.search(pat, html, re.I)
        if m:
            return m.group(1).strip()
    return ""


def _bili_meta(url: str) -> dict:
    bvid = video_id(url)
    if not bvid:
        return {}
    if bvid.startswith("av"):
        info = _get_json(f"https://api.bilibili.com/x/web-interface/view?aid={bvid[2:]}")
    else:
        info = _get_json(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
    if info.get("code") != 0:
        return {}
    d = info.get("data") or {}

    # 多 P：链接里带 ?p=2 就取第 2 个分 P 的 cid
    cid = d.get("cid")
    pages = d.get("pages") or []
    if pages:
        m = re.search(r"[?&]p=(\d+)", url)
        idx = int(m.group(1)) - 1 if m else 0
        if 0 <= idx < len(pages):
            cid = pages[idx].get("cid") or cid

    stat = d.get("stat") or {}
    return {
        "platform": "bilibili",
        "id": d.get("bvid") or bvid,
        "aid": d.get("aid"),
        "cid": cid,
        "title": (d.get("title") or "").strip(),
        "up": ((d.get("owner") or {}).get("name") or "").strip(),
        "seconds": int(d.get("duration") or 0),
        "views": int(stat.get("view") or 0),
        "desc": (d.get("desc") or "").strip(),
        "url": f"https://www.bilibili.com/video/{d.get('bvid') or bvid}",
    }


def _bili_hot(info: dict, limit: int = 3) -> list[str]:
    aid = info.get("aid")
    if not aid:
        return []
    try:
        data = _get_json("https://api.bilibili.com/x/v2/reply"
                         f"?type=1&oid={aid}&sort=2&ps={max(1, limit)}&pn=1")
    except Exception:
        return []
    if data.get("code") != 0:
        return []
    out = []
    for r in ((data.get("data") or {}).get("replies") or [])[:limit]:
        msg = ((r.get("content") or {}).get("message") or "").replace("\n", " ").strip()
        if msg:
            out.append(f"{r.get('like') or 0} 赞：{msg[:80]}")
    return out


def _direct_info(url: str, title: str = "") -> dict:
    """群友直接发的视频文件：没有页面元数据，靠 ffmpeg 抽音轨。"""
    import hashlib
    return {
        "platform": "QQ视频",
        "id": "qq:" + hashlib.md5(url.encode("utf-8")).hexdigest()[:16],
        "title": title or "群里发的视频",
        "up": "", "seconds": 0, "views": 0, "desc": "", "url": url,
        "hot": [], "audio": True, "direct": True,
    }


def fetch(url: str, title: str = "", direct: bool = False) -> dict:
    """同步抓元数据（在线程里跑）。失败返回带 error 的 dict。"""
    if direct:
        return _direct_info(url, title)
    url = _resolve_short(url)
    if "bilibili.com" in url or video_id(url) or "b23.tv" in url:
        info = _bili_meta(url)
        if info:
            info["hot"] = _bili_hot(info)
            info["audio"] = True
            return info
    title = _og_title(url)
    return {
        "platform": "其它",
        "id": video_id(url) or url,
        "title": title,
        "up": "",
        "seconds": 0,
        "views": 0,
        "desc": "",
        "url": url,
        "hot": [],
        "audio": False,
    }


# ---------- 音频 / ASR ----------

def _ffmpeg() -> str | None:
    for p in (ROOT / "tts" / "bin" / "ffmpeg.exe", ROOT / "tts" / "ffmpeg.exe"):
        if p.exists():
            return str(p)
    return shutil.which("ffmpeg")


def _bili_audio_url(info: dict) -> str:
    data = _get_json("https://api.bilibili.com/x/player/playurl"
                     f"?bvid={info['id']}&cid={info['cid']}&fnval=16",
                     referer=info.get("url") or "https://www.bilibili.com/")
    dash = ((data.get("data") or {}).get("dash") or {})
    audios = dash.get("audio") or []
    if not audios:
        return ""
    # 取码率最高的那路
    best = max(audios, key=lambda a: int(a.get("bandwidth") or 0))
    return best.get("baseUrl") or best.get("base_url") or ""


def _download(url: str, dst: Path, referer: str) -> int:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": referer or "https://www.bilibili.com/"})
    total = 0
    with _OPEN.open(req, timeout=30) as resp, open(dst, "wb") as fh:
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            fh.write(chunk)
            total += len(chunk)
            if total > MAX_DOWNLOAD:
                break
    return total


def _to_wav(src: Path, dst: Path, max_seconds: int) -> bool:
    exe = _ffmpeg()
    if not exe:
        return False
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    if max_seconds > 0:
        cmd += ["-t", str(max_seconds)]
    cmd += ["-ac", "1", "-ar", str(ASR_SAMPLE_RATE), str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       creationflags=0x08000000, encoding="utf-8", errors="ignore")
    return r.returncode == 0 and dst.exists()


def _read_wav(path: Path):
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getframerate() != ASR_SAMPLE_RATE:
            raise ValueError("需要 16k 单声道 wav")
        raw = w.readframes(w.getnframes())
    import numpy as np
    return np.frombuffer(raw, dtype=np.int16).astype("float32") / 32768.0


def _whisper(asr: dict):
    """懒加载模型。第一次会读本地缓存（small 约 464MB）。"""
    global _model, _model_cfg
    key = (str(asr.get("model") or "small"),
           str(asr.get("device") or "cpu"),
           str(asr.get("compute_type") or "int8"))
    with _model_lock:
        if _model is None or _model_cfg != key:
            from faster_whisper import WhisperModel
            _model = WhisperModel(key[0], device=key[1], compute_type=key[2])
            _model_cfg = key
        return _model


def _asr_file(wav: Path, asr: dict) -> str:
    with _asr_lock:
        model = _whisper(asr)
        audio = _read_wav(wav)
        segs, _info = model.transcribe(
            audio, language="zh", vad_filter=True, beam_size=5,
            initial_prompt="以下是普通话的句子，用简体中文。")
        return "".join(s.text for s in segs).strip()


def transcribe(info: dict, asr: dict, max_seconds: int = 600) -> str:
    """同步转写（线程里跑）。同一个视频只跑一次，结果进库。"""
    vid = str(info.get("id") or "")
    if not vid:
        return ""
    cached = store.get_video(vid)
    if cached and cached.get("transcript"):
        return cached["transcript"]
    direct = bool(info.get("direct"))
    if not direct and (not info.get("audio") or not info.get("cid")):
        return ""

    url = info.get("url") if direct else _bili_audio_url(info)
    if not url:
        return ""

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="vid_", dir=str(CACHE_DIR)))
    try:
        raw = tmp / "a.m4s"
        wav = tmp / "a.wav"
        _download(url, raw, info.get("url") or "")
        if raw.stat().st_size < 1024:
            return ""
        if not _to_wav(raw, wav, max_seconds):
            return ""
        text = _asr_file(wav, asr)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if text:
        store.save_video(vid, info.get("title") or "", info.get("up") or "",
                         int(info.get("seconds") or 0), text)
    return text


def _get_bytes(url: str, referer: str = "https://www.bilibili.com/",
               timeout: float = 20.0, limit: int = 8 * 1024 * 1024) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
    with _OPEN.open(req, timeout=timeout) as resp:
        return resp.read(limit)


def frames(info: dict, count: int = 4, width: int = 640) -> list[bytes]:
    """从 B站自带的「视频快照」里裁几帧，**不下载视频本体**。

    `x/player/videoshot` 给的是一张 10x10 的雪碧图，每格 480x270，
    138 秒的视频有 26 格——足够看清在拍什么。纯色的格子（片头片尾黑屏）
    会被跳掉，剩下的按时间均匀挑 count 张。
    """
    if not info or info.get("platform") != "bilibili" or not info.get("cid"):
        return []
    try:
        data = _get_json("https://api.bilibili.com/x/player/videoshot"
                         f"?bvid={info['id']}&cid={info['cid']}&index=1",
                         referer=info.get("url") or "")
    except Exception:
        return []
    if data.get("code") != 0:
        return []
    d = data.get("data") or {}
    imgs = d.get("image") or []
    if not imgs:
        return []

    xl = int(d.get("img_x_len") or 10)
    yl = int(d.get("img_y_len") or 10)
    try:
        total = min(int(len(d.get("index") or [])), xl * yl)
    except (TypeError, ValueError):
        total = xl * yl
    if total <= 0:
        return []

    url = str(imgs[0])
    if url.startswith("//"):
        url = "https:" + url
    try:
        raw = _get_bytes(url, info.get("url") or "")
    except Exception:
        return []

    import io

    from PIL import Image, ImageStat
    try:
        sheet = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        return []

    cw, ch = sheet.width / xl, sheet.height / yl
    cells = []
    for k in range(total):
        col, row = k % xl, k // xl
        cell = sheet.crop((col * cw, row * ch, (col + 1) * cw, (row + 1) * ch))
        if sum(ImageStat.Stat(cell).stddev) / 3 < 8:
            continue                      # 纯色/黑屏，没信息
        cells.append(cell)
    if not cells:
        return []

    if len(cells) > count:
        step = len(cells) / count
        cells = [cells[min(int(i * step), len(cells) - 1)] for i in range(count)]

    out: list[bytes] = []
    for cell in cells:
        w = min(width, cell.width)
        h = max(1, int(cell.height * w / cell.width))
        buf = io.BytesIO()
        cell.resize((w, h)).save(buf, format="JPEG", quality=80)
        out.append(buf.getvalue())
    return out


async def fetch_async(url: str, title: str = "", direct: bool = False) -> dict:
    return await asyncio.to_thread(fetch, url, title, direct)


async def transcript_async(info: dict, asr: dict, max_seconds: int) -> str:
    return await asyncio.to_thread(transcribe, info, asr, max_seconds)


# ---------- 给模型看的文本 ----------

def describe(info: dict, transcript: str = "") -> str:
    if not info:
        return ""
    title = info.get("title") or "（没抓到标题）"
    lines = [f"【群友分享的视频】《{title}》"]
    meta = []
    if info.get("up"):
        meta.append(f"UP主：{info['up']}")
    secs = int(info.get("seconds") or 0)
    if secs:
        meta.append(f"时长 {secs // 60} 分 {secs % 60} 秒")
    if info.get("views"):
        meta.append(f"{info['views']} 播放")
    if info.get("platform") and info["platform"] != "bilibili":
        meta.append(f"平台：{info['platform']}")
    if meta:
        lines.append("｜".join(meta))
    desc = (info.get("desc") or "").strip().replace("\n", " ")
    if desc:
        lines.append(f"简介：{desc[:200]}")
    hot = info.get("hot") or []
    if hot:
        lines.append("【网友热评】")
        lines += [f"- {h}" for h in hot[:3]]
    if transcript:
        body = transcript
        if len(body) > 1400:
            body = body[:1100] + "……（中间省略）……" + body[-250:]
        lines.append("【音频转写（机器识别，专有名词可能有错，别当原文引用）】")
        lines.append(body)
    elif info.get("audio"):
        lines.append("【音频还没转写，只有标题和热评】")
    return "\n".join(lines)


def cleanup(keep_days: int = 7) -> None:
    """顺手清缓存：库里的旧转写 + 残留的临时目录。"""
    try:
        store.prune_video(keep_days)
    except Exception:
        pass
    if not CACHE_DIR.exists():
        return
    cutoff = time.time() - 86400
    for p in CACHE_DIR.iterdir():
        try:
            if p.is_dir() and p.stat().st_mtime < cutoff:
                shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass


# ---------- 群里发过的视频（主动捡来吐槽用） ----------

_shares: dict[int, list[dict]] = {}
PICKED: dict[int, float] = {}


def note_share(gid, link: dict, uid, nick: str) -> None:
    """记下"谁在哪个群发了什么链接"，只存内存，重启就忘。"""
    if not gid:
        return
    url = link.get("url") or ""
    if not url.startswith("http"):
        return
    bucket = _shares.setdefault(int(gid), [])
    vid = video_id(url) or url
    for item in bucket:
        if item["vid"] == vid:
            item["ts"] = time.time()
            return
    bucket.append({"vid": vid, "url": url, "title": link.get("title") or "",
                   "uid": uid, "nick": nick, "ts": time.time(), "done": False,
                   "direct": bool(link.get("direct"))})
    del bucket[:-8]


def pick_fresh(gid, within: float = 1800) -> dict | None:
    """挑一条还没被她评论过、且是最近发的视频。"""
    if not gid:
        return None
    now = time.time()
    for item in _shares.get(int(gid), []):
        if item.get("done"):
            continue
        if now - item["ts"] > within:
            continue
        return item
    return None


def pick_recent(gid, within: float = 600) -> dict | None:
    """最近发过的那一条，不管她有没有评论过。

    手机 QQ 分享是「先甩一条链接、再单独 @ 一句看看」两步，
    所以被问到时当前消息里多半没有链接，得回头去找上一条。
    """
    if not gid:
        return None
    now = time.time()
    best = None
    for item in _shares.get(int(gid), []):
        if now - item["ts"] <= within:
            best = item
    return best


def mark_done(gid, vid: str) -> None:
    for item in _shares.get(int(gid), []):
        if item["vid"] == vid:
            item["done"] = True
