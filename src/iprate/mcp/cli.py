"""Development CLI for the public adapter."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    """Serve the adapter with its Streamable HTTP transport."""
    uvicorn.run(
        "iprate.mcp.server:mcp_http_app",
        host=os.environ.get("IPRATE_MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("IPRATE_MCP_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
