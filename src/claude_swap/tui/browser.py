"""Account-first terminal dashboard, with one row per account."""

from __future__ import annotations

import time
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, DataTable, Footer, Input, ListView, Static

from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.tui.dashboard import DashboardScreen, MenuEntries
from claude_swap.tui.data import sentinel_label, window_pct
from claude_swap.tui.theme import Palette
from claude_swap.tui.widgets import account_card_text


def visible_accounts(
    snap: AccountsSnapshot, scope: str, search: str
) -> list[AccountSnapshot]:
    """Ownership and sharing overlap: your shared accounts belong in both."""
    query = search.strip().casefold()
    return [
        acc for acc in snap.accounts
        if (scope != "mine" or acc.is_mine)
        and (scope != "shared" or acc.is_shared)
        and (not query or query in " ".join(
            (acc.number, acc.alias, acc.email, acc.org_name)
        ).casefold())
    ]


def account_state(acc: AccountSnapshot) -> str:
    if acc.usage.sentinel:
        return sentinel_label(acc.usage.sentinel)
    if not acc.switchable:
        return "Unavailable"
    if acc.disabled:
        return "Auto-switch off"
    if acc.usage.last_good is None:
        return "Usage unknown"
    if not acc.usage.fresh(time.time()):
        return "Usage stale"
    return "Ready"


class BrowserDashboardScreen(DashboardScreen):
    BINDINGS = [
        Binding("s", "open_switch", "Switch"),
        Binding("/", "search", "Search"),
        Binding("m", "toggle_actions", "Actions"),
        Binding("delete", "remove_selected", "Remove"),
        Binding("g", "app.open_auto", "Auto-switch"),
        Binding("f", "app.refresh_full", "Refresh", show=False),
        Binding("escape", "menu_back", "Back", show=False),
        Binding("q", "app.quit", "Quit"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.scope = "all"
        self._visible: list[AccountSnapshot] = []
        self._selected_number: str | None = None
        self._row_keys: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static("cswap", id="browser-heading")
        yield Static("Loading accounts…", id="browser-summary", markup=False)
        with Horizontal(id="account-scopes"):
            yield Button("All accounts", id="scope-all", variant="primary")
            yield Button("Your accounts", id="scope-mine")
            yield Button("Shared accounts", id="scope-shared")
        yield Input(placeholder="Search name, email, organization or account number", id="account-search")
        yield DataTable(id="account-table", cursor_type="row", zebra_stripes=True,
                        cursor_foreground_priority="css", cursor_background_priority="css")
        yield Static("", id="browser-empty", markup=False)
        with VerticalScroll(id="account-detail-scroll"):
            yield Static("", id="account-detail", markup=False)
        yield Static("", id="menu-title")
        yield ListView(id="menu")
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one(DataTable)
        for name, width in (("Account", 28), ("Owner", 6), ("Sharing", 12),
                            ("5h used", 7), ("7d used", 7), ("Status", 16)):
            table.add_column(name, width=width)
        table.focus()
        self.query_one("#menu").display = False
        self.query_one("#menu-title").display = False
        await self._push_menu("actions", self._root_entries())
        self.watch(self.app, "snapshot", self._on_snapshot)
        self.watch(self.app, "refresh_status", lambda _: self._update_summary())
        self.watch(self.app, "theme", lambda _: self._on_snapshot(self.app.snapshot))
        self.call_after_refresh(table.focus)

    def _update_summary(self) -> None:
        snap = self.app.snapshot
        if snap is None:
            return
        mine = sum(acc.is_mine for acc in snap.accounts)
        shared = sum(acc.is_shared for acc in snap.accounts)
        active = next((acc for acc in snap.accounts if acc.is_active), None)
        login = (active.alias or active.email) if active else "no managed login"
        summary = f"{mine} yours · {shared} shared · Active: {login}"
        if self.app.refresh_status:
            summary += f" · {self.app.refresh_status}"
        self.query_one("#browser-summary", Static).update(summary)
        for scope, count in (("all", len(snap.accounts)), ("mine", mine), ("shared", shared)):
            name = {"all": "All", "mine": "Yours", "shared": "Shared"}[scope]
            button = self.query_one(f"#scope-{scope}", Button)
            button.label = f"{name} ({count})"
            button.variant = "primary" if scope == self.scope else "default"

    def _on_snapshot(self, snap: AccountsSnapshot | None) -> None:
        if snap is None:
            return
        self._visible = visible_accounts(snap, self.scope, self.query_one(Input).value)
        table = self.query_one(DataTable)
        keys = [acc.number for acc in self._visible]
        selected = self._selected_number
        rebuild = keys != self._row_keys
        if rebuild:
            table.clear()
            self._row_keys = keys
        palette = Palette.from_theme(self.app.current_theme)
        for acc in self._visible:
            name = f"{'●' if acc.is_active else ' '} {acc.number}  {acc.alias or acc.email}"
            if acc.alias:
                name += f" · {acc.email}"
            sharing = "Shared" if acc.is_shared else "Private"
            if acc.pool_account_id and acc.pool_owned and acc.pool_shared is None:
                sharing = "Sync pending"
            cells = (
                Text(name, style=palette.accent if acc.is_active else palette.foreground),
                "Yours" if acc.is_mine else "Team",
                sharing,
                self._usage_cell(acc, "five_hour", palette),
                self._usage_cell(acc, "seven_day", palette),
                account_state(acc),
            )
            if rebuild:
                table.add_row(*cells, key=acc.number)
            else:
                for column, value in zip(table.columns, cells):
                    table.update_cell(acc.number, column, value, update_width=True)
        if keys:
            if selected not in keys:
                selected = snap.active_number if snap.active_number in keys else keys[0]
            table.move_cursor(row=keys.index(selected))
            self._selected_number = selected
        else:
            self._selected_number = None
        empty = self.query_one("#browser-empty", Static)
        empty.display = not keys
        empty.update(
            "No accounts match this search." if self.query_one(Input).value.strip()
            else {"all": "No accounts yet. Open Actions to add an account or connect shared accounts.",
                  "mine": "You have no accounts of your own yet. Open Actions to add one.",
                  "shared": "No shared accounts yet. Open Actions to connect sharing or share one of yours."}[self.scope]
        )
        self._update_summary()
        self._update_detail()

    @staticmethod
    def _usage_cell(acc: AccountSnapshot, key: str, palette: Palette) -> Text:
        pct = None if acc.usage.sentinel else window_pct(acc.usage.last_good, key)
        if pct is None:
            return Text("—", style=palette.muted)
        return Text(f"{pct:.0f}%{' *' if not acc.usage.fresh(time.time()) else ''}", style=palette.severity(pct))

    def _selected(self) -> AccountSnapshot | None:
        return next((acc for acc in self._visible if acc.number == self._selected_number), None)

    def _update_detail(self) -> None:
        acc = self._selected()
        detail = self.query_one("#account-detail", Static)
        detail.update(account_card_text(
            acc, max(30, self.size.width - 8), threshold=self.app.threshold_pct,
            palette=Palette.from_theme(self.app.current_theme),
        ) if acc else "")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._selected_number = str(event.row_key.value)
        self._update_detail()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._selected_number = str(event.row_key.value)
        self.action_open_switch()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "account-search":
            self._on_snapshot(self.app.snapshot)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one(DataTable).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id and event.button.id.startswith("scope-"):
            self.scope = event.button.id.removeprefix("scope-")
            self._on_snapshot(self.app.snapshot)
            self.query_one(DataTable).focus()

    def _root_entries(self) -> MenuEntries:
        acc = self._selected()
        entries: MenuEntries = []
        if acc:
            entries.extend([
                ("Switch to selected account", f"select:{acc.number}"),
                ("Include in / exclude from auto-switch", f"disable:{acc.number}"),
                ("Edit switching rules", f"rule:{acc.number}"),
            ])
            if acc.kind != "api_key":
                entries.append(("Set up this account in T3…", f"t3:{acc.number}"))
            if acc.is_mine:
                entries.append(("Manage sharing for this account…", f"pool-share:{acc.number}"))
                if acc.pool_account_id:
                    entries.append(("Withdraw account from shared pool…", f"pool-withdraw:{acc.number}"))
            entries.append(("Remove selected account locally…", f"remove:{acc.number}"))
        entries.extend([
            ("Add your account…", "add-menu"),
            ("Auto-switch view", "auto"),
            ("Sharing connection & sync…", "connection-menu"),
            ("T3 account sync…", "t3-settings"),
            ("Theme…", "theme-menu"),
            ("Quit", "quit"),
        ])
        return entries

    async def _dispatch(self, action_id: str) -> None:
        if action_id.startswith("select:"):
            self.app.do_switch(action_id.split(":", 1)[1])
            await self.action_menu_back_to_accounts()
        elif action_id.startswith("t3:"):
            self.app.prepare_t3_account(action_id.split(":", 1)[1])
        elif action_id == "t3-settings":
            self.app.action_t3_settings()
        elif action_id == "connection-menu":
            await self._push_menu("sharing & sync", self._connection_entries())
        elif action_id.startswith("pool-share:") and not self.app.pool_logged_in():
            self.app.notify("Connect sharing first under Sharing connection & sync", severity="warning")
        else:
            await super()._dispatch(action_id)

    def _connection_entries(self) -> MenuEntries:
        if not self.app.pool_logged_in():
            return [("Connect shared accounts…", "pool-login"), ("← back", "back")]
        return [
            ("Connection status", "pool-status"),
            ("Sync accounts now", "pool-sync"),
            ("Apply shared-account switching defaults", "pool-defaults"),
            ("Disconnect…", "pool-logout-menu"),
            ("← back", "back"),
        ]

    def action_open_switch(self) -> None:
        acc = self._selected()
        if acc:
            self.app.do_switch(acc.number)

    def action_remove_selected(self) -> None:
        acc = self._selected()
        if acc:
            self.app.confirm_remove(acc.number, acc.email)

    def action_search(self) -> None:
        self.query_one(Input).focus()

    async def action_toggle_actions(self) -> None:
        menu = self.query_one("#menu", ListView)
        if menu.display:
            await self.action_menu_back_to_accounts()
        else:
            self._menu_stack = []
            await self._push_menu("actions", self._root_entries())
            menu.display = True
            self.query_one("#menu-title").display = True
            self.query_one("#account-detail-scroll").display = False
            menu.focus()

    async def action_menu_back_to_accounts(self) -> None:
        self.query_one("#menu").display = False
        self.query_one("#menu-title").display = False
        self.query_one("#account-detail-scroll").display = True
        self.query_one(DataTable).focus()

    async def action_menu_back(self) -> None:
        if self.query_one("#menu").display:
            if len(self._menu_stack) > 1:
                await self._pop_menu()
            else:
                await self.action_menu_back_to_accounts()
        elif self.query_one(Input).value:
            self.query_one(Input).value = ""
            self.query_one(DataTable).focus()
        else:
            self.query_one(DataTable).focus()

    def action_cursor_down(self) -> None:
        if self.query_one("#menu").display:
            super().action_cursor_down()
        else:
            self.query_one(DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        if self.query_one("#menu").display:
            super().action_cursor_up()
        else:
            self.query_one(DataTable).action_cursor_up()
