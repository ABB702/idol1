import {
  app,
  BrowserWindow,
  ipcMain,
  safeStorage,
  shell,
  dialog,
} from "electron";
import { spawn, type ChildProcess } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile, rename } from "node:fs/promises";
import path from "node:path";
import net from "node:net";

let window: BrowserWindow | null = null;
let backend: ChildProcess | null = null;
let endpoint = "";
let quitting = false;
const token = randomBytes(32).toString("hex");
const streams = new Map<string, AbortController>();
const devURL =
  !app.isPackaged && process.env.IDOL_DEV_URL === "http://127.0.0.1:5173"
    ? process.env.IDOL_DEV_URL
    : undefined;
let ready: Promise<void>;
// Isolate development/smoke-test data without changing packaged user data.
if (!app.isPackaged && process.env.IDOL_USER_DATA_DIR)
  app.setPath("userData", path.resolve(process.env.IDOL_USER_DATA_DIR));

async function availablePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address() as net.AddressInfo;
      server.close(() => resolve(address.port));
    });
  });
}
interface CryptoStorage {
  isAsyncEncryptionAvailable?: () => Promise<boolean>;
  isEncryptionAvailable?: () => boolean;
  encryptStringAsync?: (value: string) => Promise<Buffer>;
  decryptStringAsync?: (
    value: Buffer,
  ) => Promise<{ result: string; shouldReEncrypt?: boolean } | string>;
  encryptString?: (value: string) => Buffer;
  decryptString?: (value: Buffer) => string;
}
const storage = safeStorage as unknown as CryptoStorage;
async function encryptionAvailable() {
  return storage.isAsyncEncryptionAvailable
    ? storage.isAsyncEncryptionAvailable()
    : (storage.isEncryptionAvailable?.() ?? false);
}
const keyPath = () => path.join(app.getPath("userData"), "credentials.bin");
async function readKey() {
  if (!existsSync(keyPath())) return process.env.IDOL_API_KEY ?? "";
  if (!(await encryptionAvailable()))
    throw new Error("系统加密不可用，无法读取已保存的密钥。");
  const value = await readFile(keyPath());
  if (storage.decryptStringAsync) {
    const decrypted = await storage.decryptStringAsync(value);
    return typeof decrypted === "string" ? decrypted : decrypted.result;
  }
  if (storage.decryptString) return storage.decryptString(value);
  throw new Error("系统解密接口不可用。");
}
async function startBackend() {
  const port = await availablePort();
  endpoint = `http://127.0.0.1:${port}/api`;
  const root = app.getAppPath();
  const backendDir = path.join(root, "backend");
  const bundled = path.join(
    process.resourcesPath,
    "backend",
    "idol-backend.exe",
  );
  const venv = path.join(backendDir, ".venv", "Scripts", "python.exe");
  const executable = app.isPackaged
    ? bundled
    : process.env.IDOL_PYTHON || (existsSync(venv) ? venv : "python");
  const dataDir = path.join(app.getPath("userData"), "data");
  await mkdir(dataDir, { recursive: true });
  const key = await readKey();
  backend = spawn(executable, app.isPackaged ? [] : ["-m", "idol"], {
    cwd: app.isPackaged ? path.dirname(bundled) : backendDir,
    windowsHide: true,
    stdio: ["ignore", "ignore", "pipe"],
    env: {
      ...process.env,
      IDOL_DATA_DIR: dataDir,
      IDOL_PORT: String(port),
      IDOL_TOKEN: token,
      IDOL_API_KEY: key,
      PYTHONUNBUFFERED: "1",
    },
  });
  let failure = "";
  backend.on("error", () => {
    failure = "无法启动 Python 服务，请先安装后端依赖。";
  });
  backend.on("exit", (code) => {
    failure = `本地服务已退出（${code ?? "中断"}）。请检查后端安装并重启应用。`;
    if (!quitting) window?.webContents.send("backend-error", failure);
  });
  // Drain stderr without logging provider responses or potentially sensitive content.
  backend.stderr?.on("data", () => {});
  for (let i = 0; i < 120; i++) {
    if (failure) throw new Error(failure);
    try {
      const response = await fetch(`${endpoint}/health`, {
        headers: { Authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(1000),
      });
      if (response.ok) return;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error("本地服务启动超时，请确认 Python 环境和后端依赖已安装。");
}
function trusted(event: Electron.IpcMainInvokeEvent | Electron.IpcMainEvent) {
  if (
    !window ||
    event.sender !== window.webContents ||
    event.senderFrame !== window.webContents.mainFrame
  )
    throw new Error("拒绝未授权的页面请求");
  const url = event.senderFrame?.url ?? "";
  if (devURL ? new URL(url).origin !== devURL : !url.startsWith("file://"))
    throw new Error("拒绝未授权的来源");
}
const allowed: Record<string, RegExp[]> = {
  GET: [
    /^\/overview$/,
    /^\/health$/,
    /^\/settings$/,
    /^\/conversations$/,
    /^\/conversations\/[\w-]+\/messages$/,
    /^\/knowledge\/documents$/,
    /^\/updates$/,
    /^\/reports$/,
    /^\/reports\/[\w-]+$/,
    /^\/evaluations$/,
  ],
  POST: [
    /^\/conversations$/,
    /^\/knowledge\/search$/,
    /^\/knowledge\/reindex$/,
    /^\/knowledge\/reset$/,
    /^\/knowledge\/model\/prepare$/,
    /^\/updates\/run$/,
    /^\/updates\/cancel$/,
    /^\/models\/test$/,
  ],
  PUT: [/^\/settings$/],
};
async function request(method: string, route: string, body?: unknown) {
  await ready;
  const response = await fetch(endpoint + route, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(120000),
  });
  const data = await response
    .json()
    .catch(() => ({ detail: "服务返回了无法读取的响应" }));
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail ?? data),
    );
  return data;
}
function registerIPC() {
  ipcMain.handle(
    "api",
    async (event, method: string, route: string, body: unknown) => {
      trusted(event);
      if (
        typeof route !== "string" ||
        typeof method !== "string" ||
        route.length > 4096 ||
        route.includes("\\") ||
        route.includes("#")
      )
        throw new Error("无效请求");
      const url = new URL(route, "http://localhost");
      if (
        url.origin !== "http://localhost" ||
        !(allowed[method] ?? []).some((rule) => rule.test(url.pathname))
      )
        throw new Error("接口不在允许列表中");
      if (
        url.search &&
        !(
          method === "GET" &&
          url.pathname === "/knowledge/documents" &&
          [...url.searchParams.keys()].every((key) => key === "query")
        )
      )
        throw new Error("不允许的查询参数");
      return request(method, url.pathname + url.search, body);
    },
  );
  ipcMain.handle("save-key", async (event, key: string) => {
    trusted(event);
    if (typeof key !== "string" || key.length > 2000)
      throw new Error("无效的 API Key");
    if (!(await encryptionAvailable()))
      throw new Error("系统加密不可用，密钥未保存。");
    const encrypted = storage.encryptStringAsync
      ? await storage.encryptStringAsync(key.trim())
      : storage.encryptString?.(key.trim());
    if (!encrypted) throw new Error("系统加密接口不可用，密钥未保存。");
    await mkdir(app.getPath("userData"), { recursive: true });
    await writeFile(keyPath() + ".tmp", encrypted);
    await rename(keyPath() + ".tmp", keyPath());
    await request("POST", "/credentials", { api_key: key.trim() });
    return { ok: true };
  });
  ipcMain.handle("open-external", async (event, url: string) => {
    trusted(event);
    const parsed = new URL(url);
    if (
      !["http:", "https:"].includes(parsed.protocol) ||
      parsed.username ||
      parsed.password
    )
      throw new Error("仅支持 HTTP(S) 链接");
    await shell.openExternal(parsed.href);
  });
  ipcMain.handle("chat-cancel", (event, id: string) => {
    trusted(event);
    streams.get(id)?.abort();
  });
  ipcMain.handle("chat-start", async (event, id: string, payload: unknown) => {
    trusted(event);
    if (typeof id !== "string" || !/^[-\w]{1,80}$/.test(id) || streams.size > 0)
      throw new Error("已有对话请求正在运行");
    await ready;
    const controller = new AbortController();
    streams.set(id, controller);
    const send = (type: string, data: unknown) => {
      if (!event.sender.isDestroyed())
        event.sender.send("chat-event", { id, type, data });
    };
    void (async () => {
      try {
        const response = await fetch(endpoint + "/chat", {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify(payload),
          signal: controller.signal,
        });
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(
            typeof data.detail === "string"
              ? data.detail
              : `请求失败 (${response.status})`,
          );
        }
        if (!response.body) throw new Error("服务未提供回复流");
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder
            .decode(value, { stream: true })
            .replace(/\r\n/g, "\n");
          let boundary: number;
          while ((boundary = buffer.indexOf("\n\n")) >= 0) {
            const block = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            let type = "message";
            const data: string[] = [];
            for (const line of block.split("\n")) {
              if (line.startsWith("event:")) type = line.slice(6).trim();
              if (line.startsWith("data:"))
                data.push(line.slice(5).trimStart());
            }
            if (
              data.length &&
              ["status", "token", "result", "trace", "error", "done"].includes(type)
            ) {
              try {
                send(type, JSON.parse(data.join("\n")));
              } catch {
                throw new Error("无法解析服务回复");
              }
            }
          }
        }
      } catch (error) {
        send("error", {
          message: controller.signal.aborted
            ? "已停止生成"
            : error instanceof Error
              ? error.message
              : "请求失败",
        });
      } finally {
        streams.delete(id);
        send("done", {});
      }
    })();
    return { started: true };
  });
}
function createWindow() {
  window = new BrowserWindow({
    width: 1320,
    height: 900,
    minWidth: 980,
    minHeight: 680,
    backgroundColor: "#f7f8fa",
    title: "Idol · 知识工作台",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webSecurity: true,
    },
  });
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-navigate", (event) => event.preventDefault());
  window.webContents.session.setPermissionRequestHandler(
    (_wc, _permission, callback) => callback(false),
  );
  window.on("closed", () => {
    window = null;
    for (const controller of streams.values()) controller.abort();
  });
  if (devURL) void window.loadURL(devURL);
  else
    void window.loadFile(path.join(__dirname, "../dist-frontend/index.html"));
}
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (window) {
      if (window.isMinimized()) window.restore();
      window.focus();
    }
  });
  app.whenReady().then(() => {
    ready = startBackend();
    ready.catch((error) => {
      dialog.showErrorBox("本地服务不可用", error.message);
    });
    registerIPC();
    createWindow();
  });
  app.on("window-all-closed", () => app.quit());
  app.on("before-quit", () => {
    quitting = true;
    for (const controller of streams.values()) controller.abort();
    backend?.kill();
  });
}
