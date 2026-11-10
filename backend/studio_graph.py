"""Local Studio adapter. Agent Server owns checkpoints; application data is isolated."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from langchain_core.runnables import RunnableConfig

from idol.db import Database
from idol.graph import ChatGraph
from idol.knowledge import KnowledgeService
from idol.llm import LLMClient

ROOT = Path(__file__).resolve().parent


@asynccontextmanager
async def make_graph(config: RunnableConfig):
    directory = Path(os.environ.get("IDOL_STUDIO_DATA_DIR", ROOT / ".data" / "studio")).resolve()
    production = Path(os.environ.get("APPDATA", "")) / "idol-knowledge-workbench" / "data"
    if directory == production.resolve():
        raise ValueError("Studio 必须使用独立数据目录，请先导入知识库副本。")
    db = await asyncio.to_thread(Database, directory / "idol.sqlite3")
    overrides = {
        key: os.environ[env]
        for key, env in (
            ("base_url", "IDOL_STUDIO_BASE_URL"),
            ("model", "IDOL_STUDIO_MODEL"),
            ("evaluation_model", "IDOL_STUDIO_EVALUATION_MODEL"),
        )
        if os.environ.get(env)
    }
    if overrides:
        await asyncio.to_thread(db.update_settings, overrides)
    llm = LLMClient(db, os.environ.get("IDOL_API_KEY", ""))
    knowledge = await asyncio.to_thread(KnowledgeService, db, directory)
    try:
        chat = await asyncio.to_thread(ChatGraph, db, knowledge, llm)
        # No SQLite checkpointer here: the development server supplies its own.
        yield chat.graph
    finally:
        await knowledge.close()
        await llm.close()
