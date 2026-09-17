"""The pass is wired into every poll surface, and never breaks one."""

from __future__ import annotations

import sys
import threading
import types
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


class TestMenuBarWiring:
    """The pool pass runs on the refresh worker thread, not the rumps main
    thread. `menubar.py` is only import-safe without the optional `rumps`/
    `AppKit` extras (see tests/test_menubar.py's own docstring: "These tests
    never import or run rumps/AppKit"), so there is no existing helper there
    that constructs a live MenuBarApp to reuse — this test builds a minimal
    fake `rumps`/`AppKit` surface itself, just enough for `menubar.run()` to
    construct the app and call `.run()` without touching a real menu bar or
    writing to the real interpreter's directory.
    """

    def test_refresh_async_runs_pool_pass_on_worker_thread_first(self, temp_home, monkeypatch):
        from claude_swap import menubar

        class _FakeMenuItem:
            def __init__(self, title="", callback=None, icon=None, dimensions=None, template=None):
                self.title = title
                self.callback = callback
                self.state = 0
                self._children = []

            def add(self, item):
                self._children.append(item)

        class _FakeTimer:
            def __init__(self, callback, interval):
                self.callback = callback
                self.interval = interval

            def start(self):
                pass

            def stop(self):
                pass

        captured_apps: list = []

        class _FakeApp:
            def __init__(self, name, quit_button=None):
                self.name = name
                self.title = name
                self.menu = []

            def run(self):
                captured_apps.append(self)

        fake_rumps = types.SimpleNamespace(
            App=_FakeApp,
            MenuItem=_FakeMenuItem,
            Timer=_FakeTimer,
            alert=lambda *a, **kw: None,
            notification=lambda *a, **kw: None,
            quit_application=lambda *a, **kw: None,
            Window=object,
        )
        # rebuild_menu() walks rumps.rumps.NSApp._ns_to_py_and_callback to purge
        # leaked callbacks; a bare namespace with no such attribute makes that
        # getattr(..., None) fall through and skip the purge, same as a rumps
        # release that renamed the private attribute (already handled).
        fake_rumps.rumps = types.SimpleNamespace(NSApp=types.SimpleNamespace())

        class _FakeNSApp:
            def setActivationPolicy_(self, policy):
                pass

            def activateIgnoringOtherApps_(self, v):
                pass

        fake_appkit = types.SimpleNamespace(
            NSApplication=types.SimpleNamespace(sharedApplication=lambda: _FakeNSApp()),
            NSApplicationActivationPolicyAccessory=1,
        )

        recorded: list = []

        def fake_run_pass_quietly(switcher):
            recorded.append(switcher)
            return None

        def inline_thread(target, args=(), daemon=None):
            # Drives the worker without a real thread: start() runs it inline.
            return types.SimpleNamespace(start=lambda: target(*args))

        monkeypatch.setitem(sys.modules, "rumps", fake_rumps)
        monkeypatch.setitem(sys.modules, "AppKit", fake_appkit)
        # Unrelated to this test: on darwin it repairs a real Info.plist next
        # to sys.executable so rumps can resolve a bundle id for notifications.
        monkeypatch.setattr(menubar, "ensure_notification_identity", lambda *a, **kw: None)
        monkeypatch.setattr(threading, "Thread", inline_thread)
        monkeypatch.setattr("claude_swap.pool.sync.run_pass_quietly", fake_run_pass_quietly)

        s = _switcher()
        menubar.run(s)  # constructs MenuBarApp (its own first refresh_async
        # fires once here, via the same inline "thread"), then app.run() — our
        # fake App.run() just captures the instance instead of blocking.
        app = captured_apps[0]
        recorded.clear()

        app.refresh_async()

        assert recorded == [s]
