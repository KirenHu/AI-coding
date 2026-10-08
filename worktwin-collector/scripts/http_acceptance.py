"""Black-box loopback HTTP acceptance against a running WorkTwin server.

Uses temporary, synthetic files and no enterprise model credentials.
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx


def available_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    with tempfile.TemporaryDirectory(prefix='worktwin-http-') as tmp:
        base = Path(tmp)
        docs = base / 'docs'
        docs.mkdir()
        (docs / 'approval-plan.md').write_text(
            '# Approval design\n审批节点应复用现有工作流，以避免重复维护审批系统。'
            '本文用于测试，不含真实项目资料。', encoding='utf-8')
        (docs / 'workflow-context.md').write_text(
            '# Workflow overview\n流程节点支持条件分支和任务分配。'
            '这是一份纯合成的测试文档。', encoding='utf-8')
        port = available_port()
        env = {**os.environ, 'WORKTWIN_DATA_DIR': str(base / 'state'),
               'WORKTWIN_GATEWAY_URL': '', 'WORKTWIN_GATEWAY_TOKEN': ''}
        server = subprocess.Popen([sys.executable, '-m', 'worktwin', 'serve', '--port', str(port)],
                                  env=env, cwd=Path(__file__).parents[1],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            url = f'http://127.0.0.1:{port}'
            for _ in range(80):
                try:
                    if httpx.get(url + '/api/health', timeout=1,trust_env=False).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(.1)
            else:
                raise RuntimeError('WorkTwin loopback service did not start')
            with httpx.Client(base_url=url, timeout=12,trust_env=False) as client:
                html = client.get('/').text
                token = re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";', html).group(1)
                h = {'X-Worktwin-Token': token}
                assert client.get('/api/documents').status_code == 403
                assert client.get('/api/documents', headers=h).status_code == 200
                assert '我的数字分身' in html
                assert client.get('/', headers={'Host': 'attacker.invalid'}).status_code == 400
                added = client.post('/api/sources', headers=h, json={'name': '项目资料', 'root': str(docs), 'kind': 'folder'})
                assert added.status_code == 200, added.text
                for _ in range(80):
                    if len(client.get('/api/documents', headers=h).json()) == 2:
                        break
                    time.sleep(.1)
                documents = client.get('/api/documents', headers=h).json()
                assert len(documents) == 2, documents
                assert client.get('/api/knowledge', headers=h).json() == []
                assert client.get('/api/ai/jobs', headers=h).json() == []
                assert client.get('/api/projects', headers=h).json()
                assert client.get('/api/search', headers=h, params={'q': '审批节点'}).json()['documents']
                doc = client.get(f"/api/documents/{documents[0]['id']}", headers=h).json()
                quote = doc['content'][:40]
                assert len(quote) >= 8
                created = client.post('/api/knowledge', headers=h, json={
                    'title': '工作流审批设计原则', 'kind': 'decision', 'status': 'confirmed',
                    'body': '复用工作流节点以避免审批系统重复维护。',
                    'document_id': doc['id'], 'quote': quote})
                assert created.status_code == 200, created.text
                kid = created.json()['id']
                twin = client.post('/api/twins', headers=h, json={'name': '交接助手'}).json()['id']
                assigned = client.put(f'/api/twins/{twin}/knowledge', headers=h, json={'knowledge_ids': [kid]})
                assert assigned.status_code == 200, assigned.text
                assert client.get('/api/twins', headers=h).json()[0]['knowledge_count'] == 1
                assert client.post(f'/api/twins/{twin}/ask', headers=h, json={'question': '为什么做审批节点？'}).status_code == 503
                # Changing the source invalidates old knowledge without silently reusing its evidence.
                source_file = docs / doc['relative_path']
                source_file.write_text('这一版已经被整体替换；旧的原文证据不再存在。', encoding='utf-8')
                for _ in range(70):
                    result = client.get('/api/knowledge', headers=h).json()
                    if result and result[0]['needs_review']:
                        break
                    time.sleep(.2)
                assert result[0]['needs_review'] == 1, result
                assert client.get('/api/twins', headers=h).json()[0]['knowledge_count'] == 0
                assert client.put(f'/api/twins/{twin}/knowledge', headers=h, json={'knowledge_ids': [kid]}).status_code == 400
                removed = client.delete(f"/api/sources/{added.json()['id']}", headers=h)
                assert removed.status_code == 200, removed.text
                assert client.get('/api/documents', headers=h).json() == []
                assert client.get('/api/knowledge', headers=h).json() == []
                assert client.get(f'/api/twins/{twin}', headers=h).json()['knowledge_ids'] == []
                print(json.dumps({'result': 'PASS', 'documents': len(documents), 'manual_knowledge': 1,
                                  'twin_assignment': 'PASS', 'stale_permission_guard': 'PASS',
                                  'revocation_cleanup': 'PASS', 'local_token': 'PASS', 'host_guard': 'PASS'},
                                 ensure_ascii=False))
        finally:
            server.terminate()
            try:
                server.wait(timeout=6)
            except subprocess.TimeoutExpired:
                server.kill()


if __name__ == '__main__':
    main()
