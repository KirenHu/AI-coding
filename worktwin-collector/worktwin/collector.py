"""Incremental, allowlisted, non-uploading local document collector."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

from .config import MAX_FILE_BYTES, POLL_INTERVAL_SECONDS
from .db import Database
from .reconcile import review_flags
from .parsers import EXCLUDED_FILES, EXCLUDED_FOLDERS, SUPPORTED_EXTENSIONS, parse_file, split_chunks
from .ingest import TRANSCRIPT_ADAPTERS, collect_transcript
from .artifacts import record_file_observation, record_file_deletion


def eligible_file(path: Path, kind: str) -> bool:
    name = path.name.lower()
    if name in EXCLUDED_FILES or name.startswith(".env") or name.endswith((".pem", ".key", ".p12", ".pfx")):
        return False
    if kind in TRANSCRIPT_ADAPTERS:
        return path.suffix.lower() == ".jsonl"
    return path.suffix.lower() in SUPPORTED_EXTENSIONS


def source_files(root: Path, kind: str, onerror=None):
    if kind == 'antigravity':
        from .antigravity import artifact_files
        yield from artifact_files(root)
        return
    resolved_root = Path(root).resolve()
    for current, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
        dirs[:] = [d for d in dirs if not (Path(current) / d).is_symlink() and d not in EXCLUDED_FOLDERS and not (d.startswith(".") and kind not in TRANSCRIPT_ADAPTERS)]
        for name in files:
            path = Path(current) / name
            if not eligible_file(path, kind) or path.is_symlink():
                continue
            try:
                if path.resolve().is_relative_to(resolved_root):
                    yield path
            except (OSError, ValueError) as exc:
                if onerror:
                    onerror(exc)
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
        self._legacy_issues_checked = False
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

    def request_large_files(self, source_id: int, issue_ids: list[int]) -> int:
        with self.db.connect() as con:
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM source_file_issues WHERE source_id=?", (source_id,))]
            selected = [r for r in rows if r['id'] in set(issue_ids)]
            if not issue_ids or len(selected) != len(set(issue_ids)) or any(r['code'] != 'oversized' for r in selected):
                raise ValueError('文件列表已变化，请重新打开查看')
            con.executemany("UPDATE source_file_issues SET state='queued' WHERE id=? AND state='waiting'",
                            [(r['id'],) for r in selected])
        self.schedule()
        return len(selected)

    def _file_issue(self, source_id, path, code, message, stat=None):
        with self.db.connect() as con:
            con.execute("""INSERT INTO source_file_issues(source_id,path,code,message,size_bytes,mtime_ns)
                VALUES(?,?,?,?,?,?) ON CONFLICT(source_id,path) DO UPDATE SET code=excluded.code,
                message=excluded.message,state=CASE WHEN source_file_issues.state='queued'
                AND source_file_issues.size_bytes=excluded.size_bytes AND source_file_issues.mtime_ns=excluded.mtime_ns
                THEN 'queued' ELSE 'waiting' END,size_bytes=excluded.size_bytes,mtime_ns=excluded.mtime_ns""",
                (source_id,str(path),code,message,stat.st_size if stat else None,stat.st_mtime_ns if stat else None))

    def _clear_issue(self, source_id, path):
        with self.db.connect() as con:
            con.execute('DELETE FROM source_file_issues WHERE source_id=? AND path=?',(source_id,str(path)))

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

    def _classify_legacy_size_errors(self):
        """Classify old size-limit messages using metadata only, including paused sources."""
        with self.db.connect() as con:
            rows = [dict(r) for r in con.execute("""SELECT * FROM sources s WHERE COALESCE(adapter,kind)='folder' AND last_error LIKE '%个文件解析失败'
                AND NOT EXISTS (SELECT 1 FROM source_file_issues f WHERE f.source_id=s.id)""")]
        for source in rows:
            oversized = 0
            for path in source_files(Path(source['root']),source.get('adapter') or source['kind']):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_size > MAX_FILE_BYTES:
                    self._file_issue(source['id'],path,'oversized','超过默认读取上限',stat)
                    oversized += 1
            if oversized:
                message = f'{oversized} 个文件超过大小限制'
                if source['last_error'] != f'{oversized} 个文件解析失败':
                    message += ' · 另有历史采集异常，重新检查后更新'
                with self.db.connect() as con:
                    con.execute('UPDATE sources SET last_error=? WHERE id=?',(message,source['id']))

    def scan_all(self) -> dict[str, int]:
        if not self._lock.acquire(blocking=False):
            return dict(self.last_result)
        summary = {"new": 0, "updated": 0, "deleted": 0, "errors": 0}
        self.is_scanning = True
        try:
            if not self._legacy_issues_checked:
                self._classify_legacy_size_errors()
                self._legacy_issues_checked = True
            with self.db.connect() as con:
                sources = [dict(r) for r in con.execute("""SELECT * FROM sources WHERE enabled=1 OR EXISTS
                    (SELECT 1 FROM source_file_issues f WHERE f.source_id=sources.id AND f.state IN ('queued','processing')) ORDER BY id""").fetchall()]
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
        with self.db.connect() as con:
            issues = {r['path']:dict(r) for r in con.execute('SELECT * FROM source_file_issues WHERE source_id=?',(src['id'],))}
        selected_only = not src['enabled']
        if not root.is_dir():
            if src.get('collection_scope') == 'assistant':
                return counts  # A selected tool can wait for its first local session.
            with self.db.connect() as con:
                con.execute("UPDATE sources SET last_error=? WHERE id=?", ("目录不可访问或已移除", src["id"]))
                con.execute("UPDATE source_file_issues SET state='waiting' WHERE source_id=?",(src['id'],))
            counts["errors"] += 1
            return counts
        with self.db.connect() as con:
            existing = {r["path"]: dict(r) for r in con.execute("SELECT id,path,title,content,sha256,mtime_ns,size_bytes FROM documents WHERE source_id=?", (src["id"],))}
        seen: set[str] = set()
        def walk_error(exc):
            counts["errors"] += 1
            self.db.event("scan_unreadable", "部分目录暂时不可读取，保留已采集资料", src["id"])
        for path in source_files(root, kind, onerror=walk_error):
            key = str(path)
            seen.add(key)
            issue = issues.get(key)
            requested = issue and issue['state'] in ('queued','processing')
            if selected_only and not requested:
                continue
            stat = None
            try:
                stat = path.stat()
                previous = existing.get(key)
                if kind in TRANSCRIPT_ADAPTERS:
                    if previous is None:
                        from .source_detection import recognizes_file
                        if not recognizes_file(kind, path):
                            continue
                    with self.db.connect() as con:
                        result = collect_transcript(con, path, src, previous, stat)
                    if result["state"]:
                        counts[result["state"]] += 1
                    counts["errors"] += result["errors"]
                    continue
                if previous and previous["mtime_ns"] == stat.st_mtime_ns and previous["size_bytes"] == stat.st_size:
                    self._clear_issue(src['id'],path)
                    continue
                same_version = issue and issue['size_bytes'] == stat.st_size and issue['mtime_ns'] == stat.st_mtime_ns
                if stat.st_size > MAX_FILE_BYTES and not (requested and same_version):
                    counts['errors'] += 1
                    if not (same_version and issue['code'] != 'oversized'):
                        self._file_issue(src['id'],path,'oversized','超过默认读取上限',stat)
                    continue
                if requested:
                    with self.db.connect() as con:
                        con.execute("UPDATE source_file_issues SET state='processing' WHERE id=?",(issue['id'],))
                digest = hashlib.sha256()
                with path.open("rb") as file_stream:
                    for block in iter(lambda: file_stream.read(1024 * 1024), b""):
                        digest.update(block)
                data_digest = digest.hexdigest()
                if previous and previous["sha256"] == data_digest:
                    with self.db.connect() as con:
                        con.execute("UPDATE documents SET mtime_ns=?,size_bytes=? WHERE id=?", (stat.st_mtime_ns, stat.st_size, previous["id"]))
                    self._clear_issue(src['id'],path)
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
                    counts['errors'] += 1
                    self._file_issue(src['id'],path,'no_text','未提取到文字' + ('；扫描件 PDF 暂不支持文字识别' if path.suffix.lower()=='.pdf' else '，文件内容可能为空'),stat)
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
                        con.execute("UPDATE documents SET title=?,project=CASE WHEN project_verified=1 THEN project ELSE ? END,relative_path=?,file_type=?,content=?,sha256=?,size_bytes=?,mtime_ns=?,indexed_at=datetime('now'),deleted=0 WHERE id=?",
                                    (title,project,relative,path.suffix.lower(),content,data_digest,stat.st_size,stat.st_mtime_ns,document_id))
                        counts["updated"] += 1
                    else:
                        cursor = con.execute("INSERT INTO documents(source_id,path,relative_path,title,file_type,project,content,sha256,size_bytes,mtime_ns) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                             (src["id"],key,relative,title,path.suffix.lower(),project,content,data_digest,stat.st_size,stat.st_mtime_ns))
                        document_id = int(cursor.lastrowid)
                        counts["new"] += 1
                    for idx, (start, end, chunk) in enumerate(chunks):
                        con.execute("INSERT INTO chunks(document_id,ordinal,start_offset,end_offset,text) VALUES(?,?,?,?,?)", (document_id, idx, start, end, chunk))
                    record_file_observation(con, document_id,
                                            previous_content=previous["content"] if previous else None,
                                            previous_sha=previous["sha256"] if previous else "")
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
                self._clear_issue(src['id'],path)
            except Exception as exc:
                counts["errors"] += 1
                code = 'unreadable' if isinstance(exc,OSError) else 'parse_failed'
                message = '文件无法读取，请检查访问权限或文件是否仍存在' if code == 'unreadable' else '无法解析文件内容，请确认文件未损坏、未加密且格式正确'
                self._file_issue(src['id'],path,code,message,stat)
        # Enumeration filters, read errors and incomplete walks do not prove
        # deletion. Only a missing path removes a previously collected file.
        removed = set()
        for old_path in (set(existing) - seen if not selected_only else set()):
            try:
                Path(old_path).stat()
            except FileNotFoundError:
                removed.add(old_path)
            except OSError:
                counts["errors"] += 1
        if removed:
            with self.db.connect() as con:
                for old_path in removed:
                    doc_id = existing[old_path]["id"]
                    if kind not in TRANSCRIPT_ADAPTERS:
                        record_file_deletion(con, doc_id)
                    affected = {int(r[0]) for r in con.execute(
                        "SELECT DISTINCT knowledge_id FROM knowledge_evidence WHERE document_id=?", (doc_id,))}
                    con.execute("DELETE FROM documents WHERE id=?", (doc_id,))
                    con.executemany("UPDATE knowledge SET review_hold=1 WHERE id=?", [(k,) for k in affected])
                    review_flags(con, affected)
                    counts["deleted"] += 1
                self._purge_orphans(con)
        for path in set(issues) - seen:
            # A removed file no longer needs an action, including queued files.
            if not Path(path).exists():
                if issues[path]['state'] in ('queued','processing'):
                    self._file_issue(src['id'],path,'unreadable','文件已移动或删除')
                else:
                    self._clear_issue(src['id'],path)
            elif issues[path]['state'] in ('queued','processing'):
                self._file_issue(src['id'],path,'unreadable','文件不在当前可采集范围内')
        with self.db.connect() as con:
            totals = dict(con.execute('SELECT code,COUNT(*) FROM source_file_issues WHERE source_id=? GROUP BY code',(src['id'],)))
            labels = {'oversized':'个文件超过大小限制','parse_failed':'个文件解析失败','unreadable':'个文件无法读取','no_text':'个文件未提取到文字'}
            error_text = ' · '.join(f"{n} {labels[code]}" for code,n in totals.items())
            if not error_text and counts['errors']:
                error_text = '部分文件或目录无法读取'
            con.execute("UPDATE sources SET last_scanned=datetime('now'),last_error=? WHERE id=?", (error_text or None,src["id"]))
            if any(counts.values()):
                con.execute("INSERT INTO events(action,details,source_id) VALUES(?,?,?)", ("scan", f"新增 {counts['new']} · 更新 {counts['updated']} · 删除 {counts['deleted']} · 错误 {counts['errors']}",src["id"]))
        return counts

    @staticmethod
    def _purge_orphans(con):
        con.execute("DELETE FROM knowledge WHERE source_bound=1 AND NOT EXISTS "
                    "(SELECT 1 FROM knowledge_evidence e WHERE e.knowledge_id=knowledge.id)")
