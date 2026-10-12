"""Run a cswap subcommand in the background at Windows logon.

The Windows counterpart of :mod:`claude_swap.launch_agent`, used by
``cswap sync --install-service`` so pooled logins keep rotating without a
terminal left open.

Three decisions worth stating:

*A ``HKCU\\...\\Run`` value, not a Task Scheduler task.* ``schtasks /SC
ONLOGON`` needs an elevated prompt on a stock Windows install; the per-user
Run key needs nothing, and is what Windows itself shows under Task Manager ->
Startup apps, where the user can see and disable it.

*``pythonw.exe -m claude_swap.windows_startup run``, not ``cswap.exe``.* The
console script opens a console window that would sit on the desktop for as
long as the loop runs. ``pythonw`` has no window -- and no stdout either, so
the ``run`` entry below points both streams at log files before handing the
argv to the normal CLI.

*A held lock file decides "running", not a pid.* Windows reuses pids
quickly, so a stale pid file could name an unrelated process and ``uninstall``
would terminate it. The runner holds an exclusive lock on ``<label>.lock`` for
its whole life: a second copy (logon start plus a manual one) exits at once,
and a pid is only acted on while that lock is held -- i.e. while its owner is
still our runner.

Everything here is Windows-only; the public functions refuse elsewhere.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from claude_swap.exceptions import ClaudeSwitchError

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_MODULE = "claude_swap.windows_startup"

# CreateProcess flags: no console, not tied to the console that started it,
# and out of its process group so a Ctrl+C there does not reach the loop.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def _require_windows() -> None:
    if sys.platform != "win32":
        raise ClaudeSwitchError("The Windows startup service is only available on Windows.")


def state_dir(backup_root: Path | None = None) -> Path:
    """Where logs, the pid file and the lock live: ``<backup root>/services``."""
    if backup_root is None:
        from claude_swap.paths import get_backup_root

        backup_root = get_backup_root()
    return backup_root / "services"


def log_paths(label: str, backup_root: Path | None = None) -> tuple[Path, Path]:
    """``(stdout, stderr)`` log destinations for ``label``."""
    d = state_dir(backup_root)
    return d / f"{label}.log", d / f"{label}.err"


def _pid_path(label: str, backup_root: Path | None) -> Path:
    return state_dir(backup_root) / f"{label}.pid"


def _lock_path(label: str, backup_root: Path | None) -> Path:
    return state_dir(backup_root) / f"{label}.lock"


def resolve_interpreter(executable: str | None = None) -> str:
    """``pythonw.exe`` beside the running interpreter, else the interpreter.

    ``sys.executable`` is the tool's own virtualenv; ``uv tool install
    --force`` rebuilds that virtualenv at the same path, so pinning it here
    survives the team's update command.
    """
    exe = Path(executable or sys.executable)
    windowless = exe.with_name("pythonw.exe")
    return str(windowless if windowless.is_file() else exe)


def build_command(label: str, arguments: tuple[str, ...], interpreter: str | None = None) -> list[str]:
    """Argv the Run value launches."""
    return [interpreter or resolve_interpreter(), "-m", _MODULE, "run", label, *arguments]


# -- registry --------------------------------------------------------------------
def _read_run_value(label: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, label)
            return str(value)
    except FileNotFoundError:
        return None


def _write_run_value(label: str, command: str) -> None:
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, label, 0, winreg.REG_SZ, command)


def _delete_run_value(label: str) -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, label)
            return True
    except FileNotFoundError:
        return False


# -- the single-instance lock ----------------------------------------------------
def _try_lock(path: Path):
    """Open ``path`` and take a non-blocking exclusive lock; the open file, or None."""
    import msvcrt

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def _lock_is_held(label: str, backup_root: Path | None) -> bool:
    path = _lock_path(label, backup_root)
    if not path.exists():
        return False
    handle = _try_lock(path)
    if handle is None:
        return True
    _release(handle)
    return False


def _release(handle) -> None:
    import msvcrt

    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    handle.close()


def _running_pid(label: str, backup_root: Path | None) -> int | None:
    """Pid of the live runner, or None. Only trusted while the lock is held."""
    if not _lock_is_held(label, backup_root):
        return None
    try:
        return int(_pid_path(label, backup_root).read_text().strip())
    except (OSError, ValueError):
        return None


def _stop(label: str, backup_root: Path | None) -> int | None:
    """Terminate the live runner if there is one; the pid it had."""
    pid = _running_pid(label, backup_root)
    if pid is None:
        return None
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/F"],
        capture_output=True,
        text=True,
        check=False,
    )
    return pid


def _spawn(command: list[str]) -> None:
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW,
    )


# -- public API ------------------------------------------------------------------
def install(
    label: str,
    arguments: tuple[str, ...],
    *,
    backup_root: Path | None = None,
    interpreter: str | None = None,
    start: bool = True,
) -> dict:
    """Register ``label`` to run at logon, and (re)start it now.

    Idempotent: a running copy is stopped first, so re-running this after an
    update picks up the new code instead of leaving the old loop alive.
    """
    _require_windows()
    command = build_command(label, arguments, interpreter)
    out_log, err_log = log_paths(label, backup_root)
    out_log.parent.mkdir(parents=True, exist_ok=True)
    _write_run_value(label, subprocess.list2cmdline(command))
    if start:
        _stop(label, backup_root)
        _spawn(command)
    return {
        "label": label,
        "registry": f"HKCU\\{RUN_KEY}\\{label}",
        "program": command,
        "stdout_log": str(out_log),
        "stderr_log": str(err_log),
    }


def uninstall(label: str, *, backup_root: Path | None = None) -> dict:
    """Remove the logon entry and stop the running copy. Tolerates partial states."""
    _require_windows()
    removed = _delete_run_value(label)
    stopped = _stop(label, backup_root)
    return {"label": label, "removed_registry_value": removed, "stopped_pid": stopped}


def status(label: str, *, backup_root: Path | None = None) -> dict:
    """Installed / running state, plus the pid when there is one."""
    _require_windows()
    command = _read_run_value(label)
    pid = _running_pid(label, backup_root)
    out_log, err_log = log_paths(label, backup_root)
    return {
        "label": label,
        "installed": command is not None,
        "running": pid is not None,
        "pid": pid,
        "command": command,
        "stdout_log": str(out_log),
        "stderr_log": str(err_log),
    }


def run(label: str, arguments: list[str], *, backup_root: Path | None = None) -> int:
    """Body of the background process: lock, redirect output, run the CLI."""
    lock = _try_lock(_lock_path(label, backup_root))
    if lock is None:
        return 0  # another copy already owns this label
    try:
        _pid_path(label, backup_root).write_text(str(os.getpid()))
        out_log, err_log = log_paths(label, backup_root)
        # Line-buffered so a log tail shows each sync pass as it happens.
        sys.stdout = open(out_log, "a", encoding="utf-8", buffering=1)
        sys.stderr = open(err_log, "a", encoding="utf-8", buffering=1)
        sys.argv = ["cswap", *arguments]
        from claude_swap.cli import main

        try:
            main()
        except SystemExit as e:
            return e.code if isinstance(e.code, int) else 0
        return 0
    finally:
        _release(lock)


if __name__ == "__main__":  # pragma: no cover - exercised by the Run value
    if len(sys.argv) < 3 or sys.argv[1] != "run":
        print(f"usage: python -m {_MODULE} run <label> <cswap args...>", file=sys.stderr)
        sys.exit(2)
    sys.exit(run(sys.argv[2], sys.argv[3:]))
