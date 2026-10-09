"""Minimal execution metadata for local coding sessions.

Tool input/output may contain credentials and unrelated private content, so
neither is copied into the AI-facing transcript. An execution result is not a
validated software outcome. All text below is *evidence metadata*, not a
statement attributed to the user or to the assistant.
"""
from __future__ import annotations

import re

_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.:/-]{0,79}$")
_EXIT = re.compile(
    r"(?:Process exited with code|Exit code|exit_code|exit status)\s*[:=]?\s*(-?\d+)",
    re.IGNORECASE,
)


def safe_tool_name(value: object) -> str:
    name = str(value or "").strip()
    return name if _TOOL_NAME.fullmatch(name) else "unknown_tool"


def exit_code_from_output(value: object) -> int | None:
    """Parse an explicitly recorded process exit code, never infer it from text."""
    if not isinstance(value, str):
        return None
    match = _EXIT.search(value[:4000])
    return int(match.group(1)) if match else None


def render_action(timestamp: str, name: str, *, result: bool = False,
                  failed: bool = False, exit_code: int | None = None) -> str:
    """Represent an action without arbitrary arguments, stdout or stderr.

    'Returned' means the tool emitted an output event, not that the operation
    succeeded or that the product result was verified.
    """
    if not result:
        status = "结果未记录"
    elif failed or (exit_code is not None and exit_code != 0):
        status = "执行报告失败"
    elif exit_code == 0:
        status = "进程退出码 0（不代表成果已验收）"
    else:
        status = "有结果返回（执行成功与否未核实）"
    return f"### 操作 · {timestamp}\n工具：{safe_tool_name(name)}\n结果：{status}"


def sort_key(record: tuple[str, int, str]) -> tuple[str, int]:
    return record[0], record[1]
