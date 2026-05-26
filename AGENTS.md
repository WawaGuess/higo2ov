# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project Overview

This is a **Higo Session Memory Plugin** — a FastAPI service that intercepts conversation messages and injects a generated memory summary before the current user message. It implements the **Higo V2 plugin protocol** with three modes: `probe` (health check), `transform` (message modification), and `result` (round result callback).

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
2. **Models**: `models.py` defines Pydantic v2 models for the Higo V2 plugin protocol (`ProbeRequest`, `TransformRequest`, `ResultRequest`, and corresponding responses).
3. **Memory Engine**: `engine/openviking_engine.py` implements `OpenVikingMemoryEngine`. The engine's `generate_memory(session_id, messages)` async method produces the memory text injected into the conversation.

### Message Reconstruction (`_build_messages` in `main.py`)

The transform endpoint rebuilds the message list according to the V2 protocol. The output order is:

1. `system` message (preserved from original)
2. `user` — injected memory message (from engine)
3. `user` — context/environment info (preserved from original)
4. `user` — current user message (always last)

When modifying `_build_messages`, maintain this ordering invariant — the Higo client depends on the final message being the current user input.

### V2 Protocol Fields

**Transform request key fields:**
- `session.sessionId` — session identifier
- `round.roundId` / `round.seq` / `round.startedAt` — round info
- `request.messages` — message list `[system, user(context), user(current)]`
- `meta.modelContextWindowTokens` — model context window size

**Transform response structure:**
- `ok` — always `true`
- `summary` — status description
- `result.request.messages` — modified message list
- `result.pluginContext` — optional plugin context (e.g. `memoryRevision`)

Note: V2 protocol does not use `anchor`, top-level `sessionId`, or `debug` fields.
