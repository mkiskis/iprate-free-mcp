# IPRATE Free Public MCP

Open-source adapter for IPRATE's four-tool, anonymous, read-only MCP endpoint:

```text
https://mcp.iprate.eu/mcp
```

The adapter exposes bounded selections from one completed IPRATE static release:

- `find_ip_representatives`
- `get_ip_representative_profile`
- `get_ip_market_snapshot`
- `get_iprate_coverage`

It has no database, ORM, SQL, database credential, application-API fallback, or private
IPRATE package dependency. Ratings and statistics are copied from released JSON assets;
they are never recomputed by the MCP service.

## Static input

Set `IPRATE_MCP_ASSET_ROOT` to a completed `/data/v1` directory containing
`manifest.json`, the released profile/cohort assets, and
`mcp/v0.1/catalog.json`. Set `IPRATE_MCP_ASSET_BASE_URL` to the public URL used in
source citations.

The catalogue is derived only from static profile assets:

```bash
iprate-free-mcp-build-catalog /path/to/data/v1
```

The builder fails if required indexes are missing, checksums differ, profile counts do
not match the release manifest, or a profile asset is unreadable. It never imports or
connects to IPRATE's database, API, models, or pipeline.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/ruff check .
```

Run the adapter against a compatible static release:

```bash
export IPRATE_MCP_ASSET_ROOT=/path/to/data/v1
export IPRATE_MCP_ASSET_BASE_URL=https://iprate.eu/data/v1
iprate-free-mcp
```

The local endpoint is `http://127.0.0.1:8000/mcp`.

Documentation: <https://iprate.eu/developers/mcp/>

## Licence

Adapter code is Apache-2.0. IPRATE data and website content have separate terms and
licences; this code licence does not grant rights to them or to IPRATE trademarks.
