"""Windows logon service for `cswap sync` (windows_startup.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from claude_swap import windows_startup as ws
from claude_swap.exceptions import ClaudeSwitchError

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="msvcrt locking is Windows-only")


@pytest.fixture
def fake_os(monkeypatch):
    """Registry, spawn and taskkill replaced by an in-memory record."""
    state = {"registry": {}, "spawned": [], "killed": []}
    monkeypatch.setattr(ws.sys, "platform", "win32")
    monkeypatch.setattr(ws, "_read_run_value", lambda label: state["registry"].get(label))
    monkeypatch.setattr(ws, "_write_run_value", lambda label, cmd: state["registry"].__setitem__(label, cmd))
    monkeypatch.setattr(ws, "_delete_run_value", lambda label: state["registry"].pop(label, None) is not None)
    monkeypatch.setattr(ws, "_spawn", lambda cmd: state["spawned"].append(cmd))
    return state


def test_build_command_runs_the_module_under_the_given_interpreter():
    cmd = ws.build_command("com.cswap.sync", ("sync",), interpreter=r"C:\v\Scripts\pythonw.exe")
    assert cmd == [r"C:\v\Scripts\pythonw.exe", "-m", "claude_swap.windows_startup", "run", "com.cswap.sync", "sync"]


def test_resolve_interpreter_prefers_the_windowless_sibling(tmp_path):
    exe = tmp_path / "python.exe"; exe.write_text("")
    assert ws.resolve_interpreter(str(exe)) == str(exe)
    (tmp_path / "pythonw.exe").write_text("")
    assert ws.resolve_interpreter(str(exe)) == str(tmp_path / "pythonw.exe")


def test_state_lives_under_the_backup_root(tmp_path):
    out, err = ws.log_paths("com.cswap.sync", tmp_path)
    assert out == tmp_path / "services" / "com.cswap.sync.log"
    assert err == tmp_path / "services" / "com.cswap.sync.err"


@pytest.mark.parametrize("call", [
    lambda: ws.install("x", ("sync",)),
    lambda: ws.uninstall("x"),
    lambda: ws.status("x"),
])
def test_refuses_off_windows(monkeypatch, call):
    monkeypatch.setattr(ws.sys, "platform", "darwin")
    with pytest.raises(ClaudeSwitchError, match="only available on Windows"):
        call()


def test_install_registers_and_starts(fake_os, tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "_stop", lambda label, root: None)
    result = ws.install("com.cswap.sync", ("sync",), backup_root=tmp_path, interpreter=r"C:\Program Files\py\pythonw.exe")
    # list2cmdline quotes the space-containing path, so the Run value parses.
    assert fake_os["registry"]["com.cswap.sync"].startswith('"C:\\Program Files\\py\\pythonw.exe" -m claude_swap.windows_startup run')
    assert fake_os["spawned"] == [result["program"]]
    assert (tmp_path / "services").is_dir()


def test_install_restarts_a_running_copy(fake_os, tmp_path, monkeypatch):
    order = []
    monkeypatch.setattr(ws, "_stop", lambda label, root: order.append("stop"))
    monkeypatch.setattr(ws, "_spawn", lambda cmd: order.append("spawn"))
    ws.install("com.cswap.sync", ("sync",), backup_root=tmp_path, interpreter="pyw")
    assert order == ["stop", "spawn"]


def test_uninstall_tolerates_nothing_installed(fake_os, tmp_path):
    assert ws.uninstall("com.cswap.sync", backup_root=tmp_path) == {
        "label": "com.cswap.sync", "removed_registry_value": False, "stopped_pid": None,
    }


@windows_only
def test_a_stale_pid_file_is_never_acted_on(fake_os, tmp_path, monkeypatch):
    """No lock held -> the pid may belong to anything now; leave it alone."""
    d = tmp_path / "services"; d.mkdir()
    (d / "com.cswap.sync.pid").write_text("4242")
    (d / "com.cswap.sync.lock").write_text("")
    killed = []
    monkeypatch.setattr(ws.subprocess, "run", lambda *a, **kw: killed.append(a))
    assert ws.status("com.cswap.sync", backup_root=tmp_path)["pid"] is None
    assert ws.uninstall("com.cswap.sync", backup_root=tmp_path)["stopped_pid"] is None
    assert killed == []


@windows_only
def test_a_held_lock_reports_running_and_is_stopped(fake_os, tmp_path, monkeypatch):
    d = tmp_path / "services"; d.mkdir()
    (d / "com.cswap.sync.pid").write_text("4242")
    lock = ws._try_lock(d / "com.cswap.sync.lock")
    try:
        killed = []
        monkeypatch.setattr(ws.subprocess, "run", lambda argv, **kw: killed.append(argv))
        st = ws.status("com.cswap.sync", backup_root=tmp_path)
        assert st["running"] is True and st["pid"] == 4242
        assert ws.uninstall("com.cswap.sync", backup_root=tmp_path)["stopped_pid"] == 4242
        assert killed == [["taskkill", "/PID", "4242", "/F"]]
    finally:
        ws._release(lock)


@windows_only
def test_run_exits_at_once_when_another_copy_holds_the_lock(tmp_path, monkeypatch):
    lock = ws._try_lock(tmp_path / "services" / "lbl.lock")
    try:
        monkeypatch.setattr("claude_swap.cli.main", lambda: pytest.fail("second copy ran the CLI"))
        assert ws.run("lbl", ["sync"], backup_root=tmp_path) == 0
    finally:
        ws._release(lock)


@windows_only
def test_run_hands_argv_to_the_cli_with_output_in_the_logs(tmp_path, monkeypatch):
    seen = {}

    def fake_main():
        seen["argv"] = list(sys.argv)
        print("one pass")
        raise SystemExit(3)

    monkeypatch.setattr("claude_swap.cli.main", fake_main)
    monkeypatch.setattr(sys, "stdout", sys.stdout)  # restored after run() swaps it
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    monkeypatch.setattr(sys, "argv", sys.argv)
    assert ws.run("lbl", ["sync"], backup_root=tmp_path) == 3
    sys.stdout.close()
    sys.stderr.close()
    assert seen["argv"] == ["cswap", "sync"]
    assert (tmp_path / "services" / "lbl.log").read_text(encoding="utf-8") == "one pass\n"
    assert (tmp_path / "services" / "lbl.pid").read_text().isdigit()


class TestSyncServiceCli:
    def _run(self, monkeypatch, flag, platform):
        from claude_swap.pool import cli as pool_cli

        calls = []
        monkeypatch.setattr(pool_cli.sys, "platform", platform)
        monkeypatch.setattr(ws, "install", lambda label, args: calls.append(("install", label, args)) or {
            "registry": "HKCU\\Run\\" + label, "stdout_log": "x.log"})
        monkeypatch.setattr(ws, "uninstall", lambda label: calls.append(("uninstall", label)))
        monkeypatch.setattr(ws, "status", lambda label: calls.append(("status", label)) or {"label": label})
        with pytest.raises(SystemExit) as exc:
            pool_cli.sync_command([flag])
        return exc.value.code, calls

    @pytest.mark.parametrize("flag,expected", [
        ("--install-service", ("install", "com.cswap.sync", ("sync",))),
        ("--uninstall-service", ("uninstall", "com.cswap.sync")),
        ("--service-status", ("status", "com.cswap.sync")),
    ])
    def test_windows_routes_to_the_logon_service(self, monkeypatch, flag, expected):
        code, calls = self._run(monkeypatch, flag, "win32")
        assert code == 0 and calls == [expected]

    def test_status_prints_json(self, monkeypatch, capsys):
        self._run(monkeypatch, "--service-status", "win32")
        assert json.loads(capsys.readouterr().out) == {"label": "com.cswap.sync"}

    def test_linux_still_refuses(self, monkeypatch, capsys):
        code, calls = self._run(monkeypatch, "--install-service", "linux")
        assert code == 1 and calls == []
        assert "macOS and Windows" in capsys.readouterr().err
