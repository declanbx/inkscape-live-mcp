"""Entry point Claude Code launches: runs the inkscape MCP server over stdio from any working directory."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inkscape_live_mcp.server import main  # noqa: E402

main()
