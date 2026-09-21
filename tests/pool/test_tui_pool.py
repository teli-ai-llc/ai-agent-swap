"""Pilot-driven tests for the pool TUI pieces.

Task 2 covers just the two modals (`PoolLoginModal`, `PoolShareModal`) and
their form dataclasses; Task 3 (below `# -- menu, dispatch, actions --`)
covers the "Pool…" submenu, its dispatch, and the app actions behind it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from textual.widgets import Checkbox, Input, ListView, Static

import claude_swap.pool.cli as pool_cli
import claude_swap.pool.sync as pool_sync
from claude_swap.tui.modals import (
    ConfirmModal,
    OutputModal,
    PoolLoginForm,
    PoolLoginModal,
    PoolShareForm,
    PoolShareModal,
)
from claude_swap.tui.widgets import MenuItem
from tests.test_tui import FakeSwitcher, make_account, make_app, menu_select, settle

pytestmark = pytest.mark.asyncio


class PoolAwareFakeSwitcher(FakeSwitcher):
    """`FakeSwitcher` plus `slot_pool_info`, for the withdraw list."""

    def __init__(self, accounts, backup_dir, pool_info=None):
        super().__init__(accounts, backup_dir)
        self._pool_info = pool_info or {}

    def slot_pool_info(self, num: str) -> tuple[str | None, bool]:
        return self._pool_info.get(str(num), (None, False))


def _menu_ids(app) -> list[str]:
    menu = app.screen.query_one("#menu", ListView)
    return [item.action_id for item in menu.query(MenuItem)]


def _output_text(app) -> str:
    return app.screen.query_one(".modal-output Static").render().plain


# ---------------------------------------------------------------------------
# modals
# ---------------------------------------------------------------------------


class TestPoolLoginModal:
    async def test_prefills_url_and_anon_key(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            app.push_screen(
                PoolLoginModal("https://pool.example.com", "anon-key-123"),
                lambda _: None,
            )
            await pilot.pause()
            screen = pilot.app.screen
            assert (
                screen.query_one("#url", Input).value == "https://pool.example.com"
            )
            assert screen.query_one("#anon-key", Input).value == "anon-key-123"
            # the anon key is not shoulder-surfable
            assert screen.query_one("#anon-key", Input).password is True
            assert screen.query_one("#code", Input).password is True

    async def test_returns_form_on_valid_input(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            results: list[PoolLoginForm | None] = []
            app.push_screen(PoolLoginModal(None, None), results.append)
            await pilot.pause()
            screen = pilot.app.screen
            screen.query_one("#url", Input).value = "https://pool.example.com/"
            screen.query_one("#anon-key", Input).value = "anon-key-123"
            screen.query_one("#email", Input).value = " Member@example.com "
            screen.query_one("#code", Input).value = " sp ace "
            await pilot.click("#login")
            await pilot.pause()
            assert results == [
                PoolLoginForm(
                    url="https://pool.example.com",
                    anon_key="anon-key-123",
                    email="member@example.com",
                    code=" sp ace ",      # verbatim: the code is a password
                )
            ]

    async def test_escape_dismisses_none(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            results: list[PoolLoginForm | None] = []
            app.push_screen(
                PoolLoginModal("https://pool.example.com", "anon-key-123"),
                results.append,
            )
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert results == [None]

    async def test_centered_over_a_dimmed_backdrop(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            app.push_screen(PoolLoginModal(None, None), lambda _: None)
            await pilot.pause()
            styles = pilot.app.screen.styles
            assert styles.align_horizontal == "center"
            assert styles.align_vertical == "middle"

    async def test_blank_email_or_code_refused_with_error(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            results: list[PoolLoginForm | None] = []
            app.push_screen(
                PoolLoginModal("https://pool.example.com", "anon-key-123"),
                results.append,
            )
            await pilot.pause()
            screen = pilot.app.screen
            # email and code left blank
            await pilot.click("#login")
            await pilot.pause()
            assert isinstance(pilot.app.screen, PoolLoginModal)  # still open
            assert results == []
            error = screen.query_one("#form-error", Static).render()
            assert str(getattr(error, "plain", error)) != ""

class TestPoolShareModal:
    async def test_returns_form_for_valid_input(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            results: list[PoolShareForm | None] = []
            app.push_screen(
                PoolShareModal("1", "user1@example.com", None), results.append
            )
            await pilot.pause()
            screen = pilot.app.screen
            screen.query_one("#swap", Input).value = "80"
            screen.query_one("#hard", Input).value = "50"
            await pilot.click("#publish")
            await pilot.pause()
            assert results == [PoolShareForm(True, 80.0, 50.0)]

    async def test_escape_dismisses_none(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            results: list[PoolShareForm | None] = []
            app.push_screen(
                PoolShareModal("1", "user1@example.com", None), results.append
            )
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert results == [None]

    async def test_invalid_value_keeps_modal_open_with_error(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            results: list[PoolShareForm | None] = []
            app.push_screen(
                PoolShareModal("1", "user1@example.com", None), results.append
            )
            await pilot.pause()
            screen = pilot.app.screen
            screen.query_one("#swap", Input).value = "abc"
            await pilot.click("#publish")
            await pilot.pause()
            assert isinstance(pilot.app.screen, PoolShareModal)  # still open
            assert results == []
            error = screen.query_one("#form-error", Static).render()
            assert str(getattr(error, "plain", error)) != ""

    async def test_prefills_from_current(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            current = PoolShareForm(shared=False, swap_limit=60.0, hard_limit=None)
            app.push_screen(
                PoolShareModal("2", "user2@example.com", current), lambda _: None
            )
            await pilot.pause()
            screen = pilot.app.screen
            assert screen.query_one("#shared", Checkbox).value is False
            assert screen.query_one("#swap", Input).value == "60"
            assert screen.query_one("#hard", Input).value == ""

    async def test_blank_limits_dismiss_with_none(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            results: list[PoolShareForm | None] = []
            app.push_screen(
                PoolShareModal("1", "user1@example.com", None), results.append
            )
            await pilot.pause()
            await pilot.click("#publish")
            await pilot.pause()
            assert results == [PoolShareForm(True, None, None)]

    @pytest.mark.parametrize("spelling", ["off", "OFF", "default", "none", "None"])
    async def test_hard_limit_off_spellings_mean_none(self, tmp_path, spelling):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            results: list[PoolShareForm | None] = []
            app.push_screen(
                PoolShareModal("1", "user1@example.com", None), results.append
            )
            await pilot.pause()
            screen = pilot.app.screen
            screen.query_one("#swap", Input).value = "80"
            screen.query_one("#hard", Input).value = spelling
            await pilot.click("#publish")
            await pilot.pause()
            assert results == [PoolShareForm(True, 80.0, None)]

    async def test_off_is_typable_into_the_hard_limit_field(self, tmp_path):
        # Regression: `type="number"` on the Input silently swallowed every
        # keystroke that wasn't a digit, so "off" could never actually be
        # typed even though the field's placeholder documents it.
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            results: list[PoolShareForm | None] = []
            app.push_screen(
                PoolShareModal("1", "user1@example.com", None), results.append
            )
            await pilot.pause()
            screen = pilot.app.screen
            screen.query_one("#hard", Input).focus()
            await pilot.pause()
            for ch in "off":
                await pilot.press(ch)
            await pilot.pause()
            assert screen.query_one("#hard", Input).value == "off"
            await pilot.click("#publish")
            await pilot.pause()
            assert results == [PoolShareForm(True, None, None)]

    async def test_hard_limit_100_means_none(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            results: list[PoolShareForm | None] = []
            app.push_screen(
                PoolShareModal("1", "user1@example.com", None), results.append
            )
            await pilot.pause()
            screen = pilot.app.screen
            screen.query_one("#swap", Input).value = "80"
            screen.query_one("#hard", Input).value = "100"
            await pilot.click("#publish")
            await pilot.pause()
            assert results == [PoolShareForm(True, 80.0, None)]


# ---------------------------------------------------------------------------
# menu, dispatch, actions
# ---------------------------------------------------------------------------


class TestPoolMenu:
    async def test_root_menu_has_pool_entry(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: False)
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            assert "pool-menu" in _menu_ids(app)

    async def test_logged_out_submenu_is_login_and_back(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: False)
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pool-menu")
            assert _menu_ids(app) == ["pool-login", "back"]

    async def test_logged_in_submenu_has_seven_entries_in_order(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: True)
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pool-menu")
            assert _menu_ids(app) == [
                "pool-status",
                "pool-share-menu",
                "pool-withdraw-menu",
                "pool-sync",
                "pool-defaults",
                "pool-logout-menu",
                "back",
            ]

    async def test_pool_share_menu_lists_every_account(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: True)
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pool-menu")
            await menu_select(pilot, "pool-share-menu")
            assert _menu_ids(app) == ["pool-share:1", "pool-share:2", "back"]

    async def test_pool_withdraw_menu_lists_only_owned_slots(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: True)
        fake = PoolAwareFakeSwitcher(
            [make_account(1, active=True), make_account(2), make_account(3)],
            tmp_path,
            pool_info={
                "1": ("acct-1", True),  # owned: listed
                "2": ("acct-2", False),  # borrowed: not listed
                # 3: not pooled at all
            },
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pool-menu")
            await menu_select(pilot, "pool-withdraw-menu")
            assert _menu_ids(app) == ["pool-withdraw:1", "back"]

    async def test_pool_withdraw_menu_placeholder_when_nothing_owned(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: True)
        # Plain FakeSwitcher has no slot_pool_info at all — must be tolerated.
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pool-menu")
            await menu_select(pilot, "pool-withdraw-menu")
            assert _menu_ids(app) == ["back"]
            menu = app.screen.query_one("#menu", ListView)
            item = next(
                it for it in menu.query(MenuItem) if it.action_id == "back"
            )
            label = item.query_one(Static).render().plain
            assert "(no owned pooled accounts)" in label

    async def test_pool_logout_menu_entries(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: True)
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pool-menu")
            await menu_select(pilot, "pool-logout-menu")
            assert _menu_ids(app) == ["pool-logout:keep", "pool-logout:remove", "back"]


class TestPoolLoginAction:
    async def test_login_calls_login_pool_and_shows_output_never_the_code(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: False)
        calls = []

        def fake_login(switcher, url, anon_key, email, code):
            calls.append((url, anon_key, email, code))
            return pool_cli.LoginResult(
                email=email,
                role="member",
                report=pool_sync.PassReport(pushed=["1"]),
                suggest_strikes=False,
                guard_applied=("disableRemoteControl",),
                signed_up=True,
            )

        monkeypatch.setattr(pool_cli, "login_pool", fake_login)

        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pool-menu")
            await menu_select(pilot, "pool-login")
            assert isinstance(app.screen, PoolLoginModal)
            app.screen.query_one("#url", Input).value = "https://pool.example.com"
            app.screen.query_one("#anon-key", Input).value = "anon-key-123"
            app.screen.query_one("#email", Input).value = "member@example.com"
            app.screen.query_one("#code", Input).value = "hunter2"
            await pilot.click("#login")
            await settle(pilot)
            assert calls == [("https://pool.example.com", "anon-key-123", "member@example.com", "hunter2")]
            assert isinstance(app.screen, OutputModal)
            output = _output_text(app)
            assert "Welcome to the pool" in output
            assert "Logged in as member@example.com" in output
            assert "Remote Control disabled" in output
            assert "hunter2" not in output

    async def test_login_prefills_from_pool_settings(self, tmp_path, monkeypatch):
        import json

        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: False)
        (tmp_path / "settings.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "pool": {"url": "https://saved.example.com", "anonKey": "saved-key"},
                }
            )
        )
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pool-menu")
            await menu_select(pilot, "pool-login")
            assert isinstance(app.screen, PoolLoginModal)
            assert app.screen.query_one("#url", Input).value == "https://saved.example.com"
            assert app.screen.query_one("#anon-key", Input).value == "saved-key"

    async def test_submenu_refreshes_after_a_successful_login(self, tmp_path, monkeypatch):
        # Before a fix, the submenu's entries were computed only when it was
        # pushed, so a login that flips `pool_logged_in` from False to True
        # left the stale "Log in…"/"Back" pair showing on re-entry.
        state = {"logged_in": False}
        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: state["logged_in"])

        def fake_login(switcher, url, anon_key, email, code):
            state["logged_in"] = True
            return pool_cli.LoginResult(
                email=email,
                role="member",
                report=pool_sync.PassReport(pushed=["1"]),
                suggest_strikes=False,
            )

        monkeypatch.setattr(pool_cli, "login_pool", fake_login)

        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pool-menu")
            await menu_select(pilot, "pool-login")
            assert isinstance(app.screen, PoolLoginModal)
            app.screen.query_one("#url", Input).value = "https://pool.example.com"
            app.screen.query_one("#anon-key", Input).value = "anon-key-123"
            app.screen.query_one("#email", Input).value = "member@example.com"
            app.screen.query_one("#code", Input).value = "hunter2"
            await pilot.click("#login")
            await settle(pilot)
            assert isinstance(app.screen, OutputModal)
            await pilot.press("enter")  # close the output modal
            await settle(pilot)
            # back on the dashboard, at the root menu (not the stale "pool" one)
            assert app.screen.query_one("#menu-title", Static).render().plain == "menu"
            await menu_select(pilot, "pool-menu")
            assert _menu_ids(app) == [
                "pool-status",
                "pool-share-menu",
                "pool-withdraw-menu",
                "pool-sync",
                "pool-defaults",
                "pool-logout-menu",
                "back",
            ]


class TestPoolStatusAction:
    async def test_status_shows_output_modal_with_patched_lines(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: True)
        monkeypatch.setattr(
            pool_cli, "status_lines", lambda switcher: ["Pool: https://x  as a@b.com", "  1 owned"]
        )
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pool-menu")
            await menu_select(pilot, "pool-status")
            await settle(pilot)
            assert isinstance(app.screen, OutputModal)
            output = _output_text(app)
            assert "Pool: https://x  as a@b.com" in output
            assert "1 owned" in output


class TestPoolShareAction:
    async def test_publish_calls_publish_slot_with_form_values(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: True)
        calls = []

        class StubSync:
            client = object()
            session = object()

            def publish_slot(self, num, *, shared, swap_limit, hard_limit):
                calls.append((num, shared, swap_limit, hard_limit))
                return SimpleNamespace(
                    shared=shared,
                    email="user1@example.com",
                    share_swap_limit=swap_limit,
                    share_hard_limit=hard_limit,
                )

        monkeypatch.setattr(pool_sync, "build_sync", lambda switcher: StubSync())

        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pool-menu")
            await menu_select(pilot, "pool-share-menu")
            await menu_select(pilot, "pool-share:1")
            await settle(pilot)  # the prefill lookup runs on a worker
            assert isinstance(app.screen, PoolShareModal)
            app.screen.query_one("#swap", Input).value = "80"
            app.screen.query_one("#hard", Input).value = "50"
            await pilot.click("#publish")
            await settle(pilot)
            assert calls == [("1", True, 80.0, 50.0)]
            assert isinstance(app.screen, OutputModal)
            # One line, exactly as the CLI's `cswap pool share` prints it.
            assert _output_text(app) == "Shared user1@example.com  swap 80  hard 50"

    async def test_share_prefills_from_the_published_row(self, tmp_path, monkeypatch):
        # `open_pool_share` must look the row up on a worker thread, not the
        # UI loop: `get_account` is a real network call with a 10s timeout.
        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: True)

        class StubClient:
            def get_account(self, session, account_id):
                assert account_id == "row-1"
                return SimpleNamespace(
                    shared=False, share_swap_limit=70.0, share_hard_limit=40.0
                )

        class StubSync:
            client = StubClient()
            session = object()

        monkeypatch.setattr(pool_sync, "build_sync", lambda switcher: StubSync())

        fake = PoolAwareFakeSwitcher(
            [make_account(1, active=True)],
            tmp_path,
            pool_info={"1": ("row-1", True)},
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pool-menu")
            await menu_select(pilot, "pool-share-menu")
            await menu_select(pilot, "pool-share:1")
            await settle(pilot)
            await pilot.pause()
            await pilot.pause()
            assert isinstance(app.screen, PoolShareModal)
            assert app.screen.query_one("#shared", Checkbox).value is False
            assert app.screen.query_one("#swap", Input).value == "70"
            assert app.screen.query_one("#hard", Input).value == "40"

    async def test_share_opens_with_fresh_defaults_when_lookup_fails(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: True)

        class StubClient:
            def get_account(self, session, account_id):
                raise RuntimeError("pool unreachable")

        class StubSync:
            client = StubClient()
            session = object()

        monkeypatch.setattr(pool_sync, "build_sync", lambda switcher: StubSync())

        fake = PoolAwareFakeSwitcher(
            [make_account(1, active=True)],
            tmp_path,
            pool_info={"1": ("row-1", True)},
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pool-menu")
            await menu_select(pilot, "pool-share-menu")
            await menu_select(pilot, "pool-share:1")
            await settle(pilot)
            await pilot.pause()
            await pilot.pause()
            assert isinstance(app.screen, PoolShareModal)
            assert app.screen.query_one("#shared", Checkbox).value is True
            assert app.screen.query_one("#swap", Input).value == ""
            assert app.screen.query_one("#hard", Input).value == ""


class TestPoolWithdrawAction:
    async def test_withdraw_confirms_then_calls_withdraw_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: True)
        calls = []

        class StubSync:
            client = object()
            session = object()

            def withdraw_slot(self, num):
                calls.append(num)

        monkeypatch.setattr(pool_sync, "build_sync", lambda switcher: StubSync())

        fake = PoolAwareFakeSwitcher(
            [make_account(1, active=True)],
            tmp_path,
            pool_info={"1": ("acct-1", True)},
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pool-menu")
            await menu_select(pilot, "pool-withdraw-menu")
            await menu_select(pilot, "pool-withdraw:1")
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("y")
            await settle(pilot)
            assert calls == ["1"]

    async def test_withdraw_cancel_is_safe(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: True)
        calls = []

        class StubSync:
            client = object()
            session = object()

            def withdraw_slot(self, num):
                calls.append(num)

        monkeypatch.setattr(pool_sync, "build_sync", lambda switcher: StubSync())

        fake = PoolAwareFakeSwitcher(
            [make_account(1, active=True)],
            tmp_path,
            pool_info={"1": ("acct-1", True)},
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pool-menu")
            await menu_select(pilot, "pool-withdraw-menu")
            await menu_select(pilot, "pool-withdraw:1")
            await pilot.press("n")
            await settle(pilot)
            assert calls == []


class TestPoolSyncNowAction:
    async def test_sync_now_shows_a_notification_with_the_report_text(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: True)
        monkeypatch.setattr(
            pool_sync,
            "run_pass_quietly",
            lambda switcher, force=False: pool_sync.PassReport(pulled=["3"]),
        )
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        notified = []
        app.notify = lambda msg, *a, **kw: notified.append(msg)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pool-menu")
            await menu_select(pilot, "pool-sync")
            await settle(pilot)
            assert any("pulled 1 update" in msg for msg in notified)

    async def test_sync_now_skipped_when_not_logged_in(self, tmp_path, monkeypatch):
        # The menu opens while logged in (so "Sync now" is reachable at
        # all), but the session ends before the action itself runs.
        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: True)
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        notified = []
        app.notify = lambda msg, *a, **kw: notified.append(msg)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pool-menu")
            monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: False)
            await menu_select(pilot, "pool-sync")
            await settle(pilot)
            assert any(msg == "skipped: not logged in" for msg in notified)

    async def test_sync_now_skipped_when_the_pass_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: True)
        monkeypatch.setattr(
            pool_sync, "run_pass_quietly", lambda switcher, force=False: None
        )
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        notified = []
        app.notify = lambda msg, *a, **kw: notified.append(msg)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pool-menu")
            await menu_select(pilot, "pool-sync")
            await settle(pilot)
            assert any(
                msg == "skipped: pool disabled or the pass failed (see the log)"
                for msg in notified
            )


class TestPoolDefaultsAction:
    async def test_defaults_applies_and_notifies(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: True)
        monkeypatch.setattr(pool_cli, "apply_pooled_defaults", lambda switcher: True)
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        notified = []
        app.notify = lambda msg, *a, **kw: notified.append(msg)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pool-menu")
            await menu_select(pilot, "pool-defaults")
            await settle(pilot)
            assert any("deadTokenStrikes set to 2" in msg for msg in notified)


class TestPoolLogoutAction:
    async def test_logout_remove_confirms_then_calls_logout_pool_with_keep_false(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: True)
        calls = []

        def fake_logout(switcher, *, keep):
            calls.append(keep)
            return pool_cli.LogoutResult(removed=["1"], kept=[], unlinked=[])

        monkeypatch.setattr(pool_cli, "logout_pool", fake_logout)
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pool-menu")
            await menu_select(pilot, "pool-logout-menu")
            await menu_select(pilot, "pool-logout:remove")
            assert isinstance(app.screen, ConfirmModal)
            assert calls == []  # not called until confirmed
            await pilot.press("y")
            await settle(pilot)
            assert calls == [False]

    async def test_logout_remove_cancel_is_safe(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: True)
        calls = []

        def fake_logout(switcher, *, keep):
            calls.append(keep)
            return pool_cli.LogoutResult(removed=["1"], kept=[], unlinked=[])

        monkeypatch.setattr(pool_cli, "logout_pool", fake_logout)
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pool-menu")
            await menu_select(pilot, "pool-logout-menu")
            await menu_select(pilot, "pool-logout:remove")
            await pilot.press("n")
            await settle(pilot)
            assert calls == []

    async def test_logout_keep_calls_logout_pool_with_keep_true_no_confirm(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: True)
        calls = []

        def fake_logout(switcher, *, keep):
            calls.append(keep)
            return pool_cli.LogoutResult(removed=[], kept=[], unlinked=["1"])

        monkeypatch.setattr(pool_cli, "logout_pool", fake_logout)
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pool-menu")
            await menu_select(pilot, "pool-logout-menu")
            await menu_select(pilot, "pool-logout:keep")
            await settle(pilot)
            assert calls == [True]

    async def test_submenu_refreshes_after_a_successful_logout(self, tmp_path, monkeypatch):
        # Mirror of the login case: after a logout flips `pool_logged_in`
        # from True to False, re-entering "Pool…" must show the logged-out
        # submenu, not the stale seven-entry one.
        state = {"logged_in": True}
        monkeypatch.setattr(pool_cli, "pool_logged_in", lambda switcher: state["logged_in"])

        def fake_logout(switcher, *, keep):
            state["logged_in"] = False
            return pool_cli.LogoutResult(removed=[], kept=[], unlinked=["1"])

        monkeypatch.setattr(pool_cli, "logout_pool", fake_logout)
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "pool-menu")
            await menu_select(pilot, "pool-logout-menu")
            await menu_select(pilot, "pool-logout:keep")
            await settle(pilot)
            assert isinstance(app.screen, OutputModal)
            await pilot.press("enter")  # close the output modal
            await settle(pilot)
            assert app.screen.query_one("#menu-title", Static).render().plain == "menu"
            await menu_select(pilot, "pool-menu")
            assert _menu_ids(app) == ["pool-login", "back"]
