"""cswap sync --once, the loop, and the launchd wrapper."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from claude_swap import cli
from claude_swap.pool import client as client_mod
from claude_swap.pool.session import save_session
from tests.pool.conftest import _publish_row, _switcher


@pytest.fixture
def logged_in(fake_pool, owner, borrower, monkeypatch, temp_home):
    real_init = client_mod.PoolClient.__init__

    def init(self, url, anon_key, *, transport=None, clock=None, timeout_s=10.0):
        real_init(self, url, anon_key, transport=fake_pool, timeout_s=timeout_s)
    monkeypatch.setattr(client_mod.PoolClient, "__init__", init)
    s = _switcher()
    client = client_mod.PoolClient(fake_pool.base_url, fake_pool.anon_key)
    _publish_row(fake_pool, client, owner, "acct-o", "rt-1", 1_000)
    save_session(s.backup_dir, fake_pool.session_for(borrower))
    return s


def _run(argv):
    with patch.object(sys, "argv", ["cswap", *argv]):
        cli.main()


class TestOnce:
    def test_once_pulls_and_exits_0(self, logged_in, capsys):
        with pytest.raises(SystemExit) as info:
            _run(["sync", "--once"])
        assert info.value.code == 0
        assert "pulled 1 account" in capsys.readouterr().out
        assert "1" in logged_in._get_sequence_data()["accounts"]

    def test_once_warns_when_the_remote_control_guard_is_missing(self, logged_in, capsys):
        from claude_swap.pool.guard import enforce_remote_control_guard, guard_settings_path

        with pytest.raises(SystemExit):
            _run(["sync", "--once"])
        assert "Remote Control is not disabled" in capsys.readouterr().out
        enforce_remote_control_guard(guard_settings_path())
        with pytest.raises(SystemExit):
            _run(["sync", "--once"])
        assert "Remote Control" not in capsys.readouterr().out

    def test_once_when_unreachable_exits_1(self, logged_in, fake_pool, capsys):
        fake_pool.offline = True
        with pytest.raises(SystemExit) as info:
            _run(["sync", "--once"])
        assert info.value.code == 1
        assert "unreachable" in capsys.readouterr().out

    def test_once_when_logged_out_exits_1(self, temp_home, capsys):
        _switcher()
        with pytest.raises(SystemExit) as info:
            _run(["sync", "--once"])
        assert info.value.code == 1
        assert "Not logged in" in capsys.readouterr().out


class TestLoop:
    def test_loop_runs_until_interrupted(self, logged_in, monkeypatch):
        ticks = []

        def fake_sleep(seconds):
            ticks.append(seconds)
            if len(ticks) == 2:
                raise KeyboardInterrupt
        monkeypatch.setattr("time.sleep", fake_sleep)
        with pytest.raises(SystemExit) as info:
            _run(["sync"])
        assert info.value.code == 0
        assert ticks == [30.0, 30.0]

    def test_loop_survives_a_failing_pass(self, logged_in, monkeypatch, capsys):
        from claude_swap.pool import cli
        ticks = []
        call_count = [0]

        def failing_build_sync(switcher):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("boom")
            return None

        def fake_sleep(seconds):
            ticks.append(seconds)
            if len(ticks) == 2:
                raise KeyboardInterrupt
        monkeypatch.setattr(cli, "build_sync", failing_build_sync)
        monkeypatch.setattr("time.sleep", fake_sleep)
        with pytest.raises(SystemExit) as info:
            _run(["sync"])
        assert info.value.code == 0
        output = capsys.readouterr().out
        assert "sync pass failed: RuntimeError: boom" in output
        assert "Not logged in" in output


class TestService:
    def test_install_uses_sync_label(self, logged_in, monkeypatch):
        calls = {}
        monkeypatch.setattr(sys, "platform", "darwin")
        from claude_swap import launch_agent
        monkeypatch.setattr(launch_agent, "install",
                            lambda **kw: calls.update(kw) or {"plist": "/x", "loaded": True, "pid": 1})
        with pytest.raises(SystemExit) as info:
            _run(["sync", "--install-service"])
        assert info.value.code == 0
        assert calls["label"] == "com.cswap.sync" and calls["arguments"] == ("sync",)

    def test_service_flags_refuse_off_macos(self, logged_in, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "linux")
        with pytest.raises(SystemExit) as info:
            _run(["sync", "--install-service"])
        assert info.value.code == 1
        assert "macOS" in capsys.readouterr().err
