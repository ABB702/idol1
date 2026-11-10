import { spawn } from "node:child_process";
import { createServer } from "vite";
import electron from "electron";
import "./build-desktop.mjs";
const server = await createServer({ configFile: "frontend/vite.config.ts" });
await server.listen();
const child = spawn(electron, ["."], {
  stdio: "inherit",
  env: { ...process.env, IDOL_DEV_URL: "http://127.0.0.1:5173" },
});
let closing = false;
async function stop() {
  if (closing) return;
  closing = true;
  child.kill();
  await server.close();
}
child.on("exit", async (code) => {
  await stop();
  process.exit(code ?? 0);
});
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
