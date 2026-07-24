"""Command-line entry point (server + client subcommands).

Fleshed out in Phase 4; for now this is a minimal placeholder so that
``python -m modbus_sim`` and the ``modbus-sim`` console script resolve.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Program entry point. Returns a process exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    print("modbus-sim: CLI not implemented yet (see PLAN.md Phase 4)")
    return 0
