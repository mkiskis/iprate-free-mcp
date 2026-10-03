// Display names and query aliases for the jurisdictions IPRATE releases cover.
// Used only by search to recognise a country in free text and by fetch to
// title documents; the set of covered jurisdictions always comes from the
// release manifest, never from this table.

import { normaliseText } from "./normalize";

const JURISDICTIONS: Record<string, { name: string; aliases: string[] }> = {
  AT: { name: "Austria", aliases: ["austrian", "osterreich"] },
  BE: { name: "Belgium", aliases: ["belgian", "belgie", "belgique", "belgien"] },
  BG: { name: "Bulgaria", aliases: ["bulgarian", "българия"] },
  CH: { name: "Switzerland", aliases: ["swiss", "schweiz", "suisse", "svizzera"] },
  CY: { name: "Cyprus", aliases: ["cypriot", "κυπρος"] },
  CZ: { name: "Czechia", aliases: ["czech republic", "czech", "cesko", "ceska republika"] },
  DE: { name: "Germany", aliases: ["german", "deutschland"] },
  DK: { name: "Denmark", aliases: ["danish", "danmark"] },
  EE: { name: "Estonia", aliases: ["estonian", "eesti"] },
  ES: { name: "Spain", aliases: ["spanish", "espana"] },
  FI: { name: "Finland", aliases: ["finnish", "suomi"] },
  FR: { name: "France", aliases: ["french"] },
  GB: { name: "United Kingdom", aliases: ["uk", "great britain", "britain", "british", "england", "scotland", "wales"] },
  GR: { name: "Greece", aliases: ["greek", "hellas", "ελλαδα"] },
  HR: { name: "Croatia", aliases: ["croatian", "hrvatska"] },
  HU: { name: "Hungary", aliases: ["hungarian", "magyarorszag"] },
  IE: { name: "Ireland", aliases: ["irish", "eire"] },
  IS: { name: "Iceland", aliases: ["icelandic"] },
  IT: { name: "Italy", aliases: ["italian", "italia"] },
  LI: { name: "Liechtenstein", aliases: [] },
  LT: { name: "Lithuania", aliases: ["lithuanian", "lietuva", "lietuvoje", "lietuvos"] },
  LU: { name: "Luxembourg", aliases: ["luxembourgish", "luxemburg", "letzebuerg"] },
  LV: { name: "Latvia", aliases: ["latvian", "latvija", "latvijas"] },
  MT: { name: "Malta", aliases: ["maltese"] },
  NL: { name: "Netherlands", aliases: ["the netherlands", "holland", "dutch", "nederland"] },
  NO: { name: "Norway", aliases: ["norwegian", "norge"] },
  PL: { name: "Poland", aliases: ["polish", "polska", "polsce"] },
  PT: { name: "Portugal", aliases: ["portuguese"] },
  RO: { name: "Romania", aliases: ["romanian"] },
  SE: { name: "Sweden", aliases: ["swedish", "sverige"] },
  SI: { name: "Slovenia", aliases: ["slovenian", "slovene", "slovenija"] },
  SK: { name: "Slovakia", aliases: ["slovak", "slovensko"] },
};

export function jurisdictionName(code: string): string {
  return JURISDICTIONS[code]?.name ?? code;
}

export function isKnownJurisdiction(code: string): boolean {
  return code in JURISDICTIONS;
}

// [normalised phrase, code], longest phrase first so "czech republic" wins
// over "czech" and "the netherlands" over "netherlands".
export const JURISDICTION_PHRASES: Array<[string, string]> = (() => {
  const phrases = new Map<string, string>();
  for (const [code, entry] of Object.entries(JURISDICTIONS)) {
    for (const phrase of [entry.name, ...entry.aliases]) {
      const key = normaliseText(phrase);
      if (key) phrases.set(key, code);
    }
  }
  return [...phrases.entries()].sort((a, b) => b[0].length - a[0].length);
})();
