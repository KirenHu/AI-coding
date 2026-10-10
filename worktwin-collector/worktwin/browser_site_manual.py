"""Maintain one evolving, locally grounded operation guide per manually watched site.

Only explicit manual browser sessions qualify. A workflow-signed capture is
never promoted into the knowledge library. Source records stay in the browser
event tables; the guide references those records and never stores input values.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import hashlib
import json
from urllib.parse import urlsplit

from .scope import snapshot_history


def operation_text(kind: str, label: str) -> str:
    label = str(label or '').strip()
    for prefix in ('按钮：','字段：','链接：','控件：'):
        if label.startswith(prefix):
            label = label[len(prefix):]
            break
    if label == '[隐藏]':
        label = ''
    if kind == 'click':
        return f'点击「{label}」' if label else '点击网页控件（名称已隐藏）'
    if kind == 'change':
        return f'修改「{label}」（未采集输入值）' if label else '修改表单项（未采集输入值）'
    if kind == 'submit':
        return '尝试提交表单'
    if kind == 'feedback':
        return {'feedback_success':'页面提示操作成功（后台结果未核实）',
                'feedback_failure':'页面提示操作失败',
                'feedback_unknown':'页面显示状态提示（内容未采集）'}.get(label,'页面出现提示')
    if kind == 'navigation':
        return '在网站内继续浏览' if '继续观察' in label else '离开当前监控页面'
    return ''


class BrowserSiteManuals:
    def __init__(self, db):
        self.db = db
        with db.connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS browser_site_manuals (
                  site TEXT PRIMARY KEY,
                  knowledge_id INTEGER NOT NULL UNIQUE REFERENCES knowledge(id) ON DELETE CASCADE,
                  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS browser_site_observations (
                  session_id TEXT PRIMARY KEY REFERENCES browser_capture_sessions(id) ON DELETE CASCADE,
                  site TEXT NOT NULL,
                  signature TEXT NOT NULL,
                  path TEXT NOT NULL,
                  summary TEXT NOT NULL,
                  steps_json TEXT NOT NULL,
                  event_seq INTEGER NOT NULL,
                  recorded_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_browser_site_observations_site
                  ON browser_site_observations(site, recorded_at);
            """)

    @staticmethod
    def _body(site, observations):
        groups = defaultdict(list)
        for observation in observations:
            groups[observation['signature']].append(observation)
        arranged = sorted(groups.values(),
                          key=lambda items: max((x['recorded_at'],x['record_order'])
                                                for x in items), reverse=True)
        lines = [
            '# ' + site + ' · 网站操作手册',
            '',
            '本手册由本人明确开启的网站操作记录自动整理，持续合并同一网站的后续操作。',
            '它仅描述观察到的控件操作和页面提示；不包含表单值、截图或未实际发生的步骤。',
            '页面提示成功不代表后台业务结果已经核实。',
            '',
        ]
        for group in arranged:
            last = max(group, key=lambda row: (row['recorded_at'],row['record_order']))
            first_path = last['path'] or '/'
            timestamp = datetime.fromtimestamp(last['recorded_at']).strftime('%Y-%m-%d %H:%M')
            lines.extend([f'## 页面 {first_path}',
                          f'最近记录：{timestamp} · 观察 {len(group)} 次', ''])
            lines.extend(f'- {step}' for step in json.loads(last['steps_json']))
            lines.extend(['', '**操作概括**：'+last['summary'],
                          '原始操作记录：'+last['session_id']+'（信息采集 → 网页操作记录）',
                          ''])
        return '\n'.join(lines).rstrip() + '\n'

    def sync(self, session_id: str):
        """Publish a completed manual session; repeat calls do not create versions."""
        with self.db.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            session = con.execute("""SELECT s.*,r.summary,r.event_seq,
                    r.status summary_status FROM browser_capture_sessions s
                JOIN browser_capture_summaries r ON r.session_id=s.id
                WHERE s.id=?""",(session_id,)).fetchone()
            if not session or session['mode']!='manual' or session['summary_status']!='final':
                return {'state':'skipped'}
            rows = [dict(row) for row in con.execute("""SELECT seq,kind,label,location
                FROM browser_capture_events WHERE session_id=? ORDER BY seq""",(session_id,))]
            steps = [operation_text(row['kind'],row['label']) for row in rows]
            steps = [step for step in steps if step]
            if not steps:
                return {'state':'skipped'}
            canonical = [(row['kind'],row['label'],row['location'])
                         for row in rows if row['kind'] in
                            ('click','change','submit','feedback','navigation')]
            if not canonical:
                return {'state':'skipped'}
            signature = hashlib.sha256(json.dumps(canonical,ensure_ascii=False)
                                       .encode('utf-8')).hexdigest()
            site = session['page_key']
            path = next((urlsplit(row['location']).path for row in rows
                         if row['location'].startswith(site)), '/') or '/'
            exists = con.execute("""SELECT event_seq,summary,signature FROM browser_site_observations
                WHERE session_id=?""",(session_id,)).fetchone()
            if exists and (exists['event_seq']==session['event_seq']
                           and exists['summary']==session['summary']
                           and exists['signature']==signature):
                return {'state':'unchanged'}
            con.execute("""INSERT INTO browser_site_observations
                (session_id,site,signature,path,summary,steps_json,event_seq,recorded_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET
                  signature=excluded.signature,path=excluded.path,
                  summary=excluded.summary,steps_json=excluded.steps_json,
                  event_seq=excluded.event_seq,recorded_at=excluded.recorded_at""",
                (session_id,site,signature,path,session['summary'],
                 json.dumps(steps,ensure_ascii=False),session['event_seq'],
                 session['ended_at'] or session['created_at']))
            observations = [dict(x) for x in con.execute("""
                SELECT rowid AS record_order,* FROM browser_site_observations WHERE site=?
                ORDER BY recorded_at DESC,rowid DESC""",(site,))]
            body = self._body(site, observations)
            record = con.execute("SELECT knowledge_id FROM browser_site_manuals WHERE site=?",
                                 (site,)).fetchone()
            knowledge = con.execute("SELECT * FROM knowledge WHERE id=?",
                                    (record['knowledge_id'],)).fetchone() if record else None
            if knowledge and knowledge['created_by']=='human':
                # Preserve owner text: subsequent observations remain available.
                return {'state':'owner_edited','knowledge_id':knowledge['id']}
            if knowledge and knowledge['body']==body:
                return {'state':'unchanged','knowledge_id':knowledge['id']}
            host=urlsplit(site).netloc
            title=('网站操作手册 · '+host)[:130]
            project=('网站操作 · '+host)[:200]
            project_key='browser-site:'+hashlib.sha256(site.encode()).hexdigest()[:24]
            if knowledge:
                snapshot_history(con,knowledge)
                con.execute("""UPDATE knowledge SET title=?,body=?,
                    version=version+1,updated_at=datetime('now') WHERE id=?""",
                    (title,body,knowledge['id']))
                knowledge_id=knowledge['id']
            else:
                knowledge_id=con.execute("""INSERT INTO knowledge
                    (kind,title,body,status,created_by,source_bound,
                     scope,project,project_key,topic,scope_detail,
                     quality,attribution,outcome)
                    VALUES('process',?,?,'confirmed','enterprise_ai',0,
                           'project',?,?,'网站操作手册',?,
                           'useful','user','none')""",
                    (title,body,project,project_key,
                     '仅限 '+site+' 的本人网页操作')).lastrowid
                con.execute("""INSERT INTO browser_site_manuals(site,knowledge_id)
                    VALUES(?,?)""",(site,knowledge_id))
            con.execute("""UPDATE browser_site_manuals SET updated_at=datetime('now')
                WHERE site=?""",(site,))
        self.db.event('browser_site_manual_updated','已自动维护 '+site+' 的网站操作手册')
        return {'state':'updated','knowledge_id':knowledge_id,'site':site}
