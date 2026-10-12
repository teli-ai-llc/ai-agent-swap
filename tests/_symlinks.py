"""Skip marker for tests that create symlinks.

Windows refuses ``os.symlink`` (WinError 1314) unless Developer Mode is on or
the process is elevated. GitHub's Windows runners are elevated, so these tests
run in CI; a stock developer laptop is not, and there they skip instead of
failing on setup.
"""

import os
import tempfile

import pytest


def _can_symlink() -> bool:
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "target")
        open(target, "w").close()
        try:
            os.symlink(target, os.path.join(d, "link"))
        except (OSError, NotImplementedError):
            return False
        return True


requires_symlinks = pytest.mark.skipif(
    not _can_symlink(), reason="this OS user may not create symlinks (Windows without Developer Mode)"
)
