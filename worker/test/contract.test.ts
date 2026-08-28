// Contract tests, ported from the Python reference suite
// (tests/test_contract.py) with byte-equivalent fixture values.

import { env } from "cloudflare:test";
import { beforeEach, expect, it } from "vitest";

import worker from "../src/index";
import { resetReleaseCache } from "../src/release";

const RELEASE_ID = "test-release";
const PREFIX = `releases/${RELEASE_ID}/build1/`;
const STATS_PATH = "lt/tm/national/long/stats.json";
const FIRMS_PATH = "lt/tm/national/long/firms.json";

const encode = (payload: unknown) => JSON.stringify(payload);

async function sha16(body: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(body));
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("")
    .slice(0, 16);
}

function publicCohort(options: {
  window: string;
  rank: number | null;
  score: number;
  client: string;
}): Record<string, unknown> {
  return {
    jurisdiction: "LT",
    right_type: "trademark",
    tier: "national",
    window: options.window,
    published_rating: { score: options.score, tier: "Q1", confidence: "A", rank: options.rank },
    released_activity: {
      case_units: 123,
      volume_per_year: 12.5,
      registration_rate: 0.9,
      time_to_grant_days: 180,
      total_firms_filing: 40,
    },
    top_classes: [{ class_code: "9", class_label: "Electronics", share_pct: 20, rank: 1 }],
    leading_clients: [{ quoted_name: options.client, share_pct: 15, rank: 1 }],
  };
}

async function seedRelease(): Promise<void> {
  const statsBody = encode({
    data: {
      country_code: "LT",
      vertical: "tm",
      tier: "national",
      window_kind: "long",
      applications_total: 777,
      rated_firms: 1,
    },
    meta: { run_id: 32, generated_at: "2026-08-27T12:00:00Z" },
  });
  const firmsBody = encode({
    data: [
      {
        id: 1,
        name: "Example IP",
        slug: "lt-example-ip",
        city: "Vilnius",
        country_code: "LT",
        score_tier: "Q1",
        confidence_grade: "A",
        volume_per_year: 12.5,
        registration_rate: 0.9,
        time_to_grant_days: 180,
        top_class: "9",
      },
    ],
    meta: { run_id: 32, count: 1 },
  });
  const analyticsBody = encode({
    by_vertical: { tm: { records: 1000, jurisdictions: 1 } },
    total: { records: 1000, jurisdictions: 1 },
    generated_at: "2026-08-27T12:00:00Z",
  });

  const search = {
    schema: 1,
    release_id: RELEASE_ID,
    entities: [
      [
        "firm",
        1,
        "lt-example-ip",
        "example ip",
        "LT",
        "ab",
        [
          ["LT", "tm", "national", "long", 1, 91.2, ["9"], ["acme ltd"]],
          ["LT", "tm", "national", "recent", null, 70.0, ["9"], ["beta corp"]],
        ],
      ],
      [
        "attorney",
        1,
        "lt-example-person",
        "example person",
        "LT",
        "ab",
        [["LT", "tm", "national", "long", null, 82.4, ["9"], ["acme ltd"]]],
      ],
    ],
  };
  const shard = {
    release_id: RELEASE_ID,
    entities: {
      "firm:lt-example-ip": {
        representative_type: "firm",
        representative_id: 1,
        quoted_name: "Example IP",
        slug: "lt-example-ip",
        home_country_code: "LT",
        city: "Vilnius",
        cohorts: [
          publicCohort({ window: "long", rank: 1, score: 91.2, client: "ACME Ltd" }),
          publicCohort({ window: "recent", rank: null, score: 70.0, client: "Beta Corp" }),
        ],
        profile_url: "https://iprate.eu/firms/lt-example-ip/",
        text_provenance: "quoted_untrusted_register_data",
      },
      "attorney:lt-example-person": {
        representative_type: "attorney",
        representative_id: 1,
        quoted_name: "Example Person",
        slug: "lt-example-person",
        home_country_code: "LT",
        city: "Kaunas",
        cohorts: [publicCohort({ window: "long", rank: null, score: 82.4, client: "ACME Ltd" })],
        profile_url: "https://iprate.eu/attorneys/lt-example-person/",
        text_provenance: "quoted_untrusted_register_data",
      },
    },
  };
  const manifest = {
    worker_schema: 1,
    release_id: RELEASE_ID,
    generated_at: "2026-08-27T12:00:00Z",
    status: "ok",
    degraded_reasons: [],
    entity_counts: { firms: 2, attorneys: 1 },
    mcp_entity_counts: { firms: 1, attorneys: 1, excluded: 1 },
    cohorts: {
      "LT:tm:national": {
        run_id: 32,
        published_at: "2026-08-27T12:00:00Z",
        degraded_reasons: [],
        windows: ["long"],
        files: { long: { stats: STATS_PATH, firms: FIRMS_PATH } },
      },
    },
    checksums: {
      [`assets/${STATS_PATH}`]: await sha16(statsBody),
      [`assets/${FIRMS_PATH}`]: await sha16(firmsBody),
      "assets/analytics_stats.json": await sha16(analyticsBody),
    },
  };

  await env.RELEASES.put(`${PREFIX}search.json`, encode(search));
  await env.RELEASES.put(`${PREFIX}entities/ab.json`, encode(shard));
  await env.RELEASES.put(`${PREFIX}assets/${STATS_PATH}`, statsBody);
  await env.RELEASES.put(`${PREFIX}assets/${FIRMS_PATH}`, firmsBody);
  await env.RELEASES.put(`${PREFIX}assets/analytics_stats.json`, analyticsBody);
  await env.RELEASES.put(`${PREFIX}mcp-manifest.json`, encode(manifest));
  await env.RELEASES.put(
    "current.json",
    encode({ release_id: RELEASE_ID, worker_schema: 1, build_id: "build1", prefix: PREFIX }),
  );
  resetReleaseCache();
}

interface RpcOptions {
  envOverride?: unknown;
  headers?: Record<string, string>;
}

async function post(payload: unknown, options: RpcOptions = {}): Promise<Response> {
  const request = new Request("https://mcp.iprate.eu/mcp", {
    method: "POST",
    headers: { "content-type": "application/json", ...(options.headers ?? {}) },
    body: encode(payload),
  });
  return worker.fetch(request, (options.envOverride ?? env) as never);
}

async function rpc(method: string, params: Record<string, unknown> = {}): Promise<any> {
  const response = await post({ jsonrpc: "2.0", id: 1, method, params });
  expect(response.status).toBe(200);
  return response.json();
}

async function callTool(name: string, args: Record<string, unknown> = {}): Promise<any> {
  const body = await rpc("tools/call", { name, arguments: args });
  expect(body.error).toBeUndefined();
  return body.result.structuredContent;
}

beforeEach(async () => {
  await seedRelease();
});

it("initialize negotiates and names the server", async () => {
  const body = await rpc("initialize", {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "contract-test", version: "1.0" },
  });
  expect(body.result.serverInfo.name).toBe("eu.iprate/ip-analytics");
  expect(body.result.protocolVersion).toBe("2025-06-18");
});

it("lists exactly four static read-only tools", async () => {
  const body = await rpc("tools/list");
  const tools = body.result.tools;
  expect(new Set(tools.map((tool: any) => tool.name))).toEqual(
    new Set([
      "find_ip_representatives",
      "get_ip_representative_profile",
      "get_ip_market_snapshot",
      "get_iprate_coverage",
    ]),
  );
  for (const tool of tools) {
    expect(tool.annotations.readOnlyHint).toBe(true);
    expect(tool.annotations.destructiveHint).toBe(false);
    expect(tool.annotations.idempotentHint).toBe(true);
    expect(tool.inputSchema.properties.activity_from).toBeUndefined();
    expect(tool.inputSchema.properties.activity_to).toBeUndefined();
  }
});

it("rejects unfiltered enumeration", async () => {
  const envelope = await callTool("find_ip_representatives");
  expect(envelope.status).toBe("invalid_request");
});

it("find preserves released ratings from the static index", async () => {
  const envelope = await callTool("find_ip_representatives", {
    jurisdiction: "LT",
    right_type: "trademark",
    nice_classes: ["09"],
    client_name: "ACME",
    tier: "national",
    window: "long",
  });
  expect(envelope.status).toBe("ok");
  expect(envelope.release_id).toBe(RELEASE_ID);
  const item = envelope.data.items[0];
  expect(item.quoted_name).toBe("Example IP");
  expect(item.matching_cohort.published_rating.score).toBe(91.2);
  expect(item.matching_cohort.released_activity.case_units).toBe(123);
  expect(envelope.coverage.static_assets).toEqual(["search.json"]);
});

it("profile identifier collisions require a type", async () => {
  const ambiguous = await callTool("get_ip_representative_profile", { representative_id: 1 });
  expect(ambiguous.status).toBe("ambiguous");
  expect(ambiguous.data.candidate_types).toEqual(["attorney", "firm"]);
  const resolved = await callTool("get_ip_representative_profile", {
    representative_id: 1,
    representative_type: "attorney",
  });
  expect(resolved.status).toBe("ok");
  expect(resolved.data.quoted_name).toBe("Example Person");
  expect(resolved.data.released_cohorts).toHaveLength(1);
});

it("profile returns the long ranked cohort first", async () => {
  const envelope = await callTool("get_ip_representative_profile", { slug: "LT-EXAMPLE-IP" });
  expect(envelope.status).toBe("ok");
  const first = envelope.data.released_cohorts[0];
  expect(first.window).toBe("long");
  expect(first.published_rating.rank).toBe(1);
});

it("market snapshot copies static values", async () => {
  const envelope = await callTool("get_ip_market_snapshot", {
    jurisdiction: "LT",
    right_type: "trademark",
    tier: "national",
    window: "long",
  });
  expect(envelope.status).toBe("ok");
  expect(envelope.data.released_statistics.applications_total).toBe(777);
  expect(envelope.data.leading_representatives[0].volume_per_year).toBe(12.5);
  expect(envelope.data.leading_representatives.length).toBeLessThanOrEqual(5);
});

it("coverage derives from the worker manifest and analytics", async () => {
  const envelope = await callTool("get_iprate_coverage", {
    jurisdiction: "LT",
    right_type: "trademark",
    tier: "national",
  });
  expect(envelope.status).toBe("ok");
  expect(envelope.data.hold_state).toBe("held");
  expect(envelope.data.holdings.total.records).toBe(1000);
  expect(envelope.data.cohorts[0].run_id).toBe(32);
  expect(envelope.data.cohorts[0].windows).toEqual(["long"]);
});

it("checksum mismatches fail closed", async () => {
  await env.RELEASES.put(
    `${PREFIX}assets/${STATS_PATH}`,
    encode({ data: { applications_total: 778 }, meta: { run_id: 32 } }),
  );
  const envelope = await callTool("get_ip_market_snapshot", {
    jurisdiction: "LT",
    right_type: "trademark",
    tier: "national",
    window: "long",
  });
  expect(envelope.status).toBe("source_unavailable");
  expect(envelope.coverage.error_type).toBe("StaticAssetError");
});

it("mixed-release indexes fail closed", async () => {
  const search = { schema: 1, release_id: "different-release", entities: [] };
  await env.RELEASES.put(`${PREFIX}search.json`, encode(search));
  resetReleaseCache();
  const envelope = await callTool("get_iprate_coverage");
  expect(envelope.status).toBe("source_unavailable");
});

it("healthz reports the current release and fails closed without one", async () => {
  const healthy = await worker.fetch(new Request("https://mcp.iprate.eu/healthz"), env as never);
  expect(healthy.status).toBe(200);
  expect(((await healthy.json()) as any).release_id).toBe(RELEASE_ID);

  await env.RELEASES.delete("current.json");
  resetReleaseCache();
  const unhealthy = await worker.fetch(new Request("https://mcp.iprate.eu/healthz"), env as never);
  expect(unhealthy.status).toBe(503);
  expect(((await unhealthy.json()) as any).status).toBe("source_unavailable");
});

it("rate limited calls get 429 with a rate_limited body", async () => {
  const limitedEnv = { ...env, RATE_LIMITER: { limit: async () => ({ success: false }) } };
  const response = await post(
    { jsonrpc: "2.0", id: 1, method: "tools/list", params: {} },
    { envOverride: limitedEnv },
  );
  expect(response.status).toBe(429);
  expect(response.headers.get("retry-after")).toBe("60");
  expect(((await response.json()) as any).status).toBe("rate_limited");
});

it("enforces transport limits", async () => {
  const oversized = await post({
    jsonrpc: "2.0",
    id: 1,
    method: "tools/list",
    params: { pad: "x".repeat(300 * 1024) },
  });
  expect(oversized.status).toBe(413);

  const batch = await post([{ jsonrpc: "2.0", id: 1, method: "tools/list" }]);
  expect(batch.status).toBe(400);

  const wrongMethod = await worker.fetch(
    new Request("https://mcp.iprate.eu/mcp", { method: "GET" }),
    env as never,
  );
  expect(wrongMethod.status).toBe(405);

  const notification = await post({ jsonrpc: "2.0", method: "notifications/initialized" });
  expect(notification.status).toBe(202);
});
