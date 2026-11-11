"""Live control of a running Inkscape: geometry, structure, layout, edits, renders.

Two routes into the document, chosen per operation by cost:
  * NATIVE  — Inkscape's own actions over D-Bus (select, align, distribute, translate, scale, style,
              attribute, z-order, group, delete...). 0.1–1 s each, applied in the open window, one
              undo step each.
  * BRIDGE  — the claude_bridge effect extension, for what no action can do (text content, inserting
              figures or markup, reparenting, arbitrary Python). One undo step per batch, but
              Inkscape serialises and reloads the whole document, so it costs ~0.5 s on a figure, ~3 s
              on 50k objects and up to ~16 s on a large poster with clipped figures: batch edits.
All coordinates crossing this API are millimetres from the page's top-left corner; internally
everything is document px (96/in), the frame of query-all and transform-translate.

Two safety nets wrap every edit:
  * REVISION — a fingerprint of the live document: every object's id and visual box, in document order
    (query-all), plus a hash of the document's content as Inkscape itself serialises it (a plain-SVG
    export: text, colours, images, transforms). Each tool call records it; each edit takes an optional
    expected_revision and refuses, changing nothing, if the person has changed anything they could see
    since. Documents over CONTENT_MAX objects fall back to geometry only (the export costs ~40 µs per
    object), and every result then says so.
  * LANDED — after an edit the touched objects' live boxes are compared with what the edit planned
    (exact positions, shared edges, equal gaps, new ids present, deleted ids gone), so a call reports
    "confirmed", "NOT LANDED" or "unconfirmed" (an edit with no geometric trace, e.g. a colour).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
import zlib
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from .bus import STATE, TAG, CallTimeout, InkscapeBus, PendingCall, UnknownAction
from .svgtree import NS, PX_PER_MM, SKIP, Box, Tree, local, mm, page_size_px, parse_query_all, parse_style, qname

LAUNCH = Path(__file__).resolve().parent.parent / "launch.sh"
INKSCAPE_BIN = os.environ.get("INKSCAPE_BIN", "/Applications/Inkscape.app/Contents/MacOS/inkscape")
BRIDGE_ACTION = "org.inkscape.claude.bridge"
EDGES_H = ("left", "hcenter", "right")
EDGES_V = ("top", "vcenter", "bottom")
NATIVE_ALIGN_TARGETS = ("first", "last", "biggest", "smallest", "page", "drawing", "selection")
TOL = 0.01 * PX_PER_MM          # "where planned" tolerance: 0.01 mm
SNAPSHOTS = 6                   # revisions kept in memory so a refusal can say what changed
EXPORT_FORMATS = ("png", "pdf", "svg", "eps", "ps")
# Content tracking exports the live document as plain SVG once per tool call (measured headless 2026-11-10:
# ~2 s at 50,000 objects, ~0.2 s at 5,000, tens of ms for a figure). Above this many objects the revision
# covers geometry only, as it did before, and says so.
CONTENT_MAX = int(os.environ.get("INKSCAPE_MCP_CONTENT_MAX", 15000))
# Export settings for that hash. Plain SVG drops the namedview (zoom, scroll, window), so looking around
# the canvas never changes the revision; the output does not depend on the export area, width, dpi or
# background another call left set (measured headless 2026-11-10: byte-identical across all four).
CONTENT_EXPORT = (("export-type", "svg"), ("export-plain-svg", True), ("export-id", ""), ("export-id-only", False),
                  ("export-overwrite", True))
# Default-named renders and window shots kept in STATE/renders (each image is also returned inline). Older
# ones are moved out with the `quarantine` command (this machine's rule: code never deletes files), in
# batches of PRUNE_BATCH so a render does not start a process every time.
KEEP_RENDERS = 20
PRUNE_BATCH = 10
# What Inkscape prints to stderr when it refuses an action ("action:transform_translate: requires two
# comma separated numbers", "select_by_id: Did not find object with id: x"). Many refusals print nothing
# at all (a bare or misspelled object-align), which is why raw actions also report whether geometry changed.
STDERR_NOISE = re.compile(r"accelerator|Gtk-CRITICAL|Gtk-WARNING|^\s*$")
REJECTION = re.compile(r"requires|did not find|invalid|error|failed|unknown|not found|could not|cannot|can't|unable",
                       re.IGNORECASE)


class InkError(RuntimeError):
    pass


def last_query_block(text: str) -> str:
    """The last complete query-all listing in `text`. A command that timed out can print its own listing
    into the next call's capture window; the listing always starts with the root, so keep only the lines
    from the root's last appearance on (a doubled read otherwise gives a revision that matches nothing)."""
    lines = text.splitlines()
    ids = [(n, ln.rsplit(",", 4)[0]) for n, ln in enumerate(lines) if len(ln.rsplit(",", 4)) == 5]
    if not ids:
        return text
    root = ids[0][1]
    start = max(n for n, i in ids if i == root)
    return "\n".join(lines[start:])


def norm_css(v) -> str:
    """A style value in a comparable spelling: case, quotes and spacing dropped, rgb() and #abc as #aabbcc,
    numbers without trailing zeros. '2, 1' == '2,1'; 'rgb(75,128,173)' == '#4B80AD'."""
    s = str(v).strip().lower().replace('"', "").replace("'", "")
    s = re.sub(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)",
               lambda m: "#" + "".join(f"{int(g):02x}" for g in m.groups()), s)
    s = re.sub(r"#([0-9a-f])([0-9a-f])([0-9a-f])\b", r"#\1\1\2\2\3\3", s)
    out = []
    for t in (t for t in re.split(r"[\s,]+", s) if t):
        try:
            out.append(f"{float(t):g}")
        except ValueError:
            out.append(t)
    return ",".join(out)


def _stat_key(path: Path):
    try:
        st = path.stat()
        return st.st_mtime_ns, st.st_size
    except FileNotFoundError:
        return None


class RevisionConflict(InkError):
    """The document changed since the revision the caller based its edit on; nothing was edited."""


class UncertainCompletion(InkError):
    """Inkscape did not answer in time: the edit may or may not have been applied."""


@dataclass
class Edit:
    steps: int                      # undo steps added
    before: dict[str, Box]
    after: dict[str, Box]
    landed: str                     # one-line verdict
    revision: str                   # revision after the edit
    ids: list[str] = field(default_factory=list)  # objects the edit produced (structure ops)


def fingerprint(order: list[str], boxes: dict[str, Box]) -> str:
    h = hashlib.sha1()
    for i in order:
        b = boxes[i]
        h.update(f"{i},{b.x:.3f},{b.y:.3f},{b.w:.3f},{b.h:.3f}\n".encode())
    return h.hexdigest()[:12]


def _same(a: Box, b: Box, tol: float = TOL) -> bool:
    return max(abs(a.x - b.x), abs(a.y - b.y), abs(a.w - b.w), abs(a.h - b.h)) <= tol


def _shift(b: Box, d: tuple[float, float]) -> Box:
    return Box(b.x + d[0], b.y + d[1], b.w, b.h)


def inkscape_said(stderr: str) -> tuple[list[str], list[str]]:
    """Split what Inkscape printed to stderr into (rejections, other messages), dropping GTK noise."""
    lines = [ln.strip() for ln in stderr.splitlines() if not STDERR_NOISE.search(ln)]
    rejected = [ln for ln in lines if REJECTION.search(ln) and not ln.lower().startswith("warning")]
    return rejected, [ln for ln in lines if ln not in rejected]


def pdf_facts(path: Path) -> tuple[int, float, float, bool] | None:
    """(pages, width mm, height mm, fonts embedded) of a PDF, read with Quartz; None if unreadable."""
    try:
        import Quartz
    except ImportError:
        return None
    raw = str(path).encode()
    doc = Quartz.CGPDFDocumentCreateWithURL(Quartz.CFURLCreateFromFileSystemRepresentation(None, raw, len(raw), False))
    if doc is None:
        return None
    r = Quartz.CGPDFPageGetBoxRect(Quartz.CGPDFDocumentGetPage(doc, 1), Quartz.kCGPDFMediaBox)
    data = path.read_bytes()
    font = re.compile(rb"/Type\s*/Font\b|/FontFile")
    fonts = bool(font.search(data))
    for m in re.finditer(rb"stream\r?\n", data) if not fonts else ():
        end = data.find(b"endstream", m.end())
        try:
            if font.search(zlib.decompress(data[m.end():end])):
                fonts = True
                break
        except zlib.error:
            continue
    return Quartz.CGPDFDocumentGetNumberOfPages(doc), r.size.width * 25.4 / 72, r.size.height * 25.4 / 72, fonts


def landed(before: dict[str, Box], after: dict[str, Box], *, exact: dict[str, Box] | None = None,
           added=(), removed=(), touched=(), unchanged: str = "") -> str:
    """One-line verdict comparing what an edit planned with Inkscape's live boxes afterwards."""
    fails, goods = [], []
    if exact:
        bad = []
        for i, want in exact.items():
            have = after.get(i)
            if have is None:
                bad.append(f"{i} is gone")
            elif not _same(have, want):
                bad.append(f"{i} at {have.mm()}, planned {want.mm()}")
        if bad:
            fails.append("not where planned: " + "; ".join(bad[:5]))
        else:
            goods.append(f"{len(exact)} object(s) exactly where planned")
    added = [i for i in added if i]
    if added:
        present = [i for i in added if i in after]
        if not present:
            fails.append(f"none of the {len(added)} new object(s) appeared")
        else:
            goods.append(f"{len(present)} of {len(added)} new object(s) present")
    removed = [i for i in removed if i in before]
    if removed:
        still = [i for i in removed if i in after]
        if still:
            fails.append(f"{len(still)} removed object(s) still present: {still[:5]}")
        else:
            goods.append(f"{len(removed)} object(s) gone")
    touched = [i for i in touched if i in before]
    if touched:
        vanished = [i for i in touched if i not in after]
        moved = [i for i in touched if i in after and not _same(after[i], before[i], 1e-6)]
        if vanished:
            fails.append(f"{vanished[:5]} vanished")
        if moved:
            goods.append(f"{len(moved)} edited object(s) changed shape or position")
    if fails:
        return "NOT LANDED as planned: " + "; ".join(fails)
    if goods:
        return "Landed: confirmed (" + "; ".join(goods) + ")."
    return "Landed: unconfirmed — " + (unchanged or "nothing about the touched objects' geometry changed, "
                                       "and geometry is all Inkscape reports back.")


class _ProcessLock:
    """Re-entrant lock shared by every process driving this Inkscape instance.

    The bridge's request/response files and the stdout log offsets are single resources: two sessions
    interleaving there would read each other's answers. fcntl.flock serialises across processes; the
    depth counter keeps nested use inside one process cheap.
    """

    def __init__(self, path: Path):
        self.path = path
        self.fh = None
        self.depth = 0
        self.local = threading.RLock()

    def __enter__(self):
        import fcntl
        self.local.acquire()
        if self.depth == 0:
            self.fh = open(self.path, "a")
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        self.depth += 1
        return self

    def __exit__(self, *exc):
        import fcntl
        self.depth -= 1
        if self.depth == 0:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
            self.fh = None
        self.local.release()


def px(v_mm: float) -> float:
    return v_mm * PX_PER_MM


class Inkscape:
    def __init__(self):
        self.bus = InkscapeBus()
        STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = _ProcessLock(STATE / "client.lock")
        self.trees: dict[str, Tree] = {}          # doc path -> cached XML tree
        self.tree_ids: dict[str, list[str]] = {}  # doc path -> query-all id order when cached
        self.docinfo: dict[str, dict] = {}        # doc path -> {name, file, page_px}
        self.baseline: dict[str, dict[str, Box]] = {}
        self.base_rev: dict[str, str] = {}        # doc path -> revision of the baseline
        self.revision: str | None = None          # fingerprint of the last query-all read
        self.reads = 0                            # query-all reads so far (tools report a revision only if they read)
        self._snapshots: OrderedDict[str, str] = OrderedDict()  # revision -> raw query-all text
        self._pending: PendingCall | None = None  # a bridge activation that timed out, still running
        self._pending_rid: str | None = None      # its request id, to read whether it applied once it ends
        self.tree_rev: dict[str, str] = {}        # doc path -> revision the cached tree is known to match
        self._fresh: set[str] = set()             # docs whose cached tree was just read from the live document
        self.geom_rev: str | None = None          # fingerprint of the last query-all alone
        self.content_tracked = True               # False: the last revision covers geometry only
        self.untracked_reason = ""
        self.notes: list[str] = []                # things the next tool result must tell the agent
        self.last_action_stderr = ""              # Inkscape's stderr from the edit inside with_selection
        self._instance = None                     # (bus connection, unique bus name) of the Inkscape we cached
        self._runs = 0                            # native runs so far
        self._full_run = -1                       # value of _runs right after the last full read
        self._last_full = None
        self._content_ok = False                  # the last full read wrote a fresh content export
        self._pre_rev: str | None = None          # revision read just before the current edit
        self._n_objects = 0
        for sub in ("renders", "live", "bridge"):
            (STATE / sub).mkdir(mode=0o700, parents=True, exist_ok=True)

    # ======================================================================== lifecycle / docs
    def launch(self, path: str | None = None, wait: float = 120.0) -> dict:
        with self.lock:
            before = self.bus.document_paths() if self.bus.running() else []
            args = [str(LAUNCH)] + ([str(Path(path).expanduser().resolve())] if path else [])
            subprocess.run(args, check=True, timeout=60)
            t0 = time.monotonic()
            while time.monotonic() - t0 < wait:
                if self.bus.running():
                    docs = self.bus.document_paths()
                    if (path and len(docs) > len(before)) or (not path and docs):
                        self.bus._describe.clear()
                        return {"documents": docs, "seconds": round(time.monotonic() - t0, 1)}
                time.sleep(0.4)
            raise InkError("Inkscape did not come up (or the file did not open) within %.0f s" % wait)

    def _require(self):
        """bus.require(), and drop everything cached about documents when Inkscape is not the instance it
        was: a restarted Inkscape numbers its first document /document/1 again, so a path alone would hand
        back the old document's page size, name, tree and boxes. The instance is the bus connection plus
        Inkscape's unique bus name (':1.N', never reused while the bus daemon lives)."""
        self.bus.require()
        ident = (getattr(self.bus, "generation", 0), getattr(self.bus, "owner", None))
        if ident != self._instance:
            if self._instance is not None:
                self._forget()
                self.notes.append("Inkscape was restarted (or its connection replaced) since the last call: "
                                  "everything cached about its documents was dropped and is re-read.")
            self._instance = ident

    def _forget(self):
        for d in (self.trees, self.tree_ids, self.tree_rev, self.docinfo, self.baseline, self.base_rev,
                  self._snapshots):
            d.clear()
        self._fresh.clear()
        self.revision = self.geom_rev = self._pre_rev = self._last_full = None
        self._full_run = -1
        self.bus._describe.clear()

    def take_notes(self) -> str:
        out, self.notes = "\n".join(self.notes), []
        return out

    def begin_call(self):
        """Start of a tool call: a full read from an earlier call is never reused."""
        self._full_run = -1
        self._last_full = None

    def documents(self) -> list[str]:
        self._require()
        return self.bus.document_paths()

    def window_titles(self) -> list[tuple[int, str]]:
        """(window number, title) of this instance's windows, front to back."""
        try:
            import Quartz
        except ImportError:
            return []
        pid = self.instance_pid()
        if not pid:
            return []
        out = []
        for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID):
            if w.get("kCGWindowOwnerPID") != pid:
                continue
            if w.get("kCGWindowLayer", 0) == 0 and w.get("kCGWindowName"):
                out.append((int(w["kCGWindowNumber"]), str(w["kCGWindowName"])))
        return out

    def instance_pid(self) -> int | None:
        # anchored, so tag 'claude' does not also match an instance tagged 'claudetest'
        r = subprocess.run(["pgrep", "-f", f"app-id-tag={TAG}( |$)"], capture_output=True, text=True)
        pids = [int(p) for p in r.stdout.split()]
        return min(pids) if pids else None

    def info(self, doc: str) -> dict:
        """Name, file path and page size of a document (one read-only bridge call, then cached)."""
        if doc not in self.docinfo:
            r = self.bridge([{"op": "ping"}], doc=doc, dump=False)
            p = r["results"][0]
            self.docinfo[doc] = {"name": p["docname"], "file": p.get("file"), "page_px": p["page_px"],
                                 "px_per_user_unit": p["px_per_user_unit"]}
        return self.docinfo[doc]

    def active_doc(self) -> str:
        """Object path of the document app-level actions act on (the frontmost Inkscape window)."""
        docs = self.documents()
        if not docs:
            raise InkError("Inkscape (Claude) is running but has no document open.")
        if len(docs) == 1:
            return docs[0]
        titles = self.window_titles()
        if titles:
            front = titles[0][1].lstrip("*").rsplit(" - Inkscape", 1)[0].strip()
            # the window title is the FILE name; a saved copy keeps its source's sodipodi:docname, so match
            # the file first and fall back to the document name
            by_file = [d for d in docs if Path(self.info(d).get("file") or "").name == front]
            if len(by_file) == 1:
                return by_file[0]
            by_name = [d for d in docs if self.info(d)["name"] == front]
            if len(by_name) == 1:
                return by_name[0]
        raise InkError("Several documents are open and the frontmost one could not be matched uniquely; "
                       "click the window you want me to work in, or close the others.")

    # ======================================================================== native layer
    def run(self, actions, path: str | None = None) -> str:
        with self.lock:
            self._require()
            self._settle_pending()  # never read or act on a document an unfinished edit is about to replace
            self._runs += 1
            try:
                return self.bus.run(actions, **({"path": path} if path else {}))
            except TimeoutError:
                raise UncertainCompletion(
                    "UNCERTAIN COMPLETION: Inkscape did not answer this command in time (a dialog may be "
                    "open). It may still apply: look with window or changes before repeating it.") from None

    def raw_boxes(self) -> tuple[list[str], dict[str, Box]]:
        """Inkscape's own visual boxes (document px), in document order. Records the revision."""
        order, boxes, _ = self._query(self.run(["query-all"]))
        return order, boxes

    def _query(self, raw: str):
        text = last_query_block(raw)
        order, boxes = parse_query_all(text)
        if not order:
            raise InkError("Inkscape's query-all returned no objects (busy, a dialog open, or the output was "
                           "lost), so nothing was read and nothing was edited. Try again; call window if it persists.")
        self.geom_rev = fingerprint(order, boxes)
        self.reads += 1
        self._n_objects = len(order)
        return order, boxes, text

    def full_read(self, force_content: bool = False, reuse: bool = False) -> tuple[list[str], dict[str, Box]]:
        """Boxes plus the content hash, in one locked run; sets self.revision. `force_content` writes the
        plain-SVG export even on a document over CONTENT_MAX (for read-back) without changing what the
        revision covers. `reuse` returns the last full read if no command has run since (same call only)."""
        with self.lock:
            if reuse and self._last_full is not None and self._full_run == self._runs and not force_content:
                return self._last_full
            content = force_content or self._n_objects <= CONTENT_MAX
            path = STATE / "live" / "content.svg"
            acts: list = ["query-all"]
            if content:
                acts += list(CONTENT_EXPORT) + [("export-filename", str(path)), ("export-do", None)]
            key0 = _stat_key(path)
            order, boxes, text = self._query(self.run(acts))
            self._content_ok = content and _stat_key(path) not in (None, key0)
            stamp = None
            if len(order) > CONTENT_MAX:
                self.untracked_reason = f"{len(order):,} objects, over the {CONTENT_MAX:,} limit for content tracking"
            elif not self._content_ok:
                self.untracked_reason = "Inkscape wrote no content export"
            else:
                stamp = hashlib.sha1(path.read_bytes()).hexdigest()
            self.content_tracked = stamp is not None
            self.revision = (hashlib.sha1(f"{self.geom_rev}|{stamp}".encode()).hexdigest()[:12] if stamp
                             else self.geom_rev)
            self._snapshots[self.revision] = text
            self._snapshots.move_to_end(self.revision)
            while len(self._snapshots) > SNAPSHOTS:
                self._snapshots.popitem(last=False)
            for d in self._fresh:
                self.tree_rev[d] = self.revision
            self._fresh.clear()
            self._full_run, self._last_full = self._runs, (order, boxes)
            return order, boxes

    def full_boxes(self, force_content: bool = False):
        """full_read() with groups that contain a nested <svg> rebuilt from their children."""
        order, boxes = self.full_read(force_content=force_content)
        boxes = dict(boxes)
        t = self.trees.get(self.active_doc())
        if t is not None:
            t.correct_boxes(boxes)
        return order, boxes

    def boxes(self) -> tuple[list[str], dict[str, Box]]:
        """Visual boxes with groups that contain a nested <svg> rebuilt from their children."""
        order, boxes = self.raw_boxes()
        try:
            t = self.trees.get(self.active_doc())
        except Exception:
            t = None
        if t is not None:
            t.correct_boxes(boxes)
        return order, boxes

    def ensure_tree(self):
        """Make sure some tree is cached (it can be slightly stale) so boxes() can be corrected."""
        doc = self.active_doc()
        if doc not in self.trees:
            self.tree()

    def affected(self, ids) -> bool:
        t = self.trees.get(self.active_doc())
        return bool(t is not None and set(ids) & set(t.nested_svg_ancestors))

    def selection(self) -> list[str]:
        out = self.run(["select-list"])
        return [line.split(" ", 1)[0] for line in out.splitlines() if " cloned: " in line]

    def select(self, ids: list[str]):
        bad = [i for i in ids if "," in i]
        if bad:
            raise InkError(f"ids containing commas cannot be selected natively: {bad}")
        self.run(["select-clear"] + ([("select-by-id", ",".join(ids))] if ids else []))

    def _check_ids(self, ids, boxes):
        missing = [i for i in ids if i not in boxes]
        if missing:
            raise InkError(f"no visible object with id(s) {missing[:10]} — use find or outline")

    def with_selection(self, ids, actions, restore=True) -> str:
        """Select ids (in order), run actions, then put the user's selection back."""
        with self.lock:
            prior = self.selection() if restore else []
            self.select(ids)
            try:
                out = self.run(actions)
                self.last_action_stderr = self.bus.last_stderr  # before restoring the selection overwrites it
                return out
            finally:
                if restore:
                    try:
                        self.select(prior)
                    except Exception:
                        pass

    def translate(self, moves: dict[str, tuple[float, float]], restore=True) -> int:
        """Move each id by (dx_px, dy_px); ids sharing a delta move together. Returns undo steps."""
        groups: dict[tuple[float, float], list[str]] = {}
        for i, d in moves.items():
            d = (round(d[0], 6), round(d[1], 6))
            if abs(d[0]) < 1e-6 and abs(d[1]) < 1e-6:
                continue
            groups.setdefault(d, []).append(i)
        with self.lock:
            prior = self.selection() if restore else []
            try:
                for (dx, dy), ids in groups.items():
                    self.select(ids)
                    self.run([("transform-translate", f"{dx:.6f},{dy:.6f}")])
            finally:
                if restore:
                    self.select(prior)
        return len(groups)

    # ======================================================================== revision guard
    def snapshot(self, revision: str) -> tuple[list[str], dict[str, Box]] | None:
        """Boxes as they were at a revision this process read (corrected with the current tree)."""
        text = self._snapshots.get(revision)
        if text is None:
            return None
        order, boxes = parse_query_all(text)
        try:
            t = self.trees.get(self.active_doc())
        except Exception:
            t = None
        if t is not None:
            t.correct_boxes(boxes)
        return order, boxes

    def guard(self, expected_revision: str | None):
        """Refuse (raise RevisionConflict) when the last read is not the revision the caller expects.
        Call it right after reading the `before` boxes, before changing anything."""
        if not expected_revision or expected_revision == self.revision:
            return
        snap = self.snapshot(expected_revision)
        if snap is None:
            what = "That revision is not one I have read in this session, so I cannot list the difference."
        else:
            order, now = self.raw_boxes()
            t = self.trees.get(self.active_doc())
            if t is not None:
                t.correct_boxes(now)
            lines = self._diff_lines(snap[1], order, now, t, top=8, per_delta=6)
            what = "\n".join(lines) if lines else (
                "No object moved, resized, appeared or vanished: the change is in what they show at the same "
                "boxes (text, colour, an image, a same-box transform). changes(deep=True) lists text and style "
                "differences.")
        raise RevisionConflict(
            f"Refused, nothing was edited: the document changed since revision {expected_revision} "
            f"(it is now {self.revision}). Changes since then:\n{what}\n"
            f"Re-check the objects you meant to edit, then retry with expected_revision={self.revision}.")

    def guarded_boxes(self, expected_revision: str | None, force_content: bool = False):
        """Read the live boxes and the revision, then apply the revision guard."""
        order, boxes = self.full_boxes(force_content=force_content)
        self._pre_rev = self.revision
        self.guard(expected_revision)
        return order, boxes

    # ======================================================================== bridge
    def _settle_pending(self, wait: float = 30.0):
        """A previous bridge call timed out: wait until Inkscape has finished it (its D-Bus reply comes
        only after the document holds the result) before sending anything that edits or reads it."""
        native = getattr(self.bus, "pending", None)
        if native is not None:
            name = getattr(self.bus, "pending_name", None) or "?"
            try:
                done, how = native.wait(wait), "finished"
            except Exception:  # it ended with a D-Bus error: no longer running either way
                done, how = True, "ended with an error"
            if not done:
                raise UncertainCompletion(
                    f"UNCERTAIN COMPLETION: an earlier command ({name}) that did not finish in time is still "
                    "running in Inkscape, so I have not sent another. Look at the window (a dialog may be open) "
                    "or call changes; try again once it has finished.")
            self.bus.pending = self.bus.pending_name = None
            self.trees.clear()
            self.notes.append(f"Late landing: the earlier command '{name}' that timed out has since {how} in "
                              "Inkscape, so it may have applied after all. Check with changes before building "
                              "on what that call reported.")
        if not self._pending:
            return
        try:
            done = self._pending.wait(wait)
        except Exception:  # it finished with an error: it is no longer running either way
            done = True
        if done:
            self._pending = None
            self.trees.clear()  # it may have changed the document after all
            self.notes.append("Late landing: the earlier edit that timed out has since finished: "
                              + self._late_bridge_outcome() + " Check with changes before building on it.")
            return
        raise UncertainCompletion(
            "UNCERTAIN COMPLETION: an earlier edit that did not finish in time is still running in "
            "Inkscape, so I have not sent another. Look at the window (a dialog may be open) or call "
            "changes; try again once it has finished.")

    def _late_bridge_outcome(self) -> str:
        try:
            r = json.loads((STATE / "bridge" / "response.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            r = None
        rid, self._pending_rid = self._pending_rid, None
        if not r or r.get("id") != rid:
            return "the extension left no answer for it (it may not have run)."
        if not r.get("ok"):
            return "it FAILED and left the document untouched."
        if r.get("noop") or not r.get("changed"):
            return "it changed nothing."
        return "it APPLIED (one undo step)."

    def bridge(self, ops: list[dict], doc: str | None = None, dump: bool = True, timeout: float = 900) -> dict:
        """Run a batch through the bridge extension. Raises InkError if the batch failed (document
        untouched) and UncertainCompletion if Inkscape did not answer within `timeout` seconds."""
        with self.lock:
            self._require()
            self._settle_pending()
            doc = doc or self.active_doc()
            br = STATE / "bridge"
            rid = uuid.uuid4().hex
            res = br / "response.json"
            res.unlink(missing_ok=True)
            req = {"id": rid, "ops": ops}
            cache = STATE / "live" / f"doc{doc.rsplit('/', 1)[-1]}.svg"
            if dump:
                req["dump_to"] = str(cache)
            tmp = br / "request.json.tmp"
            tmp.write_text(json.dumps(req))
            os.replace(tmp, br / "request.json")
            t0 = time.monotonic()
            uncertain = UncertainCompletion(
                f"UNCERTAIN COMPLETION: Inkscape did not finish this edit within {timeout:g} s. It may "
                "still be running and may yet apply (a dialog open in Inkscape can also hold it). Do not "
                "repeat it: check with changes or render first. The next edit waits for this one.")
            try:
                self.bus.activate_tracked(BRIDGE_ACTION, path=doc, timeout=timeout)
            except UnknownAction:
                raise InkError("The bridge extension is not loaded in this Inkscape: run install.sh, then quit "
                               "and reopen Inkscape (Claude), which reads extensions at startup.") from None
            except CallTimeout as e:
                self._pending, self._pending_rid = e.call, rid
                self.trees.pop(doc, None)
                raise uncertain from None
            t1 = time.monotonic()  # Inkscape has finished: the response is already written, or never will be
            while time.monotonic() - t1 < 5:
                try:
                    r = json.loads(res.read_text())
                    if r.get("id") == rid:
                        break
                except (FileNotFoundError, json.JSONDecodeError):
                    pass
                time.sleep(0.05)
            else:  # Inkscape answered but the extension never wrote its response (it did not run)
                raise InkError("Inkscape ran the bridge action but the extension did not respond: it may have "
                               f"failed to start (see {STATE / 'inkscape.err.log'}), or a dialog is open.")
            r["seconds"] = round(time.monotonic() - t0, 2)
            if not r.get("ok"):
                raise InkError("bridge batch failed, document untouched:\n" + (r.get("error") or "?"))
            for op, res_ in zip(ops, r.get("results", [])):
                if op.get("op") == "ping" and isinstance(res_, dict):
                    self.docinfo[doc] = {"name": res_["docname"], "file": res_.get("file"),
                                         "page_px": res_["page_px"], "px_per_user_unit": res_["px_per_user_unit"]}
            if r.get("dumped"):
                order, _ = self.raw_boxes()
                self.trees[doc] = Tree.from_file(cache, "bridge")
                self.tree_ids[doc] = order
                self._fresh.add(doc)
            elif r.get("changed"):
                self.trees.pop(doc, None)
            return r

    def bridge_edit(self, ops: list[dict], before: dict[str, Box], expect_added=(), timeout: float = 900,
                    dump: bool = True) -> tuple[dict, Edit]:
        """A mutating bridge batch plus its landed verdict (before = boxes read before the batch)."""
        pre = self._pre_rev
        r = self.bridge(ops, dump=dump, timeout=timeout)
        _, after = self.boxes()
        doc = self.active_doc()
        ours = doc in self.trees and doc not in self._fresh and pre is not None and self.tree_rev.get(doc) == pre
        self.full_read()
        if ours:
            self.tree_rev[doc] = self.revision
        self.remember(after)
        if r.get("noop"):
            verdict = "No change: the batch left the document byte-identical, so no undo step was added."
            steps = 0
        else:
            verdict = landed(before, after, added=list(r.get("drawn_added", [])) + list(expect_added),
                             removed=r.get("removed", []), touched=r.get("touched", []),
                             unchanged="the change left every touched object's box as it was (a colour, "
                                       "attribute or same-width text change), and geometry is all Inkscape "
                                       "reports back.")
            steps = 1 if r.get("changed") else 0
        return r, Edit(steps, before, after, verdict, self.revision)

    # ======================================================================== structure (cached)
    def tree(self, refresh: bool = False) -> tuple[Tree, list[str], dict[str, Box], str]:
        """The document tree plus fresh boxes. Re-reads the tree when the document changed in a way this
        server did not make itself (any change, text and colour included, while content is tracked; a
        change in the set of ids otherwise)."""
        with self.lock:
            doc = self.active_doc()
            order, boxes = self.full_read()
            boxes = dict(boxes)
            t = self.trees.get(doc)
            if (t is None or refresh or self.tree_ids.get(doc) != order
                    or (self.content_tracked and self.tree_rev.get(doc) != self.revision)):
                t = self._refresh_tree(doc, order)
                self._fresh.discard(doc)
                self.tree_rev[doc] = self.revision
                self._full_run = self._runs  # the refresh only read: the revision above still stands
            t.correct_boxes(boxes)
            return t, order, boxes, doc

    def _refresh_tree(self, doc: str, order: list[str]) -> Tree:
        info = self.docinfo.get(doc)
        titles = self.window_titles()
        # The saved file IS the live document when the window shows no unsaved changes: read it
        # from disk (fast) and check it carries exactly the live ids before trusting it.
        if info and info.get("file") and Path(info["file"]).exists() and titles:
            front = titles[0][1]
            if not front.startswith("*") and front.rsplit(" - Inkscape", 1)[0].strip() == info["name"]:
                t = Tree.from_file(info["file"], "disk")
                if set(order) <= set(t.ids):
                    self.trees[doc], self.tree_ids[doc] = t, order
                    return t
        self.bridge([{"op": "ping"}], doc=doc, dump=True)  # the ping also fills docinfo
        return self.trees[doc]

    # ======================================================================== geometry helpers
    def page_box(self) -> Box:
        doc = self.active_doc()
        t = self.trees.get(doc)
        if t is not None:
            w, h = page_size_px(t.root)
        else:
            w, h = self.info(doc)["page_px"]
        return Box(0, 0, w, h)

    def target_box(self, ids, boxes) -> Box:
        self._check_ids(ids, boxes)
        return Box.union(boxes[i] for i in ids)

    def report(self, ids, before, after) -> str:
        rows = []
        for i in ids:
            a = after.get(i)
            b = before.get(i)
            if a is None:
                rows.append(f"  {i}: gone")
                continue
            moved = ""
            if b is not None:
                dx, dy, dw, dh = (mm(a.x - b.x), mm(a.y - b.y), mm(a.w - b.w), mm(a.h - b.h))
                bits = [f"Δx {dx:+.2f}" if abs(dx) > 0.005 else "", f"Δy {dy:+.2f}" if abs(dy) > 0.005 else "",
                        f"Δw {dw:+.2f}" if abs(dw) > 0.005 else "", f"Δh {dh:+.2f}" if abs(dh) > 0.005 else ""]
                bits = [x for x in bits if x]
                moved = "  (" + ", ".join(bits) + " mm)" if bits else "  (unchanged)"
            rows.append(f"  {i}: {a.mm()}{moved}")
        return "\n".join(rows)

    def remember(self, boxes):
        try:
            doc = self.active_doc()
            self.baseline[doc] = dict(boxes)
            self.base_rev[doc] = self.revision
        except Exception:
            pass

    def _done(self, steps, before, after, verdict, ids=()) -> Edit:
        pre = self._pre_rev
        self.full_read(reuse=True)
        # This edit was ours: if the cached tree matched the document just before it, it still answers
        # for text, style and structure (tools that change those patch it or drop it themselves).
        doc = self.active_doc()
        if doc in self.trees and pre is not None and self.tree_rev.get(doc) == pre:
            self.tree_rev[doc] = self.revision
        self.remember(after)
        return Edit(steps, before, after, verdict, self.revision, list(ids))

    # ======================================================================== layout
    def align(self, ids, edges: str, to: str = "first", to_mm: float | None = None, as_group: bool = False,
              *, expected_revision: str | None = None) -> Edit:
        words = edges.replace(",", " ").split()
        bad = [w for w in words if w not in EDGES_H + EDGES_V]
        if not words or bad or sum(w in EDGES_H for w in words) > 1 or sum(w in EDGES_V for w in words) > 1:
            raise InkError("edges must be one of left/hcenter/right and/or one of top/vcenter/bottom")
        self.ensure_tree()
        _, before = self.guarded_boxes(expected_revision)
        self._check_ids(ids, before)
        if to_mm is not None and len(words) != 1:
            raise InkError("to_mm aligns one edge at a time (it is a single coordinate)")
        if to_mm is None and to not in NATIVE_ALIGN_TARGETS and to not in before:
            raise InkError(f"'to' must be an object id or one of {NATIVE_ALIGN_TARGETS}")
        involved = list(ids) + ([to] if to in before else [])
        if to_mm is None and not self.affected(involved) and to not in ("drawing",):
            # Inkscape's own Align: one undo step
            order, rel = (list(ids), to) if to in NATIVE_ALIGN_TARGETS else ([to] + [i for i in ids if i != to], "first")
            arg = " ".join(words) + " " + rel + (" group" if as_group else "")
            self.with_selection(order, [("object-align", arg)])
            steps = 1
        else:
            # computed from corrected boxes (groups holding nested <svg> have wrong native boxes)
            movers = [i for i in ids if i != to]
            if to_mm is not None:
                ref = {words[0]: px(to_mm)}
            else:
                if to in before and to not in NATIVE_ALIGN_TARGETS:
                    rb = before[to]
                elif to == "first":
                    rb, movers = before[ids[0]], list(ids[1:])
                elif to == "last":
                    rb, movers = before[ids[-1]], list(ids[:-1])
                elif to in ("biggest", "smallest"):
                    pick = (max if to == "biggest" else min)(ids, key=lambda i: before[i].w * before[i].h)
                    rb, movers = before[pick], [i for i in ids if i != pick]
                elif to == "page":
                    rb = self.page_box()
                elif to == "drawing":
                    t = self.trees[self.active_doc()]
                    tops = [c.get("id") for c in t.visible_children(t.root) if c.get("id") in before]
                    rb = Box.union(before[i] for i in tops)
                else:  # selection
                    rb = Box.union(before[i] for i in ids)
                ref = {w: rb.edge(w) for w in words}
            units = [movers] if as_group else [[i] for i in movers]
            moves = {}
            for unit in units:
                ub = Box.union(before[i] for i in unit)
                dx = sum(ref[w] - ub.edge(w) for w in words if w in EDGES_H)
                dy = sum(ref[w] - ub.edge(w) for w in words if w in EDGES_V)
                for i in unit:
                    moves[i] = (dx, dy)
            steps = self.translate(moves)
        _, after = self.boxes()
        return self._done(steps, before, after, self._check_align(ids, words, to, to_mm, as_group, before, after))

    def _check_align(self, ids, words, to, to_mm, as_group, before, after) -> str:
        """Whatever the target, aligned objects end up sharing the edge (with the anchor, or the page
        or a coordinate), and an anchor object stays where it was."""
        anchor, ref = None, {}
        if to_mm is not None:
            ref = {words[0]: px(to_mm)}
        elif to == "page":
            ref = {w: self.page_box().edge(w) for w in words}
        elif to in before and to not in NATIVE_ALIGN_TARGETS:
            anchor = to
        elif to in ("first", "last"):
            anchor = ids[0] if to == "first" else ids[-1]
        missing = [i for i in ids if i not in after]
        if missing:
            return f"NOT LANDED as planned: {missing[:5]} vanished"
        movers = [i for i in ids if i != anchor]
        units = ([movers] if as_group else [[i] for i in movers]) + ([[anchor]] if anchor else [])
        bad = []
        for w in words:
            vals = [Box.union(after[i] for i in u).edge(w) for u in units if u] + ([ref[w]] if w in ref else [])
            if vals and max(vals) - min(vals) > TOL:
                bad.append(f"{w} edges still span {mm(max(vals) - min(vals)):.3f} mm")
        if anchor and anchor in before and not _same(before[anchor], after[anchor]):
            bad.append(f"the anchor {anchor} moved")
        if bad:
            return "NOT LANDED as planned: " + "; ".join(bad)
        return f"Landed: confirmed ({len(units)} object(s) share the {' and '.join(words)} edge to 0.01 mm)."

    def distribute(self, ids, axis: str = "y", gap_mm: float | None = None, mode: str = "gap",
                   start_mm: float | None = None, *, expected_revision: str | None = None) -> Edit:
        self.ensure_tree()
        _, before = self.guarded_boxes(expected_revision)
        self._check_ids(ids, before)
        if axis not in ("x", "y"):
            raise InkError("axis is 'x' (left to right) or 'y' (top to bottom)")
        if gap_mm is None and start_mm is None and mode == "gap" and self.affected(ids) and len(ids) > 2:
            # equal gaps computed from corrected boxes
            key = (lambda i: before[i].x) if axis == "x" else (lambda i: before[i].y)
            seq = sorted(ids, key=key)
            size = (lambda b: b.w) if axis == "x" else (lambda b: b.h)
            start = key(seq[0])
            end = (before[seq[-1]].x2 if axis == "x" else before[seq[-1]].y2)
            gap_px = (end - start - sum(size(before[i]) for i in seq)) / (len(seq) - 1)
            gap_mm = mm(gap_px)
        if gap_mm is None and start_mm is None:
            native = {("x", "gap"): "hgap", ("y", "gap"): "vgap", ("x", "centers"): "hcenter",
                      ("y", "centers"): "vcenter", ("x", "left"): "left", ("x", "right"): "right",
                      ("y", "top"): "top", ("y", "bottom"): "bottom"}.get((axis, mode))
            if native is None:
                raise InkError("mode is gap | centers | left | right (x) | top | bottom (y)")
            self.with_selection(list(ids), [("object-distribute", native)])
            _, after = self.boxes()
            return self._done(1, before, after, self._check_spacing(ids, axis, mode, after))
        key = (lambda i: before[i].x) if axis == "x" else (lambda i: before[i].y)
        seq = sorted(ids, key=key)
        moves = {}
        gap = px(gap_mm if gap_mm is not None else 0.0)
        cursor = px(start_mm) if start_mm is not None else None
        for i in seq:
            b = before[i]
            start = b.x if axis == "x" else b.y
            size = b.w if axis == "x" else b.h
            if cursor is None:
                cursor = start
            d = cursor - start
            moves[i] = (d, 0) if axis == "x" else (0, d)
            cursor = cursor + size + gap
        steps = self.translate(moves)
        _, after = self.boxes()
        return self._done(steps, before, after,
                          landed(before, after, exact={i: _shift(before[i], d) for i, d in moves.items()}))

    @staticmethod
    def _check_spacing(ids, axis, mode, after) -> str:
        """Inkscape's equal distribution: consecutive gaps (or edge/centre steps) are all equal."""
        if any(i not in after for i in ids):
            return f"NOT LANDED as planned: {[i for i in ids if i not in after][:5]} vanished"
        lo = (lambda b: b.x) if axis == "x" else (lambda b: b.y)
        hi = (lambda b: b.x2) if axis == "x" else (lambda b: b.y2)
        if mode == "gap":
            seq = sorted(ids, key=lambda i: lo(after[i]))
            steps = [lo(after[b]) - hi(after[a]) for a, b in zip(seq, seq[1:])]
        else:
            edge = {"centers": "hcenter" if axis == "x" else "vcenter"}.get(mode, mode)
            vals = sorted(after[i].edge(edge) for i in ids)
            steps = [b - a for a, b in zip(vals, vals[1:])]
        if len(steps) < 2:
            return "Landed: unconfirmed — two objects have nothing to equalise."
        spread = max(steps) - min(steps)
        if spread > TOL:
            return f"NOT LANDED as planned: spacings differ by {mm(spread):.3f} mm"
        return f"Landed: confirmed ({len(steps)} equal spacings of {mm(steps[0]):.2f} mm, to 0.01 mm)."

    def move(self, ids, dx_mm=0.0, dy_mm=0.0, x_mm=None, y_mm=None, anchor="top-left", each=False,
             *, expected_revision: str | None = None) -> Edit:
        if x_mm is not None or y_mm is not None:
            self.ensure_tree()
        _, before = self.guarded_boxes(expected_revision)
        self._check_ids(ids, before)
        ax = {"left": "left", "center": "hcenter", "right": "right"}
        ay = {"top": "top", "middle": "vcenter", "center": "vcenter", "bottom": "bottom"}
        parts = anchor.split("-") if "-" in anchor else ([anchor, anchor] if anchor == "center" else [anchor, ""])
        ey = ay.get(parts[0], "top")
        ex = ax.get(parts[1] if len(parts) > 1 and parts[1] else "left", "left")
        units = [[i] for i in ids] if each else [list(ids)]
        moves = {}
        for unit in units:
            b = self.target_box(unit, before)
            dx = px(dx_mm) + (px(x_mm) - b.edge(ex) if x_mm is not None else 0)
            dy = px(dy_mm) + (px(y_mm) - b.edge(ey) if y_mm is not None else 0)
            for i in unit:
                moves[i] = (dx, dy)
        steps = self.translate(moves)
        _, after = self.boxes()
        return self._done(steps, before, after,
                          landed(before, after, exact={i: _shift(before[i], d) for i, d in moves.items()}))

    def resize(self, ids, width_mm=None, height_mm=None, scale=None, keep_aspect=True, anchor="top-left",
               each=False, *, expected_revision: str | None = None) -> Edit:
        self.ensure_tree()
        _, before = self.guarded_boxes(expected_revision)
        self._check_ids(ids, before)
        units = [[i] for i in ids] if each else [list(ids)]
        steps = 0
        targets = []  # (unit, b0, want_w or None, want_h or None)
        for unit in units:
            b0 = self.target_box(unit, before)
            fx = fy = None
            if scale is not None:
                fx = fy = float(scale)
            if width_mm is not None:
                fx = px(width_mm) / b0.w
            if height_mm is not None:
                fy = px(height_mm) / b0.h
            if fx is None and fy is None:
                raise InkError("give width_mm, height_mm or scale")
            ex, ey = self._anchor_edges(anchor)
            if keep_aspect:
                f = fx if fx is not None else fy
                if fx is not None and fy is not None and abs(fx - fy) > 1e-6:
                    raise InkError("keep_aspect=True takes width_mm OR height_mm, not both")
                for _attempt in range(3):  # scale strokes/text may not track linearly: re-measure
                    with self.lock:
                        prior = self.selection()
                        self.select(unit)
                        self.run([("transform-scale", f)])
                        self.select(prior)
                    steps += 1
                    _, now = self.boxes()
                    b1 = Box.union(now[i] for i in unit)
                    want_w = b0.w * (fx if fx is not None else fy)
                    want = want_w if fx is not None else b0.h * fy
                    have = b1.w if fx is not None else b1.h
                    if abs(have - want) < px(0.02):
                        break
                    f = want / have
                # restore the anchor point
                _, now = self.boxes()
                b1 = Box.union(now[i] for i in unit)
                dx = b0.edge(ex) - b1.edge(ex)
                dy = b0.edge(ey) - b1.edge(ey)
                steps += self.translate({i: (dx, dy) for i in unit})
                targets.append((unit, b0, b0.w * fx if fx is not None else None,
                                b0.h * fy if fx is None else None, ex, ey))
            else:
                ap = (b0.edge(ex), b0.edge(ey))
                self.bridge([{"op": "scale", "ids": unit, "sx": fx or 1.0, "sy": fy or 1.0, "anchor_px": ap}])
                steps += 1
                targets.append((unit, b0, b0.w * (fx or 1.0), b0.h * (fy or 1.0), ex, ey))
        _, after = self.boxes()
        bad = []
        for unit, b0, ww, wh, ex, ey in targets:
            if any(i not in after for i in unit):
                bad.append(f"{unit[:3]} vanished")
                continue
            b1 = Box.union(after[i] for i in unit)
            if ww is not None and abs(b1.w - ww) > px(0.02):
                bad.append(f"width {mm(b1.w):.3f} mm, planned {mm(ww):.3f}")
            if wh is not None and abs(b1.h - wh) > px(0.02):
                bad.append(f"height {mm(b1.h):.3f} mm, planned {mm(wh):.3f}")
            if abs(b1.edge(ex) - b0.edge(ex)) > TOL or abs(b1.edge(ey) - b0.edge(ey)) > TOL:
                bad.append(f"the {anchor} anchor moved")
        verdict = ("NOT LANDED as planned: " + "; ".join(bad[:5]) if bad else
                   f"Landed: confirmed ({len(targets)} size(s) to 0.02 mm, {anchor} anchor fixed).")
        return self._done(steps, before, after, verdict)

    @staticmethod
    def _anchor_edges(anchor: str):
        a = anchor.replace("_", "-")
        if a == "center":
            return "hcenter", "vcenter"
        v, _, h = a.partition("-")
        ey = {"top": "top", "middle": "vcenter", "center": "vcenter", "bottom": "bottom"}.get(v, "top")
        ex = {"left": "left", "center": "hcenter", "right": "right"}.get(h or "left", "left")
        return ex, ey

    # ======================================================================== style / attributes
    def _styles_of(self, ids) -> dict[str, dict[str, str]] | None:
        """Each id's style as the last full read's plain-SVG export holds it (presentation attributes as a
        fallback); None when that read wrote no export."""
        from lxml import etree
        path = STATE / "live" / "content.svg"
        if not self._content_ok:
            return None
        want, out = set(ids), {}
        for el in etree.parse(str(path), etree.XMLParser(huge_tree=True)).getroot().iter():
            i = el.get("id") if isinstance(el.tag, str) else None
            if i in want:
                st = {k: v for k, v in el.attrib.items() if "}" not in k}
                st.update(parse_style(el.get("style")))
                out[i] = st
        return out

    def style(self, ids, props: dict, *, expected_revision: str | None = None) -> Edit:
        """Set style properties, then read every value back from the live document. Inkscape's
        object-set-property splits its argument on commas and refuses anything with more than one (a dash
        list, rgb(), a font list), so those values go through the bridge with the removals."""
        _, before = self.guarded_boxes(expected_revision, force_content=True)
        self._check_ids(ids, before)
        was = self._styles_of(ids) or {}
        native = {k: v for k, v in props.items() if v is not None and "," not in str(v) and ";" not in str(v)}
        rest = {k: v for k, v in props.items() if k not in native}
        steps, said = 0, []
        if native:
            self.with_selection(list(ids), [("object-set-property", f"{k},{v}") for k, v in native.items()])
            said, _ = inkscape_said(self.last_action_stderr)
            steps += len(native)
        if rest:
            r = self.bridge([{"op": "set_style", "ids": list(ids), "props": rest}])
            steps += 1 if r.get("changed") else 0
        _, after = self.boxes()
        self.full_read(force_content=True)
        now = self._styles_of(ids)
        if now is None:
            verdict = "Landed: unconfirmed — the document could not be read back to check the values."
        else:
            refused, renamed, ok = [], [], 0
            for i in ids:
                for k, want in props.items():
                    have, old = now.get(i, {}).get(k), was.get(i, {}).get(k)
                    if want is None:
                        ok += have is None
                        if have is not None:
                            refused.append(f"{k} on {i} is still '{have}'")
                    elif have is not None and norm_css(have) == norm_css(want):
                        ok += 1
                        if have != str(want):
                            renamed.append(f"{k} on {i} stored as '{have}'")
                    else:
                        refused.append(f"{k}={want} on {i} (" + (f"still '{have}'" if have == old and have is not None
                                       else f"now '{have}'" if have is not None else "not set") + ")")
            if refused:
                verdict = ("NOT LANDED as planned: Inkscape did not take " + "; ".join(refused[:6])
                           + (f" (it said: {' | '.join(said)})" if said else "")
                           + (f". {ok} other value(s) read back as set." if ok else "."))
            else:
                verdict = (f"Landed: confirmed ({ok} value(s) read back from the live document"
                           + ("; " + "; ".join(renamed[:6]) if renamed else "") + ").")
            t = self.trees.get(self.active_doc())
            if t is not None:  # keep the cached tree to what the document holds, never to what was asked
                for i in ids:
                    el = t.ids.get(i)
                    if el is None or i not in now:
                        continue
                    st = parse_style(el.get("style"))
                    for k in props:
                        if now[i].get(k) is None:
                            st.pop(k, None)
                        else:
                            st[k] = now[i][k]
                    el.set("style", ";".join(f"{k}:{v}" for k, v in st.items()))
        return self._done(steps, before, after, verdict)

    def attributes(self, ids, attrs: dict, *, expected_revision: str | None = None) -> Edit:
        _, before = self.guarded_boxes(expected_revision)
        self._check_ids(ids, before)
        native = {k: v for k, v in attrs.items() if v is not None and "," not in str(v) and k != "id"}
        rest = {k: v for k, v in attrs.items() if k not in native}
        steps = 0
        if native:
            self.with_selection(list(ids), [("object-set-attribute", f"{k},{v}") for k, v in native.items()])
            steps += len(native)
        if rest:
            r = self.bridge([{"op": "set_attrs", "ids": list(ids), "attrs": rest}])
            steps += 1 if r.get("changed") else 0
        else:
            doc = self.active_doc()
            t = self.trees.get(doc)
            if t is not None:
                for i in ids:
                    el = t.ids.get(i)
                    if el is not None:
                        for k, v in native.items():
                            el.set(qname(k), str(v))
        _, after = self.boxes()
        new_id = attrs.get("id")
        if new_id and len(ids) == 1:  # a rename: the old id goes, the new one appears
            verdict = landed(before, after, added=[new_id], removed=[ids[0]])
        else:
            verdict = landed(before, after, touched=ids, unchanged="this attribute change has no geometric "
                             "trace, and geometry is all Inkscape reports back.")
        return self._done(steps, before, after, verdict)

    # ======================================================================== structure edits
    def structure(self, op: str, ids, parent: str | None = None, index: int | None = None,
                  new_id: str | None = None, label: str | None = None, *,
                  expected_revision: str | None = None) -> Edit:
        order_before, before = self.guarded_boxes(expected_revision)
        if ids:
            self._check_ids(ids, before)
        native = {"delete": "delete-selection", "raise": "selection-raise", "lower": "selection-lower",
                  "top": "selection-top", "bottom": "selection-bottom", "ungroup": "selection-ungroup",
                  "group": "selection-group", "duplicate": "duplicate", "unclone": "clone-unlink",
                  "clone": "clone", "pop_out": "selection-ungroup-pop"}
        result_ids: list[str] = []
        steps = 1
        if op in native:
            with self.lock:
                prior = self.selection()
                self.select(list(ids))
                self.run([native[op]])
                result_ids = self.selection()
                if op == "group" and (new_id or label) and result_ids:
                    if label:
                        self.run([("object-set-attribute", f"inkscape:label,{label}")])
                        steps += 1
                    if new_id:
                        self.bridge([{"op": "set_attrs", "ids": result_ids[:1], "attrs": {"id": new_id}}])
                        result_ids = [new_id]
                        steps += 1
                keep = [p for p in prior if p not in ids] if op in ("delete", "group", "ungroup") else prior
                self.select(keep)
            doc = self.active_doc()
            if op in ("delete", "group", "ungroup", "duplicate", "clone", "pop_out", "unclone"):
                self.trees.pop(doc, None)
        elif op == "reparent":
            r = self.bridge([{"op": "reparent", "ids": list(ids), "parent": parent, "index": index}])
            result_ids = r["results"][0]
        else:
            raise InkError("op is one of: " + ", ".join(list(native) + ["reparent"]))
        order_after, after = self.boxes()
        if op == "delete":
            verdict = landed(before, after, removed=ids)
        elif op in ("group", "duplicate", "clone"):
            new = [i for i in result_ids if i not in before]
            verdict = landed(before, after, added=new, exact={i: before[i] for i in ids if op != "group"} or None)
        elif op == "ungroup":
            verdict = landed(before, after, removed=[i for i in ids if i not in result_ids])
        elif op in ("reparent", "pop_out", "unclone"):  # these keep every object where it was on the page
            verdict = landed(before, after, exact={i: before[i] for i in ids if i in result_ids or op == "reparent"})
        else:  # z-order: query-all lists objects in document (paint) order
            pos_b = {i: n for n, i in enumerate(order_before)}
            pos_a = {i: n for n, i in enumerate(order_after)}
            shifted = [i for i in ids if pos_a.get(i) != pos_b.get(i)]
            verdict = (f"Landed: confirmed ({len(shifted)} object(s) moved in paint order)." if shifted else
                       "Landed: unconfirmed — paint order did not change (already at that end of the stack?).")
        return self._done(steps, before, after, verdict, result_ids)

    def history(self, op: str, n: int = 1, *, expected_revision: str | None = None) -> Edit:
        doc = self.active_doc()
        if op not in ("undo", "redo"):
            raise InkError("op is undo or redo")
        _, before = self.guarded_boxes(expected_revision)
        rev0 = self.revision
        self.run([op] * max(1, int(n)), path=doc)
        self.trees.pop(doc, None)
        _, after = self.boxes()
        self.full_read()
        verdict = (f"Landed: confirmed (revision {rev0} → {self.revision})." if self.revision != rev0 else
                   "Landed: unconfirmed — the document did not change (nothing to " + op + "?)"
                   + ("" if self.content_tracked else "; only geometry is tracked on this document") + ".")
        return self._done(max(1, int(n)), before, after, verdict)

    # ======================================================================== renders
    def render(self, area: Box, width_px: int = 1600, only_ids: list[str] | None = None,
               background: str = "#ffffff", path: Path | None = None) -> Path:
        path = Path(path).expanduser().resolve() if path else (STATE / "renders" / f"render-{int(time.time() * 1000)}.png")
        path.unlink(missing_ok=True)
        acts = [("export-type", "png"), ("export-plain-svg", False),
                ("export-height", 0),  # export settings persist between calls; a height set earlier would stretch this
                ("export-id", ",".join(only_ids) if only_ids else ""), ("export-id-only", bool(only_ids)),
                ("export-area", f"{area.x:.3f}:{area.y:.3f}:{area.x2:.3f}:{area.y2:.3f}"),
                ("export-width", int(width_px)), ("export-background", background),
                ("export-background-opacity", 1.0), ("export-overwrite", True),
                ("export-filename", str(path)), ("export-do", None)]
        self.run(acts)
        if not path.exists():
            raise InkError(f"render produced no file; see {STATE / 'inkscape.err.log'}")
        self._prune_renders()
        return path

    @staticmethod
    def _prune_renders(keep: int = KEEP_RENDERS, batch: int = PRUNE_BATCH):
        """Move all but the newest `keep` default-named renders and window shots out of STATE/renders once
        `batch` more have piled up. A path the caller chose is never touched; a failed move is ignored (the
        render itself already succeeded)."""
        files = [p for pat in ("render-*.png", "window-*.png") for p in (STATE / "renders").glob(pat)]
        if len(files) < keep + batch:
            return
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        try:
            subprocess.run(["quarantine", "-m", "inkscape-live-mcp: old render cache (images were returned inline)"]
                           + [str(p) for p in files[keep:]], capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass

    def window_shot(self, path: Path | None = None) -> Path:
        titles = self.window_titles()
        if not titles:
            raise InkError("no visible Inkscape (Claude) window found")
        path = path or (STATE / "renders" / f"window-{int(time.time() * 1000)}.png")
        subprocess.run(["screencapture", "-x", "-o", f"-l{titles[0][0]}", str(path)], check=True, timeout=30)
        self._prune_renders()
        return path

    def drawing_box(self, boxes: dict[str, Box]) -> Box:
        """Union of the top-level objects' corrected boxes (Inkscape's own drawing area can be wrong)."""
        return Box.union(boxes[i] for i in self._top_ids(boxes))

    def _top_ids(self, boxes) -> list[str]:
        t = self.trees[self.active_doc()]
        return [c.get("id") for c in t.visible_children(t.root) if c.get("id") in boxes and boxes[c.get("id")].w > 0]

    def export(self, path, *, area: str = "page", ids: list[str] | None = None, region_mm=None, dpi: float = 300.0,
               text_to_path: bool = False, background: str | None = None, only: bool = True,
               fmt: str | None = None) -> dict:
        """Write the live document to a file and read the file back. Returns path, size, facts, verdict.

        PNG is exported by the live Inkscape with an explicit area and pixel size. PDF/SVG/EPS/PS are not:
        Inkscape's export settings persist inside a running instance and some cannot be switched back
        (text-to-path, once on, stays on; the area mode cannot be unset), so a vector export writes the
        live document to a temporary copy, crops that copy to the wanted box, and has a fresh headless
        Inkscape export it."""
        p = Path(path).expanduser().resolve()
        fmt = (fmt or p.suffix.lstrip(".")).lower()
        if fmt not in EXPORT_FORMATS:
            raise InkError(f"format must be one of {EXPORT_FORMATS} (from the file suffix or format=)")
        self.ensure_tree()
        _, boxes = self.boxes()
        if ids:
            self._check_ids(ids, boxes)
            want, what = Box.union(boxes[i] for i in ids), f"{len(ids)} object(s)"
        elif region_mm:
            x0, y0, x1, y1 = region_mm
            want, what = Box(px(x0), px(y0), px(x1 - x0), px(y1 - y0)), "region"
        elif area in ("page", "drawing"):
            want, what = (self.page_box() if area == "page" else self.drawing_box(boxes)), area
        else:
            raise InkError("area is 'page' or 'drawing' (or pass ids / region_mm)")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.unlink(missing_ok=True)
        t0 = time.monotonic()
        w_px = max(1, round(want.w / 96 * dpi))
        if fmt == "png":
            acts = [("export-type", "png"), ("export-plain-svg", False), ("export-dpi", float(dpi)),
                    ("export-id", ",".join(ids) if (ids and only) else ""), ("export-id-only", bool(ids and only)),
                    ("export-area", f"{want.x:.3f}:{want.y:.3f}:{want.x2:.3f}:{want.y2:.3f}"),
                    ("export-width", w_px), ("export-height", 0)]
            acts += ([("export-background", background), ("export-background-opacity", 1.0)] if background
                     else [("export-background-opacity", 0.0)])
            self.run(acts + [("export-overwrite", True), ("export-filename", str(p)), ("export-do", None)])
            stderr = self.bus.last_stderr
        else:
            stderr = self._export_vector(p, fmt, None if what == "page" else want, ids if only else None,
                                         dpi, text_to_path)
        rejected, _ = inkscape_said(stderr)
        if not p.exists() or p.stat().st_size == 0:
            raise InkError(f"export to {p} failed" + (": " + " | ".join(rejected) if rejected else
                           f" (no file written; see {STATE / 'inkscape.err.log'})"))
        size = f"{p.stat().st_size / 1e6:.2f} MB"
        wmm, hmm = mm(want.w), mm(want.h)
        bad, facts, checked = [], fmt.upper(), True
        if fmt == "png":
            from PIL import Image
            with Image.open(p) as im:
                W, H = im.size
            facts = f"PNG {W}×{H} px ({wmm:.1f}×{hmm:.1f} mm at {dpi:g} dpi)"
            if abs(W - w_px) > 1 or abs(H - want.h / want.w * w_px) > 1.5:
                bad.append(f"image is {W}×{H} px, planned {w_px}×{round(want.h / want.w * w_px)}")
        elif fmt == "pdf" and (f := pdf_facts(p)) is not None:
            n, pw, ph, fonts = f
            facts = (f"PDF, {n} page{'s' if n != 1 else ''}, {pw:.1f}×{ph:.1f} mm, "
                     + ("fonts embedded" if fonts else "no fonts (text as outlines)" if text_to_path else "no fonts"))
            if abs(pw - wmm) > 0.15 or abs(ph - hmm) > 0.15:
                bad.append(f"page is {pw:.1f}×{ph:.1f} mm, the {what} measures {wmm:.1f}×{hmm:.1f} mm")
            if text_to_path and fonts:
                bad.append("fonts are still embedded although text_to_path was asked for")
        elif fmt == "svg":
            from lxml import etree
            sw, sh = page_size_px(etree.parse(str(p), etree.XMLParser(huge_tree=True)).getroot())
            facts = f"SVG, {mm(sw):.1f}×{mm(sh):.1f} mm"
            if abs(mm(sw) - wmm) > 0.15 or abs(mm(sh) - hmm) > 0.15:
                bad.append(f"document is {mm(sw):.1f}×{mm(sh):.1f} mm, the {what} measures {wmm:.1f}×{hmm:.1f} mm")
        else:
            checked = False
        if bad:
            verdict = "NOT LANDED as planned: " + "; ".join(bad)
        elif not checked or facts == "PDF":
            verdict = "Landed: unconfirmed — the size of this format is not read back."
        else:
            verdict = f"Landed: confirmed (size matches the {what} to 0.15 mm" + (" / 1 px)." if fmt == "png" else ").")
        return {"path": p, "size": size, "facts": facts, "verdict": verdict,
                "seconds": round(time.monotonic() - t0, 2)}

    def _export_vector(self, out: Path, fmt: str, box: Box | None, only_ids: list[str] | None, dpi: float,
                       text_to_path: bool) -> str:
        """Copy the live document to a temporary SVG (no reload), crop the copy to `box` (None keeps the
        page), hide everything but `only_ids`, and export it with a fresh headless Inkscape. Returns its stderr."""
        from lxml import etree
        tmpdir = STATE / "exports"
        tmpdir.mkdir(mode=0o700, exist_ok=True)
        tmp = tmpdir / f"export-{uuid.uuid4().hex}.svg"
        try:
            self.bridge([{"op": "dump", "path": str(tmp)}], dump=False)
            tree = etree.parse(str(tmp), etree.XMLParser(huge_tree=True))
            root = tree.getroot()
            src = self.info(self.active_doc()).get("file")
            href = "{%s}href" % NS["xlink"]
            for im in root.iter("{%s}image" % NS["svg"]):  # linked images must still resolve from the temp folder
                for key in (href, "href"):
                    v = im.get(key)
                    if src and v and not v.startswith(("data:", "http:", "https:", "file:", "/")):
                        im.set(key, str((Path(src).parent / v).resolve()))
            if box is not None:
                w_px, _ = page_size_px(root)
                vb = [float(v) for v in (root.get("viewBox") or "").replace(",", " ").split()] or [0.0, 0.0, w_px, 0.0]
                s = w_px / vb[2] if vb[2] else 1.0  # document px per user unit
                root.set("viewBox", f"{box.x / s + vb[0]:.6f} {box.y / s + vb[1]:.6f} {box.w / s:.6f} {box.h / s:.6f}")
                root.set("width", f"{mm(box.w):.6f}mm")
                root.set("height", f"{mm(box.h):.6f}mm")
                for nv in root.iter("{%s}namedview" % NS["sodipodi"]):  # extra pages would override the viewBox
                    for pg in list(nv.iter("{%s}page" % NS["inkscape"])):
                        pg.getparent().remove(pg)
            if only_ids:
                by_id = {e.get("id"): e for e in root.iter() if isinstance(e.tag, str) and e.get("id")}
                targets = [by_id[i] for i in only_ids if i in by_id]
                chain = {a for t in targets for a in t.iterancestors()}
                keep = chain | set(targets)
                for a in chain:
                    for c in a:
                        if isinstance(c.tag, str) and c not in keep and local(c) not in SKIP:
                            st = parse_style(c.get("style"))
                            st["display"] = "none"
                            c.set("style", ";".join(f"{k}:{v}" for k, v in st.items()))
            tree.write(str(tmp), xml_declaration=True, encoding="UTF-8")
            env = {k: v for k, v in os.environ.items() if k != "DBUS_SESSION_BUS_ADDRESS"}  # never reach a live instance
            cmd = [INKSCAPE_BIN, str(tmp), f"--export-type={fmt}", f"--export-filename={out}", "--export-area-page",
                   "--export-overwrite", f"--export-dpi={dpi:g}"] + (["--export-text-to-path"] if text_to_path else [])
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
            return r.stderr
        finally:
            tmp.unlink(missing_ok=True)

    def run_actions(self, acts, *, expected_revision: str | None = None) -> str:
        """Raw Inkscape actions, with Inkscape's own complaints surfaced and the geometric effect reported."""
        self.guarded_boxes(expected_revision)
        rev0 = self.revision
        out = self.run(acts)
        rejected, said = inkscape_said(self.bus.last_stderr)
        self.trees.pop(self.active_doc(), None)
        self.full_read()
        effect = (f"The document changed (revision {rev0} → {self.revision})." if self.revision != rev0 else
                  "Nothing in the document changed"
                  + ("" if self.content_tracked else " geometrically (only geometry is tracked here)")
                  + ". Expected for selection or export actions; if you expected a change, check the arguments: "
                  "Inkscape 1.4 ignores some malformed ones without a message (a bare or misspelled "
                  "object-align does nothing).")
        if rejected:
            raise InkError("Inkscape rejected an action: " + " | ".join(rejected)
                           + f"\nActions listed before it may have run. {effect}")
        return "\n".join(x for x in [out.strip(), ("Inkscape said: " + " | ".join(said)) if said else "", effect] if x)

    # ======================================================================== misc
    def action_catalogue(self) -> dict[str, str]:
        cache = STATE / "action-list.txt"
        if not cache.exists():
            r = subprocess.run([INKSCAPE_BIN, "--action-list"], capture_output=True, text=True, timeout=120)
            cache.write_text(r.stdout)
        out = {}
        for line in cache.read_text().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip()
        return out

    # ======================================================================== collaboration
    @staticmethod
    def _diff_lines(base: dict[str, Box], order: list[str], boxes: dict[str, Box], t: Tree | None,
                    top: int = 40, per_delta: int = 25) -> list[str]:
        """Added / removed / moved / resized objects between two box sets, grouped by shared delta."""
        added = [i for i in order if i not in base]
        removed = [i for i in base if i not in boxes]
        by_delta: dict[tuple, list[str]] = {}
        for i, b in boxes.items():
            a = base.get(i)
            if a is None:
                continue
            d = (round(mm(b.x - a.x), 2), round(mm(b.y - a.y), 2), round(mm(b.w - a.w), 2), round(mm(b.h - a.h), 2))
            if any(abs(v) >= 0.01 for v in d):
                by_delta.setdefault(d, []).append(i)

        def tops(ids):
            """Drop ids whose ancestor is in the same set (moving a group moves its contents)."""
            if t is None:
                return ids
            s = set(ids)
            out = []
            for i in ids:
                el = t.ids.get(i)
                if el is None or not any(a.get("id") in s for a in el.iterancestors()):
                    out.append(i)
            return out

        def name(i):
            el = t.ids.get(i) if t is not None else None
            return t.describe(el, boxes) if el is not None else f"#{i} @ {boxes[i].mm() if i in boxes else '?'}"

        lines = []
        if added:
            tp = tops(added)
            lines.append(f"ADDED {len(added)} objects ({len(tp)} top-level):")
            lines += [f"  + {name(i)}" for i in tp[:top]]
        if removed:
            lines.append(f"REMOVED {len(removed)} objects: " + ", ".join(removed[:top]))
        for d, ids in sorted(by_delta.items(), key=lambda kv: -len(kv[1])):
            tp = tops(ids)
            what = []
            if d[0] or d[1]:
                what.append(f"moved Δx {d[0]:+.2f}, Δy {d[1]:+.2f} mm")
            if d[2] or d[3]:
                what.append(f"resized Δw {d[2]:+.2f}, Δh {d[3]:+.2f} mm")
            lines.append(f"{' and '.join(what).upper()}: {len(tp)} top-level object(s) ({len(ids)} incl. contents)")
            lines += [f"  ~ {name(i)}" for i in tp[:per_delta]]
        return lines

    def changes(self, deep: bool = False, since_revision: str | None = None) -> str:
        """What changed in the live document since Claude last looked (or last edited), or since a
        given revision. Reports the current revision and makes it the new baseline.

        Geometry (added / removed / moved / resized objects) is always exact and cheap. With deep=True
        the tree is re-read and text and style differences are reported too (slow on big documents).
        """
        with self.lock:
            doc = self.active_doc()
            old_tree = self.trees.get(doc)
            order, boxes = self.full_boxes()
            rev = self.revision
            prev = since_revision or self.base_rev.get(doc)
            if since_revision:
                snap = self.snapshot(since_revision)
                if snap is None:
                    return (f"Revision {rev}. I have no record of revision {since_revision} in this session, "
                            "so I cannot diff against it; call changes() without since_revision.")
                base = snap[1]
            else:
                base = self.baseline.get(doc)
            self.baseline[doc] = dict(boxes)
            self.base_rev[doc] = rev
            head = f"Revision {rev}" + (f" (was {prev})" if prev and prev != rev else " (unchanged)" if prev else "") + "."
            if base is None:
                return (f"{head} First look at this document: {len(boxes)} objects. Baseline recorded; "
                        "call again later to see what changed.")
            t = None
            try:
                if deep:
                    t, _, _, _ = self.tree(refresh=True)
                else:
                    t = self.trees.get(doc)
            except Exception:
                t = None
            lines = self._diff_lines(base, order, boxes, t)
            if deep and old_tree is not None and t is not None and t is not old_tree:
                from .svgtree import text_of
                diffs = []
                for i, el in t.ids.items():
                    o = old_tree.ids.get(i)
                    if o is None or not isinstance(el.tag, str):
                        continue
                    if local(el) in ("text", "flowRoot") and text_of(el) != text_of(o):
                        diffs.append(f"  text #{i}: “{text_of(o)[:60]}” → “{text_of(el)[:60]}”")
                    if el.get("style") != o.get("style"):
                        diffs.append(f"  style #{i}: {o.get('style')} → {el.get('style')}"[:300])
                if diffs:
                    lines.append(f"TEXT/STYLE CHANGES ({len(diffs)}):")
                    lines += diffs[:60]
            if lines:
                body = "\n".join(lines)
            elif prev and prev != rev:
                body = ("No object moved, resized, appeared or vanished, but what they show changed (text, colour, "
                        "an image or a same-box transform)." + ("" if deep else " deep=True lists text and style edits."))
            else:
                body = "No changes." if self.content_tracked else (
                    "No geometric changes (only geometry is tracked on this document; deep=True compares text and style).")
            return head + "\n" + body
