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
