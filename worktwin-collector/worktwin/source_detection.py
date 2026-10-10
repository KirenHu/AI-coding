"""Recognize supported local transcripts before granting a source.

Detection only samples record structure; it neither indexes content nor calls a
model. Counts describe the bounded sample, not all sessions in the directory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .config import codex_sessions_path, claude_projects_path


def detect_transcripts(kind: str, root: str | None = None) -> dict:
    default = {'codex': codex_sessions_path(), 'claude': claude_projects_path()}.get(kind)
    path = Path(root).expanduser().resolve() if root else default
    result = {'kind': kind, 'root': str(path or ''), 'exists': bool(path and path.is_dir()),
              'recognized': 0, 'inspected': 0, 'unreadable': 0, 'sampled': True}
    if not result['exists']:
        return result
    for index, (folder, dirs, files) in enumerate(os.walk(path)):
        dirs[:] = [d for d in dirs if d not in ('.git', 'node_modules', '.venv', 'venv')
                   and not Path(folder, d).is_symlink()]
        if index >= 500:
            break
        for name in files:
            if not name.endswith('.jsonl'):
                continue
            result['inspected'] += 1
            try:
                with Path(folder, name).open('rb') as stream:
                    lines = stream.read(128 * 1024).splitlines()[:80]
                for line in lines:
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    if recognizable(kind, event):
                        result['recognized'] += 1
                        break
            except OSError:
                result['unreadable'] += 1
            if result['inspected'] >= 64:
                return result
    return result


def recognizable(kind: str, event: dict) -> bool:
    typ = event.get('type')
    if kind == 'codex':
        return typ in ('session_meta', 'turn_context', 'response_item', 'event_msg') and isinstance(event.get('payload'), dict)
    if kind == 'claude':
        return typ in ('user', 'assistant') and isinstance(event.get('message'), dict) and bool(event.get('sessionId') or event.get('uuid'))
    if kind == 'cursor':
        return bool(event.get('session_id')) and (typ in ('user','assistant','result') or
            typ == 'system' and event.get('subtype') == 'init')
    return False
