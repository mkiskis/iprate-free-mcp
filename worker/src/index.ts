// Public entry: anonymous rate control, CORS, liveness, and the MCP path.
// Serving model: Cloudflare Worker over one immutable R2 release — no
// database, no application-API fallback, no origin server.

import { handleMcpPost } from "./protocol";
import { type Env, StaticAssetError, loadRelease } from "./release";
import { SERVER_VERSION } from "./service";

const MAX_BODY_BYTES = 256 * 1024;
const ALLOWED_ORIGINS = new Set([
  "https://mcp.iprate.eu",
  "https://iprate.eu",
  "https://chatgpt.com",
  "https://claude.ai",
]);
const LOCAL_ORIGIN = /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/;

function corsHeaders(request: Request): Record<string, string> {
  const origin = request.headers.get("origin");
  if (!origin || !(ALLOWED_ORIGINS.has(origin) || LOCAL_ORIGIN.test(origin))) return {};
  return {
    "access-control-allow-origin": origin,
    vary: "origin",
    "access-control-allow-methods": "GET, POST, OPTIONS",
    "access-control-allow-headers": "content-type, accept, mcp-protocol-version, mcp-session-id, last-event-id",
    "access-control-max-age": "86400",
  };
}

function jsonResponse(
  status: number,
  payload: unknown,
  extraHeaders: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json", ...extraHeaders },
  });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const cors = corsHeaders(request);
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors });
    }

    if (url.pathname === "/healthz") {
      try {
        const release = await loadRelease(env);
        return jsonResponse(
          200,
          {
            status: "ok",
            release_id: release.releaseId,
            as_of: release.asOf,
            server_version: SERVER_VERSION,
          },
          cors,
        );
      } catch (error) {
        return jsonResponse(
          503,
          {
            status: "source_unavailable",
            error_type: error instanceof StaticAssetError ? "StaticAssetError" : "Error",
            server_version: SERVER_VERSION,
          },
          cors,
        );
      }
    }

    if (url.pathname !== "/mcp") {
      return jsonResponse(404, { error: "not_found" }, cors);
    }
    if (request.method !== "POST") {
      return new Response(null, { status: 405, headers: { allow: "POST, OPTIONS", ...cors } });
    }

    if (env.RATE_LIMITER) {
      const key = request.headers.get("cf-connecting-ip") ?? "unknown";
      const { success } = await env.RATE_LIMITER.limit({ key });
      if (!success) {
        return jsonResponse(
          429,
          {
            status: "rate_limited",
            message: "Anonymous rate limit exceeded; retry after the indicated delay.",
            retry_after_seconds: 60,
          },
          { "retry-after": "60", ...cors },
        );
      }
    }

    const declaredLength = Number(request.headers.get("content-length") ?? "0");
    if (declaredLength > MAX_BODY_BYTES) {
      return jsonResponse(413, { error: "request_too_large" }, cors);
    }
    const raw = await request.arrayBuffer();
    if (raw.byteLength > MAX_BODY_BYTES) {
      return jsonResponse(413, { error: "request_too_large" }, cors);
    }

    let body: unknown;
    try {
      body = JSON.parse(new TextDecoder().decode(raw));
    } catch {
      return jsonResponse(
        400,
        { jsonrpc: "2.0", id: null, error: { code: -32700, message: "Parse error" } },
        cors,
      );
    }

    const { status, payload } = await handleMcpPost(env, body);
    if (payload === null) {
      return new Response(null, { status, headers: cors });
    }
    return jsonResponse(status, payload, cors);
  },
};
