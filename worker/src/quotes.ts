// Free tools for requests for quotes and opposed cases (IPRATE expansion plan
// 2026-10-25, P1 stage 3 and P2). Both call public IPRATE routes. Neither takes
// personal data: the client adds contact details on iprate.eu, so the plugin
// never handles them and never links to a purchase.

import {
  WORKER_CLIENT_IP_HEADER,
  WORKER_SIGNATURE_HEADER,
  WORKER_TIMESTAMP_HEADER,
  signClientAddress,
} from "./data-api";
import type { ToolOutput } from "./research";
import type { Env } from "./release";

const API_BASE = "https://app.iprate.eu/api/v1";
const PROCEEDINGS = [
  "filing", "opposition_filing", "opposition_defence", "renewal", "assignment", "enforcement", "representation", "other",
];

export function quotesLive(env: Env): boolean {
  return env.QUOTES_LIVE === "true";
}

// Every relayed call leaves the Worker from one address. The signed end-client
// address lets the IPRATE rate limits count each user on their own.
async function relayHeaders(env: Env, clientIp: string | null | undefined): Promise<Record<string, string>> {
  const address = clientIp && clientIp.length <= 64 && !/\s/.test(clientIp) ? clientIp : "unknown";
  if (!env.DATA_API_WORKER_SECRET) return {};
  const timestamp = String(Math.floor(Date.now() / 1000));
  return {
    [WORKER_CLIENT_IP_HEADER]: address,
    [WORKER_TIMESTAMP_HEADER]: timestamp,
    [WORKER_SIGNATURE_HEADER]: await signClientAddress(env.DATA_API_WORKER_SECRET, timestamp, address),
  };
}

function failure(message: string): ToolOutput {
  return { isError: true, text: message, structured: { error: message } };
}

export const PREPARE_INPUT = {
  type: "object",
  properties: {
    jurisdictions: {
      type: "array",
      items: { type: "string", pattern: "^(EUIPO|BX|[A-Za-z]{2})$" },
      minItems: 1,
      maxItems: 10,
      description: "EUIPO, BX (Benelux) or two-letter country codes.",
    },
    proceeding: { type: "string", enum: PROCEEDINGS },
    classes: { type: "array", items: { type: "integer", minimum: 1, maximum: 45 }, minItems: 1, maxItems: 45 },
    deadline: { type: "string", format: "date" },
    language: { type: "string", maxLength: 32 },
    description: {
      type: "string",
      maxLength: 1500,
      description: "The matter in a few sentences. Leave out names, emails and phone numbers.",
    },
    case_office: { type: "string", enum: ["EUIPO", "GB", "BX"] },
    case_number: { type: "string", maxLength: 32 },
  },
  required: ["jurisdictions", "proceeding", "classes"],
  additionalProperties: false,
};

export const PREPARE_OUTPUT = {
  type: "object",
  properties: {
    url: { type: "string" },
    firms: { type: "array", items: { type: "object" } },
    ordering_url: { type: "string" },
  },
};

export const CASE_INPUT = {
  type: "object",
  properties: {
    office: { type: "string", enum: ["EUIPO", "GB", "BX"] },
    application_number: { type: "string", minLength: 1, maxLength: 32 },
  },
  required: ["office", "application_number"],
  additionalProperties: false,
};

export const CASE_OUTPUT = {
  type: "object",
  properties: {
    needs_representative: { type: ["boolean", "null"] },
    obligation: { type: ["string", "null"] },
    quote_request_url: { type: "string" },
  },
};

export async function prepareQuoteRequest(
  env: Env,
  args: Record<string, unknown>,
  clientIp: string | null = null,
): Promise<ToolOutput> {
  if (!Array.isArray(args.jurisdictions) || !Array.isArray(args.classes) || typeof args.proceeding !== "string") {
    return failure("jurisdictions, proceeding and classes are required");
  }
  const response = await fetch(`${API_BASE}/quotes/briefs`, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json", ...(await relayHeaders(env, clientIp)) },
    body: JSON.stringify({ ...args, right_type: "trademark", source: "mcp" }),
    redirect: "manual",
  });
  const body = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  if (!response.ok) {
    return failure(typeof body.detail === "string" ? body.detail : `IPRATE answered ${response.status}`);
  }
  const structured = { url: body.url, firms: body.firms, ordering_url: body.ordering_url };
  return {
    structured,
    text:
      `The request is ready. Give the user this link to add contact details and send it: ${String(body.url)}. ` +
      "The matched firms are listed in firms. The request is free for the user.",
  };
}

export async function checkCaseNeedsRep(
  env: Env,
  args: Record<string, unknown>,
  clientIp: string | null = null,
): Promise<ToolOutput> {
  const office = String(args.office || "").toUpperCase();
  const number = String(args.application_number || "").trim();
  if (!["EUIPO", "GB", "BX"].includes(office) || !number) return failure("office and application_number are required");
  const response = await fetch(`${API_BASE}/cases/${office}/${encodeURIComponent(number)}`, {
    headers: { accept: "application/json", ...(await relayHeaders(env, clientIp)) },
    redirect: "manual",
  });
  const body = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  if (!response.ok) return failure(`IPRATE answered ${response.status}`);
  return { structured: body };
}
