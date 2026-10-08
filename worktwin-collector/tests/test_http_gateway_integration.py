"""End-to-end enterprise BYOK gateway HTTP transport, without a paid provider.

A real uvicorn gateway forwards to a local HTTP provider substitute. The app
uses its real urllib gateway client and persists real source/evidence rows.
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from worktwin.api import create_app
from worktwin.inference import GatewayClient
from worktwin.jobs import KnowledgeWorker


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def test_real_enterprise_gateway_transport_and_knowledge_authorization(tmp_path):
    requests = []
    expected_quote = '我们最终决定把审批配置为工作流节点，避免重复维护两套审批系统。'

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.path == '/v1/chat/completions'
            data = json.loads(self.rfile.read(int(self.headers['content-length'])))
            requests.append((self.headers['Authorization'], data))
            raw = data['messages'][-1]['content']
            if '<source>\n' in raw:
                assert expected_quote in raw
                answer = json.dumps({'items': [{
                    'title': '审批采用工作流节点', 'kind': 'decision',
                    'body': '复用现有工作流上下文，避免独立系统重复维护。', 'quote': expected_quote
                }]}, ensure_ascii=False)
            else:
                assert '[K1]' in raw  # twin is given assigned knowledge and its provenance
                answer = '根据 [K1] 的知识，审批复用工作流节点实现。'
            body = json.dumps({'choices': [{'message': {'content': answer}}]}, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    gateway_port = free_port()
    env = {**os.environ,
           'WORKTWIN_BYOK_BASE_URL': f'http://127.0.0.1:{provider.server_port}/v1',
           'WORKTWIN_BYOK_API_KEY': 'enterprise-provider-only-secret',
           'WORKTWIN_BYOK_MODEL': 'centrally-billed-model',
           'WORKTWIN_ENTERPRISE_TOKENS': 'pilot-employee-access',
           'WORKTWIN_SERVER_DATA_DIR': str(tmp_path/'server-data')}
    process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'worktwin.gateway:create_gateway',
                                '--factory', '--host', '127.0.0.1', '--port', str(gateway_port),
                                '--no-access-log'], env=env, cwd=Path(__file__).parents[1],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        gateway_url = f'http://127.0.0.1:{gateway_port}'
        for _ in range(80):
            try:
                if httpx.get(gateway_url + '/health', timeout=.7, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(.1)
        else:
            raise AssertionError('Enterprise gateway did not start')
        assert httpx.post(gateway_url+'/v1/chat/completions',json={'messages':[]},timeout=5, trust_env=False).status_code == 401
        source = tmp_path / 'consented'
        private = tmp_path / 'unconsented'
        source.mkdir(); private.mkdir()
        (source / 'decision.md').write_text(expected_quote, encoding='utf-8')
        (private / 'secret.md').write_text('仅本地保留，不得发送给企业模型：SECRET-9988', encoding='utf-8')
        client = GatewayClient(url=gateway_url, token='pilot-employee-access')
        app = create_app(tmp_path / 'store.sqlite', start_worker=False, inference_client=client)
        with TestClient(app) as api:
            html = api.get('/').text
            access = {'X-Worktwin-Token': re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";', html).group(1)}
            approved = api.post('/api/sources', headers=access, json={
                'name': 'work', 'root': str(source), 'allow_ai': True}).json()['id']
            api.post('/api/sources', headers=access, json={
                'name': 'private', 'root': str(private), 'allow_ai': False})
            app.state.collector.scan_all()
            assert len(api.get('/api/ai/jobs', headers=access).json()) == 1
            worker = KnowledgeWorker(app.state.db, client=client)
            assert worker.process_next()['state'] == 'done'
            extracted = api.get('/api/knowledge', headers=access).json()
            assert len(extracted) == 1 and extracted[0]['evidence'][0]['is_current']
            twin = api.post('/api/twins', headers=access, json={'name': '项目交接助手'}).json()['id']
            knowledge_id = extracted[0]['id']
            api.put(f'/api/twins/{twin}/knowledge', headers=access, json={'knowledge_ids': [knowledge_id]})
            answer = api.post(f'/api/twins/{twin}/ask', headers=access, json={
                'question': '为什么把审批配置为工作流节点？'})
            assert answer.status_code == 200, answer.text
            assert answer.json()['citations'][0]['knowledge_id'] == knowledge_id
            assert len(requests) == 2
            assert all(auth == 'Bearer enterprise-provider-only-secret' for auth, _ in requests)
            assert all(payload['model'] == 'centrally-billed-model' for _, payload in requests)
            assert 'SECRET-9988' not in json.dumps(requests, ensure_ascii=False)
            api.delete(f'/api/sources/{approved}', headers=access)
            assert api.get('/api/twins', headers=access).json()[0]['knowledge_count'] == 0
    finally:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill()
        provider.shutdown()
        provider.server_close()
