"""Where a speech model lives on this machine.

Asked by the transcriber, to decide whether it can load without touching the
network, and by `voice doctor`, to say what is still to download. One copy, so
they cannot disagree about the same model.
"""
from __future__ import annotations

import os
from pathlib import Path

def hub_repository(model: str) -> str:
    """The repository faster-whisper will really fetch `model` from.

    The short names we ship are aliases: faster-whisper maps `large-v3-turbo`
    to `mobiuslabsgmbh/faster-whisper-large-v3-turbo` and `medium` to
    `Systran/faster-whisper-medium`, and the cache directory is named after the
    repository rather than the alias - so looking up the raw config value here
    reported "not downloaded yet" over a model that had been on disk all along,
    for the shipped default profile.

    faster-whisper's own table is asked, never copied: the copy `stt/local.py`
    used to keep was wrong, and removing it was right. A build without
    faster-whisper installed gets the name as it stands, which is the answer
    for anything already spelled as a repository.
    """
    try:
        from faster_whisper.utils import _MODELS
    except Exception:                       # not installed, or it moved
        return model
    return _MODELS.get(model, model)


def hub_directory(model: str, root: Path | None = None) -> Path:
    """Where `model` lives on this machine, downloaded or not.

    `root` is `stt.model_dir`. Hugging Face lays its own cache out with a `hub`
    level in it and a directory given to it explicitly without one, so this is
    not the same path with a different prefix.
    """
    name = f"models--{hub_repository(model).replace('/', '--')}"
    if root is not None:
        return Path(root) / name
    home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    return home / "hub" / name


def first_existing(path: Path) -> Path:
    """The nearest ancestor of `path` that exists - what a write would land in."""
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return path


#: What a check can conclude about one model.
UP_TO_DATE = "up to date"
UPDATE_AVAILABLE = "update available"
NOT_DOWNLOADED = "not downloaded"
UNKNOWN = "could not check"


def local_revision(model: str, root: Path | None) -> str | None:
    """The revision of the copy on this machine, or None if there is none.

    huggingface_hub writes the commit it fetched into `refs/main` beside the
    files. Reading it costs nothing and touches no model data.
    """
    try:
        return (hub_directory(model, root) / "refs" / "main").read_text().strip() or None
    except OSError:
        return None


def remote_revision(model: str) -> str:
    """The revision huggingface currently publishes. Metadata only."""
    from huggingface_hub import HfApi

    return HfApi().model_info(hub_repository(model), revision="main").sha


def update_status(models: list[tuple[str, Path | None]]) -> list[tuple[str, str, str | None]]:
    """For each (model, folder): what it is, and the revision published now.

    Downloads nothing - it compares the commit recorded beside the files with
    the one the hub reports. "could not check" is kept distinct from "up to
    date": saying the second when the network is down is how a stale model goes
    unnoticed for ever.
    """
    out = []
    for model, root in models:
        here = local_revision(model, root)
        try:
            there = remote_revision(model)
        except Exception:
            out.append((model, UNKNOWN, None))
            continue
        if here is None:
            out.append((model, NOT_DOWNLOADED, there))
        elif here == there:
            out.append((model, UP_TO_DATE, there))
        else:
            out.append((model, UPDATE_AVAILABLE, there))
    return out

