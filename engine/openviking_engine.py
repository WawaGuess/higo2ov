# 相关文档:
#   - docs/架构总览.md（主流程）
#   - docs/features/消息捕获.md
#   - docs/features/会话历史.md
#   - docs/features/记忆召回.md
#   - docs/features/记忆注入.md
#   - docs/features/归档与压缩.md
"""OpenViking memory engine implementation."""

import asyncio
import logging
from datetime import datetime, timezone

from engine.bypass import compile_session_patterns, should_bypass_session
from engine.config import OpenVikingConfig
from engine.diagnostics import emit_diag
from engine.memory import MemoryEngine
from engine.memory_ranking import (
    build_memory_lines_with_budget,
    pick_memories_for_injection,
)
from engine.agent_resolver import AgentResolver
from engine.account_registry import AccountRegistry
from engine.openviking_client import OpenVikingClient
from engine.session_utils import session_to_ov_id
from engine.text_utils import (
    extract_latest_user_text,
    message_to_ov_parts,
    prepare_recall_query,
    sanitize_user_text_for_capture,
)
from utils.token_utils import _count_tokens

logger = logging.getLogger(__name__)


def _is_directory_description(result: dict) -> bool:
    """Check if result is a directory description file (.abstract.md / .overview.md)."""
    uri = result.get("uri", "")
    return uri.endswith(".abstract.md") or uri.endswith(".overview.md")


class OpenVikingMemoryEngine(MemoryEngine):
    """Memory engine backed by OpenViking.

    Simulates OpenClaw-Plugin's assemble + afterTurn + auto-recall lifecycle
    within Higo's single transform callback.
    """

    def __init__(
        self,
        config: OpenVikingConfig,
        admin_client: OpenVikingClient,
        account_registry: AccountRegistry,
    ) -> None:
        self.config = config
        self.admin_client = admin_client
        self.account_registry = account_registry
        self._agent_resolver = AgentResolver(config.agent_id)
        self._bypass_patterns = compile_session_patterns(
            [p.strip() for p in config.bypass_session_patterns.split(",") if p.strip()]
        )
        # Round-id deduplication for result callback idempotency (keep last 1000)
        self._processed_round_ids: set[str] = set()

    async def _resolve_client(self, user_id: str | None) -> OpenVikingClient:
        """Return user-scoped client if user_id given, otherwise fallback to admin."""
        if user_id:
            return await self.account_registry.get_client(user_id)
        return self.admin_client

    def _diag(
        self, stage: str, session_id: str, data: dict
    ) -> None:
        emit_diag(stage, session_id, data, self.config.emit_diagnostics)

    def _resolve_agent_id(self, session_id: str) -> str:
        """Resolve agent ID from sessionId or fall back to config."""
        return self._agent_resolver.resolve(session_id)

    async def generate_memory(
        self, session_id: str, messages: list[dict], model_context_tokens: int = 0, user_id: str | None = None
    ) -> str:
        """Core entry called by main.py's transform handler.

        Steps:
        1. Capture messages to OpenViking session (afterTurn equivalent)
        2. Get session context (assemble equivalent)
        3. Search relevant memories (auto-recall equivalent)
        4. Assemble memory text for injection
        5. Maybe trigger async commit
        """
        import time

        ov_session_id = session_to_ov_id(session_id)

        self._diag(
            "generate_memory_entry",
            ov_session_id,
            {
                "sessionId": session_id,
                "ovSessionId": ov_session_id,
                "msg_count": len(messages),
                "auto_capture": self.config.auto_capture,
                "auto_recall": self.config.auto_recall,
            },
        )

        # Bypass check
        if should_bypass_session(session_id, self._bypass_patterns):
            logger.info(
                "[generate_memory] session bypassed sessionId=%s", session_id
            )
            self._diag(
                "generate_memory_skip",
                ov_session_id,
                {"reason": "session_bypassed", "sessionId": session_id},
            )
            return ""

        start = time.monotonic()
        logger.info(
            "[generate_memory] start sessionId=%s ovSessionId=%s userId=%s msg_count=%s",
            session_id,
            ov_session_id,
            user_id or "(default)",
            len(messages),
        )

        client = await self._resolve_client(user_id)

        # 1. Capture messages
        capture_start = time.monotonic()
        if self.config.auto_capture:
            await self._capture_messages(session_id, ov_session_id, messages, user_id=user_id)
        else:
            logger.info(
                "[generate_memory] auto_capture disabled, skipping capture"
            )
        logger.info(
            "[generate_memory] capture done in %.3fs", time.monotonic() - capture_start
        )

        # 2. Get session context
        ctx_start = time.monotonic()
        context = {}
        try:
            context = await client.get_session_context(ov_session_id)
            overview = context.get("latest_archive_overview", "")[:50]
            abstracts_count = len(context.get("pre_archive_abstracts", []))
            active_msgs = context.get("messages", [])
            logger.info(
                "[generate_memory] context fetched in %.3fs, overview='%s...', abstracts=%s active_messages=%s",
                time.monotonic() - ctx_start,
                overview,
                abstracts_count,
                len(active_msgs),
            )
        except Exception as e:
            logger.warning(
                "[generate_memory] failed to get session context for %s: %s",
                ov_session_id,
                e,
            )

        # 3. Assemble session history from OV context
        session_history = ""
        if context:
            latest_overview = context.get("latest_archive_overview", "")
            pre_archive_abstracts = context.get("pre_archive_abstracts", [])
            active_messages = context.get("messages", [])

            # Exclude the last active message if it matches the current turn
            # (already captured to OV and will be sent by Higo as currentUser)
            if active_messages:
                active_messages = active_messages[:-1]
                logger.info(
                    "[generate_memory] excluded last active message, remaining=%s",
                    len(active_messages),
                )

            # Compute history budget
            history_budget = 0
            if model_context_tokens > 0:
                messages_tokens = sum(
                    _count_tokens(m.get("content", "")) for m in messages
                )
                reserved = 2048
                available = model_context_tokens - messages_tokens - reserved
                history_budget = max(0, int(available * 0.85))
                logger.info(
                    "[generate_memory] history_budget: model=%s messages=%s reserved=%s available=%s history=%s",
                    model_context_tokens,
                    messages_tokens,
                    reserved,
                    available,
                    history_budget,
                )

            session_history = self._assemble_session_history(
                latest_overview,
                pre_archive_abstracts,
                active_messages,
                history_budget,
            )
            if session_history:
                logger.info(
                    "[generate_memory] session_history assembled, length=%s",
                    len(session_history),
                )

        # 4-5. Search and assemble memories (if auto_recall enabled)
        memory_text = ""
        if self.config.auto_recall:
            recall_start = time.monotonic()
            raw_query = extract_latest_user_text(messages)
            query_text = prepare_recall_query(raw_query)
            logger.info("[generate_memory] recall query='%s'", query_text[:100])

            if query_text:
                memories = await self._recall_memories(query_text, user_id=user_id)
                logger.info(
                    "[generate_memory] recall done in %.3fs, memories=%s",
                    time.monotonic() - recall_start,
                    len(memories),
                )

                # 5. Assemble memory text
                effective_budget = self.config.recall_token_budget
                if model_context_tokens > 0:
                    messages_tokens = sum(
                        len(m.get("content", "")) // 4 for m in messages
                    )
                    reserved = 2048
                    available = model_context_tokens - messages_tokens - reserved
                    effective_budget = min(effective_budget, max(0, available))
                    logger.info(
                        "[generate_memory] token_budget adjusted: config=%s model=%s messages=%s reserved=%s effective=%s",
                        self.config.recall_token_budget,
                        model_context_tokens,
                        messages_tokens,
                        reserved,
                        effective_budget,
                    )

                memory_text = self._assemble_memory_text(
                    memories, effective_budget, session_history=session_history
                )
                logger.info(
                    "[generate_memory] assembled memory_text length=%s",
                    len(memory_text) if memory_text else 0,
                )
            else:
                logger.info("[generate_memory] recall query empty, skipping")
        elif session_history:
            # Even without auto_recall, inject session history if available
            memory_text = session_history
            logger.info(
                "[generate_memory] auto_recall disabled, using session_history only, length=%s",
                len(memory_text),
            )
        else:
            logger.info("[generate_memory] auto_recall disabled, skipping recall")

        # 5. Async commit if threshold exceeded
        asyncio.create_task(self._maybe_commit(ov_session_id, user_id=user_id))

        total = time.monotonic() - start
        logger.info(
            "[generate_memory] complete sessionId=%s ovSessionId=%s total_time=%.3fs",
            session_id,
            ov_session_id,
            total,
        )
        self._diag(
            "generate_memory_complete",
            ov_session_id,
            {
                "sessionId": session_id,
                "total_time": total,
                "memory_text_length": len(memory_text) if memory_text else 0,
            },
        )
        return memory_text

    async def _capture_messages(
        self, session_id: str, ov_session_id: str, messages: list[dict], user_id: str | None = None
    ) -> None:
        """Append user message to OpenViking session during transform.

        In Higo V2, transform request only contains the current user input
        (no assistant reply). Assistant capture happens in capture_round_result().

        Classification logic:
        - system          -> merged into current user's parts
        - assistant       -> skipped (captured in result callback)
        - user(context)   -> skipped (not real user speech)
        - user(current)   -> stored as user (with system prefix)
        """
        # 1. Classify messages by role
        system_msg: dict | None = None
        user_msgs: list[dict] = []

        for msg in messages:
            role = msg.get("role", "")
            if role == "system":
                system_msg = msg
            elif role == "user":
                user_msgs.append(msg)

        # 2. Identify current user message
        current_user_msg = user_msgs[-1] if user_msgs else None
        context_env_msgs = user_msgs[:-1] if len(user_msgs) >= 2 else []

        self._diag(
            "capture_classified",
            ov_session_id,
            {
                "system": bool(system_msg),
                "user_total": len(user_msgs),
                "current_user": bool(current_user_msg),
                "context_env": len(context_env_msgs),
            },
        )

        captured = 0
        agent_id = self._resolve_agent_id(session_id)
        client = await self._resolve_client(user_id)

        # 3. Store current user (merge system into parts)
        if current_user_msg:
            parts = message_to_ov_parts(current_user_msg)
            if parts:
                # Merge system content into the first text part
                if system_msg:
                    system_text = system_msg.get("content", "")
                    for part in parts:
                        if part.get("type") == "text":
                            user_text = part.get("text", "")
                            part["text"] = f"[system] {system_text}\n\n{user_text}"
                            break

                # Sanitize
                for part in parts:
                    if part.get("type") == "text" and part.get("text"):
                        part["text"] = sanitize_user_text_for_capture(part["text"])

                if parts and any(p.get("text") for p in parts):
                    try:
                        await client.add_session_message(
                            ov_session_id,
                            role="user",
                            role_id="user",
                            parts=parts,
                            created_at=datetime.now(timezone.utc).isoformat(),
                            
                        )
                        captured += 1
                        logger.info(
                            "[capture] stored current_user ovSessionId=%s parts=%s system_merged=%s",
                            ov_session_id,
                            len(parts),
                            bool(system_msg),
                        )
                    except Exception as e:
                        logger.warning(
                            "[capture] failed to store current_user for %s: %s",
                            ov_session_id,
                            e,
                        )

        logger.info("[capture] total stored=%s", captured)
        self._diag(
            "capture_result",
            ov_session_id,
            {"total_stored": captured, "agent_id": agent_id},
        )

    async def _recall_memories(self, query_text: str, user_id: str | None = None) -> list[dict]:
        """Search memories globally (reference-code style)."""
        if not query_text.strip():
            logger.info("[recall] query is empty, skipping")
            return []

        agent_id = self._resolve_agent_id("")

        logger.info(
            "[recall] query='%s...' limit=20 mode=auto",
            query_text[:80],
        )

        result = await self._safe_find(
            query_text,
            limit=20,
            mode="auto",
            agent_id=agent_id,
            user_id=user_id,
        )
        results = result.get("memories", [])

        # Filter out directory description files (.abstract.md / .overview.md)
        before_filter = len(results)
        results = [
            r for r in results
            if not _is_directory_description(r)
        ]
        if len(results) < before_filter:
            logger.info(
                "[recall] filtered %s directory descriptions",
                before_filter - len(results),
            )

        logger.info("[recall] raw results=%s", len(results))

        # Post-processing
        results = pick_memories_for_injection(
            results,
            self.config.recall_inject_limit,
            query_text,
            self.config.recall_score_threshold,
        )
        logger.info("[recall] after pick=%s", len(results))

        return results

    async def _safe_find(
        self,
        query: str,
        target_uri: str | None = None,
        limit: int = 10,
        score_threshold: float | None = None,
        agent_id: str | None = None,
        mode: str | None = None,
        user_id: str | None = None,
    ) -> dict:
        """Wrapper that catches exceptions."""
        client = await self._resolve_client(user_id)
        try:
            result = await client.find(
                query, target_uri, limit, score_threshold, agent_id, mode
            )
            memories = result.get("memories", [])
            logger.info(
                "[safe_find] uri=%s returned=%s",
                target_uri or "(global)",
                len(memories),
            )
            return result
        except Exception as e:
            logger.warning("[safe_find] error for %s: %s", target_uri or "(global)", e)
            return {}

    @staticmethod
    def _extract_message_text(msg: dict) -> str:
        """Extract text content from an OpenViking message dict."""
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()

        parts = msg.get("parts", [])
        texts: list[str] = []
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text", "")
                if text:
                    texts.append(text)
        return "\n".join(texts) if texts else ""

    def _assemble_session_history(
        self,
        latest_overview: str,
        pre_archive_abstracts: list[str],
        active_messages: list[dict],
        history_budget: int,
    ) -> str:
        """Assemble and truncate session history text from OV context.

        Truncation walks from newest to oldest to preserve recent context,
        then outputs in chronological order (oldest -> newest).
        """
        if history_budget <= 0:
            return ""

        def _estimate(text: str) -> int:
            return max(1, _count_tokens(text))

        # Build formatted entries in chronological order (old -> new)
        all_entries: list[tuple[str, int]] = []

        # Pre-archive abstracts (oldest -> newest, as provided by OV)
        for abstract in pre_archive_abstracts:
            if not abstract or not abstract.strip():
                continue
            text = abstract.strip()[:800]
            fmt = f"[归档摘要]\n{text}"
            all_entries.append((fmt, _estimate(fmt)))

        # Latest overview
        if latest_overview and latest_overview.strip():
            text = latest_overview.strip()[:800]
            fmt = f"[最近归档摘要]\n{text}"
            all_entries.append((fmt, _estimate(fmt)))

        # Active messages (oldest -> newest, as provided by OV)
        for msg in active_messages:
            role = msg.get("role", "unknown")
            content = self._extract_message_text(msg)
            if not content:
                continue
            content = content[:500]
            fmt = f"- {role}: {content}"
            all_entries.append((fmt, _estimate(fmt)))

        if not all_entries:
            return ""

        # Select from newest to oldest (preserve recent context)
        total = 0
        selected: list[str] = []
        for fmt, est in reversed(all_entries):
            if total + est > history_budget:
                break
            selected.insert(0, fmt)
            total += est

        if not selected:
            return ""

        lines = ["<session-history>"]
        lines.extend(selected)
        lines.append("</session-history>")
        return "\n".join(lines)

    def _assemble_memory_text(
        self,
        memories: list[dict],
        token_budget: int,
        session_history: str = "",
    ) -> str:
        """Assemble the memory text block for Higo injection."""
        parts: list[str] = []

        if session_history:
            parts.append(session_history)

        if memories:
            lines = ["<relevant-memories>"]
            memory_lines = build_memory_lines_with_budget(memories, token_budget)
            lines.extend(memory_lines)
            lines.append("</relevant-memories>")
            parts.append("\n".join(lines))

        if not parts:
            return ""

        text = "\n\n".join(parts)
        logger.info(
            "[assemble] memories=%s session_history=%s text_len=%s",
            len(memories),
            bool(session_history),
            len(text),
        )
        return text

    async def _maybe_commit(self, ov_session_id: str, user_id: str | None = None) -> None:
        """Trigger commit if pending_tokens exceeds threshold."""
        client = await self._resolve_client(user_id)
        try:
            session_info = await client.get_session(ov_session_id)
            pending_tokens = session_info.get("pending_tokens", 0)
            logger.info(
                "[commit_check] ovSessionId=%s userId=%s pending_tokens=%s threshold=%s",
                ov_session_id,
                user_id or "(default)",
                pending_tokens,
                self.config.commit_token_threshold,
            )
            if pending_tokens > self.config.commit_token_threshold:
                logger.info(
                    "[commit] triggering ovSessionId=%s userId=%s (pending_tokens=%s > threshold=%s)",
                    ov_session_id,
                    user_id or "(default)",
                    pending_tokens,
                    self.config.commit_token_threshold,
                )
                commit_result = await client.commit_session(
                    ov_session_id, wait=False, 
                )
                logger.info(
                    "[commit] triggered for ovSessionId=%s status=%s archived=%s task_id=%s",
                    ov_session_id,
                    commit_result.get("status", "unknown"),
                    commit_result.get("archived", False),
                    commit_result.get("task_id", "none"),
                )
                self._diag(
                    "commit_triggered",
                    ov_session_id,
                    {
                        "pending_tokens": pending_tokens,
                        "threshold": self.config.commit_token_threshold,
                        "status": commit_result.get("status"),
                        "archived": commit_result.get("archived"),
                        "task_id": commit_result.get("task_id"),
                    },
                )
            else:
                logger.info("[commit] skipped for ovSessionId=%s", ov_session_id)
                self._diag(
                    "commit_skipped",
                    ov_session_id,
                    {
                        "pending_tokens": pending_tokens,
                        "threshold": self.config.commit_token_threshold,
                        "reason": "below_threshold",
                    },
                )
        except Exception as e:
            logger.warning("[commit] check failed for %s: %s", ov_session_id, e)
            self._diag(
                "commit_error",
                ov_session_id,
                {"error": str(e)},
            )

    async def compact(self, session_id: str, user_id: str | None = None) -> dict:
        """Force commit a session and return post-compact summary.

        Returns:
            {
                "ok": bool,
                "compacted": bool,
                "reason": str,
                "result": {
                    "summary": str,
                    "firstKeptEntryId": str,
                    "tokensBefore": int | None,
                    "tokensAfter": int | None,
                }
            }
        """
        ov_session_id = session_to_ov_id(session_id)
        client = await self._resolve_client(user_id)

        self._diag(
            "compact_entry",
            ov_session_id,
            {"sessionId": session_id, "ovSessionId": ov_session_id},
        )

        if should_bypass_session(session_id, self._bypass_patterns):
            logger.info("[compact] session bypassed sessionId=%s", session_id)
            return {
                "ok": True,
                "compacted": False,
                "reason": "session_bypassed",
                "result": {
                    "summary": "",
                    "firstKeptEntryId": "",
                    "tokensBefore": None,
                    "tokensAfter": None,
                },
            }

        # Pre-commit token estimate
        tokens_before: int | None = None
        try:
            pre_ctx = await client.get_session_context(ov_session_id)
            estimated = pre_ctx.get("estimatedTokens")
            if isinstance(estimated, (int, float)) and estimated > 0:
                tokens_before = int(estimated)
        except Exception as e:
            logger.info(
                "[compact] pre-commit context fetch failed for %s: %s",
                ov_session_id,
                e,
            )

        try:
            logger.info(
                "[compact] committing ovSessionId=%s userId=%s (wait=true)",
                ov_session_id,
                user_id or "(default)",
            )
            commit_result = await client.commit_session(
                ov_session_id, wait=True, 
            )

            mem_count = 0
            extracted = commit_result.get("memories_extracted", {})
            if isinstance(extracted, dict):
                mem_count = sum(len(v) for v in extracted.values() if isinstance(v, list))

            logger.info(
                "[compact] committed ovSessionId=%s archived=%s memories=%s task_id=%s",
                ov_session_id,
                commit_result.get("archived", False),
                mem_count,
                commit_result.get("task_id", "none"),
            )

            if commit_result.get("status") == "failed":
                self._diag(
                    "compact_result",
                    ov_session_id,
                    {
                        "ok": False,
                        "compacted": False,
                        "reason": "commit_failed",
                        "error": commit_result.get("error", ""),
                    },
                )
                return {
                    "ok": False,
                    "compacted": False,
                    "reason": "commit_failed",
                    "result": {
                        "summary": "",
                        "firstKeptEntryId": "",
                        "tokensBefore": tokens_before,
                        "tokensAfter": None,
                    },
                }

            if commit_result.get("status") == "timeout":
                self._diag(
                    "compact_result",
                    ov_session_id,
                    {
                        "ok": False,
                        "compacted": False,
                        "reason": "commit_timeout",
                    },
                )
                return {
                    "ok": False,
                    "compacted": False,
                    "reason": "commit_timeout",
                    "result": {
                        "summary": "",
                        "firstKeptEntryId": "",
                        "tokensBefore": tokens_before,
                        "tokensAfter": None,
                    },
                }

            if not commit_result.get("archived"):
                self._diag(
                    "compact_result",
                    ov_session_id,
                    {
                        "ok": True,
                        "compacted": False,
                        "reason": "commit_no_archive",
                        "memories": mem_count,
                    },
                )
                return {
                    "ok": True,
                    "compacted": False,
                    "reason": "commit_no_archive",
                    "result": {
                        "summary": "",
                        "firstKeptEntryId": "",
                        "tokensBefore": tokens_before,
                        "tokensAfter": tokens_before,
                    },
                }

            # Fetch post-compact context for summary
            summary = ""
            tokens_after: int | None = None
            first_kept_entry_id = ""

            try:
                post_ctx = await client.get_session_context(ov_session_id)
                overview = post_ctx.get("latest_archive_overview", "")
                if isinstance(overview, str):
                    summary = overview.strip()
                estimated = post_ctx.get("estimatedTokens")
                if isinstance(estimated, (int, float)) and estimated > 0:
                    tokens_after = int(estimated)
                archive_uri = commit_result.get("archive_uri", "")
                if archive_uri:
                    first_kept_entry_id = archive_uri.split("/")[-1]
            except Exception as e:
                logger.info(
                    "[compact] post-commit context fetch failed for %s: %s",
                    ov_session_id,
                    e,
                )

            self._diag(
                "compact_result",
                ov_session_id,
                {
                    "ok": True,
                    "compacted": True,
                    "reason": "commit_completed",
                    "memories": mem_count,
                    "tokensBefore": tokens_before,
                    "tokensAfter": tokens_after,
                    "latestArchiveId": first_kept_entry_id or None,
                    "summaryPresent": bool(summary),
                },
            )

            return {
                "ok": True,
                "compacted": True,
                "reason": "commit_completed",
                "result": {
                    "summary": summary,
                    "firstKeptEntryId": first_kept_entry_id,
                    "tokensBefore": tokens_before,
                    "tokensAfter": tokens_after,
                },
            }

        except Exception as e:
            logger.warning("[compact] failed for %s: %s", ov_session_id, e)
            self._diag(
                "compact_error",
                ov_session_id,
                {"error": str(e)},
            )
            return {
                "ok": False,
                "compacted": False,
                "reason": "commit_error",
                "result": {
                    "summary": "",
                    "firstKeptEntryId": "",
                    "tokensBefore": tokens_before,
                    "tokensAfter": None,
                },
            }

    async def capture_round_result(
        self, session_id: str, sections: list[dict], round_id: str = "", user_id: str | None = None
    ) -> int:
        """Capture assistant reply and tool results from round sections.

        Called by the result callback at the end of a round.
        Returns the number of messages captured.
        """
        ov_session_id = session_to_ov_id(session_id)
        agent_id = self._resolve_agent_id(session_id)

        # Idempotency: skip if this roundId was already processed
        if round_id and round_id in self._processed_round_ids:
            logger.info(
                "[capture_result] roundId=%s already processed, skipping",
                round_id,
            )
            return 0
        if round_id:
            self._processed_round_ids.add(round_id)
            # Keep set bounded to ~1000 entries
            if len(self._processed_round_ids) > 1000:
                self._processed_round_ids = set(list(self._processed_round_ids)[-500:])

        self._diag(
            "capture_round_result_entry",
            ov_session_id,
            {
                "sessionId": session_id,
                "roundId": round_id,
                "section_count": len(sections),
            },
        )

        captured = 0
        client = await self._resolve_client(user_id)
        for section in sections:
            sec_type = section.get("type", "")

            if sec_type == "content" and section.get("content"):
                text = sanitize_user_text_for_capture(section["content"])
                if not text:
                    continue
                try:
                    await client.add_session_message(
                        ov_session_id,
                        role="assistant",
                        role_id="assistant",
                        parts=[{"type": "text", "text": text}],
                        created_at=datetime.now(timezone.utc).isoformat(),
                    )
                    captured += 1
                    logger.info(
                        "[capture_result] stored assistant ovSessionId=%s",
                        ov_session_id,
                    )
                except Exception as e:
                    logger.warning(
                        "[capture_result] failed to store assistant: %s", e
                    )

            elif sec_type == "tool":
                parts = [
                    {
                        "type": "tool",
                        "tool_id": section.get("toolCallId"),
                        "tool_name": section.get("toolname"),
                        "tool_input": section.get("toolargs"),
                        "tool_output": section.get("toolrsp"),
                    }
                ]
                try:
                    await client.add_session_message(
                        ov_session_id,
                        role="user",
                        role_id="user",
                        parts=parts,
                        created_at=datetime.now(timezone.utc).isoformat(),
                    )
                    captured += 1
                    logger.info(
                        "[capture_result] stored tool result ovSessionId=%s tool=%s",
                        ov_session_id,
                        section.get("toolname"),
                    )
                except Exception as e:
                    logger.warning(
                        "[capture_result] failed to store tool: %s", e
                    )

        logger.info(
            "[capture_result] total stored=%s roundId=%s",
            captured,
            round_id,
        )
        self._diag(
            "capture_round_result_complete",
            ov_session_id,
            {"captured": captured, "agent_id": agent_id, "roundId": round_id},
        )
        return captured
