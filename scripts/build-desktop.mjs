import { build } from "esbuild";
await build({
  entryPoints: ["desktop/main.ts"],
  bundle: true,
  platform: "node",
  format: "cjs",
  external: ["electron"],
  outfile: "dist-desktop/main.cjs",
});
await build({
  entryPoints: ["desktop/preload.ts"],
  bundle: true,
  platform: "node",
  format: "cjs",
  external: ["electron"],
  outfile: "dist-desktop/preload.cjs",
});
