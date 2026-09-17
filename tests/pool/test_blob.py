"""The credential blob that travels through the pool."""

from __future__ import annotations

import json

import pytest

from claude_swap.exceptions import PoolError
from claude_swap.oauth import credential_fingerprint
from claude_swap.pool.blob import (
    blob_fingerprint,
    blob_from_local,
    blob_to_local,
    blob_version,
    validate_blob,
)

CREDS = {
    "claudeAiOauth": {
        "accessToken": "at-1", "refreshToken": "rt-1",
        "expiresAt": 1_800_000_000_000, "scopes": ["user:inference"],
    },
    "mcpOAuth": {"some-server": {"accessToken": "never-travels"}},
    "trustedDeviceToken": "never-travels",
}
CONFIG = {
    "oauthAccount": {
        "emailAddress": "a@x.io", "accountUuid": "acct-1",
        "organizationUuid": "org-1", "organizationName": "Acme",
    },
    "userID": "machine-local", "projects": {"/x": {}},
}


def test_from_local_keeps_only_the_login():
    blob = blob_from_local(json.dumps(CREDS), json.dumps(CONFIG))
    assert set(blob) == {"oauthAccount", "claudeAiOauth"}
    assert blob["claudeAiOauth"]["refreshToken"] == "rt-1"
    assert blob["oauthAccount"]["accountUuid"] == "acct-1"
    assert "userID" not in blob["oauthAccount"]


def test_version_and_fingerprint():
    blob = blob_from_local(json.dumps(CREDS), json.dumps(CONFIG))
    assert blob_version(blob) == 1_800_000_000_000
    creds_text, _ = blob_to_local(blob)
    assert blob_fingerprint(blob) == credential_fingerprint(creds_text)


def test_to_local_round_trips_into_switcher_shapes():
    blob = blob_from_local(json.dumps(CREDS), json.dumps(CONFIG))
    creds_text, config_text = blob_to_local(blob)
    assert json.loads(creds_text) == {"claudeAiOauth": CREDS["claudeAiOauth"]}
    assert json.loads(config_text) == {"oauthAccount": CONFIG["oauthAccount"]}


@pytest.mark.parametrize("bad", [
    None, [], "text", {},
    {"oauthAccount": {}},                                       # no login
    {"claudeAiOauth": {"accessToken": "x"}},                    # no config, no refresh token
    {"oauthAccount": {"accountUuid": "a"}, "claudeAiOauth": {"refreshToken": "r"}},  # no expiresAt
    {"oauthAccount": {"accountUuid": "a"},
     "claudeAiOauth": {"refreshToken": "r", "accessToken": "a", "expiresAt": "soon"}},
])
def test_validate_rejects(bad):
    with pytest.raises(PoolError):
        validate_blob(bad)


def test_validate_accepts_and_returns():
    blob = blob_from_local(json.dumps(CREDS), json.dumps(CONFIG))
    assert validate_blob(blob) is blob


def test_from_local_rejects_api_key_and_missing_login():
    with pytest.raises(PoolError):
        blob_from_local("sk-ant-api03-raw", json.dumps(CONFIG))
    with pytest.raises(PoolError):
        blob_from_local(json.dumps({"mcpOAuth": {}}), json.dumps(CONFIG))
    with pytest.raises(PoolError):
        blob_from_local(json.dumps(CREDS), json.dumps({"projects": {}}))
