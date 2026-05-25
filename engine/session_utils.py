"""Session identity mapping and agent resolution utilities."""

import hashlib
import re

# Matches standard UUID format (case-insensitive)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Extract agentId from sessionId with pattern: agent:<agentId>:...
_AGENT_ID_FROM_SESSION_RE = re.compile(r"^agent:([^:]+):", re.IGNORECASE)


def session_to_ov_id(session_id: str) -> str:
    """Map a Higo sessionId to a stable OpenViking session storage ID.

    Logic (mirrors openClawSessionToOvStorageId):
    - If session_id is a valid UUID → use it directly (lowercased).
    - Otherwise → sha256 hash for stability and filesystem safety.
    """
    sid = session_id.strip() if isinstance(session_id, str) else ""
    if not sid:
        return ""

    if _UUID_RE.match(sid):
        return sid.lower()

    # Non-UUID: derive a stable sha256 hex digest
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()


def extract_agent_id_from_session_id(session_id: str) -> str | None:
    """Extract agentId from sessionId when it contains an 'agent:<id>:' prefix."""
    sid = session_id.strip() if isinstance(session_id, str) else ""
    if not sid:
        return None

    match = _AGENT_ID_FROM_SESSION_RE.match(sid)
    if match:
        agent_id = match.group(1).strip()
        if agent_id:
            return agent_id
    return None
