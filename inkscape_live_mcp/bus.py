"""Talk to a running Inkscape over D-Bus (org.gtk.Actions), and read what it prints.

Inkscape publishes its actions on the bus only when launched with --app-id-tag, which makes it a
unique GApplication named org.inkscape.Inkscape.<TAG>. App-level actions (select, align, query,
export...) live on the application object; per-document actions (undo, redo, every extension) live
on <app>/document/<N>. Query actions print to Inkscape's stdout, which launch.sh sends to a log file
that OutLog tails.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import socket

from jeepney import DBusAddress, HeaderFields, new_method_call
from jeepney.auth import BEGIN, Authenticator
from jeepney.io.blocking import DBusConnection
from jeepney.wrappers import DBusErrorResponse, unwrap_msg

STATE = Path(os.environ.get("INKSCAPE_MCP_STATE", Path.home() / ".inkscape-mcp"))
SOCKET = STATE / "bus.sock"
TAG = os.environ.get("INKSCAPE_MCP_TAG", "claude")
APP_NAME = f"org.inkscape.Inkscape.{TAG}"
APP_PATH = f"/org/inkscape/Inkscape/{TAG}"
ACTIONS_IFACE = "org.gtk.Actions"


class NotRunning(RuntimeError):
    pass


class UnknownAction(LookupError):
    """Inkscape publishes no action of that name (a typo, or an extension that is not loaded)."""


class PendingCall:
    """A method call whose reply has not arrived yet, on a connection of its own.

    Inkscape answers Activate only once the action has finished (for an extension: after the
    document has been swapped for the extension's result), so the reply is the completion signal.
    """

    def __init__(self, conn: DBusConnection, serial: int):
        self.conn, self.serial = conn, serial

    def wait(self, timeout: float) -> bool:
        """True once the reply has arrived (raising if it is a D-Bus error); False on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            try:
                msg = self.conn.receive(timeout=left)
            except TimeoutError:
                return False
            if msg.header.fields.get(HeaderFields.reply_serial) == self.serial:
                self.close()
                unwrap_msg(msg)
                return True

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


class CallTimeout(TimeoutError):
    def __init__(self, call: PendingCall, timeout: float):
        super().__init__(f"no reply within {timeout:.0f} s")
        self.call = call


def open_connection(sock_path: Path, timeout: float = 2.0) -> DBusConnection:
    """jeepney's open_dbus_connection, minus the BSD SCM_CREDS message that macOS rejects (EINVAL).

    The bus authenticates EXTERNAL via the peer's uid, which macOS supplies without ancillary data.
    """
    sock = socket.socket(family=socket.AF_UNIX)
    sock.settimeout(timeout)
    try:
        sock.connect(str(sock_path))
        sock.send(b"\0")
        authr = Authenticator(enable_fds=False, inc_null_byte=False)
        for req in authr:
            sock.sendall(req)
            authr.feed(sock.recv(1024))
        sock.sendall(BEGIN)
    except BaseException:
        sock.close()
        raise
    sock.settimeout(None)
    conn = DBusConnection(sock, enable_fds=False)
    return conn


class OutLog:
    """Tail Inkscape's stdout log from a remembered offset."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = self._size()

    def _size(self) -> int:
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0

    def mark(self) -> None:
        self.offset = self._size()

    def read_new(self, settle: float = 0.03, timeout: float = 3.0) -> str:
        """Return text printed since the last mark; wait until the file stops growing."""
        if not self.path.exists():
            return ""
        t0 = time.monotonic()
        last = -1
        while time.monotonic() - t0 < timeout:
            size = self._size()
            if size == last:
                break
            last = size
            time.sleep(settle)
        size = self._size()
        if size < self.offset:  # log was rotated
            self.offset = 0
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            data = f.read(size - self.offset)
        self.offset = size
        return data.replace(b"\x00", b"").decode("utf-8", "replace")


class InkscapeBus:
    def __init__(self, socket: Path = SOCKET):
        self.socket = socket
        self.conn = None
        self._describe: dict[str, dict[str, str]] = {}
        self.out = OutLog(STATE / "inkscape.out.log")
        self.err = OutLog(STATE / "inkscape.err.log")
        self.last_stderr = ""  # what Inkscape wrote to stderr during the last run()

    # ---- connection -------------------------------------------------------------------------
    def connect(self):
        if self.conn is not None:
            return self.conn
        if not self.socket.exists():
            raise NotRunning(f"No bus socket at {self.socket}. Open 'Inkscape (Claude)' or call launch(path).")
        self.conn = open_connection(self.socket)
        return self.conn

    def reset(self):
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:
            pass
        self.conn = None
        self._describe.clear()

    def _call(self, dest, path, iface, method, sig=None, body=(), timeout=600.0):
        conn = self.connect()
        msg = new_method_call(DBusAddress(path, bus_name=dest, interface=iface), method, sig, body)
        try:
            return unwrap_msg(conn.send_and_get_reply(msg, timeout=timeout))
        except TimeoutError:
            # The call reached Inkscape and may still complete: never re-send it (a retried move would
            # apply twice). Drop the connection so its late reply cannot be mistaken for another's.
            self.reset()
            raise
        except (OSError, ConnectionError):
            self.reset()
            conn = self.connect()
            return unwrap_msg(conn.send_and_get_reply(msg, timeout=timeout))

    def running(self) -> bool:
        try:
            (has,) = self._call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                "NameHasOwner", "s", (APP_NAME,), timeout=5)
            return bool(has)
        except (NotRunning, OSError, ConnectionError, DBusErrorResponse):
            self.reset()
            return False

    def require(self):
        if not self.running():
            raise NotRunning(
                "Inkscape (Claude) is not running. Open it from ~/Applications/Inkscape (Claude).app "
                "or call launch(path).")

    def document_paths(self) -> list[str]:
        """Object paths of the open documents, e.g. ['/org/inkscape/Inkscape/claude/document/1']."""
        (xml,) = self._call(APP_NAME, f"{APP_PATH}/document", "org.freedesktop.DBus.Introspectable",
                            "Introspect", timeout=10)
        nums = sorted(int(n) for n in re.findall(r'<node name="(\d+)"', xml))
        return [f"{APP_PATH}/document/{n}" for n in nums]

    # ---- actions ----------------------------------------------------------------------------
    def _param_types(self, path: str) -> dict[str, str]:
        if path not in self._describe:
            (desc,) = self._call(APP_NAME, path, ACTIONS_IFACE, "DescribeAll", timeout=20)
            self._describe[path] = {name: info[1] for name, info in desc.items()}
        return self._describe[path]

    def action_names(self, path: str = APP_PATH) -> dict[str, str]:
        return dict(self._param_types(path))

    def activate(self, name: str, value=None, path: str = APP_PATH, timeout: float = 600.0):
        """Activate one action; `value` is coerced to the action's declared parameter type."""
        self._call(APP_NAME, path, ACTIONS_IFACE, "Activate", "sava{sv}", (name, self._params(name, value, path), {}),
                   timeout=timeout)

    def activate_tracked(self, name: str, value=None, path: str = APP_PATH, timeout: float = 600.0):
        """activate() on a connection of its own. If Inkscape has not answered within `timeout`,
        raises CallTimeout carrying the still-open call, so the caller can wait for the late reply."""
        params = self._params(name, value, path)
        self.connect()  # fail early with NotRunning if there is no bus
        conn = open_connection(self.socket)
        msg = new_method_call(DBusAddress(path, bus_name=APP_NAME, interface=ACTIONS_IFACE), "Activate",
                              "sava{sv}", (name, params, {}))
        serial = next(conn.outgoing_serial)
        conn.send_message(msg, serial=serial)
        call = PendingCall(conn, serial)
        if not call.wait(timeout):
            raise CallTimeout(call, timeout)

    def _params(self, name: str, value, path: str) -> list:
        types = self._param_types(path)
        if name not in types:
            self._describe.pop(path, None)  # extensions or documents may have appeared since
            types = self._param_types(path)
            if name not in types:
                raise UnknownAction(f"Inkscape has no action '{name}' on {path}")
        sig = types[name]
        if sig == "":
            params = []
        elif sig == "s":
            params = [("s", "" if value is None else str(value))]
        elif sig == "b":
            params = [("b", bool(value) if value is not None else True)]
        elif sig == "d":
            params = [("d", float(value))]
        elif sig == "i":
            params = [("i", int(value))]
        else:
            raise TypeError(f"action {name} takes unsupported parameter type '{sig}'")
        return params

    def run(self, actions, path: str = APP_PATH) -> str:
        """Run [(name, value), ...] in order and return everything Inkscape printed meanwhile."""
        self.out.mark()
        self.err.mark()
        for item in actions:
            name, value = (item, None) if isinstance(item, str) else (item[0], item[1] if len(item) > 1 else None)
            self.activate(name, value, path=path)
        out = self.out.read_new()
        self.last_stderr = self.err.read_new(settle=0.0, timeout=0.2)  # stdout has already settled
        return out
