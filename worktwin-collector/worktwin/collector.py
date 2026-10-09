"""Incremental, allowlisted, non-uploading local document collector."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

from .config import MAX_FILE_BYTES, MAX_SESSION_BYTES, POLL_INTERVAL_SECONDS
from .db import Database
from .reconcile import review_flags
from .parsers import EXCLUDED_FILES, EXCLUDED_FOLDERS, SUPPORTED_EXTENSIONS, parse_file, split_chunks


def eligible_file(path: Path, kind: str) -> bool:
    name = path.name.lower()
    if name in EXCLUDED_FILES or name.startswith(".env") or name.endswith((".pem", ".key", ".p12", ".pfx")):
        return False
    if kind in ("codex", "claude"):
        return path.suffix.lower() == ".jsonl"
    return path.suffix.lower() in SUPPORTED_EXTENSIONS


def source_files(root: Path, kind: str):
    resolved_root = Path(root).resolve()
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(current) / d).is_symlink() and d not in EXCLUDED_FOLDERS and not (d.startswith(".") and kind not in ("codex", "claude"))]
        for name in files:
            path = Path(current) / name
            if not eligible_file(path, kind) or path.is_symlink() or not path.is_file():
                continue
            try:
                if path.stat().st_size <= (MAX_SESSION_BYTES if kind in ("codex", "claude") else MAX_FILE_BYTES) and path.resolve().is_relative_to(resolved_root):
                    yield path
            except (OSError, ValueError):
                continue


class Collector:
    def __init__(self, db: Database, interval: int = POLL_INTERVAL_SECONDS):
        self.db = db
        self.interval = interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._watchers: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self.is_scanning = False
        self.last_scan = "尚未扫描"
        self.last_result = {"new": 0, "updated": 0, "deleted": 0, "errors": 0}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="worktwin-scanner", daemon=True)
        self._thread.start()
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()
        for _, stop in self._watchers.values():
            stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        for watcher, _ in self._watchers.values():
            watcher.join(timeout=2)

    def schedule(self):
        self._wake.set()

    def forget_source(self, source_id: int) -> str | None:
        """Serialize source revocation with scans so a scan cannot restore removed data."""
        with self._lock:
            with self.db.connect() as con:
                row = con.execute("SELECT name FROM sources WHERE id=?", (source_id,)).fetchone()
                if row is None:
                    return None
                name = row["name"]
                affected = {int(r[0]) for r in con.execute("""SELECT DISTINCT e.knowledge_id FROM knowledge_evidence e
                    JOIN documents d ON d.id=e.document_id WHERE d.source_id=?""", (source_id,))}
                # Historical knowledge revisions do not encode per-source citations.
                # When one contributing source is revoked, old revision text may
                # still contain facts from it even if another citation remains.
                # Fail closed: discard those historical snapshots, keep the
                # current article under review until its owner re-confirms it.
                con.executemany("DELETE FROM knowledge_history WHERE knowledge_id=?",
                                [(k,) for k in affected])
                con.execute("DELETE FROM sources WHERE id=?", (source_id,))
                # Extracted AND subsequently hand-edited items remain source-bound.
                # Without evidence they must not survive a revoked permission.
                con.execute("DELETE FROM knowledge WHERE source_bound=1 AND NOT EXISTS "
                            "(SELECT 1 FROM knowledge_evidence e WHERE e.knowledge_id=knowledge.id)")
                con.executemany("UPDATE knowledge SET review_hold=1 WHERE id=?", [(k,) for k in affected])
                review_flags(con, affected)
                con.execute("INSERT INTO events(action,details) VALUES(?,?)",
                            ("source_removed", f"已取消授权并清除相关知识：{name}"))
            # SQLite secure_delete removes old row contents; truncate the WAL as well.
            with self.db.connect() as con:
                con.execute("PRAGMA wal_checkpoint(PASSIVE)")
        self.schedule()
        return name

    def _loop(self):
        while not self._stop.is_set():
            self._wake.wait(self.interval)
            self._wake.clear()
            if not self._stop.is_set():
                self.scan_all()
                self._reconcile_watchers()

    def _reconcile_watchers(self):
        """Reuse watchfiles (Rust notify) for event-triggered rescans, with poll fallback."""
        try:
            from watchfiles import watch
        except ImportError:
            return
        with self.db.connect() as con:
            roots = {str(Path(r['root'])) for r in con.execute("SELECT root FROM sources WHERE enabled=1") if Path(r['root']).is_dir()}
        for root in set(self._watchers) - roots:
            self._watchers.pop(root)[1].set()
        for root in roots - set(self._watchers):
            stop = threading.Event()
            def work(folder=root, event=stop):
                try:
                    for changes in watch(folder, stop_event=event, debounce=1000, step=400, raise_interrupt=False):
                        if event.is_set() or self._stop.is_set():
                            return
                        if changes:
                            self.schedule()
                except (OSError, ValueError):
                    pass  # Periodic scanning remains available.
            worker = threading.Thread(target=work,name="worktwin-watch",daemon=True)
            self._watchers[root] = (worker, stop)
            worker.start()

    def scan_all(self) -> dict[str, int]:
        if not self._lock.acquire(blocking=False):
            return dict(self.last_result)
        summary = {"new": 0, "updated": 0, "deleted": 0, "errors": 0}
        self.is_scanning = True
        try:
            with self.db.connect() as con:
                sources = [dict(r) for r in con.execute("SELECT * FROM sources WHERE enabled=1 ORDER BY id").fetchall()]
            for src in sources:
                res = self._scan_source(src)
                for k in summary:
                    summary[k] += res[k]
            self.last_scan = time.strftime("%Y-%m-%d %H:%M:%S")
            self.last_result = summary
            return summary
        finally:
            self.is_scanning = False
            self._lock.release()

    def _scan_source(self, src: dict) -> dict[str, int]:
        counts = {"new": 0, "updated": 0, "deleted": 0, "errors": 0}
        root = Path(src["root"])
        kind = src.get("adapter") or src["kind"]
        if not root.is_dir():
            with self.db.connect() as con:
                con.execute("UPDATE sources SET last_error=? WHERE id=?", ("目录不可访问或已移除", src["id"]))
            counts["errors"] += 1
            return counts
        with self.db.connect() as con:
            existing = {r["path"]: dict(r) for r in con.execute("SELECT id,path,sha256,mtime_ns,size_bytes FROM documents WHERE source_id=?", (src["id"],))}
        seen: set[str] = set()
        for path in source_files(root, kind):
            key = str(path)
            seen.add(key)
            try:
                stat = path.stat()
                previous = existing.get(key)
                if previous and previous["mtime_ns"] == stat.st_mtime_ns and previous["size_bytes"] == stat.st_size:
                    continue
                digest = hashlib.sha256()
                with path.open("rb") as file_stream:
                    for block in iter(lambda: file_stream.read(1024 * 1024), b""):
                        digest.update(block)
                data_digest = digest.hexdigest()
                if previous and previous["sha256"] == data_digest:
                    with self.db.connect() as con:
                        con.execute("UPDATE documents SET mtime_ns=?,size_bytes=? WHERE id=?", (stat.st_mtime_ns, stat.st_size, previous["id"]))
                    continue
                title, content = parse_file(path, codex=kind == "codex", claude=kind == "claude")
                relative = str(path.relative_to(root))
                if kind in ("codex", "claude"):
                    first = content.split("\n", 1)[0] if content else ""
                    project = Path(first[len("工作目录："):]).name if first.startswith("工作目录：") else src["name"]
                else:
                    parent = path.relative_to(root).parts
                    project = parent[0] if len(parent) >= 2 else src["name"]
                if not content.strip():
                    if previous:
                        with self.db.connect() as con:
                            affected = {int(r[0]) for r in con.execute(
                                "SELECT DISTINCT knowledge_id FROM knowledge_evidence WHERE document_id=?", (previous["id"],))}
                            con.execute("DELETE FROM documents WHERE id=?", (previous["id"],))
                            self._purge_orphans(con)
                            con.executemany("UPDATE knowledge SET review_hold=1 WHERE id=?", [(k,) for k in affected])
                            review_flags(con, affected)
                        counts["deleted"] += 1
                    continue
                chunks = split_chunks(content)
                with self.db.connect() as con:
                    if previous:
                        document_id = int(previous["id"])
                        # Evidence must never silently point at new content after an edit.
                        affected = {int(r[0]) for r in con.execute(
                            "SELECT DISTINCT target_id FROM knowledge_proposals WHERE document_id=? AND status='pending'", (document_id,))}
                        con.execute("DELETE FROM knowledge_proposals WHERE document_id=?", (document_id,))
                        review_flags(con, affected)
                        con.execute("UPDATE knowledge_evidence SET is_current=0,chunk_id=NULL WHERE document_id=?", (document_id,))
                        con.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
                        con.execute("UPDATE documents SET title=?,project=?,relative_path=?,file_type=?,content=?,sha256=?,size_bytes=?,mtime_ns=?,indexed_at=datetime('now'),deleted=0 WHERE id=?",
                                    (title,project,relative,path.suffix.lower(),content,data_digest,stat.st_size,stat.st_mtime_ns,document_id))
                        counts["updated"] += 1
                    else:
                        cursor = con.execute("INSERT INTO documents(source_id,path,relative_path,title,file_type,project,content,sha256,size_bytes,mtime_ns) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                             (src["id"],key,relative,title,path.suffix.lower(),project,content,data_digest,stat.st_size,stat.st_mtime_ns))
                        document_id = int(cursor.lastrowid)
                        counts["new"] += 1
                    for idx, (start, end, chunk) in enumerate(chunks):
                        con.execute("INSERT INTO chunks(document_id,ordinal,start_offset,end_offset,text) VALUES(?,?,?,?,?)", (document_id, idx, start, end, chunk))
                    # A quote can remain valid across edits; rebind it to the new chunk.
                    if previous:
                        evs = con.execute("SELECT id,quote FROM knowledge_evidence WHERE document_id=?", (document_id,)).fetchall()
                        chunk_rows = con.execute("SELECT id,text FROM chunks WHERE document_id=?", (document_id,)).fetchall()
                        for ev in evs:
                            if ev["quote"] in content:
                                part = next((r for r in chunk_rows if ev["quote"][:60] in r["text"]), None)
                                con.execute("UPDATE knowledge_evidence SET is_current=1,chunk_id=? WHERE id=?",
                                            (part["id"] if part else None, ev["id"]))
                    # Collection is intentionally model-free. The scheduled
                    # knowledge worker handles BYOK inference asynchronously,
                    # and only for sources explicitly granted AI processing.
                    if src.get('allow_ai'):
                        con.execute("""INSERT INTO ai_jobs(document_id,content_sha,state,attempts,error)
                            VALUES(?,?,'queued',0,NULL) ON CONFLICT(document_id) DO UPDATE SET
                            content_sha=excluded.content_sha,state='queued',attempts=0,
                            error=NULL,next_run_at=NULL,updated_at=datetime('now')""", (document_id, data_digest))
                    if previous:
                        affected = {int(r[0]) for r in con.execute(
                            "SELECT DISTINCT knowledge_id FROM knowledge_evidence WHERE document_id=?", (document_id,))}
                        review_flags(con, affected)
                        con.execute("""UPDATE knowledge SET status='archived' WHERE status='draft'
                            AND created_by IN ('rules','ollama','enterprise_ai') AND id IN
                            (SELECT knowledge_id FROM knowledge_evidence WHERE document_id=? AND is_current=0)
                            AND NOT EXISTS (SELECT 1 FROM knowledge_evidence e WHERE e.knowledge_id=knowledge.id AND e.is_current=1)""", (document_id,))
            except Exception as exc:
                counts["errors"] += 1
                self.db.event("parse_error", f"{path.name}: {exc}", src["id"])
        removed = set(existing) - seen
        if removed:
            with self.db.connect() as con:
                for old_path in removed:
                    doc_id = existing[old_path]["id"]
                    affected = {int(r[0]) for r in con.execute(
                        "SELECT DISTINCT knowledge_id FROM knowledge_evidence WHERE document_id=?", (doc_id,))}
                    con.execute("DELETE FROM documents WHERE id=?", (doc_id,))
                    con.executemany("UPDATE knowledge SET review_hold=1 WHERE id=?", [(k,) for k in affected])
                    review_flags(con, affected)
                    counts["deleted"] += 1
                self._purge_orphans(con)
        with self.db.connect() as con:
            con.execute("UPDATE sources SET last_scanned=datetime('now'),last_error=? WHERE id=?", (f"{counts['errors']} 个文件解析失败" if counts["errors"] else None,src["id"]))
            if any(counts.values()):
                con.execute("INSERT INTO events(action,details,source_id) VALUES(?,?,?)", ("scan", f"新增 {counts['new']} · 更新 {counts['updated']} · 删除 {counts['deleted']} · 错误 {counts['errors']}",src["id"]))
        return counts

    @staticmethod
    def _purge_orphans(con):
        con.execute("DELETE FROM knowledge WHERE source_bound=1 AND NOT EXISTS "
                    "(SELECT 1 FROM knowledge_evidence e WHERE e.knowledge_id=knowledge.id)")
