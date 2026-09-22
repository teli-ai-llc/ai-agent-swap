"""Live auto-switch screen: the real engine, visualized.

Runs :class:`AutoSwitchEngine` in a thread worker and renders its typed
events. Opens in **dry-run** unless the user went live here before and that
choice was saved (``ui.autoLive``) — going live is an explicit, confirmed
action, and the confirmation is what persists it. The threshold (``t``) and
the per-model limit (``m``, e.g. Fable's weekly window) are saved to
settings.json too, so what this view shows is what ``cswap auto`` runs with.
The engine's own state file semantics (shared cooldown, quarantine list,
state lock) make it safe to run alongside an external ``cswap auto``.

The active account's full card sits on top (same widget as the dashboard's
panel, with the threshold tick); this screen adds the engine badge, the
ranked switch candidates, and the decision log. While it is up, the app's
snapshot poller runs store-only: the engine is the only fetcher.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, RichLog, Static

from claude_swap.autoswitch import (
    AutoSwitchEngine,
    AutoSwitchEvent,
    binding_pct,
    pct_label,
)
from claude_swap.exceptions import ConfigError
from claude_swap.models import AccountsSnapshot
from claude_swap.rules import resolve_rule
from claude_swap.settings import (
    SETTING_SPECS,
    format_setting_value,
    load_settings,
    load_ui_settings,
    parse_model_names,
    set_setting,
    unset_setting,
)
from claude_swap.tui import data
from claude_swap.tui.modals import ConfirmModal
from claude_swap.tui.theme import Palette
from claude_swap.tui.widgets import AccountsPanel

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

_EVENT_ROLES = {
    "switch": "accent",
    "error": "sev_warn",
    "account-quarantined": "sev_warn",
    "all-exhausted": "sev_crit",
}
_QUIET_KINDS = {"poll", "no-switch", "sleep", "account-unquarantined"}


def event_text(event: AutoSwitchEvent, *, palette: Palette = Palette.DARK) -> Text:
    """Log line for one engine event, styled like the CLI's human renderer."""
    role = _EVENT_ROLES.get(event.kind)
    if role is not None:
        style = getattr(palette, role)
    else:
        style = palette.muted if event.kind in _QUIET_KINDS else palette.foreground
    text = Text()
    text.append(f"{data.clock_stamp()}  ", style=palette.muted)
    text.append(event.human(), style=style)
    return text


class AutoScreen(Screen):
    BINDINGS = [
        Binding("l", "toggle_live", "Go live / dry-run"),
        Binding("t", "adjust_threshold", "Threshold"),
        Binding("m", "cycle_model", "Model limit"),
        Binding("left", "threshold_step(-1)", "-1%"),
        Binding("right", "threshold_step(1)", "+1%"),
        Binding("enter", "adjust_done", "Done"),
        Binding("escape,q", "back", "Back"),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._engine: AutoSwitchEngine | None = None
        self._settings = None
        # Threshold adjustment (t, then arrows; enter/t/esc to finish).
        # Finishing saves the value to settings.json. ``_configured_threshold``
        # tracks the file value: while the live one differs the summary says
        # "(session)" and exit reverts the bar tick to it — only relevant
        # when saving failed. ``_entry_threshold`` is the value when adjust
        # mode was entered (wake/log only on a net change).
        self._adjusting = False
        self._configured_threshold: float | None = None
        self._entry_threshold: float | None = None

    def compose(self) -> ComposeResult:
        yield AccountsPanel(show_inactive=False, id="auto-active-panel")
        with Vertical(id="auto-top"):
            with Horizontal(id="auto-title-row"):
                yield Static(" DRY-RUN ", id="mode-badge", classes="dry")
                yield Static("", id="auto-summary")
            yield Static("", id="candidates")
        yield RichLog(id="event-log", highlight=False, markup=False, wrap=True)
        yield Footer()

    # -- lifecycle ----------------------------------------------------------

    def on_mount(self) -> None:
        self.app.set_store_only(True)
        self._settings = load_settings(self.app.switcher.backup_dir)
        # The bar tick everywhere reads app.threshold_pct, loaded once at app
        # startup — sync it to the fresh file value so bars and engine agree,
        # and remember that value: unmount restores it (only the session
        # adjustment reverts, not this correction).
        self._configured_threshold = self._settings.threshold
        self.app.threshold_pct = self._settings.threshold
        self._update_summary()
        self.watch(self.app, "snapshot", self._on_snapshot)
        self.watch(self.app, "theme", self._on_theme_change)
        # Dry-run unless going live was confirmed here before and saved: a
        # reopen must not quietly drop the protection the user turned on.
        self._start_engine(dry_run=not self._saved_auto_live())

    def on_unmount(self) -> None:
        if self._engine is not None:
            self._engine.stop()
        # A session threshold must not outlive the engine it steered: unpin
        # the poll planner and put the bar tick back on the file value.
        self.app.switcher.clear_poll_policy_inputs()
        if self._configured_threshold is not None:
            self.app.threshold_pct = self._configured_threshold
        self.app.set_store_only(False)

    def _on_theme_change(self, _theme: str) -> None:
        self._update_summary()
        self._update_badge()
        snap = self.app.snapshot
        if snap is not None:
            self._on_snapshot(snap)

    def action_back(self) -> None:
        if self._adjusting:
            self._end_adjust()
            return
        self.app.pop_screen()

    # -- threshold adjust mode ------------------------------------------------

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action in ("threshold_step", "adjust_done") and not self._adjusting:
            return False  # hidden and inert until adjust mode is armed
        return True

    def action_adjust_threshold(self) -> None:
        if self._adjusting:
            self._end_adjust()
            return
        self._adjusting = True
        self._entry_threshold = self._settings.threshold
        self._update_summary()
        self.refresh_bindings()

    def action_adjust_done(self) -> None:
        if self._adjusting:
            self._end_adjust()

    def action_threshold_step(self, delta: float) -> None:
        if not self._adjusting:
            return
        spec = SETTING_SPECS["autoswitch.threshold"]
        value = min(spec.hi, max(spec.lo, self._settings.threshold + delta))
        self._set_threshold(value)

    def _end_adjust(self) -> None:
        self._adjusting = False
        self.refresh_bindings()
        if self._settings.threshold == self._entry_threshold:
            self._update_summary()
            return  # no net change: nothing to announce, no tick to force
        if self._engine is not None:
            self._engine.wake()  # show a decision at the new value now
        value = self._settings.threshold
        saved = self._persist("autoswitch.threshold", format_setting_value(value))
        if saved:
            self._configured_threshold = value
        self._update_summary()
        self._log_note(
            f"— threshold set to {pct_label(value)}% "
            f"({'saved' if saved else 'this session only'}) —"
        )

    def _set_threshold(self, value: float) -> None:
        if value == self._settings.threshold:
            return
        self._settings = replace(self._settings, threshold=value)
        if self._engine is not None:
            self._engine.apply_threshold(value)
        self.app.threshold_pct = value
        self.query_one("#auto-active-panel", AccountsPanel).refresh()
        self._update_summary()

    def _update_summary(self) -> None:
        palette = Palette.from_theme(self.app.current_theme)
        text = Text()
        text.append("auto-switch · ")
        text.append(
            f"threshold {pct_label(self._settings.threshold)}%",
            style=palette.accent if self._adjusting else "",
        )
        if self._settings.threshold != self._configured_threshold:
            text.append(" (session)", style=palette.muted)
        text.append(f" · poll every {self._settings.interval_seconds:.0f}s")
        models = parse_model_names(self._settings.model)
        text.append(" · model limit ")
        text.append(
            ", ".join(models) if models else "off",
            style=palette.accent if models else "",
        )
        if self._adjusting:
            text.append("   ← → adjust · enter done", style=palette.muted)
        self.query_one("#auto-summary", Static).update(text)

    # -- persistence & per-model limit --------------------------------------

    def _saved_auto_live(self) -> bool:
        try:
            return load_ui_settings(self.app.switcher.backup_dir).auto_live
        except Exception:
            return False

    def _persist(self, dotted_key: str, value: str | None) -> bool:
        """Write one settings.json key (``None`` unsets it). A failed write
        is reported and leaves the session value in force; never raises."""
        backup_dir = self.app.switcher.backup_dir
        try:
            if value is None:
                unset_setting(backup_dir, dotted_key)
            else:
                set_setting(backup_dir, dotted_key, value)
        except (ConfigError, OSError) as exc:
            self.app.notify(
                f"Could not save {dotted_key}: {exc}", severity="warning"
            )
            return False
        return True

    def _log_note(self, message: str) -> None:
        self.query_one("#event-log", RichLog).write(
            Text(message, style=Palette.from_theme(self.app.current_theme).muted)
        )

    def _scoped_names(self) -> list[str]:
        """Per-model window names the accounts report (e.g. ``["Fable"]``),
        first spelling wins, in snapshot order."""
        snap = self.app.snapshot
        seen: dict[str, str] = {}
        for acc in (snap.accounts if snap else ()):
            last_good = acc.usage.last_good
            scoped = last_good.get("scoped") if isinstance(last_good, dict) else None
            for window in scoped or []:
                name = window.get("name") if isinstance(window, dict) else None
                if isinstance(name, str) and name and name.lower() not in seen:
                    seen[name.lower()] = name
        return list(seen.values())

    def action_cycle_model(self) -> None:
        """Cycle ``autoswitch.model``: off → each per-model window the
        accounts report (e.g. Fable) → all → off. Saved to settings.json,
        and the engine restarts on the new axes (fixed at construction)."""
        options: list[str | None] = [None, *self._scoped_names(), "all"]
        current = (self._settings.model or "").lower()
        index = next(
            (i for i, option in enumerate(options) if (option or "").lower() == current),
            -1,  # a hand-edited list ("Fable,Opus"): the next step is "off"
        )
        chosen = options[(index + 1) % len(options)]
        self._settings = replace(self._settings, model=chosen)
        self._persist("autoswitch.model", chosen)
        if self._engine is not None:
            self._restart_engine(dry_run=self._engine.dry_run)
        self._update_summary()
        snap = self.app.snapshot
        if snap is not None:
            self._on_snapshot(snap)  # re-rank candidates on the new axes
        self._log_note(f"— model limit: {chosen or 'off'} —")

    # -- engine -------------------------------------------------------------

    def _start_engine(self, *, dry_run: bool) -> None:
        from claude_swap.pool.sync import run_pass_quietly

        engine = AutoSwitchEngine(
            self.app.switcher,
            self._settings,
            self._emit_from_thread,
            dry_run=dry_run,
            pre_tick=lambda: run_pass_quietly(self.app.switcher),
        )
        self._engine = engine
        self.run_worker(
            engine.run_loop,
            thread=True,
            group="engine",
            exit_on_error=False,
            name=f"auto-engine-{'dry' if dry_run else 'live'}",
        )
        self._update_badge()
        log = self.query_one("#event-log", RichLog)
        mode = "DRY-RUN (watching only)" if dry_run else "LIVE (will switch accounts)"
        log.write(
            Text(
                f"— engine started: {mode} —",
                style=Palette.from_theme(self.app.current_theme).muted,
            )
        )

    def _emit_from_thread(self, event: AutoSwitchEvent) -> None:
        """Engine ``on_event`` callback — runs on the worker thread."""
        try:
            self.app.call_from_thread(self._on_engine_event, event)
        except Exception:
            # App/screen tearing down mid-tick; the event has nowhere to go.
            pass

    def _on_engine_event(self, event: AutoSwitchEvent) -> None:
        if not self.is_attached:
            return
        palette = Palette.from_theme(self.app.current_theme)
        self.query_one("#event-log", RichLog).write(event_text(event, palette=palette))
        if event.kind == "switch":
            self.app.request_refresh()

    def action_toggle_live(self) -> None:
        if self._engine is None:
            return
        if self._engine.dry_run:
            self.app.push_screen(
                ConfirmModal(
                    "Go live? claude-swap will switch your active account "
                    "automatically when the threshold is reached.\n\n"
                    "(Same behavior as running `cswap auto` in a terminal. "
                    "This view stays live the next time it opens.)",
                    title="Go live",
                    yes_label="Go live",
                ),
                self._on_live_confirm,
            )
        else:
            self._restart_engine(dry_run=True)
            self._persist("ui.autoLive", "false")

    def _on_live_confirm(self, confirmed: bool | None) -> None:
        if confirmed:
            self._restart_engine(dry_run=False)
            self._persist("ui.autoLive", "true")

    def _restart_engine(self, *, dry_run: bool) -> None:
        if self._engine is not None:
            self._engine.stop()
        self._start_engine(dry_run=dry_run)

    def _update_badge(self) -> None:
        badge = self.query_one("#mode-badge", Static)
        if self._engine is not None and not self._engine.dry_run:
            badge.update(" LIVE ")
            badge.set_classes("live")
        else:
            badge.update(" DRY-RUN ")
            badge.set_classes("dry")

    # -- candidates -----------------------------------------------------------

    def _on_snapshot(self, snap: AccountsSnapshot | None) -> None:
        if snap is None:
            return
        self.query_one("#candidates", Static).update(
            self._candidates_text(snap, active_number=snap.active_number)
        )

    def _candidates_text(
        self, snap: AccountsSnapshot, active_number: str | None
    ) -> Text:
        """Switch targets ranked by remaining headroom (best first)."""
        # Same window set as the engine (autoswitch.model included), so the
        # displayed ranking can never disagree with the account it picks.
        palette = Palette.from_theme(self.app.current_theme)
        models = parse_model_names(self._settings.model) if self._settings else ()
        # (priority, pct used) — the same order the engine ranks in: a
        # more-preferred slot first, usage within a tier, capped/unknown last.
        ranked: list[tuple[tuple[int, float], str]] = []
        lines: dict[str, Text] = {}
        for acc in snap.accounts:
            if acc.number == active_number or not acc.switchable:
                continue
            pct = binding_pct(acc.usage.last_good, models)
            # A pace-bound hard limit is the week's progress right now, the
            # same number the engine decides with this tick.
            rule = resolve_rule(acc.rule, acc.usage.last_good, time.time())
            entry = Text()
            entry.append(f"\n  {acc.number:>2}  ", style=palette.foreground)
            entry.append(acc.email, style=palette.foreground)
            if acc.usage.sentinel is not None:
                entry.append(
                    f"  {data.sentinel_label(acc.usage.sentinel)}", style=palette.muted
                )
                ranked.append(((rule.priority, 998.0), acc.number))
            elif pct is None:
                entry.append("  usage unknown", style=palette.muted)
                ranked.append(((rule.priority, 999.0), acc.number))
            elif rule.hard_limit < 100.0 and pct >= rule.hard_limit:
                # Only the slot's OWN cap is called a hard limit; an account
                # at the provider's 100% reads as plain usage, like before.
                entry.append(
                    f"  {pct:3.0f}% used · at hard limit {rule.hard_limit:.10g}%",
                    style=palette.sev_crit,
                )
                ranked.append(((rule.priority, 997.0), acc.number))
            else:
                entry.append(f"  {pct:3.0f}% used", style=palette.severity(pct))
                ranked.append(((rule.priority, pct), acc.number))
            if rule.priority != 1:
                entry.append(f"  p{rule.priority}", style=palette.muted)
            lines[acc.number] = entry

        text = Text()
        text.append("Next best", style=palette.muted)
        if not ranked:
            text.append("\n  no other switchable accounts", style=palette.muted)
            return text
        for _key, number in sorted(ranked):
            text.append(lines[number])
        return text
