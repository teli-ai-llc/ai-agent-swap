"""PoolClient against the fake pool."""

from __future__ import annotations

import pytest

from claude_swap.exceptions import PoolAuthError, PoolError
from claude_swap.pool.client import PoolAccountRow, PoolClient

BLOB = {
    "oauthAccount": {"accountUuid": "acct-1", "organizationUuid": "org-1",
                     "emailAddress": "a@x.io", "organizationName": "Acme"},
    "claudeAiOauth": {"accessToken": "at-1", "refreshToken": "rt-1", "expiresAt": 1_000},
}


def _client(fake_pool, **kw) -> PoolClient:
    return PoolClient(fake_pool.base_url, fake_pool.anon_key, transport=fake_pool, **kw)


def _publish(client, session, owner, *, shared=True, version=1_000):
    return client.create_account(session, {
        "account_uuid": "acct-1", "organization_uuid": "org-1", "email": "a@x.io",
        "organization_name": "Acme", "owner_user_id": owner.user_id,
        "credential": BLOB, "credential_version": version,
        "credential_fingerprint": "sha256:x", "shared": shared,
        "updated_by_machine_id": "11111111-1111-1111-1111-111111111111",
    })


class TestAuth:
    def test_sign_in_password(self, fake_pool, owner):
        s = _client(fake_pool).sign_in_password("owner@x.io", "pw-owner")
        assert s.user_id == owner.user_id and s.email == "owner@x.io"
        assert s.url == fake_pool.base_url and s.anon_key == fake_pool.anon_key
        assert s.expires_at > 0

    def test_bad_password(self, fake_pool, owner):
        with pytest.raises(PoolAuthError):
            _client(fake_pool).sign_in_password("owner@x.io", "nope")

    def test_email_code_round_trip(self, fake_pool, owner):
        client = _client(fake_pool)
        client.request_email_code("owner@x.io")
        assert fake_pool.sent_codes == ["owner@x.io"]
        s = client.verify_email_code("owner@x.io", fake_pool.codes["owner@x.io"])
        assert s.user_id == owner.user_id and s.email == "owner@x.io"
        assert s.expires_at > 0

    def test_email_code_creates_a_first_time_member(self, fake_pool):
        fake_pool.signup_domains = ["x.io"]
        client = _client(fake_pool)
        client.request_email_code("New.Person@x.io")
        s = client.verify_email_code("New.Person@x.io", fake_pool.codes["new.person@x.io"])
        assert s.email == "new.person@x.io"
        assert client.member(s)["role"] == "member"

    def test_email_code_outside_allowed_domains_is_explained(self, fake_pool):
        fake_pool.signup_domains = ["x.io"]
        with pytest.raises(PoolAuthError, match="email domain"):
            _client(fake_pool).request_email_code("stranger@evil.example")
        assert fake_pool.sent_codes == []

    def test_email_code_wrong_or_stale(self, fake_pool, owner):
        client = _client(fake_pool)
        client.request_email_code("owner@x.io")
        with pytest.raises(PoolAuthError, match="expired or is invalid"):
            client.verify_email_code("owner@x.io", "000000")
        # the right code still works after a wrong guess
        client.verify_email_code("owner@x.io", fake_pool.codes["owner@x.io"])
        # ... but only once
        with pytest.raises(PoolAuthError):
            client.verify_email_code("owner@x.io", "000001")

    def test_email_code_is_whitespace_tolerant(self, fake_pool, owner):
        client = _client(fake_pool)
        client.request_email_code(" owner@x.io ")
        code = fake_pool.codes["owner@x.io"]
        client.verify_email_code("owner@x.io", f" {code[:3]} {code[3:]}\n")

    def test_mailer_rate_limit_is_a_pool_error_with_advice(self, fake_pool, owner):
        fake_pool.mailer_limited = True
        with pytest.raises(PoolError, match="rate limit") as info:
            _client(fake_pool).request_email_code("owner@x.io")
        assert not isinstance(info.value, PoolAuthError)

    def test_email_code_offline_is_pool_error(self, fake_pool, owner):
        fake_pool.offline = True
        with pytest.raises(PoolError, match="unreachable"):
            _client(fake_pool).request_email_code("owner@x.io")

    def test_ensure_fresh_refreshes_near_expiry(self, fake_pool, owner):
        now = [1_000_000.0]
        fake_pool.clock = lambda: now[0]
        client = _client(fake_pool, clock=lambda: now[0])
        s = client.sign_in_password("owner@x.io", "pw-owner")
        assert client.ensure_fresh(s) is s          # plenty of time left
        now[0] = s.expires_at - 100                  # inside the 300 s window
        fresh = client.ensure_fresh(s)
        assert fresh.access_token != s.access_token
        assert fresh.refresh_token != s.refresh_token

    def test_ensure_fresh_refused_is_auth_error(self, fake_pool, owner):
        client = _client(fake_pool)
        s = client.sign_in_password("owner@x.io", "pw-owner")
        fake_pool.refresh_tokens.clear()
        stale = s.__class__(**{**s.__dict__, "expires_at": 0.0})
        with pytest.raises(PoolAuthError):
            client.ensure_fresh(stale)

    def test_offline_is_pool_error_not_auth(self, fake_pool, owner):
        client = _client(fake_pool)
        s = client.sign_in_password("owner@x.io", "pw-owner")
        fake_pool.offline = True
        with pytest.raises(PoolError) as info:
            client.list_accounts(s)
        assert not isinstance(info.value, PoolAuthError)


class TestRest:
    def test_schema_version_and_member(self, fake_pool, owner):
        client = _client(fake_pool)
        s = fake_pool.session_for(owner)
        assert client.schema_version(s) == 1
        assert client.member(s) == {"user_id": owner.user_id, "display_name": "Owner", "role": "admin"}

    def test_register_machine(self, fake_pool, owner):
        client = _client(fake_pool)
        s = fake_pool.session_for(owner)
        client.register_machine(s, "11111111-1111-1111-1111-111111111111", "mac", "0.27.0")
        assert fake_pool.machines["11111111-1111-1111-1111-111111111111"]["user_id"] == owner.user_id

    def test_create_find_list(self, fake_pool, owner, borrower):
        client = _client(fake_pool)
        so, sb = fake_pool.session_for(owner), fake_pool.session_for(borrower)
        row = _publish(client, so, owner)
        assert isinstance(row, PoolAccountRow) and row.credential_version == 1_000
        assert client.find_account(sb, "acct-1", "org-1").id == row.id
        assert client.find_account(sb, "acct-1", "other") is None
        assert [r.id for r in client.list_accounts(sb)] == [row.id]
        # `since` excludes rows at or before the watermark
        assert client.list_accounts(sb, since=row.updated_at) == []

    def test_unshared_row_invisible_to_borrower(self, fake_pool, owner, borrower):
        client = _client(fake_pool)
        _publish(client, fake_pool.session_for(owner), owner, shared=False)
        assert client.list_accounts(fake_pool.session_for(borrower)) == []

    def test_push_is_conditional(self, fake_pool, owner, borrower):
        client = _client(fake_pool)
        so, sb = fake_pool.session_for(owner), fake_pool.session_for(borrower)
        row = _publish(client, so, owner)
        newer = {**BLOB, "claudeAiOauth": {**BLOB["claudeAiOauth"], "expiresAt": 2_000, "refreshToken": "rt-2"}}
        assert client.push_credential(sb, row.id, newer, 2_000, "sha256:y", "22222222-2222-2222-2222-222222222222") is True
        assert fake_pool.row(row.id)["credential_version"] == 2_000
        assert fake_pool.row(row.id)["updated_by_user_id"] == borrower.user_id
        # same or older version: nothing lands, no error
        assert client.push_credential(sb, row.id, newer, 2_000, "sha256:y", "22222222-2222-2222-2222-222222222222") is False
        assert client.push_credential(sb, row.id, BLOB, 1_000, "sha256:x", "22222222-2222-2222-2222-222222222222") is False
        assert fake_pool.row(row.id)["credential_version"] == 2_000

    def test_push_heals_needs_relogin(self, fake_pool, owner, borrower):
        client = _client(fake_pool)
        so, sb = fake_pool.session_for(owner), fake_pool.session_for(borrower)
        row = _publish(client, so, owner)
        client.set_status(sb, row.id, "needs_relogin", "22222222-2222-2222-2222-222222222222")
        assert fake_pool.row(row.id)["status"] == "needs_relogin"
        assert fake_pool.row(row.id)["needs_relogin_reported_by"] == "22222222-2222-2222-2222-222222222222"
        client.push_credential(so, row.id, BLOB, 3_000, "sha256:z", "11111111-1111-1111-1111-111111111111")
        assert fake_pool.row(row.id)["status"] == "ok"
        assert fake_pool.row(row.id)["needs_relogin_since"] is None

    def test_non_owner_newer_push_also_heals(self, fake_pool, owner, borrower):
        # Controller ruling: the guard's owner-only checks run against the
        # status the client sent, THEN the version-heal applies. So a
        # non-owner PATCH carrying only credential columns with a higher
        # version on a needs_relogin row succeeds and heals it, even when
        # the borrower itself is the one who flagged it.
        client = _client(fake_pool)
        so, sb = fake_pool.session_for(owner), fake_pool.session_for(borrower)
        row = _publish(client, so, owner)
        client.set_status(sb, row.id, "needs_relogin", "22222222-2222-2222-2222-222222222222")
        assert fake_pool.row(row.id)["status"] == "needs_relogin"
        pushed = client.push_credential(sb, row.id, BLOB, 3_000, "sha256:z", "22222222-2222-2222-2222-222222222222")
        assert pushed is True
        assert fake_pool.row(row.id)["status"] == "ok"
        assert fake_pool.row(row.id)["needs_relogin_since"] is None

    def test_sharing_is_owner_only(self, fake_pool, owner, borrower):
        client = _client(fake_pool)
        so, sb = fake_pool.session_for(owner), fake_pool.session_for(borrower)
        row = _publish(client, so, owner)
        client.update_sharing(so, row.id, shared=True, swap_limit=80.0, hard_limit=50.0)
        assert fake_pool.row(row.id)["share_hard_limit"] == 50.0
        with pytest.raises(PoolError):
            client.update_sharing(sb, row.id, shared=False, swap_limit=None, hard_limit=None)
        with pytest.raises(PoolError):
            client.set_status(sb, row.id, "withdrawn", "22222222-2222-2222-2222-222222222222")
        client.set_status(so, row.id, "withdrawn", "11111111-1111-1111-1111-111111111111")
        assert fake_pool.row(row.id)["status"] == "withdrawn"

    def test_expired_jwt_is_auth_error(self, fake_pool, owner):
        client = _client(fake_pool)
        s = fake_pool.session_for(owner)
        fake_pool.tokens.clear()
        with pytest.raises(PoolAuthError):
            client.list_accounts(s)

    def test_row_parsing_tolerates_missing_optionals(self):
        row = PoolAccountRow.from_json({
            "id": "i", "account_uuid": "a", "email": "e", "owner_user_id": "o",
            "credential_version": "5", "status": "ok", "shared": True, "updated_at": "t",
        })
        assert row.organization_uuid == "" and row.credential is None
        assert row.credential_version == 5 and row.share_hard_limit is None


def test_sign_in_refusal_names_the_gotrue_msg_envelope():
    """Current GoTrue answers 400 with ``msg`` + ``error_code``; the refusal
    must carry that text, not a bare "refused"."""
    def transport(method, url, headers, body):
        return 400, b'{"code":400,"error_code":"email_not_confirmed","msg":"Email not confirmed"}'
    client = PoolClient("https://x.supabase.co", "anon", transport=transport)
    with pytest.raises(PoolAuthError, match="Email not confirmed"):
        client.sign_in_password("a@x.io", "pw")
