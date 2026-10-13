"""Offline reconciliation tests: membership, ownership, races and failures."""

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_swap.exceptions import ConfigError, SessionError
from claude_swap.settings import T3Settings, load_t3_settings, set_setting
from claude_swap.t3_sync import (
    _entry, instance_id, is_managed, owner_key, read_state,
    reconcile_settings, run_auto_sync_quietly, sync_once,
)


@pytest.fixture
def environment(tmp_path):
    backup = tmp_path / "cswap"
    backup.mkdir()
    target = tmp_path / "t3" / "settings.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"unrelated": {"keep": True}, "providerInstances": {
        "manual": {"driver": "claudeAgent", "displayName": "My existing provider", "config": {"homePath": "/manual"}}
    }}))
    set_setting(backup, "t3.settingsPath", str(target))
    records = {"1": {"email": "personal@example.com", "uuid": "uuid-1"},
               "2": {"email": "work@example.com", "uuid": "uuid-2", "alias": "work", "poolOwned": True},
               "3": {"email": "team@example.com", "uuid": "uuid-3", "poolOwned": False}}
    sequence = backup / "sequence.json"
    sequence.write_text(json.dumps({"accounts": records}))
    switcher = SimpleNamespace(backup_dir=backup, sequence_file=sequence,
                               _get_current_account=lambda: ("personal@example.com", ""),
                               _account_kind=lambda num: "oauth", _logger=logging.getLogger("test"))
    return switcher, target, records


def prepare(num):
    return Path("/profiles") / num


def test_default_profile_preparation_never_requests_refresh(environment, monkeypatch):
    switcher, _, _ = environment
    calls = []
    class Launcher:
        def __init__(self, *args, **kwargs):
            pass
        def setup_session(self, num, *, share, refresh):
            calls.append((num, share, refresh))
            return prepare(num), num, "unused"
    monkeypatch.setattr("claude_swap.t3_sync.SessionManager", Launcher)
    sync_once(switcher, force=True)
    assert calls == [("2", True, False), ("3", True, False)]


def test_add_remove_and_repeat_are_idempotent(environment):
    switcher, target, records = environment
    owner = owner_key(switcher.backup_dir)
    first = sync_once(switcher, force=True, now=100, prepare=prepare)
    assert len(first["added"]) == 3  # default active + two pinned accounts
    assert first["errors"] == []
    settings = json.loads(target.read_text())
    assert settings["unrelated"] == {"keep": True}
    assert settings["providerInstances"]["manual"]["config"]["homePath"] == "/manual"
    entries = settings["providerInstances"]
    assert entries[f"cswap_{owner}_active"]["config"]["homePath"] == ""
    assert entries[instance_id(switcher.backup_dir, records["2"])]["displayName"] == "work@example.com"
    assert entries[f"cswap_{owner}_active"]["displayName"] == "personal@example.com · active"
    assert is_managed(entries[instance_id(switcher.backup_dir, records["3"])], owner)
    second = sync_once(switcher, force=True, now=110, prepare=prepare)
    assert second["added"] == second["updated"] == second["removed"] == []
    deleted = instance_id(switcher.backup_dir, records.pop("3"))
    switcher.sequence_file.write_text(json.dumps({"accounts": records}))
    third = sync_once(switcher, force=True, now=120, prepare=prepare)
    assert third["removed"] == [deleted]
    assert "manual" in json.loads(target.read_text())["providerInstances"]


def test_account_identity_and_name_survive_slot_and_alias_changes(environment):
    switcher, target, records = environment
    sync_once(switcher, force=True, prepare=prepare)
    key = instance_id(switcher.backup_dir, records["2"])
    records["9"] = {**records.pop("2"), "alias": "renamed"}
    switcher.sequence_file.write_text(json.dumps({"accounts": records}))
    result = sync_once(switcher, force=True, prepare=prepare)
    assert result["added"] == result["removed"] == []
    assert result["updated"] == [key]
    assert json.loads(target.read_text())["providerInstances"][key]["displayName"] == "work@example.com"


def test_current_default_is_never_prepared_as_a_second_copy(environment):
    switcher, target, records = environment
    calls = []
    def setup(num):
        calls.append(num)
        return prepare(num)
    sync_once(switcher, force=True, prepare=setup)
    assert calls == ["2", "3"]
    switcher._get_current_account = lambda: ("work@example.com", "")
    calls.clear()
    result = sync_once(switcher, force=True, prepare=setup)
    assert calls == ["1", "3"]
    assert result["activeEmail"] == "work@example.com"
    assert json.loads(target.read_text())["providerInstances"][f"cswap_{owner_key(switcher.backup_dir)}_active"]["displayName"] == "work@example.com · active"
    key = instance_id(switcher.backup_dir, records["2"])
    assert json.loads(target.read_text())["providerInstances"][key]["enabled"] is False


def test_existing_generated_names_migrate_even_for_failed_profiles(environment):
    switcher, target, records = environment
    sync_once(switcher, force=True, prepare=prepare)
    settings = json.loads(target.read_text())
    owner = owner_key(switcher.backup_dir)
    keys = [instance_id(switcher.backup_dir, records[num]) for num in ("2", "3")]
    keys.append(f"cswap_{owner}_active")
    for key in keys:
        settings["providerInstances"][key]["displayName"] = "Claude — old alias"
    target.write_text(json.dumps(settings))
    before_paths = {key: settings["providerInstances"][key]["config"]["homePath"] for key in keys}
    def setup(num):
        if num == "3":
            raise SessionError("Re-login needed")
        return prepare(num)
    report = sync_once(switcher, force=True, prepare=setup)
    entries = json.loads(target.read_text())["providerInstances"]
    assert report["added"] == report["removed"] == []
    assert entries[keys[0]]["displayName"] == "work@example.com"
    assert entries[keys[1]]["displayName"] == "team@example.com"
    assert entries[keys[1]]["enabled"] is False
    assert entries[keys[2]]["displayName"] == "personal@example.com · active"
    assert {key: entries[key]["config"]["homePath"] for key in keys} == before_paths
    assert entries["manual"]["displayName"] == "My existing provider"


def test_missing_or_bad_roster_never_removes_existing_providers(environment):
    switcher, target, _ = environment
    sync_once(switcher, force=True, prepare=prepare)
    original = target.read_text()
    switcher.sequence_file.write_text("{broken")
    with pytest.raises(ConfigError, match="roster"):
        sync_once(switcher, force=True, prepare=prepare)
    assert target.read_text() == original
    assert "lastError" in read_state(switcher.backup_dir)


def test_empty_valid_roster_removes_only_this_stores_entries(environment):
    switcher, target, _ = environment
    sync_once(switcher, force=True, prepare=prepare)
    settings = json.loads(target.read_text())
    settings["providerInstances"]["other-store"] = _entry("other", "Other", "/other")
    settings["providerInstances"]["my-duplicate"] = _entry(owner_key(switcher.backup_dir), "My duplicate", "/duplicate")
    target.write_text(json.dumps(settings))
    switcher.sequence_file.write_text(json.dumps({"accounts": {}}))
    result = sync_once(switcher, force=True, prepare=prepare)
    assert len(result["removed"]) == 3
    assert set(json.loads(target.read_text())["providerInstances"]) == {"manual", "other-store", "my-duplicate"}


def test_malformed_t3_settings_fail_before_profile_preparation(environment):
    switcher, target, _ = environment
    target.write_text("{broken")
    def setup(num):
        pytest.fail("must not prepare credentials for an invalid target")
    with pytest.raises(ConfigError, match="repair"):
        sync_once(switcher, force=True, prepare=setup)
    assert target.read_text() == "{broken"


def test_profile_failure_does_not_delete_existing_provider(environment):
    switcher, target, records = environment
    sync_once(switcher, force=True, prepare=prepare)
    def setup(num):
        if num == "2":
            raise SessionError("Re-login needed")
        return prepare(num)
    report = sync_once(switcher, force=True, prepare=setup)
    key = instance_id(switcher.backup_dir, records["2"])
    assert key not in report["removed"]
    assert json.loads(target.read_text())["providerInstances"][key]["enabled"] is False
    assert "Re-login needed" in report["errors"][0]


def test_manual_id_collision_refuses_the_whole_write(environment):
    switcher, target, records = environment
    settings = json.loads(target.read_text())
    key = instance_id(switcher.backup_dir, records["2"])
    settings["providerInstances"][key] = {"driver": "claudeAgent", "config": {"homePath": "/manual"}}
    target.write_text(json.dumps(settings))
    original = target.read_text()
    with pytest.raises(ConfigError, match="already in use"):
        sync_once(switcher, force=True, prepare=prepare)
    assert target.read_text() == original


def test_concurrent_edit_is_merged_on_retry(environment):
    switcher, target, _ = environment
    owner = owner_key(switcher.backup_dir)
    edited = False
    def edit():
        nonlocal edited
        if not edited:
            settings = json.loads(target.read_text())
            settings["concurrentEdit"] = "keep"
            target.write_text(json.dumps(settings))
            edited = True
    report = reconcile_settings(target, owner, {"cswap_test": _entry(owner, "Test", "/profiles/test")}, before_compare=edit)
    assert report["added"] == ["cswap_test"]
    assert json.loads(target.read_text())["concurrentEdit"] == "keep"


def test_custom_provider_preferences_survive_reconciliation(environment):
    switcher, target, records = environment
    sync_once(switcher, force=True, prepare=prepare)
    settings = json.loads(target.read_text())
    key = instance_id(switcher.backup_dir, records["2"])
    entry = settings["providerInstances"][key]
    entry["config"]["customModels"] = [{"id": "custom", "label": "Custom"}]
    entry["environment"].append({"name": "EXTRA", "value": "keep", "sensitive": False})
    target.write_text(json.dumps(settings))
    sync_once(switcher, force=True, prepare=prepare)
    entry = json.loads(target.read_text())["providerInstances"][key]
    assert entry["config"]["customModels"] == [{"id": "custom", "label": "Custom"}]
    assert any(v["name"] == "EXTRA" for v in entry["environment"])


def test_auto_sync_defaults_off_and_obeys_interval(environment):
    switcher, _, _ = environment
    assert load_t3_settings(switcher.backup_dir).auto_sync is False
    assert sync_once(switcher, now=100, prepare=prepare) == {"skipped": "disabled"}
    set_setting(switcher.backup_dir, "t3.autoSync", "true")
    assert "added" in sync_once(switcher, now=100, prepare=prepare)
    assert sync_once(switcher, now=120, prepare=prepare) == {"skipped": "interval"}


def test_auto_sync_errors_do_not_break_account_operations(environment):
    switcher, target, _ = environment
    set_setting(switcher.backup_dir, "t3.autoSync", "true")
    target.unlink()
    assert run_auto_sync_quietly(switcher) is None
    assert "not found" in read_state(switcher.backup_dir)["lastError"]


def test_settings_bad_types_fall_back(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"t3": {"autoSync": "false", "settingsPath": 42, "intervalSeconds": True}}))
    assert load_t3_settings(tmp_path) == T3Settings()
