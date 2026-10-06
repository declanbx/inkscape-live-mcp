#!/bin/bash
# Open Inkscape so an agent can drive it: start (or reuse) a private D-Bus session bus, then launch
# Inkscape as the unique app org.inkscape.Inkscape.<TAG> on that bus, with its stdout logged so query
# results can be read back. Files given as arguments open in the running instance if there is one.
#
#   INKSCAPE_MCP_TAG    instance name (default: claude) — a second tag is a second, separate instance
#   INKSCAPE_MCP_STATE  state folder: bus socket, logs, bridge files (default: ~/.inkscape-mcp)
#   INKSCAPE_PROFILE_DIR  Inkscape's own settings folder, passed through when set (tests use a
#                       throwaway profile so they never touch your preferences)
#   INKSCAPE_BIN        the inkscape binary (default: /Applications/Inkscape.app/Contents/MacOS/inkscape)
set -u
STATE="${INKSCAPE_MCP_STATE:-$HOME/.inkscape-mcp}"
TAG="${INKSCAPE_MCP_TAG:-claude}"
INK="${INKSCAPE_BIN:-/Applications/Inkscape.app/Contents/MacOS/inkscape}"
# the launcher app runs this with a minimal PATH, so also look where Homebrew installs (Apple / Intel)
DBUS_DAEMON="$(command -v dbus-daemon || ls /opt/homebrew/bin/dbus-daemon /usr/local/bin/dbus-daemon 2>/dev/null | head -1)"
[ -x "$DBUS_DAEMON" ] || { echo "dbus-daemon not found: brew install dbus" >&2; exit 1; }
DBUS_SEND="$(dirname "$DBUS_DAEMON")/dbus-send"
mkdir -p "$STATE" && chmod 700 "$STATE"
STATE="$(cd "$STATE" && pwd)"
SOCK="$STATE/bus.sock"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$SOCK"

bus_alive() { "$DBUS_SEND" --session --dest=org.freedesktop.DBus --type=method_call --print-reply \
  /org/freedesktop/DBus org.freedesktop.DBus.GetId >/dev/null 2>&1; }
ink_alive() { "$DBUS_SEND" --session --dest=org.freedesktop.DBus --type=method_call --print-reply \
  /org/freedesktop/DBus org.freedesktop.DBus.NameHasOwner string:"org.inkscape.Inkscape.$TAG" 2>/dev/null \
  | grep -q "boolean true"; }

if ! bus_alive; then
  rm -f "$SOCK"
  "$DBUS_DAEMON" --session --address="unix:path=$SOCK" --fork --print-pid=1 > "$STATE/bus.pid"
fi

if ink_alive; then
  # Hand the files to the running instance (GApplication forwards them and this process exits).
  [ $# -gt 0 ] && "$INK" --app-id-tag="$TAG" "$@" >/dev/null 2>&1
  exit 0
fi

for f in inkscape.out.log inkscape.err.log; do [ -f "$STATE/$f" ] && mv "$STATE/$f" "$STATE/$f.prev"; done
: > "$STATE/inkscape.out.log"; : > "$STATE/inkscape.err.log"
# Launch through LaunchServices, never by exec'ing the binary: macOS's launch constraints kill
# Inkscape's bundled Python (so every Python extension, the bridge included) when Inkscape's
# responsible process is a shell rather than Inkscape itself.
APP="${INK%/Contents/MacOS/*}"
ENVS=(--env DBUS_SESSION_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS" --env INKSCAPE_MCP_STATE="$STATE")
[ -n "${INKSCAPE_PROFILE_DIR:-}" ] && ENVS+=(--env INKSCAPE_PROFILE_DIR="$INKSCAPE_PROFILE_DIR")
ABS=()
for f in "$@"; do ABS+=("$(cd "$(dirname "$f")" && pwd)/$(basename "$f")"); done
open -n -a "$APP" "${ENVS[@]}" \
  --stdout "$STATE/inkscape.out.log" --stderr "$STATE/inkscape.err.log" \
  --args --app-id-tag="$TAG" ${ABS[@]+"${ABS[@]}"}
