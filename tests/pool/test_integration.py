"""Against a real Supabase project. Skipped unless the environment names one.

    CSWAP_POOL_TEST_URL=https://<ref>.supabase.co \
    CSWAP_POOL_TEST_KEY=<anon key> \
    CSWAP_POOL_TEST_EMAIL=... CSWAP_POOL_TEST_PASSWORD=... \
    uv run pytest tests/pool/test_integration.py -p no:xdist

Uses a throwaway account uuid so it never touches a real pooled login, and
withdraws its row at the end.
"""

from __future__ import annotations

import os
import uuid

import pytest

from claude_swap.pool.client import PoolClient

URL = os.environ.get("CSWAP_POOL_TEST_URL")
KEY = os.environ.get("CSWAP_POOL_TEST_KEY")
EMAIL = os.environ.get("CSWAP_POOL_TEST_EMAIL")
PASSWORD = os.environ.get("CSWAP_POOL_TEST_PASSWORD")

pytestmark = pytest.mark.skipif(
    not (URL and KEY and EMAIL and PASSWORD), reason="no CSWAP_POOL_TEST_* environment"
)


def test_round_trip_against_real_project():
    client = PoolClient(URL, KEY)
    session = client.sign_in_password(EMAIL, PASSWORD)
    assert client.schema_version(session) == 1
    me = client.member(session)
    marker = f"it-{uuid.uuid4()}"
    blob = {"oauthAccount": {"accountUuid": marker, "organizationUuid": "", "emailAddress": "it@example.com"},
            "claudeAiOauth": {"accessToken": "x", "refreshToken": "r1", "expiresAt": 1_000}}
    row = client.create_account(session, {
        "account_uuid": marker, "organization_uuid": "", "email": "it@example.com",
        "owner_user_id": me["user_id"], "credential": blob, "credential_version": 1_000,
        "credential_fingerprint": "sha256:it", "shared": False,
    })
    try:
        newer = {**blob, "claudeAiOauth": {**blob["claudeAiOauth"], "refreshToken": "r2", "expiresAt": 2_000}}
        assert client.push_credential(session, row.id, newer, 2_000, "sha256:it2", str(uuid.uuid4())) is True
        assert client.push_credential(session, row.id, newer, 2_000, "sha256:it2", str(uuid.uuid4())) is False
        assert client.get_account(session, row.id).credential_version == 2_000
        client.set_status(session, row.id, "needs_relogin", str(uuid.uuid4()))
        assert client.get_account(session, row.id).status == "needs_relogin"
        assert client.push_credential(session, row.id, newer | {"claudeAiOauth": {**newer["claudeAiOauth"], "expiresAt": 3_000}},
                                      3_000, "sha256:it3", str(uuid.uuid4())) is True
        assert client.get_account(session, row.id).status == "ok"
    finally:
        client.set_status(session, row.id, "withdrawn", str(uuid.uuid4()))
