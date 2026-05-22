"""OpenViking HTTP API client."""

import asyncio
import logging
from typing import Any, Optional

import httpx

from engine.config import OpenVikingConfig

logger = logging.getLogger(__name__)

MEMORY_URI_PATTERNS = [
    r"^viking:\/\/user\/(?:[^\/]+(?:\/agent\/[^\/]+)?\/)?memories(?:\/|$)",
    r"^viking:\/\/agent\/(?:[^\/]+(?:\/user\/[^\/]+)?\/)?memories(?:\/|$)",
]

USER_STRUCTURE_DIRS = {"memories", "profile.md", ".abstract.md", ".overview.md"}
AGENT_STRUCTURE_DIRS = {
    "memories",
    "skills",
    "instructions",
    "workspaces",
    ".abstract.md",
    ".overview.md",
}

DEFAULT_PHASE2_POLL_TIMEOUT_MS = 300_000
DEFAULT_WAIT_REQUEST_TIMEOUT_MS = 120_000
WAIT_REQUEST_TIMEOUT_BUFFER_MS = 5_000


def _sleep(ms: int) -> None:
    asyncio.sleep(ms / 1000)


def is_memory_uri(uri: str) -> bool:
    import re

    return any(re.match(p, uri) for p in MEMORY_URI_PATTERNS)


class OpenVikingClient:
    """Async HTTP client for all OpenViking APIs."""

    def __init__(self, config: OpenVikingConfig) -> None:
        self.config = config
        self._client = httpx.AsyncClient(
            timeout=config.timeout_ms / 1000,
        )
        self._identity_cache: dict[str, dict] = {}

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.config.api_key:
            headers["X-API-Key"] = self.config.api_key
        if self.config.account_id:
            headers["X-OpenViking-Account"] = self.config.account_id
        if self.config.user_id:
            headers["X-OpenViking-User"] = self.config.user_id
        if self.config.agent_id:
            headers["X-OpenViking-Agent"] = self.config.agent_id
        return headers

    def _parse(self, resp: httpx.Response) -> dict:
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "error":
            err = data.get("error", {})
            code = err.get("code", "")
            message = err.get("message", f"HTTP {resp.status_code}")
            raise RuntimeError(
                f"OpenViking error [{code}]: {message}"
            )
        return data.get("result", data)

    async def health_check(self) -> dict:
        """GET /health"""
        resp = await self._client.get(
            f"{self.config.base_url}/health"
        )
        return self._parse(resp)

    async def get_runtime_identity(self, agent_id: Optional[str] = None) -> dict:
        """GET /api/v1/system/status — returns {userId, agentId}."""
        effective_agent_id = agent_id or self.config.agent_id
        cached = self._identity_cache.get(effective_agent_id)
        if cached:
            return cached

        fallback = {
            "userId": "default",
            "agentId": effective_agent_id or "default",
        }
        try:
            status = await self._request(
                "/api/v1/system/status", {}, agent_id
            )
            user_id = status.get("user", "")
            if isinstance(user_id, str) and user_id.strip():
                user_id = user_id.strip()
            else:
                user_id = "default"
            identity = {
                "userId": user_id,
                "agentId": effective_agent_id or "default",
            }
            self._identity_cache[effective_agent_id] = identity
            return identity
        except Exception:
            self._identity_cache[effective_agent_id] = fallback
            return fallback

    async def _build_canonical_root(self, scope: str, agent_id: Optional[str] = None) -> str:
        identity = await self.get_runtime_identity(agent_id)
        user_id = identity.get("userId", "default")
        effective_agent_id = agent_id or self.config.agent_id

        if scope == "user":
            if self.config.isolate_user_scope_by_agent:
                return f"viking://user/{user_id}/agent/{effective_agent_id}"
            return f"viking://user/{user_id}"

        if self.config.isolate_agent_scope_by_user:
            return f"viking://agent/{effective_agent_id}/user/{user_id}"
        return f"viking://agent/{effective_agent_id}"

    async def _normalize_target_uri(
        self, target_uri: str, agent_id: Optional[str] = None
    ) -> str:
        import re

        trimmed = target_uri.strip().rstrip("/")
        match = re.match(r"^viking:\/\/(user|agent)(?:\/(.*))?", trimmed)
        if not match:
            return trimmed

        scope = match.group(1)
        raw_rest = (match.group(2) or "").strip()
        if not raw_rest:
            return trimmed

        parts = [p for p in raw_rest.split("/") if p]
        if not parts:
            return trimmed

        reserved_dirs = USER_STRUCTURE_DIRS if scope == "user" else AGENT_STRUCTURE_DIRS
        if parts[0] not in reserved_dirs:
            return trimmed

        root = await self._build_canonical_root(scope, agent_id)
        return f"{root}/{ '/'.join(parts)}"

    async def _request(
        self,
        path: str,
        init: dict[str, Any] = {},
        agent_id: Optional[str] = None,
        request_timeout_ms: Optional[int] = None,
    ) -> dict:
        import time

        effective_agent_id = agent_id or self.config.agent_id
        headers = dict(self._headers())

        body = init.get("body")
        if body and not isinstance(body, (str, bytes)):
            init_body = (
                body
                if isinstance(body, str)
                else __import__("json").dumps(body)
            )
            if "Content-Type" not in headers:
                headers["Content-Type"] = "application/json"
        else:
            init_body = body

        if effective_agent_id:
            headers["X-OpenViking-Agent"] = effective_agent_id

        method = init.get("method", "GET")
        url = f"{self.config.base_url}{path}"
        logger.info(
            "[ov_request] %s %s agent=%s body_len=%s",
            method,
            path,
            effective_agent_id,
            len(init_body) if init_body else 0,
        )

        timeout = (
            request_timeout_ms or self.config.timeout_ms
        ) / 1000
        start = time.monotonic()
        try:
            resp = await self._client.request(
                method=method,
                url=url,
                headers=headers,
                content=init_body,
                timeout=timeout,
            )
            elapsed = time.monotonic() - start
            logger.info(
                "[ov_response] %s %s status=%s time=%.3fs",
                method,
                path,
                resp.status_code,
                elapsed,
            )
            return self._parse(resp)
        except Exception as e:
            elapsed = time.monotonic() - start
            logger.error(
                "[ov_error] %s %s error=%s time=%.3fs",
                method,
                path,
                e,
                elapsed,
            )
            raise

    async def find(
        self,
        query: str,
        target_uri: str,
        limit: int = 10,
        score_threshold: Optional[float] = None,
        agent_id: Optional[str] = None,
    ) -> dict:
        """POST /api/v1/search/find — semantic search."""
        normalized_target_uri = await self._normalize_target_uri(
            target_uri, agent_id
        )
        body = {
            "query": query,
            "target_uri": normalized_target_uri,
            "limit": limit,
            "score_threshold": score_threshold,
        }
        return await self._request(
            "/api/v1/search/find",
            {"method": "POST", "body": body},
            agent_id,
        )

    async def add_session_message(
        self,
        session_id: str,
        role: str,
        parts: list[dict],
        created_at: Optional[str] = None,
        role_id: Optional[str] = None,
    ) -> dict:
        """POST /api/v1/sessions/{id}/messages."""
        body: dict[str, Any] = {"role": role, "parts": parts}
        if created_at:
            body["created_at"] = created_at
        if role_id:
            body["role_id"] = role_id
        return await self._request(
            f"/api/v1/sessions/{session_id}/messages",
            {"method": "POST", "body": body},
        )

    async def get_session(self, session_id: str) -> dict:
        """GET /api/v1/sessions/{id} — returns meta including pending_tokens."""
        return await self._request(
            f"/api/v1/sessions/{session_id}",
            {"method": "GET"},
        )

    async def get_session_context(
        self, session_id: str, token_budget: int = 128_000
    ) -> dict:
        """GET /api/v1/sessions/{id}/context — assembled session context."""
        return await self._request(
            f"/api/v1/sessions/{session_id}/context?token_budget={token_budget}",
            {"method": "GET"},
        )

    async def commit_session(
        self,
        session_id: str,
        wait: bool = False,
        timeout_ms: Optional[int] = None,
    ) -> dict:
        """POST /api/v1/sessions/{id}/commit — archive + memory extraction.

        wait=False: returns immediately after Phase 1.
        wait=True: polls until Phase 2 completes.
        """
        result = await self._request(
            f"/api/v1/sessions/{session_id}/commit",
            {"method": "POST", "body": {}},
        )

        if not wait or not result.get("task_id"):
            return result

        # Client-side poll until Phase 2 finishes
        deadline = asyncio.get_event_loop().time() + (
            timeout_ms or DEFAULT_PHASE2_POLL_TIMEOUT_MS
        ) / 1000
        poll_interval = 0.5

        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(poll_interval)
            try:
                task = await self.get_task(result["task_id"])
            except Exception:
                break

            status = task.get("status", "")
            if status == "completed":
                task_result = task.get("result", {})
                result["status"] = "completed"
                result["memories_extracted"] = task_result.get(
                    "memories_extracted", {}
                )
                return result
            if status == "failed":
                result["status"] = "failed"
                result["error"] = task.get("error", "")
                return result

        result["status"] = "timeout"
        return result

    async def get_task(self, task_id: str) -> dict:
        """GET /api/v1/tasks/{task_id} — poll background task."""
        return await self._request(
            f"/api/v1/tasks/{task_id}",
            {"method": "GET"},
        )

    async def get_session_archive(
        self, session_id: str, archive_id: str
    ) -> dict:
        """GET /api/v1/sessions/{id}/archives/{archiveId}."""
        return await self._request(
            f"/api/v1/sessions/{session_id}/archives/{archive_id}",
            {"method": "GET"},
        )

    async def delete_session(self, session_id: str) -> dict:
        """DELETE /api/v1/sessions/{id}."""
        return await self._request(
            f"/api/v1/sessions/{session_id}",
            {"method": "DELETE"},
        )

    async def delete_uri(self, uri: str) -> dict:
        """DELETE /api/v1/fs?uri=..."""
        import urllib.parse

        encoded = urllib.parse.quote(uri)
        return await self._request(
            f"/api/v1/fs?uri={encoded}&recursive=false",
            {"method": "DELETE"},
        )
