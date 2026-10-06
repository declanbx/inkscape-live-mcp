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
  * REVISION — a fingerprint of Inkscape's live boxes (every object's id and visual box, in document
    order). Each read records it; each edit takes an optional expected_revision and refuses, changing
    nothing, if the person has moved, added, removed or reshaped anything since. Colour-only edits do
    not change it (Inkscape reports geometry, not paint).
  * LANDED — after an edit the touched objects' live boxes are compared with what the edit planned
    (exact positions, shared edges, equal gaps, new ids present, deleted ids gone), so a call reports
    "confirmed", "NOT LANDED" or "unconfirmed" (an edit with no geometric trace, e.g. a colour).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from .bus import STATE, TAG, CallTimeout, InkscapeBus, PendingCall, UnknownAction
from .svgtree import PX_PER_MM, Box, Tree, local, mm, page_size_px, parse_query_all, qname

LAUNCH = Path(__file__).resolve().parent.parent / "launch.sh"
INKSCAPE_BIN = os.environ.get("INKSCAPE_BIN", "/Applications/Inkscape.app/Contents/MacOS/inkscape")
BRIDGE_ACTION = "org.inkscape.claude.bridge"
EDGES_H = ("left", "hcenter", "right")
EDGES_V = ("top", "vcenter", "bottom")
NATIVE_ALIGN_TARGETS = ("first", "last", "biggest", "smallest", "page", "drawing", "selection")
TOL = 0.01 * PX_PER_MM          # "where planned" tolerance: 0.01 mm
SNAPSHOTS = 6                   # revisions kept in memory so a refusal can say what changed


class InkError(RuntimeError):
    pass


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

    def documents(self) -> list[str]:
        self.bus.require()
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
    def run(self, actions) -> str:
        with self.lock:
            self.bus.require()
            self._settle_pending()  # never read or act on a document an unfinished edit is about to replace
            try:
                return self.bus.run(actions)
            except TimeoutError:
                raise UncertainCompletion(
                    "UNCERTAIN COMPLETION: Inkscape did not answer this command in time (a dialog may be "
                    "open). It may still apply: look with window or changes before repeating it.") from None

    def raw_boxes(self) -> tuple[list[str], dict[str, Box]]:
        """Inkscape's own visual boxes (document px), in document order. Records the revision."""
        text = self.run(["query-all"])
        order, boxes = parse_query_all(text)
        self.revision = fingerprint(order, boxes)
        self.reads += 1
        self._snapshots[self.revision] = text
        self._snapshots.move_to_end(self.revision)
        while len(self._snapshots) > SNAPSHOTS:
            self._snapshots.popitem(last=False)
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
                return self.run(actions)
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
            what = "\n".join(lines) if lines else "(the difference is below 0.01 mm)"
        raise RevisionConflict(
            f"Refused, nothing was edited: the document changed since revision {expected_revision} "
            f"(it is now {self.revision}). Changes since then:\n{what}\n"
            f"Re-check the objects you meant to edit, then retry with expected_revision={self.revision}.")

    def guarded_boxes(self, expected_revision: str | None):
        """Read the live boxes, then apply the revision guard."""
        order, boxes = self.boxes()
        self.guard(expected_revision)
        return order, boxes

    # ======================================================================== bridge
    def _settle_pending(self, wait: float = 30.0):
        """A previous bridge call timed out: wait until Inkscape has finished it (its D-Bus reply comes
        only after the document holds the result) before sending anything that edits or reads it."""
        if not self._pending:
            return
        try:
            done = self._pending.wait(wait)
        except Exception:  # it finished with an error: it is no longer running either way
            done = True
        if done:
            self._pending = None
            self.trees.clear()  # it may have changed the document after all
            return
        raise UncertainCompletion(
            "UNCERTAIN COMPLETION: an earlier edit that did not finish in time is still running in "
            "Inkscape, so I have not sent another. Look at the window (a dialog may be open) or call "
            "changes; try again once it has finished.")

    def bridge(self, ops: list[dict], doc: str | None = None, dump: bool = True, timeout: float = 900) -> dict:
        """Run a batch through the bridge extension. Raises InkError if the batch failed (document
        untouched) and UncertainCompletion if Inkscape did not answer within `timeout` seconds."""
        with self.lock:
            self.bus.require()
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
                self._pending = e.call
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
            elif r.get("changed"):
                self.trees.pop(doc, None)
            return r

    def bridge_edit(self, ops: list[dict], before: dict[str, Box], expect_added=(), timeout: float = 900,
                    dump: bool = True) -> tuple[dict, Edit]:
        """A mutating bridge batch plus its landed verdict (before = boxes read before the batch)."""
        r = self.bridge(ops, dump=dump, timeout=timeout)
        _, after = self.boxes()
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
        """The document tree plus fresh boxes. Re-reads the tree when the set of ids has changed."""
        with self.lock:
            doc = self.active_doc()
            order, boxes = self.raw_boxes()
            t = self.trees.get(doc)
            if t is None or refresh or self.tree_ids.get(doc) != order:
                t = self._refresh_tree(doc, order)
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
    def style(self, ids, props: dict, *, expected_revision: str | None = None) -> Edit:
        _, before = self.guarded_boxes(expected_revision)
        self._check_ids(ids, before)
        removals = {k: v for k, v in props.items() if v is None}
        sets = {k: v for k, v in props.items() if v is not None}
        steps = 0
        if sets:
            self.with_selection(list(ids), [("object-set-property", f"{k},{v}") for k, v in sets.items()])
            steps += len(sets)
        if removals:
            r = self.bridge([{"op": "set_style", "ids": list(ids), "props": removals}])
            steps += 1 if r.get("changed") else 0
        doc = self.active_doc()
        t = self.trees.get(doc)
        if t is not None and sets:  # keep the cached tree honest about what we just changed
            from .svgtree import parse_style
            for i in ids:
                el = t.ids.get(i)
                if el is not None:
                    st = parse_style(el.get("style"))
                    st.update({k: str(v) for k, v in sets.items()})
                    el.set("style", ";".join(f"{k}:{v}" for k, v in st.items()))
        _, after = self.boxes()
        return self._done(steps, before, after, landed(
            before, after, touched=ids, unchanged="a colour or opacity change has no geometric trace, and "
                                                  "geometry is all Inkscape reports back."))

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
        with self.lock:
            for _ in range(max(1, int(n))):
                self.bus.activate(op, path=doc, timeout=900)
        self.trees.pop(doc, None)
        _, after = self.boxes()
        verdict = (f"Landed: confirmed (revision {rev0} → {self.revision})." if self.revision != rev0 else
                   "Landed: unconfirmed — no geometry changed (a colour-only step, or nothing to "
                   f"{op}).")
        return self._done(max(1, int(n)), before, after, verdict)

    # ======================================================================== renders
    def render(self, area: Box, width_px: int = 1600, only_ids: list[str] | None = None,
               background: str = "#ffffff", path: Path | None = None) -> Path:
        path = Path(path).expanduser().resolve() if path else (STATE / "renders" / f"render-{int(time.time() * 1000)}.png")
        path.unlink(missing_ok=True)
        acts = [("export-type", "png"), ("export-plain-svg", False),
                ("export-id", ",".join(only_ids) if only_ids else ""), ("export-id-only", bool(only_ids)),
                ("export-area", f"{area.x:.3f}:{area.y:.3f}:{area.x2:.3f}:{area.y2:.3f}"),
                ("export-width", int(width_px)), ("export-background", background),
                ("export-background-opacity", 1.0), ("export-overwrite", True),
                ("export-filename", str(path)), ("export-do", None)]
        self.run(acts)
        if not path.exists():
            raise InkError(f"render produced no file; see {STATE / 'inkscape.err.log'}")
        return path

    def window_shot(self, path: Path | None = None) -> Path:
        titles = self.window_titles()
        if not titles:
            raise InkError("no visible Inkscape (Claude) window found")
        path = path or (STATE / "renders" / f"window-{int(time.time() * 1000)}.png")
        subprocess.run(["screencapture", "-x", "-o", f"-l{titles[0][0]}", str(path)], check=True, timeout=30)
        return path

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
            order, boxes = self.boxes()
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
            body = "\n".join(lines) if lines else "No geometric changes." + (
                "" if deep else " (Text/colour-only edits need deep=True.)")
            return head + "\n" + body
