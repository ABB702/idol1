"""SQLite is authoritative; LanceDB is a recoverable semantic index."""

import asyncio
import hashlib
import re
import threading
from collections import defaultdict
from pathlib import Path

from .chunking import split_document
from .db import utc_now
from .embeddings import MODEL_NAME, LocalEmbeddings


def tokenize(text: str) -> list[str]:
    import jieba

    return [part.lower() for part in jieba.cut_for_search(text) if re.search(r"\w", part)]


class KnowledgeService:
    def __init__(self, db, data_dir: Path, *, embeddings=None):
        self.db = db
        self.data_dir = Path(data_dir)
        self.embeddings = embeddings or LocalEmbeddings(self.data_dir / "models")
        self._lock = threading.RLock()
        self._index = None
        self.last_search_warning = None
        with db.transaction() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
                    topic TEXT NOT NULL, source_kind TEXT NOT NULL, current_version_id INTEGER,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, published_at TEXT
                );
                CREATE TABLE IF NOT EXISTS document_versions (
                    id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id),
                    content_hash TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS versions_hash ON document_versions(content_hash);
                CREATE TABLE IF NOT EXISTS chunk_parents (
                    id INTEGER PRIMARY KEY, version_id INTEGER NOT NULL REFERENCES document_versions(id),
                    text TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id),
                    version_id INTEGER NOT NULL REFERENCES document_versions(id), ordinal INTEGER NOT NULL,
                    text TEXT NOT NULL, parent_text TEXT NOT NULL, heading TEXT NOT NULL,
                    is_code INTEGER NOT NULL, content_hash TEXT NOT NULL, indexed INTEGER NOT NULL DEFAULT 0,
                    parent_id INTEGER REFERENCES chunk_parents(id)
                );
                CREATE INDEX IF NOT EXISTS chunks_version ON chunks(version_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(tokens, title);
                CREATE TABLE IF NOT EXISTS knowledge_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    def _meta(self, key, value):
        self.db.execute(
            "INSERT INTO knowledge_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def _index_error(self, message):
        self._meta("index_error", message)

    def _vector_table(self):
        if self._index is None:
            import lancedb

            self._index = lancedb.connect(str(self.data_dir / "vectors"))
        try:
            return self._index.open_table("chunks")
        except FileNotFoundError:
            return None
        except ValueError as exc:
            if "not found" in str(exc).lower() or "does not exist" in str(exc).lower():
                return None
            raise

    def _index_chunks(self, chunks):
        if not chunks:
            return
        manifest = self.db.one("SELECT value FROM knowledge_meta WHERE key='index_model'")
        if manifest and manifest["value"] != MODEL_NAME:
            raise RuntimeError("向量模型已变更，必须重建索引后使用。")
        vectors = self.embeddings.embed_sync([c["heading"][:60] + "\n" + c["text"] for c in chunks])
        records = [
            {"id": c["id"], "document_id": c["document_id"], "vector": v}
            for c, v in zip(chunks, vectors, strict=True)
        ]
        table = self._vector_table()
        if table is None:
            self._index.create_table("chunks", data=records)
        else:
            table.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(records)
        with self.db.transaction() as conn:
            conn.executemany("UPDATE chunks SET indexed=1 WHERE id=?", [(c["id"],) for c in chunks])
        self._meta("index_model", MODEL_NAME)

    async def prepare_model(self):
        await self.embeddings.prepare()
        return self.model_status()

    def model_status(self):
        state = self.embeddings.status()
        error = self.db.one("SELECT value FROM knowledge_meta WHERE key='index_error'")
        pending = self.db.one("""SELECT COUNT(*) AS n FROM chunks c JOIN documents d
            ON d.current_version_id=c.version_id WHERE indexed=0""")["n"]
        state.update(index_error=(error or {}).get("value") or None, pending_chunks=pending)
        return state

    def _upsert(self, url, title, text, topic, source_kind, published_at, max_chunks):
        with self._lock:
            text = text.strip()
            digest = hashlib.sha256(text.encode()).hexdigest()
            old = self.db.one("SELECT * FROM documents WHERE url=?", (url,))
            duplicate = self.db.one(
                """SELECT d.id FROM documents d JOIN document_versions v
                ON v.id=d.current_version_id WHERE v.content_hash=?""",
                (digest,),
            )
            if duplicate:
                return {"status": "duplicate", "chunk_count": 0, "document_id": duplicate["id"]}
            parts = split_document(text)
            if not parts or (max_chunks is not None and len(parts) > max_chunks):
                return {
                    "status": "skipped",
                    "chunk_count": 0,
                    "document_id": old["id"] if old else 0,
                    "reason": "正文为空或完整文档超过剩余 chunk 配额；原版本保持不变。",
                }
            now = utc_now()
            with self.db.transaction() as conn:
                if old:
                    doc_id = old["id"]
                else:
                    doc_id = conn.execute(
                        """INSERT INTO documents
                        (url,title,topic,source_kind,created_at,updated_at,published_at) VALUES(?,?,?,?,?,?,?)""",
                        (url, title, topic, source_kind, now, now, published_at),
                    ).lastrowid
                version = conn.execute(
                    """INSERT INTO document_versions
                    (document_id,content_hash,text,created_at) VALUES(?,?,?,?)""",
                    (doc_id, digest, text, now),
                ).lastrowid
                if old:
                    conn.execute(
                        "DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE version_id=?)",
                        (old["current_version_id"],),
                    )
                parents = {}
                for index, part in enumerate(parts):
                    parent_hash = hashlib.sha256(part["parent_text"].encode()).hexdigest()
                    if parent_hash not in parents:
                        parents[parent_hash] = conn.execute(
                            "INSERT INTO chunk_parents(version_id,text) VALUES(?,?)",
                            (version, part["parent_text"]),
                        ).lastrowid
                    surrounding = (
                        part["parent_text"]
                        if len(part["parent_text"]) <= 1800
                        else part["parent_text"][max(0, part["offset"] - 300) : part["offset"] + 900]
                    )
                    chunk_id = conn.execute(
                        """INSERT INTO chunks
                        (document_id,version_id,ordinal,text,parent_text,heading,is_code,content_hash,parent_id)
                        VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            doc_id,
                            version,
                            index,
                            part["text"],
                            surrounding,
                            part["heading"],
                            part["is_code"],
                            hashlib.sha256(part["text"].encode()).hexdigest(),
                            parents[parent_hash],
                        ),
                    ).lastrowid
                    conn.execute(
                        "INSERT INTO chunks_fts(rowid,tokens,title) VALUES(?,?,?)",
                        (chunk_id, " ".join(tokenize(part["text"])), " ".join(tokenize(title))),
                    )
                conn.execute(
                    """UPDATE documents SET current_version_id=?,title=?,topic=?,source_kind=?,
                    updated_at=?,published_at=? WHERE id=?""",
                    (version, title, topic, source_kind, now, published_at, doc_id),
                )
            warning = None
            if self.embeddings.ready:
                try:
                    self._index_chunks(self.db.query("SELECT * FROM chunks WHERE version_id=?", (version,)))
                    # Remove old vectors after the new version is indexed to keep exact
                    # candidate retrieval from being crowded by historical versions.
                    table = self._vector_table()
                    current_ids = [
                        str(row["id"])
                        for row in self.db.query("SELECT id FROM chunks WHERE version_id=?", (version,))
                    ]
                    if table is not None and current_ids:
                        table.delete(f"document_id = {int(doc_id)} AND id NOT IN ({','.join(current_ids)})")
                except Exception:  # noqa: BLE001 - index failure must not discard authoritative text
                    warning = "原文已保存，但向量索引失败；当前使用关键词检索，请重建索引。"
                    self._index_error(warning)
            else:
                warning = "本地向量模型尚未准备，当前仅支持关键词检索。"
            result = {
                "status": "updated" if old else "added",
                "chunk_count": len(parts),
                "document_id": doc_id,
            }
            if warning:
                result["warning"] = warning
            return result

    async def upsert_document(self, url, title, text, topic, source_kind, published_at=None, max_chunks=None):
        return await asyncio.to_thread(
            self._upsert, url, title, text, topic, source_kind, published_at, max_chunks
        )

    def _begin_reindex(self):
        with self._lock:
            if not self.embeddings.ready:
                raise RuntimeError("请先准备本地向量模型，再重建索引。")
            self._index_error("正在重建向量索引；暂时使用关键词检索。")
            self.db.execute("UPDATE chunks SET indexed=0")
            self._vector_table()
            self._index.drop_table("chunks", ignore_missing=True)
            self._meta("index_model", MODEL_NAME)

    def _reindex_batch(self, cursor):
        with self._lock:
            chunks = self.db.query(
                """SELECT c.* FROM chunks c JOIN documents d
                ON d.current_version_id=c.version_id WHERE c.id>? ORDER BY c.id LIMIT 8""",
                (cursor,),
            )
            self._index_chunks(chunks)
            return (chunks[-1]["id"] if chunks else cursor), len(chunks)

    async def reindex(self):
        try:
            await asyncio.to_thread(self._begin_reindex)
            cursor, total = 0, 0
            while True:
                cursor, count = await asyncio.to_thread(self._reindex_batch, cursor)
                total += count
                if not count:
                    break
                # Release the index lock between small batches so interactive
                # retrieval can run; never hold it across an entire knowledge base.
                await asyncio.sleep(0.01)
            await asyncio.to_thread(self._index_error, "")
            return {"indexed_chunks": total}
        except Exception as exc:
            await asyncio.to_thread(
                self._index_error, "向量索引重建失败，原文仍然保留，请检查模型缓存后重试。"
            )
            raise RuntimeError("向量索引重建失败；当前可使用关键词检索。") from exc

    def _clear(self):
        with self._lock:
            # Drop vectors first: a failed drop must not erase authoritative text.
            self._vector_table()
            self._index.drop_table("chunks", ignore_missing=True)
            with self.db.transaction() as conn:
                for table in (
                    "chunks_fts",
                    "chunks",
                    "chunk_parents",
                    "document_versions",
                    "documents",
                    "knowledge_meta",
                    "crawl_queue",
                ):
                    conn.execute(f"DELETE FROM {table}")
                # Keep daily spending/history so clearing cannot bypass daily quotas.
                conn.execute("DELETE FROM update_budgets WHERE scope='initial'")
                conn.execute(
                    "DELETE FROM settings WHERE key IN ('initial_vue_markdown_v1', 'initial_vue_unlimited_v1')"
                )
            self.last_search_warning = None

    async def clear(self):
        task = asyncio.create_task(asyncio.to_thread(self._clear))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def _search(self, query, limit):
        with self._lock:
            self.last_search_warning = None
            tokens = list(dict.fromkeys(tokenize(query)))[:30]
            if not tokens:
                return []
            match = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
            lexical = self.db.query(
                """SELECT rowid AS id, bm25(chunks_fts) AS rank FROM chunks_fts
                WHERE chunks_fts MATCH ? ORDER BY rank LIMIT 40""",
                (match,),
            )
            ranks = defaultdict(float)
            lexical_ranks = {row["id"]: i for i, row in enumerate(lexical, 1)}
            vector_ranks = {}
            for i, row in enumerate(lexical):
                ranks[row["id"]] += 1 / (60 + i + 1)
            if self.embeddings.ready:
                try:
                    manifest = self.db.one("SELECT value FROM knowledge_meta WHERE key='index_model'")
                    if manifest and manifest["value"] != MODEL_NAME:
                        raise RuntimeError("向量模型与索引不匹配。")
                    table = self._vector_table()
                    if table is not None:
                        vector = self.embeddings.embed_sync([query[:2000]])[0]
                        # Stale vectors never enter results: SQLite current-version join below.
                        candidates = table.search(vector).distance_type("cosine").limit(80).to_list()
                        ids = [row["id"] for row in candidates]
                        placeholders = ",".join("?" for _ in ids)
                        valid = (
                            {
                                row["id"]
                                for row in self.db.query(
                                    f"""SELECT c.id FROM chunks c JOIN documents d
                            ON d.current_version_id=c.version_id WHERE c.indexed=1 AND c.id IN ({placeholders})""",
                                    ids,
                                )
                            }
                            if ids
                            else set()
                        )
                        for i, row in enumerate(c for c in candidates if c["id"] in valid):
                            vector_ranks[row["id"]] = i + 1
                            ranks[row["id"]] += 1 / (60 + i + 1)
                    elif self.stats()["chunks"]:
                        self.last_search_warning = "向量索引缺失，已使用关键词检索，请重建索引。"
                        self._index_error(self.last_search_warning)
                    if self.model_status()["pending_chunks"]:
                        self.last_search_warning = (
                            "部分原文尚未建立向量索引，检索结果可能不完整，请重建索引。"
                        )
                except Exception:  # noqa: BLE001 - recover with a visible lexical-only result
                    self.last_search_warning = "语义检索失败，已降级为关键词检索，请重建索引。"
                    self._index_error(self.last_search_warning)
            else:
                self.last_search_warning = "本地向量模型未准备，当前仅使用关键词检索。"
            results = []
            for chunk_id, score in ranks.items():
                row = self.db.one(
                    """SELECT c.*,d.title,d.url,d.topic,d.source_kind,d.published_at FROM chunks c
                    JOIN documents d ON d.id=c.document_id AND d.current_version_id=c.version_id WHERE c.id=?""",
                    (chunk_id,),
                )
                if row:
                    row["retrieval"] = {
                        "lexical_rank": lexical_ranks.get(chunk_id),
                        "vector_rank": vector_ranks.get(chunk_id),
                        "rrf_score": score,
                    }
                    row["score"] = score + min(0.005, sum(t in row["title"].lower() for t in tokens) * 0.001)
                    # Parent context is retained in storage; cap supplied context to bound prompts.
                    row["context"] = row["parent_text"] if len(row["parent_text"]) <= 1800 else row["text"]
                    row.pop("parent_text")
                    if self.last_search_warning:
                        row["retrieval_warning"] = self.last_search_warning
                    results.append(row)
            results.sort(key=lambda row: row["score"], reverse=True)
            selected, counts = [], defaultdict(int)
            while results and len(selected) < max(1, min(limit, 20)):
                best = max(results, key=lambda r: r["score"] / (1 + counts[r["document_id"]] * 0.6))
                selected.append(best)
                counts[best["document_id"]] += 1
                results.remove(best)
            return selected

    async def search(self, query: str, limit=6):
        return await asyncio.to_thread(self._search, query, limit)

    def _search_details(self, query, limit):
        with self._lock:
            items = self._search(query, limit)
            return {"items": items, "warning": self.last_search_warning}

    async def search_details(self, query: str, limit=6):
        return await asyncio.to_thread(self._search_details, query, limit)

    def documents(self, query=""):
        return self.db.query(
            """SELECT d.*,COUNT(c.id) AS chunk_count FROM documents d LEFT JOIN chunks c
            ON c.version_id=d.current_version_id WHERE d.title LIKE ? OR d.url LIKE ?
            GROUP BY d.id ORDER BY d.updated_at DESC LIMIT 500""",
            (f"%{query}%", f"%{query}%"),
        )

    def stats(self):
        return {
            "documents": self.db.one("SELECT COUNT(*) AS n FROM documents")["n"],
            "chunks": self.db.one("""SELECT COUNT(*) AS n FROM chunks c JOIN documents d
                    ON d.current_version_id=c.version_id""")["n"],
        }

    async def release_idle(self):
        await self.embeddings.release_idle()

    async def close(self):
        await self.embeddings.close()
