interface Window {
  idol: {
    request(method: string, path: string, body?: unknown): Promise<any>;
    saveKey(key: string): Promise<{ ok: boolean }>;
    openExternal(url: string): Promise<void>;
    startChat(id: string, payload: unknown): Promise<{ started: boolean }>;
    cancelChat(id: string): Promise<void>;
    onChat(
      callback: (event: { id: string; type: string; data: any }) => void,
    ): () => void;
    onBackendError(callback: (message: string) => void): () => void;
  };
}
