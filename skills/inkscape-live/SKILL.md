---
name: inkscape-live
description: Use when the user wants Claude to look at, edit, align, lay out or build something in Inkscape with them (a poster, figure layout, SVG they have open), or asks to change, place or check objects in a live .svg. Not for drawing a chart or figure itself (make it with a plotting library, then place it here).
---

# Inkscape, live and shared

The `inkscape` MCP server (inkscape-live-mcp) drives the user's open Inkscape window: edits appear as you
make them and sit on their Cmd+Z stack. Geometry is millimetres from the page's top-left, on visual boxes
read live from Inkscape. To drive it from a script instead, put the checkout on `sys.path`, run with its
`.venv/bin/python`, and use `from inkscape_live_mcp.client import Inkscape`.

## Start

1. `status`. Not running → `launch(path)` or ask the user to open `~/Applications/Inkscape (Claude).app`.
   Never exec the Inkscape binary from a shell: macOS then blocks Inkscape's Python and every
   text/insert edit fails silently.
2. Anything the user calls final, printed or submitted: copy it to a new working file first and open the
   copy. You never save the window's file; the user does (Cmd+S). Use `save_copy` for snapshots.
3. More than one window open → tools act on the FRONT window; confirm with `status` before editing.

## Share the window safely

- Every result ends with `revision <id>`. When the user is working in the window too, pass the revision
  you last read as `expected_revision` to each edit. A refusal means they changed something: read what
  (`changes`), re-plan, retry with the new revision. Never retry a refused edit blindly.
- Pass `expected_revision` to `history` (undo) as well, so you never undo the user's work.
- Read each edit's verdict: `Landed: confirmed` (done as planned), `NOT LANDED as planned` (look before
  doing anything else), `Landed: unconfirmed` (a colour or attribute change; render to check if it
  matters).
- `UNCERTAIN COMPLETION` means Inkscape did not answer in time: the edit may still apply. Do not repeat
  it; call `window` (a dialog may be open) or `changes`, then decide.
- The revision tracks geometry only: a colour-only change by the user does not alter it. `changes(deep=True)`
  reports text and style differences.

## Choose the route by cost (this decides every call)

| Route | Tools | Cost |
|---|---|---|
| native | align, distribute, move, resize (aspect kept), style, attributes, structure, history, selection | ~0.1–1 s, one undo step each |
| bridge (reloads the whole document) | text, insert, add_svg, python, reparent, stretch | ~0.5 s on a small file, 3–16 s on a big poster |
| read | find, inspect, outline, changes; render ~1 s | first read of an edited big file a few s, then cached |

So: batch every bridge edit into one call (`text` takes a list; `python`/`add_svg` take many elements).

Files out: `export(path, area="page"|"drawing" | ids=[…] | region_mm=[…], dpi=…, text_to_path=…)` writes
PDF/SVG/EPS/PS/PNG and reads the file back (page size in mm, pixels, fonts). Use it, not raw export actions:
Inkscape's export settings stick between calls inside a running instance. `text_to_path=True` for printers
that cannot take fonts.

## Lay out by numbers, look only for taste

Read boxes (`find`, `inspect`), compute the target, apply `align(to=<id> | to_mm=…)`, `distribute(gap_mm=…)`,
`move(x_mm=…, anchor=…)`; the verdict confirms the result, so no re-render is needed to check position.
Never nudge-render-repeat. `render(annotate="top" | ids)` maps what you see to ids; render again only to
judge balance and legibility. Copy an existing design's numbers exactly (`inspect(…, xml=True)` on its
panel, bar, title): a stroked rect's visible box is half a stroke wider than its x/y/width.

## Traps the tools already handle (don't fight them)

- Groups holding a nested `<svg>` report a wrong box to Inkscape; the tools rebuild it, so never run raw
  `object-align` on such groups through `actions`.
- Inserted SVGs get their ids prefixed: use the ids a call returns.
- Markup you write is XML: escape `<`, `&`. Multi-line text: the `<text>` needs the first line's x/y.
- Raw `actions` exports: absolute paths only; an export area, once set, stays set.

## Building a multi-section layout (poster, figure board)

Script it so it can be rebuilt from scratch in seconds: measure the existing design (panel widths,
gutters, header bars, text sizes) into constants; one function per element kind (panel, header, label,
text block with measured wrapping); a `LAYOUT` table of sections; a rebuild that deletes and re-creates
what it owns; and an audit that checks containment, overlaps and aligned edges from the live boxes.
Make figures small in your plotting library and place them at 1.8–2.7× so small print text lands at a
legible size.
