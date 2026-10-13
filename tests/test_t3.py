import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from claude_swap.exceptions import AccountNotFoundError, SessionError
from claude_swap import t3


def switcher():
    fake = MagicMock()
    fake.resolve_account.return_value = ("2", "work@example.com", "org")
    fake._get_current_account.return_value = ("personal@example.com", "")
    fake._get_sequence_data.return_value = {"accounts": {"2": {"alias": "work"}}}
    return fake


def test_preparation_reuses_session_lifecycle(monkeypatch):
    launcher = MagicMock()
    launcher.setup_session.return_value = (Path("/profiles/work"), "2", "work@example.com")
    monkeypatch.setattr(t3, "SessionManager", lambda _: launcher)
    result = t3.prepare_provider(switcher(), "work")
    launcher.setup_session.assert_called_once_with("work", share=True, refresh=False)
    assert result["configDir"] == str(Path("/profiles/work"))
    assert result["accountPinned"] is True
    assert result["displayName"] == "work@example.com"
    assert "tokens" not in json.dumps(result)


def test_default_account_does_not_duplicate_credentials(monkeypatch):
    fake = switcher()
    fake._get_current_account.return_value = ("work@example.com", "org")
    launcher = MagicMock()
    monkeypatch.setattr(t3, "SessionManager", lambda _: launcher)
    result = t3.prepare_provider(fake, "work")
    launcher.setup_session.assert_not_called()
    assert result["accountPinned"] is False and result["configDir"] is None
    assert result["displayName"] == "work@example.com · active"
    assert "follows cswap switches" in t3.provider_setup_text(result)


def test_nested_profile_refused(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/other-profile")
    with pytest.raises(SessionError, match="outside"):
        t3.prepare_provider(switcher(), "work")


def test_api_key_refusal_uses_existing_session_guard(monkeypatch):
    launcher = MagicMock()
    launcher._ensure_not_api_key.side_effect = SessionError("API-key account")
    monkeypatch.setattr(t3, "SessionManager", lambda _: launcher)
    with pytest.raises(SessionError, match="API-key"):
        t3.prepare_provider(switcher(), "work")
    launcher.setup_session.assert_not_called()


def test_json_keeps_session_notices_on_stderr(monkeypatch, capsys):
    def prepare(*args):
        print("Profile refreshed")
        return {"schemaVersion": 1, "configDir": "/profiles/work"}
    monkeypatch.setattr(t3, "prepare_provider", prepare)
    t3.command(["prepare", "work", "--json"])
    captured = capsys.readouterr()
    assert json.loads(captured.out)["configDir"] == "/profiles/work"
    assert "Profile refreshed" in captured.err


def test_json_error_contract(monkeypatch, capsys):
    def prepare(*args):
        raise AccountNotFoundError("Missing account")
    monkeypatch.setattr(t3, "prepare_provider", prepare)
    with pytest.raises(SystemExit) as error:
        t3.command(["prepare", "missing", "--json"])
    assert error.value.code == 1
    assert json.loads(capsys.readouterr().out)["error"]["message"] == "Missing account"
