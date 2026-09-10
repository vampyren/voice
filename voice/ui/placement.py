"""Where the recording pill sits.

Nine placements - `top|middle|bottom` crossed with `left|center|right` - plus
two margins that push the pill in from the edges it is anchored to. This module
holds the vocabulary and the arithmetic only, so the config, the daemon's
launcher, the settings dialog and the GTK helper all agree without importing
each other (and without importing `gi`).

An axis whose half is `center` or `middle` is not anchored at all: the
compositor centres the pill on it, and the margin for that axis has nothing to
be a distance from, so it is not applied. `middle-center` is therefore dead
centre, margins or no margins.
"""
from __future__ import annotations

#: The vertical half of a placement, and the edge each anchors to ("" = centred).
VERTICALS = {"top": "top", "middle": "", "bottom": "bottom"}
#: The horizontal half, likewise.
HORIZONTALS = {"left": "left", "center": "", "right": "right"}
#: The nine, in the order the settings dialog and the README list them.
POSITIONS = tuple(f"{v}-{h}" for v in VERTICALS for h in HORIZONTALS)
#: What the two values this setting used to take mean now.
LEGACY_POSITIONS = {"bottom": "bottom-center", "top": "top-center"}
DEFAULT_POSITION = "bottom-center"
DEFAULT_MARGIN_X = 0
DEFAULT_MARGIN_Y = 48
#: A margin is a nudge, not a way to lose the pill off a 4K screen and think it
#: crashed; the settings dialog offers the same range.
MARGIN_LIMIT = 2000


def normalise_position(value: object) -> str:
    """One of the nine, from whatever the config or a command line holds.

    Accepts the two legacy values, and is forgiving about case and spacing.
    Anything else is not a placement at all: `Config.errors()` says so, and the
    pill still appears where it always did rather than not at all.
    """
    text = value.strip().lower() if isinstance(value, str) else ""
    text = LEGACY_POSITIONS.get(text, text)
    return text if text in POSITIONS else DEFAULT_POSITION


def split_position(position: str) -> tuple[str, str]:
    """The vertical and horizontal halves of a placement."""
    vertical, _, horizontal = normalise_position(position).partition("-")
    return vertical, horizontal


def anchors(position: str, margin_x: int = 0, margin_y: int = 0) -> dict[str, int]:
    """The edges this placement anchors to, each with the margin it takes.

    Vertical edge first, so a caller that shows them (a log line, a test) reads
    them in the order the placement is named.
    """
    vertical, horizontal = split_position(position)
    edges: dict[str, int] = {}
    if VERTICALS[vertical]:
        edges[VERTICALS[vertical]] = int(margin_y)
    if HORIZONTALS[horizontal]:
        edges[HORIZONTALS[horizontal]] = int(margin_x)
    return edges


def clamp_margin(value: object, default: int) -> int:
    """A margin in pixels, or `default` when the config holds something else.

    `bool` is an `int` in Python and never a margin, so it is refused here as
    well as by `Config.errors()`.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return default
    return max(-MARGIN_LIMIT, min(MARGIN_LIMIT, value))


def is_margin(value: object) -> bool:
    """Whether `value` is a margin `errors()` should let through."""
    return (isinstance(value, int) and not isinstance(value, bool)
            and -MARGIN_LIMIT <= value <= MARGIN_LIMIT)


def placement_note(position: str, margin_x: int, margin_y: int) -> str | None:
    """What to say when the pill cannot be placed, or None if nothing was asked.

    Without a layer surface the pill is an ordinary window and the compositor
    decides where it goes - GTK 4 has no way to move a toplevel. An owner who
    set a placement is owed that sentence once; one who never touched it is not
    owed a warning about a default.
    """
    if (normalise_position(position) == DEFAULT_POSITION
            and margin_x == DEFAULT_MARGIN_X and margin_y == DEFAULT_MARGIN_Y):
        return None
    return (f"overlay: cannot place the pill at {normalise_position(position)} "
            f"(margins {margin_x}/{margin_y}) - without gtk4-layer-shell it is an "
            "ordinary window and the compositor decides where it goes, so "
            "ui.overlay_position and ui.overlay_margin_x/y do nothing here")
