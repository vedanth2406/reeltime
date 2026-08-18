"""A mock MCP server over stdio. No credentials, no network, no API key.

Stands in for a real MCP server in `mcp_agent.py`. Two things make it useful
as a fixture rather than just a toy:

* **Its tool set is configurable.** `MCP_EXAMPLE_TOOLS=extended` exposes an
  extra `delete_file` tool. Recording one run each way and diffing them is the
  point of the example: a server that changes what it offers changes what the
  agent can attempt, and `tape diff` should say so in as many words.
* **It records that it started.** If `MCP_EXAMPLE_SPAWN_LOG` names a file, the
  server appends a line to it at startup. Replay must never add a line to that
  file, because replay must never start the server.

Run it directly and it will sit waiting for JSON-RPC on stdin, which is what
an MCP client expects; you are meant to point `mcp_agent.py` at it instead.
"""

import os
import sys

try:
    from mcp.server import MCPServer
except ImportError:  # pragma: no cover - SDK 1.x spelling
    from mcp.server.fastmcp import FastMCP as MCPServer

FILES = {
    "notes.txt": "buy milk\ncall the bank\n",
    "invoice.pdf": "<binary, 70 KB>",
    "report.txt": "Q3 was fine.\n",
}

server = MCPServer("example-files", version="1.0.0")


@server.tool()
def list_files() -> list:
    """List the files available on this server."""
    return sorted(FILES)


@server.tool()
def read_file(path: str) -> str:
    """Read one file by name."""
    if path not in FILES:
        raise ValueError("no such file: {}".format(path))
    return FILES[path]


if os.environ.get("MCP_EXAMPLE_TOOLS") == "extended":

    @server.tool()
    def delete_file(path: str) -> str:
        """Delete a file. Present only in the extended tool set."""
        FILES.pop(path, None)
        return "deleted {}".format(path)


def main() -> None:
    spawn_log = os.environ.get("MCP_EXAMPLE_SPAWN_LOG")
    if spawn_log:
        # stdout is the JSON-RPC channel and must not be written to.
        with open(spawn_log, "a") as handle:
            handle.write("started pid={}\n".format(os.getpid()))
        sys.stderr.write("mcp_server: started (pid {})\n".format(os.getpid()))
    server.run()


if __name__ == "__main__":
    main()
