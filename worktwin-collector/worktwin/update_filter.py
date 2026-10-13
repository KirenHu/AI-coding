"""Document-version screening. No semantic decisions or cross-source cache reuse."""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
from contextvars import ContextVar

processing_run = ContextVar("knowledge_processing_run", default=None)


class ModelText(str):
    """String-compatible response with per-call provider usage (never shared state)."""
    def __new__(cls, content, usage=None):
        value = super().__new__(cls, content)
        value.usage = usage or {}
        return value



def migrate(con):
    con.executescript('''
        CREATE TABLE IF NOT EXISTS document_processing_baselines (
            document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            content_sha TEXT NOT NULL, content TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS knowledge_processing_runs (
            id INTEGER PRIMARY KEY, document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
            content_sha TEXT NOT NULL, previous_sha TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL, reason TEXT NOT NULL,
            changes_json TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL DEFAULT 'running',
            source_chars INTEGER NOT NULL DEFAULT 0, submitted_chars INTEGER NOT NULL DEFAULT 0,
            model_calls INTEGER NOT NULL DEFAULT 0, elapsed_ms INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS model_call_metrics (
            id INTEGER PRIMARY KEY, model TEXT NOT NULL, state TEXT NOT NULL,
            processing_run_id INTEGER REFERENCES knowledge_processing_runs(id) ON DELETE SET NULL,
            input_tokens INTEGER, output_tokens INTEGER, elapsed_ms INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS article_source_refresh (
            article_id INTEGER PRIMARY KEY REFERENCES knowledge(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL DEFAULT 1);
    ''')


def canonical(text):
    # Do not collapse spaces, indentation, Markdown hard breaks or blank paragraphs.
    # Only line-ending encoding and empty lines at EOF are treated as formatting.
    return text.replace('\r\n', '\n').rstrip('\n')


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def plan(old, new, *, enabled=True):
    result = {'mode': 'full', 'reason': 'first_import', 'content': new, 'changes': []}
    if old is None:
        return result
    before, after = canonical(old), canonical(new)
    if digest(old) == digest(new):
        return dict(result, mode='skip', reason='exact_content', content='')
    if before == after:
        return dict(result, mode='skip', reason='format_only', content='')
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    changes = [{'kind':tag, 'old_lines':[i+1,j], 'new_lines':[k+1,l]}
               for tag,i,j,k,l in difflib.SequenceMatcher(None,a,b,autojunk=False).get_opcodes()
               if tag != 'equal']
    result['changes'] = changes
    if not enabled:
        return dict(result, reason='incremental_disabled')
    # Existing-section edits, deletions, reorder and rephrasing require full context.
    # Restrict the first release to a new, explicitly delimited Markdown section.
    if not new.startswith(old):
        return dict(result, reason='existing_content_changed')
    addition = new[len(old):]
    if not re.match(r'\n*#{1,6} ', addition) or not re.search(r'^#{1,6} ',old,re.M):
        return dict(result, reason='insufficient_section_boundary')
    # References and withdrawal language can change the interpretation of old sections.
    if re.search(r'上述|前述|上文|此前|取消|撤回|废弃|不再|替代|更正|纠正|except|instead|replac|withdraw|deprecat', addition, re.I):
        return dict(result, reason='cross_section_dependency')
    headings = list(re.finditer(r'^#{1,6} .+$',old,re.M))
    if len(headings)<2 or '```' in old or '~~~' in old or '```' in addition or '~~~' in addition:
        return dict(result,reason='ambiguous_section_structure')
    # Keep the complete opening section (purpose/constraints), not just its heading.
    opening = old[:headings[1].start()]
    context = opening + old[headings[-1].start():]
    selected = context + addition
    if len(selected) >= len(new) * .7:
        return dict(result, reason='context_not_smaller')
    return dict(result, mode='incremental', reason='new_section_with_context', content=selected)


def start_run(con, document_id, sha, selection, source_chars):
    return con.execute('''INSERT INTO knowledge_processing_runs
        (document_id,content_sha,previous_sha,mode,reason,changes_json,source_chars,submitted_chars)
        VALUES(?,?,?,?,?,?,?,?)''', (document_id,sha,selection.get('previous_sha',''),selection['mode'],selection['reason'],
        json.dumps(selection['changes']),source_chars,len(selection['content']))).lastrowid


def save_baseline(con, document_id, sha, content):
    con.execute('''INSERT INTO document_processing_baselines(document_id,content_sha,content)
        VALUES(?,?,?) ON CONFLICT(document_id) DO UPDATE SET content_sha=excluded.content_sha,
        content=excluded.content,updated_at=datetime('now')''',(document_id,sha,content))


def request_article_refresh(con, document_id):
    # A surviving quote does not prove its surrounding conclusion is still current.
    con.execute('''INSERT INTO article_source_refresh(article_id)
        SELECT DISTINCT k.id FROM knowledge k JOIN knowledge_evidence e ON e.knowledge_id=k.id
        WHERE e.document_id=? AND k.organization='article' AND k.status!='archived'
        ON CONFLICT(article_id) DO UPDATE SET revision=revision+1''',(document_id,))
    con.execute('''UPDATE knowledge SET needs_review=1,review_hold=1 WHERE id IN
        (SELECT article_id FROM article_source_refresh)''')


def measured_call(db, model, call):
    """For specialized routing services outside the shared ModelRuntime."""
    started = time.monotonic()
    result = None
    try:
        result = call()
        return result
    finally:
        usage = result.get('usage',{}) if isinstance(result,dict) else getattr(result,'usage',{})
        with db.connect() as con:
            con.execute('''INSERT INTO model_call_metrics(model,state,processing_run_id,input_tokens,output_tokens,elapsed_ms) VALUES(?,?,?,?,?,?)''',
                (model,'done' if result is not None else 'error',processing_run.get(),
                 usage.get('prompt_tokens'),usage.get('completion_tokens'),int((time.monotonic()-started)*1000)))
