"""CLI for deriving the MCP catalogue from a completed static release tree."""

from __future__ import annotations

import argparse
from pathlib import Path

from iprate.mcp.assets import build_catalog


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_root", type=Path, help="Completed /data/v1 release directory")
    parser.add_argument("--output", type=Path, help="Defaults to <release_root>/mcp/v0.1/catalog.json")
    args = parser.parse_args()
    catalog = build_catalog(args.release_root, args.output)
    counts = catalog["representative_counts"]
    print(
        f"Built static MCP catalogue for {catalog['release_id']}: "
        f"{counts['firms']} firms, {counts['attorneys']} attorneys"
    )


if __name__ == "__main__":
    main()
