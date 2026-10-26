# SPDX-License-Identifier: Apache-2.0
"""Expose the existing Altium MCP tools over Streamable HTTP."""

import argparse
import socket

from eda_agent.config import get_config
from eda_agent.server import mcp
from mcp.server.transport_security import TransportSecuritySettings


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0", help="Listen address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=14700, help="TCP port (default: 14700)")
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="Additional hostname or IP accepted by MCP Host validation",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    get_config().ensure_workspace()
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    local_addresses = {"127.0.0.1", "192.168.0.33", "192.168.2.43", *args.allowed_host}
    local_addresses.update(socket.gethostbyname_ex(socket.gethostname())[2])
    if args.host != "0.0.0.0":
        local_addresses.add(args.host)
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{address}:{args.port}" for address in sorted(local_addresses)],
    )
    mcp.run(transport="streamable-http")
