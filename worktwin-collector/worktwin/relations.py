"""Browsable, local-only knowledge relationships and portable Markdown vault.

Inspired by Rowboat's Markdown knowledge index and Obsidian wiki links. No
upstream code is copied: this module uses the existing WorkTwin SQLite schema.
Explicit links use stable [[K123|optional display title]] IDs. Derived links
only use valid, current evidence from a *shared source document*; we never
ask a model to infer a relationship from a similar title.
"""
from __future__ import annotations

import io
import json
import re
import sqlite3
import zipfile
from collections import defaultdict

from .knowledge import KIND_LABELS
from .knowledge_policy import READY_SQL, unavailable_reason

WIKILINK = re.compile(r"\[\[K(0*[1-9]\d{0,16})(?:\|([^|\]\n]{1,160}))?\]\]")


def link_targets(body: str) -> set[int]:
    """Extract normalized stable IDs, not ambiguous titles."""
    return {int(m.group(1)) for m in WIKILINK.finditer(body)}


def _available_knowledge(con: sqlite3.Connection) -> list[dict]:
    """Index only active articles that can be referenced as current knowledge."""
    return [dict(r) for r in con.execute("""
        SELECT k.id,k.kind,k.title,k.body,k.status,k.version,k.updated_at,
               k.source_bound,k.needs_review
        FROM knowledge k
        WHERE k.status!='archived' AND k.needs_review=0
          AND (k.source_bound=0 OR EXISTS (
              SELECT 1 FROM knowledge_evidence e
              JOIN documents d ON d.id=e.document_id
              JOIN sources s ON s.id=d.source_id
              WHERE e.knowledge_id=k.id AND e.is_current=1
                AND e.superseded=0 AND d.deleted=0 AND s.enabled=1))
        ORDER BY k.id
    """)]


def related_knowledge(con: sqlite3.Connection, knowledge_id: int, limit: int = 12) -> dict | None:
    """Explicit links/backlinks plus same-source recommendations; no model call.

    Relations are computed from live state rather than stored as a separate
    graph. Archive and revocation take effect immediately without reindexing.
    """
    own = con.execute("SELECT id FROM knowledge WHERE id=? AND status!='archived'",
                      (knowledge_id,)).fetchone()
    if own is None:
        return None
    entries = _available_knowledge(con)
    by_id = {int(k['id']): k for k in entries}
    body = by_id.get(knowledge_id, {}).get('body', '')

    def show(ids: set[int]) -> list[dict]:
        return [{"id": k['id'], "title": k['title'], "kind": k['kind'],
                 "status": k['status']} for kid in sorted(ids) if
                (k := by_id.get(kid)) is not None and kid != knowledge_id][:limit]

    outgoing_ids = link_targets(body)
    outgoing = show(outgoing_ids)
    incoming = show({k['id'] for k in entries if k['id'] != knowledge_id
                     and knowledge_id in link_targets(k['body'])})

    # Shared project name alone is not evidence of topical relevance.
    shared_source_ids = {int(r[0]) for r in con.execute("""
        SELECT DISTINCT e2.knowledge_id
        FROM knowledge_evidence e1
        JOIN knowledge_evidence e2 ON e1.document_id=e2.document_id
        JOIN documents d ON d.id=e1.document_id
        JOIN sources s ON s.id=d.source_id
        WHERE e1.knowledge_id=? AND e2.knowledge_id!=?
          AND e1.is_current=1 AND e1.superseded=0
          AND e2.is_current=1 AND e2.superseded=0
          AND d.deleted=0 AND s.enabled=1
    """, (knowledge_id, knowledge_id))}
    explicit = set(outgoing_ids) | {k['id'] for k in incoming}
    shared = show(shared_source_ids - explicit)
    return {
        "knowledge_id": knowledge_id,
        "outgoing": outgoing,
        "backlinks": incoming,
        "same_source": shared,
        "unresolved_ids": sorted(kid for kid in outgoing_ids if kid not in by_id)[:limit],
    }


def _wiki_alias(name: str) -> str:
    """Avoid injecting wiki syntax into link display aliases."""
    return re.sub(r"[\[\]|\n\r]", " ", name).strip() or "未命名"


def _safe_folder(name: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff-]+", "-", name)[:60].strip('-') or '未分类'


def _export_body_links(body: str, available_ids: set[int]) -> str:
    def replacement(match: re.Match) -> str:
        kid = int(match.group(1))
        alias = match.group(2)
        if kid not in available_ids:
            # A broken link must not point to an archived/revoked note.
            return alias or f"已移除的知识 K{kid}"
        stable = f"K{kid:06d}"
        return f"[[{stable}|{_wiki_alias(alias)}]]" if alias else f"[[{stable}]]"
    return WIKILINK.sub(replacement, body)


def export_wiki_archive(con: sqlite3.Connection) -> bytes:
    """Stable filenames, human index and evidence without duplicating articles.

    No raw source document is exported or uploaded. Titles and project folders
    may change without breaking note-to-note links because IDs stay stable.
    """
    records = [dict(row) for row in con.execute("""
        SELECT *
        FROM knowledge WHERE status!='archived' ORDER BY id
    """)]
    ids = {int(k['id']) for k in records}
    groups: dict[str, list[dict]] = defaultdict(list)
    exported: list[tuple[str, str]] = []
    for k in records:
        evidence = [dict(r) for r in con.execute("""
            SELECT d.project,d.title AS source_title,e.quote,e.is_current,
                   e.superseded,e.occurred_at
            FROM knowledge_evidence e JOIN documents d ON d.id=e.document_id
            WHERE e.knowledge_id=? ORDER BY e.id
        """, (k['id'],))]
        projects = ([k['project']] if k['scope']!='global' and k['project'] else
                    sorted({e['project'] for e in evidence if e['project']}) if k['scope']!='global' else [])
        project = projects[0] if projects else '我的笔记'
        groups[project].append(k)
        title = k['title'].replace('\r', ' ').replace('\n', ' ').strip()
        link_name = f"K{k['id']:06d}"
        usable=bool(con.execute('SELECT k.id FROM knowledge k WHERE k.id=? AND '+READY_SQL,(k['id'],)).fetchone())
        meta = ['---', f"worktwin_id: {k['id']}",
                'title: ' + json.dumps(title, ensure_ascii=False),
                'aliases: ' + json.dumps([title],ensure_ascii=False),
                'kind: ' + json.dumps(k['kind']),
                'status: ' + json.dumps(k['status']),
                f"version: {k['version']}",
                'project: ' + json.dumps(project, ensure_ascii=False),
                *[name+': '+json.dumps(k[name],ensure_ascii=False) for name in
                    ('project_key','scope','topic','scope_detail','quality','outcome','updated_at')],
                'ai_usable: '+('true' if usable else 'false'),'---','']
        caution = ('> **暂不供 AI 使用**：'+unavailable_reason(con,k)+'。\n' if not usable else '')
        body = [*meta, f"# {title}", '', caution,
                _export_body_links(k['body'], ids), '', '## 原始依据', '']
        if not evidence:
            body.append('- 人工知识，没有关联的原始文件')
        else:
            for e in evidence:
                flag = ('历史引用，需复核' if not e['is_current'] or e['superseded']
                        else '有效')
                origin = f" · {e['occurred_at']}" if e['occurred_at'] else ''
                quote = e['quote'][:280].replace('\n', ' ').strip()
                body.append(f"- {e['source_title']}（{flag}{origin}）：{quote}")
        folder = f"projects/{_safe_folder(project)}" if projects else "personal"
        exported.append((f"{folder}/{link_name}.md",
                         '\n'.join(body) + '\n'))

    index = ['# WorkTwin · 知识索引', '',
             '可在 Obsidian 等支持 [[Wiki 链接]] 的 Markdown 工具中浏览。',
             '以稳定知识 ID 命名文件，重命名标题不会破坏相互引用。',
             '此档案是本机手动导出的知识快照，可能含未确认信息，请勿直接公开。', '']
    for project in sorted(groups):
        index.extend([f"## {project}", ''])
        for k in sorted(groups[project], key=lambda item: (item['kind'], item['title'], item['id'])):
            label = KIND_LABELS.get(k['kind'], k['kind'])
            index.append(f"- [[K{k['id']:06d}|{_wiki_alias(k['title'])}]] · {label} · {k['status']}")
        index.append('')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('README.md', '# WorkTwin 知识快照\n\n从 INDEX.md 开始浏览。\n导出数据仅供知识拥有者本地使用。\n')
        archive.writestr('INDEX.md', '\n'.join(index))
        for name, body in exported:
            archive.writestr(name, body)
    return buffer.getvalue()
