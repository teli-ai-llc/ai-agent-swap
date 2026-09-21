"""`cswap pool …` and `cswap sync …` handlers (pre-dispatched from cli.main)."""

from __future__ import annotations

import argparse
import getpass
import json
import platform
import sys
import time
from dataclasses import dataclass

from claude_swap import __version__
from claude_swap.exceptions import ClaudeSwitchError, PoolAuthError, PoolError
from claude_swap.pool.client import PoolClient
from claude_swap.pool.guard import enforce_remote_control_guard, remote_control_guard_gaps
from claude_swap.pool.session import clear_session, load_session, machine_id, save_session
from claude_swap.pool.sync import POOL_SCHEMA_VERSION, PassReport, PoolSync, _ago, build_sync, load_state, reset_state
from claude_swap.printer import accent, bolded, dimmed, error as print_error, warning as print_warning, yellowed
from claude_swap.settings import load_pool_settings, load_settings, set_setting
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


# -- pool helpers ------------------------------------------------------------------
@dataclass(frozen=True)
class LoginResult:
    email: str
    role: str
    report: PassReport
    suggest_strikes: bool          # True when autoswitch.deadTokenStrikes < 2
    guard_applied: tuple[str, ...] = ()   # Remote Control guard keys this login had to write
    signed_up: bool = False        # first login: the member was created just now


@dataclass(frozen=True)
class LogoutResult:
    removed: list[str]             # slot numbers removed
    kept: list[tuple[str, str, str]]   # (slot, email, reason) kept locally
    unlinked: list[str]            # owned slots unlinked


def _sign_in_or_up(client: PoolClient, email: str, code: str):
    """The shared pool code is every member's password. A known email signs
    in; an unknown one is signed up on the spot (the pool's signup domains
    decide who may). Returns ``(session, signed_up)``."""
    try:
        return client.sign_in_password(email, code), False
    except PoolAuthError as e:
        if "invalid login credentials" not in str(e).lower():
            raise
    return client.sign_up_password(email, code), True


def login_pool(switcher: ClaudeAccountSwitcher, url: str, anon_key: str, email: str, code: str) -> LoginResult:
    """Sign in with the pool code (signing up a first-time email), check
    schema, load the member row, register this machine, enforce the Remote
    Control guard, save the session, persist pool.url/pool.anonKey, then run
    one sync pass.

    Raises PoolError/PoolAuthError on any failure; nothing is persisted on
    failure paths that raise before ``save_session`` (the guard write is the
    one exception — it is idempotent and wanted on any pooled machine).
    """
    client = PoolClient(url, anon_key)
    session, signed_up = _sign_in_or_up(client, email, code)
    version = client.schema_version(session)
    if version != POOL_SCHEMA_VERSION:
        raise PoolError(f"pool schema is v{version}; this cswap speaks v{POOL_SCHEMA_VERSION}")
    member = client.member(session)
    client.register_machine(session, machine_id(switcher.backup_dir), platform.node(), __version__)
    guard_applied = tuple(enforce_remote_control_guard())

    save_session(switcher.backup_dir, session)
    set_setting(switcher.backup_dir, "pool.url", url)
    set_setting(switcher.backup_dir, "pool.anonKey", anon_key)

    sync = PoolSync(switcher, client, session, machine_id=machine_id(switcher.backup_dir))
    report = sync.run_pass()
    suggest_strikes = load_settings(switcher.backup_dir).dead_token_strikes < 2
    return LoginResult(email=session.email or email.strip().lower(), role=member["role"], report=report,
                       suggest_strikes=suggest_strikes, guard_applied=guard_applied, signed_up=signed_up)


def guard_warning_lines(indent: str = "  ") -> list[str]:
    """The warning `pool status` and `sync` print when the Remote Control
    guard has been removed from this machine's Claude settings; empty when
    the guard is in place."""
    gaps = remote_control_guard_gaps()
    if not gaps:
        return []
    return [indent + yellowed(
        "Remote Control is not disabled in Claude Code's settings.json (" + ", ".join(gaps) + "); "
        "a session started on a borrowed login would be visible to its owner. Run: cswap pool login"
    )]


def logout_pool(switcher: ClaudeAccountSwitcher, *, keep: bool) -> LogoutResult:
    """Sign out; removes borrowed accounts unless ``keep``. Raises PoolError
    when not currently logged in."""
    if load_session(switcher.backup_dir) is None:
        raise PoolError("not logged in to a pool")
    data = switcher._get_sequence_data() or {}
    removed: list[str] = []
    kept: list[tuple[str, str, str]] = []
    unlinked: list[str] = []
    for num in sorted((data.get("accounts") or {}).keys(), key=int):
        account_id, owned = switcher.slot_pool_info(num)
        if not account_id:
            continue
        if owned or keep:
            switcher.set_slot_pool_info(num, None, False)
            unlinked.append(num)
        else:
            email = (data.get("accounts") or {}).get(num, {}).get("email", "")
            try:
                switcher.remove_account(num, assume_yes=True, quiet=True)
                removed.append(num)
            except ClaudeSwitchError as e:
                # Logout always completes: a slot that refuses to be removed
                # (e.g. it is the live Claude Code session) just stays as a
                # plain local account instead of blocking the sign-out.
                switcher.set_slot_pool_info(num, None, False)
                kept.append((num, email, str(e)))
    clear_session(switcher.backup_dir)
    reset_state(switcher.backup_dir)
    return LogoutResult(removed=removed, kept=kept, unlinked=unlinked)


def status_lines(switcher: ClaudeAccountSwitcher) -> list[str]:
    """Exactly the lines `cswap pool status` prints in human (non-JSON) mode."""
    session = load_session(switcher.backup_dir)
    if session is None:
        return [dimmed("Not logged in to a pool. Run: cswap pool login")]
    state = load_state(switcher.backup_dir)
    data = switcher._get_sequence_data() or {}
    rows = []
    for num in sorted((data.get("accounts") or {}).keys(), key=int):
        account_id, owned = switcher.slot_pool_info(num)
        if account_id:
            rows.append({"number": int(num), "email": data["accounts"][num].get("email", ""),
                         "owned": owned, "poolAccountId": account_id})
    lines = [f"{bolded('Pool:')} {session.url}  as {session.email}"]
    last = state.get("lastPassAt")
    lines.append("  machine " + machine_id(switcher.backup_dir) + "  last sync "
                  + (f"{int(time.time() - last)}s ago" if isinstance(last, (int, float)) else "never"))
    lines.extend(guard_warning_lines())
    for row in rows:
        tag = "owned" if row["owned"] else "borrowed"
        lines.append(f"  {row['number']:>2}  {row['email']}  {dimmed(tag)}")
    for item in state.get("attention", []):
        lines.append(yellowed(
            f"  your account {item['email']} needs re-login (reported {_ago(item.get('since'), time.time())}); "
            "log in with Claude Code, then run: cswap add"
        ))
    return lines


def apply_pooled_defaults(switcher: ClaudeAccountSwitcher) -> bool:
    """Set autoswitch.deadTokenStrikes=2 when it's lower; True if changed."""
    if load_settings(switcher.backup_dir).dead_token_strikes >= 2:
        return False
    set_setting(switcher.backup_dir, "autoswitch.deadTokenStrikes", "2")
    return True


def pool_logged_in(switcher: ClaudeAccountSwitcher) -> bool:
    return load_session(switcher.backup_dir) is not None


# -- pool ------------------------------------------------------------------------
def pool_command(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog=f"{_prog()} pool",
        description="Team pool: share Claude logins with teammates through a Supabase project.",
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    p_login = sub.add_parser("login", help="Sign in to the pool on this machine with your work email and the pool code")
    p_login.add_argument("--url", help="Supabase project URL (saved to settings)")
    p_login.add_argument("--anon-key", help="Supabase anon key (saved to settings)")
    p_login.add_argument("--email", help="Your work email; a first login creates your membership")

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
    if not (url and anon_key and email):
        raise PoolError("url, anon key and email are all required")
    code = getpass.getpass("Pool code: ")
    if not code.strip():
        raise PoolError("the pool code is required (ask the pool admin)")

    result = login_pool(switcher, url, anon_key, email, code)
    if result.signed_up:
        print(f"{accent('Welcome to the pool')}: created your membership for {result.email}")
    print(f"{accent('Logged in')} to the pool as {result.email} ({result.role})")
    if result.guard_applied:
        print(dimmed("Remote Control disabled in Claude Code's settings.json (" + ", ".join(result.guard_applied)
                     + ") so sessions on borrowed logins never surface in a teammate's claude.ai"))
    _print_report(result.report)
    if result.suggest_strikes:
        print(dimmed("Tip: cswap config set autoswitch.deadTokenStrikes 2 tolerates the pool's sync lag"))


def _logout(switcher: ClaudeAccountSwitcher, args) -> None:
    if not pool_logged_in(switcher):
        print(dimmed("Not logged in to a pool."))
        return
    result = logout_pool(switcher, keep=args.keep)
    for num, email, reason in result.kept:
        print_warning(f"kept account {num} ({email}) locally: {reason}")
    if args.keep:
        suffix = "; borrowed accounts kept"
    elif result.kept:
        suffix = f"; {len(result.kept)} borrowed account(s) kept locally (see above)"
    else:
        suffix = "; borrowed accounts removed"
    print(f"{accent('Logged out')} of the pool" + suffix)


def _status(switcher: ClaudeAccountSwitcher, args) -> None:
    if args.json:
        session = load_session(switcher.backup_dir)
        state = load_state(switcher.backup_dir)
        data = switcher._get_sequence_data() or {}
        rows = []
        for num in sorted((data.get("accounts") or {}).keys(), key=int):
            account_id, owned = switcher.slot_pool_info(num)
            if account_id:
                rows.append({"number": int(num), "email": data["accounts"][num].get("email", ""),
                             "owned": owned, "poolAccountId": account_id})
        print(json.dumps({
            "schemaVersion": 1,
            "member": None if session is None else {"email": session.email, "userId": session.user_id, "url": session.url},
            "machineId": None if session is None else machine_id(switcher.backup_dir),
            "lastPassAt": state.get("lastPassAt"),
            "accounts": rows,
            "attention": state.get("attention", []),
            "remoteControlGuardGaps": [] if session is None else remote_control_guard_gaps(),
        }, indent=2))
        return
    for line in status_lines(switcher):
        print(line)


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


def maybe_publish_after_add(switcher: ClaudeAccountSwitcher, num: str, choice: bool | None) -> None:
    """After `cswap add`: republish an owned linked slot silently, else offer."""
    sync = build_sync(switcher)
    if sync is None:
        return
    account_id, owned = switcher.slot_pool_info(num)
    try:
        if account_id and owned:
            report = sync.run_pass()
            if report.skipped:
                print_warning(f"  Pool: {report.skipped}")
            for err in report.errors:
                print_warning(f"  Pool: {err}")
            if report.pushed:
                print(dimmed("  Pool: fresh login published"))
            return
        if account_id and not owned:
            print(dimmed("  Pool: this account belongs to another member; your local copy was updated, the pool was not"))
            return
        if choice is None:
            if not (sys.stdin.isatty() and sys.stdout.isatty()):
                return
            try:
                answer = input("Publish this account to the pool? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return
            choice = answer in ("y", "yes")
        if not choice:
            return
        row = sync.publish_slot(num, shared=True, swap_limit=None, hard_limit=None)
        print(f"  {accent('Shared')} {row.email} with the pool "
              + dimmed("(limits: cswap pool share N --swap-limit P --hard-limit P)"))
    except PoolError as e:
        print_warning(f"  Pool: {e}")


# -- sync ------------------------------------------------------------------------
SYNC_LABEL = "com.cswap.sync"


def sync_command(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog=f"{_prog()} sync",
        description="Push this machine's rotated pool logins and pull everyone else's. "
                    "Runs forever unless --once.",
    )
    parser.add_argument("--once", action="store_true", help="One pass, then exit (0 ok, 1 skipped, 2 errors)")
    parser.add_argument("--install-service", action="store_true", help="macOS: run at login via launchd")
    parser.add_argument("--uninstall-service", action="store_true")
    parser.add_argument("--service-status", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    if args.install_service or args.uninstall_service or args.service_status:
        _sync_service(args)
        return

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
    except ClaudeSwitchError as e:
        print_error(str(e))
        sys.exit(1)

    def one_pass() -> PassReport | None:
        sync = build_sync(switcher)
        if sync is None:
            print(dimmed("Not logged in to a pool (or pool.enabled is false). Run: cswap pool login"))
            return None
        report = sync.run_pass()
        _print_report(report)
        return report

    if pool_logged_in(switcher):
        for line in guard_warning_lines(indent=""):
            print(line)

    if args.once:
        report = one_pass()
        if report is None or report.skipped:
            sys.exit(1)
        sys.exit(2 if report.errors else 0)

    interval = load_pool_settings(switcher.backup_dir).poll_interval_seconds
    print(dimmed(f"Syncing every {interval:g}s; Ctrl-C to stop"))
    try:
        while True:
            try:
                one_pass()
            except KeyboardInterrupt:
                raise
            except Exception as e:  # a failing pass never kills the loop
                print_warning(f"sync pass failed: {type(e).__name__}: {e}")
            time.sleep(interval)
            interval = load_pool_settings(switcher.backup_dir).poll_interval_seconds
    except KeyboardInterrupt:
        print()
        sys.exit(0)


def _sync_service(args) -> None:
    if sys.platform != "darwin":
        print_error("The sync service is only available on macOS. Elsewhere run `cswap sync` under your service manager.")
        sys.exit(1)
    from claude_swap import launch_agent
    try:
        if args.install_service:
            result = launch_agent.install(label=SYNC_LABEL, arguments=("sync",))
            print(f"{accent('Installed')} {SYNC_LABEL} ({result.get('plist')})")
            print(dimmed("  Logs: ~/Library/Logs/com.cswap.sync.{log,err}"))
            sys.exit(0)
        if args.uninstall_service:
            launch_agent.uninstall(label=SYNC_LABEL)
            print(f"{accent('Removed')} {SYNC_LABEL}")
            sys.exit(0)
        status = launch_agent.status(label=SYNC_LABEL)
        print(json.dumps(status, indent=2))
        sys.exit(0)
    except ClaudeSwitchError as e:
        print_error(str(e))
        sys.exit(1)
