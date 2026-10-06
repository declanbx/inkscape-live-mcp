"""Unit tests that need no Inkscape: the revision fingerprint, the landed verdict, the box model.

    .venv/bin/python tests/test_units.py      (or: pytest tests/test_units.py)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inkscape_live_mcp.client import fingerprint, landed  # noqa: E402
from inkscape_live_mcp.svgtree import PX_PER_MM, Box, Tree, parse_query_all  # noqa: E402

QUERY = "svgroot,0,0,793.701,1122.52\nlayer1,0,0,1511.81,1133.86\nboxA,75.5906,75.5906,188.976,113.386\n"


def test_parse_query_all():
    order, boxes = parse_query_all(QUERY + "WARNING: something unrelated\n")
    assert order == ["svgroot", "layer1", "boxA"]
    assert abs(boxes["boxA"].w - 188.976) < 1e-9


def test_fingerprint_stable_and_sensitive():
    order, boxes = parse_query_all(QUERY)
    assert fingerprint(order, boxes) == fingerprint(*parse_query_all(QUERY))
    moved = dict(boxes, boxA=Box(76.5906, 75.5906, 188.976, 113.386))
    assert fingerprint(order, moved) != fingerprint(order, boxes)
    assert fingerprint(order[::-1], boxes) != fingerprint(order, boxes)  # paint order counts


def test_landed_exact():
    before = {"a": Box(0, 0, 10, 10)}
    good = {"a": Box(5, 0, 10, 10)}
    assert landed(before, good, exact={"a": Box(5, 0, 10, 10)}).startswith("Landed: confirmed")
    off = {"a": Box(5.2, 0, 10, 10)}  # 0.2 px ≈ 0.05 mm off
    assert landed(before, off, exact={"a": Box(5, 0, 10, 10)}).startswith("NOT LANDED")


def test_landed_added_removed_touched():
    before = {"a": Box(0, 0, 10, 10), "b": Box(20, 0, 10, 10)}
    after = {"a": Box(0, 0, 12, 10), "c": Box(40, 0, 5, 5)}
    assert landed(before, after, added=["c"]).startswith("Landed: confirmed")
    assert landed(before, after, added=["zzz"]).startswith("NOT LANDED")
    assert landed(before, after, removed=["b"]).startswith("Landed: confirmed")
    assert landed(before, {**after, "b": before["b"]}, removed=["b"]).startswith("NOT LANDED")
    assert landed(before, after, touched=["a"]).startswith("Landed: confirmed")
    assert landed(before, {"a": before["a"]}, touched=["a"]).startswith("Landed: unconfirmed")


def test_nested_svg_box_correction(tmp_path=None):
    svg = ROOT / "tests" / "fixtures" / "test_layout.svg"
    t = Tree.from_file(svg, "disk")
    assert "layer1" in t.nested_svg_ancestors
    s = PX_PER_MM
    boxes = {"layer1": Box(0, 0, 400 * s, 300 * s),       # what Inkscape 1.4 reports
             "boxA": Box(20 * s, 20 * s, 50 * s, 30 * s), "fig": Box(20 * s, 200 * s, 80 * s, 60 * s)}
    t.correct_boxes(boxes)
    assert abs(boxes["layer1"].w / s - 80) < 1e-6 and abs(boxes["layer1"].y2 / s - 260) < 1e-6


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in tests:
        fn()
        print("  PASS", fn.__name__)
    print(f"{len(tests)} passed")
