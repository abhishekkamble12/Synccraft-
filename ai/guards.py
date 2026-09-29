"""
Security guardrails, prompt injection sanitization, rate limiting, and token budgets.
"""

from collections import defaultdict
import logging
import re
import time
from typing import Any

from django.core.cache import cache

logger = logging.getLogger(__name__)

# Injection patterns to detect and neutralize
INJECTION_SIGNALS = [
    re.compile(r"ignore\s+(all\s+)?(previous|above|prior)\s+instructions?", re.IGNORECASE),
    re.compile(r"disregard\s+(the\s+)?system\s+prompt", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(an?\s+)?unrestricted", re.IGNORECASE),
    re.compile(r"<\s*/?\s*system\s*>", re.IGNORECASE),
]

MAX_INPUT_CHARS = 25_000
MAX_OUTPUT_CHARS = 50_000

# In-memory fallback rate limiting tracker: user_id -> list[timestamps]
_local_rate_limits: dict[str, list[float]] = defaultdict(list)
_local_token_usage: dict[str, dict[str, int]] = defaultdict(lambda: {"date": 0, "tokens": 0})


def sanitize_document_text(text: str) -> str:
    """
    Sanitize text before embedding in prompt templates.
    Escapes tags that could terminate the untrusted block prematurely.
    """
    if not text:
        return ""
    # Neutralize closing delimiter tag injections
    sanitized = text.replace("</document_text>", "&lt;/document_text&gt;")
    sanitized = sanitized.replace("<document_text>", "&lt;document_text&gt;")
    return sanitized


def detect_prompt_injection(instruction: str) -> bool:
    """
    Returns True if malicious prompt override attempts are detected.
    """
    for pattern in INJECTION_SIGNALS:
        if pattern.search(instruction):
            return True
    return False


def validate_input_bounds(
    text: str, instruction: str = "", max_chars: int = MAX_INPUT_CHARS
) -> tuple[bool, str]:
    """
    Validate input text and instructions conform to length and security bounds.
    """
    if len(text) > max_chars:
        return False, f"Input text length ({len(text)} chars) exceeds maximum allowed ({max_chars} chars)."

    if len(instruction) > 2000:
        return False, f"Instruction length ({len(instruction)} chars) exceeds maximum allowed (2000 chars)."

    if detect_prompt_injection(instruction):
        logger.warning("Potential prompt injection pattern detected in user instruction.")
        # We flag/neutralize rather than completely blocking harmless queries,
        # but if instruction contains explicit prompt breach commands we reject.
        return False, "Instruction contained suspicious override patterns and was blocked."

    return True, ""


def validate_output_bounds(raw_output: str, max_chars: int = MAX_OUTPUT_CHARS) -> tuple[bool, str]:
    """
    Sanitize and validate LLM output text.
    """
    cleaned = raw_output.strip()
    if not cleaned:
        return False, "LLM returned empty completion."

    if len(cleaned) > max_chars:
        return False, f"LLM output exceeded safety bounds ({len(cleaned)} chars)."

    # Strip accidental code-block wrapping if the model enclosed response in backticks
    if cleaned.startswith("```") and cleaned.endswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 2:
            cleaned = "\n".join(lines[1:-1]).strip()

    return True, cleaned


def check_rate_limit(
    user_id: int | str,
    max_requests_per_minute: int = 30,
) -> tuple[bool, int]:
    """
    Rate limit check using sliding window.
    Returns (is_allowed, remaining_requests).
    """
    key = f"rl:ai:{user_id}"
    now = time.time()
    window = 60.0

    try:
        # Attempt Redis cache rate limiting
        timestamps = cache.get(key)
        if timestamps is None:
            timestamps = []
        # Filter timestamps within window
        valid_ts = [ts for ts in timestamps if now - ts < window]
        if len(valid_ts) >= max_requests_per_minute:
            return False, 0

        valid_ts.append(now)
        cache.set(key, valid_ts, timeout=int(window) + 5)
        return True, max_requests_per_minute - len(valid_ts)
    except Exception:
        # Fallback to in-memory sliding window
        ts_list = _local_rate_limits[str(user_id)]
        valid_ts = [ts for ts in ts_list if now - ts < window]
        _local_rate_limits[str(user_id)] = valid_ts

        if len(valid_ts) >= max_requests_per_minute:
            return False, 0

        valid_ts.append(now)
        return True, max_requests_per_minute - len(valid_ts)


def check_and_increment_token_budget(
    user_id: int | str,
    estimated_tokens: int,
    daily_budget: int = 150_000,
) -> bool:
    """
    Ensure user has not exceeded their daily token allotment.
    """
    today_epoch = int(time.time() // 86400)
    key = f"tb:ai:{user_id}:{today_epoch}"

    try:
        used = cache.get(key, 0)
        if used + estimated_tokens > daily_budget:
            return False
        cache.set(key, used + estimated_tokens, timeout=86400 * 2)
        return True
    except Exception:
        rec = _local_token_usage[str(user_id)]
        if rec["date"] != today_epoch:
            rec["date"] = today_epoch
            rec["tokens"] = 0

        if rec["tokens"] + estimated_tokens > daily_budget:
            return False

        rec["tokens"] += estimated_tokens
        return True
