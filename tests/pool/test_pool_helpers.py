"""Unit tests for the pool helpers `_login`/`_logout`/`_status` delegate to."""

from __future__ import annotations

import pytest

from claude_swap.exceptions import ClaudeSwitchError, PoolAuthError, PoolError
from claude_swap.pool import client as client_mod
from claude_swap.pool.cli import (
    request_login_code,
    apply_pooled_defaults,
    login_pool,
    logout_pool,
    pool_logged_in,
    status_lines,
)
from claude_swap.pool.session import load_session
from claude_swap.settings import load_pool_settings, load_settings, set_setting
from tests.pool.conftest import _publish_row, _switcher


@pytest.fixture
def wired(fake_pool, monkeypatch, temp_home):
    """Route every PoolClient the helpers build through the fake pool."""
    real_init = client_mod.PoolClient.__init__

    def init(self, url, anon_key, *, transport=None, clock=None, timeout_s=10.0):
        real_init(self, url, anon_key, transport=fake_pool, timeout_s=timeout_s)
    monkeypatch.setattr(client_mod.PoolClient, "__init__", init)
    return fake_pool


def _login(wired, switcher, email: str):
    """The two-step login: request the code, then log in with the one the
    fake pool "sent"."""
    request_login_code(wired.base_url, wired.anon_key, email)
    return login_pool(switcher, wired.base_url, wired.anon_key, email, wired.codes[email])


class TestLoginPool:
    def test_login_pool_returns_member_and_pulls(self, wired, owner, borrower):
        s = _switcher()
        client = client_mod.PoolClient(wired.base_url, wired.anon_key)
        _publish_row(wired, client, owner, "acct-o", "rt-1", 1_000)

        result = _login(wired, s, "borrower@x.io")

        assert result.email == "borrower@x.io"
        assert result.role == "member"
        assert result.report.added == ["1"]
        assert load_session(s.backup_dir) is not None
        settings = load_pool_settings(s.backup_dir)
        assert settings.url == wired.base_url
        assert settings.anon_key == wired.anon_key
        assert wired.machines

    def test_first_login_on_a_machine_that_never_added_an_account(self, wired, owner, borrower):
        """A brand-new teammate: cswap installed, nothing ever added, so the
        backup directory has no configs/, no credentials/, no sequence.json.
        The shared `_switcher()` fixture creates all three, which hid this:
        found 2026-09-21 by wiping a real machine and logging in again —
        "[Errno 2] No such file or directory: .../configs/.claude-config-1-…"."""
        from claude_swap.models import Platform
        from claude_swap.switcher import ClaudeAccountSwitcher

        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        assert not s.configs_dir.exists() and not s.sequence_file.exists()
        client = client_mod.PoolClient(wired.base_url, wired.anon_key)
        _publish_row(wired, client, owner, "acct-o1", "rt-1", 1_000)
        _publish_row(wired, client, owner, "acct-o2", "rt-2", 1_000)

        result = _login(wired, s, "borrower@x.io")

        assert result.report.errors == []
        assert result.report.added == ["1", "2"]
        accounts = s._get_sequence_data()["accounts"]
        assert [a["email"] for a in accounts.values()] == ["acct-o1@x.io", "acct-o2@x.io"]
        assert s._read_account_config("1", "acct-o1@x.io")
        assert s._read_account_credentials("1", "acct-o1@x.io")

    def test_login_pool_wrong_code_raises_auth_error(self, wired, owner):
        s = _switcher()
        request_login_code(wired.base_url, wired.anon_key, "owner@x.io")
        with pytest.raises(PoolAuthError):
            login_pool(s, wired.base_url, wired.anon_key, "owner@x.io", "wrong")
        assert load_session(s.backup_dir) is None

    def test_login_pool_suggests_strikes_only_when_low(self, wired, owner):
        s = _switcher()
        result = _login(wired, s, "owner@x.io")
        assert result.suggest_strikes is True

        s2 = _switcher()
        set_setting(s2.backup_dir, "autoswitch.deadTokenStrikes", "2")
        result2 = _login(wired, s2, "owner@x.io")
        assert result2.suggest_strikes is False


class TestLogoutPool:
    def test_logout_pool_remove_and_keep(self, wired, owner, borrower):
        s = _switcher()
        client = client_mod.PoolClient(wired.base_url, wired.anon_key)
        _publish_row(wired, client, owner, "acct-o1", "rt-1", 1_000)
        _publish_row(wired, client, owner, "acct-o2", "rt-2", 1_000)
        _publish_row(wired, client, borrower, "acct-b", "rt-b", 1_000)
        _login(wired, s, "borrower@x.io")

        result = logout_pool(s, keep=False)
        assert sorted(result.removed) == ["1", "2"]
        assert result.kept == []
        assert result.unlinked == ["3"]

    def test_logout_pool_keep_unlinks_without_removing(self, wired, owner, borrower):
        # keep=True never removes, even a slot that would otherwise be
        # eligible for removal (a borrowed, non-owned slot).
        s = _switcher()
        client = client_mod.PoolClient(wired.base_url, wired.anon_key)
        _publish_row(wired, client, owner, "acct-o1", "rt-1", 1_000)
        _publish_row(wired, client, owner, "acct-o2", "rt-2", 1_000)
        _publish_row(wired, client, borrower, "acct-b", "rt-b", 1_000)
        _login(wired, s, "borrower@x.io")

        result = logout_pool(s, keep=True)
        assert result.removed == []
        assert result.kept == []
        assert sorted(result.unlinked) == ["1", "2", "3"]

    def test_logout_pool_reports_kept_slot(self, wired, owner, borrower, monkeypatch):
        s = _switcher()
        client = client_mod.PoolClient(wired.base_url, wired.anon_key)
        _publish_row(wired, client, owner, "acct-o1", "rt-1", 1_000)
        _login(wired, s, "borrower@x.io")

        def fake_remove(identifier, assume_yes=False, quiet=False):
            raise ClaudeSwitchError("live")
        monkeypatch.setattr(s, "remove_account", fake_remove)

        result = logout_pool(s, keep=False)
        assert len(result.kept) == 1
        num, email, reason = result.kept[0]
        assert num == "1"
        assert reason == "live"
        assert load_session(s.backup_dir) is None

    def test_logout_pool_when_logged_out_raises(self):
        s = _switcher()
        with pytest.raises(PoolError):
            logout_pool(s, keep=False)

    def test_logout_then_relogin_relands_borrowed_accounts(self, wired, owner, borrower):
        # logout_pool must reset the pull watermark (pool_state.json), or a
        # same-machine re-login never re-pulls rows that didn't change
        # server-side, so a borrowed slot removed at logout never comes back.
        s = _switcher()
        client = client_mod.PoolClient(wired.base_url, wired.anon_key)
        _publish_row(wired, client, owner, "acct-o", "rt-1", 1_000)

        result1 = _login(wired, s, "borrower@x.io")
        assert result1.report.added == ["1"]

        logout_result = logout_pool(s, keep=False)
        assert logout_result.removed == ["1"]
        assert "1" not in (s._get_sequence_data().get("accounts") or {})

        result2 = _login(wired, s, "borrower@x.io")
        assert result2.report.added == ["1"]
        assert "1" in (s._get_sequence_data().get("accounts") or {})

        result3 = logout_pool(s, keep=True)
        assert result3.removed == []
        assert result3.unlinked == ["1"]
        assert "1" in (s._get_sequence_data().get("accounts") or {})


class TestStatusLines:
    def test_status_lines_logged_out_and_in(self, wired, owner):
        s = _switcher()
        lines = status_lines(s)
        assert any("Not logged in" in line for line in lines)

        client = client_mod.PoolClient(wired.base_url, wired.anon_key)
        _publish_row(wired, client, owner, "acct-o", "rt-1", 1_000)
        _login(wired, s, "owner@x.io")

        from claude_swap.pool.sync import load_state, save_state

        since = "2020-01-01T00:00:00Z"
        state = load_state(s.backup_dir)
        state["attention"] = [{"email": "acct-o@x.io", "since": since, "reportedBy": "m"}]
        save_state(s.backup_dir, state)

        lines = status_lines(s)
        assert wired.base_url in lines[0]
        assert "owner@x.io" in lines[0]
        assert any("needs re-login" in line for line in lines)


class TestApplyPooledDefaults:
    def test_apply_pooled_defaults(self):
        s = _switcher()
        assert apply_pooled_defaults(s) is True
        assert load_settings(s.backup_dir).dead_token_strikes == 2

        assert apply_pooled_defaults(s) is False

        set_setting(s.backup_dir, "autoswitch.deadTokenStrikes", "3")
        assert apply_pooled_defaults(s) is False
        assert load_settings(s.backup_dir).dead_token_strikes == 3


class TestPoolLoggedIn:
    def test_pool_logged_in(self, wired, owner):
        s = _switcher()
        assert pool_logged_in(s) is False
        _login(wired, s, "owner@x.io")
        assert pool_logged_in(s) is True
