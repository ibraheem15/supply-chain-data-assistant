"""Read tool settings from envs/*.env files, with process environment variables taking priority."""

# Makes every type hint a lazy string, so newer syntax like `X | None` and `dict[str, str]`
# is safe in annotations. Must be the first statement after the module docstring.
from __future__ import annotations

# Standard library imports. `pathlib.Path` is an object-oriented file path (like java.nio.file.Path).
import os
from pathlib import Path

# Module-level constants (UPPER_CASE by convention; Python has no `final`). Together they locate the
# env files, the way a Java app might resolve config paths relative to its jar.
# __file__ is this file's path; .parents[2] walks up two levels: resources -> src -> project root.
# The `/` operator on a Path joins path segments (ROOT / "envs" == ROOT/envs).
ROOT = Path(__file__).resolve().parents[2]
ENV_DIR = ROOT / "envs"
AGENT_ENV_FILE = ENV_DIR / "agent_sqlserver.env"
VECTORDB_ENV_FILE = ENV_DIR / "vectorDB.env"
LLM_ENV_FILE = ENV_DIR / "LLM.env"


# Parse a .env-style file (KEY=VALUE per line) into a dict.
# Input: a Path. Output: dict of names to values; empty dict if the file does not exist.
# Analogy: a tiny hand-rolled java.util.Properties loader. It is used by load_settings below.
def read_env_file(path: Path) -> dict[str, str]:
    """Parse simple KEY=VALUE lines. Missing file means no values; nothing is logged."""
    # A variable annotation: `values` is a dict mapping str to str. Annotations are hints only, not enforced.
    values: dict[str, str] = {}
    if not path.exists():
        return values
    # read_text loads the whole file as a string; splitlines() splits it into a list of lines.
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        # Skip blank lines (empty string is falsy), comment lines, and lines with no '='.
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        # Tuple unpacking: split at the FIRST '=' only, so values containing '=' stay intact.
        key, value = stripped.split("=", maxsplit=1)
        value = value.strip()
        # If the value is wrapped in matching quotes, strip them (`value[-1]` is the last char;
        # `in {...}` is a set membership test).
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            # Slice: drop the first and last character.
            value = value[1:-1]
        values[key.strip()] = value
    return values


# Build the final settings dict used by every tool.
# Inputs: any number of env file paths (`*env_files` collects them into a tuple, like Java varargs) and a
# keyword-only `prefixes` tuple (parameters after `*env_files` can only be passed by name).
# `tuple[str, ...]` means a tuple of any length holding strings.
# Output: merged dict. Precedence: later files beat earlier ones, and real environment variables beat files,
# like Spring's externalized config where env vars override application.properties.
def load_settings(*env_files: Path, prefixes: tuple[str, ...] = ()) -> dict[str, str]:
    """Merge the given env files (later wins). Environment variables override file values,
    and variables starting with one of `prefixes` are included even if no file sets them."""
    merged: dict[str, str] = {}
    for env_file in env_files:
        # dict.update overwrites existing keys, which is why later files win.
        merged.update(read_env_file(env_file))
    # os.environ behaves like a dict of the process environment variables.
    for key, value in os.environ.items():
        # Take an env var if a file already defines it (override) or its name starts with any allowed prefix.
        # str.startswith accepts a tuple of prefixes. An empty tuple matches nothing.
        if key in merged or key.startswith(prefixes):
            merged[key] = value
    return merged
