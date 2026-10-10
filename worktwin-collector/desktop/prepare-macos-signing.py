"""Import CI secrets into an ephemeral keychain; never print sensitive output."""
import base64
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"{args[0]} operation failed (diagnostics suppressed)")
    return result.stdout


def main():
    required = ("MACOS_CERTIFICATE_P12", "MACOS_CERTIFICATE_PASSWORD",
                "APPLE_NOTARY_KEY_P8", "APPLE_NOTARY_KEY_ID", "APPLE_NOTARY_ISSUER_ID")
    if any(not os.environ.get(name) for name in required):
        raise RuntimeError("Missing signing/notarization secrets; refusing unsigned release")
    root = Path(os.environ["RUNNER_TEMP"]) / "worktwin-signing"
    root.mkdir(mode=0o700, exist_ok=True)
    certificate = root / "certificate.p12"
    key = root / "notary.p8"
    keychain = root / "signing.keychain-db"
    certificate.write_bytes(base64.b64decode(os.environ["MACOS_CERTIFICATE_P12"], validate=True))
    key.write_text(os.environ["APPLE_NOTARY_KEY_P8"])
    certificate.chmod(0o600)
    key.chmod(0o600)
    password = secrets.token_urlsafe(32)
    previous = run("security", "list-keychains", "-d", "user")
    (root / "previous-keychains.txt").write_text(previous)
    run("security", "create-keychain", "-p", password, str(keychain))
    run("security", "set-keychain-settings", "-lut", "21600", str(keychain))
    run("security", "unlock-keychain", "-p", password, str(keychain))
    try:
        run("security", "import", str(certificate), "-k", str(keychain),
            "-P", os.environ["MACOS_CERTIFICATE_PASSWORD"],
            "-T", "/usr/bin/codesign", "-T", "/usr/bin/security")
    finally:
        certificate.unlink(missing_ok=True)
    run("security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:",
        "-s", "-k", password, str(keychain))
    old_paths = re.findall(r'"([^"]+)"', previous)
    run("security", "list-keychains", "-d", "user", "-s", str(keychain), *old_paths)
    identities = run("security", "find-identity", "-v", "-p", "codesigning", str(keychain))
    matches = re.findall(r'([A-Fa-f0-9]{40}) "Developer ID Application:[^"]+"', identities)
    if len(matches) != 1:
        raise RuntimeError("Expected exactly one valid Developer ID Application identity")
    with open(os.environ["GITHUB_ENV"], "a") as output:
        output.write(f"WORKTWIN_CODESIGN_IDENTITY={matches[0]}\n")
        output.write(f"WORKTWIN_NOTARY_KEY_PATH={key}\n")
    print("Developer ID identity imported; notarization key prepared")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("::error::Signing preparation failed; check secrets and certificate validity. Sensitive diagnostics suppressed.")
        sys.exit(1)
