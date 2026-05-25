"""Session bypass pattern matching with glob-like syntax."""

import re


def compile_session_pattern(pattern: str) -> re.Pattern:
    """Compile a glob-like pattern to a regex.

    - Escapes regex special chars
    - ``**`` matches any chars (including ``:``)
    - ``*``  matches any chars except ``:``
    """
    escaped = (
        re.escape(pattern)
        .replace(r"\*\*", "\x00")
        .replace(r"\*", "[^:]*")
        .replace("\x00", ".*")
    )
    return re.compile(f"^{escaped}$")


def compile_session_patterns(patterns: list[str]) -> list[re.Pattern]:
    """Compile a list of glob-like patterns."""
    return [compile_session_pattern(p) for p in patterns if isinstance(p, str) and p.strip()]


def should_bypass_session(session_id: str, patterns: list[re.Pattern]) -> bool:
    """Return True if session_id matches any compiled bypass pattern."""
    if not patterns:
        return False

    candidate = session_id.strip() if isinstance(session_id, str) else ""
    if not candidate:
        return False

    return any(p.match(candidate) for p in patterns)
