"""Submit an App ZIP or DMG, require Accepted, then staple and validate."""
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    artifact, staple_target = map(Path, sys.argv[1:])
    result = subprocess.run([
        "xcrun", "notarytool", "submit", str(artifact),
        "--key", os.environ["WORKTWIN_NOTARY_KEY_PATH"],
        "--key-id", os.environ["APPLE_NOTARY_KEY_ID"],
        "--issuer", os.environ["APPLE_NOTARY_ISSUER_ID"],
        "--wait", "--timeout", "30m", "--output-format", "json",
    ], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Apple submission failed or timed out")
    report = json.loads(result.stdout)
    if report.get("status") != "Accepted":
        raise RuntimeError("Apple submission was not Accepted")
    print(f"Apple notarization Accepted: {artifact.name}; submission {report['id']}")
    for operation in ("staple", "validate"):
        subprocess.run(["xcrun", "stapler", operation, str(staple_target)], check=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("::error::Notarization/stapling failed. Sensitive diagnostics suppressed; release blocked.")
        sys.exit(1)
