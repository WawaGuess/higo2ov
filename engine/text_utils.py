"""Text extraction, sanitization, capture decision, and message format conversion."""

import re
import unicodedata

# ---------------------------------------------------------------------------
# Regex constants (ported from openclaw-plugin/text-utils.ts)
# ---------------------------------------------------------------------------

# 7 memory trigger patterns (English + Chinese + email + phone + self-intro + preference + English preference)
MEMORY_TRIGGERS = [
    re.compile(r"remember|preference|prefer|important|decision|decided|always|never", re.IGNORECASE),
    re.compile(
        r"记住|偏好|喜欢|喜爱|崇拜|讨厌|害怕|重要|决定|总是|永远|优先|习惯|爱好|擅长|最爱|不喜欢"
    ),
    re.compile(r"[\w.-]+@[\w.-]+\.\w+"),
    re.compile(r"\+\d{10,}"),
    re.compile(
        r"(?:我|my)\s*(?:是|叫|名字|name|住在|live|来自|from|生日|birthday|电话|phone|邮箱|email)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:我|i)\s*(?:喜欢|崇拜|讨厌|害怕|擅长|不会|爱|恨|想要|需要|希望|觉得|认为|相信)",
        re.IGNORECASE,
    ),
    re.compile(
        r"favorite|favourite|love|hate|enjoy|dislike|admire|idol|fan of",
        re.IGNORECASE,
    ),
]

# Detection helpers
_CJK_CHAR_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff\uac00-\ud7af]")

# Sanitization regexes
_RELEVANT_MEMORIES_BLOCK_RE = re.compile(
    r"<relevant-memories>[\s\S]*?</relevant-memories>", re.IGNORECASE
)
_CONVERSATION_METADATA_BLOCK_RE = re.compile(
    r"(?:^|\n)\s*(?:Conversation info|Conversation metadata|会话信息|对话信息)\s*(?:\([^)]*\))?\s*:\s*```[\s\S]*?```",
    re.IGNORECASE,
)
_SENDER_METADATA_BLOCK_RE = re.compile(
    r"Sender\s*\([^)]*\)\s*:\s*```[\s\S]*?```",
    re.IGNORECASE,
)
_FENCED_JSON_BLOCK_RE = re.compile(r"```json\s*([\s\S]*?)```", re.IGNORECASE)
_METADATA_JSON_KEY_RE = re.compile(
    r'"(session|sessionid|sessionkey|conversationid|channel|sender|userid|agentid|timestamp|timezone)"\s*:',
    re.IGNORECASE,
)
_LEADING_TIMESTAMP_PREFIX_RE = re.compile(
    r"^\s*(?!\[\[)\[(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\s+)?"
    r"(?:\d{4}[-/]\d{2}[-/]\d{2}|\d{2}[-/]\d{2}[-/]\d{2,4})"
    r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[A-Z]{1,5}(?:[+-]\d{1,2})?)?)?\s*\]\s*",
    re.IGNORECASE,
)
_COMPACTED_SYSTEM_MSG_RE = re.compile(
    r"^System:\s*\[.*?\]\s*Compacted\s*(.+)$", re.IGNORECASE
)
_HEARTBEAT_RE = re.compile(r"\bHEARTBEAT(?:\.md|_OK)\b", re.IGNORECASE)

# Capture decision filters
_COMMAND_TEXT_RE = re.compile(r"^[a-z0-9_-]{1,64}\b", re.IGNORECASE)
_SUBAGENT_CONTEXT_RE = re.compile(r"^\s*\[Subagent Context\]", re.IGNORECASE)
_MEMORY_INTENT_RE = re.compile(
    r"记住|记下|remember|save|store|偏好|preference|规则|rule|事实|fact", re.IGNORECASE
)
_QUESTION_CUE_RE = re.compile(
    r"[?？]|\b(?:what|when|where|who|why|how|which|can|could|would|did|does|is|are)\b|"
    r"^(?:请问|能否|可否|怎么|如何|什么时候|谁|什么|哪|是否)",
    re.IGNORECASE,
)

CAPTURE_LIMIT = 3


def _is_non_content_text(text: str) -> bool:
    """Return True if text consists only of punctuation, symbols, and whitespace.

    Replaces the JS regex ``^[\\p{P}\\p{S}\\s]+$`` using unicodedata categories:
    - P* = Punctuation
    - S* = Symbol
    - Z* = Separator (includes spaces)
    """
    if not text:
        return False
    for ch in text:
        cat = unicodedata.category(ch)
        if cat[0] not in ("P", "S", "Z"):
            return False
    return True


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------


def _looks_like_metadata_json_block(content: str) -> bool:
    """Return True if a fenced JSON block looks like metadata (>=3 unique metadata keys)."""
    matched_keys = set()
    for match in _METADATA_JSON_KEY_RE.finditer(content):
        key = (match.group(1) or "").lower()
        if key:
            matched_keys.add(key)
    return len(matched_keys) >= 3


def sanitize_user_text_for_capture(text: str) -> str:
    """Sanitize text before capturing to OpenViking.

    Strips:
    - HEARTBEAT messages
    - Compacted system messages (extracts actual content)
    - <relevant-memories> blocks
    - Conversation metadata blocks
    - Sender metadata blocks
    - Fenced JSON metadata blocks
    - Leading timestamp prefixes
    - Null bytes
    - Excess whitespace
    """
    if not isinstance(text, str):
        return ""

    # 1. Filter HEARTBEAT messages entirely
    if _HEARTBEAT_RE.search(text):
        return ""

    # 2. Handle Compactor system messages: extract the actual user content
    match = _COMPACTED_SYSTEM_MSG_RE.match(text)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()

    # 3. Strip known noise blocks and normalize whitespace
    text = _RELEVANT_MEMORIES_BLOCK_RE.sub(" ", text)
    text = _CONVERSATION_METADATA_BLOCK_RE.sub(" ", text)
    text = _SENDER_METADATA_BLOCK_RE.sub(" ", text)

    # 4. Strip fenced JSON only if it looks like metadata
    def _replace_json_block(m: re.Match) -> str:
        inner = m.group(1) or ""
        return " " if _looks_like_metadata_json_block(inner) else m.group(0)

    text = _FENCED_JSON_BLOCK_RE.sub(_replace_json_block, text)

    # 5. Strip leading timestamps
    text = _LEADING_TIMESTAMP_PREFIX_RE.sub("", text)

    # 6. Remove null bytes and collapse whitespace
    text = text.replace("\u0000", "")
    text = re.sub(r"\s+", " ", text).strip()

    return text


# ---------------------------------------------------------------------------
# Capture Decision
# ---------------------------------------------------------------------------


def _resolve_capture_min_length(text: str) -> int:
    """CJK chars need shorter minimum (4) than Latin (10)."""
    return 4 if _CJK_CHAR_RE.search(text) else 10


def looks_like_question_only_text(text: str) -> bool:
    """Return True if text is a question that should NOT be captured."""
    if not _QUESTION_CUE_RE.search(text) or _MEMORY_INTENT_RE.search(text):
        return False

    # Multi-speaker transcripts often contain "?" but should still be captured
    speaker_tags = re.findall(r"[A-Za-z\u4e00-\u9fa5]{2,20}:\s", text)
    if len(speaker_tags) >= 2 or len(text) > 280:
        return False

    return True


def get_capture_decision(
    text: str, mode: str = "semantic", capture_max_length: int = 8192
) -> dict:
    """Decide whether a text should be captured as a memory source.

    Args:
        text: Raw text to evaluate.
        mode: "semantic" (capture all eligible) or "keyword" (trigger regex first).
        capture_max_length: Max allowed length for capture.

    Returns:
        {"should_capture": bool, "reason": str, "normalized_text": str}
    """
    trimmed = text.strip() if isinstance(text, str) else ""
    normalized = sanitize_user_text_for_capture(trimmed)
    had_sanitization = normalized != trimmed

    # Empty check
    if not normalized:
        reason = (
            "injected_memory_context_only"
            if re.search(r"<relevant-memories>", trimmed, re.IGNORECASE)
            else "empty_text"
        )
        return {"should_capture": False, "reason": reason, "normalized_text": ""}

    # Length filter
    compact_text = re.sub(r"\s+", "", normalized)
    min_length = _resolve_capture_min_length(compact_text)
    if len(compact_text) < min_length or len(normalized) > capture_max_length:
        return {
            "should_capture": False,
            "reason": "length_out_of_range",
            "normalized_text": normalized,
        }

    # Command text filter
    if _COMMAND_TEXT_RE.match(normalized):
        return {
            "should_capture": False,
            "reason": "command_text",
            "normalized_text": normalized,
        }

    # Non-content text filter
    if _is_non_content_text(normalized):
        return {
            "should_capture": False,
            "reason": "non_content_text",
            "normalized_text": normalized,
        }

    # Subagent context filter
    if _SUBAGENT_CONTEXT_RE.match(normalized):
        return {
            "should_capture": False,
            "reason": "subagent_context",
            "normalized_text": normalized,
        }

    # Question-only filter
    if looks_like_question_only_text(normalized):
        return {
            "should_capture": False,
            "reason": "question_text",
            "normalized_text": normalized,
        }

    # Mode branching
    if mode == "keyword":
        for trigger in MEMORY_TRIGGERS:
            if trigger.search(normalized):
                reason = (
                    f"matched_trigger_after_sanitize:{trigger.pattern}"
                    if had_sanitization
                    else f"matched_trigger:{trigger.pattern}"
                )
                return {
                    "should_capture": True,
                    "reason": reason,
                    "normalized_text": normalized,
                }
        reason = (
            "no_trigger_matched_after_sanitize"
            if had_sanitization
            else "no_trigger_matched"
        )
        return {
            "should_capture": False,
            "reason": reason,
            "normalized_text": normalized,
        }

    # semantic mode: always capture (subject to earlier filters)
    reason = (
        "semantic_candidate_after_sanitize"
        if had_sanitization
        else "semantic_candidate"
    )
    return {
        "should_capture": True,
        "reason": reason,
        "normalized_text": normalized,
    }


# ---------------------------------------------------------------------------
# Recall query preparation
# ---------------------------------------------------------------------------


def prepare_recall_query(raw_query: str, max_chars: int = 500) -> str:
    """Prepare a recall query by truncating and cleaning noise."""
    query = sanitize_user_text_for_capture(raw_query)
    if not query:
        return ""
    # Truncate to max_chars, preserving whole words where possible
    if len(query) > max_chars:
        query = query[:max_chars]
        last_space = query.rfind(" ")
        if last_space > max_chars * 0.8:
            query = query[:last_space]
    return query.strip()


# ---------------------------------------------------------------------------
# Message utilities
# ---------------------------------------------------------------------------


def extract_latest_user_text(messages: list[dict]) -> str:
    """Extract the latest user message text from a message list."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return msg.get("content", "")
    return ""


def strip_think_tags(text: str) -> str:
    """Remove <think>...</think> blocks and their contents."""
    return re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()


def message_to_ov_parts(message: dict) -> list[dict]:
    """Convert a single Higo message dict to OpenViking parts format."""
    role = message.get("role", "")
    content = message.get("content", "")

    if not content:
        return []

    if role == "user":
        return [{"type": "text", "text": content}]
    elif role == "assistant":
        return [{"type": "text", "text": strip_think_tags(content)}]
    elif role == "tool":
        return [{"type": "tool", "tool_output": content}]
    return []


def messages_to_ov_parts(messages: list[dict]) -> list[dict]:
    """Convert a list of Higo messages to OpenViking parts format."""
    parts: list[dict] = []
    for msg in messages:
        msg_parts = message_to_ov_parts(msg)
        parts.extend(msg_parts)
    return parts
