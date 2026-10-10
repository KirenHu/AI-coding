"""Jev SystemOne routing and safe fallback without live provider credentials."""
import json
import pytest

from worktwin.db import Database
from worktwin.decision_model import DecisionRouter, JevDecisionModel


class FakeResponse:
    def __init__(self, data):
        self.data=json.dumps(data).encode()
    def __enter__(self): return self
    def __exit__(self,*exc): return False
    def read(self,n=None): return self.data if n is None else self.data[:n]


class Main:
    mode='personal'
    configured=True
    url='https://main.invalid/v1'
    def __init__(self):
        self.calls=0
    def chat(self,messages,max_tokens=180):
        self.calls+=1
        return '{"same_project":true,"different_project":false}'


class Secrets:
    def get(self,key):
        assert key=='decision_model_key'
        return 'dummy-secret'


def test_jev_calls_typed_api_not_chat_endpoint(monkeypatch):
    recorded={}
    def reply(request,timeout=25):
        recorded['url']=request.full_url
        recorded['authorization']=request.get_header('Authorization')
        recorded['request']=json.loads(request.data)
        return FakeResponse({'answers':{'same_project':{'type':'noul','noul':.97},
                        'different_project':{'type':'noul','noul':.01}}})
    monkeypatch.setattr('worktwin.decision_model.model_urlopen',reply)
    model=JevDecisionModel('https://api.typesafe.ai/v1','dummy-secret','jev-latest')
    data=model.evaluate({'work_unit':{'title':'WorkTwin'},'candidate':{'title':'WorkTwin'}},
                        DecisionRouter._questions())
    assert model.url=='https://api.typesafe.ai'
    assert recorded['url']=='https://api.typesafe.ai/v1/systemone'
    assert recorded['authorization']=='Bearer dummy-secret'
    assert recorded['request']['model']=='jev-latest'
    assert 'messages' not in recorded['request']
    assert data['answers']['same_project']['noul']==.97


def test_fast_decisions_do_not_call_main_model(monkeypatch,tmp_path):
    db=Database(tmp_path/'db.sqlite')
    db.set_setting('decision_provider','jev')
    db.set_setting('decision_base_url','https://api.typesafe.ai')
    db.set_setting('decision_model','jev-latest')
    main=Main()
    monkeypatch.setattr(JevDecisionModel,'evaluate',lambda *args:
        {'answers':{'same_project':{'noul':.97},'different_project':{'noul':.01}}})
    result=DecisionRouter(db,Secrets(),main).compare({'topic':'知识'}, {'name':'WorkTwin'})
    assert result['engine']=='jev' and result['same']==.97
    assert main.calls==0


def test_specialized_failure_uses_main_model(monkeypatch,tmp_path):
    db=Database(tmp_path/'db.sqlite')
    db.set_setting('decision_provider','jev')
    db.set_setting('decision_base_url','https://api.typesafe.ai')
    db.set_setting('decision_model','jev-latest')
    def fail(*a):
        raise RuntimeError('temporary network error')
    monkeypatch.setattr(JevDecisionModel,'evaluate',fail)
    main=Main()
    result=DecisionRouter(db,Secrets(),main).compare({'topic':'知识'}, {'name':'WorkTwin'})
    assert result['engine']=='main-fallback'
    assert result['same']==1.0 and main.calls==1


def test_jev_rejects_invalid_probability(monkeypatch):
    model=JevDecisionModel('https://api.typesafe.ai','dummy','jev-latest')
    monkeypatch.setattr(model,'evaluate',lambda *a: {'answers':{'same_project':{'noul':1.5}}})
    with pytest.raises(Exception):
        model.test()
