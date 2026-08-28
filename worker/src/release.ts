// Validated read-only access to one immutable Worker release in R2.
//
// The builder (iprate-free-mcp-build-worker-release) uploads each release
// under an immutable prefix and activates it by rewriting current.json.
// Every read here fails closed: missing objects, mixed release ids, or
// checksum mismatches raise StaticAssetError and the tools answer
// source_unavailable — no live service, older release, or placeholder is
// ever substituted.

export class StaticAssetError extends Error {}

export interface Env {
  RELEASES: R2Bucket;
  RATE_LIMITER?: { limit(options: { key: string }): Promise<{ success: boolean }> };
  ASSET_BASE_URL?: string;
  STALE_AFTER_DAYS?: string;
}

interface Pointer {
  release_id: string;
  worker_schema: number;
  build_id?: string;
  prefix: string;
}

export interface CohortMeta {
  run_id: number | null;
  published_at: string | null;
  degraded_reasons: unknown[];
  windows: string[];
  files: Record<string, { stats?: string; firms?: string }>;
}

export interface McpManifest {
  worker_schema: number;
  release_id: string;
  generated_at: string | null;
  status: string | null;
  degraded_reasons: unknown[];
  entity_counts: Record<string, number>;
  mcp_entity_counts: Record<string, number>;
  cohorts: Record<string, CohortMeta>;
  checksums: Record<string, string>;
}

// Scan row: [type, id, slug, name_key, home_cc, shard, cohorts]
// Cohort tuple: [jurisdiction, right(asset code), tier, window, rank, score, classes, client_keys]
export type ScanCohort = [
  string,
  string,
  string,
  string,
  number | null,
  number | null,
  string[],
  string[],
];
export type ScanRow = [string, number, string, string, string | null, string, ScanCohort[]];

interface SearchIndex {
  schema: number;
  release_id: string;
  entities: ScanRow[];
}

const WORKER_SCHEMA = 1;
const POINTER_TTL_MS = 60_000;

let pointerCache: { fetchedAt: number; pointer: Pointer } | null = null;
let slot: { buildKey: string; manifest: McpManifest; search: SearchIndex } | null = null;

export function resetReleaseCache(): void {
  pointerCache = null;
  slot = null;
}

async function getBytes(env: Env, key: string): Promise<ArrayBuffer> {
  const object = await env.RELEASES.get(key);
  if (object === null) throw new StaticAssetError(`Required static asset is unavailable: ${key}`);
  return object.arrayBuffer();
}

async function getJson(env: Env, key: string): Promise<unknown> {
  const buffer = await getBytes(env, key);
  try {
    return JSON.parse(new TextDecoder().decode(buffer));
  } catch {
    throw new StaticAssetError(`Cannot read static JSON asset: ${key}`);
  }
}

async function sha256Prefix(buffer: ArrayBuffer, length: number): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("")
    .slice(0, length);
}

async function getPointer(env: Env): Promise<Pointer> {
  const now = Date.now();
  if (pointerCache && now - pointerCache.fetchedAt < POINTER_TTL_MS) return pointerCache.pointer;
  const raw = (await getJson(env, "current.json")) as Partial<Pointer> | null;
  if (
    !raw ||
    typeof raw !== "object" ||
    typeof raw.release_id !== "string" ||
    typeof raw.prefix !== "string"
  ) {
    throw new StaticAssetError("current.json is not a valid release pointer");
  }
  if (raw.worker_schema !== WORKER_SCHEMA) {
    throw new StaticAssetError("Unsupported worker release schema");
  }
  const pointer = raw as Pointer;
  pointerCache = { fetchedAt: now, pointer };
  return pointer;
}

export class Release {
  constructor(
    private readonly env: Env,
    private readonly prefix: string,
    readonly manifest: McpManifest,
    readonly search: SearchIndex,
  ) {}

  get releaseId(): string {
    return this.manifest.release_id;
  }

  get asOf(): string | null {
    return this.manifest.generated_at ?? null;
  }

  // Byte-copied release asset (cohort stats/firms, analytics), verified
  // against the checksum recorded when the release was built.
  async asset(relativePath: string): Promise<unknown> {
    const key = `assets/${relativePath}`;
    const expected = this.manifest.checksums[key];
    if (!expected) throw new StaticAssetError(`Static asset is not declared by the release: ${relativePath}`);
    const buffer = await getBytes(this.env, this.prefix + key);
    const actual = await sha256Prefix(buffer, expected.length);
    if (actual !== expected) {
      throw new StaticAssetError(
        `Static asset checksum mismatch for ${relativePath}: expected ${expected}, got ${actual}`,
      );
    }
    try {
      return JSON.parse(new TextDecoder().decode(buffer));
    } catch {
      throw new StaticAssetError(`Cannot read static JSON asset: ${relativePath}`);
    }
  }

  async entityShard(shard: string): Promise<Record<string, Record<string, unknown>>> {
    const doc = (await getJson(this.env, `${this.prefix}entities/${shard}.json`)) as {
      release_id?: string;
      entities?: Record<string, Record<string, unknown>>;
    };
    if (!doc || doc.release_id !== this.releaseId || typeof doc.entities !== "object") {
      throw new StaticAssetError(`Entity shard is inconsistent: ${shard}`);
    }
    return doc.entities ?? {};
  }
}

export async function loadRelease(env: Env): Promise<Release> {
  const pointer = await getPointer(env);
  const buildKey = `${pointer.release_id}:${pointer.build_id ?? ""}`;
  if (!slot || slot.buildKey !== buildKey) {
    const manifest = (await getJson(env, `${pointer.prefix}mcp-manifest.json`)) as McpManifest;
    if (!manifest || manifest.release_id !== pointer.release_id || manifest.worker_schema !== WORKER_SCHEMA) {
      throw new StaticAssetError("Worker release manifest and pointer belong to different releases");
    }
    slot = null; // drop the previous parse before loading the large index
    const search = (await getJson(env, `${pointer.prefix}search.json`)) as SearchIndex;
    if (!search || search.release_id !== manifest.release_id || !Array.isArray(search.entities)) {
      throw new StaticAssetError("Search index and manifest belong to different releases");
    }
    slot = { buildKey, manifest, search };
  }
  return new Release(env, pointer.prefix, slot.manifest, slot.search);
}
