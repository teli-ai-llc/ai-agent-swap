"""Account browser workflows, including ownership, large rosters and search."""

import dataclasses
import time

import pytest
from textual.widgets import DataTable, Input, ListView, Static

from claude_swap.models import AccountsSnapshot
from claude_swap.tui.app import CswapApp
from claude_swap.tui.browser import BrowserDashboardScreen, visible_accounts
from claude_swap.tui.widgets import MenuItem
from claude_swap.tui.modals import ConfirmModal
from tests.test_tui import FakeSwitcher, make_account, settle


def roster():
    return [
        make_account(1, active=True, alias="personal"),
        dataclasses.replace(make_account(2, alias="work"),
                            pool_account_id="mine", pool_owned=True, pool_shared=True),
        dataclasses.replace(make_account(3, alias="team"), pool_account_id="borrowed", pool_shared=True),
        dataclasses.replace(make_account(4), pool_account_id="private", pool_owned=True, pool_shared=False),
    ]


def test_ownership_and_sharing_are_independent():
    snap = AccountsSnapshot("1", tuple(roster()), time.time())
    assert [a.number for a in visible_accounts(snap, "mine", "")] == ["1", "2", "4"]
    assert [a.number for a in visible_accounts(snap, "shared", "")] == ["2", "3"]
    assert [a.number for a in visible_accounts(snap, "all", " WORK ")] == ["2"]
    assert visible_accounts(snap, "mine", "team") == []


@pytest.mark.asyncio
async def test_large_roster_keeps_details_and_navigation_visible(tmp_path):
    fake = FakeSwitcher([make_account(n, active=n == 1) for n in range(1, 101)], tmp_path)
    app = CswapApp(fake)
    async with app.run_test(size=(100, 32)) as pilot:
        await settle(pilot)
        assert isinstance(app.screen, BrowserDashboardScreen)
        table = app.screen.query_one(DataTable)
        assert table.row_count == 100
        assert table.region.bottom <= app.screen.query_one("#account-detail-scroll").region.y
        assert app.screen.query_one("#account-detail-scroll").region.bottom < 32
        assert table.has_focus
        await pilot.press("down", "enter")
        await settle(pilot)
        assert ("switch_to", "2") in fake.calls
        assert isinstance(app.screen, BrowserDashboardScreen)


@pytest.mark.asyncio
async def test_scope_search_and_empty_results(tmp_path):
    app = CswapApp(FakeSwitcher(roster(), tmp_path))
    async with app.run_test(size=(100, 32)) as pilot:
        await settle(pilot)
        await pilot.click("#scope-shared")
        table = app.screen.query_one(DataTable)
        assert table.row_count == 2
        assert app.screen._selected_number == "2"
        await pilot.press("/")
        await pilot.press("t", "e", "a", "m")
        assert app.screen.query_one(Input).value == "team"
        assert table.row_count == 1
        assert app.screen._selected_number == "3"
        await pilot.press("x")
        assert table.row_count == 0
        assert "No accounts match" in app.screen.query_one("#browser-empty", Static).render().plain
        await pilot.press("escape")
        assert table.row_count == 2
        assert table.has_focus


@pytest.mark.asyncio
async def test_context_actions_respect_ownership(tmp_path):
    app = CswapApp(FakeSwitcher(roster(), tmp_path))
    async with app.run_test(size=(100, 32)) as pilot:
        await settle(pilot)
        await pilot.press("m")
        menu = app.screen.query_one(ListView)
        ids = [item.action_id for item in menu.query(MenuItem)]
        assert "pool-share:1" in ids
        assert "connection-menu" in ids
        assert "pool-menu" not in ids
        await pilot.press("escape", "down", "down", "m")
        ids = [item.action_id for item in menu.query(MenuItem)]
        assert "pool-share:3" not in ids
        assert "pool-withdraw:3" not in ids
        assert "remove:3" in ids


@pytest.mark.asyncio
async def test_selection_survives_sync_reordering_and_removal(tmp_path):
    fake = FakeSwitcher(roster(), tmp_path)
    app = CswapApp(fake)
    async with app.run_test(size=(100, 32)) as pilot:
        await settle(pilot)
        await pilot.press("down", "down")
        assert app.screen._selected_number == "3"
        fake._accounts.reverse()
        app.request_refresh()
        await settle(pilot)
        assert app.screen._selected_number == "3"
        fake._accounts = [a for a in fake._accounts if a.number != "3"]
        app.request_refresh()
        await settle(pilot)
        assert app.screen._selected_number == "1"


@pytest.mark.asyncio
async def test_delete_requires_confirmation_and_removes_selected_account(tmp_path):
    fake = FakeSwitcher(roster(), tmp_path)
    app = CswapApp(fake)
    async with app.run_test(size=(100, 32)) as pilot:
        await settle(pilot)
        await pilot.press("down", "delete")
        assert isinstance(app.screen, ConfirmModal)
        body = app.screen.query_one(".modal-body", Static).render().plain
        assert "shared pool" in body and "Withdraw" in body
        await pilot.press("n")
        assert not any(call[0] == "remove" for call in fake.calls)
        await pilot.press("delete", "y")
        await settle(pilot)
        assert ("remove", "2", True) in fake.calls
        assert app.screen.query_one(DataTable).row_count == 3


@pytest.mark.asyncio
async def test_delete_in_search_edits_text_without_removing_an_account(tmp_path):
    fake = FakeSwitcher(roster(), tmp_path)
    app = CswapApp(fake)
    async with app.run_test(size=(100, 32)) as pilot:
        await settle(pilot)
        await pilot.press("/", "w", "o", "r", "k", "home", "delete")
        assert isinstance(app.screen, BrowserDashboardScreen)
        assert app.screen.query_one(Input).value == "ork"
        assert not any(call[0] == "remove" for call in fake.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (60, 20)])
async def test_compact_terminal_remains_usable(tmp_path, size):
    app = CswapApp(FakeSwitcher(roster(), tmp_path))
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        assert app.screen.query_one(DataTable).region.height >= 3
        await pilot.press("m")
        assert app.screen.query_one(ListView).has_focus
        await pilot.press("escape", "down", "s")
        await settle(pilot)
        assert ("switch_to", "2") in app.switcher.calls


def test_snapshot_reads_pool_ownership_and_shared_flag(temp_home, mock_claude_config, monkeypatch):
    from claude_swap.switcher import ClaudeAccountSwitcher
    switcher = ClaudeAccountSwitcher()
    monkeypatch.setattr(switcher, "_read_capture_credentials", lambda: '{"claudeAiOauth":{"accessToken":"test-token"}}')
    switcher.add_account()
    switcher.set_slot_pool_info("1", "account-id", True, shared=True)
    account = switcher.accounts_snapshot(fetch=set()).accounts[0]
    assert account.is_mine and account.is_shared
    switcher.set_slot_pool_info("1", "account-id", True, shared=False)
    account = switcher.accounts_snapshot(fetch=set()).accounts[0]
    assert account.is_mine and not account.is_shared
    switcher.set_slot_pool_info("1", None, False)
    account = switcher.accounts_snapshot(fetch=set()).accounts[0]
    assert account.is_mine and account.pool_shared is None


def _usage_entry(pct5, pct7, hours7, *, age=0.0):
    from datetime import datetime, timezone

    from claude_swap.usage_store import UsageEntry

    now = time.time()
    reset = datetime.fromtimestamp(now + hours7 * 3600, tz=timezone.utc).isoformat()
    usage = {"five_hour": {"pct": pct5}, "seven_day": {"pct": pct7, "resets_at": reset}}
    return UsageEntry(last_good=usage, fetched_at=now - age, age_s=age)


def test_status_says_why_and_when_a_blocked_account_is_back():
    from claude_swap import recommend
    from claude_swap.tui.browser import account_state

    now = time.time()
    blocked = make_account(3, entry=_usage_entry(0, 95, 29.5))
    outlook = recommend.outlook(blocked.usage.last_good, None, (), now)
    assert account_state(blocked, outlook, now) == "7d limit · back in 1d 5h"
    stale = make_account(4, entry=_usage_entry(0, 10, 100, age=900))
    outlook = recommend.outlook(stale.usage.last_good, None, (), now)
    assert account_state(stale, outlook, now) == "Ready *"


def test_best_pick_marks_the_most_perishable_account_under_smart():
    from claude_swap.tui.widgets import best_pick

    snap = AccountsSnapshot(
        active_number="1",
        accounts=(
            make_account(1, active=True, entry=_usage_entry(0, 0, 160)),
            make_account(2, entry=_usage_entry(0, 60, 8)),
        ),
        taken_at=time.time(),
    )
    app = type("App", (), {"routing_strategy": "smart", "routing_models": ()})()
    assert best_pick(app, snap, time.time())[0] == "2"
    app.routing_strategy = "best"
    assert best_pick(app, snap, time.time())[0] == "1"
