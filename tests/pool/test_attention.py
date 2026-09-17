"""Owner-facing re-login notes fed from pool_state.json."""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

from claude_swap import cli, oauth
from claude_swap.pool.sync import attention_lines, load_state, save_state
from tests.pool.conftest import _seed, _switcher


def _flag(s, email, since="2026-09-17T10:00:00Z"):
    state = load_state(s.backup_dir)
    state["attention"] = [{"email": email, "since": since, "reportedBy": "2222"}]
    save_state(s.backup_dir, state)


def test_no_state_no_lines(temp_home):
    s = _switcher()
    assert attention_lines(s.backup_dir) == []


def test_line_wording(temp_home):
    s = _switcher()
    _flag(s, "harsha@teli.ai")
    (line,) = attention_lines(s.backup_dir)
    assert line.startswith("your account harsha@teli.ai needs re-login")
    assert line.endswith("log in with Claude Code, then run: cswap add")


def test_list_prints_footer_and_json_field(temp_home, capsys):
    s = _switcher()
    _seed(s, "1", "harsha@teli.ai", "acct-h", "rt-1", 1_000)
    _flag(s, "harsha@teli.ai")
    # ``list`` fetches usage for stale/inactive accounts; stub the network
    # call so this test never makes a real request.
    with patch.object(sys, "argv", ["cswap", "list"]), \
         patch("claude_swap.oauth.try_fetch_usage_for_account",
               return_value=oauth.UsageOutcome(None)):
        cli.main()
    assert "needs re-login" in capsys.readouterr().out
    with patch.object(sys, "argv", ["cswap", "list", "--json"]), \
         patch("claude_swap.oauth.try_fetch_usage_for_account",
               return_value=oauth.UsageOutcome(None)):
        cli.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["poolAttention"][0]["email"] == "harsha@teli.ai"
