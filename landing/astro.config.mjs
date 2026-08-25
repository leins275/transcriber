// @ts-check
import { defineConfig } from "astro/config";

// Deployed as a GitHub Pages *project* page, so the site lives under
// /transcriber/ — `base` makes every built asset path carry that prefix.
// If a custom domain ever fronts this, drop `base` and change `site`.
export default defineConfig({
  site: "https://leins275.github.io",
  base: "/transcriber",
});
