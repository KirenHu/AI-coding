"""Release safety gates can be checked without Apple credentials or macOS."""
import importlib.util
import json
import hashlib
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


SUBMISSION = '00000000-0000-4000-8000-000000000001'


def configure(module, monkeypatch, tmp_path, resume=False):
    artifact = tmp_path / 'WorkTwin.zip'
    artifact.write_bytes(b'signed fixture')
    target = tmp_path / 'WorkTwin.app'
    monkeypatch.setattr(module.sys, 'argv', ['notarize', str(artifact), str(target)] + (['--resume'] if resume else []))
    for name in ('WORKTWIN_NOTARY_KEY_PATH', 'APPLE_NOTARY_KEY_ID', 'APPLE_NOTARY_ISSUER_ID'):
        monkeypatch.setenv(name, 'dummy')
    return artifact, target


@pytest.mark.parametrize('returncode,status', [(0, 'Invalid'), (0, 'In Progress'), (1, 'Accepted')])
def test_rejected_or_incomplete_notarization_never_staples(monkeypatch, tmp_path, returncode, status):
    module = load('notarize-macos')
    artifact, _ = configure(module, monkeypatch, tmp_path)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[2] == 'submit':
            return subprocess.CompletedProcess(args, 0, json.dumps({'id': SUBMISSION}), '')
        return subprocess.CompletedProcess(args, returncode, json.dumps({'status': status, 'id': SUBMISSION}), '')

    monkeypatch.setattr(module.subprocess, 'run', run)
    with pytest.raises(RuntimeError):
        module.main()
    assert [c[2] for c in calls] == ['submit', 'wait']
    receipt = json.loads(artifact.with_name(artifact.name + '.notary.json').read_text())
    assert receipt == {'id': SUBMISSION, 'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(), 'artifact': artifact.name}


def test_accepted_submission_must_staple_and_validate(monkeypatch, tmp_path):
    module = load('notarize-macos')
    _, target = configure(module, monkeypatch, tmp_path)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps({'status': 'Accepted', 'id': SUBMISSION}), '')

    monkeypatch.setattr(module.subprocess, 'run', run)
    module.main()
    assert calls[2:] == [
        ['xcrun', 'stapler', 'staple', str(target)],
        ['xcrun', 'stapler', 'validate', str(target)],
    ]


@pytest.mark.parametrize('changed', [False, True])
def test_resume_never_uploads_duplicate_or_accepts_changed_artifact(monkeypatch, tmp_path, changed):
    module = load('notarize-macos')
    artifact, _ = configure(module, monkeypatch, tmp_path, resume=True)
    receipt = {'id': SUBMISSION, 'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(), 'artifact': artifact.name}
    artifact.with_name(artifact.name + '.notary.json').write_text(json.dumps(receipt))
    if changed:
        artifact.write_bytes(b'changed fixture')
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps({'status': 'Accepted', 'id': SUBMISSION}), '')

    monkeypatch.setattr(module.subprocess, 'run', run)
    if changed:
        with pytest.raises(RuntimeError, match='changed'):
            module.main()
        assert not calls
    else:
        module.main()
        assert [c[2] for c in calls] == ['wait', 'staple', 'validate']
