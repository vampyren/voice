"""Post-processing of transcripts: dictionary replacements and normalisation."""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)
_QUOTES = str.maketrans({"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'"})


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.translate(_QUOTES)).strip()


def _is_word_char(ch: str) -> bool:
    return bool(ch) and re.match(r"\w", ch) is not None


def _literal_pattern(src: str) -> str:
    r"""`src` escaped, guarded only on the edges where a word boundary can apply.

    \b between two non-word characters never matches, so plain \b...\b silently
    dropped every phrase starting or ending in punctuation ("c++", "obs bot.").
    """
    left = r"(?<!\w)" if _is_word_char(src[:1]) else ""
    right = r"(?!\w)" if _is_word_char(src[-1:]) else ""
    return f"{left}{re.escape(src)}{right}"


def apply_replacements(text: str, rules: list[list]) -> str:
    for rule in rules or []:
        if len(rule) < 2:
            continue
        src, dst = str(rule[0]), str(rule[1])
        if not src:
            # An empty pattern matches at every position: it would splice `dst`
            # between every character of the transcript.
            log.debug("skipping replacement rule with an empty source")
            continue
        flags = {f.strip().lower() for f in re.split(r"[,\s]+", str(rule[2]))} if len(rule) > 2 else set()
        re_flags = re.IGNORECASE if "icase" in flags else 0
        if "regex" in flags:
            pattern, repl = src, dst
        else:
            # A literal target is text, not an re template: passing it through as a
            # template mangled backslashes and rejected anything looking like \1.
            pattern, repl = _literal_pattern(src), (lambda m, d=dst: d)
        try:
            text = re.sub(pattern, repl, text, flags=re_flags)
        except re.error as exc:
            log.warning("skipping bad replacement %r: %s", src, exc)
    return text
