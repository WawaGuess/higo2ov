"""Memory post-processing: deduplication, filtering, reranking, and budget management."""

import re


def deduplicate_by_uri(results: list[dict]) -> list[dict]:
    """Deduplicate by URI, keeping the highest-scored entry."""
    seen: dict[str, dict] = {}
    for r in results:
        uri = r.get("uri", "")
        if not uri:
            continue
        existing = seen.get(uri)
        if existing is None or r.get("score", 0) > existing.get("score", 0):
            seen[uri] = r
    return list(seen.values())


def filter_leaf_memories(results: list[dict]) -> list[dict]:
    """Keep only level == 2 (leaf memories, i.e. full content)."""
    return [r for r in results if r.get("level") == 2]


def apply_score_threshold(results: list[dict], threshold: float) -> list[dict]:
    """Filter by minimum score threshold."""
    return [r for r in results if r.get("score", 0) >= threshold]


def rerank_memories(results: list[dict], query: str) -> list[dict]:
    """Query-aware reranking with temporal/preference/lexical boosts."""
    query_lower = query.lower()
    temporal_keywords = [
        "when",
        "time",
        "date",
        "yesterday",
        "today",
        "tomorrow",
        "last week",
        "before",
        "after",
        "之前",
        "之后",
        "昨天",
        "今天",
        "明天",
        "上周",
    ]
    preference_keywords = [
        "prefer",
        "like",
        "want",
        "喜欢",
        "偏好",
        "习惯",
        "preference",
    ]

    is_temporal = any(kw in query_lower for kw in temporal_keywords)
    is_preference = any(kw in query_lower for kw in preference_keywords)

    def score_boost(r: dict) -> float:
        base = r.get("score", 0)
        # Leaf boost
        if r.get("level") == 2:
            base += 0.12
        # Event temporal boost
        if is_temporal and r.get("category") == "events":
            base += 0.1
        # Preference boost
        if is_preference and r.get("category") == "preferences":
            base += 0.08
        # Lexical overlap boost
        content = (r.get("abstract", "") + " " + r.get("overview", "")).lower()
        query_words = set(query_lower.split())
        overlap = sum(1 for word in query_words if word in content)
        base += min(overlap * 0.05, 0.2)
        return base

    results.sort(key=lambda r: score_boost(r), reverse=True)
    return results


def build_memory_lines_with_budget(
    results: list[dict], token_budget: int
) -> list[str]:
    """Build memory lines within a token budget.

    The first memory is always included even if it exceeds budget (bounded overshoot).
    """
    lines: list[str] = []
    for i, r in enumerate(results):
        category = r.get("category", "memory")
        content = r.get("abstract", r.get("overview", ""))
        score = r.get("score", 0)
        line = f"- [{category}] {content} ({score:.0%})"
        if i == 0:
            lines.append(line)
            continue
        # Rough estimation: ~4 chars per token
        estimated_tokens = len("\n".join(lines)) // 4
        if estimated_tokens < token_budget:
            lines.append(line)
    return lines
