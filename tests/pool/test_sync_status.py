"""Status phase: dead lineages get reported once, and heal on a newer pull."""

from __future__ import annotations

import pytest

from claude_swap.pool.sync import PoolSync, load_state
from claude_swap.usage_store import FetchRecord
from tests.pool.conftest import _publish_row, _seed

MID = "11111111-1111-1111-1111-111111111111"


def _mark_dead(s, num, email, org=""):
    """Put the slot into the usage store's quarantined state the way the
    collectors do: strike it against its stored fingerprint."""
    from claude_swap.oauth import credential_fingerprint
    fp = credential_fingerprint(s._read_account_credentials(num, email))
    s._usage_store.record({num: FetchRecord(error="invalid_grant", struck_fp=fp)}, {num: (email, org)})
    assert s._slot_token_dead(num, email)


class TestStatus:
    def test_dead_slot_with_no_newer_server_copy_is_flagged_once(self, sync_env, fake_pool, owner, borrower):
        s, client, _ = sync_env
        session = fake_pool.session_for(borrower)
        row = _publish_row(fake_pool, client, owner, "acct-o", "rt-1", 1_000)
        _seed(s, "3", "acct-o@x.io", "acct-o", "rt-1", 1_000, pool_id=row.id)
        _mark_dead(s, "3", "acct-o@x.io")
        report = PoolSync(s, client, session, machine_id=MID).run_pass()
        assert report.flagged == ["3"]
        assert fake_pool.row(row.id)["status"] == "needs_relogin"
        assert fake_pool.row(row.id)["needs_relogin_reported_by"] == MID
        patches = sum(1 for m, _ in fake_pool.calls if m == "PATCH")
        report = PoolSync(s, client, session, machine_id=MID).run_pass()
        assert report.flagged == []
        assert sum(1 for m, _ in fake_pool.calls if m == "PATCH") == patches

    def test_dead_slot_heals_from_a_newer_server_copy(self, sync_env, fake_pool, owner, borrower):
        s, client, _ = sync_env
        session = fake_pool.session_for(borrower)
        row = _publish_row(fake_pool, client, owner, "acct-o", "rt-1", 1_000)
        _seed(s, "3", "acct-o@x.io", "acct-o", "rt-1", 1_000, pool_id=row.id)
        _mark_dead(s, "3", "acct-o@x.io")
        client.push_credential(fake_pool.session_for(owner), row.id,
                               fake_pool.row(row.id)["credential"] | {"claudeAiOauth": {
                                   "accessToken": "a2", "refreshToken": "rt-2", "expiresAt": 2_000}},
                               2_000, "sha256:2", "99999999-9999-9999-9999-999999999999")
        report = PoolSync(s, client, session, machine_id=MID).run_pass()
        assert report.pulled == ["3"] and report.healed == ["3"] and report.flagged == []
        assert not s._slot_token_dead("3", "acct-o@x.io")
        assert fake_pool.row(row.id)["status"] == "ok"

    def test_owner_sees_attention_entry(self, sync_env, fake_pool, owner, borrower):
        s, client, session = sync_env                       # owner's machine
        row = _publish_row(fake_pool, client, owner, "acct-o", "rt-1", 1_000)
        _seed(s, "3", "acct-o@x.io", "acct-o", "rt-1", 1_000, pool_id=row.id, owned=True)
        client.set_status(fake_pool.session_for(borrower), row.id, "needs_relogin", "22222222-2222-2222-2222-222222222222")
        PoolSync(s, client, session, machine_id=MID).run_pass()
        attention = load_state(s.backup_dir)["attention"]
        assert attention and attention[0]["email"] == "acct-o@x.io"
        assert attention[0]["reportedBy"] == "22222222-2222-2222-2222-222222222222"

    def test_attention_clears_after_owner_readds(self, sync_env, fake_pool, owner, borrower):
        s, client, session = sync_env
        row = _publish_row(fake_pool, client, owner, "acct-o", "rt-1", 1_000)
        _seed(s, "3", "acct-o@x.io", "acct-o", "rt-1", 1_000, pool_id=row.id, owned=True)
        client.set_status(fake_pool.session_for(borrower), row.id, "needs_relogin", "22222222-2222-2222-2222-222222222222")
        PoolSync(s, client, session, machine_id=MID).run_pass()
        # owner logs in again: the slot's backup gets a fresh lineage (what `cswap add` does)
        _seed(s, "3", "acct-o@x.io", "acct-o", "rt-3", 3_000, pool_id=row.id, owned=True)
        report = PoolSync(s, client, session, machine_id=MID).run_pass()
        assert report.pushed == ["3"]
        assert fake_pool.row(row.id)["status"] == "ok"
        assert load_state(s.backup_dir)["attention"] == []
