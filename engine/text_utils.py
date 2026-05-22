"""Text extraction, sanitization, and message format conversion utilities."""

import re


def extract_latest_user_text(messages: list[dict]) -> str:
    """Extract the latest user message text from a message list."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return msg.get("content", "")
    return ""


def sanitize_user_text_for_capture(text: str) -> str:
    """Sanitize text before capturing to OpenViking.

    Strips:
    - <relevant-memories> blocks
    - Fenced JSON metadata blocks
    - Leading timestamps
    - Conversation metadata
    """
    # Strip <relevant-memories> blocks
    text = re.sub(
        r"<relevant-memories>.*?</relevant-memories>",
        "",
        text,
        flags=re.DOTALL,
    )
    # Strip fenced JSON metadata blocks
    text = re.sub(r"```json\n.*?\n```", "", text, flags=re.DOTALL)
    # Strip leading timestamps like [2024-01-01 10:00:00] or [2024-01-01T10:00:00]
    text = re.sub(
        r"^\[\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\]\s*", "", text
    )
    return text.strip()


def message_to_ov_parts(message: dict) -> list[dict]:
    """Convert a single Higo message dict to OpenViking parts format."""
    role = message.get("role", "")
    content = message.get("content", "")

    if not content:
        return []

    if role == "user":
        return [{"type": "text", "text": content}]
    elif role == "assistant":
        return [{"type": "text", "text": content}]
    elif role == "tool":
        return [
            {
                "type": "tool",
                "tool_output": content,
            }
        ]
    return []


def messages_to_ov_parts(messages: list[dict]) -> list[dict]:
    """Convert a list of Higo messages to OpenViking parts format."""
    parts: list[dict] = []
    for msg in messages:
        msg_parts = message_to_ov_parts(msg)
        parts.extend(msg_parts)
    return parts
