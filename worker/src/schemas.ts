// Output schemas advertised in tools/list. The four bounded tools share one
// envelope; data fields are declared but not required because their presence
// depends on status. search and fetch use the fixed shapes ChatGPT deep
// research and company knowledge expect.

export const TOOL_STATUSES = [
  "ok",
  "no_results",
  "ambiguous",
  "not_public",
  "not_covered",
  "source_unavailable",
  "invalid_request",
];

const text = { type: "string" };
const nullableText = { type: ["string", "null"] };
const nullableNumber = { type: ["number", "null"] };
const code = { type: ["string", "number", "null"] };
const textList = { type: "array", items: text };
const message = { type: "string", description: "Explanation when status is not ok." };

const cohort = {
  type: "object",
  properties: {
    jurisdiction: text,
    right_type: text,
    tier: text,
    window: text,
    published_rating: {
      type: "object",
      description: "Rating copied from the release: score, tier code (Q1 Elite to Q4 Select), confidence grade, rank.",
      properties: { score: nullableNumber, tier: nullableText, confidence: nullableText, rank: nullableNumber },
    },
    released_activity: {
      type: "object",
      properties: {
        case_units: nullableNumber,
        volume_per_year: nullableNumber,
        registration_rate: nullableNumber,
        time_to_grant_days: nullableNumber,
        total_firms_filing: nullableNumber,
      },
    },
    top_classes: {
      type: ["array", "null"],
      items: {
        type: "object",
        properties: { class_code: code, class_label: nullableText, share_pct: nullableNumber, rank: nullableNumber },
      },
    },
    leading_clients: {
      type: ["array", "null"],
      items: {
        type: "object",
        properties: { quoted_name: nullableText, share_pct: nullableNumber, rank: nullableNumber },
      },
    },
  },
};

function envelope(dataProperties: Record<string, unknown>): Record<string, unknown> {
  return {
    type: "object",
    properties: {
      status: { type: "string", enum: TOOL_STATUSES },
      data: {
        type: "object",
        description: "Tool result. Which fields are present depends on status.",
        properties: dataProperties,
      },
      as_of: { ...nullableText, description: "Generation time of the selected static release." },
      release_id: nullableText,
      coverage: {
        type: "object",
        properties: {
          hold_state: text,
          jurisdiction: nullableText,
          right_type: nullableText,
          tier: nullableText,
          static_assets: textList,
          exclusions: textList,
          availability: text,
          error_type: text,
        },
      },
      limitations: textList,
      source_urls: { ...textList, description: "Public URLs to cite." },
      links: {
        type: "object",
        properties: { methodology: text, explore: text, request_analysis: text },
      },
      server_version: text,
    },
    required: [
      "status",
      "data",
      "as_of",
      "release_id",
      "coverage",
      "limitations",
      "source_urls",
      "links",
      "server_version",
    ],
  };
}

export const FIND_OUTPUT = envelope({
  items: {
    type: "array",
    items: {
      type: "object",
      properties: {
        representative_type: text,
        representative_id: { type: "number" },
        quoted_name: nullableText,
        home_country_code: nullableText,
        city: nullableText,
        matching_cohort: cohort,
        profile_url: text,
        text_provenance: text,
      },
    },
  },
  message,
});

export const PROFILE_OUTPUT = envelope({
  representative_type: text,
  representative_id: { type: "number" },
  quoted_name: nullableText,
  slug: text,
  home_country_code: nullableText,
  city: nullableText,
  released_cohorts: { type: "array", items: cohort },
  profile_url: text,
  text_provenance: text,
  candidate_types: textList,
  message,
});

export const MARKET_OUTPUT = envelope({
  jurisdiction: text,
  right_type: text,
  tier: text,
  window: text,
  released_statistics: { type: "object" },
  leading_representatives: {
    type: "array",
    items: {
      type: "object",
      properties: {
        representative_id: nullableNumber,
        quoted_name: nullableText,
        slug: nullableText,
        city: nullableText,
        home_country_code: nullableText,
        score_tier: nullableText,
        confidence_grade: nullableText,
        volume_per_year: nullableNumber,
        registration_rate: nullableNumber,
        time_to_grant_days: nullableNumber,
        top_class: code,
        profile_url: text,
      },
    },
  },
  message,
});

export const COVERAGE_OUTPUT = envelope({
  hold_state: text,
  cohorts: {
    type: "array",
    items: {
      type: "object",
      properties: {
        jurisdiction: text,
        right_type: text,
        tier: text,
        windows: textList,
        run_id: nullableNumber,
        published_at: nullableText,
        status: text,
      },
    },
  },
  published_profile_counts: { type: "object" },
  holdings: { type: "object" },
  supported_field_groups: textList,
  message,
});

export const SEARCH_OUTPUT = {
  type: "object",
  properties: {
    results: {
      type: "array",
      maxItems: 10,
      items: {
        type: "object",
        properties: {
          id: { type: "string", description: "Document id to pass to fetch." },
          title: text,
          url: { type: "string", description: "Public IPRATE page to cite." },
        },
        required: ["id", "title", "url"],
      },
    },
  },
  required: ["results"],
};

export const FETCH_OUTPUT = {
  type: "object",
  properties: {
    id: text,
    title: text,
    text: { type: "string", description: "Full document text." },
    url: { type: "string", description: "Public IPRATE page to cite." },
    metadata: {
      type: "object",
      properties: {
        document_type: text,
        release_id: text,
        as_of: text,
        data_sources: text,
        methodology: text,
      },
    },
  },
  required: ["id", "title", "text", "url"],
};
