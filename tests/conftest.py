from datetime import UTC, datetime
from pathlib import Path

import pytest

from bnpl.ledger.seed import seed_ledger

SEEDED_AT = datetime(2026, 11, 16, 6, 0, tzinfo=UTC)


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.db"
    seed_ledger(path, now=SEEDED_AT)
    return path
