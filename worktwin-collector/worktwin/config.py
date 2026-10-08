"""Local storage locations; no network or cloud account required."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "WorkTwin Collector"
DEFAULT_PORT = 8765
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_SESSION_BYTES = 64 * 1024 * 1024
MAX_TEXT_CHARS = 800_000
POLL_INTERVAL_SECONDS = 20


def data_dir() -> Path:
    override = os.environ.get("WORKTWIN_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / "worktwin"


def database_path() -> Path:
    return data_dir() / "worktwin.sqlite"


def codex_sessions_path() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"


def claude_projects_path() -> Path:
    return Path(os.environ.get("CLAUDE_HOME", "~/.claude")).expanduser() / "projects"
