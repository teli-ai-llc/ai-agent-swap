from __future__ import annotations

import pytest

from tests.pool.fake_pool import FakePool


@pytest.fixture
def fake_pool() -> FakePool:
    return FakePool()


@pytest.fixture
def owner(fake_pool):
    return fake_pool.add_member("owner@x.io", "pw-owner", role="admin", display_name="Owner")


@pytest.fixture
def borrower(fake_pool):
    return fake_pool.add_member("borrower@x.io", "pw-borrower", display_name="Borrower")
