"""XDG-derived filesystem locations, all namespaced by APP_NAME."""
from __future__ import annotations

import os
from pathlib import Path

from voice import APP_NAME


def _xdg(var: str, default: Path) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else default


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_dir() -> Path:
    return _ensure(_xdg("XDG_CONFIG_HOME", Path.home() / ".config") / APP_NAME)


def state_dir() -> Path:
    return _ensure(_xdg("XDG_STATE_HOME", Path.home() / ".local" / "state") / APP_NAME)


def cache_dir() -> Path:
    return _ensure(_xdg("XDG_CACHE_HOME", Path.home() / ".cache") / APP_NAME)


def runtime_dir() -> Path:
    return _xdg("XDG_RUNTIME_DIR", Path("/tmp"))


def config_file() -> Path:
    return config_dir() / "config.toml"


def history_file() -> Path:
    return state_dir() / "history.jsonl"


def portal_token_file() -> Path:
    return state_dir() / "portal-token"


def socket_path() -> Path:
    return runtime_dir() / f"{APP_NAME}.sock"
