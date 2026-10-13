"""Time-aware account ranking: the ``smart`` strategy and every "best pick".

Headroom alone treats "40% left, resets in 8 hours" and "40% left, resets in
6 days" as equal. They are not: the first account's quota is gone in 8 hours
whether you use it or not, the second's will still be there tomorrow. The
``smart`` strategy ranks by **urgency** — the weekly room an account has left
divided by the hours until that weekly window resets, i.e. the burn rate (in
% per hour) you would need to use all of it before it expires. The account
whose quota is closest to being wasted wins; fresh weekly quota is saved.

Two guards keep that from picking something useless:

- a landing floor: the account's *binding* headroom (the tightest window,
  5-hour included, on the capped scale that honours an owner's hard limit)
  must be at least :data:`MIN_LANDING_HEADROOM_PCT`, so an account that is
  about to hit its 5-hour limit is never the pick however urgent its week;
- urgency is computed on the capped scale too, so a borrowed account whose
  owner allows 50% offers only what is left under that 50%.

Pure functions over the same usage dicts the engine decides on, so the
engine, ``cswap list``, the TUI, and the menu bar all agree on the pick.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from claude_swap import oauth
from claude_swap.poll_policy import parse_reset_ts
from claude_swap.rules import AccountRule

FIVE_HOUR_S = 5 * 3600.0
WEEK_S = 7 * 86400.0

# Binding headroom an account needs to be worth landing on.
MIN_LANDING_HEADROOM_PCT = 10.0
# Below the switch threshold, `smart` moves only when the target's quota is
# clearly more perishable than the active one's: by this ratio AND by at
# least MIN_URGENCY_GAIN %/h. Burning the target lowers its urgency, so the
# ratio is also what stops a move from immediately undoing itself.
URGENCY_RATIO = 1.5
MIN_URGENCY_GAIN = 0.25
# Hours are floored so a reset minutes away cannot produce an unbounded
# score; a window with no known reset counts as a whole week away.
MIN_HOURS = 1.0


def _window_period_s(key: str) -> float:
    return FIVE_HOUR_S if key == "five_hour" else WEEK_S


def project_resets(usage: dict | None, now: float) -> tuple[dict | None, bool]:
    """``usage`` with every window whose reset has passed rolled forward.

    A cached reading can outlive its own window: a 5-hour window that reset
    20 minutes ago still says 92% until the next fetch. The usage API resets
    a window to 0% at ``resets_at``, so a passed window is projected to 0%
    with its ``resets_at`` moved forward by whole periods. Returns
    ``(projected, any_rolled)``; the input is never mutated.
    """
    if not isinstance(usage, dict):
        return usage, False
    rolled = False

    def roll(window: object, period_s: float) -> object:
        nonlocal rolled
        if not isinstance(window, dict):
            return window
        ts = parse_reset_ts(window.get("resets_at"))
        if ts is None or ts > now:
            return window
        cycles = int((now - ts) // period_s) + 1
        rolled = True
        new_ts = ts + cycles * period_s
        out = {k: v for k, v in window.items() if k not in ("countdown", "clock")}
        out["pct"] = 0.0
        out["resets_at"] = datetime.fromtimestamp(new_ts, tz=timezone.utc).isoformat()
        out["rolled"] = True
        return out

    projected = dict(usage)
    for key in ("five_hour", "seven_day"):
        if key in projected:
            projected[key] = roll(projected[key], _window_period_s(key))
    scoped = projected.get("scoped")
    if isinstance(scoped, list):
        projected["scoped"] = [roll(s, WEEK_S) for s in scoped]
    return projected, rolled


@dataclass(frozen=True)
class Outlook:
    """What one account offers right now, on its owner's capped scale."""

    headroom: float | None  # binding (tightest window) headroom
    weekly_room: float | None  # room on the binding weekly window
    weekly_reset_ts: float | None  # that window's reset (None: unknown)
    urgency: float | None  # weekly_room / hours to weekly reset, %/h
    binding_label: str | None  # "5h", "7d" or a model name
    binding_reset_ts: float | None

    @property
    def usable(self) -> bool:
        return self.headroom is not None and self.headroom >= MIN_LANDING_HEADROOM_PCT


def _weekly_windows(
    usage: dict, models: Sequence[str]
) -> list[tuple[str, float, str | None]]:
    return [w for w in oauth.relevant_windows(usage, models) if w[0] != "5h"]


def outlook(
    usage: dict | str | None,
    rule: AccountRule | None,
    models: Sequence[str],
    now: float,
) -> Outlook:
    """The account's :class:`Outlook` from a usage dict (sentinels → empty)."""
    rule = rule or AccountRule()
    cap = 100.0 - rule.hard_limit
    projected, _ = project_resets(usage if isinstance(usage, dict) else None, now)
    if not isinstance(projected, dict):
        return Outlook(None, None, None, None, None, None)
    windows = oauth.relevant_windows(projected, models)
    if not windows:
        return Outlook(None, None, None, None, None, None)
    label, pct, resets_at = max(windows, key=lambda w: w[1])
    headroom = 100.0 - pct - cap
    weekly = _weekly_windows(projected, models)
    weekly_room = weekly_reset_ts = urgency = None
    if weekly:
        _wl, wpct, wreset = max(weekly, key=lambda w: w[1])
        weekly_room = max(0.0, 100.0 - wpct - cap)
        weekly_reset_ts = parse_reset_ts(wreset)
        hours = (
            (weekly_reset_ts - now) / 3600.0
            if weekly_reset_ts is not None
            else WEEK_S / 3600.0
        )
        urgency = weekly_room / max(MIN_HOURS, hours)
    return Outlook(
        headroom=headroom,
        weekly_room=weekly_room,
        weekly_reset_ts=weekly_reset_ts,
        urgency=urgency,
        binding_label=label,
        binding_reset_ts=parse_reset_ts(resets_at),
    )


def sort_key(o: Outlook, priority: int = 0) -> tuple:
    """Ascending sort key: usable first, then rule priority, then the most
    perishable quota, then the most headroom."""
    return (
        0 if o.usable else 1,
        priority,
        -(o.urgency or 0.0),
        -(o.headroom if o.headroom is not None else -1e9),
    )


def beats(candidate: Outlook, active: Outlook) -> bool:
    """Below the threshold: is ``candidate`` clearly worth moving to?"""
    if not candidate.usable or candidate.urgency is None:
        return False
    current = active.urgency or 0.0
    return (
        candidate.urgency >= current * URGENCY_RATIO
        and candidate.urgency - current >= MIN_URGENCY_GAIN
    )


def rank(
    accounts: Mapping[str, tuple[dict | str | None, AccountRule | None]],
    models: Sequence[str],
    now: float,
    exclude: Iterable[str] = (),
) -> list[tuple[str, Outlook]]:
    """Every account, best pick first. ``accounts`` maps number →
    ``(usage, rule)``. Unreadable accounts sort last."""
    skip = set(exclude)
    scored = [
        (num, outlook(usage, rule, models, now), (rule or AccountRule()).priority)
        for num, (usage, rule) in accounts.items()
        if num not in skip
    ]
    scored.sort(key=lambda t: sort_key(t[1], t[2]))
    return [(num, o) for num, o, _ in scored]


def best(
    accounts: Mapping[str, tuple[dict | str | None, AccountRule | None]],
    models: Sequence[str],
    now: float,
    exclude: Iterable[str] = (),
) -> tuple[str, Outlook] | None:
    """The recommended account, or None when nothing is usable."""
    ranked = rank(accounts, models, now, exclude)
    if ranked and ranked[0][1].usable:
        return ranked[0]
    return None


def format_duration(seconds: float) -> str:
    """``"2d 4h"`` / ``"3h 12m"`` / ``"14m"`` — the countdown format every
    surface uses (matches :func:`oauth.format_reset`)."""
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def reason(o: Outlook, now: float) -> str:
    """One-line why: ``"62% left this week · resets in 1d 4h"``."""
    if o.headroom is None:
        return "usage unknown"
    if not o.usable:
        if o.binding_reset_ts is not None and o.binding_reset_ts > now:
            return (
                f"{o.binding_label} limit · back in "
                f"{format_duration(o.binding_reset_ts - now)}"
            )
        return f"{o.binding_label} limit"
    parts = []
    if o.weekly_room is not None:
        parts.append(f"{o.weekly_room:.0f}% left this week")
    if o.weekly_reset_ts is not None and o.weekly_reset_ts > now:
        parts.append(f"resets in {format_duration(o.weekly_reset_ts - now)}")
    if o.binding_label == "5h" and o.weekly_room is not None and o.headroom < o.weekly_room:
        parts.append(f"5h {o.headroom:.0f}% left")
    return " · ".join(parts) or f"{o.headroom:.0f}% left"
