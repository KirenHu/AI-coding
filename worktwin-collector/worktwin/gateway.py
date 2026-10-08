"""Enterprise BYOK endpoint; provider credentials never leave this process."""
import os
import secrets
from urllib.request import urlopen
from fastapi import Depends, FastAPI, Header, HTTPException
from .provider import ChatRequest, ProviderService


def create_gateway() -> FastAPI:
    tokens = [x.strip() for x in os.getenv('WORKTWIN_ENTERPRISE_TOKENS', '').split(',') if x.strip()]
    if not tokens:
        raise RuntimeError('企业网关需设置 WORKTWIN_ENTERPRISE_TOKENS')
    provider = ProviderService()
    app = FastAPI(title='WorkTwin Enterprise BYOK Gateway', docs_url=None, redoc_url=None)
    app.state.provider = provider

    @app.get('/health')
    def health():
        return {'ok': True, 'model_configured': True}

    def authorize(authorization: str = Header(default='')):
        if not any(secrets.compare_digest(authorization, 'Bearer ' + token) for token in tokens):
            raise HTTPException(401, '未获得企业模型访问权限')
        return authorization

    @app.post('/v1/chat/completions')
    def completions(body: ChatRequest, authorization=Depends(authorize)):
        return provider.complete(authorization, body, transport=urlopen)

    return app
