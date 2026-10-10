"""WorkTwin-initiated task completion checks; no inbound access to an employee PC.

Only a task-status *path* signed by the trusted flow platform is accepted.
The host is pinned to WORKTWIN_CAPTURE_FLOW_ORIGIN; there is no arbitrary
callback URL or unauthenticated localhost endpoint.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fastapi import HTTPException

MAX_RESPONSE_BYTES = 4096
TASK_STATUS_INTERVAL_SECONDS = 10
# Privacy first: if task status cannot be confirmed, stop observing.
MAX_STATUS_FAILURES = 3
TASK_STATUS_TTL_SECONDS = 8 * 60 * 60

_STATUS_PATH = re.compile(r"/api/[a-zA-Z0-9_./-]{1,220}\Z")


def validate_status_path(value: object) -> str:
    """Only a canonical API path at the preconfigured, signed origin."""
    if not isinstance(value, str) or not _STATUS_PATH.fullmatch(value):
        raise HTTPException(400, "流程平台必须提供有效的任务状态查询路径")
    parts = value.split("/")
    if any(p in ("", ".", "..") for p in parts[1:]) or value.startswith("//"):
        raise HTTPException(400, "任务状态接口路径无效")
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def fetch_task_state(origin: str, task_id: str, path: str, secret: str,
                     *, clock=time.time) -> str:
    """Authenticated HTTPS GET; never follow redirects or accept a new host."""
    validate_status_path(path)
    if not origin.startswith("https://") or origin.rstrip("/") != origin:
        raise ValueError("可信流程服务地址无效")
    if len(secret) < 32:
        raise ValueError("流程平台签名未配置")
    timestamp = str(int(clock()))
    message = "\n".join(("GET", task_id, path, timestamp))
    signature = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    request = Request(origin + path, method="GET", headers={
        "X-WorkTwin-Task": task_id,
        "X-WorkTwin-Timestamp": timestamp,
        "X-WorkTwin-Signature": signature,
        "Accept": "application/json",
    })
    # Redirect refusal prevents accidental forwarding of the signed task
    # headers to a different host. urllib verifies TLS certificates.
    opener = build_opener(_NoRedirect())
    with opener.open(request, timeout=5) as response:
        if response.status != 200:
            raise ValueError("任务状态查询失败")
        content = response.read(MAX_RESPONSE_BYTES + 1)
    if len(content) > MAX_RESPONSE_BYTES:
        raise ValueError("任务状态响应过大")
    data = json.loads(content)
    if not isinstance(data, dict) or data.get("task_id") != task_id:
        raise ValueError("任务状态响应与当前任务不一致")
    status = data.get("status")
    if status not in ("running", "completed", "cancelled"):
        raise ValueError("无法识别的任务状态")
    return status


class TaskStatusMonitor:
    """Daemon with a finite number of checks and no overlapping poll loops."""

    def __init__(self, capture, *, interval: int = TASK_STATUS_INTERVAL_SECONDS,
                 fetcher=fetch_task_state):
        self.capture = capture
        self.interval = max(2, interval)
        self.fetcher = fetcher
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="worktwin-browser-status",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=6)

    def _loop(self):
        while not self._stop.is_set():
            self._stop.wait(self.interval)
            if self._stop.is_set():
                return
            try:
                self.capture.poll_task_states(self.fetcher)
            except Exception:
                # A failed check never becomes a false 'completed' observation.
                # Each task records its own bounded failure count.
                pass
