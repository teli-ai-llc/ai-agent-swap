"""Pull side of a sync pass."""

from __future__ import annotations

import json

import pytest

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.pool.sync import PoolSync, load_state
from claude_swap.rules import rule_from_record
from tests.pool.conftest import _config, _creds, _publish_row, _seed

MID = "11111111-1111-1111-1111-111111111111"


def _login(s, num, email):
    return json.loads(s._read_account_credentials(num, email))["claudeAiOauth"]


class TestPull:
    def test_new_shared_row_lands_in_a_free_slot_as_borrowed(self, sync_env, fake_pool, owner, borrower):
        s, client, _ = sync_env
        session = fake_pool.session_for(borrower)
        row = _publish_row(fake_pool, client, owner, "acct-o", "rt-1", 1_000)
        client.update_sharing(fake_pool.session_for(owner), row.id, shared=True, swap_limit=80.0, hard_limit=50.0)
        report = PoolSync(s, client, session, machine_id=MID).run_pass()
        assert report.added == ["1"]
        assert s.slot_pool_info("1") == (row.id, False)
        assert _login(s, "1", "acct-o@x.io")["refreshToken"] == "rt-1"
        record = s._get_sequence_data()["accounts"]["1"]
        assert record["email"] == "acct-o@x.io" and record["uuid"] == "acct-o"
        rule = rule_from_record(record)
        assert rule.swap_limit == 80.0 and rule.hard_limit == 50.0 and rule.priority == 2
        assert load_state(s.backup_dir)["pulledAt"] == fake_pool.row(row.id)["updated_at"]

    def test_own_row_lands_as_owned_with_default_rule(self, sync_env, fake_pool, owner):
        s, client, session = sync_env
        row = _publish_row(fake_pool, client, owner, "acct-o", "rt-1", 1_000)
        PoolSync(s, client, session, machine_id=MID).run_pass()
        assert s.slot_pool_info("1") == (row.id, True)
        assert rule_from_record(s._get_sequence_data()["accounts"]["1"]).priority == 1

    def test_newer_row_replaces_backup(self, sync_env, fake_pool, owner, borrower):
        s, client, _ = sync_env
        session = fake_pool.session_for(borrower)
        row = _publish_row(fake_pool, client, owner, "acct-o", "rt-1", 1_000)
        _seed(s, "3", "acct-o@x.io", "acct-o", "rt-1", 1_000, pool_id=row.id)
        client.push_credential(fake_pool.session_for(owner), row.id,
                               fake_pool.row(row.id)["credential"] | {"claudeAiOauth": {
                                   "accessToken": "a2", "refreshToken": "rt-2", "expiresAt": 2_000}},
                               2_000, "sha256:2", "99999999-9999-9999-9999-999999999999")
        report = PoolSync(s, client, session, machine_id=MID).run_pass()
        assert report.pulled == ["3"] and report.added == []
        assert _login(s, "3", "acct-o@x.io")["refreshToken"] == "rt-2"
        # the pulled fingerprint counts as already pushed: no echo on the next pass
        assert load_state(s.backup_dir)["pushed"]["3"].startswith("sha256:")
        second = PoolSync(s, client, session, machine_id=MID).run_pass()
        assert second.pushed == [] and second.pulled == []

    def test_older_or_equal_row_is_ignored(self, sync_env, fake_pool, owner):
        s, client, session = sync_env
        row = _publish_row(fake_pool, client, owner, "acct-o", "rt-1", 1_000)
        _seed(s, "3", "acct-o@x.io", "acct-o", "rt-5", 5_000, pool_id=row.id, owned=True)
        report = PoolSync(s, client, session, machine_id=MID).run_pass()
        assert report.pulled == []
        assert _login(s, "3", "acct-o@x.io")["refreshToken"] == "rt-5"
        assert fake_pool.row(row.id)["credential_version"] == 5_000   # push won instead

    def test_active_login_is_rewritten(self, sync_env, fake_pool, owner, borrower, temp_home):
        s, client, _ = sync_env
        session = fake_pool.session_for(borrower)
        row = _publish_row(fake_pool, client, owner, "acct-o", "rt-1", 1_000)
        _seed(s, "3", "acct-o@x.io", "acct-o", "rt-1", 1_000, pool_id=row.id)
        (temp_home / ".claude.json").write_text(_config("acct-o@x.io", "acct-o"))
        (temp_home / ".claude" / ".credentials.json").write_text(_creds("rt-1", 1_000))
        data = s._get_sequence_data(); data["activeAccountNumber"] = 3; s._write_json(s.sequence_file, data)
        client.push_credential(fake_pool.session_for(owner), row.id,
                               fake_pool.row(row.id)["credential"] | {"claudeAiOauth": {
                                   "accessToken": "a2", "refreshToken": "rt-2", "expiresAt": 2_000}},
                               2_000, "sha256:2", "99999999-9999-9999-9999-999999999999")
        PoolSync(s, client, session, machine_id=MID).run_pass()
        live = json.loads((temp_home / ".claude" / ".credentials.json").read_text())
        assert live["claudeAiOauth"]["refreshToken"] == "rt-2"

    def test_active_rewrite_requires_matching_org(self, sync_env, fake_pool, owner, borrower, temp_home):
        s, client, _ = sync_env
        session = fake_pool.session_for(borrower)
        row = _publish_row(fake_pool, client, owner, "acct-o", "rt-1", 1_000)
        _seed(s, "3", "acct-o@x.io", "acct-o", "rt-1", 1_000, pool_id=row.id)
        # same email, different org than the slot -- not the same live identity
        (temp_home / ".claude.json").write_text(_config("acct-o@x.io", "acct-o", "other-org"))
        (temp_home / ".claude" / ".credentials.json").write_text(_creds("rt-1", 1_000))
        data = s._get_sequence_data(); data["activeAccountNumber"] = 3; s._write_json(s.sequence_file, data)
        client.push_credential(fake_pool.session_for(owner), row.id,
                               fake_pool.row(row.id)["credential"] | {"claudeAiOauth": {
                                   "accessToken": "a2", "refreshToken": "rt-2", "expiresAt": 2_000}},
                               2_000, "sha256:2", "99999999-9999-9999-9999-999999999999")
        report = PoolSync(s, client, session, machine_id=MID).run_pass()
        assert report.pulled == ["3"]
        assert _login(s, "3", "acct-o@x.io")["refreshToken"] == "rt-2"   # backup updated
        live = json.loads((temp_home / ".claude" / ".credentials.json").read_text())
        assert live["claudeAiOauth"]["refreshToken"] == "rt-1"   # live untouched

    def test_withdrawn_removes_borrowed_slot_only(self, sync_env, fake_pool, owner, borrower):
        s, client, _ = sync_env
        session = fake_pool.session_for(borrower)
        row = _publish_row(fake_pool, client, owner, "acct-o", "rt-1", 1_000)
        _seed(s, "3", "acct-o@x.io", "acct-o", "rt-1", 1_000, pool_id=row.id)
        client.set_status(fake_pool.session_for(owner), row.id, "withdrawn", MID)
        report = PoolSync(s, client, session, machine_id=MID).run_pass()
        assert report.removed == ["3"]
        assert "3" not in s._get_sequence_data()["accounts"]

    def test_refused_removal_keeps_slot_unlinked(self, sync_env, fake_pool, owner, borrower, monkeypatch):
        s, client, _ = sync_env
        session = fake_pool.session_for(borrower)
        row = _publish_row(fake_pool, client, owner, "acct-o", "rt-1", 1_000)
        _seed(s, "3", "acct-o@x.io", "acct-o", "rt-1", 1_000, pool_id=row.id)
        client.set_status(fake_pool.session_for(owner), row.id, "withdrawn", MID)

        def _raise(*a, **k):
            raise ClaudeSwitchError("live session")
        monkeypatch.setattr(s, "remove_account", _raise)
        report = PoolSync(s, client, session, machine_id=MID).run_pass()
        assert report.removed == []
        assert "3" in s._get_sequence_data()["accounts"]
        assert s.slot_pool_info("3") == (None, False)
        assert report.errors
        assert load_state(s.backup_dir)["pulledAt"] == fake_pool.row(row.id)["updated_at"]

    def test_invalid_blob_is_skipped_and_watermark_advances(self, sync_env, fake_pool, owner, borrower):
        s, client, _ = sync_env
        session = fake_pool.session_for(borrower)
        row = _publish_row(fake_pool, client, owner, "acct-o", "rt-1", 1_000)
        fake_pool.row(row.id)["credential"] = {"garbage": True}
        report = PoolSync(s, client, session, machine_id=MID).run_pass()
        assert report.added == [] and report.errors
        assert load_state(s.backup_dir)["pulledAt"] == fake_pool.row(row.id)["updated_at"]

    def test_local_write_failure_holds_watermark(self, sync_env, fake_pool, owner, borrower, monkeypatch):
        s, client, _ = sync_env
        session = fake_pool.session_for(borrower)
        _publish_row(fake_pool, client, owner, "acct-o", "rt-1", 1_000)
        monkeypatch.setattr(
            s, "_write_account_credentials",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )
        report = PoolSync(s, client, session, machine_id=MID).run_pass()
        assert report.errors
        assert report.added == []
        assert load_state(s.backup_dir)["pulledAt"] is None


class TestOwnerLimitChangesReachExistingBorrowers:
    """The owner's swap/hard limits used to be applied only when a row first
    landed; a later `cswap pool share` never reached machines that already
    held the login. The rule now: a borrower who left the owner's value alone
    follows it both ways; a borrower's own stricter value survives; anything
    looser than the owner's is pulled back down."""

    def _land(self, sync_env, fake_pool, owner, borrower, *, swap, hard):
        s, client, _ = sync_env
        self.owner_session = fake_pool.session_for(owner)
        row = _publish_row(fake_pool, client, owner, "acct-o", "rt-1", 1_000)
        client.update_sharing(self.owner_session, row.id, shared=True, swap_limit=swap, hard_limit=hard)
        sync = PoolSync(s, client, fake_pool.session_for(borrower), machine_id=MID)
        assert sync.run_pass().added == ["1"]
        return s, client, sync, row

    @staticmethod
    def _rule(s):
        return rule_from_record(s._get_sequence_data()["accounts"]["1"])

    def test_tightened_limits_reach_a_borrower_without_any_token_change(self, sync_env, fake_pool, owner, borrower):
        s, client, sync, row = self._land(sync_env, fake_pool, owner, borrower, swap=80.0, hard=50.0)
        client.update_sharing(self.owner_session, row.id, shared=True, swap_limit=70.0, hard_limit=40.0)
        report = sync.run_pass()
        assert report.errors == [] and report.pulled == []   # same credential, only the limits moved
        rule = self._rule(s)
        assert (rule.swap_limit, rule.hard_limit, rule.priority) == (70.0, 40.0, 2)

    def test_loosened_limits_reach_a_borrower_who_never_touched_them(self, sync_env, fake_pool, owner, borrower):
        s, client, sync, row = self._land(sync_env, fake_pool, owner, borrower, swap=80.0, hard=50.0)
        client.update_sharing(self.owner_session, row.id, shared=True, swap_limit=None, hard_limit=None)
        sync.run_pass()
        rule = self._rule(s)
        assert rule.swap_limit is None and rule.hard_limit == 100.0

    def test_limits_added_later_reach_a_borrower_who_landed_with_none(self, sync_env, fake_pool, owner, borrower):
        s, client, sync, row = self._land(sync_env, fake_pool, owner, borrower, swap=None, hard=None)
        assert self._rule(s).hard_limit == 100.0
        client.update_sharing(self.owner_session, row.id, shared=True, swap_limit=85.0, hard_limit=60.0)
        sync.run_pass()
        rule = self._rule(s)
        assert (rule.swap_limit, rule.hard_limit) == (85.0, 60.0)

    def test_a_borrowers_stricter_choice_survives_until_the_owner_goes_lower(self, sync_env, fake_pool, owner, borrower):
        s, client, sync, row = self._land(sync_env, fake_pool, owner, borrower, swap=80.0, hard=50.0)
        s.set_account_rule("1", hard_limit=30.0, quiet=True)
        client.update_sharing(self.owner_session, row.id, shared=True, swap_limit=80.0, hard_limit=40.0)
        sync.run_pass()
        assert self._rule(s).hard_limit == 30.0          # still the borrower's stricter 30
        client.update_sharing(self.owner_session, row.id, shared=True, swap_limit=80.0, hard_limit=20.0)
        sync.run_pass()
        assert self._rule(s).hard_limit == 20.0          # the owner went below it

    def test_a_looser_local_rule_is_pulled_back_at_the_next_row_update(self, sync_env, fake_pool, owner, borrower):
        s, client, sync, row = self._land(sync_env, fake_pool, owner, borrower, swap=80.0, hard=50.0)
        s.set_account_rule("1", swap_limit=None, hard_limit=100.0, quiet=True)
        # any update of the row will do; here the owner's machine pushes a rotation
        blob = {"oauthAccount": {"accountUuid": "acct-o", "organizationUuid": "", "emailAddress": "acct-o@x.io"},
                "claudeAiOauth": {"accessToken": "a-rt-2", "refreshToken": "rt-2", "expiresAt": 2_000}}
        assert client.push_credential(self.owner_session, row.id, blob, 2_000, "sha256:r2", MID)
        report = sync.run_pass()
        assert report.pulled == ["1"]
        rule = self._rule(s)
        assert (rule.swap_limit, rule.hard_limit) == (80.0, 50.0)

    def test_the_borrowers_priority_is_never_touched(self, sync_env, fake_pool, owner, borrower):
        s, client, sync, row = self._land(sync_env, fake_pool, owner, borrower, swap=80.0, hard=50.0)
        s.set_account_rule("1", priority=5, quiet=True)
        client.update_sharing(self.owner_session, row.id, shared=True, swap_limit=70.0, hard_limit=40.0)
        sync.run_pass()
        rule = self._rule(s)
        assert (rule.swap_limit, rule.hard_limit, rule.priority) == (70.0, 40.0, 5)

    def test_the_owners_own_machine_keeps_its_own_rule(self, sync_env, fake_pool, owner):
        """Share limits are for borrowers; they are not the owner's rule."""
        s, client, session = sync_env
        row = _publish_row(fake_pool, client, owner, "acct-o", "rt-1", 1_000)
        sync = PoolSync(s, client, session, machine_id=MID)
        sync.run_pass()
        s.set_account_rule("1", swap_limit=95.0, quiet=True)
        client.update_sharing(session, row.id, shared=True, swap_limit=70.0, hard_limit=40.0)
        sync.run_pass()
        rule = self._rule(s)
        assert (rule.swap_limit, rule.hard_limit, rule.priority) == (95.0, 100.0, 1)

    def test_an_unchanged_row_does_not_rewrite_the_roster(self, sync_env, fake_pool, owner, borrower):
        s, client, sync, row = self._land(sync_env, fake_pool, owner, borrower, swap=80.0, hard=50.0)
        before = s._get_sequence_data()["lastUpdated"]
        client.update_sharing(self.owner_session, row.id, shared=True, swap_limit=80.0, hard_limit=50.0)
        sync.run_pass()
        assert s._get_sequence_data()["lastUpdated"] == before


class TestReconcileOwnerLimit:
    """``None`` is "no limit". (current, previous, new) -> result."""

    @pytest.mark.parametrize("current, previous, new, expected", [
        (50.0, 50.0, 40.0, 40.0),     # untouched: follow the owner tighter
        (50.0, 50.0, 80.0, 80.0),     # untouched: follow the owner looser
        (50.0, 50.0, None, None),     # untouched: the owner lifted the limit
        (None, None, 60.0, 60.0),     # untouched "no limit": the owner added one
        (30.0, 50.0, 40.0, 30.0),     # borrower's stricter choice survives
        (30.0, 50.0, 20.0, 20.0),     # ... until the owner goes below it
        (30.0, 50.0, None, 30.0),     # ... and when the owner lifts the limit
        (None, 50.0, 50.0, 50.0),     # looser than allowed: pulled back down
        (90.0, 50.0, 60.0, 60.0),
    ])
    def test_with_a_remembered_previous_value(self, current, previous, new, expected):
        from claude_swap.pool.sync import reconcile_owner_limit
        assert reconcile_owner_limit(current, previous, new, previous_known=True) == expected

    @pytest.mark.parametrize("current, new, expected", [
        (50.0, 40.0, 40.0),
        (50.0, 80.0, 50.0),           # cannot tell an override from the old value: stay strict
        (None, 60.0, 60.0),
        (50.0, None, 50.0),
    ])
    def test_a_record_landed_before_the_value_was_remembered(self, current, new, expected):
        from claude_swap.pool.sync import reconcile_owner_limit
        assert reconcile_owner_limit(current, None, new, previous_known=False) == expected


def test_unlinking_a_slot_forgets_what_the_owner_allowed(sync_env, fake_pool, owner, borrower):
    s, client, _ = sync_env
    row = _publish_row(fake_pool, client, owner, "acct-o", "rt-1", 1_000)
    client.update_sharing(fake_pool.session_for(owner), row.id, shared=True, swap_limit=80.0, hard_limit=50.0)
    PoolSync(s, client, fake_pool.session_for(borrower), machine_id=MID).run_pass()
    record = s._get_sequence_data()["accounts"]["1"]
    assert record["poolShareSwapLimit"] == 80.0 and record["poolShareHardLimit"] == 50.0
    s.set_slot_pool_info("1", None, False)
    record = s._get_sequence_data()["accounts"]["1"]
    assert "poolShareSwapLimit" not in record and "poolShareHardLimit" not in record


class TestConcurrentPassesNeverDuplicateASlot:
    """Two sync surfaces run on one machine (the launchd `cswap sync` service,
    the TUI, the menu bar panel). `_apply_row` decides "this row has no slot
    yet" *before* taking the roster lock, so two passes could both decide it
    and both land the same pool row in a slot of its own.

    Seen live 2026-09-21: slots 4 and 5 both carried pool row
    f032e319… with the same `added` second, showing one teammate's account
    twice in `cswap list`."""

    def test_landing_a_row_twice_reuses_the_slot(self, sync_env, fake_pool, owner, borrower):
        s, client, _ = sync_env
        row = _publish_row(fake_pool, client, owner, "acct-o", "rt-1", 1_000)
        sync = PoolSync(s, client, fake_pool.session_for(borrower), machine_id=MID)
        assert sync.run_pass().added == ["1"]

        # the racing pass: it read the roster before the first pass wrote it,
        # so its own `_slot_for_row` said None too.
        creds, config = sync.client.get_account(sync.session, row.id).credential, None
        from claude_swap.pool.blob import blob_to_local
        creds_text, config_text = blob_to_local(fake_pool.row(row.id)["credential"])
        num, created = sync._land_new_row(row, False, creds_text, config_text)
        assert (num, created) == ("1", False)
        accounts = s._get_sequence_data()["accounts"]
        assert list(accounts) == ["1"]
        assert accounts["1"]["poolAccountId"] == row.id

    def test_a_racing_pass_reports_nothing_added(self, sync_env, fake_pool, owner, borrower, monkeypatch):
        s, client, _ = sync_env
        _publish_row(fake_pool, client, owner, "acct-o", "rt-1", 1_000)
        sync = PoolSync(s, client, fake_pool.session_for(borrower), machine_id=MID)
        assert sync.run_pass().added == ["1"]

        # The race, exactly: the check in `_apply_row` reads a roster written
        # before the other pass landed the row (stale -> None); the re-check
        # under the lock reads the current one.
        real = PoolSync._slot_for_row
        seen = {"outer": False}

        def stale_once(self, data, row):
            if not seen["outer"]:
                seen["outer"] = True
                return None
            return real(self, data, row)

        monkeypatch.setattr(PoolSync, "_slot_for_row", stale_once)
        state = load_state(s.backup_dir)
        state["pulledAt"] = None          # make it look at the row again
        from claude_swap.pool.sync import save_state
        save_state(s.backup_dir, state)
        report = sync.run_pass()
        assert report.added == [] and report.errors == []
        assert list(s._get_sequence_data()["accounts"]) == ["1"]
