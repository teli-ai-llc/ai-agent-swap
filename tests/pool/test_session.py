"""pool_session.json and machine_id in the backup root."""

from __future__ import annotations

import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from claude_swap.pool.session import (
    MACHINE_ID_FILENAME,
    POOL_SESSION_FILENAME,
    PoolSession,
    clear_session,
    load_session,
    machine_id,
    save_session,
    refresh_session,
)
from claude_swap.exceptions import PoolAuthError


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


class TestRefreshSession:
    def test_overlapping_refreshes_adopt_the_saved_successor(self, tmp_path):
        old = _session(expires_at=0)
        fresh = _session(access_token="next-at", refresh_token="next-rt")
        save_session(tmp_path, old)
        entered = Event()
        release = Event()
        second_started = Event()
        consumed = []

        def ensure_fresh(current):
            if current == fresh:
                return current
            consumed.append(current.refresh_token)
            entered.set()
            assert release.wait(5)
            return fresh

        client = SimpleNamespace(ensure_fresh=ensure_fresh)
        def second():
            second_started.set()
            return refresh_session(tmp_path, client, old)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(refresh_session, tmp_path, client, old)
            try:
                assert entered.wait(5)
                other = executor.submit(second)
                assert second_started.wait(5)
            finally:
                release.set()
            assert first.result(timeout=5) == fresh
            assert other.result(timeout=5) == fresh
        assert consumed == ["rt"]
        assert load_session(tmp_path) == fresh

    def test_old_process_cannot_restore_logout_or_another_login(self, tmp_path):
        old = _session()
        client = SimpleNamespace(ensure_fresh=lambda _: pytest.fail("must not refresh"))
        save_session(tmp_path, old)
        clear_session(tmp_path)
        with pytest.raises(PoolAuthError, match="no stored"):
            refresh_session(tmp_path, client, old)
        assert load_session(tmp_path) is None
        other = _session(user_id="another-member")
        save_session(tmp_path, other)
        with pytest.raises(PoolAuthError, match="changed"):
            refresh_session(tmp_path, client, old)
        assert load_session(tmp_path) == other


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
