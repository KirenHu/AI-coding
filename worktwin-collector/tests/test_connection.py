from fastapi.testclient import TestClient
from worktwin.api import create_app
from worktwin.publishing import PublishingClient
from tests.test_sharing import auth


def test_provisioned_server_credentials_enable_model_and_sharing(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKTWIN_SERVER_URL', 'https://enterprise.example')
    monkeypatch.setenv('WORKTWIN_SERVER_TOKEN', 'employee-token')
    app = create_app(tmp_path/'local.sqlite', start_worker=False)
    assert app.state.publisher.client.configured
    model = app.state.knowledge_worker.client
    assert model.configured and model.url == app.state.publisher.client.url
    assert model.token == 'employee-token'


def test_pending_remote_revocation_blocks_switch_even_after_local_disable(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKTWIN_SERVER_URL', 'https://old.example')
    monkeypatch.setenv('WORKTWIN_SERVER_TOKEN', 'old-token')
    monkeypatch.setattr(PublishingClient, 'request', lambda *a, **k: {'ok': True})
    app = create_app(tmp_path/'local.sqlite', start_worker=False)
    app.state.db.set_setting('publication_error', 'pending revocation')
    with TestClient(app) as c:
        r = c.put('/api/connection', headers=auth(c), json={'url':'https://new.example', 'token':'new-employee-token-32-chars'})
        assert r.status_code == 409
        assert app.state.publisher.client.url == 'https://old.example'
        assert app.state.db.setting('enterprise_url') == ''
