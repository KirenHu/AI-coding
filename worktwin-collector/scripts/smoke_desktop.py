"""Black-box smoke test for frozen macOS/Windows WorkTwin applications.

Builds are only considered usable after the *packaged executable*, not the
Python source tree, starts the loopback web service and serves the dashboard.
No employee data or enterprise credentials are involved.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python scripts/smoke_desktop.py <executable>", file=sys.stderr)
        return 2
    executable = Path(sys.argv[1]).resolve()
    if not executable.is_file():
        print(f"Executable does not exist: {executable}", file=sys.stderr)
        return 2

    opener = build_opener(ProxyHandler({}))  # Never route localhost via a CI proxy.
    with tempfile.TemporaryDirectory(prefix="worktwin-native-smoke-") as temp:
        env = os.environ.copy()
        env["WORKTWIN_DATA_DIR"] = temp
        # A smoke check must not send local data to any model service.
        env.pop("WORKTWIN_GATEWAY_URL", None)
        env.pop("WORKTWIN_GATEWAY_TOKEN", None)
        process = subprocess.Popen(
            [str(executable)],
            cwd=str(executable.parent),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        last_error: Exception | None = None
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"Native executable exited with status {process.returncode}")
                try:
                    with opener.open("http://127.0.0.1:8765/api/health", timeout=2) as response:
                        health = json.load(response)
                    if health.get("ok") is not True or health.get("enterprise_model") is not False:
                        raise AssertionError(f"Unexpected health response: {health}")
                    with opener.open("http://127.0.0.1:8765/", timeout=2) as response:
                        page = response.read().decode("utf-8")
                    if "WorkTwin" not in page:
                        raise AssertionError("Dashboard was not served by packaged app")
                    if not (Path(temp) / "worktwin.sqlite").is_file():
                        raise AssertionError("Packaged app did not initialize local database")
                    print("PASS: frozen executable started, served HTTP, and created isolated SQLite DB")
                    return 0
                except (ConnectionError, HTTPError, URLError, TimeoutError, OSError) as exc:
                    last_error = exc
                    time.sleep(1)
            raise RuntimeError(f"Packaged executable did not serve the dashboard: {last_error}")
        except (RuntimeError, AssertionError) as exc:
            logfile = Path(temp) / "worktwin-launch.log"
            if logfile.exists():
                print("Recent launch diagnostics:\n" + logfile.read_text(encoding="utf-8")[-3500:], file=sys.stderr)
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
