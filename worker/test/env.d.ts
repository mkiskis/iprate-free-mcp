// Minimal ambient declaration for the vitest-pool-workers runtime module.
declare module "cloudflare:test" {
  import type { Env } from "../src/release";

  export const env: Env;
}
