import asyncio
import json
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel, Field, field_validator

from . import __version__
from .config import Config
from .db import Database, utc_now
from .llm import LLMClient, ModelError


class SettingsInput(BaseModel):
    base_url: str = Field(max_length=500)
    model: str = Field(min_length=1, max_length=200)
    evaluation_model: str = Field(default="", max_length=200)
    auto_update: bool = True

    @field_validator("base_url")
    @classmethod
    def valid_base_url(cls, value):
        parsed = urlsplit(value)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("无效模型服务地址")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        ):
            raise ValueError("请使用 HTTPS；本机服务允许 HTTP")
        return value.rstrip("/")


class CredentialsInput(BaseModel):
    api_key: str = Field(max_length=2000)


class ChatInput(BaseModel):
    conversation_id: str | None = Field(default=None, max_length=100)
    message: str = Field(min_length=1, max_length=12000)
    mode: Literal["auto", "rag"] = "auto"

    @field_validator("message")
    @classmethod
    def non_empty(cls, value):
        if not value.strip():
            raise ValueError("请输入消息")
        return value.strip()


class SearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=2000)


class UpdateInput(BaseModel):
    kind: Literal["initial", "daily"] = "daily"


class ConversationInput(BaseModel):
    title: str = Field(default="新对话", min_length=1, max_length=100)


def create_app(config: Config | None = None, *, factories: dict | None = None) -> FastAPI:
    cfg = config or Config()
    factories = factories or {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from .graph import ChatGraph
        from .ingestion import UpdateManager
        from .knowledge import KnowledgeService
        from .scheduler import Scheduler

        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        db = Database(cfg.data_dir / "idol.sqlite3")
        llm = factories.get("llm", LLMClient)(db, cfg.api_key)
        knowledge = factories.get("knowledge", KnowledgeService)(db, cfg.data_dir)
        updates = factories.get("updates", UpdateManager)(db, knowledge, cfg.data_dir, llm=llm)
        state = app.state
        state.db, state.llm, state.knowledge, state.updates = db, llm, knowledge, updates
        state.operation = {"status": "idle"}
        state.operation_task = None
        state.chat_lock = asyncio.Lock()
        state.chat_tasks = set()

        async def daily_check():
            if cfg.auto_update and db.settings()["auto_update"]:
                await updates.check_today()

        scheduler = Scheduler(daily_check)

        async def idle_maintenance():
            while True:
                await asyncio.sleep(30)
                await knowledge.release_idle()

        async with AsyncSqliteSaver.from_conn_string(str(cfg.data_dir / "checkpoints.sqlite3")) as saver:
            state.graph = factories.get("graph", ChatGraph)(db, knowledge, llm, saver)
            scheduler.start()
            startup = asyncio.create_task(daily_check())
            maintenance = asyncio.create_task(idle_maintenance())
            try:
                yield
            finally:
                startup.cancel()
                with suppress(asyncio.CancelledError):
                    await startup
                await scheduler.close()
                maintenance.cancel()
                with suppress(asyncio.CancelledError):
                    await maintenance
                if state.operation_task and not state.operation_task.done():
                    state.operation_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await state.operation_task
                for task in list(state.chat_tasks):
                    task.cancel()
                await asyncio.gather(*state.chat_tasks, return_exceptions=True)
                await updates.close()
                await knowledge.close()
                await llm.close()

    app = FastAPI(title="Idol", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + cfg.token):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)

    @app.exception_handler(ModelError)
    async def model_error(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.get("/api/health")
    async def health():
        return {"ready": True, "version": __version__}

    @app.get("/api/settings")
    async def settings():
        return {**app.state.db.settings(), "api_key_configured": app.state.llm.configured}

    @app.put("/api/settings")
    async def update_settings(body: SettingsInput):
        app.state.db.update_settings(body.model_dump())
        return await settings()

    @app.post("/api/credentials")
    async def credentials(body: CredentialsInput):
        app.state.llm.api_key = body.api_key.strip()
        return {"api_key_configured": app.state.llm.configured}

    @app.post("/api/models/test")
    async def test_model():
        return await app.state.llm.test()

    @app.get("/api/overview")
    async def overview():
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        runs = app.state.updates.list_runs()
        today_run = next(
            (
                r
                for r in runs
                if r.get("day", r.get("date", r.get("run_date"))) == today and r.get("kind") == "daily"
            ),
            None,
        )
        return {
            **app.state.knowledge.stats(),
            "conversations": app.state.db.one("SELECT COUNT(*) AS n FROM conversations")["n"],
            "today_update": today_run,
            "active_update": app.state.updates.active(),
            "model": app.state.knowledge.model_status(),
            "operation": app.state.operation,
        }

    @app.get("/api/conversations")
    async def conversations():
        return {"items": app.state.db.query("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 100")}

    @app.post("/api/conversations")
    async def create_conversation(body: ConversationInput):
        row = {"id": str(uuid4()), "title": body.title, "created_at": utc_now(), "updated_at": utc_now()}
        app.state.db.execute("INSERT INTO conversations VALUES(?,?,?,?)", tuple(row.values()))
        return row

    @app.get("/api/conversations/{conversation_id}/messages")
    async def messages(conversation_id: str):
        if not app.state.db.one("SELECT id FROM conversations WHERE id=?", (conversation_id,)):
            raise HTTPException(404, "会话不存在")
        rows = app.state.db.query(
            "SELECT * FROM (SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at DESC LIMIT 200) ORDER BY created_at",
            (conversation_id,),
        )
        for row in rows:
            row["metadata"] = json.loads(row["metadata"])
        return {"items": rows}

    @app.post("/api/chat")
    async def chat(body: ChatInput):
        state = app.state
        if state.operation.get("name") == "reset" and state.operation.get("status") == "running":
            raise HTTPException(409, "正在清空知识库，请稍后再开始对话。")
        if not state.llm.configured:
            raise HTTPException(400, "请先在设置中填写 API Key。")
        if state.chat_lock.locked():
            raise HTTPException(409, "正在回答上一条消息，请稍后再试。")
        if body.conversation_id and not state.db.one(
            "SELECT id FROM conversations WHERE id=?", (body.conversation_id,)
        ):
            raise HTTPException(404, "会话不存在")
        await state.chat_lock.acquire()
        state.updates.chat_busy.set()
        conversation_id = body.conversation_id or str(uuid4())
        request_id = str(uuid4())
        now = utc_now()
        try:
            if not body.conversation_id:
                state.db.execute(
                    "INSERT INTO conversations VALUES(?,?,?,?)",
                    (conversation_id, body.message[:40], now, now),
                )
            history_rows = state.db.query(
                "SELECT role,content FROM messages WHERE conversation_id=? ORDER BY created_at DESC LIMIT 16",
                (conversation_id,),
            )
            history, budget = [], 12000
            for row in history_rows:
                if len(row["content"]) > budget:
                    break
                history.insert(0, row)
                budget -= len(row["content"])
            state.db.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?)",
                (request_id, conversation_id, "user", body.message, now, "{}"),
            )
            state.db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
        except BaseException:
            state.chat_lock.release()
            state.updates.chat_busy.clear()
            raise

        async def event_stream():
            queue = asyncio.Queue()
            partial = []
            saved = False
            latest_trace = None
            failure = None

            async def emit(event, data):
                nonlocal latest_trace
                if event == "trace":
                    latest_trace = data
                if event == "token":
                    partial.append(data.get("text", ""))
                await queue.put((event, data))

            async def run():
                nonlocal saved, failure
                try:
                    async with asyncio.timeout(300):
                        result = await state.graph.run(body.message, history, body.mode, request_id, emit)
                    result["conversation_id"] = conversation_id
                    state.db.execute(
                        "INSERT INTO messages VALUES(?,?,?,?,?,?)",
                        (
                            str(uuid4()),
                            conversation_id,
                            "assistant",
                            result["answer"],
                            utc_now(),
                            json.dumps(
                                {k: v for k, v in result.items() if k != "answer"}, ensure_ascii=False
                            ),
                        ),
                    )
                    saved = True
                    state.db.execute(
                        "UPDATE conversations SET updated_at=? WHERE id=?", (utc_now(), conversation_id)
                    )
                    await emit("result", result)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - streaming boundary cannot return an HTTP error
                    message = (
                        str(exc)
                        if isinstance(exc, ModelError)
                        else "回答未完成，请检查模型连接或知识库状态后重试。"
                    )
                    if isinstance(exc, TimeoutError):
                        message = "回答超时，请缩小问题范围后重试。"
                    failure = message
                    await emit("error", {"message": message})
                finally:
                    if not saved and (partial or latest_trace):
                        state.db.execute(
                            "INSERT INTO messages VALUES(?,?,?,?,?,?)",
                            (
                                str(uuid4()),
                                conversation_id,
                                "assistant",
                                "".join(partial),
                                utc_now(),
                                json.dumps(
                                    {
                                        "interrupted": True,
                                        "trace": latest_trace,
                                        "error": failure or "回答已中断。",
                                    },
                                    ensure_ascii=False,
                                ),
                            ),
                        )
                    await queue.put(("done", {"conversation_id": conversation_id}))

            task = asyncio.create_task(run())
            state.chat_tasks.add(task)
            try:
                yield (
                    "event: status\ndata: "
                    + json.dumps(
                        {"stage": "start", "message": "正在分析问题", "conversation_id": conversation_id},
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                while True:
                    try:
                        event, data = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                    if event == "done":
                        break
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                state.chat_tasks.discard(task)
                state.updates.chat_busy.clear()
                state.chat_lock.release()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/knowledge/documents")
    async def documents(query: str = ""):
        return {"items": app.state.knowledge.documents(query[:200])}

    @app.post("/api/knowledge/search")
    async def search(body: SearchInput):
        return {
            "items": await app.state.knowledge.search(body.query),
            "model": app.state.knowledge.model_status(),
        }

    def launch_operation(name: str, fn):
        state = app.state
        if state.operation_task and not state.operation_task.done():
            raise HTTPException(409, "已有模型或索引任务正在运行")
        if state.updates.active():
            raise HTTPException(409, "请等待更新任务结束，再执行知识库维护操作")
        state.operation = {"status": "running", "name": name, "started_at": utc_now()}
        state.updates.maintenance = True

        async def work():
            try:
                await fn()
                state.operation = {"status": "completed", "name": name, "finished_at": utc_now()}
            except asyncio.CancelledError:
                state.operation = {"status": "cancelled", "name": name}
                raise
            except Exception:  # noqa: BLE001 - background operation reports sanitized failure status
                state.operation = {
                    "status": "failed",
                    "name": name,
                    "message": (
                        "清空并重建未完成，部分数据可能已清除；请检查磁盘和模型状态，重试本操作或点击开始构建。"
                        if name == "reset"
                        else "任务失败，请检查模型缓存、网络和磁盘空间后重试。"
                    ),
                }
            finally:
                state.updates.maintenance = False

        state.operation_task = asyncio.create_task(work())
        return {"status": "running", "name": name}

    @app.post("/api/knowledge/model/prepare")
    async def prepare_model():
        return launch_operation("prepare_model", app.state.knowledge.prepare_model)

    @app.post("/api/knowledge/reindex")
    async def reindex():
        return launch_operation("reindex", app.state.knowledge.reindex)

    @app.post("/api/knowledge/reset")
    async def reset_knowledge():
        state = app.state
        if state.chat_lock.locked():
            raise HTTPException(409, "请等待对话结束后再清空知识库。")
        if not state.knowledge.model_status()["ready"]:
            raise HTTPException(409, "请先准备本地模型，再清空并重新构建。")

        async def reset_and_build():
            await state.knowledge.clear()
            state.updates.maintenance = False
            result = await state.updates.start("initial")
            if result.get("status") != "started":
                raise RuntimeError("知识库已清空，但首次构建未启动，请手动开始构建。")

        return launch_operation("reset", reset_and_build)

    @app.get("/api/updates")
    async def update_runs():
        return {"items": app.state.updates.list_runs()}

    @app.post("/api/updates/run")
    async def start_update(body: UpdateInput):
        if app.state.operation_task and not app.state.operation_task.done():
            raise HTTPException(409, "请等待本地模型/索引任务完成")
        if not app.state.knowledge.model_status()["ready"]:
            raise HTTPException(409, "请先在设置中准备本地向量模型，再启动知识库更新。")
        return await app.state.updates.start(body.kind)

    @app.post("/api/updates/cancel")
    async def cancel_update():
        await app.state.updates.cancel()
        return {"status": "cancelled"}

    @app.get("/api/reports")
    async def reports():
        return {"items": app.state.updates.list_reports()}

    @app.get("/api/reports/{report_id}")
    async def report(report_id: str):
        row = app.state.updates.get_report(report_id)
        if row is None:
            raise HTTPException(404, "日报不存在")
        return row

    @app.get("/api/evaluations")
    async def evaluations():
        return {"items": app.state.graph.evaluator.list_evaluations(limit=100)}

    return app


app = create_app()
