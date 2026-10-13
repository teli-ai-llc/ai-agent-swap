import json
import sys
from unittest.mock import MagicMock

import pytest

from claude_swap import cli
from claude_swap.account_catalog import account_catalog


def payload():
    return {"schemaVersion": 1, "activeAccountNumber": 1, "accounts": [
        {"number": n, "email": f"user{n}@example.com", "alias": "work" if n == 2 else "",
         "active": n == 1, "usageStatus": "ok", "usage": {"fiveHour": {"pct": 20}}}
        for n in range(1, 5)
    ]}


RECORDS = {
    "2": {"poolAccountId": "owned", "poolOwned": True, "poolShared": True},
    "3": {"poolAccountId": "team", "poolOwned": False, "poolShared": True},
    "4": {"poolAccountId": "private", "poolOwned": True, "poolShared": False},
}


def test_filters_preserve_active_identity_and_do_not_mutate_payload():
    original = payload()
    filtered = account_catalog(original, RECORDS, scope="shared")
    assert [row["number"] for row in filtered["accounts"]] == [2, 3]
    assert filtered["activeAccountNumber"] == 1
    assert filtered["accounts"][0]["ownership"] == "mine"
    assert filtered["accounts"][1]["ownership"] == "team"
    assert "ownership" not in original["accounts"][1]
    mine = account_catalog(original, RECORDS, scope="mine", search=" WORK ")
    assert [row["number"] for row in mine["accounts"]] == [2]


@pytest.mark.parametrize("json_mode", [True, False])
def test_cli_scope_and_compact_listing(monkeypatch, capsys, json_mode):
    switcher = MagicMock()
    switcher.list_accounts.return_value = payload()
    switcher._get_sequence_data.return_value = {"accounts": RECORDS}
    monkeypatch.setattr(cli, "ClaudeAccountSwitcher", lambda **kwargs: switcher)
    monkeypatch.setattr("claude_swap.update_check.check_for_update", lambda _: None)
    monkeypatch.setattr(sys, "argv", ["cswap", "list", "--scope", "shared", "--compact"] + (["--json"] if json_mode else []))
    cli.main()
    out = capsys.readouterr().out
    if json_mode:
        assert [row["number"] for row in json.loads(out)["accounts"]] == [2, 3]
    else:
        assert "user2@example.com" in out and "user3@example.com" in out
        assert "user1@example.com" not in out and "user4@example.com" not in out


def test_filters_rejected_for_switch(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["cswap", "switch", "--scope", "mine"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


@pytest.mark.parametrize("command, target", [("connect", "login"), ("disconnect", "logout"),
                                             ("sharing", "status"), ("share", "share"), ("unshare", "unshare")])
def test_integrated_sharing_commands_use_existing_pool_operations(monkeypatch, command, target):
    calls = []
    monkeypatch.setattr("claude_swap.pool.cli.pool_command", calls.append)
    tail = ["2"] if command in ("share", "unshare") else []
    monkeypatch.setattr(sys, "argv", ["cswap", command, *tail])
    cli.main()
    assert calls == [[target, *tail]]
