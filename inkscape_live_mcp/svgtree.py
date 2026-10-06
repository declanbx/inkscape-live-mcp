"""Read-side model of a live Inkscape document: the XML tree (cached) joined to live bounding boxes.

Bounding boxes always come from Inkscape's query-all (visual boxes in document px, page top-left
origin). The XML tree is a cached copy and may lag the live document for style/text edits that
moved nothing; Inkscape.tree() refreshes it whenever the set of object ids changes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

NS = {
    "svg": "http://www.w3.org/2000/svg",
    "xlink": "http://www.w3.org/1999/xlink",
    "inkscape": "http://www.inkscape.org/namespaces/inkscape",
    "sodipodi": "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd",
}
LABEL = "{%s}label" % NS["inkscape"]
GROUPMODE = "{%s}groupmode" % NS["inkscape"]
ROLE = "{%s}role" % NS["sodipodi"]
PX_PER_MM = 96 / 25.4
SKIP = {"defs", "namedview", "metadata", "title", "desc", "style", "script", "clipPath", "mask",
        "marker", "pattern", "linearGradient", "radialGradient", "filter", "symbol", "path-effect",
        "flowRegion", "RDF"}
TEXTY = {"text", "flowRoot", "tspan", "flowPara", "flowSpan", "textPath"}


def qname(key: str) -> str:
    """'inkscape:label' -> '{namespace}label'; plain names unchanged."""
    if ":" in key and not key.startswith("{"):
        pre, name = key.split(":", 1)
        if pre in NS:
            return "{%s}%s" % (NS[pre], name)
    return key


def local(el) -> str:
    return el.tag.split("}", 1)[-1] if isinstance(el.tag, str) else ""


def mm(v_px: float) -> float:
    return v_px / PX_PER_MM


@dataclass
class Box:
    x: float  # px
    y: float
    w: float
    h: float

    @property
    def x2(self):
        return self.x + self.w

    @property
    def y2(self):
        return self.y + self.h

    def mm(self) -> str:
        return f"x {mm(self.x):.2f}, y {mm(self.y):.2f}, {mm(self.w):.2f}×{mm(self.h):.2f} mm"

    def mm_tuple(self):
        return tuple(round(mm(v), 3) for v in (self.x, self.y, self.w, self.h))

    def edge(self, name: str) -> float:
        return {"left": self.x, "right": self.x2, "hcenter": self.x + self.w / 2,
                "top": self.y, "bottom": self.y2, "vcenter": self.y + self.h / 2}[name]

    @staticmethod
    def union(boxes):
        boxes = list(boxes)
        x, y = min(b.x for b in boxes), min(b.y for b in boxes)
        x2, y2 = max(b.x2 for b in boxes), max(b.y2 for b in boxes)
        return Box(x, y, x2 - x, y2 - y)

    def overlaps(self, o: "Box") -> bool:
        return self.x < o.x2 and o.x < self.x2 and self.y < o.y2 and o.y < self.y2

    def inside(self, o: "Box") -> bool:
        return self.x >= o.x and self.y >= o.y and self.x2 <= o.x2 and self.y2 <= o.y2


def parse_query_all(text: str) -> tuple[list[str], dict[str, Box]]:
    order, boxes = [], {}
    for line in text.splitlines():
        parts = line.rsplit(",", 4)
        if len(parts) != 5:
            continue
        try:
            vals = [float(v) for v in parts[1:]]
        except ValueError:
            continue
        order.append(parts[0])
        boxes[parts[0]] = Box(*vals)
    return order, boxes


def text_of(el) -> str:
    tag = local(el)
    if tag in ("text", "flowRoot"):
        lines = [c for c in el if (local(c) == "tspan" and c.get(ROLE) == "line") or local(c) == "flowPara"]
        if lines:
            return "\n".join("".join(c.itertext()) for c in lines)
    return "".join(t for t in el.itertext() if t)


def parse_style(s: str | None) -> dict[str, str]:
    out = {}
    for part in (s or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def short(s: str, n: int = 70) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


@dataclass
class Tree:
    root: etree._Element
    source: str  # where the copy came from: 'bridge', 'disk' or 'export'
    ids: dict[str, etree._Element] = field(default_factory=dict)
    # Inkscape 1.4 reports a WRONG visual box for any group holding a nested <svg> (it unions the
    # nested viewBox size in parent units). Those groups, deepest first, get their box rebuilt from
    # their children; the nested <svg>'s own box is right.
    nested_svg_ancestors: list[str] = field(default_factory=list)

    @classmethod
    def from_file(cls, path, source: str) -> "Tree":
        root = etree.parse(str(path), etree.XMLParser(huge_tree=True, remove_blank_text=False)).getroot()
        t = cls(root=root, source=source)
        t.ids = {e.get("id"): e for e in root.iter() if isinstance(e.tag, str) and e.get("id")}
        anc: dict[str, int] = {}
        for e in root.iter("{%s}svg" % NS["svg"]):
            if e is root:
                continue
            for depth, a in enumerate(reversed(list(e.iterancestors()))):
                if a is not root and a.get("id") and local(a) not in SKIP:
                    anc[a.get("id")] = max(anc.get(a.get("id"), 0), depth)
        t.nested_svg_ancestors = sorted(anc, key=lambda i: -anc[i])
        return t

    def correct_boxes(self, boxes: dict[str, "Box"]) -> dict[str, "Box"]:
        for i in self.nested_svg_ancestors:
            el = self.ids.get(i)
            if el is None or i not in boxes:
                continue
            kids = []
            for c in el:
                if not isinstance(c.tag, str) or local(c) in SKIP:
                    continue
                st = parse_style(c.get("style"))
                if st.get("display") == "none" or c.get("display") == "none":
                    continue
                b = boxes.get(c.get("id"))
                if b is not None and (b.w > 0 or b.h > 0):
                    kids.append(b)
            if kids:
                boxes[i] = Box.union(kids)
        return boxes

    def label(self, el) -> str | None:
        return el.get(LABEL)

    def is_layer(self, el) -> bool:
        return local(el) == "g" and el.get(GROUPMODE) == "layer"

    def visible_children(self, el):
        return [c for c in el if isinstance(c.tag, str) and local(c) not in SKIP]

    def through_wrappers(self, el):
        """Follow chains of single-child groups/layers down to the first element that branches."""
        chain = [el]
        while True:
            kids = self.visible_children(chain[-1])
            if len(kids) == 1 and local(kids[0]) == "g":
                chain.append(kids[0])
            else:
                return chain

    def frontier(self, area_box, boxes, want=8, cap=80):
        """Objects at the shallowest level that shows at least `want` things overlapping area_box —
        what 'the parts of this region' means to a reader. Wrapper chains are skipped, leaves are
        kept as they are, largest first, at most `cap`."""
        containers = ("g", "svg", "a", "switch")

        def in_view(el):
            out = []
            for c in self.visible_children(el):
                b = boxes.get(c.get("id"))
                if b is not None and b.w * b.h > 0 and b.overlaps(area_box):
                    out.append(c)
            return out

        current = in_view(self.through_wrappers(self.root)[-1])
        for _ in range(8):
            if len(current) >= want:
                break
            expanded, grew = [], False
            for c in current:
                inner = self.through_wrappers(c)[-1] if local(c) in containers else c
                ks = in_view(inner) if local(inner) in containers else []
                if ks:
                    expanded.extend(ks)
                    grew = True
                else:
                    expanded.append(c)
            if not grew:
                break
            current = expanded
        current.sort(key=lambda e: -(boxes[e.get("id")].w * boxes[e.get("id")].h))
        return [e.get("id") for e in current[:cap]]

    def descendants_count(self, el) -> dict[str, int]:
        counts: dict[str, int] = {}
        for d in el.iterdescendants():
            if isinstance(d.tag, str) and local(d) not in SKIP:
                counts[local(d)] = counts.get(local(d), 0) + 1
        return counts

    def ancestors(self, el) -> list[str]:
        out = []
        for a in el.iterancestors():
            if a is self.root:
                break
            out.append(a.get(LABEL) or a.get("id") or local(a))
        return list(reversed(out))

    def describe(self, el, boxes: dict[str, Box], text_chars=70) -> str:
        tag = local(el)
        eid = el.get("id")
        parts = [("layer" if self.is_layer(el) else tag), f"#{eid}" if eid else "(no id)"]
        lab = el.get(LABEL)
        if lab and lab != eid:
            parts.append(f"[{lab}]")
        if tag in TEXTY:
            parts.append(f"“{short(text_of(el), text_chars)}”")
        elif tag == "image":
            href = el.get("{%s}href" % NS["xlink"]) or el.get("href") or ""
            parts.append("(embedded image)" if href.startswith("data:") else f"(linked: {short(href, 50)})")
        st = parse_style(el.get("style"))
        if st.get("display") == "none" or el.get("display") == "none":
            parts.append("HIDDEN")
        b = boxes.get(eid) if eid else None
        if b is not None:
            parts.append(f"@ {b.mm()}")
        return " ".join(parts)


def page_size_px(root) -> tuple[float, float]:
    def to_px(v):
        m = re.match(r"\s*([-+0-9.eE]+)\s*([a-z%]*)", v or "")
        if not m:
            return None
        num, unit = float(m.group(1)), m.group(2) or "px"
        return num * {"px": 1, "mm": PX_PER_MM, "cm": PX_PER_MM * 10, "in": 96, "pt": 96 / 72, "pc": 16,
                      "q": PX_PER_MM / 4}.get(unit, 1)
    w, h = to_px(root.get("width")), to_px(root.get("height"))
    vb = root.get("viewBox")
    if vb and (w is None or h is None):
        vx, vy, vw, vh = [float(v) for v in vb.replace(",", " ").split()]
        w, h = w or vw, h or vh
    return w or 0.0, h or 0.0
