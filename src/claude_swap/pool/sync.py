"""One sync pass: push local rotations, pull newer rows, report dead lineages.

Every surface (``cswap sync``, the auto engine, the TUI, the menu bar) calls
``run_pass_quietly``; ``PoolSync.run_pass`` is the unit under test.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from claude_swap.credentials import looks_like_api_key
from claude_swap.exceptions import PoolAuthError, PoolError
from claude_swap.oauth import credential_fingerprint
from claude_swap.pool.blob import blob_fingerprint, blob_from_local, blob_version
from claude_swap.pool.client import PoolClient
from claude_swap.pool.session import PoolSession, save_session
from claude_swap.settings import atomic_write_json

POOL_SCHEMA_VERSION = 1
POOL_STATE_FILENAME = "pool_state.json"

_logger = logging.getLogger("claude-swap")


@dataclass
class PassReport:
    pushed: list[str] = field(default_factory=list)
    pulled: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)
    healed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: str | None = None


def _state_path(backup_root: Path) -> Path:
    return backup_root / POOL_STATE_FILENAME


def _empty_state() -> dict:
    return {"schemaVersion": 1, "pulledAt": None, "lastPassAt": None,
            "pushed": {}, "flagged": {}, "attention": []}


def load_state(backup_root: Path) -> dict:
    try:
        raw = json.loads(_state_path(backup_root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_state()
    except (OSError, ValueError) as e:
        _logger.warning("Could not read %s (%s); starting pool state fresh", _state_path(backup_root), e)
        return _empty_state()
    if not isinstance(raw, dict):
        return _empty_state()
    state = _empty_state()
    for key in ("pulledAt", "lastPassAt"):
        if key in raw:
            state[key] = raw[key]
    for key in ("pushed", "flagged"):
        if isinstance(raw.get(key), dict):
            state[key] = {str(k): v for k, v in raw[key].items() if isinstance(v, str)}
    if isinstance(raw.get("attention"), list):
        state["attention"] = [a for a in raw["attention"] if isinstance(a, dict)]
    return state


def save_state(backup_root: Path, state: dict) -> None:
    atomic_write_json(_state_path(backup_root), state)


class PoolSync:
    def __init__(self, switcher, client: PoolClient, session: PoolSession, *,
                 machine_id: str, clock: Callable[[], float] = time.time):
        self.switcher = switcher
        self.client = client
        self.session = session
        self.machine_id = machine_id
        self.clock = clock

    # -- pass ---------------------------------------------------------------------
    def run_pass(self) -> PassReport:
        report = PassReport()
        state = load_state(self.switcher.backup_dir)
        try:
            fresh = self.client.ensure_fresh(self.session)
            if fresh is not self.session:
                self.session = fresh
                save_session(self.switcher.backup_dir, fresh)
            version = self.client.schema_version(self.session)
            if version != POOL_SCHEMA_VERSION:
                report.skipped = (
                    f"pool schema is v{version}, this cswap speaks v{POOL_SCHEMA_VERSION}; "
                    + ("upgrade cswap" if version > POOL_SCHEMA_VERSION else "the admin must migrate the pool")
                )
                return report
        except PoolAuthError as e:
            report.skipped = f"pool session refused ({e}); run: cswap pool login"
            return report
        except PoolError as e:
            report.skipped = f"pool unreachable: {e}"
            return report

        data = self.switcher._get_sequence_data() or {}
        try:
            self._push(report, state, data)
            self._pull(report, state, data)
            self._status(report, state)
        except PoolAuthError as e:
            report.skipped = f"pool session refused ({e}); run: cswap pool login"
        finally:
            state["lastPassAt"] = self.clock()
            save_state(self.switcher.backup_dir, state)
        return report

    # -- push ---------------------------------------------------------------------
    def _pooled_slots(self, data: dict) -> list[tuple[str, dict, str, bool]]:
        out = []
        for num, record in (data.get("accounts") or {}).items():
            account_id, owned = self.switcher.slot_pool_info(str(num))
            if account_id:
                out.append((str(num), record, account_id, owned))
        return out

    def _local_texts(self, num: str, record: dict, data: dict) -> tuple[str, str] | None:
        """Freshest local (creds, config) for a slot: live bytes when active."""
        email = record.get("email", "")
        config_text = self.switcher._read_account_config(num, email)
        creds_text = None
        if str(data.get("activeAccountNumber")) == num:
            current = self.switcher._get_current_account()
            if current and current[0] == email and current[1] == (record.get("organizationUuid") or ""):
                active = self.switcher._read_active_credentials()
                # A degraded read (Keychain locked, plaintext fallback served) may be
                # a superseded generation -- never push it, fall through to the backup
                # instead, same as `_refuse_degraded_capture` elsewhere in the repo.
                if active.value and not active.degraded and not looks_like_api_key(active.value):
                    creds_text = active.value
        if not creds_text:
            # `_read_account_credentials` returns "" rather than raising when
            # the backup is missing or unreadable; a falsy result here just
            # means "no usable local bytes", handled by the check below.
            creds_text = self.switcher._read_account_credentials(num, email)
        if not creds_text or not config_text:
            return None
        return creds_text, config_text

    def _push(self, report: PassReport, state: dict, data: dict) -> None:
        for num, record, account_id, _owned in self._pooled_slots(data):
            texts = self._local_texts(num, record, data)
            if texts is None:
                continue
            creds_text, config_text = texts
            fp = credential_fingerprint(creds_text)
            if not fp or state["pushed"].get(num) == fp:
                continue
            try:
                blob = blob_from_local(creds_text, config_text)
                landed = self.client.push_credential(
                    self.session, account_id, blob, blob_version(blob), blob_fingerprint(blob), self.machine_id,
                )
            except PoolAuthError:
                raise
            except PoolError as e:
                report.errors.append(f"slot {num}: push failed: {e}")
                continue
            state["pushed"][num] = fp
            if landed:
                report.pushed.append(num)

    # -- pull and status: Tasks 8 and 9 -----------------------------------------
    def _pull(self, report: PassReport, state: dict, data: dict) -> None:
        return None

    def _status(self, report: PassReport, state: dict) -> None:
        return None
