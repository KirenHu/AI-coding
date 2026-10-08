"""Authorization and citation-policy regression tests."""
import json
import re
from fastapi.testclient import TestClient

from worktwin.answer_policy import check_answer
from worktwin.api import create_app
from worktwin.evaluation import PRIVATE_MARKER, evaluate


def test_citation_policy_accepts_only_selected_ids():
    accepted = check_answer('采用流程节点 [K7]。', {7})
    assert accepted.state == 'cited' and accepted.cited_ids == {7}
    invalid = check_answer('采用流程节点 [K7]，机密在 [K8]。', {7})
    assert invalid.state == 'blocked' and not invalid.cited_ids and '[K8]' not in invalid.answer
    unsupported = check_answer('这是模型凭空给出的事实。', {7})
    assert unsupported.state == 'blocked' and not unsupported.cited_ids
    refusal = check_answer('现在还不知道。', {7})
    assert refusal.state == 'abstained' and not refusal.cited_ids


def test_synthetic_evaluation_reproducible_and_scrubbed():
    result = evaluate(mode='mock')
    assert result['passed'] == result['total'], result
    assert result['total'] >= 7
    dumped = json.dumps(result, ensure_ascii=False)
    assert PRIVATE_MARKER not in dumped
    assert 'Bearer ' not in dumped
    assert 'approval.md' not in dumped
    assert not any('answer' in case for case in result['cases'])


def test_unauthorized_api_requests_cannot_read_knowledge(tmp_path):
    app = create_app(tmp_path/'knowledge.sqlite', start_worker=False)
    with TestClient(app) as client:
        assert client.get('/api/knowledge').status_code == 403
        assert client.get('/api/twins').status_code == 403
        assert client.get('/api/documents').status_code == 403
        assert client.get('/api/health').status_code == 200
        assert re.search(r'window\.__WORKTWIN_TOKEN__="(.*?)";', client.get('/').text)
