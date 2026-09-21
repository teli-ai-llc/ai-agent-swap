"""The Remote Control guard for pooled machines.

Claude Code's Remote Control binds a local session to whichever login the CLI
holds. On a machine that rotates through teammates' logins, a session started
while a borrowed login is active would appear in — and be drivable from — the
owner's claude.ai. ``disableRemoteControl`` turns the feature off wherever it
could start, and it lives in the per-machine ``~/.claude/settings.json``,
which cswap never swaps between accounts (session profiles link to the same
file). One write therefore covers every account the machine will ever hold,
including mid-session auto-switches.

``cswap pool login`` enforces the guard; ``cswap pool status`` and ``cswap
sync`` warn when it has since been removed. Logout leaves it alone.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from claude_swap.exceptions import PoolError
from claude_swap.fsutil import replace_with_retry
from claude_swap.paths import get_default_claude_config_home

#: Claude Code settings keys and the values a pooled machine must carry.
#: ``disableRemoteControl`` is the documented off switch; ``remoteControlAtStartup``
#: is the one setting that could otherwise connect without a human asking.
REMOTE_CONTROL_GUARD: dict[str, bool] = {
    "disableRemoteControl": True,
    "remoteControlAtStartup": False,
}


def guard_settings_path() -> Path:
    """The default profile's ``settings.json`` — deliberately not
    ``CLAUDE_CONFIG_DIR``: a session profile links this very file."""
    return get_default_claude_config_home() / "settings.json"


def _read(path: Path) -> dict | None:
    """The settings object, ``{}`` for a missing file, ``None`` when the file
    exists but is not a JSON object."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def remote_control_guard_gaps(path: Path | None = None) -> list[str]:
    """Guard keys that are missing or hold the wrong value, in guard order.
    An unreadable file reports every key: the guard is not in effect."""
    data = _read(path or guard_settings_path())
    if data is None:
        return list(REMOTE_CONTROL_GUARD)
    return [key for key, want in REMOTE_CONTROL_GUARD.items() if data.get(key) is not want]


def enforce_remote_control_guard(path: Path | None = None) -> list[str]:
    """Write the guard keys into ``settings.json``, keeping everything else.
    Returns the keys that changed (empty when nothing needed writing; the file
    is then left byte-for-byte alone). Raises ``PoolError`` rather than
    clobber a file that is not a JSON object."""
    path = path or guard_settings_path()
    data = _read(path)
    if data is None:
        raise PoolError(
            f"cannot update {path}: it is not a JSON object; fix it (or move it aside) "
            "and log in to the pool again"
        )
    changed = [key for key, want in REMOTE_CONTROL_GUARD.items() if data.get(key) is not want]
    if not changed:
        return []
    data.update(REMOTE_CONTROL_GUARD)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".cswap-tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        replace_with_retry(tmp, path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise PoolError(f"cannot write {path}: {e}") from None
    return changed
