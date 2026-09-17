"""Dashboard: static account overview on top, a nested action menu below.

The accounts panel is the monitor (active account full-size, others as
one-line minis); the arrow keys drive the *menu*, not the accounts. Anything
account-targeted opens a context of its own:

- ``s`` / menu "Switch account" → :class:`SwitchScreen` — every account
  full-size, Enter switches, pops back.
- "Remove account" nests into a submenu listing the accounts.

No global command palette: actions live where their context is.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, ListView, Static

from claude_swap.models import AccountsSnapshot
from claude_swap.tui.widgets import AccountItem, AccountsPanel, MenuItem

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

FLASH_S = 1.5  # how long a just-refreshed row stays highlighted

MenuEntries = list[tuple[str, str]]  # (label, action_id)

_BACK = ("← back", "back")


# Rows one full account card takes (header + 5h + 7d + one per-model row),
# the blank row between cards (AccountsPanel.render), and the scroll
# container's own padding + bottom border (cswap.tcss #accounts-scroll).
# The menu bar panel (panel/…/main.swift) sizes its popover from the same
# numbers — keep them in sync.
CARD_ROWS = 4
CARD_GAP = 1
PANEL_CHROME = 3


def accounts_panel_max_height(cards: int) -> int:
    """Height (rows) that fits ``cards`` full cards in the accounts panel."""
    cards = max(1, cards)
    return PANEL_CHROME + cards * CARD_ROWS + (cards - 1) * CARD_GAP


class DashboardScreen(Screen):
    BINDINGS = [
        Binding("s", "open_switch", "Switch accounts"),
        Binding("escape,left", "menu_back", "Back", show=False),
        Binding("q", "app.quit", "Quit"),
        # Power shortcuts; the menu is the discoverable path.
        Binding("g", "app.open_auto", "Auto view", show=False),
        Binding("f", "app.refresh_full", "Refresh usage", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        # Stack of (title, entries); depth 1 = root menu.
        self._menu_stack: list[tuple[str, MenuEntries]] = []

    def compose(self) -> ComposeResult:
        # The panel sits inside a scroll container rather than scrolling
        # itself: Textual routes the mouse wheel only to widgets with a
        # layout or children, and AccountsPanel (a Static) has neither.
        with VerticalScroll(id="accounts-scroll"):
            yield AccountsPanel(id="accounts-panel")
        yield Static("", id="menu-title")
        yield ListView(id="menu")
        yield Footer()

    async def on_mount(self) -> None:
        self._cap_accounts_panel()
        self.query_one("#menu", ListView).focus()
        await self._push_menu("menu", self._root_entries())

    def _cap_accounts_panel(self) -> None:
        """Bound the accounts area to ``app.max_account_cards`` full cards.

        Past the cap the ``#accounts-scroll`` container scrolls (mouse
        wheel) instead of growing and pushing the menu off the bottom — what
        a fixed-height host like the menu bar panel needs. Unset means the
        terminal owns the height.
        """
        cap = getattr(self.app, "max_account_cards", None)
        if not cap:
            return
        self.query_one("#accounts-scroll").styles.max_height = accounts_panel_max_height(cap)

    # -- menu plumbing --------------------------------------------------------

    def _root_entries(self) -> MenuEntries:
        # No "Refresh" entry: every view auto-refreshes, so a menu item would
        # wrongly imply the user has to. `f` stays as a hidden escape hatch.
        return [
            ("Switch account…", "switch"),
            ("Auto-switch view", "auto"),
            ("Add account…", "add-menu"),
            ("Disable / enable account…", "disable-menu"),
            ("Account rules…", "rules-menu"),
            ("Pool…", "pool-menu"),
            ("Remove account…", "remove-menu"),
            ("Theme…", "theme-menu"),
            ("Quit", "quit"),
        ]

    def _add_entries(self) -> MenuEntries:
        return [
            ("From current Claude Code login", "add-login"),
            ("From a setup-token / API key…", "add-token"),
            _BACK,
        ]

    def _remove_entries(self) -> MenuEntries:
        snap = self.app.snapshot
        entries: MenuEntries = [
            (
                f"{acc.number}  {f'{acc.alias} ({acc.email})' if acc.alias else acc.email}"
                f"  [{acc.display_tag}]",
                f"remove:{acc.number}",
            )
            for acc in (snap.accounts if snap else ())
        ]
        entries.append(_BACK)
        return entries

    def _disable_entries(self) -> MenuEntries:
        """One row per account, labelled with its current state and the action
        selecting it will take (enable a disabled one, disable an active one)."""
        snap = self.app.snapshot
        entries: MenuEntries = []
        for acc in (snap.accounts if snap else ()):
            name = f"{acc.alias} ({acc.email})" if acc.alias else acc.email
            action = "→ enable" if acc.disabled else "→ disable"
            state = "  (disabled)" if acc.disabled else ""
            entries.append(
                (f"{acc.number}  {name}{state}   {action}", f"disable:{acc.number}")
            )
        entries.append(_BACK)
        return entries

    def _rules_entries(self) -> MenuEntries:
        """One row per account with its current rule (or "defaults")."""
        snap = self.app.snapshot
        entries: MenuEntries = []
        for acc in (snap.accounts if snap else ()):
            name = f"{acc.alias} ({acc.email})" if acc.alias else acc.email
            summary = acc.rule.summary(self.app.threshold_pct) or "defaults"
            entries.append((f"{acc.number}  {name}   {summary}", f"rule:{acc.number}"))
        entries.append(_BACK)
        return entries

    def _pool_entries(self) -> MenuEntries:
        """Log-in-only submenu when this machine has no pool session; the
        full set of pool actions once it does."""
        if not self.app.pool_logged_in():
            return [("Log in…", "pool-login"), _BACK]
        return [
            ("Status", "pool-status"),
            ("Publish / share account…", "pool-share-menu"),
            ("Withdraw account…", "pool-withdraw-menu"),
            ("Sync now", "pool-sync"),
            ("Apply pooled-machine defaults", "pool-defaults"),
            ("Log out…", "pool-logout-menu"),
            _BACK,
        ]

    def _pool_share_entries(self) -> MenuEntries:
        """One row per account (same labelling as `_remove_entries`)."""
        snap = self.app.snapshot
        entries: MenuEntries = [
            (
                f"{acc.number}  {f'{acc.alias} ({acc.email})' if acc.alias else acc.email}"
                f"  [{acc.display_tag}]",
                f"pool-share:{acc.number}",
            )
            for acc in (snap.accounts if snap else ())
        ]
        entries.append(_BACK)
        return entries

    def _pool_withdraw_entries(self) -> MenuEntries:
        """Only accounts this machine's member owns in the pool.

        A switcher without ``slot_pool_info`` (older fakes, or a test that
        never touches the pool) is tolerated: it just yields no owned rows.
        """
        snap = self.app.snapshot
        info = getattr(self.app.switcher, "slot_pool_info", None)
        entries: MenuEntries = []
        if info is not None:
            for acc in (snap.accounts if snap else ()):
                account_id, owned = info(acc.number)
                if account_id and owned:
                    name = f"{acc.alias} ({acc.email})" if acc.alias else acc.email
                    entries.append(
                        (f"{acc.number}  {name}", f"pool-withdraw:{acc.number}")
                    )
        if not entries:
            return [("(no owned pooled accounts)", "back")]
        entries.append(_BACK)
        return entries

    def _pool_logout_entries(self) -> MenuEntries:
        return [
            ("Log out, keep borrowed accounts", "pool-logout:keep"),
            ("Log out, remove borrowed accounts", "pool-logout:remove"),
            _BACK,
        ]

    def _theme_entries(self) -> MenuEntries:
        """dark / light / auto, with the active setting marked."""
        current = self.app._theme_name
        entries: MenuEntries = [
            (f"{'●' if name == current else ' '} {name}", f"theme:{name}")
            for name in ("dark", "light", "auto")
        ]
        entries.append(_BACK)
        return entries

    async def _push_menu(self, title: str, entries: MenuEntries) -> None:
        self._menu_stack.append((title, entries))
        await self._render_menu()

    async def _pop_menu(self) -> None:
        if len(self._menu_stack) > 1:
            self._menu_stack.pop()
            await self._render_menu()

    async def _pop_menu_to_root(self) -> None:
        """Unwind back to the root menu so re-entering a submenu recomputes
        its entries (e.g. after a pool login/logout changes the login state)."""
        while len(self._menu_stack) > 1:
            await self._pop_menu()

    async def _render_menu(self) -> None:
        title, entries = self._menu_stack[-1]
        crumb = " › ".join(t for t, _ in self._menu_stack)
        self.query_one("#menu-title", Static).update(crumb)
        menu = self.query_one("#menu", ListView)
        await menu.clear()
        await menu.extend(
            MenuItem(label, action_id, muted=(action_id == "back"))
            for label, action_id in entries
        )
        menu.index = 0

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, MenuItem):
            await self._dispatch(item.action_id)

    async def _dispatch(self, action_id: str) -> None:
        app = self.app
        actions: dict[str, Callable[[], None]] = {
            "switch": self.action_open_switch,
            "auto": app.action_open_auto,
            "add-login": app.action_add_current,
            "add-token": app.action_add_token,
            "quit": app.exit,
        }
        if action_id == "back":
            await self._pop_menu()
        elif action_id == "add-menu":
            await self._push_menu("add account", self._add_entries())
        elif action_id == "remove-menu":
            await self._push_menu("remove account", self._remove_entries())
        elif action_id.startswith("remove:"):
            number = action_id.split(":", 1)[1]
            snap = app.snapshot
            email = next(
                (a.email for a in (snap.accounts if snap else ()) if a.number == number),
                "?",
            )
            app.confirm_remove(number, email)
        elif action_id == "theme-menu":
            await self._push_menu("theme", self._theme_entries())
        elif action_id.startswith("theme:"):
            name = action_id.split(":", 1)[1]
            app.apply_theme(name)
            app.notify(f"Theme: {name}")
            await self._pop_menu()
        elif action_id == "disable-menu":
            await self._push_menu("disable / enable", self._disable_entries())
        elif action_id.startswith("disable:"):
            number = action_id.split(":", 1)[1]
            app.do_toggle_disabled(number)
            await self._pop_menu()
        elif action_id == "rules-menu":
            await self._push_menu("account rules", self._rules_entries())
        elif action_id.startswith("rule:"):
            number = action_id.split(":", 1)[1]
            app.open_rule_editor(number)
            await self._pop_menu()
        elif action_id == "pool-menu":
            await self._push_menu("pool", self._pool_entries())
        elif action_id == "pool-login":
            await self._pop_menu_to_root()
            app.action_pool_login()
        elif action_id == "pool-status":
            app.action_pool_status()
        elif action_id == "pool-share-menu":
            await self._push_menu("publish / share", self._pool_share_entries())
        elif action_id.startswith("pool-share:"):
            number = action_id.split(":", 1)[1]
            app.open_pool_share(number)
            await self._pop_menu()
        elif action_id == "pool-withdraw-menu":
            await self._push_menu("withdraw account", self._pool_withdraw_entries())
        elif action_id.startswith("pool-withdraw:"):
            number = action_id.split(":", 1)[1]
            snap = app.snapshot
            email = next(
                (a.email for a in (snap.accounts if snap else ()) if a.number == number),
                "?",
            )
            app.confirm_pool_withdraw(number, email)
        elif action_id == "pool-sync":
            app.action_pool_sync_now()
        elif action_id == "pool-defaults":
            app.action_pool_defaults()
        elif action_id == "pool-logout-menu":
            await self._push_menu("log out", self._pool_logout_entries())
        elif action_id == "pool-logout:keep":
            await self._pop_menu_to_root()
            app.action_pool_logout(keep=True)
        elif action_id == "pool-logout:remove":
            await self._pop_menu_to_root()
            app.action_pool_logout(keep=False)
        else:
            actions[action_id]()

    # -- actions ----------------------------------------------------------------

    def action_open_switch(self) -> None:
        if not isinstance(self.app.screen, SwitchScreen):
            self.app.push_screen(SwitchScreen())

    async def action_menu_back(self) -> None:
        await self._pop_menu()

    def action_cursor_down(self) -> None:
        self.query_one("#menu", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#menu", ListView).action_cursor_up()


class AccountListScreen(Screen):
    """Shared machinery: a live ListView of full account cards.

    Subclasses decide what the cursor does — :class:`SwitchScreen` is
    selection-first.
    """

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._numbers: list[str] = []
        self._stamps: dict[str, float | None] = {}

    def compose(self) -> ComposeResult:
        yield Static("", id="list-title")
        yield ListView(id="accounts")
        yield Footer()

    def on_mount(self) -> None:
        self.watch(self.app, "snapshot", self._on_snapshot)

    async def _on_snapshot(self, snap: AccountsSnapshot | None) -> None:
        if snap is None:
            return
        listview = self.query_one("#accounts", ListView)
        numbers = [acc.number for acc in snap.accounts]
        if numbers != self._numbers:
            first_build = not self._numbers
            previous = listview.index
            await listview.clear()
            await listview.extend(AccountItem(acc) for acc in snap.accounts)
            self._numbers = numbers
            listview.index = (
                self._index_after_build(snap, first_build, previous)
                if numbers
                else None
            )
        else:
            for item, acc in zip(listview.query(AccountItem), snap.accounts):
                item.set_account(acc)
        self._flash_updated(snap, listview)

    def _index_after_build(
        self, snap: AccountsSnapshot, first_build: bool, previous: int | None
    ) -> int | None:
        """Where the cursor lands after the list is (re)built."""
        if first_build:
            return self._active_index(snap)
        return min(previous or 0, len(snap.accounts) - 1)

    def _active_index(self, snap: AccountsSnapshot) -> int:
        return next(
            (
                i
                for i, acc in enumerate(snap.accounts)
                if acc.number == snap.active_number
            ),
            0,
        )

    def _flash_updated(self, snap: AccountsSnapshot, listview: ListView) -> None:
        """Briefly highlight rows whose stored measurement just advanced."""
        new_stamps = {acc.number: acc.usage.fetched_at for acc in snap.accounts}
        if self._stamps:
            changed = {
                num
                for num, ts in new_stamps.items()
                if ts is not None and ts != self._stamps.get(num)
            }
            for item in listview.query(AccountItem):
                if item.number in changed and not item.has_class("flash"):
                    item.add_class("flash")
                    self.set_timer(FLASH_S, partial(item.remove_class, "flash"))
        self._stamps = new_stamps

    def action_cursor_down(self) -> None:
        self.query_one("#accounts", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#accounts", ListView).action_cursor_up()


class SwitchScreen(AccountListScreen):
    """All accounts, full-size and alive: arrows pick, Enter switches."""

    BINDINGS = [
        # priority: outranks the focused ListView's own (hidden) enter binding
        # so "Switch" is visible in the footer; the action delegates right back
        # to the list cursor, so behavior is identical.
        Binding("enter", "select_highlighted", "Switch", priority=True),
        Binding("b", "app.switch_best", "Best pick"),
        Binding("escape,q,s", "back", "Back"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    def on_mount(self) -> None:
        self.query_one("#list-title", Static).update("switch to which account?")
        self.query_one("#accounts", ListView).focus()
        super().on_mount()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, AccountItem):
            self.app.do_switch(item.number)
            self.app.pop_screen()

    def action_select_highlighted(self) -> None:
        listview = self.query_one("#accounts", ListView)
        if listview.display:
            listview.action_select_cursor()

    def action_back(self) -> None:
        self.app.pop_screen()
