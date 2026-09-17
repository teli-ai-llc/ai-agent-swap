"""The pass is wired into every poll surface, and never breaks one."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from claude_swap.autoswitch import AutoSwitchEngine, ErrorEvent
from claude_swap.settings import AutoSwitchSettings
from tests.pool.conftest import _switcher


class TestEngineHook:
    def test_pre_tick_runs_before_each_tick(self, temp_home):
        s = _switcher()
        seen = []
        engine = AutoSwitchEngine(s, AutoSwitchSettings(), seen.append, dry_run=True,
                                  pre_tick=lambda: seen.append("pre"))
        engine.tick()
        assert seen[0] == "pre"

    def test_pre_tick_failure_is_an_error_event_not_a_crash(self, temp_home):
        s = _switcher()
        events = []

        def boom():
            raise RuntimeError("pool exploded")
        engine = AutoSwitchEngine(s, AutoSwitchSettings(), events.append, dry_run=True, pre_tick=boom)
        engine.tick()
        errors = [e for e in events if isinstance(e, ErrorEvent)]
        assert errors and "pool exploded" in errors[0].message and errors[0].transient

    def test_no_pre_tick_is_the_default(self, temp_home):
        s = _switcher()
        engine = AutoSwitchEngine(s, AutoSwitchSettings(), lambda e: None, dry_run=True)
        engine.tick()  # must not raise


class TestAutoCommandWiring:
    def test_auto_command_passes_run_pass_quietly(self, temp_home, monkeypatch):
        from claude_swap import cli
        captured = {}

        class FakeEngine:
            def __init__(self, *a, **kw):
                captured.update(kw)
            def tick(self):
                from claude_swap.autoswitch import TickOutcome
                return TickOutcome.NO_ACTION
            def run_loop(self):
                return 0
        monkeypatch.setattr("claude_swap.autoswitch.AutoSwitchEngine", FakeEngine)
        with pytest.raises(SystemExit):
            cli._auto_command(["--once"])
        assert captured.get("pre_tick") is not None
