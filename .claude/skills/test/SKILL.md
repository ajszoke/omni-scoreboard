---
name: test
description: Run the Omni Scoreboard test suite plus type and style checks. Use before committing, when verifying a change, or when asked to run tests/lint/typecheck.
---

# Test, typecheck, format

Run from the repo root with the project-local `.venv` (see `docs/dev_setup.md`):

```bash
.venv/bin/python -m pytest tests/ -v
.venv/bin/python -m mypy .            # config in pyproject.toml
.venv/bin/python -m black --check .   # line-length 120 (pyproject.toml)
```

`uv run pytest ...` etc. also work. For coverage on the typed `omni/` code, append
`--cov=omni --cov-branch --cov-report=term-missing` to the pytest run (needs `pytest-cov`,
pinned in `requirements.dev.txt`) — line coverage alone hides branch gaps, so prefer `--cov-branch`.

New domain code must follow the typed-domain policy in `AGENTS.md` (no new stringly-typed
league/team/event/card concepts). Add a fixture or golden-image snapshot test for every
spacing/animation bug fixed.
