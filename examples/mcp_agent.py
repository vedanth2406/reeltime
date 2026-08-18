"""An agent that talks to an MCP server, recorded as MCP rather than as HTTP.

    tape run python examples/mcp_agent.py     # starts the server, records
    tape show last                            # mcp events, one line each
    tape show last 1                          # the tool set it discovered
    tape replay last                          # the server is NOT started

No API key and no network: `mcp_server.py` is a mock served over stdio.

The tool set the server offers is part of what it records, which is the whole
argument for a first-class `mcp` event. Record it twice, once each way::

    tape run python examples/mcp_agent.py
    MCP_EXAMPLE_TOOLS=extended tape run python examples/mcp_agent.py
    tape diff <first> <second>

and the diff names the change as a change -- `+ delete_file` on its own line --
rather than reporting that two opaque payloads differ somewhere.
"""

import asyncio
import os
import sys
from pathlib import Path

import reeltime as tape

SERVER = str(Path(__file__).resolve().parent / "mcp_server.py")


async def main():
    async with tape.mcp.connect(
        sys.executable, [SERVER],
        server="files",
        # Passed through so the server can log that it started. reeltime strips
        # its own recording variables out before the subprocess sees them, so a
        # server started by a recorded agent never records a run of its own.
        env=dict(os.environ),
    ) as session:
        listing = await session.list_tools()
        names = [tool.name for tool in listing.tools]
        print("tools offered: {}".format(", ".join(names)))

        files = await session.call_tool("list_files", {})
        print("files: {}".format(_text(files)))

        notes = await session.call_tool("read_file", {"path": "notes.txt"})
        print("notes.txt: {}".format(_text(notes).strip().replace("\n", " / ")))

        if "delete_file" in names:
            # Only reachable when the server offers it -- which is exactly the
            # kind of behaviour change a tool set difference causes.
            gone = await session.call_tool("delete_file", {"path": "invoice.pdf"})
            print("extended tool set: {}".format(_text(gone)))

        missing = await session.call_tool("read_file", {"path": "nope.txt"})
        print("error path: is_error={}".format(bool(getattr(missing, "isError", None)
                                                   or getattr(missing, "is_error", None))))


def _text(result):
    """The text content of a CallToolResult, whatever the SDK calls the field."""
    parts = [getattr(block, "text", "") for block in (result.content or [])]
    return ", ".join(part for part in parts if part)


if __name__ == "__main__":
    asyncio.run(main())
