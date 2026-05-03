# Mycelium SDK (Python)

From this directory:

```bash
uv sync --all-groups
uv run pytest
uv run python -c "import mycelium; print(mycelium.__version__)"
```

Uses **`sdk/.venv`** (separate from a repo-root `.venv` if you have one).
