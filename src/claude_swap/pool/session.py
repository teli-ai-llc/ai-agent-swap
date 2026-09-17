"""The member's pool session and this machine's id, both in the backup root."""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from claude_swap.settings import atomic_write_json

POOL_SESSION_FILENAME = "pool_session.json"
MACHINE_ID_FILENAME = "machine_id"
SESSION_SCHEMA_VERSION = 1

_logger = logging.getLogger("claude-swap")

# JSON keys are camelCase like every other file in the backup root.
_FIELD_TO_KEY = {
    "url": "url",
    "anon_key": "anonKey",
    "access_token": "accessToken",
    "refresh_token": "refreshToken",
    "expires_at": "expiresAt",
    "user_id": "userId",
    "email": "email",
}


@dataclass(frozen=True)
class PoolSession:
    """Everything needed to talk to the pool as one signed-in member."""

    url: str
    anon_key: str
    access_token: str
    refresh_token: str
    expires_at: float
    user_id: str
    email: str


def session_path(backup_root: Path) -> Path:
    return backup_root / POOL_SESSION_FILENAME


def load_session(backup_root: Path) -> PoolSession | None:
    """The stored session, or None when absent, corrupt, or incomplete."""
    path = session_path(backup_root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        _logger.warning("Could not read %s (%s); not logged in to the pool", path, e)
        return None
    if not isinstance(raw, dict):
        return None
    kwargs = {}
    for field, key in _FIELD_TO_KEY.items():
        value = raw.get(key)
        if field == "expires_at":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            kwargs[field] = float(value)
        else:
            if not isinstance(value, str) or not value:
                return None
            kwargs[field] = value
    return PoolSession(**kwargs)


def save_session(backup_root: Path, session: PoolSession) -> None:
    data = {"schemaVersion": SESSION_SCHEMA_VERSION}
    for field, key in _FIELD_TO_KEY.items():
        data[key] = getattr(session, field)
    atomic_write_json(session_path(backup_root), data)


def clear_session(backup_root: Path) -> bool:
    """Remove the session file; False when there was none."""
    try:
        os.unlink(session_path(backup_root))
    except FileNotFoundError:
        return False
    return True


def machine_id(backup_root: Path) -> str:
    """This install's stable id, created on first use."""
    path = backup_root / MACHINE_ID_FILENAME
    try:
        text = path.read_text(encoding="utf-8").strip()
        return str(uuid.UUID(text))
    except (FileNotFoundError, OSError, ValueError):
        pass
    fresh = str(uuid.uuid4())
    backup_root.mkdir(parents=True, exist_ok=True)
    path.write_text(fresh + "\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)
    return fresh
