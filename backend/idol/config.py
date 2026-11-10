import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("IDOL_DATA_DIR", ".data")).resolve())
    token: str = field(default_factory=lambda: os.environ.get("IDOL_TOKEN") or secrets.token_urlsafe(32))
    api_key: str = field(default_factory=lambda: os.environ.get("IDOL_API_KEY", ""))
    port: int = field(default_factory=lambda: int(os.environ.get("IDOL_PORT", "18765")))
    auto_update: bool = field(default_factory=lambda: os.environ.get("IDOL_AUTO_UPDATE", "1") == "1")


DEFAULT_SETTINGS = {
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash",
    "evaluation_model": "",
    "auto_update": True,
    "embedding_model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "timezone": "Asia/Shanghai",
    "update_time": "09:00",
}
