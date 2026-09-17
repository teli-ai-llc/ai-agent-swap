"""`cswap pool …` and `cswap sync …` handlers (pre-dispatched from cli.main)."""

from __future__ import annotations

import argparse
import getpass
import json
import platform
import sys
import time

from claude_swap import __version__
from claude_swap.exceptions import ClaudeSwitchError, PoolAuthError, PoolError
from claude_swap.pool.client import PoolClient
from claude_swap.pool.session import clear_session, load_session, machine_id, save_session
from claude_swap.pool.sync import PassReport, PoolSync, build_sync, load_state
from claude_swap.printer import accent, bolded, dimmed, error as print_error, warning as print_warning
from claude_swap.settings import load_pool_settings, set_setting
from claude_swap.switcher import ClaudeAccountSwitcher


def _prog() -> str:
    return "cswap"


def describe_report(report: PassReport) -> str:
    if report.skipped:
        return f"skipped: {report.skipped}"
    parts = []
    if report.pushed:
        parts.append(f"pushed {len(report.pushed)}")
    if report.pulled:
        parts.append(f"pulled {len(report.pulled)} update(s)")
    if report.added:
        parts.append(f"pulled {len(report.added)} account(s)")
    if report.removed:
        parts.append(f"removed {len(report.removed)}")
    if report.flagged:
        parts.append(f"flagged {len(report.flagged)} for re-login")
    if report.healed:
        parts.append(f"healed {len(report.healed)}")
    if report.errors:
        parts.append(f"{len(report.errors)} error(s)")
    return ", ".join(parts) or "nothing to do"


def _print_report(report: PassReport) -> None:
    print(f"Sync: {describe_report(report)}")
    for err in report.errors:
        print_warning(f"  {err}")


# -- pool ------------------------------------------------------------------------
def pool_command(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog=f"{_prog()} pool",
        description="Team pool: share Claude logins with teammates through a Supabase project.",
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    p_login = sub.add_parser("login", help="Sign in to the pool on this machine")
    p_login.add_argument("--url", help="Supabase project URL (saved to settings)")
    p_login.add_argument("--anon-key", help="Supabase anon key (saved to settings)")
    p_login.add_argument("--email")

    p_logout = sub.add_parser("logout", help="Sign out; removes borrowed accounts unless --keep")
    p_logout.add_argument("--keep", action="store_true", help="Keep borrowed slots on this machine")

    p_status = sub.add_parser("status", help="Who you are, what is linked, what needs attention")
    p_status.add_argument("--json", action="store_true")

    p_share = sub.add_parser("share", help="Publish an account, or change its sharing")
    p_share.add_argument("account", metavar="NUM|EMAIL")
    p_share.add_argument("--swap-limit", metavar="PCT", help="1-100 or 'off'")
    p_share.add_argument("--hard-limit", metavar="PCT", help="1-100 or 'off'")
    p_share.add_argument("--private", action="store_true", help="Publish for your own machines only")

    p_unshare = sub.add_parser("unshare", help="Withdraw an account from the pool")
    p_unshare.add_argument("account", metavar="NUM|EMAIL")

    for p in (p_login, p_logout, p_status, p_share, p_unshare):
        p.add_argument("--debug", action="store_true")

    args = parser.parse_args(argv)
    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        handler = {
            "login": _login, "logout": _logout, "status": _status,
            "share": _share, "unshare": _unshare,
        }[args.verb]
        handler(switcher, args)
    except ClaudeSwitchError as e:
        print_error(str(e))
        sys.exit(1)


def _login(switcher: ClaudeAccountSwitcher, args) -> None:
    settings = load_pool_settings(switcher.backup_dir)
    url = (args.url or settings.url or input("Pool URL (https://<ref>.supabase.co): ")).strip().rstrip("/")
    anon_key = (args.anon_key or settings.anon_key or input("Anon key: ")).strip()
    email = (args.email or input("Email: ")).strip()
    password = getpass.getpass("Password: ")
    if not (url and anon_key and email and password):
        raise PoolError("url, anon key, email and password are all required")

    client = PoolClient(url, anon_key)
    session = client.sign_in_password(email, password)
    from claude_swap.pool.sync import POOL_SCHEMA_VERSION
    version = client.schema_version(session)
    if version != POOL_SCHEMA_VERSION:
        raise PoolError(f"pool schema is v{version}; this cswap speaks v{POOL_SCHEMA_VERSION}")
    member = client.member(session)
    client.register_machine(session, machine_id(switcher.backup_dir), platform.node(), __version__)

    save_session(switcher.backup_dir, session)
    set_setting(switcher.backup_dir, "pool.url", url)
    set_setting(switcher.backup_dir, "pool.anonKey", anon_key)
    print(f"{accent('Logged in')} to the pool as {email} ({member['role']})")

    sync = PoolSync(switcher, client, session, machine_id=machine_id(switcher.backup_dir))
    _print_report(sync.run_pass())
    from claude_swap.settings import load_settings
    if load_settings(switcher.backup_dir).dead_token_strikes < 2:
        print(dimmed("Tip: cswap config set autoswitch.deadTokenStrikes 2 tolerates the pool's sync lag"))


def _logout(switcher: ClaudeAccountSwitcher, args) -> None:
    if load_session(switcher.backup_dir) is None:
        print(dimmed("Not logged in to a pool."))
        return
    data = switcher._get_sequence_data() or {}
    for num in sorted((data.get("accounts") or {}).keys(), key=int):
        account_id, owned = switcher.slot_pool_info(num)
        if not account_id:
            continue
        if owned or args.keep:
            switcher.set_slot_pool_info(num, None, False)
        else:
            switcher.remove_account(num, assume_yes=True)
    clear_session(switcher.backup_dir)
    print(f"{accent('Logged out')} of the pool" + ("; borrowed accounts kept" if args.keep else "; borrowed accounts removed"))


def _status(switcher: ClaudeAccountSwitcher, args) -> None:
    session = load_session(switcher.backup_dir)
    state = load_state(switcher.backup_dir)
    data = switcher._get_sequence_data() or {}
    rows = []
    for num in sorted((data.get("accounts") or {}).keys(), key=int):
        account_id, owned = switcher.slot_pool_info(num)
        if account_id:
            rows.append({"number": int(num), "email": data["accounts"][num].get("email", ""),
                         "owned": owned, "poolAccountId": account_id})
    if args.json:
        print(json.dumps({
            "schemaVersion": 1,
            "member": None if session is None else {"email": session.email, "userId": session.user_id, "url": session.url},
            "machineId": machine_id(switcher.backup_dir),
            "lastPassAt": state.get("lastPassAt"),
            "accounts": rows,
            "attention": state.get("attention", []),
        }, indent=2))
        return
    if session is None:
        print(dimmed("Not logged in to a pool. Run: cswap pool login"))
        return
    print(f"{bolded('Pool:')} {session.url}  as {session.email}")
    last = state.get("lastPassAt")
    print(f"  machine {machine_id(switcher.backup_dir)}  last sync "
          + (f"{int(time.time() - last)}s ago" if isinstance(last, (int, float)) else "never"))
    for row in rows:
        tag = "owned" if row["owned"] else "borrowed"
        print(f"  {row['number']:>2}  {row['email']}  {dimmed(tag)}")
    for item in state.get("attention", []):
        print_warning(f"  your account {item['email']} needs re-login (reported {item.get('since') or 'recently'}); "
                      "log in with Claude Code, then run: cswap add")


def _share(switcher: ClaudeAccountSwitcher, args) -> None:
    from claude_swap.rules import parse_hard_limit, parse_swap_limit
    sync = build_sync(switcher)
    if sync is None:
        raise PoolError("not logged in to a pool (or pool.enabled is false); run: cswap pool login")
    num = switcher._resolve_account_identifier(args.account)
    if num is None:
        raise PoolError(f"account not found: {args.account}")
    swap = parse_swap_limit(args.swap_limit) if args.swap_limit is not None else None
    hard = parse_hard_limit(args.hard_limit) if args.hard_limit is not None else None
    row = sync.publish_slot(num, shared=not args.private, swap_limit=swap,
                            hard_limit=None if hard in (None, 100.0) else hard)
    print(f"{accent('Shared' if row.shared else 'Published (private)')} {row.email}"
          + (f"  swap {row.share_swap_limit:g}" if row.share_swap_limit else "")
          + (f"  hard {row.share_hard_limit:g}" if row.share_hard_limit else ""))


def _unshare(switcher: ClaudeAccountSwitcher, args) -> None:
    sync = build_sync(switcher)
    if sync is None:
        raise PoolError("not logged in to a pool; run: cswap pool login")
    num = switcher._resolve_account_identifier(args.account)
    if num is None:
        raise PoolError(f"account not found: {args.account}")
    sync.withdraw_slot(num)
    print(f"{accent('Withdrawn')} account {num} from the pool")


# -- sync ------------------------------------------------------------------------
def sync_command(argv: list[str]) -> None:
    raise SystemExit("cswap sync: implemented in Task 12")
