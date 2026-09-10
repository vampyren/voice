"""Post-processing of transcripts: dictionary replacements and normalisation."""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)
_QUOTES = str.maketrans({"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'"})


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.translate(_QUOTES)).strip()


def apply_replacements(text: str, rules: list[list]) -> str:
    for rule in rules or []:
        if len(rule) < 2:
            continue
        src, dst = str(rule[0]), str(rule[1])
        flags = {f.strip().lower() for f in re.split(r"[,\s]+", str(rule[2]))} if len(rule) > 2 else set()
        re_flags = re.IGNORECASE if "icase" in flags else 0
        pattern = src if "regex" in flags else rf"\b{re.escape(src)}\b"
        try:
            text = re.sub(pattern, dst, text, flags=re_flags)
        except re.error as exc:
            log.warning("skipping bad replacement %r: %s", src, exc)
    return text
