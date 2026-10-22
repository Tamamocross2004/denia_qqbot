"""QQ 机器人 x DeepSeek —— OneBot v11 反向 WebSocket 接入。

NapCat 主动反连本服务，事件走 JSON，回复通过 send_group_msg / send_private_msg 下发。
"""

import asyncio
import ast
import base64
import json
import logging
import operator
import os
import random
import re
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from openai import AsyncOpenAI

import store

try:
    from websockets.asyncio.server import serve as ws_serve
except ImportError:  # websockets < 13
    from websockets.server import serve as ws_serve

BASE = Path(__file__).resolve().parent
LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(exist_ok=True)


def load_env(path: Path) -> None:
    """极简 .env 解析，避免额外依赖。

    注意：.env 优先级高于系统环境变量。本机系统里存着一个旧的
    DEEPSEEK_API_KEY，若用 setdefault 会让 .env 里填的 key 失效。
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip().strip('"').strip("'")
        if val:
            os.environ[key.strip()] = val


load_env(BASE / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("qqbot")

CFG_PATH = BASE / "config.json"
_cfg_cache: dict = {"mtime": -1.0, "data": {}}


def cfg() -> dict:
    """读 config.json；文件改动后按 mtime 自动重载。"""
    try:
        mtime = CFG_PATH.stat().st_mtime
    except OSError:
        return _cfg_cache["data"]
    if mtime != _cfg_cache["mtime"]:
        try:
            data = json.loads(CFG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("config.json 读取失败，沿用旧配置：%s", exc)
            return _cfg_cache["data"]
        _cfg_cache["data"] = data
        _cfg_cache["mtime"] = mtime
        log.info("config.json 已重载")
    return _cfg_cache["data"]


def trigger_cfg() -> dict:
    return cfg().get("trigger") or {}


API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
HOST = str(cfg().get("host") or "0.0.0.0")
PORT = int(cfg().get("port", 8080))

client = AsyncOpenAI(api_key=API_KEY or "missing", base_url="https://api.deepseek.com")

history: dict[str, list[dict]] = {}
locks: dict[str, asyncio.Lock] = {}
last_seen: dict[str, float] = {}
seen_ids = deque(maxlen=800)


# ---------- 消息解析 ----------

def segments(evt: dict) -> list[dict]:
    msg = evt.get("message")
    return msg if isinstance(msg, list) else []


def text_of(evt: dict) -> str:
    return "".join(
        s.get("data", {}).get("text", "")
        for s in segments(evt)
        if s.get("type") == "text"
    )


def at_me(evt: dict) -> bool:
    self_id = str(evt.get("self_id", ""))
    return any(
        s.get("type") == "at" and str(s.get("data", {}).get("qq")) == self_id
        for s in segments(evt)
    )


def name_hit(text: str) -> bool:
    """没 @、但直接喊了名字，也算在叫她。"""
    names = trigger_cfg().get("names") or []
    return any(isinstance(n, str) and n.strip() and n.strip() in text
               for n in names)


def time_hint() -> str:
    """把当前时段塞进 system，让她知道几点——不用再让模型调 get_time。"""
    now = datetime.now()
    h = now.hour
    if h < 1:
        mood = "已经过零点了，你困到脑子发钝，回一两句就收，别展开"
    elif h < 6:
        mood = "凌晨还没睡，半梦半醒，能少说就少说，最多一句"
    elif h < 9:
        mood = "清早刚被吵醒，有起床气，比平时更敷衍"
    elif h < 12:
        mood = "上午，勉强醒着，还想趴回去"
    elif h < 14:
        mood = "中午饭点，正犯困"
    elif h < 18:
        mood = "下午，正常状态，但心已经飘到床上了"
    elif h < 23:
        mood = "晚上，精神稍微好一点点"
    else:
        mood = "深夜，困得不行，句子更短，省略号更多"
    wd = "一二三四五六日"[now.weekday()]
    return (f"【现在是 {now:%Y-%m-%d %H:%M} 周{wd}】{mood}。"
            "时间是真的，别自己编；没人问就别主动报时间。")


def session_key(evt: dict) -> str:
    if evt.get("message_type") == "group":
        return f"g{evt.get('group_id')}u{evt.get('user_id')}"
    return f"p{evt.get('user_id')}"


def strip_prefix(text: str) -> str | None:
    """命中前缀则返回去掉前缀的正文，否则 None。"""
    for p in trigger_cfg().get("prefixes") or ["/ds"]:
        if text.startswith(p):
            return text[len(p):].strip()
    return None


# ---------- DeepSeek ----------

async def _chat_loop(msgs: list[dict], params: dict, ctx: dict,
                     rounds: int = 4) -> tuple[str, set[str]]:
    """跑工具调用循环，返回 (最终回答, 这次用过哪些工具)。"""
    used: set[str] = set()
    for _ in range(rounds):
        resp = await asyncio.wait_for(
            client.chat.completions.create(messages=msgs, **params), timeout=90
        )
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return (msg.content or "").strip(), used
        used.update(t.function.name for t in msg.tool_calls)
        log.info("工具调用：%s", [t.function.name for t in msg.tool_calls])
        msgs.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [t.model_dump() for t in msg.tool_calls],
        })
        for tc in msg.tool_calls:
            try:
                targs = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                targs = {}
            result = await run_tool(tc.function.name, targs, ctx)
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": result})
    return "……绕不出来了，你换个说法再问一遍。", used


# 回复里露了这些，说明其实没看懂。
# 除了直说「不知道」，反问式（「XX 是什么新型饮料吗」）也算——模型经常用反问
# 掩饰不懂，光匹配「不知道」会漏掉一大半。
CONFUSED_RE = re.compile(
    r"(听不懂|没听懂|看不懂|不知道|没听过|没印象|不认识|什么梗|啥梗|不太懂"
    r"|是什么.{0,8}[吗呢啊]|什么.{0,6}[啊呀]|哪来的词|第一次听说|哪学的)")

FORCE_SEARCH_HINT = (
    "【系统提示】你刚才没查就说不知道了。现在先调用 web_search 查一下再回答。"
    "查不到就直说「查了，网上也没有」，不要装作知道。"
)


async def ask(key: str, prompt: str, ctx: dict | None = None,
              extra: str = "", images: list[bytes] | None = None) -> str:
    c = cfg()
    max_turns = int(c.get("max_history_turns", 10))
    hist = history.setdefault(key, [])

    # 历史里只留文字版：多模态 content 带着 base64 滚进后续每一轮，token 会炸
    stored = prompt if not images else f"{prompt}\n（还发了一张图）"
    hist.append({"role": "user", "content": stored})

    system = c.get("system_prompt", "")
    if (c.get("schedule") or {}).get("enable", True):
        system = f"{system}\n\n{time_hint()}"

    msgs = ([{"role": "system", "content": system}]
            + hist[-max_turns * 2:])

    # 当前问题放最前面。原来是背景在前，模型经常抓着背景里某个话题答，
    # 实测问「123」它答上一轮的歌词梗。
    body = (f"{prompt}\n\n【下面是背景资料，别逐条回应，只回答上面那句话】\n{extra}"
            if extra else prompt)
    if images:
        parts: list[dict] = [{"type": "text", "text": body}]
        for b in images:
            mime = _sniff_mime(b) or "image/jpeg"
            parts.append({"type": "image_url", "image_url": {
                "url": f"data:{mime};base64,{base64.b64encode(b).decode()}"
            }})
        msgs[-1] = {"role": "user", "content": parts}
    elif extra:
        # 群聊背景只作用于这一次请求，不写进历史，否则越滚越长
        msgs[-1] = {"role": "user", "content": body}

    params = {
        "model": c.get("model", "deepseek-flash"),
        "max_tokens": int(c.get("max_output_tokens", 1024)),
    }
    if bool(c.get("thinking", False)):
        params["extra_body"] = {"thinking": {"type": "enabled"}}
    else:
        # V4 默认开思考，机器人场景必须显式关闭，否则慢且贵
        params["extra_body"] = {"thinking": {"type": "disabled"}}
        params["temperature"] = float(c.get("temperature", 0.7))

    if images:
        # 视觉模型不一定支持 function calling，带图那次干脆不给工具
        params["model"] = ((c.get("vision") or {}).get("model")
                           or VISION_FALLBACK)
    elif (c.get("tools") or {}).get("enable", True):
        specs = TOOLS_SPEC
        if (c.get("schedule") or {}).get("enable", True):
            # 时间已经写进 system 了，把 get_time 撤掉，省一次工具往返
            specs = [t for t in specs if t["function"]["name"] != "get_time"]
        params["tools"] = specs

    answer, used = await _chat_loop(msgs, params, ctx or {}, 4)

    # 她说「不知道」但其实一次都没查——逼她补一轮搜索再重新回答。
    # 这是针对「群友说个听不懂的词，她直接敷衍过去」最直接的修法：
    # 靠 prompt 求她主动查没用，改成事后发现再补救。
    if (answer and "web_search" not in used and not images
            and (c.get("search") or {}).get("enable", True)
            and CONFUSED_RE.search(answer)):
        log.info("回复露了「不知道」但没查过，补一轮搜索")
        msgs.append({"role": "assistant", "content": answer})
        msgs.append({"role": "user", "content": FORCE_SEARCH_HINT})
        try:
            retry, used2 = await _chat_loop(msgs, params, ctx or {}, 3)
        except Exception as exc:
            log.warning("补搜失败，沿用原回答：%s", exc)
            retry, used2 = "", set()
        if retry and "web_search" in used2:
            answer = retry

    hist.append({"role": "assistant", "content": answer})
    del hist[:-max_turns * 2]
    return answer


# ---------- OneBot 发送 ----------

async def send(ws, evt: dict, text: str, quote: bool = False) -> None:
    """quote=True 时带上 reply 段，让消息挂在原消息下面。"""
    if evt.get("message_type") == "group":
        action = "send_group_msg"
        params = {"group_id": evt["group_id"]}
    else:
        action = "send_private_msg"
        params = {"user_id": evt["user_id"]}

    mid = evt.get("message_id")
    if quote and mid not in (None, ""):
        params["message"] = [
            {"type": "reply", "data": {"id": str(mid)}},
            {"type": "text", "data": {"text": text}},
        ]
    else:
        params["message"] = text
    await ws.send(json.dumps({"action": action, "params": params, "echo": "reply"}))


async def reply(ws, evt: dict, text: str, quote: bool | None = None) -> None:
    if quote is None:
        quote = bool(trigger_cfg().get("quote_reply", True))
    size = int(cfg().get("reply_chunk", 800))
    first = True
    for i in range(0, len(text), size):
        chunk = text[i:i + size].strip()
        if chunk:
            # 只有第一块引用，分块时不会每块都重复引用同一条
            await send(ws, evt, chunk, quote=quote and first)
            first = False


# ---------- OneBot 调用与工具 ----------

pending: dict[str, asyncio.Future] = {}


async def call_api(ws, action: str, params: dict | None = None,
                   timeout: float = 10.0) -> dict:
    """向 NapCat 发一个 action 并等它的响应，靠唯一 echo 匹配。"""
    eid = uuid.uuid4().hex[:12]
    fut = asyncio.get_running_loop().create_future()
    pending[eid] = fut
    try:
        await ws.send(json.dumps(
            {"action": action, "params": params or {}, "echo": eid},
            ensure_ascii=False))
        return await asyncio.wait_for(fut, timeout=timeout)
    finally:
        pending.pop(eid, None)


TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "获取当前日期、星期和精确时间。系统提示里没给时间时才用；给了就直接答。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "roll",
            "description": "掷骰子或随机抽取。用于随机数、抽人、抛硬币、骰子游戏。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sides": {"type": "integer",
                              "description": "骰子面数，掷出 1~N。默认 100"},
                    "count": {"type": "integer",
                              "description": "掷几次，默认 1，最多 10"},
                    "choices": {
                        "type": "array", "items": {"type": "string"},
                        "description": "给定候选名单时从中随机选一个（比如从群成员里抽人），此时忽略 sides",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calc",
            "description": "精确计算数学表达式。涉及四则运算、乘方、取余时用它，不要自己心算。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string",
                                   "description": "算式，如 (1200-350)*3/7 或 2**10 % 7"},
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "group_info",
            "description": "查询当前 QQ 群的名称、人数和群主。只能在群聊里用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer",
                                 "description": "群号，不填就用当前群"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": ("联网查某个词、梗、缩写、事件或时效信息的准确含义和出处。"
                            "拿不准的词义、或者问题涉及时效信息（最新/最近/今年/价格/版本）时调用。"
                            "群里的内部梗和外号不要搜——网上根本没有，搜了只会得到无关内容把你带偏。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "搜索词，尽量具体，如「典中典 梗 意思」"},
                },
                "required": ["query"],
            },
        },
    },
]

_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _eval_node(node):
    """只放行数字和算术运算，绝不 eval。"""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp):
        fn = _BINOPS.get(type(node.op))
        if fn is None:
            raise ValueError("不支持这个运算符")
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 64:
            raise ValueError("指数太大了")
        return fn(left, right)
    if isinstance(node, ast.UnaryOp):
        fn = _UNARY.get(type(node.op))
        if fn is None:
            raise ValueError("不支持这个运算符")
        return fn(_eval_node(node.operand))
    raise ValueError("表达式里只能有数字和 + - * / // % **")


def tool_get_time() -> str:
    now = datetime.now()
    week = "一二三四五六日"[now.weekday()]
    return f"{now:%Y-%m-%d} 星期{week} {now:%H:%M:%S}"


def tool_roll(args: dict) -> str:
    choices = args.get("choices") or []
    if isinstance(choices, list) and choices:
        pick = random.choice([str(c) for c in choices])
        return f"从 {len(choices)} 个候选里抽中了：{pick}"

    try:
        sides = int(args.get("sides") or 100)
        count = int(args.get("count") or 1)
    except (TypeError, ValueError):
        sides, count = 100, 1
    sides = max(2, min(sides, 10 ** 6))
    count = max(1, min(count, 10))

    rolls = [random.randint(1, sides) for _ in range(count)]
    if count == 1:
        return f"1~{sides} 掷出：{rolls[0]}"
    return f"1~{sides} 掷 {count} 次：{', '.join(map(str, rolls))}｜合计 {sum(rolls)}"


def tool_calc(args: dict) -> str:
    expr = str(args.get("expression") or "").strip()
    if not expr:
        raise ValueError("算式是空的")
    if len(expr) > 200:
        raise ValueError("算式太长了")
    value = _eval_node(ast.parse(expr, mode="eval"))
    if isinstance(value, float):
        if value.is_integer():
            return f"{expr} = {int(value)}"
        return f"{expr} = {round(value, 10):g}"
    return f"{expr} = {value}"


async def tool_group_info(args: dict, ctx: dict) -> str:
    ws = ctx.get("ws")
    gid = args.get("group_id") or ctx.get("group_id")
    if not gid:
        return "这里是私聊，没有群信息可查。"
    if ws is None:
        return "当前拿不到 QQ 接口。"
    resp = await call_api(ws, "get_group_info", {"group_id": gid})
    if resp.get("status") != "ok":
        return f"查不到：{resp.get('message') or resp.get('retcode')}"
    d = resp.get("data") or {}
    return (f"群名：{d.get('group_name')}｜人数：{d.get('member_count')}"
            f"｜群主：{d.get('owner_id')}")


# ---------- 联网搜索 ----------

AUTO_GLOSSARY = BASE / "glossary_auto.json"
AUTO_MAX = 200
SEARCH_URL = "https://api.deepseek.com/v1/tools/search"


def _post_json(url: str, payload: dict, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


async def deepseek_search(query: str, limit: int = 3) -> list[dict]:
    """调 DeepSeek 自研搜索接口。urllib 是同步的，丢线程池跑。"""
    loop = asyncio.get_running_loop()
    try:
        data = await asyncio.wait_for(
            loop.run_in_executor(None, _post_json, SEARCH_URL, {"query": query}),
            timeout=25)
    except Exception as exc:
        log.warning("搜索失败：%s", exc)
        return []
    results = [r for r in (data.get("results") or []) if isinstance(r, dict)]
    # 有 rerank_score 就按它排，没有就保持接口原序
    results.sort(key=lambda r: float(r.get("rerank_score") or 0), reverse=True)
    return results[:limit]


def _extract_term(query: str) -> str:
    """从「鸣潮 卡提希娅 是谁」这类 query 里抠出词条本身。"""
    stop = {"梗", "意思", "是什么", "什么", "出处", "来源", "的", "吗", "啊", "呢",
            "鸣潮", "游戏", "角色", "人物", "介绍", "背景", "设定", "资料"}
    s = re.sub(
        r"(是什么意思|什么意思|是什么梗|什么梗|的出处|出自哪里|出处|来源|梗|意思|是谁|是什么)$",
        "", query)
    parts = [p for p in re.split(r"[\s,，、。？！?！的]+", s) if p and p not in stop]
    if not parts:
        return ""
    # 取最长的一段：'鸣潮 卡提希娅' 该取后者而不是前者
    term = max(parts, key=len)
    # 模型爱给 query 加引号，别把引号也收进词条名
    return re.sub(r"[\"'“”「」『』]|这词|这个词", "", term).strip()


def remember_search(query: str, top: dict) -> None:
    """把搜到的定义沉淀进 auto 词典，下次直接命中，不再花搜索费。"""
    snippet = (top.get("snippet") or top.get("summary") or "").strip()
    score = float(top.get("rerank_score") or 0)
    if not snippet or score < 1.6:
        return          # 实测噪音多在 1.5 以下，宁可漏收也别污染词典
    term = _extract_term(query)
    if not (2 <= len(term) <= 12):
        return
    # 去掉数字和年月日后太短的（"2026年"这种）不是词条
    if len(re.sub(r"[0-9\s年月日]", "", term)) < 2:
        return
    try:
        data = json.loads(AUTO_GLOSSARY.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    data.pop(term, None)          # 重新插到末尾，裁剪时优先丢旧的
    data[term] = "（网上搜的，未必准）" + snippet[:110]
    if len(data) > AUTO_MAX:
        data = dict(list(data.items())[-AUTO_MAX:])
    try:
        AUTO_GLOSSARY.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("自动收录词条：%s", term)
    except OSError as exc:
        log.warning("写入 auto 词典失败：%s", exc)


async def tool_web_search(args: dict, ctx: dict) -> str:
    if not (cfg().get("search") or {}).get("enable", True):
        return "搜索功能被关掉了。"
    query = str(args.get("query") or "").strip()[:60]
    if not query:
        return "搜索词是空的。"

    results = await deepseek_search(query, limit=3)
    if not results:
        return "网上没搜到相关的东西，可能太冷门了。"

    lines = []
    for r in results:
        title = str(r.get("title") or "").strip()
        snip = str(r.get("snippet") or r.get("summary") or "").strip()
        if not (title or snip):
            continue
        score = float(r.get("rerank_score") or 0)
        body = f"{title}：{snip[:170]}" if title else snip[:170]
        lines.append(f"- [{score:.1f}] {body}")
    if not lines:
        return "搜到了但内容都是空的。"

    remember_search(query, results[0])
    return ("（来自网络搜索。方括号是相关度 0~3，低于 1.6 多半只是字面撞词、"
            "不可信；都不靠谱就直接说没查到，别硬编）\n" + "\n".join(lines))


async def run_tool(name: str, args: dict, ctx: dict) -> str:
    try:
        if name == "get_time":
            return tool_get_time()
        if name == "roll":
            return tool_roll(args)
        if name == "calc":
            return tool_calc(args)
        if name == "group_info":
            return await tool_group_info(args, ctx)
        if name == "web_search":
            return await tool_web_search(args, ctx)
        return f"没有叫 {name} 的工具"
    except Exception as exc:
        log.warning("工具 %s 出错：%s", name, exc)
        return f"工具出错了：{exc}"


# ---------- 表情包 ----------

STICKER_DIR = BASE / "stickers"
STICKER_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
STICKER_RE = re.compile(r"\[\[em:([^\[\]]{1,20})\]\]")

# 模型忘了加标记时的兜底：按回复内容猜情绪。
# 顺序有意义——越具体的排前面，命中就停。
STICKER_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("困", ("困", "累", "睡", "哈欠", "眯", "趴", "没精神", "撑不住", "犯困", "熬夜")),
    # 「嫌弃」已经并进「无语」——两者边界太模糊，分开发只是让素材分散
    ("无语", ("无语", "离谱", "服了", "什么鬼", "莫名其妙", "说不过去", "随便吧",
              "嫌弃", "烦死", "讨厌", "走开", "啧", "懒得", "蠢", "傻", "别闹")),
    ("开心", ("开心", "好耶", "笑死", "不错", "可爱", "喜欢", "谢谢", "挺好", "顺利")),
]

# 这些内容不适合配表情包，模型硬加也拦掉
TECH_MARKERS = (
    "def ", "import ", "return ", "class ", "error", "exception", "traceback",
    "报错", "代码", "函数", "参数", "变量", "接口", "编译", "配置", "部署",
    "none", "dict", "list", "tuple", "str(", "int(", "null", "true", "false",
)


def looks_technical(text: str) -> bool:
    """技术性回复不配表情包——问报错还甩个困脸很奇怪。"""
    low = text.lower()
    if any(m in low for m in TECH_MARKERS):
        return True
    letters = sum(1 for c in text if c.isascii() and c.isalpha())
    return bool(text) and letters / len(text) > 0.35


def _tag_has_image(tag: str) -> bool:
    folder = STICKER_DIR / tag
    return folder.is_dir() and any(
        p.is_file() and p.suffix.lower() in STICKER_EXTS
        for p in folder.iterdir()
    )


def auto_tag(text: str) -> str | None:
    """回复里没标记时，按内容兜一个情绪标签。

    只在回复够长、概率命中、且该标签真有图时才返回，
    避免把每条回复都变成图 + 短句。
    """
    st = cfg().get("sticker") or {}
    if not st.get("auto_fill", True):
        return None
    if len(text) < 5:
        return None
    if random.random() > float(st.get("auto_chance", 0.45)):
        return None
    for tag, words in STICKER_HINTS:
        if any(w in text for w in words) and _tag_has_image(tag):
            return tag
    return None


def pick_sticker(tag: str) -> bytes | None:
    """按标签从 stickers/<标签>/ 里随机挑一张图。"""
    folder = STICKER_DIR / tag.strip()
    if not folder.is_dir():
        return None
    files = [p for p in folder.iterdir()
             if p.is_file() and p.suffix.lower() in STICKER_EXTS]
    if not files:
        return None
    try:
        return random.choice(files).read_bytes()
    except OSError:
        return None


async def send_image(ws, evt: dict, data: bytes) -> None:
    """NapCat 的 enableLocalFile2Url 默认 false，本地路径发不出去，只能 base64。"""
    seg = [{"type": "image",
            "data": {"file": "base64://" + base64.b64encode(data).decode()}}]
    if evt.get("message_type") == "group":
        action, params = "send_group_msg", {"group_id": evt["group_id"], "message": seg}
    else:
        action, params = "send_private_msg", {"user_id": evt["user_id"], "message": seg}
    await ws.send(json.dumps({"action": action, "params": params, "echo": "sticker"}))


_sticker_hist: dict[str, deque] = {}


async def reply_with_media(ws, evt: dict, text: str,
                           quote: bool | None = None) -> None:
    """剥掉 [[em:xxx]] 标记：先发文字，再按预算补表情包。

    quote=None 时按 config 的 quote_reply；主动插话传 False，不引用任何消息。
    """
    st = cfg().get("sticker") or {}
    tags = STICKER_RE.findall(text)
    clean = STICKER_RE.sub("", text).strip()
    if not tags and clean:
        # 模型这次忘了加标记，按内容兜一个
        guess = auto_tag(clean)
        if guess:
            tags = [guess]
            log.info("兜底补了一个表情包：%s", guess)

    # 技术性回复直接不给图
    if tags and clean and looks_technical(clean):
        tags = []
        log.info("技术性回复，不带表情包")

    # 模型对"频率"没有概念，prompt 说一半它也能做到每条都贴。
    # 所以这里硬性限流：最近 N 条都带图了，这条就省掉。
    key = session_key(evt)
    streak = max(1, int(st.get("max_streak", 2)))
    hist = _sticker_hist.setdefault(key, deque(maxlen=streak))
    over = len(hist) == streak and all(hist)
    if tags and over:
        tags = []
        log.info("连续 %d 条都带图了，这条省掉", streak)

    if clean:
        await reply(ws, evt, clean, quote=quote)

    sent = 0
    if st.get("enable", True) and tags:
        budget = max(1, int(st.get("max_per_reply", 1)))
        for tag in tags:
            if sent >= budget:
                break
            data = pick_sticker(tag)
            if data:
                await send_image(ws, evt, data)
                sent += 1
            else:
                log.debug("表情包缺失：%s", tag)
    hist.append(bool(sent))


# ---------- 看图 ----------

IMG_MAX_BYTES = 6 * 1024 * 1024
VISION_FALLBACK = "deepseek-v4-flash-vision-exp"

# 下载图片必须显式禁用代理。urllib 默认会读 HTTP_PROXY / 系统代理，
# 把访问 127.0.0.1 的 NapCat 图片请求塞进 Clash，直接连不上。
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _sniff_mime(data: bytes) -> str | None:
    if data[:3] == b"GIF":
        return "image/gif"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return None


def _download(url: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with _NO_PROXY.open(req, timeout=timeout) as resp:
        return resp.read(IMG_MAX_BYTES + 1)


async def image_to_bytes(ws, seg: dict) -> bytes | None:
    """一个 image 段 -> 原始字节。三级兜底，哪级通就用哪级。"""
    d = seg.get("data") or {}

    raw = d.get("base64")
    if isinstance(raw, str) and raw:
        try:
            data = base64.b64decode(raw)
            if _sniff_mime(data):
                return data
        except (ValueError, TypeError):
            pass

    url = d.get("url")
    if isinstance(url, str) and url.startswith("http"):
        try:
            data = await asyncio.to_thread(_download, url)
            if _sniff_mime(data):
                return data
        except Exception as exc:
            log.debug("图片 URL 下载失败：%s", exc)

    f = d.get("file")
    if isinstance(f, str) and f:
        try:
            resp = await call_api(ws, "get_image", {"file": f})
            info = resp.get("data") if isinstance(resp, dict) else None
            info = info if isinstance(info, dict) else {}
            path = info.get("file")
            if isinstance(path, str) and path:
                p = Path(path)
                if p.is_file():
                    data = p.read_bytes()
                    if _sniff_mime(data):
                        return data
            u = info.get("url")
            if isinstance(u, str) and u.startswith("http"):
                data = await asyncio.to_thread(_download, u)
                if _sniff_mime(data):
                    return data
        except Exception as exc:
            log.debug("get_image 失败：%s", exc)
    return None


async def collect_images(ws, evt: dict) -> list[bytes]:
    c = cfg().get("vision") or {}
    if not c.get("enable", True):
        return []
    segs = [s for s in segments(evt) if s.get("type") == "image"]
    limit = max(1, int(c.get("max_images", 2)))
    out: list[bytes] = []
    for seg in segs[:limit]:
        try:
            data = await image_to_bytes(ws, seg)
        except Exception as exc:
            log.debug("取图异常：%s", exc)
            continue
        if not data:
            continue
        if len(data) > IMG_MAX_BYTES:
            log.info("图片太大，跳过：%d 字节", len(data))
            continue
        out.append(data)
    if segs:
        log.info("收到 %d 张图，取到 %d 张", len(segs), len(out))
    return out


# ---------- 群聊上下文与黑话 ----------

GROUP_RECENT: dict[int, deque] = {}
MINE_PENDING: dict[int, int] = {}   # 各群攒了多少条还没提炼
RECENT_MAX = 30        # 每个群记最近多少条
# 这些是给 bot 的指令，不是聊天内容。留在背景里会误导——
# 实测「@她 日报」留在缓冲里，后来问她一个陌生名字，她编了一段日报来应付
CMD_WORDS = {"日报", "/日报", "早报", "/早报", "重置", "/reset",
             "状态", "/status", "帮助", "/help", "菜单", "/菜单",
             "总结", "/总结", "捋一下", "捋一捋"}


def _is_cmd_text(t: str) -> bool:
    """去掉 @ 段之后是不是一条指令。"""
    s = re.sub(r"@\d+", "", t or "").strip()
    if s in CMD_WORDS:
        return True
    # 「查 某人」要带分隔，不然「查到问题了」会被当成查人
    parts = s.replace("：", " ").split(maxsplit=1)
    return len(parts) == 2 and parts[0] in ("查", "查一下", "你记得")
RECENT_INJECT = 15     # 每次注入多少条给模型
GLOSSARY_PATH = BASE / "glossary.json"
LORE_PATH = BASE / "lore.json"

# 明显在问具体事实的句式；本地知识没命中时用来强制走搜索
# 明显在问具体事实的句式；本地知识没命中时用来强制走搜索
_ASK_RE = re.compile(
    r"(是谁|是什么|是啥|是不是|什么颜色|什么意思|啥意思|什么梗|啥梗|出自"
    r"|哪一年|哪来的|多少钱|怎么读|哪位|长什么样|什么来头|啥玩意|什么玩意"
    r"|干嘛的|谁的|真的假的|靠不靠谱)")


def looks_like_name(text: str) -> bool:
    """整条消息就是一个名字/ID，多半是在问「这是谁」。

    实测「南怀北瑾丨林川」这种没有任何疑问词，_ASK_RE 不命中，
    她就不查、随口编一段别的内容应付。
    """
    t = (text or "").strip()
    if not 2 <= len(t) <= 16:
        return False
    if t.isdigit() or " " in t:
        return False
    if re.search(r"[？?。！!，,、：:~～…]", t):
        return False
    # 带疑问词的交给 _ASK_RE，「这是什么意思」不该当成人名
    if re.search(r"(什么|怎么|为什么|哪|谁|多少|如何)", t):
        return False
    return bool(re.search(r"[\u4e00-\u9fa5A-Za-z]", t))


def _brief(evt: dict) -> str:
    """把一条消息压成一行摘要，供上下文用。"""
    parts = []
    for s in segments(evt):
        kind = s.get("type")
        data = s.get("data", {})
        if kind == "text":
            parts.append(data.get("text", ""))
        elif kind == "image":
            parts.append("[图片]")
        elif kind == "face":
            parts.append("[表情]")
        elif kind == "at":
            parts.append(f"@{data.get('qq')}")
        elif kind == "reply":
            parts.append("[回复]")
    return "".join(parts).strip()


def _nick_of_evt(evt: dict) -> str:
    """群名片优先，其次昵称。"""
    s = evt.get("sender") or {}
    return str(s.get("card") or s.get("nickname") or "").strip()[:40]


def _group_tracked(gid) -> bool:
    """只给白名单里的群写库。白名单为空 = 所有群都记。"""
    if not gid:
        return False
    wl = {int(g) for g in (trigger_cfg().get("groups") or []) if str(g).isdigit()}
    return not wl or gid in wl


def remember(evt: dict) -> bool:
    """所有群消息都记，不只 @ 它的。接梗靠的就是这些前因。

    返回 True 表示攒够了条数，该提炼一次记忆。
    """
    if evt.get("message_type") != "group":
        return False
    # 指令不当聊天记
    if _is_cmd_text(_brief(evt)) or text_of(evt).strip() in CMD_WORDS:
        return False
    line = _brief(evt)
    if not line:
        return False
    gid = evt.get("group_id")
    uid = int(evt.get("user_id") or 0)
    GROUP_RECENT.setdefault(gid, deque(maxlen=RECENT_MAX)).append(
        (str(uid), line))

    if not _group_tracked(gid):
        return False
    try:
        nick = _nick_of_evt(evt)
        store.touch_person(uid, nick)
        store.save_msg(gid, uid, nick, line)
    except Exception as exc:
        log.warning("写记忆库失败：%s", exc)
        return False

    every = max(5, int((cfg().get("memory") or {}).get("mine_every", 20)))
    n = MINE_PENDING.get(gid, 0) + 1
    if n < every:
        MINE_PENDING[gid] = n
        return False
    MINE_PENDING[gid] = 0
    return True


def recent_context(gid, limit: int = RECENT_INJECT) -> str:
    buf = GROUP_RECENT.get(gid)
    if not buf:
        return ""
    out = []
    for uid, txt in list(buf)[-limit:]:
        name = ""
        try:
            name = store.nick_of(int(uid))
        except (TypeError, ValueError):
            pass
        who = f"{name}({uid})" if name and name != str(uid) else uid
        out.append(f"{who}: {txt}")
    return "\n".join(out)


def restore_recent() -> None:
    """启动时把库里的缓冲灌回内存，重启后能接着聊。"""
    try:
        gids = [r["gid"] for r in store.connect().execute(
            "SELECT DISTINCT gid FROM msg").fetchall()]
    except Exception as exc:
        log.warning("读取记忆库失败：%s", exc)
        return
    for gid in gids:
        buf = GROUP_RECENT.setdefault(gid, deque(maxlen=RECENT_MAX))
        buf.clear()
        for m in store.recent_msgs(gid, RECENT_MAX):
            if _is_cmd_text(m["text"]):
                continue      # 老数据里可能存过指令，别再灌回背景
            buf.append((str(m["uid"]), m["text"]))
    if gids:
        log.info("已从记忆库恢复 %d 个群的聊天缓冲：%s", len(gids), store.ping())


# ---------- 记忆提炼与注入 ----------

MINE_PROMPT = """你在整理一个 QQ 群最近的聊天记录。

今天日期：{today}

说话人对照（昵称 -> QQ 号）：
{people}

聊天记录：
{lines}

从中提炼值得长期记住的信息，输出 JSON 数组，最多 4 条：
[{{"uid": 123456, "kind": "event", "content": "...", "weight": 0.7}}]

规则：
- uid 用上面对照表里的 QQ 号；属于整个群的填 0
- kind 只能是 person / event / meme
  person：某人的稳定特征、身份、习惯，不是他今天说了什么
  event：某人遇到的、以后可能还会提起的事
  meme：群里自造的词、内部梗、黑话
- content 一句陈述，不超过 40 字
- 用具体日期，别写「今天」「刚才」
- 只记有长期价值的。寒暄、单纯情绪、重复内容不记
- 没有值得记的就输出 []
- 只输出 JSON，不要解释，不要代码块
"""


def _parse_json_array(raw: str) -> list:
    """模型偶尔会套 ```json 围栏，抠出数组本体。"""
    if not raw:
        return []
    s = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    s = re.sub(r"\s*```$", "", s)
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j < i:
        return []
    try:
        data = json.loads(s[i:j + 1])
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


async def mine_memories(gid: int) -> None:
    """把最近 20 条聊天压成几条长期记忆。失败不影响聊天。"""
    c = cfg().get("memory") or {}
    if not c.get("enable", True):
        return
    try:
        msgs = store.recent_msgs(gid, 20)
    except Exception as exc:
        log.warning("读聊天缓冲失败：%s", exc)
        return
    if len(msgs) < 5:
        return

    uids = sorted({m["uid"] for m in msgs if m["uid"]})
    info = store.person_info(uids)
    people = "\n".join(
        f"- {info[u]['nick'] or u} -> {u}" for u in uids if u in info) or "（无）"
    lines = "\n".join(f"[{m['nick'] or m['uid']}] {m['text']}" for m in msgs)
    prompt = MINE_PROMPT.format(
        today=datetime.now().strftime("%Y-%m-%d"), people=people, lines=lines)

    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=cfg().get("model", "deepseek-flash"),
                messages=[{"role": "user", "content": prompt}],
                max_tokens=700,
                temperature=0.3,
                extra_body={"thinking": {"type": "disabled"}}), timeout=60)
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        log.warning("提炼记忆失败：%s", exc)
        return

    items = _parse_json_array(raw)
    if not items:
        log.info("[mine %s] 这批没什么值得记的", gid)
        return

    idx = store.nick_index()
    kept = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            uid = int(it.get("uid") or 0)
        except (TypeError, ValueError):
            uid = 0
        if uid not in uids:
            uid = 0                     # 模型编了个号，当群整体处理
        kind = str(it.get("kind") or "event").lower()
        if kind not in ("person", "event", "meme"):
            kind = "event"
        content = store.link_names(str(it.get("content") or ""), idx)
        if not content:
            continue
        try:
            weight = min(1.0, max(0.1, float(it.get("weight") or 0.5)))
        except (TypeError, ValueError):
            weight = 0.5
        try:
            store.add_memory(gid, uid, kind, content, weight)
            kept += 1
        except Exception as exc:
            log.warning("写记忆失败：%s", exc)
    log.info("[mine %s] %d 条聊天提炼出 %d 条记忆", gid, len(msgs), kept)


def memory_block(evt: dict, text: str, bg: str = "") -> str:
    """挑出跟这次对话相关的长期记忆，塞进上下文。"""
    c = cfg().get("memory") or {}
    if not c.get("enable", True):
        return ""
    gid = evt.get("group_id")
    if not gid:
        return ""

    uids: list[int] = []
    for s in segments(evt):
        if s.get("type") == "at":
            qq = str(s.get("data", {}).get("qq") or "")
            if qq.isdigit():
                uids.append(int(qq))
    try:
        for name, uid in store.nick_index().items():
            if name in text or name in bg:
                uids.append(uid)
    except Exception:
        pass

    try:
        rows = store.memories_for(gid, uids, limit=int(c.get("inject", 6)))
    except Exception as exc:
        log.warning("查记忆失败：%s", exc)
        return ""
    if not rows:
        return ""
    lines = [f"- {store.render_names(r['content'])}" for r in rows]
    return ("【你记得的事，都是真发生过的】\n" + "\n".join(lines)
            + "\n【这些你已经知道，别反问，也别一次全倒出来】")


def _load_glossaries() -> dict:
    """auto 词典先加载、手写词典后覆盖，所以手写的优先。"""
    merged: dict = {}
    for path in (AUTO_GLOSSARY, GLOSSARY_PATH):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            merged.update(data)
    return merged


def slang_hits(*texts: str, limit: int = 5) -> str:
    """只挑命中的词条注入，不整本塞进 prompt。"""
    gl = _load_glossaries()
    if not gl:
        return ""
    blob = "\n".join(t for t in texts if t)
    # 单字键误命中太凶（"急"会命中"着急"），只认两个字符以上
    hits = [f"- {k}：{v}" for k, v in gl.items()
            if isinstance(k, str) and len(k) >= 2 and k in blob]
    return "\n".join(hits[:limit])


def _lore_entries(data: dict):
    """兼容两种写法：值是字符串（无别名），或 {aka, desc}。"""
    for name, val in data.items():
        if isinstance(val, dict):
            aka = [str(a) for a in (val.get("aka") or []) if a]
            yield name, aka, str(val.get("desc") or "")
        else:
            yield name, [], str(val)


def lore_lookup(*texts: str, limit: int = 4) -> tuple[str, list[str]]:
    """返回 (要注入的描述文本, 命中的条目名列表)。"""
    blob = "\n".join(t for t in texts if t)
    if not blob:
        return "", []
    try:
        data = json.loads(LORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", []
    if not isinstance(data, dict) or not data:
        return "", []
    lines, names = [], []
    for name, aka, desc in _lore_entries(data):
        if len(name) < 2 or not desc:
            continue
        if not any(n and n in blob for n in [name, *aka]):
            continue
        names.append(name)
        label = f"{name}（群里也叫{'、'.join(aka)}）" if aka else name
        lines.append(f"- {label}：{desc}")
    return "\n".join(lines[:limit]), names


def lore_hits(*texts: str, limit: int = 4) -> str:
    return lore_lookup(*texts, limit=limit)[0]


# ---------- 角色关系网 ----------

REL_PATH = BASE / "relations.json"

# 组织/地点不算"人"，别拿它们去算"两人怎么连上的"
ORG_NODES = {"残星会", "星炬学院", "虚质科学部", "拉海洛", "虚质"}


def _load_edges() -> list[dict]:
    try:
        data = json.loads(REL_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    edges = data.get("edges") if isinstance(data, dict) else data
    if not isinstance(edges, list):
        return []
    return [e for e in edges
            if isinstance(e, dict) and e.get("src") and e.get("dst")]


def _edge_text(e: dict) -> str:
    note = str(e.get("note") or "").strip()
    line = f"{e['src']} —{e.get('rel') or '相关'}→ {e['dst']}"
    return f"{line}：{note}" if note else line


def relations_of(names: list[str], limit: int = 6) -> str:
    """这些名字直接参与的关系（一跳）。"""
    if not names:
        return ""
    want = set(names)
    seen, out = set(), []
    for e in _load_edges():
        if e["src"] not in want and e["dst"] not in want:
            continue
        line = _edge_text(e)
        if line not in seen:
            seen.add(line)
            out.append(line)
    return "\n".join(f"- {x}" for x in out[:limit])


def path_between(a: str, b: str, max_depth: int = 3) -> str:
    """A 和 B 之间靠什么连起来（BFS 最短链）。"""
    if not (a and b and a != b):
        return ""
    adj: dict[str, list[str]] = {}
    for e in _load_edges():
        adj.setdefault(e["src"], []).append(e["dst"])
        adj.setdefault(e["dst"], []).append(e["src"])

    queue = deque([(a, [a])])
    seen = {a}
    while queue:
        cur, chain = queue.popleft()
        if len(chain) > max_depth:
            continue
        for nxt in adj.get(cur, []):
            if nxt == b:
                return " → ".join(chain + [b])
            if nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, chain + [nxt]))
    return ""


# ---------- 主动插话 ----------

IDLE_COUNT: dict[int, int] = {}      # 每个群攒了多少条没叫她的消息
CHIME_LAST: dict[int, float] = {}


def reset_chime(gid) -> None:
    """她刚说过话，重新开始数。"""
    if gid:
        IDLE_COUNT[gid] = 0


async def maybe_chime_in(ws, evt: dict) -> None:
    """群聊里没人叫她时计数，攒够 N 条就主动插一句。"""
    c = cfg().get("chime") or {}
    if not c.get("enable", True):
        return
    gid = evt.get("group_id")
    if not gid:
        return

    every = max(2, int(c.get("every", 10)))
    n = IDLE_COUNT.get(gid, 0) + 1
    if n < every:
        IDLE_COUNT[gid] = n
        return
    IDLE_COUNT[gid] = 0

    now = time.monotonic()
    gap = float(c.get("cooldown", 180))
    if now - CHIME_LAST.get(gid, 0) < gap:
        log.info("群里攒够 %d 条了，但上次插话还没过冷却", every)
        return
    CHIME_LAST[gid] = now

    bg = recent_context(gid, limit=12)
    if not bg:
        return

    log.info("群 %s 攒够 %d 条闲聊，主动插一句", gid, every)
    extra = (
        "【群里刚才在聊，这些都是别人说的，不是你自己的经历】\n"
        f"{bg}\n"
        "【现在没人叫你。你要突然冒个泡，顺着话题插一句。"
        "要求：不点名、不 @ 任何人、一两句话就行，"
        "像群里随口接一嘴；别总结、别复述别人说过的，"
        "也别问「你们在聊什么」这种废话。】")
    try:
        answer = await ask(f"g{gid}", "（没人叫你，但你一直在看着群聊，忍不住插一句）",
                           {"ws": ws, "evt": evt, "group_id": gid}, extra=extra)
    except Exception as exc:
        log.warning("主动插话失败：%s", exc)
        return
    log.info("[chime %s] A=%r", gid, answer[:40])
    # 不引用任何一条消息——她不是回复谁，是自己开口
    await reply_with_media(ws, evt, answer, quote=False)


# ---------- 日报 ----------

ACTIVE_WS = None          # NapCat 当前连接，定时任务靠它发消息
LAST_REPORT: dict = {}    # {"date": "2026-10-22", "text": "..."}，当天只生成一次

REPORT_PROMPT = """你在给一个 QQ 群写今天的早报。

今天是 {today}，下面是从网上搜到的昨天（{yesterday}）的新闻标题和摘要。

【AI 相关】
{ai}

【其他要闻】
{news}

整理成一份简短早报，输出格式：

{md} 早报

AI 圈
· 谁做了什么，一句话一条，最多 3 条
· ...

其他
· 最多 2 条
· ...

要求：
- 每条不超过 45 字，说清楚「谁做了什么、有什么影响」
- 只用上面材料里的事实。材料里没有的不要补，宁可少写一条
- 同一件事在多份材料里出现就合并成一条
- 不要写「以下是」「据 XX 报道」这类套话
- 不用 Markdown 的 # 和 **，纯文本
- 如果某一栏的材料全是没营养的，就把那一栏整个省略
"""


async def send_group_text(ws, gid: int, text: str) -> bool:
    """定时任务没有原始消息可依附，直接往群里甩。"""
    if ws is None:
        log.warning("没有活动连接，发不了群消息")
        return False
    try:
        await ws.send(json.dumps({
            "action": "send_group_msg",
            "params": {"group_id": gid, "message": text},
            "echo": "daily"}, ensure_ascii=False))
        return True
    except Exception as exc:
        log.warning("发日报失败：%s", exc)
        return False


async def _search_many(queries: list[str], per: int = 4) -> list[dict]:
    out: list[dict] = []
    for q in queries:
        try:
            out += await deepseek_search(q, limit=per)
        except Exception as exc:
            log.debug("搜「%s」失败：%s", q, exc)
    return out


def _fmt_results(rows: list[dict], limit: int = 8) -> str:
    lines, seen = [], set()
    for r in rows:
        title = str(r.get("title") or "").strip()
        snip = str(r.get("snippet") or r.get("summary") or "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        lines.append(f"- {title}：{snip[:160]}" if snip else f"- {title}")
        if len(lines) >= limit:
            break
    return "\n".join(lines) or "（没搜到）"


async def build_daily_report() -> str:
    """搜昨天的新闻，交给模型整理成日报。"""
    now = datetime.now()
    y = now - timedelta(days=1)
    ymd, md = y.strftime("%Y年%m月%d日"), f"{y.month}月{y.day}日"

    ai_rows, news_rows = await asyncio.gather(
        _search_many([f"{ymd} AI 大模型 重要新闻", f"{ymd} 人工智能 行业 发布"]),
        _search_many([f"{ymd} 国内 国际 重要新闻", f"{ymd} 科技 财经 要闻"]),
    )
    log.info("日报素材：AI %d 条，要闻 %d 条", len(ai_rows), len(news_rows))

    prompt = REPORT_PROMPT.format(
        today=now.strftime("%Y年%m月%d日"), yesterday=ymd, md=md,
        ai=_fmt_results(ai_rows), news=_fmt_results(news_rows))
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=cfg().get("model", "deepseek-flash"),
                messages=[{"role": "user", "content": prompt}],
                max_tokens=900,
                temperature=0.4,
                extra_body={"thinking": {"type": "disabled"}}), timeout=90)
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        log.warning("整理日报失败：%s", exc)
        return ""


async def daily_report(gid: int | None = None, ws=None,
                       force: bool = False) -> str:
    """生成并发日报。同一天重复请求走缓存，不重新搜。"""
    c = cfg().get("daily") or {}
    if not c.get("enable", True):
        return "日报功能关掉了。"
    gid = int(gid or c.get("group") or 0)
    if not gid:
        return "没配置日报发到哪个群，在 config.json 的 daily.group 填一下。"

    today = datetime.now().strftime("%Y-%m-%d")
    if not force and LAST_REPORT.get("date") == today and LAST_REPORT.get("text"):
        text = LAST_REPORT["text"]
        log.info("日报用今天的缓存")
    else:
        text = await build_daily_report()
        if not text:
            return "今天的素材没搜到，日报没生成出来。"
        LAST_REPORT.update(date=today, text=text)

    ok = await send_group_text(ws or ACTIVE_WS, gid, text)
    log.info("[daily %s] %s", gid, "已发送" if ok else "发送失败")
    return "" if ok else "发送失败，NapCat 可能没连上。"


async def daily_scheduler() -> None:
    """每天到点发一次。"""
    c = cfg().get("daily") or {}
    if not c.get("enable", True):
        return
    at = str(c.get("time") or "08:30")
    try:
        hh, mm = (int(x) for x in at.split(":")[:2])
    except (TypeError, ValueError):
        hh, mm = 8, 30
    while True:
        now = datetime.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        log.info("下次日报：%s（%.1f 小时后）",
                 target.strftime("%m-%d %H:%M"), wait / 3600)
        await asyncio.sleep(wait)
        try:
            await daily_report()
        except Exception as exc:
            log.warning("定时日报失败：%s", exc)
        await asyncio.sleep(60)   # 跨过这一分钟，避免重复触发


# ---------- 群里的命令 ----------

COMMANDS = [
    ("日报", "昨天的 AI 和科技新闻"),
    ("总结", "把刚才群里的聊天捋一遍"),
    ("查 某人", "我记得的关于他"),
    ("帮助", "就是这张表"),
]


def menu_text() -> str:
    lines = ["……叫我干嘛？现在会这些："]
    lines += [f"· {c} —— {d}" for c, d in COMMANDS]
    lines.append("不用记命令也行，直接跟我说人话。")
    return "\n".join(lines)


async def summarize_group(gid: int, ws, evt: dict) -> str:
    """把最近的群聊捋一遍。"""
    try:
        msgs = store.recent_msgs(gid, 30)
    except Exception as exc:
        log.warning("读缓冲失败：%s", exc)
        return "……脑子有点乱，等会儿再说。"
    if len(msgs) < 4:
        return "群里就这么几句话，捋什么？"
    lines = "\n".join(f"[{m['nick'] or m['uid']}] {m['text']}" for m in msgs)
    extra = (
        "【群里刚才聊的，都是别人说的，不是你自己的经历】\n"
        f"{lines}\n"
        "【用你自己的口气，三句话以内说清刚才在聊什么。"
        "别列清单、别复述原话、别说「以下是总结」】")
    try:
        return await ask(f"sum{gid}", "（有人让你说说刚才群里在聊什么）",
                         {"ws": ws, "evt": evt, "group_id": gid}, extra=extra)
    except Exception as exc:
        log.warning("总结失败：%s", exc)
        return "……捋不出来了，等下再说。"


async def who_is(name: str, gid: int) -> str:
    """我记得的关于某人的事。数据全来自 person / memory 两张表。"""
    name = (name or "").strip()
    if not name:
        return "查谁？你倒是说个名字啊。"
    try:
        idx = store.nick_index()
        info_all = store.person_info(list(idx.values()))
    except Exception as exc:
        log.warning("查档案失败：%s", exc)
        return "……档案翻不出来了。"

    uid = None
    for n, u in idx.items():
        if n == name or name in n or n in name:
            uid = u
            break
    if uid is None:
        return f"「{name}」……没印象，这人在群里说过话吗？"

    info = info_all.get(uid) or {}
    head = f"{info.get('nick') or uid}（{uid}）｜说过 {info.get('msg_count') or 0} 句"
    last = info.get("last_seen")
    if last:
        try:
            d = datetime.fromtimestamp(int(last))
            day = "今天" if d.date() == datetime.now().date() else f"{d.month}月{d.day}日"
            head += f"，最近 {day} {d:%H:%M}"
        except (ValueError, OSError):
            pass

    lines = [head]
    try:
        # memories_for 会连带返回群整体的梗，查个人时只要他自己的
        mems = [m for m in store.memories_for(gid, [uid], limit=12)
                if m["uid"] == uid][:6]
    except Exception:
        mems = []
    if mems:
        lines += [f"· {store.render_names(m['content'])}" for m in mems]
    else:
        lines.append("· 还没记住什么具体的事")
    return "\n".join(lines)


# ---------- 事件处理 ----------

async def handle(ws, evt: dict) -> None:
    if evt.get("post_type") != "message":
        return
    mid = evt.get("message_id")
    if mid in seen_ids:
        return
    seen_ids.append(mid)
    if evt.get("user_id") == evt.get("self_id"):
        return

    is_group = evt.get("message_type") == "group"

    # 先取背景（不含当前这条），再把当前这条记进缓冲
    bg = recent_context(evt.get("group_id")) if is_group else ""
    if is_group:
        if remember(evt):
            # 攒够条数了，后台提炼一次，不阻塞这条消息
            asyncio.create_task(mine_memories(evt.get("group_id")))

    trig = trigger_cfg()
    mode = trig.get("mode", "at")
    whitelist = {int(g) for g in (trig.get("groups") or []) if str(g).isdigit()}

    if is_group:
        if whitelist and evt.get("group_id") not in whitelist:
            return
    else:
        # 私聊白名单：留空 = 所有人都能撩；填了就只认名单里的号
        dm = {int(u) for u in (trig.get("private_whitelist") or [])
              if str(u).isdigit()}
        if dm and evt.get("user_id") not in dm:
            log.info("忽略陌生私聊：%s", evt.get("user_id"))
            return

    text = text_of(evt).strip()

    # 触发判定
    if not is_group:
        if mode == "prefix":
            stripped = strip_prefix(text)
            if stripped is None:
                return
            text = stripped
    else:
        if mode == "at":
            # @ 或者直接喊名字都算叫她
            if not at_me(evt) and not name_hit(text):
                await maybe_chime_in(ws, evt)
                return
        elif mode == "prefix":
            stripped = strip_prefix(text)
            if stripped is None:
                await maybe_chime_in(ws, evt)
                return
            text = stripped
        # mode == "all" 全部回复

    has_image = any(s.get("type") == "image" for s in segments(evt))
    # 只 @ 她、一个字都不写 —— 她在等你开口，先摆张菜单
    if not text and not has_image:
        if is_group and at_me(evt):
            await reply(ws, evt, menu_text())
        return

    key = session_key(evt)

    # 内置指令
    if text in ("/reset", "重置"):
        history.pop(key, None)
        await reply(ws, evt, "上下文已清空。")
        return
    if text in ("/status", "状态"):
        if is_group:
            return  # 配置细节不往群里发
        c, t = cfg(), trigger_cfg()
        await reply(ws, evt, "\n".join([
            f"模型 {c.get('model')}｜thinking {c.get('thinking')}",
            f"群 {t.get('groups') or '全部'}｜触发 {t.get('mode')}",
            f"私聊白名单 {t.get('private_whitelist') or '不限'}",
            f"本会话 {len(history.get(key, [])) // 2} 轮上下文",
        ]))
        return
    if text in ("/help", "帮助", "菜单", "/菜单"):
        await reply(ws, evt, menu_text())
        return
    if text.strip() in ("总结", "/总结", "捋一下", "捋一捋"):
        await reply(ws, evt, "……等会儿，我翻翻刚才的。")
        await reply(ws, evt, await summarize_group(evt.get("group_id") or 0, ws, evt))
        return
    cp = text.strip().replace("：", " ").split(maxsplit=1)
    if len(cp) == 2 and cp[0] in ("查", "查一下", "你记得"):
        await reply(ws, evt, await who_is(cp[1].strip(), evt.get("group_id") or 0))
        return
    if text.strip() in ("日报", "/日报", "早报", "/早报"):
        await reply(ws, evt, "……等着，我翻一下，别催。")
        err = await daily_report(gid=evt.get("group_id") or None, ws=ws)
        if err:
            await reply(ws, evt, err)
        return

    now = time.monotonic()
    if now - last_seen.get(key, 0) < float(cfg().get("rate_limit_seconds", 2)):
        return
    last_seen[key] = now
    if is_group:
        reset_chime(evt.get("group_id"))   # 她刚说过话，插话计时重新开始

    images = await collect_images(ws, evt) if has_image else []

    extra_parts = []
    if bg:
        extra_parts.append(
            "【群里刚才的聊天记录，都是别人说的，不是你自己的经历】\n"
            f"{bg}\n"
            "【以上只是背景，不用逐条回应】")
    lore, hit_names = lore_lookup(text, bg)
    if lore:
        extra_parts.append(f"【你认识的人】\n{lore}")
    if hit_names:
        rel = relations_of(hit_names)
        if rel:
            extra_parts.append(f"【这些人之间和跟你的关系】\n{rel}")
        if len(hit_names) >= 2:
            a, b = hit_names[0], hit_names[1]
            if a not in ORG_NODES and b not in ORG_NODES:
                chain = path_between(a, b)
                if chain:
                    extra_parts.append(
                        f"【这两个人怎么连上的】{chain}\n"
                        "（这只是连通路径，不代表两人真认识。中间隔了人就直接说"
                        "「他俩没直接关系，都是通过我这边串起来的」，别编造他们之间的来往）")
    mem = memory_block(evt, text, bg)
    if mem:
        extra_parts.append(mem)
    hits = slang_hits(text, bg)
    if hits:
        extra_parts.append(f"【群里的黑话】\n{hits}")
    # 本地词典都没命中、但句子明显在问具体事实时，硬性要求去查
    # 带图那次不给工具，塞这句只会让她说「我去查」然后查不了
    if (not lore and not hits and not images
            and (_ASK_RE.search(text) or looks_like_name(text))):
        extra_parts.append(
            "【注意】对方在问一个具体的东西（人或事），而你手头没有资料。"
            "如果不是在问你自己（你的外貌、口味、日常作息），就必须先调 web_search 查。"
            "只发一个名字或 ID 也一样要查。不许凭印象编，"
            "更不许回一段不相干的内容（比如聊吃的、报天气）来敷衍。")
    extra = "\n\n".join(extra_parts)

    prompt = text or "（对方只发了一张图，一个字都没写）"
    if images:
        extra = (extra + "\n\n" if extra else "") + (
            "【图就在这条消息里】直接说你看到了什么、什么反应，"
            "别用「这张图片显示」「图中是一位」这种识图腔，像个真人瞄了一眼随口吐个槽。"
            "图太糊或看不出重点就直说看不清，别硬编细节。")

    lock = locks.setdefault(key, asyncio.Lock())
    async with lock:
        try:
            answer = await ask(key, prompt, {
                "ws": ws, "evt": evt, "group_id": evt.get("group_id"),
            }, extra=extra, images=images)
            log.info("[%s] Q=%r A=%r", key, prompt[:40], answer[:40])
        except asyncio.TimeoutError:
            answer = "模型响应超时，稍后再试。"
        except Exception as exc:
            log.exception("调用失败")
            answer = f"出错了：{type(exc).__name__}"
        await reply_with_media(ws, evt, answer)


# ---------- 拍一拍 ----------

POKE_LINES = [
    "……别拍，困。",
    "拍什么拍，我又不会醒得快点 😑",
    "（翻了个身，把被子拉过头顶）",
    "嗯？……哦，是你啊。有事说事。",
    "再拍我要生气了……算了，懒得生气。",
    "……你手不累吗。",
    "撤回你的手，谢谢。",
]

_poke_cool: dict[str, float] = {}
POKE_GAP = 60.0


async def handle_notice(ws, evt: dict) -> None:
    """拍一拍：只在拍到机器人自己时才回一句。"""
    if not (trigger_cfg().get("poke_reply", True)):
        return
    ntype, sub = evt.get("notice_type"), evt.get("sub_type")
    if not (sub == "poke" or ntype == "poke"):
        return
    if str(evt.get("target_id", "")) != str(evt.get("self_id", "")):
        return            # 拍的是别人，不插嘴

    key = f"poke{evt.get('group_id') or evt.get('user_id')}"
    now = time.monotonic()
    if now - _poke_cool.get(key, 0) < POKE_GAP:
        return            # 被狂拍时别刷屏
    _poke_cool[key] = now

    target = dict(evt)
    target["message_type"] = "group" if evt.get("group_id") else "private"
    target["message_id"] = None       # notice 没有 message_id，不能引用
    log.info("被 %s 拍了一下", evt.get("user_id"))
    await send(ws, target, random.choice(POKE_LINES))


async def probe_login(ws) -> None:
    """连上后确认 NapCat 当前登录的是哪个号。"""
    await asyncio.sleep(0.5)
    try:
        await ws.send(json.dumps(
            {"action": "get_login_info", "params": {}, "echo": "probe"}
        ))
    except Exception:
        pass


async def handler(ws, *_args):
    global ACTIVE_WS
    ACTIVE_WS = ws
    log.info("NapCat 已连接 %s", getattr(ws, "remote_address", ""))
    asyncio.create_task(probe_login(ws))
    try:
        async for raw in ws:
            try:
                evt = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            eid = evt.get("echo")
            if eid and eid in pending:
                fut = pending[eid]
                if not fut.done():
                    fut.set_result(evt)
                continue
            if eid == "probe":
                if evt.get("status") == "ok":
                    d = evt.get("data") or {}
                    log.info("已登录账号：%s (%s)", d.get("nickname"), d.get("user_id"))
                else:
                    log.warning("探测登录信息失败：%s", evt.get("message") or evt)
                continue
            if evt.get("post_type") == "message":
                asyncio.create_task(handle(ws, evt))
            elif evt.get("post_type") == "notice":
                asyncio.create_task(handle_notice(ws, evt))
    except Exception as exc:
        log.warning("连接异常结束：%s", exc)
    finally:
        if ACTIVE_WS is ws:
            ACTIVE_WS = None
        log.info("NapCat 断开 %s", getattr(ws, "remote_address", ""))


async def main() -> None:
    if not API_KEY:
        log.error("缺少 DEEPSEEK_API_KEY，请在 qqbot/.env 里填写后重启。")
        raise SystemExit(1)
    log.info("模型=%s thinking=%s 触发=%s 群白名单=%s",
             cfg().get("model"), cfg().get("thinking"),
             trigger_cfg().get("mode"), trigger_cfg().get("groups") or "全部")
    if (cfg().get("memory") or {}).get("enable", True):
        try:
            store.connect()
            restore_recent()
        except Exception as exc:
            log.warning("记忆库初始化失败，这次不记：%s", exc)
    async with ws_serve(handler, HOST, PORT, ping_interval=30, ping_timeout=60):
        log.info("监听 ws://%s:%s  等待 NapCat 反连…", HOST, PORT)
        if (cfg().get("daily") or {}).get("enable", True):
            asyncio.create_task(daily_scheduler())
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("已停止")
