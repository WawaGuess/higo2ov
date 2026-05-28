"""Memory post-processing: deduplication, filtering, reranking, and budget management."""

import re

from monitor.collector import _count_tokens


# ------------------------------------------------------------------
# Category derivation
# ------------------------------------------------------------------

def _derive_category_from_uri(uri: str) -> str:
    """Derive memory category from its URI path.

    OpenViking stores memories under category subdirectories
    (e.g. /preferences/, /entities/) but the search API does not
    populate the category field in the response metadata.
    """
    if "/preferences" in uri:
        return "preferences"
    if "/entities" in uri:
        return "entities"
    if "/events" in uri:
        return "events"
    if "/profile" in uri:
        return "profile"
    if "/patterns" in uri:
        return "patterns"
    if "/cases" in uri:
        return "cases"
    return "memory"


# ------------------------------------------------------------------
# Dedup helpers (reference-code aligned)
# ------------------------------------------------------------------

def _normalize_dedupe_text(text: str) -> str:
    """Normalize text for deduplication: lowercase and collapse whitespace."""
    if not text:
        return ""
    return " ".join(text.lower().split())


def _is_event_or_case_memory(result: dict) -> bool:
    """Check if memory is an event or case memory."""
    cat = (result.get("category") or "").lower()
    uri = result.get("uri", "").lower()
    return cat == "events" or cat == "cases" or "/events/" in uri or "/cases/" in uri


def _memory_dedupe_key(result: dict) -> str:
    """Generate deduplication key for a memory result.

    Event/case memories are deduplicated by URI.
    Other memories are deduplicated by abstract + category + normalized abstract.
    """
    abstract = _normalize_dedupe_text(result.get("abstract") or result.get("overview") or "")
    cat = (result.get("category") or "").lower() or "unknown"
    if abstract and not _is_event_or_case_memory(result):
        return f"abstract:{cat}:{abstract}"
    return f"uri:{result.get('uri', '')}"


# ------------------------------------------------------------------
# Ranking helpers (reference-code aligned)
# ------------------------------------------------------------------

def _recall_clamp_score(value):
    if not isinstance(value, (int, float)) or value != value:  # NaN check
        return 0.0
    return max(0.0, min(1.0, value))


_RECALL_STOPWORDS = {
    "what", "when", "where", "which", "who", "whom", "whose", "why", "how",
    "did", "does", "is", "are", "was", "were", "the", "and", "for", "with",
    "from", "that", "this", "your", "you",
}

_PREFERENCE_QUERY_RE = re.compile(
    r"prefer|preference|favorite|favourite|like|偏好|喜欢|爱好|更倾向",
    re.IGNORECASE,
)
_TEMPORAL_QUERY_RE = re.compile(
    r"when|what time|date|day|month|year|yesterday|today|tomorrow|last|next"
    r"|什么时候|何时|哪天|几月|几年|昨天|今天|明天|上周|下周|上个月|下个月|去年|明年",
    re.IGNORECASE,
)


def _build_recall_query_profile(query: str):
    text = query.strip()
    all_tokens = re.findall(r"[a-z0-9]{2,}", text.lower())
    tokens = [t for t in all_tokens if t not in _RECALL_STOPWORDS]
    return {
        "tokens": tokens,
        "wants_preference": bool(_PREFERENCE_QUERY_RE.search(text)),
        "wants_temporal": bool(_TEMPORAL_QUERY_RE.search(text)),
    }


def _lexical_overlap_boost(tokens: list[str], text: str) -> float:
    if not tokens or not text:
        return 0.0
    haystack = f" {text.lower()} "
    matched = 0
    for token in tokens[:8]:
        if f" {token} " in haystack or token in haystack:
            matched += 1
    return min(0.2, (matched / min(len(tokens), 4)) * 0.2)


def _is_event_memory(result: dict) -> bool:
    cat = (result.get("category") or "").lower()
    return cat == "events" or "/events/" in result.get("uri", "")


def _is_preferences_memory(result: dict) -> bool:
    uri = result.get("uri", "")
    category = result.get("category") or _derive_category_from_uri(uri)
    return (
        category == "preferences"
        or "/preferences/" in uri
        or uri.rstrip("/").endswith("/preferences")
    )


def _is_leaf_like_memory(result: dict) -> bool:
    return result.get("level") == 2


def _rank_for_injection(result: dict, query: dict) -> float:
    base_score = _recall_clamp_score(result.get("score"))
    abstract = (result.get("abstract") or result.get("overview") or "").strip()
    leaf_boost = 0.12 if _is_leaf_like_memory(result) else 0.0
    event_boost = 0.1 if query["wants_temporal"] and _is_event_memory(result) else 0.0
    preference_boost = (
        0.08 if query["wants_preference"] and _is_preferences_memory(result) else 0.0
    )
    overlap_boost = _lexical_overlap_boost(
        query["tokens"], f"{result.get('uri', '')} {abstract}"
    )
    return base_score + leaf_boost + event_boost + preference_boost + overlap_boost


# ------------------------------------------------------------------
# Pick memories for injection (reference-code aligned)
# ------------------------------------------------------------------

def pick_memories_for_injection(
    items: list[dict],
    limit: int,
    query_text: str,
    score_threshold: float = 0.0,
) -> list[dict]:
    """Pick memories for injection — mirrors the reference TypeScript implementation.

    1. Rank items query-aware (temporal/preference/leaf/lexical boosts).
    2. Deduplicate by content key.
    3. Prefer leaf (level == 2) first; if leaf count >= limit return only leaves.
    4. Otherwise supplement with non-leaf fallback up to limit,
       filtering fallback by score_threshold.
    """
    if not items or limit <= 0:
        return []

    query = _build_recall_query_profile(query_text)
    sorted_items = sorted(
        items, key=lambda item: _rank_for_injection(item, query), reverse=True
    )

    deduped: list[dict] = []
    seen: set[str] = set()
    for item in sorted_items:
        key = _memory_dedupe_key(item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    leaves = [item for item in deduped if _is_leaf_like_memory(item)]
    if len(leaves) >= limit:
        return leaves[:limit]

    picked = list(leaves)
    used = {item.get("uri") for item in leaves}
    for item in deduped:
        if len(picked) >= limit:
            break
        uri = item.get("uri")
        if uri in used:
            continue
        if _recall_clamp_score(item.get("score")) < score_threshold:
            continue
        picked.append(item)
        used.add(uri)

    return picked


# ------------------------------------------------------------------
# Budget assembly
# ------------------------------------------------------------------

def build_memory_lines_with_budget(
    results: list[dict], token_budget: int
) -> list[str]:
    """Build memory lines within a token budget.

    The first memory is always included even if it exceeds budget (bounded overshoot).
    Uses _count_tokens for accurate token estimation (tiktoken when available).
    """
    lines: list[str] = []
    running_text = ""

    for i, r in enumerate(results):
        category = r.get("category") or _derive_category_from_uri(r.get("uri", ""))
        content = r.get("abstract") or r.get("overview") or ""
        score = r.get("score", 0)
        line = f"- [{category}] {content} ({score:.0%})"
        if i == 0:
            lines.append(line)
            running_text = line
            continue
        # Check total *after* adding this line to avoid a single oversized entry blowing the budget.
        candidate = running_text + "\n" + line
        if _count_tokens(candidate) <= token_budget:
            lines.append(line)
            running_text = candidate
    return lines
