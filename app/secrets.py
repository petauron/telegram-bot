"""Read explicit container credential files without exposing them in Docker Env."""
from __future__ import annotations

import os
from pathlib import Path


def read_setting(name: str, default: str = "") -> str:
    location = os.getenv(f"{name}_FILE", "")
    if not location:
        return os.getenv(name, default)
    if os.getenv(name):
        raise ValueError(f"Configure only {name} or {name}_FILE")
    path = Path(location)
    if not path.is_absolute():
        raise ValueError(f"{name}_FILE must be an absolute credential path")
    with path.open("r", encoding="utf-8") as stream:
        value = stream.read(16385)
    if len(value) > 16384:
        raise ValueError(f"{name}_FILE is too large")
    return value.rstrip("\r\n")
