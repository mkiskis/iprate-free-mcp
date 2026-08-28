// Input normalisation, ported 1:1 from the Python reference adapter
// (src/iprate/mcp/service.py). The builder normalises stored keys with the
// same rules, so query-side and index-side keys stay comparable.

export class InvalidRequest extends Error {}

export function normaliseText(value: string): string {
  const decomposed = value.normalize("NFKD");
  const stripped = decomposed.replace(/\p{M}+/gu, "");
  const folded = stripped.toLowerCase().replace(/ß/g, "ss");
  const tokens = folded.match(/[\p{L}\p{N}_]+/gu);
  return tokens ? tokens.join(" ") : "";
}

export function normaliseJurisdiction(
  value: string | null | undefined,
  allowEu = true,
): string | null {
  if (value === null || value === undefined) return null;
  const upper = String(value).trim().toUpperCase();
  if (/^[A-Z]{2}$/.test(upper) && (allowEu || upper !== "EU")) return upper;
  const expected = allowEu ? "an ISO 3166-1 alpha-2 code or EU" : "an ISO 3166-1 alpha-2 code";
  throw new InvalidRequest(`jurisdiction must be ${expected}`);
}

export function normaliseClass(value: unknown): string {
  return String(value).trim().replace(/^0+/, "") || "0";
}

export function normaliseClasses(values: unknown): string[] {
  const classes: string[] = [];
  for (const raw of Array.isArray(values) ? values : []) {
    const value = normaliseClass(raw);
    if (!/^(?:[1-9]|[1-3][0-9]|4[0-5])$/.test(value)) {
      throw new InvalidRequest("Nice classes must be integers from 1 through 45");
    }
    if (!classes.includes(value)) classes.push(value);
  }
  if (classes.length > 5) throw new InvalidRequest("At most five Nice classes are allowed");
  return classes;
}

export function safeText(value: unknown, limit = 512): string | null {
  if (value === null || value === undefined) return null;
  const text = String(value).normalize("NFC");
  let out = "";
  for (const ch of text) {
    const code = ch.codePointAt(0) ?? 0;
    if (ch === "\t" || ch === "\n" || code >= 32) out += ch;
  }
  return out.slice(0, limit);
}
