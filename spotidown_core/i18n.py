from __future__ import annotations

import os

SUPPORTED_LANGUAGES = {"tr", "en"}


def normalize_language(value: str | None) -> str:
    lang = (value or "").strip().lower().replace("_", "-")
    if lang.startswith("en"):
        return "en"
    return "tr"


def language() -> str:
    return normalize_language(os.environ.get("SPOTIDOWN_LANG"))


def set_language(value: str) -> str:
    lang = normalize_language(value)
    os.environ["SPOTIDOWN_LANG"] = lang
    return lang


def bi(tr: str, en: str) -> str:
    return en if language() == "en" else tr
