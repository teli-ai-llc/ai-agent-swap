"""Shared render widgets: usage bars, account cards, and the accounts panel.

``bar_cells``/``usage_bar`` are custom renderers rather than Textual's
``ProgressBar`` because the design needs three things the stock widget
doesn't do: a severity color ramp, an optional threshold tick mark (the
auto-switch trigger line), a time marker (how much of the window has
elapsed: fill past it means usage is ahead of an even pace), and
stale-measurement dimming.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import ListItem, Static

from claude_swap import pace, recommend
from claude_swap.json_output import USAGE_API_KEY
from claude_swap.models import AccountSnapshot
from claude_swap.rules import AccountRule, resolve_rule, week_progress
from claude_swap.switcher import ERROR_NOTES
from claude_swap.usage_store import STALE_OK_S
from claude_swap.tui import data
from claude_swap.tui.theme import Palette

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

_BAR_FILLED = "━"
_BAR_HALF = "╸"
_BAR_EMPTY = "─"
_BAR_TICK = "┃"
_BAR_TIME = "│"


def bar_cells(
    pct: float | None,
    width: int,
    *,
    stale: bool = False,
    threshold: float | None = None,
    hard_limit: float | None = None,
    elapsed: float | None = None,
    palette: Palette = Palette.DARK,
) -> Text:
    """Just the bar glyphs: severity-colored fill, track, optional ticks —
    the swap threshold (warning color) and, when the account has one below
    100, its hard limit (critical color) — and, when ``elapsed`` (0–100, the
    share of the window's period already gone) is known, a time marker: an
    even pace would have the fill end exactly there."""
    text = Text()
    if pct is None:
        text.append(_BAR_EMPTY * width, style=palette.track)
        return text
    frac = min(max(pct, 0.0), 100.0) / 100.0
    cells = frac * width
    full = int(cells)
    half = (cells - full) >= 0.5 and full < width
    tick_at: int | None = None
    if threshold is not None:
        tick_at = min(width - 1, max(0, round(threshold / 100.0 * width)))
    hard_at: int | None = None
    if hard_limit is not None and hard_limit < 100.0:
        hard_at = min(width - 1, max(0, round(hard_limit / 100.0 * width)))
    time_at: int | None = None
    if elapsed is not None:
        time_at = min(width - 1, max(0, int(elapsed / 100.0 * width)))
    color = palette.severity(pct)
    fill_style = f"{color} dim" if stale else color
    for i in range(width):
        if hard_at is not None and i == hard_at:
            text.append(_BAR_TICK, style=palette.sev_crit)
        elif tick_at is not None and i == tick_at:
            text.append(_BAR_TICK, style=palette.sev_warn)
        elif time_at is not None and i == time_at:
            text.append(_BAR_TIME, style=f"bold {palette.foreground}")
        elif i < full:
            text.append(_BAR_FILLED, style=fill_style)
        elif i == full and half:
            text.append(_BAR_HALF, style=fill_style)
        else:
            text.append(_BAR_EMPTY, style=palette.track)
    return text


def usage_bar(
    label: str,
    pct: float | None,
    suffix: str | None,
    width: int,
    *,
    stale: bool = False,
    threshold: float | None = None,
    hard_limit: float | None = None,
    elapsed: float | None = None,
    palette: Palette = Palette.DARK,
) -> Text:
    """One full bar line: ``5h ━━━━╸──│──┃──  47%  resets in 2h 13m · 20:39``."""
    text = Text()
    text.append(f"{label} ", style=palette.muted)
    text.append(bar_cells(
        pct, width, stale=stale, threshold=threshold, hard_limit=hard_limit,
        elapsed=elapsed, palette=palette,
    ))
    if pct is None:
        text.append("  usage unknown", style=palette.muted)
    else:
        color = palette.severity(pct)
        text.append(f" {pct:3.0f}%", style=f"{color} dim" if stale else color)
    if suffix:
        text.append(f"  {suffix}", style=palette.muted)
    return text


def _reset_parts(window: dict, now: float) -> tuple[str | None, str | None]:
    """Countdown suffix and its clock-extended variant for one window.

    ``("resets in 2h 13m", "resets in 2h 13m · 20:39")`` — the second form
    is what a row shows when it has the width for it. Equal when no clock is
    known. A window projected past its reset (recommend.project_resets) says
    so: its 0% is an expectation until the next fetch confirms it.
    """
    reset = data.reset_text(window, now)
    if window.get("rolled"):
        reset = f"just reset · next {reset}" if reset else "just reset"
    if not reset:
        return None, None
    clock = data.reset_clock(window, now)
    return reset, f"{reset} · {clock}" if clock else reset


def _pace_suffix(window: dict, fetched_at: float | None) -> str:
    """"(ahead of pace)" when a weekly window is meaningfully ahead, else ""."""
    result = pace.compute_pace(window, fetched_at=fetched_at)
    return "(ahead of pace)" if result and result.ahead else ""


def usage_rows(
    last_good: dict | None, now: float, fetched_at: float | None = None
) -> list[tuple[str, float, str, str]]:
    """(label, pct, suffix, suffix_full) rows mirroring the CLI's
    ``_format_usage_lines``.

    ``suffix_full`` extends the reset countdown with the absolute clock time
    (``resets 2h 13m · 20:39``) for rows that have room; otherwise it equals
    ``suffix``. Only windows the account actually has produce a row — an
    annual plan without a 7-day window simply has no 7d line. Order matches
    the CLI: spend, 5h, 7d, then per-model scoped windows (e.g. "Fable"),
    the latter marked ``(!)`` at/over their limit. The weekly (7d) and scoped
    rows also carry a "(ahead of pace)" marker when meaningfully ahead of the
    week's expected usage (issue #125) — never the 5h row.
    """
    if not isinstance(last_good, dict):
        return []
    last_good, _ = recommend.project_resets(last_good, now)
    rows: list[tuple[str, float, str, str]] = []
    spend = last_good.get("spend")
    if spend:
        amounts = f"${spend['used']:,.2f} / ${spend['limit']:,.2f}"
        reset, reset_full = _reset_parts(spend, now)
        suffix = f"{reset}  {amounts}" if reset else amounts
        suffix_full = f"{reset_full}  {amounts}" if reset_full else amounts
        rows.append(("$$", float(spend["pct"]), suffix, suffix_full))
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = last_good.get(key)
        if window:
            reset, reset_full = _reset_parts(window, now)
            suffix, suffix_full = reset or "", reset_full or ""
            if key == "seven_day":
                marker = _pace_suffix(window, fetched_at)
                if marker:
                    suffix = f"{suffix}  {marker}" if suffix else marker
                    suffix_full = f"{suffix_full}  {marker}" if suffix_full else marker
            rows.append((label, float(window["pct"]), suffix, suffix_full))
    for window in last_good.get("scoped") or []:
        pct = float(window["pct"])
        suffix, suffix_full = _reset_parts(window, now)
        suffix, suffix_full = suffix or "", suffix_full or ""
        if pct >= 100:
            suffix = f"{suffix}  (!)" if suffix else "(!)"
            suffix_full = f"{suffix_full}  (!)" if suffix_full else "(!)"
        else:
            marker = _pace_suffix(window, fetched_at)
            if marker:
                suffix = f"{suffix}  {marker}" if suffix else marker
                suffix_full = f"{suffix_full}  {marker}" if suffix_full else marker
        rows.append((window["name"], pct, suffix, suffix_full))
    return rows


def window_elapsed(window: dict | None, period_s: float, now: float) -> float | None:
    """How much of a window's period has gone by, 0–100, or None if unknown."""
    if not isinstance(window, dict):
        return None
    ts = recommend.parse_reset_ts(window.get("resets_at"))
    if ts is None:
        return None
    remaining = (ts - now) % period_s if ts > now else 0.0
    return max(0.0, min(100.0, (1.0 - remaining / period_s) * 100.0))


def usage_row_elapsed(last_good: dict | None, now: float) -> list[float | None]:
    """:func:`window_elapsed` for each row :func:`usage_rows` returns, in the
    same order (spend has no period, so no marker)."""
    if not isinstance(last_good, dict):
        return []
    last_good, _ = recommend.project_resets(last_good, now)
    out: list[float | None] = []
    if last_good.get("spend"):
        out.append(None)
    for key, period in (("five_hour", recommend.FIVE_HOUR_S), ("seven_day", recommend.WEEK_S)):
        if last_good.get(key):
            out.append(window_elapsed(last_good[key], period, now))
    for window in last_good.get("scoped") or []:
        out.append(window_elapsed(window, recommend.WEEK_S, now))
    return out


def best_pick(app, snap, now: float) -> tuple[str | None, dict]:
    """``(number, outlooks)`` for the ★ best pick under the app's configured
    strategy and models (recommend.py), or ``(None, ...)`` when nothing in
    rotation is usable."""
    if snap is None:
        return None, {}
    return recommend.snapshot_pick(
        snap.accounts,
        getattr(app, "routing_models", ()),
        now,
        getattr(app, "routing_strategy", "best"),
        getattr(app, "threshold_pct", None),
    )


def pick_reason(number: str | None, outlooks: dict, now: float) -> str | None:
    """The ★ line's text for the picked account."""
    if number is None or number not in outlooks:
        return None
    return recommend.reason(outlooks[number], now)


def _over_hard_limit(acc: AccountSnapshot, now: float) -> bool:
    """Whether the account's 5h/7d usage has reached its own hard limit
    (a pace-bound one resolved against the week at ``now``)."""
    rule = resolve_rule(acc.rule, acc.usage.last_good, now)
    if rule.hard_limit >= 100.0:
        return False
    pcts = [
        p
        for p in (
            data.window_pct(acc.usage.last_good, "five_hour"),
            data.window_pct(acc.usage.last_good, "seven_day"),
        )
        if p is not None
    ]
    return bool(pcts) and max(pcts) >= rule.hard_limit


def rule_chips(rule: AccountRule, threshold: float | None, progress: float | None = None) -> str:
    """The card/mini header's rule summary (``p2 · hard 50%``, ``hard pace
    (61%)``), or ``""``. ``progress`` is the week's progress for a pace-bound
    rule (``rules.week_progress``), when known."""
    return rule.summary(threshold, progress)


def _progress_for(acc: AccountSnapshot, now: float) -> float | None:
    tracks_week = acc.rule.hard_pace or acc.rule.hard_taper
    return week_progress(acc.usage.last_good, now) if tracks_week else None


def account_card_text(
    acc: AccountSnapshot,
    width: int,
    *,
    threshold: float | None = None,
    now: float | None = None,
    palette: Palette = Palette.DARK,
    pick: str | None = None,
) -> Text:
    """The full account card: header line + per-window bar rows. The bars
    carry the account's own swap threshold tick and, if it has one, a
    critical-colored hard-limit tick (rules.py), plus a time marker. ``pick``
    is set (the recommendation's reason, recommend.py) when this account is
    the best pick: its number gets a ★ in place of padding, so the card keeps
    its height, which the menu bar panel's sizing relies on."""
    now = now if now is not None else time.time()

    text = Text()
    if pick is not None:
        # In the number's own slot ("★ 2 " is as wide as " 2  "), so the
        # header never grows and the card keeps its height.
        text.append("★", style=f"bold {palette.sev_ok}")
        text.append(f"{acc.number:>2} ", style=f"bold {palette.sev_ok}")
    else:
        text.append(f"{acc.number:>2}  ", style=f"bold {palette.foreground}")
    if acc.alias:
        text.append(acc.alias, style=f"bold {palette.accent}")
        text.append(f" ({acc.email})", style=palette.foreground)
    else:
        text.append(acc.email, style=palette.foreground)
    text.append(f"  [{acc.display_tag}]", style=palette.muted)
    if acc.is_active:
        text.append("   ● active", style=f"bold {palette.accent}")
    if acc.disabled:
        text.append("   (disabled)", style=palette.muted)

    # The rule chips, the over-limit marker and the age note are one group.
    # It sits at the end of the header line when the whole line fits; when
    # it does not, the group moves to its own line, right-aligned, instead
    # of the terminal wrapping it mid-phrase ("⛔ over" / "hard limit").
    meta = Text()
    chips = rule_chips(acc.rule, threshold, _progress_for(acc, now))
    if chips:
        meta.append(chips, style=palette.muted)
    if _over_hard_limit(acc, now):
        if meta:
            meta.append("   ")
        meta.append("⛔ over hard limit", style=f"bold {palette.sev_crit}")
    age = data.format_age(acc.usage.age_s)
    if age:
        if meta:
            meta.append("   ")
        meta.append(age, style=palette.muted)
    if meta:
        if text.cell_len + 3 + meta.cell_len <= width:
            text.append("   ")
            text.append_text(meta)
        else:
            text.append("\n")
            text.append(" " * max(4, width - meta.cell_len))
            text.append_text(meta)

    sentinel = acc.usage.sentinel
    if sentinel is not None:
        text.append("\n    ")
        style = palette.muted if sentinel == USAGE_API_KEY else palette.sev_warn
        marker = "·" if sentinel == USAGE_API_KEY else "⚠"
        text.append(f"{marker} {data.sentinel_label(sentinel)}", style=style)
        # Same supplementary line `cswap list` prints: the last good
        # measurement behind the sentinel (API-key accounts have no quota to
        # have "seen").
        if sentinel != USAGE_API_KEY:
            last_seen = data.last_seen_note(acc.usage)
            if last_seen is not None:
                text.append("\n    ")
                text.append(f"└ {last_seen}", style=palette.muted)
        return text

    rows = usage_rows(acc.usage.last_good, now, acc.usage.fetched_at)
    if not rows:
        text.append("\n    ")
        text.append("usage unavailable", style=palette.muted)
        if acc.usage.last_error:
            # Same wording as the CLI detail line: error KINDS with a
            # friendly note render it, so both surfaces describe the state
            # identically.
            note = ERROR_NOTES.get(acc.usage.last_error, acc.usage.last_error)
            text.append(f" · {note}", style=palette.muted)
        return text

    stale = acc.usage.age_s is not None and acc.usage.age_s > STALE_OK_S
    # This account's own swap limit (defaulting to the global threshold) is
    # where its tick goes; a hard limit below 100 gets the second tick — for
    # a pace-bound one, where the week stands right now.
    tick = acc.rule.swap_for(threshold) if threshold is not None else None
    resolved = resolve_rule(acc.rule, acc.usage.last_good, now)
    hard_limit = resolved.hard_limit if resolved.hard_limit < 100.0 else None
    elapsed = usage_row_elapsed(acc.usage.last_good, now)
    label_width = max(len(label) for label, _pct, _suffix, _full in rows)
    bar_width = max(12, min(30, width - 42 - label_width))
    # everything on a row except the suffix: indent, label, bar, " NNN%", gap
    row_overhead = 4 + label_width + 1 + bar_width + 5 + 2
    for (label, pct, suffix, suffix_full), row_elapsed in zip(rows, elapsed):
        # per-row: show the absolute clock only where it fits, so a long
        # spend row degrading doesn't cost the 5h/7d rows their clocks
        if suffix_full != suffix and row_overhead + len(suffix_full) <= width:
            suffix = suffix_full
        text.append("\n    ")
        text.append(
            usage_bar(
                f"{label:<{label_width}}",
                pct,
                suffix or None,
                bar_width,
                stale=stale,
                threshold=tick,
                hard_limit=hard_limit,
                elapsed=row_elapsed,
                palette=palette,
            )
        )
    return text


def mini_account_text(
    acc: AccountSnapshot, now: float, *, palette: Palette = Palette.DARK,
    pick: bool = False,
) -> Text:
    """One minimized line for an inactive account.

    ``2  work@acme.dev [personal]   5h 92% (2h 13m) · 7d 63% (3d 4h)`` —
    severity-colored pcts, each with its reset countdown, and a maxed
    per-model window as ``Fable (!)``. Sentinel states show their label
    instead. ``pick`` marks the recommended account with a ★.
    """
    text = Text(no_wrap=True, overflow="ellipsis")
    if pick:
        text.append(f"★{acc.number:>2} ", style=f"bold {palette.sev_ok}")
    else:
        text.append(f"{acc.number:>2}  ", style=f"bold {palette.muted}")
    if acc.alias:
        text.append(acc.alias, style=f"bold {palette.accent}")
        text.append(f" ({acc.email})", style=palette.foreground)
    else:
        text.append(acc.email, style=palette.foreground)
    text.append(f"  [{acc.display_tag}]", style=palette.muted)
    if acc.disabled:
        text.append("  (disabled)", style=palette.muted)
    chips = rule_chips(acc.rule, None, _progress_for(acc, now))
    if chips:
        text.append(f"  {chips}", style=palette.muted)
    if _over_hard_limit(acc, now):
        text.append("  ⛔ hard limit", style=f"bold {palette.sev_crit}")
    text.append("   ")

    sentinel = acc.usage.sentinel
    if sentinel is not None:
        style = palette.muted if sentinel == USAGE_API_KEY else palette.sev_warn
        text.append(data.sentinel_label(sentinel), style=style)
        return text

    last_good, _ = recommend.project_resets(acc.usage.last_good, now)
    fetched_at = acc.usage.fetched_at
    stale = acc.usage.age_s is not None and acc.usage.age_s > STALE_OK_S
    parts = 0
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = last_good.get(key) if isinstance(last_good, dict) else None
        if not window:
            continue
        pct = float(window["pct"])
        if parts:
            text.append(" · ", style=palette.track)
        color = palette.severity(pct)
        text.append(f"{label} ", style=palette.muted)
        text.append(f"{pct:.0f}%", style=f"{color} dim" if stale else color)
        countdown = data.reset_countdown(window, now)
        if countdown:
            text.append(f" ({countdown})", style=palette.muted)
        if key == "seven_day" and pct < 100:
            result = pace.compute_pace(window, fetched_at=fetched_at)
            if result and result.ahead:
                text.append(" (ahead)", style=palette.sev_warn)
        parts += 1
    maxed = [
        w["name"]
        for w in (last_good.get("scoped") or [] if isinstance(last_good, dict) else [])
        if float(w["pct"]) >= 100
    ]
    for name in maxed:
        if parts:
            text.append(" · ", style=palette.track)
        text.append(f"{name} (!)", style=palette.sev_crit)
        parts += 1
    if not parts:
        text.append("usage unknown", style=palette.muted)
    return text


class AccountsPanel(Static):
    """Static account overview, in slot order: every account as a full card
    (bars, reset times, per-model rows) with the active one marked — or, with
    ``ui.inactiveCards = mini``, the inactive ones as one-line minis. The
    dashboard's — and with ``show_inactive=False`` the auto screen's (active
    account only) — always-visible monitor."""

    def __init__(self, *, show_inactive: bool = True, id: str | None = None) -> None:
        super().__init__(id=id)
        self._show_inactive = show_inactive

    def on_mount(self) -> None:
        self.watch(self.app, "snapshot", lambda _snap: self.refresh(layout=True))
        self.watch(self.app, "theme", lambda _t: self.refresh(layout=True))

    def render(self) -> Text:
        app: "CswapApp" = self.app  # type: ignore[assignment]
        palette = Palette.from_theme(app.current_theme)
        snap = app.snapshot
        if snap is None:
            return Text("loading…", style=palette.muted)
        if not snap.accounts:
            return Text(
                "No managed accounts yet.\n"
                "Use the menu below: Add account — from your current "
                "Claude Code login, or from a setup-token / API key.",
                style=palette.muted,
            )
        now = time.time()
        width = (self.size.width or 80) - 2
        blocks: list[Text] = []
        minis = getattr(app, "inactive_cards", "full") == "mini"
        picked, outlooks = best_pick(app, snap, now)
        for acc in snap.accounts:
            if not acc.is_active and not self._show_inactive:
                continue
            is_pick = acc.number == picked
            if acc.is_active or not minis:
                blocks.append(
                    account_card_text(
                        acc, width, threshold=app.threshold_pct, now=now,
                        palette=palette,
                        pick=pick_reason(picked, outlooks, now) if is_pick else None,
                    )
                )
            else:
                blocks.append(
                    mini_account_text(acc, now, palette=palette, pick=is_pick)
                )
        if not blocks:
            return Text("no active managed login", style=palette.muted)
        text = Text()
        previous_multiline = False
        for i, block in enumerate(blocks):
            multiline = "\n" in block.plain
            if i:
                # breathe around expanded cards
                text.append("\n\n" if (multiline or previous_multiline) else "\n")
            text.append(block)
            previous_multiline = multiline
        return text


class AccountCard(Static):
    """One account rendered full-size (used by the switch screen's list)."""

    def __init__(self, acc: AccountSnapshot, *, threshold: float | None = None) -> None:
        super().__init__()
        self._acc = acc
        self._threshold = threshold

    def set_account(self, acc: AccountSnapshot) -> None:
        self._acc = acc
        self.refresh(layout=True)

    def render(self) -> Text:
        return account_card_text(
            self._acc, self.size.width or 80, threshold=self._threshold,
            palette=Palette.from_theme(self.app.current_theme),
        )


class AccountItem(ListItem):
    """ListView row wrapping an :class:`AccountCard`; remembers its slot."""

    def __init__(self, acc: AccountSnapshot) -> None:
        super().__init__(AccountCard(acc))
        self.number = acc.number
        self.email = acc.email

    def set_account(self, acc: AccountSnapshot) -> None:
        self.number = acc.number
        self.email = acc.email
        self.query_one(AccountCard).set_account(acc)


class MenuItem(ListItem):
    """One menu row: a label plus an action id the screen dispatches on."""

    def __init__(self, label: str, action_id: str, *, muted: bool = False) -> None:
        item = Static(label, markup=False)
        if muted:
            item.add_class("menu-item-muted")
        super().__init__(item)
        self.action_id = action_id
