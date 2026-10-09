"""Conservative work scope and quality gates, shared by ingestion and answers."""
from __future__ import annotations

import re
from datetime import datetime, timezone

SCOPE_LABELS = {'project': '项目内', 'session': '本次讨论', 'global': '跨项目通用', 'unknown': '范围待核对'}
OUTCOME_LABELS = {'none': '', 'reported': '报告完成，尚未核实', 'supported': '已有验证依据', 'accepted': '用户已验收'}
NOISE = re.compile(r'^(?:用户)?(?:倾向于|选择了?|同意了?)?(?:第[一二三四五六七八九十\d]+[点项个]|这个方案|上述方案)[。！.!]*$|^(?:继续|好的|同意|收到|谢谢|完成了)[。！.!]*$')


def document_scope(document) -> dict:
    """A home directory is not a business project. Never merge by basename alone."""
    if 'project_verified' in document.keys() and document['project_verified'] and document['project_key']:
        return {key:document[key] for key in ('project_key','project','scope')}
    content = document['content']
    cwd = re.search(r'^工作目录：([^\n]+)', content)
    if cwd:
        # A working directory is a clue, never a verified business project.
        # Even a well-named directory can host unrelated work in two sessions.
        key = 'session:' + str(document['id'])
        scope = 'session'
    else:
        # A folder name is only a hint, never verification of a business
        # project. Even sibling documents may belong to unrelated projects.
        # Stable per-document scope prevents accidental cross-file merging.
        key = 'session:' + str(document['id'])
        scope = 'session'
    return {'project_key': key, 'project': document['project'], 'scope': scope}


def source_time(value: str) -> str:
    """Canonical UTC time for event chronology; never substitute scan time."""
    if not value:
        return ''
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return ''
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime('%Y-%m-%d %H:%M:%S')


def permission_key(con, document_id: int) -> str:
    row = con.execute('''SELECT s.id,s.enabled,s.allow_ai,s.allow_share FROM sources s
        JOIN documents d ON d.source_id=s.id WHERE d.id=?''', (document_id,)).fetchone()
    # Independent authorization grants are intentionally kept separate even
    # when their current booleans happen to be equal.
    return ':'.join(str(v) for v in row) if row else ''


def low_value(title: str, body: str) -> bool:
    text = re.sub(r'\s+', '', title)
    return bool(NOISE.search(text) or NOISE.fullmatch(re.sub(r'\s+', '', body)))


def topic_key(topic: str) -> str:
    return re.sub(r'[\W_]+', '', topic.casefold())[:100]


def scope_description(item) -> str:
    label = SCOPE_LABELS.get(item.get('scope', 'unknown'), '范围待核对')
    return ' · '.join(x for x in (label, item.get('project', ''), item.get('scope_detail', '')) if x)


def snapshot_history(con, item) -> None:
    con.execute('''INSERT INTO knowledge_history
        (knowledge_id,version,kind,title,body,status,scope,project,project_key,topic,scope_detail,quality,outcome)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''', tuple(item[x] for x in
        ('id','version','kind','title','body','status','scope','project','project_key','topic','scope_detail','quality','outcome')))
