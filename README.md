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
they are never recomputed by the MCP service. Anonymous calls are rate limited in memory
(HTTP 429 with `Retry-After`; results are never silently truncated), and `GET /healthz`
reports whether one internally consistent release is servable.

## Static input

Set `IPRATE_MCP_ASSET_ROOT` to a completed `/data/v1`-shaped release directory containing
`manifest.json`, the released cohort and global assets, and `mcp/v0.1/catalog.json`. Set
`IPRATE_MCP_ASSET_BASE_URL` to the public URL used in source citations.

The catalogue is derived only from static profile assets:

```bash
iprate-free-mcp-build-catalog /path/to/data/v1
```

The builder fails if required indexes are missing, checksums differ, profile counts do
not match the release manifest, or a profile asset is unreadable. It never imports or
connects to IPRATE's database, API, models, or pipeline.

## Release snapshots

In production the adapter never reads the live, mutating export tree. A refresher
(`iprate-free-mcp-refresh-release`) watches the live `manifest.json` and, when a new
`release_id` appears, copies every manifest-declared asset into an immutable per-release
snapshot (checksum-verified against the manifest), builds the catalogue inside that
snapshot, and atomically repoints a `current` symlink. The server keeps serving the
previous complete release until the next one is activated; a release that changes while
it is being snapshotted is abandoned and retried on the next pass. Snapshots contain no
entity profile files — profile data enters only through the derived catalogue — and the
catalogue is never written into the publicly served tree.

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

The local endpoint is `http://127.0.0.1:8000/mcp`; liveness is `GET /healthz`.

## Production containers

`compose.yaml` runs two hardened services built from the same image:

- `mcp` — the server, unprivileged and read-only, mounting only the snapshot directory
  (read-only) and serving `IPRATE_MCP_ASSET_ROOT=/data/current`. It joins the existing
  `runtime_default` network for Cloudflare Tunnel routing and publishes no host port.
- `refresher` — the snapshot builder, with `network_mode: none`, mounting the live
  export tree read-only and the snapshot directory read-write.

```bash
docker compose up -d --build
```

Environment overrides: `IPRATE_STATIC_ROOT` (live export tree),
`IPRATE_MCP_RELEASES_ROOT` (snapshot directory), `IPRATE_MCP_REFRESH_UID`/`GID`
(owner of the snapshot directory), `IPRATE_MCP_RATE_LIMIT_PER_MINUTE` (default 120),
`IPRATE_MCP_STALE_AFTER_DAYS` (default 21). Neither service receives a database or
application secret.

Documentation: <https://iprate.eu/developers/mcp/>

## Licence

Adapter code is Apache-2.0. IPRATE data and website content have separate terms and
licences; this code licence does not grant rights to them or to IPRATE trademarks.
