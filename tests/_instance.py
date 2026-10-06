"""A private Inkscape instance for tests: a fresh tag, its own D-Bus bus, state folder and Inkscape profile
(whose only extension is this checkout's bridge), so a test can never reach another Inkscape window.

Import this module BEFORE inkscape_live_mcp: the package reads its environment at import time.
"""
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAG = "test" + uuid.uuid4().hex[:8]
TMP = Path(tempfile.mkdtemp(prefix="ilm-", dir="/tmp"))  # short: a unix socket path must fit in 104 bytes
STATE, PROFILE = TMP / "state", TMP / "profile"
(PROFILE / "extensions").mkdir(parents=True)
(PROFILE / "extensions" / "claude_bridge").symlink_to(ROOT / "extension")
ENV = {"INKSCAPE_MCP_TAG": TAG, "INKSCAPE_MCP_STATE": str(STATE), "INKSCAPE_PROFILE_DIR": str(PROFILE)}
os.environ.update(ENV)
sys.path.insert(0, str(ROOT))

from mcp.client.stdio import StdioServerParameters  # noqa: E402

from inkscape_live_mcp.client import Inkscape  # noqa: E402


def launch(doc: Path, wait: float = 120) -> Inkscape:
    subprocess.run([str(ROOT / "launch.sh"), str(doc)], check=True, timeout=60, env={**os.environ, **ENV})
    ink = Inkscape()
    t0 = time.monotonic()
    while time.monotonic() - t0 < wait:
        if ink.bus.running() and ink.bus.document_paths() and ink.window_titles():
            return ink
        time.sleep(0.5)
    raise SystemExit(f"the test instance did not come up within {wait:.0f} s")


def server_params() -> StdioServerParameters:
    # env is passed explicitly: an MCP stdio client gives a server only a minimal environment, so the
    # private tag would otherwise be dropped and the server would drive the default instance.
    return StdioServerParameters(command=sys.executable, args=[str(ROOT / "run_server.py")], env=ENV, cwd=str(ROOT))


def check_identity(status_text: str, ink: Inkscape, doc_name: str):
    """Abort unless the server reports exactly the instance we launched, showing only our document."""
    pid = ink.instance_pid()
    if f"pid {pid}" not in status_text or doc_name not in status_text or "Documents: 1" not in status_text:
        raise SystemExit(f"ABORT: the server is not driving the test instance (pid {pid}); nothing edited.")


def close(ink: Inkscape, keep: bool = False):
    if keep:
        print(f"\nKept open: tag {TAG}, pid {ink.instance_pid()}, state {STATE}")
        return
    pid = ink.instance_pid()
    if pid:
        subprocess.run(["kill", str(pid)])
    try:
        subprocess.run(["kill", (STATE / "bus.pid").read_text().strip()])
    except (FileNotFoundError, ValueError):
        pass
    time.sleep(1)
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
