"""Persistent, quota-limited knowledge updates and evidence-based daily reports."""

import asyncio
import inspect
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from .scheduler import SHANGHAI, local_now
from .sources import (
    DAILY_SOURCES,
    INITIAL_SOURCES,
    SOURCES,
    TOPICS,
    SafeFetcher,
    canonical_url,
    is_article,
    rejection_reason,
    topic_matches,
)


class UpdateManager:
    def __init__(self, db, knowledge, data_dir, llm=None, *, fetcher=None, now=None):
        self.db, self.knowledge = db, knowledge
        self.data_dir = Path(data_dir)
        self.llm = llm
        self.fetcher = fetcher or SafeFetcher()
        self.now = now or local_now
        self.chat_busy = asyncio.Event()
        self.maintenance = False
        self._task = None
        self._run_id = None
        self._lock = asyncio.Lock()
        with db.transaction() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS update_runs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, day TEXT NOT NULL,
                    status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
                    message TEXT NOT NULL DEFAULT '', stats TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS crawl_queue (
                    id INTEGER PRIMARY KEY, scope TEXT NOT NULL, topic TEXT NOT NULL,
                    source TEXT NOT NULL, url TEXT NOT NULL, depth INTEGER NOT NULL,
                    discovery INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
                    UNIQUE(scope, topic, url)
                );
                CREATE TABLE IF NOT EXISTS update_budgets (
                    scope TEXT NOT NULL, topic TEXT NOT NULL, pages INTEGER NOT NULL DEFAULT 0,
                    chunks INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(scope,topic)
                );
                CREATE TABLE IF NOT EXISTS crawl_items (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, topic TEXT NOT NULL,
                    url TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '', chunk_count INTEGER NOT NULL DEFAULT 0,
                    source_kind TEXT NOT NULL, published_at TEXT, fetched_at TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS daily_reports (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, day TEXT NOT NULL,
                    title TEXT NOT NULL, markdown TEXT NOT NULL, created_at TEXT NOT NULL,
                    status TEXT NOT NULL
                );
            """)
            if "error_stage" not in {row[1] for row in conn.execute("PRAGMA table_info(crawl_items)")}:
                conn.execute("ALTER TABLE crawl_items ADD COLUMN error_stage TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE crawl_queue SET status='pending' WHERE status='fetching'")
            conn.execute(
                "UPDATE update_runs SET status='interrupted',finished_at=?,message=? WHERE status='running'",
                (self._stamp(), "应用退出时任务中断；不会补跑历史日期"),
            )
        for run in self.db.query(
            "SELECT * FROM update_runs WHERE status='interrupted' AND id NOT IN (SELECT run_id FROM daily_reports)"
        ):
            self._report(run["id"])

    def _local(self):
        return self.now().astimezone(SHANGHAI)

    def _stamp(self):
        return self._local().isoformat()

    def active(self):
        return self.db.one(
            "SELECT * FROM update_runs WHERE status='running' ORDER BY started_at DESC LIMIT 1"
        )

    def list_runs(self):
        result = self.db.query("SELECT * FROM update_runs ORDER BY started_at DESC LIMIT 100")
        for run in result:
            run["stats"] = json.loads(run["stats"])
        return result

    def list_reports(self):
        return self.db.query(
            "SELECT id,run_id,day,title,status,created_at FROM daily_reports ORDER BY created_at DESC LIMIT 100"
        )

    def get_report(self, report_id):
        return self.db.one("SELECT * FROM daily_reports WHERE id=?", (report_id,))

    def budgets(self, day=None):
        return self.db.query(
            "SELECT * FROM update_budgets WHERE scope=?", (day or self._local().date().isoformat(),)
        )

    async def check_today(self):
        day = self._local().date().isoformat()
        if self.db.one(
            "SELECT id FROM update_runs WHERE day=? AND kind='daily' AND status='completed'", (day,)
        ):
            return {"status": "already_updated", "day": day}
        ready = self.knowledge.model_status()
        if inspect.isawaitable(ready):
            ready = await ready
        if not ready.get("ready"):
            return {
                "status": "needs_model",
                "message": "请先准备本地向量模型；自动更新不会下载模型",
                "day": day,
            }
        return await self.start("daily")

    async def start(self, kind="daily"):
        if kind not in ("initial", "daily"):
            raise ValueError("更新类型必须为initial或daily")
        async with self._lock:
            if self.maintenance:
                return {"status": "maintenance", "message": "知识库维护中，请稍后再试"}
            if self._task and not self._task.done():
                return {"status": "already_running", "run": self.active()}
            model = self.knowledge.model_status()
            if inspect.isawaitable(model):
                model = await model
            if not model.get("ready"):
                return {"status": "needs_model", "message": "请先准备本地向量模型"}
            day = self._local().date().isoformat()
            if kind == "daily" and self.db.one(
                "SELECT id FROM update_runs WHERE day=? AND kind='daily' AND status='completed'", (day,)
            ):
                return {
                    "status": "already_updated",
                    "day": day,
                    "message": "今天的每日更新已完成，无需重复执行。",
                }
            run_id = str(uuid.uuid4())
            self.db.execute(
                "INSERT INTO update_runs(id,kind,day,status,started_at) VALUES(?,?,?,'running',?)",
                (run_id, kind, day, self._stamp()),
            )
            self._run_id = run_id
            self._task = asyncio.create_task(self._run(run_id, kind, day))
            return {"status": "started", "id": run_id, "run_id": run_id, "kind": kind, "day": day}

    def _enqueue(self, scope, topic, source, url, depth, discovery=False):
        self._enqueue_many([(scope, topic, source, url, depth, discovery)])

    def _enqueue_many(self, entries):
        # Daily discovery stays bounded; initial Vue discovery has no count limit.
        with self.db.transaction() as conn:
            counts = {}
            for scope, topic, source, url, depth, discovery in entries:
                key = (scope, source.key)
                if key not in counts:
                    counts[key] = conn.execute(
                        "SELECT count(*) FROM crawl_queue WHERE scope=? AND source=?", key
                    ).fetchone()[0]
                if scope != "initial" and counts[key] >= 200:
                    continue
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO crawl_queue(scope,topic,source,url,depth,discovery) VALUES(?,?,?,?,?,?)",
                    (scope, topic, source.key, canonical_url(url), depth, int(discovery)),
                ).rowcount
                counts[key] += inserted

    def _seed(self, kind, day):
        self.db.execute(
            "UPDATE crawl_queue SET status='pending' WHERE scope=? AND status='failed'",
            ("initial" if kind == "initial" else day,),
        )
        if kind == "initial":
            # Rediscover old Vue pages once so the former depth/frontier caps do not
            # leave reachable chapters missing. Existing documents are deduplicated.
            with self.db.transaction() as conn:
                if not conn.execute("SELECT 1 FROM settings WHERE key='initial_vue_markdown_v1'").fetchone():
                    conn.execute(
                        "UPDATE crawl_queue SET status='pending' WHERE scope='initial' AND source='vue'"
                    )
                    conn.execute("INSERT INTO settings(key,value) VALUES('initial_vue_markdown_v1','true')")
                if not conn.execute("SELECT 1 FROM settings WHERE key='initial_vue_unlimited_v1'").fetchone():
                    conn.execute(
                        "UPDATE crawl_queue SET status='pending' WHERE scope='initial' AND source='vue'"
                    )
                    conn.execute("INSERT INTO settings(key,value) VALUES('initial_vue_unlimited_v1','true')")
            for source in INITIAL_SOURCES:
                self._enqueue("initial", source.topic, source, source.seed, 0)
        else:
            # Rotate the discovery order daily and between topics, preserving priority.
            ordinal = datetime.fromisoformat(day).toordinal()
            for index, topic in enumerate(TOPICS):
                offset = (ordinal + index) % len(DAILY_SOURCES)
                for source in DAILY_SOURCES[offset:] + DAILY_SOURCES[:offset]:
                    self._enqueue(day, topic, source, source.seed, 0, discovery=True)

    def _budget(self, scope, topic):
        return self.db.one("SELECT * FROM update_budgets WHERE scope=? AND topic=?", (scope, topic)) or {
            "pages": 0,
            "chunks": 0,
        }

    def _reserve(self, scope, topic, limit, queue_id):
        with self.db.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO update_budgets(scope,topic) VALUES(?,?)", (scope, topic))
            updated = conn.execute(
                "UPDATE update_budgets SET pages=pages+1 WHERE scope=? AND topic=? AND (? IS NULL OR pages<?)",
                (scope, topic, limit, limit),
            ).rowcount
            if updated:
                conn.execute("UPDATE crawl_queue SET status='fetching' WHERE id=?", (queue_id,))
            return bool(updated)

    def _record(self, run_id, row, status, reason="", page=None, chunk_count=0, error_stage=""):
        source = SOURCES[row["source"]]
        self.db.execute(
            "INSERT INTO crawl_items(run_id,topic,url,title,status,reason,chunk_count,source_kind,published_at,fetched_at,summary,error_stage) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                row["topic"],
                page.url if page else row["url"],
                page.title if page else "",
                status,
                reason[:800],
                chunk_count,
                source.kind,
                page.published_at if page else None,
                self._stamp(),
                page.text[:280] if page else "",
                error_stage,
            ),
        )
        self.db.execute(
            "UPDATE crawl_queue SET status=? WHERE id=?",
            ("failed" if status == "failed" else "done", row["id"]),
        )
        if status == "failed" and error_stage == "fetch" and row["discovery"]:
            # A failed discovery URL should not be requested again for each topic.
            self.db.execute(
                "UPDATE crawl_queue SET status='done' WHERE scope=? AND url=? AND discovery=1 AND status='pending'",
                (row["scope"], row["url"]),
            )

    def _discover(self, row, page, kind, day):
        source = SOURCES[row["source"]]
        entries = []
        if kind == "initial":
            for url, title in page.links:
                if source.allows(url) and not re.search(
                    r"\.(pdf|png|jpg|svg|zip|js|css)$", url, re.IGNORECASE
                ):
                    entries.append(("initial", source.topic, source, url, row["depth"] + 1, False))
        elif row["discovery"]:
            for url, title in page.links:
                if source.allows(url, discovered=True) and is_article(source, url):
                    for topic in TOPICS:
                        if topic_matches(title, topic):
                            entries.append((day, topic, source, url, 1, False))
        if entries:
            self._enqueue_many(entries)

    async def _consume(self, run_id, row, kind, day):
        source = SOURCES[row["source"]]
        processed = False
        reserved_chunks = 0
        error_stage = "fetch"
        try:
            page = await asyncio.wait_for(
                self.fetcher.fetch(row["url"], source, discovered=bool(row["depth"])), timeout=90
            )
            self._discover(row, page, kind, day)
            if row["discovery"]:
                count = self.db.one(
                    "SELECT count(*) AS n FROM crawl_queue WHERE scope=? AND source=? AND discovery=0",
                    (day, source.key),
                )["n"]
                self._record(
                    run_id,
                    row,
                    "discovered",
                    f"已发现{count}条符合主题标题的候选文章；动态页面或登录内容不绕过",
                    page,
                )
                return
            reason = rejection_reason(page, source, row["topic"])
            if reason:
                self._record(run_id, row, "skipped", reason, page)
                return
            error_stage = "ingest"
            budget = self._budget(day, row["topic"]) if kind == "daily" else None
            remaining = TOPICS[row["topic"]][1] - budget["chunks"] if budget else None
            if kind == "daily":
                # Reserve before the cross-store write: a crash must not allow quota reuse.
                from .knowledge import split_document

                reserved_chunks = min(len(split_document(page.text)), remaining)
                self.db.execute(
                    "UPDATE update_budgets SET chunks=chunks+? WHERE scope=? AND topic=?",
                    (reserved_chunks, day, row["topic"]),
                )
            ingest = asyncio.create_task(
                self.knowledge.upsert_document(
                    url=page.url,
                    title=page.title,
                    text=page.text,
                    topic=row["topic"],
                    source_kind=source.kind,
                    published_at=page.published_at,
                    max_chunks=remaining,
                )
            )
            cancelled = False
            try:
                result = await asyncio.shield(ingest)
            except asyncio.CancelledError:
                # to_thread cannot be force-cancelled. Finish accounting for the active page.
                cancelled = True
                result = await ingest
            chunks = int(result.get("chunk_count", 0)) if result["status"] in ("added", "updated") else 0
            if kind == "daily":
                self.db.execute(
                    "UPDATE update_budgets SET chunks=chunks+? WHERE scope=? AND topic=?",
                    (chunks - reserved_chunks, day, row["topic"]),
                )
            self._record(
                run_id, row, result["status"], result.get("reason", result.get("warning", "")), page, chunks
            )
            processed = True
            if cancelled:
                raise asyncio.CancelledError
        except asyncio.CancelledError:
            if not processed:
                self._record(run_id, row, "failed", "任务取消或关闭，已消耗的访问预算保留")
            raise
        except Exception as exc:  # noqa: BLE001 - isolate failures per untrusted source
            self._record(
                run_id,
                row,
                "failed",
                f"{type(exc).__name__}: {exc}"
                + ("；已预留的chunk额度保留，避免重复写入超额" if reserved_chunks else ""),
                error_stage=error_stage,
            )

    async def _run(self, run_id, kind, day):
        status, message = "completed", ""
        try:
            if kind == "initial" and isinstance(self.fetcher, SafeFetcher):
                self.fetcher._vue_reader = None
            self._seed(kind, day)
            index = 0
            while True:
                # Never continue yesterday's work across midnight.
                if kind == "daily" and self._local().date().isoformat() != day:
                    status, message = "interrupted", "已跨日，停止昨日任务；不补跑历史日期"
                    break
                if self.chat_busy.is_set():
                    await asyncio.sleep(0.25)
                    continue
                row = None
                if kind == "initial":
                    for step in range(len(INITIAL_SOURCES)):
                        source = INITIAL_SOURCES[(index + step) % len(INITIAL_SOURCES)]
                        row = self.db.one(
                            "SELECT * FROM crawl_queue WHERE scope='initial' AND source=? AND status='pending' ORDER BY depth,id LIMIT 1",
                            (source.key,),
                        )
                        if row:
                            index = (index + step + 1) % len(INITIAL_SOURCES)
                            break
                    if row and not self._reserve("initial", "all", None, row["id"]):
                        break
                else:
                    for topic, (pages, chunks) in TOPICS.items():
                        budget = self._budget(day, topic)
                        if budget["pages"] >= pages or budget["chunks"] >= chunks:
                            continue
                        row = self.db.one(
                            "SELECT * FROM crawl_queue WHERE scope=? AND topic=? AND status='pending' ORDER BY discovery,id LIMIT 1",
                            (day, topic),
                        )
                        if row:
                            if not self._reserve(day, topic, pages, row["id"]):
                                row = None
                                continue
                            break
                if not row:
                    break
                await self._consume(run_id, row, kind, day)
                self._save_stats(run_id)
                await asyncio.sleep(0)
            failures = self.db.one(
                "SELECT count(*) AS n FROM crawl_items WHERE run_id=? AND status='failed'", (run_id,)
            )["n"]
            if failures and status == "completed":
                successes = self.db.one(
                    "SELECT count(*) AS n FROM crawl_items WHERE run_id=? AND status!='failed'", (run_id,)
                )["n"]
                status = "partial" if successes else "failed"
                counts = self._save_stats(run_id)
                message = (
                    f"访问失败 {counts['fetch_failed']} 次，入库失败 {counts['ingest_failed']} 次，"
                    f"其他失败 {counts['other_failed']} 次；详细出处见日报"
                )
        except asyncio.CancelledError:
            status, message = "cancelled", "任务已取消，预算和已完成内容保留"
        except Exception as exc:  # noqa: BLE001 - persist final status for any task failure
            status, message = "failed", f"{type(exc).__name__}: {exc}"[:800]
        finally:
            self.db.execute(
                "UPDATE update_runs SET status=?,message=?,finished_at=? WHERE id=?",
                (status, message, self._stamp(), run_id),
            )
            self._save_stats(run_id)
            self._report(run_id)

    def _save_stats(self, run_id):
        counts = {
            row["status"]: row["n"]
            for row in self.db.query(
                "SELECT status,count(*) AS n FROM crawl_items WHERE run_id=? GROUP BY status", (run_id,)
            )
        }
        counts["pages"] = sum(counts.values())
        stages = {
            row["error_stage"]: row["n"]
            for row in self.db.query(
                "SELECT error_stage,count(*) AS n FROM crawl_items WHERE run_id=? AND status='failed' GROUP BY error_stage",
                (run_id,),
            )
        }
        counts["fetch_failed"] = stages.get("fetch", 0)
        counts["ingest_failed"] = stages.get("ingest", 0)
        counts["other_failed"] = counts.get("failed", 0) - counts["fetch_failed"] - counts["ingest_failed"]
        counts["chunks"] = self.db.one(
            "SELECT coalesce(sum(chunk_count),0) AS n FROM crawl_items WHERE run_id=?", (run_id,)
        )["n"]
        self.db.execute("UPDATE update_runs SET stats=? WHERE id=?", (json.dumps(counts), run_id))
        return counts

    @staticmethod
    def _md(value):
        # Content is data: prevent scraped strings becoming arbitrary Markdown links/HTML.
        return re.sub(r"([\\`*_{}\[\]<>|])", r"\\\1", str(value)).replace("\n", " ").replace("\r", " ")

    def _report(self, run_id):
        run = self.db.one("SELECT * FROM update_runs WHERE id=?", (run_id,))
        counts = self._save_stats(run_id)
        kind = "首次构建" if run["kind"] == "initial" else "每日更新"
        title = f"{run['day']} {kind}报告"
        lines = [
            f"# {title}",
            "",
            f"状态：{run['status']}。{self._md(run['message'])}",
            f"开始：{run['started_at']}；结束：{run['finished_at'] or '未知'}（Asia/Shanghai）。",
            "",
            f"本次访问 {counts.get('pages', 0)} 页；新增 {counts.get('added', 0)} 篇，更新 {counts.get('updated', 0)} 篇，重复 {counts.get('duplicate', 0)} 篇，过滤 {counts.get('skipped', 0)} 篇，失败 {counts.get('failed', 0)} 次，发现页 {counts.get('discovered', 0)} 页；新入库 {counts.get('chunks', 0)} chunks。",
            f"失败分类：访问 {counts['fetch_failed']} 次；入库 {counts['ingest_failed']} 次；其他或旧版未分类 {counts['other_failed']} 次。失效发现入口在本次任务中不按主题重复访问；502/503/504/521最多额外重试一次。",
            "",
            "配额是访问上限，发现页与失败也计数；不为达到配额收录内容。摘要为原文摘录。",
            "",
            "## 累计预算",
            "",
        ]
        scope = "initial" if run["kind"] == "initial" else run["day"]
        for budget in self.db.query("SELECT * FROM update_budgets WHERE scope=?", (scope,)):
            limit = ("不限", None) if scope == "initial" else TOPICS[budget["topic"]]
            lines.append(
                f"- {budget['topic']}：页面 {budget['pages']}/{limit[0]}；chunks {budget['chunks']}"
                + (f"/{limit[1]}" if limit[1] else "（首次构建不设每日chunk额度）")
            )
        lines += ["", "## 内容与出处", ""]
        items = self.db.query("SELECT * FROM crawl_items WHERE run_id=? ORDER BY id", (run_id,))
        if not items:
            lines.append("本次没有处理页面；可能今日预算已用完或队列已完成。未声称新增知识。")
        for item in items:
            label = self._md(item["title"] or item["url"])
            safe_url = quote(item["url"], safe=":/?&=%#")
            lines += [
                f"- **{item['status']} · {item['topic']}** [{label}]({safe_url})（{item['source_kind']}）",
                f"  - 发布时间：{self._md(item['published_at'] or '未知')}；抓取时间：{item['fetched_at']}；chunks：{item['chunk_count']}。",
            ]
            if item["reason"]:
                lines.append(f"  - 说明：{self._md(item['reason'])}")
            if item["status"] in ("added", "updated"):
                lines.append(f"  - 原文摘录：{self._md(item['summary'])}")
        self.db.execute(
            "INSERT OR REPLACE INTO daily_reports(id,run_id,day,title,markdown,created_at,status) VALUES(?,?,?,?,?,?,?)",
            (run_id, run_id, run["day"], title, "\n".join(lines), self._stamp(), run["status"]),
        )

    async def cancel(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # Cancellation may arrive before the coroutine enters its finally block.
                run = self.active()
                if run:
                    self.db.execute(
                        "UPDATE update_runs SET status='cancelled',finished_at=?,message=? WHERE id=?",
                        (self._stamp(), "任务开始前已取消", run["id"]),
                    )
                    self._report(run["id"])
            return {"status": "cancelled"}
        return {"status": "idle"}

    async def close(self):
        await self.cancel()
        await self.fetcher.close()
