"""Minimal self-hosted enterprise BYOK gateway.

Provider keys live ONLY on the enterprise host, never on employee machines.
This is a pilot gateway; deploy behind organization auth, TLS and monitoring.
"""

from __future__ import annotations

import json
import os
import secrets
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    messages: list[dict]
    max_tokens: int = Field(default=1800, ge=1, le=5000)
    temperature: float = Field(default=0.1, ge=0, le=1)


def create_gateway() -> FastAPI:
    provider_url = os.getenv("WORKTWIN_BYOK_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    provider_key = os.getenv("WORKTWIN_BYOK_API_KEY", "")
    model = os.getenv("WORKTWIN_BYOK_MODEL", "")
    client_tokens = [x.strip() for x in os.getenv("WORKTWIN_ENTERPRISE_TOKENS", "").split(",") if x.strip()]
    if not provider_key or not model or not client_tokens:
        raise RuntimeError("企业网关需设置 BYOK_API_KEY、BYOK_MODEL 和 ENTERPRISE_TOKENS")
    parsed = urlparse(provider_url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")):
        raise ValueError("模型提供方必须使用 HTTPS，除非连接本机测试服务")
    app = FastAPI(title="WorkTwin Enterprise BYOK Gateway", docs_url=None, redoc_url=None)

    @app.get("/health")
    def health():
        return {"ok": True, "model_configured": True}

    @app.post("/v1/chat/completions")
    def completions(body: ChatRequest, authorization: str = Header(default="")):
        if not any(secrets.compare_digest(authorization, "Bearer " + key) for key in client_tokens):
            raise HTTPException(401, "未获得企业模型访问权限")
        # Do not accept an arbitrary model or forwarding URL from employee clients.
        payload = json.dumps({"model": model, "messages": body.messages,
                              "max_tokens": body.max_tokens, "temperature": body.temperature},
                             ensure_ascii=False).encode()
        request = Request(provider_url + "/chat/completions", data=payload, method="POST",
                          headers={"Authorization": "Bearer " + provider_key, "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=120) as response:
                return json.load(response)
        except Exception as exc:
            # Never relay upstream response bodies or credential-bearing errors.
            raise HTTPException(502, "企业模型服务暂时不可用") from exc

    return app
