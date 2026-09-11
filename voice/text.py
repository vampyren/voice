"""The dictionary: the vocabulary handed to the model, the replacements applied
to what it wrote, and transcript normalisation."""
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


def _flags(rule) -> set[str]:
    """A rule's optional third element, as a set: "icase", "regex", or neither."""
    if len(rule) <= 2:
        return set()
    return {f.strip().lower() for f in re.split(r"[,\s]+", str(rule[2]))}


#: How much vocabulary goes to the model with one recording. Hotwords share
#: Whisper's <|startofprev|> context with initial_prompt and only the last 224
#: tokens of that context survive, so an over-long list does not add words - it
#: silently pushes the earlier ones out, and the earlier ones are the ones the
#: user wrote first. Rare names cost several tokens each, so 200 characters is
#: roughly 50-70 tokens: a dozen or so terms, with room for a style prompt beside
#: them.
HOTWORDS_MAX_CHARS = 200


def build_hotwords(words, rules, limit: int = HOTWORDS_MAX_CHARS) -> str:
    """The vocabulary to bias the decoder with, as faster-whisper's `hotwords`.

    `dictionary.hotwords` first, then the spelling each replacement aims at - the
    right-hand side is what the user actually wants written, so it is also what
    they want heard, and they maintain one list rather than two. Duplicates go,
    however they are capitalised; order is the order they were written in.

    An entry that would take the string past `limit` is left out whole, never cut
    in half, and the shorter entries after it still get their chance.
    """
    terms = [str(w).strip() for w in (words or [])]
    for rule in rules or []:
        # A regex rule's target is a template ("\1"), not something anyone said,
        # and a rule with no source never fires - apply_replacements skips it too.
        if (isinstance(rule, (list, tuple)) and len(rule) >= 2 and str(rule[0])
                and "regex" not in _flags(rule)):
            terms.append(str(rule[1]).strip())
    kept: list[str] = []
    seen: set[str] = set()
    used = 0
    for term in terms:
        if not term or term.lower() in seen:
            continue
        cost = len(term) + (2 if kept else 0)     # the ", " before it
        if used + cost > limit:
            continue
        seen.add(term.lower())
        kept.append(term)
        used += cost
    return ", ".join(kept)


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
        flags = _flags(rule)
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
