"""Last-N dictations for recall and retry; JSONL on disk, audio only in memory."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from voice import paths

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Entry:
    text: str
    ts: float
    backend: str
    audio_s: float
    elapsed_s: float


class History:
    def __init__(self, path: Path | None = None, limit: int = 20):
        self._path = path or paths.history_file()
        self._limit = limit
        self._entries: list[Entry] = self._load()
        self._audio: np.ndarray | None = None

    def _load(self) -> list[Entry]:
        if not self._path.exists():
            return []
        out: list[Entry] = []
        for line in self._path.read_text().splitlines():
            try:
                out.append(Entry(**json.loads(line)))
            except (ValueError, TypeError) as exc:
                log.warning("skipping bad history line: %s", exc)
        return out[-self._limit:]

    def _flush(self) -> None:
        self._path.write_text("".join(json.dumps(asdict(e)) + "\n" for e in self._entries))
        self._path.chmod(0o600)

    def add(self, entry: Entry) -> None:
        self._entries = (self._entries + [entry])[-self._limit:]
        self._flush()

    def last(self) -> Entry | None:
        return self._entries[-1] if self._entries else None

    def entries(self) -> list[Entry]:
        return list(self._entries)

    def keep_audio(self, pcm: np.ndarray) -> None:
        self._audio = pcm

    def take_audio(self) -> np.ndarray | None:
        pcm, self._audio = self._audio, None
        return pcm
