"""Runtime settings, read from the environment with safe defaults."""

import os
from pathlib import Path

DEFAULT_DB_PATH = Path("data/ledger.db")


def db_path() -> Path:
    """Path of the SQLite ledger, from ``BNPL_DB_PATH`` or ``data/ledger.db``."""
    return Path(os.environ.get("BNPL_DB_PATH", DEFAULT_DB_PATH))


def load_env_file(path: Path = Path(".env")) -> None:
    """Load ``KEY=value`` lines from a local ``.env`` without overriding real env vars."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value.strip():
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))
