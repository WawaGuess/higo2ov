# 相关文档:
#   - docs/features/会话历史.md（Token 预算分配）
#   - docs/engine/记忆排序.md（Budget assembly）
"""Token counting utilities (tiktoken preferred, fallback to rough estimate)."""

# ------------------------------------------------------------------
# tiktoken
# ------------------------------------------------------------------
try:
    import tiktoken

    _TIKTOKEN_ENC = tiktoken.get_encoding("cl100k_base")
except Exception:
    _TIKTOKEN_ENC = None


def _count_tokens(text: str) -> int:
    if not text:
        return 0
    if _TIKTOKEN_ENC is not None:
        try:
            return len(_TIKTOKEN_ENC.encode(text))
        except Exception:
            pass
    total = 0
    for ch in text:
        total += 1 if ord(ch) > 127 else 0.25
    return max(1, int(total))
