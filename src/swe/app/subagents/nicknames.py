# -*- coding: utf-8 -*-
"""Runtime SubAgent nickname assignment."""

from __future__ import annotations

import random

BUILTIN_SUBAGENT_NICKNAMES = (
    "Aristotle",
    "Aquinas",
    "Nash",
    "Huygens",
    "Kierkegaard",
    "Carver",
    "Epicurus",
    "McClintock",
)


def assign_subagent_nickname(configured: str | None = None) -> str:
    """Return a configured nickname or a random runtime display nickname."""
    if configured and configured.strip():
        return configured.strip()
    return random.choice(BUILTIN_SUBAGENT_NICKNAMES)


def allocate_subagent_nickname(base: str, used: set[str]) -> str:
    """Choose the base name or its smallest unused numbered variant."""
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate
