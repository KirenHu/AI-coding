"""Run with `python -m worktwin` or installed `worktwin` command."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import webbrowser
from pathlib import Path

from .config import DEFAULT_PORT, data_dir, database_path


def _configure_frozen_stdio() -> None:
    """A windowed Windows executable has no standard streams by default.

    Uvicorn and even the startup print() must have working streams; persist
    diagnostics in the user's own app-data folder rather than crashing before
    the desktop UI is available.
    """
    if not getattr(sys, "frozen", False) or (sys.stdout is not None and sys.stderr is not None):
        return
    try:
        folder = data_dir()
        folder.mkdir(parents=True, exist_ok=True)
        logfile = open(folder / "worktwin-launch.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        logfile = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = logfile
    if sys.stderr is None:
        sys.stderr = logfile


def main():
    _configure_frozen_stdio()
    parser = argparse.ArgumentParser(description="WorkTwin Collector — local work knowledge")
    parser.add_argument("command", nargs="?", choices=["serve", "scan", "where"], default="serve")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", help="Open the browser automatically")
    parser.add_argument("--data-dir", default=None, help="Override data directory")
    args = parser.parse_args()
    if args.data_dir:
        import os
        os.environ["WORKTWIN_DATA_DIR"] = str(Path(args.data_dir).expanduser().resolve())
    if args.command == "where":
        print(database_path())
        return
    if args.command == "scan":
        from .collector import Collector
        from .db import Database
        print(json.dumps(Collector(Database(database_path())).scan_all(),ensure_ascii=False))
        return
    from .api import create_app
    import uvicorn
    # The server listens to loopback only. Never expose it on 0.0.0.0.
    if args.open:
        threading.Timer(1.0,lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()
    print(f"WorkTwin 本地工作台：http://127.0.0.1:{args.port}")
    print(f"数据仅保存在：{database_path()}")
    uvicorn.run(create_app(),host="127.0.0.1",port=args.port,access_log=False)


if __name__ == "__main__":
    main()
