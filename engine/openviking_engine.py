"""OpenViking memory engine implementation."""

import asyncio
import logging
from datetime import datetime, timezone

from engine.bypass import compile_session_patterns, should_bypass_session
from engine.config import OpenVikingConfig
from engine.diagnostics import emit_diag
from engine.memory import MemoryEngine
from engine.memory_ranking import (
    apply_score_threshold,
    build_memory_lines_with_budget,
    deduplicate_by_uri,
    filter_leaf_memories,
    rerank_memories,
)
from engine.agent_resolver import AgentResolver
from engine.openviking_client import OpenVikingClient
from engine.session_utils import session_to_ov_id
from engine.text_utils import (
    extract_latest_user_text,
    message_to_ov_parts,
    prepare_recall_query,
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
        self._agent_resolver = AgentResolver(config.agent_id)
        self._bypass_patterns = compile_session_patterns(
            [p.strip() for p in config.bypass_session_patterns.split(",") if p.strip()]
        )
        # Round-id deduplication for result callback idempotency (keep last 1000)
        self._processed_round_ids: set[str] = set()

    def _diag(
        self, stage: str, session_id: str, data: dict
    ) -> None:
        emit_diag(stage, session_id, data, self.config.emit_diagnostics)

    def _resolve_agent_id(self, session_id: str) -> str:
        """Resolve agent ID from sessionId or fall back to config."""
        return self._agent_resolver.resolve(session_id)

    async def generate_memory(
        self, session_id: str, messages: list[dict], model_context_tokens: int = 0
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
            "[generate_memory] start sessionId=%s ovSessionId=%s msg_count=%s",
            session_id,
            ov_session_id,
            len(messages),
        )

        # 1. Capture messages
        capture_start = time.monotonic()
        if self.config.auto_capture:
            await self._capture_messages(session_id, ov_session_id, messages)
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
            context = await self.client.get_session_context(ov_session_id)
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
                "[generate_memory] failed to get session context for %s: %s",
                ov_session_id,
                e,
            )

        memory_text = ""

        # 3-4. Search and assemble memories (if auto_recall enabled)
        if self.config.auto_recall:
            recall_start = time.monotonic()
            raw_query = extract_latest_user_text(messages)
            query_text = prepare_recall_query(raw_query)
            logger.info("[generate_memory] recall query='%s'", query_text[:100])

            if query_text:
                memories = await self._recall_memories(query_text)
                logger.info(
                    "[generate_memory] recall done in %.3fs, memories=%s",
                    time.monotonic() - recall_start,
                    len(memories),
                )

                # 4. Assemble memory text
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

                memory_text = self._assemble_memory_text(memories, effective_budget)
                logger.info(
                    "[generate_memory] assembled memory_text length=%s",
                    len(memory_text) if memory_text else 0,
                )
            else:
                logger.info("[generate_memory] recall query empty, skipping")
        else:
            logger.info("[generate_memory] auto_recall disabled, skipping recall")

        # 5. Async commit if threshold exceeded
        asyncio.create_task(self._maybe_commit(ov_session_id))

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
        self, session_id: str, ov_session_id: str, messages: list[dict]
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
                        await self.client.add_session_message(
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

    async def _recall_memories(self, query_text: str) -> list[dict]:
        """Search user and agent memories in parallel."""
        if not query_text.strip():
            logger.info("[recall] query is empty, skipping")
            return []

        agent_id = self._resolve_agent_id("")
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
                agent_id,
            ),
            self._safe_find(
                query_text,
                agent_uri,
                self.config.recall_limit,
                self.config.recall_score_threshold,
                agent_id,
            ),
        ]

        # Optionally search resources
        if self.config.recall_resources:
            tasks.append(
                self._safe_find(
                    query_text,
                    "viking://resources",
                    self.config.recall_limit,
                    self.config.recall_score_threshold,
                    agent_id,
                )
            )

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
        agent_id: str | None = None,
    ) -> dict:
        """Wrapper that catches exceptions."""
        try:
            result = await self.client.find(
                query, target_uri, limit, score_threshold, agent_id
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
        self, memories: list[dict], token_budget: int
    ) -> str:
        """Assemble the memory text block for Higo injection."""
        if not memories:
            return ""

        lines = ["<relevant-memories>"]
        memory_lines = build_memory_lines_with_budget(memories, token_budget)
        lines.extend(memory_lines)
        lines.append("</relevant-memories>")

        text = "\n".join(lines)
        logger.info(
            "[assemble] memories=%s text_len=%s",
            len(memories),
            len(text),
        )
        return text

    async def _maybe_commit(self, ov_session_id: str) -> None:
        """Trigger commit if pending_tokens exceeds threshold."""
        try:
            session_info = await self.client.get_session(ov_session_id)
            pending_tokens = session_info.get("pending_tokens", 0)
            logger.info(
                "[commit_check] ovSessionId=%s pending_tokens=%s threshold=%s",
                ov_session_id,
                pending_tokens,
                self.config.commit_token_threshold,
            )
            if pending_tokens > self.config.commit_token_threshold:
                logger.info(
                    "[commit] triggering ovSessionId=%s (pending_tokens=%s > threshold=%s)",
                    ov_session_id,
                    pending_tokens,
                    self.config.commit_token_threshold,
                )
                commit_result = await self.client.commit_session(
                    ov_session_id, wait=False
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

    async def compact(self, session_id: str) -> dict:
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
            pre_ctx = await self.client.get_session_context(ov_session_id)
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
                "[compact] committing ovSessionId=%s (wait=true)", ov_session_id
            )
            commit_result = await self.client.commit_session(
                ov_session_id, wait=True
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
                post_ctx = await self.client.get_session_context(ov_session_id)
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
        self, session_id: str, sections: list[dict], round_id: str = ""
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
        for section in sections:
            sec_type = section.get("type", "")

            if sec_type == "content" and section.get("content"):
                text = sanitize_user_text_for_capture(section["content"])
                if not text:
                    continue
                try:
                    await self.client.add_session_message(
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
                    await self.client.add_session_message(
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
