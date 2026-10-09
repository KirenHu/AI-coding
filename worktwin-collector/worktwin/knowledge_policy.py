"""One formal knowledge policy for selection, local answers and publication."""

READY_SQL = """k.status='confirmed' AND k.needs_review=0 AND k.kind!='preference'
    AND k.scope IN ('project','session','global') AND k.quality='useful'
    AND k.outcome!='reported' AND k.attribution!='assistant'
    AND (k.source_bound=0 OR EXISTS(SELECT 1 FROM knowledge_evidence e
        WHERE e.knowledge_id=k.id AND e.is_current=1 AND e.superseded=0))
    AND NOT EXISTS(SELECT 1 FROM knowledge_evidence e JOIN documents d ON d.id=e.document_id
        JOIN sources s ON s.id=d.source_id WHERE e.knowledge_id=k.id AND s.allow_ai=0)"""
SHARE_SQL = READY_SQL + """ AND NOT EXISTS(SELECT 1 FROM knowledge_evidence e JOIN documents d ON d.id=e.document_id
    JOIN sources s ON s.id=d.source_id WHERE e.knowledge_id=k.id AND s.allow_share=0)"""


def unavailable_reason(con, knowledge, *, sharing=False):
    if knowledge['status']=='archived':
        return '已归档'
    if knowledge['scope']=='unknown':
        return '适用范围尚未核对'
    if knowledge['quality']!='useful':
        return '内容价值待核对' if knowledge['quality']=='uncertain' else '已停用：低价值内容'
    if knowledge['outcome']=='reported':
        return '仅报告完成，尚无验证依据'
    if knowledge['attribution']=='assistant':
        return 'AI 建议尚未确认为工作知识'
    if knowledge['needs_review']:
        return '来源或更新需要核对'
    if knowledge['status']!='confirmed':
        return '尚未确认内容'
    if knowledge['kind']=='preference':
        return '个人偏好仅供本人管理'
    if knowledge['source_bound'] and not con.execute('SELECT 1 FROM knowledge_evidence WHERE knowledge_id=? AND is_current=1 AND superseded=0',(knowledge['id'],)).fetchone():
        return '没有有效来源'
    permissions=con.execute('''SELECT s.allow_ai,s.allow_share FROM knowledge_evidence e JOIN documents d ON d.id=e.document_id
        JOIN sources s ON s.id=d.source_id WHERE e.knowledge_id=?''',(knowledge['id'],)).fetchall()
    if any(not r['allow_ai'] for r in permissions):
        return '来源未允许 AI 使用'
    if sharing and any(not r['allow_share'] for r in permissions):
        return '来源未允许分享'
    return ''
