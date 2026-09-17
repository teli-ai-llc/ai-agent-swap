"""pool_session.json and machine_id in the backup root."""

from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

from claude_swap.pool.session import (
    MACHINE_ID_FILENAME,
    POOL_SESSION_FILENAME,
    PoolSession,
    clear_session,
    load_session,
    machine_id,
    save_session,
)


def _session(**overrides) -> PoolSession:
    base = dict(
        url="https://abc.supabase.co", anon_key="anon", access_token="at",
        refresh_token="rt", expires_at=1_800_000_000.0, user_id="u1",
        email="me@example.com",
    )
    base.update(overrides)
    return PoolSession(**base)


class TestSessionFile:
    def test_missing_is_none(self, tmp_path):
        assert load_session(tmp_path) is None

    def test_round_trip_and_mode(self, tmp_path):
        save_session(tmp_path, _session())
        path = tmp_path / POOL_SESSION_FILENAME
        raw = json.loads(path.read_text())
        assert raw["schemaVersion"] == 1
        assert raw["refreshToken"] == "rt"
        assert load_session(tmp_path) == _session()
        if sys.platform != "win32":
            assert oct(path.stat().st_mode & 0o777) == "0o600"

    def test_corrupt_reads_as_none(self, tmp_path):
        (tmp_path / POOL_SESSION_FILENAME).write_text("{not json")
        assert load_session(tmp_path) is None

    def test_missing_field_reads_as_none(self, tmp_path):
        (tmp_path / POOL_SESSION_FILENAME).write_text('{"schemaVersion": 1, "url": "x"}')
        assert load_session(tmp_path) is None

    def test_clear(self, tmp_path):
        assert clear_session(tmp_path) is False
        save_session(tmp_path, _session())
        assert clear_session(tmp_path) is True
        assert load_session(tmp_path) is None


class TestMachineId:
    def test_created_once_and_stable(self, tmp_path):
        first = machine_id(tmp_path)
        uuid.UUID(first)  # valid uuid
        assert machine_id(tmp_path) == first
        assert (tmp_path / MACHINE_ID_FILENAME).read_text().strip() == first
        # No temp file left behind
        assert not list(tmp_path.glob("*.tmp"))

    def test_garbage_file_is_replaced(self, tmp_path):
        (tmp_path / MACHINE_ID_FILENAME).write_text("not-a-uuid\n")
        fresh = machine_id(tmp_path)
        uuid.UUID(fresh)
