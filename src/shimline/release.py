"""Release identity loaded from the immutable deployment payload."""
from __future__ import annotations

import json
import os
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parent.parent
MANIFEST_PATH = BACKEND_DIR / "RELEASE.json"
MIGRATIONS_DIR = BACKEND_DIR / "migrations"


def source_schema_target() -> str:
    migrations = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    return migrations[-1].stem if migrations else "none"


def load_manifest() -> dict[str, str]:
    default = {
        "release_id": os.environ.get("SHIMLINE_RELEASE_ID", "development"),
        "git_commit": "uncommitted",
        "expected_schema": source_schema_target(),
        "built_at_utc": "not-packaged",
    }
    if not MANIFEST_PATH.is_file():
        return default
    try:
        raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    for field in default:
        value = raw.get(field)
        if isinstance(value, str) and value.strip():
            default[field] = value.strip()
    return default


MANIFEST = load_manifest()
