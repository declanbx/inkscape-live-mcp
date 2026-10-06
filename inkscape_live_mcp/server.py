"""MCP server: drive a live Inkscape window (the 'Inkscape (Claude)' instance) as native tools."""
from __future__ import annotations

import functools
import io
import re
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont

from .bus import NotRunning, UnknownAction
from .client import Edit, InkError, Inkscape, UncertainCompletion, px
from .svgtree import LABEL, NS, SKIP, TEXTY, Box, local, mm, parse_style, short, text_of

NSMAP_REV = {k: "{%s}" % v for k, v in NS.items() if k != "svg"}

INSTRUCTIONS = """\
Live, shared control of the Inkscape window the user is working in. Edits appear in their window
immediately and land on Inkscape's undo stack (they can Cmd+Z you). All coordinates are millimetres
from the page's top-left corner, measured on VISUAL bounding boxes (stroke and text glyphs included),
read live from Inkscape — exact, so align/distribute/move are one-shot: never nudge-render-repeat.

Typical loop: status → outline / find (ids) → render(annotate=…) to SEE what the ids are →
align / distribute / move / resize / style → render(around=ids) to check taste, not position.
Collaborating: call `changes` to see what the user moved/added since you last looked, and
`selection` to act on what they have selected ("align these").

Every read and edit ends with `revision <id>`, a fingerprint of every object's id and box. Pass it
back as expected_revision to an edit and the edit is refused (nothing changes) if the user has moved,
added, removed or resized anything since — use it whenever the user is working in the window too.
Colour-only edits do not change the revision. Each edit also reports whether it LANDED: "confirmed"
(the live boxes match the plan), "NOT LANDED" (they do not: look before retrying) or "unconfirmed"
(a change with no geometric trace, e.g. a colour). "UNCERTAIN COMPLETION" means Inkscape did not answer
in time: never repeat that edit blindly; check with changes or render first.

Cost model: layout, style, attribute, z-order, group and delete tools use Inkscape's own commands
(0.1–1 s, one undo step each). text, insert, add_svg, reparent, non-uniform resize and python go through
an extension that reloads the whole document: ~0.5 s on a figure, ~3 s on 50k objects, up to ~16 s on a
large poster with clipped figures, one undo step per call — so BATCH them (text takes a list of edits).
Renders take ~1 s.
"""

mcp = MCPServer("inkscape", instructions=INSTRUCTIONS)
ink = Inkscape()


def _fmt_steps(steps: int) -> str:
    return f"{steps} undo step{'s' if steps != 1 else ''}"


def _report(title: str, ids, e: Edit) -> str:
    ids = list(ids)
    body = ink.report(ids[:40], e.before, e.after)
    more = f"\n  … and {len(ids) - 40} more" if len(ids) > 40 else ""
    return f"{title} ({_fmt_steps(e.steps)}):\n{body}{more}\n{e.landed}"


def revisioned(fn):
    """Every tool goes through this. It appends the document revision when the call read the live
    document, turns an uncertain completion into a result (the edit may well have applied, so it is not
    an error), and re-raises this package's errors as ToolError so their message reaches the model (MCP
    replaces any other exception with a bare "Error executing tool")."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        reads = ink.reads
        try:
            out = fn(*args, **kwargs)
        except UncertainCompletion as e:
            return str(e)
        except (InkError, NotRunning, UnknownAction) as e:
            raise ToolError(str(e)) from e
        if ink.reads == reads or not ink.revision:
            return out
        tag = f"revision {ink.revision}"
        if isinstance(out, str):
            return f"{out}\n{tag}"
        if isinstance(out, list) and out and isinstance(out[0], str):
            return [f"{out[0]}\n{tag}"] + out[1:]
        return out
    return wrapper


MAX_SIDE = 1900  # images over ~2000 px on a side are downscaled before Claude sees them


def _render_png(area: Box, width_px: int, only=None) -> PILImage.Image:
    if area.h > 0 and width_px * area.h / area.w > MAX_SIDE:
        width_px = int(MAX_SIDE * area.w / area.h)
    width_px = min(width_px, MAX_SIDE)
    path = ink.render(area, width_px=width_px, only_ids=only)
    img = PILImage.open(path).convert("RGB")
    return img


PALETTE = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#42d4f4", "#f032e6", "#9a6324",
           "#800000", "#469990", "#000075", "#808000", "#e6beff", "#ffd8b1", "#aaffc3", "#bfef45"]


def _annotate(img: PILImage.Image, area: Box, ids, boxes, tree=None) -> PILImage.Image:
    d = ImageDraw.Draw(img)
    sx = img.width / area.w
    sy = img.height / area.h
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", max(11, img.width // 110))
    except OSError:
        font = ImageFont.load_default()
    placed: list[tuple[float, float, float, float]] = []

    def collides(r):
        return any(r[0] < q[2] and q[0] < r[2] and r[1] < q[3] and q[1] < r[3] for q in placed)

    for k, i in enumerate(ids):
        b = boxes.get(i)
        if b is None or not b.overlaps(area):
            continue
        c = PALETTE[k % len(PALETTE)]
        x0, y0 = (b.x - area.x) * sx, (b.y - area.y) * sy
        x1, y1 = (b.x2 - area.x) * sx, (b.y2 - area.y) * sy
        d.rectangle([x0, y0, x1, y1], outline=c, width=2)
        lab = i
        if tree is not None and i in tree.ids and tree.ids[i].get(LABEL):
            lab = f"{i} [{tree.ids[i].get(LABEL)}]"
        tw = d.textlength(lab, font=font) + 6
        th = font.size + 4
        # label above the box; if taken, inside its top, then inside its bottom, then walk right
        candidates = [(x0, y0 - th), (x0, y0), (x0, y1 - th)]
        candidates += [(x0 + n * (tw + 4), y0 - th) for n in range(1, 6)]
        lx, ly = candidates[0]
        for cx, cy in candidates:
            cx, cy = min(max(0, cx), img.width - tw), min(max(0, cy), img.height - th)
            if not collides((cx, cy, cx + tw, cy + th)):
                lx, ly = cx, cy
                break
        else:
            lx, ly = min(max(0, lx), img.width - tw), min(max(0, ly), img.height - th)
        placed.append((lx, ly, lx + tw, ly + th))
        d.rectangle([lx, ly, lx + tw, ly + th], fill=c)
        d.text((lx + 3, ly + 1), lab, fill="white", font=font)
    return img


def _to_image(img: PILImage.Image) -> Image:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return Image(data=buf.getvalue(), format="png")


def _show(ids, margin_mm=8.0, width_px=1100):
    _, boxes = ink.boxes()
    ids = [i for i in ids if i in boxes]
    if not ids:
        return None
    b = Box.union(boxes[i] for i in ids)
    m = px(margin_mm)
    area = Box(b.x - m, b.y - m, b.w + 2 * m, b.h + 2 * m)
    return _to_image(_annotate(_render_png(area, width_px), area, ids, boxes))


def _with_show(text: str, ids, show: bool):
    if not show:
        return text
    img = _show(ids)
    return [text, img] if img is not None else text


# ============================================================================ session
@mcp.tool()
@revisioned
def status() -> str:
    """Is Inkscape (Claude) running; which documents are open; what is frontmost; what the user has selected."""
    if not ink.bus.running():
        return ("Inkscape (Claude) is NOT running. Ask the user to open '~/Applications/Inkscape (Claude).app' "
                "(or drop a file on it), or call launch(path).")
    docs = ink.documents()
    titles = ink.window_titles()
    lines = [f"Running (pid {ink.instance_pid()}). Documents: {len(docs)}"]
    for d in docs:
        inf = ink.docinfo.get(d)
        lines.append(f"  {d.rsplit('/', 1)[-1]}: " + (f"{inf['name']} ({inf.get('file')})" if inf else "(name not read yet)"))
    if titles:
        lines.append("Frontmost window: " + titles[0][1] + ("   ← unsaved changes" if titles[0][1].startswith("*") else ""))
    if docs:
        order, boxes = ink.boxes()
        sel = ink.selection()
        d0 = ink.active_doc() if len(docs) == 1 else None
        if d0 and (d0 in ink.trees or d0 in ink.docinfo):
            page = ink.page_box()
            lines.append(f"Page: {mm(page.w):.1f} × {mm(page.h):.1f} mm. Objects: {len(order)}.")
        else:
            lines.append(f"Objects: {len(order)}. (Page size and names load with the first outline/find.)")
        lines.append(f"User selection ({len(sel)}): " + (", ".join(f"{i} @ {boxes[i].mm()}" for i in sel[:15] if i in boxes) or "nothing"))
    return "\n".join(lines)


@mcp.tool()
@revisioned
def launch(path: str | None = None) -> str:
    """Start Inkscape (Claude) if needed and open `path` in it (an .svg; Inkscape also opens .pdf/.ai/.eps)."""
    r = ink.launch(path)
    return f"Ready after {r['seconds']} s; documents: {r['documents']}"


# ============================================================================ reading
@mcp.tool()
@revisioned
def outline(root: str | None = None, depth: int = 2, max_lines: int = 250, refresh: bool = False) -> str:
    """The object tree with ids, labels, text snippets and live bounding boxes (mm).

    root: start below this id (default: the whole page). depth: levels to expand; deeper content is
    summarised as counts. refresh: force re-reading the document (normally automatic)."""
    t, order, boxes, _ = ink.tree(refresh=refresh)
    start = t.ids.get(root) if root else t.root
    if start is None:
        raise InkError(f"no element #{root}")
    lines: list[str] = []

    def walk(el, lvl):
        for c in t.visible_children(el):
            if len(lines) >= max_lines:
                return
            chain = t.through_wrappers(c) if local(c) == "g" else [c]
            last = chain[-1]
            line = "  " * lvl + t.describe(c, boxes)
            if len(chain) > 1:
                line += "  › " + " › ".join(t.describe(x, {}) for x in chain[1:])
            if local(c) not in TEXTY and c.get("id") not in boxes:
                line += "  (empty/invisible)"
            kids = t.visible_children(last)
            if kids and (lvl + 1 >= depth or local(last) in TEXTY):
                if local(last) not in TEXTY:
                    counts = t.descendants_count(last)
                    top = ", ".join(f"{n} {k}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1])[:4])
                    line += f"   ⟶ {len(kids)} children ({top})"
                lines.append(line)
            else:
                lines.append(line)
                if kids:
                    walk(last, lvl + 1)
    walk(start, 0)
    head = f"Tree source: {t.source} copy; boxes live. {len(order)} objects in document."
    if len(lines) >= max_lines:
        lines.append(f"… truncated at {max_lines} lines — narrow with root=… or find(…)")
    return head + "\n" + "\n".join(lines)


@mcp.tool()
@revisioned
def find(text: str | None = None, label: str | None = None, id_pattern: str | None = None,
         tag: str | None = None, style_has: str | None = None, within_mm: list[float] | None = None,
         overlaps_mm: list[float] | None = None, top_level_only: bool = False, max_results: int = 60) -> str:
    """Find objects by any combination of: text content (case-insensitive substring), inkscape label,
    id regex, tag (text, g, rect, image, path…), a style substring (e.g. 'fill:#4b80ad'), or a page
    region [x0, y0, x1, y1] in mm (within = fully inside, overlaps = touches).
    top_level_only drops matches nested inside another match."""
    t, order, boxes, _ = ink.tree()
    rx = re.compile(id_pattern) if id_pattern else None
    win = Box(px(within_mm[0]), px(within_mm[1]), px(within_mm[2] - within_mm[0]), px(within_mm[3] - within_mm[1])) if within_mm else None
    ovl = Box(px(overlaps_mm[0]), px(overlaps_mm[1]), px(overlaps_mm[2] - overlaps_mm[0]), px(overlaps_mm[3] - overlaps_mm[1])) if overlaps_mm else None
    hits = []
    for i in order:
        el = t.ids.get(i)
        if el is None or local(el) in SKIP:
            continue
        if tag and local(el) != tag:
            continue
        if rx and not rx.search(i):
            continue
        if label and label.lower() not in (el.get(LABEL) or "").lower():
            continue
        if text and (local(el) not in TEXTY or text.lower() not in text_of(el).lower()):
            continue
        if text and local(el) == "tspan" and el.getparent() is not None and text.lower() in text_of(el.getparent()).lower():
            continue  # report the <text>, not each of its tspans
        if style_has and style_has.lower().replace(" ", "") not in (el.get("style") or "").lower().replace(" ", ""):
            continue
        b = boxes.get(i)
        if win and (b is None or not b.inside(win)):
            continue
        if ovl and (b is None or not b.overlaps(ovl)):
            continue
        hits.append(i)
    if top_level_only:
        s = set(hits)
        hits = [i for i in hits if not any(a.get("id") in s for a in t.ids[i].iterancestors())]
    out = [f"{len(hits)} match(es)" + (f"; first {max_results}" if len(hits) > max_results else "") + ":"]
    for i in hits[:max_results]:
        el = t.ids[i]
        path = " › ".join(t.ancestors(el)[-3:])
        out.append(f"  {t.describe(el, boxes)}" + (f"    in {path}" if path else ""))
    return "\n".join(out)


@mcp.tool()
@revisioned
def inspect(ids: list[str], xml: bool = False, max_chars: int = 6000) -> str:
    """Everything about specific objects: tag, label, where it sits in the tree, bounding box, every
    attribute (embedded images summarised), parsed style, full text, and its children. xml=True adds
    the raw XML (truncated to max_chars)."""
    from lxml import etree
    t, order, boxes, _ = ink.tree()
    out = []
    for i in ids:
        el = t.ids.get(i)
        if el is None:
            out.append(f"#{i}: not found")
            continue
        out.append(t.describe(el, boxes, text_chars=200))
        out.append("  path: " + " › ".join(t.ancestors(el) + [i]))
        for k, v in el.attrib.items():
            k2 = k
            for pre, uri in NSMAP_REV.items():
                k2 = k2.replace(uri, pre + ":")
            if v.startswith("data:"):
                v = f"<embedded {v[5:v.find(';')]} {len(v) / 1e6:.2f} MB>"
            if k == "style":
                continue
            out.append(f"  @{k2} = {short(v, 160)}")
        st = parse_style(el.get("style"))
        if st:
            out.append("  style: " + "; ".join(f"{k}: {v}" for k, v in st.items()))
        if local(el) in TEXTY:
            out.append("  text: " + repr(text_of(el)))
        kids = t.visible_children(el)
        if kids:
            out.append(f"  children ({len(kids)}):")
            out += ["    " + t.describe(c, boxes) for c in kids[:30]]
        if xml:
            s = etree.tostring(el, encoding="unicode", pretty_print=True)
            s = re.sub(r'(data:[\w/+.-]+;base64,)[A-Za-z0-9+/=\s]{200,}', r"\1<…>", s)
            out.append(s[:max_chars] + ("\n<!-- truncated -->" if len(s) > max_chars else ""))
    return "\n".join(out)


@mcp.tool()
@revisioned
def selection(set_ids: list[str] | None = None) -> str:
    """Read what the user currently has selected (ids + boxes), or replace the selection with set_ids
    (useful to show the user which objects you mean)."""
    if set_ids is not None:
        ink.select(set_ids)
    sel = ink.selection()
    _, boxes = ink.boxes()
    if not sel:
        return "Nothing selected."
    return f"{len(sel)} selected:\n" + "\n".join(f"  {i} @ {boxes[i].mm()}" for i in sel if i in boxes)


@mcp.tool()
@revisioned
def render(region_mm: list[float] | None = None, around: list[str] | None = None, margin_mm: float = 5.0,
           width_px: int = 1400, only: bool = False, annotate: list[str] | str | None = None,
           background: str = "#ffffff"):
    """Render the LIVE document to an image you can see.

    region_mm [x0, y0, x1, y1] or around=[ids] (+margin) choose the area; default is the whole page.
    only=True hides everything except the `around` objects. annotate draws each object's box and id
    on the image: pass a list of ids, 'children:<id>' for that object's children, or 'top' for the
    main parts in view (the shallowest level showing several objects, wrappers skipped) — the
    fastest way to learn which id is which."""
    t = None
    _, boxes = ink.boxes()
    if around:
        missing = [i for i in around if i not in boxes]
        if missing:
            raise InkError(f"unknown ids {missing}")
        b = Box.union(boxes[i] for i in around)
        m = px(margin_mm)
        area = Box(b.x - m, b.y - m, b.w + 2 * m, b.h + 2 * m)
    elif region_mm:
        area = Box(px(region_mm[0]), px(region_mm[1]), px(region_mm[2] - region_mm[0]), px(region_mm[3] - region_mm[1]))
    else:
        area = ink.page_box()
    img = _render_png(area, width_px, only=around if (only and around) else None)
    ann_ids: list[str] = []
    if annotate:
        t, order, boxes, _ = ink.tree()
        if isinstance(annotate, list):
            ann_ids = annotate
        elif annotate.startswith("children:"):
            el = t.ids.get(annotate.split(":", 1)[1])
            ann_ids = [c.get("id") for c in t.visible_children(el)] if el is not None else []
        elif annotate in ("top", "auto"):
            ann_ids = t.frontier(area, boxes)
        img = _annotate(img, area, ann_ids, boxes, t)
    note = (f"Area x {mm(area.x):.1f}–{mm(area.x2):.1f}, y {mm(area.y):.1f}–{mm(area.y2):.1f} mm; "
            f"{img.width}×{img.height} px ({img.width / mm(area.w):.2f} px/mm)."
            + (f" Annotated {len(ann_ids)} objects." if annotate else ""))
    return [note, _to_image(img)]


@mcp.tool()
@revisioned
def window():
    """Screenshot of the Inkscape window itself (the user's view: zoom, selection handles, any open
    dialog). Use when something seems stuck or to see what the user is looking at."""
    p = ink.window_shot()
    img = PILImage.open(p).convert("RGB")
    if img.width > 1800:
        img = img.resize((1800, int(img.height * 1800 / img.width)))
    return ["Inkscape window:", _to_image(img)]


@mcp.tool()
@revisioned
def changes(deep: bool = False, since_revision: str | None = None) -> str:
    """What the user changed since you last looked or edited (or since `since_revision`): objects
    added, removed, moved or resized (exact, cheap). Reports the current revision and makes it the new
    baseline. deep=True also re-reads the document to report text and style edits (slow on posters)."""
    return ink.changes(deep=deep, since_revision=since_revision)


# ============================================================================ layout (native, fast)
@mcp.tool()
@revisioned
def align(ids: list[str], edges: str, to: str = "first", to_mm: float | None = None, as_group: bool = False,
          show: bool = False, expected_revision: str | None = None):
    """Align objects exactly. edges: 'left' | 'hcenter' | 'right' | 'top' | 'vcenter' | 'bottom', or one
    of each ('left top'). to: an object id to align to (it stays put), or first | last | biggest |
    smallest | page | drawing | selection. to_mm: align that edge to a page coordinate instead.
    as_group moves the ids as one block. expected_revision: refuse if the document changed since."""
    e = ink.align(ids, edges, to=to, to_mm=to_mm, as_group=as_group, expected_revision=expected_revision)
    return _with_show(_report(f"Aligned {edges} to {to_mm if to_mm is not None else to}", ids, e), ids, show)


@mcp.tool()
@revisioned
def distribute(ids: list[str], axis: str = "y", gap_mm: float | None = None, mode: str = "gap",
               start_mm: float | None = None, show: bool = False, expected_revision: str | None = None):
    """Space objects along x or y. Without gap_mm: Inkscape's equal distribution between the outermost
    objects (mode gap | centers | left | right | top | bottom). With gap_mm: stack them in their
    current order with exactly that gap, starting at the first object (or at start_mm)."""
    e = ink.distribute(ids, axis=axis, gap_mm=gap_mm, mode=mode, start_mm=start_mm,
                       expected_revision=expected_revision)
    return _with_show(_report(f"Distributed along {axis}", ids, e), ids, show)


@mcp.tool()
@revisioned
def move(ids: list[str], dx_mm: float = 0.0, dy_mm: float = 0.0, x_mm: float | None = None,
         y_mm: float | None = None, anchor: str = "top-left", each: bool = False, show: bool = False,
         expected_revision: str | None = None):
    """Move by (dx_mm, dy_mm) and/or place so the anchor point ('top-left', 'center', 'bottom-right',
    'top-center'…) of the ids' joint box lands at (x_mm, y_mm). each=True places every object separately."""
    e = ink.move(ids, dx_mm, dy_mm, x_mm, y_mm, anchor, each, expected_revision=expected_revision)
    return _with_show(_report("Moved", ids, e), ids, show)


@mcp.tool()
@revisioned
def resize(ids: list[str], width_mm: float | None = None, height_mm: float | None = None,
           scale: float | None = None, keep_aspect: bool = True, anchor: str = "top-left", each: bool = False,
           show: bool = False, expected_revision: str | None = None):
    """Resize to an exact visual width or height (or by a scale factor), keeping the anchor corner fixed.
    keep_aspect=False with both width_mm and height_mm stretches (goes through the slower extension)."""
    e = ink.resize(ids, width_mm, height_mm, scale, keep_aspect, anchor, each, expected_revision=expected_revision)
    return _with_show(_report("Resized", ids, e), ids, show)


# ============================================================================ appearance
@mcp.tool()
@revisioned
def style(ids: list[str], props: dict[str, str | None], show: bool = False, expected_revision: str | None = None):
    """Set CSS style properties on objects, e.g. {"fill": "#4B80AD", "font-size": "11pt", "stroke-width":
    "0.5", "opacity": "0.8"}. A null value removes the property. Applies to groups too (children inherit
    unless they override — inspect them if a colour does not take)."""
    e = ink.style(ids, props, expected_revision=expected_revision)
    return _with_show(f"Styled {len(ids)} object(s) with {props} ({_fmt_steps(e.steps)}).\n{e.landed}", ids, show)


@mcp.tool()
@revisioned
def attributes(ids: list[str], attrs: dict[str, str | None], expected_revision: str | None = None) -> str:
    """Set (or with null, remove) XML attributes: 'inkscape:label', 'width', 'rx', 'xlink:href', 'id'…"""
    e = ink.attributes(ids, attrs, expected_revision=expected_revision)
    return f"Set {attrs} on {len(ids)} object(s) ({_fmt_steps(e.steps)}).\n{e.landed}"


# ============================================================================ content (bridge, batched)
@mcp.tool()
@revisioned
def text(edits: list[dict] | None = None, replace: list[dict] | None = None, show: bool = False,
         expected_revision: str | None = None):
    """Change text in ONE undoable batch. edits: [{"id": text id, "text": "new text; \\n for new lines"}]
    keeps the first run's style. replace: [{"old": "…", "new": "…", "ids": optional scope}] swaps a
    substring inside existing runs, preserving mixed formatting (bold words, superscripts)."""
    ops = [{"op": "set_text", "id": e["id"], "text": e["text"]} for e in (edits or [])]
    ops += [{"op": "replace_text", "old": r["old"], "new": r["new"], "ids": r.get("ids"), "count": r.get("count")}
            for r in (replace or [])]
    if not ops:
        raise InkError("give edits and/or replace")
    _, before = ink.guarded_boxes(expected_revision)
    r, e = ink.bridge_edit(ops, before)
    ids = [e_["id"] for e_ in (edits or [])] + [h for res in r["results"][len(edits or []):] for h in (res or [])]
    return _with_show(_report(f"Text changed in {r['seconds']} s", ids, e), ids, show)


@mcp.tool()
@revisioned
def insert(path: str, x_mm: float = 0.0, y_mm: float = 0.0, width_mm: float | None = None,
           height_mm: float | None = None, parent: str | None = None, id: str | None = None,
           label: str | None = None, embed: bool = True, show: bool = True, expected_revision: str | None = None):
    """Place a figure file into the document with its top-left at (x_mm, y_mm), scaled to width_mm or
    height_mm (aspect kept). .svg is inlined as editable vector (ids namespaced so nothing collides);
    .png/.jpg are embedded (embed=False links the file instead)."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise InkError(f"no such file {p}")
    op = {"x_px": px(x_mm), "y_px": px(y_mm), "parent": parent, "id": id, "label": label,
          "width_px": px(width_mm) if width_mm else None, "height_px": px(height_mm) if height_mm else None}
    if p.suffix.lower() == ".svg":
        op.update(op="insert_svg", path=str(p))
    elif p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        op.update(op="insert_image", path=str(p), embed=embed)
    else:
        raise InkError("insert takes .svg, .png, .jpg, .gif or .webp (convert PDFs to SVG first)")
    _, before = ink.guarded_boxes(expected_revision)
    r, e = ink.bridge_edit([op], before)
    g = r["results"][0]["group"]
    msg = (f"Inserted {p.name} as #{g} in {r['seconds']} s: {e.after[g].mm() if g in e.after else '?'} "
           f"({_fmt_steps(e.steps)}).\n{e.landed}")
    return _with_show(msg, [g], show)


@mcp.tool()
@revisioned
def add_svg(markup: str, x_mm: float | None = None, y_mm: float | None = None, units: str = "mm",
            parent: str | None = None, index: int | None = None, show: bool = True,
            expected_revision: str | None = None):
    """Draw anything by writing SVG markup: shapes, lines, arrows, text, groups, gradients.
    With x_mm/y_mm the markup's coordinates are `units` ('mm' or 'px') from that page point, so
    '<rect width="40" height="10" rx="2" fill="#151B36"/>' at x_mm=20 is a 40×10 mm bar at 20 mm.
    Without them the markup is written as-is in the parent's own user units. Colliding ids are renamed."""
    op = {"op": "create_markup", "markup": markup, "parent": parent, "index": index, "units": units}
    if x_mm is not None or y_mm is not None:
        op.update(x_px=px(x_mm or 0.0), y_px=px(y_mm or 0.0))
    _, before = ink.guarded_boxes(expected_revision)
    r, e = ink.bridge_edit([op], before)
    res = r["results"][0]
    rows = "\n".join(f"  {i} @ {e.after[i].mm()}" for i in res["ids"] if i in e.after)
    msg = f"Added {len(res['ids'])} element(s) in {r['seconds']} s ({_fmt_steps(e.steps)}):\n{rows}"
    if res.get("renamed"):
        msg += f"\nRenamed to avoid collisions: {res['renamed']}"
    return _with_show(msg + "\n" + e.landed, res["ids"], show)


@mcp.tool()
@revisioned
def structure(op: str, ids: list[str], parent: str | None = None, index: int | None = None,
              new_id: str | None = None, label: str | None = None, expected_revision: str | None = None) -> str:
    """Rearrange objects. op: delete | duplicate | group | ungroup | pop_out (out of its group) |
    raise | lower | top | bottom (z-order) | clone | unclone | reparent (move into `parent` at
    `index`, keeping the visual position). group takes optional new_id and label."""
    e = ink.structure(op, ids, parent=parent, index=index, new_id=new_id, label=label,
                      expected_revision=expected_revision)
    rows = "\n".join(f"  {i} @ {e.after[i].mm()}" for i in e.ids[:30] if i in e.after)
    return f"{op} done ({_fmt_steps(e.steps)}). Resulting/selected objects:\n{rows or '  (none)'}\n{e.landed}"


@mcp.tool()
@revisioned
def history(op: str = "undo", n: int = 1, expected_revision: str | None = None) -> str:
    """Undo or redo n steps in the live document (the same stack as the user's Cmd+Z). Pass
    expected_revision so an undo never takes back something the user did after your edit."""
    e = ink.history(op, n, expected_revision=expected_revision)
    return f"{op} ×{n} done.\n{e.landed}"


# ============================================================================ escape hatches
@mcp.tool()
@revisioned
def actions(run: list | None = None, search: str | None = None, expected_revision: str | None = None) -> str:
    """Run any of Inkscape's ~1,070 actions, or search them. run: ["select-all", ["object-align",
    "left page"], ["transform-rotate", 90]] — returns what Inkscape printed. search: substring over
    action names and descriptions. Most act on the current selection; use selection(set_ids=…) first."""
    if search:
        cat = ink.action_catalogue()
        s = search.lower()
        hits = [f"  {k}: {v}" for k, v in cat.items() if s in k.lower() or s in v.lower()]
        return f"{len(hits)} action(s):\n" + "\n".join(hits[:120])
    if not run:
        raise InkError("give run or search")
    acts = [(a, None) if isinstance(a, str) else (a[0], a[1] if len(a) > 1 else None) for a in run]
    if expected_revision:
        ink.guarded_boxes(expected_revision)
    out = ink.run(acts)
    ink.trees.pop(ink.active_doc(), None)
    ink.raw_boxes()  # report the revision after whatever the actions did
    return out.strip() or "(done, nothing printed)"


@mcp.tool()
@revisioned
def python(code: str, readonly: bool = False, timeout_s: float = 900, expected_revision: str | None = None) -> str:
    """Run Python (inkex) against the live document in one undoable step — anything the other tools
    cannot express. In scope: svg (root element), by_id(id), selected (ids), inkex, etree, Transform,
    frames (frames.content(el) maps an element's coordinates to page px), to_px('3mm'), qname('inkscape:label'),
    unique_id(base), parse_style/style_str. print() output and `result = …` come back. readonly=True
    skips the document reload (fast inspection). timeout_s: give up waiting after this long (the
    result is then UNCERTAIN COMPLETION: the code may still finish and apply)."""
    op = {"op": "exec", "code": code, "readonly": readonly}
    if readonly:
        r = ink.bridge([op], dump=False, timeout=timeout_s)
        head = "read-only"
    else:
        _, before = ink.guarded_boxes(expected_revision)
        r, e = ink.bridge_edit([op], before, timeout=timeout_s)
        head = ("changed: 1 undo step" if r["changed"] else "no change, no undo step") + "\n" + e.landed
    out = (r.get("stdout") or "").strip()
    res = r["results"][0]
    return (f"({r['seconds']} s, {head})\n" + (out + "\n" if out else "")
            + (f"result = {res!r}" if res is not None else ""))


@mcp.tool()
@revisioned
def save_copy(path: str) -> str:
    """Write the live document (unsaved changes included) to `path` as Inkscape SVG. Does not change
    which file the window is editing; the user saves the real file with Cmd+S."""
    p = Path(path).expanduser().resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    r = ink.bridge([{"op": "dump", "path": str(p)}], dump=False)
    return f"Wrote {p} ({p.stat().st_size / 1e6:.1f} MB) in {r['seconds']} s."


def main():
    mcp.run()


if __name__ == "__main__":
    main()
