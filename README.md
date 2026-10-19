# Dynamic Tariff Simulator (work in progress)

Simulates dynamic electricity tariffs and §14a EnWG grid-fee modules for German households on
quarter-hour day-ahead prices. Built phase by phase; the full README is written in Phase 7.

Current status: Phase 0 (scaffold, typed config, verification notes in `DECISIONS.md`).

```
uv sync
uv run ets check-config
uv run pytest
```
