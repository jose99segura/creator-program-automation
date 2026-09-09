from __future__ import annotations

import pytest

from creator_program import db, obs


@pytest.fixture()
def conn(tmp_path):
    """A fresh database per test.

    On disk rather than in memory because the queue uses BEGIN IMMEDIATE, and
    testing the locking behaviour against a different storage mode than
    production uses would be testing something else.
    """
    connection = db.connect(str(tmp_path / "test.db"))
    db.init(connection)
    obs.reset()
    yield connection
    connection.close()
