"""Optional typed judgment model for project matching.

Jev is a SystemOne decision API, not an OpenAI chat-completions model.
Only narrow yes/no judgments are offloaded; knowledge extraction and free
text synthesis always use the main model. Failure falls back to main model.
"""
from __future__ import annotations

import json
from urllib.request import Request

from .model_settings import PersonalModel, validate_url
from .model_transport import ModelConnectionError, model_urlopen


class JevDecisionModel:
    def __init__(self, base_url: str, key: str, model: str = 'jev-latest'):
        base=validate_url(base_url)
        if base.endswith('/v1'):
            base=base[:-3]
        self.url,self.key,self.model=base,key.strip(),model.strip() or 'jev-latest'

    @property
    def configured(self):
        return bool(self.url and self.key and self.model)

    def evaluate(self, state, questions):
        if not self.configured:
            raise ModelConnectionError('请先配置判断模型')
        payload={'model':self.model,'state':state,'questions':questions}
        request=Request(self.url+'/v1/systemone',
            data=json.dumps(payload,ensure_ascii=False).encode('utf-8'),
            headers={'Authorization':'Bearer '+self.key,'Content-Type':'application/json'},
            method='POST')
        with model_urlopen(request,timeout=25) as response:
            body=response.read(1024*1024+1)
        if len(body)>1024*1024:
            raise ModelConnectionError('判断模型响应超出限制')
        try:
            parsed=json.loads(body)
            if not isinstance(parsed.get('answers'),dict):
                raise ValueError()
            return parsed
        except (ValueError,TypeError):
            raise ModelConnectionError('判断模型未返回有效的 SystemOne 结果') from None

    def test(self):
        data=self.evaluate(
            {'work':'WorkTwin source ingestion','candidate':'WorkTwin knowledge processing'},
            {'same_project':{'type':'noul',
                'instructions':'Are these two descriptions about the same named software project?',
                'criteria':{'true':'Both refer to WorkTwin','false':'The projects differ'}}})
        score=data['answers'].get('same_project',{}).get('noul')
        if not isinstance(score,(float,int)) or not 0<=score<=1:
            raise ModelConnectionError('判断模型未返回有效的 Noul 概率')
        return {'ok':True,'model':self.model}


class DecisionRouter:
    def __init__(self, db, secure, main_model, *, enterprise_url='', enterprise_token=''):
        self.db,self.secure,self.main=db,secure,main_model
        self.enterprise_url,self.enterprise_token=enterprise_url,enterprise_token

    def config(self):
        if self.main.mode=='enterprise':
            return {'provider':'enterprise','configured':bool(self.enterprise_url and self.enterprise_token)}
        provider=self.db.setting('decision_provider','main')
        return {'provider':provider,'configured':provider!='main',
                'base_url':self.db.setting('decision_base_url'),
                'model':self.db.setting('decision_model'),
                'has_key':bool(self.secure.get('decision_model_key')) if provider!='main' else False}

    def _specialized(self):
        settings=self.config()
        if settings['provider'] in ('main','enterprise'):
            return None
        base,key,model=(settings['base_url'],self.secure.get('decision_model_key'),settings['model'])
        if settings['provider']=='jev':
            return JevDecisionModel(base,key,model)
        return PersonalModel(base,key,model)

    def _enterprise(self, state, questions):
        if not self.enterprise_url or not self.enterprise_token:
            return None
        request=Request(self.enterprise_url.rstrip('/')+'/v1/decisions',method='POST',
            data=json.dumps({'state':state,'questions':questions},ensure_ascii=False).encode(),
            headers={'Authorization':'Bearer '+self.enterprise_token,'Content-Type':'application/json'})
        with model_urlopen(request,timeout=30) as response:
            result=json.load(response)
        return result if result.get('specialized') else None

    @staticmethod
    def _score_jev(data):
        a=data.get('answers',{})
        same=a.get('same_project',{}).get('noul')
        conflicting=a.get('different_project',{}).get('noul')
        if not all(isinstance(p,(int,float)) and 0<=p<=1 for p in (same,conflicting)):
            raise ValueError('Malformed decision probabilities')
        return float(same),float(conflicting)

    @staticmethod
    def _questions():
        return {
            'same_project':{
                'type':'noul',
                'instructions':'Do these WORK UNITS belong to exactly the same business or software project (not just the same topic, folder or organization)? Judge identity rather than mere similarity.',
                'criteria':{'true':'Same durable project with consistent identifiers and objective','false':'Only topic/name similarity or unrelated project'}
            },
            'different_project':{
                'type':'noul',
                'instructions':'Is there affirmative evidence that these work units are from different projects, customers, repositories or incompatible objectives?',
                'criteria':{'true':'Explicit contradiction or incompatible identity','false':'No explicit identity conflict'}
            }
        }

    def compare(self, work_unit: dict, candidate: dict) -> dict:
        # Never send entire documents to the fast model. This is a bounded,
        # already authorized project-relation decision.
        state={'work_unit':work_unit,'candidate':candidate}
        questions=self._questions()
        score=None
        source='main'
        try:
            if self.main.mode=='enterprise':
                data=self._enterprise(state,questions)
                if data is not None:
                    score=self._score_jev(data);source='enterprise-specialized'
            else:
                client=self._specialized()
                if isinstance(client,JevDecisionModel) and client.configured:
                    score=self._score_jev(client.evaluate(state,questions));source='jev'
                elif client is not None and client.configured:
                    response=client.chat([
                        {'role':'system','content':'判断是否为完全相同的项目身份。只能返回 JSON: {"same_project":true|false,"different_project":true|false}，不要猜测，不要依据目录名或相似主题认定同一项目。'},
                        {'role':'user','content':json.dumps(state,ensure_ascii=False)}],
                        max_tokens=160)
                    parsed=json.loads(response)
                    score=(1.0 if parsed.get('same_project') is True else 0.0,
                           1.0 if parsed.get('different_project') is True else 0.0)
                    source='chat-specialized'
        except Exception:
            # Configuration, network or malformed reply never makes ingestion fail.
            score=None
        if score is None:
            try:
                response=self.main.chat([
                    {'role':'system','content':'你负责判断两个工作单元是否确实属于同一业务项目。不得仅凭同名、相似主题或目录推断。只返回 JSON {"same_project":bool,"different_project":bool}。'},
                    {'role':'user','content':json.dumps(state,ensure_ascii=False)}
                ],max_tokens=180)
                parsed=json.loads(response.strip().removeprefix('\x60\x60\x60json').removesuffix('\x60\x60\x60').strip())
                score=(1.0 if parsed.get('same_project') is True else 0.0,
                       1.0 if parsed.get('different_project') is True else 0.0)
            except Exception:
                score=(0.0,0.0)
            source='main-fallback'
        return {'same':score[0],'conflict':score[1],'engine':source}
