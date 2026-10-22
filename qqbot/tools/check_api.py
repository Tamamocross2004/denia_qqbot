"""DeepSeek API 连通性检查：确认 key 有效、模型可用、thinking 能关掉。

只发一次极短的请求（max_tokens=16），成本可忽略。
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://api.deepseek.com"
HERE = Path(__file__).resolve().parent.parent


def mask(k: str) -> str:
    return f"{k[:6]}...{k[-4:]}" if len(k) > 12 else "(short)"


def read_env_key() -> str:
    env = HERE / ".env"
    if not env.exists():
        return ""
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("DEEPSEEK_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def call(path: str, key: str, payload: dict | None = None, timeout: int = 30):
    url = BASE + path
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST" if payload else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def main() -> int:
    key = read_env_key()
    if not key:
        print("[!] .env 里没有 DEEPSEEK_API_KEY")
        return 1
    print(f"[*] 使用 .env 中的 key：{mask(key)}")

    try:
        models = call("/models", key)
        ids = [m.get("id") for m in models.get("data", [])]
        print(f"[+] key 有效，可用模型：{', '.join(ids)}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        print(f"[!] /models 失败 HTTP {e.code}: {body}")
        return 2
    except Exception as e:
        print(f"[!] 网络错误：{type(e).__name__} {e}")
        return 3

    model = "deepseek-v4-flash"
    try:
        resp = call("/chat/completions", key, {
            "model": model,
            "messages": [{"role": "user", "content": "回复两个字：收到"}],
            "max_tokens": 16,
            "thinking": {"type": "disabled"},
        })
        msg = resp["choices"][0]["message"]
        print(f"[+] {model} 调用成功：{msg.get('content')!r}")
        usage = resp.get("usage", {})
        print(f"    usage: prompt={usage.get('prompt_tokens')} completion={usage.get('completion_tokens')}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        print(f"[!] chat 调用失败 HTTP {e.code}: {body}")
        return 4
    except Exception as e:
        print(f"[!] chat 调用异常：{type(e).__name__} {e}")
        return 5

    print("\n[*] 全部通过，可以直接启动 bot.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
