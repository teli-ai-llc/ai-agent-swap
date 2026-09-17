"""Pilot-driven tests for the pool TUI pieces.

Task 2 covers just the two modals (`PoolLoginModal`, `PoolShareModal`) and
their form dataclasses; the menu/dispatch wiring lands in Task 3.
"""

from __future__ import annotations

import pytest
from textual.widgets import Checkbox, Input, Static

from claude_swap.tui.modals import (
    PoolLoginForm,
    PoolLoginModal,
    PoolShareForm,
    PoolShareModal,
)
from tests.test_tui import FakeSwitcher, make_account, make_app

pytestmark = pytest.mark.asyncio


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
            assert screen.query_one("#password", Input).password is True

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
            screen.query_one("#email", Input).value = "member@example.com"
            screen.query_one("#password", Input).value = "hunter2"
            await pilot.click("#login")
            await pilot.pause()
            assert results == [
                PoolLoginForm(
                    url="https://pool.example.com",
                    anon_key="anon-key-123",
                    email="member@example.com",
                    password="hunter2",
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

    async def test_blank_email_or_password_refused_with_error(self, tmp_path):
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
            # email and password left blank
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
