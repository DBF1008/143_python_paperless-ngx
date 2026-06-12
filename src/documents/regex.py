from __future__ import annotations

import logging
import textwrap

import regex
from django.conf import settings

logger = logging.getLogger("paperless.regex")

REGEX_TIMEOUT_SECONDS: float = getattr(settings, "MATCH_REGEX_TIMEOUT_SECONDS", 0.1)

# Upper bound on document content length fed to content matching, to cap the
# work any single regex (or other matching algorithm) can do on huge inputs.
MATCH_CONTENT_MAX_LENGTH: int = getattr(
    settings,
    "MATCH_CONTENT_MAX_LENGTH",
    1_000_000,
)


def validate_regex_pattern(pattern: str) -> None:
    """
    Validate user provided regex for basic compile errors.
    Raises ValueError on validation failure.
    """

    try:
        regex.compile(pattern)
    except regex.error as exc:
        raise ValueError(exc.msg) from exc


# Escape sequences and character classes are collapsed to a single placeholder
# before unsafe-pattern detection, so their inner ``+``/``*`` (which are
# literals, not quantifiers) cannot trigger a false positive.
_UNSAFE_ESCAPE_SEQUENCE = regex.compile(r"\\.", flags=regex.DOTALL)
_UNSAFE_CHARACTER_CLASS = regex.compile(r"\[[^\]]*\]")
# A group ending in an unbounded quantifier (``*``, ``+`` or ``{n,}``) that is
# itself repeated by an unbounded quantifier -- the textbook catastrophic
# backtracking shape, e.g. ``(a+)+``, ``(\w+)*``, ``(.+)+``, ``(a+)+$``.
_UNSAFE_NESTED_QUANTIFIER = regex.compile(
    r"\((?:\?[:=!]|\?<[=!])?[^()]*?(?:[*+]|\{\d+,\})[^()]*\)(?:[*+]|\{\d+,\})",
)


def validate_regex_safety(pattern: str) -> None:
    """
    Reject regular expressions that contain nested unbounded quantifiers, the
    most common source of catastrophic backtracking (ReDoS). Raises ValueError
    on a likely-unsafe pattern; returns None otherwise.

    Detection is intentionally conservative so legitimate patterns are not
    rejected: escape sequences and character classes are first reduced to a
    placeholder (so e.g. ``([+*])+`` and ``alpha\\w+gamma`` stay safe), then
    only the textbook ``(X+)+`` nesting is flagged. Anything that slips through
    is still bounded at match time by the search timeout.
    """

    skeleton = _UNSAFE_ESCAPE_SEQUENCE.sub("x", pattern)
    skeleton = _UNSAFE_CHARACTER_CLASS.sub("x", skeleton)
    if _UNSAFE_NESTED_QUANTIFIER.search(skeleton):
        raise ValueError(
            "Pattern contains nested unbounded quantifiers that may cause "
            "catastrophic backtracking",
        )


def safe_regex_search(pattern: str, text: str, *, flags: int = 0):
    """
    Run a regex search with a timeout. Returns a match object or None.
    Validation errors, unsafe patterns and timeouts are logged and treated
    as no match.
    """

    try:
        validate_regex_safety(pattern)
    except ValueError as exc:
        logger.warning(
            "Skipping potentially unsafe regular expression %s: %s",
            textwrap.shorten(pattern, width=80, placeholder="…"),
            exc,
        )
        return None

    try:
        validate_regex_pattern(pattern)
        compiled = regex.compile(pattern, flags=flags)
    except (regex.error, ValueError) as exc:
        logger.error(
            "Error while processing regular expression %s: %s",
            textwrap.shorten(pattern, width=80, placeholder="…"),
            exc,
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
    Validation errors, unsafe patterns and timeouts are logged and treated
    as no match.
    """

    try:
        validate_regex_safety(pattern)
    except ValueError as exc:
        logger.warning(
            "Skipping potentially unsafe regular expression %s: %s",
            textwrap.shorten(pattern, width=80, placeholder="…"),
            exc,
        )
        return None

    try:
        validate_regex_pattern(pattern)
        compiled = regex.compile(pattern, flags=flags)
    except (regex.error, ValueError) as exc:
        logger.exception(
            "Error while processing regular expression %s: %s",
            textwrap.shorten(pattern, width=80, placeholder="…"),
            exc,
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
    or None on error/timeout/unsafe pattern.
    """

    try:
        validate_regex_safety(pattern)
    except ValueError as exc:
        logger.warning(
            "Skipping potentially unsafe regular expression %s: %s",
            textwrap.shorten(pattern, width=80, placeholder="…"),
            exc,
        )
        return None

    try:
        validate_regex_pattern(pattern)
        compiled = regex.compile(pattern, flags=flags)
    except (regex.error, ValueError) as exc:
        logger.exception(
            "Error while processing regular expression %s: %s",
            textwrap.shorten(pattern, width=80, placeholder="…"),
            exc,
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
