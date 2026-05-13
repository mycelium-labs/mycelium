# `mycelium/` — SDK package source

| Module | Purpose |
|--------|---------|
| `protect.py` | `@protect` / `@protect_sync` decorators, `Session`, cache logic |
| `http.py` | `AsyncClient` / `Client` with payload completeness checks |
| `stream_guard.py` | `StreamGuard` — cut-off and duplicate stream detection |
| `history_guard.py` | `HistoryGuard` — message history validation |
| `message_validator.py` | `MessageValidator` — tool-call/message structural validation |
| `content_block_normalizer.py` | `ContentBlockNormalizer` — provider format normalization |
| `tool_sequencer.py` | `ToolSequencer` — parallel tool call ordering detection |
| `scratchpad_guard.py` | `ScratchpadGuard` — multi-agent shared state access logging |
