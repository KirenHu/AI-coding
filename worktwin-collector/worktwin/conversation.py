"""Conversation context for the current authorized knowledge snapshot."""
import hashlib
import json
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal['user', 'assistant']
    content: str = Field(min_length=1, max_length=12000)


class ConversationQuestion(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    project_key: str | None = Field(default=None, max_length=1000)
    messages: list[ChatMessage] = Field(default_factory=list, max_length=20)
    context_revision: str | None = Field(default=None, max_length=64)


def prepare_conversation(payload: ConversationQuestion, rows: list[dict], context: str):
    snapshot = [context, payload.project_key,
                sorted((r['id'], r.get('version', 1), r.get('project_key', ''), r['body'],
                    json.dumps(r.get('source_passages', []),ensure_ascii=False,sort_keys=True)) for r in rows)]
    revision = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False).encode()).hexdigest()
    if payload.messages and payload.context_revision != revision:
        raise HTTPException(409, '知识或授权范围已更新，请开启新会话')
    recent, length = [], 0
    for message in reversed(payload.messages):
        if length + len(message.content) > 20000:
            break
        recent.append(message.model_dump())
        length += len(message.content)
    return revision, list(reversed(recent))
