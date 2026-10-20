"""Render dbt model SQL with plain Jinja so the models can be tested in an in-memory DuckDB.

Only the dbt functions the models use are stubbed: var(), ref(), config(). Macros from
dbt/macros/*.sql are prepended to the template, so the real macro code runs.
"""

from pathlib import Path

import jinja2

DBT = Path(__file__).resolve().parents[1] / "dbt"

# Same dict-valued vars `ets transform` injects, plus the scalar defaults.
BASE_VARS = {
    "bronze_dir": "unused",
    "staging_dir": "unused",
    "smard_price_filter": 4169,
    "smard_resolution": "quarterhour",
    "sim_start_utc": "2025-09-30 22:00:00",
    "sim_end_utc": "2026-09-30 22:00:00",
    "qh_start_utc": "2025-09-30 22:00:00",
    "n_qh": 35040,
    "opsd_max_kwh_per_qh": 10.0,
    "when2heat_season_start_year": 2014,
}


def _macros() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted((DBT / "macros").glob("*.sql")))


def render_text(template: str, variables: dict | None = None) -> str:
    merged = {**BASE_VARS, **(variables or {})}

    def var(name, default=None):
        if name in merged:
            return merged[name]
        if default is not None:
            return default
        raise KeyError(f"dbt var {name!r} not provided in test")

    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    return env.from_string(_macros() + "\n" + template).render(
        var=var, ref=lambda name: name, config=lambda **kwargs: ""
    )


def render_model(relative_path: str, variables: dict | None = None) -> str:
    return render_text((DBT / "models" / relative_path).read_text(encoding="utf-8"), variables)
