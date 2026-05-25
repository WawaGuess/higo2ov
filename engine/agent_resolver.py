"""Session-Agent resolver with caching and prefix support."""

import re

_AGENT_ID_FROM_SESSION_RE = re.compile(r"^agent:([^:]+):", re.IGNORECASE)


class AgentResolver:
    """Resolve agent IDs from session identifiers, with per-session caching.

    Supports prefixing: when a config agent_id is set (and not "default"),
    resolved IDs become ``<configAgentId>_<rawAgentId>``.
    """

    def __init__(self, config_agent_id: str = "default") -> None:
        self._config_agent_id = config_agent_id.strip() if config_agent_id else ""
        self._cache: dict[str, str] = {}

    def _extract_from_session_id(self, session_id: str) -> str | None:
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

    def resolve(self, session_id: str) -> str:
        """Resolve the effective agent ID for a given session."""
        sid = session_id.strip() if isinstance(session_id, str) else ""
        if not sid:
            return self._config_agent_id or "default"

        cached = self._cache.get(sid)
        if cached:
            return cached

        raw = self._extract_from_session_id(sid)
        if raw:
            if self._config_agent_id and self._config_agent_id != "default":
                resolved = f"{self._config_agent_id}_{raw}"
            else:
                resolved = raw
        elif self._config_agent_id and self._config_agent_id != "default":
            resolved = self._config_agent_id
        else:
            resolved = "default"

        self._cache[sid] = resolved
        return resolved

    def clear_cache(self) -> None:
        """Clear the resolution cache."""
        self._cache.clear()
