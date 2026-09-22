"""Tests for per-account switching rules (rules.py): storage, parsing, and the
capped-headroom math the engine decides with."""

from __future__ import annotations

import pytest

from claude_swap.rules import (
    KEEP,
    PACE,
    AccountRule,
    apply_rule,
    cap_headroom,
    effective_threshold,
    parse_hard_limit,
    parse_priority,
    parse_swap_limit,
    resolve_rule,
    rule_from_record,
    week_progress,
)


class TestRecordRoundTrip:
    def test_missing_keys_are_the_default_rule(self):
        assert rule_from_record({"email": "a@x"}) == AccountRule()
        assert rule_from_record(None) == AccountRule()
        assert AccountRule().is_default

    def test_reads_and_clamps(self):
        rule = rule_from_record({"swapLimit": 95, "hardLimit": 50, "priority": 2})
        assert rule == AccountRule(swap_limit=95.0, hard_limit=50.0, priority=2)
        # garbage → default per key; out-of-range → clamped
        rule = rule_from_record({"swapLimit": "95", "hardLimit": 500, "priority": True})
        assert rule == AccountRule(swap_limit=None, hard_limit=100.0, priority=1)
        assert rule_from_record({"priority": 0}).priority == 1

    def test_apply_writes_only_non_defaults_and_keeps_other_fields(self):
        record = {"email": "a@x", "alias": "main"}
        rule = apply_rule(record, priority=2, hard_limit=50.0)
        assert rule == AccountRule(hard_limit=50.0, priority=2)
        assert record == {"email": "a@x", "alias": "main", "hardLimit": 50.0, "priority": 2}
        # KEEP leaves fields alone; an explicit default removes the key
        apply_rule(record, swap_limit=95.0)
        assert record["swapLimit"] == 95.0 and record["priority"] == 2
        apply_rule(record, priority=1, hard_limit=KEEP)
        assert "priority" not in record and record["hardLimit"] == 50.0
        apply_rule(record, reset=True)
        assert record == {"email": "a@x", "alias": "main"}

    def test_reset_then_set_in_one_call(self):
        record = {"swapLimit": 60.0, "priority": 3}
        rule = apply_rule(record, reset=True, hard_limit=40.0)
        assert rule == AccountRule(hard_limit=40.0)
        assert record == {"hardLimit": 40.0}

    def test_pace_is_a_flag_beside_the_number(self):
        # `--hard-limit pace` (PACE) sets the flag and lifts the number; a
        # number clears the flag again; anything but exactly True is off.
        record = {"email": "a@x"}
        assert apply_rule(record, hard_limit=PACE) == AccountRule(hard_pace=True)
        assert record == {"email": "a@x", "hardLimitPace": True}
        assert rule_from_record({"hardLimitPace": True}).hard_pace is True
        assert rule_from_record({"hardLimitPace": "yes"}).hard_pace is False
        assert apply_rule(record, hard_limit=50.0) == AccountRule(hard_limit=50.0)
        assert record == {"email": "a@x", "hardLimit": 50.0}
        # The sync sets both: a borrower's own 50 under the owner's pace.
        rule = apply_rule(record, hard_limit=50.0, hard_pace=True)
        assert rule == AccountRule(hard_limit=50.0, hard_pace=True)
        assert record == {"email": "a@x", "hardLimit": 50.0, "hardLimitPace": True}
        assert apply_rule(record, hard_pace=False) == AccountRule(hard_limit=50.0)
        assert "hardLimitPace" not in record
        apply_rule(record, hard_limit=PACE, reset=True)
        assert record == {"email": "a@x", "hardLimitPace": True}


class TestParsing:
    def test_swap_limit(self):
        assert parse_swap_limit("95") == 95.0
        assert parse_swap_limit("95%") == 95.0
        assert parse_swap_limit(" 80.5 ") == 80.5
        for cleared in ("", "off", "default", None):
            assert parse_swap_limit(cleared) is None
        with pytest.raises(ValueError, match="between 1 and 100"):
            parse_swap_limit("0")
        with pytest.raises(ValueError, match="must be a number"):
            parse_swap_limit("high")

    def test_hard_limit(self):
        assert parse_hard_limit("50") == 50.0
        assert parse_hard_limit("off") == 100.0
        assert parse_hard_limit("pace") is PACE
        assert parse_hard_limit(" Pace ") is PACE
        with pytest.raises(ValueError, match="between 1 and 100"):
            parse_hard_limit("150")
        with pytest.raises(ValueError, match="must be a number.*or 'pace'"):
            parse_hard_limit("high")

    def test_priority(self):
        assert parse_priority("2") == 2
        assert parse_priority("off") == 1
        with pytest.raises(ValueError, match="between 1 and 99"):
            parse_priority("0")
        with pytest.raises(ValueError, match="whole number"):
            parse_priority("2.5")


class TestEngineMath:
    def test_cap_headroom(self):
        assert cap_headroom(60.0, AccountRule()) == 60.0  # default: unchanged
        assert cap_headroom(60.0, AccountRule(hard_limit=50.0)) == 10.0  # 40% used
        assert cap_headroom(50.0, AccountRule(hard_limit=50.0)) == 0.0  # at the cap
        assert cap_headroom(40.0, AccountRule(hard_limit=50.0)) == -10.0  # over it
        assert cap_headroom(None, AccountRule(hard_limit=50.0)) is None

    def test_effective_threshold(self):
        # default rule: the global threshold, untouched
        assert effective_threshold(AccountRule(), 90.0) == 90.0
        # own swap limit, no cap: itself
        assert effective_threshold(AccountRule(swap_limit=95.0), 90.0) == 95.0
        # swap 40 under a 50 cap: 40% used is 10 pts of capped headroom → 90
        assert effective_threshold(AccountRule(swap_limit=40.0, hard_limit=50.0), 90.0) == 90.0
        # swap at/past the cap: never proactive, only the hard-limit escape
        assert effective_threshold(AccountRule(swap_limit=95.0, hard_limit=50.0), 90.0) == 100.0

    def test_summary_and_leave_at(self):
        rule = AccountRule(swap_limit=95.0, hard_limit=50.0, priority=2)
        assert rule.summary() == "p2 · swap 95% · hard 50%"
        assert rule.summary(threshold=95.0) == "p2 · hard 50%"  # swap == global: elided
        assert rule.leave_at(90.0) == 50.0
        assert AccountRule(swap_limit=60.0).leave_at(90.0) == 60.0
        assert AccountRule().leave_at(90.0) == 90.0

    def test_pace_summary_names_the_week_progress_when_known(self):
        assert AccountRule(hard_pace=True).summary() == "hard pace"
        assert AccountRule(hard_pace=True).summary(progress=61.4) == "hard pace (61%)"
        assert AccountRule(hard_limit=50.0, hard_pace=True).summary() == "hard pace ≤50%"
        assert AccountRule(hard_limit=50.0, priority=2).summary(progress=61.4) == "p2 · hard 50%"


def _week(pct: float, reset_in_s: float, now: float) -> dict:
    """A usage dict whose 7d window resets ``reset_in_s`` after ``now``."""
    from datetime import datetime, timezone

    resets_at = datetime.fromtimestamp(now + reset_in_s, tz=timezone.utc).isoformat()
    return {"five_hour": {"pct": 0.0}, "seven_day": {"pct": pct, "resets_at": resets_at}}


class TestPaceResolution:
    """A pace-bound hard limit is the share of the 7-day window that has run:
    resolved to a plain number at decision time, never stored."""

    NOW = 1_800_000_000.0
    WEEK = 7 * 86400.0

    def test_week_progress_is_elapsed_share_of_the_window(self):
        assert week_progress(_week(10.0, 0.4 * self.WEEK, self.NOW), self.NOW) == pytest.approx(60.0)
        # right after a reset: 0, no grace period (the marker's 24h is not for limits)
        assert week_progress(_week(0.0, self.WEEK, self.NOW), self.NOW) == pytest.approx(0.0)
        assert week_progress(_week(0.0, self.WEEK - 3600, self.NOW), self.NOW) == pytest.approx(100 / 168)
        # a resets_at that has lapsed rolls forward a whole cycle, like the marker
        assert week_progress(_week(10.0, -0.2 * self.WEEK, self.NOW), self.NOW) == pytest.approx(20.0)

    def test_week_progress_is_unknown_without_a_reset_time(self):
        assert week_progress({"seven_day": {"pct": 10.0}}, self.NOW) is None
        assert week_progress({"five_hour": {"pct": 10.0}}, self.NOW) is None
        assert week_progress(None, self.NOW) is None
        assert week_progress("token-expired", self.NOW) is None

    def test_resolve_pins_the_number_and_drops_the_flag(self):
        usage = _week(10.0, 0.4 * self.WEEK, self.NOW)
        rule = AccountRule(swap_limit=80.0, priority=2, hard_pace=True)
        resolved = resolve_rule(rule, usage, self.NOW)
        assert resolved == AccountRule(swap_limit=80.0, hard_limit=pytest.approx(60.0), priority=2)
        assert resolved.hard_pace is False
        # the stricter of a borrower's own number and the week's progress
        capped = resolve_rule(AccountRule(hard_limit=50.0, hard_pace=True), usage, self.NOW)
        assert capped.hard_limit == 50.0
        later = _week(10.0, 0.1 * self.WEEK, self.NOW)
        assert resolve_rule(AccountRule(hard_limit=50.0, hard_pace=True), later, self.NOW).hard_limit == 50.0
        assert resolve_rule(AccountRule(hard_pace=True), later, self.NOW).hard_limit == pytest.approx(90.0)

    def test_resolve_without_progress_keeps_only_the_number(self):
        rule = AccountRule(hard_limit=50.0, hard_pace=True)
        assert resolve_rule(rule, {"seven_day": {"pct": 10.0}}, self.NOW) == AccountRule(hard_limit=50.0)
        assert resolve_rule(AccountRule(hard_pace=True), None, self.NOW) == AccountRule()
        # a plain rule passes through untouched (same object)
        plain = AccountRule(hard_limit=50.0)
        assert resolve_rule(plain, _week(10.0, 3600, self.NOW), self.NOW) is plain

    def test_resolved_limit_feeds_the_engine_math_unchanged(self):
        usage = _week(70.0, 0.4 * self.WEEK, self.NOW)  # 60% through, 70% used
        resolved = resolve_rule(AccountRule(hard_pace=True), usage, self.NOW)
        assert cap_headroom(30.0, resolved) == pytest.approx(-10.0)  # over the pace cap
        assert effective_threshold(resolved, 90.0) == 100.0  # only the hard-limit escape
