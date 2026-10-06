"""Time each kind of call, through the MCP server, on a large synthetic document.

    .venv/bin/python tests/bench.py                       # 50,000 objects + an 8 MB embedded image, A0
    .venv/bin/python tests/bench.py --objects 5000 --image-mb 0

Runs on a private Inkscape instance (tests/_instance.py) and prints a markdown table; the README's
cost table comes from this script.
"""
import argparse
import asyncio
import base64
import io
import os
import statistics
import sys
import time
from pathlib import Path

ap = argparse.ArgumentParser(description="Time MCP calls on a large synthetic document.")
ap.add_argument("--objects", type=int, default=50000)
ap.add_argument("--image-mb", type=float, default=8.0)
ap.add_argument("--keep", action="store_true")
ARGS = ap.parse_args()

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _instance  # noqa: E402  (first: it sets the private tag before the package is imported)
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from PIL import Image  # noqa: E402

DOC = _instance.TMP / "bench.svg"


def build(n: int, image_mb: float):
    """A0 page, 40 panels in a 5 x 8 grid, each a group of small rects with a title."""
    panels, per = 40, max(1, n // 40)
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
           'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" width="841mm" height="1189mm" '
           'viewBox="0 0 841 1189" id="root">', '<g id="layer1" inkscape:groupmode="layer" inkscape:label="Bench">']
    for p in range(panels):
        px_, py_ = 20 + (p % 5) * 162, 20 + (p // 5) * 145
        out.append(f'<g id="panel{p}" inkscape:label="Panel {p}" transform="translate({px_},{py_})">')
        out.append(f'<rect id="bg{p}" width="150" height="130" rx="3" fill="#eef1f6" stroke="#151b36" stroke-width="0.7"/>')
        out.append(f'<text id="title{p}" x="6" y="12" font-family="Helvetica" font-size="7" fill="#151b36">Panel {p} title</text>')
        cols = 60
        for k in range(per):
            x, y = 6 + (k % cols) * 2.3, 18 + (k // cols) * 2.3
            hue = (k * 37 + p * 11) % 360
            out.append(f'<rect id="p{p}r{k}" x="{x:.1f}" y="{y:.1f}" width="1.8" height="1.8" fill="hsl({hue},60%,45%)"/>')
        out.append("</g>")
    if image_mb > 0:
        side = int((image_mb * 1e6 / 3) ** 0.5)
        buf = io.BytesIO()
        Image.frombytes("RGB", (side, side), os.urandom(side * side * 3)).save(buf, format="PNG")
        out.append(f'<image id="photo" x="20" y="1180" width="60" height="6" preserveAspectRatio="none" '
                   f'xlink:href="data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"/>')
    out.append("</g></svg>")
    DOC.write_text("\n".join(out))
    return panels * (per + 2) + 2


async def main(ink):
    rows = []
    async with stdio_client(_instance.server_params()) as (r, w), ClientSession(r, w) as s:
        await s.initialize()

        async def timed(label, name, reps=1, **args):
            ts = []
            for _ in range(reps):
                t = time.perf_counter()
                res = await s.call_tool(name, args, read_timeout_seconds=900)
                ts.append(time.perf_counter() - t)
                if res.is_error:
                    print("ERROR", name, [c.text for c in res.content if getattr(c, "type", "") == "text"])
            rows.append((label, statistics.median(ts)))
            print(f"  {label}: {statistics.median(ts):.2f} s")
            return res

        res = await s.call_tool("status", {})
        _instance.check_identity(res.content[0].text, ink, "bench.svg")
        await timed("status", "status", reps=3)
        await timed("outline, first read (unedited file read from disk)", "outline", depth=1, max_lines=60)
        await timed("find / outline (tree cached)", "find", reps=3, text="Panel 7 title")
        await timed("render the whole page (1400 px)", "render", reps=3, width_px=1400)
        await timed("render one panel (around=…)", "render", reps=3, around=["panel3"], width_px=900)
        await timed("align two panels", "align", reps=3, ids=["panel1", "panel6"], edges="left", to="panel1")
        await timed("move a panel", "move", reps=3, ids=["panel2"], dx_mm=0.5)
        await timed("distribute five panels (exact gap)", "distribute", ids=[f"panel{i}" for i in range(5)],
                    axis="x", gap_mm=12, start_mm=20)
        await timed("style (fill)", "style", reps=3, ids=["bg4"], props={"fill": "#dce6f0"})
        await timed("changes (geometry diff)", "changes", reps=3)
        await timed("bridge edit: text", "text", edits=[{"id": "title0", "text": "Edited title"}])
        await timed("bridge edit: add_svg", "add_svg", markup='<rect id="added" width="20" height="5" fill="#542788"/>',
                    x_mm=30, y_mm=1100, show=False)
        await timed("bridge batch that changes nothing", "text", edits=[{"id": "title0", "text": "Edited title"}])
        await timed("undo of a bridge edit", "history", op="undo")
        await timed("outline, first read of an edited document (via the bridge)", "outline", depth=1, max_lines=60, refresh=True)
    return rows


if __name__ == "__main__":
    n = build(ARGS.objects, ARGS.image_mb)
    size_mb = DOC.stat().st_size / 1e6
    print(f"Document: {n:,} objects, {size_mb:.1f} MB")
    ink = _instance.launch(DOC, wait=300)
    try:
        rows = asyncio.run(main(ink))
    finally:
        _instance.close(ink, keep=ARGS.keep)
    print(f"\n| Operation ({n:,} objects, {size_mb:.0f} MB file) | Time |\n|---|---|")
    for label, t in rows:
        print(f"| {label} | {t:.2f} s |")
