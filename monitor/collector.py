"""TurnCollector — aggregates per-turn conversation data for the monitor UI."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_TURNS_PER_SESSION = 50

# ------------------------------------------------------------------
# Token counting (tiktoken preferred, fallback to rough estimate)
# ------------------------------------------------------------------
try:
    import tiktoken

    _HAS_TIKTOKEN = True
except ImportError:
    _HAS_TIKTOKEN = False


def _count_tokens(text: str) -> int:
    if not text:
        return 0
    if _HAS_TIKTOKEN:
        try:
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except Exception:
            pass
    total = 0
    for ch in text:
        total += 1 if ord(ch) > 127 else 0.25
    return max(1, int(total))


def _extract_text(content: Any) -> str:
    """Extract plain text from message content (str or list of parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
        return "\n".join(parts)
    return ""


def _chunk_input(messages: list[dict], memory_text: str = "") -> tuple[list[dict], int]:
    """Break reconstructed messages into categorized chunks with token counts.

    Categories:
        system  → role="system"
        memory  → role="user" and content matches memory_text
        history → other user/assistant/tool messages
        user    → last role="user" (the current query, excluding memory)
    """
    chunks: list[dict] = []
    total = 0

    # Identify the last user message index (current query)
    last_user_idx = -1
    for i, msg in enumerate(messages):
        if msg.get("role") == "user":
            last_user_idx = i

    system_content = ""
    memory_content = ""
    user_content = ""
    history_parts: list[str] = []

    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        content = _extract_text(msg.get("content"))

        if role == "system":
            system_content = content
        elif role == "user":
            # Check if this is the injected memory message
            if memory_text and memory_text in content:
                memory_content = content
            elif i == last_user_idx:
                user_content = content
            else:
                history_parts.append(f"[User]\n{content}")
        elif role == "assistant":
            history_parts.append(f"[Assistant]\n{content}")
        elif role == "tool":
            history_parts.append(f"[Tool]\n{content}")
        else:
            history_parts.append(f"[{role}]\n{content}")

    # System Prompt chunk
    if system_content:
        tokens = _count_tokens(system_content) + 3
        total += tokens
        chunks.append({
            "name": "System Prompt",
            "category": "system",
            "tokens": tokens,
            "content_preview": system_content[:200],
        })

    # Injected Memory chunk
    if memory_content:
        tokens = _count_tokens(memory_content) + 3
        total += tokens
        chunks.append({
            "name": "Injected Memory",
            "category": "memory",
            "tokens": tokens,
            "content_preview": memory_content[:200],
        })

    # Conversation History chunk
    if history_parts:
        history_text = "\n\n".join(history_parts)
        tokens = sum(_count_tokens(part) + 3 for part in history_parts)
        total += tokens
        chunks.append({
            "name": "Conversation History",
            "category": "history",
            "tokens": tokens,
            "content_preview": history_text[:200],
        })

    # User Input chunk (current query)
    if user_content:
        tokens = _count_tokens(user_content) + 3
        total += tokens
        chunks.append({
            "name": "User Input",
            "category": "user",
            "tokens": tokens,
            "content_preview": user_content[:200],
            "is_current_query": True,
        })

    return chunks, total


# ------------------------------------------------------------------
# Data model
# ------------------------------------------------------------------

@dataclass
class TurnRecord:
    turn_id: str
    session_id: str
    round_id: str
    seq: int
    created_at: float

    system_prompt: str = ""
    system_tokens: int = 0
    user_input: str = ""
    user_tokens: int = 0
    history: List[dict] = field(default_factory=list)
    history_tokens: int = 0
    memory_injected: str = ""
    memory_tokens: int = 0

    assistant_output: str = ""
    assistant_tokens: int = 0
    reasoning: str = ""
    tool_calls: List[dict] = field(default_factory=list)

    errors: List[Any] = field(default_factory=list)

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0

    chunks: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "round_id": self.round_id,
            "seq": self.seq,
            "created_at": self.created_at,
            "created_at_iso": _fmt_iso(self.created_at),
            "input": {
                "system_prompt": self.system_prompt,
                "system_tokens": self.system_tokens,
                "user_input": self.user_input,
                "user_tokens": self.user_tokens,
                "conversation_history": self.history,
                "history_tokens": self.history_tokens,
                "memory_injected": self.memory_injected,
                "memory_tokens": self.memory_tokens,
            },
            "output": {
                "assistant_output": self.assistant_output,
                "assistant_tokens": self.assistant_tokens,
                "reasoning": self.reasoning,
                "tool_calls": self.tool_calls,
            },
            "errors": [
                e.model_dump() if hasattr(e, "model_dump") else e for e in self.errors
            ],
            "totals": {
                "input_tokens": self.total_input_tokens,
                "output_tokens": self.total_output_tokens,
                "total_tokens": self.total_tokens,
            },
            "chunks": self.chunks,
        }

    @staticmethod
    def from_dict(data: dict) -> "TurnRecord":
        inp = data.get("input", {})
        out = data.get("output", {})
        totals = data.get("totals", {})
        return TurnRecord(
            turn_id=data["turn_id"],
            session_id=data["session_id"],
            round_id=data["round_id"],
            seq=data["seq"],
            created_at=data["created_at"],
            system_prompt=inp.get("system_prompt", ""),
            system_tokens=inp.get("system_tokens", 0),
            user_input=inp.get("user_input", ""),
            user_tokens=inp.get("user_tokens", 0),
            history=inp.get("conversation_history", []),
            history_tokens=inp.get("history_tokens", 0),
            memory_injected=inp.get("memory_injected", ""),
            memory_tokens=inp.get("memory_tokens", 0),
            assistant_output=out.get("assistant_output", ""),
            assistant_tokens=out.get("assistant_tokens", 0),
            reasoning=out.get("reasoning", ""),
            tool_calls=out.get("tool_calls", []),
            errors=data.get("errors", []),
            total_input_tokens=totals.get("input_tokens", 0),
            total_output_tokens=totals.get("output_tokens", 0),
            total_tokens=totals.get("total_tokens", 0),
            chunks=data.get("chunks", []),
        )


def _fmt_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# ------------------------------------------------------------------
# Collector
# ------------------------------------------------------------------

class TurnCollector:
    _instance: Optional["TurnCollector"] = None

    def __init__(self, data_dir: str | None = None) -> None:
        self._sessions: Dict[str, List[TurnRecord]] = {}
        self._pending: Dict[str, TurnRecord] = {}  # round_id -> TurnRecord (in-progress)

        if data_dir is None:
            pkg = Path(__file__).resolve().parent.parent
            data_dir = str(pkg / "data")
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)

        self._load_history()

    @classmethod
    def get_instance(cls, data_dir: str | None = None) -> "TurnCollector":
        if cls._instance is None:
            cls._instance = cls(data_dir=data_dir)
        return cls._instance

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start_turn(
        self,
        session_id: str,
        round_id: str,
        seq: int,
        messages: list[dict],
        model_tokens: int = 0,
        memory_text: str = "",
    ) -> None:
        """Called AFTER _build_messages(), with the reconstructed message list."""

        chunks, input_tokens = _chunk_input(messages, memory_text)

        system_tokens = sum(c["tokens"] for c in chunks if c["category"] == "system")
        user_tokens = sum(c["tokens"] for c in chunks if c["category"] == "user")
        history_tokens = sum(c["tokens"] for c in chunks if c["category"] == "history")
        memory_tokens = sum(c["tokens"] for c in chunks if c["category"] == "memory")

        # Extract fields for structured storage
        system_prompt = ""
        user_input = ""
        history: list[dict] = []
        memory_injected = ""

        last_user_idx = -1
        for i, msg in enumerate(messages):
            if msg.get("role") == "user":
                last_user_idx = i

        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            content = _extract_text(msg.get("content"))

            if role == "system":
                system_prompt = content
            elif role == "user":
                if memory_text and memory_text in content:
                    memory_injected = content
                elif i == last_user_idx:
                    user_input = content
                else:
                    history.append({"role": role, "content": content})
            elif role in ("assistant", "tool"):
                history.append({"role": role, "content": content})

        turn = TurnRecord(
            turn_id=f"turn_{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            round_id=round_id,
            seq=seq,
            created_at=time.time(),
            system_prompt=system_prompt,
            system_tokens=system_tokens,
            user_input=user_input,
            user_tokens=user_tokens,
            history=history,
            history_tokens=history_tokens,
            memory_injected=memory_injected,
            memory_tokens=memory_tokens,
            chunks=chunks,
            total_input_tokens=input_tokens,
        )

        self._pending[round_id] = turn
        logger.info(
            "[Collector] Turn started: round_id=%s session=%s seq=%s input_tokens=%s",
            round_id, session_id, seq, input_tokens,
        )

    def end_turn(
        self,
        round_id: str,
        sections: list[dict],
        errors: list[Any] | None = None,
    ) -> Optional[TurnRecord]:
        """Finalize a turn on result callback."""
        turn = self._pending.pop(round_id, None)
        if turn is None:
            logger.warning("[Collector] No pending turn for round_id=%s", round_id)
            return None

        # Extract assistant reply from sections
        outputs: list[str] = []
        reasoning = ""
        tool_calls: list[dict] = []

        for section in sections:
            stype = section.get("type", "")
            content = section.get("content", "") or ""

            if stype in ("text", "content"):
                outputs.append(content)
            elif stype == "reasoning":
                reasoning = content
            elif stype == "tool":
                tool_calls.append({
                    "toolname": section.get("toolname", ""),
                    "toolargs": section.get("toolargs", ""),
                    "toolrsp": section.get("toolrsp", ""),
                })

        assistant_output = "\n\n".join(outputs)
        assistant_tokens = _count_tokens(assistant_output)

        # Calculate tool call tokens
        tool_tokens = 0
        tool_previews: list[str] = []
        for tc in tool_calls:
            args_tk = _count_tokens(tc.get("toolargs", ""))
            rsp_tk = _count_tokens(tc.get("toolrsp", ""))
            tool_tokens += args_tk + rsp_tk
            tool_previews.append(f"{tc.get('toolname', '')}: {tc.get('toolargs', '')[:100]}")

        # Add tool chunk if any tool calls exist
        if tool_calls:
            turn.chunks.append({
                "name": "Tool Calls",
                "category": "tools",
                "tokens": tool_tokens,
                "content_preview": "\n".join(tool_previews)[:200],
            })

        turn.assistant_output = assistant_output
        turn.assistant_tokens = assistant_tokens
        turn.reasoning = reasoning
        turn.tool_calls = tool_calls
        turn.errors = errors or []
        turn.total_output_tokens = assistant_tokens + tool_tokens
        turn.total_tokens = turn.total_input_tokens + assistant_tokens + tool_tokens

        # Store in session
        self._sessions.setdefault(turn.session_id, []).append(turn)

        # Memory limit: keep last N turns per session in memory
        session_turns = self._sessions[turn.session_id]
        if len(session_turns) > _MAX_TURNS_PER_SESSION:
            self._sessions[turn.session_id] = session_turns[-_MAX_TURNS_PER_SESSION:]

        self._persist_session(turn.session_id)
        logger.info(
            "[Collector] Turn ended: round_id=%s input=%d output=%d total=%d",
            round_id, turn.total_input_tokens, turn.total_output_tokens, turn.total_tokens,
        )
        return turn

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    def list_sessions(self) -> list[dict]:
        """Return all sessions overview, newest first."""
        result = []
        for session_id, turns in self._sessions.items():
            if not turns:
                continue
            latest = max(turns, key=lambda t: t.created_at)
            result.append({
                "session_id": session_id,
                "turn_count": len(turns),
                "latest_turn": latest.to_dict(),
                "created_at": turns[0].created_at,
                "updated_at": latest.created_at,
                "totals": {
                    "total_input_tokens": sum(t.total_input_tokens for t in turns),
                    "total_output_tokens": sum(t.total_output_tokens for t in turns),
                    "total_tokens": sum(t.total_tokens for t in turns),
                },
            })
        return sorted(result, key=lambda s: s["updated_at"], reverse=True)

    def get_session(self, session_id: str) -> Optional[dict]:
        turns = self._sessions.get(session_id, [])
        if not turns:
            return None
        return {
            "session_id": session_id,
            "created_at": turns[0].created_at,
            "updated_at": max(t.created_at for t in turns),
            "turns": [t.to_dict() for t in turns],
            "session_totals": {
                "total_input_tokens": sum(t.total_input_tokens for t in turns),
                "total_output_tokens": sum(t.total_output_tokens for t in turns),
                "total_tokens": sum(t.total_tokens for t in turns),
                "turn_count": len(turns),
            },
        }

    def get_latest_turn(self) -> Optional[dict]:
        latest = None
        for turns in self._sessions.values():
            for t in turns:
                if latest is None or t.created_at > latest.created_at:
                    latest = t
        return latest.to_dict() if latest else None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist_session(self, session_id: str) -> None:
        try:
            turns = self._sessions.get(session_id, [])
            if not turns:
                return
            data = {
                "session_id": session_id,
                "created_at": turns[0].created_at,
                "updated_at": max(t.created_at for t in turns),
                "turns": [t.to_dict() for t in turns],
                "session_totals": {
                    "total_input_tokens": sum(t.total_input_tokens for t in turns),
                    "total_output_tokens": sum(t.total_output_tokens for t in turns),
                    "total_tokens": sum(t.total_tokens for t in turns),
                    "turn_count": len(turns),
                },
            }
            filepath = self._data_dir / f"session_{session_id}.json"
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        except Exception as exc:
            logger.warning("[Collector] Failed to persist session: %s", exc)

    def _load_history(self) -> None:
        try:
            files = sorted(
                self._data_dir.glob("session_*.json"),
                key=lambda p: p.stat().st_mtime,
            )
            loaded = 0
            for fp in files:
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                session_id = data.get("session_id")
                if not session_id:
                    continue
                for turn_data in data.get("turns", [])[-_MAX_TURNS_PER_SESSION:]:
                    try:
                        turn = TurnRecord.from_dict(turn_data)
                        self._sessions.setdefault(session_id, []).append(turn)
                        loaded += 1
                    except Exception:
                        continue
            logger.info("[Collector] Loaded %d turns from %d sessions", loaded, len(files))
        except Exception as exc:
            logger.debug("[Collector] Failed to load history: %s", exc)


def get_collector() -> TurnCollector:
    return TurnCollector.get_instance()
