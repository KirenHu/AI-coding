import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import pytest
from fastapi import HTTPException
from worktwin.provider import ProviderService, ChatRequest


def service(tmp_path,monkeypatch,limit=2):
    monkeypatch.setenv('WORKTWIN_BYOK_API_KEY','secret-provider-key')
    monkeypatch.setenv('WORKTWIN_BYOK_MODEL','company-model')
    monkeypatch.setenv('WORKTWIN_DAILY_CALL_LIMIT',str(limit))
    return ProviderService(tmp_path/'usage.sqlite')


class Reply:
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self):return json.dumps({'choices':[{'message':{'content':'回答'}}], 'usage':{'total_tokens':20},'unsafe_extra':'secret-provider-key'}).encode()


def test_budget_persists_and_audit_has_no_prompts_or_tokens(tmp_path,monkeypatch):
    p=service(tmp_path,monkeypatch)
    body=ChatRequest(messages=[{'role':'user','content':'PRIVATE-CONTENT'}])
    for _ in range(2):
        r=p.complete('employee-secret-token',body,transport=lambda *a,**kw:Reply())
        assert 'secret-provider-key' not in str(r)
    reopened=ProviderService(tmp_path/'usage.sqlite')
    with pytest.raises(HTTPException) as e:reopened.complete('another-employee',body)
    assert e.value.status_code==429
    with p.connect() as con:
        rows=con.execute('SELECT * FROM usage').fetchall()
        assert len(rows)==2
        assert 'PRIVATE-CONTENT' not in str(rows) and 'employee-secret-token' not in str(rows)


def test_concurrent_reservations_never_exceed_daily_limit(tmp_path,monkeypatch):
    p=service(tmp_path,monkeypatch,limit=1)
    entered,resume=Event(),Event()
    def slow(*args,**kwargs):entered.set();assert resume.wait(10);return Reply()
    body=ChatRequest(messages=[{'role':'user','content':'hello'}])
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(p.complete,'one',body,transport=slow)
        assert entered.wait(5)
        with pytest.raises(HTTPException) as e:p.complete('two',body,transport=slow)
        assert e.value.status_code==429
        resume.set();assert first.result()['choices']


def test_failed_provider_keeps_reservation_and_redacts_error(tmp_path,monkeypatch):
    p=service(tmp_path,monkeypatch,limit=1)
    def broken(*args,**kwargs):raise ValueError('secret-provider-key PRIVATE-CONTENT')
    body=ChatRequest(messages=[{'role':'user','content':'hello'}])
    with pytest.raises(HTTPException) as e:p.complete('one',body,transport=broken)
    assert e.value.status_code==502 and 'secret' not in e.value.detail
    with p.connect() as con:assert con.execute('SELECT state,tokens FROM usage').fetchone()==('error',None)
    with pytest.raises(HTTPException) as e:p.complete('one',body,transport=broken)
    assert e.value.status_code==429
