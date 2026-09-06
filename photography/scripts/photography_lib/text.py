"""Shared Unicode matching for saved display text."""
from __future__ import annotations

import unicodedata


def fold_text(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()
