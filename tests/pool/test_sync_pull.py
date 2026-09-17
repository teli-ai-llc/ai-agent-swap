"""Pull side of a sync pass."""

from __future__ import annotations

import json

import pytest

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
