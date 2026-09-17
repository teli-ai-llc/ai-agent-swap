"""The one object that travels through the pool: an account's own login.

Shape: ``{"oauthAccount": {...}, "claudeAiOauth": {...}}`` — the two slices
``cswap export`` writes, merged. Nothing machine-bound ever enters it.
"""

from __future__ import annotations

import json

from claude_swap.exceptions import PoolError, TransferError
from claude_swap.oauth import credential_fingerprint
from claude_swap.transfer import _slim_config, _slim_credentials

_REQUIRED_OAUTH = ("accessToken", "refreshToken", "expiresAt")


def _parse_object(text: str, label: str) -> dict:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        raise PoolError(f"{label} is not JSON: {e}") from None
    if not isinstance(parsed, dict):
        raise PoolError(f"{label} is not a JSON object")
    return parsed


def blob_from_local(creds_text: str, config_text: str) -> dict:
    """Build the blob from a slot's stored credential and config texts."""
    creds = _parse_object(creds_text, "credential")
    config = _parse_object(config_text, "config")
    if "claudeAiOauth" not in creds:
        raise PoolError("credential has no claudeAiOauth login (API keys and setup tokens never join the pool)")
    slim_creds = _slim_credentials(creds)
    try:
        slim_config = _slim_config(config, "config")
    except TransferError as e:
        raise PoolError(str(e)) from None
    return validate_blob({**slim_config, **slim_creds})


def validate_blob(blob: object) -> dict:
    """Check the shape a pull or push must have; return the same object."""
    if not isinstance(blob, dict):
        raise PoolError("pool credential is not an object")
    account = blob.get("oauthAccount")
    oauth = blob.get("claudeAiOauth")
    if not isinstance(account, dict) or not isinstance(account.get("accountUuid"), str):
        raise PoolError("pool credential has no oauthAccount.accountUuid")
    if not isinstance(oauth, dict):
        raise PoolError("pool credential has no claudeAiOauth login")
    for key in _REQUIRED_OAUTH:
        if key not in oauth:
            raise PoolError(f"pool credential login is missing {key}")
    if not isinstance(oauth["refreshToken"], str) or not oauth["refreshToken"]:
        raise PoolError("pool credential has an empty refresh token")
    expires = oauth["expiresAt"]
    if isinstance(expires, bool) or not isinstance(expires, (int, float)) or expires <= 0:
        raise PoolError("pool credential has no numeric expiresAt")
    return blob


def blob_to_local(blob: dict) -> tuple[str, str]:
    """``(creds_text, config_text)`` for ``_write_account_credentials`` /
    ``_write_account_config``."""
    validate_blob(blob)
    creds_text = json.dumps({"claudeAiOauth": blob["claudeAiOauth"]})
    config_text = json.dumps({"oauthAccount": blob["oauthAccount"]})
    return creds_text, config_text


def blob_version(blob: dict) -> int:
    """The login's ``expiresAt`` in ms: moves forward on every rotation."""
    return int(validate_blob(blob)["claudeAiOauth"]["expiresAt"])


def blob_fingerprint(blob: dict) -> str:
    """Same value ``oauth.credential_fingerprint`` gives the stored credential."""
    creds_text, _ = blob_to_local(blob)
    fp = credential_fingerprint(creds_text)
    assert fp is not None  # validate_blob guarantees a refresh token
    return fp
