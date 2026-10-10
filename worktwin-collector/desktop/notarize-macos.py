"""Submit once, record the ID, require Accepted, then staple and validate.

Use --resume with the original artifact and its .notary.json receipt to wait
for an existing submission instead of uploading a duplicate.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


def notary(*args):
    result = subprocess.run([
        'xcrun', 'notarytool', *args,
        '--key', os.environ['WORKTWIN_NOTARY_KEY_PATH'],
        '--key-id', os.environ['APPLE_NOTARY_KEY_ID'],
        '--issuer', os.environ['APPLE_NOTARY_ISSUER_ID'],
        '--output-format', 'json',
    ], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('Apple request failed or wait timed out; receipt retained')
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('artifact', type=Path)
    parser.add_argument('staple_target', type=Path)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    receipt = args.artifact.with_name(args.artifact.name + '.notary.json')
    checksum = hashlib.sha256(args.artifact.read_bytes()).hexdigest()
    if args.resume:
        report = json.loads(receipt.read_text())
        if report['sha256'] != checksum:
            raise RuntimeError('Artifact changed since submission; cannot reuse receipt')
    else:
        report = notary('submit', str(args.artifact))
        # Persist only submission metadata, never credential material.
        report = {'id': str(uuid.UUID(report['id'])), 'sha256': checksum,
                  'artifact': args.artifact.name}
        receipt.write_text(json.dumps(report) + '\n')
    submission_id = str(uuid.UUID(report['id']))
    print(f'Apple submission: {submission_id}; receipt: {receipt.name}', flush=True)
    report = notary('wait', submission_id, '--timeout', '2h')
    if report.get('status') != 'Accepted':
        raise RuntimeError('Apple submission was not Accepted')
    print(f'Apple notarization Accepted: {args.artifact.name}; submission {submission_id}', flush=True)
    for operation in ('staple', 'validate'):
        subprocess.run(['xcrun', 'stapler', operation, str(args.staple_target)], check=True)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('::error::Notarization/stapling incomplete. Sensitive diagnostics suppressed; release blocked. Retain artifact and receipt to resume.')
        sys.exit(1)
