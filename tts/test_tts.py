"""测试本地 GPT-SoVITS 服务：生成一句语音并保存。"""
import json
import sys
import urllib.request
import wave
from pathlib import Path

API = "http://127.0.0.1:9880/tts"
REF = r"D:/code/deepseek/tts/ref/ref_010.wav"
REF_TEXT = "每天都能睡个好觉，吃到美味的东西，还有就是……和朋友们在一起。"
OUT = Path(r"D:/code/deepseek/tts/out")

text = sys.argv[1] if len(sys.argv) > 1 else "今天好困，不想动了，明天再说吧。"

payload = {
    "text": text,
    # 用 all_zh 而不是 zh：zh 会走多语言分段，纯中文也可能落到 en 分支，
    # 那样就会拉 g2p_en，多一层依赖和变数。all_zh 强制走中文单一路径。
    "text_lang": "all_zh",
    "ref_audio_path": REF,
    "prompt_lang": "all_zh",
    "prompt_text": REF_TEXT,
    "text_split_method": "cut5",
    "media_type": "wav",
    "streaming_mode": False,
}

OUT.mkdir(parents=True, exist_ok=True)
req = urllib.request.Request(
    API, data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=180) as r:
        audio = r.read()
except Exception as exc:
    print(f"[fail] 请求失败: {type(exc).__name__}: {str(exc)[:200]}")
    sys.exit(1)

path = OUT / "sample.wav"
path.write_bytes(audio)
with wave.open(str(path)) as w:
    dur = w.getnframes() / w.getframerate()
    print(f"[ok] {len(audio)/1024:.0f} KB, {dur:.2f} 秒, {w.getframerate()}Hz")
    print(f"     {path}")
print(f"[text] {text}")
