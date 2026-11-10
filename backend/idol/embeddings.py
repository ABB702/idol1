"""Small CPU-only embeddings. Downloads require an explicit prepare action."""

import asyncio
import gc
import json
import threading
import time
from pathlib import Path

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DIMENSIONS = 384


class LocalEmbeddings:
    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self.marker = self.cache_dir / "verified-model.json"
        self._model = None
        self._lock = threading.RLock()
        self.last_used = 0.0
        self.error = None

    @property
    def ready(self):
        try:
            return json.loads(self.marker.read_text(encoding="utf-8"))["model"] == MODEL_NAME
        except (OSError, ValueError, KeyError):
            return False

    def status(self):
        return {
            "ready": self.ready,
            "loaded": self._model is not None,
            "name": MODEL_NAME,
            "device": "CPU",
            "threads": 2,
            "error": self.error,
        }

    def _load(self, allow_download=False):
        if self._model is None:
            if not allow_download and not self.ready:
                raise RuntimeError("请先在知识库页面准备本地向量模型。")
            from fastembed import TextEmbedding

            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._model = TextEmbedding(
                model_name=MODEL_NAME,
                cache_dir=str(self.cache_dir),
                threads=2,
                providers=["CPUExecutionProvider"],
                local_files_only=not allow_download,
            )
        self.last_used = time.monotonic()

    def _prepare(self):
        with self._lock:
            try:
                self._load(allow_download=True)
                vector = next(iter(self._model.embed(["本地模型验证"], batch_size=8)))
                if len(vector) != DIMENSIONS:
                    raise ValueError("向量维度不匹配")
                self.marker.write_text(
                    json.dumps({"model": MODEL_NAME, "dimensions": DIMENSIONS}), encoding="utf-8"
                )
                self.error = None
            except Exception as exc:
                self._model = None
                self.marker.unlink(missing_ok=True)
                self.error = "本地模型准备失败，请检查网络及缓存目录后重试。"
                raise RuntimeError(self.error) from exc

    async def prepare(self):
        await asyncio.to_thread(self._prepare)

    def embed_sync(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            self._load()
            # MiniLM has a short context: pool small overlapping windows, so a long
            # Chinese/code chunk does not silently lose its trailing information.
            import numpy as np

            result = []
            for text in texts:
                windows = [text[i : i + 100] for i in range(0, max(1, len(text)), 85)]
                vectors = np.asarray(list(self._model.embed(windows, batch_size=8)))
                vector = vectors.mean(axis=0)
                norm = np.linalg.norm(vector)
                result.append((vector / max(float(norm), 1e-12)).tolist())
            self.last_used = time.monotonic()
            return result

    def _release(self, force=False):
        with self._lock:
            if self._model is not None and (force or time.monotonic() - self.last_used >= 300):
                self._model = None
                gc.collect()

    async def release_idle(self):
        await asyncio.to_thread(self._release)

    async def close(self):
        await asyncio.to_thread(self._release, True)
