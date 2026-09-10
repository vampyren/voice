"""Microphone capture through a pw-record subprocess (PipeWire does the resampling)."""
from __future__ import annotations

import json
import logging
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable

import numpy as np

from voice.audio.level import rms_level
from voice.audio.pcm import SAMPLE_RATE, from_bytes

log = logging.getLogger(__name__)


class RecorderError(RuntimeError):
    pass


def pw_record_command(device: str | None) -> list[str]:
    cmd = ["pw-record", "--rate", str(SAMPLE_RATE), "--channels", "1", "--format", "s16"]
    if device:
        cmd += ["--target", device]
    return cmd + ["-"]


@dataclass(frozen=True)
class Source:
    name: str
    description: str
    is_default: bool


def list_sources(run: Callable = subprocess.run) -> list[Source]:
    try:
        cp = run(["pw-dump"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("pw-dump failed: %s", exc)
        return []
    if cp.returncode != 0:
        return []
    try:
        objects = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return []
    default = None
    for obj in objects:
        if obj.get("type") == "PipeWire:Interface:Metadata":
            for item in obj.get("metadata", []):
                if item.get("key") == "default.audio.source":
                    default = (item.get("value") or {}).get("name")
    sources = []
    for obj in objects:
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("media.class") == "Audio/Source":
            name = props.get("node.name", "")
            sources.append(Source(name, props.get("node.description", name), name == default))
    return sources


class Recorder:
    def __init__(self, popen: Callable = subprocess.Popen,
                 on_level: Callable[[float], None] | None = None):
        """`on_level` receives the 0..1 loudness of each captured chunk.

        It is called on the reader thread, so it must be cheap and must not
        block; the recording overlay uses it to drive the waveform.
        """
        self._popen = popen
        self._on_level = on_level
        self._proc = None
        self._chunks: list[bytes] = []
        self._reader: threading.Thread | None = None
        self._cancelled = False
        self._stopping = False
        self._eof_before_stop = False
        self.error: str | None = None

    @property
    def is_recording(self) -> bool:
        return self._proc is not None

    def start(self, device: str | None) -> None:
        if self._proc is not None:
            return
        self._chunks = []
        self._cancelled = False
        self._stopping = False
        self._eof_before_stop = False
        self.error = None
        try:
            self._proc = self._popen(pw_record_command(device), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            raise RecorderError(f"cannot start pw-record: {exc}") from exc
        self._reader = threading.Thread(target=self._pump, name="pw-record-reader", daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        proc = self._proc
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                # EOF on a process we still consider live, with no stop requested:
                # pw-record died on its own (source unplugged, PipeWire restart).
                if proc is self._proc and not self._stopping:
                    self._eof_before_stop = True
                break
            if not self._cancelled:
                self._chunks.append(chunk)
            self._report_level(chunk)

    def _report_level(self, chunk: bytes) -> None:
        # Display only: a broken overlay pipe or a buggy callback must never
        # cost us audio, so every failure is logged and swallowed here.
        if self._on_level is None:
            return
        try:
            self._on_level(rms_level(chunk))
        except Exception as exc:
            log.warning("on_level callback failed: %s", exc)

    def cancel(self) -> None:
        self._cancelled = True
        self._chunks = []
        self.stop()

    def stop(self) -> np.ndarray:
        proc = self._proc
        if proc is None:
            return np.zeros(0, dtype=np.int16)
        self._stopping = True          # set before terminate: the pump reads it at EOF
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2)   # without this returncode stays None after a kill
            except subprocess.TimeoutExpired:
                log.warning("pw-record did not exit after kill")
        if self._reader:
            self._reader.join(timeout=2)
        rc = proc.returncode
        died_early = self._eof_before_stop and rc not in (0, None)
        if not self._cancelled and (died_early or (rc and not self._chunks)):
            tail = proc.stderr.read().decode(errors="replace")[-400:]
            self.error = tail.strip() or (
                "pw-record exited early" if died_early else f"pw-record exited with {rc}")
            log.warning("pw-record failed: %s", self.error)
        elif rc not in (0, None, -15):
            log.debug("pw-record exited with %s after capturing audio; ignoring", rc)
        self._proc = None
        data = b"" if self._cancelled else b"".join(self._chunks)
        self._chunks = []
        return from_bytes(data)
