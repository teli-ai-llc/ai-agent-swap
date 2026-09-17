"""Publishing an owned account and withdrawing it."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from claude_swap import cli
from claude_swap.exceptions import PoolError
from claude_swap.pool import client as client_mod
from claude_swap.pool.client import PoolClient
from claude_swap.pool.cli import maybe_publish_after_add
from claude_swap.pool.session import save_session
from claude_swap.pool.sync import PoolSync
from claude_swap.rules import rule_from_record
from tests.pool.conftest import _config, _creds, _publish_row, _seed, _switcher

MID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def owner_env(temp_home, fake_pool, owner, monkeypatch):
    # Route every PoolClient built internally (e.g. by build_sync, inside
    # maybe_publish_after_add / cli.main) through the fake pool too, the
    # same way tests/pool/test_pool_cli.py's `wired` fixture does.
    real_init = client_mod.PoolClient.__init__

    def init(self, url, anon_key, *, transport=None, clock=None, timeout_s=10.0):
        real_init(self, url, anon_key, transport=fake_pool, timeout_s=timeout_s)
    monkeypatch.setattr(client_mod.PoolClient, "__init__", init)

    s = _switcher()
    client = PoolClient(fake_pool.base_url, fake_pool.anon_key, transport=fake_pool)
    session = fake_pool.session_for(owner)
    save_session(s.backup_dir, session)
    _seed(s, "1", "mine@x.io", "acct-mine", "rt-1", 1_000)
    return s, client, session


class TestPublishSlot:
    def test_creates_row_links_and_pushes(self, owner_env, fake_pool, owner):
        s, client, session = owner_env
        sync = PoolSync(s, client, session, machine_id=MID)
        row = sync.publish_slot("1", shared=True, swap_limit=80.0, hard_limit=50.0)
        assert row.owner_user_id == owner.user_id and row.shared
        assert row.share_swap_limit == 80.0 and row.share_hard_limit == 50.0
        assert fake_pool.row(row.id)["credential_version"] == 1_000
        assert s.slot_pool_info("1") == (row.id, True)

    def test_republish_updates_sharing_in_place(self, owner_env, fake_pool):
        s, client, session = owner_env
        sync = PoolSync(s, client, session, machine_id=MID)
        first = sync.publish_slot("1", shared=False, swap_limit=None, hard_limit=None)
        second = sync.publish_slot("1", shared=True, swap_limit=None, hard_limit=30.0)
        assert first.id == second.id and second.shared and second.share_hard_limit == 30.0
        assert len(fake_pool.rows()) == 1

    def test_someone_elses_account_is_refused(self, owner_env, fake_pool, borrower):
        s, client, session = owner_env
        theirs = _publish_row(fake_pool, client, borrower, "acct-mine", "rt-0", 500)
        sync = PoolSync(s, client, session, machine_id=MID)
        with pytest.raises(PoolError, match="Borrower"):
            sync.publish_slot("1", shared=True, swap_limit=None, hard_limit=None)
        assert s.slot_pool_info("1") == (None, False)

    def test_reshare_after_unshare(self, owner_env, fake_pool):
        s, client, session = owner_env
        sync = PoolSync(s, client, session, machine_id=MID)
        row = sync.publish_slot("1", shared=True, swap_limit=None, hard_limit=None)
        sync.withdraw_slot("1")
        assert fake_pool.row(row.id)["status"] == "withdrawn"
        again = sync.publish_slot("1", shared=True, swap_limit=None, hard_limit=None)
        assert again.id == row.id
        assert fake_pool.row(row.id)["status"] == "ok"
        assert fake_pool.row(row.id)["shared"] is True

    def test_withdraw(self, owner_env, fake_pool):
        s, client, session = owner_env
        sync = PoolSync(s, client, session, machine_id=MID)
        row = sync.publish_slot("1", shared=True, swap_limit=None, hard_limit=None)
        sync.withdraw_slot("1")
        assert fake_pool.row(row.id)["status"] == "withdrawn"
        assert s.slot_pool_info("1") == (None, False)
        assert "1" in s._get_sequence_data()["accounts"]

    def test_hidden_duplicate_is_explained(self, owner_env, fake_pool, borrower):
        # `owner_env`'s own session belongs to an admin, who can always see
        # every row regardless of sharing (fake_pool's GET visibility rule),
        # so exercising the "invisible to me but blocked by the unique
        # constraint" path needs a plain member acting on the owner_env
        # machine -- not the admin session owner_env hands back, and not a
        # second local switcher (every `_switcher()` in a test shares the
        # same on-disk sequence file under `temp_home`, which would make
        # borrower's own publish_slot link *this* slot too).
        s, client, _owner_session = owner_env
        _publish_row(fake_pool, client, borrower, "acct-mine", "rt-0", 500, shared=False)

        member = fake_pool.add_member("member@x.io", "pw-member")
        member_session = fake_pool.session_for(member)
        sync = PoolSync(s, client, member_session, machine_id=MID)
        with pytest.raises(PoolError, match="not shared it with you"):
            sync.publish_slot("1", shared=True, swap_limit=None, hard_limit=None)
        assert s.slot_pool_info("1") == (None, False)


class TestAfterAdd:
    def test_prompt_yes_publishes(self, owner_env, fake_pool, monkeypatch, capsys):
        s, _, _ = owner_env
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("sys.stdout.isatty", lambda: True)
        maybe_publish_after_add(s, "1", None)
        assert s.slot_pool_info("1")[1] is True
        assert "Shared mine@x.io" in capsys.readouterr().out

    def test_prompt_no_skips(self, owner_env, monkeypatch):
        s, _, _ = owner_env
        monkeypatch.setattr("builtins.input", lambda prompt="": "")
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("sys.stdout.isatty", lambda: True)
        maybe_publish_after_add(s, "1", None)
        assert s.slot_pool_info("1") == (None, False)

    def test_flag_false_never_prompts(self, owner_env, monkeypatch):
        s, _, _ = owner_env
        monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("prompted"))
        maybe_publish_after_add(s, "1", False)
        assert s.slot_pool_info("1") == (None, False)

    def test_relinked_owned_slot_republishes_silently(self, owner_env, fake_pool, monkeypatch):
        s, client, session = owner_env
        row = PoolSync(s, client, session, machine_id=MID).publish_slot("1", shared=True, swap_limit=None, hard_limit=None)
        # owner re-logged in: fresh lineage in the slot
        s._write_account_credentials("1", "mine@x.io", _creds("rt-2", 2_000))
        monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("prompted"))
        maybe_publish_after_add(s, "1", None)
        assert fake_pool.row(row.id)["credential_version"] == 2_000

    def test_not_logged_in_is_silent(self, temp_home, monkeypatch):
        s = _switcher()
        _seed(s, "1", "mine@x.io", "acct-mine", "rt-1", 1_000)
        monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("prompted"))
        maybe_publish_after_add(s, "1", None)


class TestAddFlag:
    def test_cswap_add_pool_flag(self, owner_env, fake_pool, temp_home, monkeypatch, capsys):
        s, _, _ = owner_env
        (temp_home / ".claude.json").write_text(_config("mine@x.io", "acct-mine"))
        (temp_home / ".claude" / ".credentials.json").write_text(_creds("rt-1", 1_000))
        with patch.object(sys, "argv", ["cswap", "add", "--pool"]):
            cli.main()
        assert s.slot_pool_info("1")[1] is True
