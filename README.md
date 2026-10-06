# inkscape-live-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an AI agent (built for Claude Code) work in
**the Inkscape window you have open**, at the same time as you. Its edits appear in your window as they
happen and sit on Inkscape's normal undo stack, so Cmd+Z takes them back. Your edits are visible to the
agent the next time it looks, because it reads the live document, not a saved file.

macOS, Inkscape 1.4.

## Why

- **Shared, live editing.** No export–edit–reimport loop. You drag a panel; the agent sees it moved. The
  agent aligns six headers; you watch them snap into place and can undo it.
- **Exact layout by numbers.** Every coordinate is millimetres from the page's top-left corner, measured
  on the *visual* bounding box (stroke and glyph edges included) that Inkscape itself reports. "Align
  these to the left edge of that panel", "stack them 8 mm apart", "make this exactly 70 mm wide" are
  one call each, computed, not eyeballed; there is no render–nudge–render loop.
- **Safe to share a document.** Every read returns a *revision*; an edit given that revision refuses to
  run if you have changed the drawing since. Every edit reports whether it actually landed.

## Requirements

- macOS (tested on macOS 27, Apple Silicon) and **Inkscape 1.4** (tested on 1.4.4) in `/Applications`.
- [Homebrew](https://brew.sh) for `dbus` (the installer runs `brew install dbus` if needed).
- [uv](https://docs.astral.sh/uv/) (`brew install uv`).
- An MCP client. The installer registers the server with [Claude Code](https://claude.com/claude-code);
  any client that runs stdio servers works.

## Install

```sh
git clone https://github.com/declanbx/inkscape-live-mcp.git
cd inkscape-live-mcp
./install.sh
```

`install.sh` is idempotent (re-run it after pulling). It builds `.venv` with uv, links `extension/` into
Inkscape's user extensions folder, creates **`~/Applications/Inkscape (Claude).app`**, and runs
`claude mcp add --scope user inkscape …`. Flags: `--no-register` (skip the Claude Code registration),
`--no-app` (skip the launcher). For another client, register
`<checkout>/.venv/bin/python <checkout>/run_server.py` as a stdio server.

Optional, for Claude Code: `cp -r skills/inkscape-live ~/.claude/skills/` adds a skill that teaches the
agent the workflow (route by cost, lay out by numbers, share the window safely).

## Quick start

1. Open **Inkscape (Claude).app**, or drop an `.svg` on it. It is the same Inkscape with your usual
   preferences, started so that the agent can reach it. (A window opened the normal way is not
   reachable; see How it works.)
2. In Claude Code, ask in plain words: "align the six section headers to the left edge of the Summary
   panel", "space column 2's boxes 8 mm apart", "what did I change?", "make these the same width as the
   map" (with objects selected), "replace the title".
3. Save with Cmd+S as usual. The agent never saves over your file; it can write a copy (`save_copy`).

## Tools (server name `inkscape`)

| Reading | Layout: Inkscape's own commands, ~0.1–1 s, one undo step each | Content: one undo step per batch | Other |
|---|---|---|---|
| `status` `outline` `find` `inspect` `selection` `render` (with each object's box and id drawn on) `window` (screenshot of the Inkscape window) `changes` (what you moved, added, removed) | `align` `distribute` (equal or exact gaps) `move` (by or to mm) `resize` (exact visual width/height) `style` `attributes` `structure` (group, z-order, delete, duplicate, reparent) `history` (undo/redo) | `text` (set or find/replace, many at once) `insert` (.svg as live vector, .png/.jpg) `add_svg` (draw anything from SVG markup, in mm) `python` (arbitrary inkex code) | `actions` (any of Inkscape's ~1,070 actions, searchable) `save_copy` `launch` |

### Safety nets on every call

- **Revision guard.** Every read and edit ends with `revision <id>`, a fingerprint of every object's id
  and visual box in paint order, read live from Inkscape. Every editing tool takes an optional
  `expected_revision`; if the document no longer matches it, the tool refuses, changes nothing, and
  lists what changed since (moved/added/removed objects). `changes` reports the current revision and
  makes it the new baseline; `changes(since_revision=…)` diffs against any revision read this session.
  `history` takes it too, so an undo never takes back something you did after the agent's edit.
- **Landed verification.** After each edit the touched objects' live boxes are compared with what the
  edit planned: exact positions for moves and gaps, a shared edge for alignment, equal spacing for
  distribution, new ids present and deleted ids gone for inserts and deletes. The result says
  `Landed: confirmed`, `NOT LANDED as planned: …` (with what is where), or `Landed: unconfirmed` for an
  edit with no geometric trace, such as a colour.
- **No-op batches add no undo step.** The bridge compares the document before and after a batch; if it
  is byte-identical it hands nothing back, so Inkscape adds no undo entry and your next Cmd+Z still
  undoes the last real change.
- **Uncertain completion is reported, never retried.** If Inkscape does not answer in time (a dialog is
  open, a huge document), the call returns `UNCERTAIN COMPLETION` instead of an error or a silent retry:
  the edit may still apply. The next call waits for Inkscape to finish it before reading or editing.

## Cost model

Measured with `tests/bench.py` on a synthetic A0 document of 50,082 objects (15 MB, one 8 MB embedded
image), Apple Silicon laptop, through the MCP server:

| Operation | Time |
|---|---|
| status | 0.3 s |
| align / move / style (native) | 0.6–0.7 s |
| distribute five panels to an exact gap | 1.1 s |
| changes (geometry diff) | 0.3 s |
| find / outline (tree cached) | 0.25 s |
| first outline of an unedited file (read from disk) | 1.6 s |
| first outline of an edited document (via the bridge) | 1.7 s |
| render the whole page / one panel | 0.8 s / 0.4 s |
| bridge edit (text, add_svg, insert, python) | 3.0–3.2 s |
| bridge batch that changes nothing | 2.2 s |
| undo of a bridge edit | 1.2 s |

On a 2,000-object figure everything is under 0.6 s. Bridge edits grow with document size, because
Inkscape serialises the whole document to the extension and reloads its result: on a real 100,000-element
poster with heavily clipped figures one bridge edit took about 16 s. So **batch content edits**: `text`
takes a list, `add_svg` and `python` take many elements at once.

## How it works

- **Native route.** `launch.sh` starts a private D-Bus session bus (`~/.inkscape-mcp/bus.sock`) and opens
  Inkscape as the unique application `org.inkscape.Inkscape.claude` on it (`--app-id-tag=claude`).
  Inkscape then publishes its actions over `org.gtk.Actions`; `inkscape_live_mcp/bus.py` calls them and
  reads query output (`query-all`, `select-list`) from Inkscape's stdout, which `launch.sh` logs to a file.
- **Bridge route.** For what no action can do (text content, inserting markup or files, reparenting,
  arbitrary edits), `extension/claude_bridge.py` is an effect extension the server triggers over the same
  bus. It reads a request file, applies the whole batch atomically (if any operation fails, nothing
  changes), and hands the document back as one undo step.
- **Reading.** Boxes come from `query-all` on every call. The XML tree is cached and re-read only when the
  set of objects changes; when the window shows no unsaved changes it is read straight from the file.
- **Choosing an instance.** `INKSCAPE_MCP_TAG` (default `claude`) names the instance and
  `INKSCAPE_MCP_STATE` (default `~/.inkscape-mcp`) its state folder, so several instances can run side by
  side; the test suite uses this to run on its own instance.

## macOS quirks, and how they are handled

- **Launch constraints silently disable Python extensions** when Inkscape is exec'd from a shell, so no
  edit through the bridge would work. `launch.sh` therefore starts Inkscape through LaunchServices
  (`open -n -a … --env … --args --app-id-tag=…`).
- **GTK on macOS exports no `/window/N` objects** on D-Bus, only the application and `/document/N`
  (confirmed on 1.4.4 by the test suite). Every operation uses application-level actions plus
  `/document/N` for undo, redo and extensions; window-level actions such as save and zoom are left to you.
- **Open-file limit.** Inkscape 1.4.2 on macOS can crash the first time it runs an extension if it
  inherits a ~1M soft `RLIMIT_NOFILE`, common under IDEs and agent hosts (reported in
  [aravindev/inkscape_mcp#33](https://github.com/aravindev/inkscape_mcp/pull/33)). Launched through
  LaunchServices, Inkscape does not inherit the caller's limit: from a shell with 1,048,576 the extension
  sees 2,560 (Inkscape 1.4.4). The test suite asserts it stays at or below 4,096.
- **Wrong boxes for groups that contain a nested `<svg>`.** Inkscape 1.4 adds the nested viewBox size,
  in parent units, to the group's box (an inserted 70 mm legend is reported 105.8 mm wide). The server
  rebuilds those groups' boxes from their children and aligns them with computed moves rather than
  Inkscape's own Align.
- `select-by-id` **adds** to the selection: every command clears it first, then restores your selection.
- `export-area` cannot be cleared once set, so every render passes an explicit area.
- A saved copy keeps its source's internal document name, so the front window is matched by file name first.
- Two sessions driving one Inkscape would interleave on the bridge files and the stdout log, so every
  client call holds a cross-process lock (`~/.inkscape-mcp/client.lock`).

## Limitations

- **macOS only.** On Linux the same D-Bus mechanism works natively; see Related work.
- **The revision sees geometry, not paint.** A colour-only or attribute-only edit you make does not change
  the revision, and `Landed` cannot confirm such an edit. `changes(deep=True)` re-reads the document and
  reports text and style differences.
- The guard is a check, not a lock: an edit you make in the milliseconds between the check and the
  agent's edit is not caught.
- Bridge edits reload the whole document: seconds on large documents (see Cost model).
- Tools act on the **front** window. With several documents open, the front one is identified from the
  window title; click the window you want if it is ambiguous.
- Inkscape 1.4 only. Other versions are untested.

## Security

- No network access, no API keys, no telemetry. Everything runs on your machine.
- `python` (and `actions`) run arbitrary code inside Inkscape's Python as you, by design, with the same
  trust as the agent's own shell access. Run the server only for agents you would give a shell to.
- The bus is a private `dbus-daemon` whose socket lives in `~/.inkscape-mcp` (mode 0700, as are its
  bridge files and logs).

## Tests

```sh
.venv/bin/python tests/test_units.py   # no Inkscape needed
.venv/bin/python tests/e2e.py          # 47 checks over MCP on a private Inkscape instance
.venv/bin/python tests/bench.py        # the cost table above
```

`e2e.py` and `bench.py` launch their own Inkscape with a fresh tag, a private bus, a temporary state folder
and a throwaway Inkscape profile, check before any edit that the server is driving that instance, and
quit it afterwards (`--keep` leaves it open). They never touch a window you have open.

## Related work

- **[aravindev/inkscape_mcp](https://github.com/aravindev/inkscape_mcp)** is the closest prior art: the
  same mechanism, a running Inkscape driven over D-Bus plus inkex extensions, together with a headless
  CLI mode and a much broader tool surface, on Linux and Windows (macOS support is an open pull request).
  This project adds layout by Inkscape-measured visual boxes in millimetres (with the nested-`<svg>` box
  correction), exact align/distribute/resize tools, renders annotated with object ids, a change diff and
  revision guard for editing alongside a person, edit-landed verification, and macOS support today.
- [Shriinivas/inkmcp](https://github.com/Shriinivas/inkmcp): live control of a running Inkscape over
  D-Bus with an extension and inkex code execution (Linux).
- [tspspi/mcpinkscape](https://github.com/tspspi/mcpinkscape): an offline SVG backend plus an optional
  native C++ Inkscape extension for live editing with snapshots, polling and revision-conflict
  protection; the revision guard here follows its `expected_revision` idea.
- [P1oN/inkscape-mcp-server](https://github.com/P1oN/inkscape-mcp-server): a native Rust server with
  guarded, verified edits; the landed check, the "uncertain completion" result and the no-op-adds-no-undo
  rule here follow its design.

## Files

`launch.sh` (bus + Inkscape) · `install.sh` · `run_server.py` (entry point) · `inkscape_live_mcp/`
(`bus.py` D-Bus and stdout log, `client.py` operations, revision guard and landed checks, `svgtree.py`
tree and box model, `server.py` MCP tools) · `extension/` (`claude_bridge.inx/.py`) · `tests/`
(`_instance.py` private test instance, `e2e.py`, `bench.py`, `test_units.py`, `fixtures/`) ·
`skills/inkscape-live/SKILL.md` (a Claude Code skill for using the tools well).
State and logs: `~/.inkscape-mcp/` (`inkscape.out.log`, `inkscape.err.log`, `bridge/bridge.log`, renders).

## Troubleshooting

- Tools say "not running": open `Inkscape (Claude).app` (a normal Inkscape window is not reachable).
- `UNCERTAIN COMPLETION` or a stuck call: call `window` to see Inkscape; a dialog may be open.
- After updating, re-run `install.sh` and restart Inkscape (Claude), which reads extensions at startup.

## License

MIT, see [LICENSE](LICENSE).
