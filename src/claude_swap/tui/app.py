"""The claude-swap Textual application.

Owns the snapshot poll loop and every mutating action (switch/add/remove),
so the dashboard, the auto view, and the command palette all drive the same
code paths. Blocking switcher work always runs in thread workers — the UI
loop never touches file locks, keychain subprocesses, or the network.
"""

from __future__ import annotations

import os

import time
from dataclasses import replace
from functools import partial

from textual.app import App
from textual.binding import Binding
from textual.reactive import reactive
from textual.worker import WorkerState

from claude_swap import printer
from claude_swap.models import AccountsSnapshot
from claude_swap.snapshot_source import account_identity
from claude_swap.settings import load_pool_settings, load_settings, load_ui_settings, set_setting
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.tui.autoview import AutoScreen
from claude_swap.tui.dashboard import DashboardScreen
from claude_swap.tui.data import ActionResult, SnapshotSource, format_duration, run_action
from claude_swap.tui.modals import (
    AddTokenModal,
    ConfirmModal,
    OutputModal,
    PoolLoginForm,
    PoolLoginModal,
    PoolShareForm,
    PoolShareModal,
    RuleForm,
    RuleModal,
    TokenForm,
)
from claude_swap.tui.theme import CSWAP_DARK, CSWAP_LIGHT


MAX_ACCOUNT_CARDS_ENV = "CSWAP_MAX_ACCOUNT_CARDS"


def _max_account_cards_from_env(environ=None) -> int | None:
    """Parse ``CSWAP_MAX_ACCOUNT_CARDS``; anything but a positive int is unset."""
    raw = (os.environ if environ is None else environ).get(MAX_ACCOUNT_CARDS_ENV, "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


class CswapApp(App):
    """claude-swap interactive dashboard."""

    TITLE = "claude-swap"
    CSS_PATH = "cswap.tcss"
    # No command palette: actions live in the dashboard's nested menu, in
    # their own context — not in a global searchable list.
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+t", "toggle_theme", "Theme")]

    POLL_INTERVAL_S = 3.0  # matches the old watch view's recapture cadence
    # Snapshot age stays hidden while polling is healthy (age never exceeds
    # ~POLL_INTERVAL_S); past this it surfaces as a staleness alarm. 60s also
    # keeps format_duration in whole minutes, so the note never ticks per
    # second.
    SNAPSHOT_AGE_NOTE_S = 60.0

    snapshot: reactive[AccountsSnapshot | None] = reactive(None)
    refresh_status: reactive[str] = reactive("")
    busy: reactive[bool] = reactive(False)

    def __init__(
        self,
        switcher: ClaudeAccountSwitcher,
        *,
        detected: str | None = None,
    ) -> None:
        super().__init__()
        self.switcher = switcher
        self._detected = detected  # terminal background sensed pre-driver, or None
        self.source = SnapshotSource(switcher)
        self._store_only = False
        self._full_next = False
        self._normal_refreshing = False
        self._store_refreshing = False
        self._normal_started_at: float | None = None
        self._refresh_generation = 0
        self._applied_generation = 0
        self._last_refresh_error = ""
        # The auto-switch threshold, drawn as a tick on the status strip's
        # bars everywhere. Missing/invalid settings fall back to the default.
        try:
            self.threshold_pct: float | None = load_settings(
                switcher.backup_dir
            ).threshold
        except Exception:
            self.threshold_pct = None
        try:
            ui = load_ui_settings(switcher.backup_dir)
            self._theme_name = ui.theme
            # How the dashboard draws accounts other than the active login
            # ("full" cards or one-line "mini" rows).
            self.inactive_cards = ui.inactive_cards
        except Exception:
            self._theme_name = "auto"
            self.inactive_cards = "full"
        # Cap on full account cards the dashboard shows before its accounts
        # panel scrolls. Set by the menu bar panel (panel.py), whose popover
        # has a fixed height; unset (the terminal) means "show them all".
        self.max_account_cards = _max_account_cards_from_env()

    def on_mount(self) -> None:
        self.register_theme(CSWAP_DARK)
        self.register_theme(CSWAP_LIGHT)
        resolved = self._resolved_theme()
        # We own the theme; $TEXTUAL_THEME is intentionally not honoured.
        self.theme = f"cswap-{resolved}"
        printer.set_theme(resolved)
        self.push_screen(DashboardScreen())
        self.set_interval(self.POLL_INTERVAL_S, self._tick)
        self.set_interval(1.0, self._update_refresh_status)
        self._tick()

    # -- snapshot poll loop ---------------------------------------------------

    def _tick(self) -> None:
        """Start one eligible refresh lane.

        Normal mode prefers the fetch-enabled lane. When that lane is blocked,
        the poll tick may still observe another process's store update through
        one store-only lane. Auto mode already has an engine fetching, so it
        launches only store-only snapshots.
        """
        if self._store_only:
            self._start_store_refresh()
        elif not self._normal_refreshing:
            full, self._full_next = self._full_next, False
            self._start_normal_refresh(full=full)
        else:
            self._start_store_refresh()

    def _start_normal_refresh(self, *, full: bool) -> None:
        if self._normal_refreshing:
            return
        self._normal_refreshing = True
        self._normal_started_at = time.time()
        generation = self._next_refresh_generation()
        self._update_refresh_status()
        self.run_worker(
            partial(self._refresh_blocking, generation, "normal", full, False),
            thread=True,
            group="refresh-normal",
            exit_on_error=False,
            name="snapshot-refresh",
        )

    def _start_store_refresh(self) -> None:
        if self._store_refreshing:
            return
        self._store_refreshing = True
        generation = self._next_refresh_generation()
        self._update_refresh_status()
        self.run_worker(
            partial(self._refresh_blocking, generation, "store", False, True),
            thread=True,
            group="refresh-store",
            exit_on_error=False,
            name="snapshot-store-refresh",
        )

    def _next_refresh_generation(self) -> int:
        self._refresh_generation += 1
        return self._refresh_generation

    def _refresh_blocking(
        self, generation: int, lane: str, full: bool, store_only: bool
    ) -> None:
        if lane == "normal" and not store_only:
            from claude_swap.pool.sync import run_pass_quietly
            run_pass_quietly(self.switcher)
        snap = self.source.take(full=full, store_only=store_only)
        self.call_from_thread(self._apply_snapshot, generation, lane, snap)

    def _apply_snapshot(
        self, generation: int, lane: str, snap: AccountsSnapshot
    ) -> None:
        if lane == "normal":
            self._normal_refreshing = False
            self._normal_started_at = None
        else:
            self._store_refreshing = False
        self._last_refresh_error = ""
        if generation >= self._applied_generation:
            self._applied_generation = generation
            self.snapshot = snap
        elif self.snapshot is not None:
            # A later-started store repaint owns account metadata, but the
            # older worker may have completed a genuinely newer provider fetch.
            # SnapshotSource has already rejected per-account regressions, so
            # merge its canonical usage rows without restoring stale metadata.
            current = self.snapshot
            incoming = {acc.number: acc for acc in snap.accounts}
            accounts = tuple(
                replace(acc, usage=other.usage)
                if (
                    (other := incoming.get(acc.number)) is not None
                    and account_identity(acc) == account_identity(other)
                )
                else acc
                for acc in current.accounts
            )
            self.snapshot = replace(
                current,
                accounts=accounts,
                taken_at=max(current.taken_at, snap.taken_at),
            )
        self._update_refresh_status()

    def _update_refresh_status(self) -> None:
        parts: list[str] = []
        now = time.time()
        if self.snapshot is not None:
            age = max(0.0, now - self.snapshot.taken_at)
            if age >= self.SNAPSHOT_AGE_NOTE_S:
                parts.append(f"snapshot {format_duration(age)} ago")
        if self._normal_refreshing and self._normal_started_at is not None:
            elapsed = now - self._normal_started_at
            if elapsed >= self.POLL_INTERVAL_S:
                parts.append(f"refreshing {format_duration(elapsed)}")
        from claude_swap.pool.sync import attention_lines
        try:  # a status note must never take the app down
            notes = attention_lines(self.switcher.backup_dir, now)
        except Exception:
            notes = []
        if notes:
            parts.insert(0, notes[0])
        self.refresh_status = " · ".join(parts)

    def request_refresh(self, *, full: bool = False) -> None:
        if full:
            self._full_next = True
        self._tick()

    def set_store_only(self, value: bool) -> None:
        """Auto screen: the engine fetches, the poller only reads the store."""
        self._store_only = value
        self.request_refresh()

    def on_worker_state_changed(self, event) -> None:
        if event.state is not WorkerState.ERROR:
            return
        if event.worker.group in {"refresh-normal", "refresh-store"}:
            if event.worker.group == "refresh-normal":
                self._normal_refreshing = False
                self._normal_started_at = None
            else:
                self._store_refreshing = False
            self._update_refresh_status()
            msg = str(event.worker.error)
            if msg != self._last_refresh_error:
                self._last_refresh_error = msg
                lane = "store refresh" if event.worker.group == "refresh-store" else "refresh"
                self.notify(
                    f"{lane.capitalize()} failed: {msg}", severity="warning", timeout=6
                )
        elif event.worker.group == "action":
            self.busy = False
            self.notify(f"Action failed: {event.worker.error}", severity="error")
        elif event.worker.group == "engine":
            self.notify(
                f"Auto-switch engine stopped: {event.worker.error}",
                severity="error",
            )

    # -- mutating actions (single-flight, captured, off-thread) ---------------

    def _start_action(self, label: str, fn, *, show_output: bool = False) -> None:
        if self.busy:
            self.notify("Another action is still running", severity="warning")
            return
        self.busy = True
        self.run_worker(
            partial(self._action_blocking, label, fn, show_output),
            thread=True,
            group="action",
            exit_on_error=False,
            name=label,
        )

    def _action_blocking(self, label: str, fn, show_output: bool) -> None:
        result = run_action(fn)
        self.call_from_thread(self._action_done, label, result, show_output)

    def _action_done(
        self, label: str, result: ActionResult, show_output: bool
    ) -> None:
        self.busy = False
        self.request_refresh()
        if not result.ok:
            self.push_screen(OutputModal(f"{label} — failed", result.output))
            return
        payload = result.payload or {}
        if "switched" in payload:
            if payload.get("switched"):
                to = payload.get("to") or {}
                target = to.get("email") or f"account {to.get('number')}"
                self.notify(f"Switched to {target}", title="Switch")
            else:
                reason = str(payload.get("reason") or "no switch performed")
                self.notify(reason, title="No switch", severity="warning")
            return
        if show_output and result.output.strip():
            self.push_screen(OutputModal(label, result.output))
        elif result.first_line:
            self.notify(result.first_line)

    # -- account operations ----------------------------------------------------

    def do_switch(self, number: str) -> None:
        self._start_action(
            f"Switch to account {number}",
            partial(self.switcher.switch_to, number, json_output=True),
        )

    def action_switch_best(self) -> None:
        self._start_action(
            "Switch (best)",
            partial(self.switcher.switch, strategy="best", json_output=True),
        )

    def do_toggle_disabled(self, number: str) -> None:
        """Hold the account out of auto-rotation, or return it — reads its
        current state from the live snapshot to pick the direction."""
        snap = self.snapshot
        acc = next(
            (a for a in (snap.accounts if snap else ()) if a.number == number), None
        )
        if acc is None:
            return
        target = not acc.disabled
        verb = "Disable" if target else "Enable"
        self._start_action(
            f"{verb} account {number}",
            partial(self.switcher.set_account_disabled, number, target),
        )

    def confirm_remove(self, number: str, email: str) -> None:
        self.push_screen(
            ConfirmModal(
                f"Remove account {number} ({email})?\n\n"
                "Its stored credentials and config backup are deleted.",
                title="Remove account",
                yes_label="Remove",
            ),
            partial(self._on_remove_confirm, number),
        )

    def _on_remove_confirm(self, number: str, confirmed: bool | None) -> None:
        if confirmed:
            self._start_action(
                f"Remove account {number}",
                partial(self.switcher.remove_account, number, assume_yes=True),
            )

    def open_rule_editor(self, number: str) -> None:
        """Edit one account's swap limit / hard limit / priority (rules.py)."""
        snap = self.snapshot
        acc = next(
            (a for a in (snap.accounts if snap else ()) if a.number == number), None
        )
        if acc is None:
            return
        label = f"{acc.alias} ({acc.email})" if acc.alias else acc.email
        self.push_screen(
            RuleModal(number, label, acc.rule, self.threshold_pct),
            partial(self._on_rule_form, number),
        )

    def _on_rule_form(self, number: str, form: RuleForm | None) -> None:
        if form is None:
            return
        if form.reset:
            fn = partial(self.switcher.set_account_rule, number, reset=True)
        else:
            fn = partial(
                self.switcher.set_account_rule,
                number,
                swap_limit=form.swap_limit,
                hard_limit=form.hard_limit,
                priority=form.priority,
            )
        self._start_action(f"Rules for account {number}", fn)

    # -- pool -------------------------------------------------------------------
    # Every ``claude_swap.pool.*`` import below lives inside its method so a
    # machine with no pool never imports the pool's network/client code on
    # the hot path (menu render, snapshot poll).

    def pool_logged_in(self) -> bool:
        from claude_swap.pool.cli import pool_logged_in as _pool_logged_in

        return _pool_logged_in(self.switcher)

    def action_pool_login(self) -> None:
        settings = load_pool_settings(self.switcher.backup_dir)
        self.push_screen(
            PoolLoginModal(settings.url, settings.anon_key), self._on_pool_login_form
        )

    def _on_pool_login_form(self, form: PoolLoginForm | None) -> None:
        if form is None:
            return

        def do_login() -> None:
            from claude_swap.pool.cli import describe_report, login_pool

            result = login_pool(
                self.switcher, form.url, form.anon_key, form.email, form.code
            )
            if result.signed_up:
                print(f"Welcome to the pool: created your membership for {result.email}")
            print(f"Logged in as {result.email} ({result.role})")
            if result.guard_applied:
                print(
                    "Remote Control disabled in Claude Code's settings.json ("
                    + ", ".join(result.guard_applied)
                    + ") so sessions on borrowed logins never surface in a "
                    "teammate's claude.ai"
                )
            print(f"Sync: {describe_report(result.report)}")
            if result.suggest_strikes:
                print(
                    "Tip: cswap config set autoswitch.deadTokenStrikes 2 "
                    "tolerates the pool's sync lag"
                )

        self._start_action("Pool login", do_login, show_output=True)

    def action_pool_status(self) -> None:
        def do_status() -> None:
            from claude_swap.pool.cli import status_lines

            for line in status_lines(self.switcher):
                print(line)

        self._start_action("Pool status", do_status, show_output=True)

    def open_pool_share(self, number: str) -> None:
        """Look up the slot's current pool row, then push the share modal.

        The lookup (``get_account``) is a real network call with a 10s
        timeout, so it runs on a worker thread — never on the UI loop — and
        the modal is pushed only once the prefill is known.
        """
        snap = self.snapshot
        acc = next(
            (a for a in (snap.accounts if snap else ()) if a.number == number), None
        )
        label = (
            (f"{acc.alias} ({acc.email})" if acc.alias else acc.email)
            if acc is not None
            else number
        )
        self.run_worker(
            partial(self._pool_share_prefill_blocking, number, label),
            thread=True,
            group="pool-prefill",
            exit_on_error=False,
            name="pool share prefill",
        )

    def _pool_share_prefill_blocking(self, number: str, label: str) -> None:
        """Worker body: the current pool row when this slot is already
        published, or ``None`` (fresh publish defaults) otherwise or on
        error; then hands off to the UI thread to push the modal."""
        from claude_swap.pool.sync import build_sync

        current: PoolShareForm | None = None
        try:
            sync = build_sync(self.switcher)
            info = getattr(self.switcher, "slot_pool_info", None)
            if sync is not None and info is not None:
                account_id, _owned = info(number)
                if account_id:
                    row = sync.client.get_account(sync.session, account_id)
                    if row is not None:
                        current = PoolShareForm(
                            shared=row.shared,
                            swap_limit=row.share_swap_limit,
                            hard_limit=row.share_hard_limit,
                        )
        except Exception:
            current = None
        self.call_from_thread(self._open_pool_share_modal, number, label, current)

    def _open_pool_share_modal(
        self, number: str, label: str, current: PoolShareForm | None
    ) -> None:
        self.push_screen(
            PoolShareModal(number, label, current),
            partial(self._on_pool_share_form, number),
        )

    def _on_pool_share_form(self, number: str, form: PoolShareForm | None) -> None:
        if form is None:
            return

        def do_publish() -> None:
            from claude_swap.exceptions import PoolError
            from claude_swap.pool.sync import build_sync

            sync = build_sync(self.switcher)
            if sync is None:
                raise PoolError("not logged in to a pool")
            row = sync.publish_slot(
                number,
                shared=form.shared,
                swap_limit=form.swap_limit,
                hard_limit=form.hard_limit,
            )
            print(
                f"{'Shared' if row.shared else 'Published (private)'} {row.email}"
                + (f"  swap {row.share_swap_limit:g}" if row.share_swap_limit else "")
                + (f"  hard {row.share_hard_limit:g}" if row.share_hard_limit else "")
            )

        self._start_action(f"Publish account {number}", do_publish, show_output=True)

    def confirm_pool_withdraw(self, number: str, email: str) -> None:
        self.push_screen(
            ConfirmModal(
                f"Withdraw {email} from the pool? "
                "Borrowers lose it on their next sync.",
                title="Withdraw account",
                yes_label="Withdraw",
            ),
            partial(self._on_pool_withdraw_confirm, number),
        )

    def _on_pool_withdraw_confirm(self, number: str, confirmed: bool | None) -> None:
        if not confirmed:
            return

        def do_withdraw() -> None:
            from claude_swap.exceptions import PoolError
            from claude_swap.pool.sync import build_sync

            sync = build_sync(self.switcher)
            if sync is None:
                raise PoolError("not logged in to a pool")
            sync.withdraw_slot(number)
            print(f"Withdrawn account {number} from the pool")

        self._start_action(f"Withdraw account {number}", do_withdraw)

    def action_pool_sync_now(self) -> None:
        def do_sync() -> None:
            from claude_swap.pool.cli import describe_report, pool_logged_in
            from claude_swap.pool.sync import run_pass_quietly

            if not pool_logged_in(self.switcher):
                print("skipped: not logged in")
                return
            report = run_pass_quietly(self.switcher, force=True)
            if report is None:
                print("skipped: pool disabled or the pass failed (see the log)")
                return
            print(describe_report(report))

        self._start_action("Sync now", do_sync)

    def action_pool_defaults(self) -> None:
        def do_defaults() -> None:
            from claude_swap.pool.cli import apply_pooled_defaults

            if apply_pooled_defaults(self.switcher):
                print("autoswitch.deadTokenStrikes set to 2")
            else:
                print("already at 2 or higher")

        self._start_action("Pooled-machine defaults", do_defaults)

    def action_pool_logout(self, keep: bool) -> None:
        if keep:
            self._do_pool_logout(keep=True)
            return
        count = self._borrowed_pool_account_count()
        self.push_screen(
            ConfirmModal(
                f"Remove {count} borrowed account(s) from this machine and log out?",
                title="Log out of the pool",
                yes_label="Log out",
            ),
            self._on_pool_logout_confirm,
        )

    def _on_pool_logout_confirm(self, confirmed: bool | None) -> None:
        if confirmed:
            self._do_pool_logout(keep=False)

    def _borrowed_pool_account_count(self) -> int:
        """Slots on this machine linked to the pool but owned by someone
        else — what a "remove borrowed accounts" logout would drop."""
        snap = self.snapshot
        info = getattr(self.switcher, "slot_pool_info", None)
        if info is None or snap is None:
            return 0
        count = 0
        for acc in snap.accounts:
            account_id, owned = info(acc.number)
            if account_id and not owned:
                count += 1
        return count

    def _do_pool_logout(self, *, keep: bool) -> None:
        def do_logout() -> None:
            from claude_swap.pool.cli import logout_pool

            result = logout_pool(self.switcher, keep=keep)
            for num, email, reason in result.kept:
                print(f"kept account {num} ({email}) locally: {reason}")
            if keep:
                suffix = "; borrowed accounts kept"
            elif result.kept:
                suffix = f"; {len(result.kept)} borrowed account(s) kept locally (see above)"
            else:
                suffix = "; borrowed accounts removed"
            print(f"Logged out of the pool{suffix}")

        self._start_action("Pool logout", do_logout, show_output=True)

    def action_add_current(self) -> None:
        self.push_screen(
            ConfirmModal(
                "Back up the current Claude Code login as a managed account?\n\n"
                "If this account is already managed, its stored credentials "
                "are refreshed in place.",
                title="Add account",
                yes_label="Add",
            ),
            self._on_add_confirm,
        )

    def _on_add_confirm(self, confirmed: bool | None) -> None:
        if confirmed:
            self._start_action(
                "Add current login",
                partial(self.switcher.add_account),
                show_output=True,
            )

    def action_add_token(self) -> None:
        self.push_screen(AddTokenModal(), self._on_token_form)

    def _on_token_form(self, form: TokenForm | None) -> None:
        if form is None:
            return
        run = partial(
            self._start_action,
            "Add account from token",
            partial(
                self.switcher.add_account_from_token,
                token=form.token,
                email=form.email,
                slot=form.slot,
                assume_yes=True,
            ),
            show_output=True,
        )
        occupant = self._slot_occupant(form.slot)
        if occupant is not None:
            self.push_screen(
                ConfirmModal(
                    f"Slot {form.slot} is occupied by {occupant}. Overwrite?",
                    title="Overwrite slot",
                    yes_label="Overwrite",
                ),
                lambda confirmed: run() if confirmed else None,
            )
        else:
            run()

    def _slot_occupant(self, slot: int | None) -> str | None:
        if slot is None or self.snapshot is None:
            return None
        for acc in self.snapshot.accounts:
            if acc.number == str(slot):
                return acc.email
        return None

    # -- navigation -------------------------------------------------------------

    def action_refresh_full(self) -> None:
        self.request_refresh(full=True)
        self.notify("Refreshing usage…", timeout=2)

    def action_open_auto(self) -> None:
        if isinstance(self.screen, AutoScreen):
            return
        self.push_screen(AutoScreen())

    # -- theme --------------------------------------------------------------

    def _resolved_theme(self) -> str:
        """Concrete 'dark'/'light' for the current setting; auto → detected → dark."""
        if self._theme_name == "auto":
            return self._detected or "dark"
        return self._theme_name

    def apply_theme(self, name: str) -> None:
        """Switch the live theme: TUI + captured printer output + persistence.

        ``name`` is the setting; ``auto`` resolves against the pre-driver
        detection (never re-probes mid-session)."""
        self._theme_name = name
        resolved = self._resolved_theme()
        self.theme = f"cswap-{resolved}"
        printer.set_theme(resolved)
        try:
            set_setting(self.switcher.backup_dir, "ui.theme", name)
        except Exception as exc:  # persistence is best-effort; never crash the UI
            self.notify(f"Could not save theme: {exc}", severity="warning")

    def action_toggle_theme(self) -> None:
        order = ("dark", "light", "auto")
        nxt = order[(order.index(self._theme_name) + 1) % len(order)]
        self.apply_theme(nxt)
        self.notify(f"Theme: {nxt}")
