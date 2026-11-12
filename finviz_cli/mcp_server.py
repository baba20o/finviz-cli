"""An MCP server over the commands: `finviz-mcp`, speaking over stdin and stdout.

It is a wrapper and nothing more. The tools, their arguments and their descriptions are the
functions in agent.py; this file only hands them to the MCP SDK. Every tool reads data. None
writes outside the CLI's own data folder, none places a trade.

Needs the `mcp` extra (`pip install "finviz-cli[mcp]"`). Works with version 1 of the SDK
(FastMCP) and version 2 (MCPServer).
"""
from __future__ import annotations

import sys

from finviz_cli import agent

NAME = "finviz"


def _server_class():
    try:
        from mcp.server.mcpserver import MCPServer as Server           # mcp 2.x
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP as Server           # mcp 1.x
        except ImportError:
            raise SystemExit('finviz-mcp needs the MCP SDK: pip install "finviz-cli[mcp]"') from None
    return Server


def _read_only():
    """The hint that these tools only read. None is fine where the SDK has no such type."""
    try:
        from mcp.types import ToolAnnotations
        return ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
    except Exception:
        return None


class _TextToStderr:
    """Stands in for sys.stdout while the server runs under version 1 of the SDK, which takes
    `sys.stdout.buffer` for the protocol and leaves `sys.stdout` itself where it was. The
    protocol still gets the real stream, through `.buffer`. Anything written as text (a
    stray print, a library's debug output) goes to stderr instead of into the protocol.
    (Version 2 of the SDK does this itself.)"""

    def __init__(self, real) -> None:
        self.buffer = real.buffer
        self.encoding = getattr(real, "encoding", None) or "utf-8"
        self.errors = getattr(real, "errors", None) or "strict"

    def write(self, text: str) -> int:
        return sys.stderr.write(text)

    def writelines(self, lines) -> None:
        for line in lines:
            sys.stderr.write(line)

    def flush(self) -> None:
        sys.stderr.flush()

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def fileno(self) -> int:
        return self.buffer.fileno()


def build():
    """The server with every tool registered, not yet running."""
    server = _server_class()(NAME, instructions=agent.GUIDE)
    hint = _read_only()
    for tool in agent.TOOLS:
        server.tool(annotations=hint)(tool) if hint is not None else server.tool()(tool)
    return server


def main() -> None:
    """Entry point of `finviz-mcp`. stdout belongs to the protocol from here on."""
    if len(sys.argv) > 1:
        sys.stderr.write("finviz-mcp takes no arguments: it serves the finviz tools over stdin and stdout to an MCP client.\n")
        raise SystemExit(2)
    server = build()
    if type(server).__module__.startswith("mcp.server.fastmcp") and hasattr(sys.stdout, "buffer"):
        sys.stdout = _TextToStderr(sys.stdout)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
