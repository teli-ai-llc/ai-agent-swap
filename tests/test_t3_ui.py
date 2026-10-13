import json
from types import SimpleNamespace

import pytest
from textual.widgets import Checkbox, Input, ListView

from claude_swap.settings import load_t3_settings, set_setting
from claude_swap.tui.app import CswapApp
from claude_swap.tui.autoview import AutoScreen
from claude_swap.tui.modals import OutputModal, T3SyncModal
from claude_swap.tui.widgets import MenuItem
from tests.test_tui import FakeSwitcher, make_account, settle


@pytest.mark.asyncio
async def test_dashboard_toggle_saves_preferences_and_syncs(tmp_path, monkeypatch):
    fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
    calls = []
    def sync(switcher, **kwargs):
        settings = load_t3_settings(switcher.backup_dir)
        calls.append((settings.auto_sync, settings.settings_path))
        return {"added": ["provider"], "updated": [], "removed": [], "managedCount": 1, "errors": []}
    monkeypatch.setattr("claude_swap.t3_sync.sync_once", sync)
    app = CswapApp(fake)
    async with app.run_test(size=(100, 32)) as pilot:
        await settle(pilot)
        await pilot.press("m")
        menu = app.screen.query_one(ListView)
        menu.index = next(i for i, item in enumerate(menu.query(MenuItem)) if item.action_id == "t3-settings")
        await pilot.press("enter")
        assert isinstance(app.screen, T3SyncModal)
        assert app.screen.query_one("#t3-enabled", Checkbox).value is False
        await pilot.click("#t3-enabled")
        app.screen.query_one("#t3-path", Input).value = str(tmp_path / "t3-settings.json")
        await pilot.click("#t3-save")
        await settle(pilot)
        assert (True, str(tmp_path / "t3-settings.json")) in calls
        assert isinstance(app.screen, OutputModal)


@pytest.mark.asyncio
async def test_auto_switch_screen_exposes_t3_toggle(tmp_path, monkeypatch):
    monkeypatch.setattr(AutoScreen, "_start_engine", lambda *args, **kwargs: None)
    app = CswapApp(FakeSwitcher([make_account(1, active=True)], tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(pilot)
        await pilot.press("g")
        assert isinstance(app.screen, AutoScreen)
        await pilot.press("i")
        assert isinstance(app.screen, T3SyncModal)
        await pilot.press("escape")
        assert isinstance(app.screen, AutoScreen)
        assert not load_t3_settings(tmp_path).auto_sync


@pytest.mark.asyncio
async def test_disabling_keeps_t3_provider_entries(tmp_path):
    target = tmp_path / "t3-settings.json"
    original = '{"providerInstances":{"cswap_test":{"driver":"claudeAgent"}}}'
    target.write_text(original)
    set_setting(tmp_path, "t3.autoSync", "true")
    set_setting(tmp_path, "t3.settingsPath", str(target))
    app = CswapApp(FakeSwitcher([make_account(1, active=True)], tmp_path))
    async with app.run_test(size=(100, 32)) as pilot:
        await settle(pilot)
        app.action_t3_settings()
        await pilot.pause()
        await pilot.click("#t3-enabled")
        await pilot.click("#t3-save")
        await settle(pilot)
        assert load_t3_settings(tmp_path).auto_sync is False
        assert target.read_text() == original


def test_cli_enable_and_status_json(tmp_path, monkeypatch, capsys):
    from claude_swap.t3 import command
    fake = SimpleNamespace(backup_dir=tmp_path)
    monkeypatch.setattr("claude_swap.switcher.ClaudeAccountSwitcher", lambda: fake)
    def sync(switcher, **kwargs):
        assert load_t3_settings(switcher.backup_dir).auto_sync is True
        return {"added": ["provider"], "updated": [], "removed": [], "managedCount": 1, "errors": []}
    monkeypatch.setattr("claude_swap.t3_sync.sync_once", sync)
    command(["enable", "--settings-path", str(tmp_path / "t3-settings.json"), "--json"])
    assert json.loads(capsys.readouterr().out)["added"] == ["provider"]
    command(["status", "--json"])
    assert json.loads(capsys.readouterr().out)["enabled"] is True
    command(["disable", "--json"])
    assert json.loads(capsys.readouterr().out)["enabled"] is False
