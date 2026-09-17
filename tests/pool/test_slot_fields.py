"""poolAccountId / poolOwned on slot records, and add_account's return value."""

from __future__ import annotations

import json

import pytest

from claude_swap.exceptions import AccountNotFoundError
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher


def _switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


def _seed(s: ClaudeAccountSwitcher, num: str, email: str) -> None:
    s._write_account_credentials(num, email, json.dumps({"claudeAiOauth": {
        "accessToken": "a", "refreshToken": f"r-{num}", "expiresAt": 1_000}}))
    s._write_account_config(num, email, json.dumps({"oauthAccount": {
        "emailAddress": email, "accountUuid": f"acct-{num}", "organizationUuid": ""}}))
    data = s._get_sequence_data()
    data["accounts"][num] = {"email": email, "uuid": f"acct-{num}", "organizationUuid": "",
                             "organizationName": "", "added": "2026-01-01T00:00:00Z"}
    data["sequence"].append(int(num))
    s._write_json(s.sequence_file, data)


class TestSlotPoolInfo:
    def test_absent_by_default(self, temp_home):
        s = _switcher()
        _seed(s, "1", "a@x.io")
        assert s.slot_pool_info("1") == (None, False)
        assert s.slot_pool_info("9") == (None, False)

    def test_set_and_clear(self, temp_home):
        s = _switcher()
        _seed(s, "1", "a@x.io")
        s.set_slot_pool_info("1", "row-1", True)
        assert s.slot_pool_info("1") == ("row-1", True)
        record = s._get_sequence_data()["accounts"]["1"]
        assert record["poolAccountId"] == "row-1" and record["poolOwned"] is True
        s.set_slot_pool_info("1", None, False)
        record = s._get_sequence_data()["accounts"]["1"]
        assert "poolAccountId" not in record and "poolOwned" not in record

    def test_unknown_slot_raises(self, temp_home):
        s = _switcher()
        with pytest.raises(AccountNotFoundError):
            s.set_slot_pool_info("4", "row", False)


class TestAddReturnsSlot:
    def test_add_returns_the_slot_number(self, temp_home, monkeypatch):
        (temp_home / ".claude.json").write_text(json.dumps({"oauthAccount": {
            "emailAddress": "me@x.io", "accountUuid": "acct-me", "organizationUuid": "org-me",
            "organizationName": "Me Org"}}))
        (temp_home / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "a", "refreshToken": "r", "expiresAt": 1_000}}))
        s = _switcher()
        assert s.add_account() == "1"
        assert s.add_account() == "1"          # refresh in place, same slot
