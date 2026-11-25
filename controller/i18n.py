"""Two-language messages (Arabic / English).

Every user-facing string is written as tr("English", "العربية") right where it
is used, so the code stays readable and adding a translation never needs a
separate key table.
"""

from __future__ import annotations

import re

_LANG = "ar"
_BRAND = ""  # display name shown instead of "Claude" (config: branding.name); empty = the real name
SUPPORTED = ("ar", "en")


def set_language(lang: str) -> None:
    global _LANG
    if lang in SUPPORTED:
        _LANG = lang


def language() -> str:
    return _LANG


def set_brand(name: str) -> None:
    global _BRAND
    _BRAND = (name or "").strip()


def brand_name() -> str:
    return _BRAND


_MODEL_RE = re.compile(r"\bclaude(?=[-_ .]?(?:\d|opus|sonnet|haiku|fable|mythos|instant|code|v\d))", re.I)


def brand(text: str) -> str:
    """Show the display name instead of the real one, in our own wording (not in commands or paths:
    `allinone install claude`, the `claude` program and claude/ folders keep their real names)."""
    if not _BRAND or not text:
        return text
    text = re.sub(r"\bClaude\.ai\b", _BRAND + (" (ويب)" if _LANG == "ar" else " (web)"), text)
    text = re.sub(r"\b(Claude|Anthropic)\b", _BRAND, text)
    return text


def mask_models(text: str) -> str:
    """Model names too: claude-sonnet-4-5 → smarter-sonnet-4-5 (for display only)."""
    if not _BRAND or not text:
        return text
    return re.sub(r"\banthropic/", _BRAND.lower() + "/", _MODEL_RE.sub(_BRAND.lower(), text), flags=re.I)


def tr(en: str, ar: str, **kwargs) -> str:
    text = brand(ar if _LANG == "ar" else en)  # our words only; values (paths, names) are not changed
    return text.format(**kwargs) if kwargs else text
