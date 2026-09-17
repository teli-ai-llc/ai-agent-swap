"""Push side of a sync pass."""

from __future__ import annotations

import json

import pytest

from claude_swap.credentials import ActiveCredentials
from claude_swap.exceptions import PoolAuthError
from claude_swap.models import Platform
from claude_swap.pool.client import PoolClient
from claude_swap.pool.session import save_session
from claude_swap.pool.sync import POOL_STATE_FILENAME, PoolSync, load_state
from claude_swap.switcher import ClaudeAccountSwitcher


def _switcher():
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


def _creds(rt: str, exp: int) -> str:
    return json.dumps({"claudeAiOauth": {"accessToken": f"a-{rt}", "refreshToken": rt, "expiresAt": exp}})


def _config(email: str, uuid: str, org: str = "") -> str:
    return json.dumps({"oauthAccount": {"emailAddress": email, "accountUuid": uuid,
                                        "organizationUuid": org, "organizationName": ""}})


def _seed(s, num, email, uuid, rt, exp, *, pool_id=None, owned=False):
    s._write_account_credentials(num, email, _creds(rt, exp))
    s._write_account_config(num, email, _config(email, uuid))
    data = s._get_sequence_data()
    data["accounts"][num] = {"email": email, "uuid": uuid, "organizationUuid": "",
                             "organizationName": "", "added": "2026-01-01T00:00:00Z"}
    data["sequence"].append(int(num))
    s._write_json(s.sequence_file, data)
    if pool_id:
        s.set_slot_pool_info(num, pool_id, owned)


def _publish_row(fake_pool, client, owner, uuid, rt, exp, shared=True):
    return client.create_account(fake_pool.session_for(owner), {
        "account_uuid": uuid, "organization_uuid": "", "email": f"{uuid}@x.io",
        "organization_name": "", "owner_user_id": owner.user_id,
        "credential": {"oauthAccount": {"accountUuid": uuid, "organizationUuid": "", "emailAddress": f"{uuid}@x.io"},
                       "claudeAiOauth": {"accessToken": f"a-{rt}", "refreshToken": rt, "expiresAt": exp}},
        "credential_version": exp, "credential_fingerprint": "sha256:seed", "shared": shared,
    })


@pytest.fixture
def sync_env(temp_home, fake_pool, owner):
    s = _switcher()
    client = PoolClient(fake_pool.base_url, fake_pool.anon_key, transport=fake_pool)
    session = fake_pool.session_for(owner)
    save_session(s.backup_dir, session)
    return s, client, session


class TestPush:
    def test_pushes_a_rotated_backup_once(self, sync_env, fake_pool, owner):
        s, client, session = sync_env
        row = _publish_row(fake_pool, client, owner, "acct-1", "rt-1", 1_000)
        _seed(s, "1", "acct-1@x.io", "acct-1", "rt-2", 2_000, pool_id=row.id, owned=True)
        sync = PoolSync(s, client, session, machine_id="11111111-1111-1111-1111-111111111111")
        report = sync.run_pass()
        assert report.pushed == ["1"] and report.errors == []
        assert fake_pool.row(row.id)["credential_version"] == 2_000
        assert fake_pool.row(row.id)["credential"]["claudeAiOauth"]["refreshToken"] == "rt-2"
        assert fake_pool.row(row.id)["updated_by_machine_id"] == "11111111-1111-1111-1111-111111111111"
        # second pass: nothing new, no PATCH
        patches_before = sum(1 for m, _ in fake_pool.calls if m == "PATCH")
        report = sync.run_pass()
        assert report.pushed == []
        assert sum(1 for m, _ in fake_pool.calls if m == "PATCH") == patches_before
        state = load_state(s.backup_dir)
        assert state["pushed"]["1"].startswith("sha256:")

    def test_losing_push_is_silent_and_recorded(self, sync_env, fake_pool, owner):
        s, client, session = sync_env
        row = _publish_row(fake_pool, client, owner, "acct-1", "rt-9", 9_000)   # server newer
        _seed(s, "1", "acct-1@x.io", "acct-1", "rt-2", 2_000, pool_id=row.id)
        sync = PoolSync(s, client, session, machine_id="11111111-1111-1111-1111-111111111111")
        report = sync.run_pass()
        assert report.pushed == [] and report.errors == []
        assert fake_pool.row(row.id)["credential_version"] == 9_000
        assert "1" in load_state(s.backup_dir)["pushed"]

    def test_active_slot_pushes_live_bytes(self, sync_env, fake_pool, owner, temp_home):
        s, client, session = sync_env
        row = _publish_row(fake_pool, client, owner, "acct-1", "rt-1", 1_000)
        _seed(s, "1", "acct-1@x.io", "acct-1", "rt-2", 2_000, pool_id=row.id, owned=True)
        # this machine is logged in as acct-1 and Claude Code rotated the live copy
        (temp_home / ".claude.json").write_text(_config("acct-1@x.io", "acct-1"))
        (temp_home / ".claude" / ".credentials.json").write_text(_creds("rt-3", 3_000))
        data = s._get_sequence_data(); data["activeAccountNumber"] = 1; s._write_json(s.sequence_file, data)
        PoolSync(s, client, session, machine_id="11111111-1111-1111-1111-111111111111").run_pass()
        assert fake_pool.row(row.id)["credential_version"] == 3_000

    def test_degraded_live_read_falls_back_to_backup(self, sync_env, fake_pool, owner, temp_home, monkeypatch):
        s, client, session = sync_env
        row = _publish_row(fake_pool, client, owner, "acct-1", "rt-1", 1_000)
        _seed(s, "1", "acct-1@x.io", "acct-1", "rt-2", 2_000, pool_id=row.id, owned=True)
        # this machine is logged in as acct-1, but the live read is degraded
        # (macOS keychain locked, plaintext fallback served) -- must not push it
        (temp_home / ".claude.json").write_text(_config("acct-1@x.io", "acct-1"))
        data = s._get_sequence_data(); data["activeAccountNumber"] = 1; s._write_json(s.sequence_file, data)
        monkeypatch.setattr(
            s, "_read_active_credentials",
            lambda: ActiveCredentials(value=_creds("rt-3", 3_000), keychain_unavailable=True, degraded=True),
        )
        PoolSync(s, client, session, machine_id="11111111-1111-1111-1111-111111111111").run_pass()
        assert fake_pool.row(row.id)["credential_version"] == 2_000

    def test_local_only_slots_are_ignored(self, sync_env, fake_pool):
        s, client, session = sync_env
        _seed(s, "1", "a@x.io", "acct-1", "rt-1", 1_000)
        report = PoolSync(s, client, session, machine_id="11111111-1111-1111-1111-111111111111").run_pass()
        assert report.pushed == [] and report.errors == []
        assert not any(m == "PATCH" for m, _ in fake_pool.calls)

    def test_unreachable_pool_skips_the_pass(self, sync_env, fake_pool, owner):
        s, client, session = sync_env
        row = _publish_row(fake_pool, client, owner, "acct-1", "rt-1", 1_000)
        _seed(s, "1", "acct-1@x.io", "acct-1", "rt-2", 2_000, pool_id=row.id)
        fake_pool.offline = True
        report = PoolSync(s, client, session, machine_id="11111111-1111-1111-1111-111111111111").run_pass()
        assert report.skipped and "unreachable" in report.skipped
        assert report.pushed == []

    def test_schema_mismatch_skips_loudly(self, sync_env, fake_pool):
        s, client, session = sync_env
        fake_pool.schema_version = "2"
        report = PoolSync(s, client, session, machine_id="11111111-1111-1111-1111-111111111111").run_pass()
        assert report.skipped and "schema" in report.skipped

    def test_refreshed_session_is_saved(self, sync_env, fake_pool, owner):
        s, client, session = sync_env
        now = [session.expires_at - 10]
        fake_pool.clock = lambda: now[0]
        client = PoolClient(fake_pool.base_url, fake_pool.anon_key, transport=fake_pool, clock=lambda: now[0])
        sync = PoolSync(s, client, session, machine_id="11111111-1111-1111-1111-111111111111", clock=lambda: now[0])
        sync.run_pass()
        from claude_swap.pool.session import load_session
        assert load_session(s.backup_dir).access_token != session.access_token

    def test_session_refused_mid_pass_skips(self, sync_env, fake_pool, owner, monkeypatch):
        s, client, session = sync_env
        row = _publish_row(fake_pool, client, owner, "acct-1", "rt-1", 1_000)
        _seed(s, "1", "acct-1@x.io", "acct-1", "rt-2", 2_000, pool_id=row.id, owned=True)
        monkeypatch.setattr(
            client, "push_credential",
            lambda *a, **k: (_ for _ in ()).throw(PoolAuthError("JWT revoked")),
        )
        sync = PoolSync(s, client, session, machine_id="11111111-1111-1111-1111-111111111111")
        report = sync.run_pass()
        assert report.skipped and "cswap pool login" in report.skipped
        assert report.pushed == []
        assert load_state(s.backup_dir)["lastPassAt"] is not None
