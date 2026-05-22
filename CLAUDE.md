# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **Higo Session Memory Plugin** — a FastAPI service that intercepts conversation messages and injects a generated memory summary before the current user message. It implements the Higo plugin protocol with two modes: `probe` (health check) and `transform` (message modification).

## Development Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the development server (auto-reload on port 8000)
python main.py

# Or run directly with uvicorn
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

There is no test suite, linting config, or build tool configured in this repo.

## Architecture

### Request Flow

1. **Entry point**: `main.py` exposes a single `POST /` endpoint that routes requests by `mode`.
2. **Models**: `models.py` defines Pydantic v2 models for the Higo plugin protocol (`ProbeRequest`, `TransformRequest`, and corresponding responses).
3. **Memory Engine**: `engine/memory.py` defines `MemoryEngine` (abstract base class) and `PlaceholderMemoryEngine` (current stub implementation). The engine's `generate_memory(session_id, messages)` async method produces the memory text injected into the conversation.

### Message Reconstruction (`_build_messages` in `main.py`)

The transform endpoint rebuilds the message list to preserve anchor semantics. The output order is:

1. `system` message (preserved from original)
2. `user` — injected memory message (from engine)
3. `assistant` — previous round reply (from `messages[1]` if role is assistant)
4. `user` — context/environment info (second-to-last original message, if distinct)
5. `user` — current user message (always last)

When modifying `_build_messages`, maintain this ordering invariant — the Higo client depends on the final message being the current user input.

### Extending the Memory Engine

To replace the placeholder with a real implementation:

1. Subclass `MemoryEngine` in `engine/memory.py` (or a new file)
2. Implement `async def generate_memory(self, session_id: str, messages: list[dict]) -> str`
3. Update `engine/__init__.py` to export the new class
4. In `main.py`, swap `PlaceholderMemoryEngine()` for the new engine instance

The `messages` parameter passed to `generate_memory` is a list of raw dicts (not Pydantic models), each with `role` and `content` keys.
