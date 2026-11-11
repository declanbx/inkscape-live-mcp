"""Tests for the 2026-11-10 review fixes, run against a fake Inkscape (no window, no D-Bus).

    .venv/bin/python tests/test_bundlefix.py      (or: pytest tests/test_bundlefix.py)

FakeBus stands in for InkscapeBus: it keeps an SVG document plus the visual boxes Inkscape would report,
answers query-all, select-*, object-set-property (refusing values with commas, as Inkscape 1.4.4 does,
measured headless 2026-11-10), export-do (plain SVG or a PNG) and the bridge extension (request.json in,
response.json out). Each test names the review finding it covers.
"""
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

STATE_DIR = Path(tempfile.mkdtemp(prefix="inkmcp-test-"))
os.environ["INKSCAPE_MCP_STATE"] = str(STATE_DIR)       # never the owner's ~/.inkscape-mcp
os.environ["INKSCAPE_MCP_TAG"] = "bundlefixtest"          # never matches a live instance
os.environ["QUARANTINE_ROOT"] = str(STATE_DIR) + "-quarantine"  # pruned test renders stay in the temp folder

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lxml import etree  # noqa: E402

from inkscape_live_mcp import client as C  # noqa: E402
from inkscape_live_mcp.bus import InkscapeBus  # noqa: E402
from inkscape_live_mcp.svgtree import Box, parse_style, text_of  # noqa: E402

SVGNS = "http://www.w3.org/2000/svg"
DOC = "/org/inkscape/Inkscape/bundlefixtest/document/1"
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def make_svg(width="210mm", height="297mm", title="p = 0.012", href="a.png", fill="#4b80ad"):
    return f'''<svg xmlns="{SVGNS}" xmlns:xlink="http://www.w3.org/1999/xlink"
 xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd" sodipodi:docname="t.svg"
 width="{width}" height="{height}" id="root"><g id="layer1">
<rect id="panel" x="10" y="10" width="80" height="40" style="fill:{fill}"/>
<text id="title" x="12" y="70" style="font-size:6px">{title}</text>
<image id="fig" x="20" y="150" width="60" height="60" xlink:href="{href}"/>
</g></svg>'''


BOXES = {"root": (0, 0, 793.7, 1122.5), "layer1": (10, 10, 80, 200), "panel": (10, 10, 80, 40),
         "title": (12, 64, 30, 6), "fig": (20, 150, 60, 60)}


class FakePending:
    def __init__(self, done):
        self.done = done

    def wait(self, timeout):
        return self.done

    def close(self):
        pass


class FakeBus:
    def __init__(self):
        self.svg = make_svg()
        self.boxes = dict(BOXES)
        self.owner = ":1.1"
        self.generation = 1
        self.selected: list[str] = []
        self.settings: dict = {}
        self.last_stderr = ""
        self.ignore_props: set[str] = set()   # properties Inkscape silently drops
        self.double_query = False
        self.empty_query = False
        self.pending = None
        self.pending_name = None
        self._describe = {}

    # identity / lifecycle
    def running(self):
        return True

    def require(self):
        return None

    def document_paths(self):
        return [DOC]

    def reset(self):
        pass

    # document helpers
    def root(self):
        return etree.fromstring(self.svg.encode())

    def set_root(self, root):
        self.svg = etree.tostring(root, encoding="unicode")

    def el(self, root, i):
        return root.find(f".//*[@id='{i}']") if root.get("id") != i else root

    def query(self):
        if self.empty_query:
            return ""
        txt = "".join(f"{i},{b[0]},{b[1]},{b[2]},{b[3]}\n" for i, b in self.boxes.items())
        return txt * 2 if self.double_query else txt

    # native actions
    def run(self, actions, path=None):
        out, err = [], []
        for item in actions:
            name, value = (item, None) if isinstance(item, str) else (item[0], item[1] if len(item) > 1 else None)
            self.activate(name, value, out=out, err=err)
        self.last_stderr = "".join(err)
        return "".join(out)

    def activate(self, name, value=None, path=None, timeout=None, out=None, err=None):
        out = out if out is not None else []
        err = err if err is not None else []
        if name == "query-all":
            out.append(self.query())
        elif name == "select-clear":
            self.selected = []
        elif name == "select-by-id":
            self.selected += str(value).split(",")
        elif name == "select-list":
            out.append("".join(f"{i} cloned: false\n" for i in self.selected))
        elif name == "object-set-property":
            parts = str(value).split(",")
            if len(parts) != 2:
                err.append("action:object_set_property: requires 'property name, property value'\n")
                return
            k, v = parts
            if k in self.ignore_props:
                return
            r = self.root()
            for i in self.selected:
                e = self.el(r, i)
                st = parse_style(e.get("style"))
                st[k] = v
                e.set("style", ";".join(f"{a}:{b}" for a, b in st.items()))
            self.set_root(r)
        elif name == "transform-translate":
            dx, dy = (float(v) for v in str(value).split(","))
            for i in self.selected:
                x, y, w, h = self.boxes[i]
                self.boxes[i] = (x + dx, y + dy, w, h)
        elif name.startswith("export-") and name != "export-do":
            self.settings[name] = value
        elif name == "export-do":
            fn = Path(self.settings["export-filename"])
            if self.settings.get("export-type") == "svg":
                fn.write_text(self.svg)
            else:
                fn.write_bytes(PNG)

    # the bridge extension
    def activate_tracked(self, name, value=None, path=None, timeout=None):
        br = STATE_DIR / "bridge"
        req = json.loads((br / "request.json").read_text())
        r = self.root()
        results, changed = [], False
        for op in req["ops"]:
            if op["op"] == "ping":
                results.append({"docname": "t.svg", "file": None, "px_per_user_unit": 1.0,
                                "page_px": list(C.page_size_px(r))})
            elif op["op"] == "set_style":
                for i in op["ids"]:
                    e = self.el(r, i)
                    st = parse_style(e.get("style"))
                    for k, v in op["props"].items():
                        if v is None:
                            st.pop(k, None)
                        else:
                            st[k] = str(v)
                    e.set("style", ";".join(f"{a}:{b}" for a, b in st.items()))
                changed = True
                results.append(True)
            elif op["op"] == "set_text":
                self.el(r, op["id"]).text = op["text"]
                changed = True
                results.append(True)
        self.set_root(r)
        resp = {"id": req["id"], "ok": True, "results": results, "changed": changed, "touched": [], "removed": []}
        if req.get("dump_to"):
            Path(req["dump_to"]).write_text(self.svg)
            resp["dumped"] = req["dump_to"]
        (br / "response.json").write_text(json.dumps(resp))


def fresh():
    ink = C.Inkscape()
    ink.bus = FakeBus()
    ink.window_titles = lambda: []
    return ink


def revision_of(ink):
    """The revision the server would report, read through changes() (exists in old and new code)."""
    head = ink.changes().split("\n", 1)[0]
    return head.split()[1].rstrip(".")


# ---- finding 1: style -------------------------------------------------------------------------------
def test_style_refused_value_is_not_landed_and_not_cached():
    ink = fresh()
    ink.tree()
    ink.bus.ignore_props = {"stroke-width"}          # Inkscape drops it without a word
    e = ink.style(["panel"], {"stroke-width": "0.5"})
    assert e.landed.startswith("NOT LANDED"), e.landed
    t = ink.trees.get(DOC)
    assert t is None or "stroke-width" not in parse_style(t.ids["panel"].get("style"))


def test_style_comma_values_land_and_are_read_back():
    ink = fresh()
    ink.tree()
    e = ink.style(["panel"], {"stroke-dasharray": "2,1", "font-family": "Helvetica Neue, Arial"})
    assert e.landed.startswith("Landed: confirmed"), e.landed
    st = parse_style(ink.bus.root().find(".//*[@id='panel']").get("style"))
    assert st.get("stroke-dasharray") == "2,1"


def test_style_rgb_value_confirmed_by_read_back():
    ink = fresh()
    ink.tree()
    e = ink.style(["panel"], {"fill": "rgb(75,128,173)"})
    assert e.landed.startswith("Landed: confirmed"), e.landed


# ---- finding 2: restart -----------------------------------------------------------------------------
def test_caches_dropped_when_inkscape_restarts():
    ink = fresh()
    ink.tree()
    a4 = ink.page_box()
    ink.bus.owner, ink.bus.generation = ":1.9", 2     # quit and reopened: /document/1 again
    ink.bus.svg = make_svg(width="100mm", height="50mm")
    b = ink.page_box()
    assert abs(b.w - a4.w) > 1, (a4, b)


# ---- finding 3: live text and style reads -----------------------------------------------------------
def test_tree_refreshes_when_owner_retypes_text():
    ink = fresh()
    t, *_ = ink.tree()
    assert text_of(t.ids["title"]) == "p = 0.012"
    ink.bus.svg = make_svg(title="p = 0.072")         # same box
    t, *_ = ink.tree()
    assert text_of(t.ids["title"]) == "p = 0.072"


# ---- finding 4: revision changes on same-box edits --------------------------------------------------
def test_revision_changes_on_same_box_content_edits():
    for change in (dict(title="p = 0.072"), dict(href="b.png"), dict(fill="#ff0000")):
        ink = fresh()
        r0 = revision_of(ink)
        ink.bus.svg = make_svg(**change)
        assert revision_of(ink) != r0, change


def test_guard_refuses_after_owner_retypes_text():
    ink = fresh()
    r0 = revision_of(ink)
    ink.bus.svg = make_svg(title="p = 0.072")
    try:
        ink.move(["panel"], dx_mm=1, expected_revision=r0)
    except C.RevisionConflict:
        return
    raise AssertionError("the edit passed the guard")


# ---- finding 5: one lock per tool call --------------------------------------------------------------
def test_revisioned_holds_the_lock_for_the_whole_call():
    from inkscape_live_mcp import server
    seen = []

    @server.revisioned
    def probe():
        seen.append(server.ink.lock.depth)
        return "ok"

    probe()
    assert seen == [1], seen


# ---- finding 7 (task item 6): timeouts, late landings, doubled reads --------------------------------
def test_native_wait_is_short():
    assert inspect.signature(InkscapeBus.activate).parameters["timeout"].default <= 60
    assert inspect.signature(InkscapeBus._call).parameters["timeout"].default <= 60


def test_doubled_query_output_gives_the_clean_revision():
    ink = fresh()
    clean, _ = ink.raw_boxes()
    ink.bus.double_query = True
    doubled, _ = ink.raw_boxes()
    assert doubled == clean, (len(doubled), len(clean))


def test_empty_query_output_raises():
    ink = fresh()
    ink.bus.empty_query = True
    try:
        ink.raw_boxes()
    except C.InkError:
        return
    raise AssertionError("an empty read was accepted")


def test_late_native_landing_is_reported():
    ink = fresh()
    ink.bus.pending, ink.bus.pending_name = FakePending(done=True), "transform-translate"
    ink.raw_boxes()
    notes = ink.take_notes()
    assert "transform-translate" in notes and "late" in notes, notes


def test_unfinished_native_call_blocks_the_next_one():
    ink = fresh()
    ink.bus.pending, ink.bus.pending_name = FakePending(done=False), "transform-translate"
    try:
        ink.raw_boxes()
    except C.UncertainCompletion:
        return
    raise AssertionError("sent another command while one was still running")


# ---- task item 7: renders pruned --------------------------------------------------------------------
def test_renders_are_pruned():
    ink = fresh()
    rd = STATE_DIR / "renders"
    for n in range(30):
        p = rd / f"render-{1000 + n}.png"
        p.write_bytes(PNG)
        os.utime(p, (1000 + n, 1000 + n))
    ink.render(Box(0, 0, 10, 10), width_px=10)
    left = list(rd.glob("render-*.png"))
    assert len(left) <= 20, len(left)


# ---- task item 8: instructions ----------------------------------------------------------------------
def test_instructions_short_and_keep_guarantees():
    from inkscape_live_mcp.server import INSTRUCTIONS
    assert len(INSTRUCTIONS.split()) <= 130, len(INSTRUCTIONS.split())
    for must in ("inkscape-live", "expected_revision", "NOT LANDED", "UNCERTAIN COMPLETION", "Cmd+Z",
                 "millimetres", "refused"):
        assert must in INSTRUCTIONS, must


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print("  PASS", fn.__name__)
        except Exception as ex:  # report every test, not just the first failure
            failed += 1
            print("  FAIL", fn.__name__, "-", type(ex).__name__, str(ex)[:160])
    print(f"{len(tests) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
