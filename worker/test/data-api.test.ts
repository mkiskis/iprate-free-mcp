import { env } from "cloudflare:test";
import { afterEach, expect, it, vi } from "vitest";

import {
  WORKER_CLIENT_IP_HEADER,
  WORKER_SIGNATURE_HEADER,
  WORKER_TIMESTAMP_HEADER,
  signClientAddress,
} from "../src/data-api";
import worker from "../src/index";

async function post(
  body: unknown,
  headers: Record<string, string> = {},
  envOverride: unknown = env,
): Promise<Response> {
  return worker.fetch(
    new Request("https://mcp.iprate.eu/data-api/mcp", {
      method: "POST",
      headers: { "content-type": "application/json", ...headers },
      body: JSON.stringify(body),
    }),
    envOverride as never,
  );
}

afterEach(() => vi.restoreAllMocks());

it("negotiates the paid protocol and lists exactly fourteen declared tools", async () => {
  const initialized = await post({
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: { protocolVersion: "2026-07-28" },
  });
  expect(((await initialized.json()) as any).result.protocolVersion).toBe("2026-07-28");

  const response = await post({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} });
  const tools = ((await response.json()) as any).result.tools;
  expect(tools.map((tool: any) => tool.name)).toEqual([
    "get_coverage",
    "resolve_entity",
    "get_portfolio",
    "get_trademark",
    "search_trademarks",
    "find_representative",
    "get_firm_profile",
    "create_watch",
    "list_rep_needed_signals",
    "list_prospects",
    "client_movement",
    "competitor_share",
    "create_quote_requests",
    "create_mark_watch",
  ]);
  expect(tools.find((tool: any) => tool.name === "create_watch").annotations.readOnlyHint).toBe(false);
  expect(tools.find((tool: any) => tool.name === "create_watch").annotations.idempotentHint).toBe(true);
});

it("requires the explicit API-key adapter credential for paid reads", async () => {
  const response = await post({
    jsonrpc: "2.0",
    id: 3,
    method: "tools/call",
    params: { name: "get_firm_profile", arguments: { representative_id: "firm:42" } },
  });
  const result = ((await response.json()) as any).result;
  expect(result.isError).toBe(true);
  expect(result.structuredContent.error.code).toBe("api_key_required");
  expect(response.headers.get("cache-control")).toBe("private, no-store");
});

it("applies the edge limiter before paid API-key validation", async () => {
  const limit = vi.fn(async () => ({ success: false }));
  const response = await post(
    {
      jsonrpc: "2.0",
      id: 31,
      method: "tools/call",
      params: { name: "get_firm_profile", arguments: { representative_id: "firm:42" } },
    },
    { "cf-connecting-ip": "203.0.113.7", "x-iprate-api-key": "bogus" },
    { ...env, RATE_LIMITER: { limit } },
  );
  expect(response.status).toBe(429);
  expect(limit).toHaveBeenCalledWith({ key: "data-api:203.0.113.7" });
  expect(((await response.json()) as any).status).toBe("rate_limited");
});

it("forwards only to the fixed Data API and preserves idempotency", async () => {
  const upstream = vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify({ data: { watch_id: 7 }, meta: { result_state: "complete" } }), {
      status: 201,
      headers: { "content-type": "application/json" },
    }),
  );
  const response = await post(
    {
      jsonrpc: "2.0",
      id: 4,
      method: "tools/call",
      params: {
        name: "create_watch",
        arguments: {
          target_type: "right",
          target_id: "tm_example",
          event_types: ["opposition"],
          idempotency_key: "watch-7",
        },
      },
    },
    { "x-iprate-api-key": "ipk_test_secret" },
  );
  const result = ((await response.json()) as any).result;
  expect(result.isError).toBe(false);
  expect(result.structuredContent.data.watch_id).toBe(7);
  const [url, init] = upstream.mock.calls[0];
  expect(String(url)).toBe("https://app.iprate.eu/api/v1/watches");
  expect(new Headers(init?.headers).get("x-api-key")).toBe("ipk_test_secret");
  expect(new Headers(init?.headers).get("idempotency-key")).toBe("watch-7");
  expect(JSON.parse(String(init?.body))).toEqual({
    target: { type: "right", id: "tm_example" },
    event_types: ["opposition"],
    delivery: "poll",
  });
});

it("signs the end-client address for the REST pre-authentication limiter", async () => {
  const upstream = vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify({ data: null, meta: { result_state: "not_found" } }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  );
  const response = await post(
    {
      jsonrpc: "2.0",
      id: 6,
      method: "tools/call",
      params: { name: "get_firm_profile", arguments: { representative_id: "firm:42" } },
    },
    { "cf-connecting-ip": "203.0.113.9", "x-iprate-api-key": "ipk_test_secret" },
    { ...env, DATA_API_WORKER_SECRET: "worker-secret" },
  );
  expect(response.status).toBe(200);
  const headers = new Headers(upstream.mock.calls[0][1]?.headers);
  expect(headers.get(WORKER_CLIENT_IP_HEADER)).toBe("203.0.113.9");
  const timestamp = headers.get(WORKER_TIMESTAMP_HEADER) ?? "";
  expect(timestamp).toMatch(/^\d+$/);
  expect(Math.abs(Number(timestamp) - Math.floor(Date.now() / 1000))).toBeLessThan(5);
  expect(headers.get(WORKER_SIGNATURE_HEADER)).toBe(
    await signClientAddress("worker-secret", timestamp, "203.0.113.9"),
  );
});

it("matches the REST signature vector and sends no relay headers without the secret", async () => {
  expect(await signClientAddress("worker-secret", "1789300000", "203.0.113.9")).toBe(
    "5b04a71da76e0b31fed545193cc80760ac4e6adae79021e7348fa4811a5d1527",
  );
  const upstream = vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response("{}", { status: 200, headers: { "content-type": "application/json" } }),
  );
  await post(
    {
      jsonrpc: "2.0",
      id: 7,
      method: "tools/call",
      params: { name: "get_firm_profile", arguments: { representative_id: "firm:42" } },
    },
    { "cf-connecting-ip": "203.0.113.9", "x-iprate-api-key": "ipk_test_secret" },
  );
  const headers = new Headers(upstream.mock.calls[0][1]?.headers);
  expect(headers.get(WORKER_CLIENT_IP_HEADER)).toBeNull();
  expect(headers.get(WORKER_TIMESTAMP_HEADER)).toBeNull();
  expect(headers.get(WORKER_SIGNATURE_HEADER)).toBeNull();
});

it("rejects an untrusted browser Origin before MCP processing", async () => {
  const response = await post(
    { jsonrpc: "2.0", id: 5, method: "tools/list", params: {} },
    { origin: "https://attacker.example" },
  );
  expect(response.status).toBe(403);
  expect(await response.json()).toEqual({ error: "origin_not_allowed" });
});
