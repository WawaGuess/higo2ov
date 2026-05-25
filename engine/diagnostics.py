"""Structured diagnostic logging utilities."""

import json
import logging
import time

logger = logging.getLogger(__name__)


def emit_diag(
    stage: str,
    session_id: str,
    data: dict,
    enabled: bool = True,
) -> None:
    """Emit a structured diagnostic log line.

    Format: ``openviking: diag {"ts": ..., "stage": ..., "sessionId": ..., "data": ...}``
    """
    if not enabled:
        return

    payload = {
        "ts": int(time.time() * 1000),
        "stage": stage,
        "sessionId": session_id,
        "data": data,
    }
    logger.info("openviking: diag %s", json.dumps(payload, ensure_ascii=False))
