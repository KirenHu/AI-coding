"""Secret storage: native keychains for desktop, private encrypted files on Linux/server."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from pathlib import Path

from cryptography.fernet import Fernet


class EncryptedSecrets:
    label = '加密存储'

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.lock = threading.RLock()

    def _cipher(self):
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        keyfile = self.folder / 'credential.key'
        if not keyfile.exists():
            try:
                fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as out:
                    out.write(Fernet.generate_key())
            except FileExistsError:
                pass
        if os.name == 'posix':
            self.folder.chmod(0o700)
            keyfile.chmod(0o600)
        return Fernet(keyfile.read_bytes())

    def _read(self):
        path = self.folder / 'credentials.json'
        return json.loads(path.read_text()) if path.exists() else {}

    def get(self, name):
        with self.lock:
            value = self._read().get(name)
            return self._cipher().decrypt(value.encode()).decode() if value else ''

    def set(self, name, value):
        with self.lock:
            cipher = self._cipher()
            values = self._read()
            if value:
                values[name] = cipher.encrypt(value.encode()).decode()
            else:
                values.pop(name, None)
            target = self.folder / 'credentials.json'
            temp = self.folder / 'credentials.tmp'
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump(values, out)
            os.replace(temp, target)


class DesktopSecrets:
    def __init__(self, database: Path):
        self.account = hashlib.sha256(str(database.resolve()).encode()).hexdigest()
        self.native = sys.platform in ('darwin', 'win32')
        self.label = '系统钥匙串' if sys.platform == 'darwin' else 'Windows 凭据管理器' if self.native else '本机加密存储'
        self.files = EncryptedSecrets(database.parent / 'credentials') if not self.native else None

    def _backend(self):
        # Select only trusted OS backends, never a third-party plaintext fallback.
        if sys.platform == 'darwin':
            from keyring.backends.macOS import Keyring
        else:
            from keyring.backends.Windows import WinVaultKeyring as Keyring
        return Keyring()

    def get(self, name):
        try:
            if self.files:
                return self.files.get(name)
            return self._backend().get_password('WorkTwin ' + name, self.account) or ''
        except Exception as exc:
            raise RuntimeError('系统安全存储不可用，请解锁钥匙串或凭据管理器后重试') from exc

    def set(self, name, value):
        try:
            if self.files:
                return self.files.set(name, value)
            backend = self._backend()
            if value:
                backend.set_password('WorkTwin ' + name, self.account, value)
            elif backend.get_password('WorkTwin ' + name, self.account):
                backend.delete_password('WorkTwin ' + name, self.account)
        except Exception as exc:
            raise RuntimeError('系统安全存储不可用，未保存凭据；请解锁后重试') from exc
