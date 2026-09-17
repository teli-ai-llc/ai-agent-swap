from __future__ import annotations

import json

import pytest

from claude_swap.models import Platform
from claude_swap.pool.client import PoolClient
from claude_swap.pool.session import save_session
from claude_swap.switcher import ClaudeAccountSwitcher
from tests.pool.fake_pool import FakePool


@pytest.fixture
def fake_pool() -> FakePool:
    return FakePool()


@pytest.fixture
def owner(fake_pool):
    return fake_pool.add_member("owner@x.io", "pw-owner", role="admin", display_name="Owner")


@pytest.fixture
def borrower(fake_pool):
    return fake_pool.add_member("borrower@x.io", "pw-borrower", display_name="Borrower")


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
