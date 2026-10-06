"""End-to-end test: launch a private Inkscape, drive it through the MCP server, check every result.

    .venv/bin/python tests/e2e.py           # its own instance: unique tag, temporary bus, state and profile
    .venv/bin/python tests/e2e.py --keep    # leave that instance open afterwards to look at it

It cannot touch any other Inkscape window: tests/_instance.py gives the instance a fresh tag, its own
D-Bus bus, state folder and throwaway Inkscape profile, hands the MCP server that tag explicitly, and
the first check aborts before any edit unless the server is driving that instance.
Geometry is asserted independently through a second connection (inkscape_live_mcp.client).
"""
import argparse
import asyncio
import base64
import re
import shutil
import sys
import time
from pathlib import Path

ap = argparse.ArgumentParser(description="End-to-end test against a private Inkscape instance.")
ap.add_argument("--keep", action="store_true", help="leave the test instance open afterwards")
ARGS = ap.parse_args()

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _instance  # noqa: E402  (first: it sets the private tag before the package is imported)
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from PIL import Image  # noqa: E402

from inkscape_live_mcp.bus import APP_PATH  # noqa: E402
from inkscape_live_mcp.client import Inkscape  # noqa: E402
from inkscape_live_mcp.svgtree import mm  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
OUT = Path(__file__).resolve().parent / "e2e_out"
OUT.mkdir(exist_ok=True)
TAG = _instance.TAG
DOC = _instance.TMP / "test_layout.svg"
shutil.copy(FIX / "test_layout.svg", DOC)
FAILS: list[str] = []
NPASS = 0


def check(name, cond, detail=""):
    global NPASS
    print(("  PASS " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if cond:
        NPASS += 1
    else:
        FAILS.append(name)


def rev(text: str) -> str | None:
    found = re.findall(r"revision ([0-9a-f]{12})", text)
    return found[-1] if found else None


async def main(ink: Inkscape):
    async with stdio_client(_instance.server_params()) as (r, w), ClientSession(r, w) as s:
        await s.initialize()

        async def call(name, **args):
            t = time.time()
            res = await s.call_tool(name, args, read_timeout_seconds=900)
            dt = time.time() - t
            texts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
            imgs = [c for c in res.content if getattr(c, "type", "") == "image"]
            for k, im in enumerate(imgs):
                (OUT / f"{name}_{int(t)}_{k}.png").write_bytes(base64.b64decode(im.data))
            print(f"\n## {name}({', '.join(f'{k}={v!r}' for k, v in args.items())[:150]})  {dt:.2f}s  error={res.is_error}")
            print("\n".join(texts)[:1500])
            return res, "\n".join(texts)

        def box(i):
            _, b = ink.boxes()
            return b[i]

        # ---------------------------------------------------------------- identity, before any edit
        _, out = await call("status")
        _instance.check_identity(out, ink, "test_layout.svg")
        r0 = rev(out)
        _, out = await call("status")
        check("revision is stable across reads", r0 is not None and rev(out) == r0, f"{r0} / {rev(out)}")

        await call("outline", depth=3)
        _, out = await call("find", text="original")
        check("find by text", "t1" in out)
        await call("render", annotate="top", width_px=900)

        # ---------------------------------------------------------------- layout, each verified live
        _, out = await call("align", ids=["boxB", "boxC"], edges="top", to="boxA")
        a, b, c = box("boxA"), box("boxB"), box("boxC")
        check("align top to anchor", abs(b.y - a.y) < 0.01 and abs(c.y - a.y) < 0.01, f"{mm(a.y):.3f} {mm(b.y):.3f} {mm(c.y):.3f}")
        check("align reports landed", "Landed: confirmed" in out)
        check("an edit returns a new revision", rev(out) not in (None, r0), f"{r0} → {rev(out)}")
        _, out = await call("distribute", ids=["boxA", "boxB", "boxC"], axis="x", gap_mm=10, start_mm=15)
        a, b, c = box("boxA"), box("boxB"), box("boxC")
        check("distribute exact gaps", abs(mm(b.x - a.x2) - 10) < 0.01 and abs(mm(c.x - b.x2) - 10) < 0.01 and abs(mm(a.x) - 15) < 0.01,
              f"gaps {mm(b.x - a.x2):.3f}, {mm(c.x - b.x2):.3f}; start {mm(a.x):.3f}")
        check("distribute reports landed", "Landed: confirmed" in out)
        _, out = await call("distribute", ids=["boxA", "boxB", "boxC"], axis="y", mode="centers")
        check("native equal distribution verified", "Landed: confirmed" in out)
        _, out = await call("move", ids=["grp1"], x_mm=105, y_mm=140, anchor="center")
        g = box("grp1")
        check("place by centre", abs(mm(g.x + g.w / 2) - 105) < 0.01 and abs(mm(g.y + g.h / 2) - 140) < 0.01)
        check("move reports landed", "Landed: confirmed" in out)
        b = box("boxB")
        _, out = await call("resize", ids=["boxB"], width_mm=45, anchor="top-left")
        b2 = box("boxB")
        check("resize exact width (stroked)", abs(mm(b2.w) - 45) < 0.03 and abs(b2.x - b.x) < 0.05, f"w {mm(b2.w):.3f}")
        check("resize reports landed", "Landed: confirmed" in out)
        _, out = await call("resize", ids=["boxC"], width_mm=30, height_mm=12, keep_aspect=False)
        c2 = box("boxC")
        check("stretch non-uniform", abs(mm(c2.w) - 30) < 0.05 and abs(mm(c2.h) - 12) < 0.05, f"{mm(c2.w):.3f}x{mm(c2.h):.3f}")
        _, out = await call("style", ids=["boxC", "dot"], props={"fill": "#e6194b", "opacity": "0.9"})
        check("colour edit says it is not checkable by geometry", "Landed: unconfirmed" in out)
        await call("attributes", ids=["boxA"], attrs={"inkscape:label": "Renamed box", "rx": "4"})
        _, out = await call("inspect", ids=["boxA", "dot"])
        check("inspect sees attrs", "Renamed box" in out and "rx" in out)

        # ---------------------------------------------------------------- text: single, replace, multi-line
        _, out = await call("text", edits=[{"id": "t1", "text": "New card title"}],
                            replace=[{"old": "two-line", "new": "TWO-LINE"}])
        check("text edit reports landed", "Landed: confirmed" in out)
        _, out = await call("find", text="new card title")
        check("text edit", "t1" in out)
        _, out = await call("inspect", ids=["para"])
        t, _, _, _ = ink.tree(refresh=True)
        check("replace keeps bold run", "TWO-LINE" in out and "boldbit" in t.ids and t.ids["boldbit"].get("font-weight") == "bold")
        await call("text", edits=[{"id": "para", "text": "Line one\nLine two\nLine three"}])
        _, out = await call("inspect", ids=["para"])
        check("multi-line text", "Line three" in out)

        # ---------------------------------------------------------------- a batch that changes nothing adds no undo step
        x0 = box("boxA").x
        await call("move", ids=["boxA"], dx_mm=5)
        _, out = await call("text", edits=[{"id": "t1", "text": "New card title"}])  # already says that
        check("no-op batch reports no change", "No change" in out and "0 undo steps" in out)
        await call("history", op="undo", n=1)
        check("no-op batch added no undo step (undo reverted the move before it)", abs(box("boxA").x - x0) < 0.05,
              f"boxA x {mm(box('boxA').x):.3f} mm, expected {mm(x0):.3f}")

        # ---------------------------------------------------------------- figures: synthetic SVG legend and PNG
        _, out = await call("insert", path=str(FIX / "legend.svg"), x_mm=110, y_mm=200, width_mm=70, id="legend")
        check("insert reports landed", "Landed: confirmed" in out)
        ink.tree(refresh=True)
        gb = box("legend")
        check("insert svg at position + corrected width", abs(mm(gb.x) - 110) < 1.0 and abs(mm(gb.y) - 200) < 1.0
              and abs(mm(gb.w) - 70) < 0.5, gb.mm())
        raw = ink.raw_boxes()[1]["legend"]
        print(f"  info: Inkscape's own box for the inserted group is {mm(raw.w):.1f} mm wide "
              f"({'still wrong: the nested-<svg> correction is needed' if abs(mm(raw.w) - 70) > 1 else 'now right'})")
        lay = box("layer1")
        check("layer box rebuilt from children (not 400x300)", mm(lay.w) < 300, lay.mm())
        _, out = await call("align", ids=["legend", "fig"], edges="bottom", to="fig")
        check("align a nested-svg group exactly", abs(box("legend").y2 - box("fig").y2) < 0.05,
              f"{mm(box('legend').y2):.3f} vs {mm(box('fig').y2):.3f}")
        png = OUT / "swatch.png"
        img = Image.new("RGB", (300, 120))
        img.putdata([(int(255 * x / 299), 80, int(255 * (1 - y / 119))) for y in range(120) for x in range(300)])
        img.save(png)
        await call("insert", path=str(png), x_mm=150, y_mm=250, height_mm=25, id="logo", show=False)
        lb = box("logo")
        check("insert png exact height", abs(mm(lb.h) - 25) < 0.05, lb.mm())
        _, out = await call("add_svg", markup='<rect id="bar" width="40" height="6" rx="1.5" fill="#151B36"/>'
                                               '<text id="bartext" x="2" y="4.5" font-size="4" font-family="Helvetica" fill="#fff">Added by Claude</text>',
                            x_mm=20, y_mm=275)
        bb = box("bar")
        check("add_svg in mm", abs(mm(bb.x) - 20) < 0.01 and abs(mm(bb.w) - 40) < 0.01, bb.mm())
        check("add_svg reports landed", "Landed: confirmed" in out)

        # ---------------------------------------------------------------- structure
        await call("structure", op="group", ids=["bar", "bartext"], label="Footer bar")
        _, out = await call("structure", op="duplicate", ids=["boxA"])
        dup = out.split("\n")[1].strip().split(" ")[0] if "\n" in out else None
        check("duplicate returns new id", bool(dup) and dup != "boxA", str(dup))
        if dup:
            await call("move", ids=[dup], dy_mm=40)
            _, out = await call("structure", op="delete", ids=[dup])
            check("delete", dup not in ink.boxes()[1])
            check("delete reports landed", "Landed: confirmed" in out)
        _, out = await call("structure", op="reparent", ids=["boxC"], parent="grp1")
        check("reparent keeps position (landed)", "Landed: confirmed" in out)
        _, out = await call("inspect", ids=["grp1"])
        check("reparent keeps it visible", "boxC" in out)

        # ---------------------------------------------------------------- revision guard against a concurrent user
        _, out = await call("status")
        r_seen = rev(out)
        ink.select(["boxA"])
        ink.run([("transform-translate", "37.795,0")])  # 10 mm, as the user dragging boxA would
        ink.select([])
        bB = box("boxB")
        res, out = await call("move", ids=["boxB"], dx_mm=3, expected_revision=r_seen)
        check("stale revision refuses the edit", res.is_error and "Refused" in out and "boxA" in out, out[:120])
        check("refused edit changed nothing", abs(box("boxB").x - bB.x) < 1e-6)
        res, out = await call("history", op="undo", expected_revision=r_seen)
        check("stale revision refuses an undo of the user's work", res.is_error and abs(mm(box("boxA").x) - 25) < 0.01,
              box("boxA").mm())
        _, out = await call("changes")
        check("changes sees the user's move", "boxA" in out and "+10.00" in out)
        r_now = rev(out)
        check("changes reports and refreshes the revision", r_now not in (None, r_seen) and f"(was {r_seen})" in out)
        _, out = await call("history", op="undo", n=1, expected_revision=r_now)
        check("undo restores (guarded by the fresh revision)", abs(mm(box("boxA").x) - 15) < 0.01, box("boxA").mm())
        res, out = await call("move", ids=["boxB"], dx_mm=3, expected_revision=rev(out))
        check("edit with the current revision goes ahead", not res.is_error and "Landed: confirmed" in out)

        # ---------------------------------------------------------------- uncertain completion
        res, out = await call("python", code="import time\ntime.sleep(5)\nsvg.set('data-late', 'yes')", timeout_s=1.5)
        check("timeout returns UNCERTAIN COMPLETION as a result, not an error",
              not res.is_error and "UNCERTAIN COMPLETION" in out)
        _, out = await call("python", code="result = svg.get('data-late')", readonly=True)
        check("next call waits for the late edit, which did apply", "result = 'yes'" in out, out[-60:])

        # ---------------------------------------------------------------- macOS quirks
        _, out = await call("python", code="import resource\nresult = resource.getrlimit(resource.RLIMIT_NOFILE)[0]",
                            readonly=True)
        m = re.search(r"result = (\d+)", out)
        soft = int(m.group(1)) if m else None
        check("extensions inherit a small open-file limit (Inkscape 1.4.2 crash, PR #33)",
              soft is not None and soft <= 4096, f"soft limit {soft}")
        (xml,) = ink.bus._call(f"org.inkscape.Inkscape.{TAG}", APP_PATH, "org.freedesktop.DBus.Introspectable",
                               "Introspect", timeout=10)
        kids = re.findall(r'<node name="([^"]+)"', xml)
        check("actions reach documents without /window/N objects", "document" in kids, f"children {kids}")
        print(f"  info: GTK exports /window/N objects here: {'window' in kids}")

        # ---------------------------------------------------------------- raw actions report what Inkscape did
        a0 = box("boxA")
        res, out = await call("actions", run=["select-clear", ["select-by-id", "boxA"], ["transform-translate", "abc"]])
        check("a rejected raw action is an error carrying Inkscape's reason",
              res.is_error and "requires two comma separated numbers" in out, out[:100])
        check("the rejected action moved nothing", abs(box("boxA").x - a0.x) < 1e-6 and abs(box("boxA").y - a0.y) < 1e-6)
        res, out = await call("actions", run=["select-clear", ["select-by-id", "boxA,boxB"], ["object-align", "sideways"]])
        check("a silently ignored action is reported as no geometric change", not res.is_error and "No geometric change" in out)
        res, out = await call("actions", run=["select-clear", ["select-by-id", "dot"], ["transform-translate", "3.7795,0"],
                                              "select-clear"])
        check("a raw action that moves something says so", not res.is_error and "Geometry changed" in out)
        await call("history", op="undo")

        # ---------------------------------------------------------------- export, read back after writing
        X = OUT / "exports"
        _, out = await call("export", path=str(X / "page.pdf"))
        check("PDF of the page: 210×297 mm with fonts, verified",
              "210.0×297.0 mm" in out and "fonts embedded" in out and "Landed: confirmed" in out)
        _, out = await call("export", path=str(X / "outlines.pdf"), text_to_path=True)
        check("text_to_path leaves no fonts in the PDF", "no fonts" in out and "Landed: confirmed" in out)
        _, out = await call("export", path=str(X / "page_again.pdf"))
        check("text_to_path does not leak into the next export", "fonts embedded" in out)
        lg = box("legend")
        _, out = await call("export", path=str(X / "legend.pdf"), ids=["legend"])
        check("PDF of a nested-svg group is sized from the corrected box",
              "Landed: confirmed" in out and f"{mm(lg.w):.1f}×{mm(lg.h):.1f} mm" in out, out[:200])
        _, out = await call("export", path=str(X / "drawing.pdf"), area="drawing")
        check("PDF of the drawing is sized from the corrected boxes", "Landed: confirmed" in out, out[:200])
        _, out = await call("export", path=str(X / "region.png"), region_mm=[20, 20, 120, 70], dpi=150)
        check("PNG of a 100 mm region at 150 dpi is 591 px wide, verified", "591×" in out and "Landed: confirmed" in out)
        ink.tree(refresh=True)  # this test's own cached tree may be stale; the corrected boxes depend on it
        d = ink.drawing_box(ink.boxes()[1])
        _, out = await call("export", path=str(X / "drawing.png"), area="drawing", dpi=96)
        check("PNG of the drawing uses the corrected drawing box", f"{round(d.w)}×" in out and "Landed: confirmed" in out,
              f"expected {round(d.w)} px wide; {out[:120]}")
        _, out = await call("export", path=str(X / "card.svg"), ids=["grp1"])
        check("SVG of one object, verified", "Landed: confirmed" in out, out[:200])
        _, out = await call("export", path=str(X / "region.pdf"), region_mm=[20, 20, 120, 70])
        check("PDF of a region is cropped to it (100×50 mm)", "100.0×50.0 mm" in out and "Landed: confirmed" in out, out[:200])
        _, out = await call("export", path=str(X / "card.png"), ids=["grp1"], dpi=200, background="#ffffff")
        check("PNG of one object on white, verified", "Landed: confirmed" in out, out[:200])
        _, out = await call("render", region_mm=[0, 0, 100, 50], width_px=400)
        check("renders are unaffected by earlier exports (400×200 px)", "400×200 px" in out, out[:120])

        # ---------------------------------------------------------------- escape hatches
        _, out = await call("python", code="result = len(svg.xpath('//svg:rect', namespaces={'svg':'http://www.w3.org/2000/svg'}))", readonly=True)
        check("python readonly", "result =" in out)
        await call("actions", search="flip")
        _, out = await call("actions", run=[["select-by-id", "dot"], "select-list"])
        check("raw actions", "dot" in out)
        _, out = await call("save_copy", path=str(OUT / "saved_copy.svg"))
        check("save_copy", (OUT / "saved_copy.svg").exists())
        await call("render", width_px=1000, annotate="top")
        await call("window")


if __name__ == "__main__":
    print(f"Test instance: tag {TAG}, state {_instance.STATE}")
    ink = _instance.launch(DOC)
    print(f"Launched pid {ink.instance_pid()} on {DOC}")
    try:
        asyncio.run(main(ink))
    finally:
        _instance.close(ink, keep=ARGS.keep)
    print(f"\n{NPASS} passed, {len(FAILS)} failed" + (": " + ", ".join(FAILS) if FAILS else " — ALL CHECKS PASSED"))
    sys.exit(1 if FAILS else 0)
