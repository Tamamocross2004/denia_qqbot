"""把 relations.json 画成关系图，生成自包含 HTML（不依赖任何 CDN）。

用法：
    python tools/graph.py            # 生成 qqbot/relations.html
    python tools/graph.py --open     # 生成并直接打开
"""

import argparse
import json
import math
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
REL_PATH = HERE / "relations.json"
OUT_PATH = HERE / "relations.html"

SIZE = 900
CENTER = SIZE / 2
RADIUS = 300
HUB = "达妮娅"          # 中心人物，单独高亮


def esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_svg(edges: list[dict]) -> tuple[str, int]:
    nodes: list[str] = []
    for e in edges:
        for n in (e["src"], e["dst"]):
            if n not in nodes:
                nodes.append(n)

    # 中心人物放正中，其余绕圈均分
    others = [n for n in nodes if n != HUB]
    pos: dict[str, tuple[float, float]] = {}
    if HUB in nodes:
        pos[HUB] = (CENTER, CENTER)
    for i, name in enumerate(others):
        step = 2 * math.pi / max(1, len(others))
        ang = -math.pi / 2 + i * step
        r = RADIUS if HUB in nodes else RADIUS * 0.8
        pos[name] = (CENTER + r * math.cos(ang), CENTER + r * math.sin(ang))

    parts = [
        f'<svg viewBox="0 0 {SIZE} {SIZE}" xmlns="http://www.w3.org/2000/svg">',
        "<defs>",
        '<marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        'markerHeight="7" orient="auto-start-reverse">',
        '<path d="M1 1L9 5L1 9" fill="none" stroke="#888780" stroke-width="1.4" '
        'stroke-linecap="round"/></marker>',
        "</defs>",
        f'<rect width="{SIZE}" height="{SIZE}" fill="#FAFAF7"/>',
    ]

    # 先画边，节点压在上面
    for e in edges:
        x1, y1 = pos[e["src"]]
        x2, y2 = pos[e["dst"]]
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        parts.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="#B4B2A9" stroke-width="1.3" marker-end="url(#a)"/>')
        label = esc(e.get("rel") or "")
        if label:
            w = len(label) * 13 + 10
            parts.append(
                f'<rect x="{mx - w / 2:.1f}" y="{my - 11:.1f}" width="{w}" height="22" '
                f'rx="6" fill="#FAFAF7" opacity="0.92"/>')
            parts.append(
                f'<text x="{mx:.1f}" y="{my + 1:.1f}" text-anchor="middle" '
                f'dominant-baseline="central" font-size="12" fill="#5F5E5A" '
                f'font-family="system-ui,sans-serif">{label}</text>')

    for name in nodes:
        x, y = pos[name]
        is_hub = name == HUB
        fill = "#E1F5EE" if is_hub else "#E6F1FB"
        stroke = "#0F6E56" if is_hub else "#185FA5"
        text = "#085041" if is_hub else "#0C447C"
        r = 34 if is_hub else 28
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="1.2"/>')
        parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="middle" '
            f'dominant-baseline="central" font-size="13" font-weight="500" '
            f'fill="{text}" font-family="system-ui,sans-serif">{esc(name)}</text>')

    parts.append("</svg>")
    return "\n".join(parts), len(nodes)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true", help="生成后直接打开")
    args = ap.parse_args()

    try:
        data = json.loads(REL_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[!] 读不到 relations.json：{exc}")
        return 1

    edges = [e for e in (data.get("edges") if isinstance(data, dict) else data)
             if isinstance(e, dict) and e.get("src") and e.get("dst")]
    if not edges:
        print("[!] relations.json 里没有可用的边")
        return 1

    svg, n = build_svg(edges)
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>角色关系图</title>
<style>
  body {{ margin:0; background:#F5F4F0; font-family:system-ui,sans-serif;
         display:flex; flex-direction:column; align-items:center; }}
  h1 {{ font-size:16px; font-weight:500; color:#2C2C2A; margin:20px 0 4px; }}
  p  {{ font-size:12px; color:#888780; margin:0 0 16px; }}
  .wrap {{ width:min(900px,96vw); }}
  svg {{ width:100%; height:auto; display:block; border-radius:12px;
         border:1px solid #D3D1C7; }}
</style></head>
<body>
  <h1>达妮娅 · 角色关系图</h1>
  <p>{n} 个节点 / {len(edges)} 条关系 —— 箭头方向即「谁对谁」，改 relations.json 后重新运行本脚本</p>
  <div class="wrap">{svg}</div>
</body></html>"""

    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"[+] {OUT_PATH}  ({n} 节点 / {len(edges)} 边)")
    if args.open:
        webbrowser.open(OUT_PATH.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
