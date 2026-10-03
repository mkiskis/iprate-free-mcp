// Contract tests, ported from the Python reference suite
// (tests/test_contract.py) with byte-equivalent fixture values, plus the
// Worker-only surface: output schemas, search, fetch, the 0.4.0 response
// cleanup, and the OpenAI domain-verification route.

import { env } from "cloudflare:test";
import { beforeEach, expect, it } from "vitest";

import worker from "../src/index";
import { resetReleaseCache } from "../src/release";
import { parseQuery } from "../src/research";

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
        ["lt-example"],
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

async function callResult(name: string, args: Record<string, unknown> = {}): Promise<any> {
  const body = await rpc("tools/call", { name, arguments: args });
  expect(body.error).toBeUndefined();
  return body.result;
}

// Minimal JSON Schema subset (type, enum, required, properties, items,
// maxItems). workerd forbids the code generation eval-based validators use.
function schemaErrors(schema: any, value: unknown, path = "$"): string[] {
  if (schema.type !== undefined) {
    const types: string[] = Array.isArray(schema.type) ? schema.type : [schema.type];
    const actual = value === null ? "null" : Array.isArray(value) ? "array" : typeof value;
    const matches = types.some(
      (type) => type === actual || (type === "integer" && typeof value === "number" && Number.isInteger(value)),
    );
    if (!matches) return [`${path}: expected ${types.join("|")}, got ${actual}`];
  }
  const errors: string[] = [];
  if (schema.enum && !schema.enum.includes(value)) errors.push(`${path}: ${String(value)} is not in the enum`);
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    const record = value as Record<string, unknown>;
    for (const key of schema.required ?? []) {
      if (!(key in record)) errors.push(`${path}.${key}: required`);
    }
    for (const [key, sub] of Object.entries(schema.properties ?? {})) {
      if (key in record) errors.push(...schemaErrors(sub, record[key], `${path}.${key}`));
    }
  }
  if (Array.isArray(value)) {
    if (schema.maxItems !== undefined && value.length > schema.maxItems) errors.push(`${path}: too many items`);
    if (schema.items) {
      value.forEach((item, index) => errors.push(...schemaErrors(schema.items, item, `${path}[${index}]`)));
    }
  }
  return errors;
}

async function outputSchemas(): Promise<Record<string, any>> {
  const body = await rpc("tools/list");
  return Object.fromEntries(body.result.tools.map((tool: any) => [tool.name, tool.outputSchema]));
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

it("lists six static read-only tools with output schemas", async () => {
  const body = await rpc("tools/list");
  const tools = body.result.tools;
  expect(new Set(tools.map((tool: any) => tool.name))).toEqual(
    new Set([
      "find_ip_representatives",
      "get_ip_representative_profile",
      "get_ip_market_snapshot",
      "get_iprate_coverage",
      "search",
      "fetch",
    ]),
  );
  for (const tool of tools) {
    expect(tool.annotations.readOnlyHint).toBe(true);
    expect(tool.annotations.destructiveHint).toBe(false);
    expect(tool.annotations.idempotentHint).toBe(true);
    expect(tool.annotations.openWorldHint).toBe(false);
    expect(tool.inputSchema.properties.activity_from).toBeUndefined();
    expect(tool.inputSchema.properties.activity_to).toBeUndefined();
    expect(tool.outputSchema.type).toBe("object");
    expect(tool.handler).toBeUndefined();
  }
  const byName = Object.fromEntries(tools.map((tool: any) => [tool.name, tool]));
  expect(byName.search.inputSchema.required).toEqual(["query"]);
  expect(byName.fetch.inputSchema.required).toEqual(["id"]);
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

it("profile resolves the site public slug and the export slug", async () => {
  for (const slug of ["lt-example", "LT-EXAMPLE", "lt-example-ip"]) {
    const envelope = await callTool("get_ip_representative_profile", { slug });
    expect(envelope.status).toBe("ok");
    expect(envelope.data.representative_id).toBe(1);
  }
  const missing = await callTool("get_ip_representative_profile", { slug: "lt-example-i" });
  expect(missing.status).toBe("not_public");
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

it("every bounded-tool response matches its advertised output schema", async () => {
  const schemas = await outputSchemas();
  expect(schemaErrors(schemas.find_ip_representatives, { status: "stale", data: [] })).not.toEqual([]);
  const calls: Array<[string, Record<string, unknown>, string]> = [
    ["find_ip_representatives", { jurisdiction: "LT", right_type: "trademark" }, "ok"],
    ["find_ip_representatives", { name: "nobody at all" }, "no_results"],
    ["find_ip_representatives", {}, "invalid_request"],
    ["get_ip_representative_profile", { slug: "lt-example-ip" }, "ok"],
    ["get_ip_representative_profile", { representative_id: 1 }, "ambiguous"],
    ["get_ip_representative_profile", { slug: "lt-unknown" }, "not_public"],
    ["get_ip_market_snapshot", { jurisdiction: "LT", right_type: "trademark", tier: "national", window: "long" }, "ok"],
    ["get_ip_market_snapshot", { jurisdiction: "LT", right_type: "design", tier: "national", window: "long" }, "not_covered"],
    ["get_iprate_coverage", {}, "ok"],
    ["get_iprate_coverage", { jurisdiction: "US" }, "not_covered"],
  ];
  for (const [name, args, expected] of calls) {
    const envelope = await callTool(name, args);
    expect(envelope.status, `${name} ${JSON.stringify(args)}`).toBe(expected);
    expect(schemaErrors(schemas[name], envelope), `${name} ${JSON.stringify(args)}`).toEqual([]);
  }
  await env.RELEASES.delete("current.json");
  resetReleaseCache();
  for (const name of ["find_ip_representatives", "get_iprate_coverage"]) {
    const envelope = await callTool(name, name === "get_iprate_coverage" ? {} : { jurisdiction: "LT" });
    expect(envelope.status).toBe("source_unavailable");
    expect(schemaErrors(schemas[name], envelope)).toEqual([]);
  }
});

it("responses carry no release incidents, status word, or release-age notice", async () => {
  const manifestKey = `${PREFIX}mcp-manifest.json`;
  const manifest = JSON.parse(await (await env.RELEASES.get(manifestKey))!.text());
  const incident = { lane: "national", detail: "TimeoutError at /home/ming/iprate-output/data/v1" };
  manifest.status = "degraded";
  manifest.generated_at = "2025-01-01T00:00:00Z";
  manifest.degraded_reasons = [incident];
  manifest.cohorts["LT:tm:national"].degraded_reasons = [incident];
  await env.RELEASES.put(manifestKey, encode(manifest));
  resetReleaseCache();

  const coverage = await callTool("get_iprate_coverage", { jurisdiction: "LT" });
  const find = await callTool("find_ip_representatives", { jurisdiction: "LT" });
  const market = await callTool("get_ip_market_snapshot", {
    jurisdiction: "LT",
    right_type: "trademark",
    tier: "national",
    window: "long",
  });
  for (const envelope of [coverage, find, market]) {
    expect(envelope.status).toBe("ok");
    expect(envelope.as_of).toBe("2025-01-01T00:00:00Z");
    expect(envelope.coverage.hold_state).toBe("partial");
    const serialised = JSON.stringify(envelope);
    for (const forbidden of [
      "/home/ming",
      "TimeoutError",
      "gaps_and_incidents",
      "known_coverage_incidents",
      "release_status",
      "degraded",
      "stale",
    ]) {
      expect(serialised).not.toContain(forbidden);
    }
  }
  expect(coverage.data.cohorts[0].status).toBe("partial");
});

it("search returns market, leading-firm and coverage documents for a country question", async () => {
  const schemas = await outputSchemas();
  const result = await callResult("search", { query: "Best trademark firms in Lithuania" });
  expect(result.isError).toBe(false);
  expect(schemaErrors(schemas.search, result.structuredContent)).toEqual([]);
  expect(JSON.parse(result.content[0].text)).toEqual(result.structuredContent);
  const ids = result.structuredContent.results.map((entry: any) => entry.id);
  expect(ids).toEqual(["market:LT:trademark:national:long", "firm:lt-example-ip", "coverage:LT"]);
  const firm = result.structuredContent.results[1];
  expect(firm.title).toContain("Example IP");
  expect(firm.title).toContain("IPRATE Elite");
  expect(firm.url).toBe("https://iprate.eu/firms/lt-example-ip/");
  expect(result.structuredContent.results[0].url).toBe("https://iprate.eu/country/lt/");

  const byCode = await callResult("search", { query: "trademark LT" });
  expect(byCode.structuredContent.results.map((entry: any) => entry.id)).toEqual(ids);
});

it("search matches representative names before anything else", async () => {
  const exact = await callResult("search", { query: "Example IP" });
  expect(exact.structuredContent.results[0].id).toBe("firm:lt-example-ip");
  expect(exact.structuredContent.results[0].title).toContain("IPRATE Elite");

  const both = await callResult("search", { query: "example" });
  const ids = both.structuredContent.results.map((entry: any) => entry.id);
  expect(ids).toContain("firm:lt-example-ip");
  expect(ids).toContain("attorney:lt-example-person");
  expect(ids.length).toBeLessThanOrEqual(10);
});

it("search reports no match for an unknown name and summarises coverage for generic questions", async () => {
  const unknown = await callResult("search", { query: "Find an IPRATE representative named IPRATE-NONEXISTENT-REVIEW-7c81b359." });
  expect(unknown.isError).toBe(false);
  expect(unknown.structuredContent.results).toEqual([]);

  const generic = await callResult("search", { query: "Best IP firms in Europe" });
  expect(generic.structuredContent.results).toEqual([
    { id: "coverage:europe", title: "IPRATE coverage · Europe", url: "https://iprate.eu/analytics/" },
  ]);
});

it("search rejects empty and oversized queries and fails closed without a release", async () => {
  for (const query of ["", " ", "x".repeat(257)]) {
    const result = await callResult("search", { query });
    expect(result.isError).toBe(true);
    expect(result.structuredContent).toBeUndefined();
  }
  await env.RELEASES.delete("current.json");
  resetReleaseCache();
  const unavailable = await callResult("search", { query: "Lithuania" });
  expect(unavailable.isError).toBe(true);
  expect(unavailable.content[0].text).toContain("unavailable");
});

it("fetch renders a representative profile from the bounded profile tool", async () => {
  const schemas = await outputSchemas();
  const result = await callResult("fetch", { id: "FIRM:LT-EXAMPLE-IP" });
  expect(result.isError).toBe(false);
  expect(schemaErrors(schemas.fetch, result.structuredContent)).toEqual([]);
  const doc = result.structuredContent;
  expect(doc.id).toBe("firm:lt-example-ip");
  expect(doc.url).toBe("https://iprate.eu/firms/lt-example-ip/");
  expect(doc.title).toBe("Example IP · IPRATE firm profile");
  expect(doc.text).toContain("IPRATE published profile: IP firm, Vilnius, Lithuania.");
  expect(doc.text).toContain("Rating: IPRATE Elite; confidence grade A; score 91.2; rank 1 in this cohort.");
  expect(doc.text).toContain("registration rate 90%");
  expect(doc.text).toContain('Leading clients (quoted register data): "ACME Ltd" (15%).');
  expect(doc.metadata.release_id).toBe(RELEASE_ID);
  expect(doc.metadata.document_type).toBe("representative_profile");
});

it("fetch renders market snapshot and coverage documents", async () => {
  const schemas = await outputSchemas();
  const market = (await callResult("fetch", { id: "market:lt:trademark:national:long" })).structuredContent;
  expect(schemaErrors(schemas.fetch, market)).toEqual([]);
  expect(market.id).toBe("market:LT:trademark:national:long");
  expect(market.text).toContain("- applications total: 777");
  expect(market.text).toContain("1. Example IP, Vilnius: IPRATE Elite; confidence grade A;");
  expect(market.url).toBe("https://iprate.eu/country/lt/");

  const country = (await callResult("fetch", { id: "coverage:LT" })).structuredContent;
  expect(country.text).toContain("Released cohorts for Lithuania:");
  expect(country.text).toContain("- trademarks, national route: windows long; published 2026-08-27; held");

  const europe = (await callResult("fetch", { id: "coverage:europe" })).structuredContent;
  expect(europe.text).toContain("Jurisdictions with released cohorts (1): Lithuania (LT).");
  expect(europe.text).toContain("Register holdings analysed: trademarks 1,000 records in 1 jurisdictions.");
  expect(europe.url).toBe("https://iprate.eu/analytics/");

  const uncovered = (await callResult("fetch", { id: "coverage:US" })).structuredContent;
  expect(uncovered.text).toContain("The current IPRATE release has no released cohorts for US.");
});

it("fetch fails closed on unknown, unpublished, or malformed ids", async () => {
  for (const id of ["firm:lt-unknown", "market:LT:design:national:long", "drop table", "", "firm:"]) {
    const result = await callResult("fetch", { id });
    expect(result.isError, id).toBe(true);
    expect(result.structuredContent, id).toBeUndefined();
  }
  await env.RELEASES.delete("current.json");
  resetReleaseCache();
  const unavailable = await callResult("fetch", { id: "firm:lt-example-ip" });
  expect(unavailable.isError).toBe(true);
  expect(unavailable.content[0].text).toContain("unavailable");
});

it("serves the OpenAI domain-verification token only when configured", async () => {
  const path = "https://mcp.iprate.eu/.well-known/openai-apps-challenge";
  const missing = await worker.fetch(new Request(path), env as never);
  expect(missing.status).toBe(404);

  const configured = { ...env, OPENAI_APPS_CHALLENGE: " token-123 " };
  const served = await worker.fetch(new Request(path), configured as never);
  expect(served.status).toBe(200);
  expect(served.headers.get("content-type")).toContain("text/plain");
  expect(await served.text()).toBe("token-123");

  const posted = await worker.fetch(new Request(path, { method: "POST" }), configured as never);
  expect(posted.status).toBe(405);
});

it("parses review and starter prompts into filters without stray name words", () => {
  const cases: Array<[string, string[], string | null, string | null]> = [
    ["Using IPRATE, research which firms lead patent work in Germany and cite your sources.", ["DE"], "patent", null],
    ["Which firms lead trademark work in Lithuania according to IPRATE?", ["LT"], "trademark", null],
    ["Show IPRATE's German patent market snapshot for the European route.", ["DE"], "patent", "euro"],
    ["Find IPRATE-rated design firms in Denmark.", ["DK"], "design", null],
    ["Show IPRATE's Lithuanian national trademark market snapshot for the long window.", ["LT"], "trademark", "national"],
    ["beste Markenanwälte in Deutschland", ["DE"], "trademark", null],
  ];
  for (const [query, jurisdictions, right, tier] of cases) {
    const parsed = parseQuery(query);
    expect(parsed.nameTokens, query).toEqual([]);
    expect(parsed.jurisdictions, query).toEqual(jurisdictions);
    expect(parsed.right, query).toBe(right);
    expect(parsed.tier, query).toBe(tier);
  }
});

it("search prefers names that start at a word and falls back to substrings", async () => {
  const person = await callResult("search", { query: "person" });
  expect(person.structuredContent.results.map((entry: any) => entry.id)).toEqual(["attorney:lt-example-person"]);

  const fragment = await callResult("search", { query: "xample" });
  const ids = fragment.structuredContent.results.map((entry: any) => entry.id);
  expect(ids).toContain("firm:lt-example-ip");
  expect(ids).toContain("attorney:lt-example-person");
});
