"""The Remote Control guard: Claude Code settings a pooled machine must carry.

A borrowed login makes any Remote Control session this machine starts show
up in — and be drivable from — the owner's claude.ai. ``disableRemoteControl``
lives in the per-machine ``~/.claude/settings.json``, which cswap never swaps
between accounts, so one write covers every account the machine rotates
through.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap.exceptions import PoolError
from claude_swap.pool.guard import (
    REMOTE_CONTROL_GUARD,
    enforce_remote_control_guard,
    guard_settings_path,
    remote_control_guard_gaps,
)


def test_guard_keys_are_the_documented_claude_code_settings():
    assert REMOTE_CONTROL_GUARD == {"disableRemoteControl": True, "remoteControlAtStartup": False}


def test_default_path_is_the_default_profile_settings(temp_home):
    assert guard_settings_path() == Path.home() / ".claude" / "settings.json"


class TestGaps:
    def test_missing_file_is_all_gaps(self, tmp_path):
        assert remote_control_guard_gaps(tmp_path / "settings.json") == [
            "disableRemoteControl", "remoteControlAtStartup",
        ]

    def test_wrong_values_are_gaps(self, tmp_path):
        p = tmp_path / "settings.json"
        p.write_text(json.dumps({"disableRemoteControl": False, "remoteControlAtStartup": True}))
        assert remote_control_guard_gaps(p) == ["disableRemoteControl", "remoteControlAtStartup"]

    def test_satisfied(self, tmp_path):
        p = tmp_path / "settings.json"
        p.write_text(json.dumps({"theme": "dark", **REMOTE_CONTROL_GUARD}))
        assert remote_control_guard_gaps(p) == []

    def test_unparseable_file_counts_as_gaps_not_a_crash(self, tmp_path):
        p = tmp_path / "settings.json"
        p.write_text("{not json")
        assert remote_control_guard_gaps(p) == ["disableRemoteControl", "remoteControlAtStartup"]


class TestEnforce:
    def test_creates_the_file_when_absent(self, tmp_path):
        p = tmp_path / ".claude" / "settings.json"
        assert enforce_remote_control_guard(p) == ["disableRemoteControl", "remoteControlAtStartup"]
        assert json.loads(p.read_text()) == REMOTE_CONTROL_GUARD
        assert remote_control_guard_gaps(p) == []

    def test_merges_and_keeps_other_keys(self, tmp_path):
        p = tmp_path / "settings.json"
        p.write_text(json.dumps({"theme": "dark", "env": {"A": "1"}, "remoteControlAtStartup": False}))
        assert enforce_remote_control_guard(p) == ["disableRemoteControl"]
        data = json.loads(p.read_text())
        assert data["theme"] == "dark" and data["env"] == {"A": "1"}
        assert data["disableRemoteControl"] is True and data["remoteControlAtStartup"] is False

    def test_noop_when_already_satisfied_leaves_bytes_alone(self, tmp_path):
        p = tmp_path / "settings.json"
        raw = '{"theme": "dark", "disableRemoteControl": true, "remoteControlAtStartup": false}'
        p.write_text(raw)
        assert enforce_remote_control_guard(p) == []
        assert p.read_text() == raw

    def test_refuses_to_clobber_an_unparseable_file(self, tmp_path):
        p = tmp_path / "settings.json"
        p.write_text("{not json")
        with pytest.raises(PoolError, match="settings.json"):
            enforce_remote_control_guard(p)
        assert p.read_text() == "{not json"

    def test_refuses_a_non_object_file(self, tmp_path):
        p = tmp_path / "settings.json"
        p.write_text("[1, 2]")
        with pytest.raises(PoolError, match="settings.json"):
            enforce_remote_control_guard(p)
