"""Governance MCP server launcher (Phase 5/6/7/8 tools).

This module imports the shared ``app.mcp_server`` module, which now defines two
FastMCP instances:

* ``mcp``     — the 21 legacy Phase 1-4 "core" tools.
* ``mcp_gov`` — the 33 governance / lifecycle / embedding-timeout tools.

Launching this entry point serves only ``mcp_gov`` over stdio, so the host can
mount the lightweight core server for daily use and attach this governance
server on demand. Both share the same ``app.*`` backend and the same SQLite
database, so there is no duplicated logic and no divergent state.

Run:  python -m app.mcp_server_gov
"""
import sys
from pathlib import Path

# Inject project root so this module also launches correctly when run as a
# script (e.g. `python app/mcp_server_gov.py`) and not only as `python -m
# app.mcp_server_gov`. Without this, `from app import ...` fails because
# sys.path[0] becomes the `app/` directory instead of the project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import mcp_server

def _run_cli():
    """Console-script entry (registered in pyproject.toml as `bionic-agent-governance`)."""
    mcp_server._run(mcp_server.mcp_gov, "stdio")


if __name__ == "__main__":
    _run_cli()
