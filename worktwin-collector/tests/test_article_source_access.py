from fastapi.testclient import TestClient
from worktwin.api import create_app
from tests.test_library_organization import fixture
from tests.test_v04 import auth


def test_article_source_access_links_actual_sources_and_restores_availability(tmp_path):
    app=create_app(tmp_path/'db',start_worker=False)
    a,b,c=fixture(app.state.db)
    with TestClient(app) as client:
        headers=auth(client)
        sources=client.get(f'/api/knowledge/{a}/sources',headers=headers).json()
        assert len(sources)==1
        source=sources[0]['id']
        assert client.get(f'/api/knowledge/{a}/sources').status_code==403
        client.put(f'/api/sources/{source}/ai',headers=headers,json={'allow_ai':False}).raise_for_status()
        blocked=client.get(f'/api/knowledge-item/{a}',headers=headers).json()
        assert blocked['unavailable_reason']=='来源未允许 AI 使用'
        assert not client.get(f'/api/knowledge/{a}/sources',headers=headers).json()[0]['allow_ai']
        client.put(f'/api/sources/{source}/ai',headers=headers,json={'allow_ai':True}).raise_for_status()
        assert client.get(f'/api/knowledge-item/{a}',headers=headers).json()['can_use']
