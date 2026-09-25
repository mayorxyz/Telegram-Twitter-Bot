"""Lightweight 'generation' for drafts: rephrase/variant creation without an LLM.

Regenerate keeps the meaning but varies presentation (emojis, hooks, CTAs).
Deterministic seed = post id + attempt counter stored in settings.
"""
from __future__ import annotations

import random
import re

_HOOKS = [
    "👀 Look at this:",
    "🔥 Hot take:",
    "💡 Worth noting:",
    "⚡ Quick one:",
    "🧵 Breaking it down:",
    "📌 Don't sleep on this:",
]

_CTAS = [
    "",
    "\n\nWhat do you think? 👇",
    "\n\nRT if you agree 🔄",
    "\n\nSave this for later 🔖",
    "\n\nAgree or disagree? 💬",
]


def _attempt(post_id: int) -> int:
    from db import crud

    key = f"regen_{post_id}"
    n = int(crud.get_setting(key, "0") or 0) + 1
    crud.set_setting(key, str(n))
    return n


def generate_draft(text: str, post_id: int | None = None) -> str:
    """Return a fresh variant of the given content."""
    body = (text or "").strip()
    if not body:
        return body

    # strip a previously added hook line so variants don't stack
    lines = body.split("\n")
    while lines and any(lines[0].startswith(h.split(" ")[0]) for h in _HOOKS):
        lines = lines[1:]
        if lines and lines[0].strip() == "":
            lines = lines[1:]
    core = "\n".join(lines).strip()
    # strip previous CTA
    for cta in _CTAS:
        c = cta.strip()
        if c and core.endswith(c):
            core = core[: -len(c)].rstrip()

    seed = (post_id or 0) * 1000 + _attempt(post_id or 0) if post_id else random.randrange(10**9)
    rng = random.Random(seed)

    first, *rest = re.split(r"(?<=[.!?])\s+", core) if core else [""]
    out = core
    if rng.random() < 0.7:
        hook = rng.choice(_HOOKS)
        out = f"{hook} {core}"
    out += rng.choice(_CTAS)
    return out.strip()
