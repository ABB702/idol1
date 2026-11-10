import { spawn } from "node:child_process";
import { existsSync, copyFileSync, constants } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const backend = path.join(root, "backend");
const python = path.join(backend, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
if (!existsSync(path.join(backend, ".env"))) {
  copyFileSync(path.join(backend, ".env.example"), path.join(backend, ".env"), constants.COPYFILE_EXCL);
}
const child = spawn(python, ["-Xfrozen_modules=off", "-m", "langgraph_cli", "dev", "--host", "127.0.0.1", "--port", "2024",
  "--allow-blocking", ...process.argv.slice(2)], {
  cwd: backend,
  stdio: "inherit",
  env: { ...process.env, PYTHONUTF8: "1", LANGSMITH_TRACING: "false", LANGGRAPH_CLI_NO_ANALYTICS: "1" },
});
child.on("error", (error) => { console.error(error.message); process.exitCode = 1; });
child.on("exit", (code) => { process.exitCode = code ?? 1; });
process.on("SIGINT", () => child.kill("SIGINT"));
process.on("SIGTERM", () => child.kill("SIGTERM"));
