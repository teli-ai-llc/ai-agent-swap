"""Ownership-aware account listings for the CLI and external integrations."""

from __future__ import annotations

import time

from rich.console import Console
from rich.table import Table
from rich.text import Text


def account_catalog(payload: dict, records: dict, *, scope: str = "all", search: str = "") -> dict:
    """Add ownership metadata without altering the original list payload."""
    rows = []
    query = search.strip().casefold()
    for source in payload["accounts"]:
        row = dict(source)
        record = records.get(str(row["number"]), {})
        pooled = bool(record.get("poolAccountId"))
        mine = not pooled or bool(record.get("poolOwned"))
        shared = pooled and (not mine or record.get("poolShared") is True)
        row["ownership"] = "mine" if mine else "team"
        row["sharing"] = (
            "shared" if shared else
            "unknown" if pooled and record.get("poolShared") is None else "private"
        )
        if scope == "mine" and not mine or scope == "shared" and not shared:
            continue
        if query and query not in " ".join(str(row.get(k, "")) for k in
                                             ("number", "email", "alias", "organizationName")).casefold():
            continue
        rows.append(row)
    return {**payload, "accounts": rows}


def _usage_cell(window: dict | None, cached: bool, now: float, live: bool = True) -> str:
    """``47% · 2h 13m``: used, then the countdown to that window's reset. A
    window whose reset has passed is back at 0% until the next fetch. A dead
    or unusable login (``live`` False) keeps its last reading, no countdown."""
    if not window or window.get("pct") is None:
        return "—"
    star = " *" if cached else ""
    if not live:
        return f"{window['pct']:.0f}%{star}"
    resets = window.get("resetsAt")
    if resets:
        from claude_swap.poll_policy import parse_reset_ts
        ts = parse_reset_ts(resets)
        if ts is not None and ts <= now:
            return f"0%{star} · just reset"
    countdown = window.get("countdown")
    tail = f" · {countdown}" if countdown and countdown != "0m" else ""
    return f"{window['pct']:.0f}%{star}{tail}"


def print_catalog(payload: dict) -> None:
    console = Console(highlight=False)
    table = Table(box=None, padding=(0, 1), expand=False)
    for name in ("#", "Account", "Owner", "Sharing", "5h used", "7d used", "Status"):
        table.add_column(name, overflow="ellipsis", no_wrap=True)
    pick = payload.get("bestPick") or {}
    now = time.time()
    for row in payload["accounts"]:
        name = row.get("alias") or row["email"]
        if row.get("alias"):
            name += f" · {row['email']}"
        usage = row.get("usage") or row.get("lastGoodUsage") or {}
        cached = not row.get("usage") and bool(row.get("lastGoodUsage"))
        live = row.get("usageStatus") in ("ok", "unavailable")
        percentages = [
            _usage_cell(usage.get(window), cached, now, live)
            for window in ("fiveHour", "sevenDay")
        ]
        status = "Auto-switch off" if row.get("disabled") else row["usageStatus"].replace("_", " ")
        if pick.get("number") == row["number"]:
            status = Text("★ Best pick", style="bold green")
        table.add_row(
            str(row["number"]), Text(f"{'● ' if row.get('active') else ''}{name}"),
            "Yours" if row["ownership"] == "mine" else "Team",
            {"shared": "Shared", "private": "Private", "unknown": "Sync pending"}[row["sharing"]],
            *percentages, status,
        )
    if payload["accounts"]:
        console.print(table)
        console.print("Times are how long until each window resets.", style="dim")
        if pick:
            console.print(f"★ Best pick: {pick['email']} — {pick['reason']}", style="green")
        if any(not row.get("usage") and row.get("lastGoodUsage") for row in payload["accounts"]):
            console.print("* Last known usage; see cswap list for details.", style="dim")
    else:
        console.print("No accounts match this view.")
