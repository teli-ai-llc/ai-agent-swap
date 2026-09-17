"""Owner machine A and borrower machine B against one fake pool."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.models import Platform
from claude_swap.pool.client import PoolClient
from claude_swap.pool.session import save_session
from claude_swap.pool.sync import PoolSync
from claude_swap.switcher import ClaudeAccountSwitcher
from tests.pool.conftest import _config, _creds, _mark_dead, _seed

A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class Machine:
    def __init__(self, home: Path, fake_pool, user, mid: str):
        self.home = home
        (home / ".claude").mkdir(parents=True)
        with self.active():
            self.switcher = ClaudeAccountSwitcher()
            self.switcher.platform = Platform.LINUX
            self.switcher._setup_directories()
            self.switcher._init_sequence_file()
            self.client = PoolClient(fake_pool.base_url, fake_pool.anon_key, transport=fake_pool)
            self.session = fake_pool.session_for(user)
            save_session(self.switcher.backup_dir, self.session)
            self.sync = PoolSync(self.switcher, self.client, self.session, machine_id=mid)

    @contextmanager
    def active(self):
        with patch("pathlib.Path.home", return_value=self.home), \
             patch.dict("os.environ", {"HOME": str(self.home), "USERPROFILE": str(self.home)}):
            yield

    def run(self):
        with self.active():
            return self.sync.run_pass()

    def login_claude(self, email, uuid, rt, exp):
        (self.home / ".claude.json").write_text(_config(email, uuid))
        (self.home / ".claude" / ".credentials.json").write_text(_creds(rt, exp))

    def live_rt(self):
        return json.loads((self.home / ".claude" / ".credentials.json").read_text())["claudeAiOauth"]["refreshToken"]

    def slot_rt(self, num, email):
        with self.active():
            return json.loads(self.switcher._read_account_credentials(num, email))["claudeAiOauth"]["refreshToken"]

    def add(self, *, publish=True):
        with self.active():
            num = self.switcher.add_account()
            if publish:
                self.sync.publish_slot(num, shared=True, swap_limit=None, hard_limit=None)
            return num


@pytest.fixture
def machines(tmp_path, fake_pool, owner, borrower):
    a = Machine(tmp_path / "a", fake_pool, owner, A)
    b = Machine(tmp_path / "b", fake_pool, borrower, B)
    return a, b


def test_rotation_on_owner_reaches_borrower(machines):
    a, b = machines
    a.login_claude("h@x.io", "acct-h", "rt-1", 1_000)
    a.add()
    assert b.run().added == ["1"]
    # Claude Code on A rotates the live login
    a.login_claude("h@x.io", "acct-h", "rt-2", 2_000)
    assert a.run().pushed == ["1"]
    assert b.run().pulled == ["1"]
    assert b.slot_rt("1", "h@x.io") == "rt-2"


def test_rotation_on_borrower_reaches_owner_live_login(machines):
    a, b = machines
    a.login_claude("h@x.io", "acct-h", "rt-1", 1_000)
    a.add()
    b.run()
    # B activates the borrowed account and its Claude Code rotates it
    with b.active():
        b.switcher.switch_to("1", json_output=True)
    b.login_claude("h@x.io", "acct-h", "rt-2", 2_000)
    assert b.run().pushed == ["1"]
    report = a.run()
    assert report.pulled == ["1"]
    assert a.live_rt() == "rt-2"          # A is still logged in as h@x.io: live rewritten


def test_race_loser_recovers_without_flagging(machines, fake_pool):
    a, b = machines
    a.login_claude("h@x.io", "acct-h", "rt-1", 1_000)
    a.add()
    b.run()
    # both rotate inside one interval; A's is the later generation
    b.login_claude("h@x.io", "acct-h", "rt-b", 1_500)
    with b.active():
        b.switcher.switch_to("1", json_output=True, force=True)
    a.login_claude("h@x.io", "acct-h", "rt-a", 2_000)
    b.run()   # pushes rt-b (1500)
    a.run()   # pushes rt-a (2000): wins
    assert fake_pool.rows()[0]["credential"]["claudeAiOauth"]["refreshToken"] == "rt-a"
    report = b.run()
    assert report.pulled == ["1"] and report.flagged == []
    assert b.slot_rt("1", "h@x.io") == "rt-a"
    assert fake_pool.rows()[0]["status"] == "ok"


def test_dead_lineage_flagged_by_borrower_and_healed_by_owner_readd(machines, fake_pool):
    a, b = machines
    a.login_claude("h@x.io", "acct-h", "rt-1", 1_000)
    a.add()
    b.run()
    # B's copy dies (simulated the way test_sync_status does it)
    with b.active():
        _mark_dead(b.switcher, "1", "h@x.io")
    assert b.run().flagged == ["1"]
    assert fake_pool.rows()[0]["status"] == "needs_relogin"
    a.run()
    from claude_swap.pool.sync import attention_lines
    with a.active():
        assert attention_lines(a.switcher.backup_dir)
    # owner logs in again and re-adds; the slot is already linked and owned,
    # so republishing is the silent `sync.run_pass()` path `maybe_publish_after_add`
    # takes for an owned+linked slot -- called directly here (rather than through
    # `maybe_publish_after_add`) so the pass stays wired to the fake pool instead
    # of `build_sync`'s real urllib-backed `PoolClient`.
    a.login_claude("h@x.io", "acct-h", "rt-9", 9_000)
    with a.active():
        a.switcher.add_account()
    report = a.run()
    assert report.pushed == ["1"]
    assert fake_pool.rows()[0]["status"] == "ok"
    report = b.run()
    assert report.pulled == ["1"] and report.healed == ["1"]
    with b.active():
        assert not b.switcher._slot_token_dead("1", "h@x.io")


def test_withdrawal_removes_borrowed_copy(machines):
    a, b = machines
    a.login_claude("h@x.io", "acct-h", "rt-1", 1_000)
    a.add()
    b.run()
    with a.active():
        a.sync.withdraw_slot("1")
    assert b.run().removed == ["1"]
    with b.active():
        assert b.switcher._get_sequence_data()["accounts"] == {}
