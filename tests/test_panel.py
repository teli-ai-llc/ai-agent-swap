"""Tests for ``cswap panel`` — the menu bar drop-down (panel.py).

``subprocess.run`` is stubbed throughout: nothing here invokes swift,
codesign or launchctl. The tests pin what the module *shapes* — the build
argv, the bundle layout, the rebuild key, the launchd plist it asks for, and
the first-run offer's decision table — against a tmp backup root.
"""

from __future__ import annotations

import json
import plistlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import __version__, launch_agent, panel
from claude_swap.exceptions import ClaudeSwitchError


@pytest.fixture(autouse=True)
def _on_macos():
    with patch.object(panel.sys, "platform", "darwin"), patch.object(
        launch_agent.sys, "platform", "darwin"
    ):
        yield


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _fake_run(calls: list[list[str]], *, fail: str | None = None):
    """Record argv; fabricate the swift binary so the bundle step can copy it."""

    def run(argv, **kwargs):
        calls.append(list(argv))
        tool = argv[0]
        if tool == fail:
            return _completed(1, stderr=f"{tool} exploded")
        if tool == "swift" and argv[1] == "build":
            scratch = Path(argv[argv.index("--scratch-path") + 1])
            out = scratch / "release" / panel.APP_NAME
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"\x00binary")
        return _completed(0)

    return run


@pytest.fixture
def which_swift(monkeypatch):
    monkeypatch.setattr(panel.shutil, "which", lambda name: f"/usr/bin/{name}")


# --- toolchain ----------------------------------------------------------------


def test_toolchain_missing_when_swift_is_not_on_path(monkeypatch):
    monkeypatch.setattr(panel.shutil, "which", lambda name: None)
    assert panel.toolchain_missing(run=lambda *a, **k: _completed(0)) == "swift not found"


def test_toolchain_missing_when_no_developer_dir_is_selected(which_swift):
    # /usr/bin/swift exists on every Mac as a shim; only xcode-select -p proves
    # the Command Line Tools are really there.
    assert panel.toolchain_missing(run=lambda *a, **k: _completed(2)) is not None


def test_toolchain_present(which_swift):
    assert panel.toolchain_missing(run=lambda *a, **k: _completed(0)) is None


def test_build_refuses_with_an_install_hint_when_toolchain_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(panel.shutil, "which", lambda name: None)
    with pytest.raises(ClaudeSwitchError, match="xcode-select --install"):
        panel.build(tmp_path, run=lambda *a, **k: _completed(0))


# --- build ---------------------------------------------------------------------


def test_build_compiles_into_the_backup_root_not_site_packages(tmp_path, which_swift):
    calls: list[list[str]] = []
    binary = panel.build(tmp_path, run=_fake_run(calls))

    swift = next(c for c in calls if c[0] == "swift")
    assert swift[:4] == ["swift", "build", "-c", "release"]
    pkg = Path(swift[swift.index("--package-path") + 1])
    scratch = Path(swift[swift.index("--scratch-path") + 1])
    assert pkg == tmp_path / "panel" / "src"
    assert scratch == tmp_path / "panel" / ".build"
    # the sources were copied, so SwiftPM never writes next to panel.py
    assert (pkg / "Package.swift").is_file()
    assert (pkg / "Sources" / "CswapPanel" / "main.swift").is_file()
    assert binary == tmp_path / "panel" / f"{panel.APP_NAME}.app" / "Contents" / "MacOS" / panel.APP_NAME
    assert binary.read_bytes() == b"\x00binary"


def test_build_writes_a_menu_bar_only_bundle_and_signs_it(tmp_path, which_swift):
    calls: list[list[str]] = []
    panel.build(tmp_path, run=_fake_run(calls))

    info = plistlib.loads((panel.app_bundle(tmp_path) / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == panel.APP_NAME
    assert info["CFBundleIdentifier"] == panel.BUNDLE_ID
    assert info["LSUIElement"] is True  # no Dock icon
    assert info["CFBundleShortVersionString"] == __version__

    sign = next(c for c in calls if c[0] == "codesign")
    assert sign[1:3] == ["--sign", "-"]
    assert sign[-1] == str(panel.app_bundle(tmp_path))


def test_build_surfaces_the_compiler_tail_on_failure(tmp_path, which_swift):
    with pytest.raises(ClaudeSwitchError, match="(?s)swift build failed.*swift exploded"):
        panel.build(tmp_path, run=_fake_run([], fail="swift"))


def test_build_fails_when_signing_fails(tmp_path, which_swift):
    with pytest.raises(ClaudeSwitchError, match="codesign failed"):
        panel.build(tmp_path, run=_fake_run([], fail="codesign"))


# --- rebuild key ---------------------------------------------------------------


def test_is_built_only_after_a_build_from_the_current_sources(tmp_path, which_swift):
    assert panel.is_built(tmp_path) is False
    panel.build(tmp_path, run=_fake_run([]))
    assert panel.is_built(tmp_path) is True


def test_source_change_invalidates_the_build(tmp_path, which_swift):
    src = tmp_path / "srcdir"
    (src / "Sources" / "CswapPanel").mkdir(parents=True)
    (src / "Package.swift").write_text("// v1")
    (src / "Sources" / "CswapPanel" / "main.swift").write_text("// v1")
    panel.build(tmp_path, source_dir=src, run=_fake_run([]))
    assert panel.is_built(tmp_path, source_dir=src)

    (src / "Sources" / "CswapPanel" / "main.swift").write_text("// v2")
    assert panel.is_built(tmp_path, source_dir=src) is False


def test_version_bump_invalidates_the_build(tmp_path, which_swift):
    panel.build(tmp_path, run=_fake_run([]))
    with patch.object(panel, "__version__", "999.0.0"):
        assert panel.is_built(tmp_path) is False


def test_ensure_built_skips_the_compiler_when_fresh(tmp_path, which_swift):
    calls: list[list[str]] = []
    panel.build(tmp_path, run=_fake_run(calls))
    n = len(calls)
    panel.ensure_built(tmp_path, run=_fake_run(calls))
    assert len(calls) == n


# --- launchd ---------------------------------------------------------------------


def test_install_service_runs_the_bundled_binary_with_its_environment(tmp_path, which_swift):
    seen = {}

    def fake_install(**kw):
        seen.update(kw)
        return {"label": kw["label"], "plist": "p", "program": kw["program"], "stdout_log": "o", "stderr_log": "e"}

    with patch.object(panel.launch_agent, "install", fake_install), patch.object(
        panel.launch_agent, "resolve_program", lambda: ["/Users/x/.local/bin/cswap"]
    ):
        panel.install_service(tmp_path, run=_fake_run([]))

    assert seen["label"] == panel.LABEL == "com.cswap.panel"
    assert seen["program"] == [str(panel.binary_path(tmp_path))]
    assert seen["arguments"] == ()  # the binary takes no subcommand
    assert seen["environment"] == {
        "CSWAP_PANEL_PROGRAM": "/Users/x/.local/bin/cswap",
        "CSWAP_PANEL_DATA_DIR": str(tmp_path),
    }


@pytest.mark.skipif(sys.platform == "win32", reason="launchd PATH is built from POSIX paths")
def test_launch_agent_plist_carries_custom_arguments_and_environment(tmp_path):
    parsed = plistlib.loads(
        launch_agent.build_plist(
            ["/bin/panel"],
            label=panel.LABEL,
            home=tmp_path,
            arguments=(),
            environment={"CSWAP_PANEL_DATA_DIR": "/d"},
        )
    )
    assert parsed["ProgramArguments"] == ["/bin/panel"]
    assert parsed["EnvironmentVariables"]["CSWAP_PANEL_DATA_DIR"] == "/d"
    assert parsed["EnvironmentVariables"]["PATH"].startswith("/bin")


def test_launch_agent_plist_default_is_unchanged_for_the_menubar(tmp_path):
    parsed = plistlib.loads(launch_agent.build_plist(["/u/cswap"], home=tmp_path))
    assert parsed["ProgramArguments"] == ["/u/cswap", "menubar"]
    assert set(parsed["EnvironmentVariables"]) == {"PATH"}


def test_uninstall_and_status_address_the_panel_label():
    with patch.object(panel.launch_agent, "uninstall", lambda **kw: kw) as _, patch.object(
        panel.launch_agent, "status", lambda **kw: kw
    ):
        assert panel.uninstall_service()["label"] == panel.LABEL
        assert panel.service_status()["label"] == panel.LABEL


def test_run_foreground_passes_the_environment_to_the_binary(tmp_path, which_swift):
    calls: list[list[str]] = []
    envs = []

    def run(argv, **kw):
        calls.append(list(argv))
        envs.append(kw.get("env"))
        if argv[0] == "swift":
            return _fake_run([])(argv, **kw)
        return _completed(0)

    with patch.object(panel.launch_agent, "resolve_program", lambda: ["/u/cswap"]):
        assert panel.run_foreground(tmp_path, run=run) == 0
    assert calls[-1] == [str(panel.binary_path(tmp_path))]
    assert envs[-1]["CSWAP_PANEL_PROGRAM"] == "/u/cswap"
    assert envs[-1]["CSWAP_PANEL_DATA_DIR"] == str(tmp_path)


def test_refuses_off_macos(tmp_path):
    with patch.object(panel.sys, "platform", "linux"):
        with pytest.raises(ClaudeSwitchError, match="only available on macOS"):
            panel.build(tmp_path)
        with pytest.raises(ClaudeSwitchError):
            panel.install_service(tmp_path)
        assert panel.offer_pending(tmp_path) is False


# --- first-run offer -------------------------------------------------------------


def test_offer_is_silent_when_not_interactive(tmp_path):
    asked = []
    assert panel.offer(tmp_path, interactive=False, ask=lambda p: asked.append(p) or "y") is False
    assert asked == []
    # left pending: a later interactive `add` still gets to ask
    assert panel.offer_pending(tmp_path, home=tmp_path)


def test_offer_asks_once_and_a_no_never_nags_again(tmp_path):
    asked = []
    out = []
    with patch.object(panel.launch_agent, "plist_path", lambda label, home=None: tmp_path / "none"):
        first = panel.offer(tmp_path, interactive=True, ask=lambda p: asked.append(p) or "n", out=out.append)
        second = panel.offer(tmp_path, interactive=True, ask=lambda p: asked.append(p) or "y", out=out.append)
    assert (first, second) == (False, False)
    assert len(asked) == 1
    assert (tmp_path / panel.OFFER_MARKER).exists()
    assert any("cswap panel --install-service" in line for line in out)


def test_offer_yes_installs_the_service(tmp_path):
    installed = {}

    def fake_install(root, run=None, log=None):
        installed["root"] = root
        return {"label": panel.LABEL}

    out = []
    with patch.object(panel, "install_service", fake_install), patch.object(
        panel.launch_agent, "plist_path", lambda label, home=None: tmp_path / "none"
    ):
        assert panel.offer(tmp_path, interactive=True, ask=lambda p: "y", out=out.append) is True
    assert installed["root"] == tmp_path
    assert any('"CS"' in line for line in out)


def test_offer_reports_a_failed_install_instead_of_raising(tmp_path):
    def boom(root, run=None, log=None):
        raise ClaudeSwitchError("no swift")

    out = []
    with patch.object(panel, "install_service", boom), patch.object(
        panel.launch_agent, "plist_path", lambda label, home=None: tmp_path / "none"
    ):
        assert panel.offer(tmp_path, interactive=True, ask=lambda p: "yes", out=out.append) is False
    assert any("no swift" in line for line in out)


def test_offer_skipped_when_the_service_is_already_installed(tmp_path):
    plist = tmp_path / "Library" / "LaunchAgents" / f"{panel.LABEL}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("")
    assert panel.offer_pending(tmp_path, home=tmp_path) is False


def test_offer_handles_ctrl_c_at_the_prompt(tmp_path):
    def interrupted(prompt):
        raise KeyboardInterrupt

    with patch.object(panel.launch_agent, "plist_path", lambda label, home=None: tmp_path / "none"):
        assert panel.offer(tmp_path, interactive=True, ask=interrupted, out=lambda s: None) is False
