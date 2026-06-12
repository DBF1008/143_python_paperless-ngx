from __future__ import annotations

import logging
import re
import textwrap

import regex
from django.conf import settings

logger = logging.getLogger("paperless.regex")

REGEX_TIMEOUT_SECONDS: float = getattr(settings, "MATCH_REGEX_TIMEOUT_SECONDS", 0.1)

MATCH_CONTENT_MAX_LENGTH: int = int(
    getattr(settings, "MATCH_REGEX_CONTENT_MAX_LENGTH", 5_000_000),
)

# ---------------------------------------------------------------------------
# Unsafe regex pattern detection
# ---------------------------------------------------------------------------
# These patterns detect common constructs that cause catastrophic backtracking
# (ReDoS).  They intentionally target only the most well-known dangerous
# idioms so that normal, legitimate regex patterns are not affected.
# ---------------------------------------------------------------------------

_UNSAFE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Capturing group with outer quantifier containing an inner quantifier:
    #   e.g. (a+)+  (\w+)*  (.{1,10}){2,}
    # This is the single most common source of ReDoS.  Non-capturing groups
    # (?:...) are excluded here because they are often used deliberately for
    # grouping alternations (e.g. (?:foo|bar)+) and do not suffer from the
    # same capture-related ambiguity.  Outer ``?`` is also excluded because
    # a single optional match does not create backtracking ambiguity.
    (
        re.compile(r"\((?!\?)[^()]*(?:[*+]|\{\d+,\d*\})[^()]*\)[*+{]"),
        "capturing group with nested quantifiers may cause catastrophic backtracking",
    ),
    # Quantified group whose inner branch can match empty:
    #   e.g. (a*)+  (\s?)*  (.{0,5})+
    # The engine can iterate the group indefinitely without consuming input.
    (
        re.compile(r"\([^()]*\{0,\d*\}[^()]*\)[*+{]"),
        "quantified group with inner zero-minimum quantifier "
        "may cause catastrophic backtracking",
    ),
    # Quantified group containing an inner ``?`` (zero-or-one):
    #   e.g. (a?)+  (\s?)*  (.?)+
    # Similar to the rule above but catches the ``?`` shorthand form.
    # Non-capturing groups (?:...) are excluded via the same negative
    # lookahead used in the first rule.
    (
        re.compile(r"\((?!\?)[^()]*\?[^()]*\)[*+{]"),
        "quantified group with inner optional element "
        "may cause catastrophic backtracking",
    ),
    # Direct quantifier-on-quantifier (no group between them):
    #   e.g. a**  .*+  \w++  (but also .*{2} which is always a mistake)
    (
        re.compile(r"[*+]\s*[*+{]"),
        "adjacent quantifiers may cause catastrophic backtracking",
    ),
]


def is_unsafe_regex_pattern(pattern: str) -> bool:
    """Check whether *pattern* contains constructs commonly associated with
    catastrophic backtracking (ReDoS).

    This is a lightweight, static heuristic -- it inspects the pattern string
    without executing it.  It is intentionally conservative: a pattern that
    passes this check may still be slow, but the timeout in the ``safe_*``
    functions provides a runtime safety net for those cases.

    Returns ``True`` if the pattern is considered unsafe.
    """
    for unsafe_re, _description in _UNSAFE_PATTERNS:
        if unsafe_re.search(pattern):
            return True
    return False


def _unsafe_regex_reason(pattern: str) -> str | None:
    """Return a human-readable reason why *pattern* is unsafe, or ``None``
    if the pattern passes all static checks."""
    for unsafe_re, description in _UNSAFE_PATTERNS:
        if unsafe_re.search(pattern):
            return description
    return None


def validate_regex_pattern(pattern: str) -> None:
    """
    Validate user provided regex for compile errors and known-unsafe
    constructs (catastrophic backtracking / ReDoS).

    Raises ``ValueError`` when the pattern is syntactically invalid or
    contains constructs that are statically identified as unsafe.

    This function is intended for the **API / serializer boundary** where
    new patterns are accepted from users.  The runtime ``safe_regex_*``
    functions intentionally do NOT call this -- they rely on compile-time
    syntax checks and the timeout mechanism instead, so that patterns
    saved before the unsafe-pattern rules were introduced are not
    silently disabled.
    """

    try:
        regex.compile(pattern)
    except regex.error as exc:
        raise ValueError(exc.msg) from exc

    unsafe_reason = _unsafe_regex_reason(pattern)
    if unsafe_reason is not None:
        raise ValueError(
            f"Potentially unsafe regex pattern: {unsafe_reason}. "
            f"Pattern: {pattern!r}",
        )


def _compile_pattern(pattern: str, *, flags: int = 0):
    """Compile a regex pattern (syntax check only -- does NOT reject
    unsafe patterns).  Returns the compiled pattern or *None* on error.

    Used by the ``safe_regex_*`` runtime wrappers so that patterns saved
    before unsafe-pattern rules were introduced are not silently disabled;
    the timeout remains their safety net.
    """
    try:
        return regex.compile(pattern, flags=flags)
    except regex.error:
        return None


def limit_content_length(
    content: str,
    max_length: int | None = None,
) -> str:
    """Truncate *content* to *max_length* characters if it exceeds the limit.

    Uses ``MATCH_REGEX_CONTENT_MAX_LENGTH`` from Django settings when
    *max_length* is not supplied.  Returns the original string unchanged
    when no truncation is necessary.
    """
    limit = max_length if max_length is not None else MATCH_CONTENT_MAX_LENGTH
    if len(content) > limit:
        logger.debug(
            "Document content truncated from %d to %d characters for matching",
            len(content),
            limit,
        )
        return content[:limit]
    return content


def safe_regex_search(pattern: str, text: str, *, flags: int = 0):
    """
    Run a regex search with a timeout. Returns a match object or None.
    Compile errors and timeouts are logged and treated as no match.

    Note: this function intentionally does NOT reject unsafe patterns --
    the timeout is the runtime safety net.  Unsafe-pattern rejection is
    reserved for ``validate_regex_pattern`` at the API boundary.
    """

    compiled = _compile_pattern(pattern, flags=flags)
    if compiled is None:
        logger.error(
            "Error while compiling regular expression %s",
            textwrap.shorten(pattern, width=80, placeholder="…"),
        )
        return None

    try:
        return compiled.search(text, timeout=REGEX_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning(
            "Regular expression matching timed out for pattern %s",
            textwrap.shorten(pattern, width=80, placeholder="…"),
        )
        return None


def safe_regex_match(pattern: str, text: str, *, flags: int = 0):
    """
    Run a regex match with a timeout. Returns a match object or None.
    Compile errors and timeouts are logged and treated as no match.
    """

    compiled = _compile_pattern(pattern, flags=flags)
    if compiled is None:
        logger.exception(
            "Error while compiling regular expression %s",
            textwrap.shorten(pattern, width=80, placeholder="…"),
        )
        return None

    try:
        return compiled.match(text, timeout=REGEX_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning(
            "Regular expression matching timed out for pattern %s",
            textwrap.shorten(pattern, width=80, placeholder="…"),
        )
        return None


def safe_regex_sub(pattern: str, repl: str, text: str, *, flags: int = 0) -> str | None:
    """
    Run a regex substitution with a timeout. Returns the substituted string,
    or None on error/timeout.
    """

    compiled = _compile_pattern(pattern, flags=flags)
    if compiled is None:
        logger.exception(
            "Error while compiling regular expression %s",
            textwrap.shorten(pattern, width=80, placeholder="…"),
        )
        return None

    try:
        return compiled.sub(repl, text, timeout=REGEX_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning(
            "Regular expression substitution timed out for pattern %s",
            textwrap.shorten(pattern, width=80, placeholder="…"),
        )
        return None


def safe_regex_finditer(compiled_pattern: regex.Pattern, text: str):
    """
    Run regex finditer with a timeout. Yields match objects.
    Stops iteration on timeout.
    """

    try:
        yield from compiled_pattern.finditer(text, timeout=REGEX_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning(
            "Regular expression finditer timed out for pattern %s",
            textwrap.shorten(compiled_pattern.pattern, width=80, placeholder="…"),
        )
        return
