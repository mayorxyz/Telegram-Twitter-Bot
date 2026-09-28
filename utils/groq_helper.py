"""Groq AI helpers for over-limit tweets."""
from __future__ import annotations

import json
import logging
import os
import re

from utils import config
from utils.formatting import twitter_len, truncate_to_limit

log = logging.getLogger(__name__)

THREAD_PART_TARGET = 250


class GroqError(RuntimeError):
    """Groq is unconfigured, unreachable, or returned an unusable result."""


def _client():
    if not config.groq_configured():
        raise GroqError("GROQ_API_KEY is not set - AI compression is disabled.")
    from groq import AsyncGroq
    return AsyncGroq(api_key=config.GROQ_API_KEY)


async def _complete(messages: list[dict], max_tokens: int = 300) -> str:
    client = _client()
    model = os.environ.get("GROQ_MODEL", "qwen/qwen3.8-27b").strip()
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.3,
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        log.warning("groq completion failed: %s", exc)
        raise GroqError(f"Groq request failed: {exc}") from exc


async def compress_tweet(text: str) -> str:
    text = (text or "").strip()
    if twitter_len(text) <= config.X_CHAR_LIMIT:
        return text

    system = (
        "You are an expert X/Twitter editor. Your job is to condense long posts into punchy, structured tweets "
        "that MUST fit under 270 characters (X/Twitter limit is 280). You MUST preserve the original post's structure "
        "(line breaks, bullet layout), tone, writing style, emojis, key message, and hashtags."
    )

    current = text
    last_overshoot_len = None

    for attempt in range(1, 4):
        target = 250 - (attempt - 1) * 20  # 250, 230, 210

        feedback = ""
        if last_overshoot_len:
            feedback = (
                f"\n⚠️ WARNING: Your previous attempt was {last_overshoot_len} characters long, which EXCEEDED the 280 limit!\n"
                f"You MUST shorten it further. Select or consolidate to 3 key bullet points maximum and keep line breaks short.\n"
            )

        prompt = (
            f"The post below is {twitter_len(current)} characters long. Condense it to strictly under {target} characters.\n"
            f"{feedback}\n"
            f"STRICT RULES:\n"
            f"1. STRUCTURE: Maintain line breaks and bullet/list formatting. Do NOT flatten into a wall of text.\n"
            f"2. BULLETS: If there are many bullet points, select or consolidate the 3-4 most essential ones.\n"
            f"3. STYLE & AIM: Preserve author's voice, tone, key message, emojis, hashtags, and links.\n"
            f"4. LENGTH: Must be under {target} characters total (including spaces/emojis).\n"
            f"5. OUTPUT: Return ONLY the final tweet text. No explanations, intro, or quotes.\n\n"
            f"POST TO COMPRESS:\n{current}"
        )

        try:
            result = _clean_reply(await _complete([
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ], max_tokens=300))
        except (GroqError, Exception) as exc:
            log.warning("compress_tweet error (%s) — trimming", exc)
            return truncate_to_limit(current, config.X_CHAR_LIMIT)

        if result:
            res_len = twitter_len(result)
            if res_len <= config.X_CHAR_LIMIT:
                return result

            if res_len < twitter_len(current):
                current = result
            last_overshoot_len = res_len
            log.info("compress_tweet attempt %d overshoot (%d chars) — retrying", attempt, res_len)

    # Emergency LLM pass if all regular retries overshot 280 chars
    try:
        emergency_prompt = (
            f"EMERGENCY SHORTEN: Condense the text below to 3 short bullet points under 240 characters total. "
            f"Keep line breaks, emojis, hashtags. RETURN ONLY THE TWEET:\n\n{current}"
        )
        final_attempt = _clean_reply(await _complete([
            {"role": "system", "content": "You shorten tweets strictly under 250 characters with line breaks."},
            {"role": "user", "content": emergency_prompt},
        ], max_tokens=200))
        if final_attempt and twitter_len(final_attempt) <= config.X_CHAR_LIMIT:
            return final_attempt
    except Exception:
        pass

    return truncate_to_limit(current, config.X_CHAR_LIMIT)
    
async def make_thread(text: str) -> list[str]:
    """Split text into a numbered thread where each tweet is strictly under 280 chars."""
    text = (text or "").strip()
    if not text:
        raise GroqError("Nothing to split into a thread.")

    system = (
        "You split long X/Twitter posts into threads. Reply with ONLY a JSON "
        "array of strings - no prose, no code fences, no numbering. Each string "
        f"is one tweet, strictly under {THREAD_PART_TARGET} characters (URLs "
        "count as 23), split on natural idea/sentence boundaries without changing "
        "meaning or tone. Preserve hashtags and mentions."
    )
    user = f"Split this into a thread:\n\n{text}"

    parts = await _parse_thread(system, user)
    if parts is None:
        parts = await _parse_thread(
            system,
            "Your previous reply was not valid JSON. Reply with ONLY a JSON "
            "array of strings and nothing else.\n\n" + user,
        )
    if parts is None:
        raise GroqError("Groq could not format the thread as JSON after 2 attempts.")
    return _number_and_guard(parts)


async def _parse_thread(system: str, user: str) -> list[str] | None:
    raw = await _complete(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=1024,
    )
    return _extract_json_array(raw)


def _extract_json_array(raw: str) -> list[str] | None:
    if not raw:
        return None
    cleaned = _clean_reply(raw)
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or not data:
        return None
    parts = [str(x).strip() for x in data if str(x).strip()]
    return parts or None


def _number_and_guard(parts: list[str]) -> list[str]:
    total = len(parts)
    out: list[str] = []
    for i, body in enumerate(parts, start=1):
        body = _strip_leading_numbering(body)
        prefix = f"{i}/{total} "
        line = f"{prefix}{body}".strip()
        if twitter_len(line) > config.X_CHAR_LIMIT:
            allowed = config.X_CHAR_LIMIT - twitter_len(prefix)
            body = truncate_to_limit(body, max(20, allowed))
            line = f"{prefix}{body}".strip()
        out.append(line)
    return out


def _clean_reply(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z0-9_+-]*\n?", "", t)
    t = re.sub(r"\n?```\s*$", "", t).strip()
    if len(t) >= 2 and t[0] in "\"\u201c" and t[-1] in "\"\u201d":
        t = t[1:-1].strip()
    return t


def _strip_leading_numbering(body: str) -> str:
    return re.sub(r"^\s*\d+\s*[/\.]\s*\d*\s*[:\-\)]?\s*", "", body).strip()
