import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { fileURLToPath } from "node:url";
export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  base: "./",
  plugins: [vue()],
  build: { outDir: "../dist-frontend", emptyOutDir: true },
  server: { host: "127.0.0.1", port: 5173, strictPort: true },
});
