"""批量生成测试语音，并对比生成音频与参考音频的基频分布。

音色克隆成功与否，看两点：
1. F0（基频）分布是否落在参考音频的区间 —— 也就是音高像不像
2. 语速是否自然

这里不评判"好不好听"，只给可量化的依据；最终还是要人耳确认。
"""
import json
import sys
import urllib.request
import wave
from pathlib import Path

import librosa
import numpy as np

API = "http://127.0.0.1:9880/tts"
REF = r"D:/code/deepseek/tts/ref/ref_010.wav"
REF_TEXT = "每天都能睡个好觉，吃到美味的东西，还有就是……和朋友们在一起。"
OUT = Path(r"D:/code/deepseek/tts/out")

LINES = [
    ("01", "困死了……今天就不该熬夜，明天再说吧。"),
    ("02", "你又在群里刷屏，烦不烦啊，我懒得理你。"),
    ("03", "哦，这个啊，我好像在哪里见过……记不清了。"),
]


def synth(text: str, out: Path) -> float:
    payload = {
        "text": text,
        "text_lang": "all_zh",
        "ref_audio_path": REF,
        "prompt_lang": "all_zh",
        "prompt_text": REF_TEXT,
        "text_split_method": "cut5",
        "media_type": "wav",
        "streaming_mode": False,
        "speed_factor": 1.0,
        "temperature": 1.0,
        "top_k": 5,
        "top_p": 1.0,
        "repetition_penalty": 1.35,
    }
    req = urllib.request.Request(
        API, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        audio = r.read()
    out.write_bytes(audio)
    with wave.open(str(out)) as w:
        dur = w.getnframes() / w.getframerate()
    return dur


def f0_stats(path: str) -> dict:
    y, sr = librosa.load(path, sr=16000, mono=True)
    f0, voiced, _ = librosa.pyin(y, fmin=60, fmax=600, sr=sr)
    v = f0[voiced]
    if v.size == 0:
        return {"n": 0}
    return {
        "n": int(v.size),
        "median": float(np.median(v)),
        "p10": float(np.percentile(v, 10)),
        "p90": float(np.percentile(v, 90)),
        "sec": float(y.size / sr),
    }


OUT.mkdir(parents=True, exist_ok=True)

print("=== 参考音频 ===")
ref = f0_stats(REF)
print(f"  {ref['sec']:.1f}s  F0 中位 {ref['median']:.0f} Hz  P10-P90 {ref['p10']:.0f}-{ref['p90']:.0f}")

print("\n=== 生成 ===")
rows = []
for tag, text in LINES:
    p = OUT / f"line{tag}.wav"
    try:
        dur = synth(text, p)
        st = f0_stats(str(p))
        rows.append((tag, text, dur, st))
        print(f"  [{tag}] {dur:5.2f}s  F0 中位 {st['median']:.0f} Hz  "
              f"P10-P90 {st['p10']:.0f}-{st['p90']:.0f}  {p.name}")
    except Exception as exc:
        print(f"  [{tag}] 失败: {type(exc).__name__}: {str(exc)[:150]}")

if rows:
    meds = [r[3]["median"] for r in rows]
    print(f"\n生成音频 F0 中位范围: {min(meds):.0f} - {max(meds):.0f} Hz")
    print(f"参考 F0 中位        : {ref['median']:.0f} Hz")
    delta = abs(sum(meds) / len(meds) - ref["median"])
    print(f"偏差                : {delta:.0f} Hz  "
          f"({'接近，音色大概率迁移成功' if delta < 45 else '偏大，可能需要调整'})")
