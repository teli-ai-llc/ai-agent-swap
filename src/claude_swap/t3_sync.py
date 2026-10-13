"""Reconcile cswap accounts into T3's watched provider settings file.

Like T3's own theme CLI, updates merge the sparse settings object, compare
its original contents immediately before an atomic write, and retry if a
concurrent edit is observed. Only entries carrying this store's ownership
marker are managed. Credentials stay in cswap's existing session profiles.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable

from claude_swap.exceptions import ClaudeSwitchError, ConfigError
from claude_swap.locking import FileLock
from claude_swap.session import SessionManager
from claude_swap.settings import atomic_write_json, load_t3_settings

_logger = logging.getLogger("claude-swap")
STATE_FILE = "t3_sync_state.json"
MARKER = "CSWAP_MANAGED_BY"


def provider_name(email: str, *, active: bool = False) -> str:
    """Use the account identity rather than an alias or provider prefix."""
    return f"{email} · active" if active else email


def target_path(backup_root: Path) -> Path:
    configured = load_t3_settings(backup_root).settings_path
    home = Path(os.environ.get("T3CODE_HOME") or Path.home() / ".t3")
    return (Path(configured).expanduser() if configured else home / "userdata" / "settings.json").resolve()


def owner_key(backup_root: Path) -> str:
    return hashlib.sha256(str(backup_root.resolve()).encode()).hexdigest()[:12]


def instance_id(backup_root: Path, record: dict) -> str:
    identity = (record.get("uuid") or record["email"], record.get("organizationUuid") or "")
    digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:20]
    return f"cswap_{owner_key(backup_root)}_{digest}"


def is_managed(entry: object, owner: str) -> bool:
    return isinstance(entry, dict) and entry.get("driver") == "claudeAgent" and isinstance(entry.get("environment"), list) and any(
        isinstance(variable, dict) and variable.get("name") == MARKER
        and variable.get("value") == f"cswap:{owner}"
        for variable in entry.get("environment", [])
    )


def _owns_instance(key: str, entry: object, owner: str) -> bool:
    # T3's Duplicate action can copy environment variables. A manually named
    # duplicate retaining our marker must not become an automatic deletion.
    return key.startswith(f"cswap_{owner}_") and is_managed(entry, owner)


def read_state(backup_root: Path) -> dict:
    try:
        value = json.loads((backup_root / STATE_FILE).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _read_target(path: Path) -> tuple[str, dict]:
    try:
        raw = path.read_text(encoding="utf-8")
        settings = json.loads(raw)
    except FileNotFoundError:
        raise ConfigError(f"T3 settings not found at {path}. Start T3 first, or set t3.settingsPath.") from None
    except (OSError, ValueError):
        raise ConfigError(f"Cannot read T3 settings at {path}; repair the file before syncing.") from None
    if not isinstance(settings, dict) or not isinstance(settings.get("providerInstances", {}), dict):
        raise ConfigError("T3 settings and providerInstances must be JSON objects; nothing was changed.")
    return raw, settings


def _entry(owner: str, name: str, home_path: str, *, enabled: bool = True) -> dict:
    return {
        "driver": "claudeAgent", "displayName": name, "enabled": enabled,
        "environment": [{"name": MARKER, "value": f"cswap:{owner}", "sensitive": False}],
        "config": {"binaryPath": "claude", "homePath": home_path},
    }


def reconcile_settings(
    path: Path, owner: str, desired: dict[str, dict | None], *,
    before_compare: Callable[[], None] | None = None,
    names: dict[str, str] | None = None,
) -> dict:
    """None retains a managed profile but disables new launches (active/failed).

    This preserves stable paths for existing per-account profiles. The one
    dynamic active provider always uses the default Claude directory.
    """
    with FileLock(path.with_name(".cswap-provider-sync.lock")):
        for _attempt in range(5):
            raw, settings = _read_target(path)
            previous = settings.get("providerInstances", {})
            entries = dict(previous)
            added, updated, removed = [], [], []
            for key, wanted in desired.items():
                existing = entries.get(key)
                if existing is not None and not is_managed(existing, owner):
                    raise ConfigError(f"T3 provider id {key} is already in use; nothing was changed.")
                if wanted is None:
                    if existing is not None:
                        replacement = {**existing, "enabled": False}
                        if names and key in names:
                            replacement["displayName"] = names[key]
                    else:
                        continue
                else:
                    replacement = wanted
                    if existing is not None:
                        # Keep custom models, launch flags, and other user preferences.
                        replacement = {**existing, **wanted,
                                       "config": {**existing.get("config", {}), **wanted["config"]},
                                       "environment": [
                                           v for v in existing.get("environment", [])
                                           if v.get("name") != MARKER
                                       ] + wanted["environment"]}
                if replacement != existing:
                    (added if existing is None else updated).append(key)
                    entries[key] = replacement
            for key, existing in previous.items():
                if _owns_instance(key, existing, owner) and key not in desired:
                    removed.append(key)
                    del entries[key]
            result = {"added": added, "updated": updated, "removed": removed,
                      "managedCount": sum(_owns_instance(k, e, owner) for k, e in entries.items())}
            if entries == previous:
                return result
            if before_compare:
                before_compare()
            if path.read_text(encoding="utf-8") != raw:
                continue
            atomic_write_json(path, {**settings, "providerInstances": entries})
            return result
    raise ConfigError("T3 settings kept changing during sync; retry after the settings editor is idle.")


def _roster(switcher) -> dict:
    # A malformed/missing roster must never be treated as an empty roster:
    # that would remove every provider during an unrelated storage failure.
    try:
        data = json.loads(switcher.sequence_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ConfigError("Cannot read the cswap roster; T3 providers were left untouched.") from None
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, dict) or any(
        not isinstance(record, dict) or not isinstance(record.get("email"), str)
        or not record.get("email") for record in accounts.values()
    ):
        raise ConfigError("Invalid cswap roster; T3 providers were left untouched.")
    return accounts


def sync_once(switcher, *, force: bool = False, now: float | None = None, prepare=None) -> dict:
    settings = load_t3_settings(switcher.backup_dir)
    if not force and not settings.auto_sync:
        return {"skipped": "disabled"}
    now = time.time() if now is None else now
    switcher.backup_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(switcher.backup_dir / "t3_sync.lock"):
        state = read_state(switcher.backup_dir)
        last = state.get("lastAttemptAt", 0)
        if not force and isinstance(last, (int, float)) and now - last < settings.interval_seconds:
            return {"skipped": "interval"}
        state["lastAttemptAt"] = now
        atomic_write_json(switcher.backup_dir / STATE_FILE, state)
        try:
            path = target_path(switcher.backup_dir)
            _read_target(path)  # validate before touching credentials
            if os.environ.get("CLAUDE_CONFIG_DIR"):
                raise ConfigError("Run automatic T3 sync outside a CLAUDE_CONFIG_DIR session shell.")
            records = _roster(switcher)
            current = switcher._get_current_account()
            owner = owner_key(switcher.backup_dir)
            launcher = SessionManager(switcher, quiet=True)
            prepare = prepare or (
                lambda num: launcher.setup_session(num, share=True, refresh=False)[0]
            )
            desired: dict[str, dict | None] = {}
            names: dict[str, str] = {}
            errors = []
            active_email = None
            for num, record in records.items():
                key = instance_id(switcher.backup_dir, record)
                names[key] = provider_name(record["email"])
                if switcher._account_kind(num) == "api_key":
                    errors.append(f"Account {num}: API-key profiles are not supported yet")
                    continue
                if current == (record["email"], record.get("organizationUuid") or ""):
                    active_email = record["email"]
                    desired[key] = None
                    desired[f"cswap_{owner}_active"] = _entry(owner, provider_name(record["email"], active=True), "")
                    continue
                try:
                    directory = prepare(num)
                    desired[key] = _entry(owner, names[key], str(directory))
                except ClaudeSwitchError as error:
                    desired[key] = None
                    errors.append(f"Account {num}: {error}")
            if _roster(switcher) != records or switcher._get_current_account() != current:
                raise ConfigError("Accounts changed while profiles were prepared; the next pass will retry.")
            report = reconcile_settings(path, owner, desired, names=names)
            report.update({"settingsPath": str(path), "errors": errors, "activeEmail": active_email})
            atomic_write_json(switcher.backup_dir / STATE_FILE,
                              {**report, "lastAttemptAt": now, "lastSyncedAt": now})
            return report
        except Exception as error:
            atomic_write_json(switcher.backup_dir / STATE_FILE,
                              {**state, "lastAttemptAt": now, "lastError": str(error)})
            raise


def run_auto_sync_quietly(switcher, *, force: bool = False) -> dict | None:
    try:
        if not load_t3_settings(switcher.backup_dir).auto_sync:
            return None
        return sync_once(switcher, force=force)
    except Exception as error:
        _logger.warning("T3 account sync failed: %s", error)
        return None


def status(switcher) -> dict:
    return {**read_state(switcher.backup_dir),
            "enabled": load_t3_settings(switcher.backup_dir).auto_sync,
            "settingsPath": str(target_path(switcher.backup_dir))}


def describe_report(report: dict) -> str:
    if report.get("skipped"):
        return f"T3 sync skipped: {report['skipped']}"
    text = (f"T3: {len(report['added'])} added, {len(report['updated'])} updated, "
            f"{len(report['removed'])} removed · {report['managedCount']} managed providers")
    if report.get("errors"):
        text += "\n" + "\n".join(report["errors"])
    return text
