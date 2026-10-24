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
    """Write dbt/seeds/holidays_de.csv with national German holidays of the simulation years."""
    from ets.profiles.calendar import write_holiday_seed

    path = write_holiday_seed(SIM_START.year, SIM_END.year)
    typer.echo(f"Wrote {path}")


@app.command()
def transform() -> None:
    """Stage BDEW, run dbt build (seed, models, tests), then gold checks; exit 1 on any failure."""
    import subprocess

    from ets.transform import transform as run_transform

    try:
        cfg = load_config()
        problems = run_transform(cfg)
    except (ValidationError, FileNotFoundError, ValueError) as e:
        typer.echo(f"Config invalid:\n{e}", err=True)
        raise typer.Exit(1) from e
    except subprocess.CalledProcessError as e:
        typer.echo(f"dbt build failed (exit {e.returncode}); see output above.", err=True)
        raise typer.Exit(1) from e
    if problems:
        for p in problems:
            typer.echo(f"[gold check] {p}", err=True)
        raise typer.Exit(1)
    typer.echo("Transform OK: dbt build green, gold checks passed.")


@app.command()
def households() -> None:
    """Build the household archetypes from the gold tables and write dim_household (Phase 3)."""
    import duckdb

    from ets.profiles.archetypes import household_totals
    from ets.settings import GOLD_DB
    from ets.simulate.context import load_gold_context
    from ets.simulate.run import build_households, write_dim_household

    try:
        cfg = load_config()
    except (ValidationError, FileNotFoundError, ValueError) as e:
        typer.echo(f"Config invalid:\n{e}", err=True)
        raise typer.Exit(1) from e
    if not GOLD_DB.exists():
        typer.echo(f"{GOLD_DB} missing: run `ets transform` first.", err=True)
        raise typer.Exit(1)
    con = duckdb.connect(str(GOLD_DB))
    try:
        ctx = load_gold_context(con)
        built = build_households(cfg, ctx)
        write_dim_household(con, cfg, built)
    except (ValueError, duckdb.Error) as e:
        typer.echo(f"Building households failed: {e}", err=True)
        raise typer.Exit(1) from e
    finally:
        con.close()
    for hh in built:
        t = household_totals(hh)
        typer.echo(
            f"{hh.id:12} base {t['base_kwh']:7.0f}  ev {t['ev_kwh']:6.0f}  "
            f"hp {t['hp_el_kwh']:6.0f} "
            f"(heat {t['hp_heat_kwh']:6.0f}, COP {t['hp_seasonal_cop']:.2f})  "
            f"pv {t['pv_kwh']:6.0f} kWh/yr"
        )
    typer.echo("dim_household written.")


@app.command()
def baseline() -> None:
    """Bill the unshifted households under every tariff and run the type-A gate (Phase 4)."""
    import duckdb

    from ets.settings import GOLD_DB
    from ets.simulate.context import load_gold_context
    from ets.simulate.run import build_households, run_baseline, saving_vs_fixed_pct

    gate_low, gate_high = -2.0, 5.0  # type A dynamic saving must lie in this window (percent)
    try:
        cfg = load_config()
    except (ValidationError, FileNotFoundError, ValueError) as e:
        typer.echo(f"Config invalid:\n{e}", err=True)
        raise typer.Exit(1) from e
    if not GOLD_DB.exists():
        typer.echo(f"{GOLD_DB} missing: run `ets transform` first.", err=True)
        raise typer.Exit(1)
    con = duckdb.connect(str(GOLD_DB), read_only=True)
    try:
        ctx = load_gold_context(con)
    finally:
        con.close()
    results = run_baseline(cfg, ctx, build_households(cfg, ctx))
    typer.echo(f"{'household':12} {'variant':14} {'EUR/year':>9} {'vs fixed':>9}")
    for r in results:
        pct = saving_vs_fixed_pct(results, r.household_id, r.variant_id)
        typer.echo(f"{r.household_id:12} {r.variant_id:14} {r.annual_eur:9.2f} {pct:8.2f}%")
    saving_a = saving_vs_fixed_pct(results, "A_standard", "dynamic")
    ok = gate_low <= saving_a <= gate_high
    typer.echo(
        f"\nType A (A_standard) dynamic vs fixed: {saving_a:.2f}% "
        f"(gate {gate_low}% to {gate_high}%, literature about 1-3%): {'PASS' if ok else 'FAIL'}"
    )
    if not ok:
        raise typer.Exit(1)


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
