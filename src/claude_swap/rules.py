"""Per-account switching rules: swap limit, hard limit, priority.

Stored on each slot's record in the sequence file (next to ``alias`` and
``disabled``) as ``swapLimit`` / ``hardLimit`` / ``priority``. Every key is
optional and a missing one means the default, so an account nobody has
edited behaves exactly as before the rules existed.

- ``swap_limit`` — utilization (%) at which the auto engine starts looking
  for a better account while this one is active. Default: the global
  ``autoswitch.threshold``.
- ``hard_limit`` — utilization (%) past which this account may not be used
  at all. It is never a switch target once there, and when it is the active
  account the engine leaves it at once, even below its swap limit — the
  same escape it takes when the provider's own limit lands. Default 100.
- ``hard_pace`` — the hard limit also tracks the week: it is the share of
  the account's 7-day window that has elapsed (60% through the week → 60%),
  so borrowing never pushes the account ahead of pace. Stored as a flag
  beside the number and resolved to a plain ``hard_limit`` at decision time
  (``resolve_rule``): the stricter of the number and the week's progress.
  Right after a weekly reset that is ~0; unknown progress (no ``resets_at``
  yet) leaves only the number. Spelled ``pace`` wherever a hard limit is
  typed. Default off.
- ``priority`` — 1 is most preferred. Candidates are ranked by priority
  first, then by the strategy (most headroom / soonest reset), and while a
  lower-priority account is active the engine returns to a higher-priority
  one as soon as one is healthy (below its swap limit) again. Default 1.

The engine folds a hard limit in by measuring an account's headroom against
its hard limit instead of 100 (``cap_headroom``), so every existing
mechanism — the at-limit escape, "all exhausted", candidate exclusion —
enforces it without a second code path; ``effective_threshold`` maps the
swap limit into that same capped scale. A pace-bound rule joins that path
by being resolved first, so nothing downstream knows the flag exists.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from claude_swap import pace

SWAP_LIMIT_RANGE = (1.0, 100.0)
HARD_LIMIT_RANGE = (1.0, 100.0)
PRIORITY_RANGE = (1, 99)

_KEYS = ("swapLimit", "hardLimit", "hardLimitPace", "priority")

# ``parse_hard_limit``'s answer for "pace": the caller hands it to
# ``apply_rule(hard_limit=PACE)``, which sets the flag and lifts the number.
PACE = "pace"


class _Keep:
    """Sentinel: leave this field as it is (``apply_rule``)."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "KEEP"


KEEP: Any = _Keep()


@dataclass(frozen=True)
class AccountRule:
    swap_limit: float | None = None  # None → the global autoswitch.threshold
    hard_limit: float = 100.0
    priority: int = 1
    hard_pace: bool = False  # hard limit also bound by the week's progress

    @property
    def is_default(self) -> bool:
        return self == AccountRule()

    def swap_for(self, threshold: float) -> float:
        """The swap limit in force: the account's own, else the global one."""
        return self.swap_limit if self.swap_limit is not None else threshold

    def leave_at(self, threshold: float) -> float:
        """Utilization at which the engine wants to leave this account: the
        swap limit, unless the hard limit comes first."""
        return min(self.swap_for(threshold), self.hard_limit)

    def summary(self, threshold: float | None = None, progress: float | None = None) -> str:
        """Compact human form, e.g. ``p2 · swap 95% · hard 50%``. Only the
        parts that differ from the defaults, so a default rule is ``""``.
        A pace-bound limit reads ``hard pace``, with the week's ``progress``
        (``week_progress``) in brackets when the caller knows it, and its
        own number after it when one is set below 100."""
        parts: list[str] = []
        if self.priority != 1:
            parts.append(f"p{self.priority}")
        if self.swap_limit is not None and (
            threshold is None or self.swap_limit != threshold
        ):
            parts.append(f"swap {_pct(self.swap_limit)}%")
        if self.hard_pace:
            hard = "hard pace"
            if progress is not None:
                hard += f" ({progress:.0f}%)"
            if self.hard_limit < 100.0:
                hard += f" ≤{_pct(self.hard_limit)}%"
            parts.append(hard)
        elif self.hard_limit < 100.0:
            parts.append(f"hard {_pct(self.hard_limit)}%")
        return " · ".join(parts)

    def to_record_fields(self) -> dict:
        """The JSON keys this rule writes on a slot record (defaults omitted)."""
        out: dict = {}
        if self.swap_limit is not None:
            out["swapLimit"] = self.swap_limit
        if self.hard_limit < 100.0:
            out["hardLimit"] = self.hard_limit
        if self.hard_pace:
            out["hardLimitPace"] = True
        if self.priority != 1:
            out["priority"] = self.priority
        return out


def _pct(value: float) -> str:
    return f"{value:.10g}"


def _num(value, lo: float, hi: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(min(max(value, lo), hi))


def rule_from_record(record: dict | None) -> AccountRule:
    """Lenient read of a slot record: bad or missing values → defaults."""
    if not isinstance(record, dict):
        return AccountRule()
    swap = _num(record.get("swapLimit"), *SWAP_LIMIT_RANGE)
    hard = _num(record.get("hardLimit"), *HARD_LIMIT_RANGE)
    prio = record.get("priority")
    if isinstance(prio, bool) or not isinstance(prio, (int, float)):
        prio = None
    else:
        prio = int(min(max(int(prio), PRIORITY_RANGE[0]), PRIORITY_RANGE[1]))
    return AccountRule(
        swap_limit=swap,
        hard_limit=hard if hard is not None else 100.0,
        priority=prio if prio is not None else 1,
        hard_pace=record.get("hardLimitPace") is True,
    )


def parse_swap_limit(raw: str | float | None) -> float | None:
    """CLI/TUI input → swap limit. ``None``/``""``/``off``/``default`` clears."""
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip().lower().rstrip("%")
        if text in ("", "off", "default", "none"):
            return None
        raw = _to_float(text, "swap limit")
    return _bounded(float(raw), SWAP_LIMIT_RANGE, "swap limit")


def parse_hard_limit(raw: str | float | None) -> float | str:
    """CLI/TUI input → hard limit: a number, ``100.0`` for cleared, or
    :data:`PACE` for ``pace`` (see ``apply_rule``)."""
    if raw is None:
        return 100.0
    if isinstance(raw, str):
        text = raw.strip().lower().rstrip("%")
        if text in ("", "off", "default", "none"):
            return 100.0
        if text == PACE:
            return PACE
        try:
            raw = float(text)
        except ValueError:
            raise ValueError(
                f"hard limit must be a number (percent) or 'pace', got '{text}'"
            ) from None
    return _bounded(float(raw), HARD_LIMIT_RANGE, "hard limit")


def parse_priority(raw: str | int | None) -> int:
    if raw is None:
        return 1
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in ("", "off", "default", "none"):
            return 1
        try:
            value = int(text)
        except ValueError:
            raise ValueError(f"priority must be a whole number, got '{raw}'") from None
    else:
        value = int(raw)
    lo, hi = PRIORITY_RANGE
    if not lo <= value <= hi:
        raise ValueError(f"priority must be between {lo} and {hi}")
    return value


def _to_float(text: str, label: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"{label} must be a number (percent), got '{text}'") from None


def _bounded(value: float, bounds: tuple[float, float], label: str) -> float:
    lo, hi = bounds
    if not lo <= value <= hi:
        raise ValueError(f"{label} must be between {_pct(lo)} and {_pct(hi)}")
    return value


def apply_rule(
    record: dict,
    *,
    swap_limit: Any = KEEP,
    hard_limit: Any = KEEP,
    hard_pace: Any = KEEP,
    priority: Any = KEEP,
    reset: bool = False,
) -> AccountRule:
    """Update a slot record in place from already-parsed values and return
    the resulting rule. ``KEEP`` leaves a field alone; ``reset`` clears
    everything first. Defaults are stored by *absence* of the key.

    ``hard_limit`` is what the user typed: a number clears the pace flag,
    :data:`PACE` sets it and lifts the number. ``hard_pace`` is the flag on
    its own, for callers (the pool sync) that carry the two separately; given
    together with a number it wins over the number's clearing.
    """
    current = AccountRule() if reset else rule_from_record(record)
    if hard_limit is PACE:
        number, flag = 100.0, True
    elif hard_limit is KEEP:
        number, flag = current.hard_limit, current.hard_pace
    else:
        number, flag = hard_limit, False
    if hard_pace is not KEEP:
        flag = bool(hard_pace)
    updated = replace(
        current,
        swap_limit=current.swap_limit if swap_limit is KEEP else swap_limit,
        hard_limit=number,
        hard_pace=flag,
        priority=current.priority if priority is KEEP else priority,
    )
    for key in _KEYS:
        record.pop(key, None)
    record.update(updated.to_record_fields())
    return updated


# -- pace ------------------------------------------------------------------------


def week_progress(usage: dict | str | None, now: float) -> float | None:
    """How far the account's 7-day window has run, 0–100, or None when the
    window's reset time is unknown (nothing fetched yet, a sentinel, or the
    API sent no ``resets_at``). Same window math as the "(ahead of pace)"
    marker, without its grace period after a reset: a limit needs the real
    number even when that is 0."""
    if not isinstance(usage, dict):
        return None
    result = pace.compute_pace(
        usage.get("seven_day"), fetched_at=now, suppress_after_reset_s=0.0
    )
    return None if result is None else result.expected_pct


def resolve_rule(rule: AccountRule, usage: dict | str | None, now: float) -> AccountRule:
    """The plain rule a pace-bound one means right now: ``hard_limit`` is the
    stricter of its number and the week's progress, and the flag is gone, so
    every consumer of ``AccountRule`` sees a fixed limit. Unknown progress
    leaves only the number. A rule without the flag is returned as is."""
    if not rule.hard_pace:
        return rule
    progress = week_progress(usage, now)
    if progress is None:
        return replace(rule, hard_pace=False)
    return replace(rule, hard_limit=min(rule.hard_limit, progress), hard_pace=False)


# -- engine math ---------------------------------------------------------------


def cap_headroom(headroom: float | None, rule: AccountRule) -> float | None:
    """Headroom measured against the account's hard limit instead of 100.

    ``100 - pct`` becomes ``hard_limit - pct``: zero or negative once the
    hard limit is reached, which is exactly what the engine already treats
    as "at its limit". Unchanged for the default rule.
    """
    if headroom is None:
        return None
    return headroom - (100.0 - rule.hard_limit)


def effective_threshold(rule: AccountRule, threshold: float) -> float:
    """The account's swap limit expressed on the capped-headroom scale
    (``100 - cap_headroom``), so ``utilization >= effective_threshold`` is
    true exactly when the raw utilization has reached the swap limit. A
    swap limit at or past the hard limit maps to 100: the account is only
    ever left through the hard-limit escape."""
    return min(100.0, rule.swap_for(threshold) + (100.0 - rule.hard_limit))
