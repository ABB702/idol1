import { contextBridge, ipcRenderer } from "electron";
contextBridge.exposeInMainWorld("idol", {
  request: (method: string, path: string, body?: unknown) =>
    ipcRenderer.invoke("api", method, path, body),
  saveKey: (key: string) => ipcRenderer.invoke("save-key", key),
  openExternal: (url: string) => ipcRenderer.invoke("open-external", url),
  startChat: (id: string, payload: unknown) =>
    ipcRenderer.invoke("chat-start", id, payload),
  cancelChat: (id: string) => ipcRenderer.invoke("chat-cancel", id),
  onChat: (callback: (event: unknown) => void) => {
    const listener = (_event: unknown, data: unknown) => callback(data);
    ipcRenderer.on("chat-event", listener);
    return () => ipcRenderer.removeListener("chat-event", listener);
  },
  onBackendError: (callback: (message: string) => void) => {
    const listener = (_event: unknown, message: string) => callback(message);
    ipcRenderer.on("backend-error", listener);
    return () => ipcRenderer.removeListener("backend-error", listener);
  },
});
