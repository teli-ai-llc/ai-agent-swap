"""recommend.py: the time-aware ranking behind `smart` and every best pick."""

from __future__ import annotations

from datetime import datetime, timezone

from claude_swap import recommend
from claude_swap.rules import AccountRule

NOW = 1_800_000_000.0
H = 3600.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _u(pct5: float, pct7: float, hours7: float | None, *, hours5: float | None = None) -> dict:
    five: dict = {"pct": pct5}
    if hours5 is not None:
        five["resets_at"] = _iso(NOW + hours5 * H)
    seven: dict = {"pct": pct7}
    if hours7 is not None:
        seven["resets_at"] = _iso(NOW + hours7 * H)
    return {"five_hour": five, "seven_day": seven}


class TestOutlook:
    def test_urgency_is_weekly_room_over_hours_to_reset(self):
        o = recommend.outlook(_u(0, 60, 8), None, (), NOW)
        assert o.weekly_room == 40
        assert o.urgency == 5.0  # 40% over 8h

    def test_unknown_reset_counts_as_a_week_away(self):
        o = recommend.outlook(_u(0, 0, None), None, (), NOW)
        assert o.urgency == 100 / 168

    def test_owner_hard_limit_caps_the_room(self):
        o = recommend.outlook(_u(0, 30, 10), AccountRule(hard_limit=50.0), (), NOW)
        assert o.weekly_room == 20
        assert o.headroom == 20

    def test_a_nearly_spent_5h_window_makes_it_unusable(self):
        o = recommend.outlook(_u(95, 10, 5), None, (), NOW)
        assert o.headroom == 5
        assert not o.usable

    def test_sentinels_are_unknown(self):
        o = recommend.outlook("token-expired", None, (), NOW)
        assert o.headroom is None and not o.usable


class TestProjectResets:
    def test_a_passed_window_projects_to_zero_and_rolls_forward(self):
        usage = _u(92, 40, 30, hours5=-0.5)
        projected, rolled = recommend.project_resets(usage, NOW)
        assert rolled
        assert projected["five_hour"]["pct"] == 0.0
        new_ts = datetime.fromisoformat(projected["five_hour"]["resets_at"]).timestamp()
        assert new_ts == NOW + 4.5 * H
        assert projected["seven_day"] == usage["seven_day"]
        assert usage["five_hour"]["pct"] == 92  # input untouched

    def test_future_windows_are_untouched(self):
        usage = _u(50, 40, 30, hours5=2)
        projected, rolled = recommend.project_resets(usage, NOW)
        assert not rolled and projected == usage


class TestRank:
    def test_perishable_quota_beats_fresh_quota(self):
        """40% left that expires in 8h ranks above 45% left for 6 days."""
        ranked = recommend.rank(
            {"1": (_u(0, 55, 6 * 24), None), "2": (_u(0, 60, 8), None)}, (), NOW
        )
        assert [n for n, _ in ranked] == ["2", "1"]

    def test_unusable_and_unknown_sort_last(self):
        ranked = recommend.rank(
            {
                "1": (_u(98, 0, 2), None),  # 5h nearly spent
                "2": (None, None),
                "3": (_u(0, 0, 160), None),
            },
            (),
            NOW,
        )
        assert ranked[0][0] == "3"

    def test_priority_leads(self):
        ranked = recommend.rank(
            {
                "1": (_u(0, 0, 160), AccountRule(priority=1)),
                "2": (_u(0, 60, 8), AccountRule(priority=2)),
            },
            (),
            NOW,
        )
        assert ranked[0][0] == "1"

    def test_best_is_none_when_nothing_is_usable(self):
        assert recommend.best({"1": (_u(99, 0, 1), None)}, (), NOW) is None


class TestBeats:
    def test_needs_ratio_and_absolute_gain(self):
        active = recommend.outlook(_u(0, 0, 160), None, (), NOW)  # ~0.63 %/h
        clearly = recommend.outlook(_u(0, 60, 8), None, (), NOW)  # 5 %/h
        barely = recommend.outlook(_u(0, 20, 120), None, (), NOW)  # ~0.67 %/h
        assert recommend.beats(clearly, active)
        assert not recommend.beats(barely, active)


class TestText:
    def test_format_duration(self):
        assert recommend.format_duration(90) == "1m"
        assert recommend.format_duration(3 * H + 12 * 60) == "3h 12m"
        assert recommend.format_duration(52 * H) == "2d 4h"

    def test_reason(self):
        o = recommend.outlook(_u(0, 38, 28), None, (), NOW)
        assert recommend.reason(o, NOW) == "62% left this week · resets in 1d 4h"

    def test_reason_for_a_blocked_account_says_when_it_returns(self):
        o = recommend.outlook(_u(97, 10, 100, hours5=2), None, (), NOW)
        assert recommend.reason(o, NOW) == "5h limit · back in 2h 0m"
