"""Typer CLI. Phase 0: only `check-config` works; the rest fail loudly until their phase."""

import typer
from pydantic import ValidationError

from ets.config import config_hash, load_config
from ets.settings import N_QH, SIM_END, SIM_START

app = typer.Typer(no_args_is_help=True, help="Dynamic tariff simulator (ets).")


def _not_yet(name: str, phase: int) -> None:
    typer.echo(f"`ets {name}` is implemented in Phase {phase}.", err=True)
    raise typer.Exit(1)


@app.command("check-config")
def check_config() -> None:
    """Load and validate config/*.yaml; exit 1 on any error."""
    try:
        cfg = load_config()
    except (ValidationError, FileNotFoundError, ValueError) as e:
        typer.echo(f"Config invalid:\n{e}", err=True)
        raise typer.Exit(1) from e
    typer.echo("Config OK")
    typer.echo(f"  simulation year : {SIM_START} .. {SIM_END} ({N_QH} quarter-hours)")
    typer.echo(f"  grid operator   : {cfg.grid_14a.grid_operator}")
    typer.echo(f"  supplier        : {cfg.tariffs.supplier.name}")
    typer.echo(f"  tariff variants : {', '.join(v.id for v in cfg.tariffs.variants)}")
    typer.echo(f"  households      : {', '.join(h.id for h in cfg.households)}")
    typer.echo(f"  config hash     : {config_hash(cfg)[:16]}")


@app.command()
def ingest() -> None:
    """Download all sources into data/bronze; exit 1 if any source or coverage check fails."""
    from ets.ingest import bdew, manifest, opsd, smard, when2heat

    try:
        cfg = load_config()
    except (ValidationError, FileNotFoundError, ValueError) as e:
        typer.echo(f"Config invalid:\n{e}", err=True)
        raise typer.Exit(1) from e

    failures: list[str] = []

    def step(name: str, fn):
        typer.echo(f"[{name}] ...")
        try:
            result = fn()
        except Exception as e:  # one failing source must not stop the others
            failures.append(f"{name}: {e}")
            typer.echo(f"[{name}] FAILED: {e}", err=True)
            return
        typer.echo(f"[{name}] ok")
        return result

    prices = step("smard", lambda: smard.ingest_smard(cfg))
    if prices:
        typer.echo(f"  {len(prices[0])} price chunks")
        for w in prices[1]:
            typer.echo(f"  warning: {w}", err=True)
    step("opsd", lambda: opsd.download_household_data(cfg))
    step("when2heat", lambda: when2heat.download_when2heat(cfg))
    step("bdew", lambda: bdew.register_bdew_file(cfg))

    for problem in manifest.check_coverage(cfg):
        failures.append(f"coverage: {problem}")
        typer.echo(f"[coverage] {problem}", err=True)

    if failures:
        typer.echo(f"\nIngest failed ({len(failures)} problem(s)).", err=True)
        raise typer.Exit(1)
    typer.echo("\nIngest OK. Manifest: data/bronze/_manifest.json")


@app.command("seed-holidays")
def seed_holidays() -> None:
    """Write dbt/seeds/holidays_de.csv (Phase 2)."""
    _not_yet("seed-holidays", 2)


@app.command()
def transform() -> None:
    """Run dbt build and gold quality checks (Phase 2)."""
    _not_yet("transform", 2)


@app.command()
def simulate() -> None:
    """Build households, bill tariffs, shift load (Phases 3-5)."""
    _not_yet("simulate", 3)


@app.command()
def herding() -> None:
    """Aggregate 1,000 households before/after shifting (Phase 6)."""
    _not_yet("herding", 6)


@app.command("app")
def app_() -> None:
    """Start the Streamlit app (Phase 7)."""
    _not_yet("app", 7)


@app.command()
def refresh() -> None:
    """Refresh the latest prices only (Phase 7)."""
    _not_yet("refresh", 7)
