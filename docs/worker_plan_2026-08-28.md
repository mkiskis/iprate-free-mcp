# IPRATE Free Public MCP — Cloudflare Worker plan (draft for owner sign-off)

Date: 2026-08-28 · Status: APPROVED (GO: cron one-shot, private R2, public repo; app stays as is) and IMPLEMENTED same day through commit 116c9ef — live on the workers.dev preview URL; remaining: scoped R2 token → builder cron, owner GO → mcp.iprate.eu attach · Product scope: unchanged spec v0.1

## 1. Goal and constraints

- Same product as the v0.1 spec: four bounded, anonymous, read-only tools over released static data. No scope change.
- **.18 is backend and app only**: it builds data files; it serves nothing public for the MCP and gets no new resident process.
- Public serving is a **Cloudflare Worker** at `mcp.iprate.eu` — no VM, no container, nothing to babysit.
- **$0 incremental cost**: Workers Paid ($5/mo) is already active on the account; R2 is already enabled.

## 2. Architecture

```
.18 (backend only)                     Cloudflare                        MCP clients
─────────────────                      ──────────                       ────────────
release publish to /data/v1
        │
cron one-shot (15 min, exits <1s      R2 bucket "iprate-mcp"            ChatGPT / Claude /
when release unchanged):               (private, no public URL)          other MCP clients
  build worker release ───────────▶   releases/{release_id}/…                  │
  validate checksums/counts           current.json  ◀── atomic pointer         │
                                            │                                  │
                                       Worker "iprate-mcp"  ◀── POST /mcp ─────┘
                                       (mcp.iprate.eu, R2 binding, read-only)
```

## 3. Data plane — R2 release prefixes

Bucket `iprate-mcp`, private (Worker binding only — never publicly downloadable):

```
releases/{release_id}/
  mcp-manifest.json                    release_id, generated_at, counts, coverage, per-file sha256
  search.json                          compact scan index for matching+ranking (~6–10 MB, columnar)
  entities/{00..ff}.json               bounded per-entity output data, ~256 shards of 100–400 KB
  cohorts/{cc}/{vert}/{tier}/{win}/stats.json   byte copies of the released files
  cohorts/{cc}/{vert}/{tier}/{win}/firms.json   byte copies
  analytics_stats.json                 byte copy
current.json                           {"release_id": …} — single-object write = atomic activation
```

- **Why R2, not public `/data/v1`**: the index aggregates the bounded data of all 34,746 published profiles; as a public file it would be a de-facto bulk export. A private binding keeps it Worker-only (same reason profile JSONs are excluded from public serving today). This is the "immutable HTTPS asset base" flavor of spec §2, one notch tighter.
- **Atomicity**: a release prefix is immutable once written; activation is one pointer write. All files embed the release_id; any mismatch at read time → `source_unavailable` (fail closed, same semantics as the Python adapter). Keep last 2 prefixes; delete older.
- Size: ~50–70 MB per release × 2 retained ≈ under a cent per month; operations negligible.
- Note: the public `search-index.json` (6.2 MB) was evaluated — it carries name/slug/tier identity but no cohort-level rank/class/client data, so the derived index is still required. Its self-filer exclusion filter is reused by the builder (below).

### search.json (matching only — display data lives in shards)

Columnar arrays, per entity: `[type, id, slug, name_key, home_cc, shard]` plus per released cohort:
`[jurisdiction, right, tier, window, rank, score, classes[≤5], client_keys[≤5]]`.
Built from the same profile-JSON cohort projection the Python `build_catalog` validates today (score_tier present = published; whitelist fields only).

### entities/{xx}.json

Shard = first 2 hex of sha256(`type:slug`). Entry = exactly the Python adapter's public projection: quoted name, city, home country, released cohorts with published_rating, released_activity, top_classes (with labels), leading_clients (quoted name, share, rank), profile_url, provenance marker.

## 4. Builder — runs on .18, no resident process

- New CLI in the `iprate-free-mcp` repo: `iprate-free-mcp-build-worker-release`. Reuses the existing, tested validation path (manifest release_id, index checksums, entity-count check, abort-and-retry if the live tree changes mid-build). Static files in → static files out; no database, pipeline, model, or API import (spec §2 preserved).
- Adds the search-index self-filer exclusion: entities absent from the published indexes never enter; blocked self-filer firms are excluded the same way `export_search_index` excludes them (via the published `search-index.json` as an allowlist input, keeping the builder DB-free).
- Uploads to R2 over the S3 API with a **bucket-scoped write-only R2 token** (created once with the account token; no other permissions), then writes `current.json`.
- Trigger: **cron one-shot on .18 every 15 minutes** — exits in under a second when the release is unchanged; ~1–2 minutes of work after a real publish. Run via `docker compose run --rm` from the existing checkout (or a host venv — either way, zero resident containers). Alternative (not recommended, touches the main repo): a pipeline-finalize hook.

## 5. Worker — public endpoint

- Lives in `worker/` inside the public `iprate-free-mcp` repo (keeps the Apache-2.0 "adapter, schemas and tests are open" promise; the Python implementation stays as the self-host reference and test oracle). No secrets in the repo — R2 access is a deploy-time binding.
- TypeScript, official MCP TS SDK, **Streamable HTTP, stateless, JSON responses** (POST `/mcp`) — no sessions, no Durable Objects, no KV, no D1.
- **Contract is a 1:1 port of the Python adapter** (it is the spec's reference): the same four tools, input schemas, read-only annotations, response envelope (status/data/as_of/release_id/coverage/limitations/source_urls/links/server_version), caps (≤5 results, ≤5 classes, name ≥2 chars, client ≥3 chars, unfiltered enumeration rejected), cohort priority ordering, `stale` after 21 days, EU→euro-route semantics, `not_public` without identity leakage.
- Data access per request: `current.json` (edge-cached ~60 s) → `mcp-manifest.json` → needed files; release_id consistency enforced on every read; any inconsistency → `source_unavailable`.
- Parsed `search.json` cached in isolate memory keyed by release_id (old parse dropped before the new one loads — same single-slot discipline as the Python fix; ~30–50 MB, within the 128 MB Worker limit).
- Rate limiting: Workers rate-limiting binding, default 120/min per client IP → HTTP 429 + `Retry-After` + `rate_limited` body. Results never silently truncated.
- CORS/origin allowlist as today (claude.ai, chatgpt.com, iprate.eu, localhost); request body cap 256 KB; `GET /healthz` → current release_id.
- `source_urls` cite public equivalents: profile pages and `iprate.eu/data/v1/...` cohort/manifest URLs.
- Privacy: no logging of tool arguments (privacy §2.5 holds). Optional phase 2: Workers Analytics Engine counters (tool name, status, latency only — already subscribed, $0).

## 6. Expected performance

Warm request ≈ 1–5 ms CPU (in-memory scan of 34,746 entities measured at 8–73 ms wall in Python; the TS scan is comparable or faster). Cold isolate ≈ 100–250 ms (R2 fetch + parse of the scan index). p95 far under the 5 s launch gate; formal measurement stays gate §9.9.

## 7. Testing and rollout

1. Contract tests: vitest + Workers test pool with R2 emulation, using **byte-identical fixtures ported from the Python suite**; cross-check envelope outputs against the Python adapter as oracle.
2. Deploy to the private workers.dev preview URL. Run: tools/list schema comparison vs Python; sampled value-equality vs the live release (gate §7); fail-closed tests (missing prefix, mixed release, corrupt file); rate-limit behavior.
3. Client interop (gate §8): ChatGPT, Claude, one independent client — against the preview URL.
4. Attach the `mcp.iprate.eu` custom domain (plain zone route on the existing account — **no tunnel, no .18 involvement**).
5. Frontend `/developers/mcp` pages, server.json, and Official MCP Registry listing: unchanged content, still owner-gated (§9.10) — merge and list only after 2–4 pass.
6. Rollback: detach the route (worker keeps running on preview), or flip `current.json` back to the previous release prefix.

## 8. Cleanup and follow-ons

- .18: remove the leftover `iprate-free-mcp:0.2.0` image and the 107 MB `/home/ming/iprate-free-mcp-releases` snapshot dir (superseded by R2); keep the repo checkout for the builder cron. Nothing else on .18 changes.
- Spec text alignment (main repo docs, owner-controlled — drafted for your approval, not auto-committed): note the R2-binding asset base under §2, and that `rate_limited` is returned at the HTTP layer (429) rather than inside the tool envelope.

## 9. Cost

$0/month incremental: Workers Paid already active (10M req + 30M CPU-ms account-wide, currently near-idle); R2 storage/ops effectively $0 at these sizes; custom domain and TLS $0; builder cron on .18 negligible. Worst realistic abuse case stays inside existing allowances due to the 120/min rate limit.

## 10. Decisions needed

1. Builder trigger: **cron one-shot on .18 (recommended)** vs pipeline-finalize hook in the main repo.
2. Index home: **private R2 (recommended)** vs publishing the index into public `/data/v1`.
3. Worker code location: **public `iprate-free-mcp` repo (recommended)** vs a separate private repo.
4. GO / NO-GO to implement (builder CLI + Worker + tests + preview deploy; custom-domain attach as a separate confirmation).
