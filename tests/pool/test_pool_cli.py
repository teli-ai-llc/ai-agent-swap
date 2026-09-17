"""cswap pool login / logout / status through the CLI entry point."""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest

from claude_swap import cli
from claude_swap.pool import client as client_mod
from claude_swap.pool.session import load_session
from claude_swap.settings import load_pool_settings
from tests.pool.conftest import _publish_row, _switcher


@pytest.fixture
def wired(fake_pool, monkeypatch, temp_home):
    """Route every PoolClient the CLI builds through the fake pool."""
    real_init = client_mod.PoolClient.__init__

    def init(self, url, anon_key, *, transport=None, clock=None, timeout_s=10.0):
        real_init(self, url, anon_key, transport=fake_pool, timeout_s=timeout_s)
    monkeypatch.setattr(client_mod.PoolClient, "__init__", init)
    return fake_pool


def _run(argv: list[str]) -> None:
    with patch.object(sys, "argv", ["cswap", *argv]):
        cli.main()


class TestLogin:
    def test_login_saves_session_settings_and_pulls(self, wired, owner, borrower, capsys, monkeypatch):
        s = _switcher()
        client = client_mod.PoolClient(wired.base_url, wired.anon_key)
        _publish_row(wired, client, owner, "acct-o", "rt-1", 1_000)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "pw-borrower")
        _run(["pool", "login", "--url", wired.base_url, "--anon-key", wired.anon_key,
              "--email", "borrower@x.io"])
        out = capsys.readouterr().out
        assert "Logged in to the pool as borrower@x.io" in out
        assert "pulled 1 account" in out
        session = load_session(s.backup_dir)
        assert session.email == "borrower@x.io"
        settings = load_pool_settings(s.backup_dir)
        assert settings.url == wired.base_url and settings.anon_key == wired.anon_key
        assert wired.machines  # registered
        assert s.slot_pool_info("1")[1] is False

    def test_bad_password_exits_1(self, wired, owner, monkeypatch):
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "wrong")
        with pytest.raises(SystemExit) as info:
            _run(["pool", "login", "--url", wired.base_url, "--anon-key", wired.anon_key,
                  "--email", "owner@x.io"])
        assert info.value.code == 1

    def test_missing_member_row_is_explained(self, wired, monkeypatch):
        user = wired.add_member("ghost@x.io", "pw")
        del wired.users[user.user_id]  # can sign in? no: keep auth but drop member row
        wired.users[user.user_id] = user
        monkeypatch.setattr(wired, "_table", lambda name: {} if name == "pool_members" else type(wired)._table(wired, name))
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "pw")
        with pytest.raises(SystemExit):
            _run(["pool", "login", "--url", wired.base_url, "--anon-key", wired.anon_key, "--email", "ghost@x.io"])


class TestLogoutAndStatus:
    def test_logout_removes_borrowed_and_unlinks_owned(self, wired, owner, borrower, monkeypatch, capsys):
        s = _switcher()
        client = client_mod.PoolClient(wired.base_url, wired.anon_key)
        _publish_row(wired, client, owner, "acct-o", "rt-1", 1_000)
        _publish_row(wired, client, borrower, "acct-b", "rt-b", 1_000)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "pw-borrower")
        _run(["pool", "login", "--url", wired.base_url, "--anon-key", wired.anon_key, "--email", "borrower@x.io"])
        accounts = s._get_sequence_data()["accounts"]
        assert len(accounts) == 2
        _run(["pool", "logout"])
        accounts = s._get_sequence_data()["accounts"]
        assert [a["email"] for a in accounts.values()] == ["acct-b@x.io"]
        assert all("poolAccountId" not in a for a in accounts.values())
        assert load_session(s.backup_dir) is None

    def test_logout_keeps_a_slot_whose_removal_fails(self, wired, owner, borrower, monkeypatch, capsys):
        s = _switcher()
        client = client_mod.PoolClient(wired.base_url, wired.anon_key)
        _publish_row(wired, client, owner, "acct-o1", "rt-1", 1_000)
        _publish_row(wired, client, owner, "acct-o2", "rt-2", 1_000)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "pw-borrower")
        _run(["pool", "login", "--url", wired.base_url, "--anon-key", wired.anon_key, "--email", "borrower@x.io"])

        from claude_swap.exceptions import ClaudeSwitchError
        from claude_swap.switcher import ClaudeAccountSwitcher
        real_remove = ClaudeAccountSwitcher.remove_account

        def fake_remove(self, identifier, assume_yes=False, quiet=False):
            if str(identifier) == "1":
                raise ClaudeSwitchError("live session")
            return real_remove(self, identifier, assume_yes=assume_yes, quiet=quiet)

        monkeypatch.setattr(ClaudeAccountSwitcher, "remove_account", fake_remove)
        capsys.readouterr()
        _run(["pool", "logout"])
        out = capsys.readouterr().out
        assert "kept account 1" in out
        assert load_session(s.backup_dir) is None
        accounts = s._get_sequence_data()["accounts"]
        assert "1" in accounts
        assert "poolAccountId" not in accounts["1"]
        assert "2" not in accounts

    def test_logout_keep(self, wired, owner, borrower, monkeypatch):
        s = _switcher()
        client = client_mod.PoolClient(wired.base_url, wired.anon_key)
        _publish_row(wired, client, owner, "acct-o", "rt-1", 1_000)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "pw-borrower")
        _run(["pool", "login", "--url", wired.base_url, "--anon-key", wired.anon_key, "--email", "borrower@x.io"])
        _run(["pool", "logout", "--keep"])
        assert len(s._get_sequence_data()["accounts"]) == 1
        assert load_session(s.backup_dir) is None

    def test_status_json(self, wired, owner, borrower, monkeypatch, capsys):
        s = _switcher()
        client = client_mod.PoolClient(wired.base_url, wired.anon_key)
        _publish_row(wired, client, owner, "acct-o", "rt-1", 1_000)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "pw-borrower")
        _run(["pool", "login", "--url", wired.base_url, "--anon-key", wired.anon_key, "--email", "borrower@x.io"])
        capsys.readouterr()
        _run(["pool", "status", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["schemaVersion"] == 1
        assert payload["member"]["email"] == "borrower@x.io"
        assert payload["accounts"][0] == {"number": 1, "email": "acct-o@x.io", "owned": False,
                                          "poolAccountId": payload["accounts"][0]["poolAccountId"]}
        assert payload["attention"] == []

    def test_status_reports_relative_attention_age(self, wired, owner, borrower, monkeypatch, capsys):
        from datetime import datetime, timedelta, timezone

        from claude_swap.pool.sync import load_state, save_state

        s = _switcher()
        client = client_mod.PoolClient(wired.base_url, wired.anon_key)
        _publish_row(wired, client, owner, "acct-o", "rt-1", 1_000)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "pw-borrower")
        _run(["pool", "login", "--url", wired.base_url, "--anon-key", wired.anon_key, "--email", "borrower@x.io"])
        since = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        state = load_state(s.backup_dir)
        state["attention"] = [{"email": "acct-o@x.io", "since": since, "reportedBy": "m"}]
        save_state(s.backup_dir, state)
        capsys.readouterr()
        _run(["pool", "status"])
        out = capsys.readouterr().out
        assert "reported 5m ago" in out
        assert since not in out

    def test_status_when_logged_out(self, temp_home, capsys):
        from claude_swap.pool.session import MACHINE_ID_FILENAME

        s = _switcher()
        _run(["pool", "status"])
        assert "Not logged in to a pool" in capsys.readouterr().out
        _run(["pool", "status", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["machineId"] is None
        assert not (s.backup_dir / MACHINE_ID_FILENAME).exists()
