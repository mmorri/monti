"""Subscription token storage.

Tokens live at ~/.config/model-router/<provider>/auth.json with mode 0600,
keyed per provider. Atomic write + fsync; no other file in the config dir may
hold key material (see acceptance criterion 5).
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Credentials:
    access: str = ""
    refresh: str = ""
    # Epoch millis when `access` should be considered expired (skew applied).
    expires: int = 0
    account_id: str = ""
    email: str = ""
    # Provider-specific extras (e.g. copilot api base, chatgpt account id).
    extra: dict | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        if data.get("extra") is None:
            data.pop("extra", None)
        return {k: v for k, v in data.items() if v != "" and v is not None}

    @classmethod
    def from_dict(cls, data: dict) -> "Credentials":
        known = {f for f in ("access", "refresh", "expires", "account_id", "email", "extra")}
        return cls(**{k: v for k, v in data.items() if k in known})


class TokenStore:
    def __init__(self, root: Path | None = None):
        self.root = root or (Path.home() / ".config" / "model-router")

    def providers(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir()
                      if p.is_dir() and (p / "auth.json").is_file())

    def path_for(self, provider: str) -> Path:
        return self.root / provider / "auth.json"

    def load(self, provider: str) -> Credentials | None:
        path = self.path_for(provider)
        try:
            data = json.loads(path.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        creds = Credentials.from_dict(data)
        if not creds.access:
            return None
        self._ensure_mode(path)
        return creds

    def save(self, provider: str, creds: Credentials) -> Path:
        directory = self.root / provider
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        path = directory / "auth.json"
        fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".auth-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(creds.to_dict(), handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        self._ensure_mode(path)
        return path

    def clear(self, provider: str) -> bool:
        path = self.path_for(provider)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    @staticmethod
    def _ensure_mode(path: Path) -> None:
        try:
            if stat.S_IMODE(os.stat(path).st_mode) != 0o600:
                os.chmod(path, 0o600)
        except OSError:
            pass
