"""Ownership-aware account listings for the CLI and external integrations."""

from __future__ import annotations

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


def print_catalog(payload: dict) -> None:
    console = Console(highlight=False)
    table = Table(box=None, padding=(0, 1), expand=False)
    for name in ("#", "Account", "Owner", "Sharing", "5h used", "7d used", "Status"):
        table.add_column(name, overflow="ellipsis", no_wrap=True)
    for row in payload["accounts"]:
        name = row.get("alias") or row["email"]
        if row.get("alias"):
            name += f" · {row['email']}"
        usage = row.get("usage") or row.get("lastGoodUsage") or {}
        cached = not row.get("usage") and bool(row.get("lastGoodUsage"))
        percentages = []
        for window in ("fiveHour", "sevenDay"):
            pct = (usage.get(window) or {}).get("pct")
            percentages.append("—" if pct is None else f"{pct:.0f}%{' *' if cached else ''}")
        status = "Auto-switch off" if row.get("disabled") else row["usageStatus"].replace("_", " ")
        table.add_row(
            str(row["number"]), Text(f"{'● ' if row.get('active') else ''}{name}"),
            "Yours" if row["ownership"] == "mine" else "Team",
            {"shared": "Shared", "private": "Private", "unknown": "Sync pending"}[row["sharing"]],
            *percentages, status,
        )
    if payload["accounts"]:
        console.print(table)
        if any(not row.get("usage") and row.get("lastGoodUsage") for row in payload["accounts"]):
            console.print("* Last known usage; see cswap list for details.", style="dim")
    else:
        console.print("No accounts match this view.")
