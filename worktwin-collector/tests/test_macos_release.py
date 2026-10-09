"""Release safety gates can be checked without Apple credentials or macOS."""
import importlib.util
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'desktop' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_credentials_cannot_prepare_unsigned_release(monkeypatch):
    module = load('prepare-macos-signing')
    for name in ('MACOS_CERTIFICATE_P12', 'MACOS_CERTIFICATE_PASSWORD',
                 'APPLE_NOTARY_KEY_P8', 'APPLE_NOTARY_KEY_ID', 'APPLE_NOTARY_ISSUER_ID'):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match='refusing unsigned release'):
        module.main()


@pytest.mark.parametrize('returncode,status', [(0, 'Invalid'), (0, 'In Progress'), (1, 'Accepted')])
def test_rejected_or_incomplete_notarization_never_staples(monkeypatch, returncode, status):
    module = load('notarize-macos')
    monkeypatch.setattr(module.sys, 'argv', ['notarize', 'WorkTwin.zip', 'WorkTwin.app'])
    for name in ('WORKTWIN_NOTARY_KEY_PATH', 'APPLE_NOTARY_KEY_ID', 'APPLE_NOTARY_ISSUER_ID'):
        monkeypatch.setenv(name, 'dummy')
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode, f'{{"status":"{status}","id":"test"}}', '')

    monkeypatch.setattr(module.subprocess, 'run', run)
    with pytest.raises(RuntimeError):
        module.main()
    assert len(calls) == 1
    assert calls[0][:3] == ['xcrun', 'notarytool', 'submit']


def test_accepted_submission_must_staple_and_validate(monkeypatch):
    module = load('notarize-macos')
    monkeypatch.setattr(module.sys, 'argv', ['notarize', 'WorkTwin.dmg', 'WorkTwin.dmg'])
    for name in ('WORKTWIN_NOTARY_KEY_PATH', 'APPLE_NOTARY_KEY_ID', 'APPLE_NOTARY_ISSUER_ID'):
        monkeypatch.setenv(name, 'dummy')
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, '{"status":"Accepted","id":"test"}', '')

    monkeypatch.setattr(module.subprocess, 'run', run)
    module.main()
    assert calls[1:] == [
        ['xcrun', 'stapler', 'staple', 'WorkTwin.dmg'],
        ['xcrun', 'stapler', 'validate', 'WorkTwin.dmg'],
    ]
