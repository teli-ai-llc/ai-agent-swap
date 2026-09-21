"""Modal screens: confirmations, token entry, and captured-output display."""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static

from claude_swap.rules import (
    AccountRule,
    parse_hard_limit,
    parse_priority,
    parse_swap_limit,
)


class ConfirmModal(ModalScreen[bool]):
    """Yes/No confirmation. Dismisses with True only on explicit confirm.

    Keyboard-first: ←/→ move between the buttons (Enter presses the focused
    one), y/n answer directly, Esc cancels. Clicking still works.
    """

    BINDINGS = [
        Binding("y", "confirm", "Yes", show=False),
        Binding("n,escape", "cancel", "No", show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def __init__(
        self, message: str, *, title: str = "Confirm", yes_label: str = "Yes"
    ) -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._yes_label = yes_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Label(self._title, classes="modal-title")
            yield Static(self._message, classes="modal-body")
            with Horizontal(classes="modal-buttons"):
                yield Button(self._yes_label, id="yes")
                yield Button("Cancel", id="no")
            yield Static(
                f"← → · enter  ·  y {self._yes_label.lower()}  ·  n / esc cancel",
                classes="modal-hint",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


@dataclass
class TokenForm:
    """What the add-token modal collects."""

    token: str
    email: str | None
    slot: int | None


class AddTokenModal(ModalScreen["TokenForm | None"]):
    """Collects a setup-token/API key, optional email label, optional slot.

    ←/→ only reach the screen when a Button is focused (a focused Input
    consumes them for cursor movement), so they safely double as button
    navigation.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Label("Add account from token", classes="modal-title")
            yield Static(
                "OAuth setup-token (sk-ant-oat…) or managed API key "
                "(sk-ant-api…); the type is auto-detected.",
                classes="modal-body",
            )
            yield Input(password=True, placeholder="token (required)", id="token")
            yield Input(placeholder="email label (optional)", id="email")
            yield Input(placeholder="slot number (optional)", id="slot", type="integer")
            yield Static("", id="form-error", classes="form-error")
            with Horizontal(classes="modal-buttons"):
                yield Button("Add", id="add")
                yield Button("Cancel", id="cancel")
            yield Static(
                "enter add  ·  tab next field  ·  esc cancel",
                classes="modal-hint",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        token = self.query_one("#token", Input).value.strip()
        email = self.query_one("#email", Input).value.strip() or None
        slot_raw = self.query_one("#slot", Input).value.strip()
        if not token:
            self.query_one("#form-error", Static).update("Token is required.")
            return
        slot: int | None = None
        if slot_raw:
            try:
                slot = int(slot_raw)
            except ValueError:
                self.query_one("#form-error", Static).update(
                    "Slot must be a number."
                )
                return
            if slot < 1:
                self.query_one("#form-error", Static).update("Slot must be >= 1.")
                return
        self.dismiss(TokenForm(token=token, email=email, slot=slot))

    def action_cancel(self) -> None:
        self.dismiss(None)


@dataclass
class RuleForm:
    """What the account-rules modal collects (already validated)."""

    swap_limit: float | None
    hard_limit: float
    priority: int
    reset: bool = False


class RuleModal(ModalScreen["RuleForm | None"]):
    """Edit one account's switching rule: swap limit, hard limit, priority.

    Blank fields mean the default (the global threshold / 100 / 1); the
    "Defaults" button clears all three. Same ←/→ button navigation as the
    other forms.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def __init__(
        self,
        number: str,
        label: str,
        rule: AccountRule,
        threshold: float | None,
    ) -> None:
        super().__init__()
        self._number = number
        self._label = label
        self._rule = rule
        self._threshold = threshold

    def compose(self) -> ComposeResult:
        rule = self._rule
        default_swap = (
            f"{self._threshold:.10g}" if self._threshold is not None else "threshold"
        )
        with Vertical(classes="modal-box"):
            yield Label(
                f"Rules for account {self._number} · {self._label}",
                classes="modal-title",
            )
            yield Static(
                "swap limit: start looking for a better account at this % "
                "(blank = global threshold).\n"
                "hard limit: never use past this % — not a target, and left "
                "at once when active (blank = 100).\n"
                "priority: 1 is most preferred; ties route by usage (blank = 1).",
                classes="modal-body",
            )
            yield Input(
                value="" if rule.swap_limit is None else f"{rule.swap_limit:.10g}",
                placeholder=f"swap limit % (default {default_swap})",
                id="swap",
                type="number",
            )
            yield Input(
                value="" if rule.hard_limit >= 100.0 else f"{rule.hard_limit:.10g}",
                placeholder="hard limit % (default 100)",
                id="hard",
                type="number",
            )
            yield Input(
                value="" if rule.priority == 1 else str(rule.priority),
                placeholder="priority (default 1)",
                id="priority",
                type="integer",
            )
            yield Static("", id="form-error", classes="form-error")
            with Horizontal(classes="modal-buttons"):
                yield Button("Save", id="save")
                yield Button("Defaults", id="defaults")
                yield Button("Cancel", id="cancel")
            yield Static(
                "enter save  ·  tab next field  ·  esc cancel",
                classes="modal-hint",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "defaults":
            self.dismiss(RuleForm(None, 100.0, 1, reset=True))
        else:
            self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        try:
            swap = parse_swap_limit(self.query_one("#swap", Input).value)
            hard = parse_hard_limit(self.query_one("#hard", Input).value)
            priority = parse_priority(self.query_one("#priority", Input).value)
        except ValueError as exc:
            self.query_one("#form-error", Static).update(str(exc))
            return
        self.dismiss(RuleForm(swap, hard, priority))

    def action_cancel(self) -> None:
        self.dismiss(None)


@dataclass(frozen=True)
class PoolLoginForm:
    """What the pool login modal collects: where the pool is, who is logging
    in, and the shared pool code (a first-time email is signed up with it)."""

    url: str
    anon_key: str
    email: str
    code: str


class PoolLoginModal(ModalScreen["PoolLoginForm | None"]):
    """Collects the pool URL/anon key, a member's email and the pool code.

    The anon key and the code use ``password=True`` so neither is
    shoulder-surfable. Same ←/→ button navigation as the other forms.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def __init__(self, url: str | None, anon_key: str | None) -> None:
        super().__init__()
        self._url = url or ""
        self._anon_key = anon_key or ""

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Label("Log in to the pool", classes="modal-title")
            yield Static(
                "Sign in with your work email and the team's pool code. "
                "A first login creates your membership.",
                classes="modal-body",
            )
            yield Input(value=self._url, placeholder="pool URL", id="url")
            yield Input(
                value=self._anon_key,
                password=True,
                placeholder="anon key",
                id="anon-key",
            )
            yield Input(placeholder="email", id="email")
            yield Input(password=True, placeholder="pool code", id="code")
            yield Static("", id="form-error", classes="form-error")
            with Horizontal(classes="modal-buttons"):
                yield Button("Log in", id="login")
                yield Button("Cancel", id="cancel")
            yield Static(
                "enter log in  ·  tab next field  ·  esc cancel",
                classes="modal-hint",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        url = self.query_one("#url", Input).value.strip().rstrip("/")
        anon_key = self.query_one("#anon-key", Input).value.strip()
        email = self.query_one("#email", Input).value.strip().lower()
        code = self.query_one("#code", Input).value
        if not (url and anon_key and email) or not code.strip():
            self.query_one("#form-error", Static).update(
                "URL, anon key, email, and pool code are all required."
            )
            return
        self.dismiss(PoolLoginForm(url=url, anon_key=anon_key, email=email, code=code))

    def action_cancel(self) -> None:
        self.dismiss(None)


@dataclass(frozen=True)
class PoolShareForm:
    """What the pool share modal collects (already validated)."""

    shared: bool
    swap_limit: float | None
    hard_limit: float | None


class PoolShareModal(ModalScreen["PoolShareForm | None"]):
    """Publish/withdraw an account and set its pool-visible limits.

    Blank swap/hard limit fields mean none. Same ←/→ button navigation as
    the other forms.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def __init__(
        self, number: str, label: str, current: PoolShareForm | None
    ) -> None:
        super().__init__()
        self._number = number
        self._label = label
        self._current = current

    def compose(self) -> ComposeResult:
        current = self._current
        shared = current.shared if current is not None else True
        swap_value = (
            ""
            if current is None or current.swap_limit is None
            else f"{current.swap_limit:.10g}"
        )
        hard_value = (
            ""
            if current is None or current.hard_limit is None
            else f"{current.hard_limit:.10g}"
        )
        with Vertical(classes="modal-box"):
            yield Label(
                f"Share account {self._number} · {self._label}",
                classes="modal-title",
            )
            yield Static(
                "Publish this account to the pool so other members can "
                "borrow it.\n"
                "swap limit: pool members stop routing to it past this % "
                "(blank = none).\n"
                "hard limit: pool members may not use it past this % "
                "(blank = none).",
                classes="modal-body",
            )
            yield Checkbox("Shared with the pool", value=shared, id="shared")
            yield Input(
                value=swap_value,
                placeholder="swap limit % (blank = none)",
                id="swap",
            )
            yield Input(
                value=hard_value,
                placeholder="hard limit % (blank = none)",
                id="hard",
            )
            yield Static("", id="form-error", classes="form-error")
            with Horizontal(classes="modal-buttons"):
                yield Button("Publish", id="publish")
                yield Button("Cancel", id="cancel")
            yield Static(
                "enter publish  ·  tab next field  ·  esc cancel",
                classes="modal-hint",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        shared = self.query_one("#shared", Checkbox).value
        swap_raw = self.query_one("#swap", Input).value
        hard_raw = self.query_one("#hard", Input).value
        try:
            swap_limit = parse_swap_limit(swap_raw)
            # parse_hard_limit() itself maps blank/"off"/"default"/"none" to
            # 100.0 (its own default), but here that spelling means "no pool
            # hard limit at all" -> None, matching parse_swap_limit's blank
            # handling and PoolShareForm.hard_limit's float | None contract.
            hard_norm = hard_raw.strip().lower()
            hard_limit = (
                None
                if hard_norm in ("", "off", "default", "none")
                else parse_hard_limit(hard_raw)
            )
            # A parsed hard limit of exactly 100 means "no limit" (matches
            # the CLI's `cswap pool share --hard-limit 100`).
            if hard_limit == 100.0:
                hard_limit = None
        except ValueError as exc:
            self.query_one("#form-error", Static).update(str(exc))
            return
        self.dismiss(
            PoolShareForm(shared=shared, swap_limit=swap_limit, hard_limit=hard_limit)
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class OutputModal(ModalScreen[None]):
    """Scrollable display of captured (ANSI-colored) action output."""

    BINDINGS = [Binding("escape,q,enter", "dismiss_modal", "Close", show=False)]

    def __init__(self, title: str, output: str) -> None:
        super().__init__()
        self._title = title
        self._output = output

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box modal-box-wide"):
            yield Label(self._title, classes="modal-title")
            with VerticalScroll(classes="modal-output"):
                yield Static(Text.from_ansi(self._output.rstrip() or "(no output)"))
            with Horizontal(classes="modal-buttons"):
                yield Button("Close", id="close")
            yield Static("esc close", classes="modal-hint")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)
