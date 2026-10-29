// IPRATE expansion plan 2026-10-25: paid signal, intelligence, quote and mark-watch
// tools, and the free quote tools that stay hidden until QUOTES_LIVE.
import { env } from "cloudflare:test";
import { afterEach, expect, it, vi } from "vitest";

import worker from "../src/index";

async function rpc(path: string, body: unknown, headers: Record<string, string> = {}, envOverride: unknown = env) {
  const response = await worker.fetch(
    new Request(`https://mcp.iprate.eu${path}`, {
      method: "POST",
      headers: { "content-type": "application/json", ...headers },
      body: JSON.stringify(body),
    }),
    envOverride as never,
  );
  return (await response.json()) as any;
}

function upstream(body: unknown, status = 200) {
  return vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }),
  );
}

afterEach(() => vi.restoreAllMocks());

it("maps list_rep_needed_signals to the signals route with its filters", async () => {
  const call = upstream({ data: { signals: [] }, meta: { result_state: "no_results" } });
  const out = await rpc("/data-api/mcp", {
    jsonrpc: "2.0", id: 1, method: "tools/call",
    params: { name: "list_rep_needed_signals", arguments: { office: "EUIPO", nice_class: 9, page_size: 10 } },
  }, { "x-iprate-api-key": "ipk_test" });
  expect(out.result.isError).toBe(false);
  expect(String(call.mock.calls[0][0])).toBe("https://app.iprate.eu/api/v1/signals?office=EUIPO&nice_class=9&page_size=10");
});

it("posts create_quote_requests with the contact and the idempotency header", async () => {
  const call = upstream({ data: { invited: 3 } }, 201);
  await rpc("/data-api/mcp", {
    jsonrpc: "2.0", id: 2, method: "tools/call",
    params: {
      name: "create_quote_requests",
      arguments: {
        jurisdictions: ["DE", "EUIPO"], proceeding: "filing", classes: [9],
        contact: { name: "Ann", email: "ann@example.com" }, idempotency_key: "q-1",
      },
    },
  }, { "x-iprate-api-key": "ipk_test" });
  const [url, init] = call.mock.calls[0];
  expect(String(url)).toBe("https://app.iprate.eu/api/v1/quote-requests");
  expect(init?.method).toBe("POST");
  expect(new Headers(init?.headers).get("idempotency-key")).toBe("q-1");
  expect(JSON.parse(String(init?.body))).toEqual({
    jurisdictions: ["DE", "EUIPO"], proceeding: "filing", classes: [9], contact: { name: "Ann", email: "ann@example.com" },
  });
});

it("routes the intelligence tools to the key holder's own firm endpoints", async () => {
  const call = upstream({ data: {} });
  for (const [name, path] of [
    ["list_prospects", "/intelligence/prospects"],
    ["client_movement", "/intelligence/client-movement"],
    ["competitor_share", "/intelligence/competitor-share"],
  ]) {
    await rpc("/data-api/mcp", { jsonrpc: "2.0", id: 3, method: "tools/call", params: { name, arguments: {} } },
      { "x-iprate-api-key": "ipk_test" });
    expect(String(call.mock.calls.at(-1)![0])).toBe(`https://app.iprate.eu/api/v1${path}`);
  }
});

it("routes the enforcer tools to the Data API", async () => {
  const call = upstream({ data: {} });
  for (const [name, args, path] of [
    ["list_enforcers", { office: "GB" }, "/enforcers?office=GB"],
    ["get_enforcer_profile", { name: "Snap Inc." }, "/enforcers/profile?name=Snap+Inc."],
    ["get_enforcement_benchmark", { names: ["Snap Inc.", "Meta Platforms, Inc."] },
      "/enforcers/benchmark?names=Snap+Inc.%3BMeta+Platforms%2C+Inc."],
  ] as const) {
    await rpc("/data-api/mcp", { jsonrpc: "2.0", id: 3, method: "tools/call", params: { name, arguments: args } },
      { "x-iprate-api-key": "ipk_test" });
    expect(String(call.mock.calls.at(-1)![0])).toBe(`https://app.iprate.eu/api/v1${path}`);
  }
});

it("hides the free quote tools until QUOTES_LIVE is true", async () => {
  const hidden = await rpc("/mcp", { jsonrpc: "2.0", id: 4, method: "tools/list", params: {} });
  expect(hidden.result.tools.map((t: any) => t.name)).not.toContain("prepare_quote_request");
  const shown = await rpc("/mcp", { jsonrpc: "2.0", id: 5, method: "tools/list", params: {} }, {},
    { ...env, QUOTES_LIVE: "true" });
  const names = shown.result.tools.map((t: any) => t.name);
  expect(names).toContain("prepare_quote_request");
  expect(names).toContain("check_case_needs_rep");
});

it("prepare_quote_request posts a brief marked as coming from MCP and returns the link", async () => {
  const call = upstream({ token: "t1", url: "https://iprate.eu/quotes/open/?t=t1", firms: [{ name: "Firm" }],
    ordering_url: "https://iprate.eu/how-results-are-ordered/" }, 201);
  const out = await rpc("/mcp", {
    jsonrpc: "2.0", id: 6, method: "tools/call",
    params: { name: "prepare_quote_request", arguments: { jurisdictions: ["EUIPO"], proceeding: "filing", classes: [9] } },
  }, {}, { ...env, QUOTES_LIVE: "true" });
  const [url, init] = call.mock.calls[0];
  expect(String(url)).toBe("https://app.iprate.eu/api/v1/quotes/briefs");
  expect(JSON.parse(String(init?.body))).toMatchObject({ source: "mcp", right_type: "trademark" });
  expect(out.result.structuredContent.url).toBe("https://iprate.eu/quotes/open/?t=t1");
  expect(out.result.structuredContent).not.toHaveProperty("token");
});

it("signs the end-client address on free quote calls when the relay secret is set", async () => {
  const call = upstream({ token: "t2", url: "https://iprate.eu/quotes/open/?t=t2", firms: [] }, 201);
  const response = await worker.fetch(
    new Request("https://mcp.iprate.eu/mcp", {
      method: "POST",
      headers: { "content-type": "application/json", "cf-connecting-ip": "198.51.100.4" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 9, method: "tools/call",
        params: { name: "prepare_quote_request", arguments: { jurisdictions: ["DE"], proceeding: "filing", classes: [9] } } }),
    }),
    { ...env, QUOTES_LIVE: "true", DATA_API_WORKER_SECRET: "worker-secret" } as never,
  );
  expect(response.status).toBe(200);
  const headers = new Headers(call.mock.calls[0][1]?.headers);
  expect(headers.get("x-iprate-client-ip")).toBe("198.51.100.4");
  expect(headers.get("x-iprate-worker-signature")).toMatch(/^[0-9a-f]{64}$/);
});
