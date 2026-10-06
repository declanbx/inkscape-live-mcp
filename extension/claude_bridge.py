#!/usr/bin/env python3
"""Claude bridge: apply a batch of edits, requested by the inkscape-live-mcp server, to the LIVE document.

Inkscape runs this as an effect extension when the server activates the document action
`org.inkscape.claude.bridge.noprefs`. It reads $INKSCAPE_MCP_STATE/bridge/request.json (default
~/.inkscape-mcp), applies every op in order, and writes response.json. The batch is atomic: if any op
raises, nothing is returned to Inkscape and the document is untouched. A batch that changes the
document is ONE undo step; a batch that leaves it byte-identical returns nothing, so Inkscape adds no
undo step at all (an empty result makes Inkscape skip the document swap).

Coordinates in requests are document pixels (96 px/in) measured from the page's top-left corner,
the same frame Inkscape's query-all reports and transform-translate uses.

Runs under Inkscape's bundled Python (3.10). Never writes to stderr: Inkscape shows stderr in a
modal dialog, so everything is logged to bridge.log and reported through response.json instead.
"""
import time as _time
_T0 = _time.time()
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import struct
import sys
import time
import traceback

STATE = os.path.join(os.path.expanduser(os.environ.get("INKSCAPE_MCP_STATE") or "~/.inkscape-mcp"), "bridge")
os.makedirs(STATE, mode=0o700, exist_ok=True)
REQ = os.path.join(STATE, "request.json")
RES = os.path.join(STATE, "response.json")
_LOG = open(os.path.join(STATE, "bridge.log"), "a", buffering=1)
sys.stderr = _LOG

import inkex  # noqa: E402
from inkex import Transform  # noqa: E402
from inkex.units import convert_unit  # noqa: E402
from lxml import etree  # noqa: E402

NS = {
    "svg": "http://www.w3.org/2000/svg",
    "xlink": "http://www.w3.org/1999/xlink",
    "inkscape": "http://www.inkscape.org/namespaces/inkscape",
    "sodipodi": "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd",
}
SVG = "{%s}" % NS["svg"]
ROLE = "{%s}role" % NS["sodipodi"]
XLINK_HREF = "{%s}href" % NS["xlink"]


def qname(key):
    """'inkscape:label' -> '{ns}label'; plain names unchanged."""
    if ":" in key and not key.startswith("{"):
        pre, local = key.split(":", 1)
        if pre in NS:
            return "{%s}%s" % (NS[pre], local)
    return key


def local(el):
    tag = el.tag if isinstance(el.tag, str) else ""
    return tag.split("}", 1)[-1]


def to_px(value, default=0.0):
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if value.strip().endswith("%"):
        return default
    return float(convert_unit(value, "px"))


def parse_style(s):
    out = {}
    for part in (s or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def style_str(d):
    return ";".join("%s:%s" % (k, v) for k, v in d.items())


# ---- coordinate frames -------------------------------------------------------------------------
def viewport_transform(el):
    """Transform a nested <svg> establishes for its children (x/y + viewBox + preserveAspectRatio)."""
    x, y = to_px(el.get("x")), to_px(el.get("y"))
    w, h = to_px(el.get("width"), None), to_px(el.get("height"), None)
    vb = el.get("viewBox")
    if not vb or not w or not h:
        return Transform(translate=(x, y))
    vx, vy, vw, vh = [float(v) for v in vb.replace(",", " ").split()]
    sx, sy = w / vw, h / vh
    par = (el.get("preserveAspectRatio") or "xMidYMid meet").split()
    align = par[0]
    tx, ty = x - vx * sx, y - vy * sy
    if align != "none":
        s = min(sx, sy) if (len(par) < 2 or par[1] == "meet") else max(sx, sy)
        sx = sy = s
        tx, ty = x - vx * s, y - vy * s
        if "xMid" in align:
            tx += (w - vw * s) / 2
        elif "xMax" in align:
            tx += w - vw * s
        if "YMid" in align:
            ty += (h - vh * s) / 2
        elif "YMax" in align:
            ty += h - vh * s
    return Transform((sx, 0, 0, sy, tx, ty))


class Frames:
    def __init__(self, svg):
        self.svg = svg
        vb = svg.get("viewBox")
        self.px_per_uu = float(svg.scale)
        vx, vy = (0.0, 0.0)
        if vb:
            vx, vy = [float(v) for v in vb.replace(",", " ").split()][:2]
        s = self.px_per_uu
        self.root = Transform((s, 0, 0, s, -vx * s, -vy * s))

    def content(self, el):
        """Map from el's CONTENT coordinate space (what its children live in) to document px."""
        chain = []
        node = el
        while node is not None and node is not self.svg:
            chain.append(node)
            node = node.getparent()
        t = self.root
        for node in reversed(chain):
            if not isinstance(node.tag, str):
                continue
            t = t @ Transform(node.get("transform"))
            if local(node) == "svg":
                t = t @ viewport_transform(node)
        return t

    def parent_space(self, el):
        """Map from the space el's own transform attribute is expressed in, to document px."""
        p = el.getparent()
        return self.root if (p is None or p is self.svg) else self.content(p)


# ---- image sizing --------------------------------------------------------------------------------
def image_size(path):
    with open(path, "rb") as f:
        head = f.read(26)
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", head[16:24])
        if head[:2] == b"\xff\xd8":
            f.seek(2)
            while True:
                marker, = struct.unpack(">H", f.read(2))
                seglen, = struct.unpack(">H", f.read(2))
                if 0xFFC0 <= marker <= 0xFFCF and marker not in (0xFFC4, 0xFFC8, 0xFFCC):
                    f.read(1)
                    h, w = struct.unpack(">HH", f.read(4))
                    return w, h
                f.seek(seglen - 2, 1)
    return None


MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
        ".webp": "image/webp", ".svg": "image/svg+xml"}


# ---- the bridge ----------------------------------------------------------------------------------
READ_ONLY_OPS = {"ping", "selected", "xml", "dump"}
# Elements that are never drawn themselves, and containers whose content is not drawn in place.
NOT_DRAWN = {"defs", "namedview", "metadata", "title", "desc", "style", "script", "clipPath", "mask",
             "marker", "pattern", "linearGradient", "radialGradient", "stop", "filter", "symbol",
             "path-effect", "flowRegion", "RDF"}
DRAWN_LEAVES = {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "image", "use", "text"}
LIST_CAP = 2000


def is_mutating(op):
    return op.get("op") not in READ_ONLY_OPS and not (op.get("op") == "exec" and op.get("readonly"))


def drawn_leaf(e):
    """True for a drawn shape/text/image outside defs-like containers and not display:none: an
    element the server can expect Inkscape's query-all to list once the edit has landed."""
    if local(e) not in DRAWN_LEAVES:
        return False
    for n in [e] + list(e.iterancestors()):
        if local(n) in NOT_DRAWN or n.get("display") == "none" or \
                parse_style(n.get("style")).get("display") == "none":
            return False
    return local(e) != "text" or bool("".join(e.itertext()).strip())


def named_ids(op):
    """Ids an op says it edits (for the server's did-it-land check)."""
    out = []
    for key in ("id", "ids"):
        v = op.get(key)
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, list):
            out.extend(i for i in v if isinstance(i, str))
    if isinstance(op.get("moves"), dict):
        out.extend(op["moves"])
    return out


class ClaudeBridge(inkex.EffectExtension):
    _mutated = False
    _out = None  # serialised result, reused by save() and the tree dump
    _ids = None  # every id in the document, kept current by unique_id (inkex's own cache misses
                 # elements parsed in from markup, so it would hand out an id already in use)

    def load(self, stream):
        # inkex's own load() deep-copies the whole tree so has_changed() can diff it afterwards; on a
        # 100k-element document that copy costs seconds. effect() instead hashes the serialised document
        # before and after a mutating batch (0.05 s at 100k elements) and reuses the bytes for save().
        t = time.time()
        document = inkex.load_svg(stream)
        self.svg = document.getroot()
        self.svg.selection.set(*self.options.ids)
        self._load_s = round(time.time() - t, 3)
        return document

    def effect(self):
        t0 = time.time()
        self._timing = {"startup_to_effect_s": round(t0 - _T0, 3), "parse_s": getattr(self, "_load_s", None)}
        resp = {"id": None, "ok": False, "results": [], "stdout": "", "error": None, "changed": False}
        try:
            with open(REQ) as f:
                req = json.load(f)
        except FileNotFoundError:
            resp["error"] = "no pending request"
            self._respond(resp)
            return False
        os.replace(REQ, REQ + ".taken")  # a stray menu click never replays an old request
        resp["id"] = req.get("id")
        self.frames = Frames(self.svg)
        ops = req.get("ops", [])
        mutating = any(is_mutating(op) for op in ops)
        root = self.document.getroot()
        if mutating:  # fingerprint the document so a batch that changes nothing adds no undo step
            before = hashlib.sha1(root.tostring()).digest()
            ids_before = self._idset()
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                for op in ops:
                    name = op.get("op")
                    fn = getattr(self, "op_" + str(name), None)
                    if fn is None:
                        raise ValueError("unknown op %r" % name)
                    resp["results"].append(fn(op))
            resp["ok"] = True
        except Exception:
            resp["error"] = traceback.format_exc(limit=6)
        resp["stdout"] = buf.getvalue()[-20000:]
        if resp["ok"] and mutating:
            self._out = root.tostring()
            self._mutated = hashlib.sha1(self._out).digest() != before
            resp["noop"] = not self._mutated
            ids_after = self._idset()
            added = [i for i in ids_after if i not in ids_before]
            removed = [i for i in ids_before if i not in ids_after]
            resp["added"], resp["removed"] = added[:LIST_CAP], removed[:LIST_CAP]
            resp["n_added"], resp["n_removed"] = len(added), len(removed)
            new = set(added)  # walk the tree: inkex's id lookup misses elements parsed in from markup
            resp["drawn_added"] = [e.get("id") for e in root.iter() if isinstance(e.tag, str)
                                   and e.get("id") in new and drawn_leaf(e)][:LIST_CAP]
            touched = []
            for op in ops:
                touched.extend(named_ids(op))
            for r in resp["results"]:  # replace_text returns the ids it changed
                if isinstance(r, list):
                    touched.extend(i for i in r if isinstance(i, str))
            resp["touched"] = [i for i in dict.fromkeys(touched) if i in ids_after][:LIST_CAP]
        if resp["ok"] and req.get("dump_to"):
            try:
                with open(req["dump_to"], "wb") as f:
                    f.write(self._out if self._out is not None else root.tostring())
                resp["dumped"] = req["dump_to"]
            except Exception:
                resp["dump_error"] = traceback.format_exc(limit=2)
        changed = resp["ok"] and self.has_changed(None)
        resp["changed"] = bool(changed)
        resp["elapsed_s"] = round(time.time() - t0, 3)
        resp["timing"] = self._timing
        self._respond(resp)
        return None if changed else False

    def has_changed(self, ret):
        # The base class compares documents and ignores `ret`; honour False so a failed batch
        # that half-edited the tree is never handed back to Inkscape (atomic batches).
        if ret is False:
            return False
        return self._mutated

    def save(self, stream):
        t = time.time()
        if self._out is not None:  # the exact bytes inkex would write, already serialised for the hash
            stream.write(self._out)
        else:
            super().save(stream)
        with open(os.path.join(STATE, "last_save_s.txt"), "w") as f:
            f.write("%.3f" % (time.time() - t))

    def _respond(self, resp):
        tmp = RES + ".tmp"
        with open(tmp, "w") as f:
            json.dump(resp, f, default=str)
        os.replace(tmp, RES)

    # ---- helpers ---------------------------------------------------------------------------------
    def _idset(self):
        return {e.get("id") for e in self.svg.iter() if isinstance(e.tag, str) and e.get("id")}

    def el(self, eid):
        found = self.svg.getElementById(eid, literal=True)
        if found is None:  # e.g. created from markup earlier in this batch
            found = next((e for e in self.svg.iter() if isinstance(e.tag, str) and e.get("id") == eid), None)
        if found is None:
            raise KeyError("no element with id %r" % eid)
        return found

    def els(self, ids):
        if isinstance(ids, str):
            ids = [ids]
        return [self.el(i) for i in ids]

    def unique_id(self, base):
        """An id no element has, reserved for the caller."""
        if self._ids is None:
            self._ids = self._idset()
        base = re.sub(r"[^A-Za-z0-9_.-]", "-", base) or "obj"
        name, n = base, 2
        while name in self._ids:
            name, n = "%s-%d" % (base, n), n + 1
        self._ids.add(name)
        return name

    def parent_for(self, op):
        """Explicit parent, else Inkscape's current layer, else the topmost layer, else the root."""
        pid = op.get("parent")
        if pid:
            return self.el(pid)
        nv = self.svg.find("{%s}namedview" % NS["sodipodi"])
        cur = nv.get(qname("inkscape:current-layer")) if nv is not None else None
        if cur:
            layer = self.svg.getElementById(cur, literal=True)
            if layer is not None and layer is not self.svg:
                return layer
        layers = [c for c in self.svg if isinstance(c.tag, str) and local(c) == "g"
                  and c.get(qname("inkscape:groupmode")) == "layer"]
        return layers[-1] if layers else self.svg

    def place_wrapper(self, parent, x_px, y_px, gid, label):
        """A <g> (id `gid`, already reserved) inside `parent` whose content space is document px with
        origin at (x_px, y_px)."""
        g = etree.SubElement(parent, SVG + "g")
        g.set("id", gid)
        if label:
            g.set(qname("inkscape:label"), label)
        p_to_px = self.frames.content(parent) if parent is not self.svg else self.frames.root
        t = (-p_to_px) @ Transform(translate=(x_px, y_px))
        g.set("transform", str(t))
        return g

    # ---- ops -------------------------------------------------------------------------------------
    def op_ping(self, op):
        return {"docname": self.svg.get(qname("sodipodi:docname")),
                "selected": [e.get("id") for e in self.svg.selection.values()],
                "px_per_user_unit": self.frames.px_per_uu,
                "file": os.environ.get("DOCUMENT_PATH"),
                "page_px": [to_px(self.svg.get("width"), None) or self.svg.viewbox_width * self.frames.px_per_uu,
                            to_px(self.svg.get("height"), None) or self.svg.viewbox_height * self.frames.px_per_uu],
                "n_elements": sum(1 for _ in self.svg.iter())}

    def op_selected(self, op):
        return [e.get("id") for e in self.svg.selection.values()]

    def op_xml(self, op):
        node = self.el(op["id"]) if op.get("id") else self.svg
        s = etree.tostring(node, encoding="unicode", pretty_print=True)
        s = re.sub(r'(data:[\w/+.-]+;base64,)[A-Za-z0-9+/=\s]{200,}', r'\1<…base64…>', s)
        mx = int(op.get("max_chars", 20000))
        return s if len(s) <= mx else s[:mx] + "\n<!-- truncated at %d of %d chars -->" % (mx, len(s))

    def op_dump(self, op):
        with open(op["path"], "wb") as f:
            f.write(etree.tostring(self.document, xml_declaration=True, encoding="UTF-8"))
        return op["path"]

    def op_set_attrs(self, op):
        for e in self.els(op["ids"]):
            for k, v in op["attrs"].items():
                if v is None:
                    e.attrib.pop(qname(k), None)
                else:
                    e.set(qname(k), str(v))
        return len(op["ids"]) if isinstance(op["ids"], list) else 1

    def op_set_style(self, op):
        for e in self.els(op["ids"]):
            st = parse_style(e.get("style"))
            for k, v in op["props"].items():
                if v is None:
                    st.pop(k, None)
                    e.attrib.pop(k, None)
                else:
                    st[k] = str(v)
                    e.attrib.pop(k, None)  # a presentation attribute would otherwise linger in the XML
            if st:
                e.set("style", style_str(st))
            else:
                e.attrib.pop("style", None)
        return True

    def op_set_text(self, op):
        e = self.el(op["id"])
        lines = str(op["text"]).split("\n")
        tag = local(e)
        if tag in ("tspan", "flowPara", "flowSpan", "textPath"):
            for c in list(e):
                e.remove(c)
            e.text = "\n".join(lines)
            return "set %s" % tag
        if tag == "flowRoot":
            paras = [c for c in e if local(c) == "flowPara"]
            template = paras[0] if paras else None
            for c in paras:
                e.remove(c)
            for line in lines:
                p = etree.SubElement(e, SVG + "flowPara")
                if template is not None:
                    for k, v in template.attrib.items():
                        if k != "id":
                            p.set(k, v)
                p.text = line
            return "flowRoot %d paras" % len(lines)
        if tag != "text":
            raise ValueError("%s is a <%s>, not text; text inside it: %s" % (
                op["id"], tag, [t.get("id") for t in e.iter(SVG + "text")][:10]))
        style = parse_style(e.get("style"))
        tspans = [c for c in e if local(c) == "tspan"]
        if "shape-inside" in style or "inline-size" in style:
            # SVG2 flowed text: Inkscape lays it out itself; lines are separate tspans
            template = tspans[0] if tspans else None
            for c in tspans:
                e.remove(c)
            e.text = None
            for i, line in enumerate(lines):
                t = etree.SubElement(e, SVG + "tspan")
                if template is not None:
                    for k, v in template.attrib.items():
                        if k not in ("id", "x", "y", "dx", "dy"):
                            t.set(k, v)
                if len(lines) > 1:
                    t.set(ROLE, "line")
                t.text = line
            return "flowed text %d lines" % len(lines)
        line_tspans = [c for c in tspans if c.get(ROLE) == "line"]
        if len(lines) == 1 and not line_tspans:
            if tspans:
                first = tspans[0]
                for c in list(first):
                    first.remove(c)
                first.text = lines[0]
                for c in tspans[1:]:
                    e.remove(c)
                e.text = None
            else:
                e.text = lines[0]
            return "single line"
        template = line_tspans[0] if line_tspans else (tspans[0] if tspans else None)
        x0 = e.get("x") if template is None else (template.get("x") or e.get("x"))
        y0 = e.get("y") if template is None else (template.get("y") or e.get("y"))
        for c in tspans:
            e.remove(c)
        e.text = None
        for i, line in enumerate(lines):
            t = etree.SubElement(e, SVG + "tspan")
            if template is not None:
                for k, v in template.attrib.items():
                    if k not in ("id", "y"):
                        t.set(k, v)
            t.set(ROLE, "line")
            if x0 is not None:
                t.set("x", x0)
            if i == 0 and y0 is not None:
                t.set("y", y0)
            t.set("id", self.unique_id("%s-line%d" % (op["id"], i + 1)))
            t.text = line
        return "%d lines (Inkscape spaces them by line-height)" % len(lines)

    def op_replace_text(self, op):
        old, new = op["old"], op["new"]
        scope = self.els(op["ids"]) if op.get("ids") else [self.svg]
        limit = op.get("count")
        hits = []
        for root in scope:
            for node in root.iter():
                if not isinstance(node.tag, str):
                    continue
                for attr in ("text", "tail"):
                    s = getattr(node, attr)
                    if s and old in s:
                        if attr == "tail" and node is root:
                            continue
                        setattr(node, attr, s.replace(old, new))
                        owner = node if attr == "text" else node.getparent()
                        hits.append(owner.get("id"))
                        if limit and len(hits) >= limit:
                            return hits
        if not hits:
            raise ValueError("text %r not found in a single text run (it may be split across tspans)" % old)
        return hits

    def op_create(self, op):
        parent = self.parent_for(op)
        e = etree.Element(SVG + op["tag"] if ":" not in op["tag"] else qname(op["tag"]))
        for k, v in (op.get("attrs") or {}).items():
            e.set(qname(k), str(v))
        if op.get("text") is not None:
            e.text = op["text"]
        if op.get("index") is None:
            parent.append(e)
        else:
            parent.insert(int(op["index"]), e)
        e.set("id", self.unique_id(op.get("id") or (e.get("id") or op["tag"])))
        if op.get("markup"):
            for child in etree.fromstring('<g xmlns="%s" xmlns:xlink="%s" xmlns:inkscape="%s" '
                                          'xmlns:sodipodi="%s">%s</g>' % (NS["svg"], NS["xlink"],
                                          NS["inkscape"], NS["sodipodi"], op["markup"])):
                e.append(child)
        return e.get("id")

    def op_create_markup(self, op):
        """Insert raw SVG markup (one or more elements). Ids that collide are renamed and every
        url(#…)/href="#…" inside the markup follows them. With x_px/y_px the markup's own
        coordinates are taken as `units` ('mm' or 'px') measured from that page point; without them
        the markup is written as-is in the parent's user units."""
        parent = self.parent_for(op)
        frag = etree.fromstring('<g xmlns="%s" xmlns:xlink="%s" xmlns:inkscape="%s" xmlns:sodipodi="%s">%s</g>'
                                % (NS["svg"], NS["xlink"], NS["inkscape"], NS["sodipodi"], op["markup"]))
        mapping = {}
        for node in frag.iter():
            if isinstance(node.tag, str) and node.get("id"):
                old = node.get("id")
                new = self.unique_id(old)
                while new in mapping.values():
                    new = self.unique_id(old + "-n")
                if new != old:
                    mapping[old] = new
                node.set("id", new)
        if mapping:
            url_re = re.compile(r"url\(\s*['\"]?#([^)'\"]+)['\"]?\s*\)")
            for node in frag.iter():
                if not isinstance(node.tag, str):
                    continue
                for k, v in list(node.attrib.items()):
                    if k in (XLINK_HREF, "href") and v.startswith("#") and v[1:] in mapping:
                        node.set(k, "#" + mapping[v[1:]])
                    elif "url(" in v:
                        node.set(k, url_re.sub(lambda m: "url(#%s)" % mapping.get(m.group(1), m.group(1)), v))
        place = None
        if op.get("x_px") is not None or op.get("y_px") is not None:
            s = 96 / 25.4 if op.get("units", "mm") == "mm" else 1.0
            p_to_px = self.frames.content(parent) if parent is not self.svg else self.frames.root
            place = (-p_to_px) @ Transform(translate=(op.get("x_px") or 0.0, op.get("y_px") or 0.0)) @ \
                Transform(scale=(s, s))
        made = []
        idx = op.get("index")
        for child in list(frag):
            if not isinstance(child.tag, str):
                continue
            if not child.get("id"):
                child.set("id", self.unique_id(local(child)))
            if place is not None:
                child.set("transform", str(place @ Transform(child.get("transform"))))
            if idx is None:
                parent.append(child)
            else:
                parent.insert(int(idx) + len(made), child)
            made.append(child.get("id"))
        return {"ids": made, "renamed": mapping}

    def op_delete(self, op):
        n = 0
        for e in self.els(op["ids"]):
            e.getparent().remove(e)
            n += 1
        return n

    def op_reparent(self, op):
        new_parent = self.el(op["parent"]) if op.get("parent") else self.svg
        keep = op.get("keep_position", True)
        moved = []
        for i, e in enumerate(self.els(op["ids"])):
            before = self.frames.parent_space(e)
            e.getparent().remove(e)
            if op.get("index") is None:
                new_parent.append(e)
            else:
                new_parent.insert(int(op["index"]) + i, e)
            if keep:
                after = self.frames.parent_space(e)
                e.set("transform", str((-after) @ before @ Transform(e.get("transform"))))
            moved.append(e.get("id"))
        return moved

    def op_translate(self, op):
        """moves: {id: [dx_px, dy_px]} in document px; each element moves exactly that far on the page."""
        for eid, (dx, dy) in op["moves"].items():
            e = self.el(eid)
            P = self.frames.parent_space(e)
            e.set("transform", str((-P) @ Transform(translate=(dx, dy)) @ P @ Transform(e.get("transform"))))
        return len(op["moves"])

    def op_scale(self, op):
        """Scale ids by (sx, sy) about a document-px anchor point."""
        ax, ay = op["anchor_px"]
        S = Transform(translate=(ax, ay)) @ Transform(scale=(op["sx"], op.get("sy", op["sx"]))) @ \
            Transform(translate=(-ax, -ay))
        for e in self.els(op["ids"]):
            P = self.frames.parent_space(e)
            e.set("transform", str((-P) @ S @ P @ Transform(e.get("transform"))))
        return True

    def op_set_transform(self, op):
        e = self.el(op["id"])
        if op.get("transform"):
            e.set("transform", op["transform"])
        else:
            e.attrib.pop("transform", None)
        return e.get("transform")

    def op_insert_svg(self, op):
        if op.get("path"):
            src = etree.parse(op["path"], etree.XMLParser(huge_tree=True, remove_blank_text=False)).getroot()
            default_name = os.path.splitext(os.path.basename(op["path"]))[0]
        else:
            src = etree.fromstring(op["markup"].encode(), etree.XMLParser(huge_tree=True))
            default_name = "inserted"
        if local(src) != "svg":
            raise ValueError("source root is <%s>, expected <svg>" % local(src))
        prefix = re.sub(r"[^A-Za-z0-9_-]", "-", op.get("id") or default_name)
        prefix = self.unique_id(prefix)
        # namespace every id so clipPaths/gradients/markers never collide with the poster's own
        ids = {n.get("id") for n in src.iter() if isinstance(n.tag, str) and n.get("id")}
        mapping = {i: "%s__%s" % (prefix, i) for i in ids}
        url_re = re.compile(r"url\(\s*['\"]?#([^)'\"]+)['\"]?\s*\)")

        def fix(s):
            return url_re.sub(lambda m: "url(#%s)" % mapping.get(m.group(1), m.group(1)), s)
        for n in src.iter():
            if not isinstance(n.tag, str):
                continue
            if n.get("id") in mapping:
                n.set("id", mapping[n.get("id")])
            for k, v in list(n.attrib.items()):
                if k in (XLINK_HREF, "href") and v.startswith("#") and v[1:] in mapping:
                    n.set(k, "#" + mapping[v[1:]])
                elif "url(" in v:
                    n.set(k, fix(v))
            if local(n) == "style" and n.text:
                n.text = fix(n.text)
        # natural size in px, and aspect from the viewBox
        vb = src.get("viewBox")
        nat_w = to_px(src.get("width"), None)
        nat_h = to_px(src.get("height"), None)
        if vb:
            vbv = [float(v) for v in vb.replace(",", " ").split()]
        else:
            vbv = [0, 0, nat_w or 100.0, nat_h or 100.0]
            src.set("viewBox", "%g %g %g %g" % tuple(vbv))
        nat_w = nat_w or vbv[2]
        nat_h = nat_h or vbv[3]
        aspect = vbv[3] / vbv[2]
        w, h = op.get("width_px"), op.get("height_px")
        if w and not h:
            h = w * aspect
        elif h and not w:
            w = h / aspect
        elif not w and not h:
            w, h = nat_w, nat_h
        for k in ("x", "y"):
            src.attrib.pop(k, None)
        src.set("width", "%.6g" % w)
        src.set("height", "%.6g" % h)
        src.set("preserveAspectRatio", op.get("preserve_aspect", "xMidYMid meet"))
        src.set("id", prefix + "__svg")
        parent = self.parent_for(op)
        g = self.place_wrapper(parent, op.get("x_px", 0.0), op.get("y_px", 0.0), prefix, op.get("label") or default_name)
        g.append(src)
        return {"group": g.get("id"), "svg": src.get("id"), "width_px": w, "height_px": h, "ids_prefixed": len(mapping)}

    def op_insert_image(self, op):
        path = os.path.abspath(op["path"])
        ext = os.path.splitext(path)[1].lower()
        size = image_size(path)
        w, h = op.get("width_px"), op.get("height_px")
        if size and (not w or not h):
            aspect = size[1] / size[0]
            if w and not h:
                h = w * aspect
            elif h and not w:
                w = h / aspect
            elif not w and not h:
                w, h = size[0] * 0.75, size[1] * 0.75  # 1 image px = 1 pt by default (96/128)
        if not w or not h:
            raise ValueError("cannot read the image size; give width_px and height_px")
        parent = self.parent_for(op)
        name = op.get("id") or os.path.splitext(os.path.basename(path))[0]
        g = self.place_wrapper(parent, op.get("x_px", 0.0), op.get("y_px", 0.0), self.unique_id(name),
                               op.get("label") or name)
        img = etree.SubElement(g, SVG + "image")
        img.set("id", self.unique_id(name + "__img"))
        img.set("x", "0")
        img.set("y", "0")
        img.set("width", "%.6g" % w)
        img.set("height", "%.6g" % h)
        img.set("preserveAspectRatio", "none")
        if op.get("embed", True):
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode()
            img.set(XLINK_HREF, "data:%s;base64,%s" % (MIME.get(ext, "image/png"), data))
        else:
            img.set(XLINK_HREF, path)
        return {"group": g.get("id"), "image": img.get("id"), "width_px": w, "height_px": h}

    def op_exec(self, op):
        """Arbitrary Python against the live document. Assign to `result` to return a value."""
        ns = {
            "svg": self.svg, "document": self.document, "inkex": inkex, "etree": etree, "Transform": Transform,
            "by_id": self.el, "selected": [e.get("id") for e in self.svg.selection.values()],
            "frames": self.frames, "to_px": to_px, "qname": qname, "NS": NS, "unique_id": self.unique_id,
            "parse_style": parse_style, "style_str": style_str, "result": None,
        }
        exec(compile(op["code"], "<claude-exec>", "exec"), ns)
        return ns.get("result")


if __name__ == "__main__":
    try:
        ClaudeBridge().run()
    except SystemExit:
        pass
    except Exception:
        traceback.print_exc(file=_LOG)
