"""Always restore the runner keychain list and remove signing material."""
import os
from pathlib import Path
import re
import shutil
import subprocess

root = Path(os.environ["RUNNER_TEMP"]) / "worktwin-signing"
previous = root / "previous-keychains.txt"
if previous.exists():
    paths = re.findall(r'"([^"]+)"', previous.read_text())
    subprocess.run(["security", "list-keychains", "-d", "user", "-s", *paths], check=True)
keychain = root / "signing.keychain-db"
if keychain.exists():
    subprocess.run(["security", "delete-keychain", str(keychain)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
shutil.rmtree(root, ignore_errors=True)
