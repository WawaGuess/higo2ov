"""OpenViking memory engine implementation."""

import asyncio
import logging
from datetime import datetime, timezone

from engine.config import OpenVikingConfig
from engine.memory import MemoryEngine
from engine.memory_ranking import (
    apply_score_threshold,
    build_memory_lines_with_budget,
    deduplicate_by_uri,
    filter_leaf_memories,
    rerank_memories,
)
from engine.openviking_client import OpenVikingClient
from engine.text_utils import (
    extract_latest_user_text,
    message_to_ov_parts,
    sanitize_user_text_for_capture,
)

logger = logging.getLogger(__name__)


class OpenVikingMemoryEngine(MemoryEngine):
    """Memory engine backed by OpenViking.

    Simulates OpenClaw-Plugin's assemble + afterTurn + auto-recall lifecycle
    within Higo's single transform callback.
    """

    def __init__(
        self, config: OpenVikingConfig, client: OpenVikingClient
    ) -> None:
        self.config = config
        self.client = client

    async def generate_memory(
        self, session_id: str, messages: list[dict]
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

        start = time.monotonic()
        logger.info("[generate_memory] start sessionId=%s msg_count=%s", session_id, len(messages))

        # 1. Capture messages
        capture_start = time.monotonic()
        await self._capture_messages(session_id, messages)
        logger.info("[generate_memory] capture done in %.3fs", time.monotonic() - capture_start)

        # 2. Get session context
        ctx_start = time.monotonic()
        try:
            context = await self.client.get_session_context(session_id)
            overview = context.get("latest_archive_overview", "")[:50]
            abstracts_count = len(context.get("pre_archive_abstracts", []))
            logger.info(
                "[generate_memory] context fetched in %.3fs, overview='%s...', abstracts=%s",
                time.monotonic() - ctx_start,
                overview,
                abstracts_count,
            )
        except Exception as e:
            logger.warning(
                "[generate_memory] failed to get session context for %s: %s", session_id, e
            )
            context = {}

        # 3. Search relevant memories
        recall_start = time.monotonic()
        query_text = extract_latest_user_text(messages)
        logger.info("[generate_memory] recall query='%s'", query_text[:100])
        memories = await self._recall_memories(query_text)
        logger.info(
            "[generate_memory] recall done in %.3fs, memories=%s",
            time.monotonic() - recall_start,
            len(memories),
        )

        # 4. Assemble memory text
        memory_text = self._assemble_memory_text(context, memories)
        logger.info(
            "[generate_memory] assembled memory_text length=%s",
            len(memory_text) if memory_text else 0,
        )

        # 5. Async commit if threshold exceeded
        asyncio.create_task(self._maybe_commit(session_id))

        total = time.monotonic() - start
        logger.info("[generate_memory] complete sessionId=%s total_time=%.3fs", session_id, total)
        return memory_text

    async def _capture_messages(
        self, session_id: str, messages: list[dict]
    ) -> None:
        """Append new messages to OpenViking session.

        Classification logic:
        - system          -> merged into current user's parts
        - assistant       -> stored as assistant
        - user(context)   -> skipped (not real user speech)
        - user(current)   -> stored as user (with system prefix)
        """
        # 1. Classify messages by role
        system_msg: dict | None = None
        assistant_msgs: list[dict] = []
        user_msgs: list[dict] = []

        for msg in messages:
            role = msg.get("role", "")
            if role == "system":
                system_msg = msg
            elif role == "assistant":
                assistant_msgs.append(msg)
            elif role == "user":
                user_msgs.append(msg)

        # 2. Identify current user: last user msg; context env: all other user msgs
        current_user_msg = user_msgs[-1] if user_msgs else None
        context_env_msgs = user_msgs[:-1] if len(user_msgs) >= 2 else []

        logger.info(
            "[capture] classified: system=%s assistant=%s user_total=%s "
            "current_user=%s context_env=%s",
            bool(system_msg),
            len(assistant_msgs),
            len(user_msgs),
            bool(current_user_msg),
            len(context_env_msgs),
        )

        captured = 0

        # 3. Store assistant messages
        for msg in assistant_msgs:
            parts = message_to_ov_parts(msg)
            if not parts:
                continue
            for part in parts:
                if part.get("type") == "text" and part.get("text"):
                    part["text"] = sanitize_user_text_for_capture(part["text"])

            try:
                await self.client.add_session_message(
                    session_id,
                    role="assistant",
                    role_id="assistant",
                    parts=parts,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
                captured += 1
                logger.info(
                    "[capture] stored assistant sessionId=%s parts=%s",
                    session_id,
                    len(parts),
                )
            except Exception as e:
                logger.warning(
                    "[capture] failed to store assistant for %s: %s",
                    session_id,
                    e,
                )

        # 4. Store current user (merge system into parts)
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

                try:
                    await self.client.add_session_message(
                        session_id,
                        role="user",
                        role_id="user",
                        parts=parts,
                        created_at=datetime.now(timezone.utc).isoformat(),
                    )
                    captured += 1
                    logger.info(
                        "[capture] stored current_user sessionId=%s parts=%s system_merged=%s",
                        session_id,
                        len(parts),
                        bool(system_msg),
                    )
                except Exception as e:
                    logger.warning(
                        "[capture] failed to store current_user for %s: %s",
                        session_id,
                        e,
                    )

        logger.info("[capture] total stored=%s/%s", captured, len(messages))

    async def _recall_memories(self, query_text: str) -> list[dict]:
        """Search user and agent memories in parallel."""
        if not query_text.strip():
            logger.info("[recall] query is empty, skipping")
            return []

        user_uri = "viking://user/memories"
        agent_uri = "viking://agent/memories"

        logger.info(
            "[recall] query='%s...' limit=%s threshold=%s",
            query_text[:80],
            self.config.recall_limit,
            self.config.recall_score_threshold,
        )

        tasks = [
            self._safe_find(
                query_text,
                user_uri,
                self.config.recall_limit,
                self.config.recall_score_threshold,
            ),
            self._safe_find(
                query_text,
                agent_uri,
                self.config.recall_limit,
                self.config.recall_score_threshold,
            ),
        ]

        results: list[dict] = []
        find_results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in find_results:
            if isinstance(res, Exception):
                logger.warning("[recall] find failed: %s", res)
                continue
            memories = res.get("memories", [])
            results.extend(memories)

        logger.info("[recall] raw results=%s", len(results))

        # Post-processing
        results = deduplicate_by_uri(results)
        logger.info("[recall] after dedup=%s", len(results))
        results = filter_leaf_memories(results)
        logger.info("[recall] after leaf filter=%s", len(results))
        results = apply_score_threshold(
            results, self.config.recall_score_threshold
        )
        logger.info("[recall] after threshold=%s", len(results))
        results = rerank_memories(results, query_text)
        logger.info("[recall] after rerank=%s", len(results))

        return results

    async def _safe_find(
        self,
        query: str,
        target_uri: str,
        limit: int,
        score_threshold: float | None,
    ) -> dict:
        """Wrapper that catches exceptions."""
        try:
            result = await self.client.find(
                query, target_uri, limit, score_threshold
            )
            memories = result.get("memories", [])
            logger.info(
                "[safe_find] uri=%s returned=%s",
                target_uri,
                len(memories),
            )
            return result
        except Exception as e:
            logger.warning("[safe_find] error for %s: %s", target_uri, e)
            return {}

    def _assemble_memory_text(
        self, context: dict, memories: list[dict]
    ) -> str:
        """Assemble the memory text block for Higo injection."""
        lines: list[str] = []

        # Session history summary
        overview = context.get("latest_archive_overview", "")
        if overview:
            lines.append("[Session History Summary]")
            lines.append(overview)
            lines.append("")

        # Archive index
        abstracts = context.get("pre_archive_abstracts", [])
        if abstracts:
            lines.append("[Archive Index]")
            for ab in abstracts:
                archive_id = ab.get("archive_id", "unknown")
                abstract = ab.get("abstract", "")
                lines.append(f"- {archive_id}: {abstract}")
            lines.append("")

        # Relevant memories
        if memories:
            lines.append("<relevant-memories>")
            for mem in memories:
                category = mem.get("category", "memory")
                content = mem.get("abstract", mem.get("overview", ""))
                score = mem.get("score", 0)
                lines.append(
                    f"- [{category}] {content} ({score:.0%})"
                )
            lines.append("</relevant-memories>")
            lines.append("")

        text = "\n".join(lines).strip()
        logger.info(
            "[assemble] overview=%s abstracts=%s memories=%s text_len=%s",
            bool(overview),
            len(abstracts),
            len(memories),
            len(text),
        )
        return text

    async def _maybe_commit(self, session_id: str) -> None:
        """Trigger commit if pending_tokens exceeds threshold."""
        try:
            session_info = await self.client.get_session(session_id)
            pending_tokens = session_info.get("pending_tokens", 0)
            logger.info(
                "[commit_check] sessionId=%s pending_tokens=%s threshold=%s",
                session_id,
                pending_tokens,
                self.config.commit_token_threshold,
            )
            if pending_tokens > self.config.commit_token_threshold:
                logger.info(
                    "[commit] triggering sessionId=%s (pending_tokens=%s > threshold=%s)",
                    session_id,
                    pending_tokens,
                    self.config.commit_token_threshold,
                )
                await self.client.commit_session(
                    session_id, wait=False
                )
                logger.info("[commit] triggered for sessionId=%s", session_id)
            else:
                logger.info("[commit] skipped for sessionId=%s", session_id)
        except Exception as e:
            logger.warning("[commit] check failed for %s: %s", session_id, e)
