"""T3 integration commands: single-profile setup and automatic roster sync."""

from __future__ import annotations

import os

from claude_swap.exceptions import SessionError
from claude_swap.session import SessionManager
from claude_swap.t3_sync import provider_name


def prepare_provider(switcher, identifier: str) -> dict:
    """Prepare a profile without spending the account's refresh grant."""
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        raise SessionError("Run T3 profile preparation outside a CLAUDE_CONFIG_DIR session shell.")
    number, email, org_uuid = switcher.resolve_account(identifier)
    launcher = SessionManager(switcher)
    launcher._ensure_not_api_key(number, email)
    current = switcher._get_current_account()
    if current is not None and current == (email, org_uuid):
        # A second credential copy of the live default login would race its
        # refresh-token lineage. The default provider follows cswap switches.
        return {
            "schemaVersion": 1, "accountNumber": int(number), "email": email,
            "displayName": provider_name(email, active=True), "binaryPath": "claude",
            "configDir": None, "accountPinned": False,
        }
    directory, number, email = launcher.setup_session(identifier, share=True, refresh=False)
    return {
        "schemaVersion": 1, "accountNumber": int(number), "email": email,
        "displayName": provider_name(email),
        "binaryPath": "claude", "configDir": str(directory), "accountPinned": True,
    }


def provider_setup_text(provider: dict) -> str:
    directory = provider["configDir"] or "Leave empty (default Claude login)"
    text = (
        "In T3 Settings → Providers, add a Claude instance:\n\n"
        f"Name: {provider['displayName']}\n"
        f"Binary path: {provider['binaryPath']}\n"
        f"CLAUDE_CONFIG_DIR path: {directory}\n\n"
    )
    if provider["accountPinned"]:
        text += "This instance uses the cswap session profile for this account."
    else:
        text += (
            "This is the current default login, so it follows cswap switches. "
            "To create a provider pinned to this account, switch the default "
            "login to another account first, then prepare this account again."
        )
    return text


def command(argv: list[str], *, prog: str = "cswap t3") -> None:
    import argparse
    import contextlib
    import json
    import sys

    from claude_swap.exceptions import ClaudeSwitchError
    from claude_swap.json_output import error_envelope
    from claude_swap.switcher import ClaudeAccountSwitcher

    parser = argparse.ArgumentParser(prog=prog, description="Manage cswap account integration with T3")
    sub = parser.add_subparsers(dest="action", required=True)
    prepare = sub.add_parser("prepare", help="Prepare a profile and print T3 setup fields")
    prepare.add_argument("account", help="Account number, email or alias")
    prepare.add_argument("--json", action="store_true", help="Return setup metadata without credentials")
    for action in ("enable", "disable", "status", "sync"):
        child = sub.add_parser(action)
        child.add_argument("--json", action="store_true")
        if action in ("enable", "sync"):
            child.add_argument("--settings-path", help="T3's watched settings.json file")
        if action == "sync":
            child.add_argument("--once", action="store_true", help="Sync once instead of watching")
    args = parser.parse_args(argv)
    try:
        if args.action != "prepare":
            _sync_command(args)
            return
        # Session setup can emit diagnostic notices; reserve stdout for JSON.
        with contextlib.redirect_stdout(sys.stderr):
            provider = prepare_provider(ClaudeAccountSwitcher(), args.account)
        print(json.dumps(provider) if args.json else provider_setup_text(provider))
    except ClaudeSwitchError as error:
        if args.json:
            print(json.dumps(error_envelope(error)))
        else:
            print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130) from None


def _sync_command(args) -> None:
    import json
    import time

    from claude_swap.settings import load_t3_settings, set_setting
    from claude_swap.switcher import ClaudeAccountSwitcher
    from claude_swap.t3_sync import describe_report, status, sync_once

    switcher = ClaudeAccountSwitcher()
    if getattr(args, "settings_path", None):
        set_setting(switcher.backup_dir, "t3.settingsPath", args.settings_path)
    if args.action in ("enable", "disable"):
        set_setting(switcher.backup_dir, "t3.autoSync", str(args.action == "enable").lower())
    if args.action in ("status", "disable"):
        report = status(switcher)
        if args.json:
            print(json.dumps(report))
        else:
            print(f"Automatic T3 sync: {'on' if report['enabled'] else 'off'}")
            print(f"Settings: {report['settingsPath']}")
            print(f"Managed providers: {report.get('managedCount', 0)}")
            if report.get("lastError"):
                print(f"Last sync error: {report['lastError']}")
        return
    while True:
        report = sync_once(switcher, force=True)
        print(json.dumps(report) if args.json else describe_report(report), flush=True)
        if args.action == "enable" or args.once:
            if report.get("errors"):
                raise SystemExit(2)
            return
        time.sleep(load_t3_settings(switcher.backup_dir).interval_seconds)
