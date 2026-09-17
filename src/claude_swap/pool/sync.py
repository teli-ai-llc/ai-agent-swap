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
from claude_swap.exceptions import ClaudeSwitchError, PoolAuthError, PoolError
from claude_swap.oauth import credential_fingerprint
from claude_swap.pool.blob import blob_fingerprint, blob_from_local, blob_to_local, blob_version
from claude_swap.pool.client import PoolAccountRow, PoolClient
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
        self._dead_at_start: set[str] = set()

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
        self._dead_at_start = {
            num for num, record, _id, _o in self._pooled_slots(data)
            if self.switcher._slot_token_dead(num, record.get("email", ""))
        }
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

    # -- pull -----------------------------------------------------------------
    def _local_version(self, num: str, record: dict, data: dict) -> int:
        texts = self._local_texts(num, record, data)
        if texts is None:
            return -1
        try:
            return int((json.loads(texts[0]).get("claudeAiOauth") or {}).get("expiresAt") or 0)
        except (ValueError, TypeError, AttributeError):
            return -1

    def _slot_for_row(self, data: dict, row: PoolAccountRow) -> str | None:
        for num, record in (data.get("accounts") or {}).items():
            if record.get("poolAccountId") == row.id:
                return str(num)
        return self.switcher._find_account_slot(data, row.email, row.organization_uuid)

    def _pull(self, report: PassReport, state: dict, data: dict) -> None:
        try:
            rows = self.client.list_accounts(self.session, since=state.get("pulledAt"))
        except PoolAuthError:
            raise
        except PoolError as e:
            report.errors.append(f"pull failed: {e}")
            return
        watermark = state.get("pulledAt")
        for row in rows:  # ascending updated_at
            try:
                self._apply_row(report, state, row)
            except PoolError as e:
                # spec failure table: a bad row (missing/invalid blob) is not
                # retried -- skip it and let the watermark advance past it, so
                # it doesn't hold up every row behind it.
                report.errors.append(f"{row.email}: {e}")
                watermark = row.updated_at
                continue
            except (ClaudeSwitchError, OSError) as e:
                # a local write failure is this machine's problem, not the
                # row's -- keep the watermark before it so the next pass retries.
                report.errors.append(f"{row.email}: {e}")
                break  # keep the watermark before this row; retry next pass
            watermark = row.updated_at
        state["pulledAt"] = watermark

    def _apply_row(self, report: PassReport, state: dict, row: PoolAccountRow) -> None:
        data = self.switcher._get_sequence_data() or {}
        num = self._slot_for_row(data, row)
        mine = row.owner_user_id == self.session.user_id

        if row.status == "withdrawn":
            if num is None:
                return
            if mine:
                self.switcher.set_slot_pool_info(num, None, False)
            else:
                self.switcher.remove_account(num, assume_yes=True)
                state["pushed"].pop(num, None)
                report.removed.append(num)
            return

        if row.credential is None:
            raise PoolError("row carries no usable credential")
        creds_text, config_text = blob_to_local(row.credential)

        if num is None:
            num = self._land_new_row(row, mine, creds_text, config_text)
            state["pushed"][num] = credential_fingerprint(creds_text) or ""
            report.added.append(num)
            return

        record = data["accounts"][num]
        if row.credential_version <= self._local_version(num, record, data):
            if self.switcher.slot_pool_info(num) == (None, False):
                self.switcher.set_slot_pool_info(num, row.id, mine)
            return

        email = record.get("email", "")
        self.switcher._write_account_credentials(num, email, creds_text)
        self.switcher._write_account_config(num, email, config_text)
        self.switcher._usage_store.clear_dead_token(
            [num], {num: (email, record.get("organizationUuid") or "")}
        )
        if self.switcher.slot_pool_info(num) == (None, False):
            self.switcher.set_slot_pool_info(num, row.id, mine)
        state["pushed"][num] = credential_fingerprint(creds_text) or ""
        state["flagged"].pop(num, None)
        report.pulled.append(num)

        if str(data.get("activeAccountNumber")) == num:
            current = self.switcher._get_current_account()
            if current and current[0] == email and current[1] == (record.get("organizationUuid") or ""):
                self.switcher.switch_to(num, json_output=True, force=True)

    def _land_new_row(self, row: PoolAccountRow, mine: bool,
                      creds_text: str, config_text: str) -> str:
        from claude_swap.models import get_timestamp
        from claude_swap.rules import apply_rule

        num = str(self.switcher._get_next_account_number())
        self.switcher._write_account_credentials(num, row.email, creds_text)
        self.switcher._write_account_config(num, row.email, config_text)
        data = self.switcher._get_sequence_data() or {"accounts": {}, "sequence": [], "activeAccountNumber": None}
        record = {
            "email": row.email, "uuid": row.account_uuid,
            "organizationUuid": row.organization_uuid, "organizationName": row.organization_name,
            "added": get_timestamp(), "poolAccountId": row.id, "poolOwned": mine,
        }
        if not mine:
            apply_rule(record, priority=2, swap_limit=row.share_swap_limit,
                       hard_limit=row.share_hard_limit if row.share_hard_limit is not None else 100.0)
        data.setdefault("accounts", {})[num] = record
        if int(num) not in data.setdefault("sequence", []):
            data["sequence"].append(int(num))
            data["sequence"].sort()
        data["lastUpdated"] = get_timestamp()
        self.switcher._write_json(self.switcher.sequence_file, data)
        return num

    # -- status -------------------------------------------------------------------
    def _status(self, report: PassReport, state: dict) -> None:
        data = self.switcher._get_sequence_data() or {}
        attention: list[dict] = []
        for num, record, account_id, owned in self._pooled_slots(data):
            email = record.get("email", "")
            if num in report.pulled and num in state["flagged"]:
                state["flagged"].pop(num, None)
            if num in report.pulled and num in self._dead_at_start:
                report.healed.append(num)
            dead = self.switcher._slot_token_dead(num, email)
            fp = credential_fingerprint(self.switcher._read_account_credentials(num, email)) or ""
            if dead and state["flagged"].get(num) != fp:
                try:
                    self.client.set_status(self.session, account_id, "needs_relogin", self.machine_id)
                except PoolAuthError:
                    raise
                except PoolError as e:
                    report.errors.append(f"slot {num}: could not report dead lineage: {e}")
                else:
                    state["flagged"][num] = fp
                    report.flagged.append(num)
            if owned:
                try:
                    row = self.client.get_account(self.session, account_id)
                except PoolAuthError:
                    raise
                except PoolError:
                    row = None
                if row is not None and row.status == "needs_relogin":
                    attention.append({"email": email, "since": row.needs_relogin_since,
                                      "reportedBy": row.needs_relogin_reported_by})
        state["attention"] = attention

    # -- sharing (Task 11) ---------------------------------------------------------
    def publish_slot(self, num: str, *, shared: bool, swap_limit: float | None,
                     hard_limit: float | None) -> PoolAccountRow:
        raise PoolError("publish_slot: implemented in Task 11")

    def withdraw_slot(self, num: str) -> None:
        raise PoolError("withdraw_slot: implemented in Task 11")


def build_sync(switcher, *, transport=None) -> PoolSync | None:
    """A PoolSync for this machine, or None when not logged in or disabled."""
    from claude_swap.pool.session import load_session, machine_id
    from claude_swap.settings import load_pool_settings

    settings = load_pool_settings(switcher.backup_dir)
    if not settings.enabled:
        return None
    session = load_session(switcher.backup_dir)
    if session is None:
        return None
    client = PoolClient(session.url, session.anon_key, transport=transport)
    return PoolSync(switcher, client, session, machine_id=machine_id(switcher.backup_dir))


_last_quiet_warning: dict[str, float] = {}


def run_pass_quietly(switcher, *, force: bool = False) -> PassReport | None:
    """One pass for poll surfaces: never raises, paced by pool.pollIntervalSeconds.

    Returns None when there is no pool on this machine or the pace says wait.
    A skipped pass (unreachable, refused session) is logged at most once per
    ten minutes per reason so a long outage does not flood the log.
    """
    from claude_swap.settings import load_pool_settings

    try:
        sync = build_sync(switcher)
        if sync is None:
            return None
        if not force:
            state = load_state(switcher.backup_dir)
            last = state.get("lastPassAt")
            interval = load_pool_settings(switcher.backup_dir).poll_interval_seconds
            if isinstance(last, (int, float)) and time.time() - last < interval:
                return None
        report = sync.run_pass()
    except Exception as e:  # pragma: no cover - the safety net the spec demands
        _logger.warning("pool pass failed: %s: %s", type(e).__name__, e)
        return None
    if report.skipped:
        now = time.time()
        if now - _last_quiet_warning.get(report.skipped, 0) > 600:
            _logger.warning("pool: %s", report.skipped)
            _last_quiet_warning[report.skipped] = now
    for err in report.errors:
        _logger.warning("pool: %s", err)
    return report
