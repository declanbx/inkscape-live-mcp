#!/bin/bash
# Set up inkscape-live-mcp from this checkout. Idempotent: re-run it after pulling changes.
#
#   ./install.sh                 all of the steps below
#   ./install.sh --no-register   skip registering the MCP server with Claude Code
#   ./install.sh --no-app        skip the launcher app
#
# Prerequisites: macOS, Inkscape 1.4 (default /Applications/Inkscape.app; else set INKSCAPE_BIN),
# Homebrew (to install dbus), and uv (https://docs.astral.sh/uv/).
#   1. dbus        the private message bus Inkscape publishes its actions on (brew install dbus)
#   2. .venv       the server's Python and its dependencies (pyproject.toml), built by uv
#   3. extension   links extension/ into Inkscape's user extensions folder
#                  ($INKSCAPE_PROFILE_DIR/extensions when INKSCAPE_PROFILE_DIR is set)
#   4. launcher    "Inkscape (Claude).app" in ~/Applications ($INKSCAPE_MCP_APPDIR to change the folder)
#   5. registration  `claude mcp add --scope user inkscape …`, so every Claude Code project gets the tools
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REGISTER=1
APPLET=1
for a in "$@"; do
  case "$a" in
    --no-register) REGISTER=0 ;;
    --no-app) APPLET=0 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $a (see --help)" >&2; exit 2 ;;
  esac
done
INK="${INKSCAPE_BIN:-/Applications/Inkscape.app/Contents/MacOS/inkscape}"
PROFILE="${INKSCAPE_PROFILE_DIR:-$HOME/Library/Application Support/org.inkscape.Inkscape/config/inkscape}"
APPDIR="${INKSCAPE_MCP_APPDIR:-$HOME/Applications}"
APP="$APPDIR/Inkscape (Claude).app"

[ "$(uname)" = Darwin ] || { echo "inkscape-live-mcp runs on macOS only (see README, Limitations)" >&2; exit 1; }
[ -x "$INK" ] || { echo "Inkscape not found at $INK: install Inkscape 1.4, or set INKSCAPE_BIN" >&2; exit 1; }
VERSION="$("$INK" --version 2>/dev/null | head -1)"
case "$VERSION" in "Inkscape 1.4"*) ;; *) echo "warning: built and tested on Inkscape 1.4.x; found '$VERSION'" >&2 ;; esac
command -v uv >/dev/null || { echo "uv is required: brew install uv (or see https://docs.astral.sh/uv/)" >&2; exit 1; }
if ! command -v dbus-daemon >/dev/null && [ ! -x /opt/homebrew/bin/dbus-daemon ]; then
  command -v brew >/dev/null || { echo "dbus is required: install Homebrew, then: brew install dbus" >&2; exit 1; }
  brew install dbus
fi

[ -x "$HERE/.venv/bin/python" ] || uv venv --python 3.12 "$HERE/.venv" -q
uv pip install --python "$HERE/.venv/bin/python" -q -r "$HERE/pyproject.toml"

mkdir -p "$PROFILE/extensions"
ln -sfn "$HERE/extension" "$PROFILE/extensions/claude_bridge"
chmod +x "$HERE/launch.sh"

if [ "$APPLET" = 1 ]; then
  mkdir -p "$APPDIR"
  TMP_AS="$(mktemp -t inkscape-claude).applescript"
  cat > "$TMP_AS" <<OSA
on run
	do shell script quoted form of "$HERE/launch.sh" & " > /dev/null 2>&1 &"
end run
on open theFiles
	set args to ""
	repeat with f in theFiles
		set args to args & " " & quoted form of POSIX path of f
	end repeat
	do shell script quoted form of "$HERE/launch.sh" & args & " > /dev/null 2>&1 &"
end open
OSA
  rm -rf "$APP"
  osacompile -o "$APP" "$TMP_AS"
  rm -f "$TMP_AS"
  ICNS="$(ls "${INK%/Contents/MacOS/*}"/Contents/Resources/*.icns 2>/dev/null | head -1 || true)"
  [ -n "$ICNS" ] && cp "$ICNS" "$APP/Contents/Resources/applet.icns"
  touch "$APP"
fi

if [ "$REGISTER" = 1 ]; then
  if command -v claude >/dev/null; then
    command claude mcp remove --scope user inkscape >/dev/null 2>&1 || true
    command claude mcp add --scope user inkscape -- "$HERE/.venv/bin/python" "$HERE/run_server.py"
  else
    echo "Claude Code not found; register the server with your MCP client as:"
    echo "  $HERE/.venv/bin/python $HERE/run_server.py"
  fi
fi
echo "Installed from $HERE."
[ "$APPLET" = 1 ] && echo "Open '$APP' (or drop an .svg on it); new Claude Code sessions get the inkscape tools."
exit 0
