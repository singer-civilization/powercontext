from __future__ import annotations

import os
from pathlib import Path

HOME_ENV = "RESEARCHVAULT_HOME"
DEFAULT_HOME = ".vault"
ENGINE_DIR = "seekdb-instance"


def home_dir() -> Path:
    """Vault root: $RESEARCHVAULT_HOME, default ./.vault (resolved against cwd)."""
    raw = os.environ.get(HOME_ENV)
    return Path(raw).expanduser().resolve() if raw else Path.cwd() / DEFAULT_HOME


def engine_dir(home: Path | None = None) -> Path:
    return (home or home_dir()) / ENGINE_DIR
