"""Key specifications ("KEY_LEFTMETA+KEY_SPACE") and press/release tracking."""
from __future__ import annotations

from dataclasses import dataclass

from evdev import ecodes

MODIFIER_CODES: frozenset[int] = frozenset({
    ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL, ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT,
    ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT, ecodes.KEY_LEFTMETA, ecodes.KEY_RIGHTMETA,
})


@dataclass(frozen=True)
class KeySpec:
    codes: frozenset[int]
    text: str


def parse_keyspec(text: str) -> KeySpec:
    parts = [p.strip().upper() for p in text.split("+") if p.strip()]
    codes: set[int] = set()
    for part in parts:
        code = ecodes.ecodes.get(part)
        if code is None or not part.startswith(("KEY_", "BTN_")):
            raise ValueError(f"unknown key name {part!r}")
        codes.add(code)
    return KeySpec(frozenset(codes), "+".join(parts))


def keyspec_name(code: int) -> str:
    name = ecodes.KEY.get(code) or ecodes.BTN.get(code)
    if isinstance(name, list):          # some codes have aliases
        name = name[0]
    return name or f"KEY_{code}"


class Tracker:
    def __init__(self, specs: dict[str, KeySpec]):
        self._held: set[int] = set()
        self._active: set[str] = set()
        self._specs: dict[str, KeySpec] = {}
        self.set_specs(specs)

    def set_specs(self, specs: dict[str, KeySpec]) -> None:
        self._specs = {n: s for n, s in specs.items() if s.codes}
        self._active.clear()

    def held(self) -> frozenset[int]:
        return frozenset(self._held)

    def modifiers_held(self) -> bool:
        return bool(self._held & MODIFIER_CODES)

    def feed(self, code: int, value: int) -> list[tuple[str, str]]:
        if value == 2:
            return []
        events: list[tuple[str, str]] = []
        if value == 1:
            self._held.add(code)
            for name, spec in self._specs.items():
                if name not in self._active and spec.codes <= self._held:
                    self._active.add(name)
                    events.append((name, "press"))
        else:
            self._held.discard(code)
            for name in list(self._active):
                if code in self._specs[name].codes:
                    self._active.discard(name)
                    events.append((name, "release"))
        return events
