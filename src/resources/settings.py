"""Read tool settings from envs/*.env files, with process environment variables taking priority."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_DIR = ROOT / "envs"
AGENT_ENV_FILE = ENV_DIR / "agent_sqlserver.env"
VECTORDB_ENV_FILE = ENV_DIR / "vectorDB.env"
LLM_ENV_FILE = ENV_DIR / "LLM.env"


def read_env_file(path: Path) -> dict[str, str]:
    """Parse simple KEY=VALUE lines. Missing file means no values; nothing is logged."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", maxsplit=1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_settings(*env_files: Path, prefixes: tuple[str, ...] = ()) -> dict[str, str]:
    """Merge the given env files (later wins). Environment variables override file values,
    and variables starting with one of `prefixes` are included even if no file sets them."""
    merged: dict[str, str] = {}
    for env_file in env_files:
        merged.update(read_env_file(env_file))
    for key, value in os.environ.items():
        if key in merged or key.startswith(prefixes):
            merged[key] = value
    return merged
